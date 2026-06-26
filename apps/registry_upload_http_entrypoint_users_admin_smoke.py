"""Smoke-check WebCore runtime users admin and bounded role access."""

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
    DEFAULT_SETTINGS_UI_PATH,
    DEFAULT_SETTINGS_USERS_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_SUPPLIER_SHIPMENTS_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


def main() -> None:
    admin_username = "owner"
    admin_password = "admin-password-not-secret"
    operator_password = "operator-password-old"
    operator_password_new = "operator-password-new"
    supply_password = "supply-password-not-secret"
    with TemporaryDirectory(prefix="webcore-users-admin-smoke-") as tmp:
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
                "WB_CORE_WEB_AUTH_USERNAME": admin_username,
                "WB_CORE_WEB_AUTH_PASSWORD_HASH": _password_hash(admin_password),
                "WB_CORE_WEB_AUTH_SESSION_SECRET": "users-admin-smoke-session-secret",
            }
        ):
            server = build_registry_upload_http_server(
                config,
                entrypoint=RegistryUploadHttpEntrypoint(runtime_dir=runtime_dir),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{config.port}"
                admin = urllib_request.build_opener(urllib_request.HTTPCookieProcessor(CookieJar()))
                admin_code, _, admin_shell = _login(
                    admin,
                    base_url,
                    admin_username,
                    admin_password,
                    DEFAULT_SETTINGS_UI_PATH,
                )
                if admin_code != 200:
                    raise AssertionError(f"bootstrap admin login must load settings shell: {admin_code}")
                if (
                    'data-unified-tab-button="settings"' not in admin_shell
                    or 'data-logout-link href="/logout"' not in admin_shell
                    or '"initial_tab": "settings"' not in admin_shell
                ):
                    raise AssertionError("direct settings path must render common shell with active settings tab")

                embedded_code, _, embedded_settings = _opener_text(
                    admin,
                    f"{base_url}{DEFAULT_SETTINGS_UI_PATH}?embedded=1",
                )
                if embedded_code != 200 or any(
                    marker not in embedded_settings
                    for marker in (
                        ">Справочники<",
                        ">Пользователи<",
                        ">Номенклатура<",
                        ">Договоры<",
                        ">Инвойсы<",
                        "id=\"userRows\"",
                    )
                ):
                    raise AssertionError("embedded settings must expose directories and users groups")

                unauth_users_code, unauth_users_payload = _get_json(f"{base_url}{DEFAULT_SETTINGS_USERS_PATH}")
                if unauth_users_code != 401 or unauth_users_payload.get("error") != "authentication_required":
                    raise AssertionError(f"unauthenticated users API must be 401: {unauth_users_code} {unauth_users_payload}")

                users_code, users_payload = _opener_json(admin, f"{base_url}{DEFAULT_SETTINGS_USERS_PATH}")
                if users_code != 200 or not _has_user(users_payload, admin_username, "admin"):
                    raise AssertionError(f"admin users API must list env bootstrap admin: {users_code} {users_payload}")
                if "password" in json.dumps(users_payload).lower() or "password_hash" in json.dumps(users_payload).lower():
                    raise AssertionError("users API must not expose password fields")

                operator_payload = _create_user(
                    admin,
                    base_url,
                    {
                        "username": "runtime-operator",
                        "display_name": "Runtime Operator",
                        "role": "operator",
                        "password": operator_password,
                        "is_active": True,
                    },
                )
                operator_id = str(operator_payload["user"]["user_id"])
                if "password" in json.dumps(operator_payload).lower() or "password_hash" in json.dumps(operator_payload).lower():
                    raise AssertionError("create user response must not expose password fields")

                operator = urllib_request.build_opener(urllib_request.HTTPCookieProcessor(CookieJar()))
                operator_code, _, _ = _login(
                    operator,
                    base_url,
                    "runtime-operator",
                    operator_password,
                    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
                )
                if operator_code != 200:
                    raise AssertionError("runtime operator must login with created password")
                operator_users_code, operator_users_payload = _opener_json(operator, f"{base_url}{DEFAULT_SETTINGS_USERS_PATH}")
                if operator_users_code != 403 or operator_users_payload.get("error") != "forbidden":
                    raise AssertionError("non-admin runtime operator must not read users API")

                _patch_user(
                    admin,
                    base_url,
                    operator_id,
                    {"password": operator_password_new},
                )
                old_password_opener = urllib_request.build_opener(urllib_request.HTTPCookieProcessor(CookieJar()))
                _, _, old_password_body = _login(
                    old_password_opener,
                    base_url,
                    "runtime-operator",
                    operator_password,
                    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
                )
                if "Неверный логин или пароль" not in old_password_body:
                    raise AssertionError("old runtime password must stop working after password change")
                new_password_opener = urllib_request.build_opener(urllib_request.HTTPCookieProcessor(CookieJar()))
                new_password_code, _, _ = _login(
                    new_password_opener,
                    base_url,
                    "runtime-operator",
                    operator_password_new,
                    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
                )
                if new_password_code != 200:
                    raise AssertionError("new runtime password must work after password change")

                supply_payload = _create_user(
                    admin,
                    base_url,
                    {
                        "username": "supply-only",
                        "display_name": "Supply Only",
                        "role": "supply_operator",
                        "password": supply_password,
                        "is_active": True,
                    },
                )
                if supply_payload["user"]["role"] != "supply_operator":
                    raise AssertionError("created supply user must keep supply_operator role")
                supply = urllib_request.build_opener(urllib_request.HTTPCookieProcessor(CookieJar()))
                supply_code, _, supply_shell = _login(
                    supply,
                    base_url,
                    "supply-only",
                    supply_password,
                    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
                )
                if supply_code != 200 or '"allowed_tabs": ["factory-order"]' not in supply_shell:
                    raise AssertionError("supply_operator must load shell with only supply tab allowed")
                supply_status_code, supply_status_payload = _opener_json(supply, f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}")
                if supply_status_code != 200:
                    raise AssertionError(f"supply_operator must access supply API: {supply_status_code} {supply_status_payload}")
                forbidden_vitrina_code, forbidden_vitrina_payload = _opener_json(
                    supply,
                    f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_READ_PATH}",
                )
                if forbidden_vitrina_code != 403 or forbidden_vitrina_payload.get("error") != "forbidden":
                    raise AssertionError("supply_operator must not access vitrina read API")
                forbidden_settings_code, _, _ = _opener_text(supply, f"{base_url}{DEFAULT_SETTINGS_UI_PATH}")
                if forbidden_settings_code != 403:
                    raise AssertionError("supply_operator must not access settings")
                forbidden_users_code, forbidden_users_payload = _opener_json(supply, f"{base_url}{DEFAULT_SETTINGS_USERS_PATH}")
                if forbidden_users_code != 403 or forbidden_users_payload.get("error") != "forbidden":
                    raise AssertionError("supply_operator must not access users API")

                _delete_user(admin, base_url, operator_id)
                archived_code, archived_payload = _opener_json(new_password_opener, f"{base_url}{DEFAULT_SHEET_STATUS_PATH}")
                if archived_code != 401 or archived_payload.get("error") != "authentication_required":
                    raise AssertionError("archived runtime user session must stop authenticating")

                duplicate_code, duplicate_payload = _opener_post_json(
                    admin,
                    f"{base_url}{DEFAULT_SETTINGS_USERS_PATH}",
                    {
                        "username": admin_username,
                        "role": "operator",
                        "password": "duplicate-password",
                        "is_active": True,
                    },
                )
                if duplicate_code != 400 or "username already exists" not in str(duplicate_payload.get("error")):
                    raise AssertionError("runtime username must be unique against env principals")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    _assert_last_admin_guard_without_env()
    print("registry_upload_http_entrypoint_users_admin_smoke: OK")


