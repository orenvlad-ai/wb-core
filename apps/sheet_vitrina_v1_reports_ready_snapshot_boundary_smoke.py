"""Regression smoke for Reports default-read ready snapshot selection around night boundary."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_daily_report import SheetVitrinaV1DailyReportBlock
from packages.application.sheet_vitrina_v1_stock_report import SheetVitrinaV1StockReportBlock
from packages.contracts.sheet_vitrina_v1 import (
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)

BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
NIGHT_NOW = datetime(2026, 5, 11, 2, 29, tzinfo=ZoneInfo("Asia/Yekaterinburg"))
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
    with TemporaryDirectory(prefix="sheet-vitrina-reports-ready-boundary-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        result = runtime.ingest_bundle(bundle, activated_at="2026-05-11T00:00:00Z")
        if result.status != "accepted":
            raise AssertionError(f"bundle ingest must be accepted, got {result}")

        current_state = runtime.load_current_state()
        enabled_nm_ids = [item.nm_id for item in current_state.config_v2 if item.enabled][:2]
        if len(enabled_nm_ids) < 2:
            raise AssertionError("fixture must expose at least 2 enabled SKU rows")

        _save_snapshot(runtime, current_state, as_of_date="2026-05-08", refreshed_at="2026-05-09T06:00:00Z")
        _save_snapshot(runtime, current_state, as_of_date="2026-05-09", refreshed_at="2026-05-10T06:00:00Z")

        daily_payload = SheetVitrinaV1DailyReportBlock(
            runtime=runtime,
            now_factory=lambda: NIGHT_NOW,
        ).build()
        if daily_payload.get("status") != "available":
            raise AssertionError(f"daily report must fallback to persisted ready dates, got {daily_payload}")
        if daily_payload.get("requested_as_of_date") != "2026-05-10":
            raise AssertionError(f"daily report must disclose requested default date, got {daily_payload}")
        if daily_payload.get("current_as_of_date") != "2026-05-09":
            raise AssertionError(f"daily report must select latest persisted ready date <= requested, got {daily_payload}")
        if daily_payload.get("previous_as_of_date") != "2026-05-08":
            raise AssertionError(f"daily report must select second latest persisted ready date, got {daily_payload}")

        stock_payload = SheetVitrinaV1StockReportBlock(
            runtime=runtime,
            now_factory=lambda: NIGHT_NOW,
        ).build()
        if stock_payload.get("status") != "available":
            raise AssertionError(f"stock report default must fallback to latest persisted ready date, got {stock_payload}")
        if stock_payload.get("requested_as_of_date") != "2026-05-10":
            raise AssertionError(f"stock report must disclose requested default date, got {stock_payload}")
        if stock_payload.get("report_date") != "2026-05-09":
            raise AssertionError(f"stock report default must read latest persisted ready date, got {stock_payload}")
        source = stock_payload.get("source_of_truth") or {}
        if source.get("snapshot_as_of_date") != "2026-05-09" or source.get("slot_date") != "2026-05-09":
            raise AssertionError(f"stock source seam must match selected persisted ready date, got {stock_payload}")

        strict_payload = SheetVitrinaV1StockReportBlock(
            runtime=runtime,
            now_factory=lambda: NIGHT_NOW,
        ).build(as_of_date="2026-05-10")
        if strict_payload.get("status") != "unavailable":
            raise AssertionError(f"explicit stock as_of_date must stay strict exact-read, got {strict_payload}")
        if strict_payload.get("report_date") != "2026-05-10":
            raise AssertionError(f"strict stock report must not rewrite requested date, got {strict_payload}")
        if "2026-05-10" not in str(strict_payload.get("reason")):
            raise AssertionError(f"strict stock report must explain missing requested date, got {strict_payload}")

        print("reports_ready_boundary_daily: ok -> requested 2026-05-10 selected 2026-05-09/2026-05-08")
        print("reports_ready_boundary_stock_default: ok -> requested 2026-05-10 selected 2026-05-09")
        print("reports_ready_boundary_stock_explicit: ok -> 2026-05-10 stays strict unavailable")


def _save_snapshot(
    runtime: RegistryUploadDbBackedRuntime,
    current_state: Any,
    *,
    as_of_date: str,
    refreshed_at: str,
) -> None:
    runtime.save_sheet_vitrina_ready_snapshot(
        current_state=current_state,
        refreshed_at=refreshed_at,
        plan=_build_plan(as_of_date=as_of_date, current_state=current_state),
    )


def _build_plan(*, as_of_date: str, current_state: Any) -> SheetVitrinaV1Envelope:
    closed_date = as_of_date
    today_date = (datetime.fromisoformat(as_of_date) + timedelta(days=1)).date().isoformat()
    metric_labels = {item.metric_key: item.label_ru for item in current_state.metrics_v2 if item.enabled}
    enabled_nm_ids = [item.nm_id for item in current_state.config_v2 if item.enabled][:2]
    rows: list[list[Any]] = [
        ["Итого: Total Order Sum", "TOTAL|total_orderSum", 10_000.0, ""],
        ["Итого: Просмотры", "TOTAL|total_view_count", 1000.0, ""],
        ["Итого: Просмотры в поиске", "TOTAL|total_views_current", 800.0, ""],
        ["Итого: CTR", "TOTAL|avg_ctr_current", 0.12, ""],
        ["Итого: Add to cart", "TOTAL|avg_addToCartConversion", 0.2, ""],
        ["Итого: Cart to order", "TOTAL|avg_cartToOrderConversion", 0.3, ""],
        ["Итого: SPP", "TOTAL|avg_spp", 0.15, ""],
        ["Итого: Ads views", "TOTAL|total_ads_views", 700.0, ""],
        ["Итого: Ads sum", "TOTAL|total_ads_sum", 55.0, ""],
        ["Итого: Localization", "TOTAL|avg_localizationPercent", 0.9, ""],
    ]
    for index, nm_id in enumerate(enabled_nm_ids):
        rows.extend(
            [
                [f"SKU {nm_id}: {metric_labels.get('orderSum', 'orderSum')}", f"SKU:{nm_id}|orderSum", 3000.0 + index, ""],
                [f"SKU {nm_id}: stock_total", f"SKU:{nm_id}|stock_total", 40.0 + index, ""],
                [f"SKU {nm_id}: stock_ru_central", f"SKU:{nm_id}|stock_ru_central", 10.0 + index, ""],
                [f"SKU {nm_id}: stock_ru_northwest", f"SKU:{nm_id}|stock_ru_northwest", 20.0 + index, ""],
                [f"SKU {nm_id}: stock_ru_volga", f"SKU:{nm_id}|stock_ru_volga", 30.0 + index, ""],
                [f"SKU {nm_id}: stock_ru_ural", f"SKU:{nm_id}|stock_ru_ural", 40.0 + index, ""],
                [f"SKU {nm_id}: stock_ru_south_caucasus", f"SKU:{nm_id}|stock_ru_south_caucasus", 45.0 + index, ""],
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
        snapshot_id=f"reports-ready-boundary-{uuid4().hex}",
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
