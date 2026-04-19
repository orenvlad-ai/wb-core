"""Targeted smoke-check for the sheet_vitrina_v1 daily-report summary builder."""

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
    with TemporaryDirectory(prefix="sheet-vitrina-daily-report-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        result = runtime.ingest_bundle(bundle, activated_at="2026-04-19T09:00:00Z")
        if result.status != "accepted":
            raise AssertionError(f"bundle ingest must be accepted, got {result}")
        current_state = runtime.load_current_state()
        enabled = [item for item in current_state.config_v2 if item.enabled][:4]
        if len(enabled) < 4:
            raise AssertionError("fixture must expose at least 4 enabled SKU rows")

        nm_ids = [item.nm_id for item in enabled]
        old_sku = {
            nm_ids[0]: {
                "orderSum": 1000.0,
                "view_count": 1000.0,
                "views_current": 800.0,
                "open_card_count": 100.0,
                "ctr": 0.10,
                "ctr_current": 0.05,
                "addToCartConversion": 0.20,
                "cartToOrderConversion": 0.30,
                "price_seller_discounted": 1000.0,
                "stock_total": 30.0,
                "stock_ru_central": 20.0,
                "stock_ru_northwest": 10.0,
                "stock_ru_volga": 0.0,
                "stock_ru_ural": 0.0,
                "stock_ru_south_caucasus": 0.0,
                "spp": 0.28,
                "ads_bid_search": 120.0,
                "ads_views": 900.0,
                "ads_sum": 300.0,
                "localizationPercent": 0.55,
            },
            nm_ids[1]: {
                "orderSum": 800.0,
                "view_count": 900.0,
                "views_current": 700.0,
                "open_card_count": 120.0,
                "ctr": 0.12,
                "ctr_current": 0.055,
                "addToCartConversion": 0.18,
                "cartToOrderConversion": 0.25,
                "price_seller_discounted": 900.0,
                "stock_total": 50.0,
                "stock_ru_central": 25.0,
                "stock_ru_northwest": 30.0,
                "stock_ru_volga": 10.0,
                "stock_ru_ural": 5.0,
                "stock_ru_south_caucasus": 0.0,
                "spp": 0.31,
                "ads_bid_search": 115.0,
                "ads_views": 850.0,
                "ads_sum": 250.0,
                "localizationPercent": 0.48,
            },
            nm_ids[2]: {
                "orderSum": 400.0,
                "view_count": 500.0,
                "views_current": 400.0,
                "open_card_count": 50.0,
                "ctr": 0.10,
                "ctr_current": 0.04,
                "addToCartConversion": 0.10,
                "cartToOrderConversion": 0.18,
                "price_seller_discounted": 1200.0,
                "stock_total": 40.0,
                "stock_ru_central": 20.0,
                "stock_ru_northwest": 20.0,
                "stock_ru_volga": 0.0,
                "stock_ru_ural": 0.0,
                "stock_ru_south_caucasus": 0.0,
                "spp": 0.27,
                "ads_bid_search": 100.0,
                "ads_views": 500.0,
                "ads_sum": 180.0,
                "localizationPercent": 0.52,
            },
            nm_ids[3]: {
                "orderSum": 300.0,
                "view_count": 450.0,
                "views_current": 300.0,
                "open_card_count": 40.0,
                "ctr": 0.09,
                "ctr_current": 0.03,
                "addToCartConversion": 0.09,
                "cartToOrderConversion": 0.16,
                "price_seller_discounted": 1300.0,
                "stock_total": 35.0,
                "stock_ru_central": 15.0,
                "stock_ru_northwest": 20.0,
                "stock_ru_volga": 0.0,
                "stock_ru_ural": 0.0,
                "stock_ru_south_caucasus": 0.0,
                "spp": 0.29,
                "ads_bid_search": 90.0,
                "ads_views": 450.0,
                "ads_sum": 140.0,
                "localizationPercent": 0.50,
            },
        }
        new_sku = {
            nm_ids[0]: {
                **old_sku[nm_ids[0]],
                "orderSum": 500.0,
                "view_count": 700.0,
                "views_current": 500.0,
                "open_card_count": 60.0,
                "ctr": 0.08,
                "ctr_current": 0.04,
                "addToCartConversion": 0.15,
                "cartToOrderConversion": 0.20,
                "price_seller_discounted": 1100.0,
                "stock_total": 0.0,
                "stock_ru_central": 0.0,
                "stock_ru_northwest": 0.0,
                "spp": 0.26,
                "ads_bid_search": 135.0,
                "ads_views": 700.0,
                "ads_sum": 260.0,
                "localizationPercent": 0.53,
            },
            nm_ids[1]: {
                **old_sku[nm_ids[1]],
                "orderSum": 600.0,
                "view_count": 750.0,
                "views_current": 600.0,
                "open_card_count": 110.0,
                "ctr": 0.11,
                "ctr_current": 0.05,
                "addToCartConversion": 0.16,
                "cartToOrderConversion": 0.22,
                "price_seller_discounted": 950.0,
                "stock_total": 0.0,
                "stock_ru_central": 0.0,
                "stock_ru_northwest": 0.0,
                "stock_ru_volga": 0.0,
                "stock_ru_ural": 0.0,
                "spp": 0.30,
                "ads_bid_search": 110.0,
                "ads_views": 780.0,
                "ads_sum": 230.0,
                "localizationPercent": 0.47,
            },
            nm_ids[2]: {
                **old_sku[nm_ids[2]],
                "orderSum": 800.0,
                "view_count": 700.0,
                "views_current": 700.0,
                "open_card_count": 90.0,
                "ctr": 0.13,
                "ctr_current": 0.06,
                "addToCartConversion": 0.15,
                "cartToOrderConversion": 0.24,
                "price_seller_discounted": 1100.0,
                "stock_total": 60.0,
                "stock_ru_central": 25.0,
                "stock_ru_northwest": 25.0,
                "spp": 0.29,
                "ads_bid_search": 95.0,
                "ads_views": 760.0,
                "ads_sum": 200.0,
                "localizationPercent": 0.56,
            },
            nm_ids[3]: {
                **old_sku[nm_ids[3]],
                "orderSum": 450.0,
                "view_count": 520.0,
                "views_current": 330.0,
                "open_card_count": 55.0,
                "ctr": 0.10,
                "ctr_current": 0.035,
                "addToCartConversion": 0.11,
                "cartToOrderConversion": 0.20,
                "price_seller_discounted": 1200.0,
                "stock_total": 45.0,
                "stock_ru_central": 18.0,
                "stock_ru_northwest": 22.0,
                "spp": 0.32,
                "ads_bid_search": 80.0,
                "ads_views": 520.0,
                "ads_sum": 150.0,
                "localizationPercent": 0.58,
            },
        }

        metric_labels = {item.metric_key: item.label_ru for item in current_state.metrics_v2 if item.enabled}
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at="2026-04-19T09:05:00Z",
            plan=_build_plan(
                as_of_date="2026-04-18",
                closed_date="2026-04-18",
                today_date="2026-04-19",
                current_state=current_state,
                metric_labels=metric_labels,
                sku_values=new_sku,
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
                sku_values=old_sku,
            ),
        )

        payload = SheetVitrinaV1DailyReportBlock(
            runtime=runtime,
            now_factory=lambda: NOW,
        ).build()

        if payload.get("status") != "available":
            raise AssertionError(f"daily report must be available, got {payload}")
        if payload.get("newer_closed_date") != "2026-04-18" or payload.get("older_closed_date") != "2026-04-17":
            raise AssertionError(f"daily report must compare the two latest closed days, got {payload}")

        total_order_sum = payload.get("total_order_sum") or {}
        if round(float(total_order_sum.get("newer_value") or 0), 2) != 2350.00:
            raise AssertionError(f"new closed-day total_orderSum mismatch: {total_order_sum}")
        if round(float(total_order_sum.get("older_value") or 0), 2) != 2500.00:
            raise AssertionError(f"old closed-day total_orderSum mismatch: {total_order_sum}")

        top_declines = payload.get("top_sku_order_sum_declines") or []
        if not top_declines or int(top_declines[0].get("nm_id") or 0) != nm_ids[0]:
            raise AssertionError(f"strongest declining SKU must be {nm_ids[0]}, got {top_declines}")

        top_growth = payload.get("top_sku_order_sum_growth") or []
        if not top_growth or int(top_growth[0].get("nm_id") or 0) != nm_ids[2]:
            raise AssertionError(f"strongest growing SKU must be {nm_ids[2]}, got {top_growth}")

        negative_factors = payload.get("top_negative_factors") or []
        negative_labels = {item.get("label") for item in negative_factors}
        if "Цена" not in negative_labels or "Нет остатков" not in negative_labels:
            raise AssertionError(f"negative factors must include price and out-of-stock, got {negative_labels}")
        if len(negative_factors) <= 5:
            raise AssertionError(f"negative factors must expose the full valid list, got {negative_factors}")

        positive_factors = payload.get("top_positive_factors") or []
        positive_labels = {item.get("label") for item in positive_factors}
        if "Цена" not in positive_labels:
            raise AssertionError(f"positive factors must include price, got {positive_labels}")
        if len(positive_factors) <= 5:
            raise AssertionError(f"positive factors must expose the full valid list, got {positive_factors}")

        negative_price = next(item for item in negative_factors if item.get("label") == "Цена")
        if negative_price.get("direction") != "up" or "₽" not in str(negative_price.get("aggregate_summary")):
            raise AssertionError(f"negative price factor must expose direction+aggregate, got {negative_price}")
        positive_price = next(item for item in positive_factors if item.get("label") == "Цена")
        if positive_price.get("direction") != "down" or "₽" not in str(positive_price.get("aggregate_summary")):
            raise AssertionError(f"positive price factor must expose direction+aggregate, got {positive_price}")

        _assert_factor_ranking_sorted(negative_factors)
        _assert_factor_ranking_sorted(positive_factors)

        metric_declines = {item.get("metric_key") for item in payload.get("top_metric_declines") or []}
        if "avg_ads_bid_search" not in metric_declines and "total_view_count" not in metric_declines:
            raise AssertionError(f"declining metric pool must include allowed canonical metrics, got {metric_declines}")

        print("daily_report_status: ok ->", payload["status"])
        print("daily_report_closed_days: ok ->", payload["newer_closed_date"], payload["older_closed_date"])
        print("daily_report_top_decline_sku: ok ->", top_declines[0]["identity_label"])
        print("daily_report_top_growth_sku: ok ->", top_growth[0]["identity_label"])
        print("daily_report_negative_factors: ok ->", ", ".join(sorted(negative_labels)))
        print("daily_report_positive_factors: ok ->", ", ".join(sorted(positive_labels)))
        print("daily_report_factor_summary: ok ->", negative_price["aggregate_summary"])


