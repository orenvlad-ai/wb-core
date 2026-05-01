"""No-submit Seller Portal replay that matches WB API feedback rows to UI rows.

The runner is intentionally read-only. It loads canonical feedback rows through
the existing sheet_vitrina_v1 feedbacks block, collects bounded Seller Portal UI
rows with the existing storage_state contour and returns match quality for a
future complaint workflow. It never opens complaint submit paths.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
import time
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import Error as PlaywrightError, Page, sync_playwright  # noqa: E402

from apps.seller_portal_feedbacks_complaints_scout import (  # noqa: E402
    BUSINESS_TZ,
    DEFAULT_START_URL,
    ScoutConfig,
    _click_tab_like,
    _wait_for_feedback_rows,
    _wait_settle,
    check_session,
    extract_visible_feedback_rows,
    field_availability,
    navigate_to_feedbacks_questions,
)
from apps.seller_portal_relogin_session import (  # noqa: E402
    DEFAULT_STORAGE_STATE_PATH,
    DEFAULT_WB_BOT_PYTHON,
)
from packages.application.sheet_vitrina_v1_feedbacks import SheetVitrinaV1FeedbacksBlock  # noqa: E402


DEFAULT_OUTPUT_ROOT = Path("/opt/wb-core-runtime/state/feedbacks_matching_replay")
LOCAL_OUTPUT_ROOT = Path("artifacts/seller_portal_feedbacks_matching_replay")
CONTRACT_NAME = "seller_portal_feedbacks_matching_replay"
CONTRACT_VERSION = "no_submit_v1"
NO_SUBMIT_MODE = "no-submit"
SELLER_PORTAL_WRITE_ACTIONS_ALLOWED = False
TEXT_WS_RE = re.compile(r"\s+")
REPEATED_PUNCT_RE = re.compile(r"([!?.,:;…])\1+")
UI_DATE_TIME_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{2,4})(?:\s+в)?\s+(\d{1,2}):(\d{2})\b")
UI_DATE_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{2,4})\b")
ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
ARTICLE_DIGITS_RE = re.compile(r"\d{5,}")
MAX_SCROLL_ATTEMPTS = 30
MAX_NETWORK_PAGES = 30
SELLER_PORTAL_NETWORK_PAGE_LIMIT = 20
SELLER_PORTAL_FEEDBACKS_ENDPOINT = (
    "https://seller-reviews.wildberries.ru/ns/fa-seller-api/reviews-ext-seller-portal/api/v2/feedbacks"
)


@dataclass(frozen=True)
class ReplayConfig:
    date_from: str
    date_to: str
    stars: tuple[int, ...]
    is_answered: str
    max_api_rows: int
    max_ui_rows: int
    mode: str
    storage_state_path: Path
    wb_bot_python: Path
    output_dir: Path
    start_url: str
    headless: bool
    timeout_ms: int
    write_artifacts: bool
    apply_ui_filters: str
    targeted_search: str
    max_targeted_searches: int


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--stars", default="1,2,3,4,5")
    parser.add_argument("--is-answered", choices=("true", "false", "all"), default="false")
    parser.add_argument("--max-api-rows", type=int, default=25)
    parser.add_argument("--max-ui-rows", type=int, default=100)
    parser.add_argument("--mode", choices=(NO_SUBMIT_MODE,), default=NO_SUBMIT_MODE)
    parser.add_argument("--apply-ui-filters", choices=("auto", "yes", "no"), default="auto")
    parser.add_argument("--targeted-search", choices=("auto", "yes", "no"), default="auto")
    parser.add_argument("--max-targeted-searches", type=int, default=10)
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
    config = ReplayConfig(
        date_from=_normalize_requested_date(args.date_from),
        date_to=_normalize_requested_date(args.date_to),
        stars=parse_stars(args.stars),
        is_answered=args.is_answered,
        max_api_rows=max(1, int(args.max_api_rows)),
        max_ui_rows=max(1, int(args.max_ui_rows)),
        mode=args.mode,
        storage_state_path=Path(args.storage_state_path).expanduser(),
        wb_bot_python=Path(args.wb_bot_python).expanduser(),
        output_dir=output_dir,
        start_url=str(args.start_url).rstrip("/") or DEFAULT_START_URL,
        headless=not args.headed,
        timeout_ms=max(5000, int(args.timeout_ms)),
        write_artifacts=not bool(args.no_artifacts),
        apply_ui_filters=args.apply_ui_filters,
        targeted_search=args.targeted_search,
        max_targeted_searches=max(0, int(args.max_targeted_searches)),
    )
    report = run_replay(config)
    if config.write_artifacts:
        paths = write_report_artifacts(report, config.output_dir)
        report["artifact_paths"] = {key: str(path) for key, path in paths.items()}
    print(json.dumps(compact_stdout_report(report), ensure_ascii=False, indent=2))


def run_replay(config: ReplayConfig) -> dict[str, Any]:
    if config.mode != NO_SUBMIT_MODE:
        raise RuntimeError("seller_portal_feedbacks_matching_replay supports no-submit mode only")
    started_at = iso_now()
    report: dict[str, Any] = {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "mode": config.mode,
        "started_at": started_at,
        "finished_at": None,
        "parameters": {
            "date_from": config.date_from,
            "date_to": config.date_to,
            "stars": list(config.stars),
            "is_answered": config.is_answered,
            "max_api_rows": config.max_api_rows,
            "max_ui_rows": config.max_ui_rows,
            "apply_ui_filters": config.apply_ui_filters,
            "targeted_search": config.targeted_search,
            "max_targeted_searches": config.max_targeted_searches,
        },
        "read_only_guards": no_submit_guards(),
        "api": {},
        "session": {},
        "navigation": {},
        "ui": {},
        "matches": [],
        "aggregate": empty_aggregate(),
        "recommendation": {},
        "errors": [],
    }

    api_report = load_api_feedback_rows(config)
    report["api"] = api_report
    ui_report = collect_seller_portal_ui_rows(config)
    report["session"] = ui_report.get("session") or {}
    report["navigation"] = ui_report.get("navigation") or {}
    report["ui"] = ui_report.get("ui") or {}
    if ui_report.get("errors"):
        report["errors"].extend(ui_report["errors"])

    api_rows = api_report.get("rows") if isinstance(api_report.get("rows"), list) else []
    ui_rows = report["ui"].get("rows") if isinstance(report["ui"].get("rows"), list) else []
    if api_report.get("success") and ui_report.get("success"):
        matches = match_api_rows_to_ui(api_rows, ui_rows)
        report["matches"] = matches
        report["aggregate"] = build_aggregate(matches, api_rows, ui_rows)
        report["recommendation"] = build_recommendation(report["aggregate"], report)
    else:
        report["aggregate"] = build_aggregate([], api_rows, ui_rows)
        report["recommendation"] = build_recommendation(report["aggregate"], report)
    report["finished_at"] = iso_now()
    return report


def parse_stars(value: str) -> tuple[int, ...]:
    stars = sorted({int(part.strip()) for part in str(value or "").split(",") if part.strip()})
    if not stars or any(star < 1 or star > 5 for star in stars):
        raise ValueError("--stars must contain comma-separated values from 1 to 5")
    return tuple(stars)


def load_api_feedback_rows(config: ReplayConfig) -> dict[str, Any]:
    report: dict[str, Any] = {
        "success": False,
        "requested": {
            "date_from": config.date_from,
            "date_to": config.date_to,
            "stars": list(config.stars),
            "is_answered": config.is_answered,
            "max_api_rows": config.max_api_rows,
        },
        "row_count": 0,
        "limited": False,
        "fields_available": {},
        "feedback_id_available": False,
        "meta": {},
        "rows": [],
        "blocker": "",
    }
    try:
        payload = SheetVitrinaV1FeedbacksBlock().build(
            date_from=config.date_from,
            date_to=config.date_to,
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
            "fields_available": availability(rows, API_MATCH_FIELDS),
            "feedback_id_available": any(bool(row.get("feedback_id")) for row in rows),
            "meta": payload.get("meta") or {},
            "summary": payload.get("summary") or {},
            "rows": rows,
        }
    )
    return report


def collect_seller_portal_ui_rows(config: ReplayConfig) -> dict[str, Any]:
    scout_config = build_scout_config(config)
    session = check_session(scout_config)
    report: dict[str, Any] = {
        "success": False,
        "session": session,
        "navigation": {},
        "ui": {
            "success": False,
            "rows_collected": 0,
            "dom_rows_collected": 0,
            "seller_portal_network_rows_collected": 0,
            "collection_strategy": "none",
            "rows": [],
            "filters": {
                "requested_is_answered": config.is_answered,
                "applied": "none",
                "limitation": "",
            },
            "scroll_stats": {},
            "seller_portal_network_stats": {},
            "targeted_search_stats": {},
            "field_availability": {},
            "hidden_feedback_id_available": False,
            "seller_portal_network_feedback_id_available": False,
            "selectors": [],
            "blocker": "",
        },
        "errors": [],
    }
    if not session.get("ok"):
        report["ui"]["blocker"] = "Seller Portal session is not valid"
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
            seller_portal_feedback_headers: dict[str, str] = {}
            page.on(
                "request",
                lambda request: capture_seller_portal_feedback_headers(request, seller_portal_feedback_headers),
            )
            try:
                navigation = navigate_to_feedbacks_questions(page, scout_config)
                report["navigation"] = navigation
                if not navigation.get("success"):
                    report["ui"]["blocker"] = str(navigation.get("blocker") or "Отзывы и вопросы page not reached")
                    return report
                if not _click_tab_like(page, "Отзывы"):
                    report["ui"]["blocker"] = "Отзывы tab was not found"
                    return report
                _wait_settle(page, 2500)
                _wait_for_feedback_rows(page, timeout_ms=10000)
                filters = describe_ui_filter_alignment(page, config)
                dom_rows, scroll_stats = collect_feedback_rows_with_scroll(
                    page,
                    max_rows=config.max_ui_rows,
                    date_from=config.date_from,
                )
                network_rows, network_stats = collect_feedback_rows_from_seller_portal_network(
                    page,
                    config,
                    request_headers=seller_portal_feedback_headers,
                )
                targeted_stats = build_targeted_search_stats(config, network_rows, dom_rows)
                rows = network_rows if network_rows else dom_rows
                collection_strategy = "seller_portal_network_cursor" if network_rows else "dom_scroll"
                report["ui"].update(
                    {
                        "success": bool(rows),
                        "rows_collected": len(rows),
                        "dom_rows_collected": len(dom_rows),
                        "seller_portal_network_rows_collected": len(network_rows),
                        "collection_strategy": collection_strategy if rows else "none",
                        "rows": rows,
                        "filters": filters,
                        "scroll_stats": scroll_stats,
                        "seller_portal_network_stats": network_stats,
                        "targeted_search_stats": targeted_stats,
                        "field_availability": field_availability(rows),
                        "hidden_feedback_id_available": any(bool(row.get("hidden_feedback_id")) for row in rows),
                        "seller_portal_network_feedback_id_available": any(
                            bool(row.get("seller_portal_feedback_id") or row.get("feedback_id")) for row in rows
                        ),
                        "selectors": sorted({selector for row in rows for selector in row.get("selector_hints", [])})[:30],
                        "blocker": "" if rows else "No Seller Portal UI feedback rows were collected",
                    }
                )
                report["success"] = bool(rows)
            finally:
                context.close()
                browser.close()
    except Exception as exc:  # pragma: no cover - live browser fallback
        report["ui"]["blocker"] = safe_text(str(exc), 500)
        report["errors"].append(
            {
                "stage": "browser_replay",
                "code": exc.__class__.__name__,
                "message": safe_text(str(exc), 800),
            }
        )
    return report


def build_scout_config(config: ReplayConfig) -> ScoutConfig:
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


def describe_ui_filter_alignment(page: Page, config: ReplayConfig) -> dict[str, Any]:
    url = page.url
    route_unanswered = "not-answered" in url
    route_answered = "answered" in url and "not-answered" not in url
    status_aligned = (
        (config.is_answered == "false" and route_unanswered)
        or (config.is_answered == "true" and route_answered)
        or config.is_answered == "all"
    )
    limitation = (
        "Seller Portal DOM renders only the first cursor page. Browser UI date/star controls are not changed; "
        "the replay uses read-only Seller Portal cursor pagination and applies requested date/star filters client-side."
    )
    if config.apply_ui_filters == "no":
        limitation = (
            "UI filter changes were disabled by --apply-ui-filters=no. The status route is observed, and requested "
            "date/star filters are applied only to collected read-only rows."
        )
    if config.is_answered == "false" and route_unanswered:
        return {
            "requested_is_answered": config.is_answered,
            "requested_apply_ui_filters": config.apply_ui_filters,
            "applied": "route_not_answered",
            "status_tab_selected": True,
            "date_filter_applied": False,
            "stars_filter_applied": False,
            "safe_filter_alignment": "partial",
            "limitation": limitation,
            "url": url,
        }
    return {
        "requested_is_answered": config.is_answered,
        "requested_apply_ui_filters": config.apply_ui_filters,
        "applied": "route_aligned" if status_aligned else "none",
        "status_tab_selected": bool(status_aligned),
        "date_filter_applied": False,
        "stars_filter_applied": False,
        "safe_filter_alignment": "partial" if status_aligned else "none",
        "limitation": limitation
        if status_aligned
        else (
            "UI filters were not changed in no-submit replay. The Seller Portal route may not align exactly with "
            f"is_answered={config.is_answered}."
        ),
        "url": url,
    }


def collect_feedback_rows_with_scroll(page: Page, *, max_rows: int, date_from: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    collected: dict[str, dict[str, Any]] = {}
    attempts = 0
    idle_attempts = 0
    oldest_date = ""
    stop_reason = ""
    max_visible_read = min(max(max_rows, 10), 120)
    for attempts in range(1, MAX_SCROLL_ATTEMPTS + 1):
        visible_rows = extract_visible_feedback_rows(page, max_rows=max_visible_read)
        new_count = 0
        for row in visible_rows:
            key = ui_row_identity(row)
            if not key or key in collected:
                continue
            enriched = dict(row)
            enriched["ui_collection_index"] = len(collected)
            collected[key] = enriched
            new_count += 1
        oldest_date = min(
            [normalize_date_key(row.get("review_date") or row.get("review_datetime")) for row in collected.values() if normalize_date_key(row.get("review_date") or row.get("review_datetime"))],
            default="",
        )
        if len(collected) >= max_rows:
            stop_reason = "max_ui_rows_reached"
            break
        if oldest_date and oldest_date < date_from:
            stop_reason = "oldest_ui_date_before_date_from"
            break
        idle_attempts = idle_attempts + 1 if new_count == 0 else 0
        if idle_attempts >= 3:
            stop_reason = "no_new_rows_after_scroll"
            break
        scroll_result = scroll_feedback_list(page)
        _wait_settle(page, 900)
        if not scroll_result.get("changed"):
            idle_attempts += 1
    rows = list(collected.values())[:max_rows]
    for index, row in enumerate(rows):
        row["row_index"] = index
    return rows, {
        "scroll_attempts": attempts,
        "max_scroll_attempts": MAX_SCROLL_ATTEMPTS,
        "stop_reason": stop_reason or "max_scroll_attempts_reached",
        "oldest_ui_date": oldest_date,
        "date_from_stop_threshold": date_from,
        "collected_unique_rows": len(rows),
    }


def collect_feedback_rows_from_seller_portal_network(
    page: Page,
    config: ReplayConfig,
    *,
    request_headers: Mapping[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Collect read-only Seller Portal rows via the same cursor endpoint used by the page."""

    stats: dict[str, Any] = {
        "enabled": True,
        "endpoint": SELLER_PORTAL_FEEDBACKS_ENDPOINT,
        "page_limit": SELLER_PORTAL_NETWORK_PAGE_LIMIT,
        "max_pages": MAX_NETWORK_PAGES,
        "pages_attempted": 0,
        "raw_rows_seen": 0,
        "rows_after_requested_filters": 0,
        "request_statuses": [],
        "captured_header_keys": sorted(request_headers.keys()),
        "cursor_pagination_available": False,
        "next_cursor_observed": False,
        "newest_seen_date": "",
        "oldest_seen_date": "",
        "oldest_collected_date": "",
        "newest_collected_date": "",
        "stop_reason": "",
        "feedback_id_available": False,
        "errors": [],
    }
    rows_by_id: dict[str, dict[str, Any]] = {}
    is_answered_values = seller_portal_is_answered_values(config.is_answered)
    stop_reason = ""

    for is_answered in is_answered_values:
        cursor = ""
        seen_cursors: set[str] = set()
        for page_index in range(1, MAX_NETWORK_PAGES + 1):
            if len(rows_by_id) >= config.max_ui_rows:
                stop_reason = "max_ui_rows_reached"
                break
            if cursor in seen_cursors and cursor:
                stop_reason = "repeated_cursor"
                break
            seen_cursors.add(cursor)
            stats["pages_attempted"] += 1
            response = fetch_seller_portal_feedbacks_page(
                page,
                cursor=cursor,
                is_answered=is_answered,
                limit=SELLER_PORTAL_NETWORK_PAGE_LIMIT,
                request_headers=request_headers,
            )
            stats["request_statuses"].append(
                {
                    "is_answered": is_answered,
                    "page_index": page_index,
                    "status": response.get("status"),
                    "ok": response.get("ok"),
                    "cursor_present": bool(cursor),
                }
            )
            if not response.get("ok"):
                stats["errors"].append(
                    {
                        "stage": "seller_portal_network_fetch",
                        "status": response.get("status"),
                        "message": safe_text(str(response.get("error") or response.get("text") or ""), 500),
                    }
                )
                stop_reason = "network_fetch_failed"
                break

            feedbacks, next_cursor = parse_seller_portal_feedbacks_payload(response.get("json"))
            stats["cursor_pagination_available"] = stats["cursor_pagination_available"] or bool(next_cursor)
            stats["next_cursor_observed"] = stats["next_cursor_observed"] or bool(next_cursor)
            stats["raw_rows_seen"] += len(feedbacks)
            if not feedbacks:
                stop_reason = "network_page_empty"
                break

            oldest_seen_this_page = ""
            for feedback in feedbacks:
                row = seller_portal_network_feedback_to_ui_row(feedback, is_answered=is_answered)
                row_date = normalize_date_key(row.get("review_datetime") or row.get("created_at"))
                if row_date:
                    oldest_seen_this_page = min([oldest_seen_this_page, row_date]) if oldest_seen_this_page else row_date
                    stats["newest_seen_date"] = max([stats["newest_seen_date"], row_date]) if stats["newest_seen_date"] else row_date
                    stats["oldest_seen_date"] = min([stats["oldest_seen_date"], row_date]) if stats["oldest_seen_date"] else row_date
                if not ui_row_matches_requested_filters(row, config):
                    continue
                key = ui_row_identity(row)
                if not key or key in rows_by_id:
                    continue
                row["ui_collection_index"] = len(rows_by_id)
                rows_by_id[key] = row
                stats["rows_after_requested_filters"] = len(rows_by_id)
                stats["feedback_id_available"] = stats["feedback_id_available"] or bool(row.get("feedback_id"))
                stats["newest_collected_date"] = (
                    max([stats["newest_collected_date"], row_date]) if stats["newest_collected_date"] and row_date else row_date
                ) or stats["newest_collected_date"]
                stats["oldest_collected_date"] = (
                    min([stats["oldest_collected_date"], row_date]) if stats["oldest_collected_date"] and row_date else row_date
                ) or stats["oldest_collected_date"]
                if len(rows_by_id) >= config.max_ui_rows:
                    stop_reason = "max_ui_rows_reached"
                    break

            if stop_reason == "max_ui_rows_reached":
                break
            if oldest_seen_this_page and oldest_seen_this_page < config.date_from:
                stop_reason = "oldest_network_date_before_date_from"
                break
            if not next_cursor:
                stop_reason = "no_next_cursor"
                break
            cursor = next_cursor
        if stop_reason in {"max_ui_rows_reached", "network_fetch_failed"}:
            break

    rows = list(rows_by_id.values())[: config.max_ui_rows]
    for index, row in enumerate(rows):
        row["row_index"] = index
    stats["rows_after_requested_filters"] = len(rows)
    stats["stop_reason"] = stop_reason or "network_collection_completed"
    return rows, stats


