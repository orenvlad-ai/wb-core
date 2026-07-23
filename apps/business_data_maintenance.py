#!/usr/bin/env python3
"""Audited quiet window for all repo-owned automatic business-data writers."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
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
POLICY_SCHEMA_VERSION = "auto_updates_owner_policy_v1"
POLICY_FILENAME = ".auto-updates-policy.json"
POLICY_AUDIT_FILENAME = ".auto-updates-policy-audit.jsonl"
WAREHOUSE_MAINTENANCE_STATE_FILENAME = ".warehouse-functional-maintenance.json"
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

PROCESS_SPECS: tuple[dict[str, Any], ...] = (
    {
        "key": "vitrina_refresh",
        "display_name": "Обновление Витрины",
        "timer": "wb-core-sheet-vitrina-refresh.timer",
        "schedule": "web_vitrina",
    },
    {
        "key": "vitrina_closure_retry",
        "display_name": "Закрытие и повтор закрытия данных Витрины",
        "timer": "wb-core-sheet-vitrina-closure-retry.timer",
    },
    {
        "key": "warehouse_functional",
        "display_name": "Склады и себестоимость",
        "timer": "wb-core-warehouse-functional-sync.timer",
    },
    {
        "key": "wb_finance_weekly",
        "display_name": "Финансовый отчёт WB",
        "timer": "wb-core-wb-finance-weekly.timer",
    },
    {
        "key": "feedback_complaints",
        "display_name": "Авто-жалобы",
        "timer": "wb-core-feedbacks-auto-complaints-tick.timer",
        "schedule": "feedback_complaints",
    },
    {
        "key": "spp_test",
        "display_name": "Автоматический тест СПП",
        "timer": "wb-core-spp-tester-schedule-tick.timer",
        "schedule": "spp",
    },
    {
        "key": "autoanswers_readonly",
        "display_name": "Autoanswers read-only sync",
        "timer": "wb-core-autoanswers-readonly-sync.timer",
    },
    {
        "key": "autoanswers_worker",
        "display_name": "Autoanswers worker",
        "timer": "wb-core-autoanswers-worker.timer",
    },
)


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

    def enable_now(self, unit: str) -> None:
        result = self._run(["enable", "--now", unit])
        if result.returncode != 0:
            raise RuntimeError(
                f"systemctl enable --now {unit} failed: "
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

    def restore_selected(
        self,
        baseline: Mapping[str, Mapping[str, Any]],
        *,
        desired: Mapping[str, bool],
    ) -> dict[str, dict[str, Any]]:
        web = dict(baseline.get("web_vitrina") or {})
        web_policy = dict(web.get("schedule_policy") or {})
        web_schedules = [
            {
                **dict(item),
                "enabled": bool(item.get("enabled")) and bool(desired.get("vitrina_refresh")),
            }
            for item in (web.get("schedules") or web.get("effective_schedules") or [])
            if isinstance(item, Mapping)
        ]
        if not bool(desired.get("vitrina_refresh")):
            web_policy.update({"mode": "manual", "interval_hours": None})
        self._request(
            WEB_SCHEDULE_PATH,
            {"schedule_policy": web_policy, "schedules": web_schedules},
        )

        feedback = dict(baseline.get("feedback_complaints") or {})
        feedback_schedules = [
            {
                **dict(item),
                "enabled": bool(item.get("enabled"))
                and bool(desired.get("feedback_complaints")),
            }
            for item in feedback.get("schedules", [])
            if isinstance(item, Mapping)
        ]
        self._request(FEEDBACK_SCHEDULE_PATH, {"schedules": feedback_schedules})

        spp = dict(baseline.get("spp") or {})
        spp_schedule = dict(spp.get("schedule") or {})
        spp_schedule["enabled"] = (
            bool(spp_schedule.get("enabled")) and bool(desired.get("spp_test"))
        )
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


def _stable_fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _load_json_object(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path.name} is not a JSON object")
    return payload


def _process_spec(process_key: str) -> dict[str, Any]:
    for spec in PROCESS_SPECS:
        if spec["key"] == process_key:
            return dict(spec)
    raise ValueError(f"unknown auto-update process key: {process_key}")


def _initial_owner_policy(runtime_dir: Path) -> dict[str, Any]:
    maintenance_state = _load_json_object(runtime_dir / STATE_FILENAME)
    if not maintenance_state:
        raise RuntimeError(
            "cannot initialize owner policy without canonical maintenance audit state"
        )
    baseline = dict(maintenance_state.get("baseline") or {})
    baseline_timers = dict(baseline.get("timers") or {})
    schedule_baseline = dict(maintenance_state.get("runtime_schedule_baseline") or {})
    warehouse_state = _load_json_object(
        runtime_dir / WAREHOUSE_MAINTENANCE_STATE_FILENAME
    )
    warehouse_baseline = dict(
        (((warehouse_state or {}).get("baseline") or {}).get("units") or {}).get(
            "timer"
        )
        or {}
    )
    processes: dict[str, dict[str, Any]] = {}
    for spec in PROCESS_SPECS:
        timer = str(spec["timer"])
        timer_evidence = dict(baseline_timers.get(timer) or {})
        evidence_source = "business_data_maintenance.baseline"
        if spec["key"] == "warehouse_functional" and warehouse_baseline:
            timer_evidence = warehouse_baseline
            evidence_source = "warehouse_functional_maintenance.baseline"
        if not timer_evidence:
            desired: bool | None = None
        else:
            desired = (
                str(timer_evidence.get("is_enabled") or "") == "enabled"
                and str(timer_evidence.get("is_active") or "") == "active"
            )
        schedule_key = str(spec.get("schedule") or "")
        schedule_evidence = dict(schedule_baseline.get(schedule_key) or {})
        if schedule_key == "web_vitrina" and schedule_evidence:
            desired = bool(
                [
                    item
                    for item in schedule_evidence.get("schedules", [])
                    if isinstance(item, Mapping) and bool(item.get("enabled"))
                ]
            )
        elif schedule_key == "feedback_complaints" and schedule_evidence:
            desired = bool(
                [
                    item
                    for item in schedule_evidence.get("schedules", [])
                    if isinstance(item, Mapping) and bool(item.get("enabled"))
                ]
            )
        elif schedule_key == "spp" and schedule_evidence:
            desired = bool((schedule_evidence.get("schedule") or {}).get("enabled"))
        evidence = {
            "source": evidence_source,
            "timer": timer_evidence,
            "schedule": schedule_evidence,
            "maintenance_hold_started_at": maintenance_state.get("hold_started_at"),
            "maintenance_baseline_captured_at": baseline.get("captured_at"),
        }
        processes[str(spec["key"])] = {
            "process_key": spec["key"],
            "display_name": spec["display_name"],
            "desired": desired,
            "provenance": "proven" if desired is not None else "unknown",
            "evidence": evidence,
            "fingerprint": _stable_fingerprint(evidence),
        }
    created_at = _utc_now()
    policy = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "revision": 1,
        "master_desired": False,
        "created_at": created_at,
        "changed_at": created_at,
        "actor": "initial_migration",
        "reason": "migrated from canonical pre-hold maintenance evidence",
        "processes": processes,
        "runtime_schedule_baseline": schedule_baseline,
        "migration_evidence": {
            "business_maintenance_state_fingerprint": _stable_fingerprint(
                maintenance_state
            ),
            "warehouse_maintenance_state_fingerprint": _stable_fingerprint(
                warehouse_state
            )
            if warehouse_state
            else None,
        },
    }
    policy["policy_fingerprint"] = _stable_fingerprint(
        {key: value for key, value in policy.items() if key != "policy_fingerprint"}
    )
    _save_json_0600(runtime_dir / POLICY_FILENAME, policy)
    _append_audit_0600(
        runtime_dir / POLICY_AUDIT_FILENAME,
        {
            "event": "initial_policy_migrated",
            "captured_at": created_at,
            "revision": 1,
            "policy_fingerprint": policy["policy_fingerprint"],
            "migration_evidence": policy["migration_evidence"],
        },
    )
    return policy


def load_or_initialize_owner_policy(runtime_dir: Path) -> dict[str, Any]:
    return _load_json_object(runtime_dir / POLICY_FILENAME) or _initial_owner_policy(
        runtime_dir
    )


def update_process_desired_state(
    runtime_dir: Path,
    *,
    process_key: str,
    desired: bool,
    expected_revision: int,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    _process_spec(process_key)
    policy = load_or_initialize_owner_policy(runtime_dir)
    if int(policy.get("revision") or 0) != int(expected_revision):
        raise RuntimeError(
            f"stale policy revision: expected {expected_revision}, "
            f"current {policy.get('revision')}"
        )
    processes = dict(policy.get("processes") or {})
    process = dict(processes.get(process_key) or {})
    before = process.get("desired")
    process.update(
        {
            "desired": bool(desired),
            "provenance": "explicit_owner_policy",
            "changed_at": _utc_now(),
            "changed_by": str(actor or "unknown"),
            "change_reason": str(reason or "owner settings change"),
        }
    )
    process["fingerprint"] = _stable_fingerprint(
        {key: value for key, value in process.items() if key != "fingerprint"}
    )
    processes[process_key] = process
    policy.update(
        {
            "revision": int(policy["revision"]) + 1,
            "processes": processes,
            "changed_at": _utc_now(),
            "actor": str(actor or "unknown"),
            "reason": str(reason or "owner settings change"),
        }
    )
    policy["policy_fingerprint"] = _stable_fingerprint(
        {key: value for key, value in policy.items() if key != "policy_fingerprint"}
    )
    _save_json_0600(runtime_dir / POLICY_FILENAME, policy)
    _append_audit_0600(
        runtime_dir / POLICY_AUDIT_FILENAME,
        {
            "event": "process_desired_changed",
            "captured_at": _utc_now(),
            "process_key": process_key,
            "before": before,
            "after": bool(desired),
            "actor": actor,
            "reason": reason,
            "revision": policy["revision"],
            "policy_fingerprint": policy["policy_fingerprint"],
        },
    )
    return policy


def _process_actual_state(
    spec: Mapping[str, Any],
    *,
    status: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    timer = dict((status.get("timers") or {}).get(str(spec["timer"])) or {})
    schedule_key = str(spec.get("schedule") or "")
    schedule = dict((status.get("runtime_schedules") or {}).get(schedule_key) or {})
    timer_on = (
        str(timer.get("is_enabled") or "") == "enabled"
        and str(timer.get("is_active") or "") == "active"
    )
    actual = timer_on
    if schedule_key == "web_vitrina":
        actual = timer_on and bool(schedule.get("enabled_ids"))
    elif schedule_key == "feedback_complaints":
        actual = timer_on and bool(schedule.get("enabled_ids"))
    elif schedule_key == "spp":
        actual = timer_on and bool(schedule.get("enabled"))
    process = dict((policy.get("processes") or {}).get(str(spec["key"])) or {})
    desired = process.get("desired")
    drift = (
        "unknown"
        if desired is None
        else "matched"
        if bool(desired) == bool(actual)
        else "drift"
    )
    properties = dict(timer.get("properties") or {})
    return {
        "process_key": spec["key"],
        "display_name": spec["display_name"],
        "desired": desired,
        "actual": bool(actual),
        "drift_status": drift,
        "timer": timer,
        "runtime_schedule": schedule,
        "last_run": str(properties.get("LastTriggerUSec") or ""),
        "last_success": (
            str(properties.get("LastTriggerUSec") or "")
            if str(properties.get("Result") or "success") == "success"
            else ""
        ),
        "next_run": str(properties.get("NextElapseUSecRealtime") or ""),
        "last_error": (
            ""
            if str(properties.get("Result") or "success") == "success"
            else str(properties.get("Result") or "unknown")
        ),
        "schedule": schedule,
        "fingerprint": process.get("fingerprint"),
        "provenance": process.get("provenance"),
    }


def owner_policy_readback(
    runtime_dir: Path,
    *,
    status: Mapping[str, Any],
) -> dict[str, Any]:
    policy = load_or_initialize_owner_policy(runtime_dir)
    processes = [
        _process_actual_state(spec, status=status, policy=policy)
        for spec in PROCESS_SPECS
    ]
    unknown = [item["process_key"] for item in processes if item["desired"] is None]
    drift = [
        item["process_key"]
        for item in processes
        if item["drift_status"] == "drift"
    ]
    if not bool(policy.get("master_desired")):
        overall = "Общая пауза включена"
    elif unknown:
        overall = "Состояние не подтверждено"
    elif drift:
        overall = "Есть расхождение или ошибка"
    elif any(item["desired"] is False for item in processes):
        overall = "Часть обновлений выключена"
    else:
        overall = "Все запланированные обновления работают"
    return {
        "schema_version": POLICY_SCHEMA_VERSION,
        "master_desired": bool(policy.get("master_desired")),
        "revision": int(policy.get("revision") or 0),
        "policy_fingerprint": str(policy.get("policy_fingerprint") or ""),
        "changed_at": str(policy.get("changed_at") or ""),
        "actor": str(policy.get("actor") or ""),
        "reason": str(policy.get("reason") or ""),
        "overall_status": overall,
        "unknown_processes": unknown,
        "drift_processes": drift,
        "processes": processes,
        "audit_path": str(runtime_dir / POLICY_AUDIT_FILENAME),
    }


def _set_master_policy_paused(
    runtime_dir: Path,
    *,
    actor: str,
    reason: str,
    expected_revision: int | None = None,
    runtime_schedule_baseline: Mapping[str, Mapping[str, Any]] | None = None,
    pre_hold_readback: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    policy = load_or_initialize_owner_policy(runtime_dir)
    if expected_revision is not None and int(policy.get("revision") or 0) != int(
        expected_revision
    ):
        raise RuntimeError(
            f"stale policy revision: expected {expected_revision}, "
            f"current {policy.get('revision')}"
        )
    if not bool(policy.get("master_desired")):
        return policy
    policy.update(
        {
            "master_desired": False,
            "revision": int(policy.get("revision") or 0) + 1,
            "changed_at": _utc_now(),
            "actor": actor,
            "reason": reason,
            "runtime_schedule_baseline": (
                dict(runtime_schedule_baseline)
                if runtime_schedule_baseline is not None
                else dict(policy.get("runtime_schedule_baseline") or {})
            ),
            "pre_hold_readback": dict(pre_hold_readback or {}),
        }
    )
    policy["policy_fingerprint"] = _stable_fingerprint(
        {key: value for key, value in policy.items() if key != "policy_fingerprint"}
    )
    _save_json_0600(runtime_dir / POLICY_FILENAME, policy)
    _append_audit_0600(
        runtime_dir / POLICY_AUDIT_FILENAME,
        {
            "event": "master_paused",
            "captured_at": _utc_now(),
            "revision": policy["revision"],
            "actor": actor,
            "reason": reason,
            "policy_fingerprint": policy["policy_fingerprint"],
        },
    )
    return policy


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
    result = {
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
    # Status/Settings GET is a readback boundary and must never create policy
    # state.  Initial migration happens only inside an explicit hold/update
    # mutation, where the canonical pre-hold evidence is already durable.
    if (runtime_dir / POLICY_FILENAME).is_file():
        result["auto_updates"] = owner_policy_readback(runtime_dir, status=result)
    else:
        result["auto_updates"] = {
            "schema_version": POLICY_SCHEMA_VERSION,
            "master_desired": False,
            "revision": 0,
            "overall_status": "Состояние не подтверждено",
            "unknown_processes": [str(item["key"]) for item in PROCESS_SPECS],
            "drift_processes": [],
            "processes": [],
        }
    return result


def maintenance_hold(
    runtime_dir: Path,
    *,
    systemd: SystemdClient,
    schedules: RuntimeScheduleClient,
    proc_root: Path = Path("/proc"),
    wait_timeout_seconds: float = 1200.0,
    poll_interval_seconds: float = 2.0,
    actor: str = "business_data_maintenance",
    reason: str = "canonical cross-writer hold",
    expected_revision: int | None = None,
) -> dict[str, Any]:
    prepared = maintenance_prepare(
        runtime_dir,
        systemd=systemd,
        schedules=schedules,
        proc_root=proc_root,
        actor=actor,
        reason=reason,
        expected_revision=expected_revision,
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


def maintenance_restore(
    runtime_dir: Path,
    *,
    systemd: SystemdClient,
    schedules: RuntimeScheduleClient,
    proc_root: Path = Path("/proc"),
    actor: str = "repo_owned_cli",
    reason: str = "bounded recovery completed",
    expected_revision: int | None = None,
    warehouse_restore: Any | None = None,
) -> dict[str, Any]:
    policy = load_or_initialize_owner_policy(runtime_dir)
    revision = int(policy.get("revision") or 0)
    if expected_revision is not None and revision != int(expected_revision):
        raise RuntimeError(
            f"stale policy revision: expected {expected_revision}, current {revision}"
        )
    desired = {
        key: value.get("desired")
        for key, value in dict(policy.get("processes") or {}).items()
        if isinstance(value, Mapping)
    }
    unknown = sorted(key for key, value in desired.items() if value is None)
    if unknown:
        raise RuntimeError(
            "unsafe resume blocked by unknown intended process states: "
            + ",".join(unknown)
        )
    if bool(desired.get("autoanswers_readonly")) or bool(
        desired.get("autoanswers_worker")
    ):
        raise RuntimeError(
            "Autoanswers ON requires its dedicated lifecycle contract; owner policy remains fail-closed"
        )
    before = maintenance_status(
        runtime_dir,
        systemd=systemd,
        schedules=schedules,
        proc_root=proc_root,
    )
    if bool(policy.get("master_desired")):
        readback = owner_policy_readback(runtime_dir, status=before)
        if not readback["unknown_processes"] and not readback["drift_processes"]:
            return {
                **before,
                "status": "restored",
                "idempotent": True,
                "auto_updates": readback,
            }
        raise RuntimeError(
            "owner policy is already resumed but desired/actual drift exists: "
            + ",".join(readback["drift_processes"] or readback["unknown_processes"])
        )
    if before["unknown_wb_core_timers"]:
        raise RuntimeError(
            "unknown wb-core timers block resume: "
            + ",".join(before["unknown_wb_core_timers"])
        )
    if before["cron_entries"]:
        raise RuntimeError("cron drift blocks auto-update resume")
    if before["writer_processes"]:
        raise RuntimeError("writer processes are still active")
    if any(
        bool(value.get("held"))
        for key, value in before["writer_locks"].items()
        if key != "seller_portal"
    ) or bool((before["writer_locks"].get("seller_portal") or {}).get("busy")):
        raise RuntimeError("maintenance/shared lock is still held")
    if not before["quiet"]:
        raise RuntimeError("business-data maintenance is not quiet before resume")
    schedule_baseline = dict(policy.get("runtime_schedule_baseline") or {})
    try:
        schedules.restore_selected(
            schedule_baseline,
            desired={key: bool(value) for key, value in desired.items()},
        )
        for spec in PROCESS_SPECS:
            key = str(spec["key"])
            unit = str(spec["timer"])
            if key == "warehouse_functional":
                continue
            if bool(desired.get(key)):
                systemd.enable_now(unit)
            else:
                systemd.disable_now(unit)
        if bool(desired.get("warehouse_functional")):
            if warehouse_restore is None:
                from packages.application.warehouse_functional_maintenance import (
                    maintenance_restore as restore_warehouse_timer,
                )

                warehouse_result = restore_warehouse_timer(runtime_dir)
            else:
                warehouse_result = warehouse_restore(runtime_dir)
            if str((warehouse_result or {}).get("status") or "") != "restored":
                raise RuntimeError("warehouse timer restore did not return restored status")
        else:
            systemd.disable_now("wb-core-warehouse-functional-sync.timer")
    except Exception as exc:
        for unit in ALL_BUSINESS_TIMER_UNITS:
            systemd.disable_now(unit)
        schedules.disable_all(schedules.read_all())
        _append_audit_0600(
            runtime_dir / POLICY_AUDIT_FILENAME,
            {
                "event": "master_resume_failed",
                "captured_at": _utc_now(),
                "revision": revision,
                "actor": actor,
                "reason": reason,
                "error": str(exc),
            },
        )
        raise

    after = maintenance_status(
        runtime_dir,
        systemd=systemd,
        schedules=schedules,
        proc_root=proc_root,
    )
    preview_policy = dict(policy)
    preview_policy["master_desired"] = True
    actual = [
        _process_actual_state(spec, status=after, policy=preview_policy)
        for spec in PROCESS_SPECS
    ]
    drift = [item["process_key"] for item in actual if item["drift_status"] != "matched"]
    if drift:
        for unit in ALL_BUSINESS_TIMER_UNITS:
            systemd.disable_now(unit)
        schedules.disable_all(schedules.read_all())
        raise RuntimeError("post-resume desired/actual drift: " + ",".join(drift))
    policy.update(
        {
            "master_desired": True,
            "revision": revision + 1,
            "changed_at": _utc_now(),
            "actor": actor,
            "reason": reason,
            "pre_resume_readback": {
                "captured_at": before.get("captured_at"),
                "quiet": before.get("quiet"),
            },
            "post_resume_readback": {
                "captured_at": after.get("captured_at"),
                "processes": actual,
            },
        }
    )
    policy["policy_fingerprint"] = _stable_fingerprint(
        {key: value for key, value in policy.items() if key != "policy_fingerprint"}
    )
    _save_json_0600(runtime_dir / POLICY_FILENAME, policy)
    _append_audit_0600(
        runtime_dir / POLICY_AUDIT_FILENAME,
        {
            "event": "master_resumed",
            "captured_at": _utc_now(),
            "revision": policy["revision"],
            "actor": actor,
            "reason": reason,
            "policy_fingerprint": policy["policy_fingerprint"],
            "processes": actual,
        },
    )
    final = maintenance_status(
        runtime_dir,
        systemd=systemd,
        schedules=schedules,
        proc_root=proc_root,
    )
    state_path = runtime_dir / STATE_FILENAME
    state = _load_json_object(state_path) or {}
    state.update(
        {
            "phase": "restored",
            "restored_at": _utc_now(),
            "restore_policy_revision": policy["revision"],
            "restore_readback": final,
        }
    )
    _save_json_0600(state_path, state)
    _append_audit_0600(
        runtime_dir / AUDIT_FILENAME,
        {
            "event": "hold_restored",
            "captured_at": _utc_now(),
            "policy_revision": policy["revision"],
            "status": final,
        },
    )
    return {
        **final,
        "status": "restored",
        "idempotent": False,
        "auto_updates": owner_policy_readback(runtime_dir, status=final),
    }


def maintenance_prepare(
    runtime_dir: Path,
    *,
    systemd: SystemdClient,
    schedules: RuntimeScheduleClient,
    proc_root: Path = Path("/proc"),
    actor: str = "business_data_maintenance",
    reason: str = "canonical cross-writer hold",
    expected_revision: int | None = None,
) -> dict[str, Any]:
    state_path = runtime_dir / STATE_FILENAME
    audit_path = runtime_dir / AUDIT_FILENAME
    existing: dict[str, Any] | None = None
    if state_path.is_file():
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise RuntimeError("business maintenance state is not a JSON object")
        if str(loaded.get("phase") or "") not in {"restored", "released"}:
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
    _set_master_policy_paused(
        runtime_dir,
        actor=actor,
        reason=reason,
        expected_revision=expected_revision,
        runtime_schedule_baseline=before_payloads,
        pre_hold_readback=before,
    )

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
    parser.add_argument(
        "action",
        choices=("status", "prepare", "hold", "restore", "set-process"),
    )
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--wait-timeout-seconds", type=float, default=1200.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=2.0)
    parser.add_argument("--process-key", default="")
    parser.add_argument("--desired", choices=("on", "off"), default="")
    parser.add_argument("--expected-revision", type=int)
    parser.add_argument("--actor", default="repo_owned_cli")
    parser.add_argument("--reason", default="")
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
    elif args.action == "hold":
        result = maintenance_hold(
            runtime_dir,
            systemd=systemd,
            schedules=schedules,
            wait_timeout_seconds=args.wait_timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
        )
    elif args.action == "restore":
        result = maintenance_restore(
            runtime_dir,
            systemd=systemd,
            schedules=schedules,
            actor=args.actor,
            reason=args.reason or "bounded recovery completed",
            expected_revision=args.expected_revision,
        )
    else:
        if not args.process_key or not args.desired or args.expected_revision is None:
            raise ValueError(
                "set-process requires --process-key, --desired and --expected-revision"
            )
        policy = update_process_desired_state(
            runtime_dir,
            process_key=args.process_key,
            desired=args.desired == "on",
            expected_revision=args.expected_revision,
            actor=args.actor,
            reason=args.reason or "owner settings change",
        )
        status = maintenance_status(runtime_dir, systemd=systemd, schedules=schedules)
        result = {
            "status": "updated",
            "policy": policy,
            "auto_updates": owner_policy_readback(runtime_dir, status=status),
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
