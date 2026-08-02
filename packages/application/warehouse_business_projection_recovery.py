"""Bounded recovery for mixed Web Vitrina warehouse-capital dates.

The recovery republishes derived projection rows only.  It selects one exact
good functional version per business date and, where an older version predates
the already-applied FF inventory documents, overlays the frozen document
quantity/capital deltas in memory.  Physical ledgers, functional versions and
their active pointer are query-only invariants.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
)
from packages.application.warehouse_business_projection import (
    CONTRACT_VERSION as BUSINESS_PROJECTION_CONTRACT_VERSION,
    CURRENT_ROW_TABLE,
    REVISION_TABLE,
    ROW_TABLE,
    STATE_TABLE,
    _metric_rows,
    _persist_projection_revision,
)
from packages.application.warehouse_event_order import ff_operation_replay_sort_key
from packages.application.warehouse_functional import (
    FUNCTIONAL_CUTOVER_ID,
    _watermark,
)
from packages.application.warehouse_functional_lock import (
    warehouse_functional_write_lock,
)
from packages.application.warehouse_recovery_policy import (
    RecoveryState,
    WarehouseRecoveryRegistry,
    recovery_operation_id,
)


CONTRACT_NAME = "warehouse_business_projection_exact_functional_recovery_v1"
MAX_DATES = 14
ZERO = Decimal("0")


class WarehouseBusinessProjectionRecoveryError(RuntimeError):
    """The bounded exact-functional projection recovery failed closed."""


def build_business_projection_recovery_plan(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    source_sha256: str,
    business_date: str,
) -> dict[str, Any]:
    """Build a deterministic query-only recovery manifest."""

    source_hash = _sha256(source_sha256)
    inventory_date = _iso_date(business_date, field_name="business_date")
    with _connect(runtime.db_path, read_only=True) as conn:
        _require_tables(conn)
        reconciliation = conn.execute(
            """
            SELECT * FROM sheet_vitrina_v1_ff_inventory_reconciliations
            WHERE source_sha256=? AND business_date=? AND status='applied'
            """,
            (source_hash, inventory_date),
        ).fetchone()
        if reconciliation is None:
            raise WarehouseBusinessProjectionRecoveryError(
                "exact applied FF inventory reconciliation is missing"
            )
        reconciliation_row = dict(reconciliation)
        operation_ids = sorted(
            {
                str(value)
                for value in _loads(
                    reconciliation_row.get("operation_ids_json"), []
                )
                if str(value)
            }
        )
        if not operation_ids:
            raise WarehouseBusinessProjectionRecoveryError(
                "inventory reconciliation has no operation identities"
            )
        operations, lines = _inventory_documents(
            conn,
            operation_ids=operation_ids,
            business_date=inventory_date,
        )
        document_deltas = _document_deltas(lines)
        if not document_deltas:
            raise WarehouseBusinessProjectionRecoveryError(
                "inventory reconciliation has no non-zero frozen-cost lines"
            )
        inventory_target_by_nm = _inventory_target_map(reconciliation_row)

        active = conn.execute(
            """
            SELECT active.version_id,active.updated_at,
                   version.business_effective_date,version.status,
                   version.plan_fingerprint,version.source_watermarks_json,
                   snapshot.snapshot_date
            FROM sheet_vitrina_v1_warehouse_functional_active AS active
            JOIN sheet_vitrina_v1_warehouse_functional_versions AS version
              ON version.version_id=active.version_id
            JOIN sheet_vitrina_v1_warehouse_wb_snapshots AS snapshot
              ON snapshot.version_id=active.version_id
            WHERE active.slot=1
              AND version.cutover_id=?
            """,
            (FUNCTIONAL_CUTOVER_ID,),
        ).fetchone()
        if (
            active is None
            or str(active["status"]) != "good"
            or str(active["business_effective_date"] or "")
            != str(active["snapshot_date"] or "")
        ):
            raise WarehouseBusinessProjectionRecoveryError(
                "active exact functional business-date version is unavailable"
            )
        active_row = dict(active)
        active_date = _iso_date(
            active_row["business_effective_date"],
            field_name="active_business_effective_date",
        )
        start_date = (date.fromisoformat(inventory_date) - timedelta(days=1)).isoformat()
        target_dates = _date_range(start_date, active_date)
        if not target_dates or len(target_dates) > MAX_DATES:
            raise WarehouseBusinessProjectionRecoveryError(
                "bounded projection recovery exceeds the reviewed date window"
            )

        all_operations = [
            dict(row)
            for row in conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_ff_stock_operations
                ORDER BY created_at,operation_id
                """
            ).fetchall()
        ]
        all_operations.sort(key=ff_operation_replay_sort_key)
        versions: list[dict[str, Any]] = []
        candidate_rows: list[dict[str, Any]] = []
        all_nm_ids: set[int] = set()
        for as_of_date in target_dates:
            version = _exact_base_version(conn, as_of_date=as_of_date)
            watermark = dict(
                _loads(version.get("source_watermarks_json"), {}).get(
                    "ff_ledger"
                )
                or {}
            )
            included_operation_ids = _included_operation_ids(
                all_operations,
                watermark=watermark,
            )
            missing_operation_ids = [
                operation_id
                for operation_id in operation_ids
                if as_of_date >= inventory_date
                and operation_id not in included_operation_ids
            ]
            balances = _balance_rows(conn, version_id=str(version["version_id"]))
            before_digest = _digest(balances)
            corrected = _apply_ff_document_deltas(
                balances,
                lines=[
                    item
                    for item in lines
                    if str(item["operation_id"]) in set(missing_operation_ids)
                ],
                as_of_date=as_of_date,
                base_version_id=str(version["version_id"]),
                reconciliation_id=str(reconciliation_row["reconciliation_id"]),
            )
            _validate_balances(corrected, as_of_date=as_of_date)
            if as_of_date >= inventory_date:
                actual_ff = {
                    int(item["nm_id"]): _decimal(item["quantity"])
                    for item in corrected
                    if str(item["warehouse_key"]) == "ff"
                }
                mismatches = [
                    {
                        "nm_id": nm_id,
                        "expected": _text(expected),
                        "actual": _text(actual_ff.get(nm_id, ZERO)),
                    }
                    for nm_id, expected in sorted(inventory_target_by_nm.items())
                    if actual_ff.get(nm_id, ZERO) != expected
                ]
                if mismatches:
                    raise WarehouseBusinessProjectionRecoveryError(
                        "corrected FF projection does not match the manager target: "
                        + _canonical_json(mismatches[:10])
                    )
            nm_ids = sorted(
                {
                    int(item.get("nm_id") or 0)
                    for item in corrected
                    if int(item.get("nm_id") or 0) > 0
                }
            )
            all_nm_ids.update(nm_ids)
            metrics_by_nm = _metric_rows(corrected, affected_nm_ids=nm_ids)
            for nm_id, item in sorted(metrics_by_nm.items()):
                provenance = {
                    "contract_name": CONTRACT_NAME,
                    "business_projection_contract_version": (
                        BUSINESS_PROJECTION_CONTRACT_VERSION
                    ),
                    "source": "exact_functional_version",
                    "as_of_date": as_of_date,
                    "base_version_id": str(version["version_id"]),
                    "base_plan_fingerprint": str(
                        version["plan_fingerprint"]
                    ),
                    "inventory_source_sha256": source_hash,
                    "inventory_business_date": inventory_date,
                    "inventory_reconciliation_id": str(
                        reconciliation_row["reconciliation_id"]
                    ),
                    "applied_missing_operation_ids": missing_operation_ids,
                    "physical_ledger_mutated": False,
                }
                material = {
                    "as_of_date": as_of_date,
                    "nm_id": int(nm_id),
                    "metrics": dict(item["metrics"]),
                    "presentation": dict(item["presentation"]),
                    "provenance": provenance,
                }
                candidate_rows.append(
                    {**material, "row_fingerprint": _digest(material)}
                )
            stage_totals = _stage_totals(corrected)
            versions.append(
                {
                    "business_date": as_of_date,
                    "base_version_id": str(version["version_id"]),
                    "base_plan_fingerprint": str(version["plan_fingerprint"]),
                    "base_balance_digest": before_digest,
                    "corrected_balance_digest": _digest(corrected),
                    "ff_ledger_watermark": watermark,
                    "missing_operation_ids_applied": missing_operation_ids,
                    "stage_totals": stage_totals,
                }
            )

        current_rows = _current_projection_rows(
            conn,
            target_dates=target_dates,
        )
        candidate_keyed = {
            (str(item["as_of_date"]), int(item["nm_id"])): item
            for item in candidate_rows
        }
        current_keyed = {
            (str(item["as_of_date"]), int(item["nm_id"])): item
            for item in current_rows
        }
        changed_keys = sorted(
            {
                *candidate_keyed,
                *current_keyed,
            }
            - {
                key
                for key in set(candidate_keyed) & set(current_keyed)
                if str(candidate_keyed[key]["row_fingerprint"])
                == str(current_keyed[key]["row_fingerprint"])
            }
        )
        source_digest = _digest(
            {
                "reconciliation": _reconciliation_identity(reconciliation_row),
                "operations": operations,
                "lines": lines,
                "versions": versions,
            }
        )
        non_target_digest = _non_target_digest(conn)
        current_target_digest = _digest(current_rows)
        material = {
            "contract_name": CONTRACT_NAME,
            "source_sha256": source_hash,
            "business_date": inventory_date,
            "active_version_id": str(active_row["version_id"]),
            "active_business_date": active_date,
            "target_dates": target_dates,
            "inventory_operation_ids": operation_ids,
            "inventory_document_deltas_by_nm": document_deltas,
            "inventory_document_lines": [
                {
                    key: item[key]
                    for key in (
                        "operation_id",
                        "line_no",
                        "nm_id",
                        "quantity_delta",
                        "unit_cost_rub",
                        "capital_delta_rub",
                        "cost_quality",
                        "cost_provenance",
                    )
                }
                for item in lines
            ],
            "source_digest": source_digest,
            "non_target_digest": non_target_digest,
            "current_target_digest": current_target_digest,
            "versions": versions,
            "candidate_row_fingerprints": [
                str(item["row_fingerprint"]) for item in candidate_rows
            ],
            "changed_keys": [[day, nm_id] for day, nm_id in changed_keys],
        }
        fingerprint = _digest(material)
        revision_id = (
            "whbpr_exact_recovery_"
            + fingerprint.removeprefix("sha256:")[:20]
        )
        target_sku_count, target_total = _inventory_target(reconciliation_row)
        plan = {
            **material,
            "status": "ready",
            "mode": "dry_run",
            "fingerprint": fingerprint,
            "revision_id": revision_id,
            "would_change": bool(changed_keys),
            "scope": {
                "source_sha256": source_hash,
                "business_date": inventory_date,
                "target_dates": target_dates,
                "target_nm_ids": sorted(all_nm_ids),
                "target_sku_count": target_sku_count,
                "target_total_quantity": target_total,
                "physical_mutation": False,
                "functional_active_pointer_mutation": False,
                "projection_tables_only": True,
            },
            "counts": {
                "operation_count": len(operations),
                "operation_line_count": len(lines),
                "candidate_row_count": len(candidate_rows),
                "current_row_count": len(current_rows),
                "changed_row_count": len(changed_keys),
            },
            "_apply_payload": {
                "revision_id": revision_id,
                "rows": candidate_rows,
                "current_keys": [
                    [str(item["as_of_date"]), int(item["nm_id"])]
                    for item in current_rows
                ],
            },
        }
        return plan


