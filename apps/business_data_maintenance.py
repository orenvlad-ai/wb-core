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
import re
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
from packages.application.storage_registry import StoreRegistry  # noqa: E402


SCHEMA_VERSION = "business_data_maintenance_v1"
STATE_FILENAME = ".business-data-maintenance.json"
AUDIT_FILENAME = ".business-data-maintenance-audit.jsonl"
POLICY_SCHEMA_VERSION = "auto_updates_owner_policy_v2"
POLICY_FILENAME = ".auto-updates-policy.json"
POLICY_AUDIT_FILENAME = ".auto-updates-policy-audit.jsonl"
WAREHOUSE_MAINTENANCE_STATE_FILENAME = ".warehouse-functional-maintenance.json"
RESTORE_LOCK_FILENAME = ".business-data-maintenance-restore.lock"
QUIET_CONFIRMED_HOLD_CONTINUITY_KIND = "quiet_confirmed_hold"
PREPARED_ABORT_QUIESCE_SCHEMA = "business_data_prepared_abort_quiesce_v1"
PREPARED_ABORT_RECOVERY_EPOCH_SCHEMA = (
    "business_data_prepared_abort_recovery_epoch_v1"
)
PREPARED_ABORT_PARTIAL_RESTORE_RECOVERY_SCHEMA = (
    "business_data_prepared_abort_partial_restore_recovery_v1"
)
HOT_JOURNAL_RECOVERY_MARKER_FILENAME = ".sqlite-hot-journal-recovery.json"
HOT_JOURNAL_RECOVERY_RESULT_CONTRACT = (
    "wbc0027_s047_split_hot_journal_recovery_result_v1"
)
QUIESCENT_SERVICE_STATES = frozenset({"inactive", "failed"})
ACTIVE_RUNTIME_STATES = frozenset(
    {
        "queued",
        "starting",
        "preflight",
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
    "wb-core-wb-finance-weekly.timer",
    "wb-core-finance-backup-rotation.timer",
)
INDEPENDENT_WRITER_TIMER_UNITS = (
    "wb-core-fbs-warehouse-registry.timer",
    "wb-core-sheet-vitrina-canary-restore.timer",
    "wb-core-sheet-vitrina-health-candidate.timer",
    "wb-core-sheet-vitrina-health-confirmation.timer",
    "wb-core-fbs-shadow-collector.timer",
)
# This projection is deliberately version-stable.  Pause ownership for the
# FBS shadow writer is recorded in the full baseline/readback and restore plan,
# without changing pre-existing control-signature bytes.
CONTROL_SIGNATURE_INDEPENDENT_WRITER_TIMER_UNITS = (
    "wb-core-fbs-warehouse-registry.timer",
    "wb-core-sheet-vitrina-canary-restore.timer",
    "wb-core-sheet-vitrina-health-candidate.timer",
    "wb-core-sheet-vitrina-health-confirmation.timer",
)
FBS_SHADOW_TIMER_UNIT = "wb-core-fbs-shadow-collector.timer"
FBS_SHADOW_SERVICE_UNIT = "wb-core-fbs-shadow-collector.service"
FBS_SHADOW_PROCESS_MARKER = "apps/wb_fbs_shadow.py"
FORCE_OFF_TIMER_UNITS = (
    "wb-core-warehouse-functional-sync.timer",
    "wb-core-autoanswers-readonly-sync.timer",
    "wb-core-autoanswers-worker.timer",
)
ALL_BUSINESS_TIMER_UNITS = (
    CORE_TIMER_UNITS
    + INDEPENDENT_WRITER_TIMER_UNITS
    + FORCE_OFF_TIMER_UNITS
)
ALL_BUSINESS_SERVICE_UNITS = tuple(unit.removesuffix(".timer") + ".service" for unit in ALL_BUSINESS_TIMER_UNITS)
PREPARED_ABORT_OUTER_TIMER_UNITS = (
    CORE_TIMER_UNITS
    + INDEPENDENT_WRITER_TIMER_UNITS
    + ("wb-core-warehouse-functional-sync.timer",)
)
CONTINUOUS_OBSERVER_TIMER_UNITS = (
    "wb-core-change-registry-observer.timer",
    "wb-core-root-storage-policy.timer",
)
CONTINUOUS_OBSERVER_SERVICE_UNITS = tuple(
    unit.removesuffix(".timer") + ".service"
    for unit in CONTINUOUS_OBSERVER_TIMER_UNITS
)
CONTINUOUS_INFRASTRUCTURE_SERVICE_UNITS = (
    "wb-core-registry-http.service",
    "wb-core-data-mcp.service",
)
CLASSIFIED_WB_CORE_TIMER_UNITS = (
    ALL_BUSINESS_TIMER_UNITS + CONTINUOUS_OBSERVER_TIMER_UNITS
)


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
    "wb_finance_weekly.py",
    "finance_storage_backup_rotation.py",
    "warehouse_functional_runner.py",
    "wb_autoanswers_readonly.py",
    "wb_autoanswers_worker.py",
    "wb_fbs_warehouse_registry.py",
    "sheet_vitrina_v1_health_tick.py",
)
EXACT_WRITER_PROCESS_ARGS = {
    FBS_SHADOW_PROCESS_MARKER: frozenset(
        {
            FBS_SHADOW_PROCESS_MARKER,
            "/opt/wb-core-runtime/app/apps/wb_fbs_shadow.py",
        }
    ),
}
SERVICE_WRITER_PROCESS_MARKERS = {
    "wb-core-sheet-vitrina-refresh.service": frozenset(
        {"sheet_vitrina_v1_auto_refresh_tick.py"}
    ),
    "wb-core-sheet-vitrina-closure-retry.service": frozenset(
        {
            "sheet_vitrina_v1_closure_retry.py",
            "sheet_vitrina_v1_temporal_closure_retry_live.py",
        }
    ),
    "wb-core-feedbacks-auto-complaints-tick.service": frozenset(
        {"sheet_vitrina_v1_feedbacks_auto_complaints_tick.py"}
    ),
    "wb-core-wb-finance-weekly.service": frozenset(
        {"wb_finance_weekly.py"}
    ),
    "wb-core-finance-backup-rotation.service": frozenset(
        {"finance_storage_backup_rotation.py"}
    ),
    "wb-core-fbs-warehouse-registry.service": frozenset(
        {"wb_fbs_warehouse_registry.py"}
    ),
    "wb-core-sheet-vitrina-canary-restore.service": frozenset(
        {"sheet_vitrina_v1_auto_refresh_tick.py"}
    ),
    "wb-core-sheet-vitrina-health-candidate.service": frozenset(
        {"sheet_vitrina_v1_health_tick.py"}
    ),
    "wb-core-sheet-vitrina-health-confirmation.service": frozenset(
        {"sheet_vitrina_v1_health_tick.py"}
    ),
    FBS_SHADOW_SERVICE_UNIT: frozenset({FBS_SHADOW_PROCESS_MARKER}),
    "wb-core-warehouse-functional-sync.service": frozenset(
        {"warehouse_functional_runner.py"}
    ),
    "wb-core-autoanswers-readonly-sync.service": frozenset(
        {"wb_autoanswers_readonly.py"}
    ),
    "wb-core-autoanswers-worker.service": frozenset(
        {"wb_autoanswers_worker.py"}
    ),
}

