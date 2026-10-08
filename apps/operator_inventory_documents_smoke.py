#!/usr/bin/env python3
"""Synthetic W2 source receipts, native documents and actual publication witnesses."""
from copy import deepcopy
from decimal import Decimal, getcontext
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import apps.operator_warehouse_documents_smoke as w1
from apps.operator_warehouse_documents_smoke import DAY, NOW, STAMP, operations, accounting, WbFinanceWeeklyBlock, warehouse_functional_job_lock
getcontext().prec=50
from apps.fbs_document_cost_smoke import capture, doc, line, row, overhead, china, initialize_candidate, evaluate_candidate, fingerprint
from packages.application.ff_pool_documents import REQUESTS_TABLE, DOCUMENTS_TABLE, FfPoolDocumentService
from packages.application.ff_pool_foundation import BALANCES_TABLE
from packages.application.ff_pool_surfaces import FfPoolSurfaceError
from packages.application.fbs_snapshot_cost_sources import _documents


def correction(q, value, pool='FBS'):
    item=line(q=abs(q),capital=str(abs(Decimal(value))),role='correction',pool=pool)
    item['metadata_json']=json.dumps({'signed_quantity_delta':q,'signed_capital_rub':str(value)})
    result=doc('adjustment','correction',lines=[item],domain={'target_document_id':'original'})
    result['cost_document']['movements']=[{'line_no':1,'facility_id':'A','pool':pool,'nm_id':1,'quantity_delta':q,'capital_delta_rub':str(value)}]
    result['fingerprint']=fingerprint(result)
    return result


def publish_guided_recovery_queues(runtime, day, timestamp):
    """Actual publisher transaction with synthetic official rows and native pools."""
    from packages.application.fulfillment_services import _ensure_schema
    from packages.application.canonical_cost_engine import ensure_canonical_cost_schema
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory=sqlite3.Row
        _ensure_schema(conn);ensure_canonical_cost_schema(conn)
        columns=conn.execute('PRAGMA table_info(sheet_vitrina_v1_canonical_cost_baseline_versions)').fetchall()
        values={r[1]:(1 if r[2]=='INTEGER' else 'fixture') for r in columns}
        values.update(baseline_id='synthetic-guided-baseline',cutover_date=day,primary_shipment_id='synthetic-primary',primary_accepted_ff_date=day,primary_quantity='1974',weighted_ff_unit_cost_rub='10',fingerprint='sha256:synthetic-guided-baseline',report_json='{}',is_current=1,created_at=timestamp,superseded_at=None)
        conn.execute(f'INSERT INTO sheet_vitrina_v1_canonical_cost_baseline_versions({",".join(values)}) VALUES({",".join("?" for _ in values)})',list(values.values()))
        queues=[w1._targeted_recalc_request_identity(dict(r)) for r in conn.execute("SELECT * FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue WHERE status IN ('queued','running')")]
        pools=[dict(r) for r in conn.execute(f'SELECT nm_id,quantity,capital_rub FROM {BALANCES_TABLE}')]
        active=conn.execute('SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1').fetchone()[0]
    nms=sorted({r['nm_id'] for r in pools})
    lines=[];opening=[];items=[];raw_rows=[]
    for nm in nms:
        q=sum(r['quantity'] for r in pools if r['nm_id']==nm)
        c=sum(Decimal(r['capital_rub']) for r in pools if r['nm_id']==nm)
        price=c/q
        # Test only: the official WB image is a bounded synthetic row per SKU.
        wbq=Decimal(10)
        for stage in w1.STAGES:
            if stage==w1.STAGE_DISCREPANCY:continue
            quantity=Decimal(q) if stage==w1.STAGE_FF else wbq if stage==w1.STAGE_WB else Decimal(0)
            capital=c if stage==w1.STAGE_FF else wbq*price if stage==w1.STAGE_WB else Decimal(0)
            lines.append(w1.WarehouseLine(warehouse_key=stage,nm_id=nm,quantity=quantity,capital=capital,cost_covered_quantity=quantity,quality='direct_24_06',provenance={'synthetic':True},certified=True,wb_quantity=wbq if stage==w1.STAGE_WB else Decimal(0)))
        opening.append(dict(nm_id=nm,ff_unit_cost_rub=str(price),wb_unit_cost_rub=str(price),quality='direct_24_06',provenance={'synthetic':True},fingerprint='sha256:fixture'+str(nm)))
        items.append(dict(nm_id=nm,quantity='10',in_way_to_client='0',in_way_from_client='0',wb_contour_quantity='10'))
        raw_rows.append(dict(snapshot_date=day,snapshot_ts=timestamp,nmId=nm,warehouseId=507,warehouseName='Synthetic WB',stockCount=10,inWayToClient=0,inWayFromClient=0))
    block=w1.WarehouseFunctionalBlock(runtime=runtime,timestamp_factory=lambda:timestamp)
    plan=dict(contract_name='sheet_vitrina_v1_warehouse_functional',contract_version='v2',status='dry_run_ready',kind='hourly_wb_sync',cutover_id=w1.FUNCTIONAL_CUTOVER_ID,captured_at=timestamp,effective_date=day,base_active_version_id=active,local_source_digest=block._local_source_digest(recovery_end_date=day,include_historical_correction=False),wb_supply_source_digest=block._wb_supply_source_digest(),source_watermarks={'synthetic':True},absorbed_supply_revisions={},
        wb_snapshot=dict(snapshot_id='synthetic-guided-wb',fetched_at=timestamp,snapshot_date=day,requested_nm_ids=nms,pagination_complete=True,page_count=1,page_offsets=[0],raw_row_count=len(nms),raw_rows_digest='sha256:fixture',raw_rows=raw_rows,items=items),
        opening_cost_map=opening,lines=[w1._line_payload(line) for line in lines],summaries=w1._summaries(lines),unmatched_doprinato=[],supplier_cost_states=[],new_events=[],movement_documents=[],targeted_recalc_requests=queues,historical_wb_cost_projection=[],diff={'changed_line_count':len(lines),'lines':[]},invariants={'warehouse_count':6,'negative_balance_count':0,'positive_cost_gap_count':0,'wb_quantity_source':'official_snapshot_only','discrepancy_opening_zero':True})
    plan['plan_fingerprint']=w1._fingerprint(plan)
    return block.apply_plan(plan,confirm_fingerprint=plan['plan_fingerprint'],backup_dir=Path(runtime.runtime_dir)/'backups')


