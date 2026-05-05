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
    DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_PATH,
    DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SYNC_STATUS_JOB_PATH,
    DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SYNC_STATUS_PATH,
    DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SUBMIT_JOB_PATH,
    DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SUBMIT_SELECTED_PATH,
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
    complaints_requests: list[str] = []
    complaints_sync_requests: list[dict[str, object]] = []
    complaints_job_polls: list[str] = []
    complaints_submit_requests: list[dict[str, object]] = []
    complaints_submit_job_polls: list[str] = []
    failed_once: set[str] = set()
    large_feedbacks_mode = False
    prompt_saved = False
    complaints_sync_completed = False
    complaints_submit_completed = False
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
            export_requests.append(
                {
                    "row_count": len(rows),
                    "first_id": str((rows[0] or {}).get("feedback_id") or ""),
                    "first_tags": list((rows[0] or {}).get("review_tags") or []),
                }
            )
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
            if feedback_id == "browser-1" and row.get("review_tags") != ["Плохое качество"]:
                raise AssertionError(f"feedbacks AI analyze request must include review_tags, got {row}")
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

        def fulfill_complaints(route: object) -> None:
            complaints_requests.append(route.request.url)
            route.fulfill(
                status=200,
                headers={"Content-Type": "application/json; charset=utf-8"},
                body=json.dumps(
                    _complaints_payload(status_label="Отклонена" if complaints_sync_completed else "Ждёт ответа"),
                    ensure_ascii=False,
                ),
            )

        def fulfill_complaints_sync(route: object) -> None:
            payload = json.loads(route.request.post_data or "{}")
            complaints_sync_requests.append(payload)
            route.fulfill(
                status=200,
                headers={"Content-Type": "application/json; charset=utf-8"},
                body=json.dumps(
                    {
                        "contract_name": "sheet_vitrina_v1_feedbacks_complaints_status_sync_job",
                        "contract_version": "v1",
                        "run_id": "complaints-sync-smoke-run",
                        "kind": "feedbacks_complaints_status_sync",
                        "status": "queued",
                        "already_running": False,
                        "created_at": "2026-05-02T05:00:00Z",
                        "started_at": "",
                        "finished_at": "",
                        "poll_url": DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SYNC_STATUS_JOB_PATH + "?run_id=complaints-sync-smoke-run",
                        "complaints_url": DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_PATH,
                        "summary": {},
                    },
                    ensure_ascii=False,
                ),
            )

        def fulfill_complaints_sync_job(route: object) -> None:
            nonlocal complaints_sync_completed
            complaints_job_polls.append(route.request.url)
            status = "success" if len(complaints_job_polls) >= 2 else "running"
            if status == "success":
                complaints_sync_completed = True
            route.fulfill(
                status=200,
                headers={"Content-Type": "application/json; charset=utf-8"},
                body=json.dumps(
                    {
                        "contract_name": "sheet_vitrina_v1_feedbacks_complaints_status_sync_job",
                        "contract_version": "v1",
                        "run_id": "complaints-sync-smoke-run",
                        "kind": "feedbacks_complaints_status_sync",
                        "status": status,
                        "already_running": False,
                        "created_at": "2026-05-02T05:00:00Z",
                        "started_at": "2026-05-02T05:00:01Z",
                        "finished_at": "2026-05-02T05:00:05Z" if status == "success" else "",
                        "poll_url": DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SYNC_STATUS_JOB_PATH + "?run_id=complaints-sync-smoke-run",
                        "complaints_url": DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_PATH,
                        "summary": {
                            "pending_rows_read": 1,
                            "answered_rows_read": 1,
                            "direct_matches": 1,
                            "strong_composite_matches": 0,
                            "weak_rejected": 1,
                            "statuses_updated": 1,
                        },
                        "statuses_updated": 1,
                        "weak_rejected": 1,
                        "direct_matches": 1,
                        "strong_composite_matches": 0,
                    },
                    ensure_ascii=False,
                ),
            )

        def fulfill_complaints_submit(route: object) -> None:
            payload = json.loads(route.request.post_data or "{}")
            feedback_ids = payload.get("feedback_ids") if isinstance(payload, dict) else None
            if not isinstance(feedback_ids, list) or not feedback_ids:
                raise AssertionError(f"submit-selected request must include feedback_ids, got {payload}")
            if "browser-1" in [str(item) for item in feedback_ids]:
                raise AssertionError("submit-selected request must not include already journaled feedback rows")
            max_submit = int(payload.get("max_submit") or 0)
            if max_submit < 1 or max_submit > 5:
                raise AssertionError(f"submit-selected request must enforce max_submit <= 5, got {payload}")
            complaints_submit_requests.append(payload)
            route.fulfill(
                status=200,
                headers={"Content-Type": "application/json; charset=utf-8"},
                body=json.dumps(
                    {
                        "contract_name": "sheet_vitrina_v1_feedbacks_complaints_submit_job",
                        "contract_version": "v1",
                        "run_id": "complaints-submit-smoke-run",
                        "kind": "feedbacks_complaints_submit_selected",
                        "status": "queued",
                        "created_at": "2026-05-06T05:00:00Z",
                        "started_at": "",
                        "finished_at": "",
                        "selected_count": len(feedback_ids),
                        "tested_count": 0,
                        "submitted_count": 0,
                        "skipped_count": 0,
                        "error_count": 0,
                        "events": [
                            {
                                "event": "job_started",
                                "message": "Submit-selected smoke queued",
                                "status": "queued",
                                "timestamp": "2026-05-06T05:00:00Z",
                            }
                        ],
                        "submitted_feedback_ids": [],
                        "skipped": [],
                        "summary": {"selected_count": len(feedback_ids), "max_submit": max_submit},
                        "poll_url": DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SUBMIT_JOB_PATH + "?run_id=complaints-submit-smoke-run",
                        "complaints_url": DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_PATH,
                    },
                    ensure_ascii=False,
                ),
            )

        def fulfill_complaints_submit_job(route: object) -> None:
            nonlocal complaints_submit_completed
            complaints_submit_job_polls.append(route.request.url)
            status = "success" if len(complaints_submit_job_polls) >= 2 else "running"
            if status == "success":
                complaints_submit_completed = True
            selected_ids = [
                str(item)
                for item in ((complaints_submit_requests[-1] or {}).get("feedback_ids") if complaints_submit_requests else [])
            ]
            selected_id = selected_ids[0] if selected_ids else "browser-2"
            events = [
                {
                    "event": "job_started",
                    "message": "Submit-selected smoke running",
                    "status": "running",
                    "timestamp": "2026-05-06T05:00:01Z",
                },
                {
                    "event": "row_selected",
                    "feedback_id": selected_id,
                    "message": "Row selected by operator",
                    "status": "running",
                    "timestamp": "2026-05-06T05:00:02Z",
                },
            ]
            if status == "success":
                events.extend(
                    [
                        {
                            "event": "row_submit_confirmed_success",
                            "feedback_id": selected_id,
                            "message": "WB accepted submit",
                            "status": "success",
                            "timestamp": "2026-05-06T05:00:04Z",
                        },
                        {
                            "event": "job_finished",
                            "message": "Submit-selected smoke finished",
                            "status": "success",
                            "timestamp": "2026-05-06T05:00:05Z",
                        },
                    ]
                )
            route.fulfill(
                status=200,
                headers={"Content-Type": "application/json; charset=utf-8"},
                body=json.dumps(
                    {
                        "contract_name": "sheet_vitrina_v1_feedbacks_complaints_submit_job",
                        "contract_version": "v1",
                        "run_id": "complaints-submit-smoke-run",
                        "kind": "feedbacks_complaints_submit_selected",
                        "status": status,
                        "created_at": "2026-05-06T05:00:00Z",
                        "started_at": "2026-05-06T05:00:01Z",
                        "finished_at": "2026-05-06T05:00:05Z" if status == "success" else "",
                        "selected_count": len(selected_ids) or 1,
                        "tested_count": 1 if status == "success" else 0,
                        "submitted_count": 1 if status == "success" else 0,
                        "skipped_count": 0,
                        "error_count": 0,
                        "events": events,
                        "submitted_feedback_ids": [selected_id] if status == "success" else [],
                        "skipped": [],
                        "summary": {
                            "selected_count": len(selected_ids) or 1,
                            "tested_count": 1 if status == "success" else 0,
                            "submitted_count": 1 if status == "success" else 0,
                            "skipped_count": 0,
                            "error_count": 0,
                        },
                        "poll_url": DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SUBMIT_JOB_PATH + "?run_id=complaints-submit-smoke-run",
                        "complaints_url": DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_PATH,
                    },
                    ensure_ascii=False,
                ),
            )

        context.route("**" + DEFAULT_SHEET_FEEDBACKS_PATH + "?**", fulfill_feedbacks)
        context.route("**" + DEFAULT_SHEET_FEEDBACKS_EXPORT_PATH, fulfill_export)
        context.route("**" + DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_PATH, fulfill_complaints)
        context.route("**" + DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SYNC_STATUS_PATH, fulfill_complaints_sync)
        context.route("**" + DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SYNC_STATUS_JOB_PATH + "?**", fulfill_complaints_sync_job)
        context.route("**" + DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SUBMIT_SELECTED_PATH, fulfill_complaints_submit)
        context.route("**" + DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SUBMIT_JOB_PATH + "?**", fulfill_complaints_submit_job)
        context.route("**" + DEFAULT_SHEET_FEEDBACKS_AI_ANALYZE_PATH, fulfill_ai_analyze)
        context.route("**" + DEFAULT_SHEET_FEEDBACKS_AI_PROMPT_PATH, fulfill_prompt)
        context.route("**" + DEFAULT_SHEET_WEB_VITRINA_READ_PATH + "?**", fulfill_web_vitrina_read)
        try:
            page.goto(page_url, wait_until="domcontentloaded")
            page.wait_for_selector("[data-unified-tab-button='feedbacks']")
            page.locator("[data-unified-tab-button='feedbacks']").click()
            page.wait_for_selector("[data-feedbacks-panel]")
            page.wait_for_selector("[data-feedbacks-subtab='reviews']")
            page.wait_for_selector("[data-feedbacks-subtab='prompt']")
            page.wait_for_selector("[data-feedbacks-subtab='complaints']")
            if complaints_sync_requests:
                raise AssertionError("complaints status sync must not auto-run on page load")
            page.locator("[data-feedbacks-subtab='complaints']").click()
            page.wait_for_selector("[data-feedbacks-complaints-table] tbody tr")
            if not complaints_requests:
                raise AssertionError("complaints subtab must load runtime journal")
            complaints_table_text = page.locator("[data-feedbacks-complaints-table]").inner_text()
            complaints_header_text = page.locator("[data-feedbacks-complaints-table] thead").inner_text()
            if "Ждёт ответа" not in complaints_table_text:
                raise AssertionError("complaints table must render waiting response status")
            for expected in ("Категория WB", "Текст жалобы", "Match status"):
                if expected not in complaints_header_text:
                    raise AssertionError(f"complaints table must render {expected!r}; header={complaints_header_text!r}")
            if page.locator("[data-feedbacks-complaints-column-id='match_score']").count() != 1:
                raise AssertionError("complaints table must expose column visibility controls")
            page.locator("[data-feedbacks-complaints-column-manager] summary").click()
            page.locator("[data-feedbacks-complaints-column-id='match_score']").click()
            if "Match score" in page.locator("[data-feedbacks-complaints-table] thead").inner_text():
                raise AssertionError("complaints column visibility must hide optional columns")
            if page.locator("button", has_text="Подать жалобу").count() or page.locator("button", has_text="Отправить жалобу").count():
                raise AssertionError("public complaints UI must not expose submit controls")
            before_sync_load_count = len(complaints_requests)
            page.locator("[data-feedbacks-complaints-sync]").click()
            page.wait_for_function("() => document.querySelector('[data-feedbacks-complaints-status]')?.textContent.includes('Запущено обновление статусов')")
            page.wait_for_function("() => document.querySelector('[data-feedbacks-complaints-status]')?.textContent.includes('Последний sync: success')")
            if not complaints_sync_requests:
                raise AssertionError("complaints status sync button must call the sync route")
            if not complaints_job_polls:
                raise AssertionError("complaints status sync UI must poll the job route")
            page.wait_for_function(
                "() => document.querySelector('[data-feedbacks-complaints-table]')?.textContent.includes('Отклонена')"
            )
            if len(complaints_requests) <= before_sync_load_count:
                raise AssertionError("complaints table must refresh after fake status sync completion")
            if "Отклонена" not in page.locator("[data-feedbacks-complaints-table]").inner_text():
                raise AssertionError("complaints table must render refreshed rows after status sync")
            page.locator("[data-feedbacks-subtab='reviews']").click()

            range_toggle = page.locator("[data-feedbacks-range-toggle]")
            range_popover = page.locator("[data-feedbacks-range-popover]")
            range_toggle.click()
            _assert_node_hidden(range_popover, False, "feedbacks range picker must open")
            if page.locator('[data-feedbacks-range-day="2026-04-25"]').count() != 1:
                page.locator("[data-feedbacks-range-prev]").click()
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
            if "Теги" not in page.locator("[data-feedbacks-table] thead").inner_text():
                raise AssertionError("feedbacks table must include review tags column")
            if "Жалоба" not in page.locator("[data-feedbacks-table] thead").inner_text():
                raise AssertionError("feedbacks table must include complaint status column")
            if "Плохое качество" not in page.locator("[data-feedbacks-table] tbody").inner_text():
                raise AssertionError("feedbacks table must render review tag chips/text")
            if page.locator("[data-feedbacks-select-row]:not([disabled])").count() != 0:
                raise AssertionError("feedback row selection must stay disabled before AI analysis is complete")
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
            if export_requests[-1]["first_tags"] != ["Плохое качество"]:
                raise AssertionError(f"feedbacks export must send review_tags, got {export_requests[-1]}")
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
            first_fit = page.locator("[data-feedbacks-table] tbody tr").nth(0).locator("td").nth(4).inner_text()
            if "Да" not in first_fit:
                raise AssertionError(f"AI-positive feedbacks must sort first, got {first_fit!r}")
            table_text_after_ai = page.locator("[data-feedbacks-table] tbody").inner_text()
            if "Нецензурная лексика" not in table_text_after_ai:
                raise AssertionError("feedbacks AI table must render exact WB category labels")
            for old_label in ("Недостаточно данных", "Претензия к товару", "Доставка, ПВЗ или логистика WB", "Мат, оскорбления или угрозы"):
                if old_label in table_text_after_ai:
                    raise AssertionError(f"feedbacks AI table must not render old/internal category label {old_label!r}")
            if "В отзыве присутствует нецензурная лексика" not in table_text_after_ai:
                raise AssertionError("feedbacks AI reason column must render complaint description text")
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

            page.locator("[data-feedbacks-subtab='reviews']").click()
            large_feedbacks_mode = True
            page.locator("[data-feedbacks-load]").click()
            page.wait_for_function("() => document.querySelectorAll('[data-feedbacks-table] tbody tr').length === 650")
            page.wait_for_selector("[data-feedbacks-table-scroll]", state="visible")
            large_scroll_metrics = page.locator("[data-feedbacks-table-scroll]").last.evaluate(
                "node => ({scrollHeight: node.scrollHeight, clientHeight: node.clientHeight, rows: document.querySelectorAll('[data-feedbacks-table] tbody tr').length})"
            )
            if large_scroll_metrics["scrollHeight"] <= large_scroll_metrics["clientHeight"]:
                raise AssertionError(f"large feedbacks table must stay inside internal scroll container: {large_scroll_metrics}")
            request_count_before_large_queue = len(ai_request_batches)
            page.locator("[data-feedbacks-ai-analyze]").click()
            page.wait_for_function(
                "() => document.querySelector('[data-feedbacks-error]')?.textContent.includes('максимум очереди')"
            )
            if len(ai_request_batches) != request_count_before_large_queue:
                raise AssertionError("oversized feedbacks AI queue must fail before sending row requests")

            large_feedbacks_mode = False
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("[data-unified-tab-button='feedbacks']")
            page.locator("[data-unified-tab-button='feedbacks']").click()
            page.locator("[data-feedbacks-load]").click()
            page.wait_for_function("() => document.querySelectorAll('[data-feedbacks-table] tbody tr').length === 24")
            page.locator("[data-feedbacks-ai-analyze]").click()
            page.wait_for_function(
                "() => document.querySelector('[data-feedbacks-source-note]')?.textContent.includes('AI готово')"
            )
            page.wait_for_function("() => Array.from(document.querySelectorAll('[data-feedbacks-select-row]')).some(node => !node.disabled)")
            if page.locator('[data-feedbacks-select-row="browser-1"]').count() != 1:
                raise AssertionError("feedbacks table must render a row checkbox for the journaled row")
            if not page.locator('[data-feedbacks-select-row="browser-1"]').is_disabled():
                raise AssertionError("existing complaint journal records must disable selection")
            first_enabled_checkbox = page.locator("[data-feedbacks-select-row]:not([disabled])").first
            selected_feedback_id = first_enabled_checkbox.get_attribute("data-feedbacks-select-row")
            if not selected_feedback_id:
                raise AssertionError("enabled submit checkbox must expose feedback_id")
            first_enabled_checkbox.check()
            if not page.locator("[data-feedbacks-submit-selected]").is_enabled():
                raise AssertionError("submit-selected button must enable after an analyzed, non-journaled row is selected")
            if "Выбрано: 1" not in page.locator("[data-feedbacks-submit-selected-count]").inner_text():
                raise AssertionError("submit-selected UI must show selected count")
            page.locator("[data-feedbacks-submit-selected]").click()
            page.wait_for_function(
                "() => document.querySelector('[data-feedbacks-submit-log-body]')?.textContent.includes('row_submit_confirmed_success')"
            )
            submit_log_text = page.locator("[data-feedbacks-submit-log-body]").inner_text()
            if "status: success" not in submit_log_text:
                raise AssertionError(f"submit-selected log must render final job status, got {submit_log_text!r}")
            if not complaints_submit_requests:
                raise AssertionError("submit-selected button must call backend route")
            if complaints_submit_requests[-1].get("feedback_ids") != [selected_feedback_id]:
                raise AssertionError(f"submit-selected payload must include selected id only, got {complaints_submit_requests[-1]}")
            if not complaints_submit_job_polls:
                raise AssertionError("submit-selected UI must poll job route")
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
        "complaints_tab_visible": True,
        "complaints_sync_requests": len(complaints_sync_requests),
        "complaints_job_polls": len(complaints_job_polls),
        "complaints_submit_requests": len(complaints_submit_requests),
        "complaints_submit_job_polls": len(complaints_submit_job_polls),
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
                "review_tags": ["Плохое качество"] if index == 1 else [],
                "tag_source": "official_wb_api" if index == 1 else "none",
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
        "starter_prompt": "Стартовый промпт разбора отзывов: reason = текст для поля «Опишите ситуацию», category только WB.",
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
                "category": "product_quality_claim" if feedback_id == "browser-3" else "profanity",
                "category_label": "Претензия к товару" if feedback_id == "browser-3" else "Нецензурная лексика",
                "reason": "В отзыве присутствует нецензурная лексика, что нарушает правила публикации отзывов.",
                "confidence": "high",
                "confidence_label": "Высокая",
                "evidence": "фрагмент",
            }
            for feedback_id in feedback_ids
        ],
    }


