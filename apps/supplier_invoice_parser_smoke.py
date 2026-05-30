"""Smoke-check supplier invoice XLSX parser and deterministic matching."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import sys
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.supplier_invoice_parser import extract_iphone_model_keys, parse_supplier_invoice_xlsx  # noqa: E402
from packages.application.supplier_shipments import _apply_nomenclature_matches  # noqa: E402


def main() -> None:
    expected_keys = {
        "iPhone 17e / 16e /14 / 13 / 13Pro": [
            "iphone_17e",
            "iphone_16e",
            "iphone_14",
            "iphone_13",
            "iphone_13_pro",
        ],
        "iPhone 15 / 16": ["iphone_15", "iphone_16"],
        "iPhone 16 Pro/17": ["iphone_16_pro", "iphone_17"],
    }
    for raw_model, expected in expected_keys.items():
        actual = extract_iphone_model_keys(raw_model)
        if actual != expected:
            raise AssertionError(f"compatible model normalizer mismatch for {raw_model!r}: {actual}")
    workbook_bytes = _build_invoice_fixture()
    payload = parse_supplier_invoice_xlsx(
        workbook_bytes,
        filename="PI-test 26GN390 (14.5.2026).xlsx",
        aliases=[
            {
                "factory_type": "clear",
                "normalized_model": "iphone_14_pro",
                "match_key": "clear|iphone_14_pro",
                "our_sku": "SKU-CLEAR-14PRO",
                "nm_id": 210183919,
                "nomenclature_name": "Clear iPhone 14 Pro",
                "group": "clear",
                "active": True,
            }
        ],
    )
    lines = payload["lines"]
    metadata = payload["metadata"]
    if metadata.get("contract_no") != "CNT-2026-0513" or metadata.get("contract_date") != "2026-05-13":
        raise AssertionError(f"parser must extract contract no/date from cells, got {metadata}")
    product_lines = [line for line in lines if line["line_type"] == "product"]
    extra_lines = [line for line in lines if line["line_type"] == "extra"]
    if len(product_lines) != 5 or len(extra_lines) != 2:
        raise AssertionError(f"parser must keep product and extra rows separately, got {len(product_lines)} / {len(extra_lines)}")
    if [line["product_type"] for line in product_lines] != ["clear", "clear", "anti_spy", "anti_spy", "matte"]:
        raise AssertionError("parser must fill down clear/anti_spy/matte markers from Chinese/comment blocks")
    if (
        product_lines[0]["match_status"] != "matched"
        or product_lines[0]["internal_nm_id"] != 210183919
        or product_lines[0]["internal_sku"] != "SKU-CLEAR-14PRO"
        or product_lines[0]["internal_name"] != "Clear iPhone 14 Pro"
    ):
        raise AssertionError("active deterministic nomenclature alias must fill SKU/nmId/name by exact type+model key")
    if product_lines[1]["match_key"] != "clear|iphone_15_16" or product_lines[1]["match_status"] != "unmatched":
        raise AssertionError("compatible model aliases like iPhone 15 / 16 must stay one invoice line")
    if product_lines[3]["match_key"] != "anti_spy|iphone_16_pro_17":
        raise AssertionError("iPhone 16 Pro/17 must stay one anti_spy invoice line")
    if product_lines[4]["match_key"] != "matte|iphone_17e_16e_14_13_13pro":
        raise AssertionError("multi-compatible matte alias must not be split into separate product rows")
    summary = payload["summary"]
    if summary["product_qty_total"] != 40.0 or summary["product_amount_total"] != 42.0:
        raise AssertionError(f"product totals mismatch: {summary}")
    if summary["extras_amount_total"] != 5.0 or summary["invoice_amount_total"] != 47.0:
        raise AssertionError(f"invoice totals mismatch: {summary}")
    if summary["checksum_error"]:
        raise AssertionError("declared invoice total must match parsed total in fixture")
    drawing_payload = parse_supplier_invoice_xlsx(
        _with_drawing_text_fixture(_build_invoice_fixture(contract_cells=False)),
        filename="PI-drawing 26GN391 (15.5.2026).xlsx",
    )
    drawing_metadata = drawing_payload["metadata"]
    if drawing_metadata.get("contract_no") != "26DRAW001" or drawing_metadata.get("contract_date") != "2026-05-13":
        raise AssertionError(f"parser must extract contract no/date from drawing XML text, got {drawing_metadata}")
    compatibility_lines = _apply_nomenclature_matches(
        [
            {
                "line_type": "product",
                "product_type": "anti_spy",
                "model_raw": "iPhone 17e / 16e /14 / 13 / 13Pro",
                "model_normalized": "iphone_17e_16e_14_13_13pro",
                "match_key": "anti_spy|iphone_17e_16e_14_13_13pro",
                "match_status": "unmatched",
            }
        ],
        [
            {
                "item_id": "nom_compat",
                "is_active": True,
                "our_sku": "SKU-AS-141313P",
                "nm_id": 391662410,
                "nomenclature_name": "anti-spy iPhone 14 / 13 / 13Pro",
                "product_type": "anti_spy",
                "match_key": "anti_spy|iphone_14_13_13pro",
                "aliases": [],
                "compatible_models_text": "iPhone 14, iPhone 13, iPhone 13 Pro",
                "compatible_model_keys": ["iphone_14", "iphone_13", "iphone_13_pro"],
            }
        ],
    )
    if (
        compatibility_lines[0].get("match_status") != "matched_by_compatibility"
        or compatibility_lines[0].get("internal_nm_id") != 391662410
        or compatibility_lines[0].get("internal_name") != "anti-spy iPhone 14 / 13 / 13Pro"
    ):
        raise AssertionError(f"compatibility matching must fill SKU/nmId/name, got {compatibility_lines[0]}")
    type_guard_lines = _apply_nomenclature_matches(
        [{**compatibility_lines[0], "internal_nm_id": None, "internal_name": "", "match_status": "unmatched"}],
        [
            {
                "item_id": "nom_wrong_type",
                "is_active": True,
                "our_sku": "SKU-CLEAR-141313P",
                "nm_id": 210183919,
                "nomenclature_name": "clean iPhone 14",
                "product_type": "clear",
                "match_key": "clear|iphone_14",
                "compatible_model_keys": ["iphone_14", "iphone_13", "iphone_13_pro"],
            }
        ],
    )
    if type_guard_lines[0].get("match_status") != "unmatched":
        raise AssertionError("compatibility matching must not cross product_type")
    ambiguous_lines = _apply_nomenclature_matches(
        [
            {
                "line_type": "product",
                "product_type": "matte",
                "model_raw": "iPhone 12",
                "model_normalized": "iphone_12",
                "match_key": "matte|iphone_12",
                "match_status": "unmatched",
            }
        ],
        [
            {
                "item_id": "nom_ambiguous_1",
                "is_active": True,
                "nomenclature_name": "matte ambiguous 1",
                "product_type": "matte",
                "match_key": "matte|legacy_a",
                "compatible_model_keys": ["iphone_12"],
            },
            {
                "item_id": "nom_ambiguous_2",
                "is_active": True,
                "nomenclature_name": "matte ambiguous 2",
                "product_type": "matte",
                "match_key": "matte|legacy_b",
                "compatible_model_keys": ["iphone_12"],
            },
        ],
    )
    if ambiguous_lines[0].get("match_status") != "ambiguous" or ambiguous_lines[0].get("internal_name"):
        raise AssertionError(f"equal compatibility candidates must stay ambiguous without SKU fill: {ambiguous_lines[0]}")
    print("supplier_invoice_parser_smoke: OK")


def _build_invoice_fixture(*, contract_cells: bool = True) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Invoice"
    sheet["A1"] = "Invoice No:"
    sheet["B1"] = "26GN390"
    sheet["A2"] = "Invoice Date:"
    sheet["B2"] = "14.5.2026"
    if contract_cells:
        sheet["A3"] = "Contract No."
        sheet["B3"] = "CNT-2026-0513"
        sheet["A4"] = "Date of Contract"
        sheet["B4"] = "2026.5.13"
        sheet["A5"] = "Supplier:"
        sheet["B5"] = "Zhejiang Supplier"
    else:
        sheet["A3"] = "Supplier:"
        sheet["B3"] = "Zhejiang Supplier"
    headers = [
        "NO.",
        "MODELS / （型号）",
        "NAME & SPECIFICATION / （品名规格）",
        "QTY (PCS) / （数量）",
        "U.PRICE / （单价） (RMB/PCS)",
        "AMOUNT / （总价） (RMB)",
        "备注",
    ]
    sheet.append(headers)
    rows = [
        [1, "iPhone 14 Pro", "高清膜 smk / 带包装", 10, 1, 10, "OPP袋子 + 标签 + 卡片 in packaging comment"],
        [2, "iPhone 15 / 16", None, 5, 2, 10, ""],
        [3, "iPhone 14 Pro Max", "防窥膜 (Anti-Spy)", 7, 1, 7, "OPP袋子 + 标签"],
        [4, "iPhone 16 Pro/17", None, 3, 2, 6, ""],
        [5, "iPhone 17e / 16e /14 / 13 / 13Pro", "磨砂膜 (Matte)", 15, 0.6, 9, ""],
        [6, "OPP bag packets", "OPP bag packets", 100, 0.03, 3, "OPP packets"],
        [7, "labels", "custom labels", 100, 0.02, 2, "labels"],
    ]
    for row in rows:
        sheet.append(row)
    sheet.append(["（总值）Total:", "", "", "", "", 47, "定金(15%)：120000元"])
    product_start_row = 7 if contract_cells else 5
    sheet.merge_cells(f"C{product_start_row}:C{product_start_row + 1}")
    sheet.merge_cells(f"C{product_start_row + 2}:C{product_start_row + 3}")
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _with_drawing_text_fixture(workbook_bytes: bytes) -> bytes:
    drawing_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
  xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <xdr:twoCellAnchor>
    <xdr:sp>
      <xdr:txBody>
        <a:bodyPr/><a:lstStyle/>
        <a:p><a:r><a:t>合同号 (Invoice No.)：26DRAW001</a:t></a:r></a:p>
        <a:p><a:r><a:t>下单日期 (Date)：2026.5.13</a:t></a:r></a:p>
      </xdr:txBody>
    </xdr:sp>
  </xdr:twoCellAnchor>
</xdr:wsDr>"""
    source = BytesIO(workbook_bytes)
    target = BytesIO()
    with ZipFile(source, "r") as zin, ZipFile(target, "w", ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            zout.writestr(item, zin.read(item.filename))
        zout.writestr("xl/drawings/drawing1.xml", drawing_xml)
    return target.getvalue()


if __name__ == "__main__":
    main()
