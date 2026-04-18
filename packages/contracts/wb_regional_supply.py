"""Contracts for the WB regional supply operator flow."""

from __future__ import annotations

from dataclasses import dataclass

from packages.contracts.factory_order_supply import FactoryOrderDatasetState


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


@dataclass(frozen=True)
class WbRegionalSupplySettings:
    sales_avg_period_days: int
    supply_horizon_days: int
    lead_time_to_region_days: int
    safety_days: int
    order_batch_qty: int
    report_date_override: str | None


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
    shared_datasets: dict[str, FactoryOrderDatasetState]
    summary: WbRegionalSupplySummary
    districts: list[WbRegionalSupplyDistrictResult]


@dataclass(frozen=True)
class WbRegionalSupplyStatus:
    status: str
    active_sku_count: int
    methodology_note: str
    shared_datasets: dict[str, FactoryOrderDatasetState]
    last_result: WbRegionalSupplyCalculationResult | None