class SignedCostTests(unittest.TestCase):
    def test_signed_property_cases_official_stock_not_debited_twice_and_original_unchanged(self):
        for q in (-10,0,10):
            for value in (-1000,0,1000):
                if not q and not value:continue
                with self.subTest(q=q,value=value):
                    initial=initialize_candidate(capture(rows=[('A',1,100,'100')]))
                    saved=deepcopy(initial)
                    result=evaluate_candidate(initial,capture(DAY,[correction(q,value)],rows=[('A',1,100+q,'100')]))
                    actual=row(result)
                    self.assertEqual(Decimal(actual['wac_rub']), (Decimal(10000)+value)/(100+q))
                    self.assertEqual(actual['quantity'],str(100+q));self.assertEqual(initial,saved)
                    self.assertEqual(result['pending_documents'],[])
                    self.assertEqual(evaluate_candidate(result,capture(DAY,[correction(q,value)],rows=[('A',1,100+q,'100')])),result)

    def test_exact_zero_money_only_and_negative_fail_closed(self):
        initial=initialize_candidate(capture(rows=[('A',1,100,'100')]))
        zero=evaluate_candidate(initial,capture(DAY,[correction(-100,-10000)],rows=[('A',1,0,'100')]))
        self.assertIsNone(row(zero)['wac_rub']);self.assertEqual(row(zero)['capital_rub'],'0')
        self.assertEqual(zero['pending_documents'],[])
        for q,value in [(-101,-10000),(0,-10001),(-100,-9999)]:
            with self.subTest(q=q,value=value),self.assertRaises(ValueError):
                evaluate_candidate(initial,capture(DAY,[correction(q,value)],rows=[('A',1,0,'100')]))
        # All cost mass removed but official positive stock cannot have a price.
        positive=evaluate_candidate(initial,capture(DAY,[correction(-100,-10000)],rows=[('A',1,1,'100')]))
        self.assertIsNone(row(positive)['wac_rub']);self.assertTrue(positive['periods'][DAY]['diagnostics'])

    def test_fbo_adjustment_and_current_overhead_inventory_shortage_policy(self):
        initial=initialize_candidate(capture(rows=[('A',1,100,'100')],fbo=[('A',1,100,'100')]))
        result=evaluate_candidate(initial,capture(DAY,[correction(-10,-1000,'FBO'),overhead(amount='90',scope='FBO')],rows=[('A',1,100,'100')]))
        fbo=result['periods'][DAY]['document_cost_state']['fbo_rows']['A:FBO:1']
        self.assertEqual(fbo['quantity'],'90');self.assertEqual(Decimal(fbo['wac_rub']),Decimal(101))
        shortage=doc('shortage','inventory_shortage',lines=[line(q=10,role='inventory_shortage',capital='999999')])
        result=evaluate_candidate(initial,capture(DAY,[shortage,overhead(amount='100')],rows=[('A',1,90,'100')]))
        self.assertEqual(Decimal(row(result)['wac_rub']),Decimal(101))
        surplus=doc('surplus','inventory_surplus',lines=[line(q=10,role='inventory_surplus',capital='2000')])
        result=evaluate_candidate(initial,capture(DAY,[surplus],rows=[('A',1,110,'100')]))
        self.assertEqual(Decimal(row(result)['wac_rub']),Decimal(12000)/110)


    def test_china_discrepancy_companion_zero_effect_exact_parent_and_adversarial(self):
        initial=initialize_candidate(capture(rows=[('A',1,100,'100')]))
        parent=china(q=10,amount='1000')
        item=line(q=2,capital='0',role='shortage',pool=None)
        companion=doc('china_discrepancy','transfer_discrepancy',lines=[item],root='china')
        companion['cost_document'].update(document_role='china_discrepancy',relations=[{'relation_type':'discrepancy_of','parent_document_id':'china','child_document_id':'china_discrepancy','root_document_id':'china'}])
        companion['fingerprint']=fingerprint(companion)
        result=evaluate_candidate(initial,capture(DAY,[parent,companion],rows=[('A',1,110,'100')]))
        self.assertEqual(row(result)['wac_rub'],'100');self.assertEqual(result['pending_documents'],[])
        self.assertIn('china_discrepancy',result['periods'][DAY]['applied_documents'])
        for field,value in [('kind','transfer_receipt'),('root_document_id','foreign-parent')]:
            wrong=deepcopy(companion);wrong[field]=value;wrong['fingerprint']=fingerprint(wrong)
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'china_discrepancy_companion_identity_invalid'):
                evaluate_candidate(initial,capture(DAY,[parent,wrong],rows=[('A',1,110,'100')]))
        for operand in ('capital_rub','expense_rub'):
            wrong=deepcopy(companion);wrong['cost_document']['lines'][0][operand]='1';wrong['fingerprint']=fingerprint(wrong)
            with self.subTest(operand=operand),self.assertRaisesRegex(ValueError,'china_discrepancy_companion_effect_invalid'):
                evaluate_candidate(initial,capture(DAY,[parent,wrong],rows=[('A',1,110,'100')]))


