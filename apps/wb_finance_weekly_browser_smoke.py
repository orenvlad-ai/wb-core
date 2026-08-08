#!/usr/bin/env python3
"""Local Playwright smoke for WB Finance temporal cost and quality UI."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from decimal import Decimal
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
            page = browser.new_page(
                viewport={"width": 960, "height": 900},
                color_scheme="dark",
            )
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
            wrap = page.locator("#wbFinanceReportTableWrap")
            wrap.evaluate("element => { element.scrollLeft = element.scrollWidth; }")
            subscriptions_row = table.locator("tbody tr", has_text="Подписки").first
            subscriptions_metric = subscriptions_row.locator("td").first
            subscriptions_before_hover = subscriptions_metric.bounding_box()
            subscriptions_metric.hover()
            subscriptions_after_hover = subscriptions_metric.bounding_box()
            facts = page.evaluate(
                r"""
                () => {
                  const wrap = document.getElementById('wbFinanceReportTableWrap');
                  const table = wrap.querySelector('table');
                  const headerTitles = [...table.querySelectorAll('thead th')].slice(1).map((cell) => cell.title);
                  const qualityLabels = [...table.querySelectorAll('.wb-finance-cost-quality')].map((cell) => cell.innerText.trim());
                  const weekStatusLabels = [...table.querySelectorAll('.wb-finance-week-status')].map((cell) => cell.innerText.trim());
                  const commissionRow = [...table.querySelectorAll('tbody tr')].find((row) => row.cells[0] && row.cells[0].innerText.trim() === 'Агентское вознаграждение WB');
                  const acquiringRow = [...table.querySelectorAll('tbody tr')].find((row) => row.cells[0] && row.cells[0].innerText.trim() === 'Эквайринг');
                  const withMarketingRow = [...table.querySelectorAll('tbody tr')].find((row) => row.cells[0] && row.cells[0].innerText.trim() === 'Расходы WB с маркетингом');
                  const noMarketingRow = [...table.querySelectorAll('tbody tr')].find((row) => row.cells[0] && row.cells[0].innerText.trim() === 'Расходы WB без маркетинга');
                  const subscriptionsRow = [...table.querySelectorAll('tbody tr')].find((row) => row.cells[0] && row.cells[0].innerText.trim() === 'Подписки');
                  const correctionsRow = [...table.querySelectorAll('tbody tr')].find((row) => row.cells[0] && row.cells[0].innerText.trim() === 'Корректировки (расходы)');
                  const cogsRow = [...table.querySelectorAll('tbody tr')].find((row) => row.cells[0] && row.cells[0].innerText.trim() === 'Себестоимость продаж');
                  const profitRow = [...table.querySelectorAll('tbody tr')].find((row) => row.cells[0] && row.cells[0].innerText.trim() === 'Прибыль после себестоимости');
                  const marginRow = [...table.querySelectorAll('tbody tr')].find((row) => row.cells[0] && row.cells[0].innerText.trim() === 'Итоговая рентабельность, %');
                  const metricCell = cogsRow.cells[0];
                  const normalize = (value) => value.trim().replace(/\s/g, ' ');
                  wrap.scrollLeft = 0;
                  const before = wrap.scrollLeft;
                  wrap.scrollLeft = wrap.scrollWidth;
                  const ownsStickyPixels = (row) => {
                    const cell = row && row.cells && row.cells[0];
                    if (!cell) return false;
                    row.scrollIntoView({block: 'center'});
                    wrap.scrollLeft = wrap.scrollWidth;
                    const rect = cell.getBoundingClientRect();
                    const points = [
                      [rect.left + 6, rect.top + rect.height / 2],
                      [rect.right - 6, rect.top + rect.height / 2]
                    ];
                    return points.every(([x, y]) => {
                      const hit = document.elementFromPoint(x, y);
                      return Boolean(hit && (hit === cell || cell.contains(hit)));
                    });
                  };
                  const opaqueBackground = (cell) => {
                    const value = getComputedStyle(cell).backgroundColor;
                    const rgba = value.match(/^rgba\([^,]+,[^,]+,[^,]+,\s*([0-9.]+)\)$/);
                    return value !== 'transparent' && (!rgba || Number(rgba[1]) === 1);
                  };
                  const groupRow = table.querySelector('tbody tr.wb-finance-group-row');
                  const internalRow = table.querySelector('tbody tr.wb-finance-internal-divider');
                  return {
                    weekCount: headerTitles.length,
                    headerTitles,
                    qualityLabels,
                    weekStatusLabels,
                    commission: [...commissionRow.cells].slice(1).map((cell) => {
                      const share = cell.querySelector('.wb-finance-expense-share');
                      return {text: normalize(cell.innerText), className: share.className, state: share.dataset.trendState, aria: share.getAttribute('aria-label'), title: share.title};
                    }),
                    acquiring: [...acquiringRow.cells].slice(1).map((cell) => normalize(cell.innerText)),
                    withMarketing: [...withMarketingRow.cells].slice(1).map((cell) => {
                      const share = cell.querySelector('.wb-finance-expense-share');
                      return {text: normalize(cell.innerText), className: share.className, state: share.dataset.trendState, aria: share.getAttribute('aria-label'), title: share.title};
                    }),
                    duplicatePercentRow: [...table.querySelectorAll('tbody tr')].some((row) => row.cells[0] && row.cells[0].innerText.trim() === 'Расходы WB, % от чистой выручки'),
                    technicalExpenseRow: [...table.querySelectorAll('tbody tr')].some((row) => row.cells[0] && row.cells[0].innerText.trim() === 'Расходы периода, учитываемые в прибыли'),
                    lastExpenseMetric: noMarketingRow.previousElementSibling && noMarketingRow.nextElementSibling ? noMarketingRow.nextElementSibling.cells[0].innerText.trim() : '',
                    noMarketing: [...noMarketingRow.cells].slice(1).map((cell) => normalize(cell.innerText)),
                    containsPointDelta: table.innerText.includes('п.п.'),
                    containsVisibleTrendGlyph: /[↑↓→]/.test(table.innerText),
                    containsAccessibleTrendGlyph: [...table.querySelectorAll('[aria-label],[title]')].some((node) => /[↑↓→]/.test((node.getAttribute('aria-label') || '') + (node.getAttribute('title') || ''))),
                    correctionsRowPresent: Boolean(correctionsRow),
                    cogs: [...cogsRow.cells].slice(1).map((cell) => normalize(cell.innerText)),
                    profit: [...profitRow.cells].slice(1).map((cell) => normalize(cell.innerText)),
                    margin: [...marginRow.cells].slice(1).map((cell) => normalize(cell.innerText)),
                    sticky: getComputedStyle(metricCell).position,
                    subscriptionsMetricText: subscriptionsRow.cells[0].innerText.trim(),
                    subscriptionsOpaque: opaqueBackground(subscriptionsRow.cells[0]),
                    subscriptionsOwnsStickyPixels: ownsStickyPixels(subscriptionsRow),
                    groupOpaque: opaqueBackground(groupRow.cells[0]),
                    groupOwnsStickyPixels: ownsStickyPixels(groupRow),
                    internalOpaque: opaqueBackground(internalRow.cells[0]),
                    internalOwnsStickyPixels: ownsStickyPixels(internalRow),
                    firstColumnWidth: subscriptionsRow.cells[0].getBoundingClientRect().width,
                    horizontalOverflow: wrap.scrollWidth > wrap.clientWidth,
                    horizontalScrollWorks: wrap.scrollLeft !== before,
                    note: document.getElementById('wbFinanceReportNotes').innerText,
                    storageHealth: document.getElementById('wbFinanceStorageHealth').innerText,
                    storageHealthTone: document.querySelector('#wbFinanceStorageHealth .section-message').className,
                  };
                }
                """
            )
            facts["subscriptionsGeometryStable"] = bool(
                subscriptions_before_hover
                and subscriptions_after_hover
                and abs(
                    subscriptions_before_hover["x"]
                    - subscriptions_after_hover["x"]
                )
                < 0.5
                and abs(
                    subscriptions_before_hover["width"]
                    - subscriptions_after_hover["width"]
                )
                < 0.5
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
            "проекция от 01.07: 2 шт." not in facts["headerTitles"][1]
            or "exact-date Our WB Cost: 4 шт." not in facts["headerTitles"][1]
        ):
            raise AssertionError(
                f"mixed-week tooltip must disclose both sources: {facts}"
            )
        if facts["cogs"] != ["200,00 ₽", "380,00 ₽", "—"]:
            raise AssertionError(f"COGS formatting/null contract mismatch: {facts}")
        if facts["profit"][2] != "—" or facts["margin"][2] != "—":
            raise AssertionError(
                f"incomplete post-cutover profit must stay blank: {facts}"
            )
        if (
            facts["commission"][0]["state"] != "neutral"
            or "wb-finance-trend-neutral" not in facts["commission"][0]["className"]
            or facts["commission"][1]["state"] != "deteriorated"
            or "wb-finance-trend-deteriorated" not in facts["commission"][1]["className"]
            or facts["commission"][2]["state"] != "improved"
            or "wb-finance-trend-improved" not in facts["commission"][2]["className"]
            or facts["withMarketing"][2]["state"] != "flat"
            or "wb-finance-trend-flat" not in facts["withMarketing"][2]["className"]
            or "Нет сопоставимой предыдущей недели" not in facts["commission"][0]["aria"]
            or "Ухудшение" not in facts["commission"][1]["title"]
            or "Улучшение" not in facts["commission"][2]["aria"]
            or facts["containsVisibleTrendGlyph"]
            or facts["containsAccessibleTrendGlyph"]
            or facts["containsPointDelta"]
            or facts["duplicatePercentRow"]
            or facts["technicalExpenseRow"]
            or not facts["correctionsRowPresent"]
        ):
            raise AssertionError(f"expense percent/color quality contract mismatch: {facts}")
        if facts["lastExpenseMetric"] != "Результат финансового отчёта WB":
            raise AssertionError(f"no-marketing metric is not last in expense block: {facts}")
        if (
            facts["sticky"] != "sticky"
            or not facts["horizontalOverflow"]
            or not facts["horizontalScrollWorks"]
            or facts["subscriptionsMetricText"] != "Подписки"
            or not facts["subscriptionsOpaque"]
            or not facts["subscriptionsOwnsStickyPixels"]
            or not facts["groupOpaque"]
            or not facts["groupOwnsStickyPixels"]
            or not facts["internalOpaque"]
            or not facts["internalOwnsStickyPixels"]
            or not facts["subscriptionsGeometryStable"]
            or abs(float(facts["firstColumnWidth"]) - 285.0) > 1.0
        ):
            raise AssertionError(
                f"weekly table scroll/sticky contract mismatch: {facts}"
            )
        if "стоимость того же nmId на 01.07" not in facts["note"] or "retro-map" not in facts["note"]:
            raise AssertionError(f"temporal source note missing: {facts}")
        if (
            "canonical: monolith" not in facts["storageHealth"]
            or "raw-epoch-1" not in facts["storageHealth"]
            or "operational-epoch-1" not in facts["storageHealth"]
            or "consumer lag: 0" not in facts["storageHealth"]
            or "live-tail cursor/lag: 42 / 0" not in facts["storageHealth"]
            or "mismatches: 0" not in facts["storageHealth"]
            or "rollback: готов" not in facts["storageHealth"]
            or "finance-backup-11111111111111111111" not in facts["storageHealth"]
            or "next replacement: доступен" not in facts["storageHealth"]
            or "прогноз retained growth 30/90 дней" not in facts["storageHealth"]
            or "headroom 30/90 дней" not in facts["storageHealth"]
            or "success" not in facts["storageHealthTone"]
        ):
            raise AssertionError(
                f"Finance storage health UI contract mismatch: {facts}"
            )
        if console_errors or failed_responses:
            raise AssertionError(
                f"local Finance UI emitted errors: console={console_errors}, network={failed_responses}"
            )
        print(
            "wb_finance_weekly_browser: ok -> clean headers, mixed-source tooltip, expense color states without glyphs, null blockers, opaque sticky hover/scroll"
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
        projected_units: int,
        exact_units: int,
        unmatched: int = 0,
        commission: str = "300.0000",
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
                "agent_remuneration": commission,
                "commission": commission,
                "combined_commission_control": str(Decimal(commission) + Decimal("20")),
                "acquiring": "20.0000",
                "logistics": "10.0000",
                "storage": "0.0000",
                "acceptance": "0.0000",
                "marketing": "50.0000",
                "transit_logistics": "0.0000",
                "penalties": "0.0000",
                "subscriptions": "0.0000",
                "paid_services": "0.0000",
                "review_points": "0.0000",
                "other_deductions": "0.0000",
                "corrections": "0.0000",
                "wb_remuneration_adjustment": "0.0000",
                "positive_adjustments": "0.0000",
                "total_wb_expenses": "360.0000",
                "wb_expenses_without_marketing": "310.0000",
                "profit_period_expenses": "360.0000",
                "wb_expenses_without_marketing_pct": "34.4444",
                "to_seller": "600.0000",
                "before_cogs_profit": "540.0000",
                "before_cogs_margin_pct": "60.0000",
                "cogs": cogs,
                "profit_after_cogs": profit,
                "final_margin_pct": margin,
            },
            "cost_coverage": {
                "matched_units": projected_units + exact_units,
                "unmatched_units": unmatched,
                "coverage_pct": "100.0000" if not unmatched else "90.0000",
                "problem_skus": [],
                "quality": {
                    "source_units": {
                        "projected_from_2026_07_01": projected_units,
                        "canonical_exact_date": exact_units,
                    },
                    "fallback_units": "0.0000",
                    "operation_date_fallback_rows": 0,
                },
            },
            "reconciliation_status": "ok",
        }

    return {
        "status": "ok",
        "storage_health": {
            "state": "shadow",
            "canonical_source": "monolith",
            "implicit_manifest": False,
            "raw": {
                "generation_id": "raw-epoch-1",
                "schema_revision": "finance_raw_v1",
                "size_bytes": 10_000_000_000,
            },
            "operational": {
                "generation_id": "operational-epoch-1",
                "schema_revision": "operational_v1",
                "size_bytes": 300_000_000,
            },
            "raw_ack_cursor": 42,
            "operational_cursor": 42,
            "consumer_lag_events": 0,
            "live_tail_cursor": 42,
            "live_tail_lag_events": 0,
            "shadow_mismatch_count": 0,
            "actionable_dead_letters": 0,
            "filesystem": {"free_bytes": 20_000_000_000},
            "backup": {
                "status": "healthy",
                "retained_backup_id": "finance-backup-11111111111111111111",
                "retained_count": 1,
                "retained_bytes": 18_000_000_000,
                "age_seconds": 3600,
                "rpo_seconds": 604800,
                "next_replacement_capacity": True,
                "last_success": {"at": "2026-07-31T04:30:00Z"},
                "last_failure": None,
                "projected_30_day_growth_bytes": 0,
                "projected_90_day_growth_bytes": 0,
                "projected_30_day_available_bytes": 60_000_000_000,
                "projected_90_day_available_bytes": 60_000_000_000,
                "blockers": [],
            },
            "rollback_ready": True,
            "cutover_ready": False,
        },
        "weeks": [
            week(
                "2026-06-22",
                "2026-06-28",
                cogs="200.0000",
                profit="340.0000",
                margin="37.7778",
                projected_units=9,
                exact_units=0,
                commission="300.0000",
            ),
            week(
                "2026-06-29",
                "2026-07-05",
                cogs="380.0000",
                profit="50.0000",
                margin="5.5556",
                projected_units=2,
                exact_units=4,
                commission="360.0000",
            ),
            week(
                "2026-07-06",
                "2026-07-12",
                cogs=None,
                profit=None,
                margin=None,
                projected_units=0,
                exact_units=0,
                unmatched=1,
                commission="315.0000",
            ),
        ],
    }


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
