"""Validation contract for the GitHub-owned Finance migration deploy lease.

The global lease itself is durable GitHub state owned by the Release Train.
Hosted Finance commands consume a freshly generated, machine-readable status
document and bind it to the exact SHA deployed on the canonical target.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping


LEASE_READBACK_CONTRACT = "wb_core_finance_migration_deploy_lease_readback_v1"
LEASE_POLICY = "finance_migration_global_deploy_hold_v1"
LEASE_RECOVERY_POLICY = "owner_bound_recovery_rebind_required_v1"
MIN_LEASE_SECONDS = 30 * 60
MAX_LEASE_SECONDS = 3 * 24 * 60 * 60
DEFAULT_MAX_EVIDENCE_AGE_SECONDS = 5 * 60


class FinanceMigrationDeployLeaseError(ValueError):
    """The global Finance migration deploy lease is absent, stale, or ambiguous."""


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def evidence_fingerprint(payload: Mapping[str, Any]) -> str:
    normalized = {
        key: value for key, value in payload.items() if key != "fingerprint"
    }
    return "sha256:" + hashlib.sha256(
        canonical_json(normalized).encode("utf-8")
    ).hexdigest()


def baseline_invalidation_epoch(
    *,
    anchor_pr: int,
    deployed_sha: str,
    lease_id: str,
    revision: int,
    task_id: str,
) -> str:
    material = {
        "anchor_pr": int(anchor_pr),
        "deployed_sha": str(deployed_sha),
        "lease_id": str(lease_id),
        "revision": int(revision),
        "task_id": str(task_id),
    }
    return "sha256:" + hashlib.sha256(
        canonical_json(material).encode("utf-8")
    ).hexdigest()


def _bounded_token(value: Any, *, field: str) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > 160
        or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
            for character in normalized
        )
    ):
        raise FinanceMigrationDeployLeaseError(
            f"Finance migration deploy lease {field} is invalid"
        )
    return normalized


def utc_timestamp(value: Any, *, field: str) -> float:
    rendered = str(value or "").strip()
    if not rendered:
        raise FinanceMigrationDeployLeaseError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FinanceMigrationDeployLeaseError(
            f"{field} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def validate_finance_migration_deploy_lease(
    payload: Mapping[str, Any],
    *,
    deployed_sha: str,
    now: float | None = None,
    max_evidence_age_seconds: float = DEFAULT_MAX_EVIDENCE_AGE_SECONDS,
) -> dict[str, Any]:
    """Return normalized active lease evidence or fail closed.

    The caller supplies the SHA read from the canonical runtime.  A lease
    rebind therefore invalidates every earlier snapshot/plan/fingerprint
    automatically: old evidence names the previous SHA or revision.
    """

    if max_evidence_age_seconds <= 0:
        raise ValueError("max evidence age must be positive")
    if payload.get("contract_version") != LEASE_READBACK_CONTRACT:
        raise FinanceMigrationDeployLeaseError(
            "Finance migration deploy lease contract is missing or unsupported"
        )
    if payload.get("policy") != LEASE_POLICY:
        raise FinanceMigrationDeployLeaseError(
            "Finance migration deploy lease policy is missing or unsupported"
        )
    expected_fingerprint = evidence_fingerprint(payload)
    if str(payload.get("fingerprint") or "") != expected_fingerprint:
        raise FinanceMigrationDeployLeaseError(
            "Finance migration deploy lease fingerprint is stale or invalid"
        )
    if payload.get("status") != "active":
        raise FinanceMigrationDeployLeaseError(
            "Finance migration deploy lease is not active"
        )
    if payload.get("allows_finance_migration") is not True:
        raise FinanceMigrationDeployLeaseError(
            "Finance migration deploy lease remains fail-closed"
        )
    if payload.get("global_release_blocked") is not True:
        raise FinanceMigrationDeployLeaseError(
            "Finance migration deploy lease lacks global Release Train hold readback"
        )

    lease = payload.get("lease")
    if not isinstance(lease, Mapping):
        raise FinanceMigrationDeployLeaseError(
            "Finance migration deploy lease identity is missing"
        )
    required_text = {
        "lease_id": _bounded_token(lease.get("lease_id"), field="lease_id"),
        "task_id": _bounded_token(lease.get("task_id"), field="task_id"),
        "window_id": _bounded_token(lease.get("window_id"), field="window_id"),
        "phase": _bounded_token(lease.get("phase"), field="phase"),
    }
    try:
        anchor_pr = int(lease.get("anchor_pr") or 0)
        revision = int(lease.get("revision") or 0)
    except (TypeError, ValueError) as exc:
        raise FinanceMigrationDeployLeaseError(
            "Finance migration deploy lease numeric identity is invalid"
        ) from exc
    if anchor_pr <= 0 or revision <= 0:
        raise FinanceMigrationDeployLeaseError(
            "Finance migration deploy lease anchor/revision is invalid"
        )

    normalized_deployed = str(deployed_sha or "").strip().lower()
    bound_deployed = str(lease.get("deployed_sha") or "").strip().lower()
    bound_head = str(lease.get("head_sha") or "").strip().lower()
    if (
        len(normalized_deployed) != 40
        or any(character not in "0123456789abcdef" for character in normalized_deployed)
        or bound_deployed != normalized_deployed
        or len(bound_head) != 40
        or any(character not in "0123456789abcdef" for character in bound_head)
    ):
        raise FinanceMigrationDeployLeaseError(
            "Finance migration deploy lease does not match exact code/deployed SHA identity"
        )

    observed = utc_timestamp(payload.get("observed_at"), field="observed_at")
    acquired = utc_timestamp(lease.get("acquired_at"), field="acquired_at")
    expires = utc_timestamp(lease.get("expires_at"), field="expires_at")
    current = (
        datetime.now(timezone.utc).timestamp()
        if now is None
        else float(now)
    )
    if observed > current + 30:
        raise FinanceMigrationDeployLeaseError(
            "Finance migration deploy lease observation is from the future"
        )
    if current - observed > max_evidence_age_seconds:
        raise FinanceMigrationDeployLeaseError(
            "Finance migration deploy lease readback is stale"
        )
    if expires <= current:
        raise FinanceMigrationDeployLeaseError(
            "Finance migration deploy lease owner window is stale; explicit rebind is required"
        )
    duration = expires - acquired
    if (
        acquired > observed
        or duration < MIN_LEASE_SECONDS
        or duration > MAX_LEASE_SECONDS
    ):
        raise FinanceMigrationDeployLeaseError(
            "Finance migration deploy lease time boundary is invalid"
        )
    invalidation_epoch = str(
        lease.get("baseline_invalidation_epoch") or ""
    ).lower()
    expected_invalidation_epoch = baseline_invalidation_epoch(
        anchor_pr=anchor_pr,
        deployed_sha=bound_deployed,
        lease_id=required_text["lease_id"],
        revision=revision,
        task_id=required_text["task_id"],
    )
    if invalidation_epoch != expected_invalidation_epoch:
        raise FinanceMigrationDeployLeaseError(
            "Finance migration baseline invalidation epoch is invalid"
        )
    if (
        str(lease.get("recovery_policy") or "")
        != LEASE_RECOVERY_POLICY
    ):
        raise FinanceMigrationDeployLeaseError(
            "Finance migration recovery policy is missing or unsupported"
        )
    if payload.get("ambiguous_reasons") not in ([], ()):
        raise FinanceMigrationDeployLeaseError(
            "Finance migration deploy lease has ambiguous durable state"
        )

    normalized: dict[str, Any] = {
        "contract_version": LEASE_READBACK_CONTRACT,
        "policy": LEASE_POLICY,
        "status": "active",
        "allows_finance_migration": True,
        "global_release_blocked": True,
        "observed_at": str(payload["observed_at"]),
        "ambiguous_reasons": [],
        "lease": {
            "lease_id": required_text["lease_id"],
            "task_id": required_text["task_id"],
            "anchor_pr": anchor_pr,
            "head_sha": bound_head,
            "deployed_sha": bound_deployed,
            "window_id": required_text["window_id"],
            "phase": required_text["phase"],
            "revision": revision,
            "acquired_at": str(lease.get("acquired_at") or ""),
            "expires_at": str(lease.get("expires_at") or ""),
            "baseline_invalidation_epoch": invalidation_epoch,
            "recovery_policy": str(lease.get("recovery_policy") or ""),
        },
    }
    normalized["fingerprint"] = evidence_fingerprint(normalized)
    return normalized
