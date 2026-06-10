"""Focused browser smoke for web-vitrina period cache ownership."""

from __future__ import annotations

from pathlib import Path
import sys
import time

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import (  # noqa: E402
    LocalWebVitrinaFixtureServer,
)
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
)

DEFAULT_LABEL = "08.04.2026 - 21.04.2026"
DEFAULT_DATE_FROM = "2026-04-08"
DEFAULT_DATE_TO = "2026-04-21"
EXPLICIT_DATE_FROM = "2026-04-15"
EXPLICIT_DATE_TO = "2026-04-21"
TABLE_CACHE_KEY = "wb_core_web_vitrina_table_snapshot_v1"
LEGACY_PERIOD_KEY = "wb-core:sheet-vitrina-v1:web-vitrina:legacy-period:v0"
LEGACY_RANGE_KEY = "wb_core_web_vitrina_legacy_history_range"
BROKEN_PERIOD_KEY = "wb-core:sheet-vitrina-v1:web-vitrina:page-state:v1:period"


def main() -> None:
    with LocalWebVitrinaFixtureServer(with_ready_snapshot=True) as base_url:
        result = run_browser_check(base_url)
    print("web_vitrina_period_cache_base_url: ok ->", result["base_url"])
    print("web_vitrina_period_cache_no_query_default: ok ->", result["no_query_default"])
    print("web_vitrina_period_cache_stale_april_reset: ok ->", result["stale_april_reset"])
    print("web_vitrina_period_cache_old_query_ignored: ok ->", result["old_query_ignored"])
    print("web_vitrina_period_cache_operator_default: ok ->", result["operator_default"])
    print("web_vitrina_period_cache_explicit_period: ok ->", result["explicit_period"])


def run_browser_check(base_url: str) -> dict[str, object]:
    page_url = base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1100, "height": 900})

        def route_page_composition(route: object) -> None:
            time.sleep(0.8)
            route.continue_()

        context.route("**/v1/sheet-vitrina-v1/web-vitrina*", route_page_composition)
        page = context.new_page()
        try:
            no_query_default = _open_and_assert_no_query_default(page, page_url)
            _seed_stale_april_no_query_state(page)
            stale_april_reset = _open_and_assert_no_query_default(page, page_url)
            if stale_april_reset["cachePresent"]:
                raise AssertionError(f"no-query load must reset stale table cache, got {stale_april_reset}")
            if stale_april_reset["legacyPeriodPresent"] or stale_april_reset["legacyRangePresent"] or stale_april_reset["brokenPeriodPresent"]:
                raise AssertionError(f"no-query load must reset obsolete period state, got {stale_april_reset}")
            if stale_april_reset["freshnessBadgeCount"] != 0 or stale_april_reset["loadStatusCount"] != 1:
                raise AssertionError(
                    f"freshness badge must be absent while load-status lamp remains, got {stale_april_reset}"
                )
            old_query_ignored = _assert_old_query_ignored(page, page_url)
            operator_default = _open_and_assert_no_query_default(page, base_url + DEFAULT_SHEET_OPERATOR_UI_PATH)
            explicit_period = _assert_explicit_period(page, page_url)
        finally:
            context.close()
            browser.close()

    return {
        "base_url": base_url,
        "no_query_default": no_query_default,
        "stale_april_reset": stale_april_reset,
        "old_query_ignored": old_query_ignored,
        "operator_default": operator_default,
        "explicit_period": explicit_period,
    }


