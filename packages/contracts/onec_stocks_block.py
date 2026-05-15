"""Contracts for the bounded 1C/Soykasoft WB stocks source."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Union


ONEC_STOCKS_PARTIAL_FETCH_META_KEY = "_wb_core_partial_fetch"


OnecCanonicalStageCode = Literal[
    "CHINA_TO_FF",
    "CN_TO_RU_TRANSIT",
    "FF_TO_WB",
    "FF_TO_WB_TRANSIT",
    "FF_STOCK",
    "WB_STOCK",
    "CN_PRODUCTION_PAID",
]

ALLOWED_ONEC_CANONICAL_STAGE_CODES: tuple[OnecCanonicalStageCode, ...] = (
    "CHINA_TO_FF",
    "CN_TO_RU_TRANSIT",
    "FF_TO_WB",
    "FF_TO_WB_TRANSIT",
    "FF_STOCK",
    "WB_STOCK",
    "CN_PRODUCTION_PAID",
)


@dataclass(frozen=True)
class OnecStocksRequest:
    """Minimal request for the confirmed 1C `/stocks_wb` method."""

    snapshot_type: str
    account_id: str
    nm_ids: list[int]
    scenario: Literal["success"] = "success"


@dataclass(frozen=True)
class OnecStocksMeta:
    version: str
    marketplace: str
    account_id: str
    date: str
    generated_at: str
    currency: str


@dataclass(frozen=True)
class OnecStocksStage:
    stage_name: str
    qty: float
    unit_cost_rub: float
    cost_total_rub: float


@dataclass(frozen=True)
class OnecStocksItem:
    nm_id: str
    product_1c_id: str
    vendor_code: str
    name: str
    stages: dict[str, OnecStocksStage]
    sizes: list[Mapping[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class OnecStocksParsedPayload:
    meta: OnecStocksMeta
    items: list[OnecStocksItem]


@dataclass(frozen=True)
class OnecStageMappingEntry:
    source_stage_name: str
    canonical_stage_code: OnecCanonicalStageCode


@dataclass(frozen=True)
class OnecStocksNormalizedStage:
    account_id: str
    date: str
    generated_at: str
    currency: str
    nm_id: int
    source_nm_id: str
    product_1c_id: str
    vendor_code: str
    name: str
    stage_name: str
    canonical_stage_code: OnecCanonicalStageCode | None
    qty: float
    unit_cost_rub: float
    cost_total_rub: float


@dataclass(frozen=True)
class OnecStocksSuccess:
    kind: Literal["success"]
    meta: OnecStocksMeta
    item_count: int
    stage_count: int
    dynamic_stage_names: list[str]
    items: list[OnecStocksNormalizedStage]
    detail: str = (
        "1C stage names are preserved as dynamic source values; canonical mapping is "
        "applied only when explicit config is supplied and no aggregation is performed."
    )

    @property
    def snapshot_date(self) -> str:
        return self.meta.date

    @property
    def date(self) -> str:
        return self.meta.date

    @property
    def date_from(self) -> str:
        return self.meta.date

    @property
    def date_to(self) -> str:
        return self.meta.date


@dataclass(frozen=True)
class OnecStocksEmpty:
    kind: Literal["empty"]
    meta: OnecStocksMeta
    item_count: int
    stage_count: int
    dynamic_stage_names: list[str]
    items: list[OnecStocksNormalizedStage]
    detail: str

    @property
    def snapshot_date(self) -> str:
        return self.meta.date

    @property
    def date(self) -> str:
        return self.meta.date

    @property
    def date_from(self) -> str:
        return self.meta.date

    @property
    def date_to(self) -> str:
        return self.meta.date


@dataclass(frozen=True)
class OnecStocksIncomplete:
    kind: Literal["incomplete"]
    meta: OnecStocksMeta
    item_count: int
    stage_count: int
    dynamic_stage_names: list[str]
    items: list[OnecStocksNormalizedStage]
    requested_count: int
    covered_count: int
    missing_nm_ids: list[int]
    detail: str
    temporal_snapshot_acceptable: bool = True

    @property
    def snapshot_date(self) -> str:
        return self.meta.date

    @property
    def date(self) -> str:
        return self.meta.date

    @property
    def date_from(self) -> str:
        return self.meta.date

    @property
    def date_to(self) -> str:
        return self.meta.date


OnecStocksResult = Union[OnecStocksSuccess, OnecStocksEmpty, OnecStocksIncomplete]


@dataclass(frozen=True)
class OnecStocksEnvelope:
    result: OnecStocksResult
