"""Контракты блока prices snapshot."""

from dataclasses import dataclass
from typing import Literal, Union


@dataclass(frozen=True)
class PricesSnapshotRequest:
    """Минимальный входной контракт блока."""

    snapshot_type: str
    snapshot_date: str
    nm_ids: list[int]
    scenario: Literal["normal", "empty"] = "normal"


@dataclass(frozen=True)
class PricesSnapshotItem:
    """Агрегированная цена на уровне nmId."""

    nm_id: int
    price_seller: int
    price_seller_discounted: int


@dataclass(frozen=True)
class PricesSnapshotSuccess:
    """Успешный snapshot-ответ."""

    kind: Literal["success"]
    snapshot_date: str
    count: int
    items: list[PricesSnapshotItem]


@dataclass(frozen=True)
class PricesSnapshotEmpty:
    """Ответ для natural empty-case."""

    kind: Literal["empty"]
    snapshot_date: str
    count: int
    items: list[PricesSnapshotItem]
    detail: str


PricesSnapshotResult = Union[PricesSnapshotSuccess, PricesSnapshotEmpty]


@dataclass(frozen=True)
class PricesSnapshotEnvelope:
    """Общий результат блока поверх success/empty."""

    result: PricesSnapshotResult
