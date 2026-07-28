#!/usr/bin/env python3
"""Audited quiet window for all repo-owned automatic business-data writers."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
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
from packages.application.business_data_write_barrier import (  # noqa: E402
    abort_barrier_acquire,
    acquire_barrier,
    barrier_status,
    confirm_barrier_hold,
    mark_barrier_restoring,
    release_barrier,
)


SCHEMA_VERSION = "business_data_maintenance_v1"
STATE_FILENAME = ".business-data-maintenance.json"
AUDIT_FILENAME = ".business-data-maintenance-audit.jsonl"
POLICY_SCHEMA_VERSION = "auto_updates_owner_policy_v2"
POLICY_FILENAME = ".auto-updates-policy.json"
POLICY_AUDIT_FILENAME = ".auto-updates-policy-audit.jsonl"
WAREHOUSE_MAINTENANCE_STATE_FILENAME = ".warehouse-functional-maintenance.json"
RESTORE_LOCK_FILENAME = ".business-data-maintenance-restore.lock"
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


class _ExclusiveRestoreLock:
    """Reject overlapping foreground or detached maintenance restores."""

    def __init__(self, runtime_dir: Path) -> None:
        self.path = Path(runtime_dir).resolve() / RESTORE_LOCK_FILENAME
        self.handle: Any | None = None

    def __enter__(self) -> "_ExclusiveRestoreLock":
        if self.path.is_symlink():
            raise RuntimeError(
                "business-data maintenance restore lock must not be a symlink"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(
                self.handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError(
                "another business-data maintenance restore is active"
            ) from exc
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        if self.handle is None:
            return
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


WRITER_PROCESS_MARKERS = (
    "sheet_vitrina_v1_auto_refresh_tick.py",
    "sheet_vitrina_v1_closure_retry.py",
    "sheet_vitrina_v1_temporal_closure_retry_live.py",
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
        "control_owner": "settings",
        "control_location": "Настройки → Автообновления",
        "control_capability": "manage",
        "desired_source": "auto_updates_owner_policy",
    },
    {
        "key": "vitrina_closure_retry",
        "display_name": "Закрытие и повтор закрытия данных Витрины",
        "timer": "wb-core-sheet-vitrina-closure-retry.timer",
        "control_owner": "settings",
        "control_location": "Настройки → Автообновления",
        "control_capability": "manage",
        "desired_source": "auto_updates_owner_policy",
    },
    {
        "key": "warehouse_functional",
        "display_name": "Склады и себестоимость",
        "timer": "wb-core-warehouse-functional-sync.timer",
        "control_owner": "settings",
        "control_location": "Настройки → Автообновления",
        "control_capability": "manage",
        "desired_source": "auto_updates_owner_policy",
    },
    {
        "key": "wb_finance_weekly",
        "display_name": "Финансовый отчёт WB",
        "timer": "wb-core-wb-finance-weekly.timer",
        "control_owner": "settings",
        "control_location": "Настройки → Автообновления",
        "control_capability": "manage",
        "desired_source": "auto_updates_owner_policy",
    },
    {
        "key": "feedback_complaints",
        "display_name": "Авто-жалобы",
        "timer": "wb-core-feedbacks-auto-complaints-tick.timer",
        "schedule": "feedback_complaints",
        "control_owner": "feature",
        "control_location": "Отзывы → Авто-жалобы",
        "control_capability": "monitor",
        "desired_source": "feedback_complaints_schedule",
    },
    {
        "key": "spp_test",
        "display_name": "Автоматический тест СПП",
        "timer": "wb-core-spp-tester-schedule-tick.timer",
        "schedule": "spp",
        "control_owner": "feature",
        "control_location": "Цены → Тест СПП",
        "control_capability": "monitor",
        "desired_source": "spp_test_schedule",
    },
    {
        "key": "autoanswers",
        "display_name": "Autoanswers",
        "components": {
            "readonly_sync": "wb-core-autoanswers-readonly-sync.timer",
            "worker": "wb-core-autoanswers-worker.timer",
        },
        "control_owner": "feature",
        "control_location": "Отзывы → Отзывы",
        "control_capability": "monitor",
        "desired_source": "autoanswers_feature_settings",
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
                "--property=LoadState,UnitFileState,ActiveState,SubState,Result,ExecMainCode,ExecMainStatus,MainPID,ExecMainStartTimestamp,LastTriggerUSec,NextElapseUSecRealtime",
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
        # A global hold owns execution, not feature/configuration intent.
        # Disabling timers is sufficient; runtime JSON schedules remain exact.
        return self.read_all()

    def restore_selected(
        self,
        baseline: Mapping[str, Mapping[str, Any]],
        *,
        desired: Mapping[str, bool],
    ) -> dict[str, dict[str, Any]]:
        # Schedule configuration is never rewritten by master resume.
        return self.read_all()

    def restore_legacy_hold(
        self,
        baseline: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Undo the one-time v1 hold that rewrote feature schedule JSON."""

        web = dict(baseline.get("web_vitrina") or {})
        self._request(
            WEB_SCHEDULE_PATH,
            {
                "schedule_policy": dict(web.get("schedule_policy") or {}),
                "schedules": [
                    dict(item)
                    for item in (
                        web.get("schedules")
                        or web.get("effective_schedules")
                        or []
                    )
                    if isinstance(item, Mapping)
                ],
            },
        )
        feedback = dict(baseline.get("feedback_complaints") or {})
        self._request(
            FEEDBACK_SCHEDULE_PATH,
            {
                "schedules": [
                    dict(item)
                    for item in feedback.get("schedules", [])
                    if isinstance(item, Mapping)
                ]
            },
        )
        spp = dict(baseline.get("spp") or {})
        self._request(
            SPP_SCHEDULE_PATH,
            {"schedule": dict(spp.get("schedule") or {})},
        )
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
        if spec.get("control_capability") != "manage":
            continue
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
    policy = _load_json_object(runtime_dir / POLICY_FILENAME) or _initial_owner_policy(
        runtime_dir
    )
    processes = dict(policy.get("processes") or {})
    policy = dict(policy)
    policy["schema_version"] = POLICY_SCHEMA_VERSION
    policy["processes"] = {
        str(spec["key"]): dict(processes.get(str(spec["key"])) or {})
        for spec in PROCESS_SPECS
        if spec.get("control_capability") == "manage"
    }
    return policy


