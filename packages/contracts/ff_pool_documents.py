"""Typed Stage 2 contracts for facility × pool business documents."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Mapping, Any


FfPool = Literal["FBS", "FBO"]
PoolScope = Literal["FBS", "FBO", "both"]
WorkflowState = Literal[
    "accepted",
    "processing",
    "blocked",
    "ready",
    "posted",
    "replay",
    "complete",
    "error",
]
DocumentRelationType = Literal[
    "shipment_of",
    "receipt_of",
    "loss_of",
    "discrepancy_of",
    "cancellation_of",
    "inventory_surplus_of",
    "inventory_shortage_of",
    "correction_of",
    "storno_of",
    "late_expense_for",
]


@dataclass(frozen=True)
class PoolLocation:
    facility_id: str
    pool: FfPool


@dataclass(frozen=True)
class DocumentIdentity:
    request_id: str
    source_system: str
    source_type: str
    source_id: str
    source_revision: str
    idempotency_epoch: int
    actor: str
    business_date: str


@dataclass(frozen=True)
class ExpenseLine:
    amount_rub: Decimal
    basis: str
    source_file_sha256: str = ""
    source_filename: str = ""


@dataclass(frozen=True)
class QuantityCapitalLine:
    nm_id: int
    quantity: int
    capital_rub: Decimal
    barcode: str = ""
    nomenclature_evidence_digest: str = ""
    metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class PoolAllocationLine:
    nm_id: int
    quantity_fbs: int
    quantity_fbo: int
    accepted_quantity: int
    accepted_capital_rub: Decimal
    barcode: str = ""
    nomenclature_evidence_digest: str = ""


@dataclass(frozen=True)
class XlsxParserLimits:
    max_request_bytes: int = 9 * 1024 * 1024
    max_file_bytes: int = 8 * 1024 * 1024
    max_zip_entries: int = 128
    max_uncompressed_bytes: int = 32 * 1024 * 1024
    max_entry_uncompressed_bytes: int = 12 * 1024 * 1024
    max_compression_ratio: int = 100
    max_rows: int = 20_000
    max_columns: int = 16
    max_cells: int = 200_000
    max_shared_strings_bytes: int = 4 * 1024 * 1024
    max_cell_text_bytes: int = 8 * 1024
