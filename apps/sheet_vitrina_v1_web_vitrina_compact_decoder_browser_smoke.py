"""Browser checks every decoded v2 cell against the legacy JSON cell."""

from __future__ import annotations

from pathlib import Path
import re
import sys

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer
from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_SHEET_WEB_VITRINA_PAGE_COMPOSITION_SURFACE,
    DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
)


def main() -> None:
    template = (ROOT / "packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html").read_text()
    match = re.search(
        r"    function decodeTableResponse\(payload\) \{.*?\n    \}\n\n    function businessProjectionMeta",
        template,
        re.S,
    )
    if match is None:
        raise AssertionError("table decoder not found in served template")
    decoder = match.group(0).rsplit("\n\n    function businessProjectionMeta", 1)[0]
    with LocalWebVitrinaFixtureServer(with_ready_snapshot=True) as base_url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH, wait_until="domcontentloaded")
                page.add_script_tag(content=decoder)
                route = (
                    DEFAULT_SHEET_WEB_VITRINA_READ_PATH
                    + f"?surface={DEFAULT_SHEET_WEB_VITRINA_PAGE_COMPOSITION_SURFACE}&include_table_data=1"
                )
                result = page.evaluate(
                    """async (route) => {
                      const [legacy, compact] = await Promise.all([
                        fetch(route).then(response => response.json()),
                        fetch(route + '&table_format=indexed_cells_v2').then(response => response.json())
                      ]);
                      decodeTableResponse(compact);
                      const oldRows = legacy.table_surface.rows;
                      const newRows = compact.table_surface.rows;
                      if (oldRows.length !== newRows.length) return {error: 'row count'};
                      let cells = 0;
                      let missing = 0;
                      let zeros = 0;
                      for (let rowIndex = 0; rowIndex < oldRows.length; rowIndex += 1) {
                        const oldValues = oldRows[rowIndex].values;
                        const newValues = newRows[rowIndex].values;
                        if (oldRows[rowIndex].row_id !== newRows[rowIndex].row_id) {
                          return {error: 'row identity', rowIndex};
                        }
                        for (const columnId of Object.keys(oldValues)) {
                          const oldCell = oldValues[columnId];
                          const newCell = newValues[columnId];
                          if (!newCell || JSON.stringify(oldCell) !== JSON.stringify(newCell)) {
                            return {error: 'cell JSON mismatch', rowIndex, columnId,
                                    oldCell, newCell: JSON.parse(JSON.stringify(newCell))};
                          }
                          cells += 1;
                          if (newCell.value === null) missing += 1;
                          if (newCell.value === 0) zeros += 1;
                        }
                      }
                      return {rows: oldRows.length, cells, missing, zeros};
                    }""",
                    route,
                )
                if result.get("error") or not result.get("cells") or not result.get("missing") or not result.get("zeros"):
                    raise AssertionError(f"browser decoder parity failed: {result}")
                print("compact_decoder_browser: ok ->", result)
            finally:
                browser.close()


if __name__ == "__main__":
    main()
