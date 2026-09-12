"""Read legacy journal compaction without inventing missing change counts."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.warehouse_current_sync_job_smoke import entry_fixture
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock


def legacy_run(entry, payload, source="manual"):
    with warehouse_functional_job_lock(entry.runtime.runtime_dir):
        run_id = entry.warehouse_update_journal.start(trigger_source=source)
        entry.warehouse_update_journal.finish(run_id, status="success", result=payload)
    return run_id


def main():
    with TemporaryDirectory(prefix="warehouse-legacy-status-") as raw:
        entry, effects, _ = entry_fixture(Path(raw))
        journal = entry.warehouse_update_journal
        # Use the existing legacy writer to create its real compacted shape.
        legacy_id = legacy_run(entry, {"diff": {"changed_line_count": 35,
            "lines": [{"warehouse_key": "ff", "nm_id": n} for n in range(35)]},
            "active_version": {"version_id": "fixture-legacy-version"}})
        stored = journal.lookup(public_id=legacy_id, request_scope="fixture_reader")
        assert stored["result"]["diff"]["lines"] == {"item_count": 35, "details_omitted": True}
        for _ in range(55):
            legacy_run(entry, {}, source="hourly")
        # Query-only authorizer also covers exact status's overview diagnostics.
        import packages.application.warehouse_update_journal as module
        original_connect = module._connect
        def readonly_connect(path, *, query_only=False):
            assert query_only, "legacy status attempted a write connection"
            conn = original_connect(path, query_only=True)
            denied = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                      sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_TABLE}
            conn.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK)
            return conn
        with patch.object(module, "_connect", side_effect=readonly_connect), patch.object(module, "ensure_warehouse_update_journal_schema", side_effect=AssertionError("GET bootstrap")):
            result = entry.handle_warehouse_manual_sync_status_request(legacy_id, request_scope="fixture_reader")
        assert result["status"] == "success" and result["run_id"] == legacy_id
        assert result["changed_warehouses"] is None and result["changed_skus"] is None
        assert "подробности изменений не сохранены" in result["user_status"]
        assert result["technical_details"] == stored["result"]
        assert result["functional_version_id"] == "fixture-legacy-version" and not effects
        # Saved exact counters remain authoritative even when lines were omitted.
        explicit_id = legacy_run(entry, {"changed_warehouses": 2, "changed_skus": 7,
            "diff": {"lines": [{"warehouse_key": "ff", "nm_id": n} for n in range(35)]}})
        explicit = entry.handle_warehouse_manual_sync_status_request(explicit_id)
        assert (explicit["changed_warehouses"], explicit["changed_skus"]) == (2, 7)
        empty_id = legacy_run(entry, {"diff": {"changed_line_count": 0, "lines": []}})
        empty = entry.handle_warehouse_manual_sync_status_request(empty_id)
        assert (empty["changed_warehouses"], empty["changed_skus"]) == (0, 0)
        assert "Без изменений" in empty["user_status"]
        # New full results still derive exact distinct counts without compaction.
        full = {**stored, "result": {"diff": {"lines": [{"warehouse_key": "ff", "nm_id": 1},
            {"warehouse_key": "wb", "nm_id": 1}]}}}
        rendered = entry._warehouse_manual_sync_status_payload(full, request_scope="fixture_reader")
        assert (rendered["changed_warehouses"], rendered["changed_skus"]) == (2, 1)
    print("warehouse_legacy_status_smoke: OK; real compacted legacy >50/exact/query-only; missing=null; saved/zero/full counts preserved")


if __name__ == "__main__":
    main()
