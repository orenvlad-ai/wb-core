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

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_FACTORY_ORDER_STATUS_PATH,
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
                json.dumps(
                    {
                        "active_sku_count": len(ACTIVE_SKUS),
                        "coverage_contract_note": "-",
                        "datasets": {},
                        "last_result": None,
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

        handler_cls = _build_handler(payloads)
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


def _build_handler(payloads: dict[str, tuple[str, bytes, HTTPStatus]]):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            payload = payloads.get(self.path)
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
            "sku_persistence": persistence_result["sku_persistence"],
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

    page.click('[data-tab-button="reports"]')
    page.click('[data-report-section-button="stock"]')
    page.wait_for_function(
        "() => document.querySelectorAll('#stockReportRows .report-list-title, #stockReportRows .report-empty').length > 0"
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

    page.wait_for_function("() => document.querySelectorAll('#stockReportRows .report-list-title').length > 0")
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

    page.click('[data-report-section-button="plan"]')
    page.select_option("#planReportPeriodSelect", "first_half")
    page.fill("#planReportH1Input", "155379879")
    page.fill("#planReportH2Input", "294620120")
    page.fill("#planReportDrrInput", "6")
    page.check("#planReportContractStartCheckbox")
    page.fill("#planReportContractStartDateInput", "2026-02-01")
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
    page.click("#planReportApplyButton")
    page.wait_for_function(
        """(storageKey) => {
            const raw = window.localStorage.getItem(storageKey);
            if (!raw) return false;
            const parsed = JSON.parse(raw);
            return parsed.plan_report_inputs &&
                parsed.plan_report_inputs.period === "first_half" &&
                parsed.plan_report_inputs.h1_buyout_plan_rub === "155379879" &&
                parsed.plan_report_inputs.h2_buyout_plan_rub === "294620120" &&
                parsed.plan_report_inputs.plan_drr_pct === "6" &&
                parsed.plan_report_inputs.use_contract_start_date === true &&
                parsed.plan_report_inputs.contract_start_date === "2026-02-01";
        }""",
        arg=STORAGE_KEY,
    )
    latest_plan_report_url = plan_report_request_urls[-1] if plan_report_request_urls else ""
    if "use_contract_start_date=true" not in latest_plan_report_url or "contract_start_date=2026-02-01" not in latest_plan_report_url:
        raise AssertionError(f"plan-report request must include contract start params when enabled, got {plan_report_request_urls}")
    page.reload(wait_until="domcontentloaded")
    page.click('[data-report-section-button="plan"]')
    restored_plan_inputs = {
        "period": page.locator("#planReportPeriodSelect").input_value(),
        "h1": page.locator("#planReportH1Input").input_value(),
        "h2": page.locator("#planReportH2Input").input_value(),
        "drr": page.locator("#planReportDrrInput").input_value(),
        "use_contract_start_date": page.locator("#planReportContractStartCheckbox").is_checked(),
        "contract_start_date": page.locator("#planReportContractStartDateInput").input_value(),
        "contract_date_disabled": page.locator("#planReportContractStartDateInput").is_disabled(),
    }
    expected_restored_inputs = {
        "period": "first_half",
        "h1": "155379879",
        "h2": "294620120",
        "drr": "6",
        "use_contract_start_date": True,
        "contract_start_date": "2026-02-01",
        "contract_date_disabled": False,
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
            "sku_persistence": {
                "kept_label": kept_label,
                "selected_labels_after_reload": selected_labels_after_reload,
                "storage_state": persisted_state,
            },
            "zero_selection_guard": validation_text.strip(),
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
        "use_contract_start_date": page.locator("#planReportContractStartCheckbox").is_checked(),
        "contract_start_date": page.locator("#planReportContractStartDateInput").input_value(),
        "contract_date_disabled": page.locator("#planReportContractStartDateInput").is_disabled(),
    }
    if invalid_plan_restore != {
        "period": "current_month",
        "h1": "",
        "h2": "",
        "drr": "",
        "use_contract_start_date": False,
        "contract_start_date": "",
        "contract_date_disabled": True,
    }:
        raise AssertionError(f"invalid persisted plan inputs must be ignored safely, got {invalid_plan_restore}")

    context.close()
    return {
        "invalid_storage_fallback": invalid_state,
        "obsolete_sku_fallback": obsolete_state,
        "invalid_plan_input_fallback": invalid_plan_restore,
    }


def _plan_report_payload() -> dict[str, object]:
    block = {
        "label": "За первое полугодие",
        "date_from": "2026-02-01",
        "date_to": "2026-04-20",
        "day_count": 79,
        "status": "available",
        "reason": "Период обрезан по дате подписания: 2026-02-01.",
        "metrics": {},
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
        },
        "periods": {
            "selected_period": block,
            "month_to_date": {**block, "label": "С начала месяца", "date_from": "2026-04-01", "day_count": 20},
            "quarter_to_date": {**block, "label": "С начала квартала", "date_from": "2026-04-01", "day_count": 20},
            "year_to_date": {**block, "label": "С начала года", "date_from": "2026-02-01", "day_count": 79},
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
        """() => Array.from(document.querySelectorAll('#stockReportRows .report-list-title'))
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
    print("operator_ui_sku_restore: ok ->", result["sku_persistence"])
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
