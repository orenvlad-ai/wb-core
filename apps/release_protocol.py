"""Protocol-v2 release and production-safety primitives without queue state."""

from __future__ import annotations

from enum import Enum
import re
from typing import Any, Mapping


CANONICAL_REPOSITORY = "orenvlad-ai/wb-core"
CANONICAL_PRODUCTION_TARGET_ID = "wb_core_eu_hosted_runtime_active"
CANONICAL_PRODUCTION_SERVICE_NAME = "wb-core-registry-http.service"
PROTOCOL_V2_CUTOVER_EPOCH = "4f0333ad7b500967fe4175aa6e53359043832360"
PRODUCTION_MANIFEST_SCHEMA = "wb-core.production-apply-manifest/v2"
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
OPERATION_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


class ExecutionContour(str, Enum):
    READ_ONLY = "read-only"
    USER_ARTIFACT = "user-artifact"
    REPO_ONLY = "repo-only"
    LIVE_RUNTIME = "live/runtime"
    PRODUCTION_DATA_MUTATION = "production data mutation/backfill"


PRODUCTION_MUTATION_REQUIREMENTS = (
    "dry_run_default",
    "explicit_apply",
    "bounded_scope",
    "pre_change_digest",
    "backup_evidence",
    "expected_affected_records",
    "non_target_invariants",
    "idempotency_or_recovery",
    "post_apply_readback",
    "reconciliation",
)


def github_closure_required(contour: ExecutionContour) -> bool:
    return contour not in {ExecutionContour.READ_ONLY, ExecutionContour.USER_ARTIFACT}


def validate_production_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    missing = []
    if manifest.get("schema") != PRODUCTION_MANIFEST_SCHEMA:
        missing.append("schema")
    missing.extend(
        field for field in PRODUCTION_MUTATION_REQUIREMENTS if manifest.get(field) is not True
    )
    command_blocks = manifest.get("commands")
    if not isinstance(command_blocks, Mapping):
        missing.append("commands")
    else:
        for field in ("dry_run", "apply", "readback", "reconcile"):
            command = command_blocks.get(field)
            if (
                not isinstance(command, list)
                or not command
                or any(not isinstance(part, str) or not part for part in command)
            ):
                missing.append(f"commands.{field}")
    operation_id = manifest.get("operation_id")
    if not isinstance(operation_id, str) or OPERATION_RE.fullmatch(operation_id) is None:
        missing.append("operation_id")
    if manifest.get("target_id") != CANONICAL_PRODUCTION_TARGET_ID:
        missing.append("target_id")
    if manifest.get("deployed_sha_contract") != "exact-merge-sha":
        missing.append("deployed_sha_contract")
    if SHA256_RE.fullmatch(str(manifest.get("pre_change_digest_value") or "")) is None:
        missing.append("pre_change_digest_value")
    backup = manifest.get("backup_evidence_value")
    if not isinstance(backup, str) or not backup.strip() or len(backup) > 500:
        missing.append("backup_evidence_value")
    affected = manifest.get("expected_affected_record_count")
    if not isinstance(affected, int) or isinstance(affected, bool) or affected < 0:
        missing.append("expected_affected_record_count")
    invariant_ids = manifest.get("non_target_invariant_ids")
    if (
        not isinstance(invariant_ids, list)
        or not invariant_ids
        or any(not isinstance(item, str) or not item.strip() for item in invariant_ids)
        or len(set(invariant_ids)) != len(invariant_ids)
    ):
        missing.append("non_target_invariant_ids")
    recovery = manifest.get("recovery_contract")
    if not isinstance(recovery, Mapping):
        missing.append("recovery_contract")
    else:
        if recovery.get("mode") not in {"idempotent", "bounded-recovery"}:
            missing.append("recovery_contract.mode")
        recovery_id = recovery.get("id")
        if not isinstance(recovery_id, str) or not recovery_id.strip():
            missing.append("recovery_contract.id")
    if manifest.get("query_only_manifest_readback") is not True:
        missing.append("query_only_manifest_readback")
    return {
        "valid": not missing,
        "missing_requirements": sorted(set(missing)),
        "apply_allowed": not missing,
    }
