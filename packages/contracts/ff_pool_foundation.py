"""Typed contracts for the inert FF facility/pool foundation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


FfPool = Literal["FBS", "FBO"]
ParityStatus = Literal["feature_off", "detail_empty", "pass", "mismatch"]
WarehouseRelationType = Literal[
    "correction_of",
    "storno_of",
    "late_expense_for",
]


@dataclass(frozen=True)
class FfFacility:
    facility_id: str
    code: str
    name: str
    active: bool
    display_timezone: str


@dataclass(frozen=True)
class WarehouseBusinessOperation:
    operation_id: str
    operation_type: str
    source_system: str
    source_type: str
    source_id: str
    source_revision: str
    idempotency_epoch: int
    business_date: str
    posted_at: str


@dataclass(frozen=True)
class FfPoolMovementLine:
    operation_id: str
    line_no: int
    facility_id: str
    pool: FfPool
    nm_id: int
    quantity_delta: int
    capital_delta_rub: Decimal
    wac_snapshot_rub: Decimal | None


@dataclass(frozen=True)
class WarehouseBusinessOperationRelation:
    parent_id: str
    child_id: str
    relation_type: WarehouseRelationType
    created_at: str


@dataclass(frozen=True)
class FfPoolBalance:
    facility_id: str
    pool: FfPool
    nm_id: int
    quantity: int
    capital_rub: Decimal
    wac_rub: Decimal | None


@dataclass(frozen=True)
class FfPoolFeatureState:
    epoch: int
    writer_configured: bool
    reader_configured: bool
    writer_effective: bool
    reader_effective: bool
    parity_status: str
    reason: str


@dataclass(frozen=True)
class FfPoolParityResult:
    status: ParityStatus
    feature_epoch: int
    detail_row_count: int
    aggregate_row_count: int
    detail_quantity: int
    aggregate_quantity: int
    detail_capital_rub: Decimal
    aggregate_capital_rub: Decimal
    mismatched_nm_ids: tuple[int, ...]
    quantity_mismatched_nm_ids: tuple[int, ...]
    canonical_capital_mismatched_nm_ids: tuple[int, ...]
    raw_capital_mismatched_nm_ids: tuple[int, ...]
    raw_capital_residuals_by_nm: tuple[tuple[int, Decimal], ...]
    detail_canonical_capital_minor_units: int
    aggregate_canonical_capital_minor_units: int
    raw_capital_residual_rub: Decimal
    raw_residual_conserved: bool
    money_parity_policy: str
    detail_fingerprint: str
    aggregate_fingerprint: str
    fail_closed: bool
    reader_allowed: bool
    aggregate_unchanged: bool = True
