#!/usr/bin/env python3
"""Desktop and narrow Playwright smoke for the operator Partner Report UI."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import sys
import threading
import zipfile

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_PARTNER_REPORT_FINALIZE_PATH,
    DEFAULT_PARTNER_REPORT_FINALIZED_PATH,
    DEFAULT_PARTNER_REPORT_OPTIONS_PATH,
    DEFAULT_PARTNER_REPORT_PREVIEW_PACKAGE_PATH,
    DEFAULT_PARTNER_REPORT_PREVIEW_PATH,
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
    package = _zip_fixture()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/operator"):
                self._write(200, "text/html; charset=utf-8", html.encode("utf-8"))
            elif self.path == DEFAULT_PARTNER_REPORT_OPTIONS_PATH:
                self._json(200, _options())
            elif self.path == f"{DEFAULT_PARTNER_REPORT_FINALIZED_PATH}/prf_fixture/package.zip":
                self._write_zip(package)
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
                self._json(200, _report(finalized=False))
            elif self.path == DEFAULT_PARTNER_REPORT_FINALIZE_PATH:
                self._json(200, _report(finalized=True))
            elif self.path == DEFAULT_PARTNER_REPORT_PREVIEW_PACKAGE_PATH:
                self._write_zip(package)
            else:
                self._json(404, {"error": "not found"})

        def log_message(self, format: str, *args: object) -> None:
            return

        def _json(self, status: int, payload: dict) -> None:
            self._write(
                status,
                "application/json; charset=utf-8",
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )

        def _write_zip(self, body: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header(
                "Content-Disposition",
                "attachment; filename*=UTF-8''partner_fixture.zip",
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
            page.goto(f"http://127.0.0.1:{port}/operator", wait_until="networkidle")
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
                raise AssertionError("unsaved business parameters must not receive hidden defaults")
            values = {
                "#partnerReportShare": "40",
                "#partnerReportCapital": "500000",
                "#partnerReportReserve": "20",
                "#partnerReportOffice": "10000",
                "#partnerReportTax": "6",
            }
            for selector, value in values.items():
                page.locator(selector).fill(value)
            page.locator("#partnerReportSaveSettings").click()
            page.wait_for_function("document.getElementById('partnerReportStatus').innerText.includes('Настройки сохранены')")
            page.locator("#partnerReportGenerate").click()
            page.locator("#partnerReportTableWrap table").wait_for(state="visible")
            facts = page.evaluate(
                r"""
                () => {
                  const wrap = document.getElementById('partnerReportTableWrap');
                  const table = wrap.querySelector('table');
                  const payout = [...table.rows].find((row) => row.cells[0] && row.cells[0].innerText.trim() === 'Выплата партнёру');
                  const summary = document.getElementById('partnerReportWeekSummary').innerText;
                  return {
                    heading: document.querySelector('[data-report-section-panel="partner"] h2').innerText,
                    summary,
                    header: [...table.tHead.rows[0].cells].map((cell) => cell.innerText.trim()),
                    payout: [...payout.cells].map((cell) => cell.innerText.trim().replace(/\s/g, ' ')),
                    payoutColor: getComputedStyle(payout.cells[1]).color,
                    sticky: getComputedStyle(table.rows[1].cells[0]).position,
                    finalizedEnabled: !document.getElementById('partnerReportFinalize').disabled,
                    downloadEnabled: !document.getElementById('partnerReportDownload').disabled,
                    bodyOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
                    sourceNote: document.getElementById('partnerReportNotes').innerText,
                  };
                }
                """
            )
            if (
                facts["heading"].casefold() != "отчёт о доходности карточки"
                or facts["header"][-1] != "Итого за период"
                or "44 253,90" not in facts["payout"][1]
                or "42 584,70" not in facts["payout"][-1]
                or facts["sticky"] != "sticky"
                or not facts["finalizedEnabled"]
                or not facts["downloadEnabled"]
                or facts["bodyOverflow"]
                or "accepted ads_compact" not in facts["sourceNote"]
            ):
                raise AssertionError(f"desktop Partner Report contract mismatch: {facts}")

            page.locator("#partnerReportShare").fill("40")
            stale = page.evaluate(
                """
                () => ({
                  tableHidden: document.getElementById('partnerReportTableWrap').hidden,
                  finalizeDisabled: document.getElementById('partnerReportFinalize').disabled,
                  downloadDisabled: document.getElementById('partnerReportDownload').disabled,
                })
                """
            )
            if stale != {
                "tableHidden": True,
                "finalizeDisabled": True,
                "downloadDisabled": True,
            }:
                raise AssertionError(f"edited parameters left a stale preview actionable: {stale}")
            page.locator("#partnerReportGenerate").click()
            page.locator("#partnerReportTableWrap table").wait_for(state="visible")

            with page.expect_download() as preview_download:
                page.locator("#partnerReportDownload").click()
            if preview_download.value.suggested_filename != "partner_fixture.zip":
                raise AssertionError("preview package filename mismatch")
            page.locator("#partnerReportFinalize").click()
            page.wait_for_function("document.getElementById('partnerReportStatus').innerText.includes('prf_fixture')")
            with page.expect_download() as finalized_download:
                page.locator("#partnerReportDownload").click()
            if finalized_download.value.suggested_filename != "partner_fixture.zip":
                raise AssertionError("finalized package filename mismatch")

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
                  };
                }
                """
            )
            if narrow != {
                "pageOverflow": False,
                "settingsColumns": 1,
                "localTableScroll": True,
                "controlsWithinViewport": True,
            }:
                raise AssertionError(f"narrow Partner Report layout mismatch: {narrow}")
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
        DEFAULT_PARTNER_REPORT_PREVIEW_PACKAGE_PATH,
        DEFAULT_PARTNER_REPORT_FINALIZE_PATH,
    ]
    if [item["path"] for item in requests] != expected_paths:
        raise AssertionError(f"Partner Report HTTP sequence mismatch: {requests}")
    saved = requests[0]["body"]
    if saved != {
        "nm_id": "101101",
        "partner_share_pct": "40",
        "invested_capital_rub": "500000",
        "replenishment_reserve_pct": "20",
        "weekly_office_expense_rub": "10000",
        "tax_rate_pct": "6",
        "common_expense_rule": "net_revenue_share",
    }:
        raise AssertionError(f"server-owned settings payload mismatch: {saved}")
    if console_errors or page_errors or failed_responses:
        raise AssertionError(
            f"Partner UI errors: console={console_errors}, page={page_errors}, network={failed_responses}"
        )
    print(
        "partner_report_browser: ok -> blank defaults, saved parameters, week picker, preview/finalize/download, desktop+narrow"
    )


