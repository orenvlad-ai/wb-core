"""Smoke-check trade document registry for supplier invoices/contracts."""

from __future__ import annotations

from contextlib import contextmanager
from http.cookiejar import CookieJar
from io import BytesIO
import base64
import hashlib
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
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SUPPLIER_SHIPMENTS_PARSE_PATH,
    DEFAULT_SUPPLIER_SHIPMENTS_PATH,
    DEFAULT_TRADE_DOCUMENTS_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


def main() -> None:
    owner_password = "owner-password-not-secret"
    supplier_password = "supplier-password-not-secret"
    invoice_bytes = _build_invoice_fixture()
    contract_bytes = b"%PDF-1.4\n% wb-core trade document smoke\n"
    with TemporaryDirectory(prefix="trade-documents-smoke-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
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
        clock = _Clock()
        with _patched_env(
            {
                "WB_CORE_WEB_AUTH_REQUIRED": "1",
                "WB_CORE_WEB_AUTH_USERNAME": "owner",
                "WB_CORE_WEB_AUTH_PASSWORD_HASH": _password_hash(owner_password),
                "WB_CORE_WEB_AUTH_SESSION_SECRET": "trade-documents-smoke-session-secret",
                "WB_CORE_SUPPLIER_AUTH_USERNAME": "supplier",
                "WB_CORE_SUPPLIER_AUTH_PASSWORD_HASH": _password_hash(supplier_password),
                "WB_CORE_SUPPLIER_AUTH_DISPLAY_NAME": "Supplier",
            }
        ):
            entrypoint = RegistryUploadHttpEntrypoint(
                runtime_dir=runtime_dir,
                runtime=runtime,
                activated_at_factory=clock.next,
            )
            server = build_registry_upload_http_server(config, entrypoint=entrypoint)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{config.port}"
                operator = urllib_request.build_opener(urllib_request.HTTPCookieProcessor(CookieJar()))
                supplier = urllib_request.build_opener(urllib_request.HTTPCookieProcessor(CookieJar()))
                _login(operator, base_url, "owner", owner_password)
                _login(supplier, base_url, "supplier", supplier_password)

                contract_code, contract_payload = _opener_post_multipart(
                    operator,
                    f"{base_url}{DEFAULT_TRADE_DOCUMENTS_PATH}",
                    contract_bytes,
                    filename="contract-cnt-2026-0513.pdf",
                    content_type="application/pdf",
                    fields={
                        "document_type": "contract",
                        "number": "CNT-2026-0513",
                        "document_date": "2026-05-13",
                        "supplier_name": "HanShang Technology",
                    },
                )
                contract_doc = contract_payload.get("document") or {}
                contract_id = str(contract_doc.get("document_id") or "")
                if contract_code != 200 or not contract_id or contract_doc.get("document_type") != "contract":
                    raise AssertionError(f"contract upload failed: {contract_code} {contract_payload}")

                duplicate_code, duplicate_payload = _opener_post_multipart(
                    operator,
                    f"{base_url}{DEFAULT_TRADE_DOCUMENTS_PATH}",
                    contract_bytes,
                    filename="contract-cnt-2026-0513.pdf",
                    content_type="application/pdf",
                    fields={"document_type": "contract", "number": "CNT-2026-0513"},
                )
                if (
                    duplicate_code != 200
                    or duplicate_payload.get("status") != "duplicate_existing"
                    or (duplicate_payload.get("document") or {}).get("document_id") != contract_id
                ):
                    raise AssertionError(f"settings duplicate upload must return existing document, got {duplicate_code} {duplicate_payload}")

                invoice_code, invoice_payload = _opener_post_multipart(
                    operator,
                    f"{base_url}{DEFAULT_TRADE_DOCUMENTS_PATH}",
                    invoice_bytes,
                    filename="PI-test 26GN390.xlsx",
                    fields={"document_type": "invoice"},
                )
                invoice_doc = invoice_payload.get("document") or {}
                invoice_id = str(invoice_doc.get("document_id") or "")
                if (
                    invoice_code != 200
                    or invoice_doc.get("number") != "26GN390"
                    or invoice_doc.get("document_date") != "2026-05-14"
                    or invoice_doc.get("parsed_metadata", {}).get("contract_no") != "CNT-2026-0513"
                ):
                    raise AssertionError(f"settings invoice upload must parse metadata, got {invoice_code} {invoice_payload}")

                empty_shipments_code, empty_shipments_payload = _opener_json(
                    operator,
                    f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}",
                )
                if empty_shipments_code != 200 or empty_shipments_payload.get("shipments") != []:
                    raise AssertionError("settings invoice upload must not create supplier shipment")

                link_code, link_payload = _opener_patch_json(
                    operator,
                    f"{base_url}{DEFAULT_TRADE_DOCUMENTS_PATH}/{invoice_id}/contract",
                    {"contract_document_id": contract_id},
                )
                if link_code != 200 or link_payload.get("link", {}).get("contract_document_id") != contract_id:
                    raise AssertionError(f"invoice->contract link failed: {link_code} {link_payload}")

                for document, expected_sha in ((contract_doc, hashlib.sha256(contract_bytes).hexdigest()), (invoice_doc, hashlib.sha256(invoice_bytes).hexdigest())):
                    path = str(document.get("download_path") or "")
                    status, body, headers = _opener_bytes(operator, f"{base_url}{path}")
                    if status != 200 or hashlib.sha256(body).hexdigest() != expected_sha:
                        raise AssertionError(f"document download mismatch for {path}: {status} {headers}")

                archive_contract_code, archive_contract_payload = _opener_delete_json(
                    operator,
                    f"{base_url}{DEFAULT_TRADE_DOCUMENTS_PATH}/{contract_id}",
                )
                if archive_contract_code != 400 or "linked invoice" not in str(archive_contract_payload.get("error", "")).lower():
                    raise AssertionError(f"linked contract archive must be rejected, got {archive_contract_code} {archive_contract_payload}")

                supplier_settings_code, supplier_settings_payload = _opener_json(
                    supplier,
                    f"{base_url}{DEFAULT_TRADE_DOCUMENTS_PATH}",
                )
                if supplier_settings_code != 403 or supplier_settings_payload.get("error") != "forbidden":
                    raise AssertionError("supplier role must not list settings documents")
                supplier_arbitrary_file_code, _, _ = _opener_bytes(
                    supplier,
                    f"{base_url}{contract_doc.get('download_path')}",
                )
                if supplier_arbitrary_file_code != 403:
                    raise AssertionError("supplier role must not download arbitrary settings document file")

                parse_code, parse_payload = _opener_post_multipart(
                    supplier,
                    f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PARSE_PATH}",
                    invoice_bytes,
                    filename="supplier-flow.xlsx",
                )
                if parse_code != 200 or not parse_payload.get("upload_id"):
                    raise AssertionError(f"supplier parse failed: {parse_code} {parse_payload}")
                create_code, create_payload = _opener_post_json(
                    supplier,
                    f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}",
                    {"upload_id": parse_payload["upload_id"], "shipment_date": "2026-05-14", "payload": parse_payload},
                )
                shipment_id = str(create_payload.get("shipment_id") or "")
                if (
                    create_code != 200
                    or not shipment_id
                    or not create_payload.get("invoice_document_id")
                    or create_payload.get("contract_document_id") != contract_id
                    or not create_payload.get("contract_download_path")
                ):
                    raise AssertionError(f"supplier shipment must auto-create/link documents, got {create_code} {create_payload}")
                supplier_contract_code, supplier_contract_bytes, _ = _opener_bytes(
                    supplier,
                    f"{base_url}{create_payload.get('contract_download_path')}",
                )
                if supplier_contract_code != 200 or supplier_contract_bytes != contract_bytes:
                    raise AssertionError("supplier role must download shipment-linked contract")

                legacy_file = runtime_dir / "supplier_invoices" / "files" / "sup_legacy_doc" / "legacy.xlsx"
                legacy_file.parent.mkdir(parents=True, exist_ok=True)
                legacy_file.write_bytes(invoice_bytes)
                runtime.save_supplier_shipment(
                    header={
                        "shipment_id": "sup_legacy_doc",
                        "created_at": "2026-05-30T08:10:00Z",
                        "updated_at": "2026-05-30T08:10:00Z",
                        "shipment_date": "2026-05-16",
                        "invoice_no": "LEGACY-DOC",
                        "invoice_date": "2026-05-15",
                        "contract_no": "CNT-2026-0513",
                        "contract_date": "2026-05-13",
                        "supplier_name": "",
                        "customer_name": "",
                        "currency": "RMB",
                        "product_qty_total": 0,
                        "product_amount_total": 0,
                        "extras_amount_total": 0,
                        "invoice_amount_total": 0,
                        "declared_invoice_total": 0,
                        "match_status": "all_matched",
                        "source_filename": "legacy.xlsx",
                        "source_file_sha256": hashlib.sha256(invoice_bytes).hexdigest(),
                        "source_file_path": legacy_file.relative_to(runtime_dir).as_posix(),
                        "parser_version": "legacy",
                        "warnings": [],
                        "errors": [],
                    },
                    lines=[],
                )
                before_docs_code, before_docs = _opener_json(operator, f"{base_url}{DEFAULT_TRADE_DOCUMENTS_PATH}")
                before_count = len(before_docs.get("documents", []))
                legacy_detail_code, legacy_detail = _opener_json(
                    operator,
                    f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/sup_legacy_doc",
                )
                if (
                    legacy_detail_code != 200
                    or not legacy_detail.get("invoice_document_id")
                    or legacy_detail.get("contract_document_id") != contract_id
                ):
                    raise AssertionError(f"legacy migration must create and link invoice document, got {legacy_detail_code} {legacy_detail}")
                after_docs_code, after_docs = _opener_json(operator, f"{base_url}{DEFAULT_TRADE_DOCUMENTS_PATH}")
                second_legacy_code, _ = _opener_json(operator, f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/sup_legacy_doc")
                final_docs_code, final_docs = _opener_json(operator, f"{base_url}{DEFAULT_TRADE_DOCUMENTS_PATH}")
                if (
                    after_docs_code != 200
                    or second_legacy_code != 200
                    or final_docs_code != 200
                    or len(after_docs.get("documents", [])) != before_count + 1
                    or len(final_docs.get("documents", [])) != len(after_docs.get("documents", []))
                ):
                    raise AssertionError("legacy migration must be idempotent")
                delete_legacy_code, _ = _opener_delete_json(
                    operator,
                    f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/sup_legacy_doc",
                )
                if delete_legacy_code != 200 or not legacy_file.exists():
                    raise AssertionError("supplier shipment delete must not remove physical legacy invoice file")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
    print("sheet_vitrina_v1_trade_documents_smoke: OK")


class _Clock:
    def __init__(self) -> None:
        self.value = 0

    def next(self) -> str:
        self.value += 1
        return f"2026-05-30T08:{self.value:02d}:00Z"


def _build_invoice_fixture() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Invoice"
    sheet.append(["Invoice No:", "26GN390"])
    sheet.append(["Invoice Date:", "14.5.2026"])
    sheet.append(["Contract No.", "CNT-2026-0513"])
    sheet.append(["Date of Contract", "2026.5.13"])
    sheet.append(["Supplier:", "HanShang Technology", "", "Currency:", "RMB"])
    sheet.append(["Invoice Total:", 15])
    sheet.append(["NO.", "NAME & SPECIFICATION", "MODELS", "QTY", "U.PRICE", "AMOUNT", "COMMENT"])
    sheet.append([1, "高清膜 smk", "iPhone 14 Pro", 10, 1, 10, ""])
    sheet.append([2, "OPP bag packets", "", 100, 0.05, 5, "OPP packets"])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _login(opener: urllib_request.OpenerDirector, base_url: str, username: str, password: str) -> None:
    body = urllib_parse.urlencode({"username": username, "password": password, "next": "/sheet-vitrina-v1/supplier"}).encode("utf-8")
    request = urllib_request.Request(
        f"{base_url}/login",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "text/html"},
        method="POST",
    )
    with opener.open(request, timeout=5) as response:
        response.read()


