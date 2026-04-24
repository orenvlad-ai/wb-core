"""Browser smoke for web-vitrina group action launch/error surfacing."""

from __future__ import annotations

import json
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import (  # noqa: E402
    LocalWebVitrinaFixtureServer,
)
from packages.adapters.registry_upload_http_entrypoint import DEFAULT_SHEET_WEB_VITRINA_UI_PATH  # noqa: E402


def main() -> None:
    with LocalWebVitrinaFixtureServer(with_ready_snapshot=True) as base_url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1100, "height": 900})

            route_hits = {"count": 0}

            def fail_group_refresh(route: object) -> None:
                route_hits["count"] += 1
                route.fulfill(
                    status=404,
                    content_type="application/json",
                    body=json.dumps({"detail": "Not Found"}),
                )

            context.route("**/v1/sheet-vitrina-v1/web-vitrina/group-refresh", fail_group_refresh)
            page = context.new_page()
            page.goto(base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH, wait_until="domcontentloaded")
            page.wait_for_selector("[data-refresh-source-group]", timeout=20000)
            group_button = page.locator("[data-refresh-source-group='wb_api']").first
            page.locator("[data-refresh-source-group-date='wb_api']").fill("2026-04-13")
            group_button.click()
            page.wait_for_function(
                """() => {
                  const group = document.querySelector('[data-loading-group="wb_api"]');
                  const log = document.querySelector('[data-activity-log-body]');
                  return !!group && !!log
                    && group.textContent.includes('Дата недоступна')
                    && log.textContent.includes('Дата 2026-04-13 недоступна для обновления группы');
                }""",
                timeout=5000,
            )
            if route_hits["count"] != 0:
                raise AssertionError(f"unsupported date must fail client-side before POST, got {route_hits}")
            valid_refresh_date = "2026-04-20"
            page.locator("[data-refresh-source-group-date='wb_api']").fill(valid_refresh_date)
            with page.expect_response("**/v1/sheet-vitrina-v1/web-vitrina/group-refresh") as response_info:
                group_button.click()
            response = response_info.value
            if response.request.method != "POST" or response.status != 404:
                raise AssertionError(
                    f"group refresh launch must POST and expose simulated 404, got "
                    f"{response.request.method} {response.status}"
                )
            request_payload = json.loads(response.request.post_data or "{}")
            if request_payload != {"async": True, "source_group_id": "wb_api", "as_of_date": valid_refresh_date}:
                raise AssertionError(f"group refresh must send date-scoped payload, got {request_payload}")
            page.wait_for_function(
                """() => {
                  const group = document.querySelector('[data-loading-group="wb_api"]');
                  const log = document.querySelector('[data-activity-log-body]');
                  return !!group && !!log
                    && group.textContent.includes('Ошибка запуска')
                    && log.textContent.includes('Не удалось запустить обновление группы WB API за 2026-04-20: HTTP 404 route not found');
                }""",
                timeout=5000,
            )
            payload = page.evaluate(
                """() => ({
                  group_text: document.querySelector('[data-loading-group="wb_api"]').textContent.trim(),
                  log_text: document.querySelector('[data-activity-log-body]').textContent.trim(),
                  top_status_badge_count: document.querySelectorAll('[data-status-badge]').length,
                  request_date: document.querySelector('[data-refresh-source-group-date="wb_api"]').value,
                  session_controls: document.querySelectorAll('[data-session-check]').length
                })"""
            )
            if payload["session_controls"] != 1:
                raise AssertionError(f"session-check controls must remain rendered, got {payload}")
            if payload["top_status_badge_count"] != 0:
                raise AssertionError(f"top status badge must not be rendered, got {payload}")
            print("web_vitrina_group_action_unsupported_date: ok -> 2026-04-13")
            print("web_vitrina_group_action_launch_404_log: ok ->", payload["request_date"])
            browser.close()


if __name__ == "__main__":
    main()
