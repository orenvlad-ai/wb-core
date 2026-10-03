"""Browser fixture for the Seller Portal login dialog, without WB traffic."""

from __future__ import annotations

from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import threading
from urllib.parse import parse_qs, urlsplit
from urllib import request as urllib_request

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_settings_sources_sessions_browser_smoke import _sources_payload  # noqa: E402
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SETTINGS_UI_PATH, DEFAULT_SOURCES_SESSIONS_PATH,
    DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH, DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH,
    DEFAULT_SELLER_PORTAL_RECOVERY_STOP_PATH, DEFAULT_SELLER_PORTAL_RECOVERY_FINISH_PATH,
    DEFAULT_SELLER_PORTAL_VIEWER_PREFIX, _render_sheet_vitrina_settings_ui,
)


class Fixture:
    def __init__(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.status = "idle"
        self.run_number = 0
        self.run_id = ""
        self.request_id = ""
        self.cancel_intents: set[str] = set()
        self.starts = 0
        self.finishes = 0
        self.stops: list[str] = []
        self.viewer_reads = 0
        self.fail_verification_once = False
        self.not_needed_next = False
        self.block_start_response = False
        self.start_published = threading.Event()
        self.stop_received = threading.Event()
        self.release_start = threading.Event()
        self.server = ThreadingHTTPServer(("127.0.0.1", port), self.handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def handler(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                fixture.respond(self, "GET")

            def do_POST(self):  # noqa: N802
                size = int(self.headers.get("Content-Length") or 0)
                self.body = self.rfile.read(size) if size else b""
                fixture.respond(self, "POST")

            def log_message(self, *_args):
                return

        return Handler

    def recovery(self) -> dict:
        return {
            "run_id": self.run_id, "status": self.status, "run_status": self.status,
            "request_id": self.request_id,
            "running": self.status in {"starting", "awaiting_login", "validating_session"},
            "run_is_final": self.status in {"completed", "not_needed", "stopped", "error"},
            "viewer_owned": self.status in {"starting", "awaiting_login", "validating_session"},
            "viewer_available": self.status == "awaiting_login",
            "summary": "Вход проверяется" if self.status == "validating_session" else "",
        }

    def respond(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        path = urlsplit(handler.path).path
        content_type = "application/json; charset=utf-8"
        if path == DEFAULT_SETTINGS_UI_PATH:
            body = _render_sheet_vitrina_settings_ui().encode()
            content_type = "text/html; charset=utf-8"
        elif path == DEFAULT_SELLER_PORTAL_VIEWER_PREFIX + "vnc.html":
            self.viewer_reads += 1
            body = b'<html><body style="font:24px sans-serif;background:#fff;color:#222;padding:50px">Local Seller Portal viewer fixture. No Wildberries account is connected.</body></html>'
            content_type = "text/html; charset=utf-8"
        else:
            if method == "POST" and path == DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH:
                assert handler.headers.get("X-Seller-Viewer-CSRF") == "1"
                request_id = str(json.loads(handler.body.decode()).get("request_id") or "")
                assert len(request_id) == 32
                self.starts += 1
                if request_id in self.cancel_intents:
                    self.cancel_intents.remove(request_id)
                    self.status = "stopped"
                elif self.status not in {"starting", "awaiting_login", "validating_session"}:
                    self.run_number += 1
                    self.run_id = f"seller-recovery-20261003T090000Z-{self.run_number:08x}"
                    self.request_id = request_id
                    if self.not_needed_next:
                        self.status = "not_needed"
                        self.not_needed_next = False
                    else:
                        self.status = "starting"
                        if not self.block_start_response:
                            threading.Timer(0.35, lambda: setattr(self, "status", "awaiting_login") if self.status == "starting" else None).start()
                if self.block_start_response:
                    self.start_published.set()
                    assert self.release_start.wait(timeout=5), "fixture start was never released"
                    if self.status == "starting":
                        self.status = "awaiting_login"
            elif method == "POST" and path == DEFAULT_SELLER_PORTAL_RECOVERY_FINISH_PATH:
                assert handler.headers.get("X-Seller-Viewer-CSRF") == "1"
                assert json.loads(handler.body.decode()).get("run_id") == self.run_id
                self.finishes += 1
                self.status = "validating_session"
                if self.fail_verification_once:
                    self.fail_verification_once = False
                    threading.Timer(0.5, lambda: setattr(self, "status", "awaiting_login")).start()
                else:
                    threading.Timer(0.5, lambda: setattr(self, "status", "completed")).start()
            elif method == "POST" and path == DEFAULT_SELLER_PORTAL_RECOVERY_STOP_PATH:
                assert handler.headers.get("X-Seller-Viewer-CSRF") == "1"
                stop_payload = json.loads(handler.body.decode())
                requested = str(stop_payload.get("run_id") or "")
                requested_id = str(stop_payload.get("request_id") or "")
                if requested_id and requested_id != self.request_id:
                    self.cancel_intents.add(requested_id)
                    payload = {"run_id": "", "run_status": "stopped", "status": "stopped", "running": False,
                               "run_failure_code": "cancel_pending_start"}
                    body = json.dumps(payload, ensure_ascii=False).encode()
                    handler.send_response(200)
                    handler.send_header("Content-Type", content_type)
                    handler.send_header("Content-Length", str(len(body)))
                    handler.end_headers()
                    handler.wfile.write(body)
                    return
                assert requested == self.run_id or requested_id == self.request_id, "UI cancelled a stale run"
                if self.block_start_response:
                    self.stop_received.set()
                    assert self.release_start.wait(timeout=5), "fixture stop was never released"
                self.stops.append(requested)
                self.status = "stopped"

            if path == DEFAULT_SOURCES_SESSIONS_PATH:
                now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                payload = _sources_payload(now)
                payload["seller_portal"]["authorization"] = {
                    **payload["seller_portal"]["authorization"], **self.recovery(),
                    "session_status": "session_valid_canonical" if self.status in {"idle", "not_needed", "completed"} else "session_invalid",
                    "organization_confirmed": self.status in {"idle", "not_needed", "completed"},
                }
            elif path in {
                DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH, DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH,
                DEFAULT_SELLER_PORTAL_RECOVERY_STOP_PATH, DEFAULT_SELLER_PORTAL_RECOVERY_FINISH_PATH,
            }:
                payload = self.recovery()
            else:
                payload = {"status": "ready", "items": [], "groups": [], "rows": []}
            body = json.dumps(payload, ensure_ascii=False).encode()
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def main() -> None:
    if "--serve" in sys.argv[1:]:
        with Fixture() as fixture:
            print(f"Seller fixture preview: {fixture.base}{DEFAULT_SETTINGS_UI_PATH}#sources-sessions", flush=True)
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                pass
        return
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/wbc0126-seller-ui")
    output.mkdir(parents=True, exist_ok=True)
    with Fixture() as fixture, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(fixture.base + DEFAULT_SETTINGS_UI_PATH + "#sources-sessions")
        page.wait_for_function("() => document.documentElement.dataset.settingsReady === 'true'")
        page.locator('[data-source-recover="seller"]').click()
        page.wait_for_function("() => !document.querySelector('#sellerViewerFrame').hidden")
        frame_url = page.locator("#sellerViewerFrame").get_attribute("src") or ""
        websocket_path = parse_qs(urlsplit(frame_url).query).get("path", [""])[0]
        assert websocket_path.startswith("v1/") and not websocket_path.startswith("/")
        assert page.locator("#sellerViewerFinish").is_visible()
        assert not page.locator("#sellerLauncherLink").is_visible()
        page.screenshot(path=str(output / "seller-login-dialog.png"), full_page=True)
        page.locator("#sellerViewerReconnect").click()
        page.wait_for_function("() => document.querySelector('#sellerViewerFrame').contentDocument?.body?.innerText.includes('Local Seller Portal viewer fixture')")
        assert fixture.viewer_reads >= 2

        page.locator("#sellerViewerClose").click()
        assert not page.locator("#sellerViewerDialog").evaluate("node => node.open")
        page.locator('[data-source-recover="seller"]').click()
        page.wait_for_function("() => !document.querySelector('#sellerViewerFrame').hidden")
        assert fixture.starts == 2 and fixture.run_number == 1, "continue must rejoin existing run"
        page.locator("#sellerViewerFinish").click()
        page.wait_for_function("() => document.querySelector('#sellerSourceBadge')?.innerText === 'Сессия активна'", timeout=10000)
        assert fixture.finishes == 1
        assert not page.locator("#sellerViewerFrame").is_visible()
        page.screenshot(path=str(output / "seller-login-confirmed.png"), full_page=True)
        page.locator("#sellerViewerClose").click()

        fixture.not_needed_next = True
        page.locator('[data-source-recover="seller"]').click()
        page.wait_for_function("() => document.querySelector('#sellerViewerStatus')?.innerText.includes('уже активна')")
        assert page.locator("#sellerViewerFrame").is_hidden()
        page.locator("#sellerViewerClose").click()

        page.locator('[data-source-recover="seller"]').click()
        page.wait_for_function("() => !document.querySelector('#sellerViewerFrame').hidden")
        fixture.fail_verification_once = True
        page.locator("#sellerViewerFinish").click()
        page.wait_for_timeout(900)
        assert page.locator("#sellerSourceBadge").inner_text() != "Сессия активна", "failed independent probe cannot mark success"
        page.locator("#sellerViewerCancel").click()
        page.wait_for_function("() => document.querySelector('#sellerSourceBadge')?.innerText === 'Вход отменён'", timeout=10000)
        assert fixture.stops[-1] == fixture.run_id
        assert not page.locator("#sellerViewerDialog").evaluate("node => node.open") or page.locator("#sellerViewerFrame").is_hidden()
        if page.locator("#sellerViewerDialog").evaluate("node => node.open"):
            page.locator("#sellerViewerClose").click()

        # The browser loses the start response after the server created a run.
        # Cancel must read back only the owned run and stop that exact run.
        starts_before_lost_response = fixture.starts
        stops_before_lost_response = len(fixture.stops)
        def lose_start_response(route):
            route.fetch()
            route.abort("failed")
        page.route("**" + DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH, lose_start_response)
        page.locator('[data-source-recover="seller"]').click()
        page.wait_for_timeout(150)
        page.locator("#sellerViewerCancel").click()
        page.wait_for_function("() => document.querySelector('#sellerSourceBadge')?.innerText === 'Вход отменён'", timeout=10000)
        assert fixture.starts == starts_before_lost_response + 1, "lost response must not retry start"
        assert len(fixture.stops) == stops_before_lost_response + 1
        assert fixture.stops[-1] == fixture.run_id
        page.unroute("**" + DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH)

        # The server publishes an owned pending run, then blocks in its start
        # probe while the browser loses the response. Cancellation must wait
        # for that start and leave no active run after it is released.
        if page.locator("#sellerViewerDialog").evaluate("node => node.open"):
            page.locator("#sellerViewerClose").click()
        fixture.block_start_response = True
        fixture.start_published.clear()
        fixture.stop_received.clear()
        fixture.release_start.clear()
        starts_before_pending = fixture.starts
        stops_before_pending = len(fixture.stops)
        remote_results: list[str] = []
        def lose_pending_response(route):
            request_body = (route.request.post_data or "{}").encode()
            def server_start():
                try:
                    req = urllib_request.Request(
                        fixture.base + DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH,
                        data=request_body,
                        headers={"Content-Type": "application/json", "X-Seller-Viewer-CSRF": "1"},
                        method="POST",
                    )
                    with urllib_request.urlopen(req, timeout=8) as response:
                        response.read()
                    remote_results.append("finished")
                except Exception as exc:
                    remote_results.append(type(exc).__name__)
            threading.Thread(target=server_start, daemon=True).start()
            assert fixture.start_published.wait(timeout=2)
            route.abort("failed")
        page.route("**" + DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH, lose_pending_response)
        page.locator('[data-source-recover="seller"]').click()
        page.locator("#sellerViewerCancel").click()
        assert fixture.stop_received.wait(timeout=3), "pending owned run was not cancelled"
        fixture.release_start.set()
        page.wait_for_function("() => document.querySelector('#sellerSourceBadge')?.innerText === 'Вход отменён'", timeout=10000)
        assert fixture.starts == starts_before_pending + 1 and len(fixture.stops) == stops_before_pending + 1
        assert fixture.status == "stopped" and fixture.stops[-1] == fixture.run_id
        page.unroute("**" + DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH)

        # The cancellation may reach the control listener before the start
        # listener receives its request. Its nonce must veto that late start.
        if page.locator("#sellerViewerDialog").evaluate("node => node.open"):
            page.locator("#sellerViewerClose").click()
        fixture.block_start_response = False
        delayed_bodies: list[bytes] = []
        def delay_start_until_after_cancel(route):
            delayed_bodies.append((route.request.post_data or "{}").encode())
            route.abort("failed")
        page.route("**" + DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH, delay_start_until_after_cancel)
        run_number_before = fixture.run_number
        page.locator('[data-source-recover="seller"]').click()
        page.locator("#sellerViewerCancel").click()
        page.wait_for_function("() => !document.querySelector('#sellerViewerDialog').open", timeout=5000)
        assert len(delayed_bodies) == 1
        late_request = urllib_request.Request(
            fixture.base + DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH,
            data=delayed_bodies[0],
            headers={"Content-Type": "application/json", "X-Seller-Viewer-CSRF": "1"},
            method="POST",
        )
        with urllib_request.urlopen(late_request, timeout=5) as response:
            late_payload = json.load(response)
        assert late_payload["run_status"] == "stopped" and fixture.run_number == run_number_before
        assert fixture.status == "stopped"
        page.unroute("**" + DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH)
        if errors:
            raise AssertionError(f"browser JavaScript errors: {errors}")
        browser.close()
    print(f"seller_portal_viewer_ui_smoke: OK; screenshots={output}")


if __name__ == "__main__":
    main()
