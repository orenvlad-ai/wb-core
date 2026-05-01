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
import sys
from typing import Any, Mapping
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import Error as PlaywrightError, Page, sync_playwright  # noqa: E402

from apps.seller_portal_feedbacks_complaint_dry_run_plan import (  # noqa: E402
    DryRunConfig,
    analyze_feedback_rows,
    apply_article_search_for_candidate,
    apply_exact_matches,
    build_candidate_records,
    build_draft_text,
    build_replay_config,
    build_scout_config,
    choose_complaint_category,
    click_complaint_category,
    collect_matching_rows,
    empty_modal_candidate_state,
    fill_description_field,
    find_visible_actionable_row,
    load_api_feedback_rows,
    normalize_requested_date,
    select_ai_candidate_ids,
    should_open_modal_for_match,
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
from packages.application.sheet_vitrina_v1_feedbacks_complaints import (  # noqa: E402
    JsonFileFeedbacksComplaintJournal,
)


CONTRACT_NAME = "seller_portal_feedbacks_complaint_submit"
CONTRACT_VERSION = "controlled_submit_v1"
DEFAULT_RUNTIME_DIR = Path(os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR", "/opt/wb-core-runtime/state"))
DEFAULT_OUTPUT_ROOT = Path("/opt/wb-core-runtime/state/feedbacks_complaint_submit")
LOCAL_OUTPUT_ROOT = Path("artifacts/seller_portal_feedbacks_complaint_submit")
MAX_SUBMIT_HARD_CAP = 3
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
        },
        "safety": {
            "hard_max_submit": MAX_SUBMIT_HARD_CAP,
            "real_submit_enabled": bool(not config.dry_run and config.submit_confirmation),
            "exact_match_required": config.require_exact,
            "non_exact_submit_allowed": False,
            "complaint_submit_route_exposed": False,
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
        report["finished_at"] = iso_now()
        return report

    ai_report = analyze_feedback_rows(dry_config, api_rows)
    report["ai"] = ai_report
    if not ai_report.get("success"):
        report["errors"].append({"stage": "ai_analyze", "code": str(ai_report.get("error_code") or ""), "message": str(ai_report.get("blocker") or "")})
        report["aggregate"] = build_submit_aggregate([])
        report["finished_at"] = iso_now()
        return report

    analysis_by_id = {str(item.get("feedback_id") or ""): item for item in ai_report.get("results") or []}
    selected_ids = select_submit_candidate_ids(list(analysis_by_id.values()), max_submit=config.max_submit, include_review=config.include_review)
    candidates = build_candidate_records(api_rows, analysis_by_id, selected_ids)
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
            if candidate.get("selected_for_dry_run")
            and not candidate.get("skip_reason")
            and should_open_modal_for_match(candidate.get("match") or {})
        ][: config.max_submit]
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
    report["finished_at"] = iso_now()
    return report


def select_submit_candidate_ids(results: list[Mapping[str, Any]], *, max_submit: int, include_review: bool) -> list[str]:
    if include_review:
        return select_ai_candidate_ids(results, max_candidates=max_submit)
    selected: list[str] = []
    for result in results:
        feedback_id = str(result.get("feedback_id") or "")
        if result.get("complaint_fit") == "yes" and feedback_id:
            selected.append(feedback_id)
            if len(selected) >= max_submit:
                break
    return selected


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
        match = candidate.get("match") or {}
        if not should_open_modal_for_match(match):
            candidate["skip_reason"] = f"match_status={match.get('match_status')} is not exact; submit blocked"


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
                for candidate in submit_candidates:
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
                    if result.get("submit_clicked") and not config.dry_run:
                        _wait_settle(page, 1800)
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
    result.update({"feedback_id": feedback_id, "dry_run": config.dry_run, "submit_clicked": False, "submit_success": False})
    if not api_row:
        result["blocker"] = "API row unavailable for submit"
        return result
    visible_rows = extract_visible_feedback_rows(page, max_rows=20)
    expected_ui = (candidate.get("match") or {}).get("best_ui_candidate") or {}
    visible_match = find_visible_actionable_row(api_row, visible_rows, expected_ui=expected_ui)
    if not visible_match.get("row"):
        search_result = apply_article_search_for_candidate(page, api_row, expected_ui=expected_ui)
        result["targeted_search"] = search_result
        if search_result.get("ok"):
            _wait_settle(page, 2500)
            visible_rows = extract_visible_feedback_rows(page, max_rows=20)
            visible_match = find_visible_actionable_row(api_row, visible_rows, expected_ui=expected_ui)
    result["visible_row_match"] = visible_match.get("match") or {}
    visible_row = visible_match.get("row") if isinstance(visible_match.get("row"), dict) else {}
    if not visible_row:
        result["blocker"] = "Exact cursor match exists, but actionable DOM row was not found"
        return result
    clicked_menu = _click_safe_row_menu(page, str(visible_row.get("dom_scout_id") or ""))
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
        result["blocker"] = str(action_click.get("reason") or "complaint action could not be clicked")
        _safe_escape(page)
        return result
    _wait_settle(page, 1500)
    modal_state = extract_complaint_modal_state(page)
    result["modal_opened"] = bool(modal_state.get("opened"))
    result["categories_found"] = modal_state.get("categories") or []
    result["submit_button_label"] = str(modal_state.get("submit_button_label") or "")
    result["description_field_found"] = bool(modal_state.get("description_field_found"))
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
    draft_text = build_draft_text((candidate.get("ai") or {}))
    if not is_reason_submit_ready(draft_text):
        result["blocker"] = "complaint text is empty or diagnostic placeholder"
        result["close_method"] = close_modal_without_submit(page)
        return result
    result["draft_text"] = draft_text
    fill_result = fill_description_field(page, draft_text)
    result["description_fill"] = fill_result
    result["description_field_found"] = bool(fill_result.get("ok"))
    if not fill_result.get("ok"):
        result["blocker"] = str(fill_result.get("reason") or "description field unavailable")
        result["close_method"] = close_modal_without_submit(page)
        return result
    after_fill_modal = extract_complaint_modal_state(page)
    result["submit_button_label"] = str(after_fill_modal.get("submit_button_label") or result["submit_button_label"])
    if config.dry_run:
        result["blocker"] = "dry_run=1; final submit not clicked"
        result["close_method"] = close_modal_without_submit(page)
        result["modal_closed"] = True
        return result
    submit_click = click_final_complaint_submit(page, result["submit_button_label"])
    result["final_submit_click"] = submit_click
    result["submit_clicked"] = bool(submit_click.get("ok"))
    if not submit_click.get("ok"):
        result["blocker"] = str(submit_click.get("reason") or "final submit button could not be clicked")
        result["close_method"] = close_modal_without_submit(page)
        return result
    _wait_settle(page, 3500)
    success_state = detect_complaint_success_state(page)
    result["success_state"] = success_state
    result["complaint_status_after_submit"] = read_network_status_after_submit(config, page, feedback_id, request_headers)
    submitted_like = bool(success_state.get("seen") or (result["complaint_status_after_submit"] or {}).get("submitted_like"))
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
        result["blocker"] = "submit clicked, but success/submitted state was not confirmed"
    if result["modal_opened"]:
        result["close_method"] = close_modal_without_submit(page)
    result["modal_closed"] = True
    return result


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
    return {
        "complaint_id": str(uuid4()),
        "feedback_id": str(api_row.get("feedback_id") or candidate.get("feedback_id") or ""),
        "submitted_at": iso_now(),
        "last_status_checked_at": "",
        "complaint_status": status,
        "wb_category_label": str(modal.get("selected_category") or ""),
        "complaint_text": str(modal.get("draft_text") or ""),
        "wb_complaint_row_fingerprint": str((match.get("best_ui_candidate") or {}).get("row_text_fingerprint") or ""),
        "seller_portal_feedback_id": str((match.get("best_ui_candidate") or {}).get("seller_portal_feedback_id") or api_row.get("feedback_id") or ""),
        "match_status": str(match.get("match_status") or ""),
        "match_score": str(match.get("match_score") or ""),
        "rating": str(api_row.get("product_valuation") or api_row.get("rating") or ""),
        "review_created_at": str(api_row.get("created_at") or ""),
        "nm_id": str(api_row.get("nm_id") or ""),
        "supplier_article": str(api_row.get("supplier_article") or ""),
        "product_name": str(api_row.get("product_name") or ""),
        "review_text": str(api_row.get("text") or ""),
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
        "skipped_existing_duplicates": sum(count for reason, count in skipped.items() if "already exists" in reason),
        "skipped_reasons": [{"reason": reason, "count": count} for reason, count in skipped.most_common(12) if reason],
    }


def is_reason_submit_ready(reason: str) -> bool:
    text = " ".join(str(reason or "").strip().lower().split())
    if len(text) < 12:
        return False
    return not any(phrase in text for phrase in BAD_REASON_PHRASES)


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
        "aggregate": report.get("aggregate"),
        "artifact_paths": report.get("artifact_paths"),
        "errors": report.get("errors"),
    }


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    main()
