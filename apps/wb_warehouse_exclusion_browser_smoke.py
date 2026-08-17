"""Browser smoke for hidden incident settings with unchanged calculation state."""

from __future__ import annotations

import json
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_WB_WAREHOUSE_EXCLUSION_OPTIONS_PATH,
    DEFAULT_WB_WAREHOUSE_EXCLUSION_SETTINGS_PATH,
    _render_sheet_vitrina_operator_ui,
)


def main() -> None:
    html = _render_sheet_vitrina_operator_ui(
        daily_report_path="/daily",
        stock_report_path="/stock",
        plan_report_path="/plan",
        refresh_path="/refresh",
        load_path="/load",
        status_path="/status",
        job_path="/job",
        embedded_tab="factory-order",
    )
    saved: list[dict[str, object]] = []
    reads: list[str] = []
    revision = [0]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        def route_handler(route):
            request = route.request
            path = request.url.split("warehouse.test", 1)[-1]
            if path == "/page":
                route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=html,
                )
                return
            if path == DEFAULT_WB_WAREHOUSE_EXCLUSION_SETTINGS_PATH:
                if request.method == "POST":
                    payload = json.loads(request.post_data or "{}")
                    saved.append(payload)
                    revision[0] += 1
                    response = {
                        "status": "ok",
                        "exists": True,
                        "revision": revision[0],
                        "updated_at": "2026-07-26T08:00:00Z",
                        "excluded_wb_warehouse_ids": payload.get(
                            "excluded_wb_warehouse_ids", []
                        ),
                        "canonical_store": "server_runtime_user_config",
                    }
                else:
                    reads.append("settings")
                    response = {
                        "status": "ok",
                        "exists": True,
                        "revision": revision[0],
                        "active": True,
                        "updated_at": "",
                        "excluded_wb_warehouse_ids": [77],
                        "effective_excluded_wb_warehouse_ids": [77],
                        "canonical_store": "server_runtime_wb_incident_policy",
                    }
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(response),
                )
                return
            if path == DEFAULT_WB_WAREHOUSE_EXCLUSION_OPTIONS_PATH:
                reads.append("options")
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "snapshot_date": "2026-07-26",
                            "fetched_at": "2026-07-26T07:30:00Z",
                            "pagination_complete": True,
                            "raw_rows_digest": "sha256:fixture",
                            "options": [
                                {
                                    "warehouse_id": 1,
                                    "warehouse_name": "Ярославль",
                                    "total_contour": 5,
                                    "stock_quantity": 5,
                                },
                                {
                                    "warehouse_id": 2,
                                    "warehouse_name": "Бета",
                                    "total_contour": 50,
                                    "stock_quantity": 50,
                                },
                                {
                                    "warehouse_id": 3,
                                    "warehouse_name": "Альфа",
                                    "total_contour": 50,
                                    "stock_quantity": 50,
                                },
                                {
                                    "warehouse_id": 4,
                                    "warehouse_name": "Альфа",
                                    "total_contour": 50,
                                    "stock_quantity": 50,
                                },
                            ],
                        },
                        ensure_ascii=False,
                    ),
                )
                return
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"status":"unavailable","rows":[],"options":[]}',
            )

        page.route("http://warehouse.test/**", route_handler)
        page.goto("http://warehouse.test/page", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => JSON.parse(localStorage.getItem('wb-core:sheet-vitrina-v1:operator-ui-state:v1') || '{}').excluded_wb_warehouse_ids?.includes(77)"
        )
        page.wait_for_timeout(100)
        if saved:
            raise AssertionError("reading canonical settings must not cause a write")
        if reads.count("settings") != 1 or reads.count("options") != 1:
            raise AssertionError(f"hidden calculation state was not loaded once: {reads}")
        for selector in (
            "#wbWarehouseExclusionSelector",
            "#wbWarehouseExclusionSummary",
            "[data-wb-warehouse-exclusion-checkbox]",
        ):
            if page.locator(selector).count():
                raise AssertionError(f"ordinary Supply UI leaked incident selector: {selector}")
        body_text = page.locator("body").inner_text()
        for text in (
            "Учитывается политика инцидентов",
            "Политика инцидентов",
            "Исключение складов WB",
        ):
            if text in body_text:
                raise AssertionError(f"ordinary Supply UI leaked incident text: {text}")
        if saved:
            raise AssertionError("hidden Supply policy must never persist a browser copy")
        browser.close()
    print("wb_warehouse_exclusion_browser_smoke: OK")


if __name__ == "__main__":
    main()
