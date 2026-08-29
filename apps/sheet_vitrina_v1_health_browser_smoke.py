#!/usr/bin/env python3
"""Focused browser smoke for the Web Vitrina health indicators and recovery UI."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys
import threading

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import (  # noqa: E402
    LocalWebVitrinaFixtureServer,
)
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
)


NOW = datetime(2026, 4, 21, 3, 0, tzinfo=timezone.utc)  # 08:00 Asia/Yekaterinburg
TODAY = "2026-04-21"
YESTERDAY = "2026-04-20"


def _payload(
    *,
    yesterday_state: str,
    bot_state: str = "ok",
    recovery: str = "none",
) -> dict[str, object]:
    matrix: list[dict[str, object]] = [
        {
            "source_group_id": "wb_api",
            "source_key": "fin_report_daily",
            "date_role": "yesterday_closed",
            "target_date": YESTERDAY,
            "expectation_state": "complete" if yesterday_state == "ok" else "partial",
            "requested_count": 2,
            "covered_count": 2 if yesterday_state == "ok" else 1,
        },
        {
            "source_group_id": "other_sources",
            "source_key": "sku_action_events",
            "date_role": "today_current",
            "target_date": TODAY,
            "expectation_state": "no_events",
            "requested_count": 0,
            "covered_count": 0,
        },
        {
            "source_group_id": "wb_api",
            "source_key": "stocks",
            "date_role": "today_current",
            "target_date": TODAY,
            "expectation_state": "inapplicable",
            "requested_count": 0,
            "covered_count": 0,
        },
    ]
    actions: list[dict[str, object]] = []
    if recovery == "unsupported":
        matrix.append(
            {
                "source_group_id": "wb_public_card_bot",
                "source_key": "spp_proxy",
                "date_role": "yesterday_closed",
                "target_date": YESTERDAY,
                "expectation_state": "partial",
                "requested_count": 2,
                "covered_count": 1,
            }
        )
        actions.append(
            {
                "source_group_id": "wb_public_card_bot",
                "target_date": YESTERDAY,
                "gap_source_keys": ["spp_proxy"],
                "recoverable_source_keys": [],
                "hook": "none",
                "apply_allowed": False,
                "action_fingerprint": "sha256:browser-unsupported",
            }
        )
    if recovery == "supported":
        matrix.append(
            {
                "source_group_id": "wb_api",
                "source_key": "sales_funnel_history",
                "date_role": "yesterday_closed",
                "target_date": YESTERDAY,
                "expectation_state": "missing",
                "requested_count": 2,
                "covered_count": 0,
            }
        )
        actions.append(
            {
                "source_group_id": "wb_api",
                "target_date": YESTERDAY,
                "gap_source_keys": ["sales_funnel_history"],
                "recoverable_source_keys": ["sales_funnel_history"],
                "hook": "group_refresh",
                "apply_allowed": True,
                "action_fingerprint": "sha256:browser-supported",
            }
        )
    return {
        "contract": "sheet_vitrina_v1_web_health/v1",
        "business_date": TODAY,
        "yesterday_date": YESTERDAY,
        "fingerprint": "sha256:browser-payload-" + recovery + "-" + yesterday_state + "-" + bot_state,
        "expectation_matrix": matrix,
        "signals": {
            "yesterday_closed": {
                "state": yesterday_state,
                "problem_count": 0 if yesterday_state == "ok" else 1,
                "problem_sources": [] if yesterday_state == "ok" else ["fin_report_daily"],
            },
            "today_current": {"state": "ok", "problem_count": 0, "problem_sources": []},
            "bot_health": {
                "state": bot_state,
                "confirmed_problem_count": 0 if bot_state == "ok" else 1,
                "confirmed_problems": [] if bot_state == "ok" else ["seller_portal_session:invalid"],
            },
        },
        "recovery_preview": {
            "status": "recovery_needed" if actions else "closed",
            "target_date": YESTERDAY,
            "gap_count": len(actions),
            "plan_fingerprint": "sha256:browser-plan-" + recovery,
            "actions": actions,
        },
    }


def _save_observation(runtime, *, observation_id: str, phase: str, observed_at: str, payload: dict[str, object]) -> None:
    runtime.save_sheet_vitrina_health_observation(
        observation_id=observation_id,
        business_date=TODAY,
        phase=phase,
        observed_at=observed_at,
        ready_snapshot_id="web-vitrina-fixture",
        payload_fingerprint=str(payload["fingerprint"]),
        payload=deepcopy(payload),
    )


def _indicator_states(page) -> dict[str, str]:
    return page.evaluate(
        """() => Object.fromEntries(Array.from(document.querySelectorAll('[data-health-indicator]')).map(node => [node.getAttribute('data-health-indicator'), node.getAttribute('data-health-state')]))"""
    )


def main() -> None:
    fixture = LocalWebVitrinaFixtureServer(with_ready_snapshot=True, now=NOW)
    base_url = fixture.__enter__()
    try:
        runtime = fixture.entrypoint.runtime
        launches: list[dict[str, object]] = []
        launch_lock = threading.Lock()

        def _start_recovery(*, source_group_id: str, as_of_date: str | None = None, health_recovery=None):
            with launch_lock:
                launches.append(
                    {
                        "source_group_id": source_group_id,
                        "as_of_date": as_of_date,
                        "health_recovery": dict(health_recovery or {}),
                    }
                )

            def _runner(log):
                log("fixture recovery completed")
                terminal_payload = _payload(yesterday_state="incomplete", recovery="supported")
                _save_observation(
                    runtime,
                    observation_id="health-browser-recovery-terminal",
                    phase="recovery",
                    observed_at="2026-04-21T03:10:00Z",
                    payload=terminal_payload,
                )
                return {
                    "as_of_date": as_of_date,
                    "source_group_id": source_group_id,
                    "health_recovery": {
                        "semantic_yesterday_state": "incomplete",
                        "observation_id": "health-browser-recovery-terminal",
                    },
                }

            return fixture.entrypoint.operator_jobs.start(operation="refresh_group", runner=_runner)

        fixture.entrypoint.start_sheet_source_group_refresh_job = _start_recovery

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            page.goto(base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH, wait_until="domcontentloaded")
            page.wait_for_selector('[data-health-indicator][data-health-state="observing"]', timeout=20000)
            if _indicator_states(page) != {
                "yesterday_closed": "observing",
                "today_current": "observing",
                "bot_health": "observing",
            }:
                raise AssertionError("pre-first-pair health indicators must remain neutral observing")
            if page.locator('[data-health-indicator][aria-haspopup="dialog"]').count() != 3:
                raise AssertionError("all health indicators must expose accessible dialog semantics")

            opener = page.locator('[data-health-indicator="yesterday_closed"]')
            opener.focus()
            opener.press("Enter")
            modal = page.locator("[data-health-modal]")
            modal.wait_for(state="visible")
            if page.locator('[role="dialog"][aria-modal="true"]#vitrina-health-dialog').count() != 1:
                raise AssertionError("health details must use one accessible modal dialog")
            if "Штатных наблюдений" not in page.locator("[data-health-modal-summary]").inner_text():
                raise AssertionError("pre-pair dialog must explain that observations are still pending")
            page.keyboard.press("Escape")
            if not modal.is_hidden() or not opener.evaluate("node => node === document.activeElement"):
                raise AssertionError("Escape must close health details and restore focus")

            page.set_viewport_size({"width": 560, "height": 820})
            before_top = page.locator("[data-table-header]").bounding_box()["y"]
            opener.click()
            after_top = page.locator("[data-table-header]").bounding_box()["y"]
            narrow_layout = page.evaluate(
                """() => ({
                  overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                  loadVisible: !!document.querySelector('[data-load-refresh-button]') && document.querySelector('[data-load-refresh-button]').offsetParent !== null,
                  healthCount: document.querySelectorAll('[data-health-indicator]').length
                })"""
            )
            page.keyboard.press("Escape")
            if narrow_layout["overflow"] > 1 or not narrow_layout["loadVisible"] or narrow_layout["healthCount"] != 3:
                raise AssertionError(f"narrow health cluster must not create a page overflow or hide load controls: {narrow_layout}")
            if abs(after_top - before_top) > 1:
                raise AssertionError("opening health dialog must not move the Vitrina table header")
            page.set_viewport_size({"width": 1440, "height": 900})

            incomplete = _payload(yesterday_state="incomplete", recovery="unsupported")
            _save_observation(runtime, observation_id="health-browser-candidate", phase="candidate", observed_at="2026-04-21T01:30:00Z", payload=incomplete)
            _save_observation(runtime, observation_id="health-browser-confirmation", phase="confirmation", observed_at="2026-04-21T02:30:00Z", payload=incomplete)
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector('[data-health-indicator="yesterday_closed"][data-health-state="incomplete"]')
            states = _indicator_states(page)
            if states != {"yesterday_closed": "incomplete", "today_current": "observing", "bot_health": "ok"}:
                raise AssertionError(f"confirmed incomplete/neutral/BOT split is wrong: {states}")
            page.locator('[data-health-indicator="yesterday_closed"]').click()
            recovery_text = page.locator("[data-health-recovery-list]").inner_text()
            if "Автоматическое историческое восстановление недоступно" not in recovery_text:
                raise AssertionError("unsupported spp_proxy must explain why historical recovery is unavailable")
            if page.locator("[data-health-recovery-index]").count() != 0:
                raise AssertionError("unsupported spp_proxy must not expose a runnable recovery button")
            page.keyboard.press("Escape")

            bot_failure = _payload(yesterday_state="ok", bot_state="error")
            _save_observation(runtime, observation_id="health-browser-bot-failure", phase="recovery", observed_at="2026-04-21T02:40:00Z", payload=bot_failure)
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector('[data-health-indicator="bot_health"][data-health-state="error"]')
            states = _indicator_states(page)
            if states["yesterday_closed"] != "ok" or states["bot_health"] != "error":
                raise AssertionError(f"BOT failure must remain independent of yesterday: {states}")

            recoverable = _payload(yesterday_state="incomplete", recovery="supported")
            _save_observation(runtime, observation_id="health-browser-recoverable", phase="recovery", observed_at="2026-04-21T02:50:00Z", payload=recoverable)
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector('[data-health-indicator="yesterday_closed"][data-health-state="incomplete"]')
            health_read = page.request.get(base_url + "/v1/sheet-vitrina-v1/web-vitrina/health").json()
            current_action = health_read["recovery_preview"]["actions"][0]
            stale_response = page.request.post(
                base_url + "/v1/sheet-vitrina-v1/web-vitrina/health/recovery/start",
                data={
                    "observation_id": "stale-observation",
                    "plan_fingerprint": health_read["recovery_preview"]["plan_fingerprint"],
                    "action_fingerprint": current_action["action_fingerprint"],
                    "source_group_id": current_action["source_group_id"],
                    "target_date": current_action["target_date"],
                },
            )
            if stale_response.status != 409 or stale_response.json().get("reason") != "stale_health_plan":
                raise AssertionError("stale recovery identity must fail closed through the HTTP route")
            page.locator('[data-health-indicator="yesterday_closed"]').click()
            preview_button = page.locator("[data-health-recovery-index]")
            if preview_button.count() != 1:
                raise AssertionError("exact supported recovery preview must expose one confirmation button")
            preview_button.click()
            confirmation = page.locator("[data-health-recovery-confirmation]")
            confirmation.wait_for(state="visible")
            if "технический успех" not in confirmation.inner_text().lower():
                raise AssertionError("confirmation must explain semantic health readback")
            page.evaluate(
                """() => {
                  const button = document.querySelector('[data-health-recovery-confirm]');
                  button.click();
                  button.click();
                }"""
            )
            page.wait_for_function(
                """() => (document.querySelector('[data-health-recovery-result]') || {}).textContent.includes('Серверная проверка')""",
                timeout=20000,
            )
            if len(launches) != 1:
                raise AssertionError(f"double confirmation must submit at most one recovery, got {launches}")
            final_states = _indicator_states(page)
            if final_states["yesterday_closed"] == "ok":
                raise AssertionError("technical recovery completion must not override semantic incomplete")
            if page.locator("[data-health-recovery-index]").count() != 0:
                raise AssertionError("durably submitted action must not remain runnable")
            browser.close()
    finally:
        fixture.__exit__(None, None, None)

    print(
        {
            "status": "ok",
            "checks": [
                "pre_first_pair_observing",
                "accessible_focus_modal",
                "wide_narrow_no_jump",
                "yesterday_incomplete_bot_ok",
                "current_before_boundary_observing",
                "bot_failure_independent",
                "unsupported_preview_only",
                "stale_http_409",
                "recovery_confirm_one_job_reread",
                "technical_success_semantic_incomplete",
            ],
        }
    )


if __name__ == "__main__":
    main()
