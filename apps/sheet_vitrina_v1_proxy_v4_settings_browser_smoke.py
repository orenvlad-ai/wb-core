"""Browser smoke for separate V3/V4 calculation-parameters Settings blocks."""

from __future__ import annotations

from contextlib import AbstractContextManager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_CALCULATION_PARAMETERS_PATH,
    DEFAULT_PROXY_V4_PARAMETERS_PATH,
    DEFAULT_PROXY_V4_PARAMETERS_PREVIEW_PATH,
    DEFAULT_SETTINGS_UI_PATH,
    _render_sheet_vitrina_settings_ui,
)


class _Server(AbstractContextManager):
    def __init__(self) -> None:
        self.bodies: list[tuple[str, dict[str, object]]] = []
        self.saved = False
        self.server = ThreadingHTTPServer(("127.0.0.1", _free_port()), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def _handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                owner._respond(self, "GET", {})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                owner._respond(self, "POST", payload)

            def log_message(self, *_args: object) -> None:
                return

        return Handler

    def _respond(self, handler: BaseHTTPRequestHandler, method: str, body: dict[str, object]) -> None:
        path = urlparse(handler.path).path
        if path == DEFAULT_SETTINGS_UI_PATH:
            raw = _render_sheet_vitrina_settings_ui().encode("utf-8")
            handler.send_response(200)
            handler.send_header("Content-Type", "text/html; charset=utf-8")
            handler.send_header("Content-Length", str(len(raw)))
            handler.end_headers()
            handler.wfile.write(raw)
            return
        if method == "POST":
            self.bodies.append((path, body))
        if path == DEFAULT_CALCULATION_PARAMETERS_PATH:
            payload = _v3_payload()
        elif path == DEFAULT_PROXY_V4_PARAMETERS_PREVIEW_PATH:
            payload = {
                "status": "preview_ready",
                "effective_date": "2026-08-09",
                "before_tax_rate_pct": "6",
                "after_tax_rate_pct": "7",
                "changed": True,
                "preview_fingerprint": "sha256:test-preview",
            }
        elif path == DEFAULT_PROXY_V4_PARAMETERS_PATH:
            if method == "POST":
                self.saved = True
            payload = _v4_payload(saved=self.saved)
        else:
            payload = {
                "items": [],
                "groups": [],
                "documents": [],
                "rows": [],
                "available_sections": [],
                "status": "ready",
            }
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    def __enter__(self) -> "_Server":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _v3_payload() -> dict[str, object]:
    parameters = {
        "buyout_rate": "0.91",
        "tax_rate": "0.06",
        "wb_agent_and_other_rate": "0.38",
        "acquiring_rate": "0",
        "wb_logistics_rate": "0",
        "wb_storage_rate": "0",
        "penalties_adjustments_rate": "0",
        "other_expense_rate": "0",
        "included_expense_rate_pct": "44",
        "retained_share_pct": "56",
        "buyout_rate_pct": "91",
    }
    return {
        "status": "ready",
        "current": {
            "version_id": "calculation_parameters_proxy_v1_20260701",
            "effective_date": "2026-07-01",
            "parameters": parameters,
            "created_by": "fixture",
            "created_at": "2026-07-01T00:00:00Z",
        },
        "history": [],
        "reference": {"status": "unavailable", "weeks": [], "rows": []},
    }


def _v4_payload(*, saved: bool) -> dict[str, object]:
    tax = "0.07" if saved else "0.06"
    version = "proxy_v4_v3_20260809" if saved else "proxy_v4_v2_20260808"
    parameters = {
        "buyout_rate": "0.9622",
        "tax_rate": tax,
        "agent_remuneration_rate": "0.121",
        "acquiring_rate": "0.018",
        "wb_logistics_rate": "0.031",
        "wb_storage_rate": "0.006",
        "penalties_adjustments_rate": "0.004",
        "other_expense_rate": "0.011",
        "included_expense_rate_pct": "25.1" if saved else "24.1",
        "retained_share_pct": "74.9" if saved else "75.9",
        "buyout_rate_pct": "96.22",
        "tax_rate_pct": "7" if saved else "6",
        "source_window_from": "2026-07-13",
        "source_window_to": "2026-08-02",
    }
    current = {
        "version_id": version,
        "effective_date": "2026-08-09" if saved else "2026-08-08",
        "version_kind": "operator_tax" if saved else "historical_initialization",
        "created_at": "2026-08-09T08:00:00Z" if saved else "2026-08-08T00:00:00Z",
        "parameters": parameters,
    }
    return {
        "status": "ready",
        "status_message": "Действует последняя подтверждённая immutable V4 version.",
        "fixed_boundary": "2026-08-01",
        "current": current,
        "history": [current],
        "aligned_window": {
            "source_window_from": "2026-07-13",
            "source_window_to": "2026-08-02",
            "status": "ready",
        },
    }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> None:
    with _Server() as server, TemporaryDirectory(prefix="proxy-v4-settings-browser-") as temp_dir:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 1200})
            page_errors: list[str] = []
            console_errors: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on(
                "console",
                lambda message: console_errors.append(message.text)
                if message.type == "error"
                else None,
            )
            response = page.goto(
                server.base_url + DEFAULT_SETTINGS_UI_PATH + "#user-directory",
                wait_until="domcontentloaded",
            )
            if response is None or response.status != 200:
                raise AssertionError("Settings V4 page did not return HTTP 200")
            page.wait_for_function("() => document.documentElement.dataset.settingsReady === 'true'")
            page.wait_for_function("() => document.querySelector('#proxyV4TaxRate')?.value === '6'")
            panel = page.locator('[data-settings-group-panel="user-directory"]')
            text = panel.inner_text()
            for token in (
                "Proxy прибыль и маржинальность · V3",
                "Proxy прибыль и маржинальность · V4",
                "Агентское вознаграждение WB",
                "2026-07-13 — 2026-08-02",
                "Действует последняя подтверждённая immutable V4 version.",
            ):
                if token not in text:
                    raise AssertionError(f"Settings V4 visible token missing: {token}")
            if page.locator("#proxyV4ParametersForm input[type=date]").count():
                raise AssertionError("V4 UI must not expose a manual effective date")
            if not page.locator('[data-proxy-v4-rate="buyout_rate"]').is_disabled() and page.locator('[data-proxy-v4-rate="buyout_rate"]').get_attribute("readonly") is None:
                raise AssertionError("V4 automatic fields must be read-only")
            if page.locator("#calculationEffectiveDate").count() != 1:
                raise AssertionError("V3 manual effective-date/history behavior must remain present")

            page.locator("#proxyV4TaxRate").fill("7")
            page.locator("#previewProxyV4TaxButton").click()
            page.wait_for_function("() => !document.querySelector('#saveProxyV4TaxButton')?.disabled")
            page.locator("#saveProxyV4TaxButton").click()
            page.wait_for_function("() => document.querySelector('#proxyV4TaxRate')?.value === '7'")
            v4_posts = [body for path, body in server.bodies if path in {DEFAULT_PROXY_V4_PARAMETERS_PATH, DEFAULT_PROXY_V4_PARAMETERS_PREVIEW_PATH}]
            if len(v4_posts) != 2 or any("effective_date" in body for body in v4_posts):
                raise AssertionError(f"V4 browser must never submit manual effective_date: {v4_posts}")
            if any(set(body) - {"tax_rate", "preview_fingerprint"} for body in v4_posts):
                raise AssertionError(f"V4 browser submitted non-tax editable fields: {v4_posts}")
            page.screenshot(path=str(Path(temp_dir) / "proxy-v4-settings.png"), full_page=True)
            if page_errors or console_errors:
                raise AssertionError(f"browser errors: page={page_errors} console={console_errors}")
            browser.close()
    print("proxy_v4_settings_v3_v4_blocks: ok")
    print("proxy_v4_settings_tax_auto_effective_date: ok")
    print("proxy_v4_settings_rates_source_status_no_errors: ok")


if __name__ == "__main__":
    main()
