"""Exact promo archive publication: missing slot, three metrics, TOTAL, CAS, readback."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.production_apply_launcher import execute  # noqa: E402
from apps.production_apply_contract import AdapterError  # noqa: E402
import apps.promo_archive_publication as publication  # noqa: E402
from apps.promo_archive_publication import PromoArchivePublicationAdapter, SKU_METRICS, TOTAL_METRICS, _plan_non_target, geometry_rollback_preview, geometry_rollback_apply, rollback_preview, rollback_apply  # noqa: E402
from apps.sheet_vitrina_v1_promo_live_source_smoke import _write_promo_run_fixture  # noqa: E402
from packages.application.promo_campaign_archive import sync_promo_campaign_archive  # noqa: E402
from packages.application.sheet_vitrina_v1 import parse_sheet_write_plan_payload  # noqa: E402
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
        _assert_reconstruction_determinism(runtime_dir, ids)
        db_path = operational_authority(runtime_dir)[0]
        with sqlite3.connect(str(db_path)) as conn:
            earlier_row = conn.execute("SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?",
                                       ("2026-05-02",)).fetchone()
            invalid = json.loads(earlier_row[0])
            invalid["sheets"][1]["row_count"] = 1
            assert _plan_non_target(invalid, {DAY}) != _plan_non_target(json.loads(earlier_row[0]), {DAY})
            conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date=?",
                         (json.dumps(invalid, ensure_ascii=False), "2026-05-02"))
        try:
            PromoArchivePublicationAdapter().preview({"runtime_dir": str(runtime_dir), "dates": [DAY]}, "invalid-status-count")
        except AdapterError as exc:
            assert str(exc) == "promo-ready-status-row-count-mismatch", exc
        else:
            raise AssertionError("publication must refuse an already invalid STATUS row count")
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date=?",
                         (earlier_row[0], "2026-05-02"))
        old_payload = json.dumps({"kind": "incomplete", "note": "retained prior capture"})
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("INSERT INTO temporal_source_slot_snapshots VALUES(?,?,?,?,?)",
                         ("promo_by_price", DAY, "accepted_current_snapshot", "2026-05-03T07:00:00Z", old_payload))
            conn.execute("INSERT INTO temporal_source_snapshots VALUES(?,?,?,?)",
                         ("promo_by_price", DAY, "2026-05-03T07:00:00Z", old_payload))
            conn.execute("INSERT INTO temporal_source_slot_snapshots VALUES(?,?,?,?,?)",
                         ("promo_by_price", "2026-05-02", "accepted_current_snapshot", "2026-05-02T07:00:00Z", old_payload))
            conn.execute("INSERT INTO temporal_source_slot_snapshots VALUES(?,?,?,?,?)",
                         ("promo_by_price", DAY, "unaccepted_fixture", "2026-05-03T07:00:00Z", old_payload))
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
        with sqlite3.connect(str(db_path)) as conn:
            original_refreshed_at = conn.execute("SELECT refreshed_at FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?", (DAY,)).fetchone()[0]
        real_create_backup = publication._create_scoped_backup

        def backup_then_metadata_refresh(*args: object, **kwargs: object) -> tuple[str, str, str]:
            result = real_create_backup(*args, **kwargs)
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET refreshed_at=? WHERE as_of_date=?",
                             ("2026-05-03T08:01:00Z", DAY))
            return result

        with patch.object(publication, "_create_scoped_backup", side_effect=backup_then_metadata_refresh):
            try:
                execute(action="apply", adapter_name="promo_archive_publication_v1",
                        operation_id=operation + "-metadata-drift", request=request,
                        expected_prestate=before["prestate_sha256"],
                        expected_candidate=before["candidate_sha256"])
            except AdapterError as exc:
                assert str(exc) == "promo-scoped-backup-full-row-drift", exc
            else:
                raise AssertionError("metadata-only refresh must invalidate the retained preimage")
        assert adapter.readback(request, operation + "-metadata-drift")["state"] == "not_submitted"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET refreshed_at=? WHERE as_of_date=?",
                         (original_refreshed_at, DAY))
        receipt = execute(
            action="apply", adapter_name="promo_archive_publication_v1", operation_id=operation,
            request=request, expected_prestate=before["prestate_sha256"],
            expected_candidate=before["candidate_sha256"],
        )
        assert receipt["state"] == "applied", receipt
        assert adapter.readback(request, operation)["state"] == "applied"
        assert adapter.preview(request, operation) == before, "applied operation retains exact preview"
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.execute("PRAGMA query_only=ON")
            slot = conn.execute("SELECT payload_json FROM temporal_source_slot_snapshots WHERE source_key='promo_by_price' AND snapshot_date=? AND snapshot_role='accepted_closed_day_snapshot'", (DAY,)).fetchone()
            exact = conn.execute("SELECT payload_json FROM temporal_source_snapshots WHERE source_key='promo_by_price' AND snapshot_date=?", (DAY,)).fetchone()
            plan = json.loads(conn.execute("SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?", (DAY,)).fetchone()[0])
        assert slot and exact and json.loads(slot[0])["kind"] == "success"
        parse_sheet_write_plan_payload(plan)
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.execute("PRAGMA query_only=ON")
            current = conn.execute("SELECT payload_json FROM temporal_source_slot_snapshots WHERE source_key='promo_by_price' AND snapshot_date=? AND snapshot_role='accepted_current_snapshot'", (DAY,)).fetchone()
            earlier_plan = json.loads(conn.execute("SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?", ("2026-05-02",)).fetchone()[0])
        assert current and json.loads(current[0])["kind"] == "success", "historical today_current needs its accepted role"
        parse_sheet_write_plan_payload(earlier_plan)
        assert earlier_plan["sheets"][1]["row_count"] == len(earlier_plan["sheets"][1]["rows"]) == 1
        assert earlier_plan["sheets"][1]["write_rect"] == "A1:K2"
        rows = {row[1]: row[2] for sheet in plan["sheets"] if sheet["sheet_name"] == "DATA_VITRINA" for row in sheet["rows"]}
        assert rows[f"SKU:{ids[0]}|promo_participation"] == 1.0
        assert rows[f"SKU:{ids[0]}|promo_count_by_price"] == 1.0
        assert rows[f"SKU:{ids[0]}|promo_entry_price_best"] == 508.0
        assert rows[TOTAL_METRICS["promo_participation"]] == 1.0
        assert rows[TOTAL_METRICS["promo_count_by_price"]] == 1.0
        assert rows[TOTAL_METRICS["promo_entry_price_best"]] == round(508.0 / len(ids), 6)
        assert rows["SKU:999999|unrelated"] == 17.0
        backup_path = Path(receipt["readback"]["backup_path"])
        assert backup_path.exists()
        with sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True) as backup:
            backup.execute("PRAGMA query_only=ON")
            tables = {row[0] for row in backup.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert tables == {"temporal_source_slot_snapshots", "temporal_source_snapshots",
                              "sheet_vitrina_v1_ready_snapshots", "promo_archive_scoped_backup_manifest"}, tables
            saved_slots = backup.execute("SELECT source_key,snapshot_date,snapshot_role,payload_json FROM temporal_source_slot_snapshots").fetchall()
            assert saved_slots == [("promo_by_price", DAY, "accepted_current_snapshot", old_payload)], saved_slots
            assert backup.execute("SELECT source_key,snapshot_date,payload_json FROM temporal_source_snapshots").fetchall() == [("promo_by_price", DAY, old_payload)]
            assert {row[0] for row in backup.execute("SELECT as_of_date FROM sheet_vitrina_v1_ready_snapshots")} == {"2026-05-02", DAY}
            manifest = json.loads(backup.execute("SELECT manifest_json FROM promo_archive_scoped_backup_manifest").fetchone()[0])
            assert manifest["operation_id"] == operation
            assert manifest["before_target_sha256"] == receipt["readback"]["before_target_sha256"]
            assert manifest["target_keys"]["slots"] == [[DAY, "accepted_closed_day_snapshot"], [DAY, "accepted_current_snapshot"]]
            assert manifest["present_keys"]["slots"] == [[DAY, "accepted_current_snapshot"]]
            assert manifest["present_keys"]["exact"] == [DAY]
        assert "sha256:" + hashlib.sha256(backup_path.read_bytes()).hexdigest() == receipt["readback"]["backup_sha256"]
        original_backup = backup_path.read_bytes()
        with sqlite3.connect(str(backup_path)) as backup:
            backup.execute("UPDATE promo_archive_scoped_backup_manifest SET manifest_json=?", ("{}",))
        try:
            rollback_preview(runtime_dir, operation)
        except AdapterError as exc:
            assert str(exc) == "promo-rollback-backup-file-drift", exc
        else:
            raise AssertionError("inverse must reject backup bytes changed after publication")
        backup_path.write_bytes(original_backup)
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
            earlier_now = json.loads(conn.execute("SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?",
                                                  ("2026-05-02",)).fetchone()[0])
            unrelated_status = ["prices_snapshot[today_current]", "success", DAY, "", "", "", "", 1, 1, "", "later unrelated status"]
            earlier_now["sheets"][1]["rows"].append(unrelated_status)
            earlier_now["sheets"][1]["row_count"] = 2
            earlier_now["sheets"][1]["write_rect"] = "A1:K3"
            conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date=?",
                         (json.dumps(earlier_now), "2026-05-02"))
        assert adapter.readback(request, operation)["state"] == "applied", "unrelated later edit must be preserved"
        rollback = rollback_preview(runtime_dir, operation)
        assert rollback_apply(runtime_dir, operation, rollback["after_target_sha256"])["state"] == "restored"
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.execute("PRAGMA query_only=ON")
            assert conn.execute("SELECT payload_json FROM temporal_source_slot_snapshots WHERE source_key='promo_by_price' AND snapshot_date=? AND snapshot_role='accepted_current_snapshot'", (DAY,)).fetchone()[0] == old_payload
            assert conn.execute("SELECT 1 FROM temporal_source_slot_snapshots WHERE source_key='promo_by_price' AND snapshot_date=? AND snapshot_role='accepted_closed_day_snapshot'", (DAY,)).fetchone() is None
            assert conn.execute("SELECT payload_json FROM temporal_source_snapshots WHERE source_key='promo_by_price' AND snapshot_date=?", (DAY,)).fetchone()[0] == old_payload
            restored = json.loads(conn.execute("SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?", (DAY,)).fetchone()[0])
            restored_earlier = json.loads(conn.execute("SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?", ("2026-05-02",)).fetchone()[0])
        parse_sheet_write_plan_payload(restored)
        parse_sheet_write_plan_payload(restored_earlier)
        assert restored_earlier["sheets"][1]["rows"] == [unrelated_status]
        assert restored_earlier["sheets"][1]["row_count"] == 1
        assert restored_earlier["sheets"][1]["write_rect"] == "A1:K2"
        restored_rows = {item[1]: item[2] for sheet in restored["sheets"] if sheet["sheet_name"] == "DATA_VITRINA" for item in sheet["rows"]}
        assert restored_rows["SKU:999999|unrelated"] == 19.0
        assert restored_rows[f"SKU:{ids[0]}|promo_participation"] == ""
        _assert_geometry_repair(runtime_dir, db_path)
    print("promo archive publication smoke passed")


def _assert_geometry_repair(runtime_dir: Path, db_path: Path) -> None:
    as_of = "2026-05-02"
    with sqlite3.connect(str(db_path)) as conn:
        old = json.loads(conn.execute("SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?", (as_of,)).fetchone()[0])
        status = next(sheet for sheet in old["sheets"] if sheet["sheet_name"] == "STATUS")
        status["rows"].append(["promo_by_price[today_current]", "success", as_of, "", "", "", "", 1, 1, "", "retained"])
        malformed = json.dumps(old, ensure_ascii=False, separators=(",", ":"))
        conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date=?", (malformed, as_of))
    request = {"mode": "repair_ready_geometry", "runtime_dir": str(runtime_dir), "as_of_date": as_of,
               "expected_status_row_count": 1, "expected_write_rect": "A1:K2"}
    adapter = PromoArchivePublicationAdapter()
    preview = adapter.preview(request, "geometry-fixture")
    assert preview == adapter.preview(request, "geometry-fixture")
    assert preview["scope"]["metric_cells_changed"] == preview["scope"]["source_rows_changed"] == 0
    with sqlite3.connect(str(db_path)) as conn:
        original_row = tuple(conn.execute("SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?", (as_of,)).fetchone())
        source_before = [tuple(row) for table in ("temporal_source_slot_snapshots", "temporal_source_snapshots")
                         for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1,2")]
        conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET refreshed_at=? WHERE as_of_date=?",
                     ("2026-05-03T08:01:00Z", as_of))
    try:
        adapter.apply(request, "geometry-fixture", preview)
    except AdapterError as exc:
        assert str(exc) == "promo-geometry-preview-drift", exc
    else:
        raise AssertionError("geometry repair must reject metadata-only ready drift")
    assert adapter.readback(request, "geometry-fixture")["state"] == "not_submitted"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET refreshed_at=? WHERE as_of_date=?",
                     (original_row[5], as_of))
    result = adapter.apply(request, "geometry-fixture", preview)
    assert result["disposition"] == "submitted"
    readback = adapter.readback(request, "geometry-fixture")
    assert readback["state"] == "applied" and not readback["superseded"]
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        after_row = tuple(conn.execute("SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?", (as_of,)).fetchone())
        source_after = [tuple(row) for table in ("temporal_source_slot_snapshots", "temporal_source_snapshots")
                        for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1,2")]
    assert source_after == source_before
    assert after_row[:6] == original_row[:6]
    before_plan = json.loads(original_row[6])
    after_plan = json.loads(after_row[6])
    before_status = next(sheet for sheet in before_plan["sheets"] if sheet["sheet_name"] == "STATUS")
    after_status = next(sheet for sheet in after_plan["sheets"] if sheet["sheet_name"] == "STATUS")
    assert (before_status["row_count"], before_status["write_rect"], len(before_status["rows"])) == (1, "A1:K2", 2)
    assert (after_status["row_count"], after_status["write_rect"], len(after_status["rows"])) == (2, "A1:K3", 2)
    parse_sheet_write_plan_payload(after_plan)
    for status in (before_status, after_status):
        status.pop("row_count")
        status.pop("write_rect")
    assert before_plan == after_plan, "only STATUS geometry may change"
    backup = Path(readback["backup_path"])
    with sqlite3.connect(f"file:{backup}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        assert conn.execute("SELECT COUNT(*) FROM temporal_source_slot_snapshots").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM temporal_source_snapshots").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_ready_snapshots").fetchone()[0] == 1
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET refreshed_at=? WHERE as_of_date=?",
                     ("2026-05-03T08:02:00Z", as_of))
    try:
        geometry_rollback_preview(runtime_dir, "geometry-fixture")
    except AdapterError as exc:
        assert str(exc) == "promo-geometry-rollback-after-row-drift", exc
    else:
        raise AssertionError("geometry inverse must refuse a later full-row write")
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET refreshed_at=? WHERE as_of_date=?",
                     (after_row[5], as_of))
    inverse = geometry_rollback_preview(runtime_dir, "geometry-fixture")
    assert geometry_rollback_apply(runtime_dir, "geometry-fixture", inverse["after_row_sha256"])["state"] == "restored"
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        assert tuple(conn.execute("SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?", (as_of,)).fetchone()) == original_row
    assert adapter.readback(request, "geometry-fixture")["state"] == "restored"



def _seed_reconstruction_fixture(runtime_dir: Path, ids: list[int]) -> None:
    """Original exact-day identity and price checkpoint; no materialization."""
    db_path = operational_authority(runtime_dir)[0]
    run_dir = runtime_dir / "promo_xlsx_collector_runs" / "2026-05-03__fixture"
    raw_dir = run_dir / "promos" / "2400__2300__promo"
    archive_dir = runtime_dir / "promo_campaign_archive" / "2400__2300__promo"
    workbook = archive_dir / "workbook.xlsx"
    observed_at = "2026-05-03T03:00:00Z"
    workbook_time = datetime.fromisoformat("2026-05-03T08:30:00+05:00").timestamp()
    os.utime(workbook, (workbook_time, workbook_time))
    (raw_dir / "archive_reuse.json").write_text(json.dumps({
        "archive_key": archive_dir.name, "reused_workbook_path": str(workbook),
        "downloaded_at": "2026-05-03T08:30:00+05:00",
    }), encoding="utf-8")
    raw_metadata = json.loads((raw_dir / "metadata.json").read_text())
    raw_metadata.update(ui_loaded_success=True, campaign_identity_match=True)
    (raw_dir / "metadata.json").write_text(json.dumps(raw_metadata))
    (run_dir / "run_summary.json").write_text(json.dumps({
        "run_dir": str(run_dir), "status": "partial", "started_at": "2026-05-03T09:00:00+05:00",
        "timeline_candidates_found": 1, "card_confirmed_count": 1, "blocked_before_card_count": 0,
        "hydration_attempts": [{"hydrated_success": True, "timeline_count": 1}],
        "promos": [{"promo_id": 2400, "timeline_block_index": 0, "promo_title": "Promo",
                    "status": "reused_archive", "metadata_path": str(raw_dir / "metadata.json"),
                    "saved_path": str(workbook),
                    "metadata": raw_metadata}],
    }), encoding="utf-8")
    evidence_digest = "sha256:" + "0" * 64
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS change_registry_checkpoints(checkpoint_id TEXT PRIMARY KEY,started_at TEXT,completed_at TEXT,completeness_status TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS change_registry_checkpoint_source_manifests(checkpoint_id TEXT,source_name TEXT,completeness_status TEXT,expected_count INTEGER,observed_count INTEGER,evidence_digest TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS change_registry_observation_values(checkpoint_id TEXT,target_kind TEXT,parameter_field TEXT,nm_id INTEGER,observation_status TEXT,value_kind TEXT,value_integer INTEGER,observed_at TEXT,evidence_digest TEXT)")
        conn.execute("INSERT INTO change_registry_checkpoints(checkpoint_id,seller_id,account_scope,source_surface,scan_kind,started_at,completed_at,completeness_status,expected_target_count,observed_target_count,completeness_digest,evidence_digest,mapping_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("crcp_fixture", "seller-fixture", "fixture", "wb_prices_ads_joint", "observer", observed_at, observed_at, "complete", len(ids), len(ids), evidence_digest, evidence_digest, "wb_change_registry_mapping_v1"))
        conn.execute("INSERT INTO change_registry_checkpoint_source_manifests(source_manifest_id,checkpoint_id,source_name,completeness_status,expected_count,observed_count,summary_json,evidence_digest,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     ("crsm_fixture", "crcp_fixture", "prices", "complete", len(ids), len(ids), "{}", evidence_digest, observed_at))
        conn.executemany("INSERT INTO change_registry_observation_values(observation_value_id,checkpoint_id,target_kind,nm_id,advert_id,placement,parameter_field,observation_status,value_kind,value_integer,health_code,health_detail,observed_at,evidence_digest,mapping_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         [(f"crobs_fixture_{nm_id}", "crcp_fixture", "price", nm_id, 0, "", "seller_price_minor", "exact", "integer", 50800, "", "", observed_at, evidence_digest, "wb_change_registry_mapping_v1") for nm_id in ids])


def _assert_reconstruction_determinism(runtime_dir: Path, ids: list[int]) -> None:
    """Independent processes must pin identical original checkpoint rows."""
    _seed_reconstruction_fixture(runtime_dir, ids)
    observed_at = "2026-05-03T03:00:00Z"
    evidence_digest = "sha256:" + "0" * 64
    child = """import json,sys
