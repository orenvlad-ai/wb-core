#!/usr/bin/env python3
"""Bounded regression checks for archived recovery inside a proven open gap."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.fbs_snapshot_cost_smoke import capture, document, gap_capture
from apps.fbs_inventory_presentation_smoke import retained
from apps.shared_sku_cost_smoke import wb
from packages.application import fbs_accounting_runtime as runtime
from packages.application import fbs_accounting_gap_archive as archive
from packages.application.fbs_snapshot_cost import evaluate_candidate, initialize_candidate, close_candidate_period
from packages.application.fbs_snapshot_cost import fingerprint
from packages.application.ready_publication import ensure_publication_schema
from packages.application.storage_registry import StoreRegistry
from packages.application.wb_fbs_warehouse_registry import REGISTRY_RUNS_TABLE


DAY7, DAY8, DAY9, DAY10 = "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10"


def archived(day=DAY9, quantity=750):
    rows = [{"nm_id": 1, "facility_id": "ff-1", "quantity": quantity}]
    result = {"date": day, "captured_at": day + "T13:00:00Z", "id": "archive-" + day,
              "digest": fingerprint(rows), "complete": True, "rows": rows,
              "source": "official_fbs_stock_snapshot_v1",
              "facility_evidence": {"ff-1": {"seller_warehouse_id": 123,
                  "mapping_id": "official-map-1"}}}
    return result


def book_with_gap():
    state = initialize_candidate(gap_capture(), open_initial_day=True)
    state = evaluate_candidate(state, gap_capture(DAY7), allow_cost_continuity_gap=True)
    state = close_candidate_period(state, DAY7, today=DAY10)
    state = evaluate_candidate(state, gap_capture(DAY10, quantity="750"), allow_cost_continuity_gap=True)
    day7 = wb(DAY7)
    return {"schema": runtime.SCHEMA, "active": True, "effective_date": DAY10,
            "state": state, "shared_days": {DAY7: {"date": DAY7, "status": "closed", "fixture": "immutable"}},
            "wb_days": {DAY7: day7}, "retained_days": {DAY7: retained(day7)},
            "presentations": {DAY7: {"fixture": "closed-presentation"}}}


class GapArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = StoreRegistry(self.root).resolve("operational")
        from packages.application.wb_fbs_warehouse_registry import ensure_wb_fbs_warehouse_registry_schema
        with sqlite3.connect(self.db) as conn:
            ensure_wb_fbs_warehouse_registry_schema(conn)
            ensure_publication_schema(conn)
        self.conn = sqlite3.connect(self.db)
        self.conn.execute("PRAGMA query_only=ON")
        self.conn.execute("BEGIN")
        self.addCleanup(self.conn.close)
        self.capture = gap_capture(DAY10, quantity="750")
        self.wb9 = wb(DAY9, q="600", capital="90000")
        self.wb9["version_id"] = "wb-archive-9"
        self.wb9["authority_complete"] = True
        self.wb9["source_digest"] = fingerprint({k: v for k, v in self.wb9.items() if k != "source_digest"})
        self.retained9 = retained(self.wb9)

    def recover(self, book=None, *, snapshot=None, wb_value=None, retained_value=None):
        with patch.object(archive, "archived_quantity_snapshot", return_value=deepcopy(snapshot or archived())), \
             patch.object(archive, "capture_wb_component", return_value=deepcopy(wb_value or self.wb9)), \
             patch.object(archive, "capture_retained_stages", return_value=deepcopy(retained_value or self.retained9)), \
             patch("packages.application.operator_ff_overhead.assert_day_can_close"):
            return archive.recover_previous_day(book or book_with_gap(), capture=deepcopy(self.capture),
                                                db=self.db, conn=self.conn)

    def test_recovers_only_day9_and_keeps_closed7_and_current10_exact(self):
        before = book_with_gap()
        current_before = deepcopy(before["state"]["periods"][DAY10])
        closed_before = {k: deepcopy(before[k][DAY7]) for k in ("shared_days", "wb_days", "retained_days", "presentations")}
        after = self.recover(before)
        self.assertEqual(set(after["state"]["periods"]), {DAY7, DAY9, DAY10})
        self.assertNotIn(DAY8, after["state"]["periods"])
        self.assertEqual(after["state"]["periods"][DAY7], before["state"]["periods"][DAY7])
        self.assertEqual(self.current_values(after["state"]["periods"][DAY10]),
                         self.current_values(current_before))
        self.assertEqual({k: after[k][DAY7] for k in closed_before}, closed_before)
        row9 = after["state"]["periods"][DAY9]["rows"]["ff-1:1"]
        self.assertEqual(row9["quantity"], "750")
        self.assertEqual(after["state"]["periods"][DAY9]["snapshot"]["id"], "archive-" + DAY9)
        self.assertEqual(after["wb_days"][DAY9], self.wb9)
        self.assertEqual(after["retained_days"][DAY9], self.retained9)
        self.assertEqual(after["presentations"][DAY9]["date"], DAY9)
        shared9 = after["shared_days"][DAY9]
        self.assertEqual((shared9["business_date"], shared9["status"], shared9["quality"]),
                         (DAY9, "closed", "complete"))
        self.assertEqual((shared9["source"]["wb_version_id"], shared9["rows"]["1"]["quantity"],
                          shared9["rows"]["1"]["capital_rub"]), (self.wb9["version_id"], "1350", "165000"))
        presentation9 = after["presentations"][DAY9]
        self.assertEqual(presentation9["retained_stage_digest"], self.retained9["source_digest"])
        self.assertEqual(presentation9["rows"]["1"]["stages"]["WB"]["quantity"], "600")
        self.assertEqual(presentation9["rows"]["1"]["shared_cost"]["capital_rub"], "165000")
        self.assertEqual(presentation9["totals"]["fbs"]["capital_rub"], "75000")
        self.assertEqual(self.recover(after), after)

    @staticmethod
    def current_values(period):
        return {key: {field: row[field] for field in ("quantity", "wac_rub", "capital_rub")}
                for key, row in period["rows"].items()}

    def test_missing_archive_or_incomplete_wb_returns_original_book(self):
        original = book_with_gap()
        with patch.object(archive, "archived_quantity_snapshot", side_effect=ValueError("historical_archived_quantity_missing:" + DAY9)):
            self.assertEqual(archive.recover_previous_day(original, capture=self.capture, db=self.db, conn=self.conn), original)
        for wb_bad in ({**self.wb9, "complete": False}, {**self.wb9, "authority_complete": False}):
            with self.subTest(wb=wb_bad):
                self.assertEqual(self.recover(original, wb_value=wb_bad), original)

    def test_corrupt_archive_mapping_or_new_gap_document_fails_closed(self):
        original = book_with_gap()
        bad = archived(); bad["date"] = DAY8
        with self.assertRaisesRegex(ValueError, "incomplete_source_capture"):
            self.recover(original, snapshot=bad)
        changed_archive = archived()
        changed_archive["facility_evidence"]["ff-1"]["mapping_id"] = "changed-map"
        with patch.object(archive, "archived_quantity_snapshot", return_value=changed_archive), \
             patch.object(archive, "capture_wb_component", return_value=self.wb9), \
             patch.object(archive, "capture_retained_stages", return_value=self.retained9), \
             patch("packages.application.operator_ff_overhead.assert_day_can_close"):
            self.assertEqual(archive.recover_previous_day(original, capture=deepcopy(self.capture),
                                                          db=self.db, conn=self.conn), original)
        with self.assertRaisesRegex(ValueError, "cost_continuity_unabsorbed_document"):
            with patch.object(archive, "archived_quantity_snapshot", return_value=archived()), \
                 patch.object(archive, "capture_wb_component", return_value=self.wb9), \
                 patch.object(archive, "capture_retained_stages", return_value=self.retained9), \
                 patch("packages.application.operator_ff_overhead.assert_day_can_close"):
                archive.recover_previous_day(original, capture=gap_capture(DAY10, docs=[
                    document(identity="new-gap-doc", day=DAY9)]), db=self.db, conn=self.conn)

    def test_no_recovery_without_proof_for_day_or_when_target_is_not_open_tail(self):
        original = book_with_gap()
        no_proof = deepcopy(original); no_proof["state"]["periods"][DAY10].pop("cost_continuity")
        self.assertEqual(self.recover(no_proof), no_proof)
        closed_suffix = deepcopy(original)
        closed_suffix["state"]["periods"][DAY9] = {"status": "closed"}
        self.assertEqual(self.recover(closed_suffix), closed_suffix)
        with self.assertRaisesRegex(ValueError, "incomplete_source_capture"):
            self.recover(original, snapshot=archived(DAY8))


class RuntimePersistenceTests(unittest.TestCase):
    @staticmethod
    def runtime_fixture(root):
        db = StoreRegistry(root).resolve("operational")
        from packages.application.wb_fbs_warehouse_registry import ensure_wb_fbs_warehouse_registry_schema
        with sqlite3.connect(db) as conn:
            ensure_wb_fbs_warehouse_registry_schema(conn)
            ensure_publication_schema(conn)
        image = {"value": gap_capture()}; current_wb = {"value": wb()}
        patches = (
            patch.object(runtime, "capture_current", side_effect=lambda *a, **kw: deepcopy(image["value"])),
            patch.object(runtime, "capture_wb_component", side_effect=lambda *a, **kw: deepcopy(current_wb["value"])),
            patch.object(runtime, "capture_retained_stages", side_effect=lambda *a, **kw: retained(current_wb["value"])),
        )
        return db, image, current_wb, patches

    def test_prepare_save_load_persists_recovered_day_read_only(self):
        tmp = TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name); db = StoreRegistry(root).resolve("operational")
        from packages.application.wb_fbs_warehouse_registry import ensure_wb_fbs_warehouse_registry_schema
        with sqlite3.connect(db) as conn:
            ensure_wb_fbs_warehouse_registry_schema(conn)
            ensure_publication_schema(conn)
        image = {"value": gap_capture()}; current_wb = {"value": wb()}
        with patch.object(runtime, "capture_current", side_effect=lambda *a, **kw: deepcopy(image["value"])), \
             patch.object(runtime, "capture_wb_component", side_effect=lambda *a, **kw: deepcopy(current_wb["value"])), \
             patch.object(runtime, "capture_retained_stages", side_effect=lambda *a, **kw: retained(current_wb["value"])):
            initial, expected = runtime.prepare(root, now=datetime(2026, 9, 7, 14, tzinfo=timezone.utc), opening=True)
            runtime.save(root, initial, expected=expected, operation_id="gap-archive-open")
            image["value"], current_wb["value"] = gap_capture(DAY10, quantity="750"), wb(DAY10)
            wb9 = {**wb(DAY9), "authority_complete": True}
            with patch.object(archive, "archived_quantity_snapshot", return_value=archived()), \
                 patch.object(archive, "capture_wb_component", return_value=wb9), \
                 patch.object(archive, "capture_retained_stages", return_value=retained(wb9)), \
                 patch("packages.application.operator_ff_overhead.assert_day_can_close"):
                candidate, expected = runtime.prepare(root, now=datetime(2026, 9, 10, 14, tzinfo=timezone.utc))
                self.assertIn(DAY9, candidate["state"]["periods"])
                version = runtime.save(root, candidate, expected=expected, operation_id="gap-archive-recover")
                loaded, loaded_version = runtime.load(root)
        self.assertEqual(loaded_version, version)
        self.assertEqual(loaded["state"]["periods"][DAY9], candidate["state"]["periods"][DAY9])
        self.assertEqual(loaded["shared_days"][DAY9], candidate["shared_days"][DAY9])
        self.assertEqual(loaded["presentations"][DAY9], candidate["presentations"][DAY9])
        self.assertEqual(loaded["state"]["periods"][DAY10]["rows"], candidate["state"]["periods"][DAY10]["rows"])
        with sqlite3.connect(f"file:{runtime.path(root)}?mode=ro", uri=True) as conn:
            conn.execute("PRAGMA query_only=ON")
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)

    def test_archive_mapping_mismatch_skips_recovery_but_current_day_saves(self):
        tmp = TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name); db, image, current_wb, patches = self.runtime_fixture(root)
        with patches[0], patches[1], patches[2]:
            initial, expected = runtime.prepare(root, now=datetime(2026, 9, 7, 14, tzinfo=timezone.utc), opening=True)
            runtime.save(root, initial, expected=expected, operation_id="mapping-skip-open")
            image["value"], current_wb["value"] = gap_capture(DAY10, quantity="750"), wb(DAY10)
            changed_archive = archived()
            changed_archive["facility_evidence"]["ff-1"]["mapping_id"] = "archived-map-changed"
            wb9 = {**wb(DAY9), "authority_complete": True}
            with patch.object(archive, "archived_quantity_snapshot", return_value=changed_archive), \
                 patch.object(archive, "capture_wb_component", return_value=wb9), \
                 patch.object(archive, "capture_retained_stages", return_value=retained(wb9)), \
                 patch("packages.application.operator_ff_overhead.assert_day_can_close"):
                candidate, expected = runtime.prepare(root, now=datetime(2026, 9, 10, 14, tzinfo=timezone.utc))
            self.assertNotIn(DAY9, candidate["state"]["periods"])
            self.assertIn(DAY10, candidate["state"]["periods"])
            version = runtime.save(root, candidate, expected=expected, operation_id="mapping-skip-current")
        self.assertEqual(runtime.load(root), (candidate, version))

    def test_archive_source_revision_change_between_prepare_and_save_fails_cas(self):
        tmp = TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name); db, image, current_wb, patches = self.runtime_fixture(root)
        with patches[0], patches[1], patches[2]:
            initial, expected = runtime.prepare(root, now=datetime(2026, 9, 7, 14, tzinfo=timezone.utc), opening=True)
            initial_version = runtime.save(root, initial, expected=expected, operation_id="archive-cas-open")
            image["value"], current_wb["value"] = gap_capture(DAY10, quantity="750"), wb(DAY10)
            wb9 = {**wb(DAY9), "authority_complete": True}
            with patch.object(archive, "archived_quantity_snapshot", return_value=archived()), \
                 patch.object(archive, "capture_wb_component", return_value=wb9), \
                 patch.object(archive, "capture_retained_stages", return_value=retained(wb9)), \
                 patch("packages.application.operator_ff_overhead.assert_day_can_close"):
                candidate, expected = runtime.prepare(root, now=datetime(2026, 9, 10, 14, tzinfo=timezone.utc))
            from packages.application.ready_publication import REVISIONS
            with sqlite3.connect(db) as conn:
                conn.execute(f"UPDATE {REVISIONS} SET revision=revision+1 WHERE source_table=?",
                             (REGISTRY_RUNS_TABLE,))
            with self.assertRaisesRegex(ValueError, "ready_material_input_changed"):
                runtime.save(root, candidate, expected=expected, operation_id="archive-cas-conflict")
        loaded, version = runtime.load(root)
        self.assertEqual(version, initial_version)
        self.assertEqual(loaded, initial)


if __name__ == "__main__":
    unittest.main(verbosity=2)
