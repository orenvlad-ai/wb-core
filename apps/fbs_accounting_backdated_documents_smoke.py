#!/usr/bin/env python3
"""LOCAL native receipt clocks, owned HTTP seam and DELETE intent recovery.

All sources are synthetic temporary fixtures. No production access, collector,
reconfirmation or bypass of the typed History/Finance completion authority.
"""
from contextlib import ExitStack, closing
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import json
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps import fbs_accounting_historical_publication_smoke as publication_fixture
from packages.application import fbs_accounting_historical_cycle as cycle
from packages.application import fbs_accounting_historical_publication as publication
from packages.application import fbs_accounting_historical_revision as revision
from packages.application import fbs_accounting_historical_revision_writer as staging
from packages.application import fbs_accounting_historical_sources as sources
from packages.application import fbs_accounting_runtime as accounting
from packages.application import operator_warehouse_documents as operations
from packages.application import ready_publication as ready
from packages.application.ff_pool_documents import DOCUMENTS_TABLE, REQUESTS_TABLE
from packages.application.fbs_snapshot_cost import canonical, fingerprint
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock

REQUEST = 'native:new-late-receipt'
NOW = publication_fixture.NOW


class BackdatedDocuments(unittest.TestCase):
    def setUp(self):
        self.fixture = publication_fixture.HistoricalPublication()
        self.fixture.service_receipt = True
        self.fixture.advancing_receipt_clock = True
        self.fixture.confirmation_actor = 'final confirmer'
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db = self.fixture.db
        self.runtime = SimpleNamespace(db_path=self.db, runtime_dir=self.fixture.runtime)
        with ready.readonly(self.db) as conn:
            self.confirmation_before = dict(conn.execute(f'SELECT * FROM {operations.TABLE} WHERE request_id=?', (REQUEST,)).fetchone())

    def status(self):
        return ready.publication_status(self.db, operation_id='local-late-publication', attempt_id='1')

    def prepared(self, boundary='after_intent'):
        def crash(point):
            if point == boundary:
                raise RuntimeError('LOCAL prepared interruption')
        with self.assertRaisesRegex(RuntimeError, 'prepared interruption'):
            self.fixture.publish(fault_injector=crash)
        self.assertEqual(self.status()['state'], 'prepared')

    def confirmation(self, **changes):
        # Adversarial corruption in this LOCAL DB, never a product migration.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute('DROP TRIGGER IF EXISTS ff_operator_confirmation_immutable')
            row = deepcopy(self.confirmation_before)
            if 'source' in changes:
                source = json.loads(row['source_json'])
                source.update(changes.pop('source'))
                changes.update(source_json=canonical(source), source_digest=fingerprint(source))
            changes = {**{k: row[k] for k in ('source_json', 'source_digest', 'accepted_at', 'actor')}, **changes}
            conn.execute(f'UPDATE {operations.TABLE} SET '+','.join(k+'=?' for k in changes)+' WHERE request_id=?', (*changes.values(), REQUEST))
            # Restore the exact native immutability fence so publication
            # refusals prove changed source bytes, not merely a missing trigger.
            operations.ensure_schema(conn)

    def verify_confirmation(self):
        with ready.readonly(self.db) as conn:
            return sources.verify_cohort_confirmations(conn, plan=self.fixture.plan, capture=self.fixture.cap)

    def test_real_advancing_native_clocks_distinct_confirmer_and_exact_raw_proof(self):
        with ready.readonly(self.db) as conn:
            request = dict(conn.execute(f'SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?', (REQUEST,)).fetchone())
            accepted = dict(conn.execute(f'SELECT * FROM {operations.TABLE} WHERE request_id=?', (REQUEST,)).fetchone())
            doc = dict(conn.execute(f'SELECT * FROM {DOCUMENTS_TABLE} WHERE document_id=?', (request['posted_document_id'],)).fetchone())
            staged = staging._confirmation(conn, self.fixture.cap, self.fixture.plan['receipt_document_ids'])
        self.assertLess(revision._time(request['accepted_at']), revision._time(accepted['accepted_at']))
        self.assertLess(revision._time(accepted['accepted_at']), revision._time(doc['posted_at']))
        self.assertLess(revision._time(doc['posted_at']), revision._time(request['posted_at']))
        self.assertNotEqual(accepted['actor'], request['actor'])
        self.assertEqual(json.loads(accepted['source_json'])['preview_actor'], request['actor'])
        self.assertEqual(staged[doc['document_id']]['request']['posted_at'], request['posted_at'])
        self.assertEqual(self.fixture.manifest['operator_confirmation'][REQUEST]['accepted_at'], accepted['accepted_at'])
        before_docs = self.document_image()
        result = self.fixture.publish()
        self.assertEqual(result['status'], 'published')
        self.assertFalse(result['operator_complete'])
        self.assertEqual(self.document_image(), before_docs)
        self.assertEqual(accounting.load(self.fixture.runtime, version=self.fixture.before), (self.fixture.book, self.fixture.before))

    def document_image(self):
        with ready.readonly(self.db) as conn:
            return {table: [dict(r) for r in conn.execute('SELECT * FROM '+table+' ORDER BY document_id')]
                    for table in (DOCUMENTS_TABLE, 'sheet_vitrina_v1_ff_pool_document_lines', 'sheet_vitrina_v1_ff_pool_document_expense_lines')}

    def test_request_clocks_malformed_naive_reversed_future_or_missing_refuse(self):
        cases = [('accepted_at', 'not-a-timestamp'), ('accepted_at', None),
                 ('accepted_at', NOW.replace(tzinfo=None).isoformat()),
                 ('accepted_at', (NOW+timedelta(days=1)).isoformat()),
                 ('posted_at', 'not-a-timestamp'), ('posted_at', ''),
                 ('posted_at', NOW.replace(tzinfo=None).isoformat()),
                 ('posted_at', '2026-09-10T11:00:00Z'),
                 ('posted_at', (NOW+timedelta(seconds=1)).isoformat())]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                cap = deepcopy(self.fixture.cap)
                cap['native_requests_by_id'][REQUEST][field] = value
                result = revision.build_historical_revision_plan(self.fixture.book, cap,
                    receipt_document_ids=self.fixture.plan['receipt_document_ids'])
                self.assertEqual(result['status'], 'blocked', result)
                self.assertNotIn('candidate_book', result)
        self.assertEqual(accounting.load(self.fixture.runtime)[1], self.fixture.before)

    def test_confirmation_clocks_malformed_naive_reversed_future_or_missing_refuse(self):
        for value in ('not-a-timestamp', '', '2026-09-10T12:00:00',
                      '2026-09-10T11:59:59Z', (NOW+timedelta(seconds=1)).isoformat()):
            with self.subTest(value=value):
                self.confirmation(accepted_at=value)
                with self.assertRaises(ValueError):
                    self.verify_confirmation()

    def test_equivalent_aware_offset_is_valid_and_stays_exact_pinned(self):
        with ready.readonly(self.db) as conn:
            actual = conn.execute(f'SELECT accepted_at FROM {operations.TABLE} WHERE request_id=?', (REQUEST,)).fetchone()[0]
        offset = revision._time(actual).astimezone(timezone(timedelta(hours=3))).isoformat()
        self.confirmation(accepted_at=offset)
        self.assertEqual(self.verify_confirmation()[REQUEST]['accepted_at'], offset)
        # The same instant cannot replace the raw authority of an existing intent.
        with self.assertRaises(ValueError):
            self.fixture.publish()
        self.assertEqual(accounting.load(self.fixture.runtime)[1], self.fixture.before)

    def test_empty_confirmer_foreign_or_missing_preview_actor_refuse(self):
        for change in ({'actor': ''}, {'actor': '   '}, {'source': {'preview_actor': 'foreign preview'}},
                       {'source': {'preview_actor': None}}):
            with self.subTest(change=change):
                self.confirmation(**change)
                with self.assertRaisesRegex(ValueError, 'historical_operator_confirmation_authority_mismatch'):
                    self.verify_confirmation()

    def test_immutable_confirmation_and_request_actor_cannot_be_rewritten(self):
        with closing(sqlite3.connect(self.db)) as conn:
            for sql in (f"UPDATE {operations.TABLE} SET actor='foreign' WHERE request_id=?",
                        f"UPDATE {operations.TABLE} SET accepted_at='foreign' WHERE request_id=?",
                        f"UPDATE {REQUESTS_TABLE} SET actor='foreign' WHERE request_id=?"):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(sql, (REQUEST,))

    def test_missing_primary_document_has_explicit_confirmation_error(self):
        capture = deepcopy(self.fixture.cap)
        capture['documents'] = [d for d in capture['documents'] if d['document_id'] not in self.fixture.plan['receipt_document_ids']]
        with ready.readonly(self.db) as conn, self.assertRaisesRegex(ValueError, 'historical_confirmation_primary_document_missing'):
            sources.verify_cohort_confirmations(conn, plan=self.fixture.plan, capture=capture)

    def test_prepared_recovery_delete_closes_reader_and_preserves_exact_identity(self):
        self.recovery_after('after_book')

    def test_prepared_intent_recovery_delete_closes_reader_before_first_book_commit(self):
        self.recovery_after('after_intent')

    def recovery_after(self, boundary):
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
        before_docs = self.document_image()
        self.prepared(boundary)
        with warehouse_functional_job_lock(self.fixture.runtime):
            recovered = cycle.refresh(self.runtime, now=NOW)
        self.assertEqual(recovered['manifest_digest'], self.fixture.manifest['manifest_digest'])
        self.assertEqual(self.status()['state'], 'complete')
        self.assertEqual(self.document_image(), before_docs)
        self.assertFalse(recovered['finance_complete'])
        self.assertFalse(recovered['history_complete'])
        self.assertFalse(recovered['operator_complete'])
        self.assertNotEqual(operations.read_acceptance(self.db, REQUEST)['state'], 'completed')

    def test_prepared_recovery_changed_source_refuses_same_intent(self):
        self.prepared()
        self.confirmation(source={'effect_digest': fingerprint('foreign effect')})
        with warehouse_functional_job_lock(self.fixture.runtime), self.assertRaises(ValueError):
            cycle.refresh(self.runtime, now=NOW)
        self.assertEqual(self.status()['state'], 'prepared')
        self.assertEqual(accounting.load(self.fixture.runtime)[1], self.fixture.before)

    def test_prepared_recovery_changed_book_refuses_same_intent(self):
        self.prepared()
        foreign = deepcopy(self.fixture.book)
        foreign['prepared_at'] = (NOW+timedelta(seconds=1)).isoformat()
        with warehouse_functional_job_lock(self.fixture.runtime):
            new = accounting._save_book(self.fixture.runtime, foreign, expected=self.fixture.before, operation_id='LOCAL foreign book')
            with self.assertRaises(ValueError):
                cycle.refresh(self.runtime, now=NOW)
        self.assertEqual(accounting.load(self.fixture.runtime)[1], new)
        self.assertEqual(self.status()['state'], 'prepared')

    def test_prepared_recovery_changed_ready_refuses_same_intent(self):
        self.prepared()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(f"UPDATE {ready.TABLE} SET plan_json='{{\"foreign\":true}}'")
        with warehouse_functional_job_lock(self.fixture.runtime), self.assertRaises(ValueError):
            cycle.refresh(self.runtime, now=NOW)
        self.assertEqual(self.status()['state'], 'prepared')
        self.assertEqual(accounting.load(self.fixture.runtime)[1], self.fixture.before)

    def test_borrowed_window_reader_cannot_start_historical_writer(self):
        from packages.application.web_vitrina_window_read_context import window_read_context
        self.prepared()
        before = self.status()
        with window_read_context(self.db, runtime_dir=self.fixture.runtime):
            with warehouse_functional_job_lock(self.fixture.runtime):
                with self.assertRaisesRegex(ValueError, 'historical_refresh_window_reader_active'):
                    cycle.refresh(self.runtime, now=NOW)
                with self.assertRaisesRegex(ValueError, 'historical_publication_window_reader_active'):
                    publication.publish(self.fixture.manifest, runtime_dir=self.fixture.runtime)
        self.assertEqual(self.status(), before)

    def owned_entrypoint(self):
        from apps.fbs_accounting_historical_history_smoke import complete_local_metadata
        from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime, _deserialize_sheet_vitrina_plan, _serialize_sheet_vitrina_plan
        from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
        from packages.business_time import default_business_as_of_date
        complete_local_metadata(self.db)
        with ready.readonly(self.db) as conn:
            raw = ready.capture_expected(conn, bundle_version='fixture-v1', as_of_date=NOW.date().isoformat()).plan_json
        envelope = replace(_deserialize_sheet_vitrina_plan(raw), as_of_date=default_business_as_of_date(NOW))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(f'INSERT INTO {ready.TABLE}(bundle_version,as_of_date,plan_json,activated_at,snapshot_id,plan_version,refreshed_at) VALUES(?,?,?,?,?,?,?)',
                ('fixture-v1', envelope.as_of_date, _serialize_sheet_vitrina_plan(envelope), NOW.isoformat(), envelope.snapshot_id, envelope.plan_version, NOW.isoformat()))
        entry = RegistryUploadHttpEntrypoint.__new__(RegistryUploadHttpEntrypoint)
        entry.runtime = RegistryUploadDbBackedRuntime(self.fixture.runtime)
        entry.warehouse_update_journal = Mock()
        entry.calculation_parameters_block = SimpleNamespace(prepare_functional_economics_backup=lambda: {})
        entry.wb_supplies_block = SimpleNamespace(sync_functional_sources=lambda **kw: {}, collect_all_due_transit_costs=lambda: {}, reconcile_functional_ff_state=lambda: {})
        entry.our_wb_cost_block = SimpleNamespace(materialize_wb_supply_cost_layers=lambda **kw: 0)
        entry.warehouse_functional_block = SimpleNamespace(build_sync_plan=lambda: {'plan_fingerprint': 'LOCAL preceding publication', 'diff': {}},
            apply_plan=lambda *args, **kw: {}, record_failed_sync=lambda error: None)
        entry.inventory_planning = SimpleNamespace(current=Mock(side_effect=RuntimeError('LOCAL stop after accounting')))
        return entry

    def owned_call(self, entry):
        from packages.application.business_data_heavy_admission import heavy_admitted
        with ExitStack() as stack:
            for name, result in (('warehouse_recovery_sync_retention.run_bounded_recovery_retention', {}),
                                 ('operator_ff_overhead.drain', {}), ('operator_warehouse_documents.drain', {'request_ids': []})):
                stack.enter_context(patch('packages.application.'+name, return_value=result))
            stack.enter_context(heavy_admitted(entry.runtime.runtime_dir, operation='cycle'))
            metrics = stack.enter_context(warehouse_functional_job_lock(entry.runtime.runtime_dir))
            return entry._handle_owned_warehouse_manual_sync_request(owner_token=metrics['owner_token'], durable_run_id='LOCAL owned route')

    def test_actual_owned_http_route_publishes_historical_suffix_before_dependents(self):
        entry = self.owned_entrypoint()
        refresh = cycle.refresh
        before_docs = self.document_image()
        with patch.object(cycle, 'refresh', side_effect=lambda runtime: refresh(runtime, now=NOW)) as historical, \
             patch.object(accounting, 'refresh', side_effect=AssertionError('ordinary fallback after historical publication')) as ordinary:
            with self.assertRaisesRegex(RuntimeError, 'LOCAL stop after accounting'):
                self.owned_call(entry)
        historical.assert_called_once_with(entry.runtime)
        ordinary.assert_not_called()
        self.assertNotEqual(accounting.load(self.fixture.runtime)[1], self.fixture.before)
        self.assertEqual(self.document_image(), before_docs)
        self.assertEqual(accounting.load(self.fixture.runtime, version=self.fixture.before), (self.fixture.book, self.fixture.before))
        self.assertIsNotNone(cycle.pending_receipt(entry.runtime))
        self.assertNotEqual(operations.read_acceptance(self.db, REQUEST)['state'], 'completed')

    def test_actual_owned_http_route_ordinary_fallback_only_on_none(self):
        entry = self.owned_entrypoint()
        with patch.object(cycle, 'refresh', return_value=None), patch.object(accounting, 'refresh', return_value={'status': 'not_active'}) as ordinary:
            with self.assertRaisesRegex(RuntimeError, 'LOCAL stop after accounting'):
                self.owned_call(entry)
        ordinary.assert_called_once_with(entry.runtime.runtime_dir, ready_runtime=entry.runtime)
        entry.inventory_planning.current.reset_mock()
        with patch.object(cycle, 'refresh', side_effect=ValueError('LOCAL historical refusal')), patch.object(accounting, 'refresh') as ordinary:
            with self.assertRaisesRegex(ValueError, 'LOCAL historical refusal'):
                self.owned_call(entry)
        ordinary.assert_not_called()
        self.assertEqual(accounting.load(self.fixture.runtime)[1], self.fixture.before)

if __name__ == '__main__':
    unittest.main()
