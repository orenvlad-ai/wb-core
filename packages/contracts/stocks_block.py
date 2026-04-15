"""Контракты блока stocks."""

from dataclasses import dataclass
from typing import Literal, Union


@dataclass(frozen=True)
class StocksRequest:
    """Минимальный входной контракт блока."""

    snapshot_type: str
    snapshot_date: str
    nm_ids: list[int]
    scenario: Literal["normal", "partial"] = "normal"


@dataclass(frozen=True)
class StocksItem:
    """Элемент snapshot-выдачи на уровне nmId."""

    nm_id: int
    stock_total: float
    stock_ru_central: float
    stock_ru_northwest: float
    stock_ru_volga: float
    stock_ru_ural: float
    stock_ru_south_caucasus: float
    stock_ru_far_siberia: float


@dataclass(frozen=True)
class StocksSuccess:
    """Успешный snapshot остатков."""

    kind: Literal["success"]
    snapshot_date: str
    count: int
    items: list[StocksItem]
    detail: str = ""


@dataclass(frozen=True)
class StocksIncomplete:
    """Ответ для неполного coverage snapshot."""

    kind: Literal["incomplete"]
    snapshot_date: str
    requested_count: int
    covered_count: int
    missing_nm_ids: list[int]
    detail: str


StocksResult = Union[StocksSuccess, StocksIncomplete]


@dataclass(frozen=True)
class StocksEnvelope:
    """Общий результат блока поверх success/incomplete."""

    result: StocksResult
