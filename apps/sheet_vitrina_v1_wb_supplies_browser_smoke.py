"""Browser smoke-check for the WB FBW supplies operator UI."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_wb_supplies_http_smoke import (  # noqa: E402
    FakeWbSuppliesSource,
    MissingTokenSource,
    _reserve_free_port,
)
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


def main() -> None:
    with TemporaryDirectory(prefix="wb-supplies-browser-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        port = _reserve_free_port()
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: "2026-06-08T08:00:00Z",
        )
        entrypoint.wb_supplies_block.source = MissingTokenSource()
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
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.goto(f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}", wait_until="domcontentloaded")
                page.locator("[data-unified-tab-button='factory-order']").click()
                operator_frame = page.frame_locator("iframe[title='Поставки']")
                expect(operator_frame.get_by_role("button", name="Расчёты")).to_be_visible()
                expect(operator_frame.get_by_role("button", name="Wildberries", exact=True)).to_be_visible()
                expect(operator_frame.get_by_role("button", name="От поставщика")).to_be_visible()
                operator_frame.get_by_role("button", name="Wildberries", exact=True).click()

                expect(operator_frame.locator("#wbSuppliesTitle")).to_contain_text("Все поставки")
                expect(operator_frame.get_by_text("Read-only список поставок WB API / FBW Supplies")).to_be_visible()
                expect(operator_frame.get_by_text("WB API / FBW Supplies · read-only")).to_be_visible()
                expect(operator_frame.locator("#wbSuppliesSearchInput")).to_have_attribute("placeholder", "Номер поставки")
                expect(operator_frame.locator("#wbSuppliesWarehouseSelect")).to_be_visible()
                expect(operator_frame.locator("#wbSuppliesStatusSelect")).to_be_visible()
                expect(operator_frame.locator("#wbSuppliesSizeFilterSelect")).to_be_visible()
                expect(operator_frame.locator("#wbSuppliesSizeFilterSelect")).to_have_value("main_250")
                expect(operator_frame.locator("#wbSuppliesPageSizeSelect")).to_have_value("20")
                expect(operator_frame.locator("#wbSuppliesBackfillButton")).to_be_visible()
                expect(operator_frame.locator("#wbSuppliesSupplyDateSortButton")).to_be_visible()
                page_size_options = operator_frame.locator("#wbSuppliesPageSizeSelect option").evaluate_all(
                    "(nodes) => nodes.map((node) => node.value)"
                )
                if page_size_options != ["20", "50", "100"]:
                    raise AssertionError(f"page-size options must be 20/50/100, got {page_size_options}")

                expected_columns = [
                    "Номер и тип",
                    "Дата поставки ↓",
                    "Склад",
                    "Статус",
                    "Добавлено, шт / Упаковано → Принято",
                    "Коэф. приёмки",
                    "Стоимость",
                ]
                actual_columns = operator_frame.locator(".wb-supplies-table thead th").evaluate_all(
                    "(nodes) => nodes.map((node) => node.textContent.trim())"
                )
                if actual_columns != expected_columns:
                    raise AssertionError(f"WB supplies columns changed: {actual_columns}")
                expect(operator_frame.locator("#wbSuppliesPageButtons")).to_be_visible()
                expect(operator_frame.locator("#wbSuppliesMessage")).to_contain_text("WB_API_TOKEN", timeout=10000)

                entrypoint.wb_supplies_block.source = FakeWbSuppliesSource()
                operator_frame.locator("#wbSuppliesRefreshButton").click()
                expect(operator_frame.locator("#wbSuppliesTableBody")).to_contain_text("39265492", timeout=10000)
                expect(operator_frame.locator("#wbSuppliesTableBody")).to_contain_text("Склад Шушары → Обухово")
                expect(operator_frame.locator("#wbSuppliesTableBody")).to_contain_text("11 543,52 ₽")
                expect(operator_frame.locator("#wbSuppliesTableBody")).to_contain_text("Короб")
                expect(operator_frame.locator("#wbSuppliesTableBody")).not_to_contain_text("boxTypeID 1")
                expect(operator_frame.locator("#wbSuppliesTableBody")).to_contain_text("1001")
                expect(operator_frame.locator("#wbSuppliesTableBody")).to_contain_text("1003")
                expect(operator_frame.locator("#wbSuppliesTableBody")).not_to_contain_text("1002")
                expect(operator_frame.locator("#wbSuppliesSummary")).to_contain_text("Скрыто размером: 2")
                expect(operator_frame.locator("#wbSuppliesSummary")).to_contain_text("Unknown qty: 1")

                const_first_row_before = operator_frame.locator("#wbSuppliesTableBody tr").first
                expect(const_first_row_before).to_contain_text("1003")
                operator_frame.locator("#wbSuppliesSupplyDateSortButton").click()
                expect(operator_frame.locator("#wbSuppliesSupplyDateSortArrow")).to_contain_text("↑", timeout=10000)
                expect(operator_frame.locator("#wbSuppliesTableBody tr").first).to_contain_text("39265492", timeout=10000)
                operator_frame.locator("#wbSuppliesSupplyDateSortButton").click()
                expect(operator_frame.locator("#wbSuppliesSupplyDateSortArrow")).to_contain_text("↓", timeout=10000)
                expect(operator_frame.locator("#wbSuppliesTableBody tr").first).to_contain_text("1003", timeout=10000)

                operator_frame.locator("#wbSuppliesSizeFilterSelect").select_option("all")
                expect(operator_frame.locator("#wbSuppliesTableBody")).to_contain_text("1002", timeout=10000)
                operator_frame.locator("#wbSuppliesSearchInput").fill("2002")
                expect(operator_frame.locator("#wbSuppliesTableBody")).to_contain_text("1002", timeout=10000)
                expect(operator_frame.locator("#wbSuppliesTableBody")).not_to_contain_text("1001")
                operator_frame.locator("#wbSuppliesSearchInput").fill("")
                expect(operator_frame.locator("#wbSuppliesTableBody")).to_contain_text("39265540", timeout=10000)
                operator_frame.locator("#wbSuppliesWarehouseSelect").select_option("777")
                expect(operator_frame.locator("#wbSuppliesTableBody")).to_contain_text("39265540", timeout=10000)
                expect(operator_frame.locator("#wbSuppliesTableBody")).to_contain_text("Электросталь")
                expect(operator_frame.locator("#wbSuppliesTableBody")).to_contain_text("9 250")
                expect(operator_frame.locator("#wbSuppliesTableBody")).to_contain_text("Упаковано 9 250 → Принято 9 237")
                operator_frame.locator("#wbSuppliesStatusSelect").select_option("2")
                expect(operator_frame.locator("#wbSuppliesTableBody")).to_contain_text("Запланировано", timeout=10000)
                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)

    print("sheet_vitrina_v1_wb_supplies_browser_smoke: OK")


if __name__ == "__main__":
    main()
