"""Contract checks for the compact factory-order recommendation workbook."""

from __future__ import annotations

import io
from pathlib import Path
import sys
from xml.etree import ElementTree as ET
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.factory_order_recommendation_export import (  # noqa: E402
    build_factory_order_recommendation,
)
from packages.application.simple_xlsx import read_first_sheet_cells, read_first_sheet_rows  # noqa: E402


def main() -> None:
    long_name = "clean iPhone 18 Pro Max " + "X" * 125
    workbook = build_factory_order_recommendation(
        rows=[
            (101, long_name, 250),
            (102, "anti-spy iPhone 18 Pro", 0),
            (103, "matte iPhone 18 Pro", 125),
            (104, "clean iPhone 17 Pro", 50),
            (105, "clean iPhone 17 Pro Max", 0),
        ],
        total_quantity=425,
        estimated_weight=36.52,
        estimated_volume=0.18,
        nomenclature_items=[
            {"nm_id": 101, "barcode": "0012345678901", "barcodes": ["0012345678901"], "barcode_status": "ready"},
            {"nm_id": 101, "barcode": "stale", "barcodes": ["stale"], "barcode_status": "ready", "is_active": False},
            {"nm_id": 102, "barcode": "", "barcodes": [], "barcode_status": "missing"},
            {"nm_id": 103, "barcode": "301", "barcodes": ["301", "302"], "barcode_status": "multiple"},
            {"nm_id": 104, "barcode": "000444", "barcodes": ["000444", "old"], "barcode_status": "manual"},
            {"nm_id": 105, "barcode": "000555", "barcodes": ["000555"], "barcode_status": "ready"},
            {"nm_id": 105, "barcode": "000555", "barcodes": ["000555"], "barcode_status": "ready"},
        ],
    )
    rows = read_first_sheet_rows(workbook)
    assert rows[0] == ["nmId", "SKU description", "Barcode", "Recommended order quantity"]
    assert rows[1] == ["101", long_name, "0012345678901", 250]
    assert rows[2] == ["102", "anti-spy iPhone 18 Pro", None, 0]
    assert rows[3] == ["103", "matte iPhone 18 Pro", None, 125]
    assert rows[4] == ["104", "clean iPhone 17 Pro", "000444", 50]
    assert rows[5] == ["105", "clean iPhone 17 Pro Max", None, 0]
    assert rows[-3:] == [
        ["Total quantity", None, None, 425],
        ["Estimated weight, kg", None, None, 36.52],
        ["Estimated volume, m³", None, None, 0.18],
    ]
    cells = read_first_sheet_cells(workbook)
    assert cells[1][0].cell_type == "inlineStr"
    assert cells[1][2].cell_type == "inlineStr"
    assert cells[1][2].raw_value == "0012345678901"
    assert cells[1][2].style_index == 1
    with ZipFile(io.BytesIO(workbook)) as archive:
        assert archive.testzip() is None
        workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
        assert 'name="Recommendation"' in workbook_xml
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        widths = [float(col.attrib["width"]) for col in root.findall("x:cols/x:col", ns)]
        assert len(widths) == 4
        assert widths[1] >= len(long_name) + 3
        assert widths[2] >= len("0012345678901") + 3
        assert widths[3] >= len("Recommended order quantity") + 3
    print("factory_order_recommendation_export_smoke: ok")


if __name__ == "__main__":
    main()
