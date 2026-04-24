"""Targeted smoke-check for the server-driven web-vitrina page composition."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_web_vitrina import SheetVitrinaV1WebVitrinaBlock
from packages.application.web_vitrina_gravity_table_adapter import (
    build_web_vitrina_gravity_table_adapter,
)
from packages.application.web_vitrina_page_composition import (
    build_web_vitrina_page_composition,
    build_web_vitrina_page_error_composition,
)
from packages.application.web_vitrina_view_model import build_web_vitrina_view_model
from packages.contracts.sheet_vitrina_v1 import (
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)

BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
NOW = datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)
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
    with TemporaryDirectory(prefix="sheet-vitrina-web-vitrina-page-composition-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        accepted = runtime.ingest_bundle(bundle, activated_at="2026-04-21T12:00:00Z")
        if accepted.status != "accepted":
            raise AssertionError(f"fixture bundle must be accepted, got {accepted}")

        current_state = runtime.load_current_state()
        enabled = [item for item in current_state.config_v2 if item.enabled]
        if len(enabled) < 2:
            raise AssertionError("fixture must expose at least two enabled SKU rows")
        first_group = enabled[0].group

        start_date = datetime(2026, 4, 14, tzinfo=timezone.utc).date()
        for offset in range(7):
            snapshot_date = (start_date + timedelta(days=offset)).isoformat()
            runtime.save_sheet_vitrina_ready_snapshot(
                current_state=current_state,
                refreshed_at=f"{snapshot_date}T12:05:00Z",
                plan=_build_plan(
                    as_of_date=snapshot_date,
                    first_nm_id=enabled[0].nm_id,
                    second_nm_id=enabled[1].nm_id,
                    first_group=first_group,
                ),
            )

        contract = SheetVitrinaV1WebVitrinaBlock(
            runtime=runtime,
            now_factory=lambda: NOW,
        ).build(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
        )
        view_model = build_web_vitrina_view_model(contract)
        adapter = build_web_vitrina_gravity_table_adapter(view_model)
        composition = build_web_vitrina_page_composition(
            contract=contract,
            view_model=view_model,
            adapter=adapter,
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            operator_route="/sheet-vitrina-v1/operator",
            available_snapshot_dates=runtime.list_sheet_vitrina_ready_snapshot_dates(descending=True),
            selected_as_of_date=None,
            selected_date_from=None,
            selected_date_to=None,
            activity_surface=_build_activity_surface_fixture(),
        )

        if composition["composition_name"] != "web_vitrina_page_composition" or composition["composition_version"] != "v1":
            raise AssertionError(f"page composition identity mismatch, got {composition}")
        if composition["meta"]["current_state"] != "ready":
            raise AssertionError(f"page composition state mismatch, got {composition['meta']}")
        if composition["meta"]["source_adapter_name"] != "web_vitrina_gravity_table_adapter":
            raise AssertionError(f"page composition source chain mismatch, got {composition['meta']}")
        if composition["meta"]["state_namespace"] != "wb-core:sheet-vitrina-v1:web-vitrina:page-state:v1":
            raise AssertionError(f"page composition namespace mismatch, got {composition['meta']}")
        if composition["meta"]["browser_state_persistence"] != "none":
            raise AssertionError(f"browser state persistence mismatch, got {composition['meta']}")
        historical_access = composition["historical_access"]
        if historical_access["current_mode"] != "default":
            raise AssertionError(f"historical mode mismatch, got {historical_access}")
        if historical_access["default_as_of_date"] != "2026-04-20":
            raise AssertionError(f"default as_of_date mismatch, got {historical_access}")
        if historical_access["supported_query_mode"] != "date_window":
            raise AssertionError(f"historical query mode mismatch, got {historical_access}")
        if [item["value"] for item in historical_access["options"]] != [
            "2026-04-20",
            "2026-04-19",
            "2026-04-18",
            "2026-04-17",
            "2026-04-16",
            "2026-04-15",
            "2026-04-14",
        ]:
            raise AssertionError(f"historical options mismatch, got {historical_access}")
        if [item["preset_id"] for item in historical_access["preset_options"]] != [
            "week",
            "two_weeks",
            "month",
            "quarter",
            "year",
        ]:
            raise AssertionError(f"historical preset options mismatch, got {historical_access}")

        controls = {item["control_id"]: item for item in composition["filter_surface"]["controls"]}
        for required in ("search", "section", "group", "scope_kind", "metric"):
            if required not in controls:
                raise AssertionError(f"missing filter control {required!r}: {controls}")
        if composition["filter_surface"]["default_sort_value"] != "row_order::asc":
            raise AssertionError(f"default sort mismatch, got {composition['filter_surface']}")
        if not composition["table_surface"]["columns"] or not composition["table_surface"]["rows"]:
            raise AssertionError(f"table surface is empty, got {composition['table_surface']}")
        table_headers = [item["header"] for item in composition["table_surface"]["columns"]]
        for expected_header in ("Раздел", "Метрика", "Обновлено"):
            if expected_header not in table_headers:
                raise AssertionError(f"main table must expose Russian header {expected_header!r}, got {table_headers}")
        for forbidden_header in ("Metric Label", "Sections", "Score Label"):
            if forbidden_header in table_headers:
                raise AssertionError(f"main table must not expose English header {forbidden_header!r}, got {table_headers}")
        if not all("row_last_updated_at" in row["values"] for row in composition["table_surface"]["rows"]):
            raise AssertionError(f"main table rows must expose row update timestamps, got {composition['table_surface']['rows']}")
        if composition["status_badge"]["tone"] != "success":
            raise AssertionError(f"status badge mismatch, got {composition['status_badge']}")
        if composition["status_badge"]["label"] != "Успешно":
            raise AssertionError(f"status badge label mismatch, got {composition['status_badge']}")
        summary_cards = {item["card_id"]: item for item in composition["summary_cards"]}
        if summary_cards["status"]["value"] != "Успешно":
            raise AssertionError(f"status summary card mismatch, got {summary_cards}")
        if summary_cards["freshness"]["label"] != "Свежесть данных":
            raise AssertionError(f"freshness summary card label mismatch, got {summary_cards}")
        if summary_cards["freshness"]["value_kind"] != "timestamp":
            raise AssertionError(f"freshness summary card must expose timestamp formatting kind, got {summary_cards['freshness']}")
        if summary_cards["freshness"]["updated_at"] != "2026-04-20T12:05:00Z":
            raise AssertionError(f"freshness summary card updated_at mismatch, got {summary_cards['freshness']}")
        if "snapshot " not in summary_cards["freshness"]["detail"] or "as_of_date 2026-04-20" not in summary_cards["freshness"]["detail"]:
            raise AssertionError(f"freshness summary card detail mismatch, got {summary_cards['freshness']}")
        if "snapshot" in summary_cards:
            raise AssertionError(f"snapshot summary card must be folded into freshness, got {summary_cards}")
        activity_surface = composition["activity_surface"]
        if activity_surface["log_block"]["title"] != "Лог" or not activity_surface["log_block"]["download_path"]:
            raise AssertionError(f"activity log block mismatch, got {activity_surface['log_block']}")
        if "update_summary" in activity_surface:
            raise AssertionError(f"page composition must not expose the removed update block, got {activity_surface}")
        loading_table = activity_surface["loading_table"]
        upload_items = activity_surface["upload_summary"]["items"]
        loading_rows = loading_table["rows"]
        loading_columns = {item["id"]: item for item in loading_table["columns"]}
        loading_groups = {item["group_id"]: item for item in loading_table["groups"]}
        if [row["source_key"] for row in loading_rows] != [item["source_key"] for item in upload_items]:
            raise AssertionError(f"loading table rows must follow the upload source truth, got {activity_surface}")
        if sorted(loading_groups) != ["other_sources", "seller_portal_bot", "wb_api"]:
            raise AssertionError(f"loading table must expose stable source groups, got {loading_groups}")
        if not loading_groups["seller_portal_bot"]["session_controls"]:
            raise AssertionError(f"Seller Portal group must expose session controls, got {loading_groups}")
        if {row["source_group_id"] for row in loading_rows} != {"wb_api", "seller_portal_bot"}:
            raise AssertionError(f"loading table rows must carry source group ids, got {loading_rows}")
        if loading_table["today_date"] != "2026-04-21" or loading_table["yesterday_date"] != "2026-04-20":
            raise AssertionError(f"loading table dates must be server/business dates, got {loading_table}")
        if loading_table["default_refresh_date"] != "2026-04-20":
            raise AssertionError(f"group refresh default must stay within available dates, got {loading_table}")
        if "2026-04-21" in loading_table["available_dates"]:
            raise AssertionError(f"group refresh available dates must not invent unsupported today, got {loading_table}")
        for group in loading_groups.values():
            action = group["refresh_action"]
            if action["default_as_of_date"] != "2026-04-20":
                raise AssertionError(f"group action default must use latest available date, got {group}")
        if loading_columns["today_status"]["label"] != "Сегодня: 2026-04-21":
            raise AssertionError(f"today loading column mismatch, got {loading_columns}")
        if loading_columns["yesterday_status"]["label"] != "Вчера: 2026-04-20":
            raise AssertionError(f"yesterday loading column mismatch, got {loading_columns}")
        for required_column in ("today_reason", "yesterday_reason", "metrics", "technical_endpoint"):
            if required_column not in loading_columns:
                raise AssertionError(f"loading table missing column {required_column!r}: {loading_columns}")
        first_loading_row = loading_rows[0]
        if first_loading_row["source_label"] != "Цены и скидки":
            raise AssertionError(f"loading table source labels must stay Russian, got {loading_table}")
        if first_loading_row["today"]["label"] != "не OK" or first_loading_row["today_reason"] != "данные не получены":
            raise AssertionError(f"loading table today status/reason mismatch, got {loading_table}")
        if first_loading_row["yesterday"]["label"] != "OK" or first_loading_row["yesterday_reason"] != "Готово":
            raise AssertionError(f"loading table yesterday status/reason mismatch, got {loading_table}")
        if "Цена со скидкой (₽)" not in first_loading_row["metric_labels"]:
            raise AssertionError(f"loading table must expose Russian metric labels, got {first_loading_row}")
        if first_loading_row["technical_endpoint"] != "POST /api/v2/list/goods/filter":
            raise AssertionError(f"loading table technical endpoint mismatch, got {first_loading_row}")
        if [item["status_label"] for item in upload_items] != ["Ошибка", "Внимание", "Успешно"]:
            raise AssertionError(f"upload activity summary statuses mismatch, got {activity_surface}")
        if [item["label_ru"] for item in upload_items] != ["Цены и скидки", "Поисковая аналитика", "Воронка продавца"]:
            raise AssertionError(f"upload activity labels mismatch, got {activity_surface}")
        if "Причина" not in upload_items[0]["detail"] or upload_items[0]["reason_ru"] != "данные не получены":
            raise AssertionError(f"upload activity reason mismatch, got {activity_surface}")
        if "web_source_snapshot" not in upload_items[1]["technical_text"]:
            raise AssertionError(f"upload activity technical text mismatch, got {activity_surface}")
        ordered_row_ids = [row["row_id"] for row in composition["table_surface"]["rows"]]
        expected_row_ids = [
            "TOTAL|total_view_count",
            "TOTAL|total_orderSum",
            f"SKU:{enabled[0].nm_id}|avg_price_seller_discounted",
            f"SKU:{enabled[0].nm_id}|avg_addToCartConversion",
            f"SKU:{enabled[1].nm_id}|avg_price_seller_discounted",
            f"SKU:{enabled[1].nm_id}|avg_addToCartConversion",
        ]
        if ordered_row_ids != expected_row_ids:
            raise AssertionError(f"page composition row ordering mismatch, got {ordered_row_ids}")
        grouping_ids = [item["grouping_id"] for item in composition["table_surface"]["groupings"]]
        if grouping_ids != ["group:overview", f"group:{first_group}"]:
            raise AssertionError(f"page composition grouping order mismatch, got {grouping_ids}")

        period_contract = SheetVitrinaV1WebVitrinaBlock(
            runtime=runtime,
            now_factory=lambda: NOW,
        ).build(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            date_from="2026-04-18",
            date_to="2026-04-20",
        )
        period_view_model = build_web_vitrina_view_model(period_contract)
        period_adapter = build_web_vitrina_gravity_table_adapter(period_view_model)
        period_composition = build_web_vitrina_page_composition(
            contract=period_contract,
            view_model=period_view_model,
            adapter=period_adapter,
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            operator_route="/sheet-vitrina-v1/operator",
            available_snapshot_dates=runtime.list_sheet_vitrina_ready_snapshot_dates(descending=True),
            selected_as_of_date=None,
            selected_date_from="2026-04-18",
            selected_date_to="2026-04-20",
            activity_surface=_build_activity_surface_fixture(),
        )
        if period_composition["historical_access"]["current_mode"] != "historical_period":
            raise AssertionError(f"period composition mode mismatch, got {period_composition['historical_access']}")
        if period_composition["historical_access"]["selected_date_from"] != "2026-04-18":
            raise AssertionError(f"period composition selected_date_from mismatch, got {period_composition['historical_access']}")
        if period_composition["historical_access"]["selected_date_to"] != "2026-04-20":
            raise AssertionError(f"period composition selected_date_to mismatch, got {period_composition['historical_access']}")
        if period_composition["table_surface"]["date_column_ids"] != ["date:2026-04-18", "date:2026-04-19", "date:2026-04-20"]:
            raise AssertionError(f"period composition date columns mismatch, got {period_composition['table_surface']}")

        error_payload = build_web_vitrina_page_error_composition(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            operator_route="/sheet-vitrina-v1/operator",
            as_of_date="2026-04-21",
            error_message="sheet_vitrina_v1 ready snapshot missing: fixture",
            available_snapshot_dates=runtime.list_sheet_vitrina_ready_snapshot_dates(descending=True),
            default_as_of_date="2026-04-20",
            selected_as_of_date="2026-04-21",
            selected_date_from=None,
            selected_date_to=None,
        )
        if error_payload["meta"]["current_state"] != "error":
            raise AssertionError(f"error composition state mismatch, got {error_payload['meta']}")
        if error_payload["table_surface"]["state_surface"]["current_state"] != "error":
            raise AssertionError(f"error table state mismatch, got {error_payload['table_surface']}")
        if error_payload["historical_access"]["current_mode"] != "historical_day":
            raise AssertionError(f"error composition historical mode mismatch, got {error_payload['historical_access']}")
        if error_payload["historical_access"]["selected_as_of_date"] != "2026-04-21":
            raise AssertionError(f"error composition historical selection mismatch, got {error_payload['historical_access']}")
        if error_payload["status_badge"]["label"] != "Ошибка":
            raise AssertionError(f"error composition status label mismatch, got {error_payload['status_badge']}")
        error_summary_cards = {item["card_id"]: item for item in error_payload["summary_cards"]}
        if error_summary_cards["freshness"]["label"] != "Свежесть данных":
            raise AssertionError(f"error freshness card label mismatch, got {error_summary_cards}")
        if "snapshot unavailable" not in error_summary_cards["freshness"]["detail"]:
            raise AssertionError(f"error freshness card detail mismatch, got {error_summary_cards}")
        if error_payload["activity_surface"]["log_block"]["title"] != "Лог":
            raise AssertionError(f"error composition activity surface mismatch, got {error_payload['activity_surface']}")

        print("web_vitrina_page_composition_identity: ok ->", composition["composition_name"], composition["composition_version"])
        print("web_vitrina_page_composition_state: ok ->", composition["meta"]["current_state"], composition["status_badge"]["tone"])
        print("web_vitrina_page_composition_history: ok ->", historical_access["current_mode"], historical_access["supported_query_mode"], len(historical_access["options"]))
        print("web_vitrina_page_composition_period: ok ->", period_composition["historical_access"]["selected_date_from"], period_composition["historical_access"]["selected_date_to"])
        print("web_vitrina_page_composition_activity_surface: ok ->", len(upload_items), len(loading_rows))
        print("web_vitrina_page_composition_filters: ok ->", ",".join(sorted(controls)))
        print("web_vitrina_page_composition_table: ok ->", len(composition["table_surface"]["columns"]), len(composition["table_surface"]["rows"]))
        print("web_vitrina_page_composition_error: ok ->", error_payload["meta"]["current_state"])


def _build_plan(
    *,
    as_of_date: str,
    first_nm_id: int,
    second_nm_id: int,
    first_group: str,
    ) -> SheetVitrinaV1Envelope:
    return SheetVitrinaV1Envelope(
        plan_version="delivery_contract_v1__sheet_scaffold_v1",
        snapshot_id=f"web-vitrina-page-composition-fixture-{as_of_date}",
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
                write_rect="A1:C7",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=["label", "key", as_of_date],
                rows=[
                    ["Итого: Показы в воронке", "TOTAL|total_view_count", 100],
                    ["Итого: Сумма заказов", "TOTAL|total_orderSum", 1000],
                    [f"SKU A: Цена продавца", f"SKU:{first_nm_id}|avg_price_seller_discounted", 990],
                    [f"SKU B: Цена продавца", f"SKU:{second_nm_id}|avg_price_seller_discounted", 1090],
                    [f"SKU A: Конверсия в корзину", f"SKU:{first_nm_id}|avg_addToCartConversion", 0.115],
                    [f"SKU B: Конверсия в корзину", f"SKU:{second_nm_id}|avg_addToCartConversion", 0.105],
                ],
                row_count=6,
                column_count=3,
            ),
            SheetVitrinaWriteTarget(
                sheet_name="STATUS",
                write_start_cell="A1",
                write_rect="A1:K5",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=STATUS_HEADER,
                rows=[
                    [
                        "seller_funnel_snapshot[yesterday_closed]",
                        "success",
                        as_of_date,
                        as_of_date,
                        as_of_date,
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
                        as_of_date,
                        as_of_date,
                        as_of_date,
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
                        as_of_date,
                        as_of_date,
                        "",
                        as_of_date,
                        as_of_date,
                        2,
                        2,
                        "",
                        "",
                    ],
                    [
                        "web_source_snapshot[today_current]",
                        "success",
                        as_of_date,
                        as_of_date,
                        "",
                        as_of_date,
                        as_of_date,
                        2,
                        2,
                        "",
                        "",
                    ],
                ],
                row_count=4,
                column_count=len(STATUS_HEADER),
            ),
        ],
        )


def _build_activity_surface_fixture() -> dict[str, object]:
    return {
        "log_block": {
            "title": "Лог",
            "subtitle": "Последний релевантный refresh-run",
            "status_label": "Успешно",
            "tone": "success",
            "detail": "job fixture-refresh-job-1 · refresh · 2026-04-20T12:05:00Z",
            "preview_lines": [
                "2026-04-20T12:04:00Z event=source_step_finish source=seller_funnel_snapshot kind=success",
                "2026-04-20T12:04:01Z event=source_step_finish source=prices_snapshot kind=error",
            ],
            "line_count": 2,
            "download_path": "/v1/sheet-vitrina-v1/job?job_id=fixture-refresh-job-1&format=text&download=1",
            "log_filename": "sheet-vitrina-v1-refresh-fixture-refresh-job-1.txt",
            "empty_message": "",
        },
        "upload_summary": {
            "title": "Загрузка данных",
            "subtitle": "Последний завершённый refresh-run.",
            "detail": "job fixture-refresh-job-1 · refresh",
            "updated_at": "2026-04-20T12:05:00Z",
            "items": [
                {
                    "endpoint_id": "prices_snapshot",
                    "endpoint_label": "POST /api/v2/list/goods/filter",
                    "source_key": "prices_snapshot",
                    "label_ru": "Цены и скидки",
                    "description_ru": "Текущие цены продавца и скидки по SKU.",
                    "reason_ru": "данные не получены",
                    "technical_key": "prices_snapshot",
                    "technical_text": "prices_snapshot · POST /api/v2/list/goods/filter",
                    "status_label": "Ошибка",
                    "tone": "error",
                    "detail": "Текущие цены продавца и скидки по SKU. Причина: данные не получены",
                },
                {
                    "endpoint_id": "web_source_snapshot",
                    "endpoint_label": "GET /v1/search-analytics/snapshot?date_from=<YYYY-MM-DD>&date_to=<YYYY-MM-DD>",
                    "source_key": "web_source_snapshot",
                    "label_ru": "Поисковая аналитика",
                    "description_ru": "Просмотры, CTR, заказы и средняя позиция в поиске.",
                    "reason_ru": "использована подтверждённая версия из runtime cache",
                    "technical_key": "web_source_snapshot",
                    "technical_text": "web_source_snapshot · GET /v1/search-analytics/snapshot?date_from=<YYYY-MM-DD>&date_to=<YYYY-MM-DD>",
                    "status_label": "Внимание",
                    "tone": "warning",
                    "detail": "Просмотры, CTR, заказы и средняя позиция в поиске. Причина: использована подтверждённая версия из runtime cache",
                },
                {
                    "endpoint_id": "seller_funnel_snapshot",
                    "endpoint_label": "GET /v1/sales-funnel/daily?date=<YYYY-MM-DD>",
                    "source_key": "seller_funnel_snapshot",
                    "label_ru": "Воронка продавца",
                    "description_ru": "Показы карточки, открытия и базовая конверсия за дату.",
                    "reason_ru": "",
                    "technical_key": "seller_funnel_snapshot",
                    "technical_text": "seller_funnel_snapshot · GET /v1/sales-funnel/daily?date=<YYYY-MM-DD>",
                    "status_label": "Успешно",
                    "tone": "success",
                    "detail": "Показы карточки, открытия и базовая конверсия за дату.",
                },
            ],
            "empty_message": "",
        },
        "loading_table": {
            "title": "Загрузка данных",
            "subtitle": "Последний завершённый refresh-run.",
            "detail": "job fixture-refresh-job-1 · refresh",
            "updated_at": "2026-04-20T12:05:00Z",
            "today_date": "2026-04-21",
            "yesterday_date": "2026-04-20",
            "available_dates": ["2026-04-18", "2026-04-20"],
            "default_refresh_date": "2026-04-21",
            "columns": [
                {"id": "source", "label": "Источник"},
                {"id": "today_status", "label": "Сегодня: 2026-04-21"},
                {"id": "today_reason", "label": "Причина сегодня"},
                {"id": "yesterday_status", "label": "Вчера: 2026-04-20"},
                {"id": "yesterday_reason", "label": "Причина вчера"},
                {"id": "metrics", "label": "Метрики"},
                {"id": "technical_endpoint", "label": "Технический endpoint"},
            ],
            "groups": [
                {
                    "group_id": "wb_api",
                    "label": "WB API",
                    "source_keys": ["prices_snapshot"],
                    "last_updated_at": "2026-04-20T12:05:00Z",
                    "refresh_action": {"label": "Обновить группу", "source_group_id": "wb_api"},
                    "session_controls": False,
                },
                {
                    "group_id": "seller_portal_bot",
                    "label": "Seller Portal / бот",
                    "source_keys": ["web_source_snapshot", "seller_funnel_snapshot"],
                    "last_updated_at": "2026-04-20T12:05:00Z",
                    "refresh_action": {"label": "Обновить группу", "source_group_id": "seller_portal_bot"},
                    "session_controls": True,
                },
                {
                    "group_id": "other_sources",
                    "label": "Прочие источники",
                    "source_keys": [],
                    "last_updated_at": "",
                    "refresh_action": {"label": "Обновить группу", "source_group_id": "other_sources"},
                    "session_controls": False,
                },
            ],
            "rows": [
                {
                    "source_key": "prices_snapshot",
                    "source_group_id": "wb_api",
                    "source_label": "Цены и скидки",
                    "today": {
                        "date": "2026-04-21",
                        "ok": False,
                        "label": "не OK",
                        "tone": "error",
                        "reason": "данные не получены",
                    },
                    "today_reason": "данные не получены",
                    "yesterday": {
                        "date": "2026-04-20",
                        "ok": True,
                        "label": "OK",
                        "tone": "success",
                        "reason": "Готово",
                    },
                    "yesterday_reason": "Готово",
                    "metric_labels": ["Цена со скидкой (₽)", "Цена продавца (₽)"],
                    "technical_endpoint": "POST /api/v2/list/goods/filter",
                },
                {
                    "source_key": "web_source_snapshot",
                    "source_group_id": "seller_portal_bot",
                    "source_label": "Поисковая аналитика",
                    "today": {
                        "date": "2026-04-21",
                        "ok": False,
                        "label": "не OK",
                        "tone": "error",
                        "reason": "использована подтверждённая версия из runtime cache",
                    },
                    "today_reason": "использована подтверждённая версия из runtime cache",
                    "yesterday": {
                        "date": "2026-04-20",
                        "ok": False,
                        "label": "не OK",
                        "tone": "error",
                        "reason": "использована подтверждённая версия из runtime cache",
                    },
                    "yesterday_reason": "использована подтверждённая версия из runtime cache",
                    "metric_labels": ["Показы в поиске", "CTR в поиске"],
                    "technical_endpoint": "GET /v1/search-analytics/snapshot?date_from=<YYYY-MM-DD>&date_to=<YYYY-MM-DD>",
                },
                {
                    "source_key": "seller_funnel_snapshot",
                    "source_group_id": "seller_portal_bot",
                    "source_label": "Воронка продавца",
                    "today": {
                        "date": "2026-04-21",
                        "ok": True,
                        "label": "OK",
                        "tone": "success",
                        "reason": "Готово",
                    },
                    "today_reason": "Готово",
                    "yesterday": {
                        "date": "2026-04-20",
                        "ok": True,
                        "label": "OK",
                        "tone": "success",
                        "reason": "Готово",
                    },
                    "yesterday_reason": "Готово",
                    "metric_labels": ["Показы в воронке", "Открытия карточки"],
                    "technical_endpoint": "GET /v1/sales-funnel/daily?date=<YYYY-MM-DD>",
                },
            ],
            "empty_message": "",
        },
    }


if __name__ == "__main__":
    main()