def fetch_seller_portal_feedbacks_page(
    page: Page,
    *,
    cursor: str,
    is_answered: bool,
    limit: int,
    request_headers: Mapping[str, str],
) -> dict[str, Any]:
    try:
        return page.evaluate(
            r"""
async ({endpoint, cursor, isAnswered, limit, requestHeaders}) => {
  const url = new URL(endpoint);
  url.searchParams.set('cursor', cursor || '');
  url.searchParams.set('isAnswered', String(Boolean(isAnswered)));
  url.searchParams.set('limit', String(limit));
  url.searchParams.set('searchText', '');
  url.searchParams.set('sortOrder', 'dateDesc');
  const result = {ok: false, status: 0, url: url.toString(), json: null, text: '', error: ''};
  try {
    const response = await fetch(url.toString(), {
      method: 'GET',
      credentials: 'include',
      headers: {
        'accept': 'application/json, text/plain, */*',
        ...requestHeaders
      }
    });
    result.ok = response.ok;
    result.status = response.status;
    const text = await response.text();
    result.text = text.slice(0, 500);
    try {
      result.json = JSON.parse(text);
    } catch (error) {
      result.error = String(error && error.message || error);
    }
  } catch (error) {
    result.error = String(error && error.message || error);
  }
  return result;
}
            """,
            {
                "endpoint": SELLER_PORTAL_FEEDBACKS_ENDPOINT,
                "cursor": cursor,
                "isAnswered": is_answered,
                "limit": max(1, min(int(limit), 100)),
                "requestHeaders": dict(request_headers),
            },
        )
    except PlaywrightError as exc:
        return {"ok": False, "status": 0, "json": None, "text": "", "error": safe_text(str(exc), 500)}


