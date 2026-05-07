"""Bounded day-by-day Seller Portal feedback complaint batch runner.

The runner delegates every real Seller Portal write to
seller_portal_feedbacks_complaint_submit.py, preserving its exact-match,
actionable DOM row, description and max-submit gates. This wrapper only
coordinates daily runs and builds an operator funnel.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.seller_portal_feedbacks_complaint_submit import (  # noqa: E402
    DEFAULT_OUTPUT_ROOT as DEFAULT_SUBMIT_OUTPUT_ROOT,
    DEFAULT_START_URL,
    DEFAULT_STORAGE_STATE_PATH,
    DEFAULT_WB_BOT_PYTHON,
    LOCAL_OUTPUT_ROOT as LOCAL_SUBMIT_OUTPUT_ROOT,
    MAX_SUBMIT_HARD_CAP,
    SubmitConfig,
    parse_stars,
    run_submit,
    write_report_artifacts as write_submit_report_artifacts,
)
from apps.seller_portal_automation_guard import (  # noqa: E402
    SellerPortalAutomationBusy,
    SellerPortalStorageStatePolicyError,
    busy_response_payload,
    seller_portal_automation_lock,
    seller_portal_storage_state_path,
    validate_storage_state_path_for_runtime,
)


CONTRACT_NAME = "seller_portal_feedbacks_complaint_batch"
CONTRACT_VERSION = "v1"
DEFAULT_OUTPUT_ROOT = Path("/opt/wb-core-runtime/state/feedbacks_complaint_batch")
LOCAL_OUTPUT_ROOT = Path("artifacts/seller_portal_feedbacks_complaint_batch")
OTHER_WITH_DETAIL = "other_with_explicit_detail"
NOT_SUBMITTED_REASON_GROUPS = {
    "already_in_journal",
    "already_complained_in_wb",
    "exact_match_not_found",
    "actionable_dom_row_not_found_wrong_tab",
    "actionable_dom_row_not_found_filter_issue",
    "actionable_dom_row_not_found_scroll_or_pagination",
    "actionable_dom_row_not_found_true_unavailable",
    "complaint_action_disabled_already_complained",
    "complaint_action_disabled_other",
    "complaint_action_missing",
    "not_submit_ready_text",
    "seller_portal_session_or_systemic_error",
    "submit_unconfirmed",
    "description_not_persisted",
    OTHER_WITH_DETAIL,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--stars", default="1,2")
    parser.add_argument("--is-answered", default="all", choices=("all", "true", "false"))
    parser.add_argument("--runtime-dir", default="/opt/wb-core-runtime/state")
    parser.add_argument("--storage-state-path", default=str(seller_portal_storage_state_path(DEFAULT_STORAGE_STATE_PATH)))
    parser.add_argument("--wb-bot-python", default=str(DEFAULT_WB_BOT_PYTHON))
    parser.add_argument("--output-root", default="")
    parser.add_argument("--start-url", default=DEFAULT_START_URL)
    parser.add_argument("--max-api-rows", type=int, default=100)
    parser.add_argument("--max-submit", type=int, default=MAX_SUBMIT_HARD_CAP)
    parser.add_argument("--max-runs-per-day", type=int, default=8)
    parser.add_argument("--timeout-ms", type=int, default=60000)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--submit-confirmation", action="store_true")
    parser.add_argument("--no-artifacts", action="store_true")
    args = parser.parse_args()

    output_root = (
        Path(args.output_root)
        if args.output_root
        else (DEFAULT_OUTPUT_ROOT if Path("/opt/wb-core-runtime/state").exists() else LOCAL_OUTPUT_ROOT)
    )
    report = run_batch(
        date_from=str(args.date_from),
        date_to=str(args.date_to),
        stars=parse_stars(str(args.stars)),
        is_answered=str(args.is_answered),
        runtime_dir=Path(args.runtime_dir),
        storage_state_path=Path(args.storage_state_path).expanduser(),
        wb_bot_python=Path(args.wb_bot_python).expanduser(),
        output_root=output_root,
        start_url=str(args.start_url).rstrip("/") or DEFAULT_START_URL,
        max_api_rows=max(1, int(args.max_api_rows)),
        max_submit=min(MAX_SUBMIT_HARD_CAP, max(1, int(args.max_submit))),
        max_runs_per_day=max(1, int(args.max_runs_per_day)),
        timeout_ms=max(5000, int(args.timeout_ms)),
        headless=not bool(args.headed),
        dry_run=bool(args.dry_run),
        submit_confirmation=bool(args.submit_confirmation),
        write_artifacts=not bool(args.no_artifacts),
    )
    print(json.dumps(_compact(report), ensure_ascii=False, indent=2))


def run_batch(
    *,
    date_from: str,
    date_to: str,
    stars: tuple[int, ...],
    is_answered: str,
    runtime_dir: Path,
    storage_state_path: Path,
    wb_bot_python: Path,
    output_root: Path,
    start_url: str,
    max_api_rows: int,
    max_submit: int,
    max_runs_per_day: int,
    timeout_ms: int,
    headless: bool,
    dry_run: bool,
    submit_confirmation: bool,
    write_artifacts: bool = True,
) -> dict[str, Any]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        storage_state_path = (
            seller_portal_storage_state_path(DEFAULT_STORAGE_STATE_PATH)
            if storage_state_path == DEFAULT_STORAGE_STATE_PATH
            else storage_state_path
        )
        validate_storage_state_path_for_runtime(storage_state_path, runtime_dir)
        with seller_portal_automation_lock(
            runtime_dir=runtime_dir,
            owner=CONTRACT_NAME,
            purpose="complaint_batch" if not dry_run else "complaint_batch_dry_run",
            run_id=run_id,
            expected_max_seconds=max(600, max_runs_per_day * max(1, int(timeout_ms / 1000) + 120)),
        ) as lock:
            return _run_batch_locked(
                date_from=date_from,
                date_to=date_to,
                stars=stars,
                is_answered=is_answered,
                runtime_dir=runtime_dir,
                storage_state_path=storage_state_path,
                wb_bot_python=wb_bot_python,
                output_root=output_root,
                start_url=start_url,
                max_api_rows=max_api_rows,
                max_submit=max_submit,
                max_runs_per_day=max_runs_per_day,
                timeout_ms=timeout_ms,
                headless=headless,
                dry_run=dry_run,
                submit_confirmation=submit_confirmation,
                write_artifacts=write_artifacts,
                automation_lock=lock.public_payload(),
            )
    except SellerPortalAutomationBusy as exc:
        return _batch_preflight_error_report(date_from, date_to, stars, is_answered, run_id, "automation_lock", busy_response_payload(exc.lock_payload), output_root, write_artifacts)
    except SellerPortalStorageStatePolicyError as exc:
        return _batch_preflight_error_report(date_from, date_to, stars, is_answered, run_id, "storage_state_policy", {"code": exc.code, "message": str(exc)}, output_root, write_artifacts)


def _run_batch_locked(
    *,
    date_from: str,
    date_to: str,
    stars: tuple[int, ...],
    is_answered: str,
    runtime_dir: Path,
    storage_state_path: Path,
    wb_bot_python: Path,
    output_root: Path,
    start_url: str,
    max_api_rows: int,
    max_submit: int,
    max_runs_per_day: int,
    timeout_ms: int,
    headless: bool,
    dry_run: bool,
    submit_confirmation: bool,
    write_artifacts: bool = True,
    automation_lock: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    started_at = _iso_now()
    report: dict[str, Any] = {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "started_at": started_at,
        "finished_at": "",
        "parameters": {
            "date_from": date_from,
            "date_to": date_to,
            "stars": list(stars),
            "is_answered": is_answered,
            "max_api_rows": max_api_rows,
            "max_submit": max_submit,
            "max_runs_per_day": max_runs_per_day,
            "dry_run": dry_run,
            "submit_confirmation": submit_confirmation,
        },
        "safety": {
            "delegates_to_guarded_submit_runner": True,
            "hard_max_submit_per_run": MAX_SUBMIT_HARD_CAP,
            "non_exact_submit_allowed": False,
            "public_browser_direct_submit": False,
            "mass_submit_allowed": False,
        },
        "automation_lock": dict(automation_lock or {}),
        "days": [],
        "runs": [],
        "candidates": {},
        "aggregate": {},
        "errors": [],
    }
    if not dry_run and not submit_confirmation:
        report["errors"].append(
            {
                "stage": "preflight",
                "code": "submit_confirmation_required",
                "message": "--submit-confirmation is required with --no-dry-run",
            }
        )
        report["finished_at"] = _iso_now()
        _maybe_write_batch_report(report, output_root, write_artifacts)
        return report

    candidate_state: dict[str, dict[str, Any]] = {}
    submit_output_root = DEFAULT_SUBMIT_OUTPUT_ROOT if Path("/opt/wb-core-runtime/state").exists() else LOCAL_SUBMIT_OUTPUT_ROOT
    for day in _date_range(date_from, date_to):
        day_report = {
            "date": day,
            "runs": [],
            "ai_candidates": 0,
            "submitted": 0,
            "not_submitted": 0,
            "stopped_reason": "",
        }
        report["days"].append(day_report)
        day_had_systemic_error = False
        for run_index in range(1, max_runs_per_day + 1):
            denied_after_previous_attempt = _not_submitted_feedback_ids_for_day(candidate_state, day)
            config = SubmitConfig(
                date_from=day,
                date_to=day,
                stars=stars,
                is_answered=is_answered,
                max_api_rows=max_api_rows,
                max_submit=max_submit,
                include_review=True,
                dry_run=dry_run,
                require_exact=True,
                retry_errors=False,
                submit_confirmation=submit_confirmation,
                runtime_dir=runtime_dir,
                storage_state_path=storage_state_path,
                wb_bot_python=wb_bot_python,
                output_dir=submit_output_root,
                start_url=start_url,
                headless=headless,
                timeout_ms=timeout_ms,
                write_artifacts=False,
                deny_feedback_ids=denied_after_previous_attempt,
                target_feedback_id="",
            )
            run_report = dict(run_submit(config))
            artifact_paths = write_submit_report_artifacts(run_report, submit_output_root) if write_artifacts else {}
            if artifact_paths:
                run_report["artifact_paths"] = {key: str(path) for key, path in artifact_paths.items()}
            run_summary = _run_summary(day, run_index, run_report)
            day_report["runs"].append(run_summary)
            report["runs"].append(run_summary)
            _merge_candidate_state(candidate_state, day=day, run_report=run_report)
            if run_index == 1:
                aggregate = run_report.get("aggregate") if isinstance(run_report.get("aggregate"), Mapping) else {}
                day_report["ai_candidates"] = int(aggregate.get("ai_yes_count") or 0) + int(aggregate.get("ai_review_count") or 0)
            systemic = _systemic_blocker(run_report)
            if systemic:
                day_had_systemic_error = True
                day_report["stopped_reason"] = systemic
                report["errors"].append({"stage": "daily_submit", "date": day, "code": "systemic_blocker", "message": systemic})
                break
            submitted_count = int((run_report.get("aggregate") or {}).get("submitted_count") or 0)
            blocker = str(((run_report.get("ui") or {}).get("submit_ui") or {}).get("blocker") or "")
            if dry_run:
                day_report["stopped_reason"] = "dry_run_completed"
                break
            if submitted_count >= max_submit and "max_submit hard cap reached" in blocker:
                continue
            if _has_pending_feedback_ids_for_day(candidate_state, day):
                continue
            day_report["stopped_reason"] = "day_exhausted_or_no_more_safe_candidates"
            break
        if not day_report["stopped_reason"]:
            day_report["stopped_reason"] = "max_runs_per_day_reached"
        if day_had_systemic_error:
            break

    _finalize_unprocessed(candidate_state, report["days"], report["errors"])
    report["candidates"] = candidate_state
    report["aggregate"] = _batch_aggregate(candidate_state, report["days"])
    report["finished_at"] = _iso_now()
    _maybe_write_batch_report(report, output_root, write_artifacts)
    return report


def _merge_candidate_state(candidate_state: dict[str, dict[str, Any]], *, day: str, run_report: Mapping[str, Any]) -> None:
    candidates = run_report.get("candidates") if isinstance(run_report.get("candidates"), list) else []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        ai = candidate.get("ai") if isinstance(candidate.get("ai"), Mapping) else {}
        fit = str(ai.get("complaint_fit") or "").strip().lower()
        if fit not in {"yes", "review"}:
            continue
        feedback_id = str(candidate.get("feedback_id") or "").strip()
        if not feedback_id:
            continue
        state = candidate_state.setdefault(
            feedback_id,
            {
                "feedback_id": feedback_id,
                "date": day,
                "complaint_fit": fit,
                "status": "pending",
                "reason_group": "",
                "reason_detail": "",
                "reason": "",
                "submitted_run_id": "",
                "attempted_run_ids": [],
            },
        )
        run_id = str(run_report.get("run_id") or "")
        if run_id and run_id not in state["attempted_run_ids"]:
            state["attempted_run_ids"].append(run_id)
        state.update(_candidate_diagnostics(candidate))
        modal = candidate.get("modal") if isinstance(candidate.get("modal"), Mapping) else {}
        if modal.get("submit_success"):
            state.update(
                {
                    "status": "submitted",
                    "reason_group": "",
                    "reason_detail": "",
                    "reason": "submitted_confirmed",
                    "submitted_run_id": run_id,
                }
            )
            continue
        if state.get("status") == "submitted":
            continue
        reason = str(candidate.get("skip_reason") or modal.get("blocker") or "")
        if reason == "skipped because max_ai_candidates slots were already filled":
            state.update({"status": "pending", "reason": reason})
        elif reason:
            group, detail = _classify_not_submitted(reason, candidate=candidate, run_report=run_report)
            state.update({"status": "not_submitted", "reason_group": group, "reason_detail": detail, "reason": reason})
        elif candidate.get("selected_for_dry_run"):
            state.update({"status": "pending", "reason": "selected but not attempted yet"})


def _batch_preflight_error_report(
    date_from: str,
    date_to: str,
    stars: tuple[int, ...],
    is_answered: str,
    run_id: str,
    stage: str,
    error: Mapping[str, Any],
    output_root: Path,
    write_artifacts: bool,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "started_at": _iso_now(),
        "finished_at": _iso_now(),
        "run_id": run_id,
        "parameters": {
            "date_from": date_from,
            "date_to": date_to,
            "stars": list(stars),
            "is_answered": is_answered,
        },
        "safety": {
            "delegates_to_guarded_submit_runner": True,
            "hard_max_submit_per_run": MAX_SUBMIT_HARD_CAP,
            "non_exact_submit_allowed": False,
            "public_browser_direct_submit": False,
            "mass_submit_allowed": False,
        },
        "automation_lock": error.get("lock") or {},
        "days": [],
        "runs": [],
        "candidates": {},
        "aggregate": {"days_processed": 0, "runs_executed": 0, "ai_candidates_to_complain": 0, "submitted": 0, "not_submitted": 0},
        "errors": [
            {
                "stage": stage,
                "code": str(error.get("code") or stage),
                "message": str(error.get("message") or ""),
            }
        ],
    }
    _maybe_write_batch_report(report, output_root, write_artifacts)
    return report


def _not_submitted_feedback_ids_for_day(candidate_state: Mapping[str, Mapping[str, Any]], day: str) -> tuple[str, ...]:
    return tuple(
        str(state.get("feedback_id") or "")
        for state in candidate_state.values()
        if str(state.get("date") or "") == day
        and str(state.get("status") or "") == "not_submitted"
        and str(state.get("feedback_id") or "")
    )


def _has_pending_feedback_ids_for_day(candidate_state: Mapping[str, Mapping[str, Any]], day: str) -> bool:
    return any(
        str(state.get("date") or "") == day and str(state.get("status") or "") == "pending"
        for state in candidate_state.values()
    )


def _finalize_unprocessed(
    candidate_state: dict[str, dict[str, Any]],
    days: list[Mapping[str, Any]] | None = None,
    errors: list[Mapping[str, Any]] | None = None,
) -> None:
    stopped_by_day = {
        str(day.get("date") or ""): str(day.get("stopped_reason") or "")
        for day in (days or [])
        if isinstance(day, Mapping)
    }
    systemic_by_day = {
        str(error.get("date") or ""): str(error.get("message") or error.get("code") or "")
        for error in (errors or [])
        if isinstance(error, Mapping)
    }
    for state in candidate_state.values():
        if state.get("status") == "pending":
            day = str(state.get("date") or "")
            stop_reason = systemic_by_day.get(day) or stopped_by_day.get(day) or "batch ended before candidate was processed"
            reason = str(state.get("reason") or "")
            if "submit_unconfirmed" in stop_reason or "unconfirmed" in stop_reason:
                group = "submit_unconfirmed"
                detail = "not_attempted_due_to_prior_submit_unconfirmed"
            else:
                group = OTHER_WITH_DETAIL
                detail = _safe_reason_detail(reason or stop_reason)
            state["status"] = "not_submitted"
            state["reason_group"] = group
            state["reason_detail"] = detail
            state["reason"] = reason or f"not processed before batch stopped: {stop_reason}"


def _reason_group(reason: str) -> str:
    return _classify_not_submitted(reason)[0]


def _classify_not_submitted(
    reason: str,
    *,
    candidate: Mapping[str, Any] | None = None,
    run_report: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    normalized = reason.lower()
    if "complaint already exists" in normalized or "already exists for feedback_id" in normalized:
        return "already_in_journal", _safe_reason_detail(reason)
    if "unconfirmed" in normalized or "not confirmed" in normalized or "submit clicked once" in normalized:
        return "submit_unconfirmed", _safe_reason_detail(reason)
    if "already_complained_in_wb" in normalized or "уже пожал" in normalized or "жалоба уже" in normalized:
        return "already_complained_in_wb", _safe_reason_detail(reason)
    if "exact actionable dom row" in normalized or "actionable dom row" in normalized:
        return _classify_actionable_dom_not_found(reason, candidate=candidate)
    if "not found" in normalized:
        return "exact_match_not_found", _safe_reason_detail(reason)
    if "complaint_action_disabled_already_complained" in normalized:
        return "already_complained_in_wb", _safe_reason_detail(reason)
    if "complaint action is disabled" in normalized or "complaint_action_disabled" in normalized:
        return "complaint_action_disabled_other", _safe_reason_detail(reason)
    if "пожаловаться на отзыв action not found" in normalized or "complaint action not found" in normalized:
        return "complaint_action_missing", _safe_reason_detail(reason)
    if "ai_no_not_submit_ready" in normalized or "жалобу не подавать" in normalized:
        return "not_submit_ready_text", _safe_reason_detail(reason)
    if "seller portal session" in normalized or "session" in normalized or _report_has_systemic_error(run_report):
        return "seller_portal_session_or_systemic_error", _safe_reason_detail(reason)
    if "description field value mismatch" in normalized or "description" in normalized and "mismatch" in normalized:
        return "description_not_persisted", _safe_reason_detail(reason)
    return OTHER_WITH_DETAIL, _safe_reason_detail(reason)


def _classify_actionable_dom_not_found(reason: str, *, candidate: Mapping[str, Any] | None) -> tuple[str, str]:
    if not isinstance(candidate, Mapping):
        return OTHER_WITH_DETAIL, _safe_reason_detail(reason)
    api_summary = _candidate_api_summary(candidate)
    match = candidate.get("match") if isinstance(candidate.get("match"), Mapping) else {}
    not_found_reason = str(match.get("not_found_reason") or "")
    resolver = _candidate_resolver(candidate)
    expected_tab = _expected_status_tab(api_summary)
    tabs_tried = [str(item or "") for item in resolver.get("tabs_tried", []) if str(item or "")]
    attempts = resolver.get("attempts") if isinstance(resolver.get("attempts"), list) else []
    expected_attempts = [
        attempt
        for attempt in attempts
        if isinstance(attempt, Mapping) and (not expected_tab or str(attempt.get("tab") or "") == expected_tab)
    ]
    if expected_tab and tabs_tried and expected_tab not in tabs_tried:
        return "actionable_dom_row_not_found_wrong_tab", f"expected_tab={expected_tab}; tabs_tried={','.join(tabs_tried)}"
    if expected_attempts and any(_attempt_filter_failed(attempt, api_summary) for attempt in expected_attempts):
        return "actionable_dom_row_not_found_filter_issue", _attempt_filter_detail(expected_attempts[0], api_summary)
    if _scroll_or_pagination_limited(expected_attempts or attempts):
        return "actionable_dom_row_not_found_scroll_or_pagination", "scroll_or_pagination_limit_reached"
    if not_found_reason == "not_found_due_to_short_text_or_duplicate":
        return "exact_match_not_found", "not_found_due_to_short_text_or_duplicate"
    if not_found_reason == "not_found_due_to_no_ui_coverage":
        return "actionable_dom_row_not_found_true_unavailable", _ui_coverage_detail(resolver, expected_attempts)
    if not_found_reason:
        return "exact_match_not_found", _safe_reason_detail(not_found_reason)
    return "actionable_dom_row_not_found_true_unavailable", _ui_coverage_detail(resolver, expected_attempts)


def _candidate_diagnostics(candidate: Mapping[str, Any]) -> dict[str, Any]:
    api_summary = _candidate_api_summary(candidate)
    match = candidate.get("match") if isinstance(candidate.get("match"), Mapping) else {}
    resolver = _candidate_resolver(candidate)
    modal = candidate.get("modal") if isinstance(candidate.get("modal"), Mapping) else {}
    return {
        "api_summary": {
            "created_date": str(api_summary.get("created_date") or ""),
            "created_at": str(api_summary.get("created_at") or ""),
            "rating": str(api_summary.get("rating") or ""),
            "is_answered": api_summary.get("is_answered"),
            "nm_id": str(api_summary.get("nm_id") or ""),
            "supplier_article": str(api_summary.get("supplier_article") or ""),
            "review_text": str(api_summary.get("review_text") or "")[:240],
            "pros": str(api_summary.get("pros") or "")[:240],
            "cons": str(api_summary.get("cons") or "")[:240],
            "review_tags": api_summary.get("review_tags") if isinstance(api_summary.get("review_tags"), list) else [],
        },
        "ai_category": str((candidate.get("ai") if isinstance(candidate.get("ai"), Mapping) else {}).get("category") or ""),
        "ai_reason": str((candidate.get("ai") if isinstance(candidate.get("ai"), Mapping) else {}).get("reason") or "")[:500],
        "match_status": str(match.get("match_status") or ""),
        "match_score": match.get("match_score"),
        "match_not_found_reason": str(match.get("not_found_reason") or ""),
        "match_candidate_count": int(match.get("candidate_count") or 0),
        "tab_used": str(resolver.get("tab_used") or modal.get("tab_used") or ""),
        "tabs_tried": [str(item or "") for item in resolver.get("tabs_tried", []) if str(item or "")],
        "date_filter_applied": bool(resolver.get("date_filter_applied")),
        "star_filter_applied": bool(resolver.get("star_filter_applied")),
        "selected_star_values_after": resolver.get("selected_star_values_after") or [],
        "dom_rows_collected": int(resolver.get("dom_rows_collected") or 0),
        "visible_rows_checked": int(resolver.get("visible_rows_checked") or 0),
        "visible_rows_checked_after_search": int(resolver.get("visible_rows_checked_after_search") or 0),
        "visible_rows_checked_after_scroll": int(resolver.get("visible_rows_checked_after_scroll") or 0),
        "search_used": bool(resolver.get("search_used")),
        "scroll_used": bool(resolver.get("scroll_used")),
        "complaint_action_found": bool(resolver.get("complaint_action_found") or modal.get("complaint_action_found")),
        "complaint_action_available": bool(resolver.get("complaint_action_available")),
        "complaint_action_disabled": bool(resolver.get("complaint_action_disabled")),
        "complaint_action_disabled_reason": str(resolver.get("complaint_action_disabled_reason") or ""),
    }


def _candidate_api_summary(candidate: Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(candidate.get("api_summary"), Mapping):
        return candidate["api_summary"]  # type: ignore[index]
    match = candidate.get("match") if isinstance(candidate.get("match"), Mapping) else {}
    if isinstance(match.get("api_summary"), Mapping):
        return match["api_summary"]  # type: ignore[index]
    return {}


def _candidate_resolver(candidate: Mapping[str, Any]) -> Mapping[str, Any]:
    modal = candidate.get("modal") if isinstance(candidate.get("modal"), Mapping) else {}
    resolver = modal.get("actionability_resolver") if isinstance(modal.get("actionability_resolver"), Mapping) else {}
    return resolver


def _expected_status_tab(api_summary: Mapping[str, Any]) -> str:
    value = api_summary.get("is_answered")
    if value is True or str(value).strip().lower() == "true":
        return "Есть ответ"
    if value is False or str(value).strip().lower() == "false":
        return "Ждут ответа"
    return ""


def _attempt_filter_failed(attempt: Mapping[str, Any], api_summary: Mapping[str, Any]) -> bool:
    if not bool(attempt.get("date_filter_applied")) or not bool(attempt.get("star_filter_applied")):
        return True
    requested_rating = _safe_int_or_none(api_summary.get("rating"))
    selected = {_safe_int_or_none(item) for item in (attempt.get("selected_star_values_after") or [])}
    return requested_rating is not None and requested_rating not in selected


def _attempt_filter_detail(attempt: Mapping[str, Any], api_summary: Mapping[str, Any]) -> str:
    return (
        f"date_filter_applied={bool(attempt.get('date_filter_applied'))}; "
        f"star_filter_applied={bool(attempt.get('star_filter_applied'))}; "
        f"selected_star_values_after={attempt.get('selected_star_values_after') or []}; "
        f"expected_rating={api_summary.get('rating') or ''}"
    )


def _scroll_or_pagination_limited(attempts: list[Any]) -> bool:
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            continue
        for item in attempt.get("scroll_attempts") or []:
            if not isinstance(item, Mapping):
                continue
            stop_reason = str(item.get("stop_reason") or "").lower()
            attempts_count = _safe_int_or_none(item.get("scroll_attempts"))
            max_attempts = _safe_int_or_none(item.get("max_scroll_attempts"))
            if "max" in stop_reason or (attempts_count is not None and max_attempts is not None and attempts_count >= max_attempts):
                return True
    return False


def _ui_coverage_detail(resolver: Mapping[str, Any], expected_attempts: list[Any]) -> str:
    attempt = expected_attempts[0] if expected_attempts and isinstance(expected_attempts[0], Mapping) else {}
    dom_rows = attempt.get("dom_rows_collected") if isinstance(attempt, Mapping) else resolver.get("dom_rows_collected")
    tab = attempt.get("tab") if isinstance(attempt, Mapping) else resolver.get("tab_used")
    return f"expected_tab={tab or resolver.get('tab_used') or ''}; dom_rows_collected={dom_rows or 0}; cursor_rows_collected=0"


def _report_has_systemic_error(run_report: Mapping[str, Any] | None) -> bool:
    if not isinstance(run_report, Mapping):
        return False
    return bool(_systemic_blocker(run_report))


def _safe_int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_reason_detail(reason: str) -> str:
    cleaned = " ".join(str(reason or "").split())
    return cleaned[:500] or "not_submitted_without_detail"


def _systemic_blocker(run_report: Mapping[str, Any]) -> str:
    aggregate = run_report.get("aggregate") if isinstance(run_report.get("aggregate"), Mapping) else {}
    if int(aggregate.get("error_count") or 0) > 0:
        return str(run_report.get("final_conclusion") or "submit_error")
    errors = run_report.get("errors") if isinstance(run_report.get("errors"), list) else []
    for error in errors:
        if not isinstance(error, Mapping):
            continue
        stage = str(error.get("stage") or "")
        if stage in {"api_feedbacks", "session", "navigation", "submit_browser"}:
            return str(error.get("message") or stage)
    conclusion = str(run_report.get("final_conclusion") or "")
    if conclusion in {"source_error", "submit_unconfirmed_error", "submit_failed_validation", "submit_failed_network"}:
        return conclusion
    return ""


def _run_summary(day: str, run_index: int, run_report: Mapping[str, Any]) -> dict[str, Any]:
    aggregate = run_report.get("aggregate") if isinstance(run_report.get("aggregate"), Mapping) else {}
    artifacts = run_report.get("artifact_paths") if isinstance(run_report.get("artifact_paths"), Mapping) else {}
    return {
        "date": day,
        "run_index": run_index,
        "run_id": str(run_report.get("run_id") or ""),
        "final_conclusion": str(run_report.get("final_conclusion") or ""),
        "api_rows_loaded": int(aggregate.get("api_rows_loaded") or 0),
        "ai_yes_count": int(aggregate.get("ai_yes_count") or 0),
        "ai_review_count": int(aggregate.get("ai_review_count") or 0),
        "submitted_count": int(aggregate.get("submitted_count") or 0),
        "skipped_reasons": aggregate.get("skipped_reasons") or [],
        "report_json_path": str(artifacts.get("json") or ""),
    }


def _batch_aggregate(candidate_state: Mapping[str, Mapping[str, Any]], days: list[Mapping[str, Any]]) -> dict[str, Any]:
    submitted = [item for item in candidate_state.values() if item.get("status") == "submitted"]
    not_submitted = [item for item in candidate_state.values() if item.get("status") == "not_submitted"]
    grouped = Counter(_normalized_reason_group(str(item.get("reason_group") or "")) for item in not_submitted)
    return {
        "days_processed": len(days),
        "runs_executed": sum(len(day.get("runs") or []) for day in days),
        "ai_candidates_to_complain": len(candidate_state),
        "submitted": len(submitted),
        "not_submitted": len(not_submitted),
        "not_submitted_reasons": dict(grouped),
        "submitted_feedback_ids": [str(item.get("feedback_id") or "") for item in submitted],
    }


def _not_submitted_inventory(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    candidates = report.get("candidates") if isinstance(report.get("candidates"), Mapping) else {}
    rows: list[dict[str, Any]] = []
    for feedback_id, candidate in candidates.items():
        if not isinstance(candidate, Mapping) or str(candidate.get("status") or "") != "not_submitted":
            continue
        rows.append(
            {
                "feedback_id": str(candidate.get("feedback_id") or feedback_id or ""),
                "date": str(candidate.get("date") or ""),
                "complaint_fit": str(candidate.get("complaint_fit") or ""),
                "reason_group": _normalized_reason_group(str(candidate.get("reason_group") or "")),
                "reason_detail": str(candidate.get("reason_detail") or ""),
                "reason": str(candidate.get("reason") or ""),
                "attempted_run_ids": candidate.get("attempted_run_ids") if isinstance(candidate.get("attempted_run_ids"), list) else [],
            }
        )
    return sorted(rows, key=lambda item: (str(item.get("date") or ""), str(item.get("feedback_id") or "")))


def _continuation_window_from_batch_report(report: Mapping[str, Any], *, calendar_days: int) -> tuple[str, str]:
    days = report.get("days") if isinstance(report.get("days"), list) else []
    if not days:
        params = report.get("parameters") if isinstance(report.get("parameters"), Mapping) else {}
        start = str(params.get("date_from") or "")
    else:
        last_day = next((day for day in reversed(days) if isinstance(day, Mapping) and str(day.get("date") or "")), {})
        if not isinstance(last_day, Mapping):
            start = ""
        else:
            stopped_reason = str(last_day.get("stopped_reason") or "")
            last_date = str(last_day.get("date") or "")
            if _is_incomplete_day_stop(stopped_reason):
                start = last_date
            else:
                start = (date.fromisoformat(last_date) + timedelta(days=1)).isoformat()
    if not start:
        raise ValueError("cannot compute continuation window without a report date")
    if calendar_days < 1:
        raise ValueError("calendar_days must be >= 1")
    end = (date.fromisoformat(start) + timedelta(days=calendar_days - 1)).isoformat()
    return start, end


def _is_incomplete_day_stop(stopped_reason: str) -> bool:
    normalized = stopped_reason.lower()
    return bool(
        not normalized
        or "unconfirmed" in normalized
        or "systemic" in normalized
        or "max_runs_per_day" in normalized
        or "source_error" in normalized
        or "session" in normalized
    )


def _normalized_reason_group(group: str) -> str:
    return group if group in NOT_SUBMITTED_REASON_GROUPS else OTHER_WITH_DETAIL


def _date_range(date_from: str, date_to: str) -> list[str]:
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if start > end:
        raise ValueError("date_from must be <= date_to")
    result: list[str] = []
    cursor = start
    while cursor <= end:
        result.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return result


def _maybe_write_batch_report(report: Mapping[str, Any], output_root: Path, write_artifacts: bool) -> None:
    if not write_artifacts:
        return
    run_dir = output_root / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)
    json_path = run_dir / "seller_portal_feedbacks_complaint_batch.json"
    md_path = run_dir / "seller_portal_feedbacks_complaint_batch.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(_render_markdown(report), encoding="utf-8")
    if isinstance(report, dict):
        report["artifact_paths"] = {"json": str(json_path), "markdown": str(md_path)}


def _render_markdown(report: Mapping[str, Any]) -> str:
    aggregate = report.get("aggregate") if isinstance(report.get("aggregate"), Mapping) else {}
    lines = [
        "# Seller Portal Feedback Complaint Batch",
        "",
        f"- Started: `{report.get('started_at')}`",
        f"- Finished: `{report.get('finished_at')}`",
        f"- AI candidates: `{aggregate.get('ai_candidates_to_complain', 0)}`",
        f"- Submitted: `{aggregate.get('submitted', 0)}`",
        f"- Not submitted: `{aggregate.get('not_submitted', 0)}`",
        f"- Runs: `{aggregate.get('runs_executed', 0)}`",
        "",
        "## Not Submitted Reasons",
        "",
    ]
    for reason, count in sorted((aggregate.get("not_submitted_reasons") or {}).items()):
        lines.append(f"- `{reason}`: `{count}`")
    return "\n".join(lines) + "\n"


def _compact(report: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "contract_name": report.get("contract_name"),
        "started_at": report.get("started_at"),
        "finished_at": report.get("finished_at"),
        "parameters": report.get("parameters"),
        "aggregate": report.get("aggregate"),
        "errors": report.get("errors"),
        "artifact_paths": report.get("artifact_paths"),
    }


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    main()
