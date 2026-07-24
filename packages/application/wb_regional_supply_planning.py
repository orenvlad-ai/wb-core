"""Read-only WB warehouse/date planning assistant for regional supply results."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping, Protocol

from packages.adapters.official_api_runtime import OfficialApiRuntimeError
from packages.adapters.wb_supplies import (
    HttpBackedWbSuppliesSource,
    WbSuppliesHttpStatusError,
    WbSuppliesTransportError,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.stocks_block import parse_excluded_wb_warehouse_ids
from packages.application.wb_supply_overlay import (
    DISTRICT_UNMAPPED,
    augment_supply_row_with_district,
    build_warehouse_district_mapping,
)
from packages.business_time import current_business_date_iso
from packages.contracts.wb_regional_supply import DISTRICT_KEYS, DISTRICT_LABELS_RU
from packages.contracts.wb_regional_supply_planning import (
    BOX_TYPE_IDS,
    CONTRACT_NAME,
    CONTRACT_VERSION,
    PACKAGE_TYPE_BOX,
    PACKAGE_TYPES,
    ROUTE_DIRECT,
    ROUTE_TRANSIT,
    STATUS_BLOCKED,
    STATUS_EMPTY,
    STATUS_NO_LAST_CALCULATION,
    STATUS_NO_OPTIONS,
    STATUS_READY,
    STATUS_UPSTREAM_ERROR,
    WAREHOUSE_SCOPE_OUTSIDE_DISTRICT,
    WAREHOUSE_SCOPE_SAME_DISTRICT,
    WAREHOUSE_SCOPE_UNMAPPED,
)
from packages.contracts.wb_supply_planning_zones import (
    CENTRAL_STORAGE_WAREHOUSES,
    CENTRAL_STORAGE_WAREHOUSES_BY_ID,
    SUPPLY_PLANNING_ZONE_KEYS,
    SUPPLY_PLANNING_ZONE_LABELS_RU,
    SUPPLY_PLANNING_ZONE_SHORT_LABELS_RU,
    WAREHOUSE_REGISTRY_VERSION,
    normalize_exact_warehouse_name,
    resolve_central_storage_warehouse,
    warehouse_name_exclusion_codes,
)


MAX_PLANNING_OPTIONS = 300
MAX_MAJOR_WAREHOUSE_PROBE_CALLS = 4

MAJOR_WAREHOUSE_PROBES_BY_DISTRICT = {
    zone_key: [
        item.canonical_name
        for item in CENTRAL_STORAGE_WAREHOUSES
        if item.planning_zone_key == zone_key
    ]
    for zone_key in SUPPLY_PLANNING_ZONE_KEYS
}


class WbRegionalSupplyPlanningSource(Protocol):
    def fetch_acceptance_options(
        self,
        *,
        products: list[Mapping[str, Any]],
        warehouse_id: int | str | None = None,
    ) -> Mapping[str, Any]:
        raise NotImplementedError

    def fetch_warehouses(self) -> list[Mapping[str, Any]]:
        raise NotImplementedError

    def fetch_marketplace_offices(self) -> list[Mapping[str, Any]]:
        raise NotImplementedError

    def fetch_box_tariffs(self, *, tariff_date: str | None = None) -> list[Mapping[str, Any]]:
        raise NotImplementedError

    def fetch_transit_tariffs(self) -> list[Mapping[str, Any]]:
        raise NotImplementedError

    def fetch_acceptance_coefficients(
        self,
        *,
        warehouse_ids: list[int | str] | None = None,
    ) -> list[Mapping[str, Any]]:
        raise NotImplementedError


class WbRegionalSupplyPlanningBlock:
    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        source: WbRegionalSupplyPlanningSource | None = None,
        timestamp_factory: Any | None = None,
        now_factory: Any | None = None,
    ) -> None:
        self.runtime = runtime
        self.source = source or HttpBackedWbSuppliesSource()
        self.timestamp_factory = timestamp_factory or _default_timestamp_factory
        self.now_factory = now_factory or _default_now_factory

    def build_options(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        request = _parse_request(payload or {})
        district_key = request["district_key"]
        if district_key not in SUPPLY_PLANNING_ZONE_KEYS:
            raise ValueError(f"Неизвестное направление расчёта поставки: {district_key}")

        last_result = self.runtime.load_wb_regional_supply_result_state()
        base_payload = self._base_payload(request=request, last_result=last_result)
        if not isinstance(last_result, Mapping):
            return {
                **base_payload,
                "status": STATUS_NO_LAST_CALCULATION,
                "blockers": [
                    {
                        "code": "no_last_calculation",
                        "message": "Результат расчёта по федеральным округам ещё не подготовлен.",
                    }
                ],
            }

        calculation_id = str(last_result.get("calculation_id") or "").strip()
        requested_calculation_id = request["calculation_id"]
        if requested_calculation_id and requested_calculation_id != calculation_id:
            return {
                **base_payload,
                "status": STATUS_BLOCKED,
                "calculation_id": calculation_id,
                "blockers": [
                    {
                        "code": "calculation_id_mismatch",
                        "message": "Последний региональный расчёт отличается от запрошенного calculation_id.",
                        "requested_calculation_id": requested_calculation_id,
                        "actual_calculation_id": calculation_id,
                    }
                ],
            }

        district = _find_district(last_result, district_key)
        if district is None:
            return {
                **base_payload,
                "status": STATUS_BLOCKED,
                "calculation_id": calculation_id,
                "blockers": [
                    {
                        "code": "district_not_in_last_calculation",
                        "message": "Выбранный округ не участвовал в последнем региональном расчёте.",
                        "district_key": district_key,
                    }
                ],
            }

        district_rows = [
            dict(row)
            for row in district.get("rows") or []
            if isinstance(row, Mapping) and _positive_int(row.get("allocated_qty")) > 0
        ]
        if not district_rows:
            return {
                **base_payload,
                "status": STATUS_EMPTY,
                "calculation_id": calculation_id,
                "district_name_ru": str(
                    district.get("planning_zone_label")
                    or district.get("district_name_ru")
                    or SUPPLY_PLANNING_ZONE_LABELS_RU[district_key]
                ),
                "summary": {
                    **base_payload["summary"],
                    "planned_product_count": 0,
                    "planned_qty_total": 0,
                },
                "warnings": ["В выбранном округе нет строк с количеством к поставке."],
            }

        products, barcode_blockers, barcode_summary = self._build_products(district_rows)
        payload_without_options = {
            **base_payload,
            "calculation_id": calculation_id,
            "district_name_ru": str(
                district.get("planning_zone_label")
                or district.get("district_name_ru")
                or SUPPLY_PLANNING_ZONE_LABELS_RU[district_key]
            ),
            "planning_zone_key": district_key,
            "planning_zone_label": SUPPLY_PLANNING_ZONE_LABELS_RU[district_key],
            "products": products,
            "barcode_summary": barcode_summary,
            "summary": {
                **base_payload["summary"],
                "planned_product_count": len(products),
                "planned_qty_total": sum(_positive_int(item.get("quantity")) for item in products),
                "missing_barcode_count": len(barcode_blockers),
            },
            "blockers": barcode_blockers,
            "evidence": {
                **base_payload["evidence"],
                "products_hash": _stable_hash(
                    [
                        {
                            "nm_id": item.get("nm_id"),
                            "barcode": item.get("barcode"),
                            "quantity": item.get("quantity"),
                        }
                        for item in products
                    ]
                ),
            },
        }
        if barcode_blockers:
            return {
                **payload_without_options,
                "status": STATUS_BLOCKED,
                "warnings": ["Для части SKU нет barcode; WB acceptance/options не вызван."],
            }

        request_products = [
            {"barcode": item["barcode"], "quantity": int(item["quantity"])}
            for item in products
            if item.get("barcode") and _positive_int(item.get("quantity")) > 0
        ]
        request_diagnostics = _acceptance_request_diagnostics(
            product_count=len(request_products),
            warehouse_id=request["warehouse_id"],
        )
        if not request_products:
            return {
                **payload_without_options,
                "status": STATUS_BLOCKED,
                "blockers": [
                    {
                        "code": "no_acceptance_products",
                        "message": "Нет товаров с barcode и положительным количеством для WB acceptance/options.",
                        "diagnostics": request_diagnostics,
                    }
                ],
                "warnings": ["WB acceptance/options не вызван: нет товаров для запроса."],
                "evidence": {
                    **payload_without_options["evidence"],
                    "acceptance_options": {
                        **request_diagnostics,
                        "status": "not_called",
                    },
                },
            }
        try:
            acceptance_payload = self.source.fetch_acceptance_options(
                products=request_products,
                warehouse_id=request["warehouse_id"],
            )
        except (OfficialApiRuntimeError, WbSuppliesHttpStatusError, WbSuppliesTransportError, OSError) as exc:
            error_payload = _upstream_error_payload(exc, request_diagnostics=request_diagnostics)
            return {
                **payload_without_options,
                "status": STATUS_UPSTREAM_ERROR,
                "blockers": [
                    {
                        "code": error_payload["code"],
                        "message": error_payload["message"],
                        "diagnostics": error_payload.get("diagnostics", {}),
                    }
                ],
                "warnings": [error_payload["message"]],
                "evidence": {
                    **payload_without_options["evidence"],
                    "acceptance_options": {
                        **request_diagnostics,
                        "status": "failed",
                        "error_code": error_payload["code"],
                        "diagnostics": error_payload.get("diagnostics", {}),
                    },
                },
            }

        warnings: list[str] = []
        acceptance_warnings, acceptance_blockers = _acceptance_payload_diagnostics(acceptance_payload, products)
        warnings.extend(acceptance_warnings)
        enrichment = self._fetch_enrichment(warnings=warnings)
        raw_option_rows = _extract_acceptance_option_rows(acceptance_payload)
        warehouse_specific_probe_result = _fetch_missing_major_warehouse_probes(
            source=self.source,
            district_key=district_key,
            request_products=request_products,
            raw_option_rows=raw_option_rows,
            warehouses=list(enrichment.get("warehouses") or []),
            warnings=warnings,
            excluded_warehouse_ids=request["excluded_wb_warehouse_ids"],
        )
        raw_option_rows.extend(list(warehouse_specific_probe_result.get("raw_rows") or []))
        if not raw_option_rows:
            operator_exclusions = set(request["excluded_wb_warehouse_ids"])
            selected_zone_ids = {
                item.warehouse_id
                for item in CENTRAL_STORAGE_WAREHOUSES
                if item.planning_zone_key == district_key and item.recommendation_enabled
            }
            exclusions_removed_zone = bool(operator_exclusions & selected_zone_ids)
            empty_blockers = (
                [
                    {
                        "code": "no_eligible_storage_warehouse_after_exclusions",
                        "message": (
                            "После выбранных исключений не осталось разрешённых "
                            "складов назначения WB."
                        ),
                    }
                ]
                if exclusions_removed_zone
                else acceptance_blockers
            )
            return {
                **payload_without_options,
                "status": STATUS_NO_OPTIONS,
                "blockers": empty_blockers,
                "warnings": warnings + ["WB acceptance/options не вернул доступных вариантов."],
                "evidence": {
                    **payload_without_options["evidence"],
                    "acceptance_options": {
                        **request_diagnostics,
                        "http_status": 200,
                        "status": "ok",
                        "raw_option_count": 0,
                    },
                },
            }

        planning_result = _build_options(
            raw_option_rows=raw_option_rows,
            acceptance_payload=acceptance_payload,
            district_key=district_key,
            products=products,
            package_type=request["package_type"],
            only_same_district=bool(request["only_same_district"]),
            include_transit=bool(request["include_transit"]),
            date_filter=request["date"],
            enrichment=enrichment,
            warnings=warnings,
            excluded_warehouse_ids=request["excluded_wb_warehouse_ids"],
            warehouse_specific_probes=dict(
                warehouse_specific_probe_result.get("diagnostics") or {}
            ),
        )
        options = list(planning_result.get("options") or [])
        status = STATUS_READY if options else STATUS_NO_OPTIONS
        available_option_count = int(
            dict(planning_result.get("summary") or {}).get("available_option_count") or 0
        )
        if not options:
            warnings.append("После фильтров не осталось доступных вариантов WB.")
        response_blockers: list[dict[str, Any]] = []
        if not options:
            exclusion_counts = dict(
                dict(planning_result.get("diagnostics") or {}).get(
                    "exclusion_reason_counts"
                )
                or {}
            )
            if exclusion_counts.get("excluded_by_operator", 0) > 0:
                response_blockers = [
                    {
                        "code": "no_eligible_storage_warehouse_after_exclusions",
                        "message": (
                            "После выбранных исключений не осталось разрешённых "
                            "складов назначения WB."
                        ),
                    }
                ]
            else:
                response_blockers = list(acceptance_blockers) or [
                    {
                        "code": "no_eligible_storage_warehouse",
                        "message": "Нет доступного склада хранения для всех обязательных ШК.",
                    }
                ]
        elif available_option_count <= 0:
            response_blockers = [
                {
                    "code": "no_available_date",
                    "message": "Для разрешённых складов хранения нет доступной даты коробочной поставки.",
                }
            ]
        return {
            **payload_without_options,
            "status": status,
            "options": options,
            "major_warehouse_diagnostics": list(planning_result.get("major_warehouse_diagnostics") or []),
            "diagnostics": dict(planning_result.get("diagnostics") or {}),
            "warnings": warnings,
            "blockers": response_blockers,
            "summary": {
                **payload_without_options["summary"],
                **dict(planning_result.get("summary") or {}),
                "option_count": len(options),
                "same_district_option_count": sum(
                    1 for item in options if item.get("warehouse_scope") == WAREHOUSE_SCOPE_SAME_DISTRICT
                ),
                "outside_district_option_count": sum(
                    1 for item in options if item.get("warehouse_scope") == WAREHOUSE_SCOPE_OUTSIDE_DISTRICT
                ),
                "unmapped_option_count": sum(
                    1 for item in options if item.get("warehouse_scope") == WAREHOUSE_SCOPE_UNMAPPED
                ),
                "transit_option_count": sum(1 for item in options if item.get("route_type") == ROUTE_TRANSIT),
            },
            "evidence": {
                **payload_without_options["evidence"],
                "acceptance_options": {
                    **request_diagnostics,
                    "http_status": 200,
                    "status": "ok",
                    "raw_option_count": len(raw_option_rows),
                    "raw_flat_row_count": planning_result.get("raw_flat_row_count", 0),
                    "grouped_warehouse_count": planning_result.get("grouped_warehouse_count", 0),
                },
                "enrichment": enrichment["evidence"],
            },
        }

    def _base_payload(
        self,
        *,
        request: Mapping[str, Any],
        last_result: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        district_key = str(request.get("district_key") or "")
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": STATUS_BLOCKED,
            "generated_at": self.timestamp_factory(),
            "calculation_id": str((last_result or {}).get("calculation_id") or ""),
            "calculated_at": str((last_result or {}).get("calculated_at") or ""),
            "district_key": district_key,
            "district_name_ru": SUPPLY_PLANNING_ZONE_LABELS_RU.get(district_key, ""),
            "planning_zone_key": district_key,
            "planning_zone_label": SUPPLY_PLANNING_ZONE_LABELS_RU.get(district_key, ""),
            "package_type": str(request.get("package_type") or PACKAGE_TYPE_BOX),
            "filters": {
                "warehouse_id": request.get("warehouse_id") or "",
                "date": request.get("date") or "",
                "only_same_district": bool(request.get("only_same_district")),
                "include_transit": bool(request.get("include_transit")),
                "excluded_wb_warehouse_ids": list(
                    request.get("excluded_wb_warehouse_ids") or ()
                ),
            },
            "products": [],
            "barcode_summary": {
                "total": 0,
                "ready": 0,
                "missing": 0,
                "manual": 0,
                "wb_content": 0,
                "multiple": 0,
            },
            "options": [],
            "summary": {
                "planned_product_count": 0,
                "planned_qty_total": 0,
                "option_count": 0,
            },
            "warnings": [],
            "blockers": [],
            "cache": {
                "enabled": False,
                "source_of_truth": "live_read_only_request",
            },
            "evidence": {
                "last_calculation_source": "sheet_vitrina_v1_wb_regional_supply_result_state.slot=1",
                "barcode_source": "sheet_vitrina_v1_nomenclature_items",
                "wb_api_read_only": True,
                "no_wb_mutations": True,
                "warehouse_registry_version": WAREHOUSE_REGISTRY_VERSION,
                "excluded_wb_warehouse_ids": list(
                    request.get("excluded_wb_warehouse_ids") or ()
                ),
            },
        }

    def _build_products(self, district_rows: list[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
        nomenclature = _nomenclature_by_nm_id(self.runtime.list_nomenclature_items(active_only=True))
        products: list[dict[str, Any]] = []
        blockers: list[dict[str, Any]] = []
        summary = {
            "total": 0,
            "ready": 0,
            "missing": 0,
            "manual": 0,
            "wb_content": 0,
            "multiple": 0,
        }
        for row in district_rows:
            nm_id = _positive_int(row.get("nm_id"))
            quantity = _positive_int(row.get("allocated_qty"))
            item = nomenclature.get(nm_id, {})
            barcodes = _normalized_barcodes(item)
            primary_barcode = str(item.get("primary_barcode") or item.get("barcode") or (barcodes[0] if barcodes else "")).strip()
            barcode_source = str(item.get("barcode_source") or "missing")
            barcode_status = str(item.get("barcode_status") or ("ready" if primary_barcode else "missing"))
            product = {
                "nm_id": nm_id,
                "sku_label": str(row.get("sku_comment") or item.get("nomenclature_name") or ""),
                "quantity": quantity,
                "barcode": primary_barcode,
                "primary_barcode": primary_barcode,
                "barcodes": barcodes,
                "barcode_source": barcode_source,
                "barcode_status": barcode_status,
                "barcode_ready": bool(primary_barcode),
                "barcode_evidence": item.get("barcode_evidence") if isinstance(item.get("barcode_evidence"), Mapping) else {},
                "stock_demand_diagnostics": {
                    "target_stock": float(row.get("target_stock_after_arrival") or 0.0),
                    "current_stock": float(row.get("current_stock") or 0.0),
                    "in_transit_qty": float(row.get("in_transit_qty") or 0.0),
                    "average_depletion": float(row.get("district_daily_demand") or 0.0),
                    "full_deficit": int(row.get("full_recommendation_qty") or 0),
                    "allocated_qty": int(row.get("allocated_qty") or 0),
                    "unfulfilled_deficit": int(row.get("deficit_qty") or 0),
                    "allocation_reason": str(row.get("allocation_reason") or ""),
                },
            }
            products.append(product)
            summary["total"] += 1
            if primary_barcode:
                summary["ready"] += 1
            else:
                summary["missing"] += 1
                blockers.append(
                    {
                        "code": "missing_barcode",
                        "nm_id": nm_id,
                        "sku_label": product["sku_label"],
                        "quantity": quantity,
                        "message": "Для SKU нет barcode в server-owned справочнике номенклатуры.",
                    }
                )
            if barcode_source == "manual":
                summary["manual"] += 1
            if barcode_source == "wb_content":
                summary["wb_content"] += 1
            if len(barcodes) > 1 or barcode_status == "multiple":
                summary["multiple"] += 1
        return products, blockers, summary

    def _fetch_enrichment(self, *, warnings: list[str]) -> dict[str, Any]:
        warehouses = _safe_fetch_list(
            lambda: self.source.fetch_warehouses(),
            warnings=warnings,
            label="warehouses",
            warning_ru="Не удалось получить список складов WB; используем данные acceptance/options.",
        )
        offices = _safe_fetch_list(
            lambda: self.source.fetch_marketplace_offices(),
            warnings=warnings,
            label="marketplace_offices",
            warning_ru="Не удалось получить Marketplace offices; mapping округов может быть неполным.",
        )
        tariff_date = current_business_date_iso(self.now_factory())
        box_tariffs = _safe_fetch_list(
            lambda: self.source.fetch_box_tariffs(tariff_date=tariff_date),
            warnings=warnings,
            label="box_tariffs",
            warning_ru="Не удалось получить box tariffs; тарифная evidence будет неполной.",
        )
        transit_tariffs = _safe_fetch_list(
            lambda: self.source.fetch_transit_tariffs(),
            warnings=warnings,
            label="transit_tariffs",
            warning_ru="Не удалось получить transit tariffs; route evidence будет неполной.",
        )
        coefficients = _safe_fetch_list(
            lambda: self.source.fetch_acceptance_coefficients(warehouse_ids=None),
            warnings=warnings,
            label="acceptance_coefficients",
            warning_ru="Не удалось получить acceptance coefficients; даты/коэффициенты будут неполными.",
        )
        return {
            "warehouses": warehouses,
            "offices": offices,
            "box_tariffs": box_tariffs,
            "transit_tariffs": transit_tariffs,
            "coefficients": coefficients,
            "evidence": {
                "warehouses": {"endpoint": "GET /api/v1/warehouses", "row_count": len(warehouses)},
                "marketplace_offices": {"endpoint": "GET /api/v3/offices", "row_count": len(offices)},
                "box_tariffs": {"endpoint": "GET /api/v1/tariffs/box", "row_count": len(box_tariffs)},
                "transit_tariffs": {"endpoint": "GET /api/v1/transit-tariffs", "row_count": len(transit_tariffs)},
                "acceptance_coefficients": {
                    "endpoint": "GET /api/tariffs/v1/acceptance/coefficients",
                    "row_count": len(coefficients),
                },
            },
        }


def _build_options(
    *,
    raw_option_rows: list[Mapping[str, Any]],
    acceptance_payload: Mapping[str, Any],
    district_key: str,
    products: list[Mapping[str, Any]],
    package_type: str,
    only_same_district: bool,
    include_transit: bool,
    date_filter: str,
    enrichment: Mapping[str, Any],
    warnings: list[str],
    excluded_warehouse_ids: tuple[int, ...] = (),
    warehouse_specific_probes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the manager view from WB evidence after fail-closed filtering."""

    flat_rows = _flatten_acceptance_option_rows(raw_option_rows)
    normalized_rows = [_normalize_acceptance_option_row(row) for row in flat_rows]
    normalized_rows = [
        row for row in normalized_rows if row.get("warehouse_id") or row.get("warehouse_name")
    ]
    warehouse_name_by_id = _warehouse_name_by_id(list(enrichment.get("warehouses") or []))
    normalized_rows = [
        _fill_acceptance_warehouse_name(row, warehouse_name_by_id) for row in normalized_rows
    ]
    normalized_rows = _dedupe_acceptance_option_rows(normalized_rows)
    grouped_rows = _group_acceptance_rows_by_warehouse(normalized_rows)

    catalog_by_id = {
        _warehouse_key(_first_string(row, "warehouseID", "warehouseId", "warehouse_id", "ID", "id")): row
        for row in list(enrichment.get("warehouses") or [])
        if _warehouse_key(_first_string(row, "warehouseID", "warehouseId", "warehouse_id", "ID", "id"))
    }
    coefficient_rows = list(enrichment.get("coefficients") or [])
    coefficients_by_warehouse = _coefficient_rows_by_warehouse(coefficient_rows)
    box_tariff_by_name = _tariff_by_warehouse_name(list(enrichment.get("box_tariffs") or []))
    transit_tariff_rows = list(enrichment.get("transit_tariffs") or [])
    product_barcodes = {
        str(item.get("barcode") or "").strip()
        for item in products
        if str(item.get("barcode") or "").strip()
    }
    generic_mapping = build_warehouse_district_mapping(
        warehouse_rows=list(enrichment.get("warehouses") or []),
        supply_rows=[
            {
                "warehouse_id": row.get("warehouse_id") or "",
                "warehouse_name": row.get("warehouse_name") or "",
            }
            for row in normalized_rows
        ],
        office_rows=list(enrichment.get("offices") or []),
        tariff_rows=list(enrichment.get("box_tariffs") or []),
    )

    manager_options: list[dict[str, Any]] = []
    exclusion_diagnostics: list[dict[str, Any]] = []
    exclusion_counts: dict[str, int] = {}
    operator_exclusions = set(excluded_warehouse_ids)
    for group in grouped_rows:
        warehouse_id = str(group.get("warehouse_id") or "").strip()
        warehouse_lookup_key = _warehouse_key(warehouse_id)
        upstream_name = str(group.get("warehouse_name") or "").strip()
        catalog_row = catalog_by_id.get(warehouse_lookup_key)
        catalog_name = (
            _first_string(catalog_row, "warehouseName", "warehouse_name", "name")
            if isinstance(catalog_row, Mapping)
            else ""
        )
        warehouse_name = catalog_name or upstream_name
        registry_item, classification_source = resolve_central_storage_warehouse(
            warehouse_id=warehouse_id,
            warehouse_name=warehouse_name,
            historical=False,
        )
        is_central_zone = district_key.startswith("central_")
        if registry_item is not None:
            planning_zone_key = registry_item.planning_zone_key
            planning_zone_label = registry_item.planning_zone_label
            warehouse_role = registry_item.role
            canonical_name = registry_item.canonical_name
            recommendation_enabled = registry_item.recommendation_enabled
            blocked_reason = registry_item.blocked_reason or ""
            is_storage_warehouse = registry_item.storage_kind == "storage"
        else:
            mapped = augment_supply_row_with_district(
                {"warehouse_id": warehouse_id, "warehouse_name": warehouse_name},
                generic_mapping,
            )
            planning_zone_key = str(mapped.get("district_key") or DISTRICT_UNMAPPED)
            planning_zone_label = DISTRICT_LABELS_RU.get(planning_zone_key, "")
            warehouse_role = "other"
            canonical_name = warehouse_name
            recommendation_enabled = not is_central_zone
            blocked_reason = ""
            is_storage_warehouse = not is_central_zone

        coefficient_matches = coefficients_by_warehouse.get(warehouse_lookup_key) or coefficients_by_warehouse.get(
            _normalize_name(warehouse_name)
        ) or []
        catalog_active = (
            _first_bool(catalog_row, "isActive", "is_active", "active")
            if isinstance(catalog_row, Mapping)
            else None
        )
        is_sorting_center = bool(
            any(
                (
                    _first_bool(row, "isSortingCenter", "is_sorting_center") is True
                    or dict(row.get("raw_flags") or {}).get("isSortingCenter") is True
                )
                for row in coefficient_matches
            )
            or (
                isinstance(catalog_row, Mapping)
                and _first_bool(catalog_row, "isSortingCenter", "is_sorting_center") is True
            )
            or "sorting_center_name" in warehouse_name_exclusion_codes(warehouse_name)
        )
        name_exclusion_codes = list(warehouse_name_exclusion_codes(warehouse_name))
        accepts_all_barcodes = _barcode_coverage(group, product_barcodes).get(
            "accepts_all_barcodes", False
        )
        barcode_coverage = _barcode_coverage(group, product_barcodes)
        package_supported = all(
            bool(
                barcode_rows := [
                    row
                    for row in list(group.get("raw_rows") or [])
                    if str(row.get("barcode") or "").strip() == barcode
                ]
            )
            and all(
                dict(row.get("raw_flags") or {}).get("canBox") is True
                for row in barcode_rows
            )
            for barcode in product_barcodes
        ) if product_barcodes else False
        route_type = ROUTE_TRANSIT if group.get("route_type") == ROUTE_TRANSIT else ROUTE_DIRECT

        exclusion_codes: list[str] = []
        if not warehouse_id:
            exclusion_codes.append("warehouse_id_missing")
        if warehouse_id == "0":
            exclusion_codes.append("wb_aggregate_service_group_not_destination")
        if warehouse_id.isdigit() and int(warehouse_id) in operator_exclusions:
            exclusion_codes.append("excluded_by_operator")
        if is_central_zone and registry_item is None:
            exclusion_codes.append("warehouse_unclassified")
        if planning_zone_key != district_key:
            exclusion_codes.append("outside_selected_planning_zone")
        if not recommendation_enabled:
            exclusion_codes.append("recommendation_disabled")
        if blocked_reason:
            exclusion_codes.append("warehouse_blocked")
        if not is_storage_warehouse:
            exclusion_codes.append("not_storage_warehouse")
        if is_sorting_center and "sorting_center" not in exclusion_codes:
            exclusion_codes.append("sorting_center")
        exclusion_codes.extend(name_exclusion_codes)
        if catalog_row is None:
            exclusion_codes.append("warehouse_catalog_missing")
        elif catalog_active is not True:
            exclusion_codes.append("warehouse_inactive")
        if not accepts_all_barcodes:
            exclusion_codes.append("partial_barcode_coverage")
        if not package_supported:
            exclusion_codes.append("box_not_supported")
        if route_type == ROUTE_TRANSIT and not include_transit:
            exclusion_codes.append("transit_route_not_allowed")
        exclusion_codes = list(dict.fromkeys(exclusion_codes))

        if exclusion_codes:
            for code in exclusion_codes:
                exclusion_counts[code] = exclusion_counts.get(code, 0) + 1
            exclusion_diagnostics.append(
                {
                    "warehouse_id": warehouse_id,
                    "warehouse_name": warehouse_name,
                    "planning_zone_key": planning_zone_key,
                    "classification_source": classification_source,
                    "exclusion_reasons": exclusion_codes,
                    "barcode_coverage": barcode_coverage,
                    "package_supported": package_supported,
                    "catalog_active": catalog_active,
                    "is_sorting_center": is_sorting_center,
                }
            )
            continue

        dates = _warehouse_acceptance_dates(group, coefficients_by_warehouse, date_filter=date_filter)
        available_dates = [item for item in dates if item.get("is_available")]
        free_dates = [item for item in dates if item.get("is_free_date")]
        first_available = available_dates[0] if available_dates else {}
        first_free = free_dates[0] if free_dates else {}
        display_name = canonical_name or warehouse_name
        box_tariff = box_tariff_by_name.get(_normalize_name(display_name)) or box_tariff_by_name.get(
            _normalize_name(warehouse_name)
        )
        transit_routes = (
            _transit_tariffs_for_destination(display_name, transit_tariff_rows)
            if include_transit
            else []
        )
        best_transit = _best_transit_tariff(transit_routes)
        blocker_codes = [] if available_dates else ["no_available_date"]
        ranking_evidence = {
            "has_available_date": bool(available_dates),
            "warehouse_role": warehouse_role,
            "role_rank": {"primary": 0, "reserve": 1, "far_reserve": 2, "other": 3}.get(
                warehouse_role, 4
            ),
            "first_available_date": first_available.get("date") or "",
            "first_free_date": first_free.get("date") or "",
            "catalog_active": catalog_active is True,
            "accepts_all_barcodes": bool(accepts_all_barcodes),
            "package_supported": bool(package_supported),
            "direct_destination": route_type == ROUTE_DIRECT,
        }
        option = {
            "option_id": _stable_hash(
                {"warehouse_id": warehouse_id, "planning_zone_key": planning_zone_key}
            )[:16],
            "option_kind": "warehouse_group",
            "rank": 0,
            "recommendation": "",
            "recommendation_explanation": "",
            "planning_zone_key": planning_zone_key,
            "planning_zone_label": planning_zone_label,
            "district_key": planning_zone_key,
            "district_name_ru": planning_zone_label,
            "warehouse_id": warehouse_id,
            "warehouse_name": display_name,
            "warehouse_role": warehouse_role,
            "warehouse_scope": WAREHOUSE_SCOPE_SAME_DISTRICT,
            "classification_source": classification_source,
            "warehouse_registry_version": WAREHOUSE_REGISTRY_VERSION,
            "accepts_all_barcodes": bool(accepts_all_barcodes),
            "barcode_coverage": barcode_coverage,
            "package_type": package_type,
            "package_supported": bool(package_supported),
            "is_storage_warehouse": bool(is_storage_warehouse),
            "is_sorting_center": bool(is_sorting_center),
            "recommendation_enabled": bool(recommendation_enabled),
            "dates": dates,
            "date": first_available.get("date") or "",
            "first_available_date": first_available.get("date") or "",
            "first_free_date": first_free.get("date") or "",
            "unique_available_date_count": len(available_dates),
            "unique_free_date_count": len(free_dates),
            "date_count": len(dates),
            "good_date_count": len(available_dates),
            "free_date_count": len(free_dates),
            "coefficient": first_available.get("coefficient"),
            "coefficient_display": _format_number(first_available.get("coefficient")),
            "allow_unload": first_available.get("allow_unload"),
            "route_type": route_type,
            "direct_destination": route_type == ROUTE_DIRECT,
            "transit_warehouse_id": "",
            "transit_warehouse_name": "",
            "box_tariff": _compact_box_tariff_row(box_tariff),
            "tariff_evidence": _tariff_evidence(box_tariff, best_transit),
            "known_tariff_value": _known_tariff_value(box_tariff, best_transit),
            "transit_route_count": len(transit_routes),
            "best_transit_route": _compact_transit_tariff_row(best_transit),
            "blocker_codes": blocker_codes,
            "exclusion_reasons": [],
            "ranking_evidence": ranking_evidence,
            "status": "available" if available_dates else "no_available_date",
            "warnings": (
                []
                if available_dates
                else ["WB не вернул доступную дату коробочной поставки в официальном горизонте."]
            ),
            "stock_demand_diagnostics": {
                "products": [
                    {
                        "nm_id": item.get("nm_id"),
                        **dict(item.get("stock_demand_diagnostics") or {}),
                    }
                    for item in products
                ],
                "allocated_qty_total": sum(
                    int(item.get("quantity") or 0) for item in products
                ),
            },
            "evidence": {
                "acceptance_options": "all_required_barcodes",
                "warehouse_catalog": "exact_warehouse_id",
                "classification": classification_source,
                "coefficient_contract": "boxTypeID in {1,2}; coefficient in {0,1}; allowUnload=true",
            },
        }
        option["_rank_tuple"] = (
            0 if available_dates else 1,
            ranking_evidence["role_rank"],
            first_available.get("date") or "9999-99-99",
            first_free.get("date") or "9999-99-99",
            float(option["known_tariff_value"])
            if isinstance(option.get("known_tariff_value"), (int, float))
            else 999_999.0,
            int(warehouse_id) if warehouse_id.isdigit() else 999_999_999,
        )
        option["operator_handoff"] = _operator_handoff(
            option=option,
            district_key=district_key,
            products=products,
            acceptance_payload=acceptance_payload,
        )
        manager_options.append(option)

    manager_options.sort(key=lambda item: item["_rank_tuple"])
    visible_options = manager_options[:MAX_PLANNING_OPTIONS]
    for index, option in enumerate(visible_options, start=1):
        option["rank"] = index
        option["recommendation"] = (
            "Рекомендуемый склад"
            if index == 1 and option.get("status") == "available"
            else f"Альтернатива #{index}"
        )
        option["recommendation_explanation"] = (
            f"{option.get('planning_zone_label')}; роль {option.get('warehouse_role')}; "
            + (
                f"ближайшая дата {option.get('first_available_date')}"
                if option.get("first_available_date")
                else "нет доступной даты"
            )
        )
        option.pop("_rank_tuple", None)
    if len(manager_options) > MAX_PLANNING_OPTIONS:
        warnings.append(
            f"После строгой фильтрации осталось {len(manager_options)} складов; показаны первые {MAX_PLANNING_OPTIONS}."
        )

    probe_diagnostics = [
        dict(value)
        for value in dict(warehouse_specific_probes or {}).values()
        if isinstance(value, Mapping)
    ]
    available_option_count = sum(1 for item in visible_options if item.get("status") == "available")
    return {
        "options": visible_options,
        "major_warehouse_diagnostics": probe_diagnostics,
        "raw_flat_row_count": len(flat_rows),
        "grouped_warehouse_count": len(grouped_rows),
        "summary": {
            "raw_acceptance_result_count": len(raw_option_rows),
            "raw_acceptance_flat_row_count": len(flat_rows),
            "grouped_warehouse_count": len(grouped_rows),
            "visible_grouped_warehouse_count": len(visible_options),
            "option_count": len(visible_options),
            "available_option_count": available_option_count,
            "excluded_option_count": len(exclusion_diagnostics),
            "accepts_all_barcode_option_count": len(visible_options),
            "sorting_center_excluded_count": sum(
                count
                for code, count in exclusion_counts.items()
                if code in {"sorting_center", "sorting_center_name"}
            ),
            "specialized_excluded_count": sum(
                count
                for code, count in exclusion_counts.items()
                if code in {"specialized_food", "specialized_fuel", "specialized_tires"}
            ),
            "sgt_excluded_count": exclusion_counts.get("sgt_warehouse", 0),
            "partial_excluded_count": exclusion_counts.get("partial_barcode_coverage", 0),
            "can_box_false_excluded_count": exclusion_counts.get("box_not_supported", 0),
            "inactive_excluded_count": exclusion_counts.get("warehouse_inactive", 0),
            "blocked_excluded_count": exclusion_counts.get("warehouse_blocked", 0),
            "unmapped_excluded_count": exclusion_counts.get("warehouse_unclassified", 0),
            "operator_excluded_count": exclusion_counts.get("excluded_by_operator", 0),
            "service_group_excluded_count": exclusion_counts.get(
                "wb_aggregate_service_group_not_destination", 0
            ),
        },
        "diagnostics": {
            "request_id": str(acceptance_payload.get("requestId") or ""),
            "requested_barcode_count": len(product_barcodes),
            "raw_option_count": len(flat_rows),
            "grouped_warehouse_count": len(grouped_rows),
            "exclusion_reason_counts": exclusion_counts,
            "excluded_options": exclusion_diagnostics,
            "warehouse_registry_version": WAREHOUSE_REGISTRY_VERSION,
            "warehouse_registry_entry_count": len(CENTRAL_STORAGE_WAREHOUSES),
            "box_type_ids": list(BOX_TYPE_IDS),
            "coefficient_horizon_days": 14,
            "manager_view_fail_closed": True,
        },
    }


