"""Minimal stdlib-only XLSX reader/writer for operator templates."""

from __future__ import annotations

from datetime import datetime, timedelta
import io
from typing import Any
import xml.etree.ElementTree as ET
import zipfile


CellValue = str | int | float | None

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_CORE_PROPS_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
_DC_NS = "http://purl.org/dc/elements/1.1/"
_DCTERMS_NS = "http://purl.org/dc/terms/"
_XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
_APP_NS = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
_VT_NS = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
_DRAWING_MAIN_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"

_BUILTIN_DATE_NUMFMT_IDS = {
    14,
    15,
    16,
    17,
    18,
    19,
    20,
    21,
    22,
    27,
    28,
    29,
    30,
    31,
    32,
    33,
    34,
    35,
    36,
    45,
    46,
    47,
    50,
    51,
    52,
    53,
    54,
    55,
    56,
    57,
    58,
}

ET.register_namespace("r", _REL_NS)
ET.register_namespace("cp", _CORE_PROPS_NS)
ET.register_namespace("dc", _DC_NS)
ET.register_namespace("dcterms", _DCTERMS_NS)
ET.register_namespace("xsi", _XSI_NS)
ET.register_namespace("vt", _VT_NS)
ET.register_namespace("a", _DRAWING_MAIN_NS)

_THEME_XML = """<?xml version="1.0"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Office Theme">
  <a:themeElements>
    <a:clrScheme name="Office">
      <a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>
      <a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>
      <a:dk2><a:srgbClr val="1F497D"/></a:dk2>
      <a:lt2><a:srgbClr val="EEECE1"/></a:lt2>
      <a:accent1><a:srgbClr val="4F81BD"/></a:accent1>
      <a:accent2><a:srgbClr val="C0504D"/></a:accent2>
      <a:accent3><a:srgbClr val="9BBB59"/></a:accent3>
      <a:accent4><a:srgbClr val="8064A2"/></a:accent4>
      <a:accent5><a:srgbClr val="4BACC6"/></a:accent5>
      <a:accent6><a:srgbClr val="F79646"/></a:accent6>
      <a:hlink><a:srgbClr val="0000FF"/></a:hlink>
      <a:folHlink><a:srgbClr val="800080"/></a:folHlink>
    </a:clrScheme>
    <a:fontScheme name="Office">
      <a:majorFont>
        <a:latin typeface="Cambria"/>
        <a:ea typeface=""/>
        <a:cs typeface=""/>
      </a:majorFont>
      <a:minorFont>
        <a:latin typeface="Calibri"/>
        <a:ea typeface=""/>
        <a:cs typeface=""/>
      </a:minorFont>
    </a:fontScheme>
    <a:fmtScheme name="Office">
      <a:fillStyleLst>
        <a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
        <a:gradFill rotWithShape="1">
          <a:gsLst>
            <a:gs pos="0"><a:schemeClr val="phClr"><a:tint val="50000"/><a:satMod val="300000"/></a:schemeClr></a:gs>
            <a:gs pos="35000"><a:schemeClr val="phClr"><a:tint val="37000"/><a:satMod val="300000"/></a:schemeClr></a:gs>
            <a:gs pos="100000"><a:schemeClr val="phClr"><a:tint val="15000"/><a:satMod val="350000"/></a:schemeClr></a:gs>
          </a:gsLst>
          <a:lin ang="16200000" scaled="1"/>
        </a:gradFill>
      </a:fillStyleLst>
      <a:lnStyleLst>
        <a:ln w="9525" cap="flat" cmpd="sng" algn="ctr"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:prstDash val="solid"/></a:ln>
        <a:ln w="25400" cap="flat" cmpd="sng" algn="ctr"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:prstDash val="solid"/></a:ln>
        <a:ln w="38100" cap="flat" cmpd="sng" algn="ctr"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:prstDash val="solid"/></a:ln>
      </a:lnStyleLst>
      <a:effectStyleLst>
        <a:effectStyle><a:effectLst/></a:effectStyle>
        <a:effectStyle><a:effectLst/></a:effectStyle>
        <a:effectStyle><a:effectLst/></a:effectStyle>
      </a:effectStyleLst>
      <a:bgFillStyleLst>
        <a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
        <a:gradFill rotWithShape="1">
          <a:gsLst>
            <a:gs pos="0"><a:schemeClr val="phClr"><a:tint val="40000"/><a:satMod val="350000"/></a:schemeClr></a:gs>
            <a:gs pos="40000"><a:schemeClr val="phClr"><a:tint val="45000"/><a:shade val="99000"/><a:satMod val="350000"/></a:schemeClr></a:gs>
            <a:gs pos="100000"><a:schemeClr val="phClr"><a:shade val="20000"/><a:satMod val="255000"/></a:schemeClr></a:gs>
          </a:gsLst>
          <a:path path="circle"><a:fillToRect l="50000" t="-80000" r="50000" b="180000"/></a:path>
        </a:gradFill>
      </a:bgFillStyleLst>
    </a:fmtScheme>
  </a:themeElements>
  <a:objectDefaults/>
  <a:extraClrSchemeLst/>
</a:theme>
"""


