"""Guarded append-only recovery of supplier certification state.

The active functional warehouse version is immutable.  This runner repairs a
missing version-scoped supplier certification projection without rebuilding
WB, rewriting the version, or touching supplier/CNY/financial source rows.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from typing import Any, Iterable, Mapping

from packages.business_time import current_business_date_iso
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.warehouse_functional import (
    _supplier_cost_allocations,
    _supplier_cost_version_states,
    _watermark,
    ensure_warehouse_functional_schema,
)


CONTRACT_NAME = "sheet_vitrina_v1_warehouse_supplier_cost_state_replay"
CONTRACT_VERSION = "v1"
MIN_BACKUP_HEADROOM_BYTES = 64 * 1024 * 1024


class WarehouseSupplierCostStateReplayError(RuntimeError):
    pass


def build_supplier_cost_state_replay_plan(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    shipment_ids: Iterable[str] = (),
    business_date: str | None = None,
    _connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Build a deterministic exact plan from local canonical supplier sources."""

    operation_business_date = str(business_date or current_business_date_iso())[:10]
    selected = sorted(
        {
            str(shipment_id or "").strip()
            for shipment_id in shipment_ids
            if str(shipment_id or "").strip()
        }
    )
    owns_connection = _connection is None
    conn = _connection or _connect(runtime.db_path)
    try:
        if owns_connection:
            ensure_warehouse_functional_schema(conn)
        active = conn.execute(
            """SELECT active.version_id,version.plan_fingerprint,version.local_source_digest,
                      version.source_watermarks_json,version.effective_at,version.version_kind
               FROM sheet_vitrina_v1_warehouse_functional_active active
               JOIN sheet_vitrina_v1_warehouse_functional_versions version
                 ON version.version_id=active.version_id
               WHERE active.slot=1 AND version.status='good'"""
        ).fetchone()
        if active is None:
            raise WarehouseSupplierCostStateReplayError(
                "active good functional warehouse version is unavailable"
            )
        sources = _supplier_sources(conn)
        source_manifest_digest = "sha256:" + _hash(sources)
        current_allocations = _supplier_cost_allocations(sources)
        desired_states = _supplier_cost_version_states(sources)
        eligible = {
            str(state["shipment_id"]): dict(state)
            for state in desired_states
            if bool(state.get("expenses_complete"))
            and bool(state.get("calculation_available"))
        }
        frozen_states = _frozen_supplier_states_from_version(
            conn,
            version_id=str(active["version_id"]),
        )
        legacy_proof_required = any(
            str(state.get("proof_kind") or "") == "legacy_balance_conservation"
            for state in frozen_states.values()
        )
        frozen_supplier_source_watermarks = _json_object(
            active["source_watermarks_json"]
        )
        current_supplier_source_watermarks = _legacy_supplier_source_watermarks(sources)
        immutable_supplier_source_watermarks_match = bool(
            legacy_proof_required
            and _supplier_source_watermarks_match(
                frozen_supplier_source_watermarks,
                current_supplier_source_watermarks,
            )
        )
        if selected:
            missing = sorted(set(selected) - set(eligible))
            if missing:
                raise WarehouseSupplierCostStateReplayError(
                    "requested shipment has no closed calculation-ready canonical state: "
                    + ", ".join(missing)
                )
            target_ids = selected
        else:
            target_ids = sorted(eligible)
        current_states = {
            shipment_id: _latest_supplier_state(
                conn,
                version_id=str(active["version_id"]),
                shipment_id=shipment_id,
            )
            for shipment_id in target_ids
        }
        target_state_fingerprints = {
            shipment_id: _state_fingerprint(eligible[shipment_id])
            for shipment_id in target_ids
        }
        replay_history_digest = _replay_audit_digest(
            conn,
            version_id=str(active["version_id"]),
        )
        replay_id = "whscr_" + _hash(
            {
                "version_id": str(active["version_id"]),
                "source_manifest_digest": source_manifest_digest,
                "target_state_fingerprints": target_state_fingerprints,
                "replay_history_digest": replay_history_digest,
            }
        )[:24]
        replay_sequence_no = 1 + int(
            conn.execute(
                """SELECT COALESCE(MAX(sequence_no),0)
                   FROM sheet_vitrina_v1_warehouse_supplier_cost_state_replays
                   WHERE version_id=?""",
                (str(active["version_id"]),),
            ).fetchone()[0]
        )
        corrections: list[dict[str, Any]] = []
        blocked_shipments: list[dict[str, str]] = []
        legacy_target_revision_proofs: dict[str, dict[str, Any]] = {}
        for shipment_id in target_ids:
            desired = eligible[shipment_id]
            desired_fingerprint = target_state_fingerprints[shipment_id]
            current = current_states[shipment_id]
            current_fingerprint = (
                _state_fingerprint(current) if current is not None else "missing"
            )
            if current_fingerprint == desired_fingerprint:
                continue
            if current is not None:
                blocked_shipments.append(
                    {
                        "shipment_id": shipment_id,
                        "code": "existing_version_state_mismatch",
                        "reason": (
                            "current canonical sources do not match the certification state "
                            "already frozen in the immutable warehouse version"
                        ),
                    }
                )
                continue
            frozen = frozen_states.get(shipment_id)
            if frozen is None:
                blocked_shipments.append(
                    {
                        "shipment_id": shipment_id,
                        "code": "missing_immutable_version_proof",
                        "reason": (
                            "the immutable warehouse version has no supplier-flow provenance "
                            "that proves the missing certification fingerprints"
                        ),
                    }
                )
                continue
            proof_kind = str(frozen.get("proof_kind") or "")
            proof_matches = False
            mismatch_code = "canonical_sources_changed_after_version"
            mismatch_reason = (
                "current canonical supplier sources do not match the fingerprints "
                "frozen in the immutable warehouse calculation"
            )
            if proof_kind == "explicit_fingerprints":
                proof_matches = _state_fingerprint(frozen) == desired_fingerprint
            elif proof_kind == "legacy_balance_conservation":
                # Legacy versions persisted only version-wide source-group
                # watermarks.  A later upload/reparse of an unrelated or
                # informational financial document legitimately changes that
                # global watermark without changing this shipment's frozen
                # supplier flow.  Certify only when every mutable source row
                # contributing to the target predates the immutable version
                # and the current allocation still conserves every immutable
                # per-SKU quantity/capital and contributing source identity.
                # The complete current source manifest remains pinned by the
                # plan and is rechecked under BEGIN IMMEDIATE before apply.
                revision_proof = _legacy_target_source_revision_proof(
                    sources,
                    current_allocations[shipment_id],
                    version_effective_at=str(active["effective_at"]),
                )
                legacy_target_revision_proofs[shipment_id] = revision_proof
                proof_matches = bool(revision_proof["unchanged_since_version"])
                mismatch_code = "legacy_target_source_revision_after_version"
                mismatch_reason = (
                    "a source row contributing to the target allocation was created or "
                    "updated after the immutable warehouse version"
                )
                if proof_matches:
                    proof_matches = _legacy_balance_proof_matches_allocation(
                        frozen,
                        current_allocations[shipment_id],
                    )
                    mismatch_code = "legacy_target_conservation_proof_mismatch"
                    mismatch_reason = (
                        "current per-SKU quantity/capital or contributing source identities do "
                        "not match immutable legacy balance provenance"
                    )
            if not proof_matches:
                blocked_shipments.append(
                    {
                        "shipment_id": shipment_id,
                        "code": mismatch_code,
                        "reason": mismatch_reason,
                    }
                )
                continue
            corrections.append(
                {
                    "correction_id": "whscc_" + _hash(
                        {
                            "replay_id": replay_id,
                            "shipment_id": shipment_id,
                            "state_fingerprint": desired_fingerprint,
                        }
                    )[:24],
                    "replay_id": replay_id,
                    "version_id": str(active["version_id"]),
                    "shipment_id": shipment_id,
                    "source_fingerprint": str(desired["source_fingerprint"]),
                    "calculation_fingerprint": str(desired["calculation_fingerprint"]),
                    "expenses_complete": True,
                    "calculation_available": True,
                    "supersedes_state_fingerprint": current_fingerprint,
                    "state_fingerprint": desired_fingerprint,
                    "frozen_version_proof": dict(frozen.get("proof") or {}),
                }
            )
        if selected and blocked_shipments:
            raise WarehouseSupplierCostStateReplayError(
                "supplier certification replay is not reconstructable from the immutable version: "
                + "; ".join(
                    f"{item['shipment_id']}:{item['code']}" for item in blocked_shipments
                )
            )
        plan = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "dry_run_ready",
            "business_date": operation_business_date,
            "active_version_id": str(active["version_id"]),
            "active_version_kind": str(active["version_kind"]),
            "active_version_effective_at": str(active["effective_at"]),
            "supersedes_version_plan_fingerprint": str(active["plan_fingerprint"]),
            "active_version_local_source_digest": str(active["local_source_digest"]),
            "frozen_supplier_source_watermarks": frozen_supplier_source_watermarks,
            "current_supplier_source_watermarks": current_supplier_source_watermarks,
            "immutable_supplier_source_watermarks_match": (
                immutable_supplier_source_watermarks_match
            ),
            "source_manifest_digest": source_manifest_digest,
            "primary_source_digest": source_manifest_digest,
            "replay_history_digest": replay_history_digest,
            "non_target_derived_digest": _non_target_derived_digest(
                conn,
                version_id=str(active["version_id"]),
            ),
            "eligible_closed_shipment_count": len(eligible),
            "target_scope": "explicit" if selected else "all_eligible",
            "target_shipment_ids": target_ids,
            "target_state_fingerprints": target_state_fingerprints,
            "frozen_version_state_fingerprints": {
                shipment_id: (
                    _state_fingerprint(state)
                    if str(state.get("proof_kind") or "") == "explicit_fingerprints"
                    else "sha256:" + _hash(state.get("proof") or {})
                )
                for shipment_id, state in sorted(frozen_states.items())
                if shipment_id in target_ids
            },
            "legacy_target_revision_proofs": legacy_target_revision_proofs,
            "blocked_shipments": blocked_shipments,
            "replay_id": replay_id,
            "replay_sequence_no": replay_sequence_no,
            "correction_count": len(corrections),
            "blocked_shipment_count": len(blocked_shipments),
            "corrections": corrections,
            "provenance": {
                "reason": "missing supplier certification projection in an immutable active version",
                "source": "canonical supplier/CNY/financial Decimal allocation",
                "version_proof": (
                    "immutable supplier fingerprints or target-scoped source revision "
                    "cut plus exact legacy per-SKU balance/document conservation"
                ),
                "frozen_version_proofs": {
                    item["shipment_id"]: item["frozen_version_proof"]
                    for item in corrections
                },
                "mutation_scope": [
                    "warehouse_supplier_cost_state_replays",
                    "warehouse_supplier_cost_state_corrections",
                ],
                "primary_sources_mutated": False,
            },
        }
        plan["plan_fingerprint"] = _plan_fingerprint(plan)
        if current_business_date_iso() != operation_business_date:
            raise WarehouseSupplierCostStateReplayError(
                "supplier certification replay crossed the canonical business-date boundary"
            )
        return plan
    finally:
        if owns_connection:
            conn.close()


