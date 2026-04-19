"""HTTP integration smoke-check for the sheet_vitrina_v1 daily-report operator surface."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from typing import Any
from urllib import request as urllib_request
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_SHEET_DAILY_REPORT_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig
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
    with TemporaryDirectory(prefix="sheet-vitrina-daily-report-http-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        port = _reserve_free_port()
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: "2026-04-19T09:00:00Z",
            now_factory=lambda: NOW,
        )
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=port,
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path="/v1/sheet-vitrina-v1/refresh",
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime_dir,
        )
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{config.port}"
            upload_status, upload_payload = _post_json(f"{base_url}{DEFAULT_UPLOAD_PATH}", bundle)
            if upload_status != 200 or upload_payload.get("status") != "accepted":
                raise AssertionError(f"bundle upload must be accepted, got {upload_status} {upload_payload}")

            current_state = runtime.load_current_state()
            enabled = [item for item in current_state.config_v2 if item.enabled][:4]
            nm_ids = [item.nm_id for item in enabled]
            metric_labels = {item.metric_key: item.label_ru for item in current_state.metrics_v2 if item.enabled}
            old_sku, new_sku = _seed_sku_values(nm_ids)
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

            operator_status, operator_html = _get_text(f"{base_url}{DEFAULT_SHEET_OPERATOR_UI_PATH}")
            if operator_status != 200:
                raise AssertionError(f"operator page must return 200, got {operator_status}")
            for expected in (
                "Ежедневные отчёты",
                "Total Order Sum",
                "Загрузить данные",
                "Отправить данные",
                "Сервер и расписание",
                DEFAULT_SHEET_DAILY_REPORT_PATH,
            ):
                if expected not in operator_html:
                    raise AssertionError(f"operator page must expose {expected!r}")

            status_code, status_payload = _get_json(f"{base_url}{DEFAULT_SHEET_STATUS_PATH}")
            if status_code != 200 or status_payload.get("status") != "success":
                raise AssertionError(f"status route must stay readable, got {status_code} {status_payload}")

            plan_code, plan_payload = _get_json(f"{base_url}{DEFAULT_SHEET_PLAN_PATH}")
            if plan_code != 200 or plan_payload.get("as_of_date") != "2026-04-18":
                raise AssertionError(f"plan route must stay readable, got {plan_code} {plan_payload}")

            report_code, report_payload = _get_json(f"{base_url}{DEFAULT_SHEET_DAILY_REPORT_PATH}")
            if report_code != 200 or report_payload.get("status") != "available":
                raise AssertionError(f"daily report route must return available JSON, got {report_code} {report_payload}")
            if report_payload.get("newer_closed_date") != "2026-04-18":
                raise AssertionError(f"daily report must expose current closed-day compare date, got {report_payload}")
            if not report_payload.get("top_metric_declines") or not report_payload.get("top_sku_order_sum_declines"):
                raise AssertionError(f"daily report must publish ranked blocks, got {report_payload}")

            print("operator_daily_report_html: ok ->", DEFAULT_SHEET_OPERATOR_UI_PATH)
            print("status_route: ok ->", status_payload["status"])
            print("plan_route: ok ->", plan_payload["as_of_date"])
            print("daily_report_route: ok ->", report_payload["newer_closed_date"], report_payload["older_closed_date"])
        finally:
            server.shutdown()
            thread.join(timeout=5)


def _seed_sku_values(nm_ids: list[int]) -> tuple[dict[int, dict[str, float]], dict[int, dict[str, float]]]:
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
            "stock_total": 40.0,
            "stock_ru_central": 15.0,
            "stock_ru_northwest": 18.0,
            "stock_ru_volga": 12.0,
            "stock_ru_ural": 4.0,
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
    return old_sku, new_sku


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
        snapshot_id=f"daily-report-http-smoke-{uuid4().hex}",
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


def _reserve_free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _post_json(url: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib_request.Request(
        url=url,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
        },
        data=json.dumps(payload).encode("utf-8"),
    )
    with urllib_request.urlopen(request, timeout=10) as response:
        return response.getcode(), json.loads(response.read().decode("utf-8"))


def _get_json(url: str) -> tuple[int, dict[str, Any]]:
    request = urllib_request.Request(url=url, method="GET", headers={"Accept": "application/json"})
    with urllib_request.urlopen(request, timeout=10) as response:
        return response.getcode(), json.loads(response.read().decode("utf-8"))


def _get_text(url: str) -> tuple[int, str]:
    request = urllib_request.Request(url=url, method="GET", headers={"Accept": "text/html"})
    with urllib_request.urlopen(request, timeout=10) as response:
        return response.getcode(), response.read().decode("utf-8")


if __name__ == "__main__":
    main()
