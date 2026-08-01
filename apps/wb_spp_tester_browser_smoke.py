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
from apps.wb_spp_tester_smoke import (  # noqa: E402
    FakeBuyerRecoveryController,
    FakeBuyerSource,
    FakePublicSource,
    FakeSppPricesSource,
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


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            with _LocalSppUiServer(spp_test_enabled=True, prices_write_enabled=True) as server:
                base_url = server.base_url
                page = browser.new_page(viewport={"width": 1440, "height": 940})
                _prepare_spp_page(page, base_url)
                if server.buyer_recovery.start_calls != 0:
                    raise AssertionError("a valid buyer session must not start recovery or create a launcher")
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
                page.locator("[data-spp-schedule-enabled]").uncheck()
                page.locator("[data-spp-schedule-save]").click()
                page.wait_for_function(
                    "() => document.querySelector('[data-spp-schedule-note]')?.innerText.includes('сохранено и выключено')",
                    timeout=7000,
                )
                if len(server.spp_prices_source.upload_payloads) != upload_count:
                    raise AssertionError("disabling the UI schedule must not start an SPP job")
                page.wait_for_selector("[data-spp-history-job]", timeout=7000)
                page.locator("[data-spp-history-job]").first.locator("summary").click()
                page.wait_for_selector("[data-spp-history-job] .spp-test-history-json", state="visible", timeout=7000)
                detail_text = page.locator("[data-spp-history-job] .spp-test-history-json").first.inner_text()
                if '"baseline"' not in detail_text or '"measurements"' not in detail_text or '"restore"' not in detail_text:
                    raise AssertionError(f"expanded history detail is incomplete: {detail_text}")
                for field in ("authenticated_buyer_price", "anonymous_buyer_price", "authenticated_spp_proxy", "anonymous_spp_proxy", "payment_context"):
                    if field not in detail_text:
                        raise AssertionError(f"expanded history is missing buyer evidence {field}: {detail_text}")
                if not page.locator("[data-spp-test-restore]").evaluate("node => node.hidden"):
                    raise AssertionError("restored terminal job must not keep emergency restore visible")
                if server.spp_block.status({})["active_job"] is not None:
                    raise AssertionError("restored terminal UI fixture must not expose a false active job")
                page.close()

            with _LocalSppUiServer(
                spp_test_enabled=True,
                prices_write_enabled=True,
                seed_manual_restore_required=True,
            ) as server:
                page = browser.new_page(viewport={"width": 1440, "height": 940})
                page.goto(f"{server.base_url}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}", wait_until="domcontentloaded")
                page.locator('[data-unified-tab-button="prices"]').click()
                page.locator('[data-prices-subtab="spp-test"]').click()
                page.wait_for_function(
                    "() => document.querySelector('[data-spp-test-state]')?.innerText.includes('нужен restore')",
                    timeout=7000,
                )
                restore_button = page.locator("[data-spp-test-restore]")
                if restore_button.evaluate("node => node.hidden") or restore_button.is_disabled():
                    raise AssertionError("active/unrestored job must expose enabled emergency restore")
                panel_text = page.locator('[data-prices-subpanel="spp-test"]').inner_text()
                if "Runtime lock" not in panel_text or "stale; нужен restore" not in panel_text:
                    raise AssertionError(f"stale active lock state must be explicit in UI: {panel_text}")
                restore_button.click()
                page.wait_for_function(
                    "() => document.querySelector('[data-spp-test-state]')?.innerText.includes('baseline восстановлен')",
                    timeout=7000,
                )
                page.wait_for_function(
                    "() => document.querySelector('[data-spp-test-restore]')?.hidden === true",
                    timeout=7000,
                )
                restored_status = server.spp_block.status({})
                if restored_status["active_job"] is not None or restored_status["job"]["result_status"] != "inconclusive":
                    raise AssertionError(f"UI restore must clear active lock without fake manual result: {restored_status}")
                restored_panel_text = page.locator('[data-prices-subpanel="spp-test"]').inner_text()
                if "lock cleared" not in restored_panel_text and "cleared after seller baseline proof" not in restored_panel_text:
                    raise AssertionError(f"reconciled/cleared lock state must stay visible: {restored_panel_text}")
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

            with _LocalSppUiServer(spp_test_enabled=True, prices_write_enabled=True, buyer_session_status="missing") as server:
                page = browser.new_page(viewport={"width": 1440, "height": 940})
                page.goto(f"{server.base_url}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}", wait_until="domcontentloaded")
                page.locator('[data-unified-tab-button="prices"]').click()
                page.locator('[data-prices-subtab="spp-test"]').click()
                page.wait_for_function(
                    "() => document.querySelector('[data-wb-buyer-session-state]')?.innerText.includes('Не установлена')",
                    timeout=7000,
                )
                panel = page.locator('[data-prices-subpanel="spp-test"]')
                if panel.locator("[data-wb-buyer-session-install], [data-wb-buyer-session-launcher]").count() != 0:
                    raise AssertionError("SPP monitoring UI must not expose buyer recovery or launcher controls")
                page.locator("[data-spp-test-nm]").select_option(str(PRIMARY_NM))
                if not page.locator("[data-spp-test-plan]").is_disabled():
                    raise AssertionError("invalid buyer session must disable manual plan before any write")
                if "Настройки → Источники и сессии" not in (page.locator("[data-spp-test-plan]").get_attribute("title") or ""):
                    raise AssertionError("invalid buyer session plan gate must point to centralized settings")
                page.locator("[data-spp-schedule-enabled]").check()
                if not page.locator("[data-spp-schedule-save]").is_disabled():
                    raise AssertionError("invalid buyer session must disable enabling the schedule")
                if "Настройки → Источники и сессии" not in (page.locator("[data-spp-schedule-save]").get_attribute("title") or ""):
                    raise AssertionError("invalid buyer session schedule gate must point to centralized settings")
                page.reload(wait_until="domcontentloaded")
                page.locator('[data-unified-tab-button="prices"]').click()
                page.locator('[data-prices-subtab="spp-test"]').click()
                page.wait_for_function(
                    "() => document.querySelector('[data-wb-buyer-session-state]')?.innerText.includes('Не установлена')",
                    timeout=7000,
                )
                if server.buyer_recovery.start_calls != 0:
                    raise AssertionError("SPP page must never start buyer recovery on open or reload")
                page.close()

            with _LocalSppUiServer(
                spp_test_enabled=True,
                prices_write_enabled=True,
                buyer_session_status="expired",
                buyer_recovery_auto_complete=True,
            ) as server:
                page = browser.new_page(viewport={"width": 1440, "height": 940})
                page.goto(f"{server.base_url}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}", wait_until="domcontentloaded")
                page.locator('[data-unified-tab-button="prices"]').click()
                page.locator('[data-prices-subtab="spp-test"]').click()
                page.wait_for_function(
                    "() => document.querySelector('[data-wb-buyer-session-state]')?.innerText.includes('Истекла')",
                    timeout=7000,
                )
                if server.buyer_recovery.start_calls != 0 or server.buyer_recovery.launcher_calls != 0:
                    raise AssertionError("SPP page must not auto-recover or download a noVNC launcher")
                page.close()
        finally:
            browser.close()
    print("wb_spp_tester_browser_smoke: OK")


def _prepare_spp_page(page, base_url: str) -> None:
    page.goto(f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}", wait_until="domcontentloaded")
    page.locator('[data-unified-tab-button="prices"]').click()
    page.locator('[data-prices-subtab="spp-test"]').click()
    page.wait_for_function(
        "() => document.querySelector('[data-wb-buyer-session-state]')?.innerText.includes('Маршрут доступен')",
        timeout=7000,
    )
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
    for expected in (
        "Покупательская сессия",
        "Проверить",
        "Автопроверка",
        "Ежедневно",
        "Оренбург",
        "История проверок",
        "Цена авторизованного покупателя",
        "Анонимная цена",
        "SPP-прокси авторизованный",
        "SPP-прокси анонимный",
        "Payment context",
        "Baseline",
        "WB writes",
        "SPP guard",
    ):
        if expected not in panel_text:
            raise AssertionError(f"SPP tester panel missing text {expected!r}: {panel_text}")
    if page.locator("[data-wb-buyer-session-install], [data-wb-buyer-session-launcher]").count() != 0:
        raise AssertionError("SPP tester must remain monitoring-only for buyer authentication")
    if "План и измерения" not in panel_text and "Результат:" not in panel_text:
        raise AssertionError(f"SPP tester panel missing current/last job heading: {panel_text}")
    if "Текущие цены" not in page.locator("[data-prices-panel]").inner_text():
        raise AssertionError("prices subtabs must include current prices")


class _LocalSppUiServer:
    def __init__(
        self,
        *,
        spp_test_enabled: bool,
        prices_write_enabled: bool,
        buyer_session_status: str = "valid",
        buyer_recovery_auto_complete: bool = False,
        seed_manual_restore_required: bool = False,
    ) -> None:
        self.tmp: TemporaryDirectory[str] | None = None
        self.server = None
        self.thread: threading.Thread | None = None
        self.base_url = ""
        self.spp_prices_source = FakeSppPricesSource()
        self.spp_test_enabled = spp_test_enabled
        self.prices_write_enabled = prices_write_enabled
        self.buyer_session_status = buyer_session_status
        self.buyer_recovery_auto_complete = buyer_recovery_auto_complete
        self.seed_manual_restore_required = seed_manual_restore_required
        self.spp_block: WbSppTesterBlock | None = None
        self.buyer_recovery: FakeBuyerRecoveryController | None = None

    def __enter__(self) -> "_LocalSppUiServer":
        self.tmp = TemporaryDirectory(prefix="wb-spp-browser-")
        runtime_dir = Path(self.tmp.name) / "runtime"
        runtime = _seed_runtime(runtime_dir)
        prices_source = FakePricesSource()
        spp_prices_source = self.spp_prices_source
        buyer_source = FakeBuyerSource(spp_prices_source, session_status=self.buyer_session_status)
        self.buyer_recovery = FakeBuyerRecoveryController(
            auto_complete=self.buyer_recovery_auto_complete,
            on_complete=lambda: setattr(buyer_source, "session_status", "valid"),
        )
        spp_block = WbSppTesterBlock(
            runtime=runtime,
            runtime_dir=runtime_dir,
            prices_source=spp_prices_source,
            public_source=FakePublicSource(spp_prices_source),
            buyer_source=buyer_source,
            now_factory=lambda: NOW,
            timestamp_factory=lambda: "2026-07-07T07:00:00Z",
            sleep=lambda _seconds: None,
            safety_config=WbSppTesterSafetyConfig(
                spp_test_enabled=self.spp_test_enabled,
                prices_write_enabled=self.prices_write_enabled,
            ),
            cadence_config=WbSppTesterCadenceConfig(run_async=False),
        )
        self.spp_block = spp_block
        if self.spp_test_enabled and self.prices_write_enabled and self.buyer_session_status == "valid":
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
        if self.seed_manual_restore_required:
            baseline = spp_block.build_baseline({"nmID": PRIMARY_NM})["baseline"]
            spp_prices_source.upload_task([{"nmID": PRIMARY_NM, "price": 1500, "discount": 10}])
            manual_job = {
                "job_id": "zz_ui_manual_restore_job",
                "created_at": "2026-07-07T07:00:00Z",
                "updated_at": "2026-07-07T07:00:00Z",
                "finished_at": "",
                "actor": "browser_smoke",
                "trigger_source": "manual",
                "status": "cooldown",
                "result_status": "",
                "nmID": PRIMARY_NM,
                "input": {
                    "range_min_discounted": 700,
                    "range_max_discounted": 900,
                    "precision_rub": 2,
                    "max_measurements": 3,
                    "mode": "safe_slow",
                    "restore_baseline": True,
                },
                "baseline": baseline,
                "plan": {},
                "measurements": [{"actual_wb_discounted_price": spp_prices_source.discounted_price}],
                "thresholds": [],
                "timeline": [],
                "restore": {"required": True, "restored": False, "proof": None, "steps": []},
                "lifecycle_diagnostics": {"classification": "live", "phase": "cooldown"},
                "manual_restore_required": False,
                "warnings": [],
                "error": "",
            }
            spp_block._save_job(manual_job)
            spp_block._write_current_job(manual_job)
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            now_factory=lambda: NOW,
            activated_at_factory=lambda: "2026-07-07T07:00:00Z",
            prices_block=_build_prices_block(runtime, runtime_dir, prices_source, write_enabled=self.prices_write_enabled),
            spp_tester_block=spp_block,
            buyer_session_block=buyer_source,  # type: ignore[arg-type]
            buyer_session_recovery_controller=self.buyer_recovery,
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
