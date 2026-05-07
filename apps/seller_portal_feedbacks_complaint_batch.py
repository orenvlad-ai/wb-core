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


CONTRACT_NAME = "seller_portal_feedbacks_complaint_batch"
CONTRACT_VERSION = "v1"
DEFAULT_OUTPUT_ROOT = Path("/opt/wb-core-runtime/state/feedbacks_complaint_batch")
LOCAL_OUTPUT_ROOT = Path("artifacts/seller_portal_feedbacks_complaint_batch")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--stars", default="1,2")
    parser.add_argument("--is-answered", default="all", choices=("all", "true", "false"))
    parser.add_argument("--runtime-dir", default="/opt/wb-core-runtime/state")
    parser.add_argument("--storage-state-path", default=str(DEFAULT_STORAGE_STATE_PATH))
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

    _finalize_unprocessed(candidate_state)
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
                "reason": "",
                "submitted_run_id": "",
                "attempted_run_ids": [],
            },
        )
        run_id = str(run_report.get("run_id") or "")
        if run_id and run_id not in state["attempted_run_ids"]:
            state["attempted_run_ids"].append(run_id)
        modal = candidate.get("modal") if isinstance(candidate.get("modal"), Mapping) else {}
        if modal.get("submit_success"):
            state.update(
                {
                    "status": "submitted",
                    "reason_group": "",
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
            group = _reason_group(reason)
            state.update({"status": "not_submitted", "reason_group": group, "reason": reason})
        elif candidate.get("selected_for_dry_run"):
            state.update({"status": "pending", "reason": "selected but not attempted yet"})


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


def _finalize_unprocessed(candidate_state: dict[str, dict[str, Any]]) -> None:
    for state in candidate_state.values():
        if state.get("status") == "pending":
            state["status"] = "not_submitted"
            state["reason_group"] = "other"
            state["reason"] = state.get("reason") or "not processed before batch stopped"


def _reason_group(reason: str) -> str:
    normalized = reason.lower()
    if "complaint already exists" in normalized or "already exists for feedback_id" in normalized:
        return "already_in_journal"
    if "already_complained_in_wb" in normalized or "уже пожал" in normalized or "жалоба уже" in normalized:
        return "already_complained_in_wb"
    if "exact actionable dom row" in normalized or "actionable dom row" in normalized:
        return "actionable_dom_row_not_found"
    if "not found" in normalized:
        return "exact_match_not_found"
    if "complaint action is disabled" in normalized or "complaint_action_disabled" in normalized:
        return "complaint_action_disabled"
    if "пожаловаться на отзыв action not found" in normalized or "complaint action not found" in normalized:
        return "complaint_action_missing"
    if "ai_no_not_submit_ready" in normalized or "жалобу не подавать" in normalized:
        return "not_submit_ready_text"
    if "seller portal session" in normalized or "session" in normalized:
        return "seller_portal_session/systemic_error"
    if "unconfirmed" in normalized:
        return "submit_unconfirmed"
    if "description field value mismatch" in normalized or "description" in normalized and "mismatch" in normalized:
        return "description_not_persisted"
    return "other"


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
    grouped = Counter(str(item.get("reason_group") or "other") for item in not_submitted)
    return {
        "days_processed": len(days),
        "runs_executed": sum(len(day.get("runs") or []) for day in days),
        "ai_candidates_to_complain": len(candidate_state),
        "submitted": len(submitted),
        "not_submitted": len(not_submitted),
        "not_submitted_reasons": dict(grouped),
        "submitted_feedback_ids": [str(item.get("feedback_id") or "") for item in submitted],
    }


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
