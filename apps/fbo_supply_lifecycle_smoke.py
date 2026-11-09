#!/usr/bin/env python3
"""Addressed FBO atomic reservation, cutover, dispatch and retry contracts."""
from pathlib import Path
from tempfile import TemporaryDirectory
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
import sqlite3
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps.ff_wb_supply_origins_smoke import _enable_fixture_writer, Clock
from apps.ff_stock_ledger_smoke import _seed_nomenclature
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.ff_wb_supply_origins import FfWbSupplyOriginAssignments, FfWbSupplyOriginError
from packages.application.ff_stock_ledger import FfStockLedgerBlock
from packages.application import fbo_supply_lifecycle as core

NOW='2026-11-09T12:00:00Z'

class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.runtime=RegistryUploadDbBackedRuntime(runtime_dir=Path(self.temp.name))
        self.runtime.list_wb_supplies();_seed_nomenclature(self.runtime,[101,102])
        _enable_fixture_writer(self.runtime.db_path,Clock())
        self.service=core.FboSupplyLifecycle(db_path=self.runtime.db_path,timestamp_factory=lambda:NOW,runtime=self.runtime)
        self.origins=FfWbSupplyOriginAssignments(db_path=self.runtime.db_path,timestamp_factory=lambda:NOW)
        self.ledger=FfStockLedgerBlock(runtime=self.runtime,timestamp_factory=lambda:NOW)
        self.runtime.create_ff_stock_operation(operation_id='opening',operation_type='receipt',source_type='fixture',source_key='opening',created_at='2026-11-01T00:00:00Z',lines=[dict(nm_id=101,quantity_delta=200),dict(nm_id=102,quantity_delta=100)])
        self.ledger.ensure_wb_supply_auto_writeoff_checkpoint([],reason='fixture',created_by='fixture')
        with self.conn() as conn:
            for facility,pool,nm,qty,capital in [('facility-a','FBO',101,10,'100'),('facility-a','FBO',102,5,'100'),('facility-b','FBO',101,20,'1000'),('facility-b','FBO',102,0,'0'),('facility-a','FBS',102,100,'500')]:
                conn.execute(f'INSERT INTO {core.BALANCES_TABLE} VALUES(?,?,?,?,?,?,?,?,?)',(facility,pool,nm,1,qty,capital,str(Decimal(capital)/qty) if qty else None,'fixture',NOW))
        self.seed('1',[(101,6),(102,3)]);self.seed('2',[(101,5),(102,1)])
        self.counter=0

    def conn(self):
        conn=sqlite3.connect(self.runtime.db_path);conn.row_factory=sqlite3.Row;return conn

    def seed(self,sid,goods,status=3,**extra):
        normalized=dict(supply_id=sid,wb_supply_id=sid,cache_key='supply:'+sid,status_id=status,source_created_at='2026-11-08T00:00:00Z',**extra)
        with self.conn() as conn:
            conn.execute('''INSERT INTO sheet_vitrina_v1_wb_supplies(supply_id,cache_key,wb_supply_id,status_id,normalized_row_json,raw_goods_json,synced_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(supply_id) DO UPDATE SET status_id=excluded.status_id,normalized_row_json=excluded.normalized_row_json,raw_goods_json=excluded.raw_goods_json''',(sid,'supply:'+sid,sid,status,json.dumps(normalized),json.dumps([dict(nmID=n,quantity=q) for n,q in goods]),NOW))

    def cutover(self,ids=('1','2')):
        plan=self.service.migration_preview(supply_ids=ids,cutoff_at=NOW)
        result=self.service.migration_apply(plan,actor='fixture')
        self.assertEqual(result['status'],'applied');return plan

    def assign(self,sid,facility='facility-a',request=None):
        self.counter+=1
        current=self.origins.assignment_detail(sid)['current_assignment']
        return self.origins.assign_origin(sid,dict(request_id=request or 'request-%08d'%self.counter,facility_id=facility,expected_assignment_id=current['assignment_id'] if current else ''),actor='fixture')

    def balance(self,facility='facility-a',nm=101,pool='FBO'):
        with self.conn() as conn:
            row=conn.execute(f'SELECT quantity,capital_rub FROM {core.BALANCES_TABLE} WHERE facility_id=? AND pool=? AND nm_id=?',(facility,pool,nm)).fetchone()
            return row[0],Decimal(row[1])

    def test_cutover_legacy_preservation_and_no_get_writes(self):
        self.runtime.create_ff_stock_reservation_operation(operation_id='legacy',source_key='legacy',supply_id='1',supply_revision='legacy',operation_type='reserve',created_at=NOW,diagnostics={},lines=[dict(nm_id=101,quantity_delta=6),dict(nm_id=102,quantity_delta=3)],expected_current={})
        self.assertEqual(self.service.detail('1')['status'],'out_of_scope')
        self.assertEqual(self.ledger.record_wb_supply_debit(self.runtime.load_wb_supply_record('1'))['fbo_lifecycle']['status'],'out_of_scope')
        plan=self.cutover();self.assertTrue(self.service.migration_apply(plan,actor='fixture')['idempotent'])
        self.assertEqual(self.service.detail('1')['legacy_reserved_qty'],9)
        self.assertEqual(len(self.service.notifications()['items']),2)
        with self.conn() as conn: count=conn.execute(f'SELECT COUNT(*) FROM {core.EVENTS}').fetchone()[0]
        self.service.notifications();self.service.detail('1')
        with self.conn() as conn: self.assertEqual(count,conn.execute(f'SELECT COUNT(*) FROM {core.EVENTS}').fetchone()[0])
        result=self.assign('1');self.assertEqual(result['fbo_lifecycle']['reserved_qty'],9)
        self.assertEqual(sum(r['quantity'] for r in self.runtime.list_ff_stock_reservations(supply_id='1')),9)
        with self.conn() as conn:
            summary=core.reservation_summary(conn,'facility-a')
            self.assertEqual(summary['by_nm_id'],{101:6,102:3});self.assertEqual(summary['quantity'],9)
            self.assertTrue(summary['updated_at'])
        self.assertEqual(self.balance(),(10,Decimal(100)))

    def test_both_legacy_reserves_first_source_shortage_releases_unscoped_only(self):
        for sid,goods in [('1',[(101,6),(102,3)]),('2',[(101,5),(102,1)])]:
            self.runtime.create_ff_stock_reservation_operation(operation_id='legacy'+sid,source_key='legacy'+sid,supply_id=sid,supply_revision='legacy',operation_type='reserve',created_at=NOW,diagnostics={},lines=[dict(nm_id=nm,quantity_delta=q) for nm,q in goods],expected_current={})
        self.cutover()
        self.assertEqual(sum(r['quantity'] for r in self.runtime.list_ff_stock_reservations()),15)
        self.assign('1');result=self.assign('2')['fbo_lifecycle']
        self.assertEqual((result['status'],result['reserved_qty'],result['legacy_reserved_qty']),('shortage',0,0))
        self.assertEqual(self.runtime.list_ff_stock_reservations(supply_id='2'),[])
        self.assertEqual(self.service.detail('2')['origin']['facility_id'],'facility-a')
        with self.conn() as conn:self.assertEqual(core.reservation_summary(conn,'facility-a')['quantity'],9)
        self.assertEqual(self.balance(),(10,Decimal(100)))
        self.assertEqual(len(self.service.notifications()['items']),1)

    def test_competing_reservation_and_failed_source_change(self):
        self.cutover();self.assign('1')
        second=self.assign('2')['fbo_lifecycle']
        self.assertEqual(second['status'],'shortage');self.assertEqual(second['reserved_qty'],0)
        self.assertEqual(second['shortages'],[dict(nm_id=101,required=5,available=4,missing=1)])
        with self.assertRaises(FfWbSupplyOriginError):self.assign('1','facility-b')
        state=self.service.detail('1');self.assertEqual(state['origin']['facility_id'],'facility-a');self.assertEqual(state['reserved_qty'],9)
        # FBS availability never covers a deficient selected FBO SKU.
        self.seed('2',[(102,6)]);self.assertEqual(self.service.reconcile('2')['status'],'shortage')
        self.assertEqual(self.balance(nm=102,pool='FBS'),(100,Decimal(500)))

    def test_dispatch_single_exact_source_cost_and_old_status(self):
        self.seed('1',[(101,6)]);self.cutover();self.assign('1','facility-b')
        self.seed('1',[(101,6)],6)
        result=self.service.reconcile('1');self.assertEqual(result['status'],'debited')
        self.assertEqual(self.balance('facility-b'),(14,Decimal(700)))
        self.assertEqual(self.balance(),(10,Decimal(100)))
        self.assertEqual(result['frozen_costs'][0]['wac_rub'],'50')
        self.assertEqual(self.runtime.list_ff_stock_reservations(supply_id='1'),[])
        self.service.reconcile('1');self.seed('1',[(101,6)],3);self.service.reconcile('1')
        with self.conn() as conn:
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {core.AGGREGATE} WHERE source_key='wb_supply_debit:supply:1'").fetchone()[0],1)
            self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {core.OPERATIONS_TABLE} WHERE operation_id=?',(result['operation_id'],)).fetchone()[0],1)
        self.assertEqual(self.service.detail('1')['reserved_qty'],0)
        with self.assertRaises(FfWbSupplyOriginError):self.assign('1')
        self.seed('1',[(101,7)],5);self.assertEqual(self.service.reconcile('1')['status'],'needs_correction')
        self.assertEqual(self.balance('facility-b'),(14,Decimal(700)))

    def test_missed_gate_and_cancellation(self):
        self.seed('1',[(101,6)],4);self.cutover()
        self.assertEqual(self.service.reconcile('1')['status'],'missing_source')
        self.assertEqual(self.assign('1')['fbo_lifecycle']['status'],'debited')
        self.seed('1',[(101,6)],5,is_cancelled=True)
        self.assertEqual(self.service.reconcile('1')['reason'],'cancelled_after_debit')
        self.seed('2',[(101,2)],3);self.assign('2')
        self.seed('2',[(101,2)],2,is_cancelled=True)
        self.assertEqual(self.service.reconcile('2')['status'],'cancelled')
        self.assertEqual(self.runtime.list_ff_stock_reservations(supply_id='2'),[])

    def test_atomic_fault_rolls_back_both_ledgers_and_reservation(self):
        self.cutover();self.assign('1');self.seed('1',[(101,6),(102,3)],6)
        original=core._replace_reservation
        def fail(conn,state,desired,now):
            if not desired:raise RuntimeError('fault after physical insert')
            return original(conn,state,desired,now)
        with patch.object(core,'_replace_reservation',side_effect=fail):
            with self.assertRaises(RuntimeError):self.service.reconcile('1')
        self.assertEqual(self.balance(),(10,Decimal(100)))
        self.assertIsNone(self.runtime.load_ff_stock_operation_by_source_key('wb_supply_debit:supply:1'))
        self.assertEqual(sum(r['quantity'] for r in self.runtime.list_ff_stock_reservations(supply_id='1')),9)
        self.assertEqual(self.service.reconcile('1')['status'],'debited')

    def test_concurrent_duplicate_dispatch(self):
        self.cutover();self.assign('1');self.seed('1',[(101,6),(102,3)],6)
        with ThreadPoolExecutor(max_workers=2) as workers:results=list(workers.map(lambda _:self.service.reconcile('1'),range(2)))
        self.assertEqual({r['status'] for r in results},{'debited'})
        self.assertEqual(self.balance(),(4,Decimal(40)))

    def test_existing_debit_adopted_and_cutover_drift_rejected(self):
        self.runtime.create_ff_stock_operation(operation_id='old',operation_type='auto_writeoff',source_type='wb_supply',source_key='wb_supply_debit:1',source_object_id='1',created_at=NOW,lines=[dict(nm_id=101,quantity_delta=-6)])
        plan=self.service.migration_preview(supply_ids=['1','2'],cutoff_at=NOW)
        self.seed('2',[(101,4)])
        with self.assertRaisesRegex(ValueError,'migration_source_changed'):self.service.migration_apply(plan,actor='fixture')
        self.cutover();self.assertEqual(self.service.detail('1')['status'],'debited')
        self.service.reconcile('1');self.assertEqual(self.balance(),(10,Decimal(100)))

    def test_changed_composition_and_successful_source_swap(self):
        self.seed('1',[(101,6)]);self.cutover();self.assign('1')
        self.seed('1',[(101,4)]);self.assertEqual(self.service.reconcile('1')['reserved_qty'],4)
        swapped=self.assign('1','facility-b')['fbo_lifecycle']
        self.assertEqual(swapped['reservation_origin']['facility_id'],'facility-b')
        self.assertEqual(swapped['reserved_qty'],4)
        self.assertEqual(self.balance(),(10,Decimal(100)))
        self.assertEqual(sum(r['quantity'] for r in self.runtime.list_ff_stock_reservations(supply_id='1')),4)

    def test_new_source_cutoff_and_historical_exclusion(self):
        self.cutover();self.seed('3',[(101,1)],5)
        self.assertEqual(self.service.reconcile('3')['status'],'out_of_scope')
        with self.conn() as conn:
            row=conn.execute('SELECT normalized_row_json FROM sheet_vitrina_v1_wb_supplies WHERE supply_id=\'3\'').fetchone()
            record=json.loads(row[0]);record['source_created_at']='2026-11-10T00:00:00Z'
            conn.execute('UPDATE sheet_vitrina_v1_wb_supplies SET normalized_row_json=? WHERE supply_id=\'3\'',(json.dumps(record),))
        self.assertEqual(self.service.reconcile('3')['status'],'missing_source')
        self.assertEqual(self.assign('3')['fbo_lifecycle']['status'],'debited')
        from packages.application.fbo_dispatch_cost_source import capture_documents
        with self.conn() as conn:
            docs=capture_documents(conn)
        self.assertEqual(len(docs),1);self.assertEqual(docs[0]['cost_document']['lines'][0]['capital_delta_rub'],'-10')

    def test_get_keeps_replenished_and_changed_composition_pending(self):
        self.cutover();self.assign('1');self.assign('2')
        self.seed('1',[(101,6),(102,3)],2,is_cancelled=True);self.service.reconcile('1')
        state=self.service.detail('2')
        self.assertEqual((state['status'],state['reserved_qty']),('reservation_pending',0))
        self.assertEqual(self.runtime.list_ff_stock_reservations(supply_id='2'),[])
        with self.conn() as conn:
            self.assertEqual(core.reservation_summary(conn,'facility-a')['quantity'],0)
        self.assertIn('2',[item['supply_id'] for item in self.service.notifications()['items']])
        self.assertEqual(self.assign('2')['fbo_lifecycle']['status'],'reserved')
        self.seed('2',[(101,4),(102,1)])
        state=self.service.detail('2')
        self.assertEqual((state['status'],state['reserved_qty']),('reservation_pending',6))
        self.assertEqual(self.service.reconcile('2')['reserved_qty'],5)

    def test_durable_baseline_and_dateless_new_shipping_source(self):
        self.seed('3',[(101,1)],3)
        plan=self.cutover()
        self.assertEqual(len(plan['cache_baseline']),3)
        self.assertEqual(self.service.detail('3')['status'],'out_of_scope')
        for sid,status in [('4',3),('5',4),('6',5)]:
            self.seed(sid,[(101,1)],status)
            with self.conn() as conn:
                row=conn.execute('SELECT normalized_row_json FROM sheet_vitrina_v1_wb_supplies WHERE supply_id=?',(sid,)).fetchone()
                normalized=json.loads(row[0]);normalized.pop('source_created_at');normalized['supply_date']='2026-11-08'
                conn.execute('UPDATE sheet_vitrina_v1_wb_supplies SET normalized_row_json=? WHERE supply_id=?',(json.dumps(normalized),sid))
        self.assertEqual(self.service.reconcile('4')['status'],'missing_source')
        self.assertEqual(self.service.detail('5')['status'],'out_of_scope')
        self.assertEqual(self.service.detail('6')['status'],'out_of_scope')
        # A later accepted observation of an admitted source remains scoped.
        with self.conn() as conn:conn.execute('UPDATE sheet_vitrina_v1_wb_supplies SET status_id=5 WHERE supply_id=\'4\'')
        self.assertEqual(self.service.detail('4')['status'],'missing_source')

    def test_raw_creation_precedes_planned_date_and_baseline_drift(self):
        plan=self.service.migration_preview(supply_ids=['1','2'],cutoff_at=NOW)
        self.seed('9',[(101,1)],3)
        with self.assertRaisesRegex(ValueError,'migration_cache_baseline_changed'):
            self.service.migration_apply(plan,actor='fixture')
        self.cutover();self.seed('4',[(101,1)],3)
        with self.conn() as conn:
            normalized=json.loads(conn.execute('SELECT normalized_row_json FROM sheet_vitrina_v1_wb_supplies WHERE supply_id=\'4\'').fetchone()[0])
            normalized.pop('source_created_at');normalized['supply_date']='2026-11-08'
            conn.execute('UPDATE sheet_vitrina_v1_wb_supplies SET normalized_row_json=?,raw_list_json=? WHERE supply_id=\'4\'',(json.dumps(normalized),json.dumps(dict(createDate='2026-11-10T00:00:00Z'))))
        self.assertEqual(self.service.reconcile('4')['status'],'missing_source')

    def test_same_source_retry_has_durable_request_identity(self):
        self.cutover();self.assign('1');self.assign('2')
        self.seed('1',[(101,6),(102,3)],2,is_cancelled=True);self.service.reconcile('1')
        result=self.assign('2',request='retry-durable-0001')
        self.assertEqual(result['fbo_lifecycle']['status'],'reserved')
        detail=self.origins.assignment_detail('2')
        self.assertEqual(detail['current_assignment']['request_id'],'retry-durable-0001')

if __name__=='__main__':unittest.main()
