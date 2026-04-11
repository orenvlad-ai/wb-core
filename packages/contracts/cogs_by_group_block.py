"""Контракты блока cogs by group."""

from dataclasses import dataclass
from typing import Literal, Union


@dataclass(frozen=True)
class CogsByGroupRequest:
    """Минимальный входной контракт rule-based блока."""

    snapshot_type: str
    date_from: str
    date_to: str
    nm_ids: list[int]
    scenario: Literal["normal", "empty"] = "normal"


@dataclass(frozen=True)
class CogsByGroupItem:
    """Одна historical row на уровне date + nmId."""

    date: str
    nm_id: int
    cost_price_rub: float


@dataclass(frozen=True)
class CogsByGroupSuccess:
    kind: Literal["success"]
    date_from: str
    date_to: str
    count: int
    items: list[CogsByGroupItem]


@dataclass(frozen=True)
class CogsByGroupEmpty:
    kind: Literal["empty"]
    date_from: str
    date_to: str
    count: int
    items: list[CogsByGroupItem]
    detail: str


CogsByGroupResult = Union[CogsByGroupSuccess, CogsByGroupEmpty]


@dataclass(frozen=True)
class CogsByGroupEnvelope:
    result: CogsByGroupResult
