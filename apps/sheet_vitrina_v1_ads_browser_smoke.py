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
    NOW,
    _reserve_free_port,
    _seed_runtime,
)
from apps.sheet_vitrina_v1_ads_confirmed_bid_smoke import NoFrameSource, SAFETY, EXTERNAL_NM
from packages.application.sheet_vitrina_v1_ads import SheetVitrinaV1AdsBlock
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
        source = NoFrameSource()
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            now_factory=lambda: NOW,
            activated_at_factory=lambda: "2026-06-28T06:00:00Z",
            ads_block=SheetVitrinaV1AdsBlock(runtime=runtime, runtime_dir=runtime_dir, source=source, now_factory=lambda: NOW, safety_config=SAFETY),
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
                page.locator('[data-unified-tab-button="sku-management"]').click()
                page.locator('[data-sku-management-subtab="ads"]').click()
                page.locator('[data-ads-section="bids"]').click()
                page.locator(f'[data-ads-open-sku="{EXTERNAL_NM}"]').wait_for(timeout=7000)
                page.locator(f'[data-ads-open-sku="{EXTERNAL_NM}"]').click()
                page.locator('[data-ads-drawer]').wait_for(state="visible", timeout=7000)
                page.locator('[data-ads-bid-input="0"]').fill("1500")
                page.locator('[data-ads-preview-index="0"]').click()
                page.locator('[data-ads-modal]').wait_for(state="visible", timeout=7000)
                modal_text = page.locator("[data-ads-modal]").inner_text()
                if "advert_id" not in modal_text or "Изменить live ставку" not in modal_text:
                    raise AssertionError(f"ads preview modal content mismatch: {modal_text}")
                warnings = page.locator('[data-ads-modal] [data-ads-threshold-warnings]')
                if warnings.locator('li').count() != 3 or "точную ставку" not in warnings.inner_text():
                    raise AssertionError(f"all seller-threshold warnings must be visible: {modal_text}")
                if source.patch_payloads:
                    raise AssertionError("preview must not submit")
                commits = []
                page.on("request", lambda req: commits.append(req.post_data_json) if req.url.endswith('/ads/bid-change/commit') else None)
                page.get_by_role("button", name="Изменить live ставку", exact=True).click()
                page.wait_for_function("document.querySelector('[data-ads-modal]').innerText.includes('operation_id')", timeout=7000)
                if len(commits) != 1 or commits[0].get("confirm") is not True or len(source.patch_payloads) != 1 or source.bid != 150_000:
                    raise AssertionError(f"one existing confirmation must send one exact target: {commits} {source.patch_payloads}")
                if warnings.locator('li').count() != 3:
                    raise AssertionError("commit result must preserve visible threshold warnings")
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    print("sheet_vitrina_v1_ads_browser_smoke: OK")


if __name__ == "__main__":
    main()
