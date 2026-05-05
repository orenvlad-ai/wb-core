"""Local smoke checks for controlled complaint submit safety gates."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.seller_portal_feedbacks_complaint_submit import (  # noqa: E402
    MAX_SUBMIT_HARD_CAP,
    SUBMIT_RESULT_CONFIRMED_NETWORK_ERROR,
    SUBMIT_RESULT_CONFIRMED_SUCCESS,
    SUBMIT_RESULT_CONFIRMED_VALIDATION_ERROR,
    SUBMIT_RESULT_UNCONFIRMED_AFTER_CLICK,
    SubmitConfig,
    build_submit_aggregate,
    classify_submit_result,
    enforce_submit_guards,
    is_reason_submit_ready,
    journal_record_for_submit,
    mark_denied_candidates,
    normalize_deny_feedback_ids,
    order_candidates_for_actionability,
    run_submit,
    sanitize_submit_network_request,
    sanitize_submit_network_response,
    select_submit_candidate_ids,
)
from packages.application.sheet_vitrina_v1_feedbacks_complaints import (  # noqa: E402
    JsonFileFeedbacksComplaintJournal,
)


def main() -> None:
    _assert_selection_rules()
    _assert_exact_and_reason_guards()
    _assert_duplicate_guard_storage()
    _assert_denylist_and_selection_rules()
    _assert_explicit_submit_flag_required()
    _assert_target_feedback_id_config()
    _assert_submit_result_classification()
    _assert_network_sanitizer()
    _assert_request_payload_sanitizer()
    _assert_journal_record_shape()
    print("seller_portal_feedbacks_complaint_submit_smoke: OK")


def _assert_selection_rules() -> None:
    results = [
        _ai("no-1", "no"),
        _ai("review-1", "review"),
        _ai("yes-1", "yes"),
        _ai("yes-2", "yes"),
    ]
    if select_submit_candidate_ids(results, max_submit=1, include_review=True) != ["yes-1"]:
        raise AssertionError("submit selection must prefer yes before review")
    if select_submit_candidate_ids(results, max_submit=1, max_candidates=3, include_review=True) != ["yes-1", "yes-2", "review-1"]:
        raise AssertionError("actionability iteration must check all bounded yes candidates before review while final submit cap stays 1")
    if select_submit_candidate_ids(results, max_submit=1, max_candidates=3, include_review=False) != ["yes-1", "yes-2"]:
        raise AssertionError("include_review=0 iteration must still skip review candidates")
    if select_submit_candidate_ids(results, max_submit=1, include_review=False) != ["yes-1"]:
        raise AssertionError("include_review=0 must skip review candidates")
    api_order_candidates = [
        _candidate("review-1", "review", "exact", "Просим проверить отзыв: review."),
        _candidate("yes-1", "yes", "exact", "Просим проверить отзыв: yes."),
        _candidate("yes-2", "yes", "exact", "Просим проверить отзыв: yes."),
    ]
    ordered = order_candidates_for_actionability(api_order_candidates, selected_ids=["yes-1", "yes-2", "review-1"])
    if [item["feedback_id"] for item in ordered] != ["yes-1", "yes-2", "review-1"]:
        raise AssertionError("resolver attempts must follow selected yes-before-review order, not API row order")
    if MAX_SUBMIT_HARD_CAP != 1:
        raise AssertionError("controlled submit hard cap must remain 1")


def _assert_exact_and_reason_guards() -> None:
    candidates = [
        _candidate("exact-good", "yes", "exact", "Просим проверить отзыв: покупатель описывает получение заказа."),
        _candidate("high", "yes", "high", "Просим проверить отзыв: покупатель описывает получение заказа."),
        _candidate("no-fit", "no", "exact", "Жалобу не подавать: обычная товарная претензия."),
        _candidate("bad-reason", "yes", "exact", "Недостаточно данных."),
        {
            **_candidate("tag-contradiction", "review", "exact", "Просим проверить отзыв: низкая оценка без текста и описания."),
            "api_summary": {"review_tags": ["Плохое качество"]},
        },
    ]
    enforce_submit_guards(candidates)
    by_id = {item["feedback_id"]: item for item in candidates}
    if by_id["exact-good"].get("skip_reason"):
        raise AssertionError(f"exact yes with ready reason must pass: {by_id['exact-good']}")
    if by_id["high"].get("skip_reason") or not by_id["high"].get("filter_aware_resolver_required"):
        raise AssertionError(f"high preliminary match must defer to filter-aware resolver: {by_id['high']}")
    if "not submit-eligible" not in by_id["no-fit"].get("skip_reason", ""):
        raise AssertionError(f"complaint_fit=no must be blocked: {by_id['no-fit']}")
    if "placeholder" not in by_id["bad-reason"].get("skip_reason", ""):
        raise AssertionError(f"diagnostic reason must be blocked: {by_id['bad-reason']}")
    if by_id["tag-contradiction"].get("skip_reason") != "reason_contradicts_review_tags":
        raise AssertionError(f"reason/tag contradiction must be blocked: {by_id['tag-contradiction']}")
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


def _assert_denylist_and_selection_rules() -> None:
    results = [_ai("GPe9vrq0kctlSfobrgq2", "yes"), _ai("fresh-yes", "yes"), _ai("fresh-review", "review")]
    deny = normalize_deny_feedback_ids([])
    if "GPe9vrq0kctlSfobrgq2" not in deny:
        raise AssertionError("historical uncertain feedback_id must be denied by default")
    if "fdQpHhNXTosEkArTHAZF" not in deny:
        raise AssertionError("previous successful empty-description feedback_id must be denied by default")
    selected = select_submit_candidate_ids(results, max_submit=1, include_review=True, deny_feedback_ids=deny)
    if selected != ["fresh-yes"]:
        raise AssertionError(f"denylist must force selection to skip old feedback_id: {selected}")
    candidates = [_candidate("GPe9vrq0kctlSfobrgq2", "yes", "exact", "Просим проверить отзыв: тестовое описание.")]
    mark_denied_candidates(candidates, deny)
    if "hard-denylisted" not in candidates[0].get("skip_reason", ""):
        raise AssertionError(f"denylisted candidate must be blocked: {candidates[0]}")


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


def _assert_target_feedback_id_config() -> None:
    with TemporaryDirectory(prefix="submit-target-config-smoke-") as tmp:
        config = SubmitConfig(
            date_from="2026-05-01",
            date_to="2026-05-01",
            stars=(1,),
            is_answered="all",
            max_api_rows=50,
            max_submit=1,
            include_review=True,
            dry_run=True,
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
            target_feedback_id="Ia1xepZzSXnGzIk4J5jY",
        )
    if config.target_feedback_id != "Ia1xepZzSXnGzIk4J5jY" or not config.dry_run:
        raise AssertionError(f"target feedback no-submit diagnostic config must be explicit: {config}")


def _assert_submit_result_classification() -> None:
    click_only = {
        "submit_clicked": True,
        "submit_network_capture": {"mutating_statuses": []},
        "success_state": {"seen": False},
        "complaint_status_after_submit": {"submitted_like": False},
        "post_submit_row_state": {"complaint_action_still_visible": "unknown"},
        "visible_messages_after_click": [],
        "modal_state_after_click": {"validation_messages": []},
    }
    if classify_submit_result(click_only) != SUBMIT_RESULT_UNCONFIRMED_AFTER_CLICK:
        raise AssertionError("click alone must not be classified as success")
    success = {**click_only, "success_state": {"seen": True, "text": "Жалоба отправлена"}}
    if classify_submit_result(success) != SUBMIT_RESULT_CONFIRMED_SUCCESS:
        raise AssertionError("success toast must confirm success")
    validation = {**click_only, "visible_messages_after_click": ["Заполните обязательное поле"]}
    if classify_submit_result(validation) != SUBMIT_RESULT_CONFIRMED_VALIDATION_ERROR:
        raise AssertionError("validation message must classify validation failure")
    network_error = {**click_only, "submit_network_capture": {"mutating_statuses": [500]}}
    if classify_submit_result(network_error) != SUBMIT_RESULT_CONFIRMED_NETWORK_ERROR:
        raise AssertionError("5xx mutating response must classify network failure")
    missing_payload = {
        **click_only,
        "success_state": {"seen": True, "text": "Жалоба отправлена"},
        "submit_network_capture": {
            "mutating_statuses": [200],
            "submit_payload_checked": True,
            "submit_payload_has_description": False,
        },
    }
    if classify_submit_result(missing_payload) != SUBMIT_RESULT_UNCONFIRMED_AFTER_CLICK:
        raise AssertionError("success toast with captured missing description payload must not classify as success")


def _assert_network_sanitizer() -> None:
    response = _FakeResponse(
        url="https://seller-reviews.wildberries.ru/api/complaints?token=secret&limit=1",
        method="POST",
        status=200,
        payload={
            "success": True,
            "complaintId": "complaint-1",
            "feedbackId": "feedback-1",
            "authorization": "must-not-leak",
        },
    )
    item = sanitize_submit_network_response(response, stage="submit", target_feedback_id="feedback-1")
    raw = repr(item).lower()
    if "must-not-leak" in raw or "token" in raw or "authorization" in raw:
        raise AssertionError(f"network sanitizer leaked forbidden data: {item}")
    if item.get("method") != "POST" or item.get("status") != 200 or "complaintId" in raw:
        raise AssertionError(f"network sanitizer must expose safe normalized facts, not raw body: {item}")
    if not item.get("safe_body"):
        raise AssertionError(f"network sanitizer must retain safe body facts: {item}")


def _assert_request_payload_sanitizer() -> None:
    intended = "Просим проверить отзыв: тестовое описание."
    request = _FakeRequestWithBody(
        url="https://seller-reviews.wildberries.ru/ns/fa-seller-api/reviews-ext-seller-portal/api/v1/feedbacks/complaints?token=secret",
        method="PATCH",
        payload={
            "feedbackId": "feedback-1",
            "description": intended,
            "cookie": "must-not-leak",
            "nested": {"authorization": "must-not-leak"},
        },
    )
    item = sanitize_submit_network_request(request, stage="submit", target_feedback_id="feedback-1", intended_description=intended)
    raw = repr(item).lower()
    if "must-not-leak" in raw or "token" in raw or "authorization" in raw or "cookie" in raw:
        raise AssertionError(f"request sanitizer leaked forbidden data: {item}")
    summary = item.get("safe_body_summary") or {}
    if not summary.get("has_description") or not summary.get("intended_description_seen") or summary.get("description_length") != len(intended):
        raise AssertionError(f"request sanitizer must prove non-empty description: {item}")
    missing = sanitize_submit_network_request(
        _FakeRequestWithBody(
            url="https://seller-reviews.wildberries.ru/ns/fa-seller-api/reviews-ext-seller-portal/api/v1/feedbacks/complaints",
            method="PATCH",
            payload={"feedbackId": "feedback-1", "category": "Другое"},
        ),
        stage="submit",
        target_feedback_id="feedback-1",
        intended_description=intended,
    )
    if (missing.get("safe_body_summary") or {}).get("has_description"):
        raise AssertionError(f"missing payload description must be explicit false: {missing}")


def _assert_journal_record_shape() -> None:
    record = journal_record_for_submit(
        _api("feedback-1"),
        _candidate("feedback-1", "yes", "exact", "Просим проверить отзыв: тестовое описание."),
        {
            "selected_category": "Другое",
            "draft_text": "Просим проверить отзыв: тестовое описание.",
            "blocker": "",
            "success_state": {"text": "Жалоба отправлена"},
            "modal_description_value_before_submit": "Просим проверить отзыв: тестовое описание.",
            "description_value_match": True,
            "submit_network_capture": {
                "submit_payload_checked": True,
                "submit_payload_has_description": True,
                "submit_payload_description_length": len("Просим проверить отзыв: тестовое описание."),
                "submit_payload_description_snippet": "Просим проверить отзыв: тестовое описание.",
            },
        },
        status="waiting_response",
        run_id="run-1",
    )
    if record["complaint_status"] != "waiting_response" or record["wb_category_label"] != "Другое":
        raise AssertionError(f"journal record must use waiting response and WB category: {record}")
    if record["submit_clicked_count"] != 0 or record["submit_result"] != "":
        raise AssertionError(f"journal record must carry submit instrumentation fields: {record}")
    if (
        record["modal_description_value_before_submit"] != "Просим проверить отзыв: тестовое описание."
        or record["submit_payload_has_description"] is not True
        or record["description_persisted"] != "unknown"
    ):
        raise AssertionError(f"journal record must carry description persistence diagnostics: {record}")
    if record["review_tags"] != ["Плохое качество"] or record["tag_source"] != "official_wb_api":
        raise AssertionError(f"journal record must carry review tag diagnostics: {record}")
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
        "review_tags": ["Плохое качество"],
        "tag_source": "official_wb_api",
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


class _FakeRequest:
    def __init__(self, method: str) -> None:
        self.method = method


class _FakeRequestWithBody:
    def __init__(self, *, url: str, method: str, payload: dict[str, object]) -> None:
        self.url = url
        self.method = method
        self.headers = {"content-type": "application/json"}
        self.post_data = json.dumps(payload, ensure_ascii=False)


class _FakeResponse:
    def __init__(self, *, url: str, method: str, status: int, payload: dict[str, object]) -> None:
        self.url = url
        self.status = status
        self.headers = {"content-type": "application/json"}
        self.request = _FakeRequest(method)
        self._payload = payload

    def json(self) -> dict[str, object]:
        return dict(self._payload)


if __name__ == "__main__":
    main()
