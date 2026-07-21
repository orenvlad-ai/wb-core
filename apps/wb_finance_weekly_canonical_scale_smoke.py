#!/usr/bin/env python3
"""Production-scale bounded-memory regression for canonical Finance dry-run."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import resource
import sqlite3
import sys
from tempfile import TemporaryDirectory
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.wb_finance_weekly import (  # noqa: E402
    WbFinanceWeeklyBlock,
    _StreamingCostDependencyDigest,
    _StreamingJsonArrayDigest,
)


RAW_ROW_COUNT = 295_919
WEEK_COUNT = 26
COST_LAYER_ROW_COUNT = 50_000
MAX_SECONDS = 60.0
MAX_RSS_MIB = 512.0


def main() -> None:
    _assert_streaming_digest_equivalence()
    with TemporaryDirectory(prefix="wb-finance-canonical-scale-") as tmp:
        runtime = Path(tmp)
        block = WbFinanceWeeklyBlock(
            runtime,
            seller_id="seller-scale",
            now_factory=lambda: datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
        )
        block.ensure_schema()
        _seed_required_sources(block.db_path)
        _seed_raw_history(block.db_path)

        started = time.perf_counter()
        plan = block.plan_canonical_finance_backfill()
        elapsed = time.perf_counter() - started
        rss_mib = _peak_rss_mib()

        if plan["finance_row_count"] != RAW_ROW_COUNT:
            raise AssertionError(f"raw-row scope mismatch: {plan['finance_row_count']}")
        if plan["week_count"] != WEEK_COUNT:
            raise AssertionError(f"all-history week scope mismatch: {plan['week_count']}")
        if plan["source_manifests"]["finance"]["row_count"] != RAW_ROW_COUNT:
            raise AssertionError("streamed Finance manifest lost rows")
        if (
            plan["non_target_manifest"]["supply_cost_layers"]["row_count"]
            != COST_LAYER_ROW_COUNT
        ):
            raise AssertionError("large capitalization-layer manifest was not covered")
        if not str(plan["source_manifests"]["finance"]["digest"]).startswith("sha256:"):
            raise AssertionError("streamed Finance manifest digest is absent")
        if (
            plan["finance_nm_id_count"] != 2
            or len(plan["week_nm_operation_date_matrix"]) != WEEK_COUNT * 2
            or any(
                item["canonical_source_date"] != "2026-07-01"
                for item in plan["week_nm_operation_date_matrix"]
                if item["nm_id"] == "101"
            )
        ):
            raise AssertionError("production-scale canonical COGS matrix is incomplete")
        missing_matrix = [
            item for item in plan["week_nm_operation_date_matrix"] if item["nm_id"] == "202"
        ]
        resolved_matrix = [
            item for item in plan["week_nm_operation_date_matrix"] if item["nm_id"] == "101"
        ]
        missing_sales_qty = sum(item["sales_qty"] for item in missing_matrix)
        if (
            len(missing_matrix) != WEEK_COUNT
            or any(item["source_quality"] != "missing" for item in missing_matrix)
            or not 140_000 <= missing_sales_qty <= 150_000
            or missing_sales_qty
            + sum(item["sales_qty"] for item in resolved_matrix)
            != RAW_ROW_COUNT
        ):
            raise AssertionError("high-volume missing-cost evidence was not aggregated by week/date")
        missing_blockers = [
            item for item in plan["blockers"] if item["code"] == "canonical_cost_coverage_incomplete"
        ]
        if (
            plan["apply_allowed"]
            or len(missing_blockers) != WEEK_COUNT
            or any(len(item["problem_skus"]) != 1 for item in missing_blockers)
        ):
            raise AssertionError(f"scale missing-cost blockers were duplicated: {plan['blockers']}")
        if elapsed >= MAX_SECONDS:
            raise AssertionError(f"canonical scale dry-run exceeded {MAX_SECONDS}s: {elapsed:.3f}s")
        if rss_mib >= MAX_RSS_MIB:
            raise AssertionError(
                f"canonical scale dry-run exceeded {MAX_RSS_MIB:.0f} MiB RSS: {rss_mib:.1f} MiB"
            )

        print(
            json.dumps(
                {
                    "status": "ok",
                    "raw_rows": RAW_ROW_COUNT,
                    "weeks": WEEK_COUNT,
                    "elapsed_ms": int(elapsed * 1000),
                    "peak_rss_mib": round(rss_mib, 1),
                    "finance_digest": plan["source_manifests"]["finance"]["digest"],
                    "target_after_digest": plan["expected_target_after_digest"],
                },
                sort_keys=True,
            )
        )


def _assert_streaming_digest_equivalence() -> None:
    rows = [
        ["2026-01-05", "1", "2", "sha256:a"],
        {"nm_id": "101", "unit_cost_rub": "100.0000", "quality": "certified"},
        {"unicode": "Себестоимость WB наша", "nullable": None},
    ]
    streamed = _StreamingJsonArrayDigest()
    for row in rows:
        streamed.add(row)
    expected = "sha256:" + hashlib.sha256(
        json.dumps(
            rows,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    if streamed.count != len(rows) or streamed.finish() != expected:
        raise AssertionError("streaming JSON-array digest differs from canonical full serialization")
    dependencies = [
        {
            "report_id": "1",
            "rrd_id": "2",
            "nm_id": "101",
            "operation_date": "2026-01-05",
            "canonical_source_date": "2026-07-01",
            "canonical_source_identity": "canonical:101:2026-07-01",
            "source_digest": "sha256:cost",
            "quality": "certified",
            "selection_method": "project_2026_07_01_backward",
            "status": "resolved",
            "reason": "",
        }
    ]
    cost_stream = _StreamingCostDependencyDigest()
    for dependency in dependencies:
        cost_stream.add(dependency)
    expected_cost = hashlib.sha256(
        json.dumps(
            {
                "formula_version": "wb_finance_canonical_our_wb_cost_v2",
                "dependencies": dependencies,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if cost_stream.finish() != expected_cost:
        raise AssertionError("streamed cost dependency digest changed canonical hash semantics")


def _seed_required_sources(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE sheet_vitrina_v1_nomenclature_items(
                is_active INTEGER,nm_id INTEGER,vendor_code TEXT,barcode TEXT,
                barcodes_json TEXT,product_type TEXT
            );
            INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES
                (1,101,'VC101','BAR101','["BAR101"]','other'),
                (1,202,'VC202','BAR202','["BAR202"]','other');
            CREATE TABLE sheet_vitrina_v1_warehouse_functional_cutovers(
                cutover_id TEXT PRIMARY KEY,cutover_at TEXT,status TEXT,
                plan_fingerprint TEXT,source_watermarks_json TEXT,
                absorbed_supply_revisions_json TEXT,backup_json TEXT,
                created_at TEXT,updated_at TEXT
            );
            INSERT INTO sheet_vitrina_v1_warehouse_functional_cutovers VALUES(
                'warehouse_functional_cutover_v1','2026-07-01T00:00:00Z','posted',
                'sha256:scale-cutover','{}','[]','{}',
                '2026-07-01T00:00:00Z','2026-07-01T00:00:00Z'
            );
            CREATE TABLE sheet_vitrina_v1_warehouse_wb_daily_cost(
                cutover_id TEXT,as_of_date TEXT,nm_id INTEGER,quantity TEXT,wac_rub TEXT,
                capital_rub TEXT,quality TEXT,provenance_json TEXT,fingerprint TEXT,
                created_at TEXT,PRIMARY KEY(cutover_id,as_of_date,nm_id)
            );
            INSERT INTO sheet_vitrina_v1_warehouse_wb_daily_cost VALUES(
                'warehouse_functional_cutover_v1','2026-07-01',101,'10','100','1000',
                'certified','{}','sha256:scale-cost','2026-07-01T00:00:00Z'
            );
            CREATE TABLE sheet_vitrina_v1_wb_supply_cost_layers(
                wb_supply_cost_layer_id TEXT PRIMARY KEY,wb_supply_id TEXT,nm_id TEXT,
                transit_cost_status TEXT,transit_amount_total TEXT,
                wb_acceptance_amount_total TEXT,inputs_hash TEXT,version INTEGER,
                is_current INTEGER
            );
            WITH RECURSIVE seq(n) AS (
                SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n<50000
            )
            INSERT INTO sheet_vitrina_v1_wb_supply_cost_layers
            SELECT 'layer-' || n,'supply-' || n,CAST(100000+n AS TEXT),'confirmed',
                   '0','0','sha256:layer-' || n,1,1
            FROM seq;
            """
        )
        conn.commit()


