"""Read-only Seller Portal feedback filter popup DOM scout.

The runner opens the real Seller Portal feedback filters popup, activates the
`Оценка отзыва` section, saves a screenshot and a sanitized DOM summary for the
star-rating checkbox rows. It never submits complaints and never creates
complaint journal records.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import Page, sync_playwright  # noqa: E402

from apps.seller_portal_feedbacks_complaint_dry_run_plan import (  # noqa: E402
    activate_seller_portal_rating_filter_section,
    inspect_seller_portal_rating_filter_popup,
    open_seller_portal_filters_popup,
)
from apps.seller_portal_feedbacks_complaints_scout import (  # noqa: E402
    BUSINESS_TZ,
    DEFAULT_START_URL,
    ScoutConfig,
    _click_tab_like,
    _safe_escape,
    _wait_for_feedback_rows,
    _wait_settle,
    check_session,
    navigate_to_feedbacks_questions,
)
from apps.seller_portal_relogin_session import DEFAULT_STORAGE_STATE_PATH, DEFAULT_WB_BOT_PYTHON  # noqa: E402


CONTRACT_NAME = "seller_portal_feedbacks_filter_dom_scout"
CONTRACT_VERSION = "read_only_v1"
DEFAULT_OUTPUT_ROOT = Path("/opt/wb-core-runtime/state/feedbacks_filter_dom_scout")
LOCAL_OUTPUT_ROOT = Path("artifacts/seller_portal_feedbacks_filter_dom_scout")
FEEDBACKS_TAB_LABEL = "Отзывы"


@dataclass(frozen=True)
class FilterDomScoutConfig:
    storage_state_path: Path
    wb_bot_python: Path
    output_root: Path
    run_dir: Path | None
    start_url: str
    headless: bool
    timeout_ms: int
    write_artifacts: bool


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-state-path", default=str(DEFAULT_STORAGE_STATE_PATH))
    parser.add_argument("--wb-bot-python", default=str(DEFAULT_WB_BOT_PYTHON))
    parser.add_argument("--output-root", default="")
    parser.add_argument("--start-url", default=DEFAULT_START_URL)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--timeout-ms", type=int, default=20000)
    parser.add_argument("--no-artifacts", action="store_true")
    args = parser.parse_args()

    output_root = (
        Path(args.output_root)
        if args.output_root
        else (DEFAULT_OUTPUT_ROOT if Path("/opt/wb-core-runtime/state").exists() else LOCAL_OUTPUT_ROOT)
    )
    write_artifacts = not bool(args.no_artifacts)
    run_dir = output_root / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") if write_artifacts else None
    config = FilterDomScoutConfig(
        storage_state_path=Path(args.storage_state_path).expanduser(),
        wb_bot_python=Path(args.wb_bot_python).expanduser(),
        output_root=output_root,
        run_dir=run_dir,
        start_url=str(args.start_url).rstrip("/") or DEFAULT_START_URL,
        headless=not args.headed,
        timeout_ms=max(5000, int(args.timeout_ms)),
        write_artifacts=write_artifacts,
    )
    report = run_filter_dom_scout(config)
    if config.write_artifacts and config.run_dir:
        paths = write_report_artifacts(report, config.run_dir)
        report["artifact_paths"] = {key: str(path) for key, path in paths.items()}
    print(json.dumps(compact_stdout_report(report), ensure_ascii=False, indent=2))


def run_filter_dom_scout(config: FilterDomScoutConfig) -> dict[str, Any]:
    session = check_session(build_scout_config(config))
    report: dict[str, Any] = {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "mode": "read-only",
        "started_at": iso_now(),
        "finished_at": None,
        "read_only_guards": {
            "seller_portal_write_actions_allowed": False,
            "complaint_submit_clicked": False,
            "complaint_final_submit_allowed": False,
            "journal_write_allowed": False,
            "submit_clicked_count": 0,
        },
        "session": session,
        "navigation": {},
        "filter_dom_scout": empty_filter_dom_scout(),
        "errors": [],
    }
    if not session.get("ok"):
        report["errors"].append(
            {
                "stage": "session",
                "code": str(session.get("status") or "session_invalid"),
                "message": str(session.get("message") or "Seller Portal session is not valid"),
            }
        )
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
            try:
                report["navigation"] = navigate_to_feedbacks_questions(page, build_scout_config(config))
                if not report["navigation"].get("success"):
                    report["filter_dom_scout"]["blocker"] = str(report["navigation"].get("blocker") or "feedbacks page not reached")
                    return report
                _click_tab_like(page, FEEDBACKS_TAB_LABEL)
                _wait_settle(page, 1600)
                _wait_for_feedback_rows(page, timeout_ms=7000)
                report["filter_dom_scout"] = scout_popup(page, config)
            finally:
                _safe_escape(page)
                context.close()
                browser.close()
    except Exception as exc:  # pragma: no cover - live browser fallback
        report["errors"].append({"stage": "browser_scout", "code": exc.__class__.__name__, "message": safe_text(str(exc), 800)})
        report["filter_dom_scout"]["blocker"] = safe_text(str(exc), 500)
    finally:
        report["finished_at"] = iso_now()
    return report


def scout_popup(page: Page, config: FilterDomScoutConfig) -> dict[str, Any]:
    result = empty_filter_dom_scout()
    opened = open_seller_portal_filters_popup(page)
    result["open_filters"] = opened
    result["popup_opened"] = bool(opened.get("ok"))
    if not opened.get("ok"):
        result["blocker"] = str(opened.get("reason") or "filters popup was not opened")
        return result
    _wait_settle(page, 800)
    section = activate_seller_portal_rating_filter_section(page)
    result["rating_section"] = section
    result["rating_section_opened"] = bool(section.get("ok"))
    _wait_settle(page, 500)
    summary = inspect_seller_portal_rating_filter_popup(page)
    result["dom_summary"] = summary
    result["stable_selector_found"] = bool(summary.get("stable_selector_found"))
    result["one_star_checkbox_selector_summary"] = str(((summary.get("candidate_selectors") or {}).get("one_star_checkbox")) or "")
    result["apply_button_selector_summary"] = str(((summary.get("candidate_selectors") or {}).get("apply_button")) or "")
    result["candidate_selectors"] = summary.get("candidate_selectors") or {}
    if config.write_artifacts and config.run_dir:
        config.run_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = config.run_dir / "seller_portal_feedbacks_filter_popup.png"
        page.screenshot(path=str(screenshot_path), full_page=False)
        result["screenshot_path"] = str(screenshot_path)
    if not result["stable_selector_found"]:
        result["blocker"] = str(summary.get("reason") or "stable 1-star checkbox/apply selector was not found")
    return result


def empty_filter_dom_scout() -> dict[str, Any]:
    return {
        "popup_opened": False,
        "rating_section_opened": False,
        "stable_selector_found": False,
        "screenshot_path": "",
        "open_filters": {},
        "rating_section": {},
        "dom_summary": {},
        "candidate_selectors": {},
        "one_star_checkbox_selector_summary": "",
        "apply_button_selector_summary": "",
        "blocker": "",
    }


def build_scout_config(config: FilterDomScoutConfig) -> ScoutConfig:
    return ScoutConfig(
        mode="scout-feedbacks",
        storage_state_path=config.storage_state_path,
        wb_bot_python=config.wb_bot_python,
        output_root=config.output_root,
        start_url=config.start_url,
        max_feedback_rows=5,
        max_complaint_rows=1,
        max_modal_reviews=0,
        open_complaint_modal=False,
        headless=config.headless,
        timeout_ms=config.timeout_ms,
        write_artifacts=False,
    )


def write_report_artifacts(report: dict[str, Any], run_dir: Path) -> dict[str, Path]:
    run_dir.mkdir(parents=True, exist_ok=True)
    json_path = run_dir / "seller_portal_feedbacks_filter_dom_scout.json"
    md_path = run_dir / "seller_portal_feedbacks_filter_dom_scout.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown_report(report), encoding="utf-8")
    paths = {"run_dir": run_dir, "json": json_path, "markdown": md_path}
    screenshot = str((report.get("filter_dom_scout") or {}).get("screenshot_path") or "")
    if screenshot:
        paths["screenshot"] = Path(screenshot)
    return paths


def render_markdown_report(report: Mapping[str, Any]) -> str:
    scout = report.get("filter_dom_scout") or {}
    summary = scout.get("dom_summary") or {}
    lines = [
        "# Seller Portal Feedback Filter DOM Scout",
        "",
        f"- Mode: `{report.get('mode')}`",
        f"- Started: `{report.get('started_at')}`",
        f"- Finished: `{report.get('finished_at')}`",
        f"- Popup opened: `{scout.get('popup_opened')}`",
        f"- Rating section opened: `{scout.get('rating_section_opened')}`",
        f"- Stable selector found: `{scout.get('stable_selector_found')}`",
        f"- Screenshot: `{scout.get('screenshot_path')}`",
        f"- 1-star checkbox selector: `{scout.get('one_star_checkbox_selector_summary')}`",
        f"- Apply button selector: `{scout.get('apply_button_selector_summary')}`",
        f"- Blocker: `{scout.get('blocker')}`",
        "",
        "## Popup Root",
        "",
        f"- `{json.dumps(summary.get('popup_root') or {}, ensure_ascii=False)}`",
        "",
        "## Star Rows",
        "",
    ]
    for row in summary.get("rows") or []:
        lines.append(
            f"- star `{row.get('star')}` checked `{row.get('checked')}` inferred `{row.get('inferred_by_order')}` role `{row.get('role')}` aria `{row.get('aria_checked')}` text `{row.get('text')}` class `{row.get('control_class')}`"
        )
    lines.extend(["", "## Buttons", ""])
    for button in summary.get("buttons") or []:
        lines.append(f"- `{button.get('text')}` tag `{button.get('tag')}` role `{button.get('role')}` class `{button.get('class_name')}`")
    if report.get("errors"):
        lines.extend(["", "## Errors", ""])
        for error in report["errors"]:
            lines.append(f"- `{error.get('stage')}` / `{error.get('code')}`: {error.get('message')}")
    return "\n".join(lines) + "\n"


def compact_stdout_report(report: Mapping[str, Any]) -> dict[str, Any]:
    scout = report.get("filter_dom_scout") or {}
    summary = scout.get("dom_summary") or {}
    return {
        "contract_name": report.get("contract_name"),
        "mode": report.get("mode"),
        "read_only_guards": report.get("read_only_guards"),
        "session": report.get("session"),
        "navigation": report.get("navigation"),
        "filter_dom_scout": {
            "popup_opened": scout.get("popup_opened"),
            "rating_section_opened": scout.get("rating_section_opened"),
            "screenshot_path": scout.get("screenshot_path"),
            "stable_selector_found": scout.get("stable_selector_found"),
            "one_star_checkbox_selector_summary": scout.get("one_star_checkbox_selector_summary"),
            "apply_button_selector_summary": scout.get("apply_button_selector_summary"),
            "rows": summary.get("rows") or [],
            "buttons": summary.get("buttons") or [],
            "blocker": scout.get("blocker"),
        },
        "errors": report.get("errors"),
        "artifact_paths": report.get("artifact_paths"),
    }


def safe_text(value: str, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    main()
