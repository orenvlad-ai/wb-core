#!/usr/bin/env python3
"""Actual nonblocking collector/History races and latest configured-slot cadence."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import io
import inspect
import json
import os
import pwd
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps import wb_buyer_authenticated_collect as collector
from apps import wb_buyer_chrome_price as price
from apps.web_vitrina_finished_snapshot_build import HEAVY_UNITS, systemd_admission
from packages.application.business_data_heavy_admission import (
    HeavyAdmissionBusy, current_heavy_owner, heavy_admitted, heavy_admission_status,
)
from packages.application.wb_buyer_authenticated_observations import append_observation, finish_run


class Seller:
    def fetch(self, request):
        return {"data": {"listGoods": []}}


@contextmanager
def foreign_owner(root, operation="cycle"):
    """A real other-thread flock, with explicit lifetime and failure propagation."""
    started, release = threading.Event(), threading.Event()
    errors = []
    def hold():
        try:
            with heavy_admitted(root, operation=operation):
                started.set()
                if not release.wait(10):
                    raise AssertionError("test owner release timed out")
        except BaseException as exc:
            errors.append(exc)
            started.set()
    thread = threading.Thread(target=hold)
    thread.start()
    assert started.wait(5)
    try:
        if errors:
            raise errors[0]
        yield
    finally:
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        if errors:
            raise errors[0]


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.db = self.root / "operational.sqlite"
        sqlite3.connect(self.db).close()
        self.runtime = SimpleNamespace(runtime_dir=self.root, db_path=self.db)

    def schedules(self, *times):
        value = {"schedules": [{"id": f"slot-{index}", "enabled": True,
                  "local_time_hhmm": clock, "timezone": "Asia/Yekaterinburg"}
                  for index, clock in enumerate(times)]}
        (self.root / "sheet_vitrina_v1_auto_refresh_schedules.json").write_text(json.dumps(value))

    def invoke(self, moment, *, selected=True, reader=None, constructor=None, publisher=None):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)
        output = io.StringIO()
        original_collect = collector.collect_once
        def collect(runtime, **kwargs):
            return original_collect(runtime, seller_source=Seller(), buyer_read=reader or (lambda ids: []),
                                    now=lambda: moment.isoformat(), **kwargs)
        def construct(root):
            self.assertIsNotNone(current_heavy_owner(root))
            return self.runtime
        with patch.object(sys, "argv", ["collector", "--runtime-dir", str(self.root), "--tick"]), \
                patch.object(collector, "datetime", Clock), patch.object(collector, "selected", return_value=selected), \
                patch.object(collector, "_read_env_file", return_value={}), \
                patch.object(collector, "RegistryUploadDbBackedRuntime", side_effect=constructor or construct), \
                patch.object(collector, "_requested_ids", return_value=[1]), \
                patch.object(collector, "collect_once", side_effect=collect), \
                patch.object(collector, "publish_once", side_effect=publisher or AssertionError("selected cycle legacy POST")), \
                patch.object(collector, "_build_web_auth_cookie", side_effect=AssertionError("selected cycle auth")) if selected else patch.object(collector, "_build_web_auth_cookie", return_value="cookie"), \
                patch("sys.stdout", output):
            result = collector.main()
        return result, [json.loads(line) for line in output.getvalue().splitlines()]

    def test_90min_busy_then_100min_collects_once_actual_business_date(self):
        self.schedules("23:00", "02:00")
        due = datetime(2026, 11, 9, 18, tzinfo=timezone.utc)
        before = self.db.read_bytes()
        with foreign_owner(self.root):
            result, payload = self.invoke(due + timedelta(minutes=90),
                                          constructor=lambda root: self.fail("busy constructed writer"))
        self.assertEqual(result, 0)
        self.assertEqual(payload, [{"status": "skipped_heavy_busy"}])
        self.assertEqual(self.db.read_bytes(), before)
        moment = due + timedelta(minutes=100)
        result, payload = self.invoke(moment)
        self.assertEqual(result, 0)
        self.assertEqual(payload[0]["business_date"], "2026-11-10")
        self.assertEqual(payload[1]["publication_status"], "cycle_managed")
        expected_id = collector._run_id("slot-0", due.isoformat())
        self.assertEqual(payload[0]["run_id"], expected_id)
        result, payload = self.invoke(moment + timedelta(minutes=10), reader=lambda ids: self.fail("duplicate read"))
        self.assertEqual(payload, [{"status": "already_collected", "run_id": expected_id}])
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM wb_buyer_authenticated_runs").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM wb_buyer_authenticated_observations").fetchone()[0], 1)

    def test_next_actual_configured_slot_wins_even_during_minimum_delay(self):
        self.schedules("23:00", "02:00")
        now = datetime(2026, 11, 9, 21, 1, tzinfo=timezone.utc)
        with patch.object(collector, "selected", return_value=True):
            self.assertIsNone(collector._recent_configured_slot(self.root, now))
            self.assertEqual(collector._recent_configured_slot(self.root, now + timedelta(minutes=4)),
                             ("slot-1", "2026-11-09T21:00:00+00:00"))
        # No cycle-derived fixed 3h or 90m window: next actual edited slot wins.
        self.schedules("23:00", "00:45")
        with patch.object(collector, "selected", return_value=True):
            self.assertEqual(collector._recent_configured_slot(self.root, datetime(2026, 11, 9, 19, 50, tzinfo=timezone.utc)),
                             ("slot-1", "2026-11-09T19:45:00+00:00"))

    def test_legacy_45min_window_is_unchanged(self):
        self.schedules("23:00")
        due = datetime(2026, 11, 9, 18, tzinfo=timezone.utc)
        with patch.object(collector, "selected", return_value=False):
            self.assertIsNotNone(collector._recent_configured_slot(self.root, due + timedelta(minutes=45)))
            self.assertIsNone(collector._recent_configured_slot(self.root, due + timedelta(minutes=46)))

    def test_direct_collection_busy_consumes_no_run_or_reader(self):
        before = self.db.read_bytes()
        with foreign_owner(self.root), patch.object(collector, "begin_run", side_effect=AssertionError("busy began run")):
            result = collector.collect_once(self.runtime, run_id="retained-slot", requested_nm_ids=[1],
                    seller_source=Seller(), buyer_read=lambda ids: self.fail("busy read"))
        self.assertEqual(result, {"status": "skipped_heavy_busy"})
        self.assertEqual(self.db.read_bytes(), before)

    def test_collector_excludes_history_until_observation_finish(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        def reader(ids):
            self.assertIsNotNone(current_heavy_owner(self.root))
            entered.set()
            self.assertTrue(release.wait(5))
            return []
        def append(*args, **kwargs):
            self.assertIsNotNone(current_heavy_owner(self.root))
            # Independent OS process, so no contextvar/nesting shortcut.
            result = subprocess.run([sys.executable, "-c", CHILD_TRY, str(self.root)],
                                    cwd=ROOT, capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), "busy")
            return append_observation(*args, **kwargs)
        def finish(*args, **kwargs):
            self.assertIsNotNone(current_heavy_owner(self.root))
            return finish_run(*args, **kwargs)
        def work():
            try:
                with patch.object(collector, "append_observation", side_effect=append), patch.object(collector, "finish_run", side_effect=finish):
                    collector.collect_once(self.runtime, run_id="race", requested_nm_ids=[1],
                                           seller_source=Seller(), buyer_read=reader)
            except BaseException as exc:
                errors.append(exc)
                entered.set()
        thread = threading.Thread(target=work)
        thread.start()
        self.assertTrue(entered.wait(5))
        try:
            with self.assertRaises(HeavyAdmissionBusy), heavy_admitted(self.root, operation="history"):
                pass
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        if errors:
            raise errors[0]
        with heavy_admitted(self.root, operation="history"):
            self.assertEqual(current_heavy_owner(self.root).operation, "history")

    def test_http_single_and_batch_readers_cannot_launch_under_history(self):
        with foreign_owner(self.root, "history"), patch.object(price.os, "geteuid", return_value=0), \
                patch.object(price.runtime, "profile_operation_lock", side_effect=AssertionError("unadmitted profile")):
            single = price.read_price(1, runtime_dir=self.root)
            batch = price.read_prices([1, 2], runtime_dir=self.root)
        self.assertEqual(single["reason"], "heavy_producer_running")
        self.assertTrue(all(row["reason"] == "heavy_producer_running" for row in batch))

    def test_active_buyer_unit_exception_requires_live_history_owner_only(self):
        def run(command, **kwargs):
            return SimpleNamespace(stdout="LoadState=loaded\nActiveState=" + (
                "activating\nMainPID=77\n" if command[2] == "wb-core-buyer-authenticated-collect.service"
                else "inactive\nMainPID=0\n"))
        self.assertEqual(systemd_admission(run, runtime_dir=self.root), "busy")
        with heavy_admitted(self.root, operation="cycle"), \
                patch("apps.web_vitrina_finished_snapshot_build.selected", return_value=True):
            self.assertEqual(systemd_admission(run, runtime_dir=self.root), "idle")
            with patch("apps.web_vitrina_finished_snapshot_build.selected", return_value=False):
                self.assertEqual(systemd_admission(run, runtime_dir=self.root), "busy")
            self.assertEqual(systemd_admission(run), "busy")
            def another_active(command, **kwargs):
                if command[2] == HEAVY_UNITS[0]:
                    return SimpleNamespace(stdout="LoadState=loaded\nActiveState=active\nMainPID=88\n")
                return run(command, **kwargs)
            self.assertEqual(systemd_admission(another_active, runtime_dir=self.root), "busy")
            def malformed_buyer(command, **kwargs):
                return SimpleNamespace(stdout="LoadState=not-found\n") if "buyer-authenticated" in command[2] else run(command, **kwargs)
            self.assertEqual(systemd_admission(malformed_buyer, runtime_dir=self.root), "unknown")
        with heavy_admitted(self.root, operation="finance"):
            self.assertEqual(systemd_admission(run, runtime_dir=self.root), "busy")
        with heavy_admitted(self.root, operation="history"):
            with patch("apps.web_vitrina_finished_snapshot_build.selected", return_value=False):
                self.assertEqual(systemd_admission(run, runtime_dir=self.root), "busy")
            with patch("apps.web_vitrina_finished_snapshot_build.selected", return_value=True):
                self.assertEqual(systemd_admission(run, runtime_dir=self.root), "idle")
        self.assertEqual(systemd_admission(run, runtime_dir=self.root), "busy")

    def test_brief_busy_collector_startup_does_not_block_actual_history_owner(self):
        self.schedules("23:00", "02:00")
        results = []
        errors = []
        def startup():
            try:
                results.append(self.invoke(datetime(2026, 11, 9, 19, 30, tzinfo=timezone.utc),
                                           constructor=lambda root: self.fail("busy constructor")))
            except BaseException as exc:
                errors.append(exc)
        def run(command, **kwargs):
            state = "activating\nMainPID=77\n" if "buyer-authenticated" in command[2] else "inactive\nMainPID=0\n"
            return SimpleNamespace(stdout="LoadState=loaded\nActiveState=" + state)
        with heavy_admitted(self.root, operation="cycle"):
            thread = threading.Thread(target=startup)
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            with patch("apps.web_vitrina_finished_snapshot_build.selected", return_value=True):
                self.assertEqual(systemd_admission(run, runtime_dir=self.root), "idle")
        if errors:
            raise errors[0]
        self.assertEqual(results, [(0, [{"status": "skipped_heavy_busy"}])])

    def test_selected_publication_does_not_create_journal_or_post(self):
        with patch.object(collector, "selected", return_value=True), \
                patch.object(collector, "begin_publication", side_effect=AssertionError("selected publication journal")):
            self.assertEqual(collector.publish_once(self.runtime, run_id="x", business_date="2026-11-10", cookie="",
                             post_json=lambda *args, **kwargs: self.fail("selected POST")), {"status": "cycle_managed"})

    def test_legacy_post_and_poll_are_outside_collection_lease(self):
        self.schedules("23:00")
        real_publish = collector.publish_once
        def publish(runtime, **kwargs):
            self.assertIsNone(current_heavy_owner(self.root))
            def post(*args, **options):
                self.assertTrue(heavy_admission_status(self.root)["idle"])
                return {"job_id": "legacy-job"}
            def poll(**options):
                self.assertTrue(heavy_admission_status(self.root)["idle"])
                return {"status": "success"}
            return real_publish(runtime, post_json=post, poll_job=poll, **kwargs)
        result, payload = self.invoke(datetime(2026, 11, 9, 18, 10, tzinfo=timezone.utc), selected=False, publisher=publish)
        self.assertEqual(result, 0)
        self.assertEqual(payload[1]["publication_status"], "completed")

    def test_raw_runner_cli_requires_parent_lock_proof(self):
        result = subprocess.run([sys.executable, str(ROOT / "apps/wb_buyer_chrome_price.py"), "read", "--nm-id", "1"],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('"status":"observed"', result.stdout)

    @unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0, "Linux root-owned runner lock authority")
    def test_child_inherits_exact_locks_without_reacquiring(self):
        with heavy_admitted(self.root, operation="buyer-authenticated-collect") as owner:
            price._reader_fds(999, self.root)  # Native parent path and lock proof.
            code = """import os,sys,json
