"""Targeted smoke-check for daily-report metric decline diagnostics."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_daily_report import SheetVitrinaV1DailyReportBlock
from packages.contracts.sheet_vitrina_v1 import (
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)

BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
NOW = datetime(2026, 4, 19, 9, 0, tzinfo=timezone.utc)
STATUS_HEADER = [
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


def main() -> None:
    bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="sheet-vitrina-daily-report-diagnostic-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        result = runtime.ingest_bundle(bundle, activated_at="2026-04-19T09:00:00Z")
        if result.status != "accepted":
            raise AssertionError(f"bundle ingest must be accepted, got {result}")

        current_state = runtime.load_current_state()
        metric_labels = {item.metric_key: item.label_ru for item in current_state.metrics_v2 if item.enabled}
        enabled_nm_ids = [item.nm_id for item in current_state.config_v2 if item.enabled][:2]
        if len(enabled_nm_ids) < 2:
            raise AssertionError("fixture must expose at least 2 enabled SKU rows")

        older_totals = {
            "total_orderSum": 3000.0,
            "total_view_count": 100.0,
            "total_views_current": 100.0,
            "total_open_card_count": 100.0,
            "avg_ctr_current": 0.10,
            "avg_addToCartConversion": 0.20,
            "avg_cartToOrderConversion": 0.30,
            "avg_spp": 0.25,
            "total_ads_views": 1000.0,
            "total_ads_sum": 100.0,
            "avg_localizationPercent": 0.50,
        }
        newer_totals = {
            "total_orderSum": 3300.0,
            "total_view_count": 110.0,
            "total_views_current": 110.0,
            "total_open_card_count": 110.0,
            "avg_ctr_current": 0.11,
            "avg_addToCartConversion": 0.19,
            "avg_cartToOrderConversion": 0.29,
            "avg_spp": 0.26,
            "total_ads_views": 1100.0,
            "total_ads_sum": 105.0,
            "avg_localizationPercent": 0.49,
        }
        older_order_sum = {
            enabled_nm_ids[0]: 1500.0,
            enabled_nm_ids[1]: 1500.0,
        }
        newer_order_sum = {
            enabled_nm_ids[0]: 1700.0,
            enabled_nm_ids[1]: 1600.0,
        }

        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at="2026-04-19T09:05:00Z",
            plan=_build_plan(
                as_of_date="2026-04-18",
                closed_date="2026-04-18",
                today_date="2026-04-19",
                current_state=current_state,
                metric_labels=metric_labels,
                total_values=newer_totals,
                sku_order_sum=newer_order_sum,
            ),
        )
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at="2026-04-18T09:05:00Z",
            plan=_build_plan(
                as_of_date="2026-04-17",
                closed_date="2026-04-17",
                today_date="2026-04-18",
                current_state=current_state,
                metric_labels=metric_labels,
                total_values=older_totals,
                sku_order_sum=older_order_sum,
            ),
        )

        payload = SheetVitrinaV1DailyReportBlock(
            runtime=runtime,
            now_factory=lambda: NOW,
        ).build()
        diagnostics = payload.get("metric_ranking_diagnostics") or {}
        if payload.get("status") != "available":
            raise AssertionError(f"daily report must be available, got {payload}")
        if diagnostics.get("raw_candidate_count") != 11:
            raise AssertionError(f"raw metric candidate count must stay 11, got {diagnostics}")
        if diagnostics.get("present_after_none_filter_count") != 10:
            raise AssertionError(f"present metric count must stay 10, got {diagnostics}")
        if diagnostics.get("negative_count") != 3:
            raise AssertionError(f"negative metric count must stay 3, got {diagnostics}")
        if diagnostics.get("positive_count") != 7:
            raise AssertionError(f"positive metric count must stay 7, got {diagnostics}")
        if diagnostics.get("flat_or_unknown_count") != 0:
            raise AssertionError(f"flat/unknown metric count must stay 0, got {diagnostics}")

        excluded = diagnostics.get("excluded_from_declines") or []
        missing_bid = next(
            (
                item
                for item in excluded
                if item.get("metric_key") == "avg_ads_bid_search"
            ),
            None,
        )
        if missing_bid is None or missing_bid.get("reason") != "missing_both_closed_day_values":
            raise AssertionError(f"avg_ads_bid_search must be excluded due to missing closed-day values, got {excluded}")
        if len(payload.get("top_metric_declines") or []) != 3:
            raise AssertionError(f"decline list must truthfully expose only 3 negative metrics, got {payload}")

        print("daily_report_metric_diagnostic: ok -> raw=11 present=10 negative=3 positive=7")
        print("daily_report_metric_missing: ok ->", missing_bid["metric_key"], missing_bid["reason"])
        print("daily_report_metric_decline_count: ok ->", len(payload["top_metric_declines"]))


def _build_plan(
    *,
    as_of_date: str,
    closed_date: str,
    today_date: str,
    current_state: Any,
    metric_labels: dict[str, str],
    total_values: dict[str, float],
    sku_order_sum: dict[int, float],
) -> SheetVitrinaV1Envelope:
    rows = []
    total_metric_keys = [
        "total_orderSum",
        "total_view_count",
        "total_views_current",
        "total_open_card_count",
        "avg_ctr_current",
        "avg_addToCartConversion",
        "avg_cartToOrderConversion",
        "avg_spp",
        "total_ads_views",
        "total_ads_sum",
        "avg_localizationPercent",
    ]
    for metric_key in total_metric_keys:
        rows.append(
            [
                f"Итого: {metric_labels.get(metric_key, metric_key)}",
                f"TOTAL|{metric_key}",
                total_values[metric_key],
                "",
            ]
        )

    for config_item in current_state.config_v2:
        if not config_item.enabled or config_item.nm_id not in sku_order_sum:
            continue
        rows.append(
            [
                f"{config_item.display_name}: {metric_labels.get('orderSum', 'orderSum')}",
                f"SKU:{config_item.nm_id}|orderSum",
                sku_order_sum[config_item.nm_id],
                "",
            ]
        )

    data_header = ["label", "key", closed_date, today_date]
    temporal_slots = [
        SheetVitrinaV1TemporalSlot(
            slot_key="yesterday_closed",
            slot_label="Вчера (закрытый день)",
            column_date=closed_date,
        ),
        SheetVitrinaV1TemporalSlot(
            slot_key="today_current",
            slot_label="Сегодня (текущий день)",
            column_date=today_date,
        ),
    ]
    return SheetVitrinaV1Envelope(
        plan_version="sheet_vitrina_v1_temporal_live_v1__sheet_scaffold_v1",
        snapshot_id=f"daily-report-diagnostic-{uuid4().hex}",
        as_of_date=as_of_date,
        date_columns=[closed_date, today_date],
        temporal_slots=temporal_slots,
        source_temporal_policies={},
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect=f"A1:D{len(rows) + 1}",
                clear_range="A:Z",
                write_mode="replace",
                partial_update_allowed=False,
                header=data_header,
                rows=rows,
                row_count=len(rows),
                column_count=len(data_header),
            ),
            SheetVitrinaWriteTarget(
                sheet_name="STATUS",
                write_start_cell="A1",
                write_rect="A1:K1",
                clear_range="A:K",
                write_mode="replace",
                partial_update_allowed=False,
                header=STATUS_HEADER,
                rows=[],
                row_count=0,
                column_count=len(STATUS_HEADER),
            ),
        ],
    )


if __name__ == "__main__":
    main()
