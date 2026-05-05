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
    normalize_date_key,
    normalize_datetime_minute,
    normalize_nm_id,
    normalize_rating,
    parse_stars,
    safe_text,
    scroll_feedback_list,
    summarize_api_row,
    summarize_ui_row,
)
from apps.seller_portal_relogin_session import (  # noqa: E402
    DEFAULT_STORAGE_STATE_PATH,
    DEFAULT_WB_BOT_PYTHON,
)
from packages.application.feedback_review_tags import normalize_review_tags, reason_contradicts_review_tags  # noqa: E402
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
DEFAULT_DENY_FEEDBACK_IDS = ("GPe9vrq0kctlSfobrgq2", "fdQpHhNXTosEkArTHAZF")
FEEDBACKS_TAB_LABEL = "Отзывы"
FEEDBACKS_UNANSWERED_TAB_LABEL = "Ждут ответа"
FEEDBACKS_ANSWERED_TAB_LABEL = "Есть ответ"
ACTIONABILITY_SCROLL_ATTEMPTS = 12
SELLER_PORTAL_DATE_FILTER_MARKER_ATTR = "data-wb-core-filter-date-input"
SELLER_PORTAL_STAR_FILTER_MARKER_ATTR = "data-wb-core-filter-star-row"


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
    deny_feedback_ids: tuple[str, ...]


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
    parser.add_argument(
        "--deny-feedback-id",
        action="append",
        default=[],
        help="Feedback id to exclude from dry-run candidate selection. Can be repeated or comma-separated.",
    )
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
        deny_feedback_ids=normalize_deny_feedback_ids(args.deny_feedback_id),
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
            "deny_feedback_ids": list(config.deny_feedback_ids),
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
    selected_ids = select_ai_candidate_ids(
        list(analysis_by_id.values()),
        max_candidates=config.max_ai_candidates,
        deny_feedback_ids=config.deny_feedback_ids,
    )
    candidates = build_candidate_records(api_rows, analysis_by_id, selected_ids, deny_feedback_ids=config.deny_feedback_ids)

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


def select_ai_candidate_ids(
    results: list[Mapping[str, Any]],
    *,
    max_candidates: int,
    deny_feedback_ids: Iterable[str] = (),
) -> list[str]:
    selected: list[str] = []
    denied = {str(item or "").strip() for item in deny_feedback_ids if str(item or "").strip()}
    for fit in ("yes", "review"):
        for result in results:
            feedback_id = str(result.get("feedback_id") or "")
            if feedback_id in denied:
                continue
            if result.get("complaint_fit") == fit and feedback_id and feedback_id not in selected:
                selected.append(feedback_id)
                if len(selected) >= max_candidates:
                    return selected
    return selected