def _fetch_missing_major_warehouse_probes(
    *,
    source: WbRegionalSupplyPlanningSource,
    district_key: str,
    request_products: list[Mapping[str, Any]],
    raw_option_rows: list[Mapping[str, Any]],
    warehouses: list[Mapping[str, Any]],
    warnings: list[str],
    excluded_warehouse_ids: tuple[int, ...] = (),
) -> dict[str, Any]:
    expected = [
        item
        for item in CENTRAL_STORAGE_WAREHOUSES
        if item.planning_zone_key == district_key and item.recommendation_enabled
    ]
    if not expected:
        return {"raw_rows": [], "diagnostics": {}}
    general_ids = {
        _warehouse_key(_first_string(row, "warehouseID", "warehouseId", "warehouse_id", "ID", "id"))
        for row in _flatten_acceptance_option_rows(raw_option_rows)
    }
    catalog_by_id = {
        _warehouse_key(_first_string(row, "warehouseID", "warehouseId", "warehouse_id", "ID", "id")): row
        for row in warehouses
        if _warehouse_key(_first_string(row, "warehouseID", "warehouseId", "warehouse_id", "ID", "id"))
    }
    diagnostics: dict[str, Any] = {}
    probed_raw_rows: list[Mapping[str, Any]] = []
    probe_calls = 0
    operator_exclusions = set(excluded_warehouse_ids)
    for registry_item in expected:
        warehouse_id = str(registry_item.warehouse_id)
        warehouse_lookup_key = _warehouse_key(warehouse_id)
        catalog_row = catalog_by_id.get(warehouse_lookup_key)
        base_diagnostic = {
            "warehouse_id": registry_item.warehouse_id,
            "expected_warehouse_name": registry_item.canonical_name,
            "planning_zone_key": registry_item.planning_zone_key,
            "found_in_catalog": catalog_row is not None,
            "catalog_active": (
                _first_bool(catalog_row, "isActive", "is_active", "active")
                if isinstance(catalog_row, Mapping)
                else None
            ),
        }
        if registry_item.warehouse_id in operator_exclusions:
            diagnostics[registry_item.canonical_name] = {
                **base_diagnostic,
                "status": "excluded_by_operator",
                "probe_called": False,
            }
            continue
        if warehouse_lookup_key in general_ids:
            diagnostics[registry_item.canonical_name] = {
                **base_diagnostic,
                "status": "returned_by_general_acceptance_options",
                "probe_called": False,
            }
            continue
        if probe_calls >= MAX_MAJOR_WAREHOUSE_PROBE_CALLS:
            diagnostics[registry_item.canonical_name] = {
                **base_diagnostic,
                "status": "probe_limit_reached",
                "probe_called": False,
            }
            continue
        probe_calls += 1
        try:
            payload = source.fetch_acceptance_options(
                products=request_products,
                warehouse_id=registry_item.warehouse_id,
            )
            top_rows = _extract_acceptance_option_rows(payload)
            probed_raw_rows.extend(top_rows)
            flat_rows = _flatten_acceptance_option_rows(top_rows)
            returned_ids = {
                _warehouse_key(
                    _first_string(row, "warehouseID", "warehouseId", "warehouse_id", "ID", "id")
                )
                for row in flat_rows
            }
            diagnostics[registry_item.canonical_name] = {
                **base_diagnostic,
                "status": "ok",
                "probe_called": True,
                "http_status": 200,
                "top_result_count": len(top_rows),
                "flat_row_count": len(flat_rows),
                "exact_warehouse_returned": warehouse_lookup_key in returned_ids,
            }
        except (OfficialApiRuntimeError, WbSuppliesHttpStatusError, WbSuppliesTransportError, OSError, ValueError) as exc:
            diagnostics[registry_item.canonical_name] = {
                **base_diagnostic,
                "status": "error",
                "probe_called": True,
                "error": _safe_error_message(exc),
            }
    if probe_calls >= MAX_MAJOR_WAREHOUSE_PROBE_CALLS:
        warnings.append("Диагностические warehouseID-probes ограничены безопасным лимитом.")
    return {"raw_rows": probed_raw_rows, "diagnostics": diagnostics}


