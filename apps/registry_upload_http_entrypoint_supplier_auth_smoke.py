"""Smoke-check WebCore supplier role isolation."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from http.cookiejar import CookieJar
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from urllib import error as urllib_error, parse as urllib_parse, request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_SUPPLIER_UI_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_SUPPLIER_SHIPMENTS_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


def main() -> None:
    owner_password = "owner-password-not-secret"
    supplier_password = "supplier-password-not-secret"
    with TemporaryDirectory(prefix="supplier-auth-smoke-") as tmp:
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
                "WB_CORE_WEB_AUTH_USERNAME": "owner",
                "WB_CORE_WEB_AUTH_PASSWORD_HASH": _password_hash(owner_password),
                "WB_CORE_WEB_AUTH_SESSION_SECRET": "supplier-auth-smoke-session-secret",
                "WB_CORE_SUPPLIER_AUTH_USERNAME": "supplier",
                "WB_CORE_SUPPLIER_AUTH_PASSWORD_HASH": _password_hash(supplier_password),
                "WB_CORE_SUPPLIER_AUTH_DISPLAY_NAME": "Supplier",
            }
        ):
            server = build_registry_upload_http_server(config, entrypoint=RegistryUploadHttpEntrypoint(runtime_dir=runtime_dir))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{config.port}"
                unauth_code, unauth_headers, _ = _request_text(
                    f"{base_url}{DEFAULT_SHEET_SUPPLIER_UI_PATH}",
                    headers={"Accept": "text/html"},
                    follow_redirects=False,
                )
                if unauth_code != 303 or "/login" not in unauth_headers.get("Location", ""):
                    raise AssertionError("unauthenticated supplier page must redirect to login")

                operator = urllib_request.build_opener(urllib_request.HTTPCookieProcessor(CookieJar()))
                _login(operator, base_url, "owner", owner_password, DEFAULT_SHEET_WEB_VITRINA_UI_PATH)
                operator_shell_code, _, operator_shell = _opener_text(
                    operator,
                    f"{base_url}{DEFAULT_SHEET_OPERATOR_UI_PATH}?embedded_tab=factory-order",
                )
                if operator_shell_code != 200 or "Поставки" not in operator_shell or "От поставщика" not in operator_shell:
                    raise AssertionError("operator must keep full operator shell and supplier module entry")
                operator_supplier_code, _, operator_supplier = _opener_text(operator, f"{base_url}{DEFAULT_SHEET_SUPPLIER_UI_PATH}")
                if operator_supplier_code != 200 or "Реестр поставок" not in operator_supplier:
                    raise AssertionError("operator role must access supplier-only module page")

                supplier = urllib_request.build_opener(urllib_request.HTTPCookieProcessor(CookieJar()))
                supplier_login_code, _, supplier_login_body = _login(
                    supplier,
                    base_url,
                    "supplier",
                    supplier_password,
                    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
                )
                if supplier_login_code != 200 or "Реестр поставок" not in supplier_login_body:
                    raise AssertionError("supplier login with full-shell next must land on supplier-only page")
                supplier_page_code, _, supplier_page = _opener_text(supplier, f"{base_url}{DEFAULT_SHEET_SUPPLIER_UI_PATH}")
                if supplier_page_code != 200 or "От поставщика" not in supplier_page:
                    raise AssertionError("supplier role must access supplier page")
                supplier_api_code, supplier_api_payload = _opener_json(supplier, f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}")
                if supplier_api_code != 200 or supplier_api_payload.get("shipments") != []:
                    raise AssertionError("supplier role must access supplier shipment APIs")
                forbidden_html_code, _, _ = _opener_text(supplier, f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}")
                if forbidden_html_code != 403:
                    raise AssertionError("supplier role must not access full web-vitrina/operator shell")
                forbidden_api_code, forbidden_api_payload = _opener_json(supplier, f"{base_url}{DEFAULT_SHEET_STATUS_PATH}")
                if forbidden_api_code != 403 or forbidden_api_payload.get("error") != "forbidden":
                    raise AssertionError("supplier role must not access unrelated operator APIs")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
    print("registry_upload_http_entrypoint_supplier_auth_smoke: OK")


def _login(
    opener: urllib_request.OpenerDirector,
    base_url: str,
    username: str,
    password: str,
    next_path: str,
) -> tuple[int, dict[str, str], str]:
    body = urllib_parse.urlencode({"username": username, "password": password, "next": next_path}).encode("utf-8")
    request = urllib_request.Request(
        f"{base_url}/login",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "text/html"},
        method="POST",
    )
    with opener.open(request, timeout=5) as response:
        return response.status, dict(response.headers), response.read().decode("utf-8")


def _opener_text(
    opener: urllib_request.OpenerDirector,
    url: str,
) -> tuple[int, dict[str, str], str]:
    request = urllib_request.Request(url, headers={"Accept": "text/html"}, method="GET")
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, dict(response.headers), response.read().decode("utf-8")
    except urllib_error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8")


def _opener_json(opener: urllib_request.OpenerDirector, url: str) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _request_text(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    follow_redirects: bool = True,
) -> tuple[int, dict[str, str], str]:
    opener = urllib_request.build_opener() if follow_redirects else urllib_request.build_opener(_NoRedirectHandler)
    request = urllib_request.Request(url, headers=headers or {}, method="GET")
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, dict(response.headers), response.read().decode("utf-8")
    except urllib_error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8")


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _password_hash(password: str) -> str:
    salt = b"supplier-auth-smoke-salt"
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


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
