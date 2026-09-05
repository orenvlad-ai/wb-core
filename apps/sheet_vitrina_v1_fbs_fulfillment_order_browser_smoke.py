#!/usr/bin/env python3
"""Browser contract smoke for the independent FBS fulfillment-order UI."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.fbs_fulfillment_order_supply_smoke import (  # noqa: E402
    INPUT_BUNDLE_FIXTURE,
    MOSCOW_ID,
    ORENBURG_ID,
    _seed_facilities,
    _seed_sales_history,
    _seed_shipments,
)
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_REFRESH_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.registry_upload_http_entrypoint import (  # noqa: E402
    RegistryUploadHttpEntrypoint,
)
from packages.contracts.registry_upload_http_entrypoint import (  # noqa: E402
    RegistryUploadHttpEntrypointConfig,
)


NOW = datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc)
NOW_TEXT = "2026-04-18T09:00:00Z"


def main() -> int:
    bundle = json.loads(INPUT_BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="fbs-fulfillment-browser-") as raw:
        runtime_dir = Path(raw) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime.ingest_bundle(bundle, activated_at=NOW_TEXT)
        active_nm_ids = [
            int(item.nm_id)
            for item in runtime.load_current_state().config_v2
            if item.enabled
        ]
        _seed_facilities(runtime, active_nm_ids)
        _seed_sales_history(runtime, active_nm_ids)
        _seed_shipments(runtime, active_nm_ids)

        port = _reserve_free_port()
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: NOW_TEXT,
            now_factory=lambda: NOW,
        )
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=port,
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path=DEFAULT_SHEET_REFRESH_PATH,
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime_dir,
        )
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        page_errors: list[str] = []
        console_errors: list[str] = []
        fbs_http_errors: list[str] = []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                page = context.new_page()
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.on(
                    "console",
                    lambda message: console_errors.append(message.text)
                    if message.type == "error"
                    and not message.text.startswith("Failed to load resource:")
                    else None,
                )
                page.on(
                    "response",
                    lambda response: fbs_http_errors.append(
                        f"{response.status} {response.url}"
                    )
                    if "fbs-fulfillment-order" in response.url
                    and response.status >= 400
                    else None,
                )
                page.goto(
                    f"http://127.0.0.1:{port}{DEFAULT_SHEET_OPERATOR_UI_PATH}"
                    "?embedded_tab=factory-order",
                    wait_until="domcontentloaded",
                )

                fbs_panel = page.locator(
                    '[data-supply-section-panel="fbs-fulfillment"]'
                )
                legacy_panel = page.locator('[data-supply-section-panel="factory"]')
                expect(fbs_panel).to_be_visible()
                expect(legacy_panel).to_be_hidden()
                expect(page.locator("#legacyFactoryOrderDetails")).not_to_have_attribute(
                    "open", ""
                )
                expect(page.locator("#fbsHistoryModeLastN")).to_be_checked()
                expect(page.locator("#fbsInboundScope")).to_have_value(
                    "selected_facility"
                )
                expect(page.locator("#fbsInboundScopeHelp")).to_contain_text(
                    "нельзя одновременно считать распределённым"
                )
                expect(page.locator("#fbsReadinessDetails")).not_to_have_attribute(
                    "open", ""
                )
                assert page.locator("#fbsFulfillmentResultBody").count() == 0
                assert fbs_panel.evaluate(
                    "element => element.scrollWidth <= element.clientWidth + 1"
                )
                expect(page.locator("#fbsSalesAvgPeriodDays")).to_have_value("14")
                expect(page.locator("#fbsSalesDateFrom")).to_be_disabled()
                expect(page.locator("#fbsSalesDateTo")).to_be_disabled()

                facility = page.locator("#fbsTargetFacility")
                expect(facility.locator("option")).to_have_count(3, timeout=15000)
                expect(facility).to_have_value(MOSCOW_ID)
                expect(page.locator("#fbsFulfillmentCalculateButton")).to_be_enabled()
                expect(page.locator("#fbsReadinessPhysical")).to_have_text("Официальный снимок FBS WB")
                expect(page.locator("#fbsReadinessReserved")).to_have_text(NOW_TEXT)
                expect(page.locator("#fbsReadinessAvailable")).not_to_have_text("-")
                expect(page.locator("#fbsHistoryCoverage")).to_contain_text(
                    "2026-04-01 — 2026-04-17"
                )

                facility.select_option(ORENBURG_ID)
                expect(page.locator("#fbsFulfillmentCalculateButton")).to_be_disabled()
                expect(page.locator("#fbsReadinessBlockers")).to_contain_text(
                    "Расчёт заблокирован"
                )
                facility.select_option(MOSCOW_ID)
                expect(page.locator("#fbsFulfillmentCalculateButton")).to_be_enabled()

                page.locator("#fbsHistoryModeCustom").check()
                expect(page.locator("#fbsSalesAvgPeriodDays")).to_be_disabled()
                page.locator("#fbsSalesDateFrom").fill("2026-04-10")
                page.locator("#fbsSalesDateTo").fill("2026-04-12")
                page.locator("#fbsFulfillmentCalculateButton").click()
                expect(page.locator("#fbsFulfillmentMessage")).to_contain_text(
                    "Расчёт завершён", timeout=15000
                )
                expect(page.locator("#fbsDemandWindow")).to_contain_text(
                    "Произвольный период"
                )
                expect(page.locator("#fbsDemandWindow")).to_contain_text(
                    "2026-04-10 — 2026-04-12"
                )
                expect(page.locator("#fbsDemandDays")).to_contain_text("3 /")
                expect(page.locator("#fbsTotalQty")).not_to_have_text("-")
                expect(page.locator("#fbsHorizonDays")).to_have_text("89")
                expect(page.locator("#fbsFulfillmentDownloadButton")).to_be_enabled()
                expect(page.locator("#fbsPreviewNote")).to_contain_text("Рассчитано")
                expect(page.locator("#fbsPreviewNote")).to_contain_text(NOW_TEXT)
                expect(page.locator("#fbsResultInboundScope")).to_have_text(
                    "Только для выбранного ФФ"
                )
                expect(page.locator("#fbsResultInbound")).to_contain_text("15 шт.")

                with page.expect_download(timeout=15000) as download_info:
                    page.locator("#fbsFulfillmentDownloadButton").click()
                download = download_info.value
                assert download.suggested_filename.endswith(".xlsx")
                expect(page.locator("#fbsFulfillmentMessage")).to_contain_text(
                    "FBS-рекомендация скачана"
                )

                page.locator("#fbsInboundScope").select_option("all_active")
                expect(page.locator("#fbsFulfillmentMessage")).to_contain_text(
                    "Параметры изменены"
                )
                expect(page.locator("#fbsTotalQty")).to_have_text("—")
                expect(page.locator("#fbsFulfillmentDownloadButton")).to_be_disabled()
                page.locator("#fbsFulfillmentCalculateButton").click()
                expect(page.locator("#fbsFulfillmentMessage")).to_contain_text(
                    "Расчёт завершён", timeout=15000
                )
                expect(page.locator("#fbsResultInboundScope")).to_have_text(
                    "Все активные заказы фабрике"
                )
                expect(page.locator("#fbsResultInbound")).to_contain_text("535 шт.")

                page.locator("#fbsHistoryModeLastN").check()
                expect(page.locator("#fbsSalesAvgPeriodDays")).to_be_enabled()
                expect(page.locator("#fbsSalesDateFrom")).to_be_disabled()
                expect(page.locator("#fbsSalesDateTo")).to_be_disabled()

                page.set_viewport_size({"width": 390, "height": 844})
                assert fbs_panel.evaluate(
                    "element => element.scrollWidth <= element.clientWidth + 1"
                )
                expect(page.locator("#fbsInboundScope")).to_be_visible()
                expect(page.locator("#fbsFulfillmentDownloadButton")).to_be_visible()

                # A historical result must not be relabelled as official current stock.
                from dataclasses import asdict
                from packages.application.fbs_fulfillment_order import FbsFulfillmentOrderBlock
                legacy = asdict(FbsFulfillmentOrderBlock(runtime=runtime,
                    now_factory=lambda: NOW, timestamp_factory=lambda: NOW_TEXT).calculate(
                        {"target_facility_id": MOSCOW_ID}))
                legacy["facility_readiness"].pop("stock_source")
                runtime.load_fbs_fulfillment_order_result_state = lambda: legacy
                page.reload(wait_until="domcontentloaded")
                expect(page.locator("#fbsPreviewNote")).to_contain_text("прежний складской учёт")
                expect(page.locator("#fbsReadinessPhysical")).to_have_text("Официальный снимок FBS WB")
                expect(page.locator("#fbsFulfillmentCalculateButton")).to_be_enabled()

                context.close()
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        if page_errors:
            raise AssertionError(f"browser page errors: {page_errors}")
        if console_errors:
            raise AssertionError(f"browser console errors: {console_errors}")
        if fbs_http_errors:
            raise AssertionError(f"browser FBS HTTP errors: {fbs_http_errors}")
    print("sheet_vitrina_v1_fbs_fulfillment_order_browser_smoke: ok")
    return 0


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    raise SystemExit(main())
