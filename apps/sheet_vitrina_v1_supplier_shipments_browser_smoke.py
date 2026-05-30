"""Browser smoke-check for supplier shipments UI in operator and supplier-only page."""

from __future__ import annotations

from io import BytesIO
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
                settings_page.get_by_role("button", name="Добавить строку").click()
                draft_row = settings_page.locator("#nomenclatureRows tr").first
                draft_row.locator("[data-field='our_sku']").fill("SKU-CLEAR-14P")
                draft_row.locator("[data-field='nm_id']").fill("210183919")
                draft_row.locator("[data-field='nomenclature_name']").fill("Clear iPhone 14 Pro")
                draft_row.locator("[data-field='match_key']").fill("clear|iphone_14_pro")
                draft_row.locator("[data-field='compatible_models_text']").fill("iPhone 14 Pro")
                draft_row.locator("[data-save-item]").click()
                expect(settings_page.locator("#nomenclatureMessage")).to_contain_text("Справочник сохранён.", timeout=5000)
                expect(draft_row.get_by_text("Keys: iphone_14_pro")).to_be_visible(timeout=5000)
                settings_page.get_by_role("button", name="Добавить строку").click()
                compat_row = settings_page.locator("#nomenclatureRows tr").first
                compat_row.locator("[data-field='our_sku']").fill("SKU-AS-141313P")
                compat_row.locator("[data-field='nm_id']").fill("391662410")
                compat_row.locator("[data-field='nomenclature_name']").fill("anti-spy iPhone 14 / 13 / 13Pro")
                compat_row.locator("[data-field='product_type']").select_option("anti_spy")
                compat_row.locator("[data-field='match_key']").fill("anti_spy|iphone_14_13_13pro")
                compat_row.locator("[data-field='compatible_models_text']").fill("iPhone 14, iPhone 13, iPhone 13 Pro")
                compat_row.locator("[data-save-item]").click()
                expect(settings_page.locator("#nomenclatureMessage")).to_contain_text("Справочник сохранён.", timeout=5000)
                page.goto(f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}", wait_until="domcontentloaded")
                expect(page.get_by_role("link", name="Настройки")).to_be_visible()
                page.locator("[data-unified-tab-button='factory-order']").click()
                operator_frame = page.frame_locator("iframe[title='Поставки']")
                expect(operator_frame.get_by_role("button", name="Расчёты")).to_be_visible()
                expect(operator_frame.get_by_role("button", name="От поставщика")).to_be_visible()
                operator_frame.get_by_role("button", name="От поставщика").click()
                frame = operator_frame.frame_locator("iframe[title='От поставщика']")
                expect(frame.locator("h1", has_text="订单登记表 / Order registry / Реестр заказов")).to_be_visible()
                expect(frame.locator("h1", has_text="订单登记表 / Order registry / Реестр заказов")).to_have_count(1)
                expect(frame.locator("h2", has_text="Реестр заказов")).to_have_count(0)
                expect(frame.get_by_text("Invoice-заказы поставщиков, сохранённые в WebCore")).to_have_count(0)
                expect(frame.get_by_text("匹配 / Matching / Матчинг").first).to_be_visible()
                expect(frame.get_by_text("Реестр поставок")).to_have_count(0)
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
                expect(frame.locator("#productLines input[data-line-field='internal_sku']")).to_have_count(0)
                expect(frame.locator("#productLines input[data-line-field='internal_nm_id']").first).to_have_value("210183919")
                expect(frame.locator("#productLines").get_by_text("已匹配 / Matched / Сопоставлено")).to_be_visible()
                expect(frame.locator("#productLines").get_by_text("按兼容型号匹配 / Matched by compatibility / Сопоставлено по совместимости")).to_be_visible()
                expect(frame.locator("select[data-line-field='match_status']")).to_have_count(0)
                frame.get_by_label("出货日期 / Shipment date / Дата отгрузки").fill("2026-05-14")
                expect(frame.get_by_role("button", name="保存 / Save / Сохранить")).to_be_enabled()
                frame.get_by_role("button", name="保存 / Save / Сохранить").click()
                expect(frame.get_by_text("订单已保存 / Order saved / Заказ сохранён.")).to_be_visible(timeout=5000)
                expect(frame.locator("#shipmentRows").get_by_text("26GN390")).to_be_visible()
                expect(frame.locator("#shipmentRows").get_by_text("Check")).to_be_visible()
                frame.locator("#shipmentRows tr[data-row]").first.click()
                expect(frame.get_by_role("link", name="下载发票 / Download invoice / Скачать invoice")).to_be_visible()
                expect(frame.get_by_role("button", name="重新匹配 / Re-match / Пересопоставить")).to_be_visible()
                frame.get_by_role("button", name="关闭 / Close / Закрыть").click()
                expect(frame.locator("#shipmentCard")).to_be_hidden()

                supplier_page = browser.new_page(viewport={"width": 1280, "height": 900})
                supplier_page.goto(f"{base_url}{DEFAULT_SHEET_SUPPLIER_UI_PATH}", wait_until="domcontentloaded")
                expect(supplier_page.locator("h1", has_text="订单登记表 / Order registry / Реестр заказов")).to_be_visible()
                expect(supplier_page.get_by_text("26GN390")).to_be_visible()
                page.once("dialog", lambda dialog: dialog.accept())
                frame.locator("[data-delete-shipment]").first.click()
                expect(frame.locator("#registryMessage")).to_contain_text("订单已删除 / Order deleted / Заказ удалён.", timeout=5000)
                expect(frame.locator("#shipmentRows")).not_to_contain_text("26GN390")
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print("sheet_vitrina_v1_supplier_shipments_browser_smoke: OK")


def _build_invoice_fixture() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Invoice"
    sheet.append(["Invoice No:", "26GN390"])
    sheet.append(["Invoice Date:", "14.5.2026"])
    sheet.append(["Supplier:", "Zhejiang Supplier", "", "Currency:", "USD"])
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
