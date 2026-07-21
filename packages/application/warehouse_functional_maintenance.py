"""Bounded maintenance hold for the functional warehouse writer."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence

from packages.application.warehouse_functional_lock import (
    WAREHOUSE_FUNCTIONAL_LOCK_FILENAME,
)


WAREHOUSE_FUNCTIONAL_TIMER_UNIT = "wb-core-warehouse-functional-sync.timer"
WAREHOUSE_FUNCTIONAL_SERVICE_UNIT = "wb-core-warehouse-functional-sync.service"
WAREHOUSE_FUNCTIONAL_MAINTENANCE_STATE_FILENAME = (
    ".warehouse-functional-maintenance.json"
)
WAREHOUSE_FUNCTIONAL_MAINTENANCE_AUDIT_FILENAME = (
    ".warehouse-functional-maintenance-audit.jsonl"
)
SYSTEMCTL_BIN = "/usr/bin/systemctl"
SERVICE_QUIESCENT_STATES = frozenset({"inactive", "failed"})


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class SystemdClient:
    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._runner = runner

    def _run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return self._runner(
            [SYSTEMCTL_BIN, *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def scalar(self, action: str, unit: str) -> str:
        result = self._run([action, unit])
        value = result.stdout.strip().splitlines()
        if value:
            return value[-1].strip()
        raise RuntimeError(
            f"systemctl {action} {unit} returned no state: "
            + (result.stderr.strip() or f"exit {result.returncode}")
        )

    def properties(self, unit: str, names: Sequence[str]) -> dict[str, str]:
        result = self._run(
            ["show", unit, "--property=" + ",".join(names), "--no-pager"]
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"systemctl show {unit} failed: "
                + (result.stderr.strip() or f"exit {result.returncode}")
            )
        properties: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            properties[key] = value
        return properties

    def cat_digest(self, unit: str) -> str:
        result = self._run(["cat", unit, "--no-pager"])
        if result.returncode != 0:
            raise RuntimeError(
                f"systemctl cat {unit} failed: "
                + (result.stderr.strip() or f"exit {result.returncode}")
            )
        return "sha256:" + hashlib.sha256(result.stdout.encode("utf-8")).hexdigest()

    def mutate(self, action: str, unit: str) -> None:
        result = self._run([action, unit])
        if result.returncode != 0:
            raise RuntimeError(
                f"systemctl {action} {unit} failed: "
                + (result.stderr.strip() or f"exit {result.returncode}")
            )


def warehouse_functional_service_is_quiescent(active_state: str) -> bool:
    """A failed oneshot is terminal evidence, not a process still executing."""

    return str(active_state or "") in SERVICE_QUIESCENT_STATES


def _unit_snapshot(client: SystemdClient) -> dict[str, Any]:
    timer_properties = client.properties(
        WAREHOUSE_FUNCTIONAL_TIMER_UNIT,
        (
            "LoadState",
            "UnitFileState",
            "ActiveState",
            "SubState",
            "LastTriggerUSec",
            "NextElapseUSecRealtime",
            "NextElapseUSecMonotonic",
        ),
    )
    service_properties = client.properties(
        WAREHOUSE_FUNCTIONAL_SERVICE_UNIT,
        (
            "LoadState",
            "ActiveState",
            "SubState",
            "Result",
            "ExecMainCode",
            "ExecMainStatus",
            "ActiveEnterTimestamp",
            "InactiveEnterTimestamp",
        ),
    )
    service_active = client.scalar(
        "is-active", WAREHOUSE_FUNCTIONAL_SERVICE_UNIT
    )
    return {
        "captured_at": _utc_now(),
        "timer": {
            "unit": WAREHOUSE_FUNCTIONAL_TIMER_UNIT,
            "is_enabled": client.scalar(
                "is-enabled", WAREHOUSE_FUNCTIONAL_TIMER_UNIT
            ),
            "is_active": client.scalar(
                "is-active", WAREHOUSE_FUNCTIONAL_TIMER_UNIT
            ),
            "properties": timer_properties,
            "unit_digest": client.cat_digest(WAREHOUSE_FUNCTIONAL_TIMER_UNIT),
        },
        "service": {
            "unit": WAREHOUSE_FUNCTIONAL_SERVICE_UNIT,
            "is_active": service_active,
            "quiescent": warehouse_functional_service_is_quiescent(service_active),
            "properties": service_properties,
            "unit_digest": client.cat_digest(WAREHOUSE_FUNCTIONAL_SERVICE_UNIT),
        },
    }


def _finance_apply_processes(proc_root: Path = Path("/proc")) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    if not proc_root.is_dir():
        return matches
    for entry in proc_root.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if (
            b"apps/wb_finance_weekly.py" in command
            and b"canonical-cost-backfill" in command
            and b"--apply" in command
        ):
            matches.append({"pid": int(entry.name), "detected": True})
    return sorted(matches, key=lambda item: int(item["pid"]))


def _lock_snapshot(runtime_dir: Path) -> dict[str, Any]:
    lock_path = (runtime_dir / WAREHOUSE_FUNCTIONAL_LOCK_FILENAME).resolve()
    result: dict[str, Any] = {
        "path": str(lock_path),
        "exists": lock_path.exists(),
        "held": False,
    }
    if not lock_path.exists():
        return result
    stat = lock_path.stat()
    result.update(
        {
            "mode": oct(stat.st_mode & 0o777),
            "size_bytes": stat.st_size,
            "modified_at_epoch": stat.st_mtime,
        }
    )
    handle = lock_path.open("r+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        result["held"] = True
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
    return result


def _state_path(runtime_dir: Path) -> Path:
    return runtime_dir / WAREHOUSE_FUNCTIONAL_MAINTENANCE_STATE_FILENAME


def _audit_path(runtime_dir: Path) -> Path:
    return runtime_dir / WAREHOUSE_FUNCTIONAL_MAINTENANCE_AUDIT_FILENAME


def _load_state(runtime_dir: Path) -> dict[str, Any] | None:
    path = _state_path(runtime_dir)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("warehouse maintenance state is not a JSON object")
    return payload


def _save_state(runtime_dir: Path, payload: Mapping[str, Any]) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    path = _state_path(runtime_dir)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=runtime_dir, prefix=path.name + ".", delete=False
    ) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)


def _append_audit(runtime_dir: Path, payload: Mapping[str, Any]) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    path = _audit_path(runtime_dir)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)


def _bounded_readback(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Exclude the state file itself so repeated hold cycles cannot nest it."""

    return {key: value for key, value in payload.items() if key != "maintenance_state"}


