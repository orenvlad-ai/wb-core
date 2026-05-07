"""Local smoke checks for read-only complaints status sync matching."""

from __future__ import annotations

from pathlib import Path
import json
import os
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.seller_portal_feedbacks_complaints_status_sync import (  # noqa: E402
    _apply_status_updates,
    _augment_session_with_storage_supplier_context,
    _can_try_direct_complaints_routes,
)
from packages.application.sheet_vitrina_v1_feedbacks_complaints import JsonFileFeedbacksComplaintJournal  # noqa: E402


def main() -> None:
    with TemporaryDirectory(prefix="complaints-status-sync-smoke-") as tmp:
        journal = JsonFileFeedbacksComplaintJournal(Path(tmp))
        journal.create_or_update(_record("pending-feedback", "Текст pending", "Другое"))
        journal.create_or_update(_record("approved-feedback", "Текст approved", "Другое", last_error="submit success not confirmed"))
        journal.create_or_update(_record("rejected-feedback", "Текст rejected", "Другое"))
        journal.create_or_update(_record("direct-feedback", "Direct text", "Другое"))
        journal.create_or_update(
            _record(
                "pros-only-feedback",
                "",
                "Другое",
                pros="Коробка была вскрыта",
                complaint_text="Отзыв касается вскрытой коробки.",
            )
        )
        journal.create_or_update(_record("already-satisfied", "Текст final satisfied", "Другое", status="satisfied"))
        journal.create_or_update(_record("already-rejected", "Текст final rejected", "Другое", status="rejected"))
        journal.create_or_update(_record("existing-error", "Текст error", "Другое", status="error"))
        report = {"aggregate": {}, "updates": []}
        rows = {
            "pending": {
                "collection_stats": {
                    "stop_reason": "no_new_complaint_rows_after_scroll",
                    "oldest_review_date": "2026-04-01",
                },
                "rows": [
                    _row("Текст pending", "Другое", decision="", status="pending"),
                    _row(
                        "Коробка была вскрыта",
                        "Другое",
                        decision="",
                        status="pending",
                        description="Отзыв касается вскрытой коробки.",
                    ),
                ]
            },
            "answered": {
                "collection_stats": {
                    "stop_reason": "max_complaint_rows_reached",
                    "oldest_review_date": "2026-04-01",
                },
                "rows": [
                    _row("Текст approved", "Другое", decision="approved", status="answered"),
                    _row("Текст rejected", "Другое", decision="rejected", status="answered"),
                    _row("unrelated visible text", "Другое", decision="approved", status="answered", hidden_feedback_id="direct-feedback"),
                    _row("unmatched", "Другое", decision="approved", status="answered"),
                    _row("чужой отзыв", "Другое", decision="rejected", status="answered", description="другое описание"),
                    _row("Текст final satisfied", "Другое", decision="rejected", status="answered"),
                    _row("Текст final rejected", "Другое", decision="approved", status="answered"),
                    _row("Текст error", "Другое", decision="approved", status="answered"),
                    _row(
                        "Коробка была вскрыта",
                        "Другое",
                        decision="approved",
                        status="answered",
                        description="Отзыв касается вскрытой коробки.",
                    ),
                ]
            },
        }
        _apply_status_updates(report, journal, journal.list_records(), rows, run_id="sync-1")
        statuses = {record["feedback_id"]: record["complaint_status"] for record in journal.list_records()}
        if statuses["pending-feedback"] != "waiting_response":
            raise AssertionError(f"pending tab must stay waiting_response: {statuses}")
        if statuses["approved-feedback"] != "satisfied":
            raise AssertionError(f"approved answered row must become satisfied: {statuses}")
        approved = journal.find_by_feedback_id("approved-feedback") or {}
        if approved.get("last_error"):
            raise AssertionError(f"resolved status sync must clear stale last_error: {approved}")
        if statuses["rejected-feedback"] != "rejected":
            raise AssertionError(f"rejected answered row must become rejected: {statuses}")
        if statuses["direct-feedback"] != "satisfied":
            raise AssertionError(f"direct feedback_id row must override text mismatch: {statuses}")
        if statuses["pros-only-feedback"] != "satisfied":
            raise AssertionError(f"pros/cons fallback must match the right answered row once: {statuses}")
        if statuses["already-satisfied"] != "satisfied":
            raise AssertionError(f"final satisfied record must be skipped by default: {statuses}")
        if statuses["already-rejected"] != "rejected":
            raise AssertionError(f"final rejected record must be skipped by default: {statuses}")
        if statuses["existing-error"] != "error":
            raise AssertionError(f"error record must be skipped by default: {statuses}")
        if report["aggregate"]["records_scanned"] != 5:
            raise AssertionError(f"default status sync scope must scan only waiting records: {report}")
        if report["aggregate"]["local_satisfied_records_skipped"] != 1:
            raise AssertionError(f"satisfied records must be counted as skipped: {report}")
        if report["aggregate"]["local_rejected_records_skipped"] != 1:
            raise AssertionError(f"rejected records must be counted as skipped: {report}")
        if report["aggregate"]["local_error_records_skipped"] != 1:
            raise AssertionError(f"error records must be counted as skipped: {report}")
        if report["aggregate"]["unmatched_rows"] != 5:
            raise AssertionError(f"unmatched row must be reported: {report}")
        if report["aggregate"]["duplicate_row_matches_skipped"] != 1:
            raise AssertionError(f"duplicate pending/answered match must be skipped: {report}")
        if report["aggregate"]["weak_matches_rejected"] < 1:
            raise AssertionError(f"weak status-sync matches must be diagnosed but rejected: {report}")
        if report["aggregate"]["statuses_updated"] != 5:
            raise AssertionError(f"each local complaint may update at most once: {report}")
        if report["aggregate"]["direct_matches"] != 1:
            raise AssertionError(f"direct feedback_id sync matches must be counted: {report}")
        if report["aggregate"]["strong_composite_matches"] != 4:
            raise AssertionError(f"strong composite sync matches must be counted: {report}")
        if report["aggregate"]["pending_collection_stop_reason"] != "no_new_complaint_rows_after_scroll":
            raise AssertionError(f"deep pending collection stats must be surfaced: {report}")
        if report["aggregate"]["answered_oldest_review_date"] != "2026-04-01":
            raise AssertionError(f"historical answered scan date must be surfaced: {report}")
    _assert_direct_complaints_session_fallback()
    print("seller_portal_feedbacks_complaints_status_sync_smoke: OK")


