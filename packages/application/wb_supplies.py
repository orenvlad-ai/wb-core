"""Read-only WB FBW supplies registry block for sheet_vitrina_v1 operator UI."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
from math import ceil
from typing import Any, Mapping, Protocol

from packages.adapters.official_api_runtime import OfficialApiRuntimeError
from packages.adapters.wb_supplies import (
    HttpBackedWbSuppliesSource,
    WbSuppliesHttpStatusError,
    WbSuppliesListResult,
    WbSuppliesTransportError,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime


CONTRACT_NAME = "sheet_vitrina_v1_wb_supplies"
CONTRACT_VERSION = "v1"
DEFAULT_SYNC_LIMIT = 1000
DEFAULT_PAGE_LIMIT = 20
ALLOWED_PAGE_LIMITS = (20, 50, 100)
SIZE_FILTER_MAIN_250 = "main_250"
SIZE_FILTER_ALL = "all"
SIZE_FILTER_SMALL_LT_250 = "small_lt_250"
SIZE_FILTER_THRESHOLD = 250
WB_SUPPLIES_SOURCE_LABEL = "WB API / FBW Supplies"

STATUS_LABELS_RU = {
    1: "Не запланировано",
    2: "Запланировано",
    3: "Отгрузка разрешена",
    4: "Идёт приёмка",
    5: "Принято",
    6: "Отгружено на воротах",
}

OFFICIAL_STATUS_IDS = (1, 2, 3, 4, 5, 6)
BOX_TYPE_LABELS_RU = {
    1: "Короб",
}

SCHEMA_COLUMNS = [
    {"key": "number_and_type", "label": "Номер и тип"},
    {"key": "supply_date", "label": "Дата поставки"},
    {"key": "warehouse", "label": "Склад"},
    {"key": "status", "label": "Статус"},
    {"key": "quantities", "label": "Добавлено, шт / Упаковано → Принято"},
    {"key": "acceptance_coefficient", "label": "Коэф. приёмки"},
    {"key": "acceptance_cost", "label": "Стоимость"},
]


class WbSuppliesSource(Protocol):
    def list_supplies(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        status_ids: list[int] | None = None,
        dates: list[Mapping[str, Any]] | None = None,
    ) -> WbSuppliesListResult | list[Mapping[str, Any]]:
        raise NotImplementedError

    def fetch_supply_details(self, supply_id: int | str, *, is_preorder_id: bool = False) -> Mapping[str, Any]:
        raise NotImplementedError

    def fetch_supply_goods(
        self,
        supply_id: int | str,
        *,
        limit: int = 1000,
        offset: int = 0,
        is_preorder_id: bool = False,
    ) -> list[Mapping[str, Any]]:
        raise NotImplementedError

    def fetch_supply_package(self, supply_id: int | str) -> list[Mapping[str, Any]]:
        raise NotImplementedError

    def fetch_warehouses(self) -> list[Mapping[str, Any]]:
        raise NotImplementedError


class WbSuppliesBlockError(RuntimeError):
    def __init__(self, message: str, *, http_status: int = 502) -> None:
        self.http_status = int(http_status)
        super().__init__(message)


class WbSuppliesBlock:
    """Builds cached read-only WB FBW supplies payloads for the operator UI."""

    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        source: WbSuppliesSource | None = None,
        timestamp_factory: Any | None = None,
    ) -> None:
        self.runtime = runtime
        self.source = source or HttpBackedWbSuppliesSource()
        self.timestamp_factory = timestamp_factory or _default_timestamp_factory

    def list_supplies(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        request = _normalize_list_request(params or {})
        rows = self.runtime.list_wb_supplies()
        warehouses = self.runtime.list_wb_supplies_warehouses()
        state = self.runtime.load_wb_supplies_sync_state()
        cache_completeness = _cache_completeness(state, rows)
        sorted_rows = sorted(rows, key=_row_sort_key, reverse=True)
        after_non_size_filters = [
            row
            for row in sorted_rows
            if _row_matches_search(row, request["search"])
            and _row_matches_warehouse(row, request["warehouse_id"], request["warehouse"])
            and _row_matches_status(row, request["status_id"])
        ]
        filtered_rows = [
            row
            for row in after_non_size_filters
            if _row_matches_size_filter(row, request["size_filter"])
        ]
        offset = min(request["offset"], len(filtered_rows))
        limit = request["limit"]
        page_rows = filtered_rows[offset : offset + limit]
        page_count = ceil(len(filtered_rows) / limit) if filtered_rows else 0
        unknown_quantity_count = sum(1 for row in after_non_size_filters if row.get("quantity_for_size_filter") is None)
        hidden_by_size_filter_count = len(after_non_size_filters) - len(filtered_rows)
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "meta": {
                "source": WB_SUPPLIES_SOURCE_LABEL,
                "read_only": True,
                "generated_at": self.timestamp_factory(),
                "cache_empty": len(rows) == 0,
                "cached_total_rows": len(rows),
                "last_synced_at": state.get("last_synced_at") or "",
                "last_successful_sync_at": state.get("last_successful_sync_at") or "",
                "last_error": state.get("last_error") or "",
                "cache_warning": _cache_warning(state, rows),
                "last_limit": state.get("last_limit"),
                "last_offset": state.get("last_offset"),
                "latest_synced_count": state.get("latest_synced_count"),
                "size_filter_threshold": SIZE_FILTER_THRESHOLD,
                "cache_completeness": cache_completeness["status"],
                "cache_completeness_label": cache_completeness["label"],
                "can_backfill_more": cache_completeness["can_backfill_more"],
            },
            "filters": {
                "current": {
                    "search": request["search"],
                    "warehouse_id": request["warehouse_id"],
                    "warehouse": request["warehouse"],
                    "status_id": request["status_id"],
                    "size_filter": request["size_filter"],
                    "limit": limit,
                    "offset": offset,
                },
                "options": {
                    "warehouses": _warehouse_options(rows, warehouses),
                    "statuses": _status_options(rows),
                    "size_filters": [
                        {"value": SIZE_FILTER_MAIN_250, "label": "Основные от 250 шт", "default": True},
                        {"value": SIZE_FILTER_ALL, "label": "Все поставки", "default": False},
                        {"value": SIZE_FILTER_SMALL_LT_250, "label": "Мелкие до 249 шт", "default": False},
                    ],
                    "limits": list(ALLOWED_PAGE_LIMITS),
                },
            },
            "summary": {
                "cached_total_rows": len(rows),
                "after_non_size_filters_rows": len(after_non_size_filters),
                "filtered_rows": len(filtered_rows),
                "visible_page_rows": len(page_rows),
                "hidden_by_size_filter_count": hidden_by_size_filter_count,
                "unknown_quantity_count": unknown_quantity_count,
                "size_filter_threshold": SIZE_FILTER_THRESHOLD,
                "cache_completeness": cache_completeness["status"],
            },
            "pagination": {
                "limit": limit,
                "offset": offset,
                "page": (offset // limit) + 1 if filtered_rows else 1,
                "page_count": page_count,
                "total": len(filtered_rows),
                "has_previous": offset > 0,
                "has_next": offset + limit < len(filtered_rows),
                "cached_total_rows": len(rows),
            },
            "schema": {"columns": SCHEMA_COLUMNS},
            "rows": page_rows,
        }

    def sync_supplies(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        request = _normalize_sync_request(payload or {})
        synced_at = self.timestamp_factory()
        warnings: list[str] = []
        try:
            warehouses = self._fetch_warehouses(warnings)
            list_result = _coerce_list_result(
                self.source.list_supplies(
                    limit=request["limit"],
                    offset=request["offset"],
                    status_ids=request["status_ids"],
                    dates=request["dates"],
                )
            )
            warehouse_by_id = _warehouse_map(warehouses)
            normalized_rows: list[dict[str, Any]] = []
            for raw_row in list_result.rows:
                detail_payload: Mapping[str, Any] | None = None
                goods_payload: list[Mapping[str, Any]] | None = None
                package_payload: list[Mapping[str, Any]] | None = None
                row_warnings: list[str] = []
                lookup_id, is_preorder_id = _resolve_upstream_lookup_id(raw_row)
                if request["enrich_details"] and lookup_id:
                    detail_payload = self._fetch_detail(lookup_id, is_preorder_id=is_preorder_id, warnings=row_warnings)
                    goods_payload = self._fetch_goods(lookup_id, is_preorder_id=is_preorder_id, warnings=row_warnings)
                normalized_rows.append(
                    _normalize_supply_row(
                        raw_list=raw_row,
                        raw_detail=detail_payload,
                        raw_goods=goods_payload,
                        raw_package=package_payload,
                        warehouse_by_id=warehouse_by_id,
                        synced_at=synced_at,
                        warnings=row_warnings,
                    )
                )
            self.runtime.upsert_wb_supplies(
                rows=normalized_rows,
                warehouses=warehouses,
                synced_at=synced_at,
                last_successful_sync_at=synced_at,
                last_error="",
                last_limit=list_result.limit,
                last_offset=list_result.offset,
                latest_synced_count=len(normalized_rows),
            )
        except Exception as exc:
            block_error = _to_block_error(exc)
            self.runtime.save_wb_supplies_sync_state(
                last_synced_at=synced_at,
                last_successful_sync_at=None,
                last_error=str(block_error),
                last_limit=request["limit"],
                last_offset=request["offset"],
                latest_synced_count=0,
            )
            raise block_error from exc

        response = self.list_supplies(
            {
                "limit": DEFAULT_PAGE_LIMIT,
                "offset": 0,
                "size_filter": SIZE_FILTER_MAIN_250,
            }
        )
        response["sync"] = {
            "status": "ok",
            "synced_at": synced_at,
            "limit": list_result.limit,
            "offset": list_result.offset,
            "raw_fetched_count": list_result.raw_count,
            "upserted_count": len(normalized_rows),
            "enrich_details": request["enrich_details"],
            "warnings": warnings,
        }
        return response

    def get_supply(self, supply_id: str) -> dict[str, Any]:
        normalized_id = str(supply_id or "").strip()
        if not normalized_id:
            raise WbSuppliesBlockError("supply_id is required", http_status=400)
        detail = self.runtime.load_wb_supply(normalized_id)
        if detail is None:
            raise WbSuppliesBlockError(f"WB supply not found in cache: {normalized_id}", http_status=404)
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "meta": {"source": WB_SUPPLIES_SOURCE_LABEL, "read_only": True},
            "supply": detail,
        }

    def _fetch_warehouses(self, warnings: list[str]) -> list[Mapping[str, Any]]:
        try:
            return self.source.fetch_warehouses()
        except WbSuppliesHttpStatusError as exc:
            if exc.status_code in {401, 403}:
                raise
            warnings.append(f"warehouses fetch failed with status {exc.status_code}; using row warehouse fields")
            return []
        except WbSuppliesTransportError as exc:
            warnings.append(str(exc))
            return []
        except RuntimeError as exc:
            if "required env WB_API_TOKEN is not set" in str(exc):
                raise
            warnings.append(str(exc))
            return []

    def _fetch_detail(
        self,
        lookup_id: str,
        *,
        is_preorder_id: bool,
        warnings: list[str],
    ) -> Mapping[str, Any] | None:
        try:
            return self.source.fetch_supply_details(lookup_id, is_preorder_id=is_preorder_id)
        except WbSuppliesHttpStatusError as exc:
            if exc.status_code in {401, 403}:
                raise
            warnings.append(f"details fetch failed for {lookup_id}: status {exc.status_code}")
            return None
        except WbSuppliesTransportError as exc:
            warnings.append(f"details fetch failed for {lookup_id}: {exc}")
            return None

    def _fetch_goods(
        self,
        lookup_id: str,
        *,
        is_preorder_id: bool,
        warnings: list[str],
    ) -> list[Mapping[str, Any]] | None:
        try:
            return self.source.fetch_supply_goods(lookup_id, limit=1000, offset=0, is_preorder_id=is_preorder_id)
        except WbSuppliesHttpStatusError as exc:
            if exc.status_code in {401, 403}:
                raise
            warnings.append(f"goods fetch failed for {lookup_id}: status {exc.status_code}")
            return None
        except WbSuppliesTransportError as exc:
            warnings.append(f"goods fetch failed for {lookup_id}: {exc}")
            return None


def _normalize_list_request(params: Mapping[str, Any]) -> dict[str, Any]:
    limit = _normalize_limit(params.get("limit"))
    return {
        "search": str(params.get("search") or "").strip(),
        "warehouse_id": str(params.get("warehouse_id") or "").strip(),
        "warehouse": str(params.get("warehouse") or "").strip(),
        "status_id": _optional_int(params.get("status_id")),
        "size_filter": _normalize_size_filter(params.get("size_filter")),
        "limit": limit,
        "offset": max(0, _optional_int(params.get("offset")) or 0),
    }


def _normalize_sync_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "limit": min(max(_optional_int(payload.get("limit")) or DEFAULT_SYNC_LIMIT, 1), 1000),
        "offset": max(0, _optional_int(payload.get("offset")) or 0),
        "enrich_details": payload.get("enrich_details") is not False,
        "status_ids": _normalize_status_ids(payload.get("status_ids") or payload.get("statusIDs")),
        "dates": [dict(item) for item in payload.get("dates") or [] if isinstance(item, Mapping)],
    }


def _normalize_limit(value: Any) -> int:
    normalized = _optional_int(value) or DEFAULT_PAGE_LIMIT
    return normalized if normalized in ALLOWED_PAGE_LIMITS else DEFAULT_PAGE_LIMIT


def _normalize_size_filter(value: Any) -> str:
    normalized = str(value or "").strip()
    if normalized in {SIZE_FILTER_MAIN_250, SIZE_FILTER_ALL, SIZE_FILTER_SMALL_LT_250}:
        return normalized
    return SIZE_FILTER_MAIN_250


def _normalize_status_ids(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    result: list[int] = []
    for item in value:
        status_id = _optional_int(item)
        if status_id is not None and status_id > 0 and status_id not in result:
            result.append(status_id)
    return result


def _coerce_list_result(value: Any) -> WbSuppliesListResult:
    if isinstance(value, WbSuppliesListResult):
        return value
    if is_dataclass(value):
        data = asdict(value)
        rows = data.get("rows") or []
        return WbSuppliesListResult(
            rows=[item for item in rows if isinstance(item, Mapping)],
            raw_count=int(data.get("raw_count") or len(rows)),
            limit=int(data.get("limit") or DEFAULT_SYNC_LIMIT),
            offset=int(data.get("offset") or 0),
            status_ids=list(data.get("status_ids") or []),
            dates=list(data.get("dates") or []),
        )
    if isinstance(value, list):
        return WbSuppliesListResult(
            rows=[item for item in value if isinstance(item, Mapping)],
            raw_count=len(value),
            limit=DEFAULT_SYNC_LIMIT,
            offset=0,
            status_ids=[],
            dates=[],
        )
    raise WbSuppliesBlockError("WB supplies source returned invalid list result", http_status=502)


def _normalize_supply_row(
    *,
    raw_list: Mapping[str, Any],
    raw_detail: Mapping[str, Any] | None,
    raw_goods: list[Mapping[str, Any]] | None,
    raw_package: list[Mapping[str, Any]] | None,
    warehouse_by_id: Mapping[str, str],
    synced_at: str,
    warnings: list[str],
) -> dict[str, Any]:
    detail = raw_detail or {}
    sources = [("detail", detail), ("list", raw_list)]
    supply_id_value, _supply_id_evidence = _first_non_empty_from_sources(
        sources, "supplyID", "supplyId", "supply_id", "ID", "id"
    )
    preorder_id_value, _preorder_id_evidence = _first_non_empty_from_sources(
        sources, "preorderID", "preorderId", "preorder_id", "orderID", "orderId"
    )
    supply_id = _id_to_string(supply_id_value)
    preorder_id = _id_to_string(preorder_id_value)
    cache_supply_id = supply_id or (f"preorder:{preorder_id}" if preorder_id else _raw_row_cache_id(raw_list))
    visible_number = supply_id or preorder_id or cache_supply_id

    status_id = _optional_int(_first_non_empty_from_sources(sources, "statusID", "statusId", "status_id")[0])
    warehouse_id, warehouse_id_evidence = _first_id_from_sources(sources, "warehouseID", "warehouseId", "warehouse_id")
    actual_warehouse_id, actual_warehouse_id_evidence = _first_id_from_sources(
        sources, "actualWarehouseID", "actualWarehouseId", "actual_warehouse_id"
    )
    transit_warehouse_id, transit_warehouse_id_evidence = _first_id_from_sources(
        sources, "transitWarehouseID", "transitWarehouseId", "transit_warehouse_id"
    )
    warehouse_name, warehouse_name_evidence = _first_string_from_sources(sources, "warehouseName", "warehouse_name")
    if not warehouse_name and warehouse_id:
        warehouse_name = warehouse_by_id.get(warehouse_id, "")
        warehouse_name_evidence = "warehouse_dict" if warehouse_name else warehouse_name_evidence
    actual_warehouse_name, actual_warehouse_name_evidence = _first_string_from_sources(
        sources, "actualWarehouseName", "actual_warehouse_name"
    )
    if not actual_warehouse_name and actual_warehouse_id:
        actual_warehouse_name = warehouse_by_id.get(actual_warehouse_id, "")
        actual_warehouse_name_evidence = "warehouse_dict" if actual_warehouse_name else actual_warehouse_name_evidence
    transit_warehouse_name, transit_warehouse_name_evidence = _first_string_from_sources(
        sources, "transitWarehouseName", "transit_warehouse_name"
    )
    if not transit_warehouse_name and transit_warehouse_id:
        transit_warehouse_name = warehouse_by_id.get(transit_warehouse_id, "")
        transit_warehouse_name_evidence = "warehouse_dict" if transit_warehouse_name else transit_warehouse_name_evidence
    box_type_id = _optional_int(_first_non_empty_from_sources(sources, "boxTypeID", "boxTypeId", "box_type_id")[0])
    planned_quantity, planned_quantity_evidence = _first_number_from_sources(
        sources, "quantity", "plannedQuantity", "planned_quantity", "addedQuantity", "added_quantity"
    )
    goods_quantity = _sum_goods_field(raw_goods, "quantity") if raw_goods is not None else None
    goods_supplier_box_quantity = _sum_goods_field(raw_goods, "supplierBoxAmount") if raw_goods is not None else None
    package_quantity = _sum_package_quantity(raw_package) if raw_package is not None else None
    quantity_added, quantity_evidence = _first_quantity_with_evidence(
        (planned_quantity, planned_quantity_evidence),
        (goods_quantity, "goods.quantity_total"),
        (package_quantity, "package.quantity_total"),
    )
    accepted_quantity, accepted_quantity_evidence = _first_number_from_sources(
        sources, "acceptedQuantity", "accepted_quantity"
    )
    if accepted_quantity is None and raw_goods is not None:
        accepted_quantity = _sum_goods_field(raw_goods, "acceptedQuantity")
        accepted_quantity_evidence = "goods.acceptedQuantity_total" if accepted_quantity is not None else accepted_quantity_evidence
    unloading_quantity, unloading_quantity_evidence = _first_number_from_sources(
        sources, "unloadingQuantity", "unloading_quantity"
    )
    if unloading_quantity is None and raw_goods is not None:
        unloading_quantity = _sum_goods_field(raw_goods, "unloadingQuantity")
        unloading_quantity_evidence = "goods.unloadingQuantity_total" if unloading_quantity is not None else unloading_quantity_evidence
    quantity_for_size_filter, quantity_evidence = _quantity_for_size_filter(
        planned_quantity=quantity_added,
        goods_quantity=goods_quantity,
        accepted_quantity=accepted_quantity,
        unloading_quantity=unloading_quantity,
        planned_quantity_evidence=quantity_evidence,
    )
    packed_quantity, packed_quantity_evidence = _packed_quantity(
        sources=sources,
        goods_quantity=goods_quantity,
        goods_supplier_box_quantity=goods_supplier_box_quantity,
        package_quantity=package_quantity,
        quantity_added=quantity_added,
        status_id=status_id,
    )
    acceptance_cost, acceptance_cost_evidence = _first_number_from_sources(sources, "acceptanceCost", "acceptance_cost")
    transit_cost, transit_cost_evidence = _first_number_from_sources(
        sources,
        "transitCost",
        "transit_cost",
        "transitTariff",
        "transit_tariff",
        "transitCostTotal",
        "transit_cost_total",
    )
    cost_total, cost_evidence = _cost_total(
        sources=sources,
        acceptance_cost=acceptance_cost,
        acceptance_cost_evidence=acceptance_cost_evidence,
        transit_cost=transit_cost,
        transit_cost_evidence=transit_cost_evidence,
        is_transit=bool(transit_warehouse_id or transit_warehouse_name),
    )
    acceptance_coefficient = _first_number_from_sources(
        sources, "paidAcceptanceCoefficient", "acceptanceCoefficient", "acceptance_coefficient"
    )[0]
    create_date = _first_string_from_sources(sources, "createDate", "createdAt", "created_at")[0]
    supply_date = _first_string_from_sources(sources, "supplyDate", "supply_date")[0]
    fact_date = _first_string_from_sources(sources, "factDate", "fact_date")[0]
    updated_date = _first_string_from_sources(sources, "updatedDate", "updated_at", "updatedDate")[0]
    is_transit = bool(transit_warehouse_id or transit_warehouse_name)
    warehouse_from_name = warehouse_name
    warehouse_to_name = transit_warehouse_name if is_transit else ""
    if is_transit and not warehouse_to_name:
        warehouse_to_name = actual_warehouse_name
    route_evidence = _route_evidence(
        is_transit=is_transit,
        warehouse_from_name=warehouse_from_name,
        warehouse_to_name=warehouse_to_name,
        warehouse_name_evidence=warehouse_name_evidence,
        transit_warehouse_name_evidence=transit_warehouse_name_evidence,
        actual_warehouse_name_evidence=actual_warehouse_name_evidence,
    )
    warehouse_display = _warehouse_display(
        is_transit=is_transit,
        warehouse_from_name=warehouse_from_name,
        warehouse_to_name=warehouse_to_name,
        warehouse_name=warehouse_name,
        actual_warehouse_name=actual_warehouse_name,
        transit_warehouse_name=transit_warehouse_name,
    )
    return {
        "supply_id": cache_supply_id,
        "wb_supply_id": supply_id,
        "preorder_id": preorder_id,
        "visible_number": visible_number,
        "number_label": visible_number or "—",
        "status_id": status_id,
        "status_label": _status_label(status_id),
        "status_tone": _status_tone(status_id),
        "box_type_id": box_type_id,
        "box_type_label": _box_type_label(box_type_id),
        "type_label": _type_label(box_type_id=box_type_id, is_transit=is_transit),
        "is_box_on_pallet": _optional_bool(_first_non_empty_from_sources(sources, "isBoxOnPallet", "is_box_on_pallet")[0]),
        "warehouse_id": warehouse_id,
        "warehouse_name": warehouse_name,
        "actual_warehouse_id": actual_warehouse_id,
        "actual_warehouse_name": actual_warehouse_name,
        "transit_warehouse_id": transit_warehouse_id,
        "transit_warehouse_name": transit_warehouse_name,
        "warehouse_from_name": warehouse_from_name,
        "warehouse_to_name": warehouse_to_name,
        "warehouse_actual_name": actual_warehouse_name,
        "warehouse_display": warehouse_display,
        "warehouse_fact_line": _warehouse_fact_line(
            is_transit=is_transit,
            warehouse_name=warehouse_name,
            actual_warehouse_name=actual_warehouse_name,
            warehouse_to_name=warehouse_to_name,
        ),
        "warehouse_evidence": {
            "warehouse_id": warehouse_id_evidence,
            "warehouse_name": warehouse_name_evidence,
            "actual_warehouse_id": actual_warehouse_id_evidence,
            "actual_warehouse_name": actual_warehouse_name_evidence,
            "transit_warehouse_id": transit_warehouse_id_evidence,
            "transit_warehouse_name": transit_warehouse_name_evidence,
        },
        "route_evidence": route_evidence,
        "supply_date": supply_date,
        "fact_date": fact_date,
        "source_created_at": create_date,
        "updated_date": updated_date,
        "quantity_added": quantity_added,
        "goods_quantity_total": goods_quantity,
        "goods_supplier_box_quantity_total": goods_supplier_box_quantity,
        "package_quantity_total": package_quantity,
        "packed_quantity": packed_quantity,
        "accepted_quantity": accepted_quantity,
        "unloading_quantity": unloading_quantity,
        "quantity_for_size_filter": quantity_for_size_filter,
        "quantity_evidence": quantity_evidence,
        "accepted_quantity_evidence": accepted_quantity_evidence,
        "unloading_quantity_evidence": unloading_quantity_evidence,
        "packed_quantity_evidence": packed_quantity_evidence,
        "acceptance_coefficient": acceptance_coefficient,
        "acceptance_cost": acceptance_cost,
        "transit_cost": transit_cost,
        "cost_total": cost_total,
        "cost_display": _cost_display(cost_total),
        "cost_evidence": cost_evidence,
        "has_transit_cost_marker": is_transit,
        "synced_at": synced_at,
        "warnings": list(warnings),
        "raw_diagnostics": {
            "list_keys": sorted(raw_list.keys()),
            "detail_keys": sorted(detail.keys()),
            "goods_count": len(raw_goods) if raw_goods is not None else None,
            "package_count": len(raw_package) if raw_package is not None else None,
        },
        "raw_list": dict(raw_list),
        "raw_detail": dict(raw_detail) if raw_detail is not None else None,
        "raw_goods": [dict(item) for item in raw_goods] if raw_goods is not None else None,
        "raw_package": [dict(item) for item in raw_package] if raw_package is not None else None,
    }


def _resolve_upstream_lookup_id(row: Mapping[str, Any]) -> tuple[str, bool]:
    supply_id = _id_to_string(_first_value(row, "supplyID", "supplyId", "supply_id", "ID", "id"))
    if supply_id:
        return supply_id, False
    preorder_id = _id_to_string(_first_value(row, "preorderID", "preorderId", "preorder_id", "orderID", "orderId"))
    if preorder_id:
        return preorder_id, True
    return "", False


def _first_non_empty_from_sources(sources: list[tuple[str, Mapping[str, Any]]], *keys: str) -> tuple[Any, str]:
    for source_name, mapping in sources:
        for key in keys:
            if key not in mapping:
                continue
            value = mapping.get(key)
            if _is_non_empty_value(value):
                return value, f"{source_name}.{key}"
    return None, "unknown"


def _first_id_from_sources(sources: list[tuple[str, Mapping[str, Any]]], *keys: str) -> tuple[str, str]:
    value, evidence = _first_non_empty_from_sources(sources, *keys)
    return _id_to_string(value), evidence


def _first_string_from_sources(sources: list[tuple[str, Mapping[str, Any]]], *keys: str) -> tuple[str, str]:
    value, evidence = _first_non_empty_from_sources(sources, *keys)
    return (str(value).strip() if value is not None else ""), evidence


def _first_number_from_sources(
    sources: list[tuple[str, Mapping[str, Any]]],
    *keys: str,
) -> tuple[float | None, str]:
    for source_name, mapping in sources:
        for key in keys:
            if key not in mapping:
                continue
            number = _optional_number(mapping.get(key))
            if number is not None:
                return number, f"{source_name}.{key}"
    return None, "unknown"


def _is_non_empty_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True


def _first_quantity_with_evidence(*candidates: tuple[float | None, str]) -> tuple[float | None, str]:
    for value, evidence in candidates:
        if value is not None:
            return value, evidence
    return None, "unknown"


def _packed_quantity(
    *,
    sources: list[tuple[str, Mapping[str, Any]]],
    goods_quantity: float | None,
    goods_supplier_box_quantity: float | None,
    package_quantity: float | None,
    quantity_added: float | None,
    status_id: int | None,
) -> tuple[float | None, str]:
    explicit, explicit_evidence = _first_number_from_sources(
        sources,
        "packedQuantity",
        "packed_quantity",
        "supplierBoxAmount",
        "supplier_box_amount",
        "packageQuantity",
        "package_quantity",
    )
    if explicit is not None:
        return explicit, explicit_evidence
    if goods_quantity is not None:
        return goods_quantity, "goods.quantity_total"
    if goods_supplier_box_quantity is not None:
        return goods_supplier_box_quantity, "goods.supplierBoxAmount_total"
    if package_quantity is not None:
        return package_quantity, "package.quantity_total"
    if status_id in {5, 6} and quantity_added is not None:
        return quantity_added, "quantity_added.accepted_supply_fallback"
    return None, "unknown"


def _cost_total(
    *,
    sources: list[tuple[str, Mapping[str, Any]]],
    acceptance_cost: float | None,
    acceptance_cost_evidence: str,
    transit_cost: float | None,
    transit_cost_evidence: str,
    is_transit: bool,
) -> tuple[float | None, str]:
    explicit, explicit_evidence = _first_number_from_sources(
        sources,
        "costTotal",
        "cost_total",
        "totalCost",
        "total_cost",
        "cost",
        "price",
        "acceptanceCostTotal",
        "acceptance_cost_total",
        "transitCostTotal",
        "transit_cost_total",
    )
    if explicit is not None:
        return explicit, explicit_evidence
    if transit_cost is not None and acceptance_cost is not None:
        return transit_cost + acceptance_cost, f"{transit_cost_evidence}+{acceptance_cost_evidence}"
    if transit_cost is not None:
        return transit_cost, transit_cost_evidence
    if is_transit and acceptance_cost in {None, 0}:
        return None, "transit_total_absent_in_official_supply_detail"
    if acceptance_cost is not None:
        return acceptance_cost, acceptance_cost_evidence
    return None, "unknown"


def _cost_display(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}"


def _route_evidence(
    *,
    is_transit: bool,
    warehouse_from_name: str,
    warehouse_to_name: str,
    warehouse_name_evidence: str,
    transit_warehouse_name_evidence: str,
    actual_warehouse_name_evidence: str,
) -> str:
    if is_transit and warehouse_from_name and warehouse_to_name:
        to_evidence = transit_warehouse_name_evidence
        if transit_warehouse_name_evidence == "unknown":
            to_evidence = actual_warehouse_name_evidence
        return f"{warehouse_name_evidence} -> {to_evidence}"
    if warehouse_from_name:
        return warehouse_name_evidence
    if warehouse_to_name:
        return transit_warehouse_name_evidence
    return actual_warehouse_name_evidence


def _quantity_for_size_filter(
    *,
    planned_quantity: float | None,
    goods_quantity: float | None,
    accepted_quantity: float | None,
    unloading_quantity: float | None,
    planned_quantity_evidence: str,
) -> tuple[float | None, str]:
    if planned_quantity is not None:
        return planned_quantity, planned_quantity_evidence or "quantity_added"
    if goods_quantity is not None:
        return goods_quantity, "goods.quantity_total"
    if accepted_quantity is not None:
        return accepted_quantity, "accepted_quantity_fallback"
    if unloading_quantity is not None:
        return unloading_quantity, "unloading_quantity_fallback"
    return None, "unknown"


def _row_matches_search(row: Mapping[str, Any], search: str) -> bool:
    if not search:
        return True
    needle = search.casefold()
    haystack = " ".join(
        str(row.get(key) or "")
        for key in ("supply_id", "wb_supply_id", "preorder_id", "visible_number", "number_label")
    ).casefold()
    return needle in haystack


def _row_matches_warehouse(row: Mapping[str, Any], warehouse_id: str, warehouse: str) -> bool:
    if warehouse_id:
        return warehouse_id in {
            str(row.get("warehouse_id") or ""),
            str(row.get("actual_warehouse_id") or ""),
            str(row.get("transit_warehouse_id") or ""),
        }
    if warehouse:
        needle = warehouse.casefold()
        return needle in " ".join(
            str(row.get(key) or "")
            for key in (
                "warehouse_name",
                "warehouse_from_name",
                "warehouse_to_name",
                "actual_warehouse_name",
                "warehouse_actual_name",
                "transit_warehouse_name",
                "warehouse_display",
            )
        ).casefold()
    return True


def _row_matches_status(row: Mapping[str, Any], status_id: int | None) -> bool:
    return status_id is None or _optional_int(row.get("status_id")) == status_id


def _row_matches_size_filter(row: Mapping[str, Any], size_filter: str) -> bool:
    if size_filter == SIZE_FILTER_ALL:
        return True
    quantity = _optional_number(row.get("quantity_for_size_filter"))
    if quantity is None:
        return False
    if size_filter == SIZE_FILTER_SMALL_LT_250:
        return quantity < SIZE_FILTER_THRESHOLD
    return quantity >= SIZE_FILTER_THRESHOLD


def _warehouse_options(rows: list[Mapping[str, Any]], warehouse_rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    options: dict[str, dict[str, Any]] = {}
    for item in warehouse_rows:
        warehouse_id = _id_to_string(_first_value(item, "warehouse_id", "ID", "id"))
        name = _first_string(item, "warehouse_name", "name")
        if warehouse_id or name:
            options[warehouse_id or name] = {"value": warehouse_id, "label": name or warehouse_id}
    for row in rows:
        for id_key, name_key in (
            ("warehouse_id", "warehouse_name"),
            ("actual_warehouse_id", "actual_warehouse_name"),
            ("transit_warehouse_id", "transit_warehouse_name"),
        ):
            warehouse_id = str(row.get(id_key) or "").strip()
            name = str(row.get(name_key) or "").strip()
            if warehouse_id or name:
                options.setdefault(warehouse_id or name, {"value": warehouse_id, "label": name or warehouse_id})
    return sorted(options.values(), key=lambda item: str(item.get("label") or "").casefold())


def _status_options(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    status_ids = sorted(
        set(OFFICIAL_STATUS_IDS)
        | {status_id for row in rows if (status_id := _optional_int(row.get("status_id"))) is not None}
    )
    return [{"value": status_id, "label": _status_label(status_id)} for status_id in status_ids]


def _warehouse_map(rows: list[Mapping[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in rows:
        warehouse_id = _id_to_string(_first_value(item, "ID", "id", "warehouseID", "warehouse_id"))
        name = _first_string(item, "name", "warehouseName", "warehouse_name")
        if warehouse_id and name:
            result[warehouse_id] = name
    return result


def _status_label(status_id: int | None) -> str:
    if status_id is None:
        return "—"
    return STATUS_LABELS_RU.get(status_id, f"Статус {status_id}")


def _status_tone(status_id: int | None) -> str:
    if status_id == 5:
        return "success"
    if status_id in {3, 4, 6}:
        return "warning"
    if status_id in {1, 2}:
        return "idle"
    return "neutral"


def _box_type_label(box_type_id: int | None) -> str:
    if box_type_id is None:
        return ""
    return BOX_TYPE_LABELS_RU.get(box_type_id, f"Тип {box_type_id}")


def _type_label(*, box_type_id: int | None, is_transit: bool) -> str:
    parts: list[str] = []
    box_label = _box_type_label(box_type_id)
    if box_label:
        parts.append(box_label)
    if is_transit:
        parts.append("с транзитом")
    return " · ".join(parts)


def _warehouse_display(
    *,
    is_transit: bool,
    warehouse_from_name: str,
    warehouse_to_name: str,
    warehouse_name: str,
    actual_warehouse_name: str,
    transit_warehouse_name: str,
) -> str:
    if is_transit:
        if warehouse_from_name and warehouse_to_name and warehouse_from_name != warehouse_to_name:
            return f"{warehouse_from_name} → {warehouse_to_name}"
        return warehouse_from_name or warehouse_to_name or transit_warehouse_name or actual_warehouse_name
    return warehouse_name or actual_warehouse_name or transit_warehouse_name


def _warehouse_fact_line(
    *,
    is_transit: bool,
    warehouse_name: str,
    actual_warehouse_name: str,
    warehouse_to_name: str,
) -> str:
    if is_transit:
        if actual_warehouse_name and warehouse_to_name and actual_warehouse_name not in {warehouse_to_name, warehouse_name}:
            return f"Факт: {actual_warehouse_name}"
        return ""
    if actual_warehouse_name and warehouse_name and actual_warehouse_name != warehouse_name:
        return f"Факт: {actual_warehouse_name}"
    return ""


def _cache_completeness(state: Mapping[str, Any], rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    last_limit = _optional_int(state.get("last_limit"))
    latest_synced_count = _optional_int(state.get("latest_synced_count"))
    if not rows:
        return {"status": "empty", "label": "Cache пуст", "can_backfill_more": False}
    if last_limit and latest_synced_count is not None and latest_synced_count >= last_limit:
        return {
            "status": "partial",
            "label": "История может быть неполной: последняя страница WB заполнена",
            "can_backfill_more": True,
        }
    if last_limit and latest_synced_count is not None and latest_synced_count < last_limit:
        return {"status": "complete_window", "label": "Загруженное окно завершено", "can_backfill_more": False}
    return {"status": "unknown", "label": "Полнота cache неизвестна", "can_backfill_more": True}


def _cache_warning(state: Mapping[str, Any], rows: list[Mapping[str, Any]]) -> str:
    last_error = str(state.get("last_error") or "").strip()
    if rows and last_error:
        return f"Показаны cached rows; последний sync завершился ошибкой: {last_error}"
    return ""


def _row_sort_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("updated_date") or ""),
        str(row.get("supply_date") or ""),
        str(row.get("source_created_at") or ""),
        str(row.get("visible_number") or row.get("supply_id") or ""),
    )


def _to_block_error(exc: Exception) -> WbSuppliesBlockError:
    if isinstance(exc, WbSuppliesBlockError):
        return exc
    if isinstance(exc, WbSuppliesHttpStatusError):
        return WbSuppliesBlockError(_friendly_http_error_message(exc.status_code), http_status=_mapped_http_status(exc.status_code))
    if isinstance(exc, WbSuppliesTransportError):
        return WbSuppliesBlockError(str(exc), http_status=502)
    if isinstance(exc, OfficialApiRuntimeError):
        message = str(exc)
        status = 503 if "required env WB_API_TOKEN is not set" in message else 500
        return WbSuppliesBlockError(message, http_status=status)
    if isinstance(exc, RuntimeError):
        message = str(exc)
        status = 503 if "required env WB_API_TOKEN is not set" in message else 502
        return WbSuppliesBlockError(message, http_status=status)
    return WbSuppliesBlockError(f"WB supplies runtime failed: {exc}", http_status=500)


def _friendly_http_error_message(status_code: int) -> str:
    if status_code in {401, 403}:
        return "WB API token has no Supplies permission or is invalid"
    if status_code == 429:
        return "WB supplies API rate limit returned 429; retry later"
    if status_code >= 500:
        return f"WB supplies API upstream is unavailable: status {status_code}"
    return f"WB supplies API request failed with status {status_code}"


def _mapped_http_status(status_code: int) -> int:
    if status_code in {401, 403, 429}:
        return status_code
    if status_code >= 500:
        return 502
    return 502


def _sum_goods_field(rows: list[Mapping[str, Any]], field_name: str) -> float | None:
    found = False
    total = 0.0
    for row in rows:
        value = _optional_number(_first_value(row, field_name, _camel_to_snake(field_name)))
        if value is None:
            continue
        found = True
        total += value
    return total if found else 0.0 if rows == [] else None


def _sum_package_quantity(rows: list[Mapping[str, Any]]) -> float | None:
    found = False
    total = 0.0
    for row in rows:
        value = _optional_number(_first_value(row, "quantity", "packageQuantity", "package_quantity"))
        if value is None:
            continue
        found = True
        total += value
    return total if found else 0.0 if rows == [] else None


def _first_value(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping.get(key) is not None:
            return mapping.get(key)
    return None


def _first_string(mapping: Mapping[str, Any], *keys: str) -> str:
    value = _first_value(mapping, *keys)
    if value is None:
        return ""
    return str(value).strip()


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "да"}:
        return True
    if normalized in {"0", "false", "no", "нет"}:
        return False
    return None


def _id_to_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _raw_row_cache_id(row: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(row), ensure_ascii=False, sort_keys=True)
    return "raw:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _camel_to_snake(value: str) -> str:
    result = []
    for char in value:
        if char.isupper() and result:
            result.append("_")
        result.append(char.lower())
    return "".join(result)


def _default_timestamp_factory() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
