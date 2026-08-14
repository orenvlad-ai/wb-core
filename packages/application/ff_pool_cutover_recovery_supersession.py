"""Exact append-only supersession of one stale failed Stage 7C recovery.

The planner is query-only.  Apply changes only Recovery Policy metadata after
re-deriving the immutable proof under the shared warehouse writer lock.  It
does not replay opening, debit FBS stock, accept a shipment, call WB, or remove
the older checkpoint artifacts.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Mapping, Sequence

from packages.application.ff_pool_cutover import (
    MANIFESTS_TABLE,
    RECOVERY_EVENTS_TABLE,
    read_ff_pool_cutover_status,
)
from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
)
from packages.application.warehouse_domain_write_guard import (
    EVENTS_TABLE as WRITE_EPOCH_EVENTS_TABLE,
)
from packages.application.warehouse_functional_lock import (
    warehouse_functional_write_lock,
)
from packages.application.warehouse_recovery_policy import (
    RecoveryState,
    WarehouseRecoveryRegistry,
)


CONTRACT_NAME = "ff_pool_cutover_recovery_supersession_v1"
CONTRACT_VERSION = 1
RECOVERY_OPERATIONS_TABLE = "sheet_vitrina_v1_recovery_operations"
RECOVERY_TRANSITIONS_TABLE = "sheet_vitrina_v1_recovery_transitions"
RECOVERY_ARTIFACTS_TABLE = "sheet_vitrina_v1_recovery_artifacts"
RECOVERY_SUPERSESSIONS_TABLE = "sheet_vitrina_v1_recovery_supersessions"
REQUIRED_TARGET_NEXT_ACTION = "exact_ff_pool_cutover_readback_or_retry"


class FfPoolCutoverRecoverySupersessionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details = details


class FfPoolCutoverRecoverySupersession:
    def __init__(
        self,
        *,
        runtime_dir: Path,
        deployed_sha: str,
        timestamp_factory: Callable[[], str] | None = None,
    ) -> None:
        self.runtime = RegistryUploadDbBackedRuntime(
            runtime_dir=Path(runtime_dir).resolve()
        )
        self.deployed_sha = _commit_sha(deployed_sha)
        self.timestamp_factory = timestamp_factory or _utc_now

    def build_plan(self, operation_id: str) -> dict[str, Any]:
        """Build a stable proof without creating schema, rows, or files."""

        target_id = _operation_id(operation_id)
        blockers: list[dict[str, Any]] = []
        with closing(_open_query_only(self.runtime.db_path)) as conn:
            conn.execute("BEGIN")
            target = _operation(conn, target_id)
            if target is None:
                blockers.append(
                    {"code": "target_recovery_missing", "operation_id": target_id}
                )
                return _blocked_plan(target_id, blockers, self.deployed_sha)
            relation = _supersession(conn, target_id)
            if str(target["lifecycle_state"]) == RecoveryState.SUPERSEDED.value:
                if relation is None:
                    blockers.append(
                        {"code": "superseded_recovery_relation_missing"}
                    )
                    return _blocked_plan(target_id, blockers, self.deployed_sha)
                proof = _loads(relation["proof_json"], {})
                return {
                    "contract_name": CONTRACT_NAME,
                    "contract_version": CONTRACT_VERSION,
                    "deployed_sha": self.deployed_sha,
                    "mode": "dry_run_exact_supersession",
                    "status": "already_applied",
                    "apply_allowed": False,
                    "would_change": False,
                    "operation_id": target_id,
                    "superseding_operation_id": str(
                        relation["superseding_operation_id"]
                    ),
                    "fingerprint": str(relation["proof_fingerprint"]),
                    "proof": proof,
                    "blockers": [],
                    "effect": _effect(),
                }

            _validate_target(target, blockers=blockers)
            target_scope = _loads(target["target_scope_json"], {})
            old_epochs = _epoch_rows(
                conn,
                gate_fingerprint=str(target["plan_fingerprint"]),
                deployed_sha=str(target_scope.get("deployed_sha") or ""),
            )
            _validate_aborted_target_epochs(old_epochs, blockers=blockers)
            if _count(
                conn,
                MANIFESTS_TABLE,
                "cutover_id=?",
                (str(target_scope.get("cutover_id") or ""),),
            ):
                blockers.append({"code": "target_cutover_manifest_exists"})
            if _count(
                conn,
                RECOVERY_EVENTS_TABLE,
                "cutover_id=?",
                (str(target_scope.get("cutover_id") or ""),),
            ):
                blockers.append({"code": "target_cutover_recovery_event_exists"})

            target_artifacts = _target_artifacts(conn, target_id)
            _validate_target_artifacts(target_artifacts, blockers=blockers)
            target_transitions = _transition_rows(conn, target_id)
            if (
                not target_transitions
                or str(target_transitions[-1]["from_state"])
                != RecoveryState.MUTATION_RUNNING.value
                or str(target_transitions[-1]["to_state"])
                != RecoveryState.FAILED_RECOVERABLE.value
            ):
                blockers.append(
                    {"code": "target_failure_transition_is_not_exact"}
                )

            manifests = _manifest_rows(conn)
            if len(manifests) != 1:
                blockers.append(
                    {
                        "code": "canonical_cutover_manifest_ambiguous",
                        "count": len(manifests),
                    }
                )
                return _blocked_plan(target_id, blockers, self.deployed_sha)
            manifest = manifests[0]
            status = read_ff_pool_cutover_status(conn)
            _validate_current_cutover_status(
                status,
                manifest=manifest,
                blockers=blockers,
            )
            success_events = _cutover_recovery_rows(
                conn, str(manifest["cutover_id"])
            )
            successful_operation_id = _readback_operation_id(
                success_events, blockers=blockers
            )
            replacement = (
                _operation(conn, successful_operation_id)
                if successful_operation_id
                else None
            )
            if replacement is None:
                blockers.append(
                    {"code": "superseding_recovery_operation_missing"}
                )
                return _blocked_plan(target_id, blockers, self.deployed_sha)
            replacement_scope = _loads(replacement["target_scope_json"], {})
            _validate_replacement(
                target,
                replacement,
                replacement_scope=replacement_scope,
                manifest=manifest,
                blockers=blockers,
            )
            replacement_epochs = _epoch_rows(
                conn,
                gate_fingerprint=str(replacement["plan_fingerprint"]),
                deployed_sha=str(replacement_scope.get("deployed_sha") or ""),
            )
            _validate_released_replacement_epochs(
                replacement_epochs,
                expected_epoch_id=str(
                    dict(status.get("barrier") or {}).get("epoch_id") or ""
                ),
                blockers=blockers,
            )
            if str(replacement["created_at"]) <= str(target["updated_at"]):
                blockers.append({"code": "superseding_recovery_is_not_later"})
            if old_epochs and replacement_epochs and max(
                int(item["event_sequence"]) for item in old_epochs
            ) >= min(int(item["event_sequence"]) for item in replacement_epochs):
                blockers.append({"code": "superseding_epoch_is_not_later"})
            if blockers:
                return _blocked_plan(target_id, blockers, self.deployed_sha)

            proof = {
                "contract_name": CONTRACT_NAME,
                "contract_version": CONTRACT_VERSION,
                "runner_deployed_sha": self.deployed_sha,
                "target_operation_id": target_id,
                "superseding_operation_id": str(replacement["operation_id"]),
                "target_operation": {
                    "operation_id": target_id,
                    "plan_fingerprint": str(target["plan_fingerprint"]),
                    "state_version": int(target["state_version"]),
                    "checkpoint_digest": str(target["checkpoint_digest"]),
                },
                "superseding_operation": {
                    "operation_id": str(replacement["operation_id"]),
                    "plan_fingerprint": str(replacement["plan_fingerprint"]),
                    "state_version": int(replacement["state_version"]),
                    "after_digest": str(replacement["after_digest"]),
                },
                "target_scope": target_scope,
                "target_failure": {
                    "lifecycle": str(target["lifecycle_state"]),
                    "next_action": str(target["next_action"]),
                    "last_error": str(target["last_error"]),
                    "transition_chain_digest": _fingerprint(target_transitions),
                    "aborted_epoch_digest": _fingerprint(old_epochs),
                    "old_manifest_absent": True,
                    "old_recovery_events_absent": True,
                    "artifacts": target_artifacts,
                },
                "pre_change": {
                    "target_row_digest": _fingerprint(
                        {
                            "operation_id": target_id,
                            "lifecycle": str(target["lifecycle_state"]),
                            "state_version": int(target["state_version"]),
                            "next_action": str(target["next_action"]),
                            "writer_state": str(target["writer_state"]),
                            "rollback_available": int(
                                target["rollback_available"]
                            ),
                            "checkpoint_digest": str(
                                target["checkpoint_digest"]
                            ),
                        }
                    ),
                    "transition_chain_digest": _fingerprint(
                        target_transitions
                    ),
                    "artifact_registry_digest": _fingerprint(
                        target_artifacts
                    ),
                    "supersession_relation_absent": True,
                },
                "canonical_success": {
                    "cutover_id": str(manifest["cutover_id"]),
                    "manifest_digest": str(manifest["manifest_digest"]),
                    "deployed_sha": str(manifest["deployed_sha"]),
                    "created_at": str(manifest["created_at"]),
                    "feature_epoch": int(manifest["feature_epoch"]),
                    "aggregate_revision": str(manifest["aggregate_revision"]),
                    "aggregate_digest": str(manifest["aggregate_digest"]),
                    "detail_digest": str(manifest["detail_digest"]),
                    "non_target_digest": str(manifest["non_target_digest"]),
                    "released_epoch_digest": _fingerprint(replacement_epochs),
                    "recovery_events_digest": _fingerprint(success_events),
                    "exact_readback_passed": True,
                    "aggregate_detail_conserved": True,
                    "barrier_released": True,
                    "reader_enabled": True,
                },
                "non_targets": {
                    "business_rows_changed_by_supersession": 0,
                    "opening_replayed": False,
                    "historical_debit_replayed": False,
                    "shipment_acceptance_changed": False,
                    "wb_writes": 0,
                    "checkpoint_artifacts_preserved": True,
                },
            }
        fingerprint = _fingerprint(proof)
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "deployed_sha": self.deployed_sha,
            "mode": "dry_run_exact_supersession",
            "status": "ready",
            "apply_allowed": True,
            "would_change": True,
            "operation_id": target_id,
            "superseding_operation_id": str(replacement["operation_id"]),
            "fingerprint": fingerprint,
            "proof": proof,
            "blockers": [],
            "effect": _effect(),
        }

    def apply(
        self,
        reviewed_plan: Mapping[str, Any],
        *,
        fingerprint: str,
        actor: str,
        approval_reference: str,
    ) -> dict[str, Any]:
        """Append the exact relation and terminal lifecycle transition."""

        plan = dict(reviewed_plan)
        expected = _sha256(fingerprint)
        operator = str(actor or "").strip()
        approval = str(approval_reference or "").strip()
        if (
            str(plan.get("contract_name") or "") != CONTRACT_NAME
            or int(plan.get("contract_version") or 0) != CONTRACT_VERSION
            or str(plan.get("mode") or "") != "dry_run_exact_supersession"
            or str(plan.get("status") or "") != "ready"
            or plan.get("apply_allowed") is not True
            or plan.get("would_change") is not True
            or str(plan.get("fingerprint") or "") != expected
            or _fingerprint(dict(plan.get("proof") or {})) != expected
        ):
            raise FfPoolCutoverRecoverySupersessionError(
                "reviewed_plan_invalid",
                "Reviewed supersession plan does not match the exact dry-run",
            )
        if not operator or not approval:
            raise FfPoolCutoverRecoverySupersessionError(
                "owner_gate_required",
                "actor and exact owner approval reference are required",
            )
        target_id = _operation_id(str(plan.get("operation_id") or ""))
        replacement_id = _operation_id(
            str(plan.get("superseding_operation_id") or "")
        )
        registry = WarehouseRecoveryRegistry(
            runtime_dir=self.runtime.runtime_dir,
            db_path=self.runtime.db_path,
        )
        existing = registry.get_operation(target_id)
        if (
            existing is not None
            and str(existing.get("lifecycle") or "")
            == RecoveryState.SUPERSEDED.value
        ):
            relation = dict(existing.get("supersession") or {})
            if (
                str(relation.get("proof_fingerprint") or "") != expected
                or str(relation.get("superseding_operation_id") or "")
                != replacement_id
            ):
                raise FfPoolCutoverRecoverySupersessionError(
                    "existing_supersession_mismatch",
                    "The target recovery already owns another immutable relation",
                )
            return {
                "contract_name": CONTRACT_NAME,
                "contract_version": CONTRACT_VERSION,
                "deployed_sha": self.deployed_sha,
                "status": "already_superseded",
                "idempotent": True,
                "fingerprint": expected,
                "approval_reference": approval,
                "readback": self.readback(target_id),
                "effect": _effect(),
            }

        with warehouse_functional_write_lock(self.runtime.runtime_dir):
            fresh = self.build_plan(target_id)
            if (
                fresh.get("apply_allowed") is not True
                or str(fresh.get("fingerprint") or "") != expected
                or str(fresh.get("superseding_operation_id") or "")
                != replacement_id
            ):
                raise FfPoolCutoverRecoverySupersessionError(
                    "supersession_proof_drift",
                    "Current canonical proof differs from the reviewed dry-run",
                    details=fresh.get("blockers"),
                )
            operation = registry.supersede_failed_operation(
                target_id,
                superseding_operation_id=replacement_id,
                proof_contract=CONTRACT_NAME,
                proof_fingerprint=expected,
                proof=dict(plan["proof"]),
                actor=operator,
                authorization_reference=approval,
            )
            readback = self.readback(target_id)
            if str(readback.get("status") or "") != "superseded_verified":
                raise FfPoolCutoverRecoverySupersessionError(
                    "supersession_readback_failed",
                    "Supersession append committed but exact readback failed",
                    details=readback,
                )
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "deployed_sha": self.deployed_sha,
            "status": "applied_superseded",
            "idempotent": False,
            "fingerprint": expected,
            "approval_reference": approval,
            "applied_at": str(operation.get("updated_at") or self.timestamp_factory()),
            "readback": readback,
            "effect": _effect(),
        }

    def readback(self, operation_id: str) -> dict[str, Any]:
        target_id = _operation_id(operation_id)
        registry = WarehouseRecoveryRegistry(
            runtime_dir=self.runtime.runtime_dir,
            db_path=self.runtime.db_path,
        )
        operation = registry.get_operation(target_id)
        if operation is None:
            return {
                "contract_name": CONTRACT_NAME,
                "contract_version": CONTRACT_VERSION,
                "deployed_sha": self.deployed_sha,
                "status": "missing",
                "operation_id": target_id,
            }
        relation = dict(operation.get("supersession") or {})
        artifacts = list(operation.get("artifacts") or [])
        artifacts_preserved = (
            len(artifacts) >= 2
            and all(str(item.get("state") or "") == "verified" for item in artifacts)
            and {str(item.get("artifact_kind") or "") for item in artifacts}
            >= {"domain_checkpoint", "manifest"}
            and all(_public_artifact_matches(item) for item in artifacts)
        )
        blocking = [
            item
            for item in registry.list_operations(limit=1000)
            if str(item.get("tier") or "") == "T2"
            and str(item.get("lifecycle") or "")
            in {
                RecoveryState.FAILED_RECOVERABLE.value,
                RecoveryState.QUARANTINED.value,
            }
        ]
        raw_relation: dict[str, Any] | None = None
        latest_transition: dict[str, Any] | None = None
        with closing(_open_query_only(self.runtime.db_path)) as conn:
            if _table_exists(conn, RECOVERY_SUPERSESSIONS_TABLE):
                row = conn.execute(
                    f"SELECT * FROM {RECOVERY_SUPERSESSIONS_TABLE} "
                    "WHERE target_operation_id=?",
                    (target_id,),
                ).fetchone()
                raw_relation = dict(row) if row is not None else None
            if _table_exists(conn, RECOVERY_TRANSITIONS_TABLE):
                row = conn.execute(
                    f"SELECT from_state,to_state,state_version,detail_json "
                    f"FROM {RECOVERY_TRANSITIONS_TABLE} WHERE operation_id=? "
                    "ORDER BY transition_id DESC LIMIT 1",
                    (target_id,),
                ).fetchone()
                latest_transition = dict(row) if row is not None else None
        proof_payload = _loads(
            (raw_relation or {}).get("proof_json"), {}
        )
        proof_verified = bool(raw_relation) and (
            str(raw_relation.get("proof_contract") or "") == CONTRACT_NAME
            and str(raw_relation.get("proof_fingerprint") or "")
            == _fingerprint(proof_payload)
            and str(proof_payload.get("target_operation_id") or "") == target_id
            and str(proof_payload.get("superseding_operation_id") or "")
            == str(raw_relation.get("superseding_operation_id") or "")
            and bool(str(raw_relation.get("authorization_reference") or ""))
        )
        transition_verified = bool(latest_transition) and (
            str(latest_transition.get("from_state") or "")
            == RecoveryState.FAILED_RECOVERABLE.value
            and str(latest_transition.get("to_state") or "")
            == RecoveryState.SUPERSEDED.value
            and int(latest_transition.get("state_version") or 0)
            == int(operation.get("state_version") or 0)
        )
        verified = (
            str(operation.get("lifecycle") or "")
            == RecoveryState.SUPERSEDED.value
            and str(relation.get("target_operation_id") or "") == target_id
            and bool(relation.get("proof_fingerprint"))
            and artifacts_preserved
            and proof_verified
            and transition_verified
        )
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "deployed_sha": self.deployed_sha,
            "status": "superseded_verified" if verified else "not_superseded",
            "operation_id": target_id,
            "lifecycle": str(operation.get("lifecycle") or ""),
            "state_version": int(operation.get("state_version") or 0),
            "supersession": relation or None,
            "artifacts_preserved": artifacts_preserved,
            "proof_verified": proof_verified,
            "transition_verified": transition_verified,
            "artifact_count": len(artifacts),
            "blocking_t2_operation_ids": [
                str(item.get("operation_id") or "") for item in blocking
            ],
            "target_blocks_future_publication": any(
                str(item.get("operation_id") or "") == target_id
                for item in blocking
            ),
            "business_rows_changed_by_supersession": 0,
            "wb_writes": 0,
        }


def _validate_target(
    target: Mapping[str, Any], *, blockers: list[dict[str, Any]]
) -> None:
    required = {
        "operation_kind": "warehouse_opening_publication",
        "closure_kind": "warehouse_domain",
        "tier": "T2",
        "lifecycle_state": RecoveryState.FAILED_RECOVERABLE.value,
        "next_action": REQUIRED_TARGET_NEXT_ACTION,
    }
    for field, expected in required.items():
        if str(target.get(field) or "") != expected:
            blockers.append(
                {
                    "code": "target_recovery_identity_mismatch",
                    "field": field,
                    "expected": expected,
                    "actual": str(target.get(field) or ""),
                }
            )
    scope = _loads(target.get("target_scope_json"), {})
    if (
        str(scope.get("owner_gate_fingerprint") or "")
        != str(target.get("plan_fingerprint") or "")
        or not re.fullmatch(r"[0-9a-f]{40}", str(scope.get("deployed_sha") or ""))
        or str(scope.get("cutover_id") or "")
        != "ffcut_"
        + str(target.get("plan_fingerprint") or "").removeprefix("sha256:")[:28]
    ):
        blockers.append({"code": "target_recovery_scope_mismatch"})


def _validate_replacement(
    target: Mapping[str, Any],
    replacement: Mapping[str, Any],
    *,
    replacement_scope: Mapping[str, Any],
    manifest: Mapping[str, Any],
    blockers: list[dict[str, Any]],
) -> None:
    if (
        str(replacement.get("operation_kind") or "")
        != "warehouse_opening_publication"
        or str(replacement.get("closure_kind") or "") != "warehouse_domain"
        or str(replacement.get("tier") or "") != "T2"
        or str(replacement.get("lifecycle_state") or "")
        not in {RecoveryState.RETAINED.value, RecoveryState.RELEASED.value}
    ):
        blockers.append({"code": "superseding_recovery_not_successful_t2"})
    if (
        str(replacement_scope.get("owner_gate_fingerprint") or "")
        != str(replacement.get("plan_fingerprint") or "")
        or str(replacement_scope.get("cutover_id") or "")
        != str(manifest.get("cutover_id") or "")
        or str(replacement_scope.get("deployed_sha") or "")
        != str(manifest.get("deployed_sha") or "")
        or str(replacement.get("after_digest") or "")
        != str(manifest.get("manifest_digest") or "")
        or str(replacement.get("non_target_digest") or "")
        != str(target.get("non_target_digest") or "")
        or str(manifest.get("non_target_digest") or "")
        != str(target.get("non_target_digest") or "")
    ):
        blockers.append({"code": "superseding_recovery_scope_or_digest_mismatch"})


def _validate_current_cutover_status(
    status: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    blockers: list[dict[str, Any]],
) -> None:
    barrier = dict(status.get("barrier") or {})
    readback = dict(status.get("readback") or {})
    current_manifest = dict(status.get("manifest") or {})
    if str(status.get("status") or "") != "applied":
        blockers.append({"code": "canonical_cutover_not_applied"})
    if (
        barrier.get("active") is not False
        or str(barrier.get("phase") or "") != "released"
        or str(barrier.get("deployed_sha") or "")
        != str(manifest.get("deployed_sha") or "")
    ):
        blockers.append({"code": "canonical_cutover_barrier_not_released"})
    if (
        str(readback.get("status") or "") != "pass"
        or list(readback.get("mismatches") or [])
        or readback.get("aggregate_unchanged") is not True
        or readback.get("reader_enabled") is not True
    ):
        blockers.append({"code": "canonical_cutover_readback_not_exact"})
    if (
        str(current_manifest.get("cutover_id") or "")
        != str(manifest.get("cutover_id") or "")
        or str(current_manifest.get("manifest_digest") or "")
        != str(manifest.get("manifest_digest") or "")
        or str(current_manifest.get("deployed_sha") or "")
        != str(manifest.get("deployed_sha") or "")
    ):
        blockers.append({"code": "canonical_cutover_manifest_identity_mismatch"})


def _validate_aborted_target_epochs(
    epochs: Sequence[Mapping[str, Any]], *, blockers: list[dict[str, Any]]
) -> None:
    grouped = _group_epochs(epochs)
    if not grouped:
        blockers.append({"code": "target_aborted_epoch_missing"})
        return
    for epoch_id, rows in grouped.items():
        phases = [str(item["phase"]) for item in rows]
        if phases != ["held", "aborted"]:
            blockers.append(
                {
                    "code": "target_epoch_not_proven_precommit_aborted",
                    "epoch_id": epoch_id,
                    "phases": phases,
                }
            )


def _validate_released_replacement_epochs(
    epochs: Sequence[Mapping[str, Any]],
    *,
    expected_epoch_id: str,
    blockers: list[dict[str, Any]],
) -> None:
    grouped = _group_epochs(epochs)
    released = 0
    for epoch_id, rows in grouped.items():
        phases = [str(item["phase"]) for item in rows]
        if phases == ["held", "aborted"]:
            continue
        if (
            epoch_id == expected_epoch_id
            and phases
            == ["held", "applying", "readback_required", "reconciled", "released"]
        ):
            released += 1
            continue
        blockers.append(
            {
                "code": "superseding_epoch_chain_ambiguous",
                "epoch_id": epoch_id,
                "phases": phases,
            }
        )
    if released != 1:
        blockers.append(
            {"code": "superseding_released_epoch_missing", "count": released}
        )


def _validate_target_artifacts(
    artifacts: Sequence[Mapping[str, Any]], *, blockers: list[dict[str, Any]]
) -> None:
    by_kind = {str(item["artifact_kind"]): item for item in artifacts}
    if set(by_kind) != {"domain_checkpoint", "manifest"}:
        blockers.append(
            {
                "code": "target_recovery_artifact_family_mismatch",
                "kinds": sorted(by_kind),
            }
        )
        return
    for kind, item in by_kind.items():
        if (
            str(item.get("state") or "") != "verified"
            or int(item.get("size_bytes") or 0) <= 0
            or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", str(item.get("digest") or "")
            )
            or item.get("bytes_present") is not True
            or item.get("size_matches") is not True
            or item.get("digest_matches") is not True
        ):
            blockers.append(
                {"code": "target_recovery_artifact_not_preserved", "kind": kind}
            )


def _readback_operation_id(
    events: Sequence[Mapping[str, Any]], *, blockers: list[dict[str, Any]]
) -> str:
    types = [str(item["event_type"]) for item in events]
    if types != ["applied", "readback_passed"]:
        blockers.append(
            {"code": "canonical_cutover_recovery_events_ambiguous", "types": types}
        )
        return ""
    details = _loads(events[-1].get("details_json"), {})
    operation_id = str(details.get("warehouse_recovery_operation_id") or "")
    if not re.fullmatch(r"recovery_[0-9a-f]{32}(?:_g[0-9]+)?", operation_id):
        blockers.append({"code": "canonical_readback_recovery_identity_missing"})
        return ""
    if str(events[0]["evidence_digest"]) != str(events[1]["evidence_digest"]):
        blockers.append({"code": "canonical_recovery_event_digest_mismatch"})
    return operation_id


def _target_artifacts(
    conn: sqlite3.Connection, operation_id: str
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in conn.execute(
        f"""SELECT artifact_kind,path,size_bytes,digest,state,created_at,expires_at
              FROM {RECOVERY_ARTIFACTS_TABLE}
              WHERE operation_id=? ORDER BY artifact_kind,artifact_id""",
        (operation_id,),
    ):
        path = Path(str(row["path"] or ""))
        try:
            bytes_present = path.is_file()
            stat_size = path.stat().st_size if bytes_present else -1
            actual_digest = _sha256_file(path) if bytes_present else ""
        except OSError:
            bytes_present = False
            stat_size = -1
            actual_digest = ""
        result.append(
            {
                "artifact_kind": str(row["artifact_kind"]),
                "size_bytes": int(row["size_bytes"]),
                "digest": str(row["digest"]),
                "state": str(row["state"]),
                "created_at": str(row["created_at"]),
                "expires_at": str(row["expires_at"] or ""),
                "bytes_present": bytes_present,
                "size_matches": stat_size == int(row["size_bytes"]),
                "digest_matches": actual_digest == str(row["digest"]),
            }
        )
    return result


def _operation(conn: sqlite3.Connection, operation_id: str) -> dict[str, Any] | None:
    if not _table_exists(conn, RECOVERY_OPERATIONS_TABLE):
        return None
    row = conn.execute(
        f"SELECT * FROM {RECOVERY_OPERATIONS_TABLE} WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _supersession(
    conn: sqlite3.Connection, operation_id: str
) -> dict[str, Any] | None:
    if not _table_exists(conn, RECOVERY_SUPERSESSIONS_TABLE):
        return None
    row = conn.execute(
        f"SELECT * FROM {RECOVERY_SUPERSESSIONS_TABLE} WHERE target_operation_id=?",
        (operation_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _transition_rows(
    conn: sqlite3.Connection, operation_id: str
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            f"""SELECT from_state,to_state,state_version,transitioned_at,detail_json
                  FROM {RECOVERY_TRANSITIONS_TABLE}
                  WHERE operation_id=? ORDER BY transition_id""",
            (operation_id,),
        )
    ]


def _epoch_rows(
    conn: sqlite3.Connection,
    *,
    gate_fingerprint: str,
    deployed_sha: str,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            f"""SELECT epoch_id,phase,manifest_digest,deployed_sha,event_at,event_sequence
                  FROM {WRITE_EPOCH_EVENTS_TABLE}
                  WHERE manifest_digest=? AND deployed_sha=?
                  ORDER BY event_sequence""",
            (gate_fingerprint, deployed_sha),
        )
    ]


def _manifest_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _table_exists(conn, MANIFESTS_TABLE):
        return []
    return [
        dict(row)
        for row in conn.execute(
            f"""SELECT cutover_id,manifest_digest,deployed_sha,cutover_at,
                       business_date,feature_epoch,aggregate_revision,
                       aggregate_digest,detail_digest,non_target_digest,created_at
                  FROM {MANIFESTS_TABLE} ORDER BY created_at,cutover_id"""
        )
    ]


def _cutover_recovery_rows(
    conn: sqlite3.Connection, cutover_id: str
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            f"""SELECT event_type,event_at,evidence_digest,details_json,recovery_sequence
                  FROM {RECOVERY_EVENTS_TABLE}
                  WHERE cutover_id=? ORDER BY recovery_sequence""",
            (cutover_id,),
        )
    ]


def _group_epochs(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, list[Mapping[str, Any]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("epoch_id") or ""), []).append(row)
    return grouped


def _blocked_plan(
    operation_id: str,
    blockers: Sequence[Mapping[str, Any]],
    deployed_sha: str,
) -> dict[str, Any]:
    return {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "deployed_sha": deployed_sha,
        "mode": "dry_run_exact_supersession",
        "status": "blocked",
        "apply_allowed": False,
        "would_change": False,
        "operation_id": operation_id,
        "superseding_operation_id": "",
        "fingerprint": "",
        "proof": None,
        "blockers": [dict(item) for item in blockers],
        "effect": _effect(),
    }


def _effect() -> dict[str, Any]:
    return {
        "target_lifecycle": "failed_recoverable -> superseded",
        "append_only_relation": True,
        "expected_affected_records": {
            "recovery_operation_updates": 1,
            "recovery_transition_inserts": 1,
            "recovery_supersession_inserts": 1,
            "warehouse_domain_rows": 0,
        },
        "checkpoint_artifacts_preserved": True,
        "backup_evidence": (
            "The verified T2 checkpoint and manifest remain byte-present; the "
            "immutable proof captures the exact pre-change recovery row and "
            "transition/artifact digests. The SQLite metadata change is atomic."
        ),
        "business_rows_changed": 0,
        "opening_replayed": False,
        "historical_debit_replayed": False,
        "shipment_acceptance_changed": False,
        "wb_writes": 0,
        "recovery": (
            "No automatic reversal: any later challenge requires a new reviewed "
            "append-only fail-closed relation; old proof and artifacts remain intact."
        ),
    }


def _count(
    conn: sqlite3.Connection,
    table: str,
    where: str,
    parameters: Sequence[Any],
) -> int:
    if not _table_exists(conn, table):
        return 0
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {where}", tuple(parameters)
        ).fetchone()[0]
    )


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _open_query_only(path: Path) -> sqlite3.Connection:
    resolved = Path(path).resolve()
    conn = sqlite3.connect(
        f"file:{resolved.as_posix()}?mode=ro", uri=True, timeout=30.0
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        conn.close()
        raise FfPoolCutoverRecoverySupersessionError(
            "query_only_preflight_failed",
            "SQLite query-only preflight failed",
        )
    return conn


def _loads(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(str(value or ""))
    except json.JSONDecodeError:
        return default
    return parsed


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical_json(value).encode("utf-8")
    ).hexdigest()


def _operation_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not re.fullmatch(r"recovery_[0-9a-f]{32}(?:_g[0-9]+)?", normalized):
        raise FfPoolCutoverRecoverySupersessionError(
            "operation_id_invalid", "Exact recovery operation id is required"
        )
    return normalized


def _sha256(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", normalized):
        raise FfPoolCutoverRecoverySupersessionError(
            "fingerprint_invalid", "Exact sha256 fingerprint is required"
        )
    return normalized


def _commit_sha(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", normalized):
        raise FfPoolCutoverRecoverySupersessionError(
            "deployed_sha_invalid", "Exact deployed SHA is required"
        )
    return normalized


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _public_artifact_matches(artifact: Mapping[str, Any]) -> bool:
    path = Path(str(artifact.get("path") or ""))
    if not path.is_file():
        return False
    try:
        if path.stat().st_size != int(artifact.get("size_bytes") or 0):
            return False
        return _sha256_file(path) == str(artifact.get("digest") or "")
    except OSError:
        return False
