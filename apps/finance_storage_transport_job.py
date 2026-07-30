#!/usr/bin/env python3
"""Durable transport boundary for one exact Finance storage hold mutation."""

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
import tempfile
import time
from typing import Any, Mapping


CONTRACT_NAME = "wb_core_finance_storage_transport_job_v1"
REQUEST_CONTRACT = "wb_core_finance_storage_transport_request_v1"
JOB_ID_RE = re.compile(r"[0-9a-f]{64}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
IDENTITY_RE = re.compile(r"sha256:[0-9a-f]{64}")
ALLOWED_ACTIONS = frozenset(
    {"snapshot-create", "cutover-apply", "rollback-apply"}
)
TERMINAL_STATUSES = frozenset({"succeeded", "failed"})
MAX_TIMEOUT_SECONDS = 43_200


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical_json(value).encode("utf-8")
    ).hexdigest()


def _stable_runner_args(runner_args: list[Any]) -> list[str]:
    stable: list[str] = []
    replace_next = False
    for raw_item in runner_args:
        item = str(raw_item)
        if replace_next:
            stable.append("<fresh-deploy-lease-transport>")
            replace_next = False
            continue
        stable.append(item)
        if item == "--deploy-lease-json":
            replace_next = True
    if replace_next:
        raise ValueError(
            "Finance transport runner args lost deploy-lease value"
        )
    return stable


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=path.name + ".",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        json.dump(
            dict(payload),
            handle,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is unreadable") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} must contain a JSON object")
    return payload


def _job_root(runtime_dir: Path) -> Path:
    return runtime_dir.resolve() / "finance-storage-transport-jobs"


def _job_dir(runtime_dir: Path, job_id: str) -> Path:
    if JOB_ID_RE.fullmatch(str(job_id or "")) is None:
        raise ValueError("Finance transport job id must be exact 64-hex")
    return _job_root(runtime_dir) / job_id