def build_single_sheet_workbook_bytes(sheet_name: str, rows: list[list[CellValue]]) -> bytes:
    normalized_sheet_name = _normalize_sheet_name(sheet_name)
    created_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _build_content_types_xml())
        archive.writestr("_rels/.rels", _build_root_rels_xml())
        archive.writestr("docProps/app.xml", _build_app_props_xml(normalized_sheet_name))
        archive.writestr("docProps/core.xml", _build_core_props_xml(created_at))
        archive.writestr("xl/workbook.xml", _build_workbook_xml(normalized_sheet_name))
        archive.writestr("xl/_rels/workbook.xml.rels", _build_workbook_rels_xml())
        archive.writestr("xl/theme/theme1.xml", _THEME_XML.encode("utf-8"))
        archive.writestr("xl/styles.xml", _build_styles_xml())
        archive.writestr("xl/worksheets/sheet1.xml", _build_sheet_xml(rows))
    return buffer.getvalue()


def read_first_sheet_rows(workbook_bytes: bytes) -> list[list[Any]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(workbook_bytes), "r")
    except zipfile.BadZipFile as exc:
        raise ValueError("файл должен быть валидным XLSX") from exc

    with archive:
        workbook_xml = _read_xml(archive, "xl/workbook.xml")
        workbook_rels_xml = _read_xml(archive, "xl/_rels/workbook.xml.rels")
        sheet_path = _resolve_first_sheet_path(workbook_xml, workbook_rels_xml)
        shared_strings = _load_shared_strings(archive)
        date_style_indexes = _load_date_style_indexes(archive)
        sheet_xml = _read_xml(archive, sheet_path)
        return _parse_sheet_rows(sheet_xml, shared_strings=shared_strings, date_style_indexes=date_style_indexes)


def _build_content_types_xml() -> bytes:
    root = ET.Element(f"{{{_CONTENT_NS}}}Types")
    ET.SubElement(
        root,
        f"{{{_CONTENT_NS}}}Default",
        Extension="rels",
        ContentType="application/vnd.openxmlformats-package.relationships+xml",
    )
    ET.SubElement(
        root,
        f"{{{_CONTENT_NS}}}Default",
        Extension="xml",
        ContentType="application/xml",
    )
    overrides = [
        ("/docProps/app.xml", "application/vnd.openxmlformats-officedocument.extended-properties+xml"),
        ("/docProps/core.xml", "application/vnd.openxmlformats-package.core-properties+xml"),
        ("/xl/styles.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"),
        ("/xl/theme/theme1.xml", "application/vnd.openxmlformats-officedocument.theme+xml"),
        ("/xl/workbook.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"),
        ("/xl/worksheets/sheet1.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"),
    ]
    for part_name, content_type in overrides:
        ET.SubElement(
            root,
            f"{{{_CONTENT_NS}}}Override",
            PartName=part_name,
            ContentType=content_type,
        )
    return _xml_bytes(root)


