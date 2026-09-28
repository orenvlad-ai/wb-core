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

from apps import wb_buyer_chrome_auth as chrome_auth  # noqa: E402
from apps.registry_upload_http_entrypoint_live import start_buyer_login_contour  # noqa: E402
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


def _post(url: str, headers: dict[str, str], payload: dict[str, str] | None = None) -> tuple[int, str]:
    req = request.Request(url, data=json.dumps(payload or {}).encode(), headers={"Accept": "application/json", "Content-Type": "application/json", **headers}, method="POST")
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
            entrypoint.runtime.save_sheet_vitrina_user({
                "user_id": "usr_fixture_settings", "username": "fixture_settings",
                "display_name": "Fixture settings", "role": "operator",
                "allowed_sections": ["settings"], "manage_users": False,
                "password_hash": _password_hash("runtime-password"), "is_active": True,
                "created_at": "2026-09-28T00:00:00Z", "updated_at": "2026-09-28T00:00:00Z",
            })
            server = http.build_registry_upload_http_server(config, entrypoint)
            buyer_server, buyer_thread = start_buyer_login_contour(server, port=_port())
            buyer_base = f"http://127.0.0.1:{buyer_server.server_port}"
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            old_status = http._buyer_viewer_raw_status
            old_stop = entrypoint.handle_wb_buyer_session_recovery_stop_request
            old_start = entrypoint.handle_wb_buyer_session_recovery_start_request
            old_finish = entrypoint.handle_wb_buyer_session_recovery_finish_request
            run_id = "buyer-recovery-chrome-20260927T120000Z-aabbccdd"
            durable_profile = Path(temp) / "durable-wb-profile"
            durable_profile.mkdir()
            (durable_profile / "Local State").write_text("fixture")
            stop_calls: list[str] = []
            status = {
                "run_id": run_id, "status": "awaiting_human", "running": True,
                "deadline_at": (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(),
                "viewer_owner": "",
            }
            http._buyer_viewer_raw_status = lambda: dict(status)
            def fake_stop(*, run_id: str | None = None, **_kwargs):
                if run_id and run_id != status["run_id"]:
                    return {**status, "status": "error", "reason": "buyer_recovery_run_not_current"}
                if status["status"] == "completed":
                    return dict(status)
                stop_calls.append(str(run_id or ""))
                status.update(status="stopping", running=True)
                return dict(status)
            entrypoint.handle_wb_buyer_session_recovery_stop_request = fake_stop
            finish_calls: list[str] = []
            def fake_finish(*, run_id: str, **_kwargs):
                finish_calls.append(run_id)
                status.update(status="validating_session", running=True)
                return dict(status)
            entrypoint.handle_wb_buyer_session_recovery_finish_request = fake_finish
            old_chrome_stop = chrome_auth.stop
            chrome_auth.stop = lambda *, requested_run_id=None: fake_stop(run_id=requested_run_id)
            entrypoint.handle_wb_buyer_session_recovery_start_request = lambda **_kwargs: {"run_id": run_id, "status": "awaiting_human", "running": True}
            try:
                auth_url = buyer_base + http.DEFAULT_WB_BUYER_VIEWER_AUTH_PATH
                viewer = http.DEFAULT_WB_BUYER_VIEWER_PREFIX
                ws_uri = viewer + "websockify?run_id=" + run_id
                public_host = {"Host": f"127.0.0.1:{port}", "X-Forwarded-Proto": "http"}
                headers = {**public_host, "X-Original-URI": ws_uri, "X-Original-Upgrade": "websocket", "X-Original-Origin": base}
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
                start_url = buyer_base + http.DEFAULT_WB_BUYER_RECOVERY_START_PATH
                start_headers = {**public_host, **cookies, "Origin": base, "X-WB-Buyer-Viewer-CSRF": "1"}
                if _post(start_url, {**start_headers, "X-WB-Buyer-Viewer-CSRF": ""})[0] != 403:
                    raise AssertionError("buyer start without CSRF marker must be denied")
                if _post(start_url, {**start_headers, "Origin": "https://other.example"})[0] != 403:
                    raise AssertionError("buyer start with foreign Origin must be denied")
                start_code, start_cookie = _post(start_url, start_headers)
                if start_code != 200 or http.WB_BUYER_VIEWER_COOKIE_NAME not in start_cookie:
                    raise AssertionError("owned start must issue scoped viewer cookie")
                finish_url = buyer_base + http.DEFAULT_WB_BUYER_RECOVERY_FINISH_PATH
                if _post(finish_url, {**start_headers, "X-WB-Buyer-Viewer-CSRF": ""}, {"run_id": run_id})[0] != 403:
                    raise AssertionError("finish without CSRF marker must be denied")
                if _post(finish_url, {**start_headers, "Origin": "https://other.example"}, {"run_id": run_id})[0] != 403:
                    raise AssertionError("finish with foreign Origin must be denied")
                if _post(finish_url, start_headers, {"run_id": "buyer-recovery-chrome-foreign"})[0] != 403:
                    raise AssertionError("finish for another run must be denied")
                owned = status["viewer_owner"]
                status["viewer_owner"] = "0" * 64
                if _post(finish_url, start_headers, {"run_id": run_id})[0] != 403:
                    raise AssertionError("foreign operator must not finish this run")
                status["viewer_owner"] = owned
                if _post(finish_url, start_headers, {"run_id": run_id})[0] != 200 or finish_calls != [run_id]:
                    raise AssertionError("owned finish must transition this run exactly once")
                status["status"] = "awaiting_human"  # Subsequent viewer tests require an open manual window.
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
                stop_code, stop_cookie = _post(
                    buyer_base + http.DEFAULT_WB_BUYER_RECOVERY_STOP_PATH,
                    start_headers,
                    {"run_id": "other-run"},
                )
                if stop_code != 409 or stop_cookie:
                    raise AssertionError("stale stop must not clear the current viewer cookie")
                if _get(buyer_base + http.DEFAULT_SOURCES_SESSIONS_PATH, cookies) != 404:
                    raise AssertionError("buyer lane must reject ordinary business GET")
                delete = request.Request(buyer_base + http.DEFAULT_WB_BUYER_RECOVERY_STATUS_PATH, headers=cookies, method="DELETE")
                try:
                    request.urlopen(delete, timeout=5)
                    raise AssertionError("buyer lane must reject inherited business DELETE")
                except error.HTTPError as exc:
                    if exc.code != 404:
                        raise AssertionError(f"unexpected buyer lane DELETE result: {exc.code}") from exc
                    exc.read()

                # Hold the one-threaded business listener. Buyer auth/status/start
                # must still respond through the separate loopback contour.
                slow_entered = threading.Event()
                slow_release = threading.Event()
                slow_calls: list[int] = []
                old_sources = entrypoint.handle_sources_sessions_status_request
                old_recovery_status = entrypoint.handle_wb_buyer_session_recovery_status_request
                def slow_sources(**_kwargs):
                    slow_calls.append(len(slow_calls) + 1)
                    slow_entered.set()
                    slow_release.wait(timeout=5)
                    return {"contract_name": "test_sources"}
                entrypoint.handle_sources_sessions_status_request = slow_sources
                entrypoint.handle_wb_buyer_session_recovery_status_request = lambda **_kwargs: {"run_id": run_id, "status": "awaiting_human", "running": True}
                ordinary_results: list[int] = []
                def ordinary_get():
                    ordinary_results.append(_get(base + http.DEFAULT_SOURCES_SESSIONS_PATH, cookies))
                first = threading.Thread(target=ordinary_get, daemon=True)
                second = threading.Thread(target=ordinary_get, daemon=True)
                try:
                    first.start()
                    if not slow_entered.wait(timeout=2):
                        raise AssertionError("slow business request did not enter")
                    # Reproduce the incident on the unchanged serial listener:
                    # even the static login form queues behind a slow business read.
                    primary_login_done = threading.Event()
                    primary_login_code: list[int] = []
                    def primary_login_get():
                        primary_login_code.append(_get(base + "/login"))
                        primary_login_done.set()
                    primary_login = threading.Thread(target=primary_login_get, daemon=True)
                    primary_login.start()
                    if primary_login_done.wait(timeout=0.3):
                        raise AssertionError("baseline primary /login unexpectedly bypassed slow business request")
                    second.start()
                    if _get(buyer_base + "/login") != 200:
                        raise AssertionError("login form must bypass a slow business read")
                    if _get(buyer_base + "/login/", {"Host": f"127.0.0.1:{port}"}) != 404:
                        raise AssertionError("login lane must reject a non-exact path")
                    if _get(buyer_base + "/logout") != 404 or _post(buyer_base + "/logout", {})[0] != 404:
                        raise AssertionError("login lane must not expose logout or unrelated endpoints")
                    class NoRedirect(request.HTTPRedirectHandler):
                        def redirect_request(self, req, fp, code, msg, headers, newurl):
                            return None
                    runtime_jar = CookieJar()
                    runtime_opener = request.build_opener(request.HTTPCookieProcessor(runtime_jar), NoRedirect())
                    def runtime_login(password: str) -> tuple[int, str]:
                        login_request = request.Request(buyer_base + "/login", data=parse.urlencode({
                            "username": "fixture_settings", "password": password,
                            "next": http.DEFAULT_SETTINGS_UI_PATH,
                        }).encode(), headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
                        try:
                            with runtime_opener.open(login_request, timeout=3) as response:
                                response.read()
                                return response.status, response.headers.get("Location", "")
                        except error.HTTPError as exc:
                            exc.read()
                            return exc.code, exc.headers.get("Location", "")
                    if runtime_login("wrong-password")[0] != 200 or list(runtime_jar):
                        raise AssertionError("runtime user wrong password must not issue session cookie")
                    if runtime_login("runtime-password") != (303, http.DEFAULT_SETTINGS_UI_PATH):
                        raise AssertionError("runtime user must receive the same authorized next redirect")
                    if not any(cookie.name == http.WEB_AUTH_COOKIE_NAME for cookie in runtime_jar):
                        raise AssertionError("runtime user login must issue a WebCore session cookie")
                    readback_request = request.Request(
                        buyer_base + http.DEFAULT_WB_BUYER_RECOVERY_STATUS_PATH + "?probe=false",
                        headers={**public_host, **cookies},
                    )
                    with request.urlopen(readback_request, timeout=5) as response:
                        readback = json.loads(response.read())
                        readback_cookie = response.headers.get("Set-Cookie", "")
                    if not readback.get("viewer_available") or http.WB_BUYER_VIEWER_COOKIE_NAME not in readback_cookie:
                        raise AssertionError("owned status readback must restore viewer cookie during slow business request")
                    stale_request = request.Request(
                        buyer_base + http.DEFAULT_WB_BUYER_RECOVERY_STATUS_PATH + "?probe=false&run_id=old-run",
                        headers={**public_host, **cookies},
                    )
                    with request.urlopen(stale_request, timeout=5) as response:
                        stale = json.loads(response.read())
                        stale_cookie = response.headers.get("Set-Cookie", "")
                    if stale.get("viewer_available") or stale_cookie:
                        raise AssertionError("stale status query must not receive current run cookie")
                    expect(200, {}, "viewer auth during slow business request")
                    if _post(start_url, start_headers)[0] != 200:
                        raise AssertionError("buyer start must bypass slow business request")
                    if len(slow_calls) != 1:
                        raise AssertionError("ordinary business requests must remain serial")
                finally:
                    slow_release.set()
                    first.join(timeout=5)
                    second.join(timeout=5)
                    primary_login.join(timeout=5)
                    entrypoint.handle_sources_sessions_status_request = old_sources
                    entrypoint.handle_wb_buyer_session_recovery_status_request = old_recovery_status
                if primary_login_code != [200] or ordinary_results != [200, 200] or len(slow_calls) != 2:
                    raise AssertionError(f"serial business listener did not drain: {ordinary_results} {slow_calls}")
                owner = status["viewer_owner"]
                status["viewer_owner"] = "0" * 64
                expect(403, {}, "foreign operator")
                if _post(start_url, start_headers)[0] != 409:
                    raise AssertionError("foreign operator must not join active run")
                status["viewer_owner"] = owner
                entrypoint.handle_wb_buyer_session_recovery_start_request = lambda **_kwargs: status.update(viewer_owner="0" * 64) or {"run_id": run_id, "status": "awaiting_human", "running": True}
                raced_code, raced_cookie = _post(start_url, start_headers)
                if raced_code != 409 or raced_cookie:
                    raise AssertionError("owner change during start must not return a usable viewer cookie")
                status["viewer_owner"] = owner
                entrypoint.handle_wb_buyer_session_recovery_start_request = lambda **_kwargs: {"run_id": run_id, "status": "awaiting_human", "running": True}
                status["deadline_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
                expect(403, {}, "expired run")
                status["deadline_at"] = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
                logout = request.Request(base + "/logout", headers={"Cookie": cookies["Cookie"]})
                with opener.open(logout, timeout=5) as response:
                    response.read()
                if stop_calls != [run_id] or not (durable_profile / "Local State").is_file():
                    raise AssertionError("WebCore logout must revoke only the login viewer, not WB profile")
                expect(403, {}, "logout revokes active run")
                # A completed auth result can coexist briefly with an active
                # unit while its main process exits. Logout must still work.
                with opener.open(login, timeout=5) as response:
                    response.read()
                second_session = next(cookie.value for cookie in jar if cookie.name == http.WEB_AUTH_COOKIE_NAME)
                status.update(status="completed", running=True, login_confirmed=True,
                    viewer_owner=hashlib.sha256(second_session.encode()).hexdigest())
                completed_cookies = {"Cookie": f"{http.WEB_AUTH_COOKIE_NAME}={second_session}; {http.WB_BUYER_VIEWER_COOKIE_NAME}={run_id}"}
                with opener.open(request.Request(base + "/logout", headers=completed_cookies), timeout=5) as response:
                    response.read()
                if status["status"] != "completed" or stop_calls != [run_id] or not (durable_profile / "Local State").is_file():
                    raise AssertionError("logout after completed proof changed WB login or stopped another run")
                expect(403, completed_cookies, "completed run never reopens viewer")
            finally:
                http._buyer_viewer_raw_status = old_status
                entrypoint.handle_wb_buyer_session_recovery_stop_request = old_stop
                chrome_auth.stop = old_chrome_stop
                entrypoint.handle_wb_buyer_session_recovery_start_request = old_start
                entrypoint.handle_wb_buyer_session_recovery_finish_request = old_finish
                buyer_server.shutdown()
                buyer_server.server_close()
                buyer_thread.join(timeout=5)
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
    print("wb_buyer_viewer_auth_smoke: OK")


if __name__ == "__main__":
    main()
