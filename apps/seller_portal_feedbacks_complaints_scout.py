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
ARTICLE_RE = re.compile(
    r"(?:артикул\s*(?:wb|вб|продавца)?|nm\s*id|nmid)\D{0,12}(\d{5,})",
    re.IGNORECASE,
)
RATING_RE = re.compile(r"\b([1-5])\s*(?:звезд|звезды|звезда|★|/ ?5)\b", re.IGNORECASE)
FEEDBACK_ID_RE = re.compile(r"(?:feedback|review|comment)[_-]?(?:id)?[=:_/ -]*([a-z0-9-]{8,})", re.IGNORECASE)


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
    _wait_settle(page)
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
    rows = extract_visible_feedback_rows(page, max_rows=config.max_feedback_rows)
    report["visible_rows_parsed_count"] = len(rows)
    report["rows"] = rows
    report["hidden_feedback_id_available"] = any(bool(row.get("hidden_feedback_id")) for row in rows)
    report["three_dot_menu_found"] = any(bool(row.get("three_dot_menu_found")) for row in rows)
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
    row_ids = [str(row.get("dom_scout_id") or "") for row in rows if row.get("dom_scout_id")]
    if not row_ids:
        report["blocker"] = "No feedback row DOM ids available for complaint modal scout"
        return report

    samples: list[dict[str, Any]] = []
    for dom_id in row_ids[: config.max_modal_reviews]:
        sample = scout_one_complaint_modal(page, dom_id)
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


def scout_one_complaint_modal(page: Page, dom_id: str) -> dict[str, Any]:
    sample: dict[str, Any] = {
        "dom_scout_id": dom_id,
        "menu_opened": False,
        "complaint_action_found": False,
        "opened": False,
        "categories": [],
        "description_fields": [],
        "validation_hints": [],
        "submit_button_seen": False,
        "submit_clicked": False,
        "close_method": "",
        "blocker": "",
    }
    clicked_menu = _click_safe_row_menu(page, dom_id)
    sample["menu_opened"] = bool(clicked_menu.get("ok"))
    if not clicked_menu.get("ok"):
        sample["blocker"] = str(clicked_menu.get("reason") or "safe row menu not found")
        return sample
    _wait_settle(page, 800)
    action = _find_text_locator(page, "Пожаловаться на отзыв")
    if action is None:
        sample["blocker"] = "Пожаловаться на отзыв action not found in row menu"
        _safe_escape(page)
        return sample
    sample["complaint_action_found"] = True
    assert_safe_click_label("Пожаловаться на отзыв", purpose="open_complaint_modal")
    action.click(timeout=5000)
    _wait_settle(page, 1500)
    modal = extract_complaint_modal_state(page)
    sample.update(modal)
    sample["opened"] = bool(modal.get("opened"))
    sample["submit_clicked"] = False
    close_method = close_modal_without_submit(page)
    sample["close_method"] = close_method
    _wait_settle(page, 600)
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
    buttons = [str(item) for item in candidate.get("buttons") or []][:10]
    hidden_feedback_id = _extract_feedback_id(attrs, links, text)
    return {
        "dom_scout_id": str(candidate.get("dom_scout_id") or ""),
        "product_title": _guess_product_title(text),
        "supplier_article": _guess_article(text, label_re=r"артикул\s*продавца"),
        "wb_article": _guess_article(text, label_re=r"артикул\s*(?:wb|вб)?"),
        "nm_id": _guess_article(text, label_re=r"(?:nm\s*id|nmid|артикул\s*(?:wb|вб)?)"),
        "rating": _guess_rating(text),
        "review_date": _guess_date(text),
        "text_snippet": _guess_review_text(text),
        "pros_snippet": _field_after_label(text, ("Достоинства", "Плюсы")),
        "cons_snippet": _field_after_label(text, ("Недостатки", "Минусы")),
        "answer_status": _guess_answer_status(text),
        "media_indicators": _guess_media_indicators(text),
        "three_dot_menu_found": _has_three_dot_button(buttons),
        "hidden_feedback_id": hidden_feedback_id,
        "links": [_safe_url(link) for link in links],
        "data_attributes": attrs,
        "dom_fingerprint": _fingerprint(text),
        "safe_text_fingerprint": _safe_text(_norm_text(text), 420),
        "selector_hints": [str(candidate.get("selector") or "")] if candidate.get("selector") else [],
        "raw_text_lines": _safe_lines(text, 10),
    }