class InventoryDocuments(unittest.TestCase):
    # Reuse fixtures, not W1 tests; each test has its own disposable native DB.
    setUp=w1.OperatorDocuments.setUp
    preview=w1.OperatorDocuments.preview
    confirm=w1.OperatorDocuments.confirm
    quantities=w1.OperatorDocuments.quantities
    drain=w1.OperatorDocuments.drain
    prepare=w1.OperatorDocuments.prepare
    functional_publish=w1.OperatorDocuments.functional_publish
    realloc=w1.OperatorDocuments.realloc

    def set_catalog(self, nms=(1,)):
        self.runtime.list_nomenclature_items()
        with sqlite3.connect(self.runtime.db_path) as conn:
            conn.execute('DELETE FROM sheet_vitrina_v1_nomenclature_items')
            for nm in nms:
                columns=conn.execute('PRAGMA table_info(sheet_vitrina_v1_nomenclature_items)').fetchall()
                values={r[1]:(0 if r[2]=='INTEGER' else '') for r in columns if r[3] and r[4] is None}
                values.update(item_id=str(nm),nm_id=nm,our_sku='SKU'+str(nm),nomenclature_name='Synthetic '+str(nm),barcode=str(1000+nm),barcodes_json='[]',is_active=1,is_hidden=0,created_at=STAMP,updated_at=STAMP)
                conn.execute(f'INSERT INTO sheet_vitrina_v1_nomenclature_items({",".join(values)}) VALUES({",".join("?" for _ in values)})',list(values.values()))

    def inventory(self,name,fbs=18,fbo=22,scope='both'):
        return self.preview(name,'pool_inventory',{'facility_id':'A','scope':scope,'targets':[{'nm_id':1,'target_fbs':fbs,'target_fbo':fbo}]})

    def base_document(self):
        return self.confirm(self.realloc('original',2))['document']['document_id']

    def signed(self,name,target,q=-1,value='-10',pool='FBS'):
        return self.preview(name,'correction',{'target_document_id':target,'movements':[{'facility_id':'A','pool':pool,'nm_id':1,'quantity_delta':q,'capital_delta_rub':value}]})

    def test_full_roster_explicit_zero_prestate_and_epoch(self):
        self.set_catalog((1,2))
        with self.assertRaises(FfPoolSurfaceError):self.inventory('incomplete')
        with self.assertRaises(FfPoolSurfaceError):self.preview('implicit','pool_inventory',{'facility_id':'A','scope':'both','targets':[{'nm_id':1,'target_fbs':0},{'nm_id':2,'target_fbs':0}]})
        manifest={'facility_id':'A','scope':'FBS','targets':[{'nm_id':1,'target_fbs':20},{'nm_id':2,'target_fbs':0}]}
        preview=self.preview('dense-explicit', 'pool_inventory',manifest)
        # Irrelevant nomenclature timestamp/name does not change canonical roster.
        with sqlite3.connect(self.runtime.db_path) as conn:conn.execute("UPDATE sheet_vitrina_v1_nomenclature_items SET updated_at='irrelevant',nomenclature_name='Renamed'")
        receipt=self.confirm(preview);self.assertTrue(receipt['physical_applied'],receipt)
        self.assertIn(('A','FBS',2,0,'0'),self.quantities())
        preview=self.preview('stale-before','pool_inventory',manifest)
        with sqlite3.connect(self.runtime.db_path) as conn:conn.execute(f"UPDATE {BALANCES_TABLE} SET quantity=21 WHERE facility_id='A' AND pool='FBS' AND nm_id=1")
        with self.assertRaises(FfPoolSurfaceError) as error:self.confirm(preview)
        self.assertEqual(error.exception.code,'inventory_source_prestate_changed')

    def test_inventory_busy_bad_parity_crash_children_and_unselected_cas(self):
        self.set_catalog();preview=self.inventory('busy')
        before=self.quantities()
        with warehouse_functional_job_lock(self.root),patch('packages.application.ff_pool_documents._guided_current_aggregate_parity_proof',side_effect=AssertionError('derived unavailable')):
            receipt=self.confirm(preview)
        self.assertTrue(receipt['durable_saved']);self.assertFalse(receipt['physical_applied']);self.assertEqual(before,self.quantities())
        self.drain();receipt=operations.read_acceptance(self.runtime.db_path,preview['request_id'])
        self.assertTrue(receipt['physical_applied'],receipt)
        with operations.readonly(self.runtime.db_path) as conn:
            docs=_documents(conn)
            kinds={d['kind'] for d in docs}
            self.assertTrue({'pool_inventory','inventory_shortage','inventory_surplus'}<=kinds)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue WHERE stable_source_id=?",('ff_pool_document:'+receipt['document']['document_id'],)).fetchone()[0],1)
        self.drain();self.assertEqual(receipt['document'],operations.read_acceptance(self.runtime.db_path,preview['request_id'])['document'])
        other=self.inventory('other-pool',scope='FBS')
        with patch.object(operations,'try_post'):self.confirm(other)
        with sqlite3.connect(self.runtime.db_path) as conn:conn.execute(f"UPDATE {BALANCES_TABLE} SET capital_rub='221' WHERE facility_id='A' AND pool='FBO'")
        before=self.quantities();self.drain();read=operations.read_acceptance(self.runtime.db_path,other['request_id'])
        self.assertEqual(read['reason_code'],'inventory_source_prestate_changed');self.assertFalse(read['physical_applied']);self.assertEqual(before,self.quantities())

    def test_correction_lost_response_concurrent_debits_and_exact_saved_operands(self):
        target=self.base_document();a=self.signed('a',target,-12,'-120');b=self.signed('b',target,-13,'-130')
        before=self.quantities()
        with patch.object(operations,'try_post',side_effect=TimeoutError('source saved')):receipt=self.confirm(a);self.confirm(b)
        self.assertFalse(receipt['physical_applied']);self.assertEqual(before,self.quantities())
        self.assertEqual(self.confirm(a),receipt)
        self.drain()
        receipts=[operations.read_acceptance(self.runtime.db_path,p['request_id']) for p in (a,b)]
        self.assertEqual(sum(r['physical_applied'] for r in receipts),1)
        posted=next(r for r in receipts if r['physical_applied'])
        with operations.readonly(self.runtime.db_path) as conn:_documents(conn)
        # SQL signs cannot replace immutable saved canonical operands.
        with sqlite3.connect(self.runtime.db_path) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE sheet_vitrina_v1_ff_pool_document_lines SET metadata_json=? WHERE document_id=?",(json.dumps({'signed_quantity_delta':12,'signed_capital_rub':'120'}),posted['document']['document_id']))
            # Simulate damaged storage only in this disposable fixture.
            for trigger in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='sheet_vitrina_v1_ff_pool_document_lines'").fetchall():
                conn.execute('DROP TRIGGER '+trigger[0])
            conn.execute("UPDATE sheet_vitrina_v1_ff_pool_document_lines SET metadata_json=? WHERE document_id=?",(json.dumps({'signed_quantity_delta':12,'signed_capital_rub':'120'}),posted['document']['document_id']))
        with operations.readonly(self.runtime.db_path) as conn,self.assertRaisesRegex(ValueError,'typed_document_saved_line_mismatch'):_documents(conn)

    def test_one_confirmed_storno_target_native_reversal_and_evidence_only_late_expense(self):
        target=self.base_document();a=self.preview('storno-a','storno',{'target_document_id':target});b=self.preview('storno-b','storno',{'target_document_id':target,'comment':'second independent confirmation'})
        with patch.object(operations,'try_post'):self.confirm(a)
        with self.assertRaises(FfPoolSurfaceError) as error:self.confirm(b)
        self.assertEqual(error.exception.code,'storno_confirmation_exists')
        self.drain();self.assertEqual(dict((pool,q) for fac,pool,nm,q,c in self.quantities() if fac=='A'),{'FBS':20,'FBO':20})
        root=self.confirm(self.preview('transfer','transfer_root',{'source':{'facility_id':'A','pool':'FBS'},'destination':{'facility_id':'B','pool':'FBO'}}))['document']['document_id']
        self.confirm(self.preview('shipment','transfer_shipment',{'root_document_id':root,'items':[{'nm_id':1,'quantity':5}]}))
        late=self.preview('late','late_expense',{'root_document_id':root,'expenses':[{'amount_rub':'25','basis':'Synthetic bill','source_file_sha256':'sha256:'+'a'*64}]})
        before=self.quantities()
        receipt=self.confirm(late);self.assertTrue(receipt['physical_applied'],receipt);self.assertEqual(before,self.quantities())
        with operations.readonly(self.runtime.db_path) as conn:
            queue=conn.execute("SELECT affected_nm_ids_json FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue WHERE stable_source_id=?",('ff_pool_document:'+receipt['document']['document_id'],)).fetchone()
            self.assertEqual(json.loads(queue[0]),[1]);_documents(conn)

    def test_native_mixed_signs_money_only_zero_negative_and_ordinary_guards(self):
        from packages.application.ff_pool_documents import _typed_adjustment_allowed, _validate_balance_effect, FfPoolDocumentError
        target=self.base_document()
        original=self.quantities()
        for name,q,value,pool in [('mixed-increase',1,'-10','FBS'),('mixed-decrease',-1,'10','FBS'),('money-only',0,'10','FBS'),('mixed-fbo',1,'-10','FBO')]:
            preview=self.signed(name,target,q,value,pool)
            receipt=self.confirm(preview);self.assertTrue(receipt['physical_applied'],receipt)
            # Verify the actual immutable target and exact reversal independently
            # of a caller-provided document kind.
            with operations.readonly(self.runtime.db_path) as conn:
                native=next(d for d in _documents(conn) if d['document_id']==receipt['document']['document_id'])
                old=native['cost_document']['movements'][0]
                movement={k:old[k] for k in ('facility_id','pool','nm_id')}
                movement.update(quantity_delta=-old['quantity_delta'],capital_delta_cents=-int(Decimal(old['capital_delta_rub'])*100),metadata={'target_document_id':native['document_id']})
                forged={'document_kind':'storno','relation':{'relation_type':'storno_of','parent_document_id':native['document_id']},'movements':[movement]}
                self.assertTrue(_typed_adjustment_allowed(conn,forged))
                movement['quantity_delta']+=1
                with self.assertRaises(FfPoolDocumentError) as mismatch:_typed_adjustment_allowed(conn,forged)
                self.assertEqual(mismatch.exception.code,'storno_correction_reversal_mismatch')
            reverse=self.preview(name+'-reverse','storno',{'target_document_id':receipt['document']['document_id']})
            reversed_receipt=self.confirm(reverse);self.assertTrue(reversed_receipt['physical_applied'],reversed_receipt)
            self.assertEqual(self.quantities(),original)
        for q,value,reason in [(-19,'-190','negative_pool_balance'),(0,'-201','negative_pool_balance'),(-18,'-170','pool_quantity_capital_zero_mismatch')]:
            preview=self.signed('invalid-'+str(q)+value,target,q,value)
            with self.assertRaises(FfPoolSurfaceError) as error:self.confirm(preview)
            self.assertEqual(error.exception.code,reason)
            self.assertIsNone(operations.read_acceptance(self.runtime.db_path,preview['request_id']))
        with operations.readonly(self.runtime.db_path) as conn:
            forged={'document_kind':'storno','relation':{'relation_type':'storno_of','parent_document_id':target},'movements':[]}
            self.assertFalse(_typed_adjustment_allowed(conn,forged)) # actual target is ordinary reallocation
        for q,value in [(1,-100),(-1,100)]:
            with self.assertRaises(FfPoolDocumentError):
                _validate_balance_effect(before_quantity=18,before_capital=Decimal(180),movement={'quantity_delta':q,'capital_delta_cents':value})
        zero=self.confirm(self.signed('native-zero',target,-18,'-180'))
        self.assertTrue(zero['physical_applied'],zero)
        self.assertIn(('A','FBS',1,0,'0'),self.quantities())

    def test_inventory_parent_storno_cannot_claim_child_movements_reversed(self):
        self.set_catalog()
        inventory=self.confirm(self.inventory('parent-storno-guard'))
        parent=inventory['document']['document_id']
        before=self.quantities()
        preview=self.preview('empty-parent-reversal','storno',{'target_document_id':parent})
        with self.assertRaises(FfPoolSurfaceError) as error:self.confirm(preview)
        self.assertEqual(error.exception.code,'storno_target_has_no_direct_effect')
        self.assertIsNone(operations.read_acceptance(self.runtime.db_path,preview['request_id']))
        self.assertEqual(before,self.quantities())
        for child in inventory['summary']['child_document_ids']:
            reverse=self.preview('child-reversal-'+child,'storno',{'target_document_id':child})
            receipt=self.confirm(reverse)
            self.assertTrue(receipt['physical_applied'],receipt)
        self.assertEqual(dict((pool,q) for fac,pool,nm,q,c in self.quantities() if fac=='A'),{'FBS':20,'FBO':20})

    def test_two_inventory_versions_cannot_reinterpret_authorized_effect(self):
        self.set_catalog();a=self.inventory('count-a',18,20);b=self.inventory('count-b',17,20)
        with patch.object(operations,'try_post'):self.confirm(a);self.confirm(b)
        self.drain();receipts=[operations.read_acceptance(self.runtime.db_path,p['request_id']) for p in (a,b)]
        self.assertEqual(sum(r['physical_applied'] for r in receipts),1)
        self.assertEqual(next(r for r in receipts if not r['physical_applied'])['reason_code'],'inventory_source_prestate_changed')

    def test_correction_after_cutoff_waits_for_next_owned_capture(self):
        target=self.base_document()
        first=self.signed('before-cutoff',target,-1,'-10')
        second=self.signed('after-cutoff',target,-2,'-20')
        with patch.object(operations,'try_post'):self.confirm(first)
        actual=operations._post;created=[]
        def crossing(service,row):
            actual(service,row)
            if row['request_id']==first['request_id'] and not created:
                operations.confirm_source(self.surface,second['request_id'],actor='final confirmer')
                created.append(second['request_id'])
        with patch.object(operations,'_post',side_effect=crossing):drained=self.drain()
        self.assertIn(first['request_id'],drained['request_ids'])
        self.assertNotIn(second['request_id'],drained['request_ids'])
        pending=operations.read_acceptance(self.runtime.db_path,second['request_id'])
        self.assertFalse(pending['physical_applied']);self.assertEqual(pending['actor'],'final confirmer')
        self.assertIn(second['request_id'],self.drain()['request_ids'])
        self.assertTrue(operations.read_acceptance(self.runtime.db_path,second['request_id'])['physical_applied'])

    def test_correction_actual_reserve_recheck(self):
        target=self.base_document()
        with patch.object(self,'realloc',side_effect=lambda name,q=1:self.signed(name,target,-q,str(-q*10))):
            w1.OperatorDocuments.test_reservation_guard_applies_after_source_save_before_physical_post(self)

    def test_inventory_native_post_crash_keeps_exact_atomic_queue(self):
        self.set_catalog();preview=self.inventory('post-crash')
        actual=operations._post
        def crash(service,row):actual(service,row);raise TimeoutError('lost after physical commit')
        with patch.object(operations,'_post',side_effect=crash):receipt=self.confirm(preview)
        self.assertTrue(receipt['physical_applied'],receipt);self.assertNotEqual(receipt['state'],'completed')
        before=self.quantities();self.drain();self.assertEqual(before,self.quantities())
        with operations.readonly(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {DOCUMENTS_TABLE} WHERE request_id=?',(receipt['request_id'],)).fetchone()[0],3)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue WHERE stable_source_id=?",('ff_pool_document:'+receipt['document']['document_id'],)).fetchone()[0],1)

    def test_native_china_mismatch_companion_and_guided_storno_continuations(self):
        from tempfile import TemporaryDirectory
        from apps import warehouse_ff_acceptance_form_smoke as guided
        with TemporaryDirectory(prefix='w2-guided-local-') as directory:
            runtime,surface=guided._fixture(Path(directory))
            payload=guided._payload(surface,'split')
            payload.update(business_date=guided.DAY,rows=[{'nm_id':guided.NM_ID,'accepted_quantity':9,'quantity_fbs':6,'quantity_fbo':3,'comment':'Synthetic shortage'}])
            preview=surface.accept_china_form(payload,actor='synthetic')
            receipt=surface.confirm_document(preview['request_id'])['acceptance']
            self.assertTrue(receipt['physical_applied'],receipt)
            with warehouse_functional_job_lock(runtime.runtime_dir):operations.drain(runtime,timestamp_factory=lambda:guided.NOW)
            with operations.readonly(runtime.db_path) as conn:
                documents=_documents(conn)
                parent=next(d for d in documents if d['document_id']==receipt['document']['document_id'])
                child=next(d for d in documents if d['document_id']==parent['document_id']+'_discrepancy')
                self.assertEqual(child['cost_document']['document_role'],'china_discrepancy')
                from packages.application.fbs_document_cost import china_discrepancy_companion
                self.assertTrue(china_discrepancy_companion(child,{d['document_id']:d for d in documents}))
            reverse=surface.accept_document_preview({'request_id':'w2:guided-storno','business_date':guided.DAY,'document_kind':'storno','manifest':{'target_document_id':parent['document_id']}},actor='synthetic')
            reversed_receipt=surface.confirm_document(reverse['request_id'])['acceptance']
            self.assertTrue(reversed_receipt['physical_applied'],reversed_receipt)
            with operations.readonly(runtime.db_path) as conn:
                source=json.loads(conn.execute(f'SELECT source_json FROM {operations.TABLE} WHERE request_id=?',(reverse['request_id'],)).fetchone()[0])
                extra=conn.execute("SELECT source_revision,effective_date,affected_nm_ids_json FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue WHERE stable_source_id=? AND source_revision=?",('supplier_shipment:'+guided.SHIPMENT,source['request_identity'])).fetchone()
                self.assertEqual(extra['effective_date'],guided.DAY);self.assertEqual(json.loads(extra['affected_nm_ids_json']),[guided.NM_ID])
            with warehouse_functional_job_lock(runtime.runtime_dir):operations.drain(runtime,timestamp_factory=lambda:guided.NOW)
            with operations.readonly(runtime.db_path) as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM sheet_vitrina_v1_ff_guided_acceptance_recoveries WHERE recovery_request_id=?',(reverse['request_id'],)).fetchone()[0],1)
                _documents(conn)
            self.assertNotEqual(operations.read_acceptance(runtime.db_path,reverse['request_id'])['state'],'completed')
            with operations.readonly(runtime.db_path) as conn:
                self.assertIsNone(conn.execute('SELECT actual_ff_acceptance_date FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?',(guided.SHIPMENT,)).fetchone()[0])
            with warehouse_functional_job_lock(runtime.runtime_dir):publish_guided_recovery_queues(runtime,guided.DAY,guided.NOW)
            receipt=operations.read_acceptance(runtime.db_path,reverse['request_id'])
            primary=receipt['processing_receipt']['functional_publication']
            supplier=receipt['processing_receipt']['recovery_functional_publication']
            self.assertEqual(primary['version_id'],supplier['version_id'])
            self.assertEqual(primary['plan_fingerprint'],supplier['plan_fingerprint'])
            self.assertEqual(supplier['source_revision'],source['request_identity'])
            self.assertEqual(supplier['stable_source_id'],'supplier_shipment:'+guided.SHIPMENT)
            with operations.readonly(runtime.db_path) as conn:
                for witness in (primary,supplier):
                    self.assertEqual(conn.execute('SELECT status FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue WHERE queue_id=?',(witness['queue_id'],)).fetchone()[0],'complete')
            self.assertNotEqual(receipt['state'],'completed') # no book/Finance proof yet

    def test_guided_storno_rejects_foreign_publication_and_material_row_drift(self):
        from tempfile import TemporaryDirectory
        from apps import warehouse_ff_acceptance_form_smoke as guided
        from packages.application.warehouse_fbs_material_rematerialization import publish_fbs_pool_aggregate_revision
        with TemporaryDirectory(prefix='w2-guided-adversarial-') as directory:
            runtime,surface=guided._fixture(Path(directory))
            payload=guided._payload(surface);payload['business_date']=guided.DAY
            preview=surface.accept_china_form(payload,actor='synthetic')
            receipt=surface.confirm_document(preview['request_id'])['acceptance']
            with warehouse_functional_job_lock(runtime.runtime_dir):operations.drain(runtime,timestamp_factory=lambda:guided.NOW)
            target=receipt['document']['document_id']
            reverse=surface.accept_document_preview({'request_id':'w2:guided-drift','business_date':guided.DAY,'document_kind':'storno','manifest':{'target_document_id':target}},actor='synthetic')
            with sqlite3.connect(runtime.db_path) as conn:
                active=conn.execute('SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1').fetchone()[0]
                quality=conn.execute("SELECT quality FROM sheet_vitrina_v1_warehouse_functional_balances WHERE version_id=? AND warehouse_key='ff' AND nm_id=?",(active,guided.NM_ID)).fetchone()[0]
                conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_balances SET quality='foreign-row' WHERE version_id=? AND warehouse_key='ff' AND nm_id=?",(active,guided.NM_ID))
            with self.assertRaises(FfPoolSurfaceError) as error:surface.confirm_document(reverse['request_id'])
            self.assertEqual(error.exception.code,'guided_recovery_projection_drift')
            with sqlite3.connect(runtime.db_path) as conn:
                conn.row_factory=sqlite3.Row
                conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_balances SET quality=? WHERE version_id=? AND warehouse_key='ff' AND nm_id=?",(quality,active,guided.NM_ID))
                # Actual native publication; no fake active-version insertion.
                conn.execute('BEGIN IMMEDIATE') if not conn.in_transaction else None
                publish_fbs_pool_aggregate_revision(conn,affected_nm_ids=[guided.NM_ID],source_kind='synthetic_foreign_publication',source_id='foreign',business_date=guided.DAY,published_at=guided.NOW,source_business_date=guided.DAY)
            with self.assertRaises(FfPoolSurfaceError) as error:surface.confirm_document(reverse['request_id'])
            self.assertEqual(error.exception.code,'guided_recovery_projection_drift')
            self.assertIsNone(operations.read_acceptance(runtime.db_path,reverse['request_id']))

    def test_guided_storno_row_race_is_checked_inside_native_transaction(self):
        from tempfile import TemporaryDirectory
        from apps import warehouse_ff_acceptance_form_smoke as guided
        from packages.application.warehouse_recovery_policy import WarehouseRecoveryRegistry
        with TemporaryDirectory(prefix='w2-guided-native-race-') as directory:
            runtime,surface=guided._fixture(Path(directory));payload=guided._payload(surface);payload['business_date']=guided.DAY
            preview=surface.accept_china_form(payload,actor='synthetic')
            receipt=surface.confirm_document(preview['request_id'])['acceptance']
            with warehouse_functional_job_lock(runtime.runtime_dir):operations.drain(runtime,timestamp_factory=lambda:guided.NOW)
            reverse=surface.accept_document_preview({'request_id':'w2:guided-race','business_date':guided.DAY,'document_kind':'storno','manifest':{'target_document_id':receipt['document']['document_id']}},actor='synthetic')
            with patch.object(operations,'try_post'):surface.confirm_document(reverse['request_id'])
            with sqlite3.connect(runtime.db_path) as conn:before=conn.execute(f'SELECT * FROM {BALANCES_TABLE} ORDER BY facility_id,pool,nm_id').fetchall()
            original=WarehouseRecoveryRegistry.prepare_t1
            def crossing(owner,**kwargs):
                result=original(owner,**kwargs)
                with sqlite3.connect(runtime.db_path) as conn:conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_balances SET quality='raced' WHERE version_id=(SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1) AND warehouse_key='ff' AND nm_id=?",(guided.NM_ID,))
                return result
            with patch.object(WarehouseRecoveryRegistry,'prepare_t1',crossing),warehouse_functional_job_lock(runtime.runtime_dir):operations.drain(runtime,timestamp_factory=lambda:guided.NOW)
            read=operations.read_acceptance(runtime.db_path,reverse['request_id'])
            self.assertFalse(read['physical_applied']);self.assertEqual(read['reason_code'],'guided_recovery_projection_drift')
            with sqlite3.connect(runtime.db_path) as conn:self.assertEqual(before,conn.execute(f'SELECT * FROM {BALANCES_TABLE} ORDER BY facility_id,pool,nm_id').fetchall())

    def test_inventory_workbook_real_template_readonly_and_confirmation(self):
        self.set_catalog()
        with patch.object(FfPoolDocumentService,'__init__',side_effect=AssertionError('GET bootstrap')):data,name=self.surface.inventory_template('A','both')
        preview=self.surface.accept_inventory_workbook(request_id='w2:workbook',business_date=DAY,workbook_bytes=data,filename=name,content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',actor='synthetic')
        self.assertEqual(preview['state'],'ready',preview)
        receipt=self.confirm(preview);self.assertTrue(receipt['physical_applied'],receipt)

    def complete(self, preview):
        with warehouse_functional_job_lock(self.root):self.functional_publish()
        self.prepare();version=accounting.load(self.root)[1]
        finance=WbFinanceWeeklyBlock(self.root,now_factory=lambda:NOW).recalculate_stale_cost_weeks()
        finance.update(accounting_version=version,accounting_version_before=version,accounting_version_unchanged=True)
        with warehouse_functional_job_lock(self.root):
            result=operations.reconcile(self.runtime,request_ids=[preview['request_id']],finance_receipt=finance,economics_receipt=accounting.current_publication_receipt(self.runtime,now=NOW),now=NOW)
        receipt=operations.read_acceptance(self.runtime.db_path,preview['request_id'])
        self.assertEqual(result['processed_count'],1,receipt);self.assertEqual(receipt['state'],'completed')
        return receipt

    def test_actual_publication_signed_correction(self):
        target=self.base_document();self.prepare(opening=True)
        preview=self.signed('exact-signed',target,-1,'-10');self.confirm(preview)
        receipt=self.complete(preview);self.assertEqual(receipt['summary']['quantity_delta'],-1)

    def test_actual_publication_same_open_day_storno(self):
        self.prepare(opening=True);target=self.base_document()
        preview=self.preview('exact-storno','storno',{'target_document_id':target});self.confirm(preview)
        self.complete(preview)

    def test_actual_publication_open_transit_late_expense(self):
        self.prepare(opening=True)
        root=self.confirm(self.preview('root','transfer_root',{'source':{'facility_id':'A','pool':'FBS'},'destination':{'facility_id':'B','pool':'FBO'}}))['document']['document_id']
        self.confirm(self.preview('shipment','transfer_shipment',{'root_document_id':root,'items':[{'nm_id':1,'quantity':5}]}))
        preview=self.preview('exact-late','late_expense',{'root_document_id':root,'expenses':[{'amount_rub':'25','basis':'Synthetic bill'}]});self.confirm(preview)
        self.complete(preview)

    def test_exact_actual_publication_covers_parent_and_both_children(self):
        self.set_catalog();self.prepare(opening=True)
        preview=self.inventory('exact-children');receipt=self.confirm(preview)
        self.assertTrue(receipt['physical_applied'],receipt)
        with warehouse_functional_job_lock(self.root):self.functional_publish()
        self.prepare();version=accounting.load(self.root)[1]
        finance=WbFinanceWeeklyBlock(self.root,now_factory=lambda:NOW).recalculate_stale_cost_weeks()
        finance.update(accounting_version=version,accounting_version_before=version,accounting_version_unchanged=True)
        with warehouse_functional_job_lock(self.root):
            result=operations.reconcile(self.runtime,request_ids=[preview['request_id']],finance_receipt=finance,economics_receipt=accounting.current_publication_receipt(self.runtime,now=NOW),now=NOW)
        receipt=operations.read_acceptance(self.runtime.db_path,preview['request_id'])
        self.assertEqual(result['processed_count'],1,receipt)
        self.assertEqual(len(receipt['processing_receipt']['document_operands']),3)
        self.assertEqual(receipt['state'],'completed')

if __name__=='__main__':unittest.main()
