"""Local smoke checks for no-submit complaint dry-run planning."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.seller_portal_feedbacks_complaint_dry_run_plan import (  # noqa: E402
    NO_SUBMIT_MODE,
    SELLER_PORTAL_WRITE_ACTIONS_ALLOWED,
    build_aggregate,
    build_candidate_records,
    build_draft_text,
    choose_complaint_category,
    empty_modal_candidate_state,
    find_visible_actionable_row,
    no_submit_guards,
    render_markdown_report,
    select_ai_candidate_ids,
    should_open_modal_for_match,
    score_visible_row_against_exact_cursor,
    write_report_artifacts,
)


def main() -> None:
    _assert_candidate_selection()
    _assert_exact_only_guard()
    _assert_visible_row_cursor_guard()
    _assert_category_other_fallback()
    _assert_draft_text_builder()
    _assert_no_submit_guard_and_aggregate()
    _assert_report_shape()
    print("seller_portal_feedbacks_complaint_dry_run_plan_smoke: OK")


def _assert_candidate_selection() -> None:
    results = [
        _ai("no-1", "no"),
        _ai("review-1", "review"),
        _ai("yes-1", "yes"),
        _ai("yes-2", "yes"),
        _ai("review-2", "review"),
    ]
    selected = select_ai_candidate_ids(results, max_candidates=3)
    if selected != ["yes-1", "yes-2", "review-1"]:
        raise AssertionError(f"yes candidates must be selected before review candidates: {selected}")
    records = build_candidate_records([_api("yes-1"), _api("review-1"), _api("no-1")], {item["feedback_id"]: item for item in results}, selected)
    by_id = {item["feedback_id"]: item for item in records}
    if not by_id["yes-1"]["selected_for_dry_run"] or not by_id["review-1"]["selected_for_dry_run"]:
        raise AssertionError(f"yes/review candidates must be selected: {records}")
    if by_id["no-1"]["selected_for_dry_run"] or by_id["no-1"]["skip_reason"] != "skipped complaint_fit=no":
        raise AssertionError(f"no candidates must be skipped: {records}")


def _assert_exact_only_guard() -> None:
    if not should_open_modal_for_match({"match_status": "exact", "safe_for_future_submit": True}):
        raise AssertionError("exact safe match must be eligible for modal draft")
    for status in ("high", "ambiguous", "not_found"):
        if should_open_modal_for_match({"match_status": status, "safe_for_future_submit": False}):
            raise AssertionError(f"{status} must not reach modal draft")
    if should_open_modal_for_match({"match_status": "exact", "safe_for_future_submit": False}):
        raise AssertionError("exact without safety flag must not reach modal draft")


def _assert_visible_row_cursor_guard() -> None:
    expected = {
        "review_datetime": "01.05.2026 в 17:24",
        "rating": "1",
        "nm_id": "391662965",
        "supplier_article": "(Anti-Spy) iPhone 15 / 16",
    }
    row = {
        "review_datetime": "01.05.2026 в 17:24",
        "rating": "1",
        "nm_id": "391662965",
        "supplier_article": "(Anti-Spy) iPhone 15 / 16",
    }
    score = score_visible_row_against_exact_cursor(row, expected)
    if not score["exact_visible_row"]:
        raise AssertionError(f"expected visible row exact guard to pass: {score}")
    wrong_rating = {**row, "rating": "5"}
    if score_visible_row_against_exact_cursor(wrong_rating, expected)["exact_visible_row"]:
        raise AssertionError("visible row guard must reject mismatched rating")
    if score_visible_row_against_exact_cursor(wrong_rating, expected)["cursor_confirmed_visible_row"]:
        raise AssertionError("cursor-confirmed visible row guard must reject mismatched rating")
    rating_missing_row = {
        "review_datetime": "01.05.2026 в 17:24",
        "rating": "",
        "nm_id": "391662965",
        "supplier_article": "(Anti-Spy) iPhone 15 / 16",
        "product_title": "Стекло антишпион iPhone 15",
        "text_snippet": "текст",
        "cons_snippet": "брак",
        "three_dot_menu_found": True,
    }
    visible_match = find_visible_actionable_row(
        {
            "feedback_id": "f1",
            "created_at": "2026-05-01T12:24:00Z",
            "product_valuation": 1,
            "text": "текст",
            "pros": "",
            "cons": "брак",
            "nm_id": 391662965,
            "supplier_article": "(Anti-Spy) iPhone 15 / 16",
            "product_name": "Стекло антишпион iPhone 15",
        },
        [rating_missing_row],
        expected_ui=expected,
    )
    if not visible_match.get("row") or (visible_match.get("match") or {}).get("match_status") != "exact":
        raise AssertionError(f"unique cursor-confirmed visible row must pass when rating is absent in DOM: {visible_match}")


def _assert_category_other_fallback() -> None:
    categories = ["Спам-реклама в тексте", "Другое", "Нецензурная лексика"]
    if choose_complaint_category(categories, force_other=True) != "Другое":
        raise AssertionError("force_category_other must select Другое when available")
    if choose_complaint_category(["Спам-реклама в тексте"], force_other=True):
        raise AssertionError("missing Другое must not choose a weak fallback")


def _assert_draft_text_builder() -> None:
    draft = build_draft_text(
        {
            "reason": "есть формальное основание",
            "evidence": "фрагмент доказательства",
        }
    )
    if not draft.startswith("Просим проверить отзыв. Основание:"):
        raise AssertionError(f"draft must be formal: {draft}")
    if "фрагмент доказательства" not in draft or len(draft) > 500:
        raise AssertionError(f"draft must include short evidence and stay bounded: {draft}")
    long = build_draft_text({"reason": "а" * 1000, "evidence": "б" * 1000})
    if len(long) > 500:
        raise AssertionError("draft text must be bounded")


def _assert_no_submit_guard_and_aggregate() -> None:
    guards = no_submit_guards()
    if SELLER_PORTAL_WRITE_ACTIONS_ALLOWED:
        raise AssertionError("Seller Portal write actions must stay disabled")
    if guards["mode"] != NO_SUBMIT_MODE or guards["complaint_final_submit_allowed"]:
        raise AssertionError(f"no-submit guard is invalid: {guards}")
    for key in ("complaint_submit_clicked", "complaint_submit_path_called", "answer_edit_clicked"):
        if guards[key]:
            raise AssertionError(f"guard {key} must be false: {guards}")
    candidate = {
        "selected_for_dry_run": True,
        "ai": _ai("yes-1", "yes"),
        "match": {"match_status": "exact"},
        "modal": {**empty_modal_candidate_state(), "modal_opened": True, "draft_prepared": True, "submit_clicked": False},
        "skip_reason": "",
    }
    aggregate = build_aggregate([candidate])
    if aggregate["submit_clicked_count"] != 0 or aggregate["modal_draft_prepared_count"] != 1:
        raise AssertionError(f"aggregate must prove no submit and draft count: {aggregate}")


def _assert_report_shape() -> None:
    report = {
        "mode": NO_SUBMIT_MODE,
        "started_at": "2026-05-01T00:00:00Z",
        "finished_at": "2026-05-01T00:00:01Z",
        "parameters": {
            "date_from": "2026-05-01",
            "date_to": "2026-05-01",
            "stars": [1],
            "is_answered": "false",
        },
        "read_only_guards": no_submit_guards(),
        "session": {"status": "ok"},
        "candidates": [
            {
                "feedback_id": "yes-1",
                "selected_for_dry_run": True,
                "api_summary": {"created_at": "2026-05-01T12:00:00Z", "rating": "1", "nm_id": "1", "supplier_article": "a", "review_text": "text"},
                "ai": _ai("yes-1", "yes"),
                "match": {"match_status": "exact"},
                "modal": {**empty_modal_candidate_state(), "selected_category": "Другое", "draft_prepared": True, "submit_clicked": False, "draft_text": "Просим проверить отзыв."},
                "skip_reason": "",
            }
        ],
        "aggregate": build_aggregate(
            [
                {
                    "selected_for_dry_run": True,
                    "ai": _ai("yes-1", "yes"),
                    "match": {"match_status": "exact"},
                    "modal": {**empty_modal_candidate_state(), "draft_prepared": True, "submit_clicked": False},
                }
            ]
        ),
        "errors": [],
    }
    markdown = render_markdown_report(report)
    if "Seller Portal Complaint Dry-Run Plan" not in markdown or "Submit clicked count" not in markdown:
        raise AssertionError(f"markdown shape mismatch: {markdown}")
    with TemporaryDirectory(prefix="complaint-dry-run-smoke-") as tmp:
        paths = write_report_artifacts(dict(report), Path(tmp))
        if not paths["json"].exists() or not paths["markdown"].exists():
            raise AssertionError(f"report artifacts missing: {paths}")


def _api(feedback_id: str) -> dict[str, object]:
    return {
        "feedback_id": feedback_id,
        "created_at": "2026-05-01T12:00:00Z",
        "product_valuation": 1,
        "text": "Отзыв",
        "pros": "",
        "cons": "",
        "nm_id": 391662965,
        "supplier_article": "test",
        "product_name": "Товар",
        "is_answered": False,
    }


def _ai(feedback_id: str, fit: str) -> dict[str, str]:
    return {
        "feedback_id": feedback_id,
        "complaint_fit": fit,
        "complaint_fit_label": {"yes": "Да", "review": "Проверить", "no": "Нет"}.get(fit, ""),
        "category": "other",
        "category_label": "Другое",
        "reason": "короткая причина",
        "confidence": "medium",
        "confidence_label": "Средняя",
        "evidence": "короткий фрагмент",
    }


if __name__ == "__main__":
    main()
