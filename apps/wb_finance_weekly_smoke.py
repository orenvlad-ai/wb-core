#!/usr/bin/env python3
"""Targeted integration smoke for the weekly Wildberries Finance contour."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
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
from packages.application.finance_raw_storage import (  # noqa: E402
    FinanceOutboxConsumer,
    FinanceRawIngestor,
    InjectedFinanceStorageFault,
    bind_generation_identity,
    ensure_operational_schema,
    ensure_raw_schema,
)
from packages.application.storage_registry import (  # noqa: E402
    StoreRegistry,
    atomic_write_manifest,
    build_manifest,
)


def main() -> None:
    _assert_client_contract()
    _assert_schedule_contract()
    _assert_functional_daily_cost_requires_exact_date()
    _assert_split_storage_contract()
    with TemporaryDirectory(prefix="wb-finance-weekly-") as tmp:
        block = WbFinanceWeeklyBlock(
            Path(tmp),
            seller_id="seller-1",
            now_factory=lambda: datetime(2026, 7, 7, 3, 0, tzinfo=timezone.utc),
        )
        block.ensure_schema()
        _seed_canonical_cost(block.db_path)
        _assert_signed_deduction_contract(block)
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
            "agent_remuneration": "81.0000",
            "commission": "81.0000",
            "combined_commission_control": "90.0000",
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
            "profit_period_expenses": "155.0000",
            "wb_expenses_without_marketing_pct": "56.2500",
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
        _assert_fbs_channel_partial_coverage(block)

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
                "insert into sheet_vitrina_v1_nomenclature_items values(1,999999,'missing','missing','[\"missing\"]','other')"
            )
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_warehouse_wb_daily_cost VALUES(
                   'warehouse_functional_cutover_v1','2026-07-01',999999,'10',
                   '100','1000','periodic_snapshot_wac_closed','{}',
                   'sha256:fixture-row-999999','2026-07-01T00:00:00Z')"""
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
            or len(symmetric_coverage["problem_skus"]) != 1
            or symmetric_coverage["problem_skus"][0]["operation_count"] != 2
            or symmetric_coverage["problem_skus"][0]["sales_qty"] != 1
            or symmetric_coverage["problem_skus"][0]["returns_qty"] != 1
            or sum(item["net_units"] for item in symmetric_coverage["problem_skus"]) != 0
            or sum(item["unmatched_units"] for item in symmetric_coverage["problem_skus"]) != 2
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
                       coverage_json,cost_state_hash,calculated_at
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


