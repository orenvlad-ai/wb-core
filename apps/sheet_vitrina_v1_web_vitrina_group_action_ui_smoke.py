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


def _read_top_session_indicator_style(page: object) -> dict[str, object]:
    return page.evaluate(
        """() => {
          const node = document.querySelector('[data-seller-top-session]');
          const word = node ? node.querySelector('[data-seller-top-session-label]') : null;
          const separator = node ? node.querySelector('[data-seller-top-session-separator]') : null;
          const dot = node ? node.querySelector('.seller-top-session-dot') : null;
          const resolveColor = (value) => {
            const probe = document.createElement('span');
            probe.style.position = 'fixed';
            probe.style.left = '-9999px';
            probe.style.color = value;
            document.body.appendChild(probe);
            const color = getComputedStyle(probe).color;
            probe.remove();
            return color;
          };
          const nodeStyle = node ? getComputedStyle(node) : null;
          const wordStyle = word ? getComputedStyle(word) : null;
          const separatorStyle = separator ? getComputedStyle(separator) : null;
          const dotStyle = dot ? getComputedStyle(dot) : null;
          return {
            exists: !!node,
            tag: node ? node.tagName : '',
            role: node ? (node.getAttribute('role') || '') : '',
            full_text: node ? (node.textContent || '').trim().replace(/\\s+/g, ' ') : '',
            word_text: word ? (word.textContent || '').trim() : '',
            separator_text: separator ? (separator.textContent || '').trim() : '',
            class_name: node ? node.className : '',
            cursor: nodeStyle ? nodeStyle.cursor : '',
            container_color: nodeStyle ? nodeStyle.color : '',
            word_color: wordStyle ? wordStyle.color : '',
            separator_color: separatorStyle ? separatorStyle.color : '',
            dot_background: dotStyle ? dotStyle.backgroundColor : '',
            muted_color: resolveColor('var(--muted)'),
            success_color: resolveColor('var(--success-text)'),
            error_color: resolveColor('var(--error-text)'),
            recovery_controls_inside: node ? node.querySelectorAll('[data-session-recovery-start], [data-session-launcher], button, a').length : 0
          };
        }"""
    )


def _assert_top_session_indicator_style(page: object, expected_tone: str) -> dict[str, object]:
    payload = _read_top_session_indicator_style(page)
    expected_word_color = payload["success_color"] if expected_tone == "success" else payload["error_color"]
    if (
        not payload["exists"]
        or payload["tag"] != "SPAN"
        or payload["role"]
        or payload["word_text"] != "сессия"
        or payload["separator_text"] != "|"
        or payload["full_text"] != "сессия |"
        or f"tone-{expected_tone}" not in str(payload["class_name"])
        or "is-actionable" in str(payload["class_name"])
        or payload["cursor"] == "pointer"
        or payload["word_color"] != expected_word_color
        or payload["separator_color"] != payload["muted_color"]
        or payload["container_color"] != payload["muted_color"]
        or payload["dot_background"] != payload["muted_color"]
        or payload["recovery_controls_inside"] != 0
    ):
        raise AssertionError(f"top session indicator style mismatch for {expected_tone}, got {payload}")
    return payload


