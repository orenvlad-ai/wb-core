"""Portable regressions for the input boundary of single-stage replay."""
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

if __package__ in (None, ""):
    sys.path[0] = str(Path(__file__).resolve().parents[2])
from apps.local_cycle_lab import full_history_vm, stage_checkpoint as cp
from apps.local_cycle_lab.state import atomic_json


class StageCheckpointTest(unittest.TestCase):
    def test_source_sync_uses_git_modes_on_private_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            lab = root / 'apps/local_cycle_lab'
            lab.mkdir(parents=True)
            for name in ('worker.py', 'command', 'new_probe.py', 'private.env'):
                path = lab / name
                path.write_text(name)
                path.chmod(0o600)
            def git(*args):
                subprocess.run(['git', *args], cwd=root, check=True, capture_output=True)
            git('init', '-q')
            git('add', 'apps/local_cycle_lab/worker.py', 'apps/local_cycle_lab/command')
            git('update-index', '--chmod=+x', 'apps/local_cycle_lab/command')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'source')
            archive = root / 'source.tgz'
            with patch.object(full_history_vm, 'ROOT', root), patch.object(full_history_vm, 'guest'):
                full_history_vm.sync_source(archive=archive)
            with tarfile.open(archive) as bundle:
                modes = {item.name: item.mode for item in bundle if item.name.startswith('apps/')}
            self.assertEqual(modes, {'apps/local_cycle_lab/worker.py': 0o644,
                                     'apps/local_cycle_lab/command': 0o755,
                                     'apps/local_cycle_lab/new_probe.py': 0o644})
            self.assertEqual((lab / 'worker.py').stat().st_mode & 0o777, 0o600)

    def test_live_wal_and_sealed_database(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime, clone = root / "runtime", root / "copy"
            runtime.mkdir()
            with closing(sqlite3.connect(runtime / "live.sqlite3")) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("CREATE TABLE entries (amount INTEGER)")
                conn.execute("INSERT INTO entries VALUES (12345)")
                conn.commit()
                sealed = runtime / "backups" / "old.sqlite3"
                sealed.parent.mkdir()
                with closing(sqlite3.connect(sealed)) as archive:
                    conn.backup(archive)
                cp.copy_inputs(runtime, clone)
                conn.execute("DELETE FROM entries")
                conn.commit()
            with closing(sqlite3.connect((clone / "live.sqlite3").as_uri() + "?mode=ro", uri=True)) as copied:
                self.assertEqual([(12345,)], copied.execute("SELECT amount FROM entries").fetchall())
            self.assertEqual(cp.file_hash(sealed), cp.file_hash(clone / "backups/old.sqlite3"))
            self.assertFalse((clone / "live.sqlite3-wal").is_file() and (clone / "live.sqlite3-wal").stat().st_size > 0)

    def test_bad_checkpoint_stops_before_restore(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reference, target, history = root / "ref", root / "attempt", root / "history"
            point = reference / "control/checkpoints/rolling14"
            (point / "runtime").mkdir(parents=True)
            (point / "history").mkdir()
            shutil.copy2(cp.SCENARIO, point / "scenario.json")
            sample = point / "runtime" / "input.json"
            sample.write_text("original")
            manifest = dict(schema=cp.FORMAT, stage="rolling14", runtime=str(target / "runtime"),
                            candidate=str(history), files=cp.inventory(point))
            atomic_json(point / "manifest.json", manifest)
            sample.write_text("changed")
            with patch.object(cp, "REFERENCE", reference), patch.object(cp, "CONTROL", target), patch.object(cp, "history_root", return_value=history):
                with self.assertRaisesRegex(ValueError, "checksum"):
                    cp.restore("rolling14")
                self.assertFalse(target.exists())
                self.assertFalse(history.exists())
                sample.write_text("original")
                cp.restore("rolling14")
                self.assertEqual("original", (target / "runtime/input.json").read_text())
                with self.assertRaises(FileExistsError):
                    cp.restore("rolling14")


if __name__ == "__main__":
    unittest.main()
