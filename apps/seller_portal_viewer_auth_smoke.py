"""Fixture-only Seller Portal viewer, owner, CSRF and independent-control checks."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from urllib import error, parse, request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import seller_portal_relogin_session as seller_recovery  # noqa: E402
from apps.registry_upload_http_entrypoint_live import start_seller_login_contours  # noqa: E402
from apps.registry_upload_http_entrypoint_auth_smoke import _password_hash  # noqa: E402
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


def _get(url: str, headers: dict[str, str] | None = None, *, timeout: float = 5) -> tuple[int, dict]:
    req = request.Request(url, headers={"Accept": "application/json", **(headers or {})})
    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read()
            return response.status, json.loads(body) if body.startswith(b"{") else {}
    except error.HTTPError as exc:
        body = exc.read()
        return exc.code, json.loads(body) if body.startswith(b"{") else {}


def _post(url: str, headers: dict[str, str], payload: dict | None = None) -> tuple[int, dict, str]:
    req = request.Request(url, data=json.dumps(payload or {}).encode(), headers={"Accept": "application/json", "Content-Type": "application/json", **headers}, method="POST")
    try:
        with request.urlopen(req, timeout=5) as response:
            body = response.read()
            return response.status, json.loads(body) if body.startswith(b"{") else {}, response.headers.get("Set-Cookie", "")
    except error.HTTPError as exc:
        body = exc.read()
        return exc.code, json.loads(body) if body.startswith(b"{") else {}, exc.headers.get("Set-Cookie", "")


def main() -> None:
    with TemporaryDirectory(prefix="seller-viewer-auth-") as temp, _environment({
        "WB_CORE_WEB_AUTH_REQUIRED": "1",
        "WB_CORE_WEB_AUTH_USERNAME": "seller_owner",
        "WB_CORE_WEB_AUTH_PASSWORD_HASH": _password_hash("fixture-password"),
        "WB_CORE_WEB_AUTH_SESSION_SECRET": "fixture-session-secret",
    }):
        port = _port()
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1", port=port, upload_path=http.DEFAULT_UPLOAD_PATH,
            sheet_plan_path=http.DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path="/v1/sheet-vitrina-v1/refresh",
            sheet_status_path=http.DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=http.DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=Path(temp) / "runtime",
        )
        entrypoint = RegistryUploadHttpEntrypoint(runtime_dir=config.runtime_dir)
        server = http.build_registry_upload_http_server(config, entrypoint)
        (control, control_thread), (start, start_thread) = start_seller_login_contours(server, control_port=_port(), start_port=_port())
        main_thread = threading.Thread(target=server.serve_forever, daemon=True)
        main_thread.start()
        base = f"http://127.0.0.1:{port}"
        control_base = f"http://127.0.0.1:{control.server_port}"
        start_base = f"http://127.0.0.1:{start.server_port}"
        run_id = "seller-recovery-20261003T090000Z-aabbccdd"
        status = {
            "run_id": run_id, "status": "awaiting_login", "running": True,
            "deadline_at": (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(),
            "viewer_expires_at": int((datetime.now(timezone.utc) + timedelta(minutes=20)).timestamp()),
            "viewer_owner": "",
        }
        stop_calls: list[str] = []
        finish_calls: list[str] = []
        cancel_calls: list[str] = []
        original = (
            http._seller_viewer_raw_status,
            entrypoint.handle_seller_portal_recovery_start_request,
            entrypoint.handle_seller_portal_recovery_stop_request,
            entrypoint.handle_seller_portal_recovery_finish_request,
            entrypoint.handle_seller_portal_recovery_cancel_request,
            entrypoint.handle_seller_portal_recovery_status_request,
            seller_recovery.stop_relogin_session,
        )
        http._seller_viewer_raw_status = lambda: dict(status)
        entrypoint.handle_seller_portal_recovery_start_request = lambda **_kwargs: {"run_id": run_id, "run_status": "awaiting_login", "running": True}
        entrypoint.handle_seller_portal_recovery_status_request = lambda **_kwargs: {"run_id": run_id, "run_status": status["status"], "running": status["running"]}

        def fake_stop(*, run_id: str | None = None, **_kwargs):
            stop_calls.append(str(run_id or ""))
            status.update(status="stopped", running=False)
            return {"run_id": status["run_id"], "run_status": "stopped", "running": False}

        def fake_finish(*, run_id: str, **_kwargs):
            finish_calls.append(run_id)
            return {"run_id": run_id, "run_status": "awaiting_login", "running": True}

        entrypoint.handle_seller_portal_recovery_stop_request = fake_stop
        entrypoint.handle_seller_portal_recovery_finish_request = fake_finish
        def fake_cancel(*, request_id: str, viewer_owner: str, **_kwargs):
            if viewer_owner != status["viewer_owner"]:
                raise PermissionError("foreign owner")
            cancel_calls.append(request_id)
            return {"run_id": "", "run_status": "stopped", "run_failure_code": "cancel_pending_start", "running": False}
        entrypoint.handle_seller_portal_recovery_cancel_request = fake_cancel
        seller_recovery.stop_relogin_session = lambda _config, requested_run_id=None: fake_stop(run_id=requested_run_id)
        try:
            auth_url = control_base + http.DEFAULT_SELLER_PORTAL_VIEWER_AUTH_PATH
            viewer = http.DEFAULT_SELLER_PORTAL_VIEWER_PREFIX
            ws_uri = viewer + "websockify?run_id=" + run_id
            public_host = {"Host": f"127.0.0.1:{port}", "X-Forwarded-Proto": "http"}
            ws_headers = {**public_host, "X-Original-URI": ws_uri, "X-Original-Upgrade": "websocket", "X-Original-Origin": base}
            assert _get(auth_url, ws_headers)[0] == 401

            jar = CookieJar()
            opener = request.build_opener(request.HTTPCookieProcessor(jar))
            login = request.Request(base + "/login", data=parse.urlencode({
                "username": "seller_owner", "password": "fixture-password", "next": http.DEFAULT_SETTINGS_UI_PATH,
            }).encode(), headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
            with opener.open(login, timeout=5) as response:
                response.read()
            session = next(cookie.value for cookie in jar if cookie.name == http.WEB_AUTH_COOKIE_NAME)
            status["viewer_owner"] = hashlib.sha256(session.encode()).hexdigest()
            cookies = {"Cookie": f"{http.WEB_AUTH_COOKIE_NAME}={session}; {http.SELLER_PORTAL_VIEWER_COOKIE_NAME}={run_id}"}
            owned_headers = {**public_host, **cookies, "Origin": base, "X-Seller-Viewer-CSRF": "1"}
            start_url = start_base + http.DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH
            legacy_start_url = base + http.DEFAULT_SHEET_WEB_VITRINA_SELLER_RECOVERY_START_PATH
            legacy_code, legacy_body, _ = _post(legacy_start_url, {**public_host, **cookies, "Origin": base}, {"replace": True})
            assert legacy_code == 410 and legacy_body["settings_url"].endswith("#sources-sessions")
            assert status["run_id"] == run_id and status["running"] and not stop_calls
            assert _post(start_url, {**owned_headers, "X-Seller-Viewer-CSRF": ""})[0] == 403
            assert _post(start_url, {**owned_headers, "Origin": "https://foreign.example"})[0] == 403
            assert _post(start_url, owned_headers, {"request_id": "../../bad"})[0] == 400
            started, _, viewer_cookie = _post(start_url, owned_headers, {"request_id": "1" * 32})
            assert started == 200 and http.SELLER_PORTAL_VIEWER_COOKIE_NAME in viewer_cookie

            finish_url = control_base + http.DEFAULT_SELLER_PORTAL_RECOVERY_FINISH_PATH
            assert _post(finish_url, {**owned_headers, "X-Seller-Viewer-CSRF": ""}, {"run_id": run_id})[0] == 403
            assert _post(finish_url, owned_headers, {"run_id": "foreign-run"})[0] == 403
            assert _post(finish_url, owned_headers, {"run_id": run_id})[0] == 200
            assert finish_calls == [run_id]
            nonce = "1" * 32
            stop_url = control_base + http.DEFAULT_SELLER_PORTAL_RECOVERY_STOP_PATH
            assert _post(stop_url, {**owned_headers, "X-Seller-Viewer-CSRF": ""}, {"request_id": nonce})[0] == 403
            assert _post(stop_url, owned_headers, {"request_id": "bad"})[0] == 400
            assert _post(stop_url, owned_headers, {"request_id": nonce})[0] == 200
            assert cancel_calls == [nonce] and status["running"]

            def expect(code: int, extra: dict[str, str], label: str) -> None:
                actual = _get(auth_url, {**cookies, **ws_headers, **extra})[0]
                if actual != code:
                    raise AssertionError(f"{label}: expected {code}, got {actual}")

            expect(200, {}, "owned WebSocket")
            expect(403, {"X-Original-Origin": ""}, "missing WebSocket Origin")
            expect(403, {"X-Original-Origin": "https://foreign.example"}, "foreign Origin")
            expect(403, {"X-Original-URI": viewer + "websockify?run_id=other"}, "foreign run")
            expect(403, {"X-Original-URI": viewer + "../secret"}, "path traversal")
            expect(403, {"Sec-Fetch-Site": "cross-site"}, "cross-site")
            owner = status["viewer_owner"]
            status["viewer_owner"] = "0" * 64
            expect(403, {}, "another operator")
            assert _post(start_url, owned_headers)[0] == 409
            assert _post(stop_url, owned_headers, {"request_id": nonce})[0] == 403
            assert cancel_calls == [nonce] and status["running"]
            assert _post(legacy_start_url, {**public_host, **cookies, "Origin": base, "X-Seller-Viewer-CSRF": "1"}, {"replace": True})[0] == 410
            assert status["run_id"] == run_id and status["running"] and not stop_calls
            status["viewer_owner"] = owner
            status["viewer_expires_at"] = int((datetime.now(timezone.utc) - timedelta(seconds=1)).timestamp())
            expect(403, {}, "expired run")
            status["viewer_expires_at"] = int((datetime.now(timezone.utc) + timedelta(minutes=20)).timestamp())

            assert _get(control_base + http.DEFAULT_SOURCES_SESSIONS_PATH, cookies)[0] == 404
            assert _post(start_base + http.DEFAULT_SELLER_PORTAL_RECOVERY_FINISH_PATH, owned_headers, {"run_id": run_id})[0] == 404

            # A long Seller preflight uses the start listener; auth/status/finish remain responsive.
            entered = threading.Event()
            release = threading.Event()
            def slow_start(**_kwargs):
                entered.set()
                release.wait(timeout=5)
                return {"run_id": run_id, "run_status": "awaiting_login", "running": True}
            entrypoint.handle_seller_portal_recovery_start_request = slow_start
            slow_result: list[int] = []
            slow_thread = threading.Thread(target=lambda: slow_result.append(_post(start_url, owned_headers)[0]), daemon=True)
            slow_thread.start()
            assert entered.wait(timeout=2)
            try:
                assert _get(auth_url, {**cookies, **ws_headers}, timeout=2)[0] == 200
                assert _get(control_base + http.DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH + "?probe=0", {**public_host, **cookies}, timeout=2)[0] == 200
                assert _post(finish_url, owned_headers, {"run_id": run_id})[0] == 200
            finally:
                release.set()
                slow_thread.join(timeout=5)
            assert slow_result == [200]

            assert _post(control_base + http.DEFAULT_SELLER_PORTAL_RECOVERY_STOP_PATH, owned_headers, {"run_id": "other"})[0] == 403
            status.update(status="awaiting_login", running=True)
            with opener.open(request.Request(base + "/logout", headers=cookies), timeout=5) as response:
                response.read()
            assert stop_calls == [run_id]
            expect(403, {}, "logout revokes viewer")
        finally:
            (http._seller_viewer_raw_status,
             entrypoint.handle_seller_portal_recovery_start_request,
             entrypoint.handle_seller_portal_recovery_stop_request,
             entrypoint.handle_seller_portal_recovery_finish_request,
             entrypoint.handle_seller_portal_recovery_cancel_request,
             entrypoint.handle_seller_portal_recovery_status_request,
             seller_recovery.stop_relogin_session) = original
            for contour, thread in ((start, start_thread), (control, control_thread)):
                contour.shutdown()
                contour.server_close()
                thread.join(timeout=5)
            server.shutdown()
            server.server_close()
            main_thread.join(timeout=5)
    print("seller_portal_viewer_auth_smoke: OK")


if __name__ == "__main__":
    main()
