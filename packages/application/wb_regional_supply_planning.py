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
from packages.application.wb_supply_overlay import (
    DISTRICT_SHORT_LABELS_RU,
    DISTRICT_UNMAPPED,
    augment_supply_row_with_district,
    build_warehouse_district_mapping,
)
from packages.business_time import current_business_date_iso
from packages.contracts.wb_regional_supply import DISTRICT_KEYS, DISTRICT_LABELS_RU
from packages.contracts.wb_regional_supply_planning import (
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


MAX_PLANNING_OPTIONS = 300


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
        if district_key not in DISTRICT_KEYS:
            raise ValueError(f"Неизвестный расчётный округ: {district_key}")

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
                "district_name_ru": str(district.get("district_name_ru") or DISTRICT_LABELS_RU[district_key]),
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
            "district_name_ru": str(district.get("district_name_ru") or DISTRICT_LABELS_RU[district_key]),
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
        if not raw_option_rows:
            return {
                **payload_without_options,
                "status": STATUS_NO_OPTIONS,
                "blockers": acceptance_blockers,
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

        options = _build_options(
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
        )
        status = STATUS_READY if options else STATUS_NO_OPTIONS
        if not options:
            warnings.append("После фильтров не осталось доступных вариантов WB.")
        return {
            **payload_without_options,
            "status": status,
            "options": options,
            "warnings": warnings,
            "blockers": [] if options else acceptance_blockers,
            "summary": {
                **payload_without_options["summary"],
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
            "district_name_ru": DISTRICT_LABELS_RU.get(district_key, ""),
            "package_type": str(request.get("package_type") or PACKAGE_TYPE_BOX),
            "filters": {
                "warehouse_id": request.get("warehouse_id") or "",
                "date": request.get("date") or "",
                "only_same_district": bool(request.get("only_same_district")),
                "include_transit": bool(request.get("include_transit")),
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
) -> list[dict[str, Any]]:
    base_rows = [_normalize_acceptance_option_row(row) for row in _flatten_acceptance_option_rows(raw_option_rows)]
    base_rows = [row for row in base_rows if row.get("warehouse_id") or row.get("warehouse_name")]
    base_rows = _dedupe_acceptance_option_rows(base_rows)
    warehouse_name_by_id = _warehouse_name_by_id(enrichment.get("warehouses") or [])
    base_rows = [_fill_acceptance_warehouse_name(row, warehouse_name_by_id) for row in base_rows]
    if not include_transit:
        base_rows = [row for row in base_rows if row.get("route_type") != ROUTE_TRANSIT]
    coefficients = _coefficient_rows_by_warehouse(enrichment.get("coefficients") or [])
    expanded_rows = _expand_with_coefficients(base_rows, coefficients)
    if date_filter:
        expanded_rows = [row for row in expanded_rows if str(row.get("date") or "") == date_filter]

    fake_supply_rows = [
        {
            "warehouse_id": row.get("warehouse_id") or "",
            "warehouse_name": row.get("warehouse_name") or "",
        }
        for row in expanded_rows
    ]
    mapping = build_warehouse_district_mapping(
        warehouse_rows=list(enrichment.get("warehouses") or []),
        supply_rows=fake_supply_rows,
        office_rows=list(enrichment.get("offices") or []),
        tariff_rows=list(enrichment.get("box_tariffs") or []),
    )
    box_tariff_by_name = _tariff_by_warehouse_name(enrichment.get("box_tariffs") or [])
    transit_tariff_rows = list(enrichment.get("transit_tariffs") or [])
    options: list[dict[str, Any]] = []
    for row in expanded_rows:
        district_row = augment_supply_row_with_district(
            {
                "warehouse_id": row.get("warehouse_id") or "",
                "warehouse_name": row.get("warehouse_name") or "",
                "district_source_warehouse_id": row.get("warehouse_id") or "",
                "district_source_warehouse_name": row.get("warehouse_name") or "",
                "district_source_warehouse_role": "acceptance_option",
                "district_source_warehouse_evidence": "acceptance_options.warehouseName",
            },
            mapping,
        )
        warehouse_district_key = str(district_row.get("district_key") or DISTRICT_UNMAPPED)
        warehouse_scope = _warehouse_scope(warehouse_district_key, district_key)
        if only_same_district and warehouse_scope != WAREHOUSE_SCOPE_SAME_DISTRICT:
            continue
        box_tariff = box_tariff_by_name.get(_normalize_name(row.get("warehouse_name")))
        transit_tariff = _match_transit_tariff(row, transit_tariff_rows)
        tariff_value = _known_tariff_value(box_tariff, transit_tariff)
        option = {
            "option_id": _stable_hash(
                {
                    "warehouse_id": row.get("warehouse_id"),
                    "warehouse_name": row.get("warehouse_name"),
                    "date": row.get("date"),
                    "transit_warehouse_id": row.get("transit_warehouse_id"),
                    "transit_warehouse_name": row.get("transit_warehouse_name"),
                }
            )[:16],
            "rank": 0,
            "recommendation": "",
            "recommendation_explanation": "",
            "date": row.get("date") or "",
            "warehouse_id": row.get("warehouse_id") or "",
            "warehouse_name": row.get("warehouse_name") or "",
            "warehouse_district_key": warehouse_district_key,
            "warehouse_district_label_ru": DISTRICT_LABELS_RU.get(warehouse_district_key, ""),
            "warehouse_district_short_label_ru": DISTRICT_SHORT_LABELS_RU.get(warehouse_district_key, ""),
            "warehouse_scope": warehouse_scope,
            "route_type": row.get("route_type") or ROUTE_DIRECT,
            "transit_warehouse_id": row.get("transit_warehouse_id") or "",
            "transit_warehouse_name": row.get("transit_warehouse_name") or "",
            "coefficient": row.get("coefficient"),
            "coefficient_display": _format_number(row.get("coefficient")),
            "allow_unload": row.get("allow_unload"),
            "dropoff_allowed": row.get("dropoff_allowed"),
            "pickup_allowed": row.get("pickup_allowed"),
            "package_type": package_type,
            "raw_flags": row.get("raw_flags") or {},
            "tariff_evidence": _tariff_evidence(box_tariff, transit_tariff),
            "known_tariff_value": tariff_value,
            "warnings": _option_warnings(row, warehouse_scope, box_tariff, transit_tariff),
            "evidence": {
                "acceptance_option": row.get("evidence") or {},
                "district_mapping_source": district_row.get("district_mapping_source") or "",
                "district_mapping_evidence": district_row.get("district_mapping_evidence") or "",
                "district_mapping_confidence": district_row.get("district_mapping_confidence") or "",
                "cost_kind": "raw_tariff_evidence_only",
                "full_cost_calculated": False,
            },
        }
        option["_rank_tuple"] = _rank_tuple(option)
        option["operator_handoff"] = _operator_handoff(
            option=option,
            district_key=district_key,
            products=products,
            acceptance_payload=acceptance_payload,
        )
        options.append(option)
    options.sort(key=lambda item: item["_rank_tuple"])
    total_option_count = len(options)
    if total_option_count > MAX_PLANNING_OPTIONS:
        warnings.append(
            f"WB вернул {total_option_count} вариантов; в UI показаны первые {MAX_PLANNING_OPTIONS} по ранжированию."
        )
        options = options[:MAX_PLANNING_OPTIONS]
    for index, option in enumerate(options, start=1):
        option["rank"] = index
        option["recommendation"] = "Рекомендуемый вариант" if index == 1 else f"Вариант #{index}"
        option["recommendation_explanation"] = _recommendation_explanation(option)
        option.pop("_rank_tuple", None)
    return options


def _parse_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    package_type = str(payload.get("package_type") or PACKAGE_TYPE_BOX).strip() or PACKAGE_TYPE_BOX
    if package_type not in PACKAGE_TYPES:
        raise ValueError(f"package_type пока поддерживается только {PACKAGE_TYPE_BOX}")
    return {
        "district_key": str(payload.get("district_key") or "").strip().lower(),
        "calculation_id": str(payload.get("calculation_id") or "").strip(),
        "package_type": package_type,
        "warehouse_id": str(payload.get("warehouse_id") or "").strip(),
        "date": str(payload.get("date") or "").strip(),
        "only_same_district": _as_bool(payload.get("only_same_district"), default=False),
        "include_transit": _as_bool(payload.get("include_transit"), default=True),
    }


def _find_district(result: Mapping[str, Any], district_key: str) -> Mapping[str, Any] | None:
    for item in result.get("districts") or []:
        if isinstance(item, Mapping) and str(item.get("district_key") or "").strip().lower() == district_key:
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
    grouped: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
    grouped_barcodes: dict[tuple[str, str, str, str, str, str], set[str]] = {}
    for row in rows:
        key = (
            _warehouse_key(row.get("warehouse_id")),
            _normalize_name(row.get("warehouse_name")),
            str(row.get("date") or ""),
            str(row.get("route_type") or ROUTE_DIRECT),
            _warehouse_key(row.get("transit_warehouse_id")),
            _normalize_name(row.get("transit_warehouse_name")),
        )
        if key not in grouped:
            grouped[key] = dict(row)
            grouped_barcodes[key] = set()
        barcode = str(row.get("barcode") or "").strip()
        if barcode:
            grouped_barcodes[key].add(barcode)
    if len(barcode_values) <= 1:
        result = list(grouped.values())
    else:
        result = [
            row
            for key, row in grouped.items()
            if len(grouped_barcodes.get(key) or set()) >= required_barcode_count
        ]
        if not result:
            # If WB returned only partial barcode-level success, keep the visible partial options
            # with warnings from _acceptance_payload_diagnostics instead of hiding everything.
            result = list(grouped.values())
    for key, row in grouped.items():
        row.setdefault("evidence", {})
        if isinstance(row.get("evidence"), dict):
            row["evidence"] = {
                **dict(row.get("evidence") or {}),
                "available_barcode_count": len(grouped_barcodes.get(key) or set()),
                "required_success_barcode_count": required_barcode_count,
            }
    return result


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
        "box": _compact_tariff_row(box_tariff),
        "transit": _compact_tariff_row(transit_tariff),
        "cost_is_estimate": True,
        "note": "Raw upstream tariff evidence only; full WB acceptance/transit cost is not calculated.",
    }


def _compact_tariff_row(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(row, Mapping):
        return None
    return {
        "warehouseName": _first_string(row, "warehouseName", "warehouse_name", "name", "destinationWarehouseName"),
        "geoName": _first_string(row, "geoName", "geo_name", "federalDistrict"),
        "activeFrom": _first_string(row, "activeFrom", "date", "dt"),
        "known_value": _first_nested_number(row),
        "raw_keys": sorted(str(key) for key in row.keys()),
    }


def _option_warnings(
    row: Mapping[str, Any],
    warehouse_scope: str,
    box_tariff: Mapping[str, Any] | None,
    transit_tariff: Mapping[str, Any] | None,
) -> list[str]:
    warnings: list[str] = []
    if warehouse_scope == WAREHOUSE_SCOPE_OUTSIDE_DISTRICT:
        warnings.append("Склад вне выбранного расчётного округа.")
    if warehouse_scope == WAREHOUSE_SCOPE_UNMAPPED:
        warnings.append("Склад не сопоставлен с расчётным округом.")
    if row.get("allow_unload") is False:
        warnings.append("WB вернул allowUnload=false.")
    if row.get("coefficient") is None:
        warnings.append("Нет coefficient evidence.")
    if not isinstance(box_tariff, Mapping):
        warnings.append("Нет box tariff evidence.")
    if row.get("route_type") == ROUTE_TRANSIT and not isinstance(transit_tariff, Mapping):
        warnings.append("Нет transit tariff evidence.")
    return warnings


def _rank_tuple(option: Mapping[str, Any]) -> tuple[Any, ...]:
    scope_rank = {
        WAREHOUSE_SCOPE_SAME_DISTRICT: 0,
        WAREHOUSE_SCOPE_OUTSIDE_DISTRICT: 1,
        WAREHOUSE_SCOPE_UNMAPPED: 2,
    }.get(str(option.get("warehouse_scope") or ""), 3)
    allow_rank = 0 if option.get("allow_unload") is True else 1 if option.get("allow_unload") is None else 2
    coefficient = option.get("coefficient")
    coefficient_rank = float(coefficient) if isinstance(coefficient, (int, float)) else 999_999.0
    route_rank = 1 if option.get("route_type") == ROUTE_TRANSIT else 0
    tariff = option.get("known_tariff_value")
    tariff_rank = float(tariff) if isinstance(tariff, (int, float)) else 999_999.0
    date_rank = str(option.get("date") or "9999-99-99")
    missing_evidence_rank = len(option.get("warnings") or [])
    return (scope_rank, allow_rank, coefficient_rank, route_rank, tariff_rank, date_rank, missing_evidence_rank)


def _recommendation_explanation(option: Mapping[str, Any]) -> str:
    parts: list[str] = []
    scope = str(option.get("warehouse_scope") or "")
    if scope == WAREHOUSE_SCOPE_SAME_DISTRICT:
        parts.append("склад внутри выбранного округа")
    elif scope == WAREHOUSE_SCOPE_OUTSIDE_DISTRICT:
        parts.append("склад вне выбранного округа")
    else:
        parts.append("округ склада не сопоставлен")
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
        "district_name_ru": DISTRICT_LABELS_RU.get(district_key, ""),
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
        normalized = value.replace(" ", "").replace(",", ".")
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