def _seed_raw_history(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute(
            """WITH RECURSIVE seq(n) AS (
                   SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n<?
               ), source AS (
                   SELECT n,
                          date('2026-01-05', printf('+%d days', ((n-1) % ?) * 7)) week_start,
                          CASE WHEN ((n-1) / ?) % 2 = 0 THEN 202 ELSE 101 END nm_id
                   FROM seq
               )
               INSERT INTO wb_finance_weekly_raw_rows(
                   seller_id,report_id,rrd_id,report_type,week_start,week_end,nm_id,
                   vendor_code,barcode,doc_type_name,seller_oper_name,row_hash,raw_json,
                   first_seen_at,updated_at
               )
               SELECT 'seller-scale','scale-' || n,'scale-' || n,1,week_start,
                      date(week_start,'+6 days'),CAST(nm_id AS TEXT),
                      CASE WHEN nm_id=101 THEN 'VC101' ELSE 'VC202' END,
                      CASE WHEN nm_id=101 THEN 'BAR101' ELSE 'BAR202' END,
                      'Продажа','Продажа',
                      'sha256:scale-' || n,
                      json_object(
                          'reportId','scale-' || n,'rrdId','scale-' || n,'nmId',nm_id,
                          'vendorCode',CASE WHEN nm_id=101 THEN 'VC101' ELSE 'VC202' END,
                          'sku',CASE WHEN nm_id=101 THEN 'BAR101' ELSE 'BAR202' END,
                          'rrDate',week_start,
                          'saleDt',week_start,'docTypeName','Продажа','sellerOperName','Продажа',
                          'quantity',1,'retailPriceWithDisc','200','forPay','140','acquiringFee','10'
                      ),
                      '2026-07-20T00:00:00Z','2026-07-20T00:00:00Z'
               FROM source""",
            (RAW_ROW_COUNT, WEEK_COUNT, WEEK_COUNT),
        )
        conn.commit()


def _peak_rss_mib() -> float:
    raw = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform == "darwin":
        return raw / (1024.0 * 1024.0)
    return raw / 1024.0


if __name__ == "__main__":
    main()
