"""Read-only WB FBW supplies registry block for sheet_vitrina_v1 operator UI."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
from math import ceil
import threading
import time
from typing import Any, Mapping, Protocol
import uuid

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
SYNC_MODE_INCREMENTAL_REFRESH = "incremental_refresh"
SYNC_MODE_FULL_BACKFILL = "full_backfill"
SYNC_MODE_ENRICH_MISSING = "enrich_missing"
RUN_ACTIVE_STATUSES = {"queued", "running"}
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
        self._run_lock = threading.Lock()

    def list_supplies(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        request = _normalize_list_request(params or {})
        rows = self.runtime.list_wb_supplies()
        warehouses = self.runtime.list_wb_supplies_warehouses()
        state = self.runtime.load_wb_supplies_sync_state()
        active_run = self.runtime.load_active_wb_supplies_sync_run()
        cache_completeness = _cache_completeness(state, rows)
        sorted_rows = _sort_rows(rows, request["sort_key"], request["sort_dir"])
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
                    "sort_key": request["sort_key"],
                    "sort_dir": request["sort_dir"],
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
            "sort": {
                "key": request["sort_key"],
                "dir": request["sort_dir"],
            },
            "schema": {"columns": SCHEMA_COLUMNS},
            "rows": page_rows,
            "sync_state": _public_sync_state(state, cache_completeness),
            "active_run": active_run if active_run and active_run.get("status") in RUN_ACTIVE_STATUSES else None,
        }

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
        try:
            warehouses = self._fetch_warehouses(warnings)
            list_result = _coerce_list_result(
                self.source.list_supplies(
                    limit=request["limit"],
                    offset=0,
                    status_ids=request["status_ids"],
                    dates=request["dates"],
                )
            )
            warehouse_by_id = _warehouse_map(warehouses)
            sync_result = self._prepare_list_rows_for_upsert(
                raw_rows=list_result.rows,
                warehouse_by_id=warehouse_by_id,
                synced_at=synced_at,
                enrich=request["enrich"] != "none",
                changed_only=request["enrich"] != "all",
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
                latest_synced_count=list_result.raw_count,
                last_mode=SYNC_MODE_INCREMENTAL_REFRESH,
                latest_window_synced_at=synced_at,
                latest_window_limit=list_result.limit,
                latest_window_returned_count=list_result.raw_count,
                may_have_more=list_result.raw_count >= list_result.limit,
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
            raw_fetched=list_result.raw_count,
            upserted=len(normalized_rows),
            new_rows=sync_result["new_rows"],
            changed_rows=sync_result["changed_rows"],
            unchanged_rows=sync_result["unchanged_rows"],
            enriched=sync_result["enriched"],
            failed_enrich=sync_result["failed_enrich"],
            may_have_more=list_result.raw_count >= list_result.limit,
            logs=[_run_log(completed_at, "latest-window sync completed")],
        )
        response = self.list_supplies(
            {
                "limit": DEFAULT_PAGE_LIMIT,
                "offset": 0,
                "size_filter": SIZE_FILTER_MAIN_250,
                "sort_key": DEFAULT_SORT_KEY,
                "sort_dir": DEFAULT_SORT_DIR,
            }
        )
        response["sync"] = {
            "status": "ok",
            "mode": SYNC_MODE_INCREMENTAL_REFRESH,
            "run_id": run_id,
            "synced_at": synced_at,
            "limit": list_result.limit,
            "offset": 0,
            "raw_fetched_count": list_result.raw_count,
            "upserted_count": len(normalized_rows),
            "new_rows": sync_result["new_rows"],
            "changed_rows": sync_result["changed_rows"],
            "unchanged_rows": sync_result["unchanged_rows"],
            "enriched": sync_result["enriched"],
            "failed_enrich": sync_result["failed_enrich"],
            "may_have_more": list_result.raw_count >= list_result.limit,
            "latest_window_only": True,
            "enrich": request["enrich"],
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

    def get_supply(self, supply_id: str) -> dict[str, Any]:
        normalized_id = str(supply_id or "").strip()
        if not normalized_id:
            raise WbSuppliesBlockError("supply_id is required", http_status=400)
        record = self.runtime.load_wb_supply_record(normalized_id)
        if record is None:
            raise WbSuppliesBlockError(f"WB supply not found in cache: {normalized_id}", http_status=404)
        record = self._ensure_supply_detail_record(record)
        detail = _supply_detail_payload(record)
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "meta": {"source": WB_SUPPLIES_SOURCE_LABEL, "read_only": True},
            **detail,
        }

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
        warehouse_by_id = _warehouse_map(self.runtime.list_wb_supplies_warehouses())
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
        return next_record

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
        logs = (logs + [_run_log(completed_at, f"full backfill {status}")])[-20:]
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

    def _prepare_list_rows_for_upsert(
        self,
        *,
        raw_rows: list[Mapping[str, Any]],
        warehouse_by_id: Mapping[str, str],
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
            "enriched": 0,
            "failed_enrich": 0,
        }
        for raw_row in raw_rows:
            cache_key = _stable_cache_key(raw_row)
            lookup_id, is_preorder_id = _resolve_upstream_lookup_id(raw_row)
            existing = existing_by_key.get(cache_key) or existing_by_key.get(lookup_id)
            raw_list_hash = _stable_payload_hash(raw_row)
            raw_updated_date = _first_string(raw_row, "updatedDate", "updated_at", "updated_date")
            existing_hash = str((existing or {}).get("raw_list_hash") or "")
            existing_updated_date = str(((existing or {}).get("normalized") or {}).get("updated_date") or "")
            force_enrich = bool(force_enrich_cache_keys and cache_key in force_enrich_cache_keys)
            is_new = existing is None
            is_changed = (not is_new) and (
                not existing_hash
                or existing_hash != raw_list_hash
                or (raw_updated_date and raw_updated_date != existing_updated_date)
            )
            needs_enrichment = bool(
                include_missing_enrichment and existing and _row_needs_enrichment(existing.get("normalized") or {})
            )
            if is_new:
                counters["new_rows"] += 1
            elif is_changed or needs_enrichment:
                counters["changed_rows"] += 1
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
                fetched_detail = self._fetch_detail(lookup_id, is_preorder_id=is_preorder_id, warnings=row_warnings)
                fetched_goods = self._fetch_goods(lookup_id, is_preorder_id=is_preorder_id, warnings=row_warnings)
                if fetched_detail is not None:
                    raw_detail = fetched_detail
                if fetched_goods is not None:
                    raw_goods = fetched_goods
            failed_enrich = bool(row_warnings and attempted_enrichment)
            if attempted_enrichment and not failed_enrich:
                counters["enriched"] += 1
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
    return {
        "search": str(params.get("search") or "").strip(),
        "warehouse_id": str(params.get("warehouse_id") or "").strip(),
        "warehouse": str(params.get("warehouse") or "").strip(),
        "status_id": _optional_int(params.get("status_id")),
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
    return {
        "mode": mode,
        "limit": min(max(_optional_int(payload.get("limit")) or DEFAULT_SYNC_LIMIT, 1), 1000),
        "offset": 0,
        "enrich": enrich,
        "status_ids": _normalize_status_ids(payload.get("status_ids") or payload.get("statusIDs")),
        "dates": [dict(item) for item in payload.get("dates") or [] if isinstance(item, Mapping)],
    }


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


def _supply_detail_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(record.get("normalized") or {})
    raw = {
        "list": record.get("raw_list"),
        "detail": record.get("raw_detail"),
        "goods": record.get("raw_goods"),
        "package": record.get("raw_package"),
    }
    supply = dict(normalized)
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
    virtual_type_id = _optional_int(
        _first_non_empty_from_sources(sources, "virtualTypeID", "virtualTypeId", "virtual_type_id")[0]
    )
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
    acceptance_coefficient = _first_number_from_sources(
        sources, "paidAcceptanceCoefficient", "acceptanceCoefficient", "acceptance_coefficient"
    )[0]
    cost_total, cost_evidence = _cost_total(
        sources=sources,
        acceptance_cost=acceptance_cost,
        acceptance_cost_evidence=acceptance_cost_evidence,
        transit_cost=transit_cost,
        transit_cost_evidence=transit_cost_evidence,
        is_transit=bool(transit_warehouse_id or transit_warehouse_name),
        status_id=status_id,
        acceptance_coefficient=acceptance_coefficient,
    )
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
        "virtual_type_id": virtual_type_id,
        "box_type_label": _box_type_label(box_type_id),
        "type_label": _type_label(box_type_id=box_type_id, virtual_type_id=virtual_type_id, is_transit=is_transit),
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
    reverse = sort_dir == "desc"
    if sort_key == "supply_date":
        return sorted(rows, key=_supply_date_sort_key, reverse=reverse)
    return sorted(rows, key=_supply_date_sort_key, reverse=True)


def _supply_date_sort_key(row: Mapping[str, Any]) -> tuple[str, str]:
    date_value = (
        str(row.get("supply_date") or "")
        or str(row.get("fact_date") or "")
        or str(row.get("updated_date") or "")
        or str(row.get("source_created_at") or "")
    )
    stable_id = str(row.get("visible_number") or row.get("wb_supply_id") or row.get("supply_id") or "")
    return (date_value, stable_id)


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
