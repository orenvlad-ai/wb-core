"""Smoke-check Fulfillment services XLSX/PDF/upload overlay contour."""

from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from urllib import error as urllib_error, request as urllib_request

from openpyxl import Workbook, load_workbook
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_FULFILLMENT_SERVICES_TEMPLATE_PATH,
    DEFAULT_FULFILLMENT_SERVICES_UPLOADS_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_UPLOAD_PATH,
    DEFAULT_WB_SUPPLIES_PATH,
    build_registry_upload_http_server,
)
from packages.application.fulfillment_services import TEMPLATE_HEADERS, FulfillmentServicesBlock  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


NOW = "2026-07-06T08:00:00Z"


def main() -> None:
    with TemporaryDirectory(prefix="fulfillment-services-app-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        _seed_wb_supplies(runtime)
        block = FulfillmentServicesBlock(runtime=runtime, timestamp_factory=lambda: NOW)

        template_bytes, template_filename, template_content_type = block.build_template()
        if template_filename != "sheet-vitrina-v1-fulfillment-services-template.xlsx":
            raise AssertionError(f"unexpected template filename: {template_filename}")
        if "spreadsheetml" not in template_content_type:
            raise AssertionError(f"unexpected template content type: {template_content_type}")
        template_headers = _read_headers(template_bytes)
        if template_headers != TEMPLATE_HEADERS:
            raise AssertionError(f"template headers must match PNG-derived form, got {template_headers}")

        ok_payload = block.upload_xlsx(_build_workbook([_valid_row("1001"), _valid_row("1002")]), uploaded_filename="ok.xlsx")
        ok_upload = ok_payload["upload"]
        if (
            ok_payload.get("validation_status") != "ok"
            or ok_upload.get("rows_total") != 2
            or ok_upload.get("rows_matched") != 2
            or ok_upload.get("amount_without_vat_total") != 3000.0
            or ok_upload.get("vat_total") != 150.0
            or ok_upload.get("amount_with_vat_total") != 3150.0
            or not ok_upload.get("payment_validation_id")
            or not ok_payload.get("pdf_available")
        ):
            raise AssertionError(f"valid upload must pass and generate PDF, got {ok_payload}")
        pdf_bytes, _, _ = block.download_pdf(ok_upload["upload_id"])
        pdf_text = _pdf_text(pdf_bytes)
        for expected in (
            "Виза на оплату Fulfillment-услуг",
            ok_upload["upload_id"],
            ok_upload["payment_validation_id"],
            ok_upload["short_file_hash"],
            "К оплате = Итого + НДС 5%",
            "1001",
            "1002",
        ):
            if expected not in pdf_text:
                raise AssertionError(f"PDF must contain {expected!r}, text={pdf_text!r}")

        unmatched = block.upload_xlsx(_build_workbook([_valid_row("9999")]), uploaded_filename="unmatched.xlsx")
        if unmatched.get("validation_status") != "failed" or unmatched["upload"].get("pdf_available"):
            raise AssertionError(f"unmatched upload must fail without PDF, got {unmatched}")
        if "does not match cached WB supply" not in json.dumps(unmatched.get("row_errors"), ensure_ascii=False):
            raise AssertionError(f"unmatched row error missing, got {unmatched}")

        duplicate = block.upload_xlsx(_build_workbook([_valid_row("1001"), _valid_row("1001")]), uploaded_filename="duplicate.xlsx")
        if duplicate.get("validation_status") != "failed" or duplicate["upload"].get("pdf_available"):
            raise AssertionError(f"duplicate upload must fail without PDF, got {duplicate}")
        if "Duplicate" not in json.dumps(duplicate.get("row_errors"), ensure_ascii=False):
            raise AssertionError(f"duplicate row error missing, got {duplicate}")

    with TemporaryDirectory(prefix="fulfillment-services-http-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        _seed_wb_supplies(runtime)
        port = _reserve_free_port()
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: NOW,
        )
        cfg = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=port,
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path="/v1/sheet-vitrina-v1/refresh",
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime_dir,
        )
        server = build_registry_upload_http_server(cfg, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{cfg.port}"
            template_status, template_body, template_headers = _get_bytes(f"{base_url}{DEFAULT_FULFILLMENT_SERVICES_TEMPLATE_PATH}")
            if template_status != 200 or "spreadsheetml" not in template_headers.get("Content-Type", ""):
                raise AssertionError(f"template route must return XLSX, got {template_status} {template_headers}")
            load_workbook(BytesIO(template_body), data_only=True)

            upload_status, upload_payload = _post_multipart(
                f"{base_url}{DEFAULT_FULFILLMENT_SERVICES_UPLOADS_PATH}",
                _build_workbook([_valid_row("1001"), _valid_row("1002")]),
                filename="http-ok.xlsx",
            )
            if upload_status != 200 or upload_payload.get("validation_status") != "ok":
                raise AssertionError(f"upload route must accept valid XLSX, got {upload_status} {upload_payload}")
            upload_id = upload_payload["upload"]["upload_id"]

            list_status, list_payload = _get_json(f"{base_url}{DEFAULT_FULFILLMENT_SERVICES_UPLOADS_PATH}")
            if list_status != 200 or not list_payload.get("uploads"):
                raise AssertionError(f"list route must return uploads, got {list_status} {list_payload}")
            detail_status, detail_payload = _get_json(f"{base_url}{DEFAULT_FULFILLMENT_SERVICES_UPLOADS_PATH}/{upload_id}")
            if detail_status != 200 or len(detail_payload.get("lines") or []) != 2:
                raise AssertionError(f"detail route must return parsed lines, got {detail_status} {detail_payload}")
            pdf_status, pdf_body, pdf_headers = _get_bytes(
                f"{base_url}{DEFAULT_FULFILLMENT_SERVICES_UPLOADS_PATH}/{upload_id}/payment-validation.pdf"
            )
            if pdf_status != 200 or "application/pdf" not in pdf_headers.get("Content-Type", ""):
                raise AssertionError(f"PDF route must return protected PDF, got {pdf_status} {pdf_headers}")
            if upload_id not in _pdf_text(pdf_body):
                raise AssertionError("PDF route must return the generated payment-validation PDF")

            wb_status, wb_payload = _get_json(f"{base_url}{DEFAULT_WB_SUPPLIES_PATH}?size_filter=all&limit=20")
            if wb_status != 200:
                raise AssertionError(f"WB supplies route failed: {wb_status} {wb_payload}")
            labels = [item.get("label") for item in wb_payload.get("schema", {}).get("columns", [])]
            if "Транзит" not in labels or "Услуги fulfillment" not in labels or "Стоимость" in labels:
                raise AssertionError(f"WB supplies schema must expose transit/fulfillment labels, got {labels}")
            by_number = {str(row.get("visible_number") or row.get("wb_supply_id")): row for row in wb_payload.get("rows") or []}
            row1001 = by_number.get("1001") or {}
            if (
                row1001.get("fulfillment_amount_with_vat_total") != 1575.0
                or "₽/шт" not in str(row1001.get("fulfillment_per_unit_display") or "")
                or "₽/шт" not in str(row1001.get("transit_per_unit_display") or "")
            ):
                raise AssertionError(f"WB supplies overlay must include fulfillment/transit per-unit, got {row1001}")

            operator_status, operator_html = _get_text(f"{base_url}{DEFAULT_SHEET_OPERATOR_UI_PATH}?embedded_tab=factory-order")
            for expected in (
                "Fulfillment",
                "Скачать шаблон",
                "Загрузить заполненный файл",
                "Услуги fulfillment",
                "Транзит",
                "fulfillment_services_template_path",
                "fulfillment_services_uploads_path",
            ):
                if operator_status != 200 or expected not in operator_html:
                    raise AssertionError(f"operator HTML must expose Fulfillment UI token {expected!r}")
            if "<th>Стоимость</th>" in operator_html:
                raise AssertionError("operator WB supplies table must no longer expose Стоимость header")
        finally:
            server.shutdown()
            thread.join(timeout=5)

    print("sheet_vitrina_v1_fulfillment_services_smoke: OK")


def _seed_wb_supplies(runtime: RegistryUploadDbBackedRuntime) -> None:
    runtime.save_wb_supply_rows(
        rows=[
            _wb_supply_row("1001", accepted_quantity=10, quantity_added=10, cost_total=200),
            _wb_supply_row("1002", accepted_quantity=20, quantity_added=20, cost_total=400),
        ],
        warehouses=[{"ID": 777, "name": "Электросталь"}],
        synced_at=NOW,
    )


def _wb_supply_row(supply_id: str, *, accepted_quantity: int, quantity_added: int, cost_total: float) -> dict:
    return {
        "supply_id": supply_id,
        "cache_key": f"supply:{supply_id}",
        "wb_supply_id": supply_id,
        "preorder_id": "",
        "visible_number": supply_id,
        "number_label": supply_id,
        "status_id": 5,
        "status_label": "Принято",
        "status_visual_tone": "success",
        "status_class": "success",
        "type_label": "Короб",
        "warehouse_id": "777",
        "warehouse_display": "Электросталь",
        "warehouse_fact_line": "",
        "quantity_added": quantity_added,
        "packed_quantity": quantity_added,
        "accepted_quantity": accepted_quantity,
        "quantity_for_size_filter": accepted_quantity,
        "acceptance_coefficient": 0,
        "cost_total": cost_total,
        "has_transit_cost_marker": True,
        "supply_date": "2026-07-06",
        "fact_date": "2026-07-06",
        "updated_date": "2026-07-06T08:00:00Z",
        "source_created_at": "2026-07-06T07:00:00Z",
        "raw_list_hash": supply_id,
        "raw_list": {"supplyID": supply_id},
        "raw_detail": {"quantity": quantity_added},
        "raw_goods": [{"nmID": 1, "quantity": quantity_added, "acceptedQuantity": accepted_quantity}],
        "raw_package": [],
    }


def _valid_row(supply_id: str) -> list[object]:
    if supply_id == "1002":
        return [supply_id, "Обухово/Краснодар", 3, 450, 0, 850, 1350, 150, 1500, 75]
    return [supply_id, "Сталь", 2, 450, 1, 500, 1400, 100, 1500, 75]


def _build_workbook(rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Fulfillment"
    sheet.append(TEMPLATE_HEADERS)
    for row in rows:
        sheet.append(row)
    if rows:
        total = sum(float(row[8]) for row in rows)
        vat = sum(float(row[9]) for row in rows)
        sheet.append(["", "", "", "", "", "", "", "", total, vat])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _read_headers(workbook_bytes: bytes) -> list[str]:
    workbook = load_workbook(BytesIO(workbook_bytes), read_only=True, data_only=True)
    sheet = workbook.worksheets[0]
    return ["" if value is None else str(value) for value in next(sheet.iter_rows(min_row=1, max_row=1, values_only=True))]


def _pdf_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get_json(url: str) -> tuple[int, dict]:
    req = urllib_request.Request(url, method="GET", headers={"Accept": "application/json"})
    return _open_json(req)


def _get_text(url: str) -> tuple[int, str]:
    req = urllib_request.Request(url, method="GET")
    try:
        with urllib_request.urlopen(req, timeout=20) as response:
            return response.status, response.read().decode("utf-8")
    except urllib_error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _get_bytes(url: str) -> tuple[int, bytes, dict[str, str]]:
    req = urllib_request.Request(url, method="GET")
    try:
        with urllib_request.urlopen(req, timeout=20) as response:
            return response.status, response.read(), dict(response.headers.items())
    except urllib_error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers.items())


def _post_multipart(url: str, file_bytes: bytes, *, filename: str) -> tuple[int, dict]:
    boundary = "----wbcorefulfillmentboundary"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"),
            b"Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n",
            file_bytes,
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    req = urllib_request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
    )
    return _open_json(req)


def _open_json(req: urllib_request.Request) -> tuple[int, dict]:
    try:
        with urllib_request.urlopen(req, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


if __name__ == "__main__":
    main()