def _complaints_payload(*, status_label: str = "Ждёт ответа") -> dict[str, object]:
    waiting_count = 1 if status_label == "Ждёт ответа" else 0
    rejected_count = 1 if status_label == "Отклонена" else 0
    return {
        "contract_name": "sheet_vitrina_v1_feedbacks_complaints",
        "contract_version": "v1",
        "meta": {
            "record_count": 1,
            "auto_sync_on_page_load": False,
            "generated_at": "2026-05-02T04:00:00Z",
        },
        "summary": {
            "total": 1,
            "waiting_response": waiting_count,
            "satisfied": 0,
            "rejected": rejected_count,
            "error": 0,
        },
        "schema": {
            "columns": [
                {"key": "complaint_status_label", "label": "Статус жалобы"},
                {"key": "wb_category_label", "label": "Категория WB"},
                {"key": "complaint_text", "label": "Текст жалобы"},
                {"key": "submitted_at", "label": "Дата подачи"},
                {"key": "review_created_at", "label": "Дата отзыва"},
                {"key": "rating", "label": "Оценка"},
                {"key": "nm_id", "label": "nmId"},
                {"key": "supplier_article", "label": "Артикул"},
                {"key": "product_name", "label": "Товар"},
                {"key": "review_text", "label": "Текст отзыва"},
                {"key": "ai_complaint_fit_label", "label": "Подходит для жалобы"},
                {"key": "ai_category_label", "label": "Категория AI"},
                {"key": "ai_reason", "label": "Причина AI / текст ситуации"},
                {"key": "feedback_id", "label": "ID отзыва"},
                {"key": "match_status", "label": "Match status"},
                {"key": "match_score", "label": "Match score"},
                {"key": "last_error", "label": "Ошибка"},
            ]
        },
        "rows": [
            {
                "complaint_status_label": status_label,
                "wb_category_label": "Другое",
                "complaint_text": "Просим проверить отзыв: тестовое описание.",
                "submitted_at": "2026-05-02T04:00:00Z",
                "review_created_at": "2026-05-01T12:00:00Z",
                "rating": "1",
                "nm_id": "123456",
                "supplier_article": "ART-1",
                "product_name": "Товар",
                "review_text": "Текст отзыва",
                "ai_complaint_fit_label": "Да",
                "ai_category_label": "Другое",
                "ai_reason": "Просим проверить отзыв: тестовое описание.",
                "feedback_id": "browser-1",
                "match_status": "exact",
                "match_score": "1.0",
                "last_error": "",
            }
        ],
    }


def _assert_node_hidden(locator: object, expected_hidden: bool, message: str) -> None:
    actual_hidden = locator.evaluate("node => node.hidden")
    if bool(actual_hidden) != expected_hidden:
        raise AssertionError(message)


if __name__ == "__main__":
    main()
