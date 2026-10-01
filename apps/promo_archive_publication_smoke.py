"""Exact promo archive publication: missing slot, three metrics, TOTAL, CAS, readback."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.production_apply_launcher import execute  # noqa: E402
from apps.production_apply_contract import AdapterError  # noqa: E402
from apps.promo_archive_publication import PromoArchivePublicationAdapter, SKU_METRICS, TOTAL_METRICS, rollback_preview, rollback_apply  # noqa: E402
from apps.sheet_vitrina_v1_promo_live_source_smoke import _write_promo_run_fixture  # noqa: E402
from packages.application.promo_campaign_archive import sync_promo_campaign_archive  # noqa: E402
from packages.application.ready_publication import operational_authority  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402

FIXTURE = ROOT / "artifacts/registry_upload_http_entrypoint/input/registry_upload_bundle__fixture.json"
DAY = "2026-05-03"


def main() -> None:
    with TemporaryDirectory(prefix="promo-archive-publication-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        bundle = json.loads(FIXTURE.read_text(encoding="utf-8"))
        assert runtime.ingest_bundle(bundle, activated_at="2026-05-03T08:00:00Z").status == "accepted"
        ids = [int(item["nm_id"]) for item in bundle["config_v2"] if item["enabled"]]
        _write_promo_run_fixture(
            runtime_dir=runtime_dir, run_name="2026-05-03__fixture",
            promo_folder="2400__2300__promo", promo_id=2400, period_id=2300,
            promo_title="Promo", promo_period_text="03 мая 02:00 -> 03 мая 23:59",
            promo_start_at="2026-05-03T02:00", promo_end_at="2026-05-03T23:59",
            workbook_rows=[{"nm_id": ids[0], "plan_price": 508.0}],
        )
        sync_promo_campaign_archive(runtime_dir)
        runtime.save_temporal_source_slot_snapshot(
            source_key="prices_snapshot", snapshot_date=DAY,
            snapshot_role="accepted_current_snapshot", captured_at="2026-05-03T08:00:00Z",
            payload=SimpleNamespace(kind="success", snapshot_date=DAY, items=[
                SimpleNamespace(nm_id=nm_id, price_seller=508.0, price_seller_discounted=508.0)
                for nm_id in ids
            ]),
        )
        _ready(runtime_dir, ids)
        request = {"runtime_dir": str(runtime_dir), "dates": [DAY]}
        operation = "promo-archive-publication-smoke"
        adapter = PromoArchivePublicationAdapter()
        before = adapter.preview(request, operation)
        assert before == adapter.preview(request, operation), "preview must have deterministic candidate"
        runtime.save_temporal_source_slot_snapshot(
            source_key="prices_snapshot", snapshot_date="2026-05-04",
            snapshot_role="accepted_current_snapshot", captured_at="2026-05-04T08:00:00Z",
            payload=SimpleNamespace(kind="success", snapshot_date="2026-05-04", items=[
                SimpleNamespace(nm_id=nm_id, price_seller=508.0, price_seller_discounted=508.0)
                for nm_id in ids
            ]),
        )
        try:
            adapter.preview({"runtime_dir": str(runtime_dir), "dates": [DAY, "2026-05-04"]}, operation + "-missing-ready")
        except AdapterError as exc:
            assert str(exc) == "promo-ready-date-coverage-incomplete", exc
        else:
            raise AssertionError("every requested date requires a ready target column")
        assert before["scope"]["metric_cells_changed"]["TOTAL"] == 6, before
        assert all(before["scope"]["metric_cells_changed"][metric] == 2 * len(ids) for metric in SKU_METRICS), before
        receipt = execute(
            action="apply", adapter_name="promo_archive_publication_v1", operation_id=operation,
            request=request, expected_prestate=before["prestate_sha256"],
            expected_candidate=before["candidate_sha256"],
        )
        assert receipt["state"] == "applied", receipt
        assert adapter.readback(request, operation)["state"] == "applied"
        assert adapter.preview(request, operation) == before, "applied operation retains exact preview"
        db_path = operational_authority(runtime_dir)[0]
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.execute("PRAGMA query_only=ON")
            slot = conn.execute("SELECT payload_json FROM temporal_source_slot_snapshots WHERE source_key='promo_by_price' AND snapshot_date=? AND snapshot_role='accepted_closed_day_snapshot'", (DAY,)).fetchone()
            exact = conn.execute("SELECT payload_json FROM temporal_source_snapshots WHERE source_key='promo_by_price' AND snapshot_date=?", (DAY,)).fetchone()
            plan = json.loads(conn.execute("SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?", (DAY,)).fetchone()[0])
        assert slot and exact and json.loads(slot[0])["kind"] == "success"
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.execute("PRAGMA query_only=ON")
            current = conn.execute("SELECT payload_json FROM temporal_source_slot_snapshots WHERE source_key='promo_by_price' AND snapshot_date=? AND snapshot_role='accepted_current_snapshot'", (DAY,)).fetchone()
        assert current and json.loads(current[0])["kind"] == "success", "historical today_current needs its accepted role"
        rows = {row[1]: row[2] for sheet in plan["sheets"] if sheet["sheet_name"] == "DATA_VITRINA" for row in sheet["rows"]}
        assert rows[f"SKU:{ids[0]}|promo_participation"] == 1.0
        assert rows[f"SKU:{ids[0]}|promo_count_by_price"] == 1.0
        assert rows[f"SKU:{ids[0]}|promo_entry_price_best"] == 508.0
        assert rows[TOTAL_METRICS["promo_participation"]] == 1.0
        assert rows[TOTAL_METRICS["promo_count_by_price"]] == 1.0
        assert rows[TOTAL_METRICS["promo_entry_price_best"]] == round(508.0 / len(ids), 6)
        assert rows["SKU:999999|unrelated"] == 17.0
        assert Path(receipt["readback"]["backup_path"]).exists()
        published_at = receipt["readback"]["applied_at"]
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("UPDATE temporal_source_slot_snapshots SET captured_at=? WHERE source_key='promo_by_price' AND snapshot_date=? AND snapshot_role='accepted_closed_day_snapshot'", ("2030-01-01T00:00:00Z", DAY))
        try:
            rollback_preview(runtime_dir, operation)
        except AdapterError as exc:
            assert str(exc) == "promo-rollback-after-source-row-drift", exc
        else:
            raise AssertionError("same payload with newer accepted capture must block inverse")
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("UPDATE temporal_source_slot_snapshots SET captured_at=? WHERE source_key='promo_by_price' AND snapshot_date=? AND snapshot_role='accepted_closed_day_snapshot'", (published_at, DAY))
            original_row = conn.execute("SELECT plan_json,refreshed_at FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?", (DAY,)).fetchone()
            conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET refreshed_at=? WHERE as_of_date=?", ("2030-01-01T00:00:00Z", DAY))
        try:
            rollback_preview(runtime_dir, operation)
        except AdapterError as exc:
            assert str(exc) == "promo-rollback-after-ready-metadata-drift", exc
        else:
            raise AssertionError("same plan with newer ready refresh must block inverse")
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET refreshed_at=? WHERE as_of_date=?", (original_row[1], DAY))
            newer_capture = json.loads(original_row[0])
            next(item for sheet in newer_capture["sheets"] if sheet["sheet_name"] == "DATA_VITRINA" for item in sheet["rows"] if item[1] == f"SKU:{ids[0]}|promo_participation")[2] = 0.0
            conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?,refreshed_at=? WHERE as_of_date=?", (json.dumps(newer_capture), "2030-01-01T00:00:00Z", DAY))
        later_readback = adapter.readback(request, operation)
        assert later_readback["state"] == "applied" and later_readback["superseded"] is True, later_readback
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?,refreshed_at=? WHERE as_of_date=?", (original_row[0], original_row[1], DAY))
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("UPDATE temporal_source_slot_snapshots SET payload_json=? WHERE source_key='promo_by_price' AND snapshot_date=? AND snapshot_role='accepted_closed_day_snapshot'", (json.dumps({"kind": "incomplete"}), DAY))
        assert adapter.readback(request, operation)["state"] == "ambiguous", "ledger alone must not claim applied"
        try:
            rollback_preview(runtime_dir, operation)
        except AdapterError as exc:
            assert str(exc) == "promo-rollback-after-target-drift"
        else:
            raise AssertionError("rollback must reject changed target")
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("UPDATE temporal_source_slot_snapshots SET payload_json=? WHERE source_key='promo_by_price' AND snapshot_date=? AND snapshot_role='accepted_closed_day_snapshot'", (exact[0], DAY))
            row = conn.execute("SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?", (DAY,)).fetchone()
            newer = json.loads(row[0])
            next(item for sheet in newer["sheets"] if sheet["sheet_name"] == "DATA_VITRINA" for item in sheet["rows"] if item[1] == "SKU:999999|unrelated")[2] = 19.0
            conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date=?", (json.dumps(newer), DAY))
        assert adapter.readback(request, operation)["state"] == "applied", "unrelated later edit must be preserved"
        rollback = rollback_preview(runtime_dir, operation)
        assert rollback_apply(runtime_dir, operation, rollback["after_target_sha256"])["state"] == "restored"
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.execute("PRAGMA query_only=ON")
            assert conn.execute("SELECT 1 FROM temporal_source_slot_snapshots WHERE source_key='promo_by_price' AND snapshot_date=?", (DAY,)).fetchone() is None
            restored = json.loads(conn.execute("SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?", (DAY,)).fetchone()[0])
        restored_rows = {item[1]: item[2] for sheet in restored["sheets"] if sheet["sheet_name"] == "DATA_VITRINA" for item in sheet["rows"]}
        assert restored_rows["SKU:999999|unrelated"] == 19.0
        assert restored_rows[f"SKU:{ids[0]}|promo_participation"] == ""
    print("promo archive publication smoke passed")


def _ready(runtime_dir: Path, ids: list[int]) -> None:
    db_path = operational_authority(runtime_dir)[0]
    data_rows = [["Unrelated", "SKU:999999|unrelated", 17.0]]
    for nm_id in ids:
        for metric in SKU_METRICS:
            data_rows.append([metric, f"SKU:{nm_id}|{metric}", ""])
    for row_id in TOTAL_METRICS.values():
        data_rows.append([row_id, row_id, ""])
    plan = {
        "plan_version": "fixture", "snapshot_id": "fixture", "date_columns": [DAY],
        "temporal_slots": [{"slot_key": "yesterday_closed", "column_date": DAY}],
        "metadata": {"refresh_diagnostics": {
            "source_slots": [{"source_key": "promo_by_price", "slot_kind": "yesterday_closed",
                              "requested_date": DAY, "status": "incomplete", "rows_accepted": 0}],
            "source_summary": [{"source_key": "promo_by_price", "status_counts": {"incomplete": 1}}],
        }},
        "sheets": [{"sheet_name": "DATA_VITRINA", "rows": data_rows},
                   {"sheet_name": "STATUS", "rows": [["promo_by_price[yesterday_closed]", "incomplete", DAY, "", "", "", "", len(ids), 0, "", "old"]]}],
    }
    with sqlite3.connect(str(db_path)) as conn:
        state = conn.execute("SELECT bundle_version,activated_at FROM registry_upload_current_state WHERE slot=1").fetchone()
        conn.execute("INSERT INTO sheet_vitrina_v1_ready_snapshots(bundle_version,activated_at,as_of_date,snapshot_id,plan_version,refreshed_at,plan_json) VALUES(?,?,?,?,?,?,?)",
                     (state[0], state[1], DAY, "fixture", "fixture", "2026-05-04T08:00:00Z", json.dumps(plan, ensure_ascii=False)))
        earlier = json.loads(json.dumps(plan, ensure_ascii=False))
        earlier["temporal_slots"][0]["slot_key"] = "today_current"
        earlier["metadata"]["refresh_diagnostics"]["source_slots"][0]["slot_kind"] = "today_current"
        earlier["sheets"][1]["rows"][0][0] = "promo_by_price[today_current]"
        conn.execute("INSERT INTO sheet_vitrina_v1_ready_snapshots(bundle_version,activated_at,as_of_date,snapshot_id,plan_version,refreshed_at,plan_json) VALUES(?,?,?,?,?,?,?)",
                     (state[0], state[1], "2026-05-02", "fixture-prior", "fixture", "2026-05-03T08:00:00Z", json.dumps(earlier, ensure_ascii=False)))


if __name__ == "__main__":
    main()