def _open_and_assert_no_query_default(page: object, page_url: str) -> dict[str, object]:
    page.goto(page_url, wait_until="commit")
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
    page.wait_for_function(
        """() => {
          const label = (document.querySelector('[data-history-label]') || {}).textContent || '';
          return label.trim() === '08.04.2026 - 21.04.2026' &&
            !window.location.search &&
            document.querySelectorAll('[data-table-body] tr').length > 0 &&
            document.querySelectorAll('[data-table-freshness-indicator]').length === 0 &&
            document.querySelectorAll('[data-table-load-status]').length === 1;
        }""",
        timeout=20000,
    )
    state = page.evaluate(
        """({tableCacheKey, legacyPeriodKey, legacyRangeKey, brokenPeriodKey}) => ({
          label: (document.querySelector('[data-history-label]') || {}).textContent || '',
          dateFrom: (document.querySelector('[data-history-date-from]') || {}).value || '',
          dateTo: (document.querySelector('[data-history-date-to]') || {}).value || '',
          query: window.location.search,
          freshnessBadgeCount: document.querySelectorAll('[data-table-freshness-indicator]').length,
          loadStatusCount: document.querySelectorAll('[data-table-load-status]').length,
          bodyText: document.body ? (document.body.innerText || '') : '',
          cachePresent: !!localStorage.getItem(tableCacheKey),
          legacyPeriodPresent: !!localStorage.getItem(legacyPeriodKey),
          legacyRangePresent: !!localStorage.getItem(legacyRangeKey),
          brokenPeriodPresent: !!localStorage.getItem(brokenPeriodKey)
        })""",
        {
            "tableCacheKey": TABLE_CACHE_KEY,
            "legacyPeriodKey": LEGACY_PERIOD_KEY,
            "legacyRangeKey": LEGACY_RANGE_KEY,
            "brokenPeriodKey": BROKEN_PERIOD_KEY,
        },
    )
    if state["label"].strip() != DEFAULT_LABEL:
        raise AssertionError(f"no-query label must use backend rolling two-week default, got {state}")
    if state["dateFrom"] != DEFAULT_DATE_FROM or state["dateTo"] != DEFAULT_DATE_TO or state["query"]:
        raise AssertionError(f"no-query fields must use backend default without URL period, got {state}")
    if state["cachePresent"]:
        raise AssertionError(f"no-query page must not persist table snapshot cache, got {state}")
    if "20.04.2026 - 24.04.2026" in state["bodyText"] or "2026-04-20..2026-04-24" in state["bodyText"]:
        raise AssertionError(f"stale April range must not be visible on no-query page, got {state}")
    if state["freshnessBadgeCount"] != 0 or state["loadStatusCount"] != 1:
        raise AssertionError(f"freshness badge must be absent while load-status lamp remains, got {state}")
    return state


def _seed_stale_april_no_query_state(page: object) -> None:
    page.evaluate(
        """async ({tableCacheKey, legacyPeriodKey, legacyRangeKey, brokenPeriodKey}) => {
          const response = await fetch('/v1/sheet-vitrina-v1/web-vitrina?surface=page_composition&history_mode=explicit&date_from=2026-04-20&date_to=2026-04-24&include_table_data=1');
          const payload = await response.json();
          localStorage.setItem(legacyPeriodKey, JSON.stringify({
            date_from: '2026-04-20',
            date_to: '2026-04-24',
            preset: 'legacy'
          }));
          localStorage.setItem(legacyRangeKey, '2026-04-20..2026-04-24');
          localStorage.setItem(brokenPeriodKey, '{broken-json');
          localStorage.setItem(tableCacheKey, JSON.stringify({
            version: 1,
            request_key: '/v1/sheet-vitrina-v1/web-vitrina?surface=page_composition',
            saved_at: '2026-04-24T12:00:00Z',
            snapshot_id: 'obsolete-april-no-query-cache',
            as_of_date: '2026-04-24',
            payload: payload
          }));
        }""",
        {
            "tableCacheKey": TABLE_CACHE_KEY,
            "legacyPeriodKey": LEGACY_PERIOD_KEY,
            "legacyRangeKey": LEGACY_RANGE_KEY,
            "brokenPeriodKey": BROKEN_PERIOD_KEY,
        },
    )


def _assert_old_query_ignored(page: object, page_url: str) -> dict[str, object]:
    old_query_url = f"{page_url}?date_from=2026-04-20&date_to=2026-04-24"
    page.goto(old_query_url, wait_until="commit")
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
    page.wait_for_function(
        """() => {
          const label = (document.querySelector('[data-history-label]') || {}).textContent || '';
          const params = new URL(window.location.href).searchParams;
          return label.trim() === '08.04.2026 - 21.04.2026' &&
            !params.has('date_from') &&
            !params.has('date_to') &&
            !params.has('history_mode') &&
            document.querySelectorAll('[data-table-body] tr').length > 0 &&
            document.querySelectorAll('[data-table-freshness-indicator]').length === 0 &&
            document.querySelectorAll('[data-table-load-status]').length === 1;
        }""",
        timeout=20000,
    )
    state = page.evaluate(
        """() => ({
          label: (document.querySelector('[data-history-label]') || {}).textContent || '',
          query: window.location.search,
          bodyText: document.body ? (document.body.innerText || '') : ''
        })"""
    )
    if "20.04.2026 - 24.04.2026" in state["bodyText"] or "2026-04-20..2026-04-24" in state["bodyText"]:
        raise AssertionError(f"legacy URL without history_mode must not render stale April range, got {state}")
    return state