from apps import wb_buyer_chrome_price as price
price._read_in_runner=lambda nm_id: {'status':'fixture_observed','nm_id':nm_id}
sys.argv=['reader','read','--nm-id','1','--runtime-dir',sys.argv[1],'--heavy-fd',sys.argv[2],'--maintenance-fd',sys.argv[3]]
price.main()
"""
            fds = (owner.fd, owner._maintenance.fd)
            result = subprocess.run([sys.executable, "-c", code, str(self.root), *map(str, fds)],
                                    cwd=ROOT, pass_fds=fds, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout), {"status": "fixture_observed", "nm_id": 1})

    @unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0, "Linux root required for dropped UID proof")
    def test_root_private_locks_prove_after_uid_and_groups_are_dropped(self):
        from packages.application.owned_history_worker_capability import inherited_lock_proof, linux_required
        try:
            user = pwd.getpwnam("wbchab")
        except KeyError:
            user = pwd.getpwnam("nobody")
        self.root.chmod(0o750)
        def drop_privileges():
            os.setgroups([])
            os.setgid(user.pw_gid)
            os.setuid(user.pw_uid)
        with heavy_admitted(self.root, operation="buyer-authenticated-collect") as owner:
            price._reader_fds(999, self.root)  # Exact canonical-path proof as root.
            # Copy the exact native read-only helper, so a private checkout path
            # cannot accidentally become a prerequisite for this UID test.
            code = "import os,sys,stat,socket,fcntl,tempfile\nfrom pathlib import Path\nHistoryDelegationError=RuntimeError\n"
            code += inspect.getsource(linux_required) + "\n" + inspect.getsource(inherited_lock_proof)
            code += "\n" + inspect.getsource(price._runner_lock_proof)
            code += """
