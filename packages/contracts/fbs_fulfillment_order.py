"""Contracts for independent factory-to-own-FBS-fulfillment planning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


CALCULATION_TYPE_FBS_FULFILLMENT_ORDER = "fbs_fulfillment_order"
SALES_HISTORY_MODE_LAST_N_DAYS = "last_n_days"
SALES_HISTORY_MODE_CUSTOM_PERIOD = "custom_period"
SALES_HISTORY_MODES = {
    SALES_HISTORY_MODE_LAST_N_DAYS,
    SALES_HISTORY_MODE_CUSTOM_PERIOD,
}
DEFAULT_SALES_HISTORY_DAYS = 14
MOSCOW_CITY = "Москва"
INBOUND_SCOPE_SELECTED_FACILITY = "selected_facility"
INBOUND_SCOPE_ALL_ACTIVE = "all_active"
INBOUND_SCOPES = {
    INBOUND_SCOPE_SELECTED_FACILITY,
    INBOUND_SCOPE_ALL_ACTIVE,
}


@dataclass(frozen=True)
class FbsFulfillmentOrderSettings:
    target_facility_id: str
    inbound_scope: str
    production_days: int
    factory_to_target_ff_days: int
    ff_safety_days: int
    order_cycle_days: int
    order_batch_qty: int
    sales_history_mode: str
    sales_avg_period_days: int | None
    sales_date_from: str | None
    sales_date_to: str | None
    report_date_override: str | None


@dataclass(frozen=True)
class FbsFulfillmentOrderRow:
    nm_id: int
    sku_comment: str
    recommended_order_qty: int
    national_daily_demand: float
    target_qty: float
    coverage_qty: float
    shortage_qty: float
    selected_facility_physical_fbs: int
    selected_facility_reserved_fbs: int
    selected_facility_available_fbs: int
    remaining_active_inbound_qty: float
    demand_estimation_mode: str
    sales_history_mode: str
    sales_avg_period_days: int | None
    sales_date_from: str
    sales_date_to: str
    sales_calendar_day_count: int
    used_trading_day_count: int
    excluded_day_count: int
    included_sales_dates: tuple[str, ...]
    excluded_sales_dates: tuple[str, ...]
    baseline_daily_sales: float
    valid_day_threshold: float
    raw_window_daily_demand: float
    demand_warning: str
    demand_notes: tuple[str, ...]


@dataclass(frozen=True)
class FbsFulfillmentOrderSummary:
    total_qty: int
    estimated_weight: float
    estimated_volume: float
    sales_calendar_day_count: int
    used_trading_days_min: int
    used_trading_days_max: int


@dataclass(frozen=True)
class FbsFulfillmentOrderResult:
    status: str
    calculation_id: str
    calculated_at: str
    report_date: str
    target_facility_id: str
    target_facility_name: str
    national_demand_scope: str
    wb_stock_used: bool
    horizon_days: int
    settings: FbsFulfillmentOrderSettings
    sales_window: dict[str, Any]
    facility_readiness: dict[str, Any]
    inbound_coverage: dict[str, Any]
    summary: FbsFulfillmentOrderSummary
    rows: list[FbsFulfillmentOrderRow]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class FbsFulfillmentOrderStatus:
    status: str
    active_sku_count: int
    national_demand_scope: str
    wb_stock_used: bool
    facilities: tuple[dict[str, Any], ...]
    sales_history_coverage: dict[str, Any]
    defaults: dict[str, Any]
    last_result: dict[str, Any] | None
