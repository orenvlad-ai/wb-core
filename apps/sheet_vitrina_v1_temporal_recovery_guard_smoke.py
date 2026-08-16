"""Sandbox-compatible exact-date temporal recovery gate smoke."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_temporal_closure_retry_live import (  # noqa: E402
    TemporalRecoveryError,
    apply_explicit_recovery,
    build_explicit_recovery_plan,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)


AS_OF_DATE = "2026-08-15"
NOW = datetime(2026, 8, 16, 7, tzinfo=timezone.utc)
DEPLOYED_SHA = "a" * 40
APPROVAL_DIGEST = "sha256:" + "b" * 64
FIXTURE = (
    ROOT
    / "artifacts"
    / "registry_upload_http_entrypoint"
    / "input"
    / "registry_upload_bundle__fixture.json"
)


def main() -> None:
    with TemporaryDirectory(prefix="wb-temporal-recovery-guard-") as tmp:
        root = Path(tmp)
        runtime_dir = root / "runtime"
        marker = root / "runtime-sha"
        marker.write_text(DEPLOYED_SHA + "\n", encoding="utf-8")
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        bundle = json.loads(FIXTURE.read_text(encoding="utf-8"))
        accepted = runtime.ingest_bundle(
            bundle,
            activated_at="2026-08-16T06:00:00Z",
        )
        if accepted.status != "accepted":
            raise AssertionError(f"fixture bundle was rejected: {accepted}")

        plan = build_explicit_recovery_plan(
            runtime_dir=runtime_dir,
            raw_dates=[AS_OF_DATE],
            sha_marker_path=marker,
            now=NOW,
        )
        if plan["mode"] != "query_only_dry_run":
            raise AssertionError("explicit date did not default to query-only dry-run")
        if Path(plan["backup_destination"]).exists():
            raise AssertionError("dry-run created a backup or other apply artifact")
        repeated = build_explicit_recovery_plan(
            runtime_dir=runtime_dir,
            raw_dates=[AS_OF_DATE],
            sha_marker_path=marker,
            now=NOW,
        )
        if repeated["manifest_fingerprint"] != plan["manifest_fingerprint"]:
            raise AssertionError("unchanged query-only manifest is not deterministic")

        try:
            build_explicit_recovery_plan(
                runtime_dir=runtime_dir,
                raw_dates=["2026-08-16"],
                sha_marker_path=marker,
                now=NOW,
            )
        except TemporalRecoveryError as exc:
            if "must be closed" not in str(exc):
                raise AssertionError(f"unexpected open-day guard: {exc}") from exc
        else:
            raise AssertionError("current business day was accepted as recovery target")

        def injected_cycle(dates: list[str]) -> dict[str, object]:
            if dates != [AS_OF_DATE]:
                raise AssertionError(f"apply dates drifted: {dates}")
            runtime.save_temporal_source_snapshot(
                source_key="stocks",
                snapshot_date=AS_OF_DATE,
                captured_at="2026-08-16T06:30:00Z",
                payload={
                    "kind": "success",
                    "snapshot_date": AS_OF_DATE,
                    "count": 33,
                    "warehouse_granularity_complete": False,
                },
            )
            return {
                "status": "success",
                "operation": "injected_exact_date_cycle",
                "requested_dates": dates,
            }

        result = apply_explicit_recovery(
            runtime_dir=runtime_dir,
            raw_dates=[AS_OF_DATE],
            sha_marker_path=marker,
            manifest_fingerprint=plan["manifest_fingerprint"],
            deployed_sha=DEPLOYED_SHA,
            approval_reference="smoke-owner-gate-1",
            approval_digest=APPROVAL_DIGEST,
            cycle_runner=injected_cycle,
            now=NOW,
        )
        if result["status"] != "success":
            raise AssertionError(f"guarded apply failed: {result}")
        backup = dict(result.get("backup") or {})
        if backup.get("integrity_check") != "ok" or not Path(str(backup.get("path"))).is_file():
            raise AssertionError(f"coherent backup evidence missing: {backup}")
        if not result.get("non_target_invariants_ok"):
            raise AssertionError(f"non-target drift was not blocked: {result}")
        changes = result["table_changes"]["temporal_source_snapshots"]
        if changes["inserted"] != 1:
            raise AssertionError(f"target mutation readback drifted: {changes}")

        try:
            apply_explicit_recovery(
                runtime_dir=runtime_dir,
                raw_dates=[AS_OF_DATE],
                sha_marker_path=marker,
                manifest_fingerprint=plan["manifest_fingerprint"],
                deployed_sha=DEPLOYED_SHA,
                approval_reference="smoke-owner-gate-1",
                approval_digest=APPROVAL_DIGEST,
                cycle_runner=injected_cycle,
                now=NOW,
            )
        except TemporalRecoveryError as exc:
            if "manifest drifted" not in str(exc):
                raise AssertionError(f"unexpected stale-manifest guard: {exc}") from exc
        else:
            raise AssertionError("stale manifest was accepted after target state changed")

    print("query_only_default: ok")
    print("closed_day_guard: ok")
    print("deterministic_manifest: ok")
    print("exact_gate_and_backup: ok")
    print("target_readback_non_target_invariants: ok")
    print("stale_manifest_rejected: ok")
    print("smoke-check passed")


if __name__ == "__main__":
    main()
