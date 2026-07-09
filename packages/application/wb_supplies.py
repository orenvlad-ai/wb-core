"""Read-only WB FBW supplies registry block for sheet_vitrina_v1 operator UI."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
import hashlib
import json
from math import ceil
import threading
import time
from typing import Any, Callable, Mapping, Protocol
import uuid

from packages.adapters.official_api_runtime import OfficialApiRuntimeError
from packages.adapters.wb_supplies import (
    HttpBackedWbSuppliesSource,
    WbSuppliesHttpStatusError,
    WbSuppliesListResult,
    WbSuppliesTransportError,
)
from packages.adapters.seller_portal_transit_costs import (
    SELLER_PORTAL_SUPPLY_COST_ENDPOINT_PATH,
    SELLER_PORTAL_TRANSIT_COST_EVIDENCE_TYPE,
    SELLER_PORTAL_TRANSIT_COST_SOURCE,
    SellerPortalTransitCostNetworkJsonSource,
    SellerPortalTransitCostSourceError,
)
from packages.application.ff_stock_ledger import FfStockLedgerBlock
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.wb_supply_overlay import (
    ELIGIBLE_WB_SUPPLY_STATUS_IDS,
    augment_supply_row_with_district,
    build_warehouse_district_mapping,
    build_wb_supply_overlay_options,
    district_filter_options,
)
from packages.business_time import current_business_date_iso


CONTRACT_NAME = "sheet_vitrina_v1_wb_supplies"
CONTRACT_VERSION = "v1"
DEFAULT_SYNC_LIMIT = 1000
DEFAULT_PAGE_LIMIT = 20
DEFAULT_TRANSIT_COST_ENRICHMENT_LIMIT = 50
MAX_TRANSIT_COST_ENRICHMENT_LIMIT = 100
MAX_FORCED_STATUS_REFRESH_ROWS = 12
TRANSIT_COST_ENRICHMENT_FRESH_SECONDS = 24 * 60 * 60
SYNC_MODE_INCREMENTAL_REFRESH = "incremental_refresh"
SYNC_MODE_FULL_BACKFILL = "full_backfill"
SYNC_MODE_ENRICH_MISSING = "enrich_missing"
RUN_ACTIVE_STATUSES = {"queued", "running"}
TRANSIT_COST_RUN_ACTIVE_STATUSES = {"queued", "running"}
ALLOWED_PAGE_LIMITS = (20, 50, 100)
ALLOWED_SORT_KEYS = {"supply_date"}
DEFAULT_SORT_KEY = "supply_date"
DEFAULT_SORT_DIR = "desc"
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
PLANNED_STATUS_ID = 2
ACTIVE_RECONCILE_STATUS_IDS = (1, 2, 3, 4)
ACTIVE_SUPPLY_BACKED_STATUS_IDS = (2, 3, 4)
HISTORICAL_STATUS_IDS = (5, 6)
RECENT_HISTORICAL_RECONCILE_STATUS_IDS = HISTORICAL_STATUS_IDS
SUPPLY_BACKED_REFRESH_STATUS_IDS = ACTIVE_SUPPLY_BACKED_STATUS_IDS + HISTORICAL_STATUS_IDS
BOX_TYPE_LABELS_RU = {
    1: "Короб",
}
VIRTUAL_TYPE_LABELS_RU = {
    5: "Допринято",
}

SCHEMA_COLUMNS = [
    {"key": "number_and_type", "label": "Номер и тип"},
    {"key": "supply_date", "label": "Дата поставки"},
    {"key": "warehouse", "label": "Склад"},
    {"key": "status", "label": "Статус"},
    {"key": "quantities", "label": "Добавлено, шт / Упаковано → Принято"},
    {"key": "acceptance_coefficient", "label": "Коэф. приёмки"},
    {"key": "transit_cost", "label": "Транзит"},
    {"key": "fulfillment_services", "label": "Услуги ФФ"},
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

    def fetch_marketplace_offices(self) -> list[Mapping[str, Any]]:
        raise NotImplementedError

    def fetch_box_tariffs(self, *, tariff_date: str | None = None) -> list[Mapping[str, Any]]:
        raise NotImplementedError


class WbTransitCostEnrichmentSource(Protocol):
    def fetch_costs(
        self,
        candidates: list[Mapping[str, Any]],
        *,
        run_id: str,
        runtime_dir: Any,
        fetched_at: str,
    ) -> list[dict[str, Any]]:
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
        transit_cost_source: WbTransitCostEnrichmentSource | None = None,
        timestamp_factory: Any | None = None,
    ) -> None:
        self.runtime = runtime
        self.source = source or HttpBackedWbSuppliesSource()
        self.transit_cost_source = transit_cost_source or SellerPortalTransitCostNetworkJsonSource()
        self.timestamp_factory = timestamp_factory or _default_timestamp_factory
        self.fulfillment_overlay_provider: Callable[[], Mapping[str, Mapping[str, Any]]] | None = None
        self.ff_stock_ledger = FfStockLedgerBlock(
            runtime=self.runtime,
            timestamp_factory=self.timestamp_factory,
        )
        self._run_lock = threading.Lock()
        self._transit_cost_run_lock = threading.Lock()

    def _ensure_ff_stock_wb_auto_writeoff_checkpoint(self, *, reason: str) -> dict[str, Any]:
        return self.ff_stock_ledger.ensure_wb_supply_auto_writeoff_checkpoint(
            self.runtime.list_wb_supplies_cache_records(),
            reason=reason,
            created_by="system",
        )

    def list_supplies(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        request = _normalize_list_request(params or {})
        rows = self.runtime.list_wb_supplies()
        transit_cost_enrichments = _transit_cost_enrichment_map(self.runtime.list_wb_supply_transit_cost_enrichments())
        rows = [_row_with_transit_cost_enrichment(row, transit_cost_enrichments) for row in rows]
        rows = self._with_fulfillment_overlay(rows)
        warehouses = self.runtime.list_wb_supplies_warehouses()
        district_mapping = self.current_warehouse_district_mapping(rows=rows, warehouses=warehouses)
        rows = [augment_supply_row_with_district(row, district_mapping) for row in rows]
        state = self.runtime.load_wb_supplies_sync_state()
        active_run = self.runtime.load_active_wb_supplies_sync_run()
        active_transit_cost_run = self.runtime.load_active_wb_supply_transit_cost_enrichment_run()
        cache_completeness = _cache_completeness(state, rows)
        after_non_size_filters = [
            row
            for row in rows
            if _row_matches_search(row, request["search"])
            and _row_matches_warehouse(row, request["warehouse_id"], request["warehouse"])
            and _row_matches_districts(row, request["district_keys"])
            and _row_matches_statuses(row, request["status_ids"])
        ]
        sorted_rows = _sort_rows(after_non_size_filters, request["sort_key"], request["sort_dir"])
        filtered_rows = [
            row
            for row in sorted_rows
            if _row_matches_size_filter(row, request["size_filter"])
        ]
        offset = min(request["offset"], len(filtered_rows))
        limit = request["limit"]
        page_rows = [_row_with_display_fields(row) for row in filtered_rows[offset : offset + limit]]
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
                    "district_keys": request["district_keys"],
                    "status_id": request["status_id"],
                    "status_ids": request["status_ids"],
                    "size_filter": request["size_filter"],
                    "limit": limit,
                    "offset": offset,
                    "sort_key": request["sort_key"],
                    "sort_dir": request["sort_dir"],
                },
                "options": {
                    "warehouses": _warehouse_options(rows, warehouses),
                    "districts": district_filter_options(),
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
            "sort": {
                "key": request["sort_key"],
                "dir": request["sort_dir"],
            },
            "schema": {"columns": SCHEMA_COLUMNS},
            "rows": page_rows,
            "sync_state": _public_sync_state(state, cache_completeness),
            "active_run": active_run if active_run and active_run.get("status") in RUN_ACTIVE_STATUSES else None,
            "transit_cost_enrichment": {
                "active_run": active_transit_cost_run
                if active_transit_cost_run and active_transit_cost_run.get("status") in TRANSIT_COST_RUN_ACTIVE_STATUSES
                else None,
                "source": SELLER_PORTAL_TRANSIT_COST_SOURCE,
                "evidence_type": SELLER_PORTAL_TRANSIT_COST_EVIDENCE_TYPE,
                "read_only": True,
                "official_api": False,
            },
        }

    def build_overlay_options(self) -> dict[str, Any]:
        records = self.runtime.list_wb_supplies_cache_records()
        eligible_rows = []
        for record in records:
            normalized = record.get("normalized") if isinstance(record.get("normalized"), Mapping) else {}
            status_id = _optional_int(normalized.get("status_id"))
            if status_id in ELIGIBLE_WB_SUPPLY_STATUS_IDS:
                eligible_rows.append(dict(normalized))
        return build_wb_supply_overlay_options(
            runtime=self.runtime,
            active_skus=self._load_active_skus(),
            warehouse_district_mapping=self.current_warehouse_district_mapping(rows=eligible_rows),
        )

    def current_warehouse_district_mapping(
        self,
        *,
        rows: list[Mapping[str, Any]] | None = None,
        warehouses: list[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        cached_rows = rows if rows is not None else self.runtime.list_wb_supplies()
        cached_warehouses = warehouses if warehouses is not None else self.runtime.list_wb_supplies_warehouses()
        warnings: list[str] = []
        return self._fetch_warehouse_district_mapping(
            warehouses=cached_warehouses,
            raw_rows=cached_rows,
            warnings=warnings,
        )

    def _load_active_skus(self) -> list[tuple[int, str]]:
        current_state = self.runtime.load_current_state()
        enabled = sorted(
            [item for item in current_state.config_v2 if item.enabled],
            key=lambda item: item.display_order,
        )
        return [(int(item.nm_id), str(item.display_name)) for item in enabled]

    def sync_supplies(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        request = _normalize_sync_request(payload or {})
        if request["mode"] == SYNC_MODE_FULL_BACKFILL:
            return self.start_full_backfill(request)
        synced_at = self.timestamp_factory()
        warnings: list[str] = []
        run_id = _new_run_id()
        self.runtime.create_wb_supplies_sync_run(
            run_id=run_id,
            mode=SYNC_MODE_INCREMENTAL_REFRESH,
            status="running",
            phase="latest_window",
            started_at=synced_at,
            limit=request["limit"],
            offset=0,
            logs=[_run_log(synced_at, "latest-window sync started")],
        )
        ff_auto_writeoff_checkpoint: dict[str, Any] = {}
        try:
            ff_auto_writeoff_checkpoint = self._ensure_ff_stock_wb_auto_writeoff_checkpoint(
                reason="wb_supplies_incremental_refresh"
            )
            warehouses = self._fetch_warehouses(warnings)
            list_result = _coerce_list_result(
                self.source.list_supplies(
                    limit=request["limit"],
                    offset=0,
                    status_ids=request["status_ids"],
                    dates=request["dates"],
                )
            )
            raw_rows = list(list_result.rows)
            targeted_status_ids: list[int] = []
            targeted_raw_count = 0
            recent_historical_status_ids: list[int] = []
            recent_historical_raw_count = 0
            partial_status_slices = False
            active_reconciliation_complete = False
            active_authoritative_keys = _active_authoritative_keys_from_raw_rows(raw_rows)
            force_enrich_source_rows: list[Mapping[str, Any]] = []
            if not request["status_ids"]:
                try:
                    active_result = _coerce_list_result(
                        self.source.list_supplies(
                            limit=request["limit"],
                            offset=0,
                            status_ids=list(ACTIVE_RECONCILE_STATUS_IDS),
                            dates=request["dates"],
                        )
                    )
                    targeted_status_ids = list(ACTIVE_RECONCILE_STATUS_IDS)
                    targeted_raw_count = active_result.raw_count
                    force_enrich_source_rows.extend(active_result.rows)
                    active_authoritative_keys.update(_active_authoritative_keys_from_raw_rows(active_result.rows))
                    active_reconciliation_complete = active_result.raw_count < active_result.limit
                    partial_status_slices = not active_reconciliation_complete
                    raw_rows = _merge_raw_supply_rows(raw_rows, active_result.rows)
                except (WbSuppliesHttpStatusError, WbSuppliesTransportError, OfficialApiRuntimeError) as exc:
                    partial_status_slices = True
                    warnings.append(f"active status refresh failed; primary latest window used: {_safe_error_message(exc)}")
                try:
                    historical_result = _coerce_list_result(
                        self.source.list_supplies(
                            limit=request["limit"],
                            offset=0,
                            status_ids=list(RECENT_HISTORICAL_RECONCILE_STATUS_IDS),
                            dates=request["dates"],
                        )
                    )
                    recent_historical_status_ids = list(RECENT_HISTORICAL_RECONCILE_STATUS_IDS)
                    recent_historical_raw_count = historical_result.raw_count
                    force_enrich_source_rows.extend(historical_result.rows)
                    partial_status_slices = partial_status_slices or historical_result.raw_count >= historical_result.limit
                    raw_rows = _merge_raw_supply_rows(raw_rows, historical_result.rows)
                except (WbSuppliesHttpStatusError, WbSuppliesTransportError, OfficialApiRuntimeError) as exc:
                    partial_status_slices = True
                    warnings.append(f"recent historical status refresh failed; primary latest window used: {_safe_error_message(exc)}")
            warehouse_by_id = _warehouse_map(warehouses)
            warehouse_district_mapping = self._fetch_warehouse_district_mapping(
                warehouses=warehouses,
                raw_rows=raw_rows,
                warnings=warnings,
            )
            status_force_enrich_keys = _status_force_enrich_keys(
                force_enrich_source_rows or raw_rows,
                self.runtime.list_wb_supplies_cache_records(),
            )
            sync_result = self._prepare_list_rows_for_upsert(
                raw_rows=raw_rows,
                warehouse_by_id=warehouse_by_id,
                warehouse_district_mapping=warehouse_district_mapping,
                synced_at=synced_at,
                enrich=request["enrich"] != "none",
                changed_only=request["enrich"] != "all",
                force_enrich_cache_keys=status_force_enrich_keys,
                include_missing_enrichment=request["enrich"] == "missing_critical",
            )
            normalized_rows = sync_result["rows"]
            self.runtime.upsert_wb_supplies(
                rows=normalized_rows,
                warehouses=warehouses,
                synced_at=synced_at,
                last_successful_sync_at=synced_at,
                last_error="",
                last_limit=list_result.limit,
                last_offset=0,
                latest_synced_count=len(raw_rows),
                last_mode=SYNC_MODE_INCREMENTAL_REFRESH,
                latest_window_synced_at=synced_at,
                latest_window_limit=list_result.limit,
                latest_window_returned_count=len(raw_rows),
                may_have_more=list_result.raw_count >= list_result.limit,
            )
            deleted_active_keys: list[str] = []
            skipped_historical_absent = 0
            if active_reconciliation_complete:
                deletion_result = self._delete_absent_active_supplies(
                    active_authoritative_keys=active_authoritative_keys,
                    merged_raw_rows=raw_rows,
                )
                deleted_active_keys = deletion_result["deleted_keys"]
                skipped_historical_absent = deletion_result["skipped_historical_absent"]
            else:
                skipped_historical_absent = _count_historical_absent_from_rows(
                    self.runtime.list_wb_supplies_cache_records(),
                    raw_rows,
                )
            ff_stock_debits = self.ff_stock_ledger.record_wb_supply_debits(
                self.runtime.list_wb_supplies_cache_records()
            )
        except Exception as exc:
            block_error = _to_block_error(exc)
            self.runtime.save_wb_supplies_sync_state(
                last_synced_at=synced_at,
                last_successful_sync_at=None,
                last_error=str(block_error),
                last_limit=request["limit"],
                last_offset=0,
                latest_synced_count=0,
                last_mode=SYNC_MODE_INCREMENTAL_REFRESH,
            )
            self.runtime.update_wb_supplies_sync_run(
                run_id,
                status="failed",
                phase="failed",
                updated_at=synced_at,
                completed_at=synced_at,
                last_error=str(block_error),
                logs=[_run_log(synced_at, str(block_error))],
            )
            raise block_error from exc

        completed_at = self.timestamp_factory()
        self.runtime.update_wb_supplies_sync_run(
            run_id,
            status="success" if sync_result["failed_enrich"] == 0 else "partial",
            phase="completed",
            updated_at=completed_at,
            completed_at=completed_at,
            offset=0,
            limit=list_result.limit,
            pages_fetched=1,
            raw_fetched=len(raw_rows),
            upserted=len(normalized_rows),
            new_rows=sync_result["new_rows"],
            changed_rows=sync_result["changed_rows"],
            unchanged_rows=sync_result["unchanged_rows"],
            enriched=sync_result["enriched"],
            failed_enrich=sync_result["failed_enrich"],
            may_have_more=list_result.raw_count >= list_result.limit,
            logs=[
                _run_log(completed_at, "latest-window sync completed"),
                *[
                    _run_log(completed_at, f"deleted active supply {cache_key} absent from WB active status slice")
                    for cache_key in deleted_active_keys[:50]
                ],
            ],
        )
        list_params = request.get("list_params") if isinstance(request.get("list_params"), Mapping) else {}
        response = self.list_supplies(list_params or {
            "limit": DEFAULT_PAGE_LIMIT,
            "offset": 0,
            "size_filter": SIZE_FILTER_MAIN_250,
            "sort_key": DEFAULT_SORT_KEY,
            "sort_dir": DEFAULT_SORT_DIR,
        })
        response["sync"] = {
            "status": "ok",
            "mode": SYNC_MODE_INCREMENTAL_REFRESH,
            "run_id": run_id,
            "synced_at": synced_at,
            "limit": list_result.limit,
            "offset": 0,
            "raw_fetched_count": list_result.raw_count,
            "fetched_default": list_result.raw_count,
            "fetched_active_statuses": targeted_raw_count,
            "fetched_recent_historical_statuses": recent_historical_raw_count,
            "raw_merged_count": len(raw_rows),
            "targeted_status_ids": targeted_status_ids,
            "targeted_raw_fetched_count": targeted_raw_count,
            "recent_historical_status_ids": recent_historical_status_ids,
            "recent_historical_raw_fetched_count": recent_historical_raw_count,
            "upserted_count": len(normalized_rows),
            "new_rows": sync_result["new_rows"],
            "changed_rows": sync_result["changed_rows"],
            "unchanged_rows": sync_result["unchanged_rows"],
            "changed_active_rows": sync_result["changed_active_rows"],
            "enriched_active_rows": sync_result["enriched_active_rows"],
            "forced_status_refresh_rows": sync_result["forced_status_refresh_rows"],
            "refreshed_recent_historical_rows": sync_result["refreshed_recent_historical_rows"],
            "accepted_qty_changed_rows": sync_result["accepted_qty_changed_rows"],
            "enriched": sync_result["enriched"],
            "failed_enrich": sync_result["failed_enrich"],
            "deleted_active_rows": len(deleted_active_keys),
            "deleted_active_keys": deleted_active_keys[:50],
            "skipped_historical_absent": skipped_historical_absent,
            "partial_status_slices": partial_status_slices,
            "active_reconciliation_complete": active_reconciliation_complete,
            "may_have_more": list_result.raw_count >= list_result.limit,
            "latest_window_only": True,
            "enrich": request["enrich"],
            "ff_stock_debits": ff_stock_debits,
            "ff_auto_writeoff_checkpoint": ff_auto_writeoff_checkpoint,
            "warnings": warnings,
        }
        return response

    def start_full_backfill(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        request = _normalize_backfill_request(payload or {})
        with self._run_lock:
            active_run = self.runtime.load_active_wb_supplies_sync_run()
            if active_run:
                return {
                    "contract_name": CONTRACT_NAME,
                    "contract_version": CONTRACT_VERSION,
                    "status": active_run.get("status") or "running",
                    "accepted": True,
                    "run_id": active_run.get("run_id"),
                    "active_run": active_run,
                    "sync_state": self.runtime.load_wb_supplies_sync_state(),
                }
            run_id = _new_run_id()
            queued_at = self.timestamp_factory()
            run = self.runtime.create_wb_supplies_sync_run(
                run_id=run_id,
                mode=SYNC_MODE_FULL_BACKFILL,
                status="queued",
                phase="queued",
                started_at=queued_at,
                limit=request["limit"],
                offset=request["start_offset"],
                logs=[_run_log(queued_at, "full backfill queued")],
            )
            thread = threading.Thread(
                target=self._run_full_backfill_guarded,
                args=(run_id, request),
                name=f"wb-supplies-backfill-{run_id[:8]}",
                daemon=True,
            )
            thread.start()
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "queued",
            "accepted": True,
            "run_id": run_id,
            "active_run": run,
            "sync_state": self.runtime.load_wb_supplies_sync_state(),
        }

    def run_full_backfill(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        request = _normalize_backfill_request(payload or {})
        run_id = str(request.get("run_id") or _new_run_id())
        started_at = self.timestamp_factory()
        self.runtime.create_wb_supplies_sync_run(
            run_id=run_id,
            mode=SYNC_MODE_FULL_BACKFILL,
            status="running",
            phase="starting",
            started_at=started_at,
            limit=request["limit"],
            offset=request["start_offset"],
            logs=[_run_log(started_at, "full backfill started")],
        )
        return self._run_full_backfill(run_id, request)

    def get_sync_status(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        run_id = str((params or {}).get("run_id") or "").strip()
        run = self.runtime.load_wb_supplies_sync_run(run_id) if run_id else self.runtime.load_active_wb_supplies_sync_run()
        state = self.runtime.load_wb_supplies_sync_state()
        rows = self.runtime.list_wb_supplies()
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "run": run,
            "sync_state": _public_sync_state(state, _cache_completeness(state, rows)),
            "cached_total_rows": len(rows),
        }

    def start_transit_cost_enrichment(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        request = _normalize_transit_cost_enrichment_request(payload or {})
        with self._transit_cost_run_lock:
            active_run = self.runtime.load_active_wb_supply_transit_cost_enrichment_run()
            if active_run:
                return {
                    "contract_name": CONTRACT_NAME,
                    "contract_version": CONTRACT_VERSION,
                    "status": active_run.get("status") or "running",
                    "accepted": True,
                    "run_id": active_run.get("run_id"),
                    "candidate_count": active_run.get("candidate_count") or 0,
                    "started_at": active_run.get("started_at") or "",
                    "active_run": active_run,
                }
            candidates = self._select_transit_cost_enrichment_candidates(request)
            run_id = _new_transit_cost_run_id()
            queued_at = self.timestamp_factory()
            run = self.runtime.create_wb_supply_transit_cost_enrichment_run(
                run_id=run_id,
                status="queued",
                phase="queued",
                started_at=queued_at,
                candidate_count=len(candidates),
                logs=[
                    _run_log(
                        queued_at,
                        f"Seller Portal transit cost enrichment queued; candidates={len(candidates)}",
                    )
                ],
            )
            if not candidates:
                completed = self.timestamp_factory()
                run = self.runtime.update_wb_supply_transit_cost_enrichment_run(
                    run_id,
                    status="success",
                    phase="no_candidates",
                    updated_at=completed,
                    completed_at=completed,
                    logs=[_run_log(completed, "no missing transit cost candidates")],
                )
            else:
                thread = threading.Thread(
                    target=self._run_transit_cost_enrichment_guarded,
                    args=(run_id, candidates),
                    name=f"wb-transit-cost-{run_id[:8]}",
                    daemon=True,
                )
                thread.start()
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": run.get("status") or "queued",
            "accepted": True,
            "run_id": run_id,
            "candidate_count": len(candidates),
            "started_at": queued_at,
            "active_run": run,
        }

    def get_transit_cost_enrichment_status(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        run_id = str((params or {}).get("run_id") or "").strip()
        run = (
            self.runtime.load_wb_supply_transit_cost_enrichment_run(run_id)
            if run_id
            else self.runtime.load_active_wb_supply_transit_cost_enrichment_run()
        )
        lock_status: dict[str, Any] = {}
        try:
            from apps.seller_portal_automation_guard import current_lock_status

            lock_status = current_lock_status(self.runtime.runtime_dir)
        except Exception:
            lock_status = {}
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "run": run,
            "lock_status": lock_status,
            "source": SELLER_PORTAL_TRANSIT_COST_SOURCE,
            "evidence_type": SELLER_PORTAL_TRANSIT_COST_EVIDENCE_TYPE,
            "read_only": True,
            "official_api": False,
        }

    def get_supply(self, supply_id: str) -> dict[str, Any]:
        normalized_id = str(supply_id or "").strip()
        if not normalized_id:
            raise WbSuppliesBlockError("supply_id is required", http_status=400)
        record = self.runtime.load_wb_supply_record(normalized_id)
        if record is None:
            raise WbSuppliesBlockError(f"WB supply not found in cache: {normalized_id}", http_status=404)
        record = self._ensure_supply_detail_record(record)
        detail = _supply_detail_payload(record)
        transit_cost = self.runtime.load_wb_supply_transit_cost_enrichment(normalized_id)
        if isinstance(detail.get("supply"), Mapping):
            detail["supply"] = _row_with_transit_cost_enrichment(
                detail["supply"],
                _transit_cost_enrichment_map([transit_cost] if transit_cost else []),
            )
        if isinstance(detail.get("supply"), Mapping):
            detail["supply"] = self._with_fulfillment_overlay([detail["supply"]])[0]
            detail["supply"] = _row_with_display_fields(detail["supply"])
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "meta": {"source": WB_SUPPLIES_SOURCE_LABEL, "read_only": True},
            **detail,
        }

    def _with_fulfillment_overlay(self, rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        if not self.fulfillment_overlay_provider:
            return [_row_with_fulfillment_overlay(row, {}) for row in rows]
        try:
            overlay = self.fulfillment_overlay_provider()
        except Exception:
            overlay = {}
        return [_row_with_fulfillment_overlay(row, overlay) for row in rows]

    def _ensure_supply_detail_record(self, record: Mapping[str, Any]) -> dict[str, Any]:
        normalized = dict(record.get("normalized") or {})
        raw_list = record.get("raw_list") if isinstance(record.get("raw_list"), Mapping) else normalized.get("raw_list")
        if not isinstance(raw_list, Mapping):
            raw_list = {}
        raw_detail = record.get("raw_detail") if isinstance(record.get("raw_detail"), Mapping) else None
        raw_goods = record.get("raw_goods") if isinstance(record.get("raw_goods"), list) else None
        raw_package = record.get("raw_package") if isinstance(record.get("raw_package"), list) else None
        lookup_id, is_preorder_id = _resolve_upstream_lookup_id_from_sources(raw_detail, raw_list, normalized)
        warnings: list[str] = []
        fetched_any = False
        attempted = False
        attempted_core_enrichment = False

        if lookup_id and raw_detail is None:
            attempted = True
            attempted_core_enrichment = True
            fetched_detail = self._fetch_detail(lookup_id, is_preorder_id=is_preorder_id, warnings=warnings)
            if fetched_detail is not None:
                raw_detail = fetched_detail
                fetched_any = True
        if lookup_id and raw_goods is None:
            attempted = True
            attempted_core_enrichment = True
            fetched_goods = self._fetch_goods(lookup_id, is_preorder_id=is_preorder_id, warnings=warnings)
            if fetched_goods is not None:
                raw_goods = fetched_goods
                fetched_any = True
        if lookup_id and not is_preorder_id and raw_package is None:
            attempted = True
            fetched_package = self._fetch_package(lookup_id, warnings=warnings)
            if fetched_package is not None:
                raw_package = fetched_package
                fetched_any = True

        synced_at = self.timestamp_factory()
        warehouse_rows = self.runtime.list_wb_supplies_warehouses()
        warehouse_by_id = _warehouse_map(warehouse_rows)
        warehouse_district_mapping = self._cached_warehouse_district_mapping(
            rows=[normalized],
            warehouses=warehouse_rows,
        )
        existing_warnings = list(normalized.get("warnings") or [])
        row_warnings = warnings if attempted_core_enrichment or warnings else existing_warnings
        next_normalized = _normalize_supply_row(
            raw_list=raw_list,
            raw_detail=raw_detail,
            raw_goods=raw_goods,
            raw_package=raw_package,
            warehouse_by_id=warehouse_by_id,
            synced_at=synced_at,
            warnings=row_warnings,
        )
        next_normalized = augment_supply_row_with_district(next_normalized, warehouse_district_mapping)
        cache_key = str(record.get("cache_key") or normalized.get("cache_key") or _stable_cache_key(raw_list) or "").strip()
        next_normalized["cache_key"] = cache_key
        next_normalized["raw_list_hash"] = _stable_payload_hash(raw_list) if raw_list else str(record.get("raw_list_hash") or "")
        next_normalized["raw_detail_hash"] = _stable_payload_hash(raw_detail) if raw_detail is not None else str(record.get("raw_detail_hash") or "")
        next_normalized["raw_goods_hash"] = _stable_payload_hash(raw_goods) if raw_goods is not None else str(record.get("raw_goods_hash") or "")
        next_normalized["raw_package_hash"] = _stable_payload_hash(raw_package) if raw_package is not None else str(record.get("raw_package_hash") or "")
        next_normalized["last_list_synced_at"] = str(normalized.get("last_list_synced_at") or "")
        previous_enriched_at = str(record.get("last_enriched_at") or normalized.get("last_enriched_at") or "")
        next_normalized["last_enriched_at"] = synced_at if attempted_core_enrichment and fetched_any and not warnings else previous_enriched_at
        if attempted and warnings:
            next_normalized["enrichment_status"] = "partial" if fetched_any else "failed"
        elif attempted_core_enrichment:
            next_normalized["enrichment_status"] = "ok"
        else:
            next_normalized["enrichment_status"] = str(record.get("enrichment_status") or normalized.get("enrichment_status") or "not_requested")
        next_normalized["enrichment_error"] = "; ".join(row_warnings) if row_warnings else ""

        next_record = {
            **dict(record),
            "normalized": next_normalized,
            "raw_list": dict(raw_list) if raw_list else None,
            "raw_detail": dict(raw_detail) if isinstance(raw_detail, Mapping) else None,
            "raw_goods": [dict(item) for item in raw_goods] if isinstance(raw_goods, list) else None,
            "raw_package": [dict(item) for item in raw_package] if isinstance(raw_package, list) else None,
            "raw_list_hash": next_normalized["raw_list_hash"],
            "raw_detail_hash": next_normalized["raw_detail_hash"],
            "raw_goods_hash": next_normalized["raw_goods_hash"],
            "raw_package_hash": next_normalized["raw_package_hash"],
            "last_enriched_at": next_normalized["last_enriched_at"],
            "enrichment_status": next_normalized["enrichment_status"],
            "enrichment_error": next_normalized["enrichment_error"],
        }
        if fetched_any or _normalized_row_public_fingerprint(normalized) != _normalized_row_public_fingerprint(next_normalized):
            self.runtime.save_wb_supply_rows(rows=[next_normalized], warehouses=[], synced_at=synced_at)
        self._ensure_ff_stock_wb_auto_writeoff_checkpoint(reason="wb_supply_detail_enrichment")
        self.ff_stock_ledger.record_wb_supply_debit(next_record)
        return next_record

    def _select_transit_cost_enrichment_candidates(self, request: Mapping[str, Any]) -> list[dict[str, Any]]:
        rows = self.runtime.list_wb_supplies()
        warehouses = self.runtime.list_wb_supplies_warehouses()
        district_mapping = self._cached_warehouse_district_mapping(rows=rows, warehouses=warehouses)
        rows = [augment_supply_row_with_district(row, district_mapping) for row in rows]
        enrichments = _transit_cost_enrichment_map(self.runtime.list_wb_supply_transit_cost_enrichments())
        force = bool(request.get("force"))
        explicit_ids = [str(item).strip() for item in request.get("supply_ids") or [] if str(item or "").strip()]
        limit = int(request.get("limit") or DEFAULT_TRANSIT_COST_ENRICHMENT_LIMIT)
        if explicit_ids:
            wanted = set(explicit_ids)
            scope = [
                row
                for row in rows
                if wanted.intersection(_row_identity_values(row))
            ]
        else:
            list_params = request.get("list_params") if isinstance(request.get("list_params"), Mapping) else {}
            if list_params:
                list_request = _normalize_list_request(list_params)
                after_filters = [
                    row
                    for row in rows
                    if _row_matches_search(row, list_request["search"])
                    and _row_matches_warehouse(row, list_request["warehouse_id"], list_request["warehouse"])
                    and _row_matches_districts(row, list_request["district_keys"])
                    and _row_matches_statuses(row, list_request["status_ids"])
                    and _row_matches_size_filter(row, list_request["size_filter"])
                ]
                sorted_rows = _sort_rows(after_filters, list_request["sort_key"], list_request["sort_dir"])
                offset = min(list_request["offset"], len(sorted_rows))
                scope = sorted_rows[offset : offset + list_request["limit"]]
            else:
                scope = _sort_rows(rows, DEFAULT_SORT_KEY, DEFAULT_SORT_DIR)
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in scope:
            supply_id = _transit_cost_supply_id(row)
            if not supply_id or supply_id in seen:
                continue
            if not _is_transit_cost_enrichment_candidate(row):
                continue
            if not force and _has_fresh_success_transit_cost(row, enrichments, now_text=self.timestamp_factory()):
                continue
            candidates.append(
                {
                    "supply_id": supply_id,
                    "warehouse_display": str(row.get("warehouse_display") or ""),
                    "supply_date": str(row.get("supply_date") or ""),
                }
            )
            seen.add(supply_id)
            if len(candidates) >= limit:
                break
        return candidates

    def _run_transit_cost_enrichment_guarded(self, run_id: str, candidates: list[Mapping[str, Any]]) -> None:
        try:
            self._run_transit_cost_enrichment(run_id, candidates)
        except Exception as exc:  # noqa: BLE001 - background job must persist controlled failure.
            failed_at = self.timestamp_factory()
            error = _safe_error_message(exc)
            self.runtime.update_wb_supply_transit_cost_enrichment_run(
                run_id,
                status="failed",
                phase="failed",
                updated_at=failed_at,
                completed_at=failed_at,
                last_error=error,
                logs=[_run_log(failed_at, error)],
            )

    def _run_transit_cost_enrichment(self, run_id: str, candidates: list[Mapping[str, Any]]) -> dict[str, Any]:
        started_at = self.timestamp_factory()
        logs = [_run_log(started_at, f"Seller Portal transit cost enrichment started; candidates={len(candidates)}")]
        self.runtime.update_wb_supply_transit_cost_enrichment_run(
            run_id,
            status="running",
            phase="browser_network_json",
            updated_at=started_at,
            candidate_count=len(candidates),
            logs=logs,
        )
        results: list[dict[str, Any]]
        try:
            results = self.transit_cost_source.fetch_costs(
                list(candidates),
                run_id=run_id,
                runtime_dir=self.runtime.runtime_dir,
                fetched_at=started_at,
            )
        except SellerPortalTransitCostSourceError as exc:
            status = "session_expired" if exc.code == "session_expired" else "failed"
            fetched_at = self.timestamp_factory()
            results = [
                {
                    "supply_id": str(candidate.get("supply_id") or ""),
                    "amount": None,
                    "currency": "RUB",
                    "amount_label": "",
                    "is_transit": True,
                    "source": SELLER_PORTAL_TRANSIT_COST_SOURCE,
                    "evidence_type": SELLER_PORTAL_TRANSIT_COST_EVIDENCE_TYPE,
                    "confidence": "none",
                    "fetched_at": fetched_at,
                    "status": status,
                    "error": str(exc),
                    "source_endpoint_path": SELLER_PORTAL_SUPPLY_COST_ENDPOINT_PATH,
                }
                for candidate in candidates
            ]
            logs.append(_run_log(fetched_at, f"Seller Portal transit cost source failed: {exc.code}"))
        counters = {
            "processed_count": 0,
            "success_count": 0,
            "not_found_count": 0,
            "failed_count": 0,
            "session_expired_count": 0,
        }
        updated_at = self.timestamp_factory()
        for result in results:
            supply_id = str(result.get("supply_id") or "").strip()
            if not supply_id:
                continue
            status = str(result.get("status") or "failed")
            record = {
                **result,
                "created_at": updated_at,
                "updated_at": updated_at,
                "source": SELLER_PORTAL_TRANSIT_COST_SOURCE,
                "evidence_type": SELLER_PORTAL_TRANSIT_COST_EVIDENCE_TYPE,
                "source_endpoint_path": str(result.get("source_endpoint_path") or SELLER_PORTAL_SUPPLY_COST_ENDPOINT_PATH),
            }
            self.runtime.upsert_wb_supply_transit_cost_enrichment(record)
            counters["processed_count"] += 1
            if status == "success":
                counters["success_count"] += 1
            elif status == "not_found":
                counters["not_found_count"] += 1
            elif status == "session_expired":
                counters["session_expired_count"] += 1
            else:
                counters["failed_count"] += 1
            self.runtime.update_wb_supply_transit_cost_enrichment_run(
                run_id,
                updated_at=self.timestamp_factory(),
                **counters,
            )
        completed_at = self.timestamp_factory()
        if counters["session_expired_count"]:
            status = "session_expired"
        elif counters["failed_count"] or counters["not_found_count"]:
            status = "partial" if counters["success_count"] else "failed"
        else:
            status = "success"
        logs.append(
            _run_log(
                completed_at,
                "Seller Portal transit cost enrichment completed: "
                f"success={counters['success_count']} not_found={counters['not_found_count']} "
                f"failed={counters['failed_count']} session_expired={counters['session_expired_count']}",
            )
        )
        return self.runtime.update_wb_supply_transit_cost_enrichment_run(
            run_id,
            status=status,
            phase="completed",
            updated_at=completed_at,
            completed_at=completed_at,
            candidate_count=len(candidates),
            last_error="" if status == "success" else _last_error(results),
            logs=logs,
            **counters,
        )

    def _run_full_backfill_guarded(self, run_id: str, request: Mapping[str, Any]) -> None:
        try:
            self._run_full_backfill(run_id, request)
        except Exception as exc:  # noqa: BLE001 - background job must persist controlled failure.
            failed_at = self.timestamp_factory()
            block_error = _to_block_error(exc)
            self.runtime.update_wb_supplies_sync_run(
                run_id,
                status="failed",
                phase="failed",
                updated_at=failed_at,
                completed_at=failed_at,
                last_error=str(block_error),
                logs=[_run_log(failed_at, str(block_error))],
            )
            self.runtime.save_wb_supplies_sync_state(
                last_synced_at=failed_at,
                last_successful_sync_at=None,
                last_error=str(block_error),
                last_limit=int(request.get("limit") or DEFAULT_SYNC_LIMIT),
                last_offset=int(request.get("start_offset") or 0),
                latest_synced_count=0,
                last_mode=SYNC_MODE_FULL_BACKFILL,
                backfill_complete=False,
                may_have_more=True,
            )

    def _run_full_backfill(self, run_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        started_at = self.timestamp_factory()
        limit = int(request.get("limit") or DEFAULT_SYNC_LIMIT)
        state = self.runtime.load_wb_supplies_sync_state()
        requested_offset = int(request.get("start_offset") or 0)
        if request.get("resume") and requested_offset == 0 and not bool(state.get("backfill_complete")):
            offset = int(state.get("highest_synced_offset") or 0)
        else:
            offset = requested_offset
        max_pages = _optional_int(request.get("max_pages"))
        enrich = bool(request.get("enrich"))
        warnings: list[str] = []
        counters = {
            "pages_fetched": 0,
            "raw_fetched": 0,
            "upserted": 0,
            "new_rows": 0,
            "changed_rows": 0,
            "unchanged_rows": 0,
            "enriched": 0,
            "failed_enrich": 0,
        }
        logs = [_run_log(started_at, f"full backfill started at offset {offset}")]
        self._ensure_ff_stock_wb_auto_writeoff_checkpoint(reason="wb_supplies_full_backfill")
        self.runtime.update_wb_supplies_sync_run(
            run_id,
            status="running",
            phase="fetching",
            updated_at=started_at,
            offset=offset,
            limit=limit,
            logs=logs,
        )
        try:
            warehouses = self._fetch_warehouses(warnings)
            warehouse_by_id = _warehouse_map(warehouses)
            warehouse_district_mapping = self._fetch_warehouse_district_mapping(
                warehouses=warehouses,
                raw_rows=[],
                warnings=warnings,
            )
            backfill_complete = False
            may_have_more = True
            while True:
                if max_pages is not None and counters["pages_fetched"] >= max_pages:
                    logs.append(_run_log(self.timestamp_factory(), f"stopped by max_pages={max_pages} at offset {offset}"))
                    may_have_more = True
                    break
                page_started_at = self.timestamp_factory()
                list_result = self._fetch_list_page_with_retry(limit=limit, offset=offset)
                rows = list_result.rows
                returned_count = list_result.raw_count
                counters["pages_fetched"] += 1
                counters["raw_fetched"] += returned_count
                if returned_count == 0:
                    backfill_complete = True
                    may_have_more = False
                    logs.append(_run_log(page_started_at, f"offset {offset}: empty page, history complete"))
                    self.runtime.save_wb_supplies_sync_state(
                        last_synced_at=page_started_at,
                        last_successful_sync_at=page_started_at,
                        last_error="",
                        last_limit=limit,
                        last_offset=offset,
                        latest_synced_count=0,
                        last_mode=SYNC_MODE_FULL_BACKFILL,
                        backfill_complete=True,
                        backfill_started_at=started_at,
                        backfill_completed_at=page_started_at,
                        highest_synced_offset=offset,
                        last_successful_offset=offset,
                        may_have_more=False,
                    )
                    break
                sync_result = self._prepare_list_rows_for_upsert(
                    raw_rows=rows,
                    warehouse_by_id=warehouse_by_id,
                    warehouse_district_mapping=warehouse_district_mapping,
                    synced_at=page_started_at,
                    enrich=False,
                    changed_only=True,
                    include_missing_enrichment=False,
                )
                normalized_rows = sync_result["rows"]
                for key in ("new_rows", "changed_rows", "unchanged_rows", "enriched", "failed_enrich"):
                    counters[key] += int(sync_result[key])
                counters["upserted"] += len(normalized_rows)
                next_offset = offset + returned_count
                may_have_more = returned_count >= limit
                backfill_complete = not may_have_more
                self.runtime.upsert_wb_supplies(
                    rows=normalized_rows,
                    warehouses=warehouses,
                    synced_at=page_started_at,
                    last_successful_sync_at=page_started_at,
                    last_error="",
                    last_limit=limit,
                    last_offset=offset,
                    latest_synced_count=returned_count,
                    last_mode=SYNC_MODE_FULL_BACKFILL,
                    backfill_started_at=started_at,
                    backfill_completed_at=page_started_at if backfill_complete else None,
                    highest_synced_offset=next_offset,
                    last_successful_offset=offset,
                    may_have_more=may_have_more,
                    backfill_complete=backfill_complete,
                )
                if enrich and sync_result["touched_cache_keys"]:
                    enrich_started_at = self.timestamp_factory()
                    enrich_result = self._prepare_list_rows_for_upsert(
                        raw_rows=rows,
                        warehouse_by_id=warehouse_by_id,
                        warehouse_district_mapping=warehouse_district_mapping,
                        synced_at=enrich_started_at,
                        enrich=True,
                        changed_only=True,
                        force_enrich_cache_keys=set(sync_result["touched_cache_keys"]),
                        include_missing_enrichment=False,
                    )
                    enriched_rows = enrich_result["rows"]
                    counters["enriched"] += int(enrich_result["enriched"])
                    counters["failed_enrich"] += int(enrich_result["failed_enrich"])
                    counters["upserted"] += len(enriched_rows)
                    if enriched_rows:
                        self.runtime.upsert_wb_supplies(
                            rows=enriched_rows,
                            warehouses=[],
                            synced_at=enrich_started_at,
                            last_successful_sync_at=enrich_started_at,
                            last_error="",
                            last_limit=limit,
                            last_offset=offset,
                            latest_synced_count=returned_count,
                            last_mode=SYNC_MODE_FULL_BACKFILL,
                            backfill_started_at=started_at,
                            backfill_completed_at=page_started_at if backfill_complete else None,
                            highest_synced_offset=next_offset,
                            last_successful_offset=offset,
                            may_have_more=may_have_more,
                            backfill_complete=backfill_complete,
                        )
                logs = (logs + [_run_log(page_started_at, f"offset {offset}: fetched {returned_count}, upserted {len(normalized_rows)}")])[-20:]
                self.runtime.update_wb_supplies_sync_run(
                    run_id,
                    status="running",
                    phase="fetching" if may_have_more else "completing",
                    updated_at=page_started_at,
                    offset=next_offset,
                    limit=limit,
                    pages_fetched=counters["pages_fetched"],
                    raw_fetched=counters["raw_fetched"],
                    upserted=counters["upserted"],
                    new_rows=counters["new_rows"],
                    changed_rows=counters["changed_rows"],
                    unchanged_rows=counters["unchanged_rows"],
                    enriched=counters["enriched"],
                    failed_enrich=counters["failed_enrich"],
                    may_have_more=may_have_more,
                    logs=logs,
                )
                offset = next_offset
                if backfill_complete:
                    break
        except Exception as exc:
            failed_at = self.timestamp_factory()
            block_error = _to_block_error(exc)
            logs = (logs + [_run_log(failed_at, str(block_error))])[-20:]
            self.runtime.save_wb_supplies_sync_state(
                last_synced_at=failed_at,
                last_successful_sync_at=None,
                last_error=str(block_error),
                last_limit=limit,
                last_offset=offset,
                latest_synced_count=0,
                last_mode=SYNC_MODE_FULL_BACKFILL,
                backfill_started_at=started_at,
                highest_synced_offset=offset,
                may_have_more=True,
                backfill_complete=False,
            )
            status = "partial" if counters["raw_fetched"] > 0 else "failed"
            return self.runtime.update_wb_supplies_sync_run(
                run_id,
                status=status,
                phase="failed",
                updated_at=failed_at,
                completed_at=failed_at,
                offset=offset,
                limit=limit,
                pages_fetched=counters["pages_fetched"],
                raw_fetched=counters["raw_fetched"],
                upserted=counters["upserted"],
                new_rows=counters["new_rows"],
                changed_rows=counters["changed_rows"],
                unchanged_rows=counters["unchanged_rows"],
                enriched=counters["enriched"],
                failed_enrich=counters["failed_enrich"],
                may_have_more=True,
                last_error=str(block_error),
                logs=logs,
            )
        completed_at = self.timestamp_factory()
        status = "success" if backfill_complete and counters["failed_enrich"] == 0 else "partial"
        ff_stock_debits = self.ff_stock_ledger.record_wb_supply_debits(
            self.runtime.list_wb_supplies_cache_records()
        )
        if backfill_complete:
            self.runtime.save_wb_supplies_sync_state(
                last_synced_at=completed_at,
                last_successful_sync_at=completed_at,
                last_error="",
                last_limit=limit,
                last_offset=offset,
                latest_synced_count=0,
                last_mode=SYNC_MODE_FULL_BACKFILL,
                backfill_started_at=started_at,
                backfill_completed_at=completed_at,
                highest_synced_offset=offset,
                last_successful_offset=offset,
                may_have_more=False,
                backfill_complete=True,
            )
        logs = (
            logs
            + [
                _run_log(completed_at, f"full backfill {status}"),
                _run_log(completed_at, f"ФФ stock debits created={ff_stock_debits.get('created_count', 0)}"),
            ]
        )[-20:]
        return self.runtime.update_wb_supplies_sync_run(
            run_id,
            status=status,
            phase="completed" if backfill_complete else "partial",
            updated_at=completed_at,
            completed_at=completed_at,
            offset=offset,
            limit=limit,
            pages_fetched=counters["pages_fetched"],
            raw_fetched=counters["raw_fetched"],
            upserted=counters["upserted"],
            new_rows=counters["new_rows"],
            changed_rows=counters["changed_rows"],
            unchanged_rows=counters["unchanged_rows"],
            enriched=counters["enriched"],
            failed_enrich=counters["failed_enrich"],
            may_have_more=may_have_more,
            logs=logs,
        )

    def _fetch_list_page_with_retry(self, *, limit: int, offset: int) -> WbSuppliesListResult:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return _coerce_list_result(self.source.list_supplies(limit=limit, offset=offset))
            except WbSuppliesHttpStatusError as exc:
                last_error = exc
                if exc.status_code != 429 and exc.status_code < 500:
                    break
            except WbSuppliesTransportError as exc:
                last_error = exc
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
        if last_error:
            raise last_error
        raise WbSuppliesBlockError("WB supplies list page fetch failed", http_status=502)

    def _delete_absent_active_supplies(
        self,
        *,
        active_authoritative_keys: set[str],
        merged_raw_rows: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        raw_keys = _raw_identity_keys_from_rows(merged_raw_rows)
        delete_keys: list[str] = []
        skipped_historical_absent = 0
        for record in self.runtime.list_wb_supplies_cache_records():
            normalized = record.get("normalized") if isinstance(record.get("normalized"), Mapping) else {}
            status_id = _optional_int(normalized.get("status_id"))
            record_keys = _record_identity_keys(record)
            if status_id in ACTIVE_RECONCILE_STATUS_IDS:
                if record_keys and not (record_keys & active_authoritative_keys) and not (record_keys & raw_keys):
                    delete_keys.append(_record_primary_delete_key(record))
            elif status_id in HISTORICAL_STATUS_IDS and record_keys and not (record_keys & raw_keys):
                skipped_historical_absent += 1
        deleted_count = self.runtime.delete_wb_supply_records(delete_keys)
        return {
            "deleted_count": deleted_count,
            "deleted_keys": delete_keys[:deleted_count] if deleted_count < len(delete_keys) else delete_keys,
            "skipped_historical_absent": skipped_historical_absent,
        }

    def _prepare_list_rows_for_upsert(
        self,
        *,
        raw_rows: list[Mapping[str, Any]],
        warehouse_by_id: Mapping[str, str],
        warehouse_district_mapping: Mapping[str, Any],
        synced_at: str,
        enrich: bool,
        changed_only: bool,
        force_enrich_cache_keys: set[str] | None = None,
        include_missing_enrichment: bool = False,
    ) -> dict[str, Any]:
        records = self.runtime.list_wb_supplies_cache_records()
        existing_by_key = _cache_record_index(records)
        rows_to_upsert: list[dict[str, Any]] = []
        counters = {
            "new_rows": 0,
            "changed_rows": 0,
            "unchanged_rows": 0,
            "changed_active_rows": 0,
            "enriched_active_rows": 0,
            "forced_status_refresh_rows": 0,
            "refreshed_recent_historical_rows": 0,
            "accepted_qty_changed_rows": 0,
            "enriched": 0,
            "failed_enrich": 0,
        }
        for raw_row in raw_rows:
            cache_key = _stable_cache_key(raw_row)
            lookup_id, is_preorder_id = _resolve_upstream_lookup_id(raw_row)
            existing = existing_by_key.get(cache_key) or existing_by_key.get(lookup_id)
            raw_list_hash = _stable_payload_hash(raw_row)
            raw_updated_date = _first_string(raw_row, "updatedDate", "updated_at", "updated_date")
            raw_status_id = _optional_int(_first_value(raw_row, "statusID", "status_id"))
            existing_hash = str((existing or {}).get("raw_list_hash") or "")
            existing_updated_date = str(((existing or {}).get("normalized") or {}).get("updated_date") or "")
            force_enrich = bool(force_enrich_cache_keys and cache_key in force_enrich_cache_keys)
            is_new = existing is None
            existing_normalized = (existing or {}).get("normalized") or {}
            existing_status_id = _optional_int(existing_normalized.get("status_id"))
            is_changed = (not is_new) and (
                not existing_hash
                or existing_hash != raw_list_hash
                or (raw_updated_date and raw_updated_date != existing_updated_date)
                or (raw_status_id is not None and existing_status_id is not None and raw_status_id != existing_status_id)
            )
            needs_enrichment = bool(
                include_missing_enrichment and existing and _row_needs_enrichment(existing.get("normalized") or {})
            )
            if is_new:
                counters["new_rows"] += 1
                if raw_status_id in ACTIVE_RECONCILE_STATUS_IDS:
                    counters["changed_active_rows"] += 1
            elif is_changed or needs_enrichment:
                counters["changed_rows"] += 1
                if raw_status_id in ACTIVE_RECONCILE_STATUS_IDS:
                    counters["changed_active_rows"] += 1
            else:
                counters["unchanged_rows"] += 1
                if changed_only and not force_enrich:
                    continue
            raw_detail = (existing or {}).get("raw_detail")
            raw_goods = (existing or {}).get("raw_goods")
            raw_package = (existing or {}).get("raw_package")
            row_warnings: list[str] = []
            attempted_enrichment = bool(
                enrich and lookup_id and (force_enrich or is_new or is_changed or needs_enrichment or not changed_only)
            )
            if attempted_enrichment:
                counters["forced_status_refresh_rows"] += 1 if force_enrich else 0
                counters["refreshed_recent_historical_rows"] += 1 if force_enrich and raw_status_id in HISTORICAL_STATUS_IDS else 0
                fetched_detail = self._fetch_detail(lookup_id, is_preorder_id=is_preorder_id, warnings=row_warnings)
                fetched_goods = self._fetch_goods(lookup_id, is_preorder_id=is_preorder_id, warnings=row_warnings)
                if fetched_detail is not None:
                    raw_detail = fetched_detail
                if fetched_goods is not None:
                    raw_goods = fetched_goods
            if (
                not is_new
                and not is_changed
                and not needs_enrichment
                and attempted_enrichment
                and _enriched_evidence_changed(existing or {}, raw_detail, raw_goods, raw_package)
            ):
                counters["unchanged_rows"] = max(0, counters["unchanged_rows"] - 1)
                counters["changed_rows"] += 1
                if raw_status_id in ACTIVE_RECONCILE_STATUS_IDS:
                    counters["changed_active_rows"] += 1
            failed_enrich = bool(row_warnings and attempted_enrichment)
            if attempted_enrichment and not failed_enrich:
                counters["enriched"] += 1
                if raw_status_id in ACTIVE_RECONCILE_STATUS_IDS:
                    counters["enriched_active_rows"] += 1
            if failed_enrich:
                counters["failed_enrich"] += 1
            normalized = _normalize_supply_row(
                raw_list=raw_row,
                raw_detail=raw_detail if isinstance(raw_detail, Mapping) else None,
                raw_goods=raw_goods if isinstance(raw_goods, list) else None,
                raw_package=raw_package if isinstance(raw_package, list) else None,
                warehouse_by_id=warehouse_by_id,
                synced_at=synced_at,
                warnings=row_warnings,
            )
            normalized = augment_supply_row_with_district(normalized, warehouse_district_mapping)
            normalized["cache_key"] = cache_key
            normalized["raw_list_hash"] = raw_list_hash
            normalized["raw_detail_hash"] = _stable_payload_hash(raw_detail) if raw_detail is not None else ""
            normalized["raw_goods_hash"] = _stable_payload_hash(raw_goods) if raw_goods is not None else ""
            normalized["raw_package_hash"] = _stable_payload_hash(raw_package) if raw_package is not None else ""
            normalized["last_list_synced_at"] = synced_at
            normalized["last_enriched_at"] = synced_at if attempted_enrichment and not failed_enrich else str((existing or {}).get("last_enriched_at") or "")
            normalized["enrichment_status"] = (
                "failed" if failed_enrich else "ok" if attempted_enrichment else str((existing or {}).get("enrichment_status") or "not_requested")
            )
            normalized["enrichment_error"] = "; ".join(row_warnings) if failed_enrich else ""
            if not is_new and _numbers_differ(existing_normalized.get("accepted_quantity"), normalized.get("accepted_quantity")):
                counters["accepted_qty_changed_rows"] += 1
            rows_to_upsert.append(normalized)
        return {"rows": rows_to_upsert, "touched_cache_keys": [str(row.get("cache_key") or "") for row in rows_to_upsert], **counters}

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

    def _fetch_warehouse_district_mapping(
        self,
        *,
        warehouses: list[Mapping[str, Any]],
        raw_rows: list[Mapping[str, Any]],
        warnings: list[str],
    ) -> dict[str, Any]:
        offices: list[Mapping[str, Any]] = []
        tariffs: list[Mapping[str, Any]] = []
        offices_fetcher = getattr(self.source, "fetch_marketplace_offices", None)
        if callable(offices_fetcher):
            try:
                offices = list(offices_fetcher())
            except WbSuppliesHttpStatusError as exc:
                warnings.append(
                    "warehouse district offices mapping failed with status "
                    + str(exc.status_code)
                    + "; trying tariffs fallback"
                )
            except (WbSuppliesTransportError, OfficialApiRuntimeError, RuntimeError) as exc:
                warnings.append(f"warehouse district offices mapping failed: {exc}; trying tariffs fallback")
        tariffs_fetcher = getattr(self.source, "fetch_box_tariffs", None)
        if callable(tariffs_fetcher):
            try:
                tariffs = list(tariffs_fetcher(tariff_date=current_business_date_iso()))
            except WbSuppliesHttpStatusError as exc:
                warnings.append(
                    "warehouse district tariffs mapping failed with status "
                    + str(exc.status_code)
                    + "; unmapped warehouses will stay unmapped"
                )
            except (WbSuppliesTransportError, OfficialApiRuntimeError, RuntimeError) as exc:
                warnings.append(f"warehouse district tariffs mapping failed: {exc}; unmapped warehouses will stay unmapped")
        return build_warehouse_district_mapping(
            warehouse_rows=warehouses,
            supply_rows=raw_rows,
            office_rows=offices,
            tariff_rows=tariffs,
        )

    def _cached_warehouse_district_mapping(
        self,
        *,
        rows: list[Mapping[str, Any]],
        warehouses: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        return build_warehouse_district_mapping(
            warehouse_rows=warehouses,
            supply_rows=rows,
        )

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
        except OfficialApiRuntimeError as exc:
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
        except OfficialApiRuntimeError as exc:
            warnings.append(f"goods fetch failed for {lookup_id}: {exc}")
            return None

    def _fetch_package(self, lookup_id: str, *, warnings: list[str]) -> list[Mapping[str, Any]] | None:
        try:
            return self.source.fetch_supply_package(lookup_id)
        except WbSuppliesHttpStatusError as exc:
            if exc.status_code in {401, 403}:
                raise
            warnings.append(f"package fetch failed for {lookup_id}: status {exc.status_code}")
            return None
        except WbSuppliesTransportError as exc:
            warnings.append(f"package fetch failed for {lookup_id}: {exc}")
            return None
        except OfficialApiRuntimeError as exc:
            warnings.append(f"package fetch failed for {lookup_id}: {exc}")
            return None


def _normalize_list_request(params: Mapping[str, Any]) -> dict[str, Any]:
    limit = _normalize_limit(params.get("limit"))
    status_ids = _normalize_status_ids(params.get("status_ids") or params.get("statusIDs"))
    legacy_status_id = _optional_int(params.get("status_id"))
    if legacy_status_id is not None and legacy_status_id > 0 and legacy_status_id not in status_ids:
        status_ids.append(legacy_status_id)
    return {
        "search": str(params.get("search") or "").strip(),
        "warehouse_id": str(params.get("warehouse_id") or "").strip(),
        "warehouse": str(params.get("warehouse") or "").strip(),
        "district_keys": _normalize_district_keys(params.get("district_keys") or params.get("district_key")),
        "status_id": legacy_status_id,
        "status_ids": status_ids,
        "size_filter": _normalize_size_filter(params.get("size_filter")),
        "limit": limit,
        "offset": max(0, _optional_int(params.get("offset")) or 0),
        "sort_key": _normalize_sort_key(params.get("sort_key")),
        "sort_dir": _normalize_sort_dir(params.get("sort_dir")),
    }


def _normalize_sync_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    mode = str(payload.get("mode") or SYNC_MODE_INCREMENTAL_REFRESH).strip()
    if mode not in {SYNC_MODE_INCREMENTAL_REFRESH, SYNC_MODE_FULL_BACKFILL}:
        mode = SYNC_MODE_INCREMENTAL_REFRESH
    enrich = str(payload.get("enrich") or "").strip()
    if not enrich:
        enrich = "changed_only" if payload.get("enrich_details") is not False else "none"
    if enrich == "true":
        enrich = "all"
    if enrich not in {"changed_only", "none", "all", "missing_critical"}:
        enrich = "changed_only"
    status_ids = _normalize_status_ids(payload.get("status_ids") or payload.get("statusIDs"))
    legacy_status_id = _optional_int(payload.get("status_id"))
    if legacy_status_id is not None and legacy_status_id > 0 and legacy_status_id not in status_ids:
        status_ids.append(legacy_status_id)
    return {
        "mode": mode,
        "limit": min(max(_optional_int(payload.get("limit")) or DEFAULT_SYNC_LIMIT, 1), 1000),
        "offset": 0,
        "enrich": enrich,
        "status_ids": status_ids,
        "dates": [dict(item) for item in payload.get("dates") or [] if isinstance(item, Mapping)],
        "list_params": dict(payload.get("list_params") or {}) if isinstance(payload.get("list_params"), Mapping) else {},
    }


def _normalize_district_keys(value: Any) -> list[str]:
    allowed = {str(item["district_key"]) for item in district_filter_options()}
    if value is None:
        return []
    if isinstance(value, str):
        raw_values: list[Any] = [item.strip() for item in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        raw_values = []
        for item in value:
            if isinstance(item, str) and "," in item:
                raw_values.extend(part.strip() for part in item.split(","))
            else:
                raw_values.append(item)
    else:
        raw_values = [value]
    result: list[str] = []
    for item in raw_values:
        key = str(item or "").strip().lower()
        if key in allowed and key not in result:
            result.append(key)
    return result


def _normalize_backfill_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    enrich_value = payload.get("enrich")
    enrich = True if enrich_value is None else bool(enrich_value)
    return {
        "mode": SYNC_MODE_FULL_BACKFILL,
        "limit": min(max(_optional_int(payload.get("limit")) or DEFAULT_SYNC_LIMIT, 1), 1000),
        "start_offset": max(0, _optional_int(payload.get("start_offset") or payload.get("offset")) or 0),
        "resume": payload.get("resume") is not False,
        "enrich": enrich,
        "max_pages": _optional_int(payload.get("max_pages")),
        "run_id": str(payload.get("run_id") or "").strip(),
    }


def _normalize_limit(value: Any) -> int:
    normalized = _optional_int(value) or DEFAULT_PAGE_LIMIT
    return normalized if normalized in ALLOWED_PAGE_LIMITS else DEFAULT_PAGE_LIMIT


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    normalized = _optional_int(value)
    if normalized is None:
        normalized = int(default)
    return min(max(int(normalized), int(minimum)), int(maximum))


def _normalize_size_filter(value: Any) -> str:
    normalized = str(value or "").strip()
    if normalized in {SIZE_FILTER_MAIN_250, SIZE_FILTER_ALL, SIZE_FILTER_SMALL_LT_250}:
        return normalized
    return SIZE_FILTER_MAIN_250


def _normalize_sort_key(value: Any) -> str:
    normalized = str(value or "").strip()
    return normalized if normalized in ALLOWED_SORT_KEYS else DEFAULT_SORT_KEY


def _normalize_sort_dir(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"asc", "desc"} else DEFAULT_SORT_DIR


def _normalize_status_ids(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_values: list[Any] = [item.strip() for item in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        raw_values = []
        for item in value:
            if isinstance(item, str) and "," in item:
                raw_values.extend(part.strip() for part in item.split(","))
            else:
                raw_values.append(item)
    else:
        raw_values = [value]
    result: list[int] = []
    for item in raw_values:
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


def _supply_detail_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(record.get("normalized") or {})
    raw = {
        "list": record.get("raw_list"),
        "detail": record.get("raw_detail"),
        "goods": record.get("raw_goods"),
        "package": record.get("raw_package"),
    }
    supply = _row_with_display_fields(normalized)
    supply["raw"] = raw
    raw_goods = record.get("raw_goods") if isinstance(record.get("raw_goods"), list) else None
    raw_package = record.get("raw_package") if isinstance(record.get("raw_package"), list) else None
    goods = _normalize_goods_rows(raw_goods)
    goods_summary = _goods_summary(goods)
    package_summary = _package_summary(raw_package)
    composition_error = str(normalized.get("enrichment_error") or record.get("enrichment_error") or "")
    composition_status = "available" if goods else "missing"
    if composition_error and not goods:
        composition_status = "error"
    elif composition_error:
        composition_status = "partial"
    return {
        "supply": supply,
        "goods": goods,
        "goods_summary": goods_summary,
        "package": {
            "summary": package_summary,
            "raw": [dict(item) for item in raw_package] if raw_package is not None else None,
        },
        "composition_status": composition_status,
        "composition_last_enriched_at": str(normalized.get("last_enriched_at") or record.get("last_enriched_at") or ""),
        "composition_error": composition_error,
        "raw_diagnostics": {
            "list_keys": sorted((record.get("raw_list") or {}).keys()) if isinstance(record.get("raw_list"), Mapping) else [],
            "detail_keys": sorted((record.get("raw_detail") or {}).keys()) if isinstance(record.get("raw_detail"), Mapping) else [],
            "goods_count": len(raw_goods) if raw_goods is not None else None,
            "package_count": len(raw_package) if raw_package is not None else None,
            "raw_hashes": {
                "list": str(record.get("raw_list_hash") or normalized.get("raw_list_hash") or ""),
                "detail": str(record.get("raw_detail_hash") or normalized.get("raw_detail_hash") or ""),
                "goods": str(record.get("raw_goods_hash") or normalized.get("raw_goods_hash") or ""),
                "package": str(record.get("raw_package_hash") or normalized.get("raw_package_hash") or ""),
            },
        },
    }


def _normalize_goods_rows(raw_goods: list[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(raw_goods or []):
        if not isinstance(item, Mapping):
            continue
        rows.append(
            {
                "nm_id": _optional_int(_first_value(item, "nmID", "nmId", "nm_id")),
                "barcode": _first_string(item, "barcode", "barCode", "barcodeID"),
                "vendor_code": _first_string(item, "vendorCode", "vendor_code", "vendorCodeWB"),
                "supplier_article": _first_string(item, "supplierArticle", "supplier_article", "article"),
                "tech_size": _first_string(item, "techSize", "tech_size", "size"),
                "color": _first_string(item, "color", "colour"),
                "quantity": _optional_number(_first_value(item, "quantity", "qty")),
                "accepted_quantity": _optional_number(_first_value(item, "acceptedQuantity", "accepted_quantity")),
                "unloading_quantity": _optional_number(_first_value(item, "unloadingQuantity", "unloading_quantity")),
                "ready_for_sale_quantity": _optional_number(
                    _first_value(item, "readyForSaleQuantity", "ready_for_sale_quantity")
                ),
                "depersonalized_quantity": _optional_number(
                    _first_value(item, "depersonalizedQuantity", "depersonalized_quantity")
                ),
                "package_code": _first_string(item, "packageCode", "package_code"),
                "raw_index": index,
                "evidence_source": "raw_goods",
            }
        )
    return rows


def _goods_summary(goods: list[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "total_quantity": _sum_normalized_goods_field(goods, "quantity"),
        "total_accepted_quantity": _sum_normalized_goods_field(goods, "accepted_quantity"),
        "total_unloading_quantity": _sum_normalized_goods_field(goods, "unloading_quantity"),
        "total_ready_for_sale_quantity": _sum_normalized_goods_field(goods, "ready_for_sale_quantity"),
        "total_depersonalized_quantity": _sum_normalized_goods_field(goods, "depersonalized_quantity"),
        "goods_row_count": len(goods),
        "unique_nm_id_count": len({str(item.get("nm_id")) for item in goods if item.get("nm_id") is not None}),
        "unique_barcode_count": len({str(item.get("barcode")) for item in goods if str(item.get("barcode") or "").strip()}),
    }


def _sum_normalized_goods_field(goods: list[Mapping[str, Any]], key: str) -> float | None:
    total = 0.0
    seen = False
    for item in goods:
        value = _optional_number(item.get(key))
        if value is None:
            continue
        total += value
        seen = True
    return total if seen else None


def _package_summary(raw_package: list[Mapping[str, Any]] | None) -> dict[str, Any]:
    return {
        "package_count": len(raw_package) if raw_package is not None else None,
        "quantity_total": _sum_package_quantity(raw_package) if raw_package is not None else None,
        "barcode_quantity_total": _sum_package_barcode_quantity(raw_package) if raw_package is not None else None,
    }


def _normalized_row_public_fingerprint(row: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "status_id",
        "status_label",
        "warehouse_display",
        "warehouse_fact_line",
        "type_label",
        "quantity_added",
        "packed_quantity",
        "accepted_quantity",
        "acceptance_coefficient",
        "cost_total",
        "cost_evidence",
        "has_transit_cost_marker",
    )
    return {key: row.get(key) for key in keys}


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
    detail_sources = [("detail", detail), ("list", raw_list)]
    list_sources = [("list", raw_list), ("detail", detail)]
    supply_id_value, _supply_id_evidence = _first_non_empty_from_sources(
        list_sources, "supplyID", "supplyId", "supply_id", "ID", "id"
    )
    preorder_id_value, _preorder_id_evidence = _first_non_empty_from_sources(
        list_sources, "preorderID", "preorderId", "preorder_id", "orderID", "orderId"
    )
    supply_id = _id_to_string(supply_id_value)
    preorder_id = _id_to_string(preorder_id_value)
    cache_supply_id = supply_id or (f"preorder:{preorder_id}" if preorder_id else _raw_row_cache_id(raw_list))
    visible_number = supply_id or preorder_id or cache_supply_id

    status_id = _optional_int(_first_non_empty_from_sources(list_sources, "statusID", "statusId", "status_id")[0])
    warehouse_id, warehouse_id_evidence = _first_id_from_sources(list_sources, "warehouseID", "warehouseId", "warehouse_id")
    actual_warehouse_id, actual_warehouse_id_evidence = _first_id_from_sources(
        detail_sources, "actualWarehouseID", "actualWarehouseId", "actual_warehouse_id"
    )
    transit_warehouse_id, transit_warehouse_id_evidence = _first_id_from_sources(
        detail_sources, "transitWarehouseID", "transitWarehouseId", "transit_warehouse_id"
    )
    warehouse_name, warehouse_name_evidence = _first_string_from_sources(list_sources, "warehouseName", "warehouse_name")
    if not warehouse_name and warehouse_id:
        warehouse_name = warehouse_by_id.get(warehouse_id, "")
        warehouse_name_evidence = "warehouse_dict" if warehouse_name else warehouse_name_evidence
    actual_warehouse_name, actual_warehouse_name_evidence = _first_string_from_sources(
        detail_sources, "actualWarehouseName", "actual_warehouse_name"
    )
    if not actual_warehouse_name and actual_warehouse_id:
        actual_warehouse_name = warehouse_by_id.get(actual_warehouse_id, "")
        actual_warehouse_name_evidence = "warehouse_dict" if actual_warehouse_name else actual_warehouse_name_evidence
    transit_warehouse_name, transit_warehouse_name_evidence = _first_string_from_sources(
        detail_sources, "transitWarehouseName", "transit_warehouse_name"
    )
    if not transit_warehouse_name and transit_warehouse_id:
        transit_warehouse_name = warehouse_by_id.get(transit_warehouse_id, "")
        transit_warehouse_name_evidence = "warehouse_dict" if transit_warehouse_name else transit_warehouse_name_evidence
    box_type_id = _optional_int(_first_non_empty_from_sources(list_sources, "boxTypeID", "boxTypeId", "box_type_id")[0])
    virtual_type_id = _optional_int(
        _first_non_empty_from_sources(list_sources, "virtualTypeID", "virtualTypeId", "virtual_type_id")[0]
    )
    planned_quantity, planned_quantity_evidence = _first_number_from_sources(
        list_sources, "quantity", "plannedQuantity", "planned_quantity", "addedQuantity", "added_quantity"
    )
    goods_quantity = _sum_goods_field(raw_goods, "quantity") if raw_goods is not None else None
    goods_supplier_box_quantity = _sum_goods_field(raw_goods, "supplierBoxAmount") if raw_goods is not None else None
    package_quantity = _sum_package_quantity(raw_package) if raw_package is not None else None
    quantity_added, quantity_evidence = _first_quantity_with_evidence(
        (planned_quantity, planned_quantity_evidence),
        (goods_quantity, "goods.quantity_total"),
        (package_quantity, "package.quantity_total"),
    )
    accepted_quantity, accepted_quantity_evidence = _quantity_from_list_goods_detail(
        raw_list=raw_list,
        raw_detail=detail,
        raw_goods=raw_goods,
        field_keys=("acceptedQuantity", "accepted_quantity"),
        goods_key="acceptedQuantity",
    )
    unloading_quantity, unloading_quantity_evidence = _quantity_from_list_goods_detail(
        raw_list=raw_list,
        raw_detail=detail,
        raw_goods=raw_goods,
        field_keys=("unloadingQuantity", "unloading_quantity"),
        goods_key="unloadingQuantity",
    )
    quantity_for_size_filter, quantity_evidence = _quantity_for_size_filter(
        planned_quantity=quantity_added,
        goods_quantity=goods_quantity,
        accepted_quantity=accepted_quantity,
        unloading_quantity=unloading_quantity,
        planned_quantity_evidence=quantity_evidence,
    )
    packed_quantity, packed_quantity_evidence = _packed_quantity(
        sources=list_sources,
        goods_quantity=goods_quantity,
        goods_supplier_box_quantity=goods_supplier_box_quantity,
        package_quantity=package_quantity,
        quantity_added=quantity_added,
        status_id=status_id,
    )
    acceptance_cost, acceptance_cost_evidence = _first_number_from_sources(list_sources, "acceptanceCost", "acceptance_cost")
    transit_cost, transit_cost_evidence = _first_number_from_sources(
        list_sources,
        "transitCost",
        "transit_cost",
        "transitTariff",
        "transit_tariff",
        "transitCostTotal",
        "transit_cost_total",
    )
    acceptance_coefficient = _first_number_from_sources(
        list_sources, "paidAcceptanceCoefficient", "acceptanceCoefficient", "acceptance_coefficient"
    )[0]
    cost_total, cost_evidence = _cost_total(
        sources=list_sources,
        acceptance_cost=acceptance_cost,
        acceptance_cost_evidence=acceptance_cost_evidence,
        transit_cost=transit_cost,
        transit_cost_evidence=transit_cost_evidence,
        is_transit=bool(transit_warehouse_id or transit_warehouse_name),
        status_id=status_id,
        acceptance_coefficient=acceptance_coefficient,
    )
    create_date = _first_string_from_sources(list_sources, "createDate", "createdAt", "created_at")[0]
    supply_date = _first_string_from_sources(list_sources, "supplyDate", "supply_date")[0]
    fact_date = _first_string_from_sources(list_sources, "factDate", "fact_date")[0]
    updated_date = _first_string_from_sources(list_sources, "updatedDate", "updated_at", "updatedDate")[0]
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
        "status_visual_tone": _status_visual_tone(status_id),
        "status_class": _status_visual_tone(status_id),
        "box_type_id": box_type_id,
        "virtual_type_id": virtual_type_id,
        "box_type_label": _box_type_label(box_type_id),
        "type_label": _type_label(box_type_id=box_type_id, virtual_type_id=virtual_type_id, is_transit=is_transit),
        "is_box_on_pallet": _optional_bool(_first_non_empty_from_sources(list_sources, "isBoxOnPallet", "is_box_on_pallet")[0]),
        "warehouse_id": warehouse_id,
        "warehouse_name": warehouse_name,
        "planned_warehouse_id": warehouse_id,
        "planned_warehouse_name": warehouse_name,
        "target_warehouse_id": warehouse_id,
        "target_warehouse_name": warehouse_name,
        "actual_warehouse_id": actual_warehouse_id,
        "actual_warehouse_name": actual_warehouse_name,
        "transit_warehouse_id": transit_warehouse_id,
        "transit_warehouse_name": transit_warehouse_name,
        "warehouse_from_name": warehouse_from_name,
        "warehouse_to_name": warehouse_to_name,
        "warehouse_actual_name": actual_warehouse_name,
        "warehouse_display": warehouse_display,
        "district_source_warehouse_id": warehouse_id,
        "district_source_warehouse_name": warehouse_name,
        "district_source_warehouse_role": "planned",
        "district_source_warehouse_evidence": warehouse_name_evidence,
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
        if supply_id.startswith("supply:"):
            return supply_id.removeprefix("supply:"), False
        if supply_id.startswith("preorder:"):
            return supply_id.removeprefix("preorder:"), True
        return supply_id, False
    preorder_id = _id_to_string(_first_value(row, "preorderID", "preorderId", "preorder_id", "orderID", "orderId"))
    if preorder_id:
        if preorder_id.startswith("preorder:"):
            return preorder_id.removeprefix("preorder:"), True
        return preorder_id, True
    return "", False


def _resolve_upstream_lookup_id_from_sources(*rows: Mapping[str, Any] | None) -> tuple[str, bool]:
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        lookup_id, is_preorder_id = _resolve_upstream_lookup_id(row)
        if lookup_id:
            return lookup_id, is_preorder_id
        cache_key = _id_to_string(row.get("cache_key"))
        if cache_key.startswith("supply:"):
            return cache_key.removeprefix("supply:"), False
        if cache_key.startswith("preorder:"):
            return cache_key.removeprefix("preorder:"), True
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


def _quantity_from_list_goods_detail(
    *,
    raw_list: Mapping[str, Any],
    raw_detail: Mapping[str, Any],
    raw_goods: list[Mapping[str, Any]] | None,
    field_keys: tuple[str, ...],
    goods_key: str,
) -> tuple[float | None, str]:
    list_value, list_evidence = _first_number_from_sources([("list", raw_list)], *field_keys)
    if list_value is not None:
        return list_value, list_evidence
    detail_value, detail_evidence = _first_number_from_sources([("detail", raw_detail)], *field_keys)
    if detail_value is not None and _detail_is_fresh_for_list(raw_detail=raw_detail, raw_list=raw_list):
        return detail_value, detail_evidence
    goods_value = _sum_goods_field(raw_goods, goods_key) if raw_goods is not None else None
    if goods_value is not None:
        return goods_value, f"goods.{goods_key}_total"
    return detail_value, detail_evidence


def _detail_is_fresh_for_list(*, raw_detail: Mapping[str, Any], raw_list: Mapping[str, Any]) -> bool:
    if not raw_detail:
        return False
    detail_status = _optional_int(_first_value(raw_detail, "statusID", "statusId", "status_id"))
    list_status = _optional_int(_first_value(raw_list, "statusID", "statusId", "status_id"))
    if detail_status is not None and list_status is not None and detail_status != list_status:
        return False
    detail_updated = _parse_iso_datetime(_first_string(raw_detail, "updatedDate", "updated_at", "updated_date"))
    list_updated = _parse_iso_datetime(_first_string(raw_list, "updatedDate", "updated_at", "updated_date"))
    if list_updated is not None:
        if detail_updated is None:
            return False
        return detail_updated >= list_updated
    return True


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
    status_id: int | None,
    acceptance_coefficient: float | None,
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
    if not is_transit and status_id in {5, 6} and acceptance_coefficient == 0:
        return 0, "paidAcceptanceCoefficient.free_accepted_non_transit"
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


def _row_matches_statuses(row: Mapping[str, Any], status_ids: list[int]) -> bool:
    if not status_ids:
        return True
    return _optional_int(row.get("status_id")) in set(status_ids)


def _row_matches_districts(row: Mapping[str, Any], district_keys: list[str]) -> bool:
    if not district_keys:
        return True
    return str(row.get("district_key") or "").strip() in set(district_keys)


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
                option = options.setdefault(warehouse_id or name, {"value": warehouse_id, "label": name or warehouse_id})
                source_name = str(row.get("district_source_warehouse_name") or row.get("warehouse_name") or "").strip()
                source_id = str(row.get("district_source_warehouse_id") or row.get("warehouse_id") or "").strip()
                is_district_source_option = (
                    id_key == "warehouse_id"
                    or (warehouse_id and source_id and warehouse_id == source_id)
                    or (name and source_name and name == source_name)
                )
                district_key = str(row.get("district_key") or "").strip()
                if is_district_source_option and district_key and district_key != "unmapped":
                    option.setdefault("district_key", district_key)
                    option.setdefault("district_label_ru", str(row.get("district_label_ru") or ""))
    return sorted(options.values(), key=lambda item: str(item.get("label") or "").casefold())


def _status_options(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    status_ids = sorted(
        set(OFFICIAL_STATUS_IDS)
        | {status_id for row in rows if (status_id := _optional_int(row.get("status_id"))) is not None}
    )
    return [
        {
            "value": status_id,
            "label": _status_label(status_id),
            "status_tone": _status_tone(status_id),
            "status_visual_tone": _status_visual_tone(status_id),
        }
        for status_id in status_ids
    ]


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


def _status_visual_tone(status_id: int | None) -> str:
    if status_id in OFFICIAL_STATUS_IDS:
        return f"status-{status_id}"
    return "neutral"


def _box_type_label(box_type_id: int | None) -> str:
    if box_type_id is None:
        return ""
    if box_type_id == 0:
        return ""
    return BOX_TYPE_LABELS_RU.get(box_type_id, f"Тип {box_type_id}")


def _type_label(*, box_type_id: int | None, virtual_type_id: int | None = None, is_transit: bool) -> str:
    parts: list[str] = []
    box_label = _box_type_label(box_type_id)
    if box_label:
        parts.append(box_label)
    elif box_type_id == 0:
        virtual_label = VIRTUAL_TYPE_LABELS_RU.get(virtual_type_id or -1, "")
        if virtual_label:
            parts.append(virtual_label)
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


def _stable_cache_key(row: Mapping[str, Any]) -> str:
    supply_id = _id_to_string(_first_value(row, "supplyID", "supplyId", "supply_id", "ID", "id"))
    if supply_id:
        return f"supply:{supply_id}"
    preorder_id = _id_to_string(_first_value(row, "preorderID", "preorderId", "preorder_id", "orderID", "orderId"))
    if preorder_id:
        return f"preorder:{preorder_id}"
    return _raw_row_cache_id(row)


def _merge_raw_supply_rows(primary_rows: list[Mapping[str, Any]], extra_rows: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    index_by_key: dict[str, int] = {}
    for row in [*primary_rows, *extra_rows]:
        if not isinstance(row, Mapping):
            continue
        cache_key = _stable_cache_key(row)
        if cache_key in index_by_key:
            result[index_by_key[cache_key]] = _prefer_raw_supply_row(result[index_by_key[cache_key]], row)
            continue
        index_by_key[cache_key] = len(result)
        result.append(row)
    return result


def _prefer_raw_supply_row(current: Mapping[str, Any], candidate: Mapping[str, Any]) -> Mapping[str, Any]:
    current_updated = _first_string(current, "updatedDate", "updated_at", "updated_date")
    candidate_updated = _first_string(candidate, "updatedDate", "updated_at", "updated_date")
    current_status = _optional_int(_first_value(current, "statusID", "status_id"))
    candidate_status = _optional_int(_first_value(candidate, "statusID", "status_id"))
    if candidate_updated and (not current_updated or candidate_updated >= current_updated):
        return candidate
    if candidate_status is not None and current_status is not None and candidate_status != current_status:
        return candidate
    return current


def _active_authoritative_keys_from_raw_rows(rows: list[Mapping[str, Any]]) -> set[str]:
    result: set[str] = set()
    for row in rows:
        status_id = _optional_int(_first_value(row, "statusID", "status_id"))
        if status_id in ACTIVE_RECONCILE_STATUS_IDS:
            result.update(_raw_identity_keys(row))
    return result


def _status_force_enrich_keys(raw_rows: list[Mapping[str, Any]], existing_records: list[Mapping[str, Any]]) -> set[str]:
    existing_by_key = _cache_record_index(existing_records)
    candidates: list[tuple[int, float, int, str]] = []
    for index, row in enumerate(raw_rows):
        status_id = _optional_int(_first_value(row, "statusID", "status_id"))
        if status_id not in SUPPLY_BACKED_REFRESH_STATUS_IDS:
            continue
        cache_key = _stable_cache_key(row)
        lookup_id, _is_preorder_id = _resolve_upstream_lookup_id(row)
        existing = existing_by_key.get(cache_key) or existing_by_key.get(lookup_id)
        if existing is None:
            continue
        priority = _status_row_forced_refresh_priority(row, existing)
        if priority is None:
            continue
        raw_updated = _parse_iso_datetime(_first_string(row, "updatedDate", "updated_at", "updated_date"))
        updated_sort = -(raw_updated.timestamp() if raw_updated is not None else 0.0)
        candidates.append((priority, updated_sort, index, cache_key))
    candidates.sort()
    return {cache_key for _priority, _updated_sort, _index, cache_key in candidates[:MAX_FORCED_STATUS_REFRESH_ROWS]}


def _status_row_forced_refresh_priority(row: Mapping[str, Any], existing: Mapping[str, Any]) -> int | None:
    normalized = existing.get("normalized") if isinstance(existing.get("normalized"), Mapping) else {}
    status_id = _optional_int(_first_value(row, "statusID", "status_id"))
    existing_status_id = _optional_int(normalized.get("status_id"))
    if status_id is not None and existing_status_id is not None and status_id != existing_status_id:
        return 0
    raw_updated = _parse_iso_datetime(_first_string(row, "updatedDate", "updated_at", "updated_date"))
    enriched_at = _parse_iso_datetime(str(existing.get("last_enriched_at") or normalized.get("last_enriched_at") or ""))
    if raw_updated is not None and (enriched_at is None or enriched_at < raw_updated):
        return 1
    if _row_needs_enrichment(normalized):
        return 2
    planned_quantity = _optional_number(
        normalized.get("quantity_added")
        if normalized.get("quantity_added") is not None
        else normalized.get("packed_quantity")
    )
    accepted_quantity = _optional_number(normalized.get("accepted_quantity"))
    if status_id == 5 and planned_quantity and accepted_quantity is not None and accepted_quantity <= 0:
        return 2
    if str(existing.get("enrichment_status") or normalized.get("enrichment_status") or "").strip() == "failed":
        return 3
    return None


def _numbers_differ(left: Any, right: Any) -> bool:
    left_number = _optional_number(left)
    right_number = _optional_number(right)
    if left_number is None and right_number is None:
        return False
    if left_number is None or right_number is None:
        return True
    return abs(float(left_number) - float(right_number)) > 1e-9


def _raw_identity_keys_from_rows(rows: list[Mapping[str, Any]]) -> set[str]:
    result: set[str] = set()
    for row in rows:
        result.update(_raw_identity_keys(row))
    return result


def _raw_identity_keys(row: Mapping[str, Any]) -> set[str]:
    keys = {_stable_cache_key(row)}
    supply_id = _id_to_string(_first_value(row, "supplyID", "supply_id", "id"))
    preorder_id = _id_to_string(_first_value(row, "preorderID", "preorder_id"))
    keys.update(_identity_key_variants(supply_id, kind="supply"))
    keys.update(_identity_key_variants(preorder_id, kind="preorder"))
    return {key for key in keys if key}


def _record_identity_keys(record: Mapping[str, Any]) -> set[str]:
    normalized = record.get("normalized") if isinstance(record.get("normalized"), Mapping) else {}
    keys = {
        str(record.get("cache_key") or "").strip(),
        str(record.get("supply_id") or "").strip(),
        str(record.get("wb_supply_id") or "").strip(),
        str(record.get("preorder_id") or "").strip(),
        str(normalized.get("cache_key") or "").strip(),
        str(normalized.get("supply_id") or "").strip(),
        str(normalized.get("wb_supply_id") or "").strip(),
        str(normalized.get("preorder_id") or "").strip(),
    }
    expanded: set[str] = set()
    for key in keys:
        if not key:
            continue
        expanded.add(key)
        if key.startswith("supply:"):
            expanded.update(_identity_key_variants(key.removeprefix("supply:"), kind="supply"))
        elif key.startswith("preorder:"):
            expanded.update(_identity_key_variants(key.removeprefix("preorder:"), kind="preorder"))
        else:
            expanded.update(_identity_key_variants(key, kind="supply"))
            expanded.update(_identity_key_variants(key, kind="preorder"))
    return {key for key in expanded if key}


def _identity_key_variants(value: str, *, kind: str) -> set[str]:
    normalized = str(value or "").strip()
    if not normalized:
        return set()
    if normalized.startswith(("supply:", "preorder:")):
        return {normalized, normalized.split(":", 1)[1]}
    if kind == "preorder":
        return {normalized, f"preorder:{normalized}"}
    return {normalized, f"supply:{normalized}"}


def _record_primary_delete_key(record: Mapping[str, Any]) -> str:
    return (
        str(record.get("cache_key") or "").strip()
        or str(record.get("supply_id") or "").strip()
        or str(record.get("wb_supply_id") or "").strip()
        or str(record.get("preorder_id") or "").strip()
    )


def _count_historical_absent_from_rows(records: list[Mapping[str, Any]], raw_rows: list[Mapping[str, Any]]) -> int:
    raw_keys = _raw_identity_keys_from_rows(raw_rows)
    count = 0
    for record in records:
        normalized = record.get("normalized") if isinstance(record.get("normalized"), Mapping) else {}
        status_id = _optional_int(normalized.get("status_id"))
        if status_id in HISTORICAL_STATUS_IDS and not (_record_identity_keys(record) & raw_keys):
            count += 1
    return count


def _enriched_evidence_changed(
    existing: Mapping[str, Any],
    raw_detail: Mapping[str, Any] | None,
    raw_goods: list[Mapping[str, Any]] | None,
    raw_package: list[Mapping[str, Any]] | None,
) -> bool:
    checks = (
        ("raw_detail_hash", raw_detail),
        ("raw_goods_hash", raw_goods),
        ("raw_package_hash", raw_package),
    )
    for key, payload in checks:
        if payload is None:
            continue
        if str(existing.get(key) or "") != _stable_payload_hash(payload):
            return True
    return False


def _stable_payload_hash(payload: Any) -> str:
    if payload is None:
        return ""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_record_index(records: list[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for record in records:
        cache_key = str(record.get("cache_key") or "").strip()
        wb_supply_id = str(record.get("wb_supply_id") or "").strip()
        supply_id = str(record.get("supply_id") or "").strip()
        preorder_id = str(record.get("preorder_id") or "").strip()
        for key in (
            cache_key,
            f"supply:{wb_supply_id}" if wb_supply_id else "",
            wb_supply_id,
            supply_id,
            f"preorder:{preorder_id}" if preorder_id else "",
            preorder_id,
        ):
            if key and key not in result:
                result[key] = record
    return result


def _row_needs_enrichment(row: Mapping[str, Any]) -> bool:
    if str(row.get("last_enriched_at") or "").strip():
        raw_diagnostics = row.get("raw_diagnostics") if isinstance(row.get("raw_diagnostics"), Mapping) else {}
        has_goods = raw_diagnostics.get("goods_count") is not None
        has_technical_type = str(row.get("type_label") or "").startswith("Тип ")
        has_transit = bool(row.get("has_transit_cost_marker"))
        non_transit_cost_missing = (
            _optional_int(row.get("status_id")) in {5, 6}
            and not has_transit
            and row.get("cost_total") is None
            and row.get("acceptance_coefficient") is None
        )
        return bool(has_technical_type or non_transit_cost_missing or not has_goods)
    raw_diagnostics = row.get("raw_diagnostics") if isinstance(row.get("raw_diagnostics"), Mapping) else {}
    has_goods = raw_diagnostics.get("goods_count") is not None
    status_id = _optional_int(row.get("status_id"))
    has_transit = bool(row.get("has_transit_cost_marker"))
    has_technical_type = str(row.get("type_label") or "").startswith("Тип ")
    non_transit_cost_missing = (
        status_id in {5, 6}
        and not has_transit
        and row.get("cost_total") is None
        and row.get("acceptance_coefficient") is None
    )
    return (
        row.get("quantity_for_size_filter") is None
        or row.get("quantity_added") is None
        or row.get("accepted_quantity") is None
        or not str(row.get("warehouse_display") or "").strip()
        or status_id is None
        or has_technical_type
        or non_transit_cost_missing
        or not has_goods
    )


def _new_run_id() -> str:
    return "wb_supplies_" + uuid.uuid4().hex


def _run_log(timestamp: str, message: str) -> dict[str, str]:
    return {"at": timestamp, "message": str(message or "")[:500]}


def _cache_completeness(state: Mapping[str, Any], rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    if bool(state.get("backfill_complete")):
        return {"status": "complete", "label": "полная загрузка завершена", "can_backfill_more": False}
    last_limit = _optional_int(state.get("last_limit"))
    latest_synced_count = _optional_int(state.get("latest_synced_count"))
    if not rows:
        return {"status": "empty", "label": "Cache пуст", "can_backfill_more": False}
    if state.get("last_mode") == SYNC_MODE_INCREMENTAL_REFRESH:
        return {"status": "latest_window", "label": "latest window only", "can_backfill_more": True}
    if bool(state.get("may_have_more")) or (last_limit and latest_synced_count is not None and latest_synced_count >= last_limit):
        return {
            "status": "partial",
            "label": f"частично загружена до offset {state.get('highest_synced_offset') or state.get('last_offset') or 0}",
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


def _sort_rows(rows: list[Mapping[str, Any]], sort_key: str, sort_dir: str) -> list[Mapping[str, Any]]:
    if sort_key != "supply_date":
        sort_key = DEFAULT_SORT_KEY
    if sort_key == "supply_date":
        return sorted(rows, key=lambda row: _supply_date_sort_key(row, sort_dir=sort_dir))
    return sorted(rows, key=lambda row: _supply_date_sort_key(row, sort_dir=DEFAULT_SORT_DIR))


def _supply_date_sort_key(row: Mapping[str, Any], *, sort_dir: str) -> tuple[Any, ...]:
    primary_date = _date_ordinal(row.get("supply_date")) or _date_ordinal(row.get("fact_date"))
    fact_date = _date_ordinal(row.get("fact_date"))
    updated_date = _date_ordinal(row.get("updated_date"))
    created_date = _date_ordinal(row.get("source_created_at"))
    stable_id = str(row.get("visible_number") or row.get("wb_supply_id") or row.get("supply_id") or "")
    empty_rank = 0 if primary_date is not None else 2 if _optional_int(row.get("status_id")) == 1 else 1
    if sort_dir == "asc":
        return (
            empty_rank,
            primary_date if primary_date is not None else 10**9,
            fact_date if fact_date is not None else 10**9,
            updated_date if updated_date is not None else 10**9,
            created_date if created_date is not None else 10**9,
            stable_id,
        )
    return (
        empty_rank,
        -(primary_date or 0),
        -(fact_date or 0),
        -(updated_date or 0),
        -(created_date or 0),
        _stable_id_desc_key(stable_id),
    )


def _stable_id_desc_key(value: str) -> tuple[int, int | str]:
    try:
        return (0, -int(value))
    except (TypeError, ValueError):
        return (1, str(value))


def _date_ordinal(value: Any) -> int | None:
    parsed = _parse_iso_date(value)
    return parsed.toordinal() if parsed else None


def _parse_iso_date(value: Any) -> date | None:
    date_part = str(value or "").strip()[:10]
    if len(date_part) != 10:
        return None
    try:
        return date.fromisoformat(date_part)
    except ValueError:
        return None


def _row_with_display_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    status_id = _optional_int(result.get("status_id"))
    result["status_visual_tone"] = str(result.get("status_visual_tone") or _status_visual_tone(status_id))
    result["status_class"] = str(result.get("status_class") or result["status_visual_tone"])
    supply_date = _parse_iso_date(row.get("supply_date"))
    fact_date = _parse_iso_date(row.get("fact_date"))
    interface_year = _current_business_year()
    result["supply_date_display"] = _format_ru_supply_date(supply_date, interface_year=interface_year)
    result["fact_date_display"] = _format_ru_supply_date(fact_date, interface_year=interface_year)
    result["supply_date_range_display"] = _format_ru_supply_date_range(
        supply_date,
        fact_date,
        interface_year=interface_year,
    )
    result.update(_per_unit_display_fields(result, "transit", _optional_number(result.get("effective_cost_total"))))
    result.update(
        _per_unit_display_fields(
            result,
            "fulfillment",
            _optional_number(result.get("fulfillment_amount_with_vat_total")),
        )
    )
    result.update(
        _per_unit_display_fields(
            result,
            "fulfillment_storage",
            _optional_number(result.get("fulfillment_storage_allocated_amount_with_vat_total")),
        )
    )
    return result


def _row_with_fulfillment_overlay(
    row: Mapping[str, Any],
    overlay_by_identity: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    result = dict(row)
    overlay: Mapping[str, Any] = {}
    for value in _row_identity_values(row):
        candidate = overlay_by_identity.get(value)
        if candidate:
            overlay = candidate
            break
    amount_with_vat = _optional_number(overlay.get("amount_with_vat_total")) if overlay else None
    amount_without_vat = _optional_number(overlay.get("amount_without_vat_total")) if overlay else None
    vat_total = _optional_number(overlay.get("vat_total")) if overlay else None
    service_without_storage = (
        _optional_number(overlay.get("service_amount_with_vat_without_storage_total")) if overlay else None
    )
    storage_allocated = (
        _optional_number(overlay.get("storage_allocated_amount_with_vat_total")) if overlay else None
    )
    result["fulfillment_amount_with_vat_total"] = amount_with_vat
    result["fulfillment_amount_without_vat_total"] = amount_without_vat
    result["fulfillment_vat_total"] = vat_total
    result["fulfillment_service_amount_with_vat_without_storage_total"] = service_without_storage
    result["fulfillment_storage_allocated_amount_with_vat_total"] = storage_allocated
    result["fulfillment_amount_display"] = _format_effective_cost(amount_with_vat)
    result["fulfillment_upload_ids"] = list(overlay.get("upload_ids") or []) if overlay else []
    result["fulfillment_payment_validation_ids"] = list(overlay.get("payment_validation_ids") or []) if overlay else []
    result["fulfillment_service_names"] = list(overlay.get("service_names") or []) if overlay else []
    result["fulfillment_line_count"] = int(overlay.get("line_count") or 0) if overlay else 0
    result["fulfillment_source"] = "fulfillment_services_upload" if overlay else ""
    return result


def _per_unit_display_fields(row: Mapping[str, Any], prefix: str, amount: float | None) -> dict[str, Any]:
    denominator, source, preliminary = _per_unit_denominator(row)
    per_unit = (float(amount) / denominator) if amount is not None and denominator and denominator > 0 else None
    return {
        f"{prefix}_per_unit_denominator": denominator,
        f"{prefix}_per_unit_denominator_source": source,
        f"{prefix}_per_unit_preliminary": preliminary,
        f"{prefix}_per_unit_amount": per_unit,
        f"{prefix}_per_unit_display": _format_per_unit(per_unit, preliminary=preliminary),
    }


def _per_unit_denominator(row: Mapping[str, Any]) -> tuple[float | None, str, bool]:
    accepted_quantity = _optional_number(row.get("accepted_quantity"))
    if accepted_quantity and accepted_quantity > 0:
        return accepted_quantity, "accepted_quantity", False
    quantity_for_size_filter = _optional_number(row.get("quantity_for_size_filter"))
    if quantity_for_size_filter and quantity_for_size_filter > 0:
        return quantity_for_size_filter, "quantity_for_size_filter", False
    planned_quantity = _optional_number(row.get("quantity_added"))
    if planned_quantity and planned_quantity > 0:
        return planned_quantity, "quantity_added", True
    return None, "", False


def _format_per_unit(value: float | None, *, preliminary: bool = False) -> str:
    if value is None:
        return "₽/шт —"
    marker = " предвар." if preliminary else ""
    return _format_effective_cost(value).replace(" ₽", " ₽/шт") + marker


def _row_with_transit_cost_enrichment(
    row: Mapping[str, Any],
    enrichments: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    result = dict(row)
    enrichment = _lookup_transit_cost_enrichment(row, enrichments)
    amount = _optional_number(enrichment.get("amount")) if enrichment else None
    status = str(enrichment.get("status") or "") if enrichment else ""
    confidence = str(enrichment.get("confidence") or "") if enrichment else ""
    is_success = bool(enrichment and status == "success" and amount is not None and confidence in {"high", "medium"})
    result["seller_portal_transit_cost"] = amount if is_success else None
    result["seller_portal_transit_cost_display"] = str(enrichment.get("amount_label") or _format_effective_cost(amount)) if is_success else "—"
    result["seller_portal_transit_cost_source"] = str(enrichment.get("source") or "") if enrichment else ""
    result["seller_portal_transit_cost_evidence_type"] = str(enrichment.get("evidence_type") or "") if enrichment else ""
    result["seller_portal_transit_cost_fetched_at"] = str(enrichment.get("fetched_at") or "") if enrichment else ""
    result["seller_portal_transit_cost_status"] = status
    result["seller_portal_transit_cost_confidence"] = confidence
    official_cost = _optional_number(row.get("cost_total"))
    if official_cost is not None:
        result["effective_cost_total"] = official_cost
        result["effective_cost_display"] = _format_effective_cost(official_cost)
        result["effective_cost_source"] = "official_wb_api"
    elif is_success:
        result["effective_cost_total"] = amount
        result["effective_cost_display"] = result["seller_portal_transit_cost_display"]
        result["effective_cost_source"] = SELLER_PORTAL_TRANSIT_COST_SOURCE
    else:
        result["effective_cost_total"] = None
        result["effective_cost_display"] = "—"
        result["effective_cost_source"] = "unknown"
    return result


def _transit_cost_enrichment_map(records: list[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for record in records:
        supply_id = str(record.get("supply_id") or "").strip()
        if supply_id:
            result[supply_id] = record
            result[f"supply:{supply_id}"] = record
    return result


def _lookup_transit_cost_enrichment(
    row: Mapping[str, Any],
    enrichments: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    for value in _row_identity_values(row):
        match = enrichments.get(value)
        if match:
            return match
    return {}


def _row_identity_values(row: Mapping[str, Any]) -> set[str]:
    values = {
        str(row.get("supply_id") or "").strip(),
        str(row.get("cache_key") or "").strip(),
        str(row.get("wb_supply_id") or "").strip(),
        str(row.get("visible_number") or "").strip(),
        str(row.get("number_label") or "").strip(),
    }
    expanded: set[str] = set()
    for value in values:
        if not value:
            continue
        expanded.add(value)
        expanded.add(value.removeprefix("supply:"))
        if not value.startswith(("supply:", "preorder:")):
            expanded.add(f"supply:{value}")
    return {value for value in expanded if value}


def _transit_cost_supply_id(row: Mapping[str, Any]) -> str:
    for key in ("wb_supply_id", "visible_number", "supply_id"):
        value = str(row.get(key) or "").strip()
        if value.startswith("supply:"):
            value = value.removeprefix("supply:")
        if value and value.isdigit():
            return value
    return ""


def _is_transit_cost_enrichment_candidate(row: Mapping[str, Any]) -> bool:
    if _optional_number(row.get("cost_total")) is not None:
        return False
    has_transit = bool(
        row.get("has_transit_cost_marker")
        or str(row.get("transit_warehouse_id") or "").strip()
        or str(row.get("transit_warehouse_name") or "").strip()
    )
    return bool(has_transit and _transit_cost_supply_id(row))


def _has_fresh_success_transit_cost(
    row: Mapping[str, Any],
    enrichments: Mapping[str, Mapping[str, Any]],
    *,
    now_text: str,
) -> bool:
    enrichment = _lookup_transit_cost_enrichment(row, enrichments)
    return bool(
        enrichment
        and str(enrichment.get("status") or "") == "success"
        and _optional_number(enrichment.get("amount")) is not None
        and _is_recent_iso_timestamp(
            str(enrichment.get("fetched_at") or enrichment.get("updated_at") or ""),
            now_text=now_text,
            max_age_seconds=TRANSIT_COST_ENRICHMENT_FRESH_SECONDS,
        )
    )


def _is_recent_iso_timestamp(value: str, *, now_text: str, max_age_seconds: int) -> bool:
    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        now = datetime.fromisoformat(str(now_text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age_seconds = (now - timestamp).total_seconds()
    return 0 <= age_seconds <= max(0, int(max_age_seconds))


def _format_effective_cost(value: float | None) -> str:
    if value is None:
        return "—"
    if abs(value - round(value)) < 0.005:
        return f"{int(round(value)):,}".replace(",", " ") + " ₽"
    integer, fractional = f"{value:.2f}".split(".")
    return f"{int(integer):,}".replace(",", " ") + f",{fractional} ₽"


def _normalize_transit_cost_enrichment_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw_ids = payload.get("supply_ids")
    supply_ids: list[str] = []
    if isinstance(raw_ids, (list, tuple, set)):
        for value in raw_ids:
            normalized = str(value or "").strip()
            if normalized:
                supply_ids.append(normalized)
    list_params = payload.get("list_params") if isinstance(payload.get("list_params"), Mapping) else {}
    return {
        "supply_ids": supply_ids[:MAX_TRANSIT_COST_ENRICHMENT_LIMIT],
        "list_params": dict(list_params),
        "limit": _bounded_int(
            payload.get("limit"),
            default=DEFAULT_TRANSIT_COST_ENRICHMENT_LIMIT,
            minimum=1,
            maximum=MAX_TRANSIT_COST_ENRICHMENT_LIMIT,
        ),
        "force": bool(payload.get("force")),
    }


def _new_transit_cost_run_id() -> str:
    return "wb_supply_transit_cost_" + uuid.uuid4().hex


def _last_error(results: list[Mapping[str, Any]]) -> str:
    for result in reversed(results):
        error = str(result.get("error") or "").strip()
        if error:
            return _safe_error_message(RuntimeError(error))
    return ""


def _current_business_year() -> int:
    try:
        return int(current_business_date_iso()[:4])
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).year


def _format_ru_supply_date_range(start: date | None, end: date | None, *, interface_year: int) -> str:
    if start and end and start != end:
        include_year = not (start.year == end.year == interface_year)
        return (
            f"{_format_ru_supply_date(start, interface_year=interface_year, force_year=include_year)}"
            f" → {_format_ru_supply_date(end, interface_year=interface_year, force_year=include_year)}"
        )
    return _format_ru_supply_date(start or end, interface_year=interface_year) or "—"


def _format_ru_supply_date(value: date | None, *, interface_year: int, force_year: bool = False) -> str:
    if value is None:
        return ""
    months = (
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    )
    text = f"{value.day} {months[value.month - 1]}"
    if force_year or value.year != interface_year:
        text = f"{text} {value.year}"
    return text


def _public_sync_state(state: Mapping[str, Any], cache_completeness: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "backfill_complete": bool(state.get("backfill_complete")),
        "backfill_started_at": state.get("backfill_started_at") or "",
        "backfill_completed_at": state.get("backfill_completed_at") or "",
        "highest_synced_offset": state.get("highest_synced_offset") or 0,
        "last_successful_offset": state.get("last_successful_offset"),
        "last_limit": state.get("last_limit"),
        "last_mode": state.get("last_mode") or "",
        "latest_window_synced_at": state.get("latest_window_synced_at") or "",
        "latest_window_limit": state.get("latest_window_limit"),
        "latest_window_returned_count": state.get("latest_window_returned_count"),
        "may_have_more": bool(state.get("may_have_more")),
        "last_error": state.get("last_error") or "",
        "cache_completeness": cache_completeness.get("status") or "",
        "cache_completeness_label": cache_completeness.get("label") or "",
    }


def _to_block_error(exc: Exception) -> WbSuppliesBlockError:
    if isinstance(exc, WbSuppliesBlockError):
        return exc
    if isinstance(exc, WbSuppliesHttpStatusError):
        return WbSuppliesBlockError(
            _friendly_http_error_message(
                exc.status_code,
                content_type=getattr(exc, "content_type", ""),
                body_prefix=getattr(exc, "body_prefix", ""),
            ),
            http_status=_mapped_http_status(exc.status_code),
        )
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


def _safe_error_message(exc: Exception) -> str:
    return str(exc or "").replace("\n", " ")[:500]


def _friendly_http_error_message(status_code: int, *, content_type: str = "", body_prefix: str = "") -> str:
    if status_code in {401, 403}:
        message = "WB API token has no Supplies permission or is invalid"
    elif status_code == 429:
        message = "WB supplies API rate limit returned 429; retry later"
    elif status_code >= 500:
        message = f"WB supplies API upstream is unavailable: status {status_code}"
    else:
        message = f"WB supplies API request failed with status {status_code}"
    details: list[str] = []
    if content_type:
        details.append(f"content-type={content_type}")
    if body_prefix:
        details.append(f"body_prefix={body_prefix}")
    if details:
        message += " (" + "; ".join(details) + ")"
    return message


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


def _sum_package_barcode_quantity(rows: list[Mapping[str, Any]]) -> float | None:
    found = False
    total = 0.0
    for row in rows:
        barcodes = row.get("barcodes") if isinstance(row, Mapping) else None
        if not isinstance(barcodes, list):
            continue
        for barcode_row in barcodes:
            if not isinstance(barcode_row, Mapping):
                continue
            value = _optional_number(_first_value(barcode_row, "quantity", "qty"))
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
