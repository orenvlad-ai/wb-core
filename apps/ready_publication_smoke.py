#!/usr/bin/env python3
"""Real CAS, source drift, two-commit crash and bound management readers.

Only disposable databases are used. Child processes stop at explicit barriers,
not timing guesses; no production API, refresh or business document is sent.
"""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ci.fixture_process import checkpoint, fixture_process
from apps.fbs_snapshot_cost_smoke import capture, document
from apps.fbs_inventory_presentation_smoke import retained
from apps.shared_sku_cost_smoke import wb
from packages.application import fbs_accounting_runtime as book
from packages.application import registry_upload_db_backed_runtime as dbmod
from packages.application import ready_publication as publication
from packages.application.fbs_snapshot_cost import fingerprint
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope, SheetVitrinaV1TemporalSlot, SheetVitrinaWriteTarget

DAY = "2026-09-08"
OUTER = "2026-09-07"
NOW = datetime(2026, 9, 8, 14, tzinfo=timezone.utc)
STAMP = NOW.isoformat().replace("+00:00", "Z")


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


@contextmanager
def clock():
    with patch.object(dbmod, "datetime", FixedDateTime), patch.object(book, "datetime", FixedDateTime), \
         patch("packages.business_time.current_business_date_iso", return_value=DAY):
        yield


def make_book(root, *, quantity="1000", opening=False):
    image = capture(DAY, quantity=quantity, docs=[] if opening else [document(day=DAY)])
    component = wb(DAY)
    with patch.object(book, "capture_current", return_value=image), \
         patch.object(book, "capture_wb_component", return_value=component), \
         patch.object(book, "capture_retained_stages", return_value=retained(component)):
        return book.prepare(root, now=NOW, opening=opening)


def make_plan():
    rows = [["cost", "SKU:1|our_wb_unit_cost_rub", 91, 92],
            ["capital", "SKU:1|own_capital_FF_capital_rub", 888, 999],
            ["orders", "SKU:1|ordersCount", 11, 12],
            ["unrelated", "SKU:1|foreign_metric", 314, 159]]
    return SheetVitrinaV1Envelope("test-v1", "same-snapshot-id", OUTER, [OUTER, DAY],
        [SheetVitrinaV1TemporalSlot("previous", "previous", OUTER), SheetVitrinaV1TemporalSlot("current", "current", DAY)],
        {}, [SheetVitrinaWriteTarget("DATA_VITRINA", "A1", "A1:D5", "A:D", "replace", False,
            ["label", "key", OUTER, DAY], rows, len(rows), 4),
            SheetVitrinaWriteTarget("STATUS", "A1", "A1:B1", "A:B", "replace", False,
                ["key", "value"], [], 0, 2)])


def seed(root):
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=root)
    bundle = json.loads((ROOT / "artifacts/registry_upload_http_entrypoint/input/registry_upload_bundle__fixture.json").read_text())
    result = runtime.ingest_bundle(bundle, activated_at=STAMP)
    assert result.status == "accepted", result
    return runtime


def save(runtime, plan, *, expected=None, prepared=None):
    current = runtime.load_current_state()
    if expected is None:
        expected = runtime.prepare_sheet_vitrina_ready_publication(bundle_version=current.bundle_version, as_of_date=plan.as_of_date)
    with clock():
        return runtime.save_sheet_vitrina_ready_snapshot(current_state=current, plan=plan,
            refreshed_at=STAMP, expected=expected, _prepared_book=prepared)


def book_ready_child(channel, root):
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(root))
    prepared = make_book(Path(root), quantity="1900")
    with patch.object(book, "_after_book_commit", side_effect=lambda: checkpoint(channel, "book_committed")):
        save(runtime, runtime.load_sheet_vitrina_ready_snapshot(), prepared=prepared)
    checkpoint(channel, "ready_committed")


def cas_child(channel, db_path, expected):
    checkpoint(channel, "prepared")
    with sqlite3.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            publication.replace_ready(conn, expected=expected, plan_json="child",
                activated_at="a", snapshot_id="s", plan_version="v", refreshed_at="r")
        except publication.ReadyPublicationConflict:
            checkpoint(channel, "conflict")
        else:
            raise AssertionError("stale candidate was accepted")


