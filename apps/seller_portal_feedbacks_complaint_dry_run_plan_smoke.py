"""Local smoke checks for no-submit complaint dry-run planning."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.seller_portal_feedbacks_complaint_dry_run_plan import (  # noqa: E402
    FEEDBACKS_ANSWERED_TAB_LABEL,
    FEEDBACKS_TAB_LABEL,
    FEEDBACKS_UNANSWERED_TAB_LABEL,
    NO_SUBMIT_MODE,
    SELLER_PORTAL_WRITE_ACTIONS_ALLOWED,
    DryRunConfig,
    actionability_block_reason,
    apply_seller_portal_feedback_filters,
    build_aggregate,
    build_candidate_records,
    build_draft_text,
    choose_complaint_category,
    coerce_boolish,
    description_is_ready_for_submit,
    description_persistence_result,
    empty_modal_candidate_state,
    feedback_filter_date_range,
    feedback_filter_stars,
    feedback_tab_candidates,
    fill_description_field,
    find_visible_actionable_row,
    no_submit_guards,
    normalize_deny_feedback_ids,
    render_markdown_report,
    select_ai_candidate_ids,
    should_open_modal_for_match,
    score_visible_row_against_exact_cursor,
    wait_for_description_field_ready,
    write_report_artifacts,
)
from playwright.sync_api import sync_playwright  # noqa: E402


def main() -> None:
    _assert_candidate_selection()
    _assert_exact_only_guard()
    _assert_visible_row_cursor_guard()
    _assert_actionability_tab_plan()
    _assert_filter_controller_sequence()
    _assert_category_other_fallback()
    _assert_draft_text_builder()
    _assert_description_fill_sequence()
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
    deny = normalize_deny_feedback_ids(["yes-1"])
    selected_with_deny = select_ai_candidate_ids(results, max_candidates=3, deny_feedback_ids=deny)
    if selected_with_deny != ["yes-2", "review-1", "review-2"]:
        raise AssertionError(f"denylist must be skipped during dry-run selection: {selected_with_deny}")
    denied_records = build_candidate_records([_api("GPe9vrq0kctlSfobrgq2")], {"GPe9vrq0kctlSfobrgq2": _ai("GPe9vrq0kctlSfobrgq2", "yes")}, [], deny_feedback_ids=deny)
    if "hard-denylisted" not in denied_records[0].get("skip_reason", ""):
        raise AssertionError(f"default historical denylist must block dry-run candidate: {denied_records}")


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


def _assert_actionability_tab_plan() -> None:
    answered_tabs = feedback_tab_candidates({"is_answered": True}, {}, requested_is_answered="all")
    if answered_tabs[:3] != [FEEDBACKS_ANSWERED_TAB_LABEL, FEEDBACKS_TAB_LABEL, FEEDBACKS_UNANSWERED_TAB_LABEL]:
        raise AssertionError(f"answered rows must prefer answered tab: {answered_tabs}")
    unanswered_tabs = feedback_tab_candidates({"is_answered": False}, {}, requested_is_answered="all")
    if unanswered_tabs[:3] != [FEEDBACKS_UNANSWERED_TAB_LABEL, FEEDBACKS_TAB_LABEL, FEEDBACKS_ANSWERED_TAB_LABEL]:
        raise AssertionError(f"unanswered rows must prefer unanswered tab: {unanswered_tabs}")
    fallback_tabs = feedback_tab_candidates({}, {}, requested_is_answered="all")
    if fallback_tabs[:3] != [FEEDBACKS_TAB_LABEL, FEEDBACKS_UNANSWERED_TAB_LABEL, FEEDBACKS_ANSWERED_TAB_LABEL]:
        raise AssertionError(f"unknown answered state must try default reviews tab first: {fallback_tabs}")
    if coerce_boolish("Есть ответ") is not True or coerce_boolish("Ждут ответа") is not False:
        raise AssertionError("Russian answered-state labels must be parsed")
    reason = actionability_block_reason({"complaint_action_found": False}, {"targeted_search": {"ok": True}})
    if "unavailable" not in reason:
        raise AssertionError(f"cursor actionability=false must produce explicit blocker: {reason}")


def _assert_filter_controller_sequence() -> None:
    config = DryRunConfig(
        date_from="2026-05-01",
        date_to="2026-05-04",
        stars=(1,),
        is_answered="all",
        max_api_rows=50,
        max_ai_candidates=12,
        force_category_other=False,
        mode=NO_SUBMIT_MODE,
        runtime_dir=Path(".runtime"),
        storage_state_path=Path(".runtime/storage_state.json"),
        wb_bot_python=Path("wb_bot.py"),
        output_dir=Path(".runtime/reports"),
        start_url="https://seller.wildberries.ru",
        headless=True,
        timeout_ms=5000,
        write_artifacts=False,
        deny_feedback_ids=normalize_deny_feedback_ids([]),
    )
    api_row = {**_api("filtered-1"), "created_at": "2026-05-04T09:38:19Z", "product_valuation": 1}
    expected_ui = {"review_datetime": "04.05.2026 в 14:38", "rating": "1"}
    if feedback_filter_date_range(config, api_row, expected_ui=expected_ui) != ("2026-05-04", "2026-05-04"):
        raise AssertionError("filter date range must prefer the exact candidate date")
    if feedback_filter_stars(config, api_row, expected_ui=expected_ui) != (1,):
        raise AssertionError("filter stars must prefer the candidate rating")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.set_content(
                """
                <button id="dateButton">01.05.2026 - 05.05.2026</button>
                <div id="datePopup" role="dialog" style="display:none">
                  <input placeholder="Дата от">
                  <input placeholder="Дата до">
                  <button id="dateApply">Применить</button>
                </div>
                <button id="filtersButton">Фильтры</button>
                <div id="filterPopup" role="dialog" style="display:none">
                  <div>Оценка отзыва</div>
                  <label><input type="checkbox" checked>5★</label>
                  <label><input type="checkbox" checked>4★</label>
                  <label><input type="checkbox">3★</label>
                  <label><input type="checkbox">2★</label>
                  <label><input type="checkbox">1★</label>
                  <button id="filterApply">Применить</button>
                  <button>Сбросить</button>
                </div>
                <section id="rows">
                  <article>Отзыв 1 01.05.2026 5★</article>
                </section>
                <script>
                  const rows = document.querySelector('#rows');
                  document.querySelector('#dateButton').addEventListener('click', () => {
                    document.querySelector('#datePopup').style.display = 'block';
                  });
                  document.querySelector('#dateApply').addEventListener('click', () => {
                    document.querySelector('#datePopup').style.display = 'none';
                    rows.innerHTML = '<article>Отзыв RD 04.05.2026 1★ date</article>';
                  });
                  document.querySelector('#filtersButton').addEventListener('click', () => {
                    document.querySelector('#filterPopup').style.display = 'block';
                  });
                  document.querySelector('#filterApply').addEventListener('click', () => {
                    document.querySelector('#filterPopup').style.display = 'none';
                    rows.innerHTML = '<article>Отзыв RD 04.05.2026 1★ date star</article>';
                  });
                </script>
                """
            )
            result = apply_seller_portal_feedback_filters(page, config, api_row, expected_ui=expected_ui)
            if not result.get("date_filter_applied") or not result.get("star_filter_applied"):
                raise AssertionError(f"date/star filters must be applied in the UI smoke: {result}")
            if result.get("requested_date_from") != "2026-05-04" or result.get("requested_date_to") != "2026-05-04":
                raise AssertionError(f"candidate date filter must be exact-day: {result}")
            if result.get("requested_stars") != [1]:
                raise AssertionError(f"candidate star filter must be exact rating: {result}")
            values = page.evaluate(
                """
                () => Array.from(document.querySelectorAll('#datePopup input')).map((input) => input.value)
                """
            )
            if values != ["04.05.2026", "04.05.2026"]:
                raise AssertionError(f"date filter inputs must be filled with Russian date values: {values}")
            checked = page.evaluate(
                """
                () => Array.from(document.querySelectorAll('#filterPopup label')).filter((label) => label.querySelector('input').checked).map((label) => label.innerText.trim())
                """
            )
            if checked != ["1★"]:
                raise AssertionError(f"star filter must leave only 1-star selected: {checked}")
            selectors = set(result.get("selectors_used") or [])
            if not {"date_inputs", "date_apply", "filters_apply"}.issubset(selectors):
                raise AssertionError(f"filter diagnostics must include selectors used: {result}")
            if not any(
                item in selectors for item in ("review_rating_checkboxes", "text_or_aria_checkbox_rows", "custom_checkbox_order_fallback_5_to_1")
            ):
                raise AssertionError(f"filter diagnostics must include rating selector strategy: {result}")
            if not any(str(item).startswith("filters_button:") for item in selectors):
                raise AssertionError(f"filter diagnostics must include selectors used: {result}")
        finally:
            browser.close()


def _assert_category_other_fallback() -> None:
    categories = ["Спам-реклама в тексте", "Другое", "Нецензурная лексика"]
    if choose_complaint_category(categories, force_other=True) != "Другое":
        raise AssertionError("force_category_other must select Другое when available")
    if choose_complaint_category(categories, force_other=False, preferred_category="Нецензурная лексика") != "Нецензурная лексика":
        raise AssertionError("AI category_label must be selected when force_category_other is disabled and label exists")
    if choose_complaint_category(categories, force_other=False, preferred_category="Фото или видео не имеет отношения к товару") != "Другое":
        raise AssertionError("unavailable AI category_label must fall back to Другое")
    if choose_complaint_category(["Спам-реклама в тексте"], force_other=True):
        raise AssertionError("missing Другое must not choose a weak fallback")


def _assert_draft_text_builder() -> None:
    draft = build_draft_text(
        {
            "reason": "Просим проверить отзыв: покупатель описывает получение заказа, а не свойства товара.",
            "evidence": "фрагмент доказательства",
        }
    )
    if draft != "Просим проверить отзыв: покупатель описывает получение заказа, а не свойства товара.":
        raise AssertionError(f"draft must use AI reason as ready WB description without prefix duplication: {draft}")
    long = build_draft_text({"reason": "а" * 1000, "evidence": "б" * 1000})
    if len(long) > 500:
        raise AssertionError("draft text must be bounded")


def _assert_description_fill_sequence() -> None:
    intended = "Отзыв описывает другой товар, а не свойства товара из карточки."
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.set_content(
                """
                <div role="dialog" aria-modal="true">
                  <label for="desc">Опишите ситуацию</label>
                  <textarea id="desc" placeholder="Опишите ситуацию"></textarea>
                  <button>Отправить</button>
                </div>
                <script>
                  window.inputEvents = 0;
                  window.changeEvents = 0;
                  const desc = document.querySelector('#desc');
                  desc.addEventListener('input', () => { window.inputEvents += 1; });
                  desc.addEventListener('change', () => { window.changeEvents += 1; });
                </script>
                """
            )
            ready = wait_for_description_field_ready(page, timeout_ms=1000)
            if not ready.get("ok") or ready.get("field_locator_strategy") != "label_or_placeholder_opishite_situaciyu":
                raise AssertionError(f"description field must be detected by label/placeholder: {ready}")
            fill = fill_description_field(page, intended)
            if not fill.get("ok") or not description_is_ready_for_submit(fill, intended):
                raise AssertionError(f"description fill must set and preserve value after blur: {fill}")
            events = page.evaluate("() => ({inputEvents: window.inputEvents, changeEvents: window.changeEvents, value: document.querySelector('#desc').value})")
            if events["value"] != intended or events["inputEvents"] < 1 or events["changeEvents"] < 1:
                raise AssertionError(f"description fill must trigger controlled-field events: {events}")

            page.set_content('<div role="dialog" aria-modal="true"><button>Отправить</button></div>')
            missing = fill_description_field(page, intended)
            if missing.get("ok") or description_is_ready_for_submit(missing, intended):
                raise AssertionError(f"missing description field must block submit readiness: {missing}")
        finally:
            browser.close()

    persisted = description_persistence_result(intended, intended)
    if persisted["description_persisted"] is not True:
        raise AssertionError(f"matching WB description must persist true: {persisted}")
    empty = description_persistence_result(intended, "")
    if empty["description_persisted"] is not False:
        raise AssertionError(f"empty WB description must persist false: {empty}")


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
        "category": "not_about_product",
        "category_label": "Отзыв не относится к товару",
        "reason": "Просим проверить отзыв: покупатель описывает получение заказа, а не свойства товара.",
        "confidence": "medium",
        "confidence_label": "Средняя",
        "evidence": "короткий фрагмент",
    }


if __name__ == "__main__":
    main()
