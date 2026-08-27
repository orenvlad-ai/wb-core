#!/usr/bin/env python3
"""Crash/retry smoke for durable detached storage sanitation jobs."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.storage_recovery_sanitation import apply_family  # noqa: E402
from apps.storage_recovery_sanitation_job import (  # noqa: E402
    JOB_DIRECTORY_NAME,
    SanitationJobError,
    job_status,
    run_worker,
    submit_job,
)


DEPLOYED_SHA = "a" * 40


def _seed_sqlite(path: Path, value: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE evidence(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO evidence VALUES('scope',?)", (value,))
        conn.commit()


def _starter(job_id: str) -> dict[str, str]:
    return {
        "name": f"wb-core-storage-recovery-sanitation@{job_id}.service",
        "start": "fixture",
    }


def run() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        runtime_dir = base / "state"
        backup_root = runtime_dir / "backups"
        root_backups = base / "root-backups"
        runtime_dir.mkdir()
        backup_root.mkdir()
        root_backups.mkdir()
        deployed_sha_file = base / ".wb-core-runtime-sha"
        deployed_sha_file.write_text(DEPLOYED_SHA, encoding="utf-8")

        family = backup_root / "supplier-cny-payment-10-recovery"
        family.mkdir()
        raw = family / "supplier-cny.sqlite3"
        _seed_sqlite(raw, "cny")

        plan_job_id = "1" * 64
        submitted = submit_job(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            deployed_sha_file=deployed_sha_file,
            job_id=plan_job_id,
            deployed_sha=DEPLOYED_SHA,
            operation="plan",
            root_name="backup",
            family=family.name,
            reserved_free_bytes=0,
            starter=_starter,
        )
        assert submitted["status"] == "queued"
        assert submitted["unit_start_requested"]
        assert not submitted["submit_idempotent"]
        planned = run_worker(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            deployed_sha_file=deployed_sha_file,
            job_id=plan_job_id,
        )
        assert planned["status"] == "succeeded" and planned["terminal"]
        assert planned["result"]["status"] == "dry_run_ready"
        fingerprint = planned["result"]["fingerprint"]
        status = job_status(
            runtime_dir=runtime_dir,
            job_id=plan_job_id,
            deployed_sha=DEPLOYED_SHA,
            include_systemd=False,
        )
        assert status["result_digest"] == status["result_record"]["result_digest"]

        repeated = submit_job(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            deployed_sha_file=deployed_sha_file,
            job_id=plan_job_id,
            deployed_sha=DEPLOYED_SHA,
            operation="plan",
            root_name="backup",
            family=family.name,
            reserved_free_bytes=0,
            starter=lambda _job_id: (_ for _ in ()).throw(
                AssertionError("terminal job must not be started again")
            ),
        )
        assert repeated["submit_idempotent"]
        assert not repeated["unit_start_requested"]
        busy_family = backup_root / "supplier-26gn527-vtb-recovery"
        busy_family.mkdir()
        busy_raw = busy_family / "supplier-vtb.sqlite3"
        _seed_sqlite(busy_raw, "vtb")
        try:
            submit_job(
                runtime_dir=runtime_dir,
                root_backups=root_backups,
                deployed_sha_file=deployed_sha_file,
                job_id=plan_job_id,
                deployed_sha=DEPLOYED_SHA,
                operation="plan",
                root_name="backup",
                family="supplier-26gn527-vtb-recovery",
                reserved_free_bytes=0,
                starter=_starter,
            )
        except SanitationJobError as exc:
            assert "different exact request" in str(exc)
        else:
            raise AssertionError("job id accepted exact-request drift")

        apply_job_id = "2" * 64
        submit_job(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            deployed_sha_file=deployed_sha_file,
            job_id=apply_job_id,
            deployed_sha=DEPLOYED_SHA,
            operation="apply",
            root_name="backup",
            family=family.name,
            fingerprint=fingerprint,
            reserved_free_bytes=0,
            starter=_starter,
        )
        # Emulate a worker crash after the audited sanitation apply but before
        # the detached job result/status was finalized.
        applied = apply_family(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            root_name="backup",
            family=family.name,
            fingerprint=fingerprint,
            deployed_sha=DEPLOYED_SHA,
            deployed_sha_file=deployed_sha_file,
            reserved_free_bytes=0,
        )
        assert applied["status"] == "applied"
        status_path = (
            runtime_dir
            / JOB_DIRECTORY_NAME
            / apply_job_id
            / "status.json"
        )
        interrupted = json.loads(status_path.read_text(encoding="utf-8"))
        interrupted.update(
            {
                "status": "running",
                "terminal": False,
                "attempt": 1,
                "updated_at": "2026-07-27T00:00:00.000000Z",
            }
        )
        status_path.write_text(
            json.dumps(interrupted, sort_keys=True),
            encoding="utf-8",
        )
        resumed = run_worker(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            deployed_sha_file=deployed_sha_file,
            job_id=apply_job_id,
        )
        assert resumed["status"] == "succeeded"
        assert resumed["attempt"] == 2
        assert resumed["result"]["idempotent"]
        assert not raw.exists()
        assert len(list(family.glob("*.zst.manifest.json"))) == 1

        failed_start_id = "3" * 64
        failed_start = submit_job(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            deployed_sha_file=deployed_sha_file,
            job_id=failed_start_id,
            deployed_sha=DEPLOYED_SHA,
            operation="plan",
            root_name="backup",
            family=family.name,
            reserved_free_bytes=0,
            starter=lambda _job_id: (_ for _ in ()).throw(
                RuntimeError("synthetic systemd failure")
            ),
        )
        assert failed_start["status"] == "start_failed"
        assert failed_start["retryable"] and not failed_start["terminal"]
        retried = submit_job(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            deployed_sha_file=deployed_sha_file,
            job_id=failed_start_id,
            deployed_sha=DEPLOYED_SHA,
            operation="plan",
            root_name="backup",
            family=family.name,
            reserved_free_bytes=0,
            starter=_starter,
        )
        assert retried["unit_start_requested"] and retried["submit_idempotent"]
        completed = run_worker(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            deployed_sha_file=deployed_sha_file,
            job_id=failed_start_id,
        )
        assert completed["result"]["status"] == "no_change"

        busy_job_id = "4" * 64
        submit_job(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            deployed_sha_file=deployed_sha_file,
            job_id=busy_job_id,
            deployed_sha=DEPLOYED_SHA,
            operation="plan",
            root_name="backup",
            family=busy_family.name,
            reserved_free_bytes=0,
            starter=_starter,
        )
        worker_lock = runtime_dir / JOB_DIRECTORY_NAME / "worker.lock"
        handle = worker_lock.open("a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            busy = run_worker(
                runtime_dir=runtime_dir,
                root_backups=root_backups,
                deployed_sha_file=deployed_sha_file,
                job_id=busy_job_id,
            )
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        assert busy["status"] == "failed"
        assert busy["error"]["code"] == "another_sanitation_job_active"
        assert busy_raw.exists()

        probe_job_id = "5" * 64
        probe_submitted = submit_job(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            deployed_sha_file=deployed_sha_file,
            job_id=probe_job_id,
            deployed_sha=DEPLOYED_SHA,
            operation="warm-archive-mount-probe",
            root_name="",
            family="",
            starter=_starter,
        )
        assert probe_submitted["request"]["operation"] == (
            "warm-archive-mount-probe"
        )
        for forbidden in (
            "root",
            "family",
            "fingerprint",
            "manifest",
            "manifest_sha256",
            "goal_operation_id",
            "approval_reference",
        ):
            assert forbidden not in probe_submitted["request"]
        probe_completed = run_worker(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            deployed_sha_file=deployed_sha_file,
            job_id=probe_job_id,
            executor=lambda request: {
                "schema": "wb-core.root-warm-archive-mount-probe/v1",
                "status": "observed",
                "query_only": True,
                "database_written": False,
                "archive_mutation_count": 0,
                "source_unlink_count": 0,
                "job_id": request["job_id"],
                "deployed_sha": request["deployed_sha"],
            },
        )
        assert probe_completed["status"] == "succeeded"
        assert probe_completed["result"]["query_only"] is True
        assert probe_completed["result"]["archive_mutation_count"] == 0
        try:
            submit_job(
                runtime_dir=runtime_dir,
                root_backups=root_backups,
                deployed_sha_file=deployed_sha_file,
                job_id="6" * 64,
                deployed_sha=DEPLOYED_SHA,
                operation="warm-archive-mount-probe",
                root_name="",
                family="",
                manifest="/tmp/not-allowed",
                starter=_starter,
            )
        except SanitationJobError as exc:
            assert "must not carry mutation inputs" in str(exc)
        else:
            raise AssertionError("mount probe accepted mutation inputs")

    print("storage_recovery_sanitation_job_smoke: ok")


if __name__ == "__main__":
    run()
