"""Disposable synthetic tests; Linux exercises real FDs/processes, never WB."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from textwrap import dedent
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.application import owned_history_worker as supervisor
from packages.application.owned_history_worker_capability import HistoryDelegationError, lock_proof, linux_required
from packages.application.business_data_heavy_admission import heavy_admitted, heavy_admission_status
from packages.application.business_data_procedure_admission import admission_idle
from packages.application.web_vitrina_snapshot_admission import ApiJobMarkers

NOW = datetime(2026, 4, 20, 12, tzinfo=timezone.utc)
REAL_POPEN = subprocess.Popen


@contextmanager
def ownership(runtime, root, *, seconds=30):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in (".web-vitrina-finished-builder.lock", ".wb-finance-daily-worker.lock"):
        (runtime.runtime_dir / name).touch(mode=0o600)
    markers = ApiJobMarkers(runtime.runtime_dir)
    marker = markers.start("owned-offline", "cycle")
    cycle = {**markers.owner, "job_id": "owned-offline", "operation": "cycle"}
    contract = json.loads((ROOT / "artifacts/registry_upload_http_entrypoint/input/web_vitrina_history_runtime.json").read_text())
    contract["candidate_root"] = str(root)
    contract_path = root / "contract.json"
    contract_path.write_text(json.dumps(contract))
    config = SimpleNamespace(candidate_root=root, runtime_contract=contract_path,
        formula_epoch=contract["formula_epoch"], budget_seconds=seconds, max_recomputes=31)
    with heavy_admitted(runtime.runtime_dir, operation="cycle"), \
            patch("apps.web_vitrina_history_candidate_build.runtime_storage_admission"), \
            patch("apps.web_vitrina_finished_snapshot_build.systemd_admission", return_value="idle"):
        with supervisor.owned_history_worker(runtime=runtime, config=config, cycle_owner=cycle) as worker:
            try:
                yield worker
            finally:
                markers.finish(marker)


def simple_runtime(directory):
    runtime = Path(directory) / "runtime"
    runtime.mkdir(mode=0o700)
    from packages.application.storage_registry import StoreRegistry
    path = StoreRegistry(runtime).resolve("operational")
    path.touch(mode=0o600)
    return SimpleNamespace(runtime_dir=runtime, db_path=path)


def logical_source_digest(path):
    # Physical main-file equality alone would miss a write living in WAL.
    with sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        value = hashlib.sha256()
        for line in conn.iterdump():
            value.update(line.encode())
        return value.hexdigest()


def fake_spawn(code):
    class BodyProcess(REAL_POPEN):
        def __init__(self, command, **kwargs):
            # Only the synthetic test substitutes the fixed worker body.
            assert command[1:3] == ["-m", "apps.web_vitrina_owned_history_worker"]
            super().__init__([sys.executable, "-c", code, command[-1]], **kwargs)
    return BodyProcess


class SupplierPhaseBudgetTests(unittest.TestCase):
    """Exercise the supervisor clock separately from the real supplier/FD integration."""

    def exchange(self, mode, sequence, *, body_deadline=1000, owner_error=None):
        from packages.application.owned_history_worker_capability import CONTRACT
        directory = self.enterContext(tempfile.TemporaryDirectory())
        runtime = simple_runtime(directory)
        config = SimpleNamespace(budget_seconds=240, runtime_contract=ROOT /
            "artifacts/registry_upload_http_entrypoint/input/web_vitrina_history_runtime.json")
        owner = supervisor._OwnedHistoryWorker(runtime=runtime, config=config, cycle_owner=None, lock_fds=())
        owner._completion_deadline = body_deadline
        owner._check = Mock(side_effect=owner_error)
        invocation = "a" * 32
        identity = dict(contract=CONTRACT, invocation=invocation)
        cap = dict(**identity, mode=mode, inner_seconds=235)
        clock, frames, grants = [100.0], iter(sequence), []
        self.grants = grants

        def receive(channel, *, deadline):
            stamp, body = next(frames)
            clock[0] = stamp
            return {**identity, **body}

        with patch.object(supervisor, "time", SimpleNamespace(monotonic=lambda: clock[0])), \
             patch.object(supervisor, "receive_message", side_effect=receive), \
             patch.object(supervisor, "send_message", side_effect=lambda channel, value: grants.append(value)):
            response, deadline = owner._receive_supplier_reply(Mock(), cap, 240, 340, {})
        return response["result"], deadline, grants

    def test_supplier_preparation_leaves_a_full_derived_interval(self):
        result, deadline, grants = self.exchange("portion", [
            (300, {"phase": "derived_work"}), (449, {"result": {"status": "pending"}})])
        self.assertEqual((result["status"], deadline), ("pending", 540))
        self.assertEqual([(g["phase"], g["inner_deadline"]) for g in grants], [("derived_work", 535)])

    def test_child_uses_only_the_bound_parent_deadline(self):
        from apps.web_vitrina_owned_history_worker import _supplier_phase
        from packages.application.owned_history_worker_capability import CONTRACT, receive_message, send_message
        invocation = "a" * 32
        cap = {"invocation": invocation, "inner_seconds": 235}
        request = {"contract": CONTRACT, "invocation": invocation, "phase": "derived_work"}
        now = time.monotonic()
        for deadline, grant_invocation, accepted in [
            (now + 45, invocation, True), (now - 1, invocation, False),
            (now + 300, invocation, False), (now + 45, "b" * 32, False),
        ]:
            parent, child = socket.socketpair()
            with self.subTest(deadline=deadline, identity=grant_invocation), parent, child:
                send_message(parent, {**request, "invocation": grant_invocation, "inner_deadline": deadline})
                if accepted:
                    self.assertEqual(_supplier_phase(child, cap, "derived_work"), deadline)
                else:
                    with self.assertRaises(HistoryDelegationError):
                        _supplier_phase(child, cap, "derived_work")
                self.assertEqual(receive_message(parent), request)

    def test_fragmented_frame_shares_one_deadline(self):
        from packages.application import owned_history_worker_capability as capability
        frame = Mock()
        frame.recv.side_effect = [b"\x00", b"\x00\x00\x02", b"{}"]
        clock = Mock(side_effect=[100, 101, 102, 103])
        with patch.object(capability, "time", SimpleNamespace(monotonic=clock)):
            self.assertEqual(capability.receive_message(frame, deadline=104), {})
        self.assertEqual([call.args[0] for call in frame.settimeout.call_args_list], [4, 3, 2])
        frame = Mock()
        frame.recv.side_effect = [b"\x00", b"\x00\x00\x02"]
        clock = Mock(side_effect=[100, 101, 104])
        with patch.object(capability, "time", SimpleNamespace(monotonic=clock)), self.assertRaises(TimeoutError):
            capability.receive_message(frame, deadline=104)
        self.assertEqual(frame.recv.call_count, 2)

    def test_changed_owner_cannot_grant_the_next_phase(self):
        with self.assertRaisesRegex(HistoryDelegationError, "history_parent_owner_changed"):
            self.exchange("portion", [(300, {"phase": "derived_work"})],
                          owner_error=HistoryDelegationError("history_parent_owner_changed"))
        self.assertEqual(self.grants, [])

    def test_only_terminal_verify_may_claim_the_native_proof_interval(self):
        terminal = {"status": "verified", "terminal": True, "supplier": {"native": {"fixture": {}}}}
        result, end, grants = self.exchange("verify", [
            (250, {"phase": "derived_work"}), (380, {"phase": "supplier_native_proof"}),
            (485, {"result": terminal})], body_deadline=510)
        self.assertEqual((result, end), (terminal, 510))
        self.assertEqual(grants[-1]["inner_deadline"], 505)
        _, end, grants = self.exchange("verify", [
            (330, {"phase": "derived_work"}), (475, {"phase": "supplier_native_proof"}),
            (570, {"result": terminal})])
        self.assertEqual((end, grants[-1]["inner_deadline"]), (580, 575))
        for mode, frames in [
            ("portion", [(170, {"result": {"status": "pending"}})]),
            ("verify", [(170, {"phase": "supplier_native_proof"})]),
            ("portion", [(170, {"phase": "derived_work"}), (180, {"phase": "supplier_native_proof"})]),
            ("verify", [(170, {"phase": "derived_work"}), (180, {"phase": "derived_work"})]),
            ("verify", [(170, {"phase": "derived_work", "invocation": "b" * 32})]),
            ("verify", [(170, {"phase": "derived_work"}), (200, {"result": terminal})]),
            ("verify", [(170, {"phase": "derived_work"}), (180, {"phase": "supplier_native_proof"}),
                        (190, {"result": {"status": "verified", "terminal": False}})]),
        ]:
            with self.subTest(mode=mode, frames=frames), self.assertRaises(HistoryDelegationError):
                self.exchange(mode, frames)

    def test_late_messages_and_exhausted_body_cannot_extend_work(self):
        for frames, body_deadline in [
            ([(341, {"phase": "derived_work"})], 1000),
            ([(200, {"phase": "derived_work"}), (441, {"result": {"status": "pending"}})], 1000),
            ([(338, {"phase": "derived_work"})], 340),
        ]:
            with self.subTest(frames=frames), self.assertRaises(TimeoutError):
                self.exchange("portion", frames, body_deadline=body_deadline)
        failure = {"status": "failed", "reason": "history_worker_binding_changed"}
        result, deadline, grants = self.exchange("capture", [(169, {"result": failure})])
        self.assertEqual((result, deadline, grants), (failure, 340, []))


@unittest.skipUnless(sys.platform == "linux", "Linux kernel FD authority required")
class LinuxWorkerSmoke(unittest.TestCase):
    def test_native_proof_timeout_reaps_the_same_child_at_the_combined_limit(self):
        from packages.application.owned_history_source_watch import HistorySourceWatch
        with tempfile.TemporaryDirectory() as directory:
            runtime = simple_runtime(directory)
            root = Path(directory) / "candidate"
            note = Path(directory) / "phase-grant.json"
            body = dedent(f"""
                import json, os, sys, time
                from pathlib import Path
                from packages.application.owned_history_worker_capability import child_bootstrap
                from apps.web_vitrina_owned_history_worker import _supplier_phase
                channel, cap = child_bootstrap(int(sys.argv[1]))
                time.sleep(1.4)
                _supplier_phase(channel, cap, "derived_work")
                time.sleep(1.4)
                deadline = _supplier_phase(channel, cap, "supplier_native_proof")
                Path({str(note)!r}).write_text(json.dumps([os.getpid(), deadline, time.monotonic()]))
                time.sleep(30)
            """)
            with ownership(runtime, root, seconds=2) as worker, \
                    patch.object(supervisor, "PARENT_GRACE_SECONDS", .3), \
                    patch.object(supervisor, "_POPEN", fake_spawn(body)):
                # The actual supplier fixture verifies typed receipt admission separately.
                worker._supplier_receipt = object()
                worker._completion_deadline = time.monotonic() + 30
                worker._source_watch = HistorySourceWatch(runtime.runtime_dir, runtime.db_path)
                start = time.monotonic()
                result = worker._invoke("verify", NOW.isoformat(), None)
                pid, inner_deadline, granted_at = json.loads(note.read_text())
                self.assertLess(inner_deadline - granted_at, 1.8)
                self.assertGreaterEqual(time.monotonic(), inner_deadline)
                self.assertLess(time.monotonic() - start, 6.2)
                self.assertEqual(result["status"], "outcome_unknown")
                self.assertFalse(result["readback"]["publication_observed"])
                self.assertFalse(Path(f"/proc/{pid}").exists())
                self.assertIsNone(worker._process)
                self.assertFalse(admission_idle(runtime.runtime_dir)["idle"])

    def test_owner_loss_after_preparation_reaps_without_a_grant(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = simple_runtime(directory)
            root = Path(directory) / "candidate"
            prepared = Path(directory) / "prepared"
            granted = Path(directory) / "granted"
            body = dedent(f"""
                import os, sys, time
                from pathlib import Path
                from packages.application.owned_history_worker_capability import child_bootstrap
                from apps.web_vitrina_owned_history_worker import _supplier_phase
                channel, cap = child_bootstrap(int(sys.argv[1]))
                Path({str(prepared)!r}).write_text(str(os.getpid()))
                _supplier_phase(channel, cap, "derived_work")
                Path({str(granted)!r}).touch()
                time.sleep(30)
            """)
            with ownership(runtime, root) as worker, patch.object(supervisor, "_POPEN", fake_spawn(body)):
                worker._supplier_receipt = object()
                worker._completion_deadline = time.monotonic() + 30
                check = worker._check

                def check_owner():
                    check()
                    if prepared.exists():
                        raise HistoryDelegationError("history_parent_owner_changed")

                with patch.object(worker, "_check", side_effect=check_owner), \
                        self.assertRaisesRegex(HistoryDelegationError, "history_parent_owner_changed"):
                    worker.capture(NOW)
                self.assertFalse(granted.exists())
                self.assertFalse(Path("/proc", prepared.read_text()).exists())
                self.assertFalse(admission_idle(runtime.runtime_dir)["idle"])
                self.assertIsNone(worker._process)

    def test_kernel_lock_proof_requires_actual_description(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock"
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            other = os.open(path, os.O_RDONLY)
            try:
                with self.assertRaises(HistoryDelegationError):
                    lock_proof(fd, path, mode="WRITE", parent_pid=os.getpid())
                fcntl.flock(fd, fcntl.LOCK_EX)
                lock_proof(fd, path, mode="WRITE", parent_pid=os.getpid())
                with self.assertRaises(HistoryDelegationError):
                    lock_proof(other, path, mode="WRITE", parent_pid=os.getpid())
            finally:
                os.close(other)
                os.close(fd)

    def test_direct_command_or_regular_fd_is_not_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "not-a-capability"
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                result = subprocess.run([sys.executable, "-m", "apps.web_vitrina_owned_history_worker", str(fd)],
                    cwd=ROOT, pass_fds=(fd,), timeout=10, capture_output=True)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, b"")
                self.assertEqual(list(Path(directory).iterdir()), [path])
            finally:
                os.close(fd)

    def test_journal_start_consumes_existing_history_budget(self):
        from contextlib import contextmanager
        from types import SimpleNamespace
        clock = [100.0]

        @contextmanager
        def delayed_event(*args, **kwargs):
            clock[0] += 10.0
            yield {}

        with tempfile.TemporaryDirectory() as directory:
            runtime = simple_runtime(directory)
            with ownership(runtime, Path(directory) / "candidate", seconds=1.0) as worker, \
                    patch.object(supervisor, "PARENT_GRACE_SECONDS", .1), \
                    patch.object(supervisor, "time", SimpleNamespace(monotonic=lambda: clock[0])), \
                    patch.object(supervisor.journal_log, "span", delayed_event), \
                    patch.object(supervisor, "_POPEN") as spawn:
                with self.assertRaisesRegex(HistoryDelegationError, "history_budget_exhausted_before_spawn"):
                    worker.capture(NOW)
                spawn.assert_not_called()

    def test_hard_cpu_timeout_reaps_before_domain_and_drain_release(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = simple_runtime(directory)
            root = Path(directory) / "candidate"
            code = 'from packages.application.owned_history_worker_capability import child_bootstrap; import sys; c,cap=child_bootstrap(int(sys.argv[1]));\nwhile True: pass'
            with ownership(runtime, root, seconds=.2) as worker, \
                    patch.object(supervisor, "PARENT_GRACE_SECONDS", .1), \
                    patch.object(supervisor, "_POPEN", fake_spawn(code)), \
                    patch.object(supervisor, "send_message", wraps=supervisor.send_message) as sent:
                start = time.monotonic()
                result = worker.capture(NOW)
                self.assertEqual(result["status"], "outcome_unknown")
                self.assertEqual(sent.call_count, 1)
                self.assertNotIn("supplier_phase_protocol", sent.call_args.args[1])
                self.assertLess(time.monotonic() - start, 3)
                self.assertIsNone(worker._process)
                self.assertFalse(admission_idle(runtime.runtime_dir)["idle"])
                with self.assertRaises(HistoryDelegationError):
                    worker.capture(NOW)
                fd = os.open(root / "candidate.lock", os.O_RDONLY)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(fd)
            self.assertTrue(admission_idle(runtime.runtime_dir)["idle"])
            self.assertTrue(heavy_admission_status(runtime.runtime_dir)["idle"])

    def test_interrupt_reaps_before_outer_lease_unwind(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = simple_runtime(directory)
            root = Path(directory) / "candidate"
            pid_file = Path(directory) / "child-pid"
            code = ('from packages.application.owned_history_worker_capability import child_bootstrap; import sys,os,time; '
                    'from pathlib import Path; c,cap=child_bootstrap(int(sys.argv[1])); '
                    f'Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)')
            def interrupt(channel):
                until = time.monotonic() + 5
                while not pid_file.exists() and time.monotonic() < until:
                    time.sleep(.01)
                self.assertTrue(pid_file.exists())
                raise KeyboardInterrupt
            with ownership(runtime, root) as worker, \
                    patch.object(supervisor, "_POPEN", fake_spawn(code)), \
                    patch.object(supervisor, "receive_message", side_effect=interrupt):
                with self.assertRaises(KeyboardInterrupt):
                    worker.capture(NOW)
                child = int(pid_file.read_text())
                self.assertFalse(Path(f"/proc/{child}").exists())
                self.assertFalse(admission_idle(runtime.runtime_dir)["idle"])
                self.assertIsNone(worker._process)

    def test_forked_supervisor_cannot_authorize_or_close_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = simple_runtime(directory)
            with ownership(runtime, Path(directory) / "candidate") as worker:
                pid = os.fork()
                if pid == 0:
                    try:
                        try:
                            worker.capture(NOW)
                        except HistoryDelegationError:
                            pass
                        else:
                            os._exit(3)
                        try:
                            worker.close()
                        except HistoryDelegationError:
                            os._exit(0)
                        os._exit(4)
                    except BaseException:
                        os._exit(5)
                _, status = os.waitpid(pid, 0)
                self.assertEqual(os.waitstatus_to_exitcode(status), 0)
                self.assertFalse(admission_idle(runtime.runtime_dir)["idle"])

    def test_exception_after_spawn_with_handle_is_reaped(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = simple_runtime(directory)
            pids = []
            class InterruptedProcess(REAL_POPEN):
                def __init__(self, command, **kwargs):
                    super().__init__(command, **kwargs)
                    pids.append(self.pid)
                    raise KeyboardInterrupt
            with ownership(runtime, Path(directory) / "candidate") as worker, \
                    patch.object(supervisor, "_POPEN", InterruptedProcess):
                with self.assertRaises(KeyboardInterrupt):
                    worker.capture(NOW)
                self.assertEqual(len(pids), 1)
                self.assertFalse(Path(f"/proc/{pids[0]}").exists())
                self.assertFalse(admission_idle(runtime.runtime_dir)["idle"])

    def test_exception_after_native_spawn_without_handle_uses_kernel_children(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = simple_runtime(directory)
            pids = []
            handles = []
            class LostHandleProcess(REAL_POPEN):
                def __init__(self, command, **kwargs):
                    # Real fork with the same fixed worker and inherited locks,
                    # then an initializer fault before its PID is recorded.
                    process = REAL_POPEN(command, **kwargs)
                    handles.append(process)
                    pids.append(process.pid)
                    self._child_created = False
                    self.returncode = None
                    raise KeyboardInterrupt
            with ownership(runtime, Path(directory) / "candidate") as worker, \
                    patch.object(supervisor, "_POPEN", LostHandleProcess):
                with self.assertRaises(KeyboardInterrupt):
                    worker.capture(NOW)
                self.assertFalse(Path(f"/proc/{pids[0]}").exists())
                self.assertFalse(admission_idle(runtime.runtime_dir)["idle"])
                for process in handles:
                    process.poll()  # Already reaped by the kernel-children recovery.

    def test_exception_after_pid_assignment_before_child_created_is_reaped(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = simple_runtime(directory)
            pids, handles = [], []
            class PartialHandleProcess(REAL_POPEN):
                def __init__(self, command, **kwargs):
                    process = REAL_POPEN(command, **kwargs)
                    handles.append(process)
                    pids.append(process.pid)
                    self.pid = process.pid
                    self._child_created = False
                    self.returncode = None
                    raise KeyboardInterrupt
            with ownership(runtime, Path(directory) / "candidate") as worker, \
                    patch.object(supervisor, "_POPEN", PartialHandleProcess):
                with self.assertRaises(KeyboardInterrupt):
                    worker.capture(NOW)
                self.assertEqual(len(pids), 1)
                self.assertFalse(Path(f"/proc/{pids[0]}").exists())
                self.assertFalse(admission_idle(runtime.runtime_dir)["idle"])
                self.assertIsNone(worker._process)
                self.assertFalse(heavy_admission_status(runtime.runtime_dir)["idle"])
                for process in handles:
                    process.poll()  # Native waitpid already established terminal.
            self.assertTrue(admission_idle(runtime.runtime_dir)["idle"])
            self.assertTrue(heavy_admission_status(runtime.runtime_dir)["idle"])

    def _parent_death(self, before_bootstrap):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "child-pid"
            prefix = 'import os,sys,time; from pathlib import Path; '
            bootstrap = 'from packages.application.owned_history_worker_capability import child_bootstrap; c,cap=child_bootstrap(int(sys.argv[1])); '
            marker = f'Path({str(pid_file)!r}).write_text(str(os.getpid())); '
            child_code = prefix + (marker + 'time.sleep(1); ' + bootstrap if before_bootstrap else bootstrap + marker) + 'time.sleep(30)'
            parent_code = ('from apps.owned_history_worker_smoke import *; '
                f'runtime=simple_runtime({directory!r}); root=Path({directory!r})/"candidate";\n'
                'with ownership(runtime,root) as worker, patch.object(supervisor,"_POPEN",fake_spawn(' + repr(child_code) + ')):\n'
                ' worker.capture(NOW)\n')
            parent = REAL_POPEN([sys.executable, "-c", parent_code], cwd=ROOT,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                until = time.monotonic() + 10
                while not pid_file.exists() and time.monotonic() < until:
                    time.sleep(.01)
                self.assertTrue(pid_file.exists())
                pid = int(pid_file.read_text())
                runtime = Path(directory) / "runtime"
                self.assertFalse(heavy_admission_status(runtime)["idle"])
                os.kill(parent.pid, signal.SIGKILL)
                parent.wait(timeout=5)
                if before_bootstrap:
                    self.assertFalse(admission_idle(runtime)["idle"])
                    self.assertFalse(heavy_admission_status(runtime)["idle"])
                until = time.monotonic() + 5
                while not heavy_admission_status(runtime)["idle"] and time.monotonic() < until:
                    time.sleep(.01)
                self.assertTrue(admission_idle(runtime)["idle"])
                self.assertTrue(heavy_admission_status(runtime)["idle"])
                stat = Path(f"/proc/{pid}/stat")
                if stat.exists():
                    self.assertIn(stat.read_text().split(") ", 1)[1].split()[0], {"Z", "X"})
            finally:
                if parent.poll() is None:
                    os.killpg(parent.pid, signal.SIGKILL)
                    parent.wait()

    def test_parent_death_after_bootstrap_kills_child(self):
        self._parent_death(False)

    def test_parent_death_before_bootstrap_preserves_lock_refs_until_exit(self):
        self._parent_death(True)

    def test_real_capture_and_portion_are_derived_only(self):
        from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer
        from packages.application.ready_publication import ensure_publication_schema
        fixture = LocalWebVitrinaFixtureServer(with_ready_snapshot=True, ready_days=14, now=NOW)
        with fixture, tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            runtime = fixture.entrypoint.runtime
            keeper = stack.enter_context(sqlite3.connect(runtime.db_path))
            keeper.execute("PRAGMA journal_mode=WAL")
            ensure_publication_schema(keeper)
            keeper.commit()
            keeper.execute("SELECT 1 FROM sqlite_master").fetchone()
            before = {path.name: path.read_bytes() for path in runtime.runtime_dir.iterdir()
                      if path.is_file() and not path.name.endswith(("-shm", "-wal"))}
            native_before = logical_source_digest(runtime.db_path)
            with ownership(runtime, Path(directory) / "candidate", seconds=60) as worker:
                capture = worker.capture(NOW)
                self.assertEqual(capture["status"], "complete", capture)
                self.assertEqual(capture["result"]["status"], "captured", capture)
                anchor = capture["result"]["anchor"]
                self.assertEqual(len(anchor["vector"]["dates"]), 14)
                wrong = {**anchor, "fence": "wrong"}
                with self.assertRaises(HistoryDelegationError):
                    worker.portion(wrong)
                result = worker.portion(anchor)
                self.assertEqual(result["status"], "complete", result)
                self.assertIn(result["result"]["status"], {"published", "unchanged"}, result)
                with self.assertRaises(HistoryDelegationError):
                    worker.portion(anchor)
            self.assertEqual(logical_source_digest(runtime.db_path), native_before)
            # One independent invocation detects source drift before writer
            # mutation; it does not adopt a fresh vector or recollect sources.
            current_path = Path(directory) / "candidate/history/CURRENT.json"
            published = current_path.read_bytes()
            with ownership(runtime, Path(directory) / "candidate", seconds=60) as worker:
                frozen = worker.capture(NOW)["result"]["anchor"]
                keeper.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET refreshed_at=? WHERE as_of_date=?",
                    ("2026-04-20T16:00:00Z", "2026-04-20"))
                keeper.commit()
                expected_native = logical_source_digest(runtime.db_path)
                drift = worker.portion(frozen)
                self.assertEqual(drift["result"]["status"], "failed", drift)
                self.assertEqual(drift["result"]["reason"], "history_worker_anchor_changed")
                self.assertEqual(current_path.read_bytes(), published)
                self.assertEqual(logical_source_digest(runtime.db_path), expected_native)
            # Lost response after the real CURRENT switch is UNKNOWN, with an
            # exact published-target observation. It is never submitted again.
            with ownership(runtime, Path(directory) / "candidate", seconds=60) as worker:
                frozen = worker.capture(NOW)["result"]["anchor"]
                receive = supervisor.receive_message
                def lose_response(channel):
                    receive(channel)
                    raise HistoryDelegationError("fixture_lost_terminal_response")
                with patch.object(supervisor, "receive_message", side_effect=lose_response):
                    unknown = worker.portion(frozen)
                self.assertEqual(unknown["status"], "outcome_unknown", unknown)
                self.assertTrue(unknown["readback"]["publication_observed"], unknown)
                self.assertNotEqual(unknown["readback"]["current"]["current"], frozen["base"])
                with self.assertRaises(HistoryDelegationError):
                    worker.portion(frozen)
                self.assertEqual(logical_source_digest(runtime.db_path), expected_native)
            # A transport failure reported by send can likewise arrive after
            # the real child has already published. Do not lose its readback.
            with ownership(runtime, Path(directory) / "send-timeout-candidate", seconds=60) as worker:
                frozen = worker.capture(NOW)["result"]["anchor"]
                send, receive = supervisor.send_message, supervisor.receive_message
                def send_then_timeout(channel, capability):
                    send(channel, capability)
                    receive(channel)  # Real child terminal, including CURRENT.
                    raise TimeoutError("fixture_send_outcome_unknown")
                with patch.object(supervisor, "send_message", side_effect=send_then_timeout):
                    unknown = worker.portion(frozen)
                self.assertEqual(unknown["status"], "outcome_unknown", unknown)
                self.assertTrue(unknown["readback"]["publication_observed"], unknown)
                self.assertIsNone(worker._process)
                self.assertFalse(admission_idle(runtime.runtime_dir)["idle"])
                self.assertFalse(heavy_admission_status(runtime.runtime_dir)["idle"])
                with self.assertRaises(HistoryDelegationError):
                    worker.portion(frozen)
                self.assertEqual(logical_source_digest(runtime.db_path), expected_native)
            self.assertEqual(before, {path.name: path.read_bytes() for path in runtime.runtime_dir.iterdir()
                if path.name in before})


class PlatformSmoke(unittest.TestCase):
    def test_unsupported_platform_refuses_before_work(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("packages.application.owned_history_worker_capability.sys.platform", "darwin"):
            with self.assertRaises(HistoryDelegationError):
                with supervisor.owned_history_worker(runtime=None, config=None, cycle_owner=None):
                    self.fail("unsupported factory entered")
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
