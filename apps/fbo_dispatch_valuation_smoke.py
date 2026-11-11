#!/usr/bin/env python3
"""Canonical FBO dispatch cost, native CAS and both downstream consumers."""
from copy import deepcopy
from decimal import Decimal, localcontext
from pathlib import Path
import json
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.fbo_supply_lifecycle_smoke import LifecycleTests
from apps import fbs_document_cost_smoke as fixture
from packages.application import fbo_dispatch_valuation as valuation
from packages.application import fbo_dispatch_cost_source as source
from packages.application import fbo_supply_lifecycle as lifecycle
from packages.application.fbs_snapshot_cost import initialize_candidate, evaluate_candidate, fingerprint
from packages.application.warehouse_functional import _ff_ledger_line_cost_snapshot

DAY = '2026-11-09'


class ValuationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = LifecycleTests()
        self.fixture.setUp()
        self.fixture.legacy_cost_capture.stop()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.seed('1', [(101, 6)])
        self.fixture.cutover()
        self.fixture.assign('1')
        self.fixture.seed('1', [(101, 6)], 6)
        self.book = dict(active=True, effective_date='2026-09-08', prepared_at=DAY+'T11:00:00Z',
            state=dict(observed_documents={}, pending_documents=[], periods={DAY: dict(status='open',quality='preliminary',
                document_cost_state=dict(fbo_rows={'facility-a:FBO:101': dict(quantity='10',capital_rub='200')}))}))
        self.version = fingerprint(self.book)

    def dispatch(self, book=None, documents=None):
        with patch('packages.application.fbs_accounting_runtime.load', return_value=(book or self.book, fingerprint(book or self.book))), \
             patch('packages.application.fbs_snapshot_cost_sources._documents', return_value=documents or []):
            return self.fixture.service.reconcile('1')

    def test_canonical_partial_cost_native_cas_get_and_consumer_parity(self):
        result = self.dispatch()
        self.assertEqual(result['status'], 'debited')
        self.assertEqual(self.fixture.balance(), (4, Decimal('40')))
        self.assertEqual(result['frozen_costs'][0]['capital_delta_rub'], '-120')
        self.assertEqual(self.fixture.service.detail('1')['frozen_costs'], result['frozen_costs'])
        from packages.application.ff_warehouse_documents import FfWarehouseDocumentView
        view=FfWarehouseDocumentView(db_path=self.fixture.runtime.db_path)
        public=view.detail('ffop:'+result['operation_id'])['document']
        self.assertEqual(Decimal(public['lines'][0]['capital_rub']),Decimal('-120'))
        listed=next(d for d in view.page(limit=100)['documents'] if d['document_id']=='ffop:'+result['operation_id'])
        self.assertEqual(Decimal(listed['total_capital_rub']),Decimal('-120'))
        with self.fixture.conn() as conn:
            doc = source.capture_documents(conn)[0]
            rows = [dict(r) for r in conn.execute(f'SELECT * FROM {lifecycle.AGGREGATE_LINES} WHERE operation_id=?', (result['operation_id'],))]
            raw = json.loads(rows[0]['raw_json'])
            self.assertEqual(raw['cost_snapshot']['capital_delta_rub'], '-60')
            normalized = valuation.normalize_warehouse_lines(conn, rows)
            frozen = _ff_ledger_line_cost_snapshot(normalized[0])
            self.assertEqual(frozen['capital_delta_rub'], Decimal('-120'))
            self.assertEqual(doc['cost_document']['lines'][0]['capital_delta_rub'], '-120')
            self.assertEqual(json.loads(rows[0]['raw_json']), raw)
            from packages.application.warehouse_functional import ensure_warehouse_functional_schema
            from packages.application.ff_inventory_reconciliation import _historical_supply_cost_facts
            ensure_warehouse_functional_schema(conn)
            returned=_historical_supply_cost_facts(conn,supply_id='1',debit_lines=rows,debit_created_at=DAY+'T12:00:00Z')
            self.assertEqual(Decimal(returned[101]['capital_rub']),Decimal('120'))
            self.assertEqual(returned[101]['version_id'],self.version)
        # Accounting retains 80, WB receives 120, and total canonical capital is 200.
        doc.update(business_date='2026-09-08', posted_at='2026-09-08T12:00:00Z')
        initial = initialize_candidate(fixture.capture(fbo=[('facility-a',101,10,'20')]))
        calculated = evaluate_candidate(initial, fixture.capture('2026-09-08', [doc]))
        remaining = calculated['periods']['2026-09-08']['document_cost_state']['fbo_rows']['facility-a:FBO:101']
        self.assertEqual((remaining['quantity'], Decimal(remaining['capital_rub'])), ('4', Decimal('80')))
        self.assertEqual(Decimal(remaining['capital_rub']) - frozen['capital_delta_rub'], Decimal('200'))
        # Existing lifecycle retry cannot create a second valuation or debit.
        self.fixture.service.reconcile('1')
        with self.fixture.conn() as conn:
            self.assertEqual(conn.execute(f'SELECT COUNT(operation_id) FROM {valuation.TABLE}').fetchone()[0], 1)

    def test_full_depletion_leaves_no_stranded_expense(self):
        self.fixture.seed('1', [(101,10)], 6)
        result = self.dispatch()
        self.assertEqual(result['status'], 'debited')
        line = result['frozen_costs'][0]
        self.assertEqual((line['capital_delta_rub'], line['after_capital_rub'], line['after_quantity']), ('-200','0',0))
        doc = dict(document_id='full', operation_id='full', root_document_id='full',kind=source.KIND,
            business_date='2026-09-08',posted_at='2026-09-08T12:00:00Z',fingerprint='full',events=[],
            fbo_lifecycle_native=dict(contract=source.CONTRACT),
            cost_document=dict(contract=source.CONTRACT,domain={},lines=[line],expense_lines=[],movements=[line],relations=[]))
        initial = initialize_candidate(fixture.capture(fbo=[('facility-a',101,10,'20')]))
        calculated = evaluate_candidate(initial, fixture.capture('2026-09-08',[doc]))
        row = calculated['periods']['2026-09-08']['document_cost_state']['fbo_rows']['facility-a:FBO:101']
        self.assertEqual((row['quantity'],Decimal(row['capital_rub'])),('0',Decimal(0)))
        # The previous native-only debit reproduces the actual stranded-money defect.
        bad = deepcopy(doc)
        bad['cost_document']['lines'][0]['capital_delta_rub'] = '-100'
        with self.assertRaisesRegex(ValueError, 'expense_without_cost_mass'):
            evaluate_candidate(initial, fixture.capture('2026-09-08',[bad]))

    def test_money_only_new_document_defers_without_moving_stock(self):
        result = self.dispatch(documents=[dict(document_id='new-fee',fingerprint='fee')])
        self.assertEqual((result['status'],result['reason']),('reserved','accounting_dispatch_valuation_pending'))
        self.assertEqual(result['valuation_blocker'],'fbo_dispatch_accounting_documents_changed')
        self.assertEqual(self.fixture.balance(),(10,Decimal(100)))
        self.assertEqual(result['reserved_qty'],6)
        self.assertEqual(self.dispatch()['status'],'debited')

    def test_same_day_both_fee_keeps_opening_receipts_weights(self):
        initial=initialize_candidate(fixture.capture('2026-11-08', rows=[('facility-a',101,10,'10')],
                                                     fbo=[('facility-a',101,10,'10')]))
        fee=fixture.overhead(amount='100',scope='both',facility='facility-a',day=DAY)
        donor=evaluate_candidate(initial,fixture.capture(DAY,[fee],rows=[('facility-a',101,10,'10')]))
        book=dict(self.book,state=donor)
        for quantity in (6,10):
            from packages.application.current_fbo_supply_close import allocate_capital
            native=dict(nm_id=101,facility_id='facility-a',pool='FBO',quantity_delta=-quantity,
                        **allocate_capital(10,'100',quantity))
            candidate=valuation.prepare(book,fingerprint(book),[fee],business_date=DAY,lines=[native])
            line=candidate['lines'][0]
            dispatch=fixture.doc('canonical-dispatch',source.KIND,lines=[line],day=DAY)
            dispatch.update(operation_id='canonical-dispatch',fbo_lifecycle_native=dict(contract=source.CONTRACT))
            dispatch['dispatch_valuation']=dict(valuation_digest=fingerprint(candidate),book_version=fingerprint(book))
            dispatch['cost_document']['contract']=source.CONTRACT
            result=evaluate_candidate(initial,fixture.capture(DAY,[fee,dispatch],rows=[('facility-a',101,10,'10')]))
            period=result['periods'][DAY]
            row=period['document_cost_state']['fbo_rows']['facility-a:FBO:101']
            self.assertEqual(Decimal(row['capital_rub']),Decimal(10-quantity)*15)
            allocations=period['document_valuation']['allocations']
            self.assertEqual(allocations[0]['weights'],{'facility-a:FBS:101':10,'facility-a:FBO:101':10})
            self.assertEqual(allocations[0]['pool_amounts_kopecks'],{'FBS':5000,'FBO':5000})
            native_only=deepcopy(dispatch);native_only.pop('dispatch_valuation')
            if quantity==10:
                with self.assertRaisesRegex(ValueError,'negative_document_cost_basis'):
                    evaluate_candidate(initial,fixture.capture(DAY,[fee,native_only],rows=[('facility-a',101,10,'10')]))
            else:
                old=evaluate_candidate(initial,fixture.capture(DAY,[fee,native_only],rows=[('facility-a',101,10,'10')]))
                self.assertEqual(old['periods'][DAY]['document_valuation']['allocations'][0]['weights']['facility-a:FBO:101'],4)

    def test_new_later_fee_after_depletion_remains_explicitly_blocked(self):
        from packages.application.current_fbo_supply_close import allocate_capital
        initial=initialize_candidate(fixture.capture(fbo=[('A',1,10,'10')]))
        line=dict(nm_id=1,facility_id='A',pool='FBO',quantity_delta=-10,**allocate_capital(10,'100',10))
        dispatch=fixture.doc('dispatch',source.KIND,lines=[line])
        dispatch.update(operation_id='dispatch',fbo_lifecycle_native=dict(contract=source.CONTRACT))
        dispatch['dispatch_valuation']=dict(valuation_digest='fixture',book_version='fixture')
        dispatch['cost_document']['contract']=source.CONTRACT
        fee=fixture.overhead(amount='100',scope='both')
        with self.assertRaisesRegex(ValueError,'expense_without_cost_mass'):
            evaluate_candidate(initial,fixture.capture('2026-09-08',[dispatch,fee]))

    def test_missing_day_error_and_quantity_mismatch_never_fallback(self):
        for change in ('date','error','quantity'):
            book=deepcopy(self.book)
            if change=='date':book['prepared_at']='2026-11-08T11:00:00Z'
            if change=='error':book['publication_error']='failed'
            if change=='quantity':book['state']['periods'][DAY]['document_cost_state']['fbo_rows']['facility-a:FBO:101']['quantity']='9'
            self.assertEqual(self.dispatch(book)['status'],'reserved')
            self.assertEqual(self.fixture.balance(),(10,Decimal(100)))

    def test_duplicate_append_and_tamper_rejected(self):
        result=self.dispatch();op=result['operation_id']
        with self.fixture.conn() as conn:
            row=conn.execute(f'SELECT valuation_json FROM {valuation.TABLE} WHERE operation_id=?',(op,)).fetchone()
            candidate=json.loads(row[0])
            conn.execute('BEGIN IMMEDIATE')
            with self.assertRaises(sqlite3.IntegrityError):valuation.append(conn,op,candidate)
            for sql in (f'DELETE FROM {valuation.TABLE}', f'UPDATE {valuation.TABLE} SET valuation_digest=valuation_digest'):
                with self.assertRaisesRegex(sqlite3.IntegrityError,'immutable_fbo_dispatch_valuation'):conn.execute(sql)
            # Simulate storage corruption by dropping only the test fixture trigger.
            conn.execute(f'DROP TRIGGER {valuation.TABLE}_no_update')
            conn.execute(f'UPDATE {valuation.TABLE} SET valuation_digest=?',('bad',))
            with self.assertRaisesRegex(ValueError,'proof_changed'):source.capture_documents(conn)
            with self.assertRaisesRegex(ValueError,'proof_changed'):valuation.normalize_warehouse_lines(conn,[])

    def test_repair_preview_same_basis_and_stale_native_pin(self):
        # Old dispatch has only native money; explicit preview changes no ledger.
        with patch('packages.application.fbo_dispatch_valuation.capture',return_value=None):
            result=self.fixture.service.reconcile('1')
        op=result['operation_id']
        with self.fixture.conn() as conn:
            documents=source.capture_documents(conn)
            with patch('packages.application.fbs_snapshot_cost_sources._documents',return_value=documents):
                candidate=valuation.prepare_repair(conn,self.book,self.version,operation_id=op,approval_reference='reviewed-fixture')
            self.assertEqual(candidate['lines'][0]['capital_delta_rub'],'-120')
            self.assertEqual(source.capture_documents(conn)[0]['cost_document']['lines'][0]['capital_delta_rub'],'-60')
            conn.execute('BEGIN IMMEDIATE')
            bad=deepcopy(candidate);bad['expected_native_digest']='changed'
            with self.assertRaisesRegex(ValueError,'native_changed'):valuation.append(conn,op,bad)
            valuation.append(conn,op,candidate)
            self.assertEqual(source.capture_documents(conn)[0]['cost_document']['lines'][0]['capital_delta_rub'],'-120')
            self.assertEqual(self.fixture.balance(),(4,Decimal(40)))

    def test_real_readonly_dependency_proof_covers_valuation(self):
        self.dispatch()
        from packages.application.dependency_proof import install_dependency_revisions, DependencyProof
        with self.fixture.conn() as conn:
            install_dependency_revisions(conn)
        with sqlite3.connect(self.fixture.runtime.db_path.as_uri()+'?mode=ro',uri=True) as conn:
            conn.row_factory=sqlite3.Row
            conn.execute('PRAGMA query_only=ON')
            proof=DependencyProof.capture(conn)
            with proof.observe():
                docs=source.capture_documents(conn)
                rows=[dict(r) for r in conn.execute(f'SELECT * FROM {lifecycle.AGGREGATE_LINES}')]
                valuation.normalize_warehouse_lines(conn,rows)
            self.assertEqual(docs[0]['cost_document']['lines'][0]['capital_delta_rub'],'-120')
            self.assertIn(('main',valuation.TABLE),proof.reads)
            proof.seal()

    def test_missing_accounting_never_falls_back_to_legacy_money(self):
        with patch('packages.application.fbs_accounting_runtime.load',return_value=(None,None)), \
             patch('packages.application.fbs_snapshot_cost_sources._documents',return_value=[]):
            result=self.fixture.service.reconcile('1')
        self.assertEqual(result['reason'],'accounting_dispatch_valuation_pending')
        self.assertEqual(result['valuation_blocker'],'fbo_dispatch_accounting_book_unavailable')
        self.assertEqual(self.fixture.balance(),(10,Decimal(100)))


if __name__=='__main__':unittest.main()