def _major_warehouse_diagnostics(
    *,
    district_key: str,
    base_rows: list[Mapping[str, Any]],
    all_options: list[Mapping[str, Any]],
    visible_options: list[Mapping[str, Any]],
    warehouses: list[Mapping[str, Any]],
    box_tariffs: list[Mapping[str, Any]],
    coefficients: list[Mapping[str, Any]],
    offices: list[Mapping[str, Any]],
    mapping: Mapping[str, Any],
    product_barcode_count: int,
    warehouse_specific_probes: Mapping[str, Any],
) -> list[dict[str, Any]]:
    expected_names = MAJOR_WAREHOUSE_PROBES_BY_DISTRICT.get(district_key, [])
    if not expected_names:
        return []
    visible_keys = {str(item.get("warehouse_group_key") or "") for item in visible_options}
    diagnostics: list[dict[str, Any]] = []
    for expected in expected_names:
        acceptance_matches = [row for row in base_rows if _warehouse_name_matches(row.get("warehouse_name"), expected)]
        catalog_matches = [
            row
            for row in warehouses
            if _warehouse_name_matches(_first_string(row, "warehouseName", "warehouse_name", "name"), expected)
        ]
        box_matches = [
            row
            for row in box_tariffs
            if _warehouse_name_matches(_first_string(row, "warehouseName", "warehouse_name", "name"), expected)
        ]
        coefficient_matches = [
            row
            for row in coefficients
            if _warehouse_name_matches(_first_string(row, "warehouseName", "warehouse_name", "name"), expected)
        ]
        office_matches = [
            row
            for row in offices
            if _warehouse_name_matches(_first_string(row, "name", "warehouseName", "warehouse_name"), expected)
        ]
        option_matches = [item for item in all_options if _warehouse_name_matches(item.get("warehouse_name"), expected)]
        visible_matches = [item for item in visible_options if _warehouse_name_matches(item.get("warehouse_name"), expected)]
        accepted_barcodes: set[str] = set()
        for row in acceptance_matches:
            row_barcodes = list(row.get("accepted_barcodes") or [])
            barcode = str(row.get("barcode") or "").strip()
            if barcode:
                row_barcodes.append(barcode)
            for item in row_barcodes:
                normalized_barcode = str(item or "").strip()
                if normalized_barcode:
                    accepted_barcodes.add(normalized_barcode)
        coeff_values = [_first_nested_number(row.get("coefficient")) for row in coefficient_matches]
        numeric_coefficients = [value for value in coeff_values if isinstance(value, (int, float))]
        non_negative_coefficients = [value for value in numeric_coefficients if value >= 0]
        probe = dict(warehouse_specific_probes.get(expected) or {})
        hidden_reason = _major_hidden_reason(
            acceptance_matches=acceptance_matches,
            option_matches=option_matches,
            visible_matches=visible_matches,
            visible_keys=visible_keys,
            probe=probe,
        )
        mapping_row = _major_mapping_row(expected, acceptance_matches, catalog_matches, box_matches)
        mapped = augment_supply_row_with_district(mapping_row, mapping) if mapping_row else {}
        diagnostics.append(
            {
                "expected_warehouse_name": expected,
                "found_in_acceptance_options": bool(acceptance_matches),
                "found_in_warehouses_catalog": bool(catalog_matches),
                "found_in_box_tariffs": bool(box_matches),
                "found_in_acceptance_coefficients": bool(coefficient_matches),
                "found_in_marketplace_offices": bool(office_matches),
                "matched_acceptance_names": sorted({str(row.get("warehouse_name") or "") for row in acceptance_matches if row.get("warehouse_name")})[:10],
                "catalog_warehouse_ids": [
                    _first_string(row, "warehouseID", "warehouseId", "warehouse_id", "ID", "id")
                    for row in catalog_matches[:10]
                ],
                "mapped_district_key": str(mapped.get("district_key") or DISTRICT_UNMAPPED),
                "mapped_district_label_ru": DISTRICT_LABELS_RU.get(str(mapped.get("district_key") or ""), ""),
                "mapped_district_source": mapped.get("district_mapping_source") or "",
                "accepted_barcode_count": len(accepted_barcodes),
                "total_barcode_count": product_barcode_count,
                "accepts_all_barcodes": bool(product_barcode_count) and len(accepted_barcodes) >= product_barcode_count,
                "min_coefficient": min(numeric_coefficients) if numeric_coefficients else None,
                "best_available_coefficient": min(non_negative_coefficients) if non_negative_coefficients else None,
                "has_free_date": 0 in numeric_coefficients,
                "allow_unload": any(_first_bool(row, "allowUnload", "allow_unload", "canUnload", "can_unload") is True for row in coefficient_matches),
                "visible_in_main_list": bool(visible_matches),
                "visible_option_ids": [str(item.get("option_id") or "") for item in visible_matches[:10]],
                "hidden_reason": hidden_reason,
                "warehouse_specific_probe": probe,
            }
        )
    return diagnostics


