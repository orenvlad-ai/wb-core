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
    FINANCIAL_DOCUMENT_PARSE_STATUS_CONFIRMED,
    FINANCIAL_DOCUMENT_PARSE_STATUS_EXCLUDED,
    FINANCIAL_DOCUMENT_PARSE_STATUS_NEEDS_REVIEW,
    FINANCIAL_DOCUMENT_PARSE_STATUS_PARSED,
    FINANCIAL_DOCUMENT_PARSE_STATUS_PARSE_ERROR,
    FINANCIAL_DOCUMENT_PARSE_STATUSES,
    FINANCIAL_DOCUMENT_PARSER_VERSION,
    FINANCIAL_DOCUMENT_TYPE_BANK_CONTROL_STATEMENT,
    FINANCIAL_DOCUMENT_TYPE_BANK_TRANSFER_APPLICATION,
    FINANCIAL_DOCUMENT_TYPE_CUSTOMS_DECLARATION,
    FINANCIAL_DOCUMENT_TYPE_LOGISTICS_INVOICE,
    FINANCIAL_DOCUMENT_TYPE_LOGISTICS_QUOTE,
    FX_RATE_SOURCE_CBR,
    FX_RATE_STATUS_MISSING,
    FX_RATE_STATUS_OK,
    FX_RATE_STATUS_PENDING,
)
from packages.contracts.supplier_shipments import TRADE_DOCUMENT_STATUS_ACTIVE

