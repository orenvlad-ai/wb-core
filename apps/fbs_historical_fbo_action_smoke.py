#!/usr/bin/env python3
"""Registered launcher and real bounded private preflight, disposable sources."""
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime,timezone
import hashlib
import json
import shutil
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps import fbs_historical_fbo_ready_smoke as ready_suite
from apps.production_apply_launcher import execute,ApplyError
from packages.application import fbs_historical_fbo_action as action
from packages.application import fbs_accounting_apply as adapter_module
from packages.application import fbs_historical_fbo_publication as typed
from packages.application import fbs_accounting_runtime as accounting
from packages.application.fbs_snapshot_cost import fingerprint,canonical
from packages.application.registry_upload_db_backed_runtime import _connect


class BeforeEffects(Exception):pass


class ActionTests(unittest.TestCase):
    # Reuse the fully real fixture but stop before its first effect recipe.
    force_pending_components=ready_suite.FullTypedReadyTests.force_pending_components
    acceptance_dates=ready_suite.FullTypedReadyTests.acceptance_dates
    seed_fee=ready_suite.FullTypedReadyTests.seed_fee
    seed_version=ready_suite.FullTypedReadyTests.seed_version
    snapshot=ready_suite.FullTypedReadyTests.snapshot
    live_workbooks=ready_suite.FullTypedReadyTests.live_workbooks
    apply=ready_suite.FullTypedReadyTests.apply
    apply_native=ready_suite.FullTypedReadyTests.apply_native
    def record(self):raise BeforeEffects()

    def setUp(self):
        try:ready_suite.FullTypedReadyTests.setUp(self)
        except BeforeEffects:pass
        self.root=self.runtime.runtime_dir
        self.now=datetime.now(timezone.utc)
        # Sources are dated2Nov; keep the fixture's observation clock stable.
        self.now=datetime.fromisoformat('2026-11-02T15:00:00+00:00')
        self.inputs_dir=self.root/'reviewed';self.inputs_dir.mkdir()
        evidence=self.inputs_dir/'physical-owner.txt';evidence.write_text('synthetic primary physical movement and separate owner allocation')
        files=[dict(path=str(evidence.resolve()),sha256=hashlib.sha256(evidence.read_bytes()).hexdigest())]
        proof=fingerprint(files)
        physical={**self.capture['reallocation_physical_source'],'source_digest':proof}
        restored={**self.capture['restored_pool_allocation'],'source_digest':proof}
        fees=[]
        for fee in self.fees:
            file=self.inputs_dir/(fee['wb_supply_id']+'.xlsx')
            data=self.workbooks[(fee['request_scope'],fee['request_id'])];file.write_bytes(data)
            invoice=self.inputs_dir/(fee['wb_supply_id']+'.pdf');invoice.write_bytes(b'%PDF-1.4\nsynthetic invoice '+fee['wb_supply_id'].encode())
            payment=b'%PDF-1.4\nsynthetic payment '+fee['wb_supply_id'].encode()
            payment_id='payment-'+fee['wb_supply_id'];payment_sha='sha256:'+hashlib.sha256(payment).hexdigest()
            with closing(_connect(self.runtime.db_path)) as conn:
                conn.execute(f'''INSERT INTO {action.REQUESTS_TABLE}(request_id,request_identity,client_request_id,document_kind,state,source_system,source_type,source_id,source_revision,idempotency_epoch,actor,business_date,source_filename,source_content_type,source_sha256,source_file_blob,request_payload_json,accepted_at,updated_at,error_code)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (payment_id,payment_id,payment_id,'pool_overhead','blocked','synthetic','payment_order_pdf',payment_id,payment_sha,1,'test','2026-11-01','payment.pdf','application/pdf',payment_sha,payment,'{"comment":"actual invoice amount is independent of payment"}',self.now.isoformat(),self.now.isoformat(),'overhead_confirmation_cancelled'));conn.commit()
            fees.append({**fee,'path':str(file),'original_invoice':dict(path=str(invoice),sha256=hashlib.sha256(invoice.read_bytes()).hexdigest(),invoice_no=fee['wb_supply_id'],payment_request_id=payment_id,payment_source_sha256=payment_sha)})
        manifest=dict(scope=deepcopy(self.scope),opening_date='2026-10-15',physical=physical,restored=restored,evidence_files=files,fees=fees)
        self.evidence=self.inputs_dir/'manifest.json';self.evidence.write_text(canonical(manifest))
        self.request=dict(action=action.RESTORE,evidence_manifest_path=str(self.evidence),
            evidence_manifest_sha256=hashlib.sha256(self.evidence.read_bytes()).hexdigest(),window_id='synthetic-window',maintenance_env_file='unused')
        self.adapter=adapter_module.FbsAccountingAdapter()
        self.target_patch=patch.object(adapter_module,'target',return_value=self.root);self.target_patch.start();self.addCleanup(self.target_patch.stop)
        self.held_patch=patch.object(action,'_held',side_effect=self.held);self.held_patch.start();self.addCleanup(self.held_patch.stop)
        self.clock=patch.object(action,'datetime');clock=self.clock.start();self.addCleanup(self.clock.stop)
        clock.now.return_value=self.now;clock.fromisoformat.side_effect=datetime.fromisoformat

    @contextmanager
    def held(self,*args):yield dict(window_id='synthetic-window',baseline_fingerprint=fingerprint('synthetic-held'))

    def launch(self,mode='preview',op='restore-fixture',request=None,preview=None):
        request=request or self.request
        return execute(action=mode,adapter_name='fbs_snapshot_accounting_v1',operation_id=op,request=request,
            expected_prestate=preview['prestate_sha256'] if preview else '',expected_candidate=preview['candidate_sha256'] if preview else '',
            adapters={'fbs_snapshot_accounting_v1':self.adapter})

    def test_selected_four_hour_sources_and_previous_container_keep_full_preview(self):
        from apps.fbs_current_snapshot_policy_smoke import canonical as bind,select
        from packages.application import business_data_schedule_profile as schedule
        with closing(_connect(self.runtime.db_path)) as conn:
            bind(self.root.resolve(),self.runtime.db_path,conn,monolith=True)
            conn.execute(f"UPDATE {action.ready.TABLE} SET as_of_date='2026-11-01' WHERE as_of_date='2026-11-02'")
            conn.commit()
        select(self.root)
        action.datetime.now.return_value=datetime.fromisoformat('2026-11-02T18:50:00+00:00')
        before=self.snapshot();p=self.launch()
        self.assertEqual(p['state'],'preview');self.assertEqual(before,self.snapshot())
        cached=action._read_json(action._directory(self.root,'restore-fixture')/'preflight.json')
        self.assertTrue(cached['snapshot']['freshness_policy']['selected'])
        self.assertEqual(cached['snapshot']['ready_target']['as_of_date'],'2026-11-01')
        self.assertIn('fbs_current_snapshot_policy.py',cached['code_authority'])
        (self.root/schedule.SELECTOR_FILENAME).unlink()
        with self.assertRaisesRegex(ValueError,'preflight_sources_changed|fbs_current_snapshot_policy_unavailable'):
            self.launch('apply',preview=p)
        self.assertEqual(before,self.snapshot())

    def test_private_copy_cap_does_not_expand_other_limits(self):
        self.assertEqual(action.MAX_BYTES,512*1024**2)
        action._check_source_copy_budget(table='fixture',count=1,size=640*1024**2,elapsed=1)
        for reason,kwargs in (
            ('bytes',dict(count=1,size=action.MAX_SOURCE_COPY_BYTES+1,elapsed=1)),
            ('rows',dict(count=action.MAX_ROWS+1,size=1,elapsed=1)),
            ('time',dict(count=1,size=1,elapsed=120))):
            with self.assertRaisesRegex(ValueError,'subset_'+reason+'_budget'):
                action._check_source_copy_budget(table='fixture',**kwargs)

    def test_unknown_action_rejected_before_target_and_money(self):
        with patch.object(adapter_module,'target',side_effect=AssertionError('target not allowed')):
            for method in ('preview','readback'):
                with self.assertRaisesRegex(ValueError,'unknown_action'):
                    getattr(self.adapter,method)({'action':'unknown'},'unknown-test')

    def test_live_capture_rejects_pasted_price_and_source_drift(self):
        with self.assertRaisesRegex(ValueError,'pasted_capture'):
            self.launch(request={**self.request,'native_capture':self.capture})
        before=self.snapshot()
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_wb_supplies SET cache_key='different' WHERE supply_id='101'");conn.commit()
        with self.assertRaisesRegex(ValueError,'canonical_supply'):
            self.launch()
        self.assertEqual(before,self.snapshot())

    def test_invoice_and_payment_are_distinct_sources_and_tamper_blocks(self):
        data=json.loads(self.evidence.read_text())
        invoice=Path(data['fees'][0]['original_invoice']['path'])
        invoice.write_bytes(b'%PDF-1.4\nchanged invoice')
        before=self.snapshot()
        with self.assertRaisesRegex(ValueError,'original_invoice_changed'):self.launch()
        self.assertEqual(before,self.snapshot())

    def test_real_full_preflight_then_registered_apply_pending_truthfully(self):
        before=self.snapshot();p=self.launch();self.assertEqual(p['state'],'preview');self.assertEqual(before,self.snapshot())
        self.assertTrue(p['scope']['full_numeric_preflight'])
        result=self.launch('apply',preview=p)
        self.assertEqual(result['state'],'ambiguous')
        self.assertTrue(result['readback']['native_complete']);self.assertTrue(result['readback']['fee_sources_complete'])
        self.assertTrue(result['readback']['ready_complete']);self.assertFalse(result['readback']['scope_complete'])
        with self.assertRaises((ApplyError,ValueError)):self.launch('apply',preview=p)

    def test_preflight_failure_writes_no_production_effect_or_attempt(self):
        before=self.snapshot()
        with patch.object(action,'_numeric',side_effect=ValueError('synthetic preflight rejection')):
            with self.assertRaisesRegex(ValueError,'preflight rejection'):self.launch()
        self.assertEqual(before,self.snapshot())
        self.assertEqual(self.launch('readback')['state'],'not_submitted')

    def test_quiet_and_source_cas_fail_before_money(self):
        before=self.snapshot()
        with patch.object(action,'_held',side_effect=ValueError('quiet_pause_required')):
            with self.assertRaisesRegex(ValueError,'quiet_pause_required'):self.launch()
        self.assertEqual(before,self.snapshot())
        p=self.launch()
        with closing(_connect(self.runtime.db_path)) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_ff_pool_balances SET source_watermark='concurrent' WHERE pool='FBO'");conn.commit()
        changed=self.snapshot()
        with self.assertRaisesRegex(ValueError,'preflight_sources_changed'):
            self.launch('apply',preview=p)
        self.assertEqual(changed,self.snapshot())

    def test_known_partial_recovers_bound_recipe_without_second_native_debit(self):
        p=self.launch();original=typed.apply_fees_from_effect_intent
        with patch.object(typed,'apply_fees_from_effect_intent',side_effect=RuntimeError('synthetic before fee owner')):
            first=self.launch('apply',preview=p)
        self.assertEqual(first['state'],'ambiguous');self.assertTrue(first['readback']['native_complete'])
        # Continuation uses retained original PDF/XLSX/evidence bytes, even
        # when the operator's original working files have disappeared.
        shutil.rmtree(self.inputs_dir)
        ref=first['readback'];req={**self.request,'action':action.RECOVER,'recovery_ref':'restore-fixture','original_recipe_digest':ref['recipe_digest']}
        p2=self.launch(op='recover-fixture',request=req)
        second=self.launch('apply',op='recover-fixture',request=req,preview=p2)
        self.assertEqual(second['state'],'ambiguous');self.assertTrue(second['readback']['ready_complete'])
        with closing(_connect(self.runtime.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM sheet_vitrina_v1_warehouse_business_operations WHERE source_system='historical_fbo_dispatch'").fetchone()[0],4)

    def test_unknown_fee_attempt_blocks_recovery_without_resend(self):
        p=self.launch()
        from packages.application.fulfillment_services import FulfillmentServicesBlock
        with patch.object(FulfillmentServicesBlock,'upload_xlsx',side_effect=RuntimeError('synthetic unknown outcome')):
            first=self.launch('apply',preview=p)
        ref=first['readback'];req={**self.request,'action':action.RECOVER,'recovery_ref':'restore-fixture','original_recipe_digest':ref['recipe_digest']}
        p2=self.launch(op='recover-fixture',request=req)
        with patch.object(FulfillmentServicesBlock,'upload_xlsx',side_effect=AssertionError('MUST NOT RESEND')) as submit:
            result=self.launch('apply',op='recover-fixture',request=req,preview=p2)
        self.assertEqual(result['state'],'ambiguous');self.assertFalse(result['readback']['fee_sources_complete']);submit.assert_not_called()
        self.assertEqual(result['readback']['fee_attempts'][0]['state'],'needs_review')


class ChildPublicationKindTests(unittest.TestCase):
    def test_recovery_cannot_parse_a_temporary_child_through_the_fbo_route(self):
        import sqlite3
        from contextlib import nullcontext
        from types import SimpleNamespace
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.row_factory=sqlite3.Row
            conn.execute('CREATE TABLE sheet_vitrina_v1_ready_publications(operation_id,attempt_id,kind,inputs_json)')
            temporary=typed.publication.TEMPORARY_CONTRACT
            conn.execute("INSERT INTO sheet_vitrina_v1_ready_publications VALUES('child','1',?,'not JSON')",(temporary,))
            recipe={'publication_operation_id':'child','publication_attempt_id':'1'}
            with patch.object(action.ready,'readonly',side_effect=lambda _:nullcontext(conn)), \
                 patch.object(typed,'apply_native_from_effect_intent',side_effect=AssertionError('native effects')):
                with patch.object(typed.publication,'decode_manifest',side_effect=AssertionError('wrong native kind decoded')):
                    with self.assertRaisesRegex(ValueError,'publication_kind_changed'):
                        action._pipeline(SimpleNamespace(db_path='fixture'),recipe=recipe,binding={},workbooks={},recapture=None,owner_token='none',now=None)
                conn.execute('UPDATE sheet_vitrina_v1_ready_publications SET kind=?,inputs_json=?',
                             (typed.CONTRACT,canonical({'contract':temporary})))
                with self.assertRaisesRegex(ValueError,'publication_kind_changed'):
                    action._pipeline(SimpleNamespace(db_path='fixture'),recipe=recipe,binding={},workbooks={},recapture=None,owner_token='none',now=None)


if __name__=='__main__':unittest.main()
