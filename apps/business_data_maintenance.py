#!/usr/bin/env python3
"""Audited quiet window for all repo-owned automatic business-data writers."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.seller_portal_automation_guard import current_lock_status  # noqa: E402
from apps.sheet_vitrina_v1_auto_refresh_tick import (  # noqa: E402
    _build_web_auth_cookie,
    _read_env_file,
)


SCHEMA_VERSION = "business_data_maintenance_v1"
STATE_FILENAME = ".business-data-maintenance.json"
AUDIT_FILENAME = ".business-data-maintenance-audit.jsonl"
QUIESCENT_SERVICE_STATES = frozenset({"inactive", "failed"})
ACTIVE_RUNTIME_STATES = frozenset(
    {
        "queued",
        "starting",
        "planning",
        "measuring",
        "cooldown",
        "running",
        "restoring",
        "manual_restore_required",
    }
)

CORE_TIMER_UNITS = (
    "wb-core-sheet-vitrina-refresh.timer",
    "wb-core-sheet-vitrina-closure-retry.timer",
    "wb-core-feedbacks-auto-complaints-tick.timer",
    "wb-core-spp-tester-schedule-tick.timer",
    "wb-core-wb-finance-weekly.timer",
)
FORCE_OFF_TIMER_UNITS = (
    "wb-core-warehouse-functional-sync.timer",
    "wb-core-autoanswers-readonly-sync.timer",
    "wb-core-autoanswers-worker.timer",
)
ALL_BUSINESS_TIMER_UNITS = CORE_TIMER_UNITS + FORCE_OFF_TIMER_UNITS
ALL_BUSINESS_SERVICE_UNITS = tuple(unit.removesuffix(".timer") + ".service" for unit in ALL_BUSINESS_TIMER_UNITS)

WRITER_PROCESS_MARKERS = (
    "sheet_vitrina_v1_auto_refresh_tick.py",
    "sheet_vitrina_v1_closure_retry.py",
    "sheet_vitrina_v1_feedbacks_auto_complaints_tick.py",
    "wb_spp_tester_schedule_tick.py",
    "wb_finance_weekly.py",
    "warehouse_functional_runner.py",
    "wb_autoanswers_readonly.py",
    "wb_autoanswers_worker.py",
)

WEB_SCHEDULE_PATH = "/v1/sheet-vitrina-v1/web-vitrina/auto-schedules"
FEEDBACK_SCHEDULE_PATH = "/v1/sheet-vitrina-v1/feedbacks/automation/schedules"
SPP_SCHEDULE_PATH = "/v1/sheet-vitrina-v1/prices/spp-test/schedule"
SPP_STATUS_PATH = "/v1/sheet-vitrina-v1/prices/spp-test/status"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class SystemdClient:
    def _run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/usr/bin/systemctl", *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def unit_state(self, unit: str) -> dict[str, Any]:
        result = self._run(
            [
                "show",
                unit,
                "--property=LoadState,UnitFileState,ActiveState,SubState,Result,ExecMainCode,ExecMainStatus,LastTriggerUSec,NextElapseUSecRealtime",
                "--no-pager",
            ]
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"systemctl show {unit} failed: "
                + (result.stderr.strip() or f"exit {result.returncode}")
            )
        properties: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                properties[key] = value
        return {
            "unit": unit,
            "is_enabled": str(properties.get("UnitFileState") or ""),
            "is_active": str(properties.get("ActiveState") or ""),
            "properties": properties,
        }

    def disable_now(self, unit: str) -> None:
        result = self._run(["disable", "--now", unit])
        if result.returncode != 0:
            raise RuntimeError(
                f"systemctl disable --now {unit} failed: "
                + (result.stderr.strip() or f"exit {result.returncode}")
            )

    def discovered_timers(self) -> list[str]:
        result = self._run(
            ["list-unit-files", "wb-core-*.timer", "--no-legend", "--no-pager"]
        )
        if result.returncode != 0:
            raise RuntimeError(
                "systemctl list-unit-files failed: "
                + (result.stderr.strip() or f"exit {result.returncode}")
            )
        return sorted(
            {
                line.split()[0]
                for line in result.stdout.splitlines()
                if line.split() and line.split()[0].startswith("wb-core-")
            }
        )


class RuntimeScheduleClient:
    def __init__(self, *, base_url: str, cookie: str, timeout_seconds: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookie = cookie
        self.timeout_seconds = max(1, int(timeout_seconds))

    def _request(self, path: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Cookie": self.cookie,
            },
            method="GET" if payload is None else "POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                status = int(response.status)
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            raw = exc.read()
        try:
            result = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"HTTP {status} {path} returned non-JSON") from exc
        if status >= 400:
            raise RuntimeError(f"HTTP {status} {path}: {result.get('error') if isinstance(result, Mapping) else result}")
        if not isinstance(result, dict):
            raise RuntimeError(f"HTTP {status} {path} returned non-object JSON")
        return result

    def read_all(self) -> dict[str, dict[str, Any]]:
        return {
            "web_vitrina": self._request(WEB_SCHEDULE_PATH),
            "feedback_complaints": self._request(FEEDBACK_SCHEDULE_PATH),
            "spp": self._request(SPP_SCHEDULE_PATH),
            "spp_status": self._request(SPP_STATUS_PATH),
        }

    def disable_all(self, current: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
        web = dict(current.get("web_vitrina") or {})
        web_policy = dict(web.get("schedule_policy") or {})
        web_policy.update({"mode": "manual", "interval_hours": None})
        web_schedules = [
            {**dict(item), "enabled": False}
            for item in (web.get("schedules") or web.get("effective_schedules") or [])
            if isinstance(item, Mapping)
        ]
        self._request(
            WEB_SCHEDULE_PATH,
            {"schedule_policy": web_policy, "schedules": web_schedules},
        )

        feedback = dict(current.get("feedback_complaints") or {})
        feedback_schedules = [
            {**dict(item), "enabled": False}
            for item in feedback.get("schedules", [])
            if isinstance(item, Mapping)
        ]
        self._request(FEEDBACK_SCHEDULE_PATH, {"schedules": feedback_schedules})

        spp = dict(current.get("spp") or {})
        spp_schedule = dict(spp.get("schedule") or {})
        spp_schedule["enabled"] = False
        self._request(SPP_SCHEDULE_PATH, {"schedule": spp_schedule})
        return self.read_all()


def _runtime_summary(payloads: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    web = payloads.get("web_vitrina") or {}
    feedback = payloads.get("feedback_complaints") or {}
    spp = payloads.get("spp") or {}
    spp_status = payloads.get("spp_status") or {}

    web_schedules = [item for item in web.get("schedules", []) if isinstance(item, Mapping)]
    feedback_schedules = [item for item in feedback.get("schedules", []) if isinstance(item, Mapping)]
    spp_schedule = spp.get("schedule") if isinstance(spp.get("schedule"), Mapping) else {}
    feedback_active = [
        {"run_id": str(item.get("run_id") or ""), "status": str(item.get("status") or "")}
        for item in feedback.get("recent_runs", [])
        if isinstance(item, Mapping) and str(item.get("status") or "").lower() in ACTIVE_RUNTIME_STATES
    ]
    spp_job = (
        spp_status.get("active_job")
        if isinstance(spp_status.get("active_job"), Mapping)
        else spp_status.get("job")
        if isinstance(spp_status.get("job"), Mapping)
        else {}
    )
    spp_active = (
        {"job_id": str(spp_job.get("job_id") or ""), "status": str(spp_job.get("status") or "")}
        if str(spp_job.get("status") or "").lower() in ACTIVE_RUNTIME_STATES
        else None
    )
    web_status = str(web.get("last_auto_run_status") or "").lower()
    return {
        "web_vitrina": {
            "schedule_count": len(web_schedules),
            "enabled_ids": [str(item.get("id") or "") for item in web_schedules if bool(item.get("enabled"))],
            "schedule_policy": dict(web.get("schedule_policy") or {}),
            "last_auto_run_status": web_status,
            "active": web_status in ACTIVE_RUNTIME_STATES,
        },
        "feedback_complaints": {
            "schedule_count": len(feedback_schedules),
            "enabled_ids": [str(item.get("id") or "") for item in feedback_schedules if bool(item.get("enabled"))],
            "active_runs": feedback_active,
        },
        "spp": {
            "enabled": bool(spp_schedule.get("enabled")),
            "schedule_id": str(spp_schedule.get("id") or ""),
            "active_job": spp_active,
        },
    }


def _writer_processes(proc_root: Path = Path("/proc")) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not proc_root.is_dir():
        return rows
    for entry in proc_root.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        for marker in WRITER_PROCESS_MARKERS:
            if marker.encode("utf-8") in command:
                rows.append({"pid": int(entry.name), "marker": marker})
                break
    return sorted(rows, key=lambda item: (str(item["marker"]), int(item["pid"])))


def _flock_snapshot(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path), "exists": path.exists(), "held": False}
    if not path.exists():
        return result
    handle = path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result["held"] = True
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
    return result


def _lock_summary(runtime_dir: Path) -> dict[str, Any]:
    return {
        "warehouse_functional": _flock_snapshot(runtime_dir / ".warehouse-functional-sync.lock"),
        "web_schedule": _flock_snapshot(runtime_dir / "sheet_vitrina_v1_auto_refresh_schedules.json.lock"),
        "spp_execution": _flock_snapshot(runtime_dir / "sheet_vitrina_v1_prices" / "spp_tests" / "execution.lock"),
        "seller_portal": current_lock_status(runtime_dir),
    }


def _cron_entries() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    sources = [Path("/etc/crontab")]
    cron_dir = Path("/etc/cron.d")
    if cron_dir.is_dir():
        sources.extend(sorted(path for path in cron_dir.iterdir() if path.is_file()))
    for source in sources:
        try:
            lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and ("wb-core" in stripped or "/opt/wb-core-runtime" in stripped):
                rows.append({"source": str(source), "entry": stripped[:500]})
    crontab = Path("/usr/bin/crontab")
    if crontab.is_file():
        result = subprocess.run([str(crontab), "-l"], text=True, capture_output=True, check=False)
        if result.returncode in {0, 1}:
            for line in result.stdout.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and ("wb-core" in stripped or "/opt/wb-core-runtime" in stripped):
                    rows.append({"source": "root_crontab", "entry": stripped[:500]})
    return rows


def _save_json_0600(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)


def _append_audit_0600(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)


def maintenance_status(
    runtime_dir: Path,
    *,
    systemd: SystemdClient,
    schedules: RuntimeScheduleClient,
    proc_root: Path = Path("/proc"),
) -> dict[str, Any]:
    timer_states = {unit: systemd.unit_state(unit) for unit in ALL_BUSINESS_TIMER_UNITS}
    service_states = {unit: systemd.unit_state(unit) for unit in ALL_BUSINESS_SERVICE_UNITS}
    discovered = systemd.discovered_timers()
    unknown_timers = [unit for unit in discovered if unit not in ALL_BUSINESS_TIMER_UNITS]
    runtime = _runtime_summary(schedules.read_all())
    processes = _writer_processes(proc_root)
    locks = _lock_summary(runtime_dir)
    cron = _cron_entries()
    timers_quiet = all(
        state["is_enabled"] == "disabled" and state["is_active"] == "inactive"
        for state in timer_states.values()
    )
    services_quiet = all(
        state["is_active"] in QUIESCENT_SERVICE_STATES
        for state in service_states.values()
    )
    runtime_quiet = (
        not runtime["web_vitrina"]["enabled_ids"]
        and not runtime["web_vitrina"]["active"]
        and not runtime["feedback_complaints"]["enabled_ids"]
        and not runtime["feedback_complaints"]["active_runs"]
        and runtime["spp"]["enabled"] is False
        and runtime["spp"]["active_job"] is None
    )
    locks_quiet = (
        not any(bool(value.get("held")) for key, value in locks.items() if key != "seller_portal")
        and not bool((locks.get("seller_portal") or {}).get("busy"))
    )
    quiet = bool(
        timers_quiet
        and services_quiet
        and runtime_quiet
        and locks_quiet
        and not processes
        and not cron
        and not unknown_timers
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "quiet" if quiet else "not_quiet",
        "quiet": quiet,
        "captured_at": _utc_now(),
        "timers": timer_states,
        "services": service_states,
        "discovered_wb_core_timers": discovered,
        "unknown_wb_core_timers": unknown_timers,
        "runtime_schedules": runtime,
        "writer_processes": processes,
        "writer_locks": locks,
        "cron_entries": cron,
        "state_path": str(runtime_dir / STATE_FILENAME),
        "audit_path": str(runtime_dir / AUDIT_FILENAME),
    }


def maintenance_hold(
    runtime_dir: Path,
    *,
    systemd: SystemdClient,
    schedules: RuntimeScheduleClient,
    proc_root: Path = Path("/proc"),
    wait_timeout_seconds: float = 1200.0,
    poll_interval_seconds: float = 2.0,
) -> dict[str, Any]:
    prepared = maintenance_prepare(
        runtime_dir,
        systemd=systemd,
        schedules=schedules,
        proc_root=proc_root,
    )
    if prepared.get("quiet"):
        state_path = runtime_dir / STATE_FILENAME
        audit_path = runtime_dir / AUDIT_FILENAME
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.update({"phase": "held", "held_at": _utc_now(), "hold_readback": prepared})
        _save_json_0600(state_path, state)
        _append_audit_0600(audit_path, {"event": "hold_acquired", "captured_at": _utc_now(), "status": prepared})
        return {**prepared, "status": "held"}
    state_path = runtime_dir / STATE_FILENAME
    audit_path = runtime_dir / AUDIT_FILENAME
    state = json.loads(state_path.read_text(encoding="utf-8"))

    deadline = time.monotonic() + max(0.0, float(wait_timeout_seconds))
    while True:
        current = maintenance_status(runtime_dir, systemd=systemd, schedules=schedules, proc_root=proc_root)
        if current["quiet"]:
            break
        if time.monotonic() >= deadline:
            state.update({"last_readback": current, "error": "timed out waiting for business-data quiet window"})
            _save_json_0600(state_path, state)
            _append_audit_0600(audit_path, {"event": "hold_wait_timeout", "captured_at": _utc_now(), "status": current})
            raise TimeoutError(state["error"])
        time.sleep(max(0.05, float(poll_interval_seconds)))

    state.update({"phase": "held", "held_at": _utc_now(), "hold_readback": current})
    _save_json_0600(state_path, state)
    _append_audit_0600(audit_path, {"event": "hold_acquired", "captured_at": _utc_now(), "status": current})
    return {**current, "status": "held", "idempotent": False}


def maintenance_prepare(
    runtime_dir: Path,
    *,
    systemd: SystemdClient,
    schedules: RuntimeScheduleClient,
    proc_root: Path = Path("/proc"),
) -> dict[str, Any]:
    state_path = runtime_dir / STATE_FILENAME
    audit_path = runtime_dir / AUDIT_FILENAME
    existing: dict[str, Any] | None = None
    if state_path.is_file():
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise RuntimeError("business maintenance state is not a JSON object")
        existing = loaded
    before_payloads = schedules.read_all()
    before = maintenance_status(runtime_dir, systemd=systemd, schedules=schedules, proc_root=proc_root)
    already_quiet = bool(before["quiet"])
    if before["unknown_wb_core_timers"]:
        raise RuntimeError(f"unknown wb-core timers require explicit classification: {before['unknown_wb_core_timers']}")
    if before["cron_entries"]:
        raise RuntimeError("repo-owned business cron entries exist outside the systemd maintenance boundary")
    state = dict(existing or {})
    state.update(
        {
            "schema_version": SCHEMA_VERSION,
            "phase": "holding",
            "hold_started_at": str(state.get("hold_started_at") or _utc_now()),
            "baseline": state.get("baseline") or before,
            "runtime_schedule_baseline": state.get("runtime_schedule_baseline") or before_payloads,
        }
    )
    _save_json_0600(state_path, state)
    _append_audit_0600(audit_path, {"event": "hold_started", "captured_at": _utc_now(), "status": before})

    for unit in CORE_TIMER_UNITS:
        systemd.disable_now(unit)
    schedules.disable_all(before_payloads)
    current = maintenance_status(runtime_dir, systemd=systemd, schedules=schedules, proc_root=proc_root)
    state.update({"phase": "prepared", "prepared_at": _utc_now(), "prepare_readback": current})
    _save_json_0600(state_path, state)
    _append_audit_0600(audit_path, {"event": "core_freeze_prepared", "captured_at": _utc_now(), "status": current})
    return {**current, "status": "prepared", "idempotent": already_quiet}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "prepare", "hold"))
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--wait-timeout-seconds", type=float, default=1200.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=2.0)
    args = parser.parse_args(argv)
    env = _read_env_file(Path(args.env_file))
    base_url = (
        args.base_url
        or env.get("BUSINESS_DATA_MAINTENANCE_BASE_URL")
        or f"http://{env.get('REGISTRY_UPLOAD_HTTP_HOST', '127.0.0.1')}:{env.get('REGISTRY_UPLOAD_HTTP_PORT', '8765')}"
    )
    schedules = RuntimeScheduleClient(base_url=base_url, cookie=_build_web_auth_cookie(env))
    runtime_dir = Path(args.runtime_dir).resolve()
    systemd = SystemdClient()
    if args.action == "status":
        result = maintenance_status(runtime_dir, systemd=systemd, schedules=schedules)
    elif args.action == "prepare":
        result = maintenance_prepare(runtime_dir, systemd=systemd, schedules=schedules)
    else:
        result = maintenance_hold(
            runtime_dir,
            systemd=systemd,
            schedules=schedules,
            wait_timeout_seconds=args.wait_timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
