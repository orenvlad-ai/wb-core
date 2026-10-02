"""A filter selected while the metadata shell is visible survives table arrival."""

from __future__ import annotations

from pathlib import Path
import sys
from threading import Event

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer
from packages.adapters.registry_upload_http_entrypoint import DEFAULT_SHEET_WEB_VITRINA_UI_PATH


def main() -> None:
    table_started = Event()
    release_table = Event()
    fixture = LocalWebVitrinaFixtureServer(with_ready_snapshot=True)
    with fixture as base_url:
        original = fixture.entrypoint.handle_sheet_web_vitrina_page_composition_request

        def hold_table(**kwargs):
            if kwargs.get("include_table_data") and not table_started.is_set():
                table_started.set()
                if not release_table.wait(timeout=15):
                    raise AssertionError("fixture table request was not released")
            return original(**kwargs)

        fixture.entrypoint.handle_sheet_web_vitrina_page_composition_request = hold_table
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    page.goto(base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH, wait_until="domcontentloaded")
                    if not table_started.wait(timeout=15):
                        raise AssertionError("deferred table request did not start")
                    page.wait_for_selector('[data-filter-control="section"]', state="attached", timeout=10000)
                    page.locator('[data-filters-toggle]').click()
                    page.locator('[data-filter-control="section"]').select_option("section:Воронка")
                    release_table.set()
                    page.wait_for_function(
                        "() => state.composition?.table_surface?.table_data_state === 'included'",
                        timeout=20000,
                    )
                    result = page.evaluate("""() => ({
                      selected: state.filters.section,
                      control: document.querySelector('[data-filter-control="section"]')?.value,
                      rows: applyFilters(state.composition.table_surface.rows).length,
                      total: state.composition.table_surface.total_row_count
                    })""")
                    if result["selected"] != "section:Воронка" or result["control"] != "section:Воронка":
                        raise AssertionError(f"shell filter was reset when table arrived: {result}")
                    if not 0 < result["rows"] < result["total"]:
                        raise AssertionError(f"selected section did not filter the complete table: {result}")
                    print("deferred_filter_browser: ok ->", result)
                finally:
                    browser.close()
        finally:
            release_table.set()


if __name__ == "__main__":
    main()
