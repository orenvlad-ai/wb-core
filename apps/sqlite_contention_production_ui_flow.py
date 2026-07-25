#!/usr/bin/env python3
"""Production UI acceptance for SQLite contention and supplier preview."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Mapping
from urllib.parse import quote, urlparse

from playwright.sync_api import BrowserContext, Page, sync_playwright


SUPPLY_UI_PATH = "/sheet-vitrina-v1/vitrina?tab=factory-order"
API_ROOT = "/v1/sheet-vitrina-v1/supply"
TARGET_STATEMENT_SHA256 = (
    "132901c6faaa83901ef445787b3b6f4bb4478f79ac2aa4b2a7dd95ae40c1569d"
)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _json_get(
    context: BrowserContext,
    url: str,
    *,
    label: str,
) -> dict[str, Any]:
    response = context.request.get(
        url,
        headers={"Accept": "application/json"},
        timeout=120_000,
    )
    _assert(response.status == 200, f"{label}: expected HTTP 200, got {response.status}")
    payload = response.json()
    _assert(isinstance(payload, dict), f"{label}: expected JSON object")
    return payload


def _post_json_with_safe_retry(
    context: BrowserContext,
    url: str,
    payload: Mapping[str, Any],
    *,
    label: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = []
    for attempt in range(1, 4):
        response = context.request.post(
            url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            data=json.dumps(dict(payload), ensure_ascii=False),
            timeout=120_000,
        )
        try:
            body = response.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        attempts.append(
            {
                "attempt": attempt,
                "status": response.status,
                "contract_name": str(body.get("contract_name") or ""),
                "code": str(body.get("code") or ""),
                "retryable": bool(body.get("retryable")),
                "waited_ms": int(body.get("waited_ms") or 0),
            }
        )
        if response.status == 200:
            return body, attempts
        if (
            response.status == 503
            and str(body.get("contract_name") or "")
            == "wb_core_sqlite_contention_v1"
            and bool(body.get("retryable"))
            and "database is locked" not in json.dumps(body).lower()
        ):
            time.sleep(min(2.0, 0.5 * attempt))
            continue
        raise AssertionError(
            f"{label}: unexpected HTTP {response.status}, "
            f"code={body.get('code') or '<none>'}"
        )
    raise AssertionError(f"{label}: controlled retry budget was exhausted")


def _calculation_payload(status: Mapping[str, Any], *, kind: str) -> dict[str, Any]:
    last_result = dict(status.get("last_result") or {})
    settings = dict(last_result.get("settings") or {})
    settings.pop("report_date_override", None)
    if settings:
        return settings
    if kind == "factory":
        return {
            "prod_lead_time_days": 10,
            "lead_time_factory_to_ff_days": 5,
            "lead_time_ff_to_wb_days": 2,
            "safety_days_mp": 3,
            "safety_days_ff": 2,
            "cycle_order_days": 14,
            "order_batch_qty": 50,
            "sales_avg_period_days": 14,
            "stock_ff_source": str(
                status.get("selected_stock_ff_source") or "manual_excel"
            ),
        }
    return {
        "sales_avg_period_days": 14,
        "cycle_supply_days": 5,
        "lead_time_to_region_days": 2,
        "safety_days": 1,
        "order_batch_qty": 50,
    }


def _shipment_by_invoice(
    payload: Mapping[str, Any],
    invoice_no: str,
) -> dict[str, Any]:
    for shipment in payload.get("shipments") or []:
        row = dict(shipment or {})
        if str(row.get("invoice_no") or "").strip() == invoice_no:
            return row
    raise AssertionError(f"supplier shipment {invoice_no} is missing")


def _active_financial_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "document_ids": sorted(
            str(item.get("document_id") or "")
            for item in payload.get("documents") or []
        ),
        "expense_line_ids": sorted(
            str(item.get("line_id") or "")
            for item in payload.get("expense_lines") or []
        ),
    }


def run_sqlite_contention_ui_flow(
    *,
    base_url: str,
    auth_cookie: str,
    evidence_dir: Path,
    deployed_sha: str,
    background_evidence: Mapping[str, Any],
    headless: bool = True,
) -> dict[str, Any]:
    evidence_dir = evidence_dir.resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    report_path = evidence_dir / "sqlite_contention_ui_evidence.json"
    try:
        result = _run_sqlite_contention_ui_flow(
            base_url=base_url,
            auth_cookie=auth_cookie,
            evidence_dir=evidence_dir,
            deployed_sha=deployed_sha,
            background_evidence=background_evidence,
            headless=headless,
        )
    except Exception as exc:
        failure = {
            "contract_name": "sqlite_contention_production_ui_flow_v1",
            "status": "failed",
            "requested_url": str(base_url or "").rstrip("/") + SUPPLY_UI_PATH,
            "deployed_sha": str(deployed_sha or ""),
            "background_evidence": dict(background_evidence),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        report_path.write_text(
            json.dumps(failure, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
            encoding="utf-8",
        )
        raise
    report_path.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        **result,
        "evidence_path": str(report_path),
        "evidence_sha256": "sha256:"
        + hashlib.sha256(report_path.read_bytes()).hexdigest(),
    }


def _run_sqlite_contention_ui_flow(
    *,
    base_url: str,
    auth_cookie: str,
    evidence_dir: Path,
    deployed_sha: str,
    background_evidence: Mapping[str, Any],
    headless: bool,
) -> dict[str, Any]:
    normalized_base_url = str(base_url or "").strip().rstrip("/")
    parsed = urlparse(normalized_base_url)
    _assert(
        parsed.scheme in {"http", "https"} and bool(parsed.hostname),
        "UI flow requires an absolute HTTP(S) base URL",
    )
    cookie_name, separator, cookie_value = str(auth_cookie or "").partition("=")
    _assert(
        separator == "="
        and cookie_name == "wb_core_web_session"
        and bool(cookie_value),
        "UI flow requires a valid app-session cookie",
    )
    _assert(
        str(background_evidence.get("active_state") or "")
        in {"active", "activating"},
        "Autoanswers background activity was not observed",
    )
    _assert(
        len(str(deployed_sha or "")) == 40
        and all(character in "0123456789abcdef" for character in deployed_sha),
        "UI flow requires the exact lowercase deployed commit SHA",
    )

    page_errors: list[str] = []
    console_errors: list[str] = []
    server_errors: list[dict[str, Any]] = []
    document_chain: list[dict[str, Any]] = []
    screenshots: list[str] = []
    requested_url = normalized_base_url + SUPPLY_UI_PATH

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
        page: Page = context.new_page()
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
            lambda response: document_chain.append(
                {"status": response.status, "url": response.url}
            )
            if response.request.resource_type == "document"
            else None,
        )

        autoanswers = _json_get(
            context,
            normalized_base_url
            + "/v1/sheet-vitrina-v1/feedbacks/autoanswers/settings",
            label="Autoanswers persistence",
        )
        persistence = dict(
            ((autoanswers.get("operational_status") or {}).get("persistence"))
            or ((autoanswers.get("runtime") or {}).get("persistence"))
            or autoanswers.get("persistence")
            or {}
        )
        _assert(
            bool(persistence.get("isolated_from_registry")),
            "Autoanswers persistence is not isolated from the registry DB",
        )

        factory_status = _json_get(
            context,
            normalized_base_url + API_ROOT + "/factory-order/status",
            label="factory status",
        )
        regional_status = _json_get(
            context,
            normalized_base_url + API_ROOT + "/wb-regional/status",
            label="regional status",
        )
        factory_result, factory_attempts = _post_json_with_safe_retry(
            context,
            normalized_base_url + API_ROOT + "/factory-order/calculate",
            _calculation_payload(factory_status, kind="factory"),
            label="factory calculation",
        )
        regional_result, regional_attempts = _post_json_with_safe_retry(
            context,
            normalized_base_url + API_ROOT + "/wb-regional/calculate",
            _calculation_payload(regional_status, kind="regional"),
            label="WB regional calculation",
        )
        _assert(
            str(factory_result.get("status") or "") == "success",
            "factory calculation did not complete successfully",
        )
        _assert(
            str(regional_result.get("status") or "") == "success",
            "WB regional calculation did not complete successfully",
        )

        shipments = _json_get(
            context,
            normalized_base_url + API_ROOT + "/supplier-shipments",
            label="supplier shipments",
        )
        source_shipment = _shipment_by_invoice(shipments, "26GN527")
        target_shipment = _shipment_by_invoice(shipments, "26GN582")
        source_id = str(source_shipment.get("shipment_id") or "")
        target_id = str(target_shipment.get("shipment_id") or "")
        source_documents = _json_get(
            context,
            normalized_base_url
            + API_ROOT
            + "/supplier-shipments/"
            + quote(source_id, safe="")
            + "/financial-documents",
            label="source financial documents",
        )
        source_document = next(
            (
                dict(item)
                for item in source_documents.get("documents") or []
                if str(item.get("file_sha256") or "") == TARGET_STATEMENT_SHA256
                and str(item.get("document_type") or "") == "bank_fee_statement"
            ),
            None,
        )
        _assert(source_document is not None, "known VTB statement source is missing")
        download_path = str((source_document or {}).get("download_path") or "")
        _assert(download_path.startswith("/"), "known VTB statement download path is missing")
        source_response = context.request.get(
            normalized_base_url + download_path,
            timeout=120_000,
        )
        _assert(source_response.status == 200, "known VTB statement download failed")
        source_bytes = source_response.body()
        _assert(
            hashlib.sha256(source_bytes).hexdigest() == TARGET_STATEMENT_SHA256,
            "known VTB statement SHA readback failed",
        )

        target_documents_url = (
            normalized_base_url
            + API_ROOT
            + "/supplier-shipments/"
            + quote(target_id, safe="")
            + "/financial-documents"
        )
        before_financial = _json_get(
            context,
            target_documents_url,
            label="target financial before preview",
        )
        before_identity = _active_financial_identity(before_financial)

        supplier_url = (
            normalized_base_url
            + "/sheet-vitrina-v1/supplier?embedded=operator&shipment_id="
            + quote(target_id, safe="")
            + "&tab=documents"
        )
        response = page.goto(
            supplier_url,
            wait_until="domcontentloaded",
            timeout=120_000,
        )
        _assert(
            response is not None and response.status == 200,
            "supplier document response must be HTTP 200",
        )
        page.locator("#shipmentCard:not([hidden])").wait_for(timeout=60_000)
        page.locator(
            "#financialDocumentsTabButton:not([hidden])"
        ).wait_for(timeout=60_000)
        page.locator("#financialDocumentsTabButton").click()
        page.locator("#financialDocumentsPanel:not([hidden])").wait_for(
            timeout=60_000
        )
        page.locator("#operatorDocumentsArea:not([hidden])").wait_for(
            timeout=60_000
        )
        page.locator("#financialDocumentFileInput").set_input_files(
            {
                "name": "VTB-statement-existing-source.pdf",
                "mimeType": "application/pdf",
                "buffer": source_bytes,
            }
        )
        page.locator("#systemModal:not([hidden])").wait_for(timeout=120_000)
        modal_screenshot = evidence_dir / "supplier_statement_source_preview.png"
        page.screenshot(path=str(modal_screenshot), full_page=True)
        screenshots.append(str(modal_screenshot))
        page.locator("#systemModalConfirm").click()
        page.locator("#bankFeeStatementPreview:not([hidden])").wait_for(
            timeout=120_000
        )
        selectable_groups = page.locator(
            '#bankFeePreviewContent label[data-bank-fee-status="new"]'
        )
        _assert(
            selectable_groups.count() == 1,
            "26GN582 must expose exactly one new logical bank-fee group",
        )
        logical_group_text = selectable_groups.first.inner_text()
        normalized_group_text = " ".join(logical_group_text.split())
        _assert(
            "платёж №9" in normalized_group_text,
            "26GN582 preview is not anchored to payment #9",
        )
        _assert(
            "13 525,89" in normalized_group_text
            or "13 525.89" in normalized_group_text,
            "26GN582 logical fee total must equal 13 525.89 RUB",
        )
        _assert(
            "2 банковских списания" in normalized_group_text,
            "26GN582 logical fee group must contain two atomic rows",
        )
        _assert(
            "платёж №7" not in normalized_group_text
            and "платёж №11" not in normalized_group_text,
            "already assigned operations #7/#11 were offered as new",
        )
        bank_preview_screenshot = (
            evidence_dir / "supplier_statement_bank_fee_preview_cancel.png"
        )
        page.screenshot(path=str(bank_preview_screenshot), full_page=True)
        screenshots.append(str(bank_preview_screenshot))
        page.locator("#cancelBankFeeImportButton").click()
        page.wait_for_function(
            "() => document.querySelector('#financialDocumentsMessage')"
            ".textContent.includes('Импорт комиссий отменён')",
            timeout=60_000,
        )
        after_financial = _json_get(
            context,
            target_documents_url,
            label="target financial after cancel",
        )
        after_identity = _active_financial_identity(after_financial)
        _assert(
            before_identity == after_identity,
            "preview/cancel changed active financial or expense rows",
        )

        response = page.goto(
            requested_url,
            wait_until="domcontentloaded",
            timeout=120_000,
        )
        _assert(
            response is not None and response.status == 200,
            "supply document response must be HTTP 200",
        )
        final_url = page.url
        page.locator(
            '[data-unified-tab-panel="factory-order"]:not([hidden])'
        ).wait_for(timeout=60_000)
        supply_frame = page.frame_locator(
            '[data-operator-embed-frame="factory-order"]'
        )
        supply_frame.locator("body").wait_for(timeout=60_000)
        _assert(bool(page.title().strip()), "document title is empty")
        _assert(bool(page.locator("body").inner_text().strip()), "document body is empty")
        supply_screenshot = evidence_dir / "supply_calculations_after_contention.png"
        page.screenshot(path=str(supply_screenshot), full_page=True)
        screenshots.append(str(supply_screenshot))
        body_text = page.locator("body").inner_text()
        fatal_markers = [
            marker
            for marker in (
                "database is locked",
                "Internal Server Error",
                "Traceback (most recent call last)",
                "Критическая ошибка",
            )
            if marker.casefold() in body_text.casefold()
        ]
        browser.close()

    _assert(not page_errors, f"pageerror events detected: {page_errors}")
    _assert(not console_errors, f"console errors detected: {console_errors}")
    _assert(not server_errors, f"HTTP 5xx responses detected: {server_errors}")
    _assert(not fatal_markers, f"fatal UI surface detected: {fatal_markers}")
    _assert(
        document_chain
        and all(int(item["status"]) < 500 for item in document_chain),
        "document response chain is missing or contains 5xx",
    )
    screenshot_evidence = [
        {
            "filename": Path(path).name,
            "sha256": "sha256:"
            + hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            "size_bytes": Path(path).stat().st_size,
        }
        for path in screenshots
    ]
    return {
        "contract_name": "sqlite_contention_production_ui_flow_v1",
        "status": "passed",
        "checked_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "requested_url": requested_url,
        "final_url": final_url,
        "deployed_sha": str(deployed_sha or ""),
        "document_chain": document_chain,
        "dom_content_loaded": True,
        "visible_render": True,
        "title_nonempty": True,
        "body_nonempty": True,
        "page_errors": page_errors,
        "console_errors": console_errors,
        "server_errors": server_errors,
        "fatal_surface_matches": fatal_markers,
        "background_evidence": dict(background_evidence),
        "autoanswers_persistence": {
            "database": str(persistence.get("database") or ""),
            "isolated_from_registry": bool(
                persistence.get("isolated_from_registry")
            ),
            "migration_status": str(persistence.get("migration_status") or ""),
        },
        "calculations": {
            "factory": {
                "status": str(factory_result.get("status") or ""),
                "calculation_id": str(
                    factory_result.get("calculation_id") or ""
                ),
                "attempts": factory_attempts,
            },
            "wb_regional": {
                "status": str(regional_result.get("status") or ""),
                "calculation_id": str(
                    regional_result.get("calculation_id") or ""
                ),
                "attempts": regional_attempts,
            },
        },
        "statement_preview_cancel": {
            "source_sha256": TARGET_STATEMENT_SHA256,
            "source_shipment": "26GN527",
            "target_shipment": "26GN582",
            "logical_group_text": normalized_group_text,
            "new_logical_group_count": 1,
            "atomic_row_count": 2,
            "expected_total_rub": "13525.89",
            "active_identity_before": before_identity,
            "active_identity_after": after_identity,
            "no_import_confirm": True,
        },
        "screenshots": screenshot_evidence,
    }
