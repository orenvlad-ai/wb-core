"""Deterministic fail-closed checks for Finance deploy-lease evidence."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.finance_migration_deploy_lease import (  # noqa: E402
    FinanceMigrationDeployLeaseError,
    LEASE_POLICY,
    LEASE_READBACK_CONTRACT,
    baseline_invalidation_epoch,
    evidence_fingerprint,
    validate_finance_migration_deploy_lease,
)


SHA_A = "a" * 40


def _evidence(*, observed: float, expires: float) -> dict[str, object]:
    payload: dict[str, object] = {
        "contract_version": LEASE_READBACK_CONTRACT,
        "policy": LEASE_POLICY,
        "status": "active",
        "allows_finance_migration": True,
        "global_release_blocked": True,
        "observed_at": datetime.fromtimestamp(
            observed,
            tz=timezone.utc,
        ).isoformat().replace("+00:00", "Z"),
        "ambiguous_reasons": [],
        "lease": {
            "lease_id": "finance-split-test",
            "task_id": "019fa739-505c-74b1-9f24-02a2c1f9bf1b",
            "anchor_pr": 850,
            "head_sha": "b" * 40,
            "deployed_sha": SHA_A,
            "window_id": "pre-snapshot-1",
            "phase": "pre-snapshot",
            "revision": 1,
            "acquired_at": datetime.fromtimestamp(
                observed - 60,
                tz=timezone.utc,
            ).isoformat().replace("+00:00", "Z"),
            "expires_at": datetime.fromtimestamp(
                expires,
                tz=timezone.utc,
            ).isoformat().replace("+00:00", "Z"),
            "baseline_invalidation_epoch": baseline_invalidation_epoch(
                anchor_pr=850,
                deployed_sha=SHA_A,
                lease_id="finance-split-test",
                revision=1,
                task_id="019fa739-505c-74b1-9f24-02a2c1f9bf1b",
            ),
            "recovery_policy": "owner_bound_recovery_rebind_required_v1",
        },
    }
    payload["fingerprint"] = evidence_fingerprint(payload)
    return payload


def _rejected(payload: dict[str, object], *, now: float, sha: str = SHA_A) -> None:
    try:
        validate_finance_migration_deploy_lease(
            payload,
            deployed_sha=sha,
            now=now,
        )
    except FinanceMigrationDeployLeaseError:
        return
    raise AssertionError("invalid Finance deploy lease was accepted")


def main() -> int:
    now = 1_800_000_000.0
    payload = _evidence(observed=now - 10, expires=now + 3600)
    accepted = validate_finance_migration_deploy_lease(
        payload,
        deployed_sha=SHA_A,
        now=now,
    )
    assert accepted["lease"]["revision"] == 1
    assert (
        validate_finance_migration_deploy_lease(
            accepted,
            deployed_sha=SHA_A,
            now=now,
        )["fingerprint"]
        == accepted["fingerprint"]
    )

    tampered = deepcopy(payload)
    tampered["lease"]["phase"] = "backfill"  # type: ignore[index]
    _rejected(tampered, now=now)
    _rejected(payload, now=now, sha="d" * 40)

    stale = _evidence(observed=now - 301, expires=now + 3600)
    _rejected(stale, now=now)
    expired = _evidence(observed=now - 10, expires=now)
    _rejected(expired, now=now)
    unbounded = _evidence(
        observed=now - 10,
        expires=now + 3 * 24 * 60 * 60 + 1,
    )
    _rejected(unbounded, now=now)

    wrong_epoch = deepcopy(payload)
    wrong_epoch["lease"]["baseline_invalidation_epoch"] = (  # type: ignore[index]
        "sha256:" + "d" * 64
    )
    wrong_epoch["fingerprint"] = evidence_fingerprint(wrong_epoch)
    _rejected(wrong_epoch, now=now)

    ambiguous = deepcopy(payload)
    ambiguous["ambiguous_reasons"] = ["lost_owner"]
    ambiguous["fingerprint"] = evidence_fingerprint(ambiguous)
    _rejected(ambiguous, now=now)

    inactive = deepcopy(payload)
    inactive["status"] = "stale"
    inactive["allows_finance_migration"] = False
    inactive["fingerprint"] = evidence_fingerprint(inactive)
    _rejected(inactive, now=now)

    print("finance_migration_deploy_lease_smoke: 10/10 ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
