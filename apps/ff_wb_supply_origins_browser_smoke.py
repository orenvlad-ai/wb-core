"""Playwright acceptance for Stage 4 WB-supply FF-origin assignment."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)

from apps.sheet_vitrina_v1_wb_supplies_http_smoke import (  # noqa: E402
    FakeTransitCostSource,
    FakeWbSuppliesSource,
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
from packages.application.ff_pool_foundation import (  # noqa: E402
    FACILITIES_TABLE,
    FEATURE_EPOCHS_TABLE,
)
from packages.application.ff_wb_supply_origins import ASSIGNMENTS_TABLE  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.registry_upload_http_entrypoint import (  # noqa: E402
    RegistryUploadHttpEntrypoint,
)
from packages.contracts.registry_upload_http_entrypoint import (  # noqa: E402
    RegistryUploadHttpEntrypointConfig,
)


def main() -> None:
    with TemporaryDirectory(prefix="ff-wb-origin-browser-") as directory:
        root = Path(directory)
        runtime_dir = root / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime.ingest_bundle(
            json.loads(INPUT_BUNDLE_FIXTURE.read_text(encoding="utf-8")),
            activated_at="2026-08-12T08:00:00Z",
        )
        _seed_facilities(runtime.db_path)
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=_clock(),
        )
        entrypoint.wb_supplies_block.source = FakeWbSuppliesSource()
        entrypoint.wb_supplies_block.transit_cost_source = FakeTransitCostSource({"1003": 3333.0})
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
            screenshot_path = Path(
                os.environ.get("FF_WB_SUPPLY_ORIGIN_SCREENSHOT_PATH") or root / "mobile.png"
            )
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    _run(browser, f"http://127.0.0.1:{config.port}", screenshot_path)
                finally:
                    browser.close()
            with sqlite3.connect(runtime.db_path) as conn:
                assignment = conn.execute(
                    f"SELECT wb_supply_id,facility_id,pool FROM {ASSIGNMENTS_TABLE}"
                ).fetchone()
                assert assignment == ("39265492", "facility-browser-good", "FBO")
                assert conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_ff_pool_movement_lines").fetchone()[0] == 0
                assert conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_business_operations").fetchone()[0] == 0
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print("ff_wb_supply_origins_browser_smoke: OK")


def _clock():
    current = datetime(2026, 8, 12, 8, 2, tzinfo=timezone.utc)

    def tick() -> str:
        nonlocal current
        result = current.isoformat(timespec="seconds").replace("+00:00", "Z")
        current += timedelta(seconds=1)
        return result

    return tick


def _seed_facilities(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            f"""INSERT INTO {FACILITIES_TABLE}(
                   facility_id,code,name,active,display_timezone,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?)""",
            [
                (
                    "facility-browser-good",
                    "FF-001",
                    "Москва Север",
                    1,
                    "Asia/Yekaterinburg",
                    "2026-08-12T08:01:00Z",
                    "2026-08-12T08:01:00Z",
                ),
                (
                    "facility-browser-xss",
                    "FF-002",
                    "<img src=x onerror=window.__ffOriginXss=1>",
                    1,
                    "Asia/Yekaterinburg",
                    "2026-08-12T08:01:00Z",
                    "2026-08-12T08:01:00Z",
                ),
            ],
        )
        conn.execute(
            f"""INSERT INTO {FEATURE_EPOCHS_TABLE}(
                   epoch,writer_enabled,reader_enabled,source_revision,created_at,metadata_json
               ) VALUES(1,1,0,'stage4-browser-writer','2026-08-12T08:01:00Z','{{}}')"""
        )
        conn.commit()


def _run(browser: object, base: str, screenshot_path: Path) -> None:
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    page_errors: list[str] = []
    console_errors: list[str] = []
    server_errors: list[str] = []
    origin_posts: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
    page.on("response", lambda response: server_errors.append(f"{response.status} {response.url}") if response.status >= 500 else None)
    page.on(
        "request",
        lambda request: origin_posts.append(request.url)
        if request.method == "POST" and "/wb-supply-origins/" in request.url
        else None,
    )
    response = page.goto(f"{base}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}", wait_until="domcontentloaded")
    assert response is not None and response.status == 200
    page.locator("[data-unified-tab-button='factory-order']").click()
    frame = page.frame_locator("iframe[title='Поставки']")
    frame.get_by_role("button", name="Wildberries", exact=True).click()
    frame.locator("#wbSuppliesRefreshButton").click()
    expect(frame.locator("#wbSuppliesTableBody")).to_contain_text("39265492", timeout=15_000)
    frame.locator("#wbSuppliesTableBody tr", has_text="39265492").click()
    expect(frame.locator("#wbSupplyFfOriginTitle")).to_be_visible(timeout=15_000)
    expect(frame.locator("#wbSupplyFfOriginMessage")).to_contain_text("Источник FF ещё не назначен", timeout=15_000)
    select = frame.locator("#wbSupplyFfOriginSelect")
    expect(select).to_be_enabled()
    option_texts = select.locator("option").all_text_contents()
    assert any("Москва Север" in text for text in option_texts)
    assert any("<img src=x" in text for text in option_texts)
    assert frame.locator("#wbSupplyFfOriginPanel img").count() == 0
    assert frame.locator("#wbSupplyFfOriginPanel").evaluate("node => node.ownerDocument.defaultView.__ffOriginXss") is None
    select.select_option("facility-browser-good")
    frame.locator("#wbSupplyFfOriginReason").fill("Фактическая отгрузка")
    frame.locator("#wbSupplyFfOriginSaveButton").click()
    expect(frame.locator("#wbSupplyFfOriginMessage")).to_contain_text("Текущий источник: FF-001 · Москва Север", timeout=15_000)
    expect(select).to_have_value("facility-browser-good")
    assert len(origin_posts) == 1, origin_posts

    page.reload(wait_until="domcontentloaded")
    page.locator("[data-unified-tab-button='factory-order']").click()
    frame = page.frame_locator("iframe[title='Поставки']")
    frame.get_by_role("button", name="Wildberries", exact=True).click()
    expect(frame.locator("#wbSuppliesTableBody")).to_contain_text("39265492", timeout=15_000)
    frame.locator("#wbSuppliesTableBody tr", has_text="39265492").click()
    expect(frame.locator("#wbSupplyFfOriginMessage")).to_contain_text("Текущий источник: FF-001 · Москва Север", timeout=15_000)
    expect(frame.locator("#wbSupplyFfOriginSelect")).to_have_value("facility-browser-good")

    page.set_viewport_size({"width": 390, "height": 844})
    expect(frame.locator("#wbSupplyFfOriginPanel")).to_be_visible()
    assert frame.locator("#wbSupplyFfOriginPanel").evaluate("node => node.scrollWidth <= node.clientWidth + 1")
    frame.locator("#wbSupplyFfOriginPanel").scroll_into_view_if_needed()
    page.screenshot(path=str(screenshot_path), full_page=False)
    assert not page_errors, page_errors
    fatal_console_errors = [item for item in console_errors if not item.startswith("Failed to load resource:")]
    assert not fatal_console_errors, fatal_console_errors
    assert not server_errors, server_errors
    context.close()


if __name__ == "__main__":
    main()
