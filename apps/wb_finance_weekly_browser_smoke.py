#!/usr/bin/env python3
"""Local Playwright smoke for WB Finance temporal cost and quality UI."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sys
import threading

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    _render_sheet_vitrina_operator_ui,
)


FINANCE_PATH = "/v1/sheet-vitrina-v1/wb-finance-report"


def main() -> None:
    port = _reserve_free_port()
    html = _render_sheet_vitrina_operator_ui(
        daily_report_path="/daily",
        stock_report_path="/stock",
        plan_report_path="/plan",
        wb_finance_report_path=FINANCE_PATH,
        refresh_path="/refresh",
        load_path="/load",
        status_path="/status",
        job_path="/job",
        embedded_tab="reports",
    )
    payload = _finance_payload()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/operator"):
                self._write(200, "text/html; charset=utf-8", html.encode("utf-8"))
                return
            body = payload if self.path.startswith(FINANCE_PATH) else {}
            self._write(
                200,
                "application/json; charset=utf-8",
                json.dumps(body, ensure_ascii=False).encode("utf-8"),
            )

        def log_message(self, format: str, *args: object) -> None:
            return

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
    failed_responses: list[str] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.on(
                "console",
                lambda message: (
                    console_errors.append(message.text)
                    if message.type == "error"
                    else None
                ),
            )
            page.on(
                "response",
                lambda response: (
                    failed_responses.append(f"{response.status} {response.url}")
                    if response.status >= 400
                    else None
                ),
            )
            page.goto(
                f"http://127.0.0.1:{port}/operator?embedded_tab=reports",
                wait_until="networkidle",
            )
            page.locator('[data-report-section-button="wb-finance"]').click()
            table = page.locator("#wbFinanceReportTableWrap table")
            table.wait_for(state="visible")
            facts = page.evaluate(
                r"""
                () => {
                  const wrap = document.getElementById('wbFinanceReportTableWrap');
                  const table = wrap.querySelector('table');
                  const headerTitles = [...table.querySelectorAll('thead th')].slice(1).map((cell) => cell.title);
                  const qualityLabels = [...table.querySelectorAll('.wb-finance-cost-quality')].map((cell) => cell.innerText.trim());
                  const weekStatusLabels = [...table.querySelectorAll('.wb-finance-week-status')].map((cell) => cell.innerText.trim());
                  const commissionRow = [...table.querySelectorAll('tbody tr')].find((row) => row.cells[0] && row.cells[0].innerText.trim() === 'Агентское вознаграждение / комиссия WB');
                  const wbShareRow = [...table.querySelectorAll('tbody tr')].find((row) => row.cells[0] && row.cells[0].innerText.trim() === 'Расходы WB, % от чистой выручки');
                  const noMarketingRow = [...table.querySelectorAll('tbody tr')].find((row) => row.cells[0] && row.cells[0].innerText.trim() === 'Расходы WB без маркетинга, % от чистой выручки');
                  const cogsRow = [...table.querySelectorAll('tbody tr')].find((row) => row.cells[0] && row.cells[0].innerText.trim() === 'Себестоимость продаж');
                  const profitRow = [...table.querySelectorAll('tbody tr')].find((row) => row.cells[0] && row.cells[0].innerText.trim() === 'Прибыль после себестоимости');
                  const marginRow = [...table.querySelectorAll('tbody tr')].find((row) => row.cells[0] && row.cells[0].innerText.trim() === 'Итоговая рентабельность, %');
                  const metricCell = cogsRow.cells[0];
                  const normalize = (value) => value.trim().replace(/\s/g, ' ');
                  wrap.scrollLeft = 0;
                  const before = wrap.scrollLeft;
                  wrap.scrollLeft = wrap.scrollWidth;
                  return {
                    weekCount: headerTitles.length,
                    headerTitles,
                    qualityLabels,
                    weekStatusLabels,
                    commission: [...commissionRow.cells].slice(1).map((cell) => ({text: normalize(cell.innerText), className: cell.querySelector('.wb-finance-expense-share').className})),
                    wbShare: [...wbShareRow.cells].slice(1).map((cell) => ({text: normalize(cell.innerText), className: cell.querySelector('.wb-finance-relative-cell').className})),
                    lastExpenseMetric: noMarketingRow.previousElementSibling && noMarketingRow.nextElementSibling ? noMarketingRow.nextElementSibling.cells[0].innerText.trim() : '',
                    noMarketing: [...noMarketingRow.cells].slice(1).map((cell) => normalize(cell.innerText)),
                    containsPointDelta: table.innerText.includes('п.п.'),
                    cogs: [...cogsRow.cells].slice(1).map((cell) => normalize(cell.innerText)),
                    profit: [...profitRow.cells].slice(1).map((cell) => normalize(cell.innerText)),
                    margin: [...marginRow.cells].slice(1).map((cell) => normalize(cell.innerText)),
                    sticky: getComputedStyle(metricCell).position,
                    horizontalOverflow: wrap.scrollWidth > wrap.clientWidth,
                    horizontalScrollWorks: wrap.scrollLeft !== before,
                    note: document.getElementById('wbFinanceReportNotes').innerText,
                  };
                }
                """
            )
            screenshot_path = str(
                os.environ.get("WB_FINANCE_BROWSER_SMOKE_SCREENSHOT") or ""
            ).strip()
            if screenshot_path:
                page.screenshot(path=screenshot_path, full_page=True)
            browser.close()
        if facts["weekCount"] != 3:
            raise AssertionError(f"weekly columns mismatch: {facts}")
        if facts["qualityLabels"]:
            raise AssertionError(f"cost-quality badges must be absent: {facts}")
        if facts["weekStatusLabels"]:
            raise AssertionError(f"week-level blockers must not render as repeated header badges: {facts}")
        if (
            "business-approved retro: 2 шт." not in facts["headerTitles"][1]
            or "Our WB Cost: 4 шт." not in facts["headerTitles"][1]
        ):
            raise AssertionError(
                f"mixed-week tooltip must disclose both sources: {facts}"
            )
        if "estimated/fallback: 0,5 шт." not in facts["headerTitles"][1]:
            raise AssertionError(f"mixed-week quality tooltip mismatch: {facts}")
        if facts["cogs"] != ["200,00 ₽", "490,00 ₽", "—"]:
            raise AssertionError(f"COGS formatting/null contract mismatch: {facts}")
        if facts["profit"][2] != "—" or facts["margin"][2] != "—":
            raise AssertionError(
                f"incomplete post-cutover profit must stay blank: {facts}"
            )
        if (
            "↑" not in facts["commission"][1]["text"]
            or "wb-finance-trend-up" not in facts["commission"][1]["className"]
            or "↓" not in facts["commission"][2]["text"]
            or "wb-finance-trend-down" not in facts["commission"][2]["className"]
            or "→" not in facts["wbShare"][2]["text"]
            or "wb-finance-trend-flat" not in facts["wbShare"][2]["className"]
            or "↑" in facts["commission"][0]["text"]
            or "↓" in facts["commission"][0]["text"]
            or "→" in facts["commission"][0]["text"]
            or facts["containsPointDelta"]
        ):
            raise AssertionError(f"expense percent/arrow quality contract mismatch: {facts}")
        if facts["lastExpenseMetric"] != "Результат финансового отчёта WB":
            raise AssertionError(f"no-marketing metric is not last in expense block: {facts}")
        if (
            facts["sticky"] != "sticky"
            or not facts["horizontalOverflow"]
            or not facts["horizontalScrollWorks"]
        ):
            raise AssertionError(
                f"weekly table scroll/sticky contract mismatch: {facts}"
            )
        if "legacy до 01.05.2026" not in facts["note"]:
            raise AssertionError(f"temporal source note missing: {facts}")
        if console_errors or failed_responses:
            raise AssertionError(
                f"local Finance UI emitted errors: console={console_errors}, network={failed_responses}"
            )
        print(
            "wb_finance_weekly_browser: ok -> clean headers, mixed-source tooltip, expense shares/arrows, null blockers, sticky scroll"
        )
        preview_seconds = float(
            os.environ.get("WB_FINANCE_BROWSER_SMOKE_PREVIEW_SECONDS") or 0
        )
        if preview_seconds > 0:
            print(
                f"wb_finance_weekly_browser_preview: http://127.0.0.1:{port}/operator"
            )
            try:
                threading.Event().wait(preview_seconds)
            except KeyboardInterrupt:
                pass
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _finance_payload() -> dict[str, object]:
    def week(
        start: str,
        end: str,
        *,
        cogs: str | None,
        profit: str | None,
        margin: str | None,
        cost_price_units: int,
        retro_units: int,
        our_units: int,
        confirmed_share: str | None,
        estimated_fallback: str,
        unmatched: int = 0,
        commission: str = "300.0000",
        wb_expenses_pct: str = "40.0000",
    ) -> dict[str, object]:
        return {
            "week_start": start,
            "week_end": end,
            "status": "completed",
            "report_count": 2,
            "raw_row_count": 10,
            "report_ids": ["1", "2"],
            "metrics": {
                "sales_qty": 10,
                "returns_qty": 1,
                "net_sales_qty": 9,
                "revenue_before_returns": "1000.0000",
                "returns_amount": "100.0000",
                "net_revenue": "900.0000",
                "commission": commission,
                "acquiring": "20.0000",
                "logistics": "10.0000",
                "storage": "0.0000",
                "acceptance": "0.0000",
                "marketing": "50.0000",
                "transit_logistics": "0.0000",
                "penalties": "0.0000",
                "subscriptions": "0.0000",
                "paid_services": "0.0000",
                "other_deductions": "0.0000",
                "positive_adjustments": "0.0000",
                "total_wb_expenses": "360.0000",
                "profit_period_expenses": "360.0000",
                "wb_expenses_pct": wb_expenses_pct,
                "wb_expenses_without_marketing_pct": "34.4444",
                "to_seller": "600.0000",
                "before_cogs_profit": "540.0000",
                "before_cogs_margin_pct": "60.0000",
                "cogs": cogs,
                "profit_after_cogs": profit,
                "final_margin_pct": margin,
            },
            "cost_coverage": {
                "matched_units": cost_price_units + retro_units + our_units,
                "unmatched_units": unmatched,
                "coverage_pct": "100.0000" if not unmatched else "90.0000",
                "problem_skus": [],
                "quality": {
                    "source_units": {
                        "cost_price": cost_price_units,
                        "business_approved_retro": retro_units,
                        "our_wb_cost_daily_state": our_units,
                    },
                    "confirmed_share_pct": confirmed_share,
                    "estimated_fallback_units": estimated_fallback,
                    "operation_date_fallback_rows": 0,
                },
            },
            "reconciliation_status": "ok",
        }

    return {
        "status": "ok",
        "weeks": [
            week(
                "2026-06-22",
                "2026-06-28",
                cogs="200.0000",
                profit="340.0000",
                margin="37.7778",
                cost_price_units=0,
                retro_units=9,
                our_units=0,
                confirmed_share=None,
                estimated_fallback="0.0000",
                commission="300.0000",
                wb_expenses_pct="40.0000",
            ),
            week(
                "2026-06-29",
                "2026-07-05",
                cogs="490.0000",
                profit="50.0000",
                margin="5.5556",
                cost_price_units=0,
                retro_units=2,
                our_units=4,
                confirmed_share="87.5000",
                estimated_fallback="0.5000",
                commission="360.0000",
                wb_expenses_pct="45.0000",
            ),
            week(
                "2026-07-06",
                "2026-07-12",
                cogs=None,
                profit=None,
                margin=None,
                cost_price_units=0,
                retro_units=0,
                our_units=0,
                confirmed_share=None,
                estimated_fallback="0.0000",
                unmatched=1,
                commission="315.0000",
                wb_expenses_pct="45.0050",
            ),
        ],
    }


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
