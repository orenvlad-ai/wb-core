#!/usr/bin/env python3
"""Browser regressions for the maintenance write-barrier UI state machine."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

from playwright.sync_api import Browser, Page, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    _inject_business_data_write_barrier_ui,
)


CONTRACT_NAME = "wb_core_business_data_write_barrier_v1"


def _status(*, active: bool, phase: str | None = None) -> dict[str, Any]:
    resolved_phase = phase or ("held" if active else "inactive")
    return {
        "contract_name": CONTRACT_NAME,
        "status": "active" if active else "inactive",
        "active": active,
        "phase": resolved_phase,
        "message": (
            "Короткое техническое обслуживание: изменения временно заблокированы."
            if active
            else ""
        ),
    }


def _invalid_fail_closed_status() -> dict[str, Any]:
    return {
        "contract_name": CONTRACT_NAME,
        "status": "invalid_fail_closed",
        "active": True,
        "phase": "invalid",
        "message": "Состояние защитного барьера не подтверждено.",
    }


def _fixture_html(
    steps: list[dict[str, Any]],
    *,
    poll_interval_ms: int = 30,
    request_timeout_ms: int = 150,
    max_backoff_ms: int = 240,
    hidden_poll_interval_ms: int = 240,
) -> str:
    steps_json = json.dumps(steps, ensure_ascii=False, separators=(",", ":"))
    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Write barrier browser smoke</title>
  <script>
    window.__barrierHidden = false;
    Object.defineProperty(document, "hidden", {{
      configurable: true,
      get: () => window.__barrierHidden
    }});
    Object.defineProperty(document, "visibilityState", {{
      configurable: true,
      get: () => window.__barrierHidden ? "hidden" : "visible"
    }});
    window.__barrierFetch = {{
      steps: {steps_json}, calls: 0, active: 0, maxActive: 0
    }};
    window.fetch = (_url, options = {{}}) => {{
      const state = window.__barrierFetch;
      const index = state.calls++;
      const step = state.steps[Math.min(index, state.steps.length - 1)];
      state.active += 1;
      state.maxActive = Math.max(state.maxActive, state.active);
      return new Promise((resolve, reject) => {{
        let settled = false;
        const finish = (callback, value) => {{
          if (settled) return;
          settled = true;
          state.active -= 1;
          callback(value);
        }};
        const timer = window.setTimeout(() => {{
          if (step.kind === "error") {{
            finish(reject, new Error("synthetic status failure"));
            return;
          }}
          finish(resolve, {{
            ok: step.kind !== "http_error",
            json: async () => step.payload
          }});
        }}, Number(step.delay_ms || 0));
        const abort = () => {{
          window.clearTimeout(timer);
          finish(reject, new DOMException("Aborted", "AbortError"));
        }};
        const signal = options.signal;
        if (signal) {{
          if (signal.aborted) abort();
          else signal.addEventListener("abort", abort, {{once: true}});
        }}
      }});
    }};
  </script>
</head>
<body>
  <form method="post">
    <button id="barrierOnly" type="button">Изменить</button>
    <button id="appDynamic" type="button">Динамический</button>
    <button id="nativeDisabled" type="button" disabled>Недоступно приложению</button>
  </form>
  <script>
    window.__barrierClickCount = 0;
    document.getElementById("barrierOnly").addEventListener("click", () => {{
      window.__barrierClickCount += 1;
    }});
  </script>
</body>
</html>"""
    return _inject_business_data_write_barrier_ui(
        html,
        poll_interval_ms=poll_interval_ms,
        request_timeout_ms=request_timeout_ms,
        max_backoff_ms=max_backoff_ms,
        hidden_poll_interval_ms=hidden_poll_interval_ms,
        expose_test_api=True,
    )


def _new_page(browser: Browser, steps: list[dict[str, Any]], **config: int) -> Page:
    page = browser.new_page()
    page.set_content(_fixture_html(steps, **config), wait_until="domcontentloaded")
    page.wait_for_function("window.__wbCoreMaintenanceBarrierTest !== undefined")
    return page


def _snapshot(page: Page) -> dict[str, Any]:
    return page.evaluate("window.__wbCoreMaintenanceBarrierTest.snapshot()")