WEB_SCHEDULE_PATH = "/v1/sheet-vitrina-v1/web-vitrina/auto-schedules"
FEEDBACK_SCHEDULE_PATH = "/v1/sheet-vitrina-v1/feedbacks/automation/schedules"
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

    def discovered_active_services(self) -> list[str]:
        result = self._run(
            ["list-units", "wb-core-*.service", "--all", "--no-legend", "--no-pager"]
        )
        if result.returncode != 0:
            raise RuntimeError(
                "systemctl list-units failed: "
                + (result.stderr.strip() or f"exit {result.returncode}")
            )
        return sorted(
            {
                fields[0]
                for line in result.stdout.splitlines()
                if len(fields := line.split()) >= 4
                and fields[0].startswith("wb-core-")
                and fields[0].endswith(".service")
                and fields[2] not in QUIESCENT_SERVICE_STATES
                and fields[3] not in {"dead", "failed", "exited"}
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
        return self.read_all()


def _runtime_summary(payloads: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    web = payloads.get("web_vitrina") or {}
    feedback = payloads.get("feedback_complaints") or {}
    spp_status = payloads.get("spp_status") or {}

    web_schedules = [item for item in web.get("schedules", []) if isinstance(item, Mapping)]
    feedback_schedules = [item for item in feedback.get("schedules", []) if isinstance(item, Mapping)]
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
            command_raw = (entry / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        arguments = [
            value.decode("utf-8", errors="surrogateescape")
            for value in command_raw.split(b"\0")
            if value
        ]
        exact_marker = next(
            (
                marker
                for marker, accepted in EXACT_WRITER_PROCESS_ARGS.items()
                if any(argument in accepted for argument in arguments)
            ),
            "",
        )
        if exact_marker:
            rows.append({"pid": int(entry.name), "marker": exact_marker})
            continue
        command = command_raw.replace(b"\0", b" ")
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
        "finance_backup": _flock_snapshot(
            runtime_dir / ".finance-storage-snapshot-retention.lock"
        ),
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


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
    worker_component = dict(components.get("worker") or {})
    service_in_progress = (
        bool(worker_component.get("desired"))
        and str(
            (worker_component.get("service") or {}).get("is_active") or ""
        )
        in {"active", "activating", "reloading"}
        and str(
            (
                (worker_component.get("service") or {}).get("properties")
                or {}
            ).get("Result")
            or "success"
        )
        == "success"
    )
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
    # Timer/service states are captured before the feature-owned SQLite
    # readback, which can legitimately take minutes on the production store.
    # Evaluate freshness at that same observation boundary; mixing the old
    # systemd snapshot with a later wall clock can falsely expire a starting
    # worker that became active while the query was running.
    now = (
        _parse_timestamp(status.get("captured_at"))
        or datetime.now(timezone.utc)
    )
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
        (
            stop_reason == "worker_error"
            and service_in_progress
        )
        or (
            stop_reason == "worker_unavailable"
            and (
                service_in_progress
                or (
                    requested_at is not None
                    and requested_at > now - timedelta(minutes=3)
                )
            )
        )
    ):
        # Match the feature-owned lifecycle contract: a newly resumed timer
        # receives one scheduler interval to produce its first post-request
        # tick.  While the exact worker service is in progress it also owns
        # replacement of a stale pre-request worker_error marker.  Neither
        # marker is ignored without that bounded startup evidence.
        stop_reason = ""
    elif (
        mode in {"manual", "draft_only", "auto_safe", "auto_all"}
        and not fresh_tick
        and stop_reason != "worker_error"
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
        "service_in_progress": service_in_progress,
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


def _validated_autoanswers_restore_readback(
    *,
    lifecycle_readback: Mapping[str, Any],
    preflight_state: Mapping[str, Any],
    status: Mapping[str, Any],
    spec: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind post-resume acceptance to the feature-owned lifecycle readback."""

    candidate = dict(lifecycle_readback)
    preflight = dict(preflight_state)
    failures: list[str] = []
    if str(candidate.get("contract") or "") != "wb_autoanswers_lifecycle_v1":
        failures.append("lifecycle_contract")
    if str(candidate.get("process_key") or "") != "autoanswers":
        failures.append("process_identity")
    if str(candidate.get("drift_status") or "") != "matched":
        failures.append("lifecycle_drift")
    if bool(candidate.get("suspended_by_master")):
        failures.append("master_suspension")
    if candidate.get("desired") is not preflight.get("desired"):
        failures.append("desired_state")
    if str(candidate.get("business_mode") or "") != str(
        preflight.get("business_mode") or ""
    ):
        failures.append("business_mode")
    preflight_schedule = dict(preflight.get("runtime_schedule") or {})
    candidate_epoch = candidate.get("policy_epoch")
    if candidate_epoch is None:
        candidate_epoch = dict(candidate.get("runtime_schedule") or {}).get(
            "policy_epoch"
        )
    if candidate_epoch != preflight_schedule.get("policy_epoch"):
        failures.append("policy_epoch")
    candidate_run_id = candidate.get("transition_run_id")
    if candidate_run_id is None:
        candidate_run_id = dict(candidate.get("runtime_schedule") or {}).get(
            "transition_run_id"
        )
    if str(candidate_run_id or "") != str(
        preflight_schedule.get("transition_run_id") or ""
    ):
        failures.append("transition_run_id")
    desired = candidate.get("desired")
    lifecycle_state = str(candidate.get("lifecycle_state") or "")
    if desired is True and lifecycle_state not in {"starting", "running"}:
        failures.append("lifecycle_state")
    if desired is False and lifecycle_state != "off":
        failures.append("lifecycle_state")
    stop_reason = str(candidate.get("stop_reason") or "")
    from packages.application.wb_autoanswers_lifecycle import (
        BLOCKING_STOP_REASONS,
    )

    if stop_reason in BLOCKING_STOP_REASONS:
        failures.append("stop_reason")
    if str(candidate.get("last_error") or ""):
        failures.append("last_error")

    candidate_captured_at = _parse_timestamp(
        candidate.get("readback_captured_at")
    )
    outer_captured_at = _parse_timestamp(status.get("captured_at"))
    requested_at = _parse_timestamp(candidate.get("requested_at"))
    if candidate_captured_at is None or outer_captured_at is None:
        failures.append("observation_time")
    elif candidate_captured_at > outer_captured_at:
        failures.append("observation_order")
    if (
        requested_at is None
        or candidate_captured_at is None
        or requested_at > candidate_captured_at
    ):
        failures.append("request_time")

    components = dict(candidate.get("components") or {})
    expected_components = dict(spec.get("components") or {})
    if set(components) != set(expected_components):
        failures.append("component_identity")
    outer_component_readback: dict[str, Any] = {}
    for component_key, timer_unit_value in expected_components.items():
        timer_unit = str(timer_unit_value)
        service_unit = timer_unit.removesuffix(".timer") + ".service"
        component = dict(components.get(str(component_key)) or {})
        timer = dict((status.get("timers") or {}).get(timer_unit) or {})
        service = dict((status.get("services") or {}).get(service_unit) or {})
        timer_on = bool(
            str(timer.get("is_enabled") or "") == "enabled"
            and str(timer.get("is_active") or "") == "active"
        )
        component_desired = component.get("desired")
        component_actual = component.get("actual")
        service_properties = dict(service.get("properties") or {})
        service_result = str(service_properties.get("Result") or "success")
        outer_component_readback[str(component_key)] = {
            "timer_unit": timer_unit,
            "service_unit": service_unit,
            "desired": component_desired,
            "actual": timer_on,
            "service_active_state": str(service.get("is_active") or ""),
            "service_result": service_result,
        }
        if (
            component.get("component_key") != str(component_key)
            or not isinstance(component_desired, bool)
            or not isinstance(component_actual, bool)
            or component_desired != component_actual
            or str(component.get("drift_status") or "") != "matched"
            or str(component.get("last_error") or "")
        ):
            failures.append(f"{component_key}_lifecycle")
        if not timer or not service or timer_on is not component_desired:
            failures.append(f"{component_key}_outer_timer")
        if service_result != "success":
            failures.append(f"{component_key}_outer_service")

    validation = {
        "contract": "business_data_autoanswers_restore_readback_v1",
        "accepted": not failures,
        "source": "feature_lifecycle_reconcile+outer_systemd",
        "lifecycle_readback_captured_at": str(
            candidate.get("readback_captured_at") or ""
        ),
        "outer_captured_at": str(status.get("captured_at") or ""),
        "identity": {
            "business_mode": candidate.get("business_mode"),
            "policy_epoch": candidate_epoch,
            "transition_run_id": candidate_run_id,
        },
        "components": outer_component_readback,
        "failures": sorted(set(failures)),
    }
    validation["fingerprint"] = _stable_fingerprint(validation)
    result = {
        **candidate,
        "post_resume_validation": validation,
        "provenance": "feature_lifecycle_reconcile+outer_systemd",
    }
    if failures:
        result.update(
            {
                "actual": False,
                "lifecycle_state": "unconfirmed",
                "drift_status": "unknown",
                "last_error": (
                    "post-resume Autoanswers readback is not confirmed: "
                    + ",".join(sorted(set(failures)))
                ),
            }
        )
    return result


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
    runtime_schedule_readback: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    captured_at = _utc_now()
    timer_states = {unit: systemd.unit_state(unit) for unit in ALL_BUSINESS_TIMER_UNITS}
    continuous_observer_timer_states = {
        unit: systemd.unit_state(unit)
        for unit in CONTINUOUS_OBSERVER_TIMER_UNITS
    }
    service_states = {unit: systemd.unit_state(unit) for unit in ALL_BUSINESS_SERVICE_UNITS}
    discovered = systemd.discovered_timers()
    unknown_timers = [
        unit
        for unit in discovered
        if unit not in CLASSIFIED_WB_CORE_TIMER_UNITS
    ]
    runtime = (
        dict(runtime_schedule_readback)
        if runtime_schedule_readback is not None
        else _runtime_summary(schedules.read_all())
    )
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
        "captured_at": captured_at,
        "timers": timer_states,
        "continuous_observer_timers": continuous_observer_timer_states,
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


def _sqlite_sidecar_readback(runtime_dir: Path) -> dict[str, Any]:
    operational = StoreRegistry(runtime_dir).resolve("operational")
    sidecars = {
        suffix: Path(str(operational) + suffix)
        for suffix in ("-journal", "-wal", "-shm")
    }
    return {
        "operational_path": str(operational),
        "sidecars": {
            suffix: {
                "path": str(path),
                "exists": path.exists(),
                "size_bytes": path.stat().st_size if path.exists() else 0,
            }
            for suffix, path in sidecars.items()
        },
    }


def _quiet_readback_projection(
    status: Mapping[str, Any],
    *,
    sqlite_sidecars: Mapping[str, Any],
    continuous_observer_services: Mapping[str, Any],
) -> dict[str, Any]:
    def units(raw: Mapping[str, Any]) -> dict[str, Any]:
        return {
            unit: {
                "is_enabled": str((value or {}).get("is_enabled") or ""),
                "is_active": str((value or {}).get("is_active") or ""),
                "main_pid": int(
                    ((value or {}).get("properties") or {}).get("MainPID")
                    or 0
                ),
                "started_at": str(
                    ((value or {}).get("properties") or {}).get(
                        "ExecMainStartTimestamp"
                    )
                    or ""
                ),
            }
            for unit, value in sorted(dict(raw or {}).items())
        }

    auto_updates = dict(status.get("auto_updates") or {})
    return {
        "quiet": status.get("quiet") is True,
        "timers": units(dict(status.get("timers") or {})),
        "services": units(dict(status.get("services") or {})),
        "continuous_observer_timers": units(
            dict(status.get("continuous_observer_timers") or {})
        ),
        "continuous_observer_services": units(continuous_observer_services),
        "runtime_schedules": dict(status.get("runtime_schedules") or {}),
        "writer_processes": list(status.get("writer_processes") or []),
        "writer_locks": dict(status.get("writer_locks") or {}),
        "cron_entries": list(status.get("cron_entries") or []),
        "discovered_wb_core_timers": list(
            status.get("discovered_wb_core_timers") or []
        ),
        "unknown_wb_core_timers": list(
            status.get("unknown_wb_core_timers") or []
        ),
        "paused_policy_revision": int(auto_updates.get("revision") or 0),
        "paused_policy_fingerprint": str(
            auto_updates.get("policy_fingerprint") or ""
        ),
        "sqlite_sidecars": dict(sqlite_sidecars),
    }


def _continuous_observer_service_readback(
    systemd: SystemdClient,
) -> dict[str, Any]:
    return {
        unit.removesuffix(".timer") + ".service": systemd.unit_state(
            unit.removesuffix(".timer") + ".service"
        )
        for unit in CONTINUOUS_OBSERVER_TIMER_UNITS
    }


def _require_fbs_shadow_terminal(
    services: Mapping[str, Any],
    *,
    context: str,
) -> None:
    fbs_shadow = dict(
        services.get(FBS_SHADOW_SERVICE_UNIT) or {}
    )
    if (
        str(fbs_shadow.get("is_active") or "") not in QUIESCENT_SERVICE_STATES
        or int((fbs_shadow.get("properties") or {}).get("MainPID") or 0)
        != 0
    ):
        raise RuntimeError(f"{context} FBS shadow writer is active")


def _stable_quiet_readback(
    runtime_dir: Path,
    *,
    first: Mapping[str, Any],
    systemd: SystemdClient,
    schedules: RuntimeScheduleClient,
    proc_root: Path,
    poll_interval_seconds: float,
    drain_validator: Any | None = None,
) -> dict[str, Any]:
    if drain_validator is not None:
        drain_validator(first)
    first_sidecars = _sqlite_sidecar_readback(runtime_dir)
    if any(
        bool(item.get("exists"))
        for item in dict(first_sidecars.get("sidecars") or {}).values()
    ):
        raise RuntimeError(
            "operational SQLite hot journal/sidecar blocks maintenance hold"
        )
    first_observer_services = _continuous_observer_service_readback(
        systemd
    )
    _require_fbs_shadow_terminal(
        dict(first.get("services") or {}),
        context="first stable quiet readback",
    )
    first_projection = _quiet_readback_projection(
        first,
        sqlite_sidecars=first_sidecars,
        continuous_observer_services=first_observer_services,
    )
    if first_projection.get("quiet") is not True:
        raise RuntimeError("business-data maintenance is not quiet")
    time.sleep(min(2.0, max(0.05, float(poll_interval_seconds))))
    second = maintenance_status(
        runtime_dir,
        systemd=systemd,
        schedules=schedules,
        proc_root=proc_root,
    )
    if drain_validator is not None:
        drain_validator(second)
    second_sidecars = _sqlite_sidecar_readback(runtime_dir)
    second_observer_services = _continuous_observer_service_readback(
        systemd
    )
    _require_fbs_shadow_terminal(
        dict(second.get("services") or {}),
        context="second stable quiet readback",
    )
    second_projection = _quiet_readback_projection(
        second,
        sqlite_sidecars=second_sidecars,
        continuous_observer_services=second_observer_services,
    )
    if first_projection != second_projection:
        raise RuntimeError(
            "business-data maintenance quiet readback is not stable"
        )
    return {
        "first_captured_at": str(first.get("captured_at") or ""),
        "second_captured_at": str(second.get("captured_at") or ""),
        "fingerprint": _stable_fingerprint(second_projection),
        "projection": second_projection,
        "status": second,
    }


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
    independent_writer_timer_intent = {
        unit: {
            "is_enabled": str(
                ((status.get("timers") or {}).get(unit) or {}).get(
                    "is_enabled"
                )
                or ""
            ),
            "is_active": str(
                ((status.get("timers") or {}).get(unit) or {}).get(
                    "is_active"
                )
                or ""
            ),
        }
        for unit in CONTROL_SIGNATURE_INDEPENDENT_WRITER_TIMER_UNITS
    }
    payload = {
        "master_desired": auto_updates.get("master_desired"),
        "process_desired": process_desired,
        "timer_control_intent": timer_control_intent,
        "independent_writer_timer_intent": independent_writer_timer_intent,
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


def _independent_writer_timer_restore_plan(
    baseline: Mapping[str, Any],
) -> dict[str, bool]:
    """Validate the exact representable pre-hold state before any restore."""

    timers = dict(baseline.get("timers") or {})
    plan: dict[str, bool] = {}
    for unit in INDEPENDENT_WRITER_TIMER_UNITS:
        state = dict(timers.get(unit) or {})
        if not state and unit == FBS_SHADOW_TIMER_UNIT:
            state = dict(
                (baseline.get("continuous_observer_timers") or {}).get(unit)
                or {}
            )
        pair = (
            str(state.get("is_enabled") or ""),
            str(state.get("is_active") or ""),
        )
        if pair == ("enabled", "active"):
            plan[unit] = True
        elif pair == ("disabled", "inactive"):
            plan[unit] = False
        else:
            raise RuntimeError(
                "independent writer timer baseline is not exactly "
                f"restorable: {unit}={pair!r}"
            )
    return plan


def _restore_independent_writer_timers(
    systemd: SystemdClient,
    plan: Mapping[str, bool],
) -> None:
    for unit in INDEPENDENT_WRITER_TIMER_UNITS:
        if plan.get(unit) is True:
            systemd.enable_now(unit)
        else:
            systemd.disable_now(unit)


def _require_independent_writer_timers_restored(
    status: Mapping[str, Any],
    plan: Mapping[str, bool],
) -> None:
    timers = dict(status.get("timers") or {})
    for unit in INDEPENDENT_WRITER_TIMER_UNITS:
        state = dict(timers.get(unit) or {})
        actual = (
            str(state.get("is_enabled") or ""),
            str(state.get("is_active") or ""),
        )
        expected = (
            ("enabled", "active")
            if plan.get(unit) is True
            else ("disabled", "inactive")
        )
        if actual != expected:
            raise RuntimeError(
                "independent writer timer exact prior state was not restored: "
                f"{unit}={actual!r}, expected={expected!r}"
            )


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


def _restore_service_continuity(
    runtime_dir: Path,
    *,
    maintenance_state: Mapping[str, Any],
    current_status: Mapping[str, Any],
) -> dict[str, Any]:
    """Capture either the legacy active generation or a proven quiet hold."""

    phase = str(maintenance_state.get("phase") or "")
    if phase in {"holding", "prepared"}:
        return _pre_hold_service_continuity(
            runtime_dir,
            maintenance_state=maintenance_state,
            current_status=current_status,
        )
    barrier = barrier_status(runtime_dir)
    if (
        phase != "held"
        or current_status.get("quiet") is not True
        or barrier.get("active") is not True
        or str(barrier.get("phase") or "") not in {"held", "restoring"}
        or barrier.get("hold_confirmed") is not True
    ):
        raise RuntimeError(
            "restore continuity requires either an exact pre-hold service "
            "generation or a quiet confirmed hold"
        )
    payload = {
        "boundary_kind": QUIET_CONFIRMED_HOLD_CONTINUITY_KIND,
        "barrier_window_id": str(barrier.get("window_id") or ""),
        "barrier_plan_fingerprint": str(
            barrier.get("plan_fingerprint") or ""
        ),
        "hold_started_at": str(
            maintenance_state.get("hold_started_at") or ""
        ),
        "services": [],
    }
    if not payload["hold_started_at"]:
        raise RuntimeError(
            "quiet confirmed hold lacks an exact maintenance timestamp"
        )
    return {
        **payload,
        "fingerprint": _stable_fingerprint(payload),
    }


def _validated_pre_hold_service_continuity_evidence(
    runtime_dir: Path,
    *,
    maintenance_state: Mapping[str, Any],
    evidence: Mapping[str, Any],
    current_status: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    candidate = dict(evidence or {})
    boundary_kind = str(candidate.get("boundary_kind") or "")
    if str(maintenance_state.get("phase") or "") not in {
        "holding",
        "prepared",
        "held",
    }:
        raise RuntimeError(
            "persisted restore continuity is outside an exact maintenance phase"
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
    if boundary_kind:
        payload = {
            "boundary_kind": boundary_kind,
            **payload,
        }
    if boundary_kind == QUIET_CONFIRMED_HOLD_CONTINUITY_KIND:
        barrier = barrier_status(runtime_dir)
        if (
            services
            or str(candidate.get("fingerprint") or "")
            != _stable_fingerprint(payload)
            or str(maintenance_state.get("phase") or "") != "held"
            or not isinstance(current_status, Mapping)
            or current_status.get("quiet") is not True
            or barrier.get("active") is not True
            or str(barrier.get("phase") or "")
            not in {"held", "restoring"}
            or barrier.get("hold_confirmed") is not True
            or payload["barrier_window_id"]
            != str(barrier.get("window_id") or "")
            or payload["barrier_plan_fingerprint"]
            != str(barrier.get("plan_fingerprint") or "")
            or payload["hold_started_at"]
            != str(maintenance_state.get("hold_started_at") or "")
        ):
            raise RuntimeError(
                "persisted quiet confirmed-hold continuity drifted"
            )
        return {
            **payload,
            "fingerprint": str(candidate["fingerprint"]),
        }
    if boundary_kind:
        raise RuntimeError("persisted restore continuity kind is invalid")
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
    boundary_kind = str(evidence.get("boundary_kind") or "")
    if boundary_kind == QUIET_CONFIRMED_HOLD_CONTINUITY_KIND:
        if list(evidence.get("services") or []):
            raise RuntimeError(
                "quiet confirmed-hold continuity contains a service"
            )
        payload = {
            "boundary_kind": boundary_kind,
            "services": [],
        }
        return {
            **payload,
            "fingerprint": _stable_fingerprint(payload),
        }
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
    window_id: str = "",
    plan_fingerprint: str = "",
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
        window_id=window_id,
        plan_fingerprint=plan_fingerprint,
        autoanswers_reconcile=autoanswers_reconcile,
    )
    state_path = runtime_dir / STATE_FILENAME
    audit_path = runtime_dir / AUDIT_FILENAME
    state = json.loads(state_path.read_text(encoding="utf-8"))
    resumed_prepared = bool(
        state.get("prepared_resume_binding")
        or state.get("pause_owned_inventory_resume_binding")
    )
    pause_owned_binding = dict(
        state.get("pause_owned_inventory_resume_binding") or {}
    )

    def validate_pause_owned_drain(
        status: Mapping[str, Any],
    ) -> dict[str, Any]:
        if pause_owned_binding:
            return _validate_pause_owned_resume_drain_status(
                runtime_dir,
                status=status,
                binding=pause_owned_binding,
                systemd=systemd,
            )
        return {}

    if prepared.get("quiet"):
        if resumed_prepared:
            stable = _stable_quiet_readback(
                runtime_dir,
                first=prepared,
                systemd=systemd,
                schedules=schedules,
                proc_root=proc_root,
                poll_interval_seconds=poll_interval_seconds,
                drain_validator=validate_pause_owned_drain,
            )
            prepared = dict(stable["status"])
            prepared["stable_quiet_readback"] = {
                key: value
                for key, value in stable.items()
                if key != "status"
            }
        state.update({"phase": "held", "held_at": _utc_now(), "hold_readback": prepared})
        _save_json_0600(state_path, state)
        _append_audit_0600(audit_path, {"event": "hold_acquired", "captured_at": _utc_now(), "status": prepared})
        return {**prepared, "status": "held"}
    deadline = time.monotonic() + max(0.0, float(wait_timeout_seconds))
    while True:
        current = maintenance_status(runtime_dir, systemd=systemd, schedules=schedules, proc_root=proc_root)
        drain_readback = validate_pause_owned_drain(current)
        if current["quiet"] and not bool(
            drain_readback.get("sidecars_hot")
        ):
            break
        if time.monotonic() >= deadline:
            state.update({"last_readback": current, "error": "timed out waiting for business-data quiet window"})
            _save_json_0600(state_path, state)
            _append_audit_0600(audit_path, {"event": "hold_wait_timeout", "captured_at": _utc_now(), "status": current})
            raise TimeoutError(state["error"])
        time.sleep(max(0.05, float(poll_interval_seconds)))

    if resumed_prepared:
        stable = _stable_quiet_readback(
            runtime_dir,
            first=current,
            systemd=systemd,
            schedules=schedules,
            proc_root=proc_root,
            poll_interval_seconds=poll_interval_seconds,
            drain_validator=validate_pause_owned_drain,
        )
        current = dict(stable["status"])
        current["stable_quiet_readback"] = {
            key: value for key, value in stable.items() if key != "status"
        }
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
    require_stable_readback: bool = False,
    prepared_abort_outer_timer_restore_plan: Mapping[str, bool] | None = None,
    prepared_abort_post_restore_validator: Any | None = None,
    poll_interval_seconds: float = 2.0,
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
    independent_writer_timer_restore_plan = (
        _independent_writer_timer_restore_plan(baseline)
    )
    abort_outer_timer_restore_plan = dict(
        prepared_abort_outer_timer_restore_plan or {}
    )
    if abort_outer_timer_restore_plan and set(
        abort_outer_timer_restore_plan
    ) != set(PREPARED_ABORT_OUTER_TIMER_UNITS):
        raise RuntimeError(
            "prepared abort outer timer restore plan changed"
        )
    if bool(abort_outer_timer_restore_plan) != (
        prepared_abort_post_restore_validator is not None
    ):
        raise RuntimeError(
            "prepared abort outer restore validation is incomplete"
        )
    if abort_outer_timer_restore_plan:
        partial_epoch = dict(
            maintenance_state.get(
                "prepared_abort_partial_restore_recovery_epoch"
            )
            or {}
        )
        expected_abort_plan = {
            unit: _unit_state_pair(
                _prepared_abort_baseline_timer_state(baseline, unit)
            )
            == ("enabled", "active")
            for unit in PREPARED_ABORT_OUTER_TIMER_UNITS
        }
        if (
            str(maintenance_state.get("phase") or "") != "abort_quiescing"
            or str(partial_epoch.get("schema_version") or "")
            != PREPARED_ABORT_PARTIAL_RESTORE_RECOVERY_SCHEMA
            or abort_outer_timer_restore_plan != expected_abort_plan
        ):
            raise RuntimeError(
                "prepared abort outer restore is outside its exact epoch"
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
        _require_independent_writer_timers_restored(
            final_status,
            independent_writer_timer_restore_plan,
        )
        if abort_outer_timer_restore_plan:
            _require_prepared_abort_outer_timer_plan_restored(
                final_status,
                abort_outer_timer_restore_plan,
            )
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
        stable_restore_readback: dict[str, Any] = {}
        if require_stable_readback:
            first_status = dict(final_status)
            time.sleep(min(2.0, max(0.05, float(poll_interval_seconds))))
            second_status = maintenance_status(
                runtime_dir,
                systemd=systemd,
                schedules=schedules,
                proc_root=proc_root,
            )
            _require_independent_writer_timers_restored(
                second_status,
                independent_writer_timer_restore_plan,
            )
            if abort_outer_timer_restore_plan:
                _require_prepared_abort_outer_timer_plan_restored(
                    second_status,
                    abort_outer_timer_restore_plan,
                )
            second_control = maintenance_control_signature(
                second_status,
                runtime_dir=runtime_dir,
            )
            second_policy = owner_policy_readback(
                runtime_dir,
                status=second_status,
            )
            if (
                second_control != control
                or second_control["fingerprint"] != expected_fingerprint
                or second_policy.get("unknown_processes")
                or second_policy.get("drift_processes")
            ):
                raise RuntimeError(
                    "exact prior maintenance control state was not stable"
                )
            stable_restore_readback = {
                "first_captured_at": str(
                    first_status.get("captured_at") or ""
                ),
                "second_captured_at": str(
                    second_status.get("captured_at") or ""
                ),
                "fingerprint": second_control["fingerprint"],
            }
            final_status = second_status
            control = second_control
        prepared_abort_post_restore_evidence: dict[str, Any] = {}
        if prepared_abort_post_restore_validator is not None:
            prepared_abort_post_restore_evidence = dict(
                prepared_abort_post_restore_validator(final_status) or {}
            )
        stable_restore_payload = (
            {"stable_restore_readback": stable_restore_readback}
            if require_stable_readback
            else {}
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
                **stable_restore_payload,
                **(
                    {
                        "prepared_abort_partial_restore_post_restore_services": (
                            prepared_abort_post_restore_evidence
                        )
                    }
                    if prepared_abort_post_restore_evidence
                    else {}
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
                **stable_restore_payload,
                **(
                    {
                        "prepared_abort_post_restore_evidence": (
                            prepared_abort_post_restore_evidence
                        )
                    }
                    if prepared_abort_post_restore_evidence
                    else {}
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
            **stable_restore_payload,
            **(
                {
                    "prepared_abort_post_restore_evidence": (
                        prepared_abort_post_restore_evidence
                    )
                }
                if prepared_abort_post_restore_evidence
                else {}
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
    preflight_autoanswers = next(
        (
            dict(item)
            for item in preflight_readback.get("processes", [])
            if isinstance(item, Mapping)
            and str(item.get("process_key") or "") == "autoanswers"
        ),
        {},
    )
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
                current_status=before,
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
        _restore_independent_writer_timers(
            systemd,
            independent_writer_timer_restore_plan,
        )
        if abort_outer_timer_restore_plan:
            _restore_prepared_abort_outer_timer_plan(
                systemd,
                abort_outer_timer_restore_plan,
            )
        before = maintenance_status(
            runtime_dir,
            systemd=systemd,
            schedules=schedules,
            proc_root=proc_root,
        )
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
        autoanswers_lifecycle_readback = reconcile_autoanswers(
            runtime_dir,
            suspended_by_master=False,
            actor=actor,
            reason=reason,
            systemd=systemd,
        )
        _restore_independent_writer_timers(
            systemd,
            independent_writer_timer_restore_plan,
        )
        if abort_outer_timer_restore_plan:
            _restore_prepared_abort_outer_timer_plan(
                systemd,
                abort_outer_timer_restore_plan,
            )
    except Exception as exc:
        for unit in CORE_TIMER_UNITS + INDEPENDENT_WRITER_TIMER_UNITS + (
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
    actual: list[dict[str, Any]] = []
    for spec in PROCESS_SPECS:
        if str(spec.get("key") or "") == "autoanswers":
            actual.append(
                _validated_autoanswers_restore_readback(
                    lifecycle_readback=autoanswers_lifecycle_readback,
                    preflight_state=preflight_autoanswers,
                    status=after,
                    spec=spec,
                )
            )
        else:
            actual.append(
                _process_actual_state(
                    spec,
                    status=after,
                    policy=preview_policy,
                    runtime_dir=runtime_dir,
                )
            )
    drift = [item["process_key"] for item in actual if item["drift_status"] != "matched"]
    if drift:
        for unit in CORE_TIMER_UNITS + INDEPENDENT_WRITER_TIMER_UNITS + (
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
                "autoanswers_validation": dict(
                    next(
                        (
                            item.get("post_resume_validation") or {}
                            for item in actual
                            if item.get("process_key") == "autoanswers"
                        ),
                        {},
                    )
                ),
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
        for unit in CORE_TIMER_UNITS + INDEPENDENT_WRITER_TIMER_UNITS + (
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


def _unit_state_pair(state: Mapping[str, Any]) -> tuple[str, str]:
    return (
        str(state.get("is_enabled") or ""),
        str(state.get("is_active") or ""),
    )


def _validate_pause_owned_timer_trigger(
    unit: str,
    *,
    recorded: Mapping[str, Any],
    current: Mapping[str, Any],
) -> None:
    recorded_trigger = str(
        (recorded.get("properties") or {}).get("LastTriggerUSec") or ""
    )
    current_trigger = str(
        (current.get("properties") or {}).get("LastTriggerUSec") or ""
    )
    if current_trigger == recorded_trigger:
        return
    # systemd clears LastTriggerUSec when an active timer is disabled.  That
    # terminal representation is admissible only after the exact timer is
    # disabled/inactive; the separately bound service generation still proves
    # that no replacement writer started while the process was restarting.
    if (
        _unit_state_pair(current) == ("disabled", "inactive")
        and not current_trigger
    ):
        return
    raise RuntimeError(
        "prepared maintenance pause-owned timer retriggered: " + unit
    )


def _pause_owned_service_generation_evidence(
    services: Mapping[str, Any],
    writer_processes: Sequence[Mapping[str, Any]],
    *,
    recorded: Mapping[str, Any] | None = None,
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    service_states = {
        str(unit): dict(state or {})
        for unit, state in dict(services or {}).items()
    }
    if set(service_states) != set(ALL_BUSINESS_SERVICE_UNITS):
        raise RuntimeError(
            "prepared maintenance pause-owned service inventory drifted"
        )
    if set(SERVICE_WRITER_PROCESS_MARKERS) != set(
        ALL_BUSINESS_SERVICE_UNITS
    ):
        raise RuntimeError(
            "prepared maintenance pause-owned service process contract drifted"
        )
    rows = [dict(row) for row in writer_processes]
    rows_by_pid: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        rows_by_pid.setdefault(int(row.get("pid") or 0), []).append(row)
    recorded_generations = {
        str(unit): dict(value or {})
        for unit, value in dict(recorded or {}).items()
    }
    if recorded is not None and set(recorded_generations) != set(
        ALL_BUSINESS_SERVICE_UNITS
    ):
        raise RuntimeError(
            "prepared maintenance pause-owned service evidence drifted"
        )
    evidence: dict[str, dict[str, Any]] = {}
    active_units: set[str] = set()
    admitted_process_pids: set[int] = set()
    for unit in ALL_BUSINESS_SERVICE_UNITS:
        state = service_states[unit]
        if str(state.get("unit") or "") != unit:
            raise RuntimeError(
                "prepared maintenance pause-owned service identity drifted: "
                + unit
            )
        if str(state.get("is_enabled") or "") != "static":
            raise RuntimeError(
                "prepared maintenance pause-owned service contract drifted: "
                + unit
            )
        properties = dict(state.get("properties") or {})
        main_pid = int(properties.get("MainPID") or 0)
        started_at = str(properties.get("ExecMainStartTimestamp") or "")
        active = (
            str(state.get("is_active") or "")
            not in QUIESCENT_SERVICE_STATES
        )
        if active:
            if main_pid <= 0:
                raise RuntimeError(
                    "prepared maintenance pause-owned service has no exact PID: "
                    + unit
                )
            _parse_systemd_utc_timestamp(started_at)
            matching_rows = rows_by_pid.get(main_pid, [])
            expected_markers = SERVICE_WRITER_PROCESS_MARKERS[unit]
            if (
                len(matching_rows) != 1
                or str(matching_rows[0].get("marker") or "")
                not in expected_markers
            ):
                raise RuntimeError(
                    "prepared maintenance pause-owned service process identity drifted: "
                    + unit
                )
            admitted_process_pids.add(main_pid)
            active_units.add(unit)
        elif main_pid != 0:
            raise RuntimeError(
                "prepared maintenance pause-owned terminal service retained a PID: "
                + unit
            )
        current = {
            "unit": unit,
            "is_enabled": str(state.get("is_enabled") or ""),
            "initial_is_active": str(state.get("is_active") or ""),
            "main_pid": main_pid,
            "started_at": started_at,
            "writer_processes": rows_by_pid.get(main_pid, []) if active else [],
        }
        if recorded is None:
            evidence[unit] = current
            continue
        expected = recorded_generations[unit]
        if (
            str(expected.get("unit") or "") != unit
            or str(expected.get("is_enabled") or "") != "static"
        ):
            raise RuntimeError(
                "prepared maintenance pause-owned service generation changed: "
                + unit
            )
        initially_active = (
            str(expected.get("initial_is_active") or "")
            not in QUIESCENT_SERVICE_STATES
        )
        expected_started_at = str(expected.get("started_at") or "")
        if initially_active:
            if expected_started_at != started_at:
                raise RuntimeError(
                    "prepared maintenance pause-owned service generation changed: "
                    + unit
                )
            if active and (
                main_pid != int(expected.get("main_pid") or 0)
                or rows_by_pid.get(main_pid, [])
                != list(expected.get("writer_processes") or [])
            ):
                raise RuntimeError(
                    "prepared maintenance pause-owned service restarted: "
                    + unit
                )
        else:
            if active:
                raise RuntimeError(
                    "prepared maintenance pause-owned service restarted: "
                    + unit
                )
            # systemd may evict the volatile start timestamp after a terminal
            # oneshot is collected.  Only that terminal/PID-zero/no-process
            # representation is admissible; a different non-empty timestamp
            # still proves another generation.
            if started_at not in {expected_started_at, ""}:
                raise RuntimeError(
                    "prepared maintenance pause-owned service generation changed: "
                    + unit
                )
        evidence[unit] = expected
    unexpected_processes = [
        row
        for row in rows
        if int(row.get("pid") or 0) not in admitted_process_pids
    ]
    if unexpected_processes:
        raise RuntimeError(
            "prepared maintenance pause-owned drain found another writer"
        )
    return evidence, active_units


def _require_pause_owned_active_service_inventory(
    systemd: SystemdClient,
) -> set[str]:
    active_services = set(systemd.discovered_active_services())
    allowed = (
        set(ALL_BUSINESS_SERVICE_UNITS)
        | set(CONTINUOUS_OBSERVER_SERVICE_UNITS)
        | set(CONTINUOUS_INFRASTRUCTURE_SERVICE_UNITS)
    )
    unknown = sorted(active_services - allowed)
    if unknown:
        raise RuntimeError(
            "prepared maintenance found an unknown active wb-core service: "
            + ", ".join(unknown)
        )
    return active_services


def _pause_owned_resume_boundary_readback(
    runtime_dir: Path,
    *,
    status: Mapping[str, Any],
    active_service_units: set[str],
    recorded: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    runtime = dict(status.get("runtime_schedules") or {})
    if (
        bool((runtime.get("web_vitrina") or {}).get("active"))
        or list(
            (runtime.get("feedback_complaints") or {}).get("active_runs")
            or []
        )
        or (runtime.get("spp") or {}).get("active_job") is not None
    ):
        raise RuntimeError(
            "prepared maintenance pause-owned runtime is not drained"
        )
    locks = dict(status.get("writer_locks") or {})
    if bool((locks.get("seller_portal") or {}).get("busy")):
        raise RuntimeError(
            "prepared maintenance pause-owned seller-portal writer is active"
        )
    lock_owners = {
        "warehouse_functional": {
            "wb-core-warehouse-functional-sync.service",
            FBS_SHADOW_SERVICE_UNIT,
        },
        "finance_backup": {"wb-core-finance-backup-rotation.service"},
        "web_schedule": {
            "wb-core-sheet-vitrina-refresh.service",
            "wb-core-sheet-vitrina-closure-retry.service",
            "wb-core-sheet-vitrina-canary-restore.service",
            "wb-core-sheet-vitrina-health-candidate.service",
            "wb-core-sheet-vitrina-health-confirmation.service",
        },
        "spp_execution": set(),
    }
    recorded_locks = dict((recorded or {}).get("locks") or {})
    recorded_active_units = set(
        (recorded or {}).get("active_service_units") or []
    )
    for key, value in locks.items():
        if key == "seller_portal" or not bool((value or {}).get("held")):
            continue
        current_owner = active_service_units & lock_owners.get(key, set())
        recorded_owner = (
            recorded_active_units & lock_owners.get(key, set())
        )
        if not current_owner and not (
            bool((recorded_locks.get(key) or {}).get("held"))
            and recorded_owner
        ):
            raise RuntimeError(
                "prepared maintenance pause-owned lock holder is unknown: "
                + key
            )
    sidecars = _sqlite_sidecar_readback(runtime_dir)
    sidecars_hot = any(
        bool(item.get("exists"))
        for item in dict(sidecars.get("sidecars") or {}).values()
    )
    if sidecars_hot and not active_service_units:
        recorded_sidecars = dict(
            ((recorded or {}).get("sidecars") or {}).get("sidecars") or {}
        )
        new_sidecars = [
            suffix
            for suffix, item in dict(sidecars.get("sidecars") or {}).items()
            if bool((item or {}).get("exists"))
            and not bool((recorded_sidecars.get(suffix) or {}).get("exists"))
        ]
        if new_sidecars:
            raise RuntimeError(
                "prepared maintenance pause-owned SQLite sidecar has no writer: "
                + ", ".join(sorted(new_sidecars))
            )
    return {
        "locks": locks,
        "sidecars": sidecars,
        "sidecars_hot": sidecars_hot,
    }


def _validate_pause_owned_resume_drain_status(
    runtime_dir: Path,
    *,
    status: Mapping[str, Any],
    binding: Mapping[str, Any],
    systemd: SystemdClient,
) -> dict[str, Any]:
    if str(binding.get("schema_version") or "") != (
        "business_data_pause_owned_inventory_resume_v2"
    ):
        raise RuntimeError(
            "prepared maintenance pause-owned drain binding is unsupported"
        )
    inventory = list(status.get("discovered_wb_core_timers") or [])
    if (
        inventory != list(binding.get("timer_inventory") or [])
        or inventory != sorted(CLASSIFIED_WB_CORE_TIMER_UNITS)
        or status.get("unknown_wb_core_timers")
        or status.get("cron_entries")
    ):
        raise RuntimeError(
            "prepared maintenance pause-owned drain inventory changed"
        )
    recorded_timers = {
        str(unit): dict(value or {})
        for unit, value in dict(
            binding.get("deploy_drift_timer_states") or {}
        ).items()
    }
    if sorted(recorded_timers) != list(
        binding.get("repaused_timer_units") or []
    ):
        raise RuntimeError(
            "prepared maintenance pause-owned drain timer evidence changed"
        )
    timers = dict(status.get("timers") or {})
    if set(timers) != set(ALL_BUSINESS_TIMER_UNITS):
        raise RuntimeError(
            "prepared maintenance pause-owned drain timer set changed"
        )
    for unit in ALL_BUSINESS_TIMER_UNITS:
        current = dict(timers.get(unit) or {})
        if (
            str(current.get("unit") or "") != unit
            or _unit_state_pair(current) != ("disabled", "inactive")
        ):
            raise RuntimeError(
                "prepared maintenance pause-owned timer restarted: " + unit
            )
        if unit in recorded_timers:
            _validate_pause_owned_timer_trigger(
                unit,
                recorded=recorded_timers[unit],
                current=current,
            )
    active_inventory = _require_pause_owned_active_service_inventory(
        systemd
    )
    service_evidence, active_service_units = (
        _pause_owned_service_generation_evidence(
            dict(status.get("services") or {}),
            [dict(row) for row in status.get("writer_processes") or []],
            recorded=dict(
                binding.get("deploy_drift_service_generations") or {}
            ),
        )
    )
    if active_service_units != (
        active_inventory & set(ALL_BUSINESS_SERVICE_UNITS)
    ):
        raise RuntimeError(
            "prepared maintenance pause-owned active service readback changed"
        )
    initially_active = sorted(
        unit
        for unit, value in service_evidence.items()
        if str((value or {}).get("initial_is_active") or "")
        not in QUIESCENT_SERVICE_STATES
    )
    if initially_active != list(binding.get("draining_service_units") or []):
        raise RuntimeError(
            "prepared maintenance pause-owned draining service set changed"
        )
    boundaries = _pause_owned_resume_boundary_readback(
        runtime_dir,
        status=status,
        active_service_units=active_service_units,
        recorded={
            "active_service_units": initially_active,
            "locks": dict(binding.get("deploy_drift_writer_locks") or {}),
            "sidecars": dict(
                binding.get("deploy_drift_sqlite_sidecars") or {}
            ),
        },
    )
    return {
        "active_service_units": sorted(active_service_units),
        **boundaries,
    }


def _read_exact_deployed_sha(
    deployed_sha_file: Path,
    *,
    expected_deployed_sha: str,
) -> str:
    expected = str(expected_deployed_sha or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", expected):
        raise RuntimeError("prepared abort requires an exact deployed SHA")
    path = Path(deployed_sha_file)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("prepared abort deployed-SHA evidence is unavailable")
    actual = path.read_text(encoding="utf-8").strip().lower()
    if actual != expected:
        raise RuntimeError(
            "prepared abort deployed SHA drifted: "
            f"expected {expected}, got {actual}"
        )
    return actual


def _validate_prepared_abort_quiesce_status(
    runtime_dir: Path,
    *,
    status: Mapping[str, Any],
    binding: Mapping[str, Any],
    systemd: SystemdClient,
    require_disabled: bool,
) -> dict[str, Any]:
    if str(binding.get("schema_version") or "") not in {
        PREPARED_ABORT_QUIESCE_SCHEMA,
        PREPARED_ABORT_RECOVERY_EPOCH_SCHEMA,
        PREPARED_ABORT_PARTIAL_RESTORE_RECOVERY_SCHEMA,
    }:
        raise RuntimeError("prepared abort quiesce binding is unsupported")
    inventory = list(status.get("discovered_wb_core_timers") or [])
    if (
        inventory != list(binding.get("timer_inventory") or [])
        or inventory != sorted(CLASSIFIED_WB_CORE_TIMER_UNITS)
        or status.get("unknown_wb_core_timers")
        or status.get("cron_entries")
    ):
        raise RuntimeError("prepared abort writer inventory changed")
    recorded_timers = {
        str(unit): dict(value or {})
        for unit, value in dict(binding.get("timer_states") or {}).items()
    }
    if set(recorded_timers) != set(ALL_BUSINESS_TIMER_UNITS):
        raise RuntimeError("prepared abort timer evidence changed")
    disable_order = list(binding.get("timer_units_to_disable") or [])
    if disable_order != [
        unit
        for unit in ALL_BUSINESS_TIMER_UNITS
        if _unit_state_pair(recorded_timers[unit])
        != ("disabled", "inactive")
    ]:
        raise RuntimeError("prepared abort timer disable order changed")
    completed = set(binding.get("disabled_timer_units") or [])
    pending = str(binding.get("pending_disable_unit") or "")
    if (
        not completed.issubset(disable_order)
        or (pending and pending not in disable_order)
    ):
        raise RuntimeError("prepared abort durable timer subset changed")
    timers = dict(status.get("timers") or {})
    if set(timers) != set(ALL_BUSINESS_TIMER_UNITS):
        raise RuntimeError("prepared abort current timer set changed")
    for unit in ALL_BUSINESS_TIMER_UNITS:
        current = dict(timers.get(unit) or {})
        recorded = recorded_timers[unit]
        if str(current.get("unit") or "") != unit:
            raise RuntimeError(
                "prepared abort timer identity changed: " + unit
            )
        current_pair = _unit_state_pair(current)
        recorded_pair = _unit_state_pair(recorded)
        if unit not in disable_order:
            if current_pair != ("disabled", "inactive"):
                raise RuntimeError(
                    "prepared abort originally paused timer restarted: "
                    + unit
                )
        elif current_pair == ("disabled", "inactive"):
            if unit not in completed and unit != pending:
                raise RuntimeError(
                    "prepared abort timer changed outside durable subset: "
                    + unit
                )
        elif (
            require_disabled
            or unit in completed
            or current_pair != recorded_pair
        ):
            raise RuntimeError(
                "prepared abort timer restarted after quiesce: " + unit
            )
        _validate_pause_owned_timer_trigger(
            unit,
            recorded=recorded,
            current=current,
        )
    active_inventory = _require_pause_owned_active_service_inventory(
        systemd
    )
    service_evidence, active_service_units = (
        _pause_owned_service_generation_evidence(
            dict(status.get("services") or {}),
            [dict(row) for row in status.get("writer_processes") or []],
            recorded=dict(binding.get("service_generations") or {}),
        )
    )
    if active_service_units != (
        active_inventory & set(ALL_BUSINESS_SERVICE_UNITS)
    ):
        raise RuntimeError(
            "prepared abort active service inventory changed"
        )
    initially_active = sorted(
        unit
        for unit, value in service_evidence.items()
        if str((value or {}).get("initial_is_active") or "")
        not in QUIESCENT_SERVICE_STATES
    )
    if initially_active != list(binding.get("draining_service_units") or []):
        raise RuntimeError("prepared abort draining service set changed")
    boundaries = _pause_owned_resume_boundary_readback(
        runtime_dir,
        status=status,
        active_service_units=active_service_units,
        recorded={
            "active_service_units": initially_active,
            "locks": dict(binding.get("writer_locks") or {}),
            "sidecars": dict(binding.get("sqlite_sidecars") or {}),
        },
    )
    return {
        "active_service_units": sorted(active_service_units),
        **boundaries,
    }


def _validate_completed_prepared_abort_binding_for_recovery(
    binding: Mapping[str, Any],
) -> None:
    """Validate the immutable completed epoch before one correction deploy."""

    if str(binding.get("schema_version") or "") not in {
        PREPARED_ABORT_QUIESCE_SCHEMA,
        PREPARED_ABORT_RECOVERY_EPOCH_SCHEMA,
    }:
        raise RuntimeError("prepared abort recovery source binding is unsupported")
    recorded_timers = {
        str(unit): dict(value or {})
        for unit, value in dict(binding.get("timer_states") or {}).items()
    }
    if set(recorded_timers) != set(ALL_BUSINESS_TIMER_UNITS):
        raise RuntimeError("prepared abort recovery source timer set changed")
    disable_order = list(binding.get("timer_units_to_disable") or [])
    if disable_order != [
        unit
        for unit in ALL_BUSINESS_TIMER_UNITS
        if _unit_state_pair(recorded_timers[unit])
        != ("disabled", "inactive")
    ]:
        raise RuntimeError("prepared abort recovery source disable order changed")
    completed = list(binding.get("disabled_timer_units") or [])
    if (
        str(binding.get("pending_disable_unit") or "")
        or len(completed) != len(set(completed))
        or set(completed) != set(disable_order)
    ):
        raise RuntimeError(
            "prepared abort recovery requires the exact completed timer subset"
        )
    recorded_services = {
        str(unit): dict(value or {})
        for unit, value in dict(binding.get("service_generations") or {}).items()
    }
    if set(recorded_services) != set(ALL_BUSINESS_SERVICE_UNITS):
        raise RuntimeError("prepared abort recovery source service set changed")
    initially_active: set[str] = set()
    for unit in ALL_BUSINESS_SERVICE_UNITS:
        value = recorded_services[unit]
        if (
            str(value.get("unit") or "") != unit
            or str(value.get("is_enabled") or "") != "static"
        ):
            raise RuntimeError(
                "prepared abort recovery source service identity changed: "
                + unit
            )
        active = (
            str(value.get("initial_is_active") or "")
            not in QUIESCENT_SERVICE_STATES
        )
        main_pid = int(value.get("main_pid") or 0)
        rows = [dict(row) for row in value.get("writer_processes") or []]
        if active:
            started_at = str(value.get("started_at") or "")
            _parse_systemd_utc_timestamp(started_at)
            if (
                main_pid <= 0
                or len(rows) != 1
                or int(rows[0].get("pid") or 0) != main_pid
                or str(rows[0].get("marker") or "")
                not in SERVICE_WRITER_PROCESS_MARKERS[unit]
            ):
                raise RuntimeError(
                    "prepared abort recovery source writer identity changed: "
                    + unit
                )
            initially_active.add(unit)
        elif main_pid != 0 or rows:
            raise RuntimeError(
                "prepared abort recovery source terminal service changed: "
                + unit
            )
    if sorted(initially_active) != list(
        binding.get("draining_service_units") or []
    ):
        raise RuntimeError("prepared abort recovery source drain set changed")


def _prepared_abort_baseline_timer_state(
    baseline: Mapping[str, Any],
    unit: str,
) -> dict[str, Any]:
    state = dict((baseline.get("timers") or {}).get(unit) or {})
    if not state and unit == FBS_SHADOW_TIMER_UNIT:
        state = dict(
            (baseline.get("continuous_observer_timers") or {}).get(unit)
            or {}
        )
    if (
        str(state.get("unit") or "") != unit
        or _unit_state_pair(state)
        not in {
            ("enabled", "active"),
            ("disabled", "inactive"),
        }
    ):
        raise RuntimeError(
            "prepared abort outer timer baseline is not exact: " + unit
        )
    return state


def _prepared_abort_breakglass_counters(runtime_dir: Path) -> dict[str, int]:
    """Read exact WBC0027 operation counters without initializing schema."""

    database_path = runtime_dir / "registry_upload_runtime.sqlite3"
    if database_path.is_symlink() or not database_path.is_file():
        raise RuntimeError(
            "prepared abort partial restore business store is unavailable"
        )
    tables = (
        "sheet_vitrina_v1_breakglass_last_good_operations",
        "sheet_vitrina_v1_breakglass_last_good_cells",
        "sheet_vitrina_v1_breakglass_last_good_revocations",
        "sheet_vitrina_v1_breakglass_last_good_revocation_audit",
    )
    connection = sqlite3.connect(
        f"file:{database_path.as_posix()}?mode=ro",
        uri=True,
        timeout=5.0,
    )
    try:
        connection.execute("PRAGMA query_only=ON")
        present = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        counters = {
            table: int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            if table in present
            else 0
            for table in tables
        }
    finally:
        connection.close()
    if any(counters.values()) or any(table in present for table in tables):
        raise RuntimeError(
            "prepared abort partial restore found business operation state"
        )
    return counters


def _prepared_abort_partial_restore_evidence(
    runtime_dir: Path,
    *,
    state: Mapping[str, Any],
    baseline: Mapping[str, Any],
    policy: Mapping[str, Any],
    recovery_epoch: Mapping[str, Any],
) -> dict[str, Any]:
    """Admit only the reviewed nested-warehouse partial restore footprint."""

    if (
        str(state.get("phase") or "") != "abort_quiescing"
        or state.get("exact_prior_state_restored") is True
        or not dict(state.get("prepared_abort_quiesce_readback") or {})
        or state.get("restore_readback")
        or state.get("stable_restore_readback")
    ):
        raise RuntimeError(
            "prepared abort partial restore outer state is not exact"
        )
    _validate_completed_prepared_abort_binding_for_recovery(recovery_epoch)
    warehouse_timer = "wb-core-warehouse-functional-sync.timer"
    outer_timer = _prepared_abort_baseline_timer_state(
        baseline,
        warehouse_timer,
    )
    warehouse_process = dict(
        (policy.get("processes") or {}).get("warehouse_functional") or {}
    )
    if (
        _unit_state_pair(outer_timer) != ("enabled", "active")
        or warehouse_process.get("desired") is not True
    ):
        raise RuntimeError(
            "prepared abort partial restore outer warehouse intent is not exact"
        )
    nested = _load_json_object(
        runtime_dir / WAREHOUSE_MAINTENANCE_STATE_FILENAME
    ) or {}
    nested_baseline = dict(
        (((nested.get("baseline") or {}).get("units") or {}).get("timer"))
        or {}
    )
    nested_restore = dict(
        (
            ((nested.get("restore_readback") or {}).get("units") or {}).get(
                "timer"
            )
        )
        or {}
    )
    if (
        str(nested.get("schema_version") or "")
        != "warehouse_functional_maintenance_v1"
        or str(nested.get("phase") or "") != "restored"
        or str(nested_baseline.get("unit") or "") != warehouse_timer
        or str(nested_restore.get("unit") or "") != warehouse_timer
        or _unit_state_pair(nested_baseline) != ("disabled", "inactive")
        or _unit_state_pair(nested_restore) != ("disabled", "inactive")
    ):
        raise RuntimeError(
            "prepared abort partial restore nested warehouse footprint drifted"
        )
    counters = _prepared_abort_breakglass_counters(runtime_dir)
    return {
        "outer_warehouse_timer_state": {
            "is_enabled": "enabled",
            "is_active": "active",
        },
        "outer_warehouse_timer_fingerprint": _stable_fingerprint(outer_timer),
        "nested_warehouse_timer_state": {
            "is_enabled": "disabled",
            "is_active": "inactive",
        },
        "nested_warehouse_state_fingerprint": _stable_fingerprint(nested),
        "business_operation_counters": counters,
    }


def _validate_hot_journal_recovery_marker(
    runtime_dir: Path,
    *,
    partial_epoch: Mapping[str, Any],
    deployed_sha: str,
    barrier: Mapping[str, Any],
) -> None:
    """Admit only the reviewed physical rollback between abort continuations."""

    marker = _load_json_object(
        runtime_dir / HOT_JOURNAL_RECOVERY_MARKER_FILENAME
    ) or {}
    marker_material = dict(marker)
    marker_fingerprint = str(marker_material.pop("marker_fingerprint", ""))
    result_path = Path(str(marker.get("result_path") or ""))
    if re.fullmatch(
        r"/opt/wb-core-runtime/state/backups/private-evidence/production-goals/"
        r"wbc0027-s047-hot-journal-recovery-[0-9a-f]{8}/"
        r"[0-9a-f]{64}/result\.json",
        str(result_path),
    ) is None:
        raise RuntimeError("hot journal recovery result path is outside scope")
    result = _load_json_object(result_path) or {}
    result_material = dict(result)
    result_fingerprint = str(result_material.pop("result_fingerprint", ""))
    counters = dict(marker.get("business_operation_counters") or {})
    if (
        marker.get("contract_name") != HOT_JOURNAL_RECOVERY_RESULT_CONTRACT
        or marker_fingerprint != _stable_fingerprint(marker_material)
        or marker.get("source_epoch_deployed_sha")
        != partial_epoch.get("deployed_sha")
        or marker.get("deployed_sha") != deployed_sha
        or dict(marker.get("barrier") or {}).get("window_id")
        != barrier.get("window_id")
        or dict(marker.get("barrier") or {}).get("plan_fingerprint")
        != barrier.get("plan_fingerprint")
        or dict(marker.get("barrier") or {}).get("state_fingerprint")
        != barrier.get("state_fingerprint")
        or marker.get("maintenance_partial_epoch_fingerprint")
        != _stable_fingerprint(partial_epoch)
        or marker.get("journal_absent") is not True
        or dict(marker.get("sqlite_readback") or {}).get("integrity_check")
        != "ok"
        or int(
            dict(marker.get("sqlite_readback") or {}).get(
                "foreign_key_violation_count", -1
            )
        )
        != 0
        or any(int(value) != 0 for value in counters.values())
        or result.get("contract_name") != HOT_JOURNAL_RECOVERY_RESULT_CONTRACT
        or result_fingerprint != _stable_fingerprint(result_material)
        or result_fingerprint != marker.get("result_fingerprint")
        or result.get("operation_id") != marker.get("operation_id")
        or result_path.parent.name != marker.get("operation_id")
        or result.get("source_epoch_deployed_sha")
        != marker.get("source_epoch_deployed_sha")
        or result.get("deployed_sha") != marker.get("deployed_sha")
        or result.get("barrier") != marker.get("barrier")
        or result.get("maintenance_partial_epoch_fingerprint")
        != marker.get("maintenance_partial_epoch_fingerprint")
        or result.get("database_after") != marker.get("database_after")
        or result.get("journal_absent") is not True
        or result.get("sqlite_readback") != marker.get("sqlite_readback")
        or result.get("business_operation_counters") != counters
        or result.get("logical_business_delta") != 0
    ):
        raise RuntimeError("hot journal recovery marker identity drifted")


def _restore_outer_warehouse_timer_for_prepared_abort(
    runtime_dir: Path,
    *,
    baseline: Mapping[str, Any],
    policy: Mapping[str, Any],
    systemd: SystemdClient,
) -> dict[str, Any]:
    """Restore warehouse ownership from the immutable outer hold only."""

    del runtime_dir
    timer_unit = "wb-core-warehouse-functional-sync.timer"
    service_unit = "wb-core-warehouse-functional-sync.service"
    baseline_timer = _prepared_abort_baseline_timer_state(
        baseline,
        timer_unit,
    )
    desired = dict(
        (policy.get("processes") or {}).get("warehouse_functional") or {}
    ).get("desired")
    expected_pair = _unit_state_pair(baseline_timer)
    if desired != (expected_pair == ("enabled", "active")):
        raise RuntimeError(
            "prepared abort outer warehouse policy/baseline identity drifted"
        )
    current_timer = systemd.unit_state(timer_unit)
    current_service = systemd.unit_state(service_unit)
    current_service_properties = dict(current_service.get("properties") or {})
    if (
        _unit_state_pair(current_timer) != ("disabled", "inactive")
        or str(current_service.get("unit") or "") != service_unit
        or str(current_service.get("is_enabled") or "") != "static"
        or str(current_service.get("is_active") or "")
        not in QUIESCENT_SERVICE_STATES
        or int(current_service_properties.get("MainPID") or 0) != 0
    ):
        raise RuntimeError(
            "prepared abort outer warehouse restore is not terminal"
        )
    if expected_pair == ("enabled", "active"):
        systemd.enable_now(timer_unit)
    else:
        systemd.disable_now(timer_unit)
    restored_timer = systemd.unit_state(timer_unit)
    if (
        str(restored_timer.get("unit") or "") != timer_unit
        or _unit_state_pair(restored_timer) != expected_pair
    ):
        systemd.disable_now(timer_unit)
        raise RuntimeError(
            "prepared abort outer warehouse timer restore drifted"
        )
    return {
        "status": "restored",
        "source": "immutable_outer_business_maintenance_baseline",
        "timer": restored_timer,
    }


def _prepared_abort_post_restore_service_evidence(
    *,
    status: Mapping[str, Any],
    systemd: SystemdClient,
) -> dict[str, Any]:
    """Admit only known service generations caused by restored-on timers."""

    active_inventory = _require_pause_owned_active_service_inventory(systemd)
    service_generations, active_service_units = (
        _pause_owned_service_generation_evidence(
            dict(status.get("services") or {}),
            [dict(row) for row in status.get("writer_processes") or []],
        )
    )
    if active_service_units != (
        active_inventory & set(ALL_BUSINESS_SERVICE_UNITS)
    ):
        raise RuntimeError(
            "prepared abort post-restore active service inventory changed"
        )
    allowed_active_services = {
        unit.removesuffix(".timer") + ".service"
        for unit, value in dict(status.get("timers") or {}).items()
        if unit in ALL_BUSINESS_TIMER_UNITS
        and _unit_state_pair(dict(value or {})) == ("enabled", "active")
    }
    unexpected = sorted(active_service_units - allowed_active_services)
    if unexpected:
        raise RuntimeError(
            "prepared abort post-restore found a new service generation: "
            + ", ".join(unexpected)
        )
    return {
        "active_service_units": sorted(active_service_units),
        "allowed_active_service_units": sorted(allowed_active_services),
        "service_generations": service_generations,
    }


def _restore_prepared_abort_outer_timer_plan(
    systemd: SystemdClient,
    plan: Mapping[str, bool],
) -> None:
    for unit in PREPARED_ABORT_OUTER_TIMER_UNITS:
        current = systemd.unit_state(unit)
        if (
            str(current.get("unit") or "") != unit
            or _unit_state_pair(current)
            not in {
                ("enabled", "active"),
                ("disabled", "inactive"),
            }
        ):
            raise RuntimeError(
                "prepared abort outer timer readback is not exact: " + unit
            )
        expected = (
            ("enabled", "active")
            if plan.get(unit) is True
            else ("disabled", "inactive")
        )
        if _unit_state_pair(current) == expected:
            continue
        if plan.get(unit) is True:
            systemd.enable_now(unit)
        else:
            systemd.disable_now(unit)
        if _unit_state_pair(systemd.unit_state(unit)) != expected:
            raise RuntimeError(
                "prepared abort outer timer restore failed: " + unit
            )


def _require_prepared_abort_outer_timer_plan_restored(
    status: Mapping[str, Any],
    plan: Mapping[str, bool],
) -> None:
    for unit in PREPARED_ABORT_OUTER_TIMER_UNITS:
        expected = (
            ("enabled", "active")
            if plan.get(unit) is True
            else ("disabled", "inactive")
        )
        actual = _unit_state_pair(
            dict((status.get("timers") or {}).get(unit) or {})
        )
        if actual != expected:
            raise RuntimeError(
                "prepared abort outer timer restore is not stable: " + unit
            )


def maintenance_abort_prepared(
    runtime_dir: Path,
    *,
    systemd: SystemdClient,
    schedules: RuntimeScheduleClient,
    expected_revision: int,
    window_id: str,
    plan_fingerprint: str,
    expected_deployed_sha: str,
    deployed_sha_file: Path,
    proc_root: Path = Path("/proc"),
    actor: str = "repo_owned_cli",
    reason: str = "abort exact prepared maintenance",
    wait_timeout_seconds: float = 1200.0,
    poll_interval_seconds: float = 2.0,
    warehouse_restore: Any | None = None,
    autoanswers_reconcile: Any | None = None,
) -> dict[str, Any]:
    """Quiesce one exact prepared revision, restore it, and abort its barrier."""

    runtime_dir = Path(runtime_dir).resolve()
    deployed_sha = _read_exact_deployed_sha(
        deployed_sha_file,
        expected_deployed_sha=expected_deployed_sha,
    )
    state_path = runtime_dir / STATE_FILENAME
    audit_path = runtime_dir / AUDIT_FILENAME
    state = _load_json_object(state_path) or {}
    barrier = barrier_status(runtime_dir)
    if (
        barrier.get("active") is not True
        or str(barrier.get("phase") or "") != "acquiring"
        or barrier.get("hold_confirmed") is not False
        or str(barrier.get("window_id") or "") != window_id
        or str(barrier.get("plan_fingerprint") or "")
        != plan_fingerprint
    ):
        raise RuntimeError("prepared abort barrier identity drifted")
    phase = str(state.get("phase") or "")
    if (
        phase == "restored"
        and state.get("exact_prior_state_restored") is True
    ):
        restore_readback = maintenance_barrier_abort_readback(
            runtime_dir,
            systemd=systemd,
            schedules=schedules,
            proc_root=proc_root,
        )
        released = abort_barrier_acquire(
            runtime_dir,
            window_id=window_id,
            plan_fingerprint=plan_fingerprint,
            actor=actor,
            reason=reason,
            restore_readback=restore_readback,
        )
        return {
            "status": "released",
            "idempotent": True,
            "deployed_sha": deployed_sha,
            "restore": restore_readback,
            "barrier": released,
        }
    binding = dict(state.get("prepared_abort_quiesce_binding") or {})
    recovery_epoch = dict(
        state.get("prepared_abort_recovery_epoch") or {}
    )
    partial_restore_epoch = dict(
        state.get("prepared_abort_partial_restore_recovery_epoch") or {}
    )
    if phase not in {"prepared", "abort_quiescing"}:
        raise RuntimeError("prepared abort requires prepared/quiescing state")
    if (phase == "abort_quiescing") != bool(binding):
        raise RuntimeError("prepared abort quiesce phase/binding is incomplete")
    if recovery_epoch and phase != "abort_quiescing":
        raise RuntimeError("prepared abort recovery epoch phase is incomplete")
    if partial_restore_epoch and phase != "abort_quiescing":
        raise RuntimeError(
            "prepared abort partial restore recovery phase is incomplete"
        )
    baseline = dict(state.get("baseline") or {})
    prepare_readback = dict(state.get("prepare_readback") or {})
    persisted_signature = dict(
        state.get("control_signature_before_hold") or {}
    )
    if (
        str(state.get("schema_version") or "") != SCHEMA_VERSION
        or str(baseline.get("schema_version") or "") != SCHEMA_VERSION
        or str(prepare_readback.get("schema_version") or "")
        != SCHEMA_VERSION
        or not str(persisted_signature.get("fingerprint") or "")
        or maintenance_control_signature(
            baseline,
            runtime_dir=runtime_dir,
        )
        != persisted_signature
    ):
        raise RuntimeError("prepared abort original control evidence drifted")
    full_timers = set(ALL_BUSINESS_TIMER_UNITS)
    legacy_timers = full_timers - {FBS_SHADOW_TIMER_UNIT}
    full_services = set(ALL_BUSINESS_SERVICE_UNITS)
    legacy_services = full_services - {FBS_SHADOW_SERVICE_UNIT}

    def prepared_inventory_kind(value: Mapping[str, Any]) -> str:
        timers = set(dict(value.get("timers") or {}))
        observers = set(
            dict(value.get("continuous_observer_timers") or {})
        )
        services = set(dict(value.get("services") or {}))
        if (
            timers == full_timers
            and observers == set(CONTINUOUS_OBSERVER_TIMER_UNITS)
            and services == full_services
        ):
            return "current"
        if (
            timers == legacy_timers
            and observers
            == set(CONTINUOUS_OBSERVER_TIMER_UNITS)
            | {FBS_SHADOW_TIMER_UNIT}
            and services == legacy_services
        ):
            return "legacy_fbs_observer"
        raise RuntimeError("prepared abort captured unit inventory drifted")

    if prepared_inventory_kind(baseline) != prepared_inventory_kind(
        prepare_readback
    ):
        raise RuntimeError("prepared abort captured unit inventory changed")
    if not _load_json_object(runtime_dir / POLICY_FILENAME):
        raise RuntimeError("prepared abort owner-policy evidence is missing")
    policy = load_or_initialize_owner_policy(runtime_dir)
    if int(policy.get("revision") or 0) != int(expected_revision):
        raise RuntimeError(
            "prepared abort owner-policy revision drifted"
        )
    prepared_policy = dict(prepare_readback.get("auto_updates") or {})
    if (
        int(prepared_policy.get("revision") or 0) != int(expected_revision)
        or str(prepared_policy.get("policy_fingerprint") or "")
        != str(policy.get("policy_fingerprint") or "")
    ):
        raise RuntimeError("prepared abort owner-policy identity drifted")
    prepared_runtime_schedules = dict(
        prepare_readback.get("runtime_schedules") or {}
    )
    if set(prepared_runtime_schedules) != {
        "web_vitrina",
        "feedback_complaints",
        "spp",
    }:
        raise RuntimeError(
            "prepared abort runtime-schedule prestate drifted"
        )
    # Binding is deliberately independent of the loopback schedule surface.
    # A busy writer can make that read time out, but the exact known timer and
    # service generations must be made durable before they are paused.  Fresh
    # schedule/job state is required immediately after the timers are paused.
    current = maintenance_status(
        runtime_dir,
        systemd=systemd,
        schedules=schedules,
        proc_root=proc_root,
        runtime_schedule_readback=prepared_runtime_schedules,
    )
    inventory = list(current.get("discovered_wb_core_timers") or [])
    if (
        list(baseline.get("discovered_wb_core_timers") or []) != inventory
        or list(prepare_readback.get("discovered_wb_core_timers") or [])
        != inventory
        or inventory != sorted(CLASSIFIED_WB_CORE_TIMER_UNITS)
        or current.get("unknown_wb_core_timers")
        or current.get("cron_entries")
    ):
        raise RuntimeError("prepared abort writer inventory drifted")
    if not binding:
        timers = {
            unit: dict((current.get("timers") or {}).get(unit) or {})
            for unit in ALL_BUSINESS_TIMER_UNITS
        }
        if any(
            str(value.get("unit") or "") != unit
            or _unit_state_pair(value)
            not in {
                ("enabled", "active"),
                ("disabled", "inactive"),
            }
            for unit, value in timers.items()
        ):
            raise RuntimeError("prepared abort timer state is not exact")
        active_inventory = _require_pause_owned_active_service_inventory(
            systemd
        )
        service_generations, active_service_units = (
            _pause_owned_service_generation_evidence(
                dict(current.get("services") or {}),
                [
                    dict(row)
                    for row in current.get("writer_processes") or []
                ],
            )
        )
        if active_service_units != (
            active_inventory & set(ALL_BUSINESS_SERVICE_UNITS)
        ):
            raise RuntimeError(
                "prepared abort active service inventory changed"
            )
        boundaries = _pause_owned_resume_boundary_readback(
            runtime_dir,
            status=current,
            active_service_units=active_service_units,
        )
        binding = {
            "schema_version": PREPARED_ABORT_QUIESCE_SCHEMA,
            "window_id": window_id,
            "plan_fingerprint": plan_fingerprint,
            "barrier_state_fingerprint": str(
                barrier.get("state_fingerprint") or ""
            ),
            "deployed_sha": deployed_sha,
            "owner_policy_revision": int(expected_revision),
            "owner_policy_fingerprint": str(
                policy.get("policy_fingerprint") or ""
            ),
            "baseline_fingerprint": _stable_fingerprint(baseline),
            "prepare_readback_fingerprint": _stable_fingerprint(
                prepare_readback
            ),
            "control_signature": str(
                persisted_signature.get("fingerprint") or ""
            ),
            "timer_inventory": inventory,
            "timer_states": timers,
            "timer_units_to_disable": [
                unit
                for unit in ALL_BUSINESS_TIMER_UNITS
                if _unit_state_pair(timers[unit])
                != ("disabled", "inactive")
            ],
            "pending_disable_unit": "",
            "disabled_timer_units": [],
            "service_generations": service_generations,
            "draining_service_units": sorted(active_service_units),
            "writer_locks": dict(boundaries["locks"]),
            "sqlite_sidecars": dict(boundaries["sidecars"]),
            "bound_at": _utc_now(),
        }
        state["phase"] = "abort_quiescing"
        state["prepared_abort_quiesce_binding"] = binding
        _save_json_0600(state_path, state)
        _fsync_directory(runtime_dir)
        _append_audit_0600(
            audit_path,
            {
                "event": "prepared_abort_quiesce_bound",
                "captured_at": _utc_now(),
                "binding": binding,
            },
        )
    else:
        identity = {
            "window_id": window_id,
            "plan_fingerprint": plan_fingerprint,
            "barrier_state_fingerprint": str(
                barrier.get("state_fingerprint") or ""
            ),
            "owner_policy_revision": int(expected_revision),
            "owner_policy_fingerprint": str(
                policy.get("policy_fingerprint") or ""
            ),
            "baseline_fingerprint": _stable_fingerprint(baseline),
            "prepare_readback_fingerprint": _stable_fingerprint(
                prepare_readback
            ),
            "control_signature": str(
                persisted_signature.get("fingerprint") or ""
            ),
            "timer_inventory": inventory,
        }
        if any(
            binding.get(key) != value for key, value in identity.items()
        ):
            raise RuntimeError("prepared abort durable identity drifted")
    quiesce = binding
    quiesce_state_key = "prepared_abort_quiesce_binding"
    quiesce_event_prefix = "prepared_abort"
    if binding:
        bound_deployed_sha = str(binding.get("deployed_sha") or "")
        if re.fullmatch(r"[0-9a-f]{40}", bound_deployed_sha) is None:
            raise RuntimeError("prepared abort durable deployed SHA drifted")
        if bound_deployed_sha == deployed_sha:
            if recovery_epoch:
                raise RuntimeError(
                    "prepared abort recovery epoch deployed SHA moved backwards"
                )
        else:
            _validate_completed_prepared_abort_binding_for_recovery(binding)
            source_binding_fingerprint = _stable_fingerprint(binding)
            if not recovery_epoch:
                timers = {
                    unit: dict((current.get("timers") or {}).get(unit) or {})
                    for unit in ALL_BUSINESS_TIMER_UNITS
                }
                if any(
                    str(value.get("unit") or "") != unit
                    or _unit_state_pair(value)
                    not in {
                        ("enabled", "active"),
                        ("disabled", "inactive"),
                    }
                    for unit, value in timers.items()
                ):
                    raise RuntimeError(
                        "prepared abort recovery timer state is not exact"
                    )
                source_disable_order = set(
                    binding.get("timer_units_to_disable") or []
                )
                reactivated_units = [
                    unit
                    for unit in ALL_BUSINESS_TIMER_UNITS
                    if _unit_state_pair(timers[unit])
                    != ("disabled", "inactive")
                ]
                if not set(reactivated_units).issubset(source_disable_order):
                    raise RuntimeError(
                        "prepared abort recovery found a timer outside the "
                        "completed source subset"
                    )
                active_inventory = (
                    _require_pause_owned_active_service_inventory(systemd)
                )
                service_generations, active_service_units = (
                    _pause_owned_service_generation_evidence(
                        dict(current.get("services") or {}),
                        [
                            dict(row)
                            for row in current.get("writer_processes") or []
                        ],
                    )
                )
                if active_service_units != (
                    active_inventory & set(ALL_BUSINESS_SERVICE_UNITS)
                ):
                    raise RuntimeError(
                        "prepared abort recovery active service inventory changed"
                    )
                boundaries = _pause_owned_resume_boundary_readback(
                    runtime_dir,
                    status=current,
                    active_service_units=active_service_units,
                )
                recovery_epoch = {
                    "schema_version": (
                        PREPARED_ABORT_RECOVERY_EPOCH_SCHEMA
                    ),
                    "epoch": 1,
                    "window_id": window_id,
                    "plan_fingerprint": plan_fingerprint,
                    "barrier_state_fingerprint": str(
                        barrier.get("state_fingerprint") or ""
                    ),
                    "source_deployed_sha": bound_deployed_sha,
                    "deployed_sha": deployed_sha,
                    "source_binding_fingerprint": (
                        source_binding_fingerprint
                    ),
                    "owner_policy_revision": int(expected_revision),
                    "owner_policy_fingerprint": str(
                        policy.get("policy_fingerprint") or ""
                    ),
                    "baseline_fingerprint": _stable_fingerprint(baseline),
                    "prepare_readback_fingerprint": _stable_fingerprint(
                        prepare_readback
                    ),
                    "control_signature": str(
                        persisted_signature.get("fingerprint") or ""
                    ),
                    "timer_inventory": inventory,
                    "timer_states": timers,
                    "timer_units_to_disable": reactivated_units,
                    "pending_disable_unit": "",
                    "disabled_timer_units": [],
                    "service_generations": service_generations,
                    "draining_service_units": sorted(
                        active_service_units
                    ),
                    "writer_locks": dict(boundaries["locks"]),
                    "sqlite_sidecars": dict(boundaries["sidecars"]),
                    "bound_at": _utc_now(),
                }
                state["prepared_abort_recovery_epoch"] = recovery_epoch
                _save_json_0600(state_path, state)
                _fsync_directory(runtime_dir)
                _append_audit_0600(
                    audit_path,
                    {
                        "event": "prepared_abort_recovery_epoch_bound",
                        "captured_at": _utc_now(),
                        "recovery_epoch": recovery_epoch,
                    },
                )
            recovery_deployed_sha = (
                str(recovery_epoch.get("deployed_sha") or "")
                if recovery_epoch
                else deployed_sha
            )
            recovery_identity = {
                "schema_version": PREPARED_ABORT_RECOVERY_EPOCH_SCHEMA,
                "epoch": 1,
                "window_id": window_id,
                "plan_fingerprint": plan_fingerprint,
                "barrier_state_fingerprint": str(
                    barrier.get("state_fingerprint") or ""
                ),
                "source_deployed_sha": bound_deployed_sha,
                "deployed_sha": recovery_deployed_sha,
                "source_binding_fingerprint": source_binding_fingerprint,
                "owner_policy_revision": int(expected_revision),
                "owner_policy_fingerprint": str(
                    policy.get("policy_fingerprint") or ""
                ),
                "baseline_fingerprint": _stable_fingerprint(baseline),
                "prepare_readback_fingerprint": _stable_fingerprint(
                    prepare_readback
                ),
                "control_signature": str(
                    persisted_signature.get("fingerprint") or ""
                ),
                "timer_inventory": inventory,
            }
            if any(
                recovery_epoch.get(key) != value
                for key, value in recovery_identity.items()
            ):
                raise RuntimeError(
                    "prepared abort recovery epoch identity drifted"
                )
            quiesce = recovery_epoch
            quiesce_state_key = "prepared_abort_recovery_epoch"
            quiesce_event_prefix = "prepared_abort_recovery"
            if recovery_deployed_sha != deployed_sha:
                _validate_completed_prepared_abort_binding_for_recovery(
                    recovery_epoch
                )
                source_recovery_fingerprint = _stable_fingerprint(
                    recovery_epoch
                )
                if not partial_restore_epoch:
                    partial_evidence = (
                        _prepared_abort_partial_restore_evidence(
                            runtime_dir,
                            state=state,
                            baseline=baseline,
                            policy=policy,
                            recovery_epoch=recovery_epoch,
                        )
                    )
                    timers = {
                        unit: dict(
                            (current.get("timers") or {}).get(unit) or {}
                        )
                        for unit in ALL_BUSINESS_TIMER_UNITS
                    }
                    if any(
                        str(value.get("unit") or "") != unit
                        or _unit_state_pair(value)
                        not in {
                            ("enabled", "active"),
                            ("disabled", "inactive"),
                        }
                        for unit, value in timers.items()
                    ):
                        raise RuntimeError(
                            "prepared abort partial restore timer state is not exact"
                        )
                    reactivated_units = [
                        unit
                        for unit in ALL_BUSINESS_TIMER_UNITS
                        if _unit_state_pair(timers[unit])
                        != ("disabled", "inactive")
                    ]
                    allowed_reactivated_units = {
                        unit
                        for unit in ALL_BUSINESS_TIMER_UNITS
                        if _unit_state_pair(
                            _prepared_abort_baseline_timer_state(
                                baseline,
                                unit,
                            )
                        )
                        == ("enabled", "active")
                    }
                    if not set(reactivated_units).issubset(
                        allowed_reactivated_units
                    ):
                        raise RuntimeError(
                            "prepared abort partial restore found a timer outside "
                            "the immutable outer enabled set"
                        )
                    active_inventory = (
                        _require_pause_owned_active_service_inventory(systemd)
                    )
                    service_generations, active_service_units = (
                        _pause_owned_service_generation_evidence(
                            dict(current.get("services") or {}),
                            [
                                dict(row)
                                for row in current.get("writer_processes") or []
                            ],
                        )
                    )
                    if active_service_units != (
                        active_inventory & set(ALL_BUSINESS_SERVICE_UNITS)
                    ):
                        raise RuntimeError(
                            "prepared abort partial restore active service "
                            "inventory changed"
                        )
                    boundaries = _pause_owned_resume_boundary_readback(
                        runtime_dir,
                        status=current,
                        active_service_units=active_service_units,
                    )
                    partial_restore_epoch = {
                        "schema_version": (
                            PREPARED_ABORT_PARTIAL_RESTORE_RECOVERY_SCHEMA
                        ),
                        "epoch": 2,
                        "window_id": window_id,
                        "plan_fingerprint": plan_fingerprint,
                        "barrier_state_fingerprint": str(
                            barrier.get("state_fingerprint") or ""
                        ),
                        "source_deployed_sha": recovery_deployed_sha,
                        "deployed_sha": deployed_sha,
                        "source_recovery_fingerprint": (
                            source_recovery_fingerprint
                        ),
                        "owner_policy_revision": int(expected_revision),
                        "owner_policy_fingerprint": str(
                            policy.get("policy_fingerprint") or ""
                        ),
                        "baseline_fingerprint": _stable_fingerprint(baseline),
                        "prepare_readback_fingerprint": _stable_fingerprint(
                            prepare_readback
                        ),
                        "control_signature": str(
                            persisted_signature.get("fingerprint") or ""
                        ),
                        "timer_inventory": inventory,
                        "timer_states": timers,
                        "timer_units_to_disable": reactivated_units,
                        "pending_disable_unit": "",
                        "disabled_timer_units": [],
                        "service_generations": service_generations,
                        "draining_service_units": sorted(active_service_units),
                        "writer_locks": dict(boundaries["locks"]),
                        "sqlite_sidecars": dict(boundaries["sidecars"]),
                        **partial_evidence,
                        "bound_at": _utc_now(),
                    }
                    state[
                        "prepared_abort_partial_restore_recovery_epoch"
                    ] = partial_restore_epoch
                    _save_json_0600(state_path, state)
                    _fsync_directory(runtime_dir)
                    _append_audit_0600(
                        audit_path,
                        {
                            "event": (
                                "prepared_abort_partial_restore_recovery_bound"
                            ),
                            "captured_at": _utc_now(),
                            "recovery_epoch": partial_restore_epoch,
                        },
                    )
                partial_deployed_sha = deployed_sha
                if partial_restore_epoch and str(
                    partial_restore_epoch.get("deployed_sha") or ""
                ) != deployed_sha:
                    _validate_hot_journal_recovery_marker(
                        runtime_dir,
                        partial_epoch=partial_restore_epoch,
                        deployed_sha=deployed_sha,
                        barrier=barrier,
                    )
                    partial_deployed_sha = str(
                        partial_restore_epoch.get("deployed_sha") or ""
                    )
                partial_identity = {
                    "schema_version": (
                        PREPARED_ABORT_PARTIAL_RESTORE_RECOVERY_SCHEMA
                    ),
                    "epoch": 2,
                    "window_id": window_id,
                    "plan_fingerprint": plan_fingerprint,
                    "barrier_state_fingerprint": str(
                        barrier.get("state_fingerprint") or ""
                    ),
                    "source_deployed_sha": recovery_deployed_sha,
                    "deployed_sha": partial_deployed_sha,
                    "source_recovery_fingerprint": source_recovery_fingerprint,
                    "owner_policy_revision": int(expected_revision),
                    "owner_policy_fingerprint": str(
                        policy.get("policy_fingerprint") or ""
                    ),
                    "baseline_fingerprint": _stable_fingerprint(baseline),
                    "prepare_readback_fingerprint": _stable_fingerprint(
                        prepare_readback
                    ),
                    "control_signature": str(
                        persisted_signature.get("fingerprint") or ""
                    ),
                    "timer_inventory": inventory,
                }
                if any(
                    partial_restore_epoch.get(key) != value
                    for key, value in partial_identity.items()
                ):
                    raise RuntimeError(
                        "prepared abort partial restore recovery identity drifted"
                    )
                current_partial_evidence = (
                    _prepared_abort_partial_restore_evidence(
                        runtime_dir,
                        state=state,
                        baseline=baseline,
                        policy=policy,
                        recovery_epoch=recovery_epoch,
                    )
                )
                if any(
                    partial_restore_epoch.get(key) != value
                    for key, value in current_partial_evidence.items()
                ):
                    raise RuntimeError(
                        "prepared abort partial restore evidence drifted"
                    )
                quiesce = partial_restore_epoch
                quiesce_state_key = (
                    "prepared_abort_partial_restore_recovery_epoch"
                )
                quiesce_event_prefix = "prepared_abort_partial_restore_recovery"
    _validate_prepared_abort_quiesce_status(
        runtime_dir,
        status=current,
        binding=quiesce,
        systemd=systemd,
        require_disabled=False,
    )

    def persist_quiesce(*, event: str, unit: str) -> None:
        state[quiesce_state_key] = quiesce
        _save_json_0600(state_path, state)
        _fsync_directory(runtime_dir)
        _append_audit_0600(
            audit_path,
            {
                "event": event,
                "captured_at": _utc_now(),
                "unit": unit,
                "binding": quiesce,
            },
        )

    completed = set(quiesce.get("disabled_timer_units") or [])
    for unit in list(quiesce.get("timer_units_to_disable") or []):
        current_state = systemd.unit_state(unit)
        if unit in completed:
            if _unit_state_pair(current_state) != ("disabled", "inactive"):
                raise RuntimeError(
                    "prepared abort completed timer restarted: " + unit
                )
            continue
        quiesce["pending_disable_unit"] = unit
        persist_quiesce(
            event=f"{quiesce_event_prefix}_timer_disable_intent",
            unit=unit,
        )
        systemd.disable_now(unit)
        if _unit_state_pair(systemd.unit_state(unit)) != (
            "disabled",
            "inactive",
        ):
            raise RuntimeError(
                "prepared abort timer did not become paused: " + unit
        )
        completed.add(unit)
        quiesce["disabled_timer_units"] = sorted(completed)
        quiesce["pending_disable_unit"] = ""
        persist_quiesce(
            event=f"{quiesce_event_prefix}_timer_disabled",
            unit=unit,
        )

    deadline = time.monotonic() + max(0.0, float(wait_timeout_seconds))
    while True:
        current = maintenance_status(
            runtime_dir,
            systemd=systemd,
            schedules=schedules,
            proc_root=proc_root,
        )
        drain = _validate_prepared_abort_quiesce_status(
            runtime_dir,
            status=current,
            binding=quiesce,
            systemd=systemd,
            require_disabled=True,
        )
        if current.get("quiet") is True and not bool(
            drain.get("sidecars_hot")
        ):
            break
        if time.monotonic() >= deadline:
            state["prepared_abort_quiesce_last_readback"] = current
            _save_json_0600(state_path, state)
            _fsync_directory(runtime_dir)
            _append_audit_0600(
                audit_path,
                {
                    "event": "prepared_abort_quiesce_timeout",
                    "captured_at": _utc_now(),
                    "status": current,
                },
            )
            raise TimeoutError(
                "timed out waiting for prepared abort quiet window"
            )
        time.sleep(max(0.05, float(poll_interval_seconds)))
    stable = _stable_quiet_readback(
        runtime_dir,
        first=current,
        systemd=systemd,
        schedules=schedules,
        proc_root=proc_root,
        poll_interval_seconds=poll_interval_seconds,
        drain_validator=lambda value: (
            _validate_prepared_abort_quiesce_status(
                runtime_dir,
                status=value,
                binding=quiesce,
                systemd=systemd,
                require_disabled=True,
            )
        ),
    )
    state["prepared_abort_quiesce_readback"] = {
        key: value for key, value in stable.items() if key != "status"
    }
    _save_json_0600(state_path, state)
    _fsync_directory(runtime_dir)
    _append_audit_0600(
        audit_path,
        {
            "event": "prepared_abort_quiesced",
            "captured_at": _utc_now(),
            "readback": state["prepared_abort_quiesce_readback"],
        },
    )
    effective_warehouse_restore = warehouse_restore
    outer_timer_restore_plan: dict[str, bool] = {}
    post_restore_validator: Any | None = None
    if partial_restore_epoch:
        effective_warehouse_restore = lambda value: (
            _restore_outer_warehouse_timer_for_prepared_abort(
                value,
                baseline=baseline,
                policy=policy,
                systemd=systemd,
            )
        )
        outer_timer_restore_plan = {
            unit: _unit_state_pair(
                _prepared_abort_baseline_timer_state(baseline, unit)
            )
            == ("enabled", "active")
            for unit in PREPARED_ABORT_OUTER_TIMER_UNITS
        }
        post_restore_validator = lambda value: (
            _prepared_abort_post_restore_service_evidence(
                status=value,
                systemd=systemd,
            )
        )
    restored = maintenance_restore(
        runtime_dir,
        systemd=systemd,
        schedules=schedules,
        proc_root=proc_root,
        actor=actor,
        reason=reason,
        expected_revision=int(expected_revision),
        warehouse_restore=effective_warehouse_restore,
        autoanswers_reconcile=autoanswers_reconcile,
        require_stable_readback=True,
        prepared_abort_outer_timer_restore_plan=outer_timer_restore_plan,
        prepared_abort_post_restore_validator=post_restore_validator,
        poll_interval_seconds=poll_interval_seconds,
    )
    _fsync_directory(runtime_dir)
    restore_readback = maintenance_barrier_abort_readback(
        runtime_dir,
        systemd=systemd,
        schedules=schedules,
        proc_root=proc_root,
    )
    released = abort_barrier_acquire(
        runtime_dir,
        window_id=window_id,
        plan_fingerprint=plan_fingerprint,
        actor=actor,
        reason=reason,
        restore_readback=restore_readback,
    )
    result = {
        "status": "released",
        "idempotent": False,
        "deployed_sha": deployed_sha,
        "abort_quiesce": state["prepared_abort_quiesce_readback"],
        "restore": restored,
        "barrier": released,
    }
    if recovery_epoch:
        result["abort_recovery_epoch"] = recovery_epoch
    if partial_restore_epoch:
        result["abort_partial_restore_recovery_epoch"] = (
            partial_restore_epoch
        )
    return result


def _resume_legacy_fbs_pause_ownership(
    runtime_dir: Path,
    *,
    existing: dict[str, Any],
    before: Mapping[str, Any],
    baseline: Mapping[str, Any],
    persisted_readback: Mapping[str, Any],
    persisted_signature: Mapping[str, Any],
    current_revision: int,
    current_policy_fingerprint: str,
    systemd: SystemdClient,
    window_id: str,
    plan_fingerprint: str,
) -> dict[str, Any] | None:
    """Bind the exact pre-upgrade FBS timer prestate before pausing it.

    Runtime revisions before FBS became pause-owned captured that timer in the
    continuous-observer section.  Only an exact active prepared revision may
    reuse that durable slot; current timer state is never captured or rebased.
    """

    if FBS_SHADOW_TIMER_UNIT in dict(baseline.get("timers") or {}):
        return None
    legacy_state = dict(
        (baseline.get("continuous_observer_timers") or {}).get(
            FBS_SHADOW_TIMER_UNIT
        )
        or {}
    )
    legacy_pair = _unit_state_pair(legacy_state)
    if legacy_pair not in {
        ("enabled", "active"),
        ("disabled", "inactive"),
    }:
        raise RuntimeError(
            "prepared maintenance FBS shadow prestate is not exactly restorable"
        )
    if str(legacy_state.get("unit") or "") != FBS_SHADOW_TIMER_UNIT:
        raise RuntimeError(
            "prepared maintenance FBS shadow prestate identity drifted"
        )
    if str(existing.get("phase") or "") != "prepared":
        raise RuntimeError(
            "legacy FBS pause ownership requires the same prepared revision"
        )
    barrier = barrier_status(runtime_dir)
    if (
        barrier.get("active") is not True
        or str(barrier.get("phase") or "") != "acquiring"
        or barrier.get("hold_confirmed") is not False
        or str(barrier.get("window_id") or "") != window_id
        or str(barrier.get("plan_fingerprint") or "") != plan_fingerprint
    ):
        raise RuntimeError(
            "prepared maintenance FBS pause ownership barrier identity drifted"
        )
    if (
        str(existing.get("schema_version") or "") != SCHEMA_VERSION
        or str(baseline.get("schema_version") or "") != SCHEMA_VERSION
        or str(persisted_readback.get("schema_version") or "")
        != SCHEMA_VERSION
    ):
        raise RuntimeError(
            "prepared maintenance FBS pause ownership schema drifted"
        )
    barrier_started = _parse_utc_instant(
        barrier.get("started_at"), label="barrier started_at"
    )
    baseline_captured = _parse_utc_instant(
        baseline.get("captured_at"), label="maintenance baseline captured_at"
    )
    hold_started = _parse_utc_instant(
        existing.get("hold_started_at"), label="maintenance hold_started_at"
    )
    prepared_at = _parse_utc_instant(
        existing.get("prepared_at"), label="maintenance prepared_at"
    )
    if not barrier_started <= baseline_captured <= hold_started <= prepared_at:
        raise RuntimeError(
            "prepared maintenance FBS pause ownership prestate chronology drifted"
        )
    current_inventory = list(before.get("discovered_wb_core_timers") or [])
    if (
        list(baseline.get("discovered_wb_core_timers") or [])
        != current_inventory
        or list(persisted_readback.get("discovered_wb_core_timers") or [])
        != current_inventory
        or current_inventory != sorted(CLASSIFIED_WB_CORE_TIMER_UNITS)
    ):
        raise RuntimeError(
            "prepared maintenance FBS pause ownership timer inventory drifted"
        )
    persisted_fbs_state = dict(
        (persisted_readback.get("continuous_observer_timers") or {}).get(
            FBS_SHADOW_TIMER_UNIT
        )
        or {}
    )
    if _unit_state_pair(persisted_fbs_state) != legacy_pair:
        raise RuntimeError(
            "prepared maintenance FBS pause ownership prestate drifted"
        )
    if before.get("unknown_wb_core_timers") or before.get("cron_entries"):
        raise RuntimeError(
            "prepared maintenance FBS pause ownership found unclassified execution"
        )
    active_service_inventory = _require_pause_owned_active_service_inventory(
        systemd
    )
    prior_binding = dict(
        existing.get("pause_owned_inventory_resume_binding") or {}
    )
    if prior_binding and str(prior_binding.get("schema_version") or "") != (
        "business_data_pause_owned_inventory_resume_v2"
    ):
        raise RuntimeError(
            "prepared maintenance pause-owned resume binding predates exact drain evidence"
        )
    timers = dict(before.get("timers") or {})
    persisted_timers = dict(persisted_readback.get("timers") or {})
    baseline_services = set(dict(baseline.get("services") or {}))
    persisted_services = set(dict(persisted_readback.get("services") or {}))
    expected_legacy_services = set(ALL_BUSINESS_SERVICE_UNITS) - {
        FBS_SHADOW_SERVICE_UNIT
    }
    if (
        baseline_services != expected_legacy_services
        or persisted_services != expected_legacy_services
    ):
        raise RuntimeError(
            "prepared maintenance FBS pause ownership service inventory drifted"
        )
    current_drift_timer_states: dict[str, dict[str, Any]] = {}
    for unit in ALL_BUSINESS_TIMER_UNITS:
        current_state = dict(timers.get(unit) or {})
        persisted_state = dict(persisted_timers.get(unit) or {})
        if unit == FBS_SHADOW_TIMER_UNIT:
            persisted_state = persisted_fbs_state
        if (
            str(current_state.get("unit") or "") != unit
            or str(persisted_state.get("unit") or "") != unit
        ):
            raise RuntimeError(
                "prepared maintenance pause-owned timer identity drifted: "
                + unit
            )
        if _unit_state_pair(persisted_state) not in {
            ("disabled", "inactive"),
            ("enabled", "active"),
        }:
            raise RuntimeError(
                "prepared maintenance pause-owned timer prestate drifted: "
                + unit
            )
        current_pair = _unit_state_pair(current_state)
        if current_pair not in {
            ("disabled", "inactive"),
            ("enabled", "active"),
        }:
            raise RuntimeError(
                "prepared maintenance pause-owned timer runtime drifted: "
                + unit
            )
        if current_pair != ("disabled", "inactive"):
            current_drift_timer_states[unit] = current_state
    recorded_drift_timer_states = {
        str(unit): dict(state or {})
        for unit, state in dict(
            prior_binding.get("deploy_drift_timer_states") or {}
        ).items()
    }
    if prior_binding:
        if (
            "deploy_drift_timer_states" not in prior_binding
            or sorted(recorded_drift_timer_states)
            != list(prior_binding.get("repaused_timer_units") or [])
            or not set(current_drift_timer_states).issubset(
                recorded_drift_timer_states
            )
        ):
            raise RuntimeError(
                "prepared maintenance pause-owned timer drift changed after binding"
            )
        for unit, state in current_drift_timer_states.items():
            if _unit_state_pair(state) != _unit_state_pair(
                recorded_drift_timer_states[unit]
            ):
                raise RuntimeError(
                    "prepared maintenance pause-owned timer state changed after binding: "
                    + unit
                )
        for unit, recorded_state in recorded_drift_timer_states.items():
            _validate_pause_owned_timer_trigger(
                unit,
                recorded=recorded_state,
                current=dict(timers.get(unit) or {}),
            )
    else:
        recorded_drift_timer_states = current_drift_timer_states
    writer_processes = [dict(row) for row in before.get("writer_processes") or []]
    recorded_service_generations = dict(
        prior_binding.get("deploy_drift_service_generations") or {}
    )
    service_generations, active_service_units = (
        _pause_owned_service_generation_evidence(
            dict(before.get("services") or {}),
            writer_processes,
            recorded=(recorded_service_generations if prior_binding else None),
        )
    )
    if active_service_units != (
        active_service_inventory & set(ALL_BUSINESS_SERVICE_UNITS)
    ):
        raise RuntimeError(
            "prepared maintenance pause-owned active service readback changed"
        )
    boundaries = _pause_owned_resume_boundary_readback(
        runtime_dir,
        status=before,
        active_service_units=active_service_units,
        recorded=(
            {
                "active_service_units": list(
                    prior_binding.get("draining_service_units") or []
                ),
                "locks": dict(
                    prior_binding.get("deploy_drift_writer_locks") or {}
                ),
                "sidecars": dict(
                    prior_binding.get("deploy_drift_sqlite_sidecars") or {}
                ),
            }
            if prior_binding
            else None
        ),
    )
    sidecars = dict(boundaries["sidecars"])
    sidecars_hot = bool(boundaries["sidecars_hot"])
    binding = {
        "schema_version": "business_data_pause_owned_inventory_resume_v2",
        "window_id": window_id,
        "plan_fingerprint": plan_fingerprint,
        "barrier_state_fingerprint": str(
            barrier.get("state_fingerprint") or ""
        ),
        "hold_started_at": str(existing.get("hold_started_at") or ""),
        "prepared_at": str(existing.get("prepared_at") or ""),
        "paused_policy_revision": current_revision,
        "paused_policy_fingerprint": current_policy_fingerprint,
        "baseline_control_fingerprint": str(
            persisted_signature.get("fingerprint") or ""
        ),
        "baseline_fingerprint": _stable_fingerprint(dict(baseline)),
        "baseline_captured_at": str(baseline.get("captured_at") or ""),
        "baseline_timer_source": "continuous_observer_timers",
        "baseline_timer_state": legacy_state,
        "prepare_readback_fingerprint": _stable_fingerprint(
            dict(persisted_readback)
        ),
        "timer_inventory": current_inventory,
        "deploy_drift_timer_states": recorded_drift_timer_states,
        "repaused_timer_units": sorted(recorded_drift_timer_states),
        "deploy_drift_service_generations": service_generations,
        "draining_service_units": sorted(
            unit
            for unit, value in service_generations.items()
            if str((value or {}).get("initial_is_active") or "")
            not in QUIESCENT_SERVICE_STATES
        ),
        "deploy_drift_writer_locks": (
            dict(prior_binding.get("deploy_drift_writer_locks") or {})
            if prior_binding
            else dict(boundaries["locks"])
        ),
        "deploy_drift_sqlite_sidecars": (
            dict(prior_binding.get("deploy_drift_sqlite_sidecars") or {})
            if prior_binding
            else sidecars
        ),
        "sqlite_operational_path": str(
            sidecars.get("operational_path") or ""
        ),
    }
    if prior_binding and prior_binding != binding:
        raise RuntimeError(
            "prepared maintenance FBS pause ownership binding drifted"
        )
    if not prior_binding:
        audit_event = _last_private_audit_event(runtime_dir / AUDIT_FILENAME)
        if (
            str(audit_event.get("event") or "") != "core_freeze_prepared"
            or dict(audit_event.get("status") or {})
            != dict(persisted_readback)
        ):
            raise RuntimeError(
                "prepared maintenance FBS pause ownership audit prestate drifted"
            )
        existing["pause_owned_inventory_resume_binding"] = binding
        _save_json_0600(runtime_dir / STATE_FILENAME, existing)
        _append_audit_0600(
            runtime_dir / AUDIT_FILENAME,
            {
                "event": "pause_owned_inventory_resume_bound",
                "captured_at": _utc_now(),
                "binding": binding,
            },
        )
    transition_applied = False
    for unit in ALL_BUSINESS_TIMER_UNITS:
        if unit in current_drift_timer_states:
            systemd.disable_now(unit)
            transition_applied = True
    return {
        "binding": binding,
        "transition_applied": transition_applied,
        "resume_pending": bool(
            transition_applied
            or active_service_units
            or writer_processes
            or sidecars_hot
        ),
    }


def _resume_prepared_nonquiet(
    runtime_dir: Path,
    *,
    existing: dict[str, Any],
    before: Mapping[str, Any],
    baseline: Mapping[str, Any],
    persisted_readback: Mapping[str, Any],
    persisted_signature: Mapping[str, Any],
    current_revision: int,
    current_policy_fingerprint: str,
    systemd: SystemdClient,
    window_id: str,
    plan_fingerprint: str,
) -> dict[str, Any]:
    if not window_id or not plan_fingerprint:
        raise RuntimeError(
            "prepared maintenance continuation requires exact barrier window "
            "and plan fingerprint"
        )
    barrier = barrier_status(runtime_dir)
    if (
        barrier.get("active") is not True
        or str(barrier.get("phase") or "") != "acquiring"
        or barrier.get("hold_confirmed") is not False
        or str(barrier.get("window_id") or "") != window_id
        or str(barrier.get("plan_fingerprint") or "")
        != plan_fingerprint
    ):
        raise RuntimeError(
            "prepared maintenance continuation barrier identity drifted"
        )
    if (
        str(existing.get("schema_version") or "") != SCHEMA_VERSION
        or str(baseline.get("schema_version") or "") != SCHEMA_VERSION
        or str(persisted_readback.get("schema_version") or "")
        != SCHEMA_VERSION
    ):
        raise RuntimeError(
            "prepared maintenance continuation schema drifted"
        )
    try:
        barrier_started = _parse_utc_instant(
            barrier.get("started_at"), label="barrier started_at"
        )
        hold_started = _parse_utc_instant(
            existing.get("hold_started_at"),
            label="maintenance hold_started_at",
        )
        prepared_at = _parse_utc_instant(
            existing.get("prepared_at"),
            label="maintenance prepared_at",
        )
    except RuntimeError as exc:
        raise RuntimeError(
            "prepared maintenance continuation timestamps are invalid"
        ) from exc
    if not barrier_started <= hold_started <= prepared_at:
        raise RuntimeError(
            "prepared maintenance continuation predates its barrier"
        )
    baseline_inventory = list(
        baseline.get("discovered_wb_core_timers") or []
    )
    current_inventory = list(
        before.get("discovered_wb_core_timers") or []
    )
    prepared_inventory = list(
        persisted_readback.get("discovered_wb_core_timers") or []
    )
    if (
        baseline_inventory != current_inventory
        or prepared_inventory != current_inventory
        or current_inventory != sorted(CLASSIFIED_WB_CORE_TIMER_UNITS)
    ):
        raise RuntimeError(
            "prepared maintenance continuation timer inventory drifted"
        )
    if before.get("unknown_wb_core_timers") or before.get("cron_entries"):
        raise RuntimeError(
            "prepared maintenance continuation found unclassified execution"
        )
    current_auto_updates = dict(before.get("auto_updates") or {})
    if set(current_auto_updates.get("drift_processes") or []) - {
        "warehouse_functional"
    }:
        raise RuntimeError(
            "prepared maintenance continuation owner policy drifted"
        )
    timer_states = dict(before.get("timers") or {})
    for unit in ALL_BUSINESS_TIMER_UNITS:
        state = dict(timer_states.get(unit) or {})
        if unit == "wb-core-warehouse-functional-sync.timer":
            expected = dict((baseline.get("timers") or {}).get(unit) or {})
            allowed_pairs = {_unit_state_pair(expected)}
            if existing.get("prepared_resume_binding"):
                allowed_pairs.add(("disabled", "inactive"))
            if _unit_state_pair(state) not in allowed_pairs:
                raise RuntimeError(
                    "prepared maintenance continuation warehouse timer drifted"
                )
            continue
        if (
            str(state.get("is_enabled") or "") != "disabled"
            or str(state.get("is_active") or "") != "inactive"
        ):
            raise RuntimeError(
                f"prepared maintenance continuation timer is not paused: {unit}"
            )
    services = dict(before.get("services") or {})
    for unit, state_raw in services.items():
        if unit == FBS_SHADOW_SERVICE_UNIT:
            continue
        if str((state_raw or {}).get("is_active") or "") not in (
            QUIESCENT_SERVICE_STATES
        ):
            raise RuntimeError(
                f"prepared maintenance continuation service is active: {unit}"
            )
    fbs_service = dict(services.get(FBS_SHADOW_SERVICE_UNIT) or {})
    fbs_service_active = (
        str(fbs_service.get("is_active") or "")
        not in QUIESCENT_SERVICE_STATES
        or int((fbs_service.get("properties") or {}).get("MainPID") or 0) != 0
    )
    fbs_pid = int((fbs_service.get("properties") or {}).get("MainPID") or 0)
    runtime = dict(before.get("runtime_schedules") or {})
    writer_processes = [dict(row) for row in before.get("writer_processes") or []]
    unexpected_writers = [
        row
        for row in writer_processes
        if str(row.get("marker") or "") != FBS_SHADOW_PROCESS_MARKER
        or (fbs_pid > 0 and int(row.get("pid") or 0) != fbs_pid)
    ]
    if (
        bool((runtime.get("web_vitrina") or {}).get("active"))
        or list(
            (runtime.get("feedback_complaints") or {}).get("active_runs")
            or []
        )
        or (runtime.get("spp") or {}).get("active_job") is not None
        or unexpected_writers
    ):
        raise RuntimeError(
            "prepared maintenance continuation runtime is not drained"
        )
    locks = dict(before.get("writer_locks") or {})
    if any(
        bool((value or {}).get("held"))
        for key, value in locks.items()
        if key not in {"seller_portal", "warehouse_functional"}
    ) or bool((locks.get("seller_portal") or {}).get("busy")):
        raise RuntimeError(
            "prepared maintenance continuation lock is active"
        )
    warehouse_lock_held = bool(
        (locks.get("warehouse_functional") or {}).get("held")
    )
    if warehouse_lock_held and not fbs_service_active:
        raise RuntimeError(
            "prepared maintenance continuation lock is active with unknown holder"
        )
    sidecars = _sqlite_sidecar_readback(runtime_dir)
    sidecars_hot = any(
        bool(item.get("exists"))
        for item in dict(sidecars.get("sidecars") or {}).values()
    )
    if sidecars_hot and not fbs_service_active:
        raise RuntimeError(
            "prepared maintenance continuation has a hot SQLite sidecar"
        )
    audit_event = _last_private_audit_event(runtime_dir / AUDIT_FILENAME)
    prior_binding = dict(existing.get("prepared_resume_binding") or {})
    pause_owned_binding = dict(
        existing.get("pause_owned_inventory_resume_binding") or {}
    )
    if prior_binding:
        if (
            str(audit_event.get("event") or "")
            != "prepared_resume_bound"
            or dict(audit_event.get("binding") or {}) != prior_binding
        ):
            raise RuntimeError(
                "prepared maintenance continuation audit binding drifted"
            )
    elif pause_owned_binding:
        if (
            str(audit_event.get("event") or "")
            != "pause_owned_inventory_resume_bound"
            or dict(audit_event.get("binding") or {})
            != pause_owned_binding
        ):
            raise RuntimeError(
                "prepared maintenance continuation pause-owned audit drifted"
            )
    elif (
        str(audit_event.get("event") or "") != "core_freeze_prepared"
        or dict(audit_event.get("status") or {})
        != dict(persisted_readback)
    ):
        raise RuntimeError(
            "prepared maintenance continuation audit prestate drifted"
        )
    binding = {
        "schema_version": "business_data_prepared_resume_v1",
        "window_id": window_id,
        "plan_fingerprint": plan_fingerprint,
        "barrier_state_fingerprint": str(
            barrier.get("state_fingerprint") or ""
        ),
        "hold_started_at": str(existing.get("hold_started_at") or ""),
        "prepared_at": str(existing.get("prepared_at") or ""),
        "paused_policy_revision": current_revision,
        "paused_policy_fingerprint": current_policy_fingerprint,
        "baseline_control_fingerprint": str(
            persisted_signature.get("fingerprint") or ""
        ),
        "prepare_readback_fingerprint": _stable_fingerprint(
            dict(persisted_readback)
        ),
        "timer_inventory": current_inventory,
        "sqlite_operational_path": str(
            sidecars.get("operational_path") or ""
        ),
    }
    if prior_binding and prior_binding != binding:
        raise RuntimeError(
            "prepared maintenance continuation identity drifted"
        )
    if not prior_binding:
        existing["prepared_resume_binding"] = binding
        _save_json_0600(runtime_dir / STATE_FILENAME, existing)
        _append_audit_0600(
            runtime_dir / AUDIT_FILENAME,
            {
                "event": "prepared_resume_bound",
                "captured_at": _utc_now(),
                "binding": binding,
            },
        )
    return {
        **dict(before),
        "status": "prepared",
        "idempotent": True,
        "reused_phase": "prepared",
        "resume_pending": True,
        "prepared_resume_binding": binding,
        "active_fbs_writer_drain": bool(
            fbs_service_active
            or writer_processes
            or warehouse_lock_held
            or sidecars_hot
        ),
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
    window_id: str = "",
    plan_fingerprint: str = "",
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
    active_phase = str((existing or {}).get("phase") or "")
    persisted_readback = dict(
        (
            (existing or {}).get("hold_readback")
            if active_phase == "held"
            else (existing or {}).get("prepare_readback")
        )
        or {}
    )
    bind_prepared_before_schedules = bool(
        active_phase == "prepared"
        and window_id
        and plan_fingerprint
        and not (existing or {}).get("prepared_resume_binding")
    )
    if bind_prepared_before_schedules:
        persisted_runtime = dict(
            persisted_readback.get("runtime_schedules") or {}
        )
        if set(persisted_runtime) != {
            "feedback_complaints",
            "spp",
            "web_vitrina",
        }:
            raise RuntimeError(
                "prepared maintenance runtime schedule readback drifted"
            )
        before_payloads: dict[str, dict[str, Any]] = {}
        before = maintenance_status(
            runtime_dir,
            systemd=systemd,
            schedules=schedules,
            proc_root=proc_root,
            runtime_schedule_readback=persisted_runtime,
        )
    else:
        before_payloads = schedules.read_all()
        before = maintenance_status(
            runtime_dir,
            systemd=systemd,
            schedules=schedules,
            proc_root=proc_root,
        )
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
    if existing is not None and str(existing.get("phase") or "") in {
        "prepared",
        "held",
    }:
        current_auto_updates = dict(before.get("auto_updates") or {})
        if (
            not owner_policy_existed
            or current_auto_updates.get("master_desired") is not False
            or current_auto_updates.get("unknown_processes")
        ):
            raise RuntimeError(
                "active maintenance hold owner policy drifted and cannot be reused"
            )
        current_revision = int(current_auto_updates.get("revision") or 0)
        if expected_revision is not None and current_revision != int(
            expected_revision
        ):
            raise RuntimeError(
                f"stale policy revision: expected {expected_revision}, "
                f"current {current_revision}"
            )
        persisted_auto_updates = dict(
            persisted_readback.get("auto_updates") or {}
        )
        if (
            current_revision <= 0
            or current_revision
            != int(persisted_auto_updates.get("revision") or 0)
            or str(current_auto_updates.get("policy_fingerprint") or "")
            != str(
                persisted_auto_updates.get("policy_fingerprint") or ""
            )
        ):
            raise RuntimeError(
                "active maintenance hold paused-policy identity drifted"
            )
        baseline = dict(existing.get("baseline") or {})
        persisted_signature = dict(
            existing.get("control_signature_before_hold") or {}
        )
        recomputed_signature = maintenance_control_signature(
            baseline,
            runtime_dir=runtime_dir,
        )
        if (
            not str(persisted_signature.get("fingerprint") or "")
            or persisted_signature != recomputed_signature
        ):
            raise RuntimeError(
                "active maintenance hold baseline signature drifted"
            )
        pause_owned_resume = _resume_legacy_fbs_pause_ownership(
            runtime_dir,
            existing=existing,
            before=before,
            baseline=baseline,
            persisted_readback=persisted_readback,
            persisted_signature=persisted_signature,
            current_revision=current_revision,
            current_policy_fingerprint=str(
                current_auto_updates.get("policy_fingerprint") or ""
            ),
            systemd=systemd,
            window_id=window_id,
            plan_fingerprint=plan_fingerprint,
        )
        if pause_owned_resume and (
            bind_prepared_before_schedules
            or pause_owned_resume["transition_applied"]
            or pause_owned_resume["resume_pending"]
        ):
            return {
                **before,
                "status": "prepared",
                "idempotent": True,
                "reused_phase": "prepared",
                "resume_pending": True,
                "pause_owned_inventory_resume_binding": (
                    pause_owned_resume["binding"]
                ),
            }
        if bind_prepared_before_schedules:
            return _resume_prepared_nonquiet(
                runtime_dir,
                existing=existing,
                before=before,
                baseline=baseline,
                persisted_readback=persisted_readback,
                persisted_signature=persisted_signature,
                current_revision=current_revision,
                current_policy_fingerprint=str(
                    current_auto_updates.get("policy_fingerprint") or ""
                ),
                systemd=systemd,
                window_id=window_id,
                plan_fingerprint=plan_fingerprint,
            )
        current_signature = maintenance_control_signature(
            before,
            runtime_dir=runtime_dir,
        )
        baseline_payload = dict(recomputed_signature.get("payload") or {})
        current_payload = dict(current_signature.get("payload") or {})
        intent_fields = (
            "process_desired",
            "timer_control_intent",
            "runtime_schedule_intent",
            "unknown_wb_core_timers",
            "cron_entries",
        )
        if any(
            current_payload.get(field) != baseline_payload.get(field)
            for field in intent_fields
        ):
            raise RuntimeError(
                "active maintenance hold control intent drifted"
            )
        if not before["quiet"]:
            if active_phase != "prepared":
                raise RuntimeError(
                    "active maintenance hold is no longer quiet and cannot be reused"
                )
            non_warehouse_timer_drift = [
                unit
                for unit in ALL_BUSINESS_TIMER_UNITS
                if unit != "wb-core-warehouse-functional-sync.timer"
                and (
                    str(
                        ((before.get("timers") or {}).get(unit) or {}).get(
                            "is_enabled"
                        )
                        or ""
                    )
                    != "disabled"
                    or str(
                        ((before.get("timers") or {}).get(unit) or {}).get(
                            "is_active"
                        )
                        or ""
                    )
                    != "inactive"
                )
            ]
            if non_warehouse_timer_drift:
                raise RuntimeError(
                    "active maintenance hold is no longer quiet and cannot be reused"
                )
            return _resume_prepared_nonquiet(
                runtime_dir,
                existing=existing,
                before=before,
                baseline=baseline,
                persisted_readback=persisted_readback,
                persisted_signature=persisted_signature,
                current_revision=current_revision,
                current_policy_fingerprint=str(
                    current_auto_updates.get("policy_fingerprint") or ""
                ),
                systemd=systemd,
                window_id=window_id,
                plan_fingerprint=plan_fingerprint,
            )
        if current_auto_updates.get("drift_processes"):
            raise RuntimeError(
                "active maintenance hold owner policy drifted and cannot be reused"
            )
        _append_audit_0600(
            audit_path,
            {
                "event": "prepare_reused",
                "captured_at": _utc_now(),
                "phase": active_phase,
                "paused_policy_revision": current_revision,
                "paused_policy_fingerprint": str(
                    current_auto_updates.get("policy_fingerprint") or ""
                ),
                "control_signature_before_hold": persisted_signature,
            },
        )
        return {
            **before,
            "status": "prepared",
            "idempotent": True,
            "reused_phase": active_phase,
        }
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
    for unit in CORE_TIMER_UNITS + INDEPENDENT_WRITER_TIMER_UNITS:
        systemd.disable_now(unit)
    schedules.disable_all(before_payloads)
    current = maintenance_status(runtime_dir, systemd=systemd, schedules=schedules, proc_root=proc_root)
    state.update({"phase": "prepared", "prepared_at": _utc_now(), "prepare_readback": current})
    _save_json_0600(state_path, state)
    _append_audit_0600(audit_path, {"event": "core_freeze_prepared", "captured_at": _utc_now(), "status": current})
    return {**current, "status": "prepared", "idempotent": already_quiet}


def _last_private_audit_event(path: Path) -> dict[str, Any]:
    """Read one bounded final audit row for an unstarted-hold proof."""

    if path.is_symlink() or not path.is_file():
        raise RuntimeError("business maintenance audit is unavailable")
    if path.stat().st_mode & 0o077:
        raise RuntimeError("business maintenance audit must be private mode 0600")
    maximum_tail_bytes = 8 * 1024 * 1024
    with path.open("rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        offset = max(0, size - maximum_tail_bytes)
        handle.seek(offset)
        payload = handle.read(maximum_tail_bytes)
    lines = [line for line in payload.splitlines() if line.strip()]
    if offset and len(lines) < 2:
        raise RuntimeError("business maintenance audit tail is not bounded")
    if not lines:
        raise RuntimeError("business maintenance audit is empty")
    try:
        event = json.loads(lines[-1].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("business maintenance audit tail is invalid") from exc
    if not isinstance(event, dict):
        raise RuntimeError("business maintenance audit tail is not an object")
    return event


def _parse_utc_instant(value: Any, *, label: str) -> datetime:
    normalized = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"{label} is not an exact UTC timestamp") from exc
    if parsed.tzinfo is None:
        raise RuntimeError(f"{label} must include timezone evidence")
    return parsed.astimezone(timezone.utc)


def maintenance_unstarted_hold_abort_readback(
    runtime_dir: Path,
    *,
    systemd: SystemdClient,
    schedules: RuntimeScheduleClient,
    proc_root: Path = Path("/proc"),
) -> dict[str, Any]:
    """Prove that an acquiring HTTP barrier preceded no maintenance hold."""

    runtime_dir = Path(runtime_dir).resolve()
    barrier = barrier_status(runtime_dir)
    if (
        barrier.get("active") is not True
        or str(barrier.get("phase") or "") != "acquiring"
        or barrier.get("hold_confirmed") is not False
    ):
        raise RuntimeError(
            "unstarted-hold abort requires an exact unconfirmed acquiring barrier"
        )
    barrier_started = _parse_utc_instant(
        barrier.get("started_at"),
        label="barrier started_at",
    )
    barrier_started_ns = int(barrier_started.timestamp() * 1_000_000_000)
    state_path = runtime_dir / STATE_FILENAME
    audit_path = runtime_dir / AUDIT_FILENAME
    maintenance_state = _load_json_object(state_path) or {}
    if (
        str(maintenance_state.get("phase") or "") != "restored"
        or maintenance_state.get("exact_prior_state_restored") is not True
    ):
        raise RuntimeError(
            "maintenance state does not prove a completed boundary predating "
            "this barrier"
        )

    def filesystem_evidence() -> dict[str, Any]:
        if state_path.is_symlink() or not state_path.is_file():
            raise RuntimeError("business maintenance state is unavailable")
        if state_path.stat().st_mode & 0o077:
            raise RuntimeError(
                "business maintenance state must be private mode 0600"
            )
        state_stat = state_path.stat()
        audit_stat = audit_path.stat()
        last_event = _last_private_audit_event(audit_path)
        last_event_at = _parse_utc_instant(
            last_event.get("captured_at"),
            label="last maintenance audit event",
        )
        if (
            state_stat.st_mtime_ns >= barrier_started_ns
            or audit_stat.st_mtime_ns >= barrier_started_ns
            or last_event_at >= barrier_started
        ):
            raise RuntimeError(
                "maintenance state or audit changed after barrier acquisition; "
                "an unstarted hold cannot be proven"
            )
        return {
            "maintenance_state_mtime_ns": int(state_stat.st_mtime_ns),
            "maintenance_audit_mtime_ns": int(audit_stat.st_mtime_ns),
            "last_maintenance_event": str(last_event.get("event") or ""),
            "last_maintenance_event_at": str(
                last_event.get("captured_at") or ""
            ),
        }

    before = filesystem_evidence()
    current = maintenance_status(
        runtime_dir,
        systemd=systemd,
        schedules=schedules,
        proc_root=proc_root,
    )
    if current.get("unknown_wb_core_timers"):
        raise RuntimeError(
            "unstarted-hold abort is blocked by unclassified wb-core timers"
        )
    if current.get("cron_entries"):
        raise RuntimeError(
            "unstarted-hold abort is blocked by repo-owned cron drift"
        )
    auto_updates = dict(current.get("auto_updates") or {})
    if auto_updates.get("unknown_processes") or auto_updates.get(
        "drift_processes"
    ):
        raise RuntimeError(
            "unstarted-hold abort is blocked by owner-policy drift"
        )
    after = filesystem_evidence()
    if before != after:
        raise RuntimeError(
            "maintenance evidence changed while proving an unstarted hold"
        )
    control = maintenance_control_signature(current, runtime_dir=runtime_dir)
    proof = {
        "boundary_kind": "no_maintenance_hold_started",
        "barrier_window_id": str(barrier.get("window_id") or ""),
        "barrier_plan_fingerprint": str(
            barrier.get("plan_fingerprint") or ""
        ),
        "barrier_started_at": str(barrier.get("started_at") or ""),
        **after,
        "current_control_signature": control["fingerprint"],
    }
    return {
        **current,
        "status": "restored",
        "exact_prior_state_restored": True,
        "control_signature": control["fingerprint"],
        "restore_boundary_kind": "no_maintenance_hold_started",
        "no_hold_proof": proof,
        "no_hold_proof_fingerprint": _stable_fingerprint(proof),
    }


def maintenance_barrier_abort_readback(
    runtime_dir: Path,
    *,
    systemd: SystemdClient,
    schedules: RuntimeScheduleClient,
    proc_root: Path = Path("/proc"),
) -> dict[str, Any]:
    """Select only a current-window restore or an exact unstarted proof."""

    runtime_dir = Path(runtime_dir).resolve()
    barrier = barrier_status(runtime_dir)
    if (
        barrier.get("active") is False
        and str(barrier.get("phase") or "") == "released"
    ):
        # abort_barrier_acquire itself still proves the exact window,
        # fingerprint and acquire_aborted release kind before returning its
        # idempotent no-op.
        return {
            "status": "restored",
            "exact_prior_state_restored": True,
            "restore_boundary_kind": "released_idempotency_probe",
        }
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
    try:
        return maintenance_unstarted_hold_abort_readback(
            runtime_dir,
            systemd=systemd,
            schedules=schedules,
            proc_root=proc_root,
        )
    except RuntimeError as unstarted_hold_error:
        # A real prepare/hold generation must use its persisted exact restore.
        # Stale restore evidence from an older maintenance window never falls
        # through merely because it also says phase=restored.
        try:
            barrier_started = _parse_utc_instant(
                barrier.get("started_at"),
                label="barrier started_at",
            )
            hold_started = _parse_utc_instant(
                maintenance_state.get("hold_started_at"),
                label="maintenance hold_started_at",
            )
            restored_at = _parse_utc_instant(
                maintenance_state.get("restored_at"),
                label="maintenance restored_at",
            )
            restore_captured_at = _parse_utc_instant(
                restore_readback.get("captured_at"),
                label="maintenance restore readback captured_at",
            )
        except RuntimeError as restore_identity_error:
            raise RuntimeError(
                "barrier abort has neither an unstarted-hold proof nor an "
                "exact restore belonging to this barrier"
            ) from restore_identity_error
        if (
            hold_started < barrier_started
            or restored_at < barrier_started
            or restore_captured_at < barrier_started
            or str(restore_readback.get("status") or "") != "restored"
            or restore_readback.get("exact_prior_state_restored") is not True
        ):
            raise RuntimeError(
                "barrier abort has neither an unstarted-hold proof nor an "
                "exact restore belonging to this barrier"
            ) from unstarted_hold_error
        return restore_readback


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
            "abort-prepared",
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
    parser.add_argument("--expected-deployed-sha", default="")
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
    if args.action == "barrier-release":
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
    if args.action == "abort-prepared":
        if (
            args.expected_revision is None
            or not args.window_id
            or not args.plan_fingerprint
            or not args.expected_deployed_sha
        ):
            raise RuntimeError(
                "abort-prepared requires exact revision, window, plan, "
                "and deployed SHA"
            )
        with _ExclusiveRestoreLock(runtime_dir):
            result = maintenance_abort_prepared(
                runtime_dir,
                systemd=systemd,
                schedules=schedules,
                expected_revision=int(args.expected_revision),
                window_id=str(args.window_id),
                plan_fingerprint=str(args.plan_fingerprint),
                expected_deployed_sha=str(args.expected_deployed_sha),
                deployed_sha_file=ROOT / ".wb-core-runtime-sha",
                actor=str(args.actor or "repo_owned_cli"),
                reason=str(args.reason or "abort exact prepared maintenance"),
                wait_timeout_seconds=float(args.wait_timeout_seconds),
                poll_interval_seconds=float(args.poll_interval_seconds),
            )
    elif args.action == "barrier-abort":
        with _ExclusiveRestoreLock(runtime_dir):
            restore_readback = maintenance_barrier_abort_readback(
                runtime_dir,
                systemd=systemd,
                schedules=schedules,
            )
            result = abort_barrier_acquire(
                runtime_dir,
                window_id=args.window_id,
                plan_fingerprint=args.plan_fingerprint,
                actor=args.actor,
                reason=args.reason,
                restore_readback=restore_readback,
            )
    elif args.action == "restore-continuity-status":
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
            "service_continuity": _restore_service_continuity(
                runtime_dir,
                maintenance_state=maintenance_state,
                current_status=status,
            ),
        }
    elif args.action == "status":
        result = maintenance_status(runtime_dir, systemd=systemd, schedules=schedules)
    elif args.action == "prepare":
        with _ExclusiveRestoreLock(runtime_dir):
            result = maintenance_prepare(
                runtime_dir,
                systemd=systemd,
                schedules=schedules,
                actor=args.actor,
                reason=args.reason or "canonical cross-writer hold",
                expected_revision=args.expected_revision,
                window_id=args.window_id,
                plan_fingerprint=args.plan_fingerprint,
            )
    elif args.action == "hold":
        with _ExclusiveRestoreLock(runtime_dir):
            result = maintenance_hold(
                runtime_dir,
                systemd=systemd,
                schedules=schedules,
                wait_timeout_seconds=args.wait_timeout_seconds,
                poll_interval_seconds=args.poll_interval_seconds,
                actor=args.actor,
                reason=args.reason or "canonical cross-writer hold",
                expected_revision=args.expected_revision,
                window_id=args.window_id,
                plan_fingerprint=args.plan_fingerprint,
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
