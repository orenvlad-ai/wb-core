#!/usr/bin/env python3
"""Temporal COST_PRICE -> Our WB Cost integration smoke for weekly Finance."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.our_wb_costs import (  # noqa: E402
    OurWbCostRebuildResult,
)
from packages.application.registry_upload_http_entrypoint import (  # noqa: E402
    RegistryUploadHttpEntrypoint,
)
from packages.application.wb_finance_weekly import (  # noqa: E402
    WbFinanceWeeklyBlock,
)


def main() -> None:
    with TemporaryDirectory(prefix="wb-finance-cost-cutover-") as tmp:
        block = WbFinanceWeeklyBlock(
            Path(tmp),
            seller_id="seller-1",
            now_factory=lambda: datetime(2026, 7, 27, tzinfo=timezone.utc),
        )
        _seed_legacy_finance_coverage_schema(block.db_path)
        block.ensure_schema()
        _assert_schema_migration(block.db_path)
        _seed_cost_sources(block.db_path)
        base_rows = _base_rows()
        pre = block.ingest_week(date(2026, 6, 22), date(2026, 6, 28), base_rows)
        if pre["aggregate"]["cogs"] != "200.0000":
            raise AssertionError(f"pre-cutover COST_PRICE baseline mismatch: {pre}")
        _assert_temporal_cost_cutover(block, base_rows)
        _assert_control_week_regression_fixture()
        _assert_our_wb_rebuild_invalidation_wiring()
    print(
        "wb_finance_weekly_cost_cutover: ok -> 30.06 COST_PRICE, 01.07 Our WB, mixed week, quality, invalidation, idempotency"
    )


def _assert_schema_migration(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        coverage_columns = {
            row[1]
            for row in conn.execute(
                "pragma table_info(wb_finance_weekly_cost_coverage)"
            )
        }
    if not {"quality_json", "cost_state_hash"}.issubset(coverage_columns):
        raise AssertionError(
            f"Finance coverage schema migration missing: {coverage_columns}"
        )


def _assert_temporal_cost_cutover(
    block: WbFinanceWeeklyBlock, base_rows: list[dict]
) -> None:
    sale, returned = base_rows
    mixed_rows = [
        dict(
            sale,
            reportId=629,
            rrdId=6291,
            quantity=2,
            rrDate="2026-06-30",
            saleDt="2026-07-01",
            retailPriceWithDisc="240",
            forPay="180",
        ),
        dict(
            sale,
            reportId=701,
            rrdId=7011,
            quantity=3,
            rrDate="2026-07-01",
            saleDt="2026-06-30",
            retailPriceWithDisc="450",
            forPay="330",
        ),
        dict(
            returned,
            reportId=702,
            rrdId=7021,
            quantity=1,
            rrDate="2026-07-02",
            saleDt="2026-06-30",
            retailPriceWithDisc="150",
            forPay="110",
        ),
    ]
    mixed = block.ingest_week(date(2026, 6, 29), date(2026, 7, 5), mixed_rows)
    mixed_week = _week(block, "2026-06-29")
    quality = mixed_week["cost_coverage"]["quality"]
    if (
        mixed["aggregate"]["cogs"] != "490.0000"
        or mixed["aggregate"]["profit_after_cogs"] != "-90.0000"
        or mixed["aggregate"]["final_margin_pct"] != "-16.6667"
    ):
        raise AssertionError(f"mixed-week COGS/profit mismatch: {mixed}")
    if quality["source_units"] != {
        "cost_price": 2,
        "our_wb_cost_daily_state": 4,
    }:
        raise AssertionError(f"30.06/01.07 temporal source split mismatch: {quality}")
    if quality["confirmed_share_pct"] != "87.5000":
        raise AssertionError(f"mixed-week confirmation quality mismatch: {quality}")

    mixed_missing_date = block.ingest_week(
        date(2026, 6, 29),
        date(2026, 7, 5),
        [
            dict(
                sale,
                reportId=703,
                rrdId=7031,
                quantity=1,
                rrDate="",
                saleDt="",
                orderDt="",
                retailPriceWithDisc="200",
                forPay="150",
            )
        ],
    )
    mixed_missing_problem = _week(block, "2026-06-29")["cost_coverage"]["problem_skus"][
        0
    ]
    if (
        mixed_missing_date["aggregate"]["cogs"] is not None
        or mixed_missing_date["aggregate"]["profit_after_cogs"] is not None
        or mixed_missing_problem["source"] != "operation_date_missing"
        or mixed_missing_problem["reason"] != "operation_date_missing"
        or mixed_missing_problem["operation_date"] != ""
    ):
        raise AssertionError(
            "mixed-week row without exact operation date must stay uncovered: "
            f"{mixed_missing_date}"
        )
    block.ingest_week(date(2026, 6, 29), date(2026, 7, 5), mixed_rows)

    missing = block.ingest_week(
        date(2026, 7, 6),
        date(2026, 7, 12),
        [
            dict(
                sale,
                reportId=704,
                rrdId=7041,
                quantity=1,
                rrDate="2026-07-04",
                retailPriceWithDisc="200",
                forPay="150",
            )
        ],
    )
    missing_week = _week(block, "2026-07-06")
    missing_problem = missing_week["cost_coverage"]["problem_skus"][0]
    if (
        missing["aggregate"]["cogs"] is not None
        or missing["aggregate"]["profit_after_cogs"] is not None
        or missing_problem["source"] != "our_wb_cost_daily_state"
        or missing_problem["operation_date"] != "2026-07-04"
        or missing_problem["reason"] != "our_wb_daily_state_missing"
    ):
        raise AssertionError(
            f"post-cutover missing state fell back unexpectedly: {missing_week}"
        )

    quality_rows = [
        dict(
            sale,
            reportId=712,
            rrdId=7121,
            quantity=2,
            rrDate="2026-07-02",
            retailPriceWithDisc="400",
            forPay="300",
        ),
        dict(
            sale,
            reportId=713,
            rrdId=7131,
            quantity=2,
            rrDate="2026-07-03",
            retailPriceWithDisc="400",
            forPay="300",
        ),
    ]
    estimated = block.ingest_week(date(2026, 7, 13), date(2026, 7, 19), quality_rows)
    estimated_quality = _week(block, "2026-07-13")["cost_coverage"]["quality"]
    if (
        estimated["aggregate"]["cogs"] != "660.0000"
        or estimated_quality["confirmed_units"] != "1.0000"
        or estimated_quality["estimated_units"] != "1.0000"
        or estimated_quality["fallback_units"] != "2.0000"
        or estimated_quality["confirmed_share_pct"] != "25.0000"
    ):
        raise AssertionError(
            "estimated/fallback cost must participate without becoming confirmed: "
            f"{estimated_quality}"
        )

    fallback_date = block.ingest_week(
        date(2026, 7, 20),
        date(2026, 7, 26),
        [
            dict(
                sale,
                reportId=720,
                rrdId=7201,
                quantity=1,
                rrDate="",
                saleDt="",
                orderDt="",
                retailPriceWithDisc="250",
                forPay="180",
            )
        ],
    )
    fallback_quality = _week(block, "2026-07-20")["cost_coverage"]["quality"]
    fallback_problem = _week(block, "2026-07-20")["cost_coverage"]["problem_skus"][0]
    if (
        fallback_date["aggregate"]["cogs"] is not None
        or fallback_date["aggregate"]["profit_after_cogs"] is not None
        or fallback_quality["operation_date_fallback_rows"] != 1
        or fallback_quality["operation_date_fallback_units"] != 1
        or fallback_problem["source"] != "operation_date_missing"
        or fallback_problem["reason"] != "operation_date_missing"
    ):
        raise AssertionError(
            "post-cutover operation-date fallback must remain uncovered: "
            f"quality={fallback_quality} problem={fallback_problem}"
        )

    with sqlite3.connect(block.db_path) as conn:
        conn.execute(
            """UPDATE sheet_vitrina_v1_wb_cost_daily_state
            SET our_wb_unit_cost_rub='200',inputs_hash='july-1-cost-200'
            WHERE as_of_date='2026-07-01' AND nm_id=101"""
        )
        conn.commit()
    invalidated = block.recalculate_stale_cost_weeks()
    mixed_after_change = _week(block, "2026-06-29")
    if (
        invalidated["recalculated_week_count"] != 1
        or mixed_after_change["metrics"]["cogs"] != "640.0000"
        or mixed_after_change["metrics"]["profit_after_cogs"] != "-240.0000"
        or mixed_after_change["metrics"]["final_margin_pct"] != "-44.4444"
    ):
        raise AssertionError(
            f"daily-state change did not invalidate affected week: {invalidated}"
        )
    if block.recalculate_stale_cost_weeks()["recalculated_week_count"] != 0:
        raise AssertionError("unchanged cost dependency hash must be idempotent")
    repeated = block.recalculate_week(date(2026, 6, 29), date(2026, 7, 5))
    if repeated != mixed_after_change["metrics"]:
        raise AssertionError("repeated mixed-week recalculation must be idempotent")
    if _week(block, "2026-06-22")["metrics"]["cogs"] != "200.0000":
        raise AssertionError(
            "pre-boundary COST_PRICE result changed after Our WB update"
        )

    alias_week = block.ingest_week(
        date(2026, 7, 27),
        date(2026, 8, 2),
        [
            dict(
                sale,
                reportId=727,
                rrdId=7271,
                nmId=999,
                vendorCode="VC101",
                sku="alias-only",
                quantity=1,
                rrDate="2026-07-27",
                retailPriceWithDisc="300",
                forPay="220",
            )
        ],
    )
    if alias_week["aggregate"]["cogs"] != "210.0000":
        raise AssertionError(
            f"canonical alias did not resolve Our WB cost: {alias_week}"
        )
    with sqlite3.connect(block.db_path) as conn:
        conn.execute(
            "UPDATE sheet_vitrina_v1_nomenclature_items SET vendor_code='OTHER101' WHERE nm_id=101"
        )
        conn.commit()
    remapped = block.recalculate_stale_cost_weeks()
    remapped_week = _week(block, "2026-07-27")
    if (
        remapped["recalculated_week_count"] != 1
        or remapped_week["metrics"]["cogs"] is not None
        or remapped_week["cost_coverage"]["problem_skus"][0]["sku"] != "999"
    ):
        raise AssertionError(
            f"nomenclature alias change must invalidate Finance cost: {remapped_week}"
        )


def _assert_control_week_regression_fixture() -> None:
    fixture = json.loads(
        (
            ROOT / "artifacts/wb_finance_weekly/fixtures/control_week_2026_06_22.json"
        ).read_text(encoding="utf-8")
    )
    if fixture["week_end"] >= "2026-07-01":
        raise AssertionError(f"control fixture crossed Our WB cutover: {fixture}")
    expected = {
        "raw_row_count": 72184,
        "report_ids": ["764583098", "764583099"],
        "net_sales_qty": 18606,
        "net_revenue": "9707534.0300",
        "commission": "3567303.7900",
        "marketing": "990903.0000",
        "logistics": "1285.0000",
        "transit_logistics": "55403.0500",
        "penalties": "360.0000",
        "cogs": "1861728.0000",
        "profit_after_cogs": "3230551.1900",
        "final_margin_pct": "33.2788",
        "cost_source": "cost_price",
    }
    if {key: fixture.get(key) for key in expected} != expected:
        raise AssertionError(f"pre-cutover acceptance regression changed: {fixture}")


def _assert_our_wb_rebuild_invalidation_wiring() -> None:
    class _CostBlock:
        def rebuild_all(self) -> OurWbCostRebuildResult:
            return OurWbCostRebuildResult(0, 0, 0, 1)

    class _FinanceBlock:
        calls = 0

        def recalculate_stale_cost_weeks(self) -> dict:
            self.calls += 1
            return {
                "status": "completed",
                "checked_week_count": 1,
                "recalculated_week_count": 1,
                "weeks": [{"week_start": "2026-06-29"}],
            }

    entrypoint = object.__new__(RegistryUploadHttpEntrypoint)
    entrypoint.our_wb_cost_block = _CostBlock()
    entrypoint.wb_finance_weekly_block = _FinanceBlock()
    result = entrypoint.handle_our_wb_cost_recalculate_request({"fixture": True})
    if (
        entrypoint.wb_finance_weekly_block.calls != 1
        or result["wb_finance_cost_recalculation"]["recalculated_week_count"] != 1
    ):
        raise AssertionError(
            f"Our WB rebuild must trigger Finance invalidation once: {result}"
        )


def _week(block: WbFinanceWeeklyBlock, week_start: str) -> dict:
    return next(
        week
        for week in block.build_payload()["weeks"]
        if week["week_start"] == week_start
    )


def _base_rows() -> list[dict]:
    base = {
        "reportType": 1,
        "nmId": 101,
        "vendorCode": "VC101",
        "sku": "4600000000101",
        "sellerOperName": "Продажа",
    }
    return [
        {
            **base,
            "reportId": 1,
            "rrdId": 1,
            "rrDate": "2026-06-23",
            "docTypeName": "Продажа",
            "quantity": 3,
            "retailPriceWithDisc": "360",
            "forPay": "240",
            "acquiringFee": "12",
        },
        {
            **base,
            "reportId": 2,
            "reportType": 2,
            "rrdId": 2,
            "rrDate": "2026-06-24",
            "docTypeName": "Возврат",
            "sellerOperName": "Возврат",
            "quantity": 1,
            "retailPriceWithDisc": "120",
            "forPay": "90",
            "acquiringFee": "3",
        },
    ]


def _seed_legacy_finance_coverage_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE wb_finance_weekly_cost_coverage (
            seller_id TEXT NOT NULL,week_start TEXT NOT NULL,week_end TEXT NOT NULL,
            matched_units INTEGER NOT NULL,unmatched_units INTEGER NOT NULL,
            coverage_pct TEXT,cogs_rub TEXT,problem_skus_json TEXT NOT NULL,
            calculated_at TEXT NOT NULL,PRIMARY KEY(seller_id,week_start,week_end))"""
        )
        conn.commit()


