"""Local smoke checks for no-submit Seller Portal feedback matching replay."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.seller_portal_feedbacks_matching_replay import (  # noqa: E402
    NO_SUBMIT_MODE,
    SELLER_PORTAL_WRITE_ACTIONS_ALLOWED,
    build_aggregate,
    build_recommendation,
    match_api_rows_to_ui,
    match_one_api_row,
    no_submit_guards,
    normalize_article,
    normalize_datetime_minute,
    normalize_nm_id,
    normalize_text,
    render_markdown_report,
    write_report_artifacts,
)


def main() -> None:
    _assert_exact_match()
    _assert_high_match()
    _assert_ambiguous_short_text_collision()
    _assert_not_found()
    _assert_normalizers()
    _assert_report_shape()
    _assert_no_submit_guard()
    print("seller_portal_feedbacks_matching_replay_smoke: OK")


def _assert_exact_match() -> None:
    match = match_one_api_row(_api_row("api-exact"), [_ui_row("ui-exact")])
    if match["match_status"] != "exact" or not match["safe_for_future_submit"]:
        raise AssertionError(f"expected exact safe match, got {match}")
    for field in ("text_exact", "exact_datetime", "rating", "nm_id"):
        if field not in match["matched_fields"]:
            raise AssertionError(f"exact match missing {field}: {match}")


def _assert_high_match() -> None:
    api = {
        **_api_row("api-high"),
        "nm_id": None,
        "supplier_article": "",
    }
    ui = {
        **_ui_row("ui-high"),
        "nm_id": "",
        "wb_article": "",
        "supplier_article": "",
    }
    match = match_one_api_row(api, [ui])
    if match["match_status"] != "high" or match["safe_for_future_submit"]:
        raise AssertionError(f"expected high non-submit match, got {match}")
    if "article" not in match["missing_fields"]:
        raise AssertionError(f"missing article must be reported: {match}")


def _assert_ambiguous_short_text_collision() -> None:
    api = {
        **_api_row("api-short"),
        "text": "Все ок",
        "pros": "",
        "cons": "",
    }
    ui_a = {
        **_ui_row("ui-short-a"),
        "text_snippet": "Все ок",
        "pros_snippet": "",
        "cons_snippet": "",
        "comment_snippet": "",
    }
    ui_b = {
        **ui_a,
        "row_text_fingerprint": "different-close-row",
        "dom_fingerprint": "different-close-row",
        "ui_collection_index": 2,
    }
    match = match_one_api_row(api, [ui_a, ui_b])
    if match["match_status"] != "ambiguous" or match["ambiguity_count"] != 2:
        raise AssertionError(f"expected ambiguous duplicate match, got {match}")
    if "duplicate candidate penalty" not in match["reason"] or "short text penalty" not in match["reason"]:
        raise AssertionError(f"duplicate/short penalties must be visible: {match}")
    if match["safe_for_future_submit"]:
        raise AssertionError(f"ambiguous match must block submit: {match}")


def _assert_not_found() -> None:
    api = {
        **_api_row("api-missing"),
        "created_at": "2026-05-01T09:00:00Z",
        "text": "Совсем другой длинный отзыв без совпадения",
        "cons": "Разные признаки",
        "nm_id": 999999999,
    }
    match = match_one_api_row(api, [_ui_row("ui-other")])
    if match["match_status"] != "not_found" or match["safe_for_future_submit"]:
        raise AssertionError(f"expected not_found, got {match}")


def _assert_normalizers() -> None:
    if normalize_text(" Ёж!!!   ТЕСТ... ") != "еж тест":
        raise AssertionError("text normalization failed")
    if normalize_datetime_minute("01.05.2026 в 17:03") != "2026-05-01 17:03":
        raise AssertionError("UI datetime normalization failed")
    if normalize_datetime_minute("2026-05-01T12:03:59Z") != "2026-05-01 17:03":
        raise AssertionError("API datetime timezone normalization failed")
    if normalize_nm_id("WB article 391662965") != "391662965":
        raise AssertionError("nmId normalization failed")
    if normalize_article(" Артикул WB (Anti-Spy) iPhone 15 / 16 ") != "anti-spy iphone 15 16":
        raise AssertionError("supplier article normalization failed")


def _assert_report_shape() -> None:
    api_rows = [_api_row("api-report")]
    ui_rows = [_ui_row("ui-report")]
    matches = match_api_rows_to_ui(api_rows, ui_rows)
    aggregate = build_aggregate(matches, api_rows, ui_rows)
    report = {
        "mode": NO_SUBMIT_MODE,
        "started_at": "2026-05-01T00:00:00Z",
        "finished_at": "2026-05-01T00:00:01Z",
        "parameters": {
            "date_from": "2026-05-01",
            "date_to": "2026-05-01",
            "stars": [1, 2, 3, 4, 5],
            "is_answered": "false",
        },
        "read_only_guards": no_submit_guards(),
        "api": {"row_count": 1, "total_available_rows": 1},
        "ui": {"rows_collected": 1, "hidden_feedback_id_available": False, "filters": {"applied": "route_not_answered"}},
        "matches": matches,
        "aggregate": aggregate,
        "recommendation": build_recommendation(aggregate, {"ui": {"hidden_feedback_id_available": False}}),
        "errors": [],
    }
    markdown = render_markdown_report(report)
    if "Seller Portal Feedback Matching Replay" not in markdown or "Exact/high/ambiguous/not_found" not in markdown:
        raise AssertionError(f"markdown shape mismatch: {markdown}")
    with TemporaryDirectory(prefix="matching-replay-smoke-") as tmp:
        paths = write_report_artifacts(dict(report), Path(tmp))
        if not paths["json"].exists() or not paths["markdown"].exists():
            raise AssertionError(f"report artifacts missing: {paths}")


def _assert_no_submit_guard() -> None:
    guards = no_submit_guards()
    if SELLER_PORTAL_WRITE_ACTIONS_ALLOWED:
        raise AssertionError("Seller Portal writes must be disabled")
    if guards["mode"] != NO_SUBMIT_MODE:
        raise AssertionError(f"unexpected mode guard: {guards}")
    for key in ("complaint_submit_clicked", "complaint_modal_opened", "answer_edit_clicked", "complaint_submit_path_called"):
        if guards[key]:
            raise AssertionError(f"no-submit guard violated: {guards}")


def _api_row(feedback_id: str) -> dict[str, object]:
    return {
        "feedback_id": feedback_id,
        "created_at": "2026-05-01T12:03:30Z",
        "created_date": "2026-05-01",
        "product_valuation": 1,
        "text": "Плохое качество, стекло не подошло",
        "pros": "",
        "cons": "Не как на фото",
        "nm_id": 391662965,
        "supplier_article": "(Anti-Spy) iPhone 15 / 16",
        "product_name": "Защитное стекло антишпион на iPhone 15 / 16",
        "is_answered": False,
        "answer_text": "",
        "photo_count": 1,
        "video_count": 0,
    }


def _ui_row(row_id: str) -> dict[str, object]:
    return {
        "ui_collection_index": 0,
        "dom_scout_id": row_id,
        "product_title": "Защитное стекло антишпион на iPhone 15 / 16",
        "supplier_article": "(Anti-Spy) iPhone 15 / 16",
        "vendor_article": "(Anti-Spy) iPhone 15 / 16",
        "wb_article": "391662965",
        "nm_id": "391662965",
        "rating": "1",
        "review_date": "01.05.2026",
        "review_datetime": "01.05.2026 в 17:03",
        "text_snippet": "Плохое качество, стекло не подошло Не как на фото",
        "pros_snippet": "",
        "cons_snippet": "",
        "comment_snippet": "",
        "media_indicators": ["photo"],
        "hidden_feedback_id": "",
        "row_text_fingerprint": row_id,
        "dom_fingerprint": row_id,
        "normalized_review_text_fingerprint": row_id,
        "three_dot_menu_found": True,
    }


if __name__ == "__main__":
    main()
