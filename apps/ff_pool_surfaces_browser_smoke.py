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

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


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
from packages.application.wb_fbs_warehouse_registry import (  # noqa: E402
    REGISTRY_ROWS_TABLE,
    REGISTRY_RUNS_TABLE,
    STOCK_ROWS_TABLE,
    STOCK_RUNS_TABLE,
    ensure_wb_fbs_warehouse_registry_schema,
)
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 12, 7, 0, tzinfo=timezone.utc)

    def __call__(self) -> str:
        result = self.value.isoformat(timespec="seconds").replace("+00:00", "Z")
        self.value += timedelta(seconds=1)
        return result


def main() -> None:
    if sync_playwright is None:
        print("ff_pool_surfaces_browser_smoke: SKIP (Playwright unavailable)")
        return
    with sync_playwright() as browser_check:
        if not Path(browser_check.chromium.executable_path).exists():
            print("ff_pool_surfaces_browser_smoke: SKIP (Chromium unavailable)")
            return
    with TemporaryDirectory(prefix="ff-pool-browser-") as directory:
        root = Path(directory)
        runtime_dir = root / "runtime"
        runtime_dir.mkdir()
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime.list_ff_stock_operations(limit=1)
        clock = Clock()
        _seed(runtime, clock)
        _seed_guided_supplier_shipment(runtime)
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
    with sqlite3.connect(runtime.db_path) as conn:
        ensure_wb_fbs_warehouse_registry_schema(conn)
        conn.execute(
            f"""INSERT INTO {REGISTRY_RUNS_TABLE}(
                   run_id,status,complete,started_at,completed_at,warehouse_count,
                   office_count,source_digest,error
               ) VALUES('browser-registry','success',1,?,?,2,1,'sha256:browser-registry','')""",
            (clock(), clock()),
        )
        conn.executemany(
            f"""INSERT INTO {REGISTRY_ROWS_TABLE}(
                   run_id,seller_warehouse_id,office_id,warehouse_name,office_name,
                   office_city,office_federal_district,cargo_type,delivery_type,
                   is_deleting,is_processing,evidence_digest
               ) VALUES('browser-registry',?,?,?,?,?,?,?,?,0,0,?)""",
            (
                (71001, 81, "WB Север", "Офис WB", "Москва", "ЦФО", 1, 1, "sha256:wb-71001"),
                (71002, 81, "WB Восток", "Офис WB", "Москва", "ЦФО", 1, 1, "sha256:wb-71002"),
            ),
        )
        conn.execute(
            f"""INSERT INTO {STOCK_RUNS_TABLE}(
                   run_id,registry_run_id,seller_warehouse_id,status,complete,
                   snapshot_at,requested_chrt_count,returned_chrt_count,
                   identity_scope_json,source_digest,error
               ) VALUES('browser-stock-71001','browser-registry',71001,'success',1,?,1,1,'{{\"complete\":true}}','sha256:stock-71001','')""",
            (clock(),),
        )
        conn.execute(
            f"""INSERT INTO {STOCK_ROWS_TABLE}(
                   run_id,seller_warehouse_id,chrt_id,nm_id,amount,evidence_digest
               ) VALUES('browser-stock-71001',71001,99101,101,7,'sha256:stock-row')"""
        )
        conn.commit()


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

    guided_hanging_routes = []
    guided_status = {"state":"not_found"}
    guided_transport = {"lose_preview":False, "lose_confirm":False, "hang_confirm":False}

    def intercept_guided_source(route: object) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "shipment_id":"sup_guided_csrf_browser", "source_revision":"sha256:" + "a" * 64,
            "activation":{"effective":True},
            "facilities":[{"facility_id":"fac_msk","name":"Москва"},{"facility_id":"fac_orb","name":"Оренбург"}],
            "lines":[{"nm_id":101,"sku":"SKU 101","barcode":"00101","quantity":10}, {"nm_id":202,"sku":"SKU 202","barcode":"00202","quantity":6}],
        }))

    def intercept_guided_mutation(route: object) -> None:
        nonlocal guided_status
        request = route.request
        body = request.post_data_json
        guided_mutation_requests.append({"url":request.url,"headers":request.all_headers(),"body":body})
        if request.url.endswith("/form/preview"):
            if guided_transport["lose_preview"]:
                guided_status = {"state":"not_found"}
                route.abort()
                return
            guided_status = {
                "request_id":body["request_id"], "state":"ready", "confirm_allowed":True,
                "business_date":body["business_date"], "preview":{"summary":{"facility_id":body["facility_id"]}},
            }
        else:
            guided_status = {**guided_status,"state":"posted","confirm_allowed":False,"document":{"document_id":"doc-browser"}}
            if guided_transport["hang_confirm"]:
                guided_hanging_routes.append(route)
                return
            if guided_transport["lose_confirm"]:
                route.abort()
                return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(guided_status))

    def intercept_guided_status(route: object) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(guided_status))


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
    assert page.locator("[data-fbs-order-counters] .fbs-order-counter").count() == 10
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
    wb_registry = page.locator("[data-ff-pool-wb-warehouses]")
    wb_registry.locator(".ff-pool-list-item", has_text="WB Север").wait_for(
        state="visible"
    )
    wb_north = wb_registry.locator(".ff-pool-list-item", has_text="WB Север")
    assert "Не привязан" in wb_north.inner_text()
    assert "WB ID 71001" in wb_north.inner_text()
    assert "WB declared 7" in wb_north.inner_text()
    assert "internal physical н/д" in wb_north.inner_text()
    wb_north.get_by_role("button", name="Привязать").click()
    binding_form = page.locator("[data-ff-pool-facility-detail] form")
    binding_form.get_by_label("Внутренний склад FF").select_option(
        label="Москва Север · активен"
    )
    binding_form.get_by_role("button", name="Проверить привязку").click()
    binding_form.get_by_role("button", name="Подтвердить привязку").wait_for(
        state="visible"
    )
    assert binding_form.get_by_label("Официальный склад продавца WB").is_disabled()
    assert binding_form.get_by_label("Внутренний склад FF").is_disabled()
    binding_form.get_by_role("button", name="Подтвердить привязку").click()
    wb_registry.locator(".ff-pool-list-item", has_text="WB Север").get_by_role(
        "button", name="Привязан"
    ).wait_for(state="visible")
    waiting_orenburg = wb_registry.locator(".ff-pool-list-item", has_text="Оренбург")
    waiting_orenburg.get_by_role("button", name="Выбрать склад WB").click()
    reverse_form = page.locator("[data-ff-pool-facility-detail] form")
    assert reverse_form.get_by_label("Внутренний склад FF").is_disabled()
    assert reverse_form.get_by_label("Официальный склад продавца WB").input_value() == "71002"
    reverse_form.get_by_role("button", name="Проверить привязку").click()
    reverse_form.get_by_role("button", name="Подтвердить привязку").wait_for(
        state="visible"
    )
    reverse_form.get_by_role("button", name="Подтвердить привязку").click()
    wb_registry.locator(".ff-pool-list-item", has_text="WB Восток").get_by_role(
        "button", name="Привязан"
    ).wait_for(state="visible")

    page.locator("[data-ff-pool-facility-new]").click()
    onboarding_form = page.locator("[data-ff-pool-facility-detail] form")
    onboarding_form.get_by_label("Название").fill("Синтетический новый FF")
    onboarding_form.get_by_label("Город").fill("Тестовый город")
    assert onboarding_form.get_by_label("Статус").is_disabled()
    assert onboarding_form.get_by_label("Статус").input_value() == "false"
    onboarding_form.get_by_role("button", name="Проверить создание").click()
    onboarding_form.get_by_role("button", name="Подтвердить создание").wait_for(
        state="visible"
    )
    assert onboarding_form.get_by_label("Название").is_disabled()
    assert onboarding_form.get_by_label("Город").is_disabled()
    assert onboarding_form.get_by_label("Часовой пояс").is_disabled()
    assert "Остаток, капитал, документы и WB не изменятся" in onboarding_form.inner_text()
    onboarding_form.get_by_role("button", name="Подтвердить создание").click()
    page.locator(
        "[data-ff-pool-facilities] .ff-pool-list-item",
        has_text="Синтетический новый FF",
    ).wait_for(state="visible")
    assert "Ожидает привязки к WB" in wb_registry.locator(
        ".ff-pool-list-item", has_text="Синтетический новый FF"
    ).inner_text()
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
    with page.expect_response(
        lambda response: response.request.method == "POST"
        and response.url.endswith(f"{DEFAULT_FF_POOL_DOCUMENTS_PATH}/overhead/preview")
    ) as response_info:
        page.locator("[data-ff-pool-preview]").click()
    assert response_info.value.status == 200
    page.wait_for_function(
        "document.querySelector('[data-ff-pool-workflow-detail]')?.textContent.includes('ручной ввод')"
    )
    assert len(overhead_mutation_requests) == 1
    manual_request = overhead_mutation_requests[0]
    assert str(dict(manual_request["headers"]).get("content-type", "")).startswith("application/json")
    manual_body = json.loads(str(manual_request["post_data"]))
    assert manual_body["facility_id"] and manual_body["scope"] == "FBS"
    assert manual_body["category"] == "other" and manual_body["amount_rub"] == "1.23"
    manual_workflow_text = page.locator(
        "[data-ff-pool-workflow-detail]"
    ).inner_text()
    assert "ручной ввод" in manual_workflow_text
    assert "Parser:" not in manual_workflow_text
    assert page.locator("[data-ff-pool-overhead-evidence]").is_hidden()

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
    assert page.locator("[data-facility-id]").count() == 4
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

    page.route(f"**{DEFAULT_FF_POOL_DOCUMENTS_PATH}/china/form?*", intercept_guided_source)
    page.route(f"**{DEFAULT_FF_POOL_DOCUMENTS_PATH}/china/form/preview", intercept_guided_mutation)
    page.route(f"**{DEFAULT_FF_POOL_PATH}/requests/*", intercept_guided_status)
    page.route(f"**{DEFAULT_FF_POOL_PATH}/requests/*/confirm", intercept_guided_mutation)
    response = page.goto(f"{base}{DEFAULT_SHEET_SUPPLIER_UI_PATH}?embedded=operator", wait_until="domcontentloaded")
    assert response is not None and response.status == 200
    page.locator('#shipmentRows tr[data-row="sup_guided_csrf_browser"]').click()
    page.locator("#shipmentCard").wait_for(state="visible")
    page.get_by_role("tab", name="Состав поставки").click()
    status_node = page.locator("#guidedAcceptanceStatus")
    for mode in ("FBS", "FBO", "split"):
        page.locator("#guidedAcceptanceButton").click()
        page.wait_for_function("!document.querySelector('#guidedAcceptanceConfirm').disabled")
        page.locator("#guidedAcceptanceDate").fill("2026-08-01")
        page.locator("#guidedAcceptanceFacility").select_option("fac_msk")
        page.locator("#guidedAcceptanceMode").select_option(mode)
        assert page.locator("#guidedAcceptanceFile").count() == 0
        assert page.locator("#guidedAcceptanceComposition").is_visible() == (mode == "split")
        before_count = len(guided_mutation_requests)
        if mode == "FBS":
            page.locator("#guidedAcceptanceFields details").evaluate("node => { node.open = true; }")
            for invalid_expense in ("-1", "0.001"):
                page.locator("#guidedExpenseFbs").fill(invalid_expense)
                page.locator("#guidedAcceptanceConfirm").click()
                assert len(guided_mutation_requests) == before_count
                assert "расходов" in status_node.inner_text()
            page.locator("#guidedExpenseFbs").fill("0")
            page.locator("#guidedAcceptanceFields details").evaluate("node => { node.open = false; }")
        if mode == "split":
            for bad in ("-1", "1.5", "8"):
                page.locator('[data-nm-id="101"] [data-quantity="quantity_fbs"]').fill(bad)
                page.locator("#guidedAcceptanceConfirm").click()
                assert len(guided_mutation_requests) == before_count
                assert "количеств" in status_node.inner_text().lower() or "целым" in status_node.inner_text()
            page.locator('[data-nm-id="101"] [data-quantity="quantity_fbs"]').fill("8")
            page.locator('[data-nm-id="101"] [data-quantity="quantity_fbo"]').fill("2")
            page.locator("#guidedAcceptanceDiscrepancies").check()
            page.locator('[data-nm-id="202"] [data-quantity="accepted_quantity"]').fill("5")
            page.locator('[data-nm-id="202"] [data-quantity="quantity_fbs"]').fill("5")
            page.locator('[data-nm-id="202"] [data-comment]').fill("Недостача одной единицы")
            guided_transport["lose_confirm"] = True
        if mode == "FBO":
            guided_transport["hang_confirm"] = True
            page.evaluate("window.__nativeTimer = window.setTimeout; window.setTimeout = (fn, ms, ...args) => window.__nativeTimer(fn, ms === 30000 ? 200 : ms, ...args)")
        page.locator("#guidedAcceptanceConfirm").evaluate("node => { node.click(); node.click(); }")
        page.wait_for_function("document.querySelector('#guidedAcceptanceStatus').textContent.includes('Документ проведён')")
        assert len(guided_mutation_requests) == before_count + 2
        assert page.locator("#guidedAcceptanceClose").is_enabled()
        if mode == "FBO":
            guided_transport["hang_confirm"] = False
            for pending_route in guided_hanging_routes:
                pending_route.abort()
            guided_hanging_routes.clear()
            page.evaluate("window.setTimeout = window.__nativeTimer")
        assert guided_mutation_requests[-2]["body"]["mode"] == mode
        assert guided_mutation_requests[-2]["body"]["business_date"] == "2026-08-01"
        assert all(item["headers"].get("x-wb-ff-pool-csrf") == "1" for item in guided_mutation_requests)
        assert "остаток нормализации" not in status_node.inner_text()
        assert "выключено" not in status_node.inner_text()
        page.locator("#guidedAcceptanceClose").click()

    # Lost preview before server acceptance is resolved by GET, with explicit edit.
    guided_transport["lose_preview"] = True
    page.locator("#guidedAcceptanceButton").click()
    page.wait_for_function("!document.querySelector('#guidedAcceptanceConfirm').disabled")
    page.locator("#guidedAcceptanceDate").fill("2026-08-01")
    page.locator("#guidedAcceptanceFacility").select_option("fac_msk")
    page.locator("#guidedAcceptanceConfirm").click()
    page.locator("#guidedAcceptanceEdit").wait_for(state="visible")
    before_count = len(guided_mutation_requests)
    page.locator("#guidedAcceptanceClose").click()
    page.locator("#guidedAcceptanceButton").click()
    page.locator("#guidedAcceptanceEdit").wait_for(state="visible")
    assert len(guided_mutation_requests) == before_count
    page.locator("#guidedAcceptanceEdit").click()
    page.wait_for_function("!document.querySelector('#guidedAcceptanceConfirm').disabled")
    assert page.locator("#guidedAcceptanceDate").input_value() == "2026-08-01"
    assert page.locator("#guidedAcceptanceFacility").input_value() == "fac_msk"
    page.screenshot(path=str(screenshot_path.with_name(screenshot_path.stem + "-guided" + screenshot_path.suffix)))
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
