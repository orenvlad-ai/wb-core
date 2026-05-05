"""Controlled Seller Portal complaint submit runner.

This is the first bounded real-submit contour. It can submit only a tiny number
of exact-matched AI yes/review candidates and requires an explicit confirmation
flag before clicking the final Seller Portal complaint submit button.
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
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import Error as PlaywrightError, Page, Request, Response, sync_playwright  # noqa: E402

from apps.seller_portal_feedbacks_actionable_resolver import (  # noqa: E402
    config_from_dry_run,
    resolve_feedback_actionability,
)
from apps.seller_portal_feedbacks_complaint_dry_run_plan import (  # noqa: E402
    DryRunConfig,
    analyze_feedback_rows,
    apply_exact_matches,
    build_candidate_records,
    build_draft_text,
    build_replay_config,
    build_scout_config,
    choose_complaint_category,
    click_complaint_category,
    collect_matching_rows,
    description_is_ready_for_submit,
    description_persistence_result,
    empty_modal_candidate_state,
    expected_ui_for_filter_aware_resolver,
    fill_description_field,
    find_visible_actionable_row,
    load_api_feedback_rows,
    normalize_requested_date,
    normalize_text,
    should_open_modal_for_match,
    should_try_actionability_resolver,
    wait_for_description_field_ready,
)
from apps.seller_portal_feedbacks_complaints_scout import (  # noqa: E402
    BUSINESS_TZ,
    DEFAULT_START_URL,
    ROW_MENU_COMPLAINT_LABEL,
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
    capture_seller_portal_feedback_headers,
    parse_stars,
    safe_text,
    summarize_api_row,
)
from apps.seller_portal_relogin_session import DEFAULT_STORAGE_STATE_PATH, DEFAULT_WB_BOT_PYTHON  # noqa: E402
from packages.application.feedback_review_tags import normalize_review_tags, reason_contradicts_review_tags  # noqa: E402
from packages.application.sheet_vitrina_v1_feedbacks_complaints import (  # noqa: E402
    JsonFileFeedbacksComplaintJournal,
)


CONTRACT_NAME = "seller_portal_feedbacks_complaint_submit"
CONTRACT_VERSION = "controlled_submit_v1"
DEFAULT_RUNTIME_DIR = Path(os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR", "/opt/wb-core-runtime/state"))
DEFAULT_OUTPUT_ROOT = Path("/opt/wb-core-runtime/state/feedbacks_complaint_submit")
LOCAL_OUTPUT_ROOT = Path("artifacts/seller_portal_feedbacks_complaint_submit")
MAX_SUBMIT_HARD_CAP = 1
DEFAULT_DENY_FEEDBACK_IDS = ("GPe9vrq0kctlSfobrgq2", "fdQpHhNXTosEkArTHAZF")
SUBMIT_RESULT_CONFIRMED_SUCCESS = "confirmed_success"
SUBMIT_RESULT_CONFIRMED_VALIDATION_ERROR = "confirmed_validation_error"
SUBMIT_RESULT_CONFIRMED_NETWORK_ERROR = "confirmed_network_error"
SUBMIT_RESULT_UNCONFIRMED_AFTER_CLICK = "unconfirmed_after_click"
SUBMIT_RELEVANT_URL_RE = re.compile(r"(complaint|claim|appeal|feedback|review|жалоб)", re.IGNORECASE)
FORBIDDEN_QUERY_KEY_RE = re.compile(r"(authorization|authorize|token|cookie|secret|key|session|storage)", re.IGNORECASE)
DESCRIPTION_BODY_KEY_RE = re.compile(r"(description|comment|message|text|reason|complaint|claim|appeal|опис|коммент|причин)", re.IGNORECASE)
SUCCESS_TEXT_RE = re.compile(r"(жалоб[ауы].{0,60}(отправ|создан|принят)|успешно.{0,60}жалоб)", re.IGNORECASE)
VALIDATION_TEXT_RE = re.compile(
    r"(обязатель|заполн|выберите|нельзя|ошибк|проверьте|символ|лимит|не удалось|повторите|validation|invalid)",
    re.IGNORECASE,
)
BAD_REASON_PHRASES = (
    "основание неясно",
    "оснований для жалобы неясно",
    "недостаточно данных",
    "оценить вручную",
    "оценить основание вручную",
)


@dataclass(frozen=True)
class SubmitConfig:
    date_from: str
    date_to: str
    stars: tuple[int, ...]
    is_answered: str
    max_api_rows: int
    max_submit: int
    include_review: bool
    dry_run: bool
    require_exact: bool
    retry_errors: bool
    submit_confirmation: bool
    runtime_dir: Path
    storage_state_path: Path
    wb_bot_python: Path
    output_dir: Path
    start_url: str
    headless: bool
    timeout_ms: int
    write_artifacts: bool
    deny_feedback_ids: tuple[str, ...] = DEFAULT_DENY_FEEDBACK_IDS
    target_feedback_id: str = ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--stars", default="1")
    parser.add_argument("--is-answered", choices=("true", "false", "all"), default="false")
    parser.add_argument("--max-api-rows", type=int, default=10)
    parser.add_argument("--max-submit", type=int, default=1)
    parser.add_argument("--include-review", choices=("0", "1"), default="1")
    parser.add_argument("--dry-run", choices=("0", "1"), default="1")
    parser.add_argument("--require-exact", choices=("0", "1"), default="1")
    parser.add_argument("--retry-errors", choices=("0", "1"), default="0")
    parser.add_argument(
        "--target-feedback-id",
        default="",
        help="Run a no-submit diagnostic for one explicit feedback_id after normal API/AI safety checks.",
    )
    parser.add_argument(
        "--deny-feedback-id",
        action="append",
        default=[],
        help="Feedback id that must never be selected for this runner. May be repeated or comma-separated.",
    )
    parser.add_argument("--i-understand-this-submits-complaints", action="store_true")
    parser.add_argument("--runtime-dir", default=str(DEFAULT_RUNTIME_DIR if DEFAULT_RUNTIME_DIR.exists() else ".runtime"))
    parser.add_argument("--storage-state-path", default=str(DEFAULT_STORAGE_STATE_PATH))
    parser.add_argument("--wb-bot-python", default=str(DEFAULT_WB_BOT_PYTHON))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--start-url", default=DEFAULT_START_URL)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--timeout-ms", type=int, default=20000)
    parser.add_argument("--no-artifacts", action="store_true")
    args = parser.parse_args()

    output_dir = Path(args.output_dir) if args.output_dir else (DEFAULT_OUTPUT_ROOT if Path("/opt/wb-core-runtime/state").exists() else LOCAL_OUTPUT_ROOT)
    config = SubmitConfig(
        date_from=normalize_requested_date(args.date_from),
        date_to=normalize_requested_date(args.date_to),
        stars=parse_stars(args.stars),
        is_answered=args.is_answered,
        max_api_rows=max(1, int(args.max_api_rows)),
        max_submit=max(1, min(MAX_SUBMIT_HARD_CAP, int(args.max_submit))),
        include_review=str(args.include_review) == "1",
        dry_run=str(args.dry_run) != "0",
        require_exact=str(args.require_exact) == "1",
        retry_errors=str(args.retry_errors) == "1",
        submit_confirmation=bool(args.i_understand_this_submits_complaints),
        runtime_dir=Path(args.runtime_dir).expanduser(),
        storage_state_path=Path(args.storage_state_path).expanduser(),
        wb_bot_python=Path(args.wb_bot_python).expanduser(),
        output_dir=output_dir,
        start_url=str(args.start_url).rstrip("/") or DEFAULT_START_URL,
        headless=not args.headed,
        timeout_ms=max(5000, int(args.timeout_ms)),
        write_artifacts=not bool(args.no_artifacts),
        deny_feedback_ids=normalize_deny_feedback_ids(args.deny_feedback_id),
        target_feedback_id=str(args.target_feedback_id or "").strip(),
    )
    report = run_submit(config)
    if config.write_artifacts:
        paths = write_report_artifacts(report, config.output_dir)
        report["artifact_paths"] = {key: str(path) for key, path in paths.items()}
    print(json.dumps(compact_stdout_report(report), ensure_ascii=False, indent=2))


def run_submit(config: SubmitConfig) -> dict[str, Any]:
    if not config.dry_run and not config.submit_confirmation:
        raise RuntimeError("real complaint submission requires --i-understand-this-submits-complaints")
    if config.max_submit > MAX_SUBMIT_HARD_CAP:
        raise RuntimeError(f"max_submit hard cap is {MAX_SUBMIT_HARD_CAP}")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    journal = JsonFileFeedbacksComplaintJournal(config.runtime_dir)
    dry_config = to_dry_run_config(config)
    report: dict[str, Any] = {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "started_at": iso_now(),
        "finished_at": None,
        "run_id": run_id,
        "parameters": {
            "date_from": config.date_from,
            "date_to": config.date_to,
            "stars": list(config.stars),
            "is_answered": config.is_answered,
            "max_api_rows": config.max_api_rows,
            "max_submit": config.max_submit,
            "include_review": config.include_review,
            "dry_run": config.dry_run,
            "require_exact": config.require_exact,
            "retry_errors": config.retry_errors,
            "explicit_submit_flag": config.submit_confirmation,
            "deny_feedback_ids": list(config.deny_feedback_ids),
            "target_feedback_id": config.target_feedback_id,
        },
        "safety": {
            "hard_max_submit": MAX_SUBMIT_HARD_CAP,
            "real_submit_enabled": bool(not config.dry_run and config.submit_confirmation),
            "exact_match_required": config.require_exact,
            "non_exact_submit_allowed": False,
            "complaint_submit_route_exposed": False,
            "old_feedback_id_denylisted": "GPe9vrq0kctlSfobrgq2" in set(config.deny_feedback_ids),
            "previous_successful_feedback_id_denylisted": "fdQpHhNXTosEkArTHAZF" in set(config.deny_feedback_ids),
            "retry_old_submit_allowed": False,
            "mass_submit_allowed": False,
        },
        "api": {},
        "ai": {},
        "session": {},
        "navigation": {},
        "ui": {},
        "candidates": [],
        "aggregate": {},
        "errors": [],
    }

    api_report = load_api_feedback_rows(dry_config)
    report["api"] = api_report
    api_rows = api_report.get("rows") if isinstance(api_report.get("rows"), list) else []
    if not api_rows:
        report["aggregate"] = build_submit_aggregate([])
        report["final_conclusion"] = "no_safe_candidate"
        report["finished_at"] = iso_now()
        return report

    ai_report = analyze_feedback_rows(dry_config, api_rows)
    report["ai"] = ai_report
    if not ai_report.get("success"):
        report["errors"].append({"stage": "ai_analyze", "code": str(ai_report.get("error_code") or ""), "message": str(ai_report.get("blocker") or "")})
        report["aggregate"] = build_submit_aggregate([])
        report["final_conclusion"] = "no_safe_candidate"
        report["finished_at"] = iso_now()
        return report

    analysis_by_id = {str(item.get("feedback_id") or ""): item for item in ai_report.get("results") or []}
    existing_feedback_ids = feedback_ids_already_in_journal(
        [str(row.get("feedback_id") or "") for row in api_rows],
        journal,
        retry_errors=config.retry_errors,
    )
    api_feedback_ids = {str(row.get("feedback_id") or "") for row in api_rows}
    if config.target_feedback_id:
        selected_ids = [config.target_feedback_id] if config.target_feedback_id in api_feedback_ids else []
        if not selected_ids:
            report["errors"].append(
                {
                    "stage": "target_feedback_id",
                    "code": "target_not_in_api_rows",
                    "message": f"target feedback_id {config.target_feedback_id} was not loaded by requested API filters",
                }
            )
    else:
        selected_ids = select_submit_candidate_ids(
            list(analysis_by_id.values()),
            max_submit=config.max_submit,
            max_candidates=config.max_api_rows,
            include_review=config.include_review,
            deny_feedback_ids=config.deny_feedback_ids,
            existing_feedback_ids=existing_feedback_ids,
        )
    candidates = build_candidate_records(api_rows, analysis_by_id, selected_ids)
    if config.target_feedback_id:
        for candidate in candidates:
            is_target = str(candidate.get("feedback_id") or "") == config.target_feedback_id
            candidate["target_feedback_id_requested"] = is_target
            if is_target:
                candidate["ai_eligible_for_submit"] = (candidate.get("ai") or {}).get("complaint_fit") in {"yes", "review"}
    mark_denied_candidates(candidates, config.deny_feedback_ids)
    mark_existing_duplicates(candidates, journal, retry_errors=config.retry_errors)
    selected_api_rows = [
        row
        for row in api_rows
        if str(row.get("feedback_id") or "") in selected_ids
        and not _candidate_by_id(candidates, str(row.get("feedback_id") or "")).get("skip_reason")
    ]
    if selected_api_rows:
        matching_report = collect_matching_rows(dry_config, selected_api_rows)
        report["session"] = matching_report.get("session") or {}
        report["navigation"] = matching_report.get("navigation") or {}
        report["ui"] = matching_report.get("ui") or {}
        if matching_report.get("errors"):
            report["errors"].extend(matching_report["errors"])
        ui_rows = report["ui"].get("rows") if isinstance(report["ui"].get("rows"), list) else []
        apply_exact_matches(candidates, selected_api_rows, ui_rows)
        enforce_submit_guards(candidates)
        submit_candidates = [
            candidate
            for candidate in candidates
            if should_try_actionability_resolver(candidate)
        ]
        if submit_candidates:
            modal_report = submit_modals_for_candidates(config, submit_candidates, selected_api_rows, journal, run_id=run_id)
            report["session"] = modal_report.get("session") or report["session"]
            report["navigation"] = modal_report.get("navigation") or report["navigation"]
            report["ui"]["submit_ui"] = modal_report.get("ui") or {}
            if modal_report.get("errors"):
                report["errors"].extend(modal_report["errors"])
            apply_modal_submit_results(candidates, modal_report.get("candidate_results") or [])
    report["candidates"] = candidates
    report["aggregate"] = build_submit_aggregate(candidates)
    report["final_conclusion"] = determine_final_conclusion(candidates, report["errors"])
    report["finished_at"] = iso_now()
    return report


def select_submit_candidate_ids(
    results: list[Mapping[str, Any]],
    *,
    max_submit: int,
    max_candidates: int | None = None,
    include_review: bool,
    deny_feedback_ids: tuple[str, ...] = (),
    existing_feedback_ids: tuple[str, ...] = (),
) -> list[str]:
    denied = {str(item or "").strip() for item in deny_feedback_ids if str(item or "").strip()}
    existing = {str(item or "").strip() for item in existing_feedback_ids if str(item or "").strip()}
    limit = max(1, int(max_candidates if max_candidates is not None else max_submit))
    if include_review:
        selected: list[str] = []
        for fit in ("yes", "review"):
            for result in results:
                feedback_id = str(result.get("feedback_id") or "").strip()
                if (
                    result.get("complaint_fit") == fit
                    and feedback_id
                    and feedback_id not in selected
                    and feedback_id not in denied
                    and feedback_id not in existing
                ):
                    selected.append(feedback_id)
                    if len(selected) >= limit:
                        return selected
        return selected
    selected: list[str] = []
    for result in results:
        feedback_id = str(result.get("feedback_id") or "")
        if result.get("complaint_fit") == "yes" and feedback_id and feedback_id not in denied and feedback_id not in existing:
            selected.append(feedback_id)
            if len(selected) >= limit:
                break
    return selected


def feedback_ids_already_in_journal(
    feedback_ids: list[str],
    journal: JsonFileFeedbacksComplaintJournal,
    *,
    retry_errors: bool,
) -> tuple[str, ...]:
    existing: list[str] = []
    for feedback_id in feedback_ids:
        normalized = str(feedback_id or "").strip()
        if not normalized:
            continue
        record = journal.find_by_feedback_id(normalized)
        if not record:
            continue
        status = str(record.get("complaint_status") or "")
        if status != "error" or not retry_errors:
            existing.append(normalized)
    return tuple(existing)


def mark_denied_candidates(candidates: list[dict[str, Any]], deny_feedback_ids: tuple[str, ...]) -> None:
    denied = {str(item or "").strip() for item in deny_feedback_ids if str(item or "").strip()}
    for candidate in candidates:
        feedback_id = str(candidate.get("feedback_id") or "").strip()
        if feedback_id and feedback_id in denied:
            candidate["skip_reason"] = "feedback_id is hard-denylisted for controlled submit"


def mark_existing_duplicates(
    candidates: list[dict[str, Any]],
    journal: JsonFileFeedbacksComplaintJournal,
    *,
    retry_errors: bool,
) -> None:
    for candidate in candidates:
        if not candidate.get("selected_for_dry_run"):
            continue
        feedback_id = str(candidate.get("feedback_id") or "")
        existing = journal.find_by_feedback_id(feedback_id)
        if not existing:
            continue
        status = str(existing.get("complaint_status") or "")
        if status != "error" or not retry_errors:
            candidate["skip_reason"] = f"complaint already exists for feedback_id with status={status}"


def enforce_submit_guards(candidates: list[dict[str, Any]]) -> None:
    for candidate in candidates:
        if not candidate.get("selected_for_dry_run") or candidate.get("skip_reason"):
            continue
        ai = candidate.get("ai") or {}
        fit = str(ai.get("complaint_fit") or "")
        if fit not in {"yes", "review"}:
            candidate["skip_reason"] = f"complaint_fit={fit} is not submit-eligible"
            continue
        reason = str(ai.get("reason") or "").strip()
        if not is_reason_submit_ready(reason):
            candidate["skip_reason"] = "AI reason is empty or diagnostic placeholder; submit blocked"
            continue
        review_tags = candidate_review_tags(candidate)
        candidate["tag_diagnostics"] = {
            "api_review_tags": normalize_review_tags((candidate.get("api_summary") or {}).get("review_tags") or []),
            "ui_review_tags": normalize_review_tags(((candidate.get("match") or {}).get("best_ui_candidate") or {}).get("review_tags") or []),
            "combined_review_tags": review_tags,
        }
        if reason_contradicts_review_tags(reason, review_tags):
            candidate["skip_reason"] = "reason_contradicts_review_tags"
            continue
        match = candidate.get("match") or {}
        if not should_open_modal_for_match(match):
            candidate["skip_reason"] = ""
            candidate["filter_aware_resolver_required"] = True
            candidate["preliminary_match_block_reason"] = (
                f"match_status={match.get('match_status')} is not exact; filter-aware resolver must prove exact actionable DOM row"
            )
        else:
            candidate["filter_aware_resolver_required"] = False


def candidate_review_tags(candidate: Mapping[str, Any]) -> list[str]:
    match = candidate.get("match") or {}
    best_ui = match.get("best_ui_candidate") if isinstance(match.get("best_ui_candidate"), Mapping) else {}
    return normalize_review_tags(
        [
            *((candidate.get("api_summary") or {}).get("review_tags") or []),
            *((best_ui or {}).get("review_tags") or []),
        ]
    )


def submit_modals_for_candidates(
    config: SubmitConfig,
    submit_candidates: list[dict[str, Any]],
    api_rows: list[dict[str, Any]],
    journal: JsonFileFeedbacksComplaintJournal,
    *,
    run_id: str,
) -> dict[str, Any]:
    scout_config = build_scout_config(to_dry_run_config(config))
    session = check_session(scout_config)
    report: dict[str, Any] = {
        "success": False,
        "session": session,
        "navigation": {},
        "ui": {"blocker": "", "real_submit_enabled": bool(not config.dry_run and config.submit_confirmation)},
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
                submit_clicks = 0
                for candidate in submit_candidates:
                    if not config.dry_run and submit_clicks >= config.max_submit:
                        report["ui"]["blocker"] = f"max_submit hard cap reached after {submit_clicks} final click(s)"
                        break
                    feedback_id = str(candidate.get("feedback_id") or "")
                    result = submit_one_candidate(
                        page,
                        config,
                        candidate,
                        api_by_id.get(feedback_id),
                        request_headers,
                        journal,
                        run_id=run_id,
                    )
                    report["candidate_results"].append(result)
                    if result.get("submit_clicked"):
                        submit_clicks += int(result.get("submit_clicked_count") or 1)
                    if result.get("submit_clicked") and not config.dry_run:
                        _wait_settle(page, 1800)
                        break
                    if config.dry_run and result.get("blocker") == "dry_run=1; final submit not clicked":
                        break
                    if not candidate_state_allows_next_attempt(result):
                        report["ui"]["blocker"] = str(result.get("blocker") or "candidate left modal/page state uncertain")
                        break
                report["success"] = any(bool(item.get("submit_success")) for item in report["candidate_results"])
            finally:
                context.close()
                browser.close()
    except Exception as exc:
        report["ui"]["blocker"] = safe_text(str(exc), 500)
        report["errors"].append({"stage": "submit_browser", "code": exc.__class__.__name__, "message": safe_text(str(exc), 800)})
    return report


def submit_one_candidate(
    page: Page,
    config: SubmitConfig,
    candidate: Mapping[str, Any],
    api_row: Mapping[str, Any] | None,
    request_headers: Mapping[str, str],
    journal: JsonFileFeedbacksComplaintJournal,
    *,
    run_id: str,
) -> dict[str, Any]:
    feedback_id = str(candidate.get("feedback_id") or "")
    result = empty_modal_candidate_state()
    result.update(
        {
            "feedback_id": feedback_id,
            "dry_run": config.dry_run,
            "submit_clicked": False,
            "submit_clicked_count": 0,
            "submit_success": False,
            "submit_result": "",
            "ai_result": dict(candidate.get("ai") or {}),
            "exact_match_proof": dict(candidate.get("match") or {}),
        }
    )
    if not api_row:
        result["blocker"] = "API row unavailable for submit"
        return result
    expected_ui = expected_ui_for_filter_aware_resolver(candidate)
    draft_text = build_draft_text((candidate.get("ai") or {}))
    resolver = resolve_feedback_actionability(
        page,
        config_from_dry_run(to_dry_run_config(config), open_complaint_modal=False),
        api_row,
        expected_ui=expected_ui,
        preferred_category=str((candidate.get("ai") or {}).get("category_label") or ""),
        description_text=draft_text,
    )
    result["actionability_resolver"] = resolver
    result["visible_rows_checked"] = int(resolver.get("visible_rows_checked") or 0)
    result["visible_rows_checked_after_search"] = int(resolver.get("visible_rows_checked_after_search") or 0)
    result["visible_rows_checked_after_scroll"] = int(resolver.get("dom_rows_collected") or 0)
    result["visible_row_match"] = resolver.get("visible_row_match") or {}
    result["targeted_search"] = resolver.get("targeted_search") or {}
    result["filter_controller"] = resolver.get("filter_controller") or {}
    result["date_filter_applied"] = bool(resolver.get("date_filter_applied"))
    result["star_filter_applied"] = bool(resolver.get("star_filter_applied"))
    result["selected_star_values_after"] = resolver.get("selected_star_values_after") or []
    result["list_update_observed"] = bool(resolver.get("list_update_observed"))
    result["dom_rows_collected"] = int(resolver.get("dom_rows_collected") or 0)
    result["search_used"] = bool(resolver.get("search_used"))
    result["scroll_used"] = bool(resolver.get("scroll_used"))
    result["row_menu_click"] = resolver.get("row_menu_click") or {}
    result["menu_labels"] = resolver.get("menu_labels") or []
    result["tab_used"] = str(resolver.get("tab_used") or "")
    result["locator_strategy"] = str(resolver.get("locator_strategy") or "")
    result["complaint_action_found"] = bool(resolver.get("complaint_action_found"))
    visible_row = resolver.get("resolved_row") if isinstance(resolver.get("resolved_row"), Mapping) else {}
    if not visible_row and resolver.get("actionable_row_found"):
        for attempt in resolver.get("attempts") or []:
            if attempt.get("actionable_row_found"):
                visible_row = attempt.get("resolved_row") or {}
                break
    result["tag_diagnostics"] = {
        "api_review_tags": normalize_review_tags(api_row.get("review_tags") or []),
        "ui_review_tags": normalize_review_tags((visible_row or {}).get("review_tags") or []),
        "combined_review_tags": normalize_review_tags([*(api_row.get("review_tags") or []), *((visible_row or {}).get("review_tags") or [])]),
    }
    if not resolver.get("actionable_row_found"):
        result["blocker"] = str(resolver.get("block_reason") or "Exact cursor match exists, but actionable DOM row was not found")
        return result
    assert_safe_click_label(ROW_MENU_COMPLAINT_LABEL, purpose="open_complaint_modal")
    action_click = click_open_row_menu_complaint_action(page)
    result["complaint_action_click"] = action_click
    if not action_click.get("ok"):
        result["blocker"] = str(action_click.get("reason") or "complaint action could not be clicked")
        _safe_escape(page)
        return result
    _wait_settle(page, 1500)
    modal_state = extract_complaint_modal_state(page)
    result["modal_opened"] = bool(modal_state.get("opened"))
    result["categories_found"] = modal_state.get("categories") or []
    result["submit_button_label"] = str(modal_state.get("submit_button_label") or "")
    result["submit_button_state_before_fill"] = submit_button_state(page, result["submit_button_label"])
    result["description_field_found"] = bool(modal_state.get("description_field_found"))
    result["validation_messages_before_click"] = modal_state.get("validation_hints") or []
    if detect_complaint_success_state(page).get("seen") and not result["modal_opened"]:
        result["blocker"] = "complaint action appears to create durable submitted state without modal"
        return result
    if not result["modal_opened"]:
        result["blocker"] = "complaint modal did not open"
        return result
    category = choose_complaint_category(
        result["categories_found"],
        force_other=False,
        preferred_category=(candidate.get("ai") or {}).get("category_label"),
    )
    result["selected_category"] = category
    if not category:
        result["blocker"] = "modal category could not be selected"
        result["close_method"] = close_modal_without_submit(page)
        return result
    category_click = click_complaint_category(page, category)
    result["category_click"] = category_click
    if not category_click.get("ok"):
        result["blocker"] = str(category_click.get("reason") or "category could not be selected")
        result["close_method"] = close_modal_without_submit(page)
        return result
    _wait_settle(page, 700)
    result["description_field_ready_after_category"] = wait_for_description_field_ready(page, timeout_ms=min(config.timeout_ms, 6000))
    if not is_reason_submit_ready(draft_text):
        result["blocker"] = "complaint text is empty or diagnostic placeholder"
        result["close_method"] = close_modal_without_submit(page)
        return result
    result["draft_text"] = draft_text
    fill_result = fill_description_field(page, draft_text)
    result["description_fill"] = fill_result
    result["description_field_found"] = bool(fill_result.get("ok"))
    result["modal_description_value_after_fill"] = str(fill_result.get("value_after_fill") or "")
    result["modal_description_value_after_blur"] = str(fill_result.get("value_after_blur") or "")
    result["modal_description_value_before_submit"] = str(fill_result.get("value_after_blur") or fill_result.get("value_after_fill") or "")
    result["description_value_match"] = bool(fill_result.get("value_match"))
    if not fill_result.get("ok"):
        result["blocker"] = str(fill_result.get("reason") or "description field unavailable")
        result["close_method"] = close_modal_without_submit(page)
        return result
    if not description_is_ready_for_submit(fill_result, draft_text):
        result["blocker"] = "description field value mismatch before final submit; submit blocked"
        result["close_method"] = close_modal_without_submit(page)
        return result
    after_fill_modal = extract_complaint_modal_state(page)
    result["submit_button_label"] = str(after_fill_modal.get("submit_button_label") or result["submit_button_label"])
    result["description_field_present_before_click"] = bool(after_fill_modal.get("description_field_found") or result["description_field_found"])
    result["submit_button_state_before_click"] = submit_button_state(page, result["submit_button_label"])
    result["validation_messages_before_click"] = after_fill_modal.get("validation_hints") or result.get("validation_messages_before_click") or []
    result["modal_state_before_click"] = {
        "modal_opened": bool(after_fill_modal.get("opened")),
        "modal_title": str(after_fill_modal.get("modal_title") or ""),
        "categories": after_fill_modal.get("categories") or [],
        "description_field_found": bool(after_fill_modal.get("description_field_found")),
        "description_value_match": bool(result.get("description_value_match")),
        "modal_description_value_before_submit": str(result.get("modal_description_value_before_submit") or ""),
        "submit_button_label": str(after_fill_modal.get("submit_button_label") or result["submit_button_label"]),
        "submit_button_enabled": bool((result.get("submit_button_state_before_click") or {}).get("enabled")),
        "validation_messages": list(result.get("validation_messages_before_click") or []),
    }
    if config.dry_run:
        result["blocker"] = "dry_run=1; final submit not clicked"
        result["close_method"] = close_modal_without_submit(page)
        result["modal_closed"] = True
        return result
    if not (result.get("submit_button_state_before_click") or {}).get("enabled", True):
        result["blocker"] = "final submit button is disabled before click"
        result["close_method"] = close_modal_without_submit(page)
        result["modal_closed"] = True
        return result
    captured_network: list[dict[str, Any]] = []
    captured_requests: list[dict[str, Any]] = []
    capture_state = {"enabled": True, "stage": "before_final_submit_click"}
    page.on(
        "request",
        lambda request: capture_submit_network_request(
            request,
            captured=captured_requests,
            stage=str(capture_state.get("stage") or "submit"),
            target_feedback_id=feedback_id,
            intended_description=draft_text,
        ),
    )
    page.on(
        "response",
        lambda response: capture_submit_network_response(
            response,
            captured=captured_network,
            stage=str(capture_state.get("stage") or "submit"),
            target_feedback_id=feedback_id,
        ),
    )
    result["final_submit_click_started_at"] = iso_now()
    capture_state["stage"] = "after_final_submit_click"
    submit_click = click_final_complaint_submit(page, result["submit_button_label"])
    result["final_submit_click_finished_at"] = iso_now()
    result["final_submit_click"] = submit_click
    result["submit_clicked"] = bool(submit_click.get("ok"))
    result["submit_clicked_count"] = 1 if submit_click.get("ok") else 0
    if not submit_click.get("ok"):
        result["blocker"] = str(submit_click.get("reason") or "final submit button could not be clicked")
        result["close_method"] = close_modal_without_submit(page)
        return result
    _wait_settle(page, 3500)
    capture_state["stage"] = "post_submit_readback"
    success_state = detect_complaint_success_state(page)
    result["success_state"] = success_state
    post_click_modal = extract_complaint_modal_state(page)
    result["modal_state_after_click"] = {
        "modal_opened": bool(post_click_modal.get("opened")),
        "validation_messages": post_click_modal.get("validation_hints") or [],
        "button_labels": post_click_modal.get("button_labels") or [],
    }
    result["visible_messages_after_click"] = extract_visible_submit_messages(page)
    result["complaint_status_after_submit"] = read_network_status_after_submit(config, page, feedback_id, request_headers)
    result["post_submit_row_state"] = inspect_post_submit_row_state(page, api_row, candidate)
    result["submit_network_capture"] = summarize_submit_network_capture(captured_network, captured_requests=captured_requests)
    result["submit_payload_has_description"] = (result["submit_network_capture"] or {}).get("submit_payload_has_description")
    result["submit_payload_description_length"] = int((result["submit_network_capture"] or {}).get("submit_payload_description_length") or 0)
    result["submit_result"] = classify_submit_result(result)
    submitted_like = result["submit_result"] == SUBMIT_RESULT_CONFIRMED_SUCCESS
    result["submit_success"] = submitted_like
    status = "waiting_response" if submitted_like else "error"
    result["journal_record"] = journal_record_for_submit(
        api_row,
        candidate,
        result,
        status=status,
        run_id=run_id,
    )
    create_result = journal.create_or_update(result["journal_record"], retry_errors=config.retry_errors)
    result["journal_write"] = {"created": create_result.created, "duplicate": create_result.duplicate, "complaint_id": create_result.record.get("complaint_id")}
    if not submitted_like:
        result["blocker"] = blocker_for_submit_result(result)
    if (result.get("modal_state_after_click") or {}).get("modal_opened"):
        result["close_method"] = close_modal_without_submit(page)
    result["modal_closed"] = True
    return result


def candidate_state_allows_next_attempt(result: Mapping[str, Any]) -> bool:
    if result.get("submit_clicked"):
        return False
    if not result.get("modal_opened"):
        return True
    if result.get("modal_closed") or result.get("close_method"):
        return True
    return False


def submit_button_state(page: Page, label: str = "") -> dict[str, Any]:
    try:
        return page.evaluate(
            r"""