def build_candidate_records(
    api_rows: list[dict[str, Any]],
    analysis_by_id: Mapping[str, Mapping[str, Any]],
    selected_ids: list[str],
    deny_feedback_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    selected_set = set(selected_ids)
    denied = {str(item or "").strip() for item in deny_feedback_ids if str(item or "").strip()}
    records: list[dict[str, Any]] = []
    for row in api_rows:
        feedback_id = str(row.get("feedback_id") or "")
        analysis = dict(analysis_by_id.get(feedback_id) or {})
        denied_feedback = bool(feedback_id and feedback_id in denied)
        selected = feedback_id in selected_set and not denied_feedback
        fit = str(analysis.get("complaint_fit") or "unknown")
        skip_reason = "hard-denylisted feedback_id; modal draft blocked" if denied_feedback else selection_reason(fit, selected)
        records.append(
            {
                "feedback_id": feedback_id,
                "api_summary": summarize_api_row(row),
                "ai": summarize_ai_result(analysis),
                "selected_for_dry_run": selected,
                "selection_reason": "hard-denylisted feedback_id" if denied_feedback else selection_reason(fit, selected),
                "match": {},
                "modal": empty_modal_candidate_state(),
                "skip_reason": "" if selected else skip_reason,
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
        review_tags = normalize_review_tags(
            [
                *((candidate.get("api_summary") or {}).get("review_tags") or []),
                *((match.get("best_ui_candidate") or {}).get("review_tags") or []),
            ]
        )
        candidate["tag_diagnostics"] = {
            "api_review_tags": normalize_review_tags((candidate.get("api_summary") or {}).get("review_tags") or []),
            "ui_review_tags": normalize_review_tags(((match.get("best_ui_candidate") or {}).get("review_tags") or [])),
            "combined_review_tags": review_tags,
        }
        if reason_contradicts_review_tags((candidate.get("ai") or {}).get("reason"), review_tags):
            candidate["skip_reason"] = "reason_contradicts_review_tags; modal draft blocked"
            continue
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

    expected_ui = (candidate.get("match") or {}).get("best_ui_candidate") or {}
    resolver = resolve_actionable_feedback_row(page, config, api_row, expected_ui=expected_ui)
    result["actionability_resolver"] = resolver
    result["visible_rows_checked"] = int(resolver.get("visible_rows_checked") or 0)
    result["visible_rows_checked_after_search"] = int(resolver.get("visible_rows_checked_after_search") or 0)
    result["visible_rows_checked_after_scroll"] = int(resolver.get("visible_rows_checked_after_scroll") or 0)
    result["visible_row_match"] = resolver.get("visible_row_match") or {}
    result["targeted_search"] = resolver.get("targeted_search") or {}
    result["filter_controller"] = resolver.get("filter_controller") or {}
    result["date_filter_applied"] = bool(resolver.get("date_filter_applied"))
    result["star_filter_applied"] = bool(resolver.get("star_filter_applied"))
    result["search_used"] = bool(resolver.get("search_used"))
    result["scroll_used"] = bool(resolver.get("scroll_used"))
    result["row_menu_click"] = resolver.get("row_menu_click") or {}
    result["menu_labels"] = resolver.get("menu_labels") or []
    result["tab_used"] = str(resolver.get("tab_used") or "")
    result["locator_strategy"] = str(resolver.get("locator_strategy") or "")
    result["complaint_action_found"] = bool(resolver.get("complaint_action_found"))
    if not resolver.get("actionable_row_found"):
        result["blocker"] = str(resolver.get("block_reason") or "Exact Seller Portal cursor match exists, but actionable DOM row was not found")
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
    if isinstance(result.get("actionability_resolver"), dict):
        result["actionability_resolver"]["modal_opened_in_dry_run"] = bool(result["modal_opened"])
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


def resolve_actionable_feedback_row(
    page: Page,
    config: DryRunConfig,
    api_row: Mapping[str, Any],
    *,
    expected_ui: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    expected_ui = expected_ui or {}
    result = empty_actionability_resolver_state()
    result["feedback_id"] = str(api_row.get("feedback_id") or expected_ui.get("feedback_id") or "")
    result["expected_ui_summary"] = summarize_ui_row(dict(expected_ui)) if expected_ui else {}
    tabs = feedback_tab_candidates(api_row, expected_ui, requested_is_answered=config.is_answered)
    result["tabs_tried"] = tabs
    last_blocker = ""
    for tab_label in tabs:
        attempt = resolve_actionable_feedback_row_in_tab(
            page,
            config,
            api_row,
            expected_ui=expected_ui,
            tab_label=tab_label,
        )
        result["attempts"].append(attempt)
        result["visible_rows_checked"] = int(result["visible_rows_checked"] or 0) + int(attempt.get("visible_rows_checked") or 0)
        result["visible_rows_checked_after_search"] = int(result["visible_rows_checked_after_search"] or 0) + int(
            attempt.get("visible_rows_checked_after_search") or 0
        )
        result["visible_rows_checked_after_scroll"] = int(result["visible_rows_checked_after_scroll"] or 0) + int(
            attempt.get("visible_rows_checked_after_scroll") or 0
        )
        result["tab_used"] = tab_label
        result["locator_strategy"] = str(attempt.get("locator_strategy") or result.get("locator_strategy") or "")
        result["targeted_search"] = attempt.get("targeted_search") or result.get("targeted_search") or {}
        result["filter_controller"] = attempt.get("filter_controller") or result.get("filter_controller") or {}
        result["date_filter_applied"] = bool(attempt.get("date_filter_applied")) or bool(result.get("date_filter_applied"))
        result["star_filter_applied"] = bool(attempt.get("star_filter_applied")) or bool(result.get("star_filter_applied"))
        result["search_used"] = bool(attempt.get("search_used")) or bool(result.get("search_used"))
        result["scroll_used"] = bool(attempt.get("scroll_used")) or bool(result.get("scroll_used"))
        last_blocker = str(attempt.get("block_reason") or last_blocker)
        if attempt.get("actionable_row_found"):
            result.update(
                {
                    "actionable_row_found": True,
                    "feedback_id": result["feedback_id"],
                    "tab_used": tab_label,
                    "locator_strategy": str(attempt.get("locator_strategy") or ""),
                    "row_visible": True,
                    "menu_found": bool(attempt.get("menu_found")),
                    "complaint_action_found": bool(attempt.get("complaint_action_found")),
                    "complaint_action_available": bool(attempt.get("complaint_action_available")),
                    "visible_row_match": attempt.get("visible_row_match") or {},
                    "targeted_search": attempt.get("targeted_search") or {},
                    "filter_controller": attempt.get("filter_controller") or {},
                    "date_filter_applied": bool(attempt.get("date_filter_applied")),
                    "star_filter_applied": bool(attempt.get("star_filter_applied")),
                    "search_used": bool(attempt.get("search_used")),
                    "scroll_used": bool(attempt.get("scroll_used")),
                    "row_menu_click": attempt.get("row_menu_click") or {},
                    "menu_labels": attempt.get("menu_labels") or [],
                    "resolved_row_summary": summarize_ui_row(attempt.get("resolved_row") or {}),
                    "block_reason": "",
                }
            )
            return result
        _safe_escape(page)
        clear_article_search(page)
    result["block_reason"] = last_blocker or "actionable DOM row was not found in tried feedback tabs"
    return result


def resolve_actionable_feedback_row_in_tab(
    page: Page,
    config: DryRunConfig,
    api_row: Mapping[str, Any],
    *,
    expected_ui: Mapping[str, Any],
    tab_label: str,
) -> dict[str, Any]:
    attempt: dict[str, Any] = {
        "tab": tab_label,
        "tab_clicked": False,
        "visible_rows_checked": 0,
        "visible_rows_checked_after_search": 0,
        "visible_rows_checked_after_scroll": 0,
        "scroll_attempts": [],
        "targeted_search": {},
        "filter_controller": {},
        "date_filter_applied": False,
        "star_filter_applied": False,
        "list_update_observed": False,
        "search_used": False,
        "scroll_used": False,
        "visible_row_match": {},
        "resolved_row": {},
        "locator_strategy": "",
        "row_visible": False,
        "menu_found": False,
        "complaint_action_found": False,
        "complaint_action_available": False,
        "actionable_row_found": False,
        "block_reason": "",
    }
    clicked = _click_tab_like(page, tab_label)
    attempt["tab_clicked"] = bool(clicked)
    if not clicked and tab_label != FEEDBACKS_TAB_LABEL:
        attempt["block_reason"] = f"feedback tab {tab_label!r} was not found"
        return attempt
    _wait_settle(page, 1800)
    _wait_for_feedback_rows(page, timeout_ms=7000)

    filters = apply_seller_portal_feedback_filters(page, config, api_row, expected_ui=expected_ui)
    attempt["filter_controller"] = filters
    attempt["date_filter_applied"] = bool(filters.get("date_filter_applied"))
    attempt["star_filter_applied"] = bool(filters.get("star_filter_applied"))
    attempt["list_update_observed"] = bool(filters.get("list_update_observed"))
    _wait_for_feedback_rows(page, timeout_ms=7000)

    visible = find_actionable_visible_row_once(
        page,
        api_row,
        expected_ui=expected_ui,
        locator_strategy="filtered_direct_visible",
    )
    attempt["visible_rows_checked"] = int(visible.get("visible_rows_checked") or 0)
    if visible.get("row"):
        return confirm_resolved_row_menu(page, attempt, visible, locator_strategy=str(visible.get("locator_strategy") or "filtered_direct_visible"))

    search_result = apply_article_search_for_candidate(page, api_row, expected_ui=expected_ui)
    attempt["targeted_search"] = search_result
    attempt["search_used"] = bool(search_result.get("ok"))
    if search_result.get("ok"):
        _wait_settle(page, 2500)
        after_search = find_actionable_visible_row_once(
            page,
            api_row,
            expected_ui=expected_ui,
            locator_strategy="filtered_article_search_visible",
        )
        attempt["visible_rows_checked_after_search"] = int(after_search.get("visible_rows_checked") or 0)
        if after_search.get("row"):
            return confirm_resolved_row_menu(
                page,
                attempt,
                after_search,
                locator_strategy=str(after_search.get("locator_strategy") or "filtered_article_search_visible"),
            )

    scroll = find_actionable_visible_row_with_scroll(
        page,
        config,
        api_row,
        expected_ui=expected_ui,
        locator_strategy="filtered_scroll_fallback",
    )
    attempt["visible_rows_checked_after_scroll"] = int(scroll.get("visible_rows_checked") or 0)
    attempt["scroll_attempts"].extend(scroll.get("scroll_attempts") or [])
    attempt["scroll_used"] = bool(scroll.get("scroll_attempts"))
    if scroll.get("row"):
        return confirm_resolved_row_menu(page, attempt, scroll, locator_strategy=str(scroll.get("locator_strategy") or "filtered_scroll_fallback"))

    attempt["visible_row_match"] = scroll.get("match") or visible.get("match") or {}
    attempt["block_reason"] = actionability_block_reason(expected_ui, attempt)
    return attempt


def find_actionable_visible_row_once(
    page: Page,
    api_row: Mapping[str, Any],
    *,
    expected_ui: Mapping[str, Any],
    locator_strategy: str,
) -> dict[str, Any]:
    visible_rows = extract_visible_feedback_rows(page, max_rows=80)
    visible_match = find_visible_actionable_row(api_row, visible_rows, expected_ui=expected_ui)
    visible_row = visible_match.get("row") if isinstance(visible_match.get("row"), dict) else {}
    return {
        "row": visible_row,
        "match": visible_match.get("match") or {},
        "visible_rows_checked": len(visible_rows),
        "locator_strategy": locator_strategy,
    }


def find_actionable_visible_row_with_scroll(
    page: Page,
    config: DryRunConfig,
    api_row: Mapping[str, Any],
    *,
    expected_ui: Mapping[str, Any],
    locator_strategy: str,
) -> dict[str, Any]:
    checked_total = 0
    last_match: dict[str, Any] = {}
    scroll_attempts: list[dict[str, Any]] = []
    max_visible_rows = min(max(config.max_api_rows * 2, 20), 80)
    for attempt_index in range(1, ACTIONABILITY_SCROLL_ATTEMPTS + 1):
        visible_rows = extract_visible_feedback_rows(page, max_rows=max_visible_rows)
        checked_total += len(visible_rows)
        visible_match = find_visible_actionable_row(api_row, visible_rows, expected_ui=expected_ui)
        last_match = visible_match.get("match") or last_match
        visible_row = visible_match.get("row") if isinstance(visible_match.get("row"), dict) else {}
        if visible_row:
            return {
                "row": visible_row,
                "match": visible_match.get("match") or {},
                "visible_rows_checked": checked_total,
                "locator_strategy": f"{locator_strategy}:attempt_{attempt_index}",
                "scroll_attempts": scroll_attempts,
            }
        scroll_result = scroll_feedback_list(page)
        scroll_attempts.append(scroll_result)
        _wait_settle(page, 800)
        if not scroll_result.get("changed"):
            break
    return {
        "row": {},
        "match": last_match,
        "visible_rows_checked": checked_total,
        "locator_strategy": locator_strategy,
        "scroll_attempts": scroll_attempts,
    }


def confirm_resolved_row_menu(
    page: Page,
    attempt: dict[str, Any],
    visible_result: Mapping[str, Any],
    *,
    locator_strategy: str,
) -> dict[str, Any]:
    row = visible_result.get("row") if isinstance(visible_result.get("row"), dict) else {}
    attempt["resolved_row"] = row
    attempt["visible_row_match"] = visible_result.get("match") or {}
    attempt["locator_strategy"] = locator_strategy
    attempt["row_visible"] = bool(row)
    dom_id = str(row.get("dom_scout_id") or "")
    if not dom_id:
        attempt["block_reason"] = "matched DOM row has no stable row id for menu click"
        return attempt
    clicked_menu = _click_safe_row_menu(page, dom_id)
    attempt["row_menu_click"] = clicked_menu
    attempt["menu_found"] = bool(clicked_menu.get("ok"))
    if not clicked_menu.get("ok"):
        attempt["block_reason"] = str(clicked_menu.get("reason") or "safe row menu not found")
        return attempt
    _wait_settle(page, 800)
    menu_state = extract_open_row_menu_state(page)
    attempt["menu_labels"] = menu_state.get("items") or []
    attempt["complaint_action_found"] = bool(menu_state.get("complaint_action_found"))
    attempt["complaint_action_available"] = bool(menu_state.get("complaint_action_found"))
    if not menu_state.get("complaint_action_found"):
        attempt["block_reason"] = "Пожаловаться на отзыв action not found in row menu"
        _safe_escape(page)
        return attempt
    attempt["actionable_row_found"] = True
    attempt["block_reason"] = ""
    return attempt


def apply_seller_portal_feedback_filters(
    page: Page,
    config: DryRunConfig,
    api_row: Mapping[str, Any],
    *,
    expected_ui: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    expected_ui = expected_ui or {}
    date_from, date_to = feedback_filter_date_range(config, api_row, expected_ui=expected_ui)
    stars = feedback_filter_stars(config, api_row, expected_ui=expected_ui)
    before_signature = feedback_list_signature(page)
    date_result = apply_seller_portal_date_filter(page, date_from=date_from, date_to=date_to)
    _wait_settle(page, 900)
    star_result = apply_seller_portal_star_filter(page, stars=stars)
    _wait_settle(page, 1200)
    after_signature = feedback_list_signature(page)
    list_update = bool(after_signature.get("fingerprint") and after_signature.get("fingerprint") != before_signature.get("fingerprint"))
    state = inspect_seller_portal_filter_state(page)
    return {
        "requested_date_from": date_from,
        "requested_date_to": date_to,
        "requested_stars": list(stars),
        "date_filter": date_result,
        "star_filter": star_result,
        "date_filter_applied": bool(date_result.get("applied")),
        "star_filter_applied": bool(star_result.get("applied")),
        "status_tab_selected": True,
        "reset_clear_used": False,
        "list_signature_before": before_signature,
        "list_signature_after": after_signature,
        "list_update_observed": list_update,
        "current_visible_date_range": state.get("visible_date_range") or "",
        "current_selected_stars": state.get("selected_stars") or star_result.get("selected_stars") or [],
        "selectors_used": [*(date_result.get("selectors_used") or []), *(star_result.get("selectors_used") or [])],
        "blocker": str(date_result.get("reason") or star_result.get("reason") or ""),
    }


def feedback_filter_date_range(
    config: DryRunConfig,
    api_row: Mapping[str, Any],
    *,
    expected_ui: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    expected_ui = expected_ui or {}
    candidate_date = (
        normalize_date_key(expected_ui.get("review_datetime") or expected_ui.get("review_date") or expected_ui.get("created_at"))
        or normalize_date_key(api_row.get("created_at") or api_row.get("created_date") or api_row.get("review_datetime"))
    )
    if candidate_date:
        return candidate_date, candidate_date
    return config.date_from, config.date_to


def feedback_filter_stars(
    config: DryRunConfig,
    api_row: Mapping[str, Any],
    *,
    expected_ui: Mapping[str, Any] | None = None,
) -> tuple[int, ...]:
    expected_ui = expected_ui or {}
    rating = normalize_rating(expected_ui.get("rating") or api_row.get("product_valuation") or api_row.get("rating"))
    if rating:
        return (int(rating),)
    return tuple(config.stars)


def apply_seller_portal_date_filter(page: Page, *, date_from: str, date_to: str) -> dict[str, Any]:
    date_from_ru = format_ru_date(date_from)
    date_to_ru = format_ru_date(date_to)
    result: dict[str, Any] = {
        "requested_date_from": date_from,
        "requested_date_to": date_to,
        "requested_date_from_ru": date_from_ru,
        "requested_date_to_ru": date_to_ru,
        "opened": False,
        "applied": False,
        "inputs_filled": False,
        "apply_clicked": False,
        "selectors_used": [],
        "reason": "",
    }
    if not date_from_ru or not date_to_ru:
        result["reason"] = "date range is unavailable"
        return result
    try:
        opened = page.evaluate(
            r"""
({dateFrom, dateTo}) => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const labelFor = (el) => (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('title') || '').replace(/\s+/g, ' ').trim();
  const dateRe = /\b\d{1,2}[.\/]\d{1,2}[.\/]\d{2,4}\s*[-–]\s*\d{1,2}[.\/]\d{1,2}[.\/]\d{2,4}\b/;
  const clickables = Array.from(document.querySelectorAll('button, [role="button"], input, [class*="date"], [class*="Date"], div, span'))
    .filter(visible)
    .map((el, index) => {
      const rect = el.getBoundingClientRect();
      const text = labelFor(el);
      let score = 0;
      if (dateRe.test(text)) score += 120;
      if (/дата|период|date/i.test(text + ' ' + (el.getAttribute('class') || '') + ' ' + (el.getAttribute('data-testid') || ''))) score += 70;
      if (rect.top < 360) score += 20;
      if (/отзыв|оценка|фильтр/i.test(text)) score -= 30;
      return {el, index, text, tag: el.tagName.toLowerCase(), rect: {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)}, score};
    })
    .filter((item) => item.score > 60)
    .sort((a, b) => b.score - a.score || a.index - b.index);
  const target = clickables[0];
  if (!target) {
    const bodyText = (document.body.innerText || '').replace(/\s+/g, ' ');
    return {ok: false, reason: 'date range control not found', visible_date_range_seen: bodyText.includes(dateFrom) && bodyText.includes(dateTo)};
  }
  target.el.click();
  return {ok: true, selector: target.tag, text: target.text.slice(0, 120), rect: target.rect};
}
            """,
            {"dateFrom": date_from_ru, "dateTo": date_to_ru},
        )
        result["open_control"] = opened
        result["opened"] = bool(opened.get("ok"))
        if opened.get("selector"):
            result["selectors_used"].append(f"date_control:{opened.get('selector')}")
    except PlaywrightError as exc:
        result["reason"] = safe_text(str(exc), 300)
        return result
    _wait_settle(page, 800)
    try:
        filled = page.evaluate(
            r"""
({dateFrom, dateTo, marker}) => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const text = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const labelFor = (el) => {
    const parts = [el.getAttribute('aria-label'), el.getAttribute('placeholder'), el.getAttribute('title'), el.getAttribute('name'), el.getAttribute('data-testid')];
    const id = el.getAttribute('id');
    if (id) {
      const label = document.querySelector(`label[for="${CSS.escape(id)}"]`);
      if (label) parts.push(label.innerText || label.textContent || '');
    }
    let parent = el.parentElement;
    for (let depth = 0; parent && depth < 3; depth += 1, parent = parent.parentElement) {
      parts.push(parent.innerText || parent.textContent || '');
    }
    return text(parts.filter(Boolean).join(' | '));
  };
  const roots = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], [class*="popup"], [class*="Popup"], [class*="popover"], [class*="Popover"], [class*="dropdown"], [class*="Dropdown"], body')).filter(visible);
  const root = roots.find((node) => /дата|период|date|календар|применить/i.test(text(node.innerText || node.textContent || ''))) || document.body;
  const inputs = Array.from(root.querySelectorAll('input')).filter(visible)
    .filter((el) => !['checkbox', 'radio', 'button', 'submit', 'hidden', 'search'].includes(String(el.getAttribute('type') || '').toLowerCase()))
    .map((el, index) => ({el, index, label: labelFor(el), value: String(el.value || ''), type: String(el.getAttribute('type') || '').toLowerCase()}));
  const isDateLike = (item) => {
    const haystack = item.label + ' ' + item.value + ' ' + item.type;
    if (/itemsPerPage|Показать отзывов|page|perPage/i.test(haystack)) return false;
    return /дата|период|date|дд|мм|yyyy|гггг|__.__.____|\d{1,2}[.\/]\d{1,2}/i.test(haystack);
  };
  const dateInputs = inputs.filter(isDateLike);
  const selected = dateInputs.length >= 1 ? dateInputs.slice(0, Math.min(2, dateInputs.length)) : [];
  root.querySelectorAll('[' + marker + ']').forEach((el) => el.removeAttribute(marker));
  if (!selected.length) return {ok: false, reason: 'visible date input was not found', inputs_considered: inputs.map((item) => item.label).slice(0, 8)};
  const setValue = (node, value) => {
    const proto = node.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
    if (descriptor && descriptor.set) descriptor.set.call(node, value);
    else node.value = value;
    node.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: value}));
    node.dispatchEvent(new Event('change', {bubbles: true}));
    node.blur();
  };
  if (selected.length === 1) {
    setValue(selected[0].el, dateFrom + ' - ' + dateTo);
    selected[0].el.setAttribute(marker, 'range');
  } else {
    setValue(selected[0].el, dateFrom);
    setValue(selected[1].el, dateTo);
    selected[0].el.setAttribute(marker, 'from');
    selected[1].el.setAttribute(marker, 'to');
  }
  return {ok: true, fields: selected.map((item) => ({label: item.label.slice(0, 160), value: item.el.value || ''}))};
}
            """,
            {"dateFrom": date_from_ru, "dateTo": date_to_ru, "marker": SELLER_PORTAL_DATE_FILTER_MARKER_ATTR},
        )
        result["fill_inputs"] = filled
        result["inputs_filled"] = bool(filled.get("ok"))
        if filled.get("ok"):
            result["selectors_used"].append("date_inputs")
    except PlaywrightError as exc:
        result["fill_inputs"] = {"ok": False, "reason": safe_text(str(exc), 300)}
    try:
        applied = click_filter_apply_button(page, context_hint="date")
        result["date_apply_click"] = applied
        result["apply_clicked"] = bool(applied.get("ok"))
        if applied.get("ok"):
            result["selectors_used"].append("date_apply")
    except Exception as exc:  # pragma: no cover - live fallback
        result["date_apply_click"] = {"ok": False, "reason": safe_text(str(exc), 300)}
    state = inspect_seller_portal_filter_state(page)
    visible_range = str(state.get("visible_date_range") or "")
    result["visible_date_range_after"] = visible_range
    result["applied"] = bool(result["inputs_filled"] or (date_from_ru in visible_range and date_to_ru in visible_range))
    if not result["applied"] and not result["reason"]:
        result["reason"] = str((result.get("fill_inputs") or {}).get("reason") or (result.get("open_control") or {}).get("reason") or "date filter was not applied")
    return result


def apply_seller_portal_star_filter(page: Page, *, stars: Iterable[int]) -> dict[str, Any]:
    requested = sorted({int(star) for star in stars if 1 <= int(star) <= 5})
    result: dict[str, Any] = {
        "requested_stars": requested,
        "opened": False,
        "rating_section_opened": False,
        "applied": False,
        "selected_stars": [],
        "selected_star_values_before": [],
        "selected_star_values_after": [],
        "state_readable": False,
        "apply_clicked": False,
        "selectors_used": [],
        "reason": "",
    }
    if not requested:
        result["reason"] = "rating/star filter is unavailable"
        return result
    try:
        opened = open_seller_portal_filters_popup(page)
        result["open_filters"] = opened
        result["opened"] = bool(opened.get("ok"))
        if opened.get("selector"):
            result["selectors_used"].append(f"filters_button:{opened.get('selector')}")
    except PlaywrightError as exc:
        result["reason"] = safe_text(str(exc), 300)
        return result
    _wait_settle(page, 800)
    try:
        section = activate_seller_portal_rating_filter_section(page)
        result["rating_section"] = section
        result["rating_section_opened"] = bool(section.get("ok"))
        if section.get("selector"):
            result["selectors_used"].append(f"rating_section:{section.get('selector')}")
    except Exception as exc:  # pragma: no cover - live fallback
        result["rating_section"] = {"ok": False, "reason": safe_text(str(exc), 300)}
    _wait_settle(page, 500)
    before_state = inspect_seller_portal_rating_filter_popup(page)
    result["popup_before"] = before_state
    result["selected_star_values_before"] = before_state.get("selected_star_values") or []
    try:
        selected = select_seller_portal_rating_filter_stars(page, stars=requested)
        result["select_stars"] = selected
        result["selected_stars"] = selected.get("selected_star_values_after") or selected.get("selected_stars") or []
        result["selected_star_values_after"] = selected.get("selected_star_values_after") or []
        result["state_readable"] = bool(selected.get("state_readable"))
        if selected.get("ok"):
            result["selectors_used"].append(str(selected.get("selector_strategy") or "review_rating_checkboxes"))
    except PlaywrightError as exc:
        result["select_stars"] = {"ok": False, "reason": safe_text(str(exc), 300)}
    _wait_settle(page, 350)
    after_state = inspect_seller_portal_rating_filter_popup(page)
    result["popup_after"] = after_state
    if after_state.get("selected_star_values"):
        result["selected_star_values_after"] = after_state.get("selected_star_values") or []
        result["selected_stars"] = result["selected_star_values_after"]
        result["state_readable"] = True
    try:
        applied = click_filter_apply_button(page, context_hint="filters")
        result["filters_apply_click"] = applied
        result["apply_clicked"] = bool(applied.get("ok"))
        if applied.get("ok"):
            result["selectors_used"].append("filters_apply")
    except Exception as exc:  # pragma: no cover - live fallback
        result["filters_apply_click"] = {"ok": False, "reason": safe_text(str(exc), 300)}
    selected_after = {int(star) for star in result.get("selected_star_values_after") or [] if str(star).isdigit()}
    requested_set = set(requested)
    result["applied"] = bool((result.get("select_stars") or {}).get("ok") and result["apply_clicked"] and selected_after == requested_set)
    if not result["applied"] and not result["reason"]:
        result["reason"] = str((result.get("select_stars") or {}).get("reason") or (result.get("open_filters") or {}).get("reason") or "star filter was not applied")
    return result


def open_seller_portal_filters_popup(page: Page) -> dict[str, Any]:
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
  const buildCandidates = (selector, allowTextFallback) => Array.from(document.querySelectorAll(selector))
    .filter(visible)
    .map((el, index) => {
      const rect = el.getBoundingClientRect();
      const text = labelFor(el);
      const interactive = el.matches('button, [role="button"], a') ? el : el.closest('button, [role="button"], a');
      const clickable = interactive || (allowTextFallback ? el : null);
      const disabled = Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true' || (interactive && interactive.getAttribute('aria-disabled') === 'true'));
      let score = 0;
      if (/^Фильтры$/i.test(text)) score += 160;
      else if (/фильтр/i.test(text)) score += 80;
      if (interactive) score += 80;
      if (rect.top < 420) score += 20;
      if (/Применить|Сбросить|Оценка отзыва/i.test(text)) score -= 60;
      return {el, clickable, index, text, tag: el.tagName.toLowerCase(), score, disabled, interactive: Boolean(interactive)};
    })
    .filter((item) => item.score > 70 && item.clickable && !item.disabled)
    .sort((a, b) => b.score - a.score || a.index - b.index);
  let candidates = buildCandidates('button, [role="button"], a', false);
  let selectorStrategy = 'interactive_button_or_role';
  if (!candidates.length) {
    candidates = buildCandidates('div, span', true);
    selectorStrategy = 'visible_text_fallback';
  }
  const target = candidates[0];
  if (!target) return {ok: false, reason: 'filters button not found', visible_candidates: candidates.map((item) => item.text).slice(0, 8)};
  target.clickable.click();
  return {ok: true, selector: target.tag, text: target.text, selector_strategy: selectorStrategy, interactive: target.interactive};
}
            """
        )
    except PlaywrightError as exc:
        return {"ok": False, "reason": safe_text(str(exc), 300)}


def activate_seller_portal_rating_filter_section(page: Page) -> dict[str, Any]:
    try:
        return page.evaluate(
            r"""
() => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const text = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const labelFor = (el) => text(el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '');
  const roots = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], [class*="popup"], [class*="Popup"], [class*="popover"], [class*="Popover"], [class*="dropdown"], [class*="Dropdown"], [class*="drawer"], [class*="Drawer"], aside, body')).filter(visible);
  const root = roots.find((node) => /Тип отзыва|Оценка отзыва|Применить|Сбросить/i.test(text(node.innerText || node.textContent || ''))) || null;
  if (!root) return {ok: false, reason: 'filters popup root not found'};
  const items = Array.from(root.querySelectorAll('button, [role="button"], [role="tab"], a, div, span'))
    .filter(visible)
    .map((el, index) => {
      const rect = el.getBoundingClientRect();
      const label = labelFor(el);
      const clickable = el.closest('button, [role="button"], [role="tab"], a') || el;
      const exact = /^Оценка отзыва$/i.test(label);
      const score = exact ? 100 : (/Оценка отзыва/i.test(label) ? 40 : 0);
      return {el, clickable, index, label, score, rect: {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)}};
    })
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score || a.rect.y - b.rect.y || a.index - b.index);
  const target = items[0];
  if (!target) {
    const rootText = text(root.innerText || root.textContent || '');
    return {ok: /Оценка отзыва/i.test(rootText), selector: 'root_text_already_visible', reason: /Оценка отзыва/i.test(rootText) ? '' : 'rating section label not found'};
  }
  target.clickable.click();
  return {ok: true, selector: 'visible_text:Оценка отзыва', label: target.label, rect: target.rect};
}
            """
        )
    except PlaywrightError as exc:
        return {"ok": False, "reason": safe_text(str(exc), 300)}


def inspect_seller_portal_rating_filter_popup(page: Page) -> dict[str, Any]:
    try:
        return page.evaluate(
            r"""
() => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const text = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const safeClass = (el) => String(el.getAttribute('class') || '').replace(/\s+/g, ' ').trim().slice(0, 160);
  const rectFor = (el) => {
    const rect = el.getBoundingClientRect();
    return {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)};
  };
  const labelFor = (el) => text(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '');
  const isChecked = (el, row) => {
    const nodes = [el, row].filter(Boolean);
    for (const node of nodes) {
      const aria = node.getAttribute && node.getAttribute('aria-checked');
      const dataState = node.getAttribute && (node.getAttribute('data-state') || node.getAttribute('data-checked') || node.getAttribute('checked'));
      const cls = node.getAttribute && String(node.getAttribute('class') || '');
      if (node.checked === true || aria === 'true' || /^(true|checked)$/i.test(String(dataState || '')) || /(checked|selected|active|is-checked|is_checked)/i.test(cls)) return true;
    }
    return false;
  };
  const starFromText = (value) => {
    const normalized = text(value).replace(/★/g, ' ★ ');
    for (let star = 1; star <= 5; star += 1) {
      const re = new RegExp('(^|[^0-9])' + star + '([^0-9]|$)');
      if (re.test(normalized) && (/★|зв[её]зд|оценк/i.test(normalized) || normalized.length <= 18)) return star;
    }
    return 0;
  };
  const rootCandidates = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], [class*="popup"], [class*="Popup"], [class*="popover"], [class*="Popover"], [class*="dropdown"], [class*="Dropdown"], [class*="drawer"], [class*="Drawer"], aside, body'))
    .filter(visible)
    .map((el, index) => ({el, index, text: text(el.innerText || el.textContent || ''), rect: rectFor(el), class_name: safeClass(el), role: el.getAttribute('role') || ''}))
    .filter((item) => /Оценка отзыва|Тип отзыва|Применить|Сбросить/i.test(item.text))
    .sort((a, b) => (a.el === document.body ? 1 : 0) - (b.el === document.body ? 1 : 0) || (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height));
  const rootItem = rootCandidates[0] || null;
  if (!rootItem) return {popup_opened: false, rating_section_opened: false, selected_star_values: [], rows: [], candidate_selectors: {}, reason: 'filters popup root not found'};
  const root = rootItem.el;
  const controls = Array.from(root.querySelectorAll('input[type="checkbox"], [role="checkbox"], [aria-checked], [class*="checkbox"], [class*="Checkbox"], [class*="check"], [class*="Check"]'))
    .filter(visible);
  const rawRows = controls.map((control, index) => {
    let row = control.closest('label, li, [role="option"], [role="menuitem"], [class*="row"], [class*="Row"], [class*="item"], [class*="Item"], div') || control.parentElement || control;
    let star = 0;
    let rowText = '';
    let current = row;
    for (let depth = 0; current && depth < 6; depth += 1, current = current.parentElement) {
      rowText = labelFor(current);
      star = starFromText(rowText);
      if (star) {
        row = current;
        break;
      }
    }
    return {control, row, index, star, text: labelFor(row), checked: isChecked(control, row), control_rect: rectFor(control), row_rect: rectFor(row), control_class: safeClass(control), row_class: safeClass(row), role: control.getAttribute('role') || '', aria_checked: control.getAttribute('aria-checked') || ''};
  });
  let rows = rawRows.filter((item) => item.star >= 1 && item.star <= 5);
  if (rows.length < 5 && rawRows.length >= 5) {
    rows = rawRows.slice(0, 5).map((item, index) => ({...item, star: 5 - index, inferred_by_order: true}));
  }
  const unique = new Map();
  rows.forEach((item) => {
    if (!unique.has(item.star)) unique.set(item.star, item);
  });
  const finalRows = Array.from(unique.values()).sort((a, b) => b.star - a.star);
  const selected = finalRows.filter((item) => item.checked).map((item) => item.star).sort((a, b) => a - b);
  const buttons = Array.from(root.querySelectorAll('button, [role="button"], a, div, span'))
    .filter(visible)
    .map((el, index) => ({index, text: labelFor(el), tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '', class_name: safeClass(el), rect: rectFor(el)}))
    .filter((item) => /^(Применить|Сбросить)$/i.test(item.text) || /Применить|Сбросить/i.test(item.text))
    .slice(0, 12);
  return {
    popup_opened: true,
    popup_root: {tag: root.tagName.toLowerCase(), role: rootItem.role, class_name: rootItem.class_name, rect: rootItem.rect, text_snippet: rootItem.text.slice(0, 500)},
    rating_section_opened: /Оценка отзыва/i.test(rootItem.text),
    rows: finalRows.map((item) => ({
      star: item.star,
      text: text(item.text).slice(0, 120),
      checked: item.checked,
      inferred_by_order: Boolean(item.inferred_by_order),
      role: item.role,
      aria_checked: item.aria_checked,
      control_class: item.control_class,
      row_class: item.row_class,
      control_rect: item.control_rect,
      row_rect: item.row_rect
    })),
    selected_star_values: selected,
    checkbox_nodes_seen: rawRows.length,
    buttons,
    candidate_selectors: {
      popup_root: '[role=\"dialog\"], [aria-modal=\"true\"], [class*=\"popup\"], [class*=\"Popover\"], [class*=\"Dropdown\"], [class*=\"Drawer\"]',
      star_row: 'rating section visible custom checkbox rows, fallback top-to-bottom 5..1',
      one_star_checkbox: finalRows.find((item) => item.star === 1) ? 'row mapped to star=1 under Оценка отзыва' : '',
      apply_button: 'visible button/role/text Применить'
    },
    stable_selector_found: Boolean(finalRows.find((item) => item.star === 1) && buttons.find((item) => /^Применить$/i.test(item.text)))
  };
}
            """
        )
    except PlaywrightError as exc:
        return {"popup_opened": False, "rating_section_opened": False, "selected_star_values": [], "rows": [], "reason": safe_text(str(exc), 300)}


def select_seller_portal_rating_filter_stars(page: Page, *, stars: Iterable[int]) -> dict[str, Any]:
    requested = sorted({int(star) for star in stars if 1 <= int(star) <= 5})
    try:
        return page.evaluate(
            r"""
