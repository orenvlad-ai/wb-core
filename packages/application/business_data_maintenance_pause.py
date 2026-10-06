"""Explicit indefinite maintenance over existing timers and the write barrier.

This is an execution boundary, not an owner-policy or schedule editor.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Callable

from apps.business_data_maintenance import (
    ALL_BUSINESS_TIMER_UNITS, CONTINUOUS_OBSERVER_TIMER_UNITS, TIMER_ROLES,
    CONTINUOUS_INFRASTRUCTURE_SERVICE_UNITS, POLICY_FILENAME, STATE_FILENAME as LEGACY_STATE_FILENAME,
    _ExclusiveRestoreLock, _cron_entries, _writer_processes,
)
from packages.application.business_data_procedure_admission import admission_idle
from packages.application.business_data_write_barrier import (
    _atomic_write_private_json, _append_private_audit, acquire_barrier,
    barrier_status, confirm_barrier_hold, mark_barrier_restoring, release_barrier,
    _validate_identifier, _validate_actor,
)

SCHEMA = "business_data_maintenance_pause_v1"
STATE_FILENAME = ".business-data-maintenance-pause.json"
AUDIT_FILENAME = ".business-data-maintenance-pause-audit.jsonl"
ACTIVITY_PATH = "/v1/business-data-maintenance/activity"
SAFETY_TIMER = "wb-core-root-storage-policy.timer"
TIMERS = tuple(unit for unit, role in TIMER_ROLES.items() if role != "safety_monitor")
PERSISTENT_SERVICES = CONTINUOUS_INFRASTRUCTURE_SERVICE_UNITS + ("wb-core-finance-liquidity-pilot.service",)
PHASES = {"prepared", "draining", "held", "restoring", "restored"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def load_state(runtime_dir: Path) -> dict | None:
    path = runtime_dir / STATE_FILENAME
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise RuntimeError("maintenance pause state is not a private regular file")
    if path.stat().st_size > 2 * 1024 * 1024:
        raise RuntimeError("maintenance pause state exceeds bound")
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA or value.get("phase") not in PHASES:
        raise RuntimeError("maintenance pause state is invalid")
    if value.get("baseline_fingerprint") != fingerprint(value.get("baseline")):
        raise RuntimeError("maintenance pause baseline fingerprint mismatch")
    return value


def save_state(runtime_dir: Path, state: dict, event: str) -> None:
    state["updated_at"] = now_iso()
    _atomic_write_private_json(runtime_dir / STATE_FILENAME, state)
    _append_private_audit(runtime_dir / AUDIT_FILENAME, {
        "event": event, "captured_at": state["updated_at"],
        "window_id": state["window_id"], "phase": state["phase"],
        "error": state.get("error", ""),
    })


def _controls(runtime_dir: Path, activity: dict) -> dict:
    path = runtime_dir / POLICY_FILENAME
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_size > 1024 * 1024)):
        raise RuntimeError("owner policy cannot be safely fingerprinted")
    return {
        "owner_policy": hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None,
        "feature_intent": activity.get("feature_intent"),
    }


def _activity(reader: Callable[[], dict], runtime_dir: Path) -> dict:
    value = reader()
    if (not isinstance(value, dict) or value.get("contract_name") != "business_data_maintenance_activity_v1"
            or value.get("admission_ready") is not True or value.get("complete") is not True
            or value.get("runtime_dir") != str(runtime_dir.resolve())
            or not isinstance(value.get("jobs"), list) or not isinstance(value.get("feature_intent"), dict)):
        raise RuntimeError("authenticated HTTP writer/readiness coverage is unproven")
    return value


def _unit_fingerprint(value: dict) -> str:
    properties = value.get("properties") or {}
    # Configured unit fragments avoid time-varying computed timer deadlines.
    if properties.get("UnitContentDigest"):
        return properties["UnitContentDigest"]
    return fingerprint({key: properties.get(key) for key in (
        "FragmentPath", "DropInPaths", "ExecStart", "Triggers", "Persistent", "UnitFileState",
        "TimersCalendar", "TimersMonotonic", "AccuracyUSec", "RandomizedDelayUSec", "RemainAfterElapse"
    ) if key != "UnitFileState"})


def _units(systemd) -> dict:
    units = {}
    for timer in TIMERS:
        for unit in (timer, timer.removesuffix(".timer") + ".service"):
            state = systemd.unit_state(unit)
            if (state.get("properties") or {}).get("LoadState") != "loaded":
                raise RuntimeError("maintenance unit unavailable: " + unit)
            units[unit] = state
    return units


def _discover(systemd) -> None:
    unknown = sorted(set(systemd.discovered_timers()) - set(ALL_BUSINESS_TIMER_UNITS + CONTINUOUS_OBSERVER_TIMER_UNITS))
    if unknown:
        raise RuntimeError("unclassified timers: " + ",".join(unknown))
    allowed = set(PERSISTENT_SERVICES) | {unit.removesuffix(".timer") + ".service" for unit in TIMERS} | {SAFETY_TIMER.removesuffix(".timer") + ".service"}
    unknown_services = []
    for unit in systemd.discovered_active_services():
        if unit in allowed:
            continue
        value = systemd.unit_state(unit)
        properties = value.get("properties") or {}
        # A finished transient oneshot (active/exited, PID0) is not a live writer.
        if int(properties.get("MainPID") or 0) or properties.get("SubState") not in {"exited", "dead", "failed"}:
            unknown_services.append(unit)
    if unknown_services:
        raise RuntimeError("unclassified live services: " + ",".join(sorted(unknown_services)))
    if _cron_entries():
        raise RuntimeError("unclassified business cron entries")


def preflight(runtime_dir: Path, *, systemd, activity_reader: Callable[[], dict]) -> dict:
    """Complete readonly validation, before state/barrier/timer mutation."""
    runtime_dir = runtime_dir.resolve()
    existing = load_state(runtime_dir)
    if existing and existing["phase"] != "restored":
        raise RuntimeError("existing pause requires the same identity resume/status")
    if barrier_status(runtime_dir)["active"]:
        raise RuntimeError("another maintenance write barrier is active")
    legacy = runtime_dir / LEGACY_STATE_FILENAME
    if legacy.exists():
        if json.loads(legacy.read_text()).get("phase") not in {"restored", "released"}:
            raise RuntimeError("legacy maintenance is active; explicit recovery required")
    _discover(systemd)
    activity = _activity(activity_reader, runtime_dir)
    admission = admission_idle(runtime_dir)
    if not admission["ready"]:
        raise RuntimeError("procedure admission infrastructure is not ready")
    units = _units(systemd)
    for unit, value in units.items():
        if unit.endswith(".timer") and (value.get("is_enabled") not in {"enabled", "disabled"}
                or value.get("is_active") not in {"active", "inactive"}):
            raise RuntimeError("unsupported exact timer baseline: " + unit)
    baseline = {"units": units, "controls": _controls(runtime_dir, activity)}
    return {"status": "preflight_ready", "baseline": baseline,
            "baseline_fingerprint": fingerprint(baseline), "activity": activity,
            "admission": admission, "captured_at": now_iso()}


def readback(runtime_dir: Path, *, systemd, activity_reader: Callable[[], dict], proc_root: Path = Path("/proc")) -> dict:
    _discover(systemd)
    units = _units(systemd)
    activity = _activity(activity_reader, runtime_dir)
    admission = admission_idle(runtime_dir)
    processes = _writer_processes(proc_root)
    live_services = []
    for unit, value in units.items():
        if not unit.endswith(".service"):
            continue
        properties = value.get("properties") or {}
        if int(properties.get("MainPID") or 0) or value.get("is_active") not in {"inactive", "failed"}:
            if not (properties.get("SubState") == "exited" and not int(properties.get("MainPID") or 0)):
                live_services.append(unit)
    timers_paused = all(v.get("is_enabled") == "disabled" and v.get("is_active") == "inactive" for u, v in units.items() if u.endswith(".timer"))
    quiet = bool(timers_paused and admission["ready"] and admission["idle"] and not activity["jobs"] and not processes and not live_services)
    return {"captured_at": now_iso(), "quiet": quiet, "units": units,
            "controls": _controls(runtime_dir, activity), "admission": admission,
            "activity": activity, "writer_processes": processes, "live_services": live_services}


def _identity(state: dict, window_id: str) -> None:
    if state["window_id"] != window_id:
        raise RuntimeError("pause identity differs; baseline is never replaced")


def _unchanged(state: dict, current: dict) -> None:
    if current["controls"] != state["baseline"]["controls"]:
        raise RuntimeError("owner policy/feature intent changed during pause")
    for unit, original in state["baseline"]["units"].items():
        if _unit_fingerprint(current["units"][unit]) != _unit_fingerprint(original):
            raise RuntimeError("unit configuration changed during pause: " + unit)


def pause(runtime_dir: Path, *, window_id: str, actor: str, reason: str,
          systemd, activity_reader: Callable[[], dict], wait_timeout_seconds: float = 1200,
          poll_interval_seconds: float = 2, proc_root: Path = Path("/proc")) -> dict:
    runtime_dir = runtime_dir.resolve()
    _validate_identifier(window_id, label="window_id")
    _validate_actor(actor)
    if not str(reason).strip():
        raise RuntimeError("audited pause reason is required")
    state = load_state(runtime_dir)
    fresh = None
    if state is None or state["phase"] == "restored":
        fresh = preflight(runtime_dir, systemd=systemd, activity_reader=activity_reader)
    with _ExclusiveRestoreLock(runtime_dir):
        current_state = load_state(runtime_dir)
        if fresh is not None:
            if current_state != state:
                raise RuntimeError("maintenance state changed after preflight")
            # Validate the binding before the first durable mutation.
            checked = preflight(runtime_dir, systemd=systemd, activity_reader=activity_reader)
            if checked["baseline_fingerprint"] != fresh["baseline_fingerprint"]:
                raise RuntimeError("baseline changed after preflight")
            state = {"schema_version": SCHEMA, "window_id": window_id,
                     "actor": actor, "reason": reason, "phase": "prepared",
                     "baseline": fresh["baseline"], "baseline_fingerprint": fresh["baseline_fingerprint"],
                     "plan_fingerprint": fresh["baseline_fingerprint"], "steps": []}
            save_state(runtime_dir, state, "pause_prepared")
        else:
            state = current_state
            _identity(state, window_id)
            if state["phase"] in {"restoring", "restored"}:
                raise RuntimeError("pause is already restoring/restored")
        try:
            acquire_barrier(runtime_dir, window_id=window_id, window_kind="maintenance_pause",
                            plan_fingerprint=state["plan_fingerprint"], approval_reference=window_id,
                            actor=actor, reason=reason)
            state["phase"] = "draining"
            state.pop("error", None)
            save_state(runtime_dir, state, "pause_draining")
            for unit in TIMERS:
                if unit not in state["steps"]:
                    value = systemd.unit_state(unit)
                    if value.get("is_enabled") != "disabled" or value.get("is_active") != "inactive":
                        systemd.disable_now(unit)
                    state["steps"].append(unit)
                    save_state(runtime_dir, state, "timer_paused")
                else:
                    value = systemd.unit_state(unit)
                    if value.get("is_enabled") != "disabled" or value.get("is_active") != "inactive":
                        raise RuntimeError("paused timer drifted: " + unit)
            deadline = time.monotonic() + max(0, min(1200, wait_timeout_seconds))
            while True:
                result = readback(runtime_dir, systemd=systemd, activity_reader=activity_reader, proc_root=proc_root)
                _unchanged(state, result)
                if result["quiet"]:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("pause drain incomplete: admitted writers/jobs/services remain")
                time.sleep(max(.05, min(2, poll_interval_seconds)))
            state.update(phase="held", held_at=now_iso(), hold_readback=result)
            save_state(runtime_dir, state, "pause_held")
            confirm_barrier_hold(runtime_dir, window_id=window_id,
                                 plan_fingerprint=state["plan_fingerprint"], maintenance_state=state)
            return {"status": "held", "window_id": window_id, "quiet": True, "expires_at": None}
        except BaseException as exc:
            state["error"] = type(exc).__name__ + ": " + str(exc)
            save_state(runtime_dir, state, "pause_incomplete")
            raise


def resume(runtime_dir: Path, *, window_id: str, actor: str, reason: str,
           systemd, activity_reader: Callable[[], dict], proc_root: Path = Path("/proc")) -> dict:
    runtime_dir = runtime_dir.resolve()
    _validate_identifier(window_id, label="window_id")
    _validate_actor(actor)
    if not str(reason).strip():
        raise RuntimeError("audited restore reason is required")
    from packages.application.business_data_schedule_profile import assert_no_partial_transition
    # Loaded unit digests can still equal baseline before daemon-reload even
    # when a target preset is already on disk. Never escape that operation.
    assert_no_partial_transition(runtime_dir)
    state = load_state(runtime_dir)
    if state is None:
        raise RuntimeError("no pause baseline")
    _identity(state, window_id)
    with _ExclusiveRestoreLock(runtime_dir):
        assert_no_partial_transition(runtime_dir)
        state = load_state(runtime_dir)
        _identity(state, window_id)
        before = readback(runtime_dir, systemd=systemd, activity_reader=activity_reader, proc_root=proc_root)
        _unchanged(state, before)
        barrier = barrier_status(runtime_dir)
        if not barrier["active"]:
            for unit in TIMERS:
                old, current = state["baseline"]["units"][unit], before["units"][unit]
                if (old["is_enabled"], old["is_active"]) != (current["is_enabled"], current["is_active"]):
                    raise RuntimeError("barrier absent with unrestored timer: " + unit)
            if state["phase"] != "restored":
                # Initial crash before acquiring, or final crash after release:
                # read exact state; never acquire another boundary or resubmit.
                if barrier.get("window_id") not in {"", window_id}:
                    raise RuntimeError("released barrier belongs to another window")
                state["phase"] = "restored"
                save_state(runtime_dir, state, "restore_observed_after_interruption")
            return {"status": "restored", "exact_prior_state_restored": True, "idempotent": True}
        if not barrier["active"] or barrier["window_id"] != window_id or barrier["plan_fingerprint"] != state["plan_fingerprint"]:
            raise RuntimeError("pause barrier identity is not proven")
        if state["phase"] != "restoring" and (not before["admission"]["idle"] or before["activity"]["jobs"] or before["writer_processes"] or before["live_services"]):
            raise RuntimeError("writers have not drained; restore refused")
        try:
            state["phase"] = "restoring"
            state.pop("error", None)
            save_state(runtime_dir, state, "restore_started")
            mark_barrier_restoring(runtime_dir, window_id=window_id, plan_fingerprint=state["plan_fingerprint"])
            for unit in TIMERS:
                old = state["baseline"]["units"][unit]
                value = systemd.unit_state(unit)
                if value["is_enabled"] != old["is_enabled"]:
                    systemd._run(["enable" if old["is_enabled"] == "enabled" else "disable", unit])
                value = systemd.unit_state(unit)
                if value["is_active"] != old["is_active"]:
                    systemd._run(["start" if old["is_active"] == "active" else "stop", unit])
                state.setdefault("restore_steps", []).append(unit)
                save_state(runtime_dir, state, "timer_restored")
            after = readback(runtime_dir, systemd=systemd, activity_reader=activity_reader, proc_root=proc_root)
            _unchanged(state, after)
            for unit in TIMERS:
                old, current = state["baseline"]["units"][unit], after["units"][unit]
                if (old["is_enabled"], old["is_active"]) != (current["is_enabled"], current["is_active"]):
                    raise RuntimeError("exact timer restore unproven: " + unit)
            receipt = {"schema_version": SCHEMA, "baseline_fingerprint": state["baseline_fingerprint"], "status": "restored", "exact_prior_state_restored": True,
                       "captured_at": now_iso(), "control_signature": fingerprint(after["controls"]),
                       "units": after["units"], "skipped_cycles_replayed": False}
            state["restore_readback"] = receipt
            save_state(runtime_dir, state, "exact_restore_verified")
            release_barrier(runtime_dir, window_id=window_id, plan_fingerprint=state["plan_fingerprint"],
                            actor=actor, reason=reason, restore_readback=receipt)
            state["phase"] = "restored"
            save_state(runtime_dir, state, "pause_released")
            return receipt
        except BaseException as exc:
            state["error"] = type(exc).__name__ + ": " + str(exc)
            save_state(runtime_dir, state, "restore_incomplete")
            raise
