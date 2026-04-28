"""Browser smoke-check for the sheet_vitrina_v1 feedbacks tab MVP."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer  # noqa: E402
from packages.adapters.registry_upload_http_entrypoint import DEFAULT_SHEET_WEB_VITRINA_UI_PATH  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Browser smoke-check feedbacks tab read-only UX.")
    parser.add_argument("--base-url", default="", help="Existing base URL, for example https://api.selleros.pro")
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
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("sheet-vitrina-v1-feedbacks-browser-smoke passed")


def run_browser_checks(base_url: str, *, ignore_https_errors: bool) -> dict[str, object]:
    captured_urls: list[str] = []
    page_url = base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=ignore_https_errors, viewport={"width": 1280, "height": 900})
        page = context.new_page()

        def fulfill_feedbacks(route: object) -> None:
            url = route.request.url
            captured_urls.append(url)
            if "/load" in url:
                raise AssertionError("feedbacks browser smoke must not call /load")
            route.fulfill(
                status=200,
                headers={"Content-Type": "application/json; charset=utf-8"},
                body=json.dumps(_feedbacks_payload(), ensure_ascii=False),
            )

        context.route("**/v1/sheet-vitrina-v1/feedbacks**", fulfill_feedbacks)
        try:
            page.goto(page_url, wait_until="domcontentloaded")
            page.wait_for_selector("[data-unified-tab-button='feedbacks']")
            page.locator("[data-unified-tab-button='feedbacks']").click()
            page.wait_for_selector("[data-feedbacks-panel]")

            range_toggle = page.locator("[data-feedbacks-range-toggle]")
            range_popover = page.locator("[data-feedbacks-range-popover]")
            range_toggle.click()
            _assert_node_hidden(range_popover, False, "feedbacks range picker must open")
            page.locator("[data-feedbacks-meta]").click()
            _assert_node_hidden(range_popover, True, "outside click must close feedbacks range picker")

            first_star = page.locator('[data-feedbacks-star][value="1"]')
            first_star.click()
            if first_star.is_checked():
                raise AssertionError("feedbacks star checkbox must update selection")

            page.locator("[data-feedbacks-load]").click()
            page.wait_for_selector("[data-feedbacks-table] tbody tr")
            row_count = page.locator("[data-feedbacks-table] tbody tr").count()
            if row_count != 1:
                raise AssertionError(f"feedbacks fixture must render one table row, got {row_count}")
            if "WB API / feedbacks" not in page.locator("[data-feedbacks-meta]").inner_text():
                raise AssertionError("feedbacks tab must expose read-only WB API source context")
            if not captured_urls:
                raise AssertionError("feedbacks tab must call the feedbacks route after manual load")
            if "stars=2%2C3%2C4%2C5" not in captured_urls[-1] and "stars=2,3,4,5" not in captured_urls[-1]:
                raise AssertionError(f"feedbacks route query must include selected stars, got {captured_urls[-1]}")
        finally:
            browser.close()

    return {
        "base_url": base_url,
        "feedbacks_tab_present": True,
        "range_outside_click_closes": True,
        "star_filter_changes_query": True,
        "table_rows": 1,
    }


def _feedbacks_payload() -> dict[str, object]:
    return {
        "contract_name": "sheet_vitrina_v1_feedbacks",
        "contract_version": "v1",
        "meta": {
            "date_from": "2026-04-23",
            "date_to": "2026-04-29",
            "stars": [2, 3, 4, 5],
            "fetched_at": "2026-04-29T09:00:00Z",
            "source": "WB API / feedbacks",
        },
        "summary": {
            "total": 1,
            "by_star": {"1": 0, "2": 0, "3": 0, "4": 0, "5": 1},
            "answered": 0,
            "unanswered": 1,
        },
        "schema": {"columns": []},
        "rows": [
            {
                "feedback_id": "browser-1",
                "created_at": "2026-04-29T07:00:00Z",
                "created_date": "2026-04-29",
                "product_valuation": 5,
                "answer_status": "Без ответа",
                "is_answered": False,
                "nm_id": 210183919,
                "supplier_article": "WB-1",
                "product_name": "Товар A",
                "brand_name": "Brand",
                "text": "Текст отзыва",
                "pros": "Плюсы",
                "cons": "",
                "answer_text": "",
            }
        ],
    }


def _assert_node_hidden(locator: object, expected_hidden: bool, message: str) -> None:
    actual_hidden = locator.evaluate("node => node.hidden")
    if bool(actual_hidden) != expected_hidden:
        raise AssertionError(message)


if __name__ == "__main__":
    main()