def _assert_initial_unknown_and_delayed_inactive(browser: Browser) -> None:
    page = _new_page(
        browser,
        [{"kind": "success", "delay_ms": 120, "payload": _status(active=False)}],
        poll_interval_ms=1_000,
        request_timeout_ms=500,
        max_backoff_ms=1_000,
        hidden_poll_interval_ms=1_000,
    )
    try:
        assert _snapshot(page)["confirmed"] is None
        assert page.locator("#wbCoreMaintenanceBarrier").is_hidden()
        assert page.locator("#barrierOnly").is_enabled()
        assert page.locator("#barrierOnly").get_attribute(
            "data-wb-core-maintenance-disabled"
        ) is None
        page.wait_for_function(
            "window.__wbCoreMaintenanceBarrierTest.snapshot().confirmed === false"
        )
        assert page.locator("#wbCoreMaintenanceBarrier").is_hidden()
        assert page.locator("#barrierOnly").is_enabled()
    finally:
        page.close()


def _assert_error_after_inactive_preserves_open_ui(browser: Browser) -> None:
    page = _new_page(
        browser,
        [
            {"kind": "success", "payload": _status(active=False)},
            {"kind": "error"},
        ],
    )
    try:
        page.wait_for_function(
            "window.__wbCoreMaintenanceBarrierTest.snapshot().confirmed === false"
        )
        page.wait_for_function("window.__barrierFetch.calls >= 2")
        page.wait_for_timeout(20)
        snapshot = _snapshot(page)
        assert snapshot["confirmed"] is False
        assert snapshot["blocked"] is False
        assert page.locator("#wbCoreMaintenanceBarrier").is_hidden()
        assert page.locator("#barrierOnly").is_enabled()
    finally:
        page.close()


def _assert_active_and_error_preserve_closed_ui(browser: Browser) -> None:
    page = _new_page(
        browser,
        [
            {"kind": "success", "payload": _status(active=True)},
            {"kind": "error"},
        ],
    )
    try:
        page.wait_for_function(
            "window.__wbCoreMaintenanceBarrierTest.snapshot().confirmed === true"
        )
        banner = page.locator("#wbCoreMaintenanceBarrier")
        assert banner.is_visible()
        assert banner.get_attribute("data-tone") == "warning"
        assert page.locator("#barrierOnly").get_attribute(
            "data-wb-core-maintenance-disabled"
        ) == "1"
        page.locator("#barrierOnly").dispatch_event("click")
        assert page.evaluate("window.__barrierClickCount") == 0
        page.wait_for_function("window.__barrierFetch.calls >= 2")
        page.wait_for_timeout(20)
        assert _snapshot(page)["blocked"] is True
        assert banner.get_attribute("data-tone") == "warning"
    finally:
        page.close()


def _assert_invalid_contract_is_danger_but_malformed_is_unknown(
    browser: Browser,
) -> None:
    invalid = _new_page(
        browser,
        [{"kind": "success", "payload": _invalid_fail_closed_status()}],
        poll_interval_ms=1_000,
        max_backoff_ms=1_000,
        hidden_poll_interval_ms=1_000,
    )
    try:
        invalid.wait_for_function(
            "window.__wbCoreMaintenanceBarrierTest.snapshot().blocked === true"
        )
        assert invalid.locator("#wbCoreMaintenanceBarrier").get_attribute(
            "data-tone"
        ) == "danger"
    finally:
        invalid.close()

    malformed = _new_page(
        browser,
        [
            {
                "kind": "success",
                "payload": {"status": "active", "active": True, "phase": "held"},
            }
        ],
        poll_interval_ms=1_000,
        max_backoff_ms=1_000,
        hidden_poll_interval_ms=1_000,
    )
    try:
        malformed.wait_for_function("window.__barrierFetch.calls >= 1")
        malformed.wait_for_timeout(20)
        assert _snapshot(malformed)["confirmed"] is None
        assert malformed.locator("#wbCoreMaintenanceBarrier").is_hidden()
        assert malformed.locator("#barrierOnly").is_enabled()
    finally:
        malformed.close()