(expectedLabel) => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const labelFor = (el) => (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').replace(/\s+/g, ' ').trim();
  const dialogs = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], [class*="modal"], [class*="Modal"], [class*="popup"], [class*="Popup"]')).filter(visible);
  const root = dialogs[dialogs.length - 1] || document.body;
  const expected = String(expectedLabel || '').trim().toLowerCase();
  const buttons = Array.from(root.querySelectorAll('button, [role="button"], input[type="submit"]')).filter(visible)
    .map((el) => ({
      el,
      label: labelFor(el),
      disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true' || el.getAttribute('disabled') !== null)
    }));
  const target = buttons.find((item) => {
    const lower = item.label.toLowerCase();
    if (!lower) return false;
    if (expected && lower === expected) return true;
    return /^(отправить|подать|подать жалобу|отправить жалобу|пожаловаться)$/i.test(item.label);
  });
  if (!target) return {found: false, enabled: false, label: '', visible_button_labels: buttons.map((item) => item.label).filter(Boolean).slice(0, 12)};
  const rect = target.el.getBoundingClientRect();
  return {
    found: true,
    enabled: !target.disabled,
    disabled: target.disabled,
    label: target.label,
    rect: {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)}
  };
}
            """,
            label,
        )
    except PlaywrightError as exc:
        return {"found": False, "enabled": False, "reason": safe_text(str(exc), 400)}


def extract_visible_submit_messages(page: Page) -> list[str]:
    try:
        payload = page.evaluate(
            r"""
() => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < window.innerHeight;
  };
  const roots = Array.from(document.querySelectorAll(
    '[role="status"], [role="alert"], [aria-live], [class*="Toast"], [class*="toast"], [class*="Notification"], [class*="notification"], [class*="Snackbar"], [class*="snackbar"], [role="dialog"], [aria-modal="true"]'
  )).filter(visible);
  return roots.map((root) => (root.innerText || '').replace(/\s+/g, ' ').trim()).filter(Boolean).slice(0, 40);
}
            """
        )
    except PlaywrightError:
        payload = []
    messages: list[str] = []
    for item in payload if isinstance(payload, list) else []:
        for line in str(item or "").split("\n"):
            text = safe_text(" ".join(line.split()), 300)
            if text and text not in messages:
                messages.append(text)
    return messages[:20]


def inspect_post_submit_row_state(
    page: Page,
    api_row: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "checked": False,
        "row_found": False,
        "row_disappeared_or_moved": "unknown",
        "complaint_action_still_visible": "unknown",
        "badge_or_hint": "",
        "menu_labels": [],
        "reason": "",
    }
    if (extract_complaint_modal_state(page) or {}).get("opened"):
        state["reason"] = "complaint modal still open after submit"
        return state
    try:
        visible_rows = extract_visible_feedback_rows(page, max_rows=20)
        expected_ui = (candidate.get("match") or {}).get("best_ui_candidate") or {}
        visible_match = find_visible_actionable_row(api_row, visible_rows, expected_ui=expected_ui)
        row = visible_match.get("row") if isinstance(visible_match.get("row"), dict) else {}
        state["checked"] = True
        state["visible_row_match"] = visible_match.get("match") or {}
        state["row_found"] = bool(row)
        state["row_disappeared_or_moved"] = False if row else True
        if not row:
            state["reason"] = "target row not visible in current feedbacks list after submit"
            return state
        state["badge_or_hint"] = safe_text(
            " ".join(
                str(row.get(key) or "")
                for key in ("complaint_status", "complaint_hint", "status_text", "row_text")
            ),
            500,
        )
        clicked_menu = _click_safe_row_menu(page, str(row.get("dom_scout_id") or ""))
        state["row_menu_click"] = clicked_menu
        if clicked_menu.get("ok"):
            _wait_settle(page, 500)
            menu_state = extract_open_row_menu_state(page)
            state["menu_labels"] = menu_state.get("items") or []
            state["complaint_action_still_visible"] = bool(menu_state.get("complaint_action_found"))
            _safe_escape(page)
        else:
            state["reason"] = str(clicked_menu.get("reason") or "row menu could not be opened after submit")
    except Exception as exc:  # pragma: no cover - live fallback
        state["reason"] = safe_text(str(exc), 500)
    return state


def click_final_complaint_submit(page: Page, label: str = "") -> dict[str, Any]:
    try:
        return page.evaluate(
            r"""
