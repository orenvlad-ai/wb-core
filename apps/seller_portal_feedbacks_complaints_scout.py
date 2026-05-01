"""Read-only Seller Portal scout for feedback complaint workflow feasibility.

The runner intentionally does not submit complaints, edit answers or persist
operator decisions. It reuses the existing wb-web-bot storage_state contour and
only records bounded, sanitized UI observations for future implementation.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from hashlib import sha256
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import (  # noqa: E402
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from apps.seller_portal_relogin_session import (  # noqa: E402
    DEFAULT_STORAGE_STATE_PATH,
    DEFAULT_WB_BOT_PYTHON,
    probe_storage_state,
)


DEFAULT_START_URL = "https://seller.wildberries.ru"
DEFAULT_OUTPUT_ROOT = Path("/opt/wb-core-runtime/state/feedbacks_complaints_scout")
LOCAL_OUTPUT_ROOT = Path("artifacts/seller_portal_feedbacks_complaints_scout")
BUSINESS_TZ = "Asia/Yekaterinburg"
SAFE_TEXT_LIMIT = 240
EXPECTED_COMPLAINT_CATEGORIES = (
    "Отзыв оставили конкуренты",
    "Другое",
    "Отзыв не относится к товару",
    "Спам-реклама в тексте",
    "Нецензурная лексика",
    "Отзыв с политическим контекстом",
    "Угрозы, оскорбления",
    "Фото или видео не имеет отношения к товару",
    "Нецензурное содержимое в фото или видео",
    "Спам-реклама на фото или видео",
)
SUBMIT_LIKE_RE = re.compile(
    r"(^|\b)(отправить|подать|сохранить|submit|send|save)(\b|$)"
    r"|создать\s+жалобу|отправить\s+жалобу|подать\s+жалобу|^пожаловаться$",
    re.IGNORECASE,
)
TEXT_WS_RE = re.compile(r"\s+")
DATE_RE = re.compile(
    r"\b(\d{1,2}[./]\d{1,2}[./]\d{2,4}|\d{1,2}\s+"
    r"(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)"
    r"(?:\s+\d{4})?)\b",
    re.IGNORECASE,
)
DATE_TIME_RE = re.compile(
    r"\b(\d{1,2}[./]\d{1,2}[./]\d{2,4}\s+(?:в\s+)?\d{1,2}:\d{2})\b",
    re.IGNORECASE,
)
ARTICLE_RE = re.compile(
    r"(?:артикул\s*(?:wb|вб|продавца)?|nm\s*id|nmid)\D{0,12}(\d{5,})",
    re.IGNORECASE,
)
RATING_RE = re.compile(r"\b([1-5])\s*(?:звезд|звезды|звезда|★|/ ?5)\b", re.IGNORECASE)
FEEDBACK_ID_RE = re.compile(r"(?:feedback|review|comment)[_-]?(?:id)?[=:_/ -]*([a-z0-9-]{8,})", re.IGNORECASE)
ROW_MENU_COMPLAINT_LABEL = "Пожаловаться на отзыв"
ROW_MENU_RETURN_LABEL = "Запросить возврат"
ROW_MENU_EXPECTED_LABELS = (ROW_MENU_RETURN_LABEL, ROW_MENU_COMPLAINT_LABEL)


@dataclass(frozen=True)
class ScoutConfig:
    mode: str
    storage_state_path: Path
    wb_bot_python: Path
    output_root: Path
    start_url: str
    max_feedback_rows: int
    max_complaint_rows: int
    max_modal_reviews: int
    open_complaint_modal: bool
    headless: bool
    timeout_ms: int
    write_artifacts: bool


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=(
            "check-session",
            "scout-feedbacks",
            "scout-complaints",
            "scout-categories",
            "full-scout",
        ),
        nargs="?",
        default="full-scout",
    )
    parser.add_argument("--storage-state-path", default=str(DEFAULT_STORAGE_STATE_PATH))
    parser.add_argument("--wb-bot-python", default=str(DEFAULT_WB_BOT_PYTHON))
    parser.add_argument("--output-root", default="")
    parser.add_argument("--start-url", default=DEFAULT_START_URL)
    parser.add_argument("--max-feedback-rows", type=int, default=5)
    parser.add_argument("--max-complaint-rows", type=int, default=20)
    parser.add_argument("--max-modal-reviews", type=int, default=3)
    parser.add_argument(
        "--open-complaint-modal",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Open complaint modal and read categories, then close it. Never submits.",
    )
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--timeout-ms", type=int, default=20000)
    parser.add_argument("--no-artifacts", action="store_true")
    args = parser.parse_args()

    output_root = (
        Path(args.output_root)
        if args.output_root
        else (DEFAULT_OUTPUT_ROOT if Path("/opt/wb-core-runtime/state").exists() else LOCAL_OUTPUT_ROOT)
    )
    config = ScoutConfig(
        mode=args.mode,
        storage_state_path=Path(args.storage_state_path).expanduser(),
        wb_bot_python=Path(args.wb_bot_python).expanduser(),
        output_root=output_root,
        start_url=str(args.start_url).rstrip("/") or DEFAULT_START_URL,
        max_feedback_rows=max(1, int(args.max_feedback_rows)),
        max_complaint_rows=max(1, int(args.max_complaint_rows)),
        max_modal_reviews=max(0, int(args.max_modal_reviews)),
        open_complaint_modal=bool(args.open_complaint_modal),
        headless=not args.headed,
        timeout_ms=max(5000, int(args.timeout_ms)),
        write_artifacts=not bool(args.no_artifacts),
    )
    report = run_scout(config)
    if config.write_artifacts:
        paths = write_report_artifacts(report, config.output_root)
        report["artifact_paths"] = {key: str(path) for key, path in paths.items()}
    print(json.dumps(_compact_stdout_report(report), ensure_ascii=False, indent=2))


def run_scout(config: ScoutConfig) -> dict[str, Any]:
    started_at = _iso_now()
    session = check_session(config)
    report: dict[str, Any] = {
        "contract_name": "seller_portal_feedbacks_complaints_scout",
        "contract_version": "read_only_v1",
        "mode": config.mode,
        "started_at": started_at,
        "finished_at": None,
        "read_only_guards": {
            "complaint_submit_clicked": False,
            "answer_edit_clicked": False,
            "seller_portal_write_actions_allowed": False,
            "open_complaint_modal_requested": config.open_complaint_modal,
        },
        "session": session,
        "navigation": {},
        "feedbacks": _empty_feedbacks_report(),
        "complaint_modal": _empty_modal_report(),
        "my_complaints": _empty_my_complaints_report(),
        "matching_feasibility": {},
        "errors": [],
    }
    if config.mode == "check-session":
        report["finished_at"] = _iso_now()
        report["matching_feasibility"] = build_matching_feasibility(report)
        return report
    if not session.get("ok"):
        report["errors"].append(
            {
                "stage": "session",
                "code": str(session.get("status") or "session_invalid"),
                "message": str(session.get("message") or "Seller Portal session is not valid"),
            }
        )
        report["finished_at"] = _iso_now()
        report["matching_feasibility"] = build_matching_feasibility(report)
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
            try:
                report["navigation"] = navigate_to_feedbacks_questions(page, config)
                if config.mode in {"scout-feedbacks", "scout-categories", "full-scout"}:
                    report["feedbacks"] = scout_feedbacks_tab(page, config)
                if config.mode in {"scout-categories", "full-scout"}:
                    report["complaint_modal"] = scout_complaint_categories(page, config, report["feedbacks"])
                if config.mode in {"scout-complaints", "full-scout"}:
                    report["my_complaints"] = scout_my_complaints(page, config)
            finally:
                context.close()
                browser.close()
    except Exception as exc:  # pragma: no cover - live scout fallback
        report["errors"].append(
            {
                "stage": "browser_scout",
                "code": exc.__class__.__name__,
                "message": _safe_text(str(exc), 800),
            }
        )

    report["matching_feasibility"] = build_matching_feasibility(report)
    report["finished_at"] = _iso_now()
    return report


def check_session(config: ScoutConfig) -> dict[str, Any]:
    payload = probe_storage_state(config.storage_state_path, wb_bot_python=config.wb_bot_python)
    supplier_context = payload.get("supplier_context") if isinstance(payload.get("supplier_context"), dict) else {}
    canonical_supplier_id = str(os.environ.get("SELLER_PORTAL_CANONICAL_SUPPLIER_ID", "") or "").strip()
    canonical_supplier_label = str(os.environ.get("SELLER_PORTAL_CANONICAL_SUPPLIER_LABEL", "") or "").strip()
    unique_supplier_ids = {
        str(value or "").strip()
        for value in (
            supplier_context.get("current_supplier_id"),
            supplier_context.get("current_supplier_external_id"),
            supplier_context.get("analytics_supplier_id"),
        )
        if str(value or "").strip()
    }
    wrong_supplier = bool(canonical_supplier_id and unique_supplier_ids and unique_supplier_ids != {canonical_supplier_id})
    return {
        "ok": bool(payload.get("ok")) and not wrong_supplier,
        "status": "session_valid_wrong_org" if wrong_supplier else str(payload.get("status") or ""),
        "message": _safe_text(str(payload.get("message") or ""), 500),
        "returncode": payload.get("returncode"),
        "canonical_supplier_configured": bool(canonical_supplier_id),
        "canonical_supplier_label": canonical_supplier_label,
        "supplier_context": _safe_mapping(supplier_context),
        "storage_state_path": str(config.storage_state_path),
        "wb_bot_python": str(config.wb_bot_python),
    }


def navigate_to_feedbacks_questions(page: Page, config: ScoutConfig) -> dict[str, Any]:
    result: dict[str, Any] = {
        "start_url": config.start_url,
        "final_url": "",
        "method": "",
        "menu_path": [],
        "direct_url_candidates": [],
        "discovered_links": [],
        "selectors": [],
        "success": False,
        "blocker": "",
    }
    page.goto(config.start_url, wait_until="domcontentloaded")
    _wait_settle(page, 4000)
    _wait_for_text_visible(page, "Товары и цены", timeout_ms=8000)
    if _looks_like_login(page):
        result["blocker"] = "seller portal redirected to login/auth page"
        result["final_url"] = page.url
        return result

    for menu_label in ("Товары и цены", "Коммуникации"):
        try:
            clicked = _hover_or_click_text(page, menu_label)
            if clicked:
                result["menu_path"].append(menu_label)
                result["selectors"].append(f"text={menu_label}")
                _wait_settle(page, 1000)
        except PlaywrightError:
            pass
    try:
        feedbacks_link = _first_visible_text_locator(page, "Отзывы и вопросы")
        if feedbacks_link is not None:
            href = _safe_locator_attr(feedbacks_link, "href")
            feedbacks_link.click(timeout=5000)
            _wait_settle(page, 3000)
            result["menu_path"].append("Отзывы и вопросы")
            result["selectors"].append("text=Отзывы и вопросы")
            if href:
                result["discovered_links"].append({"text": "Отзывы и вопросы", "href": _safe_url(href)})
            if _is_feedbacks_questions_page(page):
                result.update({"success": True, "method": "menu", "final_url": page.url})
                return result
    except PlaywrightError as exc:
        result["blocker"] = f"menu navigation failed: {_safe_text(str(exc), 200)}"

    discovered = _discover_feedback_links(page)
    result["discovered_links"] = discovered[:10]
    for link in discovered:
        href = str(link.get("href") or "")
        if not href:
            continue
        try:
            page.goto(href, wait_until="domcontentloaded")
            _wait_settle(page, 3000)
            if _is_feedbacks_questions_page(page):
                result.update({"success": True, "method": "discovered_link", "final_url": page.url})
                return result
        except PlaywrightError:
            continue

    direct_candidates = [
        f"{config.start_url}/feedbacks/feedbacks-tab",
        f"{config.start_url}/feedbacks/questions-tab",
        f"{config.start_url}/feedbacks/complaints-tab",
        f"{config.start_url}/feedbacks-questions/feedbacks",
        f"{config.start_url}/feedbacks-questions",
        f"{config.start_url}/reviews-questions/feedbacks",
        f"{config.start_url}/reviews-questions",
        f"{config.start_url}/feedbacks",
        f"{config.start_url}/questions-and-reviews",
    ]
    for candidate in direct_candidates:
        result["direct_url_candidates"].append(candidate)
        try:
            page.goto(candidate, wait_until="domcontentloaded")
            _wait_settle(page, 3000)
            if _is_feedbacks_questions_page(page):
                result.update({"success": True, "method": "direct_candidate", "final_url": page.url})
                return result
        except PlaywrightError:
            continue

    result["final_url"] = page.url
    result["blocker"] = result["blocker"] or "Отзывы и вопросы page was not reached with menu/discovered/direct navigation"
    return result


def scout_feedbacks_tab(page: Page, config: ScoutConfig) -> dict[str, Any]:
    report = _empty_feedbacks_report()
    if not _click_tab_like(page, "Отзывы"):
        report["blocker"] = "Отзывы tab was not found"
        return report
    _wait_settle(page, 2500)
    _wait_for_feedback_rows(page, timeout_ms=10000)
    rows = extract_visible_feedback_rows(page, max_rows=config.max_feedback_rows)
    menu_diagnostics = scout_feedback_row_menus(
        page,
        rows,
        max_rows=min(len(rows), max(1, min(config.max_feedback_rows, max(config.max_modal_reviews, 3)))),
    )
    diagnostics_by_id = {
        str(item.get("dom_scout_id") or ""): item
        for item in menu_diagnostics
        if item.get("dom_scout_id")
    }
    for index, row in enumerate(rows):
        row["row_index"] = index
        diagnostic = diagnostics_by_id.get(str(row.get("dom_scout_id") or ""))
        if not diagnostic:
            continue
        row["row_menu_opened"] = bool(diagnostic.get("menu_opened"))
        row["row_menu_items"] = diagnostic.get("menu_items_found") or []
        row["complaint_action_found"] = bool(diagnostic.get("complaint_action_found"))
        row["three_dot_menu_found"] = bool(row.get("three_dot_menu_found") or diagnostic.get("menu_opened"))
    report["visible_rows_parsed_count"] = len(rows)
    report["rows"] = rows
    report["hidden_feedback_id_available"] = any(bool(row.get("hidden_feedback_id")) for row in rows)
    report["three_dot_menu_found"] = any(bool(row.get("three_dot_menu_found")) for row in rows)
    report["row_level_menu_found"] = any(bool(item.get("menu_opened")) for item in menu_diagnostics)
    report["complaint_action_found"] = any(bool(item.get("complaint_action_found")) for item in menu_diagnostics)
    report["row_menu_diagnostics"] = menu_diagnostics
    report["field_availability"] = field_availability(rows)
    report["selectors"] = sorted({selector for row in rows for selector in row.get("selector_hints", [])})[:30]
    report["success"] = bool(rows)
    if not rows:
        report["blocker"] = "No visible feedback rows were identified"
    return report


def scout_complaint_categories(page: Page, config: ScoutConfig, feedbacks_report: dict[str, Any]) -> dict[str, Any]:
    report = _empty_modal_report()
    report["open_requested"] = config.open_complaint_modal
    if not config.open_complaint_modal:
        report["blocker"] = "open_complaint_modal is false; category modal not opened by default"
        return report
    rows = feedbacks_report.get("rows") if isinstance(feedbacks_report.get("rows"), list) else []
    actionable_rows = [
        row
        for row in rows
        if row.get("complaint_action_found") or row.get("row_menu_opened") or row.get("three_dot_menu_found")
    ]
    fallback_rows = [row for row in rows if row not in actionable_rows]
    sample_rows = [row for row in [*actionable_rows, *fallback_rows] if row.get("dom_scout_id")]
    if not sample_rows:
        report["blocker"] = "No feedback row DOM ids available for complaint modal scout"
        return report

    samples: list[dict[str, Any]] = []
    for row in sample_rows[: config.max_modal_reviews]:
        sample = scout_one_complaint_modal(page, row)
        samples.append(sample)
        if sample.get("opened"):
            report["modal_opened_safely"] = True
        if sample.get("submit_button_seen"):
            report["submit_button_seen"] = True
        if sample.get("submit_clicked"):
            report["submit_button_not_clicked"] = False
    report["samples"] = samples
    categories_by_sample = [
        tuple(sample.get("categories") or [])
        for sample in samples
        if sample.get("categories")
    ]
    merged_categories: list[str] = []
    for categories in categories_by_sample:
        for category in categories:
            if category not in merged_categories:
                merged_categories.append(category)
    report["categories"] = merged_categories
    report["category_list_varies_by_review"] = (
        "unknown" if len(categories_by_sample) < 2 else len(set(categories_by_sample)) > 1
    )
    report["has_other_category"] = any(_norm_text(category) == _norm_text("Другое") for category in merged_categories)
    report["known_category_hits"] = [
        expected
        for expected in EXPECTED_COMPLAINT_CATEGORIES
        if any(_norm_text(expected) == _norm_text(category) for category in merged_categories)
    ]
    report["success"] = bool(report["modal_opened_safely"] and merged_categories)
    if not report["success"] and not report["blocker"]:
        report["blocker"] = "Complaint modal did not expose readable categories"
    return report


def scout_feedback_row_menus(page: Page, rows: list[dict[str, Any]], *, max_rows: int) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for index, row in enumerate(rows[:max_rows]):
        dom_id = str(row.get("dom_scout_id") or "")
        diagnostic: dict[str, Any] = {
            "row_index": index,
            "dom_scout_id": dom_id,
            "product_title": _safe_text(str(row.get("product_title") or ""), 160),
            "supplier_article": _safe_text(str(row.get("supplier_article") or ""), 120),
            "wb_article": _safe_text(str(row.get("wb_article") or row.get("nm_id") or ""), 80),
            "rating": str(row.get("rating") or ""),
            "review_date": str(row.get("review_date") or ""),
            "review_datetime": str(row.get("review_datetime") or ""),
            "review_snippet": _safe_text(str(row.get("text_snippet") or ""), 220),
            "menu_opened": False,
            "menu_items_found": [],
            "complaint_action_found": False,
            "row_menu_click": {},
            "blocker": "",
        }
        if not dom_id:
            diagnostic["blocker"] = "row DOM id unavailable"
            diagnostics.append(diagnostic)
            continue
        clicked_menu = _click_safe_row_menu(page, dom_id)
        diagnostic["row_menu_click"] = clicked_menu
        diagnostic["menu_opened"] = bool(clicked_menu.get("ok"))
        if not clicked_menu.get("ok"):
            diagnostic["blocker"] = str(clicked_menu.get("reason") or "safe row menu not found")
            diagnostics.append(diagnostic)
            continue
        _wait_settle(page, 500)
        menu_state = extract_open_row_menu_state(page)
        diagnostic["menu_items_found"] = menu_state.get("items") or []
        diagnostic["complaint_action_found"] = bool(menu_state.get("complaint_action_found"))
        if not diagnostic["menu_items_found"]:
            diagnostic["blocker"] = "row menu opened but no readable menu items were found"
        _safe_escape(page)
        _wait_settle(page, 250)
        diagnostics.append(diagnostic)
    return diagnostics


def scout_one_complaint_modal(page: Page, row: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(row, str):
        dom_id = row
        row_context: dict[str, Any] = {}
    else:
        row_context = row
        dom_id = str(row.get("dom_scout_id") or "")
    sample: dict[str, Any] = {
        "dom_scout_id": dom_id,
        "row_index": row_context.get("row_index"),
        "product_title": _safe_text(str(row_context.get("product_title") or ""), 160),
        "supplier_article": _safe_text(str(row_context.get("supplier_article") or ""), 120),
        "wb_article": _safe_text(str(row_context.get("wb_article") or row_context.get("nm_id") or ""), 80),
        "rating": str(row_context.get("rating") or ""),
        "review_date": str(row_context.get("review_date") or ""),
        "review_datetime": str(row_context.get("review_datetime") or ""),
        "review_snippet": _safe_text(str(row_context.get("text_snippet") or ""), 220),
        "menu_opened": False,
        "complaint_action_found": False,
        "opened": False,
        "categories": [],
        "menu_labels": [],
        "description_fields": [],
        "validation_hints": [],
        "submit_button_seen": False,
        "submit_clicked": False,
        "close_method": "",
        "durable_success_state_seen": False,
        "durable_success_state_after_close": False,
        "blocker": "",
    }
    clicked_menu = _click_safe_row_menu(page, dom_id)
    sample["row_menu_click"] = clicked_menu
    sample["menu_opened"] = bool(clicked_menu.get("ok"))
    if not clicked_menu.get("ok"):
        sample["blocker"] = str(clicked_menu.get("reason") or "safe row menu not found")
        return sample
    _wait_settle(page, 800)
    menu_state = extract_open_row_menu_state(page)
    sample["menu_labels"] = menu_state.get("items") or []
    sample["complaint_action_found"] = bool(menu_state.get("complaint_action_found"))
    if not sample["complaint_action_found"]:
        sample["blocker"] = "Пожаловаться на отзыв action not found in row menu"
        _safe_escape(page)
        return sample
    assert_safe_click_label(ROW_MENU_COMPLAINT_LABEL, purpose="open_complaint_modal")
    action_click = click_open_row_menu_complaint_action(page)
    sample["complaint_action_click"] = action_click
    if not action_click.get("ok"):
        sample["blocker"] = str(action_click.get("reason") or "Пожаловаться на отзыв action could not be clicked")
        _safe_escape(page)
        return sample
    _wait_settle(page, 1500)
    modal = extract_complaint_modal_state(page)
    sample.update(modal)
    sample["opened"] = bool(modal.get("opened"))
    success_state = detect_complaint_success_state(page)
    sample["durable_success_state_seen"] = bool(success_state.get("seen"))
    sample["submit_clicked"] = False
    if sample["durable_success_state_seen"] and not sample["opened"]:
        sample["blocker"] = (
            "complaint action appears to create durable success/submitted state without a readable modal: "
            + str(success_state.get("text") or "")
        )
        return sample
    close_method = close_modal_without_submit(page)
    sample["close_method"] = close_method
    _wait_settle(page, 600)
    sample["durable_success_state_after_close"] = bool(detect_complaint_success_state(page).get("seen"))
    return sample


def scout_my_complaints(page: Page, config: ScoutConfig) -> dict[str, Any]:
    report = _empty_my_complaints_report()
    if not _click_tab_like(page, "Мои жалобы"):
        report["blocker"] = "Мои жалобы tab was not found"
        return report
    _wait_settle(page, 2500)
    for tab_label, key in (("Ждут ответа", "pending"), ("Есть ответ", "answered")):
        clicked = _click_tab_like(page, tab_label)
        _wait_settle(page, 2000)
        rows = extract_visible_complaint_rows(page, max_rows=config.max_complaint_rows)
        report[key] = {
            "tab_clicked": clicked,
            "visible_rows": len(rows),
            "rows": rows,
            "field_availability": field_availability(rows),
        }
    report["pending_count_visible"] = report["pending"]["visible_rows"]
    report["answered_count_visible"] = report["answered"]["visible_rows"]
    answered_rows = report["answered"].get("rows") or []
    report["accepted_rejected_detectable"] = _decision_detectability(answered_rows)
    report["status_decision_fields_available"] = field_availability(answered_rows).get("decision_label", False)
    report["success"] = True
    return report


def extract_visible_feedback_rows(page: Page, *, max_rows: int) -> list[dict[str, Any]]:
    raw_rows = page.evaluate(_DOM_CANDIDATE_SCRIPT, {"kind": "feedback", "limit": max_rows})
    return [parse_feedback_candidate(row) for row in raw_rows]


def extract_visible_complaint_rows(page: Page, *, max_rows: int) -> list[dict[str, Any]]:
    raw_rows = page.evaluate(_DOM_CANDIDATE_SCRIPT, {"kind": "complaint", "limit": max_rows})
    return [parse_complaint_candidate(row) for row in raw_rows]


def parse_feedback_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    text = str(candidate.get("text") or "")
    attrs = _safe_mapping(candidate.get("attrs") if isinstance(candidate.get("attrs"), dict) else {})
    links = [str(item) for item in candidate.get("links") or []][:5]
    link_texts = [str(item) for item in candidate.get("link_texts") or []][:5]
    buttons = [str(item) for item in candidate.get("buttons") or []][:10]
    structured = candidate.get("structured") if isinstance(candidate.get("structured"), dict) else {}
    hidden_feedback_id = _extract_feedback_id(attrs, links, text)
    supplier_article = _safe_text(
        str(structured.get("supplier_article") or _guess_supplier_article(text, buttons)),
        120,
    )
    wb_article = str(
        structured.get("wb_article")
        or _guess_wb_article(text, buttons=buttons, links=links)
        or ""
    )
    nm_id = str(structured.get("nm_id") or wb_article or _guess_article(text, label_re=r"(?:nm\s*id|nmid)") or "")
    review_datetime = str(structured.get("review_datetime") or _guess_datetime(text) or "")
    review_date = str(structured.get("review_date") or _guess_date(review_datetime or text) or "")
    review_text = str(structured.get("review_text") or _guess_review_text(text) or "")
    pros = str(structured.get("pros") or _field_after_label(text, ("Достоинства", "Плюсы")) or "")
    cons = str(structured.get("cons") or _field_after_label(text, ("Недостатки", "Минусы")) or "")
    comment = str(structured.get("comment") or _field_after_label(text, ("Комментарий",)) or "")
    media_indicators = _unique_preserve(
        [
            *(_guess_media_indicators(text) or []),
            *[str(item) for item in structured.get("media_indicators") or []],
        ]
    )
    return {
        "dom_scout_id": str(candidate.get("dom_scout_id") or ""),
        "product_title": _guess_product_title(
            text,
            supplier_article=supplier_article,
            wb_article=wb_article,
            link_texts=link_texts,
        ),
        "supplier_article": supplier_article,
        "vendor_article": supplier_article,
        "wb_article": wb_article,
        "nm_id": nm_id,
        "rating": str(structured.get("rating") or _guess_rating(text) or ""),
        "review_date": review_date,
        "review_datetime": review_datetime,
        "text_snippet": _safe_text(review_text, SAFE_TEXT_LIMIT),
        "pros_snippet": _safe_text(pros, SAFE_TEXT_LIMIT),
        "cons_snippet": _safe_text(cons, SAFE_TEXT_LIMIT),
        "comment_snippet": _safe_text(comment, SAFE_TEXT_LIMIT),
        "answer_status": _guess_answer_status(text),
        "purchase_status": str(structured.get("purchase_status") or _guess_purchase_status(text) or ""),
        "media_indicators": media_indicators,
        "three_dot_menu_found": bool(structured.get("row_menu_button_found") or _has_three_dot_button(buttons)),
        "hidden_feedback_id": hidden_feedback_id,
        "links": [_safe_url(link) for link in links],
        "link_texts": [_safe_text(link_text, 160) for link_text in link_texts],
        "data_attributes": attrs,
        "menu_button_candidates": structured.get("menu_button_candidates") or [],
        "dom_fingerprint": _fingerprint(text),
        "row_text_fingerprint": _fingerprint(text),
        "normalized_review_text_fingerprint": _fingerprint(review_text),
        "safe_text_fingerprint": _safe_text(_norm_text(text), 420),
        "selector_hints": [str(candidate.get("selector") or "")] if candidate.get("selector") else [],
        "raw_text_lines": _safe_lines(text, 10),
    }


def parse_complaint_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    text = str(candidate.get("text") or "")
    attrs = _safe_mapping(candidate.get("attrs") if isinstance(candidate.get("attrs"), dict) else {})
    decision = _guess_decision(text)
    review_text = _field_after_label(text, ("Отзыв",)) or _guess_review_text(text)
    return {
        "dom_scout_id": str(candidate.get("dom_scout_id") or ""),
        "product_title": _guess_product_title(text),
        "supplier_article": _guess_article(text, label_re=r"артикул\s*продавца"),
        "wb_article": _guess_article(text, label_re=r"артикул\s*(?:wb|вб)?"),
        "nm_id": _guess_article(text, label_re=r"(?:nm\s*id|nmid|артикул\s*(?:wb|вб)?)"),
        "complaint_reason": _field_after_label(text, ("Причина",)),
        "complaint_description": _field_after_label(text, ("Описание",)),
        "review_text_snippet": review_text,
        "review_rating": _guess_rating(text),
        "review_date": _guess_date(text),
        "review_datetime": _guess_datetime(text),
        "displayed_status": _guess_status(text),
        "decision_label": decision,
        "wb_response_snippet": _field_after_label(text, ("Ответ WB", "Решение", "Комментарий")),
        "row_menu_available": _has_three_dot_button([str(item) for item in candidate.get("buttons") or []]),
        "hidden_ids": _extract_hidden_ids(attrs, [str(item) for item in candidate.get("links") or []], text),
        "data_attributes": attrs,
        "dom_fingerprint": _fingerprint(text),
        "row_text_fingerprint": _fingerprint(text),
        "normalized_review_text_fingerprint": _fingerprint(review_text),
        "safe_text_fingerprint": _safe_text(_norm_text(text), 420),
        "selector_hints": [str(candidate.get("selector") or "")] if candidate.get("selector") else [],
        "raw_text_lines": _safe_lines(text, 10),
    }


def parse_feedback_rows_from_html(html: str, max_rows: int = 20) -> list[dict[str, Any]]:
    parser = _ScoutHTMLParser()
    parser.feed(html)
    rows: list[dict[str, Any]] = []
    for element in parser.elements:
        text = element["text"]
        attrs = element["attrs"]
        if (
            attrs.get("data-scout-feedback-row") is not None
            or attrs.get("data-feedback-id")
            or ("Отзыв" in text and "Артикул" in text)
        ):
            rows.append(
                parse_feedback_candidate(
                    {
                        "text": text,
                        "attrs": attrs,
                        "links": [],
                        "buttons": element.get("buttons", []),
                        "dom_scout_id": attrs.get("data-scout-id", f"fixture-feedback-{len(rows)}"),
                        "selector": "[fixture-feedback-row]",
                    }
                )
            )
        if len(rows) >= max_rows:
            break
    return rows


def parse_row_menu_diagnostics_from_html(html: str, row_index: int = 0) -> dict[str, Any]:
    parser = _ScoutHTMLParser()
    parser.feed(html)
    menu_items: list[str] = []
    for element in parser.elements:
        attrs = element["attrs"]
        role = str(attrs.get("role") or "").lower()
        class_name = str(attrs.get("class") or "")
        is_row_menu = (
            attrs.get("data-scout-row-menu") is not None
            or role == "menu"
            or bool(re.search(r"(Dropdown|dropdown|Menu|menu|Popover|popover)", class_name))
        )
        if not is_row_menu:
            continue
        menu_items.extend(
            _extract_row_menu_items_from_texts([element["text"], *[str(item) for item in element.get("buttons", [])]])
        )
    menu_items = _unique_preserve(menu_items)
    return {
        "row_index": row_index,
        "menu_opened": bool(menu_items),
        "menu_items_found": menu_items,
        "complaint_action_found": any(
            _norm_text(item).lower() == _norm_text(ROW_MENU_COMPLAINT_LABEL).lower()
            for item in menu_items
        ),
    }


def parse_complaint_categories_from_html(html: str) -> dict[str, Any]:
    parser = _ScoutHTMLParser()
    parser.feed(html)
    texts = [_norm_text(element["text"]) for element in parser.elements]
    categories = _extract_complaint_categories(texts)
    return {
        "opened": bool(categories),
        "categories": categories,
        "description_fields": [
            text
            for text in texts
            if any(token in text.lower() for token in ("опис", "коммент", "почему"))
        ][:5],
        "validation_hints": [
            text
            for text in texts
            if any(token in text.lower() for token in ("символ", "обяз", "выберите", "лимит"))
        ][:8],
        "submit_button_seen": any(SUBMIT_LIKE_RE.search(text) for text in texts),
    }


def parse_my_complaints_rows_from_html(html: str, max_rows: int = 20) -> list[dict[str, Any]]:
    parser = _ScoutHTMLParser()
    parser.feed(html)
    rows: list[dict[str, Any]] = []
    for element in parser.elements:
        text = element["text"]
        attrs = element["attrs"]
        if attrs.get("data-scout-complaint-row") is not None or (
            "Причина" in text and "Отзыв" in text
        ):
            rows.append(
                parse_complaint_candidate(
                    {
                        "text": text,
                        "attrs": attrs,
                        "links": [],
                        "buttons": element.get("buttons", []),
                        "dom_scout_id": attrs.get("data-scout-id", f"fixture-complaint-{len(rows)}"),
                        "selector": "[fixture-complaint-row]",
                    }
                )
            )
        if len(rows) >= max_rows:
            break
    return rows


def extract_complaint_modal_state(page: Page) -> dict[str, Any]:
    payload = page.evaluate(
        r"""
() => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const candidates = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], [class*="modal"], [class*="Modal"], [class*="popup"], [class*="Popup"]')).filter(visible);
  const root = candidates[candidates.length - 1] || document.body;
  const text = (root.innerText || '').trim();
  const labelFor = (el) => (el.innerText || el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('value') || '').trim();
  const labels = Array.from(root.querySelectorAll('label, [role="radio"], [role="option"], li, button, textarea, input, [class*="Complaint-form-section__item"]')).filter(visible).map((el) => ({
    tag: el.tagName.toLowerCase(),
    role: el.getAttribute('role') || '',
    type: el.getAttribute('type') || '',
    placeholder: el.getAttribute('placeholder') || '',
    text: labelFor(el)
  })).filter((item) => item.text);
  const buttons = Array.from(root.querySelectorAll('button')).filter(visible).map((button) => labelFor(button)).filter(Boolean);
  const titleCandidates = Array.from(root.querySelectorAll('h1, h2, h3, [class*="title"], [class*="Title"]')).filter(visible).map((el) => labelFor(el)).filter(Boolean);
  return { opened: candidates.length > 0 || /жалоб/i.test(text), text, labels, buttons, titleCandidates };
}
        """
    )
    texts = [_norm_text(str(item.get("text") or "")) for item in payload.get("labels") or []]
    categories = _extract_complaint_categories(texts)
    description_fields = [
        text
        for text in texts
        if any(token in text.lower() for token in ("опис", "коммент", "почему", "подроб"))
    ][:5]
    validation_hints = [
        text
        for text in _safe_lines(str(payload.get("text") or ""), 60)
        if any(token in text.lower() for token in ("символ", "обяз", "выберите", "лимит", "причин"))
    ][:8]
    button_labels = _unique_preserve(str(item) for item in payload.get("buttons") or [])
    submit_labels = [label for label in button_labels if SUBMIT_LIKE_RE.search(label)]
    submit_seen = bool(submit_labels or any(SUBMIT_LIKE_RE.search(text) for text in texts))
    return {
        "opened": bool(payload.get("opened")),
        "modal_title": _safe_text(str((payload.get("titleCandidates") or [""])[0] or ""), 180),
        "modal_text_preview": _safe_text(str(payload.get("text") or ""), 500),
        "categories": categories,
        "description_fields": description_fields,
        "description_field_found": bool(description_fields),
        "validation_hints": validation_hints,
        "submit_button_seen": submit_seen,
        "submit_button_label": submit_labels[0] if submit_labels else "",
        "button_labels": button_labels[:10],
        "modal_text_fingerprint": _fingerprint(str(payload.get("text") or "")),
    }


def extract_open_row_menu_state(page: Page) -> dict[str, Any]:
    try:
        payload = page.evaluate(
            r"""
() => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < window.innerHeight;
  };
  const labelFor = (el) => (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').replace(/\s+/g, ' ').trim();
  const selector = '[role="menu"], [role="listbox"], [data-popper-placement], [class*="Dropdown"], [class*="dropdown"], [class*="Popover"], [class*="popover"], [class*="Menu"], [class*="menu"], ul';
  const roots = Array.from(document.querySelectorAll(selector))
    .filter(visible)
    .filter((root) => /Пожаловаться\s+на\s+отзыв|Запросить\s+возврат/i.test(labelFor(root)));
  return roots.slice(0, 10).map((root) => {
    const rect = root.getBoundingClientRect();
    const items = Array.from(root.querySelectorAll('button, [role="button"], [role="menuitem"], li, div, span'))
      .filter(visible)
      .map((el) => labelFor(el))
      .filter(Boolean);
    const rootText = labelFor(root);
    if (rootText) items.unshift(rootText);
    return {
      tag: root.tagName.toLowerCase(),
      role: root.getAttribute('role') || '',
      className: String(root.className || '').slice(0, 160),
      rect: {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)},
      text: rootText,
      items
    };
  });
}
            """
        )
    except PlaywrightError:
        payload = []
    items: list[str] = []
    root_texts: list[str] = []
    for root in payload if isinstance(payload, list) else []:
        root_texts.append(_safe_text(str(root.get("text") or ""), 240))
        items.extend(_extract_row_menu_items_from_texts([str(item) for item in root.get("items") or []]))
    items = _unique_preserve(items)
    return {
        "root_count": len(payload) if isinstance(payload, list) else 0,
        "root_texts": [text for text in root_texts if text],
        "items": items,
        "complaint_action_found": any(
            _norm_text(item).lower() == _norm_text(ROW_MENU_COMPLAINT_LABEL).lower()
            for item in items
        ),
        "return_action_found": any(
            _norm_text(item).lower() == _norm_text(ROW_MENU_RETURN_LABEL).lower()
            for item in items
        ),
    }


def extract_open_menu_labels(page: Page) -> list[str]:
    return [str(item) for item in extract_open_row_menu_state(page).get("items") or []][:20]


def click_open_row_menu_complaint_action(page: Page) -> dict[str, Any]:
    try:
        return page.evaluate(
            r"""
