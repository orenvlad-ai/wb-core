"""Targeted smoke for SKU buyoutPercent and the three-closed-week reference."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.calculation_parameters import (  # noqa: E402
    CalculationParametersBlock,
    DEFAULT_PROXY_PARAMETERS,
    calculate_proxy_3,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.sheet_vitrina_v1_buyout_percent import (  # noqa: E402
    BUYOUT_PERCENT_AGGREGATION_RULE,
    BUYOUT_PERCENT_MATURITY_DAYS,
    BUYOUT_PERCENT_METRIC_KEY,
    LEGACY_AVG_BUYOUT_PERCENT_METRIC_KEY,
    aggregate_buyout_percent,
    build_three_closed_week_buyout_reference,
    capture_mature_buyout_percent_snapshots,
    extend_metrics_with_buyout_percent,
    three_closed_week_keys,
    trusted_buyout_cutoff,
)
from packages.application.sheet_vitrina_v1_web_vitrina import (  # noqa: E402
    SheetVitrinaV1WebVitrinaBlock,
)
from packages.business_time import current_business_date_iso  # noqa: E402
from packages.contracts.sheet_vitrina_v1 import (  # noqa: E402
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)
from packages.contracts.sales_funnel_history_block import (  # noqa: E402
    SalesFunnelHistoryEnvelope,
    SalesFunnelHistoryItem,
    SalesFunnelHistorySuccess,
)


BUNDLE_FIXTURE = (
    ROOT
    / "artifacts"
    / "registry_upload_http_entrypoint"
    / "input"
    / "registry_upload_bundle__fixture.json"
)
SETTINGS_TEMPLATE = (
    ROOT / "packages" / "adapters" / "templates" / "sheet_vitrina_v1_settings.html"
)
NOW = datetime(2026, 8, 9, 8, 0, tzinfo=timezone.utc)
TODAY = date(2026, 8, 9)


class _FakeMatureHistoryBlock:
    def __init__(self, *, positive_nm_ids: set[int]) -> None:
        self.calls: list[tuple[str, str]] = []
        self.positive_nm_ids = set(positive_nm_ids)

    def execute(self, request: object) -> SalesFunnelHistoryEnvelope:
        date_from = str(getattr(request, "date_from"))
        date_to = str(getattr(request, "date_to"))
        nm_ids = [int(value) for value in getattr(request, "nm_ids")]
        self.calls.append((date_from, date_to))
        items: list[SalesFunnelHistoryItem] = []
        current = date.fromisoformat(date_from)
        end = date.fromisoformat(date_to)
        while current <= end:
            snapshot_date = current.isoformat()
            buyout_percent = 0.9 if snapshot_date == "2026-08-02" else 0.96
            for nm_id in nm_ids:
                if nm_id in self.positive_nm_ids:
                    items.extend([
                        SalesFunnelHistoryItem(
                            date=snapshot_date,
                            nm_id=nm_id,
                            metric=BUYOUT_PERCENT_METRIC_KEY,
                            value=buyout_percent,
                        ),
                        SalesFunnelHistoryItem(
                            date=snapshot_date,
                            nm_id=nm_id,
                            metric="orderCount",
                            value=30 if snapshot_date == "2026-08-02" else 10,
                        ),
                    ])
                else:
                    items.append(
                        SalesFunnelHistoryItem(
                            date=snapshot_date,
                            nm_id=nm_id,
                            metric="orderCount",
                            value=0,
                        )
                    )
            current += timedelta(days=1)
        return SalesFunnelHistoryEnvelope(
            result=SalesFunnelHistorySuccess(
                kind="success",
                date_from=date_from,
                date_to=date_to,
                count=len(items),
                items=items,
            )
        )


def main() -> None:
    bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="sheet-vitrina-buyout-percent-") as temp_dir:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(temp_dir))
        accepted = runtime.ingest_bundle(bundle, activated_at="2026-08-05T08:00:00Z")
        if accepted.status != "accepted":
            raise AssertionError(f"fixture bundle must be accepted, got {accepted}")

        current_state = runtime.load_current_state()
        source_metric = next(
            item
            for item in current_state.metrics_v2
            if item.metric_key == BUYOUT_PERCENT_METRIC_KEY
        )
        if source_metric.enabled:
            raise AssertionError("fixture must retain the historical disabled registry flag")
        effective_metrics = extend_metrics_with_buyout_percent(current_state.metrics_v2)
        effective_metric = next(
            item
            for item in effective_metrics
            if item.metric_key == BUYOUT_PERCENT_METRIC_KEY
        )
        if (
            not effective_metric.enabled
            or not effective_metric.show_in_data
            or effective_metric.scope != "SKU"
            or effective_metric.format != "percent"
        ):
            raise AssertionError(f"buyoutPercent must be effective-visible, got {effective_metric}")
        legacy_average = next(
            item
            for item in effective_metrics
            if item.metric_key == LEGACY_AVG_BUYOUT_PERCENT_METRIC_KEY
        )
        if legacy_average.enabled or legacy_average.show_in_data:
            raise AssertionError(
                f"legacy arithmetic average must remain nonpublic, got {legacy_average}"
            )

        enabled_skus = [item for item in current_state.config_v2 if item.enabled]
        enabled_nm_ids = [item.nm_id for item in enabled_skus]
        first_nm_id = enabled_skus[0].nm_id
        second_nm_id = enabled_skus[1].nm_id
        _save_snapshot(
            runtime,
            "2026-08-03",
            [
                _item("2026-08-03", first_nm_id, BUYOUT_PERCENT_METRIC_KEY, 0.55),
                _item("2026-08-03", first_nm_id, "orderCount", 10),
                _item("2026-08-03", first_nm_id, "buyoutCount", 999999),
                _item("2026-08-03", second_nm_id, BUYOUT_PERCENT_METRIC_KEY, 0.7),
                _item("2026-08-03", second_nm_id, "orderCount", 30),
            ]
            + [
                _item("2026-08-03", nm_id, "orderCount", 0)
                for nm_id in enabled_nm_ids
                if nm_id not in {first_nm_id, second_nm_id}
            ],
            captured_at="2026-08-09T08:00:00Z",
        )
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at="2026-08-09T08:05:00Z",
            plan=_old_ready_snapshot_with_immature_buyout(
                first_nm_id=first_nm_id,
                second_nm_id=second_nm_id,
            ),
        )
        contract = SheetVitrinaV1WebVitrinaBlock(
            runtime=runtime,
            now_factory=lambda: NOW,
        ).build(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            as_of_date="2026-08-08",
        )
        rows_by_id = {row.row_id: row for row in contract.rows}
        total_row = rows_by_id[f"TOTAL|{BUYOUT_PERCENT_METRIC_KEY}"]
        first_row = rows_by_id[f"SKU:{first_nm_id}|{BUYOUT_PERCENT_METRIC_KEY}"]
        second_row = rows_by_id[f"SKU:{second_nm_id}|{BUYOUT_PERCENT_METRIC_KEY}"]
        expected_total = {
            "2026-08-03": 0.6625,
            **{f"2026-08-{day:02d}": "" for day in range(4, 10)},
        }
        if total_row.values_by_date != expected_total:
            raise AssertionError(
                "daily TOTAL must be weighted at D-6 and blank throughout D0..D-5, "
                f"got {total_row.values_by_date}"
            )
        if (
            total_row.scope_kind != "TOTAL"
            or total_row.metric_key != BUYOUT_PERCENT_METRIC_KEY
            or total_row.metric_label != "Процент выкупа"
            or total_row.format != "percent"
        ):
            raise AssertionError(f"paired TOTAL presentation mismatch: {total_row}")
        expected_first = {
            "2026-08-03": 0.55,
            **{f"2026-08-{day:02d}": "" for day in range(4, 10)},
        }
        if first_row.values_by_date != expected_first:
            raise AssertionError(f"first SKU buyout values mismatch: {first_row}")
        expected_second = {
            "2026-08-03": 0.7,
            **{f"2026-08-{day:02d}": "" for day in range(4, 10)},
        }
        if second_row.values_by_date != expected_second:
            raise AssertionError(f"second SKU buyout values mismatch: {second_row}")
        if (
            first_row.metric_label != "Процент выкупа"
            or first_row.section != "Воронка"
            or first_row.format != "percent"
        ):
            raise AssertionError(f"buyoutPercent presentation mismatch: {first_row}")
        if any(
            row.metric_key == LEGACY_AVG_BUYOUT_PERCENT_METRIC_KEY
            for row in contract.rows
        ):
            raise AssertionError("legacy avg_buyoutPercent must not enter the public read contract")
        if trusted_buyout_cutoff(TODAY) != date(2026, 8, 3):
            raise AssertionError("D-6 trusted cutoff changed")
        if BUYOUT_PERCENT_MATURITY_DAYS != 6:
            raise AssertionError("public maturity threshold must remain six calendar days")
        before_ekt_midnight = date.fromisoformat(
            current_business_date_iso(datetime(2026, 8, 8, 18, 59, tzinfo=timezone.utc))
        )
        after_ekt_midnight = date.fromisoformat(
            current_business_date_iso(datetime(2026, 8, 8, 19, 0, tzinfo=timezone.utc))
        )
        if (
            trusted_buyout_cutoff(before_ekt_midnight) != date(2026, 8, 2)
            or trusted_buyout_cutoff(after_ekt_midnight) != date(2026, 8, 3)
        ):
            raise AssertionError("buyout maturity must roll over at Asia/Yekaterinburg midnight")

        empty_aggregation = aggregate_buyout_percent(
            [
                (1.2, 100),
                (0.5, 0),
                (0.8, None),
                (None, 20),
            ]
        )
        if (
            empty_aggregation.value is not None
            or empty_aggregation.order_count_weight != 0
            or empty_aggregation.included_pair_count != 0
        ):
            raise AssertionError(
                f"invalid/missing/zero-weight pairs must yield blank aggregation, got {empty_aggregation}"
            )

        _seed_three_closed_week_reference(
            runtime,
            enabled_nm_ids=enabled_nm_ids,
            first_nm_id=first_nm_id,
            second_nm_id=second_nm_id,
        )
        reference = build_three_closed_week_buyout_reference(
            runtime=runtime,
            today=TODAY,
        )
        if (
            reference["date_from"] != "2026-07-13"
            or reference["date_to"] != "2026-08-02"
            or reference["business_timezone"] != "Asia/Yekaterinburg"
            or reference["trusted_cutoff"] != "2026-08-03"
            or reference["weighted_average_pct"] != "80"
            or reference["included_sku_day_count"] != 42
            or reference["order_count_weight"] != "840"
            or [week["weighted_average_pct"] for week in reference["weeks"]]
            != ["50", "80", "90"]
            or any(week["status"] != "ready" for week in reference["weeks"])
        ):
            raise AssertionError(f"three-week buyout reference mismatch: {reference}")
        if reference["aggregation_rule"] != BUYOUT_PERCENT_AGGREGATION_RULE:
            raise AssertionError(f"buyout aggregation contract mismatch: {reference}")
        if "2026-08-03" in reference["available_snapshot_dates"]:
            raise AssertionError("current open week leaked into the buyout reference")
        expected_week_keys = [
            ("2026-07-13", "2026-07-19"),
            ("2026-07-20", "2026-07-26"),
            ("2026-07-27", "2026-08-02"),
        ]
        if (
            three_closed_week_keys(date(2026, 8, 3)) != expected_week_keys
            or three_closed_week_keys(date(2026, 8, 9)) != expected_week_keys
        ):
            raise AssertionError("Monday-Sunday closed-week boundaries changed")
        immature_latest_week = build_three_closed_week_buyout_reference(
            runtime=runtime,
            today=date(2026, 8, 5),
        )
        if (
            immature_latest_week["date_to"] != "2026-08-02"
            or immature_latest_week["weeks"][2]["status"] != "immature"
            or immature_latest_week["weeks"][2]["weighted_average_pct"] is not None
            or immature_latest_week["weighted_average_pct"] != "70"
            or immature_latest_week["ready_week_count"] != 2
            or immature_latest_week["contributing_week_ranges"]
            != [["2026-07-13", "2026-07-19"], ["2026-07-20", "2026-07-26"]]
        ):
            raise AssertionError(
                "latest immature cell must stay blank while combined uses the two READY weeks: "
                + repr(immature_latest_week)
            )

        runtime.delete_temporal_source_snapshots(
            source_key="sales_funnel_history",
            date_from="2026-07-22",
            date_to="2026-07-22",
        )
        incomplete_reference = build_three_closed_week_buyout_reference(
            runtime=runtime,
            today=TODAY,
        )
        if (
            incomplete_reference["weighted_average_pct"] != "80"
            or incomplete_reference["weeks"][1]["status"] != "missing"
            or incomplete_reference["weeks"][1]["weighted_average_pct"] is not None
            or incomplete_reference["ready_week_count"] != 2
        ):
            raise AssertionError(
                "one missing mature date must blank only its week and exclude it from combined: "
                + repr(incomplete_reference)
            )
        _save_week_day(
            runtime,
            "2026-07-22",
            enabled_nm_ids,
            buyout_percent=Decimal("0.8"),
            order_count=Decimal("20"),
            positive_nm_ids={first_nm_id, second_nm_id},
        )

        capture_source = _FakeMatureHistoryBlock(
            positive_nm_ids={first_nm_id, second_nm_id}
        )
        _save_snapshot(
            runtime,
            "2026-08-02",
            [
                _item("2026-08-02", nm_id, BUYOUT_PERCENT_METRIC_KEY, 0.2)
                for nm_id in (first_nm_id, second_nm_id)
            ]
            + [
                _item("2026-08-02", nm_id, "orderCount", 10)
                for nm_id in (first_nm_id, second_nm_id)
            ]
            + [
                _item("2026-08-02", nm_id, "orderCount", 0)
                for nm_id in enabled_nm_ids
                if nm_id not in {first_nm_id, second_nm_id}
            ],
            captured_at="2026-08-08T08:00:00Z",
        )
        _save_snapshot(
            runtime,
            "2026-08-03",
            [
                _item("2026-08-03", nm_id, BUYOUT_PERCENT_METRIC_KEY, 0.2)
                for nm_id in (first_nm_id, second_nm_id)
            ]
            + [
                _item("2026-08-03", nm_id, "orderCount", 10)
                for nm_id in (first_nm_id, second_nm_id)
            ]
            + [
                _item("2026-08-03", nm_id, "orderCount", 0)
                for nm_id in enabled_nm_ids
                if nm_id not in {first_nm_id, second_nm_id}
            ],
            captured_at="2026-08-04T08:00:00Z",
        )
        capture = capture_mature_buyout_percent_snapshots(
            runtime=runtime,
            sales_funnel_history_block=capture_source,  # type: ignore[arg-type]
            enabled_nm_ids=enabled_nm_ids,
            now=NOW,
            captured_at_factory=lambda: "2026-08-09T08:15:00Z",
        )
        if (
            capture.requested_dates != ("2026-08-03",)
            or capture.saved_dates != ("2026-08-03",)
            or capture_source.calls != [("2026-08-03", "2026-08-03")]
        ):
            raise AssertionError(f"D-6 overwrite request mismatch: {capture}")
        repeated_capture = capture_mature_buyout_percent_snapshots(
            runtime=runtime,
            sales_funnel_history_block=capture_source,  # type: ignore[arg-type]
            enabled_nm_ids=enabled_nm_ids,
            now=NOW,
            captured_at_factory=lambda: "2026-08-09T09:00:00Z",
        )
        if repeated_capture.status != "already_captured" or len(capture_source.calls) != 1:
            raise AssertionError("same-business-day mature capture must be idempotent")
        runtime.delete_temporal_source_snapshots(
            source_key="sales_funnel_history",
            date_from="2026-08-02",
            date_to="2026-08-02",
        )
        catch_up = capture_mature_buyout_percent_snapshots(
            runtime=runtime,
            sales_funnel_history_block=capture_source,  # type: ignore[arg-type]
            enabled_nm_ids=enabled_nm_ids,
            now=NOW,
            captured_at_factory=lambda: "2026-08-09T09:10:00Z",
        )
        if (
            catch_up.requested_dates != ("2026-08-02",)
            or capture_source.calls[-1] != ("2026-08-02", "2026-08-02")
        ):
            raise AssertionError(f"bounded D-7 catch-up mismatch: {catch_up}")

        calculation_parameters = CalculationParametersBlock(runtime=runtime)
        with patch(
            "packages.application.calculation_parameters.current_business_date_iso",
            return_value=TODAY.isoformat(),
        ):
            settings_reference = calculation_parameters.get_payload()["reference"]
        if settings_reference["buyout_percent"]["weighted_average_pct"] != "80":
            raise AssertionError(
                "settings payload must expose buyout reference even before Finance aggregates exist"
            )

        template = SETTINGS_TEMPLATE.read_text(encoding="utf-8")
        for token in (
            "Расчётный выкуп (подтверждённый)",
            "Только подтверждённые данные возрастом не менее 6 дней",
            "calculationBuyoutReferenceValue",
            "buyout_percent",
        ):
            if token not in template:
                raise AssertionError(f"settings buyout reference UI token missing: {token}")

        proxy = calculate_proxy_3(
            order_sum="1000",
            order_count="10",
            canonical_wb_wac="20",
            ads_sum="50",
            parameters=DEFAULT_PROXY_PARAMETERS,
        )
        expected_proxy = (
            Decimal("1000") * Decimal("0.5096")
            - Decimal("10") * Decimal("0.91") * Decimal("20")
            - Decimal("50")
        )
        if proxy["proxy_profit_3"] != expected_proxy:
            raise AssertionError("Proxy formula changed while adding the buyout display")

        print("buyout_percent_metric_effective: ok -> SKU percent")
        print("buyout_percent_daily_total: ok ->", total_row.values_by_date)
        print("buyout_percent_immature_mask: ok -> D0..D-5 SKU and TOTAL blank")
        print("buyout_percent_legacy_average_nonpublic: ok")
        print("buyout_percent_vitrina_snapshot_projection: ok ->", first_row.values_by_date)
        print("buyout_percent_three_closed_weeks: ok ->", reference["weighted_average_pct"])
        print("buyout_percent_weekly_cells: ok ->", [week["weighted_average_pct"] for week in reference["weeks"]])
        print("buyout_percent_partial_exclusion: ok -> missing mature day blanks only its week")
        print("buyout_percent_mature_capture: ok -> overwrite + idempotency + D-7 catch-up")
        print("buyout_percent_current_week_excluded: ok ->", reference["date_to"])
        print("buyout_percent_settings_line: ok -> informational only")
        print("proxy_formula_unchanged: ok ->", proxy["proxy_profit_3"])


def _seed_three_closed_week_reference(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    enabled_nm_ids: list[int],
    first_nm_id: int,
    second_nm_id: int,
) -> None:
    for start, end, percentage, orders in (
        (date(2026, 7, 13), date(2026, 7, 19), Decimal("0.5"), Decimal("10")),
        (date(2026, 7, 20), date(2026, 7, 26), Decimal("0.8"), Decimal("20")),
        (date(2026, 7, 27), date(2026, 8, 2), Decimal("0.9"), Decimal("30")),
    ):
        current = start
        while current <= end:
            _save_week_day(
                runtime,
                current.isoformat(),
                enabled_nm_ids,
                buyout_percent=percentage,
                order_count=orders,
                positive_nm_ids={first_nm_id, second_nm_id},
            )
            current += timedelta(days=1)


def _save_week_day(
    runtime: RegistryUploadDbBackedRuntime,
    snapshot_date: str,
    nm_ids: list[int],
    *,
    buyout_percent: Decimal,
    order_count: Decimal,
    positive_nm_ids: set[int],
) -> None:
    _save_snapshot(
        runtime,
        snapshot_date,
        [
            item
            for nm_id in nm_ids
            for item in (
                (
                    _item(snapshot_date, nm_id, BUYOUT_PERCENT_METRIC_KEY, float(buyout_percent)),
                    _item(snapshot_date, nm_id, "orderCount", float(order_count)),
                )
                if nm_id in positive_nm_ids
                else (_item(snapshot_date, nm_id, "orderCount", 0),)
            )
        ],
        captured_at="2026-08-09T08:00:00Z",
    )


def _save_snapshot(
    runtime: RegistryUploadDbBackedRuntime,
    snapshot_date: str,
    items: list[dict[str, object]],
    *,
    captured_at: str | None = None,
) -> None:
    runtime.save_temporal_source_snapshot(
        source_key="sales_funnel_history",
        snapshot_date=snapshot_date,
        captured_at=captured_at or f"{snapshot_date}T20:00:00Z",
        payload={
            "kind": "success",
            "date_from": snapshot_date,
            "date_to": snapshot_date,
            "count": len(items),
            "items": items,
        },
    )


def _item(
    snapshot_date: str,
    nm_id: int,
    metric: str,
    value: object,
) -> dict[str, object]:
    return {
        "date": snapshot_date,
        "nm_id": nm_id,
        "metric": metric,
        "value": value,
    }


def _old_ready_snapshot_with_immature_buyout(
    *,
    first_nm_id: int,
    second_nm_id: int,
) -> SheetVitrinaV1Envelope:
    date_columns = [f"2026-08-{day:02d}" for day in range(3, 10)]
    status_header = [
        "source_key",
        "kind",
        "freshness",
        "snapshot_date",
        "date",
        "date_from",
        "date_to",
        "requested_count",
        "covered_count",
        "missing_nm_ids",
        "note",
    ]
    return SheetVitrinaV1Envelope(
        plan_version="delivery_contract_v1__buyout_read_completion",
        snapshot_id="buyout-percent-old-ready-snapshot",
        as_of_date="2026-08-08",
        date_columns=date_columns,
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(
                slot_key=f"date_{index}",
                slot_label=snapshot_date,
                column_date=snapshot_date,
            )
            for index, snapshot_date in enumerate(date_columns)
        ],
        source_temporal_policies={"sales_funnel_history": "dual_day_capable"},
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect="A1:I4",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=["label", "key", *date_columns],
                rows=[
                    [
                        "SKU: Заказы",
                        f"SKU:{first_nm_id}|orderCount",
                        *([10] * len(date_columns)),
                    ],
                    [
                        "SKU: Старый процент",
                        f"SKU:{first_nm_id}|buyoutPercent",
                        0.99,
                        0,
                        0.2,
                        0.5,
                        0.75,
                        0.88,
                        0.91,
                    ],
                    [
                        "SKU: Старый процент",
                        f"SKU:{second_nm_id}|buyoutPercent",
                        0.01,
                        0.2,
                        0,
                        0.4,
                        0.6,
                        0.7,
                        0.8,
                    ],
                ],
                row_count=3,
                column_count=9,
            ),
            SheetVitrinaWriteTarget(
                sheet_name="STATUS",
                write_start_cell="A1",
                write_rect="A1:K2",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=status_header,
                rows=[
                    [
                        "sales_funnel_history",
                        "success",
                        "fresh",
                        "2026-08-09",
                        "2026-08-09",
                        "2026-08-09",
                        "2026-08-09",
                        2,
                        2,
                        "",
                        "",
                    ]
                ],
                row_count=1,
                column_count=len(status_header),
            ),
        ],
    )


if __name__ == "__main__":
    main()