def _options() -> dict:
    return {
        "status": "ok",
        "cards": [
            {
                "nm_id": "101101",
                "product_name": "Выбранный товар",
                "vendor_code": "VC101",
                "barcode": "4600000101101",
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


def _report(*, finalized: bool) -> dict:
    first = _values("476034", "44253.904", "8.8508", "460.2416")
    second = _values("100", "0", "0", "0")
    totals = _values("476134", "42584.704", "8.5169", "221.4394")
    return {
        "status": "ready",
        "report_id": "prf_fixture" if finalized else None,
        "finalized": finalized,
        "formula_version": "partner_report_profitability_v1",
        "source_digest": "sha256:fixture",
        "weeks": [
            {"week_start": "2026-07-06", "week_end": "2026-07-12", "label": "06.07–12.07", "values": first},
            {"week_start": "2026-07-13", "week_end": "2026-07-19", "label": "13.07–19.07", "values": second},
        ],
        "totals": totals,
        "blockers": [],
    }


def _values(net_revenue: str, payout: str, roi: str, annualized: str) -> dict:
    return {
        "net_revenue": net_revenue,
        "cogs": "83837",
        "commission": "174797",
        "logistics": "0",
        "ads": "30904",
        "storage": "0",
        "other_direct_expenses": "0",
        "allocated_common_expenses": "0",
        "positive_adjustments": "0",
        "card_margin": "186496",
        "office": "10000",
        "estimated_tax": "28562.04",
        "replenishment_reserve": "37299.2",
        "distributable_profit": "110634.76",
        "partner_payout": payout,
        "period_roi_pct": roi,
        "annualized_return_pct": annualized,
    }


def _zip_fixture() -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("Методология_и_параметры.txt", "fixture")
    return output.getvalue()


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
