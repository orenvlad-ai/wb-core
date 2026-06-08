"""Contracts for the WB regional supply operator flow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.contracts.factory_order_supply import (
    STOCK_FF_SOURCE_MANUAL_EXCEL,
    FactoryOrderDatasetState,
    FactoryOrderStockFfOnecState,
)


DISTRICT_CENTRAL = "central"
DISTRICT_NORTHWEST = "northwest"
DISTRICT_VOLGA = "volga"
DISTRICT_URAL = "ural"
DISTRICT_SOUTH_CAUCASUS = "south_caucasus"
DISTRICT_FAR_SIBERIA = "far_siberia"

DISTRICT_KEYS = (
    DISTRICT_CENTRAL,
    DISTRICT_NORTHWEST,
    DISTRICT_VOLGA,
    DISTRICT_URAL,
    DISTRICT_SOUTH_CAUCASUS,
    DISTRICT_FAR_SIBERIA,
)

DISTRICT_LABELS_RU = {
    DISTRICT_CENTRAL: "Центральный федеральный округ",
    DISTRICT_NORTHWEST: "Северо-Западный федеральный округ",
    DISTRICT_VOLGA: "Приволжский федеральный округ",
    DISTRICT_URAL: "Уральский федеральный округ",
    DISTRICT_SOUTH_CAUCASUS: "Южный и Северо-Кавказский федеральный округ",
    DISTRICT_FAR_SIBERIA: "Дальневосточный и Сибирский федеральный округ",
}


@dataclass(frozen=True)
class WbRegionalSupplySettings:
    sales_avg_period_days: int
    cycle_supply_days: int
    lead_time_to_region_days: int
    safety_days: int
    order_batch_qty: int
    report_date_override: str | None
    stock_ff_source: str = STOCK_FF_SOURCE_MANUAL_EXCEL
    included_district_keys: tuple[str, ...] = DISTRICT_KEYS


@dataclass(frozen=True)
class WbRegionalSupplyDistrictRow:
    nm_id: int
    sku_comment: str
    full_recommendation_qty: int
    allocated_qty: int
    deficit_qty: int
    current_stock: float
    projected_stock_on_eta: float
    target_stock_after_arrival: float
    daily_demand_total: float
    district_daily_demand: float
    raw_recommendation_qty: float = 0.0
    demand_diagnostics: dict[str, Any] | None = None


@dataclass(frozen=True)
class WbRegionalSupplyDistrictResult:
    district_key: str
    district_name_ru: str
    total_qty: int
    deficit_qty: int
    filename: str
    rows: list[WbRegionalSupplyDistrictRow]


@dataclass(frozen=True)
class WbRegionalSupplySummary:
    total_qty: int
    estimated_weight: float
    estimated_volume: float


@dataclass(frozen=True)
class WbRegionalSupplyCalculationResult:
    status: str
    calculation_id: str
    calculated_at: str
    report_date: str
    horizon_days: int
    active_sku_count: int
    methodology_note: str
    settings: WbRegionalSupplySettings
    stock_ff_source: str
    shared_datasets: dict[str, FactoryOrderDatasetState]
    manual_stock_ff_dataset: FactoryOrderDatasetState
    onec_stock_ff_summary: FactoryOrderStockFfOnecState
    summary: WbRegionalSupplySummary
    districts: list[WbRegionalSupplyDistrictResult]
    diagnostics: dict[str, Any] | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class WbRegionalSupplyStatus:
    status: str
    active_sku_count: int
    methodology_note: str
    stock_ff_source: str
    district_options: tuple[dict[str, str], ...]
    default_included_district_keys: tuple[str, ...]
    shared_datasets: dict[str, FactoryOrderDatasetState]
    manual_stock_ff_dataset: FactoryOrderDatasetState
    onec_stock_ff_summary: FactoryOrderStockFfOnecState
    last_result: WbRegionalSupplyCalculationResult | None
