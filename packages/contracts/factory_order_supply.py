"""Contracts for the factory-order supply operator flow."""

from __future__ import annotations

from dataclasses import dataclass


DATASET_STOCK_FF = "stock_ff"
DATASET_INBOUND_FACTORY_TO_FF = "inbound_factory_to_ff"
DATASET_INBOUND_FF_TO_WB = "inbound_ff_to_wb"


@dataclass(frozen=True)
class FactoryOrderSettings:
    prod_lead_time_days: int
    lead_time_factory_to_ff_days: int
    lead_time_ff_to_wb_days: int
    safety_days_mp: int
    safety_days_ff: int
    order_batch_qty: int
    report_date_override: str | None
    sales_avg_period_days: int


@dataclass(frozen=True)
class FactoryOrderStockFfRow:
    nm_id: int
    sku_comment: str
    stock_ff: float
    snapshot_date: str | None
    comment: str


@dataclass(frozen=True)
class FactoryOrderInboundRow:
    nm_id: int
    sku_comment: str
    quantity: float
    planned_arrival_date: str
    comment: str


@dataclass(frozen=True)
class FactoryOrderDatasetState:
    dataset_type: str
    label_ru: str
    status: str
    uploaded_at: str | None
    row_count: int
    required: bool
    uploaded_filename: str | None = None
    file_available: bool = False


@dataclass(frozen=True)
class FactoryOrderUploadResult:
    status: str
    dataset: FactoryOrderDatasetState
    accepted_row_count: int
    ignored_row_count: int
    message: str


@dataclass(frozen=True)
class FactoryOrderDatasetDeleteResult:
    status: str
    dataset: FactoryOrderDatasetState
    message: str


@dataclass(frozen=True)
class FactoryOrderRecommendationRow:
    nm_id: int
    sku_comment: str
    recommended_order_qty: int
    daily_demand_total: float
    target_qty: float
    coverage_qty: float
    shortage_qty: float
    stock_total_mp: float
    stock_ff: float
    inbound_factory_to_ff: float
    inbound_ff_to_wb: float


@dataclass(frozen=True)
class FactoryOrderSummary:
    total_qty: int
    estimated_weight: float
    estimated_volume: float


@dataclass(frozen=True)
class FactoryOrderCalculationResult:
    status: str
    calculation_id: str
    calculated_at: str
    report_date: str
    horizon_days: int
    coverage_contract_note: str
    settings: FactoryOrderSettings
    datasets: dict[str, FactoryOrderDatasetState]
    summary: FactoryOrderSummary
    rows: list[FactoryOrderRecommendationRow]


@dataclass(frozen=True)
class FactoryOrderStatus:
    status: str
    active_sku_count: int
    coverage_contract_note: str
    datasets: dict[str, FactoryOrderDatasetState]
    last_result: FactoryOrderCalculationResult | None
