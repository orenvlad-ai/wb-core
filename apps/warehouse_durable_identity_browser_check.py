"""Execute the real warehouse identity/polling functions in a disposable browser."""
from pathlib import Path
import json
import re
import sys
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def client_script():
    source = (ROOT / "packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html").read_text()
    sections = [source[source.index("    function stopWarehouseCurrentSyncPolling()"):source.index("    function warehouseRecoveryBytes(")],
                source[source.index("    function warehouseCurrentSyncStorageKey()"):source.index("    function renderWarehouseDocumentRow(")]]
    names = sorted(set(re.findall(r"\bwarehouse\w+(?:Node|Button)\b", "\n".join(sections))))
    declarations = []
    for name in names:
        selector = "button" if name.endswith("Button") else "#status" if name == "warehouseUpdateStatusNode" else "#run" if name == "warehouseUpdateRunIdNode" else None
        declarations.append(f"const {name} = " + (f"document.querySelector('{selector}');" if selector else "null;"))
    return "\n".join(["const WEB_VITRINA_CONFIG = {user_config_key:'fixture_owner'};",
        "const state = {warehouses:{activeKey:'update',updateRunId:'',updateRequestKey:'',updatePollCount:0,updatePollTimer:null}};",
        "const warehouseDate = String; const warehouseNumber = String; const escapeHtml = String; const loadWarehouseRecoveryStatus = () => {};",
        *declarations, *sections,
        "document.querySelector('button').addEventListener('click',startWarehouseCurrentSync);"])


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        posts, gets = [], []
        scenario = {"status": "accepted", "unknown": False, "lost": True}
        def route(request):
            req = request.request
            if req.is_navigation_request():
                request.fulfill(status=200, content_type="text/html", body='<button>Обновить</button><p id="status"></p><p id="run"></p>')
            elif req.method == "POST":
                body = req.post_data_json
                saved = page.evaluate("JSON.parse(localStorage.getItem(warehouseCurrentSyncStorageKey()))")
                assert saved["request_key"] == body["request_key"], "key was not persisted before POST"
                posts.append(body)
                if scenario["lost"]:
                    request.abort("connectionreset")
                else:
                    request.fulfill(status=202, json={"status":"accepted", "run_id":"public_original", "can_start_new":False})
            else:
                gets.append(req.url)
                if scenario["unknown"]:
                    request.fulfill(status=404, json={"error":"not found"})
                else:
                    status = scenario["status"]
                    request.fulfill(status=200, json={"status":status, "run_id":"public_original",
                        "user_status":"Состояние " + status, "can_start_new": status == "success", "durable_journal":{}})
        page.route("**/*", route)
        page.goto("http://127.0.0.1:8912/")
        page.add_script_tag(content=client_script())
        page.locator("button").click()
        page.wait_for_function("state.warehouses.updateRunId === 'public_original'")
        assert len(posts) == 1 and "request_key=" in gets[0]
        saved_key = posts[0]["request_key"]
        # Real reload retains localStorage but destroys in-memory state.
        page.reload()
        page.add_script_tag(content=client_script())
        page.evaluate("loadWarehouseCurrentSyncStatus()")
        assert "run_id=public_original" in gets[-1] and len(posts) == 1
        assert page.evaluate("state.warehouses.updateRequestKey") == saved_key
        # A stopped operation never enables a second effect.
        for status in ("accepted", "queued", "deferred", "consumer_pending", "interrupted", "failed"):
            page.evaluate("(status) => renderWarehouseCurrentSyncStatus({status:status,run_id:'public_original',can_start_new:false})", status)
            assert page.locator("button").is_disabled(), status
        # Bound automatic polling even if every response remains queued/running.
        page.evaluate("state.warehouses.updatePollCount=100; renderWarehouseCurrentSyncStatus({status:'queued',run_id:'public_original',can_start_new:false})")
        assert page.evaluate("state.warehouses.updatePollTimer === null")
        assert "Опрос приостановлен" in page.locator("#status").inner_text()
        scenario["unknown"] = True
        page.evaluate("loadWarehouseCurrentSyncStatus('public_original')")
        assert page.locator("button").is_disabled() and len(posts) == 1
        assert "повторная отправка заблокирована" in page.locator("#status").inner_text()
        assert page.evaluate("!!localStorage.getItem(warehouseCurrentSyncStorageKey())")
        # Only confirmed completion releases the key, so a deliberate next run is new.
        scenario.update(unknown=False, status="success")
        page.evaluate("loadWarehouseCurrentSyncStatus('public_original')")
        assert not page.locator("button").is_disabled()
        assert page.evaluate("localStorage.getItem(warehouseCurrentSyncStorageKey())") is None
        # New account cannot inherit this account's pending request key.
        assert page.evaluate("localStorage.getItem('warehouse-current-sync-v1:another_user')") is None
        # A definite busy rejection never accepted this key. Clear it, then
        # reload can use the overview and enable a new explicit request.
        page.evaluate("state.warehouses.updateRequestKey='definite_busy_key'; state.warehouses.updateRunId=''; localStorage.setItem(warehouseCurrentSyncStorageKey(),JSON.stringify({request_key:'definite_busy_key',run_id:''})); renderWarehouseCurrentSyncStatus({status:'busy',run_id:'',request_accepted:false,can_start_new:false})")
        assert page.evaluate("localStorage.getItem(warehouseCurrentSyncStorageKey())") is None
        page.reload(); page.add_script_tag(content=client_script())
        page.evaluate("loadWarehouseCurrentSyncStatus()")
        assert "?" not in gets[-1] and not page.locator("button").is_disabled() and len(posts) == 1
        browser.close()
    print("warehouse_durable_identity_browser_check: OK; lost ACK/reload GET-only, key-before-POST, six pending states, bounded polling, unknown stop")


if __name__ == "__main__":
    main()