def _major_mapping_row(
    expected: str,
    acceptance_matches: list[Mapping[str, Any]],
    catalog_matches: list[Mapping[str, Any]],
    box_matches: list[Mapping[str, Any]],
) -> dict[str, Any]:
    if acceptance_matches:
        row = acceptance_matches[0]
        return {
            "warehouse_id": row.get("warehouse_id") or "",
            "warehouse_name": row.get("warehouse_name") or expected,
        }
    if catalog_matches:
        row = catalog_matches[0]
        return {
            "warehouse_id": _first_string(row, "warehouseID", "warehouseId", "warehouse_id", "ID", "id"),
            "warehouse_name": _first_string(row, "warehouseName", "warehouse_name", "name") or expected,
        }
    if box_matches:
        row = box_matches[0]
        return {
            "warehouse_id": "",
            "warehouse_name": _first_string(row, "warehouseName", "warehouse_name", "name") or expected,
        }
    return {"warehouse_id": "", "warehouse_name": expected}


def _major_hidden_reason(
    *,
    acceptance_matches: list[Mapping[str, Any]],
    option_matches: list[Mapping[str, Any]],
    visible_matches: list[Mapping[str, Any]],
    visible_keys: set[str],
    probe: Mapping[str, Any],
) -> str:
    if visible_matches:
        return "visible"
    if not acceptance_matches:
        probe_rows = 0
        for item in probe.get("probes") or []:
            if isinstance(item, Mapping):
                probe_rows += _positive_int(item.get("matching_row_count"))
        if probe_rows > 0:
            return "not_returned_in_general_batch_returned_by_warehouse_specific_probe"
        return "not_returned_by_acceptance_options"
    if not option_matches:
        return "hidden_by_filter"
    if not any(str(item.get("warehouse_group_key") or "") in visible_keys for item in option_matches):
        return "hidden_by_cap"
    return "other"


