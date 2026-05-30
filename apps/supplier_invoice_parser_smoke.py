"""Smoke-check supplier invoice XLSX parser and deterministic matching."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import sys

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.supplier_invoice_parser import parse_supplier_invoice_xlsx  # noqa: E402


def main() -> None:
    workbook_bytes = _build_invoice_fixture()
    payload = parse_supplier_invoice_xlsx(
        workbook_bytes,
        filename="PI-test 26GN390 (14.5.2026).xlsx",
        aliases=[
            {
                "factory_type": "clear",
                "normalized_model": "iphone_14_pro",
                "match_key": "clear|iphone_14_pro",
                "internal_sku": "SKU-CLEAR-14PRO",
                "internal_nm_id": 210183919,
                "internal_name": "Clear iPhone 14 Pro",
                "group": "clear",
                "active": True,
            }
        ],
    )
    lines = payload["lines"]
    product_lines = [line for line in lines if line["line_type"] == "product"]
    extra_lines = [line for line in lines if line["line_type"] == "extra"]
    if len(product_lines) != 5 or len(extra_lines) != 2:
        raise AssertionError(f"parser must keep product and extra rows separately, got {len(product_lines)} / {len(extra_lines)}")
    if [line["product_type"] for line in product_lines] != ["clear", "clear", "anti_spy", "anti_spy", "matte"]:
        raise AssertionError("parser must fill down clear/anti_spy/matte markers from Chinese/comment blocks")
    if product_lines[0]["match_status"] != "matched" or product_lines[0]["internal_nm_id"] != 210183919:
        raise AssertionError("active deterministic alias must match the exact type+normalized model key")
    if product_lines[1]["match_key"] != "clear|iphone_15_16" or product_lines[1]["match_status"] != "unmatched":
        raise AssertionError("compatible model aliases like iPhone 15 / 16 must stay one unmatched invoice alias")
    if product_lines[3]["match_key"] != "anti_spy|iphone_16_pro_17":
        raise AssertionError("iPhone 16 Pro/17 must stay one anti_spy invoice alias")
    if product_lines[4]["match_key"] != "matte|iphone_17e_16e_14_13_13pro":
        raise AssertionError("multi-compatible matte alias must not be split into separate models")
    summary = payload["summary"]
    if summary["product_qty_total"] != 40.0 or summary["product_amount_total"] != 42.0:
        raise AssertionError(f"product totals mismatch: {summary}")
    if summary["extras_amount_total"] != 5.0 or summary["invoice_amount_total"] != 47.0:
        raise AssertionError(f"invoice totals mismatch: {summary}")
    if summary["checksum_error"]:
        raise AssertionError("declared invoice total must match parsed total in fixture")
    print("supplier_invoice_parser_smoke: OK")


def _build_invoice_fixture() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Invoice"
    sheet["A1"] = "Invoice No:"
    sheet["B1"] = "26GN390"
    sheet["A2"] = "Invoice Date:"
    sheet["B2"] = "14.5.2026"
    sheet["A3"] = "Supplier:"
    sheet["B3"] = "Zhejiang Supplier"
    sheet["D3"] = "Currency:"
    sheet["E3"] = "USD"
    sheet["A4"] = "Invoice Total:"
    sheet["B4"] = 47
    headers = ["NO.", "NAME & SPECIFICATION", "MODELS", "QTY", "U.PRICE", "AMOUNT", "COMMENT"]
    sheet.append(headers)
    rows = [
        [1, "高清膜 smk", "iPhone 14 Pro", 10, 1, 10, ""],
        [2, None, "iPhone 15 / 16", 5, 2, 10, ""],
        [3, "防窥膜 (Anti-Spy)", "iPhone 14 Pro Max", 7, 1, 7, ""],
        [4, None, "iPhone 16 Pro/17", 3, 2, 6, ""],
        [5, "磨砂膜 (Matte)", "iPhone 17e / 16e /14 / 13 / 13Pro", 15, 0.6, 9, ""],
        [6, "OPP bag packets", "", 100, 0.03, 3, "OPP packets"],
        [7, "labels", "", 100, 0.02, 2, "labels"],
    ]
    for row in rows:
        sheet.append(row)
    sheet.merge_cells("B6:B7")
    sheet.merge_cells("B8:B9")
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


if __name__ == "__main__":
    main()