({stars, marker}) => {
  const requested = new Set((stars || []).map((value) => Number(value)));
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const text = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const safeText = (value, limit = 120) => text(value).slice(0, limit);
  const labelFor = (el) => text(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '');
  const cls = (el) => String((el && el.getAttribute && el.getAttribute('class')) || '');
  const isChecked = (control, row) => {
    for (const node of [control, row].filter(Boolean)) {
      const aria = node.getAttribute && node.getAttribute('aria-checked');
      const dataState = node.getAttribute && (node.getAttribute('data-state') || node.getAttribute('data-checked') || node.getAttribute('checked'));
      if (node.checked === true || aria === 'true' || /^(true|checked)$/i.test(String(dataState || '')) || /(checked|selected|active|is-checked|is_checked)/i.test(cls(node))) return true;
    }
    return false;
  };
  const stateKnown = (control, row) => {
    for (const node of [control, row].filter(Boolean)) {
      if (node.checked === true || node.checked === false) return true;
      if (node.getAttribute && (node.hasAttribute('aria-checked') || node.hasAttribute('data-state') || node.hasAttribute('data-checked') || node.hasAttribute('checked'))) return true;
      if (/(checked|selected|active|is-checked|is_checked)/i.test(cls(node))) return true;
    }
    return false;
  };
  const rectFor = (el) => {
    const rect = el.getBoundingClientRect();
    return {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)};
  };
  const starFromText = (value) => {
    const normalized = text(value).replace(/★/g, ' ★ ');
    for (let star = 1; star <= 5; star += 1) {
      const re = new RegExp('(^|[^0-9])' + star + '([^0-9]|$)');
      if (re.test(normalized) && (/★|зв[её]зд|оценк/i.test(normalized) || normalized.length <= 18)) return star;
    }
    return 0;
  };
  const roots = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], [class*="popup"], [class*="Popup"], [class*="popover"], [class*="Popover"], [class*="dropdown"], [class*="Dropdown"], [class*="drawer"], [class*="Drawer"], aside, body'))
    .filter(visible)
    .map((el, index) => ({el, index, body: text(el.innerText || el.textContent || ''), rect: rectFor(el)}))
    .filter((item) => /Оценка отзыва|Тип отзыва|Применить|Сбросить/i.test(item.body))
    .sort((a, b) => (a.el === document.body ? 1 : 0) - (b.el === document.body ? 1 : 0) || (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height));
  const rootItem = roots[0];
  if (!rootItem) return {ok: false, reason: 'filters popup with review rating section was not found', selected_star_values_after: []};
  const root = rootItem.el;
  root.querySelectorAll('[' + marker + ']').forEach((el) => el.removeAttribute(marker));
  const controls = Array.from(root.querySelectorAll('input[type="checkbox"], [role="checkbox"], [aria-checked], [class*="checkbox"], [class*="Checkbox"], [class*="check"], [class*="Check"]')).filter(visible);
  const rawRows = controls.map((control, index) => {
    let row = control.closest('label, li, [role="option"], [role="menuitem"], [class*="row"], [class*="Row"], [class*="item"], [class*="Item"], div') || control.parentElement || control;
    let star = 0;
    let rowText = '';
    let current = row;
    for (let depth = 0; current && depth < 6; depth += 1, current = current.parentElement) {
      rowText = labelFor(current);
      star = starFromText(rowText);
      if (star) {
        row = current;
        break;
      }
    }
    return {control, row, index, star, text: rowText || labelFor(row), checked: isChecked(control, row), state_known: stateKnown(control, row), rect: rectFor(row)};
  });
  let rows = rawRows.filter((item) => item.star >= 1 && item.star <= 5);
  let selectorStrategy = 'text_or_aria_checkbox_rows';
  if (rows.length < 5 && rawRows.length >= 5) {
    rows = rawRows.slice(0, 5).map((item, index) => ({...item, star: 5 - index, inferred_by_order: true}));
    selectorStrategy = 'custom_checkbox_order_fallback_5_to_1';
  }
  const unique = new Map();
  rows.forEach((item) => {
    if (!unique.has(item.star)) unique.set(item.star, item);
  });
  rows = Array.from(unique.values()).sort((a, b) => b.star - a.star);
  if (!rows.length) return {ok: false, reason: 'star checkbox rows were not found', checkbox_rows_seen: rawRows.map((item) => safeText(item.text)).slice(0, 10), selected_star_values_after: []};
  const before = rows.filter((item) => item.checked).map((item) => item.star).sort((a, b) => a - b);
  const clicked = [];
  rows.forEach((item) => {
    const shouldCheck = requested.has(item.star);
    const known = item.state_known;
    const checked = isChecked(item.control, item.row);
    if (known && checked === shouldCheck) return;
    if (!known && !shouldCheck) return;
    const target = item.control || item.row;
    const clickTarget = target.closest && target.closest('label, button, [role="checkbox"], [role="button"]') || target;
    clickTarget.click();
    item.row.setAttribute(marker, String(item.star));
    clicked.push({star: item.star, target_state: shouldCheck, state_known_before: known, text: safeText(item.text), selector_strategy: selectorStrategy});
  });
  const after = rows.map((item) => ({...item, checked_after: isChecked(item.control, item.row), state_known_after: stateKnown(item.control, item.row)}));
  const selectedAfter = after.filter((item) => item.checked_after).map((item) => item.star).sort((a, b) => a - b);
  const allReadable = after.every((item) => item.state_known_after || item.inferred_by_order);
  const requestedClicked = clicked.some((item) => requested.has(item.star));
  return {
    ok: requestedClicked || selectedAfter.some((star) => requested.has(star)),
    selector_strategy: selectorStrategy,
    state_readable: allReadable,
    rows_seen: rows.map((item) => ({star: item.star, text: safeText(item.text), checked_before: item.checked, state_known_before: item.state_known, inferred_by_order: Boolean(item.inferred_by_order), rect: item.rect})),
    clicked,
    selected_star_values_before: before,
    selected_star_values_after: selectedAfter,
    selected_stars: selectedAfter
  };
}
            """,
            {"stars": requested, "marker": SELLER_PORTAL_STAR_FILTER_MARKER_ATTR},
        )
    except PlaywrightError as exc:
        return {"ok": False, "reason": safe_text(str(exc), 300), "selected_star_values_after": []}


def click_filter_apply_button(page: Page, *, context_hint: str) -> dict[str, Any]:
    try:
        return page.evaluate(
            r"""
