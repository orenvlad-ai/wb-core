"""HTTP boundary smoke for the Stage 3 facility/pool surfaces."""

from __future__ import annotations

import json
from http.cookiejar import CookieJar
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from urllib import error as urllib_error, request as urllib_request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_FF_POOL_FACILITIES_PATH,
    DEFAULT_FF_POOL_PATH,
    DEFAULT_FF_POOL_DOCUMENTS_PATH,
    DEFAULT_FF_POOL_OVERHEAD_PREVIEW_PATH,
    DEFAULT_FF_POOL_WB_SUPPLY_ORIGINS_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_SHEET_SUPPLIER_UI_PATH,
    DEFAULT_SETTINGS_UI_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application.ff_pool_surfaces import MAX_JSON_REQUEST_BYTES  # noqa: E402
from packages.contracts.ff_pool_documents import (  # noqa: E402
    OVERHEAD_PAYMENT_ORDER_MAX_REQUEST_BYTES,
    XlsxParserLimits,
)
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402
from apps.registry_upload_http_entrypoint_supplier_auth_smoke import (  # noqa: E402
    _login,
    _opener_json,
    _password_hash,
    _patched_env,
)


def main() -> None:
    with TemporaryDirectory(prefix="ff-pool-http-") as directory:
        runtime_dir = Path(directory) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
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
        server = build_registry_upload_http_server(
            config,
            entrypoint=RegistryUploadHttpEntrypoint(runtime_dir=runtime_dir, runtime=runtime),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{config.port}"
            _read_contract(base)
            _mutation_contract(base)
            _guided_acceptance_csrf_contract(base)
            _overhead_csrf_contract(base)
            _prebuffer_limit(config.port)
            _ui_contract(base)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    _authorization_contract()
    print("ff_pool_surfaces_http_smoke: OK")


def _read_contract(base: str) -> None:
    code, payload, headers = _json_request(f"{base}{DEFAULT_FF_POOL_PATH}/capabilities")
    assert code == 200 and payload["contract_name"] == "ff_facility_pool_surfaces_v1"
    assert payload["feature"]["reason"] == "feature_epoch_absent_default_off"
    assert payload["hidden_actions"] == ["facility_pool_opening"]
    visible_kinds = {item["document_kind"] for item in payload["document_kinds"]}
    action_kinds = {item["document_kind"] for item in payload["document_actions"]}
    assert "facility_pool_opening" not in visible_kinds
    assert {"inventory_surplus", "inventory_shortage"}.issubset(visible_kinds - action_kinds)
    etag = headers.get("ETag", "")
    assert etag.startswith('"sha256:')
    not_modified, _body, response_headers = _json_request(
        f"{base}{DEFAULT_FF_POOL_PATH}/capabilities",
        headers={"If-None-Match": etag},
    )
    assert not_modified == 304 and response_headers.get("ETag") == etag
    facilities_code, facilities, _ = _json_request(f"{base}{DEFAULT_FF_POOL_FACILITIES_PATH}")
    assert facilities_code == 200 and facilities["facilities"] == []


def _mutation_contract(base: str) -> None:
    body = {
        "request_id": "fixture:http:facility",
        "name": "HTTP fixture",
        "active": True,
        "display_timezone": "Asia/Yekaterinburg",
    }
    missing_code, missing, _ = _json_request(
        f"{base}{DEFAULT_FF_POOL_FACILITIES_PATH}", method="POST", payload=body
    )
    assert missing_code == 403 and missing["code"] == "csrf_failed"
    cross_code, cross, _ = _json_request(
        f"{base}{DEFAULT_FF_POOL_FACILITIES_PATH}",
        method="POST",
        payload=body,
        headers={
            "X-WB-FF-Pool-CSRF": "1",
            "Origin": "https://evil.example",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert cross_code == 403 and cross["code"] == "csrf_failed"
    off_code, off, _ = _json_request(
        f"{base}{DEFAULT_FF_POOL_FACILITIES_PATH}",
        method="POST",
        payload=body,
        headers={"X-WB-FF-Pool-CSRF": "1", "Sec-Fetch-Site": "same-origin"},
    )
    assert off_code == 409 and off["code"] == "facility_pool_feature_off"


def _guided_acceptance_csrf_contract(base: str) -> None:
    inventory_url = f"{base}{DEFAULT_FF_POOL_DOCUMENTS_PATH}/inventory/preview"
    missing_inventory_code, missing_inventory = _multipart_request(inventory_url)
    assert missing_inventory_code == 403 and missing_inventory["code"] == "csrf_failed"
    cross_inventory_code, cross_inventory = _multipart_request(
        inventory_url,
        headers={
            "X-WB-FF-Pool-CSRF": "1",
            "Origin": "https://evil.example",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert cross_inventory_code == 403 and cross_inventory["code"] == "csrf_failed"

    preview_url = f"{base}{DEFAULT_FF_POOL_DOCUMENTS_PATH}/china/preview"
    missing_preview_code, missing_preview = _multipart_request(preview_url)
    assert missing_preview_code == 403 and missing_preview["code"] == "csrf_failed"
    cross_preview_code, cross_preview = _multipart_request(
        preview_url,
        headers={
            "X-WB-FF-Pool-CSRF": "1",
            "Origin": "https://evil.example",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert cross_preview_code == 403 and cross_preview["code"] == "csrf_failed"
    guarded_preview_code, guarded_preview = _multipart_request(
        preview_url,
        headers={"X-WB-FF-Pool-CSRF": "1", "Sec-Fetch-Site": "same-origin"},
    )
    assert guarded_preview_code == 404 and guarded_preview["code"] == "supplier_shipment_not_found"

    confirm_url = f"{base}{DEFAULT_FF_POOL_PATH}/requests/missing-guided-request/confirm"
    confirm_body = {"confirm": True}
    missing_confirm_code, missing_confirm, _ = _json_request(
        confirm_url, method="POST", payload=confirm_body
    )
    assert missing_confirm_code == 403 and missing_confirm["code"] == "csrf_failed"
    cross_confirm_code, cross_confirm, _ = _json_request(
        confirm_url,
        method="POST",
        payload=confirm_body,
        headers={
            "X-WB-FF-Pool-CSRF": "1",
            "Origin": "https://evil.example",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert cross_confirm_code == 403 and cross_confirm["code"] == "csrf_failed"
    guarded_confirm_code, guarded_confirm, _ = _json_request(
        confirm_url,
        method="POST",
        payload=confirm_body,
        headers={"X-WB-FF-Pool-CSRF": "1", "Sec-Fetch-Site": "same-origin"},
    )
    assert guarded_confirm_code == 409 and guarded_confirm["code"] == "facility_pool_feature_off"


def _overhead_csrf_contract(base: str) -> None:
    body = {
        "request_id": "fixture:http:overhead",
        "facility_id": "fac_fixture",
        "scope": "FBS",
        "category": "storage",
        "comment": "",
        "amount_rub": "1.00",
    }
    missing_code, missing, _ = _json_request(
        f"{base}{DEFAULT_FF_POOL_OVERHEAD_PREVIEW_PATH}",
        method="POST",
        payload=body,
    )
    assert missing_code == 403 and missing["code"] == "csrf_failed"
    cross_code, cross, _ = _json_request(
        f"{base}{DEFAULT_FF_POOL_OVERHEAD_PREVIEW_PATH}",
        method="POST",
        payload=body,
        headers={
            "X-WB-FF-Pool-CSRF": "1",
            "Origin": "https://evil.example",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert cross_code == 403 and cross["code"] == "csrf_failed"
    off_code, off, _ = _json_request(
        f"{base}{DEFAULT_FF_POOL_OVERHEAD_PREVIEW_PATH}",
        method="POST",
        payload=body,
        headers={"X-WB-FF-Pool-CSRF": "1", "Sec-Fetch-Site": "same-origin"},
    )
    assert off_code == 409 and off["code"] == "facility_pool_feature_off"

    missing_pdf_code, missing_pdf = _multipart_request(
        f"{base}{DEFAULT_FF_POOL_OVERHEAD_PREVIEW_PATH}"
    )
    assert missing_pdf_code == 403 and missing_pdf["code"] == "csrf_failed"
    cross_pdf_code, cross_pdf = _multipart_request(
        f"{base}{DEFAULT_FF_POOL_OVERHEAD_PREVIEW_PATH}",
        headers={
            "X-WB-FF-Pool-CSRF": "1",
            "Origin": "https://evil.example",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert cross_pdf_code == 403 and cross_pdf["code"] == "csrf_failed"
    off_pdf_code, off_pdf = _multipart_request(
        f"{base}{DEFAULT_FF_POOL_OVERHEAD_PREVIEW_PATH}",
        headers={"X-WB-FF-Pool-CSRF": "1", "Sec-Fetch-Site": "same-origin"},
    )
    assert off_pdf_code == 409 and off_pdf["code"] == "facility_pool_feature_off"


def _multipart_request(
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    boundary = "----wb-core-guided-csrf-smoke"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="request_id"\r\n\r\n'
        "guided:http:csrf\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="business_date"\r\n\r\n'
        "2026-08-17\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="shipment_id"\r\n\r\n'
        "missing-shipment\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="expenses_json"\r\n\r\n'
        "[]\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="guided.xlsx"\r\n'
        "Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n"
        "not-a-valid-xlsx\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")
    request = urllib_request.Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            **(headers or {}),
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _prebuffer_limit(port: int) -> None:
    limit = XlsxParserLimits().max_request_bytes
    path = f"{DEFAULT_FF_POOL_DOCUMENTS_PATH}/inventory/preview"
    multipart_head = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Accept: application/json\r\n"
        "Content-Type: multipart/form-data; boundary=fixture\r\n"
        "X-WB-FF-Pool-CSRF: 1\r\n"
        "Sec-Fetch-Site: same-origin\r\n"
        f"Content-Length: {limit + 1}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    _assert_prebuffer_413(port, multipart_head)
    payment_head = (
        f"POST {DEFAULT_FF_POOL_OVERHEAD_PREVIEW_PATH} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Accept: application/json\r\n"
        "Content-Type: multipart/form-data; boundary=fixture\r\n"
        "X-WB-FF-Pool-CSRF: 1\r\n"
        "Sec-Fetch-Site: same-origin\r\n"
        f"Content-Length: {OVERHEAD_PAYMENT_ORDER_MAX_REQUEST_BYTES + 1}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    _assert_prebuffer_413(port, payment_head)
    json_head = (
        f"POST {DEFAULT_FF_POOL_FACILITIES_PATH} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Accept: application/json\r\n"
        "Content-Type: application/json\r\n"
        "X-WB-FF-Pool-CSRF: 1\r\n"
        "Sec-Fetch-Site: same-origin\r\n"
        f"Content-Length: {MAX_JSON_REQUEST_BYTES + 1}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    _assert_prebuffer_413(port, json_head)


def _assert_prebuffer_413(port: int, request_head: bytes) -> None:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.sendall(request_head)
        response = b""
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            response += chunk
    head, body = response.split(b"\r\n\r\n", 1)
    assert b" 413 " in head
    assert json.loads(body.decode("utf-8"))["code"] == "request_too_large"


def _ui_contract(base: str) -> None:
    with urllib_request.urlopen(f"{base}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}?tab=warehouses&warehouse=ff", timeout=10) as response:
        page = response.read().decode("utf-8")
    assert response.status == 200
    assert "data-ff-pool-open" in page and "Документы фулфилмента" in page
    assert "Явный 0 в полном FBS-шаблоне" in page
    assert "inventory-template.xlsx?" in page and "/documents/inventory" in page
    assert "Подтвердить проведение" in page
    assert 'data-ff-pool-overhead-category' in page
    assert 'data-ff-pool-overhead-file' in page
    assert 'data-ff-pool-open-overhead' in page
    assert '.ff-pool-dialog [hidden] { display: none !important; }' in page
    assert '/documents/overhead/preview' in page
    assert "Создание агрегатного документа выведено из эксплуатации" in page
    assert "facility_pool_opening" not in page
    assert "state.capabilities.document_kinds" in page
    assert page.count('data-warehouse-key="') >= 6
    with urllib_request.urlopen(f"{base}{DEFAULT_SETTINGS_UI_PATH}?embedded=1", timeout=10) as response:
        settings = response.read().decode("utf-8")
    assert 'data-settings-group-button="warehouses"' in settings
    assert "FF Москва — active" in settings and "production rows" in settings
    assert "supplierStatus=complete" in settings
    with urllib_request.urlopen(f"{base}{DEFAULT_SHEET_SUPPLIER_UI_PATH}?embedded=operator", timeout=10) as response:
        supplier = response.read().decode("utf-8")
    assert 'id="guidedAcceptanceButton"' in supplier
    assert "Принять на FF" in supplier and "Дата — read-only результат" in supplier


def _authorization_contract() -> None:
    owner_password = "ff-pool-owner-password"
    supply_password = "ff-pool-supply-password"
    supplier_password = "ff-pool-supplier-password"
    with TemporaryDirectory(prefix="ff-pool-http-auth-") as directory:
        runtime_dir = Path(directory) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime.save_sheet_vitrina_user(
            {
                "user_id": "usr_ff_pool_supply",
                "username": "ff_pool_supply",
                "display_name": "FF pool supply",
                "role": "supply_operator",
                "allowed_sections": ["supply"],
                "manage_users": False,
                "password_hash": _password_hash(supply_password),
                "is_active": True,
                "created_at": "2026-08-12T08:00:00Z",
                "updated_at": "2026-08-12T08:00:00Z",
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
                "WB_CORE_WEB_AUTH_USERNAME": "ff_pool_owner",
                "WB_CORE_WEB_AUTH_PASSWORD_HASH": _password_hash(owner_password),
                "WB_CORE_WEB_AUTH_SESSION_SECRET": "ff-pool-http-auth-session-secret",
                "WB_CORE_SUPPLIER_AUTH_USERNAME": "ff_pool_supplier",
                "WB_CORE_SUPPLIER_AUTH_PASSWORD_HASH": _password_hash(supplier_password),
                "WB_CORE_SUPPLIER_AUTH_DISPLAY_NAME": "FF pool supplier",
            }
        ):
            server = build_registry_upload_http_server(
                config,
                entrypoint=RegistryUploadHttpEntrypoint(runtime_dir=runtime_dir, runtime=runtime),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{config.port}"
                unauthenticated, _payload, _headers = _json_request(
                    f"{base}{DEFAULT_FF_POOL_PATH}/capabilities"
                )
                assert unauthenticated == 401
                supply = urllib_request.build_opener(urllib_request.HTTPCookieProcessor(CookieJar()))
                _login(supply, base, "ff_pool_supply", supply_password, DEFAULT_SHEET_WEB_VITRINA_UI_PATH)
                supply_code, supply_payload = _opener_json(
                    supply, f"{base}{DEFAULT_FF_POOL_PATH}/capabilities"
                )
                assert supply_code == 200 and supply_payload["contract_name"] == "ff_facility_pool_surfaces_v1"
                origin_code, origin_payload = _opener_json(
                    supply, f"{base}{DEFAULT_FF_POOL_WB_SUPPLY_ORIGINS_PATH}"
                )
                assert origin_code == 200 and origin_payload["contract_name"] == "ff_wb_supply_origin_assignments_v1"
                supplier = urllib_request.build_opener(urllib_request.HTTPCookieProcessor(CookieJar()))
                _login(supplier, base, "ff_pool_supplier", supplier_password, DEFAULT_SHEET_SUPPLIER_UI_PATH)
                supplier_code, supplier_payload = _opener_json(
                    supplier, f"{base}{DEFAULT_FF_POOL_PATH}/capabilities"
                )
                assert supplier_code == 403 and supplier_payload["error"] == "forbidden"
                supplier_origin_code, supplier_origin_payload = _opener_json(
                    supplier, f"{base}{DEFAULT_FF_POOL_WB_SUPPLY_ORIGINS_PATH}"
                )
                assert supplier_origin_code == 403 and supplier_origin_payload["error"] == "forbidden"
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


def _json_request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object], dict[str, str]]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib_request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib_request.urlopen(request, timeout=10) as response:
            raw = response.read()
            return response.status, json.loads(raw.decode("utf-8")) if raw else {}, dict(response.headers)
    except urllib_error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw.decode("utf-8")) if raw else {}, dict(exc.headers)


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


if __name__ == "__main__":
    main()