def apply_business_projection_recovery_plan(
    runtime: RegistryUploadDbBackedRuntime,
    plan: Mapping[str, Any],
    *,
    confirm_fingerprint: str,
    approval_reference: str,
) -> dict[str, Any]:
    """Apply one exact reviewed plan under a target-scoped T1 journal."""

    fingerprint = str(plan.get("fingerprint") or "")
    if not fingerprint or fingerprint != str(confirm_fingerprint or ""):
        raise WarehouseBusinessProjectionRecoveryError(
            "apply requires the exact current projection-recovery fingerprint"
        )
    if not str(approval_reference or "").strip():
        raise WarehouseBusinessProjectionRecoveryError(
            "exact bounded human-gate provenance is required"
        )
    registry = WarehouseRecoveryRegistry(
        runtime_dir=runtime.runtime_dir,
        db_path=runtime.db_path,
    )
    operation_id = recovery_operation_id(
        "targeted_warehouse_publication",
        fingerprint,
    )
    existing = registry.get_operation(operation_id)
    if (
        existing is not None
        and str(existing.get("lifecycle")) == RecoveryState.RETAINED.value
    ):
        readback = readback_business_projection_recovery(runtime, plan=plan)
        return {
            **public_plan(plan),
            "mode": "apply",
            "applied": False,
            "idempotent": True,
            "second_run": {
                "tier": "T0",
                "changed_rows": 0,
                "mutations": 0,
                "recovery_bytes": 0,
            },
            "readback": readback,
            "recovery_policy": existing,
        }
    if not bool(plan.get("would_change")):
        return {
            **public_plan(plan),
            "mode": "apply",
            "applied": False,
            "idempotent": True,
            "recovery_policy": registry.plan_noop(
                mutation_kind="targeted_warehouse_publication",
                closure_kind="sku_date",
                plan_fingerprint=fingerprint,
                scope=dict(plan["scope"]),
            ),
        }
    payload = dict(plan["_apply_payload"])
    before_images = _before_images(runtime.db_path, plan=plan, payload=payload)
    recovery = registry.prepare_t1(
        mutation_kind="targeted_warehouse_publication",
        closure_kind="sku_date",
        plan_fingerprint=fingerprint,
        scope={
            **dict(plan["scope"]),
            "approval_reference": str(approval_reference),
        },
        before_images=before_images,
        source_digest=str(plan["source_digest"]),
        non_target_digest=str(plan["non_target_digest"]),
        read_bytes=sum(
            len(_canonical_json(item).encode("utf-8"))
            for item in before_images
        ),
    )
    if str(recovery.get("lifecycle")) == RecoveryState.VERIFIED.value:
        recovery = registry.begin_mutation(
            str(recovery["operation_id"]),
            expected_source_digest=str(plan["source_digest"]),
        )
    published_at = _now()
    try:
        with warehouse_functional_write_lock(
            runtime.runtime_dir,
            timeout_seconds=300,
        ):
            fresh = build_business_projection_recovery_plan(
                runtime,
                source_sha256=str(plan["source_sha256"]),
                business_date=str(plan["business_date"]),
            )
            if str(fresh["fingerprint"]) != fingerprint:
                raise WarehouseBusinessProjectionRecoveryError(
                    "source, target or non-target fingerprint drifted after dry-run"
                )
            with _connect(runtime.db_path, read_only=False) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    if _non_target_digest(conn) != str(
                        plan["non_target_digest"]
                    ):
                        raise WarehouseBusinessProjectionRecoveryError(
                            "physical/function non-target digest drifted before apply"
                        )
                    locked_target_rows = _current_projection_rows(
                        conn,
                        target_dates=list(plan["target_dates"]),
                    )
                    if _digest(locked_target_rows) != str(
                        plan["current_target_digest"]
                    ):
                        raise WarehouseBusinessProjectionRecoveryError(
                            "projection target rows drifted before locked apply"
                        )
                    candidate_keys = {
                        (str(item["as_of_date"]), int(item["nm_id"]))
                        for item in payload["rows"]
                    }
                    for as_of_date, nm_id in payload["current_keys"]:
                        if (str(as_of_date), int(nm_id)) in candidate_keys:
                            continue
                        conn.execute(
                            f"DELETE FROM {CURRENT_ROW_TABLE} "
                            "WHERE as_of_date=? AND nm_id=?",
                            (str(as_of_date), int(nm_id)),
                        )
                    projection = _persist_projection_revision(
                        conn,
                        revision_id=str(payload["revision_id"]),
                        stable_source_id=(
                            "ff_inventory_projection_recovery:"
                            + str(plan["source_sha256"])
                        ),
                        source_revision=str(plan["source_digest"]),
                        business_effective_date=str(
                            plan["active_business_date"]
                        ),
                        published_at=published_at,
                        plan_fingerprint=fingerprint,
                        base_version_id=str(
                            plan["versions"][0]["base_version_id"]
                        ),
                        published_version_id=str(plan["active_version_id"]),
                        affected_nm_ids=list(plan["scope"]["target_nm_ids"]),
                        source_kind="exact_functional_projection_recovery",
                        rows=list(payload["rows"]),
                        diagnostics={
                            "contract_name": CONTRACT_NAME,
                            "affected_dates": list(plan["target_dates"]),
                            "inventory_source_sha256": str(
                                plan["source_sha256"]
                            ),
                            "inventory_business_date": str(
                                plan["business_date"]
                            ),
                            "approval_reference": str(approval_reference),
                            "physical_mutation": False,
                        },
                    )
                    if _non_target_digest(conn) != str(
                        plan["non_target_digest"]
                    ):
                        raise WarehouseBusinessProjectionRecoveryError(
                            "projection transaction changed a non-target invariant"
                        )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
    except Exception as exc:
        registry.fail_recoverable(
            str(recovery["operation_id"]),
            error=str(exc),
            next_action="rollback_or_replan_business_projection_recovery",
        )
        raise
    readback = readback_business_projection_recovery(runtime, plan=plan)
    recovery = registry.retain(
        str(recovery["operation_id"]),
        after_digest=str(readback["after_digest"]),
        non_target_digest=str(plan["non_target_digest"]),
    )
    return {
        **public_plan(plan),
        "mode": "apply",
        "applied": True,
        "idempotent": False,
        "published_at": published_at,
        "approval_reference": str(approval_reference),
        "projection": projection,
        "readback": readback,
        "recovery_policy": recovery,
    }


