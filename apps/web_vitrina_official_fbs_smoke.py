#!/usr/bin/env python3
"""Complete-generation admission, conservative estimates and frozen day proof."""
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.application.web_vitrina_official_fbs import (
    SOURCE, _estimate_cost, build_current_official_fbs_estimate,
    materialize_current_official_fbs_estimate, restore_materialized_official_fbs_estimates,
)
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope, SheetVitrinaWriteTarget, SheetVitrinaV1TemporalSlot
from packages.contracts.web_vitrina_contract import WebVitrinaContractRow


def stage(q, k, *, wac=None, locations=()):
    return dict(quantity=str(q), capital_rub=str(k), cost_covered_quantity=str(q),
                wac_rub=str(wac) if wac is not None else None,
                quality="moving_weighted_average", certified=0,
                locations_json=json.dumps([{"locations": list(locations)}]) if locations else "[]")


def fixture(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
      CREATE TABLE sheet_vitrina_v1_wb_fbs_warehouse_registry_runs(
        run_id,run_sequence,status,complete,policy_version,catalog_scope_json,
        warehouse_scope_json,generation_digest,started_at,completed_at);
      CREATE TABLE sheet_vitrina_v1_wb_fbs_stock_snapshot_runs(
        run_id,registry_run_id,seller_warehouse_id,complete,requested_chrt_count,
        dense_row_count,explicit_chrt_count,omitted_zero_count,source_digest,snapshot_at);
      CREATE TABLE sheet_vitrina_v1_wb_fbs_stock_snapshot_rows(run_id,chrt_id,nm_id,amount,provenance);
      CREATE TABLE sheet_vitrina_v1_ff_facilities(facility_id,active);
      CREATE TABLE sheet_vitrina_v1_fbs_warehouse_mappings(mapping_id,facility_id,active);
      CREATE TABLE sheet_vitrina_v1_warehouse_business_projection_current_rows(as_of_date,nm_id,provenance_json,metrics_json);
      CREATE TABLE sheet_vitrina_v1_warehouse_functional_versions(version_id,status,business_effective_date,source_watermarks_json);
      CREATE TABLE sheet_vitrina_v1_ff_pool_balances(facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,wac_rub,source_watermark,updated_at);
      CREATE TABLE sheet_vitrina_v1_warehouse_functional_balances(version_id,nm_id,warehouse_key,
        quantity,capital_rub,wac_rub,cost_covered_quantity,quality,certified,wb_quantity,provenance_json);
    """)
    # Resolve canonical table constants so the fixture follows the actual mapping table.
    from packages.application.wb_fbs_warehouse_registry import WAREHOUSE_MAPPINGS_TABLE, FACILITIES_TABLE
    if WAREHOUSE_MAPPINGS_TABLE != "sheet_vitrina_v1_fbs_warehouse_mappings":
        conn.execute(f"ALTER TABLE sheet_vitrina_v1_fbs_warehouse_mappings RENAME TO {WAREHOUSE_MAPPINGS_TABLE}")
    if FACILITIES_TABLE != "sheet_vitrina_v1_ff_facilities":
        conn.execute(f"ALTER TABLE sheet_vitrina_v1_ff_facilities RENAME TO {FACILITIES_TABLE}")
    warehouses = [dict(facility_id=f, mapping_id=f, seller_warehouse_id=i) for i, f in enumerate(["A", "B"], 1)]
    conn.execute("INSERT INTO sheet_vitrina_v1_wb_fbs_warehouse_registry_runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                 ("g1", 1, "success", 1, "complete_catalog_stable_http200_omission_zero_v1",
                  json.dumps(dict(complete=True, requested_chrt_count=2, active_nm_id_count=2)),
                  json.dumps(dict(complete=True, warehouse_count=2, warehouses=warehouses)),
                  "sha256:g1", "2026-09-05T10:00:00Z", "2026-09-05T10:01:00Z"))
    for i, f in enumerate(["A", "B"], 1):
        conn.execute(f"INSERT INTO {FACILITIES_TABLE} VALUES(?,1)", (f,))
        conn.execute(f"INSERT INTO {WAREHOUSE_MAPPINGS_TABLE} VALUES(?,?,1)", (f, f))
        conn.execute("INSERT INTO sheet_vitrina_v1_wb_fbs_stock_snapshot_runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (f, "g1", i, 1, 2, 2, 1, 1, "sha256:" + f, "2026-09-05T10:00:00Z"))
        conn.executemany("INSERT INTO sheet_vitrina_v1_wb_fbs_stock_snapshot_rows VALUES(?,?,?,?,?)",
                         [(f, 101, 1, 3 if f == "A" else 5, "explicit_wb_row"),
                          (f, 102, 2, 0, "omitted_requested_zero")])
    from packages.application.warehouse_functional import _watermark
    pool_rows = [dict(facility_id=f, pool="FBS", nm_id=2, projection_epoch=1, quantity=0, capital_rub="0",
                     wac_rub=None, source_watermark="basis", updated_at="2026-09-05T09:00:00Z") for f in ["A", "B"]]
    for r in pool_rows:
        conn.execute("INSERT INTO sheet_vitrina_v1_ff_pool_balances VALUES(?,?,?,?,?,?,?,?,?)", tuple(r.values()))
    conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_functional_versions VALUES('v1','good','2026-09-05',?)",
                 (json.dumps(dict(ff_pool_detail=_watermark(pool_rows, "updated_at"))),))
    for nm in [1, 2]:
        conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_business_projection_current_rows VALUES(?,?,?,?)",
                     ("2026-09-05", nm, '{"functional_version_id":"v1"}',
                      json.dumps(dict(own_capital_FF_qty=0 if nm==2 else 34, own_capital_FF_capital_rub=0 if nm==2 else 920))))
    locations = [dict(facility_id=f, pool=p, quantity=q, capital_rub=str(k))
                 for f, p, q, k in [("A", "FBS", 10, 200), ("B", "FBS", 20, 600), ("B", "FBO", 4, 120)]]
    for nm, key, q, k, physical, loc in [(1, "wb", 10, 100, 6, []), (1, "ff", 34, 920, 0, locations),
                                        (2, "wb", 2, 80, 2, []), (2, "ff", 0, 0, 0, [])]:
        conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_functional_balances VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     ("v1", nm, key, str(q), str(k), str(k/q) if q else None, str(q), "moving_weighted_average", 0,
                      str(physical), json.dumps({"source_records": [{"locations": loc}]} if loc else {})))
    conn.commit()
    return conn


def main():
    now = datetime(2026, 9, 5, 10, 10, tzinfo=timezone.utc)
    with TemporaryDirectory(prefix="official-fbs-estimate-") as tmp:
        path = Path(tmp) / "fixture.sqlite3"
        conn = fixture(path)
        before = path.read_bytes()
        model = build_current_official_fbs_estimate(path, nm_ids=[1, 2], now=now)
        assert model["available"], model
        assert model["skus"][1]["capital"] == 430
        assert model["skus"][2]["cost"] == 40  # WB-only, proven empty FF with no locations
        assert model["total"]["cost"] == Decimal("21.25")  # weighted, not mean of SKU prices
        assert model["total"]["fbs_quantity"] == 8 and model["total"]["stock_quantity"] == 16
        assert path.read_bytes() == before  # no source, ledger, physical or audit writes
        conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_functional_balances WHERE nm_id=2 AND warehouse_key='ff'")
        conn.commit()
        assert build_current_official_fbs_estimate(path, nm_ids=[1, 2], now=now)["skus"][2]["cost"] == 40
        conn.execute("UPDATE sheet_vitrina_v1_ff_pool_balances SET capital_rub='1' WHERE facility_id='A'")
        conn.commit()
        assert build_current_official_fbs_estimate(path, nm_ids=[1, 2], now=now)["skus"][2]["cost"] is None
        conn.execute("UPDATE sheet_vitrina_v1_ff_pool_balances SET capital_rub='0' WHERE facility_id='A'")
        conn.commit()
        conn.execute("UPDATE sheet_vitrina_v1_wb_fbs_stock_snapshot_rows SET amount=1,provenance='explicit_wb_row' WHERE run_id='A' AND nm_id=2")
        conn.commit()
        assert build_current_official_fbs_estimate(path, nm_ids=[1, 2], now=now)["skus"][2]["cost"] is None
        conn.execute("UPDATE sheet_vitrina_v1_wb_fbs_stock_snapshot_rows SET amount=0,provenance='omitted_requested_zero' WHERE run_id='A' AND nm_id=2")
        conn.commit()
        conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_balances SET quantity='0',capital_rub='0',cost_covered_quantity='0',wb_quantity='0',wac_rub=NULL WHERE nm_id=2 AND warehouse_key='wb'")
        conn.commit()
        zero = build_current_official_fbs_estimate(path, nm_ids=[1, 2], now=now)
        assert zero["skus"][2]["cost"] is None and zero["total"]["cost"] == zero["skus"][1]["cost"]
        conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_balances SET quantity='2',capital_rub='80',cost_covered_quantity='2',wb_quantity='2',wac_rub='40' WHERE nm_id=2 AND warehouse_key='wb'")
        conn.commit()
        assert not build_current_official_fbs_estimate(path, nm_ids=[1, 2], now=now + timedelta(hours=1))["available"]
        assert not build_current_official_fbs_estimate(path, nm_ids=[1, 2], now=now + timedelta(days=1))["available"]
        assert not build_current_official_fbs_estimate(path, nm_ids=[1, 2, 3], now=now)["available"]
        conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_balances SET capital_rub='0' WHERE nm_id=1 AND warehouse_key='wb'")
        conn.commit()
        missing = build_current_official_fbs_estimate(path, nm_ids=[1, 2], now=now)
        assert missing["skus"][1]["cost"] is None and missing["total"]["cost"] is None
        assert missing["skus"][2]["cost"] == 40
        conn.execute("UPDATE sheet_vitrina_v1_wb_fbs_stock_snapshot_rows SET nm_id=9 WHERE run_id='A' AND nm_id=2")
        conn.commit()
        assert not build_current_official_fbs_estimate(path, nm_ids=[1, 2], now=now)["available"]
        conn.close()
        empty = _estimate_cost(stage(0, 0, wac=17), stage(0, 0), {"A": Decimal(0)})
        assert empty["cost"] == 17  # same-SKU retained WAC, not a made-up zero
        plan = SheetVitrinaV1Envelope("v1", "snapshot", "2026-09-04", ["2026-09-04", "2026-09-05"],
                                    [SheetVitrinaV1TemporalSlot("previous", "previous", "2026-09-04"),
                                     SheetVitrinaV1TemporalSlot("current", "current", "2026-09-05")], {}, [
            SheetVitrinaWriteTarget("DATA_VITRINA", "A1", "A1:D3", "A:D", "replace", False,
                                   ["label", "key", "2026-09-04", "2026-09-05"], [
                ["cost", "SKU:1|our_wb_unit_cost_rub", 99, ""],
                ["capital", "SKU:1|own_capital_FF_capital_rub", 999, 920]], 2, 4)])
        saved = materialize_current_official_fbs_estimate(plan, estimate=model)
        assert saved.sheets[0].rows[0][2] == 99
        assert saved.sheets[0].rows[1] == plan.sheets[0].rows[1]
        # Serialize/reload as the existing snapshot writer does, then close the date.
        frozen = json.loads(json.dumps(saved.metadata))
        row = WebVitrinaContractRow("SKU:1|our_wb_unit_cost_rub", 0, "SKU", "SKU:1", "", "our_wb_unit_cost_rub",
                                   "", "", "", None, 1, None, {"2026-09-04": 99, "2026-09-05": ""})
        restored = restore_materialized_official_fbs_estimates([row], presentation=frozen["server_cell_presentation"])[0]
        assert restored.values_by_date["2026-09-04"] == 99
        assert restored.values_by_date["2026-09-05"] == float(model["skus"][1]["cost"])
        assert restored.presentation_by_date["2026-09-05"]["source"] == SOURCE
        resaved = materialize_current_official_fbs_estimate(plan, estimate={"available": False},
                    previous_presentation=frozen["server_cell_presentation"])
        assert resaved.metadata["server_cell_presentation"] == frozen["server_cell_presentation"]
        # Exercise the actual writer and outer-key rollover, not just a same-key helper.
        from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
        from apps.sheet_vitrina_v1_web_vitrina_contract_smoke import BUNDLE_FIXTURE
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        runtime.ingest_bundle(json.loads(BUNDLE_FIXTURE.read_text()), activated_at="2026-09-05T10:00:00Z")
        state = runtime.load_current_state()
        plan = replace(plan, sheets=[*plan.sheets, SheetVitrinaWriteTarget(
            "STATUS", "A1", "A1:A1", "A:A", "replace", False, ["status"], [], 0, 1)])
        shifted = replace(plan, sheets=[replace(plan.sheets[0], write_start_cell="B5", write_rect="B5:E7"), plan.sheets[1]])
        shifted_saved = materialize_current_official_fbs_estimate(shifted, estimate=model)
        assert shifted_saved.sheets[1] is shifted.sheets[1]
        assert shifted_saved.sheets[0].write_rect == "B5:E" + str(5 + len(shifted_saved.sheets[0].rows))
        with patch("packages.application.web_vitrina_official_fbs.build_current_official_fbs_estimate", return_value=model):
            runtime.save_sheet_vitrina_ready_snapshot(current_state=state, refreshed_at="2026-09-05T10:10:00Z", plan=plan)
        tomorrow = replace(plan, as_of_date="2026-09-05", snapshot_id="tomorrow",
                           date_columns=["2026-09-05", "2026-09-06"],
                           temporal_slots=[SheetVitrinaV1TemporalSlot("previous", "previous", "2026-09-05"),
                                           SheetVitrinaV1TemporalSlot("current", "current", "2026-09-06")], sheets=[
                               replace(plan.sheets[0], header=["label", "key", "2026-09-05", "2026-09-06"]),
                               plan.sheets[1]])
        with patch("packages.application.web_vitrina_official_fbs.build_current_official_fbs_estimate", return_value={"available": False}):
            runtime.save_sheet_vitrina_ready_snapshot(current_state=state, refreshed_at="2026-09-06T10:10:00Z", plan=tomorrow)
        loaded = runtime.load_sheet_vitrina_ready_snapshot(as_of_date="2026-09-05")
        cost_row = next(r for r in loaded.sheets[0].rows if r[1] == row.row_id)
        assert cost_row[2] == restored.values_by_date["2026-09-05"]
        assert loaded.metadata["server_cell_presentation"][row.row_id]["2026-09-05"]["source"] == SOURCE
        assert any(r[1] == "TOTAL|total_inventory_fbs_total_qty_v1" and r[2] == 8 for r in loaded.sheets[0].rows)
    print("official_fbs: generation, freshness, dense zeros, cost coverage, weighted TOTAL, no writes, frozen date: ok")


if __name__ == "__main__":
    main()
