"""Supplier order financial document parsing, storage and summary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any, Callable, Mapping
from urllib import parse as urllib_parse
from urllib import request as urllib_request
from uuid import uuid4
import xml.etree.ElementTree as ET

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.contracts.supplier_financial_documents import (
    EXPENSE_CATEGORY_BORDER_EXPEDITION,
    EXPENSE_CATEGORY_BROKERAGE,
    EXPENSE_CATEGORY_COMPANY_COMMISSION,
    EXPENSE_CATEGORY_CUSTOMS_FEE_1010,
    EXPENSE_CATEGORY_CUSTOMS_PAYMENTS,
    EXPENSE_CATEGORY_DELIVERY,
    EXPENSE_CATEGORY_DOMESTIC_TRANSPORT,
    EXPENSE_CATEGORY_ECOLOGICAL_FEE,
    EXPENSE_CATEGORY_EXPORT_DOCS,
    EXPENSE_CATEGORY_IMPORT_DUTY_2010,
    EXPENSE_CATEGORY_IMPORT_VAT_5010,
    EXPENSE_CATEGORY_INSURANCE,
    EXPENSE_CATEGORY_PACKAGING,
    EXPENSE_CATEGORY_PERMISSION_DOCS,
    EXPENSE_LINE_STATUS_NEEDS_REVIEW,
    EXPENSE_LINE_STATUS_PARSED,
    EXPENSE_LINE_STATUS_POSSIBLE_NOT_INCLUDED,
    FINANCIAL_DOCUMENT_ALLOWED_EXTENSIONS,
    FINANCIAL_DOCUMENT_CONTENT_TYPE,
    FINANCIAL_DOCUMENT_PARSE_STATUS_EXCLUDED,
    FINANCIAL_DOCUMENT_PARSE_STATUS_NEEDS_REVIEW,
    FINANCIAL_DOCUMENT_PARSE_STATUS_PARSED,
    FINANCIAL_DOCUMENT_PARSE_STATUS_PARSE_ERROR,
    FINANCIAL_DOCUMENT_PARSE_STATUSES,
    FINANCIAL_DOCUMENT_PARSER_VERSION,
    FINANCIAL_DOCUMENT_TYPE_CUSTOMS_DECLARATION,
    FINANCIAL_DOCUMENT_TYPE_LOGISTICS_INVOICE,
    FINANCIAL_DOCUMENT_TYPE_LOGISTICS_QUOTE,
    FX_RATE_SOURCE_CBR,
    FX_RATE_STATUS_MISSING,
    FX_RATE_STATUS_OK,
    FX_RATE_STATUS_PENDING,
)

MONEY_QUANT = Decimal("0.01")
RATE_QUANT = Decimal("0.0001")
PCT_QUANT = Decimal("0.0001")


@dataclass(frozen=True)
class UsdRateResult:
    requested_date: str
    effective_date: str
    rate_value: Decimal | None
    source: str
    status: str
    error: str = ""


class CbrUsdRateProvider:
    """Official CBR XML daily-rate provider with previous-effective-date semantics."""

    def __init__(self, *, timeout_seconds: float = 6.0) -> None:
        self.timeout_seconds = timeout_seconds

    def get_usd_rate(self, requested_date: str) -> UsdRateResult:
        normalized = _optional_iso_date(requested_date)
        if not normalized:
            return UsdRateResult(
                requested_date=str(requested_date or ""),
                effective_date="",
                rate_value=None,
                source=FX_RATE_SOURCE_CBR,
                status=FX_RATE_STATUS_MISSING,
                error="requested_date_missing",
            )
        try:
            parsed = date.fromisoformat(normalized)
            query = urllib_parse.urlencode({"date_req": parsed.strftime("%d/%m/%Y")})
            url = f"https://www.cbr.ru/scripts/XML_daily.asp?{query}"
            with urllib_request.urlopen(url, timeout=self.timeout_seconds) as response:
                payload = response.read()
            root = ET.fromstring(payload)
            effective_raw = str(root.attrib.get("Date") or "").strip()
            effective_date = _parse_date(effective_raw) or normalized
            for valute in root.findall("Valute"):
                if (valute.findtext("CharCode") or "").strip().upper() != "USD":
                    continue
                value = _parse_decimal(valute.findtext("Value") or "")
                nominal = _parse_decimal(valute.findtext("Nominal") or "1") or Decimal("1")
                if value is None or nominal == 0:
                    break
                return UsdRateResult(
                    requested_date=normalized,
                    effective_date=effective_date,
                    rate_value=_quantize_rate(value / nominal),
                    source=FX_RATE_SOURCE_CBR,
                    status=FX_RATE_STATUS_OK,
                )
            return UsdRateResult(
                requested_date=normalized,
                effective_date=effective_date,
                rate_value=None,
                source=FX_RATE_SOURCE_CBR,
                status=FX_RATE_STATUS_MISSING,
                error="usd_rate_not_found",
            )
        except Exception as exc:
            return UsdRateResult(
                requested_date=normalized,
                effective_date="",
                rate_value=None,
                source=FX_RATE_SOURCE_CBR,
                status=FX_RATE_STATUS_PENDING,
                error=str(exc),
            )


class StaticUsdRateProvider:
    """Test/development rate provider. No network access."""

    def __init__(self, rates: Mapping[str, Decimal | float | int | str]) -> None:
        self.rates = {str(key): _parse_decimal(value) for key, value in rates.items()}

    def get_usd_rate(self, requested_date: str) -> UsdRateResult:
        normalized = _optional_iso_date(requested_date)
        value = self.rates.get(normalized or "")
        if normalized and value is not None:
            return UsdRateResult(
                requested_date=normalized,
                effective_date=normalized,
                rate_value=_quantize_rate(value),
                source="fixture",
                status=FX_RATE_STATUS_OK,
            )
        return UsdRateResult(
            requested_date=normalized or str(requested_date or ""),
            effective_date="",
            rate_value=None,
            source="fixture",
            status=FX_RATE_STATUS_MISSING,
            error="fixture_rate_missing",
        )


TextExtractor = Callable[[bytes, str], tuple[str, dict[str, Any], list[str]]]


class SupplierFinancialDocumentsBlock:
    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        timestamp_factory: Callable[[], str] | None = None,
        usd_rate_provider: Any | None = None,
        pdf_text_extractor: TextExtractor | None = None,
    ) -> None:
        self.runtime = runtime
        self.timestamp_factory = timestamp_factory or _default_timestamp_factory
        self.usd_rate_provider = usd_rate_provider or CbrUsdRateProvider()
        self.pdf_text_extractor = pdf_text_extractor or extract_pdf_text_layer

    def list_documents(self, supplier_order_id: str) -> dict[str, Any]:
        self._ensure_supplier_order(supplier_order_id)
        documents = [self._with_download_path(item) for item in self.runtime.list_supplier_financial_documents(supplier_order_id)]
        lines = self.runtime.list_supplier_financial_expense_lines(supplier_order_id)
        return {
            "contract_name": "sheet_vitrina_v1_supplier_financial_documents",
            "status": "ok",
            "supplier_order_id": supplier_order_id,
            "documents": documents,
            "expense_lines": lines,
            "summary": build_financial_summary(documents, lines),
        }

    def get_document(self, supplier_order_id: str, document_id: str) -> dict[str, Any]:
        self._ensure_supplier_order(supplier_order_id)
        document = self.runtime.load_supplier_financial_document(
            supplier_order_id=supplier_order_id,
            document_id=document_id,
        )
        if document is None:
            raise ValueError(f"financial document not found: {document_id}")
        documents = [self._with_download_path(document)]
        lines = list(document.get("expense_lines") or [])
        payload = self._with_download_path(document)
        payload["expense_lines"] = lines
        payload["summary"] = build_financial_summary(documents, lines)
        return payload

    def upload_document(
        self,
        supplier_order_id: str,
        *,
        file_bytes: bytes,
        uploaded_filename: str | None = None,
        uploaded_content_type: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_supplier_order(supplier_order_id)
        if not file_bytes:
            raise ValueError("financial document upload file is empty")
        filename = _safe_filename(uploaded_filename or "financial-document.pdf")
        if Path(filename).suffix.lower() not in FINANCIAL_DOCUMENT_ALLOWED_EXTENSIONS:
            raise ValueError("financial document upload must be a PDF file")
        content_type = str(uploaded_content_type or "").split(";", 1)[0].strip().lower() or FINANCIAL_DOCUMENT_CONTENT_TYPE
        now = self.timestamp_factory()
        document_id = "fdoc_" + uuid4().hex
        file_sha256 = hashlib.sha256(file_bytes).hexdigest()
        stored_file_path = self._write_document_file(
            supplier_order_id=supplier_order_id,
            document_id=document_id,
            filename=filename,
            body=file_bytes,
        )
        parsed = parse_financial_document_pdf(
            file_bytes,
            filename=filename,
            text_extractor=self.pdf_text_extractor,
        )
        normalized = dict(parsed.get("normalized_parse") or {})
        raw_parse = dict(parsed.get("raw_parse") or {})
        warnings = _string_list(parsed.get("warnings"))
        errors = _string_list(parsed.get("errors"))
        expense_lines = [dict(item) for item in parsed.get("expense_lines") or []]
        rate_result = self._rate_for_document(normalized)
        if rate_result is not None:
            normalized["cbr_usd_rate"] = _rate_result_to_dict(rate_result)
            if rate_result.status != FX_RATE_STATUS_OK:
                warnings.append(f"CBR USD rate is pending or missing for {rate_result.requested_date}")
            else:
                _apply_usd_rate_to_parse(normalized, expense_lines, rate_result.rate_value)
        parse_status = _parse_status_for_payload(parsed, warnings, errors, rate_result)
        if parse_status == FINANCIAL_DOCUMENT_PARSE_STATUS_PARSE_ERROR and not errors:
            errors.append("financial document parser did not recognize a supported MVP document type")
        document = {
            "document_id": document_id,
            "supplier_order_id": supplier_order_id,
            "document_type": normalized.get("document_type") or "",
            "original_filename": filename,
            "stored_file_path": stored_file_path,
            "file_content_type": content_type,
            "file_sha256": file_sha256,
            "uploaded_at": now,
            "updated_at": now,
            "parse_status": parse_status,
            "vendor": normalized.get("vendor") or "",
            "document_number": normalized.get("document_number") or normalized.get("invoice_number") or normalized.get("declaration_number") or "",
            "document_date": normalized.get("document_date") or normalized.get("invoice_date") or normalized.get("quote_date") or normalized.get("declaration_date") or "",
            "currency": normalized.get("currency") or "",
            "total_amount": _decimal_to_float(_parse_decimal(normalized.get("total_amount"))),
            "total_amount_rub": _decimal_to_float(_parse_decimal(normalized.get("total_amount_rub"))),
            "vat_rate": _decimal_to_float(_parse_decimal(normalized.get("vat_rate"))),
            "vat_amount_rub": _decimal_to_float(_parse_decimal(normalized.get("vat_amount_rub"))),
            "due_date": normalized.get("due_date") or "",
            "route": normalized.get("route") or "",
            "contract_ref": normalized.get("contract_ref") or normalized.get("contract") or "",
            "cbr_usd_rate_requested_date": rate_result.requested_date if rate_result else "",
            "cbr_usd_rate_effective_date": rate_result.effective_date if rate_result else "",
            "cbr_usd_rate_value": _decimal_to_float(rate_result.rate_value if rate_result else None),
            "rate_source": rate_result.source if rate_result else "",
            "rate_source_status": rate_result.status if rate_result else "",
            "raw_parse": raw_parse,
            "normalized_parse": normalized,
            "parser_version": parsed.get("parser_version") or FINANCIAL_DOCUMENT_PARSER_VERSION,
            "warnings": _dedupe_strings(warnings),
            "errors": _dedupe_strings(errors),
        }
        stored_lines = []
        for index, line in enumerate(expense_lines, start=1):
            stored_line = _expense_line_for_storage(
                line,
                supplier_order_id=supplier_order_id,
                document_id=document_id,
                sort_order=index,
            )
            stored_lines.append(stored_line)
        saved = self.runtime.save_supplier_financial_document(
            document=document,
            expense_lines=stored_lines,
        )
        return self.get_document(supplier_order_id, str(saved.get("document_id") or document_id))

    def update_document_status(self, supplier_order_id: str, document_id: str, parse_status: str) -> dict[str, Any]:
        self._ensure_supplier_order(supplier_order_id)
        normalized = str(parse_status or "").strip()
        if normalized not in FINANCIAL_DOCUMENT_PARSE_STATUSES:
            raise ValueError("unsupported financial document parse_status")
        document = self.runtime.update_supplier_financial_document_status(
            supplier_order_id=supplier_order_id,
            document_id=document_id,
            parse_status=normalized,
            updated_at=self.timestamp_factory(),
        )
        payload = self._with_download_path(document)
        payload["summary"] = build_financial_summary([payload], list(payload.get("expense_lines") or []))
        return payload

    def download_document_file(self, supplier_order_id: str, document_id: str) -> tuple[bytes, str, str]:
        self._ensure_supplier_order(supplier_order_id)
        document = self.runtime.load_supplier_financial_document(
            supplier_order_id=supplier_order_id,
            document_id=document_id,
        )
        if document is None:
            raise ValueError(f"financial document not found: {document_id}")
        file_path = self._resolve_runtime_file(str(document.get("stored_file_path") or ""))
        if not file_path.exists() or not file_path.is_file():
            raise ValueError(f"financial document file is missing: {document_id}")
        return (
            file_path.read_bytes(),
            str(document.get("original_filename") or "financial-document.pdf"),
            str(document.get("file_content_type") or FINANCIAL_DOCUMENT_CONTENT_TYPE),
        )

    def _rate_for_document(self, normalized: Mapping[str, Any]) -> UsdRateResult | None:
        document_type = str(normalized.get("document_type") or "")
        if document_type == FINANCIAL_DOCUMENT_TYPE_LOGISTICS_QUOTE:
            requested_date = str(normalized.get("quote_date") or normalized.get("document_date") or "")
        elif document_type == FINANCIAL_DOCUMENT_TYPE_LOGISTICS_INVOICE:
            requested_date = str(normalized.get("invoice_date") or normalized.get("document_date") or "")
        else:
            return None
        return self.usd_rate_provider.get_usd_rate(requested_date)

    def _ensure_supplier_order(self, supplier_order_id: str) -> None:
        if self.runtime.load_supplier_shipment(str(supplier_order_id or "").strip()) is None:
            raise ValueError(f"supplier shipment not found: {supplier_order_id}")

    def _write_document_file(self, *, supplier_order_id: str, document_id: str, filename: str, body: bytes) -> str:
        safe_filename = _safe_filename(filename)
        target_dir = self.runtime.runtime_dir / "supplier_financial_documents" / "files" / supplier_order_id / document_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / safe_filename
        target_path.write_bytes(body)
        return _relative_to_runtime(self.runtime.runtime_dir, target_path)

    def _resolve_runtime_file(self, relative_path: str) -> Path:
        normalized = str(relative_path or "").strip()
        if not normalized:
            raise ValueError("runtime file path is empty")
        root = self.runtime.runtime_dir.resolve()
        path = (root / normalized).resolve()
        if root != path and root not in path.parents:
            raise ValueError("runtime file path escapes runtime dir")
        return path

    def _with_download_path(self, document: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(document)
        supplier_order_id = str(payload.get("supplier_order_id") or "")
        document_id = str(payload.get("document_id") or "")
        payload["download_path"] = financial_document_download_path(supplier_order_id, document_id)
        return payload


def parse_financial_document_pdf(
    file_bytes: bytes,
    *,
    filename: str = "financial-document.pdf",
    text_extractor: TextExtractor | None = None,
) -> dict[str, Any]:
    extractor = text_extractor or extract_pdf_text_layer
    text, diagnostics, warnings = extractor(file_bytes, filename)
    parsed = parse_financial_document_text(
        text,
        filename=filename,
        extraction_diagnostics=diagnostics,
    )
    parsed["warnings"] = _dedupe_strings([*warnings, *_string_list(parsed.get("warnings"))])
    return parsed


def parse_financial_document_text(
    text: str,
    *,
    filename: str = "",
    extraction_diagnostics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_text = _normalize_text(text)
    diagnostics = dict(extraction_diagnostics or {})
    raw_parse = {
        "filename": filename,
        "text_char_count": len(normalized_text),
        "text_line_count": len(_text_to_lines(normalized_text)),
        "extraction": diagnostics,
    }
    warnings: list[str] = []
    errors: list[str] = []
    if len(normalized_text.strip()) < 40:
        errors.append("financial document parser found no readable text layer")
        return _parsed_payload(
            normalized={},
            expense_lines=[],
            raw_parse=raw_parse,
            warnings=warnings,
            errors=errors,
        )
    document_type = detect_financial_document_type(normalized_text, filename=filename)
    if document_type == FINANCIAL_DOCUMENT_TYPE_LOGISTICS_QUOTE:
        normalized, expense_lines, parser_warnings = _parse_logistics_quote(normalized_text)
    elif document_type == FINANCIAL_DOCUMENT_TYPE_LOGISTICS_INVOICE:
        normalized, expense_lines, parser_warnings = _parse_logistics_invoice(normalized_text)
    elif document_type == FINANCIAL_DOCUMENT_TYPE_CUSTOMS_DECLARATION:
        normalized, expense_lines, parser_warnings = _parse_customs_declaration(normalized_text)
    else:
        normalized, expense_lines, parser_warnings = {}, [], ["unsupported financial document type"]
    warnings.extend(parser_warnings)
    if document_type:
        normalized["document_type"] = document_type
    return _parsed_payload(
        normalized=normalized,
        expense_lines=expense_lines,
        raw_parse=raw_parse,
        warnings=warnings,
        errors=errors,
    )


def detect_financial_document_type(text: str, *, filename: str = "") -> str:
    haystack = f"{filename}\n{text}".casefold()
    if "коммерческое предложение" in haystack and "transitplus" in haystack:
        return FINANCIAL_DOCUMENT_TYPE_LOGISTICS_QUOTE
    if "счет на оплату" in haystack or "счёт на оплату" in haystack or "счет_покупателю" in haystack:
        return FINANCIAL_DOCUMENT_TYPE_LOGISTICS_INVOICE
    if "декларация на товары" in haystack or re.search(r"\b\d{8}/\d{6}/\d{6,}\b", text):
        return FINANCIAL_DOCUMENT_TYPE_CUSTOMS_DECLARATION
    return ""


def extract_pdf_text_layer(file_bytes: bytes, filename: str = "") -> tuple[str, dict[str, Any], list[str]]:
    warnings: list[str] = []
    diagnostics: dict[str, Any] = {"filename": filename}
    if shutil.which("pdftotext"):
        text = _extract_pdf_text_with_pdftotext(file_bytes)
        if _is_text_layer_sufficient(text):
            diagnostics["method"] = "pdftotext"
            diagnostics["text_char_count"] = len(text)
            return text, diagnostics, warnings
        diagnostics["pdftotext_text_nonempty"] = bool(text.strip())

    text = _extract_pdf_text_with_pypdf(file_bytes, diagnostics)
    if _is_text_layer_sufficient(text):
        diagnostics["method"] = "pypdf"
        diagnostics["text_char_count"] = len(text)
        return text, diagnostics, warnings
    if not diagnostics.get("pypdf_available"):
        warnings.append("pypdf text-layer parser is not installed")
    warnings.append("OCR fallback skipped: OCR tools are not configured for financial documents parser")
    diagnostics["method"] = diagnostics.get("method") or "no_readable_text_layer"
    diagnostics["text_char_count"] = len(text or "")
    return text or "", diagnostics, warnings


def build_financial_summary(documents: list[Mapping[str, Any]], expense_lines: list[Mapping[str, Any]]) -> dict[str, Any]:
    active_documents = [
        dict(document)
        for document in documents
        if str(document.get("parse_status") or "") != FINANCIAL_DOCUMENT_PARSE_STATUS_EXCLUDED
    ]
    active_lines = [
        dict(line)
        for line in expense_lines
        if any(str(document.get("document_id") or "") == str(line.get("financial_document_id") or "") for document in active_documents)
    ]
    warnings: list[str] = []
    quote_docs = [doc for doc in active_documents if doc.get("document_type") == FINANCIAL_DOCUMENT_TYPE_LOGISTICS_QUOTE]
    invoice_docs = [doc for doc in active_documents if doc.get("document_type") == FINANCIAL_DOCUMENT_TYPE_LOGISTICS_INVOICE]
    customs_docs = [doc for doc in active_documents if doc.get("document_type") == FINANCIAL_DOCUMENT_TYPE_CUSTOMS_DECLARATION]
    quote_doc = quote_docs[0] if quote_docs else {}
    quote_lines = [line for line in active_lines if _line_document_type(line, active_documents) == FINANCIAL_DOCUMENT_TYPE_LOGISTICS_QUOTE]
    invoice_lines = [line for line in active_lines if _line_document_type(line, active_documents) == FINANCIAL_DOCUMENT_TYPE_LOGISTICS_INVOICE]
    customs_lines = [line for line in active_lines if _line_document_type(line, active_documents) == FINANCIAL_DOCUMENT_TYPE_CUSTOMS_DECLARATION]

    quote_logistics_usd = _sum_decimal(
        line.get("amount")
        for line in quote_lines
        if line.get("currency") == "USD" and bool(line.get("included_in_logistics_efficiency"))
    )
    quote_customs_usd = _sum_decimal(
        line.get("amount")
        for line in quote_lines
        if line.get("currency") == "USD" and str(line.get("category") or "") == EXPENSE_CATEGORY_CUSTOMS_PAYMENTS
    )
    quote_total_usd = _parse_decimal(quote_doc.get("total_amount")) or _sum_decimal(
        line.get("amount") for line in quote_lines if line.get("currency") == "USD" and line.get("amount") is not None
    )
    quote_logistics_rub_cbr = _sum_decimal(line.get("amount_rub") for line in quote_lines if bool(line.get("included_in_logistics_efficiency")))
    invoice_fact_rub = _sum_decimal(line.get("amount_rub") for line in invoice_lines)
    invoice_vat_rub = _sum_decimal(line.get("vat_amount_rub") for line in invoice_lines)
    customs_fee_rub = _sum_decimal(line.get("amount_rub") for line in customs_lines if line.get("category") == EXPENSE_CATEGORY_CUSTOMS_FEE_1010)
    import_duty_rub = _sum_decimal(line.get("amount_rub") for line in customs_lines if line.get("category") == EXPENSE_CATEGORY_IMPORT_DUTY_2010)
    import_vat_rub = _sum_decimal(line.get("amount_rub") for line in customs_lines if line.get("category") == EXPENSE_CATEGORY_IMPORT_VAT_5010)
    customs_total_rub = _sum_decimal(line.get("amount_rub") for line in customs_lines if bool(line.get("included_in_customs_total")))

    quote_meta = dict(quote_doc.get("normalized_parse") or {})
    gross_weight = _parse_decimal(quote_meta.get("gross_weight_kg"))
    volume = _parse_decimal(quote_meta.get("volume_m3"))
    logistics_rub_per_kg = _safe_div(invoice_fact_rub, gross_weight)
    logistics_rub_per_m3 = _safe_div(invoice_fact_rub, volume)
    if quote_docs and gross_weight is None:
        warnings.append("Вес из КП не распознан: ₽/кг не рассчитан")
    if quote_docs and volume is None:
        warnings.append("Объем из КП не распознан: ₽/м³ не рассчитан")

    rate_summary = _build_rate_summary(
        quote_doc=quote_doc,
        invoice_docs=invoice_docs,
        invoice_fact_rub=invoice_fact_rub,
        linked_quote_usd_component=quote_logistics_usd,
    )
    warnings.extend(rate_summary.pop("warnings", []))
    if quote_docs and invoice_docs and quote_logistics_usd is not None:
        warnings.append(
            "Auto-match candidate uses logistics quote lines excluding customs payments; exact line-level evidence is reviewable"
        )
    if quote_docs and not invoice_docs:
        warnings.append("Счета логиста не загружены: implied rate и факт логистики не рассчитаны")
    if invoice_docs and not quote_docs:
        warnings.append("КП логиста не загружено: quote vs invoice delta не рассчитан")

    return {
        "quote": {
            "total_usd": _decimal_to_float(quote_total_usd),
            "logistics_usd": _decimal_to_float(quote_logistics_usd),
            "customs_payments_usd": _decimal_to_float(quote_customs_usd),
            "logistics_rub_cbr": _decimal_to_float(quote_logistics_rub_cbr),
        },
        "invoices": {
            "fact_rub": _decimal_to_float(invoice_fact_rub),
            "vat_rub": _decimal_to_float(invoice_vat_rub),
            "document_count": len(invoice_docs),
        },
        "customs_declaration": {
            "customs_fee_1010_rub": _decimal_to_float(customs_fee_rub),
            "import_duty_2010_rub": _decimal_to_float(import_duty_rub),
            "import_vat_5010_rub": _decimal_to_float(import_vat_rub),
            "total_customs_payments_rub": _decimal_to_float(customs_total_rub),
            "document_count": len(customs_docs),
        },
        "logistics_efficiency": {
            "rub_per_kg": _decimal_to_float(logistics_rub_per_kg),
            "rub_per_m3": _decimal_to_float(logistics_rub_per_m3),
            "gross_weight_kg": _decimal_to_float(gross_weight),
            "volume_m3": _decimal_to_float(volume),
        },
        "quote_invoice_match": rate_summary,
        "warnings": _dedupe_strings(warnings),
    }


def financial_documents_path(supplier_order_id: str) -> str:
    return f"/v1/sheet-vitrina-v1/supply/supplier-shipments/{supplier_order_id}/financial-documents"


def financial_document_path(supplier_order_id: str, document_id: str) -> str:
    return f"{financial_documents_path(supplier_order_id)}/{document_id}"


def financial_document_download_path(supplier_order_id: str, document_id: str) -> str:
    if not supplier_order_id or not document_id:
        return ""
    return f"{financial_document_path(supplier_order_id, document_id)}/file"


def _parse_logistics_quote(text: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    quote_date = _parse_date(_first_match(text, r"г\.\s*Москва\s+(\d{1,2}\.\d{1,2}\.\d{4})"))
    tariff = _first_match(text, r"тарифу\s+[«\"]([^»\"]+)[»\"]") or _first_match(text, r"Название тарифа:\s*[«\"]([^»\"]+)[»\"]")
    delivery_min, delivery_max = _parse_delivery_days(tariff or text)
    amounts = _extract_quote_amounts(text)
    total_quote_usd = _parse_decimal(_first_match(text, r"ИТОГО:\s*(?:\n|\s)*([\d .,]+)\s*USD"))
    if total_quote_usd is None or total_quote_usd == 0:
        total_quote_usd = amounts.get("total_quote_usd")
    if total_quote_usd is None:
        total_quote_usd = _parse_decimal(_first_match(text[:900], r"(?m)^\s*([\d .,]+)\s*USD\s*$"))
    normalized = {
        "vendor": "Transitplus International Ltd" if "Transitplus International Ltd" in text else "Transitplus",
        "quote_date": quote_date,
        "document_date": quote_date,
        "cargo_name": _normalize_cargo_name(_first_match(text, r"Наименование груза:\s*([^\n\r]+)")),
        "tariff": tariff,
        "origin": _normalize_route_place(_first_match(text, r"Город отправки:\s*([^\n\r]+)")),
        "destination": _normalize_route_place(_first_match(text, r"Пункт назначения:\s*([^\n\r]+)")),
        "delivery_days_min": delivery_min,
        "delivery_days_max": delivery_max,
        "gross_weight_kg": _decimal_to_float(_parse_decimal(_first_match(text, r"Вес брутто,\s*кг\.?\s*([\d .,]+)"))),
        "net_weight_kg": _decimal_to_float(_parse_decimal(_first_match(text, r"Вес нетто,\s*кг:?\s*([\d .,]+)"))),
        "volume_m3": _decimal_to_float(_parse_decimal(_first_match(text, r"Объем,\s*м3\s*([\d .,]+)"))),
        "estimated_cargo_value_usd": _decimal_to_float(_parse_decimal(_first_match(text, r"Оценочная стоимость груза,\s*долл\.\s*([\d .,]+)\s*USD"))),
        "estimated_cargo_value_cny": _decimal_to_float(_parse_decimal(_first_match(text, r"или\s*([\d .,]+)\s*юан"))),
        "total_amount": _decimal_to_float(total_quote_usd),
        "currency": "USD",
        "payment_rate_policy": "курс Банка ВТБ на дату выставления счёта" if "Банка ВТБ" in text else "",
        "validity_days": _int_or_none(_first_match(text, r"действительно в течение\s+(\d+)\s+календар")),
    }
    lines = [
        _expense_line(
            category=EXPENSE_CATEGORY_DELIVERY,
            stage="international_transport",
            description="Стоимость доставки по КП",
            amount=amounts.get(EXPENSE_CATEGORY_DELIVERY),
            currency="USD",
            included_in_logistics_efficiency=True,
        ),
        _expense_line(
            category=EXPENSE_CATEGORY_CUSTOMS_PAYMENTS,
            stage="customs_estimate",
            description="Таможенные платежи и сборы по КП",
            amount=amounts.get(EXPENSE_CATEGORY_CUSTOMS_PAYMENTS),
            currency="USD",
            included_in_customs_total=True,
        ),
        _expense_line(
            category=EXPENSE_CATEGORY_ECOLOGICAL_FEE,
            stage="customs_estimate",
            description="Экологический сбор по КП",
            amount=amounts.get(EXPENSE_CATEGORY_ECOLOGICAL_FEE),
            currency="USD",
            included_in_customs_total=True,
        ),
        _expense_line(
            category=EXPENSE_CATEGORY_BROKERAGE,
            stage="customs_services",
            description="Брокерские услуги по КП",
            amount=amounts.get(EXPENSE_CATEGORY_BROKERAGE),
            currency="USD",
            included_in_logistics_efficiency=True,
        ),
        _expense_line(
            category=EXPENSE_CATEGORY_COMPANY_COMMISSION,
            stage="logistics_services",
            description="Комиссия компании по КП",
            amount=amounts.get(EXPENSE_CATEGORY_COMPANY_COMMISSION),
            currency="USD",
            included_in_logistics_efficiency=True,
        ),
        _expense_line(
            category=EXPENSE_CATEGORY_INSURANCE,
            stage="insurance",
            description="Страхование по КП",
            amount=amounts.get(EXPENSE_CATEGORY_INSURANCE),
            currency="USD",
            included_in_logistics_efficiency=True,
        ),
        _expense_line(
            category=EXPENSE_CATEGORY_PERMISSION_DOCS,
            stage="documents",
            description="Оформление разрешительной документации",
            amount=amounts.get(EXPENSE_CATEGORY_PERMISSION_DOCS, Decimal("0")),
            currency="USD",
        ),
        _expense_line(
            category=EXPENSE_CATEGORY_PACKAGING,
            stage="packaging",
            description="Стоимость дополнительной упаковки",
            amount=amounts.get(EXPENSE_CATEGORY_PACKAGING),
            currency="USD",
            status=EXPENSE_LINE_STATUS_NEEDS_REVIEW if amounts.get(EXPENSE_CATEGORY_PACKAGING) is None else EXPENSE_LINE_STATUS_PARSED,
            confidence=0.5 if amounts.get(EXPENSE_CATEGORY_PACKAGING) is None else 0.9,
        ),
        _expense_line(
            category=EXPENSE_CATEGORY_EXPORT_DOCS,
            stage="documents",
            description="Стоимость оформления экспортных документов; возможно, не включено",
            amount=None,
            currency="USD",
            status=EXPENSE_LINE_STATUS_POSSIBLE_NOT_INCLUDED,
            confidence=0.7,
            raw={"possible_range_usd": amounts.get(EXPENSE_CATEGORY_EXPORT_DOCS)},
        ),
    ]
    if amounts.get(EXPENSE_CATEGORY_EXPORT_DOCS):
        normalized["export_docs_possible_range_usd"] = amounts.get(EXPENSE_CATEGORY_EXPORT_DOCS)
        warnings.append("Export documents cost is possible/not included and must be reviewed before adding to totals")
    missing = [key for key in (EXPENSE_CATEGORY_DELIVERY, EXPENSE_CATEGORY_CUSTOMS_PAYMENTS) if amounts.get(key) is None]
    if missing:
        warnings.append("Quote parser did not find required amount(s): " + ", ".join(missing))
    return normalized, lines, warnings


def _parse_logistics_invoice(text: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    number = _first_match(text, r"Сч[её]т на оплату №\s*([0-9]+)")
    invoice_date = _parse_date(_first_match(text, r"Сч[её]т на оплату №\s*[0-9]+\s*от\s*([^\n\r]+?)(?:\s*г\.|\n)"))
    vendor = "ООО ВОРЛД-ЛОГИСТИК" if "ВОРЛД-ЛОГИСТИК" in text else _first_match(text, r"Поставщик.*?\n([^\n\r]+)")
    contract = _extract_contract_ref(text)
    route = _extract_invoice_route(text)
    amount_rub = _parse_decimal(_first_match(text, r"Всего к оплате:\s*([\d .,]+)"))
    if amount_rub is None:
        amount_rub = _parse_decimal(_first_match(text, r"Итого:\s*([\d .,]+)"))
    vat_rate, vat_amount = _extract_invoice_vat(text)
    due_date = _parse_date(_first_match(text, r"Оплатить не позднее\s*([0-9.]+)"))
    category = EXPENSE_CATEGORY_DOMESTIC_TRANSPORT if "Москва" in route else EXPENSE_CATEGORY_BORDER_EXPEDITION
    cmr = _first_match(text, r"CMR\s*№\s*([A-Za-zА-Яа-я0-9-]+)")
    normalized = {
        "vendor": vendor,
        "document_number": number,
        "invoice_number": number,
        "document_date": invoice_date,
        "invoice_date": invoice_date,
        "contract": contract,
        "contract_ref": contract,
        "route": route,
        "CMR": cmr,
        "amount_rub": _decimal_to_float(amount_rub),
        "total_amount": _decimal_to_float(amount_rub),
        "total_amount_rub": _decimal_to_float(amount_rub),
        "currency": "RUB",
        "vat_rate": _decimal_to_float(vat_rate),
        "vat_amount_rub": _decimal_to_float(vat_amount),
        "due_date": due_date,
        "category_suggestion": category,
    }
    lines = [
        _expense_line(
            category=category,
            stage="logistics_stage",
            description=f"Счет логиста №{number or ''}: {route or 'логистический этап'}".strip(),
            amount=amount_rub,
            currency="RUB",
            amount_rub=amount_rub,
            vat_rate=vat_rate,
            vat_amount_rub=vat_amount,
            included_in_logistics_efficiency=True,
        )
    ]
    if not route:
        warnings.append("Invoice route was not recognized")
    if amount_rub is None:
        warnings.append("Invoice amount was not recognized")
    return normalized, lines, warnings


def _parse_customs_declaration(text: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    declaration_number = _first_match(text, r"\b(\d{8}/\d{6}/\d{6,})\b")
    declaration_date = _date_from_declaration_number(declaration_number) or _parse_date(_first_match(text, r"\b(\d{1,2}\.\d{1,2}\.\d{2,4})\s+\d{1,2}:\d{2}"))
    total_goods_count, total_places = _extract_customs_goods_places(text)
    invoice_currency, invoice_amount_cny, customs_rate = _extract_customs_invoice_currency(text)
    total_customs_value_rub = _parse_decimal(_first_match(text, r"\bCN\s+([\d .,]+)"))
    customs_fee = _parse_decimal(_first_match(text, r"\b1010-([\d .,]+)-643"))
    import_duty = _parse_decimal(_first_match(text, r"\b2010-([\d .,]+)-643"))
    import_vat = _parse_decimal(_first_match(text, r"\b5010-([\d .,]+)-643"))
    total_payments = _extract_customs_total_payments(text, customs_fee, import_duty, import_vat)
    release_status = "выпуск товаров разрешен" if "ВЫПУСК ТОВАРОВ РАЗРЕШЕН" in text.upper() else ""
    linked_references = _extract_customs_linked_references(text)
    normalized = {
        "vendor": "",
        "document_number": declaration_number,
        "declaration_number": declaration_number,
        "document_date": declaration_date,
        "declaration_date": declaration_date,
        "total_goods_count": total_goods_count,
        "total_places": total_places,
        "invoice_currency": invoice_currency,
        "invoice_amount_cny": _decimal_to_float(invoice_amount_cny),
        "customs_currency_rate_cny": _decimal_to_float(customs_rate),
        "total_customs_value_rub": _decimal_to_float(total_customs_value_rub),
        "customs_fee_1010_rub": _decimal_to_float(customs_fee),
        "import_duty_2010_rub": _decimal_to_float(import_duty),
        "import_vat_5010_rub": _decimal_to_float(import_vat),
        "total_customs_payments_rub": _decimal_to_float(total_payments),
        "release_status": release_status,
        "linked_references": linked_references,
        "currency": "RUB",
        "total_amount": _decimal_to_float(total_payments),
        "total_amount_rub": _decimal_to_float(total_payments),
    }
    lines = [
        _expense_line(
            category=EXPENSE_CATEGORY_CUSTOMS_FEE_1010,
            stage="customs_clearance",
            description="Таможенный сбор 1010 по ДТ",
            amount=customs_fee,
            currency="RUB",
            amount_rub=customs_fee,
            included_in_customs_total=True,
        ),
        _expense_line(
            category=EXPENSE_CATEGORY_IMPORT_DUTY_2010,
            stage="customs_clearance",
            description="Ввозная пошлина 2010 по ДТ",
            amount=import_duty,
            currency="RUB",
            amount_rub=import_duty,
            included_in_customs_total=True,
        ),
        _expense_line(
            category=EXPENSE_CATEGORY_IMPORT_VAT_5010,
            stage="customs_clearance",
            description="Импортный НДС 5010 по ДТ",
            amount=import_vat,
            currency="RUB",
            amount_rub=import_vat,
            vat_rate=Decimal("22"),
            vat_amount_rub=import_vat,
            included_in_customs_total=True,
        ),
    ]
    if not declaration_number:
        warnings.append("Customs declaration number was not recognized")
    if total_payments is None:
        warnings.append("Total customs payments were not recognized")
    return normalized, lines, warnings


def _extract_quote_amounts(text: str) -> dict[str, Decimal | str | None]:
    values: dict[str, Decimal | str | None] = {}
    label_patterns = {
        EXPENSE_CATEGORY_DELIVERY: r"Стоимость доставки[^\n\r]*?([\d .,]+)\s*USD",
        EXPENSE_CATEGORY_CUSTOMS_PAYMENTS: r"Таможенные платежи и сборы[^\n\r]*?([\d .,]+)\s*USD",
        EXPENSE_CATEGORY_ECOLOGICAL_FEE: r"Экологический сбор[^\n\r]*?([\d .,]+)\s*USD",
        EXPENSE_CATEGORY_BROKERAGE: r"Брокерские услуги[^\n\r]*?([\d .,]+)\s*USD",
        EXPENSE_CATEGORY_COMPANY_COMMISSION: r"Комиссия компании[^\n\r]*?([\d .,]+)\s*USD",
        EXPENSE_CATEGORY_INSURANCE: r"Страх[^\n\r]*?([\d .,]+)\s*USD",
        EXPENSE_CATEGORY_PERMISSION_DOCS: r"Оформление разрешительной документации[^\n\r]*?([\d .,]+)\s*USD",
    }
    for key, pattern in label_patterns.items():
        match = _first_match(text, pattern, flags=re.I)
        if match:
            values[key] = _parse_decimal(match)
    top = text[: max(text.find("Коммерческое предложение"), 900)]
    numbered = {}
    for match in re.finditer(r"(?m)^[ \t]*([1-7])[ \t]+([\d .,]+)[ \t]*$", top):
        numbered[int(match.group(1))] = _parse_decimal(match.group(2))
    mapping = {
        1: EXPENSE_CATEGORY_DELIVERY,
        2: EXPENSE_CATEGORY_CUSTOMS_PAYMENTS,
        3: EXPENSE_CATEGORY_ECOLOGICAL_FEE,
        4: EXPENSE_CATEGORY_BROKERAGE,
        5: EXPENSE_CATEGORY_COMPANY_COMMISSION,
        6: EXPENSE_CATEGORY_INSURANCE,
    }
    for number, key in mapping.items():
        if values.get(key) is None and numbered.get(number) is not None:
            values[key] = numbered[number]
    values[EXPENSE_CATEGORY_PERMISSION_DOCS] = values.get(EXPENSE_CATEGORY_PERMISSION_DOCS)
    if values[EXPENSE_CATEGORY_PERMISSION_DOCS] is None:
        values[EXPENSE_CATEGORY_PERMISSION_DOCS] = _parse_decimal(
            _first_match(text, r"Оформление разрешительной документации\s+([\d .,]+)")
        )
    values[EXPENSE_CATEGORY_PACKAGING] = _parse_decimal(_first_match(text, r"Стоимость дополнительной упаковки\s+([\d .,]+)\s*USD"))
    range_match = re.search(r"Стоимость оформления экспортных документов\s+([\d .,]+)\s*[-–]\s*([\d .,]+)\s*USD", text, flags=re.I)
    if range_match:
        values[EXPENSE_CATEGORY_EXPORT_DOCS] = f"{_decimal_to_display(_parse_decimal(range_match.group(1)))}-{_decimal_to_display(_parse_decimal(range_match.group(2)))}"
    total = _parse_decimal(_first_match(top, r"(?m)^\s*([\d .,]+)\s*USD\s*$"))
    if total is not None:
        values["total_quote_usd"] = total
    return values


def _extract_invoice_route(text: str) -> str:
    match = re.search(r"маршруту\s+г\.\s*([^\n\r-]+?)\s*-\s*(?:\n\s*)?г\.\s*([^\n\r,]+)", text, flags=re.I)
    if not match:
        return ""
    return f"{_clean_value(match.group(1))} -> {_clean_value(match.group(2))}"


def _extract_contract_ref(text: str) -> str:
    match = re.search(r"(ДОГОВОР\s+ТРАНСПОРТНОЙ\s+ЭКСПЕДИЦИИ\s+№\s*[A-Za-zА-Яа-я0-9-]+\s+от\s+\d{1,2}\.\d{1,2}\.\d{4})", text, flags=re.I)
    if not match:
        return ""
    value = _clean_value(match.group(1))
    value = re.sub(r"^ДОГОВОР", "договор", value, flags=re.I)
    value = re.sub(r"ТРАНСПОРТНОЙ ЭКСПЕДИЦИИ", "транспортной экспедиции", value, flags=re.I)
    return value


def _extract_invoice_vat(text: str) -> tuple[Decimal | None, Decimal | None]:
    match = re.search(r"В том числе НДС\s*([\d .,]+)%:\s*([\d .,]+)", text, flags=re.I)
    if match:
        return _parse_decimal(match.group(1)), _parse_decimal(match.group(2))
    zero = re.search(r"НДС\s*0%\s*[-—]?", text, flags=re.I)
    if zero:
        return Decimal("0"), Decimal("0")
    return None, None


def _extract_customs_goods_places(text: str) -> tuple[int | None, int | None]:
    match = re.search(r"\n\s*(\d{1,3})\s+(\d{1,5})\s*\n\s*СМ\.\s*ГРАФУ\s+14\s+ДТ", text, flags=re.I)
    if match:
        return _int_or_none(match.group(1)), _int_or_none(match.group(2))
    match = re.search(r"Всего т-ов\s+6 Всего мест.*?\n\s*(\d{1,3})\s+(\d{1,5})", text, flags=re.I | re.S)
    if match:
        return _int_or_none(match.group(1)), _int_or_none(match.group(2))
    return None, None


def _extract_customs_invoice_currency(text: str) -> tuple[str, Decimal | None, Decimal | None]:
    match = re.search(r"\b(CNY)\s+([\d .,]+)\s+([\d .,]+)\s+010\b", text)
    if not match:
        return "", None, None
    return match.group(1), _parse_decimal(match.group(2)), _parse_decimal(match.group(3))


def _extract_customs_total_payments(
    text: str,
    customs_fee: Decimal | None,
    import_duty: Decimal | None,
    import_vat: Decimal | None,
) -> Decimal | None:
    match = re.search(r"5010-[\d .,]+-643-[0-9]+\s*\n\s*([\d .,]+)", text)
    if match:
        value = _parse_decimal(match.group(1))
        if value is not None:
            return value
    values = [item for item in (customs_fee, import_duty, import_vat) if item is not None]
    return sum(values, Decimal("0")) if values else None


def _extract_customs_linked_references(text: str) -> dict[str, Any]:
    refs: dict[str, Any] = {}
    invoice = re.search(r"04031/0\s+([0-9]+)\s+от\s+(\d{1,2}\.\d{1,2}\.\d{4})", text, flags=re.I)
    if invoice:
        refs["invoice_account"] = {"number": invoice.group(1), "date": _parse_date(invoice.group(2))}
    contract = re.search(r"04033/0\s+([A-Za-zА-Яа-я0-9-]+)\s+от\s+(\d{1,2}\.\d{1,2}\.\d{4})", text, flags=re.I)
    if contract:
        refs["contract"] = {"number": contract.group(1), "date": _parse_date(contract.group(2))}
    cmr = re.search(r"\b(457-ORE-002)\s+ОТ\s+(\d{1,2}\.\d{1,2}\.\d{4})", text, flags=re.I)
    if cmr:
        refs["CMR"] = {"number": cmr.group(1), "date": _parse_date(cmr.group(2))}
    return refs


def _build_rate_summary(
    *,
    quote_doc: Mapping[str, Any],
    invoice_docs: list[Mapping[str, Any]],
    invoice_fact_rub: Decimal | None,
    linked_quote_usd_component: Decimal | None,
) -> dict[str, Any]:
    warnings: list[str] = []
    if not quote_doc or not invoice_docs or not invoice_fact_rub or not linked_quote_usd_component:
        return {
            "status": "not_available",
            "warnings": warnings,
        }
    if linked_quote_usd_component == 0:
        return {"status": "not_available", "warnings": ["Linked quote USD component is zero"]}
    invoice_doc = _invoice_doc_for_rate(invoice_docs)
    cbr_invoice_rate = _parse_decimal(invoice_doc.get("cbr_usd_rate_value"))
    quote_cbr_rate = _parse_decimal(quote_doc.get("cbr_usd_rate_value"))
    if cbr_invoice_rate is None or quote_cbr_rate is None:
        return {"status": "rate_pending", "warnings": ["CBR rate is missing for quote or invoice date"]}
    if len({doc.get("document_date") for doc in invoice_docs if doc.get("document_date")}) > 1:
        warnings.append("Multiple invoice dates loaded; rate comparison uses the largest invoice/latest invoice date as MVP benchmark")
    implied_rate = _safe_div(invoice_fact_rub, linked_quote_usd_component)
    spread_pct = _safe_div(implied_rate, cbr_invoice_rate)
    estimated_bank_rate = quote_cbr_rate * spread_pct if spread_pct is not None else None
    quote_rub_equivalent = linked_quote_usd_component * estimated_bank_rate if estimated_bank_rate is not None else None
    delta_rub = invoice_fact_rub - quote_rub_equivalent if quote_rub_equivalent is not None else None
    absolute_spread = implied_rate - cbr_invoice_rate if implied_rate is not None else None
    relative_spread_pct = _safe_div(implied_rate, cbr_invoice_rate)
    if relative_spread_pct is not None:
        relative_spread_pct -= Decimal("1")
    return {
        "status": "needs_review",
        "linked_quote_usd_component": _decimal_to_float(linked_quote_usd_component),
        "linked_quote_line_categories": [
            EXPENSE_CATEGORY_DELIVERY,
            EXPENSE_CATEGORY_BROKERAGE,
            EXPENSE_CATEGORY_COMPANY_COMMISSION,
            EXPENSE_CATEGORY_INSURANCE,
        ],
        "invoice_amount_rub": _decimal_to_float(invoice_fact_rub),
        "invoice_rate_requested_date": invoice_doc.get("cbr_usd_rate_requested_date") or invoice_doc.get("document_date") or "",
        "invoice_rate_effective_date": invoice_doc.get("cbr_usd_rate_effective_date") or "",
        "cbr_usd_rate_on_invoice_date": _decimal_to_float(cbr_invoice_rate),
        "implied_rate": _decimal_to_float(implied_rate),
        "absolute_spread": _decimal_to_float(absolute_spread),
        "relative_spread_pct": _decimal_to_float(relative_spread_pct),
        "spread_pct": _decimal_to_float(spread_pct),
        "quote_rate_requested_date": quote_doc.get("cbr_usd_rate_requested_date") or quote_doc.get("document_date") or "",
        "quote_rate_effective_date": quote_doc.get("cbr_usd_rate_effective_date") or "",
        "cbr_usd_rate_on_quote_date": _decimal_to_float(quote_cbr_rate),
        "estimated_bank_rate_on_quote_date": _decimal_to_float(estimated_bank_rate),
        "quote_rub_equivalent": _decimal_to_float(quote_rub_equivalent),
        "delta_rub": _decimal_to_float(delta_rub),
        "bank_rate_label": "расчётный курс по правилу КП",
        "warnings": warnings,
    }


def _invoice_doc_for_rate(invoice_docs: list[Mapping[str, Any]]) -> Mapping[str, Any]:
    return sorted(
        invoice_docs,
        key=lambda item: (
            _parse_decimal(item.get("total_amount_rub")) or Decimal("0"),
            str(item.get("document_date") or ""),
        ),
        reverse=True,
    )[0]


def _line_document_type(line: Mapping[str, Any], documents: list[Mapping[str, Any]]) -> str:
    document_id = str(line.get("financial_document_id") or "")
    for document in documents:
        if str(document.get("document_id") or "") == document_id:
            return str(document.get("document_type") or "")
    return ""


def _apply_usd_rate_to_parse(
    normalized: dict[str, Any],
    expense_lines: list[dict[str, Any]],
    rate_value: Decimal | None,
) -> None:
    if rate_value is None:
        return
    if normalized.get("document_type") == FINANCIAL_DOCUMENT_TYPE_LOGISTICS_QUOTE:
        total = _parse_decimal(normalized.get("total_amount"))
        if total is not None:
            normalized["total_amount_rub"] = _decimal_to_float(total * rate_value)
        for line in expense_lines:
            if line.get("currency") != "USD":
                continue
            amount = _parse_decimal(line.get("amount"))
            if amount is not None:
                line["amount_rub"] = _decimal_to_float(amount * rate_value)


def _parse_status_for_payload(
    parsed: Mapping[str, Any],
    warnings: list[str],
    errors: list[str],
    rate_result: UsdRateResult | None,
) -> str:
    if errors:
        return FINANCIAL_DOCUMENT_PARSE_STATUS_PARSE_ERROR
    normalized = parsed.get("normalized_parse") if isinstance(parsed.get("normalized_parse"), Mapping) else {}
    if not normalized or not normalized.get("document_type"):
        return FINANCIAL_DOCUMENT_PARSE_STATUS_PARSE_ERROR
    if rate_result is not None and rate_result.status != FX_RATE_STATUS_OK:
        return FINANCIAL_DOCUMENT_PARSE_STATUS_NEEDS_REVIEW
    required_by_type = {
        FINANCIAL_DOCUMENT_TYPE_LOGISTICS_QUOTE: ["quote_date", "total_amount"],
        FINANCIAL_DOCUMENT_TYPE_LOGISTICS_INVOICE: ["invoice_number", "invoice_date", "amount_rub"],
        FINANCIAL_DOCUMENT_TYPE_CUSTOMS_DECLARATION: ["declaration_number", "total_customs_payments_rub"],
    }
    missing = [key for key in required_by_type.get(str(normalized.get("document_type") or ""), []) if not normalized.get(key)]
    if missing:
        warnings.append("Parser needs review: missing " + ", ".join(missing))
        return FINANCIAL_DOCUMENT_PARSE_STATUS_NEEDS_REVIEW
    return FINANCIAL_DOCUMENT_PARSE_STATUS_PARSED


def _expense_line_for_storage(
    line: Mapping[str, Any],
    *,
    supplier_order_id: str,
    document_id: str,
    sort_order: int,
) -> dict[str, Any]:
    return {
        "line_id": str(line.get("line_id") or "fline_" + uuid4().hex),
        "financial_document_id": document_id,
        "supplier_order_id": supplier_order_id,
        "sort_order": sort_order,
        "category": line.get("category") or "",
        "stage": line.get("stage") or "",
        "description": line.get("description") or "",
        "amount": _decimal_to_float(_parse_decimal(line.get("amount"))),
        "currency": line.get("currency") or "",
        "amount_rub": _decimal_to_float(_parse_decimal(line.get("amount_rub"))),
        "vat_rate": _decimal_to_float(_parse_decimal(line.get("vat_rate"))),
        "vat_amount_rub": _decimal_to_float(_parse_decimal(line.get("vat_amount_rub"))),
        "included_in_logistics_efficiency": bool(line.get("included_in_logistics_efficiency")),
        "included_in_customs_total": bool(line.get("included_in_customs_total")),
        "status": line.get("status") or EXPENSE_LINE_STATUS_PARSED,
        "confidence": _decimal_to_float(_parse_decimal(line.get("confidence"))),
        "raw": dict(line.get("raw") or {}),
    }


def _expense_line(
    *,
    category: str,
    stage: str,
    description: str,
    amount: Any,
    currency: str,
    amount_rub: Any = None,
    vat_rate: Any = None,
    vat_amount_rub: Any = None,
    included_in_logistics_efficiency: bool = False,
    included_in_customs_total: bool = False,
    status: str = EXPENSE_LINE_STATUS_PARSED,
    confidence: float = 0.9,
    raw: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    parsed_amount = _parse_decimal(amount)
    parsed_amount_rub = _parse_decimal(amount_rub)
    return {
        "category": category,
        "stage": stage,
        "description": description,
        "amount": _decimal_to_float(parsed_amount),
        "currency": currency,
        "amount_rub": _decimal_to_float(parsed_amount_rub),
        "vat_rate": _decimal_to_float(_parse_decimal(vat_rate)),
        "vat_amount_rub": _decimal_to_float(_parse_decimal(vat_amount_rub)),
        "included_in_logistics_efficiency": included_in_logistics_efficiency,
        "included_in_customs_total": included_in_customs_total,
        "status": status,
        "confidence": confidence,
        "raw": dict(raw or {}),
    }


def _parsed_payload(
    *,
    normalized: Mapping[str, Any],
    expense_lines: list[Mapping[str, Any]],
    raw_parse: Mapping[str, Any],
    warnings: list[str],
    errors: list[str],
) -> dict[str, Any]:
    return {
        "parser_version": FINANCIAL_DOCUMENT_PARSER_VERSION,
        "normalized_parse": dict(normalized),
        "expense_lines": [dict(item) for item in expense_lines],
        "raw_parse": dict(raw_parse),
        "warnings": _dedupe_strings(warnings),
        "errors": _dedupe_strings(errors),
    }


def _extract_pdf_text_with_pdftotext(file_bytes: bytes) -> str:
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
            handle.write(file_bytes)
            temp_path = handle.name
        result = subprocess.run(
            ["pdftotext", "-layout", temp_path, "-"],
            capture_output=True,
            timeout=20,
            check=False,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return ""
    finally:
        if temp_path:
            try:
                Path(temp_path).unlink()
            except OSError:
                pass


def _extract_pdf_text_with_pypdf(file_bytes: bytes, diagnostics: dict[str, Any]) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception as exc:
        diagnostics["pypdf_available"] = False
        diagnostics["pypdf_error"] = str(exc)
        return ""
    diagnostics["pypdf_available"] = True
    try:
        reader = PdfReader(BytesIO(file_bytes))
        pages: list[str] = []
        for index, page in enumerate(reader.pages):
            try:
                pages.append(page.extract_text() or "")
            except Exception as exc:
                diagnostics[f"pypdf_page_{index + 1}_error"] = str(exc)
        diagnostics["pypdf_page_count"] = len(reader.pages)
        return "\n".join(pages)
    except Exception as exc:
        diagnostics["pypdf_error"] = str(exc)
        return ""


def _is_text_layer_sufficient(text: str) -> bool:
    normalized = _normalize_text(text)
    if len(normalized) < 80:
        return False
    letters = sum(1 for char in normalized if char.isalpha())
    return letters >= 20


def _rate_result_to_dict(rate: UsdRateResult) -> dict[str, Any]:
    return {
        "requested_date": rate.requested_date,
        "effective_date": rate.effective_date,
        "rate_value": _decimal_to_float(rate.rate_value),
        "source": rate.source,
        "status": rate.status,
        "error": rate.error,
    }


def _parse_date(value: Any) -> str:
    raw = _clean_value(value)
    if not raw:
        return ""
    match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", raw)
    if match:
        day, month, year = match.groups()
        year = ("20" + year) if len(year) == 2 else year
        return _safe_iso_date(int(year), int(month), int(day))
    match = re.search(r"(\d{1,2})\s+([А-Яа-яA-Za-z]+)\s+(\d{4})", raw)
    if match:
        day, month_name, year = match.groups()
        month = _RU_MONTHS.get(month_name.casefold())
        if month:
            return _safe_iso_date(int(year), month, int(day))
    match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if match:
        year, month, day = match.groups()
        return _safe_iso_date(int(year), int(month), int(day))
    return ""


def _optional_iso_date(value: Any) -> str:
    raw = _clean_value(value)
    if not raw:
        return ""
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        return _parse_date(raw)


def _safe_iso_date(year: int, month: int, day: int) -> str:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def _date_from_declaration_number(value: Any) -> str:
    raw = str(value or "")
    match = re.search(r"/(\d{2})(\d{2})(\d{2})/", raw)
    if not match:
        return ""
    day, month, year = match.groups()
    return _safe_iso_date(2000 + int(year), int(month), int(day))


def _parse_delivery_days(value: str) -> tuple[int | None, int | None]:
    match = re.search(r"(\d{1,3})\s*[-–]\s*(\d{1,3})\s*(?:дн|day|календар)", value, flags=re.I)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | float):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    raw = str(value).strip()
    if not raw:
        return None
    raw = raw.replace("\u00a0", " ").replace("\u202f", " ")
    raw = re.sub(r"[^\d,.\-]", "", raw.replace(" ", ""))
    if not raw or raw in {"-", ".", ","}:
        return None
    if "," in raw and "." not in raw:
        raw = raw.replace(",", ".")
    elif "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _decimal_to_float(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP))


def _decimal_to_display(value: Decimal | None) -> str:
    if value is None:
        return ""
    normalized = value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
    return str(int(normalized)) if normalized == normalized.to_integral() else str(normalized)


def _quantize_rate(value: Decimal) -> Decimal:
    return value.quantize(RATE_QUANT, rounding=ROUND_HALF_UP)


def _safe_div(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _sum_decimal(values: Any) -> Decimal | None:
    total = Decimal("0")
    found = False
    for value in values:
        parsed = _parse_decimal(value)
        if parsed is None:
            continue
        total += parsed
        found = True
    return total if found else None


def _first_match(text: str, pattern: str, *, flags: int = re.I) -> str:
    match = re.search(pattern, text, flags=flags)
    return _clean_value(match.group(1)) if match else ""


def _normalize_text(text: str) -> str:
    normalized = str(text or "").replace("\u00a0", " ")
    normalized = normalized.replace("\u202f", " ")
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _text_to_lines(text: str) -> list[str]:
    return [_clean_value(line) for line in str(text or "").splitlines() if _clean_value(line)]


def _clean_value(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\u00a0", " ").replace("\u202f", " ")).strip()


def _normalize_cargo_name(value: str) -> str:
    return _clean_value(value).casefold()


def _normalize_route_place(value: str) -> str:
    cleaned = _clean_value(value)
    if "Гуанчжоу" in cleaned and "Guangzhou" in cleaned:
        return "Guangzhou / Гуанчжоу"
    return cleaned


def _int_or_none(value: Any) -> int | None:
    try:
        raw = re.sub(r"\D+", "", str(value or ""))
        return int(raw) if raw else None
    except ValueError:
        return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _safe_filename(value: str) -> str:
    raw = Path(str(value or "financial-document.pdf")).name
    raw = raw.replace("/", "_").replace("\\", "_").strip()
    return raw or "financial-document.pdf"


def _relative_to_runtime(runtime_dir: Path, target_path: Path) -> str:
    try:
        return str(target_path.resolve().relative_to(runtime_dir.resolve()))
    except ValueError:
        return str(target_path)


def _default_timestamp_factory() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


_RU_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}