def _build_root_rels_xml() -> bytes:
    root = ET.Element(f"{{{_PKG_REL_NS}}}Relationships")
    ET.SubElement(
        root,
        f"{{{_PKG_REL_NS}}}Relationship",
        Id="rId1",
        Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
        Target="xl/workbook.xml",
    )
    ET.SubElement(
        root,
        f"{{{_PKG_REL_NS}}}Relationship",
        Id="rId2",
        Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties",
        Target="docProps/core.xml",
    )
    ET.SubElement(
        root,
        f"{{{_PKG_REL_NS}}}Relationship",
        Id="rId3",
        Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties",
        Target="docProps/app.xml",
    )
    return _xml_bytes(root)


def _build_app_props_xml(sheet_name: str) -> bytes:
    root = ET.Element(f"{{{_APP_NS}}}Properties")
    ET.SubElement(root, f"{{{_APP_NS}}}Application").text = "wb-core"
    ET.SubElement(root, f"{{{_APP_NS}}}DocSecurity").text = "0"
    ET.SubElement(root, f"{{{_APP_NS}}}ScaleCrop").text = "false"
    headings = ET.SubElement(root, f"{{{_APP_NS}}}HeadingPairs")
    vector = ET.SubElement(headings, f"{{{_VT_NS}}}vector", size="2", baseType="variant")
    variant_a = ET.SubElement(vector, f"{{{_VT_NS}}}variant")
    ET.SubElement(variant_a, f"{{{_VT_NS}}}lpstr").text = "Worksheets"
    variant_b = ET.SubElement(vector, f"{{{_VT_NS}}}variant")
    ET.SubElement(variant_b, f"{{{_VT_NS}}}i4").text = "1"
    titles = ET.SubElement(root, f"{{{_APP_NS}}}TitlesOfParts")
    titles_vector = ET.SubElement(titles, f"{{{_VT_NS}}}vector", size="1", baseType="lpstr")
    ET.SubElement(titles_vector, f"{{{_VT_NS}}}lpstr").text = sheet_name
    return _xml_bytes(root)


def _build_core_props_xml(created_at: str) -> bytes:
    root = ET.Element(f"{{{_CORE_PROPS_NS}}}coreProperties")
    ET.SubElement(root, f"{{{_DC_NS}}}creator").text = "wb-core"
    ET.SubElement(root, f"{{{_DC_NS}}}title").text = "wb-core operator template"
    ET.SubElement(root, f"{{{_DCTERMS_NS}}}created", {f"{{{_XSI_NS}}}type": "dcterms:W3CDTF"}).text = created_at
    ET.SubElement(root, f"{{{_DCTERMS_NS}}}modified", {f"{{{_XSI_NS}}}type": "dcterms:W3CDTF"}).text = created_at
    return _xml_bytes(root)


def _build_workbook_xml(sheet_name: str) -> bytes:
    root = ET.Element(f"{{{_MAIN_NS}}}workbook")
    ET.SubElement(root, f"{{{_MAIN_NS}}}workbookPr")
    ET.SubElement(root, f"{{{_MAIN_NS}}}workbookProtection")
    book_views = ET.SubElement(root, f"{{{_MAIN_NS}}}bookViews")
    ET.SubElement(
        book_views,
        f"{{{_MAIN_NS}}}workbookView",
        visibility="visible",
        minimized="0",
        showHorizontalScroll="1",
        showVerticalScroll="1",
        showSheetTabs="1",
        tabRatio="600",
        firstSheet="0",
        activeTab="0",
        autoFilterDateGrouping="1",
    )
    sheets = ET.SubElement(root, f"{{{_MAIN_NS}}}sheets")
    ET.SubElement(
        sheets,
        f"{{{_MAIN_NS}}}sheet",
        {
            "name": sheet_name,
            "sheetId": "1",
            "state": "visible",
            f"{{{_REL_NS}}}id": "rId1",
        },
    )
    ET.SubElement(root, f"{{{_MAIN_NS}}}definedNames")
    ET.SubElement(root, f"{{{_MAIN_NS}}}calcPr", calcId="124519", fullCalcOnLoad="1")
    return _xml_bytes(root)


