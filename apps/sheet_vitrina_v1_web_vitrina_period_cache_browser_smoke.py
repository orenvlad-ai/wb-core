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
            if "is-stale-loading" in stale_april_reset["freshnessClass"] or "is-stale-error" in stale_april_reset["freshnessClass"]:
                raise AssertionError(f"no-query load must not be driven by stale cache freshness, got {stale_april_reset}")
            explicit_period = _assert_explicit_period_cache(page, page_url)
        finally:
            context.close()
            browser.close()

    return {
        "base_url": base_url,
        "no_query_default": no_query_default,
        "stale_april_reset": stale_april_reset,
        "explicit_period": explicit_period,
    }


def _open_and_assert_no_query_default(page: object, page_url: str) -> dict[str, object]:
    page.goto(page_url, wait_until="commit")
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
    page.wait_for_function(
        """() => {
          const indicator = document.querySelector('[data-table-freshness-indicator]');
          const label = (document.querySelector('[data-history-label]') || {}).textContent || '';
          return !!indicator &&
            indicator.classList.contains('is-fresh') &&
            label.trim() === '08.04.2026 - 21.04.2026' &&
            !window.location.search;
        }""",
        timeout=20000,
    )
    state = page.evaluate(
        """({tableCacheKey, legacyPeriodKey, legacyRangeKey, brokenPeriodKey}) => ({
          label: (document.querySelector('[data-history-label]') || {}).textContent || '',
          dateFrom: (document.querySelector('[data-history-date-from]') || {}).value || '',
          dateTo: (document.querySelector('[data-history-date-to]') || {}).value || '',
          query: window.location.search,
          freshnessClass: ((document.querySelector('[data-table-freshness-indicator]') || {}).getAttribute('class') || ''),
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
    return state


def _seed_stale_april_no_query_state(page: object) -> None:
    page.evaluate(
        """async ({tableCacheKey, legacyPeriodKey, legacyRangeKey, brokenPeriodKey}) => {
          const response = await fetch('/v1/sheet-vitrina-v1/web-vitrina?surface=page_composition&date_from=2026-04-20&date_to=2026-04-24&include_table_data=1');
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


def _assert_explicit_period_cache(page: object, page_url: str) -> dict[str, object]:
    marker = "EXPLICIT-PERIOD-CACHED-TABLE"
    explicit_url = f"{page_url}?date_from={EXPLICIT_DATE_FROM}&date_to={EXPLICIT_DATE_TO}"
    page.goto(explicit_url, wait_until="commit")
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
    page.wait_for_function(
        """(tableCacheKey) => {
          const indicator = document.querySelector('[data-table-freshness-indicator]');
          return !!indicator &&
            indicator.classList.contains('is-fresh') &&
            !!window.localStorage.getItem(tableCacheKey);
        }""",
        arg=TABLE_CACHE_KEY,
        timeout=20000,
    )
    cache_state = page.evaluate(
        """({tableCacheKey, marker}) => {
          const raw = window.localStorage.getItem(tableCacheKey);
          const cached = JSON.parse(raw || '{}');
          const payload = cached.payload || {};
          const table = payload.table_surface || {};
          const rows = Array.isArray(table.rows) ? table.rows : [];
          const row = rows[0] || {};
          const values = row.values || {};
          const cell = values.metric_label || values.scope_label || Object.values(values)[0];
          if (cell) {
            cell.display_text = marker;
            cell.value = marker;
            row.search_text = String(row.search_text || '') + ' ' + marker;
          }
          window.localStorage.setItem(tableCacheKey, JSON.stringify(cached));
          return {
            request_key: cached.request_key || '',
            row_count: rows.length,
            label: (document.querySelector('[data-history-label]') || {}).textContent || ''
          };
        }""",
        {"tableCacheKey": TABLE_CACHE_KEY, "marker": marker},
    )
    if f"date_from={EXPLICIT_DATE_FROM}" not in cache_state["request_key"]:
        raise AssertionError(f"explicit period cache must be scoped by URL period, got {cache_state}")
    if not cache_state["row_count"]:
        raise AssertionError(f"explicit period cache must contain table rows, got {cache_state}")

    page.reload(wait_until="domcontentloaded")
    page.wait_for_function(
        """(marker) => {
          const indicator = document.querySelector('[data-table-freshness-indicator]');
          const bodyText = document.body ? (document.body.innerText || '') : '';
          return !!indicator &&
            indicator.classList.contains('is-stale-loading') &&
            bodyText.includes(marker) &&
            document.querySelectorAll('[data-table-body] tr').length > 0;
        }""",
        arg=marker,
        timeout=3000,
    )
    stale_state = page.evaluate(
        """(marker) => ({
          marker_visible: (document.body.innerText || '').includes(marker),
          freshnessClass: ((document.querySelector('[data-table-freshness-indicator]') || {}).getAttribute('class') || ''),
          label: (document.querySelector('[data-history-label]') || {}).textContent || ''
        })""",
        arg=marker,
    )
    page.wait_for_function(
        """(marker) => {
          const indicator = document.querySelector('[data-table-freshness-indicator]');
          const bodyText = document.body ? (document.body.innerText || '') : '';
          return !!indicator &&
            indicator.classList.contains('is-fresh') &&
            !bodyText.includes(marker) &&
            document.querySelectorAll('[data-table-body] tr').length > 0;
        }""",
        arg=marker,
        timeout=20000,
    )
    fresh_state = page.evaluate(
        """(marker) => ({
          marker_visible: (document.body.innerText || '').includes(marker),
          freshnessClass: ((document.querySelector('[data-table-freshness-indicator]') || {}).getAttribute('class') || ''),
          label: (document.querySelector('[data-history-label]') || {}).textContent || '',
          query: window.location.search
        })""",
        arg=marker,
    )
    if fresh_state["marker_visible"]:
        raise AssertionError(f"explicit period cache marker must disappear after fresh payload, got {fresh_state}")
    return {
        "request_key": cache_state["request_key"],
        "cached_row_count": cache_state["row_count"],
        "stale_state": stale_state,
        "fresh_state": fresh_state,
    }


if __name__ == "__main__":
    main()
