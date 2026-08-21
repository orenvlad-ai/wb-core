"""Fail-closed text-layer parser for two Russian payment-order layouts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import unicodedata
from typing import Any, Callable, Mapping

from packages.contracts.russian_payment_orders import (
    RUSSIAN_PAYMENT_ORDER_ADAPTER_VTB,
    RUSSIAN_PAYMENT_ORDER_ADAPTER_WB_BANK,
    RUSSIAN_PAYMENT_ORDER_CURRENCY,
    RUSSIAN_PAYMENT_ORDER_EXECUTION_EXECUTED,
    RUSSIAN_PAYMENT_ORDER_EXECUTION_NOT_EXECUTED,
    RUSSIAN_PAYMENT_ORDER_EXECUTION_UNCLEAR,
    RUSSIAN_PAYMENT_ORDER_FINGERPRINT_VERSION,
    RUSSIAN_PAYMENT_ORDER_FORM_CODE,
    RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_NEEDS_REVIEW,
    RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_PARSED,
    RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_PARSE_ERROR,
    RUSSIAN_PAYMENT_ORDER_PARSER_VERSION,
    RUSSIAN_PAYMENT_ORDER_SOURCE_VTB,
    RUSSIAN_PAYMENT_ORDER_SOURCE_WB_BANK,
    RUSSIAN_PAYMENT_ORDER_VAT_NOT_TAXED,
    RUSSIAN_PAYMENT_ORDER_VAT_TAXED,
    RUSSIAN_PAYMENT_ORDER_VAT_UNSPECIFIED,
    RussianPaymentOrderParseResult,
)


PdfTextExtractor = Callable[[bytes, str], tuple[str, dict[str, Any], list[str]]]

_DOTTED_DATE_RE = r"\d{2}\.\d{2}\.\d{4}"
_RUSSIAN_DATE_RE = (
    r"\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|"
    r"сентября|октября|ноября|декабря)\s+\d{4}(?:\s*г\.?)?"
)
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ACCOUNT_RE = re.compile(r"(?<!\d)(\d{20})(?!\d)")
_BIC_RE = re.compile(r"(?<!\d)(\d{9})(?!\d)")
_INN_RE = re.compile(r"ИНН\s*[:№]?\s*(\d{10}|\d{12})(?!\d)", re.IGNORECASE)
_KPP_RE = re.compile(r"КПП\s*[:№]?\s*(\d{1,9})(?!\d)", re.IGNORECASE)


@dataclass(frozen=True)
class _PaymentOrderAdapter:
    source_bank: str
    adapter: str
    bank_identity: re.Pattern[str]
    canonical_bank_name: str


_WB_BANK_ADAPTER = _PaymentOrderAdapter(
    source_bank=RUSSIAN_PAYMENT_ORDER_SOURCE_WB_BANK,
    adapter=RUSSIAN_PAYMENT_ORDER_ADAPTER_WB_BANK,
    bank_identity=re.compile(r"\bВБ\s+Банк\b", re.IGNORECASE),
    canonical_bank_name='ООО "ВБ Банк"',
)
_VTB_ADAPTER = _PaymentOrderAdapter(
    source_bank=RUSSIAN_PAYMENT_ORDER_SOURCE_VTB,
    adapter=RUSSIAN_PAYMENT_ORDER_ADAPTER_VTB,
    bank_identity=re.compile(r"\b(?:Банк|Банка)\s+ВТБ\b", re.IGNORECASE),
    canonical_bank_name="Банк ВТБ (ПАО)",
)
_ADAPTERS = (_WB_BANK_ADAPTER, _VTB_ADAPTER)

_MONTHS = {
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

_ENTITY_CONTROL_MARKERS = (
    "инн",
    "кпп",
    "сумма",
    "сч. №",
    "сч №",
    "бик",
    "вид оп.",
    "наз. пл.",
    "срок плат.",
    "очер. плат.",
    "рез. поле",
    "код",
)

_NON_BLOCKING_WARNINGS = {
    "invoice reference date is not present",
}


def parse_russian_payment_order_pdf(
    file_bytes: bytes,
    *,
    filename: str = "payment-order.pdf",
    text_extractor: PdfTextExtractor | None = None,
) -> RussianPaymentOrderParseResult:
    """Parse one PDF without OCR and bind the result to its exact byte digest."""

    file_sha256 = "sha256:" + hashlib.sha256(file_bytes).hexdigest()
    extractor = text_extractor or _repo_pdf_text_extractor
    try:
        text, diagnostics, extraction_warnings = extractor(file_bytes, filename)
    except Exception:
        result = _empty_result(file_sha256=file_sha256)
        result["parse_status"] = RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_PARSE_ERROR
        result["errors"] = ["payment-order text-layer extraction failed"]
        return result
    return parse_russian_payment_order_text(
        text,
        file_sha256=file_sha256,
        extraction_diagnostics=diagnostics,
        extraction_warnings=extraction_warnings,
    )


def parse_russian_payment_order_text(
    text: str,
    *,
    file_sha256: str,
    extraction_diagnostics: Mapping[str, Any] | None = None,
    extraction_warnings: list[str] | None = None,
) -> RussianPaymentOrderParseResult:
    """Parse an already extracted text layer using the normalized contract."""

    normalized_text = _normalize_text(text)
    result = _empty_result(file_sha256=file_sha256)
    result["extraction"] = _sanitize_extraction(extraction_diagnostics or {})
    warnings = list(extraction_warnings or [])
    errors: list[str] = []

    if not _SHA256_RE.fullmatch(file_sha256):
        errors.append("file_sha256 must be one exact prefixed SHA-256 digest")
    if len(normalized_text) < 40:
        errors.append("payment-order parser found no readable text layer")
        return _finish_result(result, warnings=warnings, errors=errors, parse_error=True)
    if RUSSIAN_PAYMENT_ORDER_FORM_CODE not in normalized_text:
        errors.append("unsupported payment-order form: expected 0401060")
        return _finish_result(result, warnings=warnings, errors=errors, parse_error=True)

    adapter = _detect_adapter(normalized_text)
    if adapter is None:
        errors.append("unsupported or ambiguous payer-bank payment-order adapter")
        return _finish_result(result, warnings=warnings, errors=errors, parse_error=True)

    if adapter is _WB_BANK_ADAPTER:
        _parse_wb_bank_adapter(normalized_text, result, warnings)
    else:
        _parse_vtb_adapter(normalized_text, result, warnings)

    _validate_result(result, warnings)
    result["payment_fingerprint"] = _payment_fingerprint(result)
    return _finish_result(result, warnings=warnings, errors=errors)


def _parse_wb_bank_adapter(
    text: str,
    result: RussianPaymentOrderParseResult,
    warnings: list[str],
) -> None:
    _parse_0401060_adapter(text, result, warnings, adapter=_WB_BANK_ADAPTER)


def _parse_vtb_adapter(
    text: str,
    result: RussianPaymentOrderParseResult,
    warnings: list[str],
) -> None:
    _parse_0401060_adapter(text, result, warnings, adapter=_VTB_ADAPTER)


def _parse_0401060_adapter(
    text: str,
    result: RussianPaymentOrderParseResult,
    warnings: list[str],
    *,
    adapter: _PaymentOrderAdapter,
) -> None:
    result["form_code"] = RUSSIAN_PAYMENT_ORDER_FORM_CODE
    result["source_bank"] = adapter.source_bank
    result["adapter"] = adapter.adapter

    order_numbers = _distinct_matches(
        re.compile(r"ПЛАТ[ЕЁ]ЖНОЕ\s+ПОРУЧЕНИЕ\s*№\s*([A-ZА-Я0-9./_-]+)", re.IGNORECASE),
        text,
    )
    if len(order_numbers) == 1:
        result["payment_order_number"] = order_numbers[0]
    elif len(order_numbers) > 1:
        warnings.append("payment order number is ambiguous")

    title_match = re.search(
        rf"ПЛАТ[ЕЁ]ЖНОЕ\s+ПОРУЧЕНИЕ\s*№\s*[A-ZА-Я0-9./_-]+\s+({_DOTTED_DATE_RE})",
        text,
        flags=re.IGNORECASE,
    )
    if title_match:
        result["document_date"] = _iso_date(title_match.group(1))

    title_position = text.casefold().find("платежное поручение")
    if title_position < 0:
        title_position = text.casefold().find("платёжное поручение")
    leading_dates = re.findall(_DOTTED_DATE_RE, text[: max(title_position, 0)])
    if len(leading_dates) >= 2:
        result["debit_date"] = _iso_date(leading_dates[1])
    else:
        warnings.append("debit date was not recognized")

    amount = _extract_amount(text)
    if amount is not None:
        result["amount"] = _decimal_text(amount)
    first_inn_position = text.casefold().find("инн")
    amount_words_region = text[:first_inn_position] if first_inn_position >= 0 else text
    if re.search(r"\bруб(?:ль|ля|лей|\.)?\b", amount_words_region, re.IGNORECASE):
        result["currency"] = RUSSIAN_PAYMENT_ORDER_CURRENCY

    execution_status, executed_at = _extract_execution(text)
    result["execution_status"] = execution_status
    result["executed_at"] = executed_at
    if executed_at:
        result["execution_date"] = executed_at[:10]

    inns = [match.group(1) for match in _INN_RE.finditer(text)]
    kpp_matches = list(_KPP_RE.finditer(text))
    kpps = [match.group(1) for match in kpp_matches]
    accounts = [match.group(1) for match in _ACCOUNT_RE.finditer(text)]
    bic_region = text[max(title_position, 0) :]
    bic_region_offset = max(title_position, 0)
    kpp_spans = [match.span(1) for match in kpp_matches]
    bics = [
        match.group(1)
        for match in _BIC_RE.finditer(bic_region)
        if not any(
            start <= match.start(1) + bic_region_offset < end
            for start, end in kpp_spans
        )
    ]
    if len(inns) >= 1:
        result["payer"]["inn"] = inns[0]
    if len(inns) >= 2:
        result["beneficiary"]["inn"] = inns[1]
    if len(kpps) >= 1:
        result["payer"]["kpp"] = _optional_kpp(kpps[0])
    if len(kpps) >= 2:
        result["beneficiary"]["kpp"] = _optional_kpp(kpps[1])
    if len(accounts) >= 4:
        result["payer"]["account"] = accounts[0]
        result["payer"]["bank"]["correspondent_account"] = accounts[1]
        result["beneficiary"]["bank"]["correspondent_account"] = accounts[2]
        result["beneficiary"]["account"] = accounts[3]
    if len(bics) >= 2:
        result["payer"]["bank"]["bic"] = bics[0]
        result["beneficiary"]["bank"]["bic"] = bics[1]

    payer_start = _INN_RE.search(text)
    bank_identity = adapter.bank_identity.search(text, payer_start.end() if payer_start else 0)
    if payer_start and bank_identity:
        bank_line_start = text.rfind("\n", 0, bank_identity.start()) + 1
        result["payer"]["name"] = _extract_entity_name(
            text[payer_start.start() : bank_line_start]
        )
    if len(inns) >= 2:
        second_inn = list(_INN_RE.finditer(text))[1]
        purpose_start = _purpose_start(text)
        if purpose_start > second_inn.start():
            result["beneficiary"]["name"] = _extract_entity_name(
                text[second_inn.start() : purpose_start]
            )

    payer_bic = result["payer"]["bank"]["bic"]
    beneficiary_bic = result["beneficiary"]["bank"]["bic"]
    payer_bank_name = ""
    if bank_identity:
        bank_line_start = text.rfind("\n", 0, bank_identity.start()) + 1
        bank_line_end = text.find("\n", bank_identity.end())
        if bank_line_end < 0:
            bank_line_end = len(text)
        payer_bank_name = _clean_bank_name(text[bank_line_start:bank_line_end], payer_bic)
    if not payer_bank_name and adapter.bank_identity.search(text):
        payer_bank_name = adapter.canonical_bank_name
    result["payer"]["bank"]["name"] = payer_bank_name
    result["beneficiary"]["bank"]["name"] = _bank_name_near_bic(
        text, beneficiary_bic
    )

    stamp_bic, stamp_correspondent = _extract_stamp_bank_details(text)
    if stamp_bic and payer_bic and stamp_bic != payer_bic:
        warnings.append("payer-bank BIC conflicts with execution stamp")
    if (
        stamp_correspondent
        and result["payer"]["bank"]["correspondent_account"]
        and stamp_correspondent != result["payer"]["bank"]["correspondent_account"]
    ):
        warnings.append("payer-bank correspondent account conflicts with execution stamp")

    purpose = _extract_payment_purpose(text)
    result["payment_purpose"] = purpose
    invoice_number, invoice_date = _extract_invoice_reference(purpose)
    result["invoice_reference"] = {
        "number": invoice_number,
        "date": invoice_date,
    }
    if invoice_number and not invoice_date:
        warnings.append("invoice reference date is not present")
    result["vat"] = _extract_vat(purpose)


def _validate_result(
    result: RussianPaymentOrderParseResult,
    warnings: list[str],
) -> None:
    critical_fields = {
        "payment order number": result["payment_order_number"],
        "document date": result["document_date"],
        "amount": result["amount"],
        "currency": result["currency"],
        "payer name": result["payer"]["name"],
        "payer INN": result["payer"]["inn"],
        "payer account": result["payer"]["account"],
        "payer bank name": result["payer"]["bank"]["name"],
        "payer bank BIC": result["payer"]["bank"]["bic"],
        "payer bank correspondent account": result["payer"]["bank"]["correspondent_account"],
        "beneficiary name": result["beneficiary"]["name"],
        "beneficiary INN": result["beneficiary"]["inn"],
        "beneficiary account": result["beneficiary"]["account"],
        "beneficiary bank name": result["beneficiary"]["bank"]["name"],
        "beneficiary bank BIC": result["beneficiary"]["bank"]["bic"],
        "beneficiary bank correspondent account": result["beneficiary"]["bank"]["correspondent_account"],
        "payment purpose": result["payment_purpose"],
    }
    for field_name, value in critical_fields.items():
        if not value:
            warnings.append(f"critical field needs review: {field_name}")
    amount = _decimal_from_text(result["amount"])
    if amount is not None and amount <= 0:
        warnings.append("critical field needs review: positive amount")
    if result["execution_status"] == RUSSIAN_PAYMENT_ORDER_EXECUTION_NOT_EXECUTED:
        warnings.append("payment order is explicitly not executed")
    elif result["execution_status"] == RUSSIAN_PAYMENT_ORDER_EXECUTION_UNCLEAR:
        warnings.append("payment-order execution status is unclear")
    elif not result["executed_at"]:
        warnings.append("critical field needs review: execution timestamp")


def _finish_result(
    result: RussianPaymentOrderParseResult,
    *,
    warnings: list[str],
    errors: list[str],
    parse_error: bool = False,
) -> RussianPaymentOrderParseResult:
    result["warnings"] = _dedupe(warnings)
    result["errors"] = _dedupe(errors)
    if parse_error or result["errors"]:
        result["parse_status"] = RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_PARSE_ERROR
    elif any(warning not in _NON_BLOCKING_WARNINGS for warning in result["warnings"]):
        result["parse_status"] = RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_NEEDS_REVIEW
    else:
        result["parse_status"] = RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_PARSED
    result["posting_eligible"] = bool(
        result["parse_status"] == RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_PARSED
        and result["execution_status"] == RUSSIAN_PAYMENT_ORDER_EXECUTION_EXECUTED
    )
    return result


def _empty_result(*, file_sha256: str) -> RussianPaymentOrderParseResult:
    empty_bank = {"name": "", "bic": "", "correspondent_account": ""}
    return {
        "form_code": "",
        "source_bank": "",
        "adapter": "",
        "payment_order_number": "",
        "document_date": "",
        "debit_date": "",
        "execution_date": "",
        "executed_at": "",
        "execution_status": RUSSIAN_PAYMENT_ORDER_EXECUTION_UNCLEAR,
        "posting_eligible": False,
        "amount": "",
        "currency": "",
        "payer": {"name": "", "inn": "", "kpp": "", "account": "", "bank": dict(empty_bank)},
        "beneficiary": {"name": "", "inn": "", "kpp": "", "account": "", "bank": dict(empty_bank)},
        "payment_purpose": "",
        "invoice_reference": {"number": "", "date": ""},
        "vat": {"status": RUSSIAN_PAYMENT_ORDER_VAT_UNSPECIFIED, "rate_percent": "", "amount": ""},
        "parse_status": RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_PARSE_ERROR,
        "warnings": [],
        "errors": [],
        "parser_version": RUSSIAN_PAYMENT_ORDER_PARSER_VERSION,
        "file_sha256": file_sha256,
        "payment_fingerprint": "",
        "fingerprint_version": RUSSIAN_PAYMENT_ORDER_FINGERPRINT_VERSION,
        "extraction": {},
    }


def _detect_adapter(text: str) -> _PaymentOrderAdapter | None:
    stamp = text
    marker = re.search(r"Отметки\s+банка", text, flags=re.IGNORECASE)
    if marker:
        stamp = text[marker.end() :]
    matches = [adapter for adapter in _ADAPTERS if adapter.bank_identity.search(stamp)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        matches = [adapter for adapter in _ADAPTERS if adapter.bank_identity.search(text)]
    return matches[0] if len(matches) == 1 else None


def _repo_pdf_text_extractor(
    file_bytes: bytes,
    filename: str,
) -> tuple[str, dict[str, Any], list[str]]:
    from packages.application.supplier_financial_documents import (  # noqa: PLC0415
        extract_pdf_text_layer,
    )

    return extract_pdf_text_layer(file_bytes, filename)


def _sanitize_extraction(diagnostics: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "method",
        "text_char_count",
        "pypdf_available",
        "pypdf_page_count",
        "pdftotext_text_nonempty",
    }
    return {key: diagnostics[key] for key in sorted(allowed) if key in diagnostics}


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", str(value or ""))
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    lines = [" ".join(line.split()) for line in normalized.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def _distinct_matches(pattern: re.Pattern[str], text: str) -> list[str]:
    values: list[str] = []
    for match in pattern.finditer(text):
        value = match.group(1).strip()
        if value not in values:
            values.append(value)
    return values


def _extract_amount(text: str) -> Decimal | None:
    patterns = (
        re.compile(r"\bСумма\s+(\d[\d ]{0,20})[-–](\d{2})\b", re.IGNORECASE),
        re.compile(r"(?<!\d)(\d{1,15})[-–](\d{2})\s*Сумма\b", re.IGNORECASE),
    )
    for pattern in patterns:
        match = pattern.search(text)
        if not match:
            continue
        try:
            return Decimal(match.group(1).replace(" ", "") + "." + match.group(2))
        except InvalidOperation:
            return None
    return None


def _extract_execution(text: str) -> tuple[str, str]:
    if re.search(r"\b(?:НЕ\s+ИСПОЛНЕН|ОТКЛОНЕН|ОТМЕНЕН|АННУЛИРОВАН)\b", text, re.IGNORECASE):
        return RUSSIAN_PAYMENT_ORDER_EXECUTION_NOT_EXECUTED, ""
    match = re.search(
        rf"\bИСПОЛНЕН\b\s*({_DOTTED_DATE_RE})\s+(\d{{2}}:\d{{2}}:\d{{2}})",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return RUSSIAN_PAYMENT_ORDER_EXECUTION_UNCLEAR, ""
    date = _iso_date(match.group(1))
    return RUSSIAN_PAYMENT_ORDER_EXECUTION_EXECUTED, f"{date}T{match.group(2)}" if date else ""


def _extract_payment_purpose(text: str) -> str:
    marker = re.search(r"(?im)^Назначение\s+платежа\s*$", text)
    if not marker:
        return ""
    prefix = text[: marker.start()]
    starts = list(re.finditer(r"(?im)^Оплата\b", prefix))
    if not starts:
        return ""
    return " ".join(prefix[starts[-1].start() :].split())


def _purpose_start(text: str) -> int:
    marker = re.search(r"(?im)^Назначение\s+платежа\s*$", text)
    if not marker:
        return len(text)
    starts = list(re.finditer(r"(?im)^Оплата\b", text[: marker.start()]))
    return starts[-1].start() if starts else marker.start()


def _extract_entity_name(segment: str) -> str:
    candidates: list[str] = []
    for line in segment.splitlines():
        compact = " ".join(line.split()).strip(" ,;:")
        folded = compact.casefold()
        if not compact or not any(character.isalpha() for character in compact):
            continue
        if folded in {
            "плательщик",
            "получатель",
            "банк плательщика",
            "банк получателя",
        }:
            continue
        if any(marker in folded for marker in _ENTITY_CONTROL_MARKERS):
            continue
        candidates.append(compact)
    return " ".join(candidates)


def _bank_name_near_bic(text: str, bic: str) -> str:
    if not bic:
        return ""
    lines = text.splitlines()
    indices = [index for index, line in enumerate(lines) if bic in line]
    for index in indices:
        candidates: list[tuple[int, str]] = []
        for candidate_index in range(max(0, index - 6), min(len(lines), index + 7)):
            line = " ".join(lines[candidate_index].split()).strip()
            folded = line.casefold()
            if "банк" not in folded:
                continue
            if folded.startswith("банк плательщика") or folded.startswith("банк получателя"):
                continue
            cleaned = _clean_bank_name(line, bic)
            if candidate_index < index:
                continuation: list[str] = []
                for continuation_index in range(candidate_index + 1, index):
                    next_line = " ".join(lines[continuation_index].split()).strip()
                    next_folded = next_line.casefold()
                    if not next_line:
                        continue
                    if (
                        next_folded.startswith("банк ")
                        or next_folded.startswith("бик")
                        or next_folded.startswith("сч.")
                        or re.search(r"\d{9,}", next_line)
                    ):
                        break
                    if any(character.isalpha() for character in next_line):
                        continuation.append(next_line)
                if continuation:
                    cleaned = " ".join([cleaned, *continuation])
            candidates.append((abs(candidate_index - index), cleaned))
        if candidates:
            candidates.sort(key=lambda item: item[0])
            return candidates[0][1]
    return ""


def _clean_bank_name(value: str, bic: str) -> str:
    cleaned = re.sub(r"\s+БИК\b.*$", "", " ".join(value.split()), flags=re.IGNORECASE)
    if bic:
        cleaned = re.sub(rf"\s+{re.escape(bic)}\s*$", "", cleaned)
    return cleaned.strip()


def _extract_stamp_bank_details(text: str) -> tuple[str, str]:
    marker = re.search(r"Отметки\s+банка", text, flags=re.IGNORECASE)
    stamp = text[marker.end() :] if marker else ""
    bic_match = re.search(r"\bБИК\s*(\d{9})(?!\d)", stamp, re.IGNORECASE)
    correspondent_match = re.search(
        r"\bК\s*/\s*С\s*(\d{20})(?!\d)",
        stamp,
        re.IGNORECASE,
    )
    return (
        bic_match.group(1) if bic_match else "",
        correspondent_match.group(1) if correspondent_match else "",
    )


def _extract_invoice_reference(purpose: str) -> tuple[str, str]:
    match = re.search(
        rf"сч[её]т(?:а|у)?\s+(?:№|номер)\s*([A-ZА-Я0-9./_-]+)(?:\s+от\s+({_DOTTED_DATE_RE}|{_RUSSIAN_DATE_RE}))?",
        purpose,
        flags=re.IGNORECASE,
    )
    if not match:
        return "", ""
    return match.group(1).strip(".,;"), _iso_date(match.group(2) or "")


def _extract_vat(purpose: str) -> dict[str, str]:
    if re.search(r"\b(?:НДС\s+не\s+облагается|без\s+НДС)\b", purpose, re.IGNORECASE):
        return {"status": RUSSIAN_PAYMENT_ORDER_VAT_NOT_TAXED, "rate_percent": "", "amount": ""}
    explicit = re.search(
        r"НДС\s*\(\s*(\d+(?:[.,]\d+)?)\s*%\s*\)\s*([\d ]+[.,]\d{2})",
        purpose,
        flags=re.IGNORECASE,
    )
    if explicit:
        amount = _decimal_from_text(explicit.group(2))
        return {
            "status": RUSSIAN_PAYMENT_ORDER_VAT_TAXED,
            "rate_percent": explicit.group(1).replace(",", "."),
            "amount": _decimal_text(amount) if amount is not None else "",
        }
    if re.search(r"\b(?:с\s+НДС|в\s+т\.?\s*ч\.?\s+НДС)\b", purpose, re.IGNORECASE):
        return {"status": RUSSIAN_PAYMENT_ORDER_VAT_TAXED, "rate_percent": "", "amount": ""}
    return {"status": RUSSIAN_PAYMENT_ORDER_VAT_UNSPECIFIED, "rate_percent": "", "amount": ""}


def _payment_fingerprint(result: RussianPaymentOrderParseResult) -> str:
    identity = {
        "version": RUSSIAN_PAYMENT_ORDER_FINGERPRINT_VERSION,
        "form_code": result["form_code"],
        "payment_order_number": _fingerprint_text(result["payment_order_number"]),
        "document_date": result["document_date"],
        "amount": result["amount"],
        "currency": result["currency"],
        "payer_inn": result["payer"]["inn"],
        "payer_account": result["payer"]["account"],
        "payer_bank_bic": result["payer"]["bank"]["bic"],
        "beneficiary_inn": result["beneficiary"]["inn"],
        "beneficiary_account": result["beneficiary"]["account"],
        "beneficiary_bank_bic": result["beneficiary"]["bank"]["bic"],
        "payment_purpose": _fingerprint_text(result["payment_purpose"]),
    }
    if any(not value for key, value in identity.items() if key != "version"):
        return ""
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _fingerprint_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return " ".join(re.sub(r"[^0-9a-zа-я%]+", " ", normalized).split())


def _iso_date(value: str) -> str:
    compact = " ".join(str(value or "").replace("г.", "").split()).strip(" .")
    if not compact:
        return ""
    if re.fullmatch(_DOTTED_DATE_RE, compact):
        try:
            return datetime.strptime(compact, "%d.%m.%Y").date().isoformat()
        except ValueError:
            return ""
    parts = compact.casefold().split()
    if len(parts) == 3 and parts[1] in _MONTHS:
        try:
            return datetime(int(parts[2]), _MONTHS[parts[1]], int(parts[0])).date().isoformat()
        except ValueError:
            return ""
    return ""


def _optional_kpp(value: str) -> str:
    compact = str(value or "").strip()
    return "" if not compact or set(compact) == {"0"} else compact


def _decimal_from_text(value: str) -> Decimal | None:
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return None


def _decimal_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        compact = " ".join(str(value or "").split())
        if compact and compact not in result:
            result.append(compact)
    return result
