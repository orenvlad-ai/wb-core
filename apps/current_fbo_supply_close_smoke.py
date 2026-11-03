#!/usr/bin/env python3
"""Exact capital, native commit/publication, rollback and no-resubmit contract."""
from decimal import Decimal, localcontext
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps import fbs_scoped_lifecycle_production_smoke as fixture
FrozenClock, NOW = fixture.FrozenClock, fixture.NOW
from packages.application import current_fbo_supply_close as core
from packages.application import current_fbo_supply_close_apply as action
from packages.application import fbs_scoped_lifecycle_production as shared
from apps.production_apply_launcher import execute


class CurrentFboTests(unittest.TestCase):
    connection = fixture.ProductionRecoveryTests.connection
    snapshot = fixture.ProductionRecoveryTests.snapshot

    def setUp(self):
        fixture.ProductionRecoveryTests.setUp(self)
        guard=patch.object(action,'datetime',FrozenClock);guard.start();self.addCleanup(guard.stop)
        book=dict(active=True,state=dict(periods={},baseline=dict(document_cost_state=dict(fbo_rows={
            'fac_moscow:FBO:101':dict(quantity='10',capital_rub='123.456789012345678901234567890123456789')}))))
        guard=patch.object(action,'load_book',return_value=(book,'book-with-seven-expenses'));guard.start();self.addCleanup(guard.stop)
        with self.connection() as conn:
            # The inherited lifecycle fixture deliberately has an unrelated
            # negative reconciliation row. A current-hourly fixture must be
            # physically complete instead of carrying that separate scenario.
            conn.execute(f"UPDATE {core.BALANCES_TABLE} SET quantity=0,capital_rub='0',wac_rub=NULL WHERE nm_id=102")
            conn.execute(f"UPDATE {core.ACTIVE_BALANCES} SET quantity='0',cost_covered_quantity='0',capital_rub='0',wac_rub=NULL WHERE warehouse_key='ff' AND nm_id=102")
            # The three sources ship ten units; exact current capital contains
            # a fractional kopeck which must survive two partial debits and be
            # completely attributed to the last full depletion.
            conn.execute(f"INSERT OR IGNORE INTO {core.BALANCES_TABLE} SELECT 'fac_moscow','FBO',101,MAX(epoch),0,'0',NULL,'fixture',? FROM {core.FEATURE_EPOCHS_TABLE}",(NOW,))
            conn.execute(f"UPDATE {core.BALANCES_TABLE} SET quantity=10,capital_rub='123.456789012345678901234567890123456789',wac_rub='12.3456789012345678901234567890123456789' WHERE facility_id='fac_moscow' AND pool='FBO' AND nm_id=101")
            with localcontext() as ctx:
                ctx.prec=160
                allrows=list(conn.execute(f'SELECT quantity,capital_rub FROM {core.BALANCES_TABLE} WHERE nm_id=101'))
                quantity=sum(r[0] for r in allrows);capital=sum((Decimal(r[1]) for r in allrows),Decimal(0))
            conn.execute(f"UPDATE {core.ACTIVE_BALANCES} SET quantity=?,cost_covered_quantity=?,capital_rub=?,wac_rub=? WHERE version_id='wf_stage7c' AND warehouse_key='ff' AND nm_id=101",
                (str(quantity),str(quantity),str(capital),str(capital/quantity)))
            conn.execute(f"INSERT INTO {core.AGGREGATE}(operation_id,operation_type,source_type,source_key,created_at,total_quantity_delta,total_quantity_abs) VALUES('current-seed','receipt','synthetic','current-seed',?,100,100)",(NOW,))
            conn.execute(f"INSERT INTO {core.AGGREGATE_LINES}(operation_id,line_no,nm_id,quantity_delta) VALUES('current-seed',1,101,100)")
            for i,q in enumerate((2,3,5),1):
                supply=str(i);key='supply:'+supply
                normalized=dict(supply_id=supply,cache_key=key,status_id=5)
                conn.execute('INSERT INTO sheet_vitrina_v1_wb_supplies(supply_id,cache_key,normalized_row_json,raw_goods_json,synced_at) VALUES(?,?,?,?,?)',
                    (supply,key,json.dumps(normalized),json.dumps([dict(nmID=101,quantity=q)]),NOW))
                conn.execute(f'INSERT INTO {core.RESERVATIONS} VALUES(?,?,?,?,?,?,?)',
                    ('reserve-'+supply,'reserve-'+supply,supply,'source-v1','reserve',NOW,'{}'))
                conn.execute(f'INSERT INTO {core.RESERVATION_LINES} VALUES(?,?,?,?,?)',('reserve-'+supply,1,101,q,'{}'))
            conn.commit()
        self.request={**self.request,'supply_ids':['1','2','3'],'expected_quantity':10}
        for key in ('nm_id','expected_order_count','expected_status_count','expected_handoff_count'):
            self.request.pop(key)
        self.operation='fbo-current-fixture-01';self.adapter=action.CurrentFboSupplyCloseAdapter()

    def test_exact_three_debits_publication_and_durable_retry(self):
        preview=self.adapter.preview(self.request,self.operation)
        with self.connection() as conn:
            protected=shared.protected_scope(conn,self.runtime.runtime_dir)
            wb=list(conn.execute(f"SELECT * FROM {core.ACTIVE_BALANCES} WHERE version_id='wf_stage7c' AND warehouse_key='wb'"))
        result=execute(action='apply',adapter_name='fixture',operation_id=self.operation,request=self.request,
            expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'],adapters={'fixture':self.adapter})
        self.assertEqual(result['state'],'applied');self.assertFalse(result['readback']['recovery_pending'])
        self.assertTrue(result['readback']['publication_retained'])
        with self.connection() as conn:
            balance=conn.execute(f"SELECT quantity,capital_rub FROM {core.BALANCES_TABLE} WHERE facility_id='fac_moscow' AND pool='FBO' AND nm_id=101").fetchone()
            self.assertEqual((balance[0],Decimal(balance[1])),(0,Decimal(0)))
            self.assertEqual(shared.protected_scope(conn,self.runtime.runtime_dir),protected)
            self.assertEqual(conn.execute(f"SELECT SUM(l.quantity_delta) FROM {core.RESERVATION_LINES} l JOIN {core.RESERVATIONS} o USING(operation_id) WHERE o.supply_id IN ('1','2','3')").fetchone()[0],0)
            active=conn.execute(f'SELECT version_id FROM {core.ACTIVE} WHERE slot=1').fetchone()[0]
            after=list(conn.execute(f"SELECT * FROM {core.ACTIVE_BALANCES} WHERE version_id=? AND warehouse_key='wb'",(active,)))
            self.assertEqual([tuple(r)[1:] for r in wb],[tuple(r)[1:] for r in after])
            with localcontext() as ctx:
                ctx.prec=160
                spent=sum((Decimal(r[0]) for r in conn.execute(f"SELECT l.capital_delta_rub FROM {core.LINES_TABLE} l JOIN {core.OPERATIONS_TABLE} o USING(operation_id) WHERE o.source_system=?",(core.CONTRACT,))),Decimal(0))
                self.assertEqual(spent,Decimal('-123.456789012345678901234567890123456789'))
        before=self.snapshot()
        # Saved JSON uses string object keys; replay must retain the same
        # digest and binding across a process/serialization boundary.
        plan=json.loads(json.dumps(preview['prepared']['plan']))
        with self.connection() as conn:
            conn.execute('BEGIN')
            core.verify(conn,plan)
            from packages.application.current_fbo_supply_cost_source import capture_documents
            documents=capture_documents(conn)
            self.assertEqual(len(documents),3)
            from packages.application.fbs_snapshot_cost_sources import _documents
            all_documents=_documents(conn)
            self.assertEqual([d['document_id'] for d in all_documents if d['kind']=='current_fbo_dispatch'],
                [d['document_id'] for d in documents])
            from packages.application.warehouse_functional import _ff_ledger_line_cost_snapshot, _current_ff_pool_projection
            with localcontext() as ctx:
                ctx.prec=50
                parsed=[_ff_ledger_line_cost_snapshot(dict(r)) for r in conn.execute(f"SELECT l.* FROM {core.AGGREGATE_LINES} l JOIN {core.AGGREGATE} o USING(operation_id) WHERE o.source_key LIKE 'wb_supply_debit:%'")]
            with localcontext() as ctx:
                ctx.prec=160
                self.assertEqual(sum((r['capital_delta_rub'] for r in parsed),Decimal(0)),Decimal('-123.456789012345678901234567890123456789'))
            capture={key:core.rows(conn,table) for key,table in (
                ('ff_pool_cutover_manifests','sheet_vitrina_v1_ff_pool_cutover_manifests'),
                ('ff_pool_balances',core.BALANCES_TABLE),('ff_pool_feature_epochs',core.FEATURE_EPOCHS_TABLE))}
            projected=_current_ff_pool_projection(capture)
            self.assertIsNotNone(projected)
            self.assertEqual(projected['by_nm'][101]['quantity'],10)
            self.assertEqual(Decimal(projected['by_nm'][101]['capital']),Decimal(100))
        self.assertEqual(self.adapter.apply(self.request,self.operation,preview)['disposition'],'already_applied')
        self.assertEqual(before,self.snapshot())
        # Ordinary pruning of presentation history must not erase permanent
        # native cost-source admission; operation readback remains stricter.
        with self.connection() as conn:
            conn.execute('DELETE FROM sheet_vitrina_v1_warehouse_wb_snapshots WHERE version_id=?',
                (result['readback']['published_version_id'],));conn.commit()
            self.assertEqual(len(capture_documents(conn)),3)
            with self.assertRaisesRegex(ValueError,'publication_receipt_changed'):
                core.verify(conn,plan)
        pruned=self.adapter.readback(self.request,self.operation)
        self.assertEqual(pruned['state'],'applied')
        self.assertFalse(pruned['publication_retained'])
        self.assertEqual(self.adapter.apply(self.request,self.operation,preview)['disposition'],'already_applied')

    def test_fault_after_second_pool_rolls_back_every_business_effect(self):
        preview=self.adapter.preview(self.request,self.operation);before=self.snapshot()
        real=core.apply_owned
        def fail(*args,**kwargs):
            def fault(phase,index):
                if (phase,index)==('pool',1):raise RuntimeError('injected')
            return real(*args,**kwargs,fault=fault)
        with patch.object(core,'apply_owned',side_effect=fail):
            with self.assertRaises(Exception):self.adapter.apply(self.request,self.operation,preview)
        after=self.snapshot()
        for table in (core.BALANCES_TABLE,core.OPERATIONS_TABLE,core.LINES_TABLE,core.AGGREGATE,core.AGGREGATE_LINES,core.RESERVATIONS,core.RESERVATION_LINES,core.ACTIVE):
            self.assertEqual(before[table],after[table],table)
        self.assertEqual(self.adapter.readback(self.request,self.operation)['state'],'ambiguous')

    def test_source_drift_and_partial_reservation_reject(self):
        preview=self.adapter.preview(self.request,self.operation)
        with self.connection() as conn:
            conn.execute("UPDATE sheet_vitrina_v1_wb_supplies SET raw_goods_hash='changed' WHERE supply_id='1'");conn.commit()
        with self.assertRaisesRegex(Exception,'preview_compare_and_swap'):
            self.adapter.apply(self.request,self.operation,preview)
        with self.connection() as conn:
            conn.execute(f"UPDATE {core.RESERVATION_LINES} SET quantity_delta=1 WHERE operation_id='reserve-1'");conn.commit()
        with self.assertRaisesRegex(Exception,'exact_supply_reservation'):
            self.adapter.preview(self.request,self.operation)

    def test_exact_money_full_partial_and_both_signed_rounding_tails(self):
        for capital in ('1.0000000000000000000000000000000000000000000000001',
                        '0.9999999999999999999999999999999999999999999999999'):
            first=core.allocate_capital(3,capital,1)
            second=core.allocate_capital(2,first['after_capital_rub'],2)
            with localcontext() as ctx:
                ctx.prec=160
                self.assertEqual(Decimal(first['capital_delta_rub'])+Decimal(second['capital_delta_rub']),-Decimal(capital))
                self.assertEqual(Decimal(second['after_capital_rub']),0)
        from packages.application.warehouse_functional import replay_ff_cost_pools
        capital=Decimal('1.00000000000000000000000000000000000000000000000001')
        frozen=core.allocate_capital(3,capital,3)
        with localcontext() as ctx:
            ctx.prec=28
            pools,_=replay_ff_cost_pools(opening_pools={101:dict(quantity=Decimal(3),capital=capital,operations=[])},
                operations=[dict(operation_id='out',created_at=NOW,business_effective_date='2026-08-16',source_type='wb_supply',source_object_id='1')],
                lines=[dict(operation_id='out',nm_id=101,quantity_delta=-3,raw=dict(cost_snapshot=dict(unit_cost_rub=frozen['wac_rub'],capital_delta_rub=frozen['capital_delta_rub'],quality='approved_current_fbo_dispatch_estimate',provenance={'contract':core.CONTRACT})))],
                supplier_flow_costs={},cost_map={},boundary='2026-08-15T00:00:00Z',expected_quantities={101:Decimal(0)})
        self.assertEqual(pools[101]['capital'],0)
        self.assertEqual(Decimal(pools[101]['operations'][0]['capital_delta_rub']),capital.copy_negate())

    def test_unadmitted_core_rejected(self):
        preview=self.adapter.preview(self.request,self.operation)
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            with self.assertRaisesRegex(Exception,'typed_owner_required'):
                core.apply_owned(conn,self.runtime,plan=preview['prepared']['plan'],occurred_at=NOW,actor='test')
            conn.rollback()

    def test_partial_capital_survives_the_ordinary_hourly_projection_owner(self):
        with self.connection() as conn:
            conn.execute("UPDATE sheet_vitrina_v1_wb_supplies SET raw_goods_json=? WHERE supply_id='3'",(json.dumps([dict(nmID=101,quantity=4)]),))
            conn.execute(f"UPDATE {core.RESERVATION_LINES} SET quantity_delta=4 WHERE operation_id='reserve-3'")
            conn.commit()
        self.request['expected_quantity']=9
        preview=self.adapter.preview(self.request,self.operation)
        self.adapter.apply(self.request,self.operation,preview)
        from packages.application.warehouse_functional import _current_ff_pool_projection
        with self.connection() as conn:
            capture={key:core.rows(conn,table) for key,table in (
                ('ff_pool_cutover_manifests','sheet_vitrina_v1_ff_pool_cutover_manifests'),
                ('ff_pool_balances',core.BALANCES_TABLE),('ff_pool_feature_epochs',core.FEATURE_EPOCHS_TABLE))}
            projected=_current_ff_pool_projection(capture)
            remainder=Decimal(preview['prepared']['plan']['effects'][-1]['lines'][0]['after_capital_rub'])
            with localcontext() as ctx:
                ctx.prec=160
                self.assertEqual(Decimal(projected['by_nm'][101]['capital']),Decimal(100)+remainder)


if __name__=='__main__':unittest.main()
