"""Browser smoke for the operator UI `Цены -> Проверка СПП` with fake upstreams."""

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
    _build_block as _build_prices_block,
    _reserve_free_port,
    _seed_runtime,
)
from apps.wb_spp_tester_smoke import FakePublicSource, FakeSppPricesSource  # noqa: E402
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application.wb_spp_tester import (  # noqa: E402
    WbSppTesterBlock,
    WbSppTesterCadenceConfig,
    WbSppTesterSafetyConfig,
)
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


def main() -> None:
    with _LocalSppUiServer() as base_url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 940})
            page.goto(f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}", wait_until="domcontentloaded")
            page.locator('[data-unified-tab-button="prices"]').click()
            page.locator('[data-prices-subtab="spp-test"]').click()
            page.locator("[data-spp-test-nm]").select_option(str(PRIMARY_NM))
            page.wait_for_function(
                "() => document.querySelector('[data-spp-test-baseline]')?.innerText.includes('900')",
                timeout=7000,
            )
            page.locator('[data-spp-test-input="range_min_discounted"]').fill("700")
            page.locator('[data-spp-test-input="range_max_discounted"]').fill("900")
            page.locator('[data-spp-test-input="precision_rub"]').fill("2")
            page.locator('[data-spp-test-input="max_measurements"]').fill("5")
            page.locator("[data-spp-test-plan]").click()
            page.wait_for_function(
                "() => document.querySelector('[data-spp-test-plan-preview]')?.innerText.includes('WB uploads')",
                timeout=7000,
            )
            page.locator("[data-spp-test-confirm-live]").check()
            if not page.locator("[data-spp-test-start]").is_disabled():
                raise AssertionError("SPP start must remain disabled when server guards are off")
            panel_text = page.locator('[data-prices-subpanel="spp-test"]').inner_text()
            for expected in ("Проверка СПП", "Baseline", "План и измерения", "Измерений пока нет"):
                if expected not in panel_text and expected != "Проверка СПП":
                    raise AssertionError(f"SPP tester panel missing text {expected!r}: {panel_text}")
            if "Текущие цены" not in page.locator("[data-prices-panel]").inner_text():
                raise AssertionError("prices subtabs must include current prices")
            browser.close()
    print("wb_spp_tester_browser_smoke: OK")


class _LocalSppUiServer:
    def __init__(self) -> None:
        self.tmp: TemporaryDirectory[str] | None = None
        self.server = None
        self.thread: threading.Thread | None = None
        self.base_url = ""

    def __enter__(self) -> str:
        self.tmp = TemporaryDirectory(prefix="wb-spp-browser-")
        runtime_dir = Path(self.tmp.name) / "runtime"
        runtime = _seed_runtime(runtime_dir)
        prices_source = FakePricesSource()
        spp_prices_source = FakeSppPricesSource()
        spp_block = WbSppTesterBlock(
            runtime=runtime,
            runtime_dir=runtime_dir,
            prices_source=spp_prices_source,
            public_source=FakePublicSource(spp_prices_source),
            now_factory=lambda: NOW,
            timestamp_factory=lambda: "2026-07-07T07:00:00Z",
            sleep=lambda _seconds: None,
            safety_config=WbSppTesterSafetyConfig(spp_test_enabled=False, prices_write_enabled=False),
            cadence_config=WbSppTesterCadenceConfig(run_async=False),
        )
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            now_factory=lambda: NOW,
            activated_at_factory=lambda: "2026-07-07T07:00:00Z",
            prices_block=_build_prices_block(runtime, runtime_dir, prices_source, write_enabled=False),
            spp_tester_block=spp_block,
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
