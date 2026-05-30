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
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_SUPPLIER_UI_PATH,
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
                page.goto(f"{base_url}{DEFAULT_SHEET_OPERATOR_UI_PATH}?embedded_tab=factory-order", wait_until="domcontentloaded")
                expect(page.get_by_role("button", name="Расчёты")).to_be_visible()
                expect(page.get_by_role("button", name="От поставщика")).to_be_visible()
                page.get_by_role("button", name="От поставщика").click()
                frame = page.frame_locator("iframe[title='От поставщика']")
                expect(frame.get_by_text("Реестр поставок")).to_be_visible()
                expect(frame.get_by_role("button", name="Добавить поставку")).to_be_visible()
                frame.get_by_role("button", name="Добавить поставку").click()
                expect(frame.get_by_label("Дата отгрузки / Shipment date")).to_be_visible()
                expect(frame.get_by_role("button", name="Сохранить")).to_be_disabled()
                frame.locator("#invoiceFileInput").set_input_files(str(invoice_path))
                expect(frame.locator("#productLines input[data-line-field='model_raw']").first).to_be_visible()
                frame.get_by_label("Дата отгрузки / Shipment date").fill("2026-05-14")
                expect(frame.get_by_role("button", name="Сохранить")).to_be_enabled()
                frame.get_by_role("button", name="Сохранить").click()
                expect(frame.get_by_text("Поставка сохранена.")).to_be_visible(timeout=5000)
                expect(frame.locator("#shipmentRows").get_by_text("26GN390")).to_be_visible()
                frame.locator("#shipmentRows tr[data-row]").first.click()
                expect(frame.get_by_role("link", name="Скачать invoice")).to_be_visible()

                supplier_page = browser.new_page(viewport={"width": 1280, "height": 900})
                supplier_page.goto(f"{base_url}{DEFAULT_SHEET_SUPPLIER_UI_PATH}", wait_until="domcontentloaded")
                expect(supplier_page.get_by_text("Реестр поставок")).to_be_visible()
                expect(supplier_page.get_by_text("26GN390")).to_be_visible()
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
    sheet.append(["Invoice Total:", 25])
    sheet.append(["NO.", "NAME & SPECIFICATION", "MODELS", "QTY", "U.PRICE", "AMOUNT", "COMMENT"])
    sheet.append([1, "高清膜 smk", "iPhone 14 Pro", 10, 1, 10, ""])
    sheet.append([2, "防窥膜 (Anti-Spy)", "iPhone 14 Pro Max", 5, 2, 10, ""])
    sheet.append([3, "OPP bag packets", "", 100, 0.05, 5, "OPP packets"])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
