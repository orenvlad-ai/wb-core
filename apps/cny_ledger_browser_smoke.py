"""Browser smoke for CNY account conversion upload, guarded delete, and replay UI."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.cny_ledger_smoke import (  # noqa: E402
    HTTP_NOW,
    _fixture_text_extractor,
    _get_json,
    _reserve_free_port,
    _seed_supplier_order,
)
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_CNY_ACCOUNT_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.cny_ledger import CnyLedgerBlock  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.contracts.cny_ledger import CNY_DOCUMENT_SOURCE_SUPPLIER_ORDER  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


class ObservedCnyEntrypoint(RegistryUploadHttpEntrypoint):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.cny_status_request_count = 0
        self.cny_delete_document_ids: list[str] = []
        self.slow_delete_document_id = ""
        self.slow_delete_started = threading.Event()
        self.slow_delete_release = threading.Event()

    def handle_cny_account_status_request(self) -> dict[str, object]:
        self.cny_status_request_count += 1
        return super().handle_cny_account_status_request()

    def handle_cny_account_document_delete_request(self, document_id: str) -> dict[str, object]:
        self.cny_delete_document_ids.append(document_id)
        if document_id == self.slow_delete_document_id:
            self.slow_delete_started.set()
            if not self.slow_delete_release.wait(timeout=5):
                raise RuntimeError("browser smoke did not release bounded CNY delete")
        return super().handle_cny_account_document_delete_request(document_id)


def main() -> None:
    with TemporaryDirectory(prefix="cny-ledger-browser-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        _seed_supplier_order(runtime, "browser-source-order")
        ledger = CnyLedgerBlock(runtime=runtime, timestamp_factory=_clock())
        ledger.create_opening_balance(
            {"operation_date": "2026-05-01", "cny_amount": "100", "rub_value": "1000"}
        )

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
        entrypoint = ObservedCnyEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: "2026-05-12T08:00:00Z",
            now_factory=lambda: HTTP_NOW,
        )
        entrypoint.cny_ledger_block.timestamp_factory = _clock()
        entrypoint.cny_ledger_block.pdf_text_extractor = _fixture_text_extractor
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{config.port}"
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.goto(f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}", wait_until="domcontentloaded")
                operator_frame = _open_cny_account(page)

                headers = operator_frame.locator("#cnyConversionsBody").locator(
                    "xpath=ancestor::table[1]//thead//th"
                ).evaluate_all("(nodes) => nodes.map((node) => node.textContent.trim())")
                if len(headers) != 11 or headers[-1] != "Действия":
                    raise AssertionError(f"CNY conversion table action column changed: {headers}")
                empty_cell = operator_frame.locator("#cnyConversionsBody .cny-account-empty")
                expect(empty_cell).to_have_attribute("colspan", "11")
                expect(empty_cell).to_contain_text("Загрузите документы конвертации")

                _upload_pdf(operator_frame, "browser-direct-1.pdf", b"browser-direct-conversion-1")
                _upload_pdf(operator_frame, "browser-direct-2.pdf", b"browser-direct-conversion-2")
                direct_one_row = operator_frame.locator(
                    "#cnyConversionsBody tr", has_text="browser-direct-1.pdf"
                )
                direct_two_row = operator_frame.locator(
                    "#cnyConversionsBody tr", has_text="browser-direct-2.pdf"
                )
                expect(direct_one_row).to_be_visible()
                expect(direct_two_row).to_be_visible()
                expect(direct_one_row.locator("[data-cny-delete-document]")).to_be_enabled()

                source_relative_path = Path(
                    "supplier_financial_documents/files/browser-source-financial/browser-source-owned.pdf"
                )
                source_file = runtime_dir / source_relative_path
                source_file.parent.mkdir(parents=True, exist_ok=True)
                source_file.write_bytes(b"browser-source-owned-conversion")
                source_owned = entrypoint.cny_ledger_block.upload_document(
                    file_bytes=b"browser-source-owned-conversion",
                    uploaded_filename="browser-source-owned.pdf",
                    uploaded_content_type="application/pdf",
                    source=CNY_DOCUMENT_SOURCE_SUPPLIER_ORDER,
                    source_order_id="browser-source-order",
                    context_order_id="browser-source-order",
                    stored_file_path=str(source_relative_path),
                    linked_financial_document_id="browser-source-financial",
                )
                operator_frame.locator("#cnyAccountReplayButton").click()
                expect(operator_frame.locator("#cnyAccountMessage")).to_contain_text(
                    "CNY ledger пересчитан", timeout=10000
                )
                source_row = operator_frame.locator(
                    "#cnyConversionsBody tr", has_text="browser-source-owned.pdf"
                )
                expect(source_row).to_be_visible()
                expect(source_row.locator(".cny-delete-button")).to_be_disabled()
                expect(source_row.locator(".cny-delete-hint")).to_contain_text("Через карточку заказа")
                if source_row.locator("[data-cny-delete-document]").count() != 0:
                    raise AssertionError("source-owned conversion must not expose an active direct-delete control")

                ui_before_error = _cny_ui_snapshot(operator_frame)
                error_button = direct_two_row.locator("[data-cny-delete-document]")
                error_button.evaluate(
                    "(button) => button.setAttribute('data-cny-delete-document', 'missing-browser-document')"
                )
                error_dialogs: list[str] = []
                page.once("dialog", lambda dialog: _accept_dialog(dialog, error_dialogs))
                error_button.click()
                expect(operator_frame.locator("#cnyAccountMessage")).to_contain_text(
                    "CNY document not found: missing-browser-document", timeout=10000
                )
                expect(direct_two_row).to_be_visible()
                expect(error_button).to_be_enabled()
                expect(error_button).to_have_text("Удалить")
                if _cny_ui_snapshot(operator_frame) != ui_before_error:
                    raise AssertionError("failed delete must preserve CNY balances, replay state, and ledger rows")
                if not error_dialogs or "browser-direct-2.pdf" not in error_dialogs[0]:
                    raise AssertionError(f"delete confirmation must name the target PDF: {error_dialogs}")

                direct_one_button = direct_one_row.locator("[data-cny-delete-document]")
                direct_one_id = str(direct_one_button.get_attribute("data-cny-delete-document") or "")
                entrypoint.slow_delete_document_id = direct_one_id
                delete_count_before = len(entrypoint.cny_delete_document_ids)
                status_count_before = entrypoint.cny_status_request_count
                success_dialogs: list[str] = []
                page.once("dialog", lambda dialog: _accept_dialog(dialog, success_dialogs))
                direct_one_button.evaluate("(button) => { button.click(); button.click(); }")
                expect(direct_one_button).to_be_disabled()
                expect(direct_one_button).to_have_text("Удаляем...")
                if not entrypoint.slow_delete_started.wait(timeout=3):
                    raise AssertionError("browser DELETE request did not reach the CNY account route")
                if len(entrypoint.cny_delete_document_ids) != delete_count_before + 1:
                    raise AssertionError(
                        f"double click must create one DELETE request: {entrypoint.cny_delete_document_ids}"
                    )
                with page.expect_request(
                    lambda request: "/sheet-vitrina-v1/supplier?embedded=operator" in request.url,
                    timeout=10000,
                ):
                    entrypoint.slow_delete_release.set()
                    expect(operator_frame.locator("#cnyAccountMessage")).to_contain_text(
                        "CNY ledger и связанные расчётные показатели пересчитаны", timeout=10000
                    )
                expect(direct_one_row).to_have_count(0)
                ui_after_success = _cny_ui_snapshot(operator_frame)
                for field in ("balance_cny", "balance_rub", "average_rate", "replay", "state"):
                    if ui_after_success[field] == ui_before_error[field]:
                        raise AssertionError(
                            f"successful delete must refresh CNY UI field {field}: "
                            f"{ui_before_error} -> {ui_after_success}"
                        )
                if ui_after_success["ledger_row_count"] != ui_before_error["ledger_row_count"] - 1:
                    raise AssertionError(
                        f"successful delete must remove its replayed ledger operation: "
                        f"{ui_before_error} -> {ui_after_success}"
                    )
                if entrypoint.cny_status_request_count <= status_count_before:
                    raise AssertionError("successful browser delete must reload the CNY account read model from the server")
                expected_warning = (
                    "Документ будет удалён. Остаток CNY, рублёвая стоимость остатка, "
                    "средний курс и последующие операции ledger будут пересчитаны"
                )
                if (
                    not success_dialogs
                    or "browser-direct-1.pdf" not in success_dialogs[0]
                    or expected_warning not in success_dialogs[0]
                ):
                    raise AssertionError(f"delete confirmation warning changed: {success_dialogs}")

                status_code, status_payload = _get_json(f"{base_url}{DEFAULT_CNY_ACCOUNT_PATH}")
                if status_code != 200:
                    raise AssertionError(f"CNY read model failed after browser delete: {status_code} {status_payload}")
                if any(
                    str(item.get("document_id") or "") == direct_one_id
                    for item in status_payload.get("documents") or []
                ):
                    raise AssertionError(f"browser-deleted canonical document remained after GET: {status_payload}")
                if any(
                    str(item.get("source_document_id") or "") == direct_one_id
                    for item in status_payload.get("ledger_operations") or []
                ):
                    raise AssertionError(f"browser-deleted document operations remained after replay: {status_payload}")
                if runtime.load_cny_document(direct_one_id) is not None:
                    raise AssertionError("browser delete removed only the row, not the canonical CNY document")
                if runtime.load_cny_document(str(source_owned.get("document_id") or "")) is None or not source_file.is_file():
                    raise AssertionError("direct delete must not affect the source-owned CNY document or its file")

                operator_frame.locator("#cnyAccountReplayButton").click()
                expect(operator_frame.locator("#cnyAccountMessage")).to_contain_text(
                    "CNY ledger пересчитан", timeout=10000
                )
                page.reload(wait_until="domcontentloaded")
                operator_frame = _open_cny_account(page)
                expect(
                    operator_frame.locator("#cnyConversionsBody tr", has_text="browser-direct-1.pdf")
                ).to_have_count(0)
                expect(
                    operator_frame.locator("#cnyConversionsBody tr", has_text="browser-direct-2.pdf")
                ).to_be_visible()
                expect(
                    operator_frame.locator("#cnyConversionsBody tr", has_text="browser-source-owned.pdf")
                ).to_be_visible()
                browser.close()
        finally:
            entrypoint.slow_delete_release.set()
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


def _open_cny_account(page: object) -> object:
    page.locator("[data-unified-tab-button='factory-order']").click()
    operator_frame = page.frame_locator("iframe[title='Поставки']")
    supplier_tab = operator_frame.get_by_role("button", name="От поставщика", exact=True)
    expect(supplier_tab).to_be_visible(timeout=10000)
    supplier_tab.click()
    expect(operator_frame.locator("#supplierShipmentsFrame")).to_have_attribute(
        "src", "/sheet-vitrina-v1/supplier?embedded=operator"
    )
    cny_tab = operator_frame.get_by_role("button", name="Счёт CNY", exact=True)
    expect(cny_tab).to_be_visible(timeout=10000)
    cny_tab.click()
    expect(operator_frame.locator("#cnyAccountTitle")).to_have_text("Счёт CNY")
    return operator_frame


def _upload_pdf(operator_frame: object, filename: str, body: bytes) -> None:
    operator_frame.locator("#cnyAccountFileInput").set_input_files(
        {"name": filename, "mimeType": "application/pdf", "buffer": body}
    )
    expect(operator_frame.locator("#cnyAccountMessage")).to_contain_text(
        "Документ CNY сохранён и ledger пересчитан", timeout=10000
    )
    expect(operator_frame.locator("#cnyConversionsBody tr", has_text=filename)).to_be_visible()


def _cny_ui_snapshot(operator_frame: object) -> dict[str, object]:
    return {
        "balance_cny": operator_frame.locator("#cnyBalanceCny").inner_text(),
        "balance_rub": operator_frame.locator("#cnyBalanceRub").inner_text(),
        "average_rate": operator_frame.locator("#cnyAverageRate").inner_text(),
        "replay": operator_frame.locator("#cnyReplayStatus").inner_text(),
        "state": operator_frame.locator("#cnyAccountState").inner_text(),
        "ledger_row_count": operator_frame.locator("#cnyLedgerBody tr").count(),
    }


def _accept_dialog(dialog: object, messages: list[str]) -> None:
    messages.append(str(dialog.message))
    dialog.accept()


def _clock():
    counter = {"value": 0}

    def now() -> str:
        counter["value"] += 1
        return f"2026-05-01T09:{counter['value']:02d}:00Z"

    return now


if __name__ == "__main__":
    main()
