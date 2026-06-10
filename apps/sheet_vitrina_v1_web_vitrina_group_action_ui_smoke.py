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
    _assert_group_controls_survive_empty_loading_rows()
    _assert_seller_session_invalid_top_action()
    _assert_group_action_launch_error()


def _assert_group_controls_survive_empty_loading_rows() -> None:
    with LocalWebVitrinaFixtureServer(with_ready_snapshot=True) as base_url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1100, "height": 900})
            refresh_hits = {"count": 0}

            def fail_hidden_refresh(route: object) -> None:
                refresh_hits["count"] += 1
                route.fulfill(
                    status=500,
                    content_type="application/json",
                    body=json.dumps({"error": "hidden refresh must not run on page open"}),
                )

            context.route("**/v1/sheet-vitrina-v1/refresh", fail_hidden_refresh)

            def empty_loading_rows(route: object) -> None:
                response = route.fetch()
                payload = response.json()
                if "include_source_status=1" not in route.request.url:
                    route.fulfill(
                        status=response.status,
                        content_type="application/json",
                        body=json.dumps(payload, ensure_ascii=False),
                    )
                    return
                activity_surface = payload.get("activity_surface") or {}
                upload_summary = activity_surface.get("upload_summary") or {}
                loading_table = activity_surface.get("loading_table") or {}
                fallback = "Status payload не содержит source rows для текущего среза. Повторите загрузку или смотрите лог."
                upload_summary["subtitle"] = "Source-status details пустые."
                upload_summary["items"] = []
                upload_summary["empty_message"] = fallback
                loading_table["subtitle"] = upload_summary["subtitle"]
                loading_table["rows"] = []
                loading_table["source_status_state"] = "empty"
                loading_table["empty_message"] = fallback
                route.fulfill(
                    status=response.status,
                    content_type="application/json",
                    body=json.dumps(payload, ensure_ascii=False),
                )

            context.route(
                "**/v1/sheet-vitrina-v1/web-vitrina*",
                empty_loading_rows,
            )
            page = context.new_page()
            page.goto(base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH, wait_until="domcontentloaded")
            page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
            page.wait_for_function(
                """() => {
                  const node = document.querySelector('[data-seller-top-session]');
                  return !!node && node.textContent.includes('Сессия Seller: актуальна');
                }""",
                timeout=10000,
            )
            initial_payload = page.evaluate(
                """() => ({
                  seller_top_text: document.querySelector('[data-seller-top-session]').textContent.trim(),
                  shell_hidden: document.querySelector('[data-loading-table-shell]').classList.contains('is-hidden'),
                  empty_hidden: document.querySelector('[data-loading-table-empty]').classList.contains('is-hidden'),
                  empty_text: document.querySelector('[data-loading-table-empty]').textContent.trim(),
                  load_button: document.querySelector('[data-source-status-load]').textContent.trim(),
                  group_count: document.querySelectorAll('[data-loading-group]').length,
                  button_count: document.querySelectorAll('[data-refresh-source-group]').length,
                  source_row_count: document.querySelectorAll('[data-loading-source]').length,
                  empty_source_rows: document.querySelectorAll('[data-loading-source-empty]').length
                })"""
            )
            if (
                not initial_payload["shell_hidden"]
                or initial_payload["empty_hidden"]
                or initial_payload["load_button"] != "Загрузить"
                or initial_payload["group_count"] != 0
                or initial_payload["button_count"] != 0
                or initial_payload["source_row_count"] != 0
                or initial_payload["empty_source_rows"] != 0
                or "не OK" in initial_payload["empty_text"]
                or "Сессия Seller: актуальна" not in initial_payload["seller_top_text"]
            ):
                raise AssertionError(f"initial source status surface must be lazy/neutral, got {initial_payload}")
            if refresh_hits["count"] != 0:
                raise AssertionError(f"page open/session indicator must not trigger hidden heavy refresh, got {refresh_hits}")
            page.locator("[data-activity-block] > summary").click()
            page.wait_for_function(
                """() => {
                  const empty = document.querySelector('[data-loading-table-empty]');
                  const loadButton = document.querySelector('[data-source-status-load]');
                  return !!empty && !empty.classList.contains('is-hidden')
                    && !!loadButton && loadButton.offsetParent !== null
                    && empty.textContent.includes('Status payload не содержит source rows')
                    && document.querySelectorAll('[data-loading-source-empty]').length === 0
                    && document.querySelectorAll('[data-refresh-source-group]').length === 0;
                }""",
                timeout=5000,
            )
            print("web_vitrina_source_status_lazy_empty: ok -> open auto-load, empty details collapse")
            print("web_vitrina_seller_session_top_indicator: ok -> visible without hidden refresh")
            browser.close()


