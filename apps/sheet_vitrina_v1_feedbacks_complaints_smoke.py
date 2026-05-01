"""Smoke checks for feedback complaint runtime journal and status contract."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.sheet_vitrina_v1_feedbacks_complaints import (  # noqa: E402
    COMPLAINT_STATUS_LABELS,
    JsonFileFeedbacksComplaintJournal,
    SheetVitrinaV1FeedbacksComplaintsBlock,
)


def main() -> None:
    _assert_journal_create_dedupe_status_update()
    _assert_error_retry()
    _assert_table_contract_and_fake_sync()
    print("sheet_vitrina_v1_feedbacks_complaints_smoke: OK")


def _assert_journal_create_dedupe_status_update() -> None:
    with TemporaryDirectory(prefix="feedbacks-complaints-journal-") as tmp:
        journal = JsonFileFeedbacksComplaintJournal(Path(tmp))
        created = journal.create_or_update(_record("feedback-1"))
        if not created.created or created.duplicate:
            raise AssertionError(f"first insert must create record: {created}")
        duplicate = journal.create_or_update(_record("feedback-1"))
        if not duplicate.duplicate or duplicate.created:
            raise AssertionError(f"second insert must be deduped by feedback_id: {duplicate}")
        updated = journal.update_status("feedback-1", status="satisfied", raw_status_text="Одобрена", wb_decision_text="Принята")
        if not updated or updated["complaint_status_label"] != COMPLAINT_STATUS_LABELS["satisfied"]:
            raise AssertionError(f"status update must set satisfied label: {updated}")
        payload = SheetVitrinaV1FeedbacksComplaintsBlock(runtime_dir=Path(tmp), journal=journal).build_table()
        if payload["contract_name"] != "sheet_vitrina_v1_feedbacks_complaints" or payload["summary"]["satisfied"] != 1:
            raise AssertionError(f"table contract mismatch: {payload}")
        if payload["meta"]["auto_sync_on_page_load"] is not False:
            raise AssertionError("complaints table must not auto-sync statuses on page load")


def _assert_error_retry() -> None:
    with TemporaryDirectory(prefix="feedbacks-complaints-retry-") as tmp:
        journal = JsonFileFeedbacksComplaintJournal(Path(tmp))
        first = journal.create_or_update({**_record("feedback-err"), "complaint_status": "error", "last_error": "timeout"})
        if not first.created:
            raise AssertionError("error record must be created")
        blocked = journal.create_or_update({**_record("feedback-err"), "complaint_status": "waiting_response"})
        if not blocked.duplicate:
            raise AssertionError("error record retry must require retry_errors flag")
        retried = journal.create_or_update(
            {**_record("feedback-err"), "complaint_status": "waiting_response"},
            retry_errors=True,
        )
        if retried.duplicate or retried.record["complaint_status"] != "waiting_response":
            raise AssertionError(f"retry_errors must update error records: {retried}")


def _assert_table_contract_and_fake_sync() -> None:
    with TemporaryDirectory(prefix="feedbacks-complaints-block-") as tmp:
        journal = JsonFileFeedbacksComplaintJournal(Path(tmp))
        journal.create_or_update(_record("feedback-2"))
        sync_called: list[dict[str, object]] = []

        def fake_sync(payload: object) -> dict[str, object]:
            sync_called.append(dict(payload or {}))
            journal.update_status("feedback-2", status="rejected", raw_status_text="Отклонена")
            return {
                "contract_name": "seller_portal_feedbacks_complaints_status_sync",
                "finished_at": "2026-05-02T00:00:00Z",
                "aggregate": {"statuses_updated": 1},
            }

        block = SheetVitrinaV1FeedbacksComplaintsBlock(runtime_dir=Path(tmp), journal=journal, status_sync_runner=fake_sync)
        result = block.sync_status({"max_complaint_rows": 3})
        if not sync_called or result["aggregate"]["statuses_updated"] != 1:
            raise AssertionError(f"fake sync route did not run: {result}")
        table = block.build_table()
        if table["summary"]["rejected"] != 1:
            raise AssertionError(f"table must reflect status sync update: {table}")
        labels = {column["label"] for column in table["schema"]["columns"]}
        for required in ("Статус жалобы", "Категория WB", "Текст жалобы", "Match status"):
            if required not in labels:
                raise AssertionError(f"complaint table schema missing {required!r}: {labels}")


def _record(feedback_id: str) -> dict[str, object]:
    return {
        "feedback_id": feedback_id,
        "complaint_status": "waiting_response",
        "submitted_at": "2026-05-02T00:00:00Z",
        "wb_category_label": "Другое",
        "complaint_text": "Просим проверить отзыв: тестовое описание.",
        "match_status": "exact",
        "match_score": "1.0",
        "rating": "1",
        "review_created_at": "2026-05-01T12:00:00Z",
        "nm_id": "123456",
        "supplier_article": "ART-1",
        "product_name": "Товар",
        "review_text": "Отзыв",
        "ai_complaint_fit": "yes",
        "ai_complaint_fit_label": "Да",
        "ai_category_label": "Другое",
        "ai_reason": "Просим проверить отзыв: тестовое описание.",
        "ai_confidence": "high",
        "ai_confidence_label": "Высокая",
    }


if __name__ == "__main__":
    main()
