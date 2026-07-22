#!/usr/bin/env python3
"""Desktop and 390px Playwright smoke for the UI-first Partner Report."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

from openpyxl import Workbook
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_PARTNER_REPORT_OPTIONS_PATH,
    DEFAULT_PARTNER_REPORT_PREVIEW_PATH,
    DEFAULT_PARTNER_REPORT_PREVIEW_XLSX_PATH,
    DEFAULT_PARTNER_REPORT_SETTINGS_PATH,
    _render_sheet_vitrina_operator_ui,
)


def main() -> None:
    port = _reserve_free_port()
    html = _render_sheet_vitrina_operator_ui(
        daily_report_path="/daily",
        stock_report_path="/stock",
        plan_report_path="/plan",
        wb_finance_report_path="/finance",
        refresh_path="/refresh",
        load_path="/load",
        status_path="/status",
        job_path="/job",
        embedded_tab="reports",
    )
    requests: list[dict] = []
    preview_calls = {"count": 0}
    workbook = _workbook_fixture()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/operator"):
                self._write(200, "text/html; charset=utf-8", html.encode("utf-8"))
            elif self.path == DEFAULT_PARTNER_REPORT_OPTIONS_PATH:
                self._json(200, _options())
            else:
                self._json(200, {})

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            requests.append({"path": self.path, "body": body})
            if self.path == DEFAULT_PARTNER_REPORT_SETTINGS_PATH:
                self._json(
                    200,
                    {
                        "settings_version_id": "prs_fixture",
                        "nm_id": "101101",
                        "parameters": body,
                        "fingerprint": "sha256:settings",
                    },
                )
            elif self.path == DEFAULT_PARTNER_REPORT_PREVIEW_PATH:
                preview_calls["count"] += 1
                if preview_calls["count"] == 2:
                    time.sleep(0.6)
                self._json(
                    200,
                    _incomplete_report() if preview_calls["count"] == 4 else _report(),
                )
            elif self.path == DEFAULT_PARTNER_REPORT_PREVIEW_XLSX_PATH:
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
                self.send_header(
                    "Content-Disposition",
                    "attachment; filename*=UTF-8''Partner_101101_2026-07-06_2026-07-19.xlsx",
                )
                self.send_header("Content-Length", str(len(workbook)))
                self.end_headers()
                self.wfile.write(workbook)
            else:
                self._json(404, {"error": "not found"})

        def log_message(self, format: str, *args: object) -> None:
            return

        def _json(self, status: int, payload: dict) -> None:
            try:
                self._write(
                    status,
                    "application/json; charset=utf-8",
                    json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                )
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _write(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    console_errors: list[str] = []
    page_errors: list[str] = []
    failed_responses: list[str] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on("response", lambda response: failed_responses.append(f"{response.status} {response.url}") if response.status >= 500 else None)
            response = page.goto(f"http://127.0.0.1:{port}/operator", wait_until="networkidle")
            if response is None or response.status != 200:
                raise AssertionError("operator document did not load")
            page.locator('[data-report-section-button="partner"]').click()
            page.locator("#partnerReportControls").wait_for(state="visible")
            if any(
                page.locator(selector).input_value()
                for selector in (
                    "#partnerReportShare",
                    "#partnerReportCapital",
                    "#partnerReportReserve",
                    "#partnerReportOffice",
                    "#partnerReportTax",
                )
            ):
                raise AssertionError("unsaved business parameters received hidden defaults")
            for selector, value in {
                "#partnerReportShare": "40",
                "#partnerReportCapital": "500000",
                "#partnerReportReserve": "20",
                "#partnerReportOffice": "10000",
                "#partnerReportTax": "6",
            }.items():
                page.locator(selector).fill(value)
            page.locator("#partnerReportSaveSettings").click()
            page.wait_for_function("document.getElementById('partnerReportStatus').innerText.includes('Настройки сохранены')")

            page.locator("#partnerReportGenerate").click()
            loading = page.locator("#partnerReportStatus").inner_text()
            if "Формируем preview" not in loading:
                raise AssertionError(f"visible loading state was not immediate: {loading}")
            page.locator("#partnerReportTableWrap table").wait_for(state="visible")
            page.locator(".partner-report-tooltip-anchor").focus()
            facts = page.evaluate(
                r"""
                () => {
                  const wrap = document.getElementById('partnerReportTableWrap');
                  const table = wrap.querySelector('table');
                  const dividends = [...table.rows].find((row) => row.cells[0] && row.cells[0].innerText.trim() === 'Дивиденды');
                  const blockers = document.getElementById('partnerReportBlockers');
                  const other = table.querySelector('[data-partner-row-key="other_direct_and_allocated_expenses"]');
                  const categories = [...table.querySelectorAll('[data-partner-expense-category]')];
                  const tooltip = document.querySelector('.partner-report-tooltip');
                  return {
                    heading: document.querySelector('[data-report-section-panel="partner"] h2').innerText,
                    header: [...table.tHead.rows[0].cells].map((cell) => cell.innerText.trim()),
                    dividends: [...dividends.cells].map((cell) => cell.innerText.trim().replace(/\s/g, ' ')),
                    sticky: getComputedStyle(table.rows[1].cells[0]).position,
                    downloadEnabled: !document.getElementById('partnerReportDownload').disabled,
                    pageOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
                    blockersAboveTable: blockers.getBoundingClientRect().top < wrap.getBoundingClientRect().top,
                    hasFinalize: Boolean(document.getElementById('partnerReportFinalize')),
                    notes: document.getElementById('partnerReportNotes').innerText,
                    otherLabel: other && other.querySelector('.partner-report-tooltip-wrap > span:first-child').innerText.trim(),
                    categoryCount: categories.length,
                    categoryPercentCount: categories.filter((row) => row.innerText.includes('%')).length,
                    oldLabelCount: [...table.querySelectorAll('td')].filter((cell) => cell.innerText.trim() === 'Прочие атрибутируемые расходы').length,
                    tooltipVisible: Boolean(tooltip && getComputedStyle(tooltip).display !== 'none'),
                  };
                }
                """
            )
            if (
                facts["heading"].casefold() != "отчёт о доходности карточки"
                or facts["header"][-1] != "Итого за период"
                or "44 253,90" not in facts["dividends"][1]
                or "49 053,90" not in facts["dividends"][-1]
                or facts["sticky"] != "sticky"
                or not facts["downloadEnabled"]
                or facts["pageOverflow"]
                or not facts["blockersAboveTable"]
                or facts["hasFinalize"]
                or "source digest" not in facts["notes"]
                or facts["otherLabel"] != "Прочие прямые и распределённые расходы"
                or facts["categoryCount"] != 4
                or facts["categoryPercentCount"] != 0
                or facts["oldLabelCount"] != 0
                or not facts["tooltipVisible"]
            ):
                raise AssertionError(f"desktop Partner Report contract mismatch: {facts}")

            # Editing any source parameter invalidates the actionable preview.
            page.locator("#partnerReportShare").fill("41")
            stale = page.evaluate(
                """
                () => ({
                  tableHidden: document.getElementById('partnerReportTableWrap').hidden,
                  downloadDisabled: document.getElementById('partnerReportDownload').disabled,
                })
                """
            )
            if stale != {"tableHidden": True, "downloadDisabled": True}:
                raise AssertionError(f"edited parameters left stale preview actionable: {stale}")

            # AbortController/cancel must give an explicit result, not an empty wait.
            page.locator("#partnerReportGenerate").click()
            page.locator("#partnerReportCancel").wait_for(state="visible")
            page.locator("#partnerReportCancel").click()
            page.wait_for_function("document.getElementById('partnerReportStatus').innerText.includes('отменено')")
            if not page.locator("#partnerReportDownload").is_disabled():
                raise AssertionError("cancelled preview left Excel enabled")

            page.locator("#partnerReportGenerate").click()
            page.locator("#partnerReportTableWrap table").wait_for(state="visible")
            with page.expect_download() as download:
                page.locator("#partnerReportDownload").click()
            if download.value.suggested_filename != "Partner_101101_2026-07-06_2026-07-19.xlsx":
                raise AssertionError(f"preview XLSX filename mismatch: {download.value.suggested_filename}")

            page.locator("#partnerReportGenerate").click()
            page.wait_for_function(
                "document.getElementById('partnerReportStatus').innerText.includes('source blockers')"
            )
            incomplete = page.evaluate(
                """
                () => ({
                  blocker: document.getElementById('partnerReportBlockers').innerText,
                  tableVisible: !document.getElementById('partnerReportTableWrap').hidden,
                  downloadDisabled: document.getElementById('partnerReportDownload').disabled,
                })
                """
            )
            if (
                "Не хватает канонической себестоимости" not in incomplete["blocker"]
                or "nmId 101101" not in incomplete["blocker"]
                or "canonical_cost_exact_date_missing" not in incomplete["blocker"]
                or not incomplete["tableVisible"]
                or not incomplete["downloadDisabled"]
            ):
                raise AssertionError(f"incomplete Partner preview UX mismatch: {incomplete}")

            page.set_viewport_size({"width": 390, "height": 844})
            narrow = page.evaluate(
                """
                () => {
                  const controls = document.getElementById('partnerReportControls');
                  const settings = controls.querySelector('.partner-report-settings');
                  const wrap = document.getElementById('partnerReportTableWrap');
                  return {
                    pageOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
                    settingsColumns: getComputedStyle(settings).gridTemplateColumns.split(' ').length,
                    localTableScroll: wrap.scrollWidth > wrap.clientWidth,
                    controlsWithinViewport: controls.getBoundingClientRect().right <= document.documentElement.clientWidth + 1,
                    tableVisible: !wrap.hidden && wrap.getBoundingClientRect().height > 0,
                  };
                }
                """
            )
            if narrow != {
                "pageOverflow": False,
                "settingsColumns": 1,
                "localTableScroll": True,
                "controlsWithinViewport": True,
                "tableVisible": True,
            }:
                raise AssertionError(f"390px Partner Report layout mismatch: {narrow}")
            screenshot_path = str(os.environ.get("PARTNER_REPORT_BROWSER_SMOKE_SCREENSHOT") or "").strip()
            if screenshot_path:
                page.screenshot(path=screenshot_path, full_page=True)
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)

    expected_paths = [
        DEFAULT_PARTNER_REPORT_SETTINGS_PATH,
        DEFAULT_PARTNER_REPORT_PREVIEW_PATH,
        DEFAULT_PARTNER_REPORT_PREVIEW_PATH,
        DEFAULT_PARTNER_REPORT_PREVIEW_PATH,
        DEFAULT_PARTNER_REPORT_PREVIEW_XLSX_PATH,
        DEFAULT_PARTNER_REPORT_PREVIEW_PATH,
    ]
    if [item["path"] for item in requests] != expected_paths:
        raise AssertionError(f"Partner Report HTTP sequence mismatch: {requests}")
    excel_request = next(
        item for item in requests if item["path"] == DEFAULT_PARTNER_REPORT_PREVIEW_XLSX_PATH
    )
    if excel_request["body"].get("expected_source_digest") != "sha256:" + "f" * 64:
        raise AssertionError("Excel request was not bound to the visible preview digest")
    if console_errors or page_errors or failed_responses:
        raise AssertionError(
            f"Partner UI errors: console={console_errors}, page={page_errors}, network={failed_responses}"
        )
    print(
        "partner_report_browser: ok -> immediate loading, cancel/error safety, UI-first preview, "
        "digest-bound XLSX, no finalization/ZIP, desktop+390px layout"
    )


def _options() -> dict:
    return {
        "status": "ok",
        "cards": [
            {
                "nm_id": "101101",
                "product_name": "Выбранный товар",
                "vendor_code": "VC101",
                "barcode": "BAR101",
                "is_active": True,
                "is_hidden": False,
                "settings": None,
            }
        ],
        "weeks": [
            {"week_start": "2026-07-06", "week_end": "2026-07-12", "status": "completed"},
            {"week_start": "2026-07-13", "week_end": "2026-07-19", "status": "completed"},
        ],
        "common_expense_rules": [
            {"value": "net_revenue_share", "label": "По доле чистой выручки карточки"}
        ],
    }


def _report() -> dict:
    first = _values("476034", "44253.904", "230.1203")
    second = _values("200000", "4800", "49.92")
    totals = _values("676034", "49053.904", "127.5402")
    return {
        "status": "ready",
        "formula_version": "partner_report_profitability_ui_first_v3",
        "source_digest": "sha256:" + "f" * 64,
        "annualized_return_formula": "Средние недельные дивиденды × 52 / вложенный капитал × 100%. Расчётная, не гарантированная доходность.",
        "weeks": [
            {"week_start": "2026-07-06", "week_end": "2026-07-12", "label": "06.07–12.07", "values": first, "other_expense_breakdown": _breakdown()},
            {"week_start": "2026-07-13", "week_end": "2026-07-19", "label": "13.07–19.07", "values": second, "other_expense_breakdown": _breakdown()},
        ],
        "totals": totals,
        "other_expense_breakdown_total": _breakdown(),
        "other_expense_tooltip": "Итог включает прямые расходы выбранного SKU и распределённую долю общих расходов кабинета. Распределённая доля рассчитывается как общие неатрибутированные расходы недели × чистая выручка SKU / общая положительная чистая выручка недели.",
        "blockers": [],
    }


def _incomplete_report() -> dict:
    report = _report()
    report["status"] = "incomplete"
    report["weeks"][0]["values"]["cogs"] = None
    report["weeks"][0]["values"]["finance_margin"] = None
    report["weeks"][0]["values"]["net_profit"] = None
    report["weeks"][0]["values"]["dividends"] = None
    report["weeks"][0]["values"]["annualized_return_pct"] = None
    report["blockers"] = [
        {
            "code": "partner_cost_coverage_incomplete",
            "week_start": "2026-07-06",
            "problem_skus": [
                {
                    "nm_id": "101101",
                    "operation_date": "2026-07-07",
                    "reason": "canonical_cost_exact_date_missing",
                }
            ],
        }
    ]
    return report


def _values(net_revenue: str, dividends: str, annualized: str) -> dict:
    return {
        "net_revenue": net_revenue,
        "cogs": "83837",
        "agent_remuneration": "160000",
        "acquiring": "14797",
        "logistics": "0",
        "storage": "0",
        "acceptance": "0",
        "ads": "30904",
        "penalties_and_adjustments": "0",
        "other_direct_and_allocated_expenses": "0",
        "finance_margin": "186496",
        "office": "10000",
        "estimated_tax": "28562.04",
        "replenishment_reserve": "37299.2",
        "net_profit": "110634.76",
        "dividends": dividends,
        "annualized_return_pct": annualized,
    }


def _breakdown() -> list[dict[str, str]]:
    return [
        {"key": "uncapitalized_transit_logistics", "label": "Транзитная логистика, не подтверждённая как капитализированная", "amount_rub": "0.00"},
        {"key": "wb_jam_subscription", "label": "Подписка WB Jam", "amount_rub": "0.00"},
        {"key": "wb_paid_services", "label": "Платные сервисы WB", "amount_rub": "0.00"},
        {"key": "other_withholdings", "label": "Прочие удержания", "amount_rub": "0.00"},
    ]


def _workbook_fixture() -> bytes:
    wb = Workbook()
    wb.active.title = "Партнёрский отчёт"
    output = BytesIO()
    wb.save(output)
    return output.getvalue()


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


if __name__ == "__main__":
    main()
