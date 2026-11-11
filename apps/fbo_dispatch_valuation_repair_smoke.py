#!/usr/bin/env python3
"""Held Production Apply for a historical FBO dispatch valuation."""
from contextlib import closing, nullcontext
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps import fbo_supply_lifecycle_smoke as fixtures
from apps.production_apply_contract import AdapterError
from apps.production_apply_launcher import ApplyError, execute
from packages.application import fbo_dispatch_cost_source as source
from packages.application import fbo_dispatch_valuation as valuation
from packages.application import fbo_lifecycle_activation as module
from packages.application import fbo_supply_lifecycle as lifecycle
from packages.application import fbs_accounting_runtime as accounting


class RepairTests(unittest.TestCase):
    operation = 'fixture-dispatch-repair'

    def setUp(self):
        self.fixture = fixtures.LifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.runtime.runtime_dir.resolve()
        self.db = self.fixture.runtime.db_path.resolve()
        self.fixture.seed('1', [(101, 6)])
        self.fixture.cutover()
        self.fixture.assign('1')
        self.fixture.seed('1', [(101, 6)], 6)
        old = self.fixture.service.reconcile('1')
        self.assertEqual(old['status'], 'debited')
        self.dispatch = old['operation_id']
        self.day = fixtures.NOW[:10]
        book = dict(schema=accounting.SCHEMA, active=True, effective_date='2026-09-08',
                    prepared_at=self.day + 'T11:00:00Z', shared_days={}, wb_days={}, retained_days={},
                    presentations={}, state=dict(baseline=dict(absorbed_documents={}),
                        observed_documents={}, pending_documents=[],
                        periods={self.day: dict(status='open', quality='preliminary', applied_documents={}, document_cost_state=dict(
                            fbo_rows={'facility-a:FBO:101': dict(quantity='10', capital_rub='200')}))}))
        donor = accounting._save_book(self.root, book, expected=None, operation_id='fixture-donor')
        self.current_book = accounting._save_book(self.root, {**book, 'publication_error': 'fixture'},
                                                  expected=donor, operation_id='fixture-invalid-current')
        self.request = dict(action=module.REPAIR_ACTION, target_dispatch_operation_id=self.dispatch,
                            book_version=donor, actor='fixture', approval_reference='test approval')
        self.adapter = module.FboLifecycleActivationAdapter()
        for owner, name, replacement in (
            (module.shared, 'target', lambda request: (self.root, self.db)),
            (module.shared, '_held_binding', lambda *args: {'window': 'fixture'}),
            (module, 'require_held_activation', lambda *args: {'window': 'fixture'}),
            (module, 'admitted', lambda *args: nullcontext()),
        ):
            patcher = patch.object(owner, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def apply(self, preview):
        return execute(action='apply', adapter_name=module.CONTRACT, operation_id=self.operation,
                       request=self.request, expected_prestate=preview['prestate_sha256'],
                       expected_candidate=preview['candidate_sha256'], adapters={module.CONTRACT: self.adapter})

    def valuation_count(self, conn):
        if valuation.TABLE not in lifecycle._tables(conn):
            return 0
        return conn.execute(f'SELECT COUNT(*) FROM {valuation.TABLE}').fetchone()[0]

    def test_pinned_historical_preview_one_append_and_native_rows_unchanged(self):
        with closing(self.fixture.conn()) as conn:
            before = module.protected(conn, repair_target=self.dispatch)
            old = source.capture_documents(conn)[0]
        preview = self.adapter.preview(self.request, self.operation)
        self.assertEqual(preview['scope']['native_digest'], old['fingerprint'])
        self.assertEqual(preview['scope']['current_book_version'], self.current_book)
        self.assertEqual(preview['scope']['valuation_lines'][0]['capital_delta_rub'], '-120')
        self.assertEqual(self.adapter.readback(self.request, self.operation)['state'], 'not_submitted')
        with closing(self.fixture.conn()) as conn:
            self.assertEqual(self.valuation_count(conn), 0)
            self.assertEqual(source.capture_documents(conn)[0], old)

        result = self.apply(preview)
        self.assertEqual(result['state'], 'applied')
        self.assertEqual(result['readback']['valuation_digest'], preview['candidate_sha256'])
        self.assertTrue(result['readback']['protected_scope_unchanged_now'])
        self.assertEqual(self.fixture.balance(), (4, Decimal('40')))
        with closing(self.fixture.conn()) as conn:
            self.assertEqual(module.protected(conn, repair_target=self.dispatch), before)
            doc = source.capture_documents(conn)[0]
            self.assertEqual(doc['cost_document']['lines'][0]['capital_delta_rub'], '-120')
            self.assertEqual(self.valuation_count(conn), 1)
        self.assertEqual(accounting.load(self.root)[1], self.current_book)
        with patch.object(self.adapter, 'apply', side_effect=AssertionError('second submit')):
            self.assertEqual(self.apply(preview)['state'], 'applied')
        with self.assertRaisesRegex(ValueError, 'repair_target_unavailable'):
            self.adapter.preview(self.request, 'fixture-repair-new-id')

    def test_stale_or_tampered_preview_cannot_claim(self):
        preview = self.adapter.preview(self.request, self.operation)
        altered = deepcopy(preview)
        altered['prepared']['candidate']['lines'][0]['capital_delta_rub'] = '-1'
        with self.assertRaisesRegex(AdapterError, 'preview_changed'):
            self.adapter.apply(self.request, self.operation, altered)
        self.fixture.seed('2', [(101, 1)])
        with self.assertRaisesRegex(ApplyError, 'candidate-or-prestate-drift'):
            self.apply(preview)
        self.assertEqual(self.adapter.readback(self.request, self.operation)['state'], 'not_submitted')

    def test_current_book_drift_invalidates_preview(self):
        preview = self.adapter.preview(self.request, self.operation)
        current, version = accounting.load(self.root)
        accounting._save_book(self.root, {**current, 'publication_error': 'new fixture failure'},
                              expected=version, operation_id='fixture-new-invalid-current')
        with self.assertRaisesRegex(ApplyError, 'candidate-or-prestate-drift'):
            self.apply(preview)
        self.assertEqual(self.adapter.readback(self.request, self.operation)['state'], 'not_submitted')

    def test_current_book_target_already_recorded_or_closed_cannot_claim(self):
        current, version = accounting.load(self.root)
        for change in ('observed', 'applied', 'closed'):
            with self.subTest(change=change):
                book = deepcopy(current)
                state = book['state']
                period = state['periods'][self.day]
                if change == 'observed':
                    state['observed_documents'][self.dispatch] = {'fingerprint': 'fixture'}
                elif change == 'applied':
                    period['applied_documents'][self.dispatch] = 'fixture'
                else:
                    period['status'] = 'closed'
                version = accounting._save_book(self.root, book, expected=version,
                                                operation_id='fixture-current-' + change)
                reason = 'repair_period_not_open' if change == 'closed' else 'repair_target_already_accounted'
                with self.assertRaisesRegex(AdapterError, reason):
                    self.adapter.preview(self.request, self.operation)
                self.assertEqual(self.adapter.readback(self.request, self.operation)['state'], 'not_submitted')

    def test_tampered_intent_or_persisted_candidate_cannot_read_back_applied(self):
        preview = self.adapter.preview(self.request, self.operation)
        self.assertEqual(self.apply(preview)['state'], 'applied')
        _, claim = module.paths(self.root, self.operation)
        raw = claim.read_bytes()
        altered = json.loads(raw)
        altered['preview']['prepared']['candidate']['lines'][0]['capital_delta_rub'] = '-1'
        claim.write_text(json.dumps(altered))
        with self.assertRaisesRegex(AdapterError, 'repair_intent_changed'):
            self.adapter.readback(self.request, self.operation)
        claim.write_bytes(raw)
        with closing(self.fixture.conn()) as conn, conn:
            conn.execute(f'DROP TRIGGER {valuation.TABLE}_no_update')
            conn.execute(f'UPDATE {valuation.TABLE} SET valuation_json=?', ('{}',))
        self.assertEqual(self.adapter.readback(self.request, self.operation)['state'], 'ambiguous')

    def test_protected_write_rolls_back_and_claim_is_readback_only(self):
        preview = self.adapter.preview(self.request, self.operation)
        append = valuation.append
        _, claim = module.paths(self.root, self.operation)

        def change_balance(conn, dispatch, candidate):
            self.assertTrue(claim.is_file())
            append(conn, dispatch, candidate)
            conn.execute(f'UPDATE {lifecycle.BALANCES_TABLE} SET quantity=999')

        with patch.object(valuation, 'append', change_balance):
            result = self.apply(preview)
        self.assertEqual(result['state'], 'ambiguous')
        self.assertEqual(result['readback']['failure']['error_code'], 'fbo_activation_protected_state_changed')
        self.assertEqual(self.fixture.balance(), (4, Decimal('40')))
        with closing(self.fixture.conn()) as conn:
            self.assertEqual(self.valuation_count(conn), 0)
        with patch.object(self.adapter, 'apply', side_effect=AssertionError('unsafe retry')):
            with self.assertRaisesRegex(ApplyError, 'operation-not-ready'):
                self.apply(preview)

    def test_commit_with_lost_readback_resolves_from_exact_row(self):
        preview = self.adapter.preview(self.request, self.operation)
        readback = self.adapter.readback

        def lose_once(request, operation):
            result = readback(request, operation)
            if result['state'] == 'applied' and 'failure' not in result:
                raise OSError('fixture readback transport lost')
            return result

        with patch.object(self.adapter, 'readback', lose_once):
            result = self.apply(preview)
        self.assertEqual(result['state'], 'applied')
        self.assertEqual(result['reason'], 'submit-ambiguous-readback-only')
        self.assertEqual(result['readback']['valuation_digest'], preview['candidate_sha256'])
        self.assertEqual(result['readback']['failure']['stage'], 'post_commit_readback')
        with patch.object(self.adapter, 'apply', side_effect=AssertionError('unsafe retry')):
            self.assertEqual(self.apply(preview)['state'], 'applied')


if __name__ == '__main__':
    unittest.main()
