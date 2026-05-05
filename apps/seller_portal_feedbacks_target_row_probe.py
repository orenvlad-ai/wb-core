"""Read-only target-row probe for WB API feedbacks vs Seller Portal UI.

The runner compares canonical official WB feedback rows with Seller Portal rows
under the same date/star/status filters. It intentionally does not use AI
candidate selection and never clicks the final complaint submit button.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import Error as PlaywrightError, Page, sync_playwright  # noqa: E402

from apps.seller_portal_feedbacks_complaint_dry_run_plan import (  # noqa: E402
    apply_seller_portal_date_filter,
    apply_seller_portal_star_filter,
    click_filter_apply_button,
    feedback_list_signature,
    inspect_seller_portal_filter_state,
)
from apps.seller_portal_feedbacks_complaints_scout import (  # noqa: E402
    BUSINESS_TZ,
    DEFAULT_START_URL,
    ROW_MENU_COMPLAINT_LABEL,
    ScoutConfig,
    _click_safe_row_menu,
    _click_tab_like,
    _safe_escape,
    _wait_for_feedback_rows,
    _wait_settle,
    assert_safe_click_label,
    check_session,
    click_open_row_menu_complaint_action,
    close_modal_without_submit,
    detect_complaint_success_state,
    extract_complaint_modal_state,
    extract_open_row_menu_state,
    extract_visible_feedback_rows,
    navigate_to_feedbacks_questions,
)
from apps.seller_portal_feedbacks_matching_replay import (  # noqa: E402
    NO_SUBMIT_MODE,
    ReplayConfig,
    build_aggregate as build_matching_aggregate,
    classify_match,
    collect_feedback_rows_from_seller_portal_network,
    collect_feedback_rows_with_scroll,
    capture_seller_portal_feedback_headers,
    match_one_api_row,
    normalize_date_key,
    render_markdown_report as render_matching_markdown,
    safe_text,
    score_candidate,
    seller_portal_is_answered_values,
    summarize_api_row,
    summarize_ui_row,
    ui_row_identity,
    ui_row_matches_requested_filters,
)
from apps.seller_portal_relogin_session import DEFAULT_STORAGE_STATE_PATH, DEFAULT_WB_BOT_PYTHON  # noqa: E402
from packages.application.feedback_review_tags import normalize_review_tags  # noqa: E402
from packages.application.sheet_vitrina_v1_feedbacks import SheetVitrinaV1FeedbacksBlock  # noqa: E402


CONTRACT_NAME = "seller_portal_feedbacks_target_row_probe"
CONTRACT_VERSION = "read_only_v1"
DEFAULT_OUTPUT_ROOT = Path("/opt/wb-core-runtime/state/feedbacks_target_row_probe")
LOCAL_OUTPUT_ROOT = Path("artifacts/seller_portal_feedbacks_target_row_probe")
SELLER_PORTAL_WRITE_ACTIONS_ALLOWED = False
STATUS_TAB_UNANSWERED = "Ждут ответа"
STATUS_TAB_ANSWERED = "Есть ответ"
STATUS_TAB_ALL = "Отзывы"


@dataclass(frozen=True)
class TargetRowProbeConfig:
    date: str
    stars: tuple[int, ...]
    is_answered: str
    max_api_rows: int
    max_ui_rows: int
    open_menu: bool
    open_complaint_modal: bool
    mode: str
    storage_state_path: Path
    wb_bot_python: Path
    output_dir: Path
    start_url: str
    headless: bool
    timeout_ms: int
    write_artifacts: bool


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--stars", default="1")
    parser.add_argument("--is-answered", choices=("true", "false", "all"), default="all")
    parser.add_argument("--max-api-rows", type=int, default=20)
    parser.add_argument("--max-ui-rows", type=int, default=50)
    parser.add_argument("--open-menu", choices=("0", "1"), default="1")
    parser.add_argument("--open-complaint-modal", choices=("0", "1"), default="1")
    parser.add_argument("--mode", choices=(NO_SUBMIT_MODE, "read-only"), default="read-only")
    parser.add_argument("--storage-state-path", default=str(DEFAULT_STORAGE_STATE_PATH))
    parser.add_argument("--wb-bot-python", default=str(DEFAULT_WB_BOT_PYTHON))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--start-url", default=DEFAULT_START_URL)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--timeout-ms", type=int, default=20000)
    parser.add_argument("--no-artifacts", action="store_true")
    args = parser.parse_args()

    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else (DEFAULT_OUTPUT_ROOT if Path("/opt/wb-core-runtime/state").exists() else LOCAL_OUTPUT_ROOT)
    )
    config = TargetRowProbeConfig(
        date=normalize_requested_date(args.date),
        stars=parse_stars(args.stars),
        is_answered=args.is_answered,
        max_api_rows=max(1, int(args.max_api_rows)),
        max_ui_rows=max(1, int(args.max_ui_rows)),
        open_menu=str(args.open_menu) == "1",
        open_complaint_modal=str(args.open_complaint_modal) == "1",
        mode="read-only" if args.mode == "read-only" else NO_SUBMIT_MODE,
        storage_state_path=Path(args.storage_state_path).expanduser(),
        wb_bot_python=Path(args.wb_bot_python).expanduser(),
        output_dir=output_dir,
        start_url=str(args.start_url).rstrip("/") or DEFAULT_START_URL,
        headless=not args.headed,
        timeout_ms=max(5000, int(args.timeout_ms)),
        write_artifacts=not bool(args.no_artifacts),
    )
    report = run_probe(config)
    if config.write_artifacts:
        paths = write_report_artifacts(report, config.output_dir)
        report["artifact_paths"] = {key: str(path) for key, path in paths.items()}
    print(json.dumps(compact_stdout_report(report), ensure_ascii=False, indent=2))


def run_probe(config: TargetRowProbeConfig) -> dict[str, Any]:
    if config.mode not in {"read-only", NO_SUBMIT_MODE}:
        raise RuntimeError("target row probe supports read-only/no-submit mode only")
    started_at = iso_now()
    report: dict[str, Any] = {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "mode": "read-only",
        "started_at": started_at,
        "finished_at": None,
        "parameters": {
            "date": config.date,
            "date_from": config.date,
            "date_to": config.date,
            "stars": list(config.stars),
            "is_answered": config.is_answered,
            "max_api_rows": config.max_api_rows,
            "max_ui_rows": config.max_ui_rows,
            "open_menu": config.open_menu,
            "open_complaint_modal": config.open_complaint_modal,
        },
        "read_only_guards": no_submit_guards(config),
        "api": {},
        "session": {},
        "navigation": {},
        "seller_portal": {},
        "count_comparison": {},
        "matches": [],
        "matching_aggregate": {},
        "actionability": empty_actionability(),
        "errors": [],
    }
    api_report = load_api_rows(config)
    report["api"] = api_report
    api_rows = api_report.get("rows") if isinstance(api_report.get("rows"), list) else []
    seller_report = collect_seller_portal_rows(config, api_rows)
    report["session"] = seller_report.get("session") or {}
    report["navigation"] = seller_report.get("navigation") or {}
    report["seller_portal"] = seller_report.get("seller_portal") or {}
    report["errors"].extend(seller_report.get("errors") or [])
    dom_rows = (report["seller_portal"].get("dom") or {}).get("rows") or []
    cursor_rows = (report["seller_portal"].get("cursor") or {}).get("rows") or []
    matches = match_api_rows_to_dom(api_rows, dom_rows)
    report["matches"] = matches
    report["matching_aggregate"] = build_probe_match_aggregate(matches, api_rows, dom_rows)
    report["count_comparison"] = compare_counts(
        api_total_count=int(api_report.get("total_available_rows") or api_report.get("row_count") or 0),
        dom_rows=dom_rows,
        cursor_rows=cursor_rows,
    )
    if config.open_menu and seller_report.get("page_actionability"):
        report["actionability"] = seller_report["page_actionability"]
    report["read_only_guards"]["submit_clicked_count"] = int(bool((report["actionability"] or {}).get("submit_clicked")))
    report["finished_at"] = iso_now()
    return report


def load_api_rows(config: TargetRowProbeConfig) -> dict[str, Any]:
    report: dict[str, Any] = {
        "success": False,
        "requested": {
            "date_from": config.date,
            "date_to": config.date,
            "stars": list(config.stars),
            "is_answered": config.is_answered,
            "max_api_rows": config.max_api_rows,
        },
        "row_count": 0,
        "total_available_rows": 0,
        "limited": False,
        "rows_with_tags": 0,
        "sample_feedback_ids": [],
        "sample_rows": [],
        "rows": [],
        "meta": {},
        "blocker": "",
    }
    try:
        payload = SheetVitrinaV1FeedbacksBlock().build(
            date_from=config.date,
            date_to=config.date,
            stars=list(config.stars),
            is_answered=config.is_answered,
        )
    except Exception as exc:
        report["blocker"] = safe_text(str(exc), 500)
        report["error_code"] = exc.__class__.__name__
        return report
    all_rows = [row for row in payload.get("rows") or [] if isinstance(row, dict)]
    rows = all_rows[: config.max_api_rows]
    report.update(
        {
            "success": True,
            "row_count": len(rows),
            "total_available_rows": len(all_rows),
            "limited": len(all_rows) > len(rows),
            "rows_with_tags": sum(1 for row in all_rows if row.get("review_tags")),
            "sample_feedback_ids": [str(row.get("feedback_id") or "") for row in rows[:10]],
            "sample_rows": [summarize_api_row(row) for row in rows[:10]],
            "rows": rows,
            "meta": payload.get("meta") or {},
            "summary": payload.get("summary") or {},
        }
    )
    return report


def collect_seller_portal_rows(config: TargetRowProbeConfig, api_rows: list[dict[str, Any]]) -> dict[str, Any]:
    scout_config = build_scout_config(config)
    session = check_session(scout_config)
    report: dict[str, Any] = {
        "success": False,
        "session": session,
        "navigation": {},
        "seller_portal": empty_seller_portal_report(),
        "page_actionability": empty_actionability(),
        "errors": [],
    }
    if not session.get("ok"):
        report["seller_portal"]["blocker"] = "Seller Portal session is not valid"
        report["errors"].append(
            {
                "stage": "session",
                "code": str(session.get("status") or "session_invalid"),
                "message": str(session.get("message") or "Seller Portal session is not valid"),
            }
        )
        return report

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=config.headless)
            context = browser.new_context(
                storage_state=str(config.storage_state_path),
                locale="ru-RU",
                timezone_id=BUSINESS_TZ,
                viewport={"width": 1600, "height": 1200},
                accept_downloads=False,
            )
            page = context.new_page()
            page.set_default_timeout(config.timeout_ms)
            request_headers: dict[str, str] = {}
            page.on("request", lambda request: capture_seller_portal_feedback_headers(request, request_headers))
            try:
                navigation = navigate_to_feedbacks_questions(page, scout_config)
                report["navigation"] = navigation
                if not navigation.get("success"):
                    report["seller_portal"]["blocker"] = str(navigation.get("blocker") or "Отзывы и вопросы page not reached")
                    return report
                if not _click_tab_like(page, STATUS_TAB_ALL):
                    report["seller_portal"]["blocker"] = "Отзывы tab was not found"
                    return report
                _wait_settle(page, 2500)
                _wait_for_feedback_rows(page, timeout_ms=10000)
                tabs = status_tabs_for_request(config.is_answered)
                tab_reports: list[dict[str, Any]] = []
                dom_by_key: dict[str, dict[str, Any]] = {}
                for tab_label in tabs:
                    tab_report, rows = collect_dom_rows_for_tab(page, config, tab_label)
                    tab_reports.append(tab_report)
                    for row in rows:
                        key = ui_row_identity(row)
                        if not key or key in dom_by_key:
                            continue
                        dom_by_key[key] = row
                dom_rows = list(dom_by_key.values())[: config.max_ui_rows]
                for index, row in enumerate(dom_rows):
                    row["row_index"] = index
                replay_config = to_replay_config(config)
                cursor_rows, cursor_stats = collect_feedback_rows_from_seller_portal_network(
                    page,
                    replay_config,
                    request_headers=request_headers,
                )
                report["seller_portal"].update(
                    {
                        "success": bool(dom_rows or cursor_rows),
                        "tabs_checked": tabs,
                        "tab_reports": tab_reports,
                        "dom": {
                            "rows_collected": len(dom_rows),
                            "rows": dom_rows,
                            "sample_rows": [summarize_ui_row(row) for row in dom_rows[:10]],
                            "field_availability": field_availability_for_rows(dom_rows),
                        },
                        "cursor": {
                            "rows_collected": len(cursor_rows),
                            "rows": cursor_rows,
                            "sample_rows": [summarize_ui_row(row) for row in cursor_rows[:10]],
                            "stats": cursor_stats,
                            "feedback_id_available": any(bool(row.get("feedback_id")) for row in cursor_rows),
                        },
                        "filters": summarize_filter_application(tab_reports),
                        "blocker": "" if (dom_rows or cursor_rows) else "No Seller Portal rows collected after requested filters",
                    }
                )
                report["success"] = bool(dom_rows or cursor_rows)
                first_exact = first_exact_match(api_rows, dom_rows)
                if first_exact and config.open_menu:
                    report["page_actionability"] = materialize_and_check_actionability(page, config, first_exact)
            finally:
                context.close()
                browser.close()
    except Exception as exc:  # pragma: no cover - live browser fallback
        report["seller_portal"]["blocker"] = safe_text(str(exc), 500)
        report["errors"].append({"stage": "browser_probe", "code": exc.__class__.__name__, "message": safe_text(str(exc), 800)})
    return report


def collect_dom_rows_for_tab(page: Page, config: TargetRowProbeConfig, tab_label: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    report: dict[str, Any] = {
        "tab": tab_label,
        "tab_clicked": False,
        "date_filter_applied": False,
        "star_filter_applied": False,
        "selected_stars": [],
        "list_update_observed": False,
        "visible_rows_after_filter": 0,
        "rows_collected": 0,
        "scroll_stats": {},
        "filter_controller": {},
        "blocker": "",
    }
    clicked = _click_tab_like(page, tab_label)
    report["tab_clicked"] = bool(clicked)
    if not clicked:
        report["blocker"] = f"status tab {tab_label!r} was not found"
        return report, []
    _wait_settle(page, 1800)
    _wait_for_feedback_rows(page, timeout_ms=7000)
    filters = apply_probe_filters(page, config)
    report["filter_controller"] = filters
    report["date_filter_applied"] = bool(filters.get("date_filter_applied"))
    report["star_filter_applied"] = bool(filters.get("star_filter_applied"))
    report["selected_stars"] = filters.get("current_selected_stars") or []
    report["list_update_observed"] = bool(filters.get("list_update_observed"))
    _wait_for_feedback_rows(page, timeout_ms=7000)
    visible_now = extract_visible_feedback_rows(page, max_rows=max(10, min(config.max_ui_rows, 80)))
    report["visible_rows_after_filter"] = len(visible_now)
    rows, scroll_stats = collect_feedback_rows_with_scroll(page, max_rows=config.max_ui_rows, date_from=config.date)
    filtered_rows: list[dict[str, Any]] = []
    replay_config = to_replay_config(config)
    for row in rows:
        if not ui_row_matches_requested_filters(row, replay_config):
            continue
        enriched = dict(row)
        enriched["source"] = "seller_portal_dom"
        enriched["status_tab"] = tab_label
        enriched["tab_used"] = tab_label
        filtered_rows.append(enriched)
    report["rows_collected"] = len(filtered_rows)
    report["scroll_stats"] = scroll_stats
    if not filtered_rows and not report["blocker"]:
        report["blocker"] = "No DOM rows matched requested date/star filters in this status tab"
    _safe_escape(page)
    return report, filtered_rows


def apply_probe_filters(page: Page, config: TargetRowProbeConfig) -> dict[str, Any]:
    before_signature = feedback_list_signature(page)
    date_result = apply_seller_portal_date_filter(page, date_from=config.date, date_to=config.date)
    _wait_settle(page, 900)
    star_result = apply_seller_portal_star_filter(page, stars=config.stars)
    _wait_settle(page, 1200)
    after_signature = feedback_list_signature(page)
    state = inspect_seller_portal_filter_state(page)
    return {
        "requested_date_from": config.date,
        "requested_date_to": config.date,
        "requested_stars": list(config.stars),
        "date_filter": date_result,
        "star_filter": star_result,
        "date_filter_applied": bool(date_result.get("applied")),
        "star_filter_applied": bool(star_result.get("applied")),
        "status_tab_selected": True,
        "list_signature_before": before_signature,
        "list_signature_after": after_signature,
        "list_update_observed": bool(after_signature.get("fingerprint") and after_signature.get("fingerprint") != before_signature.get("fingerprint")),
        "current_visible_date_range": state.get("visible_date_range") or "",
        "current_selected_stars": state.get("selected_stars") or star_result.get("selected_stars") or [],
        "selectors_used": [*(date_result.get("selectors_used") or []), *(star_result.get("selectors_used") or [])],
        "blocker": str(date_result.get("reason") or star_result.get("reason") or ""),
    }


def match_api_rows_to_dom(api_rows: list[dict[str, Any]], dom_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [match_one_api_row_to_dom(api_row, dom_rows) for api_row in api_rows]


def match_one_api_row_to_dom(api_row: Mapping[str, Any], dom_rows: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [score_candidate(api_row, row) for row in dom_rows]
    scored.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
    best = scored[0] if scored else {
        "score": 0.0,
        "ui_row": {},
        "matched_fields": [],
        "missing_fields": [],
        "mismatched_fields": [],
        "reasons": ["no DOM rows collected"],
        "text_similarity": 0.0,
        "text_containment": 0.0,
    }
    close_candidates = [
        item
        for item in scored
        if float(item.get("score") or 0.0) >= 0.5 and float(best.get("score") or 0.0) - float(item.get("score") or 0.0) <= 0.08
    ]
    status = classify_match(best, ambiguity_count=len(close_candidates))
    ui_row = best.get("ui_row") if isinstance(best.get("ui_row"), dict) else {}
    return {
        "api_feedback_id": str(api_row.get("feedback_id") or ""),
        "found_in_seller_portal_dom": status in {"exact", "high"},
        "match_status": status,
        "match_score": float(best.get("score") or 0.0),
        "matched_fields": best.get("matched_fields") or [],
        "missing_fields": best.get("missing_fields") or [],
        "mismatched_fields": best.get("mismatched_fields") or [],
        "match_reason": "; ".join(str(item) for item in best.get("reasons") or []),
        "text_similarity": best.get("text_similarity", 0.0),
        "text_containment": best.get("text_containment", 0.0),
        "tab_used": str(ui_row.get("tab_used") or ui_row.get("status_tab") or ""),
        "row_index": ui_row.get("row_index", ui_row.get("ui_collection_index")),
        "ui_row_text_snippet": safe_text(str(ui_row.get("text_snippet") or ""), 260),
        "ui_review_tags": normalize_review_tags(ui_row.get("review_tags") or []),
        "api_summary": summarize_api_row(api_row),
        "best_ui_candidate": summarize_ui_row(ui_row),
        "dom_scout_id": str(ui_row.get("dom_scout_id") or ""),
        "safe_for_actionability_probe": status == "exact" and bool(ui_row.get("dom_scout_id")),
    }


def first_exact_match(api_rows: list[dict[str, Any]], dom_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for match in match_api_rows_to_dom(api_rows, dom_rows):
        if match.get("match_status") == "exact" and match.get("dom_scout_id"):
            return match
    return None


def materialize_and_check_actionability(page: Page, config: TargetRowProbeConfig, match: Mapping[str, Any]) -> dict[str, Any]:
    result = empty_actionability()
    result.update(
        {
            "requested": True,
            "feedback_id": str(match.get("api_feedback_id") or ""),
            "tab_used": str(match.get("tab_used") or ""),
            "match": dict(match),
        }
    )
    tab_label = str(match.get("tab_used") or "") or status_tabs_for_request(config.is_answered)[0]
    if not _click_tab_like(page, tab_label):
        result["blocker"] = f"status tab {tab_label!r} was not found for actionability"
        return result
    _wait_settle(page, 1200)
    filters = apply_probe_filters(page, config)
    result["filter_controller"] = filters
    visible_match = find_exact_visible_dom_row(page, match)
    if not visible_match.get("row"):
        scroll_result = scroll_until_exact_visible(page, config, match)
        visible_match = scroll_result
        result["scroll_used"] = bool(scroll_result.get("scroll_attempts"))
        result["scroll_attempts"] = scroll_result.get("scroll_attempts") or []
    row = visible_match.get("row") if isinstance(visible_match.get("row"), dict) else {}
    result["row_visible"] = bool(row)
    result["visible_row_match"] = visible_match.get("match") or {}
    if not row:
        result["blocker"] = "exact DOM row was collected earlier, but could not be materialized for menu check"
        return result
    result["row_index"] = row.get("row_index", row.get("ui_collection_index"))
    dom_id = str(row.get("dom_scout_id") or "")
    if not dom_id:
        result["blocker"] = "materialized row has no DOM id for menu click"
        return result
    clicked_menu = _click_safe_row_menu(page, dom_id)
    result["row_menu_click"] = clicked_menu
    result["row_menu_found"] = bool(clicked_menu.get("ok"))
    if not clicked_menu.get("ok"):
        result["blocker"] = str(clicked_menu.get("reason") or "row menu not found")
        return result
    _wait_settle(page, 800)
    menu_state = extract_open_row_menu_state(page)
    result["menu_items"] = menu_state.get("items") or []
    result["complaint_action_found"] = bool(menu_state.get("complaint_action_found"))
    if not result["complaint_action_found"] or not config.open_complaint_modal:
        _safe_escape(page)
        result["modal_opened"] = False
        result["modal_closed"] = True
        result["blocker"] = "" if result["complaint_action_found"] else "Пожаловаться на отзыв action not found in row menu"
        return result
    assert_safe_click_label(ROW_MENU_COMPLAINT_LABEL, purpose="open_complaint_modal")
    action_click = click_open_row_menu_complaint_action(page)
    result["complaint_action_click"] = action_click
    if not action_click.get("ok"):
        result["blocker"] = str(action_click.get("reason") or "complaint action could not be clicked")
        _safe_escape(page)
        return result
    _wait_settle(page, 1500)
    modal_state = extract_complaint_modal_state(page)
    result["modal_opened"] = bool(modal_state.get("opened"))
    result["categories_found"] = modal_state.get("categories") or []
    result["description_field_found"] = bool(modal_state.get("description_field_found"))
    result["submit_button_seen"] = bool(modal_state.get("submit_button_seen"))
    result["submit_button_label"] = str(modal_state.get("submit_button_label") or "")
    result["submit_clicked"] = False
    result["durable_success_state_seen"] = bool(detect_complaint_success_state(page).get("seen"))
    result["close_method"] = close_modal_without_submit(page)
    _wait_settle(page, 600)
    result["modal_closed"] = not bool(extract_complaint_modal_state(page).get("opened"))
    result["durable_success_state_after_close"] = bool(detect_complaint_success_state(page).get("seen"))
    return result


def find_exact_visible_dom_row(page: Page, match: Mapping[str, Any]) -> dict[str, Any]:
    api_summary = match.get("api_summary") if isinstance(match.get("api_summary"), dict) else {}
    api_row = api_summary_to_match_row(api_summary, str(match.get("api_feedback_id") or ""))
    rows = extract_visible_feedback_rows(page, max_rows=80)
    best = match_one_api_row_to_dom(api_row, rows)
    row = {}
    if best.get("match_status") == "exact":
        dom_id = str(best.get("dom_scout_id") or "")
        row = next((item for item in rows if str(item.get("dom_scout_id") or "") == dom_id), {})
    return {"row": row, "match": best, "visible_rows_checked": len(rows)}


def scroll_until_exact_visible(page: Page, config: TargetRowProbeConfig, match: Mapping[str, Any]) -> dict[str, Any]:
    scroll_attempts: list[dict[str, Any]] = []
    for _ in range(12):
        visible = find_exact_visible_dom_row(page, match)
        if visible.get("row"):
            visible["scroll_attempts"] = scroll_attempts
            return visible
        try:
            scroll = page.evaluate(
                r"""
