#!/usr/bin/env python3
"""Fault and safety smoke for exact pre-manifest candidate abort recovery."""

from __future__ import annotations

import json
import fcntl
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.finance_raw_storage import (
    bind_generation_identity,
    ensure_operational_schema,
    ensure_raw_schema,
)
from packages.application.finance_storage_candidate_abort import (
    FinanceStorageCandidateAbort,
    FinanceStorageCandidateAbortError,
    InjectedCandidateAbortFault,
)
from packages.application.finance_storage_migration import (
    PLAN_CONTRACT as CANDIDATE_PLAN_CONTRACT,
    _digest,
    _plan_fingerprint as candidate_plan_fingerprint,
)
from packages.application.finance_storage_recovery_contract import (
    validate_recovery_preflight,
)
from apps import finance_storage_split as finance_storage_cli
from apps import registry_upload_http_entrypoint_hosted_runtime as hosted


CURRENT_SHA = "a" * 40
SOURCE_SHA = "b" * 40
SOURCE_FINGERPRINT = "sha256:" + "c" * 64
GENERATION = SOURCE_FINGERPRINT.removeprefix("sha256:")[:20]


def _private_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _fixture(root: Path) -> tuple[Path, str]:
    runtime = root / "state"
    runtime.mkdir()
    (runtime / ".finance-storage-split.lock").touch(mode=0o600)
    monolith = runtime / "registry_upload_runtime.sqlite3"
    with sqlite3.connect(monolith) as connection:
        connection.execute(
            "CREATE TABLE business_state(id INTEGER PRIMARY KEY,value TEXT)"
        )
        connection.execute(
            "INSERT INTO business_state(id,value) VALUES(1,'canonical')"
        )
        connection.commit()
    snapshots = runtime / "finance-storage-split-snapshots"
    snapshots.mkdir()
    for snapshot_id in ("finance-split-" + "1" * 20, "finance-split-" + "2" * 20):
        (snapshots / snapshot_id).mkdir()
    generation_root = runtime / "generations" / GENERATION
    generation_root.mkdir(parents=True)
    raw_path = generation_root / "finance_raw.sqlite3"
    operational_path = generation_root / "operational.sqlite3"
    raw_generation_id = f"finance-raw-{GENERATION}"
    operational_generation_id = f"operational-{GENERATION}"
    with sqlite3.connect(raw_path) as raw:
        ensure_raw_schema(raw)
        bind_generation_identity(
            raw,
            logical_store="finance_raw",
            generation_id=raw_generation_id,
            generation_epoch=GENERATION,
            source_fingerprint=SOURCE_FINGERPRINT,
        )
        raw.commit()
    with sqlite3.connect(operational_path) as operational:
        ensure_operational_schema(operational)
        bind_generation_identity(
            operational,
            logical_store="operational",
            generation_id=operational_generation_id,
            generation_epoch=GENERATION,
            source_fingerprint=SOURCE_FINGERPRINT,
        )
        operational.commit()
    raw_chunk_digest = _digest(
        {
            "logical_digest": "sha256:" + "d" * 64,
            "raw_json_digest": "sha256:" + "e" * 64,
        }
    )
    plan: dict[str, object] = {
        "contract_version": CANDIDATE_PLAN_CONTRACT,
        "mode": "dry_run",
        "deployed_sha": SOURCE_SHA,
        "source": {"fingerprint": SOURCE_FINGERPRINT},
        "raw": {
            "row_count": 3,
            "logical_digest": "sha256:" + "d" * 64,
        },
        "chunks": {
            "manifest": [
                {
                    "chunk_id": "chunk-000001",
                    "row_count": 3,
                    "verification_digest": raw_chunk_digest,
                }
            ]
        },
        "table_owner_read_write_matrix": [
            {
                "table": "business_state",
                "row_count": 1,
                "logical_digest": "sha256:" + "f" * 64,
            }
        ],
        "target_generation": {
            "generation_epoch": GENERATION,
            "generation_directory": f"generations/{GENERATION}",
            "raw_generation_id": raw_generation_id,
            "operational_generation_id": operational_generation_id,
            "candidate_manifest": {
                "raw": {
                    "generation_id": raw_generation_id,
                    "generation_epoch": GENERATION,
                    "relative_path": (
                        f"generations/{GENERATION}/finance_raw.sqlite3"
                    ),
                },
                "operational": {
                    "generation_id": operational_generation_id,
                    "generation_epoch": GENERATION,
                    "relative_path": (
                        f"generations/{GENERATION}/operational.sqlite3"
                    ),
                },
            },
        },
        "performance": {
            "query_seconds": 12.5,
        },
        "capacity": {
            "available_bytes": 10_000,
            "shortfall_bytes": 0,
            "remaining_bytes_after_reservation": 5_000,
            "sufficient": True,
        },
        "writers_and_timers": {
            "database_openers": [
                {
                    "pid": 123,
                    "comm": "python3",
                    "access_mode": "read",
                }
            ],
            "systemd_units": [
                {
                    "unit": "wb-core-wb-finance-weekly.timer",
                    "return_code": 0,
                    "load_state": "loaded",
                    "unit_file_state": "enabled",
                    "active_state": "active",
                    "sub_state": "waiting",
                    "observed_at": "2026-07-29T00:00:00Z",
                }
            ],
        },
        "apply_allowed_by_machine_preflight": True,
    }
    plan["fingerprint"] = candidate_plan_fingerprint(plan)
    plan_fingerprint = str(plan["fingerprint"])
    _private_json(generation_root / "migration_plan.json", plan)
    batch_id = _digest(
        {
            "migration_id": GENERATION,
            "source_fingerprint": SOURCE_FINGERPRINT,
            "raw_digest": "sha256:" + "d" * 64,
        }
    )
    with sqlite3.connect(raw_path) as raw:
        raw.execute(
            """INSERT INTO finance_raw_ingest_batches(
               batch_id,source_identity,source_sha256,report_period,seller_id,
               week_start,week_end,row_count,rows_digest,status,created_at,
               committed_at
               ) VALUES(?,?,?,?,?,?,?,?,?,'committed',?,?)""",
            (
                batch_id,
                SOURCE_FINGERPRINT,
                SOURCE_FINGERPRINT,
                "2026-01-01/2026-01-07",
                "*",
                "2026-01-01",
                "2026-01-07",
                3,
                "sha256:" + "d" * 64,
                "2026-07-29T00:00:00Z",
                "2026-07-29T00:01:00Z",
            ),
        )
        raw.commit()
    with sqlite3.connect(operational_path) as operational:
        operational.execute(
            """INSERT INTO finance_storage_migration_chunks(
               migration_id,store_name,chunk_id,source_first_key,
               source_last_key,source_row_count,source_digest,
               destination_row_count,destination_digest,bytes_written,
               status,error,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,'verified',NULL,?)""",
            (
                GENERATION,
                "finance_raw",
                "chunk-000001",
                "1",
                "3",
                3,
                raw_chunk_digest,
                3,
                raw_chunk_digest,
                raw_path.stat().st_size,
                "2026-07-29T00:01:00Z",
            ),
        )
        operational.execute(
            """INSERT INTO finance_storage_migration_chunks(
               migration_id,store_name,chunk_id,source_first_key,
               source_last_key,source_row_count,source_digest,
               destination_row_count,destination_digest,bytes_written,
               status,error,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,'verified',NULL,?)""",
            (
                GENERATION,
                "operational",
                "table:business_state",
                "",
                "",
                1,
                "sha256:" + "f" * 64,
                1,
                "sha256:" + "f" * 64,
                operational_path.stat().st_size,
                "2026-07-29T00:01:00Z",
            ),
        )
        operational.commit()
    return runtime, plan_fingerprint


