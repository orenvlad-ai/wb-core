"""Browser smoke-check for the SKU-first ads operator UI."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_ads_smoke import (  # noqa: E402
    FakePromotionSource,
    NOW,
    PRIMARY_NM,
    _build_ads_block,
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
    with TemporaryDirectory(prefix="sheet-vitrina-ads-browser-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = _seed_runtime(runtime_dir)
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            now_factory=lambda: NOW,
            activated_at_factory=lambda: "2026-06-28T06:00:00Z",
            ads_block=_build_ads_block(runtime, runtime_dir, FakePromotionSource(), write_enabled=False),
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
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{config.port}"
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 920})
                page.goto(f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}", wait_until="domcontentloaded")
                page.locator('[data-unified-tab-button="ads"]').click()
                page.locator(f'[data-ads-open-sku="{PRIMARY_NM}"]').wait_for(timeout=7000)
                page.locator(f'[data-ads-open-sku="{PRIMARY_NM}"]').click()
                page.locator('[data-ads-drawer]').wait_for(state="visible", timeout=7000)
                page.locator('[data-ads-bid-input="0"]').fill("16.00")
                page.locator('[data-ads-preview-index="0"]').click()
                page.locator('[data-ads-modal]').wait_for(state="visible", timeout=7000)
                modal_text = page.locator("[data-ads-modal]").inner_text()
                if "advert_id" not in modal_text or "Изменить live ставку" not in modal_text:
                    raise AssertionError(f"ads preview modal content mismatch: {modal_text}")
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    print("sheet_vitrina_v1_ads_browser_smoke: OK")


if __name__ == "__main__":
    main()
