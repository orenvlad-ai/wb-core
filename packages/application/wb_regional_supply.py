"""Server-owned WB regional supply block for the operator page."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
import math
from typing import Any, Mapping
from uuid import uuid4
import zipfile

from packages.adapters.sales_funnel_history_block import HttpBackedSalesFunnelHistorySource
from packages.adapters.stocks_block import HttpBackedStocksSource
from packages.application.factory_order_sales_history import FactoryOrderAuthoritativeSalesHistory
from packages.application.ff_stock_ledger import resolve_ff_stock_ledger_rows
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sales_funnel_history_block import SalesFunnelHistoryBlock
from packages.application.simple_xlsx import build_single_sheet_workbook_bytes
from packages.application.stocks_block import StocksBlock, build_elektrostal_stock_override
from packages.application.stock_ff_onec_source import build_onec_stock_ff_state, resolve_onec_stock_ff_rows
from packages.application.wb_supply_overlay import (
    apply_stock_ff_overlay,
    build_selected_wb_supply_overlay,
    overlay_to_public_payload,
    parse_selected_wb_supply_ids,
    regional_overlay_quantities,
)
from packages.application.wb_regional_demand import (
    build_result_diagnostics as _build_regional_demand_result_diagnostics,
    estimate_wb_regional_demand as _estimate_wb_regional_demand,
)
from packages.business_time import current_business_date_iso
from packages.contracts.factory_order_supply import (
    DATASET_STOCK_FF,
    STOCK_FF_SOURCE_LEDGER,
    STOCK_FF_SOURCE_MANUAL_EXCEL,
    STOCK_FF_SOURCE_ONEC_FF_STOCK,
    FactoryOrderDatasetState,
    FactoryOrderStockFfOnecState,
    FactoryOrderStockFfRow,
)
from packages.contracts.stocks_block import StocksRequest
from packages.contracts.wb_regional_supply import (
    DISTRICT_FAR_SIBERIA,
    DISTRICT_NORTHWEST,
    DISTRICT_SOUTH_CAUCASUS,
    DISTRICT_URAL,
    DISTRICT_VOLGA,
    WbRegionalSupplyCalculationResult,
    WbRegionalSupplyDistrictResult,
    WbRegionalSupplyDistrictRow,
    WbRegionalSupplySettings,
    WbRegionalSupplyStatus,
    WbRegionalSupplySummary,
)
from packages.contracts.wb_supply_planning_zones import (
    PLANNING_ZONE_CENTRAL_EAST,
    PLANNING_ZONE_CENTRAL_NORTH,
    PLANNING_ZONE_CENTRAL_SOUTH,
    SUPPLY_PLANNING_ZONE_KEYS,
    SUPPLY_PLANNING_ZONE_LABELS_RU,
    SUPPLY_PLANNING_ZONE_SHORT_LABELS_RU,
    SUPPLY_PLANNING_ZONE_TO_STOCK_FIELD,
)


_DISTRICT_SPECS = (
    *(
        (
            key,
            SUPPLY_PLANNING_ZONE_LABELS_RU[key],
            SUPPLY_PLANNING_ZONE_TO_STOCK_FIELD[key],
        )
        for key in SUPPLY_PLANNING_ZONE_KEYS
    ),
)
_DISTRICT_NAME_BY_KEY = {key: name for key, name, _ in _DISTRICT_SPECS}
_DISTRICT_FIELD_BY_KEY = {key: field_name for key, _, field_name in _DISTRICT_SPECS}
_DISTRICT_ORDER_INDEX = {key: index for index, key in enumerate(SUPPLY_PLANNING_ZONE_KEYS)}
_DISTRICT_FILENAME_STEMS = {
    PLANNING_ZONE_CENTRAL_NORTH: "central_north",
    PLANNING_ZONE_CENTRAL_EAST: "central_east",
    PLANNING_ZONE_CENTRAL_SOUTH: "central_south",
    DISTRICT_NORTHWEST: "northwest",
    DISTRICT_VOLGA: "volga",
    DISTRICT_URAL: "ural",
    DISTRICT_SOUTH_CAUCASUS: "south_caucasus",
    DISTRICT_FAR_SIBERIA: "far_siberia",
}
_SHARED_STOCK_LABEL = "Остатки ФФ"
_DISTRICT_FILE_HEADERS = ["nmId", "SKU", "Количество к поставке", "Дефицит"]
_WEIGHT_COEFFICIENT = 0.08593
_VOLUME_DIVISOR = 204.38
_DEFAULT_SALES_AVG_PERIOD_DAYS = 14
_DEFAULT_CYCLE_SUPPLY_DAYS = 7
_DEFAULT_LEAD_TIME_TO_REGION_DAYS = 15
_METHODOLOGY_NOTE = (
    "Расчёт использует общий источник «Остатки ФФ» из этой же вкладки: manual Excel, read-only 1C FF_STOCK "
    "или серверный ledger «Остатки ФФ». "
    "Сервер берёт общий спрос SKU из orderCount, а доли по округам восстанавливает по расширенной методологии: "
    "идеальные дни, частичные наблюдения по округам, похожие SKU, общий профиль и только затем тестовая поставка. "
    "Период усреднения продаж означает запрошенное число качественных дней; проблемная ячейка округа/дня не ломает "
    "наблюдения других округов. Старый резервный способ по текущим остаткам не является нормальным путём. "
    "Поле «Доставка, дней» по каждому округу означает лаг до доступности товара на WB: доставка, приёмка, "
    "разбор и появление в продаже. "
    "Ограниченный stock_ff распределяется по коробам сначала по спасённым штукам, затем по дням покрытия и спросу округа. "
    "Тестовая поставка нужна для сбора будущего сигнала, а не как расчётная доля спроса."
)


class WbRegionalSupplyBlock:
    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        stocks_block: StocksBlock | None = None,
        sales_funnel_history_block: SalesFunnelHistoryBlock | None = None,
        now_factory: callable | None = None,
        timestamp_factory: callable | None = None,
        wb_supply_district_mapping_provider: callable | None = None,
    ) -> None:
        self.runtime = runtime
        self.stocks_block = stocks_block or StocksBlock(HttpBackedStocksSource())
        self.sales_funnel_history_block = sales_funnel_history_block or SalesFunnelHistoryBlock(
            HttpBackedSalesFunnelHistorySource()
        )
        self.now_factory = now_factory or _default_now_factory
        self.timestamp_factory = timestamp_factory or _default_timestamp_factory
        self.wb_supply_district_mapping_provider = wb_supply_district_mapping_provider
        self.sales_history = FactoryOrderAuthoritativeSalesHistory(
            runtime=self.runtime,
            sales_funnel_history_block=self.sales_funnel_history_block,
            now_factory=self.now_factory,
            timestamp_factory=self.timestamp_factory,
        )

    def build_status(self) -> WbRegionalSupplyStatus:
        active_skus = self._load_active_skus()
        shared_datasets = {DATASET_STOCK_FF: self._load_shared_stock_ff_state()}
        raw_last_result = self.runtime.load_wb_regional_supply_result_state()
        legacy_result_available = bool(
            isinstance(raw_last_result, Mapping)
            and str(raw_last_result.get("payload_version") or "") != "v2_planning_zones"
        )
        legacy_snapshot = (
            {
                "source_payload_version": str(raw_last_result.get("payload_version") or "v1_legacy_federal_districts"),
                "calculation_id": str(raw_last_result.get("calculation_id") or ""),
                "calculated_at": str(raw_last_result.get("calculated_at") or ""),
                "report_date": str(raw_last_result.get("report_date") or ""),
                "district_keys": [
                    str(item.get("district_key") or "")
                    for item in list(raw_last_result.get("districts") or [])
                    if isinstance(item, Mapping)
                ],
                "summary": dict(raw_last_result.get("summary") or {}),
            }
            if legacy_result_available and isinstance(raw_last_result, Mapping)
            else None
        )
        last_result = self._load_last_result()
        onec_stock_ff_state = self.build_onec_stock_ff_check()
        stock_ff_source = last_result.stock_ff_source if last_result is not None else STOCK_FF_SOURCE_MANUAL_EXCEL
        return WbRegionalSupplyStatus(
            status=(
                "ready"
                if last_result is not None
                else "recalculation_required"
                if legacy_result_available
                else "idle"
            ),
            active_sku_count=len(active_skus),
            methodology_note=self.sales_history.build_operator_note(_METHODOLOGY_NOTE),
            stock_ff_source=stock_ff_source,
            district_options=_district_options(),
            default_included_district_keys=tuple(SUPPLY_PLANNING_ZONE_KEYS),
            shared_datasets=shared_datasets,
            manual_stock_ff_dataset=shared_datasets[DATASET_STOCK_FF],
            onec_stock_ff_summary=onec_stock_ff_state,
            last_result=last_result,
            planning_zone_options=_district_options(),
            migration_status={
                "legacy_result_available": legacy_result_available,
                "legacy_result_preserved": legacy_result_available,
                "recalculation_required": legacy_result_available,
                "target_payload_version": "v2_planning_zones",
                "legacy_snapshot": legacy_snapshot,
                "migration_strategy": "preserve_and_recalculate_from_authoritative_sources",
                "rollback": "Previous JSON remains in the single-slot state until a successful recalculation replaces it.",
            },
        )

    def calculate(self, settings_input: Mapping[str, Any]) -> WbRegionalSupplyCalculationResult:
        settings = _parse_settings(settings_input)
        active_skus = self._load_active_skus()
        if not active_skus:
            raise ValueError("current registry config_v2 does not contain enabled rows for расчёта")

        shared_state = self._load_shared_stock_ff_state()
        stock_ff_source = settings.stock_ff_source
        if stock_ff_source == STOCK_FF_SOURCE_MANUAL_EXCEL and shared_state.status != "uploaded":
            raise ValueError(
                "Для расчёта по федеральным округам нужен общий загруженный файл: Остатки ФФ"
            )
        shared_datasets = {DATASET_STOCK_FF: shared_state}
        ledger_stock_ff_state: Mapping[str, Any] = {}
        if stock_ff_source == STOCK_FF_SOURCE_ONEC_FF_STOCK:
            stock_ff_rows, onec_stock_ff_state = self._load_onec_stock_ff_rows(require_ready=True)
        elif stock_ff_source == STOCK_FF_SOURCE_LEDGER:
            stock_ff_rows, ledger_stock_ff_state = self._load_ledger_stock_ff_rows(active_skus)
            onec_stock_ff_state = self.build_onec_stock_ff_check()
        else:
            stock_ff_rows = self._load_stock_ff_rows()
            onec_stock_ff_state = self.build_onec_stock_ff_check()
        report_date = settings.report_date_override or current_business_date_iso(self.now_factory())
        report_date_obj = date.fromisoformat(report_date)

        nm_ids = [nm_id for nm_id, _ in active_skus]
        sku_metadata_by_nm = self._load_active_sku_metadata()
        stock_response = self.stocks_block.execute(
            StocksRequest(
                snapshot_type="stocks",
                snapshot_date=report_date,
                nm_ids=nm_ids,
            )
        ).result
        if getattr(stock_response, "kind", "") != "success":
            missing = getattr(stock_response, "missing_nm_ids", [])
            raise ValueError(
                "authoritative district stock coverage incomplete for requested nmIds at report_date "
                f"{report_date}: " + ", ".join(str(item) for item in missing)
            )
        stock_items = {int(item.nm_id): item for item in getattr(stock_response, "items", [])}
        stock_planning_reconciliation = dict(
            getattr(stock_response, "planning_reconciliation", {}) or {}
        )
        stock_warehouse_rows = list(getattr(stock_response, "warehouse_rows", []) or [])
        elektrostal_override = build_elektrostal_stock_override(
            items=list(getattr(stock_response, "items", []) or []),
            warehouse_rows=stock_warehouse_rows,
            enabled=settings.exclude_elektrostal_stock,
        )
        if set(stock_items) != set(nm_ids):
            missing = sorted(set(nm_ids) - set(stock_items))
            raise ValueError(
                "authoritative district stock coverage incomplete for requested nmIds at report_date "
                f"{report_date}: " + ", ".join(str(item) for item in missing)
            )

        selected_wb_supply_ids = settings.selected_wb_supply_ids
        wb_supply_overlay = build_selected_wb_supply_overlay(
            runtime=self.runtime,
            selected_supply_ids=selected_wb_supply_ids,
            active_skus=active_skus,
            warehouse_district_mapping=self._wb_supply_district_mapping(),
        )
        (
            effective_stock_ff_rows,
            wb_stock_ff_diagnostics,
            wb_stock_ff_warnings,
        ) = apply_stock_ff_overlay(
            stock_ff_rows=stock_ff_rows,
            active_skus=active_skus,
            overlay=wb_supply_overlay,
            deduct_selected_supplies=stock_ff_source != STOCK_FF_SOURCE_LEDGER,
        )
        (
            wb_regional_qty_by_nm_district,
            wb_regional_overlay_diagnostics,
            wb_regional_overlay_warnings,
        ) = regional_overlay_quantities(overlay=wb_supply_overlay, planning_zone_mode=True)
        stock_ff_by_nm = {row.nm_id: float(row.stock_ff) for row in effective_stock_ff_rows}
        current_stock_by_nm = {
            nm_id: {
                district_key: float(getattr(stock_items[nm_id], _DISTRICT_FIELD_BY_KEY[district_key], 0.0) or 0.0)
                for district_key in SUPPLY_PLANNING_ZONE_KEYS
            }
            for nm_id in nm_ids
        }
        if settings.exclude_elektrostal_stock:
            for nm_id in nm_ids:
                override_row = dict((elektrostal_override.get("by_nm_id") or {}).get(str(nm_id)) or {})
                excluded_qty = max(float(override_row.get("excluded_elektrostal_stock") or 0.0), 0.0)
                current_stock_by_nm[nm_id][PLANNING_ZONE_CENTRAL_EAST] = max(
                    current_stock_by_nm[nm_id][PLANNING_ZONE_CENTRAL_EAST] - excluded_qty,
                    0.0,
                )
        regional_demand_by_nm = _estimate_wb_regional_demand(
            runtime=self.runtime,
            report_date=report_date_obj,
            nm_ids=nm_ids,
            requested_valid_day_count=settings.sales_avg_period_days,
            district_field_by_key=_DISTRICT_FIELD_BY_KEY,
            district_keys=SUPPLY_PLANNING_ZONE_KEYS,
            current_stock_by_nm=current_stock_by_nm,
            included_district_keys=settings.included_district_keys,
            persistent_zero_current_stock_max_qty=max(float(settings.order_batch_qty - 1), 0.0),
            sku_metadata_by_nm=sku_metadata_by_nm,
            legacy_district_field_by_key={
                "central": "stock_ru_central",
                DISTRICT_NORTHWEST: "stock_ru_northwest",
                DISTRICT_VOLGA: "stock_ru_volga",
                DISTRICT_URAL: "stock_ru_ural",
                DISTRICT_SOUTH_CAUCASUS: "stock_ru_south_caucasus",
                DISTRICT_FAR_SIBERIA: "stock_ru_far_siberia",
            },
        )
        result_diagnostics = _build_regional_demand_result_diagnostics(regional_demand_by_nm)
        district_rows_by_key: dict[str, list[WbRegionalSupplyDistrictRow]] = {
            key: [] for key in SUPPLY_PLANNING_ZONE_KEYS
        }
        seed_candidate_sku_ids: set[int] = set()
        seed_allocated_sku_ids: set[int] = set()
        seed_candidate_sku_district_count = 0
        seed_allocated_sku_district_count = 0
        seed_allocated_qty_total = 0
        seed_unfulfilled_qty_total = 0
        seed_by_nm_id: dict[str, dict[str, Any]] = {}

        for nm_id, sku_comment in active_skus:
            demand_estimate = regional_demand_by_nm[nm_id]
            daily_demand_total = float(demand_estimate.daily_demand_total)
            district_stock_by_key = current_stock_by_nm[nm_id]
            district_daily_demand_by_key = demand_estimate.district_daily_demand_by_key
            full_recommendation_by_key: dict[str, int] = {}
            raw_recommendation_by_key: dict[str, float] = {}
            row_payloads_by_key: dict[str, dict[str, Any]] = {}
            for district_key in SUPPLY_PLANNING_ZONE_KEYS:
                current_stock = district_stock_by_key[district_key]
                district_daily_demand = district_daily_demand_by_key[district_key]
                district_lead_time_days = int(settings.lead_time_to_region_days_by_district[district_key])
                selected_wb_supply_qty = float(
                    wb_regional_qty_by_nm_district.get(nm_id, {}).get(district_key, 0.0)
                )
                projected_stock_on_eta = max(
                    current_stock
                    + selected_wb_supply_qty
                    - (district_daily_demand * district_lead_time_days),
                    0.0,
                )
                target_stock_after_arrival = district_daily_demand * (
                    settings.cycle_supply_days + settings.safety_days
                )
                raw_recommendation = max(target_stock_after_arrival - projected_stock_on_eta, 0.0)
                full_recommendation_qty = (
                    int(math.ceil(raw_recommendation / settings.order_batch_qty) * settings.order_batch_qty)
                    if raw_recommendation > 0
                    else 0
                )
                full_recommendation_by_key[district_key] = full_recommendation_qty
                raw_recommendation_by_key[district_key] = raw_recommendation
                row_payloads_by_key[district_key] = {
                    "nm_id": nm_id,
                    "sku_comment": sku_comment,
                    "current_stock": current_stock,
                    "projected_stock_on_eta": projected_stock_on_eta,
                    "target_stock_after_arrival": target_stock_after_arrival,
                    "raw_recommendation_qty": raw_recommendation,
                    "daily_demand_total": daily_demand_total,
                    "district_daily_demand": district_daily_demand,
                    "lead_time_to_region_days": district_lead_time_days,
                    "full_recommendation_qty": full_recommendation_qty,
                    "selected_wb_supply_qty": selected_wb_supply_qty,
                }

            _rebalance_central_recommendations(
                full_recommendation_by_key=full_recommendation_by_key,
                raw_recommendation_by_key=raw_recommendation_by_key,
                district_daily_demand_by_key=district_daily_demand_by_key,
                included_district_keys=settings.included_district_keys,
                order_batch_qty=settings.order_batch_qty,
            )

            demand_allocated_by_key = _allocate_boxes(
                full_recommendation_by_key=full_recommendation_by_key,
                raw_recommendation_by_key=raw_recommendation_by_key,
                district_daily_demand_by_key=district_daily_demand_by_key,
                projected_stock_by_key={
                    district_key: float(row_payloads_by_key[district_key]["projected_stock_on_eta"])
                    for district_key in SUPPLY_PLANNING_ZONE_KEYS
                },
                available_stock_ff=float(stock_ff_by_nm.get(nm_id, 0.0)),
                order_batch_qty=settings.order_batch_qty,
            )
            seed_recommendation_by_key = _seed_floor_recommendation_by_key(
                demand_diagnostics=demand_estimate.diagnostics,
                district_stock_by_key=district_stock_by_key,
                district_daily_demand_by_key=district_daily_demand_by_key,
                daily_demand_total=daily_demand_total,
                included_district_keys=settings.included_district_keys,
                order_batch_qty=settings.order_batch_qty,
            )
            seed_allocated_by_key, seed_unfulfilled_by_key = _allocate_seed_boxes(
                seed_recommendation_by_key=seed_recommendation_by_key,
                available_stock_ff=max(
                    float(stock_ff_by_nm.get(nm_id, 0.0)) - sum(demand_allocated_by_key.values()),
                    0.0,
                ),
                order_batch_qty=settings.order_batch_qty,
            )
            seed_candidate_keys = [
                key for key in SUPPLY_PLANNING_ZONE_KEYS if int(seed_recommendation_by_key.get(key, 0)) > 0
            ]
            seed_allocated_keys = [
                key for key in SUPPLY_PLANNING_ZONE_KEYS if int(seed_allocated_by_key.get(key, 0)) > 0
            ]
            seed_unfulfilled_keys = [
                key for key in SUPPLY_PLANNING_ZONE_KEYS if int(seed_unfulfilled_by_key.get(key, 0)) > 0
            ]
            if seed_candidate_keys:
                seed_candidate_sku_ids.add(int(nm_id))
                seed_candidate_sku_district_count += len(seed_candidate_keys)
            if seed_allocated_keys:
                seed_allocated_sku_ids.add(int(nm_id))
                seed_allocated_sku_district_count += len(seed_allocated_keys)
                seed_allocated_qty_total += sum(int(seed_allocated_by_key.get(key, 0)) for key in seed_allocated_keys)
            if seed_unfulfilled_keys:
                seed_unfulfilled_qty_total += sum(
                    int(seed_unfulfilled_by_key.get(key, 0)) for key in seed_unfulfilled_keys
                )
            if seed_candidate_keys:
                seed_by_nm_id[str(nm_id)] = {
                    "nm_id": int(nm_id),
                    "seed_district_keys": seed_candidate_keys,
                    "seed_allocated_district_keys": seed_allocated_keys,
                    "seed_unfulfilled_district_keys": seed_unfulfilled_keys,
                    "seed_qty_by_district": {
                        key: int(seed_allocated_by_key.get(key, 0))
                        for key in seed_candidate_keys
                    },
                    "seed_unfulfilled_qty_by_district": {
                        key: int(seed_unfulfilled_by_key.get(key, 0))
                        for key in seed_candidate_keys
                    },
                    "seed_reason_by_district": {
                        key: str(
                            dict(demand_estimate.diagnostics.get("seed_reason_by_district") or {}).get(
                                key,
                                "seed_floor",
                            )
                        )
                        for key in seed_candidate_keys
                    },
                    "seed_note": "Это тестовая поставка для сбора будущего сигнала, а не расчётная доля спроса.",
                }
            for district_key in SUPPLY_PLANNING_ZONE_KEYS:
                demand_allocated_qty = int(demand_allocated_by_key.get(district_key, 0))
                seed_qty = int(seed_allocated_by_key.get(district_key, 0))
                seed_unfulfilled_qty = int(seed_unfulfilled_by_key.get(district_key, 0))
                demand_recommendation_qty = int(full_recommendation_by_key.get(district_key, 0))
                seed_recommendation_qty = int(seed_recommendation_by_key.get(district_key, 0))
                full_recommendation_qty = demand_recommendation_qty + seed_recommendation_qty
                allocated_qty = demand_allocated_qty + seed_qty
                row_diagnostics = dict(demand_estimate.diagnostics)
                share_sources = dict(demand_estimate.diagnostics.get("district_share_sources") or {})
                share_confidences = dict(demand_estimate.diagnostics.get("confidence_by_district") or {})
                share_source = str(share_sources.get(district_key) or "")
                try:
                    share_confidence = float(share_confidences.get(district_key, 0.0) or 0.0)
                except (TypeError, ValueError):
                    share_confidence = 0.0
                if seed_recommendation_qty > 0:
                    row_diagnostics["seed_district_keys"] = seed_candidate_keys
                    row_diagnostics["seed_qty_by_district"] = {
                        key: int(seed_allocated_by_key.get(key, 0))
                        for key in seed_candidate_keys
                    }
                    row_diagnostics["seed_unfulfilled_qty_by_district"] = {
                        key: int(seed_unfulfilled_by_key.get(key, 0))
                        for key in seed_candidate_keys
                    }
                    row_diagnostics["seed_reason_by_district"] = {
                        key: str(
                            dict(demand_estimate.diagnostics.get("seed_reason_by_district") or {}).get(
                                key,
                                "seed_floor",
                            )
                        )
                        for key in seed_candidate_keys
                    }
                row_diagnostics["share_source"] = share_source
                row_diagnostics["share_confidence"] = share_confidence
                row_diagnostics["demand_recommendation_qty"] = demand_recommendation_qty
                row_diagnostics["lead_time_to_region_days"] = int(
                    row_payloads_by_key[district_key]["lead_time_to_region_days"]
                )
                row_diagnostics["seed_qty"] = seed_qty
                row_diagnostics["selected_wb_supply_qty"] = float(
                    row_payloads_by_key[district_key].get("selected_wb_supply_qty", 0.0)
                )
                allocation_reason = "demand_based"
                if seed_qty > 0:
                    allocation_reason = "demand_based_plus_seed_floor" if demand_allocated_qty > 0 else "seed_floor"
                elif seed_recommendation_qty > 0:
                    allocation_reason = "seed_floor_unfulfilled"
                row_diagnostics["allocation_reason"] = allocation_reason
                district_rows_by_key[district_key].append(
                    WbRegionalSupplyDistrictRow(
                        nm_id=nm_id,
                        sku_comment=sku_comment,
                        full_recommendation_qty=full_recommendation_qty,
                        allocated_qty=allocated_qty,
                        deficit_qty=max(full_recommendation_qty - allocated_qty, 0),
                        current_stock=float(row_payloads_by_key[district_key]["current_stock"]),
                        projected_stock_on_eta=float(
                            row_payloads_by_key[district_key]["projected_stock_on_eta"]
                        ),
                        target_stock_after_arrival=float(
                            row_payloads_by_key[district_key]["target_stock_after_arrival"]
                        ),
                        daily_demand_total=float(row_payloads_by_key[district_key]["daily_demand_total"]),
                        district_daily_demand=float(
                            row_payloads_by_key[district_key]["district_daily_demand"]
                        ),
                        lead_time_to_region_days=int(
                            row_payloads_by_key[district_key]["lead_time_to_region_days"]
                        ),
                        raw_recommendation_qty=float(
                            row_payloads_by_key[district_key]["raw_recommendation_qty"]
                        ),
                        demand_diagnostics=row_diagnostics,
                        demand_recommendation_qty=demand_recommendation_qty,
                        demand_allocated_qty=demand_allocated_qty,
                        seed_recommendation_qty=seed_recommendation_qty,
                        seed_qty=seed_qty,
                        seed_unfulfilled_qty=seed_unfulfilled_qty,
                        allocation_reason=allocation_reason,
                        persistent_zero_seed_applied=seed_qty > 0,
                        seed_floor_applied=seed_qty > 0,
                        share_source=share_source,
                        share_confidence=share_confidence,
                        in_transit_qty=float(
                            row_payloads_by_key[district_key].get("selected_wb_supply_qty", 0.0)
                        ),
                    )
                )

        result_diagnostics = dict(result_diagnostics)
        result_diagnostics.update(
            {
                "seed_allocation_enabled": True,
                "seed_candidate_sku_count": len(seed_candidate_sku_ids),
                "seed_candidate_sku_district_count": seed_candidate_sku_district_count,
                "seed_sku_count": len(seed_allocated_sku_ids),
                "seed_sku_district_count": seed_allocated_sku_district_count,
                "seed_allocated_qty_total": int(seed_allocated_qty_total),
                "seed_unfulfilled_qty_total": int(seed_unfulfilled_qty_total),
                "seed_by_nm_id": seed_by_nm_id,
                "lead_time_to_region_days": int(settings.lead_time_to_region_days),
                "lead_time_to_region_days_by_district": {
                    key: int(settings.lead_time_to_region_days_by_district[key])
                    for key in SUPPLY_PLANNING_ZONE_KEYS
                },
                "stock_ff_source_state": dict(ledger_stock_ff_state) if stock_ff_source == STOCK_FF_SOURCE_LEDGER else {},
                "wb_supply_overlay": wb_regional_overlay_diagnostics,
                "central_stock_reconciliation": stock_planning_reconciliation,
                "elektrostal_stock_override": elektrostal_override,
                "stock_warehouse_row_count": len(stock_warehouse_rows),
            }
        )
        warnings = [str(item) for item in result_diagnostics.get("warnings", []) if item]
        warnings.extend(str(item) for item in ledger_stock_ff_state.get("warnings", []) if str(item or "").strip())
        warnings.extend(str(item) for item in wb_supply_overlay.get("warnings", []) if str(item or "").strip())
        warnings.extend(str(item) for item in wb_stock_ff_warnings if item)
        warnings.extend(str(item) for item in wb_regional_overlay_warnings if item)
        if seed_unfulfilled_qty_total > 0:
            warnings.append(
                "Не хватило stock_ff для всех тестовых коробок seed floor: "
                f"{int(seed_unfulfilled_qty_total)} шт."
            )
        result_diagnostics["warnings"] = warnings
        result_warnings = tuple(warnings)
        wb_supply_overlay_payload = overlay_to_public_payload(
            overlay=wb_supply_overlay,
            stock_ff_diagnostics=wb_stock_ff_diagnostics,
            wb_regional_diagnostics=wb_regional_overlay_diagnostics,
            extra_warnings=tuple(wb_stock_ff_warnings) + tuple(wb_regional_overlay_warnings),
        )

        districts = [
            WbRegionalSupplyDistrictResult(
                district_key=district_key,
                district_name_ru=_DISTRICT_NAME_BY_KEY[district_key],
                total_qty=sum(row.allocated_qty for row in district_rows_by_key[district_key]),
                deficit_qty=sum(row.deficit_qty for row in district_rows_by_key[district_key]),
                filename=_district_filename(district_key),
                rows=district_rows_by_key[district_key],
                lead_time_to_region_days=int(settings.lead_time_to_region_days_by_district[district_key]),
                planning_zone_key=district_key,
                planning_zone_label=_DISTRICT_NAME_BY_KEY[district_key],
            )
            for district_key in settings.included_district_keys
        ]
        total_qty = sum(item.total_qty for item in districts)
        result = WbRegionalSupplyCalculationResult(
            status="success",
            calculation_id=uuid4().hex,
            calculated_at=self.timestamp_factory(),
            report_date=report_date,
            horizon_days=settings.cycle_supply_days,
            active_sku_count=len(active_skus),
            methodology_note=self.sales_history.build_operator_note(_METHODOLOGY_NOTE),
            settings=settings,
            stock_ff_source=stock_ff_source,
            shared_datasets=shared_datasets,
            manual_stock_ff_dataset=shared_datasets[DATASET_STOCK_FF],
            onec_stock_ff_summary=onec_stock_ff_state,
            summary=WbRegionalSupplySummary(
                total_qty=total_qty,
                estimated_weight=round(total_qty * _WEIGHT_COEFFICIENT, 2),
                estimated_volume=round((total_qty * _WEIGHT_COEFFICIENT) / _VOLUME_DIVISOR, 2),
            ),
            districts=districts,
            diagnostics=result_diagnostics,
            wb_supply_overlay=wb_supply_overlay_payload,
            warnings=result_warnings,
        )
        self._validate_result_consistency(result)
        for district in result.districts:
            self._build_district_workbook_bytes(district)
        self.runtime.save_wb_regional_supply_result_state(
            calculated_at=result.calculated_at,
            payload=asdict(result),
        )
        return result

    def download_district_recommendation(self, district_key: str) -> tuple[bytes, str]:
        normalized_key = str(district_key or "").strip().lower()
        if normalized_key not in _DISTRICT_NAME_BY_KEY:
            raise ValueError(f"Неизвестный федеральный округ: {district_key}")
        result = self._load_last_result()
        if result is None:
            raise ValueError("Результат расчёта по федеральным округам ещё не подготовлен")
        included_keys = set(result.settings.included_district_keys or SUPPLY_PLANNING_ZONE_KEYS)
        if normalized_key not in included_keys:
            raise ValueError(f"Округ не участвовал в последнем расчёте: {normalized_key}")
        district = next((item for item in result.districts if item.district_key == normalized_key), None)
        if district is None:
            raise ValueError(f"В последнем результате нет округа: {district_key}")
        return self._build_district_workbook_bytes(district), district.filename

    def download_all_recommendations_archive(self) -> tuple[bytes, str]:
        result = self._load_last_result()
        if result is None:
            raise ValueError("Результат расчёта по федеральным округам ещё не подготовлен")
        included_keys = tuple(result.settings.included_district_keys or SUPPLY_PLANNING_ZONE_KEYS)
        districts_by_key = {item.district_key: item for item in result.districts}
        included_districts = [
            districts_by_key[key]
            for key in included_keys
            if key in districts_by_key
        ]
        if not included_districts:
            raise ValueError("В последнем результате нет рекомендаций для выбранных округов")

        archive_buffer = BytesIO()
        with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for district in included_districts:
                archive.writestr(district.filename, self._build_district_workbook_bytes(district))
        report_date = _safe_report_date_for_filename(result.report_date)
        return archive_buffer.getvalue(), f"wb_regional_recommendations_{report_date}.zip"

    def _load_active_skus(self) -> list[tuple[int, str]]:
        current_state = self.runtime.load_current_state()
        enabled = sorted(
            [item for item in current_state.config_v2 if item.enabled],
            key=lambda item: item.display_order,
        )
        return [(int(item.nm_id), str(item.display_name)) for item in enabled]

    def _wb_supply_district_mapping(self) -> Mapping[str, Any] | None:
        provider = self.wb_supply_district_mapping_provider
        if not callable(provider):
            return None
        return provider()

    def _load_active_sku_metadata(self) -> dict[int, dict[str, Any]]:
        current_state = self.runtime.load_current_state()
        return {
            int(item.nm_id): {
                "display_name": str(item.display_name),
                "group": str(getattr(item, "group", "") or ""),
            }
            for item in current_state.config_v2
            if item.enabled
        }

    def _load_shared_stock_ff_state(self) -> FactoryOrderDatasetState:
        payload = self.runtime.load_factory_order_dataset_state(DATASET_STOCK_FF)
        if payload is None:
            return FactoryOrderDatasetState(
                dataset_type=DATASET_STOCK_FF,
                label_ru=_SHARED_STOCK_LABEL,
                status="missing",
                uploaded_at=None,
                row_count=0,
                required=True,
            )
        return FactoryOrderDatasetState(
            dataset_type=DATASET_STOCK_FF,
            label_ru=_SHARED_STOCK_LABEL,
            status="uploaded",
            uploaded_at=str(payload["uploaded_at"]),
            row_count=int(payload["row_count"]),
            required=True,
            uploaded_filename=str(payload.get("uploaded_filename") or "") or None,
            file_available=bool(payload.get("file_available")),
        )

    def _load_stock_ff_rows(self) -> list[FactoryOrderStockFfRow]:
        payload = self.runtime.load_factory_order_dataset_state(DATASET_STOCK_FF)
        if payload is None:
            return []
        return [
            FactoryOrderStockFfRow(
                nm_id=int(item["nm_id"]),
                sku_comment=str(item.get("sku_comment", "") or ""),
                stock_ff=float(item["stock_ff"]),
                snapshot_date=str(item.get("snapshot_date") or "") or None,
                comment=str(item.get("comment", "") or ""),
            )
            for item in payload["rows"]
        ]

    def build_onec_stock_ff_check(self) -> FactoryOrderStockFfOnecState:
        return build_onec_stock_ff_state(runtime=self.runtime, active_skus=self._load_active_skus())

    def _load_onec_stock_ff_rows(
        self,
        *,
        require_ready: bool,
    ) -> tuple[list[FactoryOrderStockFfRow], FactoryOrderStockFfOnecState]:
        result = resolve_onec_stock_ff_rows(runtime=self.runtime, active_skus=self._load_active_skus())
        if require_ready and result.state.status != "ready":
            details = list(result.state.errors) + list(result.state.warnings)
            if not details:
                details = [f"status={result.state.status}"]
            raise ValueError(
                "Источник «1С / Фулфилмент» для «Остатки ФФ» не готов: "
                + "; ".join(details)
            )
        return result.rows, result.state

    def _load_ledger_stock_ff_rows(
        self,
        active_skus: list[tuple[int, str]],
    ) -> tuple[list[FactoryOrderStockFfRow], Mapping[str, Any]]:
        return resolve_ff_stock_ledger_rows(runtime=self.runtime, active_skus=active_skus)

    def _load_last_result(self) -> WbRegionalSupplyCalculationResult | None:
        payload = self.runtime.load_wb_regional_supply_result_state()
        if not isinstance(payload, Mapping):
            return None
        if str(payload.get("payload_version") or "") != "v2_planning_zones":
            return None
        settings_payload = payload.get("settings") or {}
        summary_payload = payload.get("summary") or {}
        shared_datasets_payload = payload.get("shared_datasets") or {}
        districts_payload = payload.get("districts") or []
        diagnostics_payload = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), Mapping) else None
        warnings_payload = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
        stock_ff_source = _normalize_stock_ff_source(
            payload.get("stock_ff_source", settings_payload.get("stock_ff_source"))
        )
        settings_lead_time_to_region_days = _coerce_positive_int(
            settings_payload.get("lead_time_to_region_days"),
            _DEFAULT_LEAD_TIME_TO_REGION_DAYS,
        )
        settings_lead_time_to_region_days_by_district = _coerce_lead_time_map_from_saved_settings(
            settings_payload,
            settings_lead_time_to_region_days,
        )
        result_settings = WbRegionalSupplySettings(
            sales_avg_period_days=int(
                settings_payload.get("sales_avg_period_days", _DEFAULT_SALES_AVG_PERIOD_DAYS)
            ),
            cycle_supply_days=int(
                settings_payload.get(
                    "cycle_supply_days",
                    settings_payload.get("supply_horizon_days", _DEFAULT_CYCLE_SUPPLY_DAYS),
                )
            ),
            lead_time_to_region_days=settings_lead_time_to_region_days,
            lead_time_to_region_days_by_district=settings_lead_time_to_region_days_by_district,
            safety_days=int(settings_payload.get("safety_days", 0)),
            order_batch_qty=int(settings_payload.get("order_batch_qty", 0)),
            report_date_override=(
                str(settings_payload.get("report_date_override"))
                if settings_payload.get("report_date_override")
                else None
            ),
            stock_ff_source=stock_ff_source,
            included_district_keys=_parse_included_district_keys(settings_payload.get("included_district_keys")),
            selected_wb_supply_ids=_parse_selected_wb_supply_ids_from_settings(settings_payload),
            exclude_elektrostal_stock=_parse_bool(settings_payload.get("exclude_elektrostal_stock")),
        )
        shared_datasets = {
            key: FactoryOrderDatasetState(
                dataset_type=str(value.get("dataset_type", key)),
                label_ru=str(value.get("label_ru", _SHARED_STOCK_LABEL)),
                status=str(value.get("status", "missing")),
                uploaded_at=str(value.get("uploaded_at")) if value.get("uploaded_at") else None,
                row_count=int(value.get("row_count", 0)),
                required=bool(value.get("required", True)),
                uploaded_filename=str(value.get("uploaded_filename")) if value.get("uploaded_filename") else None,
                file_available=bool(value.get("file_available", False)),
            )
            for key, value in shared_datasets_payload.items()
            if isinstance(value, Mapping)
        }
        manual_stock_ff_dataset = shared_datasets.get(DATASET_STOCK_FF) or self._load_shared_stock_ff_state()
        return WbRegionalSupplyCalculationResult(
            status=str(payload.get("status", "")),
            calculation_id=str(payload.get("calculation_id", "")),
            calculated_at=str(payload.get("calculated_at", "")),
            report_date=str(payload.get("report_date", "")),
            horizon_days=int(payload.get("horizon_days", 0)),
            active_sku_count=int(payload.get("active_sku_count", 0)),
            methodology_note=str(payload.get("methodology_note", _METHODOLOGY_NOTE)),
            settings=result_settings,
            stock_ff_source=stock_ff_source,
            shared_datasets=shared_datasets,
            manual_stock_ff_dataset=manual_stock_ff_dataset,
            onec_stock_ff_summary=_parse_onec_stock_ff_state(payload.get("onec_stock_ff_summary")),
            summary=WbRegionalSupplySummary(
                total_qty=int(summary_payload.get("total_qty", 0)),
                estimated_weight=float(summary_payload.get("estimated_weight", 0.0)),
                estimated_volume=float(summary_payload.get("estimated_volume", 0.0)),
            ),
            districts=[
                WbRegionalSupplyDistrictResult(
                    district_key=str(item.get("district_key", "")),
                    district_name_ru=str(
                        item.get("district_name_ru", _DISTRICT_NAME_BY_KEY.get(str(item.get("district_key", "")), ""))
                    ),
                    total_qty=int(item.get("total_qty", 0)),
                    deficit_qty=int(item.get("deficit_qty", 0)),
                    filename=_district_filename(str(item.get("district_key", ""))),
                    rows=[
                        WbRegionalSupplyDistrictRow(
                            nm_id=int(row.get("nm_id", 0)),
                            sku_comment=str(row.get("sku_comment", "")),
                            full_recommendation_qty=int(row.get("full_recommendation_qty", 0)),
                            allocated_qty=int(row.get("allocated_qty", 0)),
                            deficit_qty=int(row.get("deficit_qty", 0)),
                            current_stock=float(row.get("current_stock", 0.0)),
                            projected_stock_on_eta=float(row.get("projected_stock_on_eta", 0.0)),
                            target_stock_after_arrival=float(row.get("target_stock_after_arrival", 0.0)),
                            daily_demand_total=float(row.get("daily_demand_total", 0.0)),
                            district_daily_demand=float(row.get("district_daily_demand", 0.0)),
                            lead_time_to_region_days=_coerce_positive_int(
                                row.get("lead_time_to_region_days"),
                                settings_lead_time_to_region_days_by_district.get(
                                    str(item.get("district_key", "") or "").strip().lower(),
                                    settings_lead_time_to_region_days,
                                ),
                            ),
                            raw_recommendation_qty=float(row.get("raw_recommendation_qty", 0.0)),
                            demand_diagnostics=(
                                dict(row.get("demand_diagnostics"))
                                if isinstance(row.get("demand_diagnostics"), Mapping)
                                else None
                            ),
                            demand_recommendation_qty=int(
                                row.get("demand_recommendation_qty", row.get("full_recommendation_qty", 0))
                            ),
                            demand_allocated_qty=int(
                                row.get("demand_allocated_qty", row.get("allocated_qty", 0))
                            ),
                            seed_recommendation_qty=int(row.get("seed_recommendation_qty", 0)),
                            seed_qty=int(row.get("seed_qty", 0)),
                            seed_unfulfilled_qty=int(row.get("seed_unfulfilled_qty", 0)),
                            allocation_reason=str(row.get("allocation_reason", "") or ""),
                            persistent_zero_seed_applied=bool(row.get("persistent_zero_seed_applied", False)),
                            seed_floor_applied=bool(row.get("seed_floor_applied", row.get("persistent_zero_seed_applied", False))),
                            share_source=str(row.get("share_source", "") or ""),
                            share_confidence=float(row.get("share_confidence", 0.0) or 0.0),
                            in_transit_qty=float(
                                row.get(
                                    "in_transit_qty",
                                    dict(row.get("demand_diagnostics") or {}).get(
                                        "selected_wb_supply_qty", 0.0
                                    ) if isinstance(row.get("demand_diagnostics"), Mapping) else 0.0,
                                )
                                or 0.0
                            ),
                        )
                        for row in item.get("rows", [])
                        if isinstance(row, Mapping)
                    ],
                    lead_time_to_region_days=settings_lead_time_to_region_days_by_district.get(
                        str(item.get("district_key", "") or "").strip().lower(),
                        settings_lead_time_to_region_days,
                    ),
                    planning_zone_key=str(item.get("planning_zone_key") or item.get("district_key") or ""),
                    planning_zone_label=str(
                        item.get("planning_zone_label")
                        or item.get("district_name_ru")
                        or _DISTRICT_NAME_BY_KEY.get(str(item.get("district_key") or ""), "")
                    ),
                )
                for item in districts_payload
                if isinstance(item, Mapping)
            ],
            diagnostics=dict(diagnostics_payload) if diagnostics_payload is not None else None,
            wb_supply_overlay=(
                dict(payload.get("wb_supply_overlay"))
                if isinstance(payload.get("wb_supply_overlay"), Mapping)
                else None
            ),
            warnings=tuple(str(item) for item in warnings_payload if item),
        )

    def _validate_result_consistency(self, result: WbRegionalSupplyCalculationResult) -> None:
        total_from_districts = 0
        for district in result.districts:
            if district.district_key not in _DISTRICT_NAME_BY_KEY:
                raise ValueError(f"district result contains unsupported key: {district.district_key}")
            district_total = sum(row.allocated_qty for row in district.rows)
            district_deficit = sum(row.deficit_qty for row in district.rows)
            if district_total != district.total_qty:
                raise ValueError(
                    f"district summary mismatch for {district.district_key}: total_qty={district.total_qty}, rows={district_total}"
                )
            if district_deficit != district.deficit_qty:
                raise ValueError(
                    f"district summary mismatch for {district.district_key}: deficit_qty={district.deficit_qty}, rows={district_deficit}"
                )
            for row in district.rows:
                if row.allocated_qty > row.full_recommendation_qty:
                    raise ValueError(
                        f"allocated_qty exceeds full_recommendation_qty for {district.district_key} nmId={row.nm_id}"
                    )
                if row.deficit_qty != max(row.full_recommendation_qty - row.allocated_qty, 0):
                    raise ValueError(
                        f"deficit_qty mismatch for {district.district_key} nmId={row.nm_id}"
                    )
            total_from_districts += district_total
        if total_from_districts != result.summary.total_qty:
            raise ValueError(
                f"summary total mismatch: summary.total_qty={result.summary.total_qty}, districts={total_from_districts}"
            )

    def _build_district_workbook_bytes(self, district: WbRegionalSupplyDistrictResult) -> bytes:
        rows: list[list[Any]] = [
            ["Федеральный округ", district.district_name_ru, "", ""],
            [],
            _DISTRICT_FILE_HEADERS,
        ]
        rows.extend(
            [
                [row.nm_id, row.sku_comment, row.allocated_qty, row.deficit_qty]
                for row in district.rows
                if row.allocated_qty > 0 or row.deficit_qty > 0
            ]
        )
        sheet_name = _truncate_sheet_name(district.district_name_ru)
        return build_single_sheet_workbook_bytes(sheet_name, rows)


def _default_now_factory() -> datetime:
    return datetime.now(timezone.utc)


def _default_timestamp_factory() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_settings(payload: Mapping[str, Any]) -> WbRegionalSupplySettings:
    lead_time_to_region_days = _parse_positive_int_with_default(
        payload.get("lead_time_to_region_days"),
        "Срок доставки до склада Wildberries",
        _DEFAULT_LEAD_TIME_TO_REGION_DAYS,
    )
    lead_time_to_region_days_by_district = _parse_lead_time_to_region_days_by_district(
        payload,
        lead_time_to_region_days,
    )
    return WbRegionalSupplySettings(
        sales_avg_period_days=_parse_sales_avg_period_days(payload.get("sales_avg_period_days")),
        cycle_supply_days=_parse_cycle_supply_days(payload),
        lead_time_to_region_days=lead_time_to_region_days,
        lead_time_to_region_days_by_district=lead_time_to_region_days_by_district,
        safety_days=_parse_nonnegative_int(payload.get("safety_days"), "Страховой запас"),
        order_batch_qty=_parse_positive_int(payload.get("order_batch_qty"), "Кратность штук в коробке"),
        report_date_override=_parse_optional_date(
            payload.get("report_date_override"),
            field_label="Дата расчёта",
        ),
        stock_ff_source=_parse_stock_ff_source(payload.get("stock_ff_source")),
        included_district_keys=_parse_included_district_keys(payload.get("included_district_keys")),
        selected_wb_supply_ids=parse_selected_wb_supply_ids(payload),
        exclude_elektrostal_stock=_parse_bool(payload.get("exclude_elektrostal_stock")),
    )


def _parse_stock_ff_source(value: Any) -> str:
    normalized = str(value or "").strip() or STOCK_FF_SOURCE_MANUAL_EXCEL
    if normalized not in {STOCK_FF_SOURCE_MANUAL_EXCEL, STOCK_FF_SOURCE_ONEC_FF_STOCK, STOCK_FF_SOURCE_LEDGER}:
        raise ValueError("Источник остатков ФФ должен быть manual_excel, onec_ff_stock или ff_stock_ledger")
    return normalized


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "да"}


def _normalize_stock_ff_source(value: Any) -> str:
    try:
        return _parse_stock_ff_source(value)
    except ValueError:
        return STOCK_FF_SOURCE_MANUAL_EXCEL


def _parse_selected_wb_supply_ids_from_settings(payload: Mapping[str, Any]) -> tuple[str, ...]:
    if not isinstance(payload, Mapping):
        return ()
    return parse_selected_wb_supply_ids(payload)


def _parse_included_district_keys(value: Any) -> tuple[str, ...]:
    if value in ("", None):
        return tuple(SUPPLY_PLANNING_ZONE_KEYS)
    if isinstance(value, str):
        raw_values = [item.strip() for item in value.split(",")]
    elif isinstance(value, (list, tuple)):
        raw_values = [str(item or "").strip() for item in value]
    else:
        raise ValueError("included_district_keys должен быть списком федеральных округов")
    requested = [item.lower() for item in raw_values if item]
    if not requested:
        raise ValueError("Выберите хотя бы один округ для расчёта пропорций")
    unknown = sorted({item for item in requested if item not in SUPPLY_PLANNING_ZONE_KEYS})
    if unknown:
        raise ValueError("Неизвестный федеральный округ: " + ", ".join(unknown))
    requested_set = set(requested)
    included = tuple(key for key in SUPPLY_PLANNING_ZONE_KEYS if key in requested_set)
    if not included:
        raise ValueError("Выберите хотя бы один округ для расчёта пропорций")
    return included


def _district_options() -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "district_key": key,
            "district_name_ru": _DISTRICT_NAME_BY_KEY[key],
            "district_short_label_ru": SUPPLY_PLANNING_ZONE_SHORT_LABELS_RU[key],
            "planning_zone_key": key,
            "planning_zone_label": _DISTRICT_NAME_BY_KEY[key],
        }
        for key in SUPPLY_PLANNING_ZONE_KEYS
    )


def _parse_lead_time_to_region_days_by_district(
    payload: Mapping[str, Any],
    scalar_fallback_days: int,
) -> dict[str, int]:
    raw_map = payload.get("lead_time_to_region_days_by_district")
    if raw_map in ("", None):
        raw_map = payload.get("district_lead_time_days")
    if raw_map in ("", None):
        return {key: int(scalar_fallback_days) for key in SUPPLY_PLANNING_ZONE_KEYS}
    if not isinstance(raw_map, Mapping):
        raise ValueError("Доставка по федеральным округам должна быть объектом district_key -> days")
    requested_keys = {str(key or "").strip().lower() for key in raw_map.keys()}
    unknown = sorted(key for key in requested_keys if key not in SUPPLY_PLANNING_ZONE_KEYS)
    if unknown:
        raise ValueError("Неизвестный федеральный округ в сроках доставки: " + ", ".join(unknown))
    missing = [key for key in SUPPLY_PLANNING_ZONE_KEYS if key not in requested_keys]
    if missing:
        raise ValueError("Не задан срок доставки для федерального округа: " + ", ".join(missing))
    normalized_payload = {str(key or "").strip().lower(): value for key, value in raw_map.items()}
    return {
        key: _parse_positive_int(
            normalized_payload.get(key),
            f"Доставка до WB для направления {SUPPLY_PLANNING_ZONE_SHORT_LABELS_RU.get(key, key)}",
        )
        for key in SUPPLY_PLANNING_ZONE_KEYS
    }


def _coerce_lead_time_map_from_saved_settings(
    settings_payload: Mapping[str, Any],
    scalar_fallback_days: int,
) -> dict[str, int]:
    raw_map = settings_payload.get("lead_time_to_region_days_by_district")
    if raw_map in ("", None):
        raw_map = settings_payload.get("district_lead_time_days")
    if not isinstance(raw_map, Mapping):
        return {key: int(scalar_fallback_days) for key in SUPPLY_PLANNING_ZONE_KEYS}
    normalized_payload = {str(key or "").strip().lower(): value for key, value in raw_map.items()}
    return {
        key: _coerce_positive_int(normalized_payload.get(key), scalar_fallback_days)
        for key in SUPPLY_PLANNING_ZONE_KEYS
    }


def _parse_positive_int_with_default(value: Any, label: str, default: int) -> int:
    if value in ("", None):
        return int(default)
    return _parse_positive_int(value, label)


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        numeric = int(str(value).strip())
    except Exception:
        return int(default)
    if numeric <= 0:
        return int(default)
    return numeric


def _parse_onec_stock_ff_state(value: Any) -> FactoryOrderStockFfOnecState:
    if not isinstance(value, Mapping):
        return _empty_onec_stock_ff_state()
    sample_rows = []
    for item in value.get("sample_rows") or []:
        if isinstance(item, Mapping):
            sample_rows.append(dict(item))
    return FactoryOrderStockFfOnecState(
        status=str(value.get("status", "missing") or "missing"),
        source=STOCK_FF_SOURCE_ONEC_FF_STOCK,
        source_label_ru=str(value.get("source_label_ru", "1С / Фулфилмент") or "1С / Фулфилмент"),
        snapshot_date=str(value.get("snapshot_date", "") or ""),
        active_sku_count=int(value.get("active_sku_count", 0)),
        covered_sku_count=int(value.get("covered_sku_count", 0)),
        positive_stock_sku_count=int(value.get("positive_stock_sku_count", 0)),
        zero_stock_sku_count=int(value.get("zero_stock_sku_count", 0)),
        missing_sku_count=int(value.get("missing_sku_count", 0)),
        total_stock_ff=float(value.get("total_stock_ff", 0.0)),
        warnings=tuple(str(item) for item in value.get("warnings", []) if str(item or "").strip()),
        errors=tuple(str(item) for item in value.get("errors", []) if str(item or "").strip()),
        sample_rows=tuple(sample_rows),
    )


def _empty_onec_stock_ff_state() -> FactoryOrderStockFfOnecState:
    return FactoryOrderStockFfOnecState(
        status="missing",
        source=STOCK_FF_SOURCE_ONEC_FF_STOCK,
        source_label_ru="1С / Фулфилмент",
        snapshot_date="",
        active_sku_count=0,
        covered_sku_count=0,
        positive_stock_sku_count=0,
        zero_stock_sku_count=0,
        missing_sku_count=0,
        total_stock_ff=0.0,
    )


def _parse_sales_avg_period_days(value: Any) -> int:
    if value in ("", None):
        return _DEFAULT_SALES_AVG_PERIOD_DAYS
    try:
        numeric = int(str(value).strip())
    except ValueError as exc:
        raise ValueError("Период усреднения продаж должен быть целым числом") from exc
    if numeric <= 0:
        return _DEFAULT_SALES_AVG_PERIOD_DAYS
    return numeric


def _parse_cycle_supply_days(payload: Mapping[str, Any]) -> int:
    raw_value = payload.get("cycle_supply_days")
    if raw_value in ("", None):
        raw_value = payload.get("supply_horizon_days")
    if raw_value in ("", None):
        return _DEFAULT_CYCLE_SUPPLY_DAYS
    try:
        numeric = int(str(raw_value).strip())
    except ValueError as exc:
        raise ValueError("Цикл поставок должен быть целым числом") from exc
    if numeric <= 0:
        return _DEFAULT_CYCLE_SUPPLY_DAYS
    return numeric


def _parse_positive_int(value: Any, label: str) -> int:
    try:
        numeric = int(str(value).strip())
    except Exception as exc:
        raise ValueError(f"{label} должен быть целым числом") from exc
    if numeric <= 0:
        raise ValueError(f"{label} должен быть больше нуля")
    return numeric


def _parse_nonnegative_int(value: Any, label: str) -> int:
    try:
        numeric = int(str(value).strip())
    except Exception as exc:
        raise ValueError(f"{label} должен быть целым числом") from exc
    if numeric < 0:
        raise ValueError(f"{label} не может быть отрицательным")
    return numeric


def _parse_optional_date(value: Any, *, field_label: str) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    for parser in (_parse_iso_date, _parse_dotted_date, _parse_iso_datetime_date):
        parsed = parser(normalized)
        if parsed is not None:
            return parsed
    raise ValueError(f"{field_label} должна быть датой в формате YYYY-MM-DD или DD.MM.YYYY")


def _parse_iso_date(value: str) -> str | None:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _parse_iso_datetime_date(value: str) -> str | None:
    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).date().isoformat()
    except ValueError:
        return None


def _parse_dotted_date(value: str) -> str | None:
    try:
        return datetime.strptime(value, "%d.%m.%Y").date().isoformat()
    except ValueError:
        return None


def _district_filename(district_key: str) -> str:
    normalized_key = str(district_key or "").strip().lower()
    raw_stem = _DISTRICT_FILENAME_STEMS.get(normalized_key, normalized_key or "district")
    stem = "".join(char if char.isascii() and (char.isalnum() or char == "_") else "_" for char in raw_stem).strip("_")
    stem = stem or "district"
    return f"wb_regional_{stem}_fo.xlsx"


def _safe_report_date_for_filename(value: Any) -> str:
    normalized = str(value or "").strip()
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError:
        return _default_now_factory().date().isoformat()


def _allocate_boxes(
    *,
    full_recommendation_by_key: Mapping[str, int],
    raw_recommendation_by_key: Mapping[str, float],
    district_daily_demand_by_key: Mapping[str, float],
    projected_stock_by_key: Mapping[str, float],
    available_stock_ff: float,
    order_batch_qty: int,
) -> dict[str, int]:
    allocation_keys = tuple(full_recommendation_by_key.keys()) or tuple(SUPPLY_PLANNING_ZONE_KEYS)
    order_index = {key: index for index, key in enumerate(allocation_keys)}
    allocated = {key: 0 for key in allocation_keys}
    total_full = sum(
        max(int(full_recommendation_by_key.get(key, 0)), 0)
        for key in allocation_keys
    )
    ff_allocatable = int(math.floor(max(available_stock_ff, 0.0) / order_batch_qty) * order_batch_qty)
    if ff_allocatable <= 0 or total_full <= 0:
        return allocated
    if ff_allocatable >= total_full:
        return {
            key: max(int(full_recommendation_by_key.get(key, 0)), 0)
            for key in allocation_keys
        }

    remaining = ff_allocatable
    while remaining >= order_batch_qty:
        candidates = [
            key
            for key in allocation_keys
            if allocated[key] < max(int(full_recommendation_by_key.get(key, 0)), 0)
        ]
        if not candidates:
            break
        chosen = max(
            candidates,
            key=lambda key: (
                _marginal_saved_units(
                    raw_shortage_units=raw_recommendation_by_key.get(key, 0.0),
                    allocated_qty=allocated[key],
                    order_batch_qty=order_batch_qty,
                ),
                -_coverage_days(
                    projected_stock=projected_stock_by_key.get(key, 0.0),
                    allocated_qty=allocated[key],
                    avg_day=district_daily_demand_by_key.get(key, 0.0),
                ),
                float(district_daily_demand_by_key.get(key, 0.0)),
                -order_index[key],
            ),
        )
        allocated[chosen] += order_batch_qty
        remaining -= order_batch_qty
    return allocated


def _rebalance_central_recommendations(
    *,
    full_recommendation_by_key: dict[str, int],
    raw_recommendation_by_key: Mapping[str, float],
    district_daily_demand_by_key: Mapping[str, float],
    included_district_keys: tuple[str, ...],
    order_batch_qty: int,
) -> None:
    """Round the two-level Central total once, then distribute boxes deterministically."""

    central_keys = tuple(
        key for key in (PLANNING_ZONE_CENTRAL_NORTH, PLANNING_ZONE_CENTRAL_EAST, PLANNING_ZONE_CENTRAL_SOUTH)
        if key in included_district_keys
    )
    if not central_keys or order_batch_qty <= 0:
        return
    raw_total = sum(max(float(raw_recommendation_by_key.get(key, 0.0)), 0.0) for key in central_keys)
    target_boxes = int(math.ceil(raw_total / order_batch_qty) * order_batch_qty) if raw_total > 0 else 0
    if target_boxes <= 0:
        for key in central_keys:
            full_recommendation_by_key[key] = 0
        return
    weights = {
        key: max(float(raw_recommendation_by_key.get(key, 0.0)), 0.0)
        for key in central_keys
    }
    if sum(weights.values()) <= 0:
        weights = {
            key: max(float(district_daily_demand_by_key.get(key, 0.0)), 0.0)
            for key in central_keys
        }
    if sum(weights.values()) <= 0:
        weights = {key: 1.0 for key in central_keys}
    total_weight = sum(weights.values())
    exact_boxes = {key: target_boxes * weights[key] / total_weight for key in central_keys}
    allocated = {
        key: int(math.floor(exact_boxes[key] / order_batch_qty) * order_batch_qty)
        for key in central_keys
    }
    remaining = target_boxes - sum(allocated.values())
    order_index = {key: index for index, key in enumerate(central_keys)}
    while remaining >= order_batch_qty:
        chosen = max(
            central_keys,
            key=lambda key: (exact_boxes[key] - allocated[key], -order_index[key]),
        )
        allocated[chosen] += order_batch_qty
        remaining -= order_batch_qty
    for key in central_keys:
        full_recommendation_by_key[key] = int(allocated[key])


def _seed_floor_recommendation_by_key(
    *,
    demand_diagnostics: Mapping[str, Any],
    district_stock_by_key: Mapping[str, float],
    district_daily_demand_by_key: Mapping[str, float],
    daily_demand_total: float,
    included_district_keys: tuple[str, ...],
    order_batch_qty: int,
) -> dict[str, int]:
    allocation_keys = tuple(district_stock_by_key.keys()) or tuple(SUPPLY_PLANNING_ZONE_KEYS)
    seed = {key: 0 for key in allocation_keys}
    if float(daily_demand_total) <= 0 or int(order_batch_qty) <= 0:
        return seed
    included = set(included_district_keys)
    seed_reasons = dict(demand_diagnostics.get("seed_reason_by_district") or {})
    for key in allocation_keys:
        if key not in included or key not in seed_reasons:
            continue
        if float(district_daily_demand_by_key.get(key, 0.0) or 0.0) != 0.0:
            continue
        if not _is_missing_or_below_one_box_stock(
            district_stock_by_key.get(key),
            order_batch_qty=order_batch_qty,
        ):
            continue
        seed[key] = int(order_batch_qty)
    return seed


def _allocate_seed_boxes(
    *,
    seed_recommendation_by_key: Mapping[str, int],
    available_stock_ff: float,
    order_batch_qty: int,
) -> tuple[dict[str, int], dict[str, int]]:
    allocation_keys = tuple(seed_recommendation_by_key.keys()) or tuple(SUPPLY_PLANNING_ZONE_KEYS)
    allocated = {key: 0 for key in allocation_keys}
    unfulfilled = {key: 0 for key in allocation_keys}
    if int(order_batch_qty) <= 0:
        return allocated, {
            key: max(int(seed_recommendation_by_key.get(key, 0)), 0)
            for key in allocation_keys
        }
    remaining = int(math.floor(max(float(available_stock_ff), 0.0) / order_batch_qty) * order_batch_qty)
    for key in allocation_keys:
        requested = max(int(seed_recommendation_by_key.get(key, 0)), 0)
        if requested <= 0:
            continue
        if remaining >= requested:
            allocated[key] = requested
            remaining -= requested
        else:
            unfulfilled[key] = requested
    return allocated, unfulfilled


def _is_missing_or_below_one_box_stock(value: Any, *, order_batch_qty: int) -> bool:
    if value in ("", None):
        return True
    try:
        return float(value) < max(float(order_batch_qty), 1.0)
    except (TypeError, ValueError):
        return True


def _marginal_saved_units(*, raw_shortage_units: float, allocated_qty: int, order_batch_qty: int) -> float:
    return min(
        float(order_batch_qty),
        max(float(raw_shortage_units) - float(allocated_qty), 0.0),
    )


def _coverage_days(*, projected_stock: float, allocated_qty: int, avg_day: float) -> float:
    if avg_day <= 0:
        return float("inf")
    return (max(float(projected_stock), 0.0) + float(allocated_qty)) / float(avg_day)


def _truncate_sheet_name(value: str) -> str:
    normalized = str(value or "").strip() or "Рекомендация"
    return normalized[:31]
