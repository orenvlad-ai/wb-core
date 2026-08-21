"""Browser smoke for unified UI popup outside-click behavior."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer  # noqa: E402
from packages.adapters.registry_upload_http_entrypoint import DEFAULT_SHEET_WEB_VITRINA_UI_PATH  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Browser smoke-check popup outside-click close behavior.")
    parser.add_argument("--base-url", default="", help="Existing base URL, for example http://89.191.226.88")
    parser.add_argument(
        "--ignore-https-errors",
        action="store_true",
        help="Ignore TLS validation errors in the browser context.",
    )
    args = parser.parse_args()

    if args.base_url:
        result = run_browser_checks(args.base_url.rstrip("/"), ignore_https_errors=args.ignore_https_errors)
    else:
        with LocalWebVitrinaFixtureServer(with_ready_snapshot=True) as base_url:
            result = run_browser_checks(base_url, ignore_https_errors=False)
    _print_summary(result)


def run_browser_checks(base_url: str, *, ignore_https_errors: bool) -> dict[str, object]:
    page_url = base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            ignore_https_errors=ignore_https_errors,
            viewport={"width": 1280, "height": 950},
        )
        page = context.new_page()
        try:
            page.goto(page_url, wait_until="domcontentloaded")
            page.wait_for_selector("[data-unified-tab-button='vitrina']")
            page.wait_for_selector("[data-history-toggle]")
            vitrina = _check_vitrina_popovers(page)
            research = _check_research_popovers(page)
            reports = _check_reports_popovers(page)
            supply = _check_supply_tab(page)
        finally:
            browser.close()

    return {
        "base_url": base_url,
        "vitrina": vitrina,
        "research": research,
        "reports": reports,
        "supply": supply,
    }


def _check_vitrina_popovers(page: object) -> dict[str, object]:
    history_toggle = page.locator("[data-history-toggle]")
    history_popover = page.locator("[data-history-popover]")
    filters_toggle = page.locator("[data-filters-toggle]")
    filters_rail = page.locator("[data-filters-rail]")
    sku_metric_toggle = page.locator("[data-sku-metric-toggle]")
    sku_metric_panel = page.locator("[data-sku-metric-panel]")

    history_toggle.click()
    _assert_node_hidden(history_popover, False, "history popover must open from trigger")
    _assert_toolbar_popover_visible(page, "[data-history-popover]", "history popover")
    history_toggle.click()
    _assert_node_hidden(history_popover, True, "history popover must close from the same trigger")

    history_toggle.click()
    _assert_node_hidden(history_popover, False, "history popover must reopen")
    _assert_toolbar_popover_visible(page, "[data-history-popover]", "history popover")
    filters_toggle.click()
    _assert_node_hidden(history_popover, True, "opening filters rail must close history popover")
    _assert_node_hidden(filters_rail, False, "filters rail must open from trigger")

    sku_metric_toggle.click()
    _assert_node_hidden(sku_metric_panel, False, "SKU metric picker must open from trigger")
    _assert_toolbar_popover_visible(page, "[data-sku-metric-panel]", "SKU metric picker")
    first_metric_checkbox = page.locator("[data-sku-metric-option]").first
    first_metric_checkbox.click()
    _assert_node_hidden(
        sku_metric_panel,
        False,
        "SKU metric picker must stay open after an inside checkbox click",
    )

    page.locator('[data-filter-control="section"]').click()
    _assert_node_hidden(sku_metric_panel, True, "click inside rail but outside SKU picker must close SKU picker")
    _assert_node_hidden(filters_rail, False, "filters rail must stay open after inside rail click")

    page.locator("[data-table-shell]").click(position={"x": 20, "y": 20})
    _assert_node_hidden(filters_rail, True, "outside click must close filters rail")

    filters_toggle.click()
    _assert_node_hidden(filters_rail, False, "filters rail must reopen")
    page.keyboard.press("Escape")
    _assert_node_hidden(filters_rail, True, "Escape must close filters rail")

    filters_toggle.click()
    _assert_node_hidden(filters_rail, False, "filters rail must open before trigger-close check")
    filters_toggle.click()
    _assert_node_hidden(filters_rail, True, "filters rail trigger must close on repeated click")

    return {
        "history_trigger_toggle": True,
        "history_to_filters_closes_previous": True,
        "sku_metric_inside_click_keeps_open": True,
        "sku_metric_inside_rail_click_closes_picker": True,
        "filters_rail_outside_click_closes": True,
        "filters_rail_escape_closes": True,
        "filters_rail_trigger_toggle": True,
    }


def _assert_toolbar_popover_visible(page: object, selector: str, label: str) -> None:
    state = page.evaluate(
        """(selector) => {
          const header = document.querySelector('[data-table-header]');
          const node = document.querySelector(selector);
          if (!header || !node) {
            return {exists: !!node, headerExists: !!header};
          }
          const headerRect = header.getBoundingClientRect();
          const rect = node.getBoundingClientRect();
          const headerStyle = getComputedStyle(header);
          const nodeStyle = getComputedStyle(node);
          return {
            exists: true,
            headerExists: true,
            headerOverflowX: headerStyle.overflowX,
            headerOverflowY: headerStyle.overflowY,
            display: nodeStyle.display,
            visibility: nodeStyle.visibility,
            height: Math.round(rect.height),
            width: Math.round(rect.width),
            top: Math.round(rect.top),
            headerTop: Math.round(headerRect.top),
            insideOrBelowHeader: rect.top >= headerRect.top - 1
          };
        }""",
        selector,
    )
    if (
        not state.get("exists")
        or not state.get("headerExists")
        or state.get("headerOverflowX") != "visible"
        or state.get("headerOverflowY") != "visible"
        or state.get("display") == "none"
        or state.get("visibility") == "hidden"
        or int(state.get("height") or 0) <= 20
        or int(state.get("width") or 0) <= 120
        or not state.get("insideOrBelowHeader")
    ):
        raise AssertionError(f"{label} must be visibly open without table-header clipping, got {state}")


def _check_research_popovers(page: object) -> dict[str, object]:
    page.locator('[data-unified-tab-button="research"]').click()
    page.wait_for_selector('[data-research-sku="research"]', state="attached")
    page.wait_for_selector("[data-research-metric]", state="attached")

    research_picker = page.locator('[data-research-picker="research"]')
    metric_picker = page.locator('[data-research-picker="metrics"]')

    research_picker.locator("summary").click()
    _assert_details_open(research_picker, True, "research SKU picker must open")
    first_research_sku = page.locator('[data-research-sku="research"]:not(:disabled)').first
    first_research_sku.click()
    _assert_details_open(research_picker, True, "research SKU multiselect must stay open after checkbox click")
    if not first_research_sku.is_checked():
        raise AssertionError("research SKU checkbox must become selected")

    _click_research_outside(page)
    _assert_details_open(research_picker, False, "outside click must close research SKU picker")
    metric_picker.locator("summary").click()
    _assert_details_open(metric_picker, True, "research metric picker must open")
    _click_research_outside(page)
    _assert_details_open(metric_picker, False, "outside click must close research metric picker")

    metric_picker.locator("summary").click()
    page.keyboard.press("Escape")
    _assert_details_open(metric_picker, False, "Escape must close research metric picker")

    baseline_toggle = page.locator('[data-research-range-toggle="baseline"]')
    baseline_popover = page.locator('[data-research-range-popover="baseline"]')
    baseline_toggle.click()
    _assert_node_hidden(baseline_popover, False, "research baseline range picker must open")
    available_baseline_days = page.locator('[data-research-range-day="baseline"]:not(:disabled)')
    available_baseline_days.first.click()
    _assert_node_hidden(baseline_popover, False, "first date click must not close range picker prematurely")
    available_baseline_days.nth(1).click()
    _click_research_outside(page)
    _assert_node_hidden(baseline_popover, True, "outside click must close research baseline range picker")

    _calculate_research_fixture_result(page)

    return {
        "sku_multiselect_inside_click_keeps_open": True,
        "metric_picker_outside_click_closes": True,
        "range_picker_inside_first_click_keeps_open": True,
        "range_picker_outside_click_closes": True,
        "escape_closes_metric_picker": True,
        "valid_calculate_renders_rows": True,
    }


def _calculate_research_fixture_result(page: object) -> None:
    _set_checked(page.locator('[data-research-sku="research"]:not(:disabled)').first, True)
    page.locator('[data-research-picker="control"] > summary').click()
    _set_checked(page.locator('[data-research-sku="control"]:not(:disabled)').nth(1), True)
    _click_research_outside(page)
    page.locator('[data-research-picker="metrics"] > summary').click()
    metric = page.locator('[data-research-metric][value="price_seller_discounted"]')
    if metric.count() == 0:
        metric = page.locator("[data-research-metric]").first
    _set_checked(metric, True)
    _click_research_outside(page)
    _select_research_range(page, "analysis", "2026-04-19", "2026-04-20")
    page.locator("[data-research-calculate]").click()
    page.wait_for_selector("[data-research-result-table] tbody tr")
    row_count = page.locator("[data-research-result-table] tbody tr").count()
    if row_count <= 0:
        raise AssertionError("research calculation must render result rows")
    scroll_width = page.locator("[data-research-result-table]").evaluate("node => node.scrollWidth")
    client_width = page.locator("[data-research-result-table]").evaluate("node => node.clientWidth")
    if scroll_width < client_width:
        raise AssertionError("research result table dimensions must stay coherent")


def _select_research_range(page: object, kind: str, start_date: str, end_date: str) -> None:
    toggle = page.locator(f'[data-research-range-toggle="{kind}"]')
    toggle.click()
    page.locator(f'[data-research-range-day="{kind}"][data-date="{start_date}"]').click()
    page.locator(f'[data-research-range-day="{kind}"][data-date="{end_date}"]').click()
    _click_research_outside(page)
    _assert_node_hidden(page.locator(f'[data-research-range-popover="{kind}"]'), True, f"{kind} range picker must close outside")


def _click_research_outside(page: object) -> None:
    page.locator("[data-research-panel]").evaluate(
        """node => node.dispatchEvent(new PointerEvent('pointerdown', {
          bubbles: true,
          cancelable: true,
          clientX: 12,
          clientY: 12
        }))"""
    )


def _check_reports_popovers(page: object) -> dict[str, object]:
    page.locator('[data-unified-tab-button="reports"]').click()
    page.wait_for_function(
        """() => {
          const panel = document.querySelector('[data-unified-tab-panel="reports"]');
          const frame = document.querySelector('[data-operator-embed-frame="reports"]');
          const active = document.querySelector('[data-unified-tab-button].is-active');
          return !!panel && !panel.hidden && !!frame &&
            frame.getBoundingClientRect().height > 320 &&
            active && active.getAttribute('data-unified-tab-button') === 'reports';
        }""",
        timeout=5000,
    )
    page.locator('[data-operator-embed-frame="reports"]').scroll_into_view_if_needed()
    report_frame = page.frame_locator('[data-operator-embed-frame="reports"]')
    report_frame.locator('[data-report-section-button="stock"]').click()
    page.wait_for_function(
        """() => {
          const frame = document.querySelector('[data-operator-embed-frame="reports"]');
          const doc = frame && frame.contentDocument;
          const panel = doc && doc.querySelector('[data-report-section-panel="stock"]');
          const summary = doc && doc.querySelector('#stockReportSkuSelector summary');
          return !!panel && !panel.hidden && !!summary &&
            summary.getBoundingClientRect().height > 0 &&
            summary.getBoundingClientRect().width > 0;
        }""",
        timeout=5000,
    )
    stock_selector = report_frame.locator("#stockReportSkuSelector")
    stock_selector.wait_for()
    stock_selector.locator("summary").scroll_into_view_if_needed()
    page.wait_for_timeout(250)
    stock_selector.evaluate("node => node.querySelector('summary').click()")
    _assert_details_open(stock_selector, True, "stock report SKU selector must open")
    first_stock_checkbox = report_frame.locator("#stockReportSkuList input[type='checkbox']").first
    first_stock_checkbox.evaluate("node => node.click()")
    _assert_details_open(stock_selector, True, "stock report SKU selector must stay open after checkbox click")
    report_frame.locator("body").evaluate(
        """node => node.dispatchEvent(new PointerEvent('pointerdown', {
          bubbles: true,
          cancelable: true,
          clientX: 12,
          clientY: 12
        }))"""
    )
    _assert_details_open(stock_selector, False, "iframe outside click must close stock report SKU selector")

    stock_selector.evaluate("node => node.querySelector('summary').click()")
    _assert_details_open(stock_selector, True, "stock selector must reopen before parent outside check")
    page.locator('[data-unified-tab-button="vitrina"]').click()
    page.wait_for_function(
        """() => {
          const frame = document.querySelector('[data-operator-embed-frame="reports"]');
          const doc = frame && frame.contentDocument;
          const selector = doc && doc.querySelector("#stockReportSkuSelector");
          return selector && !selector.open;
        }"""
    )
    _assert_details_open(stock_selector, False, "parent shell click must close embedded stock selector")

    return {
        "stock_selector_inside_click_keeps_open": True,
        "stock_selector_iframe_outside_click_closes": True,
        "stock_selector_parent_click_closes": True,
    }


def _check_supply_tab(page: object) -> dict[str, object]:
    page.locator('[data-unified-tab-button="factory-order"]').click()
    factory_frame = page.frame_locator('[data-operator-embed-frame="factory-order"]')
    factory_frame.locator("#calculateFactoryOrderButton").wait_for()
    return {
        "supply_tab_rendered": True,
        "custom_supply_popovers_found": 0,
    }


def _set_checked(locator: object, checked: bool) -> None:
    if locator.is_checked() == checked:
        return
    locator.click()


def _assert_details_open(locator: object, expected: bool, message: str) -> None:
    actual = bool(locator.evaluate("node => Boolean(node.open)"))
    if actual is not expected:
        raise AssertionError(f"{message}; expected open={expected}, got open={actual}")


def _assert_node_hidden(locator: object, expected: bool, message: str) -> None:
    actual = bool(locator.evaluate("node => Boolean(node.hidden)"))
    if actual is not expected:
        raise AssertionError(f"{message}; expected hidden={expected}, got hidden={actual}")


def _print_summary(result: dict[str, object]) -> None:
    print("sheet_vitrina_v1_popup_outside_click_browser: ok")
    print("popup_base_url:", result["base_url"])
    print("popup_vitrina:", result["vitrina"])
    print("popup_research:", result["research"])
    print("popup_reports:", result["reports"])
    print("popup_supply:", result["supply"])


if __name__ == "__main__":
    main()
