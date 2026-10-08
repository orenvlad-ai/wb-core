#!/usr/bin/env python3
"""Disposable W1 acceptance, physical guards and exact native publication proofs."""
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from apps.ready_publication_smoke import seed, save, make_plan
from apps.fbs_document_cost_smoke import capture
from apps.fbs_inventory_presentation_smoke import retained
from apps.shared_sku_cost_smoke import wb
from apps import warehouse_ff_acceptance_form_smoke as guided
from packages.application import operator_warehouse_documents as operations, operator_operations, fbs_accounting_runtime as accounting
from packages.application.ff_pool_documents import FfPoolDocumentService, REQUESTS_TABLE, DOCUMENTS_TABLE, _fingerprint
from packages.application.ff_pool_foundation import FEATURE_EPOCHS_TABLE, FACILITIES_TABLE, BALANCES_TABLE
from packages.application.ff_pool_surfaces import FfPoolSurface, FfPoolSurfaceError
from packages.application.fbs_snapshot_cost_sources import _documents
from packages.application.warehouse_functional import WarehouseFunctionalBlock, WarehouseLine, STAGES, STAGE_FF, STAGE_WB, STAGE_DISCREPANCY, FUNCTIONAL_CUTOVER_ID, _line_payload, _summaries, _targeted_recalc_request_identity
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock

DAY='2026-09-08'
NOW=datetime(2026,9,8,14,tzinfo=timezone.utc)
STAMP=NOW.isoformat().replace('+00:00','Z')

