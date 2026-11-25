"""Portable regressions for the input boundary of single-stage replay."""
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

if __package__ in (None, ""):
    sys.path[0] = str(Path(__file__).resolve().parents[2])
from apps.local_cycle_lab import stage_checkpoint as cp
from apps.local_cycle_lab.state import atomic_json


class StageCheckpointTest(unittest.TestCase):
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