def readback_business_projection_recovery(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    payload = dict(plan["_apply_payload"])
    with _connect(runtime.db_path, read_only=True) as conn:
        actual = _current_projection_rows(
            conn,
            target_dates=list(plan["target_dates"]),
        )
        expected = sorted(
            [dict(item) for item in payload["rows"]],
            key=lambda item: (str(item["as_of_date"]), int(item["nm_id"])),
        )
        if [str(item["row_fingerprint"]) for item in actual] != [
            str(item["row_fingerprint"]) for item in expected
        ]:
            raise WarehouseBusinessProjectionRecoveryError(
                "projection row readback does not match the exact plan"
            )
        revision = conn.execute(
            f"SELECT * FROM {REVISION_TABLE} WHERE revision_id=?",
            (str(payload["revision_id"]),),
        ).fetchone()
        state = conn.execute(
            f"SELECT * FROM {STATE_TABLE} WHERE slot=1"
        ).fetchone()
        if (
            revision is None
            or str(revision["status"]) != "active"
            or state is None
            or str(state["revision_id"]) != str(payload["revision_id"])
            or str(state["business_effective_date"])
            != str(plan["active_business_date"])
        ):
            raise WarehouseBusinessProjectionRecoveryError(
                "projection revision/state readback is not active and fresh"
            )
        non_target = _non_target_digest(conn)
        if non_target != str(plan["non_target_digest"]):
            raise WarehouseBusinessProjectionRecoveryError(
                "physical/function non-target digest changed during projection recovery"
            )
    totals = {
        str(item["business_date"]): dict(item["stage_totals"])
        for item in plan["versions"]
    }
    return {
        "status": "reconciled",
        "revision_id": str(payload["revision_id"]),
        "business_effective_date": str(plan["active_business_date"]),
        "row_count": len(actual),
        "target_dates": list(plan["target_dates"]),
        "stage_totals_by_date": totals,
        "non_target_digest": non_target,
        "non_target_unchanged": True,
        "physical_mutation": False,
        "after_digest": _digest(actual),
    }


def rollback_business_projection_recovery(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    fingerprint: str,
    reason: str,
) -> dict[str, Any]:
    registry = WarehouseRecoveryRegistry(
        runtime_dir=runtime.runtime_dir,
        db_path=runtime.db_path,
    )
    return registry.rollback_t1(
        recovery_operation_id(
            "targeted_warehouse_publication",
            str(fingerprint),
        ),
        reason=str(reason),
    )


def public_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(value)
        for key, value in plan.items()
        if not str(key).startswith("_")
    }


