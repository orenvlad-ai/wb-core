"""Guarded targeted ready-snapshot publication of functional WB economics."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping

from packages.business_time import business_date_from_timestamp, current_business_date_iso
from packages.application.calculation_parameters import (
    CalculationParametersBlock,
    calculate_proxy_3,
)
from packages.application.calculation_parameters_v4 import (
    aggregate_proxy_4,
    calculate_proxy_4,
    load_proxy_v4_parameters_for_date,
)
from packages.application.canonical_wb_cost_resolver import CANONICAL_COST_POLICY_DATE
from packages.application.inventory_cost_blend import (
    INVENTORY_COST_BLEND_EFFECTIVE_DATE,
    INVENTORY_COST_BLEND_FORMULA_VERSION,
    aggregate_inventory_cost_evidence,
    build_inventory_cost_blend_lookup,
    inventory_cost_evidence_reason,
)
from packages.application.own_product_capital import OwnProductCapitalBlock
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sqlite_contention import connect_sqlite
from packages.application.sheet_vitrina_v1_our_wb_costs import (
    OUR_WB_PROXY_MARGIN_3_PCT_LABEL,
    OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
    OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_LABEL,
    OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY,
    OUR_WB_PROXY_PROFIT_3_RUB_LABEL,
    OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_UNIT_COST_RUB_LABEL,
    OUR_WB_UNIT_COST_RUB_METRIC_KEY,
    TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY,
)
from packages.application.sheet_vitrina_v1_proxy_v4 import (
    PROXY_V4_MARGIN_PER_UNIT_LABEL_RU,
    PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY,
    PROXY_V4_MARGIN_LABEL_RU,
    PROXY_V4_MARGIN_PCT_METRIC_KEY,
    PROXY_V4_PROFIT_LABEL_RU,
    PROXY_V4_PROFIT_RUB_METRIC_KEY,
    PROXY_V4_SKU_METRIC_KEYS,
    PROXY_V4_TOTAL_MARGIN_PER_UNIT_RUB_METRIC_KEY,
    PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY,
    PROXY_V4_TOTAL_METRIC_KEYS,
    PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY,
)
from packages.application.sheet_vitrina_v1_archived_metrics import ARCHIVED_PUBLIC_METRIC_KEYS
from packages.application.sheet_vitrina_v1_own_product_capital import (
    OWN_AVG_COST_RUB_TOTAL_METRIC_KEY,
    OWN_PRODUCT_CAPITAL_STAGES,
    OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS,
    OWN_PRODUCT_CAPITAL_TOTAL_METRIC_KEYS,
    OWN_TOTAL_CAPITAL_RUB_METRIC_KEY,
    OWN_TOTAL_CAPITAL_RUB_TOTAL_METRIC_KEY,
    OWN_TOTAL_QTY_METRIC_KEY,
    OWN_TOTAL_QTY_TOTAL_METRIC_KEY,
    build_own_product_capital_metric_items,
    own_stage_metric_key,
    own_stage_total_metric_key,
)
from packages.application.sheet_vitrina_v1_proxy_margin_3_historical_backfill import (
    _data_sheet,
    _date_columns,
    _update_data_dimensions,
)
from packages.application.warehouse_functional import (
    FUNCTIONAL_CUTOVER_ID,
    _warehouse_balance_status_presentation,
)
from packages.application.warehouse_recovery_policy import (
    BeforeImageQuery,
    RecoveryState,
    WarehouseRecoveryRegistry,
    capture_before_images,
    recovery_operation_id,
)
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope


CONTRACT_NAME = "sheet_vitrina_v1_functional_economics_backfill"
HISTORICAL_REPAIR_CONTRACT = (
    "sheet_vitrina_v1_functional_economics_historical_repair_required/v1"
)
HISTORICAL_REPAIR_METADATA_KEY = (
    "functional_economics_historical_repair_required"
)
TARGET_KEYS = {
    OUR_WB_UNIT_COST_RUB_METRIC_KEY,
    TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY,
    OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
    OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY,
    }
WAREHOUSE_TARGET_KEYS = set(OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS) | set(
    OWN_PRODUCT_CAPITAL_TOTAL_METRIC_KEYS
)
TARGET_KEYS.update(WAREHOUSE_TARGET_KEYS)
PROXY_V4_TARGET_KEYS = set(PROXY_V4_SKU_METRIC_KEYS) | set(
    PROXY_V4_TOTAL_METRIC_KEYS
)
TARGET_KEYS.update(PROXY_V4_TARGET_KEYS)
PRESENTATION_TARGET_KEYS = WAREHOUSE_TARGET_KEYS | {
    OUR_WB_UNIT_COST_RUB_METRIC_KEY,
    TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY,
}
ARCHIVED_READY_METRIC_KEYS = ARCHIVED_PUBLIC_METRIC_KEYS
MUTATED_READY_METRIC_KEYS = frozenset(TARGET_KEYS | set(ARCHIVED_READY_METRIC_KEYS))
ZERO = Decimal("0")

_WAREHOUSE_INPUT_COMPONENT_NAMES = {
    "dates": "selection_dates",
    "cutover": "functional_cutover",
    "versions": "functional_versions",
    "balances": "functional_balances",
    "supplier_cost_states": "functional_supplier_cost_states",
    "supplier_cost_state_replays": "functional_supplier_cost_state_replays",
    "supplier_cost_state_corrections": "functional_supplier_cost_state_corrections",
    "supplier_cost_state_replay_rollbacks": (
        "functional_supplier_cost_state_replay_rollbacks"
    ),
    "active_version": "active_functional_identity",
    "supplier_shipments": "source_watermark_supplier_shipments",
    "supplier_shipment_lines": "source_watermark_supplier_shipment_lines",
    "cny_ledger_operations": "source_watermark_cny_ledger_operations",
    "supplier_financial_documents": "source_watermark_supplier_financial_documents",
    "supplier_financial_expense_lines": (
        "source_watermark_supplier_financial_expense_lines"
    ),
    "cny_documents": "source_watermark_cny_documents",
    "daily_cost": "daily_wb_cost",
    "parameters": "calculation_settings",
    "proxy_v4_parameters": "proxy_v4_settings",
}


class FunctionalEconomicsBackfillError(RuntimeError):
    pass


def carry_forward_closed_functional_economics_metadata(
    plan: SheetVitrinaV1Envelope,
    *,
    previous_plan: SheetVitrinaV1Envelope,
    business_date: str,
) -> SheetVitrinaV1Envelope:
    """Preserve exact closed-date economics evidence across an ordinary refresh.

    The ordinary Vitrina builder owns source refreshes, not warehouse-history
    certification.  When it reuses last-good closed cells it must therefore
    carry the matching version-bound evidence as one unit.  Current-date
    metadata stays owned by the fresh candidate and is never copied backward.
    """

    business_day = str(business_date or "")[:10]
    try:
        date.fromisoformat(business_day)
    except ValueError as exc:
        raise FunctionalEconomicsBackfillError(
            "ordinary refresh business date is invalid"
        ) from exc
    plan_dates = {
        str(day)[:10]
        for day in getattr(plan, "date_columns", [])
        if str(day or "")[:10]
    }
    closed_dates = {day for day in plan_dates if day < business_day}
    if not closed_dates:
        return plan

    metadata = deepcopy(dict(getattr(plan, "metadata", {}) or {}))
    previous_metadata = dict(getattr(previous_plan, "metadata", {}) or {})

    previous_coverage = previous_metadata.get("warehouse_history_coverage")
    if previous_coverage is not None:
        if not isinstance(previous_coverage, Mapping):
            raise FunctionalEconomicsBackfillError(
                "previous warehouse history coverage must be an object"
            )
        candidate_coverage = metadata.get("warehouse_history_coverage")
        if candidate_coverage is not None and not isinstance(
            candidate_coverage, Mapping
        ):
            raise FunctionalEconomicsBackfillError(
                "candidate warehouse history coverage must be an object"
            )
        merged_coverage = deepcopy(dict(candidate_coverage or {}))
        for day in sorted(closed_dates):
            if day not in previous_coverage:
                continue
            entry = previous_coverage[day]
            if not isinstance(entry, Mapping):
                raise FunctionalEconomicsBackfillError(
                    "previous warehouse history coverage entry must be an object"
                )
            merged_coverage[day] = deepcopy(dict(entry))
        if merged_coverage:
            metadata["warehouse_history_coverage"] = merged_coverage

    previous_registry = _historical_repair_registry_or_none(
        previous_metadata.get(HISTORICAL_REPAIR_METADATA_KEY)
    )
    candidate_registry = _historical_repair_registry_or_none(
        metadata.get(HISTORICAL_REPAIR_METADATA_KEY)
    )
    merged_repair_dates = deepcopy(
        dict(candidate_registry.get("dates") or {})
        if candidate_registry is not None
        else {}
    )
    if previous_registry is not None:
        for day, entry in previous_registry["dates"].items():
            if day in closed_dates:
                merged_repair_dates[day] = deepcopy(dict(entry))
    if merged_repair_dates:
        metadata[HISTORICAL_REPAIR_METADATA_KEY] = {
            "contract_name": HISTORICAL_REPAIR_CONTRACT,
            "status": "historical_repair_required",
            "dates": {
                day: merged_repair_dates[day]
                for day in sorted(merged_repair_dates)
            },
        }
    else:
        metadata.pop(HISTORICAL_REPAIR_METADATA_KEY, None)

    previous_presentation = previous_metadata.get("server_cell_presentation")
    if previous_presentation is not None:
        if not isinstance(previous_presentation, Mapping):
            raise FunctionalEconomicsBackfillError(
                "previous server cell presentation must be an object"
            )
        candidate_presentation = metadata.get("server_cell_presentation")
        if candidate_presentation is not None and not isinstance(
            candidate_presentation, Mapping
        ):
            raise FunctionalEconomicsBackfillError(
                "candidate server cell presentation must be an object"
            )
        merged_presentation = deepcopy(dict(candidate_presentation or {}))
        for row_id, raw_by_date in previous_presentation.items():
            metric_key = str(row_id or "").partition("|")[2]
            if metric_key not in PRESENTATION_TARGET_KEYS:
                continue
            if not isinstance(raw_by_date, Mapping):
                raise FunctionalEconomicsBackfillError(
                    "previous target cell presentation must be an object"
                )
            candidate_by_date = merged_presentation.get(str(row_id))
            if candidate_by_date is not None and not isinstance(
                candidate_by_date, Mapping
            ):
                raise FunctionalEconomicsBackfillError(
                    "candidate target cell presentation must be an object"
                )
            merged_by_date = deepcopy(dict(candidate_by_date or {}))
            for day, entry in raw_by_date.items():
                day_key = str(day)[:10]
                if day_key in closed_dates:
                    merged_by_date[day_key] = deepcopy(entry)
            if merged_by_date:
                merged_presentation[str(row_id)] = merged_by_date
        if merged_presentation:
            metadata["server_cell_presentation"] = merged_presentation

    previous_marker = previous_metadata.get("functional_economics_backfill")
    candidate_marker = metadata.get("functional_economics_backfill")
    if candidate_marker is not None and not isinstance(candidate_marker, Mapping):
        raise FunctionalEconomicsBackfillError(
            "candidate functional economics marker must be an object"
        )
    if previous_marker is not None:
        if not isinstance(previous_marker, Mapping):
            raise FunctionalEconomicsBackfillError(
                "previous functional economics marker must be an object"
            )
        marker_from = str(previous_marker.get("date_from") or "")[:10]
        marker_to = str(previous_marker.get("date_to") or "")[:10]
        try:
            date.fromisoformat(marker_from)
            date.fromisoformat(marker_to)
        except ValueError as exc:
            raise FunctionalEconomicsBackfillError(
                "previous functional economics marker date range is invalid"
            ) from exc
        marker_dates_are_closed = (
            marker_from in closed_dates
            and marker_to in closed_dates
            and marker_from <= marker_to
        )
        inventory_publication = previous_marker.get("inventory_cost_publication")
        if inventory_publication is not None and not isinstance(
            inventory_publication, Mapping
        ):
            raise FunctionalEconomicsBackfillError(
                "previous inventory cost publication marker must be an object"
            )
        date_evidence = (
            inventory_publication.get("date_evidence")
            if isinstance(inventory_publication, Mapping)
            else None
        )
        if date_evidence is not None and not isinstance(date_evidence, Mapping):
            raise FunctionalEconomicsBackfillError(
                "previous inventory cost date evidence must be an object"
            )
        marker_evidence_dates_are_closed = all(
            str(day)[:10] in closed_dates
            for day in (date_evidence or {})
        )
        if (
            candidate_marker is None
            and marker_dates_are_closed
            and marker_evidence_dates_are_closed
        ):
            metadata["functional_economics_backfill"] = deepcopy(
                dict(previous_marker)
            )

    return replace(plan, metadata=metadata)


def build_functional_economics_backfill_plan(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    business_date: str | None = None,
    affected_nm_ids: Iterable[int] | None = None,
    earliest_business_date: str | None = None,
    latest_business_date: str | None = None,
    _enforce_business_date_boundary: bool = True,
) -> dict[str, Any]:
    operation_business_date = str(business_date or current_business_date_iso())[:10]
    try:
        date.fromisoformat(operation_business_date)
    except ValueError as exc:
        raise FunctionalEconomicsBackfillError("canonical operation business date is invalid") from exc
    targeted = affected_nm_ids is not None
    target_nm_ids = sorted(
        {
            int(value)
            for value in (affected_nm_ids or [])
            if int(value) > 0
        }
    )
    target_earliest_date = str(earliest_business_date or "")[:10]
    target_latest_date = str(latest_business_date or "")[:10]
    if targeted and not target_nm_ids:
        raise FunctionalEconomicsBackfillError(
            "targeted economics requires affected SKU identities"
        )
    if targeted and not target_earliest_date:
        raise FunctionalEconomicsBackfillError(
            "targeted economics requires earliest business date"
        )
    if target_earliest_date:
        try:
            date.fromisoformat(target_earliest_date)
        except ValueError as exc:
            raise FunctionalEconomicsBackfillError(
                "targeted economics earliest business date is invalid"
            ) from exc
    if target_latest_date:
        try:
            date.fromisoformat(target_latest_date)
        except ValueError as exc:
            raise FunctionalEconomicsBackfillError(
                "targeted economics latest business date is invalid"
            ) from exc
        if not target_earliest_date or target_latest_date < target_earliest_date:
            raise FunctionalEconomicsBackfillError(
                "targeted economics latest business date precedes earliest date"
            )
    with _connect(runtime.db_path) as conn:
        cutover = conn.execute(
            """SELECT cutover_at,plan_fingerprint FROM sheet_vitrina_v1_warehouse_functional_cutovers
               WHERE cutover_id=? AND status='posted'""",
            (FUNCTIONAL_CUTOVER_ID,),
        ).fetchone()
        if cutover is None:
            raise FunctionalEconomicsBackfillError("functional cutover is not posted")
        cutover_business_date = business_date_from_timestamp(str(cutover["cutover_at"]))
        snapshots = [dict(row) for row in conn.execute(
            """SELECT bundle_version,as_of_date,plan_json,refreshed_at
               FROM sheet_vitrina_v1_ready_snapshots ORDER BY bundle_version,as_of_date"""
        ).fetchall()]
    dates = sorted(
        {
            day
            for row in snapshots
            for day in _snapshot_dates(row["plan_json"])
            if not target_earliest_date or day >= target_earliest_date
            if not target_latest_date or day <= target_latest_date
        }
    )
    warehouse_dates = [
        day for day in dates if day >= CANONICAL_COST_POLICY_DATE.isoformat()
    ]
    warehouse_input_manifest = _warehouse_input_manifest(
        runtime,
        dates=dates,
    )
    warehouse_input_manifest_digest = _warehouse_input_manifest_digest_from_value(
        warehouse_input_manifest
    )
    warehouse_input_component_evidence = _warehouse_input_component_evidence(
        warehouse_input_manifest
    )
    capital = OwnProductCapitalBlock(runtime=runtime)
    warehouse_context = _exact_functional_snapshot_context(runtime, warehouse_dates)
    warehouse_covered_nm_ids = {
        day: set(item["covered_nm_ids"])
        for day, item in warehouse_context.items()
    }
    warehouse_version_ids = {
        day: str(item["version_id"])
        for day, item in warehouse_context.items()
    }
    warehouse_exact_dates = set(warehouse_covered_nm_ids)
    warehouse_metrics = {
        day: capital.load_daily_metric_lookup(
            day,
            requested_nm_ids=warehouse_covered_nm_ids[day],
            revalidate_current_sources=True,
        )
        if day in warehouse_exact_dates
        else {}
        for day in dates
    }
    # The ordinary publisher must resolve the exact-date physical capital
    # image before it derives the informational WAC consumed by the visible
    # cost and both Proxy versions.  WB compatibility remains an input, never
    # the post-boundary public result by itself.
    wb_compat_costs = {
        day: runtime.load_our_wb_cost_daily_state(as_of_date=day)
        for day in dates
    }
    costs = {
        day: build_inventory_cost_blend_lookup(
            as_of_date=day,
            wb_compat_lookup=wb_compat_costs[day],
            product_capital_lookup=warehouse_metrics[day],
        )
        for day in dates
    }
    parameters = CalculationParametersBlock(runtime=runtime)
    parameter_by_date = {
        day: parameters.parameters_for_date(
            max(day, CANONICAL_COST_POLICY_DATE.isoformat())
        )
        for day in dates
    }
    proxy_v4_parameter_by_date = {
        day: load_proxy_v4_parameters_for_date(
            runtime=runtime,
            effective_date=day,
        )
        for day in dates
    }
    current_warehouse_input_manifest = _warehouse_input_manifest(
        runtime,
        dates=dates,
    )
    if (
        _warehouse_input_manifest_digest_from_value(current_warehouse_input_manifest)
        != warehouse_input_manifest_digest
    ):
        raise _material_component_drift_error(
            phase="dry_run_revalidation",
            expected=warehouse_input_component_evidence,
            actual=_warehouse_input_component_evidence(
                current_warehouse_input_manifest
            ),
        )
    source_fingerprint = "sha256:" + _hash(
        {
            "cutover_fingerprint": str(cutover["plan_fingerprint"]),
            "costs": costs,
            "wb_compat_costs": wb_compat_costs,
            "warehouse_metrics": warehouse_metrics,
            "warehouse_exact_dates": sorted(warehouse_exact_dates),
            "warehouse_covered_nm_ids": {
                day: sorted(nm_ids)
                for day, nm_ids in sorted(warehouse_covered_nm_ids.items())
            },
            "warehouse_version_ids": warehouse_version_ids,
            "parameters": {day: item.public() for day, item in parameter_by_date.items()},
            "proxy_v4_parameters": {
                day: item.public() if item is not None else None
                for day, item in proxy_v4_parameter_by_date.items()
            },
            "target_scope": (
                {
                    "affected_nm_ids": target_nm_ids,
                    "earliest_business_date": target_earliest_date,
                    "latest_business_date": target_latest_date or None,
                }
                if targeted
                else {}
            ),
        }
    )
    updates: list[dict[str, Any]] = []
    changed_cells = 0
    inserted_rows = 0
    archived_rows_removed = 0
    presentation_changes = 0
    coverage_changes = 0
    repair_signal_changes = 0
    non_target_before: list[list[str]] = []
    non_target_after: list[list[str]] = []
    non_target_mismatches: list[dict[str, str]] = []
    for snapshot in snapshots:
        try:
            transformed = _transform_snapshot(
                snapshot,
                costs=costs,
                warehouse_metrics=warehouse_metrics,
                warehouse_exact_dates=warehouse_exact_dates,
                warehouse_covered_nm_ids=warehouse_covered_nm_ids,
                warehouse_version_ids=warehouse_version_ids,
                parameters=parameter_by_date,
                proxy_v4_parameters=proxy_v4_parameter_by_date,
                source_fingerprint=source_fingerprint,
                cutover_business_date=cutover_business_date,
                operation_business_date=operation_business_date,
                affected_nm_ids=target_nm_ids if targeted else None,
                earliest_business_date=(
                    target_earliest_date if targeted else None
                ),
                latest_business_date=(
                    target_latest_date if targeted else None
                ),
            )
        except Exception as exc:
            raise FunctionalEconomicsBackfillError(
                "functional economics ready snapshot failed: "
                f"bundle_version={snapshot['bundle_version']} "
                f"as_of_date={snapshot['as_of_date']}: {exc}"
            ) from exc
        non_target_before.append([snapshot["bundle_version"], snapshot["as_of_date"], transformed["non_target_before"]])
        non_target_after.append([snapshot["bundle_version"], snapshot["as_of_date"], transformed["non_target_after"]])
        if transformed["non_target_before"] != transformed["non_target_after"]:
            non_target_mismatches.append(
                {
                    "bundle_version": str(snapshot["bundle_version"]),
                    "as_of_date": str(snapshot["as_of_date"]),
                    "before_digest": str(transformed["non_target_before"]),
                    "after_digest": str(transformed["non_target_after"]),
                }
            )
        changed_cells += int(transformed["changed_cells"])
        inserted_rows += int(transformed["inserted_rows"])
        archived_rows_removed += int(transformed["archived_rows_removed"])
        presentation_changes += int(transformed["presentation_changes"])
        coverage_changes += int(transformed["coverage_changes"])
        repair_signal_changes += int(transformed["repair_signal_changes"])
        # Marker/timestamp churn is not a business change.  Do not force a
        # coherent multi-gigabyte backup when every target cell already equals
        # the canonical value and no row is inserted or archived.
        material_change = any(
            int(transformed[key]) > 0
            for key in (
                "changed_cells",
                "inserted_rows",
                "archived_rows_removed",
                "presentation_changes",
                "coverage_changes",
                "repair_signal_changes",
            )
        )
        if material_change and transformed["after_plan_json"] != snapshot["plan_json"]:
            updates.append(
                {
                    "bundle_version": snapshot["bundle_version"],
                    "as_of_date": snapshot["as_of_date"],
                    "before_plan_sha256": "sha256:" + _sha(snapshot["plan_json"]),
                    "after_plan_json": transformed["after_plan_json"],
                    "changed_cells": transformed["changed_cells"],
                    "inserted_rows": transformed["inserted_rows"],
                    "archived_rows_removed": transformed["archived_rows_removed"],
                    "presentation_changes": transformed["presentation_changes"],
                    "coverage_changes": transformed["coverage_changes"],
                    "repair_signal_changes": transformed[
                        "repair_signal_changes"
                    ],
                    "dates": transformed["dates"],
                }
            )
    before_digest = "sha256:" + _hash(non_target_before)
    after_digest = "sha256:" + _hash(non_target_after)
    if before_digest != after_digest or non_target_mismatches:
        raise FunctionalEconomicsBackfillError(
            "non-target ready-snapshot content changed: "
            + json.dumps(
                non_target_mismatches[:20],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    plan = {
        "contract_name": CONTRACT_NAME,
        "contract_version": "v1",
        "status": "dry_run_ready",
        "cutover_id": FUNCTIONAL_CUTOVER_ID,
        "cutover_at": str(cutover["cutover_at"]),
        "business_date": operation_business_date,
        "source_fingerprint": source_fingerprint,
        "snapshot_count": len(snapshots),
        "date_from": dates[0] if dates else None,
        "date_to": dates[-1] if dates else None,
        "source_dates": dates,
        "changed_snapshot_count": len(updates),
        "changed_cell_count": changed_cells,
        "inserted_row_count": inserted_rows,
        "archived_row_count": archived_rows_removed,
        "presentation_change_count": presentation_changes,
        "coverage_change_count": coverage_changes,
        "historical_repair_signal_change_count": repair_signal_changes,
        "archived_metric_keys": sorted(ARCHIVED_READY_METRIC_KEYS),
        "ready_snapshot_manifest_digest": _snapshot_manifest_digest(snapshots),
        "warehouse_input_manifest_digest": warehouse_input_manifest_digest,
        "warehouse_input_component_evidence": warehouse_input_component_evidence,
        "non_target_digest": before_digest,
        "updates": updates,
    }
    if targeted:
        plan["target_scope"] = {
            "affected_nm_ids": target_nm_ids,
            "earliest_business_date": target_earliest_date,
            "latest_business_date": target_latest_date or None,
            "copy_bytes": 0,
            "full_database_copy": False,
            "finance_raw_rows_read": 0,
            "complexity": "O(affected SKU/date cells + dependent totals)",
        }
    plan["plan_fingerprint"] = _plan_fingerprint(plan)
    if _enforce_business_date_boundary and current_business_date_iso() != operation_business_date:
        raise FunctionalEconomicsBackfillError(
            "functional economics dry-run crossed the canonical business-date boundary"
        )
    return plan


def apply_functional_economics_backfill_plan(
    runtime: RegistryUploadDbBackedRuntime,
    plan: Mapping[str, Any],
    *,
    confirm_fingerprint: str,
    backup_dir: Any,
    verified_backup: Mapping[str, Any] | None = None,
    target_scoped_undo: bool = True,
) -> dict[str, Any]:
    normalized = json.loads(json.dumps(dict(plan), ensure_ascii=False))
    fingerprint = str(normalized.get("plan_fingerprint") or "")
    if fingerprint != str(confirm_fingerprint or "") or fingerprint != _plan_fingerprint(
        {key: value for key, value in normalized.items() if key != "plan_fingerprint"}
    ):
        raise FunctionalEconomicsBackfillError("exact functional economics plan fingerprint is required")
    operation_business_date = str(normalized.get("business_date") or "")[:10]
    if not operation_business_date or current_business_date_iso() != operation_business_date:
        raise FunctionalEconomicsBackfillError(
            "functional economics apply crossed the canonical business-date boundary"
        )
    target_scope = dict(normalized.get("target_scope") or {})
    target_nm_ids = (
        list(target_scope.get("affected_nm_ids") or [])
        if target_scope
        else None
    )
    target_earliest_date = (
        str(target_scope.get("earliest_business_date") or "")
        if target_scope
        else None
    )
    target_latest_date = (
        str(target_scope.get("latest_business_date") or "")
        if target_scope
        else None
    )
    recovery_registry = WarehouseRecoveryRegistry(
        runtime_dir=runtime.runtime_dir,
        db_path=runtime.db_path,
    )
    operation_id = recovery_operation_id(
        "functional_economics_targeted_publication",
        fingerprint,
    )
    existing_recovery = recovery_registry.get_operation(operation_id)
    fresh = build_functional_economics_backfill_plan(
        runtime,
        business_date=operation_business_date,
        affected_nm_ids=target_nm_ids,
        earliest_business_date=target_earliest_date,
        latest_business_date=target_latest_date,
    )
    if str(fresh["plan_fingerprint"]) != fingerprint:
        if (
            existing_recovery is not None
            and existing_recovery.get("lifecycle")
            in {
                RecoveryState.MUTATION_RUNNING.value,
                RecoveryState.FAILED_RECOVERABLE.value,
                RecoveryState.RETAINED.value,
            }
            and not fresh.get("updates")
        ):
            if existing_recovery.get("lifecycle") != RecoveryState.RETAINED.value:
                raise FunctionalEconomicsBackfillError(
                    "committed functional economics operation requires exact "
                    "same-operation query-only reconciliation"
                )
            return {
                **fresh,
                "status": "applied",
                "idempotent": True,
                "database_written": False,
                "backup": {
                    "kind": "target_scoped_before_image",
                    "integrity_check": "ok",
                    "full_database_copy": False,
                    "copy_bytes": 0,
                    "recovery_operation_id": operation_id,
                },
                "recovery_policy": existing_recovery,
            }
        raise _plan_material_drift_error(
            normalized,
            fresh,
            phase="pre_apply_revalidation",
        )
    if not normalized.get("updates"):
        recovery = recovery_registry.plan_noop(
            mutation_kind="functional_economics_targeted_publication",
            closure_kind="sku_date",
            plan_fingerprint=fingerprint,
            scope=target_scope
            or {
                "affected_nm_ids": "all",
                "earliest_business_date": normalized.get("date_from"),
            },
        )
        return {
            **fresh,
            "status": "applied",
            "idempotent": True,
            "database_written": False,
            "recovery_policy": recovery,
        }
    update_predicates: list[str] = []
    update_parameters: list[Any] = []
    after_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for item in normalized["updates"]:
        key = (str(item["bundle_version"]), str(item["as_of_date"]))
        update_predicates.append("(bundle_version=? AND as_of_date=?)")
        update_parameters.extend(key)
        after_by_key[key] = {
            "bundle_version": key[0],
            "as_of_date": key[1],
            "plan_json": str(item["after_plan_json"]),
        }
    policy_images, policy_read_bytes = capture_before_images(
        runtime.db_path,
        [
            BeforeImageQuery(
                table="sheet_vitrina_v1_ready_snapshots",
                query=(
                    "SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE "
                    + " OR ".join(update_predicates)
                    + " ORDER BY bundle_version,as_of_date"
                ),
                parameters=tuple(update_parameters),
                key_columns=("bundle_version", "as_of_date"),
            )
        ],
    )
    for image in policy_images:
        before = dict(image["before"])
        key = (str(before["bundle_version"]), str(before["as_of_date"]))
        after = dict(before)
        after["plan_json"] = after_by_key[key]["plan_json"]
        image["after"] = after
    recovery = recovery_registry.prepare_t1(
        mutation_kind="functional_economics_targeted_publication",
        closure_kind="sku_date",
        plan_fingerprint=fingerprint,
        scope=target_scope
        or {
            "affected_nm_ids": "all",
            "earliest_business_date": normalized.get("date_from"),
        },
        before_images=policy_images,
        source_digest=str(normalized.get("source_fingerprint") or ""),
        non_target_digest=str(normalized.get("non_target_digest") or ""),
        read_bytes=policy_read_bytes,
    )
    if recovery.get("lifecycle") == RecoveryState.VERIFIED.value:
        recovery = recovery_registry.begin_mutation(
            str(recovery["operation_id"]),
            expected_source_digest=str(
                normalized.get("source_fingerprint") or ""
            ),
        )
    backup = {
        "kind": "target_scoped_before_image",
        "integrity_check": "ok",
        "full_database_copy": False,
        "copy_bytes": 0,
        "recovery_operation_id": str(recovery["operation_id"]),
        "actual_undo_bytes": int(recovery.get("actual_bytes") or 0),
    }
    if current_business_date_iso() != operation_business_date:
        recovery_registry.fail_recoverable(
            str(recovery["operation_id"]),
            error="business date changed before bounded economics mutation",
            next_action="replan_functional_economics",
        )
        raise FunctionalEconomicsBackfillError(
            "functional economics apply crossed the canonical business-date boundary during backup"
        )
    with _connect(runtime.db_path) as schema_conn:
        schema_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS
            sheet_vitrina_v1_functional_economics_undo_manifests(
                manifest_digest TEXT PRIMARY KEY,
                plan_fingerprint TEXT NOT NULL UNIQUE,
                before_images_json TEXT NOT NULL,
                after_images_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                rolled_back_at TEXT
            )
            """
        )
        schema_conn.commit()
    before_images: list[dict[str, Any]] = []
    after_images: list[dict[str, Any]] = []
    revalidation_telemetry: dict[str, Any] = {}
    try:
        with _connect(runtime.db_path) as conn:
            # Keep the expensive rebuild outside any SQLite transaction.  The
            # connection-local data_version observer is telemetry only: it
            # intentionally sees unrelated same-store commits such as FBS
            # shadow polling and therefore cannot be a semantic publication
            # gate.  Exact material components are re-read on this same
            # connection after BEGIN IMMEDIATE, immediately before writes.
            data_version_before = int(conn.execute("PRAGMA data_version").fetchone()[0])
            final_fresh = build_functional_economics_backfill_plan(
                runtime,
                business_date=operation_business_date,
                affected_nm_ids=target_nm_ids,
                earliest_business_date=target_earliest_date,
                latest_business_date=target_latest_date,
            )
            if str(final_fresh.get("plan_fingerprint") or "") != fingerprint:
                raise _plan_material_drift_error(
                    normalized,
                    final_fresh,
                    phase="lock_free_revalidation",
                )
            data_version_after_revalidation = int(
                conn.execute("PRAGMA data_version").fetchone()[0]
            )
            _before_functional_economics_write_lock()
            conn.execute("BEGIN IMMEDIATE")
            data_version_at_writer_lock = int(
                conn.execute("PRAGMA data_version").fetchone()[0]
            )
            revalidation_telemetry = {
                "semantic_gate": False,
                "data_version_before": data_version_before,
                "data_version_after_lock_free_revalidation": (
                    data_version_after_revalidation
                ),
                "data_version_at_writer_lock": data_version_at_writer_lock,
                "changed_during_lock_free_revalidation": (
                    data_version_after_revalidation != data_version_before
                ),
                "changed_before_writer_lock": (
                    data_version_at_writer_lock
                    != data_version_after_revalidation
                ),
                "changed_since_observer_started": (
                    data_version_at_writer_lock != data_version_before
                ),
            }
            if current_business_date_iso() != operation_business_date:
                raise FunctionalEconomicsBackfillError(
                    "functional economics apply crossed the canonical business-date boundary before write"
                )
            _revalidate_functional_economics_material_cas(
                conn,
                normalized,
                telemetry=revalidation_telemetry,
            )
            for item in normalized["updates"]:
                before = conn.execute(
                    """SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots
                       WHERE bundle_version=? AND as_of_date=?""",
                    (item["bundle_version"], item["as_of_date"]),
                ).fetchone()
                if before is None or "sha256:" + _sha(str(before["plan_json"])) != item["before_plan_sha256"]:
                    raise FunctionalEconomicsBackfillError("ready snapshot drifted before atomic backfill")
                before_images.append(
                    {
                        "bundle_version": str(item["bundle_version"]),
                        "as_of_date": str(item["as_of_date"]),
                        "plan_json": str(before["plan_json"]),
                    }
                )
                after_images.append(
                    {
                        "bundle_version": str(item["bundle_version"]),
                        "as_of_date": str(item["as_of_date"]),
                        "plan_json": str(item["after_plan_json"]),
                    }
                )
                _before_functional_economics_target_update(
                    connection=conn,
                    item=item,
                )
                cursor = conn.execute(
                    """UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?
                       WHERE bundle_version=? AND as_of_date=? AND plan_json=?""",
                    (
                        item["after_plan_json"],
                        item["bundle_version"],
                        item["as_of_date"],
                        before["plan_json"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise FunctionalEconomicsBackfillError("ready snapshot optimistic update conflict")
            for item in normalized["updates"]:
                stored = conn.execute(
                    """SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots
                       WHERE bundle_version=? AND as_of_date=?""",
                    (item["bundle_version"], item["as_of_date"]),
                ).fetchone()
                if stored is None or str(stored["plan_json"]) != str(item["after_plan_json"]):
                    raise FunctionalEconomicsBackfillError(
                        "functional economics in-transaction readback failed"
                    )
            manifest_material = {
                "plan_fingerprint": fingerprint,
                "before_images": before_images,
                "after_images": after_images,
            }
            manifest_digest = "sha256:" + _hash(manifest_material)
            conn.execute(
                """
                INSERT INTO
                sheet_vitrina_v1_functional_economics_undo_manifests(
                    manifest_digest,plan_fingerprint,before_images_json,
                    after_images_json,status,created_at,rolled_back_at
                ) VALUES(?,?,?,?,'ready',?,NULL)
                ON CONFLICT(plan_fingerprint) DO NOTHING
                """,
                (
                    manifest_digest,
                    fingerprint,
                    json.dumps(
                        before_images,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        after_images,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    operation_business_date,
                ),
            )
            if current_business_date_iso() != operation_business_date:
                raise FunctionalEconomicsBackfillError(
                    "functional economics apply crossed the canonical business-date boundary before commit"
                )
            recovery_registry.record_mutation_commit(
                conn,
                str(recovery["operation_id"]),
                after_digest=manifest_digest,
                non_target_digest=str(normalized.get("non_target_digest") or ""),
            )
            conn.commit()
    except Exception as exc:
        recovery_registry.fail_recoverable(
            str(recovery["operation_id"]),
            error=str(exc),
            next_action="resume_targeted_economics_publication",
        )
        raise
    try:
        readback = build_functional_economics_backfill_plan(
            runtime,
            business_date=operation_business_date,
            affected_nm_ids=target_nm_ids,
            earliest_business_date=target_earliest_date,
            latest_business_date=target_latest_date,
            _enforce_business_date_boundary=False,
        )
    except Exception as exc:
        recovery_registry.fail_recoverable(
            str(recovery["operation_id"]),
            error=str(exc),
            next_action="retry_targeted_economics_readback",
        )
        raise
    if readback.get("updates"):
        recovery_registry.fail_recoverable(
            str(recovery["operation_id"]),
            error="functional economics readback is not a no-op",
            next_action="retry_or_rollback_targeted_economics",
        )
        raise FunctionalEconomicsBackfillError("functional economics backfill is not idempotent")
    recovery = recovery_registry.retain(
        str(recovery["operation_id"]),
        after_digest=manifest_digest,
        non_target_digest=str(normalized.get("non_target_digest") or ""),
    )
    return {
        **readback,
        "status": "applied",
        "idempotent": False,
        "database_written": True,
        "applied_snapshot_count": len(normalized["updates"]),
        "backup": backup,
        "recovery_policy": recovery,
        "applied_plan_fingerprint": fingerprint,
        "rollback_manifest_digest": manifest_digest,
        "lock_free_revalidation_telemetry": revalidation_telemetry,
    }


def readback_functional_economics_committed_operation(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    operation_id: str,
    plan_fingerprint: str,
    manifest_digest: str,
    non_target_digest: str,
) -> dict[str, Any]:
    """Prove one committed economics mutation without changing its recovery state."""

    selected_operation = str(operation_id or "").strip()
    selected_plan = str(plan_fingerprint or "").strip()
    selected_manifest = str(manifest_digest or "").strip()
    selected_non_target = str(non_target_digest or "").strip()
    if not selected_operation:
        raise FunctionalEconomicsBackfillError(
            "exact functional economics recovery operation is required"
        )
    if not selected_plan.startswith("sha256:"):
        raise FunctionalEconomicsBackfillError(
            "exact functional economics plan fingerprint is required"
        )
    if not selected_manifest.startswith("sha256:"):
        raise FunctionalEconomicsBackfillError(
            "exact functional economics undo manifest is required"
        )
    if not selected_non_target.startswith("sha256:"):
        raise FunctionalEconomicsBackfillError(
            "exact functional economics non-target digest is required"
        )
    recovery_registry = WarehouseRecoveryRegistry(
        runtime_dir=runtime.runtime_dir,
        db_path=runtime.db_path,
    )
    operation = recovery_registry.get_operation(selected_operation)
    if operation is None:
        raise FunctionalEconomicsBackfillError(
            "functional economics recovery operation was not found"
        )
    if (
        str(operation.get("operation_kind") or "")
        != "functional_economics_targeted_publication"
        or str(operation.get("closure_kind") or "") != "sku_date"
        or str(operation.get("tier") or "") != "T1"
        or str(operation.get("plan_fingerprint") or "") != selected_plan
        or str(operation.get("non_target_digest") or "")
        != selected_non_target
    ):
        raise FunctionalEconomicsBackfillError(
            "functional economics recovery identity does not match exact expectations"
        )
    lifecycle = str(operation.get("lifecycle") or "")
    if lifecycle not in {
        RecoveryState.FAILED_RECOVERABLE.value,
        RecoveryState.RETAINED.value,
    }:
        raise FunctionalEconomicsBackfillError(
            "functional economics recovery is not reconcilable from its current lifecycle"
        )

    uri = f"file:{Path(runtime.db_path).resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=120) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise FunctionalEconomicsBackfillError(
                "functional economics reconciliation is not query-only"
            )
        manifest_row = conn.execute(
            """
            SELECT manifest_digest,plan_fingerprint,before_images_json,
                   after_images_json,status,rolled_back_at
            FROM sheet_vitrina_v1_functional_economics_undo_manifests
            WHERE manifest_digest=? AND plan_fingerprint=?
            """,
            (selected_manifest, selected_plan),
        ).fetchone()
        if manifest_row is None or str(manifest_row["status"]) != "ready":
            raise FunctionalEconomicsBackfillError(
                "functional economics undo manifest is not ready"
            )
        if str(manifest_row["rolled_back_at"] or ""):
            raise FunctionalEconomicsBackfillError(
                "functional economics undo manifest was rolled back"
            )
        before_images = json.loads(str(manifest_row["before_images_json"]))
        after_images = json.loads(str(manifest_row["after_images_json"]))
        if not isinstance(before_images, list) or not isinstance(after_images, list):
            raise FunctionalEconomicsBackfillError(
                "functional economics undo images are invalid"
            )
        manifest_material = {
            "plan_fingerprint": selected_plan,
            "before_images": before_images,
            "after_images": after_images,
        }
        if "sha256:" + _hash(manifest_material) != selected_manifest:
            raise FunctionalEconomicsBackfillError(
                "functional economics undo manifest digest does not match its images"
            )
        before_keys = [
            (str(item["bundle_version"]), str(item["as_of_date"]))
            for item in before_images
        ]
        after_by_key = {
            (str(item["bundle_version"]), str(item["as_of_date"])): str(
                item["plan_json"]
            )
            for item in after_images
        }
        if (
            not before_keys
            or len(before_keys) != len(set(before_keys))
            or set(before_keys) != set(after_by_key)
            or len(after_by_key) != len(after_images)
        ):
            raise FunctionalEconomicsBackfillError(
                "functional economics undo image keys are incomplete or duplicated"
            )
        current_non_target = "sha256:" + _hash(
            [
                [
                    str(row["bundle_version"]),
                    str(row["as_of_date"]),
                    _non_target_digest(json.loads(str(row["plan_json"]))),
                ]
                for row in conn.execute(
                    """
                    SELECT bundle_version,as_of_date,plan_json
                    FROM sheet_vitrina_v1_ready_snapshots
                    ORDER BY bundle_version,as_of_date
                    """
                )
            ]
        )
        if current_non_target != selected_non_target:
            raise FunctionalEconomicsBackfillError(
                "functional economics committed non-target readback drifted"
            )
        for key in sorted(after_by_key):
            current = conn.execute(
                """
                SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots
                WHERE bundle_version=? AND as_of_date=?
                """,
                key,
            ).fetchone()
            if current is None or str(current["plan_json"]) != after_by_key[key]:
                raise FunctionalEconomicsBackfillError(
                    "functional economics committed after-image readback drifted"
                )

    committed_after = str(operation.get("after_digest") or "")
    if committed_after and committed_after != selected_manifest:
        raise FunctionalEconomicsBackfillError(
            "functional economics recovery owns a different committed after digest"
        )
    evidence = {
        "contract_name": "functional_economics_committed_operation_reconciliation/v1",
        "status": "exact_commit_confirmed",
        "operation_id": selected_operation,
        "operation_kind": "functional_economics_targeted_publication",
        "tier": "T1",
        "lifecycle": lifecycle,
        "state_version": int(operation.get("state_version") or 0),
        "plan_fingerprint": selected_plan,
        "manifest_digest": selected_manifest,
        "after_image_count": len(after_images),
        "after_images_digest": "sha256:" + _hash(after_images),
        "non_target_digest": current_non_target,
        "legacy_commit_digest_missing": not bool(committed_after),
        "recovery_metadata_mutation_required": (
            lifecycle != RecoveryState.RETAINED.value
            or committed_after != selected_manifest
        ),
        "query_only": True,
        "business_row_write_count": 0,
        "recovery_metadata_write_count": 0,
    }
    evidence["evidence_digest"] = "sha256:" + _hash(evidence)
    return evidence


def retain_reconciled_functional_economics_commit(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    operation_id: str,
    plan_fingerprint: str,
    manifest_digest: str,
    non_target_digest: str,
    evidence_digest: str,
) -> dict[str, Any]:
    """Retain one exact committed operation after a separate query-only proof."""

    evidence = readback_functional_economics_committed_operation(
        runtime,
        operation_id=operation_id,
        plan_fingerprint=plan_fingerprint,
        manifest_digest=manifest_digest,
        non_target_digest=non_target_digest,
    )
    if str(evidence.get("evidence_digest") or "") != str(
        evidence_digest or ""
    ):
        raise FunctionalEconomicsBackfillError(
            "exact functional economics reconciliation evidence digest is required"
        )
    recovery_registry = WarehouseRecoveryRegistry(
        runtime_dir=runtime.runtime_dir,
        db_path=runtime.db_path,
    )
    if (
        str(evidence["lifecycle"]) == RecoveryState.RETAINED.value
        and not bool(evidence["recovery_metadata_mutation_required"])
    ):
        return {
            **evidence,
            "status": "retained",
            "idempotent": True,
            "recovery_metadata_write_count": 0,
        }
    recovery = recovery_registry.retain(
        str(operation_id),
        after_digest=str(manifest_digest),
        non_target_digest=str(non_target_digest),
        transition_evidence_digest=str(evidence_digest),
    )
    if (
        str(recovery.get("lifecycle") or "") != RecoveryState.RETAINED.value
        or str(recovery.get("after_digest") or "") != str(manifest_digest)
    ):
        raise FunctionalEconomicsBackfillError(
            "functional economics committed operation retain readback failed"
        )
    return {
        **evidence,
        "status": "retained",
        "idempotent": False,
        "retained_state_version": int(recovery.get("state_version") or 0),
        "business_row_write_count": 0,
        "recovery_metadata_write_count": 2,
    }


def rollback_target_scoped_functional_economics(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    manifest_digest: str,
) -> dict[str, Any]:
    """Restore exact ready-snapshot before images without a database copy."""

    selected_digest = str(manifest_digest or "").strip()
    if not selected_digest.startswith("sha256:"):
        raise FunctionalEconomicsBackfillError(
            "exact functional economics rollback manifest is required"
        )
    with _connect(runtime.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            manifest = conn.execute(
                """
                SELECT * FROM
                sheet_vitrina_v1_functional_economics_undo_manifests
                WHERE manifest_digest=?
                """,
                (selected_digest,),
            ).fetchone()
            if manifest is None:
                raise FunctionalEconomicsBackfillError(
                    "functional economics rollback manifest was not found"
                )
            if str(manifest["status"]) == "rolled_back":
                return {
                    "rolled_back": False,
                    "idempotent": True,
                    "manifest_digest": selected_digest,
                }
            before_images = json.loads(str(manifest["before_images_json"]))
            after_images = json.loads(str(manifest["after_images_json"]))
            after_by_key = {
                (str(item["bundle_version"]), str(item["as_of_date"])): str(
                    item["plan_json"]
                )
                for item in after_images
            }
            for item in before_images:
                key = (
                    str(item["bundle_version"]),
                    str(item["as_of_date"]),
                )
                current = conn.execute(
                    """
                    SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots
                    WHERE bundle_version=? AND as_of_date=?
                    """,
                    key,
                ).fetchone()
                if current is None or str(current["plan_json"]) != after_by_key.get(
                    key, ""
                ):
                    raise FunctionalEconomicsBackfillError(
                        "functional economics rollback rejected: snapshot changed"
                    )
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_ready_snapshots
                    SET plan_json=?
                    WHERE bundle_version=? AND as_of_date=?
                    """,
                    (str(item["plan_json"]), *key),
                )
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_functional_economics_undo_manifests
                SET status='rolled_back',rolled_back_at=?
                WHERE manifest_digest=? AND status='ready'
                """,
                (current_business_date_iso(), selected_digest),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {
        "rolled_back": True,
        "idempotent": False,
        "manifest_digest": selected_digest,
        "restored_snapshot_count": len(before_images),
    }


def _validate_verified_backup(
    value: Mapping[str, Any],
    *,
    expected_business_date: str = "",
) -> dict[str, Any]:
    backup = json.loads(json.dumps(dict(value), ensure_ascii=False))
    if str(backup.get("integrity_check") or backup.get("source_integrity_check") or "") != "ok":
        raise FunctionalEconomicsBackfillError("verified economics backup integrity_check is required")
    raw_path = str(backup.get("path") or "").strip()
    archive_path = str(backup.get("archive_path") or "").strip()
    if not raw_path and not archive_path:
        raise FunctionalEconomicsBackfillError("verified economics backup path is required")
    backup_business_date = str(backup.get("business_date") or "")[:10]
    if (
        str(backup.get("backup_scope") or "") == "business_day"
        and backup_business_date != str(expected_business_date or backup_business_date)[:10]
    ):
        raise FunctionalEconomicsBackfillError(
            "verified economics backup belongs to another business date"
        )
    if raw_path:
        from apps.sqlite_backup_archive import build_plan

        path = Path(raw_path)
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise FunctionalEconomicsBackfillError("verified economics backup file is unavailable")
        if path.stat().st_mode & 0o777 != 0o600:
            raise FunctionalEconomicsBackfillError("verified economics backup must use mode 0600")
        actual = build_plan(source=path)
        declared_sha = str(backup.get("sha256") or backup.get("source_sha256") or "")
        declared_sha = declared_sha if declared_sha.startswith("sha256:") else f"sha256:{declared_sha}"
        declared_size = int(backup.get("size_bytes") or backup.get("source_size_bytes") or -1)
        if (
            declared_sha != str(actual.get("source_sha256") or "")
            or declared_size != int(actual.get("source_size_bytes") or -2)
            or str(actual.get("source_integrity_check") or "") != "ok"
        ):
            raise FunctionalEconomicsBackfillError(
                "verified economics backup bytes do not match their declared fingerprint"
            )
        backup.update(
            {
                "path": str(actual["source_path"]),
                "size_bytes": int(actual["source_size_bytes"]),
                "sha256": str(actual["source_sha256"]).removeprefix("sha256:"),
                "integrity_check": "ok",
            }
        )
    else:
        from apps.sqlite_backup_archive import verify_archive_manifest

        archive = Path(archive_path)
        if not archive.is_absolute() or archive.is_symlink() or not archive.is_file():
            raise FunctionalEconomicsBackfillError("verified economics backup archive is unavailable")
        if archive.stat().st_mode & 0o777 != 0o600:
            raise FunctionalEconomicsBackfillError("verified economics backup archive must use mode 0600")
        actual = verify_archive_manifest(
            archive.with_name(archive.name + ".manifest.json")
        )
        if str(actual.get("archive_path") or "") != str(archive.resolve()):
            raise FunctionalEconomicsBackfillError(
                "verified economics backup archive provenance does not match"
            )
        for field in ("archive_sha256", "decompressed_sha256", "source_sha256"):
            declared = str(backup.get(field) or "")
            if declared and declared != str(actual.get(field) or ""):
                raise FunctionalEconomicsBackfillError(
                    "verified economics backup archive fingerprint changed"
                )
        backup.update(actual)
    if str(backup.get("backup_scope") or "") == "business_day":
        provenance_date = str(expected_business_date or backup_business_date)[:10]
        expected_name = f"functional-economics-daily-{provenance_date.replace('-', '')}.sqlite3"
        source_identity = Path(str(backup.get("source_path") or backup.get("path") or ""))
        if source_identity.name != expected_name:
            raise FunctionalEconomicsBackfillError(
                "verified economics backup has invalid business-day provenance"
            )
    return backup


def _plan_dates(plan: Mapping[str, Any]) -> list[str]:
    if plan.get("source_dates") is not None:
        return sorted(
            {
                str(day or "")[:10]
                for day in plan.get("source_dates") or []
                if str(day or "")[:10]
            }
        )
    return sorted(
        {
            str(day or "")[:10]
            for update in plan.get("updates") or []
            for day in update.get("dates") or []
            if str(day or "")[:10]
        }
    )


def _warehouse_input_manifest_digest(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    dates: list[str],
    connection: sqlite3.Connection | None = None,
) -> str:
    return _warehouse_input_manifest_digest_from_value(
        _warehouse_input_manifest(
            runtime,
            dates=dates,
            connection=connection,
        )
    )


def _warehouse_input_manifest(
    runtime: RegistryUploadDbBackedRuntime | None,
    *,
    dates: list[str],
    connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Materialize every persisted input used by warehouse/economics projection.

    The same manifest is captured before/after dry-run and rechecked on the
    locked writer connection.  This prevents an hourly functional sync or
    settings publication during the coherent backup from committing stale
    warehouse-history or Proxy cells.
    """

    selected_set = {str(day or "")[:10] for day in dates if str(day or "")[:10]}
    if any(day < CANONICAL_COST_POLICY_DATE.isoformat() for day in selected_set):
        selected_set.add(CANONICAL_COST_POLICY_DATE.isoformat())
    selected = sorted(selected_set)
    own_connection = connection is None
    if connection is None and runtime is None:
        raise FunctionalEconomicsBackfillError(
            "warehouse input manifest requires a runtime or an exact connection"
        )
    if connection is not None:
        conn = connection
    else:
        assert runtime is not None
        conn = _connect(runtime.db_path)
    try:
        table_names = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        manifest: dict[str, Any] = {"dates": selected}
        if "sheet_vitrina_v1_warehouse_functional_cutovers" in table_names:
            manifest["cutover"] = _query_manifest_rows(
                conn,
                """SELECT * FROM sheet_vitrina_v1_warehouse_functional_cutovers
                   WHERE cutover_id=? ORDER BY cutover_id""",
                (FUNCTIONAL_CUTOVER_ID,),
            )
        if selected and {
            "sheet_vitrina_v1_warehouse_functional_versions",
            "sheet_vitrina_v1_warehouse_wb_snapshots",
        }.issubset(table_names):
            placeholders = ",".join("?" for _ in selected)
            versions = _query_manifest_rows(
                conn,
                    f"""SELECT version.*,snapshot.snapshot_id,snapshot.snapshot_date,
                               snapshot.requested_nm_ids_json,snapshot.pagination_complete,
                               snapshot.raw_rows_digest,snapshot.items_json
                    FROM sheet_vitrina_v1_warehouse_functional_versions version
                    JOIN sheet_vitrina_v1_warehouse_wb_snapshots snapshot
                      ON snapshot.version_id=version.version_id
                    WHERE version.cutover_id=?
                      AND snapshot.snapshot_date IN ({placeholders})
                    ORDER BY snapshot.snapshot_date,version.created_at,version.version_id""",
                (FUNCTIONAL_CUTOVER_ID, *selected),
            )
            manifest["versions"] = versions
            version_ids = sorted({str(row["version_id"]) for row in versions})
            if version_ids and "sheet_vitrina_v1_warehouse_functional_balances" in table_names:
                version_placeholders = ",".join("?" for _ in version_ids)
                manifest["balances"] = _query_manifest_rows(
                    conn,
                    f"""SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances
                        WHERE version_id IN ({version_placeholders})
                        ORDER BY version_id,warehouse_key,nm_id""",
                    tuple(version_ids),
                )
                if "sheet_vitrina_v1_warehouse_supplier_cost_states" in table_names:
                    manifest["supplier_cost_states"] = _query_manifest_rows(
                        conn,
                        f"""SELECT * FROM sheet_vitrina_v1_warehouse_supplier_cost_states
                            WHERE version_id IN ({version_placeholders})
                            ORDER BY version_id,shipment_id""",
                        tuple(version_ids),
                    )
                if "sheet_vitrina_v1_warehouse_supplier_cost_state_replays" in table_names:
                    manifest["supplier_cost_state_replays"] = _query_manifest_rows(
                        conn,
                        f"""SELECT *
                            FROM sheet_vitrina_v1_warehouse_supplier_cost_state_replays
                            WHERE version_id IN ({version_placeholders})
                            ORDER BY version_id,sequence_no""",
                        tuple(version_ids),
                    )
                if "sheet_vitrina_v1_warehouse_supplier_cost_state_corrections" in table_names:
                    manifest["supplier_cost_state_corrections"] = _query_manifest_rows(
                        conn,
                        f"""SELECT *
                            FROM sheet_vitrina_v1_warehouse_supplier_cost_state_corrections
                            WHERE version_id IN ({version_placeholders})
                            ORDER BY version_id,shipment_id,replay_id""",
                        tuple(version_ids),
                    )
                if (
                    "sheet_vitrina_v1_warehouse_supplier_cost_state_replay_rollbacks"
                    in table_names
                ):
                    manifest["supplier_cost_state_replay_rollbacks"] = _query_manifest_rows(
                        conn,
                        f"""SELECT rollback.*
                            FROM sheet_vitrina_v1_warehouse_supplier_cost_state_replay_rollbacks rollback
                            JOIN sheet_vitrina_v1_warehouse_supplier_cost_state_replays replay
                              ON replay.replay_id=rollback.replay_id
                            WHERE replay.version_id IN ({version_placeholders})
                            ORDER BY replay.version_id,replay.sequence_no""",
                        tuple(version_ids),
                    )
        if "sheet_vitrina_v1_warehouse_functional_active" in table_names:
            manifest["active_version"] = _query_manifest_rows(
                conn,
                "SELECT * FROM sheet_vitrina_v1_warehouse_functional_active ORDER BY slot",
            )
        # Current green/yellow presentation is a function of mutable supplier
        # evidence as well as frozen balances.  Include every persisted input
        # read by load_supplier_line_cost_breakdown() so optimistic recheck also
        # closes the mutation-before-replay race.
        supplier_source_queries = {
            "supplier_shipments": (
                "SELECT * FROM sheet_vitrina_v1_supplier_shipments ORDER BY shipment_id"
            ),
            "supplier_shipment_lines": (
                "SELECT * FROM sheet_vitrina_v1_supplier_shipment_lines "
                "ORDER BY shipment_id,sort_order,line_id"
            ),
            "cny_ledger_operations": (
                "SELECT * FROM sheet_vitrina_v1_cny_ledger_operations "
                "ORDER BY sequence_key,operation_id"
            ),
            "supplier_financial_documents": (
                "SELECT * FROM sheet_vitrina_v1_supplier_financial_documents "
                "ORDER BY supplier_order_id,document_date,document_id"
            ),
            "supplier_financial_expense_lines": (
                "SELECT * FROM sheet_vitrina_v1_supplier_financial_expense_lines "
                "ORDER BY supplier_order_id,financial_document_id,sort_order,line_id"
            ),
            "cny_documents": (
                "SELECT * FROM sheet_vitrina_v1_cny_documents "
                "ORDER BY source_order_id,operation_date,operation_datetime,document_id"
            ),
        }
        for key, query in supplier_source_queries.items():
            table = "sheet_vitrina_v1_" + key
            if table in table_names:
                manifest[key] = _query_manifest_rows(conn, query)
        if selected and "sheet_vitrina_v1_warehouse_wb_daily_cost" in table_names:
            placeholders = ",".join("?" for _ in selected)
            manifest["daily_cost"] = _query_manifest_rows(
                conn,
                f"""SELECT * FROM sheet_vitrina_v1_warehouse_wb_daily_cost
                    WHERE cutover_id=? AND as_of_date IN ({placeholders})
                    ORDER BY as_of_date,nm_id""",
                (FUNCTIONAL_CUTOVER_ID, *selected),
            )
        if "sheet_vitrina_v1_calculation_parameter_versions" in table_names:
            manifest["parameters"] = _query_manifest_rows(
                conn,
                """SELECT * FROM sheet_vitrina_v1_calculation_parameter_versions
                   ORDER BY block_key,effective_date,revision,created_at,version_id""",
            )
        if "sheet_vitrina_v1_proxy_v4_parameter_versions" in table_names:
            manifest["proxy_v4_parameters"] = _query_manifest_rows(
                conn,
                """SELECT * FROM sheet_vitrina_v1_proxy_v4_parameter_versions
                   ORDER BY effective_date,revision,created_at,version_id""",
            )
        return manifest
    finally:
        if own_connection:
            conn.close()


def _warehouse_input_manifest_digest_from_value(manifest: Mapping[str, Any]) -> str:
    return "sha256:" + _hash(manifest)


def _warehouse_input_component_evidence(
    manifest: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {}
    for key, value in sorted(manifest.items()):
        component = _WAREHOUSE_INPUT_COMPONENT_NAMES.get(str(key), str(key))
        evidence[component] = {
            "row_count": len(value) if isinstance(value, list) else 1,
            "digest": "sha256:" + _hash(value),
        }
    return evidence


def _query_manifest_rows(
    conn: sqlite3.Connection,
    query: str,
    parameters: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query, parameters).fetchall()]


def _material_component_drift_error(
    *,
    phase: str,
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    telemetry: Mapping[str, Any] | None = None,
) -> FunctionalEconomicsBackfillError:
    changed_components = sorted(
        key
        for key in set(expected) | set(actual)
        if expected.get(key) != actual.get(key)
    )
    if not changed_components:
        changed_components = ["material_manifest"]
    details = {
        "code": "functional_economics_material_cas_drift",
        "phase": str(phase),
        "changed_components": changed_components,
        "expected": {key: expected.get(key) for key in changed_components},
        "actual": {key: actual.get(key) for key in changed_components},
    }
    if telemetry:
        details["data_version_telemetry"] = dict(telemetry)
    return FunctionalEconomicsBackfillError(
        "functional economics material components drifted: "
        + json.dumps(
            details,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _plan_material_drift_error(
    expected_plan: Mapping[str, Any],
    actual_plan: Mapping[str, Any],
    *,
    phase: str,
) -> FunctionalEconomicsBackfillError:
    expected = dict(expected_plan.get("warehouse_input_component_evidence") or {})
    actual = dict(actual_plan.get("warehouse_input_component_evidence") or {})
    for component, field in (
        ("warehouse_input_manifest", "warehouse_input_manifest_digest"),
        ("ready_snapshot_manifest", "ready_snapshot_manifest_digest"),
        ("derived_source_fingerprint", "source_fingerprint"),
        ("ready_snapshot_non_target", "non_target_digest"),
    ):
        expected[component] = {"digest": str(expected_plan.get(field) or "")}
        actual[component] = {"digest": str(actual_plan.get(field) or "")}
    expected["ready_snapshot_target_projection"] = {
        "digest": "sha256:" + _hash(expected_plan.get("updates") or [])
    }
    actual["ready_snapshot_target_projection"] = {
        "digest": "sha256:" + _hash(actual_plan.get("updates") or [])
    }
    expected["plan_fingerprint"] = {
        "digest": str(expected_plan.get("plan_fingerprint") or "")
    }
    actual["plan_fingerprint"] = {
        "digest": str(actual_plan.get("plan_fingerprint") or "")
    }
    return _material_component_drift_error(
        phase=phase,
        expected=expected,
        actual=actual,
    )


def _ready_snapshot_manifest_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _query_manifest_rows(
        conn,
        """SELECT bundle_version,as_of_date,plan_json,refreshed_at
           FROM sheet_vitrina_v1_ready_snapshots
           ORDER BY bundle_version,as_of_date""",
    )


def _revalidate_functional_economics_material_cas(
    conn: sqlite3.Connection,
    plan: Mapping[str, Any],
    *,
    telemetry: Mapping[str, Any] | None = None,
) -> None:
    """Re-read every material input on the locked writer connection."""

    expected_components = dict(
        plan.get("warehouse_input_component_evidence") or {}
    )
    current_manifest = _warehouse_input_manifest(
        # The connection already resolves the exact operational StoreRegistry
        # generation, so no path resolution is allowed in this locked phase.
        None,
        dates=_plan_dates(plan),
        connection=conn,
    )
    actual_components = _warehouse_input_component_evidence(current_manifest)
    expected_components["warehouse_input_manifest"] = {
        "digest": str(plan.get("warehouse_input_manifest_digest") or "")
    }
    actual_components["warehouse_input_manifest"] = {
        "digest": _warehouse_input_manifest_digest_from_value(current_manifest)
    }

    current_snapshots = _ready_snapshot_manifest_rows(conn)
    expected_components["ready_snapshot_manifest"] = {
        "digest": str(plan.get("ready_snapshot_manifest_digest") or ""),
        "row_count": int(plan.get("snapshot_count") or 0),
    }
    actual_components["ready_snapshot_manifest"] = {
        "digest": _snapshot_manifest_digest(current_snapshots),
        "row_count": len(current_snapshots),
    }
    current_snapshot_by_key = {
        (str(row["bundle_version"]), str(row["as_of_date"])): row
        for row in current_snapshots
    }
    for update in plan.get("updates") or []:
        key = (str(update["bundle_version"]), str(update["as_of_date"]))
        component = f"ready_snapshot_target:{key[0]}:{key[1]}"
        current = current_snapshot_by_key.get(key)
        expected_components[component] = {
            "digest": str(update.get("before_plan_sha256") or ""),
            "row_count": 1,
        }
        actual_components[component] = {
            "digest": (
                "sha256:" + _sha(str(current["plan_json"]))
                if current is not None
                else "missing"
            ),
            "row_count": 1 if current is not None else 0,
        }

    if expected_components != actual_components:
        raise _material_component_drift_error(
            phase="bounded_writer_material_cas",
            expected=expected_components,
            actual=actual_components,
            telemetry=telemetry,
        )


def _transform_snapshot(
    snapshot: Mapping[str, Any],
    *,
    costs: Mapping[str, Mapping[int, Mapping[str, Any]]],
    warehouse_metrics: Mapping[str, Mapping[int, Mapping[str, Any]]],
    warehouse_exact_dates: set[str],
    warehouse_covered_nm_ids: Mapping[str, set[int]],
    warehouse_version_ids: Mapping[str, str],
    parameters: Mapping[str, Any],
    proxy_v4_parameters: Mapping[str, Any] | None = None,
    source_fingerprint: str,
    cutover_business_date: str,
    operation_business_date: str | None = None,
    affected_nm_ids: Iterable[int] | None = None,
    earliest_business_date: str | None = None,
    latest_business_date: str | None = None,
) -> dict[str, Any]:
    targeted = affected_nm_ids is not None
    target_nm_ids = {
        int(value) for value in (affected_nm_ids or []) if int(value) > 0
    }
    target_earliest_date = str(earliest_business_date or "")[:10]
    target_latest_date = str(latest_business_date or "")[:10]
    if targeted and (not target_nm_ids or not target_earliest_date):
        raise FunctionalEconomicsBackfillError(
            "targeted snapshot transformation requires SKU/date scope"
        )
    if target_latest_date and target_latest_date < target_earliest_date:
        raise FunctionalEconomicsBackfillError(
            "targeted snapshot latest date precedes earliest date"
        )
    original = json.loads(str(snapshot["plan_json"]))
    plan = deepcopy(original)
    sheet = _data_sheet(plan)
    rows = sheet.get("rows")
    if not isinstance(rows, list):
        raise FunctionalEconomicsBackfillError("DATA_VITRINA rows are missing")
    dates = _date_columns(plan)
    relevant_indices = [
        index
        for index, day in enumerate(dates)
        if (
            (not target_earliest_date or day >= target_earliest_date)
            and (not target_latest_date or day <= target_latest_date)
        )
    ]
    include_warehouse_rows = any(
        day >= CANONICAL_COST_POLICY_DATE.isoformat() for day in dates
    )
    include_proxy_v4_rows = any(
        day >= INVENTORY_COST_BLEND_EFFECTIVE_DATE for day in dates
    )
    active_target_keys = (
        set(TARGET_KEYS)
        if include_warehouse_rows
        else set(TARGET_KEYS) - WAREHOUSE_TARGET_KEYS
    )
    if not include_proxy_v4_rows:
        active_target_keys -= PROXY_V4_TARGET_KEYS
    before_digest = (
        _targeted_non_target_digest(
            original,
            affected_nm_ids=target_nm_ids,
            earliest_business_date=target_earliest_date,
            latest_business_date=target_latest_date,
            target_metric_keys=active_target_keys,
        )
        if targeted
        else _non_target_digest(original)
    )
    _validate_data_projection_layout(sheet, dates=dates)
    archived_rows_removed = (
        0 if targeted else _remove_archived_metric_rows(rows)
    )
    metadata_present = "metadata" in plan
    metadata = plan.get("metadata") if metadata_present else {}
    if not isinstance(metadata, dict):
        raise FunctionalEconomicsBackfillError("ready snapshot metadata must be an object")
    timestamps = metadata.get("row_last_updated_at_by_row_id")
    if isinstance(timestamps, dict):
        for row_id in list(timestamps):
            if "|" in row_id and row_id.split("|", 1)[1] in ARCHIVED_READY_METRIC_KEYS:
                timestamps.pop(row_id, None)
    if not relevant_indices:
        if plan == original:
            return {
                "after_plan_json": str(snapshot["plan_json"]),
                "changed_cells": 0,
                "inserted_rows": 0,
                "archived_rows_removed": 0,
                "presentation_changes": 0,
                "coverage_changes": 0,
                "repair_signal_changes": 0,
                "dates": [],
                "non_target_before": before_digest,
                "non_target_after": before_digest,
            }
        if archived_rows_removed:
            _update_data_dimensions(sheet)
        after = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return {
            "after_plan_json": after,
            "changed_cells": 0,
            "inserted_rows": 0,
            "archived_rows_removed": archived_rows_removed,
            "presentation_changes": 0,
            "coverage_changes": 0,
            "repair_signal_changes": 0,
            "dates": [],
            "non_target_before": before_digest,
            "non_target_after": _non_target_digest(plan),
        }
    by_id = _rows_by_id(rows)
    scopes = sorted({row_id.split("|", 1)[0] for row_id in by_id if row_id.startswith("SKU:")})
    if not scopes:
        raise FunctionalEconomicsBackfillError("ready snapshot has no SKU scopes")
    scope_nm_ids = {int(scope.split(":", 1)[1]) for scope in scopes}
    if not metadata_present:
        plan["metadata"] = metadata
    target_scopes = [
        scope
        for scope in scopes
        if int(scope.split(":", 1)[1]) in target_nm_ids
    ]
    inserted = _ensure_target_rows(
        rows,
        by_id=by_id,
        scopes=target_scopes if targeted else scopes,
        date_count=len(dates),
        include_warehouse=include_warehouse_rows,
        include_proxy_v4=include_proxy_v4_rows,
    )
    if targeted and inserted:
        raise FunctionalEconomicsBackfillError(
            "targeted economics cannot add globally missing projection rows"
        )
    by_id = _rows_by_id(rows)
    changed = 0
    presentation_changes = 0
    sku_result: dict[tuple[str, int], dict[str, Decimal | None]] = {}
    sku_v4_result: dict[tuple[str, int], dict[str, Decimal | None]] = {}
    existing_coverage = metadata.get("warehouse_history_coverage")
    existing_repair_registry = _historical_repair_registry(
        metadata.get(HISTORICAL_REPAIR_METADATA_KEY)
    )
    repair_registry = deepcopy(existing_repair_registry)
    repair_dates = repair_registry.setdefault("dates", {})
    warehouse_coverage: dict[str, dict[str, Any]] = (
        deepcopy(existing_coverage)
        if targeted and isinstance(existing_coverage, dict)
        else {}
    )
    for index in relevant_indices:
        day = dates[index]
        params = parameters[day]
        day_warehouse = warehouse_metrics.get(day, {})
        warehouse_applicable = day >= CANONICAL_COST_POLICY_DATE.isoformat()
        warehouse_known = warehouse_applicable and day in warehouse_exact_dates
        covered_nm_ids = set(warehouse_covered_nm_ids.get(day) or set())
        uncovered_scope_nm_ids = sorted(scope_nm_ids - covered_nm_ids) if warehouse_known else sorted(scope_nm_ids)
        warehouse_totals_known = warehouse_known and not uncovered_scope_nm_ids
        live_day = day == str(operation_business_date or current_business_date_iso())[:10]
        existing_day_coverage = (
            dict(existing_coverage.get(day) or {})
            if isinstance(existing_coverage, Mapping)
            else {}
        )
        closed_history_guard = _closed_history_guard_active(
            targeted=targeted,
            day=day,
            operation_business_date=str(
                operation_business_date or current_business_date_iso()
            )[:10],
            existing_coverage=existing_day_coverage,
        )
        day_repair_entry = deepcopy(
            repair_dates.get(day) if isinstance(repair_dates, Mapping) else None
        )
        if not isinstance(day_repair_entry, dict):
            day_repair_entry = {}
        day_repair_issues = [
            dict(item)
            for item in day_repair_entry.get("issues") or []
            if isinstance(item, Mapping)
        ]
        existing_functional_version_id = str(
            existing_day_coverage.get("functional_version_id") or ""
        )
        candidate_functional_version_id = str(warehouse_version_ids.get(day) or "")
        newly_closed_exact = bool(
            not targeted
            and day
            < str(operation_business_date or current_business_date_iso())[:10]
            and not closed_history_guard
            and warehouse_totals_known
            and candidate_functional_version_id
        )
        if (
            closed_history_guard
            and candidate_functional_version_id
            and candidate_functional_version_id != existing_functional_version_id
        ):
            day_repair_issues = _add_historical_repair_issue(
                day_repair_issues,
                scope="TOTAL",
                nm_id=None,
                family="warehouse_product_capital",
                component="functional_version_identity",
                reason_codes=["closed_date_functional_version_drift"],
                metric_keys=[OWN_TOTAL_CAPITAL_RUB_TOTAL_METRIC_KEY],
                last_good_preserved=_cell_has_value(
                    by_id.get(f"TOTAL|{OWN_TOTAL_CAPITAL_RUB_TOTAL_METRIC_KEY}"),
                    index=index,
                ),
            )
        if warehouse_applicable:
            warehouse_coverage[day] = {
                "status": (
                    ("live" if live_day else "closed")
                    if warehouse_totals_known
                    else ("partial" if warehouse_known else "unavailable")
                ),
                "reason_ru": (
                    (
                        "Текущие незакрытые сутки: показан live-снимок канонической бизнес-даты."
                        if live_day
                        else "Точная функциональная версия склада сохранена для закрытой бизнес-даты."
                    )
                    if warehouse_totals_known
                    else (
                        "Исторические итоги недоступны: не все SKU активной витрины входили в scope "
                        "точного складского снимка этой даты. Частичная сумма не публикуется."
                        if warehouse_known
                        else _warehouse_history_unavailable_reason(
                            day=day,
                            cutover_business_date=cutover_business_date,
                        )
                    )
                ),
                "covered_nm_id_count": len(covered_nm_ids) if warehouse_known else 0,
                "uncovered_scope_nm_ids": uncovered_scope_nm_ids,
                "functional_version_id": str(warehouse_version_ids.get(day) or ""),
            }
        for scope in scopes:
            nm_id = int(scope.split(":", 1)[1])
            mutate_sku = not targeted or nm_id in target_nm_ids
            warehouse_state = day_warehouse.get(nm_id, {})
            sku_warehouse_known = warehouse_known and nm_id in covered_nm_ids
            unavailable_reason = (
                _warehouse_history_unavailable_reason(
                    day=day,
                    cutover_business_date=cutover_business_date,
                )
                if not warehouse_known
                else (
                    "Исторические данные отсутствуют: SKU не входила в requested nmID scope "
                    "и canonical balances точного складского снимка этой даты. Нулевой остаток не предполагается."
                    if not sku_warehouse_known
                    else ""
                )
            )
            for metric_key in (
                OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS if warehouse_applicable else ()
            ):
                row = by_id.get(f"{scope}|{metric_key}")
                if row is None:
                    continue
                value = _warehouse_sku_metric_value(
                    warehouse_state,
                    metric_key=metric_key,
                    warehouse_known=sku_warehouse_known,
                )
                if not mutate_sku:
                    _assert_targeted_unrelated_cell_current(
                        row,
                        index=index,
                        value=value,
                        row_id=f"{scope}|{metric_key}",
                        day=day,
                    )
                    continue
                if closed_history_guard:
                    if not _cell_matches_value(row, index=index, value=value):
                        day_repair_issues = _add_historical_repair_issue(
                            day_repair_issues,
                            scope=scope,
                            nm_id=nm_id,
                            family="warehouse_product_capital",
                            component="closed_date_candidate",
                            reason_codes=[
                                _closed_date_drift_reason(
                                    existing_functional_version_id=str(
                                        existing_day_coverage.get(
                                            "functional_version_id"
                                        )
                                        or ""
                                    ),
                                    candidate_functional_version_id=str(
                                        warehouse_version_ids.get(day) or ""
                                    ),
                                )
                            ],
                            metric_keys=[metric_key],
                            last_good_preserved=_cell_has_value(
                                row,
                                index=index,
                            ),
                        )
                    continue
                changed += _set_cell(row, index, value)
                presentation_changes += _set_warehouse_cell_presentation(
                    metadata,
                    row_id=f"{scope}|{metric_key}",
                    day=day,
                    unavailable_reason=unavailable_reason,
                    quality_presentation=(
                        _warehouse_sku_quality_presentation(
                            warehouse_state,
                            metric_key=metric_key,
                        )
                        if sku_warehouse_known
                        else None
                    ),
                )
            cost_state = costs.get(day, {}).get(nm_id)
            cost = _optional_decimal((cost_state or {}).get("our_wb_unit_cost_rub"))
            order_sum = _cell_decimal(by_id.get(f"{scope}|orderSum"), index)
            order_count = _cell_decimal(by_id.get(f"{scope}|orderCount"), index)
            ads_sum = _cell_decimal(by_id.get(f"{scope}|ads_sum"), index)
            calculated = calculate_proxy_3(
                order_sum=order_sum,
                order_count=order_count,
                canonical_wb_wac=cost,
                ads_sum=ads_sum,
                parameters=params,
            )
            values = {
                OUR_WB_UNIT_COST_RUB_METRIC_KEY: cost,
                OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY: calculated["proxy_profit_3"],
                OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY: calculated["proxy_margin_3"],
            }
            if day >= INVENTORY_COST_BLEND_EFFECTIVE_DATE:
                calculated_v4 = calculate_proxy_4(
                    order_sum=order_sum,
                    order_count=order_count,
                    canonical_wb_wac=cost,
                    ads_sum=ads_sum,
                    parameters=(proxy_v4_parameters or {}).get(day),
                    business_date=day,
                )
                sku_v4_result[(scope, index)] = calculated_v4
                values.update(
                    {
                        PROXY_V4_PROFIT_RUB_METRIC_KEY: calculated_v4[
                            "proxy_profit_4"
                        ],
                        PROXY_V4_MARGIN_PCT_METRIC_KEY: calculated_v4[
                            "proxy_margin_4"
                        ],
                        PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY: calculated_v4[
                            "proxy_margin_per_unit"
                        ],
                    }
                )
            repair_metric_keys = _historical_repair_metric_keys(
                cost_state=cost_state,
                order_sum=order_sum,
                order_count=order_count,
                ads_sum=ads_sum,
                include_proxy_v4=(
                    day >= INVENTORY_COST_BLEND_EFFECTIVE_DATE
                    and (proxy_v4_parameters or {}).get(day) is not None
                ),
            )
            repair_reason_codes = _historical_inventory_repair_reason_codes(
                cost_state
            )
            if (closed_history_guard or newly_closed_exact) and repair_reason_codes:
                day_repair_issues = _add_historical_repair_issue(
                    day_repair_issues,
                    scope=scope,
                    nm_id=nm_id,
                    family="our_wb_cost_proxy_3_4",
                    component="inventory_cost_evidence",
                    reason_codes=repair_reason_codes,
                    metric_keys=repair_metric_keys,
                    last_good_preserved=any(
                        _cell_has_value(by_id.get(f"{scope}|{metric_key}"), index=index)
                        for metric_key in repair_metric_keys
                    ),
                )
            sku_result[(scope, index)] = calculated
            if mutate_sku:
                for metric_key, value in values.items():
                    row = by_id[f"{scope}|{metric_key}"]
                    if closed_history_guard:
                        if not _cell_matches_value(row, index=index, value=value):
                            day_repair_issues = _add_historical_repair_issue(
                                day_repair_issues,
                                scope=scope,
                                nm_id=nm_id,
                                family="our_wb_cost_proxy_3_4",
                                component="closed_date_candidate",
                                reason_codes=[
                                    _closed_date_drift_reason(
                                        existing_functional_version_id=str(
                                            existing_day_coverage.get(
                                                "functional_version_id"
                                            )
                                            or ""
                                        ),
                                        candidate_functional_version_id=str(
                                            warehouse_version_ids.get(day) or ""
                                        ),
                                    )
                                ],
                                metric_keys=[metric_key],
                                last_good_preserved=_cell_has_value(
                                    row,
                                    index=index,
                                ),
                            )
                        continue
                    changed += _set_cell(row, index, value)
            else:
                for metric_key, value in values.items():
                    _assert_targeted_unrelated_cell_current(
                        by_id[f"{scope}|{metric_key}"],
                        index=index,
                        value=value,
                        row_id=f"{scope}|{metric_key}",
                        day=day,
                    )
            if (
                day >= INVENTORY_COST_BLEND_EFFECTIVE_DATE
                and mutate_sku
                and not closed_history_guard
            ):
                presentation_changes += _set_inventory_cost_cell_presentation(
                    metadata,
                    row_id=f"{scope}|{OUR_WB_UNIT_COST_RUB_METRIC_KEY}",
                    day=day,
                    evidence=(cost_state or {}).get("inventory_cost_evidence"),
                    source_status=str(
                        (cost_state or {}).get("source_status") or ""
                    ),
                )

        eligible_proxy_3: list[dict[str, Decimal | None]] = []
        proxy_3_blocked = False
        for scope in scopes:
            item = sku_result[(scope, index)]
            if item["proxy_profit_3"] is None:
                raw_order_sum = _cell_decimal(
                    by_id.get(f"{scope}|orderSum"), index
                )
                if raw_order_sum is None or raw_order_sum > ZERO:
                    proxy_3_blocked = True
                    break
                continue
            eligible_proxy_3.append(item)
        profits = [item["proxy_profit_3"] for item in eligible_proxy_3]
        revenues = [item["expected_buyout_revenue"] for item in eligible_proxy_3]
        total_profit = (
            None
            if proxy_3_blocked or not profits
            else sum((value for value in profits if value is not None), ZERO)
        )
        total_revenue = (
            None
            if proxy_3_blocked or not revenues
            else sum((value for value in revenues if value is not None), ZERO)
        )
        total_margin = None if total_revenue in (None, ZERO) or total_profit is None else total_profit / total_revenue
        day_costs = costs.get(day, {})
        total_cost_evidence: Mapping[str, Any] | None = None
        if day >= INVENTORY_COST_BLEND_EFFECTIVE_DATE:
            total_cost_evidence = aggregate_inventory_cost_evidence(
                day_costs,
                nm_ids=sorted(scope_nm_ids),
            )
            total_cost = _optional_decimal(total_cost_evidence.get("wac_rub"))
        else:
            cost_states = list(day_costs.values())
            quantity_cost_pairs = [
                (
                    _optional_decimal((item or {}).get("stock_qty")) or ZERO,
                    _optional_decimal((item or {}).get("our_wb_unit_cost_rub")),
                )
                for item in cost_states
            ]
            total_qty = sum((quantity for quantity, _ in quantity_cost_pairs), ZERO)
            missing_visible_cost_row = any(
                nm_id not in day_costs for nm_id in scope_nm_ids
            )
            missing_positive_cost = any(
                quantity > ZERO and cost is None
                for quantity, cost in quantity_cost_pairs
            )
            total_capital = sum(
                (
                    quantity * cost
                    for quantity, cost in quantity_cost_pairs
                    if cost is not None
                ),
                ZERO,
            )
            total_cost = (
                total_capital / total_qty
                if total_qty > ZERO
                and not missing_visible_cost_row
                and not missing_positive_cost
                else None
            )
        total_values = {
            TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY: total_cost,
            OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY: total_profit,
            OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY: total_margin,
        }
        if day >= INVENTORY_COST_BLEND_EFFECTIVE_DATE:
            v4_rows = [sku_v4_result[(scope, index)] for scope in scopes]
            v4_blocked = False
            for scope, item in zip(scopes, v4_rows):
                if item["proxy_profit_4"] is not None:
                    continue
                raw_order_sum = _cell_decimal(
                    by_id.get(f"{scope}|orderSum"), index
                )
                if raw_order_sum is None or raw_order_sum > ZERO:
                    v4_blocked = True
                    break
            v4_aggregate = (
                {
                    "proxy_profit_4": None,
                    "proxy_margin_4": None,
                    "proxy_margin_per_unit": None,
                }
                if v4_blocked
                else aggregate_proxy_4(v4_rows)
            )
            total_values.update(
                {
                    PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY: v4_aggregate[
                        "proxy_profit_4"
                    ],
                    PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY: v4_aggregate[
                        "proxy_margin_4"
                    ],
                    PROXY_V4_TOTAL_MARGIN_PER_UNIT_RUB_METRIC_KEY: v4_aggregate[
                        "proxy_margin_per_unit"
                    ],
                }
            )
        for metric_key, value in total_values.items():
            row = by_id[f"TOTAL|{metric_key}"]
            if closed_history_guard:
                if not _cell_matches_value(row, index=index, value=value):
                    day_repair_issues = _add_historical_repair_issue(
                        day_repair_issues,
                        scope="TOTAL",
                        nm_id=None,
                        family="our_wb_cost_proxy_3_4",
                        component="closed_date_aggregate_candidate",
                        reason_codes=["closed_date_candidate_drift"],
                        metric_keys=[metric_key],
                        last_good_preserved=_cell_has_value(row, index=index),
                    )
                continue
            changed += _set_cell(row, index, value)
        if total_cost_evidence is not None and not closed_history_guard:
            presentation_changes += _set_inventory_cost_cell_presentation(
                metadata,
                row_id=f"TOTAL|{TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY}",
                day=day,
                evidence=total_cost_evidence,
                source_status=(
                    "blended_inventory_wac_provisional"
                    if any(
                        str(
                            (day_costs.get(nm_id) or {}).get("source_status")
                            or ""
                        )
                        == "blended_inventory_wac_provisional"
                        for nm_id in scope_nm_ids
                    )
                    else "blended_inventory_wac_confirmed"
                ),
            )
        if warehouse_applicable:
            visible_warehouse_states = {
                nm_id: state
                for nm_id, state in day_warehouse.items()
                if nm_id in scope_nm_ids
            }
            warehouse_total_values = _warehouse_total_metric_values(
                visible_warehouse_states,
                warehouse_known=warehouse_totals_known,
            )
            totals_unavailable_reason = (
                ""
                if warehouse_totals_known
                else (
                    "Исторические итоги недоступны: не все SKU активной витрины входили в scope "
                    "точного складского снимка этой даты. Частичная сумма не публикуется."
                    if warehouse_known
                    else _warehouse_history_unavailable_reason(
                        day=day,
                        cutover_business_date=cutover_business_date,
                    )
                )
            )
            for metric_key, value in warehouse_total_values.items():
                row = by_id.get(f"TOTAL|{metric_key}")
                if row is not None:
                    if closed_history_guard:
                        if not _cell_matches_value(row, index=index, value=value):
                            day_repair_issues = _add_historical_repair_issue(
                                day_repair_issues,
                                scope="TOTAL",
                                nm_id=None,
                                family="warehouse_product_capital",
                                component="closed_date_aggregate_candidate",
                                reason_codes=[
                                    _closed_date_drift_reason(
                                        existing_functional_version_id=str(
                                            existing_day_coverage.get(
                                                "functional_version_id"
                                            )
                                            or ""
                                        ),
                                        candidate_functional_version_id=str(
                                            warehouse_version_ids.get(day) or ""
                                        ),
                                    )
                                ],
                                metric_keys=[metric_key],
                                last_good_preserved=_cell_has_value(
                                    row,
                                    index=index,
                                ),
                            )
                        continue
                    changed += _set_cell(row, index, value)
                    presentation_changes += _set_warehouse_cell_presentation(
                        metadata,
                        row_id=f"TOTAL|{metric_key}",
                        day=day,
                        unavailable_reason=totals_unavailable_reason,
                        quality_presentation=(
                            _warehouse_total_quality_presentation(
                                visible_warehouse_states,
                                metric_key=metric_key,
                            )
                            if warehouse_totals_known
                            else None
                        ),
                    )
        if (closed_history_guard or newly_closed_exact) and day_repair_issues:
            stable_functional_version_id = str(
                existing_day_coverage.get("functional_version_id")
                or warehouse_version_ids.get(day)
                or ""
            )
            ordinary_publication_applied = bool(
                newly_closed_exact
                or day_repair_entry.get("ordinary_publication_applied")
            )
            day_repair_entry = {
                "status": "historical_repair_required",
                "business_date": day,
                "functional_version_id": stable_functional_version_id,
                "issues": _sorted_historical_repair_issues(day_repair_issues),
                "repair_contract": "version_bound_historical_reconciliation",
                "ordinary_publication_applied": ordinary_publication_applied,
            }
            repair_dates[day] = day_repair_entry
            warehouse_coverage[day] = {
                **dict(warehouse_coverage.get(day) or existing_day_coverage),
                "status": "historical_repair_required",
                "reason_ru": (
                    "Точная складская версия закрытой даты опубликована вместе "
                    "с типизированным сигналом предварительной себестоимости. "
                    "Требуется отдельная историческая сверка."
                    if ordinary_publication_applied
                    else (
                        "Закрытая дата сохранена без перепубликации: новая складская "
                        "проекция не согласована с её точной функциональной версией. "
                        "Требуется отдельная историческая сверка."
                    )
                ),
                "functional_version_id": stable_functional_version_id,
                "historical_repair_required": True,
                "repair_issue_count": len(day_repair_entry["issues"]),
            }
            repair_reasons_by_scope: dict[str, set[str]] = {}
            for issue in day_repair_entry["issues"]:
                if str(issue.get("family") or "") == "our_wb_cost_proxy_3_4":
                    repair_reasons_by_scope.setdefault(
                        str(issue.get("scope") or ""), set()
                    ).update(
                        str(reason)
                        for reason in issue.get("reason_codes") or []
                        if str(reason or "")
                    )
            for scope, scope_reasons in sorted(repair_reasons_by_scope.items()):
                cost_key = (
                    TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY
                    if scope == "TOTAL"
                    else OUR_WB_UNIT_COST_RUB_METRIC_KEY
                )
                row_id = f"{scope}|{cost_key}"
                if row_id in by_id:
                    presentation_changes += _set_historical_repair_cell_presentation(
                        metadata,
                        row_id=row_id,
                        day=day,
                        functional_version_id=stable_functional_version_id,
                        reason_codes=sorted(scope_reasons),
                    )
    marker = {
        "cutover_id": FUNCTIONAL_CUTOVER_ID,
        "source_fingerprint": source_fingerprint,
        "date_from": dates[relevant_indices[0]],
        "date_to": dates[relevant_indices[-1]],
        "target_metric_keys": sorted(active_target_keys),
        "archived_metric_keys": sorted(ARCHIVED_READY_METRIC_KEYS),
        "inventory_cost_publication": {
            "formula_version": INVENTORY_COST_BLEND_FORMULA_VERSION,
            "effective_date": INVENTORY_COST_BLEND_EFFECTIVE_DATE,
            "consumer_metric_keys": sorted(
                {
                    OUR_WB_UNIT_COST_RUB_METRIC_KEY,
                    TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY,
                    OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
                    OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
                    *PROXY_V4_TARGET_KEYS,
                }
            ),
            "date_evidence": {
                dates[index]: aggregate_inventory_cost_evidence(
                    costs.get(dates[index], {}),
                    nm_ids=sorted(scope_nm_ids),
                )
                for index in relevant_indices
                if dates[index] >= INVENTORY_COST_BLEND_EFFECTIVE_DATE
            },
        },
    }
    marker_key = (
        "functional_economics_targeted_replay"
        if targeted
        else "functional_economics_backfill"
    )
    if targeted:
        marker["affected_nm_ids"] = sorted(target_nm_ids)
        marker["earliest_business_date"] = target_earliest_date
        if target_latest_date:
            marker["latest_business_date"] = target_latest_date
    if metadata.get(marker_key) != marker:
        metadata[marker_key] = marker
    final_repair_registry: dict[str, Any] | None = None
    if repair_dates:
        final_repair_registry = {
            "contract_name": HISTORICAL_REPAIR_CONTRACT,
            "status": "historical_repair_required",
            "dates": {
                str(day): deepcopy(repair_dates[day])
                for day in sorted(repair_dates)
            },
        }
        metadata[HISTORICAL_REPAIR_METADATA_KEY] = final_repair_registry
    else:
        metadata.pop(HISTORICAL_REPAIR_METADATA_KEY, None)
    repair_signal_changes = int(
        _historical_repair_registry_or_none(
            original.get("metadata", {}).get(HISTORICAL_REPAIR_METADATA_KEY)
            if isinstance(original.get("metadata"), Mapping)
            else None
        )
        != final_repair_registry
    )
    coverage_changes = int(metadata.get("warehouse_history_coverage") != warehouse_coverage)
    metadata["warehouse_history_coverage"] = warehouse_coverage
    timestamps = metadata.setdefault("row_last_updated_at_by_row_id", {})
    if isinstance(timestamps, dict):
        for row_id in by_id:
            scope, separator, metric_key = row_id.partition("|")
            allowed_target_row = (
                not targeted
                or scope == "TOTAL"
                or (
                    scope.startswith("SKU:")
                    and int(scope.split(":", 1)[1]) in target_nm_ids
                )
            )
            if (
                separator
                and metric_key in active_target_keys
                and allowed_target_row
            ):
                timestamps[row_id] = str(snapshot.get("refreshed_at") or "")
    if inserted or archived_rows_removed:
        _update_data_dimensions(sheet)
    from packages.application.web_vitrina_management_history import preserve_applied_estimates
    preserve_applied_estimates(plan, original=original)
    after = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    after_digest = (
        _targeted_non_target_digest(
            plan,
            affected_nm_ids=target_nm_ids,
            earliest_business_date=target_earliest_date,
            latest_business_date=target_latest_date,
            target_metric_keys=active_target_keys,
        )
        if targeted
        else _non_target_digest(plan)
    )
    return {
        "after_plan_json": after,
        "changed_cells": changed,
        "inserted_rows": inserted,
        "archived_rows_removed": archived_rows_removed,
        "presentation_changes": presentation_changes,
        "coverage_changes": coverage_changes,
        "repair_signal_changes": repair_signal_changes,
        "dates": [dates[index] for index in relevant_indices],
        "non_target_before": before_digest,
        "non_target_after": after_digest,
    }


def _historical_repair_registry(raw: Any) -> dict[str, Any]:
    normalized = _historical_repair_registry_or_none(raw)
    if normalized is not None:
        return normalized
    return {
        "contract_name": HISTORICAL_REPAIR_CONTRACT,
        "status": "historical_repair_required",
        "dates": {},
    }


def _historical_repair_registry_or_none(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise FunctionalEconomicsBackfillError(
            "historical repair registry must be an object"
        )
    contract_name = str(raw.get("contract_name") or "")
    if contract_name != HISTORICAL_REPAIR_CONTRACT:
        raise FunctionalEconomicsBackfillError(
            "historical repair registry contract is unsupported"
        )
    dates = raw.get("dates")
    if not isinstance(dates, Mapping):
        raise FunctionalEconomicsBackfillError(
            "historical repair registry dates must be an object"
        )
    normalized_dates: dict[str, dict[str, Any]] = {}
    for day, entry in sorted(dates.items()):
        day_key = str(day)[:10]
        try:
            date.fromisoformat(day_key)
        except ValueError as exc:
            raise FunctionalEconomicsBackfillError(
                "historical repair registry date is invalid"
            ) from exc
        if not isinstance(entry, Mapping):
            raise FunctionalEconomicsBackfillError(
                "historical repair registry date entry must be an object"
            )
        normalized_dates[day_key] = deepcopy(dict(entry))
    if not normalized_dates:
        return None
    return {
        "contract_name": HISTORICAL_REPAIR_CONTRACT,
        "status": "historical_repair_required",
        "dates": normalized_dates,
    }


def _closed_history_guard_active(
    *,
    targeted: bool,
    day: str,
    operation_business_date: str,
    existing_coverage: Mapping[str, Any],
) -> bool:
    if targeted or day >= operation_business_date:
        return False
    if str(existing_coverage.get("status") or "") not in {
        "live",
        "closed",
        "historical_repair_required",
    }:
        return False
    return bool(str(existing_coverage.get("functional_version_id") or ""))


def _closed_date_drift_reason(
    *,
    existing_functional_version_id: str,
    candidate_functional_version_id: str,
) -> str:
    if (
        existing_functional_version_id
        and candidate_functional_version_id
        and existing_functional_version_id != candidate_functional_version_id
    ):
        return "closed_date_functional_version_drift"
    return "closed_date_candidate_drift"


def _historical_inventory_repair_reason_codes(
    cost_state: Mapping[str, Any] | None,
) -> list[str]:
    evidence = (
        cost_state.get("inventory_cost_evidence")
        if isinstance(cost_state, Mapping)
        else None
    )
    if not isinstance(evidence, Mapping):
        return []
    quantity = _optional_decimal(evidence.get("quantity")) or ZERO
    if quantity <= ZERO or str(evidence.get("status") or "") == "resolved":
        return []
    reasons = {
        str(reason)
        for reason in evidence.get("reason_codes") or [evidence.get("reason")]
        if str(reason or "") and str(reason or "") != "no_physical_inventory"
    }
    return sorted(reasons)


def _historical_repair_metric_keys(
    *,
    cost_state: Mapping[str, Any] | None,
    order_sum: Decimal | None,
    order_count: Decimal | None,
    ads_sum: Decimal | None,
    include_proxy_v4: bool,
) -> list[str]:
    del ads_sum
    evidence = (
        cost_state.get("inventory_cost_evidence")
        if isinstance(cost_state, Mapping)
        else None
    )
    quantity = (
        _optional_decimal(evidence.get("quantity"))
        if isinstance(evidence, Mapping)
        else None
    ) or ZERO
    if quantity <= ZERO:
        return []
    result = [OUR_WB_UNIT_COST_RUB_METRIC_KEY]
    if order_sum is not None and order_sum > ZERO:
        result.extend(
            [
                OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
                OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
            ]
        )
        if include_proxy_v4:
            result.extend(
                [
                    PROXY_V4_PROFIT_RUB_METRIC_KEY,
                    PROXY_V4_MARGIN_PCT_METRIC_KEY,
                ]
            )
    if include_proxy_v4 and order_count is not None and order_count > ZERO:
        result.append(PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY)
    return sorted(set(result))


def _cell_matches_value(
    row: list[Any] | None,
    *,
    index: int,
    value: Decimal | None,
) -> bool:
    current = row[2 + index] if row is not None and len(row) > 2 + index else ""
    expected: Any = "" if value is None else float(value)
    return _same_cell(current, expected)


def _cell_has_value(row: list[Any] | None, *, index: int) -> bool:
    return bool(
        row is not None
        and len(row) > 2 + index
        and row[2 + index] not in (None, "")
    )


def _add_historical_repair_issue(
    issues: list[dict[str, Any]],
    *,
    scope: str,
    nm_id: int | None,
    family: str,
    component: str,
    reason_codes: Iterable[str],
    metric_keys: Iterable[str],
    last_good_preserved: bool,
) -> list[dict[str, Any]]:
    reasons = sorted({str(value) for value in reason_codes if str(value or "")})
    metrics = sorted({str(value) for value in metric_keys if str(value or "")})
    if not reasons or not metrics:
        return issues
    key = (str(scope), str(family), str(component))
    for item in issues:
        if (
            str(item.get("scope") or ""),
            str(item.get("family") or ""),
            str(item.get("component") or ""),
        ) != key:
            continue
        item["reason_codes"] = sorted(
            {str(value) for value in item.get("reason_codes") or []} | set(reasons)
        )
        item["metric_keys"] = sorted(
            {str(value) for value in item.get("metric_keys") or []} | set(metrics)
        )
        item["last_good_preserved"] = bool(
            item.get("last_good_preserved") or last_good_preserved
        )
        return issues
    issues.append(
        {
            "scope": str(scope),
            "nm_id": int(nm_id) if nm_id is not None else None,
            "family": str(family),
            "component": str(component),
            "reason_codes": reasons,
            "metric_keys": metrics,
            "last_good_preserved": bool(last_good_preserved),
        }
    )
    return issues


def _sorted_historical_repair_issues(
    issues: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return sorted(
        [deepcopy(dict(item)) for item in issues],
        key=lambda item: (
            str(item.get("scope") or ""),
            str(item.get("family") or ""),
            str(item.get("component") or ""),
            ",".join(str(value) for value in item.get("metric_keys") or []),
        ),
    )


def _warehouse_history_unavailable_reason(
    *,
    day: str,
    cutover_business_date: str,
) -> str:
    if cutover_business_date and day < cutover_business_date:
        return (
            "Исторические данные отсутствуют: до функционального cutover не сохранялся "
            "полный согласованный шестиступенчатый складской снимок. Текущий snapshot назад не копируется."
        )
    return (
        "Исторические данные отсутствуют: для этой даты нет точной успешной "
        "функциональной версии склада. Last-good или snapshot другой даты сюда не переносится."
    )


def _set_warehouse_cell_presentation(
    metadata: dict[str, Any],
    *,
    row_id: str,
    day: str,
    unavailable_reason: str,
    quality_presentation: Mapping[str, str] | None = None,
) -> int:
    """Publish fail-closed history state through the contract consumed by Web UI."""

    raw = metadata.get("server_cell_presentation")
    if raw is None:
        raw = {}
        metadata["server_cell_presentation"] = raw
    if not isinstance(raw, dict):
        raise FunctionalEconomicsBackfillError(
            "ready snapshot server_cell_presentation must be an object"
        )
    by_date = raw.get(row_id)
    if by_date is None:
        by_date = {}
        raw[row_id] = by_date
    if not isinstance(by_date, dict):
        raise FunctionalEconomicsBackfillError(
            f"ready snapshot presentation for {row_id} must be an object"
        )
    if unavailable_reason:
        expected = {
            "state": "unavailable",
            "tone": "neutral",
            "reason": unavailable_reason,
            "source": "WebCore",
        }
        if by_date.get(day) == expected:
            return 0
        by_date[day] = expected
        return 1
    if quality_presentation:
        expected = dict(quality_presentation)
        if by_date.get(day) == expected:
            return 0
        by_date[day] = expected
        return 1
    current = by_date.get(day)
    if (
        isinstance(current, Mapping)
        and str(current.get("source") or "") == "WebCore"
        and str(current.get("state") or "") in {"unavailable", "unconfirmed"}
    ):
        by_date.pop(day, None)
        if not by_date:
            raw.pop(row_id, None)
        if not raw:
            metadata.pop("server_cell_presentation", None)
        return 1
    if not by_date:
        raw.pop(row_id, None)
    if not raw:
        metadata.pop("server_cell_presentation", None)
    return 0


def _set_inventory_cost_cell_presentation(
    metadata: dict[str, Any],
    *,
    row_id: str,
    day: str,
    evidence: Any,
    source_status: str,
) -> int:
    """Publish exact WB+FF source/version evidence for one visible cost cell."""

    resolved = (
        isinstance(evidence, Mapping)
        and str(evidence.get("status") or "") == "resolved"
    )
    provisional = source_status == "blended_inventory_wac_provisional"
    expected = {
        "state": (
            "unconfirmed"
            if resolved and provisional
            else ("confirmed" if resolved else "unavailable")
        ),
        "tone": (
            "yellow"
            if resolved and provisional
            else ("green" if resolved else "neutral")
        ),
        "reason": inventory_cost_evidence_reason(
            evidence if isinstance(evidence, Mapping) else {}
        ),
        "source": "WebCore · WB+FF",
    }
    raw = metadata.setdefault("server_cell_presentation", {})
    if not isinstance(raw, dict):
        raise FunctionalEconomicsBackfillError(
            "ready snapshot server_cell_presentation must be an object"
        )
    by_date = raw.setdefault(row_id, {})
    if not isinstance(by_date, dict):
        raise FunctionalEconomicsBackfillError(
            f"ready snapshot presentation for {row_id} must be an object"
        )
    if by_date.get(day) == expected:
        return 0
    by_date[day] = expected
    return 1


def _set_historical_repair_cell_presentation(
    metadata: dict[str, Any],
    *,
    row_id: str,
    day: str,
    functional_version_id: str,
    reason_codes: Iterable[str],
) -> int:
    """Keep last-good value visible while disclosing the frozen history state."""

    reasons = sorted({str(reason) for reason in reason_codes if str(reason or "")})
    expected = {
        "state": "historical_repair_required",
        "tone": "yellow",
        "reason": (
            "Закрытая дата сохранена без перепубликации: новая складская "
            "проекция не согласована с точной функциональной версией. "
            "Требуется отдельная историческая сверка."
        ),
        "source": "WebCore · WB+FF",
        "functional_version_id": str(functional_version_id),
        "reason_codes": reasons,
    }
    raw = metadata.setdefault("server_cell_presentation", {})
    if not isinstance(raw, dict):
        raise FunctionalEconomicsBackfillError(
            "ready snapshot server_cell_presentation must be an object"
        )
    by_date = raw.setdefault(row_id, {})
    if not isinstance(by_date, dict):
        raise FunctionalEconomicsBackfillError(
            f"ready snapshot presentation for {row_id} must be an object"
        )
    if by_date.get(day) == expected:
        return 0
    by_date[day] = expected
    return 1


def _warehouse_sku_quality_presentation(
    state: Mapping[str, Any],
    *,
    metric_key: str,
) -> dict[str, str] | None:
    """Project exact-date provisional quality for one canonical warehouse cell."""

    quality_code = ""
    for stage in OWN_PRODUCT_CAPITAL_STAGES:
        if metric_key not in {
            own_stage_metric_key(stage, field)
            for field in ("qty", "unit_cost_rub", "capital_rub")
        }:
            continue
        stage_state = (state.get("stage_presentation") or {}).get(stage, {})
        if str(stage_state.get("state") or "") != "unconfirmed":
            return None
        quality_code = str(stage_state.get("reason") or "provisional")
        break
    else:
        if str(state.get("presentation_state") or "") != "unconfirmed":
            return None
        quality_code = str(state.get("presentation_reason") or "provisional")
    return _unconfirmed_quality_presentation(quality_code)


def _warehouse_total_quality_presentation(
    states: Mapping[int, Mapping[str, Any]],
    *,
    metric_key: str,
) -> dict[str, str] | None:
    """Project yellow status only from the exact states contributing to TOTAL."""

    reasons: list[str] = []
    for state in states.values():
        sku_metric_key = ""
        for stage in OWN_PRODUCT_CAPITAL_STAGES:
            for field in ("qty", "unit_cost_rub", "capital_rub"):
                if metric_key == own_stage_total_metric_key(stage, field):
                    sku_metric_key = own_stage_metric_key(stage, field)
                    break
            if sku_metric_key:
                break
        presentation = (
            _warehouse_sku_quality_presentation(state, metric_key=sku_metric_key)
            if sku_metric_key
            else (
                _unconfirmed_quality_presentation(
                    str(state.get("presentation_reason") or "provisional")
                )
                if str(state.get("presentation_state") or "") == "unconfirmed"
                else None
            )
        )
        if presentation:
            reasons.append(str(presentation["reason"]))
    if not reasons:
        return None
    return {
        "state": "unconfirmed",
        "tone": "yellow",
        "reason": "; ".join(sorted(set(reasons))),
        "source": "WebCore",
    }


def _unconfirmed_quality_presentation(value: str) -> dict[str, str]:
    codes = [item.strip() for item in str(value or "").split(";") if item.strip()]
    presentations = [
        _warehouse_balance_status_presentation(code, certified=False)
        for code in (codes or ["provisional"])
    ]
    return {
        "state": "unconfirmed",
        "tone": "yellow",
        "reason": "; ".join(
            f"{item['label_ru']}. {item['description_ru']}" for item in presentations
        ),
        "source": "WebCore",
    }


def _exact_functional_snapshot_context(
    runtime: RegistryUploadDbBackedRuntime,
    dates: list[str],
) -> dict[str, dict[str, Any]]:
    """Return the exact functional version and SKU scope for each business date."""

    selected = sorted({str(day or "")[:10] for day in dates if str(day or "")[:10]})
    if not selected:
        return {}
    placeholders = ",".join("?" for _ in selected)
    with _connect(runtime.db_path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required = {
            "sheet_vitrina_v1_warehouse_functional_versions",
            "sheet_vitrina_v1_warehouse_wb_snapshots",
        }
        if not required.issubset(tables):
            return {}
        rows = conn.execute(
            f"""SELECT snapshot.snapshot_date,snapshot.requested_nm_ids_json,
                       snapshot.items_json,version.version_id,version.effective_at,
                       version.created_at
                FROM sheet_vitrina_v1_warehouse_functional_versions version
                JOIN sheet_vitrina_v1_warehouse_wb_snapshots snapshot
                  ON snapshot.version_id=version.version_id
                WHERE version.cutover_id=? AND version.status='good'
                  AND snapshot.snapshot_date IN ({placeholders})
                ORDER BY snapshot.snapshot_date,version.created_at DESC,version.version_id DESC""",
            (FUNCTIONAL_CUTOVER_ID, *selected),
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        version_by_day: dict[str, str] = {}
        for row in rows:
            day = str(row["snapshot_date"])
            if day in result or business_date_from_timestamp(str(row["effective_at"])) != day:
                continue
            covered = {
                nm_id
                for value in _json_list(row["requested_nm_ids_json"])
                if (nm_id := _positive_nm_id(value)) is not None
            }
            for item in _json_list(row["items_json"]):
                if not isinstance(item, Mapping):
                    continue
                nm_id = _positive_nm_id(item.get("nm_id") or item.get("nmId"))
                if nm_id is not None:
                    covered.add(nm_id)
            result[day] = {
                "version_id": str(row["version_id"]),
                "covered_nm_ids": covered,
            }
            version_by_day[day] = str(row["version_id"])
        if version_by_day:
            version_ids = sorted(set(version_by_day.values()))
            version_placeholders = ",".join("?" for _ in version_ids)
            balances = conn.execute(
                f"""SELECT version_id,nm_id
                    FROM sheet_vitrina_v1_warehouse_functional_balances
                    WHERE version_id IN ({version_placeholders})""",
                tuple(version_ids),
            ).fetchall()
            day_by_version = {version_id: day for day, version_id in version_by_day.items()}
            for row in balances:
                day = day_by_version.get(str(row["version_id"]))
                nm_id = _positive_nm_id(row["nm_id"])
                if day is not None and nm_id is not None:
                    result[day]["covered_nm_ids"].add(nm_id)
    return result


def _exact_functional_snapshot_coverage(
    runtime: RegistryUploadDbBackedRuntime,
    dates: list[str],
) -> dict[str, set[int]]:
    """Compatibility projection of the exact functional SKU scope."""

    return {
        day: set(item["covered_nm_ids"])
        for day, item in _exact_functional_snapshot_context(runtime, dates).items()
    }


def _exact_functional_snapshot_dates(
    runtime: RegistryUploadDbBackedRuntime,
    dates: list[str],
) -> set[str]:
    """Compatibility projection of exact business dates for diagnostics/tests."""

    return set(_exact_functional_snapshot_coverage(runtime, dates))


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _positive_nm_id(value: Any) -> int | None:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _warehouse_sku_metric_value(
    state: Mapping[str, Any],
    *,
    metric_key: str,
    warehouse_known: bool,
) -> Decimal | None:
    if not warehouse_known:
        return None
    if metric_key in state:
        return _optional_decimal(state.get(metric_key))
    zero_keys = {
        own_stage_metric_key(stage, field)
        for stage in OWN_PRODUCT_CAPITAL_STAGES
        for field in ("qty", "capital_rub")
    } | {OWN_TOTAL_QTY_METRIC_KEY, OWN_TOTAL_CAPITAL_RUB_METRIC_KEY}
    return ZERO if metric_key in zero_keys else None


def _warehouse_total_metric_values(
    states: Mapping[int, Mapping[str, Any]],
    *,
    warehouse_known: bool,
) -> dict[str, Decimal | None]:
    result: dict[str, Decimal | None] = {}
    if not warehouse_known:
        for key in OWN_PRODUCT_CAPITAL_TOTAL_METRIC_KEYS:
            result[key] = None
        return result
    total_quantity = ZERO
    total_capital = ZERO
    for stage in OWN_PRODUCT_CAPITAL_STAGES:
        qty_key = own_stage_metric_key(stage, "qty")
        capital_key = own_stage_metric_key(stage, "capital_rub")
        quantity = sum(
            (_optional_decimal(item.get(qty_key)) or ZERO for item in states.values()),
            ZERO,
        )
        capital = sum(
            (_optional_decimal(item.get(capital_key)) or ZERO for item in states.values()),
            ZERO,
        )
        total_quantity += quantity
        total_capital += capital
        result[own_stage_total_metric_key(stage, "qty")] = quantity
        result[own_stage_total_metric_key(stage, "capital_rub")] = capital
        result[own_stage_total_metric_key(stage, "unit_cost_rub")] = (
            capital / quantity if quantity > ZERO else None
        )
    result[OWN_TOTAL_QTY_TOTAL_METRIC_KEY] = total_quantity
    result[OWN_TOTAL_CAPITAL_RUB_TOTAL_METRIC_KEY] = total_capital
    result[OWN_AVG_COST_RUB_TOTAL_METRIC_KEY] = (
        total_capital / total_quantity if total_quantity > ZERO else None
    )
    return result


def _ensure_target_rows(
    rows: list[Any],
    *,
    by_id: Mapping[str, list[Any]],
    scopes: list[str],
    date_count: int,
    include_warehouse: bool,
    include_proxy_v4: bool,
) -> int:
    specs = [
        ("TOTAL", TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY, OUR_WB_UNIT_COST_RUB_LABEL),
        ("TOTAL", OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY, OUR_WB_PROXY_PROFIT_3_RUB_LABEL),
        ("TOTAL", OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY, OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_LABEL),
    ]
    if include_proxy_v4:
        specs.extend(
            [
                (
                    "TOTAL",
                    PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY,
                    PROXY_V4_PROFIT_LABEL_RU,
                ),
                (
                    "TOTAL",
                    PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY,
                    PROXY_V4_MARGIN_LABEL_RU,
                ),
                (
                    "TOTAL",
                    PROXY_V4_TOTAL_MARGIN_PER_UNIT_RUB_METRIC_KEY,
                    PROXY_V4_MARGIN_PER_UNIT_LABEL_RU,
                ),
            ]
        )
    warehouse_catalog = {
        (item.scope, item.metric_key): item.label_ru
        for item in build_own_product_capital_metric_items()
        if item.metric_key in WAREHOUSE_TARGET_KEYS
    }
    if include_warehouse:
        specs.extend(
            ("TOTAL", metric_key, label)
            for (scope, metric_key), label in warehouse_catalog.items()
            if scope == "TOTAL"
        )
    for scope in scopes:
        prefix = _scope_label_prefix(by_id, scope)
        specs.extend(
            [
                (scope, OUR_WB_UNIT_COST_RUB_METRIC_KEY, f"{prefix}: {OUR_WB_UNIT_COST_RUB_LABEL}"),
                (scope, OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY, f"{prefix}: {OUR_WB_PROXY_PROFIT_3_RUB_LABEL}"),
                (scope, OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY, f"{prefix}: {OUR_WB_PROXY_MARGIN_3_PCT_LABEL}"),
            ]
        )
        if include_proxy_v4:
            specs.extend(
                [
                    (
                        scope,
                        PROXY_V4_PROFIT_RUB_METRIC_KEY,
                        f"{prefix}: {PROXY_V4_PROFIT_LABEL_RU}",
                    ),
                    (
                        scope,
                        PROXY_V4_MARGIN_PCT_METRIC_KEY,
                        f"{prefix}: {PROXY_V4_MARGIN_LABEL_RU}",
                    ),
                    (
                        scope,
                        PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY,
                        f"{prefix}: {PROXY_V4_MARGIN_PER_UNIT_LABEL_RU}",
                    ),
                ]
            )
        if include_warehouse:
            specs.extend(
                (scope, metric_key, f"{prefix}: {label}")
                for (metric_scope, metric_key), label in warehouse_catalog.items()
                if metric_scope == "SKU"
            )
    inserted = 0
    for scope, metric, label in specs:
        row_id = f"{scope}|{metric}"
        if row_id in by_id:
            continue
        rows.append([label, row_id, *([""] * date_count)])
        inserted += 1
    return inserted


def _remove_archived_metric_rows(rows: list[Any]) -> int:
    retained = []
    removed = 0
    for row in rows:
        row_id = str(row[1] or "") if isinstance(row, list) and len(row) > 1 else ""
        metric_key = row_id.split("|", 1)[1] if "|" in row_id else ""
        if metric_key in ARCHIVED_READY_METRIC_KEYS:
            removed += 1
            continue
        retained.append(row)
    if removed:
        rows[:] = retained
    return removed


def _scope_label_prefix(by_id: Mapping[str, list[Any]], scope: str) -> str:
    for suffix in ("orderSum", "proxy_profit_2_rub", "proxy_profit_rub"):
        row = by_id.get(f"{scope}|{suffix}")
        if row:
            label = str(row[0] or scope)
            return label.split(": ", 1)[0]
    return scope


def _set_cell(row: list[Any], index: int, value: Decimal | None) -> int:
    target_index = 2 + index
    while len(row) <= target_index:
        row.append("")
    normalized: Any = "" if value is None else float(value)
    current = row[target_index]
    if _same_cell(current, normalized):
        return 0
    row[target_index] = normalized
    return 1


def _same_cell(current: Any, expected: Any) -> bool:
    if current in (None, "") or expected in (None, ""):
        return current in (None, "") and expected in (None, "")
    try:
        return abs(Decimal(str(current).replace(",", ".")) - Decimal(str(expected))) <= Decimal("0.0000005")
    except (InvalidOperation, ValueError):
        return False


def _assert_targeted_unrelated_cell_current(
    row: list[Any],
    *,
    index: int,
    value: Decimal | None,
    row_id: str,
    day: str,
) -> None:
    target_index = 2 + index
    current = row[target_index] if len(row) > target_index else ""
    expected: Any = "" if value is None else float(value)
    if not _same_cell(current, expected):
        raise FunctionalEconomicsBackfillError(
            "targeted economics found unrelated stale consumer cell: "
            f"{row_id}:{day}"
        )


def _cell_decimal(row: list[Any] | None, index: int) -> Decimal | None:
    if row is None or len(row) <= 2 + index or row[2 + index] in (None, ""):
        return None
    return _optional_decimal(row[2 + index])


def _optional_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _rows_by_id(rows: list[Any]) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 2 or not isinstance(row[1], str):
            continue
        row_id = row[1].strip()
        # Historical ready snapshots can retain presentation-only rows whose
        # second cell is a value rather than a stable projection key.  Public
        # vitrina reads already ignore those rows.  Preserve them byte-for-byte
        # and index only the same stable ``scope|metric`` contract here.
        if not _is_projection_row_id(row_id):
            continue
        if row_id in result:
            raise FunctionalEconomicsBackfillError(f"duplicate ready projection row id: {row_id}")
        result[row_id] = row
    return result


def _is_projection_row_id(value: str) -> bool:
    scope, separator, metric = str(value or "").partition("|")
    return bool(separator and scope.strip() and metric.strip())


def _validate_data_projection_layout(sheet: Mapping[str, Any], *, dates: list[str]) -> None:
    header = sheet.get("header")
    if not isinstance(header, list):
        raise FunctionalEconomicsBackfillError("DATA_VITRINA header is missing")
    if len(header) != 2 + len(dates):
        raise FunctionalEconomicsBackfillError(
            "DATA_VITRINA header width does not match date_columns"
        )
    if [str(value) for value in header[2:]] != dates:
        raise FunctionalEconomicsBackfillError(
            "DATA_VITRINA header dates do not match date_columns"
        )


def _snapshot_dates(plan_json: str) -> list[str]:
    try:
        payload = json.loads(str(plan_json))
        return _date_columns(payload)
    except Exception as exc:
        raise FunctionalEconomicsBackfillError(f"invalid ready snapshot plan: {exc}") from exc


def _targeted_non_target_digest(
    plan: Mapping[str, Any],
    *,
    affected_nm_ids: set[int],
    earliest_business_date: str,
    target_metric_keys: set[str],
    latest_business_date: str = "",
) -> str:
    """Redact only the exact SKU/date cells and dependent TOTAL cells."""

    value = deepcopy(dict(plan))
    dates = _date_columns(value)
    relevant_indices = [
        index
        for index, day in enumerate(dates)
        if day >= earliest_business_date
        and (not latest_business_date or day <= latest_business_date)
    ]
    sheet = _data_sheet(value)
    rows = sheet.get("rows") or []
    allowed_row_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, list) or len(row) < 2:
            continue
        row_id = str(row[1] or "")
        scope, separator, metric_key = row_id.partition("|")
        allowed_scope = scope == "TOTAL" or (
            scope.startswith("SKU:")
            and int(scope.split(":", 1)[1]) in affected_nm_ids
        )
        if (
            not separator
            or metric_key not in target_metric_keys
            or not allowed_scope
        ):
            continue
        allowed_row_ids.add(row_id)
        while len(row) < 2 + len(dates):
            row.append("")
        for index in relevant_indices:
            row[2 + index] = "__target_cell__"
    metadata = value.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        value["metadata"] = metadata
    metadata.pop("functional_economics_targeted_replay", None)
    coverage = metadata.get("warehouse_history_coverage")
    if isinstance(coverage, dict):
        for day in list(coverage):
            if (
                str(day) >= earliest_business_date
                and (not latest_business_date or str(day) <= latest_business_date)
            ):
                coverage.pop(day, None)
        if not coverage:
            metadata.pop("warehouse_history_coverage", None)
    presentation = metadata.get("server_cell_presentation")
    if isinstance(presentation, dict):
        for row_id in list(presentation):
            if row_id not in allowed_row_ids:
                continue
            day_map = presentation.get(row_id)
            if not isinstance(day_map, dict):
                continue
            for day in list(day_map):
                if (
                    str(day) >= earliest_business_date
                    and (
                        not latest_business_date
                        or str(day) <= latest_business_date
                    )
                ):
                    day_map.pop(day, None)
            if not day_map:
                presentation.pop(row_id, None)
        if not presentation:
            metadata.pop("server_cell_presentation", None)
    timestamps = metadata.get("row_last_updated_at_by_row_id")
    if isinstance(timestamps, dict):
        for row_id in allowed_row_ids:
            timestamps.pop(row_id, None)
        if not timestamps:
            metadata.pop("row_last_updated_at_by_row_id", None)
    if not metadata:
        value.pop("metadata", None)
    return "sha256:" + _hash(value)


def _non_target_digest(plan: Mapping[str, Any]) -> str:
    value = deepcopy(dict(plan))
    sheet = _data_sheet(value)
    rows = sheet.get("rows") or []
    sheet["rows"] = [
        row
        for row in rows
        if not (
            isinstance(row, list)
            and len(row) > 1
            and "|" in str(row[1])
            and str(row[1]).split("|", 1)[1] in MUTATED_READY_METRIC_KEYS
        )
    ]
    sheet.pop("row_count", None)
    sheet.pop("write_rect", None)
    metadata = value.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("functional_economics_backfill", None)
        metadata.pop("warehouse_history_coverage", None)
        metadata.pop(HISTORICAL_REPAIR_METADATA_KEY, None)
        presentation = metadata.get("server_cell_presentation")
        if isinstance(presentation, dict):
            for row_id in list(presentation):
                if "|" in row_id and row_id.split("|", 1)[1] in PRESENTATION_TARGET_KEYS:
                    presentation.pop(row_id, None)
            if not presentation:
                metadata.pop("server_cell_presentation", None)
        timestamps = metadata.get("row_last_updated_at_by_row_id")
        if isinstance(timestamps, dict):
            for row_id in list(timestamps):
                if "|" in row_id and row_id.split("|", 1)[1] in MUTATED_READY_METRIC_KEYS:
                    timestamps.pop(row_id, None)
            if not timestamps:
                metadata.pop("row_last_updated_at_by_row_id", None)
        if not metadata:
            value.pop("metadata", None)
    return "sha256:" + _hash(value)


def _plan_fingerprint(plan: Mapping[str, Any]) -> str:
    return "sha256:" + _hash({key: value for key, value in plan.items() if key != "plan_fingerprint"})


def _before_functional_economics_write_lock() -> None:
    """Test seam after full validation and before the bounded writer phase."""


def _before_functional_economics_target_update(
    *,
    connection: sqlite3.Connection,
    item: Mapping[str, Any],
) -> None:
    """Test seam after target read and before its optimistic plan_json CAS."""


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _snapshot_manifest_digest(rows: list[Mapping[str, Any]]) -> str:
    return "sha256:" + _hash(
        [
            [
                str(row.get("bundle_version") or ""),
                str(row.get("as_of_date") or ""),
                "sha256:" + _sha(str(row.get("plan_json") or "")),
                str(row.get("refreshed_at") or ""),
            ]
            for row in rows
        ]
    )


def _connect(path: Any) -> sqlite3.Connection:
    conn = connect_sqlite(str(path), priority="background")
    conn.row_factory = sqlite3.Row
    return conn
