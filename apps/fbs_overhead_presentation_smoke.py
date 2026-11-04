"""Expense UI uses exact applied allocations; pending is never old or zero."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.fbs_document_cost_smoke import capture, overhead, china
from apps import current_fbo_supply_cost_source_smoke as current_fixture
from packages.application.fbs_snapshot_cost import initialize_candidate, evaluate_candidate
from packages.application import fbs_overhead_presentation as display


class OverheadViewTests(unittest.TestCase):
    def setUp(self):
        self.day = "2026-09-08"
        self.now = datetime(2026,9,8,18,tzinfo=timezone.utc)
        self.basis = [("A",1,1000,"100"),("A",2,3000,"50"),("B",1,1000,"200")]
        self.initial = initialize_candidate(capture(rows=self.basis, fbo=[("A",1,1000,"300")]))
        self.docs = [overhead("fee1","1000"),overhead("fee2","2000"),overhead("fee3","3000")]
        self.capture = capture(self.day,self.docs,rows=self.basis)
        self.state = evaluate_candidate(self.initial,self.capture)
        self.book = {"active":True,"effective_date":self.day,"state":self.state}
        self.summary = {"facility_id":"A","scope":"FBS","amount_rub":"1000","denominator_quantity":99999}

    def view(self):
        with patch.object(display, "load", return_value=(self.book,"version")):
            return display.OverheadAccountingView(Path('/not-written'),Path('/not-read'),now=self.now)

    def test_three_actual_expenses_use_applied_new_weights_and_conserve(self):
        before=deepcopy(self.book)
        view=self.view()
        for i,amount in enumerate((1000,2000,3000),1):
            a=view.resolve({**self.summary,"amount_rub":str(amount)},day=self.day,document_id=f'fee{i}')
            self.assertEqual(a['allocation_status'],'published')
            self.assertEqual(a['denominator_quantity'],4000)
            self.assertEqual(sum(Decimal(r['expense_rub']) for r in a['lines']),Decimal(amount))
            self.assertEqual({r['facility_id'] for r in a['lines']},{'A'})
            self.assertEqual(a['lines'][0]['expense_rub'],str(Decimal(amount)/4))
        self.assertEqual(self.book,before)

    def test_pending_missing_and_failure_never_show_old_shares(self):
        a=self.view().resolve(self.summary,day=self.day,document_id='not-applied')
        self.assertEqual(a['allocation_status'],'pending')
        self.assertIsNone(a['denominator_quantity'])
        self.assertEqual(a['lines'],[])
        self.book['publication_error']={'reason':'failed'}
        a=self.view().resolve(self.summary,day=self.day,document_id='fee1')
        self.assertEqual(a['allocation_status'],'unavailable')
        self.assertIsNone(a['allocation_total_rub'])

    def test_preview_uses_real_engine_and_new_receipts_in_both_pools(self):
        incoming=china('in',facility='A',q=1000,amount='200000',day=self.day)
        cap=capture(self.day,[*self.docs,incoming],rows=self.basis)
        before=deepcopy(self.book)
        with patch.object(display,'capture_current',return_value=cap):
            a=self.view().resolve({**self.summary,'scope':'both','amount_rub':'6000'},day=self.day,posted=False)
        self.assertEqual(a['allocation_status'],'preview',a)
        self.assertEqual(a['denominator_quantity'],6000)
        self.assertEqual(a['pool_allocations_rub'],{'FBS':'5000','FBO':'1000'})
        self.assertEqual(self.book,before)

    def test_absorbed_and_pre_transition_documents_keep_accepted_evidence(self):
        self.book['state']['baseline']['absorbed_documents']['old']='accepted'
        self.assertIsNone(self.view().resolve(self.summary,day=self.day,document_id='old'))
        self.assertIsNone(self.view().resolve(self.summary,day='2026-09-07',document_id='fee1'))

    def test_incomplete_preview_and_wrong_scope_are_unavailable(self):
        with patch.object(display,'capture_current',return_value={'business_date':self.day,'captured_at':self.now.isoformat(),'documents':[]}):
            a=self.view().resolve(self.summary,day=self.day,posted=False)
        self.assertEqual(a['allocation_status'],'unavailable')
        a=self.view().resolve({**self.summary,'facility_id':'B'},day=self.day,document_id='fee1')
        self.assertEqual(a['allocation_status'],'unavailable')

    def test_actual_current_dispatch_allocation_without_pool_subtotal_is_read_only(self):
        native = current_fixture.CurrentCostTests()
        native.setUp()
        state = native.evaluate()
        allocation = state['periods'][self.day]['document_valuation']['allocations'][0]
        self.assertEqual(allocation['policy'], 'native_posted_current_fbo_allocation_v1')
        self.assertIn('dispatch_boundary', allocation)
        self.assertNotIn('pool_amounts_kopecks', allocation)
        self.assertEqual(allocation['total_kopecks'], 23412205)
        self.book['state'] = state
        before = deepcopy(self.book)
        result = self.view().resolve({'facility_id':'A','scope':'FBO','amount_rub':'234122.05'},
                                     day=self.day, document_id='current-fee')
        self.assertEqual(result['allocation_status'], 'published', result)
        self.assertEqual(result['pool_allocations_rub'], {'FBO':'234122.05'})
        self.assertEqual(sum(Decimal(line['expense_rub']) for line in result['lines']), Decimal('234122.05'))
        self.assertEqual(self.book, before)

    def test_both_pool_subtotals_derive_without_changing_existing_format(self):
        document = overhead('both-fee', amount='1000', scope='both')
        self.book['state'] = evaluate_candidate(self.initial, capture(self.day, [document], rows=self.basis))
        summary = {**self.summary, 'scope':'both'}
        expected = self.view().resolve(summary, day=self.day, document_id='both-fee')
        self.assertEqual(expected['allocation_status'], 'published', expected)
        self.assertEqual(expected['pool_allocations_rub'], {'FBO':'200', 'FBS':'800'})
        allocation = self.book['state']['periods'][self.day]['document_valuation']['allocations'][0]
        del allocation['pool_amounts_kopecks']
        before = deepcopy(self.book)
        self.assertEqual(self.view().resolve(summary, day=self.day, document_id='both-fee'), expected)
        self.assertEqual(self.book, before)

    def test_explicit_pool_subtotal_mismatch_is_unavailable(self):
        allocation = self.book['state']['periods'][self.day]['document_valuation']['allocations'][0]
        allocation['pool_amounts_kopecks'] = {'FBS':99999}
        result = self.view().resolve(self.summary, day=self.day, document_id='fee1')
        self.assertEqual(result['allocation_status'], 'unavailable')
        self.assertEqual(result['allocation_reason'], 'accounting_allocation_pool_money_mismatch')

    def test_explicit_zero_pool_subtotal_is_preserved_but_other_scope_is_rejected(self):
        allocation = self.book['state']['periods'][self.day]['document_valuation']['allocations'][0]
        allocation['pool_amounts_kopecks']['FBO'] = 0
        result = self.view().resolve({**self.summary, 'scope':'both'}, day=self.day, document_id='fee1')
        self.assertEqual(result['allocation_status'], 'published', result)
        self.assertEqual(result['pool_allocations_rub'], {'FBS':'1000', 'FBO':'0'})
        result = self.view().resolve(self.summary, day=self.day, document_id='fee1')
        self.assertEqual(result['allocation_reason'], 'accounting_allocation_pool_money_mismatch')

    def test_missing_subtotal_preserves_document_money_and_scope_guards(self):
        original = deepcopy(self.book)
        cases = (
            ('document_fingerprint', 'changed', 'accounting_allocation_document_mismatch'),
            ('total_kopecks', 99999, 'accounting_allocation_money_mismatch'),
            ('amounts_kopecks', {'B:FBS:1':100000}, 'accounting_allocation_scope_mismatch'),
            ('amounts_kopecks', {'A:FBO:1':100000}, 'accounting_allocation_scope_mismatch'),
        )
        for field, value, reason in cases:
            with self.subTest(field=field, value=value):
                self.book = deepcopy(original)
                allocation = self.book['state']['periods'][self.day]['document_valuation']['allocations'][0]
                del allocation['pool_amounts_kopecks']
                allocation[field] = value
                result = self.view().resolve(self.summary, day=self.day, document_id='fee1')
                self.assertEqual(result['allocation_status'], 'unavailable', result)
                self.assertEqual(result['allocation_reason'], reason)


if __name__ == '__main__':
    unittest.main()
