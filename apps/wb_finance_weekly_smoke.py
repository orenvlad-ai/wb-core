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
    classify_deduction,
    historical_week_bounds,
)


def main() -> None:
    _assert_client_contract()
    _assert_schedule_contract()
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
            "before_cogs_profit": "96.0000",
            "cogs": "200.0000",
            "profit_after_cogs": "-104.0000",
        }
        for key, value in expected.items():
            if metrics.get(key) != value:
                raise AssertionError(
                    f"{key}: expected {value!r}, got {metrics.get(key)!r}"
                )
        if Decimal(metrics["final_margin_pct"]).quantize(Decimal("0.01")) != Decimal(
            "-43.33"
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

        # Same keys update in-place; no duplicate or doubled amounts.
        rows[0]["retailPriceWithDisc"] = "390"
        second = block.ingest_week(date(2026, 6, 22), date(2026, 6, 28), rows)
        with sqlite3.connect(block.db_path) as conn:
            raw_count = conn.execute(
                "select count(*) from wb_finance_weekly_raw_rows"
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


def _seed_canonical_cost(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE registry_upload_current_state(slot INTEGER PRIMARY KEY,bundle_version TEXT,activated_at TEXT);
            CREATE TABLE registry_upload_config_v2(bundle_version TEXT,nm_id INTEGER,enabled INTEGER,display_name TEXT,group_name TEXT,display_order INTEGER);
            CREATE TABLE cost_price_current_state(slot INTEGER PRIMARY KEY,dataset_version TEXT,activated_at TEXT);
            CREATE TABLE cost_price_upload_rows(dataset_version TEXT,row_order INTEGER,group_name TEXT,cost_price_rub TEXT,effective_from TEXT);
            CREATE TABLE sheet_vitrina_v1_nomenclature_items(is_active INTEGER,nm_id INTEGER,vendor_code TEXT,barcode TEXT,barcodes_json TEXT);
            INSERT INTO registry_upload_current_state VALUES(1,'bundle','2026-01-01');
            INSERT INTO registry_upload_config_v2 VALUES('bundle',101,1,'SKU','Group',1);
            INSERT INTO cost_price_current_state VALUES(1,'cost','2026-01-01');
            INSERT INTO cost_price_upload_rows VALUES('cost',1,'Group','100','2026-01-01');
            INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES(1,101,'VC101','4600000000101','["4600000000101"]');
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
