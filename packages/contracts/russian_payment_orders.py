"""Normalized contract for Russian bank payment-order text parsing."""

from __future__ import annotations

from typing import Any, TypedDict


RUSSIAN_PAYMENT_ORDER_FORM_CODE = "0401060"
RUSSIAN_PAYMENT_ORDER_CURRENCY = "RUB"
RUSSIAN_PAYMENT_ORDER_PARSER_VERSION = "russian_payment_order_parser_v1"
RUSSIAN_PAYMENT_ORDER_FINGERPRINT_VERSION = "russian_payment_order_content_v1"

RUSSIAN_PAYMENT_ORDER_SOURCE_WB_BANK = "wb_bank"
RUSSIAN_PAYMENT_ORDER_SOURCE_VTB = "vtb"
RUSSIAN_PAYMENT_ORDER_SOURCES = {
    RUSSIAN_PAYMENT_ORDER_SOURCE_WB_BANK,
    RUSSIAN_PAYMENT_ORDER_SOURCE_VTB,
}

RUSSIAN_PAYMENT_ORDER_ADAPTER_WB_BANK = "wb_bank_0401060_v1"
RUSSIAN_PAYMENT_ORDER_ADAPTER_VTB = "vtb_0401060_v1"
RUSSIAN_PAYMENT_ORDER_ADAPTERS = {
    RUSSIAN_PAYMENT_ORDER_ADAPTER_WB_BANK,
    RUSSIAN_PAYMENT_ORDER_ADAPTER_VTB,
}

RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_PARSED = "parsed"
RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_NEEDS_REVIEW = "needs_review"
RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_PARSE_ERROR = "parse_error"
RUSSIAN_PAYMENT_ORDER_PARSE_STATUSES = {
    RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_PARSED,
    RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_NEEDS_REVIEW,
    RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_PARSE_ERROR,
}

RUSSIAN_PAYMENT_ORDER_EXECUTION_EXECUTED = "executed"
RUSSIAN_PAYMENT_ORDER_EXECUTION_NOT_EXECUTED = "not_executed"
RUSSIAN_PAYMENT_ORDER_EXECUTION_UNCLEAR = "unclear"
RUSSIAN_PAYMENT_ORDER_EXECUTION_STATUSES = {
    RUSSIAN_PAYMENT_ORDER_EXECUTION_EXECUTED,
    RUSSIAN_PAYMENT_ORDER_EXECUTION_NOT_EXECUTED,
    RUSSIAN_PAYMENT_ORDER_EXECUTION_UNCLEAR,
}

RUSSIAN_PAYMENT_ORDER_VAT_TAXED = "taxed"
RUSSIAN_PAYMENT_ORDER_VAT_NOT_TAXED = "not_taxed"
RUSSIAN_PAYMENT_ORDER_VAT_UNSPECIFIED = "unspecified"


class RussianPaymentOrderBank(TypedDict):
    name: str
    bic: str
    correspondent_account: str


class RussianPaymentOrderParty(TypedDict):
    name: str
    inn: str
    kpp: str
    account: str
    bank: RussianPaymentOrderBank


class RussianPaymentOrderInvoiceReference(TypedDict):
    number: str
    date: str


class RussianPaymentOrderVat(TypedDict):
    status: str
    rate_percent: str
    amount: str


class RussianPaymentOrderParseResult(TypedDict):
    form_code: str
    source_bank: str
    adapter: str
    payment_order_number: str
    document_date: str
    debit_date: str
    execution_date: str
    executed_at: str
    execution_status: str
    posting_eligible: bool
    amount: str
    currency: str
    payer: RussianPaymentOrderParty
    beneficiary: RussianPaymentOrderParty
    payment_purpose: str
    invoice_reference: RussianPaymentOrderInvoiceReference
    vat: RussianPaymentOrderVat
    parse_status: str
    warnings: list[str]
    errors: list[str]
    parser_version: str
    file_sha256: str
    payment_fingerprint: str
    fingerprint_version: str
    extraction: dict[str, Any]
