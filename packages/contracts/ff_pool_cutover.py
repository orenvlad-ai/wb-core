"""Typed contracts for the safe FF facility/pool cutover preparation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


CutoverOrderClass = Literal[
    "pre_t_absorbed_closed",
    "pre_t_absorbed_reservation",
    "post_t_deferred",
    "unmatched",
]
WarehouseDomainBarrierPhase = Literal[
    "held",
    "applying",
    "readback_required",
    "recovery_required",
    "recovery_applying",
    "recovery_readback_required",
    "reconciled",
    "released",
    "aborted",
]


@dataclass(frozen=True)
class CutoverAllocation:
    facility_id: str
    pool: Literal["FBS", "FBO"]
    nm_id: int
    quantity: int
    capital_rub: Decimal
    wac_rub: Decimal | None


@dataclass(frozen=True)
class CutoverOrderClassification:
    order_id: int
    observation_id: str
    source_revision: str
    classification: CutoverOrderClass
    nm_id: int
    quantity: int
    facility_id: str
    status_fingerprint: str
    mapping_digest: str


@dataclass(frozen=True)
class CutoverPlanSummary:
    cutover_id: str
    manifest_digest: str
    deployed_sha: str
    cutover_at: str
    business_date: str
    feature_epoch: int
    aggregate_quantity: int
    detail_quantity: int
    aggregate_capital_rub: Decimal
    detail_capital_rub: Decimal
    pre_t_absorbed_count: int
    opening_reservation_count: int
    post_t_deferred_count: int
    unmatched_count: int
    apply_allowed: bool
