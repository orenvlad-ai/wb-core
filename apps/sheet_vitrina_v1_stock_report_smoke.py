"""Targeted smoke-check for the sheet_vitrina_v1 stock-report builder."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_stock_report import (
    STOCK_REPORT_DISTRICTS,
    SheetVitrinaV1StockReportBlock,
)
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
    with TemporaryDirectory(prefix="sheet-vitrina-stock-report-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        result = runtime.ingest_bundle(bundle, activated_at="2026-04-19T09:00:00Z")
        if result.status != "accepted":
            raise AssertionError(f"bundle ingest must be accepted, got {result}")

        current_state = runtime.load_current_state()
        enabled = [item for item in current_state.config_v2 if item.enabled][:4]
        if len(enabled) < 4:
            raise AssertionError("fixture must expose at least 4 enabled SKU rows")

        nm_ids = [item.nm_id for item in enabled]
        metric_labels = {item.metric_key: item.label_ru for item in current_state.metrics_v2 if item.enabled}
        today_sku = {
            nm_ids[0]: {
                "stock_total": 120.0,
                "stock_ru_central": 34.0,
                "stock_ru_northwest": 60.0,
                "stock_ru_volga": 80.0,
                "stock_ru_ural": 90.0,
                "stock_ru_south_caucasus": 75.0,
                "stock_ru_far_siberia": 110.0,
            },
            nm_ids[1]: {
                "stock_total": 70.0,
                "stock_ru_central": 55.0,
                "stock_ru_northwest": 80.0,
                "stock_ru_volga": 75.0,
                "stock_ru_ural": 95.0,
                "stock_ru_south_caucasus": 49.0,
                "stock_ru_far_siberia": 7.0,
            },
            nm_ids[2]: {
                "stock_total": 40.0,
                "stock_ru_central": 70.0,
                "stock_ru_northwest": 7.0,
                "stock_ru_volga": 55.0,
                "stock_ru_ural": 60.0,
                "stock_ru_south_caucasus": 90.0,
                "stock_ru_far_siberia": 80.0,
            },
            nm_ids[3]: {
                "stock_total": 210.0,
                "stock_ru_central": 70.0,
                "stock_ru_northwest": 60.0,
                "stock_ru_volga": 80.0,
                "stock_ru_ural": 90.0,
                "stock_ru_south_caucasus": 100.0,
                "stock_ru_far_siberia": 120.0,
            },
        }
        older_sku = {
            nm_id: {"stock_total": 999.0}
            for nm_id in nm_ids
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
                closed_sku_values=older_sku,
                today_sku_values=today_sku,
            ),
        )

        payload = SheetVitrinaV1StockReportBlock(
            runtime=runtime,
            now_factory=lambda: NOW,
        ).build()

        if payload.get("status") != "available":
            raise AssertionError(f"stock report must be available, got {payload}")
        if payload.get("report_date") != "2026-04-19":
            raise AssertionError(f"stock report date must be current business day, got {payload}")
        if payload.get("threshold_lt") != 50:
            raise AssertionError(f"stock threshold must stay <50, got {payload}")

        source_of_truth = payload.get("source_of_truth") or {}
        if source_of_truth != {
            "read_model": "persisted_ready_snapshot",
            "sheet_name": "DATA_VITRINA",
            "snapshot_as_of_date": "2026-04-18",
            "temporal_slot": "today_current",
            "slot_date": "2026-04-19",
        }:
            raise AssertionError(f"stock report must disclose exact source seam, got {source_of_truth}")

        district_map = {
            item["metric_key"]: item["label"]
            for item in payload.get("districts") or []
        }
        expected_district_map = dict(STOCK_REPORT_DISTRICTS)
        if district_map != expected_district_map:
            raise AssertionError(f"stock report must expose compact district labels, got {district_map}")

        rows = payload.get("rows") or []
        if payload.get("row_count") != 3 or len(rows) != 3:
            raise AssertionError(f"stock report must keep only breached SKU rows, got {payload}")
        if [int(item["nm_id"]) for item in rows] != [nm_ids[1], nm_ids[2], nm_ids[0]]:
            raise AssertionError(f"stock severity sort must be min stock asc, then breadth desc, got {rows}")

        first_breaches = {(item["metric_key"], item["label"], item["stock"]) for item in rows[0]["breached_districts"]}
        if first_breaches != {
            ("stock_ru_south_caucasus", "Юг и СКФО", 49.0),
            ("stock_ru_far_siberia", "ДВ и Сибирь", 7.0),
        }:
            raise AssertionError(f"stock report must surface only breached districts with short truthful labels, got {rows[0]}")

        if rows[1]["min_breached_stock"] != 7.0 or rows[1]["breached_district_count"] != 1:
            raise AssertionError(f"secondary severity ordering must stay truthful, got {rows[1]}")

        print("stock_report_status: ok ->", payload["status"])
        print("stock_report_source: ok ->", source_of_truth["read_model"], source_of_truth["temporal_slot"])
        print("stock_report_threshold: ok ->", payload["threshold_lt"])
        print("stock_report_rows: ok ->", ", ".join(item["identity_label"] for item in rows))
        print("stock_report_districts: ok ->", ", ".join(item["label"] for item in rows[0]["breached_districts"]))


def _build_plan(
    *,
    as_of_date: str,
    closed_date: str,
    today_date: str,
    current_state: object,
    metric_labels: dict[str, str],
    closed_sku_values: dict[int, dict[str, float]],
    today_sku_values: dict[int, dict[str, float]],
) -> SheetVitrinaV1Envelope:
    rows = []
    for config_item in current_state.config_v2:
        if not config_item.enabled:
            continue
        closed_values = closed_sku_values.get(config_item.nm_id, {})
        today_values = today_sku_values.get(config_item.nm_id, {})
        for metric_key in [
            "stock_total",
            "stock_ru_central",
            "stock_ru_northwest",
            "stock_ru_volga",
            "stock_ru_ural",
            "stock_ru_south_caucasus",
            "stock_ru_far_siberia",
        ]:
            if metric_key not in today_values and metric_key not in closed_values:
                continue
            rows.append(
                [
                    f"{config_item.display_name}: {metric_labels.get(metric_key, metric_key)}",
                    f"SKU:{config_item.nm_id}|{metric_key}",
                    closed_values.get(metric_key, ""),
                    today_values.get(metric_key, ""),
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
        snapshot_id=f"stock-report-smoke-{uuid4().hex}",
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
