#!/usr/bin/env python3
"""Native SQLite fixtures: all-week Finance validation with zero validation writes."""
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
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
from packages.application.finance_cost_completion_readonly import validate_finance_completion_readonly as validate
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

    def test_current_all_five_images_without_mutators(self):
        result = self.checked()
        self.assertEqual(result["status"], "already_current")
        self.assertTrue(result["read_only_completion_proof"])
        self.assertTrue(result["cost_evidence_complete"])
        self.assertEqual(result["cost_quality"], "complete")
        self.assertEqual(result["checked_week_count"], 1)
        self.assertFalse(result["runtime_mutation"])
        self.assertFalse(result["posting_proof"])

    def test_changed_economic_state_and_missing_sku_fail_closed(self):
        with closing(sqlite3.connect(self.block.db_path)) as conn, conn:
            conn.execute("UPDATE wb_finance_weekly_aggregates SET metrics_json='{}'")
        self.checked(error="business_state_stale")
        self.block.recalculate_week(date.fromisoformat(WEEK[0]), date.fromisoformat(WEEK[1]))
        with closing(sqlite3.connect(self.block.db_path)) as conn, conn:
            conn.execute("DELETE FROM wb_finance_weekly_sku_aggregates")
        self.checked(error="business_state_stale")

    def test_unrelated_snapshot_identity_only_is_not_stale(self):
        # Native already_current does not rewrite the global snapshot ID when
        # this week's actual cost state, details and metrics stay unchanged.
        with closing(sqlite3.connect(self.block.db_path)) as conn, conn:
            for table in ("wb_finance_weekly_cost_coverage", "wb_finance_weekly_sku_aggregates"):
                columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
                for column in ("quality_json", "coverage_json"):
                    if column not in columns: continue
                    path = "$.shared_cost.version_id" if column == "quality_json" else "$.quality.shared_cost.version_id"
                    conn.execute(f"UPDATE {table} SET {column}=json_set({column},?,'prior-global-snapshot') WHERE json_type({column},?) IS NOT NULL", (path,path))
        result = self.checked()
        self.assertEqual(result["status"], "already_current")
        with closing(sqlite3.connect(self.block.db_path)) as conn, conn:
            conn.execute("UPDATE wb_finance_weekly_cost_coverage SET cost_state_hash='wrong'")
        self.checked(error="business_state_stale")

    def test_source_identity_cannot_be_normalized_away(self):
        with closing(sqlite3.connect(self.block.db_path)) as conn, conn:
            conn.execute("UPDATE wb_finance_weekly_cost_coverage SET coverage_json=json_set(coverage_json,'$.quality.shared_cost.policy_date','2000-01-01')")
        self.checked(error="business_state_stale")

    def test_missing_preliminary_raw_and_pins_fail_closed(self):
        self.checked(error="nonempty_weeks", expected_weeks=[])
        self.checked(error="week_scope", expected_weeks=[("2026-09-14", "2026-09-20")])
        self.checked(error="book_changed", expected_book_version="foreign")
        self.checked(error="storage_changed", expected_storage_manifest_sha256="foreign")
        with closing(sqlite3.connect(self.block.db_path)) as conn, conn:
            conn.execute("UPDATE wb_finance_weekly_reports SET row_count=99")
        self.checked(error="report_incomplete")
        with closing(sqlite3.connect(self.block.db_path)) as conn, conn:
            conn.execute("UPDATE wb_finance_weekly_reports SET row_count=1")
            conn.execute("UPDATE wb_finance_weekly_sync SET status='loaded_preliminary'")
        self.checked(error="cost_evidence_incomplete")

    def test_zero_stale_missing_cost_is_not_complete(self):
        for _ in range(2):
            self.block.ingest_week(date.fromisoformat(WEEK[0]), date.fromisoformat(WEEK[1]),
                [_row(1, "2026-09-08", nm_id=2)])
        self.checked(error="cost_evidence_incomplete")

    def test_genuine_applied_management_estimate_preserves_quality(self):
        from apps.vitrina_last_known_cost_smoke import shared_day, save_book
        from packages.application.web_vitrina_management_history import SOURCE
        prior = shared_day("2026-09-08", {"1": ("resolved", "12.5")})
        save_book(self.root, {"2026-09-08": prior})
        self.kw["expected_book_version"] = accounting.load(self.root)[1]
        cell = {"source": SOURCE, "quality_state": "management_estimate", "target_date": "2026-09-09",
            "operation_id": "approved-test", "source_snapshot_id": "last-known-cost:test",
            "source_as_of_date": "2026-09-08", "management_value": "12.5",
            "source_day_version_id": prior["version_id"], "source_digest": prior["rows"]["1"]["source_digest"],
            "evidence": {"selection_method": "latest_same_sku_closed_resolved_on_or_before_target"}}
        with closing(sqlite3.connect(self.block.db_path)) as conn, conn:
            conn.executescript("CREATE TABLE registry_upload_current_state(slot,bundle_version); INSERT INTO registry_upload_current_state VALUES(1,'b'); CREATE TABLE sheet_vitrina_v1_ready_snapshots(bundle_version,plan_json,refreshed_at,as_of_date,snapshot_id);")
            envelope = {'metadata': {'server_cell_presentation': {'SKU:1|our_wb_unit_cost_rub': {'2026-09-09': cell}}}}
            conn.execute("INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES(?,?,?,?,?)", ('b', json.dumps(envelope), 'now','2026-09-09','s'))
        for _ in range(2):
            self.block.ingest_week(date.fromisoformat(WEEK[0]), date.fromisoformat(WEEK[1]),
                [_row(1, "2026-09-09", nm_id=1)])
        result = self.checked()
        self.assertEqual(result["status"], "already_current")
        self.assertEqual(result["cost_quality"], "estimated")
        self.assertFalse(result["cost_evidence_complete"])
        self.assertGreater(result["weeks"][0]["cost_quality"]["estimated_units"], 0)
        # Corrupting applied source provenance invalidates the native overlay.
        cell["source_digest"] = 'foreign'
        with closing(sqlite3.connect(self.block.db_path)) as conn, conn:
            conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?", (json.dumps(envelope),))
        self.checked(error="cost_evidence_incomplete")

    def test_all_candidates_two_weeks_includes_unchanged_week(self):
        next_week = ("2026-09-14", "2026-09-20")
        state = self.book["state"]
        for offset in range(1, 8):
            day = date(2026, 9, 8) + timedelta(days=offset)
            state = evaluate_candidate(state, capture(day.isoformat()))
            state = close_candidate_period(state, day.isoformat(), today=(day + timedelta(days=1)).isoformat())
        self.book["state"] = state
        self.book["shared_days"]["2026-09-15"] = build_shared_cost_day(state, wb("2026-09-15"), "2026-09-15")
        self.kw["expected_book_version"] = accounting._save_book(self.root, self.book,
            expected=self.version, operation_id="second-week-fixture")
        for _ in range(2):
            self.block.ingest_week(date.fromisoformat(next_week[0]), date.fromisoformat(next_week[1]),
                [dict(_row(2, "2026-09-15", nm_id=1), reportId=999)])
        self.checked(error="week_scope")
        result = self.checked(expected_weeks=[WEEK, next_week])
        self.assertEqual(result["checked_week_count"], 2)
        self.assertEqual(result["stale_week_count"], 0)

    def test_split_raw_readonly(self):
        raw = self.root / "raw.sqlite3"
        with closing(sqlite3.connect(self.block.db_path)) as source, source, closing(sqlite3.connect(raw)) as conn, conn:
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
            with closing(sqlite3.connect(path)) as conn, conn:
                table = "finance_raw_schema_meta" if store == "finance_raw" else "finance_operational_schema_meta"
                conn.execute(f"CREATE TABLE {table}(singleton INTEGER PRIMARY KEY,schema_revision TEXT,"
                    "logical_store TEXT,generation_id TEXT,generation_epoch TEXT,source_fingerprint TEXT)")
                conn.execute(f"INSERT INTO {table} VALUES(1,?,?,?,?,?)", (generation.schema_revision,
                    store, generation.generation_id, generation.generation_epoch, manifest.source_fingerprint))
        self.kw["expected_storage_manifest_sha256"] = manifest.manifest_sha256
        result = self.checked()
        self.assertEqual(set(result["sqlite_data_versions"]), {"main", "finance_raw_store"})
        self.assertEqual(result["stale_week_count"], 0)

    def test_concurrent_target_or_book_commit_aborts(self):
        original = WbFinanceWeeklyBlock._build_week_target_projection
        def drift(block, conn, **kwargs):
            result = original(block, conn, **kwargs)
            with closing(sqlite3.connect(block.db_path)) as writer, writer:
                writer.execute("UPDATE wb_finance_weekly_aggregates SET metrics_json='{}'")
            return result
        with patch.object(WbFinanceWeeklyBlock, "_build_week_target_projection", drift):
            with self.assertRaisesRegex(ValueError, "sqlite_drift"):
                validate(self.root, **self.kw)
        def book_drift(block, conn, **kwargs):
            result = original(block, conn, **kwargs)
            with closing(sqlite3.connect(accounting.path(self.root))) as writer, writer:
                writer.execute("UPDATE accounting_current SET version='foreign'")
            return result
        with patch.object(WbFinanceWeeklyBlock, "_build_week_target_projection", book_drift):
            with self.assertRaisesRegex(ValueError, "book_changed"):
                validate(self.root, **self.kw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