def _assert_last_admin_guard_without_env() -> None:
    with TemporaryDirectory(prefix="webcore-last-admin-smoke-") as tmp:
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
        with _patched_env({"WB_CORE_WEB_AUTH_REQUIRED": ""}):
            server = build_registry_upload_http_server(
                config,
                entrypoint=RegistryUploadHttpEntrypoint(runtime_dir=runtime_dir),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{config.port}"
                opener = urllib_request.build_opener(urllib_request.HTTPCookieProcessor(CookieJar()))
                created = _create_user(
                    opener,
                    base_url,
                    {
                        "username": "only-admin",
                        "role": "admin",
                        "password": "only-admin-password",
                        "is_active": True,
                    },
                )
                admin_id = str(created["user"]["user_id"])
                delete_code, delete_payload = _opener_delete_json(
                    opener,
                    f"{base_url}{DEFAULT_SETTINGS_USERS_PATH}/{urllib_parse.quote(admin_id)}",
                )
                if delete_code != 400 or "last admin" not in str(delete_payload.get("error")):
                    raise AssertionError("users API must reject disabling the last admin")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


def _create_user(opener: urllib_request.OpenerDirector, base_url: str, payload: dict[str, object]) -> dict[str, object]:
    code, response_payload = _opener_post_json(opener, f"{base_url}{DEFAULT_SETTINGS_USERS_PATH}", payload)
    if code != 201:
        raise AssertionError(f"user create failed: {code} {response_payload}")
    return response_payload


def _patch_user(
    opener: urllib_request.OpenerDirector,
    base_url: str,
    user_id: str,
    payload: dict[str, object],
) -> dict[str, object]:
    code, response_payload = _opener_patch_json(
        opener,
        f"{base_url}{DEFAULT_SETTINGS_USERS_PATH}/{urllib_parse.quote(user_id)}",
        payload,
    )
    if code != 200:
        raise AssertionError(f"user patch failed: {code} {response_payload}")
    return response_payload


def _delete_user(opener: urllib_request.OpenerDirector, base_url: str, user_id: str) -> dict[str, object]:
    code, response_payload = _opener_delete_json(
        opener,
        f"{base_url}{DEFAULT_SETTINGS_USERS_PATH}/{urllib_parse.quote(user_id)}",
    )
    if code != 200:
        raise AssertionError(f"user delete failed: {code} {response_payload}")
    return response_payload


def _has_user(payload: dict[str, object], username: str, role: str) -> bool:
    users = payload.get("users")
    if not isinstance(users, list):
        return False
    for user in users:
        if not isinstance(user, dict):
            continue
        if user.get("username") == username and user.get("role") == role:
            return True
    return False


def _login(
    opener: urllib_request.OpenerDirector,
    base_url: str,
    username: str,
    password: str,
    next_path: str,
) -> tuple[int, dict[str, str], str]:
    login_data = urllib_parse.urlencode(
        {"username": username, "password": password, "next": next_path}
    ).encode("utf-8")
    request = urllib_request.Request(
        f"{base_url}/login",
        data=login_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, dict(response.headers), response.read().decode("utf-8")
    except urllib_error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8")


def _request_json(request: urllib_request.Request, opener: urllib_request.OpenerDirector | None = None) -> tuple[int, dict[str, object]]:
    active_opener = opener or urllib_request.build_opener()
    try:
        with active_opener.open(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_json(url: str) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(url, headers={"Accept": "application/json"}, method="GET")
    return _request_json(request)


def _opener_json(opener: urllib_request.OpenerDirector, url: str) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(url, headers={"Accept": "application/json"}, method="GET")
    return _request_json(request, opener)


def _opener_post_json(
    opener: urllib_request.OpenerDirector,
    url: str,
    payload: dict[str, object],
) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    return _request_json(request, opener)


def _opener_patch_json(
    opener: urllib_request.OpenerDirector,
    url: str,
    payload: dict[str, object],
) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json; charset=utf-8"},
        method="PATCH",
    )
    return _request_json(request, opener)


def _opener_delete_json(opener: urllib_request.OpenerDirector, url: str) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(url, headers={"Accept": "application/json"}, method="DELETE")
    return _request_json(request, opener)


def _opener_text(opener: urllib_request.OpenerDirector, url: str) -> tuple[int, dict[str, str], str]:
    request = urllib_request.Request(url, headers={"Accept": "text/html"}, method="GET")
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, dict(response.headers), response.read().decode("utf-8")
    except urllib_error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8")


@contextmanager
def _patched_env(values: dict[str, str]):
    previous = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _password_hash(password: str) -> str:
    salt = b"users-admin-smoke-static-salt"
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260_000)
    return "pbkdf2_sha256$260000$" + _b64(salt) + "$" + _b64(digest)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
