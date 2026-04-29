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
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_FEEDBACKS_AI_ANALYZE_PATH,
    DEFAULT_SHEET_FEEDBACKS_AI_PROMPT_PATH,
    DEFAULT_SHEET_FEEDBACKS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
)


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
    ai_analyze_called = False
    ai_request_rows: list[dict[str, object]] = []
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

        def fulfill_prompt(route: object) -> None:
            if route.request.method == "GET":
                route.fulfill(
                    status=200,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                    body=json.dumps(_prompt_payload(status="missing"), ensure_ascii=False),
                )
                return
            route.fulfill(
                status=200,
                headers={"Content-Type": "application/json; charset=utf-8"},
                body=json.dumps(_prompt_payload(status="ready"), ensure_ascii=False),
            )

        def fulfill_ai_analyze(route: object) -> None:
            nonlocal ai_analyze_called
            ai_analyze_called = True
            payload = json.loads(route.request.post_data or "{}")
            rows = payload.get("rows") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise AssertionError("feedbacks AI analyze request must include rows array")
            ai_request_rows[:] = [row for row in rows if isinstance(row, dict)]
            route.fulfill(
                status=200,
                headers={"Content-Type": "application/json; charset=utf-8"},
                body=json.dumps(_ai_payload(ai_request_rows), ensure_ascii=False),
            )

        context.route("**" + DEFAULT_SHEET_FEEDBACKS_AI_PROMPT_PATH, fulfill_prompt)
        context.route("**" + DEFAULT_SHEET_FEEDBACKS_AI_ANALYZE_PATH, fulfill_ai_analyze)
        context.route("**" + DEFAULT_SHEET_FEEDBACKS_PATH + "?**", fulfill_feedbacks)
        try:
            page.goto(page_url, wait_until="domcontentloaded")
            page.wait_for_selector("[data-unified-tab-button='feedbacks']")
            page.locator("[data-unified-tab-button='feedbacks']").click()
            page.wait_for_selector("[data-feedbacks-panel]")
            page.wait_for_selector("[data-feedbacks-subtab='reviews']")
            page.wait_for_selector("[data-feedbacks-subtab='prompt']")

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
            if page.locator("[data-feedbacks-ai-analyze]").is_enabled():
                raise AssertionError("AI analyze button must be disabled before feedbacks load")

            page.locator("[data-feedbacks-load]").click()
            page.wait_for_selector("[data-feedbacks-table] tbody tr")
            row_count = page.locator("[data-feedbacks-table] tbody tr").count()
            if row_count != 24:
                raise AssertionError(f"feedbacks fixture must render 24 table rows, got {row_count}")
            if not page.locator("[data-feedbacks-table-scroll]").evaluate("node => node.scrollHeight > node.clientHeight"):
                raise AssertionError("feedbacks table must use an internal scroll container")
            for expected_header in ("Подходит для жалобы", "Категория", "Причина", "Уверенность"):
                if expected_header not in page.locator("[data-feedbacks-table] thead").inner_text():
                    raise AssertionError(f"feedbacks table must include AI header {expected_header!r}")
            if not page.locator("[data-feedbacks-ai-analyze]").is_enabled():
                raise AssertionError("AI analyze button must become enabled after feedbacks load")
            page.locator("[data-feedbacks-ai-analyze]").click()
            page.wait_for_selector("[data-feedbacks-prompt-textarea]")
            if "Сначала сохраните промпт разбора" not in page.locator("[data-feedbacks-error]").inner_text():
                raise AssertionError("AI analyze without saved prompt must show prompt-required message")
            page.locator("[data-feedbacks-prompt-textarea]").fill("Новый промпт разбора отзывов")
            page.locator("[data-feedbacks-prompt-save]").click()
            page.wait_for_function(
                "() => document.querySelector('[data-feedbacks-prompt-status]')?.textContent.includes('Сохранён')"
            )
            page.locator("[data-feedbacks-back-to-reviews]").click()
            if "WB API / feedbacks" not in page.locator("[data-feedbacks-meta]").inner_text():
                raise AssertionError("feedbacks tab must expose read-only WB API source context")
            if not captured_urls:
                raise AssertionError("feedbacks tab must call the feedbacks route after manual load")
            if "stars=2%2C3%2C4%2C5" not in captured_urls[-1] and "stars=2,3,4,5" not in captured_urls[-1]:
                raise AssertionError(f"feedbacks route query must include selected stars, got {captured_urls[-1]}")
            page.locator("[data-feedbacks-ai-analyze]").click()
            page.wait_for_function(
                "() => document.querySelector('[data-feedbacks-table] tbody tr td:nth-child(4)')?.textContent.includes('Да')"
            )
            if not ai_analyze_called:
                raise AssertionError("feedbacks AI analyze route must be called")
            if len(ai_request_rows) != 1 or ai_request_rows[0].get("feedback_id") != "browser-1":
                raise AssertionError(f"feedbacks AI analyze must default to one selected row, got {ai_request_rows}")
            first_fit = page.locator("[data-feedbacks-table] tbody tr").nth(0).locator("td").nth(3).inner_text()
            if "Да" not in first_fit:
                raise AssertionError(f"AI-positive feedbacks must sort first, got {first_fit!r}")
            page.locator("[data-feedbacks-ai-filter]").select_option("yes")
            filtered_count = page.locator("[data-feedbacks-table] tbody tr").count()
            if filtered_count != 1:
                raise AssertionError(f"AI filter yes must leave one row, got {filtered_count}")
        finally:
            browser.close()

    return {
        "base_url": base_url,
        "feedbacks_tab_present": True,
        "range_outside_click_closes": True,
        "star_filter_changes_query": True,
        "table_rows": 24,
        "ai_columns_present": True,
        "ai_request_rows": len(ai_request_rows),
        "ai_filter_works": True,
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
            "total": 24,
            "by_star": {"1": 0, "2": 0, "3": 0, "4": 0, "5": 24},
            "answered": 0,
            "unanswered": 24,
        },
        "schema": {"columns": []},
        "rows": [
            {
                "feedback_id": f"browser-{index}",
                "created_at": f"2026-04-{29 - min(index, 20):02d}T07:00:00Z",
                "created_date": f"2026-04-{29 - min(index, 20):02d}",
                "product_valuation": 5,
                "answer_status": "Без ответа",
                "is_answered": False,
                "nm_id": 210183919 + index,
                "supplier_article": f"WB-{index}",
                "product_name": "Товар A",
                "brand_name": "Brand",
                "text": "Текст отзыва " + ("длинный фрагмент " * 8),
                "pros": "Плюсы",
                "cons": "",
                "answer_text": "",
            }
            for index in range(1, 25)
        ],
    }