(expectedLabel) => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const labelFor = (el) => (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').replace(/\s+/g, ' ').trim();
  const dialogs = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], [class*="modal"], [class*="Modal"], [class*="popup"], [class*="Popup"]')).filter(visible);
  const root = dialogs[dialogs.length - 1] || document.body;
  const expected = String(expectedLabel || '').trim().toLowerCase();
  const buttons = Array.from(root.querySelectorAll('button, [role="button"], input[type="submit"]')).filter(visible)
    .map((el) => ({el, label: labelFor(el)}));
  const target = buttons.find((item) => {
    const lower = item.label.toLowerCase();
    if (!lower) return false;
    if (expected && lower === expected) return true;
    return /^(отправить|подать|подать жалобу|отправить жалобу|пожаловаться)$/i.test(item.label);
  });
  if (!target) return {ok: false, reason: 'final submit button not found', labels: buttons.map((item) => item.label).slice(0, 12)};
  const rect = target.el.getBoundingClientRect();
  target.el.click();
  return {ok: true, label: target.label, rect: {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)}};
}
            """,
            label,
        )
    except PlaywrightError as exc:
        return {"ok": False, "reason": safe_text(str(exc), 500)}


def read_network_status_after_submit(
    config: SubmitConfig,
    page: Page,
    feedback_id: str,
    request_headers: Mapping[str, str],
) -> dict[str, Any]:
    if not request_headers:
        return {"checked": False, "reason": "Seller Portal request headers were not captured"}
    replay_config = build_replay_config(to_dry_run_config(config), max_ui_rows=max(config.max_api_rows * 6, 60))
    from apps.seller_portal_feedbacks_matching_replay import collect_feedback_rows_from_seller_portal_network

    rows, stats = collect_feedback_rows_from_seller_portal_network(page, replay_config, request_headers=request_headers)
    for row in rows:
        if str(row.get("feedback_id") or "") == feedback_id:
            status = str(row.get("complaint_status") or "")
            return {
                "checked": True,
                "feedback_id": feedback_id,
                "complaint_status": status,
                "complaint_action_found": bool(row.get("complaint_action_found")),
                "submitted_like": status.lower() not in {"", "unknown", "none"},
            }
    return {"checked": True, "feedback_id": feedback_id, "reason": "feedback row not found after submit", "stats": stats}


def capture_submit_network_response(
    response: Response,
    *,
    captured: list[dict[str, Any]],
    stage: str,
    target_feedback_id: str,
) -> None:
    if len(captured) >= 80:
        return
    item = sanitize_submit_network_response(response, stage=stage, target_feedback_id=target_feedback_id)
    if item:
        captured.append(item)


def capture_submit_network_request(
    request: Request,
    *,
    captured: list[dict[str, Any]],
    stage: str,
    target_feedback_id: str,
    intended_description: str,
) -> None:
    if len(captured) >= 80:
        return
    item = sanitize_submit_network_request(
        request,
        stage=stage,
        target_feedback_id=target_feedback_id,
        intended_description=intended_description,
    )
    if item:
        captured.append(item)


def sanitize_submit_network_request(
    request: Request,
    *,
    stage: str,
    target_feedback_id: str,
    intended_description: str,
) -> dict[str, Any]:
    try:
        url = request.url
        method = str(request.method or "")
        post_data = request.post_data or ""
    except Exception:
        return {}
    if method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return {}
    relevant_url = bool(SUBMIT_RELEVANT_URL_RE.search(url))
    target_seen = bool(target_feedback_id and target_feedback_id in post_data)
    intended_seen = bool(intended_description and intended_description in post_data)
    payload: Any = None
    content_type = ""
    try:
        content_type = str(request.headers.get("content-type") or "")
    except Exception:
        content_type = ""
    if post_data:
        try:
            payload = json.loads(post_data)
        except Exception:
            payload = None
    body_summary = summarize_submit_request_body(payload, post_data, target_feedback_id=target_feedback_id, intended_description=intended_description)
    if not relevant_url and not target_seen and not intended_seen and not body_summary.get("has_description"):
        return {}
    split = urlsplit(url)
    query_keys = sorted(
        {
            key
            for key, _value in parse_qsl(split.query, keep_blank_values=True)
            if not FORBIDDEN_QUERY_KEY_RE.search(str(key))
        }
    )[:20]
    return {
        "stage": safe_text(stage, 80),
        "method": safe_text(method, 12),
        "url_path": safe_text(f"{split.scheme}://{split.netloc}{split.path}", 260),
        "query_keys": query_keys,
        "content_type": safe_text(content_type, 120),
        "complaint_like_url": relevant_url,
        "submit_api_like_url": bool(re.search(r"/feedbacks/complaints", split.path, re.IGNORECASE)),
        "body_present": bool(post_data),
        "target_feedback_id_seen": target_seen or bool(body_summary.get("target_feedback_id_seen")),
        "payload_shape": payload_shape(payload),
        "safe_body_summary": body_summary,
    }


def sanitize_submit_network_response(response: Response, *, stage: str, target_feedback_id: str) -> dict[str, Any]:
    try:
        url = response.url
        status = int(response.status)
        content_type = str(response.headers.get("content-type") or "")
        method = str(response.request.method or "")
    except Exception:
        return {}
    payload: Any = None
    payload_text = ""
    if "json" in content_type.lower():
        try:
            payload = response.json()
            payload_text = json.dumps(payload, ensure_ascii=False)[:180000]
        except Exception:
            payload = None
            payload_text = ""
    relevant_url = bool(SUBMIT_RELEVANT_URL_RE.search(url))
    target_seen = bool(target_feedback_id and target_feedback_id in payload_text)
    safe_body = extract_safe_submit_body(payload, target_feedback_id=target_feedback_id)
    if not relevant_url and not target_seen and not safe_body:
        return {}
    split = urlsplit(url)
    query_keys = sorted(
        {
            key
            for key, _value in parse_qsl(split.query, keep_blank_values=True)
            if not FORBIDDEN_QUERY_KEY_RE.search(str(key))
        }
    )[:20]
    return {
        "stage": safe_text(stage, 80),
        "method": safe_text(method, 12),
        "status": status,
        "url_path": safe_text(f"{split.scheme}://{split.netloc}{split.path}", 260),
        "query_keys": query_keys,
        "content_type": safe_text(content_type, 120),
        "complaint_like_url": relevant_url,
        "target_feedback_id_seen": target_seen,
        "payload_shape": payload_shape(payload),
        "safe_body": safe_body,
    }


def extract_safe_submit_body(payload: Any, *, target_feedback_id: str) -> list[dict[str, Any]]:
    safe_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for mapping in iter_submit_mappings(payload):
        safe = safe_submit_fact(mapping, target_feedback_id=target_feedback_id)
        if not safe:
            continue
        key = json.dumps(safe, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        safe_rows.append(safe)
        if len(safe_rows) >= 40:
            break
    return safe_rows


def iter_submit_mappings(payload: Any) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 7 or len(found) >= 120:
            return
        if isinstance(value, Mapping):
            if submit_mapping_is_relevant(value):
                found.append(value)
            for nested in value.values():
                if isinstance(nested, (Mapping, list)):
                    walk(nested, depth + 1)
        elif isinstance(value, list):
            for item in value[:240]:
                if isinstance(item, (Mapping, list)):
                    walk(item, depth + 1)

    walk(payload)
    return found


def submit_mapping_is_relevant(mapping: Mapping[str, Any]) -> bool:
    keys = {str(key).lower() for key in mapping.keys()}
    joined = " ".join(keys)
    if any(token in joined for token in ("feedback", "review", "complaint", "claim", "appeal", "reason", "status", "error", "message", "success")):
        return True
    text = " ".join(str(value)[:160] for value in mapping.values() if isinstance(value, (str, int, float, bool)))
    return bool(re.search(r"(жалоб|отзыв|успеш|ошиб|обяз|выберите|invalid|validation)", text, re.IGNORECASE))


def safe_submit_fact(mapping: Mapping[str, Any], *, target_feedback_id: str) -> dict[str, Any]:
    fact = {
        "complaint_id": safe_text(deep_find_value(mapping, ("complaintId", "complaint_id", "claimId", "claim_id", "appealId", "appeal_id")), 120),
        "feedback_id": safe_text(deep_find_value(mapping, ("feedbackId", "feedback_id", "feedbackID", "sellerPortalFeedbackId")), 120),
        "review_id": safe_text(deep_find_value(mapping, ("reviewId", "review_id", "reviewID")), 120),
        "status_text": safe_text(deep_find_value(mapping, ("status", "statusName", "state", "decision", "decisionText")), 160),
        "success": safe_text(deep_find_value(mapping, ("success", "ok", "isSuccess")), 20),
        "code": safe_text(deep_find_value(mapping, ("code", "errorCode", "statusCode")), 80),
        "message": safe_text(deep_find_value(mapping, ("message", "error", "errorText", "title", "description")), 320),
        "reason": safe_text(deep_find_value(mapping, ("reason", "reasonName", "category", "categoryName")), 180),
    }
    if target_feedback_id and target_feedback_id in {fact.get("feedback_id"), fact.get("review_id")}:
        fact["target_feedback_id_match"] = True
    return {key: value for key, value in fact.items() if value not in ("", None)}


def deep_find_value(value: Any, names: tuple[str, ...]) -> str:
    wanted = {name.lower() for name in names}
    stack: list[Any] = [value]
    while stack:
        current = stack.pop(0)
        if isinstance(current, Mapping):
            for key, item in current.items():
                key_text = str(key)
                if key_text.lower() in wanted and isinstance(item, (str, int, float, bool)):
                    return str(item)
                if isinstance(item, (Mapping, list)):
                    stack.append(item)
        elif isinstance(current, list):
            stack.extend(item for item in current[:50] if isinstance(item, (Mapping, list)))
    return ""


def payload_shape(payload: Any) -> dict[str, Any]:
    if isinstance(payload, Mapping):
        return {
            "type": "dict",
            "keys": sorted(str(key) for key in payload.keys() if not FORBIDDEN_QUERY_KEY_RE.search(str(key)))[:30],
        }
    if isinstance(payload, list):
        return {"type": "list", "length": len(payload)}
    if payload is None:
        return {"type": "none"}
    return {"type": type(payload).__name__}


def summarize_submit_request_body(
    payload: Any,
    post_data: str,
    *,
    target_feedback_id: str,
    intended_description: str,
) -> dict[str, Any]:
    intended = str(intended_description or "").strip()
    raw = str(post_data or "")
    description_values: list[dict[str, Any]] = []
    safe_keys: set[str] = set()

    def walk(value: Any, path: tuple[str, ...] = (), depth: int = 0) -> None:
        if depth > 7 or len(description_values) >= 12:
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                key_text = str(key)
                if not FORBIDDEN_QUERY_KEY_RE.search(key_text):
                    safe_keys.add(key_text)
                next_path = (*path, key_text)
                if isinstance(item, str) and DESCRIPTION_BODY_KEY_RE.search(key_text):
                    text = item.strip()
                    if text:
                        description_values.append(
                            {
                                "key_path": ".".join(next_path[-4:]),
                                "length": len(text),
                                "snippet": safe_text(text, 180),
                                "matches_intended": bool(intended and normalize_text(text) == normalize_text(intended)),
                            }
                        )
                elif isinstance(item, (Mapping, list)):
                    walk(item, next_path, depth + 1)
        elif isinstance(value, list):
            for index, item in enumerate(value[:160]):
                if isinstance(item, (Mapping, list)):
                    walk(item, (*path, str(index)), depth + 1)

    walk(payload)
    intended_seen = bool(intended and intended in raw)
    best = sorted(description_values, key=lambda item: (bool(item.get("matches_intended")), int(item.get("length") or 0)), reverse=True)
    selected = best[0] if best else {}
    has_description = bool(intended_seen or description_values)
    description_length = int(selected.get("length") or (len(intended) if intended_seen else 0))
    description_snippet = str(selected.get("snippet") or (safe_text(intended, 180) if intended_seen else ""))
    return {
        "has_description": has_description,
        "intended_description_seen": intended_seen or any(bool(item.get("matches_intended")) for item in description_values),
        "description_length": description_length,
        "description_snippet": description_snippet,
        "description_key_path": safe_text(str(selected.get("key_path") or ""), 160),
        "description_candidates": best[:4],
        "target_feedback_id_seen": bool(target_feedback_id and target_feedback_id in raw),
        "safe_body_keys": sorted(safe_keys)[:30],
    }


def summarize_submit_network_capture(
    captured: list[Mapping[str, Any]],
    *,
    captured_requests: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    captured_requests = captured_requests or []
    endpoints: dict[str, dict[str, Any]] = {}
    safe_body_count = 0
    for item in captured:
        path = str(item.get("url_path") or "")
        endpoint = endpoints.setdefault(
            path,
            {
                "url_path": path,
                "methods": [],
                "statuses": [],
                "stages": [],
                "response_count": 0,
                "safe_body_count": 0,
                "complaint_like_url": False,
                "target_feedback_id_seen": False,
            },
        )
        endpoint["response_count"] += 1
        endpoint["complaint_like_url"] = bool(endpoint["complaint_like_url"] or item.get("complaint_like_url"))
        endpoint["target_feedback_id_seen"] = bool(endpoint["target_feedback_id_seen"] or item.get("target_feedback_id_seen"))
        body_count = len(item.get("safe_body") or [])
        safe_body_count += body_count
        endpoint["safe_body_count"] += body_count
        for key, value in (("methods", item.get("method")), ("statuses", item.get("status")), ("stages", item.get("stage"))):
            if value not in endpoint[key]:
                endpoint[key].append(value)
    safe_bodies = [row for item in captured for row in (item.get("safe_body") or []) if isinstance(row, Mapping)]
    mutating = [
        item
        for item in captured
        if str(item.get("method") or "").upper() in {"POST", "PUT", "PATCH", "DELETE"} and item.get("complaint_like_url")
    ]
    request_mutating = [
        item
        for item in captured_requests
        if str(item.get("method") or "").upper() in {"POST", "PUT", "PATCH", "DELETE"} and item.get("complaint_like_url")
    ]
    request_body_summaries = [
        item.get("safe_body_summary")
        for item in request_mutating
        if item.get("body_present")
        and item.get("submit_api_like_url")
        and isinstance(item.get("safe_body_summary"), Mapping)
    ]
    payload_checked = bool(request_body_summaries)
    has_description = any(bool(summary.get("has_description")) for summary in request_body_summaries)
    description_lengths = [int(summary.get("description_length") or 0) for summary in request_body_summaries]
    description_snippet = next((str(summary.get("description_snippet") or "") for summary in request_body_summaries if summary.get("description_snippet")), "")
    return {
        "responses": [dict(item) for item in captured],
        "requests": [dict(item) for item in captured_requests],
        "endpoints_observed": list(endpoints.values()),
        "response_count": len(captured),
        "request_count": len(captured_requests),
        "safe_body_count": safe_body_count,
        "mutating_response_count": len(mutating),
        "mutating_request_count": len(request_mutating),
        "mutating_statuses": [int(item.get("status") or 0) for item in mutating],
        "direct_feedback_id_found": any(row.get("target_feedback_id_match") for row in safe_bodies),
        "complaint_id_found": any(bool(row.get("complaint_id")) for row in safe_bodies),
        "success_text_seen": any(SUCCESS_TEXT_RE.search(" ".join(str(value) for value in row.values())) for row in safe_bodies),
        "validation_text_seen": any(VALIDATION_TEXT_RE.search(" ".join(str(value) for value in row.values())) for row in safe_bodies),
        "submit_payload_checked": payload_checked,
        "submit_payload_has_description": has_description if payload_checked else "unknown",
        "submit_payload_intended_description_seen": any(bool(summary.get("intended_description_seen")) for summary in request_body_summaries),
        "submit_payload_description_length": max(description_lengths) if description_lengths else 0,
        "submit_payload_description_snippet": safe_text(description_snippet, 180),
    }


def classify_submit_result(result: Mapping[str, Any]) -> str:
    network = result.get("submit_network_capture") if isinstance(result.get("submit_network_capture"), Mapping) else {}
    post_status = result.get("complaint_status_after_submit") if isinstance(result.get("complaint_status_after_submit"), Mapping) else {}
    row_state = result.get("post_submit_row_state") if isinstance(result.get("post_submit_row_state"), Mapping) else {}
    messages = [str(item or "") for item in result.get("visible_messages_after_click") or []]
    modal_after = result.get("modal_state_after_click") if isinstance(result.get("modal_state_after_click"), Mapping) else {}
    validation_messages = messages + [str(item or "") for item in modal_after.get("validation_messages") or []]
    mutating_statuses = [int(status or 0) for status in network.get("mutating_statuses") or []]
    has_mutating_2xx = any(200 <= status < 300 for status in mutating_statuses)
    has_mutating_4xx = any(400 <= status < 500 for status in mutating_statuses)
    has_mutating_5xx = any(status >= 500 for status in mutating_statuses)
    if has_mutating_5xx:
        return SUBMIT_RESULT_CONFIRMED_NETWORK_ERROR
    if has_mutating_4xx or network.get("validation_text_seen") or any(VALIDATION_TEXT_RE.search(text) for text in validation_messages):
        return SUBMIT_RESULT_CONFIRMED_VALIDATION_ERROR
    if network.get("submit_payload_checked") and network.get("submit_payload_has_description") is False:
        return SUBMIT_RESULT_UNCONFIRMED_AFTER_CLICK
    if (
        (result.get("success_state") or {}).get("seen")
        or post_status.get("submitted_like")
        or row_state.get("complaint_action_still_visible") is False
        or network.get("complaint_id_found")
        or network.get("success_text_seen")
        or (has_mutating_2xx and network.get("direct_feedback_id_found"))
    ):
        return SUBMIT_RESULT_CONFIRMED_SUCCESS
    return SUBMIT_RESULT_UNCONFIRMED_AFTER_CLICK


def blocker_for_submit_result(result: Mapping[str, Any]) -> str:
    submit_result = str(result.get("submit_result") or "")
    if submit_result == SUBMIT_RESULT_CONFIRMED_VALIDATION_ERROR:
        return "submit clicked once; WB returned or displayed validation error"
    if submit_result == SUBMIT_RESULT_CONFIRMED_NETWORK_ERROR:
        return "submit clicked once; WB submit network response failed"
    return "submit clicked once, but success was not confirmed by network/toast/post-row evidence"


def compact_network_evidence(network: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "response_count": int(network.get("response_count") or 0),
        "request_count": int(network.get("request_count") or 0),
        "mutating_response_count": int(network.get("mutating_response_count") or 0),
        "mutating_request_count": int(network.get("mutating_request_count") or 0),
        "mutating_statuses": [int(item or 0) for item in network.get("mutating_statuses") or []][:8],
        "direct_feedback_id_found": bool(network.get("direct_feedback_id_found")),
        "complaint_id_found": bool(network.get("complaint_id_found")),
        "success_text_seen": bool(network.get("success_text_seen")),
        "validation_text_seen": bool(network.get("validation_text_seen")),
        "submit_payload_checked": bool(network.get("submit_payload_checked")),
        "submit_payload_has_description": network.get("submit_payload_has_description", "unknown"),
        "submit_payload_intended_description_seen": bool(network.get("submit_payload_intended_description_seen")),
        "submit_payload_description_length": int(network.get("submit_payload_description_length") or 0),
        "submit_payload_description_snippet": safe_text(str(network.get("submit_payload_description_snippet") or ""), 180),
        "endpoints_observed": [
            {
                "url_path": safe_text(str(item.get("url_path") or ""), 260),
                "methods": [str(value) for value in item.get("methods") or []][:6],
                "statuses": [int(value or 0) for value in item.get("statuses") or []][:8],
                "response_count": int(item.get("response_count") or 0),
            }
            for item in network.get("endpoints_observed") or []
            if isinstance(item, Mapping)
        ][:8],
    }


def compact_ui_evidence(modal: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "submit_button_label": safe_text(str(modal.get("submit_button_label") or ""), 80),
        "submit_button_enabled": bool((modal.get("submit_button_state_before_click") or {}).get("enabled")),
        "description_field_present_before_click": bool(modal.get("description_field_present_before_click")),
        "description_value_match": bool(modal.get("description_value_match")),
        "modal_description_value_before_submit_length": len(str(modal.get("modal_description_value_before_submit") or "")),
        "validation_messages_before_click": [safe_text(str(item), 220) for item in modal.get("validation_messages_before_click") or []][:8],
        "visible_messages_after_click": [safe_text(str(item), 220) for item in modal.get("visible_messages_after_click") or []][:8],
        "modal_open_after_click": bool((modal.get("modal_state_after_click") or {}).get("modal_opened")),
        "success_toast_text": safe_text(str((modal.get("success_state") or {}).get("text") or ""), 260),
    }


def compact_post_submit_row_state(row_state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "checked": bool(row_state.get("checked")),
        "row_found": bool(row_state.get("row_found")),
        "row_disappeared_or_moved": row_state.get("row_disappeared_or_moved"),
        "complaint_action_still_visible": row_state.get("complaint_action_still_visible"),
        "badge_or_hint": safe_text(str(row_state.get("badge_or_hint") or ""), 300),
        "reason": safe_text(str(row_state.get("reason") or ""), 300),
    }


def journal_record_for_submit(
    api_row: Mapping[str, Any],
    candidate: Mapping[str, Any],
    modal: Mapping[str, Any],
    *,
    status: str,
    run_id: str,
) -> dict[str, Any]:
    ai = candidate.get("ai") or {}
    match = candidate.get("match") or {}
    resolver_match = modal.get("visible_row_match") if isinstance(modal.get("visible_row_match"), Mapping) else {}
    effective_match = resolver_match if resolver_match.get("match_status") == "exact" else match
    effective_ui = (
        effective_match.get("best_ui_candidate")
        if isinstance(effective_match.get("best_ui_candidate"), Mapping)
        else {}
    )
    network = modal.get("submit_network_capture") or {}
    description_evidence = description_persistence_result(modal.get("draft_text") or "", "", observed=False)
    return {
        "complaint_id": str(uuid4()),
        "feedback_id": str(api_row.get("feedback_id") or candidate.get("feedback_id") or ""),
        "submitted_at": iso_now(),
        "last_status_checked_at": "",
        "complaint_status": status,
        "wb_category_label": str(modal.get("selected_category") or ""),
        "complaint_text": str(modal.get("draft_text") or ""),
        "wb_complaint_row_fingerprint": str(effective_ui.get("row_text_fingerprint") or ""),
        "seller_portal_feedback_id": str(effective_ui.get("seller_portal_feedback_id") or api_row.get("feedback_id") or ""),
        "match_status": str(effective_match.get("match_status") or ""),
        "match_score": str(effective_match.get("match_score") or ""),
        "rating": str(api_row.get("product_valuation") or api_row.get("rating") or ""),
        "review_created_at": str(api_row.get("created_at") or ""),
        "nm_id": str(api_row.get("nm_id") or ""),
        "supplier_article": str(api_row.get("supplier_article") or ""),
        "product_name": str(api_row.get("product_name") or ""),
        "review_text": str(api_row.get("text") or ""),
        "review_tags": normalize_review_tags(api_row.get("review_tags") or []),
        "tag_source": str(api_row.get("tag_source") or ""),
        "ui_review_tags": normalize_review_tags((effective_ui or {}).get("review_tags") or []),
        "submit_tag_diagnostics": modal.get("tag_diagnostics") or candidate.get("tag_diagnostics") or {},
        "pros": str(api_row.get("pros") or ""),
        "cons": str(api_row.get("cons") or ""),
        "is_answered": bool(api_row.get("is_answered")),
        "answer_text": str(api_row.get("answer_text") or ""),
        "photo_count": int(api_row.get("photo_count") or 0),
        "video_count": int(api_row.get("video_count") or 0),
        "ai_complaint_fit": str(ai.get("complaint_fit") or ""),
        "ai_complaint_fit_label": str(ai.get("complaint_fit_label") or ""),
        "ai_category_label": str(ai.get("category_label") or ""),
        "ai_reason": str(ai.get("reason") or ""),
        "ai_confidence": str(ai.get("confidence") or ""),
        "ai_confidence_label": str(ai.get("confidence_label") or ""),
        "submit_run_id": run_id,
        "submit_clicked_count": int(modal.get("submit_clicked_count") or (1 if modal.get("submit_clicked") else 0)),
        "submit_result": str(modal.get("submit_result") or ""),
        "submit_network_evidence_summary": compact_network_evidence(modal.get("submit_network_capture") or {}),
        "submit_ui_evidence_summary": compact_ui_evidence(modal),
        "post_submit_row_state": compact_post_submit_row_state(modal.get("post_submit_row_state") or {}),
        "modal_description_value_before_submit": str(modal.get("modal_description_value_before_submit") or ""),
        "submit_payload_has_description": network.get("submit_payload_has_description", "unknown"),
        "submit_payload_description_length": int(network.get("submit_payload_description_length") or 0),
        "submit_payload_description_snippet": safe_text(str(network.get("submit_payload_description_snippet") or ""), 180),
        "post_submit_wb_description_text": description_evidence["post_submit_wb_description_text"],
        "description_persisted": description_evidence["description_persisted"],
        "last_error": "" if status == "waiting_response" else str(modal.get("blocker") or "submit success not confirmed"),
        "raw_status_text": str((modal.get("success_state") or {}).get("text") or ""),
        "wb_decision_text": "",
    }


def apply_modal_submit_results(candidates: list[dict[str, Any]], modal_results: list[Mapping[str, Any]]) -> None:
    by_id = {str(item.get("feedback_id") or ""): dict(item) for item in modal_results}
    for candidate in candidates:
        feedback_id = str(candidate.get("feedback_id") or "")
        if feedback_id in by_id:
            candidate["modal"] = by_id[feedback_id]
            if by_id[feedback_id].get("blocker") and not by_id[feedback_id].get("submit_success"):
                candidate["skip_reason"] = str(by_id[feedback_id].get("blocker") or "")


def build_submit_aggregate(candidates: list[Mapping[str, Any]]) -> dict[str, Any]:
    selected = [item for item in candidates if item.get("selected_for_dry_run")]
    modal = [item.get("modal") or {} for item in candidates]
    submit_clicked = [item for item in modal if item.get("submit_clicked")]
    submitted = [item for item in modal if item.get("submit_success")]
    skipped = Counter(str(item.get("skip_reason") or "") for item in candidates if item.get("skip_reason"))
    ai_counts = Counter(str((item.get("ai") or {}).get("complaint_fit") or "unknown") for item in candidates)
    submit_results = Counter(str(item.get("submit_result") or "") for item in modal if item.get("submit_result"))
    return {
        "api_rows_loaded": len(candidates),
        "ai_analyzed_count": sum(1 for item in candidates if (item.get("ai") or {}).get("complaint_fit")),
        "ai_yes_count": ai_counts.get("yes", 0),
        "ai_review_count": ai_counts.get("review", 0),
        "ai_no_count": ai_counts.get("no", 0),
        "candidates_selected": len(selected),
        "exact_matched": sum(1 for item in selected if (item.get("match") or {}).get("match_status") == "exact"),
        "submitted_count": len(submitted),
        "submit_clicked_count": len(submit_clicked),
        "error_count": sum(1 for item in modal if item.get("submit_clicked") and not item.get("submit_success")),
        "submit_result_counts": dict(submit_results),
        "skipped_existing_duplicates": sum(count for reason, count in skipped.items() if "already exists" in reason),
        "skipped_denied_feedback_ids": sum(count for reason, count in skipped.items() if "hard-denylisted" in reason),
        "skipped_reasons": [{"reason": reason, "count": count} for reason, count in skipped.most_common(12) if reason],
    }


def determine_final_conclusion(candidates: list[Mapping[str, Any]], errors: list[Mapping[str, Any]]) -> str:
    modal_results = [item.get("modal") or {} for item in candidates]
    submit_results = [str(item.get("submit_result") or "") for item in modal_results if item.get("submit_clicked")]
    if SUBMIT_RESULT_CONFIRMED_SUCCESS in submit_results:
        return "submitted_confirmed_waiting_response"
    if SUBMIT_RESULT_CONFIRMED_VALIDATION_ERROR in submit_results:
        return "submit_failed_validation"
    if SUBMIT_RESULT_CONFIRMED_NETWORK_ERROR in submit_results:
        return "submit_failed_network"
    if SUBMIT_RESULT_UNCONFIRMED_AFTER_CLICK in submit_results:
        return "submit_unconfirmed_error"
    if errors:
        return "no_safe_candidate"
    if not any(item.get("selected_for_dry_run") and not item.get("skip_reason") for item in candidates):
        return "no_safe_candidate"
    return "no_submit_clicked"


def is_reason_submit_ready(reason: str) -> bool:
    text = " ".join(str(reason or "").strip().lower().split())
    if len(text) < 12:
        return False
    return not any(phrase in text for phrase in BAD_REASON_PHRASES)


def normalize_deny_feedback_ids(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    ids: list[str] = []
    for raw in list(DEFAULT_DENY_FEEDBACK_IDS) + list(values or []):
        for part in str(raw or "").split(","):
            normalized = part.strip()
            if normalized and normalized not in ids:
                ids.append(normalized)
    return tuple(ids)


def _candidate_by_id(candidates: list[dict[str, Any]], feedback_id: str) -> dict[str, Any]:
    for candidate in candidates:
        if str(candidate.get("feedback_id") or "") == feedback_id:
            return candidate
    return {}


def to_dry_run_config(config: SubmitConfig) -> DryRunConfig:
    return DryRunConfig(
        date_from=config.date_from,
        date_to=config.date_to,
        stars=config.stars,
        is_answered=config.is_answered,
        max_api_rows=config.max_api_rows,
        max_ai_candidates=config.max_submit,
        force_category_other=False,
        mode=NO_SUBMIT_MODE,
        runtime_dir=config.runtime_dir,
        storage_state_path=config.storage_state_path,
        wb_bot_python=config.wb_bot_python,
        output_dir=config.output_dir,
        start_url=config.start_url,
        headless=config.headless,
        timeout_ms=config.timeout_ms,
        write_artifacts=False,
        deny_feedback_ids=config.deny_feedback_ids,
    )


def render_markdown_report(report: Mapping[str, Any]) -> str:
    params = report.get("parameters") or {}
    agg = report.get("aggregate") or {}
    lines = [
        "# Seller Portal Complaint Submit",
        "",
        f"- Started: `{report.get('started_at')}`",
        f"- Finished: `{report.get('finished_at')}`",
        f"- Dry run: `{params.get('dry_run')}`",
        f"- Explicit submit flag: `{params.get('explicit_submit_flag')}`",
        f"- Range: `{params.get('date_from')}`..`{params.get('date_to')}`",
        f"- Stars: `{','.join(str(item) for item in params.get('stars') or [])}`",
        f"- Submit clicked: `{agg.get('submit_clicked_count', 0)}`",
        f"- Submitted: `{agg.get('submitted_count', 0)}`",
        f"- Errors: `{agg.get('error_count', 0)}`",
        f"- Final conclusion: `{report.get('final_conclusion')}`",
        "",
        "## Candidates",
        "",
    ]
    for candidate in report.get("candidates") or []:
        api = candidate.get("api_summary") or summarize_api_row({})
        ai = candidate.get("ai") or {}
        match = candidate.get("match") or {}
        modal = candidate.get("modal") or {}
        lines.extend(
            [
                f"- `{candidate.get('feedback_id')}` fit `{ai.get('complaint_fit')}` match `{match.get('match_status')}` submitted `{modal.get('submit_success')}` clicked `{modal.get('submit_clicked')}`",
                f"  API: `{api.get('created_at')}` rating `{api.get('rating')}` nm `{api.get('nm_id')}` article `{api.get('supplier_article')}` text `{api.get('review_text')}`",
                f"  Complaint: category `{modal.get('selected_category')}` text `{modal.get('draft_text')}`",
                f"  Description: match `{modal.get('description_value_match')}` before-submit length `{len(str(modal.get('modal_description_value_before_submit') or ''))}` payload `{(modal.get('submit_network_capture') or {}).get('submit_payload_has_description', 'unknown')}` length `{(modal.get('submit_network_capture') or {}).get('submit_payload_description_length', 0)}`",
                f"  Evidence: result `{modal.get('submit_result')}` button `{modal.get('submit_button_label')}` network `{compact_network_evidence(modal.get('submit_network_capture') or {})}`",
                f"  Skip/blocker: `{candidate.get('skip_reason') or modal.get('blocker') or ''}`",
            ]
        )
    if report.get("errors"):
        lines.extend(["", "## Errors", ""])
        for error in report["errors"]:
            lines.append(f"- `{error.get('stage')}` / `{error.get('code')}`: {error.get('message')}")
    return "\n".join(lines) + "\n"


def write_report_artifacts(report: dict[str, Any], output_root: Path) -> dict[str, Path]:
    run_dir = output_root / str(report.get("run_id") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    run_dir.mkdir(parents=True, exist_ok=True)
    json_path = run_dir / "seller_portal_feedbacks_complaint_submit.json"
    md_path = run_dir / "seller_portal_feedbacks_complaint_submit.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown_report(report), encoding="utf-8")
    return {"json": json_path, "markdown": md_path}


def compact_stdout_report(report: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "contract_name": report.get("contract_name"),
        "run_id": report.get("run_id"),
        "started_at": report.get("started_at"),
        "finished_at": report.get("finished_at"),
        "parameters": report.get("parameters"),
        "safety": report.get("safety"),
        "aggregate": report.get("aggregate"),
        "final_conclusion": report.get("final_conclusion"),
        "artifact_paths": report.get("artifact_paths"),
        "errors": report.get("errors"),
    }


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    main()
