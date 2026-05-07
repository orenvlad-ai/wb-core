"""Read-only Seller Portal status sync for submitted feedback complaints."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

from apps.seller_portal_feedbacks_complaints_scout import (  # noqa: E402
    BUSINESS_TZ,
    DEFAULT_START_URL,
    ScoutConfig,
    check_session,
    scout_my_complaints,
)
from apps.seller_portal_feedbacks_matching_replay import safe_text  # noqa: E402
from apps.seller_portal_relogin_session import (  # noqa: E402
    DEFAULT_STORAGE_STATE_PATH,
    DEFAULT_WB_BOT_PYTHON,
    read_storage_state_supplier_context,
)
from packages.application.sheet_vitrina_v1_feedbacks_complaints import (  # noqa: E402
    COMPLAINT_STATUS_LABELS,
    CONTRACT_NAME,
    JsonFileFeedbacksComplaintJournal,
)


SYNC_CONTRACT_NAME = "seller_portal_feedbacks_complaints_status_sync"
DEFAULT_RUNTIME_DIR = Path(os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR", "/opt/wb-core-runtime/state"))
DEFAULT_OUTPUT_ROOT = Path("/opt/wb-core-runtime/state/feedbacks_complaints_status_sync")
LOCAL_OUTPUT_ROOT = Path("artifacts/seller_portal_feedbacks_complaints_status_sync")
DEFAULT_RECORD_STATUSES = ("waiting_response",)
ALL_RECORD_STATUSES = tuple(COMPLAINT_STATUS_LABELS.keys())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", default=str(DEFAULT_RUNTIME_DIR if DEFAULT_RUNTIME_DIR.exists() else ".runtime"))
    parser.add_argument("--storage-state-path", default=str(DEFAULT_STORAGE_STATE_PATH))
    parser.add_argument("--wb-bot-python", default=str(DEFAULT_WB_BOT_PYTHON))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--start-url", default=DEFAULT_START_URL)
    parser.add_argument("--max-complaint-rows", type=int, default=500)
    parser.add_argument(
        "--record-statuses",
        default=",".join(DEFAULT_RECORD_STATUSES),
        help="Comma-separated local complaint_status values to reconcile; use 'all' only for diagnostics.",
    )
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--timeout-ms", type=int, default=20000)
    parser.add_argument("--no-artifacts", action="store_true")
    args = parser.parse_args()

    output_dir = Path(args.output_dir) if args.output_dir else (DEFAULT_OUTPUT_ROOT if Path("/opt/wb-core-runtime/state").exists() else LOCAL_OUTPUT_ROOT)
    report = run_status_sync_for_runtime(
        runtime_dir=Path(args.runtime_dir).expanduser(),
        storage_state_path=Path(args.storage_state_path).expanduser(),
        wb_bot_python=Path(args.wb_bot_python).expanduser(),
        output_dir=output_dir,
        start_url=str(args.start_url).rstrip("/") or DEFAULT_START_URL,
        max_complaint_rows=max(1, int(args.max_complaint_rows)),
        record_statuses=args.record_statuses,
        headless=not args.headed,
        timeout_ms=max(5000, int(args.timeout_ms)),
        write_artifacts=not bool(args.no_artifacts),
    )
    print(json.dumps(_compact(report), ensure_ascii=False, indent=2))


def run_status_sync_for_runtime(
    *,
    runtime_dir: Path,
    storage_state_path: Path = DEFAULT_STORAGE_STATE_PATH,
    wb_bot_python: Path = DEFAULT_WB_BOT_PYTHON,
    output_dir: Path | None = None,
    start_url: str = DEFAULT_START_URL,
    max_complaint_rows: int = 500,
    record_statuses: Any = DEFAULT_RECORD_STATUSES,
    headless: bool = True,
    timeout_ms: int = 20000,
    write_artifacts: bool = True,
    run_id: str | None = None,
) -> dict[str, Any]:
    run_id = str(run_id or "").strip() or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    journal = JsonFileFeedbacksComplaintJournal(runtime_dir)
    records_before = journal.list_records()
    target_statuses = _normalize_record_statuses(record_statuses)
    target_records = _filter_records_by_status(records_before, target_statuses)
    status_counts_before = Counter(str(record.get("complaint_status") or "") for record in records_before)
    scout_config = ScoutConfig(
        mode="scout-complaints",
        storage_state_path=storage_state_path,
        wb_bot_python=wb_bot_python,
        output_root=output_dir or LOCAL_OUTPUT_ROOT,
        start_url=start_url,
        max_feedback_rows=1,
        max_complaint_rows=max_complaint_rows,
        max_modal_reviews=0,
        open_complaint_modal=False,
        headless=headless,
        timeout_ms=timeout_ms,
        write_artifacts=False,
    )
    report: dict[str, Any] = {
        "contract_name": SYNC_CONTRACT_NAME,
        "contract_version": "read_only_v1",
        "started_at": _iso_now(),
        "finished_at": None,
        "run_id": run_id,
        "runtime_journal_contract": CONTRACT_NAME,
        "runtime_journal_path": str(journal.path),
        "read_only_guards": {
            "seller_portal_write_actions_allowed": False,
            "complaint_submission_allowed": False,
            "status_sync_only": True,
            "record_status_scope": "all" if target_statuses is None else sorted(target_statuses),
        },
        "session": {},
        "navigation": {},
        "my_complaints": {},
        "updates": [],
        "aggregate": {
            "local_records_before": len(records_before),
            "local_waiting_records_before": int(status_counts_before.get("waiting_response", 0)),
            "local_satisfied_records_skipped": 0 if target_statuses is None or "satisfied" in target_statuses else int(status_counts_before.get("satisfied", 0)),
            "local_rejected_records_skipped": 0 if target_statuses is None or "rejected" in target_statuses else int(status_counts_before.get("rejected", 0)),
            "local_error_records_skipped": 0 if target_statuses is None or "error" in target_statuses else int(status_counts_before.get("error", 0)),
            "target_records_before": len(target_records),
            "records_scanned": len(target_records),
            "record_status_scope": "all" if target_statuses is None else sorted(target_statuses),
            "pending_rows_read": 0,
            "answered_rows_read": 0,
            "matched_local_complaints": 0,
            "statuses_updated": 0,
            "unmatched_rows": 0,
        },
        "errors": [],
    }
    if not target_records:
        report["session"] = {
            "ok": True,
            "status": "skipped_no_target_records",
            "message": "no local complaint records match status sync scope",
        }
        report["navigation"] = {"success": False, "skipped": True, "reason": "no_target_records"}
        report["finished_at"] = _iso_now()
        _maybe_write(report, output_dir, write_artifacts)
        return report

    session = check_session(scout_config)
    session = _augment_session_with_storage_supplier_context(session, storage_state_path)
    report["session"] = session
    if not session.get("ok") and not _can_try_direct_complaints_routes(session):
        report["errors"].append({"stage": "session", "code": str(session.get("status") or ""), "message": str(session.get("message") or "")})
        report["finished_at"] = _iso_now()
        _maybe_write(report, output_dir, write_artifacts)
        return report
    if not session.get("ok"):
        report["session"]["preflight_warning"] = (
            "generic Seller Portal probe failed; continuing with direct read-only complaints status URLs"
        )

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=headless)
            context = browser.new_context(
                storage_state=str(storage_state_path),
                locale="ru-RU",
                timezone_id=BUSINESS_TZ,
                viewport={"width": 1600, "height": 1200},
                accept_downloads=False,
            )
            page = context.new_page()
            page.set_default_timeout(timeout_ms)
            try:
                my = scout_my_complaints(page, scout_config)
                report["my_complaints"] = my
                report["navigation"] = {
                    "success": bool(my.get("success")),
                    "method": "direct_complaints_status_urls",
                    "pending_final_url": str((my.get("pending") or {}).get("navigation", {}).get("final_url") or ""),
                    "answered_final_url": str((my.get("answered") or {}).get("navigation", {}).get("final_url") or ""),
                    "blocker": str(my.get("blocker") or ""),
                }
                if not my.get("success") or my.get("blocker"):
                    report["errors"].append(
                        {"stage": "my_complaints_navigation", "code": "not_reached", "message": str(my.get("blocker") or "")}
                    )
                else:
                    _apply_status_updates(
                        report,
                        journal,
                        records_before,
                        my,
                        run_id=run_id,
                        record_statuses="all" if target_statuses is None else sorted(target_statuses),
                    )
            finally:
                context.close()
                browser.close()
    except Exception as exc:  # pragma: no cover - live fallback
        report["errors"].append({"stage": "browser_status_sync", "code": exc.__class__.__name__, "message": safe_text(str(exc), 800)})
    report["finished_at"] = _iso_now()
    _maybe_write(report, output_dir, write_artifacts)
    return report


def _normalize_record_statuses(raw: Any) -> set[str] | None:
    if raw is None:
        return set(DEFAULT_RECORD_STATUSES)
    if isinstance(raw, str):
        parts = [part.strip().lower() for part in raw.replace(";", ",").split(",")]
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(part or "").strip().lower() for part in raw]
    else:
        parts = [str(raw or "").strip().lower()]
    normalized = [part for part in parts if part]
    if not normalized:
        return set(DEFAULT_RECORD_STATUSES)
    if any(part in {"all", "*"} for part in normalized):
        return None
    allowed = set(ALL_RECORD_STATUSES)
    unknown = sorted({part for part in normalized if part not in allowed})
    if unknown:
        raise ValueError(f"unknown complaint_status for status sync scope: {', '.join(unknown)}")
    return set(normalized)


def _filter_records_by_status(records: list[dict[str, Any]], statuses: set[str] | None) -> list[dict[str, Any]]:
    if statuses is None:
        return list(records)
    return [record for record in records if str(record.get("complaint_status") or "").strip() in statuses]


def _augment_session_with_storage_supplier_context(session: Mapping[str, Any], storage_state_path: Path) -> dict[str, Any]:
    augmented = dict(session)
    storage_context = read_storage_state_supplier_context(storage_state_path)
    session_context = augmented.get("supplier_context") if isinstance(augmented.get("supplier_context"), Mapping) else {}
    session_ids = {
        str(value or "").strip()
        for value in (
            session_context.get("current_supplier_id"),
            session_context.get("current_supplier_external_id"),
            session_context.get("analytics_supplier_id"),
        )
        if str(value or "").strip()
    }
    if storage_context:
        augmented["storage_state_supplier_context"] = storage_context
    if not session_ids and storage_context:
        augmented["supplier_context"] = storage_context
    return augmented


def _can_try_direct_complaints_routes(session: Mapping[str, Any]) -> bool:
    status = str(session.get("status") or "").strip()
    if status == "session_valid_wrong_org":
        return False
    if status not in {"seller_portal_session_invalid", "seller_portal_session_probe_failed"}:
        return False
    supplier_context = session.get("supplier_context") if isinstance(session.get("supplier_context"), Mapping) else {}
    supplier_ids = {
        str(value or "").strip()
        for value in (
            supplier_context.get("current_supplier_id"),
            supplier_context.get("current_supplier_external_id"),
            supplier_context.get("analytics_supplier_id"),
        )
        if str(value or "").strip()
    }
    canonical_id = str(os.environ.get("SELLER_PORTAL_CANONICAL_SUPPLIER_ID", "") or "").strip()
    if canonical_id and supplier_ids and supplier_ids != {canonical_id}:
        return False
    return True


def _apply_status_updates(
    report: dict[str, Any],
    journal: JsonFileFeedbacksComplaintJournal,
    records: list[dict[str, Any]],
    my_complaints: Mapping[str, Any],
    *,
    run_id: str,
    record_statuses: Any = DEFAULT_RECORD_STATUSES,
) -> None:
    all_records = list(records)
    target_statuses = _normalize_record_statuses(record_statuses)
    records = _filter_records_by_status(all_records, target_statuses)
    status_counts = Counter(str(record.get("complaint_status") or "") for record in all_records)
    aggregate = report.setdefault("aggregate", {})
    aggregate.setdefault("local_records_before", len(all_records))
    aggregate.setdefault("local_waiting_records_before", int(status_counts.get("waiting_response", 0)))
    aggregate.setdefault(
        "local_satisfied_records_skipped",
        0 if target_statuses is None or "satisfied" in target_statuses else int(status_counts.get("satisfied", 0)),
    )
    aggregate.setdefault(
        "local_rejected_records_skipped",
        0 if target_statuses is None or "rejected" in target_statuses else int(status_counts.get("rejected", 0)),
    )
    aggregate.setdefault(
        "local_error_records_skipped",
        0 if target_statuses is None or "error" in target_statuses else int(status_counts.get("error", 0)),
    )
    aggregate.setdefault("target_records_before", len(records))
    aggregate.setdefault("records_scanned", len(records))
    aggregate.setdefault("record_status_scope", "all" if target_statuses is None else sorted(target_statuses))
    local_by_id = {str(record.get("feedback_id") or ""): record for record in records if record.get("feedback_id")}
    pending_rows = list(((my_complaints.get("pending") or {}).get("rows") or []))
    answered_rows = list(((my_complaints.get("answered") or {}).get("rows") or []))
    pending_stats = (my_complaints.get("pending") or {}).get("collection_stats") if isinstance(my_complaints.get("pending"), Mapping) else {}
    answered_stats = (my_complaints.get("answered") or {}).get("collection_stats") if isinstance(my_complaints.get("answered"), Mapping) else {}
    report["aggregate"]["pending_rows_read"] = len(pending_rows)
    report["aggregate"]["answered_rows_read"] = len(answered_rows)
    report["aggregate"]["pending_collection_stop_reason"] = str((pending_stats or {}).get("stop_reason") or "")
    report["aggregate"]["answered_collection_stop_reason"] = str((answered_stats or {}).get("stop_reason") or "")
    report["aggregate"]["pending_oldest_review_date"] = str((pending_stats or {}).get("oldest_review_date") or "")
    report["aggregate"]["answered_oldest_review_date"] = str((answered_stats or {}).get("oldest_review_date") or "")
    updates: list[dict[str, Any]] = []
    best_matches: dict[str, dict[str, Any]] = {}
    matched_candidate_count = 0
    weak_matches_rejected = 0
    unmatched = 0
    for row, status in [(row, "waiting_response") for row in pending_rows] + [
        (row, _status_from_answered_row(row)) for row in answered_rows
    ]:
        candidate = _match_complaint_row_to_record(row, records)
        if not candidate:
            if _weak_complaint_row_to_record(row, records):
                weak_matches_rejected += 1
            unmatched += 1
            continue
        match = candidate["record"]
        feedback_id = str(match.get("feedback_id") or "")
        if feedback_id not in local_by_id:
            unmatched += 1
            continue
        matched_candidate_count += 1
        current = {
            "feedback_id": feedback_id,
            "status": status,
            "row": row,
            "match_reason": candidate["reason"],
            "match_score": candidate["score"],
            "match_kind": candidate.get("kind") or "unknown",
        }
        previous = best_matches.get(feedback_id)
        if previous is None or _sync_match_rank(current) > _sync_match_rank(previous):
            best_matches[feedback_id] = current
    duplicate_matches_skipped = max(0, matched_candidate_count - len(best_matches))
    for feedback_id, item in best_matches.items():
        row = item["row"]
        updated = journal.update_status(
            feedback_id,
            status=str(item.get("status") or "waiting_response"),
            raw_status_text=str(row.get("displayed_status") or row.get("decision_label") or ""),
            wb_decision_text=str(row.get("wb_response_snippet") or row.get("decision_label") or ""),
            status_sync_run_id=run_id,
        )
        if updated:
            updates.append(
                {
                    "feedback_id": feedback_id,
                    "status": updated.get("complaint_status"),
                    "status_label": updated.get("complaint_status_label"),
                    "match_reason": item.get("match_reason"),
                    "match_score": item.get("match_score"),
                }
            )
    report["updates"] = updates
    report["aggregate"]["matched_local_complaints"] = len(updates)
    report["aggregate"]["statuses_updated"] = len(updates)
    report["aggregate"]["unmatched_rows"] = unmatched
    report["aggregate"]["duplicate_row_matches_skipped"] = duplicate_matches_skipped
    report["aggregate"]["weak_matches_rejected"] = weak_matches_rejected
    report["aggregate"]["updated_status_counts"] = dict(Counter(str(item.get("status") or "unknown") for item in updates))
    report["aggregate"]["match_reason_counts"] = dict(Counter(str(item.get("match_reason") or "unknown") for item in updates))
    report["aggregate"]["direct_matches"] = sum(1 for item in best_matches.values() if item.get("match_kind") == "direct_id")
    report["aggregate"]["strong_composite_matches"] = sum(
        1 for item in best_matches.values() if item.get("match_kind") == "strong_composite"
    )


def _match_complaint_row_to_record(row: Mapping[str, Any], records: list[Mapping[str, Any]]) -> dict[str, Any] | None:
    hidden_ids = row.get("hidden_ids") if isinstance(row.get("hidden_ids"), Mapping) else {}
    feedback_id = str(
        hidden_ids.get("feedback_id")
        or row.get("feedback_id")
        or row.get("seller_portal_feedback_id")
        or row.get("review_id")
        or ""
    ).strip()
    if feedback_id:
        for record in records:
            if str(record.get("feedback_id") or "").strip() == feedback_id:
                return {"record": record, "reason": "feedback_id", "score": 100, "kind": "direct_id"}
    row_text = _norm(row.get("review_text_snippet"))
    row_product = _norm(row.get("product_title"))
    row_article = _norm(row.get("supplier_article") or row.get("nm_id") or row.get("wb_article"))
    row_category = _norm(row.get("complaint_reason"))
    row_description = _norm(row.get("complaint_description"))
    best: dict[str, Any] | None = None
    for record in records:
        record_text = _record_review_text(record)
        record_product = _norm(record.get("product_name"))
        record_article = _norm(record.get("supplier_article") or record.get("nm_id"))
        record_category = _norm(record.get("wb_category_label"))
        record_description = _norm(record.get("complaint_text"))
        text_ok = _strong_text_match(row_text, record_text)
        product_ok = _strong_text_match(row_product, record_product, min_chars=12)
        article_ok = bool(row_article and record_article and (row_article == record_article or row_article in record_article or record_article in row_article))
        category_ok = bool(row_category and record_category and row_category == record_category)
        description_ok = _strong_text_match(row_description, record_description, min_chars=18)
        score = 0
        reasons: list[str] = []
        if text_ok:
            score += 45
            reasons.append("review_text")
        if description_ok:
            score += 35
            reasons.append("complaint_text")
        if product_ok:
            score += 15
            reasons.append("product")
        if article_ok:
            score += 20
            reasons.append("article")
        if category_ok:
            score += 10
            reasons.append("category")
        strong = bool(
            (description_ok and (product_ok or article_ok) and category_ok and (text_ok or article_ok))
            or (text_ok and (product_ok or article_ok) and (category_ok or description_ok))
        )
        if score < 65 or not strong:
            continue
        candidate = {"record": record, "reason": "+".join(reasons), "score": score, "kind": "strong_composite"}
        if best is None or int(candidate["score"]) > int(best["score"]):
            best = candidate
    return best


def _weak_complaint_row_to_record(row: Mapping[str, Any], records: list[Mapping[str, Any]]) -> bool:
    row_text = _norm(row.get("review_text_snippet"))
    row_product = _norm(row.get("product_title"))
    row_category = _norm(row.get("complaint_reason"))
    row_description = _norm(row.get("complaint_description"))
    for record in records:
        record_text = _record_review_text(record)
        record_product = _norm(record.get("product_name"))
        record_category = _norm(record.get("wb_category_label"))
        record_description = _norm(record.get("complaint_text"))
        weak_text = row_text and record_text and (row_text in record_text or record_text in row_text)
        weak_product = row_product and record_product and (row_product[:16] in record_product or record_product[:16] in row_product)
        weak_category = row_category and record_category and row_category == record_category
        weak_description = row_description and record_description and (
            row_description[:24] in record_description or record_description[:24] in row_description
        )
        if weak_text or weak_description or (weak_product and weak_category):
            return True
    return False


def _sync_match_rank(item: Mapping[str, Any]) -> tuple[int, int]:
    status = str(item.get("status") or "")
    final_status_rank = 2 if status in {"satisfied", "rejected"} else 1
    return (final_status_rank, int(item.get("match_score") or 0))


def _record_review_text(record: Mapping[str, Any]) -> str:
    chunks = [
        _norm(record.get("review_text")),
        _norm(record.get("pros")),
        _norm(record.get("cons")),
    ]
    return " ".join(chunk for chunk in chunks if chunk)


def _strong_text_match(left: str, right: str, *, min_chars: int = 8) -> bool:
    left = _norm(left)
    right = _norm(right)
    if len(left) < min_chars or len(right) < min_chars:
        return False
    return left in right or right in left


def _status_from_answered_row(row: Mapping[str, Any]) -> str:
    decision = str(row.get("decision_label") or "").strip()
    if decision == "approved":
        return "satisfied"
    if decision == "rejected":
        return "rejected"
    return "waiting_response"


def _norm(value: Any) -> str:
    return " ".join(str(value or "").lower().replace("ё", "е").split())


def _maybe_write(report: dict[str, Any], output_dir: Path | None, write_artifacts: bool) -> None:
    if not write_artifacts:
        return
    root = output_dir or (DEFAULT_OUTPUT_ROOT if Path("/opt/wb-core-runtime/state").exists() else LOCAL_OUTPUT_ROOT)
    run_dir = root / str(report.get("run_id") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    run_dir.mkdir(parents=True, exist_ok=True)
    json_path = run_dir / "seller_portal_feedbacks_complaints_status_sync.json"
    md_path = run_dir / "seller_portal_feedbacks_complaints_status_sync.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown_report(report), encoding="utf-8")
    report["artifact_paths"] = {"json": str(json_path), "markdown": str(md_path)}


def render_markdown_report(report: Mapping[str, Any]) -> str:
    agg = report.get("aggregate") or {}
    lines = [
        "# Seller Portal Complaints Status Sync",
        "",
        f"- Started: `{report.get('started_at')}`",
        f"- Finished: `{report.get('finished_at')}`",
        f"- Session: `{(report.get('session') or {}).get('status')}`",
        f"- Record status scope: `{agg.get('record_status_scope')}`",
        f"- Local waiting records before: `{agg.get('local_waiting_records_before', 0)}`",
        f"- Records scanned: `{agg.get('records_scanned', 0)}`",
        f"- Final records skipped: `{int(agg.get('local_satisfied_records_skipped') or 0) + int(agg.get('local_rejected_records_skipped') or 0)}`",
        f"- Error records skipped: `{agg.get('local_error_records_skipped', 0)}`",
        f"- Pending rows read: `{agg.get('pending_rows_read', 0)}`",
        f"- Answered rows read: `{agg.get('answered_rows_read', 0)}`",
        f"- Matched local complaints: `{agg.get('matched_local_complaints', 0)}`",
        f"- Statuses updated: `{agg.get('statuses_updated', 0)}`",
        f"- Unmatched rows: `{agg.get('unmatched_rows', 0)}`",
        "",
        "## Updates",
        "",
    ]
    for item in report.get("updates") or []:
        lines.append(f"- `{item.get('feedback_id')}` -> `{item.get('status_label')}` via `{item.get('match_reason')}`")
    if report.get("errors"):
        lines.extend(["", "## Errors", ""])
        for error in report["errors"]:
            lines.append(f"- `{error.get('stage')}` / `{error.get('code')}`: {error.get('message')}")
    return "\n".join(lines) + "\n"


def _compact(report: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "contract_name": report.get("contract_name"),
        "run_id": report.get("run_id"),
        "started_at": report.get("started_at"),
        "finished_at": report.get("finished_at"),
        "session": report.get("session"),
        "aggregate": report.get("aggregate"),
        "artifact_paths": report.get("artifact_paths"),
        "errors": report.get("errors"),
    }


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    main()
