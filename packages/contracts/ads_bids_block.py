"""Контракты блока ads bids."""

from dataclasses import dataclass
from typing import Literal, Union


@dataclass(frozen=True)
class AdsBidsRequest:
    """Минимальный входной контракт блока."""

    snapshot_type: str
    snapshot_date: str
    nm_ids: list[int]
    scenario: Literal["normal", "empty"] = "normal"


@dataclass(frozen=True)
class AdsBidsItem:
    """Элемент snapshot-выдачи на уровне nmId."""

    nm_id: int
    ads_bid_search: float
    ads_bid_recommendations: float


@dataclass(frozen=True)
class AdsBidsSuccess:
    """Успешный bids snapshot."""

    kind: Literal["success"]
    snapshot_date: str
    count: int
    items: list[AdsBidsItem]


@dataclass(frozen=True)
class AdsBidsEmpty:
    """Ответ для natural empty-case."""

    kind: Literal["empty"]
    snapshot_date: str
    count: int
    items: list[AdsBidsItem]
    detail: str


AdsBidsResult = Union[AdsBidsSuccess, AdsBidsEmpty]


@dataclass(frozen=True)
class AdsBidsEnvelope:
    """Общий результат блока поверх success/empty."""

    result: AdsBidsResult