def apply_supplier_cost_state_replay_plan(
    runtime: RegistryUploadDbBackedRuntime,
    plan: Mapping[str, Any],
    *,
    confirm_fingerprint: str,
    backup_dir: Path,
) -> dict[str, Any]:
    """Apply one exact reviewed replay with backup and optimistic source recheck."""

    normalized = json.loads(json.dumps(dict(plan), ensure_ascii=False))
    fingerprint = str(normalized.get("plan_fingerprint") or "")
    if (
        normalized.get("contract_name") != CONTRACT_NAME
        or fingerprint != str(confirm_fingerprint or "")
        or fingerprint != _plan_fingerprint(
            {key: value for key, value in normalized.items() if key != "plan_fingerprint"}
        )
    ):
        raise WarehouseSupplierCostStateReplayError(
            "exact supplier certification replay plan fingerprint is required"
        )
    operation_business_date = str(normalized.get("business_date") or "")[:10]
    if current_business_date_iso() != operation_business_date:
        raise WarehouseSupplierCostStateReplayError(
            "supplier certification replay crossed the canonical business-date boundary"
        )
    with _connect(runtime.db_path) as conn:
        ensure_warehouse_functional_schema(conn)
        already_applied = conn.execute(
            """SELECT replay.replay_id,replay.version_id
               FROM sheet_vitrina_v1_warehouse_supplier_cost_state_replays replay
               LEFT JOIN sheet_vitrina_v1_warehouse_supplier_cost_state_replay_rollbacks rollback
                 ON rollback.replay_id=replay.replay_id
               WHERE replay.replay_plan_fingerprint=? AND rollback.replay_id IS NULL""",
            (fingerprint,),
        ).fetchone()
        if already_applied is not None:
            if str(already_applied["version_id"]) != str(normalized["active_version_id"]):
                raise WarehouseSupplierCostStateReplayError(
                    "applied supplier certification replay version identity mismatch"
                )
            active = conn.execute(
                "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
            ).fetchone()
            if active is None or str(active["version_id"]) != str(
                normalized["active_version_id"]
            ):
                raise WarehouseSupplierCostStateReplayError(
                    "active functional version changed after the applied replay"
                )
            if _supplier_source_digest(conn) != str(normalized["primary_source_digest"]):
                raise WarehouseSupplierCostStateReplayError(
                    "canonical supplier sources changed after the applied replay"
                )
            stored = _rows(
                conn,
                """SELECT correction_id,state_fingerprint
                   FROM sheet_vitrina_v1_warehouse_supplier_cost_state_corrections
                   WHERE replay_id=? ORDER BY correction_id""",
                (str(already_applied["replay_id"]),),
            )
            expected = sorted(
                [
                    {
                        "correction_id": str(item["correction_id"]),
                        "state_fingerprint": str(item["state_fingerprint"]),
                    }
                    for item in normalized.get("corrections") or []
                ],
                key=lambda item: item["correction_id"],
            )
            if stored != expected:
                raise WarehouseSupplierCostStateReplayError(
                    "applied supplier certification replay audit rows do not match the exact plan"
                )
            return {
                **normalized,
                "status": "applied",
                "idempotent": True,
                "database_written": False,
                "primary_source_digest_after": normalized["primary_source_digest"],
            }
    fresh = build_supplier_cost_state_replay_plan(
        runtime,
        shipment_ids=_plan_shipment_selection(normalized),
        business_date=operation_business_date,
    )
    if str(fresh["plan_fingerprint"]) != fingerprint:
        raise WarehouseSupplierCostStateReplayError(
            "active version or canonical supplier sources drifted after dry-run"
        )
    if not normalized.get("corrections"):
        return {
            **fresh,
            "status": "applied",
            "idempotent": True,
            "database_written": False,
            "primary_source_digest_after": fresh["primary_source_digest"],
        }

    backup_root = Path(backup_dir)
    if not backup_root.is_absolute():
        raise WarehouseSupplierCostStateReplayError("absolute backup_dir is required")
    backup_root.mkdir(parents=True, exist_ok=True)
    backup, free_before, database_size = _create_verified_backup(
        runtime,
        root=backup_root,
        prefix="supplier-cost-state-replay",
        fingerprint=fingerprint,
    )
    committed = False
    try:
        with _connect(runtime.db_path) as conn:
            ensure_warehouse_functional_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                locked = build_supplier_cost_state_replay_plan(
                    runtime,
                    shipment_ids=_plan_shipment_selection(normalized),
                    business_date=operation_business_date,
                    _connection=conn,
                )
                if str(locked["plan_fingerprint"]) != fingerprint:
                    raise WarehouseSupplierCostStateReplayError(
                        "active version or canonical supplier sources drifted before atomic replay"
                    )
                now = _now()
                conn.execute(
                    """INSERT INTO sheet_vitrina_v1_warehouse_supplier_cost_state_replays(
                           replay_id,version_id,sequence_no,supersedes_version_plan_fingerprint,
                           replay_plan_fingerprint,source_manifest_digest,target_shipment_ids_json,
                           state_fingerprints_json,provenance_json,backup_json,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        normalized["replay_id"],
                        normalized["active_version_id"],
                        int(normalized["replay_sequence_no"]),
                        normalized["supersedes_version_plan_fingerprint"],
                        fingerprint,
                        normalized["source_manifest_digest"],
                        _json(normalized["target_shipment_ids"]),
                        _json(normalized["target_state_fingerprints"]),
                        _json(normalized["provenance"]),
                        _json(backup),
                        now,
                    ),
                )
                for correction in normalized["corrections"]:
                    conn.execute(
                        """INSERT INTO sheet_vitrina_v1_warehouse_supplier_cost_state_corrections(
                               correction_id,replay_id,version_id,shipment_id,source_fingerprint,
                               calculation_fingerprint,expenses_complete,calculation_available,
                               supersedes_state_fingerprint,state_fingerprint,created_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            correction["correction_id"],
                            correction["replay_id"],
                            correction["version_id"],
                            correction["shipment_id"],
                            correction["source_fingerprint"],
                            correction["calculation_fingerprint"],
                            int(bool(correction["expenses_complete"])),
                            int(bool(correction["calculation_available"])),
                            correction["supersedes_state_fingerprint"],
                            correction["state_fingerprint"],
                            now,
                        ),
                    )
                if _supplier_source_digest(conn) != str(normalized["primary_source_digest"]):
                    raise WarehouseSupplierCostStateReplayError(
                        "primary supplier source digest changed during atomic replay"
                    )
                if _non_target_derived_digest(
                    conn,
                    version_id=str(normalized["active_version_id"]),
                ) != str(normalized["non_target_derived_digest"]):
                    raise WarehouseSupplierCostStateReplayError(
                        "non-target functional warehouse state changed during atomic replay"
                    )
                for correction in normalized["corrections"]:
                    stored = _latest_supplier_state(
                        conn,
                        version_id=str(correction["version_id"]),
                        shipment_id=str(correction["shipment_id"]),
                    )
                    if stored is None or _state_fingerprint(stored) != str(
                        correction["state_fingerprint"]
                    ):
                        raise WarehouseSupplierCostStateReplayError(
                            "supplier certification replay in-transaction readback failed"
                        )
                if current_business_date_iso() != operation_business_date:
                    raise WarehouseSupplierCostStateReplayError(
                        "supplier certification replay crossed the business-date boundary before commit"
                    )
                conn.commit()
                committed = True
            except Exception:
                conn.rollback()
                raise
    except Exception:
        if not committed:
            _discard_uncommitted_backup(backup)
        raise
    readback = build_supplier_cost_state_replay_plan(
        runtime,
        shipment_ids=_plan_shipment_selection(normalized),
        business_date=operation_business_date,
    )
    if readback.get("corrections"):
        raise WarehouseSupplierCostStateReplayError(
            "supplier certification replay is not idempotent"
        )
    return {
        **readback,
        "status": "applied",
        "idempotent": False,
        "database_written": committed,
        "applied_correction_count": len(normalized["corrections"]),
        "applied_plan_fingerprint": fingerprint,
        "backup": backup,
        "free_space_bytes_before_backup": free_before,
        "database_size_bytes": database_size,
        "primary_source_digest_before": normalized["primary_source_digest"],
        "primary_source_digest_after": readback["primary_source_digest"],
    }