def parse_complaint_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    text = str(candidate.get("text") or "")
    attrs = _safe_mapping(candidate.get("attrs") if isinstance(candidate.get("attrs"), dict) else {})
    decision = _guess_decision(text)
    return {
        "dom_scout_id": str(candidate.get("dom_scout_id") or ""),
        "product_title": _guess_product_title(text),
        "supplier_article": _guess_article(text, label_re=r"артикул\s*продавца"),
        "wb_article": _guess_article(text, label_re=r"артикул\s*(?:wb|вб)?"),
        "nm_id": _guess_article(text, label_re=r"(?:nm\s*id|nmid|артикул\s*(?:wb|вб)?)"),
        "complaint_reason": _field_after_label(text, ("Причина",)),
        "complaint_description": _field_after_label(text, ("Описание",)),
        "review_text_snippet": _field_after_label(text, ("Отзыв",)) or _guess_review_text(text),
        "review_rating": _guess_rating(text),
        "review_date": _guess_date(text),
        "displayed_status": _guess_status(text),
        "decision_label": decision,
        "wb_response_snippet": _field_after_label(text, ("Ответ WB", "Решение", "Комментарий")),
        "row_menu_available": _has_three_dot_button([str(item) for item in candidate.get("buttons") or []]),
        "hidden_ids": _extract_hidden_ids(attrs, [str(item) for item in candidate.get("links") or []], text),
        "data_attributes": attrs,
        "dom_fingerprint": _fingerprint(text),
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
  const labels = Array.from(root.querySelectorAll('label, [role="radio"], [role="option"], li, button, textarea, input')).filter(visible).map((el) => ({
    tag: el.tagName.toLowerCase(),
    role: el.getAttribute('role') || '',
    type: el.getAttribute('type') || '',
    text: (el.innerText || el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('value') || '').trim()
  })).filter((item) => item.text);
  return { opened: candidates.length > 0 || /жалоб/i.test(text), text, labels };
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
    submit_seen = any(SUBMIT_LIKE_RE.search(text) for text in texts)
    return {
        "opened": bool(payload.get("opened")),
        "categories": categories,
        "description_fields": description_fields,
        "validation_hints": validation_hints,
        "submit_button_seen": submit_seen,
        "modal_text_fingerprint": _fingerprint(str(payload.get("text") or "")),
    }


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
    return "not_closed"


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
    if _same_dateish(source_feedback.get("created_date") or source_feedback.get("review_date"), ui_row.get("review_date")):
        score += 0.18
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
    if score >= 0.98:
        status = "exact"
    elif score >= 0.82:
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
        for field in ("text_snippet", "rating", "review_date", "nm_id", "wb_article", "supplier_article", "product_title")
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
            "same_date_or_day": 0.18,
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
        "text_snippet",
        "pros_snippet",
        "cons_snippet",
        "answer_status",
        "media_indicators",
        "hidden_feedback_id",
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
            for key in ("success", "visible_rows_parsed_count", "hidden_feedback_id_available", "three_dot_menu_found", "field_availability", "blocker")
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
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const buttons = Array.from(row.querySelectorAll('button, [role="button"]')).filter(visible);
  const danger = /(ответить|редакт|удал|сохран|отправ|подать)/i;
  const preferred = buttons.find((button) => {
    const label = (button.innerText || button.getAttribute('aria-label') || button.getAttribute('title') || '').trim();
    return !danger.test(label) && /(ещ|ещё|еще|действ|меню|more|⋮|\.\.\.)/i.test(label);
  });
  const iconOnly = buttons.find((button) => {
    const label = (button.innerText || button.getAttribute('aria-label') || button.getAttribute('title') || '').trim();
    return !label && !danger.test(label);
  });
  const target = preferred || iconOnly;
  if (!target) {
    return {ok: false, reason: 'no safe menu button', button_count: buttons.length};
  }
  const label = (target.innerText || target.getAttribute('aria-label') || target.getAttribute('title') || '').trim();
  target.click();
  return {ok: true, label, button_count: buttons.length};
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
            locator = page.get_by_role(role, name=re.compile(re.escape(text), re.IGNORECASE)).first
            if locator.count() and locator.is_visible(timeout=1000):
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
            locator = page.get_by_text(text, exact=exact).first
            if locator.count() and locator.is_visible(timeout=1000):
                return locator
        except PlaywrightError:
            pass
    return None


def _find_text_locator(page: Page, text: str):
    try:
        locator = page.get_by_text(text, exact=True).first
        if locator.count() and locator.is_visible(timeout=1200):
            return locator
    except PlaywrightError:
        pass
    try:
        locator = page.get_by_text(text, exact=False).first
        if locator.count() and locator.is_visible(timeout=1200):
            return locator
    except PlaywrightError:
        pass
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
    return "Отзывы" in text and ("Вопросы" in text or "Мои жалобы" in text or "Пожаловаться" in text)


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
    if not (complaints_pending or complaints_answered):
        risks.append("complaint status rows not observed; status sync may need empty-state handling")
    if any(len(str(row.get("text_snippet") or "")) < 30 for row in feedback_rows):
        risks.append("short review texts can create duplicate/ambiguous matches")
    return risks or ["no major matching risks observed in bounded sample"]


def _guess_product_title(text: str) -> str:
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
                "Причина",
                "Описание",
            )
        )
    ]
    return _safe_text(lines[0], 160) if lines else ""


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


def _same_nonempty(a: Any, b: Any) -> bool:
    return bool(str(a or "").strip() and str(a or "").strip() == str(b or "").strip())


def _same_dateish(a: Any, b: Any) -> bool:
    a_text = str(a or "").strip()
    b_text = str(b or "").strip()
    if not a_text or not b_text:
        return False
    return a_text == b_text or a_text[:10] == b_text[:10]


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
        if node["tag"] in {"button", "a"} and self.stack:
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
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 240 && rect.height > 24;
  };
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
    .map((button) => (button.innerText || button.getAttribute('aria-label') || button.getAttribute('title') || '').trim().slice(0, 120));
  const links = (el) => Array.from(el.querySelectorAll('a[href]')).map((a) => a.href).slice(0, 5);
  const all = Array.from(document.querySelectorAll('article, tr, li, [data-testid], [data-test-id], [data-qa], [role="row"], div'));
  const seen = new Set();
  const rows = [];
  for (const el of all) {
    if (!visible(el)) continue;
    const rect = el.getBoundingClientRect();
    if (rect.height > 900) continue;
    const text = (el.innerText || '').trim();
    const norm = text.replace(/\s+/g, ' ').trim();
    if (norm.length < 35 || norm.length > 2600) continue;
    const feedbackLike = /(Отзыв|Достоин|Недостат|Артикул|Оценка|звезд|★|Пожаловаться)/i.test(norm);
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
      attrs: attrsFor(el),
      buttons: buttonLabels(el),
      links: links(el),
      rect: {width: Math.round(rect.width), height: Math.round(rect.height)}
    });
    if (rows.length >= limit) break;
  }
  return rows;
}
"""


if __name__ == "__main__":
    main()
