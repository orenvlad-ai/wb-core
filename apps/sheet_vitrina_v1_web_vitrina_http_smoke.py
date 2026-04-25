"""HTTP integration smoke-check for the phase-1 web-vitrina sibling routes."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from urllib import request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_PAGE_COMPOSITION_SURFACE,
    DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
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
NOW = datetime(2026, 4, 20, 9, 0, tzinfo=timezone.utc)
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
    with TemporaryDirectory(prefix="sheet-vitrina-web-vitrina-http-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        current_result = runtime.ingest_bundle(bundle, activated_at="2026-04-20T09:00:00Z")
        if current_result.status != "accepted":
            raise AssertionError(f"fixture bundle must be accepted, got {current_result}")

        current_state = runtime.load_current_state()
        enabled = [item for item in current_state.config_v2 if item.enabled]
        start_date = datetime(2026, 4, 14, tzinfo=timezone.utc).date()
        for offset in range(7):
            snapshot_date = (start_date + timedelta(days=offset)).isoformat()
            runtime.save_sheet_vitrina_ready_snapshot(
                current_state=current_state,
                refreshed_at=f"{snapshot_date}T09:05:00Z",
                plan=_build_plan(
                    as_of_date=snapshot_date,
                    first_nm_id=enabled[0].nm_id,
                    second_nm_id=enabled[1].nm_id,
                    first_group=enabled[0].group,
                ),
            )

        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: "2026-04-20T09:00:00Z",
            now_factory=lambda: NOW,
        )
        seeded_job = _start_completed_refresh_job(entrypoint, runtime)
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=_reserve_free_port(),
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

            contract_status, contract_payload = _get_json(f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_READ_PATH}")
            if contract_status != 200:
                raise AssertionError(f"web-vitrina read route must return 200, got {contract_status}")
            if contract_payload.get("contract_name") != "web_vitrina_contract" or contract_payload.get("contract_version") != "v1":
                raise AssertionError(f"web-vitrina contract identity mismatch, got {contract_payload}")
            if contract_payload.get("page_route") != DEFAULT_SHEET_WEB_VITRINA_UI_PATH:
                raise AssertionError(f"web-vitrina page route mismatch, got {contract_payload}")
            if contract_payload.get("read_route") != DEFAULT_SHEET_WEB_VITRINA_READ_PATH:
                raise AssertionError(f"web-vitrina read route mismatch, got {contract_payload}")
            if contract_payload.get("meta", {}).get("row_count") != 4:
                raise AssertionError(f"web-vitrina meta row_count mismatch, got {contract_payload}")
            if contract_payload.get("status_summary", {}).get("read_model") != "persisted_ready_snapshot":
                raise AssertionError(f"web-vitrina read seam mismatch, got {contract_payload}")
            if contract_payload.get("meta", {}).get("as_of_date") != "2026-04-19":
                raise AssertionError(f"web-vitrina default as_of_date mismatch, got {contract_payload}")
            row_ids = [row["row_id"] for row in contract_payload.get("rows") or []]
            if row_ids != [
                "TOTAL|total_view_count",
                f"GROUP:{enabled[0].group}|view_count",
                f"SKU:{enabled[0].nm_id}|view_count",
                f"SKU:{enabled[1].nm_id}|orderSum",
            ]:
                raise AssertionError(f"web-vitrina rows mismatch, got {row_ids}")

            period_status, period_payload = _get_json(
                f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_READ_PATH}?date_from=2026-04-18&date_to=2026-04-20"
            )
            if period_status != 200:
                raise AssertionError(f"web-vitrina period route must return 200, got {period_status}")
            if period_payload.get("status_summary", {}).get("read_model") != "persisted_ready_snapshot_window":
                raise AssertionError(f"web-vitrina period read seam mismatch, got {period_payload}")
            if period_payload.get("meta", {}).get("as_of_date") != "2026-04-20":
                raise AssertionError(f"web-vitrina period as_of_date mismatch, got {period_payload}")
            if period_payload.get("meta", {}).get("date_columns") != ["2026-04-18", "2026-04-19", "2026-04-20"]:
                raise AssertionError(f"web-vitrina period date columns mismatch, got {period_payload}")

            composition_status, composition_payload = _get_json(
                f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_READ_PATH}?surface={DEFAULT_SHEET_WEB_VITRINA_PAGE_COMPOSITION_SURFACE}"
            )
            if composition_status != 200:
                raise AssertionError(f"web-vitrina page composition surface must return 200, got {composition_status}")
            if composition_payload.get("composition_name") != "web_vitrina_page_composition":
                raise AssertionError(f"web-vitrina page composition identity mismatch, got {composition_payload}")
            if composition_payload.get("meta", {}).get("current_state") != "ready":
                raise AssertionError(f"web-vitrina page composition state mismatch, got {composition_payload}")
            if composition_payload.get("table_surface", {}).get("total_row_count") != 4:
                raise AssertionError(f"web-vitrina page composition row count mismatch, got {composition_payload}")
            historical_access = composition_payload.get("historical_access") or {}
            if historical_access.get("current_mode") != "default":
                raise AssertionError(f"web-vitrina historical selector mode mismatch, got {composition_payload}")
            if historical_access.get("supported_query_mode") != "date_window":
                raise AssertionError(f"web-vitrina historical query mode mismatch, got {historical_access}")
            if [item.get("value") for item in historical_access.get("options") or []] != [
                "2026-04-20",
                "2026-04-19",
                "2026-04-18",
                "2026-04-17",
                "2026-04-16",
                "2026-04-15",
                "2026-04-14",
            ]:
                raise AssertionError(f"web-vitrina historical selector options mismatch, got {historical_access}")
            if [item.get("preset_id") for item in historical_access.get("preset_options") or []] != [
                "week",
                "two_weeks",
                "month",
                "quarter",
                "year",
            ]:
                raise AssertionError(f"web-vitrina preset options mismatch, got {historical_access}")

            period_composition_status, period_composition_payload = _get_json(
                f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_READ_PATH}?surface={DEFAULT_SHEET_WEB_VITRINA_PAGE_COMPOSITION_SURFACE}&date_from=2026-04-18&date_to=2026-04-20"
            )
            if period_composition_status != 200:
                raise AssertionError(f"web-vitrina period page composition must return 200, got {period_composition_status}")
            if period_composition_payload.get("historical_access", {}).get("current_mode") != "historical_period":
                raise AssertionError(f"web-vitrina period page composition mode mismatch, got {period_composition_payload}")
            if period_composition_payload.get("historical_access", {}).get("selected_date_from") != "2026-04-18":
                raise AssertionError(f"web-vitrina period selected_date_from mismatch, got {period_composition_payload}")
            if period_composition_payload.get("historical_access", {}).get("selected_date_to") != "2026-04-20":
                raise AssertionError(f"web-vitrina period selected_date_to mismatch, got {period_composition_payload}")
            activity_surface = composition_payload.get("activity_surface") or {}
            log_block = activity_surface.get("log_block", {})
            if log_block.get("tone") != "error":
                raise AssertionError(f"web-vitrina log block must surface persisted warning/error fallback, got {activity_surface}")
            if "refresh" not in str(log_block.get("subtitle", "")):
                raise AssertionError(f"web-vitrina log block subtitle mismatch, got {activity_surface}")
            upload_items = activity_surface.get("upload_summary", {}).get("items") or []
            if "update_summary" in activity_surface:
                raise AssertionError(f"web-vitrina activity surface must not expose the removed update block, got {activity_surface}")
            loading_table = activity_surface.get("loading_table", {})
            loading_rows = loading_table.get("rows") or []
            loading_columns = {item.get("id"): item for item in loading_table.get("columns") or []}
            loading_groups = {item.get("group_id"): item for item in loading_table.get("groups") or []}
            if [row.get("source_key") for row in loading_rows] != [item.get("source_key") for item in upload_items]:
                raise AssertionError(f"web-vitrina loading table must follow upload source truth, got {activity_surface}")
            if sorted(loading_groups) != ["other_sources", "seller_portal_bot", "wb_api"]:
                raise AssertionError(f"web-vitrina loading table must expose stable source groups, got {loading_groups}")
            if not loading_groups["seller_portal_bot"].get("session_controls"):
                raise AssertionError(f"seller portal group must expose session controls, got {loading_groups}")
            if {row.get("source_group_id") for row in loading_rows} != {"wb_api", "seller_portal_bot"}:
                raise AssertionError(f"loading table rows must be grouped by source group, got {loading_rows}")
            if not str((loading_columns.get("today_status") or {}).get("label") or "").startswith("Сегодня: "):
                raise AssertionError(f"web-vitrina loading table today column mismatch, got {loading_table}")
            if not str((loading_columns.get("yesterday_status") or {}).get("label") or "").startswith("Вчера: "):
                raise AssertionError(f"web-vitrina loading table yesterday column mismatch, got {loading_table}")
            for required_column in ("today_reason", "yesterday_reason", "metrics", "technical_endpoint"):
                if required_column not in loading_columns:
                    raise AssertionError(f"web-vitrina loading table missing {required_column}, got {loading_table}")
            if [item.get("endpoint_id") for item in upload_items] != [
                "prices_snapshot",
                "seller_funnel_snapshot",
                "web_source_snapshot",
            ]:
                raise AssertionError(f"upload summary must be sorted error -> source-aware success, got {activity_surface}")
            if [item.get("status_label") for item in upload_items] != ["Ошибка", "Успешно", "Успешно"]:
                raise AssertionError(f"upload summary status mismatch, got {activity_surface}")
            if upload_items[0].get("label_ru") != "Цены и скидки" or upload_items[0].get("reason_ru") != "данные не получены":
                raise AssertionError(f"upload summary russian label/reason mismatch, got {activity_surface}")
            first_loading_row = loading_rows[0]
            if first_loading_row.get("source_label") != "Цены и скидки":
                raise AssertionError(f"loading table source label mismatch, got {loading_table}")
            if "Цена со скидкой (₽)" not in (first_loading_row.get("metric_labels") or []):
                raise AssertionError(f"loading table must expose Russian metric labels, got {first_loading_row}")
            if first_loading_row.get("technical_endpoint") != "POST /api/v2/list/goods/filter":
                raise AssertionError(f"loading table technical endpoint mismatch, got {first_loading_row}")
            if composition_payload.get("status_badge", {}).get("tone") != "error":
                raise AssertionError(f"web-vitrina page composition must reflect semantic error tone, got {composition_payload}")

            page_status, page_html = _get_text(f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}")
            if page_status != 200:
                raise AssertionError(f"web-vitrina page route must return 200, got {page_status}")
            for expected in (
                "Web-витрина",
                "Витрина",
                "Расчет поставок",
                "Отчеты",
                'data-unified-tab-button="vitrina"',
                'data-unified-tab-button="factory-order"',
                'data-unified-tab-button="reports"',
                'data-operator-embed-frame="factory-order"',
                'data-operator-embed-frame="reports"',
                DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
                DEFAULT_SHEET_OPERATOR_UI_PATH,
                "surface=page_composition",
                "web_vitrina_page_composition",
                "data-top-panel",
                "Загрузить и обновить",
                "data-filter-controls",
                "data-history-calendar",
                "data-history-presets",
                "data-history-date-from",
                "data-history-date-to",
                "Сбросить",
                "Сохранить",
                "data-activity-log-body",
                "data-activity-log-download",
                "data-loading-table",
                "data-loading-table-head",
                "data-loading-table-body",
                "Загрузка данных",
                "Обновить группу",
                "Проверить сессию",
                "Восстановить сессию",
                "Скачать лаунчер",
                "Лог",
            ):
                if expected not in page_html:
                    raise AssertionError(f"web-vitrina page shell must expose {expected!r}")
            if "data-retry-button" in page_html:
                raise AssertionError("web-vitrina page must not render the removed refresh button")
            if 'data-unified-tab-button="update"' in page_html or ">Обновление данных</button>" in page_html or "data-update-summary" in page_html:
                raise AssertionError("web-vitrina page must not render the removed update data block")
            if page_html.index("data-loading-table") > page_html.index("data-activity-log-body"):
                raise AssertionError("loading table must be rendered before the secondary log block")

            operator_status, operator_html = _get_text(f"{base_url}{DEFAULT_SHEET_OPERATOR_UI_PATH}")
            if operator_status != 200:
                raise AssertionError(f"operator compat route must return 200, got {operator_status}")
            for expected in (
                'data-unified-tab-button="vitrina"',
                'data-unified-tab-button="factory-order"',
                'data-unified-tab-button="reports"',
            ):
                if expected not in operator_html:
                    raise AssertionError(f"operator compat route must expose unified token {expected!r}")
            if ">Обновление данных</button>" in operator_html:
                raise AssertionError("operator compat route must not expose the old update-data top tab")

            dated_status, dated_payload = _get_json(
                f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_READ_PATH}?as_of_date=2026-04-19"
            )
            if dated_status != 200 or dated_payload.get("meta", {}).get("as_of_date") != "2026-04-19":
                raise AssertionError(f"web-vitrina read route must honor as_of_date, got {dated_status} {dated_payload}")

            print("web_vitrina_read_route: ok ->", contract_payload["meta"]["snapshot_id"])
            print("web_vitrina_period_route: ok ->", period_payload["meta"]["date_columns"])
            print("web_vitrina_page_composition_surface: ok ->", composition_payload["composition_name"], composition_payload["meta"]["current_state"])
            print("web_vitrina_history_selector_surface: ok ->", historical_access["current_mode"], historical_access["supported_query_mode"], len(historical_access["options"]))
            print("web_vitrina_activity_surface: ok ->", len(upload_items), activity_surface["log_block"]["status_label"])
            print("web_vitrina_page_route: ok ->", DEFAULT_SHEET_WEB_VITRINA_UI_PATH)
            print("web_vitrina_query_override: ok ->", dated_payload["meta"]["as_of_date"])
        finally:
            server.shutdown()
            thread.join(timeout=5)


def _build_plan(
    *,
    as_of_date: str,
    first_nm_id: int,
    second_nm_id: int,
    first_group: str,
) -> SheetVitrinaV1Envelope:
    return SheetVitrinaV1Envelope(
        plan_version="delivery_contract_v1__sheet_scaffold_v1",
        snapshot_id=f"web-vitrina-http-fixture-{as_of_date}",
        as_of_date=as_of_date,
        date_columns=[as_of_date],
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(
                slot_key="historical_import",
                slot_label="Historical import",
                column_date=as_of_date,
            ),
        ],
        source_temporal_policies={},
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect="A1:C5",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=["label", "key", as_of_date],
                rows=[
                    ["Итого: Показы в воронке", "TOTAL|total_view_count", 100],
                    [f"Группа {first_group}: Показы в воронке", f"GROUP:{first_group}|view_count", 40],
                    [f"SKU A: Показы в воронке", f"SKU:{first_nm_id}|view_count", 20],
                    [f"SKU B: Заказы, шт.", f"SKU:{second_nm_id}|orderSum", 5],
                ],
                row_count=4,
                column_count=3,
            ),
            SheetVitrinaWriteTarget(
                sheet_name="STATUS",
                write_start_cell="A1",
                write_rect="A1:K7",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=STATUS_HEADER,
                rows=[
                    [
                        "seller_funnel_snapshot[yesterday_closed]",
                        "success",
                        "2026-04-19",
                        "2026-04-19",
                        "2026-04-19",
                        "",
                        "",
                        2,
                        2,
                        "",
                        "",
                    ],
                    [
                        "seller_funnel_snapshot[today_current]",
                        "success",
                        "2026-04-20",
                        "2026-04-20",
                        "2026-04-20",
                        "",
                        "",
                        2,
                        2,
                        "",
                        "",
                    ],
                    [
                        "web_source_snapshot[yesterday_closed]",
                        "success",
                        "2026-04-19",
                        "2026-04-19",
                        "",
                        "2026-04-19",
                        "2026-04-19",
                        2,
                        2,
                        "",
                        "",
                    ],
                    [
                        "web_source_snapshot[today_current]",
                        "success",
                        "2026-04-20",
                        "2026-04-20",
                        "",
                        "2026-04-20",
                        "2026-04-20",
                        2,
                        2,
                        "",
                        "resolution_rule=accepted_prior_current_runtime_cache",
                    ],
                    [
                        "prices_snapshot[yesterday_closed]",
                        "success",
                        "2026-04-19",
                        "2026-04-19",
                        "2026-04-19",
                        "",
                        "",
                        2,
                        2,
                        "",
                        "resolution_rule=accepted_closed_current_attempt",
                    ],
                    [
                        "prices_snapshot[today_current]",
                        "error",
                        "2026-04-20",
                        "2026-04-20",
                        "2026-04-20",
                        "",
                        "",
                        2,
                        0,
                        "101,202",
                        "no payload returned",
                    ],
                ],
                row_count=6,
                column_count=len(STATUS_HEADER),
            ),
        ],
    )


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get_json(url: str) -> tuple[int, dict[str, object]]:
    with urllib_request.urlopen(url) as response:
        return int(response.status), json.loads(response.read().decode("utf-8"))


def _get_text(url: str) -> tuple[int, str]:
    with urllib_request.urlopen(url) as response:
        return int(response.status), response.read().decode("utf-8")


def _start_completed_refresh_job(
    entrypoint: RegistryUploadHttpEntrypoint,
    runtime: RegistryUploadDbBackedRuntime,
) -> dict[str, object]:
    job_payload = entrypoint.operator_jobs.start(
        operation="refresh",
        runner=lambda log: _stub_refresh_run(entrypoint, runtime, log=log),
    )
    job_id = str(job_payload["job_id"])
    while True:
        snapshot = entrypoint.operator_jobs.get(job_id)
        if snapshot["status"] != "running":
            return snapshot


def _stub_refresh_run(
    entrypoint: RegistryUploadHttpEntrypoint,
    runtime: RegistryUploadDbBackedRuntime,
    *,
    log,
) -> dict[str, object]:
    plan = runtime.load_sheet_vitrina_ready_snapshot()
    current_state = runtime.load_current_state()
    refreshed_at = entrypoint.refreshed_at_factory()
    log('event=source_step_finish source=seller_funnel_snapshot temporal_slot=today_current endpoint="GET /v1/sales-funnel/daily?date=<YYYY-MM-DD>" kind=success')
    log('event=source_step_finish source=web_source_snapshot temporal_slot=today_current endpoint="GET /v1/search-analytics/snapshot?date_from=<YYYY-MM-DD>&date_to=<YYYY-MM-DD>" kind=success note="resolution_rule=accepted_prior_current_runtime_cache"')
    log('event=source_step_finish source=prices_snapshot temporal_slot=today_current endpoint="POST /api/v2/list/goods/filter" kind=error note="no payload returned"')
    result = runtime.save_sheet_vitrina_ready_snapshot(
        current_state=current_state,
        refreshed_at=refreshed_at,
        plan=plan,
    )
    payload = asdict(result)
    payload["server_context"] = entrypoint.build_sheet_server_context()
    payload["manual_context"] = entrypoint.build_sheet_manual_context()
    return payload


if __name__ == "__main__":
    main()
