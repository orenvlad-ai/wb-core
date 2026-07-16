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
import time
from urllib import error as urllib_error, parse as urllib_parse, request as urllib_request
from uuid import uuid4

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_FF_STOCKS_EXPORT_PATH,
    DEFAULT_FF_STOCKS_PATH,
    DEFAULT_FF_STOCKS_PREVIEW_PATH,
    DEFAULT_NOMENCLATURE_EXPORT_PATH,
    DEFAULT_NOMENCLATURE_IMPORT_PATH,
    DEFAULT_NOMENCLATURE_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SETTINGS_UI_PATH,
    DEFAULT_SETTINGS_USERS_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_SUPPLIER_UI_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_SUPPLIER_SHIPMENTS_PARSE_PATH,
    DEFAULT_SUPPLIER_SHIPMENTS_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


def main() -> None:
    owner_password = "owner-password-not-secret"
    supplier_password = "supplier-password-not-secret"
    supplier_invoice_bytes = _build_invoice_fixture()
    with TemporaryDirectory(prefix="supplier-auth-smoke-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        for item_id, nm_id, barcode, name, product_type, match_key, price in (
            ("supplier_auth_clear", 210183919, "1111111111111", "Clear iPhone 14 Pro", "clear", "clear|iphone_14_pro", 1.0),
            ("supplier_auth_anti", 210184534, "2222222222222", "Anti-Spy iPhone 14 Pro Max", "anti_spy", "anti_spy|iphone_14_pro_max", 2.0),
        ):
            runtime.save_nomenclature_item(
                {
                    "item_id": item_id,
                    "is_active": True,
                    "our_sku": "",
                    "nm_id": nm_id,
                    "barcode": barcode,
                    "nomenclature_name": name,
                    "product_type": product_type,
                    "match_key": match_key,
                    "purchase_price_yuan": price,
                    "aliases": [],
                    "compatible_models_text": "",
                    "compatible_model_keys": [],
                    "comment": "",
                    "created_at": "2026-05-30T08:00:00Z",
                    "updated_at": "2026-05-30T08:00:00Z",
                }
            )
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
            server = build_registry_upload_http_server(
                config,
                entrypoint=RegistryUploadHttpEntrypoint(runtime_dir=runtime_dir, runtime=runtime),
            )
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
                if '"can_recheck_prices": false' not in operator_supplier:
                    raise AssertionError("standalone supplier route must not expose manual price recheck even to operator role")
                if '<button id="priceCheckButton"' in operator_supplier or ">Проверить цены<" in operator_supplier:
                    raise AssertionError("standalone supplier route must not render manual price recheck button")
                operator_embedded_code, _, operator_embedded = _opener_text(
                    operator,
                    f"{base_url}{DEFAULT_SHEET_SUPPLIER_UI_PATH}?embedded=operator",
                )
                if (
                    operator_embedded_code != 200
                    or '"can_recheck_prices": true' not in operator_embedded
                    or '<button id="priceCheckButton"' not in operator_embedded
                ):
                    raise AssertionError("operator embedded supplier module must keep manual price recheck button")

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
                if (
                    '"can_delete_shipments": false' not in supplier_page
                    or '"can_edit_order_status": false' not in supplier_page
                    or '"can_recheck_prices": false' not in supplier_page
                ):
                        raise AssertionError("supplier page must not render operator-only shipment controls for supplier role")
                if '<button id="priceCheckButton"' in supplier_page or ">Проверить цены<" in supplier_page:
                        raise AssertionError("supplier page must not include manual price recheck button markup")
                if "价格匹配 / Price check / Соответствие цены" not in supplier_page:
                        raise AssertionError("supplier page must expose multilingual price conformity column header")
                if "计划出货日期 / Planned shipment date / Плановая дата отгрузки" not in supplier_page:
                        raise AssertionError("supplier page must expose planned shipment date label")
                if "实际出货日期 / Actual shipment date / Фактическая дата отгрузки" not in supplier_page:
                        raise AssertionError("supplier page must expose actual shipment date label")
                if "实际入仓日期 / Actual ФФ acceptance date / Фактическая дата приёмки на ФФ" not in supplier_page:
                        raise AssertionError("supplier page must expose actual FF acceptance date label")
                if "预估人民币汇率 / Estimated CNY rate / Примерный курс юаня, ₽/¥" not in supplier_page:
                        raise AssertionError("supplier page must expose approximate yuan rate label")
                if "预估成本 / Est. cost / Ориент. себестоимость, ₽/шт" not in supplier_page:
                        raise AssertionError("supplier page must expose approximate landed cost column")
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
                            "actual_shipment_date": "2026-05-16",
                            "payload": parse_payload,
                        },
                    )
                if create_code != 200 or not create_payload.get("shipment_id"):
                        raise AssertionError(f"supplier role must create supplier shipments, got {create_code} {create_payload}")
                if create_payload.get("order_status") != "in_transit":
                        raise AssertionError("supplier create status must derive from factual shipment date")
                if (
                    create_payload.get("planned_shipment_date") != "2026-05-14"
                    or create_payload.get("actual_shipment_date") != "2026-05-16"
                    or create_payload.get("actual_ff_acceptance_date") != ""
                ):
                        raise AssertionError("supplier role must create planned/fact shipment dates")
                shipment_id = str(create_payload["shipment_id"])
                detail_code, detail_payload = _opener_json(supplier, f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}")
                if detail_code != 200 or detail_payload.get("shipment_id") != shipment_id:
                        raise AssertionError("supplier role must read supplier shipment detail")
                if detail_payload.get("actual_ff_acceptance_date") != "":
                        raise AssertionError("supplier role detail must keep blank actual FF acceptance date until saved")
                supplier_price_check_code, supplier_price_check_payload = _opener_post_json(
                        supplier,
                        f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}/price-check",
                        {"context": {"source": "supplier_auth_smoke"}},
                    )
                if supplier_price_check_code != 403 or supplier_price_check_payload.get("error") != "forbidden":
                        raise AssertionError("supplier role must not manually recheck supplier shipment prices")
                operator_price_check_code, operator_price_check_payload = _opener_post_json(
                        operator,
                        f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}/price-check",
                        {"context": {"source": "supplier_auth_smoke"}},
                    )
                operator_price_checked_line = operator_price_check_payload.get("product_lines", [{}])[0]
                if (
                    operator_price_check_code != 200
                    or operator_price_checked_line.get("price_conformity_check_mode") != "manual_recheck"
                    or operator_price_checked_line.get("price_conformity_context", {}).get("source") != "supplier_auth_smoke"
                ):
                        raise AssertionError(f"operator role must manually recheck supplier shipment prices, got {operator_price_check_code} {operator_price_check_payload}")
                detail_payload = operator_price_check_payload
                patched_payload = json.loads(json.dumps(detail_payload, ensure_ascii=False))
                patch_code, patch_payload = _opener_patch_json(
                        supplier,
                        f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}",
                        {
                            "shipment_date": "2026-05-15",
                            "actual_shipment_date": "2026-05-16",
                            "actual_ff_acceptance_date": "2026-05-30",
                            "payload": patched_payload,
                        },
                    )
                if (
                    patch_code != 200
                    or patch_payload.get("shipment_date") != "2026-05-15"
                    or patch_payload.get("actual_shipment_date") != "2026-05-16"
                    or patch_payload.get("actual_ff_acceptance_date") != "2026-05-30"
                    or patch_payload.get("order_status") != "accepted_ff"
                ):
                        raise AssertionError(f"supplier role must edit supplier shipments, got {patch_code} {patch_payload}")
                correction_code, correction_payload = _opener_patch_json(
                        supplier,
                        f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}",
                        {"actual_shipment_date": "2026-05-17"},
                    )
                if correction_code != 202 or correction_payload.get("status") != "accepted":
                        raise AssertionError(f"supplier role must start guarded factual correction, got {correction_code} {correction_payload}")
                correction_result = _wait_supplier_correction(supplier, base_url, shipment_id)
                if correction_result.get("status") != "success":
                        raise AssertionError(f"supplier factual correction failed: {correction_result}")
                detail_code, patch_payload = _opener_json(
                        supplier,
                        f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}",
                    )
                if detail_code != 200 or patch_payload.get("actual_shipment_date") != "2026-05-17":
                        raise AssertionError(f"supplier factual correction readback failed: {patch_payload}")
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
                        {"order_status": "production"},
                    )
                if operator_status_code != 400 or "вычисляется" not in operator_status_payload.get("error", ""):
                        raise AssertionError(f"operator role must not set a divergent status, got {operator_status_code} {operator_status_payload}")
                forbidden_html_code, _, _ = _opener_text(supplier, f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}")
                if forbidden_html_code != 403:
                        raise AssertionError("supplier role must not access full web-vitrina/operator shell")
                forbidden_operator_code, _, _ = _opener_text(supplier, f"{base_url}{DEFAULT_SHEET_OPERATOR_UI_PATH}")
                if forbidden_operator_code != 403:
                        raise AssertionError("supplier role must not access operator UI")
                forbidden_api_code, forbidden_api_payload = _opener_json(supplier, f"{base_url}{DEFAULT_SHEET_STATUS_PATH}")
                if forbidden_api_code != 403 or forbidden_api_payload.get("error") != "forbidden":
                        raise AssertionError("supplier role must not access unrelated operator APIs")
                forbidden_ff_stock_code, forbidden_ff_stock_payload = _opener_json(
                    supplier,
                    f"{base_url}{DEFAULT_FF_STOCKS_PATH}",
                )
                if forbidden_ff_stock_code != 403 or forbidden_ff_stock_payload.get("error") != "forbidden":
                        raise AssertionError("supplier role must not access ФФ stock ledger API")
                forbidden_ff_export_code, _, _ = _opener_text(
                        supplier,
                        f"{base_url}{DEFAULT_FF_STOCKS_EXPORT_PATH}",
                    )
                if forbidden_ff_export_code != 403:
                        raise AssertionError("supplier role must not export ФФ stock ledger XLSX")
                forbidden_ff_preview_code, forbidden_ff_preview_payload = _opener_post_multipart(
                        supplier,
                        f"{base_url}{DEFAULT_FF_STOCKS_PREVIEW_PATH}",
                        supplier_invoice_bytes,
                        filename="ff-stock.xlsx",
                        fields={"operation_type": "manual_receipt"},
                    )
                if forbidden_ff_preview_code != 403 or forbidden_ff_preview_payload.get("error") != "forbidden":
                        raise AssertionError("supplier role must not preview ФФ stock manual documents")
                forbidden_settings_code, _, _ = _opener_text(supplier, f"{base_url}{DEFAULT_SETTINGS_UI_PATH}")
                if forbidden_settings_code != 403:
                    raise AssertionError("supplier role must not access operator settings page")
                forbidden_users_code, forbidden_users_payload = _opener_json(supplier, f"{base_url}{DEFAULT_SETTINGS_USERS_PATH}")
                if forbidden_users_code != 403 or forbidden_users_payload.get("error") != "forbidden":
                    raise AssertionError("supplier role must not access users API")
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


def _wait_supplier_correction(
    opener: urllib_request.OpenerDirector,
    base_url: str,
    shipment_id: str,
) -> dict[str, object]:
    url = f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}/factual-date-correction"
    for _ in range(200):
        status, payload = _opener_json(opener, url)
        if status == 200 and payload.get("status") in {"success", "error"}:
            return payload
        time.sleep(0.02)
    raise AssertionError("supplier factual correction did not become terminal")


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
    fields: dict[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    boundary = "----wbcore-supplier-auth" + uuid4().hex
    body_parts: list[bytes] = []
    for key, value in (fields or {}).items():
        body_parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    body_parts.extend(
        [
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                "Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n"
            ).encode("utf-8"),
            workbook_bytes,
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    body = b"".join(body_parts)
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
    sheet.append(["NO.", "MODELS", "NAME & SPECIFICATION", "Barcode", "QTY", "U.PRICE", "AMOUNT", "COMMENT"])
    sheet.append([1, "iPhone 14 Pro", "高清膜 smk", "1111111111111", 10, 1, 10, ""])
    sheet.append([2, "iPhone 14 Pro Max", "防窥膜 (Anti-Spy)", "2222222222222", 5, 2, 10, ""])
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
