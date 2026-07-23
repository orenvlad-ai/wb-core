"""Контракты блока stocks."""

from dataclasses import dataclass, field
from typing import Any, Literal, Union


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
    in_way_to_client: float = 0.0
    in_way_from_client: float = 0.0
    wb_contour_total: float = 0.0
    stock_ru_central_north: float = 0.0
    stock_ru_central_east: float = 0.0
    stock_ru_central_south: float = 0.0


@dataclass(frozen=True)
class StocksWarehouseRow:
    """Warehouse-granular evidence retained through planning aggregation."""

    nm_id: int
    warehouse_id: int | None
    warehouse_name: str
    region_name: str
    quantity: float
    planning_zone_key: str | None
    classification_status: str
    classification_source: str
    in_way_to_client: float = 0.0
    in_way_from_client: float = 0.0
    exclusion_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class StocksSuccess:
    """Успешный snapshot остатков."""

    kind: Literal["success"]
    snapshot_date: str
    count: int
    items: list[StocksItem]
    detail: str = ""
    warehouse_rows: list[StocksWarehouseRow] = field(default_factory=list)
    planning_reconciliation: dict[str, Any] = field(default_factory=dict)
    fetched_at: str = ""
    pagination_complete: bool = False
    raw_rows_digest: str = ""


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