def _runner(
    runtime: Path,
    candidate_plan: str,
    *,
    fault_after_unlinks: int = 0,
) -> FinanceStorageCandidateAbort:
    return FinanceStorageCandidateAbort(
        runtime,
        deployed_sha=CURRENT_SHA,
        generation_epoch=GENERATION,
        candidate_plan_fingerprint=candidate_plan,
        fault_after_unlinks=fault_after_unlinks,
    )


def _expect_refusal(action, message: str) -> None:
    try:
        action()
    except FinanceStorageCandidateAbortError:
        return
    raise AssertionError(message)


def _hosted_transport_smoke() -> None:
    target = hosted.load_hosted_runtime_target(hosted.DEFAULT_TARGET_FILE)
    old_plan_fingerprint = "sha256:" + "9" * 64
    abort_fingerprint = "sha256:" + "8" * 64
    lease = {
        "contract_version": (
            "wb_core_finance_migration_deploy_lease_readback_v1"
        ),
        "policy": "finance_migration_global_deploy_hold_v1",
        "lease": {
            "task_id": "finance-candidate-abort-smoke",
            "lease_id": "finance-candidate-abort-smoke",
            "revision": 1,
            "window_id": "candidate-abort-smoke",
            "phase": "candidate-abort",
            "deployed_sha": CURRENT_SHA,
        },
        "fingerprint": "sha256:" + "7" * 64,
    }
    plan_payload = {
        "contract_version": (
            "wb_core_finance_storage_candidate_abort_plan_v1"
        ),
        "mode": "candidate_abort_dry_run",
        "deployed_sha": CURRENT_SHA,
        "generation_epoch": GENERATION,
        "candidate_plan_fingerprint": old_plan_fingerprint,
        "fingerprint": abort_fingerprint,
        "candidate_abort_allowed_by_machine_preflight": True,
    }
    terminal_payload = {
        "contract_version": (
            "wb_core_finance_storage_candidate_abort_result_v1"
        ),
        "status": "completed",
        "readback": {
            "candidate_root_absent": True,
            "global_manifest_absent": True,
            "non_target_unchanged": True,
        },
    }
    with tempfile.TemporaryDirectory(
        prefix="finance-candidate-abort-transport-"
    ) as temporary:
        plan_path = Path(temporary) / "abort-plan.json"
        _private_json(plan_path, plan_payload)
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(terminal_payload) + "\n",
            stderr="",
        )
        with mock.patch.object(
            hosted.subprocess,
            "run",
            return_value=completed,
        ) as remote:
            result = hosted._run_remote_finance_storage_split_action(
                target,
                action="candidate-abort-apply",
                plan_path=plan_path,
                fingerprint=abort_fingerprint,
                approval_reference="candidate-abort-smoke-approved",
                chunk_size=10_000,
                candidate_plan_fingerprint=old_plan_fingerprint,
                candidate_generation_epoch=GENERATION,
                deploy_lease=lease,
            )
        if result.get("status") != "completed":
            raise AssertionError(
                "hosted candidate abort transport lost terminal result"
            )
        call = remote.call_args
        command = " ".join(call.args[0])
        for token in (
            "candidate-abort-apply",
            "--candidate-abort-plan-file",
            "--candidate-generation-epoch",
            GENERATION,
            "--candidate-plan-fingerprint",
            old_plan_fingerprint,
            "--confirm-fingerprint",
            abort_fingerprint,
        ):
            if token not in command:
                raise AssertionError(
                    f"hosted candidate abort transport omitted {token}"
                )
        if call.kwargs.get("input") != plan_path.read_text(
            encoding="utf-8"
        ):
            raise AssertionError(
                "hosted candidate abort did not stream the reviewed plan once"
            )


