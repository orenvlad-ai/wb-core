"""Contracts and constants for supplier invoice shipment registry."""

from __future__ import annotations

LINE_TYPE_PRODUCT = "product"
LINE_TYPE_EXTRA = "extra"

PRODUCT_TYPE_CLEAR = "clear"
PRODUCT_TYPE_ANTI_SPY = "anti_spy"
PRODUCT_TYPE_MATTE = "matte"

MATCH_STATUS_MATCHED = "matched"
MATCH_STATUS_MATCHED_BY_COMPATIBILITY = "matched_by_compatibility"
MATCH_STATUS_UNMATCHED = "unmatched"
MATCH_STATUS_AMBIGUOUS = "ambiguous"
MATCH_STATUS_EXTRA = "extra"

SHIPMENT_STATUS_ALL_MATCHED = "all_matched"
SHIPMENT_STATUS_HAS_UNMATCHED = "has_unmatched"
SHIPMENT_STATUS_MANUAL_OVERRIDE = "manual_override"
SHIPMENT_STATUS_CHECKSUM_ERROR = "checksum_error"

SUPPLIER_INVOICE_PARSER_VERSION = "supplier_invoice_parser_v1"

SUPPLIER_INVOICE_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
