#!/usr/bin/env python3
"""Typed durable pre-effect recipe and exact fee-source readback, LOCAL only."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import json
import hashlib
from unittest.mock import patch
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps import fbs_historical_fbo_native_smoke as native_tests
from apps.fbs_historical_fbo_dispatch_smoke import seal
from packages.application import fbs_historical_fbo_publication as publication
from packages.application import fbs_accounting_runtime as accounting
from packages.application import ready_publication as ready
from packages.application import operator_fulfillment_services as receipts
from packages.application import fulfillment_recalc_intents as intents
from packages.application.fulfillment_services import _ensure_schema as ensure_fee_schema
from packages.application.fbs_historical_fbo_dispatch import prepare_historical_fbo_dispatch_plan
from packages.application.fbs_historical_fbo_native import capture_native_guard
from packages.application.registry_upload_db_backed_runtime import _connect
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock


class TypedFboPublicationTests(unittest.TestCase):
    snapshot=native_tests.NativeHistoricalFboTests.snapshot
    def setUp(self):
        native_tests.NativeHistoricalFboTests.setUp(self)
        destination=self.runtime.runtime_dir/'registry_upload_runtime.sqlite3'
        self.runtime.db_path.rename(destination);self.runtime.db_path=destination
        self.book.update(shared_days={},wb_days={},retained_days={},presentations={},state=dict(baseline={},periods={},observed_documents={}))
        version=accounting._save_book(self.runtime.runtime_dir,self.book,expected=None,operation_id='local-initial')
        from packages.application import fbs_historical_fbo_native as native
        native.load.return_value=(self.book,version)
        self.capture['book_version']=version;seal(self.capture)
        self.plan=prepare_historical_fbo_dispatch_plan(self.capture,self.scope)
        self.fees=[dict(request_scope='synthetic-fbo',request_id='synthetic-fee-'+str(i),file_sha256=str(i)*64,
            wb_supply_id=supply,amount_with_vat_rub=amount) for i,(supply,amount) in enumerate(zip(('101','102','103'),('81885.05','41430.50','110806.50')),1)]
        with closing(_connect(self.runtime.db_path)) as conn:
            ready.ensure_publication_schema(conn);ensure_fee_schema(conn);receipts.ensure_schema(conn);intents.ensure_schema(conn)
            conn.execute('INSERT INTO registry_upload_versions VALUES(?,?,?)',('local-bundle',native_tests.STAMP,native_tests.STAMP))
            conn.execute(f"INSERT INTO {ready.TABLE}(bundle_version,as_of_date,plan_json,activated_at,snapshot_id,plan_version,refreshed_at) VALUES(?,?,?,?,?,?,?)",
                ('local-bundle','2026-11-02','{}',native_tests.STAMP,'synthetic','synthetic',native_tests.STAMP))
            conn.commit();conn.execute('BEGIN');self.guard=capture_native_guard(conn,self.plan)
        self.recipe=publication.prepare_effect_intent(self.plan,capture=self.capture,scope=self.scope,expected_native_guard=self.guard,
            fee_requests=self.fees,runtime_dir=self.runtime.runtime_dir,owner_generation='local-owner',operation_id='local-fbo-effects',
            ready_target=dict(bundle_version='local-bundle',as_of_date='2026-11-02'),now=datetime.fromisoformat('2026-11-02T15:00:00+00:00'))

    def record(self):
        with warehouse_functional_job_lock(self.runtime.runtime_dir) as owner:
            return publication.record_effect_intent(self.recipe,runtime=self.runtime,recapture=lambda conn:deepcopy(self.capture),owner_token=owner['owner_token'])

    def seed_fees(self):
        with closing(_connect(self.runtime.db_path)) as conn:
            for index,fee in enumerate(self.fees):
                upload='synthetic-upload-'+str(index)
                conn.execute(f'INSERT INTO {intents.UPLOADS}(upload_id,original_filename,stored_file_path,file_sha256,uploaded_at,validation_status,rows_total,rows_matched,amount_without_vat_total,vat_total,amount_with_vat_total,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (upload,'synthetic.xlsx','local-unused',fee['file_sha256'],native_tests.STAMP,'ok',1,1,float(fee['amount_with_vat_rub']),0,float(fee['amount_with_vat_rub']),native_tests.STAMP,native_tests.STAMP))
                conn.execute(f'INSERT INTO {intents.LINES}(upload_id,row_index,supply_id_input,matched_wb_supply_id,matched_wb_cache_key,match_status,amount_with_vat,is_storage_line,raw_row_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (upload,1,fee['wb_supply_id'],fee['wb_supply_id'],'supply:'+fee['wb_supply_id'],'ok',float(fee['amount_with_vat_rub']),0,'{}',native_tests.STAMP))
                receipts.record_source(conn,upload,action='upload',actor='local-test')
                receipts.bind_request(conn,request_id=fee['request_id'],request_scope=fee['request_scope'],action='upload',payload_digest=fee['file_sha256'],upload_id=upload)
                conn.execute(f'INSERT INTO {intents.TABLE} VALUES(?,?,?,?,?,?,?)',(upload,fee['file_sha256'],json.dumps({'supply:'+fee['wb_supply_id']:dict(supply_id=fee['wb_supply_id'],nm_ids=[1],effective_date='2026-10-19')}),'pending',native_tests.STAMP,None,None))
            conn.commit()

    def test_real_pending_intent_precedes_effects_and_same_identity_never_resubmits(self):
        before=self.snapshot();result=self.record();self.assertEqual(before,self.snapshot())
        self.assertFalse(result['idempotent']);self.assertEqual(result['status'],'pending')
        self.assertTrue(self.record()['idempotent'])
        with ready.readonly(self.runtime.db_path) as conn:
            self.assertEqual(publication.read_effect_intent(conn,binding=result['binding']),self.recipe)
            rows=list(conn.execute('SELECT kind,state,book_required,ready_required FROM sheet_vitrina_v1_ready_publications'))
            self.assertEqual([tuple(row) for row in rows],[(publication.EFFECT_CONTRACT,'prepared',1,1)])

    def apply_native(self,binding):
        with warehouse_functional_job_lock(self.runtime.runtime_dir) as owner:
            return publication.apply_native_from_effect_intent(binding,runtime=self.runtime,
                recapture=lambda conn:deepcopy(self.capture),owner_token=owner['owner_token'])

    def live_workbooks(self):
        from decimal import Decimal
        from apps.sheet_vitrina_v1_fulfillment_services_smoke import _build_workbook,_wb_supply_row
        from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
        self.runtime=RegistryUploadDbBackedRuntime(runtime_dir=self.runtime.runtime_dir,operational_db_path=self.runtime.db_path)
        rows=[];workbooks={}
        for fee in self.fees:
            quantity=next(d['expected_quantity'] for d in self.scope['dispatches'] if d['supply_id']==fee['wb_supply_id'])
            row=_wb_supply_row(fee['wb_supply_id'],accepted_quantity=quantity,quantity_added=quantity,cost_total=0)
            row.update(supply_date='2026-10-23',fact_date='2026-10-23',updated_date='2026-10-23T08:00:00Z')
            if getattr(self,'force_pending_components',False):
                row.update(cost_total=None,acceptance_cost=-1,acceptance_cost_status='unavailable')
                acceptance=getattr(self,'acceptance_dates',{}).get(fee['wb_supply_id'],'2026-10-23')
                row.update(supply_date=acceptance,fact_date=acceptance,updated_date=acceptance+'T08:00:00Z')
            rows.append(row)
            total=Decimal(fee['amount_with_vat_rub']);base=(total/Decimal('1.05')).quantize(Decimal('.01'));vat=total-base
            data=_build_workbook([[fee['wb_supply_id'],'Synthetic',None,None,None,None,None,None,float(base),float(vat)]])
            fee['file_sha256']=hashlib.sha256(data).hexdigest();workbooks[(fee['request_scope'],fee['request_id'])]=data
        self.runtime.save_wb_supply_rows(rows=rows,warehouses=[dict(ID=777,name='Synthetic')],synced_at=native_tests.STAMP)
        with ready.readonly(self.runtime.db_path) as conn:self.guard=capture_native_guard(conn,self.plan)
        self.recipe=publication.prepare_effect_intent(self.plan,capture=self.capture,scope=self.scope,expected_native_guard=self.guard,
            fee_requests=self.fees,runtime_dir=self.runtime.runtime_dir,owner_generation='local-owner',operation_id='local-fbo-effects',
            ready_target=dict(bundle_version='local-bundle',as_of_date='2026-11-02'),now=datetime.fromisoformat('2026-11-02T15:00:00+00:00'))
        return workbooks

    def test_real_native_and_three_uploads_recover_without_resubmission(self):
        workbooks=self.live_workbooks();binding=self.record()['binding']
        self.assertFalse(self.apply_native(binding)['idempotent']);self.assertTrue(self.apply_native(binding)['idempotent'])
        from packages.application.fulfillment_services import FulfillmentServicesBlock
        original=FulfillmentServicesBlock.upload_xlsx;calls=[]
        def lose_first_response(service,*args,**kwargs):
            result=original(service,*args,**kwargs);calls.append(kwargs['request_id'])
            if len(calls)==1:raise OSError('synthetic lost response after commit')
            return result
        with patch.object(FulfillmentServicesBlock,'upload_xlsx',lose_first_response):
            with warehouse_functional_job_lock(self.runtime.runtime_dir) as owner:
                result=publication.apply_fees_from_effect_intent(binding,runtime=self.runtime,workbooks=workbooks,owner_token=owner['owner_token'])
        self.assertEqual(len(calls),3);self.assertFalse(result['publication_complete'])
        with patch.object(FulfillmentServicesBlock,'upload_xlsx',side_effect=AssertionError('repeat upload forbidden')):
            with warehouse_functional_job_lock(self.runtime.runtime_dir) as owner:
                repeated=publication.apply_fees_from_effect_intent(binding,runtime=self.runtime,workbooks=workbooks,owner_token=owner['owner_token'])
        self.assertEqual(result['fee_scope'],repeated['fee_scope'])

    def test_unknown_upload_response_persists_no_resend_guard(self):
        workbooks=self.live_workbooks();binding=self.record()['binding'];self.apply_native(binding)
        from packages.application.fulfillment_services import FulfillmentServicesBlock
        for _ in range(2):
            with patch.object(FulfillmentServicesBlock,'upload_xlsx',side_effect=OSError('synthetic unknown response')) as upload:
                with warehouse_functional_job_lock(self.runtime.runtime_dir) as owner:
                    with self.assertRaisesRegex(ValueError,'uncertain_no_resend'):
                        publication.apply_fees_from_effect_intent(binding,runtime=self.runtime,workbooks=workbooks,owner_token=owner['owner_token'])
                self.assertEqual(upload.call_count,1 if _==0 else 0)

    def test_selected_cost_materialization_uses_native_frozen_money_and_visible_fees(self):
        self.force_pending_components=True
        workbooks=self.live_workbooks();binding=self.record()['binding'];self.apply_native(binding)
        from packages.application.our_wb_costs import OurWbCostBlock
        with warehouse_functional_job_lock(self.runtime.runtime_dir) as owner:
            accepted=publication.apply_fees_from_effect_intent(binding,runtime=self.runtime,workbooks=workbooks,owner_token=owner['owner_token'])
            block=OurWbCostBlock(runtime=self.runtime,timestamp_factory=lambda:native_tests.STAMP)
            prepared=publication.prepare_cost_layer_materialization(binding,runtime=self.runtime,now=native_tests.STAMP)
            with patch.object(OurWbCostBlock,'_load_current_supplier_ff_cost_lines_by_nm',side_effect=AssertionError('current cost forbidden')):
                self.assertEqual(block.materialize_wb_supply_cost_layers(historical_fbo_effect_binding=binding,historical_fbo_materialization_plan=prepared,supply_ids=['101','102','103']),3)
                self.assertEqual(block.materialize_wb_supply_cost_layers(historical_fbo_effect_binding=binding,historical_fbo_materialization_plan=prepared),0)
                self.assertEqual(block.materialize_wb_supply_cost_layers(historical_fbo_effect_binding=binding),0)
                self.assertEqual(block.materialize_wb_supply_cost_layers(),0)
        with ready.readonly(self.runtime.db_path) as conn:
            rows=[dict(r) for r in conn.execute('SELECT * FROM sheet_vitrina_v1_wb_supply_cost_layers WHERE is_current=1 ORDER BY wb_supply_id')]
            from packages.application import warehouse_functional as wf
            ordinary=wf._supply_downstream_component_index([dict(row) for row in conn.execute(wf._DOWNSTREAM_COST_ROWS_SQL)])
        self.assertEqual(set(ordinary),{('101',1),('102',1),('103',1)})
        for fee in self.fees:
            component=ordinary[(fee['wb_supply_id'],1)]
            quantity=next(d['expected_quantity'] for d in self.scope['dispatches'] if d['supply_id']==fee['wb_supply_id'])
            self.assertEqual(component['pre_acceptance_addon']*quantity,Decimal(fee['amount_with_vat_rub']))
            self.assertEqual(component['missing_cost_components'],['wb_transit','wb_paid_acceptance'])
        self.assertEqual(len(rows),3)
        for row,fee in zip(rows,self.fees):
            self.assertEqual(Decimal(str(row['ff_services_amount_total'])),Decimal(fee['amount_with_vat_rub']))
            self.assertEqual(row['ff_storage_amount_total'],0)
            self.assertEqual(row['ff_upload_id'],next(u['upload_id'] for u in accepted['fee_scope']['uploads'] if u['wb_supply_id']==row['wb_supply_id']))
            proof=json.loads(row['component_status_json'])['historical_fbo_native_source']
            original=next(e['aggregate_movements'][0] for e in self.plan['effects'] if e.get('source_key','').endswith(':'+row['wb_supply_id']))
            self.assertEqual(proof['unit_cost_rub'],original['wac_rub'])
            self.assertEqual(proof['capital_delta_rub'],original['capital_delta_rub'])
            self.assertIn(row['source_status'],{'pending','needs_review','estimated'})
        self.assertEqual(rows[0]['sku_ff_unit_cost_rub'],50)

    def test_ordinary_fee_change_retains_frozen_primary_without_current_fallback(self):
        workbooks=self.live_workbooks();binding=self.record()['binding'];self.apply_native(binding)
        from packages.application.our_wb_costs import OurWbCostBlock
        from packages.application.fulfillment_services import FulfillmentServicesBlock
        from apps.sheet_vitrina_v1_fulfillment_services_smoke import _build_workbook
        with warehouse_functional_job_lock(self.runtime.runtime_dir) as owner:
            publication.apply_fees_from_effect_intent(binding,runtime=self.runtime,workbooks=workbooks,owner_token=owner['owner_token'])
            block=OurWbCostBlock(runtime=self.runtime,timestamp_factory=lambda:native_tests.STAMP)
            block.materialize_wb_supply_cost_layers(historical_fbo_effect_binding=binding)
            FulfillmentServicesBlock(runtime=self.runtime).upload_xlsx(_build_workbook([['101','Synthetic',None,None,None,None,None,None,10,0]]),
                uploaded_filename='additional-legitimate-fee.xlsx',request_id='additional-legitimate-fee',request_scope='local-test',actor='test')
            with patch.object(OurWbCostBlock,'_load_current_supplier_ff_cost_lines_by_nm',side_effect=AssertionError('current fallback forbidden')):
                self.assertEqual(block.materialize_wb_supply_cost_layers(supply_ids=['101']),1)
        with ready.readonly(self.runtime.db_path) as conn:
            rows=[dict(row) for row in conn.execute('SELECT * FROM sheet_vitrina_v1_wb_supply_cost_layers WHERE is_current=1 ORDER BY wb_supply_id')]
        self.assertEqual(rows[0]['sku_ff_unit_cost_rub'],50)
        self.assertEqual(Decimal(str(rows[0]['ff_services_amount_total'])),Decimal('81895.05'))
        self.assertEqual(json.loads(rows[0]['component_status_json'])['historical_fbo_native_source']['unit_cost_rub'],'50')
        self.assertEqual([row['version'] for row in rows],[2,1,1])

    def test_native_phase_rechecks_pending_recipe_inside_write_transaction(self):
        binding=self.record()['binding'];before=self.snapshot()
        original=publication.read_effect_intent;calls=[]
        def fail_inside(conn,**kwargs):
            calls.append(bool(conn.execute('PRAGMA query_only').fetchone()[0]))
            if calls[-1] is False:raise ValueError('synthetic recipe vanished inside native CAS')
            return original(conn,**kwargs)
        with patch.object(publication,'read_effect_intent',fail_inside):
            with self.assertRaisesRegex(ValueError,'vanished inside native CAS'):self.apply_native(binding)
        self.assertEqual(calls,[True,False]);self.assertEqual(before,self.snapshot())

    def test_recipe_corruption_and_source_drift_cannot_record(self):
        original=deepcopy(self.recipe);self.recipe['fee_requests'][0]['amount_with_vat_rub']='1'
        with self.assertRaisesRegex(ValueError,'recipe_corrupt'):self.record()
        self.recipe=original
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_ff_pool_balances SET source_watermark='drift'");conn.commit()
        with self.assertRaisesRegex(ValueError,'native_guard_changed'):self.record()
        with ready.readonly(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM sheet_vitrina_v1_ready_publications').fetchone()[0],0)

    def test_exact_accepted_fee_requests_resolve_to_sources_and_durable_recalc(self):
        self.record();self.seed_fees()
        with ready.readonly(self.runtime.db_path) as conn:
            scope=publication.capture_fee_scope(conn,native_plan=self.plan,fee_requests=self.fees)
            self.assertEqual(scope['total_with_vat_rub'],'234122.05');self.assertEqual(len(scope['uploads']),3)
            self.assertEqual({r['wb_supply_id'] for r in scope['uploads']},{'101','102','103'})
            self.assertTrue(scope['query_fence'])

    def test_missing_fee_request_never_becomes_zero_or_success(self):
        self.record()
        with ready.readonly(self.runtime.db_path) as conn:
            with self.assertRaisesRegex(ValueError,'not_accepted'):
                publication.capture_fee_scope(conn,native_plan=self.plan,fee_requests=self.fees)

    def test_fee_amount_or_target_drift_is_rejected(self):
        self.record();self.seed_fees()
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute(f"UPDATE {intents.LINES} SET matched_wb_supply_id='foreign' WHERE upload_id='synthetic-upload-0'");conn.commit()
        with ready.readonly(self.runtime.db_path) as conn:
            with self.assertRaisesRegex(ValueError,'exact_supply_lines_required'):
                publication.capture_fee_scope(conn,native_plan=self.plan,fee_requests=self.fees)


if __name__=='__main__':unittest.main()
