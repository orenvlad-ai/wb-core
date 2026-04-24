"""Контракты блока seller funnel snapshot."""

from dataclasses import dataclass
from typing import Literal, Union


@dataclass(frozen=True)
class SellerFunnelSnapshotRequest:
    """Минимальный входной контракт блока."""

    snapshot_type: str
    date: str
    scenario: Literal["normal", "not_found"] = "normal"
    nm_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class SellerFunnelSnapshotItem:
    """Элемент daily snapshot."""

    nm_id: int
    name: str
    vendor_code: str
    view_count: int
    open_card_count: int
    ctr: int


@dataclass(frozen=True)
class SellerFunnelSnapshotSuccess:
    """Успешный daily snapshot."""

    kind: Literal["success"]
    date: str
    count: int
    items: list[SellerFunnelSnapshotItem]


@dataclass(frozen=True)
class SellerFunnelSnapshotNotFound:
    """Ответ для режима not-found."""

    kind: Literal["not_found"]
    detail: str


SellerFunnelSnapshotResult = Union[
    SellerFunnelSnapshotSuccess,
    SellerFunnelSnapshotNotFound,
]


@dataclass(frozen=True)
class SellerFunnelSnapshotEnvelope:
    """Общий результат блока поверх success/not-found."""

    result: SellerFunnelSnapshotResult
