"""Playwright smoke for the compact Stage 3 FF facility/pool operator modal."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import socket
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_FF_POOL_DOCUMENTS_PATH,
    DEFAULT_FF_POOL_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_SUPPLIER_UI_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_SETTINGS_UI_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.ff_pool_foundation import FEATURE_EPOCHS_TABLE  # noqa: E402
from packages.application.ff_pool_surfaces import FfPoolSurface  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 12, 7, 0, tzinfo=timezone.utc)

    def __call__(self) -> str:
        result = self.value.isoformat(timespec="seconds").replace("+00:00", "Z")
        self.value += timedelta(seconds=1)
        return result


def main() -> None:
    with TemporaryDirectory(prefix="ff-pool-browser-") as directory:
        root = Path(directory)
        runtime_dir = root / "runtime"
        runtime_dir.mkdir()
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime.list_ff_stock_operations(limit=1)
        clock = Clock()
        _seed(runtime, clock)
        _seed_guided_supplier_shipment(runtime)
        guided_upload_path = root / "guided-acceptance.xlsx"
        guided_upload_path.write_bytes(b"browser fixture intercepted before parsing")
        overhead_upload_path = root / "synthetic-payment.pdf"
        overhead_upload_path.write_bytes(b"%PDF-browser-synthetic-fixture")
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=_reserve_free_port(),
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path="/v1/sheet-vitrina-v1/refresh",
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime_dir,
        )
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=clock,
        )
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    screenshot_path = Path(os.environ.get("FF_POOL_SCREENSHOT_PATH") or root / "mobile.png")
                    _run(
                        browser,
                        f"http://127.0.0.1:{config.port}",
                        screenshot_path,
                        guided_upload_path,
                        overhead_upload_path,
                    )
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print("ff_pool_surfaces_browser_smoke: OK")


def _seed(runtime: RegistryUploadDbBackedRuntime, clock: Clock) -> None:
    with sqlite3.connect(runtime.db_path) as conn:
        conn.execute(
            f"INSERT INTO {FEATURE_EPOCHS_TABLE}(epoch,writer_enabled,reader_enabled,source_revision,created_at,metadata_json) "
            "VALUES(1,1,0,'browser-writer',?,'{}')",
            (clock(),),
        )
        conn.commit()
    surface = FfPoolSurface(db_path=runtime.db_path, runtime_dir=runtime.runtime_dir, timestamp_factory=clock)
    for request_id, name, city in (
        ("browser:facility:one", "Москва Север", "Москва"),
        ("browser:facility:two", "Оренбург", "Оренбург"),
        ("browser:facility:xss", "<img src=x onerror=window.__ffPoolXss=1>", "Москва"),
    ):
        surface.create_facility(
            {
                "request_id": request_id,
                "name": name,
                "city": city,
                "active": name != "Оренбург",
                "display_timezone": "Asia/Yekaterinburg",
            },
            actor="browser-fixture",
        )


def _seed_guided_supplier_shipment(runtime: RegistryUploadDbBackedRuntime) -> None:
    runtime.save_supplier_shipment(
        header={
            "shipment_id": "sup_guided_csrf_browser",
            "created_at": "2026-08-12T07:05:00Z",
            "updated_at": "2026-08-12T07:05:00Z",
            "shipment_date": "2026-08-10",
            "actual_shipment_date": "2026-08-11",
            "actual_ff_acceptance_date": None,
            "order_status": "in_transit",
            "invoice_no": "GUIDED-CSRF",
            "invoice_date": "2026-08-09",
            "contract_no": "",
            "contract_date": "",
            "supplier_name": "Browser fixture supplier",
            "customer_name": "",
            "currency": "RMB",
            "product_qty_total": 1,
            "product_amount_total": 1,
            "extras_amount_total": 0,
            "invoice_amount_total": 1,
            "declared_invoice_total": 1,
            "match_status": "all_matched",
            "source_filename": "guided-csrf.xlsx",
            "source_file_sha256": "",
            "source_file_path": "",
            "parser_version": "browser-smoke",
            "warnings": [],
            "errors": [],
        },
        lines=[
            {
                "line_id": "ln_guided_csrf_browser",
                "line_type": "product",
                "sort_order": 1,
                "source_no": "1",
                "product_type": "clear",
                "model_raw": "Browser fixture",
                "model_normalized": "browser_fixture",
                "match_key": "clear|browser_fixture",
                "internal_sku": "SKU-GUIDED-CSRF",
                "internal_nm_id": 210183919,
                "internal_name": "Browser fixture",
                "qty": 1,
                "unit_price": 1,
                "amount": 1,
                "currency": "RMB",
                "comment": "",
                "match_status": "matched",
                "manual_override": False,
                "raw": {},
            }
        ],
    )


def _run(
    browser: object,
    base: str,
    screenshot_path: Path,
    guided_upload_path: Path,
    overhead_upload_path: Path,
) -> None:
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page_errors: list[str] = []
    console_errors: list[str] = []
    server_errors: list[str] = []
    pool_http_errors: list[str] = []
    facility_mutation_headers: list[dict[str, str]] = []
    guided_mutation_requests: list[dict[str, object]] = []
    overhead_mutation_requests: list[dict[str, object]] = []
    generic_preview_payloads: list[dict[str, object]] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
    page.on("response", lambda response: server_errors.append(f"{response.status} {response.url}") if response.status >= 500 else None)
    page.on("response", lambda response: pool_http_errors.append(f"{response.status} {response.url}") if response.status >= 400 and "/facility-pools" in response.url else None)
    page.on(
        "request",
        lambda request: facility_mutation_headers.append(request.all_headers())
        if request.method == "POST" and "/facility-pools/facilities/" in request.url
        else None,
    )
    page.on(
        "request",
        lambda request: generic_preview_payloads.append(request.post_data_json)
        if request.method == "POST" and request.url.endswith("/documents/preview")
        else None,
    )

    def intercept_overhead_mutation(route: object) -> None:
        request = route.request
        overhead_mutation_requests.append(
            {
                "headers": request.all_headers(),
                "post_data": request.post_data or "",
            }
        )
        payload = {
            "request_id": "overhead:browser:" + str(len(overhead_mutation_requests)),
            "document_kind": "pool_overhead",
            "document_label_ru": "Накладные расходы FBS/FBO",
            "state": "ready",
            "state_label_ru": "Готово к проведению",
            "confirm_allowed": True,
            "steps": [],
            "preview": {
                "available": True,
                "summary": {
                    "facility_id": "fixture-browser-facility",
                    "scope": "FBS",
                    "category": "other",
                    "category_label_ru": "Прочие",
                    "comment": "Синтетический браузерный расход",
                    "amount_rub": "1.23",
                    "source_mode": "payment_order_pdf" if str(request.all_headers().get("content-type", "")).startswith("multipart/form-data;") else "manual",
                    "business_date": "2026-08-12",
                    "denominator_quantity": 3,
                    "denominator_sku_count": 2,
                    "affected_sku_count": 2,
                    "allocation_total_rub": "1.23",
                    "payment_evidence": {},
                },
            },
        }
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route(f"**{DEFAULT_FF_POOL_DOCUMENTS_PATH}/overhead/preview", intercept_overhead_mutation)

    def intercept_guided_mutation(route: object) -> None:
        request = route.request
        if request.method != "POST":
            route.continue_()
            return
        guided_mutation_requests.append(
            {
                "url": request.url,
                "headers": request.all_headers(),
                "content_type": request.all_headers().get("content-type", ""),
            }
        )
        if request.url.endswith("/documents/china/preview"):
            payload = {
                "request_id": "guided:browser-csrf",
                "state": "ready",
                "state_label_ru": "Готово к проведению",
                "confirm_allowed": True,
                "guided_acceptance_activation": {"reason_ru": "Локальный mutation fixture."},
                "preview": {
                    "available": False,
                    "summary": {
                        "expected_quantity": 1,
                        "accepted_quantity": 1,
                        "quantity_fbs": 1,
                        "quantity_fbo": 0,
                        "discrepancy_quantity": 0,
                        "capital_normalization": {
                            "exact_total_rub": "20.0049",
                            "canonical_total_rub": "20.00",
                            "total_residual_rub": "-0.0049",
                        },
                    },
                },
            }
        else:
            payload = {
                "request_id": "guided:browser-csrf",
                "state": "complete",
                "state_label_ru": "Завершено",
                "confirm_allowed": False,
                "business_date": "2026-08-12",
                "guided_acceptance_activation": {"reason_ru": "Локальный mutation fixture."},
                "summary": {
                    "expected_quantity": 1,
                    "accepted_quantity": 1,
                    "quantity_fbs": 1,
                    "quantity_fbo": 0,
                    "discrepancy_quantity": 0,
                },
            }
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    url = f"{base}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}?tab=warehouses&warehouse=ff"
    response = page.goto(url, wait_until="domcontentloaded")
    assert response is not None and response.status == 200
    page.locator('[data-unified-tab-button="warehouses"]').click()
    page.locator('[data-warehouse-key="ff"]').click()
    page.locator("[data-inventory-planning-card]").wait_for(state="visible")
    assert "Оперостаток" not in page.locator("[data-warehouses-panel]").inner_text()
    page.locator("[data-open-fbs-orders]").click()
    page.locator("[data-fbs-orders-view]").wait_for(state="visible")
    page.locator("[data-fbs-order-counters] .fbs-order-counter").first.wait_for(state="visible")
    assert page.locator("[data-fbs-order-counters] .fbs-order-counter").count() == 9
    assert page.locator("[data-fbs-orders-filters] input, [data-fbs-orders-filters] select").count() == 6
    page.locator("[data-open-warehouse-costs]").click()
    launcher = page.locator("[data-ff-pool-open]")
    launcher.wait_for(state="visible")
    launcher.focus()
    launcher.click()
    dialog = page.get_by_role("dialog", name="Документы фулфилмента")
    dialog.wait_for(state="visible")
    page.locator("[data-ff-pool-facilities] .ff-pool-list-item").first.wait_for(state="visible")
    assert page.locator("[data-ff-pool-facilities] .ff-pool-list-item").count() == 3
    assert page.locator("[data-ff-pool-facilities] img").count() == 0
    assert page.evaluate("window.__ffPoolXss") is None
    page.locator("[data-ff-pool-facilities] .ff-pool-list-item", has_text="Москва Север").get_by_role("button", name="Открыть").click()
    page.locator("[data-ff-pool-facility-detail] h3").wait_for(state="visible")
    assert "Москва Север" in page.locator("[data-ff-pool-facility-detail]").inner_text()
    page.get_by_role("button", name="FBS-заказы этого склада").click()
    page.locator("[data-fbs-orders-view]").wait_for(state="visible")
    assert page.locator("[data-fbs-orders-facility]").input_value()
    page.locator("[data-open-warehouse-costs]").click()
    page.locator("[data-ff-pool-open]").click()
    dialog.wait_for(state="visible")

    page.locator('[data-ff-pool-tab="create"]').click()
    page.locator("[data-ff-pool-action-kind]").select_option("pool_inventory")
    page.locator("[data-ff-pool-scope]").select_option("FBS")
    assert page.locator('[data-ff-pool-field="scope"]').is_visible()
    assert page.locator('[data-ff-pool-field="workbook"]').is_visible()
    assert page.locator("[data-ff-pool-template]").is_visible()
    assert "Явный 0 в полном FBS-шаблоне" in page.locator(
        "[data-ff-pool-action-help]"
    ).inner_text()
    page.locator("[data-ff-pool-action-kind]").select_option("pool_overhead")
    visible_overhead_fields = {
        "facility", "scope", "amount", "category", "comment", "payment_file"
    }
    for field in (
        "facility", "scope", "amount", "category", "comment", "payment_file", "payment_evidence",
        "source_pool", "destination_pool", "destination_facility", "root", "target", "items", "reason", "expenses", "shipment", "workbook",
    ):
        locator = page.locator(f'[data-ff-pool-field="{field}"]')
        assert locator.is_visible() == (field in visible_overhead_fields), field
        if field not in visible_overhead_fields:
            assert locator.locator("input,select,textarea").count() == 0 or locator.locator("input,select,textarea").first.is_disabled(), field
    assert page.locator("[data-ff-pool-business-date]").is_visible()
    assert page.locator("[data-ff-pool-business-date]").get_attribute("readonly") is not None
    assert page.locator("[data-ff-pool-business-date]").input_value() == ""
    assert page.locator("[data-ff-pool-facility]").input_value() == ""
    assert page.locator("[data-ff-pool-scope]").input_value() == ""
    assert page.locator("[data-ff-pool-overhead-category]").input_value() == ""
    page.locator("[data-ff-pool-facility]").select_option(index=1)
    assert page.locator("[data-ff-pool-business-date]").input_value() == "2026-08-12"
    page.locator("[data-ff-pool-scope]").select_option("FBS")
    page.locator("[data-ff-pool-overhead-category]").select_option("other")
    assert page.locator("[data-ff-pool-overhead-comment]").get_attribute("required") is not None
    page.locator("[data-ff-pool-overhead-comment]").fill("Синтетический браузерный расход")
    page.locator("[data-ff-pool-amount]").fill("1.23")
    page.locator("[data-ff-pool-preview]").click()
    page.locator("[data-ff-pool-workflow-detail] h3").wait_for(state="visible")
    assert len(overhead_mutation_requests) == 1
    manual_request = overhead_mutation_requests[0]
    assert str(dict(manual_request["headers"]).get("content-type", "")).startswith("application/json")
    manual_body = json.loads(str(manual_request["post_data"]))
    assert manual_body["facility_id"] and manual_body["scope"] == "FBS"
    assert manual_body["category"] == "other" and manual_body["amount_rub"] == "1.23"

    page.locator('[data-ff-pool-tab="create"]').click()
    page.locator("[data-ff-pool-overhead-file]").set_input_files(str(overhead_upload_path))
    assert page.locator('[data-ff-pool-field="payment_evidence"]').is_visible()
    assert page.locator("[data-ff-pool-overhead-file-remove]").is_visible()
    assert page.locator("[data-ff-pool-amount]").is_editable() is False
    assert page.locator("[data-ff-pool-amount]").input_value() == ""
    page.locator("[data-ff-pool-overhead-file-remove]").click()
    assert page.locator("[data-ff-pool-amount]").is_editable()
    assert page.locator("[data-ff-pool-overhead-file]").input_value() == ""
    page.locator("[data-ff-pool-overhead-file]").set_input_files(str(overhead_upload_path))
    page.locator("[data-ff-pool-preview]").click()
    page.locator("[data-ff-pool-workflow-detail] h3").wait_for(state="visible")
    assert len(overhead_mutation_requests) == 2
    assert str(dict(overhead_mutation_requests[1]["headers"]).get("content-type", "")).startswith("multipart/form-data;")

    page.locator('[data-ff-pool-tab="create"]').click()
    page.locator("[data-ff-pool-action-kind]").select_option("transfer_root")
    assert not page.locator('[data-ff-pool-field="category"]').is_visible()
    assert page.locator("[data-ff-pool-overhead-category]").is_disabled()
    assert page.locator("[data-ff-pool-overhead-category]").input_value() == ""
    assert page.locator("[data-ff-pool-overhead-comment]").input_value() == ""
    assert page.locator("[data-ff-pool-amount]").input_value() == ""
    assert page.locator("[data-ff-pool-overhead-file]").input_value() == ""
    source = page.locator("[data-ff-pool-facility]")
    destination = page.locator("[data-ff-pool-destination-facility]")
    source.select_option(index=1)
    destination.select_option(index=2)
    page.locator("[data-ff-pool-source-pool]").select_option("FBS")
    page.locator("[data-ff-pool-destination-pool]").select_option("FBO")
    page.locator("[data-ff-pool-preview]").click()
    page.locator("[data-ff-pool-workflow-detail] h3").wait_for(state="visible")
    assert generic_preview_payloads
    transfer_manifest = dict(generic_preview_payloads[-1]["manifest"])
    assert set(transfer_manifest) == {"source", "destination"}
    assert not ({"amount_rub", "category", "comment", "payment_evidence"} & set(transfer_manifest))
    assert "Готово к проведению" in page.locator("[data-ff-pool-workflow-detail]").inner_text()
    page.get_by_role("button", name="Подтвердить проведение").click()
    page.wait_for_function("document.querySelector('[data-ff-pool-workflow-detail] h3')?.textContent.includes('Завершено')")
    saved_request = page.locator("[data-ff-pool-request-id]").input_value()
    assert saved_request.startswith("ffpdr_")

    page.reload(wait_until="domcontentloaded")
    page.locator('[data-unified-tab-button="warehouses"]').click()
    page.locator('[data-warehouse-key="ff"]').click()
    page.locator("[data-ff-pool-open]").click()
    page.locator('[data-ff-pool-tab="workflow"]').click()
    page.locator("[data-ff-pool-workflow-detail] h3").wait_for(state="visible")
    assert page.locator("[data-ff-pool-request-id]").input_value() == saved_request
    assert "Завершено" in page.locator("[data-ff-pool-workflow-detail]").inner_text()

    page.set_viewport_size({"width": 390, "height": 844})
    page.locator('[data-ff-pool-tab="facilities"]').click()
    page.wait_for_timeout(100)
    assert dialog.evaluate("node => node.scrollWidth <= node.clientWidth + 1")
    page.screenshot(path=str(screenshot_path), full_page=False)
    page.keyboard.press("Escape")
    page.locator("[data-ff-pool-modal]").wait_for(state="hidden")
    assert page.evaluate("document.activeElement === document.querySelector('[data-ff-pool-open]')")

    response = page.goto(f"{base}{DEFAULT_SETTINGS_UI_PATH}?embedded=1#warehouses", wait_until="domcontentloaded")
    assert response is not None and response.status == 200
    page.get_by_role("button", name="Склады").click()
    page.locator("[data-facility-id]").first.wait_for(state="visible")
    assert page.locator("[data-facility-id]").count() == 3
    settings_text = page.locator("#warehousesGroupPanel").inner_text()
    assert "Системные пулы: FBS · FBO" in settings_text
    assert "Review range начинается с 2026-08-01" in settings_text
    assert "Default-off" in settings_text
    assert "Адрес в MVP отсутствует" in settings_text
    assert page.locator("#warehousesGroupPanel img").count() == 0
    assert page.evaluate("window.__ffPoolXss") is None
    orenburg = page.locator('[data-facility-id]', has_text="Оренбург")
    assert orenburg.get_attribute("data-active") == "false"
    orenburg.get_by_role("button", name="Активировать").click()
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('[data-facility-id]')).some((node) => node.textContent.includes('Оренбург') && node.dataset.active === 'true')"
    )
    assert facility_mutation_headers
    assert facility_mutation_headers[-1].get("x-wb-ff-pool-csrf") == "1"
    assert page.locator("#warehousesGroupPanel").evaluate("node => node.scrollWidth <= node.clientWidth + 1")
    page.screenshot(path=str(screenshot_path.with_name(screenshot_path.stem + "-settings" + screenshot_path.suffix)), full_page=False)

    page.route(f"**{DEFAULT_FF_POOL_DOCUMENTS_PATH}/china/preview", intercept_guided_mutation)
    page.route(f"**{DEFAULT_FF_POOL_PATH}/requests/*/confirm", intercept_guided_mutation)
    response = page.goto(f"{base}{DEFAULT_SHEET_SUPPLIER_UI_PATH}?embedded=operator", wait_until="domcontentloaded")
    assert response is not None and response.status == 200
    guided_row = page.locator('#shipmentRows tr[data-row="sup_guided_csrf_browser"]')
    guided_row.wait_for(state="visible")
    guided_row.click()
    page.locator("#shipmentCard").wait_for(state="visible")
    page.get_by_role("tab", name="Состав поставки").click()
    guided_acceptance_button = page.locator("#guidedAcceptanceButton")
    guided_acceptance_button.wait_for(state="visible")
    assert guided_acceptance_button.inner_text() == "Принять на FF"
    guided_acceptance_button.click()
    page.locator("#guidedAcceptanceModal").wait_for(state="visible")
    page.locator("#guidedAcceptanceFile").set_input_files(str(guided_upload_path))
    page.get_by_role("button", name="Необратимо провести приёмку").wait_for(state="visible")
    guided_status = page.locator("#guidedAcceptanceStatus").inner_text()
    assert "Капитал к проведению: 20.00 ₽" in guided_status
    assert "остаток нормализации: -0.0049 ₽" in guided_status
    assert len(guided_mutation_requests) == 1
    upload_request = guided_mutation_requests[0]
    assert str(upload_request["url"]).endswith(f"{DEFAULT_FF_POOL_DOCUMENTS_PATH}/china/preview")
    assert dict(upload_request["headers"]).get("x-wb-ff-pool-csrf") == "1"
    assert str(upload_request["content_type"]).startswith("multipart/form-data;")
    page.get_by_role("button", name="Необратимо провести приёмку").click()
    page.wait_for_function(
        "() => document.querySelector('#guidedAcceptanceStatus')?.textContent.includes('Завершено')",
        timeout=5000,
    )
    assert len(guided_mutation_requests) == 2
    confirm_request = guided_mutation_requests[1]
    assert str(confirm_request["url"]).endswith("/requests/guided%3Abrowser-csrf/confirm")
    assert dict(confirm_request["headers"]).get("x-wb-ff-pool-csrf") == "1"
    assert str(confirm_request["content_type"]).startswith("application/json")
    assert not page_errors, page_errors
    fatal_console_errors = [item for item in console_errors if not item.startswith("Failed to load resource:")]
    assert not fatal_console_errors, fatal_console_errors
    assert not server_errors, server_errors
    assert not pool_http_errors, pool_http_errors
    context.close()


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


if __name__ == "__main__":
    main()
