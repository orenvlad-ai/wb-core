"""Offline prepare/binding/hold integration; host observations are fixture inputs."""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from unittest.mock import Mock, patch

from apps import business_data_maintenance as m

WAREHOUSE = "wb-core-warehouse-functional-sync.timer"


class PreparedBindingTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / m.POLICY_FILENAME).write_text("{}")
        self.snapshot = {
            "schema_version": m.SCHEMA_VERSION,
            "quiet": False, "unknown_wb_core_timers": [], "cron_entries": [],
            "discovered_wb_core_timers": sorted(m.CLASSIFIED_WB_CORE_TIMER_UNITS),
            "auto_updates": {"master_desired": True, "revision": 10,
                "policy_fingerprint": "before", "unknown_processes": [],
                "drift_processes": [], "processes": [
                    {"process_key": "autoanswers", "desired": False},
                    {"process_key": "warehouse_functional", "desired": True}]},
            "timers": {unit: {"unit": unit, "is_enabled": "enabled" if unit == WAREHOUSE else "disabled",
                "is_active": "active" if unit == WAREHOUSE else "inactive"}
                for unit in m.ALL_BUSINESS_TIMER_UNITS},
            "services": {unit: {"is_active": "inactive", "properties": {"MainPID": 0}}
                for unit in m.ALL_BUSINESS_SERVICE_UNITS},
            "runtime_schedules": {"web_vitrina": {"active": False},
                "feedback_complaints": {"active_runs": []}, "spp": {"active_job": None}},
            "writer_locks": {}, "writer_processes": [],
        }
        self.barrier = {"active": True, "phase": "acquiring", "hold_confirmed": False,
            "window_id": "fixture-window", "plan_fingerprint": "sha256:fixture-plan",
            "state_fingerprint": "barrier-fixture", "started_at": "2026-01-01T00:00:00Z"}
        self.systemd = Mock()
        self.systemd.unit_state.side_effect = lambda unit: {"unit": unit, "is_enabled": "enabled", "is_active": "active", "properties": {"MainPID": 0}}
        self.schedules = Mock()
        self.schedules.read_all.return_value = {}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        patches = {
            "maintenance_status": lambda *_a, **_k: deepcopy(self.snapshot),
            "owner_policy_readback": lambda *_a, **_k: deepcopy(self.snapshot["auto_updates"]),
            "_set_master_policy_paused": self.pause,
            "barrier_status": lambda *_a, **_k: deepcopy(self.barrier),
            "_sqlite_sidecar_readback": lambda *_a, **_k: {"operational_path": str(self.root / "op.sqlite3"), "sidecars": {}},
        }
        for name, replacement in patches.items():
            self.stack.enter_context(patch.object(m, name, side_effect=replacement))
        self.kwargs = dict(systemd=self.systemd, schedules=self.schedules,
            window_id=self.barrier["window_id"], plan_fingerprint=self.barrier["plan_fingerprint"],
            autoanswers_reconcile=Mock())
        m.maintenance_prepare(self.root, expected_revision=10, **self.kwargs)
        self.baseline = deepcopy(self.state()["baseline"])
        self.signature = deepcopy(self.state()["control_signature_before_hold"])
        self.assertNotIn("prepared_resume_binding", self.state())

    def pause(self, *_a, **_k):
        self.snapshot["auto_updates"].update(master_desired=False, revision=11, policy_fingerprint="paused")

    def state(self):
        return json.loads((self.root / m.STATE_FILENAME).read_text())

    def bind(self, **overrides):
        return m.maintenance_prepare(self.root, **{**self.kwargs, "expected_revision": 11, **overrides})

    def warehouse_hold(self):
        self.snapshot["timers"][WAREHOUSE].update(is_enabled="disabled", is_active="inactive")
        self.snapshot["quiet"] = True

    def test_prepare_bind_warehouse_hold_business_hold_preserves_restore_baseline(self):
        bound = self.bind()["prepared_resume_binding"]
        self.assertEqual(bound["paused_policy_revision"], 11)
        self.assertEqual(bound["window_id"], self.barrier["window_id"])
        self.assertEqual(bound["plan_fingerprint"], self.barrier["plan_fingerprint"])
        self.assertEqual(self.state()["prepared_resume_binding"], bound)
        self.assertEqual(m._last_private_audit_event(self.root / m.AUDIT_FILENAME)["binding"], bound)
        self.warehouse_hold()
        # Keep the real stable-quiet validation, without wall-clock sleeping.
        with patch.object(m.time, "sleep"):
            held = m.maintenance_hold(self.root, expected_revision=11, poll_interval_seconds=0, **self.kwargs)
        self.assertEqual(held["status"], "held")
        self.assertEqual(self.state()["baseline"], self.baseline)
        self.assertEqual(self.state()["control_signature_before_hold"], self.signature)
        self.assertEqual(self.baseline["timers"][WAREHOUSE]["is_active"], "active")

    def assert_rejected_without_write(self, pattern, **overrides):
        state_before = (self.root / m.STATE_FILENAME).read_bytes()
        audit_before = (self.root / m.AUDIT_FILENAME).read_bytes()
        self.systemd.reset_mock()
        self.schedules.reset_mock()
        with self.assertRaisesRegex(RuntimeError, pattern):
            self.bind(**overrides)
        self.assertEqual((self.root / m.STATE_FILENAME).read_bytes(), state_before)
        self.assertEqual((self.root / m.AUDIT_FILENAME).read_bytes(), audit_before)
        self.systemd.disable_now.assert_not_called()
        self.schedules.disable_all.assert_not_called()

    def test_old_order_rejected(self):
        self.warehouse_hold()
        self.assert_rejected_without_write("warehouse timer drifted")

    def test_wrong_window(self):
        self.assert_rejected_without_write("barrier identity drifted", window_id="wrong")

    def test_wrong_plan(self):
        self.assert_rejected_without_write("barrier identity drifted", plan_fingerprint="wrong")

    def test_wrong_revision(self):
        self.assert_rejected_without_write("stale policy revision", expected_revision=12)

    def test_paused_policy_drift(self):
        self.snapshot["auto_updates"]["policy_fingerprint"] = "changed"
        self.assert_rejected_without_write("paused-policy identity drifted")

    def test_inventory_drift(self):
        self.snapshot["discovered_wb_core_timers"].pop()
        self.assert_rejected_without_write("timer inventory drifted")

    def test_actual_timer_drift(self):
        unit = next(unit for unit in m.ALL_BUSINESS_TIMER_UNITS if unit != WAREHOUSE)
        self.snapshot["timers"][unit]["is_active"] = "active"
        self.assert_rejected_without_write("timer is not paused")

    def test_baseline_drift(self):
        state = self.state()
        state["baseline"]["auto_updates"]["processes"][1]["desired"] = False
        m._save_json_0600(self.root / m.STATE_FILENAME, state)
        self.assert_rejected_without_write("baseline signature drifted")


if __name__ == "__main__":
    unittest.main()
