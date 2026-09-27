"""Browser regression for the Vitrina parent while Settings is selected."""

from __future__ import annotations

from pathlib import Path
import sys
from threading import Event
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer  # noqa: E402
from packages.adapters.registry_upload_http_entrypoint import DEFAULT_SHEET_WEB_VITRINA_UI_PATH  # noqa: E402


def _request_kind(url: str) -> str:
    parsed = urlsplit(url)
    base = "/v1/sheet-vitrina-v1/web-vitrina"
    if parsed.path == base:
        return "table" if parse_qs(parsed.query).get("include_table_data") == ["1"] else "shell"
    if parsed.path == base + "/business-projection/status":
        return "projection_status"
    return ""


def main() -> None:
    fixture = LocalWebVitrinaFixtureServer(with_ready_snapshot=True)
    with fixture as base_url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            requests: list[str] = []
            release_table = Event()
            page.on("request", lambda request: requests.append(kind) if (kind := _request_kind(request.url)) else None)
            try:
                page.goto(base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH + "?tab=settings", wait_until="domcontentloaded")
                page.wait_for_function(
                    """() => {
                      const active = document.querySelector('[data-unified-tab-button].is-active');
                      const frame = document.querySelector('[data-settings-embed-frame]');
                      return active?.getAttribute('data-unified-tab-button') === 'settings'
                        && (frame?.getAttribute('src') || '').includes('embedded=1');
                    }"""
                )
                page.wait_for_timeout(1800)  # Cross the initial projection poll interval.
                if requests:
                    raise AssertionError(f"Settings startup fetched hidden Vitrina data: {requests}")

                with page.expect_request(lambda request: _request_kind(request.url) == "shell"):
                    page.locator('[data-unified-tab-button="vitrina"]').click()
                page.wait_for_function(
                    """() => {
                      const panel = document.querySelector('[data-unified-tab-panel="vitrina"]');
                      return panel && !panel.hidden && document.querySelectorAll('[data-table-body] tr').length > 0;
                    }""",
                    timeout=20000,
                )
                page.wait_for_function("() => state.businessProjection.polling || !!state.businessProjection.pollTimer")
                if requests.count("shell") != 1 or requests.count("table") != 1:
                    raise AssertionError(f"Opening Vitrina must load one shell and table: {requests}")

                page.locator('[data-unified-tab-button="settings"]').click()
                page.wait_for_function(
                    """() => document.querySelector('[data-unified-tab-button].is-active')
                      ?.getAttribute('data-unified-tab-button') === 'settings'"""
                )
                before = list(requests)
                page.evaluate(
                    """async () => {
                      await pollBusinessProjectionRevision();
                      document.dispatchEvent(new Event('visibilitychange'));
                      const channel = new BroadcastChannel('wb-core:warehouse-business-projection:v1');
                      channel.postMessage({type: 'warehouse_business_projection_changed'});
                      channel.close();
                    }"""
                )
                page.wait_for_timeout(1800)
                if requests != before:
                    raise AssertionError(f"Hidden Vitrina continued polling or reloading: {requests[len(before):]}")
                if page.evaluate("() => state.businessProjection.pollTimer !== null"):
                    raise AssertionError("Hidden Vitrina retained a scheduled projection poll")

                with page.expect_request(lambda request: _request_kind(request.url) == "projection_status"):
                    page.locator('[data-unified-tab-button="vitrina"]').click()
                if requests.count("shell") != 1:
                    raise AssertionError(f"Returning to a loaded Vitrina fetched a duplicate shell: {requests}")

                # A user can switch away while the table request is already in
                # flight. Its response must not parse/render over Settings, and
                # a later return must restart the skipped table load.
                table_started = Event()
                original_composition = fixture.entrypoint.handle_sheet_web_vitrina_page_composition_request

                def hold_first_table(**kwargs: object) -> dict[str, object]:
                    if kwargs.get("include_table_data") and not table_started.is_set():
                        table_started.set()
                        if not release_table.wait(timeout=10):
                            raise AssertionError("fixture table request was not released")
                    return original_composition(**kwargs)

                fixture.entrypoint.handle_sheet_web_vitrina_page_composition_request = hold_first_table
                page.goto(base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH + "?tab=settings", wait_until="domcontentloaded")
                requests.clear()
                page.locator('[data-unified-tab-button="vitrina"]').click()
                if not table_started.wait(timeout=10):
                    raise AssertionError("visible Vitrina did not start the deferred table request")
                page.locator('[data-unified-tab-button="settings"]').click()
                release_table.set()
                page.wait_for_function("() => vitrinaDeferredTableSkipped === true", timeout=10000)
                with page.expect_request(lambda request: _request_kind(request.url) == "shell"):
                    page.locator('[data-unified-tab-button="vitrina"]').click()
                page.wait_for_function(
                    "() => document.querySelectorAll('[data-table-body] tr').length > 0",
                    timeout=20000,
                )
                if requests.count("shell") != 2 or requests.count("table") != 2:
                    raise AssertionError(f"Skipped hidden table must reload on return: {requests}")
            finally:
                release_table.set()
                browser.close()
    print("settings_lazy_browser: ok (hidden startup, visible load, paused poll, resumed poll, skipped table reload)")


if __name__ == "__main__":
    main()
