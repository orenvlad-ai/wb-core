"""Guarded presentation-only historical own-cost carry-forward.

The adapter never changes warehouse, Finance, order, reservation, movement or
raw-source truth.  It selects the last versioned same-SKU Vitrina cost before
one closed date, proves that intervening physical events preserve the SKU cost
basis, and runs the canonical Proxy 3/4 formula materializer for one exact
SKU-and-TOTAL dependency closure.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_buyout_mature_backfill import (  # noqa: E402
    _file_digest,
    _query_only_connection,
    _require_evidence_outside_repo,
    _validate_exact_deployment,
)
from apps.ff_pool_dense_fbs import PRIVATE_PLAN_MAX_BYTES, _write_private  # noqa: E402
from apps.registry_upload_http_entrypoint_hosted_runtime import (  # noqa: E402
    ACTIVE_HOSTED_RUNTIME_TARGET_ID,
    load_hosted_runtime_target,
)
from packages.application.business_data_write_barrier import (  # noqa: E402
    barrier_status,
)
from packages.application.calculation_parameters import (  # noqa: E402
    DEFAULT_PROXY_PARAMETERS,
    PROXY_BLOCK_KEY,
    _parameters_from_row as _proxy3_parameters_from_row,
)
from packages.application.calculation_parameters_v4 import (  # noqa: E402
    PROXY_V4_BLOCK_KEY,
    _parameters_from_row as _proxy4_parameters_from_row,
)
from packages.application.canonical_wb_cost_resolver import (  # noqa: E402
    COMMON_INVENTORY_COST_FORMULA_VERSION,
)
from packages.application.ff_pool_foundation import (  # noqa: E402
    LINES_TABLE,
    OPERATIONS_TABLE,
)
from packages.application.inventory_cost_blend import (  # noqa: E402
    INVENTORY_COST_BLEND_EFFECTIVE_DATE,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.sheet_vitrina_v1_our_wb_costs import (  # noqa: E402
    OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
    OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_UNIT_COST_RUB_METRIC_KEY,
)
from packages.application.sheet_vitrina_v1_own_product_capital import (  # noqa: E402
    OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS,
    OWN_TOTAL_QTY_METRIC_KEY,
)
from packages.application.sheet_vitrina_v1_proxy_v4 import (  # noqa: E402
    PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY,
    PROXY_V4_MARGIN_PCT_METRIC_KEY,
    PROXY_V4_PROFIT_RUB_METRIC_KEY,
)
from packages.application.warehouse_fbs_material_rematerialization import (  # noqa: E402
    CRITICAL_TOTAL_METRIC_KEYS,
    _ready_cells,
)
from packages.application.warehouse_functional_economics_backfill import (  # noqa: E402
    _transform_snapshot,
)
from packages.application.warehouse_sync_lock import (  # noqa: E402
    WarehouseSyncBusyError,
    warehouse_sync_lock,
)
from packages.business_time import business_date_from_timestamp  # noqa: E402


SCHEMA_VERSION = "sheet_vitrina_v1_historical_analytical_cost_carry_forward_v1"
SELECTION_METHOD = "same_sku_last_trusted_prior_cost_no_cost_change_v1"
VERSIONS_TABLE = "sheet_vitrina_v1_historical_analytical_cost_versions"
MAX_SOURCE_LOOKBACK_DAYS = 31
ZERO = Decimal("0")
MONEY_TOLERANCE = Decimal("0.000001")
SKU_FORMULA_KEYS = (
    OUR_WB_UNIT_COST_RUB_METRIC_KEY,
    OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
    PROXY_V4_PROFIT_RUB_METRIC_KEY,
    PROXY_V4_MARGIN_PCT_METRIC_KEY,
    PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY,
)
ALLOWED_TOTAL_KEYS = tuple(CRITICAL_TOTAL_METRIC_KEYS)


class HistoricalCostCarryForwardError(RuntimeError):
    """A typed presentation-only correction guard failed closed."""

    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details = details


def run(
    *,
    runtime_dir: Path,
    evidence_dir: Path,
    operation_id: str,
    business_date: str,
    nm_id: int,
    apply: bool,
    manifest_path: Path | None = None,
    expected_manifest_sha256: str = "",
    expected_deployed_sha: str = "",
    approval_reference: str = "",
    deployed_sha_file: Path | None = None,
    target_file: Path | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    runtime_dir = runtime_dir.expanduser().resolve()
    evidence_dir = evidence_dir.expanduser().resolve()
    _require_evidence_outside_repo(evidence_dir)
    operation = str(operation_id or "").strip()
    if not operation or len(operation) > 160:
        raise HistoricalCostCarryForwardError(
            "operation_identity_invalid", "operation_id is missing or too long"
        )
    target_date = date.fromisoformat(str(business_date)).isoformat()
    target_nm_id = int(nm_id)
    if target_nm_id <= 0:
        raise HistoricalCostCarryForwardError(
            "target_identity_invalid", "nm_id must be positive"
        )
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    if not runtime.db_path.is_file():
        raise HistoricalCostCarryForwardError(
            "canonical_store_missing", "canonical operational SQLite store is missing"
        )
    manifest = runtime.store_registry.load(require_files=True)
    if manifest.implicit and target_file is not None:
        raise HistoricalCostCarryForwardError(
            "canonical_generation_implicit",
            "presentation correction requires one explicit StoreRegistry generation",
        )
    generation = {
        "manifest_sha256": manifest.manifest_sha256,
        "generation_epoch": manifest.generation_epoch,
        "generation_id": manifest.operational.generation_id,
        "schema_revision": manifest.operational.schema_revision,
        "relative_path": manifest.operational.relative_path,
    }
    timestamp = created_at or _utc_now()
    target_binding = _validate_target_binding(
        runtime_dir=runtime_dir,
        target_file=target_file,
        expected_deployed_sha=expected_deployed_sha,
        deployed_sha_file=deployed_sha_file,
    )
    if not apply:
        plan = _build_plan(
            db_path=runtime.db_path,
            operation_id=operation,
            business_date=target_date,
            nm_id=target_nm_id,
            created_at=timestamp,
            storage_generation=generation,
        )
        _ensure_private_evidence_dir(evidence_dir)
        output = evidence_dir / (
            "historical-cost-carry-forward-plan-"
            + _plan_timestamp(str(timestamp))
            + ".json"
        )
        written = _write_private(
            output,
            plan,
            owner="production_apply_evidence",
            max_output_bytes=PRIVATE_PLAN_MAX_BYTES,
            require_private_parent=True,
            no_overwrite=True,
        )
        if not written.get("written"):
            raise HistoricalCostCarryForwardError(
                str(written.get("reason") or "private_plan_persistence_failed"),
                str(written.get("error") or "private plan persistence failed"),
                details=written,
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "dry-run",
            "status": "ready",
            "database_written": False,
            "query_only": True,
            "operation_id": operation,
            "version_id": plan["version_id"],
            "manifest_path": str(output),
            "manifest_sha256": _file_digest(output),
            "plan_persistence": {
                "owner": "production_apply_evidence",
                "destination": str(output),
                "evidence_dir": str(evidence_dir),
                "evidence_dir_mode": "0700",
                **{
                    key: value
                    for key, value in written.items()
                    if key not in {"written", "mode", "path"}
                },
            },
            "business_date": target_date,
            "nm_id": target_nm_id,
            "source_business_date": plan["source"]["business_date"],
            "source_unit_cost_rub": plan["source"]["unit_cost_rub"],
            "source_digest": plan["source"]["source_digest"],
            "before_metrics": plan["before"]["metrics"],
            "after_metrics": plan["after"]["metrics"],
            "non_target_digest": plan["before"]["non_target_digest"],
            "other_ready_snapshots_digest": plan["before"][
                "other_ready_snapshots_digest"
            ],
            "accepted_vitrina_version_count": 1,
            "updated_ready_snapshot_count": 1,
            "storage_generation": generation,
            "target_binding": target_binding,
            "target_generation_bound": True,
            "barrier_inactive": barrier_status(runtime_dir).get("active") is False,
            "timer_change_count": 0,
            "material_qualification_digest": plan["material_digest"],
        }

    if (
        manifest_path is None
        or not expected_manifest_sha256
        or not expected_deployed_sha
        or not approval_reference
    ):
        raise HistoricalCostCarryForwardError(
            "apply_identity_incomplete",
            "apply requires manifest, manifest SHA, deployed SHA and approval reference",
        )
    deployed_sha = str(target_binding.get("deployed_sha") or "")
    reviewed_path = manifest_path.expanduser().resolve()
    reviewed_sha = _file_digest(reviewed_path)
    if reviewed_sha != str(expected_manifest_sha256):
        raise HistoricalCostCarryForwardError(
            "manifest_digest_mismatch", "reviewed manifest SHA-256 mismatch"
        )
    reviewed = json.loads(reviewed_path.read_text(encoding="utf-8"))
    _validate_reviewed_manifest(
        reviewed,
        operation_id=operation,
        business_date=target_date,
        nm_id=target_nm_id,
        storage_generation=generation,
    )
    before_barrier = barrier_status(runtime_dir)
    if before_barrier.get("active") is not False:
        raise HistoricalCostCarryForwardError(
            "write_barrier_active", "business-data write barrier is active"
        )
    try:
        with warehouse_sync_lock(runtime_dir, blocking=False):
            under_lock_barrier = barrier_status(runtime_dir)
            if (
                under_lock_barrier.get("active") is not False
                or under_lock_barrier.get("status") != before_barrier.get("status")
            ):
                raise HistoricalCostCarryForwardError(
                    "write_barrier_drift",
                    "business-data write barrier changed at the shared-lock boundary",
                )
            rebuilt = _build_plan(
                db_path=runtime.db_path,
                operation_id=operation,
                business_date=target_date,
                nm_id=target_nm_id,
                created_at=str(reviewed["created_at"]),
                storage_generation=generation,
            )
            if _digest(_material_plan(rebuilt)) != _digest(_material_plan(reviewed)):
                raise HistoricalCostCarryForwardError(
                    "jit_material_cas_drift",
                    "target, source or formula inputs changed after qualification",
                )
            backup_path = evidence_dir / "backups" / (
                f"historical-cost-{operation}-{reviewed_sha[-12:]}.sqlite3"
            )
            backup = runtime.backup_database(
                backup_path,
                admission_owner="production_apply_evidence",
            )
            descriptor = os.open(backup_path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            result = _submit_once(
                db_path=runtime.db_path,
                plan=rebuilt,
                manifest_sha256=reviewed_sha,
                deployed_sha=deployed_sha,
                approval_reference=str(approval_reference),
                backup=backup,
            )
    except WarehouseSyncBusyError as exc:
        raise HistoricalCostCarryForwardError(
            "shared_writer_busy",
            "canonical warehouse/ready-snapshot writer is busy; submit_count remains zero",
        ) from exc

    readback = _readback(
        db_path=runtime.db_path,
        operation_id=operation,
        expected_plan=reviewed,
    )
    if readback["status"] != "reconciled":
        raise HistoricalCostCarryForwardError(
            "post_submit_reconciliation_failed",
            "query-only post-submit reconciliation did not match the reviewed operation",
            details=readback,
        )
    _ensure_private_evidence_dir(evidence_dir)
    receipt = {
        **result,
        "readback": readback,
        "barrier_before": before_barrier,
        "barrier_under_lock": under_lock_barrier,
        "runtime_controls_changed": False,
    }
    receipt_path = evidence_dir / f"historical-cost-carry-forward-receipt-{operation}.json"
    written_receipt = _write_private(
        receipt_path,
        receipt,
        owner="production_apply_evidence",
        max_output_bytes=PRIVATE_PLAN_MAX_BYTES,
        require_private_parent=True,
        no_overwrite=True,
    )
    if not written_receipt.get("written"):
        raise HistoricalCostCarryForwardError(
            str(written_receipt.get("reason") or "private_receipt_persistence_failed"),
            str(written_receipt.get("error") or "private receipt persistence failed"),
            details=written_receipt,
        )
    return {
        **receipt,
        "receipt_path": str(receipt_path),
        "receipt_sha256": _file_digest(receipt_path),
    }


def readback(
    *,
    runtime_dir: Path,
    operation_id: str,
    manifest_path: Path | None = None,
    target_file: Path | None = None,
    expected_deployed_sha: str = "",
    deployed_sha_file: Path | None = None,
) -> dict[str, Any]:
    resolved_runtime_dir = runtime_dir.expanduser().resolve()
    _validate_target_binding(
        runtime_dir=resolved_runtime_dir,
        target_file=target_file,
        expected_deployed_sha=expected_deployed_sha,
        deployed_sha_file=deployed_sha_file,
    )
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=resolved_runtime_dir)
    expected = (
        json.loads(manifest_path.expanduser().resolve().read_text(encoding="utf-8"))
        if manifest_path is not None
        else None
    )
    return _readback(
        db_path=runtime.db_path,
        operation_id=str(operation_id),
        expected_plan=expected,
    )


def _build_plan(
    *,
    db_path: Path,
    operation_id: str,
    business_date: str,
    nm_id: int,
    created_at: str,
    storage_generation: Mapping[str, Any],
) -> dict[str, Any]:
    with _query_only_connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        snapshot = _target_snapshot(conn, business_date=business_date)
        before_payload = _loads_object(snapshot["plan_json"], "target ready snapshot")
        before_cells = _ready_cells(before_payload, business_date)
        if not before_cells:
            raise HistoricalCostCarryForwardError(
                "target_date_column_missing", "target ready snapshot lacks the exact date column"
            )
        source = _select_prior_source(
            conn,
            business_date=business_date,
            nm_id=nm_id,
        )
        event_evidence = _prove_no_cost_changing_event(
            conn,
            source_date=str(source["business_date"]),
            business_date=business_date,
            nm_id=nm_id,
        )
        version_id = "hvacf_" + hashlib.sha256(
            _canonical_json(
                {
                    "operation_id": operation_id,
                    "business_date": business_date,
                    "nm_id": nm_id,
                    "source_digest": source["source_digest"],
                    "target_plan_sha256": _sha_text(str(snapshot["plan_json"])),
                    "selection_method": SELECTION_METHOD,
                }
            ).encode("utf-8")
        ).hexdigest()[:24]
        _validate_before_shape(before_cells, nm_id=nm_id)
        costs, warehouse_metrics, scope_nm_ids = _persisted_formula_inputs(
            before_cells,
            business_date=business_date,
            target_nm_id=nm_id,
            source=source,
        )
        proxy3 = _load_proxy3(conn, business_date)
        proxy4 = _load_proxy4(conn, business_date)
        formula_inputs = {
            "source": source,
            "event_evidence_digest": event_evidence["digest"],
            "costs_digest": _digest(costs),
            "proxy3": proxy3.public(),
            "proxy4": proxy4.public() if proxy4 is not None else None,
        }
        transformed = _transform_snapshot(
            dict(snapshot),
            costs={business_date: costs},
            warehouse_metrics={business_date: warehouse_metrics},
            warehouse_exact_dates={business_date},
            warehouse_covered_nm_ids={business_date: set(scope_nm_ids)},
            warehouse_version_ids={business_date: "presentation_only_preserved"},
            parameters={business_date: proxy3},
            proxy_v4_parameters={business_date: proxy4},
            source_fingerprint=_digest(formula_inputs),
            cutover_business_date=business_date,
            operation_business_date=business_date,
            affected_nm_ids=[nm_id],
            earliest_business_date=business_date,
            latest_business_date=business_date,
        )
        if int(transformed.get("inserted_rows") or 0) != 0:
            raise HistoricalCostCarryForwardError(
                "formula_rows_missing",
                "targeted materialization would need to insert formula rows",
            )
        transformed_payload = _loads_object(
            transformed["after_plan_json"], "formula candidate"
        )
        after_payload = _exact_closure_payload(
            before_payload=before_payload,
            formula_payload=transformed_payload,
            business_date=business_date,
            nm_id=nm_id,
            version_id=version_id,
            operation_id=operation_id,
            created_at=created_at,
            source=source,
            event_evidence=event_evidence,
            formula_inputs_digest=_digest(formula_inputs),
        )
        after_json = _canonical_json(after_payload)
        after_cells = _ready_cells(after_payload, business_date)
        _validate_after_shape(after_cells, nm_id=nm_id)
        before_metrics = _metric_receipt(before_cells, nm_id=nm_id)
        after_metrics = _metric_receipt(after_cells, nm_id=nm_id)
        non_target_before = _non_target_digest(
            before_payload, business_date=business_date, nm_id=nm_id
        )
        non_target_after = _non_target_digest(
            after_payload, business_date=business_date, nm_id=nm_id
        )
        if non_target_before != non_target_after:
            raise HistoricalCostCarryForwardError(
                "non_target_candidate_drift",
                "candidate changes data outside the exact SKU/formula/TOTAL closure",
            )
        other_ready_snapshots_digest = _other_ready_snapshots_digest(
            conn,
            bundle_version=str(snapshot["bundle_version"]),
            as_of_date=str(snapshot["as_of_date"]),
            snapshot_id=str(snapshot["snapshot_id"]),
        )
        plan = {
            "schema_version": SCHEMA_VERSION,
            "status": "ready",
            "operation_id": operation_id,
            "version_id": version_id,
            "created_at": created_at,
            "selection_method": SELECTION_METHOD,
            "business_date": business_date,
            "nm_id": nm_id,
            "storage_generation": dict(storage_generation),
            "target": {
                "bundle_version": str(snapshot["bundle_version"]),
                "as_of_date": str(snapshot["as_of_date"]),
                "snapshot_id": str(snapshot["snapshot_id"]),
                "plan_version": str(snapshot["plan_version"]),
                "activated_at": str(snapshot["activated_at"]),
                "refreshed_at": str(snapshot["refreshed_at"]),
                "before_plan_sha256": _sha_text(str(snapshot["plan_json"])),
                "after_plan_sha256": _sha_text(after_json),
                "after_plan_json": after_json,
                "canonical_candidate_count": int(snapshot["candidate_count"]),
            },
            "source": source,
            "cost_event_evidence": event_evidence,
            "formula": {
                "inventory_cost_formula_version": COMMON_INVENTORY_COST_FORMULA_VERSION,
                "proxy3_formula_version": proxy3.public().get("formula_version"),
                "proxy4_formula_version": (
                    proxy4.public().get("formula_version") if proxy4 is not None else None
                ),
                "inputs_digest": _digest(formula_inputs),
                "exact_target_and_total_only": True,
            },
            "before": {
                "metrics": before_metrics,
                "non_target_digest": non_target_before,
                "other_ready_snapshots_digest": other_ready_snapshots_digest,
            },
            "after": {
                "metrics": after_metrics,
                "non_target_digest": non_target_after,
                "other_ready_snapshots_digest": other_ready_snapshots_digest,
            },
            "expected_effect": {
                "accepted_vitrina_version_count": 1,
                "updated_ready_snapshot_count": 1,
                "submit_count": 1,
                "warehouse_rows_changed": 0,
                "source_facts_changed": 0,
            },
        }
        plan["material_digest"] = _digest(_material_plan(plan))
        return plan


def _target_snapshot(conn: sqlite3.Connection, *, business_date: str) -> dict[str, Any]:
    rows = conn.execute(
        """SELECT bundle_version,activated_at,as_of_date,snapshot_id,plan_version,
                  refreshed_at,plan_json
             FROM sheet_vitrina_v1_ready_snapshots
            WHERE as_of_date=?
            ORDER BY activated_at DESC,refreshed_at DESC,bundle_version DESC""",
        (business_date,),
    ).fetchall()
    if not rows:
        raise HistoricalCostCarryForwardError(
            "canonical_target_missing",
            "exact business date does not resolve to a canonical ready snapshot",
            details={"candidate_count": len(rows)},
        )
    return {**dict(rows[0]), "candidate_count": len(rows)}


def _select_prior_source(
    conn: sqlite3.Connection,
    *,
    business_date: str,
    nm_id: int,
) -> dict[str, Any]:
    costs = _load_trusted_prior_published_costs(conn)
    candidates = [
        dict(item)
        for (day, item_nm_id), item in costs.items()
        if item_nm_id == str(nm_id) and str(day) < business_date
    ]
    candidates.sort(key=lambda item: str(item["business_date"]), reverse=True)
    if not candidates:
        raise HistoricalCostCarryForwardError(
            "trusted_prior_cost_missing",
            "same-SKU trusted prior Vitrina cost is absent",
        )
    source = candidates[0]
    distance = (date.fromisoformat(business_date) - date.fromisoformat(str(source["business_date"]))).days
    if distance < 1 or distance > MAX_SOURCE_LOOKBACK_DAYS:
        raise HistoricalCostCarryForwardError(
            "trusted_prior_cost_outside_bound",
            "last trusted same-SKU cost is outside the bounded lookback",
            details={"source_business_date": source["business_date"], "days": distance},
        )
    value = _decimal(source.get("unit_cost_rub"))
    if value is None or value <= ZERO or not str(source.get("source_digest") or "").startswith("sha256:"):
        raise HistoricalCostCarryForwardError(
            "trusted_prior_cost_invalid",
            "last same-SKU prior cost is not positive versioned evidence",
        )
    return {
        "business_date": str(source["business_date"]),
        "nm_id": int(nm_id),
        "unit_cost_rub": format(value, "f"),
        "source_digest": str(source["source_digest"]),
        "bundle_version": str(source["bundle_version"]),
        "snapshot_id": str(source["snapshot_id"]),
        "refreshed_at": str(source["refreshed_at"]),
        "formula_version": str(source["formula_version"]),
        "selection_method": SELECTION_METHOD,
    }


def _load_trusted_prior_published_costs(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Load only version-tagged exact-day own-cost cells from ready snapshots."""

    result: dict[tuple[str, str], dict[str, Any]] = {}
    snapshots = conn.execute(
        """SELECT bundle_version,as_of_date,snapshot_id,refreshed_at,plan_json
             FROM sheet_vitrina_v1_ready_snapshots
            ORDER BY refreshed_at,bundle_version,as_of_date,snapshot_id"""
    )
    for snapshot in snapshots:
        try:
            plan = json.loads(str(snapshot["plan_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        dates = plan.get("date_columns")
        sheets = plan.get("sheets")
        metadata = plan.get("metadata")
        if not isinstance(dates, list) or not isinstance(sheets, list):
            continue
        eligible_dates: set[str] = set()
        if isinstance(metadata, Mapping):
            for marker_key in (
                "functional_economics_backfill",
                "functional_economics_targeted_replay",
            ):
                marker = metadata.get(marker_key)
                publication = (
                    marker.get("inventory_cost_publication")
                    if isinstance(marker, Mapping)
                    else None
                )
                evidence = (
                    publication.get("date_evidence")
                    if isinstance(publication, Mapping)
                    and str(publication.get("formula_version") or "")
                    == COMMON_INVENTORY_COST_FORMULA_VERSION
                    else None
                )
                if isinstance(evidence, Mapping):
                    eligible_dates.update(
                        str(day)
                        for day, item in evidence.items()
                        if isinstance(item, Mapping)
                    )
        if not eligible_dates:
            continue
        data_sheet = next(
            (
                item
                for item in sheets
                if isinstance(item, Mapping)
                and str(item.get("sheet_name") or item.get("name") or "")
                == "DATA_VITRINA"
            ),
            None,
        )
        if not isinstance(data_sheet, Mapping):
            continue
        normalized_dates = [str(item or "") for item in dates]
        for row in data_sheet.get("rows") or []:
            if not isinstance(row, list) or len(row) < 2:
                continue
            row_id = str(row[1] or "")
            scope, separator, metric = row_id.partition("|")
            if (
                not separator
                or not scope.startswith("SKU:")
                or metric != OUR_WB_UNIT_COST_RUB_METRIC_KEY
            ):
                continue
            item_nm_id = scope.removeprefix("SKU:").strip()
            if not item_nm_id.isdigit() or int(item_nm_id) <= 0:
                continue
            for index, item_date in enumerate(normalized_dates):
                if item_date not in eligible_dates or len(row) <= index + 2:
                    continue
                unit_cost = _decimal(row[index + 2])
                if unit_cost is None or unit_cost <= ZERO:
                    continue
                source_payload = {
                    "bundle_version": str(snapshot["bundle_version"]),
                    "snapshot_id": str(snapshot["snapshot_id"]),
                    "refreshed_at": str(snapshot["refreshed_at"]),
                    "business_date": item_date,
                    "nm_id": item_nm_id,
                    "metric": OUR_WB_UNIT_COST_RUB_METRIC_KEY,
                    "formula_version": COMMON_INVENTORY_COST_FORMULA_VERSION,
                    "unit_cost_rub": format(unit_cost, "f"),
                }
                result[(item_date, item_nm_id)] = {
                    **source_payload,
                    "source_digest": _digest(source_payload),
                }
    return result


def _prove_no_cost_changing_event(
    conn: sqlite3.Connection,
    *,
    source_date: str,
    business_date: str,
    nm_id: int,
) -> dict[str, Any]:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    required = {
        "sheet_vitrina_v1_warehouse_functional_versions",
        "sheet_vitrina_v1_warehouse_functional_balances",
        "sheet_vitrina_v1_ff_pool_fbs_lifecycle_events",
        OPERATIONS_TABLE,
        LINES_TABLE,
    }
    missing = sorted(required - tables)
    if missing:
        raise HistoricalCostCarryForwardError(
            "cost_event_evidence_schema_missing",
            "cost-event evidence schema is incomplete",
            details={"missing_tables": missing},
        )
    version_rows = [
        dict(row)
        for row in conn.execute(
            """SELECT version.version_id,version.business_effective_date,
                      version.published_at,balance.warehouse_key,balance.quantity,
                      balance.cost_covered_quantity,balance.capital_rub,balance.wac_rub,
                      balance.quality,balance.provenance_json
                 FROM sheet_vitrina_v1_warehouse_functional_versions version
                 JOIN sheet_vitrina_v1_warehouse_functional_balances balance
                   ON balance.version_id=version.version_id
                WHERE version.status='good' AND balance.nm_id=?
                  AND balance.warehouse_key IN ('wb','ff')
                  AND version.business_effective_date BETWEEN ? AND ?
                ORDER BY version.business_effective_date,version.published_at,
                         version.version_id,balance.warehouse_key""",
            (nm_id, source_date, business_date),
        ).fetchall()
    ]
    days = {str(item["business_effective_date"]) for item in version_rows}
    if source_date not in days or business_date not in days:
        raise HistoricalCostCarryForwardError(
            "cost_event_version_boundary_missing",
            "exact source and cutoff functional version boundaries are not both present",
            details={"observed_days": sorted(days)},
        )
    anchor_wac: dict[str, Decimal] = {}
    version_evidence: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    for item in version_rows:
        quantity = _decimal(item["quantity"])
        capital = _decimal(item["capital_rub"])
        wac = _decimal(item["wac_rub"])
        covered = _decimal(item["cost_covered_quantity"])
        stage = str(item["warehouse_key"])
        if quantity is None or capital is None or covered is None or quantity < ZERO or capital < ZERO:
            blockers.append({"reason": "invalid_stage_operands", "version_id": item["version_id"], "stage": stage})
            continue
        if quantity > ZERO:
            if wac is None or wac <= ZERO or abs((capital / quantity) - wac) > MONEY_TOLERANCE:
                blockers.append({"reason": "stage_wac_arithmetic_invalid", "version_id": item["version_id"], "stage": stage})
                continue
            if str(item["business_effective_date"]) == source_date:
                previous = anchor_wac.get(stage)
                if previous is not None and abs(previous - wac) > MONEY_TOLERANCE:
                    blockers.append({"reason": "source_stage_wac_ambiguous", "stage": stage})
                anchor_wac[stage] = wac
            elif stage in anchor_wac and abs(anchor_wac[stage] - wac) > MONEY_TOLERANCE:
                blockers.append(
                    {
                        "reason": "intervening_stage_wac_changed",
                        "version_id": item["version_id"],
                        "stage": stage,
                        "expected_wac": format(anchor_wac[stage], "f"),
                        "observed_wac": format(wac, "f"),
                    }
                )
        version_evidence.append(
            {
                "version_id": str(item["version_id"]),
                "business_effective_date": str(item["business_effective_date"]),
                "published_at": str(item["published_at"]),
                "stage": stage,
                "quantity": str(item["quantity"]),
                "cost_covered_quantity": str(item["cost_covered_quantity"]),
                "capital_rub": str(item["capital_rub"]),
                "wac_rub": str(item["wac_rub"] or ""),
                "quality": str(item["quality"]),
                "provenance_digest": _sha_text(str(item["provenance_json"])),
            }
        )
    if "ff" not in anchor_wac:
        blockers.append({"reason": "source_ff_wac_missing"})

    movement_rows = [
        dict(row)
        for row in conn.execute(
            f"""SELECT operation.operation_id,operation.operation_type,
                       operation.source_system,operation.source_type,
                       operation.source_id,operation.source_revision,
                       operation.business_date,line.line_no,line.facility_id,line.pool,
                       line.quantity_delta,line.capital_delta_rub,line.wac_snapshot_rub,
                       line.metadata_json
                  FROM {LINES_TABLE} line
                  JOIN {OPERATIONS_TABLE} operation
                    ON operation.operation_id=line.operation_id
                 WHERE line.nm_id=? AND operation.business_date>? AND operation.business_date<=?
                 ORDER BY operation.business_date,operation.operation_id,line.line_no""",
            (nm_id, source_date, business_date),
        ).fetchall()
    ]
    movement_evidence: list[dict[str, Any]] = []
    for item in movement_rows:
        quantity = Decimal(int(item["quantity_delta"]))
        capital = _decimal(item["capital_delta_rub"])
        snapshot_wac = _decimal(item["wac_snapshot_rub"])
        evidence = {
            key: item[key]
            for key in (
                "operation_id",
                "operation_type",
                "source_system",
                "source_type",
                "source_id",
                "source_revision",
                "business_date",
                "line_no",
                "facility_id",
                "pool",
                "quantity_delta",
                "capital_delta_rub",
                "wac_snapshot_rub",
            )
        }
        evidence["metadata_digest"] = _sha_text(str(item["metadata_json"]))
        movement_evidence.append(evidence)
        if capital is None:
            blockers.append({"reason": "movement_capital_invalid", "operation_id": item["operation_id"]})
        elif quantity > ZERO:
            blockers.append({"reason": "intervening_receipt", "operation_id": item["operation_id"]})
        elif quantity == ZERO and capital != ZERO:
            blockers.append({"reason": "intervening_cost_adjustment", "operation_id": item["operation_id"]})
        elif quantity < ZERO:
            prior = anchor_wac.get("ff")
            actual = capital / quantity if capital is not None else None
            if (
                prior is None
                or actual is None
                or abs(actual - prior) > MONEY_TOLERANCE
                or (snapshot_wac is not None and abs(snapshot_wac - prior) > MONEY_TOLERANCE)
            ):
                blockers.append({"reason": "non_proportional_debit", "operation_id": item["operation_id"]})

    lifecycle_rows = [
        dict(row)
        for row in conn.execute(
            """SELECT event_id,event_sequence,event_type,facility_id,pool,quantity,
                      physical_quantity_delta,capital_delta_rub,frozen_wac_rub,
                      evidence_digest,occurred_at
                 FROM sheet_vitrina_v1_ff_pool_fbs_lifecycle_events
                WHERE nm_id=? ORDER BY event_sequence""",
            (nm_id,),
        ).fetchall()
    ]
    lifecycle_evidence: list[dict[str, Any]] = []
    for item in lifecycle_rows:
        try:
            event_date = business_date_from_timestamp(str(item["occurred_at"]))
        except (TypeError, ValueError):
            blockers.append({"reason": "lifecycle_business_date_invalid", "event_id": item["event_id"]})
            continue
        if not (source_date < event_date <= business_date):
            continue
        compact = {**item, "business_date": event_date}
        lifecycle_evidence.append(compact)
        delta = Decimal(int(item["physical_quantity_delta"]))
        capital = _decimal(item["capital_delta_rub"])
        frozen = _decimal(item["frozen_wac_rub"])
        prior = anchor_wac.get("ff")
        if str(item["event_type"]) not in {"handoff_debit", "opening_handoff_debit"}:
            blockers.append({"reason": "intervening_lifecycle_event", "event_id": item["event_id"], "event_type": item["event_type"]})
        elif (
            delta >= ZERO
            or capital is None
            or frozen is None
            or prior is None
            or abs(frozen - prior) > MONEY_TOLERANCE
            or abs((capital / delta) - prior) > MONEY_TOLERANCE
        ):
            blockers.append({"reason": "non_proportional_lifecycle_debit", "event_id": item["event_id"]})
    if blockers:
        raise HistoricalCostCarryForwardError(
            "cost_changing_event_or_ambiguity",
            "intervening receipt, revaluation, cost change or ambiguous evidence blocks carry-forward",
            details={"blockers": blockers, "blocker_digest": _digest(blockers)},
        )
    material = {
        "source_date": source_date,
        "business_date": business_date,
        "nm_id": nm_id,
        "anchor_stage_wac": {key: format(value, "f") for key, value in sorted(anchor_wac.items())},
        "functional_versions": version_evidence,
        "movement_lines": movement_evidence,
        "lifecycle_events": lifecycle_evidence,
        "cost_changing_event_count": 0,
        "allowed_proportional_debit_count": sum(1 for item in movement_rows if int(item["quantity_delta"]) < 0),
        "allowed_lifecycle_debit_count": len(lifecycle_evidence),
    }
    return {**material, "digest": _digest(material)}


def _persisted_formula_inputs(
    cells: Mapping[str, Any],
    *,
    business_date: str,
    target_nm_id: int,
    source: Mapping[str, Any],
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]], list[int]]:
    scopes = sorted(
        {
            int(key.split("|", 1)[0].split(":", 1)[1])
            for key in cells
            if key.startswith("SKU:") and "|" in key
        }
    )
    if target_nm_id not in scopes:
        raise HistoricalCostCarryForwardError(
            "target_sku_scope_missing", "target SKU is absent from ready snapshot scope"
        )
    costs: dict[int, dict[str, Any]] = {}
    warehouse: dict[int, dict[str, Any]] = {}
    for item_nm_id in scopes:
        cost = _decimal(cells.get(f"SKU:{item_nm_id}|{OUR_WB_UNIT_COST_RUB_METRIC_KEY}"))
        if item_nm_id == target_nm_id:
            cost = _decimal(source["unit_cost_rub"])
        quantity = _decimal(cells.get(f"SKU:{item_nm_id}|{OWN_TOTAL_QTY_METRIC_KEY}"))
        if quantity is None or quantity < ZERO:
            raise HistoricalCostCarryForwardError(
                "presentation_quantity_missing",
                "exact ready-snapshot quantity is missing for analytical total weighting",
                details={"nm_id": item_nm_id},
            )
        resolved = cost is not None and cost > ZERO and quantity > ZERO
        capital = quantity * cost if resolved and cost is not None else ZERO
        evidence = {
            "status": "resolved" if resolved else "unresolved",
            "reason": "" if resolved else "no_positive_persisted_presentation_cost",
            "formula_version": COMMON_INVENTORY_COST_FORMULA_VERSION,
            "selection_method": (
                SELECTION_METHOD if item_nm_id == target_nm_id else "persisted_ready_snapshot_exact_cell_v1"
            ),
            "analytical_only": True,
            "business_date": business_date,
            "nm_id": item_nm_id,
            "source_business_date": (
                str(source["business_date"]) if item_nm_id == target_nm_id else business_date
            ),
            "source_digest": (
                str(source["source_digest"]) if item_nm_id == target_nm_id else _digest({"nm_id": item_nm_id, "business_date": business_date, "cost": format(cost, "f") if cost is not None else None})
            ),
            "quantity": format(quantity, "f"),
            "cost_covered_quantity": format(quantity if resolved else ZERO, "f"),
            "capital_rub": format(capital, "f"),
            "wac_rub": format(cost, "f") if resolved and cost is not None else None,
            "quantity_basis": "persisted_ready_snapshot_presentation_only",
            "stages": [],
        }
        costs[item_nm_id] = {
            "as_of_date": business_date,
            "canonical_source_date": evidence["source_business_date"],
            "nm_id": item_nm_id,
            "stock_qty": float(quantity),
            "cost_covered_qty": float(quantity if resolved else ZERO),
            "our_wb_unit_cost_rub": float(cost) if cost is not None and cost > ZERO else None,
            "source_status": "historical_analytical_carry_forward" if item_nm_id == target_nm_id else "persisted_ready_snapshot_exact",
            "selection_method": evidence["selection_method"],
            "source_digest": evidence["source_digest"],
            "inventory_cost_evidence": evidence,
        }
        warehouse[item_nm_id] = {
            key: cells.get(f"SKU:{item_nm_id}|{key}")
            for key in OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS
        }
    return costs, warehouse, scopes


def _exact_closure_payload(
    *,
    before_payload: Mapping[str, Any],
    formula_payload: Mapping[str, Any],
    business_date: str,
    nm_id: int,
    version_id: str,
    operation_id: str,
    created_at: str,
    source: Mapping[str, Any],
    event_evidence: Mapping[str, Any],
    formula_inputs_digest: str,
) -> dict[str, Any]:
    after = deepcopy(dict(before_payload))
    before_rows = _data_rows(after)
    formula_rows = _data_rows(formula_payload)
    dates = [str(item) for item in after.get("date_columns") or []]
    if business_date not in dates:
        raise HistoricalCostCarryForwardError(
            "target_date_column_missing", "target date is absent during closure projection"
        )
    index = dates.index(business_date) + 2
    allowed = {
        *(f"SKU:{nm_id}|{key}" for key in SKU_FORMULA_KEYS),
        *(f"TOTAL|{key}" for key in ALLOWED_TOTAL_KEYS),
    }
    for row_id in sorted(allowed):
        if row_id not in before_rows or row_id not in formula_rows:
            raise HistoricalCostCarryForwardError(
                "formula_dependency_row_missing",
                "exact formula dependency row is missing",
                details={"row_id": row_id},
            )
        before_rows[row_id][index] = formula_rows[row_id][index]
    metadata = after.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        raise HistoricalCostCarryForwardError(
            "target_metadata_invalid", "ready snapshot metadata is not an object"
        )
    versions = metadata.setdefault("historical_analytical_cost_carry_forward", {})
    if not isinstance(versions, dict):
        raise HistoricalCostCarryForwardError(
            "carry_forward_metadata_conflict", "carry-forward metadata key is not an object"
        )
    if version_id in versions:
        raise HistoricalCostCarryForwardError(
            "version_identity_already_present", "new carry-forward version identity already exists"
        )
    versions[version_id] = {
        "status": "accepted",
        "operation_id": operation_id,
        "business_date": business_date,
        "nm_id": nm_id,
        "source_business_date": str(source["business_date"]),
        "source_unit_cost_rub": str(source["unit_cost_rub"]),
        "source_digest": str(source["source_digest"]),
        "selection_method": SELECTION_METHOD,
        "event_evidence_digest": str(event_evidence["digest"]),
        "formula_inputs_digest": formula_inputs_digest,
        "created_at": created_at,
        "analytical_only": True,
        "warehouse_truth_reconstructed": False,
    }
    return after


def _submit_once(
    *,
    db_path: Path,
    plan: Mapping[str, Any],
    manifest_sha256: str,
    deployed_sha: str,
    approval_reference: str,
    backup: Mapping[str, Any],
) -> dict[str, Any]:
    target = dict(plan["target"])
    after_json = str(target["after_plan_json"])
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        _ensure_versions_schema(conn)
        existing = conn.execute(
            f"SELECT operation_id,status FROM {VERSIONS_TABLE} WHERE operation_id=? OR version_id=?",
            (str(plan["operation_id"]), str(plan["version_id"])),
        ).fetchall()
        if existing:
            raise HistoricalCostCarryForwardError(
                "operation_identity_not_fresh",
                "operation/version identity is already terminal; submit is not repeated",
            )
        current = conn.execute(
            """SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots
                WHERE bundle_version=? AND as_of_date=? AND snapshot_id=?""",
            (
                str(target["bundle_version"]),
                str(target["as_of_date"]),
                str(target["snapshot_id"]),
            ),
        ).fetchone()
        if current is None or _sha_text(str(current["plan_json"])) != str(target["before_plan_sha256"]):
            raise HistoricalCostCarryForwardError(
                "immediate_target_cas_drift",
                "canonical ready snapshot changed immediately before submit",
            )
        other_ready_snapshots_digest = _other_ready_snapshots_digest(
            conn,
            bundle_version=str(target["bundle_version"]),
            as_of_date=str(target["as_of_date"]),
            snapshot_id=str(target["snapshot_id"]),
        )
        if other_ready_snapshots_digest != str(
            plan["before"]["other_ready_snapshots_digest"]
        ):
            raise HistoricalCostCarryForwardError(
                "immediate_non_target_cas_drift",
                "another ready snapshot changed immediately before submit",
            )
        current_source = _select_prior_source(
            conn,
            business_date=str(plan["business_date"]),
            nm_id=int(plan["nm_id"]),
        )
        current_event_evidence = _prove_no_cost_changing_event(
            conn,
            source_date=str(current_source["business_date"]),
            business_date=str(plan["business_date"]),
            nm_id=int(plan["nm_id"]),
        )
        current_payload = _loads_object(current["plan_json"], "immediate target snapshot")
        current_cells = _ready_cells(current_payload, str(plan["business_date"]))
        current_costs, _warehouse, _scope = _persisted_formula_inputs(
            current_cells,
            business_date=str(plan["business_date"]),
            target_nm_id=int(plan["nm_id"]),
            source=current_source,
        )
        current_proxy3 = _load_proxy3(conn, str(plan["business_date"]))
        current_proxy4 = _load_proxy4(conn, str(plan["business_date"]))
        current_formula_inputs_digest = _digest(
            {
                "source": current_source,
                "event_evidence_digest": current_event_evidence["digest"],
                "costs_digest": _digest(current_costs),
                "proxy3": current_proxy3.public(),
                "proxy4": (
                    current_proxy4.public() if current_proxy4 is not None else None
                ),
            }
        )
        if (
            current_source != dict(plan["source"])
            or str(current_event_evidence["digest"])
            != str(plan["cost_event_evidence"]["digest"])
            or current_formula_inputs_digest != str(plan["formula"]["inputs_digest"])
        ):
            raise HistoricalCostCarryForwardError(
                "immediate_input_cas_drift",
                "source, cost-event or formula input changed immediately before submit",
            )
        conn.execute(
            f"""INSERT INTO {VERSIONS_TABLE}(
                   version_id,operation_id,status,business_date,nm_id,
                   source_business_date,source_unit_cost_rub,source_digest,
                   selection_method,event_evidence_digest,formula_inputs_digest,
                   target_bundle_version,target_as_of_date,target_snapshot_id,
                   before_plan_sha256,after_plan_sha256,non_target_digest,
                   other_ready_snapshots_digest,
                   manifest_sha256,deployed_sha,storage_generation_json,
                   approval_reference,backup_json,submit_count,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
            (
                str(plan["version_id"]),
                str(plan["operation_id"]),
                "accepted",
                str(plan["business_date"]),
                int(plan["nm_id"]),
                str(plan["source"]["business_date"]),
                str(plan["source"]["unit_cost_rub"]),
                str(plan["source"]["source_digest"]),
                SELECTION_METHOD,
                str(plan["cost_event_evidence"]["digest"]),
                str(plan["formula"]["inputs_digest"]),
                str(target["bundle_version"]),
                str(target["as_of_date"]),
                str(target["snapshot_id"]),
                str(target["before_plan_sha256"]),
                str(target["after_plan_sha256"]),
                str(plan["after"]["non_target_digest"]),
                other_ready_snapshots_digest,
                manifest_sha256,
                deployed_sha,
                _canonical_json(plan["storage_generation"]),
                approval_reference,
                _canonical_json(backup),
                str(plan["created_at"]),
            ),
        )
        changed = conn.execute(
            """UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?
                WHERE bundle_version=? AND as_of_date=? AND snapshot_id=? AND plan_json=?""",
            (
                after_json,
                str(target["bundle_version"]),
                str(target["as_of_date"]),
                str(target["snapshot_id"]),
                str(current["plan_json"]),
            ),
        )
        if changed.rowcount != 1:
            raise HistoricalCostCarryForwardError(
                "immediate_target_cas_failed", "ready snapshot CAS changed no exact row"
            )
        conn.commit()
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "apply",
        "status": "submitted",
        "database_written": True,
        "operation_id": str(plan["operation_id"]),
        "version_id": str(plan["version_id"]),
        "manifest_sha256": manifest_sha256,
        "deployed_sha": deployed_sha,
        "approval_reference": approval_reference,
        "submit_count": 1,
        "accepted_vitrina_version_count": 1,
        "updated_ready_snapshot_count": 1,
        "backup_path": str(backup["path"]),
        "backup_sha256": "sha256:" + str(backup["sha256"]),
        "backup_size_bytes": int(backup["size_bytes"]),
        "backup_integrity_check": str(backup["integrity_check"]),
        "root_storage_admission": backup.get("root_storage_admission"),
    }


def _readback(
    *,
    db_path: Path,
    operation_id: str,
    expected_plan: Mapping[str, Any] | None,
) -> dict[str, Any]:
    with _query_only_connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if VERSIONS_TABLE not in tables:
            return {"status": "not_submitted", "operation_id": operation_id, "submit_count": 0}
        rows = conn.execute(
            f"SELECT * FROM {VERSIONS_TABLE} WHERE operation_id=?",
            (operation_id,),
        ).fetchall()
        if len(rows) != 1:
            return {
                "status": "ambiguous",
                "operation_id": operation_id,
                "operation_row_count": len(rows),
            }
        version = dict(rows[0])
        snapshot = conn.execute(
            """SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots
                WHERE bundle_version=? AND as_of_date=? AND snapshot_id=?""",
            (
                version["target_bundle_version"],
                version["target_as_of_date"],
                version["target_snapshot_id"],
            ),
        ).fetchone()
        if snapshot is None:
            return {"status": "ambiguous", "operation_id": operation_id, "reason": "target_snapshot_missing"}
        payload = _loads_object(snapshot["plan_json"], "readback ready snapshot")
        cells = _ready_cells(payload, str(version["business_date"]))
        metrics = _metric_receipt(cells, nm_id=int(version["nm_id"]))
        non_target = _non_target_digest(
            payload,
            business_date=str(version["business_date"]),
            nm_id=int(version["nm_id"]),
        )
        other_ready_snapshots_digest = _other_ready_snapshots_digest(
            conn,
            bundle_version=str(version["target_bundle_version"]),
            as_of_date=str(version["target_as_of_date"]),
            snapshot_id=str(version["target_snapshot_id"]),
        )
        expected_after_sha = str(version["after_plan_sha256"])
        exact = (
            str(version["status"]) == "accepted"
            and int(version["submit_count"]) == 1
            and _sha_text(str(snapshot["plan_json"])) == expected_after_sha
            and non_target == str(version["non_target_digest"])
            and other_ready_snapshots_digest
            == str(version["other_ready_snapshots_digest"])
            and all(value not in {None, ""} for value in metrics["after_required_values"])
        )
        if expected_plan is not None:
            exact = exact and (
                str(expected_plan.get("version_id") or "") == str(version["version_id"])
                and str(expected_plan.get("material_digest") or "")
                == _digest(_material_plan(expected_plan))
                and str((expected_plan.get("target") or {}).get("after_plan_sha256") or "")
                == expected_after_sha
            )
        return {
            "status": "reconciled" if exact else "ambiguous",
            "query_only": True,
            "database_written": False,
            "operation_id": operation_id,
            "version_id": str(version["version_id"]),
            "version_status": str(version["status"]),
            "submit_count": int(version["submit_count"]),
            "business_date": str(version["business_date"]),
            "nm_id": int(version["nm_id"]),
            "source_business_date": str(version["source_business_date"]),
            "source_unit_cost_rub": str(version["source_unit_cost_rub"]),
            "source_digest": str(version["source_digest"]),
            "selection_method": str(version["selection_method"]),
            "after_plan_sha256": expected_after_sha,
            "non_target_digest": non_target,
            "other_ready_snapshots_digest": other_ready_snapshots_digest,
            "metrics": metrics,
            "accepted_vitrina_version_count": 1,
            "updated_ready_snapshot_count": 1,
            "runtime_controls_changed": False,
            "timer_change_count": 0,
        }


def _ensure_versions_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {VERSIONS_TABLE}(
            version_id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK(status='accepted'),
            business_date TEXT NOT NULL,
            nm_id INTEGER NOT NULL,
            source_business_date TEXT NOT NULL,
            source_unit_cost_rub TEXT NOT NULL,
            source_digest TEXT NOT NULL,
            selection_method TEXT NOT NULL,
            event_evidence_digest TEXT NOT NULL,
            formula_inputs_digest TEXT NOT NULL,
            target_bundle_version TEXT NOT NULL,
            target_as_of_date TEXT NOT NULL,
            target_snapshot_id TEXT NOT NULL,
            before_plan_sha256 TEXT NOT NULL,
            after_plan_sha256 TEXT NOT NULL,
            non_target_digest TEXT NOT NULL,
            other_ready_snapshots_digest TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL,
            deployed_sha TEXT NOT NULL,
            storage_generation_json TEXT NOT NULL,
            approval_reference TEXT NOT NULL,
            backup_json TEXT NOT NULL,
            submit_count INTEGER NOT NULL CHECK(submit_count=1),
            created_at TEXT NOT NULL,
            UNIQUE(business_date,nm_id,status)
        );
        CREATE TRIGGER IF NOT EXISTS historical_analytical_cost_version_update_forbidden
        BEFORE UPDATE ON {VERSIONS_TABLE}
        BEGIN SELECT RAISE(ABORT,'historical analytical cost versions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS historical_analytical_cost_version_delete_forbidden
        BEFORE DELETE ON {VERSIONS_TABLE}
        BEGIN SELECT RAISE(ABORT,'historical analytical cost versions are immutable'); END;
        """
    )


def _validate_reviewed_manifest(
    manifest: Mapping[str, Any],
    *,
    operation_id: str,
    business_date: str,
    nm_id: int,
    storage_generation: Mapping[str, Any],
) -> None:
    if (
        str(manifest.get("schema_version") or "") != SCHEMA_VERSION
        or str(manifest.get("status") or "") != "ready"
        or str(manifest.get("operation_id") or "") != operation_id
        or str(manifest.get("business_date") or "") != business_date
        or int(manifest.get("nm_id") or 0) != nm_id
        or dict(manifest.get("storage_generation") or {}) != dict(storage_generation)
        or str(manifest.get("material_digest") or "") != _digest(_material_plan(manifest))
    ):
        raise HistoricalCostCarryForwardError(
            "reviewed_manifest_invalid", "reviewed manifest identity/material is invalid"
        )


def _validate_before_shape(cells: Mapping[str, Any], *, nm_id: int) -> None:
    if _decimal(cells.get(f"SKU:{nm_id}|orderSum")) in {None, ZERO}:
        raise HistoricalCostCarryForwardError(
            "target_positive_orders_missing", "target SKU does not have positive order value"
        )
    if cells.get(f"SKU:{nm_id}|{OUR_WB_UNIT_COST_RUB_METRIC_KEY}") not in {None, ""}:
        raise HistoricalCostCarryForwardError(
            "target_cost_not_blank", "target own-cost cell is no longer blank"
        )
    missing = [
        key for key in ALLOWED_TOTAL_KEYS if cells.get(f"TOTAL|{key}") in {None, ""}
    ]
    if sorted(missing) != sorted(ALLOWED_TOTAL_KEYS):
        raise HistoricalCostCarryForwardError(
            "six_total_dependency_shape_drift",
            "target does not have the exact six missing TOTAL dependencies",
            details={"missing_metric_keys": missing},
        )


def _validate_after_shape(cells: Mapping[str, Any], *, nm_id: int) -> None:
    missing = [
        row_id
        for row_id in [
            *(f"SKU:{nm_id}|{key}" for key in SKU_FORMULA_KEYS),
            *(f"TOTAL|{key}" for key in ALLOWED_TOTAL_KEYS),
        ]
        if cells.get(row_id) in {None, ""}
    ]
    if missing:
        raise HistoricalCostCarryForwardError(
            "formula_dependency_still_missing",
            "canonical formula materialization left required cells blank",
            details={"missing_row_ids": missing},
        )


def _load_proxy3(conn: sqlite3.Connection, business_date: str) -> Any:
    row = conn.execute(
        """SELECT * FROM sheet_vitrina_v1_calculation_parameter_versions
            WHERE block_key=? AND effective_date<=?
            ORDER BY effective_date DESC,revision DESC,created_at DESC LIMIT 1""",
        (PROXY_BLOCK_KEY, business_date),
    ).fetchone()
    return DEFAULT_PROXY_PARAMETERS if row is None else _proxy3_parameters_from_row(row)


def _load_proxy4(conn: sqlite3.Connection, business_date: str) -> Any:
    if business_date < INVENTORY_COST_BLEND_EFFECTIVE_DATE:
        return None
    row = conn.execute(
        """SELECT * FROM sheet_vitrina_v1_proxy_v4_parameter_versions
            WHERE block_key=? AND effective_date<=?
            ORDER BY effective_date DESC,revision DESC,created_at DESC LIMIT 1""",
        (PROXY_V4_BLOCK_KEY, business_date),
    ).fetchone()
    if row is None:
        raise HistoricalCostCarryForwardError(
            "proxy4_parameters_missing", "exact Proxy V4 parameters are missing"
        )
    return _proxy4_parameters_from_row(row)


def _metric_receipt(cells: Mapping[str, Any], *, nm_id: int) -> dict[str, Any]:
    sku = {key: cells.get(f"SKU:{nm_id}|{key}") for key in SKU_FORMULA_KEYS}
    total = {key: cells.get(f"TOTAL|{key}") for key in ALLOWED_TOTAL_KEYS}
    return {
        "sku": sku,
        "total": total,
        "after_required_values": [*sku.values(), *total.values()],
    }


def _non_target_digest(payload: Mapping[str, Any], *, business_date: str, nm_id: int) -> str:
    normalized = deepcopy(dict(payload))
    rows = _data_rows(normalized)
    dates = [str(item) for item in normalized.get("date_columns") or []]
    if business_date not in dates:
        raise HistoricalCostCarryForwardError(
            "target_date_column_missing", "non-target digest cannot find exact date"
        )
    index = dates.index(business_date) + 2
    allowed = {
        *(f"SKU:{nm_id}|{key}" for key in SKU_FORMULA_KEYS),
        *(f"TOTAL|{key}" for key in ALLOWED_TOTAL_KEYS),
    }
    for row_id in allowed:
        if row_id in rows and len(rows[row_id]) > index:
            rows[row_id][index] = "<target-cell>"
    metadata = normalized.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("historical_analytical_cost_carry_forward", None)
    return _digest(normalized)


def _other_ready_snapshots_digest(
    conn: sqlite3.Connection,
    *,
    bundle_version: str,
    as_of_date: str,
    snapshot_id: str,
) -> str:
    rows = [
        {
            "bundle_version": str(row["bundle_version"]),
            "activated_at": str(row["activated_at"]),
            "as_of_date": str(row["as_of_date"]),
            "snapshot_id": str(row["snapshot_id"]),
            "plan_version": str(row["plan_version"]),
            "refreshed_at": str(row["refreshed_at"]),
            "plan_sha256": _sha_text(str(row["plan_json"])),
        }
        for row in conn.execute(
            """SELECT bundle_version,activated_at,as_of_date,snapshot_id,
                      plan_version,refreshed_at,plan_json
                 FROM sheet_vitrina_v1_ready_snapshots
                WHERE NOT (bundle_version=? AND as_of_date=? AND snapshot_id=?)
                ORDER BY bundle_version,as_of_date,snapshot_id""",
            (bundle_version, as_of_date, snapshot_id),
        ).fetchall()
    ]
    return _digest(rows)


def _data_rows(payload: Mapping[str, Any]) -> dict[str, list[Any]]:
    sheets = payload.get("sheets")
    if not isinstance(sheets, list):
        raise HistoricalCostCarryForwardError(
            "ready_snapshot_shape_invalid", "ready snapshot sheets are missing"
        )
    sheet = next(
        (
            item
            for item in sheets
            if isinstance(item, Mapping)
            and str(item.get("sheet_name") or item.get("name") or "") == "DATA_VITRINA"
        ),
        None,
    )
    if not isinstance(sheet, Mapping) or not isinstance(sheet.get("rows"), list):
        raise HistoricalCostCarryForwardError(
            "ready_snapshot_shape_invalid", "DATA_VITRINA rows are missing"
        )
    result: dict[str, list[Any]] = {}
    for row in sheet["rows"]:
        if isinstance(row, list) and len(row) >= 2 and str(row[1] or ""):
            row_id = str(row[1])
            if row_id in result:
                raise HistoricalCostCarryForwardError(
                    "ready_snapshot_duplicate_row", "DATA_VITRINA row identity is duplicated"
                )
            result[row_id] = row
    return result


def _material_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(plan.get(key))
        for key in (
            "schema_version",
            "status",
            "operation_id",
            "version_id",
            "created_at",
            "selection_method",
            "business_date",
            "nm_id",
            "storage_generation",
            "target",
            "source",
            "cost_event_evidence",
            "formula",
            "before",
            "after",
            "expected_effect",
        )
    }


