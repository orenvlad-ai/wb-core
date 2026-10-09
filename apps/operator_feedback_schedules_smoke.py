#!/usr/bin/env python3
"""Native synthetic schedule source/CAS/worker preservation/HTTP/Chromium."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import multiprocessing
import os
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from packages.application import operator_feedback_complaint_schedules as source,operator_operations as journal
from packages.application.sheet_vitrina_v1_feedbacks_auto_complaints import JsonFileFeedbacksAutoComplaintsStore
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.change_registry_observer import ChangeRegistryReadSurface
from apps.sheet_vitrina_v1_feedbacks_ai_smoke import NOW


def save_in_process(runtime,body,start,queue):
    store=JsonFileFeedbacksAutoComplaintsStore(Path(runtime),now_factory=lambda:NOW)
    cmd=source.command(body,actor='operator',account='fixture',account_scope='seller-portal-primary')
    start.wait(10)
    try:store.save_schedules(body['schedules'],operator_command=cmd);queue.put('saved')
    except source.SourceRejected as exc:queue.put(exc.code)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.runtime=Path(self.temp.name).resolve()
        self.app=RegistryUploadHttpEntrypoint(self.runtime,change_registry_read_surface=ChangeRegistryReadSurface(self.runtime,seller_id='fixture'))
        self.store=self.app.feedbacks_auto_complaints_block.store;self.store.now_factory=lambda:NOW
        self.scope=source.ScheduleScope.from_entrypoint(self.app,actor='operator')
    def body(self,identity='complaint-schedules:fixture_identity_0001',**change):
        return dict(operation_id=identity,expected_source_revision=source.revision(self.store.read()),
                    **(change or {'schedules':source.intent([dict(id='one',enabled=True),dict(id='two',enabled=True)])}))
    def save(self,body,actor='operator'):return self.app.handle_sheet_feedbacks_auto_complaints_schedules_save_request(body,actor=actor)
    def read(self,identity='complaint-schedules:fixture_identity_0001',scope=None,allowed=None):
        return journal.read_acceptance(self.app.runtime.db_path,identity,allowed_domains={source.DOMAIN} if allowed is None else allowed,complaint_schedules_scope=scope or self.scope)

    def test_missing_account_rejects_schedule_and_manual_before_source_write(self):
        from packages.application.operator_complaint_runs import RunScope,NotSaved
        body=self.body();before=self.store.path.read_bytes() if self.store.path.exists() else None
        self.app.change_registry_read_surface=None
        with self.assertRaisesRegex(source.SourceRejected,'complaint_schedules_native_scope_required'):
            self.save(body)
        with self.assertRaises(NotSaved) as rejected:
            RunScope.from_entrypoint(self.app,actor='operator')
        self.assertEqual(rejected.exception.code,'complaint_run_scope_required')
        self.assertEqual(self.store.path.read_bytes() if self.store.path.exists() else None,before)
        self.assertEqual(journal.journal(self.app.runtime.db_path,allowed_domains={source.DOMAIN,'feedback_complaint_run'})['total'],0)

    def test_native_immutable_original_ABA_runtime_updates_and_exact_disable_no_run_cancel(self):
        body=self.body();first=self.save(body)['acceptance'];original=source.raw(self.store.path)[source.LEDGER]
        rev=source.revision(self.store.read());self.store.add_run(dict(run_id='synthetic-running',schedule_id='one',status='running',created_at='2026-04-29T09:00:00Z'))
        self.store.update_run('synthetic-running',dict(events=[dict(message='native event',event='readback',timestamp='2026-04-29T09:00:00Z')]))
        self.store.update_schedule_after_run('one',dict(run_id='synthetic-running',status='running',created_at='2026-04-29T09:00:00Z'))
        self.assertEqual(source.revision(self.store.read()),rev);self.assertEqual(source.raw(self.store.path)[source.LEDGER],original)
        off=self.body('complaint-schedules:fixture_identity_0002',disable_schedule_id='one');receipt=self.save(off)['acceptance']
        rows=self.store.read()['schedules'];self.assertFalse(rows[0]['enabled']);self.assertTrue(rows[1]['enabled'])
        self.assertEqual(self.store.get_run('synthetic-running')['status'],'running');self.assertFalse(receipt['external_confirmed'])
        self.assertFalse(receipt['execution_required']);self.assertEqual(receipt['primary_effect'],'source_saved')
        off_revision=source.revision(self.store.read())
        self.store.update_run('synthetic-running',dict(status='completed',finished_at='2026-04-29T09:00:00Z'))
        self.store.update_schedule_after_run('one',self.store.get_run('synthetic-running'))
        self.assertFalse(self.store.read()['schedules'][0]['enabled']);self.assertEqual(source.revision(self.store.read()),off_revision)
        self.store.save_schedules(body['schedules']);self.assertNotEqual(source.revision(self.store.read()),first['source_ref']['after_revision'])
        current=self.store.path.read_bytes();self.assertEqual(self.save(body)['acceptance'],first);self.assertEqual(self.store.path.read_bytes(),current)
        self.assertTrue(self.save(off)['replayed']);self.assertTrue(self.store.read()['schedules'][0]['enabled'])
        with self.assertRaisesRegex(source.SourceRejected,'identity_conflict'):self.save(body,actor='foreign')
        with self.assertRaisesRegex(source.SourceRejected,'revision_stale'):self.save(dict(body,operation_id='complaint-schedules:fixture_identity_0003'))

    def test_native_two_process_CAS_one_save(self):
        body=self.body();ctx=multiprocessing.get_context('spawn');start=ctx.Event();queue=ctx.Queue()
        processes=[ctx.Process(target=save_in_process,args=(str(self.runtime),dict(body,operation_id='complaint-schedules:process_identity_000'+str(i)),start,queue)) for i in (1,2)]
        for p in processes:p.start()
        start.set();results=[queue.get(timeout=20) for _ in processes]
        for p in processes:p.join(10);self.assertEqual(p.exitcode,0)
        queue.close();queue.join_thread();self.assertEqual(sorted(results),['complaint_schedules_revision_stale','saved'])

    def test_native_capacity_before_source_and_worker_terminal_not_blocked_by_projection_read_bound(self):
        body=self.body()
        with patch.object(source,'MAX_RECEIPTS',0):
            with self.assertRaisesRegex(source.SourceRejected,'capacity'):self.save(body)
        self.assertIsNone(self.read());self.save(body);proof=source.raw(self.store.path)[source.LEDGER]
        self.store.add_run(dict(run_id='synthetic',status='running'))
        with patch.object(source,'MAX_READ_BYTES',1):
            with self.assertRaisesRegex(ValueError,'size_exceeded'):self.read()
            self.store.update_run('synthetic',dict(status='completed',finished_at='2026-04-29T09:00:00Z',events=[dict(event='completed',message='Native terminal proof')]))
        self.assertEqual(source.raw(self.store.path)[source.LEDGER],proof);self.assertEqual(self.store.get_run('synthetic')['status'],'completed')
        invalid=dict(body,schedules=[dict(body['schedules'][0],enabled='false')],operation_id='complaint-schedules:fixture_identity_0002')
        with self.assertRaisesRegex(source.SourceRejected,'command_invalid'):self.save(invalid)
        before=self.store.path.read_bytes()
        with self.assertRaisesRegex(source.SourceRejected,'target_missing'):self.save(self.body('complaint-schedules:fixture_identity_0002',disable_schedule_id='foreign'))
        self.assertEqual(self.store.path.read_bytes(),before)

    def test_native_postreplace_failure_retains_same_identity_and_original_receipt(self):
        body=self.body();native=source.atomic_write
        def lost(path,value):native(path,value);raise OSError('synthetic directory fsync acknowledgment lost')
        with patch.object(source,'atomic_write',side_effect=lost):
            with self.assertRaisesRegex(OSError,'acknowledgment lost'):self.save(body)
        original=self.read();self.assertTrue(original['durable_saved']);current=self.store.path.read_bytes()
        self.assertTrue(self.save(body)['replayed']);self.assertEqual(self.read(),original);self.assertEqual(self.store.path.read_bytes(),current)

    def test_native_grants_filter_before_count_search_detail_no_owner_init_or_reconcile(self):
        self.save(self.body());self.store.add_run(dict(run_id='active',status='running'));before=self.store.path.read_bytes()
        with patch.object(JsonFileFeedbacksAutoComplaintsStore,'__init__',side_effect=AssertionError('no interrupt mutation GET')):
            self.assertIsNotNone(self.read());self.assertEqual(self.store.path.read_bytes(),before)
            for scope in (replace(self.scope,actor='foreign'),replace(self.scope,account='foreign'),replace(self.scope,account_scope='foreign')):
                self.assertIsNone(self.read(scope=scope));self.assertEqual(journal.journal(self.app.runtime.db_path,allowed_domains={source.DOMAIN},complaint_schedules_scope=scope,search='complaint-schedules')['total'],0)
        with patch.object(source,'raw',side_effect=AssertionError('no denied reader')):self.assertIsNone(self.read(allowed=set()))
        raw=source.raw(self.store.path);row=raw[source.LEDGER][0];row['after']['schedules'][0]['enabled']=False;row['after_revision']=source.revision(row['after']);row['proof_digest']=source.digest({k:v for k,v in row.items() if k!='proof_digest'});self.store.path.write_text(json.dumps(raw))
        self.assertIsNone(self.read(scope=replace(self.scope,actor='foreign')))
        with self.assertRaisesRegex(ValueError,'retained_proof_invalid'):self.read()

    def test_actual_authenticated_HTTP_native_grants_actor_scope(self):
        from apps.operator_feedback_forms_browser_smoke import native_server
        from apps.registry_upload_http_entrypoint_auth_smoke import _password_hash
        import urllib.request,urllib.parse,urllib.error,http.cookiejar
        password='synthetic-schedule-grant'
        env=dict(WB_CORE_WEB_AUTH_REQUIRED='1',WB_CORE_WEB_AUTH_USERNAME='owner',WB_CORE_WEB_AUTH_PASSWORD_HASH=_password_hash(password),WB_CORE_WEB_AUTH_SESSION_SECRET='synthetic-schedule-only')
        with patch.dict(os.environ,env),native_server() as (base,app,f,job):
            for user,sections in [('denied',['prices']),('foreign',['feedbacks'])]:app.handle_sheet_vitrina_user_create_request(dict(user_id='fixture-'+user,username=user,display_name=user,role='operator',allowed_sections=sections,manage_users=False,password_hash=_password_hash(password),is_active=True,created_at='2026-07-20T12:00:00Z',updated_at='2026-07-20T12:00:00Z'))
            def login(user):
                opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                with opener.open(urllib.request.Request(base+'/login',data=urllib.parse.urlencode(dict(username=user,password=password,next='/sheet-vitrina-v1/operations')).encode(),headers={'Content-Type':'application/x-www-form-urlencoded'})) as response:response.read()
                return opener
            def get(opener,path):
                try:response=opener.open(base+path)
                except urllib.error.HTTPError as exc:response=exc
                with response:return response.status,json.load(response)
            owner=login('owner');body=dict(operation_id='complaint-schedules:http_identity_0001',schedules=source.intent([dict(id='one',enabled=True)]),expected_source_revision=source.revision(app.feedbacks_auto_complaints_block.store.read()))
            with owner.open(urllib.request.Request(base+source.PATH,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})) as response:self.assertEqual(json.load(response)['acceptance']['actor'],'owner')
            for user in ('denied','foreign'):
                opener=login(user)
                with patch.object(source,'raw',side_effect=AssertionError('no denied reader')) if user=='denied' else patch.object(JsonFileFeedbacksAutoComplaintsStore,'__init__',side_effect=AssertionError('no boot')):
                    status,payload=get(opener,'/v1/sheet-vitrina-v1/operations?domain='+source.DOMAIN+'&search='+body['operation_id']);self.assertEqual(status,200);self.assertEqual(payload['total'],0)
                    self.assertEqual(get(opener,'/v1/sheet-vitrina-v1/operations/'+body['operation_id'])[0],404)
            self.assertEqual(get(owner,'/v1/sheet-vitrina-v1/operations/'+body['operation_id'])[0],200)

    def test_actual_Chromium_unknown_enable_two_tabs_OFF_independent_and_GET_only_recovery(self):
        from apps.operator_feedback_forms_browser_smoke import native_server
        from packages.adapters import registry_upload_http_entrypoint as web
        with native_server() as (base,app,f,job),sync_playwright() as pw:
            app.feedbacks_auto_complaints_block.store.save_schedules(source.intent([dict(id='one',enabled=False)]))
            browser=pw.chromium.launch()
            try:
                context=browser.new_context();page=context.new_page();other=context.new_page();posts=[];held=[];errors=[]
                def all_routes(route):
                    path=route.request.url[len(base):].split('?')[0]
                    if path==source.PATH or path.startswith('/v1/sheet-vitrina-v1/operations/') or path==web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH or path.endswith(('.js','.css')):route.continue_()
                    else:route.fulfill(status=200,content_type='application/json',body='{}')
                context.route(base+'/**',all_routes)
                def save_lost(route):
                    if route.request.method!='POST':route.continue_();return
                    body=route.request.post_data_json;posts.append(body);response=route.fetch();self.assertEqual(response.status,200,response.text())
                    if 'schedules' in body:held.append(route)
                    else:route.fulfill(response=response)
                def missing(route):route.abort('failed')
                context.route('**/feedbacks/automation/schedules',save_lost);context.route('**/operations/complaint-schedules*',missing)
                for tab in (page,other):
                    tab.on('pageerror',lambda error:errors.append(str(error)));tab.goto(base+web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH+'?tab=settings');tab.evaluate('async()=>{await loadFeedbacksAutomation();state.feedbacks.automation.schedules[0].enabled=true;}')
                page.evaluate('()=>{window.enable=saveFeedbacksAutomationSchedules();}')
                while not held:page.wait_for_timeout(50)
                # OFF cannot await the other tab's held enable-response lock.
                other.evaluate('async()=>await disableFeedbacksAutomationSchedule("one")')
                self.assertEqual(len(posts),2);self.assertFalse(app.feedbacks_auto_complaints_block.store.read()['schedules'][0]['enabled'])
                held.pop().fulfill(status=400,content_type='application/json',body=json.dumps(dict(error='unknown after native commit')))
                page.evaluate('async()=>await window.enable');self.assertTrue(page.evaluate('()=>!!localStorage.getItem(complaintScheduleFenceKey("save"))'))
                page.evaluate('async()=>await saveFeedbacksAutomationSchedules()');page.reload();page.evaluate('async()=>{await loadFeedbacksAutomation();await saveFeedbacksAutomationSchedules();}')
                self.assertEqual(len(posts),2);self.assertFalse(app.feedbacks_auto_complaints_block.store.read()['schedules'][0]['enabled'])
                context.unroute('**/operations/complaint-schedules*',missing);page.evaluate('async()=>await loadFeedbacksAutomation()')
                self.assertFalse(page.evaluate('()=>!!localStorage.getItem(complaintScheduleFenceKey("save"))'))
                self.assertFalse(page.evaluate('state.feedbacks.automation.schedules[0].enabled'))
                self.assertEqual(len(source.records(source.raw(app.feedbacks_auto_complaints_block.store.path))),2)
                self.assertFalse(f.transport.write_calls);self.assertFalse(errors,errors)
                self.assertTrue(page.evaluate('()=>complaintSchedulePrecommitRejected({httpStatus:423,code:"business_data_maintenance"})'))
                self.assertFalse(page.evaluate('()=>complaintSchedulePrecommitRejected({httpStatus:400,code:"unknown"})'))
            finally:browser.close()


if __name__=='__main__':unittest.main(verbosity=2)