def _assert_explicit_period(page: object, page_url: str) -> dict[str, object]:
    marker = "EXPLICIT-PERIOD-CACHED-TABLE"
    explicit_url = f"{page_url}?history_mode=explicit&date_from={EXPLICIT_DATE_FROM}&date_to={EXPLICIT_DATE_TO}"
    page.goto(explicit_url, wait_until="commit")
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
    page.wait_for_function(
        """() => {
          const label = (document.querySelector('[data-history-label]') || {}).textContent || '';
          return label.trim() === '15.04.2026 - 21.04.2026' &&
            !window.localStorage.getItem('wb_core_web_vitrina_table_snapshot_v1') &&
            document.querySelectorAll('[data-table-body] tr').length > 0 &&
            document.querySelectorAll('[data-table-freshness-indicator]').length === 0 &&
            document.querySelectorAll('[data-table-load-status]').length === 1;
        }""",
        timeout=20000,
    )
    state = page.evaluate(
        """({tableCacheKey, marker}) => {
          window.localStorage.setItem(tableCacheKey, JSON.stringify({
            version: 1,
            request_key: '/v1/sheet-vitrina-v1/web-vitrina?surface=page_composition&history_mode=explicit&date_from=2026-04-15&date_to=2026-04-21',
            saved_at: '2026-04-24T12:00:00Z',
            snapshot_id: 'obsolete-explicit-cache',
            as_of_date: '2026-04-24',
            payload: {table_surface: {rows: [{values: {metric_label: {display_text: marker, value: marker}}}], columns: [], groupings: []}}
          }));
          return {
            cachePresentAfterSeed: !!window.localStorage.getItem(tableCacheKey),
            label: (document.querySelector('[data-history-label]') || {}).textContent || ''
          };
        }""",
        {"tableCacheKey": TABLE_CACHE_KEY, "marker": marker},
    )
    if not state["cachePresentAfterSeed"]:
        raise AssertionError(f"smoke failed to seed explicit cache, got {state}")

    page.reload(wait_until="domcontentloaded")
    page.wait_for_function(
        """(marker) => {
          const bodyText = document.body ? (document.body.innerText || '') : '';
          return !bodyText.includes(marker) &&
            !window.localStorage.getItem('wb_core_web_vitrina_table_snapshot_v1') &&
            document.querySelectorAll('[data-table-body] tr').length > 0 &&
            document.querySelectorAll('[data-table-freshness-indicator]').length === 0 &&
            document.querySelectorAll('[data-table-load-status]').length === 1;
        }""",
        arg=marker,
        timeout=20000,
    )
    fresh_state = page.evaluate(
        """(marker) => ({
          marker_visible: (document.body.innerText || '').includes(marker),
          freshnessBadgeCount: document.querySelectorAll('[data-table-freshness-indicator]').length,
          loadStatusCount: document.querySelectorAll('[data-table-load-status]').length,
          label: (document.querySelector('[data-history-label]') || {}).textContent || '',
          query: window.location.search,
          cachePresent: !!window.localStorage.getItem('wb_core_web_vitrina_table_snapshot_v1')
        })""",
        arg=marker,
    )
    if fresh_state["marker_visible"]:
        raise AssertionError(f"explicit period cache marker must disappear after fresh payload, got {fresh_state}")
    if fresh_state["freshnessBadgeCount"] != 0 or fresh_state["loadStatusCount"] != 1:
        raise AssertionError(f"freshness badge must be absent while load-status lamp remains, got {fresh_state}")
    return {
        "cache_disabled": not fresh_state["cachePresent"],
        "fresh_state": fresh_state,
    }


if __name__ == "__main__":
    main()
