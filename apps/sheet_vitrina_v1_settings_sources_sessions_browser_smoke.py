"""Browser smoke for centralized Settings → Sources and sessions."""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
LOGIN_CHECKED_AT = "2026-09-28T12:00:00Z"
PRICE_MEASURED_AT = "2026-09-28T12:35:42Z"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH,
    DEFAULT_SELLER_PORTAL_SESSION_CHECK_PATH,
    DEFAULT_SETTINGS_UI_PATH,
    DEFAULT_SOURCES_SESSIONS_PATH,
    DEFAULT_SPP_PROXY_SOURCE_CHECK_PATH,
    DEFAULT_WB_BUYER_RECOVERY_START_PATH,
    DEFAULT_WB_BUYER_SESSION_CHECK_PATH,
    DEFAULT_WB_SUPPLIES_TRANSIT_COST_CHECK_PATH,
    DEFAULT_WB_SUPPLIES_TRANSIT_COST_STATUS_PATH,
    _render_sheet_vitrina_settings_ui,
)


class _SettingsServer(AbstractContextManager):
    def __init__(self) -> None:
        self.calls: dict[tuple[str, str], int] = {}
        self.buyer_checked = False
        self.seller_health_warning = False
        self.server = ThreadingHTTPServer(("127.0.0.1", _reserve_free_port()), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def _handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                owner._respond(self, "GET")

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                owner._respond(self, "POST")

            def log_message(self, _format: str, *_args: object) -> None:
                return

        return Handler

    def _respond(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        path = urlparse(handler.path).path
        self.calls[(method, path)] = self.calls.get((method, path), 0) + 1
        if path == DEFAULT_SETTINGS_UI_PATH:
            body = _render_sheet_vitrina_settings_ui().encode("utf-8")
            handler.send_response(200)
            handler.send_header("Content-Type", "text/html; charset=utf-8")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return
        payload = self._payload(method, path)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _payload(self, method: str, path: str) -> dict[str, object]:
        now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        if path == DEFAULT_SOURCES_SESSIONS_PATH:
            payload = _sources_payload(now)
            if self.seller_health_warning:
                payload["seller_portal"]["authorization"].update({
                    "health_persistence_status": "probe_history",
                    "health_persistence_warning": "Проверка входа завершена, но кеш статуса источника не обновлён.",
                })
            if self.buyer_checked:
                payload["wb_buyer"]["capability"] = {
                    "status": "observed", "valid": False, "wallet_price": 139,
                    "nonwallet_price": 144, "authenticated_buyer_price": None,
                    "checked_at": PRICE_MEASURED_AT, "price_measured_at": PRICE_MEASURED_AT,
                }
            return payload
        if path == DEFAULT_SELLER_PORTAL_SESSION_CHECK_PATH:
            result = {"status": "session_valid_canonical", "organization_confirmed": True, "checked_at": now}
            if self.seller_health_warning:
                result["health_persistence_warning"] = "Проверка входа завершена, но кеш статуса источника не обновлён."
            return result
        if path == DEFAULT_WB_BUYER_SESSION_CHECK_PATH:
            self.buyer_checked = True
            return {"capability_status": "observed", "capability_valid": False,
                    "capability_checked_at": PRICE_MEASURED_AT,
                    "price": {"wallet_price": 139, "nonwallet_price": 144,
                              "authenticated_buyer_price": None, "measured_at": PRICE_MEASURED_AT}}
        if path == DEFAULT_SPP_PROXY_SOURCE_CHECK_PATH:
            return {"status": "available", "authorization_required": False, "checked_at": now}
        if path == DEFAULT_WB_SUPPLIES_TRANSIT_COST_CHECK_PATH:
            return {"accepted": True, "run_id": "route-check-1", "status": "queued"}
        if path == DEFAULT_WB_SUPPLIES_TRANSIT_COST_STATUS_PATH:
            return {"run": {"run_id": "route-check-1", "status": "success"}, "coverage": _coverage(now)}
        if path in {DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH, DEFAULT_WB_BUYER_RECOVERY_START_PATH}:
            return {"run_status": "completed", "status": "completed", "running": False, "checked_at": now}
        # The settings page eagerly loads its established directory tabs too.
        # One broad empty read model keeps this smoke focused on source/session UI.
        return {
            "items": [],
            "groups": [],
            "documents": [],
            "rows": [],
            "available_sections": [],
            "status": "ready",
        }

    def __enter__(self) -> "_SettingsServer":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _coverage(now: str) -> dict[str, object]:
    return {
        "auth_status": "valid",
        "route_status": "available",
        "collector_status": "healthy",
        "freshness_status": "fresh",
        "overall_status": "healthy",
        "confirmed": 7,
        "eligible": 7,
        "pending": 0,
        "last_success_at": now,
        "last_attempt_at": now,
        "last_error": "",
    }


def _sources_payload(now: str) -> dict[str, object]:
    return {
        "contract_name": "sheet_vitrina_v1_sources_sessions_v1",
        "generated_at": now,
        "refresh_ttl_seconds": 180,
        "seller_portal": {
            "authorization": {
                "session_status": "session_valid_canonical",
                "session_status_label": "Сессия активна",
                "organization_confirmed": True,
                "expected_supplier_label": "Канонический кабинет",
                "checked_at": now,
                "running": False,
            },
            "collectors": ["Витрина", "Поставки WB"],
            "transit_cost": {"coverage": _coverage(now)},
        },
        "wb_buyer": {
            "authorization": {
                "status": "completed",
                "running": False,
                "login_confirmed": True,
                "session": {
                    "status": "authenticated_surface",
                    "status_label": "Вход подтверждён",
                    "login_confirmed": True,
                    "account_confirmed": False,
                    "checked_at": LOGIN_CHECKED_AT,
                },
            },
            "capability": {"status": "not_checked", "valid": False, "checked_at": now},
            "collectors": ["Контрольная цена WB Buyer"],
        },
        "spp_proxy": {
            "authorization_required": False,
            "source_mode": "anonymous_public_card",
            "latest_refresh_at": now,
            "latest_refresh_outcome": {"status": "success"},
            "latest_route_probe": {"status": "available", "checked_at": now},
            "collectors": ["Витрина: SPP Proxy"],
        },
    }


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _launch_chromium(playwright):
    try:
        return playwright.chromium.launch()
    except PlaywrightError as exc:
        if "BrowserType.launch: Executable doesn't exist at " not in str(exc):
            raise
        # The PR plan uses the base check map, which can predate this smoke's
        # browser dependency. Install only the missing bundled browser.
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"],
            check=True,
            timeout=600,
        )
        return playwright.chromium.launch()


def main() -> None:
    with _SettingsServer() as server, TemporaryDirectory(prefix="sources-sessions-ui-") as tmp:
        with sync_playwright() as playwright:
            browser = _launch_chromium(playwright)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page_errors: list[str] = []
            console_errors: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            response = page.goto(
                f"{server.base_url}{DEFAULT_SETTINGS_UI_PATH}#sources-sessions",
                wait_until="domcontentloaded",
            )
            if response is None or response.status != 200:
                raise AssertionError("settings page did not return HTTP 200")
            page.wait_for_function("() => document.documentElement.dataset.settingsReady === 'true'")
            page.wait_for_function("() => document.querySelector('#sellerSourceBadge')?.innerText === 'Сессия активна'")
            panel = page.locator('[data-settings-group-panel="sources-sessions"]')
            if panel.is_hidden() or page.locator('[data-settings-group-button="sources-sessions"]').get_attribute("aria-selected") != "true":
                raise AssertionError("#sources-sessions must select the centralized group before localStorage")
            if panel.locator("[data-source-card]").count() != 3:
                raise AssertionError("centralized settings must render three separate source contours")
            public_card = panel.locator('[data-source-card="public"]')
            if public_card.locator("[data-source-recover]").count() or "Авторизация и login UI не используются" not in public_card.inner_text():
                raise AssertionError("public WB Card/SPP Proxy must stay anonymous and recovery-free")
            if "7/7" not in panel.locator("#sellerSourceHealth").inner_text():
                raise AssertionError("Seller status must expose exact transit coverage")

            with page.expect_response(
                lambda response: response.request.method == "POST"
                and urlparse(response.url).path == DEFAULT_WB_SUPPLIES_TRANSIT_COST_CHECK_PATH
            ) as transit_probe, page.expect_response(
                lambda response: response.request.method == "GET"
                and urlparse(response.url).path == DEFAULT_WB_SUPPLIES_TRANSIT_COST_STATUS_PATH
            ) as transit_status:
                page.evaluate("document.querySelector('[data-source-check=\"seller\"]').click(); document.querySelector('[data-source-check=\"seller\"]').click()")
            if transit_probe.value.status != 200 or transit_probe.value.json() != {
                "accepted": True, "run_id": "route-check-1", "status": "queued"
            }:
                raise AssertionError("Seller check must receive the exact supply/cost probe response")
            if transit_status.value.status != 200 or transit_status.value.json().get("run", {}).get("status") != "success":
                raise AssertionError("Seller check must finish the exact supply/cost route probe")
            page.wait_for_function("() => document.querySelector('#sellerSourceError')?.textContent === ''")
            if server.calls.get(("GET", DEFAULT_SELLER_PORTAL_SESSION_CHECK_PATH), 0) != 1:
                raise AssertionError("identical Seller checks must be single-flight")
            if server.calls.get(("POST", DEFAULT_WB_SUPPLIES_TRANSIT_COST_CHECK_PATH), 0) != 1:
                raise AssertionError("Seller check must include one exact supply/cost route probe")

            server.seller_health_warning = True
            page.locator('[data-source-check="seller"]').click()
            page.wait_for_function("() => document.querySelector('#sellerSourceBadge')?.innerText.includes('статус не сохранён')")
            if "Проверка маршрута supply/cost отложена" not in page.locator("#sellerSourceError").inner_text():
                raise AssertionError("a successful Seller probe with locked cache must show the deferred transit warning")
            if server.calls.get(("POST", DEFAULT_WB_SUPPLIES_TRANSIT_COST_CHECK_PATH), 0) != 1:
                raise AssertionError("locked cache must not launch another transit collector")

            if page.locator('[data-source-check="buyer"]').is_disabled():
                raise AssertionError("Buyer price check must use the isolated durable Chrome source")
            if server.calls.get(("GET", DEFAULT_WB_BUYER_SESSION_CHECK_PATH), 0):
                raise AssertionError("Opening Settings must not launch the Buyer price browser")
            page.locator('[data-source-check="buyer"]').click()
            page.wait_for_function("() => document.querySelector('#buyerSourceHealth')?.innerText.includes('139 ₽ с WB Кошельком')")
            if "144 ₽ без WB Кошелька" not in page.locator("#buyerSourceHealth").inner_text():
                raise AssertionError("Buyer check must preserve separate payment labels")
            checked_rows = page.locator("#buyerSourceHealth").inner_text()
            measured_label = page.evaluate("new Date('2026-09-28T12:35:42Z').toLocaleString('ru-RU')")
            login_label = page.evaluate("new Date('2026-09-28T12:00:00Z').toLocaleString('ru-RU')")
            if "Вход подтверждён" not in checked_rows or measured_label not in checked_rows or login_label in checked_rows:
                raise AssertionError("Buyer price must show its own measurement time, not login proof time")
            summary_calls = server.calls.get(("GET", DEFAULT_SOURCES_SESSIONS_PATH), 0)
            with page.expect_response(lambda response: urlparse(response.url).path == DEFAULT_SOURCES_SESSIONS_PATH):
                page.locator("#reloadSourcesSessionsButton").click()
            page.wait_for_timeout(100)
            if server.calls.get(("GET", DEFAULT_SOURCES_SESSIONS_PATH), 0) <= summary_calls:
                raise AssertionError("Explicit summary reload was not requested")
            reloaded_rows = page.locator("#buyerSourceHealth").inner_text()
            if "139 ₽ с WB Кошельком" not in reloaded_rows or "144 ₽ без WB Кошелька" not in reloaded_rows or "Вход подтверждён" not in reloaded_rows or measured_label not in reloaded_rows or login_label in reloaded_rows:
                raise AssertionError("Summary reload lost buyer prices, time, or auth")
            page.locator('[data-source-check="public"]').click()
            page.wait_for_function(
                "() => document.querySelector('#publicSourceError')?.innerText !== 'Проверяем точный маршрут...'"
            )
            if server.calls.get(("POST", DEFAULT_SPP_PROXY_SOURCE_CHECK_PATH), 0) != 1:
                raise AssertionError("public source check must use the anonymous WB Card route")

            for width in (760, 560):
                page.set_viewport_size({"width": width, "height": 900})
                page.wait_for_timeout(100)
                geometry = page.evaluate(
                    """() => ({
                      viewport: document.documentElement.clientWidth,
                      documentWidth: document.documentElement.scrollWidth,
                      cardOverflow: Array.from(document.querySelectorAll('[data-source-card]')).some((node) => node.scrollWidth > node.clientWidth + 1)
                    })"""
                )
                if geometry["documentWidth"] > geometry["viewport"] + 1 or geometry["cardOverflow"]:
                    raise AssertionError(f"sources/session cards overflow at {width}px: {geometry}")
            page.screenshot(path=str(Path(tmp) / "settings-sources-sessions.png"), full_page=True)
            if page_errors or console_errors:
                raise AssertionError(f"browser errors: page={page_errors} console={console_errors}")
            browser.close()
    print("sheet_vitrina_v1_settings_sources_sessions_browser_smoke: OK")


if __name__ == "__main__":
    main()