def _assert_split_storage_contract() -> None:
    with TemporaryDirectory(prefix="wb-finance-split-") as tmp:
        runtime = Path(tmp)
        bootstrap = WbFinanceWeeklyBlock(
            runtime,
            seller_id="seller-1",
            now_factory=lambda: datetime(
                2026, 7, 7, 3, 0, tzinfo=timezone.utc
            ),
        )
        bootstrap.ensure_schema()
        _seed_canonical_cost(bootstrap.db_path)
        generation_root = runtime / "generations" / "split-smoke"
        generation_root.mkdir(parents=True)
        raw_path = generation_root / "finance_raw.sqlite3"
        operational_path = generation_root / "operational.sqlite3"
        with (
            sqlite3.connect(bootstrap.db_path) as source,
            sqlite3.connect(operational_path) as operational,
        ):
            source.backup(operational)
            operational.execute(
                "DROP INDEX IF EXISTS wb_finance_raw_by_week"
            )
            operational.execute(
                "DROP INDEX IF EXISTS wb_finance_raw_by_sku_week"
            )
            operational.execute(
                "DROP TABLE wb_finance_weekly_raw_rows"
            )
            operational.row_factory = sqlite3.Row
            ensure_operational_schema(operational)
            bind_generation_identity(
                operational,
                logical_store="operational",
                generation_id="op-split-smoke",
                generation_epoch="split-smoke",
                source_fingerprint="sha256:" + ("a" * 64),
            )
            operational.commit()
        with sqlite3.connect(raw_path) as raw:
            raw.row_factory = sqlite3.Row
            ensure_raw_schema(raw)
            bind_generation_identity(
                raw,
                logical_store="finance_raw",
                generation_id="raw-split-smoke",
                generation_epoch="split-smoke",
                source_fingerprint="sha256:" + ("a" * 64),
            )
            raw.commit()
        manifest = build_manifest(
            state="cutover",
            canonical_source="split",
            generation_epoch="split-smoke",
            raw_generation_id="raw-split-smoke",
            raw_relative_path=str(raw_path.relative_to(runtime)),
            raw_watermark="0",
            operational_generation_id="op-split-smoke",
            operational_relative_path=str(
                operational_path.relative_to(runtime)
            ),
            operational_watermark="fixture",
            rollback_generation_id="monolith",
            source_fingerprint="sha256:" + ("a" * 64),
        )
        atomic_write_manifest(
            runtime / "storage_generation_manifest.json",
            manifest,
        )
        block = WbFinanceWeeklyBlock(
            runtime,
            seller_id="seller-1",
            now_factory=lambda: datetime(
                2026, 7, 7, 3, 0, tzinfo=timezone.utc
            ),
        )
        rows = _fixture_rows()
        first = block.ingest_week(
            date(2026, 6, 22),
            date(2026, 6, 28),
            rows,
        )
        if first["storage_outbox"]["status"] != "committed":
            raise AssertionError("split Finance raw/outbox commit was not atomic")
        second = block.ingest_week(
            date(2026, 6, 22),
            date(2026, 6, 28),
            rows,
            synced_at="2026-07-07T04:00:00Z",
        )
        if second["storage_outbox"]["status"] != "no_op":
            raise AssertionError("repeated split snapshot was not idempotent")
        block.ingest_week(
            date(2026, 6, 22),
            date(2026, 6, 28),
            rows[:-1],
            synced_at="2026-07-07T05:00:00Z",
        )
        recovery_rows = [
            {
                **row,
                "reportId": int(row["reportId"]) + 10_000,
                "rrdId": int(row["rrdId"]) + 10_000,
            }
            for row in rows
        ]
        recovery_hash = hashlib.sha256(
            "\n".join(
                sorted(block._row_hash(row) for row in recovery_rows)  # noqa: SLF001
            ).encode("utf-8")
        ).hexdigest()
        recovery_event = FinanceRawIngestor(
            StoreRegistry(runtime),
            seller_id="seller-1",
            now_factory=lambda: "2026-07-07T06:00:00Z",
        ).ingest_batch(
            recovery_rows,
            source_identity="wb-finance-week:2026-06-29/2026-07-05",
            source_sha256="sha256:" + recovery_hash,
            week_start="2026-06-29",
            week_end="2026-07-05",
        )
        waiting_recovery = block.recover_receipted_split_outbox()
        if (
            waiting_recovery["status"] != "waiting_for_projection"
            or waiting_recovery["pending_sequence_no"]
            != recovery_event.sequence_no
        ):
            raise AssertionError(
                "raw-only event was not left pending for its projection"
            )
        try:
            block._acknowledge_split_outbox(  # noqa: SLF001
                expected_sequence=recovery_event.sequence_no,
            )
        except ValueError as exc:
            if "does not match" not in str(exc):
                raise
        else:
            raise AssertionError(
                "raw-only crash window was acknowledged without projections"
            )
        recovered = block.ingest_week(
            date(2026, 6, 29),
            date(2026, 7, 5),
            recovery_rows,
            synced_at="2026-07-07T06:05:00Z",
        )
        if (
            recovered["storage_outbox"]["status"] != "no_op"
            or recovered["storage_outbox"]["acknowledgement"]["status"]
            != "acknowledged"
        ):
            raise AssertionError(
                "raw-first crash window did not recover idempotently"
            )
        receipt_crash_event = FinanceRawIngestor(
            StoreRegistry(runtime),
            seller_id="seller-1",
            now_factory=lambda: "2026-07-07T06:10:00Z",
        ).ingest_batch(
            recovery_rows,
            source_identity="wb-finance-week-retry:2026-06-29/2026-07-05",
            source_sha256="sha256:" + recovery_hash,
            week_start="2026-06-29",
            week_end="2026-07-05",
        )
        try:
            FinanceOutboxConsumer(
                StoreRegistry(runtime),
                apply_event=block._verify_outbox_projection,  # noqa: SLF001
                now_factory=lambda: "2026-07-07T06:11:00Z",
            ).consume_next(fault_at="after_operational_commit_before_ack")
        except InjectedFinanceStorageFault:
            pass
        else:
            raise AssertionError(
                "receipt-before-raw-ack fault window was not injected"
            )
        receipt_recovery = block.recover_receipted_split_outbox()
        if (
            receipt_recovery["status"] != "acknowledged"
            or len(receipt_recovery["events"]) != 1
            or receipt_recovery["events"][0]["event_id"]
            != receipt_crash_event.event_id
            or receipt_recovery["events"][0]["status"]
            != "duplicate_acknowledged"
        ):
            raise AssertionError(
                "receipted split event did not recover before the next due week"
            )
        if block.recover_receipted_split_outbox()["status"] != "clean":
            raise AssertionError("receipted split recovery was not idempotent")
        with sqlite3.connect(raw_path) as raw:
            if raw.execute(
                "SELECT COUNT(*) FROM finance_raw_rows"
            ).fetchone()[0] != len(rows) + len(recovery_rows):
                raise AssertionError("immutable raw history was unexpectedly lost")
            if raw.execute(
                "SELECT COUNT(*) FROM finance_raw_batch_rows"
            ).fetchone()[0] != (
                len(rows) + len(rows[:-1]) + 2 * len(recovery_rows)
            ):
                raise AssertionError("batch-to-row replay links are incomplete")
            if raw.execute(
                "SELECT COUNT(*) FROM finance_raw_current_rows"
            ).fetchone()[0] != len(rows[:-1]) + len(recovery_rows):
                raise AssertionError(
                    "current raw snapshot did not supersede old rows"
                )
            if raw.execute(
                """SELECT COUNT(*) FROM finance_raw_outbox
                   WHERE published_at IS NULL"""
            ).fetchone()[0] != 0:
                raise AssertionError("split Finance outbox was not acknowledged")
            if raw.execute(
                """SELECT last_sequence_no
                   FROM finance_raw_consumer_cursors
                   WHERE consumer_id='finance_operational_projection_v1'"""
            ).fetchone()[0] != 4:
                raise AssertionError("split raw outbox cursor did not advance")
        with sqlite3.connect(operational_path) as operational:
            if operational.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type='table'
                     AND name='wb_finance_weekly_raw_rows'"""
            ).fetchone():
                raise AssertionError(
                    "split operational store retained Finance raw rows"
                )
            sync = operational.execute(
                """SELECT raw_row_count FROM wb_finance_weekly_sync
                   WHERE seller_id='seller-1'
                     AND week_start='2026-06-22'"""
            ).fetchone()
            if sync is None or int(sync[0]) != len(rows[:-1]):
                raise AssertionError(
                    "split operational projection did not read current raw"
                )
            if operational.execute(
                "SELECT COUNT(*) FROM finance_operational_receipts"
            ).fetchone()[0] != 4:
                raise AssertionError(
                    "split operational outbox receipts are incomplete"
                )
            dead_letter = operational.execute(
                """SELECT status FROM finance_operational_dead_letters
                   WHERE event_id=?""",
                (recovery_event.event_id,),
            ).fetchone()
            if dead_letter is None or dead_letter[0] != "resolved":
                raise AssertionError(
                    "recovered split event did not resolve its failure evidence"
                )
        raw_before_plan = _file_sha256(raw_path)
        operational_before_plan = _file_sha256(operational_path)
        canonical_plan = block.plan_canonical_finance_backfill()
        expected_current_rows = len(rows[:-1]) + len(recovery_rows)
        if (
            canonical_plan["finance_row_count"] != expected_current_rows
            or canonical_plan["source_manifests"]["finance"]["row_count"]
            != expected_current_rows
        ):
            raise AssertionError(
                "canonical dry-run did not read the selected raw generation"
            )
        if (
            _file_sha256(raw_path) != raw_before_plan
            or _file_sha256(operational_path) != operational_before_plan
        ):
            raise AssertionError(
                "canonical split dry-run changed a persistent store"
            )
        observations = StoreRegistry(runtime).status()[
            "open_observations"
        ]
        if not any(
            item["logical_store"] == "finance_raw"
            and item["mode"] == "ro"
            and item["operation"] == "wb_finance_weekly_raw_read"
            for item in observations
        ):
            raise AssertionError(
                "split Finance raw reads bypassed registry observation"
            )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    if (
        classify_deduction(
            {"bonusTypeName": 'Аванс за услугу "Баллы за отзывы" cpm-12345'}
        )
        != "review_points"
    ):
        raise AssertionError("review-points classifier must win over opaque ids")
    if (
        classify_deduction(
            {
                "bonusTypeName": (
                    'Возврат неиспользованного остатка аванса за услугу '
                    '"Баллы за отзывы"'
                )
            }
        )
        != "review_points"
    ):
        raise AssertionError("review-points refund classifier mismatch")


def _assert_signed_deduction_contract(block: WbFinanceWeeklyBlock) -> None:
    base = {
        "dateFrom": "2026-05-04",
        "dateTo": "2026-05-10",
        "reportType": 1,
        "nmId": "",
        "vendorCode": "",
        "sku": "",
        "saleDt": "2026-05-05",
        "docTypeName": "",
        "sellerOperName": "Удержание",
        "quantity": 0,
    }
    rows = [
        {
            **base,
            "reportId": 501,
            "rrdId": 5010,
            "deduction": "7",
            "bonusTypeName": "Оказание услуг WB Продвижение",
        },
        {
            **base,
            "reportId": 502,
            "rrdId": 5020,
            "deduction": "15",
            "bonusTypeName": 'Аванс за услугу "Баллы за отзывы" cpm-12345',
        },
        {
            **base,
            "reportId": 503,
            "rrdId": 5030,
            "deduction": "-5",
            "bonusTypeName": (
                'Возврат неиспользованного остатка аванса за услугу '
                '"Баллы за отзывы"'
            ),
        },
        {
            **base,
            "reportId": 504,
            "rrdId": 5040,
            "deduction": "-4",
            "bonusTypeName": "Услуги доставки транзитных поставок",
        },
        {
            **base,
            "reportId": 505,
            "rrdId": 5050,
            "deduction": "0",
            "paidAcceptance": "-3",
            "bonusTypeName": "Сторно приёмки",
        },
    ]
    result = block.ingest_week(date(2026, 5, 4), date(2026, 5, 10), rows)
    metrics = result["aggregate"]
    expected = {
        "marketing": "7.0000",
        "review_points": "10.0000",
        "transit_logistics": "-4.0000",
        "acceptance": "-3.0000",
        "total_wb_expenses": "10.0000",
        "wb_expenses_without_marketing": "3.0000",
        "profit_period_expenses": "10.0000",
        "capitalized_acceptance": "0.0000",
        "capitalized_transit_logistics": "0.0000",
    }
    for key, value in expected.items():
        if metrics.get(key) != value:
            raise AssertionError(
                f"signed deduction {key}: expected {value!r}, got {metrics.get(key)!r}"
            )
    if metrics.get("before_cogs_profit") != "-10.0000":
        raise AssertionError("signed deductions must affect Finance profit exactly once")


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
            CREATE TABLE sheet_vitrina_v1_warehouse_functional_cutovers(
                cutover_id TEXT PRIMARY KEY,cutover_at TEXT NOT NULL,status TEXT NOT NULL,
                plan_fingerprint TEXT NOT NULL UNIQUE,source_watermarks_json TEXT NOT NULL,
                absorbed_supply_revisions_json TEXT NOT NULL,backup_json TEXT NOT NULL,
                created_at TEXT NOT NULL,updated_at TEXT NOT NULL
            );
            CREATE TABLE sheet_vitrina_v1_warehouse_wb_daily_cost(
                cutover_id TEXT NOT NULL,as_of_date TEXT NOT NULL,nm_id INTEGER NOT NULL,
                quantity TEXT NOT NULL,wac_rub TEXT NOT NULL,capital_rub TEXT NOT NULL,
                quality TEXT NOT NULL,provenance_json TEXT NOT NULL,fingerprint TEXT NOT NULL,
                created_at TEXT NOT NULL,PRIMARY KEY(cutover_id,as_of_date,nm_id)
            );
            INSERT INTO registry_upload_current_state VALUES(1,'bundle','2026-01-01');
            INSERT INTO registry_upload_config_v2 VALUES('bundle',101,1,'SKU','Group',1);
            INSERT INTO cost_price_current_state VALUES(1,'cost','2026-01-01');
            INSERT INTO cost_price_upload_rows VALUES('cost',1,'Group','100','2026-01-01');
            INSERT INTO cost_price_upload_rows VALUES('cost',2,'Anti-Spy','115','2026-01-28');
            INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES(1,101,'VC101','4600000000101','["4600000000101"]','other');
            INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES(1,102,'ANTI102','4600000000102','["4600000000102"]','anti_spy');
            INSERT INTO sheet_vitrina_v1_warehouse_functional_cutovers VALUES(
                'warehouse_functional_cutover_v1','2026-07-01T00:00:00Z','posted',
                'sha256:fixture-cutover','{}','[]','{}',
                '2026-07-01T00:00:00Z','2026-07-01T00:00:00Z'
            );
            INSERT INTO sheet_vitrina_v1_warehouse_wb_daily_cost VALUES(
                'warehouse_functional_cutover_v1','2026-07-01',101,'10','100','1000',
                'periodic_snapshot_wac_closed','{}','sha256:fixture-row-101','2026-07-01T00:00:00Z'
            );
            INSERT INTO sheet_vitrina_v1_warehouse_wb_daily_cost VALUES(
                'warehouse_functional_cutover_v1','2026-07-01',102,'10','115','1150',
                'periodic_snapshot_wac_closed','{}','sha256:fixture-row-102','2026-07-01T00:00:00Z'
            );
            """
        )
        conn.commit()