MONEY_QUANT = Decimal("0.01")
RATE_QUANT = Decimal("0.0001")
QUOTE_REQUIRED_AMOUNT_CATEGORIES = (EXPENSE_CATEGORY_DELIVERY, EXPENSE_CATEGORY_CUSTOMS_PAYMENTS)
QUOTE_LOGISTICS_COMPONENT_CATEGORIES = (
    EXPENSE_CATEGORY_DELIVERY,
    EXPENSE_CATEGORY_BROKERAGE,
    EXPENSE_CATEGORY_COMPANY_COMMISSION,
    EXPENSE_CATEGORY_INSURANCE,
)
QUOTE_CORE_AMOUNT_CATEGORIES = (
    EXPENSE_CATEGORY_DELIVERY,
    EXPENSE_CATEGORY_CUSTOMS_PAYMENTS,
    EXPENSE_CATEGORY_ECOLOGICAL_FEE,
    EXPENSE_CATEGORY_BROKERAGE,
    EXPENSE_CATEGORY_COMPANY_COMMISSION,
    EXPENSE_CATEGORY_INSURANCE,
)
QUOTE_AMOUNT_CATEGORY_BY_ROW = {
    1: EXPENSE_CATEGORY_DELIVERY,
    2: EXPENSE_CATEGORY_CUSTOMS_PAYMENTS,
    3: EXPENSE_CATEGORY_ECOLOGICAL_FEE,
    4: EXPENSE_CATEGORY_BROKERAGE,
    5: EXPENSE_CATEGORY_COMPANY_COMMISSION,
    6: EXPENSE_CATEGORY_INSURANCE,
}
QUOTE_AMOUNT_LABELS = (
    (EXPENSE_CATEGORY_DELIVERY, ("Стоимость доставки",)),
    (EXPENSE_CATEGORY_CUSTOMS_PAYMENTS, ("Таможенные платежи и сборы",)),
    (EXPENSE_CATEGORY_ECOLOGICAL_FEE, ("Экологический сбор",)),
    (EXPENSE_CATEGORY_BROKERAGE, ("Брокерские услуги",)),
    (EXPENSE_CATEGORY_COMPANY_COMMISSION, ("Комиссия компании",)),
    (EXPENSE_CATEGORY_INSURANCE, ("Страховая ставка", "Страхование")),
    (EXPENSE_CATEGORY_PERMISSION_DOCS, ("Оформление разрешительной документации",)),
    (EXPENSE_CATEGORY_PACKAGING, ("Стоимость дополнительной упаковки",)),
)
PCT_QUANT = Decimal("0.0001")
QUOTE_UNIT_ESTIMATOR_MISSING_WARNING = "Нет коэффициента шт/кг для оценки КП"
ORDER_MATCH_STATUS_MATCHED = "matched"
ORDER_MATCH_STATUS_PROBABLE_MATCH = "probable_match"
ORDER_MATCH_STATUS_NEEDS_REVIEW = "needs_review"
ORDER_MATCH_STATUS_MISMATCH = "mismatch"
ORDER_MATCH_DOCUMENT_TYPES = {
    FINANCIAL_DOCUMENT_TYPE_BANK_CONTROL_STATEMENT,
    FINANCIAL_DOCUMENT_TYPE_BANK_TRANSFER_APPLICATION,
}


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
        shipment = _supplier_order_shipment_with_linked_contract(self.runtime, self.runtime.load_supplier_shipment(supplier_order_id) or {})
        documents = [
            apply_supplier_order_document_match(self._with_download_path(item), shipment)
            for item in self._refresh_saved_document_parses(
                self.runtime.list_supplier_financial_documents(supplier_order_id)
            )
        ]
        lines = self.runtime.list_supplier_financial_expense_lines(supplier_order_id)
        return {
            "contract_name": "sheet_vitrina_v1_supplier_financial_documents",
            "status": "ok",
            "supplier_order_id": supplier_order_id,
            "documents": documents,
            "expense_lines": lines,
            "summary": build_financial_summary(documents, lines, shipment=shipment),
        }

    def list_shipment_registry(self) -> dict[str, Any]:
        rows = self.runtime.list_supplier_shipments()
        contexts: list[dict[str, Any]] = []
        for row in rows:
            shipment_id = str(row.get("shipment_id") or "").strip()
            if not shipment_id:
                continue
            contexts.append(self._shipment_registry_context(shipment_id, fallback_header=row))
        return build_supplier_shipment_registry(contexts)

    def compare_registry_quote(
        self,
        shipment_id: str,
        *,
        file_bytes: bytes,
        uploaded_filename: str | None = None,
        uploaded_content_type: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_supplier_order(shipment_id)
        if not file_bytes:
            raise ValueError("quote comparison upload file is empty")
        filename = _safe_filename(uploaded_filename or "logistics-quote.pdf")
        if Path(filename).suffix.lower() not in FINANCIAL_DOCUMENT_ALLOWED_EXTENSIONS:
            raise ValueError("quote comparison upload must be a PDF file")
        content_type = str(uploaded_content_type or "").split(";", 1)[0].strip().lower() or FINANCIAL_DOCUMENT_CONTENT_TYPE
        parsed = parse_financial_document_pdf(
            file_bytes,
            filename=filename,
            text_extractor=self.pdf_text_extractor,
        )
        normalized = dict(parsed.get("normalized_parse") or {})
        warnings = _string_list(parsed.get("warnings"))
        errors = _string_list(parsed.get("errors"))
        if str(normalized.get("document_type") or "") != FINANCIAL_DOCUMENT_TYPE_LOGISTICS_QUOTE:
            if not errors:
                errors.append("uploaded file was not recognized as a logistics quote")
            raise ValueError("comparison PDF must be a logistics quote")
        expense_lines = [dict(item) for item in parsed.get("expense_lines") or []]
        rate_result = self._rate_for_document(normalized)
        if rate_result is not None:
            normalized["cbr_usd_rate"] = _rate_result_to_dict(rate_result)
            if rate_result.status != FX_RATE_STATUS_OK:
                warnings.append(f"CBR USD rate is pending or missing for {rate_result.requested_date}")
            else:
                _apply_usd_rate_to_parse(normalized, expense_lines, rate_result.rate_value)
        parse_status = _parse_status_for_payload(parsed, warnings, errors, rate_result)
        if parse_status == FINANCIAL_DOCUMENT_PARSE_STATUS_PARSE_ERROR:
            raise ValueError("; ".join(errors) or "comparison PDF parser returned parse_error")
        now = self.timestamp_factory()
        document_id = "temp_quote_comparison"
        document = {
            "document_id": document_id,
            "supplier_order_id": str(shipment_id or "").strip(),
            "document_type": normalized.get("document_type") or "",
            "original_filename": filename,
            "stored_file_path": "",
            "file_content_type": content_type,
            "file_sha256": hashlib.sha256(file_bytes).hexdigest(),
            "uploaded_at": now,
            "updated_at": now,
            "parse_status": parse_status,
            "vendor": normalized.get("vendor") or "",
            "document_number": normalized.get("document_number") or "",
            "document_date": normalized.get("document_date") or normalized.get("quote_date") or "",
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
            "raw_parse": dict(parsed.get("raw_parse") or {}),
            "normalized_parse": normalized,
            "parser_version": parsed.get("parser_version") or FINANCIAL_DOCUMENT_PARSER_VERSION,
            "warnings": _dedupe_strings(warnings),
            "errors": _dedupe_strings(errors),
        }
        stored_lines = [
            _expense_line_for_storage(
                line,
                supplier_order_id=str(shipment_id or "").strip(),
                document_id=document_id,
                sort_order=index,
            )
            for index, line in enumerate(expense_lines, start=1)
        ]
        shipment_context = self._shipment_registry_context(shipment_id)
        quote_context = {
            "shipment_id": "temporary_quote",
            "header": {},
            "lines": [],
            "documents": [document],
            "expense_lines": stored_lines,
            "summary": build_financial_summary([document], stored_lines),
        }
        return build_supplier_shipment_registry_quote_comparison(
            quote_context=quote_context,
            shipment_context=shipment_context,
            uploaded_filename=filename,
        )

    def get_document(self, supplier_order_id: str, document_id: str) -> dict[str, Any]:
        self._ensure_supplier_order(supplier_order_id)
        document = self.runtime.load_supplier_financial_document(
            supplier_order_id=supplier_order_id,
            document_id=document_id,
        )
        if document is None:
            raise ValueError(f"financial document not found: {document_id}")
        document = self._refresh_saved_document_parse(document)
        shipment = _supplier_order_shipment_with_linked_contract(self.runtime, self.runtime.load_supplier_shipment(supplier_order_id) or {})
        matched_document = apply_supplier_order_document_match(self._with_download_path(document), shipment)
        documents = [matched_document]
        lines = list(document.get("expense_lines") or [])
        payload = matched_document
        payload["expense_lines"] = lines
        payload["summary"] = build_financial_summary(documents, lines, shipment=shipment)
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
        shipment = _supplier_order_shipment_with_linked_contract(self.runtime, self.runtime.load_supplier_shipment(supplier_order_id) or {})
        document = apply_supplier_order_document_match(document, shipment)
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
        shipment = self.runtime.load_supplier_shipment(supplier_order_id) or {}
        payload["summary"] = build_financial_summary([payload], list(payload.get("expense_lines") or []), shipment=shipment)
        return payload

    def delete_document(self, supplier_order_id: str, document_id: str) -> dict[str, Any]:
        self._ensure_supplier_order(supplier_order_id)
        document = self.runtime.load_supplier_financial_document(
            supplier_order_id=supplier_order_id,
            document_id=document_id,
        )
        if document is None:
            raise ValueError(f"financial document not found: {document_id}")
        deleted = self.runtime.delete_supplier_financial_document(
            supplier_order_id=supplier_order_id,
            document_id=document_id,
        )
        if deleted is None:
            raise ValueError(f"financial document not found: {document_id}")
        file_result = self._delete_owned_document_file(document)
        return {
            "contract_name": "sheet_vitrina_v1_supplier_financial_documents",
            "status": "ok",
            "supplier_order_id": supplier_order_id,
            "document_id": document_id,
            "deleted": True,
            "file_deleted": bool(file_result.get("file_deleted")),
            "warnings": _dedupe_strings(_string_list(file_result.get("warnings"))),
        }

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

    def _refresh_saved_document_parses(self, documents: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [self._refresh_saved_document_parse(document) for document in documents]

    def _refresh_saved_document_parse(self, document: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(document)
        if not self._saved_document_needs_parse_refresh(payload):
            return payload
        stored_file_path = str(payload.get("stored_file_path") or "").strip()
        original_filename = str(payload.get("original_filename") or "financial-document.pdf").strip() or "financial-document.pdf"
        try:
            file_path = self._resolve_runtime_file(stored_file_path)
            file_bytes = file_path.read_bytes()
        except OSError:
            return payload
        except ValueError:
            return payload
        parsed = parse_financial_document_pdf(
            file_bytes,
            filename=original_filename,
            text_extractor=self.pdf_text_extractor,
        )
        normalized = dict(parsed.get("normalized_parse") or {})
        existing_type = str(payload.get("document_type") or "")
        parsed_type = str(normalized.get("document_type") or "")
        if not parsed_type or (existing_type and parsed_type != existing_type):
            return payload
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
        if parse_status == FINANCIAL_DOCUMENT_PARSE_STATUS_PARSE_ERROR:
            return payload
        existing_status = str(payload.get("parse_status") or "")
        if existing_status == FINANCIAL_DOCUMENT_PARSE_STATUS_CONFIRMED:
            parse_status = FINANCIAL_DOCUMENT_PARSE_STATUS_CONFIRMED
        now = self.timestamp_factory()
        document_id = str(payload.get("document_id") or "")
        supplier_order_id = str(payload.get("supplier_order_id") or "")
        updated_document = {
            **payload,
            "document_type": parsed_type,
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
            "raw_parse": dict(parsed.get("raw_parse") or {}),
            "normalized_parse": normalized,
            "parser_version": parsed.get("parser_version") or FINANCIAL_DOCUMENT_PARSER_VERSION,
            "warnings": _dedupe_strings(warnings),
            "errors": _dedupe_strings(errors),
        }
        stored_lines = [
            _expense_line_for_storage(
                line,
                supplier_order_id=supplier_order_id,
                document_id=document_id,
                sort_order=index,
            )
            for index, line in enumerate(expense_lines, start=1)
        ]
        return self.runtime.save_supplier_financial_document(
            document=updated_document,
            expense_lines=stored_lines,
        )

    def _saved_document_needs_parse_refresh(self, document: Mapping[str, Any]) -> bool:
        if str(document.get("parse_status") or "") == FINANCIAL_DOCUMENT_PARSE_STATUS_EXCLUDED:
            return False
        document_type = str(document.get("document_type") or "")
        if document_type != FINANCIAL_DOCUMENT_TYPE_CUSTOMS_DECLARATION:
            return False
        if not str(document.get("stored_file_path") or "").strip():
            return False
        normalized = dict(document.get("normalized_parse") or {})
        if _positive_decimal(normalized.get("customs_gross_weight_kg") or normalized.get("gross_weight_kg")) is None:
            return True
        if _positive_decimal(normalized.get("total_customs_value_rub")) is None:
            return True
        if str(document.get("parser_version") or "") != FINANCIAL_DOCUMENT_PARSER_VERSION:
            return True
        return False

    def _ensure_supplier_order(self, supplier_order_id: str) -> None:
        if self.runtime.load_supplier_shipment(str(supplier_order_id or "").strip()) is None:
            raise ValueError(f"supplier shipment not found: {supplier_order_id}")

    def _shipment_registry_context(
        self,
        shipment_id: str,
        *,
        fallback_header: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_shipment_id = str(shipment_id or "").strip()
        detail = self.runtime.load_supplier_shipment(normalized_shipment_id)
        if detail is None:
            if fallback_header is None:
                raise ValueError(f"supplier shipment not found: {shipment_id}")
            detail = {"header": fallback_header, "lines": []}
        documents = self._refresh_saved_document_parses(
            self.runtime.list_supplier_financial_documents(normalized_shipment_id)
        )
        expense_lines = self.runtime.list_supplier_financial_expense_lines(normalized_shipment_id)
        summary = build_financial_summary(documents, expense_lines, shipment=detail)
        header = dict(detail.get("header") or fallback_header or {})
        if not header.get("shipment_id") and normalized_shipment_id:
            header["shipment_id"] = normalized_shipment_id
        return {
            "shipment_id": normalized_shipment_id,
            "header": header,
            "lines": [dict(item) for item in detail.get("lines") or []],
            "documents": documents,
            "expense_lines": expense_lines,
            "summary": summary,
        }

    def _write_document_file(self, *, supplier_order_id: str, document_id: str, filename: str, body: bytes) -> str:
        safe_filename = _safe_filename(filename)
        target_dir = self.runtime.runtime_dir / "supplier_financial_documents" / "files" / supplier_order_id / document_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / safe_filename
        target_path.write_bytes(body)
        return _relative_to_runtime(self.runtime.runtime_dir, target_path)

    def _delete_owned_document_file(self, document: Mapping[str, Any]) -> dict[str, Any]:
        stored_file_path = str(document.get("stored_file_path") or "").strip()
        supplier_order_id = str(document.get("supplier_order_id") or "").strip()
        document_id = str(document.get("document_id") or "").strip()
        warnings: list[str] = []
        if not stored_file_path:
            return {"file_deleted": False, "warnings": warnings}
        try:
            target_path = self._resolve_runtime_file(stored_file_path)
        except ValueError as exc:
            return {"file_deleted": False, "warnings": [f"stored PDF was not removed: {exc}"]}
        files_root = (self.runtime.runtime_dir / "supplier_financial_documents" / "files").resolve()
        expected_dir = (files_root / supplier_order_id / document_id).resolve()
        if not _path_is_relative_to(expected_dir, files_root) or not _path_is_relative_to(target_path, expected_dir):
            return {"file_deleted": False, "warnings": ["stored PDF was not removed: path does not belong to this financial document"]}
        file_deleted = False
        try:
            if target_path.exists() and target_path.is_file():
                target_path.unlink()
                file_deleted = True
            elif target_path.exists():
                warnings.append("stored PDF was not removed: path is not a regular file")
            for directory in (expected_dir, expected_dir.parent):
                try:
                    directory.rmdir()
                except OSError:
                    pass
        except OSError as exc:
            warnings.append(f"stored PDF was not removed: {exc}")
        return {"file_deleted": file_deleted, "warnings": warnings}

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
    if (
        (text_extractor is None or text_extractor is extract_pdf_text_layer)
        and _customs_parse_missing_weight_or_value(parsed)
        and dict(diagnostics).get("method") != "pypdf"
    ):
        pypdf_diagnostics: dict[str, Any] = {
            "filename": filename,
            "method": "pypdf",
            "fallback_from": dict(diagnostics).get("method") or "",
        }
        pypdf_text = _extract_pdf_text_with_pypdf(file_bytes, pypdf_diagnostics)
        if _is_text_layer_sufficient(pypdf_text):
            pypdf_diagnostics["text_char_count"] = len(pypdf_text)
            fallback = parse_financial_document_text(
                pypdf_text,
                filename=filename,
                extraction_diagnostics=pypdf_diagnostics,
            )
            if not _customs_parse_missing_weight_or_value(fallback):
                raw_parse = dict(fallback.get("raw_parse") or {})
                raw_parse["primary_extraction"] = dict(diagnostics)
                fallback["raw_parse"] = raw_parse
                fallback["warnings"] = _dedupe_strings(_string_list(fallback.get("warnings")))
                return fallback
    return parsed


def _customs_parse_missing_weight_or_value(parsed: Mapping[str, Any]) -> bool:
    normalized = dict(parsed.get("normalized_parse") or {})
    if str(normalized.get("document_type") or "") != FINANCIAL_DOCUMENT_TYPE_CUSTOMS_DECLARATION:
        return False
    gross_weight = _positive_decimal(normalized.get("customs_gross_weight_kg") or normalized.get("gross_weight_kg"))
    customs_value = _positive_decimal(normalized.get("total_customs_value_rub"))
    return gross_weight is None or customs_value is None


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
    elif document_type == FINANCIAL_DOCUMENT_TYPE_BANK_CONTROL_STATEMENT:
        normalized, expense_lines, parser_warnings = _parse_bank_control_statement(normalized_text)
    elif document_type == FINANCIAL_DOCUMENT_TYPE_BANK_TRANSFER_APPLICATION:
        normalized, expense_lines, parser_warnings = _parse_bank_transfer_application(normalized_text)
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
    if "ведомость банковского контроля по контракту" in haystack:
        return FINANCIAL_DOCUMENT_TYPE_BANK_CONTROL_STATEMENT
    if "заявление" in haystack and "на перевод" in haystack:
        return FINANCIAL_DOCUMENT_TYPE_BANK_TRANSFER_APPLICATION
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


def build_financial_summary(
    documents: list[Mapping[str, Any]],
    expense_lines: list[Mapping[str, Any]],
    *,
    shipment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
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
    quote_meta = dict(quote_doc.get("normalized_parse") or {})
    quote_required_complete = bool(quote_meta.get("quote_required_amounts_complete")) if quote_docs else False
    quote_missing_required = _string_list(quote_meta.get("quote_missing_required_amounts"))
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
    customs_without_vat_rub = _sum_required(customs_fee_rub, import_duty_rub)
    delivery_customs_total_rub = _sum_required(invoice_fact_rub, customs_total_rub)

    customs_metas = [dict(doc.get("normalized_parse") or {}) for doc in customs_docs]
    quote_gross_weight = _positive_decimal(quote_meta.get("gross_weight_kg"))
    customs_gross_weight = _positive_decimal(
        _sum_decimal(
            meta.get("customs_gross_weight_kg") if meta.get("customs_gross_weight_kg") is not None else meta.get("gross_weight_kg")
            for meta in customs_metas
        )
    )
    customs_net_weight = _positive_decimal(
        _sum_decimal(
            meta.get("customs_net_weight_kg") if meta.get("customs_net_weight_kg") is not None else meta.get("net_weight_kg")
            for meta in customs_metas
        )
    )
    quote_estimated_cargo_value_usd = _positive_decimal(quote_meta.get("estimated_cargo_value_usd"))
    customs_total_customs_value_rub = _positive_decimal(
        _sum_decimal(meta.get("total_customs_value_rub") for meta in customs_metas)
    )
    volume = _parse_decimal(quote_meta.get("volume_m3"))
    logistics_rub_per_kg = _safe_div(invoice_fact_rub, quote_gross_weight)
    logistics_rub_per_m3 = _safe_div(invoice_fact_rub, volume)
    if quote_docs and quote_gross_weight is None:
        warnings.append("Нет веса КП")
    if customs_docs and customs_gross_weight is None:
        warnings.append("Нет фактического веса из ДТ")
    if quote_docs and quote_estimated_cargo_value_usd is None:
        warnings.append("Нет стоимости груза по КП")
    if customs_docs and customs_total_customs_value_rub is None:
        warnings.append("Нет таможенной стоимости из ДТ")
    if quote_docs and volume is None:
        warnings.append("Объем из КП не распознан: ₽/м³ не рассчитан")

    per_kg_quote_weight = {
        "weight_kg": _decimal_to_float(quote_gross_weight),
        "logistics_invoice_rub_per_kg": _decimal_to_float(_safe_div(invoice_fact_rub, quote_gross_weight)),
        "customs_payments_rub_per_kg": _decimal_to_float(_safe_div(customs_total_rub, quote_gross_weight)),
        "delivery_customs_rub_per_kg": _decimal_to_float(_safe_div(delivery_customs_total_rub, quote_gross_weight)),
    }
    per_kg_customs_weight = {
        "weight_kg": _decimal_to_float(customs_gross_weight),
        "logistics_invoice_rub_per_kg": _decimal_to_float(_safe_div(invoice_fact_rub, customs_gross_weight)),
        "customs_payments_rub_per_kg": _decimal_to_float(_safe_div(customs_total_rub, customs_gross_weight)),
        "delivery_customs_rub_per_kg": _decimal_to_float(_safe_div(delivery_customs_total_rub, customs_gross_weight)),
    }
    quote_percent_of_cargo_value = {
        "cargo_value_usd": _decimal_to_float(quote_estimated_cargo_value_usd),
        "logistics_pct": _decimal_to_float(_percent(quote_logistics_usd, quote_estimated_cargo_value_usd)),
        "customs_pct": _decimal_to_float(_percent(quote_customs_usd, quote_estimated_cargo_value_usd)),
        "delivery_customs_pct": _decimal_to_float(_percent(quote_total_usd, quote_estimated_cargo_value_usd)),
    }
    fact_percent_of_customs_value = {
        "customs_value_rub": _decimal_to_float(customs_total_customs_value_rub),
        "customs_payments_without_vat_rub": _decimal_to_float(customs_without_vat_rub),
        "logistics_pct": _decimal_to_float(_percent(invoice_fact_rub, customs_total_customs_value_rub)),
        "customs_without_vat_pct": _decimal_to_float(_percent(customs_without_vat_rub, customs_total_customs_value_rub)),
        "customs_with_vat_pct": _decimal_to_float(_percent(customs_total_rub, customs_total_customs_value_rub)),
        "delivery_customs_pct": _decimal_to_float(_percent(delivery_customs_total_rub, customs_total_customs_value_rub)),
    }

    linked_quote_usd_for_rate = quote_logistics_usd if quote_required_complete else None
    rate_summary = _build_rate_summary(
        quote_doc=quote_doc,
        invoice_docs=invoice_docs,
        invoice_fact_rub=invoice_fact_rub,
        linked_quote_usd_component=linked_quote_usd_for_rate,
        quote_base_status="parsed" if quote_required_complete else ("missing_required_amounts" if quote_docs else ""),
    )
    total_units = _shipment_total_units(shipment)
    estimated_bank_rate = _estimate_bank_rate_on_quote_date(
        quote_doc=quote_doc,
        invoice_docs=invoice_docs,
        invoice_fact_rub=invoice_fact_rub,
        linked_quote_usd_component=linked_quote_usd_for_rate,
        quote_base_status="parsed" if quote_required_complete else ("missing_required_amounts" if quote_docs else ""),
    )
    quote_total_rate = estimated_bank_rate or (_parse_decimal(quote_doc.get("cbr_usd_rate_value")) if quote_doc else None)
    quote_total_rub_equivalent = quote_total_usd * quote_total_rate if quote_total_usd is not None and quote_total_rate is not None else None
    quote_delivery_customs_rub_per_unit = _safe_div(quote_total_rub_equivalent, total_units)
    fact_delivery_customs_rub_per_unit = _safe_div(delivery_customs_total_rub, total_units)
    if active_documents and total_units is None:
        warnings.append("Нет количества штук в поставке")
    warnings.extend(rate_summary.pop("warnings", []))
    if quote_docs and not quote_required_complete:
        missing_text = ", ".join(quote_missing_required) if quote_missing_required else "required quote amount(s)"
        warnings.append(f"КП требует проверки: не распознаны обязательные суммы ({missing_text}); расчётный курс не рассчитан")
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
            "total_rub_equivalent": _decimal_to_float(quote_total_rub_equivalent),
            "logistics_usd": _decimal_to_float(quote_logistics_usd),
            "customs_payments_usd": _decimal_to_float(quote_customs_usd),
            "logistics_rub_cbr": _decimal_to_float(quote_logistics_rub_cbr),
            "gross_weight_kg": _decimal_to_float(quote_gross_weight),
            "estimated_cargo_value_usd": _decimal_to_float(quote_estimated_cargo_value_usd),
            "required_amounts_complete": quote_required_complete,
            "missing_required_amounts": quote_missing_required,
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
            "customs_payments_without_vat_rub": _decimal_to_float(customs_without_vat_rub),
            "total_customs_value_rub": _decimal_to_float(customs_total_customs_value_rub),
            "gross_weight_kg": _decimal_to_float(customs_gross_weight),
            "net_weight_kg": _decimal_to_float(customs_net_weight),
            "document_count": len(customs_docs),
        },
        "logistics_efficiency": {
            "rub_per_kg": _decimal_to_float(logistics_rub_per_kg),
            "rub_per_m3": _decimal_to_float(logistics_rub_per_m3),
            "gross_weight_kg": _decimal_to_float(quote_gross_weight),
            "volume_m3": _decimal_to_float(volume),
        },
        "per_kg": {
            "quote_weight": per_kg_quote_weight,
            "customs_weight": per_kg_customs_weight,
        },
        "per_unit": {
            "total_units": _decimal_to_float(total_units),
            "quote_delivery_customs_rub_per_unit": _decimal_to_float(quote_delivery_customs_rub_per_unit),
            "fact_delivery_customs_rub_per_unit": _decimal_to_float(fact_delivery_customs_rub_per_unit),
            "quote_total_rub_equivalent": _decimal_to_float(quote_total_rub_equivalent),
            "fact_delivery_customs_total_rub": _decimal_to_float(delivery_customs_total_rub),
        },
        "percent_of_value": {
            "quote_cargo_value": quote_percent_of_cargo_value,
            "fact_customs_value": fact_percent_of_customs_value,
        },
        "quote_invoice_match": rate_summary,
        "warnings": _dedupe_strings(warnings),
    }


def _shipment_total_units(shipment: Mapping[str, Any] | None) -> Decimal | None:
    if not isinstance(shipment, Mapping):
        return None
    header = shipment.get("header") if isinstance(shipment.get("header"), Mapping) else shipment
    total = _positive_decimal(dict(header).get("product_qty_total"))
    if total is not None:
        return total
    lines = shipment.get("lines") if isinstance(shipment.get("lines"), list) else []
    return _positive_decimal(
        _sum_decimal(
            item.get("qty")
            for item in lines
            if isinstance(item, Mapping) and str(item.get("line_type") or "") == "product"
        )
    )


def build_supplier_shipment_registry(contexts: list[Mapping[str, Any]]) -> dict[str, Any]:
    sorted_contexts = sorted(
        [dict(item) for item in contexts],
        key=lambda item: _registry_sort_key(item),
    )
    columns = [_registry_column(item) for item in sorted_contexts]
    warnings = _registry_date_warnings(sorted_contexts)
    sections = []
    for section_id, title, rows in _registry_row_definitions():
        section_rows = []
        for row_id, label, cell_factory in rows:
            section_rows.append(
                {
                    "row_id": row_id,
                    "label": label,
                    "cells": {
                        column["shipment_id"]: cell_factory(context)
                        for column, context in zip(columns, sorted_contexts, strict=False)
                    },
                }
            )
        sections.append({"section_id": section_id, "title": title, "rows": section_rows})
    return {
        "contract_name": "sheet_vitrina_v1_supplier_shipment_registry",
        "status": "ok",
        "columns": columns,
        "sections": sections,
        "warnings": warnings,
        "meta": {
            "shipment_count": len(columns),
            "warning_count": len(warnings),
            "sort": "invoice_date/shipment_date/created_at ascending; newer shipments are rightmost",
        },
    }


def build_supplier_shipment_registry_quote_comparison(
    *,
    quote_context: Mapping[str, Any],
    shipment_context: Mapping[str, Any],
    uploaded_filename: str,
) -> dict[str, Any]:
    quote_doc = _registry_doc(quote_context, FINANCIAL_DOCUMENT_TYPE_LOGISTICS_QUOTE)
    parse_status = str(quote_doc.get("parse_status") or "")
    sections = _comparison_sections(quote_context, shipment_context)
    warnings = _dedupe_strings(
        [
            *_string_list(quote_doc.get("warnings")),
            *_string_list(quote_doc.get("errors")),
            *_comparison_quote_unit_estimator_warnings(quote_context, shipment_context),
        ]
    )
    return {
        "contract_name": "sheet_vitrina_v1_supplier_shipment_registry_quote_comparison",
        "status": "needs_review" if parse_status == FINANCIAL_DOCUMENT_PARSE_STATUS_NEEDS_REVIEW else "ok",
        "quote": _comparison_quote_payload(quote_context, uploaded_filename=uploaded_filename),
        "selected_shipment": _comparison_shipment_payload(shipment_context),
        "sections": sections,
        "warnings": warnings,
    }


def _comparison_quote_payload(context: Mapping[str, Any], *, uploaded_filename: str) -> dict[str, Any]:
    quote_doc = _registry_doc(context, FINANCIAL_DOCUMENT_TYPE_LOGISTICS_QUOTE)
    normalized = dict(quote_doc.get("normalized_parse") or {})
    return {
        "filename": uploaded_filename,
        "parse_status": quote_doc.get("parse_status") or "",
        "document_type": quote_doc.get("document_type") or "",
        "vendor": quote_doc.get("vendor") or normalized.get("vendor") or "",
        "quote_date": normalized.get("quote_date") or quote_doc.get("document_date") or "",
        "tariff": normalized.get("tariff") or "",
        "route": normalized.get("route") or _registry_route(context),
        "normalized_parse": normalized,
        "summary": _registry_summary(context),
        "warnings": _dedupe_strings(_string_list(quote_doc.get("warnings"))),
        "errors": _dedupe_strings(_string_list(quote_doc.get("errors"))),
    }


def _comparison_shipment_payload(context: Mapping[str, Any]) -> dict[str, Any]:
    column = _registry_column(context)
    return {
        **column,
        "supplier": _registry_header(context).get("supplier_name") or "",
        "logistics_vendor": _registry_logistics_vendor(context),
        "document_status": _registry_document_status(context),
        "summary": _registry_summary(context),
    }


def _comparison_sections(
    quote_context: Mapping[str, Any],
    shipment_context: Mapping[str, Any],
) -> list[dict[str, Any]]:
    sections = [
        (
            "cargo_physics",
            "A. Физика груза",
            [
                _comparison_row(
                    "gross_weight_kg",
                    "Вес, кг",
                    _registry_number(_summary_path(quote_context, "quote", "gross_weight_kg"), suffix=" кг"),
                    _registry_number(_summary_path(shipment_context, "customs_declaration", "gross_weight_kg"), suffix=" кг"),
                    suffix=" кг",
                ),
                _comparison_row(
                    "volume_m3",
                    "Объём, м³",
                    _registry_number(_summary_path(quote_context, "logistics_efficiency", "volume_m3"), suffix=" м³"),
                    _registry_number(_summary_path(shipment_context, "logistics_efficiency", "volume_m3"), suffix=" м³"),
                    suffix=" м³",
                ),
                _comparison_row(
                    "cargo_value_usd",
                    "Стоимость груза, USD",
                    _registry_money(_summary_path(quote_context, "quote", "estimated_cargo_value_usd"), "USD"),
                    _registry_money(_summary_path(shipment_context, "quote", "estimated_cargo_value_usd"), "USD"),
                    suffix=" USD",
                ),
            ],
        ),
        (
            "quote_logistics",
            "B. КП / логистика",
            [
                _comparison_row(
                    "quote_logistics_usd",
                    "Услуги логиста, USD",
                    _registry_money(_summary_path(quote_context, "quote", "logistics_usd"), "USD"),
                    _registry_money(_summary_path(shipment_context, "quote", "logistics_usd"), "USD"),
                    suffix=" USD",
                    direction="lower_is_better",
                ),
                _comparison_row(
                    "quote_customs_usd",
                    "Таможня по КП/оценке, USD",
                    _registry_money(_summary_path(quote_context, "quote", "customs_payments_usd"), "USD"),
                    _registry_money(_summary_path(shipment_context, "quote", "customs_payments_usd"), "USD"),
                    suffix=" USD",
                    direction="lower_is_better",
                ),
                _comparison_row(
                    "quote_total_usd",
                    "Всего доставка+таможня, USD",
                    _registry_money(_summary_path(quote_context, "quote", "total_usd"), "USD"),
                    _registry_money(_summary_path(shipment_context, "quote", "total_usd"), "USD"),
                    suffix=" USD",
                    direction="lower_is_better",
                ),
                _comparison_row(
                    "quote_logistics_pct",
                    "КП: услуги логиста, % от стоимости груза",
                    _registry_percent(_summary_path(quote_context, "percent_of_value", "quote_cargo_value", "logistics_pct")),
                    _registry_percent(_summary_path(shipment_context, "percent_of_value", "quote_cargo_value", "logistics_pct")),
                    suffix="%",
                    direction="lower_is_better",
                ),
                _comparison_row(
                    "quote_customs_pct",
                    "КП: таможня, % от стоимости груза",
                    _registry_percent(_summary_path(quote_context, "percent_of_value", "quote_cargo_value", "customs_pct")),
                    _registry_percent(_summary_path(shipment_context, "percent_of_value", "quote_cargo_value", "customs_pct")),
                    suffix="%",
                    direction="lower_is_better",
                ),
                _comparison_row(
                    "quote_total_pct",
                    "КП: доставка+таможня, % от стоимости груза",
                    _registry_percent(_summary_path(quote_context, "percent_of_value", "quote_cargo_value", "delivery_customs_pct")),
                    _registry_percent(_summary_path(shipment_context, "percent_of_value", "quote_cargo_value", "delivery_customs_pct")),
                    suffix="%",
                    direction="lower_is_better",
                ),
            ],
        ),
        (
            "normalized",
            "C. Нормализованные метрики",
            [
                _comparison_row(
                    "logistics_rub_per_kg",
                    "Услуги логиста, ₽/кг",
                    _registry_money(_quote_component_rub_per_kg_for_comparison(quote_context, "logistics"), "₽"),
                    _registry_money(_summary_path(shipment_context, "per_kg", "customs_weight", "logistics_invoice_rub_per_kg"), "₽"),
                    suffix=" ₽",
                    direction="lower_is_better",
                ),
                _comparison_row(
                    "customs_rub_per_kg",
                    "Таможня, ₽/кг",
                    _registry_money(_quote_component_rub_per_kg_for_comparison(quote_context, "customs"), "₽"),
                    _registry_money(_summary_path(shipment_context, "per_kg", "customs_weight", "customs_payments_rub_per_kg"), "₽"),
                    suffix=" ₽",
                    direction="lower_is_better",
                ),
                _comparison_row(
                    "delivery_customs_rub_per_kg",
                    "Доставка+таможня, ₽/кг",
                    _registry_money(_quote_component_rub_per_kg_for_comparison(quote_context, "total"), "₽"),
                    _registry_money(_summary_path(shipment_context, "per_kg", "customs_weight", "delivery_customs_rub_per_kg"), "₽"),
                    suffix=" ₽",
                    direction="lower_is_better",
                ),
                _comparison_row(
                    "delivery_customs_rub_per_unit",
                    "Доставка+таможня, ₽/шт",
                    _quote_delivery_customs_rub_per_unit_estimate_cell(quote_context, shipment_context),
                    _registry_money(_summary_path(shipment_context, "per_unit", "fact_delivery_customs_rub_per_unit"), "₽"),
                    suffix=" ₽",
                    direction="lower_is_better",
                ),
                _comparison_row(
                    "delivery_customs_pct_of_value",
                    "Доставка+таможня, % от стоимости",
                    _registry_percent(_summary_path(quote_context, "percent_of_value", "quote_cargo_value", "delivery_customs_pct")),
                    _registry_percent(_summary_path(shipment_context, "percent_of_value", "fact_customs_value", "delivery_customs_pct")),
                    suffix="%",
                    direction="lower_is_better",
                ),
            ],
        ),
        (
            "lead_times",
            "D. Сроки",
            [
                _comparison_row(
                    "quote_delivery_days",
                    "Срок доставки по КП",
                    _quote_delivery_days_cell(quote_context),
                    _quote_delivery_days_cell(shipment_context),
                    suffix=" дн.",
                    decimals=0,
                ),
                _comparison_row(
                    "days_to_customs_declaration",
                    "Срок до ДТ",
                    _registry_blank(),
                    _registry_number(_days_to_customs_declaration(shipment_context), suffix=" дн.", decimals=0),
                    suffix=" дн.",
                    decimals=0,
                ),
            ],
        ),
    ]
    return [{"section_id": section_id, "title": title, "rows": rows} for section_id, title, rows in sections]


def _comparison_row(
    row_id: str,
    label: str,
    quote_cell: Mapping[str, Any],
    shipment_cell: Mapping[str, Any],
    *,
    suffix: str = "",
    decimals: int = 2,
    direction: str = "neutral",
) -> dict[str, Any]:
    quote_payload = dict(quote_cell or _registry_blank())
    shipment_payload = dict(shipment_cell or _registry_blank())
    return {
        "row_id": row_id,
        "label": label,
        "quote": quote_payload,
        "shipment": shipment_payload,
        "difference": _comparison_difference_cell(
            quote_payload,
            shipment_payload,
            suffix=suffix,
            decimals=decimals,
        ),
        "status": _comparison_status_cell(quote_payload, shipment_payload, direction=direction),
    }


def _comparison_difference_cell(
    quote_cell: Mapping[str, Any],
    shipment_cell: Mapping[str, Any],
    *,
    suffix: str,
    decimals: int,
) -> dict[str, Any]:
    quote_value = _parse_decimal(quote_cell.get("value") if isinstance(quote_cell, Mapping) else None)
    shipment_value = _parse_decimal(shipment_cell.get("value") if isinstance(shipment_cell, Mapping) else None)
    if quote_value is None or shipment_value is None:
        return _registry_blank()
    delta = quote_value - shipment_value
    delta_cell = _registry_number(delta, suffix=suffix, decimals=decimals, signed=True)
    pct_delta = _percent(delta, shipment_value)
    if pct_delta is not None:
        pct_cell = _registry_number(pct_delta, suffix="%", decimals=2, signed=True)
        delta_cell["display"] = f"{delta_cell['display']} · {pct_cell['display']}"
    return delta_cell


def _comparison_status_cell(
    quote_cell: Mapping[str, Any],
    shipment_cell: Mapping[str, Any],
    *,
    direction: str,
) -> dict[str, Any]:
    if direction != "lower_is_better":
        return _registry_blank()
    quote_value = _parse_decimal(quote_cell.get("value") if isinstance(quote_cell, Mapping) else None)
    shipment_value = _parse_decimal(shipment_cell.get("value") if isinstance(shipment_cell, Mapping) else None)
    if quote_value is None or shipment_value is None:
        return _registry_blank()
    delta = quote_value - shipment_value
    tolerance = max(abs(shipment_value) * Decimal("0.005"), Decimal("0.01"))
    if abs(delta) <= tolerance:
        return _registry_cell("equal", "примерно равно")
    if delta < 0:
        return _registry_cell("better", "КП выгоднее")
    return _registry_cell("worse", "КП дороже")


def _comparison_quote_unit_estimator_warnings(
    quote_context: Mapping[str, Any],
    shipment_context: Mapping[str, Any],
) -> list[str]:
    quote_total_rub = _parse_decimal(_summary_path(quote_context, "quote", "total_rub_equivalent"))
    quote_weight = _parse_decimal(_summary_path(quote_context, "quote", "gross_weight_kg"))
    if quote_total_rub is None or quote_weight is None or quote_weight == 0:
        return []
    if _selected_shipment_units_per_kg_estimator(shipment_context) is None:
        return [QUOTE_UNIT_ESTIMATOR_MISSING_WARNING]
    return []


def _quote_delivery_customs_rub_per_unit_estimate_cell(
    quote_context: Mapping[str, Any],
    shipment_context: Mapping[str, Any],
) -> dict[str, Any]:
    quote_total_rub = _parse_decimal(_summary_path(quote_context, "quote", "total_rub_equivalent"))
    quote_weight = _parse_decimal(_summary_path(quote_context, "quote", "gross_weight_kg"))
    estimator = _selected_shipment_units_per_kg_estimator(shipment_context)
    if quote_total_rub is None or quote_weight is None or quote_weight == 0 or estimator is None:
        cell = _registry_blank()
        if quote_total_rub is not None and quote_weight is not None and quote_weight != 0:
            cell["note"] = QUOTE_UNIT_ESTIMATOR_MISSING_WARNING
        return cell
    units_per_kg = estimator["units_per_kg"]
    estimated_quote_units = quote_weight * units_per_kg
    value = _safe_div(quote_total_rub, estimated_quote_units)
    if value is None:
        cell = _registry_blank()
        cell["note"] = QUOTE_UNIT_ESTIMATOR_MISSING_WARNING
        return cell
    cell = _registry_money(value, "₽")
    units_per_kg_display = _registry_number(units_per_kg).get("display") or "—"
    estimated_units_display = _registry_number(estimated_quote_units, decimals=0).get("display") or "—"
    cell["note"] = f"оценочно по {units_per_kg_display} шт/кг из выбранной поставки; {estimated_units_display} шт."
    cell["estimator"] = {
        "method": "selected_shipment_units_per_kg",
        "units_per_kg": _decimal_to_float(units_per_kg),
        "estimated_quote_units": _decimal_to_float(estimated_quote_units),
        "quote_gross_weight_kg": _decimal_to_float(quote_weight),
        "selected_shipment_total_units": _decimal_to_float(estimator["total_units"]),
        "selected_shipment_weight_base_kg": _decimal_to_float(estimator["weight_base_kg"]),
        "selected_shipment_weight_source": estimator["weight_source"],
    }
    return cell


def _selected_shipment_units_per_kg_estimator(context: Mapping[str, Any]) -> dict[str, Any] | None:
    total_units = _parse_decimal(_summary_path(context, "per_unit", "total_units"))
    if total_units is None or total_units <= 0:
        return None
    weight_source = "customs_gross_weight_kg"
    weight_base = _parse_decimal(_summary_path(context, "customs_declaration", "gross_weight_kg"))
    if weight_base is None or weight_base <= 0:
        weight_source = "quote_gross_weight_kg"
        weight_base = _parse_decimal(_summary_path(context, "quote", "gross_weight_kg"))
    if weight_base is None or weight_base <= 0:
        return None
    units_per_kg = _safe_div(total_units, weight_base)
    if units_per_kg is None or units_per_kg <= 0:
        return None
    return {
        "total_units": total_units,
        "weight_base_kg": weight_base,
        "weight_source": weight_source,
        "units_per_kg": units_per_kg,
    }


def _quote_component_rub_per_kg_for_comparison(context: Mapping[str, Any], component: str) -> Decimal | None:
    rate = _parse_decimal(_summary_path(context, "quote_invoice_match", "estimated_bank_rate_on_quote_date"))
    quote_doc = _registry_doc(context, FINANCIAL_DOCUMENT_TYPE_LOGISTICS_QUOTE)
    if rate is None:
        rate = _parse_decimal(quote_doc.get("cbr_usd_rate_value"))
    if rate is None:
        rate = _parse_decimal(dict(_registry_quote_meta(context).get("cbr_usd_rate") or {}).get("rate_value"))
    weight = _parse_decimal(_summary_path(context, "quote", "gross_weight_kg"))
    if component == "logistics":
        usd = _parse_decimal(_summary_path(context, "quote", "logistics_usd"))
    elif component == "customs":
        usd = _parse_decimal(_summary_path(context, "quote", "customs_payments_usd"))
    else:
        usd = _parse_decimal(_summary_path(context, "quote", "total_usd"))
    return _safe_div(usd * rate if usd is not None and rate is not None else None, weight)


def _quote_delivery_days_cell(context: Mapping[str, Any]) -> dict[str, Any]:
    quote = _registry_quote_meta(context)
    min_days = _int_or_none(quote.get("delivery_days_min"))
    max_days = _int_or_none(quote.get("delivery_days_max"))
    value = max_days or min_days
    display = _quote_delivery_days_display(context)
    return _registry_cell(_decimal_to_float(Decimal(str(value))) if value is not None else None, display or "—")


def _registry_row_definitions() -> list[tuple[str, str, list[tuple[str, str, Callable[[Mapping[str, Any]], dict[str, Any]]]]]]:
    return [
        (
            "passport",
            "A. Паспорт поставки",
            [
                ("shipment_id", "order id / supplier order id", lambda item: _registry_text(_registry_header(item).get("shipment_id"))),
                ("invoice_no", "номер заказа / инвойса", lambda item: _registry_text(_registry_header(item).get("invoice_no"))),
                ("order_date", "дата заказа", lambda item: _registry_date(_date_part(_registry_header(item).get("created_at")))),
                ("invoice_date", "дата инвойса", lambda item: _registry_date(_registry_header(item).get("invoice_date"))),
                ("shipment_date", "Плановая дата отгрузки", lambda item: _registry_strict_date(_registry_header(item).get("shipment_date"))),
                ("actual_shipment_date", "Фактическая дата отгрузки", lambda item: _registry_strict_date(_registry_header(item).get("actual_shipment_date"))),
                ("actual_ff_acceptance_date", "Фактическая дата приёмки на ФФ", lambda item: _registry_strict_date(_registry_header(item).get("actual_ff_acceptance_date"))),
                ("customs_date", "дата ДТ", lambda item: _registry_date(_registry_customs_meta(item).get("document_date") or _registry_customs_meta(item).get("declaration_date"))),
                ("supplier", "поставщик", lambda item: _registry_text(_registry_header(item).get("supplier_name"))),
                ("logistics_vendor", "логист", lambda item: _registry_text(_registry_logistics_vendor(item))),
                ("route", "маршрут", lambda item: _registry_text(_registry_route(item))),
                ("delivery_type", "тип доставки / сценарий", lambda item: _registry_text(_registry_quote_meta(item).get("tariff"))),
                ("document_status", "статус документов / warnings", lambda item: _registry_text(_registry_document_status(item))),
            ],
        ),
        (
            "cargo_physics",
            "B. Физика груза",
            [
                ("units", "количество штук", lambda item: _registry_number(_summary_path(item, "per_unit", "total_units"), decimals=0)),
                ("quote_weight", "вес КП", lambda item: _registry_number(_summary_path(item, "quote", "gross_weight_kg"), suffix=" кг")),
                ("customs_weight", "вес ДТ", lambda item: _registry_number(_summary_path(item, "customs_declaration", "gross_weight_kg"), suffix=" кг")),
                ("quote_volume", "объём КП", lambda item: _registry_number(_summary_path(item, "logistics_efficiency", "volume_m3"), suffix=" м³")),
                ("density", "плотность кг/м³", lambda item: _registry_number(_safe_div(_dec(_summary_path(item, "quote", "gross_weight_kg")), _dec(_summary_path(item, "logistics_efficiency", "volume_m3"))), suffix=" кг/м³")),
                ("units_per_quote_kg", "штук/кг по КП", lambda item: _registry_number(_safe_div(_dec(_summary_path(item, "per_unit", "total_units")), _dec(_summary_path(item, "quote", "gross_weight_kg"))))),
                ("units_per_customs_kg", "штук/кг по ДТ", lambda item: _registry_number(_safe_div(_dec(_summary_path(item, "per_unit", "total_units")), _dec(_summary_path(item, "customs_declaration", "gross_weight_kg"))))),
            ],
        ),
        (
            "cargo_value",
            "C. Стоимость товара",
            [
                ("quote_cargo_usd", "стоимость груза USD по КП", lambda item: _registry_money(_summary_path(item, "quote", "estimated_cargo_value_usd"), "USD")),
                ("quote_cargo_cny", "стоимость груза CNY по КП", lambda item: _registry_money(_registry_quote_meta(item).get("estimated_cargo_value_cny"), "CNY")),
                ("customs_value_rub", "таможенная стоимость ₽ из ДТ", lambda item: _registry_money(_summary_path(item, "customs_declaration", "total_customs_value_rub"), "₽")),
                ("goods_value_rub", "стоимость товара ₽ по курсу/ДТ", lambda item: _registry_blank()),
                ("goods_value_rub_per_unit", "стоимость товара ₽/шт", lambda item: _registry_money(_safe_div(_dec(_summary_path(item, "customs_declaration", "total_customs_value_rub")), _dec(_summary_path(item, "per_unit", "total_units"))), "₽")),
            ],
        ),
        (
            "quote_logistics",
            "D. КП логиста",
            [
                ("quote_total_usd", "КП всего USD", lambda item: _registry_money(_summary_path(item, "quote", "total_usd"), "USD")),
                ("quote_total_rub", "КП всего ₽", lambda item: _registry_money(_summary_path(item, "quote", "total_rub_equivalent"), "₽")),
                ("quote_logistics_usd", "КП логистика USD", lambda item: _registry_money(_summary_path(item, "quote", "logistics_usd"), "USD")),
                ("quote_customs_usd", "КП таможня USD", lambda item: _registry_money(_summary_path(item, "quote", "customs_payments_usd"), "USD")),
                ("quote_logistics_pct", "КП: услуги логиста, % от стоимости груза", lambda item: _registry_percent(_summary_path(item, "percent_of_value", "quote_cargo_value", "logistics_pct"))),
                ("quote_customs_pct", "КП: таможня, % от стоимости груза", lambda item: _registry_percent(_summary_path(item, "percent_of_value", "quote_cargo_value", "customs_pct"))),
                ("quote_total_pct", "КП: доставка+таможня, % от стоимости груза", lambda item: _registry_percent(_summary_path(item, "percent_of_value", "quote_cargo_value", "delivery_customs_pct"))),
                ("quote_total_rub_per_unit", "КП: доставка+таможня, ₽/шт", lambda item: _registry_money(_summary_path(item, "per_unit", "quote_delivery_customs_rub_per_unit"), "₽")),
                ("quote_logistics_rub_per_quote_kg", "КП: услуги логиста, ₽/кг по весу КП", lambda item: _registry_money(_quote_component_per_kg(item, "logistics"), "₽")),
                ("quote_customs_rub_per_quote_kg", "КП: таможня, ₽/кг по весу КП", lambda item: _registry_money(_quote_component_per_kg(item, "customs"), "₽")),
                ("quote_total_rub_per_quote_kg", "КП: доставка+таможня, ₽/кг по весу КП", lambda item: _registry_money(_quote_component_per_kg(item, "total"), "₽")),
            ],
        ),
        (
            "fact_expenses",
            "E. Факт расходов",
            [
                ("invoice_fact_rub", "счета логиста ₽", lambda item: _registry_money(_summary_path(item, "invoices", "fact_rub"), "₽")),
                ("invoice_vat_rub", "НДС по счетам ₽", lambda item: _registry_money(_summary_path(item, "invoices", "vat_rub"), "₽")),
                ("customs_fee_rub", "ДТ сбор ₽", lambda item: _registry_money(_summary_path(item, "customs_declaration", "customs_fee_1010_rub"), "₽")),
                ("customs_duty_rub", "ДТ пошлина ₽", lambda item: _registry_money(_summary_path(item, "customs_declaration", "import_duty_2010_rub"), "₽")),
                ("customs_vat_rub", "ДТ НДС ₽", lambda item: _registry_money(_summary_path(item, "customs_declaration", "import_vat_5010_rub"), "₽")),
                ("customs_total_rub", "ДТ всего ₽", lambda item: _registry_money(_summary_path(item, "customs_declaration", "total_customs_payments_rub"), "₽")),
                ("customs_without_vat_rub", "таможня без НДС ₽", lambda item: _registry_money(_summary_path(item, "customs_declaration", "customs_payments_without_vat_rub"), "₽")),
                ("other_expenses_rub", "прочие расходы ₽", lambda item: _registry_blank()),
                ("fact_total_rub", "факт доставка+таможня ₽", lambda item: _registry_money(_summary_path(item, "per_unit", "fact_delivery_customs_total_rub"), "₽")),
                ("fact_total_rub_per_unit", "факт доставка+таможня ₽/шт", lambda item: _registry_money(_summary_path(item, "per_unit", "fact_delivery_customs_rub_per_unit"), "₽")),
            ],
        ),
        (
            "fact_normalized",
            "F. Нормализованные метрики факта",
            [
                ("fact_logistics_per_quote_kg", "услуги логиста ₽/кг · вес КП", lambda item: _registry_money(_summary_path(item, "per_kg", "quote_weight", "logistics_invoice_rub_per_kg"), "₽")),
                ("fact_customs_per_quote_kg", "таможня ₽/кг · вес КП", lambda item: _registry_money(_summary_path(item, "per_kg", "quote_weight", "customs_payments_rub_per_kg"), "₽")),
                ("fact_total_per_quote_kg", "доставка+таможня ₽/кг · вес КП", lambda item: _registry_money(_summary_path(item, "per_kg", "quote_weight", "delivery_customs_rub_per_kg"), "₽")),
                ("fact_logistics_per_dt_kg", "услуги логиста ₽/кг · вес ДТ", lambda item: _registry_money(_summary_path(item, "per_kg", "customs_weight", "logistics_invoice_rub_per_kg"), "₽")),
                ("fact_customs_per_dt_kg", "таможня ₽/кг · вес ДТ", lambda item: _registry_money(_summary_path(item, "per_kg", "customs_weight", "customs_payments_rub_per_kg"), "₽")),
                ("fact_total_per_dt_kg", "доставка+таможня ₽/кг · вес ДТ", lambda item: _registry_money(_summary_path(item, "per_kg", "customs_weight", "delivery_customs_rub_per_kg"), "₽")),
                ("fact_logistics_pct", "факт: услуги логиста, % от таможенной стоимости", lambda item: _registry_percent(_summary_path(item, "percent_of_value", "fact_customs_value", "logistics_pct"))),
                ("fact_customs_without_vat_pct", "факт: таможня без НДС, % от таможенной стоимости", lambda item: _registry_percent(_summary_path(item, "percent_of_value", "fact_customs_value", "customs_without_vat_pct"))),
                ("fact_customs_with_vat_pct", "факт: таможня с НДС, % от таможенной стоимости", lambda item: _registry_percent(_summary_path(item, "percent_of_value", "fact_customs_value", "customs_with_vat_pct"))),
                ("fact_total_pct", "факт: доставка+таможня, % от таможенной стоимости", lambda item: _registry_percent(_summary_path(item, "percent_of_value", "fact_customs_value", "delivery_customs_pct"))),
            ],
        ),
        (
            "lead_times",
            "G. Сроки",
            [
                ("quote_delivery_days", "срок доставки по КП", lambda item: _registry_text(_quote_delivery_days_display(item))),
                ("actual_delivery_days", "Фактический срок поставки", lambda item: _registry_number(_actual_delivery_days(item), suffix=" дн.", decimals=0)),
                ("days_to_customs_declaration", "Срок до ДТ", lambda item: _registry_number(_days_to_customs_declaration(item), suffix=" дн.", decimals=0)),
            ],
        ),
        (
            "documents",
            "H. Документы",
            [
                ("has_quote", "есть КП", lambda item: _registry_bool(bool(_registry_doc(item, FINANCIAL_DOCUMENT_TYPE_LOGISTICS_QUOTE)))),
                ("has_invoices", "есть счета логиста", lambda item: _registry_bool(bool(_registry_docs(item, FINANCIAL_DOCUMENT_TYPE_LOGISTICS_INVOICE)))),
                ("has_customs", "есть ДТ", lambda item: _registry_bool(bool(_registry_doc(item, FINANCIAL_DOCUMENT_TYPE_CUSTOMS_DECLARATION)))),
                ("has_other_expenses", "есть прочие расходы", lambda item: _registry_bool(False)),
                ("needs_review_count", "needs_review count", lambda item: _registry_number(_needs_review_count(item), decimals=0)),
                ("parse_warnings_count", "parse warnings / needs_review count", lambda item: _registry_text(_parse_warning_status(item))),
            ],
        ),
    ]


def _registry_sort_key(context: Mapping[str, Any]) -> tuple[str, str, str, str]:
    header = _registry_header(context)
    invoice_date = _date_part(header.get("invoice_date"))
    shipment_date = _strict_date_part(header.get("shipment_date"))
    created_at = _date_part(header.get("created_at"))
    return (invoice_date or shipment_date or created_at or "9999-99-99", created_at or "", str(header.get("invoice_no") or ""), str(header.get("shipment_id") or ""))


def _registry_column(context: Mapping[str, Any]) -> dict[str, Any]:
    header = _registry_header(context)
    shipment_id = str(context.get("shipment_id") or header.get("shipment_id") or "")
    invoice_no = str(header.get("invoice_no") or "").strip()
    invoice_date = _date_part(header.get("invoice_date"))
    shipment_date = _date_part(header.get("shipment_date"))
    return {
        "shipment_id": shipment_id,
        "title": invoice_no or shipment_id,
        "subtitle": invoice_date or shipment_date or _date_part(header.get("created_at")),
        "invoice_no": invoice_no,
        "invoice_date": invoice_date,
        "shipment_date": shipment_date,
        "planned_shipment_date": shipment_date,
        "actual_shipment_date": _strict_date_part(header.get("actual_shipment_date")),
        "actual_ff_acceptance_date": _strict_date_part(header.get("actual_ff_acceptance_date")),
        "order_status": header.get("order_status") or "",
    }


def _registry_header(context: Mapping[str, Any]) -> dict[str, Any]:
    return dict(context.get("header") or {})


def _registry_summary(context: Mapping[str, Any]) -> dict[str, Any]:
    return dict(context.get("summary") or {})


def _summary_path(context: Mapping[str, Any], *path: str) -> Any:
    current: Any = _registry_summary(context)
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _registry_docs(context: Mapping[str, Any], document_type: str) -> list[dict[str, Any]]:
    return [
        dict(document)
        for document in context.get("documents") or []
        if isinstance(document, Mapping) and str(document.get("document_type") or "") == document_type
    ]


def _registry_doc(context: Mapping[str, Any], document_type: str) -> dict[str, Any]:
    docs = _registry_docs(context, document_type)
    return docs[0] if docs else {}


def _registry_quote_meta(context: Mapping[str, Any]) -> dict[str, Any]:
    return dict(_registry_doc(context, FINANCIAL_DOCUMENT_TYPE_LOGISTICS_QUOTE).get("normalized_parse") or {})


def _registry_customs_meta(context: Mapping[str, Any]) -> dict[str, Any]:
    return dict(_registry_doc(context, FINANCIAL_DOCUMENT_TYPE_CUSTOMS_DECLARATION).get("normalized_parse") or {})


def _registry_logistics_vendor(context: Mapping[str, Any]) -> str:
    for document_type in (FINANCIAL_DOCUMENT_TYPE_LOGISTICS_INVOICE, FINANCIAL_DOCUMENT_TYPE_LOGISTICS_QUOTE):
        doc = _registry_doc(context, document_type)
        if doc.get("vendor"):
            return str(doc.get("vendor") or "")
    return ""


def _registry_route(context: Mapping[str, Any]) -> str:
    for doc in _registry_docs(context, FINANCIAL_DOCUMENT_TYPE_LOGISTICS_INVOICE):
        if doc.get("route"):
            return str(doc.get("route") or "")
    quote = _registry_quote_meta(context)
    origin = str(quote.get("origin") or "").strip()
    destination = str(quote.get("destination") or "").strip()
    return " -> ".join(part for part in (origin, destination) if part)


def _registry_document_status(context: Mapping[str, Any]) -> str:
    documents = [dict(item) for item in context.get("documents") or [] if isinstance(item, Mapping)]
    if not documents:
        return "Документы заказа не загружены"
    types = {
        "КП": bool(_registry_doc(context, FINANCIAL_DOCUMENT_TYPE_LOGISTICS_QUOTE)),
        "счета": bool(_registry_docs(context, FINANCIAL_DOCUMENT_TYPE_LOGISTICS_INVOICE)),
        "ДТ": bool(_registry_doc(context, FINANCIAL_DOCUMENT_TYPE_CUSTOMS_DECLARATION)),
    }
    warnings_count = _parse_warnings_count(context)
    status = ", ".join(label for label, present in types.items() if present) or "нет распознанных типов"
    if warnings_count:
        status += f" · warnings: {warnings_count}"
    return status


def _quote_delivery_days_display(context: Mapping[str, Any]) -> str:
    quote = _registry_quote_meta(context)
    min_days = _int_or_none(quote.get("delivery_days_min"))
    max_days = _int_or_none(quote.get("delivery_days_max"))
    if min_days and max_days:
        return f"{min_days}-{max_days} дн."
    if min_days:
        return f"{min_days} дн."
    return ""


def _days_to_customs_declaration(context: Mapping[str, Any]) -> Decimal | None:
    header = _registry_header(context)
    start = _date_part(header.get("shipment_date"))
    end = _date_part(_registry_customs_meta(context).get("document_date") or _registry_customs_meta(context).get("declaration_date"))
    if not start or not end:
        return None
    try:
        return Decimal(str((date.fromisoformat(end) - date.fromisoformat(start)).days))
    except ValueError:
        return None


def _actual_delivery_days(context: Mapping[str, Any]) -> Decimal | None:
    header = _registry_header(context)
    start = _strict_date_part(header.get("actual_shipment_date"))
    end = _strict_date_part(header.get("actual_ff_acceptance_date"))
    if not start or not end:
        return None
    try:
        return Decimal(str((date.fromisoformat(end) - date.fromisoformat(start)).days))
    except ValueError:
        return None


def _registry_date_warnings(contexts: list[Mapping[str, Any]]) -> list[str]:
    labels = {
        "shipment_date": "Плановая дата отгрузки",
        "actual_shipment_date": "Фактическая дата отгрузки",
        "actual_ff_acceptance_date": "Фактическая дата приёмки на ФФ",
    }
    warnings: list[str] = []
    for context in contexts:
        header = _registry_header(context)
        shipment_label = str(header.get("invoice_no") or header.get("shipment_id") or context.get("shipment_id") or "supplier shipment")
        for field_name, label in labels.items():
            raw = str(header.get(field_name) or "").strip()
            if not raw:
                continue
            if not _strict_date_part(raw):
                warnings.append(f"{shipment_label}: {label} has invalid date value {raw!r}; cell rendered as —.")
    return _dedupe_strings(warnings)


def _quote_component_per_kg(context: Mapping[str, Any], component: str) -> Decimal | None:
    summary = _registry_summary(context)
    quote = dict(summary.get("quote") or {})
    rate = _parse_decimal(_summary_path(context, "quote_invoice_match", "estimated_bank_rate_on_quote_date"))
    weight = _parse_decimal(quote.get("gross_weight_kg"))
    if rate is None or weight is None or weight == 0:
        return None
    if component == "logistics":
        usd = _parse_decimal(quote.get("logistics_usd"))
    elif component == "customs":
        usd = _parse_decimal(quote.get("customs_payments_usd"))
    else:
        usd = _parse_decimal(quote.get("total_usd"))
    return _safe_div(usd * rate if usd is not None else None, weight)


def _needs_review_count(context: Mapping[str, Any]) -> int:
    return sum(1 for document in context.get("documents") or [] if isinstance(document, Mapping) and str(document.get("parse_status") or "") == FINANCIAL_DOCUMENT_PARSE_STATUS_NEEDS_REVIEW)


def _parse_warnings_count(context: Mapping[str, Any]) -> int:
    count = 0
    for document in context.get("documents") or []:
        if isinstance(document, Mapping):
            count += len(_string_list(document.get("warnings")))
    count += len(_string_list(_registry_summary(context).get("warnings")))
    return count


def _parse_warning_status(context: Mapping[str, Any]) -> str:
    needs_review = _needs_review_count(context)
    warnings_count = _parse_warnings_count(context)
    if needs_review or warnings_count:
        return f"needs_review: {needs_review}; warnings: {warnings_count}"
    return "нет"


def _registry_cell(value: Any, display: str) -> dict[str, Any]:
    return {"value": value, "display": display or "—"}


def _registry_blank() -> dict[str, Any]:
    return _registry_cell(None, "—")


def _registry_text(value: Any) -> dict[str, Any]:
    text = str(value or "").strip()
    return _registry_cell(text or None, text or "—")


def _registry_bool(value: bool) -> dict[str, Any]:
    return _registry_cell(bool(value), "Да" if value else "Нет")


def _registry_date(value: Any) -> dict[str, Any]:
    normalized = _date_part(value)
    return _registry_cell(normalized or None, normalized or "—")


def _registry_strict_date(value: Any) -> dict[str, Any]:
    normalized = _strict_date_part(value)
    return _registry_cell(normalized or None, normalized or "—")


def _registry_number(value: Any, *, suffix: str = "", decimals: int = 2, signed: bool = False) -> dict[str, Any]:
    decimal_value = _parse_decimal(value)
    if decimal_value is None:
        return _registry_blank()
    quant = Decimal("1") if decimals <= 0 else Decimal("1").scaleb(-decimals)
    rounded = decimal_value.quantize(quant, rounding=ROUND_HALF_UP)
    display = f"{rounded:,.{max(decimals, 0)}f}".replace(",", " ")
    if decimals <= 0:
        display = str(int(rounded))
    if signed and rounded > 0:
        display = "+" + display
    return _registry_cell(_decimal_to_float(decimal_value), display + suffix)


def _registry_money(value: Any, currency: str) -> dict[str, Any]:
    decimal_value = _parse_decimal(value)
    if decimal_value is None:
        return _registry_blank()
    suffix = f" {currency}" if currency else ""
    return _registry_number(decimal_value, suffix=suffix, decimals=2)


def _registry_percent(value: Any) -> dict[str, Any]:
    decimal_value = _parse_decimal(value)
    if decimal_value is None:
        return _registry_blank()
    return _registry_number(decimal_value, suffix="%", decimals=2)


def _date_part(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "T" in raw:
        raw = raw.split("T", 1)[0]
    return _optional_iso_date(raw) or raw[:10]


def _strict_date_part(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "T" in raw:
        raw = raw.split("T", 1)[0]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return ""
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        return ""


def _dec(value: Any) -> Decimal | None:
    return _parse_decimal(value)


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
    missing_required = _missing_required_quote_amounts(amounts, total_quote_usd)
    quote_logistics_component = _sum_decimal(amounts.get(key) for key in QUOTE_LOGISTICS_COMPONENT_CATEGORIES)
    quote_customs_component = _parse_decimal(amounts.get(EXPENSE_CATEGORY_CUSTOMS_PAYMENTS))
    quote_core_sum = _sum_decimal(amounts.get(key) for key in QUOTE_CORE_AMOUNT_CATEGORIES)
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
        "total_quote_usd": _decimal_to_float(total_quote_usd),
        "currency": "USD",
        "payment_rate_policy": "курс Банка ВТБ на дату выставления счёта" if "Банка ВТБ" in text else "",
        "validity_days": _int_or_none(_first_match(text, r"действительно в течение\s+(\d+)\s+календар")),
        "delivery_cost_usd": _decimal_to_float(_parse_decimal(amounts.get(EXPENSE_CATEGORY_DELIVERY))),
        "customs_payments_and_fees_usd": _decimal_to_float(_parse_decimal(amounts.get(EXPENSE_CATEGORY_CUSTOMS_PAYMENTS))),
        "ecological_fee_usd": _decimal_to_float(_parse_decimal(amounts.get(EXPENSE_CATEGORY_ECOLOGICAL_FEE))),
        "brokerage_services_usd": _decimal_to_float(_parse_decimal(amounts.get(EXPENSE_CATEGORY_BROKERAGE))),
        "company_commission_usd": _decimal_to_float(_parse_decimal(amounts.get(EXPENSE_CATEGORY_COMPANY_COMMISSION))),
        "insurance_usd": _decimal_to_float(_parse_decimal(amounts.get(EXPENSE_CATEGORY_INSURANCE))),
        "quote_logistics_component_usd": _decimal_to_float(quote_logistics_component),
        "quote_customs_component_usd": _decimal_to_float(quote_customs_component),
        "quote_core_amounts_sum_usd": _decimal_to_float(quote_core_sum),
        "quote_required_amounts_complete": not missing_required,
        "quote_missing_required_amounts": missing_required,
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
    if missing_required:
        warnings.append("Quote parser did not find required amount(s): " + ", ".join(missing_required))
    if total_quote_usd is not None and quote_core_sum is not None and not missing_required:
        if abs(quote_core_sum - total_quote_usd) > Decimal("0.01"):
            warnings.append(
                "Quote parser needs review: core amount sum "
                f"{_decimal_to_display(quote_core_sum)} does not match total {_decimal_to_display(total_quote_usd)}"
            )
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
    gross_weight_kg, net_weight_kg, weight_item_count = _extract_customs_item_weights(text)
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
        "customs_gross_weight_kg": _decimal_to_float(gross_weight_kg),
        "customs_net_weight_kg": _decimal_to_float(net_weight_kg),
        "gross_weight_kg": _decimal_to_float(gross_weight_kg),
        "net_weight_kg": _decimal_to_float(net_weight_kg),
        "customs_weight_item_count": weight_item_count,
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
    if total_goods_count and weight_item_count and weight_item_count != total_goods_count:
        warnings.append(f"Customs declaration item weight count {weight_item_count} does not match goods count {total_goods_count}")
    if total_goods_count and not weight_item_count:
        warnings.append("Customs declaration item gross/net weights were not recognized")
    return normalized, lines, warnings


def _parse_bank_control_statement(text: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    unique_contract_number = _extract_bank_control_unique_contract_number(text)
    document_date = _parse_date(_compact_spaced_date(_first_match(
        text,
        r"Уникальный номер контракта\s+[\d\s/]+\s+от\s+(\d{1,2}\.\d{1,2}\.\s*\d\s*\d\s*\d\s*\d)",
    )))
    resident_name = _extract_bank_control_resident(text)
    non_resident = _extract_bank_control_non_resident(text)
    contract_number, contract_date, contract_currency, contract_currency_code, contract_amount = _extract_bank_control_contract(text)
    payment_date, payment_amount, payment_currency_code = _extract_bank_control_payment(text)
    balance_date, calculated_balance = _extract_bank_control_balance(text)
    normalized = {
        "vendor": non_resident,
        "document_number": unique_contract_number,
        "document_date": document_date,
        "unique_contract_registration_number": unique_contract_number,
        "resident_name": resident_name,
        "non_resident_vendor": non_resident,
        "contract_number": contract_number,
        "contract_date": contract_date,
        "contract_ref": _join_parts("контракт", contract_number, f"от {contract_date}" if contract_date else ""),
        "contract_currency": contract_currency,
        "contract_currency_code": contract_currency_code,
        "contract_amount": _decimal_to_float(contract_amount),
        "currency": contract_currency_code or contract_currency,
        "total_amount": _decimal_to_float(contract_amount),
        "payment_operation_date": payment_date,
        "payment_operation_amount": _decimal_to_float(payment_amount),
        "payment_operation_currency_code": payment_currency_code,
        "balance_date": balance_date,
        "calculated_balance": _decimal_to_float(calculated_balance),
    }
    _append_missing_warnings(
        warnings,
        normalized,
        {
            "unique_contract_registration_number": "unique contract registration number",
            "document_date": "bank control statement date",
            "resident_name": "resident name",
            "non_resident_vendor": "non-resident/vendor",
            "contract_number": "contract number",
            "contract_date": "contract date",
            "contract_currency_code": "contract currency code",
            "contract_amount": "contract amount",
            "payment_operation_date": "payment operation date",
            "payment_operation_amount": "payment operation amount",
            "calculated_balance": "calculated balance",
        },
    )
    return normalized, [], warnings


def _parse_bank_transfer_application(text: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    application_number = _first_match(text, r"на перевод\s*№\s*([A-Za-zА-Яа-я0-9/-]+)")
    document_date = _parse_date(_first_match(text, r"от\s*(?:г\.)?\s*(\d{1,2}\s+[А-Яа-яA-Za-z]+\s+\d{4})"))
    execution_status, execution_time = _extract_bank_transfer_execution(text)
    debit_account = _extract_bank_transfer_debit_account(text)
    currency = _first_match(text, r"Currency Code\s*[\n ]+([A-Z]{3})") or _first_match(text, r"\b(?:Валюта|Currency)\b\s*\n\s*([A-Z]{3})")
    transfer_amount, amount_in_words = _extract_bank_transfer_amount_and_words(text)
    ordering_block = _extract_bank_transfer_field_block(text, "50", ("Банк-посредник", "Intermediary Institution", "56"))
    ordering_customer = _clean_bank_transfer_party_name(ordering_block)
    payer_address = _clean_bank_transfer_party_address(ordering_block)
    payer_country_code = _first_match(ordering_block, r"CODE\s+COUNTRY\s*:\s*([A-Z]{2})")
    payer_inn = _first_match(ordering_block, r"\bINN\s*:\s*([0-9]{8,14})")
    intermediary_bank = _extract_bank_transfer_field_block(text, "56", ("Банк получателя", "Account with Institution", "57"))
    beneficiary_bank = _extract_bank_transfer_field_block(text, "57", ("Получатель", "Account number", "59"))
    beneficiary_block = _extract_bank_transfer_field_block(text, "59", ("Назначение платежа", "Details of payment", "70"))
    beneficiary_account = _first_match(beneficiary_block, r"\b(\d{12,34})\b") or _extract_bank_transfer_beneficiary_account(text)
    beneficiary_customer = _clean_beneficiary_customer(beneficiary_block, beneficiary_account)
    beneficiary_address = _clean_bank_transfer_party_address(beneficiary_block, skip_account=beneficiary_account)
    beneficiary_country = _extract_bank_transfer_country(beneficiary_block)
    beneficiary_bank_swift_bic = _extract_bank_transfer_swift_bic(beneficiary_bank)
    beneficiary_bank_address = _extract_bank_transfer_bank_address(beneficiary_bank, swift_bic=beneficiary_bank_swift_bic)
    beneficiary_bank_country = _extract_bank_transfer_country(beneficiary_bank)
    beneficiary_bank_clearing_code = _first_match(beneficiary_bank, r"(//[A-Z]{2}[0-9A-Z]+)")
    payment_details = _extract_bank_transfer_payment_details(text)
    contract_ref = _extract_bank_transfer_contract_ref(payment_details)
    contract_number, contract_date = _extract_bank_transfer_contract_parts(contract_ref or payment_details)
    charges_mode = _extract_bank_transfer_charges_mode(text)
    sender_to_receiver_info = _extract_bank_transfer_optional_block(text, "72", ("77B", "Расходы и комиссии"))
    regulatory_reporting = _extract_bank_transfer_optional_block(text, "77B", ("Расходы и комиссии",))
    normalized = {
        "vendor": beneficiary_customer,
        "document_number": application_number,
        "document_date": document_date,
        "transfer_application_number": application_number,
        "execution_status": execution_status,
        "execution_time": execution_time,
        "debit_account": debit_account,
        "currency": currency,
        "transfer_amount": _decimal_to_float(transfer_amount),
        "total_amount": _decimal_to_float(transfer_amount),
        "amount_in_words": amount_in_words,
        "ordering_customer": ordering_customer,
        "payer_address": payer_address,
        "payer_inn": payer_inn,
        "payer_country_code": payer_country_code,
        "payer_country": payer_country_code,
        "intermediary_bank": _clean_multiline_value(intermediary_bank),
        "beneficiary_customer": beneficiary_customer,
        "beneficiary_address": beneficiary_address,
        "beneficiary_country": beneficiary_country,
        "beneficiary_account": beneficiary_account,
        "beneficiary_bank": _clean_multiline_value(beneficiary_bank),
        "beneficiary_bank_swift_bic": beneficiary_bank_swift_bic,
        "beneficiary_bank_address": beneficiary_bank_address,
        "beneficiary_bank_country": beneficiary_bank_country,
        "beneficiary_bank_clearing_code": beneficiary_bank_clearing_code,
        "payment_details": payment_details,
        "contract_ref": contract_ref,
        "contract_number": contract_number,
        "contract_date": contract_date,
        "charges_mode": charges_mode,
        "sender_to_receiver_info": sender_to_receiver_info,
        "regulatory_reporting": regulatory_reporting,
    }
    _append_missing_warnings(
        warnings,
        normalized,
        {
            "transfer_application_number": "transfer application number",
            "document_date": "transfer application date",
            "execution_status": "execution status",
            "debit_account": "debit account",
            "currency": "currency",
            "transfer_amount": "transfer amount",
            "ordering_customer": "ordering customer",
            "beneficiary_customer": "beneficiary/customer",
            "beneficiary_account": "beneficiary account",
            "beneficiary_bank": "beneficiary bank",
            "payment_details": "payment details",
            "contract_number": "contract number",
            "contract_date": "contract date",
            "charges_mode": "charges mode",
        },
    )
    return normalized, [], warnings


def _extract_bank_control_unique_contract_number(text: str) -> str:
    value = _first_match(text, r"Уникальный номер контракта\s+([\d\s/]+)\s+от")
    compact = _compact_spaced_code(value)
    return compact if re.fullmatch(r"\d{8}/\d{4}/\d{4}/\d/\d", compact) else compact


def _extract_bank_control_resident(text: str) -> str:
    value = _first_match(text, r"1\.Сведения о резиденте\s+(.+?)\s+1\.2\s+Адрес", flags=re.I | re.S)
    return _clean_multiline_value(value)


def _extract_bank_control_non_resident(text: str) -> str:
    value = _first_match(text, r"2\.Реквизиты нерезидента.*?\n\s*1\s+2\s+3\s+4\s+(.+?)\s+3\.Общие сведения", flags=re.I | re.S)
    cleaned = _clean_multiline_value(value)
    cleaned = re.sub(r"\s+[А-Яа-яЁё]+\s+\d{3}$", "", cleaned).strip()
    return cleaned


def _extract_bank_control_contract(text: str) -> tuple[str, str, str, str, Decimal | None]:
    match = re.search(
        r"\b([A-Za-zА-Яа-я0-9/-]+)\s+(\d{1,2}\.\d{1,2}\.\d{4})\s+([А-Яа-яA-Za-z]+)\s+(\d{3})\s+([\d .,\-]+)\s+\d{1,2}\.\d{1,2}\.\d{4}",
        text,
        flags=re.I,
    )
    if not match:
        return "", "", "", "", None
    number, raw_date, currency, currency_code, amount = match.groups()
    return number, _parse_date(raw_date), currency.upper(), currency_code, _parse_decimal(amount)


def _extract_bank_control_payment(text: str) -> tuple[str, Decimal | None, str]:
    match = re.search(
        r"\b(\d{1,2}\.\d{1,2}\.\s*\d\s*\d\s*\d\s*\d)\s+\d+\s+\d+\s+(\d{3})\s+([\d .,\-]+)\s+\d{3}\s+[\d .,\-]+",
        text,
        flags=re.I,
    )
    if not match:
        return "", None, ""
    raw_date, currency_code, amount = match.groups()
    return _parse_date(_compact_spaced_date(raw_date)), _parse_decimal(amount), currency_code


def _extract_bank_control_balance(text: str) -> tuple[str, Decimal | None]:
    for line in reversed(_text_to_lines(text)):
        if not re.match(r"^\d{1,2}\.\d{1,2}\.\d{4}\s+\d{3}\s+", line):
            continue
        numbers = re.findall(r"-?\d[\d .,\u00a0\u202f]*[,.]\d{2}", line)
        if not numbers:
            continue
        return _parse_date(line), _parse_decimal(numbers[-1])
    return "", None


def _extract_bank_transfer_execution(text: str) -> tuple[str, str]:
    match = re.search(r"\b(Исполнен|Отклон[её]н|Принят|В обработке)\b\s+(\d{1,2}\.\d{1,2}\.\d{4}\s+в\s+\d{1,2}:\d{2}:\d{2})", text, flags=re.I)
    if match:
        return _clean_value(match.group(1)), _normalize_bank_transfer_execution_time(match.group(2))
    status = _first_match(text[:500], r"\b(Исполнен|Отклон[её]н|Принят|В обработке)\b")
    time_value = _first_match(text[:500], r"(\d{1,2}\.\d{1,2}\.\d{4}\s+в\s+\d{1,2}:\d{2}:\d{2})")
    return status, _normalize_bank_transfer_execution_time(time_value)


def _normalize_bank_transfer_execution_time(value: Any) -> str:
    raw = _clean_value(value)
    if not raw:
        return ""
    return re.sub(r"\s+в\s+", " ", raw, flags=re.I)


def _extract_bank_transfer_debit_account(text: str) -> str:
    value = _first_match(text, r"Please debit our account with\s+you\):\s*([\d\s]+)", flags=re.I)
    digits = re.sub(r"\D+", "", value)
    return digits


def _extract_bank_transfer_amount(text: str) -> Decimal | None:
    amount, _words = _extract_bank_transfer_amount_and_words(text)
    return amount


def _extract_bank_transfer_amount_and_words(text: str) -> tuple[Decimal | None, str]:
    match = re.search(
        r"Amount of transfer\s*\(in figures and in writing\)\s*(?:32\s+)?([0-9][\d .,\u00a0\u202f]*[,.]\d{2})(.*?)(?=\n\s*(?:Отправитель|Ordering Customer|\b50\b)|$)",
        text,
        flags=re.I | re.S,
    )
    if match:
        return _parse_decimal(match.group(1)), _clean_multiline_value(match.group(2))
    match = re.search(r"\b32\s+([0-9][\d .,\u00a0\u202f]*[,.]\d{2})(.*?)(?=\n\s*(?:Отправитель|Ordering Customer|\b50\b)|$)", text, flags=re.I | re.S)
    if match:
        return _parse_decimal(match.group(1)), _clean_multiline_value(match.group(2))
    return None, ""


def _extract_bank_transfer_beneficiary_account(text: str) -> str:
    value = _first_match(text, r"Account number \(IBAN\)\s*([\d\s]+)\s+Наименование", flags=re.I)
    return re.sub(r"\D+", "", value)


def _extract_bank_transfer_contract_ref(payment_details: str) -> str:
    match = re.search(r"\b(CONTRACT\s+[A-Za-zА-Яа-я0-9/-]+\s+DD\s+\d{1,2}\.\d{1,2}\.\d{4})\b", payment_details, flags=re.I)
    if match:
        return _clean_value(match.group(1)).upper()
    match = re.search(r"\b(контракт\s*(?:№\s*)?[A-Za-zА-Яа-я0-9/-]+\s*(?:от|DD)\s*\d{1,2}\.\d{1,2}\.\d{4})\b", payment_details, flags=re.I)
    return _clean_value(match.group(1)) if match else ""


def _extract_bank_transfer_contract_parts(contract_ref: str) -> tuple[str, str]:
    match = re.search(r"(?:CONTRACT|контракт)\s*№?\s*([A-Za-zА-Яа-я0-9/-]+)\s*(?:DD|от)\s*(\d{1,2}\.\d{1,2}\.\d{4})", contract_ref, flags=re.I)
    if not match:
        return "", ""
    return _clean_value(match.group(1)), _parse_date(match.group(2))


def _extract_bank_transfer_charges_mode(text: str) -> str:
    match = re.search(
        r"Расходы и комиссии по переводу.*?(?=Продленный операционный день|Расходы и комиссии по переводу списать)",
        text,
        flags=re.I | re.S,
    )
    haystack = match.group(0) if match else text
    if re.search(r"\bOUR\b", haystack):
        return "OUR"
    if re.search(r"\bSHA\b", haystack):
        return "SHA"
    if re.search(r"\bBEN\b", haystack):
        return "BEN"
    return ""


def _clean_beneficiary_customer(block: str, account: str) -> str:
    for line in _bank_transfer_content_lines(block):
        if account and re.sub(r"\D+", "", line) == account:
            continue
        return _clean_value(re.sub(r"^\s*59\s+", "", line))
    return ""


def _extract_block_between(text: str, start_pattern: str, end_pattern: str) -> str:
    match = re.search(start_pattern + r"\s*(.*?)\s*" + end_pattern, text, flags=re.I | re.S)
    return match.group(1) if match else ""


def _extract_bank_transfer_field_block(text: str, tag: str, next_tags: tuple[str, ...]) -> str:
    tag_pattern = rf"(?:^|\n)\s*{re.escape(tag)}\s*(?:\n|\s+)"
    next_patterns = [rf"(?:^|\n|\s){re.escape(next_tag)}\b" for next_tag in next_tags if re.fullmatch(r"\d{2}[A-Z]?", next_tag)]
    label_patterns = [re.escape(next_tag) for next_tag in next_tags if not re.fullmatch(r"\d{2}[A-Z]?", next_tag)]
    stop_pattern = "|".join(next_patterns + label_patterns) or r"$"
    match = re.search(tag_pattern + r"(.*?)(?=" + stop_pattern + r"|$)", text, flags=re.I | re.S)
    return match.group(1) if match else ""


def _extract_bank_transfer_payment_details(text: str) -> str:
    block = _extract_bank_transfer_optional_block(text, "70", ("72", "77B", "Расходы и комиссии"))
    if block:
        return _clean_multiline_value(block)
    return _first_match(text, r"Details of payment\s*70\s*([^\n\r]+)")


def _extract_bank_transfer_optional_block(text: str, tag: str, stop_markers: tuple[str, ...]) -> str:
    start = rf"(?:^|\n).*?\b{re.escape(tag)}\b\s*"
    stop_parts = [
        rf"(?:^|\n).*?\b{re.escape(marker)}\b" if re.fullmatch(r"\d{2}[A-Z]?", marker) else re.escape(marker)
        for marker in stop_markers
    ]
    stop = "|".join(stop_parts) or r"$"
    match = re.search(start + r"(.*?)(?=" + stop + r"|$)", text, flags=re.I | re.S)
    return _clean_multiline_value(match.group(1)) if match else ""


def _clean_bank_transfer_party_name(block: str) -> str:
    lines = _bank_transfer_content_lines(block)
    return _clean_value(lines[0] if lines else "")


def _clean_bank_transfer_party_address(block: str, *, skip_account: str = "") -> str:
    lines = _bank_transfer_content_lines(block)
    if skip_account:
        lines = [line for line in lines if re.sub(r"\D+", "", line) != skip_account]
    address_lines = []
    for line in lines[1:]:
        if re.search(r"\b(?:CODE\s+COUNTRY|INN|КПП|ОКПО|ОГРН)\b", line, flags=re.I):
            continue
        if re.fullmatch(r"[A-Z]{2}", line):
            continue
        address_lines.append(line)
    return _clean_value(" ".join(address_lines))


def _bank_transfer_content_lines(block: str) -> list[str]:
    lines = _text_to_lines(block)
    result: list[str] = []
    for line in lines:
        cleaned = _clean_value(line)
        if not cleaned or re.fullmatch(r"\d{1,3}[A-Z]?", cleaned):
            continue
        result.append(cleaned)
    return result


def _extract_bank_transfer_swift_bic(block: str) -> str:
    matches = re.findall(r"\b([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b", block)
    if not matches:
        return ""
    preferred = [item for item in matches if len(item) == 11 or item.endswith("XXX")]
    return (preferred or matches)[-1]


def _extract_bank_transfer_country(block: str) -> str:
    lines = _bank_transfer_content_lines(block)
    for line in reversed(lines):
        if re.fullmatch(r"[A-Z]{2}", line):
            return line
    code = _first_match(block, r"CODE\s+COUNTRY\s*:\s*([A-Z]{2})")
    return code


def _extract_bank_transfer_bank_address(block: str, *, swift_bic: str) -> str:
    lines = _bank_transfer_content_lines(block)
    address_lines: list[str] = []
    swift_seen = False
    for line in lines:
        if swift_bic and swift_bic in line:
            swift_seen = True
            continue
        if not swift_seen:
            continue
        if re.fullmatch(r"[A-Z]{2}", line):
            continue
        address_lines.append(line)
    return _clean_value(" ".join(address_lines))


def _append_missing_warnings(warnings: list[str], normalized: Mapping[str, Any], labels_by_field: Mapping[str, str]) -> None:
    missing = [label for field, label in labels_by_field.items() if not normalized.get(field)]
    if missing:
        warnings.append("Parser needs review: missing " + ", ".join(missing))


def apply_supplier_order_document_match(document: Mapping[str, Any], shipment: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(document)
    document_type = str(payload.get("document_type") or "").strip()
    if document_type not in ORDER_MATCH_DOCUMENT_TYPES:
        return payload
    normalized = dict(payload.get("normalized_parse") or {})
    match = verify_supplier_order_document_match(payload, shipment)
    normalized.update(match)
    payload["normalized_parse"] = normalized
    payload.update(match)
    warnings = _dedupe_strings([*_string_list(payload.get("warnings")), *_string_list(match.get("order_match_warnings"))])
    payload["warnings"] = warnings
    if (
        match.get("order_match_status") in {ORDER_MATCH_STATUS_NEEDS_REVIEW, ORDER_MATCH_STATUS_MISMATCH}
        and str(payload.get("parse_status") or "") == FINANCIAL_DOCUMENT_PARSE_STATUS_PARSED
    ):
        payload["parse_status"] = FINANCIAL_DOCUMENT_PARSE_STATUS_NEEDS_REVIEW
    return payload


def _supplier_order_shipment_with_linked_contract(
    runtime: RegistryUploadDbBackedRuntime,
    shipment: Mapping[str, Any],
) -> dict[str, Any]:
    payload = dict(shipment)
    source = payload.get("header") if isinstance(payload.get("header"), Mapping) else payload
    header = dict(source)
    invoice_document_id = str(header.get("invoice_document_id") or "").strip()
    if not invoice_document_id:
        return payload
    link = runtime.load_invoice_contract_link(invoice_document_id)
    if link is None:
        return payload
    contract = runtime.load_trade_document(str(link.get("contract_document_id") or ""))
    if contract is None or str(contract.get("status") or "") != TRADE_DOCUMENT_STATUS_ACTIVE:
        return payload
    header["contract_document_id"] = str(contract.get("document_id") or "")
    header["contract_no"] = str(contract.get("number") or header.get("contract_no") or "")
    header["contract_date"] = str(contract.get("document_date") or header.get("contract_date") or "")
    if isinstance(payload.get("header"), Mapping):
        payload["header"] = header
    else:
        payload.update(header)
    return payload


def verify_supplier_order_document_match(document: Mapping[str, Any], shipment: Mapping[str, Any]) -> dict[str, Any]:
    document_type = str(document.get("document_type") or "").strip()
    normalized = dict(document.get("normalized_parse") or {})
    order_identity = _supplier_order_identity(shipment)
    document_identity = _supplier_order_document_identity(document)
    reasons: list[str] = []
    warnings: list[str] = []
    order_contract_number = str(order_identity.get("contract_number") or "")
    order_contract_date = str(order_identity.get("contract_date") or "")
    document_contract_number = str(document_identity.get("contract_number") or "")
    document_contract_date = str(document_identity.get("contract_date") or "")
    number_matches = bool(order_contract_number and document_contract_number and _contract_number_key(order_contract_number) == _contract_number_key(document_contract_number))
    date_matches = bool(order_contract_date and document_contract_date and order_contract_date == document_contract_date)
    status = ORDER_MATCH_STATUS_NEEDS_REVIEW

    if document_type not in ORDER_MATCH_DOCUMENT_TYPES:
        return {
            "order_match_status": "",
            "order_match_reasons": [],
            "order_match_warnings": [],
            "matched_contract_number": "",
            "matched_contract_date": "",
        }

    if not order_contract_number:
        warnings.append("Не удалось проверить соответствие заказу: в заказе не указан номер контракта.")
    if not document_contract_number:
        warnings.append("Не удалось проверить соответствие заказу: в документе не распознан номер контракта.")

    if order_contract_number and document_contract_number and not number_matches:
        status = ORDER_MATCH_STATUS_MISMATCH
        warnings.append(
            "Документ, вероятно, относится к другому заказу: "
            f"контракт документа {document_contract_number}, контракт заказа {order_contract_number}."
        )
    elif number_matches and order_contract_date and document_contract_date and not date_matches:
        status = ORDER_MATCH_STATUS_MISMATCH
        warnings.append(
            "Документ, вероятно, относится к другому заказу: "
            f"дата контракта документа {document_contract_date}, дата контракта заказа {order_contract_date}."
        )
    elif number_matches and date_matches:
        status = ORDER_MATCH_STATUS_MATCHED
        reasons.append(f"Контракт совпадает: {order_contract_number} от {order_contract_date}.")
    elif number_matches:
        status = ORDER_MATCH_STATUS_PROBABLE_MATCH
        reasons.append(f"Номер контракта совпадает: {order_contract_number}.")
    elif warnings:
        status = ORDER_MATCH_STATUS_NEEDS_REVIEW

    amount_reason = _supplier_order_amount_match_reason(order_identity, document_identity)
    if amount_reason:
        reasons.append(amount_reason)
    vendor_reason = _supplier_order_vendor_match_reason(order_identity, document_identity)
    if vendor_reason:
        reasons.append(vendor_reason)

    if status == ORDER_MATCH_STATUS_NEEDS_REVIEW and not warnings:
        warnings.append("Не удалось проверить соответствие заказу: не распознаны сильные признаки контракта.")

    matched_contract_number = order_contract_number if number_matches else ""
    matched_contract_date = order_contract_date if date_matches else ""
    if normalized.get("unique_contract_registration_number"):
        reasons.append("УНК распознан: " + str(normalized.get("unique_contract_registration_number")))
    return {
        "order_match_status": status,
        "order_match_reasons": _dedupe_strings(reasons),
        "order_match_warnings": _dedupe_strings(warnings),
        "matched_contract_number": matched_contract_number,
        "matched_contract_date": matched_contract_date,
    }


def _supplier_order_identity(shipment: Mapping[str, Any]) -> dict[str, Any]:
    source = shipment.get("header") if isinstance(shipment.get("header"), Mapping) else shipment
    metadata = source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {}
    return {
        "invoice_no": str(source.get("invoice_no") or metadata.get("invoice_no") or ""),
        "invoice_date": _optional_iso_date(source.get("invoice_date") or metadata.get("invoice_date")),
        "invoice_amount": _parse_decimal(source.get("invoice_amount_total") if source.get("invoice_amount_total") is not None else metadata.get("declared_invoice_total")),
        "currency": str(source.get("currency") or metadata.get("currency") or "").strip().upper(),
        "contract_number": str(source.get("contract_no") or metadata.get("contract_no") or ""),
        "contract_date": _optional_iso_date(source.get("contract_date") or metadata.get("contract_date")),
        "supplier": str(source.get("supplier_name") or metadata.get("supplier_name") or ""),
    }


def _supplier_order_document_identity(document: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(document.get("normalized_parse") or {})
    contract_number = str(normalized.get("contract_number") or "")
    contract_date = _optional_iso_date(normalized.get("contract_date"))
    if not contract_number or not contract_date:
        extracted_number, extracted_date = _contract_parts_from_ref(
            str(normalized.get("contract_ref") or document.get("contract_ref") or normalized.get("payment_details") or normalized.get("contract") or "")
        )
        contract_number = contract_number or extracted_number
        contract_date = contract_date or extracted_date
    amount = (
        _parse_decimal(normalized.get("transfer_amount"))
        or _parse_decimal(normalized.get("payment_operation_amount"))
        or _parse_decimal(normalized.get("contract_amount"))
        or _parse_decimal(normalized.get("total_amount"))
        or _parse_decimal(document.get("total_amount"))
    )
    vendor = str(
        normalized.get("beneficiary_customer")
        or normalized.get("non_resident_vendor")
        or normalized.get("vendor")
        or document.get("vendor")
        or ""
    )
    return {
        "contract_number": contract_number,
        "contract_date": contract_date,
        "contract_ref": str(normalized.get("contract_ref") or document.get("contract_ref") or ""),
        "amount": amount,
        "currency": str(normalized.get("currency") or document.get("currency") or normalized.get("payment_operation_currency_code") or normalized.get("contract_currency_code") or "").strip().upper(),
        "vendor": vendor,
    }


def _contract_parts_from_ref(value: str) -> tuple[str, str]:
    match = re.search(r"(?:CONTRACT|контракт|договор)\s*№?\s*([A-Za-zА-Яа-я0-9/-]+)\s*(?:DD|от)?\s*(\d{1,2}\.\d{1,2}\.\d{4})?", value, flags=re.I)
    if not match:
        return "", ""
    return _clean_value(match.group(1)), _parse_date(match.group(2) or "")


def _contract_number_key(value: str) -> str:
    return re.sub(r"[^0-9A-ZА-Я/.-]+", "", str(value or "").upper())


def _supplier_order_amount_match_reason(order_identity: Mapping[str, Any], document_identity: Mapping[str, Any]) -> str:
    order_amount = _parse_decimal(order_identity.get("invoice_amount"))
    document_amount = _parse_decimal(document_identity.get("amount"))
    if order_amount is None or document_amount is None or order_amount <= 0 or document_amount <= 0:
        return ""
    order_currency = str(order_identity.get("currency") or "").upper()
    document_currency = str(document_identity.get("currency") or "").upper()
    if order_currency and document_currency and order_currency != document_currency:
        return ""
    tolerance = max(Decimal("0.01"), order_amount.copy_abs() * Decimal("0.001"))
    if abs(order_amount - document_amount) <= tolerance:
        return "Сумма и валюта совпадают с invoice."
    return ""


def _supplier_order_vendor_match_reason(order_identity: Mapping[str, Any], document_identity: Mapping[str, Any]) -> str:
    order_vendor = _party_key(str(order_identity.get("supplier") or ""))
    document_vendor = _party_key(str(document_identity.get("vendor") or ""))
    if not order_vendor or not document_vendor:
        return ""
    if order_vendor in document_vendor or document_vendor in order_vendor:
        return "Контрагент совпадает по названию."
    return ""


def _party_key(value: str) -> str:
    return re.sub(r"[^0-9A-ZА-Я]+", "", str(value or "").upper())


def _compact_spaced_code(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _compact_spaced_date(value: Any) -> str:
    return re.sub(r"(?<=\d)\s+(?=\d)", "", str(value or ""))


def _clean_multiline_value(value: Any) -> str:
    lines = [line for line in _text_to_lines(str(value or "")) if not re.fullmatch(r"\d{1,3}", line)]
    return _clean_value(" ".join(lines))


def _join_parts(*values: Any) -> str:
    return _clean_value(" ".join(str(value or "").strip() for value in values if str(value or "").strip()))


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
    labeled = _extract_quote_labeled_cost_rows(text)
    for key, labeled_value in labeled.items():
        current_value = _parse_decimal(values.get(key))
        if labeled_value is not None and current_value is None:
            values[key] = labeled_value
    total = _extract_quote_total_usd(text)
    numbered = _extract_quote_numbered_cost_column(text, total)
    numbered_is_usable = _quote_numbered_amounts_are_usable(numbered, total)
    for number, key in QUOTE_AMOUNT_CATEGORY_BY_ROW.items():
        numbered_value = numbered.get(number)
        current_value = _parse_decimal(values.get(key))
        should_replace = current_value is None
        if key in QUOTE_REQUIRED_AMOUNT_CATEGORIES and total is not None and total > 0 and current_value == 0:
            should_replace = True
        if key in QUOTE_CORE_AMOUNT_CATEGORIES and numbered_is_usable and numbered_value is not None and current_value != numbered_value:
            should_replace = True
        if numbered_value is not None and should_replace:
            values[key] = numbered_value
    values[EXPENSE_CATEGORY_PERMISSION_DOCS] = values.get(EXPENSE_CATEGORY_PERMISSION_DOCS)
    if values[EXPENSE_CATEGORY_PERMISSION_DOCS] is None:
        values[EXPENSE_CATEGORY_PERMISSION_DOCS] = _parse_decimal(
            _first_match(text, r"Оформление разрешительной документации\s+([\d .,]+)")
        )
    values[EXPENSE_CATEGORY_PACKAGING] = _parse_decimal(_first_match(text, r"Стоимость дополнительной упаковки\s+([\d .,]+)\s*USD"))
    values[EXPENSE_CATEGORY_EXPORT_DOCS] = _extract_quote_export_docs_range(text)
    if total is not None:
        values["total_quote_usd"] = total
    return values


def _extract_quote_total_usd(text: str) -> Decimal | None:
    explicit_totals: list[Decimal] = []
    for match in re.finditer(r"ИТОГО:\s*(?:\n|\s)*([\d .,]+)\s*USD", text, flags=re.I):
        value = _parse_decimal(match.group(1))
        if value is not None and value > 0:
            explicit_totals.append(value)
    if explicit_totals:
        return max(explicit_totals)
    commercial_index = text.find("Коммерческое предложение")
    top = text[:commercial_index] if commercial_index > 0 else text[:900]
    candidates: list[Decimal] = []
    for match in re.finditer(r"(?m)^\s*([\d .,]+)\s*USD\s*$", top):
        value = _parse_decimal(match.group(1))
        if value is not None and value > 0:
            candidates.append(value)
    return max(candidates) if candidates else None


def _extract_quote_numbered_cost_column(text: str, total_quote_usd: Decimal | None) -> dict[int, Decimal]:
    regions = _quote_amount_regions(text)
    for region in regions:
        direct = _extract_numbered_amount_pairs(region)
        if _quote_numbered_amounts_are_usable(direct, total_quote_usd):
            return direct
    for region in regions:
        sequence = _extract_amount_sequence_by_total(region, total_quote_usd)
        if _quote_numbered_amounts_are_usable(sequence, total_quote_usd):
            return sequence
    best: dict[int, Decimal] = {}
    for region in regions:
        direct = _extract_numbered_amount_pairs(region)
        if len(direct) > len(best):
            best = direct
    return best


def _quote_amount_regions(text: str) -> list[str]:
    regions: list[str] = []
    commercial_index = text.find("Коммерческое предложение")
    if commercial_index > 0:
        regions.append(text[:commercial_index])
    start = text.find("Предварительный расчет стоимости")
    if start < 0:
        start = text.find("Предварительный расч")
    if start >= 0:
        end = text.find("Дополнительные услуги", start)
        if end < 0:
            end = start + 2000
        regions.append(text[start:end])
    regions.append(text[:1200])
    deduped: list[str] = []
    for region in regions:
        cleaned = str(region or "").strip()
        if cleaned and cleaned not in deduped:
            deduped.append(cleaned)
    return deduped


def _extract_numbered_amount_pairs(text: str) -> dict[int, Decimal]:
    values: dict[int, Decimal] = {}
    lines = _text_to_lines(text)
    for index, line in enumerate(lines):
        match = re.match(r"^([1-6])[\s.)]+([0-9][\d .,\u00a0\u202f]*)(?:\s*USD)?$", line, flags=re.I)
        if match:
            amount = _parse_decimal(match.group(2))
            if amount is not None:
                values[int(match.group(1))] = amount
                continue
        table_row = re.match(
            r"^([1-6])\s+(?=.*[A-Za-zА-Яа-я]).+?\s+([0-9][\d .,\u00a0\u202f]*)(?:\s*USD)?$",
            line,
            flags=re.I,
        )
        if table_row:
            amount = _parse_decimal(table_row.group(2))
            if amount is not None:
                values[int(table_row.group(1))] = amount
                continue
        number_only = re.match(r"^([1-6])$", line)
        if not number_only or index + 1 >= len(lines):
            continue
        next_amount = _parse_decimal(lines[index + 1])
        if next_amount is None:
            continue
        if next_amount <= 6 and index + 2 < len(lines) and re.match(r"^[1-6]$", lines[index + 1]):
            continue
        values[int(number_only.group(1))] = next_amount
    return values


def _extract_amount_sequence_by_total(text: str, total_quote_usd: Decimal | None) -> dict[int, Decimal]:
    if total_quote_usd is None or total_quote_usd <= 0:
        return {}
    numbers: list[Decimal] = []
    for line in _text_to_lines(text):
        if re.search(r"[A-Za-zА-Яа-я]", line) and "USD" not in line.upper():
            continue
        if not re.match(r"^[\d .,\u00a0\u202f]+(?:\s*USD)?$", line, flags=re.I):
            continue
        value = _parse_decimal(line)
        if value is None:
            continue
        numbers.append(value)
    for index in range(0, max(0, len(numbers) - 5)):
        window = numbers[index : index + 6]
        if len(window) < 6:
            continue
        if abs(sum(window, Decimal("0")) - total_quote_usd) <= Decimal("0.01"):
            return {number: window[number - 1] for number in range(1, 7)}
    return {}


def _quote_numbered_amounts_are_usable(values: Mapping[int, Decimal], total_quote_usd: Decimal | None) -> bool:
    if not all(number in values for number in range(1, 7)):
        return False
    if values.get(1) is None or values.get(2) is None:
        return False
    if total_quote_usd is None:
        return True
    amount_sum = sum((values.get(number) or Decimal("0")) for number in range(1, 7))
    return abs(amount_sum - total_quote_usd) <= Decimal("0.01")


def _extract_quote_labeled_cost_rows(text: str) -> dict[str, Decimal]:
    values: dict[str, Decimal] = {}
    for line in _text_to_lines(text):
        for category, labels in QUOTE_AMOUNT_LABELS:
            if category in values:
                continue
            amount = _extract_amount_after_any_label(line, labels)
            if amount is not None:
                values[category] = amount
                break
    return values


def _extract_amount_after_any_label(line: str, labels: tuple[str, ...]) -> Decimal | None:
    cleaned = _clean_value(line)
    for label in labels:
        match = re.search(re.escape(label), cleaned, flags=re.I)
        if not match:
            continue
        tail = cleaned[match.end() :]
        numbers = re.findall(r"\d[\d \u00a0\u202f]*(?:[,.]\d+)?", tail)
        if not numbers:
            return None
        return _parse_decimal(numbers[-1])
    return None


def _extract_quote_export_docs_range(text: str) -> str | None:
    patterns = (
        r"Стоимость оформления экспортных документов\s+([\d .,]+)\s*[-–]\s*([\d .,]+)\s*USD",
        r"([\d .,]+)\s*[-–]\s*([\d .,]+)\s*USD[^\n\r]{0,220}\n\s*(?:\d+\s+)?Стоимость оформления экспортных документов",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue
        left = _parse_decimal(match.group(1))
        right = _parse_decimal(match.group(2))
        if left is not None and right is not None:
            return f"{_decimal_to_display(left)}-{_decimal_to_display(right)}"
    return None


def _missing_required_quote_amounts(
    amounts: Mapping[str, Decimal | str | None],
    total_quote_usd: Decimal | None,
) -> list[str]:
    missing: list[str] = []
    for key in QUOTE_REQUIRED_AMOUNT_CATEGORIES:
        value = _parse_decimal(amounts.get(key))
        if value is None:
            missing.append(key)
            continue
        if total_quote_usd is not None and total_quote_usd > 0 and value <= 0:
            missing.append(key)
    return missing


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


def _extract_customs_item_weights(text: str) -> tuple[Decimal | None, Decimal | None, int]:
    lines = _text_to_lines(text)
    gross_values: list[Decimal] = []
    net_values: list[Decimal] = []
    for index, line in enumerate(lines):
        if not re.match(r"^\d{1,3}\s+\d{10}\s+", line):
            continue
        gross_weight: Decimal | None = None
        net_weight: Decimal | None = None
        for nearby in lines[index + 1 : index + 7]:
            if gross_weight is None:
                gross_match = re.match(r"^CN\s+([0-9][\d .,]*)\s+", nearby, flags=re.I)
                if gross_match:
                    gross_weight = _parse_decimal(gross_match.group(1))
                    continue
            if net_weight is None:
                net_match = re.match(r"^\d{4}\s+\d{3}\s+([0-9][\d .,]*)\s*$", nearby)
                if net_match:
                    net_weight = _parse_decimal(net_match.group(1))
        if gross_weight is not None:
            gross_values.append(gross_weight)
        if net_weight is not None:
            net_values.append(net_weight)
    return _sum_decimal(gross_values), _sum_decimal(net_values), len(gross_values)


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


def _estimate_bank_rate_on_quote_date(
    *,
    quote_doc: Mapping[str, Any],
    invoice_docs: list[Mapping[str, Any]],
    invoice_fact_rub: Decimal | None,
    linked_quote_usd_component: Decimal | None,
    quote_base_status: str = "",
) -> Decimal | None:
    if quote_doc and invoice_docs and quote_base_status and quote_base_status != "parsed":
        return None
    if not quote_doc or not invoice_docs or not invoice_fact_rub or not linked_quote_usd_component:
        return None
    if linked_quote_usd_component == 0:
        return None
    invoice_doc = _invoice_doc_for_rate(invoice_docs)
    cbr_invoice_rate = _parse_decimal(invoice_doc.get("cbr_usd_rate_value"))
    quote_cbr_rate = _parse_decimal(quote_doc.get("cbr_usd_rate_value"))
    if cbr_invoice_rate is None or quote_cbr_rate is None:
        return None
    implied_rate = _safe_div(invoice_fact_rub, linked_quote_usd_component)
    spread_pct = _safe_div(implied_rate, cbr_invoice_rate)
    return quote_cbr_rate * spread_pct if spread_pct is not None else None


def _build_rate_summary(
    *,
    quote_doc: Mapping[str, Any],
    invoice_docs: list[Mapping[str, Any]],
    invoice_fact_rub: Decimal | None,
    linked_quote_usd_component: Decimal | None,
    quote_base_status: str = "",
) -> dict[str, Any]:
    warnings: list[str] = []
    if quote_doc and invoice_docs and quote_base_status and quote_base_status != "parsed":
        return {
            "status": "needs_review",
            "quote_base_status": quote_base_status,
            "warnings": ["Linked quote USD component is incomplete; rate comparison is not calculated"],
        }
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
    if (
        normalized.get("document_type") == FINANCIAL_DOCUMENT_TYPE_LOGISTICS_QUOTE
        and normalized.get("quote_required_amounts_complete") is False
    ):
        missing = _string_list(normalized.get("quote_missing_required_amounts"))
        if missing:
            warning = "Quote parser did not find required amount(s): " + ", ".join(missing)
            if warning not in warnings:
                warnings.append(warning)
        return FINANCIAL_DOCUMENT_PARSE_STATUS_NEEDS_REVIEW
    required_by_type = {
        FINANCIAL_DOCUMENT_TYPE_LOGISTICS_QUOTE: ["quote_date", "total_amount"],
        FINANCIAL_DOCUMENT_TYPE_LOGISTICS_INVOICE: ["invoice_number", "invoice_date", "amount_rub"],
        FINANCIAL_DOCUMENT_TYPE_CUSTOMS_DECLARATION: ["declaration_number", "total_customs_payments_rub"],
        FINANCIAL_DOCUMENT_TYPE_BANK_CONTROL_STATEMENT: [
            "unique_contract_registration_number",
            "document_date",
            "resident_name",
            "non_resident_vendor",
            "contract_number",
            "contract_date",
        ],
        FINANCIAL_DOCUMENT_TYPE_BANK_TRANSFER_APPLICATION: [
            "transfer_application_number",
            "document_date",
            "currency",
            "transfer_amount",
            "ordering_customer",
            "beneficiary_customer",
            "beneficiary_account",
            "payment_details",
            "contract_number",
            "contract_date",
        ],
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


def _percent(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    divided = _safe_div(numerator, denominator)
    return divided * Decimal("100") if divided is not None else None


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


def _sum_required(*values: Any) -> Decimal | None:
    parsed_values: list[Decimal] = []
    for value in values:
        parsed = _parse_decimal(value)
        if parsed is None:
            return None
        parsed_values.append(parsed)
    return sum(parsed_values, Decimal("0"))


def _positive_decimal(value: Any) -> Decimal | None:
    parsed = _parse_decimal(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


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


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


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
