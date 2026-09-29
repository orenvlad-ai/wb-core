#!/usr/bin/env python3
"""Browser regression for independent weekly/daily Finance tabs and late responses."""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright
from apps.sheet_vitrina_v1_operator_ui_persistence_smoke import LocalOperatorFixtureServer
from packages.adapters.registry_upload_http_entrypoint import DEFAULT_SHEET_OPERATOR_UI_PATH


def _period(day: str, value: str) -> dict:
    return {
        "day": day, "week_start": day, "week_end": day, "status": "completed",
        "report_ids": ["131887220260928"], "report_count": 1, "raw_row_count": 1,
        "metrics": {"sales_qty": 1, "net_revenue": "100.0000",
                    "spp_fbo_pct": value, "spp_fbs_pct": None},
        "cost_coverage": {"unmatched_units": 0, "spp": {"coverage": {
            "valid_quantity": 1, "candidate_quantity": 1,
            "unknown_channel_quantity": 0, "missing_spp_quantity": 0,
        }}},
    }


def main() -> None:
    weekly = _period("2026-09-21", "7.0000")
    daily = _period("2026-09-28", "12.5000")
    source = """
      window.__financeCalls = {weekly: 0, daily: 0};
      const originalFetch = window.fetch.bind(window);
      window.fetch = (url, options) => {
        const path = String(url);
        if (path.endsWith('/wb-finance-report') || path.endsWith('/wb-finance-daily')) {
          const isDaily = path.endsWith('/wb-finance-daily');
          window.__financeCalls[isDaily ? 'daily' : 'weekly']++;
          const payload = isDaily ? DAILY_PAYLOAD : WEEKLY_PAYLOAD;
          return new Promise((resolve) => setTimeout(() => resolve(new Response(
            JSON.stringify(payload), {status: 200, headers: {'Content-Type': 'application/json'}}
          )), isDaily ? 20 : 350));
        }
        return originalFetch(url, options);
      };
    """.replace("DAILY_PAYLOAD", json.dumps({"days": [daily]}, ensure_ascii=False))\
        .replace("WEEKLY_PAYLOAD", json.dumps({"weeks": [weekly]}, ensure_ascii=False))
    with LocalOperatorFixtureServer() as url, sync_playwright() as browser_api:
        browser = browser_api.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script(source)
            page.goto(url + DEFAULT_SHEET_OPERATOR_UI_PATH)
            page.locator('[data-tab-button="reports"]').click()
            page.locator('[data-report-section-button="wb-finance"]').click()
            page.locator("#wbFinanceDailyTab").click()
            page.locator("#wbFinanceDailyTableWrap table").wait_for()
            page.wait_for_timeout(500)  # weekly response arrives after daily render
            assert page.locator("#wbFinanceDailyPanel").is_visible()
            assert not page.locator("#wbFinanceWeeklyPanel").is_visible()
            assert "12,50%" in page.locator("#wbFinanceDailyTableWrap").inner_text()
            assert "Показаны последние 14 закрытых дней" in page.locator("#wbFinanceDailyPeriod").inner_text()
            page.locator("#wbFinanceWeeklyTab").click()
            assert page.locator("#wbFinanceWeeklyPanel").is_visible()
            assert "7,00%" in page.locator("#wbFinanceReportTableWrap").inner_text()
            assert "Одна заявка на выплату" in page.locator("#wbFinanceReportPeriod").inner_text()
            page.locator("#wbFinanceDailyTab").click()
            assert page.evaluate("window.__financeCalls") == {"weekly": 1, "daily": 1}
            assert not errors, errors
        finally:
            browser.close()
    print("wb_finance_daily_ui: ok -> async tabs, independent state, percent format")


if __name__ == "__main__":
    main()
