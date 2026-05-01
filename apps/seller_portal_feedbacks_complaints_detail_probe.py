"""Read-only Seller Portal probe for My Complaints detail/network fields.

The runner investigates whether Seller Portal exposes a direct feedback/review
identifier or a strong composite match for a previously attempted complaint. It
never opens complaint creation, never clicks final submit and never retries
submission.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlsplit


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import Error as PlaywrightError, Page, Response, sync_playwright  # noqa: E402

from apps.seller_portal_feedbacks_complaints_scout import (  # noqa: E402
    BUSINESS_TZ,
    DEFAULT_START_URL,
    ScoutConfig,
    _click_tab_like,
    _safe_escape,
    _wait_settle,
    check_session,
    extract_visible_complaint_rows,
    field_availability,
    navigate_to_feedbacks_questions,
    parse_complaint_candidate,
)
from apps.seller_portal_feedbacks_complaints_status_sync import (  # noqa: E402
    _match_complaint_row_to_record,
    _weak_complaint_row_to_record,
)
from apps.seller_portal_feedbacks_matching_replay import safe_text  # noqa: E402
from apps.seller_portal_relogin_session import DEFAULT_STORAGE_STATE_PATH, DEFAULT_WB_BOT_PYTHON  # noqa: E402
from packages.application.sheet_vitrina_v1_feedbacks_complaints import (  # noqa: E402
    COMPLAINT_STATUS_LABELS,
    JsonFileFeedbacksComplaintJournal,
)


CONTRACT_NAME = "seller_portal_feedbacks_complaints_detail_probe"
CONTRACT_VERSION = "read_only_v1"
READ_ONLY_MODE = "read-only"
DEFAULT_RUNTIME_DIR = Path(os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR", "/opt/wb-core-runtime/state"))
DEFAULT_OUTPUT_ROOT = Path("/opt/wb-core-runtime/state/feedbacks_complaints_detail_probe")
LOCAL_OUTPUT_ROOT = Path("artifacts/seller_portal_feedbacks_complaints_detail_probe")
TARGET_LAST_ERROR = "detail/network probe unconfirmed: no direct feedback_id/complaint_id or strong composite match in Мои жалобы"
RELEVANT_URL_RE = re.compile(r"(complaint|claim|appeal|feedback|review|жалоб)", re.IGNORECASE)
FORBIDDEN_HEADER_RE = re.compile(r"(authorization|authorize|token|cookie|secret|key|session)", re.IGNORECASE)


@dataclass(frozen=True)
class DetailProbeConfig:
    feedback_id: str
    mode: str
    runtime_dir: Path
    storage_state_path: Path
    wb_bot_python: Path
    output_dir: Path
    start_url: str
    max_pending_rows: int
    max_answered_rows: int
    open_row_details: bool
    capture_network: bool
    headless: bool
    timeout_ms: int
    write_artifacts: bool
    update_journal: bool


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feedback-id", required=True)
    parser.add_argument("--mode", choices=(READ_ONLY_MODE,), default=READ_ONLY_MODE)
    parser.add_argument("--runtime-dir", default=str(DEFAULT_RUNTIME_DIR if DEFAULT_RUNTIME_DIR.exists() else ".runtime"))
    parser.add_argument("--storage-state-path", default=str(DEFAULT_STORAGE_STATE_PATH))
    parser.add_argument("--wb-bot-python", default=str(DEFAULT_WB_BOT_PYTHON))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--start-url", default=DEFAULT_START_URL)
    parser.add_argument("--max-pending-rows", type=int, default=50)
    parser.add_argument("--max-answered-rows", type=int, default=50)
    parser.add_argument("--open-row-details", choices=("0", "1"), default="1")
    parser.add_argument("--capture-network", choices=("0", "1"), default="1")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--timeout-ms", type=int, default=20000)
    parser.add_argument("--no-artifacts", action="store_true")
    parser.add_argument("--no-journal-update", action="store_true")
    args = parser.parse_args()

    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else (DEFAULT_OUTPUT_ROOT if Path("/opt/wb-core-runtime/state").exists() else LOCAL_OUTPUT_ROOT)
    )
    config = DetailProbeConfig(
        feedback_id=str(args.feedback_id).strip(),
        mode=args.mode,
        runtime_dir=Path(args.runtime_dir).expanduser(),
        storage_state_path=Path(args.storage_state_path).expanduser(),
        wb_bot_python=Path(args.wb_bot_python).expanduser(),
        output_dir=output_dir,
        start_url=str(args.start_url).rstrip("/") or DEFAULT_START_URL,
        max_pending_rows=max(1, int(args.max_pending_rows)),
        max_answered_rows=max(1, int(args.max_answered_rows)),
        open_row_details=str(args.open_row_details) == "1",
        capture_network=str(args.capture_network) == "1",
        headless=not args.headed,
        timeout_ms=max(5000, int(args.timeout_ms)),
        write_artifacts=not bool(args.no_artifacts),
        update_journal=not bool(args.no_journal_update),
    )
    report = run_detail_probe(config)
    if config.write_artifacts:
        paths = write_report_artifacts(report, config.output_dir)
        report["artifact_paths"] = {key: str(path) for key, path in paths.items()}
    print(json.dumps(compact_stdout_report(report), ensure_ascii=False, indent=2))


def run_detail_probe(config: DetailProbeConfig) -> dict[str, Any]:
    if config.mode != READ_ONLY_MODE:
        raise RuntimeError("complaints detail probe supports read-only mode only")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    journal = JsonFileFeedbacksComplaintJournal(config.runtime_dir)
    journal_before = journal.find_by_feedback_id(config.feedback_id)
    report: dict[str, Any] = {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "run_id": run_id,
        "started_at": iso_now(),
        "finished_at": None,
        "parameters": {
            "feedback_id": config.feedback_id,
            "mode": config.mode,
            "max_pending_rows": config.max_pending_rows,
            "max_answered_rows": config.max_answered_rows,
            "open_row_details": config.open_row_details,
            "capture_network": config.capture_network,
            "journal_update_enabled": config.update_journal,
        },
        "read_only_guards": {
            "seller_portal_write_actions_allowed": False,
            "complaint_creation_modal_allowed": False,
            "complaint_submission_allowed": False,
            "retry_submit_allowed": False,
            "final_submit_click_allowed": False,
            "complaint_action_clicked": False,
            "submit_clicked_during_runner": 0,
        },
        "journal_before": dict(journal_before or {}),
        "session": {},
        "navigation": {},
        "my_complaints": empty_my_complaints_probe(),
        "network": {"responses": [], "endpoints_observed": [], "rows_captured_count": 0},
        "confirmation": empty_confirmation(),
        "journal_update": {"applied": False},
        "journal_after": {},
        "errors": [],
    }
    if not journal_before:
        report["errors"].append({"stage": "journal", "code": "missing_record", "message": "feedback_id is not present in complaint journal"})
        report["confirmation"]["reason"] = "journal record is missing"
        report["finished_at"] = iso_now()
        return report

    scout_config = build_scout_config(config)
    session = check_session(scout_config)
    report["session"] = session
    if not session.get("ok"):
        report["errors"].append({"stage": "session", "code": str(session.get("status") or ""), "message": str(session.get("message") or "")})
        report["confirmation"]["reason"] = "Seller Portal session is not valid"
        apply_probe_journal_result(config, journal, report, run_id)
        report["finished_at"] = iso_now()
        return report

    captured: list[dict[str, Any]] = []
    current_stage = {"value": "before_my_complaints"}
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
            if config.capture_network:
                page.on(
                    "response",
                    lambda response: capture_sanitized_response(
                        response,
                        captured=captured,
                        stage=current_stage["value"],
                        target_feedback_id=config.feedback_id,
                    ),
                )
            try:
                navigation = navigate_to_feedbacks_questions(page, scout_config)
                report["navigation"] = navigation
                if not navigation.get("success"):
                    report["errors"].append({"stage": "navigation", "code": "not_reached", "message": str(navigation.get("blocker") or "")})
                else:
                    report["my_complaints"] = inspect_my_complaints_details(page, config, current_stage)
            finally:
                context.close()
                browser.close()
    except Exception as exc:  # pragma: no cover - live fallback
        report["errors"].append({"stage": "browser_detail_probe", "code": exc.__class__.__name__, "message": safe_text(str(exc), 800)})

    report["network"] = summarize_network_capture(captured)
    report["confirmation"] = evaluate_detail_probe_confirmation(journal_before, report["my_complaints"], captured)
    apply_probe_journal_result(config, journal, report, run_id)
    report["journal_after"] = dict(journal.find_by_feedback_id(config.feedback_id) or {})
    report["finished_at"] = iso_now()
    return report


def build_scout_config(config: DetailProbeConfig) -> ScoutConfig:
    return ScoutConfig(
        mode="scout-complaints",
        storage_state_path=config.storage_state_path,
        wb_bot_python=config.wb_bot_python,
        output_root=config.output_dir,
        start_url=config.start_url,
        max_feedback_rows=1,
        max_complaint_rows=max(config.max_pending_rows, config.max_answered_rows),
        max_modal_reviews=0,
        open_complaint_modal=False,
        headless=config.headless,
        timeout_ms=config.timeout_ms,
        write_artifacts=False,
    )


def inspect_my_complaints_details(page: Page, config: DetailProbeConfig, current_stage: dict[str, str]) -> dict[str, Any]:
    report = empty_my_complaints_probe()
    if not _click_tab_like(page, "Мои жалобы"):
        report["blocker"] = "Мои жалобы tab was not found"
        return report
    _wait_settle(page, 2500)
    for tab_label, key, limit in (
        ("Ждут ответа", "pending", config.max_pending_rows),
        ("Есть ответ", "answered", config.max_answered_rows),
    ):
        current_stage["value"] = f"{key}_list"
        clicked = _click_tab_like(page, tab_label)
        _wait_settle(page, 2200)
        rows = extract_visible_complaint_rows(page, max_rows=limit)
        details: list[dict[str, Any]] = []
        if config.open_row_details:
            for index, row in enumerate(rows[:limit]):
                current_stage["value"] = f"{key}_detail"
                details.append(open_complaint_row_detail(page, row, tab=key, row_index=index))
                current_stage["value"] = f"{key}_list"
        report[key] = {
            "tab_clicked": clicked,
            "visible_rows": len(rows),
            "rows": rows,
            "details": details,
            "details_opened": sum(1 for item in details if item.get("opened")),
            "field_availability": field_availability([*rows, *[item.get("parsed_row") or {} for item in details]]),
        }
    report["pending_count_visible"] = int((report["pending"] or {}).get("visible_rows") or 0)
    report["answered_count_visible"] = int((report["answered"] or {}).get("visible_rows") or 0)
    report["details_opened_count"] = int((report["pending"] or {}).get("details_opened") or 0) + int(
        (report["answered"] or {}).get("details_opened") or 0
    )
    report["success"] = True
    return report


def open_complaint_row_detail(page: Page, row: Mapping[str, Any], *, tab: str, row_index: int) -> dict[str, Any]:
    dom_id = str(row.get("dom_scout_id") or "")
    result: dict[str, Any] = {
        "tab": tab,
        "row_index": row_index,
        "dom_scout_id": dom_id,
        "opened": False,
        "method": "",
        "detail_url_path": "",
        "parsed_row": {},
        "raw_text_lines": [],
        "close_method": "",
        "blocker": "",
    }
    if not dom_id:
        result["blocker"] = "row DOM id unavailable"
        return result
    before_url = page.url
    try:
        locator = page.locator(f'[data-wb-core-scout-id="{dom_id}"]').first
        locator.scroll_into_view_if_needed(timeout=3000)
        locator.click(timeout=5000, position={"x": 24, "y": 18})
        _wait_settle(page, 1400)
    except PlaywrightError as exc:
        result["blocker"] = f"row detail click failed: {safe_text(str(exc), 220)}"
        return result
    detail = extract_detail_panel(page)
    if detail.get("found"):
        parsed = parse_complaint_candidate(
            {
                "text": str(detail.get("text") or ""),
                "attrs": detail.get("attrs") or {},
                "buttons": detail.get("buttons") or [],
                "links": detail.get("links") or [],
                "dom_scout_id": f"{dom_id}-detail",
                "selector": str(detail.get("selector") or ""),
            }
        )
        result.update(
            {
                "opened": True,
                "method": "detail_panel",
                "parsed_row": parsed,
                "raw_text_lines": parsed.get("raw_text_lines") or [],
                "detail_url_path": safe_url_path(page.url),
            }
        )
    elif page.url != before_url:
        body_text = safe_body_text(page)
        parsed = parse_complaint_candidate(
            {
                "text": body_text,
                "attrs": {},
                "buttons": [],
                "links": [],
                "dom_scout_id": f"{dom_id}-detail-page",
                "selector": "document-body",
            }
        )
        result.update(
            {
                "opened": True,
                "method": "detail_navigation",
                "parsed_row": parsed,
                "raw_text_lines": parsed.get("raw_text_lines") or [],
                "detail_url_path": safe_url_path(page.url),
            }
        )
    else:
        result["blocker"] = "row click did not expose a readable detail panel"
    result["close_method"] = close_detail_view(page, before_url=before_url)
    _wait_settle(page, 500)
    return result


def extract_detail_panel(page: Page) -> dict[str, Any]:
    try:
        return page.evaluate(
            r"""
() => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 260 && rect.height > 120;
  };
  const attrsFor = (el) => {
    const attrs = {};
    for (const attr of Array.from(el.attributes || [])) {
      if (/^(data-|aria-|role|class|id$)/i.test(attr.name)) attrs[attr.name] = String(attr.value || '').slice(0, 180);
    }
    return attrs;
  };
  const textOf = (el) => String(el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const candidates = Array.from(document.querySelectorAll('[role="dialog"], [class*="Modal"], [class*="modal"], [class*="Drawer"], [class*="drawer"], [data-testid*="modal"], [data-testid*="drawer"], aside'))
    .filter(visible)
    .map((el) => {
      const rect = el.getBoundingClientRect();
      const text = textOf(el);
      return {
        el,
        text,
        area: rect.width * rect.height,
        attrs: attrsFor(el),
        buttons: Array.from(el.querySelectorAll('button,[role="button"]')).map(textOf).filter(Boolean).slice(0, 20),
        links: Array.from(el.querySelectorAll('a[href]')).map((a) => String(a.href || '')).slice(0, 10),
        selector: el.getAttribute('data-testid') || el.getAttribute('role') || el.className || el.tagName
      };
    })
    .filter((item) => item.text.length > 40 && /(жалоб|отзыв|причин|описан|решени|статус|товар)/i.test(item.text))
    .sort((a, b) => b.area - a.area);
  const best = candidates[0];
  if (!best) return {found: false};
  return {
    found: true,
    text: best.text.slice(0, 5000),
    attrs: best.attrs,
    buttons: best.buttons,
    links: best.links,
    selector: String(best.selector || '').slice(0, 180)
  };
}
            """
        )
    except PlaywrightError:
        return {"found": False}


def close_detail_view(page: Page, *, before_url: str) -> str:
    if page.url != before_url:
        try:
            page.go_back(wait_until="domcontentloaded", timeout=5000)
            return "browser_back"
        except PlaywrightError:
            pass
    _safe_escape(page)
    _wait_settle(page, 300)
    try:
        clicked = page.evaluate(
            r"""
() => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const bad = /(отправить|подать|пожаловаться|сохранить|submit|send|save)/i;
  const good = /(закрыть|close|назад|×|✕)/i;
  const buttons = Array.from(document.querySelectorAll('button,[role="button"],[aria-label]')).filter(visible);
  for (const button of buttons) {
    const label = String(button.innerText || button.textContent || button.getAttribute('aria-label') || '').trim();
    if (!label || bad.test(label) || !good.test(label)) continue;
    button.click();
    return label.slice(0, 80);
  }
  return '';
}
            """
        )
        if clicked:
            return f"safe_close_button:{safe_text(clicked, 80)}"
    except PlaywrightError:
        pass
    return "escape"


def capture_sanitized_response(
    response: Response,
    *,
    captured: list[dict[str, Any]],
    stage: str,
    target_feedback_id: str,
) -> None:
    if len(captured) >= 80:
        return
    item = sanitize_network_response(response, stage=stage, target_feedback_id=target_feedback_id)
    if item:
        captured.append(item)


def sanitize_network_response(response: Response, *, stage: str, target_feedback_id: str) -> dict[str, Any]:
    try:
        url = response.url
        status = response.status
        content_type = str(response.headers.get("content-type") or "")
        method = str(response.request.method or "")
    except Exception:
        return {}
    url_lower = url.lower()
    payload: Any = None
    text = ""
    if "json" in content_type:
        try:
            payload = response.json()
            text = json.dumps(payload, ensure_ascii=False)[:200000]
        except Exception:
            payload = None
            text = ""
    if not payload and not RELEVANT_URL_RE.search(url_lower):
        return {}
    target_seen = bool(target_feedback_id and target_feedback_id in text)
    safe_rows = extract_safe_network_rows(payload, target_feedback_id=target_feedback_id)
    if not safe_rows and not target_seen and not RELEVANT_URL_RE.search(url_lower):
        return {}
    split = urlsplit(url)
    query_keys = sorted({key for key, _value in parse_qsl(split.query, keep_blank_values=True) if not FORBIDDEN_HEADER_RE.search(key)})[:20]
    return {
        "stage": stage,
        "method": method,
        "status": status,
        "url_path": safe_text(f"{split.scheme}://{split.netloc}{split.path}", 260),
        "query_keys": query_keys,
        "content_type": safe_text(content_type, 120),
        "complaint_like_url": bool(RELEVANT_URL_RE.search(url_lower)),
        "target_feedback_id_seen": target_seen,
        "payload_shape": payload_shape(payload),
        "safe_rows": safe_rows[:25],
        "safe_row_count": len(safe_rows),
    }


def extract_safe_network_rows(payload: Any, *, target_feedback_id: str = "") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for mapping in iter_relevant_mappings(payload):
        fact = safe_fact_from_mapping(mapping)
        if not fact:
            continue
        if target_feedback_id and (
            fact.get("feedback_id") == target_feedback_id or fact.get("review_id") == target_feedback_id
        ):
            fact["target_feedback_id_match"] = True
        key = json.dumps(fact, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        rows.append(fact)
        if len(rows) >= 80:
            break
    return rows


def iter_relevant_mappings(payload: Any) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 7 or len(found) >= 160:
            return
        if isinstance(value, Mapping):
            if mapping_looks_relevant(value):
                found.append(value)
            for nested in value.values():
                if isinstance(nested, (Mapping, list)):
                    walk(nested, depth + 1)
        elif isinstance(value, list):
            for item in value[:300]:
                if isinstance(item, (Mapping, list)):
                    walk(item, depth + 1)

    walk(payload)
    return found


def mapping_looks_relevant(mapping: Mapping[str, Any]) -> bool:
    keys = {str(key).lower() for key in mapping.keys()}
    joined = " ".join(keys)
    if any(token in joined for token in ("feedback", "review", "complaint", "claim", "appeal", "reason", "decision", "status")):
        return True
    text = " ".join(str(value)[:160] for value in mapping.values() if isinstance(value, (str, int, float)))
    return bool(re.search(r"(жалоб|отзыв|удовлетвор|отклон|другое)", text, re.IGNORECASE))


def safe_fact_from_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    fact = {
        "complaint_id": safe_text(deep_find_value(mapping, ("complaintId", "complaint_id", "claimId", "claim_id", "appealId", "appeal_id")), 120),
        "feedback_id": safe_text(deep_find_value(mapping, ("feedbackId", "feedback_id", "feedbackID", "sellerPortalFeedbackId")), 120),
        "review_id": safe_text(deep_find_value(mapping, ("reviewId", "review_id", "reviewID")), 120),
        "nm_id": safe_text(deep_find_value(mapping, ("nmId", "nmID", "wbArticle", "wb_article", "nm_id")), 80),
        "supplier_article": safe_text(deep_find_value(mapping, ("supplierArticle", "supplier_article", "vendorArticle", "vendor_article")), 160),
        "product_title": safe_text(deep_find_value(mapping, ("productName", "product_name", "name", "title")), 220),
        "review_text_snippet": safe_text(deep_find_value(mapping, ("feedbackText", "reviewText", "review_text", "text")), 260),
        "complaint_reason": safe_text(deep_find_value(mapping, ("complaintReason", "complaint_reason", "reasonName", "categoryName", "category", "reason")), 180),
        "complaint_description": safe_text(deep_find_value(mapping, ("complaintText", "complaint_text", "description", "message", "comment")), 320),
        "status_text": safe_text(deep_find_value(mapping, ("status", "statusName", "state", "decision", "decisionText")), 160),
        "created_at": safe_text(deep_find_value(mapping, ("createdAt", "created_at", "createdDate", "dt")), 120),
    }
    return {key: value for key, value in fact.items() if value}


def deep_find_value(value: Any, names: tuple[str, ...]) -> str:
    wanted = {name.lower() for name in names}
    stack: list[Any] = [value]
    while stack:
        current = stack.pop(0)
        if isinstance(current, Mapping):
            for key, item in current.items():
                key_text = str(key)
                if key_text.lower() in wanted and isinstance(item, (str, int, float)):
                    return str(item)
                if isinstance(item, (Mapping, list)):
                    stack.append(item)
        elif isinstance(current, list):
            stack.extend(item for item in current[:50] if isinstance(item, (Mapping, list)))
    return ""


def payload_shape(payload: Any) -> dict[str, Any]:
    if isinstance(payload, Mapping):
        return {"type": "dict", "keys": sorted(str(key) for key in payload.keys())[:30]}
    if isinstance(payload, list):
        return {"type": "list", "length": len(payload)}
    if payload is None:
        return {"type": "none"}
    return {"type": type(payload).__name__}


def summarize_network_capture(captured: list[dict[str, Any]]) -> dict[str, Any]:
    endpoints: dict[str, dict[str, Any]] = {}
    row_count = 0
    for item in captured:
        path = str(item.get("url_path") or "")
        row_count += int(item.get("safe_row_count") or 0)
        endpoint = endpoints.setdefault(
            path,
            {
                "url_path": path,
                "methods": [],
                "statuses": [],
                "stages": [],
                "response_count": 0,
                "safe_row_count": 0,
                "target_feedback_id_seen": False,
            },
        )
        endpoint["response_count"] += 1
        endpoint["safe_row_count"] += int(item.get("safe_row_count") or 0)
        endpoint["target_feedback_id_seen"] = bool(endpoint["target_feedback_id_seen"] or item.get("target_feedback_id_seen"))
        for key, source in (("methods", item.get("method")), ("statuses", item.get("status")), ("stages", item.get("stage"))):
            if source not in endpoint[key]:
                endpoint[key].append(source)
    return {
        "responses": captured,
        "endpoints_observed": list(endpoints.values()),
        "rows_captured_count": row_count,
        "direct_feedback_id_found": any(
            row.get("target_feedback_id_match")
            for item in captured
            for row in item.get("safe_rows") or []
            if isinstance(row, Mapping)
        ),
        "complaint_id_found": any(
            bool(row.get("complaint_id"))
            for item in captured
            for row in item.get("safe_rows") or []
            if isinstance(row, Mapping)
        ),
    }


def evaluate_detail_probe_confirmation(
    record: Mapping[str, Any],
    my_complaints: Mapping[str, Any],
    network_items: list[Mapping[str, Any]],
) -> dict[str, Any]:
    result = empty_confirmation()
    rows_with_tabs: list[tuple[str, dict[str, Any], str]] = []
    for tab in ("pending", "answered"):
        section = my_complaints.get(tab) if isinstance(my_complaints.get(tab), Mapping) else {}
        for row in section.get("rows") or []:
            if isinstance(row, Mapping):
                rows_with_tabs.append((tab, dict(row), "dom_list"))
        for detail in section.get("details") or []:
            parsed = detail.get("parsed_row") if isinstance(detail, Mapping) and isinstance(detail.get("parsed_row"), Mapping) else {}
            if parsed:
                rows_with_tabs.append((tab, dict(parsed), "dom_detail"))
    for item in network_items:
        stage = str(item.get("stage") or "")
        tab = "answered" if "answered" in stage else "pending" if "pending" in stage else ""
        if not tab:
            continue
        for row in item.get("safe_rows") or []:
            if isinstance(row, Mapping):
                rows_with_tabs.append((tab, network_fact_to_complaint_row(row), "network"))

    for tab, row, source in rows_with_tabs:
        direct_id = str(row.get("feedback_id") or row.get("review_id") or (row.get("hidden_ids") or {}).get("feedback_id") or "").strip()
        if direct_id and direct_id == str(record.get("feedback_id") or ""):
            result["direct_id_candidates"].append(build_probe_match_summary(tab, row, record, source=source, reason="feedback_id"))
            continue
        matched = _match_complaint_row_to_record(row, [record])
        if matched and matched.get("kind") == "direct_id":
            result["direct_id_candidates"].append(build_probe_match_summary(tab, row, record, source=source, reason=str(matched.get("reason") or "feedback_id"), score=matched.get("score")))
            continue
        if matched and matched.get("kind") == "strong_composite":
            result["strong_composite_candidates"].append(
                build_probe_match_summary(tab, row, record, source=source, reason=str(matched.get("reason") or "strong_composite"), score=matched.get("score"))
            )
            continue
        if _weak_complaint_row_to_record(row, [record]):
            result["weak_rejected_candidates"].append(build_probe_match_summary(tab, row, record, source=source, reason="weak_only"))

    chosen = choose_probe_match(result)
    if chosen:
        status = str(chosen.get("status") or "error")
        result.update(
            {
                "result": confirmation_result_for_status(status),
                "status": status,
                "status_label": COMPLAINT_STATUS_LABELS.get(status, COMPLAINT_STATUS_LABELS["error"]),
                "reason": str(chosen.get("reason") or chosen.get("source") or "confirmed"),
            }
        )
    else:
        result["reason"] = "no direct feedback_id/complaint_id or strong composite match in Мои жалобы"
    return result


def network_fact_to_complaint_row(fact: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "feedback_id": str(fact.get("feedback_id") or ""),
        "review_id": str(fact.get("review_id") or ""),
        "complaint_id": str(fact.get("complaint_id") or ""),
        "product_title": str(fact.get("product_title") or ""),
        "supplier_article": str(fact.get("supplier_article") or ""),
        "nm_id": str(fact.get("nm_id") or ""),
        "review_text_snippet": str(fact.get("review_text_snippet") or ""),
        "complaint_reason": str(fact.get("complaint_reason") or ""),
        "complaint_description": str(fact.get("complaint_description") or ""),
        "displayed_status": str(fact.get("status_text") or ""),
        "decision_label": decision_label_from_status_text(fact.get("status_text")),
        "hidden_ids": {"feedback_id": str(fact.get("feedback_id") or "")} if fact.get("feedback_id") else {},
    }


def build_probe_match_summary(
    tab: str,
    row: Mapping[str, Any],
    record: Mapping[str, Any],
    *,
    source: str,
    reason: str,
    score: Any = "",
) -> dict[str, Any]:
    status = status_for_tab(tab, row.get("decision_label") or row.get("displayed_status"))
    return {
        "source": source,
        "tab": tab,
        "status": status,
        "status_label": COMPLAINT_STATUS_LABELS.get(status, COMPLAINT_STATUS_LABELS["error"]),
        "reason": reason,
        "score": score,
        "row_summary": {
            "complaint_id": safe_text(str(row.get("complaint_id") or ""), 120),
            "feedback_id": safe_text(str(row.get("feedback_id") or row.get("review_id") or ""), 120),
            "product_title": safe_text(str(row.get("product_title") or ""), 180),
            "supplier_article": safe_text(str(row.get("supplier_article") or ""), 120),
            "nm_id": safe_text(str(row.get("nm_id") or row.get("wb_article") or ""), 80),
            "complaint_reason": safe_text(str(row.get("complaint_reason") or ""), 180),
            "complaint_description": safe_text(str(row.get("complaint_description") or ""), 260),
            "review_text_snippet": safe_text(str(row.get("review_text_snippet") or ""), 260),
            "decision_label": safe_text(str(row.get("decision_label") or ""), 80),
            "displayed_status": safe_text(str(row.get("displayed_status") or ""), 80),
        },
    }


def choose_probe_match(result: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for key in ("direct_id_candidates", "strong_composite_candidates"):
        matches = [item for item in result.get(key) or [] if isinstance(item, Mapping)]
        if matches:
            matches.sort(key=lambda item: status_rank(str(item.get("status") or "")), reverse=True)
            return matches[0]
    return None


def apply_probe_journal_result(
    config: DetailProbeConfig,
    journal: JsonFileFeedbacksComplaintJournal,
    report: dict[str, Any],
    run_id: str,
) -> None:
    if not config.update_journal:
        report["journal_update"] = {"applied": False, "reason": "disabled"}
        return
    confirmation = report.get("confirmation") or {}
    status = str(confirmation.get("status") or "error")
    if status == "error":
        last_error = TARGET_LAST_ERROR
    else:
        last_error = ""
    updated = journal.update_status(
        config.feedback_id,
        status=status,
        raw_status_text=str(confirmation.get("result") or ""),
        wb_decision_text=str(confirmation.get("status_label") or ""),
        status_sync_run_id=run_id,
        last_error=last_error if status == "error" else "",
    )
    report["journal_update"] = {
        "applied": bool(updated),
        "status": status,
        "status_label": COMPLAINT_STATUS_LABELS.get(status, COMPLAINT_STATUS_LABELS["error"]),
        "last_error": last_error,
    }


def empty_my_complaints_probe() -> dict[str, Any]:
    return {
        "success": False,
        "pending_count_visible": 0,
        "answered_count_visible": 0,
        "details_opened_count": 0,
        "pending": {"tab_clicked": False, "visible_rows": 0, "rows": [], "details": [], "details_opened": 0, "field_availability": {}},
        "answered": {"tab_clicked": False, "visible_rows": 0, "rows": [], "details": [], "details_opened": 0, "field_availability": {}},
        "blocker": "",
    }


def empty_confirmation() -> dict[str, Any]:
    return {
        "direct_id_candidates": [],
        "strong_composite_candidates": [],
        "weak_rejected_candidates": [],
        "result": "unconfirmed",
        "status": "error",
        "status_label": COMPLAINT_STATUS_LABELS["error"],
        "reason": "",
    }


def status_for_tab(tab: str, status_text: Any = "") -> str:
    if tab == "pending":
        return "waiting_response"
    if tab == "answered":
        decision = decision_label_from_status_text(status_text)
        if decision == "approved":
            return "satisfied"
        if decision == "rejected":
            return "rejected"
        return "error"
    return "error"


def decision_label_from_status_text(status_text: Any) -> str:
    text = str(status_text or "").lower()
    if any(token in text for token in ("approved", "удовлетвор", "одобрен", "принят")):
        return "approved"
    if any(token in text for token in ("rejected", "отклон", "отказ")):
        return "rejected"
    return str(status_text or "")


def confirmation_result_for_status(status: str) -> str:
    if status == "waiting_response":
        return "confirmed_pending"
    if status == "satisfied":
        return "confirmed_satisfied"
    if status == "rejected":
        return "confirmed_rejected"
    return "unconfirmed"


def status_rank(status: str) -> int:
    return {"satisfied": 3, "rejected": 3, "waiting_response": 2, "error": 0}.get(status, 0)


def safe_url_path(url: str) -> str:
    split = urlsplit(str(url or ""))
    return safe_text(f"{split.scheme}://{split.netloc}{split.path}", 260)


def safe_body_text(page: Page) -> str:
    try:
        return safe_text(page.locator("body").inner_text(timeout=2000), 5000)
    except PlaywrightError:
        return ""


def write_report_artifacts(report: Mapping[str, Any], output_root: Path) -> dict[str, Path]:
    run_dir = output_root / str(report.get("run_id") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    run_dir.mkdir(parents=True, exist_ok=True)
    json_path = run_dir / "seller_portal_feedbacks_complaints_detail_probe.json"
    md_path = run_dir / "seller_portal_feedbacks_complaints_detail_probe.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown_report(report), encoding="utf-8")
    return {"json": json_path, "markdown": md_path}


def render_markdown_report(report: Mapping[str, Any]) -> str:
    confirmation = report.get("confirmation") or {}
    my = report.get("my_complaints") or {}
    network = report.get("network") or {}
    lines = [
        "# Seller Portal Complaints Detail Probe",
        "",
        f"- Feedback ID: `{(report.get('parameters') or {}).get('feedback_id')}`",
        f"- Started: `{report.get('started_at')}`",
        f"- Finished: `{report.get('finished_at')}`",
        f"- Session: `{(report.get('session') or {}).get('status')}`",
        f"- Pending rows read: `{my.get('pending_count_visible', 0)}`",
        f"- Answered rows read: `{my.get('answered_count_visible', 0)}`",
        f"- Details opened: `{my.get('details_opened_count', 0)}`",
        f"- Endpoints observed: `{len(network.get('endpoints_observed') or [])}`",
        f"- Network rows captured: `{network.get('rows_captured_count', 0)}`",
        f"- Direct candidates: `{len(confirmation.get('direct_id_candidates') or [])}`",
        f"- Strong composite candidates: `{len(confirmation.get('strong_composite_candidates') or [])}`",
        f"- Weak candidates rejected: `{len(confirmation.get('weak_rejected_candidates') or [])}`",
        f"- Result: `{confirmation.get('result')}`",
        f"- Journal update applied: `{(report.get('journal_update') or {}).get('applied')}`",
        f"- Submit clicked during runner: `{(report.get('read_only_guards') or {}).get('submit_clicked_during_runner')}`",
    ]
    if report.get("errors"):
        lines.extend(["", "## Errors", ""])
        for error in report["errors"]:
            lines.append(f"- `{error.get('stage')}` / `{error.get('code')}`: {error.get('message')}")
    return "\n".join(lines) + "\n"


def compact_stdout_report(report: Mapping[str, Any]) -> dict[str, Any]:
    confirmation = report.get("confirmation") or {}
    return {
        "contract_name": report.get("contract_name"),
        "run_id": report.get("run_id"),
        "feedback_id": (report.get("parameters") or {}).get("feedback_id"),
        "session": report.get("session"),
        "my_complaints": {
            "pending_count_visible": (report.get("my_complaints") or {}).get("pending_count_visible"),
            "answered_count_visible": (report.get("my_complaints") or {}).get("answered_count_visible"),
            "details_opened_count": (report.get("my_complaints") or {}).get("details_opened_count"),
        },
        "network": {
            "endpoints_observed": len((report.get("network") or {}).get("endpoints_observed") or []),
            "rows_captured_count": (report.get("network") or {}).get("rows_captured_count"),
            "direct_feedback_id_found": (report.get("network") or {}).get("direct_feedback_id_found"),
            "complaint_id_found": (report.get("network") or {}).get("complaint_id_found"),
        },
        "confirmation": confirmation,
        "journal_update": report.get("journal_update"),
        "journal_after": report.get("journal_after"),
        "read_only_guards": report.get("read_only_guards"),
        "artifact_paths": report.get("artifact_paths"),
        "errors": report.get("errors"),
    }


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    main()
