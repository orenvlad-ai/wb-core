#!/usr/bin/env python3
"""Read-only authenticated production acceptance for Finance and Partner reports."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright


REPORTS_PATH = "/sheet-vitrina-v1/vitrina?tab=reports"
FINANCE_API_PATH = "/v1/sheet-vitrina-v1/wb-finance-report"
PARTNER_OPTIONS_API_PATH = "/v1/sheet-vitrina-v1/partner-report/options"
PARTNER_PREVIEW_API_PATH = "/v1/sheet-vitrina-v1/partner-report/preview"
PARTNER_PREVIEW_XLSX_API_PATH = "/v1/sheet-vitrina-v1/partner-report/preview.xlsx"


def run_finance_partner_ui_flow(
    *,
    base_url: str,
    auth_cookie: str,
    evidence_dir: Path,
    headless: bool = True,
) -> dict[str, Any]:
    normalized_base_url = str(base_url or "").strip().rstrip("/")
    parsed = urlparse(normalized_base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Finance UI flow requires an absolute http(s) base URL")
    cookie_name, separator, cookie_value = str(auth_cookie or "").partition("=")
    if separator != "=" or cookie_name != "wb_core_web_session" or not cookie_value:
        raise ValueError("Finance UI flow requires a valid app-session cookie")

    evidence_dir = evidence_dir.resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    requested_url = normalized_base_url + REPORTS_PATH
    report_path = evidence_dir / "finance_partner_ui_flow_report.json"
    page_errors: list[str] = []
    console_errors: list[str] = []
    server_errors: list[dict[str, Any]] = []
    document_responses: list[dict[str, Any]] = []
    non_read_requests: list[dict[str, str]] = []
    screenshots: list[str] = []

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=headless)
            context = browser.new_context(viewport={"width": 1600, "height": 1000})
            context.add_cookies(
                [
                    {
                        "name": cookie_name,
                        "value": cookie_value,
                        "url": normalized_base_url,
                        "httpOnly": True,
                        "sameSite": "Lax",
                    }
                ]
            )
            page = context.new_page()
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on(
                "console",
                lambda message: console_errors.append(message.text)
                if message.type == "error"
                else None,
            )
            page.on(
                "response",
                lambda response: server_errors.append(
                    {
                        "status": response.status,
                        "url": response.url,
                        "resource_type": response.request.resource_type,
                    }
                )
                if response.status >= 500
                else None,
            )
            page.on(
                "response",
                lambda response: document_responses.append(
                    {"status": response.status, "url": response.url}
                )
                if response.request.resource_type == "document"
                else None,
            )
            page.on(
                "request",
                lambda request: non_read_requests.append(
                    {"method": request.method, "url": request.url}
                )
                if request.method not in {"GET", "HEAD", "OPTIONS"}
                else None,
            )

            response = page.goto(
                requested_url,
                wait_until="domcontentloaded",
                timeout=120_000,
            )
            _assert(response is not None and response.status == 200, "reports page HTTP 200")
            _assert(page.url == requested_url, "reports page has no unexpected redirect")
            page.locator('[data-unified-tab-panel="reports"]:not([hidden])').wait_for(
                timeout=60_000
            )
            _assert(bool(page.title().strip()), "reports document title is non-empty")
            _assert(bool(page.locator("body").inner_text().strip()), "reports body is non-empty")

            reports = page.frame_locator('iframe[data-operator-embed-frame="reports"]')
            reports.locator('[data-report-section-button="wb-finance"]').click()
            reports.locator("#wbFinanceReportTableWrap table").wait_for(timeout=120_000)

            finance_payload = _protected_json_get(
                context,
                normalized_base_url + FINANCE_API_PATH,
                label="Finance weekly API",
            )
            finance_weeks = list(finance_payload.get("weeks") or [])
            _assert(finance_payload.get("status") == "ok", "Finance API status")
            _assert(bool(finance_weeks), "Finance API contains persisted weeks")
            finance_facts = reports.locator("body").evaluate(
                """
                () => {
                  const wrap = document.getElementById('wbFinanceReportTableWrap');
                  const table = wrap && wrap.querySelector('table');
                  const rows = table ? [...table.querySelectorAll('tbody tr')] : [];
                  const value = (label) => {
                    const row = rows.find((item) => item.cells[0] && item.cells[0].innerText.trim() === label);
                    return row ? [...row.cells].slice(1).map((cell) => cell.innerText.trim()) : [];
                  };
                  const bodyStyle = getComputedStyle(document.body);
                  return {
                    status: document.getElementById('wbFinanceReportStatus').innerText.trim(),
                    headerCount: table ? table.querySelectorAll('thead th').length : 0,
                    rowCount: rows.length,
                    cogs: value('Себестоимость продаж'),
                    profit: value('Прибыль после себестоимости'),
                    margin: value('Итоговая рентабельность, %'),
                    agent: value('Агентское вознаграждение WB'),
                    acquiring: value('Эквайринг'),
                    withMarketing: value('Расходы WB с маркетингом'),
                    noMarketing: value('Расходы WB без маркетинга'),
                    hasTechnicalExpenseRow: value('Расходы периода, учитываемые в прибыли').length > 0,
                    hasDuplicatePercentRow: value('Расходы WB, % от чистой выручки').length > 0,
                    expenseCells: [...table.querySelectorAll('.wb-finance-expense-share')].map((cell) => cell.innerText.trim()),
                    notes: document.getElementById('wbFinanceReportNotes').innerText.trim(),
                    horizontalOverflow: Boolean(wrap && wrap.scrollWidth > wrap.clientWidth),
                    bodyBackground: bodyStyle.backgroundColor,
                  };
                }
                """
            )
            _assert(
                int(finance_facts["headerCount"]) == len(finance_weeks) + 1,
                "Finance table renders every API week",
            )
            _assert(int(finance_facts["rowCount"]) > 20, "Finance metric matrix is rendered")
            _assert(
                len(finance_facts["cogs"]) == len(finance_weeks)
                and len(finance_facts["profit"]) == len(finance_weeks)
                and len(finance_facts["margin"]) == len(finance_weeks)
                and len(finance_facts["agent"]) == len(finance_weeks)
                and len(finance_facts["acquiring"]) == len(finance_weeks)
                and len(finance_facts["withMarketing"]) == len(finance_weeks)
                and len(finance_facts["noMarketing"]) == len(finance_weeks),
                "Finance canonical metrics align to all weeks",
            )
            _assert(
                not finance_facts["hasTechnicalExpenseRow"]
                and not finance_facts["hasDuplicatePercentRow"],
                "Finance removes technical and duplicate percentage rows",
            )
            _assert(
                all("п.п." not in value for value in finance_facts["expenseCells"]),
                "Finance expense microcells contain no numeric percentage-point delta",
            )
            _assert("недоступен" not in finance_facts["status"].casefold(), "Finance UI is available")
            _assert("стоимость того же nmid на 01.07" in finance_facts["notes"].casefold(), "Finance temporal method is visible")
            _assert("retro-map" in finance_facts["notes"], "Finance rejects an independent retro-cost source")
            _assert(bool(finance_facts["horizontalOverflow"]), "Finance table scrolls locally")
            finance_screenshot = evidence_dir / "finance_weekly_desktop.png"
            page.screenshot(path=str(finance_screenshot), full_page=True)
            screenshots.append(str(finance_screenshot))

            reports.locator('[data-report-section-button="partner"]').click()
            reports.locator("#partnerReportControls").wait_for(timeout=120_000)
            partner_payload = _protected_json_get(
                context,
                normalized_base_url + PARTNER_OPTIONS_API_PATH,
                label="Partner report options API",
            )
            partner_facts = reports.locator("body").evaluate(
                """
                () => {
                  const controls = document.getElementById('partnerReportControls');
                  const settings = controls.querySelector('.partner-report-settings');
                  const wrap = document.getElementById('partnerReportTableWrap');
                  return {
                    status: document.getElementById('partnerReportStatus').innerText.trim(),
                    cardOptions: document.getElementById('partnerReportNmId').options.length,
                    weekOptions: document.querySelectorAll('#partnerReportWeekList input[type=checkbox]').length,
                    saveVisible: !document.getElementById('partnerReportSaveSettings').hidden,
                    generateVisible: !document.getElementById('partnerReportGenerate').hidden,
                    downloadDisabled: document.getElementById('partnerReportDownload').disabled,
                    hasFinalize: Boolean(document.getElementById('partnerReportFinalize')),
                    hasPackage: Boolean(document.getElementById('partnerReportPackage')),
                    settingsColumns: getComputedStyle(settings).gridTemplateColumns.split(' ').length,
                  };
                }
                """
            )
            _assert(
                int(partner_facts["cardOptions"]) == len(partner_payload.get("cards") or []),
                "Partner card options reconcile to API",
            )
            _assert(
                int(partner_facts["weekOptions"]) == len(partner_payload.get("weeks") or []),
                "Partner week options reconcile to API",
            )
            _assert("недоступен" not in partner_facts["status"].casefold(), "Partner UI is available")
            _assert(
                bool(partner_facts["saveVisible"])
                and bool(partner_facts["generateVisible"])
                and bool(partner_facts["downloadDisabled"])
                and not bool(partner_facts["hasFinalize"])
                and not bool(partner_facts["hasPackage"]),
                "Partner actions expose UI preview and XLSX without finalization/ZIP",
            )
            preview_required_fields = (
                "partner_share_pct",
                "invested_capital_rub",
                "replenishment_reserve_pct",
                "weekly_office_expense_rub",
                "tax_rate_pct",
                "common_expense_rule",
            )
            preview_card = next(
                (
                    card
                    for card in partner_payload.get("cards") or []
                    if _has_complete_partner_settings(card, preview_required_fields)
                ),
                None,
            )
            can_preview = bool(partner_payload.get("weeks")) and preview_card is not None
            preview_evidence: dict[str, Any] = {
                "attempted": False,
                "ready": False,
                "reason": "server-owned settings are not complete; production settings were not mutated",
            }
            if can_preview:
                reports.locator("#partnerReportNmId").select_option(
                    str(preview_card.get("nm_id"))
                )
                reports.locator("#partnerReportWeekSummary").click()
                reports.locator("#partnerReportWeeksNone").click()
                reports.locator(
                    "#partnerReportWeekList input[type=checkbox]"
                ).last.check()
                reports.locator("#partnerReportGenerate").click()
                expect(reports.locator("#partnerReportStatus")).not_to_contain_text(
                    "Формируем preview",
                    timeout=120_000,
                )
                preview_status = reports.locator("#partnerReportStatus").inner_text().strip()
                preview_ready = reports.locator(
                    "#partnerReportTableWrap:not([hidden]) table"
                ).count() == 1
                preview_evidence = {
                    "attempted": True,
                    "ready": preview_ready,
                    "status": preview_status,
                    "reason": (
                        "preview rendered from existing server-owned settings"
                        if preview_ready
                        else "source blockers are shown without saving or finalizing"
                    ),
                    "notes": reports.locator("#partnerReportNotes").inner_text().strip(),
                }
                if preview_ready:
                    _assert(
                        reports.locator("#partnerReportTableWrap")
                        .get_by_text("Дивиденды", exact=True)
                        .count()
                        == 1,
                        "Partner preview renders dividend reconciliation",
                    )
                    _assert(
                        reports.locator("#partnerReportDownload").is_enabled(),
                        "Partner Excel activates only after ready preview",
                    )
                    with page.expect_download(timeout=120_000) as download_info:
                        reports.locator("#partnerReportDownload").click()
                    download = download_info.value
                    excel_path = evidence_dir / download.suggested_filename
                    download.save_as(str(excel_path))
                    _assert(excel_path.is_file() and excel_path.stat().st_size > 0, "Partner preview XLSX downloaded")
                    preview_evidence["excel_path"] = str(excel_path)
                    preview_evidence["excel_filename"] = download.suggested_filename
                else:
                    _assert(
                        "не удалось" in preview_status.casefold()
                        or "blocker" in preview_status.casefold()
                        or reports.locator("#partnerReportBlockers").inner_text().strip(),
                        "Partner preview exposes a human-readable source blocker",
                    )
            partner_desktop = evidence_dir / "partner_report_desktop_readonly.png"
            page.screenshot(path=str(partner_desktop), full_page=True)
            screenshots.append(str(partner_desktop))

            page.set_viewport_size({"width": 390, "height": 844})
            narrow_facts = reports.locator("body").evaluate(
                """
                () => {
                  const controls = document.getElementById('partnerReportControls');
                  const settings = controls.querySelector('.partner-report-settings');
                  const wrap = document.getElementById('partnerReportTableWrap');
                  return {
                    pageOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
                    settingsColumns: getComputedStyle(settings).gridTemplateColumns.split(' ').length,
                    controlsWithinViewport: controls.getBoundingClientRect().right <= document.documentElement.clientWidth + 1,
                    localTableScroll: !wrap.hidden && wrap.scrollWidth > wrap.clientWidth,
                  };
                }
                """
            )
            _assert(
                narrow_facts
                == {
                    "pageOverflow": False,
                    "settingsColumns": 1,
                    "controlsWithinViewport": True,
                    "localTableScroll": bool(preview_evidence.get("ready")),
                },
                "Partner narrow layout",
            )
            partner_narrow = evidence_dir / "partner_report_narrow_readonly.png"
            page.screenshot(path=str(partner_narrow), full_page=True)
            screenshots.append(str(partner_narrow))
            final_url = page.url
            browser.close()

        _assert(not page_errors, f"pageerror list is empty: {page_errors}")
        _assert(not console_errors, f"console error list is empty: {console_errors}")
        _assert(not server_errors, f"5xx response list is empty: {server_errors}")
        unexpected_non_read_requests = [
            item
            for item in non_read_requests
            if urlparse(item["url"]).path
            not in {PARTNER_PREVIEW_API_PATH, PARTNER_PREVIEW_XLSX_API_PATH}
        ]
        _assert(
            not unexpected_non_read_requests,
            "UI flow did not save settings or finalize a report: "
            f"{unexpected_non_read_requests}",
        )
        report = {
            "status": "passed",
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "requested_url": requested_url,
            "final_url": final_url,
            "document_responses": document_responses,
            "page_errors": page_errors,
            "console_errors": console_errors,
            "server_errors": server_errors,
            "non_read_requests": non_read_requests,
            "allowed_read_only_post_paths": [
                PARTNER_PREVIEW_API_PATH,
                PARTNER_PREVIEW_XLSX_API_PATH,
            ],
            "finance": {
                "week_count": len(finance_weeks),
                "first_week": finance_weeks[0].get("week_start"),
                "last_week": finance_weeks[-1].get("week_end"),
                "facts": finance_facts,
            },
            "partner": {
                "card_count": len(partner_payload.get("cards") or []),
                "week_count": len(partner_payload.get("weeks") or []),
                "desktop": partner_facts,
                "narrow": narrow_facts,
                "preview": preview_evidence,
                "finalized_report_created": False,
            },
            "screenshots": screenshots,
        }
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return report
    except Exception as exc:
        failed_report = {
            "status": "failed",
            "requested_url": requested_url,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "document_responses": document_responses,
            "page_errors": page_errors,
            "console_errors": console_errors,
            "server_errors": server_errors,
            "non_read_requests": non_read_requests,
            "screenshots": screenshots,
        }
        report_path.write_text(
            json.dumps(failed_report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        raise


def _protected_json_get(context: Any, url: str, *, label: str) -> dict[str, Any]:
    response = context.request.get(url, headers={"Accept": "application/json"}, timeout=120_000)
    _assert(response.status == 200, f"{label}: HTTP 200")
    payload = response.json()
    _assert(isinstance(payload, dict), f"{label}: JSON object")
    return payload


def _has_complete_partner_settings(
    card: dict[str, Any],
    required_fields: tuple[str, ...],
) -> bool:
    parameters = dict(((card or {}).get("settings") or {}).get("parameters") or {})
    return all(parameters.get(key) not in {None, ""} for key in required_fields)


def _assert(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