def capture_seller_portal_feedback_headers(request: Any, headers: dict[str, str]) -> None:
    try:
        url = str(request.url)
    except Exception:
        return
    if "reviews-ext-seller-portal/api/v2/feedbacks" not in url:
        return
    try:
        raw_headers = request.headers
    except Exception:
        return
    for key in ("authorizev3", "wb-seller-lk", "root-version", "content-type"):
        value = raw_headers.get(key)
        if value:
            headers[key] = str(value)


def parse_seller_portal_feedbacks_payload(payload: Any) -> tuple[list[dict[str, Any]], str]:
    if not isinstance(payload, dict):
        return [], ""
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data.get("data")
    if not isinstance(data, dict):
        return [], ""
    feedbacks = [item for item in data.get("feedbacks") or [] if isinstance(item, dict)]
    pages = data.get("pages") if isinstance(data.get("pages"), dict) else {}
    next_cursor = str(pages.get("next") or "") if pages else ""
    return feedbacks, next_cursor


def seller_portal_network_feedback_to_ui_row(feedback: Mapping[str, Any], *, is_answered: bool) -> dict[str, Any]:
    product = feedback.get("productInfo") if isinstance(feedback.get("productInfo"), dict) else {}
    info = feedback.get("feedbackInfo") if isinstance(feedback.get("feedbackInfo"), dict) else {}
    complaints = feedback.get("supplierComplaints") if isinstance(feedback.get("supplierComplaints"), dict) else {}
    feedback_complaint = complaints.get("feedbackComplaint") if isinstance(complaints.get("feedbackComplaint"), dict) else {}
    created_at = iso_from_epoch_ms(feedback.get("createdDate"))
    review_datetime = ui_datetime_from_epoch_ms(feedback.get("createdDate"))
    review_date = review_datetime[:10] if review_datetime else normalize_date_key(created_at)
    feedback_id = str(feedback.get("id") or "").strip()
    text = str(info.get("feedbackText") or "").strip()
    pros = combine_reason_text(info.get("feedbackTextPros"), info.get("goodReasons"))
    cons = combine_reason_text(info.get("feedbackTextCons"), info.get("badReasons"))
    media_indicators = seller_portal_media_indicators(info)
    row_text = " ".join(part for part in (text, pros, cons) if part)
    normalized_review = normalize_text(row_text)
    return {
        "source": "seller_portal_network_cursor",
        "feedback_id": feedback_id,
        "seller_portal_feedback_id": feedback_id,
        "hidden_feedback_id": "",
        "created_at": created_at,
        "review_datetime": review_datetime,
        "review_date": review_date,
        "rating": normalize_rating(feedback.get("valuation")),
        "product_title": safe_text(str(product.get("name") or ""), 240),
        "supplier_article": safe_text(str(product.get("supplierArticle") or ""), 180),
        "vendor_article": safe_text(str(product.get("supplierArticle") or ""), 180),
        "wb_article": str(product.get("wbArticle") or ""),
        "nm_id": str(product.get("wbArticle") or ""),
        "text_snippet": safe_text(text, 700),
        "pros_snippet": safe_text(pros, 700),
        "cons_snippet": safe_text(cons, 700),
        "comment_snippet": "",
        "media_indicators": media_indicators,
        "photo_count": int(bool("photo" in media_indicators)),
        "video_count": int(bool("video" in media_indicators)),
        "is_answered": bool(is_answered or feedback.get("answer") or feedback.get("brandAnswer")),
        "answer_text": safe_text(str(feedback.get("answer") or feedback.get("brandAnswer") or ""), 500),
        "complaint_action_found": bool(feedback_complaint.get("isAvailable")),
        "complaint_status": str(feedback_complaint.get("status") or ""),
        "return_request_available": bool((feedback.get("returnProductOption") or {}).get("isAvailable"))
        if isinstance(feedback.get("returnProductOption"), dict)
        else False,
        "row_text_fingerprint": sha256(row_text.encode("utf-8")).hexdigest()[:20],
        "dom_fingerprint": sha256(f"{feedback_id}|{row_text}".encode("utf-8")).hexdigest()[:20],
        "normalized_review_text_fingerprint": sha256(normalized_review.encode("utf-8")).hexdigest()[:20],
        "selector_hints": ["seller_portal_network_cursor"],
        "three_dot_menu_found": bool(feedback_complaint.get("isAvailable")),
    }


