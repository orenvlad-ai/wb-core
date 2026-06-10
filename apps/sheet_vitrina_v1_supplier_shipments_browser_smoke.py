"""Browser smoke-check for supplier shipments UI in operator and supplier-only page."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
from io import BytesIO
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading

from openpyxl import Workbook
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SETTINGS_UI_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_SUPPLIER_UI_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_SUPPLIER_SHIPMENTS_PARSE_PATH,
    DEFAULT_SUPPLIER_SHIPMENTS_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


def main() -> None:
    with TemporaryDirectory(prefix="supplier-shipments-browser-") as tmp:
        tmp_path = Path(tmp)
        invoice_path = tmp_path / "PI-test 26GN390 (14.5.2026).xlsx"
        invoice_path.write_bytes(_build_invoice_fixture())
        runtime_dir = tmp_path / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
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
            activated_at_factory=lambda: "2026-05-30T08:00:00Z",
        )
        original_list_supplier_shipments = entrypoint.handle_supplier_shipments_list_request
        first_list_seen = threading.Event()
        first_list_release = threading.Event()

        def _delayed_first_list_request():
            if not first_list_seen.is_set():
                first_list_seen.set()
                first_list_release.wait(timeout=5)
            return original_list_supplier_shipments()

        entrypoint.handle_supplier_shipments_list_request = _delayed_first_list_request
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{config.port}"
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                settings_page = browser.new_page(viewport={"width": 1280, "height": 900})
                settings_page.goto(f"{base_url}{DEFAULT_SETTINGS_UI_PATH}", wait_until="domcontentloaded")
                expect(settings_page.get_by_text("Справочник номенклатуры")).to_be_visible()
                expect(settings_page.locator("thead")).not_to_contain_text("Наш SKU")
                expect(settings_page.locator("thead")).not_to_contain_text("Aliases")
                expect(settings_page.locator("thead")).not_to_contain_text("Комментарий")
                expect(settings_page.locator("thead")).to_contain_text("Цена закупки, ¥")
                expect(settings_page.get_by_role("button", name="Скачать Excel")).to_be_visible()
                expect(settings_page.get_by_role("button", name="Загрузить Excel")).to_be_visible()
                expect(settings_page.locator("#nomenclatureMessage")).to_have_text("", timeout=5000)
                expect(settings_page.locator("#nomenclatureRows")).to_contain_text("Справочник пуст.", timeout=5000)
                settings_page.locator("#addItemButton").click()
                expect(settings_page.locator("#nomenclatureRows tr[data-item-id=''] [data-field='nm_id']")).to_be_visible(timeout=5000)
                draft_row = settings_page.locator("#nomenclatureRows tr[data-item-id='']").first
                expect(draft_row.locator("[data-field='our_sku']")).to_have_count(0)
                expect(draft_row.locator("[data-field='aliases']")).to_have_count(0)
                expect(draft_row.locator("[data-field='comment']")).to_have_count(0)
                draft_row.locator("[data-field='nm_id']").fill("210183919")
                draft_row.locator("[data-field='nomenclature_name']").fill("Clear iPhone 14 Pro")
                expect(draft_row.locator("[data-field='product_type']")).to_contain_text("Прозрачное")
                type_width = draft_row.locator("[data-field='product_type']").evaluate("(node) => node.getBoundingClientRect().width")
                if type_width < 120:
                    raise AssertionError(f"product type select must not be squeezed, got width={type_width}")
                draft_row.locator("[data-field='match_key']").fill("clear|iphone_14_pro")
                draft_row.locator("[data-field='purchase_price_yuan']").fill("1,0")
                draft_row.locator("[data-field='compatible_models_text']").fill("iPhone 14 Pro")
                expect(draft_row.get_by_role("button", name="Сохранить")).to_be_visible()
                expect(draft_row.get_by_role("button", name="Выключить")).to_be_visible()
                draft_row.get_by_role("button", name="Сохранить").click()
                expect(settings_page.locator("#nomenclatureMessage")).to_contain_text("Справочник сохранён.", timeout=5000)
                saved_first_row = settings_page.locator("#nomenclatureRows tr").first
                expect(saved_first_row.locator("details.compat-keys")).to_have_count(1)
                expect(saved_first_row.locator("details.compat-keys")).not_to_have_attribute("open", "")
                expect(saved_first_row.locator("summary")).to_contain_text("Ключи: 1")
                settings_page.get_by_role("button", name="Добавить строку").click()
                compat_row = settings_page.locator("#nomenclatureRows tr").first
                compat_row.locator("[data-field='nm_id']").fill("391662410")
                compat_row.locator("[data-field='nomenclature_name']").fill("anti-spy iPhone 14 / 13 / 13Pro")
                compat_row.locator("[data-field='product_type']").select_option("anti_spy")
                compat_row.locator("[data-field='match_key']").fill("anti_spy|iphone_14_13_13pro")
                compat_row.locator("[data-field='compatible_models_text']").fill("iPhone 14, iPhone 13, iPhone 13 Pro")
                compat_row.get_by_role("button", name="Сохранить").click()
                expect(settings_page.locator("#nomenclatureMessage")).to_contain_text("Справочник сохранён.", timeout=5000)
                page.goto(f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}", wait_until="domcontentloaded")
                expect(page.get_by_role("link", name="Настройки")).to_be_visible()
                page.locator("[data-unified-tab-button='factory-order']").click()
                expect(page.locator("iframe[title='Поставки']")).to_be_visible(timeout=10000)
                operator_frame = page.frame_locator("iframe[title='Поставки']")
                expect(operator_frame.locator("body")).to_contain_text("Расчёты", timeout=10000)
                expect(operator_frame.get_by_role("button", name="Расчёты")).to_be_visible()
                expect(operator_frame.get_by_role("button", name="От поставщика")).to_be_visible()
                operator_frame.get_by_role("button", name="От поставщика").click()
                expect(operator_frame.locator("iframe[title='От поставщика']")).to_be_visible(timeout=10000)
                expect(operator_frame.locator("#supplier-shipments-title")).to_have_count(0)
                expect(operator_frame.locator(".supplier-embed-block")).to_have_count(0)
                expect(operator_frame.get_by_text("Реестр заказов", exact=True)).to_have_count(0)
                frame = operator_frame.frame_locator("iframe[title='От поставщика']")
                expect(frame.locator("h1", has_text="订单登记表 / Order registry / Реестр заказов")).to_be_visible(timeout=10000)
                expect(frame.locator("h1", has_text="订单登记表 / Order registry / Реестр заказов")).to_have_count(1)
                expect(frame.locator("h2", has_text="Реестр заказов")).to_have_count(0)
                expect(frame.get_by_text("Invoice-заказы поставщиков, сохранённые в WebCore")).to_have_count(0)
                if not first_list_seen.wait(timeout=3):
                    raise AssertionError("supplier registry list request must start on embedded load")
                expect(frame.locator("#shipmentRows")).to_have_attribute("data-registry-state", "loading")
                expect(frame.locator("#shipmentRows")).to_contain_text("Загрузка")
                expect(frame.locator("#shipmentRows")).not_to_contain_text("Заказов пока нет")
                first_list_release.set()
                expect(frame.locator("#shipmentRows")).to_have_attribute("data-registry-state", "loaded_empty", timeout=5000)
                expect(frame.locator("#shipmentRows")).to_contain_text("暂无订单 / No orders yet / Заказов пока нет.")
                expect(frame.get_by_text("匹配 / Matching / Матчинг").first).to_be_visible()
                expect(frame.get_by_text("供应商 / Supplier / Поставщик")).to_be_visible()
                expect(frame.get_by_text("Реестр поставок")).to_have_count(0)
                actions = frame.locator(".topbar .toolbar > *").evaluate_all("(nodes) => nodes.map((node) => node.textContent.trim())")
                expected_actions = [
                    "新增订单 / Add order / Добавить заказ",
                    "退出 / Logout / Выйти",
                    "Открыть отдельно",
                ]
                if actions != expected_actions:
                    raise AssertionError(f"supplier topbar actions must stay ordered, got {actions}")
                expect(frame.get_by_role("link", name="Открыть отдельно")).to_be_visible()
                standalone_href = frame.get_by_role("link", name="Открыть отдельно").get_attribute("href") or ""
                if standalone_href != DEFAULT_SHEET_SUPPLIER_UI_PATH:
                    raise AssertionError(f"standalone supplier link must keep existing route, got {standalone_href!r}")
                header_style = frame.locator(".registry-wrap thead th").first.evaluate(
                    """(node) => {
                        const styles = window.getComputedStyle(node);
                        return {
                            backgroundColor: styles.backgroundColor,
                            backgroundImage: styles.backgroundImage,
                            borderBottomColor: styles.borderBottomColor,
                            borderBottomWidth: styles.borderBottomWidth,
                            color: styles.color,
                            fontWeight: styles.fontWeight
                        };
                    }"""
                )
                body_style = frame.locator("#shipmentRows td").first.evaluate(
                    """(node) => {
                        const styles = window.getComputedStyle(node);
                        return { backgroundColor: styles.backgroundColor };
                    }"""
                )
                if header_style["backgroundColor"] == body_style["backgroundColor"] and header_style["backgroundImage"] == "none":
                    raise AssertionError(f"table header must differ from body background, got {header_style}")
                if float(header_style["borderBottomWidth"].replace("px", "") or 0) < 1:
                    raise AssertionError(f"table header must keep a visible lower border, got {header_style}")
                if header_style["borderBottomColor"] in {"rgba(0, 0, 0, 0)", "transparent"}:
                    raise AssertionError(f"table header border must not be transparent, got {header_style}")
                expect(frame.get_by_role("button", name="新增订单 / Add order / Добавить заказ")).to_be_visible()
                frame.get_by_role("button", name="新增订单 / Add order / Добавить заказ").click()
                expect(frame.get_by_label("出货日期 / Shipment date / Дата отгрузки")).to_be_visible()
                expect(frame.get_by_text("Supplier", exact=True)).to_have_count(0)
                expect(frame.get_by_text("Customer", exact=True)).to_have_count(0)
                expect(frame.get_by_text("Наш SKU")).to_have_count(0)
                expect(frame.get_by_text("Our SKU")).to_have_count(0)
                expect(frame.get_by_text("我方SKU")).to_have_count(0)
                expect(frame.get_by_text("Наш SKU / nmId")).to_have_count(0)
                expect(frame.get_by_role("button", name="保存 / Save / Сохранить")).to_be_disabled()
                parse_url = f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PARSE_PATH}"
                page.route(
                    parse_url,
                    lambda route: route.fulfill(
                        status=413,
                        content_type="text/html",
                        body="<html><head><title>413 Request Entity Too Large</title></head><body><h1>413</h1></body></html>",
                    ),
                )
                frame.locator("#invoiceFileInput").set_input_files(str(invoice_path))
                expect(frame.locator("#cardMessage")).to_contain_text("HTTP 413")
                expect(frame.locator("#cardMessage")).not_to_contain_text("Unexpected token")
                page.unroute(parse_url)
                frame.locator("#invoiceFileInput").set_input_files(str(invoice_path))
                expect(frame.locator("#productLines input[data-line-field='model_raw']").first).to_be_visible()
                expect(frame.get_by_text("平台ID / nmId / nmId")).to_be_visible()
                expect(frame.get_by_text("我方品名 / Our item name / Номенклатура")).to_be_visible()
                expect(frame.get_by_text("价格匹配 / Price check / Соответствие цены")).to_be_visible()
                expect(frame.locator("#productLines input[data-line-field='internal_sku']")).to_have_count(0)
                expect(frame.locator("#productLines input[data-line-field='internal_nm_id']").first).to_have_value("210183919")
                expect(frame.locator("#productLines .price-conformity")).to_have_count(3)
                expect(frame.locator("#productLines .price-conformity").nth(0)).to_have_text("✓")
                expect(frame.locator("#productLines .price-conformity").nth(1)).to_have_text("✕")
                first_price_class = frame.locator("#productLines .price-conformity").nth(0).get_attribute("class") or ""
                second_price_class = frame.locator("#productLines .price-conformity").nth(1).get_attribute("class") or ""
                if "success" not in first_price_class or "error" not in second_price_class:
                    raise AssertionError(f"price conformity symbols must map to success/error classes, got {first_price_class!r}, {second_price_class!r}")
                expect(frame.locator("#contractNoInput")).to_have_value("CNT-2026-0513")
                expect(frame.locator("#contractDateInput")).to_have_value("2026-05-13")
                expect(frame.locator("#productLines").get_by_text("已匹配 / Matched / Сопоставлено")).to_be_visible()
                expect(frame.locator("#productLines").get_by_text("按兼容型号匹配 / Matched by compatibility / Сопоставлено по совместимости")).to_be_visible()
                expect(frame.locator("select[data-line-field='match_status']")).to_have_count(0)
                expect(frame.get_by_role("button", name="重新匹配 / Re-match / Пересопоставить")).to_have_count(0)
                frame.get_by_label("出货日期 / Shipment date / Дата отгрузки").fill("2026-05-14")
                expect(frame.get_by_role("button", name="保存 / Save / Сохранить")).to_be_enabled()
                frame.get_by_role("button", name="保存 / Save / Сохранить").click()
                expect(frame.get_by_text("订单已保存 / Order saved / Заказ сохранён.")).to_be_visible(timeout=5000)
                expect(frame.get_by_role("button", name="Проверить цены")).to_be_enabled()
                frame.get_by_role("button", name="Проверить цены").click()
                expect(frame.locator("#cardMessage")).to_contain_text("Проверка цен обновлена.", timeout=5000)
                expect(frame.locator("#shipmentRows").get_by_text("26GN390")).to_be_visible()
                expect(frame.locator("#shipmentRows").get_by_text("HanShang Technology")).to_be_visible()
                expect(frame.locator("#shipmentRows").get_by_text("Check")).to_be_visible()
                frame.get_by_role("button", name="关闭 / Close / Закрыть").click()
                expect(frame.locator("#shipmentCard")).to_be_hidden()
                header_texts = frame.locator(".registry-wrap thead th:visible").evaluate_all(
                            "(nodes) => nodes.map((node) => node.textContent.trim())"
                        )
                if any("Currency" in text or "Валюта" in text for text in header_texts):
                        raise AssertionError(f"operator registry must hide Currency column, got {header_texts}")
                try:
                        invoice_index = next(index for index, text in enumerate(header_texts) if "Invoice" in text and "Файл" in text)
                        status_index = header_texts.index("Статус заказа")
                        actions_index = next(index for index, text in enumerate(header_texts) if "Actions" in text or "Действия" in text)
                except StopIteration as exc:
                        raise AssertionError(f"operator registry must expose invoice/status/actions headers, got {header_texts}") from exc
                if not invoice_index < status_index < actions_index:
                        raise AssertionError(f"order status header must be after invoice and before actions, got {header_texts}")
                status_select = frame.locator("[data-order-status-shipment]").first
                expect(status_select).to_have_value("production")
                expect(status_select.locator("option:checked")).to_have_text("На производстве")
                status_select.select_option("in_transit")
                expect(frame.locator("#registryMessage")).to_contain_text("Статус заказа сохранён.", timeout=5000)
                expect(frame.locator("#shipmentCard")).to_be_hidden()
                frame.locator("body").evaluate("() => window.location.reload()")
                expect(frame.locator("#shipmentRows").get_by_text("26GN390")).to_be_visible(timeout=5000)
                expect(frame.locator("[data-order-status-shipment]").first).to_have_value("in_transit")
                expect(frame.locator("a[data-download]").first).to_have_text("Download")
                expect(frame.locator("[data-delete-shipment]").first).to_have_text("Delete")
                frame.locator("#shipmentRows tr[data-row]").first.click()
                expect(frame.get_by_role("link", name="下载发票 / Download invoice / Скачать invoice")).to_be_visible()
                expect(frame.get_by_role("button", name="重新匹配 / Re-match / Пересопоставить")).to_have_count(0)
                frame.get_by_role("button", name="关闭 / Close / Закрыть").click()
                expect(frame.locator("#shipmentCard")).to_be_hidden()

                supplier_page = browser.new_page(viewport={"width": 1280, "height": 900})
                supplier_page.goto(f"{base_url}{DEFAULT_SHEET_SUPPLIER_UI_PATH}", wait_until="domcontentloaded")
                expect(supplier_page.locator("h1", has_text="订单登记表 / Order registry / Реестр заказов")).to_be_visible()
                expect(supplier_page.get_by_role("link", name="Открыть отдельно")).to_have_count(0)
                expect(supplier_page.get_by_text("26GN390")).to_be_visible()
                expect(supplier_page.locator("[data-order-status-shipment]")).to_have_count(0)
                frame.locator("[data-delete-shipment]").first.click()
                expect(frame.locator("[data-delete-confirmation]")).to_be_visible()
                expect(frame.locator("#shipmentRows")).to_contain_text("26GN390")
                frame.locator("#shipmentRows tr[data-row]").first.click()
                expect(frame.locator("#shipmentCard")).to_be_hidden()
                frame.locator("[data-delete-cancel]").click()
                expect(frame.locator("[data-delete-confirmation]")).to_have_count(0)
                expect(frame.locator("#shipmentRows")).to_contain_text("26GN390")
                frame.locator("[data-delete-shipment]").first.click()
                expect(frame.locator("[data-delete-confirmation]")).to_be_visible()
                frame.locator("[data-delete-confirm]").click()
                expect(frame.locator("#registryMessage")).to_contain_text("订单已删除 / Order deleted / Заказ удалён.", timeout=5000)
                expect(frame.locator("#shipmentRows")).not_to_contain_text("26GN390")
                _assert_supplier_role_browser_ui(browser, tmp_path, invoice_path)
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print("sheet_vitrina_v1_supplier_shipments_browser_smoke: OK")


def _assert_supplier_role_browser_ui(browser, tmp_path: Path, invoice_path: Path) -> None:
    owner_password = "owner-password-not-secret"
    supplier_password = "supplier-password-not-secret"
    runtime_dir = tmp_path / "supplier-role-runtime"
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    _seed_supplier_role_nomenclature(runtime)
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
    with _patched_env(
        {
            "WB_CORE_WEB_AUTH_REQUIRED": "1",
            "WB_CORE_WEB_AUTH_USERNAME": "owner",
            "WB_CORE_WEB_AUTH_PASSWORD_HASH": _password_hash(owner_password),
            "WB_CORE_WEB_AUTH_SESSION_SECRET": "supplier-browser-smoke-session-secret",
            "WB_CORE_SUPPLIER_AUTH_USERNAME": "supplier",
            "WB_CORE_SUPPLIER_AUTH_PASSWORD_HASH": _password_hash(supplier_password),
            "WB_CORE_SUPPLIER_AUTH_DISPLAY_NAME": "Supplier",
        }
    ):
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: "2026-05-30T08:00:00Z",
        )
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        context = None
        try:
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            base_url = f"http://127.0.0.1:{config.port}"
            page = context.new_page()
            page.goto(f"{base_url}/login?next={DEFAULT_SHEET_SUPPLIER_UI_PATH}", wait_until="domcontentloaded")
            page.locator("#username").fill("supplier")
            page.locator("#password").fill(supplier_password)
            page.get_by_role("button", name="Войти").click()
            expect(page.locator("h1", has_text="订单登记表 / Order registry / Реестр заказов")).to_be_visible()
            expect(page.locator("#priceCheckButton")).to_have_count(0)
            expect(page.get_by_role("button", name="Проверить цены")).to_have_count(0)

            page.get_by_role("button", name="新增订单 / Add order / Добавить заказ").click()
            expect(page.get_by_label("出货日期 / Shipment date / Дата отгрузки")).to_be_visible()
            expect(page.get_by_text("价格匹配 / Price check / Соответствие цены")).to_be_visible()
            page.locator("#invoiceFileInput").set_input_files(str(invoice_path))
            expect(page.locator("#productLines input[data-line-field='model_raw']").first).to_be_visible()
            expect(page.locator("#productLines .price-conformity")).to_have_count(3)
            expect(page.get_by_role("button", name="Проверить цены")).to_have_count(0)
            page.get_by_label("出货日期 / Shipment date / Дата отгрузки").fill("2026-05-14")
            page.get_by_role("button", name="保存 / Save / Сохранить").click()
            expect(page.get_by_text("订单已保存 / Order saved / Заказ сохранён.")).to_be_visible(timeout=5000)
            expect(page.get_by_role("button", name="Проверить цены")).to_have_count(0)
            shipment_id = page.locator("#shipmentRows tr[data-row]").first.get_attribute("data-row") or ""
            if not shipment_id:
                raise AssertionError("supplier browser smoke must create a shipment row before price-check probe")
            price_check_probe = page.evaluate(
                """async ({shipmentsPath, shipmentId}) => {
                    const response = await fetch(shipmentsPath + "/" + encodeURIComponent(shipmentId) + "/price-check", {
                        method: "POST",
                        headers: {"Content-Type": "application/json", "Accept": "application/json"},
                        body: JSON.stringify({context: {source: "supplier_browser_smoke"}})
                    });
                    let payload = {};
                    try { payload = await response.json(); } catch (error) { payload = {error: String(error)}; }
                    return {status: response.status, payload};
                }""",
                {"shipmentsPath": DEFAULT_SUPPLIER_SHIPMENTS_PATH, "shipmentId": shipment_id},
            )
            if price_check_probe.get("status") != 403 or price_check_probe.get("payload", {}).get("error") != "forbidden":
                raise AssertionError(f"supplier must not call manual price-check route, got {price_check_probe}")
        finally:
            if context is not None:
                context.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def _seed_supplier_role_nomenclature(runtime: RegistryUploadDbBackedRuntime) -> None:
    runtime.save_nomenclature_item(
        {
            "item_id": "supplier_browser_clear_14_pro",
            "is_active": True,
            "our_sku": "",
            "nm_id": 210183919,
            "nomenclature_name": "Clear iPhone 14 Pro",
            "product_type": "clear",
            "match_key": "clear|iphone_14_pro",
            "purchase_price_yuan": 1.0,
            "aliases": [],
            "compatible_models_text": "",
            "compatible_model_keys": [],
            "comment": "",
            "created_at": "2026-05-30T08:00:00Z",
            "updated_at": "2026-05-30T08:00:00Z",
        }
    )


def _password_hash(password: str) -> str:
    salt = b"supplier-browser-smoke-salt"
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260_000)
    return "pbkdf2_sha256$260000$" + _b64(salt) + "$" + _b64(digest)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


@contextmanager
def _patched_env(values: dict[str, str]):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _build_invoice_fixture() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Invoice"
    sheet.append(["Invoice No:", "26GN390"])
    sheet.append(["Invoice Date:", "14.5.2026"])
    sheet.append(["Contract No.", "CNT-2026-0513"])
    sheet.append(["Date of Contract", "2026.5.13"])
    sheet.append(["Supplier:", "Zhejiang Supplier", "", "Currency:", "RMB"])
    sheet.append(["Invoice Total:", 33])
    sheet.append(["NO.", "NAME & SPECIFICATION", "MODELS", "QTY", "U.PRICE", "AMOUNT", "COMMENT"])
    sheet.append([1, "高清膜 smk", "iPhone 14 Pro", 10, 1, 10, ""])
    sheet.append([2, "防窥膜 (Anti-Spy)", "iPhone 17e / 16e /14 / 13 / 13Pro", 4, 2, 8, ""])
    sheet.append([3, "防窥膜 (Anti-Spy)", "iPhone 14 Pro Max", 5, 2, 10, ""])
    sheet.append([4, "OPP bag packets", "", 100, 0.05, 5, "OPP packets"])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
