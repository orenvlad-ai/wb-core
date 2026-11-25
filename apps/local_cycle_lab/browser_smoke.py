"""macOS acceptance: real sandbox, native form, restart, stage and archived reset."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.local_cycle_lab.sandbox import ROOT
from apps.local_cycle_lab.state import LabState, read_json


def wait_for(check, timeout=60):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        result = check()
        if result:
            return result
        time.sleep(.1)
    raise AssertionError("Local app did not reach the expected state")


def main():
    if sys.platform != "darwin":
        print("local_cycle_lab browser: SKIP (real macOS sandbox required)")
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright
    with TemporaryDirectory(prefix="wbc-local-browser-") as temporary:
        root = Path(temporary).resolve()
        state = root / "state"
        output = args.output or root / "evidence"
        output.mkdir(parents=True, exist_ok=True)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        process = None
        log = (output / "server.log").open("w")

        def start():
            nonlocal process
            old_pid = read_json(state / "control/ready.json")["pid"] if (state / "control/ready.json").exists() else None
            process = subprocess.Popen([str(args.python.absolute()), "-m", "apps.local_cycle_lab.local", "serve",
                "--state", str(state), "--port", str(port)], cwd=ROOT, close_fds=True, stdout=log, stderr=log)
            def ready():
                if process.poll() is not None:
                    raise AssertionError("Local app failed; see " + str(output / "server.log"))
                path = state / "control/ready.json"
                return path.exists() and read_json(path)["pid"] != old_pid
            wait_for(ready)
            return read_json(state / "open.json")["url"]

        def stop():
            nonlocal process
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
            process = None

        try:
            url = start()
            isolation = read_json(state / "control/isolation.json")
            assert len(isolation["parent"]) == len(isolation["child"]) == 7, isolation
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                context = browser.new_context(viewport={"width":1440, "height":1000})
                context.set_default_timeout(8000)
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(url)
                assert page.title() == "Тестовая WBC"
                native = page.frame_locator("#wbc")
                native.locator('[data-unified-tab-button="warehouses"]').click()
                native.locator('[data-warehouse-key="ff"]').click()
                native.locator('[data-ff-pool-open]').click()
                native.locator('[data-ff-pool-tab="create"]').click()
                native.locator('[data-ff-pool-action-kind]').select_option("pool_overhead")
                native.locator('[data-ff-pool-facility]').select_option(index=1)
                native.locator('[data-ff-pool-scope]').select_option("FBS")
                native.locator('[data-ff-pool-overhead-category]').select_option("other")
                native.locator('[data-ff-pool-overhead-comment]').fill("Synthetic persistent manual expense")
                native.locator('[data-ff-pool-amount]').fill("123.45")
                native.locator('[data-ff-pool-preview]').click()
                native.get_by_role("button", name="Подтвердить проведение", exact=True).click()
                receipt = native.locator('[data-ff-operation-receipt]')
                receipt.wait_for(state="visible")
                identity = receipt.get_attribute("data-ff-operation-receipt")
                operation = "/v1/sheet-vitrina-v1/operations/" + identity
                before = page.evaluate("async path=>(await(await fetch(path)).json()).operation", operation)
                assert before["summary"]["amount_rub"] == "123.45" and before["state"] == "accepted"
                assert before["physical_applied"] is False and before["document"] is None
                page.screenshot(path=str(output / "manual-expense.png"), full_page=True)
                stop()
                page.goto(start())
                after = page.evaluate("async path=>(await(await fetch(path)).json()).operation", operation)
                assert after == before, "native source receipt changed after restart"

                # Do not rely on the native button's disabled state: repeat the
                # exact confirmation identity through its real guarded route.
                repeated = page.evaluate("""async id=>{const r=await fetch('/v1/sheet-vitrina-v1/warehouses/ff/facility-pools/requests/'+id+'/confirm',
                  {method:'POST',headers:{'Content-Type':'application/json','X-WB-FF-Pool-CSRF':'1'},body:JSON.stringify({confirm:true})});return {status:r.status,body:await r.json()};}""", identity)
                assert repeated["status"] == 200, repeated
                page.locator("#run").click()
                blocked = page.evaluate("""async()=>{const r=await fetch('/v1/sheet-vitrina-v1/warehouses/ff/facility-pools/documents/overhead/preview',
                    {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});return {status:r.status,body:await r.json()};}""")
                assert blocked["status"] == 409 and "Дождитесь" in blocked["body"]["error"], blocked
                wait_for(lambda: page.evaluate("async()=>{const d=await(await fetch('/lab/status')).json();return !d.busy&&d.attempts.length&&d;}"))
                stage = page.evaluate("async()=>await(await fetch('/lab/status')).json()")
                assert stage["attempts"][-1]["status"] == "passed", stage
                assert stage["attempts"][-1]["code_sha256"]
                assert page.evaluate("async path=>(await(await fetch(path)).json()).operation", operation) == before
                for path in ("/v1/sheet-vitrina-v1/web-vitrina/group-refresh", "/v1/sheet-vitrina-v1/health/recovery/start"):
                    assert page.evaluate("async path=>(await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).status", path) == 409

                # Kill only our disposable supervisor. The actual app must exit
                # on parent death and never leave an unlocked live writer.
                app_pid = read_json(state / "control/ready.json")["pid"]
                process.kill(); process.wait(timeout=5)
                def unlocked():
                    try:
                        with LabState(state).writer():
                            return True
                    except ValueError:
                        return False
                wait_for(unlocked, timeout=10)
                with socket.socket() as sock:
                    assert sock.connect_ex(("127.0.0.1", port)) != 0, "orphan app remained live"
                page.goto(start())
                assert page.evaluate("async path=>(await(await fetch(path)).json()).operation", operation) == before
                preserved = page.evaluate("async()=>await(await fetch('/lab/status')).json()")
                assert len(preserved["attempts"]) == 1 and preserved["attempts"][0]["status"] == "passed"
                old_generation = page.evaluate("labGeneration")
                page.once("dialog", lambda dialog: dialog.accept())
                with page.expect_response(lambda response: response.url.endswith("/lab/reset") and response.request.method == "POST") as reset:
                    page.locator("#reset").click()
                assert reset.value.status == 202
                page.wait_for_function("old=>typeof labGeneration!=='undefined'&&labGeneration!==old", arg=old_generation, timeout=60000)
                stale = page.evaluate("""async generation=>{const result={};for(const method of ['POST','PATCH','DELETE','PUT']){
                  const r=await nativeFetch('/v1/not-a-route',{method,headers:{'Content-Type':'application/json','X-WBC-Lab-Generation':generation},body:'{}'});result[method]=r.status;}return result;}""", old_generation)
                assert all(status == 409 for status in stale.values()), stale
                assert page.evaluate("async path=>(await fetch(path)).status", operation) == 404
                assert page.evaluate("async()=>await(await fetch('/lab/status')).json()")['attempts'] == []
                archive = Path(read_json(state / "lab.json")["previous_archive"])
                with sqlite3.connect((archive / "runtime/registry_upload_runtime.sqlite3").as_uri() + "?mode=ro", uri=True) as conn:
                    conn.execute("PRAGMA query_only=ON")
                    assert conn.execute("SELECT count(*) FROM sheet_vitrina_v1_ff_pool_overhead_confirmations").fetchone()[0] == 1
                assert not errors, errors
                page.screenshot(path=str(output / "after-reset.png"), full_page=True)
                (output / "acceptance.json").write_text(json.dumps(dict(isolation=isolation,
                    receipt_before=before, receipt_after=after, stage=stage,
                    supervisor_death_recovered=True, reset_archived=True, stale_methods=stale,
                    browser_errors=errors), ensure_ascii=False, indent=2))
                browser.close()
        finally:
            stop()
            log.close()
    print("local_cycle_lab browser: PASS (OS isolation parent+child, native source, restart, idempotent confirmation, stage/checkpoint, freeze, parent death, archived reset, stale methods)")


if __name__ == "__main__":
    main()