def _assert_fbs_channel_partial_coverage(block: WbFinanceWeeklyBlock) -> None:
    """One resolver keeps exact FBS facilities separate and missing fail closed."""

    def identity_hash(value: str) -> str:
        return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()

    with sqlite3.connect(block.db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE sheet_vitrina_v1_wb_supplies_fbs_order_observations(
                observation_sequence INTEGER PRIMARY KEY,
                order_id INTEGER NOT NULL,
                rid_sha256 TEXT NOT NULL DEFAULT '',
                order_uid_sha256 TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE sheet_vitrina_v1_ff_pool_cutover_manifests(
                cutover_id TEXT PRIMARY KEY,cutover_at TEXT NOT NULL
            );
            CREATE TABLE sheet_vitrina_v1_ff_pool_fbs_lifecycle_current(
                cutover_id TEXT NOT NULL,order_id INTEGER NOT NULL,
                facility_id TEXT NOT NULL,pool TEXT NOT NULL,nm_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,frozen_wac_rub TEXT NOT NULL,
                debit_event_id TEXT NOT NULL
            );
            CREATE TABLE sheet_vitrina_v1_ff_pool_fbs_lifecycle_events(
                event_sequence INTEGER PRIMARY KEY,event_id TEXT NOT NULL,
                event_type TEXT NOT NULL,evidence_digest TEXT NOT NULL,
                occurred_at TEXT NOT NULL,details_json TEXT NOT NULL,
                source_observed_at TEXT NOT NULL
            );
            INSERT INTO sheet_vitrina_v1_ff_pool_cutover_manifests
            VALUES('cutover-channel-cost','2026-07-15T08:00:00Z');
            INSERT INTO sheet_vitrina_v1_ff_pool_fbs_lifecycle_events VALUES
            (1,'event-msk','handoff_debit','sha256:event-msk','2026-07-15T09:00:00Z','{}','2026-07-15T08:59:00Z'),
            (2,'event-orenburg','handoff_debit','sha256:event-orenburg','2026-07-15T09:01:00Z','{}','2026-07-15T09:00:00Z');
            INSERT INTO sheet_vitrina_v1_ff_pool_fbs_lifecycle_current VALUES
            ('cutover-channel-cost',71001,'fac_moscow','FBS',101,1,'80','event-msk'),
            ('cutover-channel-cost',71002,'fac_orenburg','FBS',101,1,'120','event-orenburg');
            """
        )
        conn.executemany(
            """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_order_observations
               (observation_sequence,order_id,rid_sha256,order_uid_sha256)
               VALUES(?,?,?,?)""",
            [
                (1, 71001, identity_hash("synthetic-msk-order"), ""),
                (2, 71002, identity_hash("synthetic-orenburg-order"), ""),
                (3, 71003, identity_hash("synthetic-no-handoff-order"), ""),
            ],
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_warehouse_wb_daily_cost VALUES(
               'warehouse_functional_cutover_v1','2026-07-15',101,'10','100','1000',
               'periodic_snapshot_wac_closed','{}','sha256:fixture-row-101-jul15',
               '2026-07-15T00:00:00Z')"""
        )
        conn.commit()

    base = {
        "dateFrom": "2026-07-13",
        "dateTo": "2026-07-19",
        "reportId": 713,
        "reportType": 1,
        "nmId": 101,
        "vendorCode": "VC101",
        "sku": "4600000000101",
        "docTypeName": "Продажа",
        "sellerOperName": "Продажа",
        "quantity": 1,
        "forPay": "500",
        "rrDate": "2026-07-15",
    }
    rows = [
        {
            **base,
            "rrdId": 7131,
            "retailPriceWithDisc": "500",
            "rid": "synthetic-msk-order",
            "deliveryType": "fbs",
        },
        {
            **base,
            "rrdId": 7132,
            "retailPriceWithDisc": "500",
            "rid": "synthetic-orenburg-order",
            "deliveryType": "fbs",
        },
        {
            **base,
            "rrdId": 7133,
            "retailPriceWithDisc": "500",
            "rid": "synthetic-unknown-order",
            "deliveryType": "fbs",
        },
        {
            **base,
            "rrdId": 7134,
            "retailPriceWithDisc": "400",
            "forPay": "400",
            "rid": "synthetic-no-handoff-order",
            "deliveryType": "fbs",
        },
        {
            **base,
            "rrdId": 7135,
            "retailPriceWithDisc": "500",
            "rid": "",
            "deliveryType": "fbo",
        },
    ]
    result = block.ingest_week(date(2026, 7, 13), date(2026, 7, 19), rows)
    metrics = result["aggregate"]
    expected = {
        "net_revenue": "2400.0000",
        "profit_revenue_covered": "1500.0000",
        "profit_revenue_uncovered": "900.0000",
        "sales_without_cost_rub": "900.0000",
        "orders_without_cost": 2,
        "units_without_cost": 2,
        "cogs": "300.0000",
        "profit_after_cogs": "1200.0000",
        "final_margin_pct": "80.0000",
        "profit_coverage_status": "partial",
    }
    for key, value in expected.items():
        if metrics.get(key) != value:
            raise AssertionError(f"FBS partial coverage {key}: {metrics.get(key)!r}")
    payload_week = next(
        item
        for item in block.build_payload()["weeks"]
        if item["week_start"] == "2026-07-13"
    )
    coverage = payload_week["cost_coverage"]
    if (
        coverage["uncovered_fbs_sales_revenue_rub"] != "900.0000"
        or coverage["uncovered_fbs_sales_order_count"] != 2
        or coverage["uncovered_fbs_sales_units"] != 2
    ):
        raise AssertionError(f"FBS warning evidence is not exact: {coverage}")
    if coverage["quality"]["source_units"] != {
        "projected_from_2026_07_01": 0,
        "canonical_exact_date": 1,
        "fbs_exact_handoff": 2,
    }:
        raise AssertionError(f"channel source split mismatch: {coverage}")
    reasons = {
        item["reason"] for item in coverage["problem_skus"]
    }
    if reasons != {"fbs_order_identity_missing", "fbs_handoff_cost_missing"}:
        raise AssertionError(f"FBS missing reason evidence mismatch: {coverage}")
    with sqlite3.connect(block.db_path) as conn:
        sku_row = conn.execute(
            """SELECT coverage_json FROM wb_finance_weekly_sku_aggregates
               WHERE seller_id='seller-1' AND week_start='2026-07-13' AND nm_id='101'"""
        ).fetchone()
    sku_coverage = json.loads(str(sku_row[0]))
    facilities = {
        str(item["facility_id"])
        for item in sku_coverage["detail_rows"]
        if item["channel"] == "FBS"
    }
    if facilities != {"fac_moscow", "fac_orenburg"}:
        raise AssertionError(f"FBS facilities were mixed or lost: {sku_coverage}")


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
