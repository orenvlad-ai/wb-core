"""Контракты блока spp."""

from dataclasses import dataclass
from typing import Literal, Union


@dataclass(frozen=True)
class SppRequest:
    """Минимальный входной контракт блока."""

    snapshot_type: str
    snapshot_date: str
    nm_ids: list[int]
    scenario: Literal["normal", "empty"] = "normal"


@dataclass(frozen=True)
class SppItem:
    """Элемент snapshot-выдачи на уровне nmId."""

    nm_id: int
    spp: float


@dataclass(frozen=True)
class SppSuccess:
    """Успешный snapshot spp."""

    kind: Literal["success"]
    snapshot_date: str
    count: int
    items: list[SppItem]


@dataclass(frozen=True)
class SppEmpty:
    """Ответ для natural empty-case."""

    kind: Literal["empty"]
    snapshot_date: str
    count: int
    items: list[SppItem]
    detail: str


SppResult = Union[SppSuccess, SppEmpty]


@dataclass(frozen=True)
class SppEnvelope:
    """Общий результат блока поверх success/empty."""

    result: SppResult
