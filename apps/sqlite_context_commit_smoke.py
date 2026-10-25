#!/usr/bin/env python3
"""Standalone SQLite commit contention and atomic rollback regression checks."""
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.sqlite_contention import SQLiteContentionExhausted, connect_sqlite


def _assert_closed(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return
    raise AssertionError("context exit left its connection open")


def _run_context_commit_contract(runtime_dir: Path) -> None:
    path = runtime_dir / "context-commit.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("CREATE TABLE counters(value INTEGER NOT NULL)")
        conn.execute("INSERT INTO counters VALUES(0)")

    # A reader permits UPDATE but delays COMMIT. Release it only after the
    # first physical busy timeout; retry must not replay the UPDATE.
    reader_ready = threading.Event()
    begin_commit = threading.Event()
    failures: list[BaseException] = []

    def hold_reader() -> None:
        try:
            with sqlite3.connect(path) as reader:
                reader.execute("BEGIN")
                reader.execute("SELECT value FROM counters").fetchall()
                reader_ready.set()
                if not begin_commit.wait(timeout=3):
                    raise AssertionError("writer did not reach commit")
                time.sleep(0.8)
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=hold_reader, daemon=True)
    thread.start()
    try:
        if not reader_ready.wait(timeout=3):
            raise AssertionError("reader lock unavailable")
        with connect_sqlite(path, timeout_ms=3_000) as writer:
            writer.execute("UPDATE counters SET value=value+1")
            begin_commit.set()
    finally:
        begin_commit.set()
        thread.join(timeout=4)
    if thread.is_alive() or failures:
        raise AssertionError(f"reader fixture failed: {failures}")
    _assert_closed(writer)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM counters").fetchone()[0] == 1

    # Exhausted COMMIT rolls back every business write, closes the connection,
    # and exposes the controlled retryable error instead of a raw BUSY error.
    with sqlite3.connect(path) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT value FROM counters").fetchall()
        try:
            with connect_sqlite(path, timeout_ms=600) as writer:
                writer.execute("UPDATE counters SET value=value+10")
                writer.execute("INSERT INTO counters VALUES(99)")
        except SQLiteContentionExhausted as exc:
            assert exc.phase == "commit"
        else:
            raise AssertionError("commit should exhaust its bounded wait")
        _assert_closed(writer)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM counters").fetchall() == [(1,)]

    # Exceptions inside the business body still roll back and propagate the
    # same exception, including interruption; they must never commit a prefix.
    for failure in (ValueError("body failed"), KeyboardInterrupt()):
        try:
            with connect_sqlite(path) as writer:
                writer.execute("UPDATE counters SET value=value+100")
                raise failure
        except BaseException as exc:
            assert exc is failure
        _assert_closed(writer)
    with connect_sqlite(path) as conn:
        assert conn.execute("SELECT value FROM counters").fetchall() == [(1,)]
    _assert_closed(conn)



def main() -> None:
    with TemporaryDirectory(prefix="sqlite-context-commit-") as directory:
        _run_context_commit_contract(Path(directory))
    print("sqlite_context_commit_smoke: OK")


if __name__ == "__main__":
    main()
