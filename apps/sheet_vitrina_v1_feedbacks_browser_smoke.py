"""Browser smoke-check for the sheet_vitrina_v1 feedbacks tab MVP."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
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
    DEFAULT_SHEET_FEEDBACKS_EXPORT_PATH,
    DEFAULT_SHEET_FEEDBACKS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Browser smoke-check feedbacks tab read-only UX.")
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
        with LocalWebVitrinaFixtureServer(with_ready_snapshot=True, now=datetime(2026, 4, 30, 9, 0, tzinfo=timezone.utc)) as base_url:
            result = run_browser_checks(base_url, ignore_https_errors=False)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("sheet-vitrina-v1-feedbacks-browser-smoke passed")


def run_browser_checks(base_url: str, *, ignore_https_errors: bool) -> dict[str, object]:
    captured_urls: list[str] = []
    ai_request_batches: list[list[str]] = []
    export_requests: list[dict[str, object]] = []
    failed_once: set[str] = set()
    large_feedbacks_mode = False
    prompt_saved = False
    selected_model = "gpt-5-mini"
    page_url = base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=ignore_https_errors, viewport={"width": 1280, "height": 900})
        context.add_init_script(
            "if (!window.sessionStorage.getItem('wb_core_feedbacks_broken_widths_seeded')) {"
            "window.localStorage.setItem('wb_core_feedbacks_column_widths_v1', '{broken-json');"
            "window.sessionStorage.setItem('wb_core_feedbacks_broken_widths_seeded', '1');"
            "}"
        )
        page = context.new_page()

        def fulfill_feedbacks(route: object) -> None:
            url = route.request.url
            captured_urls.append(url)
            if "/load" in url:
                raise AssertionError("feedbacks browser smoke must not call /load")
            route.fulfill(
                status=200,
                headers={"Content-Type": "application/json; charset=utf-8"},
                body=json.dumps(_feedbacks_payload(row_count=650 if large_feedbacks_mode else 24), ensure_ascii=False),
            )

        def fulfill_web_vitrina_read(route: object) -> None:
            response = route.fetch()
            payload = json.loads(response.text())
            meta = payload.get("meta") if isinstance(payload, dict) else None
            if isinstance(meta, dict):
                meta["snapshot_as_of_date"] = "2026-04-24"
                meta["today_current_date"] = "2026-04-24"
                meta["server_now_business_tz"] = "2026-04-30T09:00:00+05:00"
                meta["generated_at"] = "2026-04-30T04:00:00Z"
            route.fulfill(
                status=response.status,
                headers={"Content-Type": "application/json; charset=utf-8"},
                body=json.dumps(payload, ensure_ascii=False),
            )

        def fulfill_prompt(route: object) -> None:
            nonlocal prompt_saved, selected_model
            if route.request.method == "GET":
                route.fulfill(
                    status=200,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                    body=json.dumps(_prompt_payload(status="ready" if prompt_saved else "missing", model=selected_model), ensure_ascii=False),
                )
                return
            payload = json.loads(route.request.post_data or "{}")
            selected_model = str(payload.get("model") or "gpt-5-mini")
            prompt_saved = True
            route.fulfill(
                status=200,
                headers={"Content-Type": "application/json; charset=utf-8"},
                body=json.dumps(_prompt_payload(status="ready", model=selected_model), ensure_ascii=False),
            )

        def fulfill_export(route: object) -> None:
            payload = json.loads(route.request.post_data or "{}")
            rows = payload.get("rows") if isinstance(payload, dict) else None
            if not isinstance(rows, list) or not rows:
                raise AssertionError("feedbacks export must send current visible rows")
            export_requests.append({"row_count": len(rows), "first_id": str((rows[0] or {}).get("feedback_id") or "")})
            route.fulfill(
                status=200,
                headers={
                    "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "Content-Disposition": "attachment; filename=\"wb_feedbacks_2026-04-23_2026-04-29.xlsx\"",
                },
                body=b"PK\x03\x04fake-xlsx-for-browser-smoke",
            )

        def fulfill_ai_analyze(route: object) -> None:
            payload = json.loads(route.request.post_data or "{}")
            rows = payload.get("rows") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise AssertionError("feedbacks AI analyze request must include rows array")
            if len(rows) != 1:
                raise AssertionError(f"feedbacks AI queue must send exactly one row per request, got {len(rows)}")
            row = rows[0] if isinstance(rows[0], dict) else {}
            feedback_id = str(row.get("feedback_id") or "")
            ai_request_batches.append([feedback_id])
            if feedback_id == "browser-2" and feedback_id not in failed_once:
                failed_once.add(feedback_id)
                route.fulfill(
                    status=502,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                    body=json.dumps({"error": "temporary AI failure"}, ensure_ascii=False),
                )
                return
            route.fulfill(
                status=200,
                headers={"Content-Type": "application/json; charset=utf-8"},
                body=json.dumps(_ai_payload([row]), ensure_ascii=False),
            )

        context.route("**" + DEFAULT_SHEET_WEB_VITRINA_READ_PATH + "?**", fulfill_web_vitrina_read)
        context.route("**" + DEFAULT_SHEET_FEEDBACKS_AI_PROMPT_PATH, fulfill_prompt)
        context.route("**" + DEFAULT_SHEET_FEEDBACKS_AI_ANALYZE_PATH, fulfill_ai_analyze)
        context.route("**" + DEFAULT_SHEET_FEEDBACKS_EXPORT_PATH, fulfill_export)
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
            for day in ("2026-04-25", "2026-04-26", "2026-04-27", "2026-04-28", "2026-04-29"):
                day_button = page.locator(f'[data-feedbacks-range-day="{day}"]')
                if day_button.count() != 1 or not day_button.is_enabled():
                    raise AssertionError(f"feedbacks range picker must allow non-future day {day}")
            page.locator("[data-feedbacks-date-from]").fill("2026-03-24")
            page.locator("[data-feedbacks-date-from]").dispatch_event("change")
            page.locator("[data-feedbacks-date-to]").fill("2026-04-25")
            page.locator("[data-feedbacks-date-to]").dispatch_event("change")
            save_button = page.locator("[data-feedbacks-range-save]")
            save_button.hover()
            if not save_button.is_enabled():
                raise AssertionError("hovering feedbacks range save must not disable a valid >31 day range")
            if "62" in page.locator("[data-feedbacks-range-summary]").inner_text() and not save_button.is_enabled():
                raise AssertionError("valid >31 day range must not show max-range validation")
            save_button.click()
            if "24.03.2026 - 25.04.2026" not in page.locator("[data-feedbacks-range-label]").inner_text():
                raise AssertionError("feedbacks range save must apply a valid >31 day range")
            range_toggle.click()
            page.locator("[data-feedbacks-meta]").click()
            _assert_node_hidden(range_popover, True, "outside click must close feedbacks range picker")

            first_star = page.locator('[data-feedbacks-star][value="1"]')
            first_star.click()
            if first_star.is_checked():
                raise AssertionError("feedbacks star checkbox must update selection")
            if page.locator("[data-feedbacks-ai-analyze]").is_enabled():
                raise AssertionError("AI analyze button must be disabled before feedbacks load")
            if page.locator("[data-feedbacks-export]").is_enabled():
                raise AssertionError("Excel export button must be disabled before feedbacks load")

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
            if not page.locator("[data-feedbacks-export]").is_enabled():
                raise AssertionError("Excel export button must become enabled after feedbacks load")
            with page.expect_download() as download_info:
                page.locator("[data-feedbacks-export]").click()
            download = download_info.value
            if not download.suggested_filename.endswith(".xlsx"):
                raise AssertionError(f"feedbacks export download must be xlsx, got {download.suggested_filename!r}")
            if not export_requests or export_requests[-1]["row_count"] != 24:
                raise AssertionError(f"feedbacks export must use current visible rows, got {export_requests}")
            page.locator('[data-feedbacks-star][value="2"]').click()
            if page.locator("[data-feedbacks-table]").count() != 0:
                raise AssertionError("changing feedback filters must clear stale loaded table before next load")
            if page.locator("[data-feedbacks-export]").is_enabled():
                raise AssertionError("Excel export must disable after filters invalidate loaded rows")
            page.locator('[data-feedbacks-star][value="2"]').click()
            page.locator("[data-feedbacks-load]").click()
            page.wait_for_selector("[data-feedbacks-table] tbody tr")
            if page.locator("[data-feedbacks-ai-select]").count() != 0:
                raise AssertionError("feedbacks AI queue must not require manual row checkboxes")
            page.locator("[data-feedbacks-ai-analyze]").click()
            page.wait_for_selector("[data-feedbacks-prompt-textarea]")
            if "Сначала сохраните промпт разбора" not in page.locator("[data-feedbacks-error]").inner_text():
                raise AssertionError("AI analyze without saved prompt must show prompt-required message")
            if page.locator("[data-feedbacks-model]").count() != 1:
                raise AssertionError("feedbacks prompt panel must expose model selector")
            model_options = page.locator("[data-feedbacks-model] option").evaluate_all("nodes => nodes.map(node => node.value)")
            if "gpt-5.5" not in model_options:
                raise AssertionError(f"feedbacks model selector must expose discovered modern models, got {model_options}")
            prompt_width = page.locator("[data-feedbacks-prompt-textarea]").evaluate("node => node.getBoundingClientRect().width")
            editor_width = page.locator(".feedbacks-prompt-editor").evaluate("node => node.getBoundingClientRect().width")
            if prompt_width < editor_width * 0.94:
                raise AssertionError(f"feedbacks prompt textarea must use almost full editor width, got {prompt_width} of {editor_width}")
            page.locator("[data-feedbacks-model]").select_option("gpt-5.5")
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
            if page.locator("[data-feedbacks-column-resize]").count() == 0:
                raise AssertionError("feedbacks table must expose column resize handles")
            first_handle = page.locator("[data-feedbacks-column-resize='created_date']").first
            before_width = page.locator("[data-feedbacks-column-key='created_date']").first.evaluate("node => node.getBoundingClientRect().width")
            handle_box = first_handle.bounding_box()
            if not handle_box:
                raise AssertionError("feedbacks column resize handle must have a bounding box")
            page.mouse.move(handle_box["x"] + handle_box["width"] / 2, handle_box["y"] + handle_box["height"] / 2)
            page.mouse.down()
            page.mouse.move(handle_box["x"] + handle_box["width"] / 2 + 90, handle_box["y"] + handle_box["height"] / 2)
            page.mouse.up()
            after_width = page.locator("[data-feedbacks-column-key='created_date']").first.evaluate("node => node.getBoundingClientRect().width")
            if after_width <= before_width:
                raise AssertionError(f"feedbacks column resize must increase width, got {before_width} -> {after_width}")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("[data-unified-tab-button='feedbacks']")
            page.locator("[data-unified-tab-button='feedbacks']").click()
            page.locator("[data-feedbacks-load]").click()
            page.wait_for_selector("[data-feedbacks-table] tbody tr")
            persisted_width = page.locator("[data-feedbacks-column-key='created_date']").first.evaluate("node => node.getBoundingClientRect().width")
            if persisted_width < after_width - 2:
                raise AssertionError("feedbacks column width must persist in localStorage across reload")
            page.locator("[data-feedbacks-ai-analyze]").click()
            page.wait_for_function(
                "() => document.querySelector('[data-feedbacks-error]')?.textContent.includes('ошибками')"
            )
            if len(ai_request_batches) != 24:
                raise AssertionError(f"feedbacks AI queue must process all visible rows, got {ai_request_batches}")
            if any(len(batch) != 1 for batch in ai_request_batches):
                raise AssertionError(f"feedbacks AI queue must send one row per request, got {ai_request_batches}")
            if ai_request_batches[0] != ["browser-1"] or ai_request_batches[1] != ["browser-2"]:
                raise AssertionError(f"feedbacks AI queue must follow visible row order, got {ai_request_batches[:3]}")
            first_fit = page.locator("[data-feedbacks-table] tbody tr").nth(0).locator("td").nth(2).inner_text()
            if "Да" not in first_fit:
                raise AssertionError(f"AI-positive feedbacks must sort first, got {first_fit!r}")
            if "Ошибка" not in page.locator("[data-feedbacks-table] tbody").inner_text():
                raise AssertionError("row-level AI failure must be visible in the feedbacks table")
            page.locator("[data-feedbacks-ai-analyze]").click()
            page.wait_for_function(
                "() => document.querySelector('[data-feedbacks-source-note]')?.textContent.includes('AI готово')"
            )
            if len(ai_request_batches) != 25 or ai_request_batches[-1] != ["browser-2"]:
                raise AssertionError(f"feedbacks AI retry must process only failed/unresolved rows, got {ai_request_batches}")
            page.locator("[data-feedbacks-ai-filter]").select_option("yes")
            filtered_count = page.locator("[data-feedbacks-table] tbody tr").count()
            if filtered_count != 24:
                raise AssertionError(f"AI filter yes must leave analyzed queue rows, got {filtered_count}")

            large_feedbacks_mode = True
            page.locator("[data-feedbacks-load]").click()
            page.wait_for_function("() => document.querySelectorAll('[data-feedbacks-table] tbody tr').length === 650")
            if page.locator("[data-feedbacks-table-scroll]").evaluate("node => node.scrollHeight <= node.clientHeight"):
                raise AssertionError("large feedbacks table must stay inside internal scroll container")
            request_count_before_large_queue = len(ai_request_batches)
            page.locator("[data-feedbacks-ai-analyze]").click()
            page.wait_for_function(
                "() => document.querySelector('[data-feedbacks-error]')?.textContent.includes('максимум очереди')"
            )
            if len(ai_request_batches) != request_count_before_large_queue:
                raise AssertionError("oversized feedbacks AI queue must fail before sending row requests")
        finally:
            browser.close()

    return {
        "base_url": base_url,
        "feedbacks_tab_present": True,
        "range_outside_click_closes": True,
        "star_filter_changes_query": True,
        "table_rows": 24,
        "ai_columns_present": True,
        "ai_request_batches": len(ai_request_batches),
        "ai_request_max_batch_size": max((len(batch) for batch in ai_request_batches), default=0),
        "export_requests": len(export_requests),
        "large_table_rows": 650,
        "large_range_picker": True,
        "model_selector_discovery": selected_model,
        "prompt_full_width": True,
        "broken_local_storage_ignored": True,
        "resizable_columns": True,
        "ai_retry_requests": 1,
        "large_queue_blocked": True,
        "ai_filter_works": True,
    }


def _feedbacks_payload(*, row_count: int) -> dict[str, object]:
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
            "total": row_count,
            "by_star": {"1": 0, "2": 0, "3": 0, "4": 0, "5": row_count},
            "answered": 0,
            "unanswered": row_count,
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
            for index in range(1, row_count + 1)
        ],
    }


def _prompt_payload(*, status: str, model: str = "gpt-5-mini") -> dict[str, object]:
    prompt = "Сохранённый промпт" if status == "ready" else ""
    return {
        "contract_name": "sheet_vitrina_v1_feedbacks_ai_prompt",
        "contract_version": "v1",
        "prompt": prompt,
        "model": model,
        "available_models": ["gpt-5.5", "gpt-5.4-mini", "gpt-5-mini", "gpt-5"],
        "preferred_models": ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.2", "gpt-5.2-pro", "gpt-5", "gpt-5-mini", "gpt-5-nano"],
        "unavailable_models": [{"model": "gpt-5.4", "reason": "not returned by /v1/models for current key"}],
        "model_source": "saved" if status == "ready" else "default",
        "model_discovery_status": "available",
        "model_discovery_error": "",
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
