"""Independent planning order for an own FBS fulfillment facility."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import math
from typing import Any, Mapping
from uuid import uuid4

from packages.application.demand_estimation import (
    estimate_availability_adjusted_demand_for_window,
)
from packages.application.factory_order_sales_history import (
    FactoryOrderAuthoritativeSalesHistory,
)
from packages.application.inventory_planning_read_model import InventoryPlanningReadModel
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.adapters.sales_funnel_history_block import HttpBackedSalesFunnelHistorySource
from packages.application.sales_funnel_history_block import SalesFunnelHistoryBlock
from packages.application.simple_xlsx import build_single_sheet_workbook_bytes
from packages.application.supply_calculation_registry import (
    build_fbs_fulfillment_order_calculation_evidence,
)
from packages.business_time import current_business_date_iso
from packages.contracts.fbs_fulfillment_order import (
    DEFAULT_SALES_HISTORY_DAYS,
    INBOUND_SCOPE_ALL_ACTIVE,
    INBOUND_SCOPE_SELECTED_FACILITY,
    INBOUND_SCOPES,
    MOSCOW_CITY,
    SALES_HISTORY_MODE_CUSTOM_PERIOD,
    SALES_HISTORY_MODE_LAST_N_DAYS,
    SALES_HISTORY_MODES,
    FbsFulfillmentOrderResult,
    FbsFulfillmentOrderRow,
    FbsFulfillmentOrderSettings,
    FbsFulfillmentOrderStatus,
    FbsFulfillmentOrderSummary,
)
from packages.contracts.supplier_shipments import (
    LINE_TYPE_PRODUCT,
    MATCH_STATUSES_WITH_AUTHORITATIVE_NM_ID,
    ORDER_STATUS_IN_TRANSIT,
    ORDER_STATUS_PRODUCTION,
)


DEFAULTS = {
    "inbound_scope": INBOUND_SCOPE_SELECTED_FACILITY,
    "production_days": 30,
    "factory_to_target_ff_days": 30,
    "ff_safety_days": 15,
    "order_cycle_days": 14,
    "order_batch_qty": 250,
    "sales_history_mode": SALES_HISTORY_MODE_LAST_N_DAYS,
    "sales_avg_period_days": DEFAULT_SALES_HISTORY_DAYS,
}
NATIONAL_DEMAND_SCOPE = "russia_total_orderCount"
WB_STOCK_USED = False
_WEIGHT_COEFFICIENT = 0.08593
_VOLUME_DIVISOR = 204.38


class FbsFulfillmentOrderBlock:
    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        sales_funnel_history_block: SalesFunnelHistoryBlock | None = None,
        now_factory: callable | None = None,
        timestamp_factory: callable | None = None,
    ) -> None:
        self.runtime = runtime
        self.now_factory = now_factory or _default_now_factory
        self.timestamp_factory = timestamp_factory or _default_timestamp_factory
        history_block = sales_funnel_history_block or SalesFunnelHistoryBlock(
            HttpBackedSalesFunnelHistorySource()
        )
        self.sales_history = FactoryOrderAuthoritativeSalesHistory(
            runtime=runtime,
            sales_funnel_history_block=history_block,
            now_factory=self.now_factory,
            timestamp_factory=self.timestamp_factory,
        )
        self.inventory = InventoryPlanningReadModel(db_path=runtime.db_path)

    def build_status(self) -> FbsFulfillmentOrderStatus:
        active_skus = self._load_active_skus()
        planning = self.inventory.current_fbs_facilities(
            requested_nm_ids=[nm_id for nm_id, _ in active_skus]
        )
        facilities = self._facility_readiness(planning, active_skus)
        for facility in facilities:
            inbound = self._remaining_inbound(
                selected_facility_id=str(facility["facility_id"]),
                active_skus=active_skus,
                inbound_scope=INBOUND_SCOPE_SELECTED_FACILITY,
            )
            facility["remaining_active_inbound_qty"] = inbound["total_quantity"]
            facility["remaining_active_inbound_shipment_count"] = inbound[
                "included_shipment_count"
            ]
        coverage = self.sales_history.describe_coverage()
        last_result = self.runtime.load_fbs_fulfillment_order_result_state()
        any_executable = any(
            bool(facility.get("calculation_enabled")) for facility in facilities
        )
        return FbsFulfillmentOrderStatus(
            status=(
                "ready"
                if any_executable
                else "blocked"
                if facilities
                else "unavailable"
            ),
            active_sku_count=len(active_skus),
            national_demand_scope=NATIONAL_DEMAND_SCOPE,
            wb_stock_used=WB_STOCK_USED,
            facilities=tuple(facilities),
            sales_history_coverage={
                "earliest_available_date": coverage.earliest_available_date,
                "latest_available_date": coverage.latest_available_date,
                "exact_date_snapshot_count": coverage.exact_date_snapshot_count,
            },
            defaults=dict(DEFAULTS),
            last_result=last_result,
        )

    def calculate(self, payload: Mapping[str, Any]) -> FbsFulfillmentOrderResult:
        settings = _parse_settings(payload)
        current_business_date = date.fromisoformat(
            current_business_date_iso(self.now_factory())
        )
        report_date = (
            date.fromisoformat(settings.report_date_override)
            if settings.report_date_override
            else current_business_date
        )
        if report_date > current_business_date:
            raise ValueError("Дата расчёта не может быть в будущем")

        active_skus = self._load_active_skus()
        if not active_skus:
            raise ValueError("Нет active SKU для расчёта")
        planning = self.inventory.current_fbs_facilities(
            requested_nm_ids=[nm_id for nm_id, _ in active_skus]
        )
        readiness = self._facility_readiness(planning, active_skus)
        selected = next(
            (
                facility
                for facility in readiness
                if facility["facility_id"] == settings.target_facility_id
            ),
            None,
        )
        if selected is None:
            raise ValueError("Выбранный целевой фулфилмент не существует или не active")
        if not selected["calculation_enabled"]:
            reasons = "; ".join(selected["blockers"])
            raise ValueError(
                f"Расчёт для {selected['name']} заблокирован: {reasons}"
            )

        history_from, history_to = _resolve_sales_window(settings, report_date)
        order_counts_by_nm = self.sales_history.load_order_count_samples_by_date(
            date_from=history_from.isoformat(),
            date_to=history_to.isoformat(),
            nm_ids=[nm_id for nm_id, _ in active_skus],
            clamp_to_coverage=False,
        )
        inbound = self._remaining_inbound(
            selected_facility_id=str(selected["facility_id"]),
            active_skus=active_skus,
            inbound_scope=settings.inbound_scope,
        )
        inbound_by_nm = {
            int(key): float(value)
            for key, value in dict(inbound["quantity_by_nm_id"]).items()
        }
        selected_sku_values = {
            int(item["nm_id"]): item for item in selected["sku_values"]
        }
        horizon_days = (
            settings.production_days
            + settings.factory_to_target_ff_days
            + settings.ff_safety_days
            + settings.order_cycle_days
        )
        warnings: list[str] = []
        rows: list[FbsFulfillmentOrderRow] = []
        unavailable_demand: list[int] = []
        for nm_id, sku_comment in active_skus:
            demand = estimate_availability_adjusted_demand_for_window(
                order_counts_by_nm.get(nm_id, []),
                date_from=history_from,
                date_to=history_to,
            )
            if demand.used_trading_day_count == 0:
                unavailable_demand.append(nm_id)
                continue
            if demand.demand_warning:
                warnings.append(f"nmId {nm_id}: {demand.demand_warning}")
            ledger = selected_sku_values[nm_id]
            physical = int(ledger["physical"])
            reserved = int(ledger["reserved"])
            available = int(ledger["available"])
            inbound_qty = float(inbound_by_nm.get(nm_id, 0.0))
            target_qty = demand.daily_demand_total * horizon_days
            coverage_qty = float(available) + inbound_qty
            shortage_qty = max(target_qty - coverage_qty, 0.0)
            recommended = (
                int(
                    math.ceil(shortage_qty / settings.order_batch_qty)
                    * settings.order_batch_qty
                )
                if shortage_qty > 0
                else 0
            )
            rows.append(
                FbsFulfillmentOrderRow(
                    nm_id=nm_id,
                    sku_comment=sku_comment,
                    recommended_order_qty=recommended,
                    national_daily_demand=demand.daily_demand_total,
                    target_qty=target_qty,
                    coverage_qty=coverage_qty,
                    shortage_qty=shortage_qty,
                    selected_facility_physical_fbs=physical,
                    selected_facility_reserved_fbs=reserved,
                    selected_facility_available_fbs=available,
                    remaining_active_inbound_qty=inbound_qty,
                    demand_estimation_mode=demand.demand_estimation_mode,
                    sales_history_mode=settings.sales_history_mode,
                    sales_avg_period_days=(
                        settings.sales_avg_period_days
                        if settings.sales_history_mode
                        == SALES_HISTORY_MODE_LAST_N_DAYS
                        else None
                    ),
                    sales_date_from=history_from.isoformat(),
                    sales_date_to=history_to.isoformat(),
                    sales_calendar_day_count=demand.calendar_day_count,
                    used_trading_day_count=demand.used_trading_day_count,
                    excluded_day_count=demand.excluded_day_count,
                    included_sales_dates=demand.included_dates,
                    excluded_sales_dates=demand.excluded_dates,
                    baseline_daily_sales=demand.baseline_daily_sales,
                    valid_day_threshold=demand.valid_day_threshold,
                    raw_window_daily_demand=demand.raw_window_daily_demand,
                    demand_warning=demand.demand_warning,
                    demand_notes=demand.demand_notes,
                )
            )
        if unavailable_demand:
            raise ValueError(
                "Недостаточно валидной истории orderCount внутри выбранного периода "
                "для nmId: " + ", ".join(str(item) for item in unavailable_demand)
            )

        total_qty = sum(row.recommended_order_qty for row in rows)
        used_counts = [row.used_trading_day_count for row in rows]
        summary = FbsFulfillmentOrderSummary(
            total_qty=total_qty,
            estimated_weight=round(total_qty * _WEIGHT_COEFFICIENT, 2),
            estimated_volume=round(
                total_qty * _WEIGHT_COEFFICIENT / _VOLUME_DIVISOR,
                2,
            ),
            sales_calendar_day_count=(history_to - history_from).days + 1,
            used_trading_days_min=min(used_counts, default=0),
            used_trading_days_max=max(used_counts, default=0),
        )
        sales_window = {
            "mode": settings.sales_history_mode,
            "requested_last_n_days": (
                settings.sales_avg_period_days
                if settings.sales_history_mode == SALES_HISTORY_MODE_LAST_N_DAYS
                else None
            ),
            "requested_date_from": settings.sales_date_from,
            "requested_date_to": settings.sales_date_to,
            "actual_date_from": history_from.isoformat(),
            "actual_date_to": history_to.isoformat(),
            "inclusive": True,
            "calendar_day_count": (history_to - history_from).days + 1,
            "outside_window_samples_used": False,
            "used_trading_days_min": summary.used_trading_days_min,
            "used_trading_days_max": summary.used_trading_days_max,
        }
        result = FbsFulfillmentOrderResult(
            status="success",
            calculation_id=uuid4().hex,
            calculated_at=self.timestamp_factory(),
            report_date=report_date.isoformat(),
            target_facility_id=str(selected["facility_id"]),
            target_facility_name=str(selected["name"]),
            national_demand_scope=NATIONAL_DEMAND_SCOPE,
            wb_stock_used=WB_STOCK_USED,
            horizon_days=horizon_days,
            settings=settings,
            sales_window=sales_window,
            facility_readiness=dict(selected),
            inbound_coverage=inbound,
            summary=summary,
            rows=rows,
            warnings=tuple(dict.fromkeys(warnings)),
        )
        export_bytes, export_filename = self._build_export(result)
        evidence = build_fbs_fulfillment_order_calculation_evidence(
            runtime=self.runtime,
            result=asdict(result),
            order_count_samples_by_nm=order_counts_by_nm,
            planning_inventory=planning,
            inbound_coverage=inbound,
        )
        self.runtime.save_fbs_fulfillment_order_result_state(
            calculated_at=result.calculated_at,
            payload=asdict(result),
            evidence=evidence,
            export_bytes=export_bytes,
            export_filename=export_filename,
            export_content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
        return result

    def download_recommendation(self) -> tuple[bytes, str]:
        result = self.runtime.load_fbs_fulfillment_order_result_state()
        if not result:
            raise ValueError("Результат FBS-расчёта ещё не подготовлен")
        calculation_id = str(result.get("calculation_id") or "")
        body, filename, _ = self.runtime.load_supply_calculation_registry_export(
            calculation_id
        )
        return body, filename

    def _facility_readiness(
        self,
        planning: Mapping[str, Any],
        active_skus: list[tuple[int, str]],
    ) -> list[dict[str, Any]]:
        requested = {nm_id for nm_id, _ in active_skus}
        facilities: list[dict[str, Any]] = []
        for raw in planning.get("facilities") or []:
            if not isinstance(raw, Mapping) or not bool(raw.get("active")):
                continue
            sku_values = [
                dict(item)
                for item in raw.get("sku_values") or []
                if isinstance(item, Mapping) and int(item.get("nm_id") or 0) in requested
            ]
            by_nm_id = {int(item["nm_id"]): item for item in sku_values}
            inapplicable_nm_ids = sorted(
                nm_id
                for nm_id in requested
                if nm_id in by_nm_id
                and str(by_nm_id[nm_id].get("state") or "") == "inapplicable"
            )
            missing_nm_ids = sorted(
                nm_id
                for nm_id in requested
                if nm_id not in by_nm_id
                or str(by_nm_id[nm_id].get("state") or "") == "missing"
                or by_nm_id[nm_id].get("physical") is None
                or by_nm_id[nm_id].get("available") is None
                if nm_id not in inapplicable_nm_ids
            )
            is_moscow = (
                str(raw.get("city") or "").strip() == MOSCOW_CITY
                or str(raw.get("name") or "").strip() == "FF Москва"
            )
            blockers: list[str] = []
            if missing_nm_ids:
                blockers.append(
                    "Нет полного подтверждённого facility-specific FBS physical ledger "
                    "для active SKU: "
                    + ", ".join(str(item) for item in missing_nm_ids)
                )
            if inapplicable_nm_ids:
                blockers.append(
                    "SKU явно неприменимы к выбранному FBS facility: "
                    + ", ".join(str(item) for item in inapplicable_nm_ids)
                )
            if not is_moscow:
                blockers.append(
                    "MVP закрепляет общероссийский спрос только за FF Москва; "
                    "второй независимый расчёт на 100% спроса запрещён"
                )
            facilities.append(
                {
                    "facility_id": str(raw.get("facility_id") or ""),
                    "code": str(raw.get("code") or ""),
                    "name": str(raw.get("name") or ""),
                    "city": str(raw.get("city") or ""),
                    "active": True,
                    "physical": raw.get("physical"),
                    "reserved": raw.get("reserved"),
                    "available": raw.get("available"),
                    "sku_values": sku_values,
                    "missing_physical_nm_ids": missing_nm_ids,
                    "inapplicable_nm_ids": inapplicable_nm_ids,
                    "calculation_enabled": not blockers,
                    "blockers": blockers,
                    "wb_stock_used": False,
                    "global_fbs_readiness_ignored_for_selected_facility": True,
                }
            )
        return facilities

    def _remaining_inbound(
        self,
        *,
        selected_facility_id: str,
        active_skus: list[tuple[int, str]],
        inbound_scope: str,
    ) -> dict[str, Any]:
        active_nm_ids = {nm_id for nm_id, _ in active_skus}
        totals: dict[int, float] = {}
        evidence_rows: list[dict[str, Any]] = []
        unassigned_count = 0
        explicit_other_count = 0
        unassigned_quantity = 0.0
        explicit_other_quantity = 0.0
        for shipment in self.runtime.list_supplier_shipments():
            status = str(shipment.get("order_status") or "")
            if status not in {ORDER_STATUS_PRODUCTION, ORDER_STATUS_IN_TRANSIT}:
                continue
            explicit_target = str(shipment.get("target_facility_id") or "").strip()
            if not explicit_target:
                unassigned_count += 1
            elif explicit_target != selected_facility_id:
                explicit_other_count += 1
            detail = self.runtime.load_supplier_shipment(
                str(shipment.get("shipment_id") or "")
            )
            if not detail:
                continue
            shipment_qty = 0.0
            quantity_by_nm: dict[int, float] = {}
            for line in detail.get("lines") or []:
                if line.get("line_type") != LINE_TYPE_PRODUCT:
                    continue
                if str(line.get("match_status") or "") not in MATCH_STATUSES_WITH_AUTHORITATIVE_NM_ID:
                    continue
                try:
                    nm_id = int(line.get("internal_nm_id"))
                    quantity = float(line.get("qty"))
                except (TypeError, ValueError):
                    continue
                if nm_id not in active_nm_ids or quantity <= 0:
                    continue
                quantity_by_nm[nm_id] = quantity_by_nm.get(nm_id, 0.0) + quantity
                shipment_qty += quantity
            if not explicit_target:
                unassigned_quantity += shipment_qty
            elif explicit_target != selected_facility_id:
                explicit_other_quantity += shipment_qty
            include_shipment = (
                inbound_scope == INBOUND_SCOPE_ALL_ACTIVE
                or explicit_target == selected_facility_id
            )
            if not include_shipment:
                continue
            if shipment_qty > 0:
                for nm_id, quantity in quantity_by_nm.items():
                    totals[nm_id] = totals.get(nm_id, 0.0) + quantity
                evidence_rows.append(
                    {
                        "shipment_id": str(shipment.get("shipment_id") or ""),
                        "order_status": status,
                        "target_facility_id": explicit_target,
                        "target_assignment_source": (
                            "explicit" if explicit_target else "unassigned"
                        ),
                        "remaining_quantity": shipment_qty,
                        "quantity_by_nm_id": quantity_by_nm,
                    }
                )
        return {
            "selected_facility_id": selected_facility_id,
            "scope": inbound_scope,
            "scope_label": (
                "Все активные заказы фабрике"
                if inbound_scope == INBOUND_SCOPE_ALL_ACTIVE
                else "Только для выбранного ФФ"
            ),
            "quantity_by_nm_id": totals,
            "total_quantity": sum(totals.values()),
            "included_shipments": evidence_rows,
            "included_shipment_count": len(evidence_rows),
            "unassigned_target_active_count": unassigned_count,
            "unassigned_target_excluded_count": (
                0 if inbound_scope == INBOUND_SCOPE_ALL_ACTIVE else unassigned_count
            ),
            "unassigned_target_included_count": (
                unassigned_count if inbound_scope == INBOUND_SCOPE_ALL_ACTIVE else 0
            ),
            "unassigned_target_eligible_quantity": unassigned_quantity,
            "unassigned_target_included": inbound_scope == INBOUND_SCOPE_ALL_ACTIVE,
            "legacy_null_target_fallback_moscow_count": 0,
            "explicit_other_facility_active_count": explicit_other_count,
            "explicit_other_facility_excluded_count": (
                0 if inbound_scope == INBOUND_SCOPE_ALL_ACTIVE else explicit_other_count
            ),
            "explicit_other_facility_included_count": (
                explicit_other_count
                if inbound_scope == INBOUND_SCOPE_ALL_ACTIVE
                else 0
            ),
            "explicit_other_facility_eligible_quantity": explicit_other_quantity,
            "explicit_other_facility_included": (
                inbound_scope == INBOUND_SCOPE_ALL_ACTIVE
            ),
            "active_statuses": [ORDER_STATUS_PRODUCTION, ORDER_STATUS_IN_TRANSIT],
            "accepted_or_inactive_excluded": True,
        }

    def _load_active_skus(self) -> list[tuple[int, str]]:
        state = self.runtime.load_current_state()
        enabled = sorted(
            (item for item in state.config_v2 if item.enabled),
            key=lambda item: item.display_order,
        )
        return [(int(item.nm_id), str(item.display_name)) for item in enabled]

    def _build_export(
        self,
        result: FbsFulfillmentOrderResult,
    ) -> tuple[bytes, str]:
        rows: list[list[Any]] = [
            [
                "nmId",
                "SKU",
                "Рекомендованный заказ, шт",
                "Национальный спрос, шт/день",
                "Target, шт",
                "Physical FBS, шт",
                "Резерв FBS, шт",
                "Доступно FBS, шт",
                "Активные входящие, шт",
                "Coverage, шт",
                "Режим истории",
                "Последние N дней",
                "Период с",
                "Период по",
                "Календарных дней",
                "Использовано торговых дней",
                "Включённые даты",
                "Исключённые даты",
                "Baseline продаж, шт/день",
                "Порог валидного дня, шт",
                "Спрос до очистки, шт/день",
                "Итоговый demand basis, шт/день",
                "WB stock used",
                "Целевой фулфилмент",
                "Охват заказов фабрике",
            ]
        ]
        for item in result.rows:
            rows.append(
                [
                    item.nm_id,
                    item.sku_comment,
                    item.recommended_order_qty,
                    round(item.national_daily_demand, 6),
                    round(item.target_qty, 6),
                    item.selected_facility_physical_fbs,
                    item.selected_facility_reserved_fbs,
                    item.selected_facility_available_fbs,
                    round(item.remaining_active_inbound_qty, 6),
                    round(item.coverage_qty, 6),
                    item.sales_history_mode,
                    item.sales_avg_period_days,
                    item.sales_date_from,
                    item.sales_date_to,
                    item.sales_calendar_day_count,
                    item.used_trading_day_count,
                    ",".join(item.included_sales_dates),
                    ",".join(item.excluded_sales_dates),
                    round(item.baseline_daily_sales, 6),
                    round(item.valid_day_threshold, 6),
                    round(item.raw_window_daily_demand, 6),
                    round(item.national_daily_demand, 6),
                    "false",
                    f"{result.target_facility_name} ({result.target_facility_id})",
                    result.inbound_coverage.get("scope_label", ""),
                ]
            )
        rows.extend(
            [
                [],
                ["Общее количество", "", result.summary.total_qty],
                ["Горизонт, дней", "", result.horizon_days],
                ["Остатки WB учитываются", "", "Нет"],
                ["Область спроса", "", NATIONAL_DEMAND_SCOPE],
                [
                    "Охват заказов фабрике",
                    "",
                    result.inbound_coverage.get("scope_label", ""),
                ],
                [
                    "Учтено активных входящих, шт",
                    "",
                    result.inbound_coverage.get("total_quantity", 0),
                ],
            ]
        )
        filename = (
            "sheet-vitrina-v1-fbs-fulfillment-order-"
            f"{result.target_facility_id}-{result.report_date}.xlsx"
        )
        return build_single_sheet_workbook_bytes("Заказ на ФФ", rows), filename


def _parse_settings(payload: Mapping[str, Any]) -> FbsFulfillmentOrderSettings:
    mode = str(payload.get("sales_history_mode") or SALES_HISTORY_MODE_LAST_N_DAYS)
    if mode not in SALES_HISTORY_MODES:
        raise ValueError("Режим истории должен быть last_n_days или custom_period")
    sales_days = (
        _positive_int(
            payload.get("sales_avg_period_days", DEFAULT_SALES_HISTORY_DAYS),
            "Количество последних дней",
        )
        if mode == SALES_HISTORY_MODE_LAST_N_DAYS
        else None
    )
    date_from = _optional_iso_date(payload.get("sales_date_from"), "Дата начала")
    date_to = _optional_iso_date(payload.get("sales_date_to"), "Дата окончания")
    if mode == SALES_HISTORY_MODE_CUSTOM_PERIOD:
        if not date_from or not date_to:
            raise ValueError(
                "Для произвольного периода обязательны дата начала и дата окончания"
            )
        if date_from > date_to:
            raise ValueError("Дата начала периода не может быть позже даты окончания")
    else:
        date_from = None
        date_to = None
    return FbsFulfillmentOrderSettings(
        target_facility_id=str(payload.get("target_facility_id") or "").strip(),
        inbound_scope=_inbound_scope(payload.get("inbound_scope")),
        production_days=_positive_int(
            payload.get("production_days", DEFAULTS["production_days"]),
            "Срок производства",
        ),
        factory_to_target_ff_days=_positive_int(
            payload.get(
                "factory_to_target_ff_days",
                DEFAULTS["factory_to_target_ff_days"],
            ),
            "Срок фабрика → выбранный ФФ",
        ),
        ff_safety_days=_nonnegative_int(
            payload.get("ff_safety_days", DEFAULTS["ff_safety_days"]),
            "Страховой запас ФФ",
        ),
        order_cycle_days=_positive_int(
            payload.get("order_cycle_days", DEFAULTS["order_cycle_days"]),
            "Цикл заказа",
        ),
        order_batch_qty=_positive_int(
            payload.get("order_batch_qty", DEFAULTS["order_batch_qty"]),
            "Производственная партия",
        ),
        sales_history_mode=mode,
        sales_avg_period_days=sales_days,
        sales_date_from=date_from,
        sales_date_to=date_to,
        report_date_override=_optional_iso_date(
            payload.get("report_date_override"),
            "Дата расчёта",
        ),
    )


def _inbound_scope(value: Any) -> str:
    normalized = str(value or INBOUND_SCOPE_SELECTED_FACILITY).strip()
    if normalized not in INBOUND_SCOPES:
        raise ValueError(
            "Охват заказов фабрике должен быть selected_facility или all_active"
        )
    return normalized


def _resolve_sales_window(
    settings: FbsFulfillmentOrderSettings,
    report_date: date,
) -> tuple[date, date]:
    latest_closed = report_date - timedelta(days=1)
    if settings.sales_history_mode == SALES_HISTORY_MODE_LAST_N_DAYS:
        assert settings.sales_avg_period_days is not None
        return (
            latest_closed - timedelta(days=settings.sales_avg_period_days - 1),
            latest_closed,
        )
    date_from = date.fromisoformat(str(settings.sales_date_from))
    date_to = date.fromisoformat(str(settings.sales_date_to))
    if date_to > latest_closed:
        raise ValueError(
            "Дата окончания произвольного периода должна быть раньше даты расчёта; "
            "будущие и незакрытые даты недопустимы"
        )
    return date_from, date_to


def _positive_int(value: Any, label: str) -> int:
    try:
        result = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} должен быть целым числом") from exc
    if result <= 0:
        raise ValueError(f"{label} должен быть больше нуля")
    return result


def _nonnegative_int(value: Any, label: str) -> int:
    try:
        result = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} должен быть целым числом") from exc
    if result < 0:
        raise ValueError(f"{label} не может быть отрицательным")
    return result


def _optional_iso_date(value: Any, label: str) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError as exc:
        raise ValueError(f"{label} должна быть датой YYYY-MM-DD") from exc


def _default_now_factory() -> datetime:
    return datetime.now(timezone.utc)


def _default_timestamp_factory() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
