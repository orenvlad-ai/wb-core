"""Контракты блока sf period."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class SfPeriodRequest:
    """Минимальный входной контракт блока."""

    snapshot_type: str
    snapshot_date: str
    nm_ids: list[int]
    scenario: Literal["normal"] = "normal"


@dataclass(frozen=True)
class SfPeriodItem:
    """Элемент snapshot-выдачи на уровне nmId."""

    nm_id: int
    localization_percent: int
    feedback_rating: float


@dataclass(frozen=True)
class SfPeriodSuccess:
    """Успешный period snapshot."""

    kind: Literal["success"]
    snapshot_date: str
    count: int
    items: list[SfPeriodItem]


@dataclass(frozen=True)
class SfPeriodEnvelope:
    """Общий результат блока."""

    result: SfPeriodSuccess
