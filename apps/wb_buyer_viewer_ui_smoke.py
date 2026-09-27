"""Browser fixture for the in-site buyer login dialog, without WB traffic."""

from __future__ import annotations

from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import threading
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_settings_sources_sessions_browser_smoke import _sources_payload  # noqa: E402
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SETTINGS_UI_PATH, DEFAULT_SOURCES_SESSIONS_PATH,
    DEFAULT_WB_BUYER_RECOVERY_START_PATH, DEFAULT_WB_BUYER_RECOVERY_STATUS_PATH,
    DEFAULT_WB_BUYER_RECOVERY_STOP_PATH, DEFAULT_WB_BUYER_VIEWER_PREFIX,
    _render_sheet_vitrina_settings_ui,
)


class Fixture:
    def __init__(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.status = "idle"
        self.price_ok = False
        self.viewer_reads = 0
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
                if size:
                    self.rfile.read(size)
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
                if handler.headers.get("X-WB-Buyer-Viewer-CSRF") != "1":
                    raise AssertionError("buyer start missed CSRF marker")
                self.status = "awaiting_human"
            elif method == "POST" and parsed.path == DEFAULT_WB_BUYER_RECOVERY_STOP_PATH:
                self.status = "stopped"
            if parsed.path == DEFAULT_SOURCES_SESSIONS_PATH:
                payload = _sources_payload(now)
                payload["wb_buyer"]["authorization"] = self.recovery(now)
                payload["wb_buyer"]["capability"] = {
                    "status": "available" if self.status == "awaiting_human" or self.status == "completed" and self.price_ok else "price_unavailable",
                    "valid": self.status == "awaiting_human" or self.status == "completed" and self.price_ok,
                    "session_valid": self.status == "awaiting_human",
                    "account_confirmed": self.status == "awaiting_human",
                    "authenticated_buyer_price": 999 if self.status == "awaiting_human" else 386 if self.price_ok else None,
                    "checked_at": now,
                }
            elif parsed.path in {DEFAULT_WB_BUYER_RECOVERY_START_PATH, DEFAULT_WB_BUYER_RECOVERY_STATUS_PATH, DEFAULT_WB_BUYER_RECOVERY_STOP_PATH}:
                payload = self.recovery(now)
            else:
                payload = {"items": [], "groups": [], "documents": [], "rows": [], "available_sections": [], "status": "ready"}
            body = json.dumps(payload, ensure_ascii=False).encode()
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def recovery(self, now: str) -> dict[str, object]:
        running = self.status == "awaiting_human"
        return {
            "run_id": "buyer-recovery-20260927T120000Z-aabbccdd",
            "status": self.status, "running": running, "run_is_final": self.status in {"completed", "stopped"},
            "human_action": "Введите SMS-код на странице Wildberries." if running else "",
            "session": {"status": "valid" if self.status == "completed" else "missing", "valid": self.status == "completed", "account_confirmed": self.status == "completed", "checked_at": now},
            "price": {"status": "ok" if self.price_ok else "price_missing"},
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
        if "Введите SMS-код" not in page.locator("#buyerViewerStatus").inner_text():
            raise AssertionError("human action was not displayed")
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
        page.locator('[data-source-recover="buyer"]').click()
        page.wait_for_function("() => document.querySelector('#buyerViewerDialog').open")
        fixture.status = "completed"
        fixture.price_ok = False
        page.wait_for_function("() => document.querySelector('#buyerSourceBadge')?.innerText.includes('цена недоступна')", timeout=10000)
        if "без SMS" not in page.locator("#buyerSourceError").inner_text():
            raise AssertionError("missing price must not request login again")
        page.locator('[data-source-recover="buyer"]').click()
        page.wait_for_function("() => document.querySelector('#buyerViewerDialog').open")
        fixture.status = "completed"
        fixture.price_ok = True
        page.wait_for_function("() => document.querySelector('#buyerSourceBadge')?.innerText === 'Готов к сбору цен'", timeout=10000)
        page.screenshot(path=str(output / "buyer-ready-card.png"), full_page=True)
        if errors:
            raise AssertionError(f"browser JavaScript errors: {errors}")
        browser.close()
    print(f"wb_buyer_viewer_ui_smoke: OK; screenshots={output}")


if __name__ == "__main__":
    main()
