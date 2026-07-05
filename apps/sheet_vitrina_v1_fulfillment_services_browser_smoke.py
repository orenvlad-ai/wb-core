"""Browser live-flow smoke for Fulfillment services operator UI."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading

from openpyxl import load_workbook
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_fulfillment_services_smoke import (  # noqa: E402
    NOW,
    _get_bytes,
    _get_json,
    _pdf_text,
    _reserve_free_port,
    _seed_wb_supplies,
    _valid_row,
)
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_FULFILLMENT_SERVICES_UPLOADS_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_UPLOAD_PATH,
    DEFAULT_WB_SUPPLIES_PATH,
    build_registry_upload_http_server,
)
from packages.application.fulfillment_services import TEMPLATE_HEADERS  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import DB_FILENAME, RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


def main() -> None:
    with TemporaryDirectory(prefix="fulfillment-services-browser-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        _seed_wb_supplies(runtime)
        port = _reserve_free_port()
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: NOW,
        )
        cfg = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=port,
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path="/v1/sheet-vitrina-v1/refresh",
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime_dir,
        )
        server = build_registry_upload_http_server(cfg, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{cfg.port}"
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
                page.goto(f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}", wait_until="domcontentloaded")
                page.locator("[data-unified-tab-button='factory-order']").click()
                operator_frame = page.frame_locator("iframe[title='Поставки']")
                expect(operator_frame.get_by_role("button", name="Fulfillment", exact=True)).to_be_visible(timeout=10000)
                operator_frame.get_by_role("button", name="Fulfillment", exact=True).click()
                expect(operator_frame.locator("#fulfillmentServicesTitle")).to_contain_text("Fulfillment")

                template_path = Path(tmp) / "downloaded-fulfillment-template.xlsx"
                with page.expect_download() as download_info:
                    operator_frame.locator("#fulfillmentTemplateButton").click()
                download = download_info.value
                if not download.suggested_filename.endswith(".xlsx"):
                    raise AssertionError(f"template download must be xlsx, got {download.suggested_filename!r}")
                download.save_as(str(template_path))

                ok_xlsx_path = Path(tmp) / "fulfillment-ok.xlsx"
                _fill_downloaded_template(template_path, ok_xlsx_path, [_valid_row("1001"), _valid_row("1002")])
                operator_frame.locator("#fulfillmentFileInput").set_input_files(str(ok_xlsx_path))
                expect(operator_frame.locator("#fulfillmentServicesMessage")).to_contain_text(
                    "Fulfillment upload OK, PDF-виза доступна.",
                    timeout=10000,
                )
                expect(operator_frame.locator("#fulfillmentServicesSummary")).to_contain_text("Status: ok")
                expect(operator_frame.locator("#fulfillmentServicesSummary")).to_contain_text("Rows: 2/2")
                expect(operator_frame.locator("#fulfillmentServicesSummary")).to_contain_text("К оплате: 3 150 ₽")
                expect(operator_frame.locator("#fulfillmentLatestSummary")).to_contain_text("payment_validation_id:")
                expect(operator_frame.locator("#fulfillmentLatestSummary")).to_contain_text("Скачать PDF-визу")
                expect(operator_frame.locator("#fulfillmentErrorsBody")).to_contain_text("Ошибок нет.")

                list_status, list_payload = _get_json(f"{base_url}{DEFAULT_FULFILLMENT_SERVICES_UPLOADS_PATH}")
                latest_upload = (list_payload.get("uploads") or [{}])[0]
                upload_id = str(latest_upload.get("upload_id") or "")
                if list_status != 200 or latest_upload.get("validation_status") != "ok" or not upload_id:
                    raise AssertionError(f"valid upload must be latest OK upload, got {list_status} {list_payload}")
                pdf_status, pdf_body, _ = _get_bytes(
                    f"{base_url}{DEFAULT_FULFILLMENT_SERVICES_UPLOADS_PATH}/{upload_id}/payment-validation.pdf"
                )
                pdf_text = _pdf_text(pdf_body)
                for expected in (
                    "Виза на оплату Fulfillment-услуг",
                    upload_id,
                    str(latest_upload.get("payment_validation_id") or ""),
                    str(latest_upload.get("short_file_hash") or ""),
                    "3 150",
                    "1001",
                    "1002",
                ):
                    if pdf_status != 200 or expected not in pdf_text:
                        raise AssertionError(f"PDF validation text missing {expected!r}: {pdf_text!r}")

                operator_frame.get_by_role("button", name="Wildberries", exact=True).click()
                expect(operator_frame.locator("#wbSuppliesTitle")).to_contain_text("Все поставки", timeout=10000)
                operator_frame.locator("#wbSuppliesSizeFilterSelect").select_option("all")
                expect(operator_frame.locator("#wbSuppliesTableBody")).to_contain_text("1001", timeout=10000)
                actual_columns = operator_frame.locator("#wbSuppliesTableBody").locator("xpath=ancestor::table[1]//thead//th").evaluate_all(
                    "(nodes) => nodes.map((node) => node.textContent.trim())"
                )
                if "Стоимость" in actual_columns or "Транзит" not in actual_columns or "Услуги fulfillment" not in actual_columns:
                    raise AssertionError(f"WB supplies columns must expose new overlay contract, got {actual_columns}")
                row1001_text = _normalize_ui_text(
                    operator_frame.locator("#wbSuppliesTableBody tr", has_text="1001").inner_text()
                )
                for expected in ("200 ₽", "20 ₽/шт", "1 575 ₽", "157,50 ₽/шт"):
                    if expected not in row1001_text:
                        raise AssertionError(f"WB row 1001 must show transit/fulfillment amount and per-unit {expected!r}: {row1001_text}")
                if "Seller Portal" in row1001_text:
                    raise AssertionError(f"transit cell must show per-unit instead of Seller Portal source label: {row1001_text}")

                operator_frame.get_by_role("button", name="Fulfillment", exact=True).click()
                unmatched_xlsx_path = Path(tmp) / "fulfillment-unmatched.xlsx"
                _fill_downloaded_template(template_path, unmatched_xlsx_path, [_valid_row("9999")])
                operator_frame.locator("#fulfillmentFileInput").set_input_files(str(unmatched_xlsx_path))
                expect(operator_frame.locator("#fulfillmentServicesMessage")).to_contain_text("Fulfillment upload failed", timeout=10000)
                expect(operator_frame.locator("#fulfillmentServicesSummary")).to_contain_text("Status: failed")
                expect(operator_frame.locator("#fulfillmentErrorsBody")).to_contain_text("9999")
                expect(operator_frame.locator("#fulfillmentErrorsBody")).to_contain_text("does not match cached WB supply")
                if operator_frame.locator("#fulfillmentLatestSummary a").count() != 0:
                    raise AssertionError("failed unmatched upload must not expose latest PDF link")

                duplicate_xlsx_path = Path(tmp) / "fulfillment-duplicate.xlsx"
                _fill_downloaded_template(template_path, duplicate_xlsx_path, [_valid_row("1001"), _valid_row("1001")])
                operator_frame.locator("#fulfillmentFileInput").set_input_files(str(duplicate_xlsx_path))
                expect(operator_frame.locator("#fulfillmentServicesMessage")).to_contain_text("Fulfillment upload failed", timeout=10000)
                expect(operator_frame.locator("#fulfillmentServicesSummary")).to_contain_text("Status: failed")
                expect(operator_frame.locator("#fulfillmentErrorsBody")).to_contain_text("Duplicate")
                if operator_frame.locator("#fulfillmentLatestSummary a").count() != 0:
                    raise AssertionError("failed duplicate upload must not expose latest PDF link")

                wb_status, wb_payload = _get_json(f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}?search=1001&size_filter=all")
                wb_row = (wb_payload.get("rows") or [{}])[0]
                if wb_status != 200 or wb_row.get("fulfillment_amount_with_vat_total") != 1575.0:
                    raise AssertionError(f"failed uploads must not alter approved WB overlay, got {wb_status} {wb_payload}")
                if "₽/шт" not in str(wb_row.get("fulfillment_per_unit_display") or ""):
                    raise AssertionError(f"approved fulfillment overlay must keep per-unit display, got {wb_row}")

                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        print(f"runtime_db: ok -> {runtime_dir / DB_FILENAME}")

    print("sheet_vitrina_v1_fulfillment_services_browser_smoke: OK")


def _fill_downloaded_template(template_path: Path, output_path: Path, rows: list[list[object]]) -> None:
    workbook = load_workbook(template_path)
    sheet = workbook.worksheets[0]
    headers = ["" if cell.value is None else str(cell.value) for cell in sheet[1]]
    if headers != TEMPLATE_HEADERS:
        raise AssertionError(f"downloaded template headers mismatch: {headers}")
    for row in rows:
        sheet.append(row)
    if rows:
        total = sum(float(row[8]) for row in rows)
        vat = sum(float(row[9]) for row in rows)
        sheet.append(["", "", "", "", "", "", "", "", total, vat])
    workbook.save(output_path)


def _normalize_ui_text(value: str) -> str:
    return " ".join(str(value or "").replace("\u00a0", " ").replace("\u202f", " ").split())


if __name__ == "__main__":
    main()
