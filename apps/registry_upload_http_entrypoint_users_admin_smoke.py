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
    DEFAULT_SKU_MANAGEMENT_SKU_PREFIX,
    DEFAULT_SETTINGS_UI_PATH,
    DEFAULT_SETTINGS_USERS_PATH,
    DEFAULT_SHEET_DAILY_REPORT_PATH,
    DEFAULT_SHEET_FEEDBACKS_LOCAL_PATH,
    DEFAULT_SHEET_FEEDBACKS_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_RESEARCH_SKU_GROUP_COMPARISON_OPTIONS_PATH,
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
    settings_password = "settings-password-not-secret"
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
                "WB_CORE_SUPPLIER_AUTH_USERNAME": "hunshang",
                "WB_CORE_SUPPLIER_AUTH_PASSWORD_HASH": _password_hash("supplier-password-not-secret"),
            }
        ):
            runtime_entrypoint = RegistryUploadHttpEntrypoint(runtime_dir=runtime_dir)
            _seed_service_user(runtime_entrypoint)
            server = build_registry_upload_http_server(
                config,
                entrypoint=runtime_entrypoint,
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
                admin_main_nav = admin_shell.split('<div class="shell-actions">', 1)[0]
                if (
                    'data-unified-tab-button="settings"' in admin_main_nav
                    or 'data-logout-link href="/logout"' not in admin_shell
                    or '"initial_tab": "settings"' not in admin_shell
                    or '<button class="shell-logout-link" type="button" data-unified-tab-button="settings"' not in admin_shell
                ):
                    raise AssertionError("direct settings path must render common shell with right-side settings action")
                if (
                    '<button class="shell-logout-link" type="button" data-unified-tab-button="settings" hidden disabled'
                    not in admin_shell
                ):
                    raise AssertionError("settings shell action must be hidden by default before section JS filtering")
                if ".shell-logout-link[hidden]" not in admin_shell:
                    raise AssertionError("settings shell action hidden state must not be overridden by shell action display CSS")

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
                        "data-new-user-sections",
                        'data-access-picker="new"',
                        "data-access-picker-toggle",
                        "data-access-picker-popover",
                        "data-access-summary",
                        'label: "Витрина"',
                        'label: "Поставки"',
                        'label: "Отчёты"',
                        'label: "Отзывы"',
                        'label: "Исследования"',
                        'label: "Настройки"',
                    )
                ):
                    raise AssertionError("embedded settings must expose directories and users groups")
                if (
                    "newRoleSelect" in embedded_settings
                    or "window.prompt" in embedded_settings
                    or "access-fieldset" in embedded_settings
                    or "section-checkbox-grid" in embedded_settings
                ):
                    raise AssertionError("users UI must use compact access picker and inline password change, not role select/prompt")

                unauth_users_code, unauth_users_payload = _get_json(f"{base_url}{DEFAULT_SETTINGS_USERS_PATH}")
                if unauth_users_code != 401 or unauth_users_payload.get("error") != "authentication_required":
                    raise AssertionError(f"unauthenticated users API must be 401: {unauth_users_code} {unauth_users_payload}")

                users_code, users_payload = _opener_json(admin, f"{base_url}{DEFAULT_SETTINGS_USERS_PATH}")
                if users_code != 200 or not _has_user(users_payload, admin_username, "admin"):
                    raise AssertionError(f"admin users API must list env bootstrap admin: {users_code} {users_payload}")
                if _service_usernames(users_payload):
                    raise AssertionError(f"default users API must hide service users: {users_payload}")
                if users_payload.get("hidden_service_users_count") != 1 or users_payload.get("service_users_hidden") is not True:
                    raise AssertionError(f"default users API must report hidden service user count: {users_payload}")
                if "service_users" in users_payload:
                    raise AssertionError(f"default users API must not expose service_users diagnostics: {users_payload}")
                service_code, service_payload = _opener_json(
                    admin,
                    f"{base_url}{DEFAULT_SETTINGS_USERS_PATH}?include_service=1",
                )
                if service_code != 200 or "codex_live_supply_smoke" not in _service_usernames(service_payload, key="service_users"):
                    raise AssertionError(f"diagnostic users API must expose service users separately: {service_code} {service_payload}")
                if _service_usernames(service_payload):
                    raise AssertionError(f"diagnostic users API must still keep users[] user-facing only: {service_payload}")
                if _section_ids(users_payload) != [
                    "vitrina",
                    "supply",
                    "reports",
                    "feedbacks",
                    "feedbacks.ai_review",
                    "feedbacks.autoanswers_admin",
                    "ads",
                    "prices",
                    "sku_management",
                    "research",
                    "instructions",
                    "settings",
                ]:
                    raise AssertionError(f"users API must expose available sections: {users_payload}")
                if not _env_user_has_readonly_reason(users_payload, admin_username) or not _env_user_has_readonly_reason(users_payload, "hunshang"):
                    raise AssertionError(f"env users must expose readonly reason: {users_payload}")
                if "password" in json.dumps(users_payload).lower() or "password_hash" in json.dumps(users_payload).lower():
                    raise AssertionError("users API must not expose password fields")

                reserved_code, reserved_payload = _opener_post_json(
                    admin,
                    f"{base_url}{DEFAULT_SETTINGS_USERS_PATH}",
                    {
                        "username": "codex_live_supply_manual",
                        "display_name": "Codex live supply manual",
                        "allowed_sections": ["supply"],
                        "password": "reserved-prefix-password",
                        "is_active": True,
                    },
                )
                if reserved_code != 400 or "reserved service/debug/test identity" not in str(reserved_payload.get("error")):
                    raise AssertionError(f"reserved service username must be rejected: {reserved_code} {reserved_payload}")
                reserved_display_code, reserved_display_payload = _opener_post_json(
                    admin,
                    f"{base_url}{DEFAULT_SETTINGS_USERS_PATH}",
                    {
                        "username": "reserved-display-user",
                        "display_name": "Codex live smoke user",
                        "allowed_sections": ["supply"],
                        "password": "reserved-display-password",
                        "is_active": True,
                    },
                )
                if (
                    reserved_display_code != 400
                    or "reserved service/debug/test identity" not in str(reserved_display_payload.get("error"))
                ):
                    raise AssertionError(
                        f"reserved service display name must be rejected: "
                        f"{reserved_display_code} {reserved_display_payload}"
                    )

                invalid_code, invalid_payload = _opener_post_json(
                    admin,
                    f"{base_url}{DEFAULT_SETTINGS_USERS_PATH}",
                    {
                        "username": "invalid-section",
                        "display_name": "Invalid Section",
                        "allowed_sections": ["vitrina", "unknown"],
                        "password": "invalid-section-password",
                        "is_active": True,
                    },
                )
                if invalid_code != 400 or "unsupported section" not in str(invalid_payload.get("error")):
                    raise AssertionError(f"invalid section must be rejected: {invalid_code} {invalid_payload}")

                operator_payload = _create_user(
                    admin,
                    base_url,
                    {
                        "username": "runtime-operator",
                        "display_name": "Runtime Operator",
                        "allowed_sections": ["vitrina"],
                        "manage_users": False,
                        "password": operator_password,
                        "is_active": True,
                    },
                )
                operator_id = str(operator_payload["user"]["user_id"])
                if operator_payload["user"]["allowed_sections"] != ["vitrina"]:
                    raise AssertionError(f"created runtime user must keep selected sections: {operator_payload}")
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
                    raise AssertionError("runtime user without manage_users must not read users API")

                patched_operator = _patch_user(
                    admin,
                    base_url,
                    operator_id,
                    {"allowed_sections": ["vitrina", "reports"]},
                )
                if patched_operator["user"]["allowed_sections"] != ["vitrina", "reports"]:
                    raise AssertionError(f"patch allowed_sections must persist: {patched_operator}")

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
                operator_shell_code, _, operator_shell = _opener_text(new_password_opener, f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}")
                if operator_shell_code != 200 or '"allowed_tabs": ["vitrina", "reports"]' not in operator_shell:
                    raise AssertionError("patched runtime operator must see only selected shell sections")

                settings_payload = _create_user(
                    admin,
                    base_url,
                    {
                        "username": "settings-no-users",
                        "display_name": "Settings Without Users",
                        "allowed_sections": ["settings"],
                        "manage_users": False,
                        "password": settings_password,
                        "is_active": True,
                    },
                )
                if settings_payload["user"]["allowed_sections"] != ["settings"] or settings_payload["user"]["manage_users"]:
                    raise AssertionError(f"settings-only user must not receive manage_users: {settings_payload}")
                settings_user = urllib_request.build_opener(urllib_request.HTTPCookieProcessor(CookieJar()))
                settings_code, _, settings_shell = _login(
                    settings_user,
                    base_url,
                    "settings-no-users",
                    settings_password,
                    DEFAULT_SETTINGS_UI_PATH,
                )
                if settings_code != 200 or '"allowed_tabs": ["settings"]' not in settings_shell:
                    raise AssertionError("settings-only user must load shell with only settings tab allowed")
                settings_embedded_code, _, settings_embedded = _opener_text(settings_user, f"{base_url}{DEFAULT_SETTINGS_UI_PATH}?embedded=1")
                if settings_embedded_code != 200 or '"can_manage_users": false' not in settings_embedded:
                    raise AssertionError("settings-only user must render settings without users management access")
                settings_users_code, settings_users_payload = _opener_json(settings_user, f"{base_url}{DEFAULT_SETTINGS_USERS_PATH}")
                if settings_users_code != 403 or settings_users_payload.get("error") != "forbidden":
                    raise AssertionError("settings section without manage_users must not access users API")

                supply_payload = _create_user(
                    admin,
                    base_url,
                    {
                        "username": "supply-only",
                        "display_name": "Supply Only",
                        "allowed_sections": ["supply"],
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
                if supply_code != 200 or '"allowed_tabs": ["factory-order", "warehouses"]' not in supply_shell:
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
                forbidden_sku_detail_code, forbidden_sku_detail_payload = _opener_json(
                    supply,
                    f"{base_url}{DEFAULT_SKU_MANAGEMENT_SKU_PREFIX}/210183919",
                )
                if (
                    forbidden_sku_detail_code != 403
                    or forbidden_sku_detail_payload.get("error") != "forbidden"
                ):
                    raise AssertionError(
                        "narrow Vitrina SKU read must retain the sku_management permission"
                    )
                forbidden_reports_code, forbidden_reports_payload = _opener_json(supply, f"{base_url}{DEFAULT_SHEET_DAILY_REPORT_PATH}")
                if forbidden_reports_code != 403 or forbidden_reports_payload.get("error") != "forbidden":
                    raise AssertionError("supply_operator must not access reports API")
                forbidden_feedbacks_code, forbidden_feedbacks_payload = _opener_json(supply, f"{base_url}{DEFAULT_SHEET_FEEDBACKS_PATH}")
                if forbidden_feedbacks_code != 403 or forbidden_feedbacks_payload.get("error") != "forbidden":
                    raise AssertionError("supply_operator must not access feedbacks API")
                forbidden_local_code, forbidden_local_payload = _opener_json(
                    supply, f"{base_url}{DEFAULT_SHEET_FEEDBACKS_LOCAL_PATH}"
                )
                if forbidden_local_code != 403 or forbidden_local_payload.get("error") != "forbidden":
                    raise AssertionError("supply_operator must not access local autoanswers feedbacks API")
                forbidden_research_code, forbidden_research_payload = _opener_json(
                    supply,
                    f"{base_url}{DEFAULT_SHEET_RESEARCH_SKU_GROUP_COMPARISON_OPTIONS_PATH}",
                )
                if forbidden_research_code != 403 or forbidden_research_payload.get("error") != "forbidden":
                    raise AssertionError("supply_operator must not access research API")
                forbidden_settings_code, _, _ = _opener_text(supply, f"{base_url}{DEFAULT_SETTINGS_UI_PATH}")
                if forbidden_settings_code != 403:
                    raise AssertionError("supply_operator must not access settings")
                forbidden_users_code, forbidden_users_payload = _opener_json(supply, f"{base_url}{DEFAULT_SETTINGS_USERS_PATH}")
                if forbidden_users_code != 403 or forbidden_users_payload.get("error") != "forbidden":
                    raise AssertionError("supply_operator must not access users API")

                ui_payload = _create_user(
                    admin,
                    base_url,
                    {
                        "username": "ui-picker-user",
                        "display_name": "UI Picker User",
                        "allowed_sections": ["supply"],
                        "password": "ui-picker-password",
                        "is_active": True,
                    },
                )
                ui_user_id = str(ui_payload["user"]["user_id"])
                try:
                    _assert_users_access_picker_browser(base_url, admin_username, admin_password, ui_user_id)
                    ui_user_after_code, ui_user_after_payload = _opener_json(
                        admin,
                        f"{base_url}{DEFAULT_SETTINGS_USERS_PATH}",
                    )
                    if ui_user_after_code != 200:
                        raise AssertionError(f"users API must remain readable after UI picker save: {ui_user_after_code}")
                    ui_user_after = _find_user(ui_user_after_payload, "ui-picker-user")
                    if not ui_user_after or ui_user_after.get("allowed_sections") != ["supply", "reports"]:
                        raise AssertionError(f"UI picker save must persist selected sections: {ui_user_after_payload}")
                finally:
                    _delete_user(admin, base_url, ui_user_id)

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
                final_users_code, final_users_payload = _opener_json(admin, f"{base_url}{DEFAULT_SETTINGS_USERS_PATH}")
                service_usernames = _service_usernames(final_users_payload)
                if final_users_code != 200 or service_usernames:
                    raise AssertionError(f"users-admin smoke must not expose service/debug users: {service_usernames}")
                if final_users_payload.get("hidden_service_users_count") != 1:
                    raise AssertionError(f"seeded service user must stay hidden by default: {final_users_payload}")
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


def _seed_service_user(entrypoint: RegistryUploadHttpEntrypoint) -> None:
    entrypoint.handle_sheet_vitrina_user_create_request(
        {
            "user_id": "user_service_codex_live_supply_smoke",
            "username": "codex_live_supply_smoke",
            "display_name": "Codex live supply smoke",
            "role": "supply_operator",
            "allowed_sections": ["supply"],
            "manage_users": False,
            "password_hash": _password_hash("service-password-not-secret"),
            "is_active": False,
            "created_at": "2026-06-26T00:00:00Z",
            "updated_at": "2026-06-26T00:00:00Z",
        }
    )


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


def _find_user(payload: dict[str, object], username: str) -> dict[str, object] | None:
    users = payload.get("users")
    if not isinstance(users, list):
        return None
    for user in users:
        if isinstance(user, dict) and user.get("username") == username:
            return user
    return None


def _service_usernames(payload: dict[str, object], *, key: str = "users") -> list[str]:
    users = payload.get(key)
    if not isinstance(users, list):
        return []
    result: list[str] = []
    for user in users:
        if not isinstance(user, dict):
            continue
        username = str(user.get("username") or "")
        display_name = str(user.get("display_name") or "").lower()
        if username.startswith(("codex_", "smoke_", "test_")) or "codex live" in display_name or "codex debug" in display_name:
            result.append(username)
    return result


def _section_ids(payload: dict[str, object]) -> list[str]:
    sections = payload.get("available_sections")
    if not isinstance(sections, list):
        return []
    result: list[str] = []
    for section in sections:
        if isinstance(section, dict):
            result.append(str(section.get("section_id") or ""))
    return result


def _env_user_has_readonly_reason(payload: dict[str, object], username: str) -> bool:
    users = payload.get("users")
    if not isinstance(users, list):
        return False
    for user in users:
        if not isinstance(user, dict):
            continue
        if user.get("username") != username:
            continue
        return bool(user.get("readonly")) and "env-пользователь" in str(user.get("readonly_reason") or "")
    return False


def _assert_users_access_picker_browser(
    base_url: str,
    admin_username: str,
    admin_password: str,
    ui_user_id: str,
) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(8_000)
            page.goto(
                f"{base_url}/login?next={urllib_parse.quote(DEFAULT_SETTINGS_UI_PATH)}",
                wait_until="domcontentloaded",
            )
            page.fill('input[name="username"]', admin_username)
            page.fill('input[name="password"]', admin_password)
            page.click('button[type="submit"]')
            page.wait_for_load_state("domcontentloaded")

            page.goto(f"{base_url}{DEFAULT_SETTINGS_UI_PATH}?embedded=1", wait_until="domcontentloaded")
            page.locator('[data-settings-group-button="users"]').click()
            page.wait_for_selector(f'tr[data-user-id="{ui_user_id}"]')
            if page.locator("#userRows").inner_text().find("codex_live_supply_smoke") != -1:
                raise AssertionError("users UI must not render service/debug runtime rows")
            users_message = page.locator("#usersMessage").inner_text()
            if "скрыто служебных: 1" not in users_message:
                raise AssertionError(f"users UI must disclose hidden service count: {users_message}")

            if page.locator("fieldset.access-fieldset").count() or page.locator(".section-checkbox-grid").count():
                raise AssertionError("users UI must not render always-expanded access fieldset/grid")
            create_picker = page.locator('[data-access-picker="new"]')
            if create_picker.locator("[data-access-picker-popover]").is_visible():
                raise AssertionError("create user access picker must be collapsed by default")
            if "Доступы: 5 разделов" not in create_picker.locator("[data-access-summary]").inner_text():
                raise AssertionError("create user access picker must render compact default summary")

            create_picker.locator("[data-access-picker-toggle]").click()
            create_popover = create_picker.locator("[data-access-picker-popover]")
            if not create_popover.is_visible():
                raise AssertionError("create user access picker must open on click")
            create_labels = create_popover.inner_text()
            for label in ("Витрина", "Поставки", "Отчёты", "Отзывы", "Исследования", "Инструкции", "Настройки", "Управление пользователями"):
                if label not in create_labels:
                    raise AssertionError(f"create user access picker must include {label}")
            create_picker.locator('[data-section-checkbox][value="vitrina"]').uncheck()
            if "Доступы: 4 раздела" not in create_picker.locator("[data-access-summary]").inner_text():
                raise AssertionError("create user access summary must update after section changes")
            create_picker.locator("#newManageUsersInput").check()
            if not create_picker.locator('[data-section-checkbox][value="settings"]').is_checked():
                raise AssertionError("manage_users checkbox must auto-check settings section")
            if "+ управление" not in create_picker.locator("[data-access-summary]").inner_text():
                raise AssertionError("create user access summary must include manage-users state")
            page.keyboard.press("Escape")
            if create_popover.is_visible():
                raise AssertionError("access picker must close on Escape")

            user_row = page.locator(f'tr[data-user-id="{ui_user_id}"]')
            if "Доступы: Поставки" not in user_row.locator("[data-access-summary]").inner_text():
                raise AssertionError("table access picker must render compact row summary")
            user_row.locator("[data-access-picker-toggle]").click()
            if not user_row.locator("[data-access-picker-popover]").is_visible():
                raise AssertionError("table access picker must open on click")
            user_row.locator('[data-section-checkbox][value="reports"]').check()
            if "Доступы: 2 раздела" not in user_row.locator("[data-access-summary]").inner_text():
                raise AssertionError("table access summary must update after section changes")
            with page.expect_request(
                lambda request: request.method == "PATCH"
                and f"{DEFAULT_SETTINGS_USERS_PATH}/{urllib_parse.quote(ui_user_id)}" in request.url
            ) as request_info:
                user_row.locator(f'[data-user-save="{ui_user_id}"]').click()
            request = request_info.value
            patch_payload = json.loads(request.post_data or "{}")
            if patch_payload.get("allowed_sections") != ["supply", "reports"] or patch_payload.get("manage_users") is not False:
                raise AssertionError(f"UI picker save must send selected access payload: {patch_payload}")
            page.wait_for_selector("#usersMessage.message.success")

            owner_row = page.locator("#userRows tr").filter(has_text="owner").first
            if owner_row.locator(".access-picker-summary.is-readonly").count() != 1:
                raise AssertionError("readonly env row must render compact access summary")
            if owner_row.locator("[data-access-picker-toggle]").count() or owner_row.locator("[data-section-checkbox]").count():
                raise AssertionError("readonly env row must not expose active access checkboxes")
        finally:
            browser.close()


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