def maintenance_status(
    runtime_dir: Path,
    *,
    client: SystemdClient | None = None,
    proc_root: Path = Path("/proc"),
) -> dict[str, Any]:
    systemd = client or SystemdClient()
    state = _load_state(runtime_dir)
    return {
        "status": "ok",
        "operation": "status",
        "runtime_dir": str(runtime_dir.resolve()),
        "captured_at": _utc_now(),
        "units": _unit_snapshot(systemd),
        "warehouse_lock": _lock_snapshot(runtime_dir),
        "finance_apply_processes": _finance_apply_processes(proc_root),
        "maintenance_state": state,
        "state_path": str(_state_path(runtime_dir).resolve()),
        "audit_path": str(_audit_path(runtime_dir).resolve()),
    }


def maintenance_hold(
    runtime_dir: Path,
    *,
    client: SystemdClient | None = None,
    proc_root: Path = Path("/proc"),
    wait_timeout_seconds: float = 1200.0,
    poll_interval_seconds: float = 2.0,
) -> dict[str, Any]:
    systemd = client or SystemdClient()
    existing = _load_state(runtime_dir)
    resuming = bool(existing and existing.get("phase") == "holding")
    if existing and existing.get("phase") in {"holding", "held"}:
        current = maintenance_status(runtime_dir, client=systemd, proc_root=proc_root)
        recorded_units = (existing.get("baseline") or {}).get("units") or {}
        recorded_timer = recorded_units.get("timer") or {}
        recorded_service = recorded_units.get("service") or {}
        if (
            existing.get("phase") == "held"
            and current["units"]["timer"]["is_active"] == "inactive"
            and current["units"]["timer"]["is_enabled"]
            == recorded_timer.get("is_enabled")
            and current["units"]["timer"]["unit_digest"]
            == recorded_timer.get("unit_digest")
            and current["units"]["service"]["quiescent"] is True
            and current["units"]["service"]["unit_digest"]
            == recorded_service.get("unit_digest")
            and not current["warehouse_lock"]["held"]
            and not current["finance_apply_processes"]
        ):
            return {**current, "status": "held", "idempotent": True}
        if existing.get("phase") == "held":
            raise RuntimeError("existing maintenance hold no longer satisfies its invariants")

    before = maintenance_status(runtime_dir, client=systemd, proc_root=proc_root)
    if before["finance_apply_processes"]:
        raise RuntimeError("Finance canonical apply is already running")
    baseline = (existing or {}).get("baseline") if resuming else _bounded_readback(before)
    baseline_units = (baseline or {}).get("units") or {}
    enabled = str((baseline_units.get("timer") or {}).get("is_enabled") or "")
    active = str((baseline_units.get("timer") or {}).get("is_active") or "")
    if enabled not in {"enabled", "disabled"} or active not in {"active", "inactive"}:
        raise RuntimeError(
            f"unsupported timer baseline: is_enabled={enabled}, is_active={active}"
        )
    state: dict[str, Any] = dict(existing or {}) if resuming else {
        "schema_version": "warehouse_functional_maintenance_v1",
        "phase": "holding",
        "hold_started_at": _utc_now(),
        "baseline": _bounded_readback(before),
    }
    _save_state(runtime_dir, state)
    _append_audit(
        runtime_dir,
        {"event": "hold_resumed" if resuming else "hold_started", **state},
    )
    systemd.mutate("stop", WAREHOUSE_FUNCTIONAL_TIMER_UNIT)

    deadline = time.monotonic() + wait_timeout_seconds
    while True:
        current = maintenance_status(runtime_dir, client=systemd, proc_root=proc_root)
        service_done = current["units"]["service"]["quiescent"] is True
        lock_free = not current["warehouse_lock"]["held"]
        if service_done and lock_free:
            break
        if time.monotonic() >= deadline:
            state["last_readback"] = _bounded_readback(current)
            state["error"] = "timed out waiting for warehouse service/shared lock"
            _save_state(runtime_dir, state)
            _append_audit(runtime_dir, {"event": "hold_wait_timeout", **state})
            raise TimeoutError(state["error"])
        time.sleep(max(0.05, poll_interval_seconds))

    if current["finance_apply_processes"]:
        raise RuntimeError("Finance canonical apply appeared while establishing hold")
    if current["units"]["timer"]["is_active"] != "inactive":
        raise RuntimeError("warehouse timer is not inactive after stop")
    if current["units"]["timer"]["is_enabled"] != enabled:
        raise RuntimeError("warehouse timer enabled state changed while stopping it")
    state.update(
        {
            "phase": "held",
            "held_at": _utc_now(),
            "hold_readback": _bounded_readback(current),
        }
    )
    _save_state(runtime_dir, state)
    _append_audit(runtime_dir, {"event": "hold_acquired", **state})
    final = maintenance_status(runtime_dir, client=systemd, proc_root=proc_root)
    return {**final, "status": "held", "idempotent": False}