def _exact_base_version(
    conn: sqlite3.Connection,
    *,
    as_of_date: str,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT version.*,snapshot.snapshot_date,snapshot.raw_rows_digest
        FROM sheet_vitrina_v1_warehouse_functional_versions AS version
        JOIN sheet_vitrina_v1_warehouse_wb_snapshots AS snapshot
          ON snapshot.version_id=version.version_id
        WHERE version.status='good'
          AND version.cutover_id=?
          AND version.business_effective_date=?
          AND snapshot.snapshot_date=?
        ORDER BY COALESCE(NULLIF(version.published_at,''),version.created_at) DESC,
                 version.created_at DESC,version.version_id DESC
        LIMIT 1
        """,
        (FUNCTIONAL_CUTOVER_ID, as_of_date, as_of_date),
    ).fetchone()
    if row is None:
        raise WarehouseBusinessProjectionRecoveryError(
            f"exact good functional version is missing: {as_of_date}"
        )
    return dict(row)


def _inventory_documents(
    conn: sqlite3.Connection,
    *,
    operation_ids: Sequence[str],
    business_date: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    placeholders = ",".join("?" for _ in operation_ids)
    operations = [
        dict(row)
        for row in conn.execute(
            f"SELECT * FROM sheet_vitrina_v1_ff_stock_operations "
            f"WHERE operation_id IN ({placeholders}) ORDER BY created_at,operation_id",
            tuple(operation_ids),
        ).fetchall()
    ]
    if {str(item["operation_id"]) for item in operations} != set(operation_ids):
        raise WarehouseBusinessProjectionRecoveryError(
            "inventory reconciliation operation set is incomplete"
        )
    if any(
        str(item.get("business_effective_date") or "") != business_date
        for item in operations
    ):
        raise WarehouseBusinessProjectionRecoveryError(
            "inventory document business date differs from reconciliation"
        )
    raw_lines = conn.execute(
        f"""
        SELECT line.*,operation.created_at,operation.business_effective_date,
               operation.operation_type,operation.source_type,
               operation.source_object_id
        FROM sheet_vitrina_v1_ff_stock_operation_lines AS line
        JOIN sheet_vitrina_v1_ff_stock_operations AS operation
          ON operation.operation_id=line.operation_id
        WHERE line.operation_id IN ({placeholders})
        ORDER BY operation.created_at,line.operation_id,line.line_no
        """,
        tuple(operation_ids),
    ).fetchall()
    lines: list[dict[str, Any]] = []
    for raw in raw_lines:
        item = dict(raw)
        parsed = _loads(item.get("raw_json"), {})
        snapshot = (
            dict(parsed.get("cost_snapshot") or {})
            if isinstance(parsed, Mapping)
            else {}
        )
        quantity_delta = _decimal(item.get("quantity_delta"))
        unit_cost = _decimal(snapshot.get("unit_cost_rub"))
        capital_delta = _decimal(snapshot.get("capital_delta_rub"))
        quality = str(snapshot.get("quality") or "").strip()
        provenance = snapshot.get("provenance")
        if (
            quantity_delta == ZERO
            or unit_cost <= ZERO
            or not quality
            or not isinstance(provenance, Mapping)
            or not provenance
        ):
            raise WarehouseBusinessProjectionRecoveryError(
                "inventory projection line has no auditable exact frozen cost"
            )
        if capital_delta != quantity_delta * unit_cost:
            raise WarehouseBusinessProjectionRecoveryError(
                "inventory projection line capital does not match frozen unit cost"
            )
        lines.append(
            {
                **item,
                "quantity_delta": _text(quantity_delta),
                "unit_cost_rub": _text(unit_cost),
                "capital_delta_rub": _text(capital_delta),
                "cost_quality": quality,
                "cost_provenance": dict(provenance),
            }
        )
    if not lines:
        raise WarehouseBusinessProjectionRecoveryError(
            "inventory reconciliation operation lines are missing"
        )
    return operations, lines


def _included_operation_ids(
    all_operations: Sequence[Mapping[str, Any]],
    *,
    watermark: Mapping[str, Any],
) -> set[str]:
    try:
        row_count = int(watermark.get("row_count") or 0)
    except (TypeError, ValueError) as exc:
        raise WarehouseBusinessProjectionRecoveryError(
            "functional FF ledger watermark row count is invalid"
        ) from exc
    if row_count < 0 or row_count > len(all_operations):
        raise WarehouseBusinessProjectionRecoveryError(
            "functional FF ledger watermark exceeds append-only source"
        )
    prefix = [dict(item) for item in all_operations[:row_count]]
    expected = _watermark(prefix, "created_at")
    if any(
        str(expected.get(key) or "") != str(watermark.get(key) or "")
        for key in ("row_count", "max", "digest")
    ):
        raise WarehouseBusinessProjectionRecoveryError(
            "functional FF ledger watermark is not an exact append-only prefix"
        )
    return {str(item["operation_id"]) for item in prefix}


def _balance_rows(
    conn: sqlite3.Connection,
    *,
    version_id: str,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances
            WHERE version_id=? ORDER BY warehouse_key,nm_id
            """,
            (version_id,),
        ).fetchall()
    ]