def update_process_desired_state(
    runtime_dir: Path,
    *,
    process_key: str,
    desired: bool,
    expected_revision: int,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    spec = _process_spec(process_key)
    if str(spec.get("control_capability") or "") != "manage":
        raise RuntimeError(
            f"{process_key} is monitoring-only in Settings; manage it in "
            f"{spec.get('control_location')}"
        )
    policy = load_or_initialize_owner_policy(runtime_dir)
    if int(policy.get("revision") or 0) != int(expected_revision):
        raise RuntimeError(
            f"stale policy revision: expected {expected_revision}, "
            f"current {policy.get('revision')}"
        )
    processes = dict(policy.get("processes") or {})
    process = dict(processes.get(process_key) or {})
    before = process.get("desired")
    if before is not None and bool(before) == bool(desired):
        raise RuntimeError(
            f"no-op desired state for {process_key}: already "
            f"{'ON' if bool(desired) else 'OFF'}"
        )
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


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except ValueError:
        return None


def _autoanswers_budget_monitor_state(
    conn: sqlite3.Connection,
    *,
    tables: set[str],
) -> dict[str, Any]:
    """Read budget evidence without initializing schema or changing lifecycle."""

    required = {
        "sheet_vitrina_v1_wb_autoanswers_budget_reservations",
        "sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds",
        "sheet_vitrina_v1_wb_autoanswers_cost_events",
        "sheet_vitrina_v1_wb_autoanswers_failed_cost_events",
        "sheet_vitrina_v1_wb_autoanswers_budget_adjustments",
        "sheet_vitrina_v1_wb_autoanswer_jobs",
    }
    missing = sorted(required - tables)
    if missing:
        return {
            "budget_state": "unknown",
            "confirmed_actual_usd": None,
            "active_reserved_usd": None,
            "uncertainty_hold_usd": None,
            "uncertainty_hold_count": None,
            "unresolved_uncertainty_count": None,
            "last_budget_evidence_at": None,
            "hold_explanation": (
                "Бюджет не подтверждён: отсутствуют runtime-таблицы "
                + ", ".join(missing)
            ),
        }
    reservations = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN status='settled' THEN actual_cost_usd ELSE 0 END),0),
            COALESCE(SUM(CASE WHEN status='reserved' THEN reserved_usd ELSE 0 END),0),
            MAX(updated_at)
        FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations
        """
    ).fetchone()
    cost_events = conn.execute(
        """
        SELECT COALESCE(SUM(actual_cost_usd),0),MAX(incurred_at)
        FROM sheet_vitrina_v1_wb_autoanswers_cost_events
        """
    ).fetchone()
    failed_cost_events = conn.execute(
        """
        SELECT COALESCE(SUM(actual_cost_usd),0),MAX(incurred_at)
        FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events
        """
    ).fetchone()
    adjustments = conn.execute(
        """
        SELECT COALESCE(SUM(amount_usd),0),MAX(effective_at)
        FROM sheet_vitrina_v1_wb_autoanswers_budget_adjustments
        """
    ).fetchone()
    holds = conn.execute(
        """
        SELECT COALESCE(SUM(upper_bound_usd),0),COUNT(*),MAX(created_at)
        FROM sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds
        """
    ).fetchone()
    if "sheet_vitrina_v1_wb_autoanswers_provider_uncertainty_attempts" in tables:
        attempt_holds = conn.execute(
            """
            SELECT COALESCE(SUM(upper_bound_usd),0),COUNT(*),MAX(created_at)
            FROM sheet_vitrina_v1_wb_autoanswers_provider_uncertainty_attempts
            """
        ).fetchone()
        attempt_resolution_clause = """
          AND NOT EXISTS(
                SELECT 1
                FROM sheet_vitrina_v1_wb_autoanswers_provider_uncertainty_attempts u
                WHERE u.processing_key=r.processing_key
                  AND u.attempt_number=j.attempts
              )
        """
    else:
        attempt_holds = (0, 0, None)
        attempt_resolution_clause = ""
    unresolved = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM sheet_vitrina_v1_wb_autoanswers_budget_reservations r
        JOIN sheet_vitrina_v1_wb_autoanswer_jobs j
          ON j.processing_key=r.processing_key
        WHERE r.provider_call_started_at IS NOT NULL
          AND r.status='released'
          AND CAST(COALESCE(r.actual_cost_usd,'0') AS REAL)=0
          AND (
                j.last_error_code IN ('node_timeout','node_invalid_json')
                OR j.last_error_code LIKE 'node_process_exit_%'
              )
          AND NOT EXISTS(
                SELECT 1
                FROM sheet_vitrina_v1_wb_autoanswers_cost_events c
                WHERE c.processing_key=r.processing_key
              )
          AND NOT EXISTS(
                SELECT 1
                FROM sheet_vitrina_v1_wb_autoanswers_failed_cost_events f
                WHERE f.processing_key=r.processing_key
              )
          AND NOT EXISTS(
                SELECT 1
                FROM sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds h
                WHERE h.processing_key=r.processing_key
              )
          {attempt_resolution_clause}
        """
    ).fetchone()[0]
    confirmed_actual = sum(
        float(value or 0)
        for value in (
            reservations[0],
            cost_events[0],
            failed_cost_events[0],
            adjustments[0],
        )
    )
    hold_total = float(holds[0] or 0) + float(attempt_holds[0] or 0)
    hold_count = int(holds[1] or 0) + int(attempt_holds[1] or 0)
    unresolved_count = int(unresolved or 0)
    budget_state = (
        "unknown"
        if unresolved_count
        else "conservative_unverified"
        if hold_count
        else "confirmed"
    )
    evidence_times = [
        str(value)
        for value in (
            reservations[2],
            cost_events[1],
            failed_cost_events[1],
            adjustments[1],
            holds[2],
            attempt_holds[2],
        )
        if value
    ]
    return {
        "budget_state": budget_state,
        "confirmed_actual_usd": round(confirmed_actual, 6),
        "active_reserved_usd": round(float(reservations[1] or 0), 6),
        "uncertainty_hold_usd": round(hold_total, 6),
        "uncertainty_hold_count": hold_count,
        "unresolved_uncertainty_count": unresolved_count,
        "last_budget_evidence_at": max(evidence_times) if evidence_times else None,
        "hold_explanation": (
            "Консервативный hold — верхняя граница возможного расхода, "
            "а не подтверждённое списание."
            if hold_count
            else "Консервативных holds нет."
        ),
    }


def _autoanswers_feature_state(runtime_dir: Path) -> dict[str, Any]:
    from packages.application.wb_autoanswers_runtime import AUTOANSWERS_DB_FILENAME

    database = runtime_dir / AUTOANSWERS_DB_FILENAME
    if not database.is_file():
        return {
            "mode": "unknown",
            "policy_epoch": None,
            "transition_run_id": None,
            "last_scheduler_tick_at": None,
            "last_sync_at": None,
            "stop_reason": "settings_missing",
        }
    with sqlite3.connect(
        f"file:{database.resolve()}?mode=ro",
        uri=True,
        timeout=10,
    ) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required = {
            "sheet_vitrina_v1_wb_autoanswers_settings",
            "sheet_vitrina_v1_wb_autoanswers_runtime_state",
            "sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps",
            "sheet_vitrina_v1_wb_sync_state",
        }
        missing = sorted(required - tables)
        if missing:
            return {
                "mode": "unknown",
                "policy_epoch": None,
                "transition_run_id": None,
                "last_scheduler_tick_at": None,
                "last_sync_at": None,
                "stop_reason": "settings_missing",
                "last_error": "missing Autoanswers tables: " + ",".join(missing),
            }
        settings = conn.execute(
            """
            SELECT master_enabled,mode,policy_epoch
            FROM sheet_vitrina_v1_wb_autoanswers_settings WHERE singleton=1
            """
        ).fetchone()
        runtime = conn.execute(
            """
            SELECT stop_reason,last_scheduler_tick_at,last_successful_ai_call_at,
                   last_confirmed_publication_at
            FROM sheet_vitrina_v1_wb_autoanswers_runtime_state WHERE singleton=1
            """
        ).fetchone()
        sweep = conn.execute(
            """
            SELECT transition_run_id,run_max_usd,run_max_paid_reviews
            FROM sheet_vitrina_v1_wb_autoanswers_reconciliation_sweeps
            ORDER BY created_at DESC LIMIT 1
            """
        ).fetchone()
        last_sync = conn.execute(
            "SELECT MAX(last_success_at) FROM sheet_vitrina_v1_wb_sync_state"
        ).fetchone()[0]
        budget = _autoanswers_budget_monitor_state(conn, tables=tables)
    lifecycle = _load_json_object(runtime_dir / ".wb-autoanswers-lifecycle.json") or {}
    return {
        "mode": (
            str(settings["mode"])
            if settings is not None and bool(settings["master_enabled"])
            else "off"
        ),
        "policy_epoch": (
            int(settings["policy_epoch"]) if settings is not None else None
        ),
        "transition_run_id": (
            str(sweep["transition_run_id"]) if sweep is not None else None
        ),
        "run_max_usd": sweep["run_max_usd"] if sweep is not None else None,
        "run_max_paid_reviews": (
            sweep["run_max_paid_reviews"] if sweep is not None else None
        ),
        "last_scheduler_tick_at": (
            str(runtime["last_scheduler_tick_at"]) if runtime is not None else None
        ),
        "last_successful_ai_call_at": (
            str(runtime["last_successful_ai_call_at"])
            if runtime is not None
            else None
        ),
        "last_confirmed_publication_at": (
            str(runtime["last_confirmed_publication_at"])
            if runtime is not None
            else None
        ),
        "last_sync_at": str(last_sync) if last_sync else None,
        "stop_reason": str(runtime["stop_reason"] or "") if runtime is not None else "",
        "lifecycle": lifecycle,
        "budget": budget,
    }


