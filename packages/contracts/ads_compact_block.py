"""Контракты блока ads compact."""

from dataclasses import dataclass, field
from typing import Any, Literal, Union


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
    ads_cpc: float | None
    ads_ctr: float | None
    ads_cr: float | None


@dataclass(frozen=True)
class AdsCompactSuccess:
    kind: Literal["success"]
    snapshot_date: str
    count: int
    items: list[AdsCompactItem]
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AdsCompactEmpty:
    kind: Literal["empty"]
    snapshot_date: str
    count: int
    items: list[AdsCompactItem]
    detail: str
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AdsCompactPartial:
    """Validated observed contribution, never a complete account/day report."""

    kind: Literal["incomplete"]
    snapshot_date: str
    count: int
    items: list[AdsCompactItem]
    requested_count: int
    covered_count: int
    missing_nm_ids: list[int]
    detail: str
    diagnostics: dict[str, Any] = field(default_factory=dict)
    temporal_snapshot_acceptable: bool = True


AdsCompactResult = Union[AdsCompactSuccess, AdsCompactEmpty, AdsCompactPartial]


@dataclass(frozen=True)
class AdsCompactEnvelope:
    result: AdsCompactResult