def _assert_group_action_launch_error() -> None:
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
            page.wait_for_selector("[data-activity-block]", timeout=20000)
            page.locator("[data-activity-block] > summary").click()
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

            onec_button = page.locator("[data-refresh-source-group='onec_product_capital']").first
            page.locator("[data-refresh-source-group-date='onec_product_capital']").fill(valid_refresh_date)
            with page.expect_response("**/v1/sheet-vitrina-v1/web-vitrina/group-refresh") as onec_response_info:
                onec_button.click()
            onec_response = onec_response_info.value
            onec_request_payload = json.loads(onec_response.request.post_data or "{}")
            if onec_request_payload != {
                "async": True,
                "source_group_id": "onec_product_capital",
                "as_of_date": valid_refresh_date,
            }:
                raise AssertionError(f"1C group refresh must send date-scoped payload, got {onec_request_payload}")
            page.wait_for_function(
                """() => {
                  const group = document.querySelector('[data-loading-group="onec_product_capital"]');
                  const log = document.querySelector('[data-activity-log-body]');
                  return !!group && !!log
                    && group.textContent.includes('Ошибка запуска')
                    && log.textContent.includes('Не удалось запустить обновление группы 1С за 2026-04-20: HTTP 404 route not found');
                }""",
                timeout=5000,
            )
            print("web_vitrina_group_action_unsupported_date: ok -> 2026-04-13")
            print("web_vitrina_group_action_launch_404_log: ok ->", payload["request_date"])
            print("web_vitrina_onec_group_action_payload: ok ->", onec_request_payload["as_of_date"])
            browser.close()


def _assert_seller_session_invalid_top_action() -> None:
    with LocalWebVitrinaFixtureServer(with_ready_snapshot=True) as base_url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1100, "height": 900})
            recovery_hits = {"count": 0}

            def fake_session_check(route: object) -> None:
                route.fulfill(
                    status=202,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "job_id": "session-invalid-job",
                            "job_path": "/v1/sheet-vitrina-v1/job?job_id=session-invalid-job",
                        },
                        ensure_ascii=False,
                    ),
                )

            def fake_job(route: object) -> None:
                url = route.request.url
                if "session-invalid-job" in url:
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "job_id": "session-invalid-job",
                                "operation": "session_check",
                                "status": "success",
                                "result": {
                                    "operation": "session_check",
                                    "status": "failed",
                                    "session_status": "session_invalid",
                                    "session_ok": False,
                                    "status_label": "Нужен вход",
                                    "status_tone": "error",
                                    "summary": "Сохранённая seller-сессия больше не действует.",
                                    "instruction": "Нажмите «Восстановить сессию».",
                                    "semantic_status": "error",
                                    "semantic_tone": "error",
                                },
                            },
                            ensure_ascii=False,
                        ),
                    )
                    return
                if "recovery-start-job" in url:
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "job_id": "recovery-start-job",
                                "operation": "session_recovery_start",
                                "status": "success",
                                "result": {
                                    "operation": "session_recovery_start",
                                    "status": "starting",
                                    "run_status": "starting",
                                    "status_label": "Запускаем",
                                    "status_tone": "warning",
                                    "summary": "Окно входа Seller Portal готовится.",
                                    "running": True,
                                },
                            },
                            ensure_ascii=False,
                        ),
                    )
                    return
                route.fallback()

            def fake_recovery_start(route: object) -> None:
                recovery_hits["count"] += 1
                route.fulfill(
                    status=202,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "job_id": "recovery-start-job",
                            "job_path": "/v1/sheet-vitrina-v1/job?job_id=recovery-start-job",
                        },
                        ensure_ascii=False,
                    ),
                )

            context.route("**/v1/sheet-vitrina-v1/seller-portal-session/check", fake_session_check)
            context.route("**/v1/sheet-vitrina-v1/job?*", fake_job)
            context.route("**/v1/sheet-vitrina-v1/web-vitrina/seller-portal-recovery/start", fake_recovery_start)
            page = context.new_page()
            page.goto(base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH, wait_until="domcontentloaded")
            page.wait_for_function(
                """() => {
                  const node = document.querySelector('[data-seller-top-session]');
                  return !!node
                    && node.textContent.includes('Сессия Seller: требуется вход')
                    && !!node.querySelector('[data-session-recovery-start]');
                }""",
                timeout=10000,
            )
            with page.expect_response("**/v1/sheet-vitrina-v1/web-vitrina/seller-portal-recovery/start") as response_info:
                page.locator("[data-seller-top-session] [data-session-recovery-start]").click()
            response = response_info.value
            if response.status != 202 or recovery_hits["count"] != 1:
                raise AssertionError(f"invalid seller session action must call existing recovery start route, got {response.status} / {recovery_hits}")
            print("web_vitrina_seller_session_invalid_action: ok -> top indicator exposes recovery action")
            browser.close()


if __name__ == "__main__":
    main()