def _apply_ff_document_deltas(
    balances: Sequence[Mapping[str, Any]],
    *,
    lines: Sequence[Mapping[str, Any]],
    as_of_date: str,
    base_version_id: str,
    reconciliation_id: str,
) -> list[dict[str, Any]]:
    result = [deepcopy(dict(item)) for item in balances]
    by_key = {
        (str(item["warehouse_key"]), int(item["nm_id"])): item
        for item in result
    }
    grouped: dict[int, dict[str, Any]] = {}
    for line in lines:
        nm_id = int(line["nm_id"])
        target = grouped.setdefault(
            nm_id,
            {"quantity": ZERO, "capital": ZERO, "lines": []},
        )
        target["quantity"] += _decimal(line["quantity_delta"])
        target["capital"] += _decimal(line["capital_delta_rub"])
        target["lines"].append(
            {
                "operation_id": str(line["operation_id"]),
                "line_no": int(line["line_no"]),
                "quantity_delta": str(line["quantity_delta"]),
                "capital_delta_rub": str(line["capital_delta_rub"]),
                "unit_cost_rub": str(line["unit_cost_rub"]),
                "cost_quality": str(line["cost_quality"]),
                "cost_provenance": dict(line["cost_provenance"]),
            }
        )
    for nm_id, delta in sorted(grouped.items()):
        row = by_key.get(("ff", nm_id))
        if row is None:
            row = {
                "version_id": base_version_id,
                "warehouse_key": "ff",
                "nm_id": nm_id,
                "quantity": "0",
                "wac_rub": None,
                "capital_rub": "0",
                "cost_covered_quantity": "0",
                "quality": "certified",
                "certified": 1,
                "wb_quantity": "0",
                "wb_in_way_to_client": "0",
                "wb_in_way_from_client": "0",
                "provenance_json": "{}",
            }
            result.append(row)
            by_key[("ff", nm_id)] = row
        quantity = _decimal(row.get("quantity")) + _decimal(delta["quantity"])
        capital = _decimal(row.get("capital_rub")) + _decimal(delta["capital"])
        if quantity < ZERO or capital < ZERO or (quantity > ZERO and capital <= ZERO):
            raise WarehouseBusinessProjectionRecoveryError(
                f"inventory projection would make FF invalid: {as_of_date}:{nm_id}"
            )
        row["quantity"] = _text(quantity)
        row["capital_rub"] = _text(capital)
        row["wac_rub"] = _text(capital / quantity) if quantity > ZERO else None
        row["cost_covered_quantity"] = _text(quantity)
        row["quality"] = "certified_inventory_reconciliation"
        row["certified"] = 1
        provenance = _loads(row.get("provenance_json"), {})
        provenance["business_projection_recovery"] = {
            "contract_name": CONTRACT_NAME,
            "as_of_date": as_of_date,
            "base_version_id": base_version_id,
            "reconciliation_id": reconciliation_id,
            "lines": list(delta["lines"]),
            "physical_ledger_mutated": False,
        }
        row["provenance_json"] = _canonical_json(provenance)
    return sorted(
        result,
        key=lambda item: (str(item["warehouse_key"]), int(item["nm_id"])),
    )