from pathlib import Path
from apps.promo_archive_publication import _candidate
r=Path(sys.argv[1]);d='2026-05-03';c=_candidate(r,[d],{d:{'identity_run':'2026-05-03__fixture','price_checkpoint_id':'crcp_fixture'}})
summary=json.loads(c['ready_updates'][0]['plan_json'])['metadata']['refresh_diagnostics']['source_summary'][0]
print(json.dumps({'candidate':c['candidate_sha'],'fingerprint':c['results'][d]['diagnostics']['fingerprints']['accepted_price_truth_fingerprint'],'rows':c['reconstruction_proof'][d]['price_rows_sha256'],'status_counts':summary['status_counts'],'origin_counts':summary['origin_counts']}))
"""
    observed = []
    for seed in ("1", "2"):
        result = subprocess.run([sys.executable, "-c", child, str(runtime_dir)], cwd=ROOT,
                                env={**os.environ, "PYTHONHASHSEED": seed},
                                text=True, capture_output=True, check=True)
        observed.append(json.loads(result.stdout))
    expected_rows = [[nm_id, "exact", "integer", 50800, observed_at, evidence_digest] for nm_id in sorted(ids)]
    expected = "sha256:" + hashlib.sha256(json.dumps(expected_rows, ensure_ascii=False, sort_keys=True,
                                                     separators=(",", ":")).encode()).hexdigest()
    assert observed[0] == observed[1], observed
    assert observed[0]["fingerprint"] == observed[0]["rows"] == expected, observed
    assert len(observed[0]["status_counts"]) == len(observed[0]["origin_counts"]) == 2, observed


def _ready(runtime_dir: Path, ids: list[int]) -> None:
    db_path = operational_authority(runtime_dir)[0]
    data_rows = [["Unrelated", "SKU:999999|unrelated", 17.0]]
    for nm_id in ids:
        for metric in SKU_METRICS:
            data_rows.append([metric, f"SKU:{nm_id}|{metric}", ""])
    for row_id in TOTAL_METRICS.values():
        data_rows.append([row_id, row_id, ""])
    plan = {
        "plan_version": "fixture", "snapshot_id": "fixture", "as_of_date": DAY, "date_columns": [DAY],
        "temporal_slots": [{"slot_key": "yesterday_closed", "slot_label": "closed", "column_date": DAY}],
        "metadata": {"refresh_diagnostics": {
            "source_slots": [{"source_key": "promo_by_price", "slot_kind": "yesterday_closed",
                              "requested_date": DAY, "status": "incomplete", "rows_accepted": 0},
                             {"source_key": "promo_by_price", "slot_kind": "older_fixture",
                              "requested_date": "2026-05-01", "status": "partial", "origin": "earlier_fixture",
                              "rows_accepted": 0}],
            "source_summary": [{"source_key": "promo_by_price", "status_counts": {"incomplete": 1}}],
        }},
        "sheets": [{"sheet_name": "DATA_VITRINA", "header": ["label", "key", DAY],
                    "rows": data_rows, "row_count": len(data_rows), "column_count": 3,
                    "write_start_cell": "A1", "write_rect": f"A1:C{len(data_rows) + 1}", "clear_range": "A:C",
                    "write_mode": "replace", "partial_update_allowed": False},
                   {"sheet_name": "STATUS", "header": [f"column_{index}" for index in range(11)],
                    "rows": [["promo_by_price[yesterday_closed]", "incomplete", DAY, "", "", "", "", len(ids), 0, "", "old"]],
                    "row_count": 1, "column_count": 11,
                    "write_start_cell": "A1", "write_rect": "A1:K2", "clear_range": "A:K",
                    "write_mode": "replace", "partial_update_allowed": False}],
    }
    with sqlite3.connect(str(db_path)) as conn:
        state = conn.execute("SELECT bundle_version,activated_at FROM registry_upload_current_state WHERE slot=1").fetchone()
        conn.execute("INSERT INTO sheet_vitrina_v1_ready_snapshots(bundle_version,activated_at,as_of_date,snapshot_id,plan_version,refreshed_at,plan_json) VALUES(?,?,?,?,?,?,?)",
                     (state[0], state[1], DAY, "fixture", "fixture", "2026-05-04T08:00:00Z", json.dumps(plan, ensure_ascii=False)))
        earlier = json.loads(json.dumps(plan, ensure_ascii=False))
        earlier["as_of_date"] = "2026-05-02"
        earlier["temporal_slots"][0]["slot_key"] = "today_current"
        earlier["temporal_slots"][0]["slot_label"] = "current"
        earlier["metadata"]["refresh_diagnostics"]["source_slots"][0]["slot_kind"] = "today_current"
        earlier["sheets"][1]["rows"] = []
        earlier["sheets"][1]["row_count"] = 0
        earlier["sheets"][1]["write_rect"] = "A1:K1"
        conn.execute("INSERT INTO sheet_vitrina_v1_ready_snapshots(bundle_version,activated_at,as_of_date,snapshot_id,plan_version,refreshed_at,plan_json) VALUES(?,?,?,?,?,?,?)",
                     (state[0], state[1], "2026-05-02", "fixture-prior", "fixture", "2026-05-03T08:00:00Z", json.dumps(earlier, ensure_ascii=False)))


if __name__ == "__main__":
    main()