def _build_workbook_rels_xml() -> bytes:
    root = ET.Element(f"{{{_PKG_REL_NS}}}Relationships")
    ET.SubElement(
        root,
        f"{{{_PKG_REL_NS}}}Relationship",
        Id="rId1",
        Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet",
        Target="worksheets/sheet1.xml",
    )
    ET.SubElement(
        root,
        f"{{{_PKG_REL_NS}}}Relationship",
        Id="rId2",
        Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles",
        Target="styles.xml",
    )
    ET.SubElement(
        root,
        f"{{{_PKG_REL_NS}}}Relationship",
        Id="rId3",
        Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme",
        Target="theme/theme1.xml",
    )
    return _xml_bytes(root)


def _build_styles_xml() -> bytes:
    root = ET.Element(f"{{{_MAIN_NS}}}styleSheet")
    ET.SubElement(root, f"{{{_MAIN_NS}}}numFmts", count="0")
    fonts = ET.SubElement(root, f"{{{_MAIN_NS}}}fonts", count="1")
    font = ET.SubElement(fonts, f"{{{_MAIN_NS}}}font")
    ET.SubElement(font, f"{{{_MAIN_NS}}}name", val="Calibri")
    ET.SubElement(font, f"{{{_MAIN_NS}}}family", val="2")
    ET.SubElement(font, f"{{{_MAIN_NS}}}color", theme="1")
    ET.SubElement(font, f"{{{_MAIN_NS}}}sz", val="11")
    ET.SubElement(font, f"{{{_MAIN_NS}}}scheme", val="minor")
    fills = ET.SubElement(root, f"{{{_MAIN_NS}}}fills", count="2")
    fill_none = ET.SubElement(fills, f"{{{_MAIN_NS}}}fill")
    ET.SubElement(fill_none, f"{{{_MAIN_NS}}}patternFill")
    fill_gray = ET.SubElement(fills, f"{{{_MAIN_NS}}}fill")
    ET.SubElement(fill_gray, f"{{{_MAIN_NS}}}patternFill", patternType="gray125")
    borders = ET.SubElement(root, f"{{{_MAIN_NS}}}borders", count="1")
    border = ET.SubElement(borders, f"{{{_MAIN_NS}}}border")
    ET.SubElement(border, f"{{{_MAIN_NS}}}left")
    ET.SubElement(border, f"{{{_MAIN_NS}}}right")
    ET.SubElement(border, f"{{{_MAIN_NS}}}top")
    ET.SubElement(border, f"{{{_MAIN_NS}}}bottom")
    ET.SubElement(border, f"{{{_MAIN_NS}}}diagonal")
    cell_style_xfs = ET.SubElement(root, f"{{{_MAIN_NS}}}cellStyleXfs", count="1")
    ET.SubElement(
        cell_style_xfs,
        f"{{{_MAIN_NS}}}xf",
        numFmtId="0",
        fontId="0",
        fillId="0",
        borderId="0",
    )
    cell_xfs = ET.SubElement(root, f"{{{_MAIN_NS}}}cellXfs", count="1")
    ET.SubElement(
        cell_xfs,
        f"{{{_MAIN_NS}}}xf",
        numFmtId="0",
        fontId="0",
        fillId="0",
        borderId="0",
        xfId="0",
        pivotButton="0",
        quotePrefix="0",
    )
    cell_styles = ET.SubElement(root, f"{{{_MAIN_NS}}}cellStyles", count="1")
    ET.SubElement(cell_styles, f"{{{_MAIN_NS}}}cellStyle", name="Normal", xfId="0", builtinId="0", hidden="0")
    ET.SubElement(
        root,
        f"{{{_MAIN_NS}}}tableStyles",
        count="0",
        defaultTableStyle="TableStyleMedium9",
        defaultPivotStyle="PivotStyleLight16",
    )
    return _xml_bytes(root)


