#!/usr/bin/env python3
"""Active cutover, same-day receipts, daily closure, persistence and consumers."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
import json
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.fbs_snapshot_cost_smoke import capture, document
from apps.fbs_inventory_presentation_smoke import retained, row
from apps.shared_sku_cost_smoke import wb
from apps.shared_sku_cost_reports_smoke import SharedCostReportTests, shared_periods, sale, DAY, END, WEEK_ONE
from packages.application import fbs_accounting_runtime as runtime
from packages.application.fbs_snapshot_cost import fingerprint
from packages.application.canonical_wb_cost_resolver import CanonicalChannelCostSnapshot
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope, SheetVitrinaWriteTarget, SheetVitrinaV1TemporalSlot


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        from packages.application.storage_registry import StoreRegistry
        from packages.application.ready_publication import ensure_publication_schema
        self.operational = StoreRegistry(self.root).resolve("operational")
        with sqlite3.connect(self.operational) as conn:
            ensure_publication_schema(conn)
        self.day = "2026-09-07"
        self.image = capture()
        self.wb = wb()
        self.now = datetime(2026, 9, 7, 14, tzinfo=timezone.utc)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.read = self.stack.enter_context(patch.object(runtime, "capture_current", side_effect=lambda *a, **kw: deepcopy(self.image)))
        self.stack.enter_context(patch.object(runtime, "capture_wb_component", side_effect=lambda *a, **kw: deepcopy(self.wb)))
        self.stack.enter_context(patch.object(runtime, "capture_retained_stages", side_effect=lambda *a, **kw: retained(self.wb)))

    def opening(self):
        book, expected = runtime.prepare(self.root, now=self.now, opening=True)
        version = runtime.save(self.root, book, expected=expected, operation_id="activate-test")
        return book, version

    def test_cutover_is_intraday_and_receipt_counted_once(self):
        book, first = self.opening()
        baseline = deepcopy(book["state"]["baseline"])
        self.assertIn(self.day, book["state"]["periods"])
        self.image = capture(quantity="1900", docs=[document(day=self.day)])
        book, expected = runtime.prepare(self.root, now=self.now)
        item = book["state"]["periods"][self.day]["rows"]["ff-1:1"]
        self.assertEqual((item["quantity"], item["wac_rub"], item["capital_rub"]), ("1900", "150", "285000"))
        runtime.save(self.root, book, expected=expected, operation_id="refresh-one")
        again, _ = runtime.prepare(self.root, now=self.now)
        self.assertEqual(again["state"]["periods"][self.day], book["state"]["periods"][self.day])
        self.assertEqual(again["state"]["baseline"], baseline)
        self.assertFalse(self.read.call_args.kwargs["include_baseline"])

    def test_next_day_closes_prior_with_exact_saved_stock(self):
        book, _ = self.opening()
        self.day = "2026-09-08"
        self.now = datetime(2026, 9, 8, 14, tzinfo=timezone.utc)
        self.image = capture(self.day, quantity="900")
        self.wb = wb(self.day)
        after, previous = runtime.prepare(self.root, now=self.now)
        self.assertEqual(after["shared_days"]["2026-09-07"]["status"], "closed")
        self.assertEqual(after["state"]["periods"][self.day]["rows"]["ff-1:1"]["opening_quantity"], "1000")
        runtime.save(self.root, after, expected=previous, operation_id="day-two")
        self.image = capture("2026-09-09")
        self.wb = wb("2026-09-09")
        third, _ = runtime.prepare(self.root, now=datetime(2026, 9, 9, 14, tzinfo=timezone.utc))
        self.assertEqual(third["shared_days"]["2026-09-07"], after["shared_days"]["2026-09-07"])

    def test_no_missing_day_invention(self):
        self.opening()
        self.image = capture("2026-09-09")
        self.wb = wb("2026-09-09")
        before = runtime.path(self.root).read_bytes()
        with self.assertRaisesRegex(ValueError, "current_fbs_period_missing"):
            runtime.prepare(self.root, now=datetime(2026, 9, 9, 14, tzinfo=timezone.utc))
        self.assertEqual(runtime.path(self.root).read_bytes(), before)

    def test_atomic_cas_opening_and_closed_cost_guards(self):
        book, version = self.opening()
        with self.assertRaisesRegex(ValueError, "compare_and_swap"):
            runtime.save(self.root, book, expected=None, operation_id="conflict")
        edited = deepcopy(book)
        edited["state"]["baseline"]["rows"]["ff-1:1"]["wac_rub"] = "1"
        with self.assertRaisesRegex(ValueError, "opening_is_immutable"):
            runtime.save(self.root, edited, expected=version, operation_id="bad-opening")
        self.assertEqual(runtime.load(self.root)[1], version)

    def test_readers_share_one_published_revision_and_stale_never_legacy(self):
        book, _ = self.opening()
        s = runtime.load_inventory(self.root, now=self.now)
        ff, cards = s.warehouse_detail(), s.planning_payload()
        self.assertFalse(ff["candidate_only"])
        self.assertEqual(ff["warehouse"]["total_quantity"], "1000")
        self.assertEqual(next(m["value"] for m in cards["metrics"] if m["metric_key"] == "fbs_total"), 1000)
        self.assertEqual(ff["version_id"], cards["version_id"])
        shared = runtime.load_shared(self.root)
        resolved = shared.resolve(nm_id="1", operation_date=date(2026, 9, 7))
        self.assertFalse(resolved["candidate_only"])
        self.assertEqual(resolved["unit_cost_rub"], s.metrics(1)["our_wb_unit_cost_rub"])
        stale = runtime.load_inventory(self.root, now=datetime(2026, 9, 8, 14, tzinfo=timezone.utc))
        self.assertIsNotNone(stale)
        self.assertIsNone(stale.warehouse_detail()["warehouse"]["total_quantity"])
        self.assertIsNone(stale.metrics(1)["stock_fbs_total"] if "stock_fbs_total" in stale.metrics(1) else stale.metrics(1)["own_capital_FF_qty"])
        self.assertIsNone(shared.resolve(nm_id="1", operation_date=date(2026, 9, 8)).get("unit_cost_rub"))

    def test_current_publisher_verifies_ready_without_loading_legacy_history(self):
        self.opening()
        snapshot = runtime.load_inventory(self.root, now=self.now)
        data = snapshot.payload()
        cells = {}
        for nm in [None, *data["rows"]]:
            scope = "TOTAL" if nm is None else "SKU:" + nm
            for key, value in snapshot._metrics(data, nm).items():
                cells[scope + "|" + key] = {self.day: snapshot.presentation(value, data=data)}
        db = self.root / "ready.sqlite3"
        from packages.application.ready_publication import ensure_publication_schema, record_intent, complete_publication, ExpectedReady, digest
        version = runtime.load(self.root)[1]
        plan_json = json.dumps({"metadata": {"server_cell_presentation": cells,
            "fbs_accounting_bindings": {self.day: {"book_version": version}}}})
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE sheet_vitrina_v1_ready_snapshots(bundle_version TEXT,as_of_date TEXT,plan_json TEXT,refreshed_at TEXT)")
            conn.execute("CREATE TABLE registry_upload_current_state(slot INTEGER,bundle_version TEXT)")
            conn.execute("INSERT INTO registry_upload_current_state VALUES(1,'b1')")
            ensure_publication_schema(conn)
            record_intent(conn, operation_id="ready-test", attempt_id="1", kind="ready",
                expected=ExpectedReady("b1", self.day, None), inputs={}, expected_book=version,
                book_required=False, ready_required=True, created_at=self.now.isoformat())
            conn.execute("INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES(?,?,?,?)",
                         ("b1", self.day, plan_json, self.now.isoformat()))
            complete_publication(conn, operation_id="ready-test", attempt_id="1", book_version=version,
                after_digest=digest(plan_json), finished_at=self.now.isoformat())
        owner = SimpleNamespace(runtime_dir=self.root, db_path=db)
        from packages.application.calculation_parameters import CalculationParametersBlock
        block = CalculationParametersBlock.__new__(CalculationParametersBlock)
        block.runtime = owner
        from packages.application import warehouse_functional_economics_backfill as legacy
        with patch.object(legacy, "build_functional_economics_backfill_plan", side_effect=AssertionError("historical scan")), \
             patch.object(runtime, "current_business_date_iso", return_value=self.day), \
             patch.object(runtime, "inventory_from_book", return_value=snapshot):
            result = block.publish_current_functional_economics()
        self.assertGreater(result["checked_cell_count"], 0)
        self.assertFalse(result["database_written"])
        self.assertFalse(result["historical_replay"])
        cells["SKU:1|our_wb_unit_cost_rub"][self.day]["management_value"] = "999"
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?",
                         (json.dumps({"metadata":{"server_cell_presentation":cells}}),))
        with self.assertRaisesRegex(ValueError, "ready_publication_mismatch"):
            runtime.current_publication_receipt(owner, now=self.now)
        with self.assertRaisesRegex(ValueError, "publication_unavailable"):
            runtime.current_publication_receipt(owner, now=datetime(2026,9,8,14,tzinfo=timezone.utc))

    def test_explicit_history_request_keeps_its_separate_path(self):
        from packages.application.calculation_parameters import CalculationParametersBlock
        from packages.application import warehouse_functional_economics_backfill as legacy
        block = CalculationParametersBlock.__new__(CalculationParametersBlock)
        block.runtime = SimpleNamespace(runtime_dir=self.root)
        with patch.object(runtime, "current_publication_receipt", side_effect=AssertionError("wrong owner")), \
             patch.object(legacy, "build_functional_economics_backfill_plan", return_value={"plan_fingerprint":"p"}) as build, \
             patch.object(legacy, "apply_functional_economics_backfill_plan", return_value={"status":"applied"}):
            self.assertEqual(block.publish_current_functional_economics(include_history=True)["status"], "applied")
            build.assert_called_once()

    def test_foreign_database_rejected_without_modification(self):
        with sqlite3.connect(runtime.path(self.root)) as conn:
            conn.execute("CREATE TABLE business(value)")
        before = runtime.path(self.root).read_bytes()
        with self.assertRaisesRegex(ValueError, "isolated_fbs"):
            runtime.load(self.root)
        self.assertEqual(runtime.path(self.root).read_bytes(), before)

    def test_ready_publication_replaces_current_cells_and_preserves_prior_dates(self):
        self.opening()
        days = ["2026-09-06", self.day]
        plan = SheetVitrinaV1Envelope("v1", "snapshot", days[0], days,
            [SheetVitrinaV1TemporalSlot("previous", "previous", days[0]),
             SheetVitrinaV1TemporalSlot("current", "current", days[1])], {}, [
            SheetVitrinaWriteTarget("DATA_VITRINA", "A1", "A1:D3", "A:D", "replace", False,
                ["label", "key", *days], [["cost", "SKU:1|our_wb_unit_cost_rub", 99, 999],
                ["capital", "SKU:1|own_capital_FF_capital_rub", 888, 999]], 2, 4)])
        after = runtime.materialize(plan, runtime_dir=self.root, now=self.now)
        self.assertEqual(after.sheets[0].rows[0][2], 99)
        self.assertEqual(after.sheets[0].rows[1][2:], [888, 100000.0])
        self.assertEqual(after.metadata["server_cell_presentation"]["SKU:1|own_capital_FF_capital_rub"][self.day]["candidate_only"], False)
        self.assertEqual(runtime.materialize(after, runtime_dir=self.root, now=self.now), after)


class ActiveReportsTests(SharedCostReportTests):
    def test_active_ingestion_and_partner_use_indexed_new_cost(self):
        shared = runtime.ActiveSharedCostSnapshot(shared_periods({DAY: "175"}), effective_date=DAY.isoformat())
        with patch.object(runtime, "load_shared", return_value=shared):
            self.ingest([sale(1, quantity=2, deliveryType="FBS"), sale(2, deliveryType="FBO")])
            with patch.object(CanonicalChannelCostSnapshot, "from_connection", side_effect=AssertionError("legacy prices")):
                finance = self.active.finance.build_payload()
                report = self.active.preview(self.payload)
            self.assertEqual(finance["weeks"][0]["metrics"]["cogs"], "525.0000")
            self.assertEqual(report["weeks"][0]["values"]["cogs"], "525.0000")
            self.assertFalse(report["performance"]["raw_finance_full_scan"])
            self.assertFalse(report["shared_cost"]["candidate_only"])
            self.assertFalse(report.get("candidate_only", False))


if __name__ == "__main__":
    unittest.main()
