"""Bounded XLSX parser for supplier invoice shipment registry."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from packages.contracts.supplier_shipments import (
    LINE_TYPE_EXTRA,
    LINE_TYPE_PRODUCT,
    MATCH_STATUS_EXTRA,
    MATCH_STATUS_MATCHED,
    MATCH_STATUS_UNMATCHED,
    PRODUCT_TYPE_ANTI_SPY,
    PRODUCT_TYPE_CLEAR,
    PRODUCT_TYPE_MATTE,
    SUPPLIER_INVOICE_PARSER_VERSION,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ALIAS_MAP_PATH = ROOT / "artifacts" / "supplier_shipments" / "factory_invoice_aliases.json"


@dataclass(frozen=True)
class SupplierInvoiceAlias:
    factory_type: str
    normalized_model: str
    match_key: str
    internal_sku: str
    internal_nm_id: int | None
    internal_name: str
    group: str
    active: bool


def parse_supplier_invoice_xlsx(
    workbook_bytes: bytes,
    *,
    filename: str = "",
    aliases: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Parse a supplier XLSX invoice into editable shipment payload."""

    parser = SupplierInvoiceParser(aliases=aliases)
    return parser.parse(workbook_bytes, filename=filename)


def load_factory_invoice_aliases(path: Path = DEFAULT_ALIAS_MAP_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"factory invoice alias map must be valid JSON: {path}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"factory invoice alias map must contain a JSON object: {path}")
    aliases = payload.get("aliases") or []
    if not isinstance(aliases, list):
        raise ValueError(f"factory invoice alias map aliases must be a list: {path}")
    return [dict(item) for item in aliases if isinstance(item, Mapping)]


class SupplierInvoiceParser:
    def __init__(self, *, aliases: Iterable[Mapping[str, Any]] | None = None) -> None:
        self.aliases = _normalize_aliases(aliases or load_factory_invoice_aliases())

    def parse(self, workbook_bytes: bytes, *, filename: str = "") -> dict[str, Any]:
        if not workbook_bytes:
            raise ValueError("supplier invoice workbook is empty")
        try:
            workbook = load_workbook(BytesIO(workbook_bytes), data_only=True)
        except Exception as exc:  # pragma: no cover - openpyxl owns exact exception types
            raise ValueError(f"supplier invoice workbook must be a readable XLSX file: {exc}") from exc
        if not workbook.worksheets:
            raise ValueError("supplier invoice workbook does not contain worksheets")

        worksheet = workbook.worksheets[0]
        merged_values = _build_merged_value_index(worksheet)
        header_row, columns = _find_header_row(worksheet, merged_values)
        metadata = _extract_metadata(worksheet, merged_values, header_row=header_row, filename=filename)
        lines, warnings = self._parse_lines(worksheet, merged_values, header_row=header_row, columns=columns)
        if metadata.get("declared_invoice_total") is None:
            metadata["declared_invoice_total"] = _extract_declared_invoice_total(
                worksheet,
                merged_values,
                header_row=header_row,
            )
        summary = _build_summary(lines, declared_total=_to_number(metadata.get("declared_invoice_total")))
        warnings.extend(_metadata_warnings(metadata))
        errors: list[str] = []
        if summary.get("checksum_error"):
            errors.append(
                "invoice total checksum mismatch: declared "
                f"{summary.get('declared_invoice_total')} vs parsed {summary.get('invoice_amount_total')}"
            )

        return {
            "parser_version": SUPPLIER_INVOICE_PARSER_VERSION,
            "metadata": metadata,
            "summary": summary,
            "lines": lines,
            "warnings": warnings,
            "errors": errors,
        }

    def _parse_lines(
        self,
        worksheet: Worksheet,
        merged_values: Mapping[tuple[int, int], Any],
        *,
        header_row: int,
        columns: Mapping[str, int],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        lines: list[dict[str, Any]] = []
        warnings: list[str] = []
        current_product_type = ""
        blank_run = 0
        sort_order = 0

        for row_index in range(header_row + 1, worksheet.max_row + 1):
            row_values = {
                key: _cell_value(worksheet, row_index, col_index, merged_values)
                for key, col_index in columns.items()
            }
            row_text = " ".join(_stringify(value) for value in row_values.values() if _stringify(value))
            detected_type = detect_product_type(row_text)

            model_raw = _stringify(row_values.get("models"))
            name_spec = _stringify(row_values.get("name_spec"))
            comment = _stringify(row_values.get("comment")) or name_spec
            qty = _to_number(row_values.get("qty"))
            unit_price = _to_number(row_values.get("unit_price"))
            amount = _to_number(row_values.get("amount"))
            source_no = _stringify(row_values.get("no"))

            if not row_text:
                blank_run += 1
                if blank_run >= 12 and lines:
                    break
                continue
            blank_run = 0

            has_numeric_payload = qty is not None or unit_price is not None or amount is not None
            if lines and _is_total_row(row_text):
                break
            if not (model_raw or name_spec or source_no or has_numeric_payload):
                continue
            if has_numeric_payload and not (model_raw or name_spec or source_no):
                continue

            is_extra = _is_extra_row(source_no, model_raw, name_spec)
            if not is_extra and detected_type:
                current_product_type = detected_type

            if is_extra:
                sort_order += 1
                lines.append(
                    {
                        "line_type": LINE_TYPE_EXTRA,
                        "sort_order": sort_order,
                        "source_no": source_no,
                        "product_type": "",
                        "model_raw": model_raw or name_spec,
                        "model_normalized": normalize_invoice_model(model_raw or name_spec),
                        "match_key": "",
                        "internal_sku": "",
                        "internal_nm_id": None,
                        "internal_name": "",
                        "qty": qty,
                        "unit_price": unit_price,
                        "amount": amount,
                        "currency": "",
                        "comment": comment,
                        "match_status": MATCH_STATUS_EXTRA,
                        "manual_override": False,
                        "raw": _raw_row_payload(row_index, row_values),
                    }
                )
                continue

            if not model_raw and not has_numeric_payload:
                continue
            product_type = detected_type or current_product_type
            normalized_model = normalize_invoice_model(model_raw)
            match_key = f"{product_type}|{normalized_model}" if product_type and normalized_model else ""
            alias = self.aliases.get(match_key)
            match_status = MATCH_STATUS_MATCHED if alias else MATCH_STATUS_UNMATCHED
            if not product_type:
                warnings.append(f"row {row_index}: product type is not detected")
            if not normalized_model:
                warnings.append(f"row {row_index}: model is empty or unsupported")

            sort_order += 1
            lines.append(
                {
                    "line_type": LINE_TYPE_PRODUCT,
                    "sort_order": sort_order,
                    "source_no": source_no,
                    "product_type": product_type,
                    "model_raw": model_raw,
                    "model_normalized": normalized_model,
                    "match_key": match_key,
                    "internal_sku": alias.internal_sku if alias else "",
                    "internal_nm_id": alias.internal_nm_id if alias else None,
                    "internal_name": alias.internal_name if alias else "",
                    "qty": qty,
                    "unit_price": unit_price,
                    "amount": amount,
                    "currency": "",
                    "comment": comment,
                    "match_status": match_status,
                    "manual_override": False,
                    "raw": _raw_row_payload(row_index, row_values),
                }
            )

        if not lines:
            warnings.append("invoice table was found but no editable invoice rows were parsed")
        return lines, warnings


def detect_product_type(value: Any) -> str:
    text = _stringify(value).lower()
    if not text:
        return ""
    if "防窥膜" in text or "anti-spy" in text or "anti spy" in text:
        return PRODUCT_TYPE_ANTI_SPY
    if "磨砂膜" in text or "matte" in text:
        return PRODUCT_TYPE_MATTE
    if "高清膜" in text or re.search(r"\bsmk\b", text):
        return PRODUCT_TYPE_CLEAR
    return ""


def normalize_invoice_model(value: Any) -> str:
    text = _stringify(value).lower()
    if not text:
        return ""
    text = text.replace("（", "(").replace("）", ")")
    text = re.sub(r"\((?:anti[-\s]?spy|matte|smk|clear)[^)]*\)", " ", text, flags=re.IGNORECASE)
    text = text.replace("iphone", "iphone ")
    text = re.sub(r"\bpromax\b", "pro max", text)
    text = re.sub(r"\bip\s*hone\b", "iphone", text)
    text = text.replace("&", " ")
    text = text.replace("+", " plus ")
    text = re.sub(r"[/\\]+", "_", text)
    text = re.sub(r"[-–—]+", "_", text)
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    text = re.sub(r"^iphone_", "iphone_", text)
    return text


def _normalize_aliases(raw_aliases: Iterable[Mapping[str, Any]]) -> dict[str, SupplierInvoiceAlias]:
    aliases: dict[str, SupplierInvoiceAlias] = {}
    for item in raw_aliases:
        active = bool(item.get("active"))
        factory_type = str(item.get("factory_type") or item.get("product_type") or "").strip()
        normalized_model = normalize_invoice_model(item.get("normalized_model") or item.get("factory_model_raw") or "")
        match_key = str(item.get("match_key") or "").strip()
        if not match_key and factory_type and normalized_model:
            match_key = f"{factory_type}|{normalized_model}"
        if not active or not match_key:
            continue
        raw_nm_id = item.get("internal_nm_id") or item.get("nm_id")
        try:
            internal_nm_id = int(raw_nm_id) if raw_nm_id not in {None, ""} else None
        except (TypeError, ValueError):
            internal_nm_id = None
        aliases[match_key] = SupplierInvoiceAlias(
            factory_type=factory_type,
            normalized_model=normalized_model,
            match_key=match_key,
            internal_sku=str(item.get("internal_sku") or item.get("our_sku") or item.get("sku") or "").strip(),
            internal_nm_id=internal_nm_id,
            internal_name=str(
                item.get("internal_name") or item.get("nomenclature_name") or item.get("name") or ""
            ).strip(),
            group=str(item.get("group") or "").strip(),
            active=active,
        )
    return aliases


def _build_merged_value_index(worksheet: Worksheet) -> dict[tuple[int, int], Any]:
    merged_values: dict[tuple[int, int], Any] = {}
    for merged_range in worksheet.merged_cells.ranges:
        top_left = worksheet.cell(merged_range.min_row, merged_range.min_col).value
        for row_index in range(merged_range.min_row, merged_range.max_row + 1):
            for col_index in range(merged_range.min_col, merged_range.max_col + 1):
                merged_values[(row_index, col_index)] = top_left
    return merged_values


def _cell_value(
    worksheet: Worksheet,
    row_index: int,
    col_index: int,
    merged_values: Mapping[tuple[int, int], Any],
) -> Any:
    if (row_index, col_index) in merged_values:
        return merged_values[(row_index, col_index)]
    return worksheet.cell(row_index, col_index).value


def _find_header_row(
    worksheet: Worksheet,
    merged_values: Mapping[tuple[int, int], Any],
) -> tuple[int, dict[str, int]]:
    for row_index in range(1, min(worksheet.max_row, 80) + 1):
        columns: dict[str, int] = {}
        for col_index in range(1, worksheet.max_column + 1):
            normalized = _normalize_header(_cell_value(worksheet, row_index, col_index, merged_values))
            if normalized in {"NO", "NO."}:
                columns["no"] = col_index
            elif "MODEL" in normalized or "型号" in normalized:
                columns["models"] = col_index
            elif "QTY" in normalized or "数量" in normalized:
                columns["qty"] = col_index
            elif normalized in {"U.PRICE", "U PRICE", "UNIT PRICE", "UNITPRICE"} or "单价" in normalized:
                columns["unit_price"] = col_index
            elif "AMOUNT" in normalized or "总价" in normalized or "金额" in normalized:
                columns["amount"] = col_index
            elif ("NAME" in normalized and "SPEC" in normalized) or "品名" in normalized or "规格" in normalized:
                columns["name_spec"] = col_index
            elif normalized in {"COMMENT", "COMMENTS", "REMARK", "REMARKS"} or "备注" in normalized:
                columns["comment"] = col_index
        required = {"no", "models", "qty", "unit_price", "amount"}
        if required.issubset(columns):
            return row_index, columns
    raise ValueError("supplier invoice table headers not found: expected NO., MODELS, QTY, U.PRICE, AMOUNT")


def _extract_metadata(
    worksheet: Worksheet,
    merged_values: Mapping[tuple[int, int], Any],
    *,
    header_row: int,
    filename: str,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "invoice_no": "",
        "invoice_date": "",
        "contract_no": "",
        "contract_date": "",
        "supplier_name": "",
        "customer_name": "",
        "currency": "",
        "declared_invoice_total": None,
    }
    scan_rows = range(1, min(max(header_row, 1), 40) + 1)
    for row_index in scan_rows:
        row = [
            _stringify(_cell_value(worksheet, row_index, col_index, merged_values))
            for col_index in range(1, worksheet.max_column + 1)
        ]
        for col_index, text in enumerate(row, start=1):
            if not text:
                continue
            next_value = _stringify(
                _cell_value(worksheet, row_index, min(col_index + 1, worksheet.max_column), merged_values)
            )
            _maybe_assign_metadata(metadata, text, next_value)
            if not metadata["currency"]:
                currency = _detect_currency(text)
                if currency:
                    metadata["currency"] = currency
            if metadata["declared_invoice_total"] is None and re.search(r"\btotal\b|合计|总计", text, re.IGNORECASE):
                number = _to_number(next_value) or _last_number(text)
                if number is not None:
                    metadata["declared_invoice_total"] = number

    filename_meta = _metadata_from_filename(filename)
    for key, value in filename_meta.items():
        if not metadata.get(key) and value:
            metadata[key] = value
    return metadata


def _extract_declared_invoice_total(
    worksheet: Worksheet,
    merged_values: Mapping[tuple[int, int], Any],
    *,
    header_row: int,
) -> float | None:
    for row_index in range(header_row + 1, min(worksheet.max_row, header_row + 120) + 1):
        row = [
            _cell_value(worksheet, row_index, col_index, merged_values)
            for col_index in range(1, worksheet.max_column + 1)
        ]
        row_text = " ".join(_stringify(value) for value in row if _stringify(value))
        if not _is_total_row(row_text):
            continue
        numeric_values = [
            float(value)
            for value in row
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
        if not numeric_values:
            numeric_values = [number for number in (_to_number(value) for value in row) if number is not None]
        if numeric_values:
            return numeric_values[-1]
    return None


def _maybe_assign_metadata(metadata: dict[str, Any], text: str, next_value: str) -> None:
    normalized = re.sub(r"\s+", " ", text).strip()
    lower = normalized.lower()
    value_after_colon = ""
    if ":" in normalized:
        value_after_colon = normalized.split(":", 1)[1].strip()
    candidate = value_after_colon or next_value
    if not candidate:
        return
    if "invoice" in lower and ("no" in lower or "number" in lower) and not metadata["invoice_no"]:
        metadata["invoice_no"] = candidate
    elif "invoice" in lower and "date" in lower and not metadata["invoice_date"]:
        metadata["invoice_date"] = _normalize_date(candidate) or candidate
    elif "contract" in lower and ("no" in lower or "number" in lower) and not metadata["contract_no"]:
        metadata["contract_no"] = candidate
    elif "contract" in lower and "date" in lower and not metadata["contract_date"]:
        metadata["contract_date"] = _normalize_date(candidate) or candidate
    elif "supplier" in lower and not metadata["supplier_name"]:
        metadata["supplier_name"] = candidate
    elif "customer" in lower and not metadata["customer_name"]:
        metadata["customer_name"] = candidate
    elif "currency" in lower and not metadata["currency"]:
        metadata["currency"] = _detect_currency(candidate) or candidate


def _metadata_from_filename(filename: str) -> dict[str, str]:
    stem = Path(str(filename or "")).stem
    result = {"invoice_no": "", "invoice_date": ""}
    if not stem:
        return result
    date_match = re.search(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})", stem)
    if date_match:
        day, month, year = date_match.groups()
        if len(year) == 2:
            year = "20" + year
        result["invoice_date"] = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    invoice_match = re.search(r"(\d{2}[A-Z]{1,5}\d{2,})", stem, flags=re.IGNORECASE)
    if invoice_match:
        result["invoice_no"] = invoice_match.group(1).upper()
    return result


def _normalize_date(value: Any) -> str:
    text = _stringify(value)
    if not text:
        return ""
    date_match = re.search(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})", text)
    if not date_match:
        return ""
    day, month, year = date_match.groups()
    if len(year) == 2:
        year = "20" + year
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _detect_currency(value: Any) -> str:
    text = _stringify(value).upper()
    if "USD" in text or "$" in text:
        return "USD"
    if "CNY" in text or "RMB" in text or "¥" in text:
        return "RMB"
    if "EUR" in text:
        return "EUR"
    return ""


def _metadata_warnings(metadata: Mapping[str, Any]) -> list[str]:
    warnings: list[str] = []
    if not metadata.get("invoice_no"):
        warnings.append("invoice no was not detected; operator must verify metadata")
    if not metadata.get("invoice_date"):
        warnings.append("invoice date was not detected; operator must verify metadata")
    if not metadata.get("supplier_name"):
        warnings.append("supplier name was not detected; operator must verify metadata")
    return warnings


def _build_summary(lines: list[Mapping[str, Any]], *, declared_total: float | None) -> dict[str, Any]:
    product_qty_total = _sum_numeric(item.get("qty") for item in lines if item.get("line_type") == LINE_TYPE_PRODUCT)
    product_amount_total = _sum_numeric(item.get("amount") for item in lines if item.get("line_type") == LINE_TYPE_PRODUCT)
    extras_amount_total = _sum_numeric(item.get("amount") for item in lines if item.get("line_type") == LINE_TYPE_EXTRA)
    invoice_amount_total = round(product_amount_total + extras_amount_total, 2)
    checksum_error = bool(declared_total is not None and abs(round(declared_total - invoice_amount_total, 2)) > 0.02)
    return {
        "product_qty_total": product_qty_total,
        "product_amount_total": product_amount_total,
        "extras_amount_total": extras_amount_total,
        "invoice_amount_total": invoice_amount_total,
        "declared_invoice_total": declared_total,
        "checksum_error": checksum_error,
    }


def _is_extra_row(*values: Any) -> bool:
    lower = " ".join(_stringify(value) for value in values if _stringify(value)).lower()
    if not lower:
        return False
    strong_markers = (
        "opp",
        "label",
        "labels",
        "card",
        "cards",
        "shipping",
        "freight",
        "售后卡",
        "定制卡",
        "卡片",
        "标签",
        "标贴",
        "运费",
    )
    if any(marker in lower for marker in strong_markers):
        return True
    packaging_markers = ("bag", "packet", "package", "packaging", "袋", "包装")
    return any(marker in lower for marker in packaging_markers) and not detect_product_type(lower) and "iphone" not in lower


def _is_total_row(row_text: str) -> bool:
    return bool(re.search(r"\btotal\b|合计|总计|总值", _stringify(row_text), re.IGNORECASE))


def _normalize_header(value: Any) -> str:
    return re.sub(r"\s+", " ", _stringify(value).upper()).strip()


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _to_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _stringify(value)
    if not text:
        return None
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[^0-9,.\-]", "", text)
    if not text or text in {"-", ".", ","}:
        return None
    if "," in text and "." in text:
        text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _last_number(value: Any) -> float | None:
    matches = re.findall(r"[-+]?\d+(?:[,.]\d+)?", _stringify(value))
    if not matches:
        return None
    return _to_number(matches[-1])


def _sum_numeric(values: Iterable[Any]) -> float:
    total = 0.0
    for value in values:
        number = _to_number(value)
        if number is not None:
            total += number
    return round(total, 2)


def _raw_row_payload(row_index: int, row_values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "worksheet_row": row_index,
        "values": {key: _stringify(value) for key, value in row_values.items()},
    }
