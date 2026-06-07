"""Contracts for public-card SPP proxy source."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping


@dataclass(frozen=True)
class SppProxyRequest:
    """Input contract for SPP proxy over public WB buyer prices."""

    snapshot_type: str
    snapshot_date: str
    nm_ids: list[int]
    price_seller_discounted_by_nm_id: Mapping[int, float | int | None] = field(default_factory=dict)
    scenario: Literal["normal", "empty"] = "normal"


@dataclass(frozen=True)
class SppProxyItem:
    """SPP proxy result at nmId level."""

    nm_id: int
    spp_proxy: float
    price_seller_discounted: float
    public_buyer_price: float
    spp_proxy_rub: float
    diagnostic: str = ""


@dataclass(frozen=True)
class SppProxySuccess:
    kind: Literal["success"]
    snapshot_date: str
    count: int
    requested_count: int
    covered_count: int
    items: list[SppProxyItem]
    detail: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SppProxyIncomplete:
    kind: Literal["incomplete"]
    snapshot_date: str
    count: int
    requested_count: int
    covered_count: int
    items: list[SppProxyItem]
    missing_nm_ids: list[int]
    detail: str
    temporal_snapshot_acceptable: bool = True
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SppProxyEmpty:
    kind: Literal["empty"]
    snapshot_date: str
    count: int
    requested_count: int
    covered_count: int
    items: list[SppProxyItem]
    detail: str
    diagnostics: dict[str, Any] = field(default_factory=dict)


SppProxyResult = SppProxySuccess | SppProxyIncomplete | SppProxyEmpty


@dataclass(frozen=True)
class SppProxyEnvelope:
    result: SppProxyResult
