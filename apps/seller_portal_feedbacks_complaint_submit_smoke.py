"""Local smoke checks for controlled complaint submit safety gates."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.seller_portal_feedbacks_complaint_submit import (  # noqa: E402
    MAX_SUBMIT_HARD_CAP,
    SubmitConfig,
    build_submit_aggregate,
    enforce_submit_guards,
    is_reason_submit_ready,
    journal_record_for_submit,
    run_submit,
    select_submit_candidate_ids,
)
from packages.application.sheet_vitrina_v1_feedbacks_complaints import (  # noqa: E402
    JsonFileFeedbacksComplaintJournal,
)


def main() -> None:
    _assert_selection_rules()
    _assert_exact_and_reason_guards()
    _assert_duplicate_guard_storage()
    _assert_explicit_submit_flag_required()
    _assert_journal_record_shape()
    print("seller_portal_feedbacks_complaint_submit_smoke: OK")


def _assert_selection_rules() -> None:
    results = [
        _ai("no-1", "no"),
        _ai("review-1", "review"),
        _ai("yes-1", "yes"),
        _ai("yes-2", "yes"),
    ]
    if select_submit_candidate_ids(results, max_submit=3, include_review=True) != ["yes-1", "yes-2", "review-1"]:
        raise AssertionError("submit selection must prefer yes before review")
    if select_submit_candidate_ids(results, max_submit=3, include_review=False) != ["yes-1", "yes-2"]:
        raise AssertionError("include_review=0 must skip review candidates")
    if MAX_SUBMIT_HARD_CAP != 3:
        raise AssertionError("controlled submit hard cap must remain 3")


def _assert_exact_and_reason_guards() -> None:
    candidates = [
        _candidate("exact-good", "yes", "exact", "Просим проверить отзыв: покупатель описывает получение заказа."),
        _candidate("high", "yes", "high", "Просим проверить отзыв: покупатель описывает получение заказа."),
        _candidate("no-fit", "no", "exact", "Жалобу не подавать: обычная товарная претензия."),
        _candidate("bad-reason", "yes", "exact", "Недостаточно данных."),
    ]
    enforce_submit_guards(candidates)
    by_id = {item["feedback_id"]: item for item in candidates}
    if by_id["exact-good"].get("skip_reason"):
        raise AssertionError(f"exact yes with ready reason must pass: {by_id['exact-good']}")
    if "not exact" not in by_id["high"].get("skip_reason", ""):
        raise AssertionError(f"high match must be blocked: {by_id['high']}")
    if "not submit-eligible" not in by_id["no-fit"].get("skip_reason", ""):
        raise AssertionError(f"complaint_fit=no must be blocked: {by_id['no-fit']}")
    if "placeholder" not in by_id["bad-reason"].get("skip_reason", ""):
        raise AssertionError(f"diagnostic reason must be blocked: {by_id['bad-reason']}")
    if not is_reason_submit_ready("Просим проверить отзыв: тестовое описание."):
        raise AssertionError("ready complaint reason must pass")
    if is_reason_submit_ready("Основание неясно."):
        raise AssertionError("diagnostic reason must not pass")


def _assert_duplicate_guard_storage() -> None:
    with TemporaryDirectory(prefix="submit-duplicate-smoke-") as tmp:
        journal = JsonFileFeedbacksComplaintJournal(Path(tmp))
        first = journal.create_or_update({"feedback_id": "dupe", "complaint_status": "waiting_response"})
        if not first.created:
            raise AssertionError("first complaint record must be created")
        second = journal.create_or_update({"feedback_id": "dupe", "complaint_status": "waiting_response"})
        if not second.duplicate:
            raise AssertionError("same feedback_id must be deduped before submit")


def _assert_explicit_submit_flag_required() -> None:
    with TemporaryDirectory(prefix="submit-flag-smoke-") as tmp:
        config = SubmitConfig(
            date_from="2026-05-01",
            date_to="2026-05-01",
            stars=(1,),
            is_answered="false",
            max_api_rows=1,
            max_submit=1,
            include_review=True,
            dry_run=False,
            require_exact=True,
            retry_errors=False,
            submit_confirmation=False,
            runtime_dir=Path(tmp),
            storage_state_path=Path(tmp) / "storage_state.json",
            wb_bot_python=Path("/usr/bin/python3"),
            output_dir=Path(tmp),
            start_url="https://seller.wildberries.ru",
            headless=True,
            timeout_ms=5000,
            write_artifacts=False,
        )
        try:
            run_submit(config)
        except RuntimeError as exc:
            if "requires --i-understand-this-submits-complaints" not in str(exc):
                raise
            return
        raise AssertionError("real submit must require explicit confirmation flag before any API/browser work")


def _assert_journal_record_shape() -> None:
    record = journal_record_for_submit(
        _api("feedback-1"),
        _candidate("feedback-1", "yes", "exact", "Просим проверить отзыв: тестовое описание."),
        {
            "selected_category": "Другое",
            "draft_text": "Просим проверить отзыв: тестовое описание.",
            "blocker": "",
            "success_state": {"text": "Жалоба отправлена"},
        },
        status="waiting_response",
        run_id="run-1",
    )
    if record["complaint_status"] != "waiting_response" or record["wb_category_label"] != "Другое":
        raise AssertionError(f"journal record must use waiting response and WB category: {record}")
    aggregate = build_submit_aggregate(
        [
            {
                "selected_for_dry_run": True,
                "ai": _ai("feedback-1", "yes"),
                "match": {"match_status": "exact"},
                "modal": {"submit_clicked": True, "submit_success": True},
            }
        ]
    )
    if aggregate["submit_clicked_count"] != 1 or aggregate["submitted_count"] != 1:
        raise AssertionError(f"aggregate must count controlled submit: {aggregate}")


def _candidate(feedback_id: str, fit: str, match_status: str, reason: str) -> dict[str, object]:
    return {
        "feedback_id": feedback_id,
        "selected_for_dry_run": True,
        "ai": {**_ai(feedback_id, fit), "reason": reason},
        "match": {"match_status": match_status, "safe_for_future_submit": match_status == "exact"},
        "modal": {},
        "skip_reason": "",
    }


def _api(feedback_id: str) -> dict[str, object]:
    return {
        "feedback_id": feedback_id,
        "created_at": "2026-05-01T12:00:00Z",
        "product_valuation": 1,
        "text": "Отзыв",
        "pros": "",
        "cons": "",
        "nm_id": 123456,
        "supplier_article": "ART-1",
        "product_name": "Товар",
        "is_answered": False,
    }


def _ai(feedback_id: str, fit: str) -> dict[str, str]:
    return {
        "feedback_id": feedback_id,
        "complaint_fit": fit,
        "complaint_fit_label": {"yes": "Да", "review": "Проверить", "no": "Нет"}.get(fit, ""),
        "category_label": "Другое",
        "reason": "Просим проверить отзыв: тестовое описание.",
        "confidence": "high",
        "confidence_label": "Высокая",
    }


if __name__ == "__main__":
    main()
