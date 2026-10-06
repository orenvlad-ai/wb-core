"""Exact legacy -> one fixed profile transition under the existing held pause.

One operation journal retains immutable original plan/baseline/before-images.
Every continuation observes files/units first, never blindly resends a step.
"""
from __future__ import annotations
import base64
import hashlib
import os
from pathlib import Path
import re
import shlex
import stat
import tempfile

from packages.application import business_data_maintenance_pause as pause
from packages.application import business_data_schedule_profile as profile
from packages.application.business_data_write_barrier import (
    _atomic_write_private_json, _append_private_audit, _validate_actor, _BarrierLock,
    _validate_identifier, barrier_status, release_schedule_target_barrier,
)

def _image(path: Path) -> dict | None:
    if path.is_symlink() or path.parent.is_symlink():
        raise RuntimeError("schedule file or parent is a symlink")
    if not path.exists():
        return None
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_size > 65536:
        raise RuntimeError("schedule file is not a bounded regular file")
    data = path.read_bytes()
    return {"bytes_b64": base64.b64encode(data).decode(), "sha256": hashlib.sha256(data).hexdigest(),
            "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid}


def _write_image(path: Path, expected: dict | None, target: dict | None) -> None:
    current = _image(path)
    if current == target:
        return
    if current != expected:
        raise RuntimeError("schedule before-image CAS differs: " + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    if target is None:
        path.unlink()
    else:
        fd, temporary = tempfile.mkstemp(prefix=".cycle-profile-", dir=path.parent)
        temporary = Path(temporary)
        try:
            data = base64.b64decode(target["bytes_b64"], validate=True)
            if hashlib.sha256(data).hexdigest() != target["sha256"]:
                raise RuntimeError("schedule before-image bytes do not match hash")
            os.fchmod(fd, target["mode"])
            os.fchown(fd, target["uid"], target["gid"])
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(data); stream.flush(); os.fsync(fd)
            os.replace(temporary, path)
        finally:
            os.close(fd)
            if temporary.exists():
                temporary.unlink()
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _run(systemd, arguments) -> None:
    result = systemd._run(arguments)
    if result is not None and result.returncode:
        raise RuntimeError("systemd transition command failed; observe same operation before continuing")


def _original_reload(systemd, plan) -> None:
    """Observe ambiguous reload, including systemd's stale removed drop-in path.

    unit_state hashes every loaded path and cannot read our removed file before
    reload. Only that exact missing preset may use raw property readback; prove
    all timers paused first. Unknown missing/foreign fragments remain blocked.
    """
    try:
        digest = pause._unit_fingerprint(systemd.unit_state(profile.WAREHOUSE_TIMER))
    except RuntimeError:
        if plan["dropin_before"] is not None or _image(Path(plan["dropin_path"])) is not None:
            raise
        def raw(unit):
            response = systemd._run(["show", unit,
                "--property=FragmentPath,DropInPaths,UnitFileState,ActiveState", "--no-pager"])
            if response.returncode:
                raise RuntimeError("raw rollback reload readback failed")
            return dict(line.split("=", 1) for line in response.stdout.splitlines() if "=" in line)
        loaded = raw(profile.WAREHOUSE_TIMER)
        if (shlex.split(loaded.get("DropInPaths", "")) != [plan["dropin_path"]]
                or loaded.get("FragmentPath") != plan["baseline"]["units"][profile.WAREHOUSE_TIMER]["properties"]["FragmentPath"]):
            raise RuntimeError("missing unit content is outside the exact rollback preset")
        for timer in pause.TIMERS:
            observed = raw(timer)
            if (observed.get("UnitFileState"), observed.get("ActiveState")) != ("disabled", "inactive"):
                raise RuntimeError("removed-preset reload requires all timers paused")
        digest = None
    if digest != pause._unit_fingerprint(plan["baseline"]["units"][profile.WAREHOUSE_TIMER]):
        _run(systemd, ["daemon-reload"])


def _save(runtime, state, event):
    state["updated_at"] = pause.now_iso()
    state["fingerprint"] = profile.fingerprint({k: v for k, v in state.items() if k != "fingerprint"})
    root = runtime / profile.TRANSITIONS_DIRECTORY
    if not root.exists() and not root.is_symlink():
        root.mkdir(mode=0o700)
    _atomic_write_private_json(profile.transition_path(runtime, state["plan"]["operation_id"]), state)
    _append_private_audit(runtime / ".business-data-schedule-audit.jsonl", {
        "event": event, "operation_id": state["plan"]["operation_id"], "phase": state["phase"],
        "plan_fingerprint": state["plan"]["fingerprint"], "captured_at": state["updated_at"],
    })


def _pause_binding(runtime, plan, *, active=True):
    state = pause.load_state(runtime)
    if (not state or state["window_id"] != plan["window_id"]
            or state["baseline_fingerprint"] != plan["baseline_fingerprint"]
            or state["baseline"] != plan["baseline"]):
        raise RuntimeError("immutable original pause baseline/window differs")
    barrier = barrier_status(runtime)
    if (barrier.get("window_id") != plan["window_id"]
            or barrier.get("plan_fingerprint") != plan["baseline_fingerprint"]
            or barrier.get("window_kind") != "maintenance_pause"
            or (active and (not barrier.get("active") or not barrier.get("hold_confirmed")))):
        raise RuntimeError("exact held maintenance barrier is unproven")
    return state, barrier


def _observed(runtime, plan, systemd, activity_reader, proc_root, *, target=False, mixed=False):
    current = pause.readback(runtime, systemd=systemd, activity_reader=activity_reader, proc_root=proc_root)
    if current["controls"] != plan["baseline"]["controls"]:
        raise RuntimeError("raw owner policy/feature intent changed; projection is separate")
    if (not current["admission"]["ready"] or not current["admission"]["idle"]
            or current["activity"]["jobs"] or current["writer_processes"] or current["live_services"]):
        raise RuntimeError("writers have not drained for schedule transition")
    for unit, original in plan["baseline"]["units"].items():
        original_digest = pause._unit_fingerprint(original)
        wanted = plan["target_unit_digests"][unit] if target else original_digest
        allowed = {original_digest, plan["target_unit_digests"][unit]} if mixed else {wanted}
        if pause._unit_fingerprint(current["units"][unit]) not in allowed:
            raise RuntimeError("unexpected unit configuration drift: " + unit)
    return current


def preview(runtime_dir: Path, *, operation_id: str, window_id: str, deployed_sha: str,
            systemd, activity_reader, proc_root=Path("/proc"), unit_directory=Path("/etc/systemd/system")) -> dict:
    runtime = runtime_dir.resolve()
    _validate_identifier(operation_id, label="operation_id")
    if re.fullmatch(r"[0-9a-f]{40}", deployed_sha) is None:
        raise RuntimeError("exact deployed revision is required")
    profile.assert_no_partial_transition(runtime)
    if profile.load_selector(runtime) is not None:
        raise RuntimeError("only the fixed legacy-to-cycle transition is supported")
    old = pause.load_state(runtime)
    if not old or old["phase"] != "held" or old["window_id"] != window_id:
        raise RuntimeError("target preview requires the exact held maintenance pause")
    seed = {"window_id": window_id, "baseline_fingerprint": old["baseline_fingerprint"], "baseline": old["baseline"]}
    _pause_binding(runtime, seed)
    current = pause.readback(runtime, systemd=systemd, activity_reader=activity_reader, proc_root=proc_root)
    pause._unchanged(old, current)
    if not current["quiet"]:
        raise RuntimeError("target preview requires complete held idle proof")
    path = unit_directory / profile.DROPIN_RELATIVE
    before = _image(path)
    properties = current["units"][profile.WAREHOUSE_TIMER]["properties"]
    overrides = shlex.split(properties.get("DropInPaths", ""))
    if overrides != ([str(path)] if before else []):
        raise RuntimeError("unknown warehouse timer drop-in refuses transition")
    if path.parent.exists() and set(p.name for p in path.parent.iterdir()) != ({path.name} if before else set()):
        raise RuntimeError("unknown warehouse timer override file refuses transition")
    if before and base64.b64decode(before["bytes_b64"]) != profile.DROPIN_BYTES:
        raise RuntimeError("existing warehouse preset path contains foreign bytes")
    fragment = Path(properties.get("FragmentPath", ""))
    expected = Path(__file__).resolve().parents[2] / "artifacts/registry_upload_http_entrypoint/systemd" / profile.WAREHOUSE_TIMER
    if fragment.is_symlink() or not fragment.is_file() or fragment.read_bytes() != expected.read_bytes():
        raise RuntimeError("warehouse timer base fragment is not the reviewed preset base")
    target_digest = profile.fingerprint([
        {"path": str(fragment), "sha256": hashlib.sha256(fragment.read_bytes()).hexdigest()},
        {"path": str(path), "sha256": profile.PROFILE["dropin_sha256"]},
    ])
    target_pairs = {unit: [v["is_enabled"], v["is_active"]]
                    for unit, v in old["baseline"]["units"].items() if unit.endswith(".timer")}
    target_pairs.update({unit: list(pair) for unit, pair in profile.effective_core(runtime).items()})
    readiness = profile.activation_readiness(runtime)
    plan = {"schema_version": profile.PLAN_SCHEMA, "runtime_dir": str(runtime), "operation_id": operation_id,
            "window_id": window_id, "deployed_sha": deployed_sha, **seed,
            "profile": profile.PROFILE, "profile_fingerprint": profile.fingerprint(profile.PROFILE),
            "dropin_path": str(path), "dropin_before": before,
            "selector_before": _image(runtime / profile.SELECTOR_FILENAME),
            "target_unit_digests": {unit: target_digest if unit == profile.WAREHOUSE_TIMER else pause._unit_fingerprint(value)
                                    for unit, value in old["baseline"]["units"].items()},
            "target_timer_states": target_pairs,
            "raw_controls_fingerprint": profile.fingerprint(old["baseline"]["controls"]),
            "activation_readiness": readiness}
    plan["fingerprint"] = profile.fingerprint(plan)
    return plan


def _validate_plan(runtime, plan, expected_fingerprint, deployed_sha, unit_directory):
    profile.signed(plan, profile.PLAN_SCHEMA)
    if (plan["fingerprint"] != expected_fingerprint or plan["runtime_dir"] != str(runtime)
            or plan["deployed_sha"] != deployed_sha or plan["profile"] != profile.PROFILE
            or plan["dropin_path"] != str(unit_directory / profile.DROPIN_RELATIVE)):
        raise RuntimeError("exact reviewed fixed target plan differs")
    if not profile.activation_readiness(runtime)["ready"] or not plan["activation_readiness"]["ready"]:
        raise RuntimeError("cycle activation dependencies are not integrated")


def _selector(plan):
    value = {"schema_version": profile.SCHEMA, "runtime_dir": plan["runtime_dir"], "profile": profile.PROFILE,
             "profile_fingerprint": plan["profile_fingerprint"], "operation_id": plan["operation_id"],
             "window_id": plan["window_id"], "plan_fingerprint": plan["fingerprint"],
             "baseline_fingerprint": plan["baseline_fingerprint"], "deployed_sha": plan["deployed_sha"]}
    value["fingerprint"] = profile.fingerprint(value)
    return value


def _target_image(data, mode=0o644):
    return {"bytes_b64": base64.b64encode(data).decode(), "sha256": hashlib.sha256(data).hexdigest(),
            "mode": mode, "uid": os.geteuid(), "gid": os.getegid()}


def apply(runtime_dir: Path, *, reviewed_plan: dict, expected_fingerprint: str, deployed_sha: str,
          actor: str, reason: str, systemd, activity_reader, proc_root=Path("/proc"),
          unit_directory=Path("/etc/systemd/system"), _fault=None) -> dict:
    runtime = runtime_dir.resolve(); plan = reviewed_plan
    _validate_actor(actor)
    if not str(reason).strip():
        raise RuntimeError("audited schedule transition reason is required")
    _validate_plan(runtime, plan, expected_fingerprint, deployed_sha, unit_directory)
    fault = _fault or (lambda point: None)  # Private offline test hook, no CLI option.
    with pause._ExclusiveRestoreLock(runtime):
        state = profile.load_transition(runtime, plan["operation_id"])
        if state and state["plan"] != plan:
            raise RuntimeError("same operation has a different reviewed target")
        if state and state["phase"] in {"rolling_back", "rolled_back"}:
            raise RuntimeError("transition rollback requires same-operation rollback/status")
        old, barrier = _pause_binding(runtime, plan, active=not state or state["phase"] not in {"committed", "released"})
        if not state:
            fresh = preview(runtime, operation_id=plan["operation_id"], window_id=plan["window_id"],
                            deployed_sha=deployed_sha, systemd=systemd, activity_reader=activity_reader,
                            proc_root=proc_root, unit_directory=unit_directory)
            if fresh != plan:
                raise RuntimeError("schedule target changed after preview")
            state = {"schema_version": profile.TRANSITION_SCHEMA, "plan": plan, "phase": "prepared",
                     "actor": actor, "reason": reason}
            # Ordinary direct release does not own the restore lock. Serialize
            # the first intent with its release authority, re-proving held before
            # recording anything; subsequent ordinary release sees partial state.
            with _BarrierLock(runtime):
                _pause_binding(runtime, plan)
                _save(runtime, state, "prepared")
            fault("prepared")
        current = _observed(runtime, plan, systemd, activity_reader, proc_root, mixed=True)
        if state["phase"] not in {"committed", "released"}:
            _write_image(Path(plan["dropin_path"]), plan["dropin_before"], _target_image(profile.DROPIN_BYTES))
            fault("dropin_written")
            state["phase"] = "dropin_installed"; _save(runtime, state, "dropin_installed")
            fault("dropin_installed")
            if pause._unit_fingerprint(systemd.unit_state(profile.WAREHOUSE_TIMER)) != plan["target_unit_digests"][profile.WAREHOUSE_TIMER]:
                _run(systemd, ["daemon-reload"])
            fault("reloaded")
            _observed(runtime, plan, systemd, activity_reader, proc_root, target=True)
            profile.prove_preset(systemd, unit_directory=unit_directory)
            state["phase"] = "reloaded"; _save(runtime, state, "reloaded")
            selector_path = runtime / profile.SELECTOR_FILENAME
            selected = profile.read_private(selector_path, optional=True)
            if selected != _selector(plan):
                if _image(selector_path) != plan["selector_before"]:
                    raise RuntimeError("selector before-image CAS differs")
                _atomic_write_private_json(selector_path, _selector(plan))
            fault("selector_written")
            state["phase"] = "selector_selected"; _save(runtime, state, "selector_selected")
            state["phase"] = "timers_restoring"; _save(runtime, state, "timers_restoring")
            for unit, pair in plan["target_timer_states"].items():
                value = systemd.unit_state(unit)
                if value["is_enabled"] != pair[0]:
                    _run(systemd, ["enable" if pair[0] == "enabled" else "disable", unit])
                value = systemd.unit_state(unit)
                if value["is_active"] != pair[1]:
                    _run(systemd, ["start" if pair[1] == "active" else "stop", unit])
                fault("timer:" + unit)
            after = _observed(runtime, plan, systemd, activity_reader, proc_root, target=True)
            _prove_target(runtime, state, after, systemd, unit_directory)
            state["receipt"] = {"schema_version": profile.RECEIPT_SCHEMA, "status": "restored",
                                "exact_target_state_restored": True, "exact_prior_state_restored": False,
                                "operation_id": plan["operation_id"], "window_id": plan["window_id"],
                                "baseline_fingerprint": plan["baseline_fingerprint"], "target_fingerprint": plan["fingerprint"],
                                "profile_fingerprint": plan["profile_fingerprint"], "captured_at": pause.now_iso(),
                                "control_signature": plan["raw_controls_fingerprint"], "units": after["units"],
                                "skipped_cycles_replayed": False}
            state["phase"] = "target_verified"; _save(runtime, state, "target_verified"); fault("target_verified")
            state["phase"] = "committed"; _save(runtime, state, "committed"); fault("committed")
        else:
            _prove_target(runtime, state, current, systemd, unit_directory)
        final = _observed(runtime, plan, systemd, activity_reader, proc_root, target=True)
        _prove_target(runtime, state, final, systemd, unit_directory)
        release_schedule_target_barrier(runtime, window_id=plan["window_id"],
                                       plan_fingerprint=plan["baseline_fingerprint"], operation_id=plan["operation_id"],
                                       actor=actor, reason=reason)
        fault("barrier_released")
        # Preserve the original pause baseline. Its receipt is explicitly target,
        # so ordinary resume will still refuse the intentional unit drift.
        old.update(phase="restored", restore_readback=state["receipt"])
        pause.save_state(runtime, old, "schedule_target_released")
        state["phase"] = "released"; _save(runtime, state, "released")
        return state["receipt"]


def _prove_target(runtime, state, current, systemd, unit_directory):
    plan = state["plan"]
    if profile.read_private(runtime / profile.SELECTOR_FILENAME) != _selector(plan):
        raise RuntimeError("exact selected target is unproven")
    profile.prove_preset(systemd, unit_directory=unit_directory)
    if _image(Path(plan["dropin_path"])) != _target_image(profile.DROPIN_BYTES):
        raise RuntimeError("exact target preset mode/ownership is unproven")
    if {unit: list(pair) for unit, pair in profile.effective_core(runtime).items()} != {
            unit: plan["target_timer_states"][unit] for unit in profile.CORE_TIMERS}:
        raise RuntimeError("effective raw owner intent differs from approved target")
    for unit, pair in plan["target_timer_states"].items():
        actual = current["units"][unit]
        if [actual["is_enabled"], actual["is_active"]] != pair:
            raise RuntimeError("exact target timer state is unproven: " + unit)


def rollback(runtime_dir: Path, *, operation_id: str, actor: str, reason: str,
             systemd, activity_reader, proc_root=Path("/proc"), _fault=None) -> dict:
    runtime = runtime_dir.resolve()
    _validate_actor(actor)
    if not str(reason).strip():
        raise RuntimeError("audited rollback reason is required")
    fault = _fault or (lambda point: None)
    with pause._ExclusiveRestoreLock(runtime):
        state = profile.load_transition(runtime, operation_id)
        if not state or state["phase"] in {"committed", "released"}:
            raise RuntimeError("schedule rollback is available only before commit")
        plan = state["plan"]; _pause_binding(runtime, plan)
        if state["phase"] in {"rolling_back", "rolled_back"} and _image(Path(plan["dropin_path"])) == plan["dropin_before"]:
            _original_reload(systemd, plan)
        _observed(runtime, plan, systemd, activity_reader, proc_root, mixed=True)
        state["phase"] = "rolling_back"; _save(runtime, state, "rollback_started")
        for unit in pause.TIMERS:
            value = systemd.unit_state(unit)
            if (value["is_enabled"], value["is_active"]) != ("disabled", "inactive"):
                systemd.disable_now(unit)
            fault("rollback_timer:" + unit)
        current = _observed(runtime, plan, systemd, activity_reader, proc_root, mixed=True)
        if not current["quiet"]:
            raise RuntimeError("rollback drain is incomplete")
        _write_image(Path(plan["dropin_path"]), _target_image(profile.DROPIN_BYTES), plan["dropin_before"])
        fault("rollback_dropin")
        selector = runtime / profile.SELECTOR_FILENAME
        selected_image = _image(selector)
        if selected_image != plan["selector_before"]:
            if profile.read_private(selector) != _selector(plan):
                raise RuntimeError("rollback selector is not the same operation")
            _write_image(selector, selected_image, plan["selector_before"])
        fault("rollback_selector")
        _original_reload(systemd, plan)
        fault("rollback_reloaded")
        _observed(runtime, plan, systemd, activity_reader, proc_root)
        state["phase"] = "rolled_back"; _save(runtime, state, "rolled_back")
        return {"status": "rolled_back", "window_id": plan["window_id"], "original_configuration_restored": True,
                "barrier_active": True, "next_action": "ordinary exact pause resume"}
