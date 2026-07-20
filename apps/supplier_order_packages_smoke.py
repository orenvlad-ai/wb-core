"""Smoke-check deterministic supplier package assembly and DT XLSX mapping."""

from __future__ import annotations

import hashlib
from io import BytesIO
import json
from pathlib import Path
import sys
import zipfile

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_http_entrypoint import (  # noqa: E402
    _build_supplier_order_documents_archive,
)
from packages.application.supplier_customs_breakdown import (  # noqa: E402
    build_customs_breakdown_xlsx,
    match_customs_goods_items,
)


SHIPMENT_LINES = [
    {
        "line_id": "line-101",
        "line_type": "product",
        "sort_order": 1,
        "internal_nm_id": 101,
        "internal_name": "Our Alpha",
        "model_raw": "Exact Model Alpha",
        "barcode": "4600000000001",
        "qty": 10,
        "match_status": "matched_by_barcode",
        "raw": {},
    },
    {
        "line_id": "line-102",
        "line_type": "product",
        "sort_order": 2,
        "internal_nm_id": 102,
        "internal_name": "Our Group One",
        "model_raw": "Exact Model Group",
        "barcode": "4600000000002",
        "qty": 2,
        "match_status": "matched",
        "raw": {},
    },
    {
        "line_id": "line-103",
        "line_type": "product",
        "sort_order": 3,
        "internal_nm_id": 103,
        "internal_name": "Our Group Two",
        "model_raw": "Exact Model Group",
        "barcode": "4600000000003",
        "qty": 3,
        "match_status": "matched",
        "raw": {},
    },
]

NOMENCLATURE = [
    {
        "is_active": True,
        "nm_id": item["internal_nm_id"],
        "barcode": item["barcode"],
        "barcodes": [item["barcode"]],
        "nomenclature_name": item["internal_name"],
    }
    for item in SHIPMENT_LINES
]


def main() -> None:
    _assert_matching_and_workbook()
    _assert_logistics_assemblies()
    _assert_accounting_multiple_customs()
    print("supplier_order_packages_smoke: OK")


def _assert_matching_and_workbook() -> None:
    goods_items = [
        _goods("1", "Exact Model Alpha", 10, barcode="4600000000001"),
        _goods("2", "Exact Model Group", 5),
        _goods("3", "Exact Model Group", 4),
        _goods("4", "Unknown exact source", 7),
    ]
    matching = match_customs_goods_items(
        goods_items=goods_items,
        shipment_lines=SHIPMENT_LINES,
        nomenclature_items=NOMENCLATURE,
    )
    if (
        matching.get("position_count") != 4
        or matching.get("output_row_count") != 5
        or matching.get("status_counts") != {
            "ambiguous": 1,
            "matched": 1,
            "reconciled_group": 1,
            "unmatched": 1,
        }
        or not matching.get("quantity_conserved")
        or not matching.get("requires_review")
    ):
        raise AssertionError(f"DT deterministic matching contract changed: {matching}")
    matched = matching["rows"][0]
    ambiguous = next(item for item in matching["rows"] if item.get("status") == "ambiguous")
    unmatched = next(item for item in matching["rows"] if item.get("status") == "unmatched")
    if matched.get("nm_id") != 101 or ambiguous.get("nm_id") is not None or unmatched.get("nm_id") is not None:
        raise AssertionError(f"DT matcher guessed an nmID or lost exact barcode: {matching}")

    body, filename, receipt = build_customs_breakdown_xlsx(
        customs_document={
            "document_id": "dt-matching",
            "document_number": "10131010/100626/5187132",
            "file_original_name": "sanitized-customs.pdf",
            "normalized_parse": {
                "declaration_number": "10131010/100626/5187132",
                "declaration_date": "2026-06-10",
                "goods_items": goods_items,
            },
        },
        shipment={"header": {"shipment_id": "order-safe", "invoice_no": "SAFE-1"}},
        shipment_lines=SHIPMENT_LINES,
        nomenclature_items=NOMENCLATURE,
    )
    if filename != "DT_10131010_100626_5187132_rasshifrovka.xlsx" or not receipt.get("workbook_valid"):
        raise AssertionError(f"DT workbook filename/validation mismatch: {filename} {receipt}")
    if "rows" in receipt:
        raise AssertionError("bounded DT generation receipt must not expose row-level product evidence")
    workbook = load_workbook(BytesIO(body), read_only=True, data_only=True)
    sheet = workbook["Расшифровка ДТ"]
    header_row = next(
        row_index
        for row_index in range(1, sheet.max_row + 1)
        if sheet.cell(row=row_index, column=1).value == "№ позиции ДТ"
    )
    quantities = [sheet.cell(row=row_index, column=3).value for row_index in range(header_row + 1, sheet.max_row + 1)]
    if not quantities or not all(isinstance(value, (int, float)) for value in quantities):
        raise AssertionError(f"DT workbook quantities must be numeric Excel cells: {quantities}")
    barcode = sheet.cell(row=header_row + 1, column=10)
    if barcode.value != "4600000000001" or barcode.data_type != "s":
        raise AssertionError(f"DT workbook barcode must be lossless text: {barcode.value} {barcode.data_type}")