def _assert_direct_complaints_session_fallback() -> None:
    previous = os.environ.get("SELLER_PORTAL_CANONICAL_SUPPLIER_ID")
    os.environ["SELLER_PORTAL_CANONICAL_SUPPLIER_ID"] = "canonical-supplier-id"
    try:
        with TemporaryDirectory(prefix="complaints-status-session-smoke-") as tmp:
            storage = Path(tmp) / "storage_state.json"
            storage.write_text(
                json.dumps(
                    {
                        "cookies": [
                            {
                                "name": "x-supplier-id",
                                "value": "canonical-supplier-id",
                                "domain": "seller.wildberries.ru",
                                "path": "/",
                            },
                            {
                                "name": "x-supplier-id-external",
                                "value": "canonical-supplier-id",
                                "domain": ".wildberries.ru",
                                "path": "/",
                            },
                        ],
                        "origins": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            session = _augment_session_with_storage_supplier_context(
                {
                    "ok": False,
                    "status": "seller_portal_session_invalid",
                    "supplier_context": {},
                },
                storage,
            )
            if not _can_try_direct_complaints_routes(session):
                raise AssertionError(f"canonical storage supplier should allow direct complaints probe: {session}")
            wrong = _augment_session_with_storage_supplier_context(
                {
                    "ok": False,
                    "status": "seller_portal_session_invalid",
                    "supplier_context": {"current_supplier_id": "wrong-supplier-id"},
                },
                storage,
            )
            if _can_try_direct_complaints_routes(wrong):
                raise AssertionError(f"wrong supplier must not allow direct complaints probe: {wrong}")
    finally:
        if previous is None:
            os.environ.pop("SELLER_PORTAL_CANONICAL_SUPPLIER_ID", None)
        else:
            os.environ["SELLER_PORTAL_CANONICAL_SUPPLIER_ID"] = previous


def _record(
    feedback_id: str,
    text: str,
    category: str,
    *,
    pros: str = "",
    complaint_text: str = "Просим проверить отзыв: тест.",
    last_error: str = "",
    status: str = "waiting_response",
) -> dict[str, object]:
    return {
        "feedback_id": feedback_id,
        "complaint_status": status,
        "wb_category_label": category,
        "complaint_text": complaint_text,
        "review_text": text,
        "pros": pros,
        "product_name": "Защитное стекло антишпион на iPhone 17 Pro Max",
        "last_error": last_error,
    }


def _row(
    text: str,
    category: str,
    *,
    decision: str,
    status: str,
    description: str = "Просим проверить отзыв: тест.",
    hidden_feedback_id: str = "",
) -> dict[str, object]:
    return {
        "review_text_snippet": text,
        "product_title": "Защитное стекло антишпион на iPhone 17 Pro Max",
        "complaint_reason": category,
        "complaint_description": description,
        "decision_label": decision,
        "displayed_status": status,
        "wb_response_snippet": decision,
        "hidden_ids": {"feedback_id": hidden_feedback_id} if hidden_feedback_id else {},
    }


if __name__ == "__main__":
    main()
