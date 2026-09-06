#!/usr/bin/env python3
"""Current cards and Balance share admitted FBS evidence without historical writes."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.web_vitrina_official_fbs_smoke import fixture
from packages.application.inventory_planning_read_model import InventoryPlanningReadModel
from packages.application.ff_pool_foundation import FACILITY_PROFILES_TABLE
from packages.application.sku_management import SkuManagementBlock, validate_forecast_settings
from packages.application.wb_fbs_warehouse_registry import FACILITIES_TABLE

NOW = datetime(2026, 9, 5, 10, 10, tzinfo=timezone.utc)


def seed(path):
    conn = fixture(path)
    for column in ("code", "name"):
        conn.execute(f"ALTER TABLE {FACILITIES_TABLE} ADD COLUMN {column} TEXT")
    conn.execute(f"UPDATE {FACILITIES_TABLE} SET name=facility_id,code=facility_id")
    conn.execute(f"CREATE TABLE {FACILITY_PROFILES_TABLE}(facility_id,city)")
    conn.executescript("""
        CREATE TABLE sheet_vitrina_v1_warehouse_functional_active(slot,version_id);
        INSERT INTO sheet_vitrina_v1_warehouse_functional_active VALUES(1,'v1');
        CREATE TABLE sheet_vitrina_v1_warehouse_wb_snapshots(
            version_id,snapshot_id,snapshot_date,raw_rows_digest,fetched_at,items_json,created_at);
        CREATE TABLE immutable_calculation(calculation_id,payload_json);
        INSERT INTO immutable_calculation VALUES('old','{"stock_ff":999,"generation_id":"old"}');
    """)
    conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_wb_snapshots VALUES(?,?,?,?,?,?,?)",
                 ("v1", "wb1", "2026-09-05", "sha256:wb", NOW.isoformat(),
                  json.dumps([{"nm_id": 1, "quantity": 10}]), NOW.isoformat()))
    # A positive product outside both WB rows and Balance's two-SKU config must
    # still count in warehouse totals. Dense omission is explicit zero evidence.
    conn.executemany("INSERT INTO sheet_vitrina_v1_wb_fbs_stock_snapshot_rows VALUES(?,?,?,?,?)",
                     [("A", 103, 3, 29000, "explicit_wb_row"), ("B", 103, 3, 0, "omitted_requested_zero")])
    conn.execute("UPDATE sheet_vitrina_v1_wb_fbs_warehouse_registry_runs SET catalog_scope_json=?",
                 (json.dumps(dict(complete=True, requested_chrt_count=3, active_nm_id_count=3)),))
    conn.execute("UPDATE sheet_vitrina_v1_wb_fbs_stock_snapshot_runs SET requested_chrt_count=3,dense_row_count=3,explicit_chrt_count=CASE WHEN run_id='A' THEN 2 ELSE 1 END,omitted_zero_count=CASE WHEN run_id='A' THEN 1 ELSE 2 END")
    conn.commit()
    return conn


def main():
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "source.sqlite3"
        conn = seed(path)
        reader = InventoryPlanningReadModel(db_path=path)
        balance = object.__new__(SkuManagementBlock)
        balance.runtime = SimpleNamespace(db_path=path)
        balance.stocks_block = None
        balance.now_factory = lambda: NOW

        def inputs():
            return balance._collect_forecast_evidence(
                active=[{"nm_id": 1}, {"nm_id": 2}], settings=validate_forecast_settings({}),
                inventory_balance_only=True)

        def cards(at=NOW):
            return reader.current_official_snapshot(now=at)

        def metric(payload, key):
            return next(m["value"] for m in payload["metrics"] if m["metric_key"] == key)

        before = path.read_bytes()
        # A present legacy ledger contains contradictory values; it and lifecycle
        # are forbidden as current-card sources.
        with patch("packages.application.inventory_planning_read_model._fbs_facilities", side_effect=AssertionError("legacy read")):
            current = cards()
            evidence = inputs()
        assert metric(current, "wb_total") == 10
        assert metric(current, "fbs_total") == 29008
        assert metric(current, "total") == 29018
        assert current["fbs"]["sku_count"] == 3
        assert current["fbs"]["scope"] == "complete_official_catalog"
        assert current["freshness"]["fbs_updated_at"] == "2026-09-05T10:00:00Z"
        assert current["fbs"]["legacy_ledger_used"] is False
        assert current["fbs"]["lifecycle_used"] is False
        by_nm = {r["nm_id"]: r for r in current["skus"]}
        for nm, row in evidence.items():
            assert by_nm[nm]["fbs_total"] == row["stock_ff"]
            for source, expected in zip(by_nm[nm]["fbs_facilities"], row["fbs_stock_evidence"]["facilities"]):
                assert source["available"] == expected["quantity"]
                assert source["stock_source"] == expected["source"]
                assert source["stock_source"]["generation_id"] == "g1"
                assert source["stock_source"]["stock_run_id"] in {"A", "B"}
        assert evidence[2]["stock_ff"] == 0
        assert path.read_bytes() == before

        # Partial newest collection cannot replace an admissible complete run.
        conn.execute("INSERT INTO sheet_vitrina_v1_wb_fbs_warehouse_registry_runs SELECT 'partial',2,'partial',0,policy_version,catalog_scope_json,warehouse_scope_json,generation_digest,started_at,completed_at FROM sheet_vitrina_v1_wb_fbs_warehouse_registry_runs WHERE run_id='g1'")
        conn.commit()
        assert metric(cards(), "fbs_total") == 29008
        stale = cards(NOW + timedelta(minutes=30))
        assert metric(stale, "fbs_total") is None and metric(stale, "total") is None
        assert "устарел" in stale["fbs"]["source_blocker"]
        assert stale["fbs"]["sku_count"] is None
        assert metric(stale, "wb_total") == 10
        assert metric(cards(NOW + timedelta(days=1)), "fbs_total") is None

        # New complete evidence changes current inputs only, never the old result.
        conn.execute("INSERT INTO sheet_vitrina_v1_wb_fbs_warehouse_registry_runs SELECT 'g2',3,status,complete,policy_version,catalog_scope_json,warehouse_scope_json,'sha256:g2',started_at,completed_at FROM sheet_vitrina_v1_wb_fbs_warehouse_registry_runs WHERE run_id='g1'")
        conn.execute("INSERT INTO sheet_vitrina_v1_wb_fbs_stock_snapshot_runs SELECT run_id||'2','g2',seller_warehouse_id,complete,requested_chrt_count,dense_row_count,explicit_chrt_count,omitted_zero_count,source_digest||'2','2026-09-05T10:05:00Z' FROM sheet_vitrina_v1_wb_fbs_stock_snapshot_runs WHERE registry_run_id='g1'")
        conn.execute("INSERT INTO sheet_vitrina_v1_wb_fbs_stock_snapshot_rows SELECT run_id||'2',chrt_id,nm_id,CASE WHEN run_id='A' AND nm_id=1 THEN 7 ELSE amount END,provenance FROM sheet_vitrina_v1_wb_fbs_stock_snapshot_rows WHERE run_id IN ('A','B')")
        conn.commit()
        before = path.read_bytes()
        assert metric(cards(), "fbs_total") == 29012
        assert inputs()[1]["stock_ff"] == 12
        assert inputs()[1]["fbs_stock_evidence"]["facilities"][0]["source"]["generation_id"] == "g2"
        assert conn.execute("SELECT payload_json FROM immutable_calculation").fetchone()[0] == '{"stock_ff":999,"generation_id":"old"}'
        assert path.read_bytes() == before

        # Missing dense row, even outside Balance's requested SKUs, invalidates
        # the complete proof instead of becoming an inferred zero or old fallback.
        conn.execute("DELETE FROM sheet_vitrina_v1_wb_fbs_stock_snapshot_rows WHERE run_id='A2' AND nm_id=3")
        conn.commit()
        assert metric(cards(), "fbs_total") is None
        assert inputs()[1]["stock_ff"] is None
        conn.close()
    print("warehouse_official_fbs_cards_smoke: ok")


if __name__ == "__main__":
    main()
