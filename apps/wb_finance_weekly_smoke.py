#!/usr/bin/env python3
"""Targeted integration smoke for the weekly Wildberries Finance contour."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.wb_finance_weekly import (  # noqa: E402
    FinanceHttpResult,
    WbFinanceApiClient,
    WbFinanceWeeklyBlock,
    _functional_wb_cost_state,
    classify_deduction,
    historical_week_bounds,
)


def main() -> None:
    _assert_client_contract()
    _assert_schedule_contract()
    _assert_functional_daily_cost_requires_exact_date()
    with TemporaryDirectory(prefix="wb-finance-weekly-") as tmp:
        block = WbFinanceWeeklyBlock(
            Path(tmp),
            seller_id="seller-1",
            now_factory=lambda: datetime(2026, 7, 7, 3, 0, tzinfo=timezone.utc),
        )
        block.ensure_schema()
        _seed_canonical_cost(block.db_path)
        waiting = block.sync_week(date(2026, 6, 15), date(2026, 6, 21), _EmptyClient())
        if waiting["status"] != "waiting":
            raise AssertionError(f"HTTP 204/no rows must keep week waiting: {waiting}")
        rows = _fixture_rows()
        first = block.ingest_week(date(2026, 6, 22), date(2026, 6, 28), rows)
        metrics = first["aggregate"]
        expected = {
            "sales_qty": 3,
            "returns_qty": 1,
            "net_sales_qty": 2,
            "revenue_before_returns": "360.0000",
            "returns_amount": "120.0000",
            "net_revenue": "240.0000",
            "commission": "90.0000",
            "acquiring": "9.0000",
            "logistics": "10.0000",
            "storage": "2.0000",
            "acceptance": "3.0000",
            "marketing": "20.0000",
            "transit_logistics": "5.0000",
            "penalties": "4.0000",
            "subscriptions": "6.0000",
            "paid_services": "7.0000",
            "other_deductions": "8.0000",
            "positive_adjustments": "11.0000",
            "total_wb_expenses": "155.0000",
            "profit_period_expenses": "147.0000",
            "wb_expenses_without_marketing_pct": "56.2500",
            "before_cogs_profit": "104.0000",
            "cogs": "200.0000",
            "profit_after_cogs": "-96.0000",
        }
        for key, value in expected.items():
            if metrics.get(key) != value:
                raise AssertionError(
                    f"{key}: expected {value!r}, got {metrics.get(key)!r}"
                )
        if Decimal(metrics["final_margin_pct"]).quantize(Decimal("0.01")) != Decimal(
            "-40.00"
        ):
            raise AssertionError(
                f"final margin mismatch: {metrics['final_margin_pct']}"
            )
        payload = block.build_payload()
        control = next(
            week for week in payload["weeks"] if week["week_start"] == "2026-06-22"
        )
        if control["report_count"] != 2:
            raise AssertionError(f"main/buyout report merge mismatch: {payload}")
        if control["cost_coverage"]["coverage_pct"] != "100.0000":
            raise AssertionError(f"cost coverage mismatch: {payload}")

        nomenclature_fallback = block.ingest_week(
            date(2026, 2, 2),
            date(2026, 2, 8),
            [
                {
                    **rows[0],
                    "reportId": 102,
                    "rrdId": 1020,
                    "nmId": 102,
                    "vendorCode": "ANTI102",
                    "sku": "4600000000102",
                    "quantity": 1,
                    "retailPriceWithDisc": "150",
                    "forPay": "100",
                    "rrDate": "2026-02-03",
                    "saleDt": "2026-01-02T00:00:00Z",
                }
            ],
        )
        if nomenclature_fallback["aggregate"]["cogs"] != "115.0000":
            raise AssertionError(
                "canonical nomenclature product_type/rrDate cost mapping failed"
            )

        # Same keys update in-place; no duplicate or doubled amounts.
        rows[0]["retailPriceWithDisc"] = "390"
        second = block.ingest_week(date(2026, 6, 22), date(2026, 6, 28), rows)
        with sqlite3.connect(block.db_path) as conn:
            raw_count = conn.execute(
                "select count(*) from wb_finance_weekly_raw_rows where week_start='2026-06-22'"
            ).fetchone()[0]
        if (
            raw_count != len(rows)
            or second["aggregate"]["revenue_before_returns"] != "390.0000"
        ):
            raise AssertionError("idempotent upsert/change update failed")

        missing = dict(
            rows[1], rrdId=999, nmId=999999, vendorCode="missing", sku="missing"
        )
        incomplete = block.ingest_week(date(2026, 6, 29), date(2026, 7, 5), [missing])
        if (
            incomplete["aggregate"]["cogs"] is not None
            or incomplete["aggregate"]["profit_after_cogs"] is not None
        ):
            raise AssertionError(
                "missing cost must not be coerced to zero or precise profit"
            )
        with sqlite3.connect(block.db_path) as conn:
            conn.execute(
                "insert into registry_upload_config_v2 values('bundle',999999,1,'Recovered SKU','Group',2)"
            )
            conn.execute(
                """INSERT INTO wb_finance_retro_cost_map VALUES(
                   'seller-1','999999','100.0000','2026-07-01','fixture',
                   '{"nm_id":"999999","unit_cost_rub":"100.0000"}',
                   'sha256:fixture-row-999999','sha256:fixture-calculation-999999',
                   'exact_2026_07_01','wb_finance_business_approved_retro_cost_v1',
                   'business_approved_retro','2026-07-01T00:00:00Z')"""
            )
            conn.commit()
        recovered = block.recalculate_week(date(2026, 6, 29), date(2026, 7, 5))
        recovered_week = next(
            week
            for week in block.build_payload()["weeks"]
            if week["week_start"] == "2026-06-29"
        )
        if recovered["cogs"] is None or recovered_week["status"] != "completed":
            raise AssertionError(
                "recovered cost coverage must restore completed status"
            )
        distinct_missing = block.ingest_week(
            date(2026, 4, 6),
            date(2026, 4, 12),
            [
                dict(
                    rows[0],
                    reportId=401,
                    rrdId=4010,
                    nmId=401,
                    vendorCode="missing-sale",
                    sku="missing-sale",
                    quantity=1,
                ),
                dict(
                    rows[1],
                    reportId=402,
                    rrdId=4020,
                    nmId=402,
                    vendorCode="missing-return",
                    sku="missing-return",
                    quantity=1,
                ),
            ],
        )
        distinct_coverage = next(
            week
            for week in block.build_payload()["weeks"]
            if week["week_start"] == "2026-04-06"
        )["cost_coverage"]
        if (
            distinct_missing["aggregate"]["cogs"] is not None
            or distinct_coverage["unmatched_units"] != 2
            or len(distinct_coverage["problem_skus"]) != 2
        ):
            raise AssertionError(
                f"different missing SKU movements must not cancel: {distinct_coverage}"
            )
        symmetric_missing = block.ingest_week(
            date(2026, 3, 30),
            date(2026, 4, 5),
            [
                dict(
                    rows[0],
                    reportId=403,
                    rrdId=4030,
                    rrDate="2026-04-01",
                    nmId=403,
                    vendorCode="missing-symmetric",
                    sku="missing-symmetric",
                    quantity=1,
                ),
                dict(
                    rows[1],
                    reportId=404,
                    rrdId=4040,
                    rrDate="2026-04-01",
                    nmId=403,
                    vendorCode="missing-symmetric",
                    sku="missing-symmetric",
                    quantity=1,
                ),
            ],
        )
        symmetric_coverage = next(
            week
            for week in block.build_payload()["weeks"]
            if week["week_start"] == "2026-03-30"
        )["cost_coverage"]
        if (
            symmetric_missing["aggregate"]["cogs"] is not None
            or symmetric_coverage["unmatched_units"] != 2
            or symmetric_coverage["problem_skus"][0]["net_units"] != 0
            or symmetric_coverage["problem_skus"][0]["unmatched_units"] != 2
        ):
            raise AssertionError(
                "same-SKU sale/return symmetry must not hide missing gross cost coverage: "
                f"{symmetric_coverage}"
            )
        second_sync = block.ingest_week(date(2026, 6, 22), date(2026, 6, 28), rows)
        if second_sync["status"] != "completed":
            raise AssertionError(
                f"stable repeated sync must complete the week: {second_sync}"
            )
        resumable = _ResumableClient()
        first_backfill = block.run_backfill(resumable, today=date(2026, 1, 19))
        second_backfill = block.run_backfill(resumable, today=date(2026, 1, 19))
        if (
            first_backfill["status"] != "completed_with_errors"
            or second_backfill["status"] != "completed"
        ):
            raise AssertionError(
                f"resumable backfill mismatch: {first_backfill['status']}/{second_backfill['status']}"
            )
        recalculated = block.recalculate_all_weeks()
        with sqlite3.connect(block.db_path) as conn:
            raw_week_count = conn.execute(
                """select count(*) from (
                select distinct week_start,week_end
                from wb_finance_weekly_raw_rows where seller_id='seller-1')"""
            ).fetchone()[0]
            conn.executescript(
                """
                INSERT INTO wb_finance_weekly_aggregates
                SELECT 'orphan',week_start,week_end,classifier_version,metrics_json,
                       report_ids_json,report_types_json,unknown_reasons_json,calculated_at
                FROM wb_finance_weekly_aggregates WHERE seller_id='seller-1' LIMIT 1;
                INSERT INTO wb_finance_weekly_cost_coverage
                SELECT 'orphan',week_start,week_end,matched_units,unmatched_units,
                       coverage_pct,cogs_rub,problem_skus_json,quality_json,
                       cost_state_hash,calculated_at
                FROM wb_finance_weekly_cost_coverage WHERE seller_id='seller-1' LIMIT 1;
                INSERT INTO wb_finance_weekly_reconciliation
                SELECT 'orphan',week_start,week_end,status,difference_rub,detail_json,checked_at
                FROM wb_finance_weekly_reconciliation WHERE seller_id='seller-1' LIMIT 1;
                """
            )
            conn.commit()
        if recalculated["week_count"] != raw_week_count:
            raise AssertionError(f"all-week recalculation mismatch: {recalculated}")
        repaired = block.repair_orphan_derived_rows()
        if repaired["deleted_total"] != 3:
            raise AssertionError(f"orphan derived repair mismatch: {repaired}")

        print(
            "wb_finance_weekly: ok -> pagination, 204, 429, merge, idempotency, classifications, COGS, margins, coverage"
        )


def _assert_client_contract() -> None:
    calls: list[dict] = []
    sleeps: list[float] = []
    responses = [
        FinanceHttpResult(429, [], {"X-Ratelimit-Retry": "2"}),
        FinanceHttpResult(200, [{"reportId": 1, "rrdId": 10}], {}),
        FinanceHttpResult(200, [{"reportId": 2, "rrdId": 20}], {}),
        FinanceHttpResult(204, [], {}),
    ]

    def request(payload: dict) -> FinanceHttpResult:
        calls.append(dict(payload))
        return responses.pop(0)

    client = WbFinanceApiClient(
        "super-secret",
        limit=1,
        min_interval_seconds=0,
        request=request,
        sleep=sleeps.append,
    )
    rows = client.fetch_week(date(2026, 6, 22), date(2026, 6, 28))
    if (
        [call["rrdId"] for call in calls] != [0, 0, 10, 20]
        or len(rows) != 2
        or sleeps != [2.0]
    ):
        raise AssertionError(
            f"pagination/retry contract mismatch: {calls}, {sleeps}, {rows}"
        )
    if "super-secret" in repr(client) or "<redacted>" not in repr(client):
        raise AssertionError("client repr leaked authorization token")


class _EmptyClient:
    def fetch_week(self, date_from: date, date_to: date) -> list[dict]:
        return []


class _ResumableClient:
    def __init__(self) -> None:
        self.failed_once = False

    def fetch_week(self, date_from: date, date_to: date) -> list[dict]:
        if not self.failed_once and date_from == date(2025, 12, 29):
            self.failed_once = True
            raise RuntimeError("temporary fixture failure")
        report_id = int(date_from.strftime("%Y%m%d"))
        return [
            {
                "dateFrom": date_from.isoformat(),
                "dateTo": date_to.isoformat(),
                "reportId": report_id,
                "reportType": 1,
                "rrdId": report_id * 10,
                "nmId": 101,
                "vendorCode": "VC101",
                "sku": "4600000000101",
                "saleDt": max(date_from, date(2026, 1, 1)).isoformat(),
                "docTypeName": "Продажа",
                "sellerOperName": "Продажа",
                "quantity": 1,
                "retailPriceWithDisc": "120",
                "forPay": "90",
                "acquiringFee": "3",
            }
        ]


def _assert_schedule_contract() -> None:
    weeks = historical_week_bounds(date(2026, 7, 12))
    if weeks[0] != (date(2025, 12, 29), date(2026, 1, 4)) or weeks[-1] != (
        date(2026, 6, 29),
        date(2026, 7, 5),
    ):
        raise AssertionError(f"historical bounds mismatch: {weeks[0]}..{weeks[-1]}")
    block = WbFinanceWeeklyBlock(Path("/tmp/not-used"))
    if (
        block.due_tick_week(datetime(2026, 7, 6, 1, 59, tzinfo=timezone.utc))
        is not None
    ):
        raise AssertionError("Monday before 05:00 Europe/Moscow must not sync")
    if (
        classify_deduction({"bonusTypeName": "Оказание услуг WB Продвижение"})
        != "marketing"
    ):
        raise AssertionError("marketing classifier mismatch")
    if (
        classify_deduction({"bonusTypeName": "Услуги доставки транзитных поставок"})
        != "transit_logistics"
    ):
        raise AssertionError("transit classifier mismatch")


def _assert_functional_daily_cost_requires_exact_date() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sheet_vitrina_v1_warehouse_functional_cutovers(
            cutover_id TEXT,status TEXT,cutover_at TEXT
        );
        CREATE TABLE sheet_vitrina_v1_warehouse_functional_versions(
            version_id TEXT,cutover_id TEXT,status TEXT,effective_at TEXT,
            created_at TEXT,plan_fingerprint TEXT
        );
        CREATE TABLE sheet_vitrina_v1_warehouse_functional_balances(
            version_id TEXT,warehouse_key TEXT,nm_id TEXT,quantity TEXT,
            cost_covered_quantity TEXT,certified INTEGER,quality TEXT,
            wac_rub TEXT,provenance_json TEXT
        );
        CREATE TABLE sheet_vitrina_v1_warehouse_wb_daily_cost(
            cutover_id TEXT,as_of_date TEXT,nm_id TEXT,quantity TEXT,
            quality TEXT,wac_rub TEXT,provenance_json TEXT,fingerprint TEXT
        );
        INSERT INTO sheet_vitrina_v1_warehouse_functional_cutovers
        VALUES('warehouse_functional_cutover_v1','posted','2026-07-19T22:00:00Z');
        INSERT INTO sheet_vitrina_v1_warehouse_functional_versions
        VALUES('later-version','warehouse_functional_cutover_v1','good',
               '2026-07-20T12:00:00Z','2026-07-20T12:00:00Z','sha256:later');
        INSERT INTO sheet_vitrina_v1_warehouse_functional_balances
        VALUES('later-version','wb','101','10','10',1,'certified','200','{}');
        """
    )
    missing, applies = _functional_wb_cost_state(
        conn,
        as_of_date="2026-07-20",
        nm_id="101",
    )
    if not applies or missing is not None:
        raise AssertionError(
            "weekly WB cost must stay unknown without an exact-day daily projection; "
            f"got applies={applies}, state={missing}"
        )
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_warehouse_wb_daily_cost
           VALUES('warehouse_functional_cutover_v1','2026-07-20','101','10',
                  'periodic_snapshot_wac_closed','150','{}','sha256:exact')"""
    )
    exact, applies = _functional_wb_cost_state(
        conn,
        as_of_date="2026-07-20",
        nm_id="101",
    )
    conn.close()
    if (
        not applies
        or exact is None
        or Decimal(str(exact.get("our_wb_unit_cost_rub"))) != Decimal("150")
    ):
        raise AssertionError(f"weekly WB cost must consume its exact-day row: {exact}")


def _seed_canonical_cost(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE registry_upload_current_state(slot INTEGER PRIMARY KEY,bundle_version TEXT,activated_at TEXT);
            CREATE TABLE registry_upload_config_v2(bundle_version TEXT,nm_id INTEGER,enabled INTEGER,display_name TEXT,group_name TEXT,display_order INTEGER);
            CREATE TABLE cost_price_current_state(slot INTEGER PRIMARY KEY,dataset_version TEXT,activated_at TEXT);
            CREATE TABLE cost_price_upload_rows(dataset_version TEXT,row_order INTEGER,group_name TEXT,cost_price_rub TEXT,effective_from TEXT);
            CREATE TABLE sheet_vitrina_v1_nomenclature_items(is_active INTEGER,nm_id INTEGER,vendor_code TEXT,barcode TEXT,barcodes_json TEXT,product_type TEXT);
            INSERT INTO registry_upload_current_state VALUES(1,'bundle','2026-01-01');
            INSERT INTO registry_upload_config_v2 VALUES('bundle',101,1,'SKU','Group',1);
            INSERT INTO cost_price_current_state VALUES(1,'cost','2026-01-01');
            INSERT INTO cost_price_upload_rows VALUES('cost',1,'Group','100','2026-01-01');
            INSERT INTO cost_price_upload_rows VALUES('cost',2,'Anti-Spy','115','2026-01-28');
            INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES(1,101,'VC101','4600000000101','["4600000000101"]','other');
            INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES(1,102,'ANTI102','4600000000102','["4600000000102"]','anti_spy');
            INSERT INTO wb_finance_retro_cost_map VALUES(
                'seller-1','101','100.0000','2026-07-01','fixture',
                '{"nm_id":"101","unit_cost_rub":"100.0000"}',
                'sha256:fixture-row-101','sha256:fixture-calculation-101',
                'exact_2026_07_01','wb_finance_business_approved_retro_cost_v1',
                'business_approved_retro','2026-07-01T00:00:00Z'
            );
            """
        )
        conn.commit()


