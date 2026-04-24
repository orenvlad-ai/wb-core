"""Browser smoke for session-only web-vitrina cell highlights."""

from __future__ import annotations

import json
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer  # noqa: E402
from packages.adapters.registry_upload_http_entrypoint import DEFAULT_SHEET_WEB_VITRINA_UI_PATH  # noqa: E402


def main() -> None:
    with LocalWebVitrinaFixtureServer(with_ready_snapshot=True) as base_url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1100, "height": 900})

            def launch_refresh(route: object) -> None:
                route.fulfill(
                    status=202,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "job_id": "highlight-smoke",
                            "status": "running",
                            "job_path": "/v1/sheet-vitrina-v1/job?job_id=highlight-smoke",
                        }
                    ),
                )

            def poll_job(route: object) -> None:
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "job_id": "highlight-smoke",
                            "operation": "refresh",
                            "status": "success",
                            "result": {
                                "updated_cells": [
                                    {
                                        "row_id": "TOTAL|total_orderSum",
                                        "metric_key": "total_orderSum",
                                        "as_of_date": "2026-04-20",
                                        "source_group_id": "wb_api",
                                        "source_key": "sales_funnel_history",
                                        "status": "updated",
                                    },
                                    {
                                        "row_id": "TOTAL|total_view_count",
                                        "metric_key": "total_view_count",
                                        "as_of_date": "2026-04-20",
                                        "source_group_id": "seller_portal_bot",
                                        "source_key": "seller_funnel_snapshot",
                                        "status": "latest_confirmed",
                                    },
                                ],
                            },
                            "log_lines": ["event=highlight_smoke_finish"],
                        }
                    ),
                )

            context.route("**/v1/sheet-vitrina-v1/refresh", launch_refresh)
            context.route("**/v1/sheet-vitrina-v1/job?job_id=highlight-smoke", poll_job)
            page = context.new_page()
            page.goto(base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH, wait_until="domcontentloaded")
            page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
            page.locator("[data-load-refresh-button]").click()
            page.wait_for_selector(".cell-session-highlight-updated", timeout=10000)
            highlight_state = page.evaluate(
                """() => ({
                  green: document.querySelectorAll('.cell-session-highlight-updated').length,
                  yellow: document.querySelectorAll('.cell-session-highlight-latest-confirmed').length,
                  greenCell: document.querySelector('[data-row-id="TOTAL|total_orderSum"][data-cell-date="2026-04-20"]')?.className || '',
                  yellowCell: document.querySelector('[data-row-id="TOTAL|total_view_count"][data-cell-date="2026-04-20"]')?.className || '',
                  unrelated: document.querySelector('[data-row-id="SKU:210183919|avg_price_seller_discounted"][data-cell-date="2026-04-20"]')?.className || ''
                })"""
            )
            if (
                highlight_state["green"] < 1
                or highlight_state["yellow"] < 1
                or "cell-session-highlight-updated" not in highlight_state["greenCell"]
                or "cell-session-highlight-latest-confirmed" not in highlight_state["yellowCell"]
                or "cell-session-highlight" in highlight_state["unrelated"]
            ):
                raise AssertionError(f"unexpected session highlight state: {highlight_state}")

            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
            after_reload = page.evaluate(
                """() => ({
                  green: document.querySelectorAll('.cell-session-highlight-updated').length,
                  yellow: document.querySelectorAll('.cell-session-highlight-latest-confirmed').length
                })"""
            )
            if after_reload["green"] or after_reload["yellow"]:
                raise AssertionError(f"session highlights must disappear after reload, got {after_reload}")
            print("web_vitrina_session_highlights: ok ->", highlight_state, after_reload)
            browser.close()


if __name__ == "__main__":
    main()