def _build_sheet_xml(rows: list[list[CellValue]]) -> bytes:
    root = ET.Element(f"{{{_MAIN_NS}}}worksheet")
    sheet_pr = ET.SubElement(root, f"{{{_MAIN_NS}}}sheetPr")
    ET.SubElement(sheet_pr, f"{{{_MAIN_NS}}}outlinePr", summaryBelow="1", summaryRight="1")
    ET.SubElement(sheet_pr, f"{{{_MAIN_NS}}}pageSetUpPr")
    ET.SubElement(root, f"{{{_MAIN_NS}}}dimension", ref=_sheet_dimension(rows))
    sheet_views = ET.SubElement(root, f"{{{_MAIN_NS}}}sheetViews")
    sheet_view = ET.SubElement(sheet_views, f"{{{_MAIN_NS}}}sheetView", workbookViewId="0")
    ET.SubElement(sheet_view, f"{{{_MAIN_NS}}}selection", activeCell="A1", sqref="A1")
    ET.SubElement(root, f"{{{_MAIN_NS}}}sheetFormatPr", baseColWidth="8", defaultRowHeight="15")
    sheet_data = ET.SubElement(root, f"{{{_MAIN_NS}}}sheetData")
    for row_index, row in enumerate(rows, start=1):
        row_el = ET.SubElement(sheet_data, f"{{{_MAIN_NS}}}row", r=str(row_index))
        for col_index, value in enumerate(row, start=1):
            if value in (None, ""):
                continue
            cell_ref = f"{_column_name(col_index)}{row_index}"
            if isinstance(value, bool):
                cell = ET.SubElement(row_el, f"{{{_MAIN_NS}}}c", r=cell_ref, t="inlineStr")
                is_el = ET.SubElement(cell, f"{{{_MAIN_NS}}}is")
                ET.SubElement(is_el, f"{{{_MAIN_NS}}}t").text = "TRUE" if value else "FALSE"
                continue
            if isinstance(value, (int, float)):
                cell = ET.SubElement(row_el, f"{{{_MAIN_NS}}}c", r=cell_ref, t="n")
                ET.SubElement(cell, f"{{{_MAIN_NS}}}v").text = _format_number(value)
                continue
            cell = ET.SubElement(row_el, f"{{{_MAIN_NS}}}c", r=cell_ref, t="inlineStr")
            is_el = ET.SubElement(cell, f"{{{_MAIN_NS}}}is")
            text = ET.SubElement(is_el, f"{{{_MAIN_NS}}}t")
            text.text = str(value)
            if str(value).strip() != str(value):
                text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    ET.SubElement(
        root,
        f"{{{_MAIN_NS}}}pageMargins",
        left="0.75",
        right="0.75",
        top="1",
        bottom="1",
        header="0.5",
        footer="0.5",
    )
    return _xml_bytes(root)