def _parse_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    package_type = str(payload.get("package_type") or PACKAGE_TYPE_BOX).strip() or PACKAGE_TYPE_BOX
    if package_type not in PACKAGE_TYPES:
        raise ValueError(f"package_type пока поддерживается только {PACKAGE_TYPE_BOX}")
    return {
        "district_key": str(
            payload.get("planning_zone_key") or payload.get("district_key") or ""
        ).strip().lower(),
        "calculation_id": str(payload.get("calculation_id") or "").strip(),
        "package_type": package_type,
        "warehouse_id": str(payload.get("warehouse_id") or "").strip(),
        "date": str(payload.get("date") or "").strip(),
        "only_same_district": _as_bool(payload.get("only_same_district"), default=True),
        "include_transit": _as_bool(payload.get("include_transit"), default=False),
        "excluded_wb_warehouse_ids": parse_excluded_wb_warehouse_ids(
            payload,
            allow_legacy_elektrostal=False,
        ),
    }


def _find_district(result: Mapping[str, Any], district_key: str) -> Mapping[str, Any] | None:
    for item in result.get("districts") or []:
        if isinstance(item, Mapping) and str(
            item.get("planning_zone_key") or item.get("district_key") or ""
        ).strip().lower() == district_key:
            return item
    return None


def _nomenclature_by_nm_id(items: list[Mapping[str, Any]]) -> dict[int, Mapping[str, Any]]:
    result: dict[int, Mapping[str, Any]] = {}
    for item in items:
        nm_id = _positive_int(item.get("nm_id"))
        if nm_id > 0 and nm_id not in result:
            result[nm_id] = item
    return result


def _normalized_barcodes(item: Mapping[str, Any]) -> list[str]:
    values: list[Any] = []
    if item.get("barcode"):
        values.append(item.get("barcode"))
    values.extend(item.get("barcodes") or [])
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        barcode = str(value or "").strip()
        if barcode and barcode not in seen:
            seen.add(barcode)
            result.append(barcode)
    return result


