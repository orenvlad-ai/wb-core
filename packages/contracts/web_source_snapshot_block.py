"""Контракты блока web-source snapshot."""

from dataclasses import dataclass
from typing import Literal, Union


@dataclass(frozen=True)
class WebSourceSnapshotRequest:
    """Минимальный входной контракт блока."""

    snapshot_type: str
    date_from: str
    date_to: str
    scenario: Literal["normal", "not_found"] = "normal"


@dataclass(frozen=True)
class WebSourceSnapshotItem:
    """Элемент snapshot-выдачи."""

    nm_id: int
    views_current: int
    ctr_current: int
    orders_current: int
    position_avg: int


@dataclass(frozen=True)
class WebSourceSnapshotSuccess:
    """Успешный snapshot-ответ."""

    kind: Literal["success"]
    date_from: str
    date_to: str
    count: int
    items: list[WebSourceSnapshotItem]


@dataclass(frozen=True)
class WebSourceSnapshotNotFound:
    """Ответ для режима not-found."""

    kind: Literal["not_found"]
    detail: str


WebSourceSnapshotResult = Union[
    WebSourceSnapshotSuccess,
    WebSourceSnapshotNotFound,
]


@dataclass(frozen=True)
class WebSourceSnapshotEnvelope:
    """Общий результат блока поверх success/not-found."""

    result: WebSourceSnapshotResult