() => {
  const target = document.scrollingElement || document.documentElement;
  const before = target.scrollTop;
  window.scrollBy(0, Math.max(360, Math.floor(window.innerHeight * 0.85)));
  return {before, after: target.scrollTop, changed: target.scrollTop !== before};
}
                """
            )
        except PlaywrightError as exc:
            scroll = {"changed": False, "error": safe_text(str(exc), 200)}
        scroll_attempts.append(scroll)
        _wait_settle(page, 900)
        if not scroll.get("changed"):
            break
    return {"row": {}, "match": {}, "scroll_attempts": scroll_attempts, "visible_rows_checked": 0}


def api_summary_to_match_row(summary: Mapping[str, Any], feedback_id: str) -> dict[str, Any]:
    return {
        "feedback_id": feedback_id,
        "created_at": str(summary.get("created_at") or ""),
        "created_date": str(summary.get("created_date") or ""),
        "product_valuation": summary.get("rating"),
        "text": str(summary.get("review_text") or ""),
        "pros": "",
        "cons": "",
        "review_tags": summary.get("review_tags") or [],
        "nm_id": summary.get("nm_id"),
        "supplier_article": summary.get("supplier_article"),
        "product_name": summary.get("product_name"),
        "is_answered": summary.get("is_answered"),
        "photo_count": summary.get("photo_count"),
        "video_count": summary.get("video_count"),
    }


def compare_counts(*, api_total_count: int, dom_rows: list[dict[str, Any]], cursor_rows: list[dict[str, Any]]) -> dict[str, Any]:
    dom_count = len(dom_rows)
    cursor_count = len(cursor_rows)
    return {
        "api_count": api_total_count,
        "seller_portal_dom_count": dom_count,
        "seller_portal_cursor_count": cursor_count,
        "counts_match": api_total_count == dom_count,
        "cursor_counts_match": api_total_count == cursor_count,
        "diagnostics": {
            "dom_minus_api": dom_count - api_total_count,
            "cursor_minus_api": cursor_count - api_total_count,
            "status_tab_split": dict(Counter(str(row.get("status_tab") or "") for row in dom_rows)),
            "dom_dates": sorted({normalize_date_key(row.get("review_date") or row.get("review_datetime")) for row in dom_rows if normalize_date_key(row.get("review_date") or row.get("review_datetime"))}),
            "cursor_dates": sorted({normalize_date_key(row.get("review_date") or row.get("review_datetime")) for row in cursor_rows if normalize_date_key(row.get("review_date") or row.get("review_datetime"))}),
        },
    }


def build_probe_match_aggregate(matches: list[dict[str, Any]], api_rows: list[dict[str, Any]], dom_rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(str(match.get("match_status") or "not_found") for match in matches)
    return {
        "api_rows_tested": len(api_rows),
        "dom_rows_collected": len(dom_rows),
        "exact_count": counts.get("exact", 0),
        "high_count": counts.get("high", 0),
        "ambiguous_count": counts.get("ambiguous", 0),
        "not_found_count": counts.get("not_found", 0),
        "found_in_seller_portal_dom_count": sum(1 for match in matches if match.get("found_in_seller_portal_dom")),
        "first_exact_feedback_id": next((str(match.get("api_feedback_id") or "") for match in matches if match.get("match_status") == "exact"), ""),
        "matching_replay_aggregate": build_matching_aggregate(
            [match_one_api_row(row, dom_rows) for row in api_rows],
            api_rows,
            dom_rows,
        ),
    }


def status_tabs_for_request(is_answered: str) -> list[str]:
    if is_answered == "true":
        return [STATUS_TAB_ANSWERED]
    if is_answered == "false":
        return [STATUS_TAB_UNANSWERED]
    return [STATUS_TAB_UNANSWERED, STATUS_TAB_ANSWERED]


def build_scout_config(config: TargetRowProbeConfig) -> ScoutConfig:
    return ScoutConfig(
        mode="scout-feedbacks",
        storage_state_path=config.storage_state_path,
        wb_bot_python=config.wb_bot_python,
        output_root=config.output_dir,
        start_url=config.start_url,
        max_feedback_rows=config.max_ui_rows,
        max_complaint_rows=1,
        max_modal_reviews=0,
        open_complaint_modal=False,
        headless=config.headless,
        timeout_ms=config.timeout_ms,
        write_artifacts=False,
    )


def to_replay_config(config: TargetRowProbeConfig) -> ReplayConfig:
    return ReplayConfig(
        date_from=config.date,
        date_to=config.date,
        stars=config.stars,
        is_answered=config.is_answered,
        max_api_rows=config.max_api_rows,
        max_ui_rows=config.max_ui_rows,
        mode=NO_SUBMIT_MODE,
        storage_state_path=config.storage_state_path,
        wb_bot_python=config.wb_bot_python,
        output_dir=config.output_dir,
        start_url=config.start_url,
        headless=config.headless,
        timeout_ms=config.timeout_ms,
        write_artifacts=False,
        apply_ui_filters="yes",
        targeted_search="no",
        max_targeted_searches=0,
    )


def summarize_filter_application(tab_reports: list[Mapping[str, Any]]) -> dict[str, Any]:
    clicked = [item for item in tab_reports if item.get("tab_clicked")]
    return {
        "status_tabs_checked": [str(item.get("tab") or "") for item in tab_reports],
        "status_tab_selected": bool(clicked),
        "date_filter_applied": bool(clicked) and all(bool(item.get("date_filter_applied")) for item in clicked),
        "star_filter_applied": bool(clicked) and all(bool(item.get("star_filter_applied")) for item in clicked),
        "list_update_observed": any(bool(item.get("list_update_observed")) for item in clicked),
        "selected_stars": sorted({int(star) for item in clicked for star in item.get("selected_stars") or [] if str(star).isdigit()}),
        "rows_visible_after_filter": sum(int(item.get("visible_rows_after_filter") or 0) for item in clicked),
        "rows_collected": sum(int(item.get("rows_collected") or 0) for item in clicked),
        "blockers": [str(item.get("blocker") or "") for item in tab_reports if item.get("blocker")],
    }


def field_availability_for_rows(rows: list[dict[str, Any]]) -> dict[str, bool]:
    fields = (
        "dom_scout_id",
        "feedback_id",
        "hidden_feedback_id",
        "review_datetime",
        "review_date",
        "rating",
        "nm_id",
        "supplier_article",
        "product_title",
        "text_snippet",
        "review_tags",
        "three_dot_menu_found",
    )
    return {field: any(bool(row.get(field)) for row in rows) for field in fields}


def no_submit_guards(config: TargetRowProbeConfig | None = None) -> dict[str, Any]:
    return {
        "mode": "read-only",
        "seller_portal_write_actions_allowed": SELLER_PORTAL_WRITE_ACTIONS_ALLOWED,
        "complaint_submit_clicked": False,
        "complaint_submit_path_called": False,
        "complaint_final_submit_allowed": False,
        "complaint_modal_open_requested": bool(config.open_complaint_modal) if config else False,
        "complaint_modal_open_allowed": bool(config.open_complaint_modal) if config else True,
        "answer_edit_clicked": False,
        "status_persistence_allowed": False,
        "journal_write_allowed": False,
        "submit_clicked_count": 0,
    }


def empty_seller_portal_report() -> dict[str, Any]:
    return {
        "success": False,
        "tabs_checked": [],
        "tab_reports": [],
        "filters": {},
        "dom": {"rows_collected": 0, "rows": [], "sample_rows": [], "field_availability": {}},
        "cursor": {"rows_collected": 0, "rows": [], "sample_rows": [], "stats": {}, "feedback_id_available": False},
        "blocker": "",
    }


def empty_actionability() -> dict[str, Any]:
    return {
        "requested": False,
        "feedback_id": "",
        "tab_used": "",
        "row_visible": False,
        "row_menu_found": False,
        "menu_items": [],
        "complaint_action_found": False,
        "modal_opened": False,
        "categories_found": [],
        "description_field_found": False,
        "submit_button_seen": False,
        "submit_button_label": "",
        "modal_closed": False,
        "submit_clicked": False,
        "durable_success_state_seen": False,
        "durable_success_state_after_close": False,
        "journal_records_created": 0,
        "blocker": "",
    }


def render_markdown_report(report: Mapping[str, Any]) -> str:
    params = report.get("parameters") or {}
    api = report.get("api") or {}
    sp = report.get("seller_portal") or {}
    filters = sp.get("filters") or {}
    comparison = report.get("count_comparison") or {}
    aggregate = report.get("matching_aggregate") or {}
    action = report.get("actionability") or {}
    lines = [
        "# Seller Portal Feedback Target Row Probe",
        "",
        f"- Mode: `{report.get('mode')}`",
        f"- Started: `{report.get('started_at')}`",
        f"- Finished: `{report.get('finished_at')}`",
        f"- Date: `{params.get('date')}`",
        f"- Stars: `{','.join(str(item) for item in params.get('stars') or [])}`",
        f"- is_answered: `{params.get('is_answered')}`",
        f"- API count: `{comparison.get('api_count')}`",
        f"- Seller Portal DOM count: `{comparison.get('seller_portal_dom_count')}`",
        f"- Seller Portal cursor count: `{comparison.get('seller_portal_cursor_count')}`",
        f"- Counts match DOM/cursor: `{comparison.get('counts_match')}` / `{comparison.get('cursor_counts_match')}`",
        f"- Date filter applied: `{filters.get('date_filter_applied')}`",
        f"- Star filter applied: `{filters.get('star_filter_applied')}`",
        f"- Status tabs checked: `{', '.join(filters.get('status_tabs_checked') or [])}`",
        f"- Rows visible/collected: `{filters.get('rows_visible_after_filter')}` / `{filters.get('rows_collected')}`",
        f"- Exact/high/ambiguous/not_found: `{aggregate.get('exact_count', 0)}` / `{aggregate.get('high_count', 0)}` / `{aggregate.get('ambiguous_count', 0)}` / `{aggregate.get('not_found_count', 0)}`",
        f"- First exact feedback_id: `{aggregate.get('first_exact_feedback_id')}`",
        f"- Submit clicked: `{(report.get('read_only_guards') or {}).get('submit_clicked_count')}`",
        "",
        "## API Samples",
        "",
    ]
    for row in api.get("sample_rows") or []:
        lines.append(
            f"- `{row.get('feedback_id')}` `{row.get('created_at')}` rating `{row.get('rating')}` nm `{row.get('nm_id')}` article `{row.get('supplier_article')}` tags `{', '.join(row.get('review_tags') or [])}` text `{row.get('review_text')}`"
        )
    lines.extend(["", "## Matches", ""])
    for match in report.get("matches") or []:
        lines.extend(
            [
                f"- `{match.get('match_status')}` score `{match.get('match_score')}` found `{match.get('found_in_seller_portal_dom')}` feedback `{match.get('api_feedback_id')}` tab `{match.get('tab_used')}` row `{match.get('row_index')}`",
                f"  Matched: `{', '.join(match.get('matched_fields') or [])}` Missing: `{', '.join(match.get('missing_fields') or [])}` Mismatched: `{', '.join(match.get('mismatched_fields') or [])}`",
                f"  UI: `{match.get('ui_row_text_snippet')}` tags `{', '.join(match.get('ui_review_tags') or [])}`",
            ]
        )
    lines.extend(
        [
            "",
            "## Actionability",
            "",
            f"- Requested: `{action.get('requested')}`",
            f"- Row menu found: `{action.get('row_menu_found')}`",
            f"- Menu items: `{', '.join(action.get('menu_items') or [])}`",
            f"- Complaint action found: `{action.get('complaint_action_found')}`",
            f"- Modal opened: `{action.get('modal_opened')}`",
            f"- Categories: `{', '.join(action.get('categories_found') or [])}`",
            f"- Modal closed: `{action.get('modal_closed')}`",
            f"- Submit clicked: `{action.get('submit_clicked')}`",
            f"- Blocker: `{action.get('blocker')}`",
        ]
    )
    if report.get("errors"):
        lines.extend(["", "## Errors", ""])
        for error in report["errors"]:
            lines.append(f"- `{error.get('stage')}` / `{error.get('code')}`: {error.get('message')}")
    return "\n".join(lines) + "\n"


def write_report_artifacts(report: dict[str, Any], output_root: Path) -> dict[str, Path]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    json_path = run_dir / "seller_portal_feedbacks_target_row_probe.json"
    md_path = run_dir / "seller_portal_feedbacks_target_row_probe.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown_report(report), encoding="utf-8")
    return {"run_dir": run_dir, "json": json_path, "markdown": md_path}


def compact_stdout_report(report: Mapping[str, Any]) -> dict[str, Any]:
    sp = report.get("seller_portal") or {}
    return {
        "contract_name": report.get("contract_name"),
        "mode": report.get("mode"),
        "parameters": report.get("parameters"),
        "read_only_guards": report.get("read_only_guards"),
        "api": {
            key: (report.get("api") or {}).get(key)
            for key in ("success", "row_count", "total_available_rows", "limited", "rows_with_tags", "sample_feedback_ids", "blocker")
        },
        "session": report.get("session"),
        "navigation": report.get("navigation"),
        "seller_portal": {
            "success": sp.get("success"),
            "tabs_checked": sp.get("tabs_checked"),
            "filters": sp.get("filters"),
            "dom_rows_collected": (sp.get("dom") or {}).get("rows_collected"),
            "cursor_rows_collected": (sp.get("cursor") or {}).get("rows_collected"),
            "cursor_feedback_id_available": (sp.get("cursor") or {}).get("feedback_id_available"),
            "blocker": sp.get("blocker"),
        },
        "count_comparison": report.get("count_comparison"),
        "matching_aggregate": report.get("matching_aggregate"),
        "actionability": report.get("actionability"),
        "errors": report.get("errors"),
        "artifact_paths": report.get("artifact_paths"),
    }


def parse_stars(value: str) -> tuple[int, ...]:
    stars = sorted({int(part.strip()) for part in str(value or "").split(",") if part.strip()})
    if not stars or any(star < 1 or star > 5 for star in stars):
        raise ValueError("--stars must contain comma-separated values from 1 to 5")
    return tuple(stars)


def normalize_requested_date(value: str) -> str:
    date_key = normalize_date_key(value)
    if not date_key:
        raise ValueError(f"invalid --date: {value!r}")
    return date_key


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    main()
