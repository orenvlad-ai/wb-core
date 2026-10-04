#!/usr/bin/env python3
"""Exact supported restore and interruption tests; isolated state only."""
from __future__ import annotations
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.application import business_data_maintenance_pause as pause
from packages.application.business_data_procedure_admission import initialize_admission, AdmissionLease
from packages.application.business_data_write_barrier import barrier_status


class FakeSystemd:
    def __init__(self):
        self.states = {}
        self.calls = []
        self.fail_once = None
        pairs = [("enabled", "active"), ("enabled", "inactive"), ("disabled", "active"), ("disabled", "inactive")]
        for i, unit in enumerate(pause.TIMERS):
            enabled, active = pairs[i % 4]
            self.states[unit] = {"is_enabled": enabled, "is_active": active,
                "properties": {"LoadState": "loaded", "MainPID": "0", "SubState": "waiting",
                               "FragmentPath": unit, "Persistent": "true", "ExecStart": "known-command"}}
            service = unit.removesuffix(".timer") + ".service"
            self.states[service] = {"is_enabled": "static", "is_active": "inactive",
                "properties": {"LoadState": "loaded", "MainPID": "0", "SubState": "dead",
                               "FragmentPath": service, "ExecStart": "known-command"}}

    def unit_state(self, unit):
        return deepcopy(self.states[unit])

    def discovered_timers(self):
        return list(pause.TIMERS) + [pause.SAFETY_TIMER]

    def discovered_active_services(self):
        return []

    def disable_now(self, unit):
        self.calls.append(("disable_now", unit))
        self.states[unit].update(is_enabled="disabled", is_active="inactive")
        if self.fail_once == unit:
            self.fail_once = None
            raise RuntimeError("ambiguous local command exit")

    def _run(self, args):
        action, unit = args
        self.calls.append((action, unit))
        if action in {"enable", "disable"}:
            self.states[unit]["is_enabled"] = "enabled" if action == "enable" else "disabled"
        else:
            self.states[unit]["is_active"] = "active" if action == "start" else "inactive"
        if self.fail_once == (action, unit):
            self.fail_once = None
            raise RuntimeError("ambiguous restore exit after mutation")


def fixture(runtime):
    initialize_admission(runtime)
    (runtime / pause.POLICY_FILENAME).write_text(json.dumps({"master_desired": True, "revision": 7}))
    systemd = FakeSystemd()
    def activity():
        return {"contract_name": "business_data_maintenance_activity_v1", "admission_ready": True,
                "complete": True, "runtime_dir": str(runtime.resolve()), "jobs": [],
                "feature_intent": {"enabled": True, "schedules": ["01:17"]}}
    return systemd, activity


def main():
    with patch.object(pause, "_cron_entries", return_value=[]):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            systemd, activity = fixture(runtime)
            originals = deepcopy(systemd.states)
            policy = (runtime / pause.POLICY_FILENAME).read_bytes()
            proc = runtime / "proc"
            proc.mkdir()
            options = dict(systemd=systemd, activity_reader=activity, proc_root=proc,
                           window_id="pause-test-001", actor="test", reason="focused test")
            before_files = {p.name: p.read_bytes() for p in runtime.iterdir() if p.is_file()}
            systemd.states[pause.TIMERS[0]]["is_enabled"] = "masked"
            try:
                pause.pause(runtime, **options)
                raise AssertionError("unrestorable baseline accepted")
            except RuntimeError as exc:
                assert "unsupported" in str(exc)
            assert systemd.calls == [] and not barrier_status(runtime)["active"]
            assert before_files == {p.name: p.read_bytes() for p in runtime.iterdir() if p.is_file()}
            systemd.states = deepcopy(originals)
            systemd.fail_once = pause.TIMERS[2]
            try:
                pause.pause(runtime, **options)
                raise AssertionError("injected interruption ignored")
            except RuntimeError:
                pass
            assert barrier_status(runtime)["active"]
            baseline = pause.load_state(runtime)["baseline_fingerprint"]
            assert pause.pause(runtime, **options)["status"] == "held"
            assert pause.load_state(runtime)["baseline_fingerprint"] == baseline
            assert systemd.calls.count(("disable_now", pause.TIMERS[2])) == 1
            assert pause.pause(runtime, **options)["quiet"] is True
            assert pause.resume(runtime, **options)["exact_prior_state_restored"]
            assert not barrier_status(runtime)["active"]
            for unit in pause.TIMERS:
                assert (systemd.states[unit]["is_enabled"], systemd.states[unit]["is_active"]) == (originals[unit]["is_enabled"], originals[unit]["is_active"])
                assert systemd.states[unit]["properties"]["Persistent"] == "true"
            assert (runtime / pause.POLICY_FILENAME).read_bytes() == policy
            calls = list(systemd.calls)
            assert pause.resume(runtime, **options)["idempotent"]
            assert systemd.calls == calls
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            systemd, activity = fixture(runtime)
            proc = runtime / "proc"
            proc.mkdir()
            options = dict(systemd=systemd, activity_reader=activity, proc_root=proc,
                           window_id="pause-test-002", actor="test", reason="partial abort")
            systemd.fail_once = pause.TIMERS[0]
            try:
                pause.pause(runtime, **options)
            except RuntimeError:
                pass
            assert pause.load_state(runtime)["phase"] == "draining"
            assert pause.resume(runtime, **options)["exact_prior_state_restored"]
            assert not barrier_status(runtime)["active"]
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            systemd, activity = fixture(runtime)
            originals = deepcopy(systemd.states)
            proc = runtime / "proc"
            proc.mkdir()
            options = dict(systemd=systemd, activity_reader=activity, proc_root=proc,
                           window_id="pause-test-003", actor="test", reason="drain and exact retry")
            lease = AdmissionLease(runtime)
            try:
                pause.pause(runtime, wait_timeout_seconds=0, **options)
                raise AssertionError("active writer reported quiet")
            except TimeoutError:
                pass
            assert pause.load_state(runtime)["phase"] == "draining"
            assert barrier_status(runtime)["active"]
            assert not pause.readback(runtime, systemd=systemd, activity_reader=activity, proc_root=proc)["quiet"]
            lease.close()
            assert pause.pause(runtime, **options)["expires_at"] is None
            with patch.object(pause, "now_iso", return_value="2036-10-04T00:00:00Z"):
                assert barrier_status(runtime)["active"], "pause must never expire by time"
            systemd.states[pause.TIMERS[0]]["properties"]["Persistent"] = "false"
            calls = list(systemd.calls)
            try:
                pause.resume(runtime, **options)
                raise AssertionError("unit config drift accepted")
            except RuntimeError as exc:
                assert "configuration changed" in str(exc)
            assert systemd.calls == calls and barrier_status(runtime)["active"]
            systemd.states[pause.TIMERS[0]]["properties"]["Persistent"] = "true"
            systemd.fail_once = ("enable", pause.TIMERS[0])
            try:
                pause.resume(runtime, **options)
                raise AssertionError("restore interruption ignored")
            except RuntimeError:
                pass
            assert barrier_status(runtime)["active"] and pause.load_state(runtime)["phase"] == "restoring"
            assert pause.resume(runtime, **options)["exact_prior_state_restored"]
            assert systemd.calls.count(("enable", pause.TIMERS[0])) == 1
            for unit in pause.TIMERS:
                assert (systemd.states[unit]["is_enabled"], systemd.states[unit]["is_active"]) == (originals[unit]["is_enabled"], originals[unit]["is_active"])
    print("business_data_maintenance_pause_smoke: OK")


if __name__ == "__main__":
    main()
