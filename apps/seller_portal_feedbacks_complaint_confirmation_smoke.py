"""Local smoke checks for read-only complaint confirmation matching."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.seller_portal_feedbacks_complaint_confirmation import (  # noqa: E402
    ConfirmationConfig,
    READ_ONLY_MODE,
    apply_journal_confirmation_result,
    evaluate_confirmation,
)
from apps.seller_portal_feedbacks_complaints_scout import parse_my_complaints_rows_from_html  # noqa: E402
from apps.seller_portal_relogin_session import DEFAULT_STORAGE_STATE_PATH, DEFAULT_WB_BOT_PYTHON  # noqa: E402
from packages.application.sheet_vitrina_v1_feedbacks_complaints import JsonFileFeedbacksComplaintJournal  # noqa: E402


def main() -> None:
    record = _record()
    direct_pending = evaluate_confirmation(record, {"pending": {"rows": [_row(hidden_feedback_id="target-feedback")]}, "answered": {"rows": []}})
    if direct_pending["result"] != "confirmed_pending":
        raise AssertionError(f"direct id pending must confirm waiting_response: {direct_pending}")

    direct_approved = evaluate_confirmation(
        record,
        {"pending": {"rows": []}, "answered": {"rows": [_row(hidden_feedback_id="target-feedback", decision="approved")]}},
    )
    if direct_approved["result"] != "confirmed_satisfied":
        raise AssertionError(f"direct id approved must confirm satisfied: {direct_approved}")

    direct_rejected = evaluate_confirmation(
        record,
        {"pending": {"rows": []}, "answered": {"rows": [_row(hidden_feedback_id="target-feedback", decision="rejected")]}},
    )
    if direct_rejected["result"] != "confirmed_rejected":
        raise AssertionError(f"direct id rejected must confirm rejected: {direct_rejected}")

    composite = evaluate_confirmation(record, {"pending": {"rows": [_row()]}, "answered": {"rows": []}})
    if composite["result"] != "confirmed_pending" or not composite["composite_matches"]:
        raise AssertionError(f"strong composite must confirm waiting_response: {composite}")

    weak = evaluate_confirmation(
        record,
        {
            "pending": {
                "rows": [
                    _row(
                        product="Другой товар",
                        category="",
                        description="",
                        text="Коробка была вскрыта",
                    )
                ]
            },
            "answered": {"rows": []},
        },
    )
    if weak["result"] != "unconfirmed" or not weak["weak_matches_rejected"]:
        raise AssertionError(f"weak text-only match must remain unconfirmed: {weak}")

    no_match = evaluate_confirmation(
        record,
        {
            "pending": {
                "rows": [
                    _row(
                        text="чужой отзыв",
                        product="Другой товар",
                        article="other-article",
                        nm_id="123456789",
                        category="Нецензурная лексика",
                        description="Совсем другое описание.",
                    )
                ]
            },
            "answered": {"rows": []},
        },
    )
    if no_match["result"] != "unconfirmed":
        raise AssertionError(f"no match must remain unconfirmed: {no_match}")

    parsed = parse_my_complaints_rows_from_html(
        """
        <div data-scout-complaint-row>
          <div>Защитное стекло антишпион на iPhone 17 Pro Max</div>
          <div>Арт: (Anti-Spy) iPhone 17 Pro Max</div>
          <div>Другое</div>
          <div>Отзыв касается вскрытой коробки.</div>
          <div>01.05.2026 в 19:42</div>
        </div>
        """,
        max_rows=1,
    )[0]
    if parsed["complaint_reason"] != "Другое" or parsed["complaint_description"] != "Отзыв касается вскрытой коробки.":
        raise AssertionError(f"complaint row parser must extract category/description from raw lines: {parsed}")

    with TemporaryDirectory(prefix="complaint-confirmation-smoke-") as tmp:
        journal = JsonFileFeedbacksComplaintJournal(Path(tmp))
        journal.create_or_update({**record, "complaint_status": "error", "last_error": "submit success not confirmed"})
        config = ConfirmationConfig(
            feedback_id="target-feedback",
            mode=READ_ONLY_MODE,
            runtime_dir=Path(tmp),
            storage_state_path=DEFAULT_STORAGE_STATE_PATH,
            wb_bot_python=DEFAULT_WB_BOT_PYTHON,
            output_dir=Path(tmp),
            start_url="https://seller.wildberries.ru",
            max_complaint_rows=20,
            headless=True,
            timeout_ms=5000,
            write_artifacts=False,
            update_journal=True,
        )
        report = {
            "confirmation": direct_pending,
            "original_review": {"complaint_status": "unknown", "complaint_action_still_visible": False},
            "journal_update": {},
        }
        apply_journal_confirmation_result(config, journal, report, run_id="confirm-1")
        updated = journal.find_by_feedback_id("target-feedback") or {}
        if updated.get("complaint_status") != "waiting_response" or updated.get("last_error"):
            raise AssertionError(f"confirmed pending must update journal and clear stale error: {updated}")
        if len(journal.list_records()) != 1:
            raise AssertionError("confirmation must not create duplicate complaint records")

    print("seller_portal_feedbacks_complaint_confirmation_smoke: OK")


def _record() -> dict[str, object]:
    return {
        "feedback_id": "target-feedback",
        "complaint_status": "error",
        "wb_category_label": "Другое",
        "complaint_text": "Отзыв касается вскрытой коробки.",
        "review_text": "",
        "pros": "Коробка была вскрыта",
        "product_name": "Защитное стекло антишпион на iPhone 17 Pro Max",
        "supplier_article": "(Anti-Spy) iPhone 17 Pro Max",
        "nm_id": "497416559",
        "rating": "1",
        "review_created_at": "2026-05-01T14:42:32Z",
    }


def _row(
    *,
    hidden_feedback_id: str = "",
    decision: str = "",
    product: str = "Защитное стекло антишпион на iPhone 17 Pro Max",
    article: str = "(Anti-Spy) iPhone 17 Pro Max",
    nm_id: str = "497416559",
    category: str = "Другое",
    description: str = "Отзыв касается вскрытой коробки.",
    text: str = "Коробка была вскрыта",
) -> dict[str, object]:
    return {
        "hidden_ids": {"feedback_id": hidden_feedback_id} if hidden_feedback_id else {},
        "product_title": product,
        "supplier_article": article,
        "nm_id": nm_id,
        "complaint_reason": category,
        "complaint_description": description,
        "review_text_snippet": text,
        "review_datetime": "01.05.2026 в 19:42",
        "review_rating": "1",
        "decision_label": decision,
        "displayed_status": "answered" if decision else "pending",
    }


if __name__ == "__main__":
    main()