def _assert_logistics_assemblies() -> None:
    rows = [
        _document("contract-1", "contract", "contract.pdf"),
        _document("payment-1", "bank_transfer_application", "payment.pdf"),
        _document("payment-2", "bank_transfer_application", "payment.pdf"),
        _document("control-1", "bank_control_statement", "control.pdf"),
    ]
    payload = {"supplier_order_id": "order-safe", "required_documents": rows, "shipment": {}}

    archive_bytes, receipt = _build_supplier_order_documents_archive(
        payload,
        package_type="logistics",
        file_loader=_fixture_loader,
    )
    manifest, names = _manifest_and_names(archive_bytes)
    if (
        receipt.get("status") != "complete"
        or receipt.get("counts", {}).get("included") != 4
        or len(names) != len(set(names))
        or manifest.get("included") != receipt.get("included")
        or sorted(names) != sorted(item["archive_name"] for item in manifest["included"])
    ):
        raise AssertionError(f"complete logistics package mismatch: {receipt} {names}")
    reversed_bytes, _ = _build_supplier_order_documents_archive(
        {**payload, "required_documents": list(reversed(rows))},
        package_type="logistics",
        file_loader=_fixture_loader,
    )
    reversed_manifest, _ = _manifest_and_names(reversed_bytes)
    names_by_document = {
        str(item.get("document_id") or ""): str(item.get("archive_name") or "")
        for item in manifest.get("included") or []
    }
    reversed_names_by_document = {
        str(item.get("document_id") or ""): str(item.get("archive_name") or "")
        for item in reversed_manifest.get("included") or []
    }
    if reversed_names_by_document != names_by_document:
        raise AssertionError("archive names must remain deterministic when source row order changes")

    failed_bytes, failed_receipt = _build_supplier_order_documents_archive(
        payload,
        package_type="logistics",
        file_loader=lambda row: (_fixture_loader(row) if row.get("document_id") != "payment-2" else _raise_unreadable()),
    )
    failed_manifest, failed_names = _manifest_and_names(failed_bytes)
    if (
        failed_receipt.get("status") != "error"
        or failed_receipt.get("counts", {}).get("expected") != 4
        or failed_receipt.get("counts", {}).get("included") != 3
        or failed_receipt.get("counts", {}).get("failed") != 1
        or failed_manifest.get("included") != failed_receipt.get("included")
        or len(failed_names) != 3
    ):
        raise AssertionError(f"unreadable package member must be a red exact-assembly receipt: {failed_receipt}")

    damaged_rows = [dict(row) for row in rows]
    damaged_rows[1]["file_sha256"] = hashlib.sha256(b"different stored upload bytes").hexdigest()
    _, damaged_receipt = _build_supplier_order_documents_archive(
        {**payload, "required_documents": damaged_rows},
        package_type="logistics",
        file_loader=_fixture_loader,
    )
    if (
        damaged_receipt.get("status") != "error"
        or damaged_receipt.get("counts", {}).get("failed") != 1
        or "checksum" not in str(damaged_receipt.get("failed", [{}])[0].get("reason") or "")
    ):
        raise AssertionError(f"damaged package member must fail stored-upload integrity: {damaged_receipt}")

    partial_payload = {
        **payload,
        "required_documents": [row for row in rows if row["document_type"] != "bank_control_statement"],
    }
    _, partial_receipt = _build_supplier_order_documents_archive(
        partial_payload,
        package_type="logistics",
        file_loader=_fixture_loader,
    )
    if partial_receipt.get("status") != "partial" or partial_receipt.get("counts", {}).get("missing") != 1:
        raise AssertionError(f"missing logistics type must remain distinct from read failure: {partial_receipt}")


