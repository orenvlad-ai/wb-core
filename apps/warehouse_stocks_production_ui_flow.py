#!/usr/bin/env python3
"""Read-only Playwright acceptance flow for the production warehouse UI."""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urlparse

from playwright.sync_api import FrameLocator, Page, expect, sync_playwright


WAREHOUSES = (
    ("production", "На производстве"),
    ("china_to_ff", "Китай → FF"),
    ("ff", "Склад FF"),
    ("ff_to_wb", "FF → WB"),
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
    strict_business_acceptance: bool = True,
    allowed_server_error_paths: tuple[str, ...] = (),
    allowed_console_error_messages: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Render every warehouse and compare visible values with canonical readback."""

    normalized_base_url = str(base_url or "").strip().rstrip("/")
    parsed_base_url = urlparse(normalized_base_url)
    if parsed_base_url.scheme not in {"http", "https"} or not parsed_base_url.hostname:
        raise ValueError("warehouse UI flow requires an absolute http(s) base URL")
    documents = [
        dict(item)
        for item in expected_readback.get("documents") or []
        if str(item.get("document_type") or "") in {"functional_cutover", "warehouse_sync"}
    ]
    expected_by_key = {str(item.get("warehouse_key") or ""): dict(item) for item in documents}
    expected_keys = [key for key, _ in WAREHOUSES]
    if expected_readback.get("status") != "ready" or sorted(expected_by_key) != sorted(expected_keys):
        raise ValueError("warehouse UI flow requires a reconciled six-document readback")
    if len(documents) != len(expected_keys):
        raise ValueError("warehouse UI flow requires exactly six opening documents")
    reconciliation = dict(expected_readback.get("reconciliation") or {})
    if int(reconciliation.get("negative_balance_count") or 0) != 0 or int(reconciliation.get("positive_cost_gap_count") or 0) != 0:
        raise ValueError("warehouse UI flow requires non-negative fully cost-covered readback")
    cutover_discrepancy = dict(expected_readback.get("cutover_opening_discrepancy") or {})
    if Decimal(str(cutover_discrepancy.get("quantity") or 0)) != 0:
        raise ValueError("functional cutover discrepancy opening must be zero")
    historical_cost = dict(expected_readback.get("historical_wb_cost_projection") or {})
    if int(historical_cost.get("gap_count") or 0) != 0:
        raise ValueError("historical WB cost projection has positive uncovered rows")
    sync_readback = dict(expected_readback.get("sync") or {})
    if not str(sync_readback.get("last_success_at") or ""):
        raise ValueError("warehouse UI flow requires a successful bounded WB sync timestamp")

    evidence_dir = evidence_dir.resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    page_errors: list[str] = []
    console_errors: list[str] = []
    server_errors: list[dict[str, Any]] = []
    navigation_chain: list[dict[str, Any]] = []
    fatal_surface_matches: list[dict[str, str]] = []
    screenshots: list[str] = []
    warehouse_evidence: list[dict[str, Any]] = []
    settings_evidence: dict[str, Any] = {}
    supplier_evidence: dict[str, Any] = {}
    consumer_evidence: dict[str, Any] = {}
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
        _assert(page.locator("[data-warehouse-key]").count() == 6, "exactly six warehouse selectors must render")
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
            matching_documents = [
                item
                for item in detail_documents
                if str(item.get("document_id") or "") == str(expected.get("document_id") or "")
            ]
            _assert(len(matching_documents) == 1, f"{warehouse_name}: active version document is in detail registry")
            detail_document = matching_documents[0]
            _assert(
                str(detail_document.get("document_id") or "") == str(expected.get("document_id") or ""),
                f"{warehouse_name}: detail/readback document identity",
            )
            _assert(
                Decimal(str(detail_document.get("total_quantity") or 0))
                == Decimal(str(expected.get("quantity") or 0)),
                f"{warehouse_name}: opening document/readback quantity",
            )
            _assert(
                dict(detail_document.get("provenance") or {})
                == dict(expected.get("provenance") or {}),
                f"{warehouse_name}: document provenance/readback",
            )
            if warehouse_key == "wb_acceptance_discrepancy":
                _assert(
                    all(Decimal(str(item.get("quantity") or 0)) >= 0 for item in detail_balances),
                    "Расхождения приёмки WB: negative balances are forbidden",
                )

            summary_values = page.locator("[data-warehouse-summary] .warehouse-summary-value").all_inner_texts()
            _assert(len(summary_values) == (8 if warehouse_key == "wb" else 4), f"{warehouse_name}: summary values")
            visible_sku_count = _visible_decimal(summary_values[0])
            visible_quantity = _visible_decimal(summary_values[1])
            visible_capital = _visible_money(summary_values[2])
            expected_sku_count = Decimal(str(detail_summary.get("sku_count") or 0))
            expected_quantity = Decimal(str(detail_summary.get("total_quantity") or 0))
            expected_capital = Decimal(str(detail_summary.get("total_capital_rub") or 0))
            expected_wac = Decimal(str(detail_summary.get("average_unit_cost_rub") or 0))
            _assert(visible_sku_count == expected_sku_count, f"{warehouse_name}: visible SKU count")
            _assert(visible_quantity == expected_quantity, f"{warehouse_name}: visible total quantity")
            _assert(abs(visible_capital - expected_capital) < Decimal("0.02"), f"{warehouse_name}: visible capital")
            if expected_quantity > 0:
                visible_wac = _visible_money(summary_values[3])
                _assert(abs(visible_wac - expected_wac) < Decimal("0.02"), f"{warehouse_name}: visible WAC")
                _assert(expected_capital > 0 and expected_wac > 0, f"{warehouse_name}: positive cost coverage")
            else:
                _assert(summary_values[3].strip() == "—", f"{warehouse_name}: zero warehouse WAC is honestly empty")
            _assert("Актуальность:" in page.locator("[data-warehouse-meta]").inner_text(), f"{warehouse_name}: timestamp")
            _assert("Источник:" in page.locator("[data-warehouse-meta]").inner_text(), f"{warehouse_name}: source")
            _assert(
                any(marker in page.locator("[data-warehouse-status]").inner_text() for marker in ("Сертифицировано", "provisional", "last good")),
                f"{warehouse_name}: functional status",
            )
            _assert(
                all(
                    Decimal(str(item.get("quantity") or 0)) >= 0
                    and (Decimal(str(item.get("quantity") or 0)) == 0 or Decimal(str(item.get("wac_rub") or 0)) > 0)
                    for item in detail_balances
                ),
                f"{warehouse_name}: no negative or uncovered SKU balances",
            )
            if warehouse_key == "wb":
                contour = dict(detail_summary.get("wb_contour") or {})
                expected_contour = [
                    Decimal(str(contour.get("quantity") or 0)),
                    Decimal(str(contour.get("in_way_to_client") or 0)),
                    Decimal(str(contour.get("in_way_from_client") or 0)),
                    Decimal(str(contour.get("total") or 0)),
                ]
                _assert([_visible_decimal(value) for value in summary_values[4:8]] == expected_contour, "Склад WB: four contour quantities")
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
            expected_lines = list(detail_document.get("lines") or [])
            rendered_document_lines = document_row.locator(".warehouse-document-lines tbody tr").count()
            _assert(
                rendered_document_lines == max(1, len(expected_lines)),
                f"{warehouse_name}: opening document line count",
            )
            document_text = document_row.inner_text()
            _assert(str(expected.get("document_id") or "") in document_text, f"{warehouse_name}: document number")
            _assert(
                any(label in document_text for label in ("Функциональный cutover", "Почасовая версия склада")),
                f"{warehouse_name}: functional document type",
            )
            document_provenance = document_row.locator(".warehouse-document-provenance details")
            _assert(document_provenance.count() == 1, f"{warehouse_name}: document provenance control")
            document_provenance.click()
            document_provenance_text = document_row.locator(".warehouse-document-provenance pre").inner_text()
            _assert(bool(document_provenance_text.strip()), f"{warehouse_name}: document provenance payload")
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
                    "opening_total_quantity": str(expected.get("quantity") or 0),
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
        legacy_ff_expected = next(
            item for item in warehouse_evidence if item.get("warehouse_key") == "ff"
        )
        legacy_ff_document = page.locator(
            f'[data-warehouse-document-id="{legacy_ff_expected["document_id"]}"]'
        )
        legacy_ff_document.wait_for(timeout=60_000)
        legacy_summary_values = page.locator(
            "[data-warehouse-summary] .warehouse-summary-value"
        ).all_inner_texts()
        _assert(len(legacy_summary_values) == 4, "legacy FF transition: four loaded summary values")
        _assert(
            _visible_decimal(legacy_summary_values[0])
            == Decimal(str(legacy_ff_expected["sku_count"])),
            "legacy FF transition: loaded SKU count",
        )
        _assert(
            _visible_decimal(legacy_summary_values[1])
            == Decimal(str(legacy_ff_expected["total_quantity"])),
            "legacy FF transition: loaded total quantity",
        )
        _assert(_visible_money(legacy_summary_values[2]) > 0, "legacy FF transition: capital loaded")
        _assert(_visible_money(legacy_summary_values[3]) > 0, "legacy FF transition: WAC loaded")
        _assert(
            any(marker in page.locator("[data-warehouse-status]").inner_text() for marker in ("Сертифицировано", "provisional", "last good")),
            "legacy FF transition: functional status",
        )
        _assert(
            page.locator("[data-warehouse-balance-row]").count()
            == int(legacy_ff_expected["balance_rows"]),
            "legacy FF transition: loaded balance rows",
        )
        legacy_screenshot = evidence_dir / "warehouse_legacy_ff_transition.png"
        page.screenshot(path=str(legacy_screenshot), full_page=False)
        screenshots.append(str(legacy_screenshot))

        if not strict_business_acceptance:
            final_url = page.url
            context.close()
            browser.close()
            unexpected_server_errors = [
                item
                for item in server_errors
                if urlparse(str(item.get("url") or "")).path not in set(allowed_server_error_paths)
            ]
            unexpected_console_errors = [
                message for message in console_errors if message not in set(allowed_console_error_messages)
            ]
            _assert(not unexpected_server_errors, f"5xx responses: {unexpected_server_errors}")
            _assert(not page_errors, f"pageerror: {page_errors}")
            _assert(not unexpected_console_errors, f"console errors: {unexpected_console_errors}")
            _assert(not fatal_surface_matches, f"fatal UI surface: {fatal_surface_matches}")
            report = {
                "status": "ok",
                "acceptance_scope": "local_warehouse_structure",
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
                "calculation_parameters": {"status": "not_in_local_structure_scope"},
                "supplier_registry": {"status": "not_in_local_structure_scope"},
                "dependent_consumers": {"status": "not_in_local_structure_scope"},
                "hourly_sync": dict(expected_readback.get("sync") or {}),
                "historical_wb_cost_projection": dict(expected_readback.get("historical_wb_cost_projection") or {}),
                "legacy_ff_transition": True,
                "legacy_ff_reconciliation": {
                    "document_id": str(legacy_ff_expected["document_id"]),
                    "sku_count": int(legacy_ff_expected["sku_count"]),
                    "total_quantity": str(legacy_ff_expected["total_quantity"]),
                    "balance_rows": int(legacy_ff_expected["balance_rows"]),
                    "economics_loaded": True,
                    "loaded_before_screenshot": True,
                },
                "server_errors": server_errors,
                "unexpected_server_errors": unexpected_server_errors,
                "page_errors": page_errors,
                "console_errors": console_errors,
                "unexpected_console_errors": unexpected_console_errors,
                "fatal_surface_matches": fatal_surface_matches,
                "screenshots": screenshots,
            }
            report_path = evidence_dir / "warehouse_ui_flow_report.json"
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            report["report_path"] = str(report_path)
            return report

        settings_url = normalized_base_url + "/sheet-vitrina-v1/settings"
        settings_response = page.goto(settings_url, wait_until="domcontentloaded", timeout=120_000)
        _assert(settings_response is not None and settings_response.status == 200, "calculation settings page status")
        settings_surface = _settings_frame_locator(page)
        settings_surface.locator('[data-settings-group-button="user-directory"]').click()
        settings_surface.locator('[data-settings-group-panel="user-directory"]:not([hidden])').wait_for(
            timeout=60_000
        )
        buyout_input = settings_surface.locator('[data-calculation-rate="buyout_rate"]')
        buyout_input.wait_for(timeout=60_000)
        expect(buyout_input).to_have_value("91", timeout=60_000)
        _assert(
            buyout_input.input_value() == "91",
            "visible calculation settings buyout 91%",
        )
        settings_api = context.request.get(
            normalized_base_url + "/v1/sheet-vitrina-v1/settings/calculation-parameters",
            headers={"Accept": "application/json"},
            timeout=60_000,
        )
        _assert(settings_api.status == 200, "calculation settings API status")
        settings_payload = settings_api.json()
        parameters = dict(((settings_payload.get("current") or {}).get("parameters") or {}))
        _assert(parameters.get("buyout_rate_pct") == "91", "calculation settings buyout 91%")
        _assert(parameters.get("included_expense_rate_pct") == "44", "calculation settings expenses 44%")
        _assert(parameters.get("retained_share_pct") == "56", "calculation settings retained share 56%")
        reference = dict(settings_payload.get("reference") or {})
        _assert(reference.get("status") == "ready" and len(reference.get("weeks") or []) == 3, "three closed WB weeks")
        _assert(
            settings_surface.locator("#calculationExpenseTotal").inner_text().strip() == "44%",
            "visible expenses 44%",
        )
        _assert(
            settings_surface.locator("#calculationRetainedShare").inner_text().strip() == "56%",
            "visible retained share 56%",
        )
        _assert(
            "canonical_WB_WAC" in settings_surface.locator("#calculationFormulaPreview").inner_text(),
            "visible Proxy formula",
        )
        reference_rows = settings_surface.locator("#calculationReferenceRows tr")
        expect(reference_rows).to_have_count(6, timeout=60_000)
        _assert(reference_rows.count() == 6, "six WB reference rows")
        history_rows = settings_surface.locator("#calculationHistoryRows tr")
        history_rows.first.wait_for(timeout=60_000)
        _assert(history_rows.count() >= 1, "settings version history")
        settings_screenshot = evidence_dir / "calculation_parameters.png"
        page.screenshot(path=str(settings_screenshot), full_page=True)
        screenshots.append(str(settings_screenshot))
        settings_evidence = {
            "url": page.url,
            "embedded_url": page.locator("[data-settings-embed-frame]").get_attribute("src"),
            "buyout_rate_pct": parameters.get("buyout_rate_pct"),
            "included_expense_rate_pct": parameters.get("included_expense_rate_pct"),
            "retained_share_pct": parameters.get("retained_share_pct"),
            "effective_date": (settings_payload.get("current") or {}).get("effective_date"),
            "reference_weeks": reference.get("weeks"),
            "history_count": len(settings_payload.get("history") or []),
            "screenshot": str(settings_screenshot),
        }

        supplier_registry_url = normalized_base_url + "/sheet-vitrina-v1/vitrina?tab=factory-order"
        supplier_registry_response = page.goto(
            supplier_registry_url,
            wait_until="domcontentloaded",
            timeout=120_000,
        )
        _assert(
            supplier_registry_response is not None and supplier_registry_response.status == 200,
            "supplier registry shell status",
        )
        supplier_operator = page.frame_locator('[data-operator-embed-frame="factory-order"]')
        supplier_operator.locator('[data-supply-mode-button="shipment-registry"]').click()
        supplier_operator.locator(
            '[data-supply-mode-panel="shipment-registry"]:not([hidden])'
        ).wait_for(timeout=60_000)
        supplier_operator.get_by_text("Средняя себестоимость: на производстве", exact=True).wait_for(
            timeout=60_000
        )
        supplier_operator.get_by_text("Средняя себестоимость: Китай → FF", exact=True).wait_for(
            timeout=60_000
        )
        supplier_registry_embedded_url = supplier_operator.locator("body").evaluate(
            "element => element.ownerDocument.location.href"
        )
        supplier_registry_screenshot = evidence_dir / "supplier_registry_stage_costs.png"
        page.screenshot(path=str(supplier_registry_screenshot), full_page=True)
        screenshots.append(str(supplier_registry_screenshot))
        registry_api = context.request.get(
            normalized_base_url + "/v1/sheet-vitrina-v1/supply/supplier-shipments/registry",
            headers={"Accept": "application/json"},
            timeout=60_000,
        )
        _assert(registry_api.status == 200, "supplier registry API status")
        registry_payload = registry_api.json()
        registry_json = json.dumps(registry_payload, ensure_ascii=False)
        _assert("production_average_cost_rub" in registry_json, "supplier production cost field")
        _assert("china_to_ff_average_cost_rub" in registry_json, "supplier China-to-FF cost field")
        bank_fee_payload: dict[str, Any] | None = None
        bank_fee_shipment_id = ""
        for column in reversed(list(registry_payload.get("columns") or [])):
            shipment_id = str((column or {}).get("shipment_id") or "").strip()
            if not shipment_id:
                continue
            financial_api = context.request.get(
                normalized_base_url
                + "/v1/sheet-vitrina-v1/supply/supplier-shipments/"
                + quote(shipment_id, safe="")
                + "/financial-documents",
                headers={"Accept": "application/json"},
                timeout=60_000,
            )
            _assert(financial_api.status == 200, f"supplier financial API status: {shipment_id}")
            candidate = financial_api.json()
            exact_fee = ((candidate.get("summary") or {}).get("per_unit") or {}).get("exact_bank_fees_rub")
            if exact_fee is not None and Decimal(str(exact_fee)) > 0:
                bank_fee_payload = candidate
                bank_fee_shipment_id = shipment_id
                break
        _assert(bank_fee_payload is not None, "supplier shipment with confirmed positive bank fees")
        bank_fee_lines = [
            dict(item)
            for item in (bank_fee_payload or {}).get("expense_lines") or []
            if str(item.get("stage") or "") == "bank_fee"
            and Decimal(str(item.get("amount") or 0)) > 0
        ]
        _assert(bank_fee_lines, "supplier bank fee detail lines")
        _assert(
            all(str((item.get("raw") or {}).get("source") or "") == "bank_fee_statement" for item in bank_fee_lines),
            "supplier bank fee provenance",
        )
        _assert(
            all(str(item.get("currency") or "") in {"RUB", "CNY"} for item in bank_fee_lines),
            "supplier bank fee source currencies",
        )
        _assert(
            all(Decimal(str(item.get("amount_rub") or 0)) > 0 for item in bank_fee_lines),
            "supplier bank fee RUB equivalents",
        )
        supplier_url = normalized_base_url + "/sheet-vitrina-v1/supplier"
        supplier_detail_url = supplier_url + "?shipment_id=" + quote(bank_fee_shipment_id, safe="") + "&tab=documents"
        supplier_detail_response = page.goto(supplier_detail_url, wait_until="domcontentloaded", timeout=120_000)
        _assert(supplier_detail_response is not None and supplier_detail_response.status == 200, "supplier fee detail page status")
        page.locator("#shipmentCard:not([hidden])").wait_for(timeout=60_000)
        page.wait_for_function(
            "document.querySelector('[data-bank-fee-total]').textContent.trim() !== '-'",
            timeout=60_000,
        )
        parent_fee_text = page.locator("[data-bank-fee-total]").inner_text().strip()
        _assert(_visible_money(parent_fee_text) > 0, "supplier bank fee parent total")
        fee_lines_by_document: dict[str, list[dict[str, Any]]] = {}
        for item in bank_fee_lines:
            document_id = str(item.get("financial_document_id") or "")
            if document_id:
                fee_lines_by_document.setdefault(document_id, []).append(item)
        _assert(fee_lines_by_document, "supplier bank fee source document")
        fee_document_id, selected_fee_lines = max(
            fee_lines_by_document.items(),
            key=lambda item: (len(item[1]), item[0]),
        )
        fee_document_row = page.locator(f'[data-financial-document-row="{fee_document_id}"]')
        fee_document_row.wait_for(timeout=60_000)
        fee_document_row.click()
        visible_fee_rows = page.locator('#financialExpenseRows tr[data-expense-source="bank_fee_statement"]')
        _assert(visible_fee_rows.count() == len(selected_fee_lines), "visible supplier bank fee detail lines")
        _assert(
            all("bank_fee_statement" in visible_fee_rows.nth(index).inner_text() for index in range(visible_fee_rows.count())),
            "visible supplier bank fee provenance",
        )
        _assert(
            {visible_fee_rows.nth(index).get_attribute("data-expense-currency") for index in range(visible_fee_rows.count())}
            == {str(item.get("currency") or "") for item in selected_fee_lines},
            "visible supplier bank fee currencies",
        )
        supplier_screenshot = evidence_dir / "supplier_registry_costs.png"
        page.screenshot(path=str(supplier_screenshot), full_page=True)
        screenshots.append(str(supplier_screenshot))
        supplier_evidence = {
            "url": page.url,
            "registry_url": supplier_registry_url,
            "registry_embedded_url": supplier_registry_embedded_url,
            "registry_status": registry_payload.get("status"),
            "production_cost_field": True,
            "china_to_ff_cost_field": True,
            "bank_commissions_visible": True,
            "bank_fee_shipment_id": bank_fee_shipment_id,
            "bank_fee_total": parent_fee_text,
            "bank_fee_line_count": len(bank_fee_lines),
            "bank_fee_currencies": sorted({str(item.get("currency") or "") for item in bank_fee_lines}),
            "bank_fee_sources": sorted({str((item.get("raw") or {}).get("source") or "") for item in bank_fee_lines}),
            "registry_screenshot": str(supplier_registry_screenshot),
            "screenshot": str(supplier_screenshot),
        }

        vitrina_url = normalized_base_url + "/sheet-vitrina-v1/vitrina?tab=warehouses&warehouse=production"
        vitrina_response = page.goto(vitrina_url, wait_until="domcontentloaded", timeout=120_000)
        _assert(vitrina_response is not None and vitrina_response.status == 200, "vitrina consumer page status")
        page.locator("[data-open-stock-report]").click()
        report_frame = page.locator('[data-warehouse-stock-report-frame]:not([hidden])')
        report_frame.wait_for(timeout=60_000)
        report_body = report_frame.content_frame.locator("body")
        report_body.wait_for(timeout=60_000)
        _assert("Отчёт по остаткам" in report_body.inner_text(), "stock report navigation")
        stock_report_screenshot = evidence_dir / "stock_report_navigation.png"
        page.screenshot(path=str(stock_report_screenshot), full_page=False)
        screenshots.append(str(stock_report_screenshot))
        page.locator('[data-unified-tab-button="sku-management"]').click()
        page.locator('[data-unified-tab-panel="sku-management"]:not([hidden])').wait_for(timeout=60_000)
        _assert(bool(page.locator('[data-unified-tab-panel="sku-management"]').inner_text().strip()), "SKU management visible render")
        sku_api = context.request.get(
            normalized_base_url + "/v1/sheet-vitrina-v1/sku-management",
            headers={"Accept": "application/json"},
            timeout=120_000,
        )
        _assert(sku_api.status == 200, "SKU management protected API status")
        sku_payload = sku_api.json()
        sku_rows_with_proxy_3 = [
            item
            for item in sku_payload.get("rows") or []
            if item.get("profit_rub") is not None and item.get("margin_pct") is not None
        ]
        _assert(sku_rows_with_proxy_3, "SKU management consumes populated Proxy 3")
        sku_screenshot = evidence_dir / "sku_management_consumer.png"
        page.screenshot(path=str(sku_screenshot), full_page=False)
        screenshots.append(str(sku_screenshot))
        page.locator('[data-unified-tab-button="vitrina"]').click()
        page.locator('[data-unified-tab-panel="vitrina"]:not([hidden])').wait_for(timeout=60_000)
        page.wait_for_function(
            "document.body.innerText.includes('proxy прибыль 3') && document.body.innerText.includes('Прокси маржинальность 3')",
            timeout=120_000,
        )
        filled_metrics = {
            metric_key: _filled_metric_cells(page, metric_key=metric_key, date_from="2026-07-01")
            for metric_key in (
                "our_wb_unit_cost_rub",
                "proxy_profit_3_rub",
                "proxy_margin_3_pct",
            )
        }
        _assert(
            all(count > 0 for count in filled_metrics.values()),
            "canonical WB cost and Proxy 3 are filled from 2026-07-01 where persisted inputs exist",
        )
        proxy_screenshot = evidence_dir / "proxy3_vitrina.png"
        page.screenshot(path=str(proxy_screenshot), full_page=False)
        screenshots.append(str(proxy_screenshot))
        consumer_evidence = {
            "stock_report_navigation": True,
            "sku_management_visible": True,
            "sku_management_proxy_3_row_count": len(sku_rows_with_proxy_3),
            "proxy_profit_3_visible": True,
            "proxy_margin_3_visible": True,
            "filled_metric_cells_from_2026_07_01": filled_metrics,
            "screenshots": [str(stock_report_screenshot), str(sku_screenshot), str(proxy_screenshot)],
        }
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
        "calculation_parameters": settings_evidence,
        "supplier_registry": supplier_evidence,
        "dependent_consumers": consumer_evidence,
        "hourly_sync": dict(expected_readback.get("sync") or {}),
        "historical_wb_cost_projection": dict(expected_readback.get("historical_wb_cost_projection") or {}),
        "legacy_ff_transition": True,
        "legacy_ff_reconciliation": {
            "document_id": str(legacy_ff_expected["document_id"]),
            "sku_count": int(legacy_ff_expected["sku_count"]),
            "total_quantity": str(legacy_ff_expected["total_quantity"]),
            "balance_rows": int(legacy_ff_expected["balance_rows"]),
            "economics_loaded": True,
            "loaded_before_screenshot": True,
        },
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


def _settings_frame_locator(page: Page) -> FrameLocator:
    frame = page.locator("[data-settings-embed-frame]")
    frame.wait_for(state="visible", timeout=60_000)
    page.wait_for_function(
        "Boolean(document.querySelector('[data-settings-embed-frame]')?.getAttribute('src'))",
        timeout=60_000,
    )
    surface = page.frame_locator("[data-settings-embed-frame]")
    surface.locator("body").wait_for(timeout=60_000)
    return surface


def _visible_money(value: str) -> Decimal:
    normalized = (
        str(value or "")
        .replace("\u00a0", "")
        .replace("\u202f", "")
        .replace(" ", "")
        .replace("₽", "")
        .replace("RUB", "")
        .replace("CNY", "")
        .replace(",", ".")
    )
    try:
        return Decimal(normalized)
    except Exception as exc:
        raise AssertionError(f"visible money value is not numeric: {value!r}") from exc


def _filled_metric_cells(page: Page, *, metric_key: str, date_from: str) -> int:
    cells = page.locator(f'td[data-metric-key="{metric_key}"][data-cell-date]')
    filled = 0
    for index in range(cells.count()):
        cell = cells.nth(index)
        if str(cell.get_attribute("data-cell-date") or "") < date_from:
            continue
        text = cell.inner_text().strip()
        if text and text not in {"—", "-"}:
            filled += 1
    return filled


def _assert(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
