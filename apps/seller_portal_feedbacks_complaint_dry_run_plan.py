"""No-submit Seller Portal complaint draft dry-run.

This runner exercises the bounded technical chain for a future complaint flow:
WB feedbacks API -> transient AI analysis -> exact Seller Portal match -> modal
draft. It never clicks the final complaint submit button and does not persist
operator decisions or statuses.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import Error as PlaywrightError, Page, sync_playwright  # noqa: E402

from apps.seller_portal_feedbacks_complaints_scout import (  # noqa: E402
    BUSINESS_TZ,
    DEFAULT_START_URL,
    ROW_MENU_COMPLAINT_LABEL,
    ScoutConfig,
    _click_safe_row_menu,
    _click_tab_like,
    _safe_escape,
    _wait_for_feedback_rows,
    _wait_settle,
    assert_safe_click_label,
    check_session,
    click_open_row_menu_complaint_action,
    close_modal_without_submit,
    detect_complaint_success_state,
    extract_complaint_modal_state,
    extract_open_row_menu_state,
    extract_visible_feedback_rows,
    navigate_to_feedbacks_questions,
)
from apps.seller_portal_feedbacks_matching_replay import (  # noqa: E402
    NO_SUBMIT_MODE,
    ReplayConfig,
    capture_seller_portal_feedback_headers,
    collect_feedback_rows_from_seller_portal_network,
    collect_feedback_rows_with_scroll,
    match_one_api_row,
    normalize_article,
    normalize_datetime_minute,
    normalize_nm_id,
    normalize_rating,
    parse_stars,
    safe_text,
    summarize_api_row,
    summarize_ui_row,
)
from apps.seller_portal_relogin_session import (  # noqa: E402
    DEFAULT_STORAGE_STATE_PATH,
    DEFAULT_WB_BOT_PYTHON,
)
from packages.application.sheet_vitrina_v1_feedbacks import SheetVitrinaV1FeedbacksBlock  # noqa: E402
from packages.application.sheet_vitrina_v1_feedbacks_ai import (  # noqa: E402
    MAX_ROWS_PER_RUN,
    SheetVitrinaV1FeedbacksAiBlock,
)


CONTRACT_NAME = "seller_portal_feedbacks_complaint_dry_run_plan"
CONTRACT_VERSION = "no_submit_v1"
DEFAULT_OUTPUT_ROOT = Path("/opt/wb-core-runtime/state/feedbacks_complaint_dry_run_plan")
LOCAL_OUTPUT_ROOT = Path("artifacts/seller_portal_feedbacks_complaint_dry_run_plan")
SELLER_PORTAL_WRITE_ACTIONS_ALLOWED = False
DEFAULT_RUNTIME_DIR = Path(os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR", "/opt/wb-core-runtime/state"))
TEXT_WS_RE = re.compile(r"\s+")
DRAFT_LIMIT = 500
DESCRIPTION_FIELD_MARKER_ATTR = "data-wb-core-description-field"


@dataclass(frozen=True)
class DryRunConfig:
    date_from: str
    date_to: str
    stars: tuple[int, ...]
    is_answered: str
    max_api_rows: int
    max_ai_candidates: int
    force_category_other: bool
    mode: str
    runtime_dir: Path
    storage_state_path: Path
    wb_bot_python: Path
    output_dir: Path
    start_url: str
    headless: bool
    timeout_ms: int
    write_artifacts: bool


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--stars", default="1")
    parser.add_argument("--is-answered", choices=("true", "false", "all"), default="false")
    parser.add_argument("--max-api-rows", type=int, default=10)
    parser.add_argument("--max-ai-candidates", type=int, default=3)
    parser.add_argument("--force-category-other", choices=("0", "1"), default="1")
    parser.add_argument("--mode", choices=(NO_SUBMIT_MODE,), default=NO_SUBMIT_MODE)
    parser.add_argument("--runtime-dir", default=str(DEFAULT_RUNTIME_DIR if DEFAULT_RUNTIME_DIR.exists() else ".runtime"))
    parser.add_argument("--storage-state-path", default=str(DEFAULT_STORAGE_STATE_PATH))
    parser.add_argument("--wb-bot-python", default=str(DEFAULT_WB_BOT_PYTHON))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--start-url", default=DEFAULT_START_URL)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--timeout-ms", type=int, default=20000)
    parser.add_argument("--no-artifacts", action="store_true")
    args = parser.parse_args()

    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else (DEFAULT_OUTPUT_ROOT if Path("/opt/wb-core-runtime/state").exists() else LOCAL_OUTPUT_ROOT)
    )
    config = DryRunConfig(
        date_from=normalize_requested_date(args.date_from),
        date_to=normalize_requested_date(args.date_to),
        stars=parse_stars(args.stars),
        is_answered=args.is_answered,
        max_api_rows=max(1, int(args.max_api_rows)),
        max_ai_candidates=max(1, int(args.max_ai_candidates)),
        force_category_other=str(args.force_category_other) == "1",
        mode=args.mode,
        runtime_dir=Path(args.runtime_dir).expanduser(),
        storage_state_path=Path(args.storage_state_path).expanduser(),
        wb_bot_python=Path(args.wb_bot_python).expanduser(),
        output_dir=output_dir,
        start_url=str(args.start_url).rstrip("/") or DEFAULT_START_URL,
        headless=not args.headed,
        timeout_ms=max(5000, int(args.timeout_ms)),
        write_artifacts=not bool(args.no_artifacts),
    )
    report = run_dry_run(config)
    if config.write_artifacts:
        paths = write_report_artifacts(report, config.output_dir)
        report["artifact_paths"] = {key: str(path) for key, path in paths.items()}
    print(json.dumps(compact_stdout_report(report), ensure_ascii=False, indent=2))


def run_dry_run(config: DryRunConfig) -> dict[str, Any]:
    if config.mode != NO_SUBMIT_MODE:
        raise RuntimeError("complaint dry-run supports no-submit mode only")

    report: dict[str, Any] = {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "mode": config.mode,
        "started_at": iso_now(),
        "finished_at": None,
        "parameters": {
            "date_from": config.date_from,
            "date_to": config.date_to,
            "stars": list(config.stars),
            "is_answered": config.is_answered,
            "max_api_rows": config.max_api_rows,
            "max_ai_candidates": config.max_ai_candidates,
            "force_category_other": config.force_category_other,
        },
        "read_only_guards": no_submit_guards(),
        "api": {},
        "ai": {},
        "session": {},
        "navigation": {},
        "ui": {},
        "candidates": [],
        "aggregate": empty_aggregate(),
        "errors": [],
    }

    api_report = load_api_feedback_rows(config)
    report["api"] = api_report
    api_rows = api_report.get("rows") if isinstance(api_report.get("rows"), list) else []
    if not api_rows:
        report["aggregate"] = build_aggregate(report["candidates"])
        report["finished_at"] = iso_now()
        return report

    ai_report = analyze_feedback_rows(config, api_rows)
    report["ai"] = ai_report
    if not ai_report.get("success"):
        report["errors"].append(
            {
                "stage": "ai_analyze",
                "code": str(ai_report.get("error_code") or "ai_failed"),
                "message": str(ai_report.get("blocker") or "AI analysis failed"),
            }
        )
        report["aggregate"] = build_aggregate(report["candidates"])
        report["finished_at"] = iso_now()
        return report

    analysis_by_id = {str(item.get("feedback_id") or ""): item for item in ai_report.get("results") or []}
    selected_ids = select_ai_candidate_ids(list(analysis_by_id.values()), max_candidates=config.max_ai_candidates)
    candidates = build_candidate_records(api_rows, analysis_by_id, selected_ids)

    selected_api_rows = [row for row in api_rows if str(row.get("feedback_id") or "") in selected_ids]
    if selected_api_rows:
        matching_report = collect_matching_rows(config, selected_api_rows)
        report["session"] = matching_report.get("session") or {}
        report["navigation"] = matching_report.get("navigation") or {}
        report["ui"] = matching_report.get("ui") or {}
        if matching_report.get("errors"):
            report["errors"].extend(matching_report["errors"])
        ui_rows = report["ui"].get("rows") if isinstance(report["ui"].get("rows"), list) else []
        apply_exact_matches(candidates, selected_api_rows, ui_rows)
        exact_candidates = [
            candidate
            for candidate in candidates
            if candidate.get("selected_for_dry_run") and should_open_modal_for_match(candidate.get("match") or {})
        ]
        if exact_candidates:
            modal_report = draft_modals_for_exact_candidates(config, exact_candidates, selected_api_rows)
            report["session"] = modal_report.get("session") or report["session"]
            report["navigation"] = modal_report.get("navigation") or report["navigation"]
            if modal_report.get("ui"):
                report["ui"]["modal_draft_ui"] = modal_report.get("ui")
            if modal_report.get("errors"):
                report["errors"].extend(modal_report["errors"])
            apply_modal_results(candidates, modal_report.get("candidate_results") or [])
    else:
        report["session"] = {}
        report["navigation"] = {}
        report["ui"] = {}

    report["candidates"] = candidates
    report["aggregate"] = build_aggregate(candidates)
    report["read_only_guards"]["submit_clicked_count"] = report["aggregate"]["submit_clicked_count"]
    report["finished_at"] = iso_now()
    return report


def load_api_feedback_rows(config: DryRunConfig) -> dict[str, Any]:
    report: dict[str, Any] = {
        "success": False,
        "requested": {
            "date_from": config.date_from,
            "date_to": config.date_to,
            "stars": list(config.stars),
            "is_answered": config.is_answered,
            "max_api_rows": config.max_api_rows,
        },
        "row_count": 0,
        "total_available_rows": 0,
        "limited": False,
        "feedback_id_available": False,
        "rows": [],
        "meta": {},
        "blocker": "",
    }
    try:
        payload = SheetVitrinaV1FeedbacksBlock().build(
            date_from=config.date_from,
            date_to=config.date_to,
            stars=list(config.stars),
            is_answered=config.is_answered,
        )
    except Exception as exc:
        report["blocker"] = safe_text(str(exc), 500)
        report["error_code"] = exc.__class__.__name__
        return report

    all_rows = [row for row in payload.get("rows") or [] if isinstance(row, dict)]
    rows = all_rows[: config.max_api_rows]
    report.update(
        {
            "success": True,
            "row_count": len(rows),
            "total_available_rows": len(all_rows),
            "limited": len(all_rows) > len(rows),
            "feedback_id_available": any(bool(row.get("feedback_id")) for row in rows),
            "rows": rows,
            "meta": payload.get("meta") or {},
            "summary": payload.get("summary") or {},
            "blocker": "" if rows else "No feedback rows matched requested filters; date range was not expanded.",
        }
    )
    return report


def analyze_feedback_rows(config: DryRunConfig, rows: list[dict[str, Any]]) -> dict[str, Any]:
    report: dict[str, Any] = {
        "success": False,
        "prompt_status": "",
        "model": "",
        "model_source": "",
        "row_count": 0,
        "results": [],
        "counts": {},
        "blocker": "",
        "error_code": "",
    }
    try:
        block = SheetVitrinaV1FeedbacksAiBlock(
            runtime_dir=config.runtime_dir,
            min_analyze_interval_seconds=0.0,
        )
        prompt_payload = block.get_prompt()
        report["prompt_status"] = str(prompt_payload.get("status") or "")
        report["model"] = str(prompt_payload.get("model") or "")
        report["model_source"] = str(prompt_payload.get("model_source") or "")
        if prompt_payload.get("status") != "ready":
            report["blocker"] = "Saved feedbacks AI prompt is missing; dry-run does not modify prompt/model config."
            return report
        results: list[dict[str, Any]] = []
        provider_batches: list[dict[str, Any]] = []
        for batch in chunks(rows, MAX_ROWS_PER_RUN):
            payload = block.analyze({"rows": batch})
            results.extend([dict(item) for item in payload.get("results") or [] if isinstance(item, dict)])
            provider_batches.extend((payload.get("meta") or {}).get("provider_batches") or [])
        counts = Counter(str(item.get("complaint_fit") or "unknown") for item in results)
        report.update(
            {
                "success": True,
                "row_count": len(results),
                "results": results,
                "counts": dict(counts),
                "provider_batches": provider_batches,
                "persistence": "not_persisted",
            }
        )
    except Exception as exc:
        report["blocker"] = safe_text(str(exc), 800)
        report["error_code"] = exc.__class__.__name__
    return report


def select_ai_candidate_ids(results: list[Mapping[str, Any]], *, max_candidates: int) -> list[str]:
    selected: list[str] = []
    for fit in ("yes", "review"):
        for result in results:
            feedback_id = str(result.get("feedback_id") or "")
            if result.get("complaint_fit") == fit and feedback_id and feedback_id not in selected:
                selected.append(feedback_id)
                if len(selected) >= max_candidates:
                    return selected
    return selected


def build_candidate_records(
    api_rows: list[dict[str, Any]],
    analysis_by_id: Mapping[str, Mapping[str, Any]],
    selected_ids: list[str],
) -> list[dict[str, Any]]:
    selected_set = set(selected_ids)
    records: list[dict[str, Any]] = []
    for row in api_rows:
        feedback_id = str(row.get("feedback_id") or "")
        analysis = dict(analysis_by_id.get(feedback_id) or {})
        selected = feedback_id in selected_set
        fit = str(analysis.get("complaint_fit") or "unknown")
        records.append(
            {
                "feedback_id": feedback_id,
                "api_summary": summarize_api_row(row),
                "ai": summarize_ai_result(analysis),
                "selected_for_dry_run": selected,
                "selection_reason": selection_reason(fit, selected),
                "match": {},
                "modal": empty_modal_candidate_state(),
                "skip_reason": "" if selected else selection_reason(fit, selected),
            }
        )
    return records


def collect_matching_rows(config: DryRunConfig, selected_api_rows: list[dict[str, Any]]) -> dict[str, Any]:
    replay_config = build_replay_config(config, max_ui_rows=max(config.max_api_rows * 6, 60))
    scout_config = build_scout_config(config)
    session = check_session(scout_config)
    report: dict[str, Any] = {
        "success": False,
        "session": session,
        "navigation": {},
        "ui": {
            "rows": [],
            "rows_collected": 0,
            "dom_rows_collected": 0,
            "seller_portal_network_rows_collected": 0,
            "collection_strategy": "none",
            "hidden_feedback_id_available": False,
            "seller_portal_network_feedback_id_available": False,
            "scroll_stats": {},
            "seller_portal_network_stats": {},
            "blocker": "",
        },
        "errors": [],
    }
    if not selected_api_rows:
        report["ui"]["blocker"] = "No AI-selected candidates to match"
        return report
    if not session.get("ok"):
        report["ui"]["blocker"] = "Seller Portal session is not valid"
        report["errors"].append({"stage": "session", "code": str(session.get("status") or ""), "message": str(session.get("message") or "")})
        return report

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=config.headless)
            context = browser.new_context(
                storage_state=str(config.storage_state_path),
                locale="ru-RU",
                timezone_id=BUSINESS_TZ,
                viewport={"width": 1600, "height": 1200},
                accept_downloads=False,
            )
            page = context.new_page()
            page.set_default_timeout(config.timeout_ms)
            request_headers: dict[str, str] = {}
            page.on("request", lambda request: capture_seller_portal_feedback_headers(request, request_headers))
            try:
                navigation = navigate_to_feedbacks_questions(page, scout_config)
                report["navigation"] = navigation
                if not navigation.get("success"):
                    report["ui"]["blocker"] = str(navigation.get("blocker") or "Отзывы и вопросы page not reached")
                    return report
                if not _click_tab_like(page, "Отзывы"):
                    report["ui"]["blocker"] = "Отзывы tab was not found"
                    return report
                _wait_settle(page, 2500)
                _wait_for_feedback_rows(page, timeout_ms=10000)
                dom_rows, scroll_stats = collect_feedback_rows_with_scroll(page, max_rows=replay_config.max_ui_rows, date_from=config.date_from)
                network_rows, network_stats = collect_feedback_rows_from_seller_portal_network(
                    page,
                    replay_config,
                    request_headers=request_headers,
                )
                rows = network_rows if network_rows else dom_rows
                report["ui"].update(
                    {
                        "rows": rows,
                        "rows_collected": len(rows),
                        "dom_rows_collected": len(dom_rows),
                        "seller_portal_network_rows_collected": len(network_rows),
                        "collection_strategy": "seller_portal_network_cursor" if network_rows else "dom_scroll",
                        "hidden_feedback_id_available": any(bool(row.get("hidden_feedback_id")) for row in rows),
                        "seller_portal_network_feedback_id_available": any(bool(row.get("feedback_id")) for row in rows),
                        "scroll_stats": scroll_stats,
                        "seller_portal_network_stats": network_stats,
                        "blocker": "" if rows else "No Seller Portal rows were collected",
                    }
                )
                report["success"] = bool(rows)
            finally:
                context.close()
                browser.close()
    except Exception as exc:
        report["ui"]["blocker"] = safe_text(str(exc), 500)
        report["errors"].append({"stage": "matching_browser", "code": exc.__class__.__name__, "message": safe_text(str(exc), 800)})
    return report


def apply_exact_matches(
    candidates: list[dict[str, Any]],
    selected_api_rows: list[dict[str, Any]],
    ui_rows: list[dict[str, Any]],
) -> None:
    api_by_id = {str(row.get("feedback_id") or ""): row for row in selected_api_rows}
    for candidate in candidates:
        if not candidate.get("selected_for_dry_run"):
            continue
        api_row = api_by_id.get(str(candidate.get("feedback_id") or ""))
        if not api_row:
            candidate["skip_reason"] = "selected API row is unavailable"
            continue
        match = match_one_api_row(api_row, ui_rows)
        candidate["match"] = match
        if should_open_modal_for_match(match):
            candidate["skip_reason"] = ""
        else:
            candidate["skip_reason"] = f"match_status={match.get('match_status')} is not exact; modal draft blocked"


def should_open_modal_for_match(match: Mapping[str, Any]) -> bool:
    return bool(match.get("match_status") == "exact" and match.get("safe_for_future_submit"))


def draft_modals_for_exact_candidates(
    config: DryRunConfig,
    exact_candidates: list[dict[str, Any]],
    api_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    scout_config = build_scout_config(config)
    session = check_session(scout_config)
    report: dict[str, Any] = {
        "success": False,
        "session": session,
        "navigation": {},
        "ui": {
            "visible_rows_checked": 0,
            "visible_row_match_source": "dom_exact_fields",
            "blocker": "",
        },
        "candidate_results": [],
        "errors": [],
    }
    if not session.get("ok"):
        report["ui"]["blocker"] = "Seller Portal session is not valid"
        report["errors"].append({"stage": "session", "code": str(session.get("status") or ""), "message": str(session.get("message") or "")})
        return report

    api_by_id = {str(row.get("feedback_id") or ""): row for row in api_rows}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=config.headless)
            context = browser.new_context(
                storage_state=str(config.storage_state_path),
                locale="ru-RU",
                timezone_id=BUSINESS_TZ,
                viewport={"width": 1600, "height": 1200},
                accept_downloads=False,
            )
            page = context.new_page()
            page.set_default_timeout(config.timeout_ms)
            request_headers: dict[str, str] = {}
            page.on("request", lambda request: capture_seller_portal_feedback_headers(request, request_headers))
            try:
                navigation = navigate_to_feedbacks_questions(page, scout_config)
                report["navigation"] = navigation
                if not navigation.get("success"):
                    report["ui"]["blocker"] = str(navigation.get("blocker") or "Отзывы и вопросы page not reached")
                    return report
                if not _click_tab_like(page, "Отзывы"):
                    report["ui"]["blocker"] = "Отзывы tab was not found"
                    return report
                _wait_settle(page, 2500)
                _wait_for_feedback_rows(page, timeout_ms=10000)
                for candidate in exact_candidates:
                    feedback_id = str(candidate.get("feedback_id") or "")
                    api_row = api_by_id.get(feedback_id)
                    result = draft_one_candidate_modal(page, config, candidate, api_row, request_headers)
                    report["candidate_results"].append(result)
                    if result.get("submit_clicked"):
                        report["errors"].append(
                            {"stage": "no_submit_guard", "code": "submit_clicked", "message": f"submit clicked for {feedback_id}"}
                        )
                report["success"] = any(bool(item.get("modal_opened")) for item in report["candidate_results"])
            finally:
                context.close()
                browser.close()
    except Exception as exc:
        report["ui"]["blocker"] = safe_text(str(exc), 500)
        report["errors"].append({"stage": "modal_browser", "code": exc.__class__.__name__, "message": safe_text(str(exc), 800)})
    return report


def draft_one_candidate_modal(
    page: Page,
    config: DryRunConfig,
    candidate: Mapping[str, Any],
    api_row: Mapping[str, Any] | None,
    request_headers: Mapping[str, str],
) -> dict[str, Any]:
    feedback_id = str(candidate.get("feedback_id") or "")
    result = empty_modal_candidate_state()
    result.update({"feedback_id": feedback_id})
    if not api_row:
        result["blocker"] = "API row unavailable for modal draft"
        return result

    visible_rows = extract_visible_feedback_rows(page, max_rows=20)
    result["visible_rows_checked"] = len(visible_rows)
    expected_ui = (candidate.get("match") or {}).get("best_ui_candidate") or {}
    visible_match = find_visible_actionable_row(api_row, visible_rows, expected_ui=expected_ui)
    if not visible_match.get("row"):
        search_result = apply_article_search_for_candidate(page, api_row, expected_ui=expected_ui)
        result["targeted_search"] = search_result
        if search_result.get("ok"):
            _wait_settle(page, 2500)
            visible_rows = extract_visible_feedback_rows(page, max_rows=20)
            result["visible_rows_checked_after_search"] = len(visible_rows)
            visible_match = find_visible_actionable_row(api_row, visible_rows, expected_ui=expected_ui)
    result["visible_row_match"] = visible_match.get("match") or {}
    visible_row = visible_match.get("row") if isinstance(visible_match.get("row"), dict) else {}
    if not visible_row:
        result["blocker"] = (
            "Exact Seller Portal cursor match exists, but actionable DOM row was not found after targeted "
            "WB-article search."
        )
        return result
    dom_id = str(visible_row.get("dom_scout_id") or "")
    clicked_menu = _click_safe_row_menu(page, dom_id)
    result["row_menu_click"] = clicked_menu
    if not clicked_menu.get("ok"):
        result["blocker"] = str(clicked_menu.get("reason") or "safe row menu not found")
        return result
    _wait_settle(page, 800)
    menu_state = extract_open_row_menu_state(page)
    result["menu_labels"] = menu_state.get("items") or []
    if not menu_state.get("complaint_action_found"):
        result["blocker"] = "Пожаловаться на отзыв action not found in row menu"
        _safe_escape(page)
        return result

    assert_safe_click_label(ROW_MENU_COMPLAINT_LABEL, purpose="open_complaint_modal")
    action_click = click_open_row_menu_complaint_action(page)
    result["complaint_action_click"] = action_click
    if not action_click.get("ok"):
        result["blocker"] = str(action_click.get("reason") or "Пожаловаться на отзыв action could not be clicked")
        _safe_escape(page)
        return result
    _wait_settle(page, 1500)
    modal_state = extract_complaint_modal_state(page)
    result["modal_opened"] = bool(modal_state.get("opened"))
    result["categories_found"] = modal_state.get("categories") or []
    result["submit_button_label"] = str(modal_state.get("submit_button_label") or "")
    result["description_field_found"] = bool(modal_state.get("description_field_found"))
    result["submit_clicked"] = False
    if detect_complaint_success_state(page).get("seen") and not result["modal_opened"]:
        result["blocker"] = "complaint action appears to create durable submitted state without modal"
        return result
    if not result["modal_opened"]:
        result["blocker"] = "complaint modal did not open"
        return result

    category = choose_complaint_category(
        result["categories_found"],
        force_other=config.force_category_other,
        preferred_category=(candidate.get("ai") or {}).get("category_label"),
    )
    result["selected_category"] = category
    if not category:
        result["blocker"] = "Другое category unavailable and no obvious safe fallback category was selected"
        result["close_method"] = close_modal_without_submit(page)
        result["modal_closed"] = not is_modal_visible(page)
        return result

    category_click = click_complaint_category(page, category)
    result["category_click"] = category_click
    if not category_click.get("ok"):
        result["blocker"] = str(category_click.get("reason") or f"category {category!r} could not be selected")
        result["close_method"] = close_modal_without_submit(page)
        result["modal_closed"] = not is_modal_visible(page)
        return result
    _wait_settle(page, 700)
    result["description_field_ready_after_category"] = wait_for_description_field_ready(page, timeout_ms=min(config.timeout_ms, 6000))
    after_category_modal = extract_complaint_modal_state(page)
    result["description_field_found"] = bool(after_category_modal.get("description_field_found") or result["description_field_found"])
    result["submit_button_label"] = str(after_category_modal.get("submit_button_label") or result["submit_button_label"])

    draft_text = build_draft_text((candidate.get("ai") or {}))
    result["draft_text"] = draft_text
    fill_result = fill_description_field(page, draft_text)
    result["description_fill"] = fill_result
    result["description_field_found"] = bool(fill_result.get("ok") or result["description_field_found"])
    result["modal_description_value_after_fill"] = str(fill_result.get("value_after_fill") or "")
    result["modal_description_value_after_blur"] = str(fill_result.get("value_after_blur") or "")
    result["modal_description_value_before_submit"] = str(fill_result.get("value_after_blur") or fill_result.get("value_after_fill") or "")
    result["description_value_match"] = bool(fill_result.get("value_match"))
    if not fill_result.get("ok"):
        result["blocker"] = str(fill_result.get("reason") or "description field unavailable")
        result["close_method"] = close_modal_without_submit(page)
        result["modal_closed"] = not is_modal_visible(page)
        return result

    after_fill_modal = extract_complaint_modal_state(page)
    result["submit_button_label"] = str(after_fill_modal.get("submit_button_label") or result["submit_button_label"])
    result["submit_clicked"] = False
    result["durable_submitted_state_seen"] = bool(detect_complaint_success_state(page).get("seen"))
    close = close_draft_modal_without_submit(page)
    result.update(close)
    _wait_settle(page, 800)
    result["durable_submitted_state_after_close"] = bool(detect_complaint_success_state(page).get("seen"))
    result["complaint_status_after_close"] = read_network_complaint_status_after_close(page, config, feedback_id, request_headers)
    result["durable_submitted_state_after_close"] = bool(
        result["durable_submitted_state_after_close"]
        or (result["complaint_status_after_close"] or {}).get("submitted_like")
    )
    result["draft_prepared"] = bool(
        result["modal_opened"]
        and result["selected_category"]
        and result["description_field_found"]
        and result["description_value_match"]
        and result["modal_closed"]
        and not result["submit_clicked"]
        and not result["durable_submitted_state_after_close"]
    )
    if not result["draft_prepared"] and not result["blocker"]:
        result["blocker"] = "modal draft was not confirmed closed cleanly"
    return result


def find_visible_actionable_row(
    api_row: Mapping[str, Any],
    visible_rows: list[dict[str, Any]],
    *,
    expected_ui: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    best: dict[str, Any] = {}
    cursor_confirmed_candidates: list[dict[str, Any]] = []
    expected_ui = expected_ui or {}
    for row in visible_rows:
        match = match_one_api_row(dict(api_row), [row])
        if match.get("match_status") == "exact":
            return {"row": row, "match": match}
        strong_visible = score_visible_row_against_exact_cursor(row, expected_ui)
        if strong_visible.get("exact_visible_row"):
            return {
                "row": row,
                "match": {
                    **match,
                    "match_status": "exact",
                    "match_score": max(float(match.get("match_score") or 0.0), 0.91),
                    "matched_fields": strong_visible.get("matched_fields") or [],
                    "reason": "visible DOM row matched exact Seller Portal cursor row by "
                    + ", ".join(str(item) for item in strong_visible.get("matched_fields") or []),
                    "safe_for_future_submit": True,
                },
            }
        match_fields = set(str(item) for item in match.get("matched_fields") or [])
        has_text_or_product_support = bool(
            {"text_exact", "text_high_similarity", "text_contained", "product_title"} & match_fields
        )
        if (
            strong_visible.get("cursor_confirmed_visible_row")
            and has_text_or_product_support
            and bool(row.get("menu_action_available") or row.get("complaint_action_found") or row.get("three_dot_menu_found"))
            and float(match.get("match_score") or 0.0) >= 0.75
        ):
            cursor_confirmed_candidates.append(
                {
                    "row": row,
                    "match": {
                        **match,
                        "match_status": "exact",
                        "match_score": max(float(match.get("match_score") or 0.0), 0.9),
                        "matched_fields": sorted(set(match.get("matched_fields") or []) | set(strong_visible.get("matched_fields") or [])),
                        "missing_fields": sorted(set(match.get("missing_fields") or []) | set(strong_visible.get("missing_fields") or [])),
                        "reason": "visible DOM row uniquely matched exact Seller Portal cursor row by "
                        + ", ".join(str(item) for item in strong_visible.get("matched_fields") or []),
                        "safe_for_future_submit": True,
                    },
                }
            )
        if not best or float(match.get("match_score") or 0.0) > float((best.get("match") or {}).get("match_score") or 0.0):
            best = {"row": row, "match": match}
    if len(cursor_confirmed_candidates) == 1:
        return cursor_confirmed_candidates[0]
    if len(cursor_confirmed_candidates) > 1:
        ambiguous = cursor_confirmed_candidates[0]["match"]
        ambiguous["match_status"] = "ambiguous"
        ambiguous["safe_for_future_submit"] = False
        ambiguous["reason"] = "multiple visible DOM rows matched exact Seller Portal cursor row fields"
        ambiguous["ambiguity_count"] = len(cursor_confirmed_candidates)
        return {"row": {}, "match": ambiguous}
    return {"row": {}, "match": best.get("match") or {}}


def score_visible_row_against_exact_cursor(row: Mapping[str, Any], expected_ui: Mapping[str, Any]) -> dict[str, Any]:
    matched: list[str] = []
    missing: list[str] = []
    mismatched: list[str] = []
    expected_dt = normalize_datetime_minute(expected_ui.get("review_datetime") or expected_ui.get("created_at"))
    row_dt = normalize_datetime_minute(row.get("review_datetime") or row.get("created_at"))
    if expected_dt and row_dt and expected_dt == row_dt:
        matched.append("exact_datetime")
    elif expected_dt:
        missing.append("exact_datetime")
    expected_rating = normalize_rating(expected_ui.get("rating"))
    row_rating = normalize_rating(row.get("rating"))
    if expected_rating and row_rating and expected_rating == row_rating:
        matched.append("rating")
    elif expected_rating and row_rating and expected_rating != row_rating:
        mismatched.append("rating")
    elif expected_rating:
        missing.append("rating")
    expected_nm = normalize_nm_id(expected_ui.get("nm_id") or expected_ui.get("wb_article"))
    row_nm = normalize_nm_id(row.get("nm_id") or row.get("wb_article"))
    if expected_nm and row_nm and expected_nm == row_nm:
        matched.append("nm_id")
    elif expected_nm:
        missing.append("nm_id")
    expected_article = normalize_article(expected_ui.get("supplier_article"))
    row_article = normalize_article(row.get("supplier_article") or row.get("vendor_article"))
    if expected_article and row_article and expected_article == row_article:
        matched.append("supplier_article")
    elif expected_article:
        missing.append("supplier_article")
    exact = "exact_datetime" in matched and "rating" in matched and bool({"nm_id", "supplier_article"} & set(matched))
    cursor_confirmed = (
        "exact_datetime" in matched
        and bool({"nm_id", "supplier_article"} & set(matched))
        and "rating" not in mismatched
    )
    return {
        "exact_visible_row": exact,
        "cursor_confirmed_visible_row": cursor_confirmed,
        "matched_fields": matched,
        "missing_fields": missing,
        "mismatched_fields": mismatched,
    }


def apply_article_search_for_candidate(
    page: Page,
    api_row: Mapping[str, Any],
    *,
    expected_ui: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    expected_ui = expected_ui or {}
    search_value = (
        normalize_nm_id(expected_ui.get("nm_id") or expected_ui.get("wb_article"))
        or normalize_nm_id(api_row.get("nm_id") or api_row.get("wb_article"))
    )
    if not search_value:
        return {"ok": False, "reason": "no WB article/nmId available for targeted UI search"}
    try:
        locator = page.locator('input[type="search"][placeholder*="Артикулам WB"]').first
        locator.click(timeout=3000)
        locator.fill(search_value, timeout=3000)
        page.keyboard.press("Enter")
        return {"ok": True, "search_value": search_value, "field": "Поиск по Артикулам WB"}
    except PlaywrightError as exc:
        return {"ok": False, "search_value": search_value, "reason": safe_text(str(exc), 400)}


def choose_complaint_category(categories: Iterable[str], *, force_other: bool, preferred_category: Any = "") -> str:
    normalized = {normalize_text(category): str(category) for category in categories if str(category or "").strip()}
    if force_other and normalize_text("Другое") in normalized:
        return normalized[normalize_text("Другое")]
    preferred = normalize_text(preferred_category)
    if preferred and preferred in normalized:
        return normalized[preferred]
    if normalize_text("Другое") in normalized:
        return normalized[normalize_text("Другое")]
    return ""


def click_complaint_category(page: Page, category: str) -> dict[str, Any]:
    try:
        return page.evaluate(
            r"""
