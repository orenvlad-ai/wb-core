#!/usr/bin/env python3
"""Opt-in shared SKU cost through the real Finance and active Partner routes."""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
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

from apps.fbs_snapshot_cost_smoke import capture
from apps.partner_report_smoke import (
    TARGET_NM, WEEK_ONE, _sale, _seed_ads, _seed_sources, _settings,
)
from packages.application.canonical_wb_cost_resolver import (
    CanonicalChannelCostSnapshot, resolve_channel_location_cost,
)
from packages.application.fbs_snapshot_cost import (
    close_candidate_period, evaluate_candidate, fingerprint, initialize_candidate,
)
from packages.application.partner_report import PartnerReportBlock, PartnerReportError
from packages.application.shared_sku_cost import (
    SharedSkuCostSnapshot, SharedSkuCostStore, build_shared_cost_day,
)
from packages.application.storage_registry import atomic_write_manifest, build_manifest
from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock


DAY = WEEK_ONE + timedelta(days=1)
END = WEEK_ONE + timedelta(days=6)
NOW = lambda: datetime(2026, 7, 20, 12, tzinfo=timezone.utc)


def shared_periods(prices: dict[date, str]) -> list[dict]:
    """Build actual versioned periods, using a fixed 10-unit FBS opening."""
    def image(day: date) -> dict:
        result = capture(day.isoformat(), quantity="10", wac="100")
        result["quantity_snapshot"]["rows"][0]["nm_id"] = TARGET_NM
        result["quantity_snapshot"]["digest"] = fingerprint(result["quantity_snapshot"]["rows"])
        result["baseline_costs"]["rows"][0]["nm_id"] = TARGET_NM
        result["source_digest"] = fingerprint(result)
        return result

    first, last = min(prices), max(prices)
    state = initialize_candidate(image(first))
    result = []
    current = first
    while current <= last:
        if current != first:
            state = evaluate_candidate(state, image(current))
            state = close_candidate_period(state, current.isoformat(), today=(current + timedelta(days=1)).isoformat())
        if current in prices:
            # FBS: 10 * 100; WB: 10 * (2 * requested shared price - 100).
            source = {
                "contract": "shared_sku_cost_wb_source_v1",
                "business_date": current.isoformat(), "complete": True,
                "version_id": "wb-" + prices[current],
                "rows": [{"nm_id": TARGET_NM, "quantity": "10",
                          "capital_rub": str(20 * Decimal(prices[current]) - 1000)}],
            }
            source["source_digest"] = fingerprint(source)
            result.append(build_shared_cost_day(state, source, current.isoformat()))
        current += timedelta(days=1)
    return result


def sale(identity: int, day: date = DAY, **changes) -> dict:
    return {**_sale(identity, day, TARGET_NM, revenue="1000", for_pay="900", acquiring="0"), **changes}


class SharedCostReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="shared-cost-reports-")
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name)
        self.active = PartnerReportBlock(self.runtime, seller_id="seller-1", now_factory=NOW)
        self.active.ensure_schema()
        _seed_sources(self.active.db_path)
        _seed_ads(self.active.db_path)
        self.active.save_settings(_settings(), actor="smoke")
        self.payload = {"nm_id": str(TARGET_NM), "selected_weeks": [WEEK_ONE.isoformat()]}

    def ingest(self, rows):
        self.active.finance.ingest_week(WEEK_ONE, END, rows)

    def candidate(self, prices=None, *, snapshot=None, effective_date=DAY):
        if snapshot is None:
            periods = shared_periods(prices) if prices else []
            snapshot = SharedSkuCostSnapshot(periods, effective_date=effective_date.isoformat())
        return PartnerReportBlock(self.runtime, seller_id="seller-1", now_factory=NOW,
                                  shared_cost_snapshot=snapshot)

    def read_candidate(self, block):
        finance = block.finance.preview_candidate_week(WEEK_ONE, END)
        partner = block.preview(self.payload)
        sku = next(p for p in finance["sku_projections"] if p["nm_id"] == str(TARGET_NM))
        coverage = json.loads(sku["coverage_json"])
        self.assertEqual(coverage["cost_state_hash"], partner["weeks"][0]["coverage"]["cost"]["cost_state_hash"])
        self.assertEqual(json.loads(sku["metrics_json"])["cogs"], partner["weeks"][0]["values"]["cogs"])
        return finance, partner, coverage

    def test_same_operations_prices_signed_cogs_and_provenance(self):
        next_day = DAY + timedelta(days=1)
        rows = [sale(1, quantity=2, deliveryType="FBO"),
                sale(2, quantity=3, deliveryType="FBS", rid="unmatched-observer"),
                sale(3, next_day, docTypeName="Возврат", saleDt="2026-01-01"),
                sale(4, next_day, quantity=-1),
                sale(5, next_day, docTypeName="Возврат", quantity=-1),
                sale(6, next_day, docTypeName="", quantity=0, additionalPayment="37")]
        self.ingest(rows)
        candidate = self.candidate({DAY: "100", next_day: "200"})
        before = self.active.db_path.read_bytes()
        # Both the independent Finance classifier and Partner revalidation
        # must avoid all legacy price and Lifecycle observer loading.
        with patch.object(CanonicalChannelCostSnapshot, "from_connection", side_effect=AssertionError("legacy cost read")):
            finance, partner, coverage = self.read_candidate(candidate)
            with closing(candidate.finance._connect()) as conn, conn:
                detailed = candidate.finance._calculate_cogs(conn, rows, WEEK_ONE, include_details=True)
            with closing(sqlite3.connect(":memory:")) as empty:
                resolved = resolve_channel_location_cost(empty, nm_id=str(TARGET_NM), operation_date=DAY,
                                                        shared_cost_snapshot=candidate.finance.shared_cost_snapshot)
        self.assertEqual(self.active.db_path.read_bytes(), before)
        self.assertEqual(finance["aggregate"]["cogs"], "300.0000")  # 5*100 - 200 - 200 + 200
        self.assertEqual(finance["aggregate"]["corrections"], "37.0000")  # positive additionalPayment is a WB charge
        self.assertEqual(resolved["unit_cost_rub"], "100")
        self.assertEqual([d["signed_cogs_rub"] for d in detailed["detail_rows"]],
                         ["200.0000", "300.0000", "-200.0000", "-200.0000", "200.0000"])
        self.assertEqual(detailed["detail_rows"][2]["operation_date"], next_day.isoformat())
        self.assertEqual(finance["cost_coverage"]["cost_state_hash"], coverage["cost_state_hash"])
        self.assertEqual(coverage["quality"]["source_units"]["shared_sku_daily"], 8)
        self.assertEqual(coverage["quality"]["fbs_pooled_physical_units"], 0)
        for detail in coverage["detail_rows"]:
            self.assertEqual(detail["channel"], "COMMON")
            self.assertEqual(detail["pool"], "WB+FBS+FBO")
            self.assertEqual(detail["formula_version"], candidate.finance.shared_cost_snapshot.formula_version)
        self.assertTrue(partner["candidate_only"])
        self.assertEqual(partner["shared_cost"], finance["shared_cost"])
        self.assertEqual(partner["status"], "ready")

    def test_missing_cost_and_partial_coverage_never_create_zero_profit(self):
        self.ingest([sale(1)])
        missing = self.candidate()
        with patch.object(CanonicalChannelCostSnapshot, "from_connection", side_effect=AssertionError("legacy fallback")):
            finance, partner, coverage = self.read_candidate(missing)
        self.assertIsNone(finance["aggregate"]["cogs"])
        self.assertIsNone(finance["aggregate"]["profit_after_cogs"])
        self.assertIsNone(partner["weeks"][0]["values"]["net_profit"])
        self.assertEqual(coverage["problem_skus"][0]["reason"], "shared_cost_exact_date_missing")
        self.assertEqual(coverage["problem_skus"][0]["source"], "shared_sku_daily_cost")
        self.ingest([sale(1), sale(2, DAY + timedelta(days=1))])
        finance, partner, coverage = self.read_candidate(self.candidate({DAY: "100"}))
        self.assertEqual(finance["aggregate"]["profit_revenue_covered"], "1000.0000")
        self.assertEqual(finance["aggregate"]["profit_revenue_uncovered"], "1000.0000")
        self.assertEqual(partner["weeks"][0]["values"]["cogs"], "100.0000")
        values = partner["weeks"][0]["values"]
        self.assertEqual(values["net_revenue"], "2000.0000")
        self.assertEqual(values["sales_without_cost_rub"], "1000.0000")
        self.assertEqual(values["orders_without_cost"], "1.0000")
        self.assertEqual(values["estimated_tax"], "60.0000")  # 6% of the covered 1000 only
        # Covered revenue1000 - COGS100 - commission200 - ads30904
        # - office10000 - tax60; uncovered revenue never enters profit.
        self.assertEqual(values["net_profit"], "-40264.0000")
        self.assertEqual(coverage["unmatched_units"], 1)

    def test_mixed_period_and_default_legacy_before_boundary(self):
        next_day = DAY + timedelta(days=1)
        rows = [sale(1), sale(2, next_day, docTypeName="Возврат")]
        self.ingest(rows)
        mixed = self.candidate({next_day: "200"}, effective_date=next_day)
        finance, partner, coverage = self.read_candidate(mixed)
        self.assertEqual(finance["aggregate"]["cogs"], "83637.0000")  # published legacy 83837 - shared 200
        self.assertTrue(coverage["quality"]["mixed_cost_methods"])
        self.assertEqual(coverage["quality"]["source_units"]["canonical_exact_date"], 1)
        with closing(self.active.finance._connect()) as conn, conn:
            old = self.active.finance._calculate_cogs(conn, rows[:1], WEEK_ONE, include_details=True)
        with closing(mixed.finance._connect()) as conn, conn:
            same_old = mixed.finance._calculate_cogs(conn, rows[:1], WEEK_ONE, include_details=True)
        self.assertEqual(old, same_old)
        default = WbFinanceWeeklyBlock(self.runtime, seller_id="seller-1", now_factory=NOW,
                                      shared_cost_snapshot=None)
        with closing(default._connect()) as conn, conn:
            self.assertEqual(old, default._calculate_cogs(conn, rows[:1], WEEK_ONE, include_details=True))

    def test_pinned_version_cache_and_active_aggregate_poisoning(self):
        self.ingest([sale(1)])
        store = SharedSkuCostStore(self.runtime / "shared-candidate.sqlite3")
        initial = shared_periods({DAY: "100"})[0]
        store.save(initial, expected_version=None)
        old = self.candidate(snapshot=store.snapshot(effective_date=DAY.isoformat()))
        _, old_report, _ = self.read_candidate(old)
        revision = shared_periods({DAY: "300"})[0]
        store.save(revision, expected_version=initial["version_id"])
        with closing(sqlite3.connect(self.active.db_path)) as conn, conn:
            conn.execute("UPDATE wb_finance_weekly_sku_aggregates SET metrics_json='{}',coverage_json='{}',formula_version='stale' ")
            conn.execute("UPDATE sheet_vitrina_v1_warehouse_wb_daily_cost SET wac_rub='999999'")
            conn.commit()
        before = self.active.db_path.read_bytes()
        with patch.object(CanonicalChannelCostSnapshot, "from_connection", side_effect=AssertionError("legacy read")):
            _, unchanged, _ = self.read_candidate(old)
            _, changed, _ = self.read_candidate(self.candidate(snapshot=store.snapshot(effective_date=DAY.isoformat())))
        self.assertEqual(old_report["source_digest"], unchanged["source_digest"])
        self.assertEqual(unchanged["weeks"][0]["values"]["cogs"], "100.0000")
        self.assertEqual(changed["weeks"][0]["values"]["cogs"], "300.0000")
        self.assertNotEqual(changed["source_digest"], unchanged["source_digest"])
        self.assertEqual(before, self.active.db_path.read_bytes())

    def test_missing_operation_date_does_not_probe_legacy(self):
        rows = [sale(1, rrDate="", saleDt="", orderDt="")]
        self.ingest(rows)
        candidate = self.candidate({DAY: "100"})
        with patch.object(CanonicalChannelCostSnapshot, "from_connection", side_effect=AssertionError("legacy read")):
            finance, _, coverage = self.read_candidate(candidate)
        self.assertIsNone(finance["aggregate"]["cogs"])
        self.assertEqual(coverage["problem_skus"][0]["reason"], "operation_date_missing")

    def test_candidate_cannot_persist_or_finalize(self):
        self.ingest([sale(1)])
        candidate = self.candidate({DAY: "100"})
        before = self.active.db_path.read_bytes()
        for action in (candidate.finance.ensure_schema,
                       lambda: candidate.finance.recalculate_week(WEEK_ONE, END),
                       candidate.finance.build_payload,
                       lambda: candidate.save_settings(_settings(), actor="smoke")):
            with self.assertRaisesRegex(ValueError, "shared_cost_candidate_is_read_only"):
                action()
        with closing(candidate.finance._connect()) as conn, conn:
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaisesRegex(ValueError, "cannot_replace_active"):
                candidate.finance._recalculate_week_in_connection(conn, WEEK_ONE, END)
            settings = candidate._load_settings(conn, nm_id=str(TARGET_NM))
            with self.assertRaises(PartnerReportError):
                candidate._calculate_report(conn, settings=settings, selected_weeks=[WEEK_ONE.isoformat()], finalization=True)
        self.assertEqual(before, self.active.db_path.read_bytes())

    def test_public_candidate_connections_close_and_export_binds_version(self):
        self.ingest([sale(1)])
        candidate = self.candidate({DAY: "100"})
        opened = []
        real_connect = candidate.finance._connect_shared_cost_preview

        def tracked():
            conn = real_connect()
            self.assertTrue(conn.in_transaction)
            opened.append(conn)
            return conn

        with patch.object(candidate.finance, "_connect_shared_cost_preview", side_effect=tracked):
            _, report, _ = self.read_candidate(candidate)
            body, _, evidence = candidate.build_preview_workbook(
                self.payload, expected_source_digest=report["source_digest"],
            )
            self.assertTrue(body.startswith(b"PK"))
            self.assertEqual(evidence["source_digest"], report["source_digest"])
            with self.assertRaises(PartnerReportError) as rejected:
                candidate.build_preview_workbook(self.payload, expected_source_digest="stale-version")
            self.assertEqual(rejected.exception.code, "preview_source_digest_changed")
        self.assertEqual(len(opened), 4)
        for conn in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")

    def test_candidate_keeps_one_database_image_during_concurrent_ingest(self):
        self.ingest([sale(1)])
        candidate = self.candidate({DAY: "100"})
        with closing(sqlite3.connect(self.active.db_path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        real_project = candidate.finance._candidate_week_projection_in_connection

        def concurrent_write(conn, **kwargs):
            self.assertTrue(conn.in_transaction)
            # Partner has already read settings and sync in this transaction.
            # A writer can now publish a different raw quantity, but the
            # current preview must keep the earlier raw/settings/ads image.
            changed = sale(1, quantity=9)
            with closing(sqlite3.connect(self.active.db_path)) as writer, writer:
                writer.execute("UPDATE wb_finance_weekly_raw_rows SET raw_json=?", (json.dumps(changed),))
            return real_project(conn, **kwargs)

        with patch.object(candidate.finance, "_candidate_week_projection_in_connection", side_effect=concurrent_write):
            report = candidate.preview(self.payload)
        self.assertEqual(report["weeks"][0]["values"]["cogs"], "100.0000")
        with closing(sqlite3.connect(self.active.db_path)) as conn:
            self.assertEqual(json.loads(conn.execute("SELECT raw_json FROM wb_finance_weekly_raw_rows").fetchone()[0])["quantity"], 9)

    def test_split_candidate_rejects_unacknowledged_attached_raw_revision(self):
        self.ingest([sale(1)])
        raw_path = self.runtime / "split-raw.sqlite3"
        # Two real files, generation identities and the production attach/view
        # path. The raw fixture needs only the current-row relation read by it.
        with closing(sqlite3.connect(self.active.db_path)) as main:
            with closing(sqlite3.connect(raw_path)) as raw:
                main.backup(raw)
        source_fingerprint = "sha256:" + "a" * 64
        for path, table, logical, revision, generation in (
            (self.active.db_path, "finance_operational_schema_meta", "operational", "operational_v1", "op-test"),
            (raw_path, "finance_raw_schema_meta", "finance_raw", "finance_raw_v1", "raw-test"),
        ):
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(f"CREATE TABLE IF NOT EXISTS {table}(singleton INTEGER PRIMARY KEY,schema_revision TEXT,logical_store TEXT,generation_id TEXT,generation_epoch TEXT,source_fingerprint TEXT,created_at TEXT)")
                conn.execute(f"INSERT OR REPLACE INTO {table} VALUES(1,?,?,?,?,?,?)",
                             (revision, logical, generation, "split-test", source_fingerprint, "2026-07-20T00:00:00Z"))
                if path == raw_path:
                    conn.execute("ALTER TABLE wb_finance_weekly_raw_rows RENAME TO finance_raw_current_rows")
        manifest = build_manifest(
            state="cutover", canonical_source="split", generation_epoch="split-test",
            raw_generation_id="raw-test", raw_relative_path=raw_path.name, raw_watermark="1",
            operational_generation_id="op-test", operational_relative_path=self.active.db_path.name,
            operational_watermark="1", rollback_generation_id="monolith",
            source_fingerprint=source_fingerprint,
        )
        atomic_write_manifest(self.active.store_registry.manifest_path, manifest)
        candidate = self.candidate({DAY: "100"})
        self.assertEqual(candidate.finance.preview_candidate_week(WEEK_ONE, END)["aggregate"]["cogs"], "100.0000")
        before = self.active.db_path.read_bytes()
        real_build = candidate.finance._build_week_target_projection
        changed = sale(1, quantity=9)
        new_row_hash = candidate.finance._row_hash(changed)

        def concurrent_raw_revision(conn, **kwargs):
            self.assertTrue(conn.in_transaction)
            # Freeze main before the raw writer commits, without reading the
            # attached file inside this transaction yet.
            conn.execute("SELECT content_hash FROM main.wb_finance_weekly_sync").fetchall()
            with closing(sqlite3.connect(raw_path)) as writer, writer:
                writer.execute("UPDATE finance_raw_current_rows SET raw_json=?,row_hash=?",
                               (json.dumps(changed), new_row_hash))
            observed = conn.execute("SELECT raw_json FROM wb_finance_weekly_raw_rows").fetchone()
            self.assertEqual(json.loads(observed[0])["quantity"], 9)
            return real_build(conn, **kwargs)

        with patch.object(candidate.finance, "_build_week_target_projection", side_effect=concurrent_raw_revision):
            with self.assertRaisesRegex(ValueError, "shared_cost_candidate_raw_week_incomplete"):
                candidate.finance.preview_candidate_week(WEEK_ONE, END)
        self.assertEqual(self.active.db_path.read_bytes(), before)
        # Once operational sync acknowledges that exact raw revision, the
        # same candidate may price it. Quantity count is checked independently.
        new_week_hash = hashlib.sha256(new_row_hash.encode("utf-8")).hexdigest()
        with closing(sqlite3.connect(self.active.db_path)) as main, main:
            main.execute("UPDATE wb_finance_weekly_sync SET content_hash=?,raw_row_count=2", (new_week_hash,))
        with self.assertRaisesRegex(ValueError, "shared_cost_candidate_raw_week_incomplete"):
            candidate.finance.preview_candidate_week(WEEK_ONE, END)
        with closing(sqlite3.connect(self.active.db_path)) as main, main:
            main.execute("UPDATE wb_finance_weekly_sync SET raw_row_count=1")
        self.assertEqual(candidate.finance.preview_candidate_week(WEEK_ONE, END)["aggregate"]["cogs"], "900.0000")


if __name__ == "__main__":
    unittest.main()