def seller_portal_media_indicators(info: Mapping[str, Any]) -> list[str]:
    indicators: list[str] = []
    photos = info.get("photos")
    video = info.get("video")
    if photos:
        indicators.append("photo")
    if video:
        indicators.append("video")
    return indicators


def combine_reason_text(text_value: Any, reasons_value: Any) -> str:
    parts: list[str] = []
    if str(text_value or "").strip():
        parts.append(str(text_value).strip())
    if isinstance(reasons_value, list):
        parts.extend(str(item).strip() for item in reasons_value if str(item or "").strip())
    return " ".join(unique_preserve(parts))


def ui_row_matches_requested_filters(row: Mapping[str, Any], config: ReplayConfig) -> bool:
    row_date = normalize_date_key(row.get("review_datetime") or row.get("review_date") or row.get("created_at"))
    if row_date and (row_date < config.date_from or row_date > config.date_to):
        return False
    rating = normalize_rating(row.get("rating"))
    if rating and int(rating) not in set(config.stars):
        return False
    if config.is_answered in {"true", "false"}:
        expected = config.is_answered == "true"
        if bool(row.get("is_answered")) != expected:
            return False
    return True


def seller_portal_is_answered_values(value: str) -> list[bool]:
    if value == "true":
        return [True]
    if value == "false":
        return [False]
    return [False, True]