(category) => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const labelFor = (el) => (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').replace(/\s+/g, ' ').trim();
  const dialogs = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], [class*="modal"], [class*="Modal"], [class*="popup"], [class*="Popup"]')).filter(visible);
  const root = dialogs[dialogs.length - 1] || document.body;
  const candidates = Array.from(root.querySelectorAll('label, [role="radio"], [role="option"], li, button, div, span'))
    .filter(visible)
    .filter((el) => labelFor(el) === category)
    .sort((a, b) => {
      const aPreferred = /label|button/i.test(a.tagName) || a.getAttribute('role') ? 0 : 1;
      const bPreferred = /label|button/i.test(b.tagName) || b.getAttribute('role') ? 0 : 1;
      return aPreferred - bPreferred;
    });
  const target = candidates[0];
  if (!target) return {ok: false, reason: 'category not found'};
  const rect = target.getBoundingClientRect();
  target.click();
  return {ok: true, label: labelFor(target), tag: target.tagName.toLowerCase(), rect: {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)}};
}
            """,
            category,
        )
    except PlaywrightError as exc:
        return {"ok": False, "reason": safe_text(str(exc), 300)}


def fill_description_field(page: Page, draft_text: str) -> dict[str, Any]:
    intended = str(draft_text or "")
    if not intended.strip():
        return {"ok": False, "reason": "description text is empty", "value_match": False}
    ready = wait_for_description_field_ready(page, timeout_ms=5000)
    if not ready.get("ok"):
        return {**ready, "ok": False, "reason": str(ready.get("reason") or "description field not found"), "value_match": False}

    result: dict[str, Any] = {
        "ok": False,
        "field_found": True,
        "field_ready": ready,
        "field_locator_strategy": str(ready.get("field_locator_strategy") or ""),
        "tag": str(ready.get("tag") or ""),
        "label": str(ready.get("label") or ""),
        "placeholder": str(ready.get("placeholder") or ""),
        "editable": bool(ready.get("editable")),
        "visible": bool(ready.get("visible")),
        "enabled": bool(ready.get("enabled")),
        "intended_length": len(intended),
        "value_after_fill": "",
        "value_after_blur": "",
        "value_match": False,
        "attempts": [],
    }
    marker_selector = f'[{DESCRIPTION_FIELD_MARKER_ATTR}="1"]'
    try:
        field = page.locator(marker_selector).first
        field.click(timeout=2500)
        field.fill("", timeout=2500)
        field.fill(intended, timeout=4500)
        result["attempts"].append({"method": "playwright_locator_fill", "ok": True})
    except PlaywrightError as exc:
        result["attempts"].append({"method": "playwright_locator_fill", "ok": False, "reason": safe_text(str(exc), 300)})
        fallback = _native_set_description_field_value(page, intended)
        result["attempts"].append({"method": "native_value_setter", **fallback})

    after_fill = inspect_description_field(page)
    result["value_after_fill"] = str(after_fill.get("value") or "")
    try:
        page.locator(marker_selector).first.evaluate("(el) => { el.blur(); el.dispatchEvent(new Event('change', {bubbles: true})); }", timeout=1500)
        page.wait_for_timeout(150)
        result["attempts"].append({"method": "blur_change", "ok": True})
    except PlaywrightError as exc:
        result["attempts"].append({"method": "blur_change", "ok": False, "reason": safe_text(str(exc), 220)})

    after_blur = inspect_description_field(page)
    result["value_after_blur"] = str(after_blur.get("value") or "")
    result["value_match"] = description_values_match(result["value_after_blur"], intended)

    if not result["value_match"]:
        fallback = _native_set_description_field_value(page, intended)
        result["attempts"].append({"method": "native_value_setter_retry", **fallback})
        retry_after_fill = inspect_description_field(page)
        result["value_after_fill"] = str(retry_after_fill.get("value") or "")
        try:
            page.locator(marker_selector).first.evaluate("(el) => { el.blur(); el.dispatchEvent(new Event('change', {bubbles: true})); }", timeout=1500)
            page.wait_for_timeout(150)
        except PlaywrightError:
            pass
        retry_after_blur = inspect_description_field(page)
        result["value_after_blur"] = str(retry_after_blur.get("value") or "")
        result["value_match"] = description_values_match(result["value_after_blur"], intended)

    result["value_length_after_fill"] = len(result["value_after_fill"])
    result["value_length_after_blur"] = len(result["value_after_blur"])
    result["ok"] = bool(result["field_found"] and result["editable"] and result["value_match"])
    if not result["ok"]:
        result["reason"] = "description field value mismatch after blur" if result["field_found"] else "description field not found"
    return result


def wait_for_description_field_ready(page: Page, *, timeout_ms: int = 5000) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.2, timeout_ms / 1000)
    last_state: dict[str, Any] = {"ok": False, "reason": "description field not found"}
    while time.monotonic() < deadline:
        last_state = inspect_description_field(page)
        if last_state.get("ok"):
            return last_state
        time.sleep(0.2)
    return last_state


def inspect_description_field(page: Page) -> dict[str, Any]:
    try:
        return page.evaluate(
            r"""
() => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const text = (value, limit = 220) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, limit);
  const labelFor = (el) => {
    const parts = [
      el.getAttribute('aria-label'),
      el.getAttribute('placeholder'),
      el.getAttribute('title'),
      el.getAttribute('name'),
      el.getAttribute('data-testid'),
    ];
    const id = el.getAttribute('id');
    if (id) {
      const explicit = document.querySelector(`label[for="${CSS.escape(id)}"]`);
      if (explicit) parts.push(explicit.innerText || explicit.textContent || '');
    }
    const label = el.closest('label');
    if (label) parts.push(label.innerText || label.textContent || '');
    let parent = el.parentElement;
    for (let depth = 0; parent && depth < 4; depth += 1, parent = parent.parentElement) {
      const parentText = text(parent.innerText || parent.textContent || '', 180);
      if (/опишите ситуацию|опис|коммент|почему|подроб|причин/i.test(parentText)) parts.push(parentText);
    }
    return text(parts.filter(Boolean).join(' | '), 360);
  };
  const dialogs = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], [class*="modal"], [class*="Modal"], [class*="popup"], [class*="Popup"]')).filter(visible);
  const root = dialogs[dialogs.length - 1] || document.body;
  const controls = Array.from(root.querySelectorAll('textarea, input, [contenteditable="true"]')).filter(visible)
    .filter((el) => {
      const type = String(el.getAttribute('type') || '').toLowerCase();
      return !['radio', 'checkbox', 'button', 'submit', 'hidden'].includes(type);
    })
    .map((el, index) => {
      const label = labelFor(el);
      const tag = el.tagName.toLowerCase();
      const type = String(el.getAttribute('type') || '').toLowerCase();
      const disabled = Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true' || el.getAttribute('disabled') !== null);
      const readonly = Boolean(el.readOnly || el.getAttribute('readonly') !== null);
      let score = 0;
      if (/опишите ситуацию/i.test(label)) score += 120;
      if (/опис|коммент|почему|подроб|причин/i.test(label)) score += 80;
      if (tag === 'textarea') score += 60;
      if (el.isContentEditable) score += 45;
      if (type === 'text' || !type) score += 25;
      if (disabled || readonly) score -= 300;
      return {el, index, label, tag, type, disabled, readonly, score};
    })
    .sort((a, b) => b.score - a.score || a.index - b.index);
  const target = controls[0];
  root.querySelectorAll('[' + '%MARKER%' + ']').forEach((el) => el.removeAttribute('%MARKER%'));
  if (!target) return {ok: false, reason: 'description field not found', controls_considered: 0};
  const el = target.el;
  el.setAttribute('%MARKER%', '1');
  const rect = el.getBoundingClientRect();
  const value = el.isContentEditable ? (el.innerText || el.textContent || '') : (el.value || '');
  const editable = Boolean(!target.disabled && !target.readonly);
  return {
    ok: editable,
    reason: editable ? '' : 'description field is disabled or readonly',
    field_locator_strategy: /опишите ситуацию/i.test(target.label) ? 'label_or_placeholder_opishite_situaciyu' : (target.tag === 'textarea' ? 'visible_textarea_fallback' : 'visible_textbox_fallback'),
    tag: target.tag,
    input_type: target.type,
    label: target.label,
    placeholder: text(el.getAttribute('placeholder') || '', 180),
    visible: true,
    enabled: !target.disabled,
    editable,
    value,
    value_length: value.length,
    controls_considered: controls.length,
    rect: {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)}
  };
}
            """.replace("%MARKER%", DESCRIPTION_FIELD_MARKER_ATTR),
        )
    except PlaywrightError as exc:
        return {"ok": False, "reason": safe_text(str(exc), 300)}


def _native_set_description_field_value(page: Page, draft_text: str) -> dict[str, Any]:
    try:
        return page.evaluate(
            r"""