def _resolve_first_sheet_path(workbook_xml: ET.Element, workbook_rels_xml: ET.Element) -> str:
    relationships: dict[str, str] = {}
    for rel in workbook_rels_xml.findall(f"{{{_PKG_REL_NS}}}Relationship"):
        rel_id = rel.get("Id", "")
        target = rel.get("Target", "")
        if rel_id and target:
            relationships[rel_id] = target
    sheets = workbook_xml.find(f"{{{_MAIN_NS}}}sheets")
    if sheets is None:
        raise ValueError("XLSX workbook does not contain sheets")
    first_sheet = sheets.find(f"{{{_MAIN_NS}}}sheet")
    if first_sheet is None:
        raise ValueError("XLSX workbook does not contain a readable sheet")
    rel_id = first_sheet.get(f"{{{_REL_NS}}}id", "")
    target = relationships.get(rel_id)
    if not target:
        raise ValueError("XLSX workbook sheet relationship is missing")
    normalized = target.lstrip("/")
    if normalized.startswith("xl/"):
        return normalized
    return f"xl/{normalized}"


def _load_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _read_xml(archive, "xl/sharedStrings.xml")
    items: list[str] = []
    for item in root.findall(f"{{{_MAIN_NS}}}si"):
        items.append(_extract_text(item))
    return items


def _load_date_style_indexes(archive: zipfile.ZipFile) -> set[int]:
    if "xl/styles.xml" not in archive.namelist():
        return set()
    root = _read_xml(archive, "xl/styles.xml")
    custom_numfmts: dict[int, str] = {}
    numfmts = root.find(f"{{{_MAIN_NS}}}numFmts")
    if numfmts is not None:
        for numfmt in numfmts.findall(f"{{{_MAIN_NS}}}numFmt"):
            try:
                numfmt_id = int(numfmt.get("numFmtId", ""))
            except ValueError:
                continue
            custom_numfmts[numfmt_id] = str(numfmt.get("formatCode", "") or "")
    indexes: set[int] = set()
    cell_xfs = root.find(f"{{{_MAIN_NS}}}cellXfs")
    if cell_xfs is None:
        return indexes
    for index, xf in enumerate(cell_xfs.findall(f"{{{_MAIN_NS}}}xf")):
        try:
            numfmt_id = int(xf.get("numFmtId", "0"))
        except ValueError:
            continue
        format_code = custom_numfmts.get(numfmt_id, "")
        if numfmt_id in _BUILTIN_DATE_NUMFMT_IDS or _format_code_is_date_like(format_code):
            indexes.add(index)
    return indexes


def _parse_sheet_rows(
    root: ET.Element,
    *,
    shared_strings: list[str],
    date_style_indexes: set[int],
) -> list[list[Any]]:
    parsed_rows: list[list[Any]] = []
    sheet_data = root.find(f"{{{_MAIN_NS}}}sheetData")
    if sheet_data is None:
        return parsed_rows
    for row_el in sheet_data.findall(f"{{{_MAIN_NS}}}row"):
        values_by_col: dict[int, Any] = {}
        max_col = 0
        for cell in row_el.findall(f"{{{_MAIN_NS}}}c"):
            cell_ref = cell.get("r", "")
            col_index = _column_index_from_ref(cell_ref)
            if col_index <= 0:
                continue
            max_col = max(max_col, col_index)
            cell_type = cell.get("t", "")
            try:
                style_index = int(cell.get("s", "0") or "0")
            except ValueError:
                style_index = 0
            values_by_col[col_index] = _parse_cell_value(
                cell,
                cell_type=cell_type,
                style_index=style_index,
                shared_strings=shared_strings,
                date_style_indexes=date_style_indexes,
            )
        if max_col == 0:
            parsed_rows.append([])
            continue
        parsed_rows.append([values_by_col.get(index) for index in range(1, max_col + 1)])
    return _trim_empty_rows(parsed_rows)


