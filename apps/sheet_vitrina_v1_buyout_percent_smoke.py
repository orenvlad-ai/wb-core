"""Targeted smoke for SKU buyoutPercent and the three-closed-week reference."""

from __future__ import annotations

from datetime import date, datetime, timezone
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
    BUYOUT_PERCENT_METRIC_KEY,
    build_three_closed_week_buyout_reference,
    extend_metrics_with_buyout_percent,
    three_closed_week_keys,
)
from packages.application.sheet_vitrina_v1_web_vitrina import (  # noqa: E402
    SheetVitrinaV1WebVitrinaBlock,
)
from packages.contracts.sheet_vitrina_v1 import (  # noqa: E402
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
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
NOW = datetime(2026, 8, 5, 8, 0, tzinfo=timezone.utc)
TODAY = date(2026, 8, 5)


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
        effective_metric = next(
            item
            for item in extend_metrics_with_buyout_percent(current_state.metrics_v2)
            if item.metric_key == BUYOUT_PERCENT_METRIC_KEY
        )
        if (
            not effective_metric.enabled
            or not effective_metric.show_in_data
            or effective_metric.scope != "SKU"
            or effective_metric.format != "percent"
        ):
            raise AssertionError(f"buyoutPercent must be effective-visible, got {effective_metric}")

        enabled_skus = [item for item in current_state.config_v2 if item.enabled]
        first_nm_id = enabled_skus[0].nm_id
        second_nm_id = enabled_skus[1].nm_id
        _save_snapshot(
            runtime,
            "2026-08-04",
            [
                _item("2026-08-04", first_nm_id, BUYOUT_PERCENT_METRIC_KEY, 0.55),
                _item("2026-08-04", second_nm_id, BUYOUT_PERCENT_METRIC_KEY, 0.7),
            ],
        )
        _save_snapshot(
            runtime,
            "2026-08-05",
            [
                _item("2026-08-05", first_nm_id, BUYOUT_PERCENT_METRIC_KEY, 0.65),
                _item("2026-08-05", second_nm_id, BUYOUT_PERCENT_METRIC_KEY, 0.75),
            ],
        )
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at="2026-08-05T08:05:00Z",
            plan=_old_ready_snapshot_without_buyout(first_nm_id=first_nm_id),
        )
        contract = SheetVitrinaV1WebVitrinaBlock(
            runtime=runtime,
            now_factory=lambda: NOW,
        ).build(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
        )
        rows_by_id = {row.row_id: row for row in contract.rows}
        first_row = rows_by_id[f"SKU:{first_nm_id}|{BUYOUT_PERCENT_METRIC_KEY}"]
        second_row = rows_by_id[f"SKU:{second_nm_id}|{BUYOUT_PERCENT_METRIC_KEY}"]
        if first_row.values_by_date != {"2026-08-04": 0.55, "2026-08-05": 0.65}:
            raise AssertionError(f"first SKU buyout values mismatch: {first_row}")
        if second_row.values_by_date != {"2026-08-04": 0.7, "2026-08-05": 0.75}:
            raise AssertionError(f"second SKU buyout values mismatch: {second_row}")
        if (
            first_row.metric_label != "Процент выкупа"
            or first_row.section != "Воронка"
            or first_row.format != "percent"
        ):
            raise AssertionError(f"buyoutPercent presentation mismatch: {first_row}")

        _seed_three_closed_week_reference(
            runtime,
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
            or reference["weighted_average_pct"] != "83"
            or reference["included_sku_day_count"] != 3
            or reference["order_count_weight"] != "100"
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

        calculation_parameters = CalculationParametersBlock(runtime=runtime)
        with patch(
            "packages.application.calculation_parameters.current_business_date_iso",
            return_value=TODAY.isoformat(),
        ):
            settings_reference = calculation_parameters.get_payload()["reference"]
        if settings_reference["buyout_percent"]["weighted_average_pct"] != "83":
            raise AssertionError(
                "settings payload must expose buyout reference even before Finance aggregates exist"
            )

        template = SETTINGS_TEMPLATE.read_text(encoding="utf-8")
        for token in (
            "Процент выкупа за 3 закрытые недели",
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
        print("buyout_percent_vitrina_snapshot_projection: ok ->", first_row.values_by_date)
        print("buyout_percent_three_closed_weeks: ok ->", reference["weighted_average_pct"])
        print("buyout_percent_current_week_excluded: ok ->", reference["date_to"])
        print("buyout_percent_settings_line: ok -> informational only")
        print("proxy_formula_unchanged: ok ->", proxy["proxy_profit_3"])


def _seed_three_closed_week_reference(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    first_nm_id: int,
    second_nm_id: int,
) -> None:
    _save_snapshot(
        runtime,
        "2026-07-13",
        [
            _item("2026-07-13", first_nm_id, BUYOUT_PERCENT_METRIC_KEY, 0.5),
            _item("2026-07-13", first_nm_id, "orderCount", 10),
            _item("2026-07-13", first_nm_id, "buyoutCount", 999999),
        ],
    )
    _save_snapshot(
        runtime,
        "2026-07-14",
        [
            _item("2026-07-14", first_nm_id, BUYOUT_PERCENT_METRIC_KEY, 0.99),
            _item("2026-07-14", first_nm_id, "orderCount", 0),
        ],
    )
    _save_snapshot(
        runtime,
        "2026-07-20",
        [
            _item("2026-07-20", first_nm_id, BUYOUT_PERCENT_METRIC_KEY, 0.8),
            _item("2026-07-20", first_nm_id, "orderCount", 30),
        ],
    )
    _save_snapshot(
        runtime,
        "2026-07-21",
        [
            _item("2026-07-21", first_nm_id, BUYOUT_PERCENT_METRIC_KEY, 1.2),
            _item("2026-07-21", first_nm_id, "orderCount", 100),
        ],
    )
    _save_snapshot(
        runtime,
        "2026-07-22",
        [_item("2026-07-22", first_nm_id, BUYOUT_PERCENT_METRIC_KEY, 0.7)],
    )
    _save_snapshot(
        runtime,
        "2026-07-23",
        [_item("2026-07-23", first_nm_id, "orderCount", 20)],
    )
    _save_snapshot(
        runtime,
        "2026-08-02",
        [
            _item("2026-08-02", second_nm_id, BUYOUT_PERCENT_METRIC_KEY, 0.9),
            _item("2026-08-02", second_nm_id, "orderCount", 60),
        ],
    )
    _save_snapshot(
        runtime,
        "2026-08-03",
        [
            _item("2026-08-03", first_nm_id, BUYOUT_PERCENT_METRIC_KEY, 0.01),
            _item("2026-08-03", first_nm_id, "orderCount", 10000),
        ],
    )


def _save_snapshot(
    runtime: RegistryUploadDbBackedRuntime,
    snapshot_date: str,
    items: list[dict[str, object]],
) -> None:
    runtime.save_temporal_source_snapshot(
        source_key="sales_funnel_history",
        snapshot_date=snapshot_date,
        captured_at=f"{snapshot_date}T20:00:00Z",
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


def _old_ready_snapshot_without_buyout(*, first_nm_id: int) -> SheetVitrinaV1Envelope:
    date_columns = ["2026-08-04", "2026-08-05"]
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
        as_of_date="2026-08-04",
        date_columns=date_columns,
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(
                slot_key="yesterday_closed",
                slot_label="Yesterday closed",
                column_date="2026-08-04",
            ),
            SheetVitrinaV1TemporalSlot(
                slot_key="today_current",
                slot_label="Today current",
                column_date="2026-08-05",
            ),
        ],
        source_temporal_policies={"sales_funnel_history": "dual_day_capable"},
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect="A1:D2",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=["label", "key", *date_columns],
                rows=[
                    [
                        "SKU: Заказы",
                        f"SKU:{first_nm_id}|orderCount",
                        1,
                        2,
                    ]
                ],
                row_count=1,
                column_count=4,
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
                        "2026-08-05",
                        "2026-08-05",
                        "2026-08-05",
                        "2026-08-05",
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
