"""Operator SKU management: forecast read model and guarded single-target actions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
import json
import math
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

from packages.adapters.spp_proxy_block import HttpBackedPublicWbCardBuyerPriceSource, SppProxySource
from packages.application.demand_estimation import estimate_availability_adjusted_demand, sales_lookup_days
from packages.application.sheet_vitrina_v1_ads import SheetVitrinaV1AdsBlock
from packages.application.stocks_block import (
    build_wb_warehouse_exclusion,
)
from packages.application.vitrina_incident_rematerialization import (
    DEFAULT_MAX_DATES as VITRINA_INCIDENT_REMATERIALIZATION_MAX_DATES,
    apply_vitrina_incident_rematerialization,
    plan_vitrina_incident_rematerialization,
)
from packages.application.wb_incident_policy import (
    WbIncidentPolicyError,
    build_incident_stock_projection,
    canonical_seller_id,
    get_latest_policy_state,
    policy_badge,
    save_policy_revision,
)
from packages.application.wb_prices_management import WbPricesManagementBlock, normalize_goods_payload
from packages.business_time import current_business_date_iso
from packages.contracts.spp_proxy_block import SppProxyRequest
from packages.contracts.stocks_block import StocksRequest
from packages.contracts.supplier_shipments import (
    MATCH_STATUS_MATCHED,
    MATCH_STATUS_MATCHED_BY_BARCODE,
    MATCH_STATUS_MATCHED_BY_COMPATIBILITY,
)


SKU_MANAGEMENT_CONFIG_KEY = "sku_management"
SKU_MANAGEMENT_CONFIG_SCHEMA_VERSION = 2
PRICE_PARAMETER = "seller_price"
BID_PARAMETER = "advertising_bid"

SKU_CUMULATIVE_METRIC_FIELDS = (
    "view_count",
    "openCount",
    "cartCount",
    "addToCartConversion",
    "cartToOrderConversion",
    "orderCount",
    "orderSum",
    "ads_drr",
    "ads_drr_attributed",
    "ads_views",
    "ads_clicks",
    "ads_atbs",
    "ads_orders",
    "ads_sum",
    "ads_sum_price",
    "ads_cpc",
    "ads_ctr",
    "ads_cr",
    "proxy_profit_3_rub",
    "proxy_profit_rub",
    "proxy_margin_3_pct",
    "proxy_margin_pct",
)
SKU_SNAPSHOT_METRIC_FIELDS = (
    "seller_price",
    "buyer_price_rub",
    "spp_proxy",
    "promo_participation",
    "promo_count_by_price",
    "campaigns",
    "advertising_bid",
)


class SkuManagementError(ValueError):
    def __init__(self, message: str, *, http_status: int = 400, payload: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.http_status = int(http_status)
        self.payload = dict(payload or {})


@dataclass(frozen=True)
class ForecastSettings:
    sales_avg_period_days: int = 14
    forecast_horizon_days: int = 90
    future_order_period_days: int = 30
    production_lead_days: int = 30
    factory_to_ff_lead_days: int = 30
    ff_to_wb_lead_days: int = 7
    safety_stock_days: int = 14
    price_stabilization_days: int = 3
    bid_stabilization_days: int = 3
    cross_warnings_enabled: bool = True
    order_batch_qty: int = 100


DEFAULT_TABLE_PREFERENCES: dict[str, Any] = {
    "visible_columns": [],
    "column_order": [],
    "column_widths": {},
    "filters": {},
    "sort": [{"key": "risk_rank", "direction": "desc"}, {"key": "deficit_date", "direction": "asc"}],
}
TABLE_COLUMN_KEYS = {
    "product", "risk", "deficit_date", "coverage_pct", "deficit_units",
    "nearest_inbound", "seller_price", "buyer_price", "spp_proxy", "promo",
    "campaigns", "current_bid", "ads_drr", "ads_drr_attributed", "ads_spend_rub", "funnel", "orders",
    "profit_rub", "margin_pct", "last_price_change_at", "last_bid_change_at", "diagnostics",
}
TABLE_SORT_KEYS = TABLE_COLUMN_KEYS | {"risk_rank"}
TABLE_FILTER_KEYS = {"search"}
TABLE_MANDATORY_COLUMNS = ("product",)


@dataclass(frozen=True)
class ForecastInbound:
    arrival_date: str
    quantity: float
    source: str
    source_id: str = ""
    district_key: str = ""
    synthetic: bool = False
    consumes_initial_ff: bool = False
    initial_ff_reservation_qty: float | None = None


def select_nearest_supplier_inbound(
    inbounds: Sequence[Mapping[str, Any]],
    *,
    as_of_date: str,
) -> dict[str, Any] | None:
    """Select the nearest invoice projection already admitted to the forecast timeline."""

    if not _is_iso_date(as_of_date):
        return None
    today = date.fromisoformat(as_of_date)
    eligible: list[dict[str, Any]] = []
    for raw in inbounds:
        arrival_date = str(raw.get("arrival_date") or "")
        invoice_no = str(raw.get("invoice_no") or "").strip()
        quantity = _optional_float(raw.get("quantity"))
        if (
            not invoice_no
            or not _is_iso_date(arrival_date)
            or date.fromisoformat(arrival_date) < today
            or quantity is None
            or quantity <= 0
        ):
            continue
        eligible.append(
            {
                "shipment_id": str(raw.get("shipment_id") or ""),
                "invoice_no": invoice_no,
                "arrival_date": arrival_date,
                "quantity": round(quantity, 2),
                "date_source": str(raw.get("date_source") or ""),
            }
        )
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda item: (
            item["arrival_date"],
            item["invoice_no"].casefold(),
            item["shipment_id"],
        ),
    )


def validate_forecast_settings(payload: Mapping[str, Any] | None) -> ForecastSettings:
    raw = dict(payload or {})
    period = _integer(raw.get("sales_avg_period_days", 14), "sales_avg_period_days", 7, 60)
    if period not in {7, 14, 30, 60}:
        raise SkuManagementError("sales_avg_period_days must be one of 7, 14, 30, 60", http_status=422)
    return ForecastSettings(
        sales_avg_period_days=period,
        forecast_horizon_days=_integer(raw.get("forecast_horizon_days", 90), "forecast_horizon_days", 7, 365),
        future_order_period_days=_integer(raw.get("future_order_period_days", 30), "future_order_period_days", 1, 180),
        production_lead_days=_integer(raw.get("production_lead_days", 30), "production_lead_days", 0, 365),
        factory_to_ff_lead_days=_integer(raw.get("factory_to_ff_lead_days", 30), "factory_to_ff_lead_days", 0, 365),
        ff_to_wb_lead_days=_integer(raw.get("ff_to_wb_lead_days", 7), "ff_to_wb_lead_days", 0, 365),
        safety_stock_days=_integer(raw.get("safety_stock_days", 14), "safety_stock_days", 0, 180),
        price_stabilization_days=_integer(raw.get("price_stabilization_days", 3), "price_stabilization_days", 0, 365),
        bid_stabilization_days=_integer(raw.get("bid_stabilization_days", 3), "bid_stabilization_days", 0, 365),
        cross_warnings_enabled=_boolean(raw.get("cross_warnings_enabled", True)),
        order_batch_qty=_integer(raw.get("order_batch_qty", 100), "order_batch_qty", 1, 100000),
    )


def calculate_depletion_forecast(
    *,
    as_of_date: str,
    stock_wb: float | None,
    stock_ff: float | None,
    daily_demand: float | None,
    settings: ForecastSettings,
    real_inbounds: Sequence[ForecastInbound] = (),
    districts: Mapping[str, Mapping[str, Any]] | None = None,
    evidence_warnings: Sequence[str] = (),
) -> dict[str, Any]:
    """Sequential calculation-only WB availability timeline.

    Current FF stock is not treated as instantly saleable on WB. Its unreserved
    part becomes available only after the configured FF -> WB lead time. A WB
    supply that has not yet produced an FF-ledger debit consumes that same
    initial FF pool instead of creating inventory a second time.
    """

    start = date.fromisoformat(as_of_date)
    warnings = [str(item) for item in evidence_warnings if str(item).strip()]
    if stock_wb is None:
        warnings.append("Текущий остаток WB недоступен")
    if stock_ff is None:
        warnings.append("Текущий остаток ФФ недоступен")
    if daily_demand is None:
        warnings.append("Интенсивность продаж недоступна")
    elif daily_demand < 0:
        warnings.append("Интенсивность продаж не может быть отрицательной")
    if stock_wb is None or stock_ff is None or daily_demand is None or daily_demand < 0:
        return {
            "risk": "unknown",
            "risk_rank": -1,
            "deficit_date": None,
            "minimum_stock": None,
            "deficit_units": None,
            "coverage_pct": None,
            "first_problem_district": "unknown",
            "reason": "; ".join(warnings) or "Недостаточно evidence для расчёта",
            "quality": "unknown",
            "quality_warnings": warnings,
            "daily_demand": daily_demand,
            "timeline": [],
            "synthetic_orders": [],
            "regional_status": "unknown",
        }

    initial_ff = float(stock_ff)
    balance = float(stock_wb)
    demand = float(daily_demand)
    if balance < 0:
        warnings.append("Расчётный остаток WB отрицательный")
    if initial_ff < 0:
        warnings.append("Расчётный остаток ФФ отрицательный")
    safety_units = demand * settings.safety_stock_days
    horizon_end = start + timedelta(days=settings.forecast_horizon_days - 1)
    scheduled: dict[date, list[ForecastInbound]] = {}
    real_plan_dates: list[date] = []
    seen_real_keys: set[tuple[str, str, str, str]] = set()
    reserved_initial_ff_qty = 0.0
    for inbound in real_inbounds:
        if inbound.quantity <= 0:
            continue
        if not _is_iso_date(inbound.arrival_date):
            warnings.append(
                f"Inbound evidence без usable даты исключён: {inbound.source}:{inbound.source_id or 'unknown'}"
            )
            continue
        arrival = date.fromisoformat(inbound.arrival_date)
        if arrival < start:
            warnings.append(
                f"Просроченный inbound plan исключён: {inbound.source}:{inbound.source_id or 'unknown'} ({inbound.arrival_date})"
            )
            continue
        identity = (inbound.source, inbound.source_id, inbound.arrival_date, inbound.district_key)
        if identity in seen_real_keys:
            warnings.append(f"Дубликат inbound evidence исключён: {inbound.source}:{inbound.source_id}")
            continue
        seen_real_keys.add(identity)
        scheduled.setdefault(arrival, []).append(inbound)
        real_plan_dates.append(arrival)
        if inbound.consumes_initial_ff:
            reserved_initial_ff_qty += max(
                float(
                    inbound.initial_ff_reservation_qty
                    if inbound.initial_ff_reservation_qty is not None
                    else inbound.quantity
                ),
                0.0,
            )

    available_initial_ff = max(initial_ff, 0.0)
    generic_initial_ff_qty = max(available_initial_ff - reserved_initial_ff_qty, 0.0)
    reserved_initial_ff_remaining = min(available_initial_ff, reserved_initial_ff_qty)
    if reserved_initial_ff_qty > available_initial_ff:
        warnings.append(
            "WB supply reservations exceed the current FF balance; transfers are capped by authoritative FF stock"
        )
    if generic_initial_ff_qty > 0:
        initial_ff_arrival = start + timedelta(days=settings.ff_to_wb_lead_days)
        if initial_ff_arrival <= horizon_end:
            scheduled.setdefault(initial_ff_arrival, []).append(
                ForecastInbound(
                    arrival_date=initial_ff_arrival.isoformat(),
                    quantity=generic_initial_ff_qty,
                    source="current_ff_stock",
                    source_id="authoritative_ff_balance",
                )
            )

    real_plan_end = max(real_plan_dates, default=start - timedelta(days=1))
    synthetic_order_start = max(start, real_plan_end + timedelta(days=1))
    total_lead = settings.production_lead_days + settings.factory_to_ff_lead_days + settings.ff_to_wb_lead_days
    target_level = demand * (settings.future_order_period_days + settings.safety_stock_days)
    first_deficit_date: str | None = None
    minimum_stock = balance
    timeline: list[dict[str, Any]] = []
    synthetic_orders: list[dict[str, Any]] = []
    effective_district_inbounds: list[ForecastInbound] = []

    current = start
    while current <= horizon_end:
        arrivals = scheduled.get(current, [])
        inbound_qty = 0.0
        inbound_sources: list[str] = []
        for item in arrivals:
            quantity = float(item.quantity)
            if item.consumes_initial_ff:
                applied = min(quantity, reserved_initial_ff_remaining)
                reserved_initial_ff_remaining -= applied
                if applied + 1e-9 < quantity:
                    warnings.append(
                        f"WB supply {item.source_id or 'unknown'} applied partially because FF evidence is insufficient"
                    )
                quantity = applied
            if quantity <= 0:
                continue
            inbound_qty += quantity
            inbound_sources.append(item.source)
            if item.district_key and not item.synthetic:
                effective_district_inbounds.append(
                    ForecastInbound(
                        arrival_date=item.arrival_date,
                        quantity=quantity,
                        source=item.source,
                        source_id=item.source_id,
                        district_key=item.district_key,
                    )
                )
        balance += inbound_qty

        if current >= synthetic_order_start and (current - synthetic_order_start).days % settings.future_order_period_days == 0:
            arrival_date = current + timedelta(days=total_lead)
            if arrival_date <= horizon_end:
                future_items = [
                    item
                    for scheduled_date, items in scheduled.items()
                    if current < scheduled_date <= arrival_date
                    for item in items
                ]
                known_before_arrival = sum(
                    float(item.quantity)
                    for item in future_items
                    if not item.consumes_initial_ff
                ) + min(
                    sum(float(item.quantity) for item in future_items if item.consumes_initial_ff),
                    reserved_initial_ff_remaining,
                )
                projected_on_arrival = balance - demand * total_lead + known_before_arrival
                shortage = max(target_level - projected_on_arrival, 0.0)
                quantity = int(math.ceil(shortage / settings.order_batch_qty) * settings.order_batch_qty) if shortage else 0
                if quantity:
                    item = ForecastInbound(
                        arrival_date=arrival_date.isoformat(),
                        quantity=float(quantity),
                        source="synthetic_factory_order",
                        source_id=f"calculation:{current.isoformat()}",
                        synthetic=True,
                    )
                    if arrival_date == current:
                        inbound_qty += float(quantity)
                        balance += float(quantity)
                        inbound_sources.append(item.source)
                    else:
                        scheduled.setdefault(arrival_date, []).append(item)
                    synthetic_orders.append(
                        {
                            "order_date": current.isoformat(),
                            "arrival_date": arrival_date.isoformat(),
                            "quantity": quantity,
                            "calculation_only": True,
                        }
                    )

        balance -= demand
        minimum_stock = min(minimum_stock, balance)
        if first_deficit_date is None and balance < safety_units:
            first_deficit_date = current.isoformat()

        timeline.append(
            {
                "date": current.isoformat(),
                "inbound_qty": round(inbound_qty, 2),
                "inbound_sources": inbound_sources,
                "demand_qty": round(demand, 4),
                "ending_stock": round(balance, 2),
            }
        )
        current += timedelta(days=1)

    deficit_units = max(safety_units - minimum_stock, 0.0)
    coverage_pct = None if safety_units <= 0 else max(minimum_stock, 0.0) / safety_units * 100.0
    days_to_deficit = (
        (date.fromisoformat(first_deficit_date) - start).days if first_deficit_date else None
    )
    if first_deficit_date is None:
        risk, rank = "low", 0
    elif days_to_deficit is not None and days_to_deficit <= total_lead:
        risk, rank = "high", 2
    else:
        risk, rank = "medium", 1

    district_problem, regional_status = _first_problem_district(
        as_of_date=start,
        horizon_end=horizon_end,
        safety_days=settings.safety_stock_days,
        districts=districts or {},
        inbounds=effective_district_inbounds,
    )
    quality = "complete" if not warnings else "partial"
    reason_parts = []
    if first_deficit_date:
        reason_parts.append(f"остаток опустится ниже страхового норматива {first_deficit_date}")
    else:
        reason_parts.append("дефицит внутри горизонта не прогнозируется")
    if synthetic_orders:
        reason_parts.append(f"учтено calculation-only заказов: {len(synthetic_orders)}")
    if regional_status == "unknown":
        reason_parts.append("региональный риск unknown: authoritative evidence отсутствует")
    if warnings:
        reason_parts.append("evidence неполный")
    return {
        "risk": risk,
        "risk_rank": rank,
        "deficit_date": first_deficit_date,
        "minimum_stock": round(minimum_stock, 2),
        "deficit_units": round(deficit_units, 2),
        "coverage_pct": None if coverage_pct is None else round(coverage_pct, 2),
        "first_problem_district": district_problem,
        "regional_status": regional_status,
        "reason": "; ".join(reason_parts),
        "quality": quality,
        "quality_warnings": warnings,
        "daily_demand": round(demand, 4),
        "safety_stock_units": round(safety_units, 2),
        "timeline": timeline,
        "synthetic_orders": synthetic_orders,
    }


def choose_target_price_configuration(
    *,
    target_seller_price: Any,
    current_price: Any,
    current_discount: Any,
) -> dict[str, int | float]:
    """Find an exact integer WB price/discount pair deterministically."""

    target = _money_decimal(target_seller_price, "target_seller_price")
    if target <= 0:
        raise SkuManagementError("target_seller_price must be > 0", http_status=422)
    old_price = int(_money_decimal(current_price, "current_price"))
    old_discount = int(_money_decimal(current_discount, "current_discount"))
    candidates: list[tuple[tuple[int, int, int], int, int, Decimal]] = []
    for discount in range(100):
        divisor = Decimal(100 - discount)
        raw_price = target * Decimal(100) / divisor
        for price in {
            int(raw_price.to_integral_value(rounding=ROUND_HALF_UP)),
            int(raw_price.to_integral_value(rounding=ROUND_FLOOR)),
            int(raw_price.to_integral_value(rounding=ROUND_CEILING)),
        }:
            if price <= 0:
                continue
            seller = (Decimal(price) * divisor / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if seller != target:
                continue
            rank = (abs(discount - old_discount), abs(price - old_price), discount)
            candidates.append((rank, price, discount, seller))
    if not candidates:
        raise SkuManagementError(
            "target seller price cannot be represented exactly by integer WB price and discount",
            http_status=422,
        )
    _, price, discount, seller = min(candidates, key=lambda item: item[0])
    return {"price": price, "discount": discount, "seller_price": float(seller)}


class SkuManagementBlock:
    """Composes canonical reads and guarded single-target write contours."""

    def __init__(
        self,
        *,
        runtime: Any,
        runtime_dir: Path,
        prices_block: WbPricesManagementBlock,
        ads_block: SheetVitrinaV1AdsBlock,
        stocks_block: Any | None = None,
        sales_history: Any | None = None,
        buyer_price_source: SppProxySource | None = None,
        now_factory: Callable[[], datetime] | None = None,
        timestamp_factory: Callable[[], str] | None = None,
        sleep: Callable[[float], None] | None = None,
        monotonic_factory: Callable[[], float] | None = None,
        readback_attempts: int = 8,
        readback_delay_seconds: float = 0.35,
        readback_deadline_seconds: float = 20.0,
    ) -> None:
        self.runtime = runtime
        self.runtime_dir = runtime_dir
        self.prices_block = prices_block
        self.ads_block = ads_block
        self.stocks_block = stocks_block
        self.sales_history = sales_history
        self.buyer_price_source = buyer_price_source or HttpBackedPublicWbCardBuyerPriceSource()
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self.timestamp_factory = timestamp_factory or (lambda: datetime.now(timezone.utc).isoformat())
        self.sleep = sleep or time.sleep
        self.monotonic_factory = monotonic_factory or time.monotonic
        self.readback_attempts = max(int(readback_attempts), 1)
        self.readback_delay_seconds = max(float(readback_delay_seconds), 0.0)
        self.readback_deadline_seconds = max(float(readback_deadline_seconds), 0.0)
        self._preview_dir = runtime_dir / "sheet_vitrina_v1_sku_management" / "previews"

    def _measure_phase(
        self,
        phases_ms: dict[str, float],
        name: str,
        operation: Callable[[], Any],
    ) -> Any:
        started = self.monotonic_factory()
        try:
            return operation()
        finally:
            phases_ms[name] = round(
                max(self.monotonic_factory() - started, 0.0) * 1000,
                2,
            )

    def _diagnostics(
        self,
        *,
        started: float,
        phases_ms: Mapping[str, float],
        remote_call_counts: Mapping[str, int],
        skipped_paths: Sequence[str] = (),
    ) -> dict[str, Any]:
        return {
            "total_ms": round(
                max(self.monotonic_factory() - started, 0.0) * 1000,
                2,
            ),
            "phases_ms": dict(phases_ms),
            "remote_call_counts": {
                str(key): int(value)
                for key, value in remote_call_counts.items()
            },
            "skipped_paths": [str(item) for item in skipped_paths],
        }

    def get_settings(self, *, user_key: str) -> dict[str, Any]:
        record = self.runtime.load_sheet_vitrina_user_config(user_key=user_key, config_key=SKU_MANAGEMENT_CONFIG_KEY)
        config = dict(record.get("config") or {}) if record.get("status") == "ok" else {}
        forecast = validate_forecast_settings(config.get("forecast") if isinstance(config.get("forecast"), Mapping) else {})
        table = _sanitize_table_preferences(config.get("table") if isinstance(config.get("table"), Mapping) else {})
        return {
            "status": "ok",
            "revision": int(record.get("revision") or 0),
            "updated_at": str(record.get("updated_at") or ""),
            "forecast": asdict(forecast),
            "table": table,
            "canonical_store": "server_runtime_user_config",
        }

    def save_settings(self, *, user_key: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        forecast = validate_forecast_settings(payload.get("forecast") if isinstance(payload.get("forecast"), Mapping) else {})
        table = _sanitize_table_preferences(payload.get("table") if isinstance(payload.get("table"), Mapping) else {})
        saved = self.runtime.save_sheet_vitrina_user_config(
            user_key=user_key,
            config_key=SKU_MANAGEMENT_CONFIG_KEY,
            schema_version=SKU_MANAGEMENT_CONFIG_SCHEMA_VERSION,
            payload={"forecast": asdict(forecast), "table": table},
            updated_at=self.timestamp_factory(),
            expected_revision=_optional_int(payload.get("base_revision")),
        )
        if saved.get("status") == "conflict":
            raise SkuManagementError("sku management settings revision conflict", http_status=409, payload=saved)
        return self.get_settings(user_key=user_key)

    def get_warehouse_exclusion_settings(self, *, user_key: str) -> dict[str, Any]:
        del user_key  # policy ownership is seller/account-level, never per browser user
        snapshot_date = current_business_date_iso(self.now_factory())
        policy = get_latest_policy_state(self.runtime, snapshot_date=snapshot_date)
        excluded = list(policy.get("warehouse_ids") or [])
        return {
            **policy,
            "status": "ok",
            "exists": bool(int(policy.get("revision") or 0) or policy.get("migration_pending")),
            "revision": int(policy.get("revision") or 0),
            "updated_at": str(policy.get("created_at") or ""),
            "excluded_wb_warehouse_ids": excluded,
            "warehouse_entries": list(policy.get("configured_warehouse_entries") or policy.get("warehouse_entries") or []),
            "canonical_business_date": snapshot_date,
            "effective_excluded_wb_warehouse_ids": list(
                policy.get("effective_warehouse_ids")
                if "effective_warehouse_ids" in policy
                else (policy.get("warehouse_ids") or [])
            ),
            "canonical_store": "server_runtime_wb_incident_policy",
            "badge": policy_badge(policy),
        }

    def save_warehouse_exclusion_settings(
        self,
        *,
        user_key: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        if self.stocks_block is None:
            raise SkuManagementError("stocks contour is unavailable", http_status=503)
        snapshot_date = current_business_date_iso(self.now_factory())
        active = self._active_skus()
        stock_result = self.stocks_block.execute(
            StocksRequest(
                snapshot_type="stocks",
                snapshot_date=snapshot_date,
                nm_ids=[int(item["nm_id"]) for item in active],
            )
        ).result
        if getattr(stock_result, "kind", "") != "success":
            raise SkuManagementError(
                "Нельзя применить политику: официальный снимок WB неполный",
                http_status=409,
            )
        options_contract = build_wb_warehouse_exclusion(
            items=list(getattr(stock_result, "items", []) or []),
            warehouse_rows=list(getattr(stock_result, "warehouse_rows", []) or []),
            excluded_warehouse_ids=(),
            snapshot_date=str(getattr(stock_result, "snapshot_date", "") or snapshot_date),
            fetched_at=str(getattr(stock_result, "fetched_at", "") or ""),
            pagination_complete=bool(getattr(stock_result, "pagination_complete", False)),
            raw_rows_digest=str(getattr(stock_result, "raw_rows_digest", "") or ""),
            require_complete=True,
        )
        normalized_payload = dict(payload)
        seller_id = canonical_seller_id()
        previous_record = self.runtime.load_latest_wb_incident_policy(
            seller_id=seller_id,
        )
        previous_policy = get_latest_policy_state(
            self.runtime,
            snapshot_date=snapshot_date,
            seller_id=seller_id,
        )
        if "active" not in normalized_payload and "effective_from" not in normalized_payload:
            legacy_ids = list(normalized_payload.get("excluded_wb_warehouse_ids") or [])
            normalized_payload.update(
                {
                    "active": bool(legacy_ids),
                    "reason": "Миграция совместимой настройки исключения складов",
                    "effective_from": snapshot_date,
                    "effective_to": "",
                    "status": "active" if legacy_ids else "disabled",
                }
            )
        policy_saved_at = self.timestamp_factory()
        normalized_payload.setdefault("change_effective_from", snapshot_date)
        try:
            saved_policy = save_policy_revision(
                self.runtime,
                payload=normalized_payload,
                actor=user_key,
                warehouse_options=list(options_contract.get("options") or []),
                timestamp=policy_saved_at,
            )
        except WbIncidentPolicyError as exc:
            status = 409 if "conflict" in str(exc).casefold() else 422
            raise SkuManagementError(str(exc), http_status=status) from exc
        changed_from = str(saved_policy.get("changed_from") or "")
        if str(saved_policy.get("idempotency_status") or "") == "T0":
            rematerialization = {
                "status": "T0",
                "date_from": "",
                "date_to": snapshot_date,
                "snapshot_count": 0,
                "changed_cells": 0,
            }
        else:
            rematerialization = self._rematerialize_incident_policy_ready_dates(
                user_key=user_key,
                revision=int(saved_policy.get("revision") or 0),
                effective_from=changed_from or snapshot_date,
                date_to=snapshot_date,
                generated_at=policy_saved_at,
            )
        recovery: dict[str, Any] = {}
        policy_apply_status = "applied"
        if str(rematerialization.get("status") or "") == "failed":
            prior = (
                dict(previous_record)
                if isinstance(previous_record, Mapping)
                and previous_record.get("status") == "ok"
                else dict(previous_policy)
            )
            prior_entries = list(
                prior.get("warehouse_entries")
                or previous_policy.get("configured_warehouse_entries")
                or []
            )
            prior_ids = sorted(
                {
                    int(item.get("warehouse_id") or 0)
                    for item in prior_entries
                    if int(item.get("warehouse_id") or 0) > 0
                    and not str(item.get("effective_to_exclusive") or "")
                }
            )
            prior_identities = list(prior.get("warehouse_identities") or [])
            if not prior_identities and prior_ids:
                option_by_id = {
                    int(item.get("warehouse_id") or 0): dict(item)
                    for item in options_contract.get("options") or []
                }
                prior_identities = [
                    {
                        "warehouse_id": warehouse_id,
                        "warehouse_name": str(
                            option_by_id.get(warehouse_id, {}).get("warehouse_name")
                            or ""
                        ),
                        "effective_from": next(
                            str(item.get("effective_from") or "")
                            for item in prior_entries
                            if int(item.get("warehouse_id") or 0) == warehouse_id
                            and not str(item.get("effective_to_exclusive") or "")
                        ),
                    }
                    for warehouse_id in prior_ids
                ]
            restored = self.runtime.append_wb_incident_policy_revision(
                seller_id=seller_id,
                active=bool(
                    prior.get("active")
                    if previous_record.get("status") == "ok"
                    else previous_policy.get("configured_active")
                    if "configured_active" in previous_policy
                    else previous_policy.get("active")
                ),
                warehouse_ids=prior_ids,
                warehouse_identities=prior_identities,
                warehouse_entries=prior_entries,
                reason=str(prior.get("reason") or ""),
                effective_from=changed_from or snapshot_date,
                effective_to=str(prior.get("effective_to") or ""),
                policy_status=str(prior.get("policy_status") or "disabled"),
                actor="system:incident-policy-last-good-recovery",
                created_at=self.timestamp_factory(),
                source="incident_policy_v2_last_good_recovery",
                legacy_payloads=list(prior.get("legacy_payloads") or []),
                expected_revision=int(saved_policy.get("revision") or 0),
            )
            if restored.get("status") == "conflict":
                raise SkuManagementError(
                    "Incident policy replay failed and last-good recovery revision conflicted",
                    http_status=500,
                )
            policy_apply_status = "failed_reverted_to_last_good"
            recovery = {
                "status": "last_good_restored",
                "attempted_revision": int(saved_policy.get("revision") or 0),
                "recovery_revision": int(restored.get("revision") or 0),
                "changed_from": changed_from,
            }
        return {
            **self.get_warehouse_exclusion_settings(user_key=user_key),
            "idempotency_status": (
                policy_apply_status
                if policy_apply_status != "applied"
                else str(saved_policy.get("idempotency_status") or "applied")
            ),
            "policy_apply_status": policy_apply_status,
            "changed_from": changed_from,
            "rematerialization": rematerialization,
            "last_good_recovery": recovery,
            "dependent_replay": {
                "status": (
                    "failed_reverted_to_last_good"
                    if policy_apply_status != "applied"
                    else "T0"
                    if str(saved_policy.get("idempotency_status") or "") == "T0"
                    else "applied"
                ),
                "date_from": changed_from,
                "contours": [
                    "supply_calculation_read_through",
                    "sku_management_read_through",
                    "vitrina_effective_availability",
                    "vitrina_historical_projection",
                ],
                "physical_wb_mutated": False,
                "warehouse_capital_mutated": False,
                "warehouse_wac_mutated": False,
            },
        }

    def _rematerialize_incident_policy_ready_dates(
        self,
        *,
        user_key: str,
        revision: int,
        effective_from: str,
        date_to: str,
        generated_at: str,
    ) -> dict[str, Any]:
        if effective_from > date_to:
            return {
                "status": "scheduled",
                "reason": "policy effective date is in the future",
                "date_from": effective_from,
                "date_to": date_to,
            }
        try:
            reviewed_plan, _ = plan_vitrina_incident_rematerialization(
                self.runtime,
                date_from=effective_from,
                date_to=date_to,
                max_dates=VITRINA_INCIDENT_REMATERIALIZATION_MAX_DATES,
                generated_at=generated_at,
            )
            if not reviewed_plan.get("apply_allowed"):
                return {
                    "status": "no_ready_dates",
                    "date_from": reviewed_plan.get("date_from_effective"),
                    "date_to": reviewed_plan.get("date_to"),
                    "snapshot_count": 0,
                    "changed_cells": 0,
                    "fingerprint": reviewed_plan.get("fingerprint"),
                }
            applied = apply_vitrina_incident_rematerialization(
                self.runtime,
                reviewed_plan=reviewed_plan,
                fingerprint=str(reviewed_plan.get("fingerprint") or ""),
                approval_reference=f"policy-apply:revision:{revision}",
                actor=user_key,
                applied_at=generated_at,
            )
            return {
                "status": str(applied.get("status") or "applied"),
                "date_from": reviewed_plan.get("date_from_effective"),
                "date_to": reviewed_plan.get("date_to"),
                "snapshot_count": reviewed_plan.get("snapshot_count"),
                "changed_snapshot_count": reviewed_plan.get(
                    "changed_snapshot_count"
                ),
                "changed_cells": reviewed_plan.get("changed_cells"),
                "fingerprint": reviewed_plan.get("fingerprint"),
                "readback_status": applied.get("readback_status"),
                "readback_changed_cells": applied.get("readback_changed_cells"),
                "raw_stock_truth_mutated": False,
                "non_target_invariant": applied.get("non_target_invariant"),
            }
        except Exception as exc:
            return {
                "status": "failed",
                "date_from": effective_from,
                "date_to": date_to,
                "error": str(exc),
                "retry_contract": (
                    "repo-owned bounded vitrina incident rematerialization dry-run/apply"
                ),
            }

    def build_table(
        self,
        *,
        user_key: str,
        only_nm_ids: Sequence[int] | None = None,
    ) -> dict[str, Any]:
        settings_payload = self.get_settings(user_key=user_key)
        warehouse_settings = self.get_warehouse_exclusion_settings(user_key=user_key)
        excluded_warehouse_ids = tuple(
            int(item)
            for item in warehouse_settings.get("effective_excluded_wb_warehouse_ids") or []
            if warehouse_settings.get("active")
        )
        settings = validate_forecast_settings(settings_payload["forecast"])
        active = self._active_skus()
        if only_nm_ids is not None:
            requested = {int(item) for item in only_nm_ids}
            active = [item for item in active if int(item["nm_id"]) in requested]
            missing = sorted(requested - {int(item["nm_id"]) for item in active})
            if missing:
                raise SkuManagementError(
                    "SKU is not active in canonical config_v2",
                    http_status=404,
                    payload={"missing_nm_ids": missing},
                )
        nm_ids = [int(item["nm_id"]) for item in active]
        cumulative_date = (
            date.fromisoformat(current_business_date_iso(self.now_factory())) - timedelta(days=2)
        ).isoformat()
        commercial = self._commercial_projection(nm_ids, target_date=cumulative_date)
        snapshot_metrics = self._snapshot_projection(nm_ids)
        current_buyer_prices = self._current_buyer_price_projection(nm_ids)
        source_warnings: list[str] = []
        try:
            prices_payload = self.prices_block.build_goods_table()
        except Exception as exc:
            prices_payload = {"rows": []}
            source_warnings.append(f"price evidence error: {exc}")
        price_by_nm = {int(row["nmID"]): row for row in prices_payload.get("rows", [])}
        try:
            ads_payload = self.ads_block.build_sku_table()
        except Exception as exc:
            ads_payload = {"rows": []}
            source_warnings.append(f"advertising evidence error: {exc}")
        ads_by_nm = {int(row["nm_id"]): row for row in ads_payload.get("rows", []) if int(row.get("nm_id") or 0) in set(nm_ids)}
        try:
            placement_index = self.ads_block.build_placement_index()
            ad_options = {nm_id: list(placement_index.get(nm_id, [])) for nm_id in nm_ids}
        except Exception as exc:
            ad_options = {nm_id: [] for nm_id in nm_ids}
            source_warnings.append(f"advertising placement evidence error: {exc}")
        evidence = self._collect_forecast_evidence(
            active=active,
            settings=settings,
        )
        for item_evidence in evidence.values():
            item_evidence["warnings"].extend(source_warnings)
        last_events = self.runtime.latest_sku_action_events_by_nm(nm_ids)
        rows: list[dict[str, Any]] = []
        for sku in active:
            nm_id = int(sku["nm_id"])
            item_evidence = evidence.get(nm_id, {})
            forecast = calculate_depletion_forecast(
                as_of_date=item_evidence.get("as_of_date") or current_business_date_iso(self.now_factory()),
                stock_wb=item_evidence.get("stock_wb"),
                stock_ff=item_evidence.get("stock_ff"),
                daily_demand=item_evidence.get("daily_demand"),
                settings=settings,
                real_inbounds=item_evidence.get("real_inbounds", []),
                districts=item_evidence.get("districts", {}),
                evidence_warnings=item_evidence.get("warnings", []),
            )
            price = price_by_nm.get(nm_id, {})
            ads = ads_by_nm.get(nm_id, {})
            options = ad_options.get(nm_id, [])
            bid_values = [
                value
                for item in options
                if (value := _optional_float(item.get("current_bid_rub"))) is not None
            ]
            current_bid = bid_values[0] if len(options) == 1 and bid_values else None
            latest = last_events.get(nm_id, {})
            metrics = commercial.get(nm_id, {})
            snapshots = snapshot_metrics.get(nm_id, {})
            latest_price_readback = (latest.get(PRICE_PARAMETER) or {}).get("readback") or {}
            event_buyer = (
                latest_price_readback.get("buyer_price") or {}
                if isinstance(latest_price_readback, Mapping)
                else {}
            )
            buyer = _select_observed_buyer_price(
                event_buyer=event_buyer,
                metrics=snapshots,
                current_buyer=current_buyer_prices.get(nm_id),
            )
            promo_count = snapshots.get("promo_count_by_price")
            price_updated_at = str(
                price.get("updated_at")
                or price.get("fetched_at")
                or prices_payload.get("generated_at")
                or ""
            )
            ads_updated_at = str(
                ads.get("last_refreshed_at")
                or ads_payload.get("last_refreshed_at")
                or ""
            )
            bid_updated_at = max(
                (
                    str(item.get("campaign_fetched_at") or ads_updated_at)
                    for item in options
                ),
                default=ads_updated_at,
            )
            rows.append(
                {
                    **sku,
                    **forecast,
                    "nearest_supplier_inbound": select_nearest_supplier_inbound(
                        item_evidence.get("supplier_inbounds", []),
                        as_of_date=str(item_evidence.get("as_of_date") or ""),
                    ),
                    "seller_price": price.get("discountedPrice"),
                    "seller_price_updated_at": price_updated_at,
                    "initial_price": price.get("price"),
                    "seller_discount": price.get("discount"),
                    "buyer_price": buyer["value"],
                    "buyer_price_source": buyer["source"],
                    "buyer_price_freshness": buyer["freshness"],
                    "buyer_price_quality": buyer["quality"],
                    "buyer_price_updated_at": str(
                        buyer.get("observed_at") or buyer.get("freshness") or ""
                    ),
                    "spp_proxy": snapshots.get("spp_proxy"),
                    "spp_proxy_updated_at": str(
                        snapshots.get("spp_proxy__observed_at")
                        or snapshots.get("spp_proxy__date")
                        or price_updated_at
                    ),
                    "promo_label": price.get("promoLabel") or "н/д",
                    "promo_count": promo_count,
                    "promo_participation": snapshots.get("promo_participation"),
                    "promo_freshness": snapshots.get("promo_participation__observed_at")
                    or snapshots.get("promo_count_by_price__observed_at")
                    or snapshots.get("promo_participation__date")
                    or snapshots.get("promo_count_by_price__date")
                    or "",
                    "campaign_count": ads.get("campaign_count", 0),
                    "placement_count": ads.get("placement_count", 0),
                    "campaigns_updated_at": ads_updated_at,
                    "ad_options": options,
                    "current_bid": current_bid,
                    "current_bid_updated_at": bid_updated_at,
                    "bid_sort_value": min(bid_values) if bid_values else None,
                    "ads_drr": metrics.get("ads_drr"),
                    "ads_drr_attributed": metrics.get("ads_drr_attributed"),
                    "ads_spend_rub": metrics.get("ads_sum"),
                    "funnel": {key: metrics.get(key) for key in ("view_count", "openCount", "cartCount", "addToCartConversion", "cartToOrderConversion")},
                    "orders": metrics.get("orderCount"),
                    "sales_rub": metrics.get("orderSum"),
                    "profit_rub": metrics.get("proxy_profit_3_rub", metrics.get("proxy_profit_rub")),
                    "margin_pct": metrics.get("proxy_margin_3_pct", metrics.get("proxy_margin_pct")),
                    "last_price_change_at": str((latest.get(PRICE_PARAMETER) or {}).get("confirmed_at") or ""),
                    "last_bid_change_at": str((latest.get(BID_PARAMETER) or {}).get("confirmed_at") or ""),
                }
            )
        rows.sort(key=lambda item: (-int(item["risk_rank"]) if item.get("risk_rank") is not None else 1, str(item.get("deficit_date") or "9999-12-31"), int(item["nm_id"])))
        return {
            "contract_name": "sheet_vitrina_v1_sku_management_table",
            "generated_at": self.timestamp_factory(),
            "settings": settings_payload,
            "rows": rows,
            "meta": {
                "active_sku_count": len(active),
                "sku_source": "registry_upload_config_v2",
                "forecast_is_calculation_only": True,
                "writes_enabled": True,
                "write_gates": ["section_authorization", "preview", "explicit_confirmation", "validation", "audit", "readback"],
                "metric_policy": {
                    "business_timezone": "Asia/Yekaterinburg",
                    "business_date": current_business_date_iso(self.now_factory()),
                    "cumulative_exact_date": cumulative_date,
                    "cumulative_no_fallback": True,
                    "cumulative_fields": list(SKU_CUMULATIVE_METRIC_FIELDS),
                    "snapshot_latest_successful_fields": list(SKU_SNAPSHOT_METRIC_FIELDS),
                },
                "warehouse_exclusion": {
                    **warehouse_settings,
                    "names": self._warehouse_exclusion_names(
                        evidence,
                        excluded_warehouse_ids=excluded_warehouse_ids,
                    ),
                },
            },
        }

    def build_sku_detail(self, nm_id: int, *, user_key: str) -> dict[str, Any]:
        """Build the quick mutation read model without the forecast/table fanout."""

        if isinstance(nm_id, bool) or int(nm_id) <= 0:
            raise SkuManagementError("nm_id must be positive", http_status=422)
        del user_key  # quick read uses section auth, not per-user forecast settings
        normalized_nm_id = int(nm_id)
        started = self.monotonic_factory()
        phases_ms: dict[str, float] = {}
        remote_call_counts = {
            "wb_prices_exact_goods": 0,
            "wb_ads_campaign_count": 0,
            "wb_ads_adverts_batch": 0,
            "wb_ads_fullstats": 0,
            "wb_ads_min_bid": 0,
            "wb_ads_recommendations": 0,
            "wb_stocks": 0,
            "forecast_or_supply": 0,
        }
        source_warnings: list[str] = []

        active = self._measure_phase(
            phases_ms,
            "identity",
            self._active_skus,
        )
        sku = next(
            (
                dict(item)
                for item in active
                if int(item.get("nm_id") or 0) == normalized_nm_id
            ),
            None,
        )
        if sku is None:
            raise SkuManagementError(
                "SKU is not active in canonical config_v2",
                http_status=404,
                payload={"missing_nm_ids": [normalized_nm_id]},
            )

        price_payload: Mapping[str, Any] = {}
        try:
            remote_call_counts["wb_prices_exact_goods"] += 1
            price_payload = self._measure_phase(
                phases_ms,
                "price_exact_read",
                lambda: self.prices_block.source.fetch_goods_by_nm_ids(
                    [normalized_nm_id]
                ),
            )
            price_table = self._measure_phase(
                phases_ms,
                "price_local_enrichment",
                lambda: self.prices_block.build_goods_table(
                    {
                        "filterNmID": normalized_nm_id,
                        "_current_goods_payload": price_payload,
                    }
                ),
            )
        except Exception as exc:
            price_table = {"rows": [], "generated_at": ""}
            source_warnings.append(
                f"price evidence unavailable ({type(exc).__name__})"
            )
        price = next(
            (
                dict(item)
                for item in price_table.get("rows", [])
                if int(item.get("nmID") or 0) == normalized_nm_id
            ),
            {},
        )

        snapshots = self._measure_phase(
            phases_ms,
            "current_snapshot_projection",
            lambda: self._snapshot_projection([normalized_nm_id]).get(
                normalized_nm_id,
                {},
            ),
        )
        buyer = {
            "value": snapshots.get("public_buyer_price"),
            "source": "spp_proxy_temporal_snapshot",
            "freshness": snapshots.get("public_buyer_price__date", ""),
            "observed_at": snapshots.get(
                "public_buyer_price__observed_at",
                "",
            ),
            "quality": (
                "observed"
                if snapshots.get("public_buyer_price") is not None
                else "missing"
            ),
        }
        try:
            placement_read = self._measure_phase(
                phases_ms,
                "ads_exact_placement_index",
                self.ads_block.build_placement_index_read,
            )
            placement_index = placement_read["index"]
            placement_call_counts = (
                placement_read.get("diagnostics", {}).get(
                    "remote_call_counts",
                    {},
                )
            )
            remote_call_counts["wb_ads_campaign_count"] += int(
                placement_call_counts.get("campaign_count") or 0
            )
            remote_call_counts["wb_ads_adverts_batch"] += int(
                placement_call_counts.get("adverts_batch") or 0
            )
            ad_options = [
                dict(item)
                for item in placement_index.get(normalized_nm_id, [])
            ]
        except Exception as exc:
            ad_options = []
            source_warnings.append(
                "advertising placement evidence unavailable "
                f"({type(exc).__name__})"
            )
        history = self._measure_phase(
            phases_ms,
            "history",
            lambda: self.history(
                {"nm_id": normalized_nm_id, "limit": 50, "offset": 0}
            ),
        )
        latest = self.runtime.latest_sku_action_events_by_nm(
            [normalized_nm_id]
        ).get(normalized_nm_id, {})
        bid_values = [
            value
            for item in ad_options
            if (value := _optional_float(item.get("current_bid_rub"))) is not None
        ]
        price_updated_at = str(
            price.get("updated_at")
            or price.get("fetched_at")
            or price_table.get("generated_at")
            or ""
        )
        bid_updated_at = max(
            (
                str(item.get("campaign_fetched_at") or "")
                for item in ad_options
            ),
            default="",
        )
        row = {
            **sku,
            "seller_price": price.get("discountedPrice"),
            "seller_price_updated_at": price_updated_at,
            "initial_price": price.get("price"),
            "seller_discount": price.get("discount"),
            "buyer_price": buyer.get("value"),
            "buyer_price_source": buyer.get("source", ""),
            "buyer_price_freshness": buyer.get("freshness", ""),
            "buyer_price_quality": buyer.get("quality", "missing"),
            "buyer_price_updated_at": str(
                buyer.get("observed_at") or buyer.get("freshness") or ""
            ),
            "spp_proxy": snapshots.get("spp_proxy"),
            "spp_proxy_updated_at": str(
                snapshots.get("spp_proxy__observed_at")
                or snapshots.get("spp_proxy__date")
                or ""
            ),
            "promo_label": price.get("promoLabel") or "н/д",
            "promo_count": snapshots.get("promo_count_by_price"),
            "promo_participation": snapshots.get("promo_participation"),
            "promo_freshness": str(
                snapshots.get("promo_participation__observed_at")
                or snapshots.get("promo_count_by_price__observed_at")
                or snapshots.get("promo_participation__date")
                or snapshots.get("promo_count_by_price__date")
                or ""
            ),
            "campaign_count": len(
                {int(item.get("advert_id") or 0) for item in ad_options}
            ),
            "placement_count": len(ad_options),
            "campaigns_updated_at": bid_updated_at,
            "ad_options": ad_options,
            "current_bid": (
                bid_values[0]
                if len(ad_options) == 1 and bid_values
                else None
            ),
            "current_bid_updated_at": bid_updated_at,
            "bid_sort_value": min(bid_values) if bid_values else None,
            "last_price_change_at": str(
                (latest.get(PRICE_PARAMETER) or {}).get("confirmed_at") or ""
            ),
            "last_bid_change_at": str(
                (latest.get(BID_PARAMETER) or {}).get("confirmed_at") or ""
            ),
            "source_warnings": source_warnings,
        }
        diagnostics = self._diagnostics(
            started=started,
            phases_ms=phases_ms,
            remote_call_counts=remote_call_counts,
            skipped_paths=(
                "sku_management.build_table",
                "stocks",
                "forecast",
                "supplier_shipments",
                "wb_supplies",
                "ads_fullstats",
                "ads_min_bids",
                "ads_recommendations",
                "all_sku_prices",
            ),
        )
        return {
            "contract_name": "sheet_vitrina_v1_sku_management_detail",
            "generated_at": self.timestamp_factory(),
            "row": row,
            "meta": {
                "read_scope": "quick_exact_sku_mutation",
                "writes_enabled": True,
                "diagnostics": diagnostics,
            },
            "history": history,
        }

    def preview_price(self, payload: Mapping[str, Any], *, actor: str) -> dict[str, Any]:
        started = self.monotonic_factory()
        phases_ms: dict[str, float] = {}
        remote_call_counts = {
            "wb_prices_exact_goods": 0,
            "wb_prices_preview_duplicate_goods": 0,
            "wb_quarantine_scan": 0,
            "public_buyer_price_network": 0,
        }
        nm_id = _positive_int(payload.get("nm_id"), "nm_id")
        target = _money_decimal(payload.get("target_seller_price"), "target_seller_price")
        identity = self._measure_phase(
            phases_ms,
            "identity",
            lambda: self._product_identity(nm_id),
        )
        remote_call_counts["wb_prices_exact_goods"] += 1
        current_payload = self._measure_phase(
            phases_ms,
            "current_price",
            lambda: self.prices_block.source.fetch_goods_by_nm_ids([nm_id]),
        )
        goods = normalize_goods_payload(current_payload)
        if not goods:
            raise SkuManagementError("current WB price was not found", http_status=404)
        good = goods[0]
        combination = choose_target_price_configuration(
            target_seller_price=target,
            current_price=good.price,
            current_discount=good.discount,
        )
        delegated = self._measure_phase(
            phases_ms,
            "guarded_price_preview",
            lambda: self.prices_block.preview_changes(
                {
                    "changes": [
                        {
                            "nmID": nm_id,
                            "price": combination["price"],
                            "discount": combination["discount"],
                        }
                    ]
                },
                current_payload=current_payload,
            ),
        )
        row = delegated["preview"]["rows"][0]
        if not row.get("valid"):
            raise SkuManagementError("price preview is blocked", http_status=422, payload={"preview": delegated})
        warnings = list(row.get("warnings") or [])
        current_view = {}
        try:
            current_view = self._measure_phase(
                phases_ms,
                "price_local_enrichment",
                lambda: next(
                    (
                        item
                        for item in self.prices_block.build_goods_table(
                            {
                                "filterNmID": nm_id,
                                "_current_goods_payload": current_payload,
                            }
                        ).get("rows", [])
                        if int(item.get("nmID") or 0) == nm_id
                    ),
                    {},
                ),
            )
        except Exception:
            current_view = {}
        quarantine_rows: list[Mapping[str, Any]] = []
        try:
            remote_call_counts["wb_quarantine_scan"] += 1
            quarantine_rows = self._measure_phase(
                phases_ms,
                "quarantine",
                lambda: self._load_price_quarantine_rows(nm_id),
            )
        except SkuManagementError:
            raise
        except Exception as exc:
            raise SkuManagementError(
                "quarantine evidence is unavailable; price preview is blocked",
                http_status=503,
                payload={"safety_status": "quarantine_evidence_unavailable"},
            ) from exc
        if quarantine_rows:
            raise SkuManagementError(
                "current WB quarantine blocks the price change",
                http_status=409,
                payload={"safety_status": "current_quarantine", "quarantine": quarantine_rows},
            )
        promo_snapshot = self._price_promo_snapshot(nm_id=nm_id, current_view=current_view)
        override_required_warnings = list(warnings)
        if promo_snapshot["quality"] != "observed":
            warnings.append("promo_evidence_unavailable_or_stale")
            override_required_warnings.append("promo_evidence_unavailable_or_stale")
        elif (_optional_float(promo_snapshot.get("participation")) or 0.0) > 0:
            warnings.append("active_promo_participation")
            override_required_warnings.append("active_promo_participation")
        stabilization = self._stabilization_warnings(nm_id=nm_id, parameter=PRICE_PARAMETER, actor=actor)
        stabilization_codes = [item["code"] for item in stabilization]
        warnings.extend(stabilization_codes)
        override_required_warnings.extend(stabilization_codes)
        buyer = self._measure_phase(
            phases_ms,
            "buyer_price_cached_projection",
            lambda: self._current_buyer_price_projection([nm_id]).get(
                nm_id,
                {
                    "value": None,
                    "source": "spp_proxy_temporal_snapshot",
                    "freshness": "",
                    "observed_at": "",
                    "quality": "missing",
                },
            ),
        )
        if buyer.get("quality") != "observed":
            warnings.append("public_buyer_price_missing_or_stale")
        diagnostics = self._diagnostics(
            started=started,
            phases_ms=phases_ms,
            remote_call_counts=remote_call_counts,
            skipped_paths=(
                "duplicate_current_price_reads",
                "public_buyer_price_network",
            ),
        )
        preview = {
            "preview_id": uuid4().hex,
            "operation_id": str(delegated["preview"].get("operation_id") or uuid4().hex),
            "parameter": PRICE_PARAMETER,
            "nm_id": nm_id,
            "product_name": identity["product_name"],
            "sku": identity["sku"],
            "actor": actor,
            "created_at": self.timestamp_factory(),
            "expires_at_epoch": int(delegated["preview"].get("expires_at_epoch") or 0),
            "delegated_confirmation": delegated["confirmation_payload"],
            "current": row["current"],
            "new": row["new"],
            "target_seller_price": float(target),
            "current_buyer_price": buyer.get("value"),
            "estimated_buyer_price": None,
            "buyer_price_evidence": buyer,
            "promo": promo_snapshot,
            "quarantine": quarantine_rows,
            "warnings": _dedupe_strings(warnings),
            "override_required_warnings": _dedupe_strings(override_required_warnings),
            "stabilization_warnings": stabilization,
            "diagnostics": diagnostics,
        }
        self._save_preview(preview)
        response_preview = dict(preview)
        response_preview.pop("delegated_confirmation", None)
        return {"status": "preview_ready", "preview": response_preview, "writes_enabled": True}

    def commit_price(self, payload: Mapping[str, Any], *, actor: str) -> dict[str, Any]:
        started = self.monotonic_factory()
        phases_ms: dict[str, float] = {}
        remote_call_counts = {
            "wb_quarantine_scan": 0,
            "wb_prices_current_tuple": 0,
            "wb_prices_upload": 0,
            "wb_prices_upload_status": 0,
            "wb_prices_tuple_readback": 0,
            "public_buyer_price_network": 0,
        }
        preview = self._load_preview(payload.get("preview_id"), expected_parameter=PRICE_PARAMETER)
        self._validate_confirmation(preview, payload=payload, actor=actor)
        stabilization_override = bool(preview.get("stabilization_warnings")) and (
            _boolean(payload.get("override_stabilization", False))
            or _boolean(payload.get("override_warnings", False))
        )
        warning_override = bool(preview.get("override_required_warnings")) and (
            _boolean(payload.get("override_warnings", False)) or stabilization_override
        )
        self._claim_preview(preview)
        try:
            remote_call_counts["wb_quarantine_scan"] += 1
            self._measure_phase(
                phases_ms,
                "quarantine_recheck",
                lambda: self._assert_price_not_quarantined(
                    int(preview["nm_id"])
                ),
            )
            remote_call_counts["wb_prices_current_tuple"] += 1
            current_goods_payload = self._measure_phase(
                phases_ms,
                "current_tuple_recheck",
                lambda: self.prices_block.source.fetch_goods_by_nm_ids(
                    [int(preview["nm_id"])]
                ),
            )
            current_goods = normalize_goods_payload(current_goods_payload)
            if not current_goods:
                raise SkuManagementError("current WB price disappeared after preview", http_status=409)
            current_good = current_goods[0]
            self._measure_phase(
                phases_ms,
                "promo_recheck",
                lambda: self._assert_price_promo_fresh(
                    preview,
                    current_goods_payload=current_goods_payload,
                ),
            )
            expected_current = preview["current"]
            if (
                int(current_good.price) != int(expected_current["price"])
                or int(current_good.discount) != int(expected_current["discount"])
                or abs(float(current_good.discounted_price) - float(expected_current["discountedPrice"])) >= 0.011
            ):
                raise SkuManagementError(
                    "current WB price differs from preview; create a new preview",
                    http_status=409,
                    payload={
                        "readback": {
                            "price": float(current_good.price),
                            "discount": int(current_good.discount),
                            "discountedPrice": float(current_good.discounted_price),
                        }
                    },
                )
            remote_call_counts["wb_prices_upload"] += 1
            upload = self._measure_phase(
                phases_ms,
                "upload",
                lambda: self.prices_block.upload_task(
                    preview["delegated_confirmation"],
                    actor=actor,
                ),
            )
            upload_id = _positive_int(upload.get("uploadID"), "uploadID")
            confirmed, observed, status_payload, polling = self._measure_phase(
                phases_ms,
                "upload_status_and_tuple_readback",
                lambda: self._poll_price_upload_and_tuple(
                    upload_id=upload_id,
                    nm_id=int(preview["nm_id"]),
                    target=float(preview["target_seller_price"]),
                    expected_price=int(preview["new"]["price"]),
                    expected_discount=int(preview["new"]["discount"]),
                ),
            )
            remote_call_counts["wb_prices_upload_status"] += int(
                polling.get("status_reads") or 0
            )
            remote_call_counts["wb_prices_tuple_readback"] += int(
                polling.get("tuple_reads") or 0
            )
            if status_payload.get("status") != "success":
                raise SkuManagementError(
                    "WB price upload did not finish with success",
                    http_status=502,
                    payload={
                        "readback_status": "error",
                        "readback": dict(status_payload),
                        "polling": polling,
                    },
                )
            if confirmed is None:
                raise SkuManagementError(
                    "WB price readback does not match the requested price/discount/seller-price tuple",
                    http_status=409,
                    payload={
                        "readback_status": "mismatch",
                        "readback": observed,
                        "target": preview["new"],
                        "polling": polling,
                    },
                )
            confirmed_at = self.timestamp_factory()
            buyer = {
                **dict(preview.get("buyer_price_evidence") or {}),
                "quality": "deferred",
                "value": None,
                "post_commit_refresh": "not_awaited",
                "reason": (
                    "optional public buyer-price enrichment does not block "
                    "confirmed seller-price success"
                ),
            }
            diagnostics = self._diagnostics(
                started=started,
                phases_ms=phases_ms,
                remote_call_counts=remote_call_counts,
                skipped_paths=(
                    "second_current_tuple_read",
                    "sequential_status_then_tuple_polling",
                    "blocking_public_buyer_price_readback",
                ),
            )
            event = self._persist_event(
                preview=preview,
                actor=actor,
                requested_value=float(preview["target_seller_price"]),
                confirmed_value=confirmed,
                old_value=float(preview["current"]["discountedPrice"]),
                status="confirmed",
                stabilization_override=stabilization_override,
                warning_override=warning_override,
                readback_status="matching",
                readback={
                    "upload": dict(status_payload),
                    "price": observed,
                    "buyer_price": buyer,
                    "polling": polling,
                    "diagnostics": diagnostics,
                },
                confirmed_at=confirmed_at,
            )
            return {
                "status": "success",
                "nm_id": int(preview["nm_id"]),
                "product_name": str(preview.get("product_name") or "SKU"),
                "confirmed_value": confirmed,
                "old_value": float(preview["current"]["discountedPrice"]),
                "requested_value": float(preview["target_seller_price"]),
                "confirmed_price": observed.get("price"),
                "confirmed_discount": observed.get("discount"),
                "confirmed_seller_price": observed.get("seller_price"),
                "readback_status": "matching",
                "buyer_price": buyer,
                "diagnostics": diagnostics,
                "event": event,
            }
        except Exception as exc:
            diagnostics = self._diagnostics(
                started=started,
                phases_ms=phases_ms,
                remote_call_counts=remote_call_counts,
                skipped_paths=("blocking_public_buyer_price_readback",),
            )
            if isinstance(exc, SkuManagementError):
                exc.payload.setdefault("diagnostics", diagnostics)
            self._persist_failure(
                preview=preview,
                actor=actor,
                exc=exc,
                stabilization_override=stabilization_override,
                warning_override=warning_override,
            )
            raise

    def preview_bid(self, payload: Mapping[str, Any], *, actor: str) -> dict[str, Any]:
        started = self.monotonic_factory()
        phases_ms: dict[str, float] = {}
        remote_call_counts = {
            "wb_ads_exact_advert": 1,
            "wb_ads_min_bid": 1,
            "wb_ads_fullstats": 0,
            "wb_ads_recommendations": 0,
        }
        delegated = self._measure_phase(
            phases_ms,
            "exact_bid_preview",
            lambda: self.ads_block.preview_bid_change(payload),
        )
        facts = dict(delegated["preview"])
        if facts.get("min_bid_rub") is None:
            raise SkuManagementError(
                "minimum bid evidence is unavailable; bid preview is blocked",
                http_status=503,
                payload={"safety_status": "min_bid_unavailable"},
            )
        nm_id = int(facts["nm_id"])
        identity = self._measure_phase(
            phases_ms,
            "identity",
            lambda: self._product_identity(nm_id),
        )
        stabilization = self._stabilization_warnings(nm_id=nm_id, parameter=BID_PARAMETER, actor=actor)
        warnings = list(facts.get("warnings") or []) + [item["code"] for item in stabilization]
        diagnostics = self._diagnostics(
            started=started,
            phases_ms=phases_ms,
            remote_call_counts=remote_call_counts,
            skipped_paths=("ads_fullstats", "ads_recommendations"),
        )
        preview = {
            "preview_id": uuid4().hex,
            "operation_id": str(facts.get("operation_id") or uuid4().hex),
            "parameter": BID_PARAMETER,
            "nm_id": nm_id,
            "product_name": identity["product_name"],
            "sku": identity["sku"],
            "actor": actor,
            "created_at": self.timestamp_factory(),
            "expires_at_epoch": int(facts.get("expires_at_epoch") or 0),
            "delegated_preview_id": facts["preview_id"],
            "advert_id": int(facts["advert_id"]),
            "campaign_name": str(facts.get("campaign_name") or ""),
            "placement": str(facts["placement"]),
            "old_value": facts["old_bid_rub"],
            "requested_value": facts["new_bid_rub"],
            "min_bid_rub": facts.get("min_bid_rub"),
            "warnings": warnings,
            "override_required_warnings": [item["code"] for item in stabilization],
            "stabilization_warnings": stabilization,
            "current_bid_freshness": facts.get("created_at"),
            "diagnostics": diagnostics,
        }
        self._save_preview(preview)
        response_preview = dict(preview)
        response_preview.pop("delegated_preview_id", None)
        return {"status": "preview_ready", "preview": response_preview, "writes_enabled": True}

    def commit_bid(self, payload: Mapping[str, Any], *, actor: str) -> dict[str, Any]:
        started = self.monotonic_factory()
        phases_ms: dict[str, float] = {}
        remote_call_counts = {
            "wb_ads_commit_exact_advert": 0,
            "wb_ads_commit_min_bid": 0,
            "wb_ads_patch": 0,
            "wb_ads_exact_bid_readback": 0,
            "wb_ads_fullstats": 0,
            "wb_ads_min_bid_readback": 0,
            "wb_ads_recommendations": 0,
        }
        preview = self._load_preview(payload.get("preview_id"), expected_parameter=BID_PARAMETER)
        self._validate_confirmation(preview, payload=payload, actor=actor)
        stabilization_override = bool(preview.get("stabilization_warnings")) and (
            _boolean(payload.get("override_stabilization", False))
            or _boolean(payload.get("override_warnings", False))
        )
        warning_override = bool(preview.get("override_required_warnings")) and (
            _boolean(payload.get("override_warnings", False)) or stabilization_override
        )
        self._claim_preview(preview)
        try:
            delegated = self._measure_phase(
                phases_ms,
                "guarded_bid_commit",
                lambda: self.ads_block.commit_bid_change(
                    {"preview_id": preview["delegated_preview_id"]},
                    actor=actor,
                ),
            )
            remote_call_counts["wb_ads_commit_exact_advert"] = 1
            remote_call_counts["wb_ads_commit_min_bid"] = 1
            remote_call_counts["wb_ads_patch"] = 1
            confirmed, readback, polling = self._measure_phase(
                phases_ms,
                "exact_bid_readback",
                lambda: self._poll_exact_bid_readback(preview),
            )
            remote_call_counts["wb_ads_exact_bid_readback"] += int(
                polling.get("exact_reads") or 0
            )
            if confirmed is None:
                raise SkuManagementError(
                    "WB bid readback does not match requested bid",
                    http_status=409,
                    payload={
                        "readback_status": "mismatch",
                        "readback": dict(readback),
                        "target": preview["requested_value"],
                        "polling": polling,
                    },
                )
            confirmed_at = self.timestamp_factory()
            diagnostics = self._diagnostics(
                started=started,
                phases_ms=phases_ms,
                remote_call_counts=remote_call_counts,
                skipped_paths=(
                    "ads_sku_detail",
                    "ads_fullstats",
                    "ads_min_bid_readback",
                    "ads_recommendations",
                ),
            )
            event = self._persist_event(
                preview=preview,
                actor=actor,
                requested_value=float(preview["requested_value"]),
                confirmed_value=confirmed,
                old_value=float(preview["old_value"]),
                status="confirmed",
                stabilization_override=stabilization_override,
                warning_override=warning_override,
                readback_status="matching",
                readback={
                    "commit": delegated,
                    "exact_bid": dict(readback),
                    "polling": polling,
                    "diagnostics": diagnostics,
                },
                confirmed_at=confirmed_at,
            )
            return {
                "status": "success",
                "nm_id": int(preview["nm_id"]),
                "product_name": str(preview.get("product_name") or "SKU"),
                "advert_id": int(preview["advert_id"]),
                "campaign_name": str(preview.get("campaign_name") or ""),
                "placement": str(preview["placement"]),
                "old_value": float(preview["old_value"]),
                "requested_value": float(preview["requested_value"]),
                "confirmed_value": confirmed,
                "readback_status": "matching",
                "diagnostics": diagnostics,
                "event": event,
            }
        except Exception as exc:
            diagnostics = self._diagnostics(
                started=started,
                phases_ms=phases_ms,
                remote_call_counts=remote_call_counts,
                skipped_paths=(
                    "ads_sku_detail",
                    "ads_fullstats",
                    "ads_min_bid_readback",
                    "ads_recommendations",
                ),
            )
            if isinstance(exc, SkuManagementError):
                exc.payload.setdefault("diagnostics", diagnostics)
            self._persist_failure(
                preview=preview,
                actor=actor,
                exc=exc,
                stabilization_override=stabilization_override,
                warning_override=warning_override,
            )
            raise

    def _poll_exact_bid_readback(
        self,
        preview: Mapping[str, Any],
    ) -> tuple[float | None, Mapping[str, Any], dict[str, Any]]:
        started = self.monotonic_factory()
        deadline = (
            started + self.readback_deadline_seconds
            if self.readback_deadline_seconds > 0
            else None
        )
        readback: Mapping[str, Any] = {}
        cadence: list[float] = []
        exact_reads = 0
        attempts = 0
        target = float(preview["requested_value"])
        for attempt in range(self.readback_attempts):
            attempts = attempt + 1
            exact_reads += 1
            readback = self.ads_block.read_exact_bid(
                nm_id=int(preview["nm_id"]),
                advert_id=int(preview["advert_id"]),
                placement=str(preview["placement"]),
            )
            value = _optional_float(readback.get("current_bid_rub"))
            if value is not None and abs(value - target) < 0.001:
                return (
                    value,
                    readback,
                    {
                        "attempts": attempts,
                        "exact_reads": exact_reads,
                        "deadline_seconds": self.readback_deadline_seconds,
                        "cadence_seconds": cadence,
                        "elapsed_ms": round(
                            max(self.monotonic_factory() - started, 0.0)
                            * 1000,
                            2,
                        ),
                        "outcome": "matching",
                    },
                )
            if attempt + 1 >= self.readback_attempts:
                break
            now = self.monotonic_factory()
            if deadline is not None and now >= deadline:
                break
            delay = min(
                self.readback_delay_seconds * (1.7**attempt),
                3.0,
            )
            if deadline is not None:
                delay = min(delay, max(deadline - now, 0.0))
            cadence.append(round(delay, 3))
            if delay > 0:
                self.sleep(delay)
        return (
            None,
            readback,
            {
                "attempts": attempts,
                "exact_reads": exact_reads,
                "deadline_seconds": self.readback_deadline_seconds,
                "cadence_seconds": cadence,
                "elapsed_ms": round(
                    max(self.monotonic_factory() - started, 0.0) * 1000,
                    2,
                ),
                "outcome": "deadline_or_attempts_exhausted",
            },
        )

    def history(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        return self.runtime.list_sku_action_events(
            limit=_integer(params.get("limit", 50), "limit", 1, 200),
            offset=_integer(params.get("offset", 0), "offset", 0, 1000000),
            nm_id=_optional_int(params.get("nm_id")),
            parameter=str(params.get("parameter") or ""),
            status=str(params.get("status") or ""),
        )

    def _active_skus(self) -> list[dict[str, Any]]:
        current = self.runtime.load_current_state()
        enrichment = {int(item["nm_id"]): item for item in self.runtime.list_nomenclature_items(active_only=True) if item.get("nm_id")}
        rows = []
        for item in sorted(current.config_v2, key=lambda value: value.display_order):
            if not item.enabled:
                continue
            extra = enrichment.get(int(item.nm_id), {})
            rows.append(
                {
                    "nm_id": int(item.nm_id),
                    "sku": str(extra.get("our_sku") or ""),
                    "name": str(extra.get("nomenclature_name") or item.display_name or ""),
                    "group": str(item.group or ""),
                }
            )
        return rows

    def _product_identity(self, nm_id: int) -> dict[str, Any]:
        identity = next(
            (
                dict(item)
                for item in self._active_skus()
                if int(item.get("nm_id") or 0) == int(nm_id)
            ),
            None,
        )
        if identity is None:
            raise SkuManagementError(
                "SKU is not active in canonical config_v2",
                http_status=404,
                payload={"missing_nm_ids": [int(nm_id)]},
            )
        return {
            "nm_id": int(nm_id),
            "product_name": str(
                identity.get("name") or identity.get("sku") or "SKU"
            ),
            "sku": str(identity.get("sku") or ""),
        }

    def _collect_forecast_evidence(
        self,
        *,
        active: Sequence[Mapping[str, Any]],
        settings: ForecastSettings,
    ) -> dict[int, dict[str, Any]]:
        today = current_business_date_iso(self.now_factory())
        nm_ids = [int(item["nm_id"]) for item in active]
        result = {
            nm_id: {
                "as_of_date": today,
                "stock_wb": None,
                "stock_ff": None,
                "daily_demand": None,
                "real_inbounds": [],
                "supplier_inbounds": [],
                "districts": {},
                "warnings": [],
                "warehouse_exclusion_options": [],
            }
            for nm_id in nm_ids
        }
        if self.stocks_block is None:
            for row in result.values():
                row["warnings"].append("stocks contour is unavailable")
        else:
            try:
                stock_result = self.stocks_block.execute(
                    StocksRequest(
                        snapshot_type="stocks",
                        snapshot_date=today,
                        nm_ids=nm_ids,
                    )
                ).result
                if getattr(stock_result, "kind", "") != "success":
                    raise ValueError("official WB stock snapshot is incomplete")
                exclusion = build_incident_stock_projection(
                    self.runtime,
                    items=list(getattr(stock_result, "items", []) or []),
                    warehouse_rows=list(
                        getattr(stock_result, "warehouse_rows", []) or []
                    ),
                    snapshot_date=str(
                        getattr(stock_result, "snapshot_date", "") or today
                    ),
                    fetched_at=str(getattr(stock_result, "fetched_at", "") or ""),
                    pagination_complete=bool(
                        getattr(stock_result, "pagination_complete", False)
                    ),
                    raw_rows_digest=str(
                        getattr(stock_result, "raw_rows_digest", "") or ""
                    ),
                )
                for item in getattr(stock_result, "items", []):
                    target = result.get(int(item.nm_id))
                    if target is None:
                        continue
                    exclusion_row = exclusion["by_nm_id"].get(str(int(item.nm_id)), {})
                    target["stock_wb"] = _optional_float(
                        exclusion_row.get("effective_stock_total_mp")
                    )
                    target["warehouse_exclusion_options"] = list(
                        exclusion.get("options") or []
                    )
                    if target["stock_wb"] is None:
                        target["warnings"].append("current WB stock evidence is unavailable")
                    for key, attribute in (
                        ("central", "stock_ru_central"), ("northwest", "stock_ru_northwest"),
                        ("volga", "stock_ru_volga"), ("ural", "stock_ru_ural"),
                        ("south_caucasus", "stock_ru_south_caucasus"), ("far_siberia", "stock_ru_far_siberia"),
                    ):
                        value = _optional_float(
                            exclusion_row.get(f"effective_{attribute}")
                        )
                        if value is not None:
                            target["districts"].setdefault(key, {})["stock"] = value
            except Exception as exc:
                for row in result.values():
                    row["warnings"].append(f"stocks evidence error: {exc}")
        try:
            activation = self.runtime.load_ff_stock_activation_operation()
            if not activation:
                for row in result.values():
                    row["warnings"].append("FF stock ledger is not activated")
            else:
                balances = {
                    int(item["nm_id"]): float(item.get("quantity") or item.get("balance") or 0.0)
                    for item in self.runtime.list_ff_stock_balances()
                }
                for nm_id in nm_ids:
                    result[nm_id]["stock_ff"] = balances.get(nm_id, 0.0)
        except Exception as exc:
            for row in result.values():
                row["warnings"].append(f"FF stock evidence error: {exc}")
        if self.sales_history is None:
            for row in result.values():
                row["warnings"].append("sales history contour is unavailable")
        else:
            lookup_days = sales_lookup_days(settings.sales_avg_period_days)
            try:
                samples = self.sales_history.load_order_count_samples_by_date(
                    date_from=(date.fromisoformat(today) - timedelta(days=lookup_days)).isoformat(),
                    date_to=(date.fromisoformat(today) - timedelta(days=1)).isoformat(),
                    nm_ids=nm_ids,
                    clamp_to_coverage=True,
                )
                for nm_id in nm_ids:
                    sku_samples = list(samples.get(nm_id, []))
                    if not sku_samples:
                        result[nm_id]["daily_demand"] = None
                        result[nm_id]["warnings"].append(
                            "authoritative sales history has no dated samples for this SKU"
                        )
                        continue
                    estimate = estimate_availability_adjusted_demand(
                        sku_samples,
                        report_date=date.fromisoformat(today),
                        sales_avg_period_days=settings.sales_avg_period_days,
                        sales_lookup_days=lookup_days,
                    )
                    result[nm_id]["daily_demand"] = estimate.daily_demand_total
                    if estimate.demand_warning:
                        result[nm_id]["warnings"].append(estimate.demand_warning)
            except Exception as exc:
                for row in result.values():
                    row["warnings"].append(f"sales history evidence error: {exc}")
        self._append_supplier_inbounds(result, settings=settings)
        self._append_factory_order_inbounds(result, settings=settings)
        self._append_wb_supply_inbounds(result, settings=settings)
        self._append_regional_demand(result, settings=settings, as_of_date=today)
        return result

    @staticmethod
    def _warehouse_exclusion_names(
        evidence: Mapping[int, Mapping[str, Any]],
        *,
        excluded_warehouse_ids: Sequence[int],
    ) -> list[str]:
        for row in evidence.values():
            selected = [
                str(item.get("warehouse_name") or f"warehouseId {item.get('warehouse_id')}")
                for item in row.get("warehouse_exclusion_options", [])
                if item.get("selected")
            ]
            if selected:
                return selected
        return [f"warehouseId {item}" for item in excluded_warehouse_ids]

    def _append_supplier_inbounds(self, result: dict[int, dict[str, Any]], *, settings: ForecastSettings) -> None:
        try:
            shipments = self.runtime.list_supplier_shipments()
        except Exception as exc:
            for row in result.values(): row["warnings"].append(f"supplier shipment evidence error: {exc}")
            return
        for shipment in shipments:
            shipment_id = str(shipment.get("shipment_id") or "")
            try:
                detail = self.runtime.load_supplier_shipment(shipment_id)
            except Exception as exc:
                for row in result.values():
                    row["warnings"].append(f"supplier shipment {shipment_id} evidence error: {exc}")
                continue
            if not detail:
                continue
            header = detail.get("header") or {}
            if str(header.get("actual_ff_acceptance_date") or shipment.get("actual_ff_acceptance_date") or ""):
                continue
            order_status = str(header.get("order_status") or shipment.get("order_status") or "production")
            if order_status not in {"production", "in_transit"}:
                for line in detail.get("lines", []):
                    nm_id = _optional_int(line.get("internal_nm_id"))
                    if nm_id in result:
                        result[nm_id]["warnings"].append(
                            f"supplier shipment {shipment_id} status {order_status or 'unknown'} is not usable forecast evidence"
                        )
                continue
            actual_shipment_date = str(
                header.get("actual_shipment_date")
                or shipment.get("actual_shipment_date")
                or ""
            )
            planned_shipment_date = str(
                header.get("planned_shipment_date")
                or header.get("shipment_date")
                or shipment.get("planned_shipment_date")
                or shipment.get("shipment_date")
                or ""
            )
            uses_actual_shipment_date = _is_iso_date(actual_shipment_date)
            base_date = actual_shipment_date if uses_actual_shipment_date else planned_shipment_date
            if not _is_iso_date(base_date):
                for line in detail.get("lines", []):
                    nm_id = _optional_int(line.get("internal_nm_id"))
                    if nm_id in result:
                        result[nm_id]["warnings"].append(
                            f"supplier shipment {shipment_id} has no usable shipment date"
                        )
                continue
            arrival = date.fromisoformat(base_date) + timedelta(
                days=settings.factory_to_ff_lead_days + settings.ff_to_wb_lead_days
            )
            invoice_no = str(header.get("invoice_no") or shipment.get("invoice_no") or "").strip()
            quantity_by_nm: dict[int, float] = {}
            for line in detail.get("lines", []):
                if str(line.get("line_type") or "product") != "product":
                    continue
                if bool(line.get("manual_override")) or str(line.get("match_status") or "") not in {
                    MATCH_STATUS_MATCHED,
                    MATCH_STATUS_MATCHED_BY_BARCODE,
                    MATCH_STATUS_MATCHED_BY_COMPATIBILITY,
                }:
                    continue
                nm_id = _optional_int(line.get("internal_nm_id"))
                qty = _optional_float(line.get("qty"))
                if nm_id in result and qty and qty > 0:
                    quantity_by_nm[nm_id] = quantity_by_nm.get(nm_id, 0.0) + qty
            for nm_id, quantity in quantity_by_nm.items():
                result[nm_id]["real_inbounds"].append(
                    ForecastInbound(
                        arrival.isoformat(),
                        quantity,
                        "supplier_shipment",
                        f"{shipment_id}:{nm_id}",
                    )
                )
                result[nm_id].setdefault("supplier_inbounds", []).append(
                    {
                        "shipment_id": shipment_id,
                        "invoice_no": invoice_no,
                        "arrival_date": arrival.isoformat(),
                        "quantity": quantity,
                        "date_source": (
                            "actual_shipment_date"
                            if uses_actual_shipment_date
                            else "planned_shipment_date"
                        ),
                    }
                )

    def _append_factory_order_inbounds(self, result: dict[int, dict[str, Any]], *, settings: ForecastSettings) -> None:
        """Reuse only manual factory-order rows; supplier-registry rows are already consumed above."""

        try:
            payload = self.runtime.load_factory_order_result_state() or {}
        except Exception as exc:
            for row in result.values():
                row["warnings"].append(f"factory-order evidence error: {exc}")
            return
        if not isinstance(payload, Mapping) or not payload:
            for row in result.values():
                row["warnings"].append("factory-order calculation evidence is unavailable")
            return
        raw_settings = payload.get("settings") if isinstance(payload.get("settings"), Mapping) else {}
        source = str(payload.get("factory_inbound_source") or raw_settings.get("factory_inbound_source") or "")
        if source != "manual_excel":
            return
        calculation_id = str(payload.get("calculation_id") or "factory-order")
        for index, item in enumerate(payload.get("effective_inbound_factory_to_ff") or []):
            if not isinstance(item, Mapping):
                continue
            nm_id = _optional_int(item.get("nm_id"))
            quantity = _optional_float(item.get("quantity"))
            arrival_raw = str(item.get("effective_arrival_date") or item.get("planned_arrival_date") or "")
            if nm_id not in result or quantity is None or quantity <= 0:
                continue
            if not _is_iso_date(arrival_raw):
                result[nm_id]["warnings"].append(
                    f"factory-order inbound {calculation_id}:{index} has no usable arrival date"
                )
                continue
            arrival = date.fromisoformat(arrival_raw) + timedelta(days=settings.ff_to_wb_lead_days)
            result[nm_id]["real_inbounds"].append(
                ForecastInbound(
                    arrival.isoformat(),
                    quantity,
                    "factory_order_manual",
                    f"{calculation_id}:{index}:{nm_id}",
                )
            )

    def _append_wb_supply_inbounds(self, result: dict[int, dict[str, Any]], *, settings: ForecastSettings) -> None:
        try:
            records = self.runtime.list_wb_supplies_cache_records()
        except Exception as exc:
            for row in result.values(): row["warnings"].append(f"WB supply evidence error: {exc}")
            return
        for record in records:
            normalized = record.get("normalized") or {}
            status = _optional_int(normalized.get("status_id") or normalized.get("statusID") or record.get("status_id"))
            if status not in {3, 4, 6}:
                continue
            cache_key = str(
                normalized.get("cache_key")
                or record.get("cache_key")
                or record.get("supply_id")
                or ""
            ).strip()
            source_key = f"wb_supply_debit:{cache_key}" if cache_key else ""
            writeoff = self.runtime.load_ff_stock_operation_by_source_key(source_key) if source_key else None
            arrival_raw = str(
                normalized.get("fact_date")
                or normalized.get("supply_date")
                or normalized.get("date")
                or ""
            )[:10]
            quantities_by_nm: dict[int, dict[str, Any]] = {}
            for good in record.get("raw_goods") or []:
                nm_id = _optional_int(good.get("nmID") or good.get("nmId") or good.get("nm_id"))
                quantities = _wb_supply_quantities(good)
                if nm_id not in result or not quantities["planned"] or quantities["planned"] <= 0:
                    continue
                bucket = quantities_by_nm.setdefault(
                    nm_id,
                    {"planned": 0.0, "progressed": 0.0, "remaining": 0.0, "progressed_exceeds_planned": False},
                )
                bucket["planned"] += float(quantities["planned"] or 0.0)
                bucket["progressed"] += float(quantities["progressed"] or 0.0)
                bucket["remaining"] += float(quantities["remaining"] or 0.0)
                bucket["progressed_exceeds_planned"] = bool(
                    bucket["progressed_exceeds_planned"] or quantities["progressed_exceeds_planned"]
                )
            for nm_id, quantities in quantities_by_nm.items():
                qty = float(quantities["remaining"])
                planned_qty = float(quantities["planned"])
                consumes_initial_ff = not bool(writeoff)
                if consumes_initial_ff:
                    result[nm_id]["warnings"].append(
                        f"WB supply {record.get('supply_id') or cache_key} will transfer existing FF stock instead of adding inventory twice"
                    )
                if float(quantities["progressed"]) > 0:
                    result[nm_id]["warnings"].append(
                        f"WB supply {record.get('supply_id') or cache_key} remaining quantity excludes factual progressed/accepted units already covered by current WB stock"
                    )
                if quantities["progressed_exceeds_planned"]:
                    result[nm_id]["warnings"].append(
                        f"WB supply {record.get('supply_id') or cache_key} progressed quantity exceeds planned composition; future inbound is capped at zero"
                    )
                if not _is_iso_date(arrival_raw):
                    result[nm_id]["warnings"].append(
                        f"WB supply {record.get('supply_id') or cache_key} has no usable arrival date"
                    )
                district_key = str(
                    normalized.get("district_key")
                    or normalized.get("warehouse_district_key")
                    or ""
                )
                result[nm_id]["real_inbounds"].append(
                    ForecastInbound(
                        arrival_raw,
                        qty,
                        "wb_supply",
                        str(record.get("supply_id") or cache_key),
                        district_key=district_key,
                        consumes_initial_ff=consumes_initial_ff,
                        initial_ff_reservation_qty=planned_qty if consumes_initial_ff else None,
                    )
                )

    def _append_regional_demand(
        self,
        result: dict[int, dict[str, Any]],
        *,
        settings: ForecastSettings,
        as_of_date: str,
    ) -> None:
        try:
            state = self.runtime.load_wb_regional_supply_result_state() or {}
            payload = state.get("payload") if isinstance(state.get("payload"), Mapping) else state
            report_date = str(payload.get("report_date") or "") if isinstance(payload, Mapping) else ""
            if not _is_iso_date(report_date):
                raise ValueError("regional calculation report_date is missing")
            age_days = (date.fromisoformat(as_of_date) - date.fromisoformat(report_date)).days
            if age_days < 0 or age_days > settings.sales_avg_period_days:
                raise ValueError(
                    f"regional calculation is stale for forecast: report_date={report_date}, age_days={age_days}"
                )
            for district in payload.get("districts", []) if isinstance(payload, Mapping) else []:
                key = str(district.get("district_key") or "")
                for item in district.get("rows", []):
                    nm_id = _optional_int(item.get("nm_id"))
                    if nm_id in result:
                        result[nm_id]["districts"].setdefault(key, {})["daily_demand"] = _optional_float(item.get("district_daily_demand"))
        except Exception as exc:
            for row in result.values():
                row["warnings"].append(f"regional demand evidence error: {exc}")
            return
        for row in result.values():
            if not any(_optional_float(item.get("daily_demand")) is not None for item in row["districts"].values()):
                row["warnings"].append("regional demand evidence is unavailable")

    def _commercial_projection(
        self,
        nm_ids: Sequence[int],
        *,
        target_date: str | None = None,
    ) -> dict[int, dict[str, Any]]:
        result = {int(nm_id): {} for nm_id in nm_ids}
        exact_date = target_date or (
            date.fromisoformat(current_business_date_iso(self.now_factory()))
            - timedelta(days=2)
        ).isoformat()
        try:
            snapshot = self.runtime.load_sheet_vitrina_ready_snapshot_covering_date_any_bundle(
                column_date=exact_date
            )
        except Exception:
            snapshot = None
        data = (
            next(
                (
                    sheet
                    for sheet in snapshot.sheets
                    if sheet.sheet_name == "DATA_VITRINA"
                ),
                None,
            )
            if snapshot is not None
            else None
        )
        date_columns = (
            [str(item) for item in getattr(snapshot, "date_columns", [])]
            if snapshot is not None
            else []
        )
        exact_index = (
            date_columns.index(exact_date)
            if exact_date in date_columns
            else None
        )
        if data is not None and exact_index is not None:
            for row in data.rows:
                if not isinstance(row, list) or len(row) < 3:
                    continue
                key = str(row[1])
                if not key.startswith("SKU:") or "|" not in key:
                    continue
                nm_raw, metric = key[4:].split("|", 1)
                nm_id = _optional_int(nm_raw)
                if nm_id not in result or metric not in SKU_CUMULATIVE_METRIC_FIELDS:
                    continue
                values = list(row[2:])
                value = (
                    _optional_float(values[exact_index])
                    if exact_index < len(values)
                    else None
                )
                if value is not None:
                    result[nm_id][metric] = value
                    result[nm_id][f"{metric}__date"] = exact_date
        try:
            ads_payload, _ = self.runtime.load_temporal_source_snapshot(
                source_key="ads_compact",
                snapshot_date=exact_date,
            )
        except Exception:
            ads_payload = None
        for item in _temporal_payload_items(ads_payload):
            nm_id = _optional_int(
                item.get("nm_id")
                if isinstance(item, Mapping)
                else getattr(item, "nm_id", None)
            )
            if nm_id not in result:
                continue
            for metric in (
                "ads_views",
                "ads_clicks",
                "ads_atbs",
                "ads_orders",
                "ads_sum",
                "ads_sum_price",
                "ads_cpc",
                "ads_ctr",
                "ads_cr",
            ):
                raw = (
                    item.get(metric)
                    if isinstance(item, Mapping)
                    else getattr(item, metric, None)
                )
                value = _optional_float(raw)
                if value is not None:
                    result[nm_id][metric] = value
                    result[nm_id][f"{metric}__date"] = exact_date
        return result

    def _current_buyer_price_projection(self, nm_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
        projected = self._latest_temporal_projection(
            source_key="spp_proxy",
            nm_ids=nm_ids,
            fields=("public_buyer_price",),
        )
        return {
            nm_id: {
                "value": row.get("public_buyer_price"),
                "source": "spp_proxy_temporal_snapshot",
                "freshness": row.get("public_buyer_price__date", ""),
                "observed_at": row.get("public_buyer_price__observed_at", ""),
                "quality": "observed",
            }
            for nm_id, row in projected.items()
            if row.get("public_buyer_price") is not None
        }

    def _snapshot_projection(self, nm_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
        result = {int(nm_id): {} for nm_id in nm_ids}
        for source_key, fields in (
            ("spp_proxy", ("spp_proxy", "public_buyer_price")),
            ("promo_by_price", ("promo_participation", "promo_count_by_price")),
        ):
            projection = self._latest_temporal_projection(
                source_key=source_key,
                nm_ids=nm_ids,
                fields=fields,
            )
            for nm_id, row in projection.items():
                result[nm_id].update(row)
        for nm_id, row in result.items():
            if row.get("public_buyer_price") is not None:
                row["buyer_price_rub"] = row["public_buyer_price"]
                row["buyer_price_rub__date"] = row.get(
                    "public_buyer_price__date", ""
                )
                row["buyer_price_rub__observed_at"] = row.get(
                    "public_buyer_price__observed_at", ""
                )
        return result

    def _latest_temporal_projection(
        self,
        *,
        source_key: str,
        nm_ids: Sequence[int],
        fields: Sequence[str],
    ) -> dict[int, dict[str, Any]]:
        requested = {int(item) for item in nm_ids}
        result = {nm_id: {} for nm_id in requested}
        today = current_business_date_iso(self.now_factory())
        snapshots: list[tuple[str, Any, str]] = []
        if hasattr(self.runtime, "list_temporal_source_snapshots"):
            try:
                snapshots = list(
                    self.runtime.list_temporal_source_snapshots(
                        source_key=source_key,
                        date_to=today,
                    )
                )
            except Exception:
                snapshots = []
        else:
            try:
                dates = [
                    item
                    for item in self.runtime.list_temporal_source_snapshot_dates(
                        source_key=source_key
                    )
                    if _is_iso_date(str(item)) and str(item) <= today
                ]
            except Exception:
                return result
            for snapshot_date in sorted(dates, reverse=True):
                try:
                    payload, captured_at = self.runtime.load_temporal_source_snapshot(
                        source_key=source_key,
                        snapshot_date=str(snapshot_date),
                    )
                except Exception:
                    continue
                snapshots.append(
                    (str(snapshot_date), payload, str(captured_at or ""))
                )
        for snapshot_date, payload, captured_at in snapshots:
            if not _is_iso_date(str(snapshot_date)) or str(snapshot_date) > today:
                continue
            for item in _temporal_payload_items(payload):
                nm_id = _optional_int(
                    item.get("nm_id") if isinstance(item, Mapping) else getattr(item, "nm_id", None)
                )
                if nm_id not in requested:
                    continue
                for field in fields:
                    if field in result[nm_id]:
                        continue
                    raw = item.get(field) if isinstance(item, Mapping) else getattr(item, field, None)
                    value = _optional_float(raw)
                    if value is None:
                        continue
                    result[nm_id][field] = value
                    result[nm_id][f"{field}__date"] = str(snapshot_date)
                    result[nm_id][f"{field}__observed_at"] = str(captured_at or "")
            if all(all(field in result[nm_id] for field in fields) for nm_id in requested):
                break
        return result

    def _stabilization_warnings(self, *, nm_id: int, parameter: str, actor: str) -> list[dict[str, Any]]:
        settings = validate_forecast_settings(self.get_settings(user_key=actor)["forecast"])
        latest = self.runtime.latest_sku_action_events_by_nm([nm_id]).get(nm_id, {})
        now = self.now_factory()
        warnings: list[dict[str, Any]] = []
        same_days = settings.price_stabilization_days if parameter == PRICE_PARAMETER else settings.bid_stabilization_days
        same = latest.get(parameter)
        if same_days > 0 and same:
            elapsed = _elapsed_days(now, same.get("confirmed_at"))
            if elapsed is not None and elapsed < same_days:
                remaining = same_days - elapsed
                label = "Цена" if parameter == PRICE_PARAMETER else "Рекламная ставка"
                warnings.append({"code": "same_parameter_stabilization", "message": f"{label} этого SKU изменялась {elapsed} дн. назад. Для стабильного наблюдения рекомендуется подождать ещё {remaining} дн.", "elapsed_days": elapsed, "remaining_days": remaining})
        if settings.cross_warnings_enabled:
            other = BID_PARAMETER if parameter == PRICE_PARAMETER else PRICE_PARAMETER
            other_days = settings.bid_stabilization_days if other == BID_PARAMETER else settings.price_stabilization_days
            other_event = latest.get(other)
            if other_days > 0 and other_event:
                elapsed = _elapsed_days(now, other_event.get("confirmed_at"))
                if elapsed is not None and elapsed < other_days:
                    source_label = "рекламной ставки" if other == BID_PARAMETER else "цены"
                    target_label = "цены" if parameter == PRICE_PARAMETER else "рекламной ставки"
                    warnings.append({"code": "cross_parameter_stabilization", "message": f"По этому SKU продолжается период стабилизации после изменения {source_label}. Изменение {target_label} усложнит оценку результата.", "elapsed_days": elapsed, "remaining_days": other_days - elapsed})
        return warnings

    def _poll_price_upload_and_tuple(
        self,
        *,
        upload_id: int,
        nm_id: int,
        target: float,
        expected_price: int,
        expected_discount: int,
    ) -> tuple[
        float | None,
        dict[str, Any],
        Mapping[str, Any],
        dict[str, Any],
    ]:
        started = self.monotonic_factory()
        deadline = (
            started + self.readback_deadline_seconds
            if self.readback_deadline_seconds > 0
            else None
        )
        status_payload: Mapping[str, Any] = {}
        observed: dict[str, Any] = {
            "price": None,
            "discount": None,
            "seller_price": None,
            "status": "missing",
        }
        status_reads = 0
        tuple_reads = 0
        cadence: list[float] = []
        attempts = 0
        for attempt in range(self.readback_attempts):
            attempts = attempt + 1
            status_reads += 1
            status_payload = self.prices_block.get_upload_task(upload_id)
            if status_payload.get("is_final"):
                if status_payload.get("status") != "success":
                    break
                tuple_reads += 1
                goods = normalize_goods_payload(
                    self.prices_block.source.fetch_goods_by_nm_ids([nm_id])
                )
                if goods:
                    good = goods[0]
                    observed = {
                        "price": float(good.price),
                        "discount": int(good.discount),
                        "seller_price": float(good.discounted_price),
                        "status": "observed",
                    }
                    if (
                        int(good.price) == int(expected_price)
                        and int(good.discount) == int(expected_discount)
                        and abs(float(good.discounted_price) - target) < 0.011
                    ):
                        observed["status"] = "matching"
                        return (
                            float(good.discounted_price),
                            observed,
                            status_payload,
                            {
                                "attempts": attempts,
                                "status_reads": status_reads,
                                "tuple_reads": tuple_reads,
                                "deadline_seconds": self.readback_deadline_seconds,
                                "cadence_seconds": cadence,
                                "elapsed_ms": round(
                                    max(
                                        self.monotonic_factory() - started,
                                        0.0,
                                    )
                                    * 1000,
                                    2,
                                ),
                                "outcome": "matching",
                            },
                        )
            if attempt + 1 >= self.readback_attempts:
                break
            now = self.monotonic_factory()
            if deadline is not None and now >= deadline:
                break
            delay = min(
                self.readback_delay_seconds * (1.7**attempt),
                3.0,
            )
            if deadline is not None:
                delay = min(delay, max(deadline - now, 0.0))
            cadence.append(round(delay, 3))
            if delay > 0:
                self.sleep(delay)
        if observed.get("seller_price") is not None:
            observed["status"] = "mismatch"
        return (
            None,
            observed,
            status_payload,
            {
                "attempts": attempts,
                "status_reads": status_reads,
                "tuple_reads": tuple_reads,
                "deadline_seconds": self.readback_deadline_seconds,
                "cadence_seconds": cadence,
                "elapsed_ms": round(
                    max(self.monotonic_factory() - started, 0.0) * 1000,
                    2,
                ),
                "outcome": (
                    "upload_error"
                    if status_payload.get("is_final")
                    and status_payload.get("status") != "success"
                    else "mismatch"
                    if observed.get("seller_price") is not None
                    else "deadline_or_attempts_exhausted"
                ),
            },
        )

    def _readback_price(
        self,
        nm_id: int,
        *,
        target: float,
        expected_price: int,
        expected_discount: int,
    ) -> tuple[float | None, dict[str, Any]]:
        observed: dict[str, Any] = {
            "price": None,
            "discount": None,
            "seller_price": None,
            "status": "missing",
        }
        for attempt in range(self.readback_attempts):
            goods = normalize_goods_payload(self.prices_block.source.fetch_goods_by_nm_ids([nm_id]))
            if goods:
                good = goods[0]
                observed = {
                    "price": float(good.price),
                    "discount": int(good.discount),
                    "seller_price": float(good.discounted_price),
                    "status": "observed",
                }
                if (
                    int(good.price) == int(expected_price)
                    and int(good.discount) == int(expected_discount)
                    and abs(float(good.discounted_price) - target) < 0.011
                ):
                    observed["status"] = "matching"
                    return float(good.discounted_price), observed
            if attempt + 1 < self.readback_attempts:
                self.sleep(self.readback_delay_seconds)
        observed["status"] = "mismatch" if observed.get("seller_price") is not None else "missing"
        return None, observed

    def _assert_price_not_quarantined(self, nm_id: int) -> None:
        try:
            rows = self._load_price_quarantine_rows(nm_id)
        except SkuManagementError:
            raise
        except Exception as exc:
            raise SkuManagementError(
                "quarantine evidence is unavailable before commit",
                http_status=503,
                payload={"safety_status": "quarantine_evidence_unavailable"},
            ) from exc
        if rows:
            raise SkuManagementError(
                "current WB quarantine blocks the price commit",
                http_status=409,
                payload={"safety_status": "current_quarantine", "quarantine": rows},
            )

    def _load_price_quarantine_rows(self, nm_id: int) -> list[Mapping[str, Any]]:
        limit = 1000
        for page in range(10):
            rows = list(
                self.prices_block.get_quarantine_goods({"limit": limit, "offset": page * limit}).get("rows", [])
            )
            matched = [item for item in rows if int(item.get("nmID") or 0) == nm_id]
            if matched:
                return matched
            if len(rows) < limit:
                return []
        raise SkuManagementError(
            "quarantine evidence exceeds bounded pagination; price action is blocked",
            http_status=503,
            payload={"safety_status": "quarantine_evidence_truncated"},
        )

    def _assert_price_promo_fresh(
        self,
        preview: Mapping[str, Any],
        *,
        current_goods_payload: Mapping[str, Any] | None = None,
    ) -> None:
        nm_id = int(preview["nm_id"])
        table_params: dict[str, Any] = {"filterNmID": nm_id}
        if current_goods_payload is not None:
            table_params["_current_goods_payload"] = current_goods_payload
        try:
            current = next(
                (
                    item
                    for item in self.prices_block.build_goods_table(
                        table_params
                    ).get("rows", [])
                    if int(item.get("nmID") or 0) == nm_id
                ),
                {},
            )
        except Exception:
            current = {}
        current_snapshot = self._price_promo_snapshot(nm_id=nm_id, current_view=current)
        if current_snapshot != dict(preview.get("promo") or {}):
            raise SkuManagementError(
                "promo evidence changed after preview; create a new preview",
                http_status=409,
                payload={"safety_status": "promo_evidence_changed", "promo": current_snapshot},
            )

    def _price_promo_snapshot(self, *, nm_id: int, current_view: Mapping[str, Any]) -> dict[str, Any]:
        metrics = self._snapshot_projection([nm_id]).get(nm_id, {})
        participation = _optional_float(metrics.get("promo_participation"))
        participation_date = str(metrics.get("promo_participation__date") or "")
        count = _optional_float(metrics.get("promo_count_by_price"))
        count_date = str(metrics.get("promo_count_by_price__date") or "")
        captured_at = str(
            metrics.get("promo_participation__observed_at")
            or metrics.get("promo_count_by_price__observed_at")
            or ""
        )
        freshness = min(
            (item for item in (participation_date, count_date) if _is_iso_date(item)),
            default="",
        )
        quality = (
            "observed"
            if participation is not None and count is not None
            else "partial" if participation is not None or count is not None
            else "missing"
        )
        return {
            "label": str(current_view.get("promoLabel") or "н/д"),
            "eligible_count": _optional_float(current_view.get("promoEligibleCount")),
            "current_count": _optional_float(current_view.get("promoCurrentCount")),
            "participation": participation,
            "participation_count": count,
            "freshness": freshness,
            "observed_at": captured_at,
            "quality": quality,
            "reason": str(current_view.get("promoReason") or ""),
        }

    def _fetch_buyer_price(self, nm_id: int) -> dict[str, Any]:
        observed_at = self.timestamp_factory()
        today = current_business_date_iso(self.now_factory())
        try:
            payload = self.buyer_price_source.fetch(SppProxyRequest(snapshot_type="spp_proxy", snapshot_date=today, nm_ids=[nm_id]))
            items = ((payload.get("data") or {}).get("items") or []) if isinstance(payload, Mapping) else []
            item = next((row for row in items if int(row.get("nmId") or 0) == nm_id), None)
            value = _optional_float((item or {}).get("public_buyer_price"))
            diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), Mapping) else {}
            source_date = str(payload.get("snapshot_date") or today)
            quality = (
                "observed"
                if value is not None and source_date == today and diagnostics.get("fresh") is not False
                else "stale" if value is not None
                else "missing"
            )
            return {"value": value, "observed_at": observed_at, "source": "public_wb_card", "freshness": source_date, "quality": quality, "evidence": item or diagnostics}
        except Exception as exc:
            return {"value": None, "observed_at": observed_at, "source": "public_wb_card", "freshness": today, "quality": "error", "error": str(exc)}

    def _readback_buyer_price(self, nm_id: int) -> dict[str, Any]:
        latest: dict[str, Any] = {}
        for attempt in range(self.readback_attempts):
            latest = self._fetch_buyer_price(nm_id)
            if latest.get("quality") == "observed" and latest.get("value") is not None:
                return latest
            if attempt + 1 < self.readback_attempts:
                self.sleep(self.readback_delay_seconds)
        return latest

    def _persist_event(
        self,
        *,
        preview: Mapping[str, Any],
        actor: str,
        requested_value: float,
        confirmed_value: float,
        old_value: float,
        status: str,
        stabilization_override: bool,
        warning_override: bool,
        readback_status: str,
        readback: Mapping[str, Any],
        confirmed_at: str,
    ) -> dict[str, Any]:
        delta = _money_decimal(confirmed_value, "confirmed_value") - _money_decimal(old_value, "old_value")
        event = {
            "event_id": uuid4().hex,
            "nm_id": int(preview["nm_id"]),
            "parameter": str(preview["parameter"]),
            "old_value": old_value,
            "requested_value": requested_value,
            "confirmed_value": confirmed_value,
            "delta": float(delta.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            "requested_at": str(preview.get("created_at") or ""),
            "confirmed_at": confirmed_at,
            "actor": actor,
            "source": "sku_management",
            "advert_id": _optional_int(preview.get("advert_id")),
            "campaign": str(preview.get("campaign_name") or ""),
            "placement": str(preview.get("placement") or ""),
            "preview_id": str(preview.get("preview_id") or ""),
            "correlation_id": str(preview.get("operation_id") or ""),
            "commit_status": status,
            "readback_status": readback_status,
            "readback": dict(readback),
            "warnings": list(preview.get("warnings") or []),
            "stabilization_override": bool(stabilization_override),
            "warning_override": bool(warning_override),
            "error": "",
        }
        return self.runtime.create_sku_action_event(event)

    def _persist_failure(
        self,
        *,
        preview: Mapping[str, Any],
        actor: str,
        exc: Exception,
        stabilization_override: bool,
        warning_override: bool,
    ) -> None:
        payload = getattr(exc, "payload", {})
        readback_status = str(payload.get("readback_status") or "") if isinstance(payload, Mapping) else ""
        if not readback_status:
            readback_status = "mismatch" if "mismatch" in str(exc).lower() or "does not match" in str(exc).lower() else "error"
        self.runtime.create_sku_action_event({
            "event_id": uuid4().hex, "nm_id": int(preview["nm_id"]), "parameter": str(preview["parameter"]),
            "old_value": _optional_float(preview["old_value"] if "old_value" in preview else (preview.get("current") or {}).get("discountedPrice")),
            "requested_value": _optional_float(preview["requested_value"] if "requested_value" in preview else preview.get("target_seller_price")),
            "confirmed_value": None, "delta": None, "requested_at": str(preview.get("created_at") or ""), "confirmed_at": "",
            "actor": actor, "source": "sku_management", "advert_id": _optional_int(preview.get("advert_id")),
            "campaign": str(preview.get("campaign_name") or ""), "placement": str(preview.get("placement") or ""),
            "preview_id": str(preview.get("preview_id") or ""), "correlation_id": str(preview.get("operation_id") or ""),
            "commit_status": "error", "readback_status": readback_status,
            "readback": payload if isinstance(payload, Mapping) else {}, "warnings": list(preview.get("warnings") or []),
            "stabilization_override": bool(stabilization_override), "warning_override": bool(warning_override),
            "error": str(exc),
        })

    def _validate_confirmation(self, preview: Mapping[str, Any], *, payload: Mapping[str, Any], actor: str) -> None:
        if str(preview.get("actor") or "") != actor:
            raise SkuManagementError("preview belongs to another operator", http_status=403)
        if not _boolean(payload.get("confirm", False)):
            raise SkuManagementError("confirm=true is required", http_status=400)
        override_required = list(preview.get("override_required_warnings") or [])
        stabilization_override = _boolean(payload.get("override_stabilization", False))
        warning_override = _boolean(payload.get("override_warnings", False))
        if override_required and not warning_override:
            only_stabilization = set(override_required).issubset(
                {str(item.get("code") or "") for item in preview.get("stabilization_warnings") or []}
            )
            if not (only_stabilization and stabilization_override):
                raise SkuManagementError(
                    "preview warnings require explicit override",
                    http_status=409,
                    payload={"warnings": override_required},
                )

    def _save_preview(self, payload: Mapping[str, Any]) -> None:
        self._preview_dir.mkdir(parents=True, exist_ok=True)
        (self._preview_dir / f"{payload['preview_id']}.json").write_text(json.dumps(dict(payload), ensure_ascii=False, sort_keys=True), encoding="utf-8")

    def _claim_preview(self, preview: dict[str, Any]) -> None:
        claim_path = self._preview_dir / f"{preview['preview_id']}.claim"
        try:
            with claim_path.open("x", encoding="utf-8") as handle:
                handle.write(self.timestamp_factory())
        except FileExistsError as exc:
            raise SkuManagementError("preview was already committed or attempted; create a new preview", http_status=409)
        preview["commit_started_at"] = self.timestamp_factory()
        self._save_preview(preview)

    def _load_preview(self, preview_id: Any, *, expected_parameter: str) -> dict[str, Any]:
        normalized = str(preview_id or "").strip()
        if not normalized.isalnum():
            raise SkuManagementError("invalid preview_id", http_status=400)
        path = self._preview_dir / f"{normalized}.json"
        if not path.exists():
            raise SkuManagementError("preview_id not found", http_status=404)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("parameter") != expected_parameter:
            raise SkuManagementError("preview parameter mismatch", http_status=409)
        if int(payload.get("expires_at_epoch") or 0) < int(time.time()):
            raise SkuManagementError("preview is stale; create a new preview", http_status=409)
        return payload


def _first_problem_district(
    *,
    as_of_date: date,
    horizon_end: date,
    safety_days: int,
    districts: Mapping[str, Mapping[str, Any]],
    inbounds: Sequence[ForecastInbound],
) -> tuple[str | None, str]:
    candidates: list[tuple[date, str]] = []
    evidence_count = 0
    for key, row in districts.items():
        stock = _optional_float(row.get("stock"))
        demand = _optional_float(row.get("daily_demand"))
        if stock is None or demand is None or demand < 0:
            continue
        evidence_count += 1
        if demand == 0:
            continue
        threshold = demand * safety_days
        day = as_of_date
        balance = stock
        while day <= horizon_end:
            balance += sum(
                float(item.quantity)
                for item in inbounds
                if item.district_key == key and item.arrival_date == day.isoformat()
            )
            balance -= demand
            if balance < threshold:
                candidates.append((day, str(key)))
                break
            day += timedelta(days=1)
    if evidence_count == 0:
        return "unknown", "unknown"
    return (min(candidates)[1] if candidates else None), "available"


def _select_observed_buyer_price(
    *,
    event_buyer: Mapping[str, Any],
    metrics: Mapping[str, Any],
    current_buyer: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    event_value = _optional_float(event_buyer.get("value"))
    if event_value is not None and str(event_buyer.get("quality") or "") == "observed":
        candidates.append(
            {
                "value": event_value,
                "source": str(event_buyer.get("source") or "public_wb_card"),
                "freshness": str(event_buyer.get("freshness") or event_buyer.get("observed_at") or ""),
                "observed_at": str(event_buyer.get("observed_at") or ""),
                "quality": "observed",
                "_sort_freshness": str(event_buyer.get("observed_at") or event_buyer.get("freshness") or ""),
            }
        )
    metric_value = _optional_float(metrics.get("buyer_price_rub"))
    if metric_value is not None:
        candidates.append(
            {
                "value": metric_value,
                "source": "web_vitrina_spp_proxy_projection",
                "freshness": str(metrics.get("buyer_price_rub__date") or ""),
                "observed_at": str(metrics.get("buyer_price_rub__observed_at") or ""),
                "quality": "observed",
                "_sort_freshness": str(
                    metrics.get("buyer_price_rub__observed_at")
                    or metrics.get("buyer_price_rub__date")
                    or ""
                ),
            }
        )
    current_value = _optional_float((current_buyer or {}).get("value"))
    if current_value is not None and str((current_buyer or {}).get("quality") or "") == "observed":
        candidates.append(
            {
                "value": current_value,
                "source": str((current_buyer or {}).get("source") or "spp_proxy_temporal_snapshot"),
                "freshness": str((current_buyer or {}).get("freshness") or ""),
                "quality": "observed",
                "observed_at": str((current_buyer or {}).get("observed_at") or ""),
                "_sort_freshness": str(
                    (current_buyer or {}).get("observed_at")
                    or (current_buyer or {}).get("freshness")
                    or ""
                ),
            }
        )
    if not candidates:
        return {"value": None, "source": "public_wb_card", "freshness": "", "quality": "missing"}
    selected = max(candidates, key=lambda item: str(item.get("_sort_freshness") or ""))
    return {key: value for key, value in selected.items() if not key.startswith("_")}


def _sanitize_table_preferences(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw_filters = dict(payload.get("filters") or {}) if isinstance(payload.get("filters"), Mapping) else {}
    filters = {
        key: str(raw_filters[key])[:200]
        for key in TABLE_FILTER_KEYS
        if key in raw_filters and isinstance(raw_filters[key], (str, int, float)) and not isinstance(raw_filters[key], bool)
    }
    widths = {
        str(key): min(max(int(value), 60), 600)
        for key, value in (payload.get("column_widths") or {}).items()
        if str(key) in TABLE_COLUMN_KEYS and str(value).isdigit()
    } if isinstance(payload.get("column_widths"), Mapping) else {}
    sort_supplied = isinstance(payload.get("sort"), list)
    sort = []
    for item in payload.get("sort", []) if sort_supplied else []:
        if (
            isinstance(item, Mapping)
            and str(item.get("key")) in TABLE_SORT_KEYS
            and str(item.get("direction")) in {"asc", "desc"}
        ):
            sort.append({"key": str(item.get("key") or ""), "direction": str(item["direction"])})
    visible_columns = [item for item in _string_list(payload.get("visible_columns")) if item in TABLE_COLUMN_KEYS]
    if visible_columns:
        visible_columns = [*TABLE_MANDATORY_COLUMNS, *visible_columns]
        visible_columns = list(dict.fromkeys(visible_columns))
    requested_order = [
        item
        for item in _string_list(payload.get("column_order"))
        if item in TABLE_COLUMN_KEYS and item not in TABLE_MANDATORY_COLUMNS
    ]
    return {
        "visible_columns": visible_columns,
        "column_order": [*TABLE_MANDATORY_COLUMNS, *requested_order],
        "column_widths": widths,
        "filters": filters,
        "sort": sort if sort_supplied else list(DEFAULT_TABLE_PREFERENCES["sort"]),
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        normalized = str(item or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result[:100]


def _temporal_payload_items(payload: Any) -> list[Any]:
    if payload is None:
        return []
    direct = (
        payload.get("items")
        if isinstance(payload, Mapping)
        else getattr(payload, "items", None)
    )
    if isinstance(direct, (list, tuple)):
        return list(direct)
    nested = (
        payload.get("result")
        if isinstance(payload, Mapping)
        else getattr(payload, "result", None)
    )
    items = (
        nested.get("items")
        if isinstance(nested, Mapping)
        else getattr(nested, "items", None)
    )
    return list(items) if isinstance(items, (list, tuple)) else []


def _dedupe_strings(values: Sequence[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise SkuManagementError(f"{field} must be an integer", http_status=422) from exc
    if parsed < minimum or parsed > maximum:
        raise SkuManagementError(f"{field} must be between {minimum} and {maximum}", http_status=422)
    return parsed


def _positive_int(value: Any, field: str) -> int:
    return _integer(value, field, 1, 10**15)


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _wb_supply_quantities(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return stage-aware supply quantities without re-adding units already in current WB stock."""

    planned = next(
        (
            quantity
            for key in ("quantity", "qty", "supplierBoxAmount", "supplier_box_amount")
            if (quantity := _optional_float(value.get(key))) is not None and quantity > 0
        ),
        None,
    )
    progressed_candidates = [
        quantity
        for key in (
            "readyForSaleQuantity", "ready_for_sale_quantity",
            "acceptedQuantity", "accepted_quantity",
            "addedQuantity", "added_quantity",
        )
        if (quantity := _optional_float(value.get(key))) is not None and quantity > 0
    ]
    progressed = max(progressed_candidates, default=0.0)
    if planned is None:
        return {
            "planned": None,
            "progressed": progressed,
            "remaining": None,
            "progressed_exceeds_planned": False,
        }
    return {
        "planned": planned,
        "progressed": progressed,
        "remaining": max(planned - progressed, 0.0),
        "progressed_exceeds_planned": progressed > planned,
    }


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _money_decimal(value: Any, field: str) -> Decimal:
    try:
        return Decimal(str(value).strip().replace(",", ".")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception as exc:
        raise SkuManagementError(f"{field} must be numeric", http_status=422) from exc


def _is_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(str(value))
        return True
    except ValueError:
        return False


def _elapsed_days(now: datetime, timestamp: Any) -> int | None:
    try:
        parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max((now.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)).days, 0)
    except (TypeError, ValueError):
        return None