def _autoanswers_process_actual_state(
    *,
    status: Mapping[str, Any],
    policy: Mapping[str, Any],
    runtime_dir: Path,
    spec: Mapping[str, Any],
) -> dict[str, Any]:
    feature = _autoanswers_feature_state(runtime_dir)
    mode = str(feature.get("mode") or "unknown")
    feature_desired: bool | None = None if mode == "unknown" else mode != "off"
    suspended = not bool(policy.get("master_desired"))
    components: dict[str, Any] = {}
    for component_key, timer_unit in dict(spec.get("components") or {}).items():
        timer = dict((status.get("timers") or {}).get(str(timer_unit)) or {})
        service_unit = str(timer_unit).removesuffix(".timer") + ".service"
        service = dict((status.get("services") or {}).get(service_unit) or {})
        timer_on = (
            str(timer.get("is_enabled") or "") == "enabled"
            and str(timer.get("is_active") or "") == "active"
        )
        desired = bool(
            not suspended
            and (
                str(component_key) == "readonly_sync"
                or mode in {"manual", "draft_only", "auto_safe", "auto_all"}
            )
        )
        timer_properties = dict(timer.get("properties") or {})
        service_properties = dict(service.get("properties") or {})
        result = str(service_properties.get("Result") or "success")
        components[str(component_key)] = {
            "component_key": str(component_key),
            "desired": desired,
            "actual": timer_on,
            "drift_status": "matched" if desired == timer_on else "drift",
            "timer": timer,
            "service": service,
            "last_run": str(timer_properties.get("LastTriggerUSec") or ""),
            "last_success": (
                str(timer_properties.get("LastTriggerUSec") or "")
                if result == "success"
                else ""
            ),
            "next_run": str(
                timer_properties.get("NextElapseUSecRealtime") or ""
            ),
            "last_error": "" if result == "success" else result,
        }
    drift_components = [
        key
        for key, value in components.items()
        if value.get("drift_status") != "matched"
    ]
    lifecycle = dict(feature.get("lifecycle") or {})
    lifecycle_identity_matches = bool(
        lifecycle
        and str(lifecycle.get("requested_mode") or "") == mode
        and int(
            lifecycle.get("policy_epoch")
            if lifecycle.get("policy_epoch") is not None
            else -1
        )
        == int(
            feature.get("policy_epoch")
            if feature.get("policy_epoch") is not None
            else -2
        )
        and bool(lifecycle.get("suspended_by_master")) == suspended
        and (
            mode not in {"draft_only", "auto_safe", "auto_all"}
            or str(lifecycle.get("transition_run_id") or "")
            == str(feature.get("transition_run_id") or "")
        )
    )
    requested_at = _parse_timestamp(lifecycle.get("requested_at"))
    last_tick = _parse_timestamp(feature.get("last_scheduler_tick_at"))
    now = datetime.now(timezone.utc)
    fresh_tick = bool(
        mode in {"manual", "draft_only", "auto_safe", "auto_all"}
        and last_tick is not None
        and last_tick >= now - timedelta(minutes=3)
        and (requested_at is None or last_tick >= requested_at)
    )
    stop_reason = str(feature.get("stop_reason") or "")
    if (
        mode in {"draft_only", "auto_safe", "auto_all"}
        and (
            not str(feature.get("transition_run_id") or "")
            or (
                feature.get("run_max_usd") in {None, ""}
                and feature.get("run_max_paid_reviews") in {None, ""}
            )
        )
    ):
        stop_reason = "run_cap_missing"
    elif (
        stop_reason == "worker_unavailable"
        and requested_at is not None
        and requested_at > now - timedelta(minutes=3)
    ):
        # Match the feature-owned lifecycle contract: a newly resumed timer
        # receives one scheduler interval to produce its first post-request
        # tick.  Treating the stale pre-request worker_unavailable marker as
        # immediate drift makes an otherwise exact outer restore impossible.
        stop_reason = ""
    elif (
        mode in {"manual", "draft_only", "auto_safe", "auto_all"}
        and not fresh_tick
        and (
            requested_at is None
            or requested_at <= now - timedelta(minutes=3)
        )
    ):
        stop_reason = "worker_unavailable"
    blocking = stop_reason in {
        "budget_state_unknown",
        "openai_quota_exhausted",
        "run_cap_missing",
        "worker_unavailable",
        "worker_error",
    }
    if suspended:
        lifecycle_state = (
            "error"
            if drift_components
            else "suspended_by_master"
            if lifecycle_identity_matches
            else "unconfirmed"
        )
    elif drift_components:
        lifecycle_state = "error"
    elif not lifecycle_identity_matches:
        lifecycle_state = "unconfirmed"
    elif mode == "off":
        lifecycle_state = "off"
    elif blocking:
        lifecycle_state = "error"
    elif fresh_tick:
        lifecycle_state = "running"
    else:
        lifecycle_state = "starting"
    worker = dict(components.get("worker") or {})
    readonly = dict(components.get("readonly_sync") or {})
    actual = bool(
        not suspended
        and mode != "off"
        and not drift_components
        and lifecycle_identity_matches
        and fresh_tick
        and not blocking
    )
    last_error = str(
        lifecycle.get("last_error") or feature.get("last_error") or ""
    )
    if blocking and not last_error:
        last_error = stop_reason
    if not lifecycle_identity_matches and not last_error:
        last_error = "persisted lifecycle identity is not confirmed"
    return {
        "process_key": "autoanswers",
        "display_name": str(spec["display_name"]),
        "control_owner": str(spec["control_owner"]),
        "control_location": str(spec["control_location"]),
        "control_capability": "monitor",
        "desired_source": str(spec["desired_source"]),
        "desired": feature_desired,
        "business_mode": mode,
        "actual": actual,
        "lifecycle_state": lifecycle_state,
        "last_run": str(worker.get("last_run") or readonly.get("last_run") or ""),
        "last_success": str(
            worker.get("last_success") or readonly.get("last_success") or ""
        ),
        "next_run": str(worker.get("next_run") or readonly.get("next_run") or ""),
        "last_error": last_error,
        "runtime_schedule": {
            "policy_epoch": feature.get("policy_epoch"),
            "transition_run_id": feature.get("transition_run_id"),
            "last_scheduler_tick_at": feature.get("last_scheduler_tick_at"),
            "last_sync_at": feature.get("last_sync_at"),
            "last_successful_ai_call_at": feature.get(
                "last_successful_ai_call_at"
            ),
            "last_confirmed_publication_at": feature.get(
                "last_confirmed_publication_at"
            ),
        },
        "drift_status": (
            "drift"
            if drift_components
            else "unknown"
            if not lifecycle_identity_matches
            else "matched"
            if suspended
            else "blocked"
            if blocking
            else "matched"
        ),
        "suspended_by_master": suspended,
        "component_states": components,
        "components": components,
        "stop_reason": stop_reason,
        "budget_state": (
            str(dict(feature.get("budget") or {}).get("budget_state") or "unknown")
        ),
        "budget": dict(feature.get("budget") or {}),
        "fresh_scheduler_tick": fresh_tick,
        "provenance": "feature_settings+systemd+lifecycle",
    }


