"""Contracts for the factory-order supply operator flow."""

from __future__ import annotations

from dataclasses import dataclass


DATASET_STOCK_FF = "stock_ff"
DATASET_INBOUND_FACTORY_TO_FF = "inbound_factory_to_ff"
DATASET_INBOUND_FF_TO_WB = "inbound_ff_to_wb"

FACTORY_INBOUND_SOURCE_MANUAL_EXCEL = "manual_excel"
FACTORY_INBOUND_SOURCE_SUPPLIER_REGISTRY = "supplier_registry"
SUPPLIER_REGISTRY_FACTORY_TO_FF_ACCEPTANCE_DAYS = 30


@dataclass(frozen=True)
class FactoryOrderSettings:
    prod_lead_time_days: int
    lead_time_factory_to_ff_days: int
    lead_time_ff_to_wb_days: int
    safety_days_mp: int
    safety_days_ff: int
    cycle_order_days: int
    order_batch_qty: int
    report_date_override: str | None
    sales_avg_period_days: int
    factory_inbound_source: str = FACTORY_INBOUND_SOURCE_MANUAL_EXCEL


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
    shipment_name: str = ""


@dataclass(frozen=True)
class FactoryOrderInboundShipmentSummary:
    shipment: str
    total_quantity: float
    acceptance_date: str


@dataclass(frozen=True)
class FactoryOrderSupplierRegistryShipmentSummary:
    shipment_id: str
    shipment_label: str
    invoice_no: str
    invoice_date: str
    total_product_quantity: float
    shipment_date: str
    calculated_acceptance_date: str
    matched_line_count: int
    unmatched_line_count: int
    ambiguous_line_count: int
    missing_shipment_date_line_count: int
    usable_quantity: float


@dataclass(frozen=True)
class FactoryOrderSupplierRegistryDiagnostics:
    shipment_count: int
    product_line_count: int
    matched_line_count: int
    unmatched_line_count: int
    ambiguous_line_count: int
    missing_shipment_date_line_count: int
    invalid_quantity_line_count: int
    usable_line_count: int
    usable_quantity: float


@dataclass(frozen=True)
class FactoryOrderSupplierRegistryInboundState:
    source: str
    status: str
    acceptance_days: int
    shipment_summary: tuple[FactoryOrderSupplierRegistryShipmentSummary, ...]
    diagnostics: FactoryOrderSupplierRegistryDiagnostics
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class FactoryOrderEffectiveInboundRow:
    source: str
    nm_id: int
    sku_comment: str
    quantity: float
    planned_arrival_date: str
    effective_arrival_date: str
    shipment_name: str
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
    shipment_summary: tuple[FactoryOrderInboundShipmentSummary, ...] = ()


@dataclass(frozen=True)
class FactoryOrderUploadResult:
    status: str
    dataset: FactoryOrderDatasetState
    accepted_row_count: int
    ignored_row_count: int
    message: str
    shipment_summary: tuple[FactoryOrderInboundShipmentSummary, ...] = ()


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
    demand_estimation_mode: str = "availability_adjusted"
    sales_avg_period_days: int = 0
    sales_lookup_days: int = 0
    sales_calendar_day_count: int = 0
    valid_sales_day_count: int = 0
    excluded_low_sales_day_count: int = 0
    baseline_daily_sales: float = 0.0
    valid_day_threshold: float = 0.0
    raw_recent_daily_demand: float = 0.0
    earliest_used_sales_date: str = ""
    latest_used_sales_date: str = ""
    demand_warning: str = ""
    demand_notes: tuple[str, ...] = ()


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
    target_window_days: int
    inbound_window_end: str
    coverage_contract_note: str
    settings: FactoryOrderSettings
    factory_inbound_source: str
    datasets: dict[str, FactoryOrderDatasetState]
    manual_factory_inbound_dataset: FactoryOrderDatasetState
    supplier_registry_inbound_summary: FactoryOrderSupplierRegistryInboundState
    effective_inbound_factory_to_ff: list[FactoryOrderEffectiveInboundRow]
    summary: FactoryOrderSummary
    rows: list[FactoryOrderRecommendationRow]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class FactoryOrderStatus:
    status: str
    active_sku_count: int
    coverage_contract_note: str
    factory_inbound_source: str
    datasets: dict[str, FactoryOrderDatasetState]
    manual_factory_inbound_dataset: FactoryOrderDatasetState
    supplier_registry_inbound_summary: FactoryOrderSupplierRegistryInboundState
    last_result: FactoryOrderCalculationResult | None
