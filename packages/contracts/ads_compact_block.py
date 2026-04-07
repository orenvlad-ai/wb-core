"""Контракты блока ads compact."""

from dataclasses import dataclass
from typing import Literal, Union


@dataclass(frozen=True)
class AdsCompactRequest:
    """Минимальный входной контракт блока."""

    snapshot_type: str
    snapshot_date: str
    nm_ids: list[int]
    scenario: Literal["normal", "empty"] = "normal"


@dataclass(frozen=True)
class AdsCompactItem:
    """Элемент snapshot-выдачи на уровне snapshot_date + nmId."""

    nm_id: int
    ads_views: float
    ads_clicks: float
    ads_atbs: float
    ads_orders: float
    ads_sum: float
    ads_sum_price: float
    ads_cpc: float
    ads_ctr: float
    ads_cr: float


@dataclass(frozen=True)
class AdsCompactSuccess:
    kind: Literal["success"]
    snapshot_date: str
    count: int
    items: list[AdsCompactItem]


@dataclass(frozen=True)
class AdsCompactEmpty:
    kind: Literal["empty"]
    snapshot_date: str
    count: int
    items: list[AdsCompactItem]
    detail: str


AdsCompactResult = Union[AdsCompactSuccess, AdsCompactEmpty]


@dataclass(frozen=True)
class AdsCompactEnvelope:
    result: AdsCompactResult
