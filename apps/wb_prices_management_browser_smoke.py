"""Browser smoke-check for the WB prices operator UI with fake WB upstream."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.wb_prices_management_smoke import (  # noqa: E402
    FakePricesSource,
    NOW,
    PRIMARY_NM,
    SIZE_PRICE_NM,
    _build_block,
    _reserve_free_port,
    _seed_runtime,
)
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


def main() -> None:
    with _LocalPricesServer(write_enabled=False) as base_url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 920})
            page.goto(f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}", wait_until="domcontentloaded")
            page.locator('[data-unified-tab-button="prices"]').click()
            page.locator(f'[data-prices-row="{PRIMARY_NM}"]').wait_for(timeout=7000)
            for removed_selector in (
                "[data-prices-filter-errors]",
                "[data-prices-filter-size]",
                "[data-prices-filter-quarantine]",
            ):
                if page.locator(removed_selector).count():
                    raise AssertionError(f"removed prices filter is still present: {removed_selector}")
            table_text = page.locator("[data-prices-table]").inner_text()
            if "СПП" not in table_text or "29%" not in table_text:
                raise AssertionError(f"SPP column must render by default, got: {table_text}")
            if "Акции" not in table_text or "2 / 4" not in table_text:
                raise AssertionError(f"promo column must render by default, got: {table_text}")
            page.locator("[data-prices-column-manager] summary").click()
            if not page.locator("[data-prices-column-controls]").is_visible():
                raise AssertionError("prices column menu must open")
            page.locator('[data-prices-column-id="spp"]').uncheck()
            table_text = page.locator("[data-prices-table]").inner_text()
            if "СПП" in table_text or "29%" in table_text:
                raise AssertionError(f"SPP column must hide, got: {table_text}")
            page.locator('[data-prices-column-id="spp"]').check()
            guard_text = page.locator("[data-prices-guard]").inner_text()
            if "выключено" not in guard_text:
                raise AssertionError(f"write-disabled guard text mismatch: {guard_text}")
            page.locator(f'[data-prices-edit-nm="{PRIMARY_NM}"][data-prices-edit-field="price"]').fill("200")
            page.locator('[data-prices-column-id="promo"]').uncheck()
            edited_value = page.locator(f'[data-prices-edit-nm="{PRIMARY_NM}"][data-prices-edit-field="price"]').input_value()
            if edited_value != "200":
                raise AssertionError(f"draft price edit must survive column toggle, got {edited_value!r}")
            page.locator('[data-prices-column-id="promo"]').check()
            page.locator("[data-prices-column-manager] summary").click()
            page.locator("[data-prices-preview]").click()
            page.locator("[data-prices-modal]").wait_for(state="visible", timeout=7000)
            modal_text = page.locator("[data-prices-modal]").inner_text()
            if "Live write выключен" not in modal_text or "quarantine_risk" not in modal_text:
                raise AssertionError(f"disabled preview modal mismatch: {modal_text}")
            if not page.locator("[data-prices-commit]").is_disabled():
                raise AssertionError("commit button must be disabled when WB_PRICES_WRITE_ENABLED is off")
            browser.close()

    with _LocalPricesServer(write_enabled=True) as base_url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 920})
            page.goto(f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}", wait_until="domcontentloaded")
            page.locator('[data-unified-tab-button="prices"]').click()
            page.locator(f'[data-prices-row="{PRIMARY_NM}"]').wait_for(timeout=7000)
            page.locator(f'[data-prices-edit-nm="{SIZE_PRICE_NM}"][data-prices-edit-field="discount"]').fill("25")
            page.locator("[data-prices-preview]").click()
            page.locator("[data-prices-modal]").wait_for(state="visible", timeout=7000)
            page.locator("[data-prices-commit]").click()
            page.locator("[data-prices-modal]").wait_for(state="visible", timeout=7000)
            page.wait_for_function(
                "() => document.body.innerText.includes('часть с ошибкой') || document.body.innerText.includes('price is blocked')",
                timeout=12000,
            )
            table_text = page.locator("[data-prices-table]").inner_text()
            if "price is blocked by size-based pricing" not in table_text:
                raise AssertionError(f"row-level upload error must be visible, got: {table_text}")
            browser.close()

    print("wb_prices_management_browser_smoke: OK")


class _LocalPricesServer:
    def __init__(self, *, write_enabled: bool) -> None:
        self.write_enabled = write_enabled
        self.tmp: TemporaryDirectory[str] | None = None
        self.server = None
        self.thread: threading.Thread | None = None
        self.base_url = ""

    def __enter__(self) -> str:
        self.tmp = TemporaryDirectory(prefix="wb-prices-browser-")
        runtime_dir = Path(self.tmp.name) / "runtime"
        runtime = _seed_runtime(runtime_dir)
        source = FakePricesSource()
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            now_factory=lambda: NOW,
            activated_at_factory=lambda: "2026-07-07T07:00:00Z",
            prices_block=_build_block(runtime, runtime_dir, source, write_enabled=self.write_enabled),
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
        self.server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{config.port}"
        return self.base_url

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)
        if self.tmp is not None:
            self.tmp.cleanup()


if __name__ == "__main__":
    main()
