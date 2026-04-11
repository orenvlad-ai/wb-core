"""Контракты блока promo by price."""

from dataclasses import dataclass
from typing import Literal, Union


@dataclass(frozen=True)
class PromoByPriceRequest:
    """Минимальный входной контракт rule-based блока."""

    snapshot_type: str
    date_from: str
    date_to: str
    nm_ids: list[int]
    scenario: Literal["normal", "empty"] = "normal"


@dataclass(frozen=True)
class PromoByPriceItem:
    """Одна historical row на уровне date + nmId."""

    date: str
    nm_id: int
    promo_count_by_price: float
    promo_entry_price_best: float
    promo_participation: float


@dataclass(frozen=True)
class PromoByPriceSuccess:
    kind: Literal["success"]
    date_from: str
    date_to: str
    count: int
    items: list[PromoByPriceItem]


@dataclass(frozen=True)
class PromoByPriceEmpty:
    kind: Literal["empty"]
    date_from: str
    date_to: str
    count: int
    items: list[PromoByPriceItem]
    detail: str


PromoByPriceResult = Union[PromoByPriceSuccess, PromoByPriceEmpty]


@dataclass(frozen=True)
class PromoByPriceEnvelope:
    result: PromoByPriceResult
