#!/usr/bin/env python3
"""Read-only Playwright acceptance flow for the production warehouse UI."""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from playwright.sync_api import Page, sync_playwright


WAREHOUSES = (
    ("production", "На производстве"),
    ("china_to_ff", "В пути: Китай → FF"),
    ("ff", "Склад FF"),
    ("ff_to_wb", "В пути: FF → WB"),
    ("wb", "Склад WB"),
    ("wb_acceptance_discrepancy", "Расхождения приёмки WB"),
)
WAREHOUSE_UI_PATH = "/sheet-vitrina-v1/vitrina?tab=warehouses&warehouse=production"


def run_warehouse_ui_flow(
    *,
    base_url: str,
    auth_cookie: str | None,
    expected_readback: Mapping[str, Any],
    evidence_dir: Path,
    headless: bool = True,
    allowed_server_error_paths: tuple[str, ...] = (),
    allowed_console_error_messages: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Render every warehouse and compare visible values with canonical readback."""

    normalized_base_url = str(base_url or "").strip().rstrip("/")
    parsed_base_url = urlparse(normalized_base_url)
    if parsed_base_url.scheme not in {"http", "https"} or not parsed_base_url.hostname:
        raise ValueError("warehouse UI flow requires an absolute http(s) base URL")
    documents = list(expected_readback.get("documents") or [])
    expected_by_key = {str(item.get("warehouse_key") or ""): dict(item) for item in documents}
    expected_keys = [key for key, _ in WAREHOUSES]
    if expected_readback.get("status") != "ready" or sorted(expected_by_key) != sorted(expected_keys):
        raise ValueError("warehouse UI flow requires a reconciled six-document readback")
    if len(documents) != len(expected_keys):
        raise ValueError("warehouse UI flow requires exactly six opening documents")

    evidence_dir = evidence_dir.resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    page_errors: list[str] = []
    console_errors: list[str] = []
    server_errors: list[dict[str, Any]] = []
    navigation_chain: list[dict[str, Any]] = []
    fatal_surface_matches: list[dict[str, str]] = []
    screenshots: list[str] = []
    warehouse_evidence: list[dict[str, Any]] = []
    requested_url = normalized_base_url + WAREHOUSE_UI_PATH

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        if auth_cookie:
            cookie_name, separator, cookie_value = auth_cookie.partition("=")
            if separator != "=" or cookie_name != "wb_core_web_session" or not cookie_value:
                raise ValueError("invalid app-session cookie supplied to warehouse UI flow")
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
            lambda message: console_errors.append(message.text) if message.type == "error" else None,
        )
        page.on(
            "response",
            lambda response: server_errors.append(
                {"status": response.status, "url": response.url, "resource_type": response.request.resource_type}
            )
            if response.status >= 500
            else None,
        )
        page.on(
            "response",
            lambda navigation_response: navigation_chain.append(
                {"status": navigation_response.status, "url": navigation_response.url}
            )
            if navigation_response.request.resource_type == "document"
            else None,
        )
        response = page.goto(requested_url, wait_until="domcontentloaded", timeout=120_000)
        _assert(response is not None and response.status == 200, "warehouse document response must be HTTP 200")
        initial_final_url = page.url
        main_navigation_chain = list(navigation_chain)
        _assert(initial_final_url == requested_url, "warehouse main navigation must not redirect unexpectedly")
        page.locator('[data-unified-tab-panel="warehouses"]:not([hidden])').wait_for(timeout=60_000)
        _assert(page.locator(".warehouse-switch").count() == 6, "exactly six warehouse selectors must render")
        _assert(bool(page.title().strip()), "document title must be non-empty")
        _assert(bool(page.locator("body").inner_text().strip()), "document body must be non-empty")

        for warehouse_key, warehouse_name in WAREHOUSES:
            expected = expected_by_key[warehouse_key]
            page.locator(f'[data-warehouse-key="{warehouse_key}"]').click()
            page.wait_for_function(
                "expected => document.querySelector('[data-warehouse-title]').textContent === expected",
                arg=warehouse_name,
                timeout=60_000,
            )
            document_row = page.locator(
                f'[data-warehouse-document-id="{expected["document_id"]}"]'
            )
            document_row.wait_for(timeout=60_000)
            _assert(page.locator("[data-warehouse-documents] [data-warehouse-document-id]").count() == 1, warehouse_name)

            detail_response = context.request.get(
                normalized_base_url + "/v1/sheet-vitrina-v1/warehouses/" + warehouse_key,
                headers={"Accept": "application/json"},
                timeout=60_000,
            )
            _assert(detail_response.status == 200, f"{warehouse_name}: detail API status")
            detail_payload = detail_response.json()
            detail_summary = dict(detail_payload.get("warehouse") or {})
            detail_balances = list(detail_payload.get("balances") or [])
            detail_documents = list(detail_payload.get("documents") or [])
            _assert(len(detail_documents) == 1, f"{warehouse_name}: one detail API document")
            _assert(
                str(detail_documents[0].get("document_id") or "") == str(expected.get("document_id") or ""),
                f"{warehouse_name}: detail/readback document identity",
            )
            _assert(
                Decimal(str(detail_documents[0].get("total_quantity") or 0))
                == Decimal(str(expected.get("total_quantity") or 0)),
                f"{warehouse_name}: opening document/readback quantity",
            )
            _assert(
                dict(detail_documents[0].get("provenance") or {})
                == dict(expected.get("provenance") or {}),
                f"{warehouse_name}: document provenance/readback",
            )
            if warehouse_key == "wb_acceptance_discrepancy":
                discrepancy_provenance = dict(detail_documents[0].get("provenance") or {})
                _assert(detail_balances == [], "Расхождения приёмки WB: no opening SKU rows")
                _assert(
                    int(detail_summary.get("sku_count") or 0) == 0
                    and Decimal(str(detail_summary.get("total_quantity") or 0)) == 0,
                    "Расхождения приёмки WB: visible opening balance is exactly zero",
                )
                _assert(
                    discrepancy_provenance.get("opening_policy") == "zero_at_cutover"
                    and discrepancy_provenance.get("historical_backfill") is False
                    and discrepancy_provenance.get("historical_wb_acceptance_evaluated") is False,
                    "Расхождения приёмки WB: zero_at_cutover provenance",
                )

            summary_values = page.locator("[data-warehouse-summary] .warehouse-summary-value").all_inner_texts()
            _assert(len(summary_values) == 4, f"{warehouse_name}: four summary values")
            visible_sku_count = _visible_decimal(summary_values[0])
            visible_quantity = _visible_decimal(summary_values[1])
            expected_sku_count = Decimal(str(detail_summary.get("sku_count") or 0))
            expected_quantity = Decimal(str(detail_summary.get("total_quantity") or 0))
            _assert(visible_sku_count == expected_sku_count, f"{warehouse_name}: visible SKU count")
            _assert(visible_quantity == expected_quantity, f"{warehouse_name}: visible total quantity")
            _assert(summary_values[2].strip() == "—", f"{warehouse_name}: capital is a dash")
            _assert(summary_values[3].strip() == "—", f"{warehouse_name}: average cost is a dash")
            _assert("Актуальность:" in page.locator("[data-warehouse-meta]").inner_text(), f"{warehouse_name}: timestamp")
            _assert("Источник:" in page.locator("[data-warehouse-meta]").inner_text(), f"{warehouse_name}: source")
            _assert(
                "Количество зафиксировано, стоимость не задана"
                in page.locator("[data-warehouse-status]").inner_text(),
                f"{warehouse_name}: quantity-only status",
            )
            warehouse_surface_text = page.locator('[data-unified-tab-panel="warehouses"]').inner_text()
            for marker in ("Internal Server Error", "Traceback", "Остатки / Склады failed", "Данные склада не загружены."):
                if marker in warehouse_surface_text:
                    fatal_surface_matches.append({"warehouse_key": warehouse_key, "marker": marker})

            balance_count = page.locator("[data-warehouse-balance-row]").count()
            _assert(balance_count == int(expected_sku_count), f"{warehouse_name}: balance row count")
            _assert(balance_count == len(detail_balances), f"{warehouse_name}: UI/detail balance rows")
            if warehouse_key == "ff":
                legacy_response = context.request.get(
                    normalized_base_url
                    + "/v1/sheet-vitrina-v1/supply/ff-stocks?operations_limit=50&operations_page=1&show_technical_archive=0",
                    headers={"Accept": "application/json"},
                    timeout=60_000,
                )
                _assert(legacy_response.status == 200, "Склад FF: legacy canonical API status")
                legacy_payload = legacy_response.json()
                legacy_rows = list(((legacy_payload.get("registry") or {}).get("rows") or []))
                legacy_nonzero = {
                    int(item.get("nm_id") or 0): Decimal(str(item.get("quantity") or 0))
                    for item in legacy_rows
                    if Decimal(str(item.get("quantity") or 0)) != 0
                }
                detail_nonzero = {
                    int(item.get("nm_id") or 0): Decimal(str(item.get("quantity") or 0))
                    for item in detail_balances
                    if Decimal(str(item.get("quantity") or 0)) != 0
                }
                _assert(detail_nonzero == legacy_nonzero, "Склад FF: unified/legacy canonical quantities")
            top_screenshot = evidence_dir / f"warehouse_{warehouse_key}_top.png"
            page.screenshot(path=str(top_screenshot), full_page=False)
            screenshots.append(str(top_screenshot))

            document_row.scroll_into_view_if_needed()
            document_row.locator("details").first.click()
            expected_lines = list(expected.get("lines") or [])
            rendered_document_lines = document_row.locator(".warehouse-document-lines tbody tr").count()
            _assert(
                rendered_document_lines == max(1, len(expected_lines)),
                f"{warehouse_name}: opening document line count",
            )
            document_text = document_row.inner_text()
            _assert(str(expected.get("document_number") or "") in document_text, f"{warehouse_name}: document number")
            _assert("Ввод начальных остатков" in document_text, f"{warehouse_name}: document type")
            _assert(document_text.count("—") >= 2, f"{warehouse_name}: document economics are dashes")
            document_provenance = document_row.locator(".warehouse-document-provenance details")
            _assert(document_provenance.count() == 1, f"{warehouse_name}: document provenance control")
            document_provenance.click()
            document_provenance_text = document_row.locator(".warehouse-document-provenance pre").inner_text()
            _assert(bool(document_provenance_text.strip()), f"{warehouse_name}: document provenance payload")
            if warehouse_key == "wb_acceptance_discrepancy":
                _assert(
                    '"opening_policy": "zero_at_cutover"' in document_provenance_text,
                    "Расхождения приёмки WB: visible zero_at_cutover policy",
                )
            if expected_lines:
                document_row.locator(".warehouse-document-lines details").first.click()
                _assert(
                    bool(document_row.locator(".warehouse-document-lines pre").first.inner_text().strip()),
                    f"{warehouse_name}: line provenance",
                )
            document_screenshot = evidence_dir / f"warehouse_{warehouse_key}_document.png"
            page.screenshot(path=str(document_screenshot), full_page=False)
            screenshots.append(str(document_screenshot))
            warehouse_evidence.append(
                {
                    "warehouse_key": warehouse_key,
                    "warehouse_name": warehouse_name,
                    "document_id": str(expected.get("document_id") or ""),
                    "sku_count": int(expected_sku_count),
                    "total_quantity": str(detail_summary.get("total_quantity") or 0),
                    "opening_sku_count": int(expected.get("sku_count") or 0),
                    "opening_total_quantity": str(expected.get("total_quantity") or 0),
                    "balance_rows": balance_count,
                    "document_lines": len(expected_lines),
                    "document_provenance": dict(expected.get("provenance") or {}),
                    "top_screenshot": str(top_screenshot),
                    "document_screenshot": str(document_screenshot),
                }
            )

        page.locator('[data-unified-tab-button="factory-order"]').click()
        frame = page.frame_locator('[data-operator-embed-frame="factory-order"]')
        frame.locator('[data-supply-mode-button="fulfillment"]').click()
        frame.locator('[data-ff-section-button="stocks"]').click()
        page.wait_for_function(
            "document.querySelector('[data-warehouse-title]').textContent === 'Склад FF'",
            timeout=60_000,
        )
        _assert("tab=warehouses" in page.url and "warehouse=ff" in page.url, "legacy FF transition")
        legacy_screenshot = evidence_dir / "warehouse_legacy_ff_transition.png"
        page.screenshot(path=str(legacy_screenshot), full_page=False)
        screenshots.append(str(legacy_screenshot))
        final_url = page.url
        context.close()
        browser.close()

    unexpected_server_errors = [
        item
        for item in server_errors
        if urlparse(str(item.get("url") or "")).path not in set(allowed_server_error_paths)
    ]
    _assert(not unexpected_server_errors, f"5xx responses: {unexpected_server_errors}")
    _assert(not page_errors, f"pageerror: {page_errors}")
    unexpected_console_errors = [
        message for message in console_errors if message not in set(allowed_console_error_messages)
    ]
    _assert(not unexpected_console_errors, f"console errors: {unexpected_console_errors}")
    _assert(not fatal_surface_matches, f"fatal UI surface: {fatal_surface_matches}")
    report = {
        "status": "ok",
        "requested_url": requested_url,
        "final_url": final_url,
        "initial_final_url": initial_final_url,
        "document_status": int(response.status) if response is not None else None,
        "main_navigation_chain": main_navigation_chain,
        "all_document_responses": navigation_chain,
        "title_nonempty": True,
        "body_nonempty": True,
        "cutover": dict(expected_readback.get("cutover") or {}),
        "warehouses": warehouse_evidence,
        "legacy_ff_transition": True,
        "server_errors": server_errors,
        "unexpected_server_errors": unexpected_server_errors,
        "page_errors": page_errors,
        "console_errors": console_errors,
        "unexpected_console_errors": unexpected_console_errors,
        "fatal_surface_matches": fatal_surface_matches,
        "screenshots": screenshots,
    }
    report_path = evidence_dir / "warehouse_ui_flow_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def _visible_decimal(value: str) -> Decimal:
    normalized = str(value or "").replace("\u00a0", "").replace("\u202f", "").replace(" ", "").replace(",", ".")
    try:
        return Decimal(normalized)
    except Exception as exc:
        raise AssertionError(f"visible value is not numeric: {value!r}") from exc


def _assert(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