class OperatorDocuments(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory(prefix='w1-operator-local-');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.runtime=seed(self.root)
        self.service=FfPoolDocumentService(db_path=self.runtime.db_path,runtime_dir=self.root,resume=False,timestamp_factory=lambda:STAMP)
        self.surface=FfPoolSurface(db_path=self.runtime.db_path,runtime_dir=self.root,timestamp_factory=lambda:STAMP)
        with sqlite3.connect(self.runtime.db_path) as conn:
            conn.execute(f"INSERT INTO {FEATURE_EPOCHS_TABLE}(epoch,writer_enabled,reader_enabled,source_revision,created_at,metadata_json) VALUES(1,1,0,'fixture',?,'{{}}')",(STAMP,))
            for facility in ('A','B'):
                conn.execute(f"INSERT INTO {FACILITIES_TABLE}(facility_id,code,name,active,display_timezone,created_at,updated_at) VALUES(?,?,?,1,'Europe/Moscow',?,?)",(facility,facility,'Синтетический '+facility,STAMP,STAMP))
            for pool in ('FBS','FBO'):
                conn.execute(f"INSERT INTO {BALANCES_TABLE}(facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,wac_rub,source_watermark,updated_at) VALUES('A',?,1,1,20,'200','10','fixture',?)",(pool,STAMP))

    def preview(self, name, kind, manifest, day=DAY):
        return self.surface.accept_document_preview(dict(request_id="w1:doc:"+name,document_kind=kind,business_date=day,manifest=manifest),actor='synthetic operator')

    def confirm(self, preview):
        return self.surface.confirm_document(preview['request_id'])['acceptance']

    def realloc(self,name,q=1):
        return self.preview(name,'pool_reallocation',{'facility_id':'A','source_pool':'FBS','destination_pool':'FBO','items':[{'nm_id':1,'quantity':q}]})

    def quantities(self):
        with sqlite3.connect(self.runtime.db_path) as conn:
            return conn.execute(f'SELECT facility_id,pool,nm_id,quantity,capital_rub FROM {BALANCES_TABLE} ORDER BY facility_id,pool,nm_id').fetchall()

    def drain(self):
        with warehouse_functional_job_lock(self.root):return operations.drain(self.runtime,timestamp_factory=lambda:STAMP)

    def test_source_save_crash_same_id_readback_and_no_global_processing_reset(self):
        first=self.realloc('saved-before-crash')
        other=self.realloc('unrelated-processing',2)
        with sqlite3.connect(self.runtime.db_path) as conn:conn.execute(f"UPDATE {REQUESTS_TABLE} SET state='processing' WHERE request_id=?",(other['request_id'],))
        before=self.quantities()
        with patch.object(operations,'try_post',side_effect=TimeoutError('lost after save')):
            receipt=self.confirm(first)
        self.assertTrue(receipt['durable_saved']);self.assertFalse(receipt['physical_applied'])
        self.assertEqual(before,self.quantities())
        with patch.object(FfPoolDocumentService,'__init__',side_effect=AssertionError('GET construction')):
            self.assertEqual(operator_operations.read_acceptance(self.runtime.db_path,'w1:doc:saved-before-crash'),receipt)
            self.assertEqual(operator_operations.journal(self.runtime.db_path)['total'],1)
        FfPoolDocumentService(db_path=self.runtime.db_path,runtime_dir=self.root,resume=False,bootstrap=False)
        with sqlite3.connect(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute(f'SELECT state FROM {REQUESTS_TABLE} WHERE request_id=?',(other['request_id'],)).fetchone()[0],'processing')
            for sql in (f"UPDATE {REQUESTS_TABLE} SET business_date='2026-09-07' WHERE request_id=?",f"DELETE FROM {operations.TABLE} WHERE request_id=?"):
                with self.assertRaises(sqlite3.IntegrityError):conn.execute(sql,(first['request_id'],))
        self.assertEqual(self.confirm(first),receipt) # readback, no second physical attempt
        self.drain();after=self.quantities();self.assertNotEqual(before,after)
        self.drain();self.assertEqual(after,self.quantities())

    def test_common_journal_global_pagination_keeps_native_expense_identity(self):
        preview=self.realloc('journal-warehouse')
        with patch.object(operations,'try_post'):warehouse_receipt=self.confirm(preview)
        expense=self.surface.accept_pool_overhead_preview({'request_id':'w1:journal:expense','facility_id':'A','business_date':DAY,'scope':'FBS','amount_rub':'10','category':'storage','comment':'Synthetic journal expense','source_mode':'manual'},actor='expense author')
        expense_receipt=self.surface.confirm_document(expense['request_id'])['acceptance']
        page1=operator_operations.journal(self.runtime.db_path,page=1,limit=1)
        page2=operator_operations.journal(self.runtime.db_path,page=2,limit=1)
        self.assertEqual(page1['total'],2);self.assertTrue(page1['has_more']);self.assertFalse(page2['has_more'])
        self.assertEqual({page1['items'][0]['operation_id'],page2['items'][0]['operation_id']},{warehouse_receipt['operation_id'],expense_receipt['operation_id']})
        detail=operator_operations.read_acceptance(self.runtime.db_path,expense['request_id'])
        self.assertEqual(detail['operation_id'],expense_receipt['operation_id']);self.assertEqual(detail['document_kind'],'pool_overhead')
        self.assertEqual(detail['domain'],'ff_pool_document');self.assertTrue(detail['title_ru'])

    def test_concurrent_confirmed_documents_actual_debit_rechecks_available_stock(self):
        a=self.realloc('concurrent-a',15);b=self.realloc('concurrent-b',16)
        before=self.quantities()
        with patch.object(operations,'try_post'):
            self.confirm(a);self.confirm(b)
        self.assertEqual(before,self.quantities())
        self.drain()
        receipts=[operations.read_acceptance(self.runtime.db_path,p['request_id']) for p in (a,b)]
        self.assertEqual(sum(r['physical_applied'] for r in receipts),1)
        self.assertEqual([r for r in receipts if not r['physical_applied']][0]['state'],'needs_attention')
        self.assertEqual(dict((pool,q) for fac,pool,nm,q,c in self.quantities() if fac=='A'),{'FBS':5,'FBO':35})

    def test_reservation_guard_applies_after_source_save_before_physical_post(self):
        preview=self.realloc('reserved-later',15)
        with patch.object(operations,'try_post'):self.confirm(preview)
        # Native current reservation + exact epoch source, without fabricated physical movement.
        from packages.application.ff_pool_cutover import ensure_ff_pool_cutover_schema, MANIFESTS_TABLE
        from packages.application.ff_pool_fbs_lifecycle import ensure_ff_pool_fbs_lifecycle_schema, CURRENT_TABLE
        with sqlite3.connect(self.runtime.db_path) as conn:
            ensure_ff_pool_cutover_schema(conn);ensure_ff_pool_fbs_lifecycle_schema(conn)
            columns=conn.execute(f'PRAGMA table_info({MANIFESTS_TABLE})').fetchall()
            values={r[1]:(0 if r[2]=='INTEGER' else 'fixture') for r in columns};values.update(cutover_id='reserve-fixture',manifest_digest='sha256:'+'a'*64,deployed_sha='a'*40,cutover_at=STAMP,business_date=DAY,feature_epoch=1,created_at=STAMP,manifest_json='{}')
            conn.execute(f'INSERT INTO {MANIFESTS_TABLE}({",".join(values)}) VALUES({",".join("?" for _ in values)})',list(values.values()))
            columns=conn.execute(f'PRAGMA table_info({CURRENT_TABLE})').fetchall()
            values={r[1]:(1 if r[2]=='INTEGER' else 'fixture') for r in columns};values.update(order_id=1,cutover_id='reserve-fixture',facility_id='A',pool='FBS',nm_id=1,quantity=10,state='reserved',frozen_wac_rub='10',updated_at=STAMP)
            conn.execute(f'INSERT INTO {CURRENT_TABLE}({",".join(values)}) VALUES({",".join("?" for _ in values)})',list(values.values()))
        before=self.quantities();self.drain()
        receipt=operations.read_acceptance(self.runtime.db_path,preview['request_id'])
        self.assertEqual(receipt['reason_code'],'reserved_stock_unavailable');self.assertFalse(receipt['physical_applied']);self.assertEqual(before,self.quantities())

    def test_fbo_reservation_origin_is_retryable_and_exact_reserve_is_enforced(self):
        from packages.application.ff_wb_supply_origins import ensure_ff_wb_supply_origin_schema, ASSIGNMENTS_TABLE
        preview=self.preview('fbo-dependency','pool_reallocation',{'facility_id':'A','source_pool':'FBO','destination_pool':'FBS','items':[{'nm_id':1,'quantity':15}]})
        with sqlite3.connect(self.runtime.db_path) as conn:
            ensure_ff_wb_supply_origin_schema(conn)
            conn.execute("INSERT INTO sheet_vitrina_v1_ff_stock_reservation_operations(operation_id,source_key,supply_id,supply_revision,operation_type,created_at) VALUES('reserve-operation','reserve-source','WB123','fixture-revision','reserve',?)",(STAMP,))
            conn.execute("INSERT INTO sheet_vitrina_v1_ff_stock_reservation_lines(operation_id,line_no,nm_id,quantity_delta) VALUES('reserve-operation',1,1,10)")
        before=self.quantities();receipt=self.confirm(preview)
        self.assertTrue(receipt['durable_saved']);self.assertFalse(receipt['physical_applied'])
        self.assertEqual(receipt['reason_code'],'reservation_origin_unresolved');self.assertEqual(receipt['state'],'delayed')
        with sqlite3.connect(self.runtime.db_path) as conn:
            conn.execute(f"INSERT INTO {ASSIGNMENTS_TABLE}(assignment_id,request_id,request_fingerprint,wb_supply_cache_key,wb_supply_id,source_revision,feature_epoch,facility_id,pool,actor,assigned_at) VALUES('origin-fixture','request-fixture','fingerprint-fixture','cache-WB123','WB123','revision-fixture',1,'A','FBO','synthetic',?)",(STAMP,))
        self.drain();receipt=operations.read_acceptance(self.runtime.db_path,preview['request_id'])
        self.assertEqual(receipt['reason_code'],'reserved_stock_unavailable');self.assertEqual(before,self.quantities())

    def test_transfer_lifecycle_preserves_exact_parents_and_conservation(self):
        root=self.preview('transfer-root','transfer_root',{'source':{'facility_id':'A','pool':'FBS'},'destination':{'facility_id':'B','pool':'FBO'}})
        with patch.object(operations,'try_post'):receipt=self.confirm(root)
        self.assertIsNone(receipt['document'])
        dependent=self.preview('child-unapplied','transfer_shipment',{'root_document_id':root['request_id'],'items':[{'nm_id':1,'quantity':4}]})
        with self.assertRaises(FfPoolSurfaceError):self.confirm(dependent)
        self.drain();root_id=operations.read_acceptance(self.runtime.db_path,root['request_id'])['document']['document_id']
        shipment=self.confirm(self.preview('dispatch','transfer_shipment',{'root_document_id':root_id,'items':[{'nm_id':1,'quantity':8}]}))
        self.assertTrue(shipment['physical_applied'])
        for name,kind,q in [('partial','transfer_receipt',2),('loss','transfer_loss',2),('full','transfer_receipt',4)]:
            self.assertTrue(self.confirm(self.preview(name,kind,{'root_document_id':root_id,'items':[{'nm_id':1,'quantity':q}]}))['physical_applied'])
        state=self.service.open_transfer_projection(root_id);self.assertEqual(state['state'],'closed');self.assertTrue(state['quantity_conserved'])
        root2=self.confirm(self.preview('root-two','transfer_root',{'source':{'facility_id':'A','pool':'FBO'},'destination':{'facility_id':'B','pool':'FBO'}}))['document']['document_id']
        self.confirm(self.preview('dispatch-two','transfer_shipment',{'root_document_id':root2,'items':[{'nm_id':1,'quantity':2}]}))
        self.confirm(self.preview('cancel-remainder','transfer_cancellation',{'root_document_id':root2}))
        self.assertTrue(self.service.open_transfer_projection(root2)['quantity_conserved'])

    def test_transfer_discrepancy_preserves_confirmed_effect_and_parent(self):
        with sqlite3.connect(self.runtime.db_path) as conn:
            conn.execute(f"INSERT INTO {BALANCES_TABLE}(facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,wac_rub,source_watermark,updated_at) VALUES('A','FBS',2,1,10,'100','10','fixture',?)",(STAMP,))
        root=self.confirm(self.preview('discrepancy-root','transfer_root',{'source':{'facility_id':'A','pool':'FBS'},'destination':{'facility_id':'B','pool':'FBO'}}))['document']['document_id']
        self.confirm(self.preview('discrepancy-dispatch','transfer_shipment',{'root_document_id':root,'items':[{'nm_id':1,'quantity':4}]}))
        preview=self.preview('discrepancy-final','transfer_discrepancy',{'root_document_id':root,'expected_not_sent':[{'nm_id':1,'quantity':1}],'unexpected':[{'nm_id':2,'quantity':2}]})
        receipt=self.confirm(preview);self.assertTrue(receipt['physical_applied'],receipt)
        state=self.service.open_transfer_projection(root);self.assertTrue(state['quantity_conserved'])
        before=self.quantities();self.assertEqual(self.confirm(preview)['document'],receipt['document']);self.assertEqual(before,self.quantities())
        with operations.readonly(self.runtime.db_path) as conn:
            row=conn.execute(f"SELECT source_json FROM {operations.TABLE} WHERE request_id=?",(preview['request_id'],)).fetchone()
            self.assertEqual(json.loads(row['source_json'])['manifest']['root_document_id'],root)

    def next_epoch(self):
        with sqlite3.connect(self.runtime.db_path) as conn:
            conn.execute(f"INSERT INTO {FEATURE_EPOCHS_TABLE}(epoch,writer_enabled,reader_enabled,source_revision,created_at,metadata_json) VALUES(2,1,0,'next-epoch',?,'{{}}')",(STAMP,))

    def pending_credit_receipt(self, name):
        root=self.confirm(self.preview(name+'-root','transfer_root',{'source':{'facility_id':'A','pool':'FBS'},'destination':{'facility_id':'B','pool':'FBO'}}))['document']['document_id']
        self.confirm(self.preview(name+'-dispatch','transfer_shipment',{'root_document_id':root,'items':[{'nm_id':1,'quantity':8}]}))
        preview=self.preview(name,'transfer_receipt',{'root_document_id':root,'items':[{'nm_id':1,'quantity':5}]})
        with patch.object(operations,'try_post'):self.confirm(preview)
        return preview

    def assert_no_new_physical_effect(self, preview, balances, documents, queue_count):
        receipt=operations.read_acceptance(self.runtime.db_path,preview['request_id'])
        self.assertTrue(receipt['durable_saved']);self.assertFalse(receipt['physical_applied'])
        self.assertEqual(receipt['state'],'needs_attention');self.assertEqual(receipt['reason_code'],'feature_epoch_changed')
        self.assertEqual(self.quantities(),balances)
        with sqlite3.connect(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {DOCUMENTS_TABLE}').fetchone()[0],documents)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue').fetchone()[0],queue_count)

    def test_credit_receipt_cannot_migrate_accepted_epoch(self):
        preview=self.pending_credit_receipt('epoch-credit')
        balances=self.quantities()
        with sqlite3.connect(self.runtime.db_path) as conn:
            documents=conn.execute(f'SELECT COUNT(*) FROM {DOCUMENTS_TABLE}').fetchone()[0]
            queued=conn.execute('SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue').fetchone()[0]
        self.next_epoch();self.drain()
        self.assert_no_new_physical_effect(preview,balances,documents,queued)

    def test_epoch_race_after_native_plan_is_checked_inside_actual_transaction(self):
        from packages.application.warehouse_recovery_policy import WarehouseRecoveryRegistry
        preview=self.pending_credit_receipt('epoch-race')
        balances=self.quantities()
        with sqlite3.connect(self.runtime.db_path) as conn:
            documents=conn.execute(f'SELECT COUNT(*) FROM {DOCUMENTS_TABLE}').fetchone()[0]
            queued=conn.execute('SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue').fetchone()[0]
        original=WarehouseRecoveryRegistry.prepare_t1;crossed=[]
        def prepare_then_cross(owner, **kwargs):
            result=original(owner,**kwargs)
            self.next_epoch();crossed.append(True)
            return result
        with patch.object(WarehouseRecoveryRegistry,'prepare_t1',prepare_then_cross):self.drain()
        self.assertEqual(crossed,[True]) # after native plan/preimages, before actual BEGIN IMMEDIATE
        self.assert_no_new_physical_effect(preview,balances,documents,queued)

    def test_after_cutoff_source_waits_for_next_owner_capture(self):
        first=self.realloc('before-cutoff',1);second=self.realloc('after-cutoff',2)
        with patch.object(operations,'try_post'):self.confirm(first)
        actual=operations._post;created=[]
        def crossing(service,row):
            actual(service,row)
            if not created:
                operations.confirm_source(self.surface,second['request_id'],actor='final confirmer')
                created.append(second['request_id'])
        with patch.object(operations,'_post',side_effect=crossing):drained=self.drain()
        self.assertEqual(drained['request_ids'],[first['request_id']])
        pending=operations.read_acceptance(self.runtime.db_path,second['request_id'])
        self.assertFalse(pending['physical_applied']);self.assertEqual(pending['actor'],'final confirmer')
        self.assertIn(second['request_id'],self.drain()['request_ids'])
        self.assertTrue(operations.read_acceptance(self.runtime.db_path,second['request_id'])['physical_applied'])

    def test_dependency_can_resolve_next_cycle_without_resubmitting(self):
        from packages.application.ff_pool_documents import FfPoolDocumentError
        preview=self.realloc('dependency-retry')
        with patch.object(operations,'try_post'):self.confirm(preview)
        with patch.object(operations,'assert_reservations',side_effect=FfPoolDocumentError('reservation_origin_unresolved','synthetic dependency')):self.drain()
        receipt=operations.read_acceptance(self.runtime.db_path,preview['request_id'])
        self.assertEqual(receipt['state'],'delayed');self.assertFalse(receipt['physical_applied'])
        self.drain();self.assertTrue(operations.read_acceptance(self.runtime.db_path,preview['request_id'])['physical_applied'])

    def test_original_old_date_is_retained_and_closed_period_is_attention(self):
        self.prepare(opening=True)
        preview=self.preview('late-original','pool_reallocation',{'facility_id':'A','source_pool':'FBS','destination_pool':'FBO','items':[{'nm_id':1,'quantity':1}]},day='2026-08-01')
        self.confirm(preview);self.prepare()
        with warehouse_functional_job_lock(self.root):operations.reconcile(self.runtime,request_ids=[preview['request_id']],finance_receipt={},now=NOW)
        receipt=operations.read_acceptance(self.runtime.db_path,preview['request_id'])
        self.assertEqual(receipt['business_date'],'2026-08-01');self.assertEqual(receipt['state'],'needs_attention');self.assertEqual(receipt['reason_code'],'late_closed_period_document')

    def prepare(self, opening=False):
        with operations.readonly(self.runtime.db_path) as conn:docs=_documents(conn)
        physical=dict((pool,q) for facility,pool,nm,q,c in self.quantities() if facility=='A')
        image=capture(DAY,docs=docs,rows=[('A',1,physical['FBS'],'10')],fbo=[('A',1,physical['FBO'],'10')]);image['captured_at']=STAMP;image['quantity_snapshot']['captured_at']=STAMP
        image['source_digest']=_fingerprint({k:v for k,v in image.items() if k!='source_digest'})
        component=wb(DAY)
        with patch.object(accounting,'capture_current',return_value=image),patch.object(accounting,'capture_wb_component',return_value=component),patch.object(accounting,'capture_retained_stages',return_value=retained(component)):
            prepared=accounting.prepare(self.root,now=NOW,opening=opening)
        return save(self.runtime,make_plan('2026-09-07',DAY),prepared=prepared,now=NOW)

    def functional_publish(self):
        """Actual serialized publisher + source CAS + exact queue acknowledgement.

        Candidate uses synthetic official WB data and exact local FF balances.
        No external collector or queue-status mutation is substituted.
        """
        from packages.application.fulfillment_services import _ensure_schema
        from packages.application.canonical_cost_engine import ensure_canonical_cost_schema
        with sqlite3.connect(self.runtime.db_path) as conn:
            conn.row_factory=sqlite3.Row;_ensure_schema(conn);ensure_canonical_cost_schema(conn)
            columns=conn.execute('PRAGMA table_info(sheet_vitrina_v1_canonical_cost_baseline_versions)').fetchall()
            values={r[1]:(1 if r[2]=='INTEGER' else 'fixture') for r in columns}
            values.update(baseline_id='synthetic-baseline',cutover_date=DAY,primary_shipment_id='synthetic-primary',primary_accepted_ff_date='2026-09-07',primary_quantity='40',weighted_ff_unit_cost_rub='10',fingerprint='sha256:synthetic-baseline',report_json='{}',is_current=1,created_at=STAMP,superseded_at=None)
            conn.execute(f'INSERT INTO sheet_vitrina_v1_canonical_cost_baseline_versions({",".join(values)}) VALUES({",".join("?" for _ in values)})',list(values.values()))
        block=WarehouseFunctionalBlock(runtime=self.runtime,timestamp_factory=lambda:STAMP)
        with operations.readonly(self.runtime.db_path) as conn:
            queues=[_targeted_recalc_request_identity(dict(r)) for r in conn.execute("SELECT * FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue WHERE status IN ('queued','running')")]
        ff_quantity=sum(q for fac,pool,nm,q,c in self.quantities());ff_capital=sum(Decimal(c) for fac,pool,nm,q,c in self.quantities())
        lines=[WarehouseLine(warehouse_key=stage,nm_id=1,quantity=Decimal(ff_quantity if stage==STAGE_FF else 500 if stage==STAGE_WB else 0),capital=ff_capital if stage==STAGE_FF else Decimal(5000 if stage==STAGE_WB else 0),cost_covered_quantity=Decimal(ff_quantity if stage==STAGE_FF else 500 if stage==STAGE_WB else 0),quality='direct_24_06',provenance={'synthetic':True},certified=True,wb_quantity=Decimal(500 if stage==STAGE_WB else 0)) for stage in STAGES if stage!=STAGE_DISCREPANCY]
        plan=dict(contract_name='sheet_vitrina_v1_warehouse_functional',contract_version='v2',status='dry_run_ready',kind='functional_cutover',cutover_id=FUNCTIONAL_CUTOVER_ID,captured_at=STAMP,effective_date=DAY,base_active_version_id='',local_source_digest=block._local_source_digest(recovery_end_date=DAY,include_historical_correction=False),wb_supply_source_digest=block._wb_supply_source_digest(),source_watermarks={'synthetic':True},absorbed_supply_revisions={},
            wb_snapshot=dict(snapshot_id='fixture-wb',fetched_at=STAMP,snapshot_date=DAY,requested_nm_ids=[1],pagination_complete=True,page_count=1,page_offsets=[0],raw_row_count=1,raw_rows_digest='sha256:fixture',raw_rows=[dict(snapshot_date=DAY,snapshot_ts=STAMP,nmId=1,warehouseId=507,warehouseName='Синтетический WB',stockCount=500,inWayToClient=0,inWayFromClient=0)],items=[dict(nm_id=1,quantity='500',in_way_to_client='0',in_way_from_client='0',wb_contour_quantity='500')]),
            opening_cost_map=[dict(nm_id=1,ff_unit_cost_rub='10',wb_unit_cost_rub='10',quality='direct_24_06',provenance={'synthetic':True},fingerprint='sha256:fixture')],lines=[_line_payload(l) for l in lines],summaries=_summaries(lines),unmatched_doprinato=[],supplier_cost_states=[],new_events=[],movement_documents=[],targeted_recalc_requests=queues,
            historical_wb_cost_projection=[dict(as_of_date=d,nm_id=1,quantity='500',wac_rub='10',capital_rub='5000',quality='periodic_snapshot_wac_closed',provenance={'synthetic':True,'frozen_at_cutover':True},fingerprint='sha256:'+d) for d in ('2026-09-07',DAY)],diff={'changed_line_count':len(lines),'lines':[]},invariants={'warehouse_count':6,'negative_balance_count':0,'positive_cost_gap_count':0,'wb_quantity_source':'official_snapshot_only','discrepancy_opening_zero':True})
        plan['plan_fingerprint']=_fingerprint(plan)
        return block.apply_plan(plan,confirm_fingerprint=plan['plan_fingerprint'],backup_dir=self.root/'backups')

    def test_completion_requires_exact_real_book_ready_functional_and_finance(self):
        self.prepare(opening=True)
        preview=self.realloc('exact-publication');receipt=self.confirm(preview)
        self.assertTrue(receipt['physical_applied']);self.assertNotEqual(receipt['state'],'completed')
        with warehouse_functional_job_lock(self.root):
            # A generic complete Finance status cannot prove either native queue or book operands.
            self.assertEqual(operations.reconcile(self.runtime,request_ids=[preview['request_id']],finance_receipt={'status':'complete'},now=NOW)['processed_count'],0)
            self.functional_publish()
        receipt=operations.read_acceptance(self.runtime.db_path,preview['request_id']);self.assertIn('functional_publication',receipt['processing_receipt'])
        self.prepare()
        version=accounting.load(self.root)[1]
        finance=WbFinanceWeeklyBlock(self.root,now_factory=lambda:NOW).recalculate_stale_cost_weeks()
        finance.update(accounting_version=version,accounting_version_before=version,accounting_version_unchanged=True)
        with warehouse_functional_job_lock(self.root):
            self.assertEqual(operations.reconcile(self.runtime,request_ids=[preview['request_id']],finance_receipt={**finance,'accounting_version_before':'foreign'},now=NOW)['processed_count'],0)
            missing=dict(finance);missing.pop('post_verify_stale_week_count')
            self.assertEqual(operations.reconcile(self.runtime,request_ids=[preview['request_id']],finance_receipt=missing,economics_receipt=accounting.current_publication_receipt(self.runtime,now=NOW),now=NOW)['processed_count'],0)
            complete=operations.reconcile(self.runtime,request_ids=[preview['request_id']],finance_receipt=finance,economics_receipt=accounting.current_publication_receipt(self.runtime,now=NOW),now=NOW)
        self.assertEqual(complete['processed_count'],1,(finance,operations.read_acceptance(self.runtime.db_path,preview['request_id'])))
        final=operations.read_acceptance(self.runtime.db_path,preview['request_id']);self.assertEqual(final['state'],'completed');self.assertTrue(final['processing_receipt']['publication_verified'])
        self.assertEqual(final['processing_receipt']['accounting_publication']['accounting_version'],version)