def _deployed_sha(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip().lower()
    except OSError as exc:
        raise RuntimeError("Finance transport deployed SHA is unreadable") from exc
    if SHA_RE.fullmatch(value) is None:
        raise RuntimeError("Finance transport deployed SHA is invalid")
    return value


def _pid_matches(pid: int, *, job_id: str) -> bool:
    if pid <= 0:
        return False
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    try:
        cmdline = (
            proc_root / str(pid) / "cmdline"
        ).read_bytes().replace(b"\0", b" ").decode(
            "utf-8",
            errors="replace",
        )
    except OSError:
        return False
    return (
        "finance_storage_transport_job.py" in cmdline
        and "worker" in cmdline
        and job_id in cmdline
    )


def _validated_request(
    payload: Mapping[str, Any],
    *,
    job_id: str,
    deployed_sha: str,
) -> dict[str, Any]:
    request = dict(payload)
    identity = dict(request.get("identity") or {})
    runner_args = request.get("runner_args")
    timeout_seconds = int(request.get("timeout_seconds") or 0)
    action = str(request.get("action") or "")
    repo_root = Path(str(request.get("repo_root") or "")).resolve()
    stdin_text = request.get("stdin_text")
    if (
        str(request.get("contract_name") or "") != REQUEST_CONTRACT
        or str(request.get("job_id") or "") != job_id
        or str(request.get("deployed_sha") or "") != deployed_sha
        or action not in ALLOWED_ACTIONS
        or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS
        or not isinstance(runner_args, list)
        or len(runner_args) < 3
        or Path(str(runner_args[0])).name not in {"python3", "python"}
        or str(runner_args[1]) != "apps/finance_storage_split.py"
        or str(runner_args[2]) != action
        or not repo_root.is_dir()
        or not (repo_root / "apps" / "finance_storage_split.py").is_file()
        or (stdin_text is not None and not isinstance(stdin_text, str))
    ):
        raise ValueError("Finance transport request contract is invalid")
    request_identity = str(request.get("request_identity") or "")
    identity_without_job = {
        key: value
        for key, value in identity.items()
        if key != "job_id"
    }
    expected_job_id = hashlib.sha256(
        _canonical_json(identity_without_job).encode("utf-8")
    ).hexdigest()
    stdin_sha256 = (
        "sha256:"
        + hashlib.sha256(
            str(stdin_text or "").encode("utf-8")
        ).hexdigest()
    )
    if (
        IDENTITY_RE.fullmatch(request_identity) is None
        or _digest(identity) != request_identity
        or expected_job_id != job_id
        or str(identity.get("job_id") or "") != job_id
        or str(identity.get("deployed_sha") or "") != deployed_sha
        or str(identity.get("action") or "") != action
        or identity.get("runner_args")
        != _stable_runner_args(runner_args)
        or str(identity.get("stdin_sha256") or "") != stdin_sha256
        or int(identity.get("timeout_seconds") or 0)
        != timeout_seconds
    ):
        raise ValueError("Finance transport request identity is invalid")
    return request


def _status_payload(
    runtime_dir: Path,
    *,
    job_id: str,
    deployed_sha: str,
) -> dict[str, Any]:
    directory = _job_dir(runtime_dir, job_id)
    if not directory.is_dir():
        return {
            "contract_name": CONTRACT_NAME,
            "job_id": job_id,
            "deployed_sha": deployed_sha,
            "status": "absent",
            "terminal": False,
            "worker_classification": "absent",
        }
    request = _load_json(directory / "request.json", label="transport request")
    if str(request.get("deployed_sha") or "") != deployed_sha:
        raise RuntimeError("Finance transport job deployed SHA drifted")
    status = _load_json(directory / "status.json", label="transport status")
    state = str(status.get("status") or "")
    pid = int(status.get("pid") or 0)
    if state in TERMINAL_STATUSES:
        classification = "terminal_" + state
        terminal = True
    elif state in {"queued", "running"} and _pid_matches(
        pid,
        job_id=job_id,
    ):
        classification = "active_worker"
        terminal = False
        state = "running"
    else:
        classification = "lost_worker"
        terminal = False
        state = "ambiguous"
    result = (
        _load_json(directory / "result.json", label="transport result")
        if (directory / "result.json").is_file()
        else None
    )
    return {
        "contract_name": CONTRACT_NAME,
        "job_id": job_id,
        "deployed_sha": deployed_sha,
        "request_identity": request.get("request_identity"),
        "action": request.get("action"),
        "status": state,
        "terminal": terminal,
        "worker_classification": classification,
        "pid": pid,
        "created_at": status.get("created_at"),
        "started_at": status.get("started_at"),
        "completed_at": status.get("completed_at"),
        "exit_code": status.get("exit_code"),
        "error": status.get("error"),
        "result": result,
    }


def submit_job(
    runtime_dir: Path,
    *,
    job_id: str,
    deployed_sha_file: Path,
    request_payload: Mapping[str, Any],
) -> dict[str, Any]:
    runtime = runtime_dir.resolve()
    deployed_sha = _deployed_sha(deployed_sha_file)
    request = _validated_request(
        request_payload,
        job_id=job_id,
        deployed_sha=deployed_sha,
    )
    root = _job_root(runtime)
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    submit_lock = root / "submit.lock"
    with submit_lock.open("a+b") as lock_handle:
        os.chmod(submit_lock, 0o600)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        directory = _job_dir(runtime, job_id)
        if directory.exists():
            existing = _load_json(
                directory / "request.json",
                label="existing transport request",
            )
            if str(existing.get("request_identity") or "") != str(
                request.get("request_identity") or ""
            ):
                raise RuntimeError(
                    "Finance transport job belongs to a different request"
                )
            return _status_payload(
                runtime,
                job_id=job_id,
                deployed_sha=deployed_sha,
            )
        directory.mkdir(mode=0o700)
        _write_json(directory / "request.json", request)
        _write_json(
            directory / "status.json",
            {
                "status": "queued",
                "pid": 0,
                "created_at": _utc_now(),
            },
        )
        worker = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "worker",
                "--runtime-dir",
                str(runtime),
                "--job-id",
                job_id,
                "--deployed-sha-file",
                str(deployed_sha_file.resolve()),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        _write_json(
            directory / "status.json",
            {
                "status": "running",
                "pid": int(worker.pid),
                "created_at": _utc_now(),
                "started_at": _utc_now(),
            },
        )
        for _ in range(100):
            if _pid_matches(int(worker.pid), job_id=job_id):
                break
            if worker.poll() is not None:
                break
            time.sleep(0.01)
    return _status_payload(
        runtime,
        job_id=job_id,
        deployed_sha=deployed_sha,
    )


def run_worker(
    runtime_dir: Path,
    *,
    job_id: str,
    deployed_sha_file: Path,
) -> int:
    runtime = runtime_dir.resolve()
    deployed_sha = _deployed_sha(deployed_sha_file)
    directory = _job_dir(runtime, job_id)
    request = _load_json(directory / "request.json", label="transport request")
    request = _validated_request(
        request,
        job_id=job_id,
        deployed_sha=deployed_sha,
    )
    status = _load_json(directory / "status.json", label="transport status")
    worker_ready_deadline = time.monotonic() + 5.0
    while (
        int(status.get("pid") or 0) != os.getpid()
        or str(status.get("status") or "") != "running"
    ):
        if time.monotonic() >= worker_ready_deadline:
            raise RuntimeError(
                "Finance transport worker start handoff is ambiguous"
            )
        time.sleep(0.01)
        status = _load_json(
            directory / "status.json",
            label="transport status",
        )
    started_at = str(status.get("started_at") or _utc_now())
    runner_args = [str(item) for item in request["runner_args"]]
    try:
        completed = subprocess.run(
            runner_args,
            cwd=Path(str(request["repo_root"])).resolve(),
            text=True,
            input=request.get("stdin_text"),
            capture_output=True,
            timeout=int(request["timeout_seconds"]),
            check=False,
        )
        stdout_lines = [
            line
            for line in completed.stdout.splitlines()
            if line.strip()
        ]
        result_payload: dict[str, Any] | None = None
        if completed.returncode == 0:
            try:
                parsed = json.loads(
                    stdout_lines[-1] if stdout_lines else ""
                )
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    "Finance transport worker returned invalid JSON"
                ) from exc
            if not isinstance(parsed, dict):
                raise RuntimeError(
                    "Finance transport worker returned non-object JSON"
                )
            result_payload = parsed
            _write_json(directory / "result.json", result_payload)
        error = (
            ""
            if completed.returncode == 0
            else (
                completed.stderr.strip()
                or completed.stdout.strip()
                or f"exit {completed.returncode}"
            )[:16_000]
        )
        _write_json(
            directory / "status.json",
            {
                "status": (
                    "succeeded"
                    if completed.returncode == 0
                    else "failed"
                ),
                "pid": os.getpid(),
                "created_at": status.get("created_at"),
                "started_at": started_at,
                "completed_at": _utc_now(),
                "exit_code": int(completed.returncode),
                "error": error,
            },
        )
        return int(completed.returncode != 0)
    except Exception as exc:
        _write_json(
            directory / "status.json",
            {
                "status": "failed",
                "pid": os.getpid(),
                "created_at": status.get("created_at"),
                "started_at": started_at,
                "completed_at": _utc_now(),
                "exit_code": 75,
                "error": f"{type(exc).__name__}: {str(exc)[:15_000]}",
            },
        )
        return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=("submit", "status", "worker"),
    )
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--deployed-sha-file", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "submit":
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError(
                "Finance transport submit requires a JSON object"
            )
        result = submit_job(
            args.runtime_dir,
            job_id=args.job_id,
            deployed_sha_file=args.deployed_sha_file,
            request_payload=payload,
        )
    elif args.action == "worker":
        return run_worker(
            args.runtime_dir,
            job_id=args.job_id,
            deployed_sha_file=args.deployed_sha_file,
        )
    else:
        result = _status_payload(
            args.runtime_dir.resolve(),
            job_id=args.job_id,
            deployed_sha=_deployed_sha(args.deployed_sha_file),
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
