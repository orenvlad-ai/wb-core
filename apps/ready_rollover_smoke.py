#!/usr/bin/env python3
"""Default rollover preserves accepted intraday columns without closing them."""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.fbs_accounting_degraded_smoke import DegradedTests
from apps.ready_publication_smoke import make_plan, make_book, save, NOW
from packages.application import ready_rollover, ready_rollover_recovery, ready_publication as ready
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1TemporalSlot
from packages.application import fbs_accounting_runtime as accounting
from packages.application.business_data_heavy_admission import heavy_admitted
from packages.application.web_vitrina_window_v3 import _ready_headers, _select_bindings


class RolloverTests(unittest.TestCase):
    setUp = DegradedTests.setUp
    scope = DegradedTests.scope
    def donor(self):
        plan = make_plan('2026-09-07', '2026-09-08')
        plan = replace(plan, temporal_slots=[SheetVitrinaV1TemporalSlot('yesterday_closed','closed','2026-09-07'),
            SheetVitrinaV1TemporalSlot('today_current','current','2026-09-08')])
        with heavy_admitted(self.root, operation='cycle'), self.scope():
            save(self.runtime, plan, now=datetime(2026,9,8,14,tzinfo=timezone.utc))
        return self.runtime.load_sheet_vitrina_ready_snapshot('2026-09-07')

    def advance(self):
        with heavy_admitted(self.root, operation='cycle'), self.scope():
            return save(self.runtime, make_plan('2026-09-09', '2026-09-10'),
                        now=datetime(2026,9,10,14,tzinfo=timezone.utc))

    def test_rollover_keeps_intraday_truth_and_book(self):
        donor = self.donor()
        self.advance()
        retained = self.runtime.load_sheet_vitrina_ready_snapshot('2026-09-08')
        self.assertEqual(retained.temporal_slots, donor.temporal_slots)
        self.assertEqual(retained.snapshot_id, donor.snapshot_id)
        self.assertEqual(retained.metadata['ready_rollover']['source_as_of_date'], '2026-09-07')
        self.assertEqual(retained.metadata['fbs_accounting_unavailable']['dates'], ['2026-09-08'])
        rows = {r[1]:r for r in retained.sheets[0].rows}
        self.assertEqual(rows['SKU:1|ordersCount'][3],12)
        self.assertEqual(rows['SKU:1|our_wb_unit_cost_rub'][3],'')
        self.assertEqual(accounting.load(self.root), (self.book,self.version))
        with sqlite3.connect(self.runtime.db_path) as conn:
            selected,_ = _select_bindings(conn, _ready_headers(conn), ['2026-09-08'],
                self.runtime.load_current_state().bundle_version, '2026-09-09')
            self.assertEqual(selected[0].coverage,'exact')
            self.assertEqual(conn.execute("SELECT count(*) FROM sheet_vitrina_v1_ready_publications WHERE kind='ready_rollover' AND state='complete'").fetchone()[0],1)
            stamp = conn.execute('SELECT refreshed_at FROM '+ready.TABLE+' WHERE as_of_date=?',('2026-09-08',)).fetchone()[0]
            self.assertEqual(stamp,'2026-09-08T14:00:00Z')
        self.advance()
        self.assertEqual(self.runtime.load_sheet_vitrina_ready_snapshot('2026-09-08'),retained)

    def test_transaction_failure_rolls_back_both_targets(self):
        self.donor()
        original = ready_rollover.apply
        def fail(*args,**kwargs):
            original(*args,**kwargs)
            raise RuntimeError('after promotion')
        with patch.object(ready_rollover,'apply',side_effect=fail), self.assertRaisesRegex(RuntimeError,'after promotion'):
            self.advance()
        with sqlite3.connect(self.runtime.db_path) as conn:
            dates = {r[0] for r in conn.execute('SELECT as_of_date FROM '+ready.TABLE)}
        self.assertNotIn('2026-09-08',dates)
        self.assertNotIn('2026-09-09',dates)

    def test_donor_change_rejected(self):
        self.donor()
        current = self.runtime.load_current_state()
        expected = self.runtime.prepare_sheet_vitrina_ready_publication(bundle_version=current.bundle_version,as_of_date='2026-09-09')
        with ready.readonly(self.runtime.db_path) as conn:
            candidates = ready_rollover.prepare(conn,plan=make_plan('2026-09-09','2026-09-10'),expected=expected,default_date='2026-09-09')
        self.assertEqual(len(candidates),1)
        with sqlite3.connect(self.runtime.db_path) as conn:
            conn.execute('UPDATE '+ready.TABLE+" SET plan_json=plan_json||' ' WHERE as_of_date='2026-09-07'")
        with ready.readonly(self.runtime.db_path) as conn,self.assertRaises(ready.ReadyPublicationConflict):
            ready_rollover.check(conn,candidates)

    def test_other_ready_cohort_change_rejected(self):
        self.donor()
        with sqlite3.connect(self.runtime.db_path) as setup:
            setup.execute("PRAGMA journal_mode=WAL")
        original = ready_rollover.prepare
        def prepare(*args, **kwargs):
            result = original(*args, **kwargs)
            from packages.application.registry_upload_db_backed_runtime import _serialize_sheet_vitrina_plan
            with sqlite3.connect(self.runtime.db_path) as other:
                other.execute('BEGIN IMMEDIATE')
                ready.replace_ready(other, expected=ready.ExpectedReady(self.runtime.load_current_state().bundle_version,'2026-09-06',None),
                    plan_json=_serialize_sheet_vitrina_plan(make_plan('2026-09-06','2026-09-08')),
                    activated_at='a',refreshed_at='r',snapshot_id='other',plan_version='v')
            return result
        with patch.object(ready_rollover,'prepare',side_effect=prepare), self.assertRaisesRegex(ready.ReadyPublicationConflict,'ready_history_changed'):
            self.advance()

    def recovery_preview(self):
        donor = self.donor()
        with sqlite3.connect(self.runtime.db_path) as conn:
            operation, attempt, bundle, stamp = conn.execute("SELECT operation_id,attempt_id,bundle_version,finished_at FROM sheet_vitrina_v1_ready_publications WHERE as_of_date='2026-09-07' AND state='complete'").fetchone()
        cycle_id = 'a'*32
        path = self.root / 'sheet-vitrina-cycles' / (cycle_id+'.json')
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(dict(cycle_id=cycle_id, status="degraded", finished_at=stamp, final_versions=dict(
            history_status='published',history_window_from='2026-09-01',history_window_to='2026-09-08',
            history_edition_id='fixture-edition',history_vector_digest='fixture-vector',
            as_of_date='2026-09-07',bundle_version=bundle,publication_operation_id=operation,
            publication_attempt_id=attempt,refreshed_at=stamp,snapshot_id=donor.snapshot_id))))
        with patch.object(ready_rollover,'prepare',return_value=[]):
            self.advance()
        return ready_rollover_recovery.preview(self.runtime,cycle_id=cycle_id,day='2026-09-08')

    def test_reviewed_recovery_and_no_repeat(self):
        preview = self.recovery_preview()
        result = ready_rollover_recovery.apply(self.runtime,reviewed=preview,operation_id='fixture-recovery',finished_at='2026-09-10T15:00:00Z')
        self.assertEqual(result['status'],'complete')
        retained = self.runtime.load_sheet_vitrina_ready_snapshot('2026-09-08')
        self.assertEqual(retained.metadata['ready_rollover']['witness_cycle_id'],'a'*32)
        self.assertEqual(accounting.load(self.root),(self.book,self.version))
        with self.assertRaisesRegex(ready.ReadyPublicationConflict,'target_exists'):
            ready_rollover_recovery.apply(self.runtime,reviewed=preview,operation_id='fixture-recovery',finished_at='2026-09-10T15:00:00Z')

    def test_reviewed_recovery_rejects_changed_preview(self):
        preview = self.recovery_preview()
        preview['candidate']['refreshed_at'] = '2026-09-10T15:00:00Z'
        with self.assertRaisesRegex(ready.ReadyPublicationConflict,'preview_changed'):
            ready_rollover_recovery.apply(self.runtime,reviewed=preview,operation_id='fixture-recovery',finished_at='2026-09-10T15:00:00Z')

    def insert_foreign(self):
        from packages.application.registry_upload_db_backed_runtime import _serialize_sheet_vitrina_plan
        with sqlite3.connect(self.runtime.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            ready.replace_ready(conn,expected=ready.ExpectedReady('foreign-bundle','2026-09-08',None),
                plan_json=_serialize_sheet_vitrina_plan(make_plan('2026-09-08','2026-09-09')),
                activated_at='2026-09-01T00:00:00Z',refreshed_at='2026-09-08T15:00:00Z',snapshot_id='foreign',plan_version='v')

    def test_foreign_exact_remains_authoritative(self):
        self.donor()
        self.insert_foreign()
        self.advance()
        with sqlite3.connect(self.runtime.db_path) as conn:
            selected,_ = _select_bindings(conn,_ready_headers(conn),['2026-09-08'],self.runtime.load_current_state().bundle_version,'2026-09-09')
            self.assertEqual(selected[0].source_key,('foreign-bundle','2026-09-08'))
            self.assertEqual(conn.execute("SELECT count(*) FROM sheet_vitrina_v1_ready_publications WHERE kind='ready_rollover'").fetchone()[0],0)

    def test_recovery_rejects_foreign_accepted_date(self):
        preview = self.recovery_preview()
        self.insert_foreign()
        with self.assertRaisesRegex(ready.ReadyPublicationConflict,'day_not_missing_after_advance'):
            ready_rollover_recovery.preview(self.runtime,cycle_id=preview['cycle_id'],day=preview['day'])

    def test_healthy_binding_retains_original_owner(self):
        healthy, expected_book = make_book(self.root,quantity='1900',typed=True)
        accounting.save(self.root,healthy,expected=expected_book,operation_id='healthy')
        save(self.runtime,make_plan(),now=NOW)
        donor = self.runtime.load_sheet_vitrina_ready_snapshot('2026-09-07')
        current = self.runtime.load_current_state()
        expected = self.runtime.prepare_sheet_vitrina_ready_publication(bundle_version=current.bundle_version,as_of_date='2026-09-09')
        with ready.readonly(self.runtime.db_path) as conn:
            candidates = ready_rollover.prepare(conn,plan=make_plan('2026-09-09','2026-09-10'),expected=expected,default_date='2026-09-09')
        with sqlite3.connect(self.runtime.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            ready_rollover.apply(conn,candidates,operation_id='healthy-rollover',finished_at='2026-09-10T14:00:00Z',book_version=accounting.load(self.root)[1])
        retained = self.runtime.load_sheet_vitrina_ready_snapshot('2026-09-08')
        self.assertEqual(retained.metadata['fbs_accounting_bindings'],donor.metadata['fbs_accounting_bindings'])
        self.assertEqual(retained.metadata['fbs_accounting_targets']['2026-09-08'],donor.metadata['ready_publication_target'])
        self.assertIsNotNone(accounting.load_management_inventory(self.root,retained,now=NOW))

    def test_recovery_rejects_mutated_donor_witness(self):
        preview = self.recovery_preview()
        with sqlite3.connect(self.runtime.db_path) as conn:
            conn.execute('UPDATE '+ready.TABLE+" SET plan_json=plan_json||' ' WHERE as_of_date='2026-09-07'")
        with self.assertRaisesRegex(ready.ReadyPublicationConflict,'publication_witness_changed'):
            ready_rollover_recovery.preview(self.runtime,cycle_id=preview['cycle_id'],day=preview['day'])

    def test_header_budget_fails_closed(self):
        self.donor()
        with patch.object(ready_rollover,'MAX_BYTES',1), self.assertRaisesRegex(ready.ReadyPublicationConflict,'header_limit'):
            self.advance()


if __name__ == '__main__':
    unittest.main(defaultTest="RolloverTests")
