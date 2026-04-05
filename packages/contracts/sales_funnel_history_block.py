"""Контракты блока sales funnel history."""

from dataclasses import dataclass
from typing import Literal, Union


@dataclass(frozen=True)
class SalesFunnelHistoryRequest:
    """Минимальный входной контракт блока."""

    snapshot_type: str
    date_from: str
    date_to: str
    nm_ids: list[int]
    scenario: Literal["normal", "empty"] = "normal"


@dataclass(frozen=True)
class SalesFunnelHistoryItem:
    """Одна history-row на уровне date + nmId + metric."""

    date: str
    nm_id: int
    metric: str
    value: float


@dataclass(frozen=True)
class SalesFunnelHistorySuccess:
    kind: Literal["success"]
    date_from: str
    date_to: str
    count: int
    items: list[SalesFunnelHistoryItem]


@dataclass(frozen=True)
class SalesFunnelHistoryEmpty:
    kind: Literal["empty"]
    date_from: str
    date_to: str
    count: int
    items: list[SalesFunnelHistoryItem]
    detail: str


SalesFunnelHistoryResult = Union[SalesFunnelHistorySuccess, SalesFunnelHistoryEmpty]


@dataclass(frozen=True)
class SalesFunnelHistoryEnvelope:
    result: SalesFunnelHistoryResult
