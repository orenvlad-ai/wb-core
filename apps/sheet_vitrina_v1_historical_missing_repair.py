"""Exact version-bound repair for the WBC0010 closed-date Vitrina incident.

The repair is intentionally narrower than an ordinary refresh.  It restores
only the six accepted cost/Proxy families (and their totals) from the verified
T1 before-image of the first destructive publication.  Every logical date is
bound to its exact canonical as_of-date source image while overlapping temporal
bundles are restored only from their own source rows.  The known-bad 2026-08-26 image
is excluded until its separate warehouse invariant has authoritative proof.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.ff_pool_dense_fbs import PRIVATE_PLAN_MAX_BYTES, _write_private  # noqa: E402
from apps.sheet_vitrina_v1_buyout_mature_backfill import (  # noqa: E402
    _file_digest,
    _query_only_connection,
    _require_evidence_outside_repo,
)
from apps.sheet_vitrina_v1_historical_cost_carry_forward import (  # noqa: E402
    _ensure_private_evidence_dir,
    _plan_timestamp,
    _validate_target_binding,
)
from packages.application.business_data_write_barrier import barrier_status  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.sheet_vitrina_v1_our_wb_costs import (  # noqa: E402
    OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
    OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY,
    OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_UNIT_COST_RUB_METRIC_KEY,
    TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY,
)
from packages.application.sheet_vitrina_v1_proxy_v4 import (  # noqa: E402
    PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY,
    PROXY_V4_MARGIN_PCT_METRIC_KEY,
    PROXY_V4_PROFIT_RUB_METRIC_KEY,
    PROXY_V4_TOTAL_MARGIN_PER_UNIT_RUB_METRIC_KEY,
    PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY,
    PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY,
)
from packages.application.warehouse_functional_economics_backfill import (  # noqa: E402
    HISTORICAL_REPAIR_METADATA_KEY,
)
from packages.application.warehouse_recovery_policy import (  # noqa: E402
    RecoveryState,
    WarehouseRecoveryRegistry,
    recovery_operation_id,
)
from packages.application.warehouse_sync_lock import (  # noqa: E402
    WarehouseSyncBusyError,
    warehouse_sync_lock,
)


SCHEMA_VERSION = "sheet_vitrina_v1_historical_missing_repair_v1"
MAX_PLAN_BYTES = 24_000_000
SOURCE_OPERATION_ID = "recovery_6b52b021d0d8302fdf87004487661709"
SOURCE_OPERATION_DIGEST = (
    "sha256:510138ca43f717751ebcbc85997bc66baec3f7c65bf89c041f52943a4eb59181"
)
SOURCE_MUTATION_KIND = "functional_economics_targeted_publication"
REPAIR_MUTATION_KIND = "functional_economics_historical_repair"
TARGET_DATES = (
    "2026-08-22",
    "2026-08-23",
    "2026-08-24",
    "2026-08-25",
    "2026-08-27",
    "2026-08-28",
    "2026-08-29",
)
EXCLUDED_DATE = "2026-08-26"
SKU_KEYS = (
    OUR_WB_UNIT_COST_RUB_METRIC_KEY,
    OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
    PROXY_V4_PROFIT_RUB_METRIC_KEY,
    PROXY_V4_MARGIN_PCT_METRIC_KEY,
    PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY,
)
TOTAL_KEYS = (
    TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY,
    OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY,
    PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY,
    PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY,
    PROXY_V4_TOTAL_MARGIN_PER_UNIT_RUB_METRIC_KEY,
)
TARGET_KEYS = frozenset((*SKU_KEYS, *TOTAL_KEYS))
PRESENTATION_KEYS = frozenset(
    (OUR_WB_UNIT_COST_RUB_METRIC_KEY, TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY)
)
EXPECTED_SKU_COUNT = 33
EXPECTED_LOGICAL_TARGETS_PER_DATE = EXPECTED_SKU_COUNT * len(SKU_KEYS) + len(
    TOTAL_KEYS
)
EXPECTED_SOURCE_SNAPSHOTS_PER_DATE = 2
EXPECTED_TARGET_SNAPSHOT_COUNT = 9
EXPECTED_LOGICAL_TARGET_COUNT = len(TARGET_DATES) * EXPECTED_LOGICAL_TARGETS_PER_DATE
EXPECTED_CURRENT_MISSING_PER_DATE = 186


class HistoricalMissingRepairError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details = details


def run(
    *,
    runtime_dir: Path,
    evidence_dir: Path,
    operation_id: str,
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
        raise HistoricalMissingRepairError(
            "operation_identity_invalid", "operation_id is missing or too long"
        )
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    if not runtime.db_path.is_file():
        raise HistoricalMissingRepairError(
            "canonical_store_missing", "canonical operational SQLite store is missing"
        )
    store_manifest = runtime.store_registry.load(require_files=True)
    if store_manifest.implicit:
        raise HistoricalMissingRepairError(
            "canonical_generation_implicit",
            "historical repair requires one explicit StoreRegistry generation",
        )
    generation = {
        "manifest_sha256": store_manifest.manifest_sha256,
        "generation_epoch": store_manifest.generation_epoch,
        "generation_id": store_manifest.operational.generation_id,
        "schema_revision": store_manifest.operational.schema_revision,
        "relative_path": store_manifest.operational.relative_path,
    }
    target_binding = _validate_target_binding(
        runtime_dir=runtime_dir,
        target_file=target_file,
        expected_deployed_sha=expected_deployed_sha,
        deployed_sha_file=deployed_sha_file,
    )
    timestamp = str(created_at or _utc_now())
    if not apply:
        plan = _build_plan(
            db_path=runtime.db_path,
            operation_id=operation,
            created_at=timestamp,
            storage_generation=generation,
        )
        _ensure_private_evidence_dir(evidence_dir)
        output = evidence_dir / (
            "historical-missing-repair-plan-" + _plan_timestamp(timestamp) + ".json"
        )
        written = _write_private(
            output,
            plan,
            owner="production_apply_evidence",
            max_output_bytes=MAX_PLAN_BYTES,
            require_private_parent=True,
            no_overwrite=True,
        )
        if not written.get("written"):
            raise HistoricalMissingRepairError(
                str(written.get("reason") or "private_plan_persistence_failed"),
                str(written.get("error") or "private plan persistence failed"),
                details=written,
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "dry-run",
            "status": "ready" if plan["would_change"] else "already_reconciled",
            "query_only": True,
            "database_written": False,
            "operation_id": operation,
            "manifest_path": str(output),
            "manifest_sha256": _file_digest(output),
            "material_qualification_digest": plan["material_digest"],
            "source_operation_id": SOURCE_OPERATION_ID,
            "source_digest": SOURCE_OPERATION_DIGEST,
            "target_dates": list(TARGET_DATES),
            "excluded_date": EXCLUDED_DATE,
            "logical_target_count": plan["counts"]["logical_target_count"],
            "persisted_cell_instance_count": plan["counts"][
                "persisted_cell_instance_count"
            ],
            "updated_ready_snapshot_count": plan["counts"]["snapshot_count"],
            "current_missing_count": plan["counts"]["current_missing_count"],
            "after_missing_count": plan["counts"]["after_missing_count"],
            "repair_signals_cleared": plan["counts"]["repair_signals_cleared"],
            "would_change": plan["would_change"],
            "non_target_digest": plan["before"]["non_target_digest"],
            "other_ready_snapshots_digest": plan["before"][
                "other_ready_snapshots_digest"
            ],
            "storage_generation": generation,
            "target_binding": target_binding,
            "target_generation_bound": True,
            "barrier_inactive": barrier_status(runtime_dir).get("active") is False,
            "timer_change_count": 0,
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
        }

    if (
        manifest_path is None
        or not expected_manifest_sha256
        or not expected_deployed_sha
        or not approval_reference
    ):
        raise HistoricalMissingRepairError(
            "apply_identity_incomplete",
            "apply requires manifest, manifest SHA, deployed SHA and approval reference",
        )
    reviewed_path = manifest_path.expanduser().resolve()
    reviewed_sha = _file_digest(reviewed_path)
    if reviewed_sha != str(expected_manifest_sha256):
        raise HistoricalMissingRepairError(
            "manifest_digest_mismatch", "reviewed manifest SHA-256 mismatch"
        )
    reviewed = json.loads(reviewed_path.read_text(encoding="utf-8"))
    _validate_reviewed_manifest(reviewed, operation_id=operation, generation=generation)
    before_barrier = barrier_status(runtime_dir)
    if before_barrier.get("active") is not False:
        raise HistoricalMissingRepairError(
            "write_barrier_active", "business-data write barrier is active"
        )
    try:
        with warehouse_sync_lock(runtime_dir, blocking=False):
            under_lock_barrier = barrier_status(runtime_dir)
            if (
                under_lock_barrier.get("active") is not False
                or under_lock_barrier.get("status") != before_barrier.get("status")
            ):
                raise HistoricalMissingRepairError(
                    "write_barrier_drift",
                    "business-data write barrier changed at the shared-lock boundary",
                )
            fresh = _build_plan(
                db_path=runtime.db_path,
                operation_id=operation,
                created_at=str(reviewed["created_at"]),
                storage_generation=generation,
            )
            if fresh["material_digest"] != reviewed["material_digest"]:
                raise HistoricalMissingRepairError(
                    "jit_material_cas_drift",
                    "target or verified source changed after qualification",
                )
            if not fresh["would_change"]:
                raise HistoricalMissingRepairError(
                    "operation_already_reconciled",
                    "reviewed mutation became a no-op before its single submit",
                )
            result = _submit_once(
                runtime=runtime,
                plan=fresh,
                manifest_sha256=reviewed_sha,
                deployed_sha=str(target_binding.get("deployed_sha") or ""),
                approval_reference=str(approval_reference),
            )
    except WarehouseSyncBusyError as exc:
        raise HistoricalMissingRepairError(
            "shared_writer_busy",
            "canonical warehouse/ready-snapshot writer is busy; submit_count remains zero",
        ) from exc
    readback = _readback(
        db_path=runtime.db_path,
        runtime_dir=runtime_dir,
        operation_id=operation,
        expected_plan=reviewed,
    )
    if readback["status"] != "reconciled":
        raise HistoricalMissingRepairError(
            "post_submit_reconciliation_failed",
            "query-only post-submit reconciliation did not match the reviewed operation",
            details=readback,
        )
    receipt = {
        **result,
        "readback": readback,
        "barrier_before": before_barrier,
        "barrier_under_lock": under_lock_barrier,
        "runtime_controls_changed": False,
    }
    _ensure_private_evidence_dir(evidence_dir)
    receipt_path = evidence_dir / f"historical-missing-repair-receipt-{operation}.json"
    written = _write_private(
        receipt_path,
        receipt,
        owner="production_apply_evidence",
        max_output_bytes=PRIVATE_PLAN_MAX_BYTES,
        require_private_parent=True,
        no_overwrite=True,
    )
    if not written.get("written"):
        raise HistoricalMissingRepairError(
            str(written.get("reason") or "private_receipt_persistence_failed"),
            str(written.get("error") or "private receipt persistence failed"),
            details=written,
        )
    return {**receipt, "receipt_path": str(receipt_path), "receipt_sha256": _file_digest(receipt_path)}


def readback(
    *,
    runtime_dir: Path,
    operation_id: str,
    manifest_path: Path,
    target_file: Path | None = None,
    expected_deployed_sha: str = "",
    deployed_sha_file: Path | None = None,
) -> dict[str, Any]:
    runtime_dir = runtime_dir.expanduser().resolve()
    _validate_target_binding(
        runtime_dir=runtime_dir,
        target_file=target_file,
        expected_deployed_sha=expected_deployed_sha,
        deployed_sha_file=deployed_sha_file,
    )
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    expected = json.loads(manifest_path.expanduser().resolve().read_text(encoding="utf-8"))
    return _readback(
        db_path=runtime.db_path,
        runtime_dir=runtime_dir,
        operation_id=str(operation_id),
        expected_plan=expected,
    )


def _build_plan(
    *,
    db_path: Path,
    operation_id: str,
    created_at: str,
    storage_generation: Mapping[str, Any],
) -> dict[str, Any]:
    with _query_only_connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        source_operation = _source_operation(conn)
        source_rows = _source_rows(conn)
        source_by_date, source_bindings = _bind_source(source_rows)
        target_identities = {
            (str(binding["bundle_version"]), str(binding["as_of_date"]), str(binding["snapshot_id"]))
            for binding in source_bindings
        }
        current_rows = _current_rows(conn, target_identities)
        if len(current_rows) != EXPECTED_TARGET_SNAPSHOT_COUNT:
            raise HistoricalMissingRepairError(
                "target_snapshot_count_invalid",
                "current exact target snapshots do not match the reviewed source set",
                details={"expected": EXPECTED_TARGET_SNAPSHOT_COUNT, "actual": len(current_rows)},
            )
        patches: list[dict[str, Any]] = []
        logical_before: dict[str, dict[str, Any]] = {day: {} for day in TARGET_DATES}
        logical_after: dict[str, dict[str, Any]] = {day: {} for day in TARGET_DATES}
        missing_before = 0
        missing_after = 0
        repair_signals_cleared = 0
        before_non_target: list[dict[str, Any]] = []
        after_non_target: list[dict[str, Any]] = []
        bindings_by_identity: dict[
            tuple[str, str, str], list[dict[str, Any]]
        ] = {}
        for binding in source_bindings:
            identity = (
                str(binding["bundle_version"]),
                str(binding["as_of_date"]),
                str(binding["snapshot_id"]),
            )
            bindings_by_identity.setdefault(identity, []).append(binding)
        for identity, identity_bindings in sorted(bindings_by_identity.items()):
            current = current_rows.get(identity)
            if current is None:
                raise HistoricalMissingRepairError(
                    "target_snapshot_missing", "one exact ready snapshot disappeared"
                )
            before_payload = _loads_object(current["plan_json"], "current ready snapshot")
            after_payload = deepcopy(before_payload)
            for source_binding in sorted(
                identity_bindings, key=lambda item: str(item["target_date"])
            ):
                day = str(source_binding["target_date"])
                before_cells = _target_cells(before_payload, day)
                before_registry = _repair_dates(before_payload)
                after_payload = _repair_payload(
                    before_payload=after_payload,
                    source={
                        "cells": source_binding["cells"],
                        "functional_version_id": source_binding[
                            "functional_version_id"
                        ],
                        "coverage": source_binding["coverage"],
                        "date_evidence": source_binding["date_evidence"],
                        "presentation": source_binding["presentation"],
                    },
                    business_date=day,
                )
                after_cells = _target_cells(after_payload, day)
                if identity[1] == day:
                    logical_before[day] = deepcopy(before_cells)
                    logical_after[day] = deepcopy(after_cells)
                missing_before += sum(
                    _is_missing(value) for value in before_cells.values()
                )
                missing_after += sum(
                    _is_missing(value) for value in after_cells.values()
                )
                if day in before_registry and day not in _repair_dates(after_payload):
                    repair_signals_cleared += 1
            before_stripped = deepcopy(before_payload)
            after_stripped = deepcopy(after_payload)
            repaired_dates = sorted(
                str(item["target_date"]) for item in identity_bindings
            )
            for day in repaired_dates:
                before_stripped = _strip_exact_target(before_stripped, day)
                after_stripped = _strip_exact_target(after_stripped, day)
            if before_stripped != after_stripped:
                raise HistoricalMissingRepairError(
                    "non_target_candidate_drift",
                    "candidate changes data outside the exact date/family/evidence closure",
                    details={"identity": identity, "business_dates": repaired_dates},
                )
            before_json = _canonical_json(before_payload)
            after_json = _canonical_json(after_payload)
            patches.append(
                {
                    "bundle_version": identity[0],
                    "as_of_date": identity[1],
                    "snapshot_id": identity[2],
                    "business_dates": repaired_dates,
                    "before_plan_sha256": _sha_text(before_json),
                    "after_plan_sha256": _sha_text(after_json),
                    "after_plan_json": after_json,
                    "would_change": before_json != after_json,
                }
            )
            before_non_target.append({"identity": identity, "payload": before_stripped})
            after_non_target.append({"identity": identity, "payload": after_stripped})
        for day in TARGET_DATES:
            if len(logical_before[day]) != EXPECTED_LOGICAL_TARGETS_PER_DATE:
                raise HistoricalMissingRepairError(
                    "logical_target_count_invalid",
                    "one date does not contain the exact 33-SKU plus TOTAL closure",
                    details={"business_date": day, "count": len(logical_before[day])},
                )
            actual_missing = sum(_is_missing(value) for value in logical_before[day].values())
            if actual_missing not in {0, EXPECTED_CURRENT_MISSING_PER_DATE}:
                raise HistoricalMissingRepairError(
                    "current_missing_shape_invalid",
                    "one date is neither the exact damaged image nor an exact repaired no-op",
                    details={"business_date": day, "missing": actual_missing},
                )
            if any(_is_missing(value) for value in logical_after[day].values()):
                raise HistoricalMissingRepairError(
                    "source_target_missing",
                    "verified source still contains a missing accepted target",
                    details={"business_date": day},
                )
        other_digest = _other_ready_snapshots_digest(conn, target_identities)
        logical_targets = [
            {
                "business_date": day,
                "row_id": row_id,
                "before": logical_before[day][row_id],
                "after": logical_after[day][row_id],
                "value_type": _value_type(logical_after[day][row_id]),
                "source_functional_version_id": source_by_date[day]["functional_version_id"],
                "source_sha256": _digest(
                    {"business_date": day, "row_id": row_id, "value": logical_after[day][row_id]}
                ),
            }
            for day in TARGET_DATES
            for row_id in sorted(logical_after[day])
        ]
        before_non_target_digest = _digest(before_non_target)
        after_non_target_digest = _digest(after_non_target)
        if before_non_target_digest != after_non_target_digest:
            raise HistoricalMissingRepairError(
                "non_target_digest_drift", "candidate non-target digest changed"
            )
        would_change = any(item["would_change"] for item in patches)
        plan: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "status": "ready" if would_change else "already_reconciled",
            "operation_id": operation_id,
            "created_at": created_at,
            "would_change": would_change,
            "storage_generation": dict(storage_generation),
            "source": {
                "operation_id": SOURCE_OPERATION_ID,
                "operation_kind": SOURCE_MUTATION_KIND,
                "source_digest": SOURCE_OPERATION_DIGEST,
                "lifecycle": str(source_operation["lifecycle_state"]),
                "tier": str(source_operation["tier"]),
                "undo_row_count": int(source_operation["undo_row_count"]),
                "bindings": source_bindings,
            },
            "target_dates": list(TARGET_DATES),
            "excluded": {
                "business_date": EXCLUDED_DATE,
                "reason": "unresolved_exact_functional_cost_coverage_invariant",
                "nm_id": 428853741,
            },
            "counts": {
                "date_count": len(TARGET_DATES),
                "sku_count": EXPECTED_SKU_COUNT,
                "metric_family_count": len(SKU_KEYS),
                "total_key_count": len(TOTAL_KEYS),
                "logical_target_count": len(logical_targets),
                "snapshot_count": len(patches),
                "persisted_cell_instance_count": len(source_bindings)
                * EXPECTED_LOGICAL_TARGETS_PER_DATE,
                "current_missing_count": missing_before // EXPECTED_SOURCE_SNAPSHOTS_PER_DATE,
                "after_missing_count": missing_after // EXPECTED_SOURCE_SNAPSHOTS_PER_DATE,
                "repair_signals_cleared": repair_signals_cleared,
            },
            "logical_targets": logical_targets,
            "patches": patches,
            "before": {
                "non_target_digest": before_non_target_digest,
                "other_ready_snapshots_digest": other_digest,
            },
            "after": {
                "non_target_digest": after_non_target_digest,
                "other_ready_snapshots_digest": other_digest,
            },
            "expected_effect": {
                "submit_count": 1 if would_change else 0,
                "updated_ready_snapshot_count": len(patches) if would_change else 0,
                "source_facts_changed": 0,
                "functional_versions_changed": 0,
                "finance_rows_changed": 0,
                "runtime_controls_changed": False,
            },
        }
        plan["plan_fingerprint"] = _digest(_material_plan(plan))
        plan["material_digest"] = _digest(_material_plan(plan))
        return plan


def _source_operation(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute(
        """SELECT operation_id,operation_kind,tier,lifecycle_state,source_digest,
                  (SELECT COUNT(*) FROM sheet_vitrina_v1_recovery_undo_rows u
                    WHERE u.operation_id=o.operation_id) AS undo_row_count
             FROM sheet_vitrina_v1_recovery_operations o WHERE operation_id=?""",
        (SOURCE_OPERATION_ID,),
    ).fetchone()
    if row is None:
        raise HistoricalMissingRepairError(
            "source_operation_missing", "verified source recovery operation is missing"
        )
    if not (
        str(row["operation_kind"]) == SOURCE_MUTATION_KIND
        and str(row["tier"]) == "T1"
        and str(row["lifecycle_state"]) == RecoveryState.RETAINED.value
        and str(row["source_digest"]) == SOURCE_OPERATION_DIGEST
        and int(row["undo_row_count"]) == 18
    ):
        raise HistoricalMissingRepairError(
            "source_operation_identity_drift",
            "verified source operation no longer matches its immutable identity",
            details=dict(row),
        )
    return row


def _source_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT sequence_no,key_json,before_json,status
             FROM sheet_vitrina_v1_recovery_undo_rows
            WHERE operation_id=? ORDER BY sequence_no""",
        (SOURCE_OPERATION_ID,),
    ).fetchall()
    if len(rows) != 18 or any(str(row["status"]) != "verified" for row in rows):
        raise HistoricalMissingRepairError(
            "source_undo_identity_drift", "source undo journal is incomplete or unverified"
        )
    return [dict(row) for row in rows]


