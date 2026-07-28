#!/usr/bin/env python3
"""Durable detached submit/status worker for one exact maintenance restore."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable, Mapping


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_NAME = "business_data_maintenance_restore_job_v1"
JOB_DIRECTORY_NAME = "business-data-maintenance-restore-jobs"
SYSTEMD_UNIT_TEMPLATE = "wb-core-business-data-maintenance-restore@.service"
RESUME_BINDING_FILENAME = "resume.json"
MAX_RESUME_SEQUENCE = 3
JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DEPLOYED_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
WINDOW_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TERMINAL_STATUSES = frozenset({"succeeded", "failed"})
VALID_STATUSES = frozenset(
    {"queued", "start_failed", "running", "succeeded", "failed"}
)
RESTORE_DEADLINE_SECONDS = 10_500
HEARTBEAT_INTERVAL_SECONDS = 5.0
STALE_HEARTBEAT_SECONDS = 30.0
QUIET_CONFIRMED_HOLD_CONTINUITY_KIND = "quiet_confirmed_hold"


class MaintenanceRestoreJobError(RuntimeError):
    """Fail-closed detached restore contract violation."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-dir",
        default="/opt/wb-core-runtime/state",
        help="Canonical runtime state directory.",
    )
    parser.add_argument(
        "--app-dir",
        default="/opt/wb-core-runtime/app",
        help="Canonical deployed application directory.",
    )
    parser.add_argument(
        "--env-file",
        default="/opt/wb-ai/.env",
        help="Canonical hosted environment file.",
    )
    parser.add_argument(
        "--deployed-sha-file",
        default="/opt/wb-core-runtime/app/.wb-core-runtime-sha",
        help="Canonical deployed SHA marker.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    submit = subparsers.add_parser(
        "submit",
        help="Persist one exact restore request and start its fixed detached unit.",
    )
    _add_job_identity_arguments(submit)
    submit.add_argument("--expected-revision", type=int, required=True)
    submit.add_argument("--window-id", required=True)
    submit.add_argument("--plan-fingerprint", required=True)
    submit.add_argument("--service-continuity-fingerprint", required=True)
    submit.add_argument("--actor", required=True)
    submit.add_argument("--reason", required=True)
    submit.add_argument(
        "--allow-pre-hold-service-continuity",
        action="store_true",
        help="Allow only the exact audited pre-hold systemd generation.",
    )

    status = subparsers.add_parser(
        "status",
        help="Read one durable restore request/status/result without mutation.",
    )
    _add_job_identity_arguments(status)

    resume = subparsers.add_parser(
        "resume",
        help=(
            "Explicitly resume the same failed job through the bounded "
            "append-only recovery sequence after a reviewed recovery deploy "
            "and exact fail-closed boundary readback."
        ),
    )
    _add_job_identity_arguments(resume)
    resume.add_argument("--expected-failure-digest", required=True)
    resume.add_argument("--service-continuity-fingerprint", required=True)
    resume.add_argument("--actor", required=True)
    resume.add_argument("--reason", required=True)

    worker = subparsers.add_parser(
        "worker",
        help="Run one persisted exact restore request inside the fixed systemd unit.",
    )
    worker.add_argument("--job-id", required=True)
    return parser


def _add_job_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--deployed-sha", required=True)


