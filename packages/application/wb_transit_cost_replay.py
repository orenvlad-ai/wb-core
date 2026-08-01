"""Bounded late transit-cost materialization and replay enqueue."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from packages.application.our_wb_costs import OurWbCostBlock
from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
)
from packages.application.warehouse_functional import (
    _normalized_wb_record,
    _supply_business_date,
    _validated_wb_goods,
    enqueue_warehouse_targeted_recalculation,
)
from packages.application.warehouse_sync_lock import warehouse_sync_lock


def reconcile_completed_transit_costs(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    cost_block: OurWbCostBlock,
    supply_ids: list[str],
    timestamp_factory: Callable[[], str],
) -> dict[str, Any]:
    """Materialize only named supplies and enqueue their dependent replay."""

    targets = sorted({str(value) for value in supply_ids if str(value)})
    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    materialized_count = 0
    with warehouse_sync_lock(runtime.runtime_dir, blocking=True):
        for supply_id in targets:
            try:
                materialized_count += int(
                    cost_block.materialize_wb_supply_cost_layers(
                        opening_date="2026-07-01",
                        supply_ids=[supply_id],
                    )
                )
                enrichment = runtime.load_wb_supply_transit_cost_enrichment(
                    supply_id
                ) or {}
                if (
                    str(enrichment.get("status") or "") != "success"
                    or enrichment.get("amount") is None
                    or not str(enrichment.get("source_revision") or "")
                ):
                    raise ValueError(
                        "canonical successful transit fact is missing"
                    )
                record = runtime.load_wb_supply_record(supply_id)
                if record is None:
                    raise ValueError("WB supply record is missing")
                normalized = _normalized_wb_record(record)
                nm_ids = sorted(
                    {
                        int(item["nm_id"])
                        for item in _validated_wb_goods(normalized)
                    }
                )
                effective_date = _originating_business_date(
                    normalized,
                    record,
                )
                if not nm_ids or not effective_date:
                    raise ValueError(
                        "originating supply date/SKU composition is incomplete"
                    )
                queued = enqueue_warehouse_targeted_recalculation(
                    runtime=runtime,
                    stable_source_id=f"wb_transit_cost:{supply_id}",
                    source_revision=str(enrichment["source_revision"]),
                    effective_date=effective_date,
                    affected_nm_ids=nm_ids,
                    requested_at=timestamp_factory(),
                )
                queue_status = str(queued.get("status") or "queued")
                runtime.update_wb_supply_transit_cost_recalculation_status(
                    supply_id,
                    status=(
                        "complete" if queue_status == "complete" else "queued"
                    ),
                    updated_at=timestamp_factory(),
                )
                results.append(
                    {
                        "supply_id": supply_id,
                        "effective_date": effective_date,
                        "affected_nm_ids": nm_ids,
                        "queue": queued,
                    }
                )
            except Exception as exc:
                error = str(exc).replace("\n", " ")[:1000]
                runtime.update_wb_supply_transit_cost_recalculation_status(
                    supply_id,
                    status="recalculation_error",
                    error=error,
                    updated_at=timestamp_factory(),
                )
                failures.append({"supply_id": supply_id, "error": error})
    if failures:
        raise ValueError(
            "transit cost replay enqueue failed: "
            + json.dumps(failures, ensure_ascii=False, sort_keys=True)
        )
    return {
        "target_supply_ids": targets,
        "wb_supply_cost_layers_materialized": materialized_count,
        "targeted_recalculations": results,
        "physical_movements_created": 0,
    }


def _originating_business_date(
    normalized: Mapping[str, Any],
    record: Mapping[str, Any],
) -> str:
    business_date = _supply_business_date(normalized, record)
    if business_date:
        return business_date
    for key in ("supply_date", "fact_date", "updated_date", "source_created_at"):
        value = str(normalized.get(key) or record.get(key) or "")[:10]
        if value:
            return value
    return ""