def _validate_balances(
    balances: Sequence[Mapping[str, Any]],
    *,
    as_of_date: str,
) -> None:
    seen: set[tuple[str, int]] = set()
    for item in balances:
        key = (str(item.get("warehouse_key") or ""), int(item.get("nm_id") or 0))
        if key in seen or not key[0] or key[1] <= 0:
            raise WarehouseBusinessProjectionRecoveryError(
                f"duplicate/invalid functional balance identity: {as_of_date}:{key}"
            )
        seen.add(key)
        quantity = _decimal(item.get("quantity"))
        capital = _decimal(item.get("capital_rub"))
        covered = _decimal(item.get("cost_covered_quantity"))
        if (
            quantity < ZERO
            or capital < ZERO
            or covered < ZERO
            or covered > quantity
            or (quantity > ZERO and (capital <= ZERO or covered != quantity))
        ):
            raise WarehouseBusinessProjectionRecoveryError(
                f"functional balance quantity/capital invariant failed: {as_of_date}:{key}"
            )


def _stage_totals(
    balances: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, Decimal]] = {}
    for item in balances:
        stage = str(item["warehouse_key"])
        target = result.setdefault(stage, {"quantity": ZERO, "capital_rub": ZERO})
        target["quantity"] += _decimal(item.get("quantity"))
        target["capital_rub"] += _decimal(item.get("capital_rub"))
    return {
        stage: {key: _text(value) for key, value in totals.items()}
        for stage, totals in sorted(result.items())
    }


