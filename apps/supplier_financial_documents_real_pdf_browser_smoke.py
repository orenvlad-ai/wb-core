"""Browser smoke-check for supplier order financial documents with local real PDFs."""

from __future__ import annotations

from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_SUPPLIER_UI_PATH,
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
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


SUPPLIER_ORDER_ID = "sup_financial_real_pdf"


def main() -> None:
    pdfs = _find_local_sample_pdfs()
    with TemporaryDirectory(prefix="supplier-financial-real-browser-") as tmp:
        tmp_path = Path(tmp)
        runtime_dir = tmp_path / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        _seed_supplier_order(runtime)
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
            activated_at_factory=lambda: "2026-06-19T08:00:00Z",
        )
        entrypoint.supplier_financial_documents_block = SupplierFinancialDocumentsBlock(
            runtime=runtime,
            timestamp_factory=lambda: "2026-06-19T08:00:00Z",
            usd_rate_provider=StaticUsdRateProvider(
                {
                    "2026-06-02": "78.00",
                    "2026-06-05": "77.50",
                    "2026-06-18": "78.20",
                }
            ),
        )
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{config.port}"
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    page = browser.new_page(viewport={"width": 1440, "height": 1000})
                    page.goto(f"{base_url}{DEFAULT_SHEET_SUPPLIER_UI_PATH}?embedded=operator", wait_until="domcontentloaded")
                    expect(page.locator("h1", has_text="订单登记表 / Order registry / Реестр заказов")).to_be_visible(timeout=10000)
                    expect(page.locator("#shipmentRows")).to_contain_text("FIN-REAL-ORDER", timeout=10000)
                    order_row = page.locator("#shipmentRows tr[data-row]", has_text="FIN-REAL-ORDER").first
                    expect(order_row).to_be_visible()
                    order_row.click()
                    expect(page.locator("#shipmentCard")).to_be_visible(timeout=5000)
                    expect(page.locator("#supplyCompositionTabButton")).to_be_visible()
                    expect(page.locator("#financialDocumentsTabButton")).to_be_visible()
                    expect(page.locator("#supplyCompositionPanel")).to_be_visible()
                    expect(page.locator("#productLines input[data-line-field='internal_name']")).to_have_value(
                        "Стекла для смартфона",
                        timeout=5000,
                    )

                    page.locator("#financialDocumentsTabButton").click()
                    expect(page.locator("#financialDocumentsPanel")).to_be_visible()
                    expect(page.locator("#financialDocumentsRows")).to_contain_text("Финансовые документы не загружены.", timeout=5000)

                    page.locator("#financialDocumentFileInput").set_input_files(
                        [str(pdfs[key]) for key in ("quote", "invoice_103", "invoice_113", "customs")]
                    )
                    expect(page.locator("#financialDocumentsMessage")).to_contain_text("Загрузка завершена: 4", timeout=30000)
                    expect(page.locator("#financialUploadProgress li")).to_have_count(4, timeout=5000)
                    expect(page.locator("#financialUploadProgress")).to_contain_text("Распознан", timeout=5000)

                    expect(page.locator("#financialDocumentsRows tr[data-financial-document-row]")).to_have_count(4, timeout=10000)
                    expect(page.locator("#financialDocumentsRows")).to_contain_text("КП логиста", timeout=5000)
                    expect(page.locator("#financialDocumentsRows")).to_contain_text("Счёт логиста", timeout=5000)
                    expect(page.locator("#financialDocumentsRows")).to_contain_text("ДТ", timeout=5000)
                    expect(page.locator("#financeQuoteTotalUsd")).to_contain_text("57 136", timeout=5000)
                    expect(page.locator("#financeQuoteLogisticsUsd")).to_contain_text("16 151", timeout=5000)
                    expect(page.locator("#financeQuoteCustomsUsd")).to_contain_text("40 985", timeout=5000)
                    expect(page.locator("#financeInvoiceRub")).to_contain_text("1 215 975", timeout=5000)
                    expect(page.locator("#financeInvoiceVatRub")).to_contain_text("57 665", timeout=5000)
                    expect(page.locator("#financeCustomsRub")).to_contain_text("2 892 511", timeout=5000)
                    expect(page.locator("#financeRubPerKg")).not_to_have_text("-")
                    expect(page.locator("#financeImpliedRate")).not_to_have_text("-")
                    expect(page.locator("#financeImpliedRate")).not_to_contain_text("3 799", timeout=5000)
                    expect(page.locator("#financeRateSpread")).not_to_contain_text("5 123", timeout=5000)
                    expect(page.locator("#financialWarnings")).to_be_visible()
                    expect(page.locator("#financialWarnings")).to_contain_text("reviewable", timeout=5000)

                    quote_row = page.locator("#financialDocumentsRows tr[data-financial-document-row]", has_text="КП логиста").first
                    quote_row.click()
                    expect(page.locator("#financialRecognizedFields")).to_contain_text("стекла для смартфона", timeout=5000)
                    expect(page.locator("#financialExpenseRows")).to_contain_text("Стоимость доставки", timeout=5000)
                    expect(page.locator("#financialExpenseRows")).to_contain_text("14 360,00 USD", timeout=5000)
                    expect(page.locator("#financialExpenseRows")).to_contain_text("Таможенные платежи", timeout=5000)
                    expect(page.locator("#financialExpenseRows")).to_contain_text("40 985,00 USD", timeout=5000)
                    expect(page.locator("#financialExpenseRows")).to_contain_text("320,00 USD", timeout=5000)
                    expect(page.locator("#financialExpenseRows")).to_contain_text("350,00 USD", timeout=5000)
                    expect(page.locator("#financialExpenseRows")).to_contain_text("1 121,00 USD", timeout=5000)
                    expect(page.locator("#financialWarnings")).not_to_contain_text("Quote parser did not find required amount", timeout=5000)
                    expect(page.locator("#financialDocumentsRows a[data-download]").first).to_be_visible()
                    expect(quote_row.locator("[data-delete-financial-document]")).to_be_visible()

                    page.once("dialog", lambda dialog: dialog.accept())
                    quote_row.locator("[data-delete-financial-document]").click()
                    expect(page.locator("#financialDocumentsMessage")).to_contain_text("Документ удалён.", timeout=10000)
                    expect(page.locator("#financialDocumentsRows tr[data-financial-document-row]")).to_have_count(3, timeout=10000)
                    expect(page.locator("#financialDocumentsRows")).not_to_contain_text("КП логиста", timeout=5000)
                    expect(page.locator("#financeQuoteLogisticsUsd")).to_contain_text("0,00 USD", timeout=5000)
                    expect(page.locator("#financeImpliedRate")).to_have_text("-", timeout=5000)
                    expect(page.locator("#financeInvoiceRub")).to_contain_text("1 215 975", timeout=5000)

                    page.locator("#financialDocumentFileInput").set_input_files(str(pdfs["quote"]))
                    expect(page.locator("#financialDocumentsMessage")).to_contain_text("Загрузка завершена: 1", timeout=15000)
                    expect(page.locator("#financialDocumentsRows tr[data-financial-document-row]")).to_have_count(4, timeout=10000)
                    expect(page.locator("#financeQuoteLogisticsUsd")).to_contain_text("16 151", timeout=5000)
                    expect(page.locator("#financeImpliedRate")).not_to_contain_text("3 799", timeout=5000)
                    quote_row = page.locator("#financialDocumentsRows tr[data-financial-document-row]", has_text="КП логиста").first
                    quote_row.click()
                    expect(page.locator("#financialExpenseRows")).to_contain_text("14 360,00 USD", timeout=5000)

                    for _ in range(4):
                        if page.locator("#financialDocumentsRows tr[data-financial-document-row]").count() <= 0:
                            break
                        page.once("dialog", lambda dialog: dialog.accept())
                        page.locator("#financialDocumentsRows [data-delete-financial-document]").first.click()
                        expect(page.locator("#financialDocumentsMessage")).to_contain_text("Документ удалён.", timeout=10000)
                    expect(page.locator("#financialDocumentsRows")).to_contain_text("Финансовые документы не загружены.", timeout=10000)
                    expect(page.locator("#financialExpenseRows")).to_contain_text("Расходные строки не загружены.", timeout=5000)
                    expect(page.locator("#financeImpliedRate")).to_have_text("-", timeout=5000)

                    page.locator("#supplyCompositionTabButton").click()
                    expect(page.locator("#supplyCompositionPanel")).to_be_visible()
                    expect(page.locator("#productLines input[data-line-field='model_raw']")).to_have_value(
                        "Стекла для смартфона",
                        timeout=5000,
                    )
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print("supplier_financial_documents_real_pdf_browser_smoke: OK")