def _assert_factor_ranking_sorted(items: list[dict[str, Any]]) -> None:
    previous = None
    for item in items:
        current = (
            int(item.get("matched_sku_count") or 0),
            float(item.get("aggregate_sort_value") or 0.0),
            str(item.get("label") or ""),
        )
        if previous is not None:
            if current[0] > previous[0]:
                raise AssertionError(f"factor ranking must be non-increasing by count, got {items}")
            if current[0] == previous[0] and current[1] > previous[1] + 1e-9:
                raise AssertionError(f"factor ranking must use aggregate strength as secondary sort, got {items}")
        previous = current


def _build_plan(
    *,
    as_of_date: str,
    closed_date: str,
    today_date: str,
    current_state: Any,
    metric_labels: dict[str, str],
    sku_values: dict[int, dict[str, float]],
) -> SheetVitrinaV1Envelope:
    total_values = _aggregate_totals(sku_values)
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
        "avg_ads_bid_search",
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
        if not config_item.enabled:
            continue
        values = sku_values.get(config_item.nm_id, {})
        for metric_key in [
            "orderSum",
            "view_count",
            "views_current",
            "open_card_count",
            "ctr",
            "ctr_current",
            "addToCartConversion",
            "cartToOrderConversion",
            "price_seller_discounted",
            "stock_total",
            "stock_ru_central",
            "stock_ru_northwest",
            "stock_ru_volga",
            "stock_ru_ural",
            "stock_ru_south_caucasus",
            "spp",
            "ads_bid_search",
            "ads_views",
            "ads_sum",
            "localizationPercent",
        ]:
            if metric_key not in values:
                continue
            rows.append(
                [
                    f"{config_item.display_name}: {metric_labels.get(metric_key, metric_key)}",
                    f"SKU:{config_item.nm_id}|{metric_key}",
                    values[metric_key],
                    "",
                ]
            )

    data_header = ["label", "key", closed_date, today_date]
    status_header = STATUS_HEADER
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
        snapshot_id=f"daily-report-smoke-{uuid4().hex}",
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
                header=status_header,
                rows=[],
                row_count=0,
                column_count=len(status_header),
            ),
        ],
    )


def _aggregate_totals(sku_values: dict[int, dict[str, float]]) -> dict[str, float]:
    rows = list(sku_values.values())
    return {
        "total_orderSum": sum(item["orderSum"] for item in rows),
        "total_view_count": sum(item["view_count"] for item in rows),
        "total_views_current": sum(item["views_current"] for item in rows),
        "total_open_card_count": sum(item["open_card_count"] for item in rows),
        "avg_ctr_current": sum(item["ctr_current"] for item in rows) / len(rows),
        "avg_addToCartConversion": sum(item["addToCartConversion"] for item in rows) / len(rows),
        "avg_cartToOrderConversion": sum(item["cartToOrderConversion"] for item in rows) / len(rows),
        "avg_spp": sum(item["spp"] for item in rows) / len(rows),
        "avg_ads_bid_search": sum(item["ads_bid_search"] for item in rows) / len(rows),
        "total_ads_views": sum(item["ads_views"] for item in rows),
        "total_ads_sum": sum(item["ads_sum"] for item in rows),
        "avg_localizationPercent": sum(item["localizationPercent"] for item in rows) / len(rows),
    }


if __name__ == "__main__":
    main()
