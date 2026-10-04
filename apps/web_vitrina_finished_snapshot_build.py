#!/usr/bin/env python3
"""Bounded two-hour finished-snapshot job; busy/unknown skips without retries."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packages.application.web_vitrina_snapshot_admission import api_jobs_admission
from packages.application.warehouse_functional_lock import warehouse_functional_job_is_busy

HEAVY_UNITS = (
    "wb-core-warehouse-functional-sync.service", "wb-core-wb-finance-daily.service",
    "wb-core-wb-finance-weekly.service", "wb-core-sheet-vitrina-refresh.service",
    "wb-core-sheet-vitrina-closure-retry.service", "wb-core-finance-backup-rotation.service",
    "wb-core-sheet-vitrina-health-candidate.service", "wb-core-sheet-vitrina-health-confirmation.service",
    "wb-core-buyer-authenticated-collect.service",
)


def deadline_seconds(now: datetime) -> float:
    local = now.astimezone(ZoneInfo("Asia/Tbilisi"))
    if local.hour % 2 != 1 or not 55 <= local.minute < 59:
        return 0
    return max(0, (local.replace(minute=59, second=0, microsecond=0) - local).total_seconds())


def systemd_admission(run=subprocess.run) -> str:
    for unit in HEAVY_UNITS:
        try:
            result = run(["systemctl", "show", unit, "--property=LoadState,ActiveState,MainPID"],
                         capture_output=True, text=True, timeout=2, check=True)
            fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            if fields.get("LoadState") != "loaded" or fields.get("ActiveState") not in {
                "active", "activating", "deactivating", "inactive", "failed"
            } or not fields.get("MainPID", "").isdigit():
                return "unknown"
            if fields["ActiveState"] in {"active", "activating", "deactivating"} or int(fields["MainPID"]):
                return "busy"
        except (OSError, ValueError, subprocess.SubprocessError):
            return "unknown"
    return "idle"


def lock_admission(path: Path) -> str:
    try:
        # Read-only descriptor, no creation; probe releases immediately.
        with path.open("rb") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return "busy"
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return "idle"
    except OSError:
        return "unknown"


def admission(runtime_dir: Path) -> str:
    for state in (api_jobs_admission(runtime_dir), systemd_admission(),
                  lock_admission(runtime_dir / ".wb-finance-daily-worker.lock")):
        if state != "idle":
            return state
    try:
        if warehouse_functional_job_is_busy(runtime_dir):
            return "busy"
    except OSError:
        return "unknown"
    return "idle"


@contextmanager
def singleflight(runtime_dir: Path):
    with (runtime_dir / ".web-vitrina-finished-builder.lock").open("a+b") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def bounded_worker(command: list[str], seconds: float) -> dict:
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        return {"status": "skipped_deadline", "last_good_retained": True}
    if process.returncode:
        # No source/business data is printed in diagnostics.
        return {"status": "build_failed", "exit_code": process.returncode, "last_good_retained": True}
    try:
        return json.loads(stdout)
    except ValueError:
        return {"status": "build_result_unknown"}


def storage_admission(runtime_dir: Path, store: Path) -> None:
    from packages.application.finance_generation_filesystem import inspect_generation_filesystem
    from packages.application.web_vitrina_snapshot_pilot import MAX_STORE_BYTES, MAX_ARTIFACT_BYTES
    target = json.loads((ROOT / "artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__europe_api.json").read_text())
    mount = runtime_dir / "generations"
    if store.parent.resolve() != mount.resolve() or store.name != "web-vitrina-finished.sqlite3":
        raise ValueError("snapshot store is outside its exact owned generation path")
    evidence = inspect_generation_filesystem(runtime_dir, target["finance_generation_filesystem"])
    copy_bytes = store.stat().st_size if store.exists() else 0
    if evidence["available_bytes"] < 8 * 1024**3 + copy_bytes + 4 * MAX_ARTIFACT_BYTES:
        raise ValueError("snapshot filesystem reserve would be exceeded")
    if store.is_symlink() or (store.exists() and store.stat().st_size > MAX_STORE_BYTES):
        raise ValueError("snapshot store exceeds its own quota or is a symlink")
    # Dead temporary files can only be from this singleflight owner's killed
    # bootstrap worker. Never clean any business/generation family.
    for path in mount.glob(store.name + ".building-*"):
        if path.is_file() and not path.is_symlink():
            path.unlink()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--captured-now", default="", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        from packages.application.web_vitrina_finished_snapshot_builder import build_current_periods
        # Parent already holds singleflight and performed the storage preflight.
        # Repeat the mount identity to fail closed if it disappeared meanwhile.
        storage_admission(args.runtime_dir, args.store)
        result = build_current_periods(args.runtime_dir, args.store,
                                       now=datetime.fromisoformat(args.captured_now))
    else:
        with singleflight(args.runtime_dir) as acquired:
            if not acquired:
                result = {"status": "skipped_duplicate"}
            elif deadline_seconds(datetime.now(timezone.utc)) <= 0:
                result = {"status": "skipped_outside_window"}
            else:
                state = admission(args.runtime_dir)
                captured = datetime.now(timezone.utc)
                remaining = deadline_seconds(captured)
                if state != "idle":
                    result = {"status": "skipped_" + state}
                elif remaining <= 0:
                    result = {"status": "skipped_deadline"}
                else:
                    try:
                        storage_admission(args.runtime_dir, args.store)
                    except (OSError, ValueError) as exc:
                        print(json.dumps({"status": "skipped_storage", "reason": type(exc).__name__}))
                        return 0
                    remaining = deadline_seconds(datetime.now(timezone.utc))
                    if remaining <= 0:
                        print(json.dumps({"status": "skipped_deadline"}))
                        return 0
                    result = bounded_worker([sys.executable, str(Path(__file__).resolve()),
                        "--runtime-dir", str(args.runtime_dir), "--store", str(args.store),
                        "--worker", "--captured-now", captured.isoformat()], remaining)
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["status"] != "build_failed" else 1


from packages.application.business_data_procedure_admission import guard_cli

main = guard_cli(default_runtime='.runtime/registry_upload')(main)


if __name__ == "__main__":
    raise SystemExit(main())
