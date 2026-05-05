"""Smoke-check WebCore simple session auth boundary."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from urllib import error as urllib_error, parse as urllib_parse, request as urllib_request
from http.cookiejar import CookieJar


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


def main() -> None:
    username = "owner"
    password = "test-password-not-secret"
    with TemporaryDirectory(prefix="webcore-auth-smoke-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=_reserve_free_port(),
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path="/v1/sheet-vitrina-v1/refresh",
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime_dir,
        )
        with _patched_env(
            {
                "WB_CORE_WEB_AUTH_REQUIRED": "1",
                "WB_CORE_WEB_AUTH_USERNAME": username,
                "WB_CORE_WEB_AUTH_PASSWORD_HASH": _password_hash(password),
                "WB_CORE_WEB_AUTH_SESSION_SECRET": "auth-smoke-session-secret",
            }
        ):
            server = build_registry_upload_http_server(config, entrypoint=RegistryUploadHttpEntrypoint(runtime_dir=runtime_dir))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{config.port}"
                html_code, html_headers, html_body = _request_text(
                    f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}",
                    headers={"Accept": "text/html"},
                    follow_redirects=False,
                )
                if html_code != 303 or "/login" not in html_headers.get("Location", ""):
                    raise AssertionError(f"unauthenticated HTML route must redirect to login: {html_code} {html_headers}")
                json_code, json_payload = _get_json(f"{base_url}{DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_PATH}")
                if json_code != 401 or json_payload.get("error") != "authentication_required":
                    raise AssertionError(f"unauthenticated JSON route must return 401 JSON: {json_code} {json_payload}")
                login_code, _, login_body = _request_text(f"{base_url}/login", headers={"Accept": "text/html"})
                if login_code != 200 or "Вход в WebCore" not in login_body:
                    raise AssertionError("login form must be rendered")
                opener = urllib_request.build_opener(urllib_request.HTTPCookieProcessor(CookieJar()))
                login_data = urllib_parse.urlencode(
                    {"username": username, "password": password, "next": DEFAULT_SHEET_WEB_VITRINA_UI_PATH}
                ).encode("utf-8")
                login_request = urllib_request.Request(
                    f"{base_url}/login",
                    data=login_data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    method="POST",
                )
                with opener.open(login_request, timeout=5) as response:
                    if response.status != 200:
                        raise AssertionError(f"login redirect target must load after successful auth: {response.status}")
                    body = response.read().decode("utf-8")
                    if password in body:
                        raise AssertionError("login password must not be reflected in HTML")
                complaints_request = urllib_request.Request(
                    f"{base_url}{DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_PATH}",
                    headers={"Accept": "application/json"},
                    method="GET",
                )
                with opener.open(complaints_request, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    if response.status != 200 or payload.get("contract_name") != "sheet_vitrina_v1_feedbacks_complaints":
                        raise AssertionError(f"authenticated JSON route must work: {response.status} {payload}")
                with opener.open(f"{base_url}/logout", timeout=5) as response:
                    if response.status != 200:
                        raise AssertionError("logout redirect target must render login form")
                logout_code, logout_payload = _get_json(f"{base_url}{DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_PATH}")
                if logout_code != 401 or logout_payload.get("error") != "authentication_required":
                    raise AssertionError(f"logout must clear auth session: {logout_code} {logout_payload}")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
    print("registry_upload_http_entrypoint_auth_smoke: OK")


def _password_hash(password: str) -> str:
    salt = b"auth-smoke-static-salt"
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260_000)
    return "pbkdf2_sha256$260000$" + _b64(salt) + "$" + _b64(digest)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


@contextmanager
def _patched_env(values: dict[str, str]):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _request_text(url: str, *, headers: dict[str, str] | None = None, follow_redirects: bool = True) -> tuple[int, dict[str, str], str]:
    opener = urllib_request.build_opener() if follow_redirects else urllib_request.build_opener(_NoRedirectHandler)
    request = urllib_request.Request(url, headers=headers or {}, method="GET")
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, dict(response.headers), response.read().decode("utf-8")
    except urllib_error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8")


def _get_json(url: str) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib_request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