def _seed_cost_sources(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE registry_upload_current_state(slot INTEGER PRIMARY KEY,bundle_version TEXT,activated_at TEXT);
            CREATE TABLE registry_upload_config_v2(bundle_version TEXT,nm_id INTEGER,enabled INTEGER,display_name TEXT,group_name TEXT,display_order INTEGER);
            CREATE TABLE cost_price_current_state(slot INTEGER PRIMARY KEY,dataset_version TEXT,activated_at TEXT);
            CREATE TABLE cost_price_upload_rows(dataset_version TEXT,row_order INTEGER,group_name TEXT,cost_price_rub TEXT,effective_from TEXT);
            CREATE TABLE sheet_vitrina_v1_nomenclature_items(is_active INTEGER,nm_id INTEGER,vendor_code TEXT,barcode TEXT,barcodes_json TEXT,product_type TEXT);
            CREATE TABLE sheet_vitrina_v1_wb_cost_daily_state(
                as_of_date TEXT NOT NULL,nm_id INTEGER NOT NULL,stock_qty REAL NOT NULL,
                our_wb_unit_cost_rub REAL,confirmed_qty REAL NOT NULL,estimated_qty REAL NOT NULL,
                fallback_qty REAL NOT NULL,confirmed_share_pct REAL,source_status TEXT NOT NULL,
                component_status_json TEXT NOT NULL,calculated_at TEXT NOT NULL,inputs_hash TEXT NOT NULL,
                PRIMARY KEY(as_of_date,nm_id)
            );
            INSERT INTO registry_upload_current_state VALUES(1,'bundle','2026-01-01');
            INSERT INTO registry_upload_config_v2 VALUES('bundle',101,1,'SKU','Group',1);
            INSERT INTO cost_price_current_state VALUES(1,'cost','2026-01-01');
            INSERT INTO cost_price_upload_rows VALUES('cost',1,'Group','100','2026-01-01');
            INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES(1,101,'VC101','4600000000101','["4600000000101"]','other');
            INSERT INTO sheet_vitrina_v1_wb_cost_daily_state VALUES
                ('2026-07-01',101,100,150,100,0,0,1,'confirmed','{}','2026-07-01T23:00:00Z','july-1-cost-150'),
                ('2026-07-02',101,100,160,50,50,0,0.5,'estimated','{}','2026-07-02T23:00:00Z','july-2-cost-160'),
                ('2026-07-03',101,100,170,0,0,100,0,'fallback','{}','2026-07-03T23:00:00Z','july-3-cost-170'),
                ('2026-07-05',101,100,180,100,0,0,1,'confirmed','{}','2026-07-05T23:00:00Z','july-5-cost-180'),
                ('2026-07-20',101,100,190,100,0,0,1,'confirmed','{}','2026-07-20T23:00:00Z','july-20-cost-190'),
                ('2026-07-27',101,100,210,100,0,0,1,'confirmed','{}','2026-07-27T23:00:00Z','july-27-cost-210');
            """
        )
        conn.commit()


if __name__ == "__main__":
    main()
