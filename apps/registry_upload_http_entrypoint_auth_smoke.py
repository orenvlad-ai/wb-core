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
    DEFAULT_BUSINESS_DATA_WRITE_BARRIER_PATH,
    DEFAULT_SHEET_FEEDBACKS_AUTO_COMPLAINTS_RUNS_PATH,
    DEFAULT_SHEET_FEEDBACKS_AUTO_COMPLAINTS_SCHEDULES_PATH,
    DEFAULT_SHEET_FEEDBACKS_AUTO_COMPLAINTS_TICK_PATH,
    DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_PATH,
    DEFAULT_SETTINGS_UI_PATH,
    DEFAULT_SETTINGS_USERS_PATH,
    DEFAULT_NOMENCLATURE_PATH,
    DEFAULT_PARTNER_REPORT_OPTIONS_PATH,
    DEFAULT_PARTNER_REPORT_PREVIEW_PATH,
    DEFAULT_PARTNER_REPORT_PREVIEW_XLSX_PATH,
    DEFAULT_PARTNER_REPORT_SETTINGS_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SKU_MANAGEMENT_PATH,
    DEFAULT_SUPPLY_CALCULATIONS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_BUSINESS_PROJECTION_STATUS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_USER_CONFIG_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_SUPPLIER_SHIPMENTS_PARSE_PATH,
    DEFAULT_SUPPLIER_SHIPMENTS_PATH,
    DEFAULT_TRADE_DOCUMENTS_PATH,
    DEFAULT_UPLOAD_PATH,
    DEFAULT_WB_BUYER_RECOVERY_LAUNCHER_PATH,
    DEFAULT_WB_BUYER_RECOVERY_START_PATH,
    DEFAULT_WB_BUYER_SESSION_CHECK_PATH,
    build_registry_upload_http_server,
    _required_section_for_path,
    WEB_AUTH_SECTION_REPORTS,
    WEB_AUTH_SECTION_SKU_MANAGEMENT,
    WEB_AUTH_SECTION_SUPPLY,
    WEB_AUTH_SECTION_VITRINA,
)
from packages.application.business_data_write_barrier import (  # noqa: E402
    acquire_barrier,
    confirm_barrier_hold,
    mark_barrier_restoring,
    release_barrier,
)
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