(targetText) => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < window.innerHeight;
  };
  const labelFor = (el) => (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').replace(/\s+/g, ' ').trim();
  const selector = '[role="menu"], [role="listbox"], [data-popper-placement], [class*="Dropdown"], [class*="dropdown"], [class*="Popover"], [class*="popover"], [class*="Menu"], [class*="menu"], ul';
  const roots = Array.from(document.querySelectorAll(selector))
    .filter(visible)
    .filter((root) => /Пожаловаться\s+на\s+отзыв|Запросить\s+возврат/i.test(labelFor(root)));
  for (const root of roots) {
    const candidates = Array.from(root.querySelectorAll('button, [role="button"], [role="menuitem"], li'))
      .filter(visible)
      .filter((el) => labelFor(el) === targetText)
      .sort((a, b) => {
        const aButton = a.tagName === 'BUTTON' ? 0 : 1;
        const bButton = b.tagName === 'BUTTON' ? 0 : 1;
        return aButton - bButton;
      });
    const target = candidates.find((el) => !el.disabled) || candidates[0];
    if (target) {
      const rect = target.getBoundingClientRect();
      target.click();
      return {
        ok: true,
        label: labelFor(target),
        tag: target.tagName.toLowerCase(),
        rect: {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)}
      };
    }
  }
  return {ok: false, reason: 'complaint action not found inside visible row menu'};
}
            """,
            ROW_MENU_COMPLAINT_LABEL,
        )
    except PlaywrightError as exc:
        return {"ok": False, "reason": _safe_text(str(exc), 300)}


def detect_complaint_success_state(page: Page) -> dict[str, Any]:
    try:
        texts = page.evaluate(
            r"""
() => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < window.innerHeight;
  };
  const roots = Array.from(document.querySelectorAll('[role="status"], [aria-live], [class*="Toast"], [class*="toast"], [class*="Notification"], [class*="notification"], [class*="Snackbar"], [class*="snackbar"]')).filter(visible);
  return roots.map((root) => (root.innerText || '').replace(/\s+/g, ' ').trim()).filter(Boolean).slice(0, 20);
}
            """
        )
    except PlaywrightError:
        texts = []
    for text in [str(item) for item in texts]:
        if re.search(r"(жалоб[ауы].{0,40}(отправ|создан|принят)|успешно.{0,40}жалоб)", text, re.IGNORECASE):
            return {"seen": True, "text": _safe_text(text, 300)}
    return {"seen": False, "text": ""}


def close_modal_without_submit(page: Page) -> str:
    _safe_escape(page)
    time.sleep(0.3)
    if not _modal_visible(page):
        return "escape"
    for label in ("Закрыть", "Отмена", "Отменить"):
        locator = _find_text_locator(page, label)
        if locator is None:
            continue
        assert_safe_click_label(label, purpose="close_modal")
        try:
            locator.click(timeout=2000)
            time.sleep(0.3)
            if not _modal_visible(page):
                return f"text={label}"
        except PlaywrightError:
            continue
    close_icon = click_modal_close_icon(page)
    if close_icon.get("ok"):
        time.sleep(0.3)
        if not _modal_visible(page):
            return "icon_close"
    return "not_closed"


def click_modal_close_icon(page: Page) -> dict[str, Any]:
    try:
        return page.evaluate(
            r"""
() => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < window.innerHeight;
  };
  const labelFor = (el) => (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').replace(/\s+/g, ' ').trim();
  const dialogs = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], [class*="modal"], [class*="Modal"], [class*="popup"], [class*="Popup"]')).filter(visible);
  const root = dialogs[dialogs.length - 1];
  if (!root) return {ok: false, reason: 'modal not visible'};
  const rr = root.getBoundingClientRect();
  const candidates = Array.from(document.querySelectorAll('button, [role="button"]'))
    .filter(visible)
    .map((button) => {
      const rect = button.getBoundingClientRect();
      const label = labelFor(button);
      return {button, label, rect};
    })
    .filter((item) => {
      if (/отправ|подать|сохран|пожаловаться/i.test(item.label)) return false;
      const small = item.rect.width >= 24 && item.rect.width <= 56 && item.rect.height >= 24 && item.rect.height <= 56;
      const explicit = /закрыть|close|отмена|отменить/i.test(item.label);
      const nearTopRight = item.rect.x >= rr.right - 90 && item.rect.x <= rr.right + 70 && item.rect.y >= rr.y - 90 && item.rect.y <= rr.y + 30;
      return explicit || (small && nearTopRight);
    })
    .sort((a, b) => {
      const aExplicit = /закрыть|close|отмена|отменить/i.test(a.label) ? 0 : 1;
      const bExplicit = /закрыть|close|отмена|отменить/i.test(b.label) ? 0 : 1;
      if (aExplicit !== bExplicit) return aExplicit - bExplicit;
      return b.rect.x - a.rect.x;
    });
  const target = candidates[0];
  if (!target) return {ok: false, reason: 'safe close icon not found'};
  const rect = target.rect;
  target.button.click();
  return {ok: true, label: target.label, rect: {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)}};
}
            """
        )
    except PlaywrightError as exc:
        return {"ok": False, "reason": _safe_text(str(exc), 300)}


def assert_safe_click_label(label: str, *, purpose: str) -> None:
    normalized = _norm_text(label).lower()
    allowed_modal_open = purpose == "open_complaint_modal" and normalized == _norm_text("Пожаловаться на отзыв").lower()
    allowed_close = purpose == "close_modal" and normalized in {
        _norm_text("Закрыть").lower(),
        _norm_text("Отмена").lower(),
        _norm_text("Отменить").lower(),
    }
    if allowed_modal_open or allowed_close:
        return
    if SUBMIT_LIKE_RE.search(normalized):
        raise RuntimeError(f"read-only guard refused submit-like click: {label!r}")


def score_feedback_match(source_feedback: dict[str, Any], ui_row: dict[str, Any]) -> dict[str, Any]:
    source_id = str(source_feedback.get("feedback_id") or source_feedback.get("id") or "").strip()
    ui_id = str(ui_row.get("hidden_feedback_id") or ui_row.get("feedback_id") or "").strip()
    if source_id and ui_id and source_id == ui_id:
        return {"score": 1.0, "status": "exact", "reasons": ["feedback_id exact match"]}

    reasons: list[str] = []
    score = 0.0
    text_a = _norm_text(str(source_feedback.get("text") or source_feedback.get("text_snippet") or ""))
    text_b = _norm_text(str(ui_row.get("text_snippet") or ""))
    text_similarity = SequenceMatcher(None, text_a.lower(), text_b.lower()).ratio() if text_a and text_b else 0.0
    if text_similarity >= 0.92:
        score += 0.42
        reasons.append("text high similarity")
    elif text_similarity >= 0.72:
        score += 0.28
        reasons.append("text medium similarity")
    elif text_similarity >= 0.45:
        score += 0.12
        reasons.append("text weak similarity")

    if _same_nonempty(source_feedback.get("rating"), ui_row.get("rating")):
        score += 0.15
        reasons.append("same rating")
    source_datetime = source_feedback.get("created_at") or source_feedback.get("created_datetime") or source_feedback.get("review_datetime")
    ui_datetime = ui_row.get("review_datetime") or ui_row.get("created_at")
    if _same_datetimeish(source_datetime, ui_datetime):
        score += 0.22
        reasons.append("same exact datetime")
    elif _same_dateish(source_feedback.get("created_date") or source_feedback.get("review_date") or source_datetime, ui_row.get("review_date") or ui_datetime):
        score += 0.14
        reasons.append("same date/day")
    if _same_nonempty(source_feedback.get("nm_id") or source_feedback.get("wb_article"), ui_row.get("nm_id") or ui_row.get("wb_article")):
        score += 0.2
        reasons.append("same nmId/WB article")
    elif _same_nonempty(source_feedback.get("supplier_article"), ui_row.get("supplier_article")):
        score += 0.14
        reasons.append("same supplier article")
    if len(text_a) < 30 or len(text_b) < 30:
        score -= 0.08
        reasons.append("short text penalty")
    score = max(0.0, min(0.99, round(score, 3)))
    if score >= 0.82:
        status = "high"
    elif score >= 0.55:
        status = "ambiguous"
    else:
        status = "not_found"
    return {"score": score, "status": status, "reasons": reasons, "text_similarity": round(text_similarity, 3)}


def build_matching_feasibility(report: dict[str, Any]) -> dict[str, Any]:
    feedback_rows = report.get("feedbacks", {}).get("rows") or []
    complaints_pending = report.get("my_complaints", {}).get("pending", {}).get("rows") or []
    complaints_answered = report.get("my_complaints", {}).get("answered", {}).get("rows") or []
    hidden_id = any(bool(row.get("hidden_feedback_id")) for row in feedback_rows)
    reliable_fields = [
        field
        for field in (
            "text_snippet",
            "rating",
            "review_datetime",
            "review_date",
            "nm_id",
            "wb_article",
            "supplier_article",
            "product_title",
        )
        if any(row.get(field) for row in feedback_rows)
    ]
    return {
        "direct_feedback_id_match_possible": bool(hidden_id),
        "hidden_feedback_id_available": bool(hidden_id),
        "direct_match_possible": bool(hidden_id),
        "reliable_fields": reliable_fields,
        "complaint_status_match_fields": [
            field
            for field in (
                "complaint_reason",
                "complaint_description",
                "review_text_snippet",
                "review_rating",
                "review_datetime",
                "review_date",
                "nm_id",
                "decision_label",
            )
            if any(row.get(field) for row in [*complaints_pending, *complaints_answered])
        ],
        "proposed_score_formula": {
            "feedback_id_exact": 1.0,
            "text_high_similarity": 0.42,
            "same_rating": 0.15,
            "same_exact_datetime": 0.22,
            "same_date_or_day": 0.14,
            "same_nm_id_or_wb_article": 0.20,
            "same_supplier_article": 0.14,
            "short_text_penalty": -0.08,
        },
        "proposed_statuses": ["exact", "high", "ambiguous", "not_found"],
        "recommended_thresholds": {
            "auto_submit": "exact only",
            "operator_confirmation_required": "high",
            "no_submit": "ambiguous/not_found",
        },
        "main_risks": _matching_risks(feedback_rows, complaints_pending, complaints_answered),
    }


def field_availability(rows: list[dict[str, Any]]) -> dict[str, bool]:
    fields = [
        "product_title",
        "supplier_article",
        "wb_article",
        "nm_id",
        "rating",
        "review_date",
        "review_datetime",
        "text_snippet",
        "pros_snippet",
        "cons_snippet",
        "comment_snippet",
        "answer_status",
        "purchase_status",
        "media_indicators",
        "hidden_feedback_id",
        "row_menu_opened",
        "complaint_action_found",
        "complaint_reason",
        "complaint_description",
        "review_text_snippet",
        "displayed_status",
        "decision_label",
        "wb_response_snippet",
    ]
    return {field: any(bool(row.get(field)) for row in rows) for field in fields}


def write_report_artifacts(report: dict[str, Any], output_root: Path) -> dict[str, Path]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    json_path = run_dir / "seller_portal_feedbacks_complaints_scout.json"
    md_path = run_dir / "seller_portal_feedbacks_complaints_scout.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown_report(report), encoding="utf-8")
    return {"run_dir": run_dir, "json": json_path, "markdown": md_path}


def render_markdown_report(report: dict[str, Any]) -> str:
    modal = report.get("complaint_modal", {})
    my = report.get("my_complaints", {})
    nav = report.get("navigation", {})
    feedbacks = report.get("feedbacks", {})
    matching = report.get("matching_feasibility", {})
    lines = [
        "# Seller Portal Feedback Complaints Scout",
        "",
        f"- Mode: `{report.get('mode')}`",
        f"- Started: `{report.get('started_at')}`",
        f"- Finished: `{report.get('finished_at')}`",
        f"- Session status: `{report.get('session', {}).get('status')}`",
        f"- Navigation: `{nav.get('method') or 'not_reached'}` -> `{nav.get('final_url', '')}`",
        f"- Feedback rows parsed: `{feedbacks.get('visible_rows_parsed_count', 0)}`",
        f"- Hidden feedback id available: `{feedbacks.get('hidden_feedback_id_available')}`",
        f"- Three-dot menu found: `{feedbacks.get('three_dot_menu_found')}`",
        f"- Row-level menu opened: `{feedbacks.get('row_level_menu_found')}`",
        f"- Complaint action found: `{feedbacks.get('complaint_action_found')}`",
        f"- Complaint modal opened safely: `{modal.get('modal_opened_safely')}`",
        f"- Submit button not clicked: `{modal.get('submit_button_not_clicked')}`",
        f"- Categories: `{', '.join(modal.get('categories') or [])}`",
        f"- My complaints pending visible: `{my.get('pending_count_visible', 0)}`",
        f"- My complaints answered visible: `{my.get('answered_count_visible', 0)}`",
        f"- Accepted/rejected detectable: `{my.get('accepted_rejected_detectable')}`",
        f"- Direct match possible: `{matching.get('direct_match_possible')}`",
        "",
        "## Matching",
        "",
        f"- Reliable fields: `{', '.join(matching.get('reliable_fields') or [])}`",
        f"- Thresholds: `{json.dumps(matching.get('recommended_thresholds') or {}, ensure_ascii=False)}`",
        f"- Risks: `{', '.join(matching.get('main_risks') or [])}`",
    ]
    if report.get("errors"):
        lines.extend(["", "## Errors", ""])
        for error in report["errors"]:
            lines.append(f"- `{error.get('stage')}` / `{error.get('code')}`: {error.get('message')}")
    return "\n".join(lines) + "\n"


def _compact_stdout_report(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract_name": report.get("contract_name"),
        "mode": report.get("mode"),
        "session": report.get("session"),
        "navigation": report.get("navigation"),
        "feedbacks": {
            key: report.get("feedbacks", {}).get(key)
            for key in (
                "success",
                "visible_rows_parsed_count",
                "hidden_feedback_id_available",
                "three_dot_menu_found",
                "row_level_menu_found",
                "complaint_action_found",
                "field_availability",
                "blocker",
            )
        },
        "complaint_modal": {
            key: report.get("complaint_modal", {}).get(key)
            for key in (
                "success",
                "modal_opened_safely",
                "categories",
                "category_list_varies_by_review",
                "has_other_category",
                "submit_button_not_clicked",
                "blocker",
            )
        },
        "my_complaints": {
            key: report.get("my_complaints", {}).get(key)
            for key in (
                "success",
                "pending_count_visible",
                "answered_count_visible",
                "accepted_rejected_detectable",
                "blocker",
            )
        },
        "matching_feasibility": report.get("matching_feasibility"),
        "errors": report.get("errors"),
        "artifact_paths": report.get("artifact_paths"),
    }


def _empty_feedbacks_report() -> dict[str, Any]:
    return {
        "success": False,
        "visible_rows_parsed_count": 0,
        "field_availability": {},
        "rows": [],
        "selectors": [],
        "hidden_feedback_id_available": False,
        "three_dot_menu_found": False,
        "row_level_menu_found": False,
        "complaint_action_found": False,
        "row_menu_diagnostics": [],
        "blocker": "",
    }


def _empty_modal_report() -> dict[str, Any]:
    return {
        "success": False,
        "open_requested": False,
        "modal_opened_safely": False,
        "categories": [],
        "category_list_varies_by_review": "unknown",
        "has_other_category": False,
        "known_category_hits": [],
        "submit_button_seen": False,
        "submit_button_not_clicked": True,
        "samples": [],
        "blocker": "",
    }


def _empty_my_complaints_report() -> dict[str, Any]:
    return {
        "success": False,
        "pending_count_visible": 0,
        "answered_count_visible": 0,
        "pending": {"visible_rows": 0, "rows": [], "field_availability": {}, "tab_clicked": False},
        "answered": {"visible_rows": 0, "rows": [], "field_availability": {}, "tab_clicked": False},
        "status_decision_fields_available": False,
        "accepted_rejected_detectable": "unknown",
        "blocker": "",
    }


def _click_safe_row_menu(page: Page, dom_id: str) -> dict[str, Any]:
    return page.evaluate(
        r"""
