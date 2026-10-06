"""Opt-in live API-job markers; no polling, queue, or operator-job locking."""
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

OPERATIONS = frozenset({"auto_update", "refresh", "refresh_group", "cycle"})
DIRECTORY = "finished-snapshot-api-jobs"


def process_identity(pid: int) -> str | None:
    """Linux PID reuse is distinguished by kernel start ticks and boot ID."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        ticks = stat[stat.rfind(")") + 2:].split()[19]
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return f"{boot}:{ticks}"
    except FileNotFoundError:
        return None


def _atomic(path: Path, payload: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(payload, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ApiJobMarkers:
    def __init__(self, runtime_dir: Path) -> None:
        self.root = Path(runtime_dir) / DIRECTORY
        self.owner = {"pid": os.getpid(), "identity": process_identity(os.getpid())}
        self.disabled = False
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        if not self.owner["identity"]:
            raise ValueError("snapshot API admission requires Linux process identity")
        # A previous live HTTP owner must not be silently replaced.
        prior = self.root / "ready.json"
        if prior.exists():
            old = json.loads(prior.read_text())
            if old != self.owner and process_identity(int(old["pid"])) == old["identity"]:
                raise ValueError("snapshot API admission already has a live HTTP owner")
        for path in self.root.glob("job-*.json"):
            value = json.loads(path.read_text())
            if process_identity(int(value["pid"])) != value["identity"]:
                path.unlink()
        _atomic(prior, self.owner)

    def disable(self) -> None:
        self.disabled = True
        try:
            (self.root / "ready.json").unlink(missing_ok=True)
        except OSError:
            pass

    def start(self, job_id: str, operation: str) -> Path | None:
        if operation not in OPERATIONS:
            return None
        if self.disabled:
            return None
        path = self.root / f"job-{job_id}.json"
        try:
            _atomic(path, {**self.owner, "job_id": job_id, "operation": operation})
            return path
        except (OSError, ValueError):
            self.disable()
            return None

    def finish(self, path: Path | None) -> None:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                self.disable()


def api_jobs_admission(runtime_dir: Path, *, cycle_owner: dict | None = None) -> str:
    """A point-in-time read; future manual starts remain allowed."""
    root = Path(runtime_dir) / DIRECTORY
    try:
        before = (root / "ready.json").read_bytes()
        owner = json.loads(before)
        if not owner.get("identity") or process_identity(int(owner["pid"])) != owner["identity"]:
            return "unknown"
        matched_owner = False
        if cycle_owner is not None:
            from packages.application.business_data_procedure_admission import already_admitted
            if (not already_admitted(runtime_dir) or cycle_owner.get('operation') != 'cycle'
                    or cycle_owner.get('pid') != os.getpid()
                    or cycle_owner.get('identity') != process_identity(os.getpid())
                    or {key: cycle_owner.get(key) for key in ('pid', 'identity')} != owner):
                return 'unknown'
        for path in root.glob("job-*.json"):
            try:
                job = json.loads(path.read_text())
            except FileNotFoundError:
                continue  # Completed after the directory listing.
            if not job.get("identity") or job.get("operation") not in OPERATIONS or not job.get("job_id"):
                return "unknown"
            if process_identity(int(job["pid"])) == job["identity"]:
                if cycle_owner is not None and job == cycle_owner:
                    matched_owner = True
                    continue
                return "busy"
        if (root / "ready.json").read_bytes() != before:
            return "unknown"
        return "idle" if cycle_owner is None or matched_owner else "unknown"
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return "unknown"