def _fixture_rows() -> list[dict]:
    base = {
        "dateFrom": "2026-06-22",
        "dateTo": "2026-06-28",
        "reportType": 1,
        "nmId": 101,
        "vendorCode": "VC101",
        "sku": "4600000000101",
        "saleDt": "2026-06-23",
    }
    rows = [
        {
            **base,
            "reportId": 1,
            "rrdId": 1,
            "docTypeName": "Продажа",
            "sellerOperName": "Продажа",
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
            "docTypeName": "Возврат",
            "sellerOperName": "Возврат",
            "quantity": 1,
            "retailPriceWithDisc": "120",
            "forPay": "90",
            "acquiringFee": "3",
        },
        {
            **base,
            "reportId": 1,
            "rrdId": 3,
            "docTypeName": "",
            "sellerOperName": "Логистика",
            "quantity": 0,
            "deliveryService": "10",
            "paidStorage": "2",
            "paidAcceptance": "3",
            "penalty": "4",
        },
        {
            **base,
            "reportId": 1,
            "rrdId": 4,
            "docTypeName": "",
            "sellerOperName": "Удержание",
            "quantity": 0,
            "deduction": "20",
            "bonusTypeName": "WB Продвижение",
        },
        {
            **base,
            "reportId": 1,
            "rrdId": 5,
            "docTypeName": "",
            "sellerOperName": "Удержание",
            "quantity": 0,
            "deduction": "5",
            "bonusTypeName": "Услуги доставки транзитных поставок",
        },
        {
            **base,
            "reportId": 1,
            "rrdId": 6,
            "docTypeName": "",
            "sellerOperName": "Удержание",
            "quantity": 0,
            "deduction": "6",
            "bonusTypeName": "Подписка Jamm",
        },
        {
            **base,
            "reportId": 1,
            "rrdId": 7,
            "docTypeName": "",
            "sellerOperName": "Удержание",
            "quantity": 0,
            "deduction": "7",
            "bonusTypeName": "Платный сервис",
        },
        {
            **base,
            "reportId": 1,
            "rrdId": 8,
            "docTypeName": "",
            "sellerOperName": "Удержание",
            "quantity": 0,
            "deduction": "8",
            "bonusTypeName": "Неизвестное основание",
            "additionalPayment": "11",
        },
    ]
    return rows


if __name__ == "__main__":
    main()
