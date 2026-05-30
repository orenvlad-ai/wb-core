"""HTTP smoke-check for supplier invoice shipment parse/storage/API routes."""

from __future__ import annotations

from io import BytesIO
import hashlib
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from urllib import error as urllib_error, request as urllib_request
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
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


def main() -> None:
    workbook_bytes = _build_invoice_fixture()
    workbook_sha256 = hashlib.sha256(workbook_bytes).hexdigest()
    with TemporaryDirectory(prefix="supplier-shipments-http-") as tmp:
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
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: "2026-05-30T08:00:00Z",
        )
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{config.port}"
            list_status, list_payload = _get_json(f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}")
            if list_status != 200 or list_payload.get("shipments") != []:
                raise AssertionError(f"empty registry must load, got {list_status} {list_payload}")

            parse_status, parse_payload = _post_multipart(
                f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PARSE_PATH}",
                workbook_bytes,
                filename="PI-test 26GN390 (14.5.2026).xlsx",
            )
            if parse_status != 200 or not parse_payload.get("upload_id"):
                raise AssertionError(f"parse route must stage upload and return editable payload, got {parse_status} {parse_payload}")
            if parse_payload.get("source_file_sha256") != workbook_sha256:
                raise AssertionError("parse route must expose sha256 of original upload")

            missing_date_status, missing_date_payload = _post_json(
                f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}",
                {"upload_id": parse_payload["upload_id"], "payload": parse_payload},
            )
            if missing_date_status != 400 or "shipment_date" not in str(missing_date_payload.get("error", "")):
                raise AssertionError("create must reject missing shipment_date")

            create_status, detail = _post_json(
                f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}",
                {
                    "upload_id": parse_payload["upload_id"],
                    "shipment_date": "2026-05-14",
                    "payload": parse_payload,
                },
            )
            if create_status != 200 or not detail.get("shipment_id"):
                raise AssertionError(f"create route must persist shipment, got {create_status} {detail}")
            shipment_id = detail["shipment_id"]
            if detail.get("shipment_date") != "2026-05-14" or detail.get("match_status") != "has_unmatched":
                raise AssertionError("created shipment must keep date and unmatched status")
            if len(detail.get("product_lines", [])) != 2 or len(detail.get("extra_lines", [])) != 1:
                raise AssertionError("detail must split product and extra lines")

            detail_status, loaded_detail = _get_json(f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}")
            if detail_status != 200 or loaded_detail.get("shipment_id") != shipment_id:
                raise AssertionError("detail route must return persisted card payload")

            edited = json.loads(json.dumps(loaded_detail, ensure_ascii=False))
            edited["lines"][0]["internal_sku"] = "SKU-MANUAL"
            edited["lines"][0]["internal_nm_id"] = 123456
            edited["lines"][0]["internal_name"] = "Manual SKU"
            edited["lines"][0]["match_status"] = "matched"
            edited["lines"][0]["amount"] = 12
            edited["metadata"]["declared_invoice_total"] = 27
            patch_status, patched = _patch_json(
                f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}",
                {"shipment_date": "2026-05-15", "payload": edited},
            )
            if patch_status != 200 or patched.get("shipment_date") != "2026-05-15":
                raise AssertionError(f"patch route must update shipment date, got {patch_status} {patched}")
            if patched.get("match_status") != "manual_override" or patched.get("summary", {}).get("product_amount_total") != 22.0:
                raise AssertionError("patch route must mark manual_override and recalculate totals server-side")

            registry_status, registry_payload = _get_json(f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}")
            if registry_status != 200 or len(registry_payload.get("shipments", [])) != 1:
                raise AssertionError("list route must expose saved shipment")
            invoice_path = registry_payload["shipments"][0].get("invoice_download_path")
            invoice_status, invoice_bytes, invoice_headers = _get_bytes(f"{base_url}{invoice_path}")
            if invoice_status != 200 or hashlib.sha256(invoice_bytes).hexdigest() != workbook_sha256:
                raise AssertionError("invoice download must preserve original XLSX bytes")
            if "attachment" not in str(invoice_headers.get("Content-Disposition", "")):
                raise AssertionError("invoice download must be an attachment")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print("sheet_vitrina_v1_supplier_shipments_http_smoke: OK")


def _build_invoice_fixture() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Invoice"
    sheet.append(["Invoice No:", "26GN390"])
    sheet.append(["Invoice Date:", "14.5.2026"])
    sheet.append(["Supplier:", "Zhejiang Supplier", "", "Currency:", "USD"])
    sheet.append(["Invoice Total:", 25])
    sheet.append(["NO.", "NAME & SPECIFICATION", "MODELS", "QTY", "U.PRICE", "AMOUNT", "COMMENT"])
    sheet.append([1, "高清膜 smk", "iPhone 14 Pro", 10, 1, 10, ""])
    sheet.append([2, "防窥膜 (Anti-Spy)", "iPhone 14 Pro Max", 5, 2, 10, ""])
    sheet.append([3, "OPP bag packets", "", 100, 0.05, 5, "OPP packets"])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _post_multipart(url: str, workbook_bytes: bytes, *, filename: str) -> tuple[int, dict[str, object]]:
    boundary = "----wbcore-supplier" + uuid4().hex
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
    return _open_json(request)


def _post_json(url: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        method="POST",
    )
    return _open_json(request)


def _patch_json(url: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        method="PATCH",
    )
    return _open_json(request)


def _get_json(url: str) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(url, headers={"Accept": "application/json"}, method="GET")
    return _open_json(request)


def _open_json(request: urllib_request.Request) -> tuple[int, dict[str, object]]:
    try:
        with urllib_request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_bytes(url: str) -> tuple[int, bytes, dict[str, str]]:
    request = urllib_request.Request(url, method="GET")
    try:
        with urllib_request.urlopen(request, timeout=5) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib_error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