def _opener_post_multipart(
    opener: urllib_request.OpenerDirector,
    url: str,
    body_bytes: bytes,
    *,
    filename: str,
    content_type: str = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    fields: dict[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    boundary = "----wbcore-trade-documents" + uuid4().hex
    chunks: list[bytes] = []
    for key, value in (fields or {}).items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    chunks.extend(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"),
            f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
            body_bytes,
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    request = urllib_request.Request(
        url,
        data=b"".join(chunks),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "application/json"},
        method="POST",
    )
    return _opener_json_request(opener, request)


def _opener_post_json(opener: urllib_request.OpenerDirector, url: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        method="POST",
    )
    return _opener_json_request(opener, request)


def _opener_patch_json(opener: urllib_request.OpenerDirector, url: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        method="PATCH",
    )
    return _opener_json_request(opener, request)


def _opener_delete_json(opener: urllib_request.OpenerDirector, url: str) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(url, headers={"Accept": "application/json"}, method="DELETE")
    return _opener_json_request(opener, request)


def _opener_json(opener: urllib_request.OpenerDirector, url: str) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(url, headers={"Accept": "application/json"}, method="GET")
    return _opener_json_request(opener, request)


def _opener_json_request(opener: urllib_request.OpenerDirector, request: urllib_request.Request) -> tuple[int, dict[str, object]]:
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _opener_bytes(opener: urllib_request.OpenerDirector, url: str) -> tuple[int, bytes, dict[str, str]]:
    request = urllib_request.Request(url, headers={"Accept": "*/*"}, method="GET")
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, response.read(), dict(response.headers.items())
    except urllib_error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers.items())


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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


def _password_hash(password: str) -> str:
    salt = b"trade-documents-smoke-salt"
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return "pbkdf2_sha256$100000$" + base64.b64encode(salt).decode("ascii") + "$" + base64.b64encode(digest).decode("ascii")


if __name__ == "__main__":
    main()
