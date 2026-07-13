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
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            with _LocalSppUiServer(spp_test_enabled=True, prices_write_enabled=True) as server:
                base_url = server.base_url
                page = browser.new_page(viewport={"width": 1440, "height": 940})
                _prepare_spp_page(page, base_url)
                if page.locator("[data-spp-test-start]").is_disabled():
                    raise AssertionError("SPP start must become enabled after successful plan and confirmations")
                if page.locator("[data-spp-test-start-reason]").inner_text().strip():
                    raise AssertionError("enabled SPP start must not show disabled reason")
                upload_count = len(server.spp_prices_source.upload_payloads)
                page.locator("[data-spp-schedule-nm]").select_option(str(PRIMARY_NM))
                page.locator('[data-spp-schedule-input="range_min_discounted"]').fill("700")
                page.locator('[data-spp-schedule-input="range_max_discounted"]').fill("900")
                page.locator('[data-spp-schedule-input="precision_rub"]').fill("2")
                page.locator('[data-spp-schedule-input="max_measurements"]').fill("3")
                page.locator("[data-spp-schedule-time]").fill("12:05")
                page.locator("[data-spp-schedule-enabled]").check()
                page.locator("[data-spp-schedule-consent]").check()
                page.locator("[data-spp-schedule-save]").click()
                page.wait_for_function(
                    "() => document.querySelector('[data-spp-schedule-note]')?.innerText.includes('ждёт назначенного времени')",
                    timeout=7000,
                )
                if len(server.spp_prices_source.upload_payloads) != upload_count:
                    raise AssertionError("saving the UI schedule must not immediately start an SPP job")
                page.wait_for_selector("[data-spp-history-job]", timeout=7000)
                page.locator("[data-spp-history-job]").first.locator("summary").click()
                page.wait_for_selector("[data-spp-history-job] .spp-test-history-json", state="visible", timeout=7000)
                detail_text = page.locator("[data-spp-history-job] .spp-test-history-json").first.inner_text()
                if '"baseline"' not in detail_text or '"measurements"' not in detail_text or '"restore"' not in detail_text:
                    raise AssertionError(f"expanded history detail is incomplete: {detail_text}")
                page.close()

            with _LocalSppUiServer(spp_test_enabled=False, prices_write_enabled=True) as server:
                base_url = server.base_url
                page = browser.new_page(viewport={"width": 1440, "height": 940})
                _prepare_spp_page(page, base_url)
                if not page.locator("[data-spp-test-start]").is_disabled():
                    raise AssertionError("SPP start must remain disabled when WB_SPP_TEST_ENABLED is off")
                reason = page.locator("[data-spp-test-start-reason]").inner_text().strip()
                if "включите WB_SPP_TEST_ENABLED" not in reason:
                    raise AssertionError(f"disabled SPP guard reason missing: {reason!r}")
                page.close()
        finally:
            browser.close()
    print("wb_spp_tester_browser_smoke: OK")


def _prepare_spp_page(page, base_url: str) -> None:
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
    page.locator('[data-spp-test-input="max_measurements"]').fill("30")
    page.locator("[data-spp-test-plan]").click()
    page.wait_for_function(
        "() => document.querySelector('[data-spp-test-plan-preview]')?.innerText.includes('WB uploads')",
        timeout=7000,
    )
    page.wait_for_function(
        "() => document.querySelector('[data-spp-test-plan-preview]')?.innerText.includes('30')",
        timeout=7000,
    )
    page.locator("[data-spp-test-confirm-live]").check()
    panel_text = page.locator('[data-prices-subpanel="spp-test"]').inner_text()
    for expected in ("Автопроверка", "Ежедневно", "Оренбург", "История проверок", "Baseline", "WB writes", "SPP guard"):
        if expected not in panel_text:
            raise AssertionError(f"SPP tester panel missing text {expected!r}: {panel_text}")
    if "План и измерения" not in panel_text and "Результат:" not in panel_text:
        raise AssertionError(f"SPP tester panel missing current/last job heading: {panel_text}")
    if "Текущие цены" not in page.locator("[data-prices-panel]").inner_text():
        raise AssertionError("prices subtabs must include current prices")


class _LocalSppUiServer:
    def __init__(self, *, spp_test_enabled: bool, prices_write_enabled: bool) -> None:
        self.tmp: TemporaryDirectory[str] | None = None
        self.server = None
        self.thread: threading.Thread | None = None
        self.base_url = ""
        self.spp_prices_source = FakeSppPricesSource()
        self.spp_test_enabled = spp_test_enabled
        self.prices_write_enabled = prices_write_enabled

    def __enter__(self) -> "_LocalSppUiServer":
        self.tmp = TemporaryDirectory(prefix="wb-spp-browser-")
        runtime_dir = Path(self.tmp.name) / "runtime"
        runtime = _seed_runtime(runtime_dir)
        prices_source = FakePricesSource()
        spp_prices_source = self.spp_prices_source
        spp_block = WbSppTesterBlock(
            runtime=runtime,
            runtime_dir=runtime_dir,
            prices_source=spp_prices_source,
            public_source=FakePublicSource(spp_prices_source),
            now_factory=lambda: NOW,
            timestamp_factory=lambda: "2026-07-07T07:00:00Z",
            sleep=lambda _seconds: None,
            safety_config=WbSppTesterSafetyConfig(
                spp_test_enabled=self.spp_test_enabled,
                prices_write_enabled=self.prices_write_enabled,
            ),
            cadence_config=WbSppTesterCadenceConfig(run_async=False),
        )
        if self.spp_test_enabled and self.prices_write_enabled:
            spp_block.start(
                {
                    "nmID": PRIMARY_NM,
                    "range_min_discounted": 700,
                    "range_max_discounted": 900,
                    "precision_rub": 2,
                    "max_measurements": 3,
                    "mode": "safe_slow",
                    "confirm_live_price_change": True,
                    "restore_baseline": True,
                },
                actor="browser_smoke",
            )
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            now_factory=lambda: NOW,
            activated_at_factory=lambda: "2026-07-07T07:00:00Z",
            prices_block=_build_prices_block(runtime, runtime_dir, prices_source, write_enabled=self.prices_write_enabled),
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
        return self

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