def main() -> int:
    with tempfile.TemporaryDirectory(
        prefix="finance-candidate-abort-smoke-"
    ) as temporary:
        runtime, candidate_plan = _fixture(Path(temporary))
        generation_root = runtime / "generations" / GENERATION
        monolith = runtime / "registry_upload_runtime.sqlite3"
        monolith_stat = monolith.stat()
        snapshot_names = sorted(
            item.name
            for item in (
                runtime / "finance-storage-split-snapshots"
            ).iterdir()
        )
        planned = _runner(runtime, candidate_plan).build_plan()
        if (
            planned.get(
                "candidate_abort_allowed_by_machine_preflight"
            )
            is not True
            or int(
                (planned.get("exact_state") or {}).get(
                    "candidate_allocated_bytes"
                )
                or 0
            )
            <= 0
            or (
                (planned.get("exact_state") or {}).get("checkpoints")
                or {}
            ).get("raw_verified_rows")
            != 3
        ):
            raise AssertionError("candidate abort plan evidence is incomplete")
        plan_fingerprint = str(planned["fingerprint"])
        recovery = validate_recovery_preflight(
            runtime,
            action="candidate-abort-apply",
            phase="mutation",
            deployed_sha=CURRENT_SHA,
            approval_reference="smoke-approved",
            expected_fingerprint=plan_fingerprint,
            deploy_lease={
                "lease": {
                    "task_id": "finance-candidate-abort-smoke",
                    "lease_id": "finance-candidate-abort-smoke",
                    "revision": 1,
                    "window_id": "candidate-abort-smoke",
                    "phase": "candidate-abort",
                    "deployed_sha": CURRENT_SHA,
                }
            },
            runner_contracts=finance_storage_cli.RUNNER_CONTRACTS,
            restore_job_contract=(
                finance_storage_cli.RESTORE_JOB_CONTRACT
            ),
            restore_max_resume_sequence=(
                finance_storage_cli.RESTORE_MAX_RESUME_SEQUENCE
            ),
            downstream_capabilities={
                "maintenance_restore": True,
                "barrier_release": True,
                "durable_restore_submit_status": True,
                "durable_restore_inventory": True,
                "durable_restore_resume": True,
                "restore_systemd_template": True,
            },
            reviewed_plan=planned,
        )
        if (
            recovery.get("status") != "ready"
            or "candidate.abort"
            not in recovery.get("relevant_transitions", [])
        ):
            raise AssertionError(
                "candidate abort recovery preflight is incomplete"
            )
        try:
            _runner(
                runtime,
                candidate_plan,
                fault_after_unlinks=1,
            ).apply(
                reviewed_plan=planned,
                expected_fingerprint=plan_fingerprint,
                approval_reference="smoke-approved",
            )
        except InjectedCandidateAbortFault:
            pass
        else:
            raise AssertionError("candidate abort fault injection did not fire")
        transaction = json.loads(
            (
                runtime
                / ".finance-storage-candidate-aborts"
                / f"{GENERATION}.json"
            ).read_text(encoding="utf-8")
        )
        if (
            transaction.get("status") != "deleting"
            or len(transaction.get("deleted_files") or []) != 1
        ):
            raise AssertionError(
                "candidate abort did not persist crash-resume progress"
            )
        result = _runner(runtime, candidate_plan).apply(
            reviewed_plan=planned,
            expected_fingerprint=plan_fingerprint,
            approval_reference="smoke-approved",
        )
        if (
            result.get("status") != "completed"
            or generation_root.exists()
            or result.get("readback", {}).get(
                "global_manifest_absent"
            )
            is not True
        ):
            raise AssertionError("candidate abort resume did not terminalize")
        repeated = _runner(runtime, candidate_plan).apply(
            reviewed_plan=planned,
            expected_fingerprint=plan_fingerprint,
            approval_reference="smoke-approved",
        )
        if repeated.get("status") != "completed":
            raise AssertionError("candidate abort exact repeat is not a no-op")
        if (
            monolith.stat().st_ino != monolith_stat.st_ino
            or sorted(
                item.name
                for item in (
                    runtime / "finance-storage-split-snapshots"
                ).iterdir()
            )
            != snapshot_names
        ):
            raise AssertionError(
                "candidate abort changed monolith or snapshot non-targets"
            )
    with tempfile.TemporaryDirectory(
        prefix="finance-candidate-abort-unknown-"
    ) as temporary:
        runtime, candidate_plan = _fixture(Path(temporary))
        (
            runtime / "generations" / GENERATION / "unexpected.bin"
        ).write_bytes(b"blocked")
        _expect_refusal(
            lambda: _runner(runtime, candidate_plan).build_plan(),
            "unknown candidate file must fail closed",
        )
    with tempfile.TemporaryDirectory(
        prefix="finance-candidate-abort-pending-"
    ) as temporary:
        runtime, candidate_plan = _fixture(Path(temporary))
        runner = _runner(runtime, candidate_plan)
        planned = runner.build_plan()
        transaction = runner._transaction_binding(
            planned,
            approval_reference="pending-unlink-smoke",
        )
        transaction["pending_file"] = "finance_raw.sqlite3"
        _private_json(runner.transaction_path, transaction)
        (
            runtime
            / "generations"
            / GENERATION
            / "finance_raw.sqlite3"
        ).unlink()
        resumed = runner.apply(
            reviewed_plan=planned,
            expected_fingerprint=str(planned["fingerprint"]),
            approval_reference="pending-unlink-smoke",
        )
        if (
            resumed.get("status") != "completed"
            or (
                runtime / "generations" / GENERATION
            ).exists()
        ):
            raise AssertionError(
                "pending unlink crash boundary did not resume exactly"
            )
    with tempfile.TemporaryDirectory(
        prefix="finance-candidate-abort-lock-"
    ) as temporary:
        runtime, candidate_plan = _fixture(Path(temporary))
        descriptor = os.open(
            runtime / ".finance-storage-split.lock",
            os.O_RDWR,
        )
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            _expect_refusal(
                lambda: _runner(
                    runtime,
                    candidate_plan,
                ).build_plan(),
                "busy migration lock must block candidate abort planning",
            )
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
    with tempfile.TemporaryDirectory(
        prefix="finance-candidate-abort-manifest-"
    ) as temporary:
        runtime, candidate_plan = _fixture(Path(temporary))
        _private_json(
            runtime
            / "generations"
            / GENERATION
            / "candidate_generation_manifest.json",
            {"state": "shadow"},
        )
        _expect_refusal(
            lambda: _runner(runtime, candidate_plan).build_plan(),
            "candidate manifest must block pre-manifest abort",
        )
    _hosted_transport_smoke()
    print(
        "finance_storage_candidate_abort_smoke: ok -> exact partial "
        "candidate, durable pending/unlinked crash resume, idempotent "
        "terminal readback, lock/manifest/unknown-file fail-closed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
