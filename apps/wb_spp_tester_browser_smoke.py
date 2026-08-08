"""Browser smoke for the manual operator SPP price checker."""

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
    FakePricesSource as CurrentPricesSource,
    NOW,
    _build_block as _build_prices_block,
    _reserve_free_port,
    _seed_runtime,
)
from apps.wb_spp_tester_smoke import (  # noqa: E402
    FakeBuyerSource,
    FakePricesSource,
    MutableClock,
    PRIMARY_NM,
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
from packages.application.wb_spp_tester import (  # noqa: E402
    WbSppTesterBlock,
    WbSppTesterCadenceConfig,
    WbSppTesterSafetyConfig,
)
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


def _open_manual_panel(page, base_url: str) -> None:
    page.goto(f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}", wait_until="domcontentloaded")
    page.locator('[data-unified-tab-button="prices"]').click()
    page.locator('[data-prices-subtab="spp-test"]').click()
    page.wait_for_function(
        "() => document.querySelector('[data-wb-buyer-session-state]')?.innerText.includes('Готов')",
        timeout=7000,
    )
    page.locator("[data-spp-test-nm]").select_option(str(PRIMARY_NM))


def _assert_minimal_surface(page) -> None:
    panel = page.locator('[data-prices-subpanel="spp-test"]')
    text = panel.inner_text()
    for expected in (
        "Бот покупателя",
        "Сколько цен проверить",
        "Старт проверки",
        "Текущий результат",
        "История проверок",
        "Технический лог · последние 10 событий",
    ):
        if expected not in text:
            raise AssertionError(f"manual SPP panel is missing {expected!r}: {text}")
    for removed in (
        "Автопроверка",
        "Мин. цена",
        "Макс. цена",
        "Точность",
        "Макс. измерений",
        "Построить план",
        "Пороги",
        "Анонимная цена",
        "Payment context",
        "Raw JSON",
    ):
        if removed in text:
            raise AssertionError(f"removed SPP product surface is still visible: {removed!r}")
    forbidden_selectors = (
        "[data-spp-schedule-enabled]",
        "[data-spp-test-plan]",
        "[data-spp-test-baseline]",
        "[data-spp-test-thresholds]",
        "[data-wb-buyer-session-install]",
        "[data-wb-buyer-session-launcher]",
    )
    if panel.locator(", ".join(forbidden_selectors)).count() != 0:
        raise AssertionError("removed or centralized controls remain in the tester DOM")


def _assert_dynamic_fields_and_validation(page) -> None:
    count = page.locator("[data-spp-test-price-count]")
    count.select_option("1")
    if page.locator("[data-spp-test-price-index]").count() != 1:
        raise AssertionError("one-price selection must render exactly one field")
    count.select_option("6")
    if page.locator("[data-spp-test-price-index]").count() != 6:
        raise AssertionError("six-price selection must render exactly six fields")
    page.locator('[data-spp-test-price-index="0"]').fill("0")
    if not page.locator("[data-spp-test-start]").is_disabled():
        raise AssertionError("zero and incomplete prices must keep Start disabled")
    if "положительными денежными значениями" not in page.locator("[data-spp-test-start-reason]").inner_text():
        raise AssertionError("invalid-price explanation must be short and explicit")


def _assert_progressive_run(page, server: "_LocalSppUiServer") -> None:
    page.locator("[data-spp-test-price-count]").select_option("2")
    page.locator('[data-spp-test-price-index="0"]').fill("810")
    page.locator('[data-spp-test-price-index="1"]').fill("800")
    page.wait_for_function("() => document.querySelector('[data-spp-test-start]')?.disabled === false")
    page.locator("[data-spp-test-start]").click()
    page.wait_for_function(
        "() => document.querySelectorAll('[data-spp-test-measurements] tr').length === 1 && "
        "document.querySelector('[data-spp-test-state]')?.innerText.includes('ожидание')",
        timeout=7000,
    )
    page.wait_for_function(
        "() => document.querySelector('[data-spp-test-state]')?.innerText.includes('готово')",
        timeout=10000,
    )
    rows = page.locator("[data-spp-test-measurements] tr")
    if rows.count() != 2:
        raise AssertionError("each entered price must produce one compact progressive result row")
    result_text = rows.all_inner_texts()
    if "810" not in result_text[0] or "800" not in result_text[1] or "10,00%" not in " ".join(result_text):
        raise AssertionError(f"ordered results or SPP display are wrong: {result_text}")
    page.wait_for_function(
        "() => document.querySelectorAll('[data-spp-test-log] .spp-test-log-row').length === 10",
        timeout=7000,
    )
    log_text = page.locator("[data-spp-test-log]").inner_text()
    for forbidden in ("Authorization", "must-not-leak", "access_token", "cookie", "/opt/"):
        if forbidden in log_text:
            raise AssertionError(f"technical log leaked {forbidden!r}")
    page.wait_for_selector("[data-spp-history-list] .spp-test-history-row", timeout=7000)
    history_text = page.locator("[data-spp-history-list]").inner_text()
    if str(PRIMARY_NM) not in history_text or "810" not in history_text or "800" not in history_text:
        raise AssertionError(f"compact history is incomplete: {history_text}")
    final = server.spp_block.status({})["job"]
    proof = final["restore"]
    if not (
        final["status"] == "complete"
        and proof["restored"] is True
        and proof["price_matches"] is True
        and proof["discount_matches"] is True
        and proof["discountedPrice_matches"] is True
        and proof["quarantine_absent"] is True
        and server.spp_prices_source.discounted_price == 900.0
        and server.spp_block.status({})["active_job"] is None
    ):
        raise AssertionError(f"browser run did not prove exact restore/quarantine/lock cleanup: {final}")


def _assert_fresh_start_preflight_zero_writes(browser) -> None:
    with _LocalSppUiServer() as server:
        page = browser.new_page(viewport={"width": 1440, "height": 940})
        _open_manual_panel(page, server.base_url)
        page.locator('[data-spp-test-price-index="0"]').fill("810")
        page.wait_for_function("() => document.querySelector('[data-spp-test-start]')?.disabled === false")
        server.buyer_source.logged_out = True
        page.locator("[data-spp-test-start]").click()
        page.wait_for_function(
            "() => document.querySelector('[data-spp-test-error]')?.innerText.includes('Ни одна цена не изменена')",
            timeout=7000,
        )
        if server.spp_prices_source.upload_payloads:
            raise AssertionError("fresh logged-out Start preflight must perform zero seller writes")
        if "Разлогинен" not in page.locator("[data-wb-buyer-session-state]").inner_text():
            raise AssertionError("fresh failed preflight must update the compact bot status")
        page.wait_for_function(
            "() => document.querySelector('[data-spp-test-log]')?.innerText.includes('Проверка бота')",
            timeout=7000,
        )
        page.close()


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            with _LocalSppUiServer() as server:
                page = browser.new_page(viewport={"width": 1440, "height": 940})
                page_errors: list[str] = []
                console_errors: list[str] = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.on(
                    "console",
                    lambda message: console_errors.append(message.text)
                    if message.type == "error" and "Failed to load resource" not in message.text
                    else None,
                )
                _open_manual_panel(page, server.base_url)
                _assert_minimal_surface(page)
                _assert_dynamic_fields_and_validation(page)
                _assert_progressive_run(page, server)
                if "Текущие цены" not in page.locator("[data-prices-panel]").inner_text():
                    raise AssertionError("the neighboring current-prices subtab regressed")
                if page_errors or console_errors:
                    raise AssertionError(f"fatal browser errors: page={page_errors} console={console_errors}")
                page.close()
            _assert_fresh_start_preflight_zero_writes(browser)
        finally:
            browser.close()
    print("wb_spp_tester_browser_smoke: OK")


class _LocalSppUiServer:
    def __init__(self) -> None:
        self.tmp: TemporaryDirectory[str] | None = None
        self.server = None
        self.thread: threading.Thread | None = None
        self.base_url = ""
        self.spp_prices_source = FakePricesSource()
        self.buyer_source = FakeBuyerSource(self.spp_prices_source)
        self.spp_block: WbSppTesterBlock

    def __enter__(self) -> "_LocalSppUiServer":
        self.tmp = TemporaryDirectory(prefix="wb-spp-browser-")
        runtime_dir = Path(self.tmp.name) / "runtime"
        runtime = _seed_runtime(runtime_dir)
        current_prices_source = CurrentPricesSource()
        clock = MutableClock()
        self.spp_block = WbSppTesterBlock(
            runtime=runtime,
            runtime_dir=runtime_dir,
            prices_source=self.spp_prices_source,
            buyer_source=self.buyer_source,
            now_factory=clock.now,
            timestamp_factory=clock.timestamp,
            sleep=lambda seconds: threading.Event().wait(float(seconds)),
            safety_config=WbSppTesterSafetyConfig(spp_test_enabled=True, prices_write_enabled=True),
            cadence_config=WbSppTesterCadenceConfig(
                run_async=True,
                measurement_upload_cooldown_seconds=1,
                first_buyer_poll_delay_seconds=0,
                buyer_poll_gap_seconds=0,
                upload_status_poll_seconds=0,
                upload_status_max_polls=2,
                readback_poll_seconds=0,
                readback_max_polls=2,
                rate_limit_min_cooldown_seconds=0,
                active_lock_ttl_seconds=60,
            ),
        )
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            now_factory=lambda: NOW,
            activated_at_factory=lambda: "2026-08-08T08:00:00Z",
            prices_block=_build_prices_block(runtime, runtime_dir, current_prices_source, write_enabled=True),
            spp_tester_block=self.spp_block,
            buyer_session_block=self.buyer_source,  # type: ignore[arg-type]
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
