"""Контракты блока sku display bundle."""

from dataclasses import dataclass
from typing import Literal, Union


@dataclass(frozen=True)
class SkuDisplayBundleRequest:
    """Минимальный входной контракт display bundle блока."""

    bundle_type: str
    scenario: Literal["normal", "empty"] = "normal"


@dataclass(frozen=True)
class SkuDisplayBundleItem:
    """Один table-facing SKU display item."""

    nm_id: int
    display_name: str
    group: str
    enabled: bool
    display_order: int


@dataclass(frozen=True)
class SkuDisplayBundleSuccess:
    kind: Literal["success"]
    count: int
    items: list[SkuDisplayBundleItem]


@dataclass(frozen=True)
class SkuDisplayBundleEmpty:
    kind: Literal["empty"]
    count: int
    items: list[SkuDisplayBundleItem]
    detail: str


SkuDisplayBundleResult = Union[SkuDisplayBundleSuccess, SkuDisplayBundleEmpty]


@dataclass(frozen=True)
class SkuDisplayBundleEnvelope:
    result: SkuDisplayBundleResult
