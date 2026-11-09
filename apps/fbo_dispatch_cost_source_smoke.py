#!/usr/bin/env python3
"""Frozen live FBO cost survives independent-book refresh and later receipt."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps import fbs_document_cost_smoke as fixture
from packages.application.current_fbo_supply_close import allocate_capital
from packages.application import fbo_dispatch_cost_source as source
from packages.application.fbs_snapshot_cost import initialize_candidate,evaluate_candidate,close_candidate_period,fingerprint

class CostTests(unittest.TestCase):
    def setUp(self):
        self.initial=initialize_candidate(fixture.capture(fbo=[('A',1,10,'10')]))
        money=allocate_capital(10,'100',6)
        self.dispatch=fixture.doc('live-dispatch',source.KIND,lines=[dict(nm_id=1,facility_id='A',pool='FBO',quantity_delta=-6,**money)],domain=dict(facility_id='A',pool='FBO',supply_id='1'))
        self.dispatch['operation_id']='live-dispatch'
        self.dispatch['cost_document']['contract']=source.CONTRACT
        self.dispatch['fbo_lifecycle_native']=dict(contract=source.CONTRACT)
        self.dispatch['fingerprint']=fingerprint(self.dispatch)

    def test_fixed_debit_later_receipt_quiet_refresh_and_repeat(self):
        receipt=fixture.china('later-receipt',facility='A',pool='FBO',nm=1,q=2,amount='100')
        receipt['posted_at']='2026-09-08T14:00:00Z';receipt['fingerprint']=fingerprint(receipt)
        capture=fixture.capture('2026-09-08',[self.dispatch,receipt])
        result=evaluate_candidate(self.initial,capture)
        row=result['periods']['2026-09-08']['document_cost_state']['fbo_rows']['A:FBO:1']
        self.assertEqual((row['quantity'],Decimal(row['capital_rub'])),('6',Decimal(140)))
        repeated=evaluate_candidate(result,capture)
        self.assertEqual(repeated['periods']['2026-09-08']['document_cost_state'],result['periods']['2026-09-08']['document_cost_state'])
        closed=close_candidate_period(result,'2026-09-08',today='2026-09-09')
        quiet=evaluate_candidate(closed,fixture.capture('2026-09-09',[self.dispatch,receipt]))
        self.assertEqual(quiet['periods']['2026-09-09']['document_cost_state']['fbo_rows']['A:FBO:1']['capital_rub'],row['capital_rub'])

    def test_untyped_claim_rejected(self):
        forged=deepcopy(self.dispatch);forged.pop('fbo_lifecycle_native');forged['fingerprint']=fingerprint(forged)
        with self.assertRaisesRegex(ValueError,'typed_fbo_lifecycle_dispatch_required'):
            evaluate_candidate(self.initial,fixture.capture('2026-09-08',[forged]))

if __name__=='__main__':unittest.main()
