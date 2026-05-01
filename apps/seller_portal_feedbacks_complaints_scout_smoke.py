"""Local smoke checks for the read-only Seller Portal complaint scout."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.seller_portal_feedbacks_complaints_scout import (
    DEFAULT_WB_BOT_PYTHON,
    LOCAL_OUTPUT_ROOT,
    ScoutConfig,
    assert_safe_click_label,
    parse_complaint_categories_from_html,
    parse_feedback_rows_from_html,
    parse_my_complaints_rows_from_html,
    parse_row_menu_diagnostics_from_html,
    run_scout,
    score_feedback_match,
)


def main() -> None:
    _assert_feedback_parser()
    _assert_row_menu_parser()
    _assert_category_parser()
    _assert_my_complaints_parser()
    _assert_matching_scores()
    _assert_no_submit_guard()
    _assert_session_missing_blocker()
    print("seller_portal_feedbacks_complaints_scout_smoke: OK")


def _assert_feedback_parser() -> None:
    rows = parse_feedback_rows_from_html(
        """
        <article data-scout-feedback-row data-feedback-id="feedback-abc-12345678">
          <div>Товар: Крем для рук</div>
          <div>Артикул WB 123456789</div>
          <div>Артикул продавца 777777</div>
          <div>Оценка 2 звезды</div>
          <div>Дата 28.04.2026</div>
          <div>Отзыв</div><div>Упаковка пришла мятая, запах не соответствует описанию.</div>
          <button aria-label="Ещё">⋮</button>
        </article>
        """,
        max_rows=3,
    )
    if len(rows) != 1:
        raise AssertionError(f"expected one feedback row, got {rows}")
    row = rows[0]
    if row["hidden_feedback_id"] != "feedback-abc-12345678":
        raise AssertionError(f"feedback id not extracted: {row}")
    if row["rating"] != "2" or row["review_date"] != "28.04.2026":
        raise AssertionError(f"rating/date not extracted: {row}")
    if not row["three_dot_menu_found"]:
        raise AssertionError(f"three-dot menu not detected: {row}")

    live_like_rows = parse_feedback_rows_from_html(
        """
        <tr data-scout-feedback-row>
          <td><a href="https://www.wildberries.ru/catalog/428855306/detail.aspx">
            Защитное стекло матовое на iPhone 15 / 16
          </a></td>
          <td><button>(Matte) iPhone 15 / 16</button><button>428855306</button></td>
          <td>01.05.2026 в 16:38</td>
          <td><div>Плюсы: Упаковала</div><div>Минусы: Все отлично</div><div>Комментарий: Спасибо</div></td>
          <td><button>...</button></td>
        </tr>
        """,
        max_rows=3,
    )
    live_row = live_like_rows[0]
    if live_row["supplier_article"] != "(Matte) iPhone 15 / 16" or live_row["wb_article"] != "428855306":
        raise AssertionError(f"live-like articles not extracted: {live_row}")
    if live_row["review_datetime"] != "01.05.2026 в 16:38":
        raise AssertionError(f"review datetime not extracted: {live_row}")
    if live_row["pros_snippet"] != "Упаковала" or live_row["cons_snippet"] != "Все отлично":
        raise AssertionError(f"pros/cons not extracted: {live_row}")


def _assert_row_menu_parser() -> None:
    global_only = parse_row_menu_diagnostics_from_html(
        """
        <main>
          <button>Пожаловаться на отзыв</button>
        </main>
        """
    )
    if global_only["complaint_action_found"]:
        raise AssertionError(f"global complaint action must not count as row menu: {global_only}")

    menu = parse_row_menu_diagnostics_from_html(
        """
        <div data-scout-row-menu class="Dropdown-list">
          <button>Запросить возврат</button>
          <button>Пожаловаться на отзыв</button>
        </div>
        """
    )
    if menu["menu_items_found"] != ["Запросить возврат", "Пожаловаться на отзыв"]:
        raise AssertionError(f"row menu items not parsed: {menu}")
    if not menu["complaint_action_found"]:
        raise AssertionError(f"complaint action not found in row menu: {menu}")


def _assert_category_parser() -> None:
    modal = parse_complaint_categories_from_html(
        """
        <section role="dialog">
          <h2>Пожаловаться на отзыв</h2>
          <label><input type="radio">Отзыв не относится к товару</label>
          <label><input type="radio">Спам-реклама в тексте</label>
          <label><input type="radio">Другое</label>
          <textarea placeholder="Опишите причину жалобы"></textarea>
          <div>Выберите причину, описание до 1000 символов</div>
          <button>Отправить</button>
        </section>
        """
    )
    if modal["categories"] != ["Отзыв не относится к товару", "Спам-реклама в тексте", "Другое"]:
        raise AssertionError(f"category list mismatch: {modal}")
    if not modal["submit_button_seen"]:
        raise AssertionError(f"submit button must be detected, not clicked: {modal}")
    if not modal["validation_hints"]:
        raise AssertionError(f"validation hints not extracted: {modal}")


def _assert_my_complaints_parser() -> None:
    rows = parse_my_complaints_rows_from_html(
        """
        <tr data-scout-complaint-row data-complaint-id="complaint-1">
          <td>Товар: Крем для рук</td>
          <td>Артикул WB 123456789</td>
          <td>Причина</td><td>Другое</td>
          <td>Описание</td><td>Просим проверить отзыв</td>
          <td>Отзыв</td><td>Упаковка пришла мятая.</td>
          <td>Оценка 2 звезды</td>
          <td>Дата 28.04.2026</td>
          <td>Отклонена</td>
        </tr>
        """,
        max_rows=3,
    )
    if len(rows) != 1:
        raise AssertionError(f"expected one complaint row, got {rows}")
    row = rows[0]
    if row["complaint_reason"] != "Другое":
        raise AssertionError(f"reason not extracted: {row}")
    if row["decision_label"] != "rejected":
        raise AssertionError(f"decision not extracted: {row}")
    if row["review_rating"] != "2":
        raise AssertionError(f"review rating not extracted: {row}")


def _assert_matching_scores() -> None:
    exact = score_feedback_match(
        {"feedback_id": "fb-1", "text": "Очень длинный отзыв про товар", "rating": "2"},
        {"hidden_feedback_id": "fb-1", "text_snippet": "другой текст"},
    )
    if exact["status"] != "exact" or exact["score"] != 1.0:
        raise AssertionError(f"exact match failed: {exact}")

    high = score_feedback_match(
        {
            "text": "Упаковка пришла мятая, запах не соответствует описанию товара",
            "rating": "2",
            "created_at": "2026-04-28 10:25",
            "nm_id": "123456789",
        },
        {
            "text_snippet": "Упаковка пришла мятая, запах не соответствует описанию товара",
            "rating": "2",
            "review_datetime": "28.04.2026 в 10:25",
            "review_date": "28.04.2026",
            "nm_id": "123456789",
        },
    )
    if high["status"] != "high" or high["score"] < 0.82:
        raise AssertionError(f"high match failed: {high}")
    if "same exact datetime" not in high["reasons"]:
        raise AssertionError(f"datetime match reason missing: {high}")

    ambiguous = score_feedback_match(
        {"text": "Короткий похожий отзыв", "rating": "3", "created_date": "2026-04-28"},
        {"text_snippet": "Короткий похожий отзыв", "rating": "3", "review_date": "2026-04-28"},
    )
    if ambiguous["status"] != "ambiguous":
        raise AssertionError(f"ambiguous match failed: {ambiguous}")

    missing = score_feedback_match({"text": "Совсем другой отзыв"}, {"text_snippet": "Нет совпадения"})
    if missing["status"] != "not_found":
        raise AssertionError(f"not_found match failed: {missing}")


def _assert_no_submit_guard() -> None:
    assert_safe_click_label("Пожаловаться на отзыв", purpose="open_complaint_modal")
    assert_safe_click_label("Закрыть", purpose="close_modal")
    assert_safe_click_label("Отмена", purpose="close_modal")
    for label in ("Отправить", "Подать жалобу", "Сохранить", "Пожаловаться"):
        try:
            assert_safe_click_label(label, purpose="tab_navigation")
        except RuntimeError:
            continue
        raise AssertionError(f"submit-like label was not blocked: {label}")


def _assert_session_missing_blocker() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config = ScoutConfig(
            mode="full-scout",
            storage_state_path=Path(tmp) / "missing_storage_state.json",
            wb_bot_python=DEFAULT_WB_BOT_PYTHON,
            output_root=LOCAL_OUTPUT_ROOT,
            start_url="https://seller.wildberries.ru",
            max_feedback_rows=1,
            max_complaint_rows=1,
            max_modal_reviews=0,
            open_complaint_modal=False,
            headless=True,
            timeout_ms=5000,
            write_artifacts=False,
        )
        report = run_scout(config)
    if report["session"]["status"] != "seller_portal_session_missing":
        raise AssertionError(f"missing session blocker not surfaced: {report}")
    if report["read_only_guards"]["complaint_submit_clicked"]:
        raise AssertionError(f"submit guard violated: {report}")


if __name__ == "__main__":
    main()