def rollback_supplier_cost_state_replay(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    replay_plan_fingerprint: str,
    reason: str,
    backup_dir: Path,
) -> dict[str, Any]:
    """Append one exact rollback tombstone; never delete replay audit rows."""

    selected = str(replay_plan_fingerprint or "").strip()
    rollback_reason = str(reason or "").strip()
    if not selected or not rollback_reason:
        raise WarehouseSupplierCostStateReplayError(
            "exact replay plan fingerprint and rollback reason are required"
        )
    with _connect(runtime.db_path) as conn:
        ensure_warehouse_functional_schema(conn)
        replay = conn.execute(
            """SELECT * FROM sheet_vitrina_v1_warehouse_supplier_cost_state_replays
               WHERE replay_plan_fingerprint=?""",
            (selected,),
        ).fetchone()
        if replay is None:
            raise WarehouseSupplierCostStateReplayError("supplier certification replay is unknown")
        existing = conn.execute(
            """SELECT * FROM sheet_vitrina_v1_warehouse_supplier_cost_state_replay_rollbacks
               WHERE replay_id=?""",
            (replay["replay_id"],),
        ).fetchone()
        if existing is not None:
            expected_rollback_fingerprint = "sha256:" + _hash(
                {
                    "replay_plan_fingerprint": selected,
                    "reason": rollback_reason,
                    "primary_source_digest": str(existing["primary_source_digest"]),
                }
            )
            if (
                str(existing["reason"]) != rollback_reason
                or str(existing["replay_plan_fingerprint"]) != selected
                or str(existing["rollback_fingerprint"]) != expected_rollback_fingerprint
            ):
                raise WarehouseSupplierCostStateReplayError(
                    "existing supplier certification rollback does not match the exact audit request"
                )
            return {
                "status": "rolled_back",
                "idempotent": True,
                "database_written": False,
                "rollback_id": str(existing["rollback_id"]),
                "rollback_fingerprint": str(existing["rollback_fingerprint"]),
            }
        later = conn.execute(
            """SELECT later.replay_id
               FROM sheet_vitrina_v1_warehouse_supplier_cost_state_corrections target
               JOIN sheet_vitrina_v1_warehouse_supplier_cost_state_corrections candidate
                 ON candidate.version_id=target.version_id
                AND candidate.shipment_id=target.shipment_id
               JOIN sheet_vitrina_v1_warehouse_supplier_cost_state_replays later
                 ON later.replay_id=candidate.replay_id
               LEFT JOIN sheet_vitrina_v1_warehouse_supplier_cost_state_replay_rollbacks rolled
                 ON rolled.replay_id=later.replay_id
               WHERE target.replay_id=? AND later.sequence_no>? AND rolled.replay_id IS NULL
               LIMIT 1""",
            (replay["replay_id"], replay["sequence_no"]),
        ).fetchone()
        if later is not None:
            raise WarehouseSupplierCostStateReplayError(
                "cannot rollback a replay superseded by a later active correction"
            )
        primary_before = _supplier_source_digest(conn)
        non_target_before = _non_target_derived_digest(
            conn,
            version_id=str(replay["version_id"]),
        )
        replay_audit_before = _replay_audit_digest(
            conn,
            version_id=str(replay["version_id"]),
        )
    backup_root = Path(backup_dir)
    if not backup_root.is_absolute():
        raise WarehouseSupplierCostStateReplayError("absolute backup_dir is required")
    backup_root.mkdir(parents=True, exist_ok=True)
    backup, free_before, database_size = _create_verified_backup(
        runtime,
        root=backup_root,
        prefix="supplier-cost-state-rollback",
        fingerprint=selected,
    )
    rollback_fingerprint = "sha256:" + _hash(
        {
            "replay_plan_fingerprint": selected,
            "reason": rollback_reason,
            "primary_source_digest": primary_before,
        }
    )
    rollback_id = "whscrb_" + rollback_fingerprint.removeprefix("sha256:")[:24]
    committed = False
    try:
        with _connect(runtime.db_path) as conn:
            ensure_warehouse_functional_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                if _supplier_source_digest(conn) != primary_before:
                    raise WarehouseSupplierCostStateReplayError(
                        "primary supplier sources drifted before rollback"
                    )
                if _non_target_derived_digest(
                    conn,
                    version_id=str(replay["version_id"]),
                ) != non_target_before:
                    raise WarehouseSupplierCostStateReplayError(
                        "non-target functional warehouse state drifted before rollback"
                    )
                if _replay_audit_digest(
                    conn,
                    version_id=str(replay["version_id"]),
                ) != replay_audit_before:
                    raise WarehouseSupplierCostStateReplayError(
                        "supplier certification replay audit drifted before rollback"
                    )
                conn.execute(
                    """INSERT INTO sheet_vitrina_v1_warehouse_supplier_cost_state_replay_rollbacks(
                           rollback_id,replay_id,replay_plan_fingerprint,rollback_fingerprint,
                           reason,primary_source_digest,backup_json,created_at
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        rollback_id,
                        replay["replay_id"],
                        selected,
                        rollback_fingerprint,
                        rollback_reason,
                        primary_before,
                        _json(backup),
                        _now(),
                    ),
                )
                if _supplier_source_digest(conn) != primary_before:
                    raise WarehouseSupplierCostStateReplayError(
                        "primary supplier source digest changed during rollback"
                    )
                conn.commit()
                committed = True
            except Exception:
                conn.rollback()
                raise
    except Exception:
        if not committed:
            _discard_uncommitted_backup(backup)
        raise
    return {
        "status": "rolled_back",
        "idempotent": False,
        "database_written": True,
        "rollback_id": rollback_id,
        "rollback_fingerprint": rollback_fingerprint,
        "backup": backup,
        "free_space_bytes_before_backup": free_before,
        "database_size_bytes": database_size,
        "primary_source_digest_before": primary_before,
        "primary_source_digest_after": _supplier_source_digest_from_runtime(runtime),
    }


def _supplier_sources(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    required = {
        "sheet_vitrina_v1_supplier_shipments",
        "sheet_vitrina_v1_supplier_shipment_lines",
        "sheet_vitrina_v1_cny_ledger_operations",
        "sheet_vitrina_v1_supplier_financial_documents",
        "sheet_vitrina_v1_supplier_financial_expense_lines",
    }
    missing = sorted(required - tables)
    if missing:
        raise WarehouseSupplierCostStateReplayError(
            "canonical supplier source tables are unavailable: " + ", ".join(missing)
        )
    return {
        "shipments": _rows(
            conn,
            "SELECT * FROM sheet_vitrina_v1_supplier_shipments ORDER BY shipment_id",
        ),
        "shipment_lines": _rows(
            conn,
            """SELECT * FROM sheet_vitrina_v1_supplier_shipment_lines
               ORDER BY shipment_id,sort_order,line_id""",
        ),
        "cny_operations": _rows(
            conn,
            """SELECT * FROM sheet_vitrina_v1_cny_ledger_operations
               ORDER BY sequence_key,operation_id""",
        ),
        "financial_documents": _rows(
            conn,
            """SELECT * FROM sheet_vitrina_v1_supplier_financial_documents
               ORDER BY supplier_order_id,document_date,document_id""",
        ),
        "financial_expense_lines": _rows(
            conn,
            """SELECT * FROM sheet_vitrina_v1_supplier_financial_expense_lines
               ORDER BY supplier_order_id,financial_document_id,sort_order,line_id""",
        ),
        "cny_documents": (
            _rows(
                conn,
                """SELECT * FROM sheet_vitrina_v1_cny_documents
                   ORDER BY source_order_id,operation_date,operation_datetime,document_id""",
            )
            if "sheet_vitrina_v1_cny_documents" in tables
            else []
        ),
    }


def _latest_supplier_state(
    conn: sqlite3.Connection,
    *,
    version_id: str,
    shipment_id: str,
) -> dict[str, Any] | None:
    correction = conn.execute(
        """SELECT correction.*
           FROM sheet_vitrina_v1_warehouse_supplier_cost_state_corrections correction
           JOIN sheet_vitrina_v1_warehouse_supplier_cost_state_replays replay
             ON replay.replay_id=correction.replay_id
           LEFT JOIN sheet_vitrina_v1_warehouse_supplier_cost_state_replay_rollbacks rollback
             ON rollback.replay_id=replay.replay_id
           WHERE correction.version_id=? AND correction.shipment_id=?
             AND rollback.replay_id IS NULL
           ORDER BY replay.sequence_no DESC LIMIT 1""",
        (version_id, shipment_id),
    ).fetchone()
    if correction is not None:
        return dict(correction)
    base = conn.execute(
        """SELECT * FROM sheet_vitrina_v1_warehouse_supplier_cost_states
           WHERE version_id=? AND shipment_id=?""",
        (version_id, shipment_id),
    ).fetchone()
    return dict(base) if base is not None else None


def _frozen_supplier_states_from_version(
    conn: sqlite3.Connection,
    *,
    version_id: str,
) -> dict[str, dict[str, Any]]:
    """Recover explicit or legacy supplier proof from immutable balances."""

    candidates: dict[str, list[dict[str, Any]]] = {}
    rows = conn.execute(
        """SELECT warehouse_key,nm_id,provenance_json
           FROM sheet_vitrina_v1_warehouse_functional_balances
           WHERE version_id=? ORDER BY warehouse_key,nm_id""",
        (version_id,),
    ).fetchall()
    for row in rows:
        try:
            provenance = json.loads(str(row["provenance_json"] or "{}"))
        except json.JSONDecodeError as exc:
            raise WarehouseSupplierCostStateReplayError(
                "immutable warehouse balance provenance is not valid JSON"
            ) from exc
        for record in _mapping_tree(provenance):
            shipment_id = str(record.get("shipment_id") or "").strip()
            source_fingerprint = str(
                record.get("certified_source_fingerprint")
                or record.get("source_fingerprint")
                or ""
            ).strip()
            calculation_fingerprint = str(
                record.get("certified_calculation_fingerprint")
                or record.get("calculation_fingerprint")
                or ""
            ).strip()
            if (
                not shipment_id
                or not bool(record.get("expenses_complete_certification"))
            ):
                continue
            candidates.setdefault(shipment_id, []).append(
                {
                    "shipment_id": shipment_id,
                    "source_fingerprint": source_fingerprint,
                    "calculation_fingerprint": calculation_fingerprint,
                    "expenses_complete": True,
                    "calculation_available": True,
                    "warehouse_key": str(row["warehouse_key"]),
                    "nm_id": int(row["nm_id"]),
                    "flow_quantity": str(record.get("flow_quantity") or ""),
                    "flow_capital_rub": str(record.get("flow_capital_rub") or ""),
                    "invoice_no": str(record.get("invoice_no") or ""),
                    "invoice_date": str(record.get("invoice_date") or "")[:10],
                    "actual_shipment_date": str(
                        record.get("actual_shipment_date") or ""
                    )[:10],
                    "payment_operation_ids": sorted(
                        str(value) for value in record.get("payment_operation_ids") or []
                    ),
                    "cny_fee_operation_ids": sorted(
                        str(value) for value in record.get("cny_fee_operation_ids") or []
                    ),
                    "direct_rub_bank_fees": str(
                        record.get("direct_rub_bank_fees") or "0"
                    ),
                    "china_expense_sources": sorted(
                        str(value) for value in record.get("china_expense_sources") or []
                    ),
                    "allocation": str(record.get("allocation") or ""),
                }
            )
    result: dict[str, dict[str, Any]] = {}
    for shipment_id, records in sorted(candidates.items()):
        explicit_identities = {
            (
                str(record["source_fingerprint"]),
                str(record["calculation_fingerprint"]),
            )
            for record in records
            if str(record["source_fingerprint"])
            and str(record["calculation_fingerprint"])
        }
        if len(explicit_identities) > 1:
            raise WarehouseSupplierCostStateReplayError(
                "immutable warehouse version has ambiguous supplier fingerprints: "
                + shipment_id
            )
        balance_lines: dict[int, dict[str, str]] = {}
        for record in records:
            nm_id = int(record["nm_id"])
            line = {
                "warehouse_key": str(record["warehouse_key"]),
                "quantity": str(record["flow_quantity"]),
                "capital_rub": str(record["flow_capital_rub"]),
            }
            if nm_id in balance_lines and balance_lines[nm_id] != line:
                raise WarehouseSupplierCostStateReplayError(
                    "immutable warehouse version has ambiguous supplier balance proof: "
                    + f"{shipment_id}:{nm_id}"
                )
            balance_lines[nm_id] = line
        metadata: dict[str, Any] = {}
        for key in (
            "invoice_no",
            "invoice_date",
            "actual_shipment_date",
            "payment_operation_ids",
            "cny_fee_operation_ids",
            "direct_rub_bank_fees",
            "china_expense_sources",
            "allocation",
        ):
            values = {_json(record[key]) for record in records}
            if len(values) != 1:
                raise WarehouseSupplierCostStateReplayError(
                    "immutable warehouse version has ambiguous supplier provenance: "
                    + f"{shipment_id}:{key}"
                )
            metadata[key] = records[0][key]
        source_fingerprint, calculation_fingerprint = (
            next(iter(explicit_identities)) if explicit_identities else ("", "")
        )
        result[shipment_id] = {
            "shipment_id": shipment_id,
            "source_fingerprint": source_fingerprint,
            "calculation_fingerprint": calculation_fingerprint,
            "expenses_complete": True,
            "calculation_available": True,
            "proof_kind": (
                "explicit_fingerprints"
                if explicit_identities
                else "legacy_balance_conservation"
            ),
            "proof": {
                "version_id": version_id,
                "balance_locations": sorted(
                    {
                        f"{record['warehouse_key']}:{record['nm_id']}"
                        for record in records
                    }
                ),
                "balance_lines": {
                    str(nm_id): balance_lines[nm_id]
                    for nm_id in sorted(balance_lines)
                },
                **metadata,
            },
        }
    return result


def _legacy_supplier_source_watermarks(
    sources: Mapping[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    """Rebuild only source groups durably fingerprinted by legacy versions.

    The version-wide local digest also includes WB, FF and historical derived
    projections.  Those can legitimately advance after the immutable supplier
    balance was calculated, so they are not evidence that a supplier cost
    changed.  Legacy versions did persist exact watermarks for the three
    primary source groups used here; detailed line/document conservation below
    covers their child rows and allocation result.
    """

    shipments = sorted(
        (dict(row) for row in sources.get("shipments") or []),
        key=lambda row: str(row.get("shipment_id") or ""),
    )
    cny_operations = sorted(
        (dict(row) for row in sources.get("cny_operations") or []),
        key=lambda row: (
            str(row.get("sequence_key") or ""),
            str(row.get("operation_id") or ""),
        ),
    )
    financial_documents = sorted(
        (dict(row) for row in sources.get("financial_documents") or []),
        key=lambda row: (
            str(row.get("document_date") or ""),
            str(row.get("document_id") or ""),
        ),
    )
    return {
        "supplier_shipments": _watermark(shipments, "updated_at"),
        "cny_ledger": _watermark(cny_operations, "updated_at"),
        "financial_documents": _watermark(financial_documents, "updated_at"),
    }


def _supplier_source_watermarks_match(
    frozen: Mapping[str, Any],
    current: Mapping[str, Any],
) -> bool:
    """Require every persisted primary watermark field to match exactly."""

    for key in ("supplier_shipments", "cny_ledger", "financial_documents"):
        frozen_value = frozen.get(key)
        current_value = current.get(key)
        if not isinstance(frozen_value, Mapping) or not isinstance(
            current_value, Mapping
        ):
            return False
        try:
            frozen_identity = {
                "row_count": int(frozen_value.get("row_count")),
                "max": str(frozen_value.get("max") or ""),
                "digest": str(frozen_value.get("digest") or ""),
            }
            current_identity = {
                "row_count": int(current_value.get("row_count")),
                "max": str(current_value.get("max") or ""),
                "digest": str(current_value.get("digest") or ""),
            }
        except (TypeError, ValueError):
            return False
        digest = frozen_identity["digest"]
        if (
            not digest.startswith("sha256:")
            or len(digest) != 71
            or frozen_identity != current_identity
        ):
            return False
    return True


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or ""))
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _legacy_balance_proof_matches_allocation(
    frozen: Mapping[str, Any],
    allocation: Mapping[str, Any],
) -> bool:
    """Require old per-SKU capital and every legacy component identity to match."""

    proof = dict(frozen.get("proof") or {})
    frozen_lines = {
        int(nm_id): dict(line)
        for nm_id, line in dict(proof.get("balance_lines") or {}).items()
    }
    current_lines: dict[int, dict[str, Decimal]] = {}
    components: dict[str, dict[str, Any]] = {}
    for line in allocation.get("lines") or []:
        nm_id = int(line.get("nm_id") or 0)
        target = current_lines.setdefault(
            nm_id,
            {"quantity": Decimal("0"), "capital_rub": Decimal("0")},
        )
        target["quantity"] += Decimal(str(line.get("quantity") or "0"))
        target["capital_rub"] += Decimal(str(line.get("capital_rub") or "0"))
        for component in line.get("components") or []:
            component_id = str(component.get("source_component_id") or "")
            if component_id:
                components.setdefault(component_id, dict(component))
    if set(current_lines) != set(frozen_lines):
        return False
    for nm_id, current in current_lines.items():
        frozen_line = frozen_lines[nm_id]
        # ``warehouse_key`` is where the immutable balance happened to retain
        # the nested supplier-flow provenance.  Once a lot reaches FF/WB that
        # outer bucket is intentionally later than the original supplier flow
        # stage, while ``flow_quantity`` and ``flow_capital_rub`` remain the
        # exact immutable receipt proof.  Treat the location as audit evidence,
        # not as an equality constraint on the supplier allocation stage.
        if not _decimal_equal(current["quantity"], frozen_line.get("quantity")):
            return False
        if not _decimal_equal(current["capital_rub"], frozen_line.get("capital_rub")):
            return False
    payment_ids: list[str] = []
    cny_fee_ids: list[str] = []
    direct_rub_fees = Decimal("0")
    china_sources: list[str] = []
    for component_id, component in sorted(components.items()):
        component_key = str(component.get("component_key") or "")
        if component_id.startswith("cny_operation:"):
            operation_id = component_id.split(":", 1)[1]
            if component_key == "supplier_payment":
                payment_ids.append(operation_id)
            elif component_key == "bank_fee":
                cny_fee_ids.append(operation_id)
        elif component_id.startswith("expense_line:"):
            line_id = component_id.split(":", 1)[1]
            if component_key == "bank_fee":
                direct_rub_fees += Decimal(
                    str(component.get("source_amount_rub") or "0")
                )
            else:
                document_id = str(
                    dict(component.get("document") or {}).get("document_id") or ""
                )
                if not document_id:
                    return False
                china_sources.append(f"{document_id}:{line_id}")
    return bool(
        str(proof.get("invoice_no") or "") == str(allocation.get("invoice_no") or "")
        and str(proof.get("invoice_date") or "")[:10]
        == str(allocation.get("invoice_date") or "")[:10]
        and str(proof.get("actual_shipment_date") or "")[:10]
        == str(allocation.get("actual_shipment_date") or "")[:10]
        and sorted(proof.get("payment_operation_ids") or []) == sorted(payment_ids)
        and sorted(proof.get("cny_fee_operation_ids") or []) == sorted(cny_fee_ids)
        and _decimal_equal(proof.get("direct_rub_bank_fees"), direct_rub_fees)
        and sorted(proof.get("china_expense_sources") or []) == sorted(china_sources)
    )


def _legacy_target_source_revision_proof(
    sources: Mapping[str, list[dict[str, Any]]],
    allocation: Mapping[str, Any],
    *,
    version_effective_at: str,
) -> dict[str, Any]:
    """Prove that every mutable row driving one legacy allocation is not newer.

    Legacy versions did not persist target-scoped source fingerprints.  Their
    exact balance provenance proves the arithmetic and contributing IDs; the
    server-owned revision timestamps prove those contributing mutable rows
    were strictly older than the immutable version.  Equality is ambiguous at
    whole-second precision and therefore fails closed.  Informational
    documents outside the canonical allocation are deliberately not targets.
    """

    boundary = _timestamp(version_effective_at)
    shipment_id = str(allocation.get("shipment_id") or "")
    target_rows: list[tuple[str, str, Mapping[str, Any]]] = []
    blockers: list[dict[str, str]] = []

    shipments = {
        str(row.get("shipment_id") or ""): row
        for row in sources.get("shipments") or []
    }
    shipment = shipments.get(shipment_id)
    if shipment is None:
        blockers.append({"source_kind": "shipment", "source_id": shipment_id, "code": "missing"})
    else:
        target_rows.append(("shipment", shipment_id, shipment))

    operation_ids: set[str] = set()
    expense_line_ids: set[str] = set()
    for line in allocation.get("lines") or []:
        for component in line.get("components") or []:
            component_id = str(component.get("source_component_id") or "")
            if component_id.startswith("cny_operation:"):
                operation_ids.add(component_id.split(":", 1)[1])
            elif component_id.startswith("expense_line:"):
                expense_line_ids.add(component_id.split(":", 1)[1])

    operations = {
        str(row.get("operation_id") or ""): row
        for row in sources.get("cny_operations") or []
    }
    cny_document_ids: set[str] = set()
    for operation_id in sorted(operation_ids):
        operation = operations.get(operation_id)
        if operation is None:
            blockers.append(
                {"source_kind": "cny_operation", "source_id": operation_id, "code": "missing"}
            )
            continue
        target_rows.append(("cny_operation", operation_id, operation))
        source_document_id = str(operation.get("source_document_id") or "")
        if source_document_id:
            cny_document_ids.add(source_document_id)

    cny_documents = {
        str(row.get("document_id") or ""): row
        for row in sources.get("cny_documents") or []
    }
    for document_id in sorted(cny_document_ids):
        document = cny_documents.get(document_id)
        if document is None:
            blockers.append(
                {"source_kind": "cny_document", "source_id": document_id, "code": "missing"}
            )
        else:
            target_rows.append(("cny_document", document_id, document))

    expense_lines = {
        str(row.get("line_id") or ""): row
        for row in sources.get("financial_expense_lines") or []
    }
    financial_document_ids: set[str] = set()
    for line_id in sorted(expense_line_ids):
        expense = expense_lines.get(line_id)
        if expense is None:
            blockers.append(
                {"source_kind": "financial_expense_line", "source_id": line_id, "code": "missing"}
            )
            continue
        financial_document_ids.add(str(expense.get("financial_document_id") or ""))
    financial_documents = {
        str(row.get("document_id") or ""): row
        for row in sources.get("financial_documents") or []
    }
    for document_id in sorted(financial_document_ids):
        document = financial_documents.get(document_id)
        if not document_id or document is None:
            blockers.append(
                {
                    "source_kind": "financial_document",
                    "source_id": document_id or "missing-id",
                    "code": "missing",
                }
            )
        else:
            target_rows.append(("financial_document", document_id, document))

    if boundary is None:
        blockers.append(
            {
                "source_kind": "warehouse_version",
                "source_id": "effective_at",
                "code": "invalid_timestamp",
            }
        )
    for source_kind, source_id, row in target_rows:
        updated_at = _timestamp(row.get("updated_at"))
        if updated_at is None:
            blockers.append(
                {"source_kind": source_kind, "source_id": source_id, "code": "invalid_timestamp"}
            )
        elif boundary is not None and updated_at >= boundary:
            blockers.append(
                {
                    "source_kind": source_kind,
                    "source_id": source_id,
                    "code": "not_strictly_older_than_version",
                }
            )
    return {
        "unchanged_since_version": not blockers,
        "version_effective_at": str(version_effective_at),
        "checked_source_count": len(target_rows),
        "blockers": blockers,
    }


def _timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _decimal_equal(left: Any, right: Any) -> bool:
    return abs(Decimal(str(left or "0")) - Decimal(str(right or "0"))) <= Decimal(
        "0.000000000000000001"
    )


def _mapping_tree(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for nested in value.values():
            yield from _mapping_tree(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _mapping_tree(nested)


def _state_fingerprint(state: Mapping[str, Any]) -> str:
    return "sha256:" + _hash(
        {
            "shipment_id": str(state.get("shipment_id") or ""),
            "source_fingerprint": str(state.get("source_fingerprint") or ""),
            "calculation_fingerprint": str(state.get("calculation_fingerprint") or ""),
            "expenses_complete": bool(state.get("expenses_complete")),
            "calculation_available": bool(state.get("calculation_available")),
        }
    )


def _non_target_derived_digest(conn: sqlite3.Connection, *, version_id: str) -> str:
    manifest = {
        "active": _rows(
            conn,
            "SELECT * FROM sheet_vitrina_v1_warehouse_functional_active ORDER BY slot",
        ),
        "version": _rows(
            conn,
            """SELECT * FROM sheet_vitrina_v1_warehouse_functional_versions
               WHERE version_id=? ORDER BY version_id""",
            (version_id,),
        ),
        "balances": _rows(
            conn,
            """SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances
               WHERE version_id=? ORDER BY warehouse_key,nm_id""",
            (version_id,),
        ),
        "snapshots": _rows(
            conn,
            """SELECT * FROM sheet_vitrina_v1_warehouse_wb_snapshots
               WHERE version_id=? ORDER BY snapshot_id""",
            (version_id,),
        ),
        "documents": _rows(
            conn,
            """SELECT * FROM sheet_vitrina_v1_warehouse_functional_documents
               WHERE version_id=? ORDER BY document_id""",
            (version_id,),
        ),
        "document_lines": _rows(
            conn,
            """SELECT * FROM sheet_vitrina_v1_warehouse_functional_document_lines
               WHERE version_id=? ORDER BY line_id""",
            (version_id,),
        ),
        "base_supplier_states": _rows(
            conn,
            """SELECT * FROM sheet_vitrina_v1_warehouse_supplier_cost_states
               WHERE version_id=? ORDER BY shipment_id""",
            (version_id,),
        ),
    }
    return "sha256:" + _hash(manifest)


def _supplier_source_digest(conn: sqlite3.Connection) -> str:
    return "sha256:" + _hash(_supplier_sources(conn))


def _replay_audit_digest(conn: sqlite3.Connection, *, version_id: str) -> str:
    return "sha256:" + _hash(
        {
            "replays": _rows(
                conn,
                """SELECT replay_id,sequence_no,replay_plan_fingerprint,source_manifest_digest,
                          target_shipment_ids_json,state_fingerprints_json,created_at
                   FROM sheet_vitrina_v1_warehouse_supplier_cost_state_replays
                   WHERE version_id=? ORDER BY sequence_no""",
                (version_id,),
            ),
            "corrections": _rows(
                conn,
                """SELECT correction_id,replay_id,shipment_id,supersedes_state_fingerprint,
                          state_fingerprint,created_at
                   FROM sheet_vitrina_v1_warehouse_supplier_cost_state_corrections
                   WHERE version_id=? ORDER BY replay_id,correction_id""",
                (version_id,),
            ),
            "rollbacks": _rows(
                conn,
                """SELECT rollback.replay_id,rollback.rollback_fingerprint,rollback.created_at
                   FROM sheet_vitrina_v1_warehouse_supplier_cost_state_replay_rollbacks rollback
                   JOIN sheet_vitrina_v1_warehouse_supplier_cost_state_replays replay
                     ON replay.replay_id=rollback.replay_id
                   WHERE replay.version_id=? ORDER BY replay.sequence_no""",
                (version_id,),
            ),
        }
    )


def _supplier_source_digest_from_runtime(runtime: RegistryUploadDbBackedRuntime) -> str:
    with _connect(runtime.db_path) as conn:
        return _supplier_source_digest(conn)


def _available_backup_destination(
    root: Path,
    *,
    prefix: str,
    fingerprint: str,
) -> Path:
    digest = str(fingerprint or "").removeprefix("sha256:")
    for length in (16, 24, 64):
        candidate = root / f"{prefix}-{digest[:length]}.sqlite3"
        if not candidate.exists():
            return candidate
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return root / f"{prefix}-{digest[:24]}-{timestamp}.sqlite3"


def _create_verified_backup(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    root: Path,
    prefix: str,
    fingerprint: str,
) -> tuple[dict[str, Any], int, int]:
    database_size = runtime.db_path.stat().st_size
    free_before = shutil.disk_usage(root).free
    if free_before < database_size + MIN_BACKUP_HEADROOM_BYTES:
        raise WarehouseSupplierCostStateReplayError(
            "insufficient free space for coherent supplier certification replay backup"
        )
    destination = _available_backup_destination(
        root,
        prefix=prefix,
        fingerprint=fingerprint,
    )
    backup = runtime.backup_database(destination)
    destination.chmod(0o600)
    if str(backup.get("integrity_check") or "").lower() != "ok":
        _discard_uncommitted_backup(backup)
        raise WarehouseSupplierCostStateReplayError(
            "supplier certification replay backup integrity_check failed"
        )
    return backup, free_before, database_size


def _discard_uncommitted_backup(backup: Mapping[str, Any] | None) -> None:
    path_value = str((backup or {}).get("path") or "")
    if not path_value:
        return
    path = Path(path_value)
    if not path.is_absolute():
        return
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        candidate.unlink(missing_ok=True)


def _rows(
    conn: sqlite3.Connection,
    query: str,
    parameters: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query, parameters).fetchall()]


def _plan_fingerprint(plan: Mapping[str, Any]) -> str:
    normalized = {key: value for key, value in plan.items() if key != "plan_fingerprint"}
    return "sha256:" + _hash(normalized)


def _plan_shipment_selection(plan: Mapping[str, Any]) -> list[str]:
    if str(plan.get("target_scope") or "") == "all_eligible":
        return []
    return [str(value) for value in plan.get("target_shipment_ids") or []]


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
