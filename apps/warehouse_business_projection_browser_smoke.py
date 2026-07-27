#!/usr/bin/env python3
"""Browser smoke for table-only business projection revision refresh."""

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
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
)


STATUS_PATH = (
    "/v1/sheet-vitrina-v1/web-vitrina/business-projection/status"
)
TARGET_ROW_ID = "TOTAL|total_orderSum"
TARGET_DATE = "2026-04-20"


def main() -> None:
    with LocalWebVitrinaFixtureServer(with_ready_snapshot=True) as base_url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 760, "height": 620})
            status_requested = False
            publish_enabled = False
            updated_composition_requests = 0
            observed_urls: list[str] = []

            def route_web_vitrina(route: object) -> None:
                nonlocal status_requested, updated_composition_requests
                url = route.request.url
                observed_urls.append(url)
                if STATUS_PATH in url:
                    status_requested = True
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "contract_name": "warehouse_business_projection",
                                "contract_version": 1,
                                "status": "ready",
                                "updating": False,
                                "revision_no": 1 if publish_enabled else 0,
                                "revision_id": (
                                    "whbpr_browser_smoke"
                                    if publish_enabled
                                    else ""
                                ),
                                "source_revision": (
                                    "source-browser-smoke"
                                    if publish_enabled
                                    else ""
                                ),
                                "business_effective_date": (
                                    "2026-04-20" if publish_enabled else ""
                                ),
                                "published_at": (
                                    "2026-04-21T10:00:00Z"
                                    if publish_enabled
                                    else ""
                                ),
                                "outbox_counts": (
                                    {"complete": 1} if publish_enabled else {}
                                ),
                                "queue_counts": {},
                                "latest_failure": None,
                            }
                        ),
                    )
                    return
                if (
                    "surface=page_composition" not in url
                    and "surface=page%5Fcomposition" not in url
                ):
                    route.continue_()
                    return
                response = route.fetch()
                payload = response.json()
                if publish_enabled and _apply_projection_fixture(payload):
                    updated_composition_requests += 1
                route.fulfill(
                    response=response,
                    content_type="application/json",
                    body=json.dumps(payload, ensure_ascii=False),
                )

            context.route(
                "**/v1/sheet-vitrina-v1/web-vitrina*",
                route_web_vitrina,
            )
            context.route(
                "**/v1/sheet-vitrina-v1/web-vitrina/business-projection/status",
                route_web_vitrina,
            )
            page = context.new_page()
            page.goto(
                base_url
                + DEFAULT_SHEET_WEB_VITRINA_UI_PATH
                + "?date_from=2026-04-19&date_to=2026-04-20",
                wait_until="domcontentloaded",
            )
            page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
            page.locator("[data-filters-toggle]").click()
            disclosure = page.locator(
                '[data-metric-anchor-toggle][aria-expanded="false"]'
            )
            if disclosure.count():
                disclosure.first.click()
            before = page.evaluate(
                """() => {
                  const scroll = document.querySelector('[data-table-scroll]');
                  if (scroll) {
                    scroll.scrollLeft = Math.max(0, scroll.scrollWidth - scroll.clientWidth);
                    scroll.scrollTop = Math.min(120, scroll.scrollHeight - scroll.clientHeight);
                  }
                  return {
                    period: (document.querySelector('[data-history-label]')?.textContent || '').trim(),
                    filtersOpen: !document.querySelector('[data-filters-rail]')?.hidden,
                    expanded: Array.from(document.querySelectorAll(
                      '[data-metric-anchor-toggle][aria-expanded="true"]'
                    )).map(node => node.getAttribute('data-metric-anchor-toggle')),
                    scrollLeft: scroll ? scroll.scrollLeft : 0,
                    scrollTop: scroll ? scroll.scrollTop : 0
                  };
                }"""
            )
            publish_enabled = True
            page.evaluate("() => pollBusinessProjectionRevision()")
            page.wait_for_timeout(300)
            if updated_composition_requests != 1:
                raise AssertionError(
                    "projection poll did not request the table composition: "
                    f"urls={observed_urls}, state="
                    + str(
                        page.evaluate(
                            "() => ({revisionNo: state.businessProjection.revisionNo,"
                            " revisionId: state.businessProjection.revisionId,"
                            " polling: state.businessProjection.polling,"
                            " path: WEB_VITRINA_CONFIG.business_projection_status_path,"
                            " visibility: document.visibilityState})"
                        )
                    )
                )
            page.wait_for_selector(".cell-business-projection-updated", timeout=10000)
            after = page.evaluate(
                """() => {
                  const scroll = document.querySelector('[data-table-scroll]');
                  const cell = document.querySelector(
                    '[data-row-id="TOTAL|total_orderSum"][data-cell-date="2026-04-20"]'
                  );
                  const style = cell ? getComputedStyle(cell) : null;
                  return {
                    period: (document.querySelector('[data-history-label]')?.textContent || '').trim(),
                    filtersOpen: !document.querySelector('[data-filters-rail]')?.hidden,
                    expanded: Array.from(document.querySelectorAll(
                      '[data-metric-anchor-toggle][aria-expanded="true"]'
                    )).map(node => node.getAttribute('data-metric-anchor-toggle')),
                    scrollLeft: scroll ? scroll.scrollLeft : 0,
                    scrollTop: scroll ? scroll.scrollTop : 0,
                    value: (cell?.textContent || '').trim(),
                    className: cell?.className || '',
                    background: style?.backgroundColor || '',
                    warning: cell?.classList.contains('cell-server-unconfirmed') || false,
                    statusTitle: document.querySelector('[data-table-load-status]')?.title || ''
                  };
                }"""
            )
            if not status_requested or updated_composition_requests != 1:
                raise AssertionError(
                    "revision poll must perform exactly one table composition reread: "
                    f"status={status_requested}, reads={updated_composition_requests}"
                )
            if (
                not before["period"]
                or after["period"] != before["period"]
                or not before["filtersOpen"]
                or not after["filtersOpen"]
                or after["expanded"] != before["expanded"]
                or abs(int(after["scrollLeft"]) - int(before["scrollLeft"])) > 2
                or abs(int(after["scrollTop"]) - int(before["scrollTop"])) > 2
            ):
                raise AssertionError(
                    f"table-only reread lost user state: before={before}, after={after}"
                )
            if (
                "1 111" not in after["value"].replace("\u00a0", " ")
                or "cell-business-projection-updated" not in after["className"]
                or after["warning"]
                or "139" not in after["background"]
                or "согласованной business-time projection" not in after["statusTitle"]
            ):
                raise AssertionError(
                    f"business projection accent/status mismatch: {after}"
                )
            print(
                "warehouse_business_projection_browser_smoke: OK ->",
                {
                    "before": before,
                    "after": after,
                    "updated_composition_requests": updated_composition_requests,
                },
            )
            browser.close()


def _apply_projection_fixture(payload: dict) -> bool:
    target_cell = None
    for row in (payload.get("table_surface") or {}).get("rows") or []:
        if str(row.get("row_id") or "") != TARGET_ROW_ID:
            continue
        target_cell = dict(row.get("values") or {}).get(
            "date:" + TARGET_DATE
        )
        if target_cell is not None:
            break
        for cell in row.get("cells") or []:
            if str(cell.get("column_id") or "") == "date:" + TARGET_DATE:
                target_cell = cell
                break
    if target_cell is None:
        return False
    payload.setdefault("meta", {})["warehouse_business_projection"] = {
        "revision_no": 1,
        "revision_id": "whbpr_browser_smoke",
        "source_revision": "source-browser-smoke",
        "business_effective_date": "2026-04-20",
        "published_at": "2026-04-21T10:00:00Z",
        "changed_cells": [
            {"row_id": TARGET_ROW_ID, "as_of_date": TARGET_DATE}
        ],
    }
    target_cell["value"] = 1111
    target_cell["display_text"] = "1 111"
    return True


if __name__ == "__main__":
    main()