def _extract_acceptance_option_rows(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    for key in ("result", "warehouses", "options", "items", "rows", "response", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
        nested = _extract_acceptance_option_rows(value)
        if nested:
            return nested
    return []


def _normalize_acceptance_option_row(row: Mapping[str, Any]) -> dict[str, Any]:
    warehouse_id = _first_string(row, "warehouseID", "warehouseId", "warehouse_id", "ID", "id")
    warehouse_name = _first_string(row, "warehouseName", "warehouse_name", "name")
    transit_warehouse_id = _first_string(row, "transitWarehouseID", "transitWarehouseId", "transit_warehouse_id")
    transit_warehouse_name = _first_string(row, "transitWarehouseName", "transit_warehouse_name")
    route_type = ROUTE_TRANSIT if transit_warehouse_id or transit_warehouse_name or _as_bool(row.get("isTransit")) else ROUTE_DIRECT
    return {
        "warehouse_id": warehouse_id,
        "warehouse_name": warehouse_name,
        "barcode": _first_string(row, "barcode", "Barcode"),
        "date": _first_string(
            row,
            "date",
            "supplyDate",
            "acceptanceDate",
            "unloadingDate",
            "availableDate",
        ),
        "route_type": route_type,
        "transit_warehouse_id": transit_warehouse_id,
        "transit_warehouse_name": transit_warehouse_name,
        "coefficient": _first_number(
            row,
            "coefficient",
            "coef",
            "acceptanceCoefficient",
            "acceptance_coefficient",
            "storageCoef",
            "paidAcceptanceCoefficient",
        ),
        "allow_unload": _first_bool(row, "allowUnload", "allow_unload", "canUnload", "can_unload"),
        "dropoff_allowed": _first_bool(row, "dropoff", "allowDropoff", "dropoffAllowed", "dropoff_allowed"),
        "pickup_allowed": _first_bool(row, "pickup", "allowPickup", "pickupAllowed", "pickup_allowed"),
        "raw_flags": {
            key: row.get(key)
            for key in (
                "canBox",
                "canMonopallet",
                "canSupersafe",
                "boxTypeID",
                "boxTypeName",
                "coefficient",
                "allowUnload",
                "isSortingCenter",
            )
            if key in row
        },
        "evidence": {
            "source": "acceptance_options",
            "barcode": _first_string(row, "barcode", "Barcode"),
            "barcode_row_has_warehouses": bool(row.get("_barcode_row_had_warehouses")),
            "raw_keys": sorted(str(key) for key in row.keys()),
        },
    }


def _flatten_acceptance_option_rows(rows: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    nested_keys = ("warehouses", "dates", "dateList", "acceptanceDates", "acceptance_dates", "options")
    for row in rows:
        nested_value: Any = None
        nested_key = ""
        for key in nested_keys:
            if isinstance(row.get(key), list):
                nested_value = row.get(key)
                nested_key = key
                break
        if not isinstance(nested_value, list):
            result.append(row)
            continue
        barcode = _first_string(row, "barcode", "Barcode")
        for item in nested_value:
            merged = dict(row)
            for key in nested_keys:
                merged.pop(key, None)
            if isinstance(item, Mapping):
                merged.update(dict(item))
            else:
                merged["date"] = str(item or "").strip()
            if barcode and not _first_string(merged, "barcode", "Barcode"):
                merged["barcode"] = barcode
            if nested_key == "warehouses":
                merged["_barcode_row_had_warehouses"] = True
            result.append(merged)
    return result


def _dedupe_acceptance_option_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    barcode_values = {str(row.get("barcode") or "").strip() for row in rows if str(row.get("barcode") or "").strip()}
    required_barcode_count = max(1, len(barcode_values))
    grouped: dict[tuple[str, str, str, str, str, str, str, str], dict[str, Any]] = {}
    grouped_barcodes: dict[tuple[str, str, str, str, str, str, str, str], set[str]] = {}
    for row in rows:
        key = (
            _warehouse_key(row.get("warehouse_id")),
            _normalize_name(row.get("warehouse_name")),
            str(row.get("date") or ""),
            str(row.get("route_type") or ROUTE_DIRECT),
            _warehouse_key(row.get("transit_warehouse_id")),
            _normalize_name(row.get("transit_warehouse_name")),
            str(row.get("barcode") or "").strip(),
            str(dict(row.get("raw_flags") or {}).get("canBox")),
        )
        if key not in grouped:
            grouped[key] = dict(row)
            grouped_barcodes[key] = set()
        barcode = str(row.get("barcode") or "").strip()
        if barcode:
            grouped_barcodes[key].add(barcode)
    for key, row in grouped.items():
        row["accepted_barcodes"] = sorted(grouped_barcodes.get(key) or set())
        row.setdefault("evidence", {})
        if isinstance(row.get("evidence"), dict):
            row["evidence"] = {
                **dict(row.get("evidence") or {}),
                "available_barcode_count": len(grouped_barcodes.get(key) or set()),
                "required_success_barcode_count": required_barcode_count,
            }
    return list(grouped.values())


def _group_acceptance_rows_by_warehouse(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = _warehouse_group_key(row)
        if not key:
            continue
        group = grouped.setdefault(
            key,
            {
                "group_key": key,
                "warehouse_id": row.get("warehouse_id") or "",
                "warehouse_name": row.get("warehouse_name") or "",
                "route_type": row.get("route_type") or ROUTE_DIRECT,
                "transit_warehouse_id": row.get("transit_warehouse_id") or "",
                "transit_warehouse_name": row.get("transit_warehouse_name") or "",
                "dropoff_allowed": row.get("dropoff_allowed"),
                "pickup_allowed": row.get("pickup_allowed"),
                "raw_flags": {},
                "raw_rows": [],
                "accepted_barcodes": set(),
                "raw_row_count": 0,
            },
        )
        if not group.get("warehouse_name") and row.get("warehouse_name"):
            group["warehouse_name"] = row.get("warehouse_name") or ""
        if not group.get("warehouse_id") and row.get("warehouse_id"):
            group["warehouse_id"] = row.get("warehouse_id") or ""
        if row.get("route_type") == ROUTE_DIRECT:
            group["route_type"] = ROUTE_DIRECT
        elif group.get("route_type") != ROUTE_DIRECT and row.get("route_type") == ROUTE_TRANSIT:
            group["route_type"] = ROUTE_TRANSIT
        if not group.get("transit_warehouse_name") and row.get("transit_warehouse_name"):
            group["transit_warehouse_name"] = row.get("transit_warehouse_name") or ""
        if not group.get("transit_warehouse_id") and row.get("transit_warehouse_id"):
            group["transit_warehouse_id"] = row.get("transit_warehouse_id") or ""
        for flag_key, flag_value in dict(row.get("raw_flags") or {}).items():
            group["raw_flags"].setdefault(flag_key, flag_value)
        row_barcodes = list(row.get("accepted_barcodes") or [])
        barcode = str(row.get("barcode") or "").strip()
        if barcode:
            row_barcodes.append(barcode)
        for item in row_barcodes:
            normalized_barcode = str(item or "").strip()
            if normalized_barcode:
                group["accepted_barcodes"].add(normalized_barcode)
        group["raw_rows"].append(row)
        group["raw_row_count"] += 1
    return list(grouped.values())


def _warehouse_group_key(row: Mapping[str, Any]) -> str:
    warehouse_id = _warehouse_key(row.get("warehouse_id"))
    if warehouse_id:
        return warehouse_id
    name = _normalize_name(row.get("warehouse_name"))
    return f"name:{name}" if name else ""


def _barcode_coverage(group: Mapping[str, Any], product_barcodes: set[str]) -> dict[str, Any]:
    accepted = {
        str(item or "").strip()
        for item in (group.get("accepted_barcodes") or set())
        if str(item or "").strip()
    }
    missing = sorted(product_barcodes - accepted)
    return {
        "accepted_count": len(accepted),
        "total_count": len(product_barcodes),
        "missing_count": len(missing),
        "accepts_all_barcodes": bool(product_barcodes) and len(accepted) >= len(product_barcodes),
        "partial": bool(product_barcodes) and 0 < len(accepted) < len(product_barcodes),
        "accepted_barcode_count": len(accepted),
        "required_barcode_count": len(product_barcodes),
        "missing_barcodes_masked": [_masked_barcode(item) for item in missing[:10]],
        "source": "acceptance_options.result[].warehouses[]",
    }


def _warehouse_acceptance_dates(
    group: Mapping[str, Any],
    coefficients: Mapping[str, list[dict[str, Any]]],
    *,
    date_filter: str,
) -> list[dict[str, Any]]:
    matches = coefficients.get(_warehouse_key(group.get("warehouse_id"))) or coefficients.get(
        _normalize_name(group.get("warehouse_name"))
    ) or []
    rows = matches or list(group.get("raw_rows") or [])
    candidates_by_date: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        raw_flags = dict(row.get("raw_flags") or {}) if isinstance(row, Mapping) else {}
        box_type_id = _positive_int(raw_flags.get("boxTypeID", row.get("boxTypeID")))
        if box_type_id not in BOX_TYPE_IDS:
            continue
        date_value = _first_string(row, "date", "dt", "day", "supplyDate", "acceptanceDate", "unloadingDate", "availableDate")
        normalized_date = _normalize_date_value(date_value)
        if not normalized_date:
            continue
        if date_filter and not _date_matches_filter(normalized_date or date_value, date_filter):
            continue
        coefficient = row.get("coefficient")
        if coefficient is None:
            coefficient = _first_number(row, "coefficient", "coef", "acceptanceCoefficient", "acceptance_coefficient")
        allow_unload = row.get("allow_unload")
        if allow_unload is None and isinstance(row, Mapping):
            allow_unload = _first_bool(row, "allowUnload", "allow_unload", "canUnload", "can_unload")
        coefficient_value = coefficient if isinstance(coefficient, (int, float)) else _first_nested_number(coefficient)
        is_available = bool(allow_unload is True and coefficient_value in (0, 1))
        candidates_by_date.setdefault(normalized_date, []).append(
            {
                "date": normalized_date,
                "raw_date": date_value,
                "coefficient": coefficient_value,
                "coefficient_display": _format_number(coefficient_value),
                "allow_unload": allow_unload,
                "box_type_id": box_type_id,
                "package_type": PACKAGE_TYPE_BOX,
                "is_available": is_available,
                "is_free_date": bool(is_available and coefficient_value == 0),
                "is_good_date": is_available,
                "is_paid_date": bool(is_available and coefficient_value == 1),
                "status": _date_status(coefficient_value, allow_unload),
                "source": "acceptance_coefficients" if matches else "acceptance_options",
            }
        )
    result: list[dict[str, Any]] = []
    for normalized_date in sorted(candidates_by_date):
        day_rows = candidates_by_date[normalized_date]
        day_rows.sort(
            key=lambda item: (
                0 if item.get("is_available") else 1,
                _coefficient_sort_rank(item.get("coefficient")),
                int(item.get("box_type_id") or 999),
            )
        )
        selected = dict(day_rows[0])
        selected["box_type_ids"] = sorted(
            {int(item.get("box_type_id")) for item in day_rows if item.get("box_type_id")}
        )
        selected["raw_row_count"] = len(day_rows)
        result.append(selected)
    return result


def _best_date_entry(dates: list[Mapping[str, Any]]) -> dict[str, Any]:
    if not dates:
        return {}
    available = [item for item in dates if item.get("is_available")]
    return dict((available or dates)[0])


def _date_sort_tuple(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(row.get("date") or "9999-99-99"),
        0 if row.get("is_available") else 1,
        _coefficient_sort_rank(row.get("coefficient")),
    )


def _date_status(coefficient: Any, allow_unload: Any) -> str:
    if allow_unload is not True or coefficient not in (0, 1):
        return "unavailable"
    if coefficient == 0:
        return "free"
    if coefficient == 1:
        return "paid"
    return "unavailable"


def _normalize_date_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    return match.group(1) if match else text


def _date_matches_filter(value: Any, date_filter: str) -> bool:
    expected = _normalize_date_value(date_filter)
    actual = _normalize_date_value(value)
    return bool(expected and actual == expected)


def _best_numeric_coefficient(dates: list[Mapping[str, Any]]) -> float | None:
    for row in dates:
        value = row.get("coefficient")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return float(value)
    for row in dates:
        value = row.get("coefficient")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _coefficient_sort_rank(value: Any) -> tuple[int, float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        if numeric == 0:
            return (0, 0.0)
        if numeric == 1:
            return (1, 1.0)
        if numeric > 1:
            return (2, numeric)
        return (5, abs(numeric))
    return (4, 999_999.0)


def _is_sgt_warehouse(value: Any) -> bool:
    return "сгт" in _normalize_name(value).split()


def _is_major_expected_warehouse(district_key: str, warehouse_name: Any) -> bool:
    return any(_warehouse_name_matches(warehouse_name, expected) for expected in MAJOR_WAREHOUSE_PROBES_BY_DISTRICT.get(district_key, []))


def _warehouse_name_matches(candidate: Any, expected: Any) -> bool:
    candidate_name = normalize_exact_warehouse_name(candidate)
    expected_name = normalize_exact_warehouse_name(expected)
    return bool(candidate_name and expected_name and candidate_name == expected_name)


def _warehouse_name_by_id(rows: list[Mapping[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in rows:
        warehouse_id = _warehouse_key(_first_string(row, "warehouseID", "warehouseId", "warehouse_id", "ID", "id"))
        warehouse_name = _first_string(row, "warehouseName", "warehouse_name", "name")
        if warehouse_id and warehouse_name and warehouse_id not in result:
            result[warehouse_id] = warehouse_name
    return result


def _fill_acceptance_warehouse_name(row: Mapping[str, Any], warehouse_name_by_id: Mapping[str, str]) -> dict[str, Any]:
    result = dict(row)
    if not result.get("warehouse_name"):
        warehouse_name = warehouse_name_by_id.get(_warehouse_key(result.get("warehouse_id")))
        if warehouse_name:
            result["warehouse_name"] = warehouse_name
            result["evidence"] = {
                **dict(result.get("evidence") or {}),
                "warehouse_name_source": "warehouses_by_id",
            }
    return result


def _coefficient_rows_by_warehouse(rows: list[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        normalized = _normalize_acceptance_option_row(row)
        if not normalized.get("date"):
            normalized["date"] = _first_string(row, "date", "dt", "day")
        for key in (_warehouse_key(normalized.get("warehouse_id")), _normalize_name(normalized.get("warehouse_name"))):
            if key:
                result.setdefault(key, []).append(normalized)
    return result


def _expand_with_coefficients(
    base_rows: list[dict[str, Any]],
    coefficients: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for row in base_rows:
        matches = coefficients.get(_warehouse_key(row.get("warehouse_id"))) or coefficients.get(
            _normalize_name(row.get("warehouse_name"))
        ) or []
        if not matches:
            expanded.append(row)
            continue
        for coeff in matches:
            merged = dict(row)
            merged["date"] = row.get("date") or coeff.get("date") or ""
            if merged.get("coefficient") is None:
                merged["coefficient"] = coeff.get("coefficient")
            if merged.get("allow_unload") is None:
                merged["allow_unload"] = coeff.get("allow_unload")
            merged["evidence"] = {
                **dict(merged.get("evidence") or {}),
                "coefficient_source": "acceptance_coefficients",
            }
            expanded.append(merged)
    return expanded


def _tariff_by_warehouse_name(rows: list[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        name = _normalize_name(_first_string(row, "warehouseName", "warehouse_name", "name"))
        if name and name not in result:
            result[name] = row
    return result


def _match_transit_tariff(option: Mapping[str, Any], rows: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    if option.get("route_type") != ROUTE_TRANSIT:
        return None
    target = _normalize_name(option.get("warehouse_name"))
    transit = _normalize_name(option.get("transit_warehouse_name"))
    for row in rows:
        destination = _normalize_name(
            _first_string(row, "destinationWarehouseName", "warehouseName", "warehouse_name", "toWarehouseName")
        )
        source = _normalize_name(_first_string(row, "transitWarehouseName", "fromWarehouseName", "warehouseFromName"))
        if target and destination and target == destination and (not transit or not source or transit == source):
            return row
    return None


def _transit_tariffs_for_destination(warehouse_name: Any, rows: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    target = _normalize_name(warehouse_name)
    if not target:
        return []
    result: list[Mapping[str, Any]] = []
    for row in rows:
        destination = _normalize_name(
            _first_string(row, "destinationWarehouseName", "warehouseName", "warehouse_name", "toWarehouseName")
        )
        if destination and (destination == target or _warehouse_name_matches(destination, target)):
            result.append(row)
    result.sort(key=lambda item: (_first_nested_number(item.get("boxTariff")) or 999_999.0, _first_nested_number(item.get("palletTariff")) or 999_999.0))
    return result


def _best_transit_tariff(rows: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    return rows[0] if rows else None


def _acceptance_request_diagnostics(*, product_count: int, warehouse_id: str | int | None) -> dict[str, Any]:
    return {
        "endpoint": "POST /api/v1/acceptance/options",
        "read_only": True,
        "request_shape": "json_array",
        "body_schema": "[{barcode, quantity}]",
        "product_count": max(0, int(product_count or 0)),
        "warehouse_id": str(warehouse_id or "").strip(),
        "warehouse_id_location": "query_parameter",
    }


def _acceptance_payload_diagnostics(
    payload: Mapping[str, Any],
    products: list[Mapping[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    product_by_barcode = {
        str(item.get("barcode") or "").strip(): item
        for item in products
        if str(item.get("barcode") or "").strip()
    }
    warnings: list[str] = []
    blockers: list[dict[str, Any]] = []
    for row in _extract_acceptance_option_rows(payload):
        messages = _row_error_messages(row)
        if not messages:
            continue
        barcode = _first_string(row, "barcode", "Barcode")
        product = product_by_barcode.get(barcode, {})
        nm_id = _positive_int(product.get("nm_id"))
        sku_label = str(product.get("sku_label") or "").strip()
        detail = "; ".join(messages)[:260]
        subject = f"SKU {nm_id}" if nm_id > 0 else "barcode " + _masked_barcode(barcode)
        if sku_label:
            subject += f" ({sku_label})"
        message = f"WB acceptance/options вернул предупреждение по {subject}: {detail}."
        warnings.append(message)
        blockers.append(
            {
                "code": "acceptance_options_barcode_error",
                "nm_id": nm_id,
                "sku_label": sku_label,
                "barcode_masked": _masked_barcode(barcode),
                "message": message,
            }
        )
    return warnings, blockers


def _row_error_messages(row: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    for key in ("errors", "error", "errorText", "message", "messages", "warnings", "warning"):
        if key not in row:
            continue
        result.extend(_error_value_messages(row.get(key)))
    deduped: list[str] = []
    seen: set[str] = set()
    for item in result:
        normalized = _safe_plain_text(item, limit=220)
        if normalized and normalized not in seen and normalized.casefold() not in {"false", "none", "null", "0"}:
            seen.add(normalized)
            deduped.append(normalized)
    return deduped


def _error_value_messages(value: Any) -> list[str]:
    if value in (None, "", False, 0):
        return []
    if value is True:
        return ["upstream row error"]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_error_value_messages(item))
        return result
    if isinstance(value, Mapping):
        for key in ("message", "errorText", "error", "description", "detail", "text"):
            if key in value:
                nested = _error_value_messages(value.get(key))
                if nested:
                    return nested
        return [_safe_plain_text(json.dumps(dict(value), ensure_ascii=False, sort_keys=True), limit=220)]
    return [_safe_plain_text(value, limit=220)]


def _masked_barcode(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 4:
        return "***" + text
    return "***" + text[-4:]


def _known_tariff_value(box_tariff: Mapping[str, Any] | None, transit_tariff: Mapping[str, Any] | None) -> float | None:
    values: list[float] = []
    for row in (box_tariff, transit_tariff):
        if not isinstance(row, Mapping):
            continue
        for key in (
            "boxDeliveryBase",
            "boxDeliveryLiter",
            "boxStorageBase",
            "boxTariff",
            "palletTariff",
            "tariff",
            "value",
        ):
            value = _first_nested_number(row.get(key))
            if value is not None:
                values.append(value)
    return sum(values) if values else None


def _tariff_evidence(box_tariff: Mapping[str, Any] | None, transit_tariff: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "box": _compact_box_tariff_row(box_tariff),
        "transit": _compact_transit_tariff_row(transit_tariff),
        "cost_is_estimate": True,
        "note": "Raw upstream tariff evidence only; full WB acceptance/transit cost is not calculated.",
    }


def _compact_tariff_row(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(row, Mapping):
        return None
    if any(key in row for key in ("destinationWarehouseName", "transitWarehouseName", "boxTariff", "palletTariff")):
        return _compact_transit_tariff_row(row)
    return _compact_box_tariff_row(row)


def _compact_box_tariff_row(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(row, Mapping):
        return None
    delivery_coef = _first_string(row, "boxDeliveryCoefExpr", "boxDeliveryMarketplaceCoefExpr")
    storage_coef = _first_string(row, "boxStorageCoefExpr")
    return {
        "warehouseName": _first_string(row, "warehouseName", "warehouse_name", "name", "destinationWarehouseName"),
        "geoName": _first_string(row, "geoName", "geo_name", "federalDistrict"),
        "activeFrom": _first_string(row, "activeFrom", "date", "dt"),
        "boxDeliveryBase": _first_string(row, "boxDeliveryBase"),
        "boxDeliveryLiter": _first_string(row, "boxDeliveryLiter"),
        "boxDeliveryCoefExpr": delivery_coef,
        "boxDeliveryMarketplaceBase": _first_string(row, "boxDeliveryMarketplaceBase"),
        "boxDeliveryMarketplaceLiter": _first_string(row, "boxDeliveryMarketplaceLiter"),
        "boxDeliveryMarketplaceCoefExpr": _first_string(row, "boxDeliveryMarketplaceCoefExpr"),
        "boxStorageBase": _first_string(row, "boxStorageBase"),
        "boxStorageLiter": _first_string(row, "boxStorageLiter"),
        "boxStorageCoefExpr": storage_coef,
        "logistics_percent": _first_nested_number(delivery_coef),
        "storage_percent": _first_nested_number(storage_coef),
        "delivery_base_value": _first_nested_number(row.get("boxDeliveryBase")),
        "delivery_liter_value": _first_nested_number(row.get("boxDeliveryLiter")),
        "storage_base_value": _first_nested_number(row.get("boxStorageBase")),
        "storage_liter_value": _first_nested_number(row.get("boxStorageLiter")),
        "logistics_display": delivery_coef or "нет tariff evidence",
        "storage_display": storage_coef or "нет tariff evidence",
        "known_value": _first_nested_number(row),
        "raw_keys": sorted(str(key) for key in row.keys()),
    }


def _compact_transit_tariff_row(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(row, Mapping):
        return None
    intervals: list[dict[str, Any]] = []
    for item in row.get("boxTariff") or []:
        if isinstance(item, Mapping):
            intervals.append(
                {
                    "from": item.get("from"),
                    "to": item.get("to"),
                    "value": item.get("value"),
                    "value_numeric": _first_nested_number(item.get("value")),
                }
            )
    best_box_value = None
    for item in intervals:
        value = item.get("value_numeric")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            best_box_value = float(value) if best_box_value is None else min(best_box_value, float(value))
    return {
        "transitWarehouseName": _first_string(row, "transitWarehouseName", "fromWarehouseName", "warehouseFromName"),
        "destinationWarehouseName": _first_string(row, "destinationWarehouseName", "warehouseName", "warehouse_name", "toWarehouseName"),
        "activeFrom": _first_string(row, "activeFrom", "date", "dt"),
        "boxTariff": intervals,
        "best_box_tariff_value": best_box_value,
        "palletTariff": row.get("palletTariff"),
        "pallet_tariff_value": _first_nested_number(row.get("palletTariff")),
        "known_value": _first_nested_number(row),
        "raw_keys": sorted(str(key) for key in row.keys()),
    }


def _group_option_warnings(
    *,
    group: Mapping[str, Any],
    dates: list[Mapping[str, Any]],
    warehouse_scope: str,
    box_tariff: Mapping[str, Any] | None,
    transit_routes: list[Mapping[str, Any]],
    barcode_coverage: Mapping[str, Any],
) -> list[str]:
    warnings: list[str] = []
    if warehouse_scope == WAREHOUSE_SCOPE_OUTSIDE_DISTRICT:
        warnings.append("Склад вне выбранного расчётного округа.")
    if warehouse_scope == WAREHOUSE_SCOPE_UNMAPPED:
        warnings.append("Склад не сопоставлен с расчётным округом.")
    if barcode_coverage.get("partial"):
        warnings.append(
            f"Склад принимает часть ШК: {barcode_coverage.get('accepted_count')}/{barcode_coverage.get('total_count')}."
        )
    if not barcode_coverage.get("accepts_all_barcodes"):
        warnings.append("Склад не подтвердил приёмку всех barcode из партии.")
    if not dates:
        warnings.append("Нет date/coefficient evidence.")
    elif not any(item.get("is_good_date") for item in dates):
        warnings.append("Нет хороших дат с coefficient 0/1 и allowUnload=true.")
    if dates and dates[0].get("coefficient") == -1:
        warnings.append("Лучший доступный coefficient=-1; считаем дату проблемной/недоступной.")
    if dates and dates[0].get("allow_unload") is False:
        warnings.append("WB вернул allowUnload=false для лучшей даты.")
    if not isinstance(box_tariff, Mapping):
        warnings.append("Нет box tariff evidence.")
    if not transit_routes:
        warnings.append("Нет transit tariff evidence.")
    if group.get("route_type") == ROUTE_TRANSIT and not transit_routes:
        warnings.append("Acceptance option выглядит транзитным, но transit tariff evidence не найден.")
    return warnings


def _rank_tuple(option: Mapping[str, Any]) -> tuple[Any, ...]:
    scope_rank = {
        WAREHOUSE_SCOPE_SAME_DISTRICT: 0,
        WAREHOUSE_SCOPE_OUTSIDE_DISTRICT: 1,
        WAREHOUSE_SCOPE_UNMAPPED: 2,
    }.get(str(option.get("warehouse_scope") or ""), 3)
    is_sgt = bool(option.get("is_sgt"))
    warehouse_kind_rank = 0 if option.get("is_major_expected") and not is_sgt else 2 if is_sgt else 1
    coverage_rank = 0 if option.get("accepts_all_barcodes") else 1
    coefficient_rank = _coefficient_sort_rank(option.get("coefficient"))
    allow_rank = 0 if option.get("allow_unload") is True else 1 if option.get("allow_unload") is None else 2
    route_rank = 1 if option.get("route_type") == ROUTE_TRANSIT else 0
    box_tariff = option.get("box_tariff") if isinstance(option.get("box_tariff"), Mapping) else {}
    logistics_rank = _first_nested_number((box_tariff or {}).get("boxDeliveryCoefExpr"))
    storage_rank = _first_nested_number((box_tariff or {}).get("boxStorageCoefExpr"))
    tariff_rank = float(option.get("known_tariff_value")) if isinstance(option.get("known_tariff_value"), (int, float)) else 999_999.0
    date_rank = str(option.get("date") or "9999-99-99")
    missing_evidence_rank = len(option.get("warnings") or [])
    return (
        scope_rank,
        warehouse_kind_rank,
        coverage_rank,
        coefficient_rank,
        allow_rank,
        logistics_rank if logistics_rank is not None else 999_999.0,
        storage_rank if storage_rank is not None else 999_999.0,
        route_rank,
        tariff_rank,
        date_rank,
        missing_evidence_rank,
    )


def _recommendation_explanation(option: Mapping[str, Any]) -> str:
    parts: list[str] = []
    scope = str(option.get("warehouse_scope") or "")
    if scope == WAREHOUSE_SCOPE_SAME_DISTRICT:
        parts.append("склад внутри выбранного округа")
    elif scope == WAREHOUSE_SCOPE_OUTSIDE_DISTRICT:
        parts.append("склад вне выбранного округа")
    else:
        parts.append("округ склада не сопоставлен")
    coverage = option.get("barcode_coverage") if isinstance(option.get("barcode_coverage"), Mapping) else {}
    if option.get("accepts_all_barcodes"):
        parts.append(
            f"принимает все ШК ({coverage.get('accepted_count', 0)}/{coverage.get('total_count', 0)})"
        )
    elif coverage:
        parts.append(
            f"принимает часть ШК ({coverage.get('accepted_count', 0)}/{coverage.get('total_count', 0)})"
        )
    if option.get("is_major_expected"):
        parts.append("ключевой склад округа")
    elif option.get("is_sgt"):
        parts.append("СГТ")
    if option.get("allow_unload") is True:
        parts.append("allowUnload=true")
    elif option.get("allow_unload") is False:
        parts.append("allowUnload=false")
    else:
        parts.append("allowUnload отсутствует")
    if option.get("coefficient") is not None:
        parts.append("коэффициент " + _format_number(option.get("coefficient")))
    else:
        parts.append("коэффициент не получен")
    parts.append("прямой маршрут" if option.get("route_type") == ROUTE_DIRECT else "транзитный маршрут")
    if option.get("known_tariff_value") is not None:
        parts.append("есть raw tariff evidence")
    else:
        parts.append("tariff evidence неполная")
    box_tariff = option.get("box_tariff") if isinstance(option.get("box_tariff"), Mapping) else {}
    if box_tariff and box_tariff.get("logistics_display"):
        parts.append("логистика " + str(box_tariff.get("logistics_display")))
    if box_tariff and box_tariff.get("storage_display"):
        parts.append("хранение " + str(box_tariff.get("storage_display")))
    if option.get("transit_route_count"):
        parts.append(f"транзитных маршрутов: {option.get('transit_route_count')}")
    if option.get("date"):
        parts.append("дата " + str(option.get("date")))
    return "; ".join(parts) + "."


def _operator_handoff(
    *,
    option: Mapping[str, Any],
    district_key: str,
    products: list[Mapping[str, Any]],
    acceptance_payload: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "copy_format": "json",
        "district_key": district_key,
        "district_name_ru": SUPPLY_PLANNING_ZONE_LABELS_RU.get(district_key, ""),
        "planning_zone_key": district_key,
        "planning_zone_label": SUPPLY_PLANNING_ZONE_LABELS_RU.get(district_key, ""),
        "date": option.get("date") or "",
        "warehouse_id": option.get("warehouse_id") or "",
        "warehouse_name": option.get("warehouse_name") or "",
        "route_type": option.get("route_type") or ROUTE_DIRECT,
        "transit_warehouse_id": option.get("transit_warehouse_id") or "",
        "transit_warehouse_name": option.get("transit_warehouse_name") or "",
        "products": [
            {
                "nm_id": item.get("nm_id"),
                "sku_label": item.get("sku_label") or "",
                "barcode": item.get("barcode") or "",
                "quantity": item.get("quantity"),
            }
            for item in products
        ],
        "warnings": list(option.get("warnings") or []),
        "evidence": {
            "source": CONTRACT_NAME,
            "wb_acceptance_options_seen": bool(acceptance_payload),
            "no_wb_mutation": True,
        },
    }


def _warehouse_scope(warehouse_district_key: str, selected_district_key: str) -> str:
    if warehouse_district_key == selected_district_key:
        return WAREHOUSE_SCOPE_SAME_DISTRICT
    if warehouse_district_key in DISTRICT_KEYS:
        return WAREHOUSE_SCOPE_OUTSIDE_DISTRICT
    return WAREHOUSE_SCOPE_UNMAPPED


def _safe_fetch_list(fetcher: Any, *, warnings: list[str], label: str, warning_ru: str) -> list[Mapping[str, Any]]:
    try:
        rows = fetcher()
    except (OfficialApiRuntimeError, WbSuppliesHttpStatusError, WbSuppliesTransportError, OSError) as exc:
        warnings.append(warning_ru + " " + _safe_error_message(exc))
        return []
    if not isinstance(rows, list):
        warnings.append(f"{label}: upstream вернул неподдерживаемую форму ответа.")
        return []
    return [item for item in rows if isinstance(item, Mapping)]


def _upstream_error_payload(
    exc: Exception,
    *,
    request_diagnostics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    message = _safe_error_message(exc)
    diagnostics = dict(request_diagnostics or {})
    if isinstance(exc, WbSuppliesHttpStatusError):
        diagnostics.update(
            {
                "http_status": exc.status_code,
                "content_type": exc.content_type,
                "wb_body_prefix": exc.body_prefix,
            }
        )
    elif isinstance(exc, WbSuppliesTransportError):
        if exc.status_code is not None:
            diagnostics["http_status"] = exc.status_code
        if exc.content_type:
            diagnostics["content_type"] = exc.content_type
        if exc.body_prefix:
            diagnostics["wb_body_prefix"] = exc.body_prefix
    if isinstance(exc, OfficialApiRuntimeError) and "WB_API_TOKEN" in str(exc):
        return {
            "code": "token_missing",
            "message": "WB_API_TOKEN не настроен для read-only WB planning request.",
            "diagnostics": diagnostics,
        }
    if isinstance(exc, WbSuppliesHttpStatusError):
        if exc.status_code in {401, 403}:
            return {
                "code": "token_permission_error",
                "message": "WB API token не имеет нужных прав или недействителен для acceptance/options.",
                "diagnostics": diagnostics,
            }
        if exc.status_code == 429:
            return {
                "code": "rate_limited",
                "message": "WB API вернул rate limit для acceptance/options.",
                "diagnostics": diagnostics,
            }
        return {
            "code": f"wb_http_{exc.status_code}",
            "message": f"WB API вернул HTTP {exc.status_code} для read-only planning request.",
            "diagnostics": diagnostics,
        }
    return {"code": "upstream_error", "message": message, "diagnostics": diagnostics}


def _safe_error_message(exc: Exception) -> str:
    text = _safe_plain_text(str(exc), limit=420)
    text = re.sub(
        r"(?i)(token|authorization|api[-_ ]?key)([\"']?\s*[:=]\s*[\"']?)[^\"'\s;,&}]+",
        r"\1\2***",
        text,
    )
    return text.replace("\n", " ").replace("\r", " ")[:420]


def _safe_plain_text(value: Any, *, limit: int = 420) -> str:
    text = str(value or "").replace("\x00", "")
    text = re.sub(
        r"(?i)([\"']?(?:token|authorization|api[-_ ]?key|cookie|password|secret)[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^,\s}]+)",
        r'\1"***"',
        text,
    )
    text = re.sub(r"\b\d{8,}\b", lambda match: "***" + match.group(0)[-4:], text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _first_string(mapping: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _first_bool(mapping: Mapping[str, Any], *keys: str) -> bool | None:
    for key in keys:
        if key in mapping:
            return _as_bool(mapping.get(key), default=None)
    return None


def _first_number(mapping: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _first_nested_number(mapping.get(key))
        if value is not None:
            return value
    return None


def _first_nested_number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        normalized = value.replace(" ", "").replace("\u00a0", "").replace(",", ".").replace("%", "")
        match = re.search(r"-?\d+(?:\.\d+)?", normalized)
        if match:
            normalized = match.group(0)
        try:
            return float(normalized)
        except ValueError:
            return None
    if isinstance(value, Mapping):
        return _first_number(value, "value", "amount", "price", "tariff", "from", "base")
    if isinstance(value, list):
        for item in value:
            nested = _first_nested_number(item)
            if nested is not None:
                return nested
    return None


def _as_bool(value: Any, *, default: bool | None = False) -> bool | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "да"}:
        return True
    if normalized in {"0", "false", "no", "нет"}:
        return False
    return default


def _positive_int(value: Any) -> int:
    try:
        return max(int(float(str(value).replace(",", ".").strip())), 0)
    except (TypeError, ValueError):
        return 0


def _warehouse_key(value: Any) -> str:
    normalized = str(value or "").strip()
    return f"id:{normalized}" if normalized else ""


def _normalize_name(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().replace("ё", "е").split())


def _format_number(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    if float(value).is_integer():
        return str(int(value))
    return f"{float(value):.2f}".rstrip("0").rstrip(".")


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _default_timestamp_factory() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _default_now_factory() -> datetime:
    return datetime.now(timezone.utc)
