"""Local smoke checks for read-only Seller Portal target row probe."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.seller_portal_feedbacks_complaints_scout import (  # noqa: E402
    parse_complaint_categories_from_html,
    parse_feedback_rows_from_html,
    parse_row_menu_diagnostics_from_html,
)
from apps.seller_portal_feedbacks_target_row_probe import (  # noqa: E402
    CONTRACT_NAME,
    TargetRowProbeConfig,
    build_probe_match_aggregate,
    compare_counts,
    match_api_rows_to_dom,
    no_submit_guards,
    parse_stars,
    render_markdown_report,
    status_tabs_for_request,
    write_report_artifacts,
)


def main() -> None:
    _assert_count_comparison()
    _assert_exact_dom_match_by_feedback_id()
    _assert_exact_dom_match_by_fields()
    _assert_menu_and_modal_parsers()
    _assert_report_shape()
    _assert_guards_and_params()
    print("seller_portal_feedbacks_target_row_probe_smoke: OK")


def _assert_count_comparison() -> None:
    rows = [_ui_row("a"), _ui_row("b")]
    same = compare_counts(api_total_count=2, dom_rows=rows, cursor_rows=rows)
    if not same["counts_match"] or not same["cursor_counts_match"]:
        raise AssertionError(f"equal counts must match: {same}")
    mismatch = compare_counts(api_total_count=3, dom_rows=rows, cursor_rows=rows)
    if mismatch["counts_match"] or mismatch["diagnostics"]["dom_minus_api"] != -1:
        raise AssertionError(f"mismatch diagnostics must be explicit: {mismatch}")


def _assert_exact_dom_match_by_feedback_id() -> None:
    api = _api_row("feedback-id-exact")
    ui = {**_ui_row("dom-row-1"), "feedback_id": "feedback-id-exact"}
    matches = match_api_rows_to_dom([api], [ui])
    match = matches[0]
    if match["match_status"] != "exact" or "feedback_id" not in match["matched_fields"]:
        raise AssertionError(f"feedback_id must produce exact DOM match: {match}")
    if not match["safe_for_actionability_probe"]:
        raise AssertionError(f"exact DOM row with dom id must be actionability-probe safe: {match}")


def _assert_exact_dom_match_by_fields() -> None:
    api = _api_row("api-field-exact")
    ui = _ui_row("dom-field-exact")
    match = match_api_rows_to_dom([api], [ui])[0]
    if match["match_status"] != "exact":
        raise AssertionError(f"text/date/rating/article must produce exact DOM match: {match}")
    for field in ("text_exact", "exact_datetime", "rating", "nm_id"):
        if field not in match["matched_fields"]:
            raise AssertionError(f"expected matched field {field}: {match}")
    aggregate = build_probe_match_aggregate([match], [api], [ui])
    if aggregate["exact_count"] != 1 or aggregate["first_exact_feedback_id"] != "api-field-exact":
        raise AssertionError(f"aggregate must expose first exact id: {aggregate}")


def _assert_menu_and_modal_parsers() -> None:
    menu = parse_row_menu_diagnostics_from_html(
        """
        <div role="menu" data-scout-row-menu>
          <button>Запросить возврат</button>
          <button>Пожаловаться на отзыв</button>
        </div>
        """
    )
    if not menu["menu_opened"] or not menu["complaint_action_found"]:
        raise AssertionError(f"menu parser must see complaint action: {menu}")
    modal = parse_complaint_categories_from_html(
        """
        <div role="dialog">
          <button>Отзыв не относится к товару</button>
          <button>Другое</button>
          <label>Опишите ситуацию</label>
          <textarea></textarea>
          <button>Отправить</button>
        </div>
        """
    )
    if not modal["opened"] or "Другое" not in modal["categories"] or not modal["submit_button_seen"]:
        raise AssertionError(f"modal parser shape changed: {modal}")


def _assert_report_shape() -> None:
    api = _api_row("api-report")
    ui = _ui_row("dom-report")
    matches = match_api_rows_to_dom([api], [ui])
    report = {
        "contract_name": CONTRACT_NAME,
        "mode": "read-only",
        "started_at": "2026-04-04T00:00:00Z",
        "finished_at": "2026-04-04T00:00:01Z",
        "parameters": {"date": "2026-04-04", "stars": [1], "is_answered": "all"},
        "read_only_guards": no_submit_guards(),
        "api": {"sample_rows": [{"feedback_id": "api-report", "created_at": "2026-04-04T12:00:00Z", "rating": "1", "nm_id": "1", "supplier_article": "a", "review_tags": [], "review_text": "text"}]},
        "seller_portal": {"filters": {"date_filter_applied": True, "star_filter_applied": True, "status_tabs_checked": ["Ждут ответа", "Есть ответ"], "rows_visible_after_filter": 2, "rows_collected": 1}},
        "count_comparison": compare_counts(api_total_count=1, dom_rows=[ui], cursor_rows=[ui]),
        "matches": matches,
        "matching_aggregate": build_probe_match_aggregate(matches, [api], [ui]),
        "actionability": {"requested": True, "row_menu_found": True, "menu_items": ["Пожаловаться на отзыв"], "complaint_action_found": True, "modal_opened": True, "categories_found": ["Другое"], "modal_closed": True, "submit_clicked": False, "blocker": ""},
        "errors": [],
    }
    markdown = render_markdown_report(report)
    if "Seller Portal Feedback Target Row Probe" not in markdown or "Counts match" not in markdown:
        raise AssertionError(f"markdown shape mismatch: {markdown}")
    with TemporaryDirectory(prefix="target-row-probe-smoke-") as tmp:
        paths = write_report_artifacts(dict(report), Path(tmp))
        if not paths["json"].exists() or not paths["markdown"].exists():
            raise AssertionError(f"report artifacts missing: {paths}")


def _assert_guards_and_params() -> None:
    if parse_stars("1") != (1,) or parse_stars("1,2") != (1, 2):
        raise AssertionError("star parser failed")
    if status_tabs_for_request("all") != ["Ждут ответа", "Есть ответ"]:
        raise AssertionError("all status must probe unanswered and answered tabs")
    config = TargetRowProbeConfig(
        date="2026-04-04",
        stars=(1,),
        is_answered="all",
        max_api_rows=20,
        max_ui_rows=50,
        open_menu=True,
        open_complaint_modal=True,
        mode="read-only",
        storage_state_path=Path("/tmp/storage.json"),
        wb_bot_python=Path("/tmp/python"),
        output_dir=Path("/tmp/out"),
        start_url="https://seller.wildberries.ru",
        headless=True,
        timeout_ms=5000,
        write_artifacts=False,
    )
    guards = no_submit_guards(config)
    if guards["complaint_submit_clicked"] or guards["complaint_final_submit_allowed"] or guards["journal_write_allowed"]:
        raise AssertionError(f"read-only guard must forbid writes: {guards}")


def _api_row(feedback_id: str) -> dict[str, object]:
    return {
        "feedback_id": feedback_id,
        "created_at": "2026-04-04T12:03:30Z",
        "created_date": "2026-04-04",
        "product_valuation": 1,
        "text": "Плохое качество, стекло не подошло",
        "review_tags": ["Плохое качество"],
        "tag_source": "official_wb_api",
        "pros": "",
        "cons": "Не как на фото",
        "nm_id": 391662965,
        "supplier_article": "(Anti-Spy) iPhone 15 / 16",
        "product_name": "Защитное стекло антишпион на iPhone 15 / 16",
        "is_answered": False,
        "photo_count": 1,
        "video_count": 0,
    }


def _ui_row(row_id: str) -> dict[str, object]:
    rows = parse_feedback_rows_from_html(
        """
        <article data-scout-feedback-row data-scout-id="dom-field-exact">
          <div>04.04.2026 в 17:03</div>
          <div>1 звезда</div>
          <div>Артикул WB 391662965</div>
          <div>Артикул продавца (Anti-Spy) iPhone 15 / 16</div>
          <div>Отзыв Плохое качество, стекло не подошло</div>
          <div>Минусы Не как на фото</div>
          <button>...</button>
        </article>
        """,
        max_rows=1,
    )
    row = rows[0]
    row["dom_scout_id"] = row_id
    row["status_tab"] = "Ждут ответа"
    row["tab_used"] = "Ждут ответа"
    row["source"] = "seller_portal_dom"
    return row


if __name__ == "__main__":
    main()
