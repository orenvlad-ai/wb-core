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
        closed_sku = {
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
                "stock_ru_far_siberia": 12.0,
            },
        }
        today_sku = {
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
                closed_sku_values=closed_sku,
                today_sku_values=today_sku,
            ),
        )
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at="2026-04-19T09:06:00Z",
            plan=_build_plan(
                as_of_date="2026-04-17",
                closed_date="2026-04-17",
                today_date="2026-04-18",
                current_state=current_state,
                metric_labels=metric_labels,
                closed_sku_values={
                    nm_ids[0]: {"stock_total": 88.0, "stock_ru_central": 12.0},
                    nm_ids[1]: {"stock_total": 77.0, "stock_ru_south_caucasus": 11.0},
                },
                today_sku_values=today_sku,
            ),
        )

        payload = SheetVitrinaV1StockReportBlock(
            runtime=runtime,
            now_factory=lambda: NOW,
        ).build()

        if payload.get("status") != "available":
            raise AssertionError(f"stock report must be available, got {payload}")
        if payload.get("report_date") != "2026-04-18":
            raise AssertionError(f"stock report date must default to previous closed business day, got {payload}")
        if payload.get("threshold_lt") != 50:
            raise AssertionError(f"stock threshold must stay <50, got {payload}")

        source_of_truth = payload.get("source_of_truth") or {}
        if source_of_truth != {
            "read_model": "persisted_ready_snapshot",
            "sheet_name": "DATA_VITRINA",
            "snapshot_as_of_date": "2026-04-18",
            "temporal_slot": "yesterday_closed",
            "slot_date": "2026-04-18",
        }:
            raise AssertionError(f"stock report must disclose exact source seam, got {source_of_truth}")

        district_map = {
            item["metric_key"]: item["label"]
            for item in payload.get("districts") or []
        }
        expected_district_map = dict(STOCK_REPORT_DISTRICTS)
        if district_map != expected_district_map:
            raise AssertionError(f"stock report must expose compact district labels, got {district_map}")
        if "stock_ru_far_siberia" in district_map:
            raise AssertionError(f"whole merged far-east bucket must be excluded from the district map, got {district_map}")

        rows = payload.get("rows") or []
        if payload.get("row_count") != 3 or len(rows) != 3:
            raise AssertionError(f"stock report must keep only breached SKU rows, got {payload}")
        if [int(item["nm_id"]) for item in rows] != [nm_ids[2], nm_ids[0], nm_ids[1]]:
            raise AssertionError(f"stock severity sort must be min stock asc, then breadth desc, got {rows}")

        first_breaches = {(item["metric_key"], item["label"], item["stock"]) for item in rows[0]["breached_districts"]}
        if first_breaches != {
            ("stock_ru_northwest", "Северо-Западный ФО", 7.0),
        }:
            raise AssertionError(f"stock report must surface only breached districts with short truthful labels, got {rows[0]}")

        if rows[2]["min_breached_stock"] != 49.0 or rows[2]["breached_district_count"] != 1:
            raise AssertionError(f"secondary severity ordering must stay truthful after far-east exclusion, got {rows[2]}")
        if any(
            district["metric_key"] == "stock_ru_far_siberia"
            for row in rows
            for district in row["breached_districts"]
        ):
            raise AssertionError(f"far-east merged bucket must not be rendered in breached districts, got {rows}")
        if nm_ids[3] in [int(item["nm_id"]) for item in rows]:
            raise AssertionError(f"SKU with only far-east breach must be excluded from the report, got {rows}")

        override_payload = SheetVitrinaV1StockReportBlock(
            runtime=runtime,
            now_factory=lambda: NOW,
        ).build(as_of_date="2026-04-17")
        if override_payload.get("report_date") != "2026-04-17":
            raise AssertionError(f"explicit stock report as_of_date must override the default, got {override_payload}")
        override_source = override_payload.get("source_of_truth") or {}
        if override_source.get("snapshot_as_of_date") != "2026-04-17" or override_source.get("slot_date") != "2026-04-17":
            raise AssertionError(f"explicit stock report as_of_date must keep the requested closed-day seam, got {override_payload}")
        if not override_payload.get("rows"):
            raise AssertionError(f"explicit stock report as_of_date must keep rows for the requested closed day, got {override_payload}")

        print("stock_report_status: ok ->", payload["status"])
        print("stock_report_source: ok ->", source_of_truth["read_model"], source_of_truth["temporal_slot"])
        print("stock_report_threshold: ok ->", payload["threshold_lt"])
        print("stock_report_rows: ok ->", ", ".join(item["identity_label"] for item in rows))
        print("stock_report_districts: ok ->", ", ".join(item["label"] for item in rows[0]["breached_districts"]))
        print("stock_report_override: ok ->", override_payload["report_date"])


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
