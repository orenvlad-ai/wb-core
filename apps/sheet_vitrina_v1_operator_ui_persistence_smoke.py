"""Browser smoke-check for operator UI persistence across reloads."""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import sys
import threading
import time
from urllib import parse as urllib_parse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_FACTORY_ORDER_CALCULATE_PATH,
    DEFAULT_FACTORY_ORDER_STATUS_PATH,
    DEFAULT_FACTORY_ORDER_STOCK_FF_ONEC_CHECK_PATH,
    DEFAULT_SELLER_PORTAL_SESSION_CHECK_PATH,
    DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH,
    DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH,
    DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH,
    DEFAULT_SELLER_PORTAL_RECOVERY_STOP_PATH,
    DEFAULT_SHEET_DAILY_REPORT_PATH,
    DEFAULT_SHEET_JOB_PATH,
    DEFAULT_SHEET_LOAD_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_REPORT_PATH,
    DEFAULT_SHEET_REFRESH_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_STOCK_REPORT_PATH,
    DEFAULT_SUPPLY_CALCULATIONS_PATH,
    DEFAULT_WB_REGIONAL_CALCULATE_PATH,
    DEFAULT_WB_REGIONAL_PLANNING_OPTIONS_PATH,
    DEFAULT_WB_REGIONAL_STATUS_PATH,
    DEFAULT_WB_SUPPLIES_OVERLAY_OPTIONS_PATH,
    DEFAULT_WB_SUPPLIES_SYNC_PATH,
    _render_sheet_vitrina_operator_ui,
)