def _process_actual_state(
    spec: Mapping[str, Any],
    *,
    status: Mapping[str, Any],
    policy: Mapping[str, Any],
    runtime_dir: Path,
) -> dict[str, Any]:
    if str(spec.get("key") or "") == "autoanswers":
        return _autoanswers_process_actual_state(
            status=status,
            policy=policy,
            runtime_dir=runtime_dir,
            spec=spec,
        )
    timer = dict((status.get("timers") or {}).get(str(spec["timer"])) or {})
    service_unit = str(spec["timer"]).removesuffix(".timer") + ".service"
    service = dict((status.get("services") or {}).get(service_unit) or {})
    schedule_key = str(spec.get("schedule") or "")
    schedule = dict((status.get("runtime_schedules") or {}).get(schedule_key) or {})
    timer_on = (
        str(timer.get("is_enabled") or "") == "enabled"
        and str(timer.get("is_active") or "") == "active"
    )
    capability = str(spec.get("control_capability") or "manage")
    process = dict((policy.get("processes") or {}).get(str(spec["key"])) or {})
    if capability == "monitor" and schedule_key == "feedback_complaints":
        desired: bool | None = bool(schedule.get("enabled_ids"))
    elif capability == "monitor" and schedule_key == "spp":
        desired = bool(schedule.get("enabled"))
    else:
        desired = process.get("desired")
    suspended = not bool(policy.get("master_desired"))
    if schedule_key == "feedback_complaints":
        actual = timer_on and bool(schedule.get("enabled_ids"))
    elif schedule_key == "spp":
        actual = timer_on and bool(schedule.get("enabled"))
    else:
        actual = timer_on
    effective_timer_desired = (
        None if desired is None else bool(desired) and not suspended
    )
    drift = (
        "unknown"
        if effective_timer_desired is None
        else "matched"
        if bool(effective_timer_desired) == bool(timer_on)
        else "drift"
    )
    properties = dict(timer.get("properties") or {})
    service_properties = dict(service.get("properties") or {})
    result = str(service_properties.get("Result") or "success")
    return {
        "process_key": spec["key"],
        "display_name": spec["display_name"],
        "control_owner": str(spec.get("control_owner") or "settings"),
        "control_location": str(
            spec.get("control_location") or "Настройки → Автообновления"
        ),
        "control_capability": capability,
        "desired_source": str(
            spec.get("desired_source") or "auto_updates_owner_policy"
        ),
        "desired": desired,
        "actual": bool(actual),
        "lifecycle_state": (
            "suspended_by_master"
            if suspended and drift == "matched"
            else "unconfirmed"
            if drift == "unknown"
            else "running"
            if actual
            else "off"
            if desired is False
            else "error"
        ),
        "drift_status": drift,
        "suspended_by_master": suspended,
        "timer": timer,
        "service": service,
        "component_states": {
            "timer": {
                "desired": effective_timer_desired,
                "actual": timer_on,
                "timer": timer,
                "service": service,
            }
        },
        "runtime_schedule": schedule,
        "last_run": str(properties.get("LastTriggerUSec") or ""),
        "last_success": (
            str(properties.get("LastTriggerUSec") or "")
            if result == "success"
            else ""
        ),
        "next_run": str(properties.get("NextElapseUSecRealtime") or ""),
        "last_error": "" if result == "success" else result,
        "schedule": schedule,
        "fingerprint": process.get("fingerprint"),
        "provenance": (
            process.get("provenance")
            if capability == "manage"
            else str(spec.get("desired_source") or "feature")
        ),
    }


def _with_operator_process_status(
    process: Mapping[str, Any],
    *,
    master_desired: bool,
) -> dict[str, Any]:
    result = dict(process)
    desired = result.get("desired")
    lifecycle = str(result.get("lifecycle_state") or "")
    drift = str(result.get("drift_status") or "")
    stop_reason = str(result.get("stop_reason") or "")
    if not master_desired:
        code = "global_pause"
    elif desired is None:
        code = "unknown"
    elif desired is False or lifecycle == "off":
        code = "user_pause"
    elif stop_reason == "worker_unavailable" or (
        result.get("process_key") == "autoanswers"
        and desired is True
        and result.get("fresh_scheduler_tick") is False
        and lifecycle != "starting"
    ):
        code = "stale"
    elif drift == "drift":
        code = "drift"
    elif lifecycle == "error" or bool(result.get("last_error")):
        code = "process_error"
    elif lifecycle == "starting":
        code = "starting"
    elif result.get("actual") is True and drift == "matched":
        code = "healthy"
    else:
        code = "unknown"
    labels = {
        "healthy": "Работает штатно",
        "starting": "Запускается",
        "user_pause": "Приостановлено пользователем",
        "global_pause": "Приостановлено общей паузой",
        "drift": "Есть расхождение",
        "process_error": "Ошибка процесса",
        "stale": "Нет свежего подтверждения",
        "unknown": "Состояние неизвестно",
    }
    explanations = {
        "healthy": "Desired и actual совпадают; runtime readback свежий.",
        "starting": "Запуск запрошен, ожидается первое свежее подтверждение.",
        "user_pause": "Процесс выключен владельцем в функциональном разделе.",
        "global_pause": "Desired сохранён, но выполнение удерживается общей паузой.",
        "drift": "Desired и actual не совпадают.",
        "process_error": "Runtime сообщил ошибку процесса.",
        "stale": "Scheduler tick или runtime readback устарел.",
        "unknown": "Недостаточно evidence для подтверждения состояния.",
    }
    result["operator_status_code"] = code
    result["operator_status"] = labels[code]
    result["operator_explanation"] = explanations[code]
    return result


