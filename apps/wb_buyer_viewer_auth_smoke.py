"""Local HTTP fixture for the run-bound buyer viewer auth_request boundary."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from urllib import error, parse, request
from http.cookiejar import CookieJar

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import wb_buyer_session_recovery as recovery  # noqa: E402
from packages.adapters import registry_upload_http_entrypoint as http  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def _environment(values: dict[str, str]):
    old = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _get(url: str, headers: dict[str, str] | None = None, opener=None) -> int:
    req = request.Request(url, headers={"Accept": "application/json", **(headers or {})})
    try:
        with (opener.open if opener else request.urlopen)(req, timeout=5) as response:
            response.read()
            return response.status
    except error.HTTPError as exc:
        exc.read()
        return exc.code


def _post(url: str, headers: dict[str, str]) -> tuple[int, str]:
    req = request.Request(url, data=b"{}", headers={"Accept": "application/json", "Content-Type": "application/json", **headers}, method="POST")
    try:
        with request.urlopen(req, timeout=5) as response:
            response.read()
            return response.status, response.headers.get("Set-Cookie", "")
    except error.HTTPError as exc:
        exc.read()
        return exc.code, exc.headers.get("Set-Cookie", "")


def main() -> None:
    with TemporaryDirectory(prefix="buyer-viewer-auth-") as temp:
        port = _port()
        base = f"http://127.0.0.1:{port}"
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1", port=port,
            upload_path=http.DEFAULT_UPLOAD_PATH,
            sheet_plan_path=http.DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path="/v1/sheet-vitrina-v1/refresh",
            sheet_status_path=http.DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=http.DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=Path(temp) / "runtime",
        )
        from apps.registry_upload_http_entrypoint_auth_smoke import _password_hash
        with _environment({
            "WB_CORE_WEB_AUTH_REQUIRED": "1",
            "WB_CORE_WEB_AUTH_USERNAME": "buyer_owner",
            "WB_CORE_WEB_AUTH_PASSWORD_HASH": _password_hash("fixture-password"),
            "WB_CORE_WEB_AUTH_SESSION_SECRET": "fixture-session-secret",
        }):
            entrypoint = RegistryUploadHttpEntrypoint(runtime_dir=config.runtime_dir)
            server = http.build_registry_upload_http_server(config, entrypoint)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            old_status = http._buyer_viewer_raw_status
            old_stop = recovery.stop_recovery
            old_start = entrypoint.handle_wb_buyer_session_recovery_start_request
            run_id = "buyer-recovery-20260927T120000Z-aabbccdd"
            status = {
                "run_id": run_id, "status": "awaiting_human", "running": True,
                "deadline_at": (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(),
                "viewer_owner": "",
            }
            http._buyer_viewer_raw_status = lambda: dict(status)
            recovery.stop_recovery = lambda *_args, **_kwargs: status.update(status="stopped", running=False) or dict(status)
            entrypoint.handle_wb_buyer_session_recovery_start_request = lambda **_kwargs: {"run_id": run_id, "status": "awaiting_human", "running": True}
            try:
                auth_url = base + http.DEFAULT_WB_BUYER_VIEWER_AUTH_PATH
                viewer = http.DEFAULT_WB_BUYER_VIEWER_PREFIX
                ws_uri = viewer + "websockify?run_id=" + run_id
                headers = {"X-Original-URI": ws_uri, "X-Original-Upgrade": "websocket", "X-Original-Origin": base}
                if _get(auth_url, headers) != 401:
                    raise AssertionError("anonymous forged nginx headers must be denied")

                jar = CookieJar()
                opener = request.build_opener(request.HTTPCookieProcessor(jar))
                login = request.Request(base + "/login", data=parse.urlencode({
                    "username": "buyer_owner", "password": "fixture-password", "next": http.DEFAULT_SETTINGS_UI_PATH,
                }).encode(), headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
                with opener.open(login, timeout=5) as response:
                    response.read()
                session = next(cookie.value for cookie in jar if cookie.name == http.WEB_AUTH_COOKIE_NAME)
                status["viewer_owner"] = hashlib.sha256(session.encode()).hexdigest()
                cookies = {"Cookie": f"{http.WEB_AUTH_COOKIE_NAME}={session}; {http.WB_BUYER_VIEWER_COOKIE_NAME}={run_id}"}
                start_url = base + http.DEFAULT_WB_BUYER_RECOVERY_START_PATH
                start_headers = {**cookies, "Origin": base, "X-WB-Buyer-Viewer-CSRF": "1"}
                if _post(start_url, {**start_headers, "X-WB-Buyer-Viewer-CSRF": ""})[0] != 403:
                    raise AssertionError("buyer start without CSRF marker must be denied")
                if _post(start_url, {**start_headers, "Origin": "https://other.example"})[0] != 403:
                    raise AssertionError("buyer start with foreign Origin must be denied")
                start_code, start_cookie = _post(start_url, start_headers)
                if start_code != 200 or http.WB_BUYER_VIEWER_COOKIE_NAME not in start_cookie:
                    raise AssertionError("owned start must issue scoped viewer cookie")
                def expect(expected: int, extra: dict[str, str], label: str) -> None:
                    actual = _get(auth_url, {**cookies, **headers, **extra})
                    if actual != expected:
                        raise AssertionError(f"{label}: expected {expected}, got {actual}")

                expect(200, {}, "valid same-origin websocket")
                expect(403, {"X-Original-Origin": ""}, "missing origin")
                expect(403, {"X-Original-Origin": "https://other.example"}, "foreign origin")
                expect(403, {"X-Original-URI": viewer + "websockify?run_id=other-run"}, "foreign run")
                expect(403, {"Cookie": f"{http.WEB_AUTH_COOKIE_NAME}={session}; {http.WB_BUYER_VIEWER_COOKIE_NAME}=other-run"}, "foreign run cookie")
                expect(403, {"Sec-Fetch-Site": "cross-site"}, "cross-site")
                expect(403, {"X-Original-URI": viewer + "../secret"}, "path traversal")
                owner = status["viewer_owner"]
                status["viewer_owner"] = "0" * 64
                expect(403, {}, "foreign operator")
                if _post(start_url, start_headers)[0] != 409:
                    raise AssertionError("foreign operator must not join active run")
                status["viewer_owner"] = owner
                status["deadline_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
                expect(403, {}, "expired run")
                status["deadline_at"] = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
                logout = request.Request(base + "/logout", headers={"Cookie": cookies["Cookie"]})
                with opener.open(logout, timeout=5) as response:
                    response.read()
                expect(403, {}, "logout revokes active run")
            finally:
                http._buyer_viewer_raw_status = old_status
                recovery.stop_recovery = old_stop
                entrypoint.handle_wb_buyer_session_recovery_start_request = old_start
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
    print("wb_buyer_viewer_auth_smoke: OK")


if __name__ == "__main__":
    main()