({marker, draftText}) => {
  const el = document.querySelector('[' + marker + '="1"]');
  if (!el) return {ok: false, reason: 'marked description field not found'};
  const setNativeValue = (node, value) => {
    if (node.isContentEditable) {
      node.focus();
      node.textContent = '';
      node.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'deleteContentBackward', data: null}));
      node.textContent = value;
      return;
    }
    const proto = node.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
    if (descriptor && descriptor.set) {
      descriptor.set.call(node, '');
      node.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'deleteContentBackward', data: null}));
      descriptor.set.call(node, value);
    } else {
      node.value = value;
    }
  };
  el.focus();
  setNativeValue(el, String(draftText || ''));
  el.dispatchEvent(new InputEvent('beforeinput', {bubbles: true, inputType: 'insertText', data: String(draftText || '')}));
  el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: String(draftText || '')}));
  el.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true, key: 'a'}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  el.blur();
  const value = el.isContentEditable ? (el.innerText || el.textContent || '') : (el.value || '');
  return {ok: value === String(draftText || ''), value_length: value.length};
}
            """,
            {"marker": DESCRIPTION_FIELD_MARKER_ATTR, "draftText": draft_text},
        )
    except PlaywrightError as exc:
        return {"ok": False, "reason": safe_text(str(exc), 300)}


def description_values_match(actual: Any, intended: Any) -> bool:
    actual_text = str(actual or "").strip()
    intended_text = str(intended or "").strip()
    if not actual_text or not intended_text:
        return False
    if actual_text == intended_text:
        return True
    return normalize_text(actual_text) == normalize_text(intended_text)


def description_is_ready_for_submit(fill_result: Mapping[str, Any], intended_text: str) -> bool:
    return bool(
        fill_result.get("ok")
        and fill_result.get("value_match")
        and description_values_match(fill_result.get("value_after_blur") or fill_result.get("value_after_fill"), intended_text)
    )


def description_persistence_result(intended_text: Any, wb_description_text: Any, *, observed: bool = True) -> dict[str, Any]:
    intended = str(intended_text or "").strip()
    observed_text = str(wb_description_text or "").strip()
    if not observed:
        persisted: bool | str = "unknown"
    elif not intended:
        persisted = "unknown"
    else:
        persisted = description_values_match(observed_text, intended) or normalize_text(intended) in normalize_text(observed_text)
    return {
        "description_persisted": persisted,
        "post_submit_wb_description_text": safe_text(observed_text, 500),
        "intended_description_length": len(intended),
        "wb_description_length": len(observed_text),
    }


def close_draft_modal_without_submit(page: Page) -> dict[str, Any]:
    methods: list[str] = []
    close_method = close_modal_without_submit(page)
    methods.append(close_method)
    for _ in range(4):
        _wait_settle(page, 400)
        if not is_modal_visible(page):
            return {"close_method": ",".join(methods), "modal_closed": True}
        click = click_safe_discard_or_close(page)
        methods.append(str(click.get("label") or click.get("reason") or "safe_discard"))
        if not click.get("ok"):
            _safe_escape(page)
            methods.append("escape")
    return {"close_method": ",".join(methods), "modal_closed": not is_modal_visible(page)}


def click_safe_discard_or_close(page: Page) -> dict[str, Any]:
    try:
        return page.evaluate(
            r"""
() => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const labelFor = (el) => (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').replace(/\s+/g, ' ').trim();
  const buttons = Array.from(document.querySelectorAll('button, [role="button"]')).filter(visible).map((el) => ({el, label: labelFor(el)}));
  const safe = buttons.filter((item) => {
    const lower = item.label.toLowerCase();
    if (!lower) return false;
    if (/отправ|подать|submit|send|сохран|save|пожаловаться/.test(lower)) return false;
    return /не сохранять|закрыть|отмена|отменить|выйти|discard|close|cancel/.test(lower);
  });
  const target = safe[0];
  if (!target) return {ok: false, reason: 'safe discard/close button not found'};
  const rect = target.el.getBoundingClientRect();
  target.el.click();
  return {ok: true, label: target.label, rect: {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)}};
}
            """
        )
    except PlaywrightError as exc:
        return {"ok": False, "reason": safe_text(str(exc), 300)}


def read_network_complaint_status_after_close(
    page: Page,
    config: DryRunConfig,
    feedback_id: str,
    request_headers: Mapping[str, str],
) -> dict[str, Any]:
    if not request_headers:
        return {"checked": False, "reason": "Seller Portal request headers were not captured"}
    replay_config = build_replay_config(config, max_ui_rows=max(config.max_api_rows * 6, 60))
    rows, stats = collect_feedback_rows_from_seller_portal_network(page, replay_config, request_headers=request_headers)
    for row in rows:
        if str(row.get("feedback_id") or "") == feedback_id:
            return {
                "checked": True,
                "feedback_id": feedback_id,
                "complaint_status": str(row.get("complaint_status") or ""),
                "complaint_action_found": bool(row.get("complaint_action_found")),
                "submitted_like": str(row.get("complaint_status") or "").lower() not in {"", "unknown", "none"},
            }
    return {"checked": True, "feedback_id": feedback_id, "reason": "feedback row not found after close", "stats": stats}


def is_modal_visible(page: Page) -> bool:
    try:
        return bool(
            page.evaluate(
                r"""
() => Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], [class*="modal"], [class*="Modal"], [class*="popup"], [class*="Popup"]')).some((el) => {
  const style = window.getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
})
                """
            )
        )
    except PlaywrightError:
        return False


def apply_modal_results(candidates: list[dict[str, Any]], modal_results: list[Mapping[str, Any]]) -> None:
    by_id = {str(item.get("feedback_id") or ""): dict(item) for item in modal_results}
    for candidate in candidates:
        feedback_id = str(candidate.get("feedback_id") or "")
        if feedback_id in by_id:
            candidate["modal"] = by_id[feedback_id]
            if by_id[feedback_id].get("blocker"):
                candidate["skip_reason"] = str(by_id[feedback_id].get("blocker") or "")


def build_draft_text(ai_result: Mapping[str, Any]) -> str:
    reason = TEXT_WS_RE.sub(" ", str(ai_result.get("reason") or "").strip())
    evidence = normalize_sentence(ai_result.get("evidence") or "")
    if reason:
        draft = reason
    elif evidence:
        draft = f"Просим проверить отзыв: фрагмент отзыва требует проверки. Фрагмент: {evidence}."
    else:
        draft = "Просим проверить отзыв: требуется ручная проверка корректности отзыва."
    return safe_text(draft, DRAFT_LIMIT)


def normalize_sentence(value: Any) -> str:
    text = TEXT_WS_RE.sub(" ", str(value or "").strip())
    text = text.strip(" .;:!?\n\t")
    return safe_text(text, 220)


def summarize_ai_result(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "feedback_id": str(result.get("feedback_id") or ""),
        "complaint_fit": str(result.get("complaint_fit") or ""),
        "complaint_fit_label": str(result.get("complaint_fit_label") or ""),
        "category": str(result.get("category") or ""),
        "category_label": str(result.get("category_label") or ""),
        "reason": safe_text(str(result.get("reason") or ""), 360),
        "confidence": str(result.get("confidence") or ""),
        "confidence_label": str(result.get("confidence_label") or ""),
        "evidence": safe_text(str(result.get("evidence") or ""), 240),
    }


def selection_reason(fit: str, selected: bool) -> str:
    if selected:
        return f"selected complaint_fit={fit}"
    if fit == "no":
        return "skipped complaint_fit=no"
    if fit in {"yes", "review"}:
        return "skipped because max_ai_candidates slots were already filled"
    return "skipped because AI result is missing or invalid"


def empty_modal_candidate_state() -> dict[str, Any]:
    return {
        "feedback_id": "",
        "visible_rows_checked": 0,
        "visible_rows_checked_after_search": 0,
        "visible_row_match": {},
        "targeted_search": {},
        "row_menu_click": {},
        "menu_labels": [],
        "complaint_action_click": {},
        "modal_opened": False,
        "categories_found": [],
        "selected_category": "",
        "category_click": {},
        "description_field_found": False,
        "description_field_ready_after_category": {},
        "description_fill": {},
        "modal_description_value_after_fill": "",
        "modal_description_value_after_blur": "",
        "modal_description_value_before_submit": "",
        "description_value_match": False,
        "draft_text": "",
        "draft_prepared": False,
        "submit_button_label": "",
        "submit_clicked": False,
        "modal_closed": False,
        "close_method": "",
        "durable_submitted_state_seen": False,
        "durable_submitted_state_after_close": False,
        "complaint_status_after_close": {},
        "blocker": "",
    }


def build_aggregate(candidates: list[Mapping[str, Any]]) -> dict[str, Any]:
    selected = [item for item in candidates if item.get("selected_for_dry_run")]
    exact = [item for item in selected if (item.get("match") or {}).get("match_status") == "exact"]
    modal_opened = [item for item in candidates if (item.get("modal") or {}).get("modal_opened")]
    draft_prepared = [item for item in candidates if (item.get("modal") or {}).get("draft_prepared")]
    submit_clicked_count = sum(1 for item in candidates if (item.get("modal") or {}).get("submit_clicked"))
    skipped_reasons = Counter(str(item.get("skip_reason") or "") for item in candidates if item.get("skip_reason"))
    ai_counts = Counter(str((item.get("ai") or {}).get("complaint_fit") or "unknown") for item in candidates)
    return {
        "api_rows_loaded": len(candidates),
        "ai_analyzed_count": sum(1 for item in candidates if (item.get("ai") or {}).get("complaint_fit")),
        "ai_yes_count": ai_counts.get("yes", 0),
        "ai_review_count": ai_counts.get("review", 0),
        "ai_no_count": ai_counts.get("no", 0),
        "candidates_selected": len(selected),
        "exact_matched": len(exact),
        "modal_opened_count": len(modal_opened),
        "modal_draft_prepared_count": len(draft_prepared),
        "submit_clicked_count": submit_clicked_count,
        "durable_submitted_state_seen_count": sum(
            1 for item in candidates if (item.get("modal") or {}).get("durable_submitted_state_after_close")
        ),
        "skipped_reasons": [
            {"reason": reason, "count": count}
            for reason, count in skipped_reasons.most_common(10)
            if reason
        ],
    }


def empty_aggregate() -> dict[str, Any]:
    return {
        "api_rows_loaded": 0,
        "ai_analyzed_count": 0,
        "ai_yes_count": 0,
        "ai_review_count": 0,
        "ai_no_count": 0,
        "candidates_selected": 0,
        "exact_matched": 0,
        "modal_opened_count": 0,
        "modal_draft_prepared_count": 0,
        "submit_clicked_count": 0,
        "durable_submitted_state_seen_count": 0,
        "skipped_reasons": [],
    }


def no_submit_guards() -> dict[str, Any]:
    return {
        "mode": NO_SUBMIT_MODE,
        "seller_portal_write_actions_allowed": SELLER_PORTAL_WRITE_ACTIONS_ALLOWED,
        "complaint_submit_clicked": False,
        "complaint_submit_path_called": False,
        "complaint_modal_open_allowed": True,
        "complaint_category_select_allowed": True,
        "complaint_description_fill_allowed": True,
        "complaint_final_submit_allowed": False,
        "answer_edit_clicked": False,
        "status_persistence_allowed": False,
        "persistent_account_changes_allowed": False,
        "submit_clicked_count": 0,
    }


def build_replay_config(config: DryRunConfig, *, max_ui_rows: int) -> ReplayConfig:
    return ReplayConfig(
        date_from=config.date_from,
        date_to=config.date_to,
        stars=config.stars,
        is_answered=config.is_answered,
        max_api_rows=config.max_api_rows,
        max_ui_rows=max_ui_rows,
        mode=config.mode,
        storage_state_path=config.storage_state_path,
        wb_bot_python=config.wb_bot_python,
        output_dir=config.output_dir,
        start_url=config.start_url,
        headless=config.headless,
        timeout_ms=config.timeout_ms,
        write_artifacts=False,
        apply_ui_filters="auto",
        targeted_search="auto",
        max_targeted_searches=0,
    )


def build_scout_config(config: DryRunConfig) -> ScoutConfig:
    return ScoutConfig(
        mode="scout-feedbacks",
        storage_state_path=config.storage_state_path,
        wb_bot_python=config.wb_bot_python,
        output_root=config.output_dir,
        start_url=config.start_url,
        max_feedback_rows=max(5, config.max_api_rows),
        max_complaint_rows=1,
        max_modal_reviews=0,
        open_complaint_modal=False,
        headless=config.headless,
        timeout_ms=config.timeout_ms,
        write_artifacts=False,
    )


def normalize_text(value: Any) -> str:
    return TEXT_WS_RE.sub(" ", str(value or "").strip()).lower().replace("ё", "е")


def normalize_requested_date(value: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text).date()
    except ValueError as exc:
        raise ValueError("date-from/date-to must use YYYY-MM-DD") from exc
    return parsed.isoformat()


def chunks(rows: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(rows), max(1, size)):
        yield rows[start : start + max(1, size)]


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def render_markdown_report(report: Mapping[str, Any]) -> str:
    params = report.get("parameters") or {}
    agg = report.get("aggregate") or {}
    session = report.get("session") or {}
    lines = [
        "# Seller Portal Complaint Dry-Run Plan",
        "",
        f"- Mode: `{report.get('mode')}`",
        f"- Started: `{report.get('started_at')}`",
        f"- Finished: `{report.get('finished_at')}`",
        f"- Requested range: `{params.get('date_from')}`..`{params.get('date_to')}`",
        f"- Stars: `{','.join(str(item) for item in params.get('stars') or [])}`",
        f"- is_answered: `{params.get('is_answered')}`",
        f"- Session status: `{session.get('status')}`",
        f"- API rows loaded: `{agg.get('api_rows_loaded', 0)}`",
        f"- AI yes/review/no: `{agg.get('ai_yes_count', 0)}` / `{agg.get('ai_review_count', 0)}` / `{agg.get('ai_no_count', 0)}`",
        f"- Candidates selected: `{agg.get('candidates_selected', 0)}`",
        f"- Exact matched: `{agg.get('exact_matched', 0)}`",
        f"- Modal opened: `{agg.get('modal_opened_count', 0)}`",
        f"- Draft prepared: `{agg.get('modal_draft_prepared_count', 0)}`",
        f"- Submit clicked count: `{agg.get('submit_clicked_count', 0)}`",
        f"- Durable submitted state seen: `{agg.get('durable_submitted_state_seen_count', 0)}`",
        "",
        "## Candidates",
        "",
    ]
    for candidate in report.get("candidates") or []:
        api = candidate.get("api_summary") or {}
        ai = candidate.get("ai") or {}
        match = candidate.get("match") or {}
        modal = candidate.get("modal") or {}
        lines.extend(
            [
                f"- `{candidate.get('feedback_id')}` selected `{candidate.get('selected_for_dry_run')}` fit `{ai.get('complaint_fit')}` match `{match.get('match_status')}` modal `{modal.get('draft_prepared')}`",
                f"  API: `{api.get('created_at')}` rating `{api.get('rating')}` nm `{api.get('nm_id')}` article `{api.get('supplier_article')}` text `{api.get('review_text')}`",
                f"  AI: `{ai.get('category_label')}` / `{ai.get('confidence_label')}` reason `{ai.get('reason')}` evidence `{ai.get('evidence')}`",
                f"  Draft: category `{modal.get('selected_category')}` submit `{modal.get('submit_button_label')}` clicked `{modal.get('submit_clicked')}` text `{modal.get('draft_text')}`",
                f"  Description: match `{modal.get('description_value_match')}` after-fill length `{len(str(modal.get('modal_description_value_after_fill') or ''))}` after-blur length `{len(str(modal.get('modal_description_value_after_blur') or ''))}`",
                f"  Skip/blocker: `{candidate.get('skip_reason') or modal.get('blocker') or ''}`",
            ]
        )
    if report.get("errors"):
        lines.extend(["", "## Errors", ""])
        for error in report["errors"]:
            lines.append(f"- `{error.get('stage')}` / `{error.get('code')}`: {error.get('message')}")
    return "\n".join(lines) + "\n"


def write_report_artifacts(report: dict[str, Any], output_root: Path) -> dict[str, Path]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    json_path = run_dir / "seller_portal_feedbacks_complaint_dry_run_plan.json"
    md_path = run_dir / "seller_portal_feedbacks_complaint_dry_run_plan.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown_report(report), encoding="utf-8")
    return {"run_dir": run_dir, "json": json_path, "markdown": md_path}


def compact_stdout_report(report: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "contract_name": report.get("contract_name"),
        "mode": report.get("mode"),
        "parameters": report.get("parameters"),
        "read_only_guards": report.get("read_only_guards"),
        "api": {key: (report.get("api") or {}).get(key) for key in ("success", "row_count", "total_available_rows", "limited", "feedback_id_available", "blocker")},
        "ai": {key: (report.get("ai") or {}).get(key) for key in ("success", "row_count", "counts", "prompt_status", "model", "model_source", "blocker")},
        "session": report.get("session"),
        "navigation": report.get("navigation"),
        "ui": {
            key: (report.get("ui") or {}).get(key)
            for key in (
                "rows_collected",
                "dom_rows_collected",
                "seller_portal_network_rows_collected",
                "collection_strategy",
                "hidden_feedback_id_available",
                "seller_portal_network_feedback_id_available",
                "modal_draft_ui",
            )
        },
        "aggregate": report.get("aggregate"),
        "errors": report.get("errors"),
        "artifact_paths": report.get("artifact_paths"),
    }


if __name__ == "__main__":
    main()