class GuidedAcceptance(unittest.TestCase):
    def test_query_only_source_and_unrelated_catalog_edit_do_not_invalidate_receipt(self):
        with TemporaryDirectory(prefix='w1-guided-catalog-') as directory:
            runtime,surface=guided._fixture(Path(directory))
            before=runtime.db_path.read_bytes()
            with patch('packages.application.registry_upload_db_backed_runtime.RegistryUploadDbBackedRuntime.__init__',side_effect=AssertionError('GET bootstrap')):
                surface.china_acceptance_form(guided.SHIPMENT)
            self.assertEqual(before,runtime.db_path.read_bytes())
            preview=surface.accept_china_form(guided._payload(surface),actor='preview author')
            with patch.object(operations,'try_post'):surface.confirm_document(preview['request_id'],actor='actual confirmer')
            runtime.save_nomenclature_item({'item_id':'unrelated','is_active':True,'our_sku':'Foreign','nm_id':999,'barcode':'foreign-barcode','nomenclature_name':'Unrelated','created_at':guided.NOW,'updated_at':guided.NOW})
            with warehouse_functional_job_lock(runtime.runtime_dir):operations.drain(runtime,timestamp_factory=lambda:guided.NOW)
            receipt=operations.read_acceptance(runtime.db_path,preview['request_id'])
            self.assertTrue(receipt['physical_applied'],receipt);self.assertEqual(receipt['actor'],'actual confirmer')


    def test_bad_live_derived_parity_active_cycle_and_crash_after_native_post(self):
        with TemporaryDirectory(prefix='w1-guided-parity-') as directory:
            runtime,surface=guided._fixture(Path(directory));payload=guided._payload(surface)
            with sqlite3.connect(runtime.db_path) as conn:
                original_quantity=conn.execute("SELECT quantity FROM sheet_vitrina_v1_warehouse_functional_balances WHERE warehouse_key='ff' AND nm_id=?",(guided.NM_ID,)).fetchone()[0]
                conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_balances SET quantity='999' WHERE warehouse_key='ff' AND nm_id=?",(guided.NM_ID,))
            preview=surface.accept_china_form(payload,actor='synthetic operator')
            self.assertTrue(preview['confirm_allowed'],preview)
            with warehouse_functional_job_lock(runtime.runtime_dir):receipt=surface.confirm_document(preview['request_id'])['acceptance']
            self.assertTrue(receipt['durable_saved']);self.assertFalse(receipt['physical_applied']);self.assertNotEqual(receipt['state'],'completed')
            with sqlite3.connect(runtime.db_path) as conn:
                self.assertIsNone(conn.execute('SELECT actual_ff_acceptance_date FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?',(guided.SHIPMENT,)).fetchone()[0])
            self.assertEqual(surface.confirm_document(payload['request_id'])['acceptance'],receipt)
            with warehouse_functional_job_lock(runtime.runtime_dir):operations.drain(runtime,timestamp_factory=lambda:guided.NOW)
            deferred=operations.read_acceptance(runtime.db_path,preview['request_id'])
            self.assertFalse(deferred['physical_applied']);self.assertEqual(deferred['state'],'delayed')
            self.assertIn(deferred['reason_code'],{'guided_acceptance_parity_failed','guided_acceptance_parity_not_current'})
            with sqlite3.connect(runtime.db_path) as conn:
                conn.execute("UPDATE sheet_vitrina_v1_warehouse_functional_balances SET quantity=? WHERE warehouse_key='ff' AND nm_id=?",(original_quantity,guided.NM_ID))
            with warehouse_functional_job_lock(runtime.runtime_dir):operations.drain(runtime,timestamp_factory=lambda:guided.NOW)
            self.assertTrue(operations.read_acceptance(runtime.db_path,preview['request_id'])['physical_applied'])


    def test_authorized_supplier_price_cannot_change_while_pending(self):
        with TemporaryDirectory(prefix='w1-guided-price-drift-') as directory:
            runtime,surface=guided._fixture(Path(directory));preview=surface.accept_china_form(guided._payload(surface),actor='synthetic operator')
            with patch.object(operations,'try_post'):surface.confirm_document(preview['request_id'])
            with sqlite3.connect(runtime.db_path) as conn:
                conn.execute("UPDATE sheet_vitrina_v1_supplier_shipment_lines SET unit_price=unit_price+1 WHERE shipment_id=?",(guided.SHIPMENT,))
            with warehouse_functional_job_lock(runtime.runtime_dir):operations.drain(runtime,timestamp_factory=lambda:guided.NOW)
            receipt=operations.read_acceptance(runtime.db_path,preview['request_id'])
            self.assertEqual(receipt['reason_code'],'supplier_source_revision_changed');self.assertEqual(receipt['state'],'needs_attention')
            self.assertFalse(receipt['physical_applied']);self.assertTrue(receipt['durable_saved'])

    def test_guided_source_cannot_migrate_accepted_epoch(self):
        with TemporaryDirectory(prefix='w1-guided-epoch-') as directory:
            runtime,surface=guided._fixture(Path(directory));preview=surface.accept_china_form(guided._payload(surface),actor='synthetic operator')
            with patch.object(operations,'try_post'):surface.confirm_document(preview['request_id'])
            with sqlite3.connect(runtime.db_path) as conn:
                balances=conn.execute(f'SELECT * FROM {BALANCES_TABLE} ORDER BY facility_id,pool,nm_id').fetchall()
                documents=conn.execute(f'SELECT COUNT(*) FROM {DOCUMENTS_TABLE}').fetchone()[0]
                conn.execute(f"INSERT INTO {FEATURE_EPOCHS_TABLE}(epoch,writer_enabled,reader_enabled,source_revision,created_at,metadata_json) VALUES(2,1,0,'next-epoch',?,'{{}}')",(guided.NOW,))
            with warehouse_functional_job_lock(runtime.runtime_dir):operations.drain(runtime,timestamp_factory=lambda:guided.NOW)
            receipt=operations.read_acceptance(runtime.db_path,preview['request_id'])
            self.assertEqual(receipt['reason_code'],'feature_epoch_changed');self.assertEqual(receipt['state'],'needs_attention');self.assertFalse(receipt['physical_applied'])
            with sqlite3.connect(runtime.db_path) as conn:
                self.assertEqual(balances,conn.execute(f'SELECT * FROM {BALANCES_TABLE} ORDER BY facility_id,pool,nm_id').fetchall())
                self.assertEqual(documents,conn.execute(f'SELECT COUNT(*) FROM {DOCUMENTS_TABLE}').fetchone()[0])
                self.assertIsNone(conn.execute('SELECT actual_ff_acceptance_date FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?',(guided.SHIPMENT,)).fetchone()[0])

    def test_native_commit_then_crash_keeps_one_document_and_continues(self):
        with TemporaryDirectory(prefix='w1-guided-crash-') as directory:
            runtime,surface=guided._fixture(Path(directory));preview=surface.accept_china_form(guided._payload(surface),actor='synthetic operator')
            actual=operations._post
            def crash(service,row):actual(service,row);raise TimeoutError('lost after native commit')
            with patch.object(operations,'_post',side_effect=crash):receipt=surface.confirm_document(preview['request_id'])['acceptance']
            self.assertTrue(receipt['durable_saved']);self.assertTrue(receipt['physical_applied']);self.assertNotEqual(receipt['state'],'completed')
            with warehouse_functional_job_lock(runtime.runtime_dir):operations.drain(runtime,timestamp_factory=lambda:guided.NOW)
            with sqlite3.connect(runtime.db_path) as conn:self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {DOCUMENTS_TABLE} WHERE request_id=?',(preview['request_id'],)).fetchone()[0],1)

if __name__=='__main__':unittest.main()
