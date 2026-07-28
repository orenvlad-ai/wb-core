#!/usr/bin/env python3
"""Crash/retry and identity smoke for the detached maintenance restore."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


from business_data_maintenance_restore_job import (
    JOB_DIRECTORY_NAME,
    MaintenanceRestoreJobError,
    RESTORE_DEADLINE_SECONDS,
    _classify_worker_observation,
    _fingerprint,
    job_status,
    resume_failed_job,
    run_worker,
    submit_job,
)


DEPLOYED_SHA = "a" * 40
RECOVERY_DEPLOYED_SHA = "e" * 40
JOB_ID = "1" * 64
SECOND_JOB_ID = "2" * 64
WINDOW_ID = "snapshot-fixture"
PLAN_FINGERPRINT = "sha256:" + "b" * 64
CONTROL_FINGERPRINT = "sha256:" + "c" * 64
CONTINUITY_FINGERPRINT = "sha256:" + "d" * 64
ACTOR = "fixture_replacement_task"
REASON = "restore exact prior intent after detached worker verification"
SERVICE_STARTED_AT = "Mon 2026-07-27 05:35:06 UTC"
SERVICE_CONTINUITY_PAYLOAD = {
    "barrier_window_id": WINDOW_ID,
    "barrier_plan_fingerprint": PLAN_FINGERPRINT,
    "hold_started_at": "2026-07-27T18:09:44Z",
    "services": [
        {
            "unit": "wb-core-sheet-vitrina-closure-retry.service",
            "main_pid": 1499161,
            "started_at": SERVICE_STARTED_AT,
            "baseline_active_state": "activating",
        }
    ],
}
SERVICE_CONTINUITY = {
    **SERVICE_CONTINUITY_PAYLOAD,
    "fingerprint": _fingerprint(SERVICE_CONTINUITY_PAYLOAD),
}


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def _seed_boundary(
    runtime_dir: Path,
    *,
    restored: bool = False,
    policy_revision: int = 19,
    service_completed: bool = False,
) -> None:
    _write_json(
        runtime_dir / ".business-data-write-barrier.json",
        {
            "schema_version": "business_data_write_barrier_v1",
            "active": True,
            "phase": "acquiring",
            "hold_confirmed": False,
            "window_id": WINDOW_ID,
            "plan_fingerprint": PLAN_FINGERPRINT,
        },
    )
    maintenance: dict[str, object] = {
        "schema_version": "business_data_maintenance_v1",
        "phase": "restored" if restored else "holding",
    }
    if restored:
        maintenance.update(
            {
                "exact_prior_state_restored": True,
                "restore_control_signature": {
                    "fingerprint": CONTROL_FINGERPRINT,
                },
                "pre_hold_service_continuity_readback": {
                    "fingerprint": CONTINUITY_FINGERPRINT,
                    "services": [
                        {
                            "unit": (
                                "wb-core-sheet-vitrina-closure-retry.service"
                            ),
                            "outcome": (
                                "completed"
                                if service_completed
                                else "continued"
                            ),
                            "main_pid": (
                                0 if service_completed else 1499161
                            ),
                            "started_at": (
                                "" if service_completed else SERVICE_STARTED_AT
                            ),
                        }
                    ],
                },
            }
        )
    _write_json(
        runtime_dir / ".business-data-maintenance.json",
        maintenance,
    )
    policy: dict[str, object] = {
        "schema_version": "auto_updates_owner_policy_v2",
        "revision": policy_revision,
        "master_desired": policy_revision == 20,
    }
    if policy_revision == 20:
        policy.update({"actor": ACTOR, "reason": REASON})
    _write_json(runtime_dir / ".auto-updates-policy.json", policy)


def _fixture() -> tuple[tempfile.TemporaryDirectory[str], dict[str, Path]]:
    temporary = tempfile.TemporaryDirectory()
    base = Path(temporary.name)
    runtime_dir = base / "state"
    app_dir = base / "app"
    runtime_dir.mkdir()
    app_dir.mkdir()
    apps_dir = app_dir / "apps"
    apps_dir.mkdir()
    continuity_payload = {
        "status": "ready",
        "service_continuity": SERVICE_CONTINUITY,
    }
    (apps_dir / "business_data_maintenance.py").write_text(
        "import json\n"
        "print(json.dumps(" + repr(continuity_payload) + "))\n",
        encoding="utf-8",
    )
    env_file = base / "wb.env"
    env_file.write_text("FIXTURE=1\n", encoding="utf-8")
    deployed_sha_file = app_dir / ".wb-core-runtime-sha"
    deployed_sha_file.write_text(DEPLOYED_SHA + "\n", encoding="utf-8")
    _seed_boundary(runtime_dir)
    return temporary, {
        "runtime_dir": runtime_dir,
        "app_dir": app_dir,
        "env_file": env_file,
        "deployed_sha_file": deployed_sha_file,
    }


def _paths_from_base(base: Path) -> dict[str, Path]:
    app_dir = base / "app"
    return {
        "runtime_dir": base / "state",
        "app_dir": app_dir,
        "env_file": base / "wb.env",
        "deployed_sha_file": app_dir / ".wb-core-runtime-sha",
    }


def _submit(
    paths: dict[str, Path],
    *,
    job_id: str = JOB_ID,
    starter=None,
    reason: str = REASON,
) -> dict[str, object]:
    return submit_job(
        **paths,
        job_id=job_id,
        deployed_sha=DEPLOYED_SHA,
        expected_revision=19,
        window_id=WINDOW_ID,
        plan_fingerprint=PLAN_FINGERPRINT,
        service_continuity_fingerprint=str(
            SERVICE_CONTINUITY["fingerprint"]
        ),
        actor=ACTOR,
        reason=reason,
        allow_pre_hold_service_continuity=True,
        continuity_reader=lambda: dict(SERVICE_CONTINUITY),
        starter=starter
        or (
            lambda exact_job_id: {
                "name": (
                    "wb-core-business-data-maintenance-restore@"
                    f"{exact_job_id}.service"
                ),
                "start": "fixture",
            }
        ),
    )


def _successful_executor(
    paths: dict[str, Path],
    calls: list[int],
):
    def execute(
        _request: dict[str, object],
        effective_revision: int,
    ) -> dict[str, object]:
        calls.append(effective_revision)
        _seed_boundary(
            paths["runtime_dir"],
            restored=True,
            policy_revision=20,
            service_completed=True,
        )
        return {
            "status": "restored",
            "exact_prior_state_restored": True,
            "control_signature": CONTROL_FINGERPRINT,
        }

    return execute


def _assert_success_and_terminal_idempotency() -> None:
    temporary, paths = _fixture()
    try:
        submitted = _submit(paths)
        assert submitted["status"] == "queued"
        assert submitted["unit_start_requested"]
        calls: list[int] = []
        completed = run_worker(
            **paths,
            job_id=JOB_ID,
            executor=_successful_executor(paths, calls),
        )
        assert calls == [19]
        assert completed["status"] == "succeeded"
        assert completed["terminal"]
        assert completed["result"]["readback"]["policy_revision"] == 20
        assert completed["result"]["readback"]["barrier_active"] is True
        readback = job_status(
            runtime_dir=paths["runtime_dir"],
            job_id=JOB_ID,
            deployed_sha=DEPLOYED_SHA,
            include_systemd=False,
        )
        assert readback["result_digest"] == (
            readback["result_record"]["result_digest"]
        )
        repeated = _submit(
            paths,
            starter=lambda _job_id: (_ for _ in ()).throw(
                AssertionError("terminal job must not start again")
            ),
        )
        assert repeated["submit_idempotent"]
        assert not repeated["unit_start_requested"]
        try:
            _submit(paths, reason="different exact request")
        except MaintenanceRestoreJobError as exc:
            assert "different exact restore" in str(exc)
        else:
            raise AssertionError("job id accepted restore-request drift")
    finally:
        temporary.cleanup()


def _assert_continuity_subprocess_and_fingerprint_binding() -> None:
    temporary, paths = _fixture()
    try:
        submitted = submit_job(
            **paths,
            job_id=JOB_ID,
            deployed_sha=DEPLOYED_SHA,
            expected_revision=19,
            window_id=WINDOW_ID,
            plan_fingerprint=PLAN_FINGERPRINT,
            service_continuity_fingerprint=str(
                SERVICE_CONTINUITY["fingerprint"]
            ),
            actor=ACTOR,
            reason=REASON,
            allow_pre_hold_service_continuity=True,
            starter=lambda exact_job_id: {
                "name": exact_job_id,
                "start": "fixture",
            },
        )
        assert submitted["request"]["service_continuity"] == (
            SERVICE_CONTINUITY
        )
    finally:
        temporary.cleanup()

    temporary, paths = _fixture()
    try:
        try:
            submit_job(
                **paths,
                job_id=JOB_ID,
                deployed_sha=DEPLOYED_SHA,
                expected_revision=19,
                window_id=WINDOW_ID,
                plan_fingerprint=PLAN_FINGERPRINT,
                service_continuity_fingerprint="sha256:" + "0" * 64,
                actor=ACTOR,
                reason=REASON,
                allow_pre_hold_service_continuity=True,
                starter=lambda exact_job_id: {
                    "name": exact_job_id,
                    "start": "fixture",
                },
            )
        except MaintenanceRestoreJobError as exc:
            assert "continuity fingerprint/boundary mismatch" in str(exc)
        else:
            raise AssertionError("continuity fingerprint drift was accepted")
        assert not (
            paths["runtime_dir"] / JOB_DIRECTORY_NAME / JOB_ID
        ).exists()
    finally:
        temporary.cleanup()


def _assert_single_non_terminal_job() -> None:
    temporary, paths = _fixture()
    try:
        _submit(paths)
        try:
            _submit(paths, job_id=SECOND_JOB_ID)
        except MaintenanceRestoreJobError as exc:
            assert "another restore job is non-terminal" in str(exc)
        else:
            raise AssertionError("parallel restore request was accepted")
    finally:
        temporary.cleanup()


def _assert_start_retry() -> None:
    temporary, paths = _fixture()
    try:
        failed = _submit(
            paths,
            starter=lambda _job_id: (_ for _ in ()).throw(
                RuntimeError("synthetic systemd failure")
            ),
        )
        assert failed["status"] == "start_failed"
        assert failed["retryable"] and not failed["terminal"]
        retried = _submit(paths)
        assert retried["submit_idempotent"]
        assert retried["unit_start_requested"]
    finally:
        temporary.cleanup()


def _assert_active_foreground_restore_lock_blocks_start() -> None:
    temporary, paths = _fixture()
    try:
        lock_path = (
            paths["runtime_dir"]
            / ".business-data-maintenance-restore.lock"
        )
        handle = lock_path.open("a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            blocked = _submit(
                paths,
                starter=lambda _job_id: (_ for _ in ()).throw(
                    AssertionError(
                        "active foreground restore lock must block unit start"
                    )
                ),
            )
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        assert blocked["status"] == "start_failed"
        assert (
            blocked["error"]["code"]
            == "restore_start_preflight_failed"
        )
        retried = _submit(paths)
        assert retried["unit_start_requested"]
    finally:
        temporary.cleanup()


def _assert_global_worker_lock() -> None:
    temporary, paths = _fixture()
    try:
        _submit(paths)
        lock_path = (
            paths["runtime_dir"] / JOB_DIRECTORY_NAME / "worker.lock"
        )
        handle = lock_path.open("a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = run_worker(
                **paths,
                job_id=JOB_ID,
                executor=lambda _request, _revision: (_ for _ in ()).throw(
                    AssertionError("busy worker must not execute restore")
                ),
            )
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        assert result["status"] == "queued"
        assert not result["terminal"]
        assert result["worker_lock_busy"]
    finally:
        temporary.cleanup()


def _assert_running_resubmit_does_not_restart() -> None:
    temporary, paths = _fixture()
    try:
        _submit(paths)
        status_path = (
            paths["runtime_dir"]
            / JOB_DIRECTORY_NAME
            / JOB_ID
            / "status.json"
        )
        status = json.loads(status_path.read_text(encoding="utf-8"))
        started = datetime.now(timezone.utc)
        status.update(
            {
                "status": "running",
                "attempt": 1,
                "worker_pid": 12345,
                "started_at": started.isoformat(),
                "heartbeat_at": started.isoformat(),
                "deadline_at": (
                    started + timedelta(seconds=RESTORE_DEADLINE_SECONDS)
                ).isoformat(),
                "updated_at": started.isoformat(),
            }
        )
        _write_json(status_path, status)
        repeated = _submit(
            paths,
            starter=lambda _job_id: (_ for _ in ()).throw(
                AssertionError("running exact job must not be started again")
            ),
        )
        assert repeated["status"] == "running"
        assert repeated["submit_idempotent"]
        assert not repeated["unit_start_requested"]
        assert (
            repeated["worker_observation"]["classification"]
            == "active_worker"
        )
    finally:
        temporary.cleanup()


def _assert_crash_resume_after_durable_restore() -> None:
    temporary, paths = _fixture()
    try:
        _submit(paths)
        _seed_boundary(
            paths["runtime_dir"],
            restored=True,
            policy_revision=20,
            service_completed=True,
        )
        resumed = run_worker(
            **paths,
            job_id=JOB_ID,
            executor=lambda _request, _revision: (_ for _ in ()).throw(
                AssertionError(
                    "durably restored state must not execute restore again"
                )
            ),
        )
        assert resumed["status"] == "succeeded"
        assert resumed["result"]["idempotent_recovery"] is True
        assert resumed["result"]["effective_revision"] == 20
        assert resumed["result"]["readback"][
            "pre_hold_service_continuity_readback"
        ]["services"][0]["outcome"] == "completed"
        try:
            _submit(paths, job_id=SECOND_JOB_ID)
        except MaintenanceRestoreJobError as exc:
            assert "revision/state drifted" in str(exc)
        else:
            raise AssertionError(
                "fresh job was accepted after exact restore completion"
            )
    finally:
        temporary.cleanup()


def _assert_boundary_drift_rejected() -> None:
    temporary, paths = _fixture()
    try:
        barrier_path = (
            paths["runtime_dir"] / ".business-data-write-barrier.json"
        )
        barrier = json.loads(barrier_path.read_text(encoding="utf-8"))
        barrier["hold_confirmed"] = True
        _write_json(barrier_path, barrier)
        try:
            _submit(paths)
        except MaintenanceRestoreJobError as exc:
            assert "barrier identity is unavailable" in str(exc)
        else:
            raise AssertionError("confirmed barrier accepted restore submit")
        assert not (
            paths["runtime_dir"] / JOB_DIRECTORY_NAME / JOB_ID
        ).exists()
    finally:
        temporary.cleanup()


def _assert_deployed_sha_drift_is_durable_failure() -> None:
    temporary, paths = _fixture()
    try:
        _submit(paths)
        paths["deployed_sha_file"].write_text("f" * 40 + "\n", encoding="utf-8")
        failed = run_worker(
            **paths,
            job_id=JOB_ID,
            executor=lambda _request, _revision: (_ for _ in ()).throw(
                AssertionError("SHA-drifted worker must not execute restore")
            ),
        )
        assert failed["status"] == "failed"
        assert failed["terminal"]
        assert failed["error"]["code"] == "maintenance_restore_failed"
        readback = job_status(
            runtime_dir=paths["runtime_dir"],
            job_id=JOB_ID,
            deployed_sha=DEPLOYED_SHA,
            include_systemd=False,
        )
        assert readback["result_record"]["error"] == failed["error"]
    finally:
        temporary.cleanup()


def _assert_exact_failed_job_resumes_once_after_recovery_deploy() -> None:
    temporary, paths = _fixture()
    try:
        _submit(paths)

        def fail_once(
            _request: dict[str, object],
            _effective_revision: int,
        ) -> dict[str, object]:
            raise RuntimeError("post-resume desired/actual drift: autoanswers")

        failed = run_worker(
            **paths,
            job_id=JOB_ID,
            executor=fail_once,
        )
        assert failed["status"] == "failed"
        assert failed["attempt"] == 1
        failure_digest = str(failed["result_digest"])
        paths["deployed_sha_file"].write_text(
            RECOVERY_DEPLOYED_SHA + "\n",
            encoding="utf-8",
        )
        resumed = resume_failed_job(
            **paths,
            job_id=JOB_ID,
            deployed_sha=RECOVERY_DEPLOYED_SHA,
            expected_failure_digest=failure_digest,
            service_continuity_fingerprint=str(
                SERVICE_CONTINUITY["fingerprint"]
            ),
            actor=ACTOR,
            reason="reviewed same-job recovery deploy",
            continuity_reader=lambda: dict(SERVICE_CONTINUITY),
            starter=lambda exact_job_id: {
                "name": (
                    "wb-core-business-data-maintenance-restore@"
                    f"{exact_job_id}.service"
                ),
                "start": "fixture-resume",
            },
        )
        assert resumed["status"] == "queued"
        assert resumed["attempt"] == 1
        assert resumed["deployment_binding"]["resume_sequence"] == 1
        job_dir = (
            paths["runtime_dir"]
            / JOB_DIRECTORY_NAME
            / JOB_ID
        )
        assert (job_dir / "attempt-1-result.json").is_file()
        try:
            job_status(
                runtime_dir=paths["runtime_dir"],
                job_id=JOB_ID,
                deployed_sha=DEPLOYED_SHA,
                include_systemd=False,
            )
        except MaintenanceRestoreJobError as exc:
            assert "deployed SHA" in str(exc)
        else:
            raise AssertionError("old deployed SHA read resumed job")
        calls: list[int] = []
        succeeded = run_worker(
            **paths,
            job_id=JOB_ID,
            executor=_successful_executor(paths, calls),
        )
        assert calls == [19]
        assert succeeded["status"] == "succeeded"
        assert succeeded["attempt"] == 2
        readback = job_status(
            runtime_dir=paths["runtime_dir"],
            job_id=JOB_ID,
            deployed_sha=RECOVERY_DEPLOYED_SHA,
            include_systemd=False,
        )
        assert readback["deployment_binding"]["resume_sequence"] == 1
        assert readback["audit"]["events"] == [
            "queued",
            "worker_started",
            "failed",
            "resume_queued",
            "worker_started",
            "succeeded",
        ]
        repeated = resume_failed_job(
            **paths,
            job_id=JOB_ID,
            deployed_sha=RECOVERY_DEPLOYED_SHA,
            expected_failure_digest=failure_digest,
            service_continuity_fingerprint=str(
                SERVICE_CONTINUITY["fingerprint"]
            ),
            actor=ACTOR,
            reason="reviewed same-job recovery deploy",
            continuity_reader=lambda: dict(SERVICE_CONTINUITY),
            starter=lambda _job_id: (_ for _ in ()).throw(
                AssertionError("terminal resumed job must not start again")
            ),
        )
        assert repeated["status"] == "succeeded"
        try:
            resume_failed_job(
                **paths,
                job_id=JOB_ID,
                deployed_sha="f" * 40,
                expected_failure_digest=failure_digest,
                service_continuity_fingerprint=str(
                    SERVICE_CONTINUITY["fingerprint"]
                ),
                actor=ACTOR,
                reason="different recovery binding",
                continuity_reader=lambda: dict(SERVICE_CONTINUITY),
            )
        except MaintenanceRestoreJobError as exc:
            assert (
                "deployed SHA" in str(exc)
                or "different evidence" in str(exc)
            )
        else:
            raise AssertionError("second same-job resume binding was accepted")
    finally:
        temporary.cleanup()


def _assert_same_job_resume_rejects_continuity_drift_before_binding() -> None:
    temporary, paths = _fixture()
    try:
        _submit(paths)

        def fail_once(
            _request: dict[str, object],
            _effective_revision: int,
        ) -> dict[str, object]:
            raise RuntimeError("post-resume desired/actual drift: autoanswers")

        failed = run_worker(
            **paths,
            job_id=JOB_ID,
            executor=fail_once,
        )
        paths["deployed_sha_file"].write_text(
            RECOVERY_DEPLOYED_SHA + "\n",
            encoding="utf-8",
        )
        drifted_payload = {
            **SERVICE_CONTINUITY_PAYLOAD,
            "services": [
                *SERVICE_CONTINUITY_PAYLOAD["services"],
                {
                    "unit": "wb-core-unexpected-writer.service",
                    "main_pid": 4242,
                    "started_at": "Tue 2026-07-28 08:00:00 UTC",
                    "baseline_active_state": "active",
                },
            ],
        }
        drifted_continuity = {
            **drifted_payload,
            "fingerprint": _fingerprint(drifted_payload),
        }
        try:
            resume_failed_job(
                **paths,
                job_id=JOB_ID,
                deployed_sha=RECOVERY_DEPLOYED_SHA,
                expected_failure_digest=str(failed["result_digest"]),
                service_continuity_fingerprint=str(
                    SERVICE_CONTINUITY["fingerprint"]
                ),
                actor=ACTOR,
                reason="reviewed same-job recovery deploy",
                continuity_reader=lambda: drifted_continuity,
                starter=lambda _job_id: (_ for _ in ()).throw(
                    AssertionError("continuity drift must not start a worker")
                ),
            )
        except MaintenanceRestoreJobError as exc:
            assert "continuity fingerprint" in str(exc)
        else:
            raise AssertionError("same-job resume accepted continuity drift")
        job_dir = (
            paths["runtime_dir"]
            / JOB_DIRECTORY_NAME
            / JOB_ID
        )
        assert not (job_dir / "resume.json").exists()
        status = job_status(
            runtime_dir=paths["runtime_dir"],
            job_id=JOB_ID,
            deployed_sha=DEPLOYED_SHA,
            include_systemd=False,
        )
        assert status["status"] == "failed"
        assert status["attempt"] == 1
        assert status["audit"]["last_event"] == "failed"
    finally:
        temporary.cleanup()


def _assert_attempt_two_revalidates_continuity_inside_worker() -> None:
    temporary, paths = _fixture()
    try:
        _submit(paths)

        def fail_once(
            _request: dict[str, object],
            _effective_revision: int,
        ) -> dict[str, object]:
            raise RuntimeError("post-resume desired/actual drift: autoanswers")

        failed = run_worker(
            **paths,
            job_id=JOB_ID,
            executor=fail_once,
        )
        paths["deployed_sha_file"].write_text(
            RECOVERY_DEPLOYED_SHA + "\n",
            encoding="utf-8",
        )
        resume_failed_job(
            **paths,
            job_id=JOB_ID,
            deployed_sha=RECOVERY_DEPLOYED_SHA,
            expected_failure_digest=str(failed["result_digest"]),
            service_continuity_fingerprint=str(
                SERVICE_CONTINUITY["fingerprint"]
            ),
            actor=ACTOR,
            reason="reviewed same-job recovery deploy",
            continuity_reader=lambda: dict(SERVICE_CONTINUITY),
            starter=lambda exact_job_id: {
                "name": (
                    "wb-core-business-data-maintenance-restore@"
                    f"{exact_job_id}.service"
                ),
                "start": "fixture-resume",
            },
        )
        drifted_payload = {
            **SERVICE_CONTINUITY_PAYLOAD,
            "services": [],
        }
        drifted_continuity = {
            **drifted_payload,
            "fingerprint": _fingerprint(drifted_payload),
        }
        continuity_payload = {
            "status": "ready",
            "service_continuity": drifted_continuity,
        }
        (
            paths["app_dir"]
            / "apps"
            / "business_data_maintenance.py"
        ).write_text(
            "import json\n"
            "print(json.dumps(" + repr(continuity_payload) + "))\n",
            encoding="utf-8",
        )
        called = False

        def must_not_execute(
            _request: dict[str, object],
            _effective_revision: int,
        ) -> dict[str, object]:
            nonlocal called
            called = True
            raise AssertionError("attempt 2 executed after continuity drift")

        blocked = run_worker(
            **paths,
            job_id=JOB_ID,
            executor=must_not_execute,
        )
        assert not called
        assert blocked["status"] == "failed"
        assert blocked["attempt"] == 2
        assert blocked["terminal"] is True
        assert "continuity fingerprint" in blocked["error"]["message"]
    finally:
        temporary.cleanup()


def _assert_worker_observation_classification() -> None:
    now = datetime(2026, 7, 28, 7, 0, tzinfo=timezone.utc)
    base = {
        "status": "running",
        "terminal": False,
        "heartbeat_at": (now - timedelta(seconds=45)).isoformat(),
        "deadline_at": (now + timedelta(minutes=5)).isoformat(),
    }
    ambiguous_active = _classify_worker_observation(
        base,
        unit={"properties": {"ActiveState": "activating", "SubState": "start"}},
        now=now,
    )
    assert (
        ambiguous_active["classification"]
        == "ambiguous_active_unit_stale_heartbeat"
    )
    lost = _classify_worker_observation(
        base,
        unit={"properties": {"ActiveState": "failed", "SubState": "failed"}},
        now=now,
    )
    assert lost["classification"] == "lost_worker"
    ambiguous = _classify_worker_observation(base, unit=None, now=now)
    assert ambiguous["classification"] == "ambiguous_worker"
    stale = _classify_worker_observation(
        {
            **base,
            "heartbeat_at": (now - timedelta(seconds=1)).isoformat(),
            "deadline_at": (now - timedelta(seconds=1)).isoformat(),
        },
        unit={"properties": {"ActiveState": "active", "SubState": "running"}},
        now=now,
    )
    assert stale["classification"] == "stale_deadline_exceeded"
    for observation in (ambiguous_active, lost, ambiguous, stale):
        assert observation["fail_closed"]
        assert not observation["second_restore_auto_start_allowed"]
        assert not observation["barrier_abort_candidate"]


def _fault_worker(base: Path) -> None:
    paths = _paths_from_base(base)

    def executor(
        _request: dict[str, object],
        _effective_revision: int,
    ) -> dict[str, object]:
        time.sleep(3.0)
        _seed_boundary(
            paths["runtime_dir"],
            restored=True,
            policy_revision=20,
        )
        return {
            "status": "restored",
            "exact_prior_state_restored": True,
            "control_signature": CONTROL_FINGERPRINT,
        }

    completed = run_worker(
        **paths,
        job_id=JOB_ID,
        executor=executor,
    )
    if completed["status"] != "succeeded":
        raise RuntimeError("fault worker did not persist terminal success")


def _fault_submitting_client(base: Path) -> None:
    paths = _paths_from_base(base)

    def starter(exact_job_id: str) -> dict[str, object]:
        worker = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "_fault_worker",
                str(base),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        (base / "fault-worker.pid").write_text(
            str(worker.pid),
            encoding="utf-8",
        )
        return {
            "name": (
                "wb-core-business-data-maintenance-restore@"
                f"{exact_job_id}.service"
            ),
            "start": "fixture-detached",
        }

    submitted = _submit(paths, starter=starter)
    if not submitted["unit_start_requested"]:
        raise RuntimeError("fault submitter did not start detached worker")
    (base / "fault-submitter.ready").write_text("ready\n", encoding="utf-8")
    time.sleep(120.0)


def _assert_submitter_disconnect_does_not_own_worker() -> None:
    temporary, paths = _fixture()
    base = paths["runtime_dir"].parent
    submitter = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "_fault_submitter",
            str(base),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    worker_pid = 0
    try:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if (base / "fault-submitter.ready").is_file():
                break
            if submitter.poll() is not None:
                raise AssertionError("fault submitter exited before disconnect")
            time.sleep(0.05)
        else:
            raise AssertionError("fault submitter did not become disconnectable")

        worker_pid = int(
            (base / "fault-worker.pid").read_text(encoding="utf-8")
        )
        while time.monotonic() < deadline:
            current = job_status(
                runtime_dir=paths["runtime_dir"],
                job_id=JOB_ID,
                deployed_sha=DEPLOYED_SHA,
                include_systemd=False,
            )
            if current["status"] == "running":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("detached worker did not enter running state")

        submitter.kill()
        submitter.wait(timeout=5.0)
        os.kill(worker_pid, 0)

        terminal_deadline = time.monotonic() + 15.0
        while time.monotonic() < terminal_deadline:
            completed = job_status(
                runtime_dir=paths["runtime_dir"],
                job_id=JOB_ID,
                deployed_sha=DEPLOYED_SHA,
                include_systemd=False,
            )
            if completed["status"] == "succeeded":
                break
            time.sleep(0.05)
        else:
            raise AssertionError(
                "detached worker did not survive submitting-client death"
            )
        assert completed["result_record"]["result_digest"]
        audit_path = (
            paths["runtime_dir"]
            / JOB_DIRECTORY_NAME
            / JOB_ID
            / "audit.jsonl"
        )
        events = [
            json.loads(line)["event"]
            for line in audit_path.read_text(encoding="utf-8").splitlines()
        ]
        assert events == ["queued", "worker_started", "succeeded"]
        assert completed["audit"]["events"] == events
        assert completed["audit"]["sha256"].startswith("sha256:")
        assert (
            completed["worker_observation"]["classification"]
            == "terminal_succeeded"
        )
        assert completed["worker_observation"]["barrier_abort_candidate"]
    finally:
        if submitter.poll() is None:
            submitter.kill()
            submitter.wait(timeout=5.0)
        if worker_pid:
            try:
                os.kill(worker_pid, 0)
            except ProcessLookupError:
                pass
        temporary.cleanup()


def run() -> None:
    _assert_success_and_terminal_idempotency()
    _assert_continuity_subprocess_and_fingerprint_binding()
    _assert_single_non_terminal_job()
    _assert_start_retry()
    _assert_active_foreground_restore_lock_blocks_start()
    _assert_global_worker_lock()
    _assert_running_resubmit_does_not_restart()
    _assert_crash_resume_after_durable_restore()
    _assert_boundary_drift_rejected()
    _assert_deployed_sha_drift_is_durable_failure()
    _assert_exact_failed_job_resumes_once_after_recovery_deploy()
    _assert_same_job_resume_rejects_continuity_drift_before_binding()
    _assert_attempt_two_revalidates_continuity_inside_worker()
    _assert_worker_observation_classification()
    _assert_submitter_disconnect_does_not_own_worker()
    print("business_data_maintenance_restore_job_smoke: ok")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "_fault_worker":
        _fault_worker(Path(sys.argv[2]))
    elif len(sys.argv) == 3 and sys.argv[1] == "_fault_submitter":
        _fault_submitting_client(Path(sys.argv[2]))
    else:
        run()
