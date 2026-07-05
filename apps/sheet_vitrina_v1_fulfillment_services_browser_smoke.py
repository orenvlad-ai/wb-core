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
                fulfillment_tab = operator_frame.get_by_role("button", name="Услуги фулфилмента", exact=True)
                expect(fulfillment_tab).to_be_visible(timeout=10000)
                fulfillment_tab.click()
                expect(operator_frame.locator("#fulfillmentServicesTitle")).to_contain_text("Услуги фулфилмента")
                expect(operator_frame.locator(".fulfillment-services-block")).to_contain_text("Загруженные документы")
                if operator_frame.locator("#fulfillmentLatestBlock").count() != 0:
                    raise AssertionError("Fulfillment UI must not render the old latest-upload block")
                if operator_frame.locator("#fulfillmentServicesSummary").count() != 0:
                    raise AssertionError("Fulfillment UI must not render technical summary chips")
                expect(operator_frame.locator("#fulfillmentUploadsBody")).to_contain_text("Загруженных документов пока нет")

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
                    "Документ принят. Все строки смэтчены, PDF-виза сформирована.",
                    timeout=10000,
                )
                fulfillment_section_text = _normalize_ui_text(operator_frame.locator(".fulfillment-services-block").inner_text())
                for expected in ("Дата загрузки", "fulfillment-ok", "2/2", "3 000 ₽", "150 ₽", "3 150 ₽", "Скачать PDF-визу", "Удалить"):
                    if expected not in fulfillment_section_text:
                        raise AssertionError(f"accepted table must expose {expected!r}: {fulfillment_section_text}")
                for forbidden in ("Последняя загрузка", "upload_id:", "hash:", "status:", "payment_validation_id:"):
                    if forbidden in fulfillment_section_text:
                        raise AssertionError(f"Fulfillment main screen must not expose technical token {forbidden!r}: {fulfillment_section_text}")

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
                if "Стоимость" in actual_columns or "Транзит" not in actual_columns or "Услуги фулфилмента" not in actual_columns:
                    raise AssertionError(f"WB supplies columns must expose new overlay contract, got {actual_columns}")
                row1001_text = _normalize_ui_text(
                    operator_frame.locator("#wbSuppliesTableBody tr", has_text="1001").inner_text()
                )
                for expected in ("200 ₽", "20 ₽/шт", "1 575 ₽", "157,50 ₽/шт"):
                    if expected not in row1001_text:
                        raise AssertionError(f"WB row 1001 must show transit/fulfillment amount and per-unit {expected!r}: {row1001_text}")
                if "Seller Portal" in row1001_text:
                    raise AssertionError(f"transit cell must show per-unit instead of Seller Portal source label: {row1001_text}")

                fulfillment_tab.click()
                operator_frame.locator("#fulfillmentUploadsBody [data-fulfillment-delete]").first.click()
                expect(operator_frame.locator("#fulfillmentUploadsBody")).to_contain_text(
                    "Удалить документ? Данные услуг фулфилмента по связанным WB-поставкам будут удалены из системы.",
                    timeout=10000,
                )
                operator_frame.locator("#fulfillmentUploadsBody [data-fulfillment-delete-cancel]").first.click()
                expect(operator_frame.locator("#fulfillmentUploadsBody")).to_contain_text("fulfillment-ok.xlsx")
                if operator_frame.locator("#fulfillmentUploadsBody [data-fulfillment-delete-confirm]").count() != 0:
                    raise AssertionError("cancel must close Fulfillment delete confirmation")
                operator_frame.locator("#fulfillmentUploadsBody [data-fulfillment-delete]").first.click()
                operator_frame.locator("#fulfillmentUploadsBody [data-fulfillment-delete-confirm]").first.click()
                expect(operator_frame.locator("#fulfillmentUploadsBody")).to_contain_text("Загруженных документов пока нет", timeout=10000)
                if "fulfillment-ok.xlsx" in operator_frame.locator("#fulfillmentUploadsBody").inner_text():
                    raise AssertionError("deleted accepted document must disappear from accepted table")
                pdf_after_delete_status, _, _ = _get_bytes(
                    f"{base_url}{DEFAULT_FULFILLMENT_SERVICES_UPLOADS_PATH}/{upload_id}/payment-validation.pdf"
                )
                if pdf_after_delete_status != 404:
                    raise AssertionError(f"deleted upload PDF must be unavailable, got HTTP {pdf_after_delete_status}")
                operator_frame.get_by_role("button", name="Wildberries", exact=True).click()
                operator_frame.locator("#wbSuppliesSizeFilterSelect").select_option("all")
                expect(operator_frame.locator("#wbSuppliesTableBody")).to_contain_text("1001", timeout=10000)
                row1001_after_delete = _normalize_ui_text(
                    operator_frame.locator("#wbSuppliesTableBody tr", has_text="1001").inner_text()
                )
                if "1 575 ₽" in row1001_after_delete or "157,50 ₽/шт" in row1001_after_delete:
                    raise AssertionError(f"deleted upload must disappear from WB overlay: {row1001_after_delete}")
                if "200 ₽" not in row1001_after_delete or "20 ₽/шт" not in row1001_after_delete:
                    raise AssertionError(f"transit amount/per-unit must remain after Fulfillment delete: {row1001_after_delete}")

                fulfillment_tab.click()
                unmatched_xlsx_path = Path(tmp) / "fulfillment-unmatched.xlsx"
                _fill_downloaded_template(template_path, unmatched_xlsx_path, [_valid_row("9999")])
                operator_frame.locator("#fulfillmentFileInput").set_input_files(str(unmatched_xlsx_path))
                expect(operator_frame.locator("#fulfillmentServicesMessage")).to_contain_text(
                    "Документ не принят. Исправьте ошибки и загрузите файл заново.",
                    timeout=10000,
                )
                expect(operator_frame.locator("#fulfillmentErrorsBody")).to_contain_text("9999")
                expect(operator_frame.locator("#fulfillmentErrorsBody")).to_contain_text("не найдена в WB-поставках")
                if "fulfillment-unmatched.xlsx" in operator_frame.locator("#fulfillmentUploadsBody").inner_text():
                    raise AssertionError("failed unmatched upload must not enter accepted table")

                duplicate_xlsx_path = Path(tmp) / "fulfillment-duplicate.xlsx"
                _fill_downloaded_template(template_path, duplicate_xlsx_path, [_valid_row("1001"), _valid_row("1001")])
                operator_frame.locator("#fulfillmentFileInput").set_input_files(str(duplicate_xlsx_path))
                expect(operator_frame.locator("#fulfillmentServicesMessage")).to_contain_text(
                    "Документ не принят. Исправьте ошибки и загрузите файл заново.",
                    timeout=10000,
                )
                expect(operator_frame.locator("#fulfillmentErrorsBody")).to_contain_text("дублируется номер поставки 1001")
                if "fulfillment-duplicate.xlsx" in operator_frame.locator("#fulfillmentUploadsBody").inner_text():
                    raise AssertionError("failed duplicate upload must not enter accepted table")

                wb_status, wb_payload = _get_json(f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}?search=1001&size_filter=all")
                wb_row = (wb_payload.get("rows") or [{}])[0]
                if wb_status != 200 or wb_row.get("fulfillment_amount_with_vat_total") is not None:
                    raise AssertionError(f"failed/deleted uploads must not alter approved WB overlay, got {wb_status} {wb_payload}")

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