def _document_deltas(
    lines: Iterable[Mapping[str, Any]],
) -> dict[int, dict[str, str]]:
    result: dict[int, dict[str, Decimal]] = {}
    for item in lines:
        target = result.setdefault(
            int(item["nm_id"]),
            {"quantity": ZERO, "capital_rub": ZERO},
        )
        target["quantity"] += _decimal(item["quantity_delta"])
        target["capital_rub"] += _decimal(item["capital_delta_rub"])
    return {
        nm_id: {key: _text(value) for key, value in delta.items()}
        for nm_id, delta in sorted(result.items())
        if delta["quantity"] != ZERO or delta["capital_rub"] != ZERO
    }


def _current_projection_rows(
    conn: sqlite3.Connection,
    *,
    target_dates: Sequence[str],
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in target_dates)
    return [
        dict(row)
        for row in conn.execute(
            f"SELECT * FROM {CURRENT_ROW_TABLE} "
            f"WHERE as_of_date IN ({placeholders}) ORDER BY as_of_date,nm_id",
            tuple(target_dates),
        ).fetchall()
    ]


def _before_images(
    db_path: Path,
    *,
    plan: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    with _connect(db_path, read_only=True) as conn:
        revision_id = str(payload["revision_id"])
        revision = conn.execute(
            f"SELECT * FROM {REVISION_TABLE} WHERE revision_id=?",
            (revision_id,),
        ).fetchone()
        images.append(
            _before_image(
                REVISION_TABLE,
                {"revision_id": revision_id},
                dict(revision) if revision is not None else None,
            )
        )
        for item in payload["rows"]:
            key = {
                "revision_id": revision_id,
                "as_of_date": str(item["as_of_date"]),
                "nm_id": int(item["nm_id"]),
            }
            row = conn.execute(
                f"SELECT * FROM {ROW_TABLE} WHERE revision_id=? "
                "AND as_of_date=? AND nm_id=?",
                (key["revision_id"], key["as_of_date"], key["nm_id"]),
            ).fetchone()
            images.append(
                _before_image(
                    ROW_TABLE,
                    key,
                    dict(row) if row is not None else None,
                )
            )
        union_keys = {
            (str(item["as_of_date"]), int(item["nm_id"]))
            for item in payload["rows"]
        } | {
            (str(day), int(nm_id))
            for day, nm_id in payload["current_keys"]
        }
        for as_of_date, nm_id in sorted(union_keys):
            row = conn.execute(
                f"SELECT * FROM {CURRENT_ROW_TABLE} "
                "WHERE as_of_date=? AND nm_id=?",
                (as_of_date, nm_id),
            ).fetchone()
            images.append(
                _before_image(
                    CURRENT_ROW_TABLE,
                    {"as_of_date": as_of_date, "nm_id": nm_id},
                    dict(row) if row is not None else None,
                )
            )
        state = conn.execute(f"SELECT * FROM {STATE_TABLE} WHERE slot=1").fetchone()
        images.append(
            _before_image(
                STATE_TABLE,
                {"slot": 1},
                dict(state) if state is not None else None,
            )
        )
    return images


def _non_target_digest(conn: sqlite3.Connection) -> str:
    active = conn.execute(
        "SELECT * FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
    ).fetchone()
    if active is None:
        raise WarehouseBusinessProjectionRecoveryError(
            "functional active pointer is missing"
        )
    active_row = dict(active)
    version_id = str(active_row["version_id"])
    active_version = conn.execute(
        "SELECT * FROM sheet_vitrina_v1_warehouse_functional_versions WHERE version_id=?",
        (version_id,),
    ).fetchone()
    balances = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances "
            "WHERE version_id=? ORDER BY warehouse_key,nm_id",
            (version_id,),
        ).fetchall()
    ]
    operations = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM sheet_vitrina_v1_ff_stock_operations "
            "ORDER BY created_at,operation_id"
        ).fetchall()
    ]
    lines = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM sheet_vitrina_v1_ff_stock_operation_lines "
            "ORDER BY operation_id,line_no"
        ).fetchall()
    ]
    return _digest(
        {
            "functional_active": active_row,
            "active_version": dict(active_version) if active_version else None,
            "active_balances": balances,
            "ff_operations": operations,
            "ff_lines": lines,
        }
    )


