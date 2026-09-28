"""Browser fixture for the in-site buyer login dialog, without WB traffic."""

from __future__ import annotations

from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import threading
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_settings_sources_sessions_browser_smoke import _sources_payload  # noqa: E402
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SETTINGS_UI_PATH, DEFAULT_SOURCES_SESSIONS_PATH,
    DEFAULT_WB_BUYER_RECOVERY_START_PATH, DEFAULT_WB_BUYER_RECOVERY_STATUS_PATH,
    DEFAULT_WB_BUYER_RECOVERY_STOP_PATH, DEFAULT_WB_BUYER_VIEWER_PREFIX,
    DEFAULT_WB_BUYER_RECOVERY_FINISH_PATH, DEFAULT_WB_BUYER_SESSION_CHECK_PATH,
    _render_sheet_vitrina_settings_ui,
)


class Fixture:
    def __init__(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.status = "idle"
        self.price_ok = False
        self.price_checked = False
        self.fail_price_once = False
        self.viewer_reads = 0
        self.run_number = 0
        self.start_requests = 0
        self.finish_requests = 0
        self.fail_start_response_once = False
        self.run_id = "buyer-recovery-20260927T120000Z-00000000"
        self.last_stop_run_id = ""
        self.hold_sources_once = False
        self.sources_entered = threading.Event()
        self.sources_release = threading.Event()
        self.hold_start_once = False
        self.start_entered = threading.Event()
        self.start_release = threading.Event()
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

    def respond(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        parsed = urlsplit(handler.path)
        now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        content_type = "application/json; charset=utf-8"
        if parsed.path == DEFAULT_SETTINGS_UI_PATH:
            body = _render_sheet_vitrina_settings_ui().encode()
            content_type = "text/html; charset=utf-8"
        elif parsed.path == DEFAULT_WB_BUYER_VIEWER_PREFIX + "vnc.html":
            self.viewer_reads += 1
            body = b'<html><body style="font:24px sans-serif;background:#fff;color:#222;padding:50px">Local viewer fixture: real WB page appears here on the server.</body></html>'
            content_type = "text/html; charset=utf-8"
        else:
            if method == "POST" and parsed.path == DEFAULT_WB_BUYER_RECOVERY_START_PATH:
                self.start_requests += 1
                if handler.headers.get("X-WB-Buyer-Viewer-CSRF") != "1":
                    raise AssertionError("buyer start missed CSRF marker")
                if self.hold_start_once:
                    self.hold_start_once = False
                    self.start_entered.set()
                    self.start_release.wait(timeout=5)
                self.run_number += 1
                self.run_id = f"buyer-recovery-20260927T120000Z-{self.run_number:08x}"
                self.status = "awaiting_human"
            elif method == "POST" and parsed.path == DEFAULT_WB_BUYER_RECOVERY_STOP_PATH:
                self.last_stop_run_id = str(json.loads(handler.body.decode()).get("run_id") or "") if getattr(handler, "body", None) else ""
                if self.last_stop_run_id and self.last_stop_run_id != self.run_id:
                    raise AssertionError("UI cancelled a stale buyer run")
                self.status = "stopped"
            elif method == "POST" and parsed.path == DEFAULT_WB_BUYER_RECOVERY_FINISH_PATH:
                assert handler.headers.get("X-WB-Buyer-Viewer-CSRF") == "1"
                assert json.loads(handler.body.decode()).get("run_id") == self.run_id
                self.finish_requests += 1
                self.status = "validating_session"
                threading.Timer(0.5, lambda: setattr(self, "status", "completed")).start()
            if parsed.path == DEFAULT_SOURCES_SESSIONS_PATH:
                payload = _sources_payload(now)
                payload["wb_buyer"]["authorization"] = self.recovery(now)
                payload["wb_buyer"]["capability"] = {
                    "status": "observed" if self.price_ok else "price_unavailable" if self.price_checked else "not_checked",
                    "valid": False,
                    "session_valid": False,
                    "account_confirmed": False,
                    "authenticated_buyer_price": 999 if self.status == "awaiting_human" else 386 if self.price_ok else None,
                    "wallet_price": 139 if self.price_ok else None,
                    "nonwallet_price": 144 if self.price_ok else None,
                    "checked_at": now,
                }
                if self.hold_sources_once:
                    self.hold_sources_once = False
                    self.sources_entered.set()
                    self.sources_release.wait(timeout=15)
            elif parsed.path in {DEFAULT_WB_BUYER_RECOVERY_START_PATH, DEFAULT_WB_BUYER_RECOVERY_STATUS_PATH, DEFAULT_WB_BUYER_RECOVERY_STOP_PATH}:
                payload = self.recovery(now)
            elif parsed.path == DEFAULT_WB_BUYER_RECOVERY_FINISH_PATH:
                payload = self.recovery(now)
            elif parsed.path == DEFAULT_WB_BUYER_SESSION_CHECK_PATH:
                self.price_checked = True
                self.price_ok = not self.fail_price_once
                self.fail_price_once = False
                payload = {"status": "authenticated_surface", "capability_status": "observed" if self.price_ok else "price_unavailable", "capability_valid": False,
                           "price": {"wallet_price": 139 if self.price_ok else None, "nonwallet_price": 144 if self.price_ok else None, "authenticated_buyer_price": None}}
            else:
                payload = {"items": [], "groups": [], "documents": [], "rows": [], "available_sections": [], "status": "ready"}
            body = json.dumps(payload, ensure_ascii=False).encode()
        response_status = 200
        if method == "POST" and parsed.path == DEFAULT_WB_BUYER_RECOVERY_START_PATH and self.fail_start_response_once:
            self.fail_start_response_once = False
            response_status = 504
            content_type = "text/html; charset=utf-8"
            body = b"<html>gateway timeout upstream details</html>"
        handler.send_response(response_status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def recovery(self, now: str) -> dict[str, object]:
        running = self.status in {"awaiting_human", "validating_session"}
        return {
            "run_id": self.run_id,
            "status": self.status, "running": running, "run_is_final": self.status in {"completed", "stopped"},
            "viewer_available": self.status == "awaiting_human",
            "human_action": "Войдите в Wildberries и нажмите «Я вошёл»." if self.status == "awaiting_human" else "",
            "login_confirmed": self.status == "completed",
            "session": {"status": "authenticated_surface" if self.status == "completed" else "missing", "valid": False, "account_confirmed": False, "login_confirmed": self.status == "completed", "checked_at": now},
            "price": {"status": "not_checked"},
        }

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def main() -> None:
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/wbc0081-buyer-ui")
    output.mkdir(parents=True, exist_ok=True)
    with Fixture() as fixture, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(fixture.base + DEFAULT_SETTINGS_UI_PATH + "#sources-sessions")
        page.wait_for_function("() => document.documentElement.dataset.settingsReady === 'true'")
        page.locator('[data-source-recover="buyer"]').click()
        page.wait_for_function("() => !document.querySelector('#buyerViewerFrame').hidden")
        frame_url = page.locator("#buyerViewerFrame").get_attribute("src") or ""
        websocket_path = parse_qs(urlsplit(frame_url).query).get("path", [""])[0]
        if not websocket_path.startswith("v1/") or websocket_path.startswith("/"):
            raise AssertionError(f"noVNC adds the slash itself; path must be relative: {websocket_path}")
        if "Я вошёл" not in page.locator("#buyerViewerStatus").inner_text() or not page.locator("#buyerViewerFinish").is_visible():
            raise AssertionError("manual finish action was not displayed")
        if page.locator("#buyerSourceBadge").inner_text() != "Вход выполняется" or "999" in page.locator("#buyerSourceHealth").inner_text():
            raise AssertionError("active recovery must hide previously cached green capability")
        if page.locator("#buyerLauncherLink").is_visible():
            raise AssertionError("ZIP launcher must be hidden")
        page.screenshot(path=str(output / "buyer-login-dialog.png"), full_page=True)
        page.locator("#buyerViewerReconnect").click()
        page.wait_for_function("() => document.querySelector('#buyerViewerFrame').contentDocument?.body?.innerText.includes('Local viewer fixture')")
        if fixture.viewer_reads < 2:
            raise AssertionError("reconnect must reopen the viewer transport")
        page.locator("#buyerViewerCancel").click()
        page.wait_for_function("() => document.querySelector('#buyerSourceBadge')?.innerText === 'Вход отменён'")
        if page.locator("#buyerViewerDialog").evaluate("node => node.open"):
            raise AssertionError("cancel must close dialog")
        # A stale final summary from run A must not close the modal for run B.
        fixture.hold_sources_once = True
        fixture.sources_release.clear()
        page.locator("#reloadSourcesSessionsButton").click()
        if not fixture.sources_entered.wait(timeout=2):
            raise AssertionError("stale summary request did not enter")
        fixture.hold_start_once = True
        fixture.start_release.clear()
        page.locator('[data-source-recover="buyer"]').click()
        if not fixture.start_entered.wait(timeout=2):
            raise AssertionError("new start request did not enter")
        fixture.sources_release.set()
        page.wait_for_timeout(300)
        if not page.locator("#buyerViewerDialog").evaluate("node => node.open"):
            raise AssertionError("stale final response closed the pending new dialog")
        fixture.start_release.set()
        page.wait_for_function("() => !document.querySelector('#buyerViewerFrame').hidden")
        if fixture.run_id not in (page.locator("#buyerViewerFrame").get_attribute("src") or ""):
            raise AssertionError("new run did not receive its own viewer")
        page.locator("#buyerViewerCancel").click()
        page.wait_for_function("() => document.querySelector('#buyerSourceBadge')?.innerText === 'Вход отменён'")
        if fixture.last_stop_run_id != fixture.run_id:
            raise AssertionError("cancel must target run B, not stale run A")
        # Cancel during an in-flight start must wait for the new run identity.
        fixture.start_entered.clear()
        fixture.hold_start_once = True
        fixture.start_release.clear()
        page.locator('[data-source-recover="buyer"]').click()
        if not fixture.start_entered.wait(timeout=2):
            raise AssertionError("pending start request did not enter")
        page.locator("#buyerViewerCancel").click()
        fixture.start_release.set()
        page.wait_for_function("() => document.querySelector('#buyerSourceBadge')?.innerText === 'Вход отменён'")
        if fixture.last_stop_run_id != fixture.run_id:
            raise AssertionError("pending cancel used the previous run identity")
        # Ambiguous 504 after the server starts a run: GET readback restores
        # the viewer without issuing a second POST or exposing gateway HTML.
        fixture.fail_start_response_once = True
        starts_before = fixture.start_requests
        page.locator('[data-source-recover="buyer"]').click()
        page.wait_for_function("() => !document.querySelector('#buyerViewerFrame').hidden")
        if fixture.start_requests != starts_before + 1 or "gateway timeout" in page.locator("#buyerViewerStatus").inner_text():
            raise AssertionError("ambiguous start must use readback, not repeat POST or show raw 504")
        page.locator("#buyerViewerCancel").click()
        page.wait_for_function("() => document.querySelector('#buyerSourceBadge')?.innerText === 'Вход отменён'")
        # A terminal poll may still be waiting on the slow summary when a new
        # run begins. It must release its poll generation and leave the new
        # iframe alone when that old summary finally returns.
        page.locator('[data-source-recover="buyer"]').click()
        page.wait_for_function("() => !document.querySelector('#buyerViewerFrame').hidden")
        old_run_id = fixture.run_id
        fixture.hold_sources_once = True
        fixture.sources_entered.clear()
        fixture.sources_release.clear()
        fixture.status = "completed"
        fixture.price_ok = False
        if not fixture.sources_entered.wait(timeout=8):
            raise AssertionError("terminal summary request did not enter")
        page.locator('[data-source-recover="buyer"]').click()
        page.wait_for_function("() => document.querySelector('#buyerViewerDialog').open")
        page.wait_for_function("() => !document.querySelector('#buyerViewerFrame').hidden")
        if fixture.run_id == old_run_id:
            raise AssertionError("new run was not created during slow terminal summary")
        fixture.sources_release.set()
        page.wait_for_timeout(200)
        if not page.locator("#buyerViewerDialog").evaluate("node => node.open"):
            raise AssertionError("old terminal summary closed the new run dialog")
        fixture.status = "completed"
        fixture.price_ok = False
        page.wait_for_function("() => document.querySelector('#buyerSourceBadge')?.innerText === 'Вход подтверждён'", timeout=10000)
        if "Не проверена" not in page.locator("#buyerSourceHealth").inner_text() or page.locator('[data-source-check="buyer"]').is_disabled():
            raise AssertionError("confirmed login must not imply a price capability")
        page.locator('[data-source-recover="buyer"]').click()
        page.wait_for_function("() => document.querySelector('#buyerViewerDialog').open")
        page.wait_for_function("() => !document.querySelector('#buyerViewerFinish').hidden")
        page.locator("#buyerViewerFinish").click()
        page.wait_for_function("() => document.querySelector('#buyerSourceBadge')?.innerText === 'Вход подтверждён'", timeout=10000)
        if fixture.finish_requests != 1:
            raise AssertionError("one manual finish must send one request")
        fixture.hold_sources_once = True
        fixture.sources_entered.clear()
        fixture.sources_release.clear()
        page.locator("#reloadSourcesSessionsButton").click()
        if not fixture.sources_entered.wait(timeout=3):
            raise AssertionError("pre-check summary did not enter")
        page.locator('[data-source-check="buyer"]').click()
        page.wait_for_function("() => document.querySelector('#buyerSourceHealth')?.innerText.includes('139 ₽ с WB Кошельком')")
        fixture.sources_release.set()
        page.wait_for_timeout(300)
        if "144 ₽ без WB Кошелька" not in page.locator("#buyerSourceHealth").inner_text():
            raise AssertionError("wallet and nonwallet prices must remain distinct")
        if page.locator("#buyerSourceBadge").inner_text() != "Вход подтверждён":
            raise AssertionError("stale pre-check summary must not revoke saved login")
        fixture.fail_price_once = True
        page.locator('[data-source-check="buyer"]').click()
        page.wait_for_function("() => document.querySelector('#buyerSourceHealth')?.innerText.includes('Цена недоступна')")
        if page.locator("#buyerSourceBadge").inner_text() != "Вход подтверждён":
            raise AssertionError("unavailable price must not revoke saved login")
        page.screenshot(path=str(output / "buyer-ready-card.png"), full_page=True)
        if errors:
            raise AssertionError(f"browser JavaScript errors: {errors}")
        browser.close()
    print(f"wb_buyer_viewer_ui_smoke: OK; screenshots={output}")


if __name__ == "__main__":
    main()
