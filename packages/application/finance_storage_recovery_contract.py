"""Fail-closed recovery contract for every Finance storage mutation boundary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from packages.application.business_data_write_barrier import (
    SCHEMA_VERSION as WRITE_BARRIER_SCHEMA_VERSION,
    barrier_status,
)
from packages.application.storage_registry import StoreRegistry, parse_manifest


RECOVERY_CONTRACT_VERSION = "wb_core_finance_storage_recovery_contract_v1"
RECOVERY_VALIDATION_VERSION = (
    "wb_core_finance_storage_recovery_preflight_v1"
)
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}")

EXPECTED_RUNNER_CONTRACTS = {
    "snapshot_plan": "wb_core_finance_storage_snapshot_plan_v1",
    "snapshot": "wb_core_finance_storage_coherent_snapshot_v1",
    "candidate_plan": "wb_core_finance_storage_split_plan_v1",
    "candidate": "wb_core_finance_storage_split_candidate_v1",
    "candidate_abort_plan": (
        "wb_core_finance_storage_candidate_abort_plan_v1"
    ),
    "candidate_abort_result": (
        "wb_core_finance_storage_candidate_abort_result_v1"
    ),
    "shadow": "wb_core_finance_shadow_ingest_state_v1",
    "shadow_verification": "wb_core_finance_shadow_verification_v1",
    "cutover_plan": "wb_core_finance_storage_cutover_plan_v1",
    "cutover_result": "wb_core_finance_storage_cutover_result_v1",
    "rollback_plan": "wb_core_finance_storage_rollback_plan_v1",
    "rollback_candidate": "wb_core_finance_storage_rollback_candidate_v1",
    "rollback_result": "wb_core_finance_storage_rollback_result_v1",
    "stale_writer_plan": (
        "wb_core_finance_storage_stale_writer_recovery_plan_v1"
    ),
    "stale_writer_result": (
        "wb_core_finance_storage_stale_writer_recovery_result_v1"
    ),
    "snapshot_retention_plan": (
        "wb_core_finance_storage_snapshot_retention_plan_v1"
    ),
    "snapshot_retention_result": (
        "wb_core_finance_storage_snapshot_retention_result_v1"
    ),
}

MUTATION_ACTIONS = frozenset(
    {
        "apply",
        "candidate-abort-apply",
        "snapshot-create",
        "snapshot-integrity",
        "snapshot-retention-apply",
        "stale-writer-stop",
        "shadow-activate",
        "shadow-reconcile",
        "shadow-verify",
        "live-tail-apply",
        "shadow-deactivate",
        "cutover-apply",
        "rollback-prepare",
        "rollback-apply",
    }
)
EXPLICIT_APPROVAL_ACTIONS = frozenset(
    {
        "apply",
        "candidate-abort-apply",
        "snapshot-create",
        "snapshot-retention-apply",
        "stale-writer-stop",
        "shadow-activate",
        "shadow-reconcile",
        "live-tail-apply",
        "shadow-deactivate",
        "cutover-apply",
        "rollback-prepare",
        "rollback-apply",
    }
)
BARRIER_OWNING_ACTIONS = {
    "snapshot-create": "snapshot",
    "cutover-apply": "final_cutover",
    "rollback-apply": "rollback_drill",
}

_TRANSITIONS = (
    {
        "transition": "transport.hold_mutation",
        "persisted_store": (
            "finance-storage-transport-jobs/<request-digest>/"
            "request.json + status.json + result.json"
        ),
        "from": ("absent", "queued", "running"),
        "to": ("succeeded",),
        "recovery": (
            "same_request_digest_observe_only_no_duplicate_worker"
        ),
        "command": "finance-storage-transport-job submit/status",
    },
    {
        "transition": "snapshot.acquire",
        "persisted_store": ".business-data-write-barrier.json",
        "from": ("absent", "released"),
        "to": ("acquiring",),
        "recovery": "exact_identity_idempotent_resume",
        "command": "finance-storage-snapshot-apply",
    },
    {
        "transition": "snapshot.hold",
        "persisted_store": ".business-data-maintenance.json",
        "from": ("preparing", "holding"),
        "to": ("held",),
        "recovery": "exact_control_signature_idempotent_resume",
        "command": "finance-storage-snapshot-apply",
    },
    {
        "transition": "snapshot.copy",
        "persisted_store": (
            "finance-storage-split-snapshots/<id>/"
            "snapshot_manifest.json"
        ),
        "from": ("partial", "database_without_manifest"),
        "to": ("captured_unverified",),
        "recovery": "plan_bound_copy_resume_or_fail_closed",
        "command": "finance-storage-snapshot-apply",
    },
    {
        "transition": "snapshot.restore",
        "persisted_store": (
            ".business-data-maintenance.json + "
            "business-data-maintenance-restore-jobs/<job>"
        ),
        "from": ("held", "restoring"),
        "to": ("restored",),
        "recovery": "same_job_digest_bound_resume",
        "command": "business-data-maintenance-restore-resume",
    },
    {
        "transition": "snapshot.release",
        "persisted_store": ".business-data-write-barrier.json",
        "from": ("held", "restoring"),
        "to": ("released",),
        "recovery": "exact_restore_readback_idempotent_release",
        "command": "business-data-maintenance barrier-release",
    },
    {
        "transition": "snapshot_retention.archive",
        "persisted_store": (
            "backups/finance-storage-split-snapshots/<id>/"
            "retention_transaction.json"
        ),
        "from": ("absent", "copying"),
        "to": ("archive_verified",),
        "recovery": "exact_plan_hash_bound_copy_resume",
        "command": "finance-storage-snapshot-retention-apply",
    },
    {
        "transition": "snapshot_retention.release",
        "persisted_store": (
            "backups/finance-storage-split-snapshots/<id>/"
            "archive_manifest.json"
        ),
        "from": ("archive_verified", "partial_source_release"),
        "to": ("source_released",),
        "recovery": "verified_archive_idempotent_source_release",
        "command": "finance-storage-snapshot-retention-apply",
    },
    {
        "transition": "candidate.backfill",
        "persisted_store": (
            "generations/<id>/operational.sqlite3:"
            "finance_storage_migration_chunks"
        ),
        "from": ("loading", "verified_chunks"),
        "to": ("candidate_ready",),
        "recovery": (
            "plan_bound_foreign_key_ordered_verified_chunk_"
            "idempotent_resume"
        ),
        "command": "finance-storage-split-apply",
    },
    {
        "transition": "candidate.manifest",
        "persisted_store": "generations/<id>/candidate_manifest.json",
        "from": ("candidate_bytes_ready",),
        "to": ("shadow",),
        "recovery": "exact_manifest_idempotent_readback",
        "command": "finance-storage-split-apply",
    },
    {
        "transition": "candidate.abort",
        "persisted_store": (
            ".finance-storage-candidate-aborts/<generation>.json + "
            "optional inactive candidate/shadow manifests"
        ),
        "from": (
            "loading",
            "verified_chunks",
            "candidate_bytes_without_manifest",
            "completed_unselected_shadow_inactive",
        ),
        "to": ("absent",),
        "recovery": (
            "exact_allowlist_durable_idempotent_release_or_fail_closed"
        ),
        "command": "finance-storage-candidate-abort-apply",
    },
    {
        "transition": "shadow.activate",
        "persisted_store": ".finance-storage-shadow-ingest.json",
        "from": ("absent", "inactive"),
        "to": ("active",),
        "recovery": "exact_candidate_idempotent_resume",
        "command": "finance-storage-shadow-activate",
    },
    {
        "transition": "shadow.reconcile",
        "persisted_store": (
            "finance_raw.sqlite3:finance_raw_ingest_batches/"
            "finance_raw_batch_rows"
        ),
        "from": ("loading", "committed_chunks"),
        "to": ("committed",),
        "recovery": "immutable_identity_idempotent_resume",
        "command": "finance-storage-shadow-reconcile",
    },
    {
        "transition": "shadow.live_tail",
        "persisted_store": (
            "finance_raw.sqlite3:finance_raw_bridge_cursors/"
            "finance_raw_outbox"
        ),
        "from": ("source_committed", "destination_committed"),
        "to": ("acknowledged",),
        "recovery": "event_and_sequence_idempotent_resume",
        "command": "finance-storage-live-tail-apply",
    },
    {
        "transition": "shadow.soak",
        "persisted_store": "generations/<id>/shadow_verification.json",
        "from": ("soaking",),
        "to": ("ready",),
        "recovery": "append_observation_idempotent_reverify",
        "command": "finance-storage-shadow-verify",
    },
    {
        "transition": "cutover.pre_manifest",
        "persisted_store": (
            ".finance-storage-shadow-ingest.json + candidate stores"
        ),
        "from": ("held", "shadow_deactivated"),
        "to": ("manifest_pending",),
        "recovery": "monolith_canonical_fail_closed_or_resume",
        "command": "finance-storage-cutover-apply",
    },
    {
        "transition": "cutover.post_manifest",
        "persisted_store": "storage_generation_manifest.json",
        "from": ("manifest_pending",),
        "to": ("cutover",),
        "recovery": "exact_split_manifest_idempotent_readback",
        "command": "finance-storage-cutover-apply",
    },
    {
        "transition": "cutover.release",
        "persisted_store": ".business-data-write-barrier.json",
        "from": ("held", "restoring"),
        "to": ("released",),
        "recovery": "post_manifest_restore_then_idempotent_release",
        "command": "business-data-maintenance barrier-release",
    },
    {
        "transition": "rollback.prepare",
        "persisted_store": (
            "generations/rollback-<id>/rollback_candidate.json"
        ),
        "from": ("partial", "candidate_without_evidence"),
        "to": ("candidate_ready",),
        "recovery": "plan_bound_rebuild_or_idempotent_readback",
        "command": "finance-storage-rollback-prepare",
    },
    {
        "transition": "rollback.pre_manifest",
        "persisted_store": "rollback candidate + split stores",
        "from": ("held", "candidate_reconciled"),
        "to": ("manifest_pending",),
        "recovery": "split_canonical_fail_closed_or_resume",
        "command": "finance-storage-rollback-apply",
    },
    {
        "transition": "rollback.post_manifest",
        "persisted_store": "storage_generation_manifest.json",
        "from": ("manifest_pending",),
        "to": ("monolith",),
        "recovery": "exact_rollback_manifest_idempotent_readback",
        "command": "finance-storage-rollback-apply",
    },
    {
        "transition": "rollback.release",
        "persisted_store": ".business-data-write-barrier.json",
        "from": ("held", "restoring"),
        "to": ("released",),
        "recovery": "post_manifest_restore_then_idempotent_release",
        "command": "business-data-maintenance barrier-release",
    },
)


class FinanceStorageRecoveryContractError(ValueError):
    """The Finance recovery contract is missing, stale or ambiguous."""


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


def _reviewed_plan_fingerprint(
    action: str,
    plan: Mapping[str, Any],
) -> str:
    """Recompute the runner-owned deterministic hash before any mutation."""

    if action == "apply":
        from packages.application.finance_storage_migration import (
            _plan_fingerprint,
        )

        return _plan_fingerprint(plan)
    if action == "snapshot-create":
        from packages.application.finance_storage_migration import (
            _snapshot_plan_fingerprint,
        )

        return _snapshot_plan_fingerprint(plan)
    if action == "snapshot-retention-apply":
        from packages.application.finance_storage_snapshot_retention import (
            _fingerprint as retention_fingerprint,
        )

        stable = {
            key: value
            for key, value in plan.items()
            if key not in {"fingerprint", "deploy_lease"}
        }
        return retention_fingerprint(stable)
    if action == "candidate-abort-apply":
        from packages.application.finance_storage_candidate_abort import (
            _plan_fingerprint as candidate_abort_plan_fingerprint,
        )

        return candidate_abort_plan_fingerprint(plan)
    if action == "stale-writer-stop":
        from packages.application.finance_storage_stale_writer_recovery import (
            _plan_fingerprint as stale_writer_plan_fingerprint,
        )

        return stale_writer_plan_fingerprint(plan)
    if action == "cutover-apply":
        from packages.application.finance_storage_migration import (
            FinanceStorageCutover,
        )

        return FinanceStorageCutover._fingerprint(plan)
    if action in {"rollback-prepare", "rollback-apply"}:
        from packages.application.finance_storage_migration import (
            FinanceStorageRollback,
        )

        return FinanceStorageRollback._fingerprint(plan)
    raise FinanceStorageRecoveryContractError(
        f"unsupported reviewed-plan fingerprint action: {action!r}"
    )


def recovery_contract(
    *,
    runner_contracts: Mapping[str, str],
    restore_job_contract: str,
    restore_max_resume_sequence: int,
    downstream_capabilities: Mapping[str, bool],
) -> dict[str, Any]:
    actual = {str(key): str(value) for key, value in runner_contracts.items()}
    if actual != EXPECTED_RUNNER_CONTRACTS:
        raise FinanceStorageRecoveryContractError(
            "Finance recovery runner-version support is incomplete or stale"
        )
    if (
        str(restore_job_contract)
        != "business_data_maintenance_restore_job_v1"
        or int(restore_max_resume_sequence) < 3
    ):
        raise FinanceStorageRecoveryContractError(
            "downstream durable restore runner capability is unsupported"
        )
    required_capabilities = (
        "maintenance_restore",
        "barrier_release",
        "durable_restore_submit_status",
        "durable_restore_inventory",
        "durable_restore_resume",
        "restore_systemd_template",
        "durable_storage_transport",
    )
    missing_capabilities = sorted(
        key
        for key in required_capabilities
        if downstream_capabilities.get(key) is not True
    )
    if missing_capabilities:
        raise FinanceStorageRecoveryContractError(
            "downstream restore/release capabilities are missing: "
            + ", ".join(missing_capabilities)
        )
    payload: dict[str, Any] = {
        "contract_version": RECOVERY_CONTRACT_VERSION,
        "write_barrier_schema_version": WRITE_BARRIER_SCHEMA_VERSION,
        "restore_job_contract": str(restore_job_contract),
        "restore_max_resume_sequence": int(restore_max_resume_sequence),
        "runner_contracts": actual,
        "downstream_capabilities": {
            key: True for key in required_capabilities
        },
        "transitions": [dict(item) for item in _TRANSITIONS],
        "fail_closed_default": True,
        "second_restore_job_allowed": False,
        "automatic_manifest_guessing_allowed": False,
    }
    payload["fingerprint"] = _fingerprint(payload)
    return payload


def _load_object(path: Path, *, label: str) -> dict[str, Any]:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise FinanceStorageRecoveryContractError(
            f"{label} is missing or unsafe"
        )
    resolved = expanded.resolve()
    if (
        not resolved.is_file()
        or resolved.stat().st_mode & 0o077
    ):
        raise FinanceStorageRecoveryContractError(
            f"{label} is missing or unsafe"
        )
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FinanceStorageRecoveryContractError(
            f"{label} is unreadable"
        ) from exc
    if not isinstance(payload, dict):
        raise FinanceStorageRecoveryContractError(
            f"{label} must contain a JSON object"
        )
    return payload


def _within_runtime(runtime_dir: Path, value: Path, *, label: str) -> Path:
    expanded = value.expanduser()
    if expanded.is_symlink():
        raise FinanceStorageRecoveryContractError(
            f"{label} is a symlink and is unsafe"
        )
    resolved = expanded.resolve()
    try:
        resolved.relative_to(runtime_dir)
    except ValueError as exc:
        raise FinanceStorageRecoveryContractError(
            f"{label} escapes the canonical runtime directory"
        ) from exc
    return resolved


def _path_identity(path: Path) -> dict[str, Any]:
    target = Path(path)
    payload: dict[str, Any] = {
        "path": str(target),
        "exists": target.exists(),
        "is_file": target.is_file(),
        "sidecars": [],
    }
    if target.exists():
        stat = target.stat()
        payload.update(
            {
                "device": int(stat.st_dev),
                "inode": int(stat.st_ino),
                "size_bytes": int(stat.st_size),
                "mtime_ns": int(stat.st_mtime_ns),
            }
        )
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(target) + suffix)
        if sidecar.exists():
            stat = sidecar.stat()
            payload["sidecars"].append(
                {
                    "path": str(sidecar),
                    "size_bytes": int(stat.st_size),
                    "inode": int(stat.st_ino),
                }
            )
    return payload


def validate_recovery_preflight(
    runtime_dir: Path,
    *,
    action: str,
    phase: str,
    deployed_sha: str,
    approval_reference: str,
    expected_fingerprint: str,
    deploy_lease: Mapping[str, Any] | None,
    runner_contracts: Mapping[str, str],
    restore_job_contract: str,
    restore_max_resume_sequence: int,
    downstream_capabilities: Mapping[str, bool],
    reviewed_plan: Mapping[str, Any] | None = None,
    source_snapshot_manifest: Path | None = None,
    candidate_manifest_path: Path | None = None,
    rollback_candidate_evidence_path: Path | None = None,
) -> dict[str, Any]:
    """Validate all recovery paths before a Finance storage mutation."""

    exact_action = str(action or "").strip()
    exact_phase = str(phase or "").strip()
    root = Path(runtime_dir).expanduser().resolve()
    if exact_action not in MUTATION_ACTIONS:
        raise FinanceStorageRecoveryContractError(
            f"unsupported Finance recovery action: {exact_action!r}"
        )
    if exact_phase not in {"pre_barrier", "mutation"}:
        raise FinanceStorageRecoveryContractError(
            "Finance recovery preflight phase is invalid"
        )
    if _SHA_RE.fullmatch(str(deployed_sha or "")) is None:
        raise FinanceStorageRecoveryContractError(
            "exact deployed SHA is required by recovery preflight"
        )
    if deploy_lease is None:
        raise FinanceStorageRecoveryContractError(
            "active Finance deploy lease is required by recovery preflight"
        )
    lease = dict(deploy_lease.get("lease") or {})
    if (
        str(lease.get("deployed_sha") or "") != deployed_sha
        or not str(lease.get("task_id") or "")
        or not str(lease.get("lease_id") or "")
        or not str(lease.get("window_id") or "")
        or not str(lease.get("phase") or "")
        or int(lease.get("revision") or 0) <= 0
    ):
        raise FinanceStorageRecoveryContractError(
            "Finance deploy lease binding is incomplete or stale"
        )
    if (
        exact_action in EXPLICIT_APPROVAL_ACTIONS
        and not str(approval_reference or "").strip()
    ):
        raise FinanceStorageRecoveryContractError(
            f"{exact_action} requires an exact approval reference"
        )
    fingerprint = str(expected_fingerprint or "")
    if (
        exact_action
        not in {"snapshot-integrity"}
        and _FINGERPRINT_RE.fullmatch(fingerprint) is None
    ):
        raise FinanceStorageRecoveryContractError(
            f"{exact_action} requires an exact plan fingerprint"
        )
    contract = recovery_contract(
        runner_contracts=runner_contracts,
        restore_job_contract=restore_job_contract,
        restore_max_resume_sequence=restore_max_resume_sequence,
        downstream_capabilities=downstream_capabilities,
    )
    registry = StoreRegistry(root)
    active = registry.load()
    active_payload = {
        "state": active.state,
        "canonical_source": active.canonical_source,
        "generation_epoch": active.generation_epoch,
        "manifest_sha256": active.manifest_sha256,
        "raw_generation_id": active.raw.generation_id,
        "operational_generation_id": active.operational.generation_id,
    }
    plan = dict(reviewed_plan or {})
    exact_cutover_recovery_deploy = False
    if (
        exact_action == "cutover-apply"
        and active.state == "cutover"
        and active.canonical_source == "split"
        and reviewed_plan is not None
    ):
        target_manifest = dict(plan.get("target_manifest") or {})
        cutover_evidence_path = (
            root
            / "generations"
            / active.generation_epoch
            / "cutover_evidence.json"
        )
        cutover_evidence = (
            _load_object(
                cutover_evidence_path,
                label="cutover result evidence",
            )
            if cutover_evidence_path.is_file()
            else {}
        )
        evidence_manifest = dict(
            cutover_evidence.get("manifest") or {}
        )
        exact_cutover_recovery_deploy = bool(
            active.generation_epoch
            == str(target_manifest.get("generation_epoch") or "")
            and active.raw.generation_id
            == str(
                (target_manifest.get("raw") or {}).get(
                    "generation_id"
                )
                or ""
            )
            and active.operational.generation_id
            == str(
                (target_manifest.get("operational") or {}).get(
                    "generation_id"
                )
                or ""
            )
            and active.source_fingerprint
            == str(target_manifest.get("source_fingerprint") or "")
            and str(cutover_evidence.get("status") or "")
            == "cutover_complete"
            and str(
                cutover_evidence.get("plan_fingerprint") or ""
            )
            == str(plan.get("fingerprint") or "")
            and str(
                evidence_manifest.get("manifest_sha256") or ""
            )
            == active.manifest_sha256
            and cutover_evidence.get("global_manifest_switched")
            is True
            and str(
                cutover_evidence.get("canonical_source") or ""
            )
            == "split"
            and cutover_evidence.get("old_monolith_retained") is True
            and cutover_evidence.get("retirement_authorized") is False
        )
    if reviewed_plan is not None:
        if str(plan.get("fingerprint") or "") != fingerprint:
            raise FinanceStorageRecoveryContractError(
                "reviewed plan fingerprint does not match recovery binding"
            )
        if (
            str(plan.get("deployed_sha") or "") != deployed_sha
            and not exact_cutover_recovery_deploy
        ):
            raise FinanceStorageRecoveryContractError(
                "reviewed plan deployed SHA does not match recovery binding"
            )
        expected_plan = {
            "apply": (
                EXPECTED_RUNNER_CONTRACTS["candidate_plan"],
                "dry_run",
                "apply_allowed_by_machine_preflight",
            ),
            "snapshot-create": (
                EXPECTED_RUNNER_CONTRACTS["snapshot_plan"],
                "snapshot_dry_run",
                "snapshot_allowed_by_machine_preflight",
            ),
            "stale-writer-stop": (
                EXPECTED_RUNNER_CONTRACTS["stale_writer_plan"],
                "stale_writer_recovery_dry_run",
                "stop_allowed_by_machine_preflight",
            ),
            "snapshot-retention-apply": (
                EXPECTED_RUNNER_CONTRACTS["snapshot_retention_plan"],
                "snapshot_retention_dry_run",
                "apply_allowed_by_machine_preflight",
            ),
            "candidate-abort-apply": (
                EXPECTED_RUNNER_CONTRACTS["candidate_abort_plan"],
                "candidate_abort_dry_run",
                "candidate_abort_allowed_by_machine_preflight",
            ),
            "cutover-apply": (
                EXPECTED_RUNNER_CONTRACTS["cutover_plan"],
                "cutover_dry_run",
                "apply_allowed_by_machine_preflight",
            ),
            "rollback-prepare": (
                EXPECTED_RUNNER_CONTRACTS["rollback_plan"],
                "rollback_dry_run",
                "prepare_allowed_by_machine_preflight",
            ),
            "rollback-apply": (
                EXPECTED_RUNNER_CONTRACTS["rollback_plan"],
                "rollback_dry_run",
                "apply_allowed_after_candidate_readback",
            ),
        }.get(exact_action)
        if expected_plan is not None and (
            str(plan.get("contract_version") or "")
            != expected_plan[0]
            or str(plan.get("mode") or "") != expected_plan[1]
            or plan.get(expected_plan[2]) is not True
        ):
            raise FinanceStorageRecoveryContractError(
                "reviewed plan contract/capability is invalid"
            )
        if (
            expected_plan is not None
            and _reviewed_plan_fingerprint(exact_action, plan)
            != fingerprint
        ):
            raise FinanceStorageRecoveryContractError(
                "reviewed plan deterministic fingerprint is stale"
            )
    elif exact_action in {
        "apply",
        "snapshot-create",
        "snapshot-retention-apply",
        "candidate-abort-apply",
        "stale-writer-stop",
        "cutover-apply",
        "rollback-prepare",
        "rollback-apply",
    }:
        raise FinanceStorageRecoveryContractError(
            f"{exact_action} requires an exact reviewed plan"
        )

    if exact_action == "snapshot-create":
        target_snapshot = dict(plan.get("target_snapshot") or {})
        snapshot_root = _within_runtime(
            root,
            Path(str(target_snapshot.get("snapshot_root") or "")),
            label="snapshot root",
        )
        snapshot_database = _within_runtime(
            root,
            Path(str(target_snapshot.get("database_path") or "")),
            label="snapshot database",
        )
        snapshot_manifest_path = _within_runtime(
            root,
            Path(str(target_snapshot.get("manifest_path") or "")),
            label="snapshot manifest",
        )
        if (
            not str(target_snapshot.get("snapshot_id") or "")
            or not str(target_snapshot.get("window_id") or "")
            or snapshot_database.parent != snapshot_root
            or snapshot_manifest_path.parent != snapshot_root
        ):
            raise FinanceStorageRecoveryContractError(
                "reviewed snapshot target identity is incomplete"
            )

    snapshot: dict[str, Any] | None = None
    if source_snapshot_manifest is not None:
        snapshot_path = _within_runtime(
            root,
            source_snapshot_manifest,
            label="source snapshot manifest",
        )
        snapshot = _load_object(
            snapshot_path,
            label="source snapshot manifest",
        )
        snapshot_status = str(snapshot.get("status") or "")
        if exact_action == "snapshot-integrity":
            valid_snapshot_status = (
                snapshot_status
                in {"captured_unverified", "integrity_verified"}
            )
        else:
            valid_snapshot_status = (
                snapshot_status == "integrity_verified"
                and snapshot.get("candidate_build_allowed") is True
            )
        if (
            str(snapshot.get("contract_version") or "")
            != EXPECTED_RUNNER_CONTRACTS["snapshot"]
            or not valid_snapshot_status
            or str(snapshot.get("deployed_sha") or "") != deployed_sha
            or not str(snapshot.get("approval_reference") or "")
        ):
            raise FinanceStorageRecoveryContractError(
                "verified approved coherent snapshot is required"
            )
        database_path = _within_runtime(
            root,
            Path(str(snapshot.get("database_path") or "")),
            label="coherent snapshot database",
        )
        if not database_path.is_file():
            raise FinanceStorageRecoveryContractError(
                "coherent snapshot database is missing"
            )
        if snapshot.get("snapshot_identity") != _path_identity(
            database_path
        ):
            raise FinanceStorageRecoveryContractError(
                "coherent snapshot database binding is stale"
            )
        capture_intent = dict(snapshot.get("capture_intent") or {})
        capture_intent_path = _within_runtime(
            root,
            Path(str(capture_intent.get("path") or "")),
            label="snapshot capture intent",
        )
        persisted_intent = _load_object(
            capture_intent_path,
            label="snapshot capture intent",
        )
        persisted_intent_fingerprint = str(
            persisted_intent.get("fingerprint") or ""
        )
        stable_intent = dict(persisted_intent)
        stable_intent.pop("fingerprint", None)
        if (
            persisted_intent_fingerprint != _fingerprint(stable_intent)
            or persisted_intent_fingerprint
            != str(capture_intent.get("fingerprint") or "")
            or str(
                persisted_intent.get("snapshot_plan_fingerprint") or ""
            )
            != str(snapshot.get("snapshot_plan_fingerprint") or "")
            or str(persisted_intent.get("database_path") or "")
            != str(database_path)
        ):
            raise FinanceStorageRecoveryContractError(
                "snapshot capture intent binding is stale"
            )

    candidate_payload: dict[str, Any] | None = None
    candidate = None
    if candidate_manifest_path is not None:
        exact_candidate_path = _within_runtime(
            root,
            candidate_manifest_path,
            label="candidate manifest",
        )
        candidate_payload = _load_object(
            exact_candidate_path,
            label="candidate manifest",
        )
        candidate = parse_manifest(candidate_payload)
        if (
            candidate.state != "shadow"
            or candidate.canonical_source != "monolith"
            or not registry.resolve(
                "finance_raw",
                manifest=candidate,
            ).is_file()
            or not registry.resolve(
                "operational",
                manifest=candidate,
            ).is_file()
        ):
            raise FinanceStorageRecoveryContractError(
                "complete unselected shadow candidate is required"
            )
        saved_candidate_plan_path = (
            exact_candidate_path.parent / "migration_plan.json"
        )
        saved_candidate_plan = _load_object(
            saved_candidate_plan_path,
            label="saved candidate plan",
        )
        candidate_plan_fingerprint = (
            str(plan.get("candidate_plan_fingerprint") or "")
            if exact_action == "cutover-apply"
            else fingerprint
        )
        if (
            str(saved_candidate_plan.get("contract_version") or "")
            != EXPECTED_RUNNER_CONTRACTS["candidate_plan"]
            or str(saved_candidate_plan.get("fingerprint") or "")
            != candidate_plan_fingerprint
            or str(
                (
                    saved_candidate_plan.get("target_generation") or {}
                ).get("generation_epoch")
                or ""
            )
            != candidate.generation_epoch
        ):
            raise FinanceStorageRecoveryContractError(
                "candidate manifest and saved reviewed plan disagree"
            )
        if (
            reviewed_plan is not None
            and exact_action == "cutover-apply"
            and str(plan.get("candidate_manifest_sha256") or "")
            != candidate.manifest_sha256
        ):
            raise FinanceStorageRecoveryContractError(
                "cutover plan and candidate manifest identity disagree"
            )

    rollback_evidence: dict[str, Any] | None = None
    if rollback_candidate_evidence_path is not None:
        rollback_path = _within_runtime(
            root,
            rollback_candidate_evidence_path,
            label="rollback candidate evidence",
        )
        rollback_evidence = _load_object(
            rollback_path,
            label="rollback candidate evidence",
        )
        if (
            str(rollback_evidence.get("contract_version") or "")
            != EXPECTED_RUNNER_CONTRACTS["rollback_candidate"]
            or str(rollback_evidence.get("status") or "")
            != "candidate_ready"
            or str(rollback_evidence.get("plan_fingerprint") or "")
            != fingerprint
        ):
            raise FinanceStorageRecoveryContractError(
                "exact rollback candidate evidence is required"
            )
        stable_rollback = dict(rollback_evidence)
        candidate_fingerprint = str(
            stable_rollback.pop("candidate_fingerprint", "") or ""
        )
        if (
            candidate_fingerprint != _fingerprint(stable_rollback)
            or _within_runtime(
                root,
                Path(
                    str(
                        rollback_evidence.get("candidate_path") or ""
                    )
                ),
                label="rollback candidate database",
            )
            != Path(
                str((plan.get("target") or {}).get("path") or "")
            ).resolve()
        ):
            raise FinanceStorageRecoveryContractError(
                "rollback candidate fingerprint/path binding is stale"
            )

    if exact_action in {
        "snapshot-create",
        "apply",
        "candidate-abort-apply",
        "stale-writer-stop",
    }:
        if active.state != "monolith" or active.canonical_source != "monolith":
            raise FinanceStorageRecoveryContractError(
                f"{exact_action} requires the canonical monolith"
            )
    if exact_action == "snapshot-retention-apply":
        if active.state != "monolith" or active.canonical_source != "monolith":
            raise FinanceStorageRecoveryContractError(
                "snapshot-retention-apply requires the canonical monolith"
            )
    if exact_action == "apply" and snapshot is None:
        raise FinanceStorageRecoveryContractError(
            "candidate apply requires a verified coherent snapshot"
        )
    if exact_action.startswith("shadow-") or exact_action == "live-tail-apply":
        if candidate is None:
            raise FinanceStorageRecoveryContractError(
                f"{exact_action} requires an exact candidate manifest"
            )
        if active.state != "monolith" or active.canonical_source != "monolith":
            raise FinanceStorageRecoveryContractError(
                f"{exact_action} requires the canonical monolith"
            )
    if exact_action == "cutover-apply":
        if candidate is None:
            raise FinanceStorageRecoveryContractError(
                "cutover apply requires an exact candidate manifest"
            )
        exact_post_manifest = bool(
            active.state == "cutover"
            and active.canonical_source == "split"
            and active.generation_epoch == candidate.generation_epoch
            and active.raw.generation_id == candidate.raw.generation_id
            and active.raw.relative_path == candidate.raw.relative_path
            and active.operational.generation_id
            == candidate.operational.generation_id
            and active.operational.relative_path
            == candidate.operational.relative_path
            and active.source_fingerprint
            == candidate.source_fingerprint
        )
        if (
            not exact_post_manifest
            and (
                active.state != "monolith"
                or active.canonical_source != "monolith"
            )
        ):
            raise FinanceStorageRecoveryContractError(
                "cutover canonical state is ambiguous"
            )
    if exact_action in {"rollback-prepare", "rollback-apply"}:
        target = dict(plan.get("target") or {})
        reviewed_active = dict(plan.get("active_manifest") or {})
        exact_split_manifest = bool(
            active.state == "cutover"
            and active.canonical_source == "split"
            and active.manifest_sha256
            == str(reviewed_active.get("manifest_sha256") or "")
        )
        rollback_source_fingerprint = (
            _fingerprint(
                {
                    "split_manifest": str(
                        (
                            rollback_evidence
                            or {}
                        ).get("active_manifest_sha256")
                        or ""
                    ),
                    "rollback_candidate": str(
                        (
                            rollback_evidence
                            or {}
                        ).get("candidate_fingerprint")
                        or ""
                    ),
                }
            )
            if rollback_evidence is not None
            else ""
        )
        exact_post_manifest = bool(
            exact_action == "rollback-apply"
            and active.state == "monolith"
            and active.canonical_source == "monolith"
            and active.generation_epoch
            == str(target.get("generation_epoch") or "")
            and str(target.get("generation_id") or "")
            == active.raw.generation_id
            and active.raw.generation_id
            == active.operational.generation_id
            and active.raw.relative_path
            == str(target.get("relative_path") or "")
            and active.operational.relative_path
            == str(target.get("relative_path") or "")
            and active.rollback_generation_id
            == str(reviewed_active.get("generation_epoch") or "")
            and active.source_fingerprint
            == rollback_source_fingerprint
        )
        if not exact_post_manifest and not exact_split_manifest:
            raise FinanceStorageRecoveryContractError(
                "rollback canonical state is ambiguous"
            )
        if exact_action == "rollback-apply" and rollback_evidence is None:
            raise FinanceStorageRecoveryContractError(
                "rollback apply requires exact candidate evidence"
            )
        if (
            rollback_evidence is not None
            and str(rollback_evidence.get("active_manifest_sha256") or "")
            != str(
                (plan.get("active_manifest") or {}).get(
                    "manifest_sha256"
                )
                or ""
            )
        ):
            raise FinanceStorageRecoveryContractError(
                "rollback candidate and reviewed split manifest disagree"
            )

    barrier = barrier_status(root)
    expected_kind = BARRIER_OWNING_ACTIONS.get(exact_action)
    expected_window = ""
    if exact_action == "snapshot-create":
        expected_window = str(
            (plan.get("target_snapshot") or {}).get("window_id") or ""
        )
    elif exact_action == "cutover-apply":
        expected_window = (
            "final-cutover-" + fingerprint.removeprefix("sha256:")[:20]
        )
    elif exact_action == "rollback-apply":
        expected_window = (
            "rollback-" + fingerprint.removeprefix("sha256:")[:20]
        )
    boundary_classification = "not_required"
    if expected_kind:
        exact_active_boundary = bool(
            barrier.get("active") is True
            and str(barrier.get("window_kind") or "") == expected_kind
            and str(barrier.get("window_id") or "") == expected_window
            and str(barrier.get("plan_fingerprint") or "") == fingerprint
        )
        if exact_phase == "pre_barrier":
            if barrier.get("active") is True:
                if not exact_active_boundary:
                    raise FinanceStorageRecoveryContractError(
                        "a different or ambiguous write barrier is active"
                    )
                barrier_phase = str(barrier.get("phase") or "")
                if (
                    exact_action == "snapshot-create"
                    and barrier_phase == "restoring"
                    and barrier.get("hold_confirmed") is True
                ):
                    boundary_classification = (
                        "exact_restore_release_resume"
                    )
                elif barrier_phase not in {"acquiring", "held"}:
                    raise FinanceStorageRecoveryContractError(
                        "write barrier is already restoring; finish exact "
                        "restore/release before re-dispatch"
                    )
                else:
                    boundary_classification = "exact_idempotent_resume"
            else:
                boundary_classification = "fresh_acquire"
        else:
            if (
                not exact_active_boundary
                or str(barrier.get("phase") or "") != "held"
                or barrier.get("hold_confirmed") is not True
            ):
                raise FinanceStorageRecoveryContractError(
                    "exact confirmed held write barrier is required"
                )
            maintenance = _load_object(
                root / ".business-data-maintenance.json",
                label="business-data maintenance state",
            )
            if (
                str(maintenance.get("schema_version") or "")
                != "business_data_maintenance_v1"
                or str(maintenance.get("phase") or "") != "held"
                or not bool(
                    (maintenance.get("hold_readback") or {}).get("quiet")
                )
            ):
                raise FinanceStorageRecoveryContractError(
                    "exact quiet maintenance hold is required"
                )
            boundary_classification = "held_and_recoverable"
    elif barrier.get("active") is True:
        raise FinanceStorageRecoveryContractError(
            f"{exact_action} is blocked by an active maintenance barrier"
        )

    transition_prefix = {
        "snapshot-create": "snapshot.",
        "snapshot-integrity": "snapshot.",
        "snapshot-retention-apply": "snapshot_retention.",
        "apply": "candidate.",
        "candidate-abort-apply": "candidate.",
        "shadow-activate": "shadow.",
        "shadow-reconcile": "shadow.",
        "shadow-verify": "shadow.",
        "live-tail-apply": "shadow.",
        "shadow-deactivate": "shadow.",
        "cutover-apply": "cutover.",
        "rollback-prepare": "rollback.",
        "rollback-apply": "rollback.",
        "stale-writer-stop": "snapshot.",
    }[exact_action]
    relevant_transitions = [
        item["transition"]
        for item in contract["transitions"]
        if str(item["transition"]).startswith(transition_prefix)
    ]
    if exact_action in BARRIER_OWNING_ACTIONS:
        relevant_transitions.insert(0, "transport.hold_mutation")
    evidence: dict[str, Any] = {
        "contract_version": RECOVERY_VALIDATION_VERSION,
        "status": "ready",
        "action": exact_action,
        "phase": exact_phase,
        "deployed_sha": deployed_sha,
        "approval_reference": str(approval_reference or ""),
        "plan_fingerprint": fingerprint,
        "lease": {
            "task_id": str(lease.get("task_id") or ""),
            "lease_id": str(lease.get("lease_id") or ""),
            "revision": int(lease.get("revision") or 0),
            "window_id": str(lease.get("window_id") or ""),
            "phase": str(lease.get("phase") or ""),
            "deployed_sha": str(lease.get("deployed_sha") or ""),
        },
        "active_manifest": active_payload,
        "boundary_classification": boundary_classification,
        "expected_window_kind": expected_kind or "",
        "expected_window_id": expected_window,
        "recovery_contract_version": contract["contract_version"],
        "recovery_contract_fingerprint": contract["fingerprint"],
        "relevant_transitions": relevant_transitions,
        "downstream_capabilities": {
            key: bool(value)
            for key, value in sorted(downstream_capabilities.items())
        },
        "fail_closed": True,
    }
    if snapshot is not None:
        evidence["source_snapshot"] = {
            "snapshot_id": str(snapshot.get("snapshot_id") or ""),
            "status": str(snapshot.get("status") or ""),
            "evidence_fingerprint": str(
                snapshot.get("evidence_fingerprint") or ""
            ),
        }
    if candidate is not None:
        evidence["candidate"] = {
            "manifest_sha256": candidate.manifest_sha256,
            "generation_epoch": candidate.generation_epoch,
            "raw_generation_id": candidate.raw.generation_id,
            "operational_generation_id": (
                candidate.operational.generation_id
            ),
        }
    if rollback_evidence is not None:
        evidence["rollback_candidate"] = {
            "candidate_fingerprint": str(
                rollback_evidence.get("candidate_fingerprint") or ""
            ),
            "active_manifest_sha256": str(
                rollback_evidence.get("active_manifest_sha256") or ""
            ),
        }
    if exact_cutover_recovery_deploy:
        evidence["post_manifest_recovery_deploy"] = {
            "status": "exact_cutover_evidence_readback",
            "original_deployed_sha": str(
                plan.get("deployed_sha") or ""
            ),
            "recovery_deployed_sha": deployed_sha,
            "active_manifest_sha256": active.manifest_sha256,
        }
    evidence["fingerprint"] = _fingerprint(evidence)
    return evidence
