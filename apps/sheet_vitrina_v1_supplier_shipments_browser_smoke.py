"""Browser smoke-check for supplier shipments UI in operator and supplier-only page."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
from io import BytesIO
import os
from pathlib import Path
import re
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
from packages.application.supplier_financial_documents import (  # noqa: E402
    StaticUsdRateProvider,
    SupplierFinancialDocumentsBlock,
)
from packages.application.supplier_shipments import SupplierShipmentsBlock  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


QUOTE_COMPARISON_TEXT = """
Коммерческое предложение на транспортно-экспедиционные услуги по тарифу «Авто стандарт 25-30 дней»
Transitplus International Ltd
Наименование груза: СТЕКЛА ДЛЯ СМАРТФОНА
г. Москва 19.06.2026
Город отправки: Guangzhou (Гуанчжоу)
Пункт назначения: Москва
Сроки доставки: 25-30 дней
Вес брутто, кг. 6713,45
Вес нетто, кг: 6713,45
Объем, м3 31,28
Оценочная стоимость груза, долл. 77423,22 USD или 541962,50 юаней
1. Предварительный расчет стоимости:
№ Перечень услуг Общая стоимость
1 Стоимость доставки 12420
2 Таможенные платежи и сборы 27175
3 Экологический сбор 0
4 Брокерские услуги 350
5 Комиссия компании 0
6 Страховая ставка, % 775
ИТОГО: 40720 USD
Оформление разрешительной документации 0 USD
Оплата за доставку производится: по курсу Банка ВТБ (на дату выставления счета)
Предложение действительно в течение 5 календарных дней
"""


def main() -> None:
    with TemporaryDirectory(prefix="supplier-shipments-browser-") as tmp:
        tmp_path = Path(tmp)
        invoice_path = tmp_path / "PI-test 26GN390 (14.5.2026).xlsx"
        invoice_path.write_bytes(_build_invoice_fixture())
        quote_comparison_path = tmp_path / "quote-comparison.pdf"
        quote_comparison_path.write_bytes(b"%PDF-1.4\n% synthetic quote comparison browser smoke\n")
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
        entrypoint.supplier_financial_documents_block = SupplierFinancialDocumentsBlock(
            runtime=runtime,
            timestamp_factory=lambda: "2026-05-30T08:00:00Z",
            usd_rate_provider=StaticUsdRateProvider({"2026-06-19": "78.00"}),
            pdf_text_extractor=_fixture_financial_text_extractor,
        )
        documents_block = SupplierShipmentsBlock(
            runtime=runtime,
            timestamp_factory=lambda: "2026-05-30T08:00:00Z",
        )
        contract_upload = documents_block.create_trade_document_from_upload(
            document_type="contract",
            file_bytes=_build_contract_xlsx_fixture("BROWSER-CONTRACT-0601", "2026-06-01"),
            uploaded_filename="browser-contract.xlsx",
        )
        contract_id = str((contract_upload.get("document") or {}).get("document_id") or "")
        invoice_upload = documents_block.create_trade_document_from_upload(
            document_type="invoice",
            file_bytes=_build_invoice_fixture(),
            uploaded_filename="browser-invoice.xlsx",
        )
        invoice_id = str((invoice_upload.get("document") or {}).get("document_id") or "")
        if not contract_id or not invoice_id:
            raise AssertionError(f"settings document fixture creation failed: {contract_upload} {invoice_upload}")
        documents_block.link_invoice_to_contract(
            invoice_id,
            contract_document_id=contract_id,
            linked_by="browser-smoke",
        )
        documents_block.create_trade_document_from_upload(
            document_type="invoice",
            file_bytes=b"%PDF-1.4\n% wb-core browser smoke invoice without amount\n",
            uploaded_filename="browser-invoice-no-amount.pdf",
            uploaded_content_type="application/pdf",
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
                settings_page.goto(f"{base_url}{DEFAULT_SETTINGS_UI_PATH}?embedded=1", wait_until="domcontentloaded")
                settings_page.evaluate("window.localStorage.removeItem('wb-core:sheet-vitrina-v1:settings-active-tab:v2')")
                settings_page.reload(wait_until="domcontentloaded")
                expect(settings_page.get_by_role("button", name="Номенклатура")).to_be_visible()
                expect(settings_page.get_by_role("button", name="Договоры")).to_be_visible()
                expect(settings_page.get_by_role("button", name="Инвойсы")).to_be_visible()
                expect(settings_page.get_by_role("button", name="Договоры и инвойсы")).to_have_count(0)
                expect(settings_page.get_by_text("Справочник номенклатуры")).to_be_visible()
                expect(settings_page.locator("section[aria-labelledby='contractsTitle']")).to_be_hidden()
                expect(settings_page.locator("section[aria-labelledby='invoicesTitle']")).to_be_hidden()
                expect(settings_page.get_by_role("button", name="Добавить контракт")).to_be_hidden()
                settings_page.get_by_role("button", name="Договоры").click()
                expect(settings_page.get_by_text("Справочник номенклатуры")).to_be_hidden()
                expect(settings_page.get_by_role("heading", name="Договоры")).to_be_visible()
                expect(settings_page.get_by_role("button", name="Добавить контракт")).to_be_visible()
                expect(settings_page.get_by_role("button", name="Добавить invoice")).to_be_hidden()
                expect(settings_page.locator("#contractsMessage")).to_have_text("", timeout=5000)
                contracts_section = settings_page.locator("section[aria-labelledby='contractsTitle']")
                expect(contracts_section.locator("thead")).not_to_contain_text("Тип")
                expect(contracts_section.locator("thead")).not_to_contain_text("Сумма invoice")
                expect(contracts_section.locator("#contractRows")).to_contain_text("BROWSER-CONTRACT-0601", timeout=5000)
                expect(contracts_section.locator("#contractRows")).to_contain_text("HanShang Technology", timeout=5000)
                expect(contracts_section.locator("#contractRows")).not_to_contain_text("26GN390")
                contract_row = contracts_section.locator("#contractRows tr", has_text="BROWSER-CONTRACT-0601").first
                expect(contract_row.get_by_role("button", name="Редактировать")).to_be_visible()
                contract_row.get_by_role("button", name="Редактировать").click()
                edit_row = contracts_section.locator("#contractRows tr[data-document-id]").first
                expect(edit_row.get_by_role("button", name="Сохранить")).to_be_visible()
                expect(edit_row.get_by_role("button", name="Отмена")).to_be_visible()
                edit_row.locator("[data-contract-edit-field='number']").fill("SHOULD-CANCEL")
                edit_row.get_by_role("button", name="Отмена").click()
                expect(contracts_section.locator("#contractRows")).to_contain_text("BROWSER-CONTRACT-0601")
                expect(contracts_section.locator("#contractRows")).not_to_contain_text("SHOULD-CANCEL")
                contracts_section.locator("#contractRows tr", has_text="BROWSER-CONTRACT-0601").first.get_by_role("button", name="Редактировать").click()
                edit_row = contracts_section.locator("#contractRows tr[data-document-id]").first
                edit_row.locator("[data-contract-edit-field='number']").fill("BROWSER-CONTRACT-EDITED")
                edit_row.locator("[data-contract-edit-field='document_date']").fill("2026-06-02")
                edit_row.locator("[data-contract-edit-field='supplier_name']").fill("Browser Edited Supplier")
                edit_row.get_by_role("button", name="Сохранить").click()
                expect(settings_page.locator("#contractsMessage")).to_contain_text("Договор сохранён.", timeout=5000)
                expect(contracts_section.locator("#contractRows")).to_contain_text("BROWSER-CONTRACT-EDITED", timeout=5000)
                expect(contracts_section.locator("#contractRows")).to_contain_text("2026-06-02", timeout=5000)
                expect(contracts_section.locator("#contractRows")).to_contain_text("Browser Edited Supplier", timeout=5000)
                settings_page.reload(wait_until="domcontentloaded")
                expect(settings_page.get_by_role("heading", name="Договоры")).to_be_visible()
                contracts_section = settings_page.locator("section[aria-labelledby='contractsTitle']")
                expect(contracts_section.locator("#contractRows")).to_contain_text("BROWSER-CONTRACT-EDITED", timeout=5000)
                expect(contracts_section.locator("#contractRows")).to_contain_text("Browser Edited Supplier", timeout=5000)
                settings_page.get_by_role("button", name="Инвойсы").click()
                expect(settings_page.get_by_role("button", name="Добавить invoice")).to_be_visible()
                expect(settings_page.get_by_role("button", name="Добавить контракт")).to_be_hidden()
                expect(settings_page.locator("#invoicesMessage")).to_have_text("", timeout=5000)
                invoices_section = settings_page.locator("section[aria-labelledby='invoicesTitle']")
                expect(invoices_section.locator("thead")).not_to_contain_text("Тип")
                expect(invoices_section.locator("thead")).to_contain_text("Контракт")
                expect(invoices_section.locator("thead")).to_contain_text("Сумма invoice")
                expect(invoices_section.locator("#invoiceRows")).to_contain_text("26GN390", timeout=5000)
                expect(invoices_section.locator("#invoiceRows")).to_contain_text("Zhejiang Supplier", timeout=5000)
                expect(invoices_section.locator("#invoiceRows")).to_contain_text("33 RMB", timeout=5000)
                invoice_row = invoices_section.locator("#invoiceRows tr", has_text="26GN390").first
                expect(invoice_row).to_contain_text("Контракт №BROWSER-CONTRACT-EDITED", timeout=5000)
                expect(invoice_row.get_by_role("button", name="Сменить")).to_be_visible()
                expect(invoice_row.get_by_role("button", name="Отвязать")).to_be_visible()
                invoice_row.get_by_role("button", name="Отвязать").click()
                expect(settings_page.locator("#invoicesMessage")).to_contain_text("Связь удалена.", timeout=5000)
                invoice_row = invoices_section.locator("#invoiceRows tr", has_text="26GN390").first
                expect(invoice_row).to_contain_text("Не привязан", timeout=5000)
                expect(invoice_row.get_by_role("button", name="Связать")).to_be_visible()
                invoice_row.locator("[data-contract-select]").select_option(contract_id)
                invoice_row.get_by_role("button", name="Связать").click()
                expect(settings_page.locator("#invoicesMessage")).to_contain_text("Связь сохранена.", timeout=5000)
                invoice_row = invoices_section.locator("#invoiceRows tr", has_text="26GN390").first
                expect(invoice_row).to_contain_text("Контракт №BROWSER-CONTRACT-EDITED", timeout=5000)
                no_amount_row = invoices_section.locator("#invoiceRows tr", has_text="browser-invoice-no-amount.pdf").first
                expect(no_amount_row.locator("td").nth(3)).to_have_text("-")
                expect(invoices_section.locator("#invoiceRows")).not_to_contain_text("browser-contract.xlsx")
                settings_page.get_by_role("button", name="Номенклатура").click()
                expect(settings_page.get_by_text("Справочник номенклатуры")).to_be_visible()
                nomenclature_section = settings_page.locator("section[aria-labelledby='nomenclatureTitle']")
                expect(nomenclature_section.locator("thead")).not_to_contain_text("Наш SKU")
                expect(nomenclature_section.locator("thead")).not_to_contain_text("Aliases")
                expect(nomenclature_section.locator("thead")).not_to_contain_text("Комментарий")
                expect(nomenclature_section.locator("thead")).to_contain_text("Цена закупки, ¥")
                expect(nomenclature_section.locator("thead")).to_contain_text("ШК")
                expect(settings_page.get_by_role("button", name="Скачать Excel")).to_be_visible()
                expect(settings_page.get_by_role("button", name="Загрузить Excel")).to_be_visible()
                expect(settings_page.get_by_role("button", name="Синхронизировать SKU с WB")).to_be_visible()
                expect(settings_page.get_by_role("button", name="Группы SKU")).to_be_visible()
                expect(settings_page.locator("#nomenclatureMessage")).to_have_text("", timeout=5000)
                expect(settings_page.locator("#nomenclatureRows")).to_contain_text("Справочник пуст.", timeout=5000)
                settings_page.locator("#addItemButton").click()
                expect(settings_page.locator("#nomenclatureRows tr[data-item-id=''] [data-field='nm_id']")).to_be_visible(timeout=5000)
                draft_row = settings_page.locator("#nomenclatureRows tr[data-item-id='']").first
                expect(draft_row.locator("[data-field='our_sku']")).to_have_count(0)
                expect(draft_row.locator("[data-field='aliases']")).to_have_count(0)
                expect(draft_row.locator("[data-field='comment']")).to_have_count(0)
                draft_row.locator("[data-field='nm_id']").fill("210183919")
                draft_row.locator("[data-field='barcode']").fill("1000000000001")
                draft_row.locator("[data-field='nomenclature_name']").fill("Clear iPhone 14 Pro")
                expect(draft_row.locator("[data-field='product_type']")).to_contain_text("Clean")
                type_width = draft_row.locator("[data-field='product_type']").evaluate("(node) => node.getBoundingClientRect().width")
                if type_width < 120:
                    raise AssertionError(f"product type select must not be squeezed, got width={type_width}")
                settings_page.locator("#nomenclatureColumnPicker summary").click()
                settings_page.locator("[data-column-option='match_key']").check()
                settings_page.locator("[data-column-option='compatible_models']").check()
                settings_page.locator("#nomenclatureColumnPicker summary").click()
                draft_row.locator("[data-field='match_key']").fill("clear|iphone_14_pro")
                draft_row.locator("[data-field='purchase_price_yuan']").fill("1,0")
                draft_row.locator("[data-field='compatible_models_text']").fill("iPhone 14 Pro")
                expect(draft_row.get_by_role("button", name="Сохранить")).to_be_visible()
                expect(draft_row.get_by_role("button", name="Скрыть SKU")).to_be_visible()
                draft_row.get_by_role("button", name="Сохранить").click()
                expect(settings_page.locator("#nomenclatureMessage")).to_contain_text("Справочник сохранён.", timeout=5000)
                saved_first_row = settings_page.locator("#nomenclatureRows tr").first
                expect(saved_first_row.locator("[data-field='barcode']")).to_have_value("1000000000001")
                expect(saved_first_row.locator(".barcode-status").first).to_contain_text("manual")
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
                page.evaluate("window.localStorage.removeItem('wb-core:sheet-vitrina-v1:supplier-order-status-filter:v1')")
                expect(page.locator("[data-unified-tab-button='factory-order']")).to_be_visible()
                page.locator("[data-unified-tab-button='factory-order']").click()
                expect(page.locator("iframe[title='Поставки']")).to_be_visible(timeout=10000)
                operator_frame = page.frame_locator("iframe[title='Поставки']")
                expect(operator_frame.locator("body")).to_contain_text("Расчёты", timeout=10000)
                expect(operator_frame.get_by_role("button", name="Расчёты")).to_be_visible()
                expect(operator_frame.get_by_role("button", name="Реестр поставок")).to_be_visible()
                expect(operator_frame.get_by_role("button", name="От поставщика")).to_be_visible()
                operator_frame.get_by_role("button", name="От поставщика").click()
                expect(operator_frame.locator("iframe[title='От поставщика']")).to_be_visible(timeout=10000)
                expect(operator_frame.locator("#supplier-shipments-title")).to_have_count(0)
                expect(operator_frame.locator(".supplier-embed-block")).to_have_count(0)
                expect(operator_frame.get_by_text("Реестр заказов", exact=True)).to_have_count(0)
                frame = operator_frame.frame_locator("iframe[title='От поставщика']")
                expect(frame.locator("h1")).to_have_text("Реестр заказов", timeout=10000)
                expect(frame.locator("h1")).not_to_contain_text("Order registry")
                expect(frame.locator("h1")).not_to_contain_text("订单登记表")
                expect(frame.locator("h2", has_text="Реестр заказов")).to_have_count(0)
                expect(frame.get_by_text("Invoice-заказы поставщиков, сохранённые в WebCore")).to_have_count(0)
                if not first_list_seen.wait(timeout=3):
                    raise AssertionError("supplier registry list request must start on embedded load")
                expect(frame.locator("#shipmentRows")).to_have_attribute("data-registry-state", "loading")
                expect(frame.locator("#shipmentRows")).to_contain_text("Загрузка")
                expect(frame.locator("#shipmentRows")).not_to_contain_text("Заказов пока нет")
                first_list_release.set()
                expect(frame.locator("#shipmentRows")).to_have_attribute("data-registry-state", "loaded_empty", timeout=5000)
                expect(frame.locator("#shipmentRows")).to_contain_text("Заказов пока нет.")
                expect(frame.locator("#shipmentRows")).not_to_contain_text("No orders yet")
                expect(frame.get_by_text("Матчинг").first).to_be_visible()
                expect(frame.get_by_text("Поставщик")).to_be_visible()
                expect(frame.get_by_text("匹配 / Matching / Матчинг")).to_have_count(0)
                expect(frame.get_by_text("供应商 / Supplier / Поставщик")).to_have_count(0)
                expect(frame.get_by_text("Реестр поставок")).to_have_count(0)
                actions = frame.locator(".topbar .toolbar > *").evaluate_all("(nodes) => nodes.map((node) => node.textContent.trim())")
                expected_actions = [
                    "Добавить заказ",
                    "Выйти",
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
                expect(frame.get_by_role("button", name="Добавить заказ")).to_be_visible()
                frame.get_by_role("button", name="Добавить заказ").click()
                expect(frame.get_by_label("Плановая дата отгрузки")).to_be_visible()
                expect(frame.get_by_label("Фактическая дата отгрузки")).to_be_visible()
                expect(frame.get_by_label("Фактическая дата приёмки на ФФ")).to_be_visible()
                expect(frame.get_by_label("Примерный курс юаня, ₽/¥")).to_be_visible()
                expect(frame.get_by_text("Supplier", exact=True)).to_have_count(0)
                expect(frame.get_by_text("Customer", exact=True)).to_have_count(0)
                expect(frame.get_by_text("Наш SKU")).to_have_count(0)
                expect(frame.get_by_text("Our SKU")).to_have_count(0)
                expect(frame.get_by_text("我方SKU")).to_have_count(0)
                expect(frame.get_by_text("Наш SKU / nmId")).to_have_count(0)
                expect(frame.get_by_role("button", name="Сохранить")).to_be_disabled()
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
                expect(frame.get_by_text("nmId", exact=True)).to_be_visible()
                expect(frame.get_by_text("Номенклатура", exact=True)).to_be_visible()
                expect(frame.get_by_text("Соответствие цены", exact=True)).to_be_visible()
                expect(frame.get_by_text("平台ID / nmId / nmId")).to_have_count(0)
                expect(frame.get_by_text("我方品名 / Our item name / Номенклатура")).to_have_count(0)
                expect(frame.get_by_text("价格匹配 / Price check / Соответствие цены")).to_have_count(0)
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
                expect(frame.locator("#productLines").get_by_text("Сопоставлено", exact=True)).to_be_visible()
                expect(frame.locator("#productLines").get_by_text("Сопоставлено по совместимости", exact=True)).to_be_visible()
                expect(frame.locator("select[data-line-field='match_status']")).to_have_count(0)
                expect(frame.get_by_role("button", name="重新匹配 / Re-match / Пересопоставить")).to_have_count(0)
                frame.get_by_label("Плановая дата отгрузки").fill("2026-05-14")
                frame.get_by_label("Фактическая дата отгрузки").fill("2026-05-16")
                frame.get_by_label("Примерный курс юаня, ₽/¥").fill("13.2")
                expect(frame.get_by_role("button", name="Сохранить")).to_be_enabled()
                frame.get_by_role("button", name="Сохранить").click()
                expect(frame.get_by_text("Заказ сохранён.")).to_be_visible(timeout=5000)
                expect(frame.locator("#supplyCompositionPanel #documentInvoiceDownloadLink")).to_have_count(0)
                expect(frame.locator("#supplyCompositionPanel #contractManageControls")).to_have_count(0)
                expect(frame.get_by_role("tab", name="Документы")).to_be_visible()
                frame.get_by_role("tab", name="Документы").click()
                exact_cost_tile = frame.locator("#financialSummaryGroups .total", has_text="Себестоимость на единицу товара")
                expect(exact_cost_tile).to_be_visible(timeout=5000)
                expect(exact_cost_tile.locator("strong")).to_have_text("—")
                expect(exact_cost_tile).to_have_attribute("title", "cny_payment_cost_unavailable")
                _seed_first_supplier_factual_expense(runtime, amount_rub=48.0)
                _seed_first_supplier_exact_cny_cost(runtime, payment_cost_rub=200000.0)
                frame.get_by_role("tab", name="Состав поставки").click()
                frame.get_by_role("tab", name="Документы").click()
                exact_cost_tile = frame.locator("#financialSummaryGroups .total", has_text="Себестоимость на единицу товара")
                expect(exact_cost_tile).to_be_visible(timeout=5000)
                expect(exact_cost_tile.locator("strong")).to_have_text(re.compile(r"10\s*528,84 ₽"), timeout=5000)
                exact_cost_value = exact_cost_tile.locator("strong").inner_text(timeout=5000).strip()
                if exact_cost_value in {"—", "-", "0", "0,00 ₽"}:
                    raise AssertionError(f"documents tab exact cost tile must show money value, got {exact_cost_value!r}")
                expect(exact_cost_tile).to_have_attribute("title", "по CNY ledger и подтверждённым документам")
                expect(frame.locator("#documentInvoiceDownloadLink")).to_be_visible()
                expect(frame.locator("#invoiceDocumentLabel .document-label-primary")).to_contain_text("26GN390")
                expect(frame.locator("#invoiceDocumentLabel")).to_contain_text("ID документа:")
                expect(frame.locator("#contractDocumentLabel")).to_contain_text("Контракт: выбрать")
                expect(frame.locator("#contractManageControls")).to_be_visible()
                expect(frame.locator("#uploadContractButton")).to_be_visible()
                expect(frame.get_by_role("link", name="Скачать все документы")).to_be_visible(timeout=5000)
                expect(frame.get_by_role("link", name="Скачать пакет для логистов")).to_be_visible()
                expect(frame.locator("#financialDocumentsRows")).to_contain_text("Invoice", timeout=5000)
                expect(frame.locator("#financialDocumentsRows")).to_contain_text("Контракт")
                expect(frame.locator("#financialDocumentsRows")).to_contain_text("КП логистов")
                expect(frame.locator("#financialDocumentsRows")).to_contain_text("Не загружен")
                frame.get_by_role("tab", name="Состав поставки").click()
                expect(frame.get_by_role("button", name="Проверить цены")).to_be_enabled()
                frame.get_by_role("button", name="Проверить цены").click()
                expect(frame.locator("#cardMessage")).to_contain_text("Проверка цен обновлена.", timeout=5000)
                expect(frame.locator("#shipmentRows").get_by_text("26GN390")).to_be_visible()
                expect(frame.locator("#shipmentRows").get_by_text("2026-05-16")).to_be_visible()
                expect(frame.locator("#shipmentRows").get_by_text("HanShang Technology")).to_be_visible()
                expect(frame.locator("#shipmentRows").get_by_text("Проверить")).to_be_visible()
                frame.get_by_role("button", name="Закрыть").click()
                expect(frame.locator(".registry-wrap thead")).to_contain_text("Ориент. себестоимость, ₽/шт")
                expect(frame.locator("#shipmentRows")).to_contain_text("25,45", timeout=5000)
                expect(frame.locator("#shipmentCard")).to_be_hidden()
                _seed_accepted_ff_supplier_order(entrypoint, invoice_path)
                frame.locator("body").evaluate("() => window.location.reload()")
                expect(frame.locator("#orderStatusFilter")).to_be_visible(timeout=5000)
                expect(frame.locator("#orderStatusFilter input[value='production']")).to_be_checked()
                expect(frame.locator("#orderStatusFilter input[value='in_transit']")).to_be_checked()
                expect(frame.locator("#orderStatusFilter input[value='accepted_ff']")).not_to_be_checked()
                expect(frame.locator("#shipmentRows").get_by_text("26GN390")).to_be_visible(timeout=5000)
                expect(frame.locator("#shipmentRows")).not_to_contain_text("26GN391")
                frame.locator("#orderStatusFilter input[value='all']").check()
                expect(frame.locator("#shipmentRows").get_by_text("26GN391")).to_be_visible(timeout=5000)
                accepted_row = frame.locator("#shipmentRows tr[data-row]", has_text="26GN391").first
                expect(accepted_row.locator("[data-order-status-shipment]")).to_have_count(0)
                expect(accepted_row.locator(".badge", has_text="Принято на ФФ")).to_be_visible()
                frame.locator("#orderStatusFilter input[value='all']").uncheck()
                expect(frame.locator("#orderStatusFilter input[value='production']")).to_be_checked()
                expect(frame.locator("#orderStatusFilter input[value='in_transit']")).to_be_checked()
                expect(frame.locator("#shipmentRows").get_by_text("26GN390")).to_be_visible(timeout=5000)
                expect(frame.locator("#shipmentRows")).not_to_contain_text("26GN391")
                header_texts = frame.locator(".registry-wrap thead th:visible").evaluate_all(
                            "(nodes) => nodes.map((node) => node.textContent.trim())"
                        )
                if any("Currency" in text or "Валюта" in text for text in header_texts):
                        raise AssertionError(f"operator registry must hide Currency column, got {header_texts}")
                try:
                        invoice_index = next(index for index, text in enumerate(header_texts) if text == "Документы")
                        status_index = header_texts.index("Статус заказа")
                        actions_index = next(index for index, text in enumerate(header_texts) if text == "Действия")
                except StopIteration as exc:
                        raise AssertionError(f"operator registry must expose invoice/status/actions headers, got {header_texts}") from exc
                if not invoice_index < status_index < actions_index:
                        raise AssertionError(f"order status header must be after invoice and before actions, got {header_texts}")
                active_row = frame.locator("#shipmentRows tr[data-row]", has_text="26GN390").first
                status_select = active_row.locator("[data-order-status-shipment]")
                expect(status_select).to_have_value("production")
                expect(status_select.locator("option:checked")).to_have_text("На производстве")
                status_select.select_option("in_transit")
                expect(frame.locator("#registryMessage")).to_contain_text("Статус заказа сохранён.", timeout=5000)
                expect(frame.locator("#shipmentCard")).to_be_hidden()
                frame.locator("body").evaluate("() => window.location.reload()")
                expect(frame.locator("#shipmentRows").get_by_text("26GN390")).to_be_visible(timeout=5000)
                active_row = frame.locator("#shipmentRows tr[data-row]", has_text="26GN390").first
                expect(active_row.locator("[data-order-status-shipment]")).to_have_value("in_transit")
                expect(active_row.locator("a[data-download]").first).to_have_text("Скачать invoice")
                expect(active_row.locator("[data-delete-shipment]").first).to_have_text("Удалить")
                active_row.click()
                expect(active_row).to_have_class(re.compile(r"(^|\\s)is-active(\\s|$)"))
                active_row_style = active_row.locator("td").first.evaluate(
                    """(node) => {
                        const styles = window.getComputedStyle(node);
                        return {
                            backgroundColor: styles.backgroundColor,
                            boxShadow: styles.boxShadow,
                            outlineStyle: styles.outlineStyle,
                            outlineWidth: styles.outlineWidth
                        };
                    }"""
                )
                if active_row_style.get("boxShadow") not in ("none", ""):
                    raise AssertionError(f"active supplier row must not use per-cell blue inset shadow: {active_row_style}")
                if "56, 189, 248" in str(active_row_style.get("backgroundColor") or ""):
                    raise AssertionError(f"active supplier row must not use bright cyan background: {active_row_style}")
                if active_row_style.get("outlineStyle") not in ("none", "") and active_row_style.get("outlineWidth") != "0px":
                    raise AssertionError(f"active supplier row must not use per-cell outline: {active_row_style}")
                expect(frame.get_by_label("Фактическая дата отгрузки")).to_have_value("2026-05-16")
                expect(frame.get_by_label("Фактическая дата приёмки на ФФ")).to_have_value("")
                expect(frame.get_by_label("Примерный курс юаня, ₽/¥")).to_have_value("13.2")
                expect(frame.get_by_role("link", name="下载发票 / Download invoice / Скачать invoice")).to_have_count(0)
                expect(frame.get_by_role("tab", name="Документы")).to_be_visible()
                frame.get_by_role("tab", name="Документы").click()
                expect(frame.locator("#documentInvoiceDownloadLink")).to_be_visible()
                expect(frame.locator("#contractDocumentLabel")).to_contain_text("Контракт: выбрать")
                expect(frame.get_by_role("button", name="Проверить цены")).to_be_enabled()
                expect(frame.get_by_role("button", name="重新匹配 / Re-match / Пересопоставить")).to_have_count(0)
                frame.get_by_role("button", name="Закрыть").click()
                expect(frame.locator("#shipmentCard")).to_be_hidden()
                _seed_first_supplier_quote_and_customs_documents(runtime)

                operator_frame.get_by_role("button", name="Реестр поставок").click()
                expect(operator_frame.locator("#shipmentRegistryTitle")).to_be_visible(timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryBody")).to_contain_text("A. Паспорт поставки", timeout=10000)
                expect(operator_frame.locator("#shipmentRegistryBody")).to_contain_text("B. КП логиста", timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryBody")).to_contain_text("C. Нормализованные метрики по КП", timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryBody")).to_contain_text("D. Сроки", timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryBody")).to_contain_text("E. Физика груза", timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryBody")).to_contain_text("F. Стоимость товара", timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryBody")).to_contain_text("G. Факт расходов", timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryBody")).to_contain_text("H. Нормализованные метрики факта / по ДТ", timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryBody")).to_contain_text("I. Документы", timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryHead")).to_contain_text("26GN390", timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryBody")).to_contain_text("КП: услуги логиста, USD/кг по весу КП", timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryBody")).to_contain_text("КП: таможня, USD/кг по весу КП", timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryBody")).to_contain_text("КП: доставка+таможня, USD/кг по весу КП", timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryBody")).to_contain_text("КП: доставка+таможня, ₽/шт", timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryBody")).to_contain_text("ждём счета", timeout=5000)
                waiting_quote_cell = operator_frame.locator(
                    ".shipment-registry-cell-main.is-registry-warning",
                    has_text="ждём счета",
                ).first
                expect(waiting_quote_cell).to_be_visible(timeout=5000)
                waiting_quote_title = waiting_quote_cell.locator("xpath=..").get_attribute("title") or ""
                if "sanity-check" not in waiting_quote_title or "после загрузки всех счетов логиста" not in waiting_quote_title:
                    raise AssertionError(f"waiting quote RUB cell must keep explanatory tooltip, got {waiting_quote_title!r}")
                expect(operator_frame.locator("#shipmentRegistryBody")).to_contain_text("факт доставка+таможня ₽/шт", timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryBody")).to_contain_text("Плановая дата отгрузки", timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryBody")).to_contain_text("Фактическая дата отгрузки", timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryBody")).to_contain_text("Фактическая дата приёмки на ФФ", timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryBody")).not_to_contain_text("Ориентировочная себестоимость на ФФ, ₽/шт")
                expect(operator_frame.locator("#shipmentRegistryBody")).to_contain_text("количество штук по packing list", timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryBody")).to_contain_text("стоимость товара по инвойсу, ₽/шт", timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryBody")).to_contain_text("полнота расходов", timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryBody")).to_contain_text("Плановый срок производства", timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryBody")).to_contain_text("Фактический срок производства", timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryBody")).to_contain_text("Фактический срок доставки", timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryBody")).to_contain_text("12 дн.", timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryBody")).to_contain_text("Срок до ДТ", timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryBody")).to_contain_text("—", timeout=5000)
                registry_text = operator_frame.locator("#shipmentRegistryBody").inner_text()
                lowered_registry_text = registry_text.lower()
                if "отклонение срока" in lowered_registry_text:
                    raise AssertionError(f"shipment registry browser output contains misleading lead-time rows: {registry_text}")
                expect(operator_frame.locator("#shipmentRegistryQuoteFileButton")).to_be_visible(timeout=5000)
                expect(operator_frame.locator("[data-shipment-registry-select]").first).to_be_visible(timeout=5000)
                expect(operator_frame.locator("[data-shipment-registry-detail]").first).to_be_visible(timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryCompareButton")).to_be_disabled()
                operator_frame.locator("#shipmentRegistryQuoteFileInput").set_input_files(str(quote_comparison_path))
                expect(operator_frame.locator("#shipmentRegistryQuoteFileName")).to_contain_text("quote-comparison.pdf", timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryCompareButton")).to_be_disabled()
                expect(operator_frame.locator("#shipmentRegistryDetailBlock")).to_be_hidden()
                operator_frame.locator("[data-shipment-registry-select]").first.check()
                expect(operator_frame.locator("#shipmentRegistryDetailBlock")).to_be_hidden()
                expect(operator_frame.locator("#shipmentRegistryCompareButton")).to_be_enabled(timeout=5000)
                expense_select = operator_frame.locator("[data-shipment-registry-expenses]").first
                expect(expense_select).to_have_value("false")
                expect(expense_select).to_have_class(re.compile(r"(^|\s)is-expenses-incomplete(\s|$)"))
                expect(operator_frame.locator(".shipment-registry-cell-main.is-expenses-partial").first).to_be_visible(timeout=5000)
                expense_select.select_option("true")
                expect(operator_frame.locator("#shipmentRegistryMessage")).to_contain_text("Статус полноты расходов сохранён.", timeout=5000)
                expect(operator_frame.locator("[data-shipment-registry-expenses]").first).to_have_class(re.compile(r"(^|\s)is-expenses-complete(\s|$)"))
                expect(operator_frame.locator(".shipment-registry-cell-main.is-expenses-partial")).to_have_count(0)
                operator_frame.locator("#shipmentRegistryRefreshButton").click()
                expect(operator_frame.locator("[data-shipment-registry-expenses]").first).to_have_value("true", timeout=5000)
                expect(operator_frame.locator("[data-shipment-registry-expenses]").first).to_have_class(re.compile(r"(^|\s)is-expenses-complete(\s|$)"))
                expect(operator_frame.locator(".shipment-registry-cell-main.is-expenses-partial")).to_have_count(0)
                operator_frame.locator("[data-shipment-registry-expenses]").first.select_option("false")
                expect(operator_frame.locator("#shipmentRegistryMessage")).to_contain_text("Статус полноты расходов сохранён.", timeout=5000)
                expect(operator_frame.locator("[data-shipment-registry-expenses]").first).to_have_class(re.compile(r"(^|\s)is-expenses-incomplete(\s|$)"))
                operator_frame.locator("[data-shipment-registry-expenses]").first.select_option("true")
                expect(operator_frame.locator("#shipmentRegistryMessage")).to_contain_text("Статус полноты расходов сохранён.", timeout=5000)
                operator_frame.locator("[data-shipment-registry-detail]").first.click()
                expect(operator_frame.locator("#shipmentRegistryDetailBlock")).to_be_visible(timeout=10000)
                detail_frame = operator_frame.frame_locator("iframe[title='Детализация поставки']")
                expect(detail_frame.get_by_role("tab", name="Состав поставки")).to_be_visible(timeout=10000)
                expect(detail_frame.get_by_role("tab", name="Документы")).to_be_visible(timeout=10000)
                operator_frame.locator("#shipmentRegistryCompareButton").click()
                try:
                    expect(operator_frame.locator("#shipmentRegistryComparisonBlock")).to_be_visible(timeout=10000)
                except AssertionError as exc:
                    message = operator_frame.locator("#shipmentRegistryMessage").inner_text(timeout=1000)
                    button_text = operator_frame.locator("#shipmentRegistryCompareButton").inner_text(timeout=1000)
                    raise AssertionError(
                        f"shipment registry comparison did not render; message={message!r}; compare_button={button_text!r}"
                    ) from exc
                expect(operator_frame.locator("#shipmentRegistryComparisonBody")).to_contain_text("КП: доставка+таможня, % от стоимости груза", timeout=10000)
                expect(operator_frame.locator("#shipmentRegistryComparisonBody")).to_contain_text("52.59%", timeout=10000)
                expect(operator_frame.locator(".shipment-registry-comparison-table thead")).to_contain_text("Оценка КП", timeout=5000)
                expect(operator_frame.locator("#shipmentRegistryComparisonBody")).to_contain_text("Срок до ДТ", timeout=5000)
                comparison_text = operator_frame.locator("#shipmentRegistryComparisonBody").inner_text()
                if "NaN" in comparison_text or "Infinity" in comparison_text:
                    raise AssertionError(f"shipment registry comparison browser output contains invalid numbers: {comparison_text}")
                comparison_header_text = operator_frame.locator(".shipment-registry-comparison-table thead").inner_text()
                if "Вывод" in comparison_header_text:
                    raise AssertionError("shipment registry comparison header must use Оценка КП, not Вывод")
                lowered_comparison_text = comparison_text.lower()
                if "фактический срок" in lowered_comparison_text or "отклонение срока" in lowered_comparison_text:
                    raise AssertionError(f"shipment registry comparison contains misleading lead-time rows: {comparison_text}")
                if "лучше" in comparison_text or "хуже" in comparison_text:
                    raise AssertionError(f"shipment registry comparison must not expose bare better/worse statuses: {comparison_text}")
                if "оценочно" not in comparison_text and "Нет коэффициента шт/кг для оценки КП" not in comparison_text:
                    raise AssertionError(f"shipment registry comparison must explain quote ₽/шт estimator: {comparison_text}")
                if "NaN" in registry_text or "Infinity" in registry_text:
                    raise AssertionError(f"shipment registry browser output contains invalid numbers: {registry_text}")
                sticky_style = operator_frame.locator("#shipmentRegistryBody td.shipment-registry-sticky").first.evaluate(
                    """(node) => {
                        const styles = window.getComputedStyle(node);
                        return {position: styles.position, left: styles.left};
                    }"""
                )
                if sticky_style.get("position") != "sticky" or sticky_style.get("left") != "0px":
                    raise AssertionError(f"shipment registry row labels must stay sticky, got {sticky_style}")
                operator_frame.get_by_role("button", name="От поставщика").click()
                expect(operator_frame.locator("iframe[title='От поставщика']")).to_be_visible(timeout=5000)

                supplier_page = browser.new_page(viewport={"width": 1280, "height": 900})
                supplier_page.goto(f"{base_url}{DEFAULT_SHEET_SUPPLIER_UI_PATH}", wait_until="domcontentloaded")
                expect(supplier_page.locator("h1", has_text="订单登记表 / Order registry / Реестр заказов")).to_be_visible()
                expect(supplier_page.get_by_role("link", name="Открыть отдельно")).to_have_count(0)
                expect(supplier_page.locator("#priceCheckButton")).to_have_count(0)
                expect(supplier_page.get_by_role("button", name="Проверить цены")).to_have_count(0)
                expect(supplier_page.locator(".registry-wrap thead")).to_contain_text("预估成本 / Est. cost / Ориент. себестоимость, ₽/шт")
                expect(supplier_page.get_by_text("26GN390")).to_be_visible()
                expect(supplier_page.locator("#shipmentRows")).to_contain_text(re.compile(r"10\s*560,42"), timeout=5000)
                expect(supplier_page.locator("[data-order-status-shipment]")).to_have_count(0)
                frame.locator("#shipmentRows tr[data-row]", has_text="26GN390").first.locator("[data-delete-shipment]").click()
                expect(frame.locator("[data-delete-confirmation]")).to_be_visible()
                expect(frame.locator("#shipmentRows")).to_contain_text("26GN390")
                frame.locator("#shipmentRows tr[data-row]", has_text="26GN390").first.click()
                expect(frame.locator("#shipmentCard")).to_be_hidden()
                frame.locator("[data-delete-cancel]").click()
                expect(frame.locator("[data-delete-confirmation]")).to_have_count(0)
                expect(frame.locator("#shipmentRows")).to_contain_text("26GN390")
                frame.locator("#shipmentRows tr[data-row]", has_text="26GN390").first.locator("[data-delete-shipment]").click()
                expect(frame.locator("[data-delete-confirmation]")).to_be_visible()
                frame.locator("[data-delete-confirm]").click()
                expect(frame.locator("#registryMessage")).to_contain_text("Заказ удалён.", timeout=5000)
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
            expect(page.get_by_label("计划出货日期 / Planned shipment date / Плановая дата отгрузки")).to_be_visible()
            expect(page.get_by_label("实际出货日期 / Actual shipment date / Фактическая дата отгрузки")).to_be_visible()
            expect(page.get_by_label("实际入仓日期 / Actual ФФ acceptance date / Фактическая дата приёмки на ФФ")).to_be_visible()
            expect(page.get_by_label("预估人民币汇率 / Estimated CNY rate / Примерный курс юаня, ₽/¥")).to_be_visible()
            expect(page.get_by_label("预估人民币汇率 / Estimated CNY rate / Примерный курс юаня, ₽/¥")).to_have_value("")
            expect(page.get_by_text("价格匹配 / Price check / Соответствие цены")).to_be_visible()
            page.locator("#invoiceFileInput").set_input_files(str(invoice_path))
            expect(page.locator("#productLines input[data-line-field='model_raw']").first).to_be_visible()
            expect(page.locator("#productLines .price-conformity")).to_have_count(3)
            expect(page.get_by_role("button", name="Проверить цены")).to_have_count(0)
            page.get_by_label("计划出货日期 / Planned shipment date / Плановая дата отгрузки").fill("2026-05-14")
            page.get_by_label("实际出货日期 / Actual shipment date / Фактическая дата отгрузки").fill("2026-05-16")
            expect(page.get_by_role("button", name="保存 / Save / Сохранить")).to_be_enabled()
            page.get_by_role("button", name="保存 / Save / Сохранить").click()
            expect(page.get_by_text("订单已保存 / Order saved / Заказ сохранён.")).to_be_visible(timeout=5000)
            expect(page.locator("#priceCheckButton")).to_have_count(0)
            expect(page.get_by_role("button", name="Проверить цены")).to_have_count(0)
            expect(page.locator(".registry-wrap thead")).to_contain_text("预估成本 / Est. cost / Ориент. себестоимость, ₽/шт")
            expect(page.locator("#shipmentRows tr[data-row]").first).to_contain_text("—")
            shipment_id = page.locator("#shipmentRows tr[data-row]").first.get_attribute("data-row") or ""
            if not shipment_id:
                raise AssertionError("supplier browser smoke must create a shipment row before price-check probe")
            page.get_by_role("button", name="关闭 / Close / Закрыть").click()
            expect(page.locator("#shipmentCard")).to_be_hidden()
            page.locator("#shipmentRows tr[data-row]").first.click()
            expect(page.locator("#shipmentCard")).to_be_visible()
            expect(page.get_by_label("实际出货日期 / Actual shipment date / Фактическая дата отгрузки")).to_have_value("2026-05-16")
            expect(page.get_by_label("实际入仓日期 / Actual ФФ acceptance date / Фактическая дата приёмки на ФФ")).to_have_value("")
            expect(page.get_by_label("预估人民币汇率 / Estimated CNY rate / Примерный курс юаня, ₽/¥")).to_have_value("")
            expect(page.get_by_text("价格匹配 / Price check / Соответствие цены")).to_be_visible()
            expect(page.locator("#priceCheckButton")).to_have_count(0)
            expect(page.get_by_role("button", name="Проверить цены")).to_have_count(0)
            page.reload(wait_until="domcontentloaded")
            expect(page.locator("h1", has_text="订单登记表 / Order registry / Реестр заказов")).to_be_visible()
            expect(page.locator("#shipmentRows tr[data-row]")).to_have_count(1)
            page.locator("#shipmentRows tr[data-row]").first.click()
            expect(page.locator("#shipmentCard")).to_be_visible()
            expect(page.get_by_text("价格匹配 / Price check / Соответствие цены")).to_be_visible()
            expect(page.locator("#priceCheckButton")).to_have_count(0)
            expect(page.get_by_role("button", name="Проверить цены")).to_have_count(0)
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
            documents_probe = page.evaluate(
                """async ({shipmentsPath, shipmentId}) => {
                    const response = await fetch(shipmentsPath + "/" + encodeURIComponent(shipmentId) + "/documents", {
                        method: "GET",
                        headers: {"Accept": "application/json"}
                    });
                    let payload = {};
                    try { payload = await response.json(); } catch (error) { payload = {error: String(error)}; }
                    return {status: response.status, payload};
                }""",
                {"shipmentsPath": DEFAULT_SUPPLIER_SHIPMENTS_PATH, "shipmentId": shipment_id},
            )
            if documents_probe.get("status") != 403 or documents_probe.get("payload", {}).get("error") != "forbidden":
                raise AssertionError(f"supplier must not call operator documents route, got {documents_probe}")
        finally:
            if context is not None:
                context.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def _seed_first_supplier_factual_expense(runtime: RegistryUploadDbBackedRuntime, *, amount_rub: float) -> None:
    shipments = runtime.list_supplier_shipments()
    if not shipments:
        raise AssertionError("cannot seed factual expense without a supplier shipment")
    shipment_id = str(shipments[0].get("shipment_id") or "")
    if not shipment_id:
        raise AssertionError(f"cannot seed factual expense for shipment without id: {shipments[0]}")
    runtime.save_supplier_financial_document(
        document={
            "document_id": f"fdoc_{shipment_id}_browser_logistics",
            "supplier_order_id": shipment_id,
            "document_type": "logistics_invoice",
            "original_filename": "browser-factual-logistics.pdf",
            "stored_file_path": "",
            "file_content_type": "application/pdf",
            "file_sha256": "",
            "uploaded_at": "2026-05-30T08:00:00Z",
            "updated_at": "2026-05-30T08:00:00Z",
            "parse_status": "parsed",
            "vendor": "Browser Logistics",
            "document_number": "BROWSER-EXPENSE",
            "document_date": "2026-05-20",
            "currency": "RUB",
            "total_amount": amount_rub,
            "total_amount_rub": amount_rub,
            "cbr_usd_rate_value": 75.0,
            "normalized_parse": {"document_type": "logistics_invoice", "amount_rub": amount_rub},
            "raw_parse": {},
            "parser_version": "browser-smoke",
            "warnings": [],
            "errors": [],
        },
        expense_lines=[
            {
                "line_id": f"fline_{shipment_id}_browser_logistics",
                "financial_document_id": f"fdoc_{shipment_id}_browser_logistics",
                "supplier_order_id": shipment_id,
                "sort_order": 1,
                "category": "domestic_transport",
                "stage": "fact",
                "description": "Browser smoke factual logistics expense",
                "amount": amount_rub,
                "currency": "RUB",
                "amount_rub": amount_rub,
                "vat_rate": None,
                "vat_amount_rub": None,
                "included_in_logistics_efficiency": True,
                "included_in_customs_total": False,
                "status": "parsed",
                "confidence": 1.0,
                "raw": {},
            }
        ],
    )


def _seed_first_supplier_quote_and_customs_documents(runtime: RegistryUploadDbBackedRuntime) -> None:
    shipments = runtime.list_supplier_shipments()
    if not shipments:
        raise AssertionError("cannot seed quote/customs documents without a supplier shipment")
    shipment_id = str(shipments[0].get("shipment_id") or "")
    if not shipment_id:
        raise AssertionError(f"cannot seed quote/customs documents for shipment without id: {shipments[0]}")
    runtime.save_supplier_financial_document(
        document={
            "document_id": f"fdoc_{shipment_id}_browser_quote",
            "supplier_order_id": shipment_id,
            "document_type": "logistics_quote",
            "original_filename": "browser-quote.pdf",
            "stored_file_path": "",
            "file_content_type": "application/pdf",
            "file_sha256": "",
            "uploaded_at": "2026-05-30T08:00:00Z",
            "updated_at": "2026-05-30T08:00:00Z",
            "parse_status": "parsed",
            "vendor": "Browser Logistics",
            "document_number": "BROWSER-QUOTE",
            "document_date": "2026-05-14",
            "currency": "USD",
            "total_amount": 300.0,
            "total_amount_rub": 22500.0,
            "cbr_usd_rate_value": 75.0,
            "normalized_parse": {
                "document_type": "logistics_quote",
                "quote_date": "2026-05-14",
                "gross_weight_kg": 100.0,
                "volume_m3": 1.0,
                "estimated_cargo_value_usd": 1000.0,
                "estimated_cargo_value_cny": 7000.0,
                "delivery_days_min": 25,
                "delivery_days_max": 30,
                "quote_required_amounts_complete": True,
                "quote_missing_required_amounts": [],
            },
            "raw_parse": {},
            "parser_version": "browser-smoke",
            "warnings": [],
            "errors": [],
        },
        expense_lines=[
            {
                "line_id": f"fline_{shipment_id}_browser_quote_delivery",
                "financial_document_id": f"fdoc_{shipment_id}_browser_quote",
                "supplier_order_id": shipment_id,
                "sort_order": 1,
                "category": "delivery_cost",
                "stage": "quote",
                "description": "Browser smoke quote delivery",
                "amount": 100.0,
                "currency": "USD",
                "amount_rub": 7500.0,
                "vat_rate": None,
                "vat_amount_rub": None,
                "included_in_logistics_efficiency": True,
                "included_in_customs_total": False,
                "status": "parsed",
                "confidence": 1.0,
                "raw": {},
            },
            {
                "line_id": f"fline_{shipment_id}_browser_quote_customs",
                "financial_document_id": f"fdoc_{shipment_id}_browser_quote",
                "supplier_order_id": shipment_id,
                "sort_order": 2,
                "category": "customs_payments_and_fees",
                "stage": "quote",
                "description": "Browser smoke quote customs",
                "amount": 200.0,
                "currency": "USD",
                "amount_rub": 15000.0,
                "vat_rate": None,
                "vat_amount_rub": None,
                "included_in_logistics_efficiency": False,
                "included_in_customs_total": True,
                "status": "parsed",
                "confidence": 1.0,
                "raw": {},
            },
        ],
    )
    runtime.save_supplier_financial_document(
        document={
            "document_id": f"fdoc_{shipment_id}_browser_customs",
            "supplier_order_id": shipment_id,
            "document_type": "customs_declaration",
            "original_filename": "browser-customs.pdf",
            "stored_file_path": "",
            "file_content_type": "application/pdf",
            "file_sha256": "",
            "uploaded_at": "2026-05-30T08:00:00Z",
            "updated_at": "2026-05-30T08:00:00Z",
            "parse_status": "parsed",
            "vendor": "ФТС",
            "document_number": "BROWSER-CUSTOMS",
            "document_date": "2026-05-22",
            "currency": "RUB",
            "total_amount": 600.0,
            "total_amount_rub": 600.0,
            "normalized_parse": {
                "document_type": "customs_declaration",
                "declaration_date": "2026-05-22",
                "gross_weight_kg": 120.0,
                "total_customs_value_rub": 10000.0,
            },
            "raw_parse": {},
            "parser_version": "browser-smoke",
            "warnings": [],
            "errors": [],
        },
        expense_lines=[
            {
                "line_id": f"fline_{shipment_id}_browser_customs_fee",
                "financial_document_id": f"fdoc_{shipment_id}_browser_customs",
                "supplier_order_id": shipment_id,
                "sort_order": 1,
                "category": "customs_fee_1010",
                "stage": "fact",
                "description": "Browser smoke customs fee",
                "amount": 100.0,
                "currency": "RUB",
                "amount_rub": 100.0,
                "vat_rate": None,
                "vat_amount_rub": None,
                "included_in_logistics_efficiency": False,
                "included_in_customs_total": True,
                "status": "parsed",
                "confidence": 1.0,
                "raw": {},
            },
            {
                "line_id": f"fline_{shipment_id}_browser_customs_duty",
                "financial_document_id": f"fdoc_{shipment_id}_browser_customs",
                "supplier_order_id": shipment_id,
                "sort_order": 2,
                "category": "import_duty_2010",
                "stage": "fact",
                "description": "Browser smoke import duty",
                "amount": 200.0,
                "currency": "RUB",
                "amount_rub": 200.0,
                "vat_rate": None,
                "vat_amount_rub": None,
                "included_in_logistics_efficiency": False,
                "included_in_customs_total": True,
                "status": "parsed",
                "confidence": 1.0,
                "raw": {},
            },
            {
                "line_id": f"fline_{shipment_id}_browser_customs_vat",
                "financial_document_id": f"fdoc_{shipment_id}_browser_customs",
                "supplier_order_id": shipment_id,
                "sort_order": 3,
                "category": "import_vat_5010",
                "stage": "fact",
                "description": "Browser smoke import VAT",
                "amount": 300.0,
                "currency": "RUB",
                "amount_rub": 300.0,
                "vat_rate": None,
                "vat_amount_rub": None,
                "included_in_logistics_efficiency": False,
                "included_in_customs_total": True,
                "status": "parsed",
                "confidence": 1.0,
                "raw": {},
            },
        ],
    )


def _seed_first_supplier_exact_cny_cost(runtime: RegistryUploadDbBackedRuntime, *, payment_cost_rub: float) -> None:
    shipments = runtime.list_supplier_shipments()
    if not shipments:
        raise AssertionError("cannot seed exact CNY cost without a supplier shipment")
    shipment_id = str(shipments[0].get("shipment_id") or "")
    if not shipment_id:
        raise AssertionError(f"cannot seed exact CNY cost for shipment without id: {shipments[0]}")
    runtime.update_supplier_shipments_cny_calculations(
        [
            {
                "shipment_id": shipment_id,
                "cny_ledger_effective_rate": "10",
                "cny_payment_currency_rub_cost": str(payment_cost_rub),
                "cny_paid_amount": str(payment_cost_rub / 10),
                "cny_bank_fee_rub": "0",
                "cny_calculation_status": "ok",
                "cny_calculation_error": "",
                "cny_calculated_at": "2026-05-30T08:00:00Z",
            }
        ]
    )


def _seed_accepted_ff_supplier_order(
    entrypoint: RegistryUploadHttpEntrypoint,
    invoice_path: Path,
) -> str:
    parsed = entrypoint.handle_supplier_shipments_parse_request(
        invoice_path.read_bytes(),
        uploaded_filename="accepted-ff-browser-invoice.xlsx",
    )
    edited_payload = dict(parsed)
    metadata = dict(edited_payload.get("metadata") or {})
    metadata["invoice_no"] = "26GN391"
    edited_payload["metadata"] = metadata
    payload = {
        "upload_id": parsed.get("upload_id"),
        "shipment_date": "2026-05-14",
        "actual_shipment_date": "2026-05-16",
        "actual_ff_acceptance_date": "2026-05-28",
        "payload": edited_payload,
    }
    created = entrypoint.handle_supplier_shipments_create_request(payload)
    shipment_id = str(created.get("shipment_id") or "")
    if not shipment_id:
        raise AssertionError(f"accepted_ff seed shipment was not created: {created}")
    return shipment_id


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


def _fixture_financial_text_extractor(file_bytes: bytes, filename: str):
    del file_bytes
    if str(filename or "") == "quote-comparison.pdf":
        return QUOTE_COMPARISON_TEXT, {"method": "fixture_text", "filename": filename}, []
    return "", {"method": "fixture_text", "filename": filename}, ["fixture text is not configured for this filename"]


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


def _build_contract_xlsx_fixture(number: str, date_text: str) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Contract"
    sheet.append([f"Contract No. {number}"])
    sheet.append(["Contract Date", date_text])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
