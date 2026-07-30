"""Server-owned lifecycle reconciliation for WB Autoanswers.

Business intent stays in ``sheet_vitrina_v1_wb_autoanswers_settings``.  This
module owns only runtime reconciliation and its durable readback/error state.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Iterator, Mapping, Protocol

from packages.application.wb_autoanswers_runtime import (
    AutoanswersRepository,
    iso_utc,
    parse_timestamp,
)


LIFECYCLE_CONTRACT = "wb_autoanswers_lifecycle_v1"
LIFECYCLE_STATE_FILENAME = ".wb-autoanswers-lifecycle.json"
LIFECYCLE_LOCK_FILENAME = ".wb-autoanswers-lifecycle.lock"
READONLY_TIMER = "wb-core-autoanswers-readonly-sync.timer"
WORKER_TIMER = "wb-core-autoanswers-worker.timer"
READONLY_SERVICE = "wb-core-autoanswers-readonly-sync.service"
WORKER_SERVICE = "wb-core-autoanswers-worker.service"
WORKER_MODES = frozenset({"manual", "draft_only", "auto_safe", "auto_all"})
BLOCKING_STOP_REASONS = frozenset(
    {
        "budget_state_unknown",
        "openai_quota_exhausted",
        "run_cap_missing",
        "worker_unavailable",
        "worker_error",
    }
)


class SystemdPort(Protocol):
    def unit_state(self, unit: str) -> dict[str, Any]: ...

    def disable_now(self, unit: str) -> None: ...

    def enable_now(self, unit: str) -> None: ...


class SystemdClient:
    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
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
                "--property=LoadState,UnitFileState,ActiveState,SubState,Result,"
                "ExecMainCode,ExecMainStatus,LastTriggerUSec,NextElapseUSecRealtime,"
                "ActiveEnterTimestamp,InactiveEnterTimestamp",
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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _save_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=path.name + ".",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("Autoanswers lifecycle state is not a JSON object")
    return value


@contextmanager
def _lifecycle_lock(runtime_dir: Path) -> Iterator[None]:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    path = runtime_dir / LIFECYCLE_LOCK_FILENAME
    with path.open("a+b") as handle:
        os.chmod(path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _timer_actual(state: Mapping[str, Any]) -> bool:
    return (
        str(state.get("is_enabled") or "") == "enabled"
        and str(state.get("is_active") or "") == "active"
    )


def _component(
    *,
    component_key: str,
    desired: bool,
    timer: Mapping[str, Any],
    service: Mapping[str, Any],
) -> dict[str, Any]:
    actual = _timer_actual(timer)
    properties = dict(timer.get("properties") or {})
    service_properties = dict(service.get("properties") or {})
    service_result = str(service_properties.get("Result") or "success")
    last_error = "" if service_result == "success" else service_result
    return {
        "component_key": component_key,
        "desired": bool(desired),
        "actual": bool(actual),
        "timer": dict(timer),
        "service": dict(service),
        "last_run": str(properties.get("LastTriggerUSec") or ""),
        "last_success": (
            str(properties.get("LastTriggerUSec") or "")
            if service_result == "success"
            else ""
        ),
        "next_run": str(properties.get("NextElapseUSecRealtime") or ""),
        "last_error": last_error,
        "drift_status": "matched" if bool(actual) == bool(desired) else "drift",
    }


class AutoanswersLifecycle:
    def __init__(
        self,
        *,
        runtime_dir: Path,
        repository: AutoanswersRepository,
        systemd: SystemdPort | None = None,
        now_factory: Any = _utc_now,
    ) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.repository = repository
        self.systemd = systemd or SystemdClient()
        self.now_factory = now_factory
        self.state_path = self.runtime_dir / LIFECYCLE_STATE_FILENAME

    def _now(self) -> datetime:
        value = self.now_factory()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def desired_mode(self) -> str:
        settings = self.repository.settings()
        return settings.mode if settings.master_enabled else "off"

    def _read_components(
        self,
        *,
        desired_mode: str,
        suspended_by_master: bool,
    ) -> dict[str, dict[str, Any]]:
        readonly_desired = not suspended_by_master
        worker_desired = not suspended_by_master and desired_mode in WORKER_MODES
        return {
            "readonly_sync": _component(
                component_key="readonly_sync",
                desired=readonly_desired,
                timer=self.systemd.unit_state(READONLY_TIMER),
                service=self.systemd.unit_state(READONLY_SERVICE),
            ),
            "worker": _component(
                component_key="worker",
                desired=worker_desired,
                timer=self.systemd.unit_state(WORKER_TIMER),
                service=self.systemd.unit_state(WORKER_SERVICE),
            ),
        }

    def status(self, *, suspended_by_master: bool) -> dict[str, Any]:
        now = self._now()
        settings = self.repository.settings()
        desired_mode = settings.mode if settings.master_enabled else "off"
        persisted = _load_json(self.state_path)
        components = self._read_components(
            desired_mode=desired_mode,
            suspended_by_master=suspended_by_master,
        )
        component_drift = [
            key
            for key, item in components.items()
            if str(item.get("drift_status") or "") != "matched"
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
        operational = self.repository.operational_status()
        budget = self.repository.budget_status()
        reconciliation = self.repository.reconciliation_status() or {}
        progress = dict(operational.get("progress") or {})
        transition_run_id = str(
            reconciliation.get("transition_run_id")
            or progress.get("transition_run_id")
            or ""
        )
        requested_at = parse_timestamp(persisted.get("requested_at"))
        last_tick = parse_timestamp(progress.get("last_scheduler_tick_at"))
        fresh_tick = bool(
            desired_mode in WORKER_MODES
            and last_tick is not None
            and last_tick >= now - timedelta(minutes=3)
            and (requested_at is None or last_tick >= requested_at)
        )
        persisted_matches = bool(
            persisted
            and str(persisted.get("requested_mode") or "") == desired_mode
            and int(
                persisted.get("policy_epoch")
                if persisted.get("policy_epoch") is not None
                else -1
            )
            == int(settings.policy_epoch)
            and bool(persisted.get("suspended_by_master")) == bool(suspended_by_master)
            and (
                desired_mode not in {"draft_only", "auto_safe", "auto_all"}
                or str(persisted.get("transition_run_id") or "")
                == transition_run_id
            )
        )
        stop_reason = str(progress.get("stop_reason") or "")
        if (
            desired_mode in {"draft_only", "auto_safe", "auto_all"}
            and (
                not transition_run_id
                or (
                    reconciliation.get("run_max_usd") in {None, ""}
                    and reconciliation.get("run_max_paid_reviews") in {None, ""}
                )
            )
        ):
            stop_reason = "run_cap_missing"
        elif str(budget.get("budget_state") or "") == "unknown":
            stop_reason = "budget_state_unknown"
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
            # A newly enabled timer has one scheduler interval to produce its
            # first post-request tick.  An in-progress worker also owns the
            # right to replace a stale pre-request worker_error marker.
            # Outside those bounded facts the same markers remain blocking.
            stop_reason = ""
        last_error = str(persisted.get("last_error") or "")
        if suspended_by_master:
            lifecycle_state = (
                "suspended_by_master" if not component_drift else "error"
            )
        elif component_drift:
            lifecycle_state = "error" if persisted_matches else "unconfirmed"
        elif not persisted_matches:
            lifecycle_state = "unconfirmed"
        elif desired_mode == "off":
            lifecycle_state = "off"
        elif stop_reason in BLOCKING_STOP_REASONS:
            lifecycle_state = "error"
        elif fresh_tick:
            lifecycle_state = "running"
        else:
            lifecycle_state = "starting"
        if lifecycle_state == "error" and not last_error:
            last_error = stop_reason or ("component drift: " + ",".join(component_drift))
        actual = bool(
            not suspended_by_master
            and desired_mode in WORKER_MODES
            and not component_drift
            and persisted_matches
            and fresh_tick
            and stop_reason not in BLOCKING_STOP_REASONS
        )
        drift_status = (
            "drift"
            if component_drift
            else "unknown"
            if not persisted_matches
            else "matched"
            if suspended_by_master
            else "blocked"
            if stop_reason in BLOCKING_STOP_REASONS
            else "matched"
        )
        return {
            "contract": LIFECYCLE_CONTRACT,
            "process_key": "autoanswers",
            "display_name": "Autoanswers",
            "control_owner": "feature",
            "control_location": "Отзывы → Отзывы",
            "control_capability": "monitor",
            "desired_source": "autoanswers_feature_settings",
            "desired": desired_mode != "off",
            "business_mode": desired_mode,
            "actual": actual,
            "lifecycle_state": lifecycle_state,
            "last_run": components["worker"]["last_run"]
            or components["readonly_sync"]["last_run"],
            "last_success": components["worker"]["last_success"]
            or components["readonly_sync"]["last_success"],
            "next_run": components["worker"]["next_run"]
            or components["readonly_sync"]["next_run"],
            "last_error": last_error,
            "runtime_schedule": {
                "policy_epoch": int(settings.policy_epoch),
                "transition_run_id": transition_run_id or None,
                "last_scheduler_tick_at": progress.get("last_scheduler_tick_at"),
            },
            "drift_status": drift_status,
            "suspended_by_master": bool(suspended_by_master),
            "components": components,
            "component_states": components,
            "stop_reason": stop_reason,
            "budget_state": str(budget.get("budget_state") or "unknown"),
            "budget": budget,
            "fresh_scheduler_tick": fresh_tick,
            "service_in_progress": service_in_progress,
            "requested_at": persisted.get("requested_at"),
            "policy_epoch": int(settings.policy_epoch),
            "transition_run_id": transition_run_id or None,
            "readback_captured_at": iso_utc(now),
        }

    def reconcile(
        self,
        *,
        suspended_by_master: bool,
        actor: str,
        reason: str,
        transition_run_id: str | None = None,
    ) -> dict[str, Any]:
        with _lifecycle_lock(self.runtime_dir):
            now = self._now()
            settings = self.repository.settings()
            desired_mode = settings.mode if settings.master_enabled else "off"
            state = {
                "contract": LIFECYCLE_CONTRACT,
                "requested_mode": desired_mode,
                "policy_epoch": int(settings.policy_epoch),
                "transition_run_id": transition_run_id,
                "suspended_by_master": bool(suspended_by_master),
                "requested_at": iso_utc(now),
                "actor": str(actor or "unknown"),
                "reason": str(reason or "feature lifecycle reconciliation"),
                "status": "reconciling",
                "last_error": "",
            }
            _save_json(self.state_path, state)
            try:
                # Stop write-capable execution first.  Read-only sync is the
                # first component started and the last component stopped.
                if suspended_by_master:
                    self.systemd.disable_now(WORKER_TIMER)
                    self.systemd.disable_now(READONLY_TIMER)
                else:
                    # Worker hard gates must pass before its timer is enabled.
                    # Readback below verifies identity and actual unit state,
                    # but must not be the first line of defence.
                    self.systemd.disable_now(WORKER_TIMER)
                    self.systemd.enable_now(READONLY_TIMER)
                    if desired_mode in WORKER_MODES:
                        reconciliation = self.repository.reconciliation_status() or {}
                        budget = self.repository.budget_status()
                        progress = dict(
                            self.repository.operational_status().get("progress")
                            or {}
                        )
                        if desired_mode in {"draft_only", "auto_safe", "auto_all"}:
                            persisted_run_id = str(
                                reconciliation.get("transition_run_id") or ""
                            )
                            if (
                                not persisted_run_id
                                or not transition_run_id
                                or str(transition_run_id) != persisted_run_id
                                or (
                                    reconciliation.get("run_max_usd") in {None, ""}
                                    and reconciliation.get("run_max_paid_reviews")
                                    in {None, ""}
                                )
                            ):
                                raise RuntimeError(
                                    "Autoanswers lifecycle is blocked: run_cap_missing"
                                )
                        if (
                            str(budget.get("budget_state") or "") == "unknown"
                            or str(progress.get("stop_reason") or "")
                            == "budget_state_unknown"
                        ):
                            raise RuntimeError(
                                "Autoanswers lifecycle is blocked: "
                                "budget_state_unknown"
                            )
                        self.systemd.enable_now(WORKER_TIMER)
                readback = self.status(
                    suspended_by_master=suspended_by_master,
                )
                if readback["drift_status"] in {"drift", "unknown"}:
                    mismatch = [
                        key
                        for key, value in readback["components"].items()
                        if value.get("drift_status") != "matched"
                    ]
                    raise RuntimeError(
                        "Autoanswers lifecycle readback is not confirmed: "
                        + (",".join(mismatch) or "persisted lifecycle identity")
                    )
                if readback["drift_status"] == "blocked":
                    self.systemd.disable_now(WORKER_TIMER)
                    raise RuntimeError(
                        "Autoanswers lifecycle is blocked: "
                        + str(readback.get("stop_reason") or "unknown")
                    )
                state.update(
                    {
                        "status": str(readback["lifecycle_state"]),
                        "readback": readback,
                        "confirmed_at": iso_utc(self._now()),
                    }
                )
                _save_json(self.state_path, state)
                return self.status(suspended_by_master=suspended_by_master)
            except Exception as exc:
                # Persisted business intent is retained, but a partially
                # reconciled write-capable worker always fails closed.
                try:
                    self.systemd.disable_now(WORKER_TIMER)
                    if suspended_by_master:
                        self.systemd.disable_now(READONLY_TIMER)
                    else:
                        self.systemd.enable_now(READONLY_TIMER)
                except Exception:
                    pass
                state.update(
                    {
                        "status": "error",
                        "last_error": str(exc),
                        "failed_at": iso_utc(self._now()),
                    }
                )
                _save_json(self.state_path, state)
                raise
