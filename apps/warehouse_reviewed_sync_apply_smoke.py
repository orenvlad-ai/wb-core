#!/usr/bin/env python3
"""Read-only CAS and response-loss contract for the held native hourly adapter."""
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from packages.application import warehouse_reviewed_sync_apply as action
from packages.business_time import current_business_date_iso


def refused(fn, reason):
    try:
        fn()
    except action.AdapterError as exc:
        assert reason in str(exc), str(exc)
    else:
        raise AssertionError("expected refusal: " + reason)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = root / "registry_upload_runtime.sqlite3"
        c = sqlite3.connect(db)
        c.executescript("""
        CREATE TABLE sheet_vitrina_v1_warehouse_functional_active(slot INTEGER,version_id TEXT);
        INSERT INTO sheet_vitrina_v1_warehouse_functional_active VALUES(1,'before');
        CREATE TABLE sheet_vitrina_v1_warehouse_functional_versions(
            version_id TEXT,plan_fingerprint TEXT,status TEXT,business_effective_date TEXT);
        CREATE TABLE sheet_vitrina_v1_warehouse_wb_snapshots(version_id TEXT,snapshot_id TEXT,snapshot_date TEXT);
        CREATE TABLE sheet_vitrina_v1_recovery_operations(operation_kind TEXT,plan_fingerprint TEXT,lifecycle_state TEXT);
        CREATE TABLE immutable_source(value TEXT);
        INSERT INTO immutable_source VALUES('original');
        """)
        c.commit()
        day = current_business_date_iso()
        source = {"value": "original"}
        plan = dict(kind="hourly_wb_sync", effective_date=day,
            captured_at=datetime.now(timezone.utc).isoformat(),
            wb_snapshot=dict(snapshot_id="official", snapshot_date=day),
            base_active_version_id="before",
            local_source_digest="sha256:" + action.native._hash(source),
            wb_supply_source_digest="sha256:" + action.native._hash([]),
            new_events=[], targeted_recalc_requests=[],
            historical_correction=dict(required=False, row_count=0))
        plan["plan_fingerprint"] = action.native._fingerprint(plan)
        file = root / "plan.json"
        def request_for(value):
            blob = json.dumps(value).encode()
            file.write_bytes(blob)
            return dict(runtime_dir=str(root), plan_path=str(file),
                        plan_sha256=hashlib.sha256(blob).hexdigest())
        request = request_for(plan)
        assert action.plan_for(request) == plan
        file.write_text("{}")
        refused(lambda: action.plan_for(request), "plan_file_changed")
        request = request_for(plan)
        with action.readonly(db) as ro:
            ro.execute("BEGIN")
            def read_source(conn, **_):
                return {"value": conn.execute("SELECT value FROM immutable_source").fetchone()[0]}
            with patch.object(action.native, "_source_rows", side_effect=read_source), \
                    patch.object(action.native, "_functional_local_source_view", side_effect=lambda x:x), \
                    patch.object(action.native, "_guarded_local_sources", side_effect=lambda x:x), \
                    patch.object(action.native, "_current_wb_supply_revisions", return_value=[]):
                before = action.capture(ro, plan)
                assert before["active_version"] == "before" and ro.total_changes == 0
                stale = dict(plan, effective_date="2000-01-01")
                refused(lambda: action.capture(ro, stale), "current_business_date")
                changed = dict(plan, local_source_digest="sha256:" + "0"*64)
                refused(lambda: action.capture(ro, changed), "local_source_drift")
                assert action.committed(ro, plan) is None
        adapter = action.WarehouseReviewedSyncAdapter()
        with patch.object(action, "target", return_value=root):
            assert adapter.readback(request, "sync-test-operation")["state"] == "not_submitted"
            c.execute("INSERT INTO sheet_vitrina_v1_recovery_operations VALUES(?,?,?)",
                      ("hourly_warehouse_sync", plan["plan_fingerprint"], "mutation_running"))
            c.commit()
            assert adapter.readback(request, "sync-test-operation")["state"] == "ambiguous"
            c.execute("INSERT INTO sheet_vitrina_v1_warehouse_functional_versions VALUES(?,?,?,?)",
                      ("after", plan["plan_fingerprint"], "good", day))
            c.execute("INSERT INTO sheet_vitrina_v1_warehouse_wb_snapshots VALUES(?,?,?)",
                      ("after", "official", day))
            c.commit()
            # A fresh instance after lost response observes the durable native
            # version; it does not submit or require the old active pointer.
            result = action.WarehouseReviewedSyncAdapter().readback(request, "sync-test-operation")
            assert result["state"] == "applied" and result["recovery_pending"] and not result["current_active"]
            c.execute("UPDATE sheet_vitrina_v1_recovery_operations SET lifecycle_state='retained'")
            c.execute("UPDATE sheet_vitrina_v1_warehouse_functional_active SET version_id='after'")
            c.commit()
            result = adapter.readback(request, "sync-test-operation")
            assert result["state"] == "applied" and not result["recovery_pending"] and result["current_active"]
            with patch.object(action, "_held", return_value=nullcontext()):
                preview = adapter.preview(request, "sync-test-operation")
                result = adapter.apply(request, "sync-test-operation", preview)
                assert result["disposition"] == "already_applied"
            c.execute("UPDATE sheet_vitrina_v1_warehouse_wb_snapshots SET snapshot_id='wrong'")
            c.commit()
            refused(lambda: adapter.readback(request, "sync-test-operation"), "snapshot_conflict")
        c.close()
        for field, value, reason in (
            ("new_events", [{"quantity": 1}], "business_events"),
            ("targeted_recalc_requests", [{"id": "pending"}], "targeted_work"),
            ("historical_correction", {"required": True, "row_count": 1}, "historical_correction"),
        ):
            changed = {k:v for k,v in plan.items() if k != "plan_fingerprint"}
            changed[field] = value
            changed["plan_fingerprint"] = action.native._fingerprint(changed)
            altered = request_for(changed)
            refused(lambda: action.plan_for(altered), reason)
    print("warehouse reviewed sync apply smoke: PASS")


if __name__ == "__main__":
    main()