def committed_child(channel, root):
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(root))
    save(runtime, make_plan(), prepared=make_book(Path(root), quantity="1900"))
    checkpoint(channel, "ready_committed_before_ack")


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = seed(self.root)
        self.current = self.runtime.load_current_state()

    def expected(self):
        return self.runtime.prepare_sheet_vitrina_ready_publication(bundle_version=self.current.bundle_version, as_of_date=OUTER)

    def test_exact_raw_cas_and_first_insert_real_processes(self):
        for first in (True, False):
            if not first:
                save(self.runtime, make_plan())
            expected = self.expected()
            with fixture_process(cas_child, str(self.runtime.db_path), expected) as child:
                child.wait("prepared")
                save(self.runtime, replace(make_plan(), metadata={"winner": str(first)}), expected=expected)
                winner = self.expected()
                child.release("prepared")
                child.wait("conflict")
                child.release("conflict")
                child.finish()
            self.assertEqual(self.expected(), winner)

    def test_finalize_updates_only_original_receipt(self):
        first = save(self.runtime, make_plan())
        save(self.runtime, replace(make_plan(), metadata={"later": True}))
        before = self.expected()
        self.runtime.finalize_sheet_vitrina_publication(operation_id=first.publication_operation_id,
            attempt_id=first.publication_attempt_id, diagnostics={"done": True})
        self.assertEqual(before, self.expected())

    def test_source_pins_distinguish_own_acceptance_consumed_drift_and_unrelated_commit(self):
        db = self.runtime.db_path
        def write(value):
            self.runtime.save_temporal_source_slot_snapshot(source_key="spp", snapshot_date=DAY,
                snapshot_role="accepted_current_snapshot", captured_at=STAMP, payload={"kind": "success", "value": value})
        with publication.capture_build_inputs(db) as inputs:
            write(1)
            publication.consume_source(source_key="spp", snapshot_date=DAY)
            with publication.readonly(db) as conn:
                publication.check_build_inputs(conn, inputs)
            write(2)
            with publication.readonly(db) as conn:
                with self.assertRaisesRegex(publication.ReadyPublicationConflict, "during_build"):
                    publication.check_build_inputs(conn, inputs)
        with publication.readonly(db) as conn:
            material = publication.capture_material(conn)
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE unrelated_job_log(message TEXT)")
            conn.execute("INSERT INTO unrelated_job_log VALUES('done')")
        with publication.readonly(db) as conn:
            publication.check_material(conn, material)

    def test_lazy_parameter_source_installs_revision_before_first_write(self):
        from packages.application.calculation_parameters_v4 import ensure_proxy_v4_schema
        db = self.root / "lazy.sqlite3"
        with sqlite3.connect(db) as conn:
            publication.ensure_publication_schema(conn)
        with publication.readonly(db) as conn:
            before = publication.capture_material(conn)
        with sqlite3.connect(db) as conn:
            ensure_proxy_v4_schema(conn)
        with publication.readonly(db) as conn:
            with self.assertRaisesRegex(publication.ReadyPublicationConflict, "parameter_versions"):
                publication.check_material(conn, before)
            publication.capture_material(conn)

    def test_actual_nested_prices_sku_events_and_cross_bundle_donor_drift(self):
        from packages.application.promo_campaign_archive import _load_daily_price_truth
        with publication.capture_build_inputs(self.runtime.db_path) as inputs:
            # Even absence is an operand: no exact price produces a missing cell.
            _load_daily_price_truth(runtime_dir=self.root, snapshot_date=DAY, requested_nm_ids=[1])
        self.assertTrue(inputs["consumed"])
        self.runtime.save_temporal_source_slot_snapshot(source_key="prices_snapshot", snapshot_date=DAY,
            snapshot_role="accepted_current_snapshot", captured_at=STAMP, payload={"items": []})
        with publication.readonly(self.runtime.db_path) as conn:
            with self.assertRaisesRegex(publication.ReadyPublicationConflict, "ready_source_changed"):
                publication.check_build_inputs(conn, inputs)
        with publication.capture_build_inputs(self.runtime.db_path) as inputs:
            self.runtime.load_sku_action_daily_metric_lookup(as_of_date=DAY)
        with sqlite3.connect(self.runtime.db_path) as conn:
            conn.execute("INSERT INTO sheet_vitrina_v1_sku_action_events(event_id,nm_id,parameter,requested_at,confirmed_at,commit_status,readback_status) VALUES('confirmed',1,'price',?,?, 'confirmed','matched')", (STAMP, STAMP))
        with publication.readonly(self.runtime.db_path) as conn:
            with self.assertRaisesRegex(publication.ReadyPublicationConflict, "sku_action_events"):
                publication.check_build_inputs(conn, inputs)
        save(self.runtime, make_plan())
        with sqlite3.connect(self.runtime.db_path) as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET bundle_version='donor'")
        absent = self.expected()
        self.assertFalse(absent.exists)
        with publication.capture_build_inputs(self.runtime.db_path) as inputs:
            self.runtime.load_sheet_vitrina_ready_snapshot_any_bundle(as_of_date=OUTER)
        with sqlite3.connect(self.runtime.db_path) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=json_set(plan_json,'$.donor_changed',1)")
        with publication.readonly(self.runtime.db_path) as conn:
            publication.check_expected(conn, absent)
            with self.assertRaisesRegex(publication.ReadyPublicationConflict, "ready_history"):
                publication.check_build_inputs(conn, inputs)

    def test_prepared_reconciliation_outside_writer_matches_committed_candidate(self):
        from packages.application import warehouse_business_projection as projection
        from packages.application import sheet_vitrina_v1_inventory_history as history
        original, original_history = projection.reconcile_warehouse_business_projection, history.prepare_inventory_history_from_ready_plan
        observed = []
        def prepare(conn, **kwargs):
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            value = original(conn, **kwargs)
            observed.append(value)
            return value
        def inventory(conn, **kwargs):
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            return original_history(conn, **kwargs)
        with patch.object(projection, "reconcile_warehouse_business_projection", side_effect=prepare), \
             patch.object(history, "prepare_inventory_history_from_ready_plan", side_effect=inventory):
            save(self.runtime, make_plan())
        self.assertEqual(len(observed), 1)
        with publication.readonly(self.runtime.db_path) as conn:
            self.assertEqual(original(conn), observed[0])

    def test_crash_after_ready_commit_reads_same_exact_complete_receipt(self):
        initial, expected = make_book(self.root, opening=True)
        book.save(self.root, initial, expected=expected, operation_id="opening")
        with fixture_process(committed_child, str(self.root)) as child:
            child.wait("ready_committed_before_ack")
            accepted = self.expected()
            child.crash()
        with publication.readonly(self.runtime.db_path) as conn:
            operation = conn.execute("SELECT operation_id FROM sheet_vitrina_v1_ready_publications WHERE kind='book_ready'").fetchone()[0]
        status = book.publication_status(self.root, operation_id=operation)
        self.assertEqual(status["state"], "complete")
        self.assertEqual(status["after_digest"], publication.digest(accepted.plan_json))
        self.assertEqual(book.publication_status(self.root, operation_id=operation), status)
        self.assertEqual(self.expected(), accepted)

    def test_book_ahead_crash_and_restart_keep_accepted_pair_and_strict_consumer(self):
        initial, expected = make_book(self.root, opening=True)
        first_version = book.save(self.root, initial, expected=expected, operation_id="opening")
        save(self.runtime, make_plan())
        first_ready = self.expected()
        original = self.runtime.load_sheet_vitrina_ready_snapshot()
        first_cost = book.load_management_inventory(self.root, original, now=NOW).metrics(1)["our_wb_unit_cost_rub"]
        with fixture_process(book_ready_child, str(self.root)) as child:
            child.wait("book_committed")
            # Reader does not wait for the operational writer; book B2 is
            # independently accepted but must not become R1's inventory.
            self.assertEqual(self.expected(), first_ready)
            self.assertNotEqual(book.load(self.root)[1], first_version)
            self.assertEqual(book.load_management_inventory(self.root, original, now=NOW).metrics(1)["our_wb_unit_cost_rub"], first_cost)
            self.assertNotEqual(book.load_inventory(self.root, now=NOW).metrics(1)["our_wb_unit_cost_rub"], first_cost)
            child.crash()
        restarted = RegistryUploadDbBackedRuntime(runtime_dir=self.root)
        self.assertEqual(self.expected(), first_ready)
        with publication.readonly(self.runtime.db_path) as conn:
            intent = conn.execute("SELECT operation_id FROM sheet_vitrina_v1_ready_publications WHERE kind='book_ready'").fetchone()[0]
        status = book.publication_status(self.root, operation_id=intent)
        self.assertEqual(status["state"], "book_committed_ready_pending")
        self.assertEqual(book.load_management_inventory(self.root, restarted.load_sheet_vitrina_ready_snapshot(), now=NOW).metrics(1)["our_wb_unit_cost_rub"], first_cost)
        self.assertEqual(book.publication_status(self.root, operation_id=intent), status)

    def test_bound_period_and_legacy_never_guess_latest(self):
        initial, expected = make_book(self.root, opening=True)
        book.save(self.root, initial, expected=expected, operation_id="opening")
        save(self.runtime, make_plan())
        plan = self.runtime.load_sheet_vitrina_ready_snapshot()
        from packages.application.sheet_vitrina_v1_web_vitrina import _build_period_snapshot
        period, _ = _build_period_snapshot(runtime=self.runtime, date_from=OUTER, date_to=DAY, default_visible_snapshot=plan)
        self.assertEqual(book.load_management_inventory(self.root, period, now=NOW).payload(),
                         book.load_management_inventory(self.root, plan, now=NOW).payload())
        legacy = replace(plan, metadata={})
        self.assertIsNone(book.load_management_inventory(self.root, legacy, now=NOW))
        invalid = deepcopy(plan.metadata)
        invalid["fbs_accounting_bindings"][DAY]["ready_target"]["bundle_version"] = "other"
        with self.assertRaisesRegex(ValueError, "binding_target"):
            book.load_management_inventory(self.root, replace(plan, metadata=invalid), now=NOW)
        paused = {**initial, "active": False}
        book.save(self.root, paused, expected=book.load(self.root)[1], operation_id="pause")
        self.assertIsNone(book.load_management_inventory(self.root, plan, now=NOW))

    def test_prepare_failure_preserves_display_but_rejects_open_operands(self):
        initial, expected = make_book(self.root, opening=True)
        book.save(self.root, initial, expected=expected, operation_id="opening")
        save(self.runtime, make_plan())
        before = self.expected()
        with patch.object(book, "prepare", side_effect=ValueError("source_corrupt")):
            with self.assertRaisesRegex(ValueError, "source_corrupt"):
                book.refresh(self.root)
        self.assertEqual(before, self.expected())
        self.assertIsNone(book.load_management_inventory(self.root, self.runtime.load_sheet_vitrina_ready_snapshot(), now=NOW))
        self.assertEqual(book.load_inventory(self.root, now=NOW).payload()["quality"], "unavailable")
        self.assertIsNone(book.load_shared(self.root).resolve(nm_id="1", operation_date=NOW.date()).get("unit_cost_rub"))
        with self.assertRaisesRegex(publication.ReadyPublicationConflict, "book_operand_unavailable"):
            save(self.runtime, make_plan())
        self.assertEqual(before, self.expected())

    def test_old_failed_prepare_does_not_mark_newer_book_or_paused_book(self):
        initial, expected = make_book(self.root, opening=True)
        old = book.save(self.root, initial, expected=expected, operation_id="opening")
        newer, expected = make_book(self.root, quantity="1900")
        version = book.save(self.root, newer, expected=expected, operation_id="newer")
        book._record_prepare_failure(self.root, expected=old, error="old_failure")
        self.assertEqual(book.load(self.root)[1], version)
        self.assertFalse(book.load(self.root)[0].get("publication_error"))
        paused = book.save(self.root, {**newer, "active": False}, expected=version, operation_id="pause")
        book._record_prepare_failure(self.root, expected=paused, error="paused_failure")
        self.assertEqual(book.load(self.root)[1], paused)


if __name__ == "__main__":
    unittest.main()
