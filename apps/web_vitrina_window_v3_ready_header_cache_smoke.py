"""Focused identity and memory checks for the ready-header cache."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.web_vitrina_ready_header_cache import ReadyHeaderCache  # noqa: E402
from packages.application.web_vitrina_window_read_context import WindowReadContext  # noqa: E402
from packages.application.web_vitrina_window_v3 import (  # noqa: E402
    _DateBinding, _bound_book_versions, _ready_headers,
)


@dataclass(frozen=True)
class Header:
    bundle: str
    day: str
    columns: tuple[str, ...]


def check_file_identity() -> None:
    with TemporaryDirectory(prefix="window-v3-header-cache-") as directory:
        first_path = Path(directory) / "first.sqlite3"
        second_path = Path(directory) / "second.sqlite3"
        for path, columns in ((first_path, ["2026-04-20"]), (second_path, ["2026-04-19", "2026-04-20"])):
            with sqlite3.connect(path) as conn:
                conn.executescript("""
                  CREATE TABLE registry_upload_current_state (id INTEGER);
                  CREATE TABLE sheet_vitrina_v1_ready_snapshots (
                    bundle_version TEXT,as_of_date TEXT,snapshot_id TEXT,
                    activated_at TEXT,refreshed_at TEXT,plan_json TEXT);
                  CREATE TABLE sheet_vitrina_v1_ready_revisions (
                    bundle_version TEXT,as_of_date TEXT,revision INTEGER);
                """)
                conn.execute("INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES (?,?,?,?,?,?)",
                             ("same-bundle", "2026-04-20", "same-snapshot", "same-activation",
                              "same-refresh", json.dumps({"date_columns": columns})))
                conn.execute("INSERT INTO sheet_vitrina_v1_ready_revisions VALUES (?,?,1)",
                             ("same-bundle", "2026-04-20"))
        calls = [0]

        def decode(conn: sqlite3.Connection) -> list[Header]:
            calls[0] += 1
            row = conn.execute("SELECT bundle_version,as_of_date,json_extract(plan_json,'$.date_columns') "
                               "FROM sheet_vitrina_v1_ready_snapshots").fetchone()
            return [Header(row[0], row[1], tuple(json.loads(row[2])))]

        cache = ReadyHeaderCache()
        with sqlite3.connect(first_path) as first:
            if cache.load(first, decode)[0].columns != ("2026-04-20",):
                raise AssertionError("first file columns wrong")
        with sqlite3.connect(second_path) as second:
            if cache.load(second, decode)[0].columns != ("2026-04-19", "2026-04-20") or calls[0] != 2:
                raise AssertionError("different DB with same ready identity reused cached headers")
        with sqlite3.connect(first_path) as first:
            cache.load(first, decode)
        os.replace(second_path, first_path)
        with sqlite3.connect(first_path) as replacement:
            if cache.load(replacement, decode)[0].columns != ("2026-04-19", "2026-04-20") or calls[0] != 4:
                raise AssertionError("DB replacement at the same path reused cached headers")

        # The old read transaction remains attached to its inode even after
        # replacement. The cached generation must come from pin time, not a
        # fresh stat of the pathname at load time.
        old_path = Path(directory) / "pinned.sqlite3"
        new_path = Path(directory) / "pinned-next.sqlite3"
        for path, columns in ((old_path, ["2026-04-20"]), (new_path, ["2026-04-19", "2026-04-20"])):
            with sqlite3.connect(path) as conn:
                conn.executescript("""
                  CREATE TABLE registry_upload_current_state (id INTEGER);
                  CREATE TABLE sheet_vitrina_v1_ready_snapshots (
                    bundle_version TEXT,as_of_date TEXT,snapshot_id TEXT,
                    activated_at TEXT,refreshed_at TEXT,plan_json TEXT);
                  CREATE TABLE sheet_vitrina_v1_ready_revisions (
                    bundle_version TEXT,as_of_date TEXT,revision INTEGER);
                """)
                conn.execute("INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES (?,?,?,?,?,?)",
                             ("same-bundle", "2026-04-20", "same-snapshot", "same-activation",
                              "same-refresh", json.dumps({"date_columns": columns})))
                conn.execute("INSERT INTO sheet_vitrina_v1_ready_revisions VALUES (?,?,1)",
                             ("same-bundle", "2026-04-20"))
        pinned_cache = ReadyHeaderCache()
        old_context = WindowReadContext(old_path)
        old_context.start()
        try:
            os.replace(new_path, old_path)
            if pinned_cache.load(old_context.borrow(old_path), decode)[0].columns != ("2026-04-20",):
                raise AssertionError("detached pinned connection did not read old headers")
            new_context = WindowReadContext(old_path)
            new_context.start()
            try:
                if pinned_cache.load(new_context.borrow(old_path), decode)[0].columns != ("2026-04-19", "2026-04-20"):
                    raise AssertionError("replacement reused detached pinned headers")
            finally:
                new_context.close()
        finally:
            old_context.close()


def main() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript("""
        CREATE TABLE sheet_vitrina_v1_ready_snapshots (
          bundle_version TEXT,as_of_date TEXT,snapshot_id TEXT,
          activated_at TEXT,refreshed_at TEXT,plan_json TEXT);
        CREATE TABLE sheet_vitrina_v1_ready_revisions (
          bundle_version TEXT,as_of_date TEXT,revision INTEGER);
        CREATE TABLE unrelated(value TEXT);
        """)
        for day in ("2026-04-20", "2026-04-21"):
            conn.execute("INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES (?,?,?,?,?,?)",
                         ("bundle-1", day, "snapshot-" + day, "2026-04-21T00:00:00Z",
                          day + "T12:00:00Z", json.dumps({"date_columns": [day]})))
            conn.execute("INSERT INTO sheet_vitrina_v1_ready_revisions VALUES (?,?,1)", ("bundle-1", day))
        calls = [0]

        def decode(connection: sqlite3.Connection) -> list[Header]:
            calls[0] += 1
            result = connection.execute("""
              SELECT s.bundle_version,s.as_of_date,json_extract(s.plan_json,'$.date_columns'),r.revision
              FROM sheet_vitrina_v1_ready_snapshots s
              LEFT JOIN sheet_vitrina_v1_ready_revisions r
                ON r.bundle_version=s.bundle_version AND r.as_of_date=s.as_of_date
              ORDER BY s.activated_at DESC,s.refreshed_at DESC,s.as_of_date DESC,s.bundle_version DESC
            """).fetchall()
            if any(row[3] is None for row in result):
                raise RuntimeError("missing ready revision")
            return [Header(row[0], row[1], tuple(json.loads(row[2]))) for row in result]

        cache = ReadyHeaderCache()
        first = cache.load(conn, decode)
        if calls[0] != 1 or cache.retained_bytes <= 0 or len(first) != 2:
            raise AssertionError("first header decode was not retained")
        first.clear()
        if len(cache.load(conn, decode)) != 2 or calls[0] != 1:
            raise AssertionError("list mutation damaged cache or identity hit decoded")
        conn.execute("INSERT INTO unrelated VALUES ('unchanged ready headers')")
        cache.load(conn, decode)
        if calls[0] != 1:
            raise AssertionError("unrelated SQLite write invalidated ready headers")

        conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date='2026-04-20'",
                     (json.dumps({"date_columns": ["2026-04-19", "2026-04-20"]}),))
        conn.execute("UPDATE sheet_vitrina_v1_ready_revisions SET revision=revision+1 WHERE as_of_date='2026-04-20'")
        updated = cache.load(conn, decode)
        if calls[0] != 2 or updated[-1].columns != ("2026-04-19", "2026-04-20"):
            raise AssertionError("revision change did not refresh coverage")

        conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET activated_at='2026-04-22T00:00:00Z' WHERE as_of_date='2026-04-20'")
        reordered = cache.load(conn, decode)
        if calls[0] != 3 or reordered[0].day != "2026-04-20":
            raise AssertionError("header reorder was not detected")
        conn.execute("INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES (?,?,?,?,?,?)",
                     ("bundle-1", "2026-04-22", "snapshot-new", "2026-04-22T00:00:00Z",
                      "2026-04-22T12:00:00Z", json.dumps({"date_columns": ["2026-04-22"]})))
        conn.execute("INSERT INTO sheet_vitrina_v1_ready_revisions VALUES (?,?,1)", ("bundle-1", "2026-04-22"))
        if len(cache.load(conn, decode)) != 3 or calls[0] != 4:
            raise AssertionError("insert did not change identity")
        conn.execute("DELETE FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date='2026-04-22'")
        if len(cache.load(conn, decode)) != 2 or calls[0] != 5:
            raise AssertionError("delete did not change identity")

        conn.execute("DELETE FROM sheet_vitrina_v1_ready_revisions WHERE as_of_date='2026-04-20'")
        try:
            cache.load(conn, decode)
        except RuntimeError as error:
            if str(error) != "missing ready revision":
                raise
        else:
            raise AssertionError("missing revision reused stale headers")
        conn.execute("INSERT INTO sheet_vitrina_v1_ready_revisions VALUES (?,?,3)", ("bundle-1", "2026-04-20"))
        cache.load(conn, decode)
        if calls[0] != 7:
            raise AssertionError("cache did not recover after revision restored")

        uncached = ReadyHeaderCache(max_retained_bytes=1)
        uncached.load(conn, decode)
        uncached.load(conn, decode)
        if uncached.retained_bytes != 0 or calls[0] != 9:
            raise AssertionError("cache retained data beyond its cap")
        binding_cache = ReadyHeaderCache()
        key = ("bundle-1", "2026-04-20")
        selected = {_DateBinding("2026-04-20", "exact", "2026-04-20", key)}
        for version in ("bound-v1", "bound-v2"):
            conn.execute(
                "UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date=?",
                (json.dumps({
                    "date_columns": ["2026-04-20"],
                    "metadata": {"fbs_accounting_bindings": {
                        "2026-04-20": {"book_version": version},
                    }},
                }), key[1]),
            )
            conn.execute(
                "UPDATE sheet_vitrina_v1_ready_revisions SET revision=revision+1 WHERE as_of_date=?",
                (key[1],),
            )
            headers = {item.key: item for item in binding_cache.load(conn, _ready_headers)}
            if _bound_book_versions(headers, {key}, list(selected)) != [version]:
                raise AssertionError("revision-bound FBS binding metadata stayed stale")
            if not 0 < binding_cache.retained_bytes <= binding_cache.max_retained_bytes:
                raise AssertionError("FBS binding metadata escaped the shared header cap")
        conn.execute("DROP TABLE sheet_vitrina_v1_ready_revisions")
        try:
            cache.load(conn, decode)
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError("missing revision table reused stale headers")
        check_file_identity()
        print("window_v3_ready_header_cache: identity, fail-closed and cap ok")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
