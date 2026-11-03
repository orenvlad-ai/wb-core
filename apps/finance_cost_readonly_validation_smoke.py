#!/usr/bin/env python3
"""Native SQLite fixtures: all-week Finance validation with zero validation writes."""
from contextlib import closing
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.wb_finance_weekly_cost_cutover_smoke import _row, _seed_sources
from apps.shared_sku_cost_smoke import fbs, wb, capture
from packages.application import fbs_accounting_runtime as accounting
from packages.application.fbs_snapshot_cost import evaluate_candidate, close_candidate_period
from packages.application.shared_sku_cost import build_shared_cost_day
from packages.application.finance_cost_readonly_validation import validate_finance_cost_weeks_readonly as validate
from packages.application.storage_registry import build_manifest, atomic_write_manifest
from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock

WEEK = ("2026-09-07", "2026-09-13")
AUDIT = []


def files(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def contents(root):
    result = {}
    for p in sorted(root.rglob("*.sqlite3")):
        with closing(sqlite3.connect(p.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.execute("PRAGMA query_only=ON")
            result[str(p.relative_to(root))] = hashlib.sha256(
                "\n".join(conn.iterdump()).encode()).hexdigest()
    return result


class ValidationSmoke(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="finance-readonly-native-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.block = WbFinanceWeeklyBlock(self.root,
            now_factory=lambda: datetime(2026, 9, 14, tzinfo=timezone.utc))
        self.block.ensure_schema()
        _seed_sources(self.block.db_path)
        state = evaluate_candidate(fbs(), capture("2026-09-08"))
        state = close_candidate_period(state, "2026-09-08", today="2026-09-09")
        period = build_shared_cost_day(state, wb("2026-09-08"), "2026-09-08")
        self.book = {"schema": accounting.SCHEMA, "active": True,
            "effective_date": "2026-09-07", "state": state,
            "shared_days": {"2026-09-08": period}, "wb_days": {},
            "retained_days": {}, "presentations": {}}
        self.version = accounting._save_book(self.root, self.book, expected=None,
                                             operation_id="synthetic-fixture-only")
        self.block.ingest_week(date.fromisoformat(WEEK[0]), date.fromisoformat(WEEK[1]),
                               [_row(1, "2026-09-08", nm_id=1)])
        self.block.ingest_week(date.fromisoformat(WEEK[0]), date.fromisoformat(WEEK[1]),
                               [_row(1, "2026-09-08", nm_id=1)])
        self.kw = dict(expected_book_version=self.version,
            expected_storage_manifest_sha256=self.block.store_registry.load().manifest_sha256,
            expected_weeks=[WEEK])

    def checked(self, *, error=None, **kwargs):
        before, content_before = files(self.root), contents(self.root)
        # Writer entrypoints/admission must never be touched during validation.
        with patch.object(WbFinanceWeeklyBlock, "ensure_schema", side_effect=AssertionError("schema write")), \
             patch.object(WbFinanceWeeklyBlock, "apply_stale_cost_weeks", side_effect=AssertionError("apply")), \
             patch("packages.application.wb_finance_weekly.heavy_admitted", side_effect=AssertionError("admission")):
            if error:
                with self.assertRaisesRegex((ValueError, sqlite3.Error), error):
                    validate(self.root, **{**self.kw, **kwargs})
                result = {"expected_failure": error}
            else:
                result = validate(self.root, **{**self.kw, **kwargs})
        self.assertEqual(before, files(self.root))
        self.assertEqual(content_before, contents(self.root))
        AUDIT.append({"case": self.id(), "files_before": before,
                      "files_after": files(self.root), "contents_before": content_before,
                      "contents_after": contents(self.root), "receipt": result})
        return result

    def test_unchanged_all_weeks_and_cli(self):
        result = self.checked()
        self.assertEqual(result["status"], "validated_readonly")
        self.assertEqual(result["stale_week_count"], 0)
        self.assertTrue(result["cost_evidence_complete"])
        self.assertFalse(result["posting_proof"])
        self.assertFalse(result["eligible_for_completion"])
        before = files(self.root)
        process = subprocess.run([sys.executable, str(ROOT / "apps/finance_cost_readonly_validation.py"),
            "--offline-runtime-copy", str(self.root), "--expected-book-version", self.version,
            "--expected-storage-manifest-sha256", self.kw["expected_storage_manifest_sha256"],
            "--expected-week", ":".join(WEEK)], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        self.assertEqual(json.loads(process.stdout)["stale_week_count"], 0)
        self.assertEqual(before, files(self.root))

    def test_stale_metrics_and_sku_images(self):
        with sqlite3.connect(self.block.db_path) as conn:
            conn.execute("UPDATE wb_finance_weekly_aggregates SET metrics_json='{}'")
            conn.execute("DELETE FROM wb_finance_weekly_sku_aggregates")
        result = self.checked()
        self.assertEqual(result["stale_week_count"], 1)
        self.assertIn("wb_finance_weekly_sku_aggregates", result["weeks"][0]["changed_tables"])

    def test_stale_cost_and_raw_changes(self):
        with sqlite3.connect(self.block.db_path) as conn:
            conn.execute("UPDATE wb_finance_weekly_cost_coverage SET cost_state_hash='stale'")
        self.assertEqual(self.checked()["stale_week_count"], 1)
        with sqlite3.connect(self.block.db_path) as conn:
            row = json.loads(conn.execute("SELECT raw_json FROM wb_finance_weekly_raw_rows").fetchone()[0])
            row["forPay"] = "1234"
            conn.execute("UPDATE wb_finance_weekly_raw_rows SET raw_json=?", (json.dumps(row),))
        self.checked(error="raw_hash_mismatch")
        # Valid, newly acknowledged raw source must also alter recomputation.
        digest = self.block._row_hash(row)
        week_hash = hashlib.sha256(digest.encode()).hexdigest()
        with sqlite3.connect(self.block.db_path) as conn:
            conn.execute("UPDATE wb_finance_weekly_raw_rows SET row_hash=?", (digest,))
            conn.execute("UPDATE wb_finance_weekly_reports SET content_hash=?", (week_hash,))
            conn.execute("UPDATE wb_finance_weekly_sync SET content_hash=?", (week_hash,))
        self.assertEqual(self.checked()["stale_week_count"], 1)

    def test_empty_missing_extra_scope_and_incomplete_sources(self):
        self.checked(error="nonempty_weeks", expected_weeks=[])
        self.checked(error="week_scope", expected_weeks=[("2026-09-14", "2026-09-20")])
        with sqlite3.connect(self.block.db_path) as conn:
            conn.execute("UPDATE wb_finance_weekly_reports SET row_count=99")
        self.checked(error="report_incomplete")
        with sqlite3.connect(self.block.db_path) as conn:
            conn.execute("DELETE FROM wb_finance_weekly_raw_rows")
        self.checked(error="evidence_missing")

    def test_pinned_versions_and_missing_book(self):
        self.checked(error="book_changed", expected_book_version="foreign")
        self.checked(error="storage_changed", expected_storage_manifest_sha256="foreign")
        accounting.path(self.root).unlink()
        with self.assertRaises(FileNotFoundError):
            validate(self.root, **self.kw)

    def test_incomplete_quality_never_becomes_complete(self):
        # A native cost period can be arithmetically resolved yet preliminary.
        self.book["shared_days"]["2026-09-08"]["status"] = "preliminary"
        period = build_shared_cost_day(evaluate_candidate(fbs(), capture("2026-09-08")),
                                       wb("2026-09-08"), "2026-09-08")
        self.book["shared_days"]["2026-09-08"] = period
        accounting.path(self.root).unlink()
        self.kw["expected_book_version"] = accounting._save_book(
            self.root, self.book, expected=None, operation_id="preliminary-fixture")
        self.block.recalculate_week(date.fromisoformat(WEEK[0]), date.fromisoformat(WEEK[1]))
        result = self.checked()
        self.assertEqual(result["stale_week_count"], 0)
        self.assertEqual(result["status"], "validation_incomplete")
        self.assertFalse(result["cost_evidence_complete"])

    def test_split_raw_readonly(self):
        raw = self.root / "raw.sqlite3"
        with sqlite3.connect(self.block.db_path) as source, sqlite3.connect(raw) as conn:
            schema = source.execute("SELECT sql FROM sqlite_master WHERE name='wb_finance_weekly_raw_rows'").fetchone()[0]
            conn.execute(schema)
            rows = source.execute("SELECT * FROM wb_finance_weekly_raw_rows").fetchall()
            conn.executemany("INSERT INTO wb_finance_weekly_raw_rows VALUES(" + ",".join("?" * len(rows[0])) + ")", rows)
            conn.execute("CREATE VIEW finance_raw_current_rows AS SELECT * FROM wb_finance_weekly_raw_rows")
            source.execute("DELETE FROM wb_finance_weekly_raw_rows")
        manifest = build_manifest(state="cutover", canonical_source="split", generation_epoch="fixture-1",
            raw_generation_id="raw-1", raw_relative_path=raw.name, raw_watermark="fixture",
            operational_generation_id="operational-1", operational_relative_path=self.block.db_path.name,
            operational_watermark="fixture", rollback_generation_id="monolith",
            source_fingerprint="sha256:" + "b" * 64)
        atomic_write_manifest(self.block.store_registry.manifest_path, manifest)
        for path, store, generation in ((raw, "finance_raw", manifest.raw),
                (self.block.db_path, "operational", manifest.operational)):
            with sqlite3.connect(path) as conn:
                table = "finance_raw_schema_meta" if store == "finance_raw" else "finance_operational_schema_meta"
                conn.execute(f"CREATE TABLE {table}(singleton INTEGER PRIMARY KEY,schema_revision TEXT,"
                    "logical_store TEXT,generation_id TEXT,generation_epoch TEXT,source_fingerprint TEXT)")
                conn.execute(f"INSERT INTO {table} VALUES(1,?,?,?,?,?)", (generation.schema_revision,
                    store, generation.generation_id, generation.generation_epoch, manifest.source_fingerprint))
        self.kw["expected_storage_manifest_sha256"] = manifest.manifest_sha256
        result = self.checked()
        self.assertEqual(set(result["sqlite_data_versions"]), {"main", "finance_raw_store"})
        self.assertEqual(result["stale_week_count"], 0)

    def test_all_candidates_two_weeks_with_one_stale(self):
        next_week = ("2026-09-14", "2026-09-20")
        state = self.book["state"]
        for offset in range(1, 8):
            day = date(2026, 9, 8) + timedelta(days=offset)
            state = evaluate_candidate(state, capture(day.isoformat()))
            state = close_candidate_period(state, day.isoformat(), today=(day + timedelta(days=1)).isoformat())
        self.book["state"] = state
        self.book["shared_days"]["2026-09-15"] = build_shared_cost_day(
            state, wb("2026-09-15"), "2026-09-15")
        self.kw["expected_book_version"] = accounting._save_book(self.root,
            self.book, expected=self.version, operation_id="second-week-fixture")
        for _ in range(2):
            self.block.ingest_week(date.fromisoformat(next_week[0]), date.fromisoformat(next_week[1]),
                [dict(_row(2, "2026-09-15", nm_id=1), reportId=999)])
        # Omitting an unchanged week must fail, even though only the first is stale.
        with sqlite3.connect(self.block.db_path) as conn:
            conn.execute("UPDATE wb_finance_weekly_aggregates SET metrics_json='{}' WHERE week_start=?", (WEEK[0],))
        self.checked(error="week_scope")
        result = self.checked(expected_weeks=[WEEK, next_week])
        self.assertEqual(result["checked_week_count"], 2)
        self.assertEqual([r["stale"] for r in result["weeks"]], [True, False])
        self.assertEqual(result["stale_week_count"], 1)

    def test_zero_stale_missing_cost_stays_incomplete(self):
        for _ in range(2):
            self.block.ingest_week(date.fromisoformat(WEEK[0]), date.fromisoformat(WEEK[1]),
                [_row(1, "2026-09-08", nm_id=2)])
        result = self.checked()
        self.assertEqual(result["stale_week_count"], 0)
        self.assertEqual(result["status"], "validation_incomplete")
        self.assertFalse(result["cost_evidence_complete"])
        self.assertGreater(result["weeks"][0]["cost_quality"]["unmatched_units"], 0)

    def test_concurrent_target_and_book_drift(self):
        original = WbFinanceWeeklyBlock._build_week_target_projection
        def drift(block, conn, **kwargs):
            result = original(block, conn, **kwargs)
            with sqlite3.connect(block.db_path) as writer:
                writer.execute("UPDATE wb_finance_weekly_aggregates SET metrics_json='{}'")
            return result
        with patch.object(WbFinanceWeeklyBlock, "_build_week_target_projection", drift):
            with self.assertRaisesRegex(ValueError, "sqlite_drift"):
                validate(self.root, **self.kw)
        # Fault injection is the only writer; validator must report no receipt.
        def book_drift(block, conn, **kwargs):
            result = original(block, conn, **kwargs)
            with sqlite3.connect(accounting.path(self.root)) as writer:
                writer.execute("UPDATE accounting_current SET version='foreign'")
            return result
        with patch.object(WbFinanceWeeklyBlock, "_build_week_target_projection", book_drift):
            with self.assertRaisesRegex(ValueError, "book_changed"):
                validate(self.root, **self.kw)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ValidationSmoke)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if os.environ.get("FINANCE_VALIDATION_AUDIT_DIR"):
        output = Path(os.environ["FINANCE_VALIDATION_AUDIT_DIR"])
        output.mkdir(parents=True, exist_ok=True)
        (output / "native-fixture-validation.json").write_text(json.dumps({
            "fixture_provenance": "Synthetic native fbs/shared-cost fixtures + native ingest in TemporaryDirectory; no production inputs",
            "tests_run": result.testsRun, "successful": result.wasSuccessful(),
            "actual_583_validation": "not_performed_missing_complete_offline_finance_inputs",
            "cases": AUDIT}, indent=2))
    sys.exit(0 if result.wasSuccessful() else 1)
