#!/usr/bin/env python3
"""Synthetic native prompt file/HTTP/Chromium proofs; no paid analysis or WB."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import multiprocessing
import os
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from packages.application import operator_feedback_analysis_settings as source, operator_operations as journal
from packages.application.sheet_vitrina_v1_feedbacks_ai import SheetVitrinaV1FeedbacksAiBlock, JsonFileFeedbacksAiPromptStore
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.change_registry_observer import ChangeRegistryReadSurface
from apps.sheet_vitrina_v1_feedbacks_ai_smoke import FakeAiProvider, NOW


def concurrent_save(runtime, body, start, queue):
    block = SheetVitrinaV1FeedbacksAiBlock(runtime_dir=Path(runtime), provider=FakeAiProvider(), now_factory=lambda: NOW)
    cmd = source.command(body, actor='operator', account='fixture', account_scope='seller-portal-primary')
    start.wait(10)
    try:
        block.save_prompt(body, operator_command=cmd)
        queue.put('saved')
    except source.SourceRejected as exc:
        queue.put(exc.code)


class PromptTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name).resolve()
        self.app = RegistryUploadHttpEntrypoint(self.runtime,
            change_registry_read_surface=ChangeRegistryReadSurface(self.runtime, seller_id='fixture'))
        self.block = self.app.feedbacks_ai_block
        self.block.provider = FakeAiProvider(); self.block.now_factory = lambda: NOW
        self.scope = source.PromptScope.from_entrypoint(self.app, actor='operator')

    def body(self, identity='analysis-settings:fixture_identity_0001', **change):
        return dict(operation_id=identity, prompt='Правила разбора.', model='gpt-5.5',
                    expected_source_revision=self.block.get_prompt()['source_revision'], **change)

    def save(self, body, actor='operator'):
        return self.app.handle_sheet_feedbacks_ai_prompt_save_request(body, actor=actor)

    def read(self, identity='analysis-settings:fixture_identity_0001', scope=None, allowed=None):
        return journal.read_acceptance(self.app.runtime.db_path, identity,
            allowed_domains={source.DOMAIN} if allowed is None else allowed, analysis_settings_scope=scope or self.scope)

    def test_native_exact_receipt_ABA_legacy_preservation_and_duplicate_before_provider(self):
        first_body=self.body(); first=self.save(first_body)['acceptance']
        self.assertEqual(first['primary_effect'],'source_saved'); self.assertFalse(first['external_confirmed'])
        self.assertFalse(first['calculation_completed']); self.assertFalse(self.block.provider.calls)
        before = source.raw(self.block.prompt_store.path); retained = before[source.LEDGER]
        self.block.save_prompt(dict(prompt='Other',model='gpt-5.5'))
        self.block.save_prompt(dict(prompt='Правила разбора.',model='gpt-5.5'))
        current = source.raw(self.block.prompt_store.path)
        self.assertEqual(current[source.LEDGER],retained)
        self.assertNotEqual(source.revision(current),first['source_ref']['after_revision'])
        with patch.object(self.block.provider,'list_models',side_effect=AssertionError('no provider on retry')):
            self.assertTrue(self.save(first_body)['replayed']); self.assertEqual(self.save(first_body)['acceptance'],first)
            self.assertEqual(self.read()['saved_source'],first['saved_source'])
        self.assertEqual(source.raw(self.block.prompt_store.path),current)
        with self.assertRaisesRegex(source.SourceRejected,'identity_conflict'):self.save(dict(first_body,prompt='Foreign'))
        with self.assertRaisesRegex(source.SourceRejected,'identity_conflict'):self.save(first_body,actor='foreign')
        with self.assertRaisesRegex(source.SourceRejected,'revision_stale'):
            self.save(dict(first_body,operation_id='analysis-settings:fixture_identity_0002'))

    def test_native_two_process_CAS_one_source_commit_and_legacy_generation_ABA(self):
        initial=self.body(); ctx=multiprocessing.get_context('spawn'); start=ctx.Event(); queue=ctx.Queue()
        processes=[ctx.Process(target=concurrent_save,args=(str(self.runtime),dict(initial,operation_id='analysis-settings:process_identity_000'+str(i)),start,queue)) for i in (1,2)]
        for process in processes:process.start()
        start.set()
        results=[queue.get(timeout=20) for _ in processes]
        for process in processes:process.join(10); self.assertEqual(process.exitcode,0)
        queue.close(); queue.join_thread()
        self.assertEqual(sorted(results),['analysis_settings_revision_stale','saved'])
        self.assertEqual(len(source.records(source.raw(self.block.prompt_store.path))),1)

    def test_native_atomic_failure_capacity_invalid_operands_before_source_save(self):
        body=self.body(); path=self.block.prompt_store.path
        with patch.object(source,'atomic_write',side_effect=OSError('synthetic before replacement')):
            with self.assertRaises(OSError):self.save(body)
        self.assertFalse(path.exists()); self.assertIsNone(self.read())
        with patch.object(source,'MAX_RECEIPTS',0):
            with self.assertRaisesRegex(source.SourceRejected,'capacity'):self.save(body)
        self.assertFalse(path.exists())

        with self.assertRaisesRegex(source.SourceRejected,'model_invalid'):self.save(dict(body,model='not-available'))
        with self.assertRaisesRegex(source.SourceRejected,'command_invalid'):self.save(dict(body,prompt=''))
        self.assertFalse(path.exists())
        cmd=source.command(body,actor='operator',account='fixture',account_scope='seller-portal-primary')
        with self.assertRaisesRegex(source.SourceRejected,'native_operand_mismatch'):
            self.block.prompt_store.write(prompt='Substituted',model='gpt-5.5',updated_at='2026-04-29T09:00:00Z',operator_command=cmd)
        self.assertFalse(path.exists())

    def test_missing_account_rejects_prompt_before_source_write(self):
        body=self.body()
        self.app.change_registry_read_surface=None
        with self.assertRaisesRegex(source.SourceRejected,'analysis_settings_native_scope_required'):
            self.save(body)
        self.assertFalse(self.block.prompt_store.path.exists())
        self.assertIsNone(journal.read_acceptance(self.app.runtime.db_path,body['operation_id'],
            allowed_domains={source.DOMAIN},analysis_settings_scope=None))

    def test_native_multibyte_capacity_before_replace_and_catalog_is_not_source_CAS(self):
        body=self.body(); self.save(body); path=self.block.prompt_store.path; before=path.read_bytes()
        request=self.body('analysis-settings:fixture_identity_0002'); request['prompt']='😀'*16000
        with patch.object(source,'MAX_LEDGER_BYTES',len(source.encoded(source.raw(path)[source.LEDGER]))+1000):
            with self.assertRaisesRegex(source.SourceRejected,'capacity'):self.save(request)
        self.assertEqual(path.read_bytes(),before)
        current=self.block.get_prompt()['source_revision']
        with patch.object(self.block.provider,'list_models',return_value=['gpt-5.5','gpt-5-mini']):
            self.assertEqual(self.block.get_prompt()['source_revision'],current)
        # Discovery happens after the source snapshot, and cannot pair the old
        # displayed prompt with an unseen newer CAS revision.
        original=self.block.provider.list_models
        def change_during_discovery():
            self.block.prompt_store.write(prompt='Concurrent native save',model='gpt-5.5',updated_at='2026-04-29T09:00:00Z')
            return original()
        with patch.object(self.block.provider,'list_models',side_effect=change_during_discovery):observed=self.block.get_prompt()
        self.assertEqual(observed['source_revision'],current);self.assertEqual(observed['prompt'],'Правила разбора.')
        with self.assertRaisesRegex(source.SourceRejected,'revision_stale'):
            self.save(dict(request,expected_source_revision=observed['source_revision']))

    def test_native_symlink_source_denied_and_GET_creates_no_source_lock(self):
        path=self.block.prompt_store.path; lock=path.with_suffix(path.suffix+'.lock')
        self.block.get_prompt(); self.read(); self.assertFalse(path.exists());self.assertFalse(lock.exists())
        target=self.runtime/'foreign.json';target.write_text('{}');path.symlink_to(target)
        with self.assertRaisesRegex(ValueError,'path_unsafe'):self.read()
        with self.assertRaisesRegex(ValueError,'path_unsafe'):self.save(dict(operation_id='analysis-settings:fixture_identity_0001',prompt='Test',model='gpt-5.5',expected_source_revision=source.revision({})))
        self.assertEqual(target.read_text(),'{}')

    def test_native_postcommit_read_failure_exact_recovery_and_proof_tamper(self):
        body=self.body(); original=source.retained; reads=[]
        def fail_after_commit(value,cmd):
            reads.append(1)
            if len(reads)==3:raise ValueError('synthetic committed projection failed')
            return original(value,cmd)
        with patch.object(source,'retained',side_effect=fail_after_commit):
            with self.assertRaisesRegex(ValueError,'projection failed'):self.save(body)
        receipt=self.read(); self.assertTrue(receipt['durable_saved'])
        with patch.object(self.block.provider,'list_models',side_effect=AssertionError('no retry provider')):
            self.assertTrue(self.save(body)['replayed'])
        raw=source.raw(self.block.prompt_store.path); row=raw[source.LEDGER][0]
        row['after']['prompt']='Forged'; row['proof_digest']=source.digest({k:v for k,v in row.items() if k!='proof_digest'})
        self.block.prompt_store.path.write_text(json.dumps(raw))
        self.assertIsNone(self.read(scope=replace(self.scope,actor='foreign')))
        with self.assertRaisesRegex(ValueError,'retained_proof_invalid'):self.read()

    def test_native_grants_account_actor_before_file_read_counts_pages_search_no_bootstrap(self):
        self.save(self.body()); path=self.block.prompt_store.path; before=path.read_bytes()
        with (patch.object(JsonFileFeedbacksAiPromptStore,'__init__',side_effect=AssertionError('no constructor GET')),
              patch.object(self.block.provider,'list_models',side_effect=AssertionError('no provider GET'))):
            self.assertIsNotNone(self.read())
            for scope in (replace(self.scope,actor='foreign'),replace(self.scope,account='foreign'),replace(self.scope,account_scope='foreign')):
                self.assertIsNone(self.read(scope=scope))
                listing=journal.journal(self.app.runtime.db_path,allowed_domains={source.DOMAIN},analysis_settings_scope=scope,search='analysis-settings')
                self.assertEqual(listing['total'],0)
            listing=journal.journal(self.app.runtime.db_path,allowed_domains={source.DOMAIN},analysis_settings_scope=self.scope,page=100,search='analysis-settings')
            self.assertEqual(listing['total'],1);self.assertFalse(listing['items'])
        with patch.object(source,'raw',side_effect=AssertionError('denied before source reader')):
            self.assertIsNone(self.read(allowed=set()))
        self.assertEqual(path.read_bytes(),before)

    def test_actual_native_HTTP_grants_actor_scope_and_no_constructor_GET(self):
        from apps.operator_feedback_forms_browser_smoke import native_server
        from apps.registry_upload_http_entrypoint_auth_smoke import _password_hash
        from packages.adapters import registry_upload_http_entrypoint as web
        import urllib.request,urllib.parse,urllib.error,http.cookiejar
        password='synthetic-prompt-grant-only'
        env=dict(WB_CORE_WEB_AUTH_REQUIRED='1',WB_CORE_WEB_AUTH_USERNAME='owner',WB_CORE_WEB_AUTH_PASSWORD_HASH=_password_hash(password),WB_CORE_WEB_AUTH_SESSION_SECRET='synthetic-prompt-secret-only')
        with patch.dict(os.environ,env),native_server() as (base,app,f,job):
            app.feedbacks_ai_block.provider=FakeAiProvider()
            for username,sections in [('denied',['prices']),('foreign',['feedbacks'])]:
                app.handle_sheet_vitrina_user_create_request(dict(user_id='fixture-'+username,username=username,display_name=username,role='operator',allowed_sections=sections,manage_users=False,password_hash=_password_hash(password),is_active=True,created_at='2026-07-20T12:00:00Z',updated_at='2026-07-20T12:00:00Z'))
            def login(username):
                opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                with opener.open(urllib.request.Request(base+'/login',data=urllib.parse.urlencode(dict(username=username,password=password,next='/sheet-vitrina-v1/operations')).encode(),headers={'Content-Type':'application/x-www-form-urlencoded'})) as response:response.read()
                return opener
            def get(opener,path):
                try:response=opener.open(base+path)
                except urllib.error.HTTPError as exc:response=exc
                with response:return response.status,json.load(response)
            owner=login('owner'); body=dict(operation_id='analysis-settings:http_identity_0001',prompt='Saved',model='gpt-5.5',expected_source_revision=app.feedbacks_ai_block.get_prompt()['source_revision'])
            with owner.open(urllib.request.Request(base+source.PATH,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})) as response:receipt=json.load(response)['acceptance']
            self.assertEqual(receipt['actor'],'owner')
            for username in ('denied','foreign'):
                opener=login(username)
                with patch.object(source,'raw',side_effect=AssertionError('denied before read')) if username=='denied' else patch.object(app.feedbacks_ai_block.provider,'list_models',side_effect=AssertionError('no model GET')):
                    status,payload=get(opener,'/v1/sheet-vitrina-v1/operations?domain='+source.DOMAIN+'&search='+body['operation_id'])
                    self.assertEqual(status,200);self.assertEqual(payload['total'],0)
                    self.assertEqual(get(opener,'/v1/sheet-vitrina-v1/operations/'+body['operation_id'])[0],404)
            with patch.object(JsonFileFeedbacksAiPromptStore,'__init__',side_effect=AssertionError('no bootstrap')),patch.object(app.feedbacks_ai_block.provider,'list_models',side_effect=AssertionError('no provider')):
                self.assertEqual(get(owner,'/v1/sheet-vitrina-v1/operations/'+body['operation_id'])[0],200)

    def test_actual_Chromium_two_tabs_committed_unknown400_failed_GET_reload_no_second_POST(self):
        from apps.operator_feedback_forms_browser_smoke import native_server
        from packages.adapters import registry_upload_http_entrypoint as web
        with native_server() as (base,app,f,job),sync_playwright() as pw:
            app.feedbacks_ai_block.provider=FakeAiProvider(); browser=pw.chromium.launch()
            try:
                context=browser.new_context(); page=context.new_page(); other=context.new_page(); errors=[]; posts=[]; reads=[]; held=[]
                def route_all(route):
                    path=route.request.url[len(base):].split('?')[0]
                    if path==source.PATH or path.startswith('/v1/sheet-vitrina-v1/operations/') or path==web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH or path.endswith(('.js','.css')):route.continue_()
                    else:route.fulfill(status=200,content_type='application/json',body='{}')
                context.route(base+'/**',route_all)
                def save_lost(route):
                    posts.append(route.request.post_data_json); response=route.fetch(); self.assertEqual(response.status,200,response.text()); held.append(route)
                def missing(route):reads.append(route.request.url);route.abort('failed')
                context.route('**/feedbacks/ai-prompt',lambda route:save_lost(route) if route.request.method=='POST' else route.continue_())
                context.route('**/operations/analysis-settings*',missing)
                for tab in (page,other):
                    tab.on('pageerror',lambda error:errors.append(str(error)))
                    tab.goto(base+web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH+'?tab=settings')
                    tab.evaluate('async()=>{await loadFeedbacksPrompt();state.feedbacks.promptDraft="Новая инструкция";state.feedbacks.model="gpt-5.5";}')
                page.evaluate('()=>{window.firstSave=saveFeedbacksPrompt();}')
                page.wait_for_function('()=>localStorage.getItem(analysisSettingsFenceKey())!==null')
                page.wait_for_timeout(150)
                while not held:page.wait_for_timeout(50)
                other.evaluate('()=>{window.secondSave=saveFeedbacksPrompt();}')
                held.pop().fulfill(status=400,content_type='application/json',body=json.dumps(dict(error='synthetic projection unknown after commit')))
                page.evaluate('async()=>await window.firstSave');other.evaluate('async()=>await window.secondSave')
                self.assertEqual(len(posts),1); self.assertTrue(page.evaluate('()=>!!localStorage.getItem(analysisSettingsFenceKey())'))
                page.reload();page.evaluate('async()=>{await loadFeedbacksPrompt();state.feedbacks.promptDraft="Another";await saveFeedbacksPrompt();}')
                self.assertEqual(len(posts),1);self.assertGreaterEqual(len(reads),3)
                context.unroute('**/operations/analysis-settings*',missing)
                page.evaluate('async()=>await onceAnalysisSettings(null,true)')
                self.assertFalse(page.evaluate('()=>!!localStorage.getItem(analysisSettingsFenceKey())'))
                self.assertEqual(len(source.records(source.raw(app.feedbacks_ai_block.prompt_store.path))),1)
                self.assertEqual(page.locator('#operator-analysis-settings-receipt .ff-operation-status').inner_text(),'Сохранено')
                self.assertFalse(f.transport.write_calls);self.assertFalse(app.feedbacks_ai_block.provider.calls);self.assertFalse(errors,errors)
                self.assertFalse(page.evaluate('()=>analysisSettingsPrecommitRejected({httpStatus:422,code:"analysis_settings_revision_stale"})'))
                self.assertTrue(page.evaluate('()=>analysisSettingsPrecommitRejected({httpStatus:423,code:"business_data_maintenance"})'))
                self.assertFalse(page.evaluate('()=>analysisSettingsPrecommitRejected({httpStatus:423,code:"unknown"})'))
                # The exact native maintenance refusal occurs before dispatch,
                # so it can release this uncommitted fence. Unknown 423 cannot.
                context.unroute('**/feedbacks/ai-prompt')
                original=app.feedbacks_ai_block.prompt_store.path.read_bytes()
                page.evaluate('version=>{state.feedbacks.promptSourceRevision=version;}',source.revision(source.raw(app.feedbacks_ai_block.prompt_store.path)))
                with (patch.object(web,'_public_business_data_write_barrier_status',return_value=dict(active=True,status='held',phase='held',window_id='synthetic',window_kind='deploy',hold_confirmed=True,message='synthetic maintenance')),
                      patch.object(app.feedbacks_ai_block,'save_prompt',side_effect=AssertionError('no dispatch under maintenance'))):
                    code=page.evaluate('async()=>{try{await onceAnalysisSettings({prompt:"Blocked",model:"gpt-5.5"},false);}catch(error){return error.code;}}')
                self.assertEqual(code,'business_data_maintenance');self.assertFalse(page.evaluate('()=>!!localStorage.getItem(analysisSettingsFenceKey())'))
                self.assertEqual(app.feedbacks_ai_block.prompt_store.path.read_bytes(),original)
            finally:browser.close()


    def test_actual_queued_prompt_preserves_clicked_revision_operands_and_DOM_readback(self):
        from apps.operator_feedback_forms_browser_smoke import native_server
        from packages.adapters import registry_upload_http_entrypoint as web
        with native_server() as (base,app,f,job),sync_playwright() as pw:
            app.feedbacks_ai_block.provider=FakeAiProvider();browser=pw.chromium.launch();page=browser.new_page();posts=[];errors=[]
            try:
                def routes(route):
                    path=route.request.url[len(base):].split('?')[0]
                    if path==source.PATH and route.request.method=='POST':
                        response=route.fetch();posts.append((route.request.post_data_json,response.status,response.json()));route.fulfill(response=response)
                    elif path==source.PATH or path.startswith('/v1/sheet-vitrina-v1/operations/') or path==web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH or path.endswith(('.js','.css')):route.continue_()
                    else:route.fulfill(status=200,content_type='application/json',body='{}')
                page.on('pageerror',lambda error:errors.append(str(error)));page.route(base+'/**',routes)
                page.goto(base+web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH+'?tab=feedbacks')
                page.locator('[data-feedbacks-subtab="prompt"]').click()
                page.wait_for_function('()=>state.feedbacks.promptLoaded && !state.feedbacks.promptLoading')
                clicked_revision=page.evaluate('state.feedbacks.promptSourceRevision')
                page.evaluate('''()=>{window.held=false;window.blocker=navigator.locks.request(analysisSettingsFenceKey(),()=>new Promise(resolve=>{window.held=true;window.releaseLock=resolve;}));}''')
                page.wait_for_function('window.held')
                page.evaluate('''()=>{window.clicked={prompt:"Clicked instruction",model:"gpt-5.5"};window.pending=onceAnalysisSettings(window.clicked,false).catch(error=>({error:error.code}));}''')
                app.feedbacks_ai_block.save_prompt(dict(prompt='Another native editor',model='gpt-5.5'))
                late_revision=source.revision(source.raw(app.feedbacks_ai_block.prompt_store.path))
                page.evaluate('''version=>{window.clicked.prompt="Late caller value";window.clicked.model="gpt-5-mini";state.feedbacks.promptSourceRevision=version;window.releaseLock();}''',late_revision)
                result=page.evaluate('async()=>await window.pending')
                self.assertEqual(result['error'],'analysis_settings_revision_stale');self.assertEqual(len(posts),1)
                self.assertEqual(posts[0][0]['prompt'],'Clicked instruction');self.assertEqual(posts[0][0]['model'],'gpt-5.5')
                self.assertEqual(posts[0][0]['expected_source_revision'],clicked_revision);self.assertEqual(posts[0][1],409)
                self.assertEqual(source.raw(app.feedbacks_ai_block.prompt_store.path)['prompt'],'Another native editor')
                self.assertEqual(len(source.records(source.raw(app.feedbacks_ai_block.prompt_store.path))),0)
                self.assertIsNone(page.evaluate('localStorage.getItem(analysisSettingsFenceKey())'))
                page.evaluate('async()=>await loadFeedbacksPrompt()')
                page.locator('[data-feedbacks-model]').select_option('gpt-5.5')
                page.locator('[data-feedbacks-prompt-textarea]').fill('Confirmed DOM instruction')
                page.locator('[data-feedbacks-prompt-save]').click()
                page.wait_for_selector('#operator-analysis-settings-receipt [data-ff-operation-receipt]')
                self.assertEqual(len(posts),2);receipt=posts[1][2]['acceptance']
                self.assertEqual(posts[1][0]['expected_source_revision'],late_revision)
                self.assertEqual(receipt['saved_source']['prompt'],'Confirmed DOM instruction');self.assertFalse(receipt['external_confirmed'])
                self.assertEqual(page.locator('#operator-analysis-settings-receipt .ff-operation-status').inner_text(),'Сохранено')
                page.locator('#operator-analysis-settings-receipt button').filter(has_text='Закрыть').click()
                page.wait_for_function('()=>!state.feedbacks.promptLoading && !state.feedbacks.promptSaving')
                self.assertEqual(page.locator('[data-feedbacks-prompt-textarea]').input_value(),receipt['saved_source']['prompt'])
                self.assertEqual(source.raw(app.feedbacks_ai_block.prompt_store.path)['prompt'],receipt['saved_source']['prompt'])
                # Without a real actor lock, a new command cannot be submitted.
                before=app.feedbacks_ai_block.prompt_store.path.read_bytes()
                page.evaluate('Object.defineProperty(navigator,"locks",{value:undefined})')
                page.locator('[data-feedbacks-prompt-textarea]').fill('Must not send')
                page.locator('[data-feedbacks-prompt-save]').click()
                page.wait_for_function('()=>!state.feedbacks.promptSaving')
                self.assertEqual(len(posts),2);self.assertEqual(app.feedbacks_ai_block.prompt_store.path.read_bytes(),before)
                self.assertFalse(f.transport.write_calls);self.assertFalse(app.feedbacks_ai_block.provider.calls);self.assertFalse(errors,errors)
            finally:browser.close()


if __name__=='__main__':unittest.main(verbosity=2)
