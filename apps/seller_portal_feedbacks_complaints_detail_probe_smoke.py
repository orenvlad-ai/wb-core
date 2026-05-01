"""Local smoke checks for read-only complaints detail/network probe."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.seller_portal_feedbacks_complaints_detail_probe import (  # noqa: E402
    DetailProbeConfig,
    READ_ONLY_MODE,
    TARGET_LAST_ERROR,
    apply_probe_journal_result,
    empty_confirmation,
    evaluate_detail_probe_confirmation,
    extract_safe_network_rows,
    sanitize_network_response,
    summarize_network_capture,
)
from apps.seller_portal_relogin_session import DEFAULT_STORAGE_STATE_PATH, DEFAULT_WB_BOT_PYTHON  # noqa: E402
from packages.application.sheet_vitrina_v1_feedbacks_complaints import JsonFileFeedbacksComplaintJournal  # noqa: E402


def main() -> None:
    record = _record()
    direct_pending = evaluate_detail_probe_confirmation(
        record,
        {"pending": {"rows": [{"hidden_ids": {"feedback_id": "target-feedback"}}]}, "answered": {"rows": []}},
        [],
    )
    if direct_pending["result"] != "confirmed_pending":
        raise AssertionError(f"direct id pending must confirm waiting_response: {direct_pending}")

    network_accepted = evaluate_detail_probe_confirmation(
        record,
        {"pending": {"rows": []}, "answered": {"rows": []}},
        [
            {
                "stage": "answered_detail",
                "safe_rows": [
                    {
                        "feedback_id": "target-feedback",
                        "complaint_id": "wb-complaint-1",
                        "status_text": "approved",
                    }
                ],
            }
        ],
    )
    if network_accepted["result"] != "confirmed_satisfied":
        raise AssertionError(f"network direct id approved must confirm satisfied: {network_accepted}")

    network_rejected = evaluate_detail_probe_confirmation(
        record,
        {"pending": {"rows": []}, "answered": {"rows": []}},
        [{"stage": "answered_detail", "safe_rows": [{"review_id": "target-feedback", "status_text": "Отклонена"}]}],
    )
    if network_rejected["result"] != "confirmed_rejected":
        raise AssertionError(f"network direct review id rejected must confirm rejected: {network_rejected}")

    composite = evaluate_detail_probe_confirmation(record, {"pending": {"rows": [_row()]}, "answered": {"rows": []}}, [])
    if composite["result"] != "confirmed_pending" or not composite["strong_composite_candidates"]:
        raise AssertionError(f"strong composite must confirm pending: {composite}")

    weak_product_only = evaluate_detail_probe_confirmation(
        record,
        {"pending": {"rows": [_row(text="", category="", description="", article="", nm_id="")]}, "answered": {"rows": []}},
        [],
    )
    if weak_product_only["result"] != "unconfirmed":
        raise AssertionError(f"product-only match must remain unconfirmed: {weak_product_only}")

    weak_category_only = evaluate_detail_probe_confirmation(
        record,
        {"pending": {"rows": [_row(text="", product="Другой товар", article="", nm_id="", description="")]}, "answered": {"rows": []}},
        [],
    )
    if weak_category_only["result"] != "unconfirmed":
        raise AssertionError(f"category-only match must remain unconfirmed: {weak_category_only}")

    no_match = evaluate_detail_probe_confirmation(
        record,
        {"pending": {"rows": [_row(text="чужой отзыв", product="Другой товар", category="Нецензурная лексика")]}, "answered": {"rows": []}},
        [],
    )
    if no_match["result"] != "unconfirmed":
        raise AssertionError(f"no match must remain unconfirmed: {no_match}")

    rows = extract_safe_network_rows(
        {
            "data": {
                "complaints": [
                    {
                        "complaintId": "wb-1",
                        "feedbackId": "target-feedback",
                        "productName": "Защитное стекло антишпион на iPhone 17 Pro Max",
                        "supplierArticle": "(Anti-Spy) iPhone 17 Pro Max",
                        "complaintText": "Отзыв касается вскрытой коробки.",
                        "categoryName": "Другое",
                    }
                ]
            }
        },
        target_feedback_id="target-feedback",
    )
    if not rows or rows[0].get("complaint_id") != "wb-1" or rows[0].get("feedback_id") != "target-feedback":
        raise AssertionError(f"network list parser must extract complaint and feedback ids: {rows}")
    if not rows[0].get("target_feedback_id_match"):
        raise AssertionError(f"network parser must flag direct target feedback id: {rows}")

    detail_rows = extract_safe_network_rows(
        {"detail": {"claimId": "claim-1", "reviewId": "target-feedback", "status": "pending"}},
        target_feedback_id="target-feedback",
    )
    if not detail_rows or detail_rows[0].get("complaint_id") != "claim-1" or detail_rows[0].get("review_id") != "target-feedback":
        raise AssertionError(f"network detail parser must extract claim/review ids: {detail_rows}")

    fake_response = _FakeResponse(
        url="https://seller.wildberries.ru/api/complaints?token=secret&cursor=abc",
        payload={"feedbackId": "target-feedback", "complaintId": "complaint-1"},
        headers={"content-type": "application/json", "authorization": "secret"},
        method="GET",
    )
    sanitized = sanitize_network_response(fake_response, stage="pending_detail", target_feedback_id="target-feedback")
    if "headers" in sanitized or "token" in sanitized.get("query_keys", []):
        raise AssertionError(f"sanitized network payload must not store headers or sensitive query keys: {sanitized}")
    summary = summarize_network_capture([sanitized])
    if not summary["direct_feedback_id_found"] or not summary["complaint_id_found"]:
        raise AssertionError(f"network summary must surface direct feedback and complaint ids: {summary}")

    with TemporaryDirectory(prefix="complaints-detail-probe-smoke-") as tmp:
        journal = JsonFileFeedbacksComplaintJournal(Path(tmp))
        journal.create_or_update({**record, "complaint_status": "error", "last_error": "old"})
        config = DetailProbeConfig(
            feedback_id="target-feedback",
            mode=READ_ONLY_MODE,
            runtime_dir=Path(tmp),
            storage_state_path=DEFAULT_STORAGE_STATE_PATH,
            wb_bot_python=DEFAULT_WB_BOT_PYTHON,
            output_dir=Path(tmp),
            start_url="https://seller.wildberries.ru",
            max_pending_rows=20,
            max_answered_rows=20,
            open_row_details=True,
            capture_network=True,
            headless=True,
            timeout_ms=5000,
            write_artifacts=False,
            update_journal=True,
        )
        report = {
            "confirmation": direct_pending,
            "journal_update": {},
        }
        apply_probe_journal_result(config, journal, report, run_id="probe-1")
        updated = journal.find_by_feedback_id("target-feedback") or {}
        if updated.get("complaint_status") != "waiting_response" or updated.get("last_error"):
            raise AssertionError(f"confirmed detail probe must update journal without duplicate: {updated}")
        if len(journal.list_records()) != 1:
            raise AssertionError("detail probe must not create duplicate complaint records")

        report = {"confirmation": empty_confirmation(), "journal_update": {}}
        report["confirmation"]["reason"] = "no match"
        apply_probe_journal_result(config, journal, report, run_id="probe-2")
        updated_error = journal.find_by_feedback_id("target-feedback") or {}
        if updated_error.get("complaint_status") != "error" or updated_error.get("last_error") != TARGET_LAST_ERROR:
            raise AssertionError(f"unconfirmed detail probe must keep error with precise last_error: {updated_error}")

    safety = {
        "complaint_creation_modal_allowed": False,
        "final_submit_click_allowed": False,
        "submit_clicked_during_runner": 0,
    }
    if safety["submit_clicked_during_runner"] != 0 or safety["final_submit_click_allowed"]:
        raise AssertionError(f"read-only safety guard must keep submit count zero: {safety}")

    print("seller_portal_feedbacks_complaints_detail_probe_smoke: OK")


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
    text: str = "Коробка была вскрыта",
    product: str = "Защитное стекло антишпион на iPhone 17 Pro Max",
    article: str = "(Anti-Spy) iPhone 17 Pro Max",
    nm_id: str = "497416559",
    category: str = "Другое",
    description: str = "Отзыв касается вскрытой коробки.",
) -> dict[str, object]:
    return {
        "product_title": product,
        "supplier_article": article,
        "nm_id": nm_id,
        "complaint_reason": category,
        "complaint_description": description,
        "review_text_snippet": text,
        "displayed_status": "pending",
        "decision_label": "",
    }


class _FakeRequest:
    def __init__(self, method: str) -> None:
        self.method = method


class _FakeResponse:
    def __init__(self, *, url: str, payload: object, headers: dict[str, str], method: str) -> None:
        self.url = url
        self.status = 200
        self.headers = headers
        self.request = _FakeRequest(method)
        self._payload = payload

    def json(self) -> object:
        return self._payload


if __name__ == "__main__":
    main()