def _loads_object(value: Any, label: str) -> dict[str, Any]:
    try:
        parsed = value if isinstance(value, Mapping) else json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HistoricalCostCarryForwardError(
            "json_evidence_invalid", f"{label} is not valid JSON"
        ) from exc
    if not isinstance(parsed, Mapping):
        raise HistoricalCostCarryForwardError(
            "json_evidence_invalid", f"{label} is not a JSON object"
        )
    return deepcopy(dict(parsed))


def _decimal(value: Any) -> Decimal | None:
    if value in {None, ""}:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ensure_private_evidence_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir() or path.stat().st_mode & 0o777 != 0o700:
        raise HistoricalCostCarryForwardError(
            "private_evidence_directory_invalid",
            "evidence directory must be one regular private mode-0700 directory",
        )


def _plan_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _validate_target_binding(
    *,
    runtime_dir: Path,
    target_file: Path | None,
    expected_deployed_sha: str,
    deployed_sha_file: Path | None,
) -> dict[str, Any]:
    expected_sha = str(expected_deployed_sha or "").strip().lower()
    if target_file is None:
        if not expected_sha:
            return {"validated": False, "reason": "not_requested"}
        sha_file = (
            deployed_sha_file.expanduser().resolve()
            if deployed_sha_file is not None
            else runtime_dir.parent / "app" / ".wb-core-runtime-sha"
        )
        deployed_sha = _validate_exact_deployment(
            expected_deployed_sha=expected_sha,
            deployed_sha_file=sha_file,
        )
        return {"validated": True, "deployed_sha": deployed_sha}
    resolved_target_file = target_file.expanduser().resolve()
    target = load_hosted_runtime_target(resolved_target_file)
    target_runtime_dir = (
        Path(str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""))
        .expanduser()
        .resolve()
    )
    if not (
        target.target_id == ACTIVE_HOSTED_RUNTIME_TARGET_ID
        and target.target_status == "active"
        and target.target_role == "primary_live"
        and target.target_lifecycle == "current_live"
        and target_runtime_dir == runtime_dir
        and re.fullmatch(r"[0-9a-f]{40}", expected_sha)
    ):
        raise HistoricalCostCarryForwardError(
            "canonical_target_binding_invalid",
            "exact active hosted runtime target or deployed SHA is invalid",
        )
    marker_candidates = (
        runtime_dir / ".wb-core-runtime-sha",
        runtime_dir.parent / "app" / ".wb-core-runtime-sha",
    )
    actual_shas = {
        marker.read_text(encoding="utf-8").strip().lower()
        for marker in marker_candidates
        if marker.is_file()
    }
    if actual_shas != {expected_sha}:
        raise HistoricalCostCarryForwardError(
            "deployed_sha_marker_drift",
            "canonical deployed SHA markers do not match the exact release",
            details={"marker_count": len(actual_shas)},
        )
    return {
        "validated": True,
        "target_id": target.target_id,
        "target_status": target.target_status,
        "target_role": target.target_role,
        "target_lifecycle": target.target_lifecycle,
        "runtime_dir": str(runtime_dir),
        "target_file_sha256": _file_digest(resolved_target_file),
        "deployed_sha": expected_sha,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--business-date", required=True)
    parser.add_argument("--nm-id", type=int, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--readback", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--manifest-sha256", default="")
    parser.add_argument("--deployed-sha", default="")
    parser.add_argument("--deployed-sha-file", type=Path)
    parser.add_argument("--target-file", type=Path)
    parser.add_argument("--approval-reference", default="")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        if args.readback:
            result = readback(
                runtime_dir=args.runtime_dir,
                operation_id=args.operation_id,
                manifest_path=args.manifest,
                target_file=args.target_file,
                expected_deployed_sha=args.deployed_sha,
                deployed_sha_file=args.deployed_sha_file,
            )
        else:
            result = run(
                runtime_dir=args.runtime_dir,
                evidence_dir=args.evidence_dir,
                operation_id=args.operation_id,
                business_date=args.business_date,
                nm_id=args.nm_id,
                apply=bool(args.apply),
                manifest_path=args.manifest,
                expected_manifest_sha256=args.manifest_sha256,
                expected_deployed_sha=args.deployed_sha,
                approval_reference=args.approval_reference,
                deployed_sha_file=args.deployed_sha_file,
                target_file=args.target_file,
            )
        print(_canonical_json(result))
        return 0 if result.get("status") in {"ready", "submitted", "reconciled"} else 2
    except (HistoricalCostCarryForwardError, OSError, sqlite3.Error, ValueError) as exc:
        print(
            _canonical_json(
                {
                    "status": "blocked",
                    "code": str(getattr(exc, "code", "") or type(exc).__name__),
                    "message": str(exc),
                    "details": getattr(exc, "details", None),
                    "submit_count": 0,
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