def owner_policy_readback(
    runtime_dir: Path,
    *,
    status: Mapping[str, Any],
) -> dict[str, Any]:
    policy = load_or_initialize_owner_policy(runtime_dir)
    raw_processes = [
        _process_actual_state(
            spec,
            status=status,
            policy=policy,
            runtime_dir=runtime_dir,
        )
        for spec in PROCESS_SPECS
    ]
    processes = [
        _with_operator_process_status(
            item,
            master_desired=bool(policy.get("master_desired")),
        )
        for item in raw_processes
    ]
    unknown = [item["process_key"] for item in processes if item["desired"] is None]
    drift = [
        item["process_key"]
        for item in processes
        if item["drift_status"] == "drift"
    ]
    status_codes = {str(item.get("operator_status_code") or "") for item in processes}
    if not bool(policy.get("master_desired")):
        overall_code = "global_pause"
    elif "process_error" in status_codes:
        overall_code = "process_error"
    elif "drift" in status_codes:
        overall_code = "drift"
    elif "stale" in status_codes:
        overall_code = "stale"
    elif "starting" in status_codes:
        overall_code = "starting"
    elif "unknown" in status_codes or unknown:
        overall_code = "unknown"
    elif "user_pause" in status_codes:
        overall_code = "user_pause"
    else:
        overall_code = "healthy"
    overall_labels = {
        "healthy": "Работает штатно",
        "starting": "Запускается",
        "user_pause": "Приостановлено пользователем",
        "global_pause": "Приостановлено общей паузой",
        "drift": "Есть расхождение",
        "process_error": "Ошибка процесса",
        "stale": "Нет свежего подтверждения",
        "unknown": "Состояние неизвестно",
    }
    overall_explanations = {
        "healthy": "Все включённые процессы подтверждены runtime readback.",
        "starting": "Один или несколько процессов ещё подтверждают запуск.",
        "user_pause": "Часть процессов выключена в своём функциональном разделе.",
        "global_pause": "Общая пауза временно удерживает все автоматические запуски.",
        "drift": "Desired и фактическое состояние расходятся хотя бы у одного процесса.",
        "process_error": "Хотя бы один процесс сообщил ошибку выполнения.",
        "stale": "Для включённого процесса нет свежего scheduler/runtime подтверждения.",
        "unknown": "Недостаточно runtime evidence для уверенного статуса.",
    }
    return {
        "schema_version": POLICY_SCHEMA_VERSION,
        "master_desired": bool(policy.get("master_desired")),
        "revision": int(policy.get("revision") or 0),
        "policy_fingerprint": str(policy.get("policy_fingerprint") or ""),
        "changed_at": str(policy.get("changed_at") or ""),
        "captured_at": str(status.get("captured_at") or ""),
        "actor": str(policy.get("actor") or ""),
        "reason": str(policy.get("reason") or ""),
        "overall_status_code": overall_code,
        "overall_status": overall_labels[overall_code],
        "overall_explanation": overall_explanations[overall_code],
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
    # A cross-writer hold owns execution only.  Canonical schedule JSON is
    # feature intent and must remain byte-for-byte meaningful across pause.
    runtime_quiet = (
        not runtime["web_vitrina"]["active"]
        and not runtime["feedback_complaints"]["active_runs"]
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
            "overall_status_code": "unknown",
            "overall_status": "Состояние неизвестно",
            "overall_explanation": "Owner policy ещё не инициализирована.",
            "unknown_processes": [str(item["key"]) for item in PROCESS_SPECS],
            "drift_processes": [],
            "processes": [],
        }
    return result


def maintenance_control_signature(
    status: Mapping[str, Any],
    *,
    runtime_dir: Path | None = None,
) -> dict[str, Any]:
    """Stable intended/runtime control state used for exact hold restoration."""

    auto_updates = dict(status.get("auto_updates") or {})
    process_rows = [
        dict(item)
        for item in auto_updates.get("processes", [])
        if isinstance(item, Mapping)
    ]
    runtime = dict(status.get("runtime_schedules") or {})
    web = dict(runtime.get("web_vitrina") or {})
    feedback = dict(runtime.get("feedback_complaints") or {})
    spp = dict(runtime.get("spp") or {})
    process_desired = {
        str(item.get("process_key") or ""): item.get("desired")
        for item in process_rows
    }
    if process_desired.get("autoanswers") is None:
        autoanswer_units = (
            "wb-core-autoanswers-readonly-sync.timer",
            "wb-core-autoanswers-worker.timer",
        )
        known_autoanswer_timers = [
            dict((status.get("timers") or {}).get(unit) or {})
            for unit in autoanswer_units
        ]
        if all(timer for timer in known_autoanswer_timers):
            process_desired["autoanswers"] = any(
                str(timer.get("is_enabled") or "") == "enabled"
                and str(timer.get("is_active") or "") == "active"
                for timer in known_autoanswer_timers
            )
    timer_control_intent = {
        str(spec.get("timer") or spec.get("key") or ""): process_desired.get(
            str(spec.get("key") or "")
        )
        for spec in PROCESS_SPECS
        if spec.get("timer")
    }
    timer_control_intent["autoanswers"] = process_desired.get("autoanswers")
    payload = {
        "master_desired": auto_updates.get("master_desired"),
        "process_desired": process_desired,
        "timer_control_intent": timer_control_intent,
        "runtime_schedule_intent": {
            "web_vitrina": {
                "schedule_count": int(web.get("schedule_count") or 0),
                "enabled_ids": sorted(
                    str(value) for value in web.get("enabled_ids", [])
                ),
                "schedule_policy": dict(web.get("schedule_policy") or {}),
            },
            "feedback_complaints": {
                "schedule_count": int(feedback.get("schedule_count") or 0),
                "enabled_ids": sorted(
                    str(value) for value in feedback.get("enabled_ids", [])
                ),
            },
            "spp": {
                "enabled": bool(spp.get("enabled")),
                "schedule_id": str(spp.get("schedule_id") or ""),
            },
        },
        "unknown_wb_core_timers": sorted(
            str(value) for value in status.get("unknown_wb_core_timers", [])
        ),
        "cron_entries": [
            {
                "source": str((item or {}).get("source") or ""),
                "entry": str((item or {}).get("entry") or ""),
            }
            for item in status.get("cron_entries", [])
            if isinstance(item, Mapping)
        ],
    }
    return {
        "payload": payload,
        "fingerprint": _stable_fingerprint(payload),
    }


def _parse_systemd_utc_timestamp(raw: str) -> datetime:
    value = str(raw or "").strip()
    try:
        parsed = datetime.strptime(value, "%a %Y-%m-%d %H:%M:%S UTC")
    except ValueError as exc:
        raise RuntimeError(
            f"cannot prove pre-hold service generation timestamp: {value!r}"
        ) from exc
    return parsed.replace(tzinfo=timezone.utc)


def _pre_hold_service_continuity(
    runtime_dir: Path,
    *,
    maintenance_state: Mapping[str, Any],
    current_status: Mapping[str, Any],
) -> dict[str, Any]:
    phase = str(maintenance_state.get("phase") or "")
    if phase not in {"holding", "prepared"}:
        raise RuntimeError(
            "pre-hold service-continuity restore requires holding/prepared state"
        )
    barrier = barrier_status(runtime_dir)
    if (
        barrier.get("active") is not True
        or str(barrier.get("phase") or "") != "acquiring"
        or barrier.get("hold_confirmed") is not False
    ):
        raise RuntimeError(
            "pre-hold service-continuity restore requires an unconfirmed "
            "acquiring write barrier"
        )
    hold_started_at = datetime.fromisoformat(
        str(maintenance_state.get("hold_started_at") or "").replace(
            "Z",
            "+00:00",
        )
    )
    if hold_started_at.tzinfo is None:
        raise RuntimeError("maintenance hold timestamp is not timezone-aware")
    baseline_services = dict(
        (maintenance_state.get("baseline") or {}).get("services") or {}
    )
    continuing: list[dict[str, Any]] = []
    stable_properties = (
        "LoadState",
        "UnitFileState",
        "ActiveState",
        "SubState",
        "Result",
        "ExecMainCode",
        "ExecMainStatus",
    )
    for unit, current_raw in dict(
        current_status.get("services") or {}
    ).items():
        current = dict(current_raw or {})
        if str(current.get("is_active") or "") in QUIESCENT_SERVICE_STATES:
            continue
        baseline = dict(baseline_services.get(unit) or {})
        if not baseline:
            raise RuntimeError(
                f"active service {unit} has no exact pre-hold baseline"
            )
        baseline_properties = dict(baseline.get("properties") or {})
        current_properties = dict(current.get("properties") or {})
        drift = [
            key
            for key in stable_properties
            if str(current_properties.get(key) or "")
            != str(baseline_properties.get(key) or "")
        ]
        if (
            str(current.get("is_active") or "")
            != str(baseline.get("is_active") or "")
            or str(current.get("is_enabled") or "")
            != str(baseline.get("is_enabled") or "")
            or drift
        ):
            raise RuntimeError(
                f"active service {unit} drifted from its exact pre-hold state"
            )
        main_pid = int(current_properties.get("MainPID") or 0)
        started_at_raw = str(
            current_properties.get("ExecMainStartTimestamp") or ""
        )
        baseline_main_pid = int(
            baseline_properties.get("MainPID") or 0
        )
        baseline_started_at = str(
            baseline_properties.get("ExecMainStartTimestamp") or ""
        )
        if baseline_main_pid and baseline_main_pid != main_pid:
            raise RuntimeError(
                f"active service {unit} PID changed after the hold began"
            )
        if baseline_started_at and baseline_started_at != started_at_raw:
            raise RuntimeError(
                f"active service {unit} start timestamp changed after the hold began"
            )
        started_at = _parse_systemd_utc_timestamp(started_at_raw)
        if main_pid <= 0 or started_at > hold_started_at:
            raise RuntimeError(
                f"active service {unit} is not a proven pre-hold generation"
            )
        continuing.append(
            {
                "unit": unit,
                "main_pid": main_pid,
                "started_at": started_at_raw,
                "baseline_active_state": str(
                    baseline.get("is_active") or ""
                ),
            }
        )
    if not continuing:
        raise RuntimeError(
            "maintenance is not quiet and no continuing pre-hold service "
            "generation explains it"
        )
    timers_quiet = all(
        str(state.get("is_enabled") or "") == "disabled"
        and str(state.get("is_active") or "") == "inactive"
        for state in dict(current_status.get("timers") or {}).values()
    )
    runtime = dict(current_status.get("runtime_schedules") or {})
    runtime_quiet = (
        not bool((runtime.get("web_vitrina") or {}).get("active"))
        and not list(
            (runtime.get("feedback_complaints") or {}).get(
                "active_runs"
            )
            or []
        )
        and (runtime.get("spp") or {}).get("active_job") is None
    )
    allowed_pids = {int(row["main_pid"]) for row in continuing}
    unexpected_processes = [
        dict(row)
        for row in current_status.get("writer_processes") or []
        if int(row.get("pid") or 0) not in allowed_pids
    ]
    if not timers_quiet or not runtime_quiet or unexpected_processes:
        raise RuntimeError(
            "pre-hold service continuity does not explain every non-quiet "
            "business-data boundary"
        )
    payload = {
        "barrier_window_id": str(barrier.get("window_id") or ""),
        "barrier_plan_fingerprint": str(
            barrier.get("plan_fingerprint") or ""
        ),
        "hold_started_at": str(
            maintenance_state.get("hold_started_at") or ""
        ),
        "services": continuing,
    }
    return {
        **payload,
        "fingerprint": _stable_fingerprint(payload),
    }


def _validated_pre_hold_service_continuity_evidence(
    runtime_dir: Path,
    *,
    maintenance_state: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    candidate = dict(evidence or {})
    if str(maintenance_state.get("phase") or "") not in {
        "holding",
        "prepared",
    }:
        raise RuntimeError(
            "persisted pre-hold service continuity requires holding/prepared state"
        )
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
    if (
        not services
        or str(candidate.get("fingerprint") or "")
        != _stable_fingerprint(payload)
    ):
        raise RuntimeError(
            "persisted pre-hold service continuity fingerprint is invalid"
        )
    barrier = barrier_status(runtime_dir)
    if (
        barrier.get("active") is not True
        or str(barrier.get("phase") or "") != "acquiring"
        or barrier.get("hold_confirmed") is not False
        or payload["barrier_window_id"]
        != str(barrier.get("window_id") or "")
        or payload["barrier_plan_fingerprint"]
        != str(barrier.get("plan_fingerprint") or "")
        or payload["hold_started_at"]
        != str(maintenance_state.get("hold_started_at") or "")
    ):
        raise RuntimeError(
            "persisted pre-hold service continuity boundary drifted"
        )
    baseline_services = dict(
        (maintenance_state.get("baseline") or {}).get("services") or {}
    )
    hold_started_at = datetime.fromisoformat(
        payload["hold_started_at"].replace("Z", "+00:00")
    )
    if hold_started_at.tzinfo is None:
        raise RuntimeError("maintenance hold timestamp is not timezone-aware")
    seen: set[str] = set()
    for service in services:
        unit = str(service.get("unit") or "")
        if unit in seen or unit not in ALL_BUSINESS_SERVICE_UNITS:
            raise RuntimeError(
                "persisted pre-hold service continuity unit is invalid"
            )
        seen.add(unit)
        main_pid = int(service.get("main_pid") or 0)
        started_at_raw = str(service.get("started_at") or "")
        started_at = _parse_systemd_utc_timestamp(started_at_raw)
        baseline = dict(baseline_services.get(unit) or {})
        baseline_properties = dict(baseline.get("properties") or {})
        baseline_pid = int(baseline_properties.get("MainPID") or 0)
        baseline_started_at = str(
            baseline_properties.get("ExecMainStartTimestamp") or ""
        )
        if (
            main_pid <= 0
            or started_at > hold_started_at
            or str(service.get("baseline_active_state") or "")
            != str(baseline.get("is_active") or "")
            or (baseline_pid and baseline_pid != main_pid)
            or (
                baseline_started_at
                and baseline_started_at != started_at_raw
            )
        ):
            raise RuntimeError(
                "persisted pre-hold service generation does not match baseline"
            )
    return {
        **payload,
        "fingerprint": str(candidate["fingerprint"]),
    }


def _verify_pre_hold_service_continuity(
    systemd: SystemdClient,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    readback: list[dict[str, Any]] = []
    for expected_raw in evidence.get("services") or []:
        expected = dict(expected_raw)
        unit = str(expected.get("unit") or "")
        current = systemd.unit_state(unit)
        properties = dict(current.get("properties") or {})
        main_pid = int(properties.get("MainPID") or 0)
        started_at = str(
            properties.get("ExecMainStartTimestamp") or ""
        )
        if (
            str(current.get("is_active") or "")
            in QUIESCENT_SERVICE_STATES
        ):
            if (
                str(properties.get("Result") or "") != "success"
                or str(properties.get("ExecMainStatus") or "0") != "0"
            ):
                raise RuntimeError(
                    f"pre-hold service {unit} ended unsuccessfully during restore"
                )
            outcome = "completed"
        else:
            if (
                main_pid != int(expected.get("main_pid") or 0)
                or started_at != str(expected.get("started_at") or "")
            ):
                raise RuntimeError(
                    f"pre-hold service generation changed during restore: {unit}"
                )
            outcome = "continued"
        readback.append(
            {
                "unit": unit,
                "outcome": outcome,
                "main_pid": main_pid,
                "started_at": started_at,
            }
        )
    payload = {"services": readback}
    return {
        **payload,
        "fingerprint": _stable_fingerprint(payload),
    }


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
    autoanswers_reconcile: Any | None = None,
) -> dict[str, Any]:
    prepared = maintenance_prepare(
        runtime_dir,
        systemd=systemd,
        schedules=schedules,
        proc_root=proc_root,
        actor=actor,
        reason=reason,
        expected_revision=expected_revision,
        autoanswers_reconcile=autoanswers_reconcile,
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


def _reconcile_autoanswers_lifecycle(
    runtime_dir: Path,
    *,
    suspended_by_master: bool,
    actor: str,
    reason: str,
    systemd: SystemdClient,
) -> dict[str, Any]:
    from packages.application.wb_autoanswers_lifecycle import AutoanswersLifecycle
    from packages.application.wb_autoanswers_runtime import AutoanswersRepository

    repository = AutoanswersRepository(runtime_dir=runtime_dir)
    return AutoanswersLifecycle(
        runtime_dir=runtime_dir,
        repository=repository,
        systemd=systemd,
    ).reconcile(
        suspended_by_master=suspended_by_master,
        actor=actor,
        reason=reason,
        transition_run_id=(repository.reconciliation_status() or {}).get(
            "transition_run_id"
        ),
    )


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
    autoanswers_reconcile: Any | None = None,
    allow_pre_hold_service_continuity: bool = False,
    pre_hold_service_continuity_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    state_path = runtime_dir / STATE_FILENAME
    maintenance_state = _load_json_object(state_path) or {}
    baseline = dict(maintenance_state.get("baseline") or {})
    expected_control = dict(
        maintenance_state.get("control_signature_before_hold") or {}
    )
    if not baseline or not str(expected_control.get("fingerprint") or ""):
        raise RuntimeError(
            "exact prior maintenance control state is missing; restore is fail-closed"
        )
    prior_master_desired = (baseline.get("auto_updates") or {}).get(
        "master_desired"
    )
    if not isinstance(prior_master_desired, bool):
        raise RuntimeError(
            "prior master desired state is unknown; restore is fail-closed"
        )

    def finish_exact_restore(
        final_status: Mapping[str, Any],
        *,
        policy_revision: int,
        idempotent: bool,
        service_continuity_readback: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        control = maintenance_control_signature(
            final_status,
            runtime_dir=runtime_dir,
        )
        expected_fingerprint = str(expected_control.get("fingerprint") or "")
        if control["fingerprint"] != expected_fingerprint:
            raise RuntimeError(
                "exact prior maintenance control state was not restored: "
                f"expected {expected_fingerprint}, got {control['fingerprint']}"
            )
        updated_state = _load_json_object(state_path) or maintenance_state
        updated_state.update(
            {
                "phase": "restored",
                "restored_at": _utc_now(),
                "restore_policy_revision": policy_revision,
                "restore_readback": dict(final_status),
                "restore_control_signature": control,
                "exact_prior_state_restored": True,
                "pre_hold_service_continuity_readback": dict(
                    service_continuity_readback or {}
                ),
            }
        )
        _save_json_0600(state_path, updated_state)
        _append_audit_0600(
            runtime_dir / AUDIT_FILENAME,
            {
                "event": "hold_restored",
                "captured_at": _utc_now(),
                "policy_revision": policy_revision,
                "exact_prior_state_restored": True,
                "control_signature": control["fingerprint"],
                "pre_hold_service_continuity_readback": dict(
                    service_continuity_readback or {}
                ),
                "status": dict(final_status),
            },
        )
        return {
            **dict(final_status),
            "status": "restored",
            "idempotent": idempotent,
            "exact_prior_state_restored": True,
            "control_signature": control["fingerprint"],
            "pre_hold_service_continuity_readback": dict(
                service_continuity_readback or {}
            ),
            "auto_updates": owner_policy_readback(
                runtime_dir,
                status=final_status,
            ),
        }

    raw_policy = _load_json_object(runtime_dir / POLICY_FILENAME) or {}
    restore_legacy_schedule_hold = (
        str(raw_policy.get("schema_version") or "")
        == "auto_updates_owner_policy_v1"
    )
    policy = load_or_initialize_owner_policy(runtime_dir)
    revision = int(policy.get("revision") or 0)
    if expected_revision is not None and revision != int(expected_revision):
        raise RuntimeError(
            f"stale policy revision: expected {expected_revision}, current {revision}"
        )
    preflight = maintenance_status(
        runtime_dir,
        systemd=systemd,
        schedules=schedules,
        proc_root=proc_root,
    )
    preflight_readback = owner_policy_readback(runtime_dir, status=preflight)
    desired = {
        str(item.get("process_key") or ""): item.get("desired")
        for item in preflight_readback.get("processes", [])
        if isinstance(item, Mapping)
    }
    schedule_baseline = dict(policy.get("runtime_schedule_baseline") or {})
    if restore_legacy_schedule_hold:
        # Policy-v1 hold rewrote feature schedule JSON to disabled. Recover
        # those feature-owned desired values from its exact pre-hold baseline,
        # not from the intentionally disabled post-hold files.
        feedback_baseline = dict(
            schedule_baseline.get("feedback_complaints") or {}
        )
        desired["feedback_complaints"] = any(
            bool(item.get("enabled"))
            for item in feedback_baseline.get("schedules", [])
            if isinstance(item, Mapping)
        )
        spp_baseline = dict(schedule_baseline.get("spp") or {})
        desired["spp_test"] = bool(
            (spp_baseline.get("schedule") or {}).get("enabled")
        )
    unknown = sorted(key for key, value in desired.items() if value is None)
    if unknown:
        raise RuntimeError(
            "unsafe resume blocked by unknown intended process states: "
            + ",".join(unknown)
        )
    before = preflight
    if bool(policy.get("master_desired")):
        if not prior_master_desired:
            raise RuntimeError(
                "current master state is enabled but prior state was paused"
            )
        readback = owner_policy_readback(runtime_dir, status=before)
        if not readback["unknown_processes"] and not readback["drift_processes"]:
            return finish_exact_restore(
                before,
                policy_revision=revision,
                idempotent=True,
            )
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
    service_continuity: dict[str, Any] | None = None
    if pre_hold_service_continuity_evidence:
        if not allow_pre_hold_service_continuity:
            raise RuntimeError(
                "persisted pre-hold service continuity requires explicit permission"
            )
        service_continuity = (
            _validated_pre_hold_service_continuity_evidence(
                runtime_dir,
                maintenance_state=maintenance_state,
                evidence=pre_hold_service_continuity_evidence,
            )
        )
        if not before["quiet"]:
            current_continuity = _pre_hold_service_continuity(
                runtime_dir,
                maintenance_state=maintenance_state,
                current_status=before,
            )
            if (
                current_continuity["fingerprint"]
                != service_continuity["fingerprint"]
            ):
                raise RuntimeError(
                    "pre-hold service generation drifted after restore submit"
                )
    elif not before["quiet"] and allow_pre_hold_service_continuity:
        service_continuity = _pre_hold_service_continuity(
            runtime_dir,
            maintenance_state=maintenance_state,
            current_status=before,
        )
    allowed_continuing_pids = {
        int(row.get("main_pid") or 0)
        for row in (service_continuity or {}).get("services", [])
    }
    unexpected_writer_processes = [
        dict(row)
        for row in before["writer_processes"]
        if int(row.get("pid") or 0) not in allowed_continuing_pids
    ]
    if unexpected_writer_processes:
        raise RuntimeError("writer processes are still active")
    if any(
        bool(value.get("held"))
        for key, value in before["writer_locks"].items()
        if key != "seller_portal"
    ) or bool((before["writer_locks"].get("seller_portal") or {}).get("busy")):
        raise RuntimeError("maintenance/shared lock is still held")
    if not before["quiet"] and service_continuity is None:
        raise RuntimeError("business-data maintenance is not quiet before resume")
    if not prior_master_desired:
        continuity_readback = (
            _verify_pre_hold_service_continuity(systemd, service_continuity)
            if service_continuity is not None
            else None
        )
        return finish_exact_restore(
            before,
            policy_revision=revision,
            idempotent=True,
            service_continuity_readback=continuity_readback,
        )
    try:
        if restore_legacy_schedule_hold:
            schedules.restore_legacy_hold(schedule_baseline)
        else:
            schedules.restore_selected(
                schedule_baseline,
                desired={key: bool(value) for key, value in desired.items()},
            )
        for spec in PROCESS_SPECS:
            key = str(spec["key"])
            if key == "autoanswers":
                continue
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

                warehouse_result = restore_warehouse_timer(
                    runtime_dir,
                    allow_outer_hold_recovery=service_continuity
                    is not None,
                )
            else:
                warehouse_result = warehouse_restore(runtime_dir)
            if str((warehouse_result or {}).get("status") or "") != "restored":
                raise RuntimeError("warehouse timer restore did not return restored status")
        else:
            systemd.disable_now("wb-core-warehouse-functional-sync.timer")
        reconcile_autoanswers = (
            autoanswers_reconcile or _reconcile_autoanswers_lifecycle
        )
        reconcile_autoanswers(
            runtime_dir,
            suspended_by_master=False,
            actor=actor,
            reason=reason,
            systemd=systemd,
        )
    except Exception as exc:
        for unit in CORE_TIMER_UNITS + (
            "wb-core-warehouse-functional-sync.timer",
        ):
            systemd.disable_now(unit)
        try:
            (autoanswers_reconcile or _reconcile_autoanswers_lifecycle)(
                runtime_dir,
                suspended_by_master=True,
                actor=actor,
                reason="fail-closed after master resume failure",
                systemd=systemd,
            )
        except Exception:
            systemd.disable_now("wb-core-autoanswers-worker.timer")
            systemd.disable_now("wb-core-autoanswers-readonly-sync.timer")
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
        _process_actual_state(
            spec,
            status=after,
            policy=preview_policy,
            runtime_dir=runtime_dir,
        )
        for spec in PROCESS_SPECS
    ]
    drift = [item["process_key"] for item in actual if item["drift_status"] != "matched"]
    if drift:
        for unit in CORE_TIMER_UNITS + (
            "wb-core-warehouse-functional-sync.timer",
        ):
            systemd.disable_now(unit)
        try:
            (autoanswers_reconcile or _reconcile_autoanswers_lifecycle)(
                runtime_dir,
                suspended_by_master=True,
                actor=actor,
                reason="fail-closed after post-resume drift",
                systemd=systemd,
            )
        except Exception:
            systemd.disable_now("wb-core-autoanswers-worker.timer")
            systemd.disable_now("wb-core-autoanswers-readonly-sync.timer")
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
    try:
        continuity_readback = (
            _verify_pre_hold_service_continuity(systemd, service_continuity)
            if service_continuity is not None
            else None
        )
        return finish_exact_restore(
            final,
            policy_revision=int(policy["revision"]),
            idempotent=False,
            service_continuity_readback=continuity_readback,
        )
    except Exception:
        _set_master_policy_paused(
            runtime_dir,
            actor=actor,
            reason="fail-closed after exact restore mismatch",
            runtime_schedule_baseline=schedule_baseline,
            pre_hold_readback=final,
        )
        for unit in CORE_TIMER_UNITS + (
            "wb-core-warehouse-functional-sync.timer",
        ):
            systemd.disable_now(unit)
        try:
            (autoanswers_reconcile or _reconcile_autoanswers_lifecycle)(
                runtime_dir,
                suspended_by_master=True,
                actor=actor,
                reason="fail-closed after exact restore mismatch",
                systemd=systemd,
            )
        except Exception:
            systemd.disable_now("wb-core-autoanswers-worker.timer")
            systemd.disable_now("wb-core-autoanswers-readonly-sync.timer")
        schedules.disable_all(schedules.read_all())
        raise


def maintenance_prepare(
    runtime_dir: Path,
    *,
    systemd: SystemdClient,
    schedules: RuntimeScheduleClient,
    proc_root: Path = Path("/proc"),
    actor: str = "business_data_maintenance",
    reason: str = "canonical cross-writer hold",
    expected_revision: int | None = None,
    autoanswers_reconcile: Any | None = None,
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
    if existing is not None and not str(
        (existing.get("control_signature_before_hold") or {}).get(
            "fingerprint"
        )
        or ""
    ):
        raise RuntimeError(
            "active maintenance state predates exact control-signature "
            "capture; prior state is unknown and mutation is fail-closed"
        )
    owner_policy_existed = (runtime_dir / POLICY_FILENAME).is_file()
    before_payloads = schedules.read_all()
    before = maintenance_status(runtime_dir, systemd=systemd, schedules=schedules, proc_root=proc_root)
    prior_auto_updates = (
        owner_policy_readback(runtime_dir, status=before)
        if owner_policy_existed
        else {}
    )
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
    if not prior_auto_updates:
        prior_auto_updates = owner_policy_readback(runtime_dir, status=before)
        prior_auto_updates["master_desired"] = any(
            item.get("desired") is True
            for item in prior_auto_updates.get("processes", [])
            if isinstance(item, Mapping)
        )
    baseline_with_policy = dict(before)
    baseline_with_policy["auto_updates"] = prior_auto_updates
    if not state.get("control_signature_before_hold"):
        state["baseline"] = baseline_with_policy
        state["control_signature_before_hold"] = maintenance_control_signature(
            baseline_with_policy,
            runtime_dir=runtime_dir,
        )
        _save_json_0600(state_path, state)

    (autoanswers_reconcile or _reconcile_autoanswers_lifecycle)(
        runtime_dir,
        suspended_by_master=True,
        actor=actor,
        reason=reason,
        systemd=systemd,
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
        choices=(
            "status",
            "prepare",
            "hold",
            "restore",
            "restore-continuity-status",
            "set-process",
            "barrier-status",
            "barrier-acquire",
            "barrier-confirm",
            "barrier-restoring",
            "barrier-release",
            "barrier-abort",
        ),
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
    parser.add_argument("--window-id", default="")
    parser.add_argument(
        "--window-kind",
        choices=("snapshot", "final_cutover", "rollback_drill"),
        default="snapshot",
    )
    parser.add_argument("--plan-fingerprint", default="")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument(
        "--allow-pre-hold-service-continuity",
        action="store_true",
        help=(
            "Abort an unconfirmed acquiring window while a proven service "
            "generation that predates the hold continues unchanged."
        ),
    )
    parser.add_argument(
        "--pre-hold-service-continuity-file",
        default="",
        help=(
            "Private exact continuity evidence persisted by the detached "
            "restore job."
        ),
    )
    parser.add_argument(
        "--expected-pre-hold-service-continuity-fingerprint",
        default="",
        help="Exact fingerprint bound by the detached restore request.",
    )
    args = parser.parse_args(argv)
    runtime_dir = Path(args.runtime_dir).resolve()
    if args.action == "barrier-status":
        result = barrier_status(runtime_dir)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.action == "barrier-acquire":
        result = acquire_barrier(
            runtime_dir,
            window_id=args.window_id,
            window_kind=args.window_kind,
            plan_fingerprint=args.plan_fingerprint,
            approval_reference=args.approval_reference,
            actor=args.actor,
            reason=args.reason,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.action == "barrier-confirm":
        result = confirm_barrier_hold(
            runtime_dir,
            window_id=args.window_id,
            plan_fingerprint=args.plan_fingerprint,
            maintenance_state=(
                _load_json_object(runtime_dir / STATE_FILENAME) or {}
            ),
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.action == "barrier-restoring":
        result = mark_barrier_restoring(
            runtime_dir,
            window_id=args.window_id,
            plan_fingerprint=args.plan_fingerprint,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.action in {"barrier-release", "barrier-abort"}:
        maintenance_state = _load_json_object(runtime_dir / STATE_FILENAME) or {}
        restore_readback = dict(maintenance_state.get("restore_readback") or {})
        restore_readback.update(
            {
                "status": str(maintenance_state.get("phase") or ""),
                "exact_prior_state_restored": bool(
                    maintenance_state.get("exact_prior_state_restored")
                ),
                "control_signature": str(
                    (
                        maintenance_state.get("restore_control_signature")
                        or {}
                    ).get("fingerprint")
                    or ""
                ),
            }
        )
        if args.action == "barrier-abort":
            result = abort_barrier_acquire(
                runtime_dir,
                window_id=args.window_id,
                plan_fingerprint=args.plan_fingerprint,
                actor=args.actor,
                reason=args.reason,
                restore_readback=restore_readback,
            )
        else:
            result = release_barrier(
                runtime_dir,
                window_id=args.window_id,
                plan_fingerprint=args.plan_fingerprint,
                actor=args.actor,
                reason=args.reason,
                restore_readback=restore_readback,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0

    env = _read_env_file(Path(args.env_file))
    base_url = (
        args.base_url
        or env.get("BUSINESS_DATA_MAINTENANCE_BASE_URL")
        or f"http://{env.get('REGISTRY_UPLOAD_HTTP_HOST', '127.0.0.1')}:{env.get('REGISTRY_UPLOAD_HTTP_PORT', '8765')}"
    )
    schedules = RuntimeScheduleClient(base_url=base_url, cookie=_build_web_auth_cookie(env))
    systemd = SystemdClient()
    if args.action == "restore-continuity-status":
        status = maintenance_status(
            runtime_dir,
            systemd=systemd,
            schedules=schedules,
        )
        maintenance_state = (
            _load_json_object(runtime_dir / STATE_FILENAME) or {}
        )
        result = {
            "status": "ready",
            "maintenance": status,
            "service_continuity": _pre_hold_service_continuity(
                runtime_dir,
                maintenance_state=maintenance_state,
                current_status=status,
            ),
        }
    elif args.action == "status":
        result = maintenance_status(runtime_dir, systemd=systemd, schedules=schedules)
    elif args.action == "prepare":
        result = maintenance_prepare(
            runtime_dir,
            systemd=systemd,
            schedules=schedules,
            actor=args.actor,
            reason=args.reason or "canonical cross-writer hold",
            expected_revision=args.expected_revision,
        )
    elif args.action == "hold":
        result = maintenance_hold(
            runtime_dir,
            systemd=systemd,
            schedules=schedules,
            wait_timeout_seconds=args.wait_timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
            actor=args.actor,
            reason=args.reason or "canonical cross-writer hold",
            expected_revision=args.expected_revision,
        )
    elif args.action == "restore":
        continuity_evidence: dict[str, Any] | None = None
        if args.pre_hold_service_continuity_file:
            continuity_path = Path(
                str(args.pre_hold_service_continuity_file)
            )
            if continuity_path.is_symlink() or not continuity_path.is_file():
                raise RuntimeError(
                    "pre-hold service continuity evidence is unavailable"
                )
            loaded_continuity = json.loads(
                continuity_path.read_text(encoding="utf-8")
            )
            if not isinstance(loaded_continuity, dict):
                raise RuntimeError(
                    "pre-hold service continuity evidence must be an object"
                )
            if (
                args.expected_pre_hold_service_continuity_fingerprint
                and str(loaded_continuity.get("fingerprint") or "")
                != str(
                    args.expected_pre_hold_service_continuity_fingerprint
                )
            ):
                raise RuntimeError(
                    "pre-hold service continuity fingerprint drifted from "
                    "the detached request"
                )
            continuity_evidence = loaded_continuity
        with _ExclusiveRestoreLock(runtime_dir):
            result = maintenance_restore(
                runtime_dir,
                systemd=systemd,
                schedules=schedules,
                actor=args.actor,
                reason=args.reason or "bounded recovery completed",
                expected_revision=args.expected_revision,
                allow_pre_hold_service_continuity=bool(
                    args.allow_pre_hold_service_continuity
                ),
                pre_hold_service_continuity_evidence=continuity_evidence,
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
