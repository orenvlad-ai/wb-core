#!/usr/bin/env python3
"""Regression smoke for exact stale Stage 7C recovery supersession."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.ff_pool_cutover_production_smoke import (  # noqa: E402
    SHIPMENT_ID,
    _Clock,
    _barrier,
    _seed,
)
from apps.warehouse_functional_runner import (  # noqa: E402
    _run_bounded_recovery_retention,
)
from apps import registry_upload_http_entrypoint_hosted_runtime as hosted_runtime  # noqa: E402
from packages.application.ff_pool_cutover_production import (  # noqa: E402
    FfPoolCutoverProductionMutation,
)
from packages.application.ff_pool_cutover_recovery_supersession import (  # noqa: E402
    FfPoolCutoverRecoverySupersession,
    FfPoolCutoverRecoverySupersessionError,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
    _ensure_schema,
)
from packages.application.warehouse_functional import (  # noqa: E402
    _warehouse_sync_error_reason,
    ensure_warehouse_functional_schema,
)


OLD_SHA = "a" * 40
SUCCESS_SHA = "b" * 40
RUNNER_SHA = "d" * 40


def main() -> int:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        runtime = _runtime(root / "proved")
        old_operation_id = _create_failed_stage_7c(runtime, root / "old-backups")
        _create_successful_stage_7c(runtime, root / "success-backups")

        before = _invariants(runtime.db_path)
        recovery_before = _recovery_evidence(runtime.db_path, old_operation_id)
        before_bytes = runtime.db_path.read_bytes()
        runner = FfPoolCutoverRecoverySupersession(
            runtime_dir=runtime.runtime_dir,
            deployed_sha=RUNNER_SHA,
        )
        plan = runner.build_plan(old_operation_id)
        assert plan["status"] == "ready", plan["blockers"]
        assert plan["apply_allowed"] is True
        assert plan["deployed_sha"] == RUNNER_SHA
        assert plan["proof"]["canonical_success"]["deployed_sha"] == SUCCESS_SHA
        assert plan["proof"]["target_failure"]["old_manifest_absent"] is True
        assert plan["proof"]["target_failure"]["old_recovery_events_absent"] is True
        assert plan["effect"]["historical_debit_replayed"] is False
        assert runtime.db_path.read_bytes() == before_bytes, "dry-run changed the registry"

        artifact_path = _target_manifest_path(runtime.db_path, old_operation_id)
        artifact_bytes = artifact_path.read_bytes()
        artifact_path.write_bytes(artifact_bytes + b"tamper")
        try:
            runner.apply(
                plan,
                fingerprint=plan["fingerprint"],
                actor="supersession-smoke",
                approval_reference="owner-comment:smoke",
            )
        except FfPoolCutoverRecoverySupersessionError as exc:
            assert exc.code == "supersession_proof_drift"
        else:
            raise AssertionError("changed checkpoint artifact did not block apply")
        finally:
            artifact_path.write_bytes(artifact_bytes)
        assert runner.build_plan(old_operation_id)["fingerprint"] == plan["fingerprint"]

        timeout_bearing_error = (
            "warehouse recovery contains unresolved protected T2 evidence; "
            f"another domain checkpoint is blocked: {old_operation_id}; "
            "sqlite_busy_timeout_ms=120000"
        )
        reason = _warehouse_sync_error_reason(timeout_bearing_error)
        assert "Recovery Policy" in reason and "не ответил" not in reason

        result = runner.apply(
            plan,
            fingerprint=plan["fingerprint"],
            actor="supersession-smoke",
            approval_reference="owner-comment:smoke",
        )
        assert result["status"] == "applied_superseded"
        readback = result["readback"]
        assert readback["status"] == "superseded_verified"
        assert readback["target_blocks_future_publication"] is False
        assert readback["artifacts_preserved"] is True
        assert readback["blocking_t2_operation_ids"] == []
        assert _invariants(runtime.db_path) == before
        recovery_after = _recovery_evidence(runtime.db_path, old_operation_id)
        assert recovery_after["rollback_available"] == recovery_before["rollback_available"]
        assert recovery_after["last_error"] == recovery_before["last_error"]
        assert recovery_after["artifact_count"] == recovery_before["artifact_count"]
        assert _run_bounded_recovery_retention(runtime)["status"] == "no_change"

        repeated = runner.apply(
            plan,
            fingerprint=plan["fingerprint"],
            actor="supersession-smoke",
            approval_reference="owner-comment:smoke",
        )
        assert repeated["status"] == "already_superseded"
        assert repeated["idempotent"] is True
        _assert_append_only(runtime.db_path, old_operation_id)

        ambiguous = _runtime(root / "ambiguous")
        ambiguous_operation_id = _create_failed_stage_7c(
            ambiguous, root / "ambiguous-backups"
        )
        blocked = FfPoolCutoverRecoverySupersession(
            runtime_dir=ambiguous.runtime_dir,
            deployed_sha=RUNNER_SHA,
        ).build_plan(ambiguous_operation_id)
        assert blocked["status"] == "blocked"
        assert blocked["apply_allowed"] is False
        assert any(
            item["code"] == "canonical_cutover_manifest_ambiguous"
            for item in blocked["blockers"]
        )
        try:
            _run_bounded_recovery_retention(ambiguous)
        except RuntimeError as exc:
            assert ambiguous_operation_id in str(exc)
        else:
            raise AssertionError("unproven failed recovery stopped blocking publication")

        _assert_hosted_contract(root)

    print("ff_pool_cutover_recovery_supersession_smoke: OK")
    return 0


def _runtime(path: Path) -> RegistryUploadDbBackedRuntime:
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=path)
    path.mkdir(parents=True)
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_warehouse_functional_schema(conn)
        _ensure_schema(conn)
        _seed(conn)
        conn.commit()
    (path / "runtime.env").write_text(
        "WB_FBS_COLLECTOR_ENABLED=true\n", encoding="utf-8"
    )
    return runtime


def _runner(
    runtime: RegistryUploadDbBackedRuntime, deployed_sha: str
) -> FfPoolCutoverProductionMutation:
    return FfPoolCutoverProductionMutation(
        runtime_dir=runtime.runtime_dir,
        env_file=runtime.runtime_dir / "runtime.env",
        deployed_sha=deployed_sha,
        timestamp_factory=_Clock(),
    )


def _create_failed_stage_7c(
    runtime: RegistryUploadDbBackedRuntime, backup_dir: Path
) -> str:
    runner = _runner(runtime, OLD_SHA)
    gate = runner.build_gate_plan(excluded_shipment_ids=[SHIPMENT_ID])
    try:
        runner.apply(
            gate,
            fingerprint=gate["fingerprint"],
            approval_reference="owner-gate-old-failure",
            actor="supersession-smoke",
            backup_dir=backup_dir,
            external_barrier_evidence=_barrier(),
            crash="before_commit",
        )
    except RuntimeError as exc:
        assert "crash before commit" in str(exc)
    else:
        raise AssertionError("failed Stage 7C fixture did not fail before commit")
    with sqlite3.connect(runtime.db_path) as conn:
        row = conn.execute(
            "SELECT operation_id,lifecycle_state,next_action "
            "FROM sheet_vitrina_v1_recovery_operations "
            "WHERE operation_kind='warehouse_opening_publication' "
            "AND lifecycle_state='failed_recoverable'"
        ).fetchone()
    assert row is not None and row[2] == "exact_ff_pool_cutover_readback_or_retry"
    return str(row[0])


def _create_successful_stage_7c(
    runtime: RegistryUploadDbBackedRuntime, backup_dir: Path
) -> None:
    runner = _runner(runtime, SUCCESS_SHA)
    gate = runner.build_gate_plan(excluded_shipment_ids=[SHIPMENT_ID])
    result = runner.apply(
        gate,
        fingerprint=gate["fingerprint"],
        approval_reference="owner-gate-later-success",
        actor="supersession-smoke",
        backup_dir=backup_dir,
        external_barrier_evidence=_barrier(),
    )
    assert result["status"] == "applied_reconciled"
    assert result["readback"]["readback"]["status"] == "pass"


def _invariants(path: Path) -> dict[str, object]:
    with sqlite3.connect(path) as conn:
        shipment = conn.execute(
            "SELECT actual_ff_acceptance_date,order_status FROM "
            "sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?",
            (SHIPMENT_ID,),
        ).fetchone()
        pool_rows = conn.execute(
            "SELECT facility_id,pool,nm_id,quantity,capital_rub FROM "
            "sheet_vitrina_v1_ff_pool_balances ORDER BY facility_id,pool,nm_id"
        ).fetchall()
        business_operations = conn.execute(
            "SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_business_operations"
        ).fetchone()[0]
        cutover_events = conn.execute(
            "SELECT event_type,evidence_digest FROM "
            "sheet_vitrina_v1_ff_pool_cutover_recovery_events "
            "ORDER BY recovery_sequence"
        ).fetchall()
        artifacts = conn.execute(
            "SELECT operation_id,artifact_kind,size_bytes,digest,state FROM "
            "sheet_vitrina_v1_recovery_artifacts ORDER BY operation_id,artifact_kind"
        ).fetchall()
    return {
        "shipment": tuple(shipment or ()),
        "pool_rows": [tuple(row) for row in pool_rows],
        "business_operations": int(business_operations),
        "cutover_events": [tuple(row) for row in cutover_events],
        "artifacts": [tuple(row) for row in artifacts],
        "digest": hashlib.sha256(
            json.dumps(
                {
                    "shipment": tuple(shipment or ()),
                    "pool_rows": [tuple(row) for row in pool_rows],
                    "business_operations": int(business_operations),
                    "cutover_events": [tuple(row) for row in cutover_events],
                    "artifacts": [tuple(row) for row in artifacts],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }


def _assert_append_only(path: Path, operation_id: str) -> None:
    with sqlite3.connect(path) as conn:
        relation = conn.execute(
            "SELECT authorization_reference FROM "
            "sheet_vitrina_v1_recovery_supersessions WHERE target_operation_id=?",
            (operation_id,),
        ).fetchone()
        assert relation == ("owner-comment:smoke",)
        for statement in (
            "UPDATE sheet_vitrina_v1_recovery_supersessions SET actor='edited' "
            "WHERE target_operation_id=?",
            "DELETE FROM sheet_vitrina_v1_recovery_supersessions "
            "WHERE target_operation_id=?",
        ):
            try:
                conn.execute(statement, (operation_id,))
            except sqlite3.IntegrityError:
                conn.rollback()
            else:
                raise AssertionError("supersession relation was not append-only")


def _recovery_evidence(path: Path, operation_id: str) -> dict[str, object]:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT rollback_available,last_error FROM "
            "sheet_vitrina_v1_recovery_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        artifact_count = conn.execute(
            "SELECT COUNT(*) FROM sheet_vitrina_v1_recovery_artifacts "
            "WHERE operation_id=?",
            (operation_id,),
        ).fetchone()[0]
    assert row is not None
    return {
        "rollback_available": int(row[0]),
        "last_error": str(row[1]),
        "artifact_count": int(artifact_count),
    }


def _target_manifest_path(path: Path, operation_id: str) -> Path:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT path FROM sheet_vitrina_v1_recovery_artifacts "
            "WHERE operation_id=? AND artifact_kind='manifest'",
            (operation_id,),
        ).fetchone()
    assert row is not None
    return Path(str(row[0]))


def _assert_hosted_contract(root: Path) -> None:
    target = hosted_runtime.load_hosted_runtime_target(
        hosted_runtime.DEFAULT_TARGET_FILE
    )
    operation_id = "recovery_" + "6" * 32
    fingerprint = "sha256:" + "7" * 64
    dry_run = {
        "contract_name": "ff_pool_cutover_recovery_supersession_v1",
        "contract_version": 1,
        "deployed_sha": RUNNER_SHA,
        "mode": "dry_run_exact_supersession",
        "status": "ready",
        "apply_allowed": True,
        "would_change": True,
        "operation_id": operation_id,
        "fingerprint": fingerprint,
        "blockers": [],
    }
    with mock.patch.object(
        hosted_runtime.subprocess,
        "run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(dry_run), stderr=""
        ),
    ) as remote_run:
        result = hosted_runtime._run_remote_ff_pool_recovery_supersession(
            target,
            action="dry-run",
            deployed_sha=RUNNER_SHA,
            operation_id=operation_id,
            plan_path=None,
            fingerprint="",
            approval_reference="",
            actor="",
        )
    command = " ".join(remote_run.call_args.args[0])
    assert result == dry_run
    assert all(
        token in command
        for token in (
            "ff_pool_cutover_recovery_supersession.py",
            ".wb-core-runtime-sha",
            RUNNER_SHA,
            "--operation-id",
            operation_id,
        )
    )
    assert (
        remote_run.call_args.kwargs.get("timeout")
        == hosted_runtime.FF_POOL_RECOVERY_SUPERSESSION_TIMEOUT_SECONDS
    )
    parsed = hosted_runtime.build_arg_parser().parse_args(
        [
            "ff-pool-recovery-supersession-dry-run",
            "--deployed-sha",
            RUNNER_SHA,
            "--operation-id",
            operation_id,
            "--output",
            str(root / "hosted-dry-run.json"),
        ]
    )
    assert parsed.handler is hosted_runtime.run_ff_pool_recovery_supersession_command
    assert parsed.ff_pool_recovery_supersession_action == "dry-run"


if __name__ == "__main__":
    raise SystemExit(main())