def _inventory_target(row: Mapping[str, Any]) -> tuple[int, str]:
    manifest = _loads(row.get("manifest_json"), {})
    per_sku = list(manifest.get("per_sku") or [])
    total = manifest.get("target_total")
    if total in (None, ""):
        total = sum(
            (_decimal(item.get("target_quantity")) for item in per_sku),
            ZERO,
        )
    return len(per_sku), _text(_decimal(total))


def _inventory_target_map(row: Mapping[str, Any]) -> dict[int, Decimal]:
    manifest = _loads(row.get("manifest_json"), {})
    result: dict[int, Decimal] = {}
    for item in manifest.get("per_sku") or []:
        nm_id = int(item.get("nm_id") or 0)
        if nm_id <= 0 or nm_id in result:
            raise WarehouseBusinessProjectionRecoveryError(
                "inventory target contains an invalid/duplicate nmID"
            )
        result[nm_id] = _decimal(item.get("target_quantity"))
    if not result or any(value < ZERO for value in result.values()):
        raise WarehouseBusinessProjectionRecoveryError(
            "inventory target per-SKU manifest is missing or negative"
        )
    declared_total = _decimal(manifest.get("target_total"))
    if sum(result.values(), ZERO) != declared_total:
        raise WarehouseBusinessProjectionRecoveryError(
            "inventory target per-SKU quantities do not match declared total"
        )
    return result


def _reconciliation_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "reconciliation_id",
            "source_sha256",
            "business_date",
            "plan_fingerprint",
            "approval_reference",
            "created_at",
            "status",
            "operation_ids_json",
            "before_digest",
            "non_target_digest",
            "after_digest",
            "manifest_json",
            "reconciliation_json",
        )
    }


def _require_tables(conn: sqlite3.Connection) -> None:
    required = {
        "sheet_vitrina_v1_ff_inventory_reconciliations",
        "sheet_vitrina_v1_ff_stock_operations",
        "sheet_vitrina_v1_ff_stock_operation_lines",
        "sheet_vitrina_v1_warehouse_functional_versions",
        "sheet_vitrina_v1_warehouse_functional_active",
        "sheet_vitrina_v1_warehouse_functional_balances",
        "sheet_vitrina_v1_warehouse_wb_snapshots",
        REVISION_TABLE,
        ROW_TABLE,
        CURRENT_ROW_TABLE,
        STATE_TABLE,
    }
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    missing = sorted(required - tables)
    if missing:
        raise WarehouseBusinessProjectionRecoveryError(
            "required projection recovery tables are missing: "
            + ",".join(missing)
        )


def _before_image(
    table: str,
    key: Mapping[str, Any],
    before: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "table": str(table),
        "key": dict(key),
        "before": dict(before) if before is not None else None,
        "after": None,
    }


def _date_range(start: str, end: str) -> list[str]:
    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    if first > last:
        raise WarehouseBusinessProjectionRecoveryError(
            "inventory date is after active functional business date"
        )
    return [
        (first + timedelta(days=offset)).isoformat()
        for offset in range((last - first).days + 1)
    ]


def _iso_date(value: Any, *, field_name: str) -> str:
    try:
        return date.fromisoformat(str(value or "")[:10]).isoformat()
    except ValueError as exc:
        raise WarehouseBusinessProjectionRecoveryError(
            f"{field_name} must be an ISO business date"
        ) from exc


def _sha256(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("sha256:"):
        text = text[7:]
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise WarehouseBusinessProjectionRecoveryError(
            "source_sha256 must be one exact sha256 digest"
        )
    return "sha256:" + text


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except Exception as exc:
        raise WarehouseBusinessProjectionRecoveryError(
            f"invalid decimal value: {value!r}"
        ) from exc


def _text(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(Decimal("1")))
    return format(normalized, "f")


def _loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return deepcopy(value)
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return deepcopy(fallback)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical_json(value).encode("utf-8")
    ).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _connect(db_path: Path, *, read_only: bool) -> sqlite3.Connection:
    if read_only:
        conn = sqlite3.connect(
            f"file:{Path(db_path).resolve()}?mode=ro",
            uri=True,
            timeout=300,
        )
        conn.execute("PRAGMA query_only=ON")
    else:
        conn = sqlite3.connect(Path(db_path), timeout=300)
    conn.row_factory = sqlite3.Row
    return conn
