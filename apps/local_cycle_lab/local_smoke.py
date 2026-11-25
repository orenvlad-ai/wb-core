"""Portable lifecycle/receipt checks; macOS OS probes run in the real launcher."""
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.local_cycle_lab.local import StageController
from apps.local_cycle_lab.state import LabState, atomic_json, read_json


class LocalLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="wbc-local-lifecycle-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.scenario = self.root / "fixture.json"
        self.scenario.write_text('{"today":"2026-11-24","catalog":[]}')
        self.state = LabState(self.root / "lab")
        self.before = self.state.open(self.scenario, "normal")
        self.state.runtime.mkdir()
        (self.state.runtime / "manual-document.txt").write_text("saved manual source")
        (self.state.root / "runs" / "keep.json").write_text("saved stage result")

    def test_reopen_preserves_manual_data_and_refuses_other_fixture(self):
        self.assertEqual(self.state.open(self.scenario, "normal"), self.before)
        self.assertEqual((self.state.runtime / "manual-document.txt").read_text(), "saved manual source")
        with self.assertRaisesRegex(ValueError, "отличаются"):
            self.state.open(self.scenario, "missing")
        foreign = self.root / "foreign"
        foreign.mkdir()
        (foreign / "keep").write_text("untouched")
        with self.assertRaises(ValueError):
            LabState(foreign).open(self.scenario, "normal")
        self.assertEqual([p.name for p in foreign.iterdir()], ["keep"])

    def test_inherited_lifetime_lock_prevents_reset_after_supervisor_descriptor_closes(self):
        child = None
        try:
            with self.state.writer() as fd:
                child = subprocess.Popen([sys.executable, "-u", "-c",
                    "import os,sys,time; os.fstat(int(sys.argv[1])); print('ready'); time.sleep(10)", str(fd)],
                    pass_fds=(fd,), stdout=subprocess.PIPE, text=True)
                self.assertEqual(child.stdout.readline().strip(), "ready")
            # Simulates supervisor death: only the app holds the original open description.
            with self.assertRaisesRegex(ValueError, "второй writer"):
                with LabState(self.state.root).writer():
                    self.fail("live app lock was lost")
            self.assertEqual((self.state.runtime / "manual-document.txt").read_text(), "saved manual source")
        finally:
            if child is not None:
                child.terminate()
                child.wait(timeout=5)
                child.stdout.close()
        with self.state.writer():
            self.assertTrue(self.state.runtime.exists())

    def test_reset_requires_exclusive_writer_and_exact_id(self):
        with self.assertRaisesRegex(ValueError, "эксклюзивной"):
            self.state.reset(self.before["id"])
        with self.state.writer():
            with self.assertRaisesRegex(ValueError, "точный ID"):
                self.state.reset("another-lab")
            archive = self.state.reset(self.before["id"])
        self.assertEqual((archive / "runtime" / "manual-document.txt").read_text(), "saved manual source")
        self.assertEqual((archive / "runs" / "keep.json").read_text(), "saved stage result")
        self.assertFalse(self.state.runtime.exists())
        self.assertNotEqual(self.state.identity()["generation"], self.before["generation"])

    def test_reset_recovers_after_ambiguous_move_without_losing_or_moving_data_twice(self):
        import os
        original = os.rename
        calls = 0
        def lost_reply(source, target):
            nonlocal calls
            original(source, target)
            calls += 1
            if calls == 1:
                raise OSError("lost after the runtime move")
        with self.state.writer():
            with patch("apps.local_cycle_lab.state.os.rename", side_effect=lost_reply):
                with self.assertRaises(OSError):
                    self.state.reset(self.before["id"])
        self.assertTrue((self.state.root / "reset-transaction.json").exists())
        reopened = LabState(self.state.root)
        reopened.open(self.scenario, "normal")
        with reopened.writer():
            archive = reopened.recover_reset()
            self.assertIsNone(reopened.recover_reset())
        self.assertEqual((archive / "runtime" / "manual-document.txt").read_text(), "saved manual source")
        self.assertEqual((archive / "runs" / "keep.json").read_text(), "saved stage result")
        self.assertFalse(reopened.runtime.exists())

    def test_reset_recovers_when_both_moves_finished_before_generation_commit(self):
        original = __import__("os").rename
        calls = 0
        def lost_reply(source, target):
            nonlocal calls
            original(source, target)
            calls += 1
            if calls == 2:
                raise OSError("lost after the runs move")
        with self.state.writer():
            with patch("apps.local_cycle_lab.state.os.rename", side_effect=lost_reply):
                with self.assertRaises(OSError):
                    self.state.reset(self.before["id"])
        reopened = LabState(self.state.root)
        reopened.open(self.scenario, "normal")
        with reopened.writer():
            archive = reopened.recover_reset()
        self.assertEqual((archive / "runs" / "keep.json").read_text(), "saved stage result")
        self.assertFalse(reopened.runtime.exists())

    def test_committed_generation_recovers_without_archiving_new_working_data(self):
        original = Path.unlink
        def lost_cleanup(path, *args, **kwargs):
            if path.name == "reset-transaction.json":
                raise OSError("lost after generation commit")
            return original(path, *args, **kwargs)
        with self.state.writer():
            with patch.object(Path, "unlink", lost_cleanup):
                with self.assertRaises(OSError):
                    self.state.reset(self.before["id"])
        self.state.runtime.mkdir()
        (self.state.runtime / "new.txt").write_text("new generation")
        with self.state.writer():
            archive = self.state.recover_reset()
        self.assertEqual((self.state.runtime / "new.txt").read_text(), "new generation")
        self.assertEqual((archive / "runtime" / "manual-document.txt").read_text(), "saved manual source")

    def test_interrupted_stage_is_read_back_and_never_automatically_restarted(self):
        folder = self.state.root / "runs" / "attempt-1"
        folder.mkdir()
        atomic_json(folder / "attempt.json", dict(status="running", stage="sources_ready", run_id="attempt-1"))
        (folder / "state.sqlite3").write_bytes(b"preserved input image")
        controller = StageController(self.state, json.loads(self.scenario.read_text()), "original-code")
        self.assertEqual(controller.status()["attempts"][0]["status"], "interrupted")
        self.assertIsNone(controller.thread)
        self.assertEqual((folder / "state.sqlite3").read_bytes(), b"preserved input image")
        with patch("apps.local_cycle_lab.local.code_digest", return_value="changed-code"):
            with self.assertRaisesRegex(ValueError, "Код изменился"):
                controller.start()
        self.assertEqual(len(controller.status()["attempts"]), 1)


if __name__ == "__main__":
    unittest.main()