def _find_local_sample_pdfs() -> dict[str, Path]:
    checked_dirs = [
        Path.home() / "Desktop" / "Финансовые документы",
        Path.home() / "Рабочий стол" / "Финансовые документы",
        Path.home() / "Desktop" / "Financial documents",
        Path.home() / "Desktop" / "финансовые документы",
        Path.home() / "Desktop" / "финанасовые документы",
    ]
    existing_dirs = [path for path in checked_dirs if path.is_dir()]
    if not existing_dirs:
        checked = "\n".join(str(path) for path in checked_dirs)
        raise FileNotFoundError(f"financial document sample folder not found; checked:\n{checked}")
    candidates = [path for directory in existing_dirs for path in directory.iterdir() if path.suffix.lower() == ".pdf"]
    if not candidates:
        checked = "\n".join(str(path) for path in checked_dirs)
        raise FileNotFoundError(f"financial document sample PDFs not found; checked:\n{checked}")

    def find_one(label: str, predicate) -> Path:
        matches = [path for path in candidates if predicate(path.name.lower())]
        if not matches:
            listed = "\n".join(str(path) for path in candidates)
            raise FileNotFoundError(f"missing {label} real PDF among:\n{listed}")
        return sorted(matches, key=lambda path: path.name)[0]

    return {
        "quote": find_one("Transitplus quote", lambda name: "9644" in name or "стекла" in name),
        "invoice_103": find_one("logistics invoice 103", lambda name: "103" in name),
        "invoice_113": find_one("logistics invoice 113", lambda name: "113" in name),
        "customs": find_one("customs declaration", lambda name: name.startswith("gtd_") or "5187132" in name),
    }


