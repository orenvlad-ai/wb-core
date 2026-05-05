"""Read-only confirmation runner for one Seller Portal complaint attempt.

The runner checks whether a previous controlled submit attempt is visible in
Seller Portal. It never opens the final complaint submit path and never retries
submission.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import Page, Response, sync_playwright  # noqa: E402

from apps.seller_portal_feedbacks_complaint_dry_run_plan import (  # noqa: E402
    apply_article_search_for_candidate,
    description_persistence_result,
    find_visible_actionable_row,
)
from apps.seller_portal_feedbacks_complaints_scout import (  # noqa: E402
    BUSINESS_TZ,
    DEFAULT_START_URL,
    ScoutConfig,
    _click_safe_row_menu,
    _click_tab_like,
    _safe_escape,
    _wait_for_feedback_rows,
    _wait_settle,
    check_session,
    extract_open_row_menu_state,
    extract_visible_complaint_rows,
    extract_visible_feedback_rows,
    field_availability,
    navigate_to_feedbacks_questions,
)
from apps.seller_portal_feedbacks_complaints_status_sync import (  # noqa: E402
    _match_complaint_row_to_record,
    _record_review_text,
    _strong_text_match,
    _weak_complaint_row_to_record,
)
from apps.seller_portal_feedbacks_matching_replay import (  # noqa: E402
    NO_SUBMIT_MODE,
    ReplayConfig,
    capture_seller_portal_feedback_headers,
    collect_feedback_rows_from_seller_portal_network,
    normalize_date_key,
    normalize_rating,
    safe_text,
    summarize_ui_row,
)
from apps.seller_portal_relogin_session import DEFAULT_STORAGE_STATE_PATH, DEFAULT_WB_BOT_PYTHON  # noqa: E402
from packages.application.sheet_vitrina_v1_feedbacks_complaints import (  # noqa: E402
    COMPLAINT_STATUS_LABELS,
    JsonFileFeedbacksComplaintJournal,
)


CONTRACT_NAME = "seller_portal_feedbacks_complaint_confirmation"
CONTRACT_VERSION = "read_only_v1"
READ_ONLY_MODE = "read-only"
DEFAULT_RUNTIME_DIR = Path(os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR", "/opt/wb-core-runtime/state"))
DEFAULT_OUTPUT_ROOT = Path("/opt/wb-core-runtime/state/feedbacks_complaint_confirmation")
LOCAL_OUTPUT_ROOT = Path("artifacts/seller_portal_feedbacks_complaint_confirmation")


@dataclass(frozen=True)
class ConfirmationConfig:
    feedback_id: str
    mode: str
    runtime_dir: Path
    storage_state_path: Path
    wb_bot_python: Path
    output_dir: Path
    start_url: str
    max_complaint_rows: int
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
    parser.add_argument("--max-complaint-rows", type=int, default=120)
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
    config = ConfirmationConfig(
        feedback_id=str(args.feedback_id).strip(),
        mode=args.mode,
        runtime_dir=Path(args.runtime_dir).expanduser(),
        storage_state_path=Path(args.storage_state_path).expanduser(),
        wb_bot_python=Path(args.wb_bot_python).expanduser(),
        output_dir=output_dir,
        start_url=str(args.start_url).rstrip("/") or DEFAULT_START_URL,
        max_complaint_rows=max(1, int(args.max_complaint_rows)),
        headless=not args.headed,
        timeout_ms=max(5000, int(args.timeout_ms)),
        write_artifacts=not bool(args.no_artifacts),
        update_journal=not bool(args.no_journal_update),
    )
    report = run_confirmation(config)
    if config.write_artifacts:
        paths = write_report_artifacts(report, config.output_dir)
        report["artifact_paths"] = {key: str(path) for key, path in paths.items()}
    print(json.dumps(compact_stdout_report(report), ensure_ascii=False, indent=2))


def run_confirmation(config: ConfirmationConfig) -> dict[str, Any]:
    if config.mode != READ_ONLY_MODE:
        raise RuntimeError("complaint confirmation supports read-only mode only")
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
            "max_complaint_rows": config.max_complaint_rows,
            "journal_update_enabled": config.update_journal,
        },
        "read_only_guards": {
            "seller_portal_write_actions_allowed": False,
            "complaint_submission_allowed": False,
            "retry_submit_allowed": False,
            "final_submit_click_allowed": False,
            "submit_clicked_during_runner": 0,
        },
        "journal_before": dict(journal_before or {}),
        "session": {},
        "navigation": {},
        "original_review": {},
        "my_complaints": {},
        "confirmation": {
            "direct_id_matches": [],
            "composite_matches": [],
            "weak_matches_rejected": [],
            "result": "unconfirmed",
            "status": "error",
            "status_label": COMPLAINT_STATUS_LABELS["error"],
            "reason": "",
        },
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
        apply_journal_confirmation_result(config, journal, report, run_id)
        report["finished_at"] = iso_now()
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
            feedback_headers: dict[str, str] = {}
            page.on("request", lambda request: capture_seller_portal_feedback_headers(request, feedback_headers))
            try:
                navigation = navigate_to_feedbacks_questions(page, scout_config)
                report["navigation"] = navigation
                if not navigation.get("success"):
                    report["errors"].append({"stage": "navigation", "code": "not_reached", "message": str(navigation.get("blocker") or "")})
                else:
                    report["original_review"] = inspect_original_review(page, config, journal_before, feedback_headers)
                    report["my_complaints"] = inspect_my_complaints_read_only(page, config)
                    report["confirmation"] = evaluate_confirmation(journal_before, report["my_complaints"])
            finally:
                context.close()
                browser.close()
    except Exception as exc:  # pragma: no cover - live fallback
        report["errors"].append({"stage": "browser_confirmation", "code": exc.__class__.__name__, "message": safe_text(str(exc), 800)})
        report["confirmation"]["reason"] = safe_text(str(exc), 400)

    apply_journal_confirmation_result(config, journal, report, run_id)
    report["journal_after"] = dict(journal.find_by_feedback_id(config.feedback_id) or {})
    report["finished_at"] = iso_now()
    return report


def build_scout_config(config: ConfirmationConfig) -> ScoutConfig:
    return ScoutConfig(
        mode="scout-complaints",
        storage_state_path=config.storage_state_path,
        wb_bot_python=config.wb_bot_python,
        output_root=config.output_dir,
        start_url=config.start_url,
        max_feedback_rows=40,
        max_complaint_rows=config.max_complaint_rows,
        max_modal_reviews=0,
        open_complaint_modal=False,
        headless=config.headless,
        timeout_ms=config.timeout_ms,
        write_artifacts=False,
    )


def inspect_original_review(
    page: Page,
    config: ConfirmationConfig,
    record: Mapping[str, Any],
    request_headers: Mapping[str, str],
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "found": False,
        "source": "",
        "network_row": {},
        "dom_row_found": False,
        "dom_row_match": {},
        "row_menu_items": [],
        "complaint_action_still_visible": "unknown",
        "complaint_status": "",
        "complaint_action_found": None,
        "targeted_search": {},
        "blocker": "",
    }
    if not _click_tab_like(page, "Отзывы"):
        report["blocker"] = "Отзывы tab was not found"
        return report
    _wait_settle(page, 2500)
    _wait_for_feedback_rows(page, timeout_ms=10000)
    replay_config = replay_config_for_record(config, record)
    network_rows, network_stats = collect_feedback_rows_from_seller_portal_network(
        page,
        replay_config,
        request_headers=request_headers,
    )
    report["network_stats"] = network_stats
    target_network = next((row for row in network_rows if str(row.get("feedback_id") or "") == config.feedback_id), None)
    if target_network:
        report.update(
            {
                "found": True,
                "source": "seller_portal_network_cursor",
                "network_row": summarize_ui_row(target_network),
                "complaint_status": str(target_network.get("complaint_status") or ""),
                "complaint_action_found": bool(target_network.get("complaint_action_found")),
            }
        )
    api_row = api_row_from_journal(record)
    expected_ui = target_network or {}
    visible_rows = extract_visible_feedback_rows(page, max_rows=25)
    visible_match = find_visible_actionable_row(api_row, visible_rows, expected_ui=expected_ui)
    if not visible_match.get("row"):
        search_result = apply_article_search_for_candidate(page, api_row, expected_ui=expected_ui)
        report["targeted_search"] = search_result
        if search_result.get("ok"):
            _wait_settle(page, 2500)
            visible_rows = extract_visible_feedback_rows(page, max_rows=25)
            visible_match = find_visible_actionable_row(api_row, visible_rows, expected_ui=expected_ui)
    report["dom_row_match"] = visible_match.get("match") or {}
    visible_row = visible_match.get("row") if isinstance(visible_match.get("row"), dict) else {}
    report["dom_row_found"] = bool(visible_row)
    if not visible_row:
        report["complaint_action_still_visible"] = bool(target_network.get("complaint_action_found")) if target_network else "unknown"
        return report
    clicked_menu = _click_safe_row_menu(page, str(visible_row.get("dom_scout_id") or ""))
    report["row_menu_click"] = clicked_menu
    if not clicked_menu.get("ok"):
        report["blocker"] = str(clicked_menu.get("reason") or "row menu not opened")
        return report
    _wait_settle(page, 800)
    menu_state = extract_open_row_menu_state(page)
    report["row_menu_items"] = menu_state.get("items") or []
    report["complaint_action_still_visible"] = bool(menu_state.get("complaint_action_found"))
    _safe_escape(page)
    return report


def inspect_my_complaints_read_only(
    page: Page,
    config: ConfirmationConfig,
) -> dict[str, Any]:
    captured: list[dict[str, Any]] = []
    current_stage = {"tab": "before_my_complaints"}

    def on_response(response: Response) -> None:
        if len(captured) >= 40:
            return
        item = sanitize_network_response(response, stage=current_stage["tab"], target_feedback_id=config.feedback_id)
        if item:
            captured.append(item)

    page.on("response", on_response)
    report: dict[str, Any] = {
        "success": False,
        "pending_count_visible": 0,
        "answered_count_visible": 0,
        "pending": {"tab_clicked": False, "visible_rows": 0, "rows": [], "field_availability": {}},
        "answered": {"tab_clicked": False, "visible_rows": 0, "rows": [], "field_availability": {}},
        "network_evidence": [],
        "blocker": "",
    }
    if not _click_tab_like(page, "Мои жалобы"):
        report["blocker"] = "Мои жалобы tab was not found"
        return report
    _wait_settle(page, 2500)
    for tab_label, key in (("Ждут ответа", "pending"), ("Есть ответ", "answered")):
        current_stage["tab"] = key
        clicked = _click_tab_like(page, tab_label)
        _wait_settle(page, 2500)
        rows = extract_visible_complaint_rows(page, max_rows=config.max_complaint_rows)
        report[key] = {
            "tab_clicked": clicked,
            "visible_rows": len(rows),
            "rows": rows,
            "field_availability": field_availability(rows),
        }
    report["pending_count_visible"] = int(report["pending"]["visible_rows"])
    report["answered_count_visible"] = int(report["answered"]["visible_rows"])
    report["network_evidence"] = captured
    report["network_direct_id_seen"] = any(item.get("target_feedback_id_seen") for item in captured if item.get("complaint_like_url"))
    report["success"] = True
    return report


def evaluate_confirmation(record: Mapping[str, Any], my_complaints: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "direct_id_matches": [],
        "composite_matches": [],
        "weak_matches_rejected": [],
        "network_direct_id_matches": [],
        "result": "unconfirmed",
        "status": "error",
        "status_label": COMPLAINT_STATUS_LABELS["error"],
        "reason": "",
    }
    rows_with_tabs: list[tuple[str, dict[str, Any]]] = []
    for tab in ("pending", "answered"):
        for row in ((my_complaints.get(tab) or {}).get("rows") or []):
            if isinstance(row, Mapping):
                rows_with_tabs.append((tab, dict(row)))
    for tab, row in rows_with_tabs:
        if row_direct_feedback_id(row) == str(record.get("feedback_id") or ""):
            result["direct_id_matches"].append(build_match_summary(tab, row, record, reason="feedback_id"))
            continue
        composite = strong_composite_match(row, record)
        if composite.get("ok"):
            result["composite_matches"].append(build_match_summary(tab, row, record, reason=str(composite.get("reason") or ""), score=composite.get("score")))
            continue
        if _weak_complaint_row_to_record(row, [record]):
            result["weak_matches_rejected"].append(build_match_summary(tab, row, record, reason="weak_only"))
    for item in my_complaints.get("network_evidence") or []:
        if item.get("complaint_like_url") and item.get("target_feedback_id_seen"):
            tab = str(item.get("stage") or "")
            status = status_for_tab(tab, item.get("status_text"))
            if status != "error":
                result["network_direct_id_matches"].append(
                    {
                        "source": "network_response",
                        "stage": tab,
                        "status": status,
                        "status_label": COMPLAINT_STATUS_LABELS[status],
                        "url_path": item.get("url_path"),
                        "status_text": item.get("status_text"),
                    }
                )

    chosen = choose_confirmation_match(result)
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
        result["reason"] = "no direct feedback_id or strong composite match in Seller Portal Мои жалобы"
    return result


def strong_composite_match(row: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
    base = _match_complaint_row_to_record(row, [record])
    row_text = str(row.get("review_text_snippet") or "")
    record_text = _record_review_text(record)
    row_product = str(row.get("product_title") or "")
    record_product = str(record.get("product_name") or "")
    row_category = str(row.get("complaint_reason") or "")
    record_category = str(record.get("wb_category_label") or "")
    row_description = str(row.get("complaint_description") or "")
    record_description = str(record.get("complaint_text") or "")
    row_article = str(row.get("supplier_article") or row.get("nm_id") or row.get("wb_article") or "")
    record_article = str(record.get("supplier_article") or record.get("nm_id") or "")
    text_ok = _strong_text_match(row_text, record_text)
    product_ok = _strong_text_match(row_product, record_product, min_chars=12) or _strong_text_match(row_article, record_article, min_chars=6)
    category_ok = bool(row_category and record_category and row_category == record_category)
    description_ok = _strong_text_match(row_description, record_description, min_chars=18)
    row_date = normalize_date_key(row.get("review_datetime") or row.get("review_date"))
    record_date = normalize_date_key(record.get("review_created_at"))
    date_ok = bool(row_date and record_date and row_date == record_date)
    rating_ok = bool(normalize_rating(row.get("review_rating")) and normalize_rating(row.get("review_rating")) == normalize_rating(record.get("rating")))
    strong = bool(
        (text_ok and product_ok and (category_ok or description_ok) and (date_ok or rating_ok or row_article))
        or (description_ok and product_ok and category_ok and (text_ok or date_ok))
    )
    reasons = []
    for name, ok in (
        ("review_text", text_ok),
        ("product_or_article", product_ok),
        ("category", category_ok),
        ("complaint_text", description_ok),
        ("date", date_ok),
        ("rating", rating_ok),
    ):
        if ok:
            reasons.append(name)
    return {
        "ok": strong,
        "score": (base or {}).get("score", 0),
        "reason": "+".join(reasons) if reasons else "no_strong_fields",
    }


def choose_confirmation_match(result: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for key in ("direct_id_matches", "network_direct_id_matches", "composite_matches"):
        matches = list(result.get(key) or [])
        if matches:
            matches.sort(key=lambda item: status_rank(str(item.get("status") or "")), reverse=True)
            return matches[0]
    return None


def build_match_summary(
    tab: str,
    row: Mapping[str, Any],
    record: Mapping[str, Any],
    *,
    reason: str,
    score: Any = "",
) -> dict[str, Any]:
    status = status_for_tab(tab, row.get("decision_label") or row.get("displayed_status"))
    return {
        "source": "my_complaints_dom",
        "tab": tab,
        "status": status,
        "status_label": COMPLAINT_STATUS_LABELS.get(status, COMPLAINT_STATUS_LABELS["error"]),
        "reason": reason,
        "score": score,
        "row_summary": {
            "product_title": safe_text(str(row.get("product_title") or ""), 180),
            "supplier_article": safe_text(str(row.get("supplier_article") or ""), 120),
            "nm_id": safe_text(str(row.get("nm_id") or row.get("wb_article") or ""), 80),
            "complaint_reason": safe_text(str(row.get("complaint_reason") or ""), 180),
            "complaint_description": safe_text(str(row.get("complaint_description") or ""), 260),
            "review_text_snippet": safe_text(str(row.get("review_text_snippet") or ""), 260),
            "review_datetime": safe_text(str(row.get("review_datetime") or ""), 80),
            "decision_label": safe_text(str(row.get("decision_label") or ""), 80),
            "displayed_status": safe_text(str(row.get("displayed_status") or ""), 80),
        },
    }


def row_direct_feedback_id(row: Mapping[str, Any]) -> str:
    hidden_ids = row.get("hidden_ids") if isinstance(row.get("hidden_ids"), Mapping) else {}
    return str(hidden_ids.get("feedback_id") or row.get("feedback_id") or row.get("seller_portal_feedback_id") or "").strip()


def status_for_tab(tab: str, status_text: Any = "") -> str:
    if tab == "pending":
        return "waiting_response"
    if tab == "answered":
        text = str(status_text or "").lower()
        if any(token in text for token in ("approved", "удовлетвор", "одобрен", "принят")):
            return "satisfied"
        if any(token in text for token in ("rejected", "отклон", "отказ")):
            return "rejected"
        return "error"
    return "error"


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


def apply_journal_confirmation_result(
    config: ConfirmationConfig,
    journal: JsonFileFeedbacksComplaintJournal,
    report: dict[str, Any],
    run_id: str,
) -> None:
    if not config.update_journal:
        report["journal_update"] = {"applied": False, "reason": "disabled"}
        return
    status = str((report.get("confirmation") or {}).get("status") or "error")
    reason = str((report.get("confirmation") or {}).get("reason") or "")
    raw_status = str((report.get("confirmation") or {}).get("result") or "")
    if status == "error":
        original = report.get("original_review") or {}
        reason = (
            f"read-only confirmation unconfirmed: {reason}; "
            f"original complaint_status={original.get('complaint_status') or ''}; "
            f"complaint_action_still_visible={original.get('complaint_action_still_visible')}"
        )
    updated = journal.update_status(
        config.feedback_id,
        status=status,
        raw_status_text=raw_status,
        wb_decision_text=str((report.get("confirmation") or {}).get("status_label") or ""),
        status_sync_run_id=run_id,
        last_error=reason if status == "error" else "",
    )
    record = report.get("journal_before") if isinstance(report.get("journal_before"), Mapping) else {}
    description_metadata = confirmation_description_metadata(record, report.get("confirmation") or {})
    if updated and description_metadata:
        updated = journal.update_metadata(config.feedback_id, description_metadata) or updated
    report["journal_update"] = {
        "applied": bool(updated),
        "status": status,
        "status_label": COMPLAINT_STATUS_LABELS.get(status, COMPLAINT_STATUS_LABELS["error"]),
        "last_error": reason if status == "error" else "",
        "description_persisted": description_metadata.get("description_persisted", "unknown"),
    }


def confirmation_description_metadata(record: Mapping[str, Any], confirmation: Mapping[str, Any]) -> dict[str, Any]:
    chosen = choose_confirmation_match(confirmation)
    row_summary = chosen.get("row_summary") if isinstance(chosen, Mapping) and isinstance(chosen.get("row_summary"), Mapping) else {}
    if row_summary:
        return description_persistence_result(record.get("complaint_text") or "", row_summary.get("complaint_description") or "", observed=True)
    return description_persistence_result(record.get("complaint_text") or "", "", observed=False)


def replay_config_for_record(config: ConfirmationConfig, record: Mapping[str, Any]) -> ReplayConfig:
    date = normalize_date_key(record.get("review_created_at")) or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rating = normalize_rating(record.get("rating")) or "1"
    return ReplayConfig(
        date_from=date,
        date_to=date,
        stars=(int(rating),),
        is_answered="true" if record.get("is_answered") else "false",
        max_api_rows=1,
        max_ui_rows=80,
        mode=NO_SUBMIT_MODE,
        storage_state_path=config.storage_state_path,
        wb_bot_python=config.wb_bot_python,
        output_dir=config.output_dir,
        start_url=config.start_url,
        headless=config.headless,
        timeout_ms=config.timeout_ms,
        write_artifacts=False,
        apply_ui_filters="auto",
        targeted_search="auto",
        max_targeted_searches=1,
    )


def api_row_from_journal(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "feedback_id": str(record.get("feedback_id") or ""),
        "created_at": str(record.get("review_created_at") or ""),
        "created_date": normalize_date_key(record.get("review_created_at")),
        "product_valuation": str(record.get("rating") or ""),
        "rating": str(record.get("rating") or ""),
        "nm_id": str(record.get("nm_id") or ""),
        "supplier_article": str(record.get("supplier_article") or ""),
        "product_name": str(record.get("product_name") or ""),
        "text": str(record.get("review_text") or ""),
        "pros": str(record.get("pros") or ""),
        "cons": str(record.get("cons") or ""),
        "is_answered": bool(record.get("is_answered")),
    }


def sanitize_network_response(response: Response, *, stage: str, target_feedback_id: str) -> dict[str, Any]:
    try:
        url = response.url
    except Exception:
        return {}
    lower_url = url.lower()
    if not any(token in lower_url for token in ("complaint", "claim", "appeal", "feedback", "reviews")):
        return {}
    try:
        status = response.status
        headers = response.headers
    except Exception:
        return {}
    content_type = str(headers.get("content-type") or "")
    if "json" not in content_type and not any(token in lower_url for token in ("complaint", "claim", "appeal")):
        return {}
    payload: Any = None
    text_preview = ""
    try:
        payload = response.json()
    except Exception:
        try:
            text_preview = response.text()[:500]
            payload = json.loads(text_preview) if text_preview.strip().startswith(("{", "[")) else None
        except Exception:
            payload = None
    payload_text = safe_json_text(payload)
    target_seen = bool(target_feedback_id and target_feedback_id in payload_text)
    split = urlsplit(url)
    url_path = f"{split.scheme}://{split.netloc}{split.path}"
    status_text = status_text_from_payload(payload_text)
    return {
        "stage": stage,
        "url_path": safe_text(url_path, 260),
        "status": status,
        "content_type": safe_text(content_type, 120),
        "complaint_like_url": any(token in lower_url for token in ("complaint", "claim", "appeal")),
        "target_feedback_id_seen": target_seen,
        "status_text": status_text,
        "payload_shape": payload_shape(payload),
    }


def safe_json_text(payload: Any) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False)[:200000]
    except Exception:
        return ""


def status_text_from_payload(payload_text: str) -> str:
    lower = payload_text.lower()
    if any(token in lower for token in ("approved", "удовлетвор", "одобрен", "принят")):
        return "approved"
    if any(token in lower for token in ("rejected", "отклон", "отказ")):
        return "rejected"
    if any(token in lower for token in ("pending", "ждёт ответа", "ждет ответа", "на рассмотрении")):
        return "pending"
    return ""


def payload_shape(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return {"type": "dict", "keys": sorted(str(key) for key in payload.keys())[:20]}
    if isinstance(payload, list):
        return {"type": "list", "length": len(payload)}
    if payload is None:
        return {"type": "none"}
    return {"type": type(payload).__name__}


def write_report_artifacts(report: Mapping[str, Any], output_root: Path) -> dict[str, Path]:
    run_dir = output_root / str(report.get("run_id") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    run_dir.mkdir(parents=True, exist_ok=True)
    json_path = run_dir / "seller_portal_feedbacks_complaint_confirmation.json"
    md_path = run_dir / "seller_portal_feedbacks_complaint_confirmation.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown_report(report), encoding="utf-8")
    return {"json": json_path, "markdown": md_path}


def render_markdown_report(report: Mapping[str, Any]) -> str:
    confirmation = report.get("confirmation") or {}
    original = report.get("original_review") or {}
    my = report.get("my_complaints") or {}
    lines = [
        "# Seller Portal Complaint Confirmation",
        "",
        f"- Feedback ID: `{(report.get('parameters') or {}).get('feedback_id')}`",
        f"- Started: `{report.get('started_at')}`",
        f"- Finished: `{report.get('finished_at')}`",
        f"- Session: `{(report.get('session') or {}).get('status')}`",
        f"- Original review found: `{original.get('found')}`",
        f"- Complaint action still visible: `{original.get('complaint_action_still_visible')}`",
        f"- Pending rows read: `{my.get('pending_count_visible', 0)}`",
        f"- Answered rows read: `{my.get('answered_count_visible', 0)}`",
        f"- Direct ID matches: `{len(confirmation.get('direct_id_matches') or []) + len(confirmation.get('network_direct_id_matches') or [])}`",
        f"- Composite matches: `{len(confirmation.get('composite_matches') or [])}`",
        f"- Weak matches rejected: `{len(confirmation.get('weak_matches_rejected') or [])}`",
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
    return {
        "contract_name": report.get("contract_name"),
        "run_id": report.get("run_id"),
        "feedback_id": (report.get("parameters") or {}).get("feedback_id"),
        "session": report.get("session"),
        "original_review": {
            "found": (report.get("original_review") or {}).get("found"),
            "complaint_action_still_visible": (report.get("original_review") or {}).get("complaint_action_still_visible"),
            "complaint_status": (report.get("original_review") or {}).get("complaint_status"),
        },
        "my_complaints": {
            "pending_count_visible": (report.get("my_complaints") or {}).get("pending_count_visible"),
            "answered_count_visible": (report.get("my_complaints") or {}).get("answered_count_visible"),
        },
        "confirmation": report.get("confirmation"),
        "journal_update": report.get("journal_update"),
        "journal_after": report.get("journal_after"),
        "artifact_paths": report.get("artifact_paths"),
        "errors": report.get("errors"),
    }


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    main()
