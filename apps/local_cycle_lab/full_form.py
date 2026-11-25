"""Enter the control expense through the native form inside the private Linux guest."""
from pathlib import Path
from threading import Thread

from packages.adapters import registry_upload_http_entrypoint as web
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig


def enter_expense(entry, *, facility_id, now, username, password, evidence_dir: Path):
    from playwright.sync_api import sync_playwright

    config = RegistryUploadHttpEntrypointConfig(host="127.0.0.1", port=0,
        upload_path=web.DEFAULT_UPLOAD_PATH, sheet_plan_path=web.DEFAULT_SHEET_PLAN_PATH,
        sheet_refresh_path="/v1/sheet-vitrina-v1/refresh", sheet_status_path=web.DEFAULT_SHEET_STATUS_PATH,
        sheet_operator_ui_path=web.DEFAULT_SHEET_OPERATOR_UI_PATH, runtime_dir=entry.runtime.runtime_dir)
    server = web.build_registry_upload_http_server(config, entrypoint=entry, advertise_window_v3=False)
    server.service_actions = lambda: None  # Only explicit manual cycles run on this bench.
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as automation:
            browser = automation.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 1000}, locale="ru-RU")
                page.clock.set_fixed_time(now)
                page.goto(f"http://127.0.0.1:{server.server_port}/login")
                page.locator('input[name="username"]').fill(username)
                page.locator('input[name="password"]').fill(password)
                page.locator('button[type="submit"]').click()
                page.locator('[data-unified-tab-button="warehouses"]').click()
                page.locator('[data-warehouse-key="ff"]').click()
                page.locator('[data-ff-pool-open]').click()
                page.locator('[data-ff-pool-tab="create"]').click()
                page.locator('[data-ff-pool-action-kind]').select_option("pool_overhead")
                page.locator('[data-ff-pool-facility]').select_option(facility_id)
                page.locator('[data-ff-pool-scope]').select_option("FBS")
                page.locator('[data-ff-pool-overhead-category]').select_option("other")
                page.locator('[data-ff-pool-overhead-comment]').fill("Контрольный расход WBC · два полных цикла")
                page.locator('[data-ff-pool-amount]').fill("123.45")
                page.locator('[data-ff-pool-preview]').click()
                page.get_by_role("button", name="Подтвердить проведение", exact=True).click()
                receipt = page.locator('[data-ff-operation-receipt]')
                receipt.wait_for(state="visible")
                operation = receipt.get_attribute("data-ff-operation-receipt")
                page.screenshot(path=evidence_dir / "accepted-expense.png")
                if not operation:
                    raise ValueError("Native confirmation did not return an operation ID")
                return operation
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