def _seed_supplier_order(runtime: RegistryUploadDbBackedRuntime) -> None:
    runtime.save_supplier_shipment(
        header={
            "shipment_id": SUPPLIER_ORDER_ID,
            "created_at": "2026-06-19T08:00:00Z",
            "updated_at": "2026-06-19T08:00:00Z",
            "shipment_date": "2026-06-02",
            "order_status": "in_transit",
            "invoice_no": "FIN-REAL-ORDER",
            "invoice_date": "2026-06-02",
            "contract_no": "ORE",
            "contract_date": "2026-06-04",
            "supplier_name": "HanShang Technology",
            "customer_name": "",
            "currency": "CNY",
            "product_qty_total": 1,
            "product_amount_total": 785087.50,
            "extras_amount_total": 0,
            "invoice_amount_total": 785087.50,
            "declared_invoice_total": 785087.50,
            "match_status": "all_matched",
            "source_filename": "browser-real-pdf-fixture.xlsx",
            "source_file_sha256": "",
            "source_file_path": "",
            "invoice_document_id": "",
            "parser_version": "fixture",
            "warnings": [],
            "errors": [],
        },
        lines=[
            {
                "line_id": "sup-fin-real-line-1",
                "line_type": "product",
                "sort_order": 1,
                "product_type": "clear",
                "model_raw": "Стекла для смартфона",
                "model_normalized": "smartphone_glass",
                "match_key": "clear|smartphone_glass",
                "internal_nm_id": 210183919,
                "internal_name": "Стекла для смартфона",
                "qty": 1,
                "unit_price": 785087.50,
                "amount": 785087.50,
                "currency": "CNY",
                "match_status": "matched",
                "manual_override": False,
                "raw": {},
            }
        ],
    )


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
