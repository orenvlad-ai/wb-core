"""Immutable evidence helpers for supply calculation registry records."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, timedelta
import hashlib
import json
from typing import Any, Iterable, Mapping

from packages.application.factory_order_sales_history import SALES_HISTORY_SOURCE_KEY
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.wb_regional_demand import (
    DEFAULT_MAX_LOOKUP_DAYS,
    DEFAULT_MIN_LOOKUP_DAYS,
    STOCKS_SOURCE_KEY,
)


EVIDENCE_CONTRACT_NAME = "sheet_vitrina_v1_supply_calculation_evidence"
EVIDENCE_CONTRACT_VERSION = 1


def canonical_fingerprint(value: Any) -> str:
    normalized = _json_ready(value)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def build_factory_order_calculation_evidence(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    active_skus: Iterable[tuple[int, str]],
    report_date: date,
    sales_lookup_days: int,
    order_count_samples_by_nm: Mapping[int, Any],
    stock_response: Any,
    incident_projection: Mapping[str, Any],
    stock_ff_source: str,
    stock_ff_rows: Iterable[Any],
    factory_inbound_source: str,
    effective_inbound_factory_rows: Iterable[Any],
    selected_wb_supply_ids: Iterable[str],
    wb_supply_overlay: Mapping[str, Any],
    dataset_types: Iterable[str],
) -> dict[str, Any]:
    history_date_from = report_date - timedelta(days=max(int(sales_lookup_days), 1))
    history_date_to = report_date - timedelta(days=1)
    stock_ff_payload = list(stock_ff_rows)
    inbound_payload = list(effective_inbound_factory_rows)
    return {
        "contract_name": EVIDENCE_CONTRACT_NAME,
        "contract_version": EVIDENCE_CONTRACT_VERSION,
        "calculation_type": "factory_order",
        "active_skus": _active_sku_evidence(active_skus),
        "calculation_inputs": {
            "order_count_samples_by_nm_fingerprint": canonical_fingerprint(
                order_count_samples_by_nm
            ),
        },
        "sources": {
            "sales_history": runtime.describe_temporal_source_window(
                source_key=SALES_HISTORY_SOURCE_KEY,
                date_from=history_date_from.isoformat(),
                date_to=history_date_to.isoformat(),
            ),
            "current_stocks": _current_stock_evidence(stock_response),
            "stock_ff": {
                "source": str(stock_ff_source or ""),
                "row_count": len(stock_ff_payload),
                "rows_fingerprint": canonical_fingerprint(stock_ff_payload),
            },
            "factory_inbound": {
                "source": str(factory_inbound_source or ""),
                "row_count": len(inbound_payload),
                "rows_fingerprint": canonical_fingerprint(inbound_payload),
            },
            "datasets": {
                str(dataset_type): runtime.describe_factory_order_dataset_evidence(
                    str(dataset_type)
                )
                for dataset_type in dataset_types
            },
            "wb_supply_overlay": _wb_supply_overlay_evidence(
                selected_wb_supply_ids=selected_wb_supply_ids,
                wb_supply_overlay=wb_supply_overlay,
            ),
        },
        "incident_policy": _incident_policy_evidence(incident_projection),
    }


def build_wb_regional_calculation_evidence(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    active_skus: Iterable[tuple[int, str]],
    report_date: date,
    requested_valid_day_count: int,
    regional_demand_by_nm: Mapping[int, Any],
    stock_response: Any,
    incident_projection: Mapping[str, Any],
    stock_ff_source: str,
    stock_ff_rows: Iterable[Any],
    selected_wb_supply_ids: Iterable[str],
    wb_supply_overlay: Mapping[str, Any],
    dataset_types: Iterable[str],
    export_nomenclature_items: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    lookup_days = min(
        DEFAULT_MAX_LOOKUP_DAYS,
        max(DEFAULT_MIN_LOOKUP_DAYS, max(int(requested_valid_day_count), 1) * 8),
    )
    sales_date_from = report_date - timedelta(days=lookup_days)
    stock_date_from = report_date - timedelta(days=lookup_days + 1)
    date_to = report_date - timedelta(days=1)
    stock_ff_payload = list(stock_ff_rows)
    nomenclature_payload = [
        {
            "nm_id": item.get("nm_id"),
            "barcode": item.get("barcode"),
            "barcodes": item.get("barcodes"),
            "is_active": item.get("is_active"),
        }
        for item in export_nomenclature_items
        if isinstance(item, Mapping)
    ]
    return {
        "contract_name": EVIDENCE_CONTRACT_NAME,
        "contract_version": EVIDENCE_CONTRACT_VERSION,
        "calculation_type": "wb_regional",
        "active_skus": _active_sku_evidence(active_skus),
        "calculation_inputs": {
            "regional_demand_by_nm_fingerprint": canonical_fingerprint(
                regional_demand_by_nm
            ),
        },
        "sources": {
            "sales_history": runtime.describe_temporal_source_window(
                source_key=SALES_HISTORY_SOURCE_KEY,
                date_from=sales_date_from.isoformat(),
                date_to=date_to.isoformat(),
            ),
            "regional_stock_history": runtime.describe_temporal_source_window(
                source_key=STOCKS_SOURCE_KEY,
                date_from=stock_date_from.isoformat(),
                date_to=date_to.isoformat(),
            ),
            "current_stocks": _current_stock_evidence(stock_response),
            "stock_ff": {
                "source": str(stock_ff_source or ""),
                "row_count": len(stock_ff_payload),
                "rows_fingerprint": canonical_fingerprint(stock_ff_payload),
            },
            "datasets": {
                str(dataset_type): runtime.describe_factory_order_dataset_evidence(
                    str(dataset_type)
                )
                for dataset_type in dataset_types
            },
            "wb_supply_overlay": _wb_supply_overlay_evidence(
                selected_wb_supply_ids=selected_wb_supply_ids,
                wb_supply_overlay=wb_supply_overlay,
            ),
            "export_nomenclature": {
                "row_count": len(nomenclature_payload),
                "fingerprint": canonical_fingerprint(nomenclature_payload),
            },
        },
        "incident_policy": _incident_policy_evidence(incident_projection),
    }


def _active_sku_evidence(active_skus: Iterable[tuple[int, str]]) -> dict[str, Any]:
    payload = [
        {"nm_id": int(nm_id), "display_name": str(display_name)}
        for nm_id, display_name in active_skus
    ]
    return {
        "count": len(payload),
        "fingerprint": canonical_fingerprint(payload),
    }


def _current_stock_evidence(stock_response: Any) -> dict[str, Any]:
    items = list(getattr(stock_response, "items", []) or [])
    warehouse_rows = list(getattr(stock_response, "warehouse_rows", []) or [])
    return {
        "snapshot_date": str(getattr(stock_response, "snapshot_date", "") or ""),
        "fetched_at": str(getattr(stock_response, "fetched_at", "") or ""),
        "pagination_complete": bool(getattr(stock_response, "pagination_complete", False)),
        "warehouse_granularity_complete": bool(
            getattr(stock_response, "warehouse_granularity_complete", True)
        ),
        "raw_rows_digest": str(getattr(stock_response, "raw_rows_digest", "") or ""),
        "item_count": len(items),
        "items_fingerprint": canonical_fingerprint(items),
        "warehouse_row_count": len(warehouse_rows),
        "warehouse_rows_fingerprint": canonical_fingerprint(warehouse_rows),
    }


def _wb_supply_overlay_evidence(
    *,
    selected_wb_supply_ids: Iterable[str],
    wb_supply_overlay: Mapping[str, Any],
) -> dict[str, Any]:
    selected_ids = [str(item) for item in selected_wb_supply_ids if str(item or "").strip()]
    selected_supplies = list(wb_supply_overlay.get("selected_supplies") or [])
    skipped_supplies = list(wb_supply_overlay.get("skipped_supplies") or [])
    return {
        "selected_supply_ids": selected_ids,
        "selected_supply_ids_fingerprint": canonical_fingerprint(selected_ids),
        "selected_supplies_fingerprint": canonical_fingerprint(selected_supplies),
        "skipped_supplies_fingerprint": canonical_fingerprint(skipped_supplies),
        "payload_fingerprint": canonical_fingerprint(wb_supply_overlay),
    }


def _incident_policy_evidence(projection: Mapping[str, Any]) -> dict[str, Any]:
    policy = projection.get("policy") if isinstance(projection.get("policy"), Mapping) else {}
    quality = projection.get("quality") if isinstance(projection.get("quality"), Mapping) else {}
    return {
        "contract_name": str(projection.get("contract_name") or ""),
        "contract_version": projection.get("contract_version"),
        "policy_revision": int(
            projection.get("policy_revision")
            or quality.get("policy_revision")
            or policy.get("revision")
            or 0
        ),
        "policy_active": bool(projection.get("policy_active", policy.get("active"))),
        "policy_effective_date": str(
            policy.get("effective_from")
            or quality.get("policy_effective_date")
            or ""
        ),
        "snapshot_date": str(
            projection.get("snapshot_date")
            or quality.get("snapshot_date")
            or ""
        ),
        "snapshot_digest": str(
            projection.get("snapshot_digest")
            or projection.get("raw_rows_digest")
            or quality.get("raw_rows_digest")
            or ""
        ),
        "quality_state": str(quality.get("state") or ""),
        "projection_fingerprint": canonical_fingerprint(projection),
    }


def _json_ready(value: Any) -> Any:
    if is_dataclass(value):
        return _json_ready(asdict(value))
    if isinstance(value, Mapping):
        return {
            str(key): _json_ready(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_json_ready(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
