#!/usr/bin/env python3
"""Current FBO money remains owned after book refresh and later ordinary flows."""
from copy import deepcopy
from decimal import Decimal, localcontext
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps import fbs_document_cost_smoke as fixture
from packages.application import current_fbo_supply_close as owner
from packages.application import current_fbo_supply_cost_source as source
from packages.application.fbs_snapshot_cost import initialize_candidate,evaluate_candidate,close_candidate_period,fingerprint


class CurrentCostTests(unittest.TestCase):
    def setUp(self):
        self.seven=[fixture.overhead('old-fee-'+str(i),amount='100',day='2026-09-06') for i in range(7)]
        self.initial=initialize_candidate(fixture.capture(docs=self.seven,fbo=[('A',1,10,'12.345678901234567890123456789'),('A',2,20,'5')]))
        q=10;c='123.456789012345678901234567890123456789';self.dispatch=[]
        for i,debit in enumerate((2,3,5)):
            money=owner.allocate_capital(q,c,debit)
            line=dict(nm_id=1,facility_id='A',pool='FBO',quantity_delta=-debit,**money)
            doc=fixture.doc('dispatch-'+str(i),'current_fbo_dispatch',lines=[line],domain=dict(facility_id='A',pool='FBO',supply_id=str(i+1)))
            doc['cost_document']['contract']=source.CONTRACT
            doc['current_fbo_native']=dict(contract=source.CONTRACT,batch_id='batch',effect_index=i)
            doc['fingerprint']=fingerprint(doc);self.dispatch.append(doc)
            q=money['after_quantity'];c=money['after_capital_rub']
        self.fee=fixture.doc('current-fee','pool_overhead',hour=13,amount='234122.05',
            domain=dict(facility_id='A',scope='FBO',amount_rub='234122.05'),
            lines=[fixture.line('A','FBO',nm=2,q=20,role='overhead_allocation',capital='234122.05',expense='234122.05')])

    def evaluate(self, extra=()):
        return evaluate_candidate(self.initial,fixture.capture('2026-09-08',[*self.seven,*self.dispatch,self.fee,*extra]))

    def test_remaining_stock_fee_full_depletion_and_seven_fbs_sources_preserved(self):
        result=self.evaluate();period=result['periods']['2026-09-08'];rows=period['document_cost_state']['fbo_rows']
        self.assertEqual((rows['A:FBO:1']['quantity'],Decimal(rows['A:FBO:1']['capital_rub'])),('0',Decimal(0)))
        self.assertEqual(Decimal(rows['A:FBO:2']['capital_rub']),Decimal('234222.05'))
        self.assertEqual(result['baseline'],self.initial['baseline'])
        self.assertEqual(period['rows']['A:1']['wac_rub'],'100')
        self.assertEqual(period['rows']['B:1']['wac_rub'],'200')
        self.assertEqual(sum(a['total_kopecks'] for a in period['document_valuation']['allocations']),23412205)
        self.assertTrue(period['document_valuation']['current_fbo_dispatch_bridges'])

    def test_later_receipt_and_transfer_are_allowed_without_reallocating_old_fee(self):
        incoming=fixture.china('later-receipt',facility='A',pool='FBO',nm=1,q=1,amount='10')
        incoming['posted_at']='2026-09-08T14:00:00Z';incoming['fingerprint']=fingerprint(incoming)
        root=fixture.root('later-transfer',src='A',dst='B',src_pool='FBO',dst_pool='FBO')
        root['posted_at']='2026-09-08T15:00:00Z';root['fingerprint']=fingerprint(root)
        shipment=fixture.shipment('later-shipped',transfer='later-transfer',q=1,src='A',pool='FBO')
        shipment['posted_at']='2026-09-08T16:00:00Z';shipment['fingerprint']=fingerprint(shipment)
        result=self.evaluate([incoming,root,shipment]);period=result['periods']['2026-09-08']
        self.assertEqual(Decimal(period['document_cost_state']['fbo_rows']['A:FBO:1']['capital_rub']),0)
        self.assertEqual(Decimal(period['document_cost_state']['fbo_rows']['A:FBO:2']['capital_rub']),Decimal('234222.05'))
        self.assertEqual(Decimal(next(iter(period['document_cost_state']['transfers'].values()))['base_capital_rub']),Decimal(10))

    def test_real_money_mismatch_and_earlier_conflicting_document_fail(self):
        changed=deepcopy(self.initial)
        changed['baseline']['document_cost_state']['fbo_rows']['A:FBO:1']['capital_rub']='120'
        with self.assertRaisesRegex(ValueError,'book_native_capital_bridge_mismatch'):
            evaluate_candidate(changed,fixture.capture('2026-09-08',[*self.seven,*self.dispatch,self.fee]))
        earlier=fixture.china('earlier',facility='A',pool='FBO',q=1,amount='10')
        earlier['posted_at']='2026-09-08T11:00:00Z';earlier['fingerprint']=fingerprint(earlier)
        with self.assertRaisesRegex(ValueError,'earlier_same_day_fbo'):
            self.evaluate([earlier])

    def test_partial_exact_capital_survives_next_quiet_day(self):
        self.dispatch=self.dispatch[:1];self.fee['cost_document']['lines']=[
            fixture.line('A','FBO',nm=1,q=8,role='overhead_allocation',capital='66900.00',expense='66900.00'),
            fixture.line('A','FBO',nm=2,q=20,role='overhead_allocation',capital='167222.05',expense='167222.05')]
        self.fee['fingerprint']=fingerprint(self.fee)
        result=self.evaluate();row=result['periods']['2026-09-08']['document_cost_state']['fbo_rows']['A:FBO:1']
        with localcontext() as ctx:
            ctx.prec=160
            expected=Decimal(self.dispatch[0]['cost_document']['lines'][0]['after_capital_rub'])+Decimal('66900')
        self.assertEqual(Decimal(row['capital_rub']),expected)
        result=close_candidate_period(result,'2026-09-08',today='2026-09-09')
        nextday=evaluate_candidate(result,fixture.capture('2026-09-09',[*self.seven,*self.dispatch,self.fee]))
        self.assertEqual(Decimal(nextday['periods']['2026-09-09']['document_cost_state']['fbo_rows']['A:FBO:1']['capital_rub']),expected)


if __name__=='__main__':unittest.main()