def build_targeted_search_stats(
    config: ReplayConfig,
    network_rows: list[dict[str, Any]],
    dom_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if config.targeted_search == "no":
        return {
            "requested": config.targeted_search,
            "used": False,
            "searches_attempted": 0,
            "limitation": "Targeted search was disabled by --targeted-search=no.",
        }
    if network_rows:
        return {
            "requested": config.targeted_search,
            "used": False,
            "searches_attempted": 0,
            "limitation": (
                "Targeted search was not needed because read-only Seller Portal cursor pagination collected "
                f"{len(network_rows)} requested rows."
            ),
        }
    return {
        "requested": config.targeted_search,
        "used": False,
        "searches_attempted": 0,
        "limitation": (
            "No stable read-only Seller Portal search input was exercised in this block; DOM fallback collected "
            f"{len(dom_rows)} rows."
        ),
    }


def scroll_feedback_list(page: Page) -> dict[str, Any]:
    try:
        return page.evaluate(
            r"""
() => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 240 && rect.height > 120;
  };
  const candidates = Array.from(document.querySelectorAll('[data-testid*="table"], [class*="Table"], [class*="table"], main, section, div'))
    .filter(visible)
    .filter((el) => el.scrollHeight > el.clientHeight + 40)
    .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
  const target = candidates[0] || document.scrollingElement || document.documentElement;
  const before = target.scrollTop;
  const delta = Math.max(320, Math.floor((target.clientHeight || window.innerHeight) * 0.85));
  target.scrollBy(0, delta);
  if (target.scrollTop === before && document.scrollingElement) {
    const docBefore = document.scrollingElement.scrollTop;
    window.scrollBy(0, Math.max(320, Math.floor(window.innerHeight * 0.85)));
    return {
      changed: document.scrollingElement.scrollTop !== docBefore,
      target: 'window',
      before: Math.round(docBefore),
      after: Math.round(document.scrollingElement.scrollTop)
    };
  }
  return {
    changed: target.scrollTop !== before,
    target: target === document.scrollingElement ? 'document' : String(target.className || target.tagName || '').slice(0, 120),
    before: Math.round(before),
    after: Math.round(target.scrollTop)
  };
}
            """
        )
    except PlaywrightError as exc:
        return {"changed": False, "error": safe_text(str(exc), 300)}


def ui_row_identity(row: Mapping[str, Any]) -> str:
    hidden = str(row.get("hidden_feedback_id") or row.get("feedback_id") or row.get("seller_portal_feedback_id") or "").strip()
    if hidden:
        return f"id:{hidden}"
    parts = [
        str(row.get("nm_id") or row.get("wb_article") or ""),
        str(row.get("supplier_article") or ""),
        normalize_datetime_minute(row.get("review_datetime") or row.get("review_date")),
        str(row.get("rating") or ""),
        str(row.get("row_text_fingerprint") or row.get("dom_fingerprint") or ""),
    ]
    key = "|".join(parts)
    return sha256(key.encode("utf-8")).hexdigest()[:20]


def match_api_rows_to_ui(api_rows: list[dict[str, Any]], ui_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [match_one_api_row(api_row, ui_rows) for api_row in api_rows]


def match_one_api_row(api_row: Mapping[str, Any], ui_rows: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [score_candidate(api_row, ui_row) for ui_row in ui_rows]
    scored.sort(key=lambda item: item["score"], reverse=True)
    best = scored[0] if scored else empty_candidate_score()
    close_candidates = [
        item
        for item in scored
        if item["score"] >= 0.5 and best["score"] - item["score"] <= 0.08
    ]
    ambiguity_count = len(close_candidates)
    duplicate_penalty = ambiguity_count > 1
    if duplicate_penalty:
        best = {
            **best,
            "score": round(max(0.0, float(best["score"]) - 0.07), 3),
            "reasons": [*best.get("reasons", []), "duplicate candidate penalty"],
        }
    status = classify_match(best, ambiguity_count=ambiguity_count)
    not_found_reason = classify_not_found_reason(status, api_row, ui_rows, best)
    return {
        "api_feedback_id": str(api_row.get("feedback_id") or ""),
        "api_summary": summarize_api_row(api_row),
        "best_ui_candidate": summarize_ui_row(best.get("ui_row") or {}),
        "match_status": status,
        "match_score": best["score"],
        "matched_fields": best.get("matched_fields") or [],
        "missing_fields": best.get("missing_fields") or [],
        "mismatched_fields": best.get("mismatched_fields") or [],
        "ambiguity_count": ambiguity_count,
        "candidate_count": len(scored),
        "close_candidate_summaries": [summarize_ui_row(item.get("ui_row") or {}) for item in close_candidates[:5]],
        "text_similarity": best.get("text_similarity", 0.0),
        "text_containment": best.get("text_containment", 0.0),
        "not_found_reason": not_found_reason,
        "reason": "; ".join(best.get("reasons") or ["no UI candidates above minimum threshold"]),
        "safe_for_future_submit": status == "exact",
    }


def classify_not_found_reason(
    status: str,
    api_row: Mapping[str, Any],
    ui_rows: list[dict[str, Any]],
    best: Mapping[str, Any],
) -> str:
    if status != "not_found":
        return ""
    if not ui_rows:
        return "not_found_due_to_no_ui_coverage"
    reason = " ".join(str(item) for item in best.get("reasons") or [])
    if "short text penalty" in reason or "duplicate candidate penalty" in reason:
        return "not_found_due_to_short_text_or_duplicate"
    api_date = normalize_date_key(api_row.get("created_date") or api_row.get("created_at"))
    ui_dates = {
        normalize_date_key(row.get("review_date") or row.get("review_datetime") or row.get("created_at"))
        for row in ui_rows
    }
    if api_date and api_date not in ui_dates:
        return "not_found_due_to_no_ui_coverage"
    api_nm = normalize_nm_id(api_row.get("nm_id"))
    api_supplier = normalize_article(api_row.get("supplier_article"))
    ui_nm_ids = {normalize_nm_id(row.get("nm_id") or row.get("wb_article")) for row in ui_rows}
    ui_articles = {normalize_article(row.get("supplier_article") or row.get("vendor_article")) for row in ui_rows}
    if (api_nm and api_nm not in ui_nm_ids) and (api_supplier and api_supplier not in ui_articles):
        return "not_found_due_to_no_ui_coverage"
    return "not_found_due_to_mismatch"


def score_candidate(api_row: Mapping[str, Any], ui_row: Mapping[str, Any]) -> dict[str, Any]:
    api_id = str(api_row.get("feedback_id") or "").strip()
    ui_id = str(ui_row.get("hidden_feedback_id") or ui_row.get("feedback_id") or "").strip()
    if api_id and ui_id and api_id == ui_id:
        return {
            "score": 1.0,
            "status_hint": "exact",
            "ui_row": dict(ui_row),
            "matched_fields": ["feedback_id"],
            "missing_fields": [],
            "mismatched_fields": [],
            "reasons": ["feedback_id exact match"],
            "text_similarity": 1.0,
        }

    score = 0.0
    reasons: list[str] = []
    matched: list[str] = []
    missing: list[str] = []
    mismatched: list[str] = []

    api_text = normalize_text(api_review_text(api_row))
    ui_text = normalize_text(ui_review_text(ui_row))
    text_similarity = SequenceMatcher(None, api_text, ui_text).ratio() if api_text and ui_text else 0.0
    text_containment = token_containment(api_text, ui_text) if api_text and ui_text else 0.0
    if api_text and ui_text:
        if api_text == ui_text:
            score += 0.36
            matched.append("text_exact")
            reasons.append("text exact normalized match")
        elif text_containment >= 0.92:
            score += 0.34
            matched.append("text_contained")
            reasons.append("text token containment match")
        elif text_similarity >= 0.90 or text_containment >= 0.86:
            score += 0.32
            matched.append("text_high_similarity")
            reasons.append("text high similarity")
        elif text_similarity >= 0.72 or text_containment >= 0.68:
            score += 0.22
            matched.append("text_medium_similarity")
            reasons.append("text medium similarity")
        elif text_similarity >= 0.55 or text_containment >= 0.50:
            score += 0.10
            matched.append("text_weak_similarity")
            reasons.append("text weak similarity")
        else:
            mismatched.append("text")
    else:
        missing.append("text")
        score -= 0.12
        reasons.append("missing text penalty")

    api_rating = normalize_rating(api_row.get("product_valuation") or api_row.get("rating"))
    ui_rating = normalize_rating(ui_row.get("rating"))
    if api_rating and ui_rating and api_rating == ui_rating:
        score += 0.12
        matched.append("rating")
        reasons.append("same rating")
    elif api_rating and ui_rating:
        mismatched.append("rating")
    else:
        missing.append("rating")

    api_dt = normalize_datetime_minute(api_row.get("created_at") or api_row.get("created_datetime"))
    ui_dt = normalize_datetime_minute(ui_row.get("review_datetime") or ui_row.get("created_at"))
    api_date = normalize_date_key(api_row.get("created_date") or api_row.get("created_at"))
    ui_date = normalize_date_key(ui_row.get("review_date") or ui_row.get("review_datetime"))
    if api_dt and ui_dt and api_dt == ui_dt:
        score += 0.22
        matched.append("exact_datetime")
        reasons.append("same exact datetime minute")
    elif api_date and ui_date and api_date == ui_date:
        score += 0.12
        matched.append("same_date")
        reasons.append("same date")
    elif api_dt or ui_dt or api_date or ui_date:
        mismatched.append("datetime")
    else:
        missing.append("datetime")

    api_nm = normalize_nm_id(api_row.get("nm_id"))
    ui_nm = normalize_nm_id(ui_row.get("nm_id") or ui_row.get("wb_article"))
    api_supplier = normalize_article(api_row.get("supplier_article"))
    ui_supplier = normalize_article(ui_row.get("supplier_article") or ui_row.get("vendor_article"))
    if api_nm and ui_nm and api_nm == ui_nm:
        score += 0.22
        matched.append("nm_id")
        reasons.append("same nmId/WB article")
    elif api_supplier and ui_supplier and api_supplier == ui_supplier:
        score += 0.16
        matched.append("supplier_article")
        reasons.append("same supplier article")
    else:
        api_article_present = bool(api_nm or api_supplier)
        ui_article_present = bool(ui_nm or ui_supplier)
        if api_article_present and ui_article_present:
            mismatched.append("article")
        else:
            missing.append("article")

    product_overlap = token_overlap(
        normalize_text(api_row.get("product_name") or ""),
        normalize_text(ui_row.get("product_title") or ""),
    )
    if product_overlap >= 0.72:
        score += 0.06
        matched.append("product_title")
        reasons.append("product title high overlap")
    elif product_overlap >= 0.45:
        score += 0.03
        matched.append("product_title_partial")
        reasons.append("product title partial overlap")
    elif api_row.get("product_name") or ui_row.get("product_title"):
        mismatched.append("product_title")

    if media_matches(api_row, ui_row):
        score += 0.03
        matched.append("media_presence")
        reasons.append("media presence matches")

    if min(len(api_text), len(ui_text)) < 30:
        score -= 0.10
        reasons.append("short text penalty")

    score = max(0.0, min(0.99, round(score, 3)))
    return {
        "score": score,
        "status_hint": "",
        "ui_row": dict(ui_row),
        "matched_fields": unique_preserve(matched),
        "missing_fields": unique_preserve(missing),
        "mismatched_fields": unique_preserve(mismatched),
        "reasons": unique_preserve(reasons),
        "text_similarity": round(text_similarity, 3),
        "text_containment": round(text_containment, 3),
    }


def classify_match(best: Mapping[str, Any], *, ambiguity_count: int) -> str:
    if best.get("status_hint") == "exact":
        return "exact"
    score = float(best.get("score") or 0.0)
    matched = set(best.get("matched_fields") or [])
    has_text = bool({"text_exact", "text_contained", "text_high_similarity"} & matched)
    has_article = bool({"nm_id", "supplier_article"} & matched)
    has_exact_support = has_text and "exact_datetime" in matched and "rating" in matched and has_article
    if ambiguity_count > 1 and score >= 0.50:
        return "ambiguous"
    if score >= 0.86 and has_exact_support:
        return "exact"
    if score >= 0.70:
        return "high"
    if score >= 0.50:
        return "ambiguous"
    return "not_found"


def empty_candidate_score() -> dict[str, Any]:
    return {
        "score": 0.0,
        "status_hint": "",
        "ui_row": {},
        "matched_fields": [],
        "missing_fields": [],
        "mismatched_fields": [],
        "reasons": ["no UI rows collected"],
        "text_similarity": 0.0,
    }


def build_aggregate(matches: list[dict[str, Any]], api_rows: list[dict[str, Any]], ui_rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(str(match.get("match_status") or "not_found") for match in matches)
    not_found_reasons = Counter(
        str(match.get("not_found_reason") or "not_found_due_to_mismatch")
        for match in matches
        if match.get("match_status") == "not_found"
    )
    tested = len(api_rows)
    risk_count = sum(
        1
        for match in matches
        if "short text penalty" in str(match.get("reason") or "")
        or "duplicate candidate penalty" in str(match.get("reason") or "")
    )
    mismatch_reasons = Counter()
    for match in matches:
        for field in match.get("missing_fields") or []:
            mismatch_reasons[f"missing:{field}"] += 1
        for field in match.get("mismatched_fields") or []:
            mismatch_reasons[f"mismatch:{field}"] += 1
        if match.get("match_status") in {"ambiguous", "not_found"}:
            for reason in str(match.get("reason") or "").split("; "):
                if reason:
                    mismatch_reasons[reason] += 1
    return {
        "api_rows_tested": tested,
        "ui_rows_collected": len(ui_rows),
        "ui_coverage_ratio": rate(min(len(ui_rows), tested), tested),
        "exact_count": counts.get("exact", 0),
        "high_count": counts.get("high", 0),
        "ambiguous_count": counts.get("ambiguous", 0),
        "not_found_count": counts.get("not_found", 0),
        "exact_rate": rate(counts.get("exact", 0), tested),
        "high_rate": rate(counts.get("high", 0), tested),
        "ambiguous_rate": rate(counts.get("ambiguous", 0), tested),
        "not_found_rate": rate(counts.get("not_found", 0), tested),
        "not_found_reason_split": {
            "not_found_due_to_no_ui_coverage": not_found_reasons.get("not_found_due_to_no_ui_coverage", 0),
            "not_found_due_to_mismatch": not_found_reasons.get("not_found_due_to_mismatch", 0),
            "not_found_due_to_short_text_or_duplicate": not_found_reasons.get(
                "not_found_due_to_short_text_or_duplicate", 0
            ),
        },
        "duplicate_or_short_text_risk_count": risk_count,
        "top_mismatch_reasons": [
            {"reason": reason, "count": count}
            for reason, count in mismatch_reasons.most_common(10)
        ],
    }


def build_recommendation(aggregate: Mapping[str, Any], report: Mapping[str, Any]) -> dict[str, Any]:
    tested = int(aggregate.get("api_rows_tested") or 0)
    exact = int(aggregate.get("exact_count") or 0)
    high = int(aggregate.get("high_count") or 0)
    ambiguous = int(aggregate.get("ambiguous_count") or 0)
    not_found = int(aggregate.get("not_found_count") or 0)
    risk_count = int(aggregate.get("duplicate_or_short_text_risk_count") or 0)
    ready = bool(tested and exact == tested and risk_count == 0)
    exact_only_feasibility = "feasible_for_exact_matches" if exact > 0 else "not_proven"
    required: list[str] = []
    ui = report.get("ui", {}) if isinstance(report.get("ui"), dict) else {}
    if ui.get("seller_portal_network_feedback_id_available"):
        required.append(
            "Seller Portal cursor endpoint exposes feedback_id for read-only matching; DOM hidden feedback_id is still not observed."
        )
    elif not ui.get("hidden_feedback_id_available"):
        required.append("No hidden UI feedback_id observed; keep exact-only matching on text+datetime+rating+article/nmId.")
    if float(aggregate.get("ui_coverage_ratio") or 0.0) < 1.0:
        required.append("UI coverage is lower than API rows tested; keep improving date/star alignment or cursor pagination.")
    if high:
        required.append("High matches need operator confirmation because one strong field is missing.")
    if ambiguous:
        required.append("Ambiguous matches must block submit; reduce short-text collisions with stronger UI fields or filters.")
    if not_found:
        required.append("Not-found rows need better UI filter alignment, longer scrolling or date/star filter selectors.")
    if risk_count:
        required.append("Short or duplicate text risk is present; do not allow autonomous submit for those rows.")
    return {
        "readiness_for_controlled_submit": "ready" if ready else "not_ready",
        "future_auto_submit_policy": "exact_only",
        "exact_only_feasibility": exact_only_feasibility,
        "safe_for_future_submit_count": exact,
        "operator_confirmation_required_count": high,
        "blocked_count": ambiguous + not_found,
        "required_improvements": required or ["No additional improvements required in this bounded sample."],
    }


def no_submit_guards() -> dict[str, Any]:
    return {
        "mode": NO_SUBMIT_MODE,
        "seller_portal_write_actions_allowed": SELLER_PORTAL_WRITE_ACTIONS_ALLOWED,
        "complaint_submit_clicked": False,
        "complaint_modal_opened": False,
        "answer_edit_clicked": False,
        "complaint_submit_path_called": False,
        "persistent_account_changes_allowed": False,
    }


def empty_aggregate() -> dict[str, Any]:
    return {
        "api_rows_tested": 0,
        "ui_rows_collected": 0,
        "ui_coverage_ratio": 0.0,
        "exact_count": 0,
        "high_count": 0,
        "ambiguous_count": 0,
        "not_found_count": 0,
        "exact_rate": 0.0,
        "high_rate": 0.0,
        "ambiguous_rate": 0.0,
        "not_found_rate": 0.0,
        "not_found_reason_split": {
            "not_found_due_to_no_ui_coverage": 0,
            "not_found_due_to_mismatch": 0,
            "not_found_due_to_short_text_or_duplicate": 0,
        },
        "duplicate_or_short_text_risk_count": 0,
        "top_mismatch_reasons": [],
    }


def render_markdown_report(report: Mapping[str, Any]) -> str:
    params = report.get("parameters") or {}
    api = report.get("api") or {}
    ui = report.get("ui") or {}
    agg = report.get("aggregate") or {}
    rec = report.get("recommendation") or {}
    lines = [
        "# Seller Portal Feedback Matching Replay",
        "",
        f"- Mode: `{report.get('mode')}`",
        f"- Started: `{report.get('started_at')}`",
        f"- Finished: `{report.get('finished_at')}`",
        f"- Requested range: `{params.get('date_from')}`..`{params.get('date_to')}`",
        f"- Stars: `{','.join(str(item) for item in params.get('stars') or [])}`",
        f"- is_answered: `{params.get('is_answered')}`",
        f"- API rows loaded: `{api.get('row_count', 0)}` / total `{api.get('total_available_rows', 0)}`",
        f"- UI rows collected: `{ui.get('rows_collected', 0)}`",
        f"- UI collection strategy: `{ui.get('collection_strategy')}`",
        f"- UI coverage ratio: `{agg.get('ui_coverage_ratio', 0.0)}`",
        f"- DOM rows / Seller Portal cursor rows: `{ui.get('dom_rows_collected', 0)}` / `{ui.get('seller_portal_network_rows_collected', 0)}`",
        f"- UI filters applied: `{(ui.get('filters') or {}).get('applied')}`",
        f"- Hidden UI feedback_id available: `{ui.get('hidden_feedback_id_available')}`",
        f"- Seller Portal network feedback_id available: `{ui.get('seller_portal_network_feedback_id_available')}`",
        f"- Exact/high/ambiguous/not_found: `{agg.get('exact_count', 0)}` / `{agg.get('high_count', 0)}` / `{agg.get('ambiguous_count', 0)}` / `{agg.get('not_found_count', 0)}`",
        f"- Exact rate: `{agg.get('exact_rate', 0.0)}`",
        f"- Not-found reason split: `{agg.get('not_found_reason_split')}`",
        f"- Duplicate/short-text risk count: `{agg.get('duplicate_or_short_text_risk_count', 0)}`",
        f"- Controlled submit readiness: `{rec.get('readiness_for_controlled_submit')}`",
        f"- Future policy: `{rec.get('future_auto_submit_policy')}`",
        "",
        "## Match Rows",
        "",
    ]
    for match in report.get("matches") or []:
        api_summary = match.get("api_summary") or {}
        ui_summary = match.get("best_ui_candidate") or {}
        lines.extend(
            [
                f"- `{match.get('match_status')}` score `{match.get('match_score')}` safe `{match.get('safe_for_future_submit')}` feedback `{match.get('api_feedback_id')}`",
                f"  API: `{api_summary.get('created_at')}` rating `{api_summary.get('rating')}` nm `{api_summary.get('nm_id')}` article `{api_summary.get('supplier_article')}` text `{api_summary.get('review_text')}`",
                f"  UI: `{ui_summary.get('review_datetime')}` rating `{ui_summary.get('rating')}` nm `{ui_summary.get('nm_id')}` article `{ui_summary.get('supplier_article')}` text `{ui_summary.get('review_text')}`",
                f"  Not-found reason: `{match.get('not_found_reason')}`",
                f"  Reason: {match.get('reason')}",
            ]
        )
    if report.get("errors"):
        lines.extend(["", "## Errors", ""])
        for error in report["errors"]:
            lines.append(f"- `{error.get('stage')}` / `{error.get('code')}`: {error.get('message')}")
    if rec.get("required_improvements"):
        lines.extend(["", "## Required Improvements", ""])
        for item in rec.get("required_improvements") or []:
            lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def write_report_artifacts(report: dict[str, Any], output_root: Path) -> dict[str, Path]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    json_path = run_dir / "seller_portal_feedbacks_matching_replay.json"
    md_path = run_dir / "seller_portal_feedbacks_matching_replay.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown_report(report), encoding="utf-8")
    return {"run_dir": run_dir, "json": json_path, "markdown": md_path}


def compact_stdout_report(report: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "contract_name": report.get("contract_name"),
        "mode": report.get("mode"),
        "parameters": report.get("parameters"),
        "read_only_guards": report.get("read_only_guards"),
        "session": report.get("session"),
        "navigation": report.get("navigation"),
        "api": {
            key: (report.get("api") or {}).get(key)
            for key in ("success", "row_count", "total_available_rows", "limited", "feedback_id_available", "blocker")
        },
        "ui": {
            key: (report.get("ui") or {}).get(key)
            for key in (
                "success",
                "rows_collected",
                "dom_rows_collected",
                "seller_portal_network_rows_collected",
                "collection_strategy",
                "filters",
                "scroll_stats",
                "seller_portal_network_stats",
                "targeted_search_stats",
                "field_availability",
                "hidden_feedback_id_available",
                "seller_portal_network_feedback_id_available",
                "blocker",
            )
        },
        "aggregate": report.get("aggregate"),
        "recommendation": report.get("recommendation"),
        "errors": report.get("errors"),
        "artifact_paths": report.get("artifact_paths"),
    }


def summarize_api_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "feedback_id": str(row.get("feedback_id") or ""),
        "created_at": str(row.get("created_at") or ""),
        "created_date": str(row.get("created_date") or ""),
        "rating": normalize_rating(row.get("product_valuation") or row.get("rating")),
        "nm_id": str(row.get("nm_id") or ""),
        "supplier_article": safe_text(str(row.get("supplier_article") or ""), 120),
        "product_name": safe_text(str(row.get("product_name") or ""), 160),
        "review_text": safe_text(api_review_text(row), 220),
        "is_answered": bool(row.get("is_answered")),
        "photo_count": int(row.get("photo_count") or 0),
        "video_count": int(row.get("video_count") or 0),
    }


def summarize_ui_row(row: Mapping[str, Any]) -> dict[str, Any]:
    if not row:
        return {}
    return {
        "row_index": row.get("row_index", row.get("ui_collection_index")),
        "source": str(row.get("source") or ""),
        "feedback_id": str(row.get("feedback_id") or row.get("seller_portal_feedback_id") or ""),
        "review_datetime": str(row.get("review_datetime") or ""),
        "review_date": str(row.get("review_date") or ""),
        "rating": normalize_rating(row.get("rating")),
        "nm_id": str(row.get("nm_id") or row.get("wb_article") or ""),
        "supplier_article": safe_text(str(row.get("supplier_article") or ""), 120),
        "product_title": safe_text(str(row.get("product_title") or ""), 160),
        "review_text": safe_text(ui_review_text(row), 220),
        "media_indicators": [str(item) for item in row.get("media_indicators") or []],
        "hidden_feedback_id": str(row.get("hidden_feedback_id") or ""),
        "row_text_fingerprint": str(row.get("row_text_fingerprint") or ""),
        "normalized_review_text_fingerprint": str(row.get("normalized_review_text_fingerprint") or ""),
        "menu_action_available": bool(row.get("complaint_action_found") or row.get("three_dot_menu_found")),
    }


def api_review_text(row: Mapping[str, Any]) -> str:
    return " ".join(
        safe_text(str(row.get(key) or ""), 500)
        for key in ("text", "pros", "cons")
        if str(row.get(key) or "").strip()
    )


def ui_review_text(row: Mapping[str, Any]) -> str:
    return " ".join(
        safe_text(str(row.get(key) or ""), 500)
        for key in ("text_snippet", "pros_snippet", "cons_snippet", "comment_snippet")
        if str(row.get(key) or "").strip()
    )


def normalize_text(value: Any) -> str:
    text = str(value or "").replace("ё", "е").replace("Ё", "е").lower()
    text = REPEATED_PUNCT_RE.sub(r"\1", text)
    text = re.sub(r"[^\w\sа-яА-Я-]+", " ", text, flags=re.UNICODE)
    text = text.replace("_", " ")
    return TEXT_WS_RE.sub(" ", text).strip()


def normalize_article(value: Any) -> str:
    text = normalize_text(value)
    text = re.sub(r"\b(?:арт|артикул|wb|вб|nm|nmid|id)\b", " ", text)
    return TEXT_WS_RE.sub(" ", text).strip()


def normalize_nm_id(value: Any) -> str:
    match = ARTICLE_DIGITS_RE.search(str(value or ""))
    return match.group(0) if match else ""


def normalize_rating(value: Any) -> str:
    try:
        rating = int(str(value or "").strip())
    except ValueError:
        match = re.search(r"\b([1-5])\b", str(value or ""))
        rating = int(match.group(1)) if match else 0
    return str(rating) if 1 <= rating <= 5 else ""


def normalize_datetime_minute(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    ui_match = UI_DATE_TIME_RE.search(raw)
    if ui_match:
        day, month, year, hour, minute = ui_match.groups()
        full_year = int(year) + 2000 if int(year) < 100 else int(year)
        return f"{full_year:04d}-{int(month):02d}-{int(day):02d} {int(hour):02d}:{minute}"
    parsed = parse_iso_datetime(raw)
    if parsed is None:
        return ""
    business_dt = parsed.astimezone(ZoneInfo(BUSINESS_TZ)) if parsed.tzinfo else parsed
    return business_dt.strftime("%Y-%m-%d %H:%M")


def normalize_date_key(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    minute = normalize_datetime_minute(raw)
    if minute:
        return minute[:10]
    iso_match = ISO_DATE_RE.search(raw)
    if iso_match:
        year, month, day = iso_match.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    ui_match = UI_DATE_RE.search(raw)
    if ui_match:
        day, month, year = ui_match.groups()
        full_year = int(year) + 2000 if int(year) < 100 else int(year)
        return f"{full_year:04d}-{int(month):02d}-{int(day):02d}"
    return raw[:10]


def parse_iso_datetime(value: str) -> datetime | None:
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None and "T" in value:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def iso_from_epoch_ms(value: Any) -> str:
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(millis / 1000, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ui_datetime_from_epoch_ms(value: Any) -> str:
    iso_value = iso_from_epoch_ms(value)
    parsed = parse_iso_datetime(iso_value) if iso_value else None
    if parsed is None:
        return ""
    business_dt = parsed.astimezone(ZoneInfo(BUSINESS_TZ))
    return business_dt.strftime("%d.%m.%Y в %H:%M")


def token_overlap(left: str, right: str) -> float:
    left_tokens = {token for token in left.split() if len(token) > 2}
    right_tokens = {token for token in right.split() if len(token) > 2}
    if not left_tokens or not right_tokens:
        return 0.0
    return round(len(left_tokens & right_tokens) / max(len(left_tokens), len(right_tokens)), 3)


def token_containment(left: str, right: str) -> float:
    left_tokens = {token for token in left.split() if len(token) > 1}
    right_tokens = {token for token in right.split() if len(token) > 1}
    if not left_tokens or not right_tokens:
        return 0.0
    return round(len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens)), 3)


def media_matches(api_row: Mapping[str, Any], ui_row: Mapping[str, Any]) -> bool:
    api_photo = int(api_row.get("photo_count") or 0) > 0
    api_video = int(api_row.get("video_count") or 0) > 0
    indicators = {str(item).lower() for item in ui_row.get("media_indicators") or []}
    ui_photo = "photo" in indicators or "фото" in indicators
    ui_video = "video" in indicators or "видео" in indicators
    return (api_photo == ui_photo and api_video == ui_video) and (api_photo or api_video)


def availability(rows: list[Mapping[str, Any]], fields: Iterable[str]) -> dict[str, bool]:
    return {field: any(bool(row.get(field)) for row in rows) for field in fields}


def rate(count: int, total: int) -> float:
    return round(count / total, 3) if total else 0.0


def safe_text(value: str, limit: int) -> str:
    normalized = TEXT_WS_RE.sub(" ", str(value or "")).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "…"


def unique_preserve(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_requested_date(value: str) -> str:
    date_key = normalize_date_key(value)
    try:
        datetime.fromisoformat(date_key)
    except ValueError as exc:
        raise ValueError("date-from/date-to must use YYYY-MM-DD") from exc
    return date_key


API_MATCH_FIELDS = (
    "feedback_id",
    "created_at",
    "created_date",
    "product_valuation",
    "text",
    "pros",
    "cons",
    "nm_id",
    "product_name",
    "supplier_article",
    "is_answered",
    "answer_text",
    "photo_count",
    "video_count",
)


if __name__ == "__main__":
    main()