def _assert_accounting_multiple_customs() -> None:
    first_dt = _document("dt-1", "customs_declaration", "dt.pdf")
    first_dt["document_number"] = "10131010/100626/5187132"
    first_dt["normalized_parse"] = {
        "declaration_number": first_dt["document_number"],
        "declaration_date": "2026-06-10",
        "goods_items": [_goods("1", "Exact Model Alpha", 10, barcode="4600000000001")],
    }
    second_dt = _document("dt-2", "customs_declaration", "dt.pdf")
    second_dt["document_number"] = "10228010/030726/5211187"
    second_dt["normalized_parse"] = {
        "declaration_number": second_dt["document_number"],
        "declaration_date": "2026-07-03",
        "goods_items": [_goods("1", "Unknown exact source", 7)],
    }
    payload = {
        "supplier_order_id": "order-safe",
        "shipment": {
            "header": {"shipment_id": "order-safe", "invoice_no": "SAFE-1"},
            "lines": SHIPMENT_LINES,
        },
        "required_documents": [
            _document("invoice-1", "invoice", "invoice.xlsx"),
            _document("contract-1", "contract", "contract.pdf"),
            first_dt,
            second_dt,
        ],
    }
    archive_bytes, receipt = _build_supplier_order_documents_archive(
        payload,
        package_type="accounting",
        file_loader=_fixture_loader,
        nomenclature_items=NOMENCLATURE,
    )
    manifest, names = _manifest_and_names(archive_bytes)
    generated = manifest.get("generated_files") or []
    if (
        receipt.get("status") != "complete"
        or receipt.get("counts", {}).get("expected") != 6
        or receipt.get("counts", {}).get("included") != 6
        or len(generated) != 2
        or len({item.get("archive_name") for item in generated}) != 2
        or not receipt.get("requires_review")
        or receipt.get("review_message") != "Расшифровка ДТ требует проверки"
    ):
        raise AssertionError(f"multi-DT accounting package mismatch: {receipt} {names}")
    with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
        for item in generated:
            workbook = load_workbook(BytesIO(archive.read(item["archive_name"])), read_only=True, data_only=True)
            if "Контроль" not in workbook.sheetnames:
                raise AssertionError(f"generated DT workbook is unreadable: {item}")


def _document(document_id: str, document_type: str, filename: str) -> dict[str, object]:
    return {
        "document_id": document_id,
        "document_type": document_type,
        "document_name": document_type,
        "document_number": document_id,
        "file_original_name": filename,
        "is_uploaded": True,
        "status": "uploaded",
        "warnings": [],
    }


def _goods(position: str, name: str, quantity: int, *, barcode: str = "") -> dict[str, object]:
    return {
        "position_number": position,
        "source_name": name,
        "quantity": quantity,
        "unit": "ШТ",
        "barcode": barcode,
        "identifiers": {"barcode": barcode} if barcode else {},
    }


def _fixture_loader(row: dict[str, object]) -> tuple[bytes, str, str]:
    filename = str(row.get("file_original_name") or "document.bin")
    return f"sanitized:{row.get('document_id')}".encode(), filename, "application/octet-stream"


def _raise_unreadable() -> tuple[bytes, str, str]:
    raise OSError("sanitized source file is unreadable")


def _manifest_and_names(archive_bytes: bytes) -> tuple[dict[str, object], list[str]]:
    with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        return manifest, [name for name in archive.namelist() if name != "manifest.json"]


if __name__ == "__main__":
    main()