def _parse_cell_value(
    cell: ET.Element,
    *,
    cell_type: str,
    style_index: int,
    shared_strings: list[str],
    date_style_indexes: set[int],
) -> Any:
    if cell_type == "inlineStr":
        is_el = cell.find(f"{{{_MAIN_NS}}}is")
        return _extract_text(is_el) if is_el is not None else ""
    value_el = cell.find(f"{{{_MAIN_NS}}}v")
    value_text = value_el.text if value_el is not None else ""
    if cell_type == "s":
        try:
            return shared_strings[int(value_text or "0")]
        except (IndexError, ValueError) as exc:
            raise ValueError("XLSX shared string index is invalid") from exc
    if cell_type == "str":
        return value_text or ""
    if value_text in ("", None):
        return ""
    if style_index in date_style_indexes:
        return _excel_serial_to_iso_date(value_text)
    try:
        numeric = float(value_text)
    except ValueError:
        return value_text
    if numeric.is_integer():
        return int(numeric)
    return numeric


def _excel_serial_to_iso_date(value_text: str) -> str:
    try:
        serial = float(value_text)
    except ValueError as exc:
        raise ValueError("XLSX date cell must contain a numeric serial value") from exc
    base = datetime(1899, 12, 30)
    converted = base + timedelta(days=serial)
    return converted.date().isoformat()


def _extract_text(element: ET.Element) -> str:
    parts = [node.text or "" for node in element.findall(".//{%s}t" % _MAIN_NS)]
    if parts:
        return "".join(parts)
    return element.text or ""


def _sheet_dimension(rows: list[list[CellValue]]) -> str:
    if not rows:
        return "A1"
    max_row = max(len(rows), 1)
    max_col = max((len(row) for row in rows), default=1)
    return f"A1:{_column_name(max_col)}{max_row}"


def _column_name(index: int) -> str:
    result = []
    value = index
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        result.append(chr(ord("A") + remainder))
    return "".join(reversed(result))


def _column_index_from_ref(cell_ref: str) -> int:
    letters = []
    for char in cell_ref:
        if char.isalpha():
            letters.append(char.upper())
        else:
            break
    if not letters:
        return 0
    value = 0
    for char in letters:
        value = value * 26 + (ord(char) - ord("A") + 1)
    return value


def _trim_empty_rows(rows: list[list[Any]]) -> list[list[Any]]:
    trimmed: list[list[Any]] = []
    last_nonempty_index = -1
    for index, row in enumerate(rows):
        normalized = list(row)
        while normalized and normalized[-1] in ("", None):
            normalized.pop()
        trimmed.append(normalized)
        if any(value not in ("", None) for value in normalized):
            last_nonempty_index = index
    if last_nonempty_index < 0:
        return []
    return trimmed[: last_nonempty_index + 1]


def _format_number(value: int | float) -> str:
    numeric = float(value)
    if numeric.is_integer():
        return str(int(numeric))
    return str(numeric)


def _format_code_is_date_like(format_code: str) -> bool:
    normalized = format_code.lower()
    if not normalized:
        return False
    in_quote = False
    cleaned = []
    for char in normalized:
        if char == '"':
            in_quote = not in_quote
            continue
        if in_quote:
            continue
        cleaned.append(char)
    normalized = "".join(cleaned)
    return any(token in normalized for token in ("yy", "dd", "mm", "m/", "d/"))


def _normalize_sheet_name(sheet_name: str) -> str:
    normalized = str(sheet_name or "").strip() or "Sheet1"
    invalid_chars = set("[]:*?/\\")
    normalized = "".join("_" if char in invalid_chars else char for char in normalized)
    return normalized[:31]


def _read_xml(archive: zipfile.ZipFile, path: str) -> ET.Element:
    try:
        raw = archive.read(path)
    except KeyError as exc:
        raise ValueError(f"XLSX part is missing: {path}") from exc
    return ET.fromstring(raw)


def _xml_bytes(root: ET.Element) -> bytes:
    if root.tag.startswith("{"):
        namespace = root.tag[1:].split("}", 1)[0]
        if namespace in {_MAIN_NS, _PKG_REL_NS, _CONTENT_NS, _APP_NS, _CORE_PROPS_NS}:
            ET.register_namespace("", namespace)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


__all__ = [
    "build_single_sheet_workbook_bytes",
    "read_first_sheet_rows",
]