def main() -> None:
    if _required_section_for_path(DEFAULT_SKU_MANAGEMENT_PATH) != WEB_AUTH_SECTION_SKU_MANAGEMENT:
        raise AssertionError("SKU management API must use its own section authorization boundary")
    if _required_section_for_path(DEFAULT_PARTNER_REPORT_OPTIONS_PATH) != WEB_AUTH_SECTION_REPORTS:
        raise AssertionError("Partner Report API must use the reports authorization boundary")
    if _required_section_for_path(DEFAULT_SUPPLY_CALCULATIONS_PATH) != WEB_AUTH_SECTION_SUPPLY:
        raise AssertionError(
            "supply calculation registry must use the supply authorization boundary"
        )
    if (
        _required_section_for_path(
            DEFAULT_SHEET_WEB_VITRINA_BUSINESS_PROJECTION_STATUS_PATH
        )
        != WEB_AUTH_SECTION_VITRINA
    ):
        raise AssertionError(
            "warehouse business projection status must use the Vitrina "
            "authorization boundary"
        )
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
                registry_code, registry_payload = _get_json(
                    f"{base_url}{DEFAULT_SUPPLY_CALCULATIONS_PATH}"
                )
                if (
                    registry_code != 401
                    or registry_payload.get("error") != "authentication_required"
                ):
                    raise AssertionError(
                        "unauthenticated supply calculation registry must return 401 JSON"
                    )
                sku_code, sku_payload = _get_json(f"{base_url}{DEFAULT_SKU_MANAGEMENT_PATH}")
                if sku_code != 401 or sku_payload.get("error") != "authentication_required":
                    raise AssertionError(f"unauthenticated SKU management route must return 401 JSON: {sku_code} {sku_payload}")
                partner_code, partner_payload = _get_json(
                    f"{base_url}{DEFAULT_PARTNER_REPORT_OPTIONS_PATH}"
                )
                if partner_code != 401 or partner_payload.get("error") != "authentication_required":
                    raise AssertionError(
                        f"unauthenticated Partner Report read must return 401 JSON: {partner_code} {partner_payload}"
                    )
                partner_preview_code, partner_preview_payload = _post_json(
                    f"{base_url}{DEFAULT_PARTNER_REPORT_PREVIEW_PATH}",
                    {"nm_id": "101", "selected_weeks": ["2026-07-06"]},
                )
                if partner_preview_code != 401 or partner_preview_payload.get("error") != "authentication_required":
                    raise AssertionError(
                        "unauthenticated Partner Report preview must return 401 JSON: "
                        f"{partner_preview_code} {partner_preview_payload}"
                    )
                partner_settings_code, partner_settings_payload = _post_json(
                    f"{base_url}{DEFAULT_PARTNER_REPORT_SETTINGS_PATH}",
                    {"nm_id": "101"},
                )
                if (
                    partner_settings_code != 401
                    or partner_settings_payload.get("error") != "authentication_required"
                ):
                    raise AssertionError("unauthenticated Partner Report settings write must be denied")
                partner_excel_code, partner_excel_payload = _post_json(
                    f"{base_url}{DEFAULT_PARTNER_REPORT_PREVIEW_XLSX_PATH}",
                    {"nm_id": "101", "selected_weeks": ["2026-07-06"]},
                )
                if (
                    partner_excel_code != 401
                    or partner_excel_payload.get("error") != "authentication_required"
                ):
                    raise AssertionError("unauthenticated Partner Report Excel must be denied")
                auto_code, auto_payload = _get_json(f"{base_url}{DEFAULT_SHEET_FEEDBACKS_AUTO_COMPLAINTS_SCHEDULES_PATH}")
                if auto_code != 401 or auto_payload.get("error") != "authentication_required":
                    raise AssertionError(f"unauthenticated automation route must return 401 JSON: {auto_code} {auto_payload}")
                supplier_list_code, supplier_list_payload = _get_json(f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}")
                if supplier_list_code != 401 or supplier_list_payload.get("error") != "authentication_required":
                    raise AssertionError(f"unauthenticated supplier shipment list must return 401 JSON: {supplier_list_code} {supplier_list_payload}")
                supplier_parse_code, supplier_parse_payload = _post_multipart(
                    f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PARSE_PATH}",
                    b"not-authenticated",
                    filename="invoice.xlsx",
                )
                if supplier_parse_code != 401 or supplier_parse_payload.get("error") != "authentication_required":
                    raise AssertionError(f"unauthenticated supplier parse must return 401 JSON: {supplier_parse_code} {supplier_parse_payload}")
                nomenclature_code, nomenclature_payload = _get_json(f"{base_url}{DEFAULT_NOMENCLATURE_PATH}")
                if nomenclature_code != 401 or nomenclature_payload.get("error") != "authentication_required":
                    raise AssertionError(f"unauthenticated nomenclature API must return 401 JSON: {nomenclature_code} {nomenclature_payload}")
                documents_code, documents_payload = _get_json(f"{base_url}{DEFAULT_TRADE_DOCUMENTS_PATH}")
                if documents_code != 401 or documents_payload.get("error") != "authentication_required":
                    raise AssertionError(f"unauthenticated trade documents API must return 401 JSON: {documents_code} {documents_payload}")
                user_config_code, user_config_payload = _get_json(f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_USER_CONFIG_PATH}")
                if user_config_code != 401 or user_config_payload.get("error") != "authentication_required":
                    raise AssertionError(f"unauthenticated user-config API must return 401 JSON: {user_config_code} {user_config_payload}")
                users_code, users_payload = _get_json(f"{base_url}{DEFAULT_SETTINGS_USERS_PATH}")
                if users_code != 401 or users_payload.get("error") != "authentication_required":
                    raise AssertionError(f"unauthenticated users API must return 401 JSON: {users_code} {users_payload}")
                tick_code, tick_payload = _post_json(f"{base_url}{DEFAULT_SHEET_FEEDBACKS_AUTO_COMPLAINTS_TICK_PATH}", {})
                if tick_code != 401 or tick_payload.get("error") != "authentication_required":
                    raise AssertionError(f"unauthenticated automation tick must return 401 JSON: {tick_code} {tick_payload}")
                buyer_check_code, buyer_check_payload = _get_json(f"{base_url}{DEFAULT_WB_BUYER_SESSION_CHECK_PATH}")
                if buyer_check_code != 401 or buyer_check_payload.get("error") != "authentication_required":
                    raise AssertionError(f"unauthenticated buyer-session check must return 401 JSON: {buyer_check_code} {buyer_check_payload}")
                buyer_start_code, buyer_start_payload = _post_json(f"{base_url}{DEFAULT_WB_BUYER_RECOVERY_START_PATH}", {})
                if buyer_start_code != 401 or buyer_start_payload.get("error") != "authentication_required":
                    raise AssertionError(f"unauthenticated buyer recovery start must return 401 JSON: {buyer_start_code} {buyer_start_payload}")
                buyer_launcher_code, buyer_launcher_payload = _get_json(f"{base_url}{DEFAULT_WB_BUYER_RECOVERY_LAUNCHER_PATH}")
                if buyer_launcher_code != 401 or buyer_launcher_payload.get("error") != "authentication_required":
                    raise AssertionError(f"unauthenticated buyer launcher must return 401 JSON: {buyer_launcher_code} {buyer_launcher_payload}")
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
                registry_request = urllib_request.Request(
                    f"{base_url}{DEFAULT_SUPPLY_CALCULATIONS_PATH}",
                    headers={"Accept": "application/json"},
                    method="GET",
                )
                with opener.open(registry_request, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    if (
                        response.status != 200
                        or payload.get("contract_name")
                        != "sheet_vitrina_v1_supply_calculation_registry"
                    ):
                        raise AssertionError(
                            "authenticated operator must read the supply calculation registry"
                        )
                settings_request = urllib_request.Request(
                    f"{base_url}{DEFAULT_SETTINGS_UI_PATH}",
                    headers={"Accept": "text/html"},
                    method="GET",
                )
                with opener.open(settings_request, timeout=5) as response:
                    body = response.read().decode("utf-8")
                    main_nav = body.split('<div class="shell-actions">', 1)[0]
                    if (
                        response.status != 200
                        or 'data-unified-tab-button="settings"' in main_nav
                        or '<button class="shell-logout-link" type="button" data-unified-tab-button="settings"' not in body
                        or 'data-logout-link href="/logout"' not in body
                        or '"initial_tab": "settings"' not in body
                    ):
                        raise AssertionError("authenticated operator settings path must render common shell")
                embedded_settings_request = urllib_request.Request(
                    f"{base_url}{DEFAULT_SETTINGS_UI_PATH}?embedded=1",
                    headers={"Accept": "text/html"},
                    method="GET",
                )
                with opener.open(embedded_settings_request, timeout=5) as response:
                    body = response.read().decode("utf-8")
                    if (
                        response.status != 200
                        or ">Справочники<" not in body
                        or ">Пользователи<" not in body
                        or ">Номенклатура<" not in body
                        or ">Договоры<" not in body
                        or ">Инвойсы<" not in body
                        or ">Договоры и инвойсы<" in body
                    ):
                        raise AssertionError("authenticated operator settings page must render settings tabs and registry sections")
                user_config_get = urllib_request.Request(
                    f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_USER_CONFIG_PATH}",
                    headers={"Accept": "application/json"},
                    method="GET",
                )
                with opener.open(user_config_get, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    if response.status != 200 or payload.get("status") != "missing":
                        raise AssertionError(f"authenticated user-config GET must start missing: {response.status} {payload}")
                user_config_post = urllib_request.Request(
                    f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_USER_CONFIG_PATH}",
                    data=json.dumps(
                        {
                            "base_revision": 0,
                            "config": {
                                "version": 2,
                                "scopes": {"total": {"order": ["avg_ctr_current"], "display": {"avg_ctr_current": "collapsed"}, "manual": True}},
                                "expanded_anchors": [],
                            },
                        },
                        ensure_ascii=False,
                    ).encode("utf-8"),
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                    method="POST",
                )
                with opener.open(user_config_post, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    if response.status != 200 or payload.get("revision") != 1:
                        raise AssertionError(f"authenticated user-config POST must persist revision 1: {response.status} {payload}")
                plan_fingerprint = "sha256:" + ("a" * 64)
                window_id = "snapshot-smoke-001"
                acquire_barrier(
                    runtime_dir,
                    window_id=window_id,
                    window_kind="snapshot",
                    plan_fingerprint=plan_fingerprint,
                    approval_reference="smoke-approval-001",
                    actor="smoke",
                    reason="HTTP write barrier smoke",
                )
                barrier_request = urllib_request.Request(
                    f"{base_url}{DEFAULT_BUSINESS_DATA_WRITE_BARRIER_PATH}",
                    headers={"Accept": "application/json"},
                    method="GET",
                )
                with opener.open(barrier_request, timeout=5) as response:
                    barrier_payload = json.loads(response.read().decode("utf-8"))
                    if (
                        response.status != 200
                        or barrier_payload.get("active") is not True
                        or barrier_payload.get("window_id") != window_id
                    ):
                        raise AssertionError(
                            f"authenticated barrier status must expose active window: {barrier_payload}"
                        )
                with opener.open(settings_request, timeout=5) as response:
                    maintenance_html = response.read().decode("utf-8")
                    if (
                        "wbCoreMaintenanceBarrier" not in maintenance_html
                        or DEFAULT_BUSINESS_DATA_WRITE_BARRIER_PATH not in maintenance_html
                    ):
                        raise AssertionError(
                            "authenticated UI must contain automatic maintenance banner/control guard"
                        )
                blocked_body = json.dumps(
                    {
                        "base_revision": 1,
                        "config": {
                            "version": 2,
                            "scopes": {},
                            "expanded_anchors": [],
                            "secret_marker": "must-not-enter-audit",
                        },
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                blocked_request = urllib_request.Request(
                    f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_USER_CONFIG_PATH}",
                    data=blocked_body,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "X-Request-ID": "barrier-smoke-request",
                    },
                    method="POST",
                )
                try:
                    opener.open(blocked_request, timeout=5)
                except urllib_error.HTTPError as exc:
                    blocked_payload = json.loads(exc.read().decode("utf-8"))
                    if (
                        exc.code != 423
                        or blocked_payload.get("code") != "business_data_maintenance"
                        or blocked_payload.get("attempt_audited") is not True
                    ):
                        raise AssertionError(
                            f"active barrier must return audited 423: {exc.code} {blocked_payload}"
                        )
                else:
                    raise AssertionError("active barrier must reject authenticated POST")
                audit_text = (
                    runtime_dir / ".business-data-write-barrier-audit.jsonl"
                ).read_text(encoding="utf-8")
                if (
                    "manual_business_write_blocked" not in audit_text
                    or "barrier-smoke-request" not in audit_text
                    or "must-not-enter-audit" in audit_text
                ):
                    raise AssertionError(
                        "blocked attempt audit must persist identity without request body"
                    )
                maintenance_state = {
                    "schema_version": "business_data_maintenance_v1",
                    "phase": "held",
                    "held_at": "2026-07-27T00:00:00Z",
                    "hold_readback": {
                        "quiet": True,
                        "auto_updates": {"revision": 2},
                    },
                }
                confirm_barrier_hold(
                    runtime_dir,
                    window_id=window_id,
                    plan_fingerprint=plan_fingerprint,
                    maintenance_state=maintenance_state,
                )
                mark_barrier_restoring(
                    runtime_dir,
                    window_id=window_id,
                    plan_fingerprint=plan_fingerprint,
                )
                release_barrier(
                    runtime_dir,
                    window_id=window_id,
                    plan_fingerprint=plan_fingerprint,
                    actor="smoke",
                    reason="HTTP write barrier smoke restore",
                    restore_readback={
                        "status": "restored",
                        "captured_at": "2026-07-27T00:00:01Z",
                        "exact_prior_state_restored": True,
                        "control_signature": "sha256:" + ("b" * 64),
                        "auto_updates": {
                            "revision": 3,
                            "master_desired": True,
                        },
                    },
                )
                resumed_post = urllib_request.Request(
                    f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_USER_CONFIG_PATH}",
                    data=json.dumps(
                        {
                            "base_revision": 1,
                            "config": {
                                "version": 2,
                                "scopes": {},
                                "expanded_anchors": [],
                            },
                        },
                        ensure_ascii=False,
                    ).encode("utf-8"),
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with opener.open(resumed_post, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    if response.status != 200 or payload.get("revision") != 2:
                        raise AssertionError(
                            f"released barrier must restore normal POST work: {payload}"
                        )
                schedules_request = urllib_request.Request(
                    f"{base_url}{DEFAULT_SHEET_FEEDBACKS_AUTO_COMPLAINTS_SCHEDULES_PATH}",
                    headers={"Accept": "application/json"},
                    method="GET",
                )
                with opener.open(schedules_request, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    if response.status != 200 or payload.get("contract_name") != "sheet_vitrina_v1_feedbacks_auto_complaints_schedules":
                        raise AssertionError(f"authenticated automation schedules route must work: {response.status} {payload}")
                runs_request = urllib_request.Request(
                    f"{base_url}{DEFAULT_SHEET_FEEDBACKS_AUTO_COMPLAINTS_RUNS_PATH}",
                    headers={"Accept": "application/json"},
                    method="GET",
                )
                with opener.open(runs_request, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    if response.status != 200 or payload.get("contract_name") != "sheet_vitrina_v1_feedbacks_auto_complaints_runs":
                        raise AssertionError(f"authenticated automation runs route must work: {response.status} {payload}")
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


def _post_json(url: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib_request.Request(
        url,
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _post_multipart(url: str, body_bytes: bytes, *, filename: str) -> tuple[int, dict[str, object]]:
    boundary = "----webcore-auth-smoke"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"),
            b"Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n",
            body_bytes,
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    request = urllib_request.Request(
        url,
        data=body,
        headers={"Accept": "application/json", "Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
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
