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
    DEFAULT_NOMENCLATURE_PATH,
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

            unsupported_status, unsupported_payload = _post_multipart(
                f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PARSE_PATH}",
                b"not an invoice",
                filename="invoice.txt",
                content_type="text/plain",
            )
            if unsupported_status != 400 or "xlsx" not in str(unsupported_payload.get("error", "")).lower():
                raise AssertionError(f"unsupported file type must return controlled JSON 400, got {unsupported_status} {unsupported_payload}")

            nomenclature_status, nomenclature_payload = _get_json(f"{base_url}{DEFAULT_NOMENCLATURE_PATH}")
            if nomenclature_status != 200 or nomenclature_payload.get("items") != []:
                raise AssertionError(f"empty nomenclature must load, got {nomenclature_status} {nomenclature_payload}")
            create_nom_status, create_nom_payload = _post_json(
                f"{base_url}{DEFAULT_NOMENCLATURE_PATH}",
                {
                    "is_active": True,
                    "our_sku": "SKU-CLEAR-14P",
                    "nm_id": 210183919,
                    "nomenclature_name": "Clear iPhone 14 Pro",
                    "product_type": "clear",
                    "match_key": "clear|iphone_14_pro",
                    "aliases": ["iPhone 14 Pro"],
                    "compatible_models_text": "iPhone 14 Pro",
                    "comment": "smoke",
                },
            )
            if create_nom_status != 200 or create_nom_payload.get("item", {}).get("nm_id") != 210183919:
                raise AssertionError(f"nomenclature create must persist item, got {create_nom_status} {create_nom_payload}")
            if create_nom_payload.get("item", {}).get("compatible_model_keys") != ["iphone_14_pro"]:
                raise AssertionError("nomenclature create must normalize compatible model keys")
            duplicate_nom_status, duplicate_nom_payload = _post_json(
                f"{base_url}{DEFAULT_NOMENCLATURE_PATH}",
                {
                    "is_active": True,
                    "nomenclature_name": "Duplicate",
                    "product_type": "clear",
                    "match_key": "clear|iphone_14_pro",
                },
            )
            if duplicate_nom_status != 400 or "duplicate" not in str(duplicate_nom_payload.get("error", "")).lower():
                raise AssertionError(f"duplicate active match_key must be rejected, got {duplicate_nom_status} {duplicate_nom_payload}")
            compat_nom_status, compat_nom_payload = _post_json(
                f"{base_url}{DEFAULT_NOMENCLATURE_PATH}",
                {
                    "is_active": True,
                    "our_sku": "SKU-AS-141313P",
                    "nm_id": 391662410,
                    "nomenclature_name": "anti-spy iPhone 14 / 13 / 13Pro",
                    "product_type": "anti_spy",
                    "match_key": "anti_spy|iphone_14_13_13pro",
                    "compatible_models_text": "iPhone 14, iPhone 13, iPhone 13 Pro",
                    "comment": "compatibility smoke",
                },
            )
            if compat_nom_status != 200 or compat_nom_payload.get("item", {}).get("compatible_model_keys") != [
                "iphone_14",
                "iphone_13",
                "iphone_13_pro",
            ]:
                raise AssertionError(f"compatible nomenclature item must save normalized keys, got {compat_nom_status} {compat_nom_payload}")

            parse_status, parse_payload = _post_multipart(
                f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PARSE_PATH}",
                workbook_bytes,
                filename="PI-test 26GN390 (14.5.2026).xlsx",
            )
            if parse_status != 200 or not parse_payload.get("upload_id"):
                raise AssertionError(f"parse route must stage upload and return editable payload, got {parse_status} {parse_payload}")
            if parse_payload.get("source_file_sha256") != workbook_sha256:
                raise AssertionError("parse route must expose sha256 of original upload")
            product_lines = [item for item in parse_payload.get("lines", []) if item.get("line_type") == "product"]
            if product_lines[0].get("internal_nm_id") != 210183919 or product_lines[0].get("match_status") != "matched":
                raise AssertionError("parse route must resolve active nomenclature match_key into nmId/name")
            if (
                product_lines[1].get("match_status") != "matched_by_compatibility"
                or product_lines[1].get("internal_nm_id") != 391662410
            ):
                raise AssertionError(f"parse route must resolve compatible model overlap, got {product_lines[1]}")
            if product_lines[2].get("match_status") != "unmatched":
                raise AssertionError("unknown product match_key must remain visible and unmatched")

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
            if len(detail.get("product_lines", [])) != 3 or len(detail.get("extra_lines", [])) != 1:
                raise AssertionError("detail must split product and extra lines")
            if detail["product_lines"][0].get("internal_name") != "Clear iPhone 14 Pro":
                raise AssertionError("created shipment must persist nomenclature auto-match")

            detail_status, loaded_detail = _get_json(f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}")
            if detail_status != 200 or loaded_detail.get("shipment_id") != shipment_id:
                raise AssertionError("detail route must return persisted card payload")

            edited = json.loads(json.dumps(loaded_detail, ensure_ascii=False))
            edited["lines"][0]["internal_sku"] = "SKU-MANUAL"
            edited["lines"][0]["internal_nm_id"] = 123456
            edited["lines"][0]["internal_name"] = "Manual SKU"
            edited["lines"][0]["match_status"] = "matched"
            edited["lines"][0]["manual_override"] = True
            edited["lines"][0]["amount"] = 12
            edited["metadata"]["declared_invoice_total"] = 35
            patch_status, patched = _patch_json(
                f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}",
                {"shipment_date": "2026-05-15", "payload": edited},
            )
            if patch_status != 200 or patched.get("shipment_date") != "2026-05-15":
                raise AssertionError(f"patch route must update shipment date, got {patch_status} {patched}")
            if patched.get("match_status") != "manual_override" or patched.get("summary", {}).get("product_amount_total") != 30.0:
                raise AssertionError("patch route must mark manual_override and recalculate totals server-side")

            second_nom_status, second_nom_payload = _post_json(
                f"{base_url}{DEFAULT_NOMENCLATURE_PATH}",
                {
                    "is_active": True,
                    "our_sku": "SKU-AS-14PM",
                    "nm_id": 210184534,
                    "nomenclature_name": "Anti-Spy iPhone 14 Pro Max",
                    "product_type": "anti_spy",
                    "match_key": "anti_spy|iphone_14_pro_max",
                    "comment": "rematch smoke",
                },
            )
            if second_nom_status != 200 or second_nom_payload.get("item", {}).get("nm_id") != 210184534:
                raise AssertionError("second nomenclature item must save for rematch")
            rematch_status, rematched = _post_json(
                f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}/rematch",
                {"overwrite_manual": False},
            )
            if rematch_status != 200:
                raise AssertionError(f"rematch route must return updated detail, got {rematch_status} {rematched}")
            rematched_products = rematched.get("product_lines", [])
            if rematched_products[0].get("internal_sku") != "SKU-MANUAL":
                raise AssertionError("rematch must not overwrite manual_override rows by default")
            if rematched_products[2].get("internal_nm_id") != 210184534:
                raise AssertionError("rematch must fill previously unmatched rows from nomenclature")

            registry_status, registry_payload = _get_json(f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}")
            if registry_status != 200 or len(registry_payload.get("shipments", [])) != 1:
                raise AssertionError("list route must expose saved shipment")
            invoice_path = registry_payload["shipments"][0].get("invoice_download_path")
            invoice_status, invoice_bytes, invoice_headers = _get_bytes(f"{base_url}{invoice_path}")
            if invoice_status != 200 or hashlib.sha256(invoice_bytes).hexdigest() != workbook_sha256:
                raise AssertionError("invoice download must preserve original XLSX bytes")
            if "attachment" not in str(invoice_headers.get("Content-Disposition", "")):
                raise AssertionError("invoice download must be an attachment")
            delete_status, delete_payload = _delete_json(f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/{shipment_id}")
            if delete_status != 200 or delete_payload.get("deleted") is not True:
                raise AssertionError(f"delete route must remove shipment, got {delete_status} {delete_payload}")
            after_delete_status, after_delete_payload = _get_json(f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}")
            if after_delete_status != 200 or after_delete_payload.get("shipments") != []:
                raise AssertionError("deleted supplier order must disappear from registry")
            deleted_invoice_status, _, _ = _get_bytes(f"{base_url}{invoice_path}")
            if deleted_invoice_status != 404:
                raise AssertionError("deleted supplier invoice must not remain downloadable")
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
    sheet.append(["Invoice Total:", 33])
    sheet.append(["NO.", "NAME & SPECIFICATION", "MODELS", "QTY", "U.PRICE", "AMOUNT", "COMMENT"])
    sheet.append([1, "高清膜 smk", "iPhone 14 Pro", 10, 1, 10, ""])
    sheet.append([2, "防窥膜 (Anti-Spy)", "iPhone 17e / 16e /14 / 13 / 13Pro", 4, 2, 8, ""])
    sheet.append([3, "防窥膜 (Anti-Spy)", "iPhone 14 Pro Max", 5, 2, 10, ""])
    sheet.append([4, "OPP bag packets", "", 100, 0.05, 5, "OPP packets"])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _post_multipart(
    url: str,
    workbook_bytes: bytes,
    *,
    filename: str,
    content_type: str = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
) -> tuple[int, dict[str, object]]:
    boundary = "----wbcore-supplier" + uuid4().hex
    body = b"".join(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"),
            f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
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


def _delete_json(url: str) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(url, headers={"Accept": "application/json"}, method="DELETE")
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
