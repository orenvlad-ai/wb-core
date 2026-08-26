#!/usr/bin/env python3
"""Durable detached submit/status worker for exact storage sanitation jobs."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sqlite_backup_archive import DEFAULT_RESERVED_FREE_BYTES  # noqa: E402
from apps.storage_recovery_sanitation import (  # noqa: E402
    FAMILY_POLICIES,
    SanitationError,
    _canonical_roots,
    _resolve_family,
    _verify_deployed_sha,
    apply_family,
    plan_family,
)


CONTRACT_NAME = "storage_recovery_sanitation_job_v1"
JOB_DIRECTORY_NAME = "storage-recovery-sanitation-jobs"
SYSTEMD_UNIT_TEMPLATE = "wb-core-storage-recovery-sanitation@.service"
JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
TERMINAL_STATUSES = frozenset({"succeeded", "failed"})


class SanitationJobError(RuntimeError):
    """Fail-closed detached sanitation job contract violation."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-dir",
        default="/opt/wb-core-runtime/state",
        help="Canonical runtime state directory.",
    )
    parser.add_argument(
        "--root-backups",
        default="/opt/wb-core-runtime/backups",
        help="Canonical root-filesystem backup directory.",
    )
    parser.add_argument(
        "--deployed-sha-file",
        default=str(ROOT / ".wb-core-runtime-sha"),
        help="Canonical deployed SHA marker.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    submit = subparsers.add_parser(
        "submit",
        help="Persist one exact request and start its fixed detached unit.",
    )
    submit.add_argument("--job-id", required=True)
    submit.add_argument("--deployed-sha", required=True)
    submit.add_argument(
        "--operation",
        choices=("plan", "apply", "warm-archive-apply"),
        required=True,
    )
    submit.add_argument("--root", dest="root_name", choices=("root", "backup"))
    submit.add_argument("--family", default="")
    submit.add_argument("--fingerprint", default="")
    submit.add_argument("--manifest", default="")
    submit.add_argument("--manifest-sha256", default="")
    submit.add_argument("--goal-operation-id", default="")
    submit.add_argument("--approval-reference", default="")
    submit.add_argument(
        "--reserved-free-bytes",
        type=int,
        default=DEFAULT_RESERVED_FREE_BYTES,
    )

    status = subparsers.add_parser(
        "status",
        help="Read one durable request/status/result without mutation.",
    )
    status.add_argument("--job-id", required=True)
    status.add_argument("--deployed-sha", required=True)

    worker = subparsers.add_parser(
        "worker",
        help="Run one persisted exact request inside the fixed systemd unit.",
    )
    worker.add_argument("--job-id", required=True)
    return parser