def maintenance_restore(
    runtime_dir: Path,
    *,
    client: SystemdClient | None = None,
    proc_root: Path = Path("/proc"),
) -> dict[str, Any]:
    systemd = client or SystemdClient()
    state = _load_state(runtime_dir)
    if not state:
        raise RuntimeError("warehouse maintenance state does not exist")
    if state.get("phase") == "restored":
        current = maintenance_status(runtime_dir, client=systemd, proc_root=proc_root)
        baseline_timer = (
            ((state.get("baseline") or {}).get("units") or {}).get("timer") or {}
        )
        if (
            current["units"]["timer"]["is_enabled"]
            != baseline_timer.get("is_enabled")
            or current["units"]["timer"]["is_active"]
            != baseline_timer.get("is_active")
            or current["units"]["timer"]["unit_digest"]
            != baseline_timer.get("unit_digest")
        ):
            raise RuntimeError("restored warehouse timer drifted from its recorded baseline")
        return {**current, "status": "restored", "idempotent": True}
    if state.get("phase") != "held":
        raise RuntimeError(f"warehouse maintenance phase is {state.get('phase')!r}, not held")
    current = maintenance_status(runtime_dir, client=systemd, proc_root=proc_root)
    if current["finance_apply_processes"]:
        raise RuntimeError("Finance canonical apply is still running")
    if current["warehouse_lock"]["held"]:
        raise RuntimeError("warehouse functional shared lock is still held")
    if current["units"]["service"]["quiescent"] is not True:
        raise RuntimeError("warehouse functional service is still active")

    baseline_units = (state.get("baseline") or {}).get("units") or {}
    baseline_timer = baseline_units.get("timer") or {}
    baseline_service = baseline_units.get("service") or {}
    enabled = str(baseline_timer.get("is_enabled") or "")
    active = str(baseline_timer.get("is_active") or "")
    if enabled not in {"enabled", "disabled"} or active not in {"active", "inactive"}:
        raise RuntimeError("stored timer baseline is not restorable")
    if current["units"]["timer"]["unit_digest"] != baseline_timer.get("unit_digest"):
        raise RuntimeError("warehouse timer unit configuration changed during hold")
    if current["units"]["service"]["unit_digest"] != baseline_service.get("unit_digest"):
        raise RuntimeError("warehouse service unit configuration changed during hold")

    systemd.mutate("enable" if enabled == "enabled" else "disable", WAREHOUSE_FUNCTIONAL_TIMER_UNIT)
    systemd.mutate("start" if active == "active" else "stop", WAREHOUSE_FUNCTIONAL_TIMER_UNIT)
    restored = maintenance_status(runtime_dir, client=systemd, proc_root=proc_root)
    if (
        restored["units"]["timer"]["is_enabled"] != enabled
        or restored["units"]["timer"]["is_active"] != active
    ):
        raise RuntimeError("warehouse timer did not return to its exact baseline state")
    state.update(
        {
            "phase": "restored",
            "restored_at": _utc_now(),
            "restore_readback": _bounded_readback(restored),
        }
    )
    _save_state(runtime_dir, state)
    _append_audit(runtime_dir, {"event": "hold_restored", **state})
    final = maintenance_status(runtime_dir, client=systemd, proc_root=proc_root)
    return {**final, "status": "restored", "idempotent": False}