def _assert_released_transition_preserves_application_disabled_state(
    browser: Browser,
) -> None:
    page = _new_page(
        browser,
        [
            {"kind": "success", "payload": _status(active=True)},
            {
                "kind": "success",
                "payload": _status(active=False, phase="released"),
            },
        ],
        poll_interval_ms=80,
    )
    try:
        page.wait_for_function(
            "window.__wbCoreMaintenanceBarrierTest.snapshot().confirmed === true"
        )
        page.evaluate(
            """
            document.getElementById("appDynamic").disabled = true;
            const dynamic = document.createElement("button");
            dynamic.id = "addedWhileHeld";
            dynamic.textContent = "Добавлен во время обслуживания";
            document.body.appendChild(dynamic);
            """
        )
        page.wait_for_function(
            "document.getElementById('addedWhileHeld').dataset.wbCoreMaintenanceDisabled === '1'"
        )
        page.wait_for_function(
            "window.__barrierFetch.calls >= 2 && "
            "window.__wbCoreMaintenanceBarrierTest.snapshot().confirmed === false"
        )
        assert page.locator("#wbCoreMaintenanceBarrier").is_hidden()
        assert page.locator("#barrierOnly").is_enabled()
        assert page.locator("#appDynamic").is_disabled()
        assert page.locator("#nativeDisabled").is_disabled()
        assert page.locator("#addedWhileHeld").is_enabled()
        for selector in ("#barrierOnly", "#appDynamic", "#addedWhileHeld"):
            assert page.locator(selector).get_attribute(
                "data-wb-core-maintenance-disabled"
            ) is None
    finally:
        page.close()


def _assert_stale_response_cannot_overwrite_newer_confirmation(
    browser: Browser,
) -> None:
    page = _new_page(
        browser,
        [{"kind": "success", "delay_ms": 1_000, "payload": _status(active=False)}],
        poll_interval_ms=1_000,
        request_timeout_ms=1_000,
        max_backoff_ms=1_000,
        hidden_poll_interval_ms=1_000,
    )
    try:
        result = page.evaluate(
            """([activePayload, inactivePayload]) => {
              const api = window.__wbCoreMaintenanceBarrierTest;
              const newer = api.commit(activePayload, 100);
              const stale = api.commit(inactivePayload, 99);
              return {newer, stale, snapshot: api.snapshot()};
            }""",
            [_status(active=True), _status(active=False)],
        )
        assert result["newer"] is True
        assert result["stale"] is False
        assert result["snapshot"]["blocked"] is True
        assert page.locator("#wbCoreMaintenanceBarrier").is_visible()
    finally:
        page.close()


def _assert_timeout_single_flight_and_hidden_tab_no_storm(browser: Browser) -> None:
    page = _new_page(
        browser,
        [
            {"kind": "success", "delay_ms": 80, "payload": _status(active=True)},
            {"kind": "success", "delay_ms": 5, "payload": _status(active=False)},
        ],
        poll_interval_ms=15,
        request_timeout_ms=25,
        max_backoff_ms=80,
        hidden_poll_interval_ms=250,
    )
    try:
        page.wait_for_function(
            "window.__barrierFetch.calls >= 2 && "
            "window.__wbCoreMaintenanceBarrierTest.snapshot().confirmed === false"
        )
        assert page.evaluate("window.__barrierFetch.maxActive") == 1
        page.evaluate(
            """
            window.__barrierHidden = true;
            document.dispatchEvent(new Event("visibilitychange"));
            """
        )
        page.wait_for_function(
            "window.__wbCoreMaintenanceBarrierTest.snapshot().inFlight === false"
        )
        hidden_calls = page.evaluate("window.__barrierFetch.calls")
        page.wait_for_timeout(100)
        assert page.evaluate("window.__barrierFetch.calls") == hidden_calls
        page.evaluate(
            """
            window.__barrierHidden = false;
            document.dispatchEvent(new Event("visibilitychange"));
            """
        )
        page.wait_for_function(
            f"window.__barrierFetch.calls > {int(hidden_calls)}"
        )
        assert page.evaluate("window.__barrierFetch.maxActive") == 1
    finally:
        page.close()


def main() -> int:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            _assert_initial_unknown_and_delayed_inactive(browser)
            _assert_error_after_inactive_preserves_open_ui(browser)
            _assert_active_and_error_preserve_closed_ui(browser)
            _assert_invalid_contract_is_danger_but_malformed_is_unknown(browser)
            _assert_released_transition_preserves_application_disabled_state(browser)
            _assert_stale_response_cannot_overwrite_newer_confirmation(browser)
            _assert_timeout_single_flight_and_hidden_tab_no_storm(browser)
        finally:
            browser.close()
    print("business_data_write_barrier_browser_smoke: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
