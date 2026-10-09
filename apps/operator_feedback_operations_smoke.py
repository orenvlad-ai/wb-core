#!/usr/bin/env python3
"""Actual native repositories/workers with fake transports, temporary stores."""
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps import wb_autoanswers_publication_test as ai_fixture
from apps import wb_buyer_support_pilot_smoke as buyer_fixture
from packages.application import operator_operations as journal
from packages.application.operator_feedback_operations import FeedbackSurface,read_native
from packages.application.wb_autoanswers_runtime import autoanswers_store_path,final_reply_hash


class ProjectionTests(unittest.TestCase):
    def primary(self,runtime):
        path=runtime/'projection-operational.sqlite3'
        sqlite3.connect(path).close()
        return path

    def scope(self,runtime,*,cabinet='cabinet-fixture',actor='reviewer',account='seller-fixture'):
        runtime=runtime.resolve()
        return FeedbackSurface(runtime,autoanswers_store_path(runtime),runtime/'buyer-support'/'pilot.sqlite3',cabinet,actor,account)

    def test_real_manual_draft_approval_worker_readback_and_query_only(self):
        fixture=ai_fixture.PublicationTest();fixture.setUp();self.addCleanup(fixture.tearDown)
        runtime=Path(fixture.temp.name);db=self.primary(runtime);scope=self.scope(runtime)
        job=fixture.manual_reviewed();identity='feedback-reply:'+job['processing_key']
        def read():return journal.read_acceptance(db,identity,allowed_domains={'feedback_reply'},feedback_scope=scope)
        draft=read();self.assertEqual(draft['state'],'completed');self.assertEqual(draft['primary_effect'],'reply_draft_saved')
        self.assertFalse(draft['external_confirmed']);self.assertEqual(draft['source_ref']['manual_edit_revision'],1)
        fixture.repo.approve_for_publication(job['processing_key'],actor_id='reviewer',confirmed=True,
            expected_reply_sha256=job['manual_reply_sha256'])
        self.assertEqual(read()['state'],'processing');self.assertFalse(read()['external_confirmed'])
        fixture.transport.readbacks=[{'answer':{'text':job['manual_reply']}}]
        fixture.worker.run_once()
        fixture.worker.run_once()
        receipt=read();self.assertTrue(receipt['external_confirmed']);self.assertEqual(receipt['state'],'completed')
        self.assertEqual(len(fixture.transport.write_calls),1)
        files={p:p.read_bytes() for p in runtime.rglob('*') if p.is_file()}
        with patch.object(fixture.repo,'_connect',side_effect=AssertionError('no writable owner connection')):
            self.assertEqual(read(),receipt)
            listing=journal.journal(db,allowed_domains={'feedback_reply'},feedback_scope=scope)
        self.assertEqual(listing['total'],1);self.assertEqual(listing['items'],[receipt])
        self.assertEqual(files,{p:p.read_bytes() for p in runtime.rglob('*') if p.is_file()})
        # Native flag alone is insufficient: mismatched exact readback blocks.
        with fixture.repo.transaction() as conn:
            conn.execute("UPDATE sheet_vitrina_v1_wb_publication_jobs SET readback_hash=?",('forged',))
        self.assertFalse(read()['external_confirmed']);self.assertEqual(read()['state'],'needs_attention')

    def test_actual_pilot_send_unknown_confirmed_cabinet_no_calls_on_projection(self):
        fixture=buyer_fixture.PilotTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        runtime=fixture.runtime;db=self.primary(runtime);scope=self.scope(runtime,cabinet=buyer_fixture.CABINET,actor='operator')
        draft=fixture.draft();op=fixture.send(draft);identity='buyer-support:'+op['operation_id']
        from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
        app=RegistryUploadHttpEntrypoint(runtime,buyer_support_pilot=fixture.service)
        native=app._feedback_operator_result(op,'buyer_support',op['operation_id'],'operator')
        self.assertEqual(native['acceptance']['operation_id'],identity)
        self.assertFalse(native['acceptance']['external_confirmed'])
        def read(s=scope):return journal.read_acceptance(db,identity,allowed_domains={'buyer_support'},feedback_scope=s)
        self.assertEqual(read()['state'],'processing');self.assertFalse(read()['external_confirmed'])
        with fixture.service.store.db(write=True) as conn:
            changed=dict(op,state='write_started');fixture.service._operation_save(conn,buyer_fixture.CABINET,changed)
        self.assertEqual(read()['state'],'needs_attention')
        fixture.service.reconcile(buyer_fixture.CABINET,operation_id=op['operation_id'],request_id=fixture.request(),actor='operator')
        self.assertTrue(read()['external_confirmed'])
        counts=(fixture.wb.sends,fixture.wb.reads,fixture.wb.decisions,fixture.provider.calls)
        files={p:p.read_bytes() for p in runtime.rglob('*') if p.is_file()}
        self.assertEqual(journal.journal(db,allowed_domains={'buyer_support'},feedback_scope=scope)['total'],1)
        self.assertIsNone(read(self.scope(runtime,cabinet='foreign')))
        self.assertEqual(journal.journal(db,allowed_domains={'buyer_support'},feedback_scope=self.scope(runtime,cabinet='foreign'))['total'],0)
        self.assertIsNone(journal.read_acceptance(db,identity,allowed_domains=set(),feedback_scope=scope))
        self.assertEqual(journal.journal(db,allowed_domains=set(),feedback_scope=scope,search=op['operation_id'])['total'],0)
        self.assertEqual(files,{p:p.read_bytes() for p in runtime.rglob('*') if p.is_file()})
        self.assertEqual(counts,(fixture.wb.sends,fixture.wb.reads,fixture.wb.decisions,fixture.provider.calls))

    def test_missing_sources_do_not_initialize_storage_or_read_denied_domains(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as folder:
            runtime=Path(folder);db=self.primary(runtime);scope=self.scope(runtime)
            self.assertEqual(journal.journal(db,allowed_domains={'feedback_reply','buyer_support','feedback_complaint'},feedback_scope=scope)['total'],0)
            self.assertFalse(scope.autoanswers_db.exists());self.assertFalse(scope.buyer_db.exists())
            # Denied sources are not touched even if unreadable/malformed.
            directory=runtime/'feedbacks_complaints_submit_jobs';directory.mkdir();(directory/'jobs.json').write_text('invalid')
            self.assertEqual(journal.journal(db,allowed_domains=set(),feedback_scope=scope)['total'],0)

    def test_mixed_native_file_and_sql_sort_count_search_page(self):
        fixture=ai_fixture.PublicationTest();fixture.setUp();self.addCleanup(fixture.tearDown)
        runtime=Path(fixture.temp.name);db=self.primary(runtime);scope=self.scope(runtime)
        fixture.manual_reviewed()
        directory=runtime/'feedbacks_complaints_submit_jobs';directory.mkdir()
        jobs=[dict(run_id='run-'+str(i),created_at=f'2026-07-20T12:{i:02}:00+00:00',status='success',
            requested_by='reviewer',account_id='seller-fixture',selected_count=2,submitted_count=1,selected_feedback_ids=['one','two'],
            submitted_feedback_ids=['one'],attempts=[dict(feedback_id='one',run_id='run-'+str(i),attempt_status='submitted',code='row_submit_confirmed_success')]) for i in range(50)]
        (directory/'jobs.json').write_text(json.dumps(dict(jobs=jobs)))
        allowed={'feedback_reply','feedback_complaint'}
        full=journal.journal(db,allowed_domains=allowed,feedback_scope=scope,limit=100)
        paged=[]
        for page in range(1,8):
            result=journal.journal(db,allowed_domains=allowed,feedback_scope=scope,page=page,limit=9)
            self.assertEqual(result['total'],51);paged.extend(result['items'])
        self.assertEqual(paged,full['items'])
        receipt=full['items'][0];self.assertTrue(receipt['partial']);self.assertFalse(receipt['external_confirmed'])
        self.assertEqual(journal.journal(db,allowed_domains={'feedback_complaint'},feedback_scope=scope,search='run-49')['total'],1)
        self.assertEqual(journal.journal(db,allowed_domains={'feedback_reply'},feedback_scope=scope,search='run-49')['total'],0)

    def test_native_complaint_invalid_retained_inventory_never_forgets_identity(self):
        from tempfile import TemporaryDirectory
        from packages.application import sheet_vitrina_v1_feedbacks_complaints as source
        with TemporaryDirectory() as folder:
            runtime=Path(folder).resolve()
            owner=source.SheetVitrinaV1FeedbacksComplaintsBlock(runtime_dir=runtime)
            owner.submit_jobs.path.parent.mkdir(parents=True,exist_ok=True)
            bad=json.dumps({'jobs':'damaged inventory'}).encode()
            owner.submit_jobs.path.write_bytes(bad)
            payload=dict(feedback_ids=['one'],max_submit=1,date_from='2026-07-01',date_to='2026-07-20',
                stars=[1],request_key='exact',account_id='seller-fixture',requested_by='reviewer')
            with patch.object(source,'admitted_thread') as thread:
                with self.assertRaisesRegex(source.SheetVitrinaV1FeedbacksComplaintsError,'invalid inventory'):
                    owner.submit_selected(payload)
                thread.assert_not_called()
            self.assertEqual(owner.submit_jobs.path.read_bytes(),bad)
            with self.assertRaisesRegex(ValueError,'inventory_invalid'):
                journal.journal(self.primary(runtime),allowed_domains={'feedback_complaint'},feedback_scope=self.scope(runtime))


    def test_native_complaint_atomic_request_manifest_repeat_conflict_busy_and_scope(self):
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from concurrent.futures import ThreadPoolExecutor
        from packages.application import sheet_vitrina_v1_feedbacks_complaints as source
        with TemporaryDirectory() as folder:
            runtime=Path(folder).resolve();db=self.primary(runtime)
            owner=source.SheetVitrinaV1FeedbacksComplaintsBlock(runtime_dir=runtime)
            payload=dict(feedback_ids=['one','two'],max_submit=2,date_from='2026-07-01',date_to='2026-07-20',
                stars=[1],request_key='request-exact-one',account_id='seller-fixture',requested_by='reviewer')
            threads=[]
            def spawn(*args,**kwargs):
                threads.append(kwargs);return SimpleNamespace(start=lambda:None)
            with patch.object(source,'admitted_thread',side_effect=spawn),patch.object(source,'current_lock_status',return_value={'busy':False}):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results=list(pool.map(lambda _:owner.submit_selected(payload),range(2)))
                self.assertEqual(len(threads),1);self.assertEqual(results[0]['run_id'],results[1]['run_id'])
                job=results[0];self.assertEqual(job['selected_feedback_ids'],['one','two'])
                self.assertEqual(len(job['request_digest']),64)
                for changed in (dict(payload,feedback_ids=['foreign']),dict(payload,requested_by='foreign'),dict(payload,account_id='foreign')):
                    with self.assertRaisesRegex(ValueError,'identity_conflict'):owner.submit_selected(changed)
                # Another native job does not leak its identity through busy.
                busy=owner.submit_selected(dict(payload,request_key='new-command',requested_by='foreign'))
                self.assertTrue(busy['not_accepted']);self.assertFalse(busy['run_id'])
                self.assertNotIn(job['run_id'],str(busy))
            with patch.object(source,'current_lock_status',return_value={'busy':True,'run_id':'foreign-portal-job'}):
                self.assertEqual(owner.submit_selected(payload)['run_id'],job['run_id'])
                busy=owner.submit_selected(dict(payload,request_key='new-command'))
                self.assertFalse(busy['run_id']);self.assertNotIn('foreign-portal-job',str(busy))
            for actor,account in (('foreign','seller-fixture'),('reviewer','foreign')):
                with self.assertRaises(source.SheetVitrinaV1FeedbacksComplaintsError):
                    owner.submit_jobs.get(job['run_id'],actor=actor,account_id=account)
                s=self.scope(runtime,actor=actor,account=account)
                self.assertEqual(journal.journal(db,allowed_domains={'feedback_complaint'},feedback_scope=s,search='request-exact-one')['total'],0)
                self.assertIsNone(read_native(db,domain='feedback_complaint',native_id=payload['request_key'],allowed_domains={'feedback_complaint'},scope=s))
            scope=self.scope(runtime)
            receipt=read_native(db,domain='feedback_complaint',native_id=payload['request_key'],allowed_domains={'feedback_complaint'},scope=scope)
            self.assertEqual(receipt['source_ref']['selected_feedback_ids'],['one','two']);self.assertEqual(receipt['state'],'processing')
            with self.assertRaisesRegex(ValueError,'manifest_immutable'):
                owner.submit_jobs.patch(job['run_id'],{'account_id':'foreign'})
            owner.submit_jobs.patch(job['run_id'],dict(status='success',submitted_count=2,submitted_feedback_ids=['one','two'],
                attempts=[dict(feedback_id=target,run_id=job['run_id'],attempt_status='submitted',code='row_submit_confirmed_success') for target in ['one','two']]))
            receipt=read_native(db,domain='feedback_complaint',native_id=payload['request_key'],allowed_domains={'feedback_complaint'},scope=scope)
            self.assertTrue(receipt['external_confirmed']);self.assertEqual(receipt['state'],'completed')
            self.assertFalse(receipt['resubmit_allowed']);self.assertEqual(len(threads),1)
            with patch.object(source,'admitted_thread',side_effect=spawn),patch.object(source,'current_lock_status',return_value={'busy':False}):
                for index in range(110):
                    newer=owner.submit_selected(dict(payload,request_key='new-'+str(index)))
                    owner.submit_jobs.patch(newer['run_id'],{'status':'success'})
                saved=owner.submit_selected(payload)
                self.assertEqual(saved['run_id'],job['run_id']);self.assertEqual(len(threads),111)
            retained=json.loads(owner.submit_jobs.path.read_bytes())
            self.assertEqual(len(retained['jobs']),111)
            self.assertEqual(owner.submit_jobs.get(job['run_id'],actor='reviewer',account_id='seller-fixture')['request_digest'],job['request_digest'])


if __name__=='__main__':unittest.main()
