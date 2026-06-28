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
    DEFAULT_WB_REGIONAL_CALCULATE_PATH,
    DEFAULT_WB_REGIONAL_STATUS_PATH,
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
    args = parser.parse_args()

    if args.base_url:
        result = run_browser_checks(args.base_url.rstrip("/"), ignore_https_errors=args.ignore_https_errors)
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
                        },
                        "row_count": 3,
                        "rows": [
                            {
                                "nm_id": 1001,
                                "display_name": "SKU Alpha",
                                "identity_label": "SKU Alpha · nmId 1001",
                                "stock_total": 21.0,
                                "breached_districts": [{"metric_key": "stock_ru_central", "label": "Центральный ФО", "stock": 21.0}],
                                "breached_district_count": 1,
                                "min_breached_stock": 21.0,
                            },
                            {
                                "nm_id": 1002,
                                "display_name": "SKU Beta",
                                "identity_label": "SKU Beta · nmId 1002",
                                "stock_total": 13.0,
                                "breached_districts": [{"metric_key": "stock_ru_central", "label": "Центральный ФО", "stock": 13.0}],
                                "breached_district_count": 1,
                                "min_breached_stock": 13.0,
                            },
                            {
                                "nm_id": 1003,
                                "display_name": "SKU Gamma",
                                "identity_label": "SKU Gamma · nmId 1003",
                                "stock_total": 7.0,
                                "breached_districts": [{"metric_key": "stock_ru_central", "label": "Центральный ФО", "stock": 7.0}],
                                "breached_district_count": 1,
                                "min_breached_stock": 7.0,
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
        "sku_persistence": persistence_result["sku_persistence"],
        "plan_input_defaults": persistence_result["plan_input_defaults"],
        "plan_input_persistence": persistence_result["plan_input_persistence"],
        "zero_selection_guard": persistence_result["zero_selection_guard"],
        "invalid_storage_fallback": fallback_result["invalid_storage_fallback"],
        "obsolete_sku_fallback": fallback_result["obsolete_sku_fallback"],
        "invalid_plan_input_fallback": fallback_result["invalid_plan_input_fallback"],
    }


def _run_persistence_scenario(context, base_url: str) -> dict[str, object]:
    page = context.new_page()
    operator_url = base_url + DEFAULT_SHEET_OPERATOR_UI_PATH
    page.goto(operator_url, wait_until="domcontentloaded")

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

    def _capture_factory_calculate(route) -> None:
        body = route.request.post_data or "{}"
        calculate_requests.append(json.loads(body))
        route.fulfill(
            status=200,
            content_type="application/json; charset=utf-8",
            body=json.dumps(
                {
                    "status": "success",
                    "report_date": "2026-04-20",
                    "horizon_days": 45,
                    "target_window_days": 74,
                    "inbound_window_end": "2026-07-03",
                    "factory_inbound_source": "supplier_registry",
                    "stock_ff_source": "onec_ff_stock",
                    "settings": {
                        "factory_inbound_source": "supplier_registry",
                        "stock_ff_source": "onec_ff_stock",
                    },
                    "summary": {"total_qty": 0, "estimated_weight": 0.0, "estimated_volume": 0.0},
                    "warnings": [],
                    "recommendation_download_path": "/v1/sheet-vitrina-v1/supply/factory-order/recommendation.xlsx",
                },
                ensure_ascii=False,
            ),
        )

    page.route("**" + DEFAULT_FACTORY_ORDER_CALCULATE_PATH, _capture_factory_calculate)
    page.click("#calculateFactoryOrderButton")
    page.wait_for_function("() => document.getElementById('factoryMessage') && document.getElementById('factoryMessage').textContent.includes('Расчёт завершён')")
    if not calculate_requests or calculate_requests[-1].get("stock_ff_source") != "onec_ff_stock":
        raise AssertionError(f"factory calculate payload must include selected stock_ff_source, got {calculate_requests}")
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
    if page.locator('input[name="regionalIncludedDistrict"]').count() != 6:
        raise AssertionError("regional district selector must render six district checkboxes")
    if page.locator('input[name="regionalIncludedDistrict"]:checked').count() != 6:
        raise AssertionError("regional district selector must default to all districts")
    page.click("#regionalDistrictExcludeFarSiberiaButton")
    page.wait_for_function(
        """(storageKey) => {
            const raw = window.localStorage.getItem(storageKey);
            if (!raw) return false;
            const parsed = JSON.parse(raw);
            return Array.isArray(parsed.wb_regional_included_district_keys) &&
                parsed.wb_regional_included_district_keys.length === 5 &&
                !parsed.wb_regional_included_district_keys.includes("far_siberia");
        }""",
        arg=STORAGE_KEY,
    )
    regional_requests: list[dict[str, object]] = []
    regional_result_payload: dict[str, object] = {}

    def _capture_regional_status(route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json; charset=utf-8",
            body=json.dumps(
                {
                    "active_sku_count": len(ACTIVE_SKUS),
                    "methodology_note": "-",
                    "shared_datasets": {},
                    "last_result": regional_result_payload or None,
                },
                ensure_ascii=False,
            ),
        )

    def _capture_regional_calculate(route) -> None:
        nonlocal regional_result_payload
        body = route.request.post_data or "{}"
        regional_requests.append(json.loads(body))
        regional_result_payload = {
            "status": "success",
            "report_date": "2026-04-20",
            "horizon_days": 7,
            "active_sku_count": 33,
            "methodology_note": "test methodology note",
            "settings": {
                "included_district_keys": ["central", "northwest", "volga", "ural", "south_caucasus"],
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
                "included_district_keys": ["central", "northwest", "volga", "ural", "south_caucasus"],
                "excluded_district_keys": ["far_siberia"],
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
                    "district_key": "central",
                    "district_name_ru": "Центральный федеральный округ",
                    "total_qty": 100,
                    "deficit_qty": 10,
                    "filename": "wb_regional_central_fo.xlsx",
                    "download_path": "/v1/sheet-vitrina-v1/supply/wb-regional/district/central.xlsx",
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
                    "total_qty": 300,
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

    page.route("**" + DEFAULT_WB_REGIONAL_STATUS_PATH, _capture_regional_status)
    page.route("**" + DEFAULT_WB_REGIONAL_CALCULATE_PATH, _capture_regional_calculate)
    page.click("#calculateRegionalSupplyButton")
    page.wait_for_function("() => document.getElementById('regionalMessage') && document.getElementById('regionalMessage').textContent.includes('Расчёт выполнен')")
    if not regional_requests or regional_requests[-1].get("included_district_keys") != ["central", "northwest", "volga", "ural", "south_caucasus"]:
        raise AssertionError(f"regional calculate payload must include selected districts, got {regional_requests}")
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
    if district_rows.count() != 5:
        raise AssertionError(f"regional district table must show only included districts, got {district_rows.count()} rows")
    district_table_text = page.locator("#regionalDistrictTableBody").inner_text()
    if "Дальневосточный и Сибирский" in district_table_text or "far_siberia" in district_table_text:
        raise AssertionError(f"excluded far_siberia must not be visible in summary/download table, got {district_table_text!r}")
    if not page.locator('input[name="regionalIncludedDistrict"][value="far_siberia"]').count():
        raise AssertionError("excluded far_siberia must remain available in selector options")
    if page.locator('input[name="regionalIncludedDistrict"][value="far_siberia"]').is_checked():
        raise AssertionError("far_siberia selector checkbox must stay unchecked after exclusion")
    if page.locator("#downloadRegionalRecommendationsZipButton").is_disabled():
        raise AssertionError("regional ZIP download button must be enabled after successful result")
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
        "Исключённые округа: Дальневосточный и Сибирский федеральный округ",
        "тестовая поставка",
    ):
        if expected not in details_text:
            raise AssertionError(f"regional diagnostics details must include {expected!r}, got {details_text!r}")
    for technical in ("partial_district_observations", "district_zero_zero_no_signal", "district_restock_or_upward_correction", "seed_floor"):
        if technical in details_text:
            raise AssertionError(f"technical code {technical!r} must be translated in visible diagnostics details")
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

    context.close()
    return {
        "default_state": default_state,
        "top_tab_persistence": factory_state,
        "subsection_persistence": reports_state,
        "supplier_registry_refresh": supplier_refresh_state,
        "factory_source_persistence": factory_source_state,
        "sku_persistence": {
            "kept_label": kept_label,
            "selected_labels_after_reload": selected_labels_after_reload,
            "storage_state": persisted_state,
        },
        "zero_selection_guard": validation_text.strip(),
        "plan_input_defaults": default_plan_inputs,
        "plan_input_persistence": restored_plan_inputs,
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
            "label": "DRR, %",
            "fact": 10.0,
            "plan": 6.0,
            "completion_pct": None,
            "delta_pp": 4.0,
            "delta_pct": 66.66666666666666,
            "status": "alert",
            "status_label": "выше плана",
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
            "annual_ads_plan_rub": 27000000.0,
            "fact_buyout_elapsed_rub": 100000000.0,
            "fact_ads_elapsed_rub": 6000000.0,
            "projected_buyout_rub": 422784810.12658226,
            "projected_buyout_pct_of_annual_plan": 93.95218002812939,
            "projected_ads_sum_rub": 25367088.607594937,
            "projected_ads_pct_of_annual_ads_plan": 93.95218002812939,
            "projected_drr_pct": 6.0,
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
            .filter(Boolean)"""
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


def _nm_id_from_label(label: str) -> str:
    marker = "nmId "
    if marker not in label:
        raise AssertionError(f"stock-report label must contain nmId marker, got {label!r}")
    return label.split(marker, 1)[1].strip()


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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
