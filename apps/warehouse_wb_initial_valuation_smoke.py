#!/usr/bin/env python3
"""Initial WB valuation, truthful partial publication and immutable replay."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.warehouse_functional import (
    WarehouseFunctionalBlock, WarehouseFunctionalError, WarehouseLine, STAGE_WB,
    FUNCTIONAL_CUTOVER_ID, _line_payload, _summaries, _total_summary, _fingerprint,
    _daily_wb_cost_row, _date_range, _balance_public, _line_from_payload,
)
from packages.application.wb_initial_fbs_cost import resolve, valid_anchor

DAY = "2026-10-08"
NOW = DAY + "T10:00:00Z"


def basis(previous):
    return {"available": True, "business_date": DAY, "wb_version_id": previous,
        "prepared_at": DAY + "T09:00:00Z", "book_version": "sha256:accepted", "period_digest": "sha256:period",
        "official_fbs_generation_id": "accepted-qty", "official_fbs_generation_digest": "sha256:accepted-qty",
        "verified_fbs_generation_id": "current-qty", "verified_fbs_generation_digest": "sha256:current-qty",
        "rows": [{"nm_id": 200, "facility_id": facility, "quantity": quantity,
            "cost_mass_quantity": mass, "wac_rub": wac, "quality": "preliminary_snapshot_wac",
            "document_proofs": [{"document_id": "receipt-"+facility, "fingerprint": "sha256:receipt"}],
            "official_facility_evidence": {"facility_id": facility, "warehouse_ids": [1]},
            "accepted_official_facility_evidence": {"facility_id": facility, "warehouse_ids": [1]}}
            for facility, quantity, mass, wac in [("a", "0", "1", "100"), ("b", "3", "3", "200")]]}


def payload(physical="0", transit="2", fetched=NOW):
    items = [{"nm_id": 104, "quantity": "1", "in_way_to_client": "0", "in_way_from_client": "0"},
        {"nm_id": 200, "quantity": physical, "in_way_to_client": transit, "in_way_from_client": "0"},
        {"nm_id": 201, "quantity": "0", "in_way_to_client": "1", "in_way_from_client": "0"}]
    for item in items:
        item["wb_contour_quantity"] = str(int(item["quantity"]) + int(item["in_way_to_client"]) + int(item["in_way_from_client"]))
    rows = [{"nmId": i["nm_id"], "warehouseId": 0, "warehouseName": "WB",
        "stockCount": int(i["quantity"]), "inWayToClient": int(i["in_way_to_client"]), "inWayFromClient": 0} for i in items]
    return {"snapshot_date": fetched[:10], "requested_nm_ids": [104, 200, 201], "canonical_items": items,
        "data": {"fetched_at": fetched, "pagination_complete": True, "page_count": 1,
            "page_offsets": [0], "raw_rows": rows}}


def seed(block):
    """Install a tiny reviewed cutover through the real publication boundary."""
    # The canonical baseline is a local fixture, with no supplier documents.
    with sqlite3.connect(block.runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        from packages.application.fulfillment_services import FulfillmentServicesBlock
        FulfillmentServicesBlock(runtime=block.runtime)._ensure_service_schema(conn)
        conn.execute("""INSERT INTO sheet_vitrina_v1_canonical_cost_baseline_versions
            VALUES('fixture',1,'2026-07-01','fixture-primary','2026-07-01','1',1,'10',0,0,'sha256:fixture','{}',1,?,NULL)""", (NOW,))
    line = WarehouseLine(STAGE_WB, 104, Decimal(1), Decimal(10), Decimal(1), "direct_24_06", {})
    snapshot = {"snapshot_id": "seed", "fetched_at": NOW, "snapshot_date": DAY, "requested_nm_ids": [104],
        "pagination_complete": True, "page_count": 1, "page_offsets": [0], "raw_row_count": 1,
        "raw_rows_digest": "sha256:seed", "raw_rows": [], "items": [payload()["canonical_items"][0]]}
    plan = {"contract_name": "sheet_vitrina_v1_warehouse_functional", "contract_version": "v2",
        "kind": "functional_cutover", "cutover_id": FUNCTIONAL_CUTOVER_ID, "status": "dry_run_ready",
        "captured_at": NOW, "effective_date": DAY, "base_active_version_id": "",
        "local_source_digest": block._local_source_digest(recovery_end_date=DAY),
        "wb_supply_source_digest": block._wb_supply_source_digest(), "source_watermarks": {}, "absorbed_supply_revisions": {},
        "wb_snapshot": snapshot, "opening_cost_map": [{"nm_id": 104, "ff_unit_cost_rub": "10", "wb_unit_cost_rub": "10",
            "quality": "direct_24_06", "provenance": {}, "fingerprint": "sha256:seed-cost"}],
        "lines": [_line_payload(line)], "summaries": _summaries([line]), "new_events": [],
        "movement_documents": [], "supplier_cost_states": [], "unmatched_doprinato": [],
        "historical_wb_cost_projection": [_daily_wb_cost_row(day=day, nm_id=104, quantity=Decimal(1), wac=Decimal(10),
            quality="periodic_snapshot_wac_closed", provenance={}) for day in _date_range("2026-07-01", DAY)],
        "invariants": {}, "diff": {}}
    plan["plan_fingerprint"] = _fingerprint(plan)
    return block.apply_plan(plan, confirm_fingerprint=plan["plan_fingerprint"], backup_dir=block.runtime.runtime_dir / "backup")


def test_saved_fbs_sources():
    # Exercise the real official registry reader, saved document normalization,
    # candidate row shape and immutable packed book, with no basis mock.
    from types import SimpleNamespace
    from apps.fbs_snapshot_cost_sources_smoke import fixture, document, NOW as source_now, DAY as source_day
    from packages.application.fbs_snapshot_cost_sources import capture_current
    from packages.application.fbs_snapshot_cost import initialize_candidate, evaluate_candidate
    from packages.application import fbs_accounting_runtime as accounting
    from packages.application.wb_initial_fbs_cost_sources import capture as capture_basis
    with tempfile.TemporaryDirectory(prefix="wb-initial-source-") as tmp:
        root = Path(tmp)
        operational = root / "fixture.sqlite3"
        conn = fixture(operational)
        document(conn, "receipt-A", "china_acceptance", role="accepted_pool_allocation", q=10, capital="200", facility="A")
        document(conn, "receipt-B", "china_acceptance", role="accepted_pool_allocation", q=20, capital="600", facility="B")
        conn.commit(); conn.close()
        captured = capture_current(operational, now=source_now)
        assert captured["documents_complete"] and captured["quantity_snapshot"]["complete"] and captured["baseline_costs"]["available"]
        state = evaluate_candidate(initialize_candidate(captured, open_initial_day=True), captured)
        book = {"schema": accounting.SCHEMA, "active": True, "effective_date": source_day,
            "state": state, "prepared_at": captured["captured_at"], "source_digest": captured["source_digest"],
            "wb_days": {source_day: {"version_id": "v1"}}, "shared_days": {}, "retained_days": {}, "presentations": {}}
        accounting._save_book(root, book, expected=None, operation_id="fixture")
        runtime = SimpleNamespace(runtime_dir=root, db_path=operational)
        source = capture_basis(runtime, day=source_day, fetched_at=source_now.isoformat(), previous_version="v1")
        assert source["available"], source
        priced = resolve(1, {"quantity": "0", "in_way_to_client": "1", "in_way_from_client": "0"}, source,
            business_date=source_day, snapshot_id="real-format-snapshot", fetched_at=source_now.isoformat(), previous_version="v1")
        assert priced["available"] and valid_anchor(priced["anchor"]), priced
        assert len(priced["anchor"]["facilities"]) == 2
        assert Decimal(priced["wac_rub"]) == Decimal("26.25")
        with sqlite3.connect(operational) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_wb_fbs_warehouse_registry_runs SET generation_digest='sha256:updated-generation'")
            conn.execute("UPDATE sheet_vitrina_v1_wb_fbs_stock_snapshot_rows SET amount=1 WHERE nm_id=1")
        refreshed = capture_basis(runtime, day=source_day, fetched_at=source_now.isoformat(), previous_version="v1")
        assert refreshed["available"], refreshed
        assert refreshed["official_fbs_generation_digest"] == source["official_fbs_generation_digest"]
        assert refreshed["verified_fbs_generation_digest"] != source["verified_fbs_generation_digest"]
        from packages.application.wb_fbs_warehouse_registry import WAREHOUSE_MAPPINGS_TABLE
        with sqlite3.connect(operational) as conn:
            conn.execute("UPDATE " + WAREHOUSE_MAPPINGS_TABLE + " SET active=0 WHERE facility_id='A'")
        assert not capture_basis(runtime, day=source_day, fetched_at=source_now.isoformat(), previous_version="v1")["available"]
        with sqlite3.connect(operational) as conn:
            conn.execute("UPDATE " + WAREHOUSE_MAPPINGS_TABLE + " SET active=1 WHERE facility_id='A'")
        reprice = resolve(1, {"quantity": "0", "in_way_to_client": "1", "in_way_from_client": "0"}, refreshed,
            business_date=source_day, snapshot_id="new-snapshot", fetched_at=source_now.isoformat(), previous_version="v1")
        assert reprice["available"] and reprice["wac_rub"] == priced["wac_rub"]
        with sqlite3.connect(operational) as conn:
            document(conn, "pending", "china_acceptance", role="accepted_pool_allocation", q=1, capital="100", facility="A")
        assert capture_basis(runtime, day=source_day, fetched_at=source_now.isoformat(), previous_version="v1")["reason"] == "accepted_fbs_documents_pending"
        with sqlite3.connect(operational) as conn:
            for suffix in ("documents", "document_lines", "document_expense_lines"):
                conn.execute("DELETE FROM sheet_vitrina_v1_ff_pool_"+suffix+" WHERE document_id='pending'")
            conn.execute("DELETE FROM sheet_vitrina_v1_ff_pool_movement_lines WHERE operation_id='pending'")
        with sqlite3.connect(operational) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_ff_pool_documents SET source_revision='changed' WHERE document_id='receipt-A'")
        assert not capture_basis(runtime, day=source_day, fetched_at=source_now.isoformat(), previous_version="v1")["available"]


def main():
    test_saved_fbs_sources()
    row = payload()["canonical_items"][1]
    proof = resolve(200, row, basis("prior"), business_date=DAY, snapshot_id="s", fetched_at=NOW, previous_version="prior")
    assert proof["available"] and Decimal(proof["wac_rub"]) == 175  # includes zero available last-unit facility
    assert valid_anchor(proof["anchor"]) and proof["anchor"]["proves_physical_movement"] is False
    bad = deepcopy(proof["anchor"]); bad["wac_rub"] = "0"
    assert not valid_anchor(bad)
    for mutate in [lambda b: b.update(wb_version_id="other"), lambda b: b["rows"][0].update(document_proofs=[]),
        lambda b: b["rows"][0].update(wac_rub=None), lambda b: b.update(prepared_at=DAY+"T11:00:00Z")]:
        b = basis("prior"); mutate(b)
        assert not resolve(200, row, b, business_date=DAY, snapshot_id="s", fetched_at=NOW, previous_version="prior")["available"]
    assert not resolve(200, {**row, "quantity": "1"}, basis("prior"), business_date=DAY, snapshot_id="s", fetched_at=NOW, previous_version="prior")["available"]
    with tempfile.TemporaryDirectory(prefix="wb-initial-valuation-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        block = WarehouseFunctionalBlock(runtime=runtime, timestamp_factory=lambda: DAY+"T09:00:00Z")
        seed(block)
        block.timestamp_factory = lambda: NOW
        unavailable = {"available": False, "reason": "accepted_fbs_predecessor_unavailable"}
        with patch("packages.application.wb_initial_fbs_cost_sources.capture", return_value=unavailable):
            plan = block._build_plan(kind="hourly_wb_sync", wb_payload=payload())
            assert [m["nm_id"] for m in plan["wb_valuation"]["missing"]] == [200, 201]
            assert plan["summaries"][STAGE_WB]["quantity"] == "4"
            assert plan["summaries"][STAGE_WB]["capital_rub"] is None
            assert _total_summary(plan["summaries"])["capital_rub"] is None
            result = block.apply_plan(plan, confirm_fingerprint=plan["plan_fingerprint"])
        missing = next(r for r in result["balances"] if r["nm_id"] == 200)
        assert missing["quantity"] == "2" and missing["capital_rub"] is None and missing["known_capital_rub"] == "0"
        assert result["wb_valuation"]["status"] == "partial"
        assert result["historical_wb_cost_projection"]["contour_quantities_by_date"][DAY]["SKU:200"] == "2"
        previous = block._active_version_id()
        priced = basis(previous)
        block.timestamp_factory = lambda: DAY+"T10:01:00Z"
        with patch("packages.application.wb_initial_fbs_cost_sources.capture", return_value=priced):
            recovery = block._build_plan(kind="hourly_wb_sync", wb_payload=payload(fetched=DAY+"T10:01:00Z"))
            line = next(r for r in recovery["lines"] if r["nm_id"] == 200)
            assert line["wac_rub"] == "175" and line["capital_rub"] == "350"
            assert recovery["new_events"] == [] and recovery["movement_documents"] == []
            # A reviewed proof must still match at the final publication boundary.
            with patch("packages.application.wb_initial_fbs_cost_sources.capture", return_value=unavailable):
                try: block.apply_plan(recovery, confirm_fingerprint=recovery["plan_fingerprint"])
                except WarehouseFunctionalError as error: assert "proof drifted" in str(error)
                else: raise AssertionError("changed FBS proof admitted")
            result = block.apply_plan(recovery, confirm_fingerprint=recovery["plan_fingerprint"])
        # Process restart cannot require the live FBS book again or forget the anchor.
        restarted = WarehouseFunctionalBlock(runtime=runtime, timestamp_factory=lambda: DAY+"T10:02:00Z")
        with patch("packages.application.wb_initial_fbs_cost_sources.capture", return_value=unavailable):
            returned = restarted._build_plan(kind="hourly_wb_sync", wb_payload=payload("1", "1", DAY+"T10:02:00Z"))
        line = next(r for r in returned["lines"] if r["nm_id"] == 200)
        assert line["wac_rub"] == "175" and line["wb_quantity"] == "1" and line["cost_covered_quantity"] == "2"
        returned_result = restarted.apply_plan(returned, confirm_fingerprint=returned["plan_fingerprint"])
        next_day = "2026-10-09"
        next_block = WarehouseFunctionalBlock(runtime=runtime, timestamp_factory=lambda: next_day+"T10:00:00Z")
        with patch("packages.application.wb_initial_fbs_cost_sources.capture", return_value=unavailable):
            next_plan = next_block._build_plan(kind="hourly_wb_sync", wb_payload=payload("1", "0", next_day+"T10:00:00Z"))
            receipt = {"event_id": "accepted-next-day", "event_type": "wb_final_acceptance", "source_id": "fbo-receipt:201",
                "source_fingerprint": "sha256:fbo-receipt", "business_date": next_day, "nm_id": 201,
                "quantity": "1", "capital_rub": "100", "provenance": {"current_pool_capital_delta_rub": "100"}}
            next_plan["new_events"] = [receipt]
            row201 = next(r for r in next_plan["wb_snapshot"]["items"] if r["nm_id"] == 201)
            row201.update(in_way_to_client="2", wb_contour_quantity="2")
            next_plan["lines"] = [{**r, "quantity": "2", "wb_in_way_to_client": "2"} if r["nm_id"] == 201 else r for r in next_plan["lines"]]
            projected = next_block._build_post_cutover_daily_cost_projection(captured_at=next_plan["captured_at"],
                candidate_lines=[_line_from_payload(r) for r in next_plan["lines"]], candidate_snapshot=next_plan["wb_snapshot"],
                new_events=[receipt], opening_cost_map=[], cutover_mode=False, allow_partial=True)
            assert not any(r["nm_id"] == 201 for r in projected)
            next_plan["historical_wb_cost_projection"] = [r for r in next_plan["historical_wb_cost_projection"] if r["as_of_date"] < DAY] + projected
            next_plan["summaries"] = _summaries([_line_from_payload(r) for r in next_plan["lines"]])
            next_plan.pop("plan_fingerprint")
            next_plan["plan_fingerprint"] = _fingerprint(next_plan)
            next_result = next_block.apply_plan(next_plan, confirm_fingerprint=next_plan["plan_fingerprint"])
            assert next(r for r in next_result["balances"] if r["nm_id"] == 201)["capital_rub"] is None
            later = WarehouseFunctionalBlock(runtime=runtime, timestamp_factory=lambda: next_day+"T11:00:00Z")
            later_payload = payload("1", "0", next_day+"T11:00:00Z")
            later_payload["canonical_items"][2].update(in_way_to_client="2", wb_contour_quantity="2")
            after_receipt = later._build_plan(kind="hourly_wb_sync", wb_payload=later_payload)
            assert next(r for r in after_receipt["lines"] if r["nm_id"] == 201)["wac_rub"] is None
        assert next(r for r in next_result["balances"] if r["nm_id"] == 200)["wac_rub"] == "175"
        assert any(r["as_of_date"] == next_day and r["nm_id"] == 200 for r in next_plan["historical_wb_cost_projection"])
        zero_block = WarehouseFunctionalBlock(runtime=runtime, timestamp_factory=lambda: next_day+"T12:00:00Z")
        with patch("packages.application.wb_initial_fbs_cost_sources.capture", return_value=unavailable):
            zero_plan = zero_block._build_plan(kind="hourly_wb_sync", wb_payload=payload("0", "0", next_day+"T12:00:00Z"))
            zero_result = zero_block.apply_plan(zero_plan, confirm_fingerprint=zero_plan["plan_fingerprint"])
        assert not any(r["nm_id"] == 200 for r in zero_result["balances"])
        return_day = "2026-10-10"
        return_block = WarehouseFunctionalBlock(runtime=runtime, timestamp_factory=lambda: return_day+"T10:00:00Z")
        with patch("packages.application.wb_initial_fbs_cost_sources.capture", return_value=unavailable):
            physical_plan = return_block._build_plan(kind="hourly_wb_sync", wb_payload=payload("1", "0", return_day+"T10:00:00Z"))
            physical_result = return_block.apply_plan(physical_plan, confirm_fingerprint=physical_plan["plan_fingerprint"])
        physical = next(r for r in physical_result["balances"] if r["nm_id"] == 200)
        assert physical["capital_rub"] == "175" and physical["cost_covered_quantity"] == "1"
        assert physical["provenance"]["initial_valuation_anchor"]["wac_rub"] == "175"
        after_return = WarehouseFunctionalBlock(runtime=runtime, timestamp_factory=lambda: return_day+"T11:00:00Z")
        with patch("packages.application.wb_initial_fbs_cost_sources.capture", return_value=unavailable):
            persisted_return = after_return._build_plan(kind="hourly_wb_sync", wb_payload=payload("1", "0", return_day+"T11:00:00Z"))
        assert next(r for r in persisted_return["lines"] if r["nm_id"] == 200)["wac_rub"] == "175"
        # An unknown positive predecessor was never rolled into a valid zero WAC.
        assert next(r for r in returned["lines"] if r["nm_id"] == 201)["wac_rub"] is None
        correction_day = "2026-10-11"
        correction_block = WarehouseFunctionalBlock(runtime=runtime, timestamp_factory=lambda: correction_day+"T10:00:00Z")
        with patch("packages.application.wb_initial_fbs_cost_sources.capture", return_value=unavailable):
            correction_plan = correction_block._build_plan(kind="hourly_wb_sync", wb_payload=payload("1", "0", correction_day+"T10:00:00Z"))
            correction = {**receipt, "event_id": "negative-correction", "source_fingerprint": "sha256:negative-correction",
                "business_date": correction_day, "quantity": "-1", "capital_rub": "-100",
                "provenance": {"current_pool_capital_delta_rub": "-100"}}
            corrected_projection = correction_block._build_post_cutover_daily_cost_projection(
                captured_at=correction_plan["captured_at"], candidate_lines=[_line_from_payload(r) for r in correction_plan["lines"]],
                candidate_snapshot=correction_plan["wb_snapshot"], new_events=[correction], opening_cost_map=[], cutover_mode=False, allow_partial=True)
            assert not any(r["nm_id"] == 201 for r in corrected_projection)
            correction_plan["historical_wb_cost_projection"] = [r for r in correction_plan["historical_wb_cost_projection"] if r["as_of_date"] < DAY] + corrected_projection
            correction_plan["new_events"] = [correction]
            correction_plan.pop("plan_fingerprint")
            correction_plan["plan_fingerprint"] = _fingerprint(correction_plan)
            corrected_result = correction_block.apply_plan(correction_plan, confirm_fingerprint=correction_plan["plan_fingerprint"])
            assert next(r for r in corrected_result["balances"] if r["nm_id"] == 201)["capital_rub"] is None
        with sqlite3.connect(runtime.db_path) as conn:
            version = result["active_version"]["version_id"]
            raw = conn.execute("SELECT provenance_json FROM sheet_vitrina_v1_warehouse_functional_balances WHERE version_id=? AND nm_id=200", (version,)).fetchone()[0]
            changed = json.loads(raw); changed["source_records"][0]["initial_valuation_anchor"]["wac_rub"] = "176"
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_balances SET provenance_json=? WHERE version_id=? AND nm_id=200", (json.dumps(changed), version))
        with patch("packages.application.wb_initial_fbs_cost_sources.capture", return_value=unavailable):
            try: correction_block._build_plan(kind="hourly_wb_sync", wb_payload=payload(fetched=correction_day+"T10:03:00Z"))
            except WarehouseFunctionalError as error: assert "proof" in str(error)
            else: raise AssertionError("tampered persisted anchor admitted")
    print("warehouse WB initial valuation smoke: ok")


if __name__ == "__main__":
    main()