def submit_job(
    *,
    runtime_dir: Path,
    root_backups: Path,
    deployed_sha_file: Path,
    job_id: str,
    deployed_sha: str,
    operation: str,
    root_name: str,
    family: str,
    fingerprint: str = "",
    manifest: str = "",
    manifest_sha256: str = "",
    goal_operation_id: str = "",
    approval_reference: str = "",
    reserved_free_bytes: int = DEFAULT_RESERVED_FREE_BYTES,
    starter: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Persist and start one caller-known job idempotently."""

    job_id = _require_job_id(job_id)
    deployed_sha = _require_deployed_sha(deployed_sha)
    operation = str(operation or "").strip()
    if operation not in {"plan", "apply", "warm-archive-apply"}:
        raise SanitationJobError("unsupported sanitation job operation")
    if int(reserved_free_bytes) < 0:
        raise SanitationJobError("reserved free bytes must be non-negative")
    approved = str(fingerprint or "").strip()
    if operation in {"plan", "apply"}:
        if root_name not in FAMILY_POLICIES or family not in FAMILY_POLICIES[root_name]:
            raise SanitationJobError(
                "sanitation job family is outside the exact allowlist"
            )
    if operation == "apply":
        if not FINGERPRINT_PATTERN.fullmatch(approved):
            raise SanitationJobError("apply job requires an exact sanitation fingerprint")
    elif operation == "plan" and approved:
        raise SanitationJobError("plan job must not carry an apply fingerprint")
    if operation == "warm-archive-apply":
        if root_name or family or approved or int(reserved_free_bytes) != DEFAULT_RESERVED_FREE_BYTES:
            raise SanitationJobError(
                "warm archive job must not carry generic family inputs"
            )
        if (
            re.fullmatch(
                r"/opt/wb-core-runtime/state/private-evidence/production-goals/"
                r"production-goal-v1-[0-9a-f]{32}/"
                r"root-warm-archive-plan-[0-9]{8}T[0-9]{6}Z(?:-[0-9]+)?\.json",
                str(manifest or ""),
            )
            is None
            or not FINGERPRINT_PATTERN.fullmatch(str(manifest_sha256 or ""))
            or re.fullmatch(
                r"production-goal-v1-[0-9a-f]{32}",
                str(goal_operation_id or ""),
            )
            is None
            or not str(approval_reference or "")
            or len(str(approval_reference)) > 500
        ):
            raise SanitationJobError("warm archive exact request binding is invalid")

    runtime_dir = _canonical_directory(runtime_dir, label="runtime")
    root_backups = _canonical_directory(root_backups, label="root backup")
    if operation in {"plan", "apply"}:
        roots = _canonical_roots(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
        )
        _resolve_family(
            roots=roots,
            root_name=root_name,
            family=family,
        )
    _verify_deployed_sha(
        deployed_sha=deployed_sha,
        deployed_sha_file=deployed_sha_file,
    )

    request_material: dict[str, Any] = {
        "contract_name": CONTRACT_NAME,
        "job_id": job_id,
        "deployed_sha": deployed_sha,
        "operation": operation,
    }
    if operation == "warm-archive-apply":
        request_material.update(
            {
                "manifest": str(manifest),
                "manifest_sha256": str(manifest_sha256),
                "goal_operation_id": str(goal_operation_id),
                "approval_reference": str(approval_reference),
            }
        )
    else:
        request_material.update(
            {
                "root": root_name,
                "family": family,
                "fingerprint": approved,
                "reserved_free_bytes": int(reserved_free_bytes),
            }
        )
    request = {
        **request_material,
        "request_digest": _fingerprint(request_material),
        "created_at": _now(),
    }
    job_dir = _job_directory(runtime_dir, job_id, create=True)
    lock_path = job_dir / "job.lock"
    with _exclusive_lock(lock_path):
        request_path = job_dir / "request.json"
        existing_request = request_path.exists()
        if request_path.exists():
            stored = _read_request(job_dir)
            if stored["request_digest"] != request["request_digest"]:
                raise SanitationJobError(
                    "job id is already bound to a different exact request"
                )
            request = stored
        else:
            _atomic_write_json(request_path, request)
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
        try:
            unit = (starter or _start_systemd_unit)(job_id)
        except Exception as exc:
            failed_start = {
                **current,
                "status": "start_failed",
                "terminal": False,
                "retryable": True,
                "error": {
                    "code": "systemd_start_failed",
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
                "updated_at": _now(),
            }
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


def job_status(
    *,
    runtime_dir: Path,
    job_id: str,
    deployed_sha: str,
    include_systemd: bool = True,
) -> dict[str, Any]:
    """Read one job without creating or changing any job state."""

    return _status_report(
        runtime_dir=_canonical_directory(runtime_dir, label="runtime"),
        job_id=_require_job_id(job_id),
        expected_deployed_sha=_require_deployed_sha(deployed_sha),
        include_systemd=include_systemd,
    )


def run_worker(
    *,
    runtime_dir: Path,
    root_backups: Path,
    deployed_sha_file: Path,
    job_id: str,
    executor: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute or crash-resume one exact persisted request."""

    job_id = _require_job_id(job_id)
    runtime_dir = _canonical_directory(runtime_dir, label="runtime")
    root_backups = _canonical_directory(root_backups, label="root backup")
    job_dir = _job_directory(runtime_dir, job_id, create=False)
    request = _read_request(job_dir)
    current = _read_status(job_dir, request=request)
    if current["status"] in TERMINAL_STATUSES:
        return _status_report(
            runtime_dir=runtime_dir,
            job_id=job_id,
            expected_deployed_sha=request["deployed_sha"],
            include_systemd=False,
        )

    jobs_root = job_dir.parent
    global_lock = jobs_root / "worker.lock"
    global_handle = global_lock.open("a+b")
    os.chmod(global_lock, 0o600)
    try:
        try:
            fcntl.flock(global_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _finish_failed(
                job_dir=job_dir,
                request=request,
                status=current,
                code="another_sanitation_job_active",
                error_type="SanitationJobError",
                message="another detached sanitation worker holds the exact global lock",
            )

        _verify_deployed_sha(
            deployed_sha=request["deployed_sha"],
            deployed_sha_file=deployed_sha_file,
        )
        with _exclusive_lock(job_dir / "job.lock"):
            current = _read_status(job_dir, request=request)
            if current["status"] in TERMINAL_STATUSES:
                return _status_report(
                    runtime_dir=runtime_dir,
                    job_id=job_id,
                    expected_deployed_sha=request["deployed_sha"],
                    include_systemd=False,
                )
            running = {
                **current,
                "status": "running",
                "terminal": False,
                "attempt": int(current.get("attempt") or 0) + 1,
                "worker_pid": os.getpid(),
                "started_at": str(current.get("started_at") or _now()),
                "updated_at": _now(),
            }
            running.pop("error", None)
            _atomic_write_json(job_dir / "status.json", running)

        try:
            result = (
                executor(request)
                if executor is not None
                else _execute_request(
                    request=request,
                    runtime_dir=runtime_dir,
                    root_backups=root_backups,
                    deployed_sha_file=deployed_sha_file,
                )
            )
            _verify_deployed_sha(
                deployed_sha=request["deployed_sha"],
                deployed_sha_file=deployed_sha_file,
            )
        except Exception as exc:
            return _finish_failed(
                job_dir=job_dir,
                request=request,
                status=running,
                code="sanitation_runner_failed",
                error_type=type(exc).__name__,
                message=str(exc),
                evidence=(
                    dict(exc.evidence)
                    if isinstance(getattr(exc, "evidence", None), dict)
                    else None
                ),
            )

        result_record = {
            "contract_name": CONTRACT_NAME,
            "job_id": job_id,
            "request_digest": request["request_digest"],
            "completed_at": _now(),
            "result": result,
        }
        result_record["result_digest"] = _fingerprint(result_record["result"])
        _atomic_write_json(job_dir / "result.json", result_record)
        succeeded = {
            **running,
            "status": "succeeded",
            "terminal": True,
            "completed_at": result_record["completed_at"],
            "result_digest": result_record["result_digest"],
            "updated_at": _now(),
        }
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


def _execute_request(
    *,
    request: dict[str, Any],
    runtime_dir: Path,
    root_backups: Path,
    deployed_sha_file: Path,
) -> dict[str, Any]:
    if request["operation"] == "plan":
        return plan_family(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            root_name=request["root"],
            family=request["family"],
            reserved_free_bytes=int(request["reserved_free_bytes"]),
        )
    if request["operation"] == "apply":
        return apply_family(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            root_name=request["root"],
            family=request["family"],
            fingerprint=request["fingerprint"],
            deployed_sha=request["deployed_sha"],
            deployed_sha_file=deployed_sha_file,
            reserved_free_bytes=int(request["reserved_free_bytes"]),
        )
    if request["operation"] == "warm-archive-apply":
        from apps.root_storage_warm_archive import apply_batch

        evidence_dir = Path(str(request["manifest"])).parent
        return apply_batch(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            deployed_sha=request["deployed_sha"],
            deployed_sha_file=deployed_sha_file,
            evidence_dir=evidence_dir,
            operation_id=request["goal_operation_id"],
            manifest_path=Path(str(request["manifest"])),
            manifest_sha256=request["manifest_sha256"],
            approval_reference=request["approval_reference"],
            own_job_id=request["job_id"],
        )
    raise SanitationJobError("persisted sanitation operation is invalid")


def _finish_failed(
    *,
    job_dir: Path,
    request: dict[str, Any],
    status: dict[str, Any],
    code: str,
    error_type: str,
    message: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error = {
        "code": code,
        "type": error_type,
        "message": message,
    }
    if evidence:
        error["evidence"] = evidence
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
        **status,
        "status": "failed",
        "terminal": True,
        "error": error,
        "completed_at": result_record["completed_at"],
        "result_digest": result_record["result_digest"],
        "updated_at": _now(),
    }
    _atomic_write_json(job_dir / "status.json", failed)
    return {
        **failed,
        "request": request,
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
    if request["deployed_sha"] != expected_deployed_sha:
        raise SanitationJobError("job deployed SHA does not match status request")
    status = _read_status(job_dir, request=request)
    report = {
        **status,
        "request": request,
    }
    result_path = job_dir / "result.json"
    if result_path.exists():
        result_record = _read_json(result_path, label="result")
        if (
            result_record.get("contract_name") != CONTRACT_NAME
            or result_record.get("job_id") != job_id
            or result_record.get("request_digest") != request["request_digest"]
        ):
            raise SanitationJobError("job result identity mismatch")
        material = result_record.get("result", result_record.get("error"))
        if result_record.get("result_digest") != _fingerprint(material):
            raise SanitationJobError("job result digest mismatch")
        report["result_record"] = result_record
        if "result" in result_record:
            report["result"] = result_record["result"]
    if include_systemd:
        report["unit"] = _systemd_unit_status(job_id)
    return report


def _read_request(job_dir: Path) -> dict[str, Any]:
    request = _read_json(job_dir / "request.json", label="request")
    if request.get("contract_name") != CONTRACT_NAME:
        raise SanitationJobError("job request contract mismatch")
    job_id = _require_job_id(str(request.get("job_id") or ""))
    if job_id != job_dir.name:
        raise SanitationJobError("job request id/path mismatch")
    operation = str(request.get("operation") or "")
    material: dict[str, Any] = {
        "contract_name": CONTRACT_NAME,
        "job_id": job_id,
        "deployed_sha": _require_deployed_sha(
            str(request.get("deployed_sha") or "")
        ),
        "operation": operation,
    }
    if operation == "warm-archive-apply":
        material.update(
            {
                "manifest": str(request.get("manifest") or ""),
                "manifest_sha256": str(request.get("manifest_sha256") or ""),
                "goal_operation_id": str(request.get("goal_operation_id") or ""),
                "approval_reference": str(request.get("approval_reference") or ""),
            }
        )
        if (
            re.fullmatch(
                r"/opt/wb-core-runtime/state/private-evidence/production-goals/"
                r"production-goal-v1-[0-9a-f]{32}/"
                r"root-warm-archive-plan-[0-9]{8}T[0-9]{6}Z(?:-[0-9]+)?\.json",
                material["manifest"],
            )
            is None
            or not FINGERPRINT_PATTERN.fullmatch(material["manifest_sha256"])
            or re.fullmatch(
                r"production-goal-v1-[0-9a-f]{32}",
                material["goal_operation_id"],
            )
            is None
            or not material["approval_reference"]
            or len(material["approval_reference"]) > 500
        ):
            raise SanitationJobError("persisted warm archive request is invalid")
    else:
        material.update(
            {
                "root": str(request.get("root") or ""),
                "family": str(request.get("family") or ""),
                "fingerprint": str(request.get("fingerprint") or ""),
                "reserved_free_bytes": int(request.get("reserved_free_bytes") or 0),
            }
        )
    if operation not in {"plan", "apply", "warm-archive-apply"}:
        raise SanitationJobError("persisted sanitation operation is invalid")
    if operation in {"plan", "apply"} and (
        material["root"] not in FAMILY_POLICIES
        or material["family"] not in FAMILY_POLICIES[material["root"]]
    ):
        raise SanitationJobError("persisted sanitation family is outside allowlist")
    if operation == "apply":
        if not FINGERPRINT_PATTERN.fullmatch(material["fingerprint"]):
            raise SanitationJobError("persisted apply fingerprint is invalid")
    elif operation == "plan" and material["fingerprint"]:
        raise SanitationJobError("persisted plan has an unexpected fingerprint")
    if request.get("request_digest") != _fingerprint(material):
        raise SanitationJobError("job request digest mismatch")
    return dict(request)


def _read_status(
    job_dir: Path,
    *,
    request: dict[str, Any],
) -> dict[str, Any]:
    status = _read_json(job_dir / "status.json", label="status")
    if (
        status.get("contract_name") != CONTRACT_NAME
        or status.get("job_id") != request["job_id"]
        or status.get("request_digest") != request["request_digest"]
    ):
        raise SanitationJobError("job status identity mismatch")
    value = str(status.get("status") or "")
    if value not in {
        "queued",
        "start_failed",
        "running",
        "succeeded",
        "failed",
    }:
        raise SanitationJobError("job status value is invalid")
    if bool(status.get("terminal")) != (value in TERMINAL_STATUSES):
        raise SanitationJobError("job terminal status is inconsistent")
    return dict(status)


def _start_systemd_unit(job_id: str) -> dict[str, Any]:
    unit_name = _systemd_unit_name(job_id)
    completed = subprocess.run(
        ["systemctl", "start", "--no-block", unit_name],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise SanitationJobError(
            completed.stderr.strip()
            or completed.stdout.strip()
            or f"systemctl start exited {completed.returncode}"
        )
    return {
        "name": unit_name,
        "start": "requested",
    }


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
            ],
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "name": unit_name,
            "status": "unavailable",
            "error": str(exc),
        }
    values = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return {
        "name": unit_name,
        "status": "readback",
        "returncode": completed.returncode,
        "properties": values,
        "error": completed.stderr.strip(),
    }


def _systemd_unit_name(job_id: str) -> str:
    return SYSTEMD_UNIT_TEMPLATE.replace("@.", f"@{_require_job_id(job_id)}.")


def _canonical_directory(path: Path, *, label: str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise SanitationJobError(f"{label} directory must not be a symlink")
    resolved = candidate.resolve()
    if not resolved.is_dir():
        raise SanitationJobError(f"{label} directory is unavailable")
    return resolved


def _job_directory(runtime_dir: Path, job_id: str, *, create: bool) -> Path:
    job_id = _require_job_id(job_id)
    jobs_root = runtime_dir / JOB_DIRECTORY_NAME
    if jobs_root.is_symlink():
        raise SanitationJobError("sanitation jobs directory must not be a symlink")
    if create:
        jobs_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(jobs_root, 0o700)
    elif not jobs_root.is_dir():
        raise SanitationJobError("sanitation jobs directory is unavailable")
    job_dir = jobs_root / job_id
    if job_dir.is_symlink():
        raise SanitationJobError("sanitation job directory must not be a symlink")
    if create:
        job_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(job_dir, 0o700)
        _fsync_directory(jobs_root)
    elif not job_dir.is_dir():
        raise SanitationJobError("sanitation job is unavailable")
    if job_dir.resolve().parent != jobs_root.resolve():
        raise SanitationJobError("sanitation job escaped the durable root")
    return job_dir.resolve()


class _exclusive_lock:
    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        if self.path.is_symlink():
            raise SanitationJobError("sanitation job lock must not be a symlink")
        self.handle = self.path.open("a+b")
        os.chmod(self.path, 0o600)
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self.handle

    def __exit__(self, exc_type, exc, traceback):
        assert self.handle is not None
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise SanitationJobError(f"sanitation job {label} is unavailable")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SanitationJobError(f"sanitation job {label} is invalid") from exc
    if not isinstance(payload, dict):
        raise SanitationJobError(f"sanitation job {label} must be a JSON object")
    return payload


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    if path.parent.is_symlink():
        raise SanitationJobError("sanitation job directory must not be a symlink")
    data = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    temp = path.with_name(path.name + f".tmp-{os.getpid()}")
    try:
        with temp.open("xb") as handle:
            os.chmod(temp, 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        _fsync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_job_id(value: str) -> str:
    job_id = str(value or "").strip().lower()
    if not JOB_ID_PATTERN.fullmatch(job_id):
        raise SanitationJobError("job id must be exactly 64 lowercase hex characters")
    return job_id


def _require_deployed_sha(value: str) -> str:
    deployed_sha = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", deployed_sha):
        raise SanitationJobError("job requires an exact 40-hex deployed SHA")
    return deployed_sha


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00",
        "Z",
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    runtime_dir = Path(str(args.runtime_dir))
    root_backups = Path(str(args.root_backups))
    deployed_sha_file = Path(str(args.deployed_sha_file))
    if args.command == "submit":
        return submit_job(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            deployed_sha_file=deployed_sha_file,
            job_id=str(args.job_id),
            deployed_sha=str(args.deployed_sha),
            operation=str(args.operation),
            root_name=str(args.root_name or ""),
            family=str(args.family),
            fingerprint=str(args.fingerprint),
            manifest=str(args.manifest),
            manifest_sha256=str(args.manifest_sha256),
            goal_operation_id=str(args.goal_operation_id),
            approval_reference=str(args.approval_reference),
            reserved_free_bytes=int(args.reserved_free_bytes),
        )
    if args.command == "status":
        return job_status(
            runtime_dir=runtime_dir,
            job_id=str(args.job_id),
            deployed_sha=str(args.deployed_sha),
        )
    if args.command == "worker":
        return run_worker(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            deployed_sha_file=deployed_sha_file,
            job_id=str(args.job_id),
        )
    raise SanitationJobError("unsupported sanitation job command")


def main() -> int:
    args = build_parser().parse_args()
    try:
        payload = run(args)
    except (SanitationJobError, SanitationError, ValueError, OSError) as exc:
        print(
            json.dumps(
                {
                    "contract_name": CONTRACT_NAME,
                    "status": "error",
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
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
