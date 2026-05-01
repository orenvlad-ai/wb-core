"""Local smoke checks for read-only complaints status sync matching."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.seller_portal_feedbacks_complaints_status_sync import _apply_status_updates  # noqa: E402
from packages.application.sheet_vitrina_v1_feedbacks_complaints import JsonFileFeedbacksComplaintJournal  # noqa: E402


def main() -> None:
    with TemporaryDirectory(prefix="complaints-status-sync-smoke-") as tmp:
        journal = JsonFileFeedbacksComplaintJournal(Path(tmp))
        journal.create_or_update(_record("pending-feedback", "Текст pending", "Другое"))
        journal.create_or_update(_record("approved-feedback", "Текст approved", "Другое"))
        journal.create_or_update(_record("rejected-feedback", "Текст rejected", "Другое"))
        journal.create_or_update(
            _record(
                "pros-only-feedback",
                "",
                "Другое",
                pros="Коробка была вскрыта",
                complaint_text="Отзыв касается вскрытой коробки.",
            )
        )
        report = {"aggregate": {}, "updates": []}
        rows = {
            "pending": {
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
                "rows": [
                    _row("Текст approved", "Другое", decision="approved", status="answered"),
                    _row("Текст rejected", "Другое", decision="rejected", status="answered"),
                    _row("unmatched", "Другое", decision="approved", status="answered"),
                    _row("чужой отзыв", "Другое", decision="rejected", status="answered", description="другое описание"),
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
        if statuses["rejected-feedback"] != "rejected":
            raise AssertionError(f"rejected answered row must become rejected: {statuses}")
        if statuses["pros-only-feedback"] != "satisfied":
            raise AssertionError(f"pros/cons fallback must match the right answered row once: {statuses}")
        if report["aggregate"]["unmatched_rows"] != 2:
            raise AssertionError(f"unmatched row must be reported: {report}")
        if report["aggregate"]["duplicate_row_matches_skipped"] != 1:
            raise AssertionError(f"duplicate pending/answered match must be skipped: {report}")
        if report["aggregate"]["statuses_updated"] != 4:
            raise AssertionError(f"each local complaint may update at most once: {report}")
    print("seller_portal_feedbacks_complaints_status_sync_smoke: OK")


def _record(
    feedback_id: str,
    text: str,
    category: str,
    *,
    pros: str = "",
    complaint_text: str = "Просим проверить отзыв: тест.",
) -> dict[str, object]:
    return {
        "feedback_id": feedback_id,
        "complaint_status": "waiting_response",
        "wb_category_label": category,
        "complaint_text": complaint_text,
        "review_text": text,
        "pros": pros,
        "product_name": "Товар",
    }


def _row(
    text: str,
    category: str,
    *,
    decision: str,
    status: str,
    description: str = "Просим проверить отзыв: тест.",
) -> dict[str, object]:
    return {
        "review_text_snippet": text,
        "product_title": "Товар",
        "complaint_reason": category,
        "complaint_description": description,
        "decision_label": decision,
        "displayed_status": status,
        "wb_response_snippet": decision,
        "hidden_ids": {},
    }


if __name__ == "__main__":
    main()