assert os.geteuid()!=0 and os.getgroups()==[]
try: Path(sys.argv[1]).lstat()
except PermissionError: pass
else: raise AssertionError('runtime path unexpectedly traversable')
_runner_lock_proof(int(sys.argv[2]),int(sys.argv[3]),Path(sys.argv[1]).parent)
try: _runner_lock_proof(int(sys.argv[4]),int(sys.argv[3]),Path(sys.argv[1]).parent)
except RuntimeError: pass
else: raise AssertionError('unlocked alternate description accepted')
with tempfile.TemporaryDirectory(prefix='buyer-ownprivate-') as private:
 own=[]
 try:
  for name,mode in [('.business-data-heavy-admission.lock',fcntl.LOCK_EX),('.business-data-procedure-admission.lock',fcntl.LOCK_SH)]:
   fd=os.open(Path(private)/name,os.O_CREAT|os.O_RDWR,0o600);own.append(fd);fcntl.flock(fd,mode)
  try:_runner_lock_proof(*own,Path(private))
  except RuntimeError:pass
  else:raise AssertionError('own private unrelated locks accepted')
 finally:
  for fd in reversed(own):os.close(fd)
print('dropped-uid-private-path-proof-ok')
"""
            # An open of the same inode does not inherit the parent's flock.
            other_fd = os.open(self.root / ".business-data-heavy-admission.lock", os.O_RDONLY)
            try:
                fds = (owner.fd, owner._maintenance.fd, other_fd)
                result = subprocess.run([sys.executable, "-c", code,
                                         str(self.root / ".business-data-heavy-admission.lock"), *map(str, fds)],
                                        cwd="/tmp", pass_fds=fds, preexec_fn=drop_privileges,
                                        capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.strip(), "dropped-uid-private-path-proof-ok")
            finally:
                os.close(other_fd)


CHILD_TRY = """from pathlib import Path
import sys
from packages.application.business_data_heavy_admission import HeavyAdmissionBusy,heavy_admitted
try:
 with heavy_admitted(Path(sys.argv[1]),operation='history'): print('entered')
except HeavyAdmissionBusy: print('busy')
"""

if __name__ == "__main__":
    unittest.main()
