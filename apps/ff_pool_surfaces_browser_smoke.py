"""Playwright smoke for the compact Stage 3 FF facility/pool operator modal."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import socket
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.ff_pool_foundation import FEATURE_EPOCHS_TABLE  # noqa: E402
from packages.application.ff_pool_surfaces import FfPoolSurface  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 12, 7, 0, tzinfo=timezone.utc)

    def __call__(self) -> str:
        result = self.value.isoformat(timespec="seconds").replace("+00:00", "Z")
        self.value += timedelta(seconds=1)
        return result


def main() -> None:
    with TemporaryDirectory(prefix="ff-pool-browser-") as directory:
        root = Path(directory)
        runtime_dir = root / "runtime"
        runtime_dir.mkdir()
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime.list_ff_stock_operations(limit=1)
        clock = Clock()
        _seed(runtime, clock)
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
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=clock,
        )
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    screenshot_path = Path(os.environ.get("FF_POOL_SCREENSHOT_PATH") or root / "mobile.png")
                    _run(browser, f"http://127.0.0.1:{config.port}", screenshot_path)
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print("ff_pool_surfaces_browser_smoke: OK")


def _seed(runtime: RegistryUploadDbBackedRuntime, clock: Clock) -> None:
    with sqlite3.connect(runtime.db_path) as conn:
        conn.execute(
            f"INSERT INTO {FEATURE_EPOCHS_TABLE}(epoch,writer_enabled,reader_enabled,source_revision,created_at,metadata_json) "
            "VALUES(1,1,0,'browser-writer',?,'{}')",
            (clock(),),
        )
        conn.commit()
    surface = FfPoolSurface(db_path=runtime.db_path, runtime_dir=runtime.runtime_dir, timestamp_factory=clock)
    for request_id, name in (
        ("browser:facility:one", "Москва Север"),
        ("browser:facility:two", "Оренбург"),
        ("browser:facility:xss", "<img src=x onerror=window.__ffPoolXss=1>"),
    ):
        surface.create_facility(
            {
                "request_id": request_id,
                "name": name,
                "active": True,
                "display_timezone": "Asia/Yekaterinburg",
            },
            actor="browser-fixture",
        )


def _run(browser: object, base: str, screenshot_path: Path) -> None:
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page_errors: list[str] = []
    console_errors: list[str] = []
    server_errors: list[str] = []
    pool_http_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
    page.on("response", lambda response: server_errors.append(f"{response.status} {response.url}") if response.status >= 500 else None)
    page.on("response", lambda response: pool_http_errors.append(f"{response.status} {response.url}") if response.status >= 400 and "/facility-pools" in response.url else None)
    url = f"{base}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}?tab=warehouses&warehouse=ff"
    response = page.goto(url, wait_until="domcontentloaded")
    assert response is not None and response.status == 200
    page.locator('[data-unified-tab-button="warehouses"]').click()
    page.locator('[data-warehouse-key="ff"]').click()
    launcher = page.locator("[data-ff-pool-open]")
    launcher.wait_for(state="visible")
    launcher.focus()
    launcher.click()
    dialog = page.get_by_role("dialog", name="Документы фулфилмента")
    dialog.wait_for(state="visible")
    page.locator("[data-ff-pool-facilities] .ff-pool-list-item").first.wait_for(state="visible")
    assert page.locator("[data-ff-pool-facilities] .ff-pool-list-item").count() == 3
    assert page.locator("[data-ff-pool-facilities] img").count() == 0
    assert page.evaluate("window.__ffPoolXss") is None
    page.locator("[data-ff-pool-facilities] .ff-pool-list-item", has_text="Москва Север").get_by_role("button", name="Открыть").click()
    page.locator("[data-ff-pool-facility-detail] h3").wait_for(state="visible")
    assert "Москва Север" in page.locator("[data-ff-pool-facility-detail]").inner_text()

    page.locator('[data-ff-pool-tab="create"]').click()
    page.locator("[data-ff-pool-action-kind]").select_option("transfer_root")
    source = page.locator("[data-ff-pool-facility]")
    destination = page.locator("[data-ff-pool-destination-facility]")
    source.select_option(index=0)
    destination.select_option(index=1)
    page.locator("[data-ff-pool-source-pool]").select_option("FBS")
    page.locator("[data-ff-pool-destination-pool]").select_option("FBO")
    page.locator("[data-ff-pool-preview]").click()
    page.locator("[data-ff-pool-workflow-detail] h3").wait_for(state="visible")
    assert "Готово к проведению" in page.locator("[data-ff-pool-workflow-detail]").inner_text()
    page.get_by_role("button", name="Подтвердить проведение").click()
    page.wait_for_function("document.querySelector('[data-ff-pool-workflow-detail] h3')?.textContent.includes('Завершено')")
    saved_request = page.locator("[data-ff-pool-request-id]").input_value()
    assert saved_request.startswith("ffpdr_")

    page.reload(wait_until="domcontentloaded")
    page.locator('[data-unified-tab-button="warehouses"]').click()
    page.locator('[data-warehouse-key="ff"]').click()
    page.locator("[data-ff-pool-open]").click()
    page.locator('[data-ff-pool-tab="workflow"]').click()
    page.locator("[data-ff-pool-workflow-detail] h3").wait_for(state="visible")
    assert page.locator("[data-ff-pool-request-id]").input_value() == saved_request
    assert "Завершено" in page.locator("[data-ff-pool-workflow-detail]").inner_text()

    page.set_viewport_size({"width": 390, "height": 844})
    page.locator('[data-ff-pool-tab="facilities"]').click()
    page.wait_for_timeout(100)
    assert dialog.evaluate("node => node.scrollWidth <= node.clientWidth + 1")
    page.screenshot(path=str(screenshot_path), full_page=False)
    page.keyboard.press("Escape")
    page.locator("[data-ff-pool-modal]").wait_for(state="hidden")
    assert page.evaluate("document.activeElement === document.querySelector('[data-ff-pool-open]')")
    assert not page_errors, page_errors
    fatal_console_errors = [item for item in console_errors if not item.startswith("Failed to load resource:")]
    assert not fatal_console_errors, fatal_console_errors
    assert not server_errors, server_errors
    assert not pool_http_errors, pool_http_errors
    context.close()


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


if __name__ == "__main__":
    main()