def _bind_source(
    source_rows: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    source_by_date: dict[str, dict[str, Any]] = {}
    bindings: list[dict[str, Any]] = []
    seen_per_date = {day: 0 for day in TARGET_DATES}
    for row in source_rows:
        before = _loads_object(row["before_json"], "source undo before image")
        plan = _loads_object(before.get("plan_json"), "source ready snapshot")
        for day in TARGET_DATES:
            if day not in list(plan.get("date_columns") or []):
                continue
            cells = _target_cells(plan, day)
            if len(cells) != EXPECTED_LOGICAL_TARGETS_PER_DATE or any(
                _is_missing(value) for value in cells.values()
            ):
                raise HistoricalMissingRepairError(
                    "source_target_shape_invalid",
                    "verified source does not contain the complete target closure",
                    details={"business_date": day, "count": len(cells)},
                )
            metadata = _metadata(plan)
            coverage = deepcopy(
                _mapping(metadata.get("warehouse_history_coverage"), "source coverage").get(day)
            )
            marker = _mapping(metadata.get("functional_economics_backfill"), "source marker")
            publication = _mapping(marker.get("inventory_cost_publication"), "source publication")
            evidence = deepcopy(
                _mapping(publication.get("date_evidence"), "source date evidence").get(day)
            )
            if not isinstance(coverage, Mapping) or not isinstance(evidence, Mapping):
                raise HistoricalMissingRepairError(
                    "source_evidence_missing",
                    "verified source lacks exact date-bound coverage/evidence",
                    details={"business_date": day},
                )
            functional_version_id = str(
                coverage.get("functional_version_id")
                or evidence.get("functional_version_id")
                or ""
            )
            if not functional_version_id.startswith("whfv_"):
                raise HistoricalMissingRepairError(
                    "source_functional_version_invalid",
                    "verified source functional version is missing",
                    details={"business_date": day},
                )
            presentation = _presentation_for_day(plan, day)
            canonical_candidate = {
                "business_date": day,
                "functional_version_id": functional_version_id,
                "cells": cells,
            }
            source_material = {
                **canonical_candidate,
                "coverage": coverage,
                "date_evidence": evidence,
                "presentation": presentation,
            }
            canonical_candidate["material_digest"] = _digest(canonical_candidate)
            if str(before.get("as_of_date") or "") == day:
                if day in source_by_date:
                    raise HistoricalMissingRepairError(
                        "source_canonical_ambiguous",
                        "one date has more than one canonical as_of-date source",
                        details={"business_date": day},
                    )
                source_by_date[day] = canonical_candidate
            seen_per_date[day] += 1
            bindings.append(
                {
                    "source_sequence_no": int(row["sequence_no"]),
                    "bundle_version": str(before["bundle_version"]),
                    "as_of_date": str(before["as_of_date"]),
                    "snapshot_id": str(before["snapshot_id"]),
                    "target_date": day,
                    "before_plan_sha256": _sha_text(str(before["plan_json"])),
                    "functional_version_id": functional_version_id,
                    "source_material_digest": _digest(source_material),
                    "cells": cells,
                    "coverage": coverage,
                    "date_evidence": evidence,
                    "presentation": presentation,
                }
            )
    if (
        any(count != EXPECTED_SOURCE_SNAPSHOTS_PER_DATE for count in seen_per_date.values())
        or set(source_by_date) != set(TARGET_DATES)
    ):
        raise HistoricalMissingRepairError(
            "source_snapshot_coverage_invalid",
            "each logical date must have two source bundles and one canonical as_of-date source",
            details={"counts": seen_per_date, "canonical_dates": sorted(source_by_date)},
        )
    return source_by_date, sorted(
        bindings, key=lambda item: (item["target_date"], item["as_of_date"])
    )


def _current_rows(
    conn: sqlite3.Connection, identities: set[tuple[str, str, str]]
) -> dict[tuple[str, str, str], dict[str, Any]]:
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for bundle_version, as_of_date, snapshot_id in sorted(identities):
        rows = conn.execute(
            """SELECT bundle_version,activated_at,as_of_date,snapshot_id,plan_version,
                      refreshed_at,plan_json
                 FROM sheet_vitrina_v1_ready_snapshots
                WHERE bundle_version=? AND as_of_date=? AND snapshot_id=?""",
            (bundle_version, as_of_date, snapshot_id),
        ).fetchall()
        if len(rows) != 1:
            raise HistoricalMissingRepairError(
                "target_snapshot_identity_ambiguous",
                "exact source-bound ready snapshot does not resolve to one current row",
                details={"identity": [bundle_version, as_of_date, snapshot_id], "count": len(rows)},
            )
        result[(bundle_version, as_of_date, snapshot_id)] = dict(rows[0])
    return result


def _repair_payload(
    *, before_payload: Mapping[str, Any], source: Mapping[str, Any], business_date: str
) -> dict[str, Any]:
    after = deepcopy(dict(before_payload))
    sheet = _data_sheet(after)
    header = list(sheet["header"])
    try:
        index = header.index(business_date)
    except ValueError as exc:
        raise HistoricalMissingRepairError(
            "target_date_column_missing", "current snapshot lacks exact target date"
        ) from exc
    source_cells = dict(source["cells"])
    seen: set[str] = set()
    for row in sheet["rows"]:
        if not isinstance(row, list) or len(row) < 2:
            continue
        row_id = str(row[1])
        if row_id not in source_cells:
            continue
        while len(row) <= index:
            row.append("")
        row[index] = deepcopy(source_cells[row_id])
        seen.add(row_id)
    if seen != set(source_cells):
        raise HistoricalMissingRepairError(
            "target_row_identity_drift", "current snapshot lacks exact source row identities"
        )
    metadata = _metadata(after)
    coverage = _mapping_mutable(metadata, "warehouse_history_coverage")
    coverage[business_date] = deepcopy(source["coverage"])
    marker = _mapping_mutable(metadata, "functional_economics_backfill")
    publication = _mapping_mutable(marker, "inventory_cost_publication")
    date_evidence = _mapping_mutable(publication, "date_evidence")
    date_evidence[business_date] = deepcopy(source["date_evidence"])
    presentation = _mapping_mutable(metadata, "server_cell_presentation")
    for row_id in sorted(source_cells):
        if row_id.partition("|")[2] not in PRESENTATION_KEYS:
            continue
        by_date = _mapping_mutable(presentation, row_id)
        if row_id in source["presentation"]:
            by_date[business_date] = deepcopy(source["presentation"][row_id])
        else:
            by_date.pop(business_date, None)
        if not by_date:
            presentation.pop(row_id, None)
    if not presentation:
        metadata.pop("server_cell_presentation", None)
    registry = metadata.get(HISTORICAL_REPAIR_METADATA_KEY)
    if registry is not None:
        registry = _mapping(registry, "historical repair registry")
        dates = dict(_mapping(registry.get("dates"), "historical repair dates"))
        dates.pop(business_date, None)
        if dates:
            metadata[HISTORICAL_REPAIR_METADATA_KEY] = {**dict(registry), "dates": dates}
        else:
            metadata.pop(HISTORICAL_REPAIR_METADATA_KEY, None)
    return after


def _submit_once(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    plan: Mapping[str, Any],
    manifest_sha256: str,
    deployed_sha: str,
    approval_reference: str,
) -> dict[str, Any]:
    registry = WarehouseRecoveryRegistry(runtime_dir=runtime.runtime_dir, db_path=runtime.db_path)
    fingerprint = str(plan["plan_fingerprint"])
    recovery_id = recovery_operation_id(REPAIR_MUTATION_KIND, fingerprint)
    before_images: list[dict[str, Any]] = []
    with _query_only_connection(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        for sequence, patch in enumerate(plan["patches"], start=1):
            row = conn.execute(
                """SELECT bundle_version,activated_at,as_of_date,snapshot_id,plan_version,
                          refreshed_at,plan_json
                     FROM sheet_vitrina_v1_ready_snapshots
                    WHERE bundle_version=? AND as_of_date=? AND snapshot_id=?""",
                (patch["bundle_version"], patch["as_of_date"], patch["snapshot_id"]),
            ).fetchone()
            if row is None or _sha_text(str(row["plan_json"])) != patch["before_plan_sha256"]:
                raise HistoricalMissingRepairError(
                    "pre_recovery_target_cas_drift", "target changed before T1 capture"
                )
            before = dict(row)
            after = dict(before)
            after["plan_json"] = str(patch["after_plan_json"])
            before_images.append(
                {
                    "table": "sheet_vitrina_v1_ready_snapshots",
                    "key": {
                        "bundle_version": patch["bundle_version"],
                        "as_of_date": patch["as_of_date"],
                        "snapshot_id": patch["snapshot_id"],
                    },
                    "before": before,
                    "after": after,
                    "sequence_no": sequence,
                }
            )
    recovery = registry.prepare_t1(
        mutation_kind=REPAIR_MUTATION_KIND,
        closure_kind="sku_date",
        plan_fingerprint=fingerprint,
        scope={
            "goal_operation_id": plan["operation_id"],
            "target_dates": list(TARGET_DATES),
            "excluded_date": EXCLUDED_DATE,
            "logical_target_count": EXPECTED_LOGICAL_TARGET_COUNT,
            "snapshot_count": EXPECTED_TARGET_SNAPSHOT_COUNT,
            "source_operation_id": SOURCE_OPERATION_ID,
        },
        before_images=before_images,
        expected_after_images=[item["after"] for item in before_images],
        source_digest=SOURCE_OPERATION_DIGEST,
        non_target_digest=str(plan["before"]["non_target_digest"]),
        read_bytes=len(_canonical_json(before_images).encode("utf-8")),
    )
    if str(recovery.get("operation_id")) != recovery_id:
        raise HistoricalMissingRepairError(
            "recovery_identity_mismatch", "T1 registry resolved an unexpected identity"
        )
    if recovery.get("lifecycle") == RecoveryState.VERIFIED.value:
        recovery = registry.begin_mutation(
            recovery_id, expected_source_digest=SOURCE_OPERATION_DIGEST
        )
    if recovery.get("lifecycle") != RecoveryState.MUTATION_RUNNING.value:
        raise HistoricalMissingRepairError(
            "recovery_not_ready", "T1 recovery is not ready for the single mutation"
        )
    try:
        with sqlite3.connect(runtime.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            if _other_ready_snapshots_digest(
                conn,
                {
                    (str(item["bundle_version"]), str(item["as_of_date"]), str(item["snapshot_id"]))
                    for item in plan["patches"]
                },
            ) != str(plan["before"]["other_ready_snapshots_digest"]):
                raise HistoricalMissingRepairError(
                    "immediate_non_target_cas_drift",
                    "another ready snapshot changed immediately before submit",
                )
            for patch in plan["patches"]:
                current = conn.execute(
                    """SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots
                        WHERE bundle_version=? AND as_of_date=? AND snapshot_id=?""",
                    (patch["bundle_version"], patch["as_of_date"], patch["snapshot_id"]),
                ).fetchone()
                if current is None or _sha_text(str(current["plan_json"])) != patch["before_plan_sha256"]:
                    raise HistoricalMissingRepairError(
                        "immediate_target_cas_drift", "one exact target changed before submit"
                    )
                changed = conn.execute(
                    """UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?
                        WHERE bundle_version=? AND as_of_date=? AND snapshot_id=? AND plan_json=?""",
                    (
                        patch["after_plan_json"],
                        patch["bundle_version"],
                        patch["as_of_date"],
                        patch["snapshot_id"],
                        str(current["plan_json"]),
                    ),
                )
                if changed.rowcount != 1:
                    raise HistoricalMissingRepairError(
                        "immediate_target_cas_failed", "one exact target CAS changed no row"
                    )
            conn.commit()
        recovery = registry.retain(
            recovery_id,
            after_digest=str(plan["material_digest"]),
            non_target_digest=str(plan["after"]["non_target_digest"]),
        )
    except Exception as exc:
        registry.fail_recoverable(
            recovery_id,
            error=str(exc),
            next_action="query_only_reconcile_before_any_owner_decision",
        )
        raise
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "apply",
        "status": "submitted",
        "database_written": True,
        "operation_id": str(plan["operation_id"]),
        "recovery_operation_id": recovery_id,
        "recovery_lifecycle": str(recovery["lifecycle"]),
        "manifest_sha256": manifest_sha256,
        "deployed_sha": deployed_sha,
        "approval_reference": approval_reference,
        "submit_count": 1,
        "updated_ready_snapshot_count": EXPECTED_TARGET_SNAPSHOT_COUNT,
        "logical_target_count": EXPECTED_LOGICAL_TARGET_COUNT,
        "source_operation_id": SOURCE_OPERATION_ID,
        "excluded_date": EXCLUDED_DATE,
    }


def _readback(
    *,
    db_path: Path,
    runtime_dir: Path,
    operation_id: str,
    expected_plan: Mapping[str, Any],
) -> dict[str, Any]:
    with _query_only_connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        target_identities = {
            (str(item["bundle_version"]), str(item["as_of_date"]), str(item["snapshot_id"]))
            for item in expected_plan["patches"]
        }
        exact = True
        missing = 0
        active_repair_dates: set[str] = set()
        for patch in expected_plan["patches"]:
            row = conn.execute(
                """SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots
                    WHERE bundle_version=? AND as_of_date=? AND snapshot_id=?""",
                (patch["bundle_version"], patch["as_of_date"], patch["snapshot_id"]),
            ).fetchone()
            if row is None or _sha_text(str(row["plan_json"])) != str(patch["after_plan_sha256"]):
                exact = False
                continue
            payload = _loads_object(row["plan_json"], "readback ready snapshot")
            for business_date in patch["business_dates"]:
                cells = _target_cells(payload, str(business_date))
                missing += sum(_is_missing(value) for value in cells.values())
            active_repair_dates.update(_repair_dates(payload).keys())
        other_digest = _other_ready_snapshots_digest(conn, target_identities)
        recovery_id = recovery_operation_id(
            REPAIR_MUTATION_KIND, str(expected_plan["plan_fingerprint"])
        )
        recovery = WarehouseRecoveryRegistry(
            runtime_dir=runtime_dir, db_path=db_path
        ).get_operation(recovery_id)
        exact = bool(
            exact
            and missing == 0
            and not (set(TARGET_DATES) & active_repair_dates)
            and other_digest == str(expected_plan["after"]["other_ready_snapshots_digest"])
            and isinstance(recovery, Mapping)
            and recovery.get("lifecycle") == RecoveryState.RETAINED.value
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "reconciled" if exact else "ambiguous",
            "query_only": True,
            "database_written": False,
            "operation_id": operation_id,
            "recovery_operation_id": recovery_id,
            "recovery_lifecycle": recovery.get("lifecycle") if isinstance(recovery, Mapping) else None,
            "submit_count": 1 if isinstance(recovery, Mapping) else 0,
            "target_dates": list(TARGET_DATES),
            "excluded_date": EXCLUDED_DATE,
            "logical_target_count": EXPECTED_LOGICAL_TARGET_COUNT,
            "persisted_cell_instance_count": EXPECTED_LOGICAL_TARGET_COUNT
            * EXPECTED_SOURCE_SNAPSHOTS_PER_DATE,
            "updated_ready_snapshot_count": EXPECTED_TARGET_SNAPSHOT_COUNT,
            "after_missing_count": missing // EXPECTED_SOURCE_SNAPSHOTS_PER_DATE,
            "active_target_repair_dates": sorted(set(TARGET_DATES) & active_repair_dates),
            "other_ready_snapshots_digest": other_digest,
            "source_operation_id": SOURCE_OPERATION_ID,
            "source_digest": SOURCE_OPERATION_DIGEST,
            "runtime_controls_changed": False,
            "timer_change_count": 0,
        }


def _validate_reviewed_manifest(
    plan: Mapping[str, Any], *, operation_id: str, generation: Mapping[str, Any]
) -> None:
    if not (
        plan.get("schema_version") == SCHEMA_VERSION
        and plan.get("operation_id") == operation_id
        and plan.get("would_change") is True
        and plan.get("storage_generation") == dict(generation)
        and plan.get("target_dates") == list(TARGET_DATES)
        and (plan.get("source") or {}).get("operation_id") == SOURCE_OPERATION_ID
        and (plan.get("source") or {}).get("source_digest") == SOURCE_OPERATION_DIGEST
        and (plan.get("counts") or {}).get("logical_target_count") == EXPECTED_LOGICAL_TARGET_COUNT
        and (plan.get("counts") or {}).get("snapshot_count") == EXPECTED_TARGET_SNAPSHOT_COUNT
        and plan.get("material_digest") == _digest(_material_plan(plan))
        and plan.get("plan_fingerprint") == _digest(_material_plan(plan))
    ):
        raise HistoricalMissingRepairError(
            "reviewed_manifest_invalid", "reviewed manifest escaped the exact repair contract"
        )


def _target_cells(payload: Mapping[str, Any], business_date: str) -> dict[str, Any]:
    sheet = _data_sheet(payload)
    header = list(sheet["header"])
    try:
        index = header.index(business_date)
    except ValueError as exc:
        raise HistoricalMissingRepairError(
            "target_date_column_missing", "ready snapshot lacks exact target date"
        ) from exc
    result: dict[str, Any] = {}
    for row in sheet["rows"]:
        if not isinstance(row, list) or len(row) < 2:
            continue
        row_id = str(row[1])
        metric_key = row_id.partition("|")[2]
        if metric_key in TARGET_KEYS:
            result[row_id] = deepcopy(row[index] if index < len(row) else "")
    return result


def _data_sheet(payload: Mapping[str, Any]) -> dict[str, Any]:
    sheets = payload.get("sheets")
    if not isinstance(sheets, list) or not sheets:
        raise HistoricalMissingRepairError("ready_shape_invalid", "ready snapshot sheets are invalid")
    for sheet in sheets:
        if isinstance(sheet, dict) and "header" in sheet and "rows" in sheet:
            return sheet
    raise HistoricalMissingRepairError("data_sheet_missing", "DATA_VITRINA sheet is missing")


def _metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise HistoricalMissingRepairError("metadata_invalid", "ready metadata is not an object")
    return metadata


def _presentation_for_day(payload: Mapping[str, Any], day: str) -> dict[str, Any]:
    raw = _metadata(payload).get("server_cell_presentation")
    if raw is None:
        return {}
    presentation = _mapping(raw, "source presentation")
    result: dict[str, Any] = {}
    for row_id, by_date in presentation.items():
        if str(row_id).partition("|")[2] not in PRESENTATION_KEYS:
            continue
        entry = _mapping(by_date, "source row presentation").get(day)
        if entry is not None:
            result[str(row_id)] = deepcopy(entry)
    return result


def _repair_dates(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw = _metadata(payload).get(HISTORICAL_REPAIR_METADATA_KEY)
    if raw is None:
        return {}
    return dict(_mapping(_mapping(raw, "repair registry").get("dates"), "repair dates"))


def _strip_exact_target(payload: Mapping[str, Any], day: str) -> dict[str, Any]:
    stripped = deepcopy(dict(payload))
    sheet = _data_sheet(stripped)
    index = list(sheet["header"]).index(day)
    for row in sheet["rows"]:
        if isinstance(row, list) and len(row) > 1 and str(row[1]).partition("|")[2] in TARGET_KEYS:
            while len(row) <= index:
                row.append("")
            row[index] = "__TARGET_CELL__"
    metadata = _metadata(stripped)
    coverage = metadata.get("warehouse_history_coverage")
    if isinstance(coverage, dict):
        coverage.pop(day, None)
    marker = metadata.get("functional_economics_backfill")
    if isinstance(marker, dict):
        publication = marker.get("inventory_cost_publication")
        if isinstance(publication, dict) and isinstance(publication.get("date_evidence"), dict):
            publication["date_evidence"].pop(day, None)
    presentation = metadata.get("server_cell_presentation")
    if isinstance(presentation, dict):
        for row_id in list(presentation):
            if str(row_id).partition("|")[2] not in PRESENTATION_KEYS:
                continue
            by_date = presentation.get(row_id)
            if isinstance(by_date, dict):
                by_date.pop(day, None)
                if not by_date:
                    presentation.pop(row_id, None)
        if not presentation:
            metadata.pop("server_cell_presentation", None)
    registry = metadata.get(HISTORICAL_REPAIR_METADATA_KEY)
    if isinstance(registry, dict) and isinstance(registry.get("dates"), dict):
        registry["dates"].pop(day, None)
        if not registry["dates"]:
            metadata.pop(HISTORICAL_REPAIR_METADATA_KEY, None)
    return stripped


def _other_ready_snapshots_digest(
    conn: sqlite3.Connection, excluded: set[tuple[str, str, str]]
) -> str:
    rows = conn.execute(
        """SELECT bundle_version,as_of_date,snapshot_id,plan_json
             FROM sheet_vitrina_v1_ready_snapshots
            ORDER BY bundle_version,as_of_date,snapshot_id"""
    ).fetchall()
    return _digest(
        [
            {
                "identity": [str(row[0]), str(row[1]), str(row[2])],
                "plan_sha256": _sha_text(str(row[3])),
            }
            for row in rows
            if (str(row[0]), str(row[1]), str(row[2])) not in excluded
        ]
    )


def _material_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(value)
        for key, value in plan.items()
        if key not in {"created_at", "material_digest", "plan_fingerprint"}
    }


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalMissingRepairError("source_shape_invalid", f"{name} is not an object")
    return value


def _mapping_mutable(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.setdefault(key, {})
    if not isinstance(value, dict):
        raise HistoricalMissingRepairError("metadata_shape_invalid", f"{key} is not an object")
    return value


def _loads_object(value: Any, name: str) -> dict[str, Any]:
    parsed = json.loads(str(value)) if isinstance(value, str) else deepcopy(value)
    if not isinstance(parsed, dict):
        raise HistoricalMissingRepairError("json_shape_invalid", f"{name} is not an object")
    return parsed


def _is_missing(value: Any) -> bool:
    return value is None or value == ""


def _value_type(value: Any) -> str:
    if _is_missing(value):
        return "missing"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "exact_zero" if value == 0 else "numeric"
    return "text"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--readback", action="store_true")
    parser.add_argument("--manifest-path", type=Path)
    parser.add_argument("--expected-manifest-sha256", default="")
    parser.add_argument("--expected-deployed-sha", default="")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--deployed-sha-file", type=Path)
    parser.add_argument("--target-file", type=Path)
    args = parser.parse_args()
    try:
        if args.readback:
            if args.manifest_path is None:
                raise HistoricalMissingRepairError(
                    "manifest_required", "readback requires the reviewed manifest"
                )
            result = readback(
                runtime_dir=args.runtime_dir,
                operation_id=args.operation_id,
                manifest_path=args.manifest_path,
                target_file=args.target_file,
                expected_deployed_sha=args.expected_deployed_sha,
                deployed_sha_file=args.deployed_sha_file,
            )
        else:
            result = run(
                runtime_dir=args.runtime_dir,
                evidence_dir=args.evidence_dir,
                operation_id=args.operation_id,
                apply=args.apply,
                manifest_path=args.manifest_path,
                expected_manifest_sha256=args.expected_manifest_sha256,
                expected_deployed_sha=args.expected_deployed_sha,
                approval_reference=args.approval_reference,
                deployed_sha_file=args.deployed_sha_file,
                target_file=args.target_file,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except HistoricalMissingRepairError as exc:
        print(
            json.dumps(
                {"status": "blocked", "code": exc.code, "error": str(exc), "details": exc.details},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