(domId) => {
  const row = document.querySelector(`[data-wb-core-scout-id="${domId}"]`);
  if (!row) return {ok: false, reason: 'row not found'};
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < window.innerHeight;
  };
  const labelFor = (button) => (button.innerText || button.getAttribute('aria-label') || button.getAttribute('title') || '').replace(/\s+/g, ' ').trim();
  const rowRect = row.getBoundingClientRect();
  const actualButtons = Array.from(row.querySelectorAll('button')).filter(visible);
  const roleButtons = Array.from(row.querySelectorAll('[role="button"]')).filter(visible).filter((button) => !button.querySelector('button'));
  const buttons = actualButtons.concat(roleButtons).map((button) => {
    const rect = button.getBoundingClientRect();
    return {
      button,
      tag: button.tagName.toLowerCase(),
      disabled: Boolean(button.disabled || button.getAttribute('aria-disabled') === 'true'),
      rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
      label: labelFor(button),
      html: String(button.innerHTML || '').slice(0, 260)
    };
  });
  const danger = /(ответить|редакт|удал|сохран|отправ|подать)/i;
  const preferred = buttons.find((item) => {
    return !danger.test(item.label) && /(ещ|ещё|еще|действ|меню|more|⋮|\.\.\.)/i.test(item.label);
  });
  const iconOnly = buttons
    .filter((item) => {
      const small = item.rect.width >= 24 && item.rect.width <= 56 && item.rect.height >= 24 && item.rect.height <= 56;
      const rightEdge = item.rect.x >= rowRect.right - 150;
      const dotSvg = /viewBox="-10 -3 24 24"|C0 3\.10457|0 2C0 3/i.test(item.html);
      return !item.disabled && !item.label && small && rightEdge && (dotSvg || item.rect.x >= rowRect.right - 80);
    })
    .sort((a, b) => b.rect.x - a.rect.x)[0];
  const target = preferred || iconOnly;
  if (!target) {
    return {
      ok: false,
      reason: 'no safe row-level menu button',
      button_count: buttons.length,
      right_button_count: buttons.filter((item) => item.rect.x >= rowRect.right - 150).length
    };
  }
  target.button.click();
  return {
    ok: true,
    label: target.label,
    tag: target.tag,
    button_count: buttons.length,
    rect: {
      x: Math.round(target.rect.x),
      y: Math.round(target.rect.y),
      width: Math.round(target.rect.width),
      height: Math.round(target.rect.height)
    }
  };
}
        """,
        dom_id,
    )


def _hover_or_click_text(page: Page, text: str) -> bool:
    locator = _first_visible_text_locator(page, text)
    if locator is None:
        return False
    try:
        locator.hover(timeout=3000)
        time.sleep(0.4)
    except PlaywrightError:
        pass
    try:
        locator.click(timeout=3000)
    except PlaywrightError:
        return True
    return True


def _click_tab_like(page: Page, text: str) -> bool:
    for role in ("tab", "button", "link"):
        try:
            locator = _first_visible_locator(
                page.get_by_role(role, name=re.compile(re.escape(text), re.IGNORECASE))
            )
            if locator is not None:
                assert_safe_click_label(text, purpose="tab_navigation")
                locator.click(timeout=3000)
                return True
        except PlaywrightError:
            pass
    locator = _first_visible_text_locator(page, text)
    if locator is not None:
        assert_safe_click_label(text, purpose="tab_navigation")
        try:
            locator.click(timeout=3000)
            return True
        except PlaywrightError:
            return False
    return False


def _first_visible_text_locator(page: Page, text: str):
    for exact in (True, False):
        try:
            locator = _first_visible_locator(page.get_by_text(text, exact=exact))
            if locator is not None:
                return locator
        except PlaywrightError:
            pass
    return None


def _find_text_locator(page: Page, text: str):
    try:
        locator = _first_visible_locator(page.get_by_text(text, exact=True))
        if locator is not None:
            return locator
    except PlaywrightError:
        pass
    try:
        locator = _first_visible_locator(page.get_by_text(text, exact=False))
        if locator is not None:
            return locator
    except PlaywrightError:
        pass
    return None


def _first_visible_locator(locator: Any, *, limit: int = 20):
    try:
        count = min(locator.count(), limit)
    except PlaywrightError:
        return None
    for index in range(count):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible(timeout=600):
                return candidate
        except PlaywrightError:
            continue
    return None


def _safe_locator_attr(locator: Any, attr: str) -> str:
    try:
        value = locator.get_attribute(attr, timeout=1000)
        return str(value or "")
    except PlaywrightError:
        return ""


def _discover_feedback_links(page: Page) -> list[dict[str, str]]:
    try:
        return page.evaluate(
            r"""
() => Array.from(document.querySelectorAll('a')).map((anchor) => ({
  text: (anchor.innerText || '').trim(),
  href: anchor.href || ''
})).filter((item) => /Отзывы и вопросы|Отзывы|Вопросы|жалоб/i.test(item.text + ' ' + item.href)).slice(0, 30)
            """
        )
    except PlaywrightError:
        return []


def _is_feedbacks_questions_page(page: Page) -> bool:
    try:
        body = page.locator("body").inner_text(timeout=3000)
    except PlaywrightError:
        return False
    text = _norm_text(body)
    url = page.url.lower()
    has_feedback_tabs = "Отзывы" in text and "Вопросы" in text and ("Мои жалобы" in text or "/feedbacks/" in url)
    return bool(has_feedback_tabs and "/feedbacks/" in url)


def _looks_like_login(page: Page) -> bool:
    url = page.url.lower()
    if "seller-auth" in url or "passport" in url or "login" in url:
        return True
    try:
        text = page.locator("body").inner_text(timeout=1500).lower()
    except PlaywrightError:
        return False
    return "войти" in text and ("телефон" in text or "пароль" in text or "qr" in text)


def _modal_visible(page: Page) -> bool:
    try:
        return bool(
            page.evaluate(
                r"""
() => Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], [class*="modal"], [class*="Modal"]')).some((el) => {
  const style = window.getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
})
                """
            )
        )
    except PlaywrightError:
        return False


def _safe_escape(page: Page) -> None:
    try:
        page.keyboard.press("Escape")
    except PlaywrightError:
        pass


def _wait_settle(page: Page, timeout_ms: int = 2000) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 5000))
    except PlaywrightError:
        pass
    page.wait_for_timeout(timeout_ms)


def _wait_for_text_visible(page: Page, text: str, *, timeout_ms: int) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        if _first_visible_text_locator(page, text) is not None:
            return True
        page.wait_for_timeout(500)
    return False


def _wait_for_feedback_rows(page: Page, *, timeout_ms: int) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        try:
            found = bool(
                page.evaluate(
                    r"""
() => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 240 && rect.height > 32 && rect.bottom > 0 && rect.top < window.innerHeight;
  };
  const text = (el) => (el.innerText || '').replace(/\s+/g, ' ').trim();
  return Array.from(document.querySelectorAll('tr[data-testid="Base-table-row"][role="button"], [data-testid="Base-table-row"][role="button"], [data-testid="feedback-item"]'))
    .filter(visible)
    .some((el) => /Плюсы|Минусы|Комментарий|Выкуп|\d{1,2}\.\d{1,2}\.\d{4}\s+в\s+\d{1,2}:\d{2}/i.test(text(el)));
}
                    """
                )
            )
        except PlaywrightError:
            found = False
        if found:
            return True
        page.wait_for_timeout(500)
    return False


def _decision_detectability(rows: list[dict[str, Any]]) -> str:
    decisions = [str(row.get("decision_label") or "").strip() for row in rows]
    if any(decision in {"approved", "rejected"} for decision in decisions):
        return "yes"
    if rows:
        return "needs_detail_click"
    return "unknown"


def _matching_risks(
    feedback_rows: list[dict[str, Any]],
    complaints_pending: list[dict[str, Any]],
    complaints_answered: list[dict[str, Any]],
) -> list[str]:
    risks: list[str] = []
    if not any(row.get("hidden_feedback_id") for row in feedback_rows):
        risks.append("no hidden feedback_id observed in visible feedback rows")
    if not any(row.get("review_date") for row in feedback_rows):
        risks.append("review date unavailable in visible feedback rows")
    if not any(row.get("rating") for row in feedback_rows):
        risks.append("rating unavailable in visible feedback rows")
    if not any(row.get("nm_id") or row.get("wb_article") for row in feedback_rows):
        risks.append("nmId/WB article unavailable in visible feedback rows")
    if not (complaints_pending or complaints_answered):
        risks.append("complaint status rows not observed; status sync may need empty-state handling")
    if any(len(str(row.get("text_snippet") or "")) < 30 for row in feedback_rows):
        risks.append("short review texts can create duplicate/ambiguous matches")
    return risks or ["no major matching risks observed in bounded sample"]


def _guess_product_title(
    text: str,
    *,
    supplier_article: str = "",
    wb_article: str = "",
    link_texts: Iterable[str] = (),
) -> str:
    for link_text in link_texts:
        normalized = _norm_text(str(link_text or ""))
        if normalized and not re.fullmatch(r"\d{5,}", normalized):
            return _safe_text(normalized, 160)
    lines = [
        line
        for line in _safe_lines(text, 20)
        if not any(
            token.lower() in line.lower()
            for token in (
                "Отзыв",
                "Артикул",
                "Оценка",
                "Дата",
                "Пожаловаться",
                "Достоинства",
                "Недостатки",
                "Плюсы",
                "Минусы",
                "Комментарий",
                "Причина",
                "Описание",
                "Выкуп",
            )
        )
    ]
    if lines:
        candidate = lines[0]
    else:
        candidate = _norm_text(text)
    for marker in (supplier_article, wb_article):
        marker_text = _norm_text(str(marker or ""))
        if marker_text and marker_text in candidate:
            candidate = candidate.split(marker_text, 1)[0]
    candidate = re.split(r"\b(?:Выкуп|Плюсы|Минусы|Комментарий|Отзыв|Достоинства|Недостатки)\b", candidate, maxsplit=1)[0]
    candidate = DATE_TIME_RE.sub("", candidate)
    candidate = DATE_RE.sub("", candidate)
    return _safe_text(candidate, 160)


def _guess_supplier_article(text: str, buttons: Iterable[str]) -> str:
    for button in buttons:
        label = _norm_text(str(button or ""))
        if not label or re.fullmatch(r"\d{5,}", label):
            continue
        if re.search(r"(выкуп|оценка|дата|отправ|пожаловаться|запросить|фильтр)", label, re.IGNORECASE):
            continue
        if len(label) <= 120:
            return label
    labeled = _field_after_label(text, ("Артикул продавца", "Артикул поставщика", "Артикул"))
    return labeled


def _guess_wb_article(text: str, *, buttons: Iterable[str], links: Iterable[str]) -> str:
    for button in buttons:
        label = _norm_text(str(button or ""))
        if re.fullmatch(r"\d{5,}", label):
            return label
    for link in links:
        match = re.search(r"/catalog/(\d{5,})", str(link or ""))
        if match:
            return match.group(1)
    return _guess_article(text, label_re=r"артикул\s*(?:wb|вб)?")


def _guess_article(text: str, *, label_re: str) -> str:
    local_re = re.compile(label_re + r"\D{0,16}(\d{5,})", re.IGNORECASE)
    match = local_re.search(text)
    if match:
        return match.group(1)
    match = ARTICLE_RE.search(text)
    return match.group(1) if match else ""


def _guess_rating(text: str) -> str:
    match = RATING_RE.search(text)
    if match:
        return match.group(1)
    star_count = text.count("★")
    if 1 <= star_count <= 5:
        return str(star_count)
    return ""


def _guess_datetime(text: str) -> str:
    match = DATE_TIME_RE.search(text)
    return _norm_text(match.group(1)) if match else ""


def _guess_date(text: str) -> str:
    match = DATE_RE.search(text)
    return match.group(1) if match else ""


def _guess_review_text(text: str) -> str:
    labeled = _field_after_label(text, ("Отзыв", "Текст отзыва"))
    if labeled:
        return labeled
    lines = [line for line in _safe_lines(text, 30) if len(line) >= 20]
    if not lines:
        return ""
    lines.sort(key=len, reverse=True)
    return _safe_text(lines[0], SAFE_TEXT_LIMIT)


def _guess_purchase_status(text: str) -> str:
    lower = text.lower()
    if "не выкуп" in lower or "возврат" in lower:
        return "return_or_not_bought"
    if "выкуп" in lower:
        return "buyout"
    return ""


def _field_after_label(text: str, labels: Iterable[str]) -> str:
    lines = _safe_lines(text, 80)
    label_set = tuple(label.lower() for label in labels)
    for index, line in enumerate(lines):
        lower = line.lower().strip(": ")
        for label in label_set:
            if lower == label or lower.startswith(label + ":"):
                suffix = line.split(":", 1)[1].strip() if ":" in line else ""
                if suffix:
                    return _safe_text(suffix, SAFE_TEXT_LIMIT)
                if index + 1 < len(lines):
                    return _safe_text(lines[index + 1], SAFE_TEXT_LIMIT)
    normalized = _norm_text(text)
    stop_labels = (
        "Товар",
        "Артикул",
        "Причина",
        "Описание",
        "Отзыв",
        "Оценка",
        "Дата",
        "Плюсы",
        "Минусы",
        "Достоинства",
        "Недостатки",
        "Ответ WB",
        "Решение",
        "Комментарий",
        "Статус",
    )
    for label in labels:
        pattern = re.compile(
            rf"(?:^|\s){re.escape(label)}\s*:?\s+(.+?)(?=\s+(?:{'|'.join(re.escape(item) for item in stop_labels)})\b|$)",
            re.IGNORECASE,
        )
        match = pattern.search(normalized)
        if match:
            return _safe_text(match.group(1), SAFE_TEXT_LIMIT)
    return ""


def _guess_answer_status(text: str) -> str:
    lower = text.lower()
    if "есть ответ" in lower or "отвечен" in lower:
        return "answered"
    if "ждут ответа" in lower or "без ответа" in lower or "нет ответа" in lower:
        return "unanswered"
    return ""


def _guess_media_indicators(text: str) -> list[str]:
    lower = text.lower()
    indicators: list[str] = []
    if "фото" in lower:
        indicators.append("photo")
    if "видео" in lower:
        indicators.append("video")
    return indicators


def _guess_status(text: str) -> str:
    lower = text.lower()
    if "ждет ответа" in lower or "ждёт ответа" in lower or "на рассмотрении" in lower:
        return "pending"
    if "есть ответ" in lower or "рассмотр" in lower:
        return "answered"
    return ""


def _guess_decision(text: str) -> str:
    lower = text.lower()
    if any(token in lower for token in ("одобрен", "удовлетвор", "принят")):
        return "approved"
    if any(token in lower for token in ("отклон", "не одобрен", "отказ")):
        return "rejected"
    return ""


def _extract_feedback_id(attrs: dict[str, Any], links: list[str], text: str) -> str:
    for key, value in attrs.items():
        combined = f"{key}={value}"
        match = FEEDBACK_ID_RE.search(combined)
        if match:
            return match.group(1)
    for value in [*links, text]:
        match = FEEDBACK_ID_RE.search(value)
        if match:
            return match.group(1)
    return ""


def _extract_hidden_ids(attrs: dict[str, Any], links: list[str], text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    feedback_id = _extract_feedback_id(attrs, links, text)
    if feedback_id:
        result["feedback_id"] = feedback_id
    for key, value in attrs.items():
        if "id" in key.lower() and value:
            result[key] = _safe_text(str(value), 120)
    return result


def _has_three_dot_button(buttons: list[str]) -> bool:
    for button in buttons:
        if re.search(r"(ещ|ещё|еще|действ|меню|more|⋮|\.\.\.)", button, re.IGNORECASE):
            return True
    return any(not button.strip() for button in buttons)


def _looks_like_category_text(text: str) -> bool:
    if len(text) < 4 or len(text) > 120:
        return False
    lower = text.lower()
    if any(token in lower for token in ("выберите", "опис", "символ", "отправ", "подать", "сохран")):
        return False
    return any(token in lower for token in ("отзыв", "спам", "лексик", "угроз", "конкур", "фото", "видео", "другое"))


def _extract_complaint_categories(texts: Iterable[str]) -> list[str]:
    categories: list[str] = []
    for raw_text in texts:
        text = _norm_text(raw_text)
        lower = text.lower()
        for expected in EXPECTED_COMPLAINT_CATEGORIES:
            if expected.lower() in lower and expected not in categories:
                categories.append(expected)
        if _looks_like_category_text(text) and text not in categories:
            categories.append(text)
    return categories


def _extract_row_menu_items_from_texts(texts: Iterable[str]) -> list[str]:
    items: list[str] = []
    for raw_text in texts:
        text = _norm_text(str(raw_text or ""))
        if not text:
            continue
        lower = text.lower()
        matched_expected = False
        for expected in ROW_MENU_EXPECTED_LABELS:
            if expected.lower() in lower and expected not in items:
                items.append(expected)
                matched_expected = True
            elif expected.lower() in lower:
                matched_expected = True
        if (
            not matched_expected
            and 3 <= len(text) <= 80
            and any(token in lower for token in ("возврат", "пожаловаться", "отзыв"))
        ):
            if text not in items:
                items.append(text)
    return items


def _same_nonempty(a: Any, b: Any) -> bool:
    return bool(str(a or "").strip() and str(a or "").strip() == str(b or "").strip())


def _normalize_date_key(value: Any) -> str:
    text = _norm_text(str(value or ""))
    if not text:
        return ""
    match = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text)
    if match:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    match = re.search(r"\b(\d{1,2})[./](\d{1,2})[./](\d{2,4})\b", text)
    if match:
        year = int(match.group(3))
        if year < 100:
            year += 2000
        return f"{year:04d}-{int(match.group(2)):02d}-{int(match.group(1)):02d}"
    return text[:10]


def _normalize_datetime_key(value: Any) -> str:
    date_key = _normalize_date_key(value)
    if not date_key:
        return ""
    match = re.search(r"\b(\d{1,2}):(\d{2})\b", str(value or ""))
    if not match:
        return ""
    return f"{date_key} {int(match.group(1)):02d}:{match.group(2)}"


def _same_datetimeish(a: Any, b: Any) -> bool:
    a_key = _normalize_datetime_key(a)
    b_key = _normalize_datetime_key(b)
    return bool(a_key and b_key and a_key == b_key)


def _same_dateish(a: Any, b: Any) -> bool:
    a_key = _normalize_date_key(a)
    b_key = _normalize_date_key(b)
    return bool(a_key and b_key and a_key == b_key)


def _safe_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    safe: dict[str, Any] = {}
    for key, raw in value.items():
        key_text = str(key)
        if any(token in key_text.lower() for token in ("token", "cookie", "secret", "password", "auth")):
            continue
        safe[key_text[:80]] = _safe_text(str(raw), 160)
    return safe


def _safe_url(value: str) -> str:
    if not value:
        return ""
    return re.sub(r"([?&](?:token|auth|cookie|key|signature|sig)=)[^&]+", r"\1<redacted>", value, flags=re.IGNORECASE)


def _safe_text(value: str, limit: int) -> str:
    normalized = TEXT_WS_RE.sub(" ", str(value or "")).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "…"


def _safe_lines(value: str, limit: int) -> list[str]:
    lines = [_safe_text(line, SAFE_TEXT_LIMIT) for line in str(value or "").splitlines()]
    return [line for line in lines if line][:limit]


def _norm_text(value: str) -> str:
    return TEXT_WS_RE.sub(" ", str(value or "")).strip()


def _fingerprint(value: str) -> str:
    return sha256(_norm_text(value).encode("utf-8")).hexdigest()[:16]


def _unique_preserve(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = _norm_text(value)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class _ScoutHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[dict[str, Any]] = []
        self.elements: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key: value or "" for key, value in attrs}
        if tag in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}:
            attr_text = _norm_text(" ".join(attr_map.get(key, "") for key in ("aria-label", "placeholder", "value", "title")))
            if attr_text and self.stack:
                self.stack[-1]["text"].append(attr_text)
            return
        self.stack.append({"tag": tag, "attrs": attr_map, "text": [], "buttons": []})

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self.stack:
            self.stack[-1]["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack:
            return
        node = self.stack.pop()
        text = _norm_text(" ".join(node["text"]))
        if node["tag"] == "button" and self.stack:
            self.stack[-1].setdefault("buttons", []).append(text)
        if text and (
            node["tag"] in {"article", "tr", "li", "section", "div", "label", "button"}
            or any(key.startswith("data-") for key in node["attrs"])
        ):
            self.elements.append({"tag": node["tag"], "attrs": node["attrs"], "text": text, "buttons": node.get("buttons", [])})
        if self.stack:
            self.stack[-1]["text"].append(text)
            self.stack[-1].setdefault("buttons", []).extend(node.get("buttons", []))


_DOM_CANDIDATE_SCRIPT = r"""
({kind, limit}) => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < window.innerHeight;
  };
  const textFor = (el) => (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').replace(/\s+/g, ' ').trim();
  const attrsFor = (el) => {
    const attrs = {};
    for (const attr of Array.from(el.attributes || [])) {
      const name = attr.name || '';
      if (/token|cookie|secret|password|auth/i.test(name)) continue;
      if (name.startsWith('data-') || name === 'id' || name === 'class' || name === 'aria-label' || name === 'role') {
        let value = String(attr.value || '');
        if (/token|cookie|secret|password|auth/i.test(value)) value = '<redacted>';
        attrs[name] = value.slice(0, 160);
      }
    }
    return attrs;
  };
  const buttonLabels = (el) => Array.from(el.querySelectorAll('button, [role="button"]'))
    .filter((button) => {
      const style = window.getComputedStyle(button);
      const rect = button.getBoundingClientRect();
      return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    })
    .map((button) => textFor(button).slice(0, 120));
  const links = (el) => Array.from(el.querySelectorAll('a[href]')).map((a) => a.href).slice(0, 5);
  const linkTexts = (el) => Array.from(el.querySelectorAll('a[href]')).map((a) => textFor(a)).filter(Boolean).slice(0, 5);
  const uniqueTexts = (values) => {
    const result = [];
    for (const value of values) {
      const normalized = String(value || '').replace(/\s+/g, ' ').trim();
      if (normalized && !result.includes(normalized)) result.push(normalized);
    }
    return result;
  };
  const textBlocks = (el) => {
    let primaryNodes = Array.from(el.querySelectorAll('[class*="Feedback-text-block__text-wrapper"]')).filter(visible);
    if (!primaryNodes.length) {
      primaryNodes = Array.from(el.querySelectorAll('[data-testid="text-ellipse"]')).filter(visible);
    }
    const primary = primaryNodes
      .filter(visible)
      .map((node) => textFor(node));
    if (primary.length) return uniqueTexts(primary);
    return uniqueTexts(Array.from(el.querySelectorAll('[class*="Feedback-text-block"]')).filter(visible).map((node) => textFor(node)));
  };
  const fieldAfter = (parts, labels) => {
    for (const part of parts) {
      const text = String(part || '').replace(/\s+/g, ' ').trim();
      for (const label of labels) {
        const re = new RegExp('^' + label + '\\s*:?\\s*(.+)$', 'i');
        const match = text.match(re);
        if (match) return match[1].trim().slice(0, 240);
      }
    }
    return '';
  };
  const structuredFor = (el, norm) => {
    const labels = buttonLabels(el);
    const hrefs = links(el);
    const linkLabels = linkTexts(el);
    const numericButton = labels.find((label) => /^\d{5,}$/.test(label)) || '';
    const supplierButton = labels.find((label) => label && !/^\d{5,}$/.test(label) && !/(выкуп|оценка|дата|фильтр|отправ|пожаловаться|запросить)/i.test(label)) || '';
    const hrefNm = (hrefs.map((href) => String(href || '').match(/\/catalog\/(\d{5,})/)).find(Boolean) || [])[1] || '';
    const dateTimeMatch = norm.match(/\b(\d{1,2}[./]\d{1,2}[./]\d{2,4}\s+(?:в\s+)?\d{1,2}:\d{2})\b/i);
    const dateMatch = norm.match(/\b(\d{1,2}[./]\d{1,2}[./]\d{2,4})\b/i);
    const activeRating = el.querySelector('[class*="Rating--active"], [class*="rating--active"]');
    const activeStars = activeRating ? Array.from(activeRating.querySelectorAll('svg')).filter(visible).length : 0;
    const blocks = textBlocks(el);
    const reviewText = blocks.join(' ').replace(/\s+/g, ' ').trim().slice(0, 240);
    const rowRect = el.getBoundingClientRect();
    const menuButtonCandidates = Array.from(el.querySelectorAll('button')).filter(visible).map((button) => {
      const rect = button.getBoundingClientRect();
      return {
        label: textFor(button).slice(0, 80),
        disabled: Boolean(button.disabled || button.getAttribute('aria-disabled') === 'true'),
        rect: {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)},
        right_edge_candidate: rect.x >= rowRect.right - 150 && rect.width >= 24 && rect.width <= 56 && rect.height >= 24 && rect.height <= 56
      };
    }).filter((item) => item.right_edge_candidate).slice(0, 8);
    return {
      product_title: linkLabels.find((label) => label && !/^\d{5,}$/.test(label)) || '',
      supplier_article: supplierButton,
      wb_article: numericButton || hrefNm,
      nm_id: numericButton || hrefNm,
      review_datetime: dateTimeMatch ? dateTimeMatch[1].replace(/\s+/g, ' ').trim() : '',
      review_date: dateMatch ? dateMatch[1] : '',
      rating: activeStars >= 1 && activeStars <= 5 ? String(activeStars) : '',
      review_text: reviewText,
      pros: fieldAfter(blocks, ['Плюсы', 'Достоинства']),
      cons: fieldAfter(blocks, ['Минусы', 'Недостатки']),
      comment: fieldAfter(blocks, ['Комментарий']),
      purchase_status: /выкуп/i.test(norm) ? 'buyout' : '',
      media_indicators: [
        el.querySelector('[class*="Photo-item"], button[class*="Photo"], [class*="photo"]') ? 'photo' : '',
        el.querySelector('[class*="Video"], [class*="video"]') ? 'video' : ''
      ].filter(Boolean),
      row_menu_button_found: menuButtonCandidates.some((item) => !item.disabled && !item.label),
      menu_button_candidates: menuButtonCandidates
    };
  };
  const feedbackBaseRows = Array.from(document.querySelectorAll('tr[data-testid="Base-table-row"][role="button"], [data-testid="Base-table-row"][role="button"]'));
  const preferred = kind === 'feedback'
    ? (feedbackBaseRows.length ? feedbackBaseRows : Array.from(document.querySelectorAll('[data-testid="feedback-item"], article, li')))
    : Array.from(document.querySelectorAll('[data-testid="Base-table-row"], tr, [role="row"], article, li'));
  const fallback = kind === 'feedback' && feedbackBaseRows.length
    ? []
    : Array.from(document.querySelectorAll('[data-testid], [data-test-id], [data-qa], div'));
  const all = [];
  const seenElements = new Set();
  for (const el of preferred.concat(fallback)) {
    if (seenElements.has(el)) continue;
    seenElements.add(el);
    all.push(el);
  }
  const seen = new Set();
  const rows = [];
  for (const el of all) {
    if (!visible(el)) continue;
    const rect = el.getBoundingClientRect();
    if (rect.width < 240 || rect.height < 24) continue;
    if (rect.height > 420) continue;
    const text = (el.innerText || '').trim();
    const norm = text.replace(/\s+/g, ' ').trim();
    if (norm.length < 35 || norm.length > 2600) continue;
    const attrs = attrsFor(el);
    const className = String(attrs.class || '');
    if (kind === 'feedback' && /Оценка\/дата\s+Отзыв/i.test(norm)) continue;
    if (kind === 'feedback' && /(narrow-banner|carousel|onboarding|New-main-tabs|banner-page)/i.test(className + ' ' + norm)) continue;
    const structured = structuredFor(el, norm);
    const feedbackLike = (
      el.matches('tr[data-testid="Base-table-row"][role="button"], [data-testid="Base-table-row"][role="button"]')
      && (/Плюсы|Минусы|Комментарий|Достоинства|Недостатки|Выкуп|\d{1,2}[./]\d{1,2}[./]\d{2,4}\s+(?:в\s+)?\d{1,2}:\d{2}/i.test(norm) || structured.row_menu_button_found)
    ) || (
      el.matches('[data-testid="feedback-item"]') && /Плюсы|Минусы|Комментарий|Достоинства|Недостатки/i.test(norm)
    );
    const complaintLike = /(Причина|Описание|Мои жалобы|Ждут ответа|Есть ответ|Одобрен|Отклон|Отзыв)/i.test(norm);
    if (kind === 'feedback' && !feedbackLike) continue;
    if (kind === 'complaint' && !complaintLike) continue;
    const hash = norm.slice(0, 260);
    if (seen.has(hash)) continue;
    seen.add(hash);
    const scoutId = `${kind}-${rows.length}`;
    el.setAttribute('data-wb-core-scout-id', scoutId);
    rows.push({
      dom_scout_id: scoutId,
      selector: `[data-wb-core-scout-id="${scoutId}"]`,
      text: text,
      attrs,
      buttons: buttonLabels(el),
      links: links(el),
      link_texts: linkTexts(el),
      structured,
      rect: {width: Math.round(rect.width), height: Math.round(rect.height)}
    });
    if (rows.length >= limit) break;
  }
  return rows;
}
"""


if __name__ == "__main__":
    main()