def main() -> None:
    _assert_group_controls_survive_empty_loading_rows()
    _assert_seller_session_indicator_readonly_and_manual_actions()
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
            session_check_hits = {"count": 0}

            def fail_auto_session_check(route: object) -> None:
                session_check_hits["count"] += 1
                route.fulfill(
                    status=500,
                    content_type="application/json",
                    body=json.dumps({"error": "session-check must not run on page open"}),
                )

            context.route("**/v1/sheet-vitrina-v1/seller-portal-session/check", fail_auto_session_check)

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
                  const word = node ? node.querySelector('[data-seller-top-session-label]') : null;
                  const separator = node ? node.querySelector('[data-seller-top-session-separator]') : null;
                  return !!node
                    && !!word
                    && !!separator
                    && word.textContent.trim() === 'сессия'
                    && separator.textContent.trim() === '|'
                    && node.classList.contains('tone-success')
                    && !node.querySelector('[data-session-recovery-start]');
                }""",
                timeout=10000,
            )
            initial_indicator = _assert_top_session_indicator_style(page, "success")
            initial_payload = page.evaluate(
                """() => ({
	                  seller_top_text: document.querySelector('[data-seller-top-session]').textContent.trim().replace(/\\s+/g, ' '),
	                  seller_top_class: document.querySelector('[data-seller-top-session]').className,
	                  seller_top_tag: document.querySelector('[data-seller-top-session]').tagName,
	                  seller_top_role: document.querySelector('[data-seller-top-session]').getAttribute('role') || '',
	                  seller_top_recovery_buttons: document.querySelectorAll('[data-seller-top-session] [data-session-recovery-start]').length,
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
                or initial_payload["seller_top_text"] != "сессия |"
                or initial_payload["seller_top_tag"] != "SPAN"
                or initial_payload["seller_top_role"]
                or "is-actionable" in initial_payload["seller_top_class"]
                or "tone-success" not in initial_payload["seller_top_class"]
                or initial_payload["seller_top_recovery_buttons"] != 0
            ):
                raise AssertionError(f"initial source status surface must be lazy/neutral, got {initial_payload}")
            if refresh_hits["count"] != 0:
                raise AssertionError(f"page open/session indicator must not trigger hidden heavy refresh, got {refresh_hits}")
            if session_check_hits["count"] != 0:
                raise AssertionError(f"page open must not trigger session-check for indicator, got {session_check_hits}")
            print(f"web_vitrina_seller_session_indicator_active_style: ok -> {initial_indicator}")
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
	                  session_check_controls: document.querySelectorAll('[data-session-check]').length,
	                  session_install_controls: document.querySelectorAll('[data-session-install]').length,
	                  session_recovery_controls: document.querySelectorAll('[data-session-recovery-start]').length,
	                  session_launcher_controls: document.querySelectorAll('[data-session-launcher]').length
	                })"""
            )
            if (
                payload["session_check_controls"] != 1
                or payload["session_install_controls"] != 1
                or payload["session_recovery_controls"] != 0
                or payload["session_launcher_controls"] != 0
            ):
                raise AssertionError(f"seller session controls must render only check/install actions, got {payload}")
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


def _assert_seller_session_indicator_readonly_and_manual_actions() -> None:
    with LocalWebVitrinaFixtureServer(with_ready_snapshot=True) as base_url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1100, "height": 900})
            session_check_hits = {"count": 0}
            recovery_hits = {"count": 0}
            launcher_hits = {"count": 0}

            def runtime_session_expired(route: object) -> None:
                if "/web-vitrina/seller-portal-recovery/start" in route.request.url:
                    route.fallback()
                    return
                response = route.fetch()
                payload = response.json()
                payload["status_badge"] = {
                    "label": "Ошибка",
                    "tone": "error",
                    "detail": "seller_portal_session_invalid: login_required",
                }
                status_summary = payload.setdefault("status_summary", {})
                status_summary["refresh_status"] = "error"
                status_summary["refresh_status_tone"] = "error"
                status_summary["refresh_status_reason"] = "seller_portal_session_invalid: login_redirect"
                route.fulfill(
                    status=response.status,
                    content_type="application/json",
                    body=json.dumps(payload, ensure_ascii=False),
                )

            def fake_session_check(route: object) -> None:
                session_check_hits["count"] += 1
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
	                                    "status": "success",
	                                    "session_status": "session_valid_canonical",
	                                    "session_ok": True,
	                                    "status_label": "Сессия активна",
	                                    "status_tone": "success",
	                                    "summary": "Сохранённая seller-сессия активна.",
	                                    "instruction": "",
	                                    "semantic_status": "success",
	                                    "semantic_tone": "success",
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
	                                    "status": "awaiting_login",
	                                    "run_status": "awaiting_login",
	                                    "status_label": "Нужно войти",
	                                    "status_tone": "warning",
	                                    "summary": "Окно входа Seller Portal готово.",
	                                    "running": True,
	                                    "run_id": "recovery-start-job",
	                                    "launcher_ready": True,
	                                    "can_download_launcher": True,
	                                    "launcher_enabled": True,
	                                    "launcher_download_path": "/v1/sheet-vitrina-v1/seller-portal-recovery/launcher.zip",
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

            def fake_launcher(route: object) -> None:
                launcher_hits["count"] += 1
                route.fulfill(
                    status=200,
                    content_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="seller-portal-relogin-macos.zip"'},
                    body=b"PK\x05\x06" + b"\x00" * 18,
                )

            context.route("**/v1/sheet-vitrina-v1/web-vitrina*", runtime_session_expired)
            context.route("**/v1/sheet-vitrina-v1/seller-portal-session/check", fake_session_check)
            context.route("**/v1/sheet-vitrina-v1/job?*", fake_job)
            context.route("**/v1/sheet-vitrina-v1/web-vitrina/seller-portal-recovery/start", fake_recovery_start)
            context.route("**/v1/sheet-vitrina-v1/seller-portal-recovery/launcher.zip", fake_launcher)
            page = context.new_page()
            page.goto(base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH, wait_until="domcontentloaded")
            page.wait_for_function(
                """() => {
                  const node = document.querySelector('[data-seller-top-session]');
                  const word = node ? node.querySelector('[data-seller-top-session-label]') : null;
                  const separator = node ? node.querySelector('[data-seller-top-session-separator]') : null;
                  return !!node
                    && !!word
                    && !!separator
                    && word.textContent.trim() === 'сессия'
                    && separator.textContent.trim() === '|'
                    && node.classList.contains('tone-error')
                    && !node.classList.contains('is-actionable')
                    && !node.querySelector('[data-session-recovery-start]');
                }""",
                timeout=10000,
            )
            expired_indicator = _assert_top_session_indicator_style(page, "error")
            page.locator("[data-seller-top-session]").click()
            page.wait_for_timeout(300)
            if session_check_hits["count"] != 0 or recovery_hits["count"] != 0:
                raise AssertionError(
                    "read-only top indicator must not call session/recovery routes, "
                    f"got session={session_check_hits} recovery={recovery_hits}"
                )
            print(f"web_vitrina_seller_session_indicator_expired_style: ok -> {expired_indicator}")
            page.locator("[data-activity-block] > summary").click()
            page.wait_for_selector("[data-session-check]", timeout=10000)
            controls_payload = page.evaluate(
                """() => ({
                  check: document.querySelectorAll('[data-session-check]').length,
                  install: document.querySelectorAll('[data-session-install]').length,
                  recovery: document.querySelectorAll('[data-session-recovery-start]').length,
                  launcher: document.querySelectorAll('[data-session-launcher]').length,
                  refresh_group: document.querySelectorAll('[data-refresh-source-group]').length
                })"""
            )
            if (
                controls_payload["check"] != 1
                or controls_payload["install"] != 1
                or controls_payload["recovery"] != 0
                or controls_payload["launcher"] != 0
            ):
                raise AssertionError(f"session action block must expose only check/install, got {controls_payload}")
            if controls_payload["refresh_group"] < 1:
                raise AssertionError(f"group refresh actions must remain available, got {controls_payload}")

            with page.expect_response("**/v1/sheet-vitrina-v1/seller-portal-session/check") as check_response_info:
                page.locator("[data-session-check]").click()
            check_response = check_response_info.value
            if check_response.status != 202 or session_check_hits["count"] != 1:
                raise AssertionError(f"manual check must call session-check once, got {check_response.status} / {session_check_hits}")
            page.wait_for_function(
                """() => {
                  const node = document.querySelector('[data-seller-top-session]');
                  const word = node ? node.querySelector('[data-seller-top-session-label]') : null;
                  const separator = node ? node.querySelector('[data-seller-top-session-separator]') : null;
                  return !!node
                    && !!word
                    && !!separator
                    && word.textContent.trim() === 'сессия'
                    && separator.textContent.trim() === '|'
                    && node.classList.contains('tone-success')
                    && !node.classList.contains('is-actionable');
                }""",
                timeout=5000,
            )
            _assert_top_session_indicator_style(page, "success")
            with page.expect_response("**/v1/sheet-vitrina-v1/web-vitrina/seller-portal-recovery/start") as recovery_response_info:
                page.locator("[data-session-install]").click()
            recovery_response = recovery_response_info.value
            if recovery_response.status != 202 or recovery_hits["count"] != 1:
                raise AssertionError(f"install action must start recovery once, got {recovery_response.status} / {recovery_hits}")
            page.wait_for_function(
                """() => document.querySelector('[data-activity-log-body]').textContent.includes('Launcher для Seller Portal recovery скачан')""",
                timeout=5000,
            )
            if launcher_hits["count"] != 1:
                raise AssertionError(f"install action must download launcher once, got {launcher_hits}")
            post_install_controls = page.evaluate(
                """() => ({
                  check: document.querySelectorAll('[data-session-check]').length,
                  install: document.querySelectorAll('[data-session-install]').length,
                  recovery: document.querySelectorAll('[data-session-recovery-start]').length,
                  launcher: document.querySelectorAll('[data-session-launcher]').length
                })"""
            )
            if post_install_controls != {"check": 1, "install": 1, "recovery": 0, "launcher": 0}:
                raise AssertionError(f"install must not render extra session buttons, got {post_install_controls}")
            print("web_vitrina_seller_session_indicator_readonly: ok -> runtime red without auto-check/click action")
            print("web_vitrina_seller_session_manual_check: ok -> manual check updates indicator")
            print("web_vitrina_seller_session_install_download: ok -> install downloads launcher without extra buttons")
            browser.close()


if __name__ == "__main__":
    main()
