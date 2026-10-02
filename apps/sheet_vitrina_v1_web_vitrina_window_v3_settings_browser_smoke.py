"""Focused V3 browser check for non-default presentation across projection refresh."""

from __future__ import annotations

from pathlib import Path
import sys

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer  # noqa: E402
from packages.adapters.registry_upload_http_entrypoint import DEFAULT_SHEET_WEB_VITRINA_UI_PATH  # noqa: E402


SNAPSHOT = """() => {
  const view = state.windowV3;
  const dateColumn = state.composition.table_surface.columns.find(column =>
    String(column.id).startsWith('date:'));
  const heading = document.querySelector('[data-table-head] th[data-col-id="' + dateColumn.id + '"]');
  const skuRows = view.catalogRows.filter(row => row.row_kind === 'sku');
  const first = skuRows.find(row => cellValue(row, 'metric_key') === 'view_count');
  const second = skuRows.find(row => cellValue(row, 'metric_key') === 'orderCount');
  return {
    session: view.sessionId, generation: view.generation,
    selected: Array.from(state.filters.selectedMetricKeys || []).sort(),
    sort: state.filters.sort, section_visible: isColumnVisible('section'),
    first_visible: !!first && view.displayIndexes.includes(first.rowIndex),
    collapsed_hidden: !!second && isMetricHiddenByPresentation(second)
      && !view.displayIndexes.includes(second.rowIndex),
    collapsed_status: metricDisplayStatusForScope('sku', 'orderCount'),
    column_size: dateColumn.size, rendered_width: heading?.style.width || '',
    column_count: document.querySelectorAll('[data-table-head] th[data-col-id^="date:"]').length,
    visible_cells: view.visibleCells.size
  };
}"""


def wait_cells(page) -> None:
    page.wait_for_function("""() => {
      const view = state.windowV3;
      const viewport = view.lastViewport;
      return !!view.globalResult && !!viewport && viewport.rowIndexes.length > 0
        && viewport.dateIndexes.length > 0
        && viewport.rowIndexes.every(row => viewport.dateIndexes.every(date =>
          view.visibleCells.get(row + ':' + date)?.status === 'loaded'));
    }""", timeout=90000)


def main() -> None:
    with LocalWebVitrinaFixtureServer(with_ready_snapshot=True, advertise_window_v3=True) as base_url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1200, "height": 800})
                page.goto(base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH +
                          "?history_mode=explicit&date_from=2026-04-20&date_to=2026-04-21",
                          wait_until="domcontentloaded", timeout=90000)
                wait_cells(page)
                chosen = page.evaluate("""() => {
                  const keys = state.filters.metricKeys;
                  if (!keys.includes('view_count') || !keys.includes('orderCount')) {
                    throw new Error('fixture lacks required SKU metrics');
                  }
                  const selection = ['view_count', 'orderCount'];
                  state.filters.selectedMetricKeys = new Set(selection);
                  state.metricPresentation.skuMetricSelection = {
                    mode: 'manual', presetId: '', all: false, metricKeys: selection.slice()
                  };
                  setMetricDisplayStatusForScope('sku', 'view_count', 'shown');
                  setMetricDisplayStatusForScope('sku', 'orderCount', 'collapsed');
                  // Keep this local fixture read-only while exercising a pending
                  // user preference through the same materialized configuration.
                  persistMetricPresentation({skipServer: true});
                  state.metricPresentation.serverConfig.dirty = true;
                  state.metricPresentation.serverConfig.desiredConfig = buildMetricPresentationPersistedPayload();
                  updateColumnVisibility('section', false);
                  const option = (state.composition.filter_surface.sort_options || []).find(item =>
                    String(item.column_id).startsWith('date:'));
                  if (!option) throw new Error('fixture lacks a date sort option');
                  state.filters.sort = option.value;
                  renderTable();
                  return {selection: selection.slice().sort(), sort: option.value};
                }""")
                page.wait_for_function("(sort) => state.windowV3.globalResult?.sort === sort", arg=chosen["sort"], timeout=90000)
                wait_cells(page)
                before = page.evaluate(SNAPSHOT)
                if before["selected"] != chosen["selection"] or before["sort"] != chosen["sort"] \
                        or before["section_visible"] or not before["first_visible"] or not before["collapsed_hidden"] \
                        or before["collapsed_status"] != "collapsed" or before["rendered_width"] != f"{before['column_size']}px":
                    raise AssertionError(f"non-default V3 settings were not rendered: {before}")
                page.evaluate("() => reloadBusinessProjectionTable({revision_no: state.businessProjection.revisionNo + 1, revision_id: 'settings-smoke'})")
                page.wait_for_function("(old) => state.windowV3.sessionId && state.windowV3.sessionId !== old", arg=before["session"], timeout=90000)
                wait_cells(page)
                after = page.evaluate(SNAPSHOT)
                expected = {key: value for key, value in before.items() if key not in {"session", "generation", "visible_cells"}}
                actual = {key: value for key, value in after.items() if key not in {"session", "generation", "visible_cells"}}
                if expected != actual or after["generation"] <= before["generation"]:
                    raise AssertionError(f"projection refresh lost V3 settings or column widths: {before} -> {after}")
                page.locator("[data-history-toggle]").click()
                page.locator('[data-history-preset="all_history"]').click()
                all_history = page.evaluate("""() => ({
                  from: state.history.draftDateFrom, to: state.history.draftDateTo,
                  first: state.history.availableDates[0],
                  last: state.history.availableDates[state.history.availableDates.length - 1]
                })""")
                if all_history["from"] != all_history["first"] or all_history["to"] != all_history["last"]:
                    raise AssertionError(f"UI all-history preset did not select the full range: {all_history}")
                print("window_v3_settings_browser: selection, collapse, sort, visibility, metadata widths and UI all-history ok")
            finally:
                browser.close()


if __name__ == "__main__":
    main()
