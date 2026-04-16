"""Контракты отдельного COST_PRICE upload contour."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class CostPriceRow:
    group: str
    cost_price_rub: float
    effective_from: str


@dataclass(frozen=True)
class CostPriceUploadPayload:
    dataset_version: str
    uploaded_at: str
    cost_price_rows: list[CostPriceRow]


@dataclass(frozen=True)
class CostPriceUploadAcceptedCounts:
    cost_price_rows: int


@dataclass(frozen=True)
class CostPriceUploadResult:
    status: Literal["accepted", "rejected"]
    dataset_version: str
    accepted_counts: CostPriceUploadAcceptedCounts
    validation_errors: list[str]
    activated_at: str | None


@dataclass(frozen=True)
class CostPriceCurrentState:
    dataset_version: str
    activated_at: str
    cost_price_rows: list[CostPriceRow]
