"""Smoke-check WebCore supplier role isolation."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from http.cookiejar import CookieJar
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from urllib import error as urllib_error, parse as urllib_parse, request as urllib_request
from uuid import uuid4

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_NOMENCLATURE_EXPORT_PATH,
    DEFAULT_NOMENCLATURE_IMPORT_PATH,
    DEFAULT_NOMENCLATURE_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SETTINGS_UI_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_SUPPLIER_UI_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_SUPPLIER_SHIPMENTS_PARSE_PATH,
    DEFAULT_SUPPLIER_SHIPMENTS_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


def main() -> None:
    owner_password = "owner-password-not-secret"
    supplier_password = "supplier-password-not-secret"
    supplier_invoice_bytes = _build_invoice_fixture()
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
                if operator_supplier_code != 200 or "Реестр заказов" not in operator_supplier:
                    raise AssertionError("operator role must access supplier-only module page")

                supplier = urllib_request.build_opener(urllib_request.HTTPCookieProcessor(CookieJar()))
                supplier_login_code, _, supplier_login_body = _login(
                    supplier,
                    base_url,
                    "supplier",
                    supplier_password,
                    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
                )
                if supplier_login_code != 200 or "Реестр заказов" not in supplier_login_body:
                        raise AssertionError("supplier login with full-shell next must land on supplier-only page")
                supplier_page_code, _, supplier_page = _opener_text(supplier, f"{base_url}{DEFAULT_SHEET_SUPPLIER_UI_PATH}")
                if supplier_page_code != 200 or "Реестр заказов" not in supplier_page:
                        raise AssertionError("supplier role must access supplier page")
                if '"can_delete_shipments": false' not in supplier_page or '"can_edit_order_status": false' not in supplier_page:
                        raise AssertionError("supplier page must not render operator-only shipment controls for supplier role")
                supplier_api_code, supplier_api_payload = _opener_json(supplier, f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}")
                if supplier_api_code != 200 or supplier_api_payload.get("shipments") != []:
                        raise AssertionError("supplier role must access supplier shipment APIs")
                parse_code, parse_payload = _opener_post_multipart(
                        supplier,
                        f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PARSE_PATH}",
                        supplier_invoice_bytes,
                        filename="PI-test 26GN390.xlsx",
                    )
                if parse_code != 200 or not parse_payload.get("upload_id"):
                        raise AssertionError(f"supplier role must parse supplier invoices, got {parse_code} {parse_payload}")
                create_code, create_payload = _opener_post_json(
                        supplier,
                        f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}",
                        {
                            "upload_id": parse_payload["upload_id"],
                            "shipment_date": "2026-05-14",
                            "order_status": "accepted_ff",
                            "payload": parse_payload,
                        },
                    )
                if create_code != 200 or not create_payload.get("shipment_id"):
                        raise AssertionError(f"supplier role must create supplier shipments, got {create_code} {create_payload}")
                if create_payload.get("order_status") != "production":
                        raise AssertionError("supplier role must not set order_status during create")
                shipment_id = str(create_payload["shipment_id"])
                detail_code, detail_payload = _opener_json(supplier, f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}")
                if detail_code != 200 or detail_payload.get("shipment_id") != shipment_id:
                        raise AssertionError("supplier role must read supplier shipment detail")
                patched_payload = json.loads(json.dumps(detail_payload, ensure_ascii=False))
                patch_code, patch_payload = _opener_patch_json(
                        supplier,
                        f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}",
                        {"shipment_date": "2026-05-15", "payload": patched_payload},
                    )
                if patch_code != 200 or patch_payload.get("shipment_date") != "2026-05-15":
                        raise AssertionError(f"supplier role must edit supplier shipments, got {patch_code} {patch_payload}")
                supplier_status_code, supplier_status_payload = _opener_patch_json(
                        supplier,
                        f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}",
                        {"order_status": "in_transit"},
                    )
                if supplier_status_code != 403 or supplier_status_payload.get("error") != "forbidden":
                        raise AssertionError("supplier role must not update supplier order_status")
                operator_status_code, operator_status_payload = _opener_patch_json(
                        operator,
                        f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}",
                        {"order_status": "accepted_ff"},
                    )
                if operator_status_code != 200 or operator_status_payload.get("order_status") != "accepted_ff":
                        raise AssertionError(f"operator role must update supplier order_status, got {operator_status_code} {operator_status_payload}")
                forbidden_html_code, _, _ = _opener_text(supplier, f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}")
                if forbidden_html_code != 403:
                        raise AssertionError("supplier role must not access full web-vitrina/operator shell")
                forbidden_operator_code, _, _ = _opener_text(supplier, f"{base_url}{DEFAULT_SHEET_OPERATOR_UI_PATH}")
                if forbidden_operator_code != 403:
                        raise AssertionError("supplier role must not access operator UI")
                forbidden_api_code, forbidden_api_payload = _opener_json(supplier, f"{base_url}{DEFAULT_SHEET_STATUS_PATH}")
                if forbidden_api_code != 403 or forbidden_api_payload.get("error") != "forbidden":
                        raise AssertionError("supplier role must not access unrelated operator APIs")
                forbidden_settings_code, _, _ = _opener_text(supplier, f"{base_url}{DEFAULT_SETTINGS_UI_PATH}")
                if forbidden_settings_code != 403:
                        raise AssertionError("supplier role must not access operator settings page")
                forbidden_nomenclature_code, forbidden_nomenclature_payload = _opener_json(
                        supplier,
                        f"{base_url}{DEFAULT_NOMENCLATURE_PATH}",
                    )
                if forbidden_nomenclature_code != 403 or forbidden_nomenclature_payload.get("error") != "forbidden":
                        raise AssertionError("supplier role must not access nomenclature API")
                forbidden_nomenclature_export_code, _, _ = _opener_text(
                        supplier,
                        f"{base_url}{DEFAULT_NOMENCLATURE_EXPORT_PATH}",
                    )
                if forbidden_nomenclature_export_code != 403:
                        raise AssertionError("supplier role must not export nomenclature XLSX")
                forbidden_nomenclature_import_code, forbidden_nomenclature_import_payload = _opener_post_multipart(
                        supplier,
                        f"{base_url}{DEFAULT_NOMENCLATURE_IMPORT_PATH}",
                        supplier_invoice_bytes,
                        filename="nomenclature.xlsx",
                    )
                if (
                    forbidden_nomenclature_import_code != 403
                    or forbidden_nomenclature_import_payload.get("error") != "forbidden"
                ):
                        raise AssertionError("supplier role must not import nomenclature XLSX")
                supplier_rematch_code, supplier_rematch_payload = _opener_post_json(
                        supplier,
                        f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}/rematch",
                        {"overwrite_manual": False},
                    )
                if supplier_rematch_code != 403 or supplier_rematch_payload.get("error") != "forbidden":
                        raise AssertionError("supplier role must not rematch supplier orders")
                supplier_delete_code, supplier_delete_payload = _opener_delete_json(
                        supplier,
                        f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}",
                    )
                if supplier_delete_code != 403 or supplier_delete_payload.get("error") != "forbidden":
                        raise AssertionError("supplier role must not delete supplier orders")
                operator_delete_code, operator_delete_payload = _opener_delete_json(
                        operator,
                        f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}",
                    )
                if operator_delete_code != 200 or operator_delete_payload.get("deleted") is not True:
                        raise AssertionError(f"operator role must delete supplier orders, got {operator_delete_code} {operator_delete_payload}")
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


def _opener_delete_json(opener: urllib_request.OpenerDirector, url: str) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(url, headers={"Accept": "application/json"}, method="DELETE")
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _opener_post_json(
    opener: urllib_request.OpenerDirector,
    url: str,
    payload: dict[str, object],
) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        method="POST",
    )
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _opener_patch_json(
    opener: urllib_request.OpenerDirector,
    url: str,
    payload: dict[str, object],
) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        method="PATCH",
    )
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _opener_post_multipart(
    opener: urllib_request.OpenerDirector,
    url: str,
    workbook_bytes: bytes,
    *,
    filename: str,
) -> tuple[int, dict[str, object]]:
    boundary = "----wbcore-supplier-auth" + uuid4().hex
    body = b"".join(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"),
            b"Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n",
            workbook_bytes,
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    request = urllib_request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "application/json"},
        method="POST",
    )
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


def _build_invoice_fixture() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Invoice"
    sheet.append(["Invoice No:", "26GN390"])
    sheet.append(["Invoice Date:", "14.5.2026"])
    sheet.append(["Contract No.", "CNT-2026-0513"])
    sheet.append(["Date of Contract", "2026.5.13"])
    sheet.append(["Supplier:", "Zhejiang Supplier", "", "Currency:", "USD"])
    sheet.append(["Invoice Total:", 33])
    sheet.append(["NO.", "NAME & SPECIFICATION", "MODELS", "QTY", "U.PRICE", "AMOUNT", "COMMENT"])
    sheet.append([1, "高清膜 smk", "iPhone 14 Pro", 10, 1, 10, ""])
    sheet.append([2, "防窥膜 (Anti-Spy)", "iPhone 14 Pro Max", 5, 2, 10, ""])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


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