def _prompt_payload(*, status: str) -> dict[str, object]:
    prompt = "Сохранённый промпт" if status == "ready" else ""
    return {
        "contract_name": "sheet_vitrina_v1_feedbacks_ai_prompt",
        "contract_version": "v1",
        "prompt": prompt,
        "starter_prompt": "Стартовый промпт разбора отзывов",
        "updated_at": "2026-04-29T09:00:00Z" if status == "ready" else None,
        "status": status,
    }


def _ai_payload(rows: list[dict[str, object]]) -> dict[str, object]:
    feedback_ids = [str(row.get("feedback_id") or "") for row in rows if row.get("feedback_id")]
    return {
        "contract_name": "sheet_vitrina_v1_feedbacks_ai_analysis",
        "contract_version": "v1",
        "meta": {"analyzed_at": "2026-04-29T09:00:00Z", "row_count": len(feedback_ids)},
        "results": [
            {
                "feedback_id": feedback_id,
                "complaint_fit": "yes",
                "complaint_fit_label": "Да",
                "category": "profanity_or_insult",
                "category_label": "Мат, оскорбления или угрозы",
                "reason": "Есть оскорбление",
                "confidence": "high",
                "confidence_label": "Высокая",
                "evidence": "фрагмент",
            }
            for feedback_id in feedback_ids
        ],
    }


def _assert_node_hidden(locator: object, expected_hidden: bool, message: str) -> None:
    actual_hidden = locator.evaluate("node => node.hidden")
    if bool(actual_hidden) != expected_hidden:
        raise AssertionError(message)


if __name__ == "__main__":
    main()