def submit_job(
    *,
    runtime_dir: Path,
    app_dir: Path,
    env_file: Path,
    deployed_sha_file: Path,
    job_id: str,
    deployed_sha: str,
    expected_revision: int,
    window_id: str,
    plan_fingerprint: str,
    service_continuity_fingerprint: str,
    actor: str,
    reason: str,
    allow_pre_hold_service_continuity: bool,
    starter: Callable[[str], dict[str, Any]] | None = None,
    continuity_reader: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Persist and start one exact request idempotently."""

    job_id = _require_job_id(job_id)
    deployed_sha = _require_deployed_sha(deployed_sha)
    expected_revision = _require_revision(expected_revision)
    window_id = _require_window_id(window_id)
    plan_fingerprint = _require_fingerprint(plan_fingerprint)
    service_continuity_fingerprint = _require_fingerprint(
        service_continuity_fingerprint
    )
    actor = _require_audit_text(actor, label="actor", maximum=160)
    reason = _require_audit_text(reason, label="reason", maximum=500)
    if not allow_pre_hold_service_continuity:
        raise MaintenanceRestoreJobError(
            "detached restore requires explicit pre-hold service continuity"
        )

    runtime_dir = _canonical_directory(runtime_dir, label="runtime")
    app_dir = _canonical_directory(app_dir, label="application")
    env_file = _canonical_file(env_file, label="environment")
    deployed_sha_file = _canonical_file(
        deployed_sha_file,
        label="deployed SHA",
    )
    _verify_deployed_sha(deployed_sha_file, deployed_sha)
    jobs_candidate = runtime_dir / JOB_DIRECTORY_NAME
    job_candidate = jobs_candidate / job_id
    if jobs_candidate.is_symlink() or job_candidate.is_symlink():
        raise MaintenanceRestoreJobError(
            "restore job durable path must not be a symlink"
        )
    if (job_candidate / "request.json").is_file():
        stored_before_lock = _read_request(job_candidate)
        service_continuity = _require_service_continuity(
            dict(stored_before_lock.get("service_continuity") or {}),
            expected_fingerprint=service_continuity_fingerprint,
            window_id=window_id,
            plan_fingerprint=plan_fingerprint,
        )
    else:
        service_continuity = _require_service_continuity(
            (
                continuity_reader()
                if continuity_reader is not None
                else _capture_service_continuity(
                    runtime_dir=runtime_dir,
                    app_dir=app_dir,
                    env_file=env_file,
                )
            ),
            expected_fingerprint=service_continuity_fingerprint,
            window_id=window_id,
            plan_fingerprint=plan_fingerprint,
        )
        _validate_recovery_boundary(
            runtime_dir=runtime_dir,
            expected_revision=expected_revision,
            window_id=window_id,
            plan_fingerprint=plan_fingerprint,
            actor=actor,
            reason=reason,
            allow_resumed_policy=False,
            service_continuity=service_continuity,
        )

    material = {
        "contract_name": CONTRACT_NAME,
        "job_id": job_id,
        "deployed_sha": deployed_sha,
        "expected_revision": expected_revision,
        "window_id": window_id,
        "plan_fingerprint": plan_fingerprint,
        "service_continuity": service_continuity,
        "actor": actor,
        "reason": reason,
        "allow_pre_hold_service_continuity": True,
        "app_dir": str(app_dir),
        "env_file": str(env_file),
    }
    request = {
        **material,
        "request_digest": _fingerprint(material),
        "created_at": _now(),
    }
    jobs_root = _jobs_root(runtime_dir, create=True)
    with _exclusive_lock(jobs_root / "submit.lock"):
        _reject_concurrent_request(jobs_root, requested_job_id=job_id)
        candidate = jobs_root / job_id
        if candidate.is_symlink():
            raise MaintenanceRestoreJobError(
                "restore job directory must not be a symlink"
            )
        if candidate.exists() and not candidate.is_dir():
            raise MaintenanceRestoreJobError(
                "restore job path is not a directory"
            )
        existing_request = (candidate / "request.json").is_file()
        if candidate.is_dir() and not existing_request:
            unclassified = sorted(
                item.name
                for item in candidate.iterdir()
                if item.name
                not in {
                    "job.lock",
                    "service-continuity.json",
                    "status.json",
                    "audit.jsonl",
                }
            )
            if unclassified:
                raise MaintenanceRestoreJobError(
                    "incomplete restore job contains unclassified state"
                )
        if existing_request:
            stored = _read_request(candidate)
            if stored["request_digest"] != request["request_digest"]:
                raise MaintenanceRestoreJobError(
                    "job id is already bound to a different exact restore"
                )
            request = stored
        _validate_recovery_boundary(
            runtime_dir=runtime_dir,
            expected_revision=expected_revision,
            window_id=window_id,
            plan_fingerprint=plan_fingerprint,
            actor=actor,
            reason=reason,
            allow_resumed_policy=existing_request,
            service_continuity=service_continuity,
        )
        job_dir = _job_directory(runtime_dir, job_id, create=True)
        with _exclusive_lock(job_dir / "job.lock"):
            request_path = job_dir / "request.json"
            if not existing_request:
                _atomic_write_json(
                    job_dir / "service-continuity.json",
                    service_continuity,
                )
                _atomic_write_json(
                    job_dir / "status.json",
                    {
                        "contract_name": CONTRACT_NAME,
                        "job_id": job_id,
                        "request_digest": request["request_digest"],
                        "status": "queued",
                        "terminal": False,
                        "attempt": 0,
                        "updated_at": _now(),
                    },
                )
                _append_audit(
                    job_dir,
                    {
                        "event": "queued",
                        "captured_at": _now(),
                        "request_digest": request["request_digest"],
                    },
                )
                _atomic_write_json(request_path, request)
            else:
                _read_service_continuity(job_dir, request=request)

            current = _read_status(job_dir, request=request)
            if current["status"] in TERMINAL_STATUSES:
                return {
                    **_status_report(
                        runtime_dir=runtime_dir,
                        job_id=job_id,
                        expected_deployed_sha=deployed_sha,
                        include_systemd=False,
                    ),
                    "submit_idempotent": True,
                    "unit_start_requested": False,
                }
            if current["status"] == "running":
                return {
                    **_status_report(
                        runtime_dir=runtime_dir,
                        job_id=job_id,
                        expected_deployed_sha=deployed_sha,
                        include_systemd=False,
                    ),
                    "submit_idempotent": True,
                    "unit_start_requested": False,
                }
            start_preflight_complete = False
            try:
                _prove_lock_free(
                    jobs_root / "worker.lock",
                    label="detached restore worker",
                )
                _prove_lock_free(
                    runtime_dir / ".business-data-maintenance-restore.lock",
                    label="business-data maintenance restore",
                )
                start_preflight_complete = True
                unit = (starter or _start_systemd_unit)(job_id)
            except Exception as exc:
                failed_start = {
                    **current,
                    "status": "start_failed",
                    "terminal": False,
                    "retryable": True,
                    "error": _error_record(
                        code=(
                            "systemd_start_failed"
                            if start_preflight_complete
                            else "restore_start_preflight_failed"
                        ),
                        exc=exc,
                    ),
                    "updated_at": _now(),
                }
                _append_audit(
                    job_dir,
                    {
                        "event": "start_failed",
                        "captured_at": _now(),
                        "error": failed_start["error"],
                    },
                )
                _atomic_write_json(job_dir / "status.json", failed_start)
                return {
                    **failed_start,
                    "request": request,
                    "submit_idempotent": existing_request,
                    "unit_start_requested": False,
                }

    return {
        **_status_report(
            runtime_dir=runtime_dir,
            job_id=job_id,
            expected_deployed_sha=deployed_sha,
            include_systemd=False,
        ),
        "submit_idempotent": existing_request,
        "unit_start_requested": True,
        "unit": unit,
    }


def resume_failed_job(
    *,
    runtime_dir: Path,
    app_dir: Path,
    env_file: Path,
    deployed_sha_file: Path,
    job_id: str,
    deployed_sha: str,
    expected_failure_digest: str,
    service_continuity_fingerprint: str,
    actor: str,
    reason: str,
    starter: Callable[[str], dict[str, Any]] | None = None,
    continuity_reader: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Explicitly append one bounded binding and resume the same failed job."""

    job_id = _require_job_id(job_id)
    deployed_sha = _require_deployed_sha(deployed_sha)
    expected_failure_digest = _require_fingerprint(
        expected_failure_digest
    )
    service_continuity_fingerprint = _require_fingerprint(
        service_continuity_fingerprint
    )
    actor = _require_audit_text(actor, label="resume actor", maximum=160)
    reason = _require_audit_text(reason, label="resume reason", maximum=500)
    runtime_dir = _canonical_directory(runtime_dir, label="runtime")
    app_dir = _canonical_directory(app_dir, label="application")
    env_file = _canonical_file(env_file, label="environment")
    deployed_sha_file = _canonical_file(
        deployed_sha_file,
        label="deployed SHA",
    )
    _verify_deployed_sha(deployed_sha_file, deployed_sha)
    job_dir = _job_directory(runtime_dir, job_id, create=False)
    request = _read_request(job_dir)
    expected_continuity = _read_service_continuity(
        job_dir,
        request=request,
    )
    if request["app_dir"] != str(app_dir) or request["env_file"] != str(
        env_file
    ):
        raise MaintenanceRestoreJobError(
            "persisted restore runtime binding does not match resume"
        )
    if (
        str(expected_continuity.get("fingerprint") or "")
        != service_continuity_fingerprint
    ):
        raise MaintenanceRestoreJobError(
            "resume continuity fingerprint disagrees with the exact job"
        )
    jobs_root = job_dir.parent

    def validate_resume_boundary() -> None:
        _validate_recovery_boundary(
            runtime_dir=runtime_dir,
            expected_revision=int(request["expected_revision"]),
            window_id=str(request["window_id"]),
            plan_fingerprint=str(request["plan_fingerprint"]),
            actor=str(request["actor"]),
            reason=str(request["reason"]),
            allow_resumed_policy=False,
            service_continuity=expected_continuity,
        )
        current_continuity = _require_service_continuity(
            (
                continuity_reader()
                if continuity_reader is not None
                else _capture_service_continuity(
                    runtime_dir=runtime_dir,
                    app_dir=app_dir,
                    env_file=env_file,
                )
            ),
            expected_fingerprint=service_continuity_fingerprint,
            window_id=str(request["window_id"]),
            plan_fingerprint=str(request["plan_fingerprint"]),
        )
        if current_continuity != expected_continuity:
            raise MaintenanceRestoreJobError(
                "same-job resume boundary drifted from the original "
                "continuity evidence"
            )
        _prove_lock_free(
            jobs_root / "worker.lock",
            label="detached restore worker",
        )
        _prove_lock_free(
            runtime_dir / ".business-data-maintenance-restore.lock",
            label="business-data maintenance restore",
        )

    with _exclusive_lock(jobs_root / "submit.lock"):
        _reject_concurrent_request(jobs_root, requested_job_id=job_id)
        with _exclusive_lock(job_dir / "job.lock"):
            status = _read_status(job_dir, request=request)
            audit = _read_audit_summary(
                job_dir,
                request=request,
                status=status,
            )
            bindings = _read_resume_bindings(
                job_dir,
                request=request,
            )
            effective_binding = (
                bindings[-1]
                if bindings
                else {
                    "resume_sequence": 0,
                    "deployed_sha": str(request["deployed_sha"]),
                }
            )
            existing_binding = (
                str(effective_binding["deployed_sha"]) == deployed_sha
                and int(effective_binding["resume_sequence"]) > 0
            )
            if existing_binding:
                binding = effective_binding
                expected_binding = {
                    "deployed_sha": deployed_sha,
                    "expected_failure_digest": expected_failure_digest,
                    "service_continuity_fingerprint": (
                        service_continuity_fingerprint
                    ),
                    "actor": actor,
                    "reason": reason,
                }
                if any(
                    binding.get(key) != value
                    for key, value in expected_binding.items()
                ):
                    raise MaintenanceRestoreJobError(
                        "same-job resume is already bound to different evidence"
                    )
                resume_sequence = int(binding["resume_sequence"])
                archived_result = _read_job_result(
                    job_dir,
                    request=request,
                    filename=f"attempt-{resume_sequence}-result.json",
                )
                if (
                    archived_result.get("result_digest")
                    != expected_failure_digest
                    or str(
                        (archived_result.get("error") or {}).get("code")
                        or ""
                    )
                    != "maintenance_restore_failed"
                ):
                    raise MaintenanceRestoreJobError(
                        "archived failure disagrees with resume binding"
                    )
                attempt = int(status.get("attempt") or 0)
                if (
                    status["status"] == "failed"
                    and attempt == resume_sequence
                ):
                    if (
                        str((status.get("error") or {}).get("code") or "")
                        != "maintenance_restore_failed"
                        or str(status.get("result_digest") or "")
                        != expected_failure_digest
                        or audit.get("last_event") != "failed"
                        or archived_result.get("error") != status.get("error")
                    ):
                        raise MaintenanceRestoreJobError(
                            "persisted same-job resume no longer has the "
                            "exact bound terminal failure"
                        )
                elif status["status"] in TERMINAL_STATUSES:
                    if attempt != resume_sequence + 1:
                        raise MaintenanceRestoreJobError(
                            "terminal same-job attempt disagrees with the "
                            "bounded recovery sequence"
                        )
                    return _status_report(
                        runtime_dir=runtime_dir,
                        job_id=job_id,
                        expected_deployed_sha=deployed_sha,
                        include_systemd=False,
                    )
                elif status["status"] == "running":
                    if attempt != resume_sequence + 1:
                        raise MaintenanceRestoreJobError(
                            "running same-job attempt disagrees with the "
                            "bounded recovery sequence"
                        )
                    return _status_report(
                        runtime_dir=runtime_dir,
                        job_id=job_id,
                        expected_deployed_sha=deployed_sha,
                        include_systemd=False,
                    )
                elif (
                    int(status.get("resume_sequence") or 0)
                    != resume_sequence
                    or attempt != resume_sequence
                ):
                    raise MaintenanceRestoreJobError(
                        "startable same-job state disagrees with the bounded "
                        "recovery sequence"
                    )
                validate_resume_boundary()
            else:
                if deployed_sha == str(effective_binding["deployed_sha"]):
                    raise MaintenanceRestoreJobError(
                        "same-job resume requires a new reviewed deployed SHA"
                    )
                resume_sequence = len(bindings) + 1
                if resume_sequence > MAX_RESUME_SEQUENCE:
                    raise MaintenanceRestoreJobError(
                        "same-job recovery sequence is exhausted"
                    )
                if (
                    status["status"] != "failed"
                    or int(status.get("attempt") or 0) != resume_sequence
                    or str((status.get("error") or {}).get("code") or "")
                    != "maintenance_restore_failed"
                    or str(status.get("result_digest") or "")
                    != expected_failure_digest
                    or audit.get("last_event") != "failed"
                ):
                    raise MaintenanceRestoreJobError(
                        "same-job resume requires the exact next terminal "
                        "maintenance restore failure"
                    )
                result_record = _read_job_result(
                    job_dir,
                    request=request,
                )
                if (
                    result_record.get("result_digest")
                    != expected_failure_digest
                    or result_record.get("error") != status.get("error")
                ):
                    raise MaintenanceRestoreJobError(
                        "failed status/result evidence disagrees"
                    )
                validate_resume_boundary()
                binding_material = {
                    "contract_name": CONTRACT_NAME,
                    "job_id": job_id,
                    "request_digest": request["request_digest"],
                    "resume_sequence": resume_sequence,
                    "previous_deployed_sha": str(
                        effective_binding["deployed_sha"]
                    ),
                    "deployed_sha": deployed_sha,
                    "expected_failure_digest": expected_failure_digest,
                    "service_continuity_fingerprint": (
                        service_continuity_fingerprint
                    ),
                    "actor": actor,
                    "reason": reason,
                    "created_at": _now(),
                }
                binding = {
                    **binding_material,
                    "binding_digest": _fingerprint(binding_material),
                }
                archive_path = (
                    job_dir / f"attempt-{resume_sequence}-result.json"
                )
                if archive_path.exists():
                    archived_result = _read_job_result(
                        job_dir,
                        request=request,
                        filename=archive_path.name,
                    )
                    if archived_result != result_record:
                        raise MaintenanceRestoreJobError(
                            "immutable attempt archive contains different "
                            "failure evidence"
                        )
                else:
                    _atomic_write_json(archive_path, result_record)
                binding_path = job_dir / _resume_binding_filename(
                    resume_sequence
                )
                if binding_path.exists():
                    raise MaintenanceRestoreJobError(
                        "same-job resume binding appeared concurrently"
                    )
                _atomic_write_json(binding_path, binding)
            if status["status"] == "failed" and int(
                status.get("attempt") or 0
            ) == resume_sequence:
                queued = {
                    "contract_name": CONTRACT_NAME,
                    "job_id": job_id,
                    "request_digest": request["request_digest"],
                    "status": "queued",
                    "terminal": False,
                    "attempt": resume_sequence,
                    "resume_sequence": resume_sequence,
                    "updated_at": _now(),
                }
                _atomic_write_json(job_dir / "status.json", queued)
                if audit.get("last_event") != "resume_queued":
                    _append_audit(
                        job_dir,
                        {
                            "event": "resume_queued",
                            "captured_at": _now(),
                            "attempt": resume_sequence,
                            "resume_sequence": resume_sequence,
                            "deployed_sha": deployed_sha,
                            "expected_failure_digest": (
                                expected_failure_digest
                            ),
                            "binding_digest": binding["binding_digest"],
                            "actor": actor,
                            "reason": reason,
                        },
                    )
                status = queued
            elif (
                status["status"] == "queued"
                and audit.get("last_event") != "resume_queued"
            ):
                _append_audit(
                    job_dir,
                    {
                        "event": "resume_queued",
                        "captured_at": _now(),
                        "attempt": resume_sequence,
                        "resume_sequence": resume_sequence,
                        "deployed_sha": deployed_sha,
                        "expected_failure_digest": expected_failure_digest,
                        "binding_digest": binding["binding_digest"],
                        "actor": actor,
                        "reason": reason,
                    },
                )
            if status["status"] not in {"queued", "start_failed"}:
                raise MaintenanceRestoreJobError(
                    "same-job resume is outside a startable state"
                )
            start_preflight_complete = False
            try:
                _prove_lock_free(
                    jobs_root / "worker.lock",
                    label="detached restore worker",
                )
                _prove_lock_free(
                    runtime_dir / ".business-data-maintenance-restore.lock",
                    label="business-data maintenance restore",
                )
                start_preflight_complete = True
                unit = (starter or _start_systemd_unit)(job_id)
            except Exception as exc:
                start_failed = {
                    **status,
                    "status": "start_failed",
                    "terminal": False,
                    "retryable": True,
                    "error": _error_record(
                        code=(
                            "systemd_resume_start_failed"
                            if start_preflight_complete
                            else "restore_resume_start_preflight_failed"
                        ),
                        exc=exc,
                    ),
                    "updated_at": _now(),
                }
                _append_audit(
                    job_dir,
                    {
                        "event": "start_failed",
                        "captured_at": _now(),
                        "error": start_failed["error"],
                    },
                )
                _atomic_write_json(job_dir / "status.json", start_failed)
                return {
                    **start_failed,
                    "request": request,
                    "resume_idempotent": existing_binding,
                    "unit_start_requested": False,
                }
    return {
        **_status_report(
            runtime_dir=runtime_dir,
            job_id=job_id,
            expected_deployed_sha=deployed_sha,
            include_systemd=False,
        ),
        "resume_idempotent": existing_binding,
        "unit_start_requested": True,
        "unit": unit,
    }


def job_status(
    *,
    runtime_dir: Path,
    job_id: str,
    deployed_sha: str,
    include_systemd: bool = True,
) -> dict[str, Any]:
    """Read one exact job without creating or changing state."""

    return _status_report(
        runtime_dir=_canonical_directory(runtime_dir, label="runtime"),
        job_id=_require_job_id(job_id),
        expected_deployed_sha=_require_deployed_sha(deployed_sha),
        include_systemd=include_systemd,
    )


def run_worker(
    *,
    runtime_dir: Path,
    app_dir: Path,
    env_file: Path,
    deployed_sha_file: Path,
    job_id: str,
    executor: Callable[[dict[str, Any], int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute or resume one persisted exact restore."""

    job_id = _require_job_id(job_id)
    runtime_dir = _canonical_directory(runtime_dir, label="runtime")
    app_dir = _canonical_directory(app_dir, label="application")
    env_file = _canonical_file(env_file, label="environment")
    deployed_sha_file = _canonical_file(
        deployed_sha_file,
        label="deployed SHA",
    )
    job_dir = _job_directory(runtime_dir, job_id, create=False)
    request = _read_request(job_dir)
    _read_service_continuity(job_dir, request=request)
    deployment_binding = _effective_deployment_binding(
        job_dir,
        request=request,
    )
    if request["app_dir"] != str(app_dir) or request["env_file"] != str(env_file):
        raise MaintenanceRestoreJobError(
            "persisted restore runtime binding does not match the fixed worker"
        )
    current = _read_status(job_dir, request=request)
    _read_audit_summary(
        job_dir,
        request=request,
        status=current,
    )
    if current["status"] in TERMINAL_STATUSES:
        return _status_report(
            runtime_dir=runtime_dir,
            job_id=job_id,
            expected_deployed_sha=deployment_binding["deployed_sha"],
            include_systemd=False,
        )

    jobs_root = job_dir.parent
    worker_lock_path = jobs_root / "worker.lock"
    if worker_lock_path.is_symlink():
        raise MaintenanceRestoreJobError(
            "restore worker lock must not be a symlink"
        )
    global_handle = worker_lock_path.open("a+b")
    os.chmod(worker_lock_path, 0o600)
    try:
        try:
            fcntl.flock(
                global_handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            return {
                **current,
                "request": request,
                "worker_lock_busy": True,
            }

        with _exclusive_lock(job_dir / "job.lock"):
            current = _read_status(job_dir, request=request)
            if current["status"] in TERMINAL_STATUSES:
                return _status_report(
                    runtime_dir=runtime_dir,
                    job_id=job_id,
                    expected_deployed_sha=deployment_binding["deployed_sha"],
                    include_systemd=False,
                )
            started_at = str(current.get("started_at") or _now())
            deadline_at = str(
                current.get("deadline_at")
                or _format_timestamp(
                    _parse_timestamp(started_at, label="worker start")
                    + timedelta(seconds=RESTORE_DEADLINE_SECONDS)
                )
            )
            running = {
                **current,
                "status": "running",
                "terminal": False,
                "attempt": int(current.get("attempt") or 0) + 1,
                "worker_pid": os.getpid(),
                "started_at": started_at,
                "deadline_at": deadline_at,
                "heartbeat_at": _now(),
                "updated_at": _now(),
            }
            running.pop("error", None)
            _atomic_write_json(job_dir / "status.json", running)
            _append_audit(
                job_dir,
                {
                    "event": "worker_started",
                    "captured_at": _now(),
                    "attempt": running["attempt"],
                    "worker_pid": os.getpid(),
                },
            )

        try:
            _verify_deployed_sha(
                deployed_sha_file,
                deployment_binding["deployed_sha"],
            )
            if int(deployment_binding.get("resume_sequence") or 0) >= 1:
                _validate_recovery_boundary(
                    runtime_dir=runtime_dir,
                    expected_revision=int(request["expected_revision"]),
                    window_id=str(request["window_id"]),
                    plan_fingerprint=str(request["plan_fingerprint"]),
                    actor=str(request["actor"]),
                    reason=str(request["reason"]),
                    allow_resumed_policy=False,
                    service_continuity=dict(
                        request.get("service_continuity") or {}
                    ),
                )
                current_continuity = _require_service_continuity(
                    _capture_service_continuity(
                        runtime_dir=runtime_dir,
                        app_dir=app_dir,
                        env_file=env_file,
                    ),
                    expected_fingerprint=str(
                        deployment_binding[
                            "service_continuity_fingerprint"
                        ]
                    ),
                    window_id=str(request["window_id"]),
                    plan_fingerprint=str(request["plan_fingerprint"]),
                )
                expected_continuity = _read_service_continuity(
                    job_dir,
                    request=request,
                )
                if current_continuity != expected_continuity:
                    raise MaintenanceRestoreJobError(
                        "same-job recovery attempt boundary drifted from the "
                        "original continuity evidence"
                    )
                _prove_lock_free(
                    runtime_dir
                    / ".business-data-maintenance-restore.lock",
                    label="business-data maintenance restore",
                )
            effective_revision, already_restored = _effective_restore_revision(
                runtime_dir=runtime_dir,
                request=request,
            )
            if already_restored:
                restore_result: dict[str, Any] = {}
            else:
                restore_result = (
                    executor(request, effective_revision)
                    if executor is not None
                else _execute_restore(
                    request=request,
                    effective_revision=effective_revision,
                    runtime_dir=runtime_dir,
                    app_dir=app_dir,
                    env_file=env_file,
                    job_dir=job_dir,
                    deadline_at=_parse_timestamp(
                        str(running["deadline_at"]),
                        label="worker deadline",
                    ),
                    attempt=int(running["attempt"]),
                )
            )
            _verify_deployed_sha(
                deployed_sha_file,
                deployment_binding["deployed_sha"],
            )
            readback = _validated_terminal_readback(
                runtime_dir=runtime_dir,
                request=request,
                restore_result=restore_result,
            )
        except Exception as exc:
            return _finish_failed(
                job_dir=job_dir,
                request=request,
                status=running,
                code="maintenance_restore_failed",
                exc=exc,
            )

        result = {
            "status": "restored",
            "idempotent_recovery": already_restored,
            "effective_revision": effective_revision,
            "restore": restore_result,
            "readback": readback,
        }
        result_record = {
            "contract_name": CONTRACT_NAME,
            "job_id": job_id,
            "request_digest": request["request_digest"],
            "completed_at": _now(),
            "result": result,
        }
        result_record["result_digest"] = _fingerprint(result)
        _atomic_write_json(job_dir / "result.json", result_record)
        succeeded = {
            **running,
            "status": "succeeded",
            "terminal": True,
            "completed_at": result_record["completed_at"],
            "result_digest": result_record["result_digest"],
            "updated_at": _now(),
        }
        _append_audit(
            job_dir,
            {
                "event": "succeeded",
                "captured_at": _now(),
                "attempt": succeeded["attempt"],
                "result_digest": succeeded["result_digest"],
            },
        )
        _atomic_write_json(job_dir / "status.json", succeeded)
        return {
            **succeeded,
            "request": request,
            "result": result,
        }
    finally:
        try:
            fcntl.flock(global_handle.fileno(), fcntl.LOCK_UN)
        finally:
            global_handle.close()


def _capture_service_continuity(
    *,
    runtime_dir: Path,
    app_dir: Path,
    env_file: Path,
) -> dict[str, Any]:
    completed = subprocess.run(
        [
            sys.executable,
            str(app_dir / "apps" / "business_data_maintenance.py"),
            "restore-continuity-status",
            "--runtime-dir",
            str(runtime_dir),
            "--env-file",
            str(env_file),
        ],
        cwd=app_dir,
        text=True,
        capture_output=True,
        timeout=120.0,
        check=False,
    )
    if completed.returncode != 0:
        raise MaintenanceRestoreJobError(
            _bounded_message(
                completed.stderr.strip()
                or completed.stdout.strip()
                or (
                    "maintenance restore continuity preflight exited "
                    f"{completed.returncode}"
                )
            )
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MaintenanceRestoreJobError(
            "maintenance restore continuity preflight returned invalid JSON"
        ) from exc
    if (
        not isinstance(payload, dict)
        or str(payload.get("status") or "") != "ready"
        or not isinstance(payload.get("service_continuity"), dict)
    ):
        raise MaintenanceRestoreJobError(
            "maintenance restore continuity preflight is incomplete"
        )
    return dict(payload["service_continuity"])


def _execute_restore(
    *,
    request: Mapping[str, Any],
    effective_revision: int,
    runtime_dir: Path,
    app_dir: Path,
    env_file: Path,
    job_dir: Path,
    deadline_at: datetime,
    attempt: int,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(app_dir / "apps" / "business_data_maintenance.py"),
        "restore",
        "--runtime-dir",
        str(runtime_dir),
        "--env-file",
        str(env_file),
        "--expected-revision",
        str(effective_revision),
        "--actor",
        str(request["actor"]),
        "--reason",
        str(request["reason"]),
        "--allow-pre-hold-service-continuity",
        "--pre-hold-service-continuity-file",
        str(job_dir / "service-continuity.json"),
        "--expected-pre-hold-service-continuity-fingerprint",
        str(request["service_continuity"]["fingerprint"]),
    ]
    remaining = (deadline_at - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        raise TimeoutError("maintenance restore deadline was exceeded")
    process = subprocess.Popen(
        command,
        cwd=app_dir,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout = ""
    stderr = ""
    while True:
        remaining = (deadline_at - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=10.0)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
            raise TimeoutError(
                "maintenance restore exceeded its durable worker deadline"
            )
        try:
            stdout, stderr = process.communicate(
                timeout=min(HEARTBEAT_INTERVAL_SECONDS, remaining)
            )
            break
        except subprocess.TimeoutExpired:
            _write_worker_heartbeat(
                job_dir=job_dir,
                request=request,
                attempt=attempt,
                deadline_at=deadline_at,
                worker_pid=os.getpid(),
            )
    completed = subprocess.CompletedProcess(
        args=command,
        returncode=int(process.returncode or 0),
        stdout=stdout,
        stderr=stderr,
    )
    if completed.returncode != 0:
        raise MaintenanceRestoreJobError(
            _bounded_message(
                completed.stderr.strip()
                or completed.stdout.strip()
                or f"restore exited {completed.returncode}"
            )
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MaintenanceRestoreJobError(
            "maintenance restore returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise MaintenanceRestoreJobError(
            "maintenance restore returned a non-object payload"
        )
    if (
        str(payload.get("status") or "") != "restored"
        or payload.get("exact_prior_state_restored") is not True
        or not FINGERPRINT_PATTERN.fullmatch(
            str(payload.get("control_signature") or "")
        )
    ):
        raise MaintenanceRestoreJobError(
            "maintenance restore returned incomplete exact-state evidence"
        )
    return payload


def _effective_restore_revision(
    *,
    runtime_dir: Path,
    request: Mapping[str, Any],
) -> tuple[int, bool]:
    boundary = _validate_recovery_boundary(
        runtime_dir=runtime_dir,
        expected_revision=int(request["expected_revision"]),
        window_id=str(request["window_id"]),
        plan_fingerprint=str(request["plan_fingerprint"]),
        actor=str(request["actor"]),
        reason=str(request["reason"]),
        allow_resumed_policy=True,
        service_continuity=dict(
            request.get("service_continuity") or {}
        ),
    )
    maintenance = boundary["maintenance"]
    policy = boundary["policy"]
    exact_restored = (
        str(maintenance.get("phase") or "") == "restored"
        and maintenance.get("exact_prior_state_restored") is True
    )
    return int(policy["revision"]), exact_restored


def _validate_recovery_boundary(
    *,
    runtime_dir: Path,
    expected_revision: int,
    window_id: str,
    plan_fingerprint: str,
    actor: str,
    reason: str,
    allow_resumed_policy: bool,
    service_continuity: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    boundary_kind = str(
        service_continuity.get("boundary_kind") or ""
    )
    barrier = _read_json(
        runtime_dir / ".business-data-write-barrier.json",
        label="write barrier",
    )
    if boundary_kind == QUIET_CONFIRMED_HOLD_CONTINUITY_KIND:
        if (
            barrier.get("active") is not True
            or str(barrier.get("phase") or "")
            not in {"held", "restoring"}
            or barrier.get("hold_confirmed") is not True
            or str(barrier.get("window_id") or "") != window_id
            or str(barrier.get("plan_fingerprint") or "")
            != plan_fingerprint
        ):
            raise MaintenanceRestoreJobError(
                "exact quiet confirmed barrier identity is unavailable"
            )
    elif boundary_kind:
        raise MaintenanceRestoreJobError(
            "restore continuity kind is not supported"
        )
    elif (
        barrier.get("active") is not True
        or str(barrier.get("phase") or "") != "acquiring"
        or barrier.get("hold_confirmed") is not False
        or str(barrier.get("window_id") or "") != window_id
        or str(barrier.get("plan_fingerprint") or "") != plan_fingerprint
    ):
        raise MaintenanceRestoreJobError(
            "exact unconfirmed acquiring barrier identity is unavailable"
        )
    maintenance = _read_json(
        runtime_dir / ".business-data-maintenance.json",
        label="maintenance state",
    )
    phase = str(maintenance.get("phase") or "")
    allowed_phases = (
        {"held", "restored"}
        if boundary_kind == QUIET_CONFIRMED_HOLD_CONTINUITY_KIND
        else {"holding", "restored"}
    )
    if phase not in allowed_phases:
        raise MaintenanceRestoreJobError(
            "maintenance state is outside the exact recovery phase"
        )
    policy = _read_json(
        runtime_dir / ".auto-updates-policy.json",
        label="owner policy",
    )
    revision = int(policy.get("revision") or -1)
    master_desired = policy.get("master_desired")
    if revision == expected_revision and master_desired is False:
        pass
    elif (
        allow_resumed_policy
        and revision == expected_revision + 1
        and master_desired is True
        and str(policy.get("actor") or "") == actor
        and str(policy.get("reason") or "") == reason
    ):
        pass
    else:
        raise MaintenanceRestoreJobError(
            "owner policy revision/state drifted from the exact restore request"
        )
    if (
        phase == "restored"
        and (
            maintenance.get("exact_prior_state_restored") is not True
            or not FINGERPRINT_PATTERN.fullmatch(
                str(
                    (
                        maintenance.get("restore_control_signature")
                        or {}
                    ).get("fingerprint")
                    or ""
                )
            )
        )
    ):
        raise MaintenanceRestoreJobError(
            "restored maintenance state lacks exact control evidence"
        )
    return {
        "barrier": barrier,
        "maintenance": maintenance,
        "policy": policy,
    }


def _validated_terminal_readback(
    *,
    runtime_dir: Path,
    request: Mapping[str, Any],
    restore_result: Mapping[str, Any],
) -> dict[str, Any]:
    boundary = _validate_recovery_boundary(
        runtime_dir=runtime_dir,
        expected_revision=int(request["expected_revision"]),
        window_id=str(request["window_id"]),
        plan_fingerprint=str(request["plan_fingerprint"]),
        actor=str(request["actor"]),
        reason=str(request["reason"]),
        allow_resumed_policy=True,
        service_continuity=dict(
            request.get("service_continuity") or {}
        ),
    )
    maintenance = boundary["maintenance"]
    policy = boundary["policy"]
    signature = str(
        (maintenance.get("restore_control_signature") or {}).get(
            "fingerprint"
        )
        or ""
    )
    if (
        str(maintenance.get("phase") or "") != "restored"
        or maintenance.get("exact_prior_state_restored") is not True
        or not FINGERPRINT_PATTERN.fullmatch(signature)
        or policy.get("master_desired") is not True
        or int(policy.get("revision") or -1)
        != int(request["expected_revision"]) + 1
    ):
        raise MaintenanceRestoreJobError(
            "terminal restore readback is incomplete"
        )
    if restore_result:
        result_signature = str(restore_result.get("control_signature") or "")
        if result_signature != signature:
            raise MaintenanceRestoreJobError(
                "restore result and durable control signature disagree"
            )
    continuity = dict(
        maintenance.get("pre_hold_service_continuity_readback") or {}
    )
    if not FINGERPRINT_PATTERN.fullmatch(
        str(continuity.get("fingerprint") or "")
    ):
        raise MaintenanceRestoreJobError(
            "terminal restore lacks pre-hold service continuity readback"
        )
    _validate_terminal_service_continuity(
        expected=dict(request.get("service_continuity") or {}),
        readback=continuity,
    )
    barrier = boundary["barrier"]
    return {
        "maintenance_phase": "restored",
        "exact_prior_state_restored": True,
        "control_signature": signature,
        "policy_revision": int(policy["revision"]),
        "master_desired": True,
        "barrier_active": True,
        "barrier_phase": str(barrier.get("phase") or ""),
        "barrier_hold_confirmed": bool(
            barrier.get("hold_confirmed")
        ),
        "window_id": str(request["window_id"]),
        "plan_fingerprint": str(request["plan_fingerprint"]),
        "pre_hold_service_continuity_readback": continuity,
    }


def _validate_terminal_service_continuity(
    *,
    expected: Mapping[str, Any],
    readback: Mapping[str, Any],
) -> None:
    boundary_kind = str(expected.get("boundary_kind") or "")
    if boundary_kind == QUIET_CONFIRMED_HOLD_CONTINUITY_KIND:
        payload = {
            "boundary_kind": boundary_kind,
            "services": [],
        }
        if (
            list(expected.get("services") or [])
            or str(readback.get("boundary_kind") or "") != boundary_kind
            or list(readback.get("services") or [])
            or str(readback.get("fingerprint") or "")
            != _fingerprint(payload)
        ):
            raise MaintenanceRestoreJobError(
                "terminal quiet confirmed-hold continuity disagrees with "
                "the request"
            )
        return
    expected_services = {
        str(item.get("unit") or ""): dict(item)
        for item in expected.get("services") or []
        if isinstance(item, Mapping)
    }
    actual_services = {
        str(item.get("unit") or ""): dict(item)
        for item in readback.get("services") or []
        if isinstance(item, Mapping)
    }
    if not expected_services or set(actual_services) != set(expected_services):
        raise MaintenanceRestoreJobError(
            "terminal service-continuity units disagree with the request"
        )
    for unit, actual in actual_services.items():
        expected_service = expected_services[unit]
        outcome = str(actual.get("outcome") or "")
        if outcome == "continued":
            if (
                int(actual.get("main_pid") or 0)
                != int(expected_service.get("main_pid") or 0)
                or str(actual.get("started_at") or "")
                != str(expected_service.get("started_at") or "")
            ):
                raise MaintenanceRestoreJobError(
                    "terminal continuing service generation drifted"
                )
        elif outcome != "completed":
            raise MaintenanceRestoreJobError(
                "terminal service-continuity outcome is invalid"
            )


def _finish_failed(
    *,
    job_dir: Path,
    request: Mapping[str, Any],
    status: Mapping[str, Any],
    code: str,
    exc: Exception,
) -> dict[str, Any]:
    error = _error_record(code=code, exc=exc)
    result_record = {
        "contract_name": CONTRACT_NAME,
        "job_id": request["job_id"],
        "request_digest": request["request_digest"],
        "completed_at": _now(),
        "error": error,
    }
    result_record["result_digest"] = _fingerprint(error)
    _atomic_write_json(job_dir / "result.json", result_record)
    failed = {
        **dict(status),
        "status": "failed",
        "terminal": True,
        "error": error,
        "completed_at": result_record["completed_at"],
        "result_digest": result_record["result_digest"],
        "updated_at": _now(),
    }
    _append_audit(
        job_dir,
        {
            "event": "failed",
            "captured_at": _now(),
            "attempt": int(status.get("attempt") or 0),
            "error": error,
        },
    )
    _atomic_write_json(job_dir / "status.json", failed)
    return {
        **failed,
        "request": dict(request),
    }


def _write_worker_heartbeat(
    *,
    job_dir: Path,
    request: Mapping[str, Any],
    attempt: int,
    deadline_at: datetime,
    worker_pid: int,
) -> None:
    with _exclusive_lock(job_dir / "job.lock"):
        current = _read_status(job_dir, request=request)
        if (
            current["status"] != "running"
            or int(current.get("attempt") or 0) != attempt
            or int(current.get("worker_pid") or 0) != worker_pid
        ):
            raise MaintenanceRestoreJobError(
                "durable worker identity changed during restore"
            )
        heartbeat_at = _now()
        _atomic_write_json(
            job_dir / "status.json",
            {
                **current,
                "deadline_at": _format_timestamp(deadline_at),
                "heartbeat_at": heartbeat_at,
                "updated_at": heartbeat_at,
            },
        )


def _read_audit_summary(
    job_dir: Path,
    *,
    request: Mapping[str, Any],
    status: Mapping[str, Any],
) -> dict[str, Any]:
    path = job_dir / "audit.jsonl"
    if path.is_symlink() or not path.is_file():
        raise MaintenanceRestoreJobError(
            "restore job audit is unavailable"
        )
    raw = path.read_bytes()
    rows: list[dict[str, Any]] = []
    try:
        for line in raw.decode("utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError("audit row is not an object")
            rows.append(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise MaintenanceRestoreJobError(
            "restore job audit is invalid"
        ) from exc
    if (
        not rows
        or str(rows[0].get("event") or "") != "queued"
        or rows[0].get("request_digest") != request["request_digest"]
    ):
        raise MaintenanceRestoreJobError(
            "restore job audit identity is invalid"
        )
    value = str(status.get("status") or "")
    last_event = str(rows[-1].get("event") or "")
    if value in TERMINAL_STATUSES and last_event != value:
        raise MaintenanceRestoreJobError(
            "terminal restore job audit/status mismatch"
        )
    if value == "start_failed" and last_event != "start_failed":
        raise MaintenanceRestoreJobError(
            "restore start-failure audit/status mismatch"
        )
    return {
        "event_count": len(rows),
        "events": [str(row.get("event") or "") for row in rows],
        "last_event": last_event,
        "last_captured_at": str(rows[-1].get("captured_at") or ""),
        "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
    }


def _status_report(
    *,
    runtime_dir: Path,
    job_id: str,
    expected_deployed_sha: str,
    include_systemd: bool,
) -> dict[str, Any]:
    job_dir = _job_directory(runtime_dir, job_id, create=False)
    request = _read_request(job_dir)
    _read_service_continuity(job_dir, request=request)
    deployment_binding = _effective_deployment_binding(
        job_dir,
        request=request,
    )
    if deployment_binding["deployed_sha"] != expected_deployed_sha:
        raise MaintenanceRestoreJobError(
            "job deployed SHA does not match status request"
        )
    status = _read_status(job_dir, request=request)
    audit = _read_audit_summary(
        job_dir,
        request=request,
        status=status,
    )
    report: dict[str, Any] = {
        **status,
        "request": request,
        "deployment_binding": deployment_binding,
        "audit": audit,
    }
    result_path = job_dir / "result.json"
    if result_path.exists():
        result_record = _read_job_result(job_dir, request=request)
        report["result_record"] = result_record
        if "result" in result_record:
            report["result"] = result_record["result"]
    unit: dict[str, Any] | None = None
    if include_systemd:
        unit = _systemd_unit_status(job_id)
        report["unit"] = unit
    report["worker_observation"] = _classify_worker_observation(
        status,
        unit=unit,
    )
    return report


def _read_job_result(
    job_dir: Path,
    *,
    request: Mapping[str, Any],
    filename: str = "result.json",
) -> dict[str, Any]:
    result_record = _read_json(job_dir / filename, label="job result")
    if (
        result_record.get("contract_name") != CONTRACT_NAME
        or result_record.get("job_id") != request["job_id"]
        or result_record.get("request_digest")
        != request["request_digest"]
    ):
        raise MaintenanceRestoreJobError("job result identity mismatch")
    material = result_record.get("result", result_record.get("error"))
    if result_record.get("result_digest") != _fingerprint(material):
        raise MaintenanceRestoreJobError("job result digest mismatch")
    return result_record


def _resume_binding_filename(resume_sequence: int) -> str:
    if resume_sequence == 1:
        return RESUME_BINDING_FILENAME
    if 1 < resume_sequence <= MAX_RESUME_SEQUENCE:
        return f"resume-{resume_sequence}.json"
    raise MaintenanceRestoreJobError(
        "same-job recovery sequence is outside the bounded contract"
    )


def _read_resume_binding(
    job_dir: Path,
    *,
    request: Mapping[str, Any],
    resume_sequence: int,
    previous_deployed_sha: str,
) -> dict[str, Any]:
    binding = _read_json(
        job_dir / _resume_binding_filename(resume_sequence),
        label="job resume binding",
    )
    material = {
        "contract_name": str(binding.get("contract_name") or ""),
        "job_id": str(binding.get("job_id") or ""),
        "request_digest": str(binding.get("request_digest") or ""),
        "resume_sequence": int(binding.get("resume_sequence") or 0),
        "previous_deployed_sha": _require_deployed_sha(
            str(binding.get("previous_deployed_sha") or "")
        ),
        "deployed_sha": _require_deployed_sha(
            str(binding.get("deployed_sha") or "")
        ),
        "expected_failure_digest": _require_fingerprint(
            str(binding.get("expected_failure_digest") or "")
        ),
        "service_continuity_fingerprint": _require_fingerprint(
            str(binding.get("service_continuity_fingerprint") or "")
        ),
        "actor": _require_audit_text(
            str(binding.get("actor") or ""),
            label="resume actor",
            maximum=160,
        ),
        "reason": _require_audit_text(
            str(binding.get("reason") or ""),
            label="resume reason",
            maximum=500,
        ),
        "created_at": str(binding.get("created_at") or ""),
    }
    if (
        material["contract_name"] != CONTRACT_NAME
        or material["job_id"] != request["job_id"]
        or material["request_digest"] != request["request_digest"]
        or material["resume_sequence"] != resume_sequence
        or material["previous_deployed_sha"] != previous_deployed_sha
        or material["deployed_sha"] == previous_deployed_sha
        or material["service_continuity_fingerprint"]
        != str(request["service_continuity"]["fingerprint"])
        or not material["created_at"]
        or binding.get("binding_digest") != _fingerprint(material)
    ):
        raise MaintenanceRestoreJobError(
            "same-job resume binding identity/digest is invalid"
        )
    return {
        **material,
        "binding_digest": str(binding["binding_digest"]),
    }


def _read_resume_bindings(
    job_dir: Path,
    *,
    request: Mapping[str, Any],
) -> list[dict[str, Any]]:
    allowed_names = {
        _resume_binding_filename(sequence)
        for sequence in range(1, MAX_RESUME_SEQUENCE + 1)
    }
    unexpected = sorted(
        path.name
        for path in job_dir.glob("resume-*.json")
        if path.name not in allowed_names
    )
    if unexpected:
        raise MaintenanceRestoreJobError(
            "restore job contains an out-of-contract recovery binding"
        )
    bindings: list[dict[str, Any]] = []
    previous_deployed_sha = str(request["deployed_sha"])
    missing_seen = False
    for resume_sequence in range(1, MAX_RESUME_SEQUENCE + 1):
        path = job_dir / _resume_binding_filename(resume_sequence)
        if not path.exists():
            missing_seen = True
            continue
        if missing_seen:
            raise MaintenanceRestoreJobError(
                "same-job recovery binding sequence is not contiguous"
            )
        binding = _read_resume_binding(
            job_dir,
            request=request,
            resume_sequence=resume_sequence,
            previous_deployed_sha=previous_deployed_sha,
        )
        archived_result = _read_job_result(
            job_dir,
            request=request,
            filename=f"attempt-{resume_sequence}-result.json",
        )
        if (
            archived_result.get("result_digest")
            != binding["expected_failure_digest"]
            or str(
                (archived_result.get("error") or {}).get("code") or ""
            )
            != "maintenance_restore_failed"
        ):
            raise MaintenanceRestoreJobError(
                "archived failure disagrees with recovery binding"
            )
        bindings.append(binding)
        previous_deployed_sha = str(binding["deployed_sha"])
    return bindings


def _effective_deployment_binding(
    job_dir: Path,
    *,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    bindings = _read_resume_bindings(job_dir, request=request)
    if not bindings:
        return {
            "resume_sequence": 0,
            "deployed_sha": str(request["deployed_sha"]),
            "request_digest": str(request["request_digest"]),
        }
    return bindings[-1]


def _classify_worker_observation(
    status: Mapping[str, Any],
    *,
    unit: Mapping[str, Any] | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    captured_at = now or datetime.now(timezone.utc)
    value = str(status.get("status") or "")
    observation: dict[str, Any] = {
        "captured_at": _format_timestamp(captured_at),
        "classification": "",
        "fail_closed": True,
        "status_read_only": True,
        "second_restore_auto_start_allowed": False,
        "barrier_abort_candidate": value == "succeeded",
        "worker_active": False,
    }
    if value in TERMINAL_STATUSES:
        observation["classification"] = f"terminal_{value}"
        return observation
    if value == "queued":
        observation["classification"] = "awaiting_systemd_start"
        return observation
    if value == "start_failed":
        observation["classification"] = "systemd_start_failed"
        return observation
    if value != "running":
        observation["classification"] = "ambiguous_worker_status"
        return observation

    try:
        heartbeat_at = _parse_timestamp(
            str(status.get("heartbeat_at") or ""),
            label="worker heartbeat",
        )
        deadline_at = _parse_timestamp(
            str(status.get("deadline_at") or ""),
            label="worker deadline",
        )
    except MaintenanceRestoreJobError:
        observation["classification"] = "ambiguous_worker_metadata"
        return observation
    heartbeat_age = max(
        0.0,
        (captured_at - heartbeat_at).total_seconds(),
    )
    observation.update(
        {
            "heartbeat_at": _format_timestamp(heartbeat_at),
            "heartbeat_age_seconds": round(heartbeat_age, 3),
            "deadline_at": _format_timestamp(deadline_at),
            "deadline_remaining_seconds": round(
                (deadline_at - captured_at).total_seconds(),
                3,
            ),
        }
    )
    if captured_at >= deadline_at:
        observation["classification"] = "stale_deadline_exceeded"
        return observation
    if heartbeat_age <= STALE_HEARTBEAT_SECONDS:
        observation["classification"] = "active_worker"
        observation["worker_active"] = True
        return observation

    properties = dict((unit or {}).get("properties") or {})
    active_state = str(properties.get("ActiveState") or "")
    sub_state = str(properties.get("SubState") or "")
    observation["systemd_active_state"] = active_state
    observation["systemd_sub_state"] = sub_state
    if active_state in {"active", "activating", "reloading"}:
        observation["classification"] = (
            "ambiguous_active_unit_stale_heartbeat"
        )
    elif active_state in {"inactive", "failed", "deactivating"}:
        observation["classification"] = "lost_worker"
    else:
        observation["classification"] = "ambiguous_worker"
    return observation


def _reject_concurrent_request(
    jobs_root: Path,
    *,
    requested_job_id: str,
) -> None:
    for candidate in sorted(jobs_root.iterdir()):
        if (
            candidate.name in {"submit.lock", "worker.lock"}
            or candidate.name == requested_job_id
        ):
            continue
        if candidate.is_symlink() or not candidate.is_dir():
            raise MaintenanceRestoreJobError(
                "restore jobs root contains an unclassified entry"
            )
        _require_job_id(candidate.name)
        request = _read_request(candidate)
        status = _read_status(candidate, request=request)
        if status["status"] not in TERMINAL_STATUSES:
            raise MaintenanceRestoreJobError(
                f"another restore job is non-terminal: {candidate.name}"
            )


def _require_service_continuity(
    value: Mapping[str, Any],
    *,
    expected_fingerprint: str,
    window_id: str,
    plan_fingerprint: str,
) -> dict[str, Any]:
    candidate = dict(value or {})
    boundary_kind = str(candidate.get("boundary_kind") or "")
    services = [
        dict(item)
        for item in candidate.get("services") or []
        if isinstance(item, Mapping)
    ]
    payload = {
        "barrier_window_id": str(
            candidate.get("barrier_window_id") or ""
        ),
        "barrier_plan_fingerprint": str(
            candidate.get("barrier_plan_fingerprint") or ""
        ),
        "hold_started_at": str(candidate.get("hold_started_at") or ""),
        "services": services,
    }
    if boundary_kind:
        payload = {
            "boundary_kind": boundary_kind,
            **payload,
        }
    fingerprint = _require_fingerprint(
        str(candidate.get("fingerprint") or "")
    )
    quiet_confirmed = (
        boundary_kind == QUIET_CONFIRMED_HOLD_CONTINUITY_KIND
    )
    if (
        fingerprint != expected_fingerprint
        or fingerprint != _fingerprint(payload)
        or payload["barrier_window_id"] != window_id
        or payload["barrier_plan_fingerprint"] != plan_fingerprint
        or not payload["hold_started_at"]
        or (quiet_confirmed and bool(services))
        or (not quiet_confirmed and (bool(boundary_kind) or not services))
    ):
        raise MaintenanceRestoreJobError(
            "exact restore continuity fingerprint/boundary mismatch"
        )
    if quiet_confirmed:
        return {
            **payload,
            "fingerprint": fingerprint,
        }
    seen: set[str] = set()
    for service in services:
        unit = str(service.get("unit") or "")
        main_pid = int(service.get("main_pid") or 0)
        started_at = str(service.get("started_at") or "")
        baseline_active_state = str(
            service.get("baseline_active_state") or ""
        )
        if (
            unit in seen
            or not re.fullmatch(r"wb-core-[A-Za-z0-9@_.-]+\.service", unit)
            or main_pid <= 0
            or not started_at
            or not baseline_active_state
        ):
            raise MaintenanceRestoreJobError(
                "exact pre-hold service continuity contains invalid generation"
            )
        seen.add(unit)
    return {
        **payload,
        "fingerprint": fingerprint,
    }


def _read_service_continuity(
    job_dir: Path,
    *,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    evidence = _read_json(
        job_dir / "service-continuity.json",
        label="service continuity evidence",
    )
    expected = dict(request.get("service_continuity") or {})
    validated = _require_service_continuity(
        evidence,
        expected_fingerprint=str(expected.get("fingerprint") or ""),
        window_id=str(request["window_id"]),
        plan_fingerprint=str(request["plan_fingerprint"]),
    )
    if validated != expected:
        raise MaintenanceRestoreJobError(
            "service continuity evidence disagrees with the exact request"
        )
    return validated


def _read_request(job_dir: Path) -> dict[str, Any]:
    request = _read_json(job_dir / "request.json", label="job request")
    job_id = _require_job_id(str(request.get("job_id") or ""))
    window_id = _require_window_id(str(request.get("window_id") or ""))
    plan_fingerprint = _require_fingerprint(
        str(request.get("plan_fingerprint") or "")
    )
    service_continuity_raw = dict(
        request.get("service_continuity") or {}
    )
    service_continuity = _require_service_continuity(
        service_continuity_raw,
        expected_fingerprint=str(
            service_continuity_raw.get("fingerprint") or ""
        ),
        window_id=window_id,
        plan_fingerprint=plan_fingerprint,
    )
    material = {
        "contract_name": CONTRACT_NAME,
        "job_id": job_id,
        "deployed_sha": _require_deployed_sha(
            str(request.get("deployed_sha") or "")
        ),
        "expected_revision": _require_revision(
            int(request.get("expected_revision") or 0)
        ),
        "window_id": window_id,
        "plan_fingerprint": plan_fingerprint,
        "service_continuity": service_continuity,
        "actor": _require_audit_text(
            str(request.get("actor") or ""),
            label="actor",
            maximum=160,
        ),
        "reason": _require_audit_text(
            str(request.get("reason") or ""),
            label="reason",
            maximum=500,
        ),
        "allow_pre_hold_service_continuity": bool(
            request.get("allow_pre_hold_service_continuity")
        ),
        "app_dir": str(request.get("app_dir") or ""),
        "env_file": str(request.get("env_file") or ""),
    }
    if (
        job_id != job_dir.name
        or not material["allow_pre_hold_service_continuity"]
        or request.get("request_digest") != _fingerprint(material)
    ):
        raise MaintenanceRestoreJobError("job request identity/digest mismatch")
    return dict(request)


def _read_status(
    job_dir: Path,
    *,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    status = _read_json(job_dir / "status.json", label="job status")
    value = str(status.get("status") or "")
    if (
        status.get("contract_name") != CONTRACT_NAME
        or status.get("job_id") != request["job_id"]
        or status.get("request_digest") != request["request_digest"]
        or value not in VALID_STATUSES
        or bool(status.get("terminal")) != (value in TERMINAL_STATUSES)
    ):
        raise MaintenanceRestoreJobError("job status identity/state mismatch")
    return dict(status)


def _start_systemd_unit(job_id: str) -> dict[str, Any]:
    unit_name = _systemd_unit_name(job_id)
    completed = subprocess.run(
        ["systemctl", "start", "--no-block", unit_name],
        text=True,
        capture_output=True,
        timeout=30.0,
        check=False,
    )
    if completed.returncode != 0:
        raise MaintenanceRestoreJobError(
            completed.stderr.strip()
            or completed.stdout.strip()
            or f"systemctl start exited {completed.returncode}"
        )
    return {"name": unit_name, "start": "requested"}


def _prove_lock_free(path: Path, *, label: str) -> None:
    if path.is_symlink():
        raise MaintenanceRestoreJobError(
            f"{label} lock must not be a symlink"
        )
    handle = path.open("a+b")
    os.chmod(path, 0o600)
    try:
        try:
            fcntl.flock(
                handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            raise MaintenanceRestoreJobError(
                f"{label} lock is active"
            ) from exc
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _systemd_unit_status(job_id: str) -> dict[str, Any]:
    unit_name = _systemd_unit_name(job_id)
    try:
        completed = subprocess.run(
            [
                "systemctl",
                "show",
                unit_name,
                "--no-pager",
                "--property=LoadState",
                "--property=ActiveState",
                "--property=SubState",
                "--property=Result",
                "--property=NRestarts",
                "--property=ExecMainStatus",
                "--property=MainPID",
                "--property=ExecMainStartTimestamp",
            ],
            text=True,
            capture_output=True,
            timeout=15.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "name": unit_name,
            "status": "unavailable",
            "error": _bounded_message(str(exc)),
        }
    properties = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key] = value
    return {
        "name": unit_name,
        "status": "readback",
        "returncode": completed.returncode,
        "properties": properties,
        "error": _bounded_message(completed.stderr.strip()),
    }


def _systemd_unit_name(job_id: str) -> str:
    return SYSTEMD_UNIT_TEMPLATE.replace("@.", f"@{_require_job_id(job_id)}.")


def _verify_deployed_sha(path: Path, expected: str) -> None:
    actual = path.read_text(encoding="utf-8").strip().lower()
    if actual != expected:
        raise MaintenanceRestoreJobError(
            "deployed SHA marker does not match the exact restore request"
        )


def _jobs_root(runtime_dir: Path, *, create: bool) -> Path:
    path = runtime_dir / JOB_DIRECTORY_NAME
    if path.is_symlink():
        raise MaintenanceRestoreJobError(
            "restore jobs directory must not be a symlink"
        )
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    elif not path.is_dir():
        raise MaintenanceRestoreJobError(
            "restore jobs directory is unavailable"
        )
    return path.resolve()


def _job_directory(
    runtime_dir: Path,
    job_id: str,
    *,
    create: bool,
) -> Path:
    jobs_root = _jobs_root(runtime_dir, create=create)
    path = jobs_root / _require_job_id(job_id)
    if path.is_symlink():
        raise MaintenanceRestoreJobError(
            "restore job directory must not be a symlink"
        )
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
        _fsync_directory(jobs_root)
    elif not path.is_dir():
        raise MaintenanceRestoreJobError("restore job is unavailable")
    resolved = path.resolve()
    if resolved.parent != jobs_root:
        raise MaintenanceRestoreJobError(
            "restore job escaped the durable root"
        )
    return resolved


def _canonical_directory(path: Path, *, label: str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise MaintenanceRestoreJobError(
            f"{label} directory must not be a symlink"
        )
    resolved = candidate.resolve()
    if not resolved.is_dir():
        raise MaintenanceRestoreJobError(f"{label} directory is unavailable")
    return resolved


def _canonical_file(path: Path, *, label: str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise MaintenanceRestoreJobError(
            f"{label} file must not be a symlink"
        )
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise MaintenanceRestoreJobError(f"{label} file is unavailable")
    return resolved


class _exclusive_lock:
    def __init__(self, path: Path):
        self.path = path
        self.handle: Any | None = None

    def __enter__(self) -> Any:
        if self.path.is_symlink():
            raise MaintenanceRestoreJobError(
                "restore job lock must not be a symlink"
            )
        self.handle = self.path.open("a+b")
        os.chmod(self.path, 0o600)
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self.handle

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        assert self.handle is not None
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise MaintenanceRestoreJobError(f"{label} is unavailable")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaintenanceRestoreJobError(f"{label} is invalid") from exc
    if not isinstance(payload, dict):
        raise MaintenanceRestoreJobError(f"{label} must be a JSON object")
    return payload


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    if path.parent.is_symlink():
        raise MaintenanceRestoreJobError(
            "restore job directory must not be a symlink"
        )
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    try:
        with temporary.open("xb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _append_audit(job_dir: Path, payload: Mapping[str, Any]) -> None:
    path = job_dir / "audit.jsonl"
    if path.is_symlink():
        raise MaintenanceRestoreJobError(
            "restore job audit must not be a symlink"
        )
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    with path.open("ab") as handle:
        os.chmod(path, 0o600)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(job_dir)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_job_id(value: str) -> str:
    job_id = str(value or "").strip().lower()
    if not JOB_ID_PATTERN.fullmatch(job_id):
        raise MaintenanceRestoreJobError(
            "job id must be exactly 64 lowercase hex characters"
        )
    return job_id


def _require_deployed_sha(value: str) -> str:
    deployed_sha = str(value or "").strip().lower()
    if not DEPLOYED_SHA_PATTERN.fullmatch(deployed_sha):
        raise MaintenanceRestoreJobError(
            "restore job requires an exact 40-hex deployed SHA"
        )
    return deployed_sha


def _require_revision(value: int) -> int:
    revision = int(value)
    if revision < 0:
        raise MaintenanceRestoreJobError(
            "expected policy revision must be non-negative"
        )
    return revision


def _require_window_id(value: str) -> str:
    window_id = str(value or "").strip()
    if not WINDOW_ID_PATTERN.fullmatch(window_id):
        raise MaintenanceRestoreJobError("restore window id is invalid")
    return window_id


def _require_fingerprint(value: str) -> str:
    fingerprint = str(value or "").strip().lower()
    if not FINGERPRINT_PATTERN.fullmatch(fingerprint):
        raise MaintenanceRestoreJobError(
            "restore job requires an exact sha256 fingerprint"
        )
    return fingerprint


def _require_audit_text(value: str, *, label: str, maximum: int) -> str:
    text = str(value or "").strip()
    if (
        not text
        or len(text) > maximum
        or any(ord(character) < 32 for character in text)
    ):
        raise MaintenanceRestoreJobError(f"restore {label} is invalid")
    return text


def _error_record(*, code: str, exc: Exception) -> dict[str, str]:
    return {
        "code": code,
        "type": type(exc).__name__,
        "message": _bounded_message(str(exc)),
    }


def _bounded_message(value: str, *, maximum: int = 2000) -> str:
    return str(value or "").replace("\x00", "")[:maximum]


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _parse_timestamp(value: str, *, label: str) -> datetime:
    raw = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MaintenanceRestoreJobError(
            f"{label} timestamp is invalid"
        ) from exc
    if parsed.tzinfo is None:
        raise MaintenanceRestoreJobError(
            f"{label} timestamp must include a timezone"
        )
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _now() -> str:
    return _format_timestamp(datetime.now(timezone.utc))


def run(args: argparse.Namespace) -> dict[str, Any]:
    runtime_dir = Path(str(args.runtime_dir))
    app_dir = Path(str(args.app_dir))
    env_file = Path(str(args.env_file))
    deployed_sha_file = Path(str(args.deployed_sha_file))
    if args.command == "submit":
        return submit_job(
            runtime_dir=runtime_dir,
            app_dir=app_dir,
            env_file=env_file,
            deployed_sha_file=deployed_sha_file,
            job_id=str(args.job_id),
            deployed_sha=str(args.deployed_sha),
            expected_revision=int(args.expected_revision),
            window_id=str(args.window_id),
            plan_fingerprint=str(args.plan_fingerprint),
            service_continuity_fingerprint=str(
                args.service_continuity_fingerprint
            ),
            actor=str(args.actor),
            reason=str(args.reason),
            allow_pre_hold_service_continuity=bool(
                args.allow_pre_hold_service_continuity
            ),
        )
    if args.command == "status":
        return job_status(
            runtime_dir=runtime_dir,
            job_id=str(args.job_id),
            deployed_sha=str(args.deployed_sha),
        )
    if args.command == "resume":
        return resume_failed_job(
            runtime_dir=runtime_dir,
            app_dir=app_dir,
            env_file=env_file,
            deployed_sha_file=deployed_sha_file,
            job_id=str(args.job_id),
            deployed_sha=str(args.deployed_sha),
            expected_failure_digest=str(args.expected_failure_digest),
            service_continuity_fingerprint=str(
                args.service_continuity_fingerprint
            ),
            actor=str(args.actor),
            reason=str(args.reason),
        )
    if args.command == "worker":
        return run_worker(
            runtime_dir=runtime_dir,
            app_dir=app_dir,
            env_file=env_file,
            deployed_sha_file=deployed_sha_file,
            job_id=str(args.job_id),
        )
    raise MaintenanceRestoreJobError("unsupported restore job command")


def main() -> int:
    args = build_parser().parse_args()
    try:
        payload = run(args)
    except (MaintenanceRestoreJobError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "contract_name": CONTRACT_NAME,
                    "status": "error",
                    "error": {
                        "type": type(exc).__name__,
                        "message": _bounded_message(str(exc)),
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