({contextHint}) => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const labelFor = (el) => (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').replace(/\s+/g, ' ').trim();
  const roots = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], [class*="popup"], [class*="Popup"], [class*="popover"], [class*="Popover"], [class*="dropdown"], [class*="Dropdown"], aside, body')).filter(visible);
  const root = roots.find((node) => /Применить/i.test(node.innerText || node.textContent || '')) || document.body;
  const buttons = Array.from(root.querySelectorAll('button, [role="button"]')).filter(visible).map((el, index) => ({el, index, text: labelFor(el), disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true')}));
  const target = buttons.find((item) => /^Применить$/i.test(item.text) && !item.disabled) || buttons.find((item) => /Применить/i.test(item.text) && !item.disabled);
  if (target) {
    target.el.click();
    return {ok: true, label: target.text, context_hint: contextHint, selector: 'button_or_role'};
  }
  const textCandidates = Array.from(root.querySelectorAll('button, [role="button"], a, div, span')).filter(visible).map((el, index) => {
    const rect = el.getBoundingClientRect();
    const text = labelFor(el);
    const clickable = el.closest('button, [role="button"], a') || el;
    return {el, clickable, index, text, disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'), rect: {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)}};
  }).filter((item) => /^Применить$/i.test(item.text) && !item.disabled)
    .sort((a, b) => a.rect.y - b.rect.y || a.index - b.index);
  const textTarget = textCandidates[0];
  if (textTarget) {
    textTarget.clickable.click();
    return {ok: true, label: textTarget.text, context_hint: contextHint, selector: 'visible_text', rect: textTarget.rect};
  }
  return {ok: false, reason: 'apply button not found', context_hint: contextHint, visible_buttons: buttons.map((item) => item.text).filter(Boolean).slice(0, 12)};
}
            """,
            {"contextHint": context_hint},
        )
    except PlaywrightError as exc:
        return {"ok": False, "reason": safe_text(str(exc), 300), "context_hint": context_hint}


def inspect_seller_portal_filter_state(page: Page) -> dict[str, Any]:
    try:
        return page.evaluate(
            r"""
() => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const text = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const bodyText = text(document.body.innerText || '');
  const rangeMatch = bodyText.match(/\b\d{1,2}[.\/]\d{1,2}[.\/]\d{2,4}\s*[-–]\s*\d{1,2}[.\/]\d{1,2}[.\/]\d{2,4}\b/);
  const selectedStars = [];
  Array.from(document.querySelectorAll('input[type="checkbox"]')).filter(visible).forEach((input) => {
    if (!input.checked && input.getAttribute('aria-checked') !== 'true') return;
    const root = input.closest('label, li, [role="checkbox"], [class*="checkbox"], [class*="Checkbox"], div') || input.parentElement || input;
    const rowText = text(root.innerText || root.textContent || '');
    for (let star = 1; star <= 5; star += 1) {
      const re = new RegExp('(^|[^0-9])' + star + '([^0-9]|$)');
      if (re.test(rowText) && (/★|зв[её]зд|оценк/i.test(rowText) || rowText.length <= 12) && !selectedStars.includes(star)) selectedStars.push(star);
    }
  });
  return {visible_date_range: rangeMatch ? rangeMatch[0] : '', selected_stars: selectedStars.sort(), text_fingerprint: bodyText.slice(0, 500)};
}
            """
        )
    except PlaywrightError as exc:
        return {"reason": safe_text(str(exc), 300), "visible_date_range": "", "selected_stars": []}


def feedback_list_signature(page: Page) -> dict[str, Any]:
    try:
        return page.evaluate(
            r"""
() => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 120 && rect.height > 20;
  };
  const rows = Array.from(document.querySelectorAll('[data-testid*="feedback"], [class*="Feedback"], [class*="feedback"], [class*="Table-row"], [class*="table-row"], article, li'))
    .filter(visible)
    .map((el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim())
    .filter(Boolean)
    .slice(0, 20);
  const text = rows.join(' || ') || (document.body.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 2000);
  let hash = 0;
  for (let i = 0; i < text.length; i += 1) hash = ((hash << 5) - hash + text.charCodeAt(i)) | 0;
  return {row_count: rows.length, fingerprint: String(hash), sample: rows.slice(0, 3)};
}
            """
        )
    except PlaywrightError as exc:
        return {"row_count": 0, "fingerprint": "", "reason": safe_text(str(exc), 300), "sample": []}


def format_ru_date(value: Any) -> str:
    date_key = normalize_date_key(value)
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_key):
        return ""
    year, month, day = date_key.split("-")
    return f"{day}.{month}.{year}"


def feedback_tab_candidates(
    api_row: Mapping[str, Any],
    expected_ui: Mapping[str, Any] | None = None,
    *,
    requested_is_answered: str = "all",
) -> list[str]:
    expected_ui = expected_ui or {}
    is_answered = coerce_boolish(api_row.get("is_answered"))
    if is_answered is None:
        is_answered = coerce_boolish(expected_ui.get("is_answered"))
    if is_answered is None:
        if requested_is_answered == "true":
            is_answered = True
        elif requested_is_answered == "false":
            is_answered = False
    if is_answered is True:
        ordered = [FEEDBACKS_ANSWERED_TAB_LABEL, FEEDBACKS_TAB_LABEL, FEEDBACKS_UNANSWERED_TAB_LABEL]
    elif is_answered is False:
        ordered = [FEEDBACKS_UNANSWERED_TAB_LABEL, FEEDBACKS_TAB_LABEL, FEEDBACKS_ANSWERED_TAB_LABEL]
    else:
        ordered = [FEEDBACKS_TAB_LABEL, FEEDBACKS_UNANSWERED_TAB_LABEL, FEEDBACKS_ANSWERED_TAB_LABEL]
    return unique_strings(ordered)


def coerce_boolish(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "y", "answered", "есть ответ", "answered_feedback"}:
        return True
    if text in {"false", "0", "no", "n", "not_answered", "unanswered", "ждут ответа", "без ответа"}:
        return False
    return None


def unique_strings(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def clear_article_search(page: Page) -> dict[str, Any]:
    try:
        locator = page.locator('input[type="search"][placeholder*="Артикулам WB"]').first
        if locator.count() < 1:
            return {"ok": False, "reason": "article search field not found"}
        locator.fill("", timeout=2000)
        page.keyboard.press("Enter")
        _wait_settle(page, 700)
        return {"ok": True, "field": "Поиск по Артикулам WB"}
    except PlaywrightError as exc:
        return {"ok": False, "reason": safe_text(str(exc), 240)}


def actionability_block_reason(expected_ui: Mapping[str, Any], attempt: Mapping[str, Any]) -> str:
    if expected_ui and expected_ui.get("complaint_action_found") is False:
        return "Seller Portal cursor row says complaint action is unavailable"
    filters = attempt.get("filter_controller") if isinstance(attempt.get("filter_controller"), Mapping) else {}
    if filters and not (filters.get("date_filter_applied") and filters.get("star_filter_applied")):
        return "exact cursor match exists, but Seller Portal date/star UI filters could not be fully applied"
    if (attempt.get("targeted_search") or {}).get("ok"):
        return "exact cursor match exists, but actionable DOM row was not found after status/date/star filters, WB-article search and bounded scroll"
    return "exact cursor match exists, but actionable DOM row was not found after status/date/star filters and bounded scroll"


def empty_actionability_resolver_state() -> dict[str, Any]:
    return {
        "feedback_id": "",
        "tabs_tried": [],
        "tab_used": "",
        "attempts": [],
        "expected_ui_summary": {},
        "actionable_row_found": False,
        "row_visible": False,
        "menu_found": False,
        "complaint_action_found": False,
        "complaint_action_available": False,
        "modal_opened_in_dry_run": False,
        "locator_strategy": "",
        "visible_rows_checked": 0,
        "visible_rows_checked_after_search": 0,
        "visible_rows_checked_after_scroll": 0,
        "visible_row_match": {},
        "targeted_search": {},
        "filter_controller": {},
        "date_filter_applied": False,
        "star_filter_applied": False,
        "search_used": False,
        "scroll_used": False,
        "row_menu_click": {},
        "menu_labels": [],
        "resolved_row_summary": {},
        "block_reason": "",
    }


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
        "visible_rows_checked_after_scroll": 0,
        "actionability_resolver": empty_actionability_resolver_state(),
        "tab_used": "",
        "locator_strategy": "",
        "complaint_action_found": False,
        "visible_row_match": {},
        "targeted_search": {},
        "filter_controller": {},
        "date_filter_applied": False,
        "star_filter_applied": False,
        "search_used": False,
        "scroll_used": False,
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
    actionable_found = [
        item
        for item in candidates
        if bool(((item.get("modal") or {}).get("actionability_resolver") or {}).get("actionable_row_found"))
    ]
    complaint_action_found = [item for item in candidates if (item.get("modal") or {}).get("complaint_action_found")]
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
        "actionable_row_found_count": len(actionable_found),
        "complaint_action_found_count": len(complaint_action_found),
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
        "actionable_row_found_count": 0,
        "complaint_action_found_count": 0,
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


def normalize_deny_feedback_ids(values: Iterable[Any]) -> tuple[str, ...]:
    result: list[str] = []
    for item in [*DEFAULT_DENY_FEEDBACK_IDS, *list(values or [])]:
        for part in str(item or "").split(","):
            text = part.strip()
            if text and text not in result:
                result.append(text)
    return tuple(result)


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
        f"- Actionable rows found: `{agg.get('actionable_row_found_count', 0)}`",
        f"- Complaint actions found: `{agg.get('complaint_action_found_count', 0)}`",
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
        filters = modal.get("filter_controller") or {}
        lines.extend(
            [
                f"- `{candidate.get('feedback_id')}` selected `{candidate.get('selected_for_dry_run')}` fit `{ai.get('complaint_fit')}` match `{match.get('match_status')}` modal `{modal.get('draft_prepared')}`",
                f"  API: `{api.get('created_at')}` rating `{api.get('rating')}` nm `{api.get('nm_id')}` article `{api.get('supplier_article')}` text `{api.get('review_text')}` tags `{', '.join(api.get('review_tags') or [])}`",
                f"  AI: `{ai.get('category_label')}` / `{ai.get('confidence_label')}` reason `{ai.get('reason')}` evidence `{ai.get('evidence')}`",
                f"  Resolver: tab `{modal.get('tab_used')}` strategy `{modal.get('locator_strategy')}` date_filter `{modal.get('date_filter_applied')}` star_filter `{modal.get('star_filter_applied')}` search `{modal.get('search_used')}` scroll `{modal.get('scroll_used')}` rows `{modal.get('visible_rows_checked')}`/`{modal.get('visible_rows_checked_after_search')}`/`{modal.get('visible_rows_checked_after_scroll')}` requested `{filters.get('requested_date_from')}`..`{filters.get('requested_date_to')}` stars `{filters.get('requested_stars')}`",
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
