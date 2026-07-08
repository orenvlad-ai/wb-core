"""HTTP smoke-check for the server-owned Остатки ФФ ledger routes."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from urllib import error, request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_FF_STOCKS_CONFIRM_PATH,
    DEFAULT_FF_STOCKS_EXPORT_PATH,
    DEFAULT_FF_STOCKS_PATH,
    DEFAULT_FF_STOCKS_PREVIEW_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_REFRESH_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.simple_xlsx import build_single_sheet_workbook_bytes, read_first_sheet_rows
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig


INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
ACTIVATED_AT = "2026-04-18T09:00:00Z"
NOW = datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc)
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def main() -> None:
    bundle = json.loads(INPUT_BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="ff-stock-ledger-http-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)
        active_nm_ids = [int(item.nm_id) for item in runtime.load_current_state().config_v2 if item.enabled]
        probe_nm_id = active_nm_ids[0]
        _seed_nomenclature(runtime, active_nm_ids)

        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: ACTIVATED_AT,
            now_factory=lambda: NOW,
        )
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=_reserve_free_port(),
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path=DEFAULT_SHEET_REFRESH_PATH,
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime_dir,
        )
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{config.port}"

            status_code, status_payload = _get_json(f"{base_url}{DEFAULT_FF_STOCKS_PATH}")
            _assert(status_code == 200, f"status route must return 200, got {status_code}")
            _assert(status_payload["contract_name"] == "sheet_vitrina_v1_ff_stock_ledger", "status contract changed")
            _assert(status_payload["registry"]["summary"]["sku_count"] == len(active_nm_ids), "status must include active SKU registry")

            export_code, export_bytes, export_headers = _get_bytes(f"{base_url}{DEFAULT_FF_STOCKS_EXPORT_PATH}")
            _assert(export_code == 200, f"export route must return 200, got {export_code}")
            _assert(export_headers.get("Content-Type", "").startswith(XLSX_TYPE), "export must be XLSX")
            export_rows = read_first_sheet_rows(export_bytes)
            _assert(export_rows[0] == ["barcode", "nmId", "SKU/название/комментарий", "группа", "количество"], "export headers changed")

            upload_bytes = _operation_xlsx([[f"460{probe_nm_id}", probe_nm_id, "Probe", "Clear", 12]])
            preview_code, preview_payload = _post_multipart(
                f"{base_url}{DEFAULT_FF_STOCKS_PREVIEW_PATH}",
                upload_bytes,
                filename="receipt.xlsx",
                fields={"operation_type": "manual_receipt"},
            )
            _assert(preview_code == 200, f"preview route must return 200, got {preview_code} {preview_payload}")
            _assert(preview_payload["apply_allowed"] is True, "preview must be applicable")
            _assert(preview_payload["preview"]["summary"]["sku_count"] == 1, "preview SKU count changed")

            confirm_code, confirm_payload = _post_json(
                f"{base_url}{DEFAULT_FF_STOCKS_CONFIRM_PATH}",
                {"preview_id": preview_payload["preview"]["preview_id"]},
            )
            _assert(confirm_code == 200, f"confirm route must return 200, got {confirm_code} {confirm_payload}")
            operation = confirm_payload["operation"]
            _assert(operation["operation_type"] == "manual_receipt", "confirm must create manual receipt")
            _assert(operation["file_available"] is True, "manual operation must expose source file")

            status_after_code, status_after_payload = _get_json(f"{base_url}{DEFAULT_FF_STOCKS_PATH}")
            _assert(status_after_code == 200, "status after confirm must return 200")
            probe_row = next(item for item in status_after_payload["registry"]["rows"] if int(item["nm_id"]) == probe_nm_id)
            _assert(probe_row["current_stock_ff"] == 12.0, "confirmed receipt must affect computed balance")

            source_path = f"{DEFAULT_FF_STOCKS_PATH}/operations/{operation['operation_id']}/file"
            file_code, file_bytes, file_headers = _get_bytes(f"{base_url}{source_path}")
            _assert(file_code == 200, f"source-file route must return 200, got {file_code}")
            _assert(file_bytes == upload_bytes, "source-file download must preserve original XLSX bytes")
            _assert("receipt.xlsx" in file_headers.get("Content-Disposition", ""), "source-file filename missing")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    print("ff_stock_ledger_http_smoke: ok")


def _operation_xlsx(rows: list[list[object]]) -> bytes:
    return build_single_sheet_workbook_bytes(
        "Остатки ФФ",
        [["barcode", "nmId", "SKU/название/комментарий", "группа", "количество"], *rows],
    )


def _seed_nomenclature(runtime: RegistryUploadDbBackedRuntime, active_nm_ids: list[int]) -> None:
    runtime.save_sku_group(
        {
            "group_key": "clear",
            "label": "Clear",
            "is_active": True,
            "is_system": False,
            "created_at": ACTIVATED_AT,
            "updated_at": ACTIVATED_AT,
        }
    )
    runtime.save_nomenclature_items_atomic(
        [
            {
                "item_id": f"nom_{nm_id}",
                "is_active": True,
                "is_hidden": False,
                "our_sku": f"SKU-{index}",
                "nm_id": nm_id,
                "barcode": f"460{nm_id}",
                "barcodes": [f"460{nm_id}"],
                "nomenclature_name": f"SKU name {index}",
                "product_type": "clear",
                "match_key": f"sku-{index}",
                "comment": f"comment {index}",
                "created_at": ACTIVATED_AT,
                "updated_at": ACTIVATED_AT,
            }
            for index, nm_id in enumerate(active_nm_ids, start=1)
        ]
    )


def _post_json(url: str, payload: object) -> tuple[int, object]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib_request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _post_multipart(
    url: str,
    file_bytes: bytes,
    *,
    filename: str,
    fields: dict[str, str] | None = None,
) -> tuple[int, object]:
    boundary = "----ffStockLedgerSmokeBoundary"
    parts: list[bytes] = []
    for key, value in (fields or {}).items():
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {XLSX_TYPE}\r\n\r\n"
        ).encode("utf-8")
        + file_bytes
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    req = urllib_request.Request(
        url,
        data=b"".join(parts),
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "application/json"},
    )
    try:
        with urllib_request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_json(url: str) -> tuple[int, object]:
    req = urllib_request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urllib_request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_bytes(url: str) -> tuple[int, bytes, dict[str, str]]:
    req = urllib_request.Request(url, method="GET")
    try:
        with urllib_request.urlopen(req, timeout=10) as response:
            return response.status, response.read(), dict(response.headers.items())
    except error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers.items())


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


if __name__ == "__main__":
    main()
