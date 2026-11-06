#!/usr/bin/env python3
"""Native schema waits preserve scripts' commit and partial-effect semantics."""

from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sqlite_contention import connect_sqlite, is_sqlite_contention_error

STAMP = "2026-07-26T08:00:00Z"


def _run_executescript_native_wait_contract(runtime_dir: Path) -> None:
    for recover in (True, False):
        path = runtime_dir / f"executescript-{recover}.sqlite3"
        with sqlite3.connect(path) as seed:
            seed.execute("CREATE TABLE events(value TEXT NOT NULL)")
        acquire = threading.Event()
        acquired = threading.Event()
        release = threading.Event()
        failures: list[BaseException] = []

        def blocker() -> None:
            try:
                assert acquire.wait(timeout=3), "script never reached its prelude"
                with sqlite3.connect(path, isolation_level=None) as owner:
                    owner.execute("BEGIN IMMEDIATE")
                    acquired.set()
                    assert release.wait(timeout=3), "script did not release its blocker"
                    owner.commit()
            except BaseException as exc:
                failures.append(exc)

        thread = threading.Thread(target=blocker, daemon=True)
        thread.start()

        def wait_for_writer() -> int:
            acquire.set()
            assert acquired.wait(timeout=2), "script blocker did not acquire its lock"
            if recover:
                threading.Timer(0.65, release.set).start()
            return 0

        conn = connect_sqlite(path, timeout_ms=2_000 if recover else 400)
        try:
            conn.create_function("wait_for_writer", 0, wait_for_writer)
            conn.execute("PRAGMA busy_timeout = 137")
            # executescript historically commits this caller transaction.
            # A fix must neither roll it back nor wrap the script atomically.
            conn.execute("INSERT INTO events VALUES('caller')")
            started_at = time.monotonic()
            try:
                conn.executescript(
                    "INSERT INTO events VALUES('prelude');"
                    "SELECT wait_for_writer();"
                    "INSERT INTO events VALUES('after');"
                )
            except sqlite3.OperationalError as exc:
                if recover or not is_sqlite_contention_error(exc):
                    raise
            else:
                assert recover, "script ignored its configured wait budget"
            elapsed = time.monotonic() - started_at
            assert elapsed >= (0.5 if recover else 0.3), elapsed
            assert elapsed < 2.5, "script wait was not bounded"
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 137
            assert not conn.in_transaction
        finally:
            release.set()
            thread.join(timeout=3)
            conn.close()
        assert not thread.is_alive() and not failures, failures
        with sqlite3.connect(path) as check:
            values = [row[0] for row in check.execute("SELECT value FROM events ORDER BY rowid")]
        assert values == (["caller", "prelude", "after"] if recover else ["caller", "prelude"]), values

    with connect_sqlite(path) as conn:
        conn.execute("PRAGMA busy_timeout = 83")
        try:
            conn.executescript("THIS IS NOT SQL;")
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError("invalid script unexpectedly succeeded")
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 83


def _run_runtime_schema_invalidation_contract(runtime: RegistryUploadDbBackedRuntime) -> None:
    # Unrelated lazy schema owners invalidate the runtime's schema_version
    # cache. The ensuing bootstrap includes backfill DML even on a ready DB.
    with sqlite3.connect(runtime.db_path) as conn:
        conn.execute("CREATE TABLE unrelated_lazy_schema(value TEXT)")
    acquired = threading.Event()
    failures: list[BaseException] = []

    def blocker() -> None:
        try:
            with sqlite3.connect(runtime.db_path, isolation_level=None) as conn:
                conn.execute("BEGIN IMMEDIATE")
                acquired.set()
                time.sleep(0.65)
                conn.commit()
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=blocker, daemon=True)
    thread.start()
    assert acquired.wait(timeout=3), "runtime schema blocker did not acquire its lock"
    try:
        runtime.save_temporal_source_slot_snapshot(
            source_key="stocks", snapshot_date="2026-07-26", snapshot_role="day_open",
            captured_at=STAMP, payload={"source": "schema-contention", "values": [1, 2]},
        )
    finally:
        thread.join(timeout=3)
    assert not thread.is_alive() and not failures, failures
    payload, captured_at = runtime.load_temporal_source_slot_snapshot(
        source_key="stocks", snapshot_date="2026-07-26", snapshot_role="day_open",
    )
    assert payload.source == "schema-contention" and payload.values == [1, 2]
    assert captured_at == STAMP


def main() -> None:
    with TemporaryDirectory(prefix="sqlite-schema-contention-") as directory:
        root = Path(directory)
        _run_executescript_native_wait_contract(root)
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=root / "runtime")
        runtime.list_supplier_shipments()
        _run_runtime_schema_invalidation_contract(runtime)
    print("sqlite_schema_contention_smoke: OK")


if __name__ == "__main__":
    main()