STORAGE_KEY = "wb-core:sheet-vitrina-v1:operator-ui-state:v1"
ACTIVE_SKUS = [
    {"nm_id": 1001, "display_name": "SKU Alpha", "identity_label": "SKU Alpha · nmId 1001"},
    {"nm_id": 1002, "display_name": "SKU Beta", "identity_label": "SKU Beta · nmId 1002"},
    {"nm_id": 1003, "display_name": "SKU Gamma", "identity_label": "SKU Gamma · nmId 1003"},
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-check operator UI persistence.")
    parser.add_argument("--base-url", default="", help="Existing operator base URL, for example http://89.191.226.88")
    parser.add_argument(
        "--ignore-https-errors",
        action="store_true",
        help="Ignore TLS validation errors in the browser context.",
    )
    parser.add_argument(
        "--allow-live-mutations",
        action="store_true",
        help=(
            "Allow this non-read-only smoke against a non-loopback --base-url. "
            "Use only for an isolated runtime; the scenario clicks refresh/calculate flows."
        ),
    )
    args = parser.parse_args()

    if args.base_url:
        base_url = args.base_url.rstrip("/")
        if not args.allow_live_mutations and not _is_loopback_base_url(base_url):
            raise SystemExit(
                "Refusing to run operator UI persistence smoke against a non-loopback --base-url "
                "without --allow-live-mutations. This smoke clicks refresh/calculate flows and "
                "can overwrite live operator result state."
            )
        result = run_browser_checks(base_url, ignore_https_errors=args.ignore_https_errors)
        _print_summary(result)
        return

    with LocalOperatorFixtureServer() as base_url:
        result = run_browser_checks(base_url, ignore_https_errors=False)
    _print_summary(result)


class LocalOperatorFixtureServer:
    def __init__(self) -> None:
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.base_url = ""

    def __enter__(self) -> str:
        port = _reserve_free_port()
        self.base_url = f"http://127.0.0.1:{port}"
        html = _render_sheet_vitrina_operator_ui(
            refresh_path=DEFAULT_SHEET_REFRESH_PATH,
            load_path=DEFAULT_SHEET_LOAD_PATH,
            status_path=DEFAULT_SHEET_STATUS_PATH,
            job_path=DEFAULT_SHEET_JOB_PATH,
            daily_report_path=DEFAULT_SHEET_DAILY_REPORT_PATH,
            stock_report_path=DEFAULT_SHEET_STOCK_REPORT_PATH,
            plan_report_path=DEFAULT_SHEET_PLAN_REPORT_PATH,
            operator_context={
                "current_business_date": "2026-04-20",
                "stock_report_active_skus": ACTIVE_SKUS,
                "stock_report_active_sku_count": len(ACTIVE_SKUS),
                "stock_report_active_sku_source": "current_registry_config_v2",
            },
        )
        payloads = {
            DEFAULT_SHEET_OPERATOR_UI_PATH: ("text/html; charset=utf-8", html.encode("utf-8"), HTTPStatus.OK),
            DEFAULT_SHEET_STATUS_PATH: (
                "application/json; charset=utf-8",
                json.dumps(
                    {
                        "error": "sheet_vitrina_v1 ready snapshot missing: fixture",
                        "server_context": {},
                        "manual_context": {},
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                HTTPStatus.UNPROCESSABLE_ENTITY,
            ),
            DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH: (
                "application/json; charset=utf-8",
                json.dumps(
                    {
                        "status": "idle",
                        "status_label": "Не запущено",
                        "status_tone": "idle",
                        "run_status": "idle",
                        "run_status_label": "Не запущено",
                        "run_status_tone": "idle",
                        "summary": "Новый запуск восстановления сейчас не выполняется. Сохранённая seller-сессия больше не действует.",
                        "instruction": "Нажмите «Восстановить сессию» и войдите через launcher для Mac.",
                        "technical_line": "Нужный кабинет: ИП Сагитов В. Р. · supplier canonical-supplier-id",
                        "running": False,
                        "can_start": True,
                        "can_stop": False,
                        "launcher_enabled": False,
                        "launcher_download_path": DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH,
                        "run_id": "",
                        "run_is_final": False,
                        "run_final_status": "",
                        "run_final_label": "",
                        "session_status": "session_invalid",
                        "session_status_label": "Нужен вход",
                        "session_status_tone": "error",
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                HTTPStatus.OK,
            ),
            DEFAULT_SELLER_PORTAL_SESSION_CHECK_PATH: (
                "application/json; charset=utf-8",
                json.dumps(
                    {
                        "status": "session_valid_wrong_org",
                        "status_label": "Не тот кабинет",
                        "status_tone": "warning",
                        "summary": "Сессия активна, но открыт не тот кабинет.",
                        "instruction": "Нажмите «Восстановить сессию»: система откроет временное окно входа и переключит кабинет на нужный supplier.",
                        "technical_line": "Нужный кабинет: ИП Сагитов В. Р. · supplier canonical-supplier-id",
                        "running": False,
                        "can_start": True,
                        "can_stop": False,
                        "launcher_enabled": False,
                        "launcher_download_path": DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH,
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                HTTPStatus.OK,
            ),
            DEFAULT_SHEET_DAILY_REPORT_PATH: (
                "application/json; charset=utf-8",
                json.dumps(
                    {
                        "status": "unavailable",
                        "reason": "daily fixture not materialized",
                        "notes": [],
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                HTTPStatus.OK,
            ),
            DEFAULT_SHEET_STOCK_REPORT_PATH: (
                "application/json; charset=utf-8",
                json.dumps(
                    {
                        "status": "available",
                        "business_timezone": "Asia/Yekaterinburg",
                        "current_business_date": "2026-04-20",
                        "report_date": "2026-04-19",
                        "threshold_lt": 50,
                        "notes": [],
                        "districts": [
                            {"metric_key": "stock_ru_central", "label": "Центральный ФО"},
                        ],
                        "source_of_truth": {
                            "read_model": "persisted_ready_snapshot",
                            "sheet_name": "DATA_VITRINA",
                            "snapshot_as_of_date": "2026-04-19",
                            "temporal_slot": "yesterday_closed",
                            "slot_date": "2026-04-19",
                            "wb_supplies_source": "sheet_vitrina_v1_wb_supplies runtime cache",
                            "supplier_shipments_source": "sheet_vitrina_v1_supplier_shipments runtime registry",
                            "stock_ff_source": "ff_stock_ledger current balances",
                        },
                        "row_count": 3,
                        "rows": [
                            {
                                "nm_id": 1001,
                                "display_name": "SKU Alpha",
                                "identity_label": "SKU Alpha · nmId 1001",
                                "active_order": 0,
                                "promotion_participation": True,
                                "promotion_participation_label": "Да",
                                "supplier_production_qty": 5.0,
                                "supplier_in_transit_qty": 2.0,
                                "wb_supplies_inbound_qty": 9.0,
                                "stock_ff": 31.0,
                                "stock_wb": 21.0,
                                "stock_total": 21.0,
                                "zero_district_count": 0,
                                "avg_sales_per_day": 3.0,
                                "days_left_total": 7.0,
                                "districts": [{"metric_key": "stock_ru_central", "label": "Центральный ФО", "stock": 21.0, "days_left": 7.0}],
                            },
                            {
                                "nm_id": 1002,
                                "display_name": "SKU Beta",
                                "identity_label": "SKU Beta · nmId 1002",
                                "active_order": 1,
                                "promotion_participation": False,
                                "promotion_participation_label": "Нет",
                                "supplier_production_qty": 0.0,
                                "supplier_in_transit_qty": 3.0,
                                "wb_supplies_inbound_qty": 0.0,
                                "stock_ff": 0.0,
                                "stock_wb": 13.0,
                                "stock_total": 13.0,
                                "zero_district_count": 0,
                                "avg_sales_per_day": 1.0,
                                "days_left_total": 13.0,
                                "districts": [{"metric_key": "stock_ru_central", "label": "Центральный ФО", "stock": 13.0, "days_left": 13.0}],
                            },
                            {
                                "nm_id": 1003,
                                "display_name": "SKU Gamma",
                                "identity_label": "SKU Gamma · nmId 1003",
                                "active_order": 2,
                                "promotion_participation": None,
                                "promotion_participation_label": "н/д",
                                "supplier_production_qty": 4.0,
                                "supplier_in_transit_qty": 0.0,
                                "wb_supplies_inbound_qty": 4.0,
                                "stock_ff": -2.0,
                                "stock_wb": 7.0,
                                "stock_total": 7.0,
                                "zero_district_count": 0,
                                "avg_sales_per_day": 1.0,
                                "days_left_total": 7.0,
                                "districts": [{"metric_key": "stock_ru_central", "label": "Центральный ФО", "stock": 7.0, "days_left": 7.0}],
                            },
                        ],
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                HTTPStatus.OK,
            ),
            DEFAULT_FACTORY_ORDER_STATUS_PATH: (
                "application/json; charset=utf-8",
                json.dumps(_factory_status_payload(refreshed=False), ensure_ascii=False).encode("utf-8"),
                HTTPStatus.OK,
            ),
            DEFAULT_FACTORY_ORDER_STOCK_FF_ONEC_CHECK_PATH: (
                "application/json; charset=utf-8",
                json.dumps(
                    {
                        "status": "ready",
                        "source": "onec_ff_stock",
                        "source_label_ru": "1С / Фулфилмент",
                        "snapshot_date": "2026-04-20",
                        "active_sku_count": len(ACTIVE_SKUS),
                        "covered_sku_count": len(ACTIVE_SKUS),
                        "positive_stock_sku_count": 2,
                        "zero_stock_sku_count": 1,
                        "missing_sku_count": 0,
                        "total_stock_ff": 125.0,
                        "warnings": [],
                        "errors": [],
                        "sample_rows": [],
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                HTTPStatus.OK,
            ),
            DEFAULT_WB_REGIONAL_STATUS_PATH: (
                "application/json; charset=utf-8",
                json.dumps(
                    {
                        "active_sku_count": len(ACTIVE_SKUS),
                        "methodology_note": "-",
                        "shared_datasets": {},
                        "last_result": None,
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                HTTPStatus.OK,
            ),
            DEFAULT_WB_SUPPLIES_OVERLAY_OPTIONS_PATH: (
                "application/json; charset=utf-8",
                json.dumps(
                    _wb_supply_overlay_options_payload(),
                    ensure_ascii=False,
                ).encode("utf-8"),
                HTTPStatus.OK,
            ),
            DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH: (
                "application/json; charset=utf-8",
                json.dumps(
                    {
                        "status": "awaiting_login",
                        "status_label": "Нужно войти",
                        "status_tone": "warning",
                        "run_status": "awaiting_login",
                        "run_status_label": "Нужно войти",
                        "run_status_tone": "warning",
                        "summary": "Откройте launcher и войдите в seller portal.",
                        "instruction": "После входа система сама проверит кабинет, сохранит storage_state.json и закроет временное окно входа.",
                        "technical_line": "Нужный кабинет: ИП Сагитов В. Р. · supplier canonical-supplier-id",
                        "running": True,
                        "can_start": False,
                        "can_stop": True,
                        "launcher_enabled": True,
                        "launcher_download_path": DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH,
                        "run_id": "seller-recovery-run-1",
                        "run_is_final": False,
                        "run_final_status": "",
                        "run_final_label": "",
                        "session_status": "session_invalid",
                        "session_status_label": "Нужен вход",
                        "session_status_tone": "error",
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                HTTPStatus.OK,
            ),
            DEFAULT_SELLER_PORTAL_RECOVERY_STOP_PATH: (
                "application/json; charset=utf-8",
                json.dumps(
                    {
                        "status": "stopped",
                        "status_label": "Остановлено",
                        "status_tone": "idle",
                        "run_status": "stopped",
                        "run_status_label": "Остановлено",
                        "run_status_tone": "idle",
                        "summary": "Восстановление остановлено: временное окно входа закрыто. Сохранённая seller-сессия и бот не изменены.",
                        "instruction": "Кнопка «Остановить восстановление» закрывает только временное окно входа.",
                        "technical_line": "Нужный кабинет: ИП Сагитов В. Р. · supplier canonical-supplier-id",
                        "running": False,
                        "can_start": True,
                        "can_stop": False,
                        "launcher_enabled": False,
                        "launcher_download_path": DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH,
                        "run_id": "seller-recovery-run-1",
                        "run_is_final": True,
                        "run_final_status": "stopped",
                        "run_final_label": "Восстановление остановлено",
                        "session_status": "session_invalid",
                        "session_status_label": "Нужен вход",
                        "session_status_tone": "error",
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                HTTPStatus.OK,
            ),
            DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH: (
                "application/zip",
                b"PK\x05\x06" + (b"\x00" * 18),
                HTTPStatus.OK,
            ),
        }

        handler_cls = _build_handler(
            payloads,
            factory_status_refresh_payload=_factory_status_payload(refreshed=True),
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self.base_url

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)


def _build_handler(
    payloads: dict[str, tuple[str, bytes, HTTPStatus]],
    *,
    factory_status_refresh_payload: dict[str, object] | None = None,
):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed_path = urllib_parse.urlparse(self.path).path
            if (
                parsed_path == DEFAULT_FACTORY_ORDER_STATUS_PATH
                and self.headers.get("X-WB-Core-Refresh") == "supplier-registry-inbound"
                and factory_status_refresh_payload is not None
            ):
                body = json.dumps(factory_status_refresh_payload, ensure_ascii=False).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            payload = payloads.get(parsed_path)
            if payload is None:
                self.send_response(HTTPStatus.NOT_FOUND)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": f"unsupported path: {self.path}"}).encode("utf-8"))
                return
            content_type, body, status = payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

    return Handler


def _factory_status_payload(*, refreshed: bool) -> dict[str, object]:
    supplier_summary = (
        {
            "source": "supplier_registry",
            "status": "empty",
            "acceptance_days": 30,
            "shipment_summary": [],
            "diagnostics": {
                "shipment_count": 1,
                "product_line_count": 0,
                "matched_line_count": 0,
                "unmatched_line_count": 0,
                "ambiguous_line_count": 0,
                "missing_shipment_date_line_count": 0,
                "invalid_quantity_line_count": 0,
                "usable_line_count": 0,
                "usable_quantity": 0.0,
                "excluded_accepted_ff_shipment_count": 1,
                "excluded_accepted_ff_line_count": 1,
                "excluded_accepted_ff_quantity": 33.0,
            },
            "warnings": ["Источник supplier registry: accepted_ff excluded in fixture."],
        }
        if refreshed
        else {
            "source": "supplier_registry",
            "status": "ready",
            "acceptance_days": 30,
            "shipment_summary": [
                {
                    "shipment_id": "supplier_refresh_probe",
                    "shipment_label": "PATCH-TARGET",
                    "invoice_no": "PATCH-TARGET",
                    "invoice_date": "2026-04-19",
                    "total_product_quantity": 33.0,
                    "shipment_date": "2026-04-20",
                    "calculated_acceptance_date": "2026-05-20",
                    "matched_line_count": 1,
                    "unmatched_line_count": 0,
                    "ambiguous_line_count": 0,
                    "missing_shipment_date_line_count": 0,
                    "usable_quantity": 33.0,
                    "order_status": "production",
                }
            ],
            "diagnostics": {
                "shipment_count": 1,
                "product_line_count": 1,
                "matched_line_count": 1,
                "unmatched_line_count": 0,
                "ambiguous_line_count": 0,
                "missing_shipment_date_line_count": 0,
                "invalid_quantity_line_count": 0,
                "usable_line_count": 1,
                "usable_quantity": 33.0,
                "excluded_accepted_ff_shipment_count": 0,
                "excluded_accepted_ff_line_count": 0,
                "excluded_accepted_ff_quantity": 0.0,
            },
            "warnings": [],
        }
    )
    return {
        "active_sku_count": len(ACTIVE_SKUS),
        "coverage_contract_note": "-",
        "datasets": {},
        "factory_inbound_source": "manual_excel",
        "stock_ff_source": "manual_excel",
        "manual_stock_ff_dataset": {},
        "onec_stock_ff_summary": {
            "status": "ready",
            "source": "onec_ff_stock",
            "source_label_ru": "1С / Фулфилмент",
            "snapshot_date": "2026-04-20",
            "active_sku_count": len(ACTIVE_SKUS),
            "covered_sku_count": len(ACTIVE_SKUS),
            "positive_stock_sku_count": 2,
            "zero_stock_sku_count": 1,
            "missing_sku_count": 0,
            "total_stock_ff": 125.0,
            "warnings": [],
            "errors": [],
            "sample_rows": [],
        },
        "manual_factory_inbound_dataset": {},
        "supplier_registry_inbound_summary": supplier_summary,
        "last_result": None,
    }


def _wb_supply_overlay_options_payload() -> dict[str, object]:
    common = {
        "status_label": "Отгрузка разрешена",
        "selected_date": "2026-04-22",
        "warehouse_name": "Коледино",
        "district_key": "central_north",
        "usable_sku_count": 1,
        "date_evidence": "supply_date",
        "district_mapping_source": "warehouse_id",
        "disabled_reasons": [],
    }
    return {
        "contract_name": "sheet_vitrina_v1_wb_supply_overlay_options",
        "contract_version": 1,
        "eligible_status_ids": [3, 4, 6],
        "summary": {
            "total": 4,
            "eligible": 2,
            "disabled": 1,
            "excluded_by_status": 1,
        },
        "warnings": [],
        "warning_details": {},
        "options": [
            {
                **common,
                "supply_id": "supply-A",
                "number_label": "WB-A",
                "eligible_for_overlay": True,
                "disabled": False,
                "usable_total_qty": 12,
            },
            {
                **common,
                "supply_id": "supply-B",
                "number_label": "WB-B",
                "eligible_for_overlay": True,
                "disabled": False,
                "usable_total_qty": 7,
            },
            {
                **common,
                "supply_id": "supply-disabled",
                "number_label": "WB-disabled",
                "eligible_for_overlay": True,
                "disabled": True,
                "usable_total_qty": 4,
                "disabled_reasons": ["нет usable active SKU quantity"],
            },
            {
                **common,
                "supply_id": "supply-ineligible",
                "number_label": "WB-ineligible",
                "status_label": "Принято",
                "eligible_for_overlay": False,
                "disabled": False,
                "usable_total_qty": 20,
            },
        ],
    }


def run_browser_checks(base_url: str, *, ignore_https_errors: bool) -> dict[str, object]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            persistence_result = _run_persistence_scenario(
                browser.new_context(ignore_https_errors=ignore_https_errors),
                base_url,
            )
            fallback_result = _run_fallback_scenario(
                browser.new_context(ignore_https_errors=ignore_https_errors),
                base_url,
            )
        finally:
            browser.close()
    return {
        "base_url": base_url,
        "storage_key": STORAGE_KEY,
        "default_state": persistence_result["default_state"],
        "top_tab_persistence": persistence_result["top_tab_persistence"],
        "subsection_persistence": persistence_result["subsection_persistence"],
        "supplier_registry_refresh": persistence_result["supplier_registry_refresh"],
        "factory_source_persistence": persistence_result["factory_source_persistence"],
        "regional_planning": persistence_result["regional_planning"],
        "ff_stock_negative_row_style": persistence_result["ff_stock_negative_row_style"],
        "ff_stock_operations_controls": persistence_result["ff_stock_operations_controls"],
        "sku_persistence": persistence_result["sku_persistence"],
        "plan_input_defaults": persistence_result["plan_input_defaults"],
        "plan_input_persistence": persistence_result["plan_input_persistence"],
        "wb_supply_auto_default": persistence_result[
            "wb_supply_auto_default"
        ],
        "calculation_registry": persistence_result["calculation_registry"],
        "zero_selection_guard": persistence_result["zero_selection_guard"],
        "invalid_storage_fallback": fallback_result["invalid_storage_fallback"],
        "obsolete_sku_fallback": fallback_result["obsolete_sku_fallback"],
        "invalid_plan_input_fallback": fallback_result["invalid_plan_input_fallback"],
    }


def _run_persistence_scenario(context, base_url: str) -> dict[str, object]:
    page = context.new_page()
    page_errors: list[str] = []
    target_http_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))

    def _capture_target_response(response) -> None:
        path = urllib_parse.urlparse(response.url).path
        if (
            response.status >= 400
            and (
                path == DEFAULT_WB_SUPPLIES_OVERLAY_OPTIONS_PATH
                or path == DEFAULT_SUPPLY_CALCULATIONS_PATH
                or path.startswith(DEFAULT_SUPPLY_CALCULATIONS_PATH + "/")
                or path == DEFAULT_FACTORY_ORDER_CALCULATE_PATH
                or path == DEFAULT_WB_REGIONAL_CALCULATE_PATH
            )
        ):
            target_http_errors.append(f"{response.status} {path}")

    page.on("response", _capture_target_response)
    operator_url = base_url + DEFAULT_SHEET_OPERATOR_UI_PATH
    page.goto(operator_url, wait_until="domcontentloaded")
    ff_stock_negative_row_style = _assert_ff_stock_negative_row_dark_style(page)
    ff_stock_operations_controls = _assert_ff_stock_operations_controls(page)

    default_state = {
        "top_tab": _selected_data_attr(page, "[data-tab-button][aria-selected=\"true\"]", "data-tab-button"),
        "report_section": _selected_data_attr(
            page,
            "[data-report-section-button][aria-selected=\"true\"]",
            "data-report-section-button",
        ),
        "supply_section": _selected_data_attr(
            page,
            "[data-supply-section-button][aria-selected=\"true\"]",
            "data-supply-section-button",
        ),
    }
    if default_state != {"top_tab": "vitrina", "report_section": "daily", "supply_section": "factory"}:
        raise AssertionError(f"default operator state must stay truthful, got {default_state}")
    page.wait_for_function(
        "() => document.getElementById('sellerRecoverySummary') && document.getElementById('sellerRecoverySummary').textContent.includes('seller')"
    )
    if page.locator("#sellerSessionCheckButton").count() != 1:
        raise AssertionError("operator UI must render the seller session-check action")
    if page.locator("#sellerRecoveryStartButton").count() != 1:
        raise AssertionError("operator UI must render the seller recovery start action")
    if page.locator("#sellerRecoverySummary").inner_text().strip() != "Новый запуск восстановления сейчас не выполняется. Сохранённая seller-сессия больше не действует.":
        raise AssertionError("operator UI must hydrate the seller recovery summary from server-side status")
    if page.locator("#sellerRecoveryRunStatus").inner_text().strip() != "Не запущено":
        raise AssertionError("operator UI must show the current recovery run status separately from the session state")
    if page.locator("#sellerRecoverySessionState").inner_text().strip() != "Нужен вход":
        raise AssertionError("operator UI must show the current session state separately from the run lifecycle")
    page.click("#sellerSessionCheckButton")
    page.wait_for_function(
        "() => document.getElementById('sellerRecoverySummary') && document.getElementById('sellerRecoverySummary').textContent.includes('не тот кабинет')"
    )
    if page.locator("#sellerRecoverySummary").inner_text().strip() != "Сессия активна, но открыт не тот кабинет.":
        raise AssertionError("session-check action must refresh the seller recovery summary without starting recovery")

    page.click('[data-tab-button="factory-order"]')
    page.wait_for_function(
        "() => document.querySelectorAll('[data-wb-supply-overlay-checkbox]').length === 4"
    )
    eligible_a = page.locator(
        '[data-wb-supply-overlay-checkbox][value="supply-A"]'
    )
    eligible_b = page.locator(
        '[data-wb-supply-overlay-checkbox][value="supply-B"]'
    )
    disabled_option = page.locator(
        '[data-wb-supply-overlay-checkbox][value="supply-disabled"]'
    )
    ineligible_option = page.locator(
        '[data-wb-supply-overlay-checkbox][value="supply-ineligible"]'
    )
    if (
        not eligible_a.is_checked()
        or not eligible_b.is_checked()
        or not disabled_option.is_disabled()
        or disabled_option.is_checked()
        or not ineligible_option.is_disabled()
        or ineligible_option.is_checked()
    ):
        raise AssertionError(
            "first open must auto-select only backend-eligible, enabled WB supplies"
        )
    if "Автоматически выбрано eligible: 2" not in page.locator(
        "#wbSupplyOverlayMessage"
    ).inner_text():
        raise AssertionError(
            "operator UI must visibly explain the initial eligible auto-selection"
        )
    eligible_a.uncheck()
    page.route(
        "**" + DEFAULT_WB_SUPPLIES_SYNC_PATH,
        lambda route: route.fulfill(
            status=200,
            content_type="application/json; charset=utf-8",
            body=json.dumps(
                {
                    "sync": {
                        "new_rows": 0,
                        "changed_rows": 0,
                        "unchanged_rows": 4,
                        "enriched": 0,
                    }
                }
            ),
        ),
    )
    page.click("#wbSupplyOverlayRefreshButton")
    page.wait_for_function(
        "() => document.getElementById('wbSupplyOverlayMessage')"
        " && document.getElementById('wbSupplyOverlayMessage').textContent"
        ".includes('WB-поставки обновлены')"
    )
    if eligible_a.is_checked() or not eligible_b.is_checked():
        raise AssertionError(
            "options refresh must not silently undo the operator's manual WB selection"
        )
    page.click('[data-supply-section-button="regional"]')
    if eligible_a.is_checked() or not eligible_b.is_checked():
        raise AssertionError(
            "switching between calculation forms must preserve the manual WB selection"
        )
    page.click('[data-supply-section-button="factory"]')
    page.wait_for_function(
        "() => document.getElementById('supplierRegistryInboundSummaryState') && document.getElementById('supplierRegistryInboundSummaryState').textContent.includes('Доступных к factory inbound заказов: 1')"
    )
    if page.locator("#refreshSupplierRegistryInboundButton").count() != 1:
        raise AssertionError("factory-order supplier registry block must expose the manual refresh button")
    initial_supplier_table = page.locator("#supplierRegistryInboundSummaryTableWrap").inner_text()
    if "PATCH-TARGET" not in initial_supplier_table:
        raise AssertionError(f"supplier registry fixture row must be visible before refresh, got {initial_supplier_table!r}")
    page.click("#refreshSupplierRegistryInboundButton")
    page.wait_for_function(
        "() => document.getElementById('supplierRegistryInboundSummaryState') && document.getElementById('supplierRegistryInboundSummaryState').textContent.includes('Исключено accepted_ff: 1')"
    )
    supplier_refresh_state = {
        "summary": page.locator("#supplierRegistryInboundSummaryState").inner_text(),
        "diagnostics": page.locator("#supplierRegistryInboundDiagnostics").inner_text(),
        "table_hidden": page.locator("#supplierRegistryInboundSummaryTableWrap").is_hidden(),
        "message": page.locator("#factoryMessage").inner_text(),
    }
    if "PATCH-TARGET" in (page.locator("#supplierRegistryInboundSummaryBody").text_content() or ""):
        raise AssertionError("accepted_ff supplier row must disappear from the supplier registry inbound table after refresh")
    if not supplier_refresh_state["table_hidden"]:
        raise AssertionError(f"supplier registry table must collapse when only accepted_ff rows remain, got {supplier_refresh_state}")
    if "excluded accepted_ff shipments: 1" not in supplier_refresh_state["diagnostics"]:
        raise AssertionError(f"supplier registry diagnostics must expose accepted_ff exclusion counters, got {supplier_refresh_state}")
    if "обновлена" not in supplier_refresh_state["message"].lower():
        raise AssertionError(f"manual supplier registry refresh must show a success message, got {supplier_refresh_state}")
    if not page.locator('input[name="factoryInboundSource"][value="manual_excel"]').is_checked():
        raise AssertionError("factory-order UI must default factory inbound source to manual_excel")
    if not page.locator('input[name="stockFfSource"][value="manual_excel"]').is_checked():
        raise AssertionError("stock_ff UI must default source to manual_excel")
    if page.locator("#stockFfManualPanel").is_hidden() or not page.locator("#stockFfOnecPanel").is_hidden():
        raise AssertionError("manual stock_ff controls must be visible only in manual mode")
    page.check('input[name="factoryInboundSource"][value="supplier_registry"]')
    page.check('input[name="stockFfSource"][value="onec_ff_stock"]')
    page.wait_for_function(
        """(storageKey) => {
            const raw = window.localStorage.getItem(storageKey);
            if (!raw) return false;
            const parsed = JSON.parse(raw);
            return parsed.factory_inbound_source === "supplier_registry" &&
                parsed.stock_ff_source === "onec_ff_stock";
        }""",
        arg=STORAGE_KEY,
    )
    if not page.locator("#stockFfManualPanel").is_hidden() or page.locator("#stockFfOnecPanel").is_hidden():
        raise AssertionError("1C stock_ff controls must be visible only in 1C mode")
    page.click("#checkStockFfOnecButton")
    page.wait_for_function(
        "() => document.getElementById('stockFfOnecDiagnostics') && document.getElementById('stockFfOnecDiagnostics').textContent.includes('готов')"
    )
    calculate_requests: list[dict[str, object]] = []
    factory_result_payload: dict[str, object] = {}

    def _capture_factory_calculate(route) -> None:
        nonlocal factory_result_payload
        body = route.request.post_data or "{}"
        request_payload = json.loads(body)
        calculate_requests.append(request_payload)
        factory_result_payload = {
            "status": "success",
            "calculation_id": "calc-browser-factory-registry",
            "calculated_at": "2026-04-20T09:30:00Z",
            "report_date": "2026-04-20",
            "horizon_days": 45,
            "target_window_days": 74,
            "inbound_window_end": "2026-07-03",
            "factory_inbound_source": "supplier_registry",
            "stock_ff_source": "onec_ff_stock",
            "settings": {
                "factory_inbound_source": "supplier_registry",
                "stock_ff_source": "onec_ff_stock",
                "sales_avg_period_days": request_payload.get(
                    "sales_avg_period_days"
                ),
                "cycle_order_days": request_payload.get("cycle_order_days"),
                "selected_wb_supply_ids": request_payload.get(
                    "selected_wb_supply_ids"
                )
                or [],
            },
            "summary": {
                "total_qty": 12,
                "estimated_weight": 1.03,
                "estimated_volume": 0.01,
            },
            "rows": [
                {
                    "nm_id": 1001,
                    "sku_comment": "SKU Alpha",
                    "recommended_order_qty": 12,
                    "coverage_qty": 9,
                    "shortage_qty": 12,
                }
            ],
            "wb_supply_overlay": {
                "selected_supply_count": 1,
                "stock_ff": {"total_selected_qty": 7},
            },
            "warnings": [],
            "recommendation_download_path": (
                "/v1/sheet-vitrina-v1/supply/factory-order/"
                "recommendation.xlsx"
            ),
        }
        route.fulfill(
            status=200,
            content_type="application/json; charset=utf-8",
            body=json.dumps(factory_result_payload, ensure_ascii=False),
        )

    def _capture_supply_registry(route) -> None:
        parsed = urllib_parse.urlparse(route.request.url)
        selected_ids = (
            factory_result_payload.get("settings", {}).get(
                "selected_wb_supply_ids", []
            )
            if isinstance(factory_result_payload.get("settings"), dict)
            else []
        )
        list_item = {
            "record_id": "calc-browser-factory-registry",
            "calculation_id": "calc-browser-factory-registry",
            "calculation_type": "factory_order",
            "completeness": "complete",
            "is_reproducible": True,
            "calculated_at": "2026-04-20T09:30:00Z",
            "report_date": "2026-04-20",
            "status": "success",
            "summary": factory_result_payload.get("summary") or {},
            "key_settings": factory_result_payload.get("settings") or {},
            "selected_wb_supply_count": len(selected_ids),
            "selected_wb_supply_qty": 7,
            "incident_policy": {
                "revision": 4,
                "status": "active",
                "quality_state": "complete",
            },
            "warning_count": 0,
            "download_available": True,
            "export": {
                "available": True,
                "filename": "factory-registry.xlsx",
                "content_type": (
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet"
                ),
                "sha256": "sha256:browser-registry",
            },
            "legacy_note": "",
        }
        if parsed.path.endswith("/download"):
            route.fulfill(
                status=200,
                content_type=(
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet"
                ),
                body=b"historical-browser-xlsx",
            )
            return
        if parsed.path == DEFAULT_SUPPLY_CALCULATIONS_PATH:
            route.fulfill(
                status=200,
                content_type="application/json; charset=utf-8",
                body=json.dumps(
                    {
                        "contract_name": (
                            "sheet_vitrina_v1_supply_calculation_registry"
                        ),
                        "contract_version": 1,
                        "pagination": {"limit": 25, "offset": 0, "total": 1},
                        "records": [list_item],
                    },
                    ensure_ascii=False,
                ),
            )
            return
        route.fulfill(
            status=200,
            content_type="application/json; charset=utf-8",
            body=json.dumps(
                {
                    **list_item,
                    "payload": factory_result_payload,
                    "payload_sha256": "sha256:browser-payload",
                    "metadata": {
                        "selected_wb_supply_ids": selected_ids,
                        "key_settings": factory_result_payload.get(
                            "settings"
                        )
                        or {},
                    },
                    "evidence": {
                        "sources": {
                            "sales_history": {
                                "fingerprint": "sha256:browser-source"
                            }
                        }
                    },
                    "export_sha256": "sha256:browser-registry",
                },
                ensure_ascii=False,
            ),
        )

    page.route("**" + DEFAULT_FACTORY_ORDER_CALCULATE_PATH, _capture_factory_calculate)
    page.route(
        "**" + DEFAULT_SUPPLY_CALCULATIONS_PATH + "**",
        _capture_supply_registry,
    )
    page.click("#calculateFactoryOrderButton")
    page.wait_for_function("() => document.getElementById('factoryMessage') && document.getElementById('factoryMessage').textContent.includes('Расчёт завершён')")
    if (
        not calculate_requests
        or calculate_requests[-1].get("stock_ff_source") != "onec_ff_stock"
        or calculate_requests[-1].get("selected_wb_supply_ids")
        != ["supply-B"]
    ):
        raise AssertionError(f"factory calculate payload must include selected stock_ff_source, got {calculate_requests}")
    page.click('[data-supply-section-button="registry"]')
    page.wait_for_function(
        "() => document.querySelectorAll('[data-supply-calculation-open]').length === 1"
    )
    registry_table_text = page.locator(
        "#supplyCalculationRegistryBody"
    ).inner_text()
    if (
        "Заказ на фабрике" not in registry_table_text
        or "12 шт." not in registry_table_text
        or "1 шт. поставок" not in registry_table_text
        or "Полная · immutable" not in registry_table_text
    ):
        raise AssertionError(
            f"registry list must expose truthful calculation metadata: {registry_table_text}"
        )
    page.click(
        '[data-supply-calculation-open="calc-browser-factory-registry"]'
    )
    page.wait_for_function(
        "() => document.getElementById('supplyCalculationRegistryDetail')"
        " && !document.getElementById('supplyCalculationRegistryDetail').hidden"
    )
    if (
        "supply-B"
        not in page.locator(
            "#supplyCalculationRegistrySelectedSupplies"
        ).inner_text()
        or "SKU Alpha"
        not in page.locator("#supplyCalculationRegistryDetailBody").inner_text()
        or not (
            page.locator("#supplyCalculationRegistryDownload")
            .get_attribute("href")
            or ""
        ).endswith("/calc-browser-factory-registry/download")
    ):
        raise AssertionError(
            "registry detail must show the exact saved WB IDs, result row and historical download"
        )
    page.click('[data-supply-section-button="regional"]')
    page.reload(wait_until="domcontentloaded")
    factory_state = {
        "top_tab": _selected_data_attr(page, "[data-tab-button][aria-selected=\"true\"]", "data-tab-button"),
        "supply_section": _selected_data_attr(
            page,
            "[data-supply-section-button][aria-selected=\"true\"]",
            "data-supply-section-button",
        ),
    }
    if factory_state != {"top_tab": "factory-order", "supply_section": "regional"}:
        raise AssertionError(f"top tab + supply subsection must survive reload, got {factory_state}")
    page.wait_for_function(
        "() => document.querySelectorAll('[data-wb-supply-overlay-checkbox]').length === 4"
    )
    if (
        not page.locator(
            '[data-wb-supply-overlay-checkbox][value="supply-A"]'
        ).is_checked()
        or not page.locator(
            '[data-wb-supply-overlay-checkbox][value="supply-B"]'
        ).is_checked()
    ):
        raise AssertionError(
            "a genuinely new page opening must apply the current eligible defaults again"
        )
    if page.locator('input[name="regionalIncludedDistrict"]').count() != 8:
        raise AssertionError("regional selector must render eight planning-zone checkboxes")
    if page.locator('input[name="regionalIncludedDistrict"]:checked').count() != 8:
        raise AssertionError("regional district selector must default to all districts")
    if page.locator("[data-regional-lead-time-district]").count() != 8:
        raise AssertionError("regional selector must render eight planning-zone delivery inputs")
    for district_key in ("central_north", "central_east", "central_south", "northwest", "volga", "ural", "south_caucasus", "far_siberia"):
        if page.locator(f'[data-regional-lead-time-district="{district_key}"]').input_value() != "15":
            raise AssertionError("regional district delivery inputs must default to 15")
    selector_text = page.locator("#regionalDistrictSelectorList").inner_text()
    for abbreviation in ("ЦФО Север", "ЦФО Восток", "ЦФО Юг", "СЗФО", "ПФО", "УФО", "ЮФО/СКФО", "ДВФО/СФО"):
        if abbreviation not in selector_text:
            raise AssertionError(f"regional selector must use district abbreviation {abbreviation!r}")
    if "Центральный федеральный округ" in selector_text:
        raise AssertionError("regional selector must not use full district names as primary card labels")
    page.click("#regionalDistrictExcludeFarSiberiaButton")
    page.wait_for_function(
        """(storageKey) => {
            const raw = window.localStorage.getItem(storageKey);
            if (!raw) return false;
            const parsed = JSON.parse(raw);
            return Array.isArray(parsed.wb_regional_included_district_keys) &&
                parsed.wb_regional_included_district_keys.length === 7 &&
                !parsed.wb_regional_included_district_keys.includes("far_siberia");
        }""",
        arg=STORAGE_KEY,
    )
    page.fill('[data-regional-lead-time-district="central_north"]', "2")
    page.dispatch_event('[data-regional-lead-time-district="central_north"]', "change")
    page.fill('[data-regional-lead-time-district="northwest"]', "10")
    page.dispatch_event('[data-regional-lead-time-district="northwest"]', "change")
    page.click("#regionalDistrictSelectAllButton")
    page.click("#regionalDistrictExcludeFarSiberiaButton")
    if page.locator('[data-regional-lead-time-district="central_north"]').input_value() != "2":
        raise AssertionError("regional select-all/exclude buttons must not reset central delivery days")
    if page.locator('[data-regional-lead-time-district="northwest"]').input_value() != "10":
        raise AssertionError("regional select-all/exclude buttons must not reset northwest delivery days")
    regional_requests: list[dict[str, object]] = []
    regional_planning_requests: list[dict[str, object]] = []
    regional_result_payload: dict[str, object] = {}
    regional_status_last_result: dict[str, object] = {
        "status": "success",
        "calculation_id": "calc-browser-old",
        "calculated_at": "2026-04-20T09:00:00Z",
        "report_date": "2026-04-20",
        "horizon_days": 7,
        "active_sku_count": 33,
        "payload_version": "v2_planning_zones",
        "settings": {
            "included_district_keys": ["central_north", "central_east", "central_south", "northwest", "volga", "ural", "south_caucasus"],
        },
        "summary": {"total_qty": 100, "estimated_weight": 0.0, "estimated_volume": 0.0},
        "districts": [
            {
                "district_key": "central_north",
                "planning_zone_key": "central_north",
                "planning_zone_label": "ЦФО Север",
                "district_name_ru": "ЦФО Север",
                "total_qty": 100,
                "deficit_qty": 10,
                "filename": "wb_regional_central_old.xlsx",
                "download_path": "/v1/sheet-vitrina-v1/supply/wb-regional/district/central_north.xlsx",
                "rows": [],
            },
        ],
    }

    def _capture_regional_status(route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json; charset=utf-8",
            body=json.dumps(
                {
                    "active_sku_count": len(ACTIVE_SKUS),
                    "methodology_note": "-",
                    "shared_datasets": {},
                    "last_result": regional_status_last_result or regional_result_payload or None,
                },
                ensure_ascii=False,
            ),
        )

    def _capture_regional_calculate(route) -> None:
        nonlocal regional_result_payload
        body = route.request.post_data or "{}"
        request_payload = json.loads(body)
        regional_requests.append(request_payload)
        regional_result_payload = {
            "status": "success",
            "calculation_id": "calc-browser-regional",
            "calculated_at": "2026-04-20T10:00:00Z",
            "report_date": "2026-04-20",
            "horizon_days": 7,
            "active_sku_count": 33,
            "payload_version": "v2_planning_zones",
            "methodology_note": "test methodology note",
            "settings": {
                "included_district_keys": ["central_north", "central_east", "central_south", "northwest", "volga", "ural", "south_caucasus"],
                "lead_time_to_region_days_by_district": request_payload.get("lead_time_to_region_days_by_district") or {},
            },
            "summary": {"total_qty": 0, "estimated_weight": 0.0, "estimated_volume": 0.0},
            "diagnostics": {
                "regional_demand_method": "regional_share_ladder",
                "sku_count": 33,
                "fallback_sku_count": 0,
                "fallback_nm_ids": list(range(100000001, 100000041)),
                "share_source_counts": {
                    "full_clean_days": 1,
                    "partial_district_observations": 18,
                    "sku_group_prior": 82,
                    "global_prior": 57,
                    "seed_floor": 7,
                },
                "low_confidence_sku_district_count": 24,
                "persistent_zero_sku_count": 6,
                "persistent_zero_nm_ids": [497413772, 497415593],
                "zero_zero_no_signal_day_count_by_district": {
                    "south_caucasus": 28,
                    "ural": 12,
                },
                "seed_candidate_sku_count": 6,
                "seed_candidate_sku_district_count": 8,
                "seed_sku_count": 5,
                "seed_sku_district_count": 7,
                "seed_allocated_qty_total": 1750,
                "seed_unfulfilled_qty_total": 250,
                "seed_by_nm_id": {
                    "497413772": {
                        "seed_district_keys": ["south_caucasus"],
                        "seed_qty_by_district": {"south_caucasus": 250},
                        "seed_reason_by_district": {"south_caucasus": "seed_floor"},
                    }
                },
                "requested_valid_day_count": 14,
                "min_selected_valid_day_count": 0,
                "max_selected_valid_day_count": 14,
                "max_inspected_day_count": 120,
                "included_district_keys": ["central_north", "central_east", "central_south", "northwest", "volga", "ural", "south_caucasus"],
                "excluded_district_keys": ["far_siberia"],
                "lead_time_to_region_days_by_district": request_payload.get("lead_time_to_region_days_by_district") or {},
                "excluded_day_reason_counts": {
                    "district_out_of_stock_risk": 1397,
                    "district_restock_or_upward_correction": 30,
                },
                "method_counts": {
                    "full_clean_days": 1,
                    "partial_district_observations": 9,
                    "sku_group_prior": 10,
                    "global_prior": 13,
                },
            },
            "warnings": ["Low-confidence SKU-district regional shares: 24"],
            "recommendations_zip_path": "/v1/sheet-vitrina-v1/supply/wb-regional/recommendations.zip",
            "districts": [
                {
                    "district_key": "central_north",
                    "planning_zone_key": "central_north",
                    "planning_zone_label": "ЦФО Север",
                    "district_name_ru": "ЦФО Север",
                    "total_qty": 100,
                    "deficit_qty": 10,
                    "filename": "wb_regional_central_north_fo.xlsx",
                    "download_path": "/v1/sheet-vitrina-v1/supply/wb-regional/district/central_north.xlsx",
                    "rows": [],
                },
                {
                    "district_key": "central_east",
                    "planning_zone_key": "central_east",
                    "planning_zone_label": "ЦФО Восток",
                    "district_name_ru": "ЦФО Восток",
                    "total_qty": 50,
                    "deficit_qty": 15,
                    "filename": "wb_regional_central_east_fo.xlsx",
                    "download_path": "/v1/sheet-vitrina-v1/supply/wb-regional/district/central_east.xlsx",
                    "rows": [],
                },
                {
                    "district_key": "central_south",
                    "planning_zone_key": "central_south",
                    "planning_zone_label": "ЦФО Юг",
                    "district_name_ru": "ЦФО Юг",
                    "total_qty": 75,
                    "deficit_qty": 25,
                    "filename": "wb_regional_central_south_fo.xlsx",
                    "download_path": "/v1/sheet-vitrina-v1/supply/wb-regional/district/central_south.xlsx",
                    "rows": [],
                },
                {
                    "district_key": "northwest",
                    "district_name_ru": "Северо-Западный федеральный округ",
                    "total_qty": 200,
                    "deficit_qty": 20,
                    "filename": "wb_regional_northwest_fo.xlsx",
                    "download_path": "/v1/sheet-vitrina-v1/supply/wb-regional/district/northwest.xlsx",
                    "rows": [],
                },
                {
                    "district_key": "volga",
                    "district_name_ru": "Приволжский федеральный округ",
                    "total_qty": 0,
                    "deficit_qty": 30,
                    "filename": "wb_regional_volga_fo.xlsx",
                    "download_path": "/v1/sheet-vitrina-v1/supply/wb-regional/district/volga.xlsx",
                    "rows": [],
                },
                {
                    "district_key": "ural",
                    "district_name_ru": "Уральский федеральный округ",
                    "total_qty": 400,
                    "deficit_qty": 40,
                    "filename": "wb_regional_ural_fo.xlsx",
                    "download_path": "/v1/sheet-vitrina-v1/supply/wb-regional/district/ural.xlsx",
                    "rows": [],
                },
                {
                    "district_key": "south_caucasus",
                    "district_name_ru": "Южный и Северо-Кавказский федеральный округ",
                    "total_qty": 500,
                    "deficit_qty": 50,
                    "filename": "wb_regional_south_caucasus_fo.xlsx",
                    "download_path": "/v1/sheet-vitrina-v1/supply/wb-regional/district/south_caucasus.xlsx",
                    "rows": [],
                },
                {
                    "district_key": "far_siberia",
                    "district_name_ru": "Дальневосточный и Сибирский федеральный округ",
                    "total_qty": 600,
                    "deficit_qty": 60,
                    "filename": "wb_regional_far_siberia_fo.xlsx",
                    "download_path": "/v1/sheet-vitrina-v1/supply/wb-regional/district/far_siberia.xlsx",
                    "rows": [],
                },
            ],
        }
        route.fulfill(
            status=200,
            content_type="application/json; charset=utf-8",
            body=json.dumps(regional_result_payload, ensure_ascii=False),
        )

    def _capture_regional_planning(route) -> None:
        nonlocal regional_status_last_result
        body = route.request.post_data or "{}"
        request_payload = json.loads(body)
        regional_planning_requests.append(request_payload)
        if len(regional_planning_requests) == 1:
            regional_status_last_result = {
                **regional_result_payload,
                "calculation_id": "calc-browser-regional-retry",
                "calculated_at": "2026-04-20T10:01:00Z",
            }
            route.fulfill(
                status=200,
                content_type="application/json; charset=utf-8",
                body=json.dumps(
                    {
                        "contract_name": "sheet_vitrina_v1_wb_regional_supply_planning",
                        "contract_version": "v2_planning_zones",
                        "status": "blocked",
                        "calculation_id": "calc-browser-regional-retry",
                        "district_key": "central_north",
                        "planning_zone_key": "central_north",
                        "planning_zone_label": "ЦФО Север",
                        "warnings": [],
                        "blockers": [
                            {
                                "code": "calculation_id_mismatch",
                                "message": "Последний региональный расчёт отличается от запрошенного calculation_id.",
                                "requested_calculation_id": request_payload.get("calculation_id"),
                                "actual_calculation_id": "calc-browser-regional-retry",
                            }
                        ],
                        "summary": {"planned_product_count": 0, "planned_qty_total": 0, "option_count": 0},
                        "options": [],
                    },
                    ensure_ascii=False,
                ),
            )
            return
        if len(regional_planning_requests) == 3:
            route.fulfill(
                status=500,
                content_type="application/json; charset=utf-8",
                body=json.dumps({"error": "planned fixture error"}, ensure_ascii=False),
            )
            return
        route.fulfill(
            status=200,
            content_type="application/json; charset=utf-8",
            body=json.dumps(
                {
                    "contract_name": "sheet_vitrina_v1_wb_regional_supply_planning",
                    "contract_version": "v2_planning_zones",
                    "status": "ready",
                    "calculation_id": request_payload.get("calculation_id") or "calc-browser-regional-retry",
                    "district_key": "central_north",
                    "district_name_ru": "ЦФО Север",
                    "planning_zone_key": "central_north",
                    "planning_zone_label": "ЦФО Север",
                    "package_type": "box",
                    "products": [
                        {
                            "nm_id": 1001,
                            "sku_label": "SKU Alpha",
                            "quantity": 100,
                            "barcode": "4600000000001",
                            "barcodes": ["4600000000001"],
                            "barcode_source": "manual",
                            "barcode_status": "manual",
                            "barcode_ready": True,
                        }
                    ],
                    "barcode_summary": {"total": 1, "ready": 1, "missing": 0, "manual": 1, "wb_content": 0, "multiple": 0},
                    "summary": {
                        "planned_product_count": 1,
                        "planned_qty_total": 100,
                        "option_count": 1,
                        "grouped_warehouse_count": 1,
                        "available_option_count": 1,
                        "excluded_option_count": 8,
                        "sorting_center_excluded_count": 1,
                        "specialized_excluded_count": 2,
                        "partial_excluded_count": 1,
                        "blocked_excluded_count": 2,
                        "accepts_all_barcode_option_count": 1,
                        "sgt_option_count": 0,
                        "same_district_option_count": 1,
                        "outside_district_option_count": 0,
                        "unmapped_option_count": 0,
                        "transit_option_count": 0,
                    },
                    "warnings": [],
                    "blockers": [],
                    "options": [
                        {
                            "option_id": "browser-option-1",
                            "rank": 1,
                            "recommendation": "Рекомендуемый склад",
                            "recommendation_explanation": "ЦФО Север; основной; ближайшая дата 2026-07-01.",
                            "option_kind": "warehouse_group",
                            "date": "2026-07-01",
                            "dates": [{"date": "2026-07-01", "coefficient": 1, "allow_unload": True, "is_available": True, "is_good_date": True, "is_free_date": False, "package_type": "box", "status": "paid"}],
                            "date_count": 1,
                            "good_date_count": 1,
                            "free_date_count": 0,
                            "first_available_date": "2026-07-01",
                            "first_free_date": "",
                            "unique_available_date_count": 1,
                            "unique_free_date_count": 0,
                            "planning_zone_key": "central_north",
                            "planning_zone_label": "ЦФО Север",
                            "warehouse_id": 301806,
                            "warehouse_name": "Тверь",
                            "warehouse_role": "primary",
                            "warehouse_scope": "same_district",
                            "route_type": "direct",
                            "transit_warehouse_id": "",
                            "transit_warehouse_name": "",
                            "coefficient": 1,
                            "coefficient_display": "1",
                            "allow_unload": True,
                            "barcode_coverage": {"accepted_count": 1, "total_count": 1, "accepts_all_barcodes": True},
                            "accepts_all_barcodes": True,
                            "package_type": "box",
                            "package_supported": True,
                            "is_storage_warehouse": True,
                            "is_sorting_center": False,
                            "recommendation_enabled": True,
                            "direct_destination": True,
                            "box_tariff": {
                                "warehouseName": "Тверь",
                                "boxDeliveryCoefExpr": "110%",
                                "boxStorageCoefExpr": "105%",
                                "boxDeliveryBase": "5",
                                "boxDeliveryLiter": "1,5",
                                "logistics_display": "110%",
                                "storage_display": "105%",
                            },
                            "transit_route_count": 0,
                            "best_transit_route": None,
                            "tariff_evidence": {"box": {"warehouseName": "Тверь"}, "transit": None},
                            "blocker_codes": [],
                            "exclusion_reasons": [],
                            "status": "available",
                            "warnings": [],
                            "operator_handoff": {
                                "copy_format": "json",
                                "district_key": "central_north",
                                "planning_zone_key": "central_north",
                                "warehouse_name": "Тверь",
                                "date": "2026-07-01",
                                "products": [{"nm_id": 1001, "barcode": "4600000000001", "quantity": 100}],
                            },
                        }
                    ],
                    "major_warehouse_diagnostics": [
                        {
                            "warehouse_id": 301806,
                            "expected_warehouse_name": "Тверь",
                            "planning_zone_key": "central_north",
                            "found_in_catalog": True,
                            "catalog_active": True,
                            "status": "returned_by_general_acceptance_options",
                            "probe_called": False,
                        },
                    ],
                    "diagnostics": {
                        "request_id": "browser-request-id",
                        "requested_barcode_count": 1,
                        "raw_option_count": 12,
                        "grouped_warehouse_count": 9,
                        "warehouse_registry_version": "central-storage-v1-2026-07-19",
                        "exclusion_reason_counts": {"sorting_center": 1, "specialized_food": 1, "partial_barcode_coverage": 1},
                        "excluded_options": [
                            {
                                "warehouse_id": 910004,
                                "warehouse_name": "СЦ Тверь",
                                "exclusion_reasons": ["sorting_center", "warehouse_unclassified"],
                            }
                        ],
                    },
                    "cache": {"enabled": False},
                    "evidence": {"wb_api_read_only": True, "no_wb_mutations": True},
                },
                ensure_ascii=False,
            ),
        )

    page.route("**" + DEFAULT_WB_REGIONAL_STATUS_PATH, _capture_regional_status)
    page.route("**" + DEFAULT_WB_REGIONAL_CALCULATE_PATH, _capture_regional_calculate)
    page.route("**" + DEFAULT_WB_REGIONAL_PLANNING_OPTIONS_PATH, _capture_regional_planning)
    page.click("#calculateRegionalSupplyButton")
    page.wait_for_function("() => document.getElementById('regionalMessage') && document.getElementById('regionalMessage').textContent.includes('Расчёт выполнен')")
    if not regional_requests or regional_requests[-1].get("included_district_keys") != ["central_north", "central_east", "central_south", "northwest", "volga", "ural", "south_caucasus"]:
        raise AssertionError(f"regional calculate payload must include selected districts, got {regional_requests}")
    if regional_requests[-1].get("selected_wb_supply_ids") != [
        "supply-A",
        "supply-B",
    ]:
        raise AssertionError(
            "regional calculate payload must include the exact visible auto-default WB IDs"
        )
    lead_time_payload = regional_requests[-1].get("lead_time_to_region_days_by_district")
    expected_lead_times = {
        "central_north": 2,
        "central_east": 15,
        "central_south": 15,
        "northwest": 10,
        "volga": 15,
        "ural": 15,
        "south_caucasus": 15,
        "far_siberia": 15,
    }
    if lead_time_payload != expected_lead_times:
        raise AssertionError(f"regional calculate payload must include per-district delivery days, got {regional_requests}")
    regional_message = page.locator("#regionalMessage").inner_text()
    if "100000001" in regional_message:
        raise AssertionError("regional main result message must not include long fallback nmIds")
    if "Расчёт завершён с предупреждениями" in regional_message:
        raise AssertionError("normal ladder recovery must not look like a warning in the main result")
    if "Все 33 SKU рассчитаны без старого fallback" not in regional_message:
        raise AssertionError(f"regional main result must explain fallback-free calculation, got {regional_message!r}")
    if "Тестовые коробки: 5 SKU / 7 направлений SKU-округ, всего 1750 шт." not in regional_message:
        raise AssertionError(f"regional main result must use SKU-district wording for seed, got {regional_message!r}")
    for technical in ("partial_district_observations", "district_zero_zero_no_signal", "district_restock_or_upward_correction"):
        if technical in regional_message:
            raise AssertionError(f"technical code {technical!r} must not be visible in the main result")
    diagnostics_note = page.locator("#regionalDiagnosticsNote").inner_text()
    if "Доли восстановлены по расширенной методологии" not in diagnostics_note or "направлений SKU-округ" not in diagnostics_note:
        raise AssertionError(f"regional diagnostics note must include compact Russian methodology summary, got {diagnostics_note}")
    if "Fallback на старую формулу: 0 SKU" not in diagnostics_note:
        raise AssertionError(f"regional diagnostics note must show human fallback wording, got {diagnostics_note}")
    for technical in ("partial_district_observations", "district_zero_zero_no_signal", "district_restock_or_upward_correction"):
        if technical in diagnostics_note:
            raise AssertionError(f"technical code {technical!r} must not be visible in diagnostics note")
    if page.locator("#regionalDiagnosticsDetails").is_hidden():
        raise AssertionError("regional diagnostics details must be visible after fallback result")
    district_rows = page.locator("#regionalDistrictTableBody tr")
    if district_rows.count() != 7:
        raise AssertionError(f"regional district table must show only included districts, got {district_rows.count()} rows")
    district_table_text = page.locator("#regionalDistrictTableBody").inner_text()
    if not all(label in district_table_text for label in ("ЦФО Север", "ЦФО Восток", "ЦФО Юг", "СЗФО")):
        raise AssertionError(f"regional district table must use short district labels, got {district_table_text!r}")
    if "ЦФО Запад" in district_table_text:
        raise AssertionError(f"central west must not appear in planning results: {district_table_text!r}")
    if "Центральный федеральный округ" in district_table_text:
        raise AssertionError(f"regional district table must not use full district names as primary labels, got {district_table_text!r}")
    if "Дальневосточный и Сибирский" in district_table_text or "far_siberia" in district_table_text:
        raise AssertionError(f"excluded far_siberia must not be visible in summary/download table, got {district_table_text!r}")
    if not page.locator('input[name="regionalIncludedDistrict"][value="far_siberia"]').count():
        raise AssertionError("excluded far_siberia must remain available in selector options")
    if page.locator('input[name="regionalIncludedDistrict"][value="far_siberia"]').is_checked():
        raise AssertionError("far_siberia selector checkbox must stay unchecked after exclusion")
    if page.locator("#downloadRegionalRecommendationsZipButton").is_disabled():
        raise AssertionError("regional ZIP download button must be enabled after successful result")
    if not page.locator("#regionalPlanningPanel").is_hidden():
        raise AssertionError("new regional calculation must clear stale planning panel before a district is selected")
    overflow_state = page.evaluate(
        """() => {
            const card = document.querySelector('[aria-labelledby="regional-summary-title"]');
            const details = document.getElementById('regionalDiagnosticsDetails');
            return {
                cardOverflow: card ? card.scrollWidth > card.clientWidth + 2 : true,
                detailsOverflow: details ? details.scrollWidth > details.clientWidth + 2 : true,
                detailsText: details ? details.textContent : ""
            };
        }"""
    )
    if overflow_state["cardOverflow"] or overflow_state["detailsOverflow"]:
        raise AssertionError(f"regional fallback diagnostics must not overflow card, got {overflow_state}")
    if "100000001" not in str(overflow_state["detailsText"]):
        raise AssertionError("regional fallback nmIds must remain available inside diagnostics details")
    details_text = str(overflow_state["detailsText"])
    for expected in (
        "Как считались доли",
        "Частичные наблюдения по округам",
        "пополнение или скачок остатка вверх",
        "Нулевой остаток без сигнала по округам",
        "Исключённые округа: ДВФО/СФО",
        "тестовая поставка",
    ):
        if expected not in details_text:
            raise AssertionError(f"regional diagnostics details must include {expected!r}, got {details_text!r}")
    for technical in ("partial_district_observations", "district_zero_zero_no_signal", "district_restock_or_upward_correction", "seed_floor"):
        if technical in details_text:
            raise AssertionError(f"technical code {technical!r} must be translated in visible diagnostics details")
    if page.locator('[data-regional-planning-district="central_north"]').is_disabled():
        raise AssertionError("regional planning button must be enabled for a district with positive quantity")
    volga_button = page.locator('[data-regional-planning-district="volga"]')
    if not volga_button.is_disabled():
        raise AssertionError("zero-quantity regional planning button must be disabled")
    if "Нет количества к поставке" not in (volga_button.get_attribute("title") or ""):
        raise AssertionError("zero-quantity disabled planning button must explain the reason in title")
    volga_cell_text = volga_button.locator("xpath=ancestor::td[1]").inner_text()
    if "Нет количества к поставке" not in volga_cell_text:
        raise AssertionError(f"zero-quantity disabled reason must be visible near the button, got {volga_cell_text!r}")
    if page.locator('[data-regional-planning-district="central_east"]').count() != 1 or page.locator('[data-regional-planning-district="central_south"]').count() != 1:
        raise AssertionError("all three Central planning-zone rows must expose warehouse selection")
    if page.locator('[data-regional-planning-district="central_west"]').count() != 0:
        raise AssertionError("ЦФО Запад must not appear in result rows")
    page.click('[data-regional-planning-district="central_north"]')
    page.wait_for_function(
        "() => document.getElementById('regionalPlanningMessage') && document.getElementById('regionalPlanningMessage').textContent.includes('Найдено вариантов: 1')"
    )
    if len(regional_planning_requests) < 2:
        raise AssertionError("regional planning button must call planning-options route")
    if regional_planning_requests[0].get("calculation_id") != "calc-browser-regional":
        raise AssertionError(f"regional planning must use fresh calculate result, not stale status result, got {regional_planning_requests}")
    latest_planning_request = regional_planning_requests[-1]
    if latest_planning_request.get("district_key") != "central_north" or latest_planning_request.get("planning_zone_key") != "central_north" or latest_planning_request.get("calculation_id") != "calc-browser-regional-retry":
        raise AssertionError(f"regional planning must retry once with actual calculation id after mismatch, got {regional_planning_requests}")
    if page.locator('[data-regional-planning-district="central_north"]').is_disabled():
        raise AssertionError("regional planning button must re-enable after successful planning response")
    if page.locator("#regionalPlanningPanel").is_hidden():
        raise AssertionError("regional planning panel must render after successful planning response")
    regional_planning_text = page.locator("#regionalPlanningPanel").inner_text()
    for expected in (
        "Подбор складов WB",
        "SKU: 1",
        "ШК готово: 1/1",
        "Разрешённых складов: 1",
        "С доступной датой: 1",
        "Исключено: 8",
        "Тверь",
        "ЦФО Север",
        "Прямой склад хранения",
        "Все обязательные ШК",
        "Логистика: 110%",
        "Хранение: 105%",
        "Безопасная диагностика подбора",
        "WB request ID: browser-request-id",
        "Точные warehouseID-probes складов направления",
        "Почему варианты исключены",
        "Скопировать диагностику JSON",
        "Скопировать",
    ):
        if expected not in regional_planning_text:
            raise AssertionError(f"regional planning panel must include {expected!r}, got {regional_planning_text!r}")
    if "СЦ Тверь" in page.locator("#regionalPlanningTableBody").inner_text():
        raise AssertionError("excluded sorting centres must stay out of the manager table")
    page.locator("#regionalPlanningDiagnostics details summary").click()
    exclusion_detail_text = page.locator("#regionalPlanningDiagnostics details").inner_text()
    if "СЦ Тверь" not in exclusion_detail_text or "Сортировочный центр" not in exclusion_detail_text:
        raise AssertionError(f"diagnostic-only exclusions must explain the warehouse reason: {exclusion_detail_text!r}")
    regional_planning_overflow = page.evaluate(
        """() => {
            const root = document.scrollingElement || document.documentElement;
            const panel = document.getElementById('regionalPlanningPanel');
            const wrap = document.querySelector('.regional-planning-table-wrap');
            const table = document.querySelector('.regional-planning-table');
            const copy = document.getElementById('copyRegionalPlanningPayloadButton');
            return {
                pageOverflow: root ? root.scrollWidth > root.clientWidth + 2 : true,
                panelOverflow: panel ? panel.scrollWidth > panel.clientWidth + 2 : true,
                wrapLocalScrollReady: wrap ? ['auto', 'scroll'].includes(window.getComputedStyle(wrap).overflowX) : false,
                copyVisible: copy ? copy.getBoundingClientRect().width > 0 && copy.getBoundingClientRect().height > 0 : false,
                rootWidth: root ? root.scrollWidth : 0,
                rootClientWidth: root ? root.clientWidth : 0,
                panelWidth: panel ? panel.scrollWidth : 0,
                panelClientWidth: panel ? panel.clientWidth : 0,
                wrapWidth: wrap ? wrap.clientWidth : 0,
                tableWidth: table ? table.scrollWidth : 0
            };
        }"""
    )
    if regional_planning_overflow["pageOverflow"] or regional_planning_overflow["panelOverflow"] or not regional_planning_overflow["copyVisible"]:
        raise AssertionError(f"regional planning panel must not expand the page and copy button must stay visible, got {regional_planning_overflow}")
    if not regional_planning_overflow["wrapLocalScrollReady"]:
        raise AssertionError(f"regional planning table must be contained in local scroll wrapper, got {regional_planning_overflow}")
    regional_planning_layout = page.evaluate(
        """() => {
            const panel = document.getElementById('regionalPlanningPanel');
            const grid = panel && panel.parentElement ? panel.parentElement.querySelector(':scope > .factory-grid') : null;
            const resultCard = document.querySelector('[aria-labelledby="regional-summary-title"]');
            const gridRect = grid ? grid.getBoundingClientRect() : null;
            const panelRect = panel ? panel.getBoundingClientRect() : null;
            return {
                outsideResultCard: Boolean(panel && resultCard && !resultCard.contains(panel)),
                outsideTwoColumnGrid: Boolean(panel && grid && !grid.contains(panel)),
                belowTwoColumnGrid: Boolean(gridRect && panelRect && panelRect.top >= gridRect.bottom - 1),
                sameSurfaceWidth: Boolean(gridRect && panelRect && Math.abs(gridRect.width - panelRect.width) <= 2),
                duplicatePanelCount: document.querySelectorAll('#regionalPlanningPanel').length
            };
        }"""
    )
    if regional_planning_layout != {
        "outsideResultCard": True,
        "outsideTwoColumnGrid": True,
        "belowTwoColumnGrid": True,
        "sameSurfaceWidth": True,
        "duplicatePanelCount": 1,
    }:
        raise AssertionError(f"planning panel must be one full-width card below the two-column grid: {regional_planning_layout}")
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(50)
    regional_planning_mobile = page.evaluate(
        """() => {
            const root = document.scrollingElement || document.documentElement;
            const panel = document.getElementById('regionalPlanningPanel');
            const wrap = document.querySelector('.regional-planning-table-wrap');
            const grid = panel && panel.parentElement ? panel.parentElement.querySelector(':scope > .factory-grid') : null;
            return {
                pageOverflow: root ? root.scrollWidth > root.clientWidth + 2 : true,
                panelWithinViewport: panel ? panel.getBoundingClientRect().width <= window.innerWidth + 1 : false,
                localTableScroll: wrap ? wrap.scrollWidth > wrap.clientWidth : false,
                singleColumnGrid: grid ? window.getComputedStyle(grid).gridTemplateColumns.split(' ').length === 1 : false
            };
        }"""
    )
    if regional_planning_mobile != {
        "pageOverflow": False,
        "panelWithinViewport": True,
        "localTableScroll": True,
        "singleColumnGrid": True,
    }:
        raise AssertionError(f"planning card must remain contained at a narrow viewport: {regional_planning_mobile}")
    page.set_viewport_size({"width": 1280, "height": 720})
    if page.locator("#regionalPlanningFilters").is_hidden():
        raise AssertionError("regional planning grouped filters must be visible when options exist")
    page.fill("#regionalPlanningSearch", "нет такого склада")
    if "Среди разрешённых складов нет совпадений" not in page.locator("#regionalPlanningTableBody").inner_text():
        raise AssertionError("manager search must only filter already allowed warehouse options")
    page.fill("#regionalPlanningSearch", "Тверь")
    if "Тверь" not in page.locator("#regionalPlanningTableBody").inner_text():
        raise AssertionError("warehouse search must restore the allowed option")
    regional_planning_state = {
        "request": latest_planning_request,
        "summary": page.locator("#regionalPlanningSummary").inner_text(),
        "first_option": page.locator("#regionalPlanningTableBody tr").first.inner_text(),
        "overflow": regional_planning_overflow,
        "layout": regional_planning_layout,
        "mobile": regional_planning_mobile,
    }
    page.click('[data-regional-planning-district="central_north"]')
    page.wait_for_function(
        "() => document.getElementById('regionalPlanningMessage') && document.getElementById('regionalPlanningMessage').textContent.includes('Подбор завершился с ошибкой')"
    )
    if page.locator('[data-regional-planning-district="central_north"]').is_disabled():
        raise AssertionError("regional planning button must re-enable after planning error")
    page.click('[data-supply-section-button="factory"]')
    if not page.locator('input[name="factoryInboundSource"][value="supplier_registry"]').is_checked():
        raise AssertionError("factory-order inbound source selection must survive reload")
    if not page.locator('input[name="stockFfSource"][value="onec_ff_stock"]').is_checked():
        raise AssertionError("stock_ff source selection must survive reload")
    factory_source_state = {
        "selected": page.locator('input[name="factoryInboundSource"]:checked').input_value(),
        "stock_ff_selected": page.locator('input[name="stockFfSource"]:checked').input_value(),
        "calculate_payload": calculate_requests[-1] if calculate_requests else {},
        "storage_state": page.evaluate(
            """(storageKey) => {
                const raw = window.localStorage.getItem(storageKey);
                return raw ? JSON.parse(raw) : null;
            }""",
            STORAGE_KEY,
        ),
    }

    page.click('[data-tab-button="reports"]')
    page.click('[data-report-section-button="stock"]')
    if "Настройте SKU" not in page.locator("#stockReportStatus").inner_text():
        raise AssertionError("stock report must start in manual-calculate idle state")
    page.click("#stockReportApplyButton")
    page.wait_for_function(
        "() => document.querySelectorAll('#stockReportRows .stock-report-table tbody tr, #stockReportRows .report-empty').length > 0"
    )
    page.reload(wait_until="domcontentloaded")
    reports_state = {
        "top_tab": _selected_data_attr(page, "[data-tab-button][aria-selected=\"true\"]", "data-tab-button"),
        "report_section": _selected_data_attr(
            page,
            "[data-report-section-button][aria-selected=\"true\"]",
            "data-report-section-button",
        ),
    }
    if reports_state != {"top_tab": "reports", "report_section": "stock"}:
        raise AssertionError(f"reports subsection must survive reload, got {reports_state}")

    if "Настройте SKU" not in page.locator("#stockReportStatus").inner_text():
        raise AssertionError("stock report reload must keep manual-calculate idle state before explicit apply")
    page.click("#stockReportApplyButton")
    page.wait_for_function("() => document.querySelectorAll('#stockReportRows .stock-report-table tbody tr').length > 0")
    if page.locator("#stockReportRows .stock-report-table tbody tr").first.locator("td").first.inner_text().strip() != "Итого":
        raise AssertionError("stock report must keep Итого as the first rendered table row")
    visible_rows = _visible_stock_report_titles(page)
    if len(visible_rows) < 1:
        raise AssertionError("stock report must render at least one row for the persistence smoke")
    kept_label = visible_rows[0]

    _open_stock_selector(page)
    available_labels = _stock_selector_labels(page)
    if kept_label not in available_labels:
        raise AssertionError(f"selector must expose currently visible report rows, missing {kept_label!r} in {available_labels}")
    for label in available_labels:
        checkbox = page.locator(f'#stockReportSkuList input[value="{_nm_id_from_label(label)}"]')
        if label == kept_label:
            checkbox.check()
        else:
            checkbox.uncheck()
    page.click("#stockReportApplyButton")
    _wait_for_row_titles(page, [kept_label])
    page.reload(wait_until="domcontentloaded")
    if "Настройте SKU" not in page.locator("#stockReportStatus").inner_text():
        raise AssertionError("stock report reload must keep manual-calculate idle state before applying persisted SKU subset")
    page.click("#stockReportApplyButton")
    _wait_for_row_titles(page, [kept_label])
    _open_stock_selector(page)
    selected_labels_after_reload = _checked_stock_selector_labels(page)
    if selected_labels_after_reload != [kept_label]:
        raise AssertionError(
            f"stock-report selector must restore the last non-default selection, got {selected_labels_after_reload}"
        )

    page.click("#stockReportClearAllButton")
    page.click("#stockReportApplyButton")
    validation_text = page.locator("#stockReportSkuValidation").text_content() or ""
    if "Выберите хотя бы один SKU" not in validation_text:
        raise AssertionError(f"zero-selection validation must stay active, got {validation_text!r}")

    plan_report_request_urls: list[str] = []

    def _capture_plan_report_request(route) -> None:
        plan_report_request_urls.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json; charset=utf-8",
            body=json.dumps(_plan_report_payload(), ensure_ascii=False),
        )

    page.route(
        "**/v1/sheet-vitrina-v1/plan-report?**",
        _capture_plan_report_request,
    )

    page.click('[data-report-section-button="plan"]')
    default_plan_inputs = {
        "period": page.locator("#planReportPeriodSelect").input_value(),
        "h1": page.locator("#planReportH1Input").input_value(),
        "h2": page.locator("#planReportH2Input").input_value(),
        "drr": page.locator("#planReportDrrInput").input_value(),
        "contract_checkbox_count": page.locator("#planReportContractStartCheckbox").count(),
        "annual_even_checkbox_count": page.locator("#planReportAnnualEvenCheckbox").count(),
        "annual_even_checked": page.locator("#planReportAnnualEvenCheckbox").is_checked(),
        "contract_date_input_count": page.locator("#planReportContractStartDateInput").count(),
    }
    expected_default_plan_inputs = {
        "period": "first_half",
        "h1": "155379879",
        "h2": "294620121",
        "drr": "6",
        "contract_checkbox_count": 0,
        "annual_even_checkbox_count": 1,
        "annual_even_checked": False,
        "contract_date_input_count": 0,
    }
    if default_plan_inputs != expected_default_plan_inputs:
        raise AssertionError(f"clean plan-report storage must restore WB/VB defaults, got {default_plan_inputs}")
    tooltip_text = page.locator("#planReportAnnualEvenTooltip").text_content() or ""
    if "альтернативная оценка темпа" not in tooltip_text and "оценить темп закрытия года" not in tooltip_text:
        raise AssertionError(f"annual-even tooltip must explain alternative pace mode, got {tooltip_text!r}")
    page.focus("#planReportAnnualEvenCheckbox")
    page.wait_for_timeout(160)
    tooltip_visible_on_focus = page.locator("#planReportAnnualEvenTooltip").evaluate(
        """(element) => {
            const style = window.getComputedStyle(element);
            return style.visibility === "visible" && Number(style.opacity) > 0;
        }"""
    )
    if not tooltip_visible_on_focus:
        raise AssertionError("annual-even tooltip must be visible on keyboard focus")
    page.click("#planReportApplyButton")
    page.wait_for_function(
        "() => document.getElementById('planReportContent') && !document.getElementById('planReportContent').hidden"
    )
    result_card_titles = page.evaluate(
        """() => Array.from(document.querySelectorAll('#planReportContent .plan-report-card h3'))
            .map((item) => item.textContent.trim())
            .filter(Boolean)"""
    )
    if not result_card_titles or result_card_titles[0] != "Прогноз к концу договорного периода при текущем темпе":
        raise AssertionError(f"projection block must render first, got {result_card_titles}")
    projection_rows = page.evaluate(
        """() => Array.from(document.querySelectorAll('#planReportProjectionTable tbody tr')).map((row) => {
            const cells = Array.from(row.querySelectorAll('td'));
            const label = row.querySelector('.plan-report-metric-label-text');
            const icon = row.querySelector('.plan-report-metric-tooltip-anchor');
            const tooltip = row.querySelector('.plan-report-metric-tooltip');
            return {
                label: label ? label.textContent.trim() : '',
                forecast: cells[1] ? cells[1].innerText.trim() : '',
                target: cells[2] ? cells[2].innerText.trim() : '',
                comparison: cells[3] ? cells[3].innerText.trim() : '',
                iconAriaLabel: icon ? icon.getAttribute('aria-label') : '',
                iconTitle: icon ? icon.getAttribute('title') : '',
                tooltipText: tooltip ? tooltip.textContent.replace(/\\s+/g, ' ').trim() : '',
                hasAlertClass: row.classList.contains('is-alert') || Boolean(row.querySelector('.is-alert'))
            };
        })"""
    )
    projection_rows_by_label = {row["label"]: row for row in projection_rows}
    annual_row = projection_rows_by_label.get("Годовой план выкупов")
    usn_row = projection_rows_by_label.get("Верхний порог УСН")
    drr_row = projection_rows_by_label.get("Минимальный DRR по договору")
    if not annual_row or annual_row["target"] != "450 млн ₽":
        raise AssertionError(f"projection must preserve the annual 450m buyout plan, got {projection_rows}")
    expected_usn_tooltip = "Управленческий ориентир 2026 года. При превышении 490,5 млн ₽ утрачивается право на УСН. Фактический налоговый лимит контролируется по данным налогового учёта."
    if (
        not usn_row
        or usn_row["target"] != "490,5 млн ₽"
        or "86,19%" not in usn_row["comparison"]
        or expected_usn_tooltip not in usn_row["tooltipText"]
        or expected_usn_tooltip not in usn_row["iconAriaLabel"]
        or expected_usn_tooltip not in usn_row["iconTitle"]
    ):
        raise AssertionError(f"projection must render the USN upper-limit row and accessible tooltip, got {projection_rows}")
    expected_drr_tooltip = "Минимальная доля рекламных расходов по договору с Wildberries — 6%. Значение выше 6% означает запас относительно договорного минимума."
    if (
        not drr_row
        or drr_row["forecast"] != "7%"
        or drr_row["target"] != "6%"
        or drr_row["comparison"] != "запас +1 п.п."
        or drr_row["hasAlertClass"]
        or expected_drr_tooltip not in drr_row["tooltipText"]
        or expected_drr_tooltip not in drr_row["iconAriaLabel"]
        or expected_drr_tooltip not in drr_row["iconTitle"]
    ):
        raise AssertionError(f"DRR above 6% must render as positive minimum margin, got {projection_rows}")
    ads_metric_state = page.evaluate(
        """() => {
            const rows = Array.from(document.querySelectorAll('#planReportSelectedTable tbody tr'));
            const row = rows.find((item) => {
                const labelCell = item.querySelector('td');
                return labelCell && labelCell.textContent.includes('Рекламные расходы, руб.');
            });
            if (!row) return null;
            const labelCell = row.querySelector('td');
            const icon = labelCell.querySelector('.plan-report-metric-tooltip-anchor');
            const tooltip = labelCell.querySelector('.plan-report-metric-tooltip');
            return {
                visibleText: labelCell.innerText.replace(/\\s+/g, ' ').trim(),
                textContent: labelCell.textContent.replace(/\\s+/g, ' ').trim(),
                iconText: icon ? icon.textContent.trim() : '',
                iconAriaLabel: icon ? icon.getAttribute('aria-label') : '',
                iconTitle: icon ? icon.getAttribute('title') : '',
                tooltipText: tooltip ? tooltip.textContent.replace(/\\s+/g, ' ').trim() : '',
                tooltipVisibility: tooltip ? window.getComputedStyle(tooltip).visibility : ''
            };
        }"""
    )
    expected_ads_note = "Рекламный план пересчитан от фактического оборота, так как оборот выше плана."
    if not ads_metric_state:
        raise AssertionError("ads metric row must render in plan-report selected table")
    if expected_ads_note in ads_metric_state["visibleText"]:
        raise AssertionError(f"ads explanatory note must not be visible inline, got {ads_metric_state}")
    if ads_metric_state["iconText"] != "?" or expected_ads_note not in ads_metric_state["tooltipText"]:
        raise AssertionError(f"ads explanatory note must move behind a question tooltip, got {ads_metric_state}")
    if expected_ads_note not in ads_metric_state["iconAriaLabel"] or expected_ads_note not in ads_metric_state["iconTitle"]:
        raise AssertionError(f"ads tooltip must be accessible through aria/title, got {ads_metric_state}")
    page.focus("#planReportSelectedTable .plan-report-metric-tooltip-anchor")
    page.wait_for_timeout(160)
    metric_tooltip_visible = page.locator("#planReportSelectedTable .plan-report-metric-tooltip").evaluate(
        """(element) => {
            const style = window.getComputedStyle(element);
            return style.visibility === "visible" && Number(style.opacity) > 0;
        }"""
    )
    if not metric_tooltip_visible:
        raise AssertionError("ads metric tooltip must be visible on keyboard focus")
    default_plan_report_url = plan_report_request_urls[-1] if plan_report_request_urls else ""
    for expected_query_part in (
        "period=first_half",
        "h1_buyout_plan_rub=155379879",
        "h2_buyout_plan_rub=294620121",
        "plan_drr_pct=6",
        "use_contract_start_date=true",
        "contract_start_date=2026-02-01",
        "annual_plan_evenly_distributed=false",
    ):
        if expected_query_part not in default_plan_report_url:
            raise AssertionError(
                f"default plan-report request must include {expected_query_part}, got {plan_report_request_urls}"
            )

    request_count_before_toggle = len(plan_report_request_urls)
    page.check("#planReportAnnualEvenCheckbox")
    page.click("#planReportApplyButton")
    _wait_for_request_count(plan_report_request_urls, request_count_before_toggle + 1)
    annual_plan_report_url = plan_report_request_urls[-1] if plan_report_request_urls else ""
    for expected_query_part in (
        "use_contract_start_date=true",
        "contract_start_date=2026-02-01",
        "annual_plan_evenly_distributed=true",
    ):
        if expected_query_part not in annual_plan_report_url:
            raise AssertionError(
                f"checked annual-even request must include {expected_query_part}, got {plan_report_request_urls}"
            )

    page.select_option("#planReportPeriodSelect", "current_month")
    page.fill("#planReportH1Input", "123456789")
    page.fill("#planReportH2Input", "234567890")
    page.fill("#planReportDrrInput", "7.5")
    page.click("#planReportApplyButton")
    page.wait_for_function(
        """(storageKey) => {
            const raw = window.localStorage.getItem(storageKey);
            if (!raw) return false;
            const parsed = JSON.parse(raw);
            return parsed.plan_report_inputs &&
                parsed.plan_report_inputs.period === "current_month" &&
                parsed.plan_report_inputs.h1_buyout_plan_rub === "123456789" &&
                parsed.plan_report_inputs.h2_buyout_plan_rub === "234567890" &&
                parsed.plan_report_inputs.plan_drr_pct === "7.5" &&
                parsed.plan_report_inputs.annual_plan_evenly_distributed === true &&
                !("use_contract_start_date" in parsed.plan_report_inputs) &&
                !("contract_start_date" in parsed.plan_report_inputs);
        }""",
        arg=STORAGE_KEY,
    )
    latest_plan_report_url = plan_report_request_urls[-1] if plan_report_request_urls else ""
    if (
        "use_contract_start_date=true" not in latest_plan_report_url
        or "contract_start_date=2026-02-01" not in latest_plan_report_url
        or "annual_plan_evenly_distributed=true" not in latest_plan_report_url
    ):
        raise AssertionError(f"plan-report request must always include canonical WB/VB params, got {plan_report_request_urls}")
    page.reload(wait_until="domcontentloaded")
    page.click('[data-report-section-button="plan"]')
    restored_plan_inputs = {
        "period": page.locator("#planReportPeriodSelect").input_value(),
        "h1": page.locator("#planReportH1Input").input_value(),
        "h2": page.locator("#planReportH2Input").input_value(),
        "drr": page.locator("#planReportDrrInput").input_value(),
        "contract_checkbox_count": page.locator("#planReportContractStartCheckbox").count(),
        "annual_even_checkbox_count": page.locator("#planReportAnnualEvenCheckbox").count(),
        "annual_even_checked": page.locator("#planReportAnnualEvenCheckbox").is_checked(),
        "contract_date_input_count": page.locator("#planReportContractStartDateInput").count(),
    }
    expected_restored_inputs = {
        "period": "current_month",
        "h1": "123456789",
        "h2": "234567890",
        "drr": "7.5",
        "contract_checkbox_count": 0,
        "annual_even_checkbox_count": 1,
        "annual_even_checked": True,
        "contract_date_input_count": 0,
    }
    if restored_plan_inputs != expected_restored_inputs:
        raise AssertionError(f"plan-report H1/H2/DRR/contract inputs must survive reload, got {restored_plan_inputs}")

    persisted_state = page.evaluate(
        """(storageKey) => {
            const raw = window.localStorage.getItem(storageKey);
            return raw ? JSON.parse(raw) : null;
        }""",
        STORAGE_KEY,
    )
    if not isinstance(persisted_state, dict):
        raise AssertionError("browser smoke must leave a structured persisted UI state in localStorage")
    if page_errors:
        raise AssertionError(f"operator UI emitted pageerror events: {page_errors}")
    if target_http_errors:
        raise AssertionError(
            "new WB-selection/registry UI requests failed: "
            f"{target_http_errors}"
        )

    context.close()
    return {
        "default_state": default_state,
        "top_tab_persistence": factory_state,
        "subsection_persistence": reports_state,
        "supplier_registry_refresh": supplier_refresh_state,
        "factory_source_persistence": factory_source_state,
        "regional_planning": regional_planning_state,
        "ff_stock_negative_row_style": ff_stock_negative_row_style,
        "ff_stock_operations_controls": ff_stock_operations_controls,
        "sku_persistence": {
            "kept_label": kept_label,
            "selected_labels_after_reload": selected_labels_after_reload,
            "storage_state": persisted_state,
        },
        "zero_selection_guard": validation_text.strip(),
        "plan_input_defaults": default_plan_inputs,
        "plan_input_persistence": restored_plan_inputs,
        "wb_supply_auto_default": {
            "initial": ["supply-A", "supply-B"],
            "after_manual_refresh": ["supply-B"],
            "fresh_page": ["supply-A", "supply-B"],
            "factory_request": calculate_requests[-1].get(
                "selected_wb_supply_ids"
            ),
            "regional_request": regional_requests[-1].get(
                "selected_wb_supply_ids"
            ),
        },
        "calculation_registry": {
            "record_id": "calc-browser-factory-registry",
            "selected_wb_supply_ids": ["supply-B"],
            "detail_visible": True,
        },
    }


def _run_fallback_scenario(context, base_url: str) -> dict[str, object]:
    page = context.new_page()
    operator_url = base_url + DEFAULT_SHEET_OPERATOR_UI_PATH
    page.goto(operator_url, wait_until="domcontentloaded")

    page.evaluate("(storageKey) => window.localStorage.setItem(storageKey, '{broken-json')", STORAGE_KEY)
    page.reload(wait_until="domcontentloaded")
    invalid_state = {
        "top_tab": _selected_data_attr(page, "[data-tab-button][aria-selected=\"true\"]", "data-tab-button"),
        "report_section": _selected_data_attr(
            page,
            "[data-report-section-button][aria-selected=\"true\"]",
            "data-report-section-button",
        ),
        "supply_section": _selected_data_attr(
            page,
            "[data-supply-section-button][aria-selected=\"true\"]",
            "data-supply-section-button",
        ),
    }
    if invalid_state != {"top_tab": "vitrina", "report_section": "daily", "supply_section": "factory"}:
        raise AssertionError(f"broken storage must fall back to default operator state, got {invalid_state}")

    page.click('[data-tab-button="reports"]')
    page.click('[data-report-section-button="stock"]')
    _open_stock_selector(page)
    if len(_checked_stock_selector_labels(page)) != len(_stock_selector_labels(page)):
        raise AssertionError("broken storage fallback must restore all active SKU as the default selector state")

    page.evaluate(
            """(storageKey) => {
                window.localStorage.setItem(storageKey, JSON.stringify({
                    version: 1,
                    active_tab: "reports",
                    report_section: "stock",
                    supply_section: "regional",
                    stock_report_selected_sku_ids: [999999],
                    stock_report_applied_sku_ids: [999999],
                    plan_report_inputs: {
                        period: "unsupported",
                        h1_buyout_plan_rub: "-1",
                        h2_buyout_plan_rub: "not-a-number",
                        plan_drr_pct: "",
                        use_contract_start_date: true,
                        annual_plan_evenly_distributed: "yes",
                        contract_start_date: "not-a-date"
                    }
                }));
            }""",
        STORAGE_KEY,
    )
    page.reload(wait_until="domcontentloaded")
    obsolete_state = {
        "top_tab": _selected_data_attr(page, "[data-tab-button][aria-selected=\"true\"]", "data-tab-button"),
        "report_section": _selected_data_attr(
            page,
            "[data-report-section-button][aria-selected=\"true\"]",
            "data-report-section-button",
        ),
    }
    if obsolete_state != {"top_tab": "reports", "report_section": "stock"}:
        raise AssertionError(f"valid persisted tab state must survive even when SKU ids become obsolete, got {obsolete_state}")
    _open_stock_selector(page)
    if len(_checked_stock_selector_labels(page)) != len(_stock_selector_labels(page)):
        raise AssertionError("obsolete persisted SKU ids must be dropped and replaced with the current default all-selected state")
    page.click('[data-report-section-button="plan"]')
    invalid_plan_restore = {
        "period": page.locator("#planReportPeriodSelect").input_value(),
        "h1": page.locator("#planReportH1Input").input_value(),
        "h2": page.locator("#planReportH2Input").input_value(),
        "drr": page.locator("#planReportDrrInput").input_value(),
        "contract_checkbox_count": page.locator("#planReportContractStartCheckbox").count(),
        "annual_even_checkbox_count": page.locator("#planReportAnnualEvenCheckbox").count(),
        "annual_even_checked": page.locator("#planReportAnnualEvenCheckbox").is_checked(),
        "contract_date_input_count": page.locator("#planReportContractStartDateInput").count(),
    }
    if invalid_plan_restore != {
        "period": "first_half",
        "h1": "155379879",
        "h2": "294620121",
        "drr": "6",
        "contract_checkbox_count": 0,
        "annual_even_checkbox_count": 1,
        "annual_even_checked": False,
        "contract_date_input_count": 0,
    }:
        raise AssertionError(f"invalid persisted plan inputs must fall back to WB/VB defaults, got {invalid_plan_restore}")

    context.close()
    return {
        "invalid_storage_fallback": invalid_state,
        "obsolete_sku_fallback": obsolete_state,
        "invalid_plan_input_fallback": invalid_plan_restore,
    }


def _plan_report_payload() -> dict[str, object]:
    metrics = {
        "buyout_rub": {
            "entity_key": "buyout_rub",
            "label": "Выкуп, руб.",
            "fact": 20000.0,
            "plan": 15000.0,
            "completion_pct": 133.33333333333331,
            "delta_abs": 5000.0,
            "delta_pct": 33.33333333333333,
            "status": "ok",
            "status_label": "выполнен",
        },
        "drr_pct": {
            "entity_key": "drr_pct",
            "label": "DRR, % (минимум по договору)",
            "fact": 10.0,
            "plan": 6.0,
            "completion_pct": None,
            "delta_pp": 4.0,
            "delta_pct": 66.66666666666666,
            "status": "ok",
            "status_label": "минимум выполнен",
        },
        "ads_sum_rub": {
            "entity_key": "ads_sum_rub",
            "label": "Рекламные расходы, руб.",
            "fact": 2000.0,
            "plan": 1200.0,
            "completion_pct": 166.66666666666669,
            "delta_abs": 800.0,
            "delta_pct": 66.66666666666666,
            "status": "ok",
            "status_label": "выполнен",
            "ads_plan_base_label": "фактический оборот 20 000",
            "ads_plan_base_rub": 20000.0,
            "ads_plan_base_mode": "fact_turnover_overperformance_base",
        },
    }
    block = {
        "label": "За первое полугодие",
        "date_from": "2026-02-01",
        "date_to": "2026-04-20",
        "day_count": 79,
        "status": "available",
        "reason": "Период обрезан по дате подписания: 2026-02-01.",
        "metrics": metrics,
        "coverage": {},
        "source_breakdown": {},
    }
    return {
        "status": "available",
        "selected_period_label": "За первое полугодие",
        "effective_as_of_date": "2026-04-20",
        "active_sku_count": len(ACTIVE_SKUS),
        "inputs": {
            "use_contract_start_date": True,
            "contract_start_date": "2026-02-01",
            "annual_plan_evenly_distributed": False,
        },
        "periods": {
            "selected_period": block,
            "month_to_date": {**block, "label": "С начала месяца", "date_from": "2026-04-01", "day_count": 20},
            "quarter_to_date": {**block, "label": "С начала квартала", "date_from": "2026-04-01", "day_count": 20},
            "year_to_date": {**block, "label": "С начала года", "date_from": "2026-02-01", "day_count": 79},
        },
        "contract_period_projection": {
            "label": "Прогноз к концу договорного периода при текущем темпе",
            "status": "available",
            "reason": "",
            "period_date_from": "2026-02-01",
            "period_date_to": "2026-12-31",
            "total_contract_day_count": 334,
            "elapsed_date_from": "2026-02-01",
            "elapsed_date_to": "2026-04-20",
            "elapsed_day_count": 79,
            "annual_buyout_plan_rub": 450000000.0,
            "usn_upper_limit_rub": 490500000.0,
            "annual_ads_plan_rub": 27000000.0,
            "plan_drr_pct": 6.0,
            "drr_minimum_pct": 6.0,
            "drr_requirement_type": "minimum",
            "fact_buyout_elapsed_rub": 100000000.0,
            "fact_ads_elapsed_rub": 7000000.0,
            "projected_buyout_rub": 422784810.12658226,
            "projected_buyout_pct_of_annual_plan": 93.95218002812939,
            "projected_buyout_pct_of_usn_upper_limit": 86.1946605762655,
            "projected_buyout_remaining_to_usn_upper_limit_rub": 67715189.87341774,
            "projected_buyout_exceeds_usn_upper_limit": False,
            "projected_ads_sum_rub": 29594936.70886076,
            "projected_ads_pct_of_annual_ads_plan": 109.61087669948429,
            "projected_drr_pct": 7.0,
            "projected_drr_margin_to_minimum_pp": 1.0,
            "projected_drr_minimum_met": True,
            "fact_is_partial": False,
            "coverage": {"fact_is_partial": False, "missing_dates": []},
            "source_mix": {},
            "source_breakdown": {},
        },
        "baseline": {"status": "missing", "months": []},
        "notes": [],
    }


def _open_stock_selector(page) -> None:
    page.locator("#stockReportSkuSelector").evaluate("(element) => { element.open = true; }")
    page.wait_for_function("() => document.querySelectorAll('#stockReportSkuList input[type=\"checkbox\"]').length > 0")


def _stock_selector_labels(page) -> list[str]:
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('#stockReportSkuList .stock-selector-option'))
            .map((item) => item.textContent.trim())
            .filter(Boolean)"""
    )


def _checked_stock_selector_labels(page) -> list[str]:
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('#stockReportSkuList .stock-selector-option'))
            .filter((item) => {
                const input = item.querySelector('input[type="checkbox"]');
                return Boolean(input && input.checked);
            })
            .map((item) => item.textContent.trim())
            .filter(Boolean)"""
    )


def _visible_stock_report_titles(page) -> list[str]:
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('#stockReportRows .stock-report-table tbody tr td:first-child'))
            .map((item) => item.textContent.trim())
            .filter((item) => item && item !== 'Итого')"""
    )


def _wait_for_row_titles(page, expected_titles: list[str]) -> None:
    deadline = time.time() + 10
    while time.time() < deadline:
        actual_titles = _visible_stock_report_titles(page)
        if actual_titles == expected_titles:
            return
        time.sleep(0.1)
    raise AssertionError(f"expected stock-report titles {expected_titles}, got {_visible_stock_report_titles(page)}")


def _wait_for_request_count(request_urls: list[str], expected_count: int) -> None:
    deadline = time.time() + 10
    while time.time() < deadline:
        if len(request_urls) >= expected_count:
            return
        time.sleep(0.1)
    raise AssertionError(f"expected at least {expected_count} plan-report requests, got {request_urls}")


def _selected_data_attr(page, selector: str, attribute_name: str) -> str:
    locator = page.locator(selector)
    if locator.count() != 1:
        raise AssertionError(f"expected one selected element for {selector}, got {locator.count()}")
    value = locator.first.get_attribute(attribute_name)
    if not value:
        raise AssertionError(f"selected element for {selector} must expose {attribute_name}")
    return value


def _assert_ff_stock_negative_row_dark_style(page) -> dict[str, object]:
    style = page.evaluate(
        """() => {
            const body = document.getElementById("ffStockBalancesBody");
            if (!body) {
                return {missing: true};
            }
            body.innerHTML = [
                "<tr class=\\"is-warning\\">",
                "<td>4600000000000</td>",
                "<td>1001</td>",
                "<td>SKU Alpha</td>",
                "<td>Clear</td>",
                "<td>-10</td>",
                "<td>Отрицательный остаток ФФ</td>",
                "</tr>"
            ].join("");
            const cell = body.querySelector("tr.is-warning td");
            const computed = cell ? window.getComputedStyle(cell) : null;
            return {
                missing: false,
                backgroundColor: computed ? computed.backgroundColor : "",
                color: computed ? computed.color : ""
            };
        }"""
    )
    if style.get("missing"):
        raise AssertionError("FF stock balances table must be present in operator UI")
    rgb = _parse_rgb_triplet(str(style.get("backgroundColor") or ""))
    if rgb is None:
        raise AssertionError(f"negative FF stock row must have computed background color, got {style}")
    if min(rgb) >= 230:
        raise AssertionError(f"negative FF stock row must not use light/white background, got {style}")
    return style


def _assert_ff_stock_operations_controls(page) -> dict[str, object]:
    controls = page.evaluate(
        """() => {
            const pageSize = document.getElementById("ffStockOperationsPageSizeSelect");
            const archiveToggle = document.getElementById("ffStockShowTechnicalArchiveToggle");
            const prevButton = document.getElementById("ffStockOperationsPrevButton");
            const nextButton = document.getElementById("ffStockOperationsNextButton");
            const pageInfo = document.getElementById("ffStockOperationsPageInfo");
            const archiveHint = document.getElementById("ffStockOperationsArchiveHint");
            return {
                hasPageSize: Boolean(pageSize),
                pageSizeOptions: pageSize ? Array.from(pageSize.options).map((option) => option.value) : [],
                pageSizeDefault: pageSize ? pageSize.value : "",
                hasArchiveToggle: Boolean(archiveToggle),
                archiveToggleChecked: archiveToggle ? archiveToggle.checked : null,
                prevText: prevButton ? prevButton.textContent.trim() : "",
                nextText: nextButton ? nextButton.textContent.trim() : "",
                pageInfoText: pageInfo ? pageInfo.textContent.trim() : "",
                hasArchiveHint: Boolean(archiveHint)
            };
        }"""
    )
    if not controls.get("hasPageSize") or controls.get("pageSizeOptions") != ["50", "100", "200"]:
        raise AssertionError(f"FF stock operations page-size control must expose 50/100/200, got {controls}")
    if controls.get("pageSizeDefault") != "50":
        raise AssertionError(f"FF stock operations page-size default must be 50, got {controls}")
    if not controls.get("hasArchiveToggle") or controls.get("archiveToggleChecked") is not False:
        raise AssertionError(f"FF stock operations archive toggle must default off in UI, got {controls}")
    if controls.get("prevText") != "Назад" or controls.get("nextText") != "Вперёд":
        raise AssertionError(f"FF stock operations pagination buttons changed, got {controls}")
    if "Страница" not in str(controls.get("pageInfoText") or "") or not controls.get("hasArchiveHint"):
        raise AssertionError(f"FF stock operations page info/archive hint missing, got {controls}")
    return controls


def _parse_rgb_triplet(value: str) -> tuple[int, int, int] | None:
    if not value.startswith("rgb"):
        return None
    start = value.find("(")
    end = value.find(")", start + 1)
    if start < 0 or end < 0:
        return None
    parts = [part.strip() for part in value[start + 1 : end].split(",")]
    if len(parts) < 3:
        return None
    try:
        return int(float(parts[0])), int(float(parts[1])), int(float(parts[2]))
    except ValueError:
        return None


def _nm_id_from_label(label: str) -> str:
    marker = "nmId "
    if marker not in label:
        raise AssertionError(f"stock-report label must contain nmId marker, got {label!r}")
    return label.split(marker, 1)[1].strip()


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _is_loopback_base_url(base_url: str) -> bool:
    parsed = urllib_parse.urlparse(base_url)
    host = (parsed.hostname or "").strip().lower()
    return host in {"127.0.0.1", "::1", "localhost"}


def _print_summary(result: dict[str, object]) -> None:
    print("operator_ui_persistence_base: ok ->", result["base_url"])
    print(
        "operator_ui_tabs: ok ->",
        result["top_tab_persistence"],
        result["subsection_persistence"],
    )
    print("operator_ui_supplier_registry_refresh: ok ->", result["supplier_registry_refresh"])
    print("operator_ui_sku_restore: ok ->", result["sku_persistence"])
    print("operator_ui_factory_source_restore: ok ->", result["factory_source_persistence"])
    print("operator_ui_regional_planning: ok ->", result["regional_planning"])
    print("operator_ui_ff_stock_negative_row_style: ok ->", result["ff_stock_negative_row_style"])
    print("operator_ui_ff_stock_operations_controls: ok ->", result["ff_stock_operations_controls"])
    print("operator_ui_plan_input_defaults: ok ->", result["plan_input_defaults"])
    print("operator_ui_plan_input_restore: ok ->", result["plan_input_persistence"])
    print("operator_ui_zero_guard: ok ->", result["zero_selection_guard"])
    print(
        "operator_ui_storage_fallback: ok ->",
        result["invalid_storage_fallback"],
        result["obsolete_sku_fallback"],
        result["invalid_plan_input_fallback"],
    )


if __name__ == "__main__":
    main()
