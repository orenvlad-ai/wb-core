#!/usr/bin/env python3
"""Actual native TX/CAS/audit, HTTP and forms; temporary stores/fake lifecycle only."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import os
import sqlite3
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from packages.application import operator_autoanswers_settings as source,operator_operations as journal
from packages.application.wb_autoanswers_runtime import AutoanswersRepository,AutoanswersRuntimeError,autoanswers_settings_revision
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.change_registry_observer import ChangeRegistryReadSurface
from apps.wb_autoanswers_http_ui_test import FakeAutoanswersLifecycle
from apps.wb_autoanswers_runtime_test import MutableClock,feedback


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.runtime=Path(self.temp.name);self.repo=AutoanswersRepository(runtime_dir=self.runtime,now_factory=MutableClock(),env={})
        self.app=RegistryUploadHttpEntrypoint(self.runtime,autoanswers_repository=self.repo,
            change_registry_read_surface=ChangeRegistryReadSurface(self.runtime,seller_id='fixture'))
        (self.runtime/'.auto-updates-policy.json').write_text(json.dumps(dict(master_desired=True,revision=1)))
        self.app.autoanswers_lifecycle=FakeAutoanswersLifecycle(self.repo)
        self.scope=source.SettingsSurface(self.runtime,'fixture','operator')
    def body(self,identity='ai-settings:fixture_identity_0001',**change):
        return dict(operation_id=identity,expected_policy_epoch=self.repo.settings().policy_epoch,**change)
    def save(self,body,actor='operator'):
        return self.app.handle_sheet_feedbacks_autoanswers_settings_update_request(body,actor_id=actor)
    def read(self,identity='ai-settings:fixture_identity_0001',scope=None,allowed=None):
        return journal.read_acceptance(self.app.runtime.db_path,identity,allowed_domains={source.DOMAIN} if allowed is None else allowed,ai_settings_scope=scope or self.scope)
    def listing(self,scope=None,**kw):
        return journal.journal(self.app.runtime.db_path,allowed_domains={source.DOMAIN},ai_settings_scope=scope or self.scope,**kw)

    def test_native_limits_immutable_original_revision_ABA_and_duplicate_before_CAS(self):
        old=self.repo.settings()
        body=self.body(daily_cap_usd=6,expected_settings_revision=autoanswers_settings_revision(old))
        first=self.save(body)['acceptance'];self.assertEqual(first['state'],'completed');self.assertFalse(first['external_confirmed'])
        self.assertEqual(first['saved_settings']['daily_cap_usd'],6);self.assertEqual(self.read(),first)
        self.repo.now_factory.advance(1)
        second=self.body('ai-settings:fixture_identity_0002',daily_cap_usd=8,expected_settings_revision=autoanswers_settings_revision(self.repo.settings()))
        self.save(second)
        self.repo.now_factory.advance(1)
        third=self.body('ai-settings:fixture_identity_0003',daily_cap_usd=6,expected_settings_revision=autoanswers_settings_revision(self.repo.settings()))
        self.save(third)
        current=autoanswers_settings_revision(self.repo.settings());self.assertNotEqual(current,first['source_ref']['settings_revision']);calls=self.app.autoanswers_lifecycle.reconcile_calls
        result=self.save(body);self.assertTrue(result['replayed']);self.assertEqual(result['acceptance'],first)
        self.assertEqual(autoanswers_settings_revision(self.repo.settings()),current);self.assertEqual(self.app.autoanswers_lifecycle.reconcile_calls,calls)
        with self.assertRaisesRegex(ValueError,'identity_conflict'):self.save(dict(body,daily_cap_usd=9))
        with self.assertRaisesRegex(ValueError,'identity_conflict'):self.save(body,actor='foreign')
        with self.repo.transaction() as conn:
            with self.assertRaises(sqlite3.IntegrityError):conn.execute('DELETE FROM '+source.AUDIT+' WHERE event_id=?',(body['operation_id'],))
            with self.assertRaises(sqlite3.IntegrityError):conn.execute('UPDATE '+source.AUDIT+' SET actor_id=? WHERE event_id=?',('foreign',body['operation_id']))
        self.assertEqual(self.listing()['total'],3)
        missing_revision=self.body('ai-settings:fixture_identity_0004',daily_cap_usd=9)
        with self.assertRaisesRegex(ValueError,'revision_required'):self.save(missing_revision)
        command=source.command(dict(missing_revision,expected_settings_revision=current),actor='operator',account='fixture')
        with self.assertRaisesRegex(ValueError,'native_operand_mismatch'):
            self.repo.update_settings(daily_cap_usd=9,expected_policy_epoch=self.repo.settings().policy_epoch,
                actor_id='operator',operator_command=command)
        self.assertEqual(self.listing()['total'],3)

    def test_native_audit_failure_rolls_back_settings_and_legacy_has_no_typed_receipt(self):
        old=self.repo.settings();body=self.body(selector_state='manual')
        with patch.object(source,'record',side_effect=RuntimeError('proof commit failure')):
            with self.assertRaisesRegex(RuntimeError,'proof commit failure'):self.save(body)
        self.assertEqual(self.repo.settings(),old);self.assertIsNone(self.read());self.assertEqual(self.app.autoanswers_lifecycle.reconcile_calls,0)
        ordinary=self.save(dict(selector_state='manual',expected_policy_epoch=old.policy_epoch))
        self.assertNotIn('acceptance',ordinary);self.assertEqual(self.listing()['total'],0)
        with self.assertRaisesRegex(ValueError,'native_operand_mismatch'):
            self.repo.update_settings(master_enabled=False,expected_policy_epoch=old.policy_epoch,actor_id='operator',
                operator_command=source.command(body,actor='operator',account='fixture'))

    def test_native_lifecycle_failed_after_commit_exact_read_never_reconciles_again_and_OFF(self):
        body=self.body(selector_state='manual')
        with patch.object(self.app.autoanswers_lifecycle,'reconcile',side_effect=RuntimeError('lost native lifecycle readback')) as reconcile:
            saved=self.save(body);self.assertEqual(saved['acceptance']['state'],'needs_attention')
            receipt=self.read();self.assertEqual(receipt['state'],'needs_attention');self.assertTrue(receipt['execution_required'])
            self.assertFalse(receipt['external_confirmed']);self.assertEqual(reconcile.call_count,1)
            self.assertTrue(self.save(body)['replayed']);self.assertEqual(reconcile.call_count,1)
        # An uncertain master ON result must not block a new explicit OFF source
        # CAS. The existing native stop reconciliation remains separate.
        normal=self.app._autoanswers_master_suspension
        def master(*,require_confirmed=False):
            if require_confirmed:raise AutoanswersRuntimeError('master unknown',code='master_readback_unconfirmed')
            return normal(require_confirmed=False)
        off=self.body('ai-settings:fixture_identity_0002',selector_state='off')
        with patch.object(self.app,'_autoanswers_master_suspension',side_effect=master):
            result=self.save(off)
        self.assertFalse(self.repo.settings().master_enabled);self.assertEqual(result['acceptance']['state'],'accepted')
        with self.assertRaises(AutoanswersRuntimeError):self.save(dict(off,operation_id='ai-settings:fixture_identity_0003',expected_policy_epoch=0))

    def test_native_same_identity_concurrent_facade_precheck_cannot_repeat_lifecycle(self):
        body=self.body(selector_state='manual');first=self.save(body);calls=self.app.autoanswers_lifecycle.reconcile_calls
        original=source.read;reads=[]
        def raced(app,cmd):
            reads.append(1)
            return None if len(reads)==1 else original(app,cmd)
        with patch.object(source,'read',side_effect=raced):second=self.save(body)
        self.assertTrue(second['replayed']);self.assertEqual(second['acceptance'],first['acceptance']);self.assertEqual(self.app.autoanswers_lifecycle.reconcile_calls,calls)

    def test_native_preview_exact_sweep_consumed_new_identity_rejects_before_effect(self):
        self.repo.upsert_feedback(feedback('one'),source_stream='backfill',run_kind='backfill')
        preview=self.repo.preview_mode_transition('draft_only',actor_id='operator',scope_from='2026-01-01',run_max_usd='0.50')
        body=self.body(selector_state='draft_only',preview_id=preview['preview_id'])
        first=self.save(body)['acceptance'];self.assertEqual(first['state'],'accepted')
        self.assertEqual(first['source_ref']['preview_id'],preview['preview_id']);self.assertTrue(first['source_ref']['sweep_id'])
        self.assertEqual(first['source_ref']['sweep_id'],first['source_ref']['transition_run_id'])
        current=self.repo.settings();new=dict(body,operation_id='ai-settings:fixture_identity_0002',expected_policy_epoch=current.policy_epoch)
        with self.assertRaises(AutoanswersRuntimeError):self.save(new)
        self.assertEqual(self.repo.settings(),current);self.assertEqual(self.listing()['total'],1)
        self.assertTrue(self.save(body)['replayed'])

    def test_native_grants_account_actor_before_counts_search_pages_exact_GET_and_no_bootstrap(self):
        self.save(self.body(selector_state='manual'));identity='ai-settings:fixture_identity_0001'
        with closing(self.repo._connect()) as conn:conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        files={self.repo.db_path:self.repo.db_path.read_bytes()}
        with patch.object(self.repo,'_connect',side_effect=AssertionError('no writable owner GET')),patch.object(AutoanswersRepository,'__init__',side_effect=AssertionError('no bootstrap GET')):
            self.assertEqual(self.listing()['total'],1)
            self.assertEqual(self.listing(search=identity)['total'],1)
            self.assertFalse(self.listing(page=100)['items'])
            for scope in (replace(self.scope,actor='foreign'),replace(self.scope,account='foreign'),replace(self.scope,account_scope='foreign')):
                self.assertEqual(self.listing(scope,search=identity)['total'],0);self.assertIsNone(self.read(scope=scope))
            self.assertIsNone(self.read(allowed=set()));self.assertEqual(journal.journal(self.app.runtime.db_path,allowed_domains=set(),ai_settings_scope=self.scope,search=identity)['total'],0)
        self.assertEqual(files,{self.repo.db_path:self.repo.db_path.read_bytes()})

    def test_existing_v10_database_installs_typed_immutability_additively(self):
        with self.repo.transaction() as conn:
            conn.execute('DROP TRIGGER operator_ai_settings_no_update');conn.execute('DROP TRIGGER operator_ai_settings_no_delete')
            conn.execute('DELETE FROM sheet_vitrina_v1_wb_autoanswers_schema_migrations WHERE version=11')
        before=self.repo.settings();restarted=AutoanswersRepository(runtime_dir=self.runtime,now_factory=MutableClock(),env={})
        self.assertEqual(restarted.settings(),before)
        with closing(restarted._connect()) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'operator_ai_settings_no_%'").fetchone()[0],2)

    def test_native_dated_unit_readback_can_complete_but_empty_flags_cannot(self):
        from apps.wb_autoanswers_lifecycle_test import FakeSystemd
        from packages.application.wb_autoanswers_lifecycle import AutoanswersLifecycle
        units=FakeSystemd()
        self.app.autoanswers_lifecycle=AutoanswersLifecycle(runtime_dir=self.runtime,repository=self.repo,
            systemd=units,now_factory=self.repo.now_factory)
        body=self.body(selector_state='off')
        first=self.save(body)['acceptance'];self.assertTrue(first['execution_confirmed']);self.assertEqual(first['state'],'completed')
        self.assertFalse(first['external_confirmed']);self.assertTrue(first['execution_observed_at'])
        calls=list(units.calls)
        self.assertTrue(self.save(body)['replayed']);self.assertEqual(units.calls,calls)
        # Later legitimate lifecycle changes never change the dated command.
        self.repo.update_settings(master_enabled=True,mode='manual',actor_id='operator')
        self.assertEqual(self.read(),first)
        # The old immutable observation cannot be replaced with a success flag.
        with self.repo.transaction() as conn:
            with self.assertRaises(sqlite3.IntegrityError):conn.execute("UPDATE "+source.AUDIT+" SET details_json='{}' WHERE aggregate_type='operator_settings_execution'")
        current=self.repo.settings()
        self.assertFalse(source.execution_proven(dict(policy_epoch=current.policy_epoch,business_mode='manual',
            readback_captured_at='2026-07-20T12:00:00Z',lifecycle_state='running',drift_status='matched'),current,{}))

    def test_actual_authenticated_HTTP_source_grants_before_count_search_detail(self):
        from apps.operator_feedback_forms_browser_smoke import native_server
        from apps.registry_upload_http_entrypoint_auth_smoke import _password_hash
        from packages.adapters import registry_upload_http_entrypoint as web
        import urllib.request,urllib.parse,urllib.error,http.cookiejar
        password='synthetic-ai-grant-only'
        env=dict(WB_CORE_WEB_AUTH_REQUIRED='1',WB_CORE_WEB_AUTH_USERNAME='owner',
            WB_CORE_WEB_AUTH_PASSWORD_HASH=_password_hash(password),WB_CORE_WEB_AUTH_SESSION_SECRET='synthetic-ai-receipt-auth-only')
        with patch.dict(os.environ,env),native_server() as (base,app,f,job):
            (app.runtime.runtime_dir/'.auto-updates-policy.json').write_text(json.dumps(dict(master_desired=True,revision=1)))
            for username,sections in [('reader',['feedbacks']),('foreign',['feedbacks',web.WEB_AUTH_PERMISSION_FEEDBACKS_AUTOANSWERS_ADMIN])]:
                app.handle_sheet_vitrina_user_create_request(dict(user_id='fixture-'+username,username=username,display_name=username,
                    role='operator',allowed_sections=sections,manage_users=False,password_hash=_password_hash(password),is_active=True,
                    created_at='2026-07-20T12:00:00Z',updated_at='2026-07-20T12:00:00Z'))
            def login(username):
                opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                request=urllib.request.Request(base+'/login',data=urllib.parse.urlencode(dict(username=username,password=password,next='/sheet-vitrina-v1/operations')).encode(),headers={'Content-Type':'application/x-www-form-urlencoded'})
                with opener.open(request) as response:response.read()
                return opener
            def get(opener,path):
                try:response=opener.open(base+path)
                except urllib.error.HTTPError as exc:response=exc
                with response:return response.status,json.load(response)
            owner=login('owner');body=dict(operation_id='ai-settings:fixture_http_identity_0001',selector_state='manual',expected_policy_epoch=f.repo.settings().policy_epoch)
            request=urllib.request.Request(base+source.PATH,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','X-WB-Autoanswers-CSRF':'1'})
            with owner.open(request) as response:receipt=json.load(response)['acceptance']
            self.assertEqual(receipt['actor'],'owner')
            for actor in ('reader','foreign'):
                opener=login(actor)
                status,payload=get(opener,'/v1/sheet-vitrina-v1/operations?domain='+source.DOMAIN+'&search='+body['operation_id'])
                self.assertEqual(status,200,payload);self.assertEqual(payload['total'],0)
                status,_=get(opener,'/v1/sheet-vitrina-v1/operations/'+body['operation_id']);self.assertEqual(status,404)
            status,payload=get(owner,'/v1/sheet-vitrina-v1/operations/'+body['operation_id'])
            self.assertEqual(status,200);self.assertEqual(payload['operation'],receipt)


class BrowserTests(unittest.TestCase):
    def test_actual_native_HTTP_form_lost_response_two_tabs_reload_GET_ONLY_AND_OFF(self):
        from apps.operator_feedback_forms_browser_smoke import native_server
        from packages.adapters import registry_upload_http_entrypoint as web
        from playwright.sync_api import sync_playwright
        with native_server() as (base,app,f,job),sync_playwright() as pw:
            (app.runtime.runtime_dir/'.auto-updates-policy.json').write_text(json.dumps(dict(master_desired=True,revision=1)))
            browser=pw.chromium.launch()
            context=browser.new_context();page=context.new_page();other=context.new_page()
            posts=[];reads=[];lost={'get':True};errors=[];held={'route':None}
            def route_all(route):
                path=route.request.url[len(base):].split('?')[0]
                if path.startswith('/v1/sheet-vitrina-v1/operations/'):
                    reads.append(path)
                    if lost['get']:route.abort('failed')
                    else:route.continue_()
                elif path==web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH or path==source.PATH or path.endswith('.js') or path.endswith('.css'):route.continue_()
                else:route.fulfill(status=200,content_type='application/json',body='{}')
            context.route(base+'/**',route_all)
            def post_lost(route):
                if route.request.method!='POST':route.continue_();return
                posts.append(route.request.post_data_json)
                response=route.fetch()
                self.assertEqual(response.status,200,response.text())
                if len(posts)==1:held['route']=route
                else:route.abort('failed')
            context.route('**/autoanswers/settings',post_lost)
            for tab in (page,other):
                tab.on('pageerror',lambda error:errors.append(str(error)))
                tab.goto(base+web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH+'?tab=settings')
                tab.evaluate('async()=>{await loadAutoanswersSettings();}')
            # Actual main form starts one exact native command; lost POST and
            # lost proof keep the fence. The second tab cannot repeat submission.
            page.evaluate('()=>{window.aiPending=updateAutoanswersSettings({selector_state:"manual"});}')
            page.wait_for_function('localStorage.getItem(aiSettingsFenceKey("selector"))!==null')
            other.evaluate('()=>{window.aiPending=updateAutoanswersSettings({selector_state:"manual"});}')
            other.wait_for_timeout(100)
            self.assertEqual(len(posts),1);self.assertIsNotNone(held['route'])
            held['route'].abort('failed')
            page.evaluate('async()=>await window.aiPending');other.evaluate('async()=>await window.aiPending')
            self.assertEqual(len(posts),1);self.assertTrue(f.repo.settings().master_enabled)
            self.assertEqual(app.autoanswers_lifecycle.reconcile_calls,1)
            lost['get']=False
            # OFF is a separate native command with fresh epoch; uncertain ON
            # is retained and does not block the explicit stop action.
            page.evaluate('async()=>await updateAutoanswersSettings({selector_state:"off"})')
            self.assertEqual(len(posts),2);self.assertFalse(f.repo.settings().master_enabled)
            self.assertEqual(posts[1]['expected_policy_epoch'],posts[0]['expected_policy_epoch'])
            page.locator('#operator-ai-settings-receipt').evaluate('node=>node.close()')
            page.reload();page.evaluate('async()=>await loadAutoanswersSettings()')
            self.assertEqual(len(posts),2);self.assertEqual(app.autoanswers_lifecycle.reconcile_calls,2)
            self.assertIsNone(page.evaluate('localStorage.getItem(aiSettingsFenceKey("selector"))'))
            self.assertEqual(page.locator('#operator-ai-settings-receipt .ff-operation-status').inner_text(),'Ожидает обработки')
            self.assertTrue(reads);self.assertFalse(f.transport.write_calls);self.assertFalse(errors,errors)
            # Exact source grant is required before all common journal reads.
            scope=source.SettingsSurface.from_entrypoint(app,actor='local_operator')
            self.assertEqual(journal.journal(app.runtime.db_path,allowed_domains={source.DOMAIN},ai_settings_scope=scope)['total'],2)
            browser.close()

    def test_actual_maintenance_refusal_has_no_source_and_clears_only_its_fence(self):
        from apps.operator_feedback_forms_browser_smoke import native_server
        from packages.adapters import registry_upload_http_entrypoint as web
        from packages.application.business_data_write_barrier import acquire_barrier
        from playwright.sync_api import sync_playwright
        with native_server() as (base,app,f,job),sync_playwright() as pw:
            (app.runtime.runtime_dir/'.auto-updates-policy.json').write_text(json.dumps(dict(master_desired=True,revision=1)))
            browser=pw.chromium.launch();page=browser.new_page();posts=[]
            def routes(route):
                path=route.request.url[len(base):].split('?')[0]
                if path==source.PATH and route.request.method=='POST':
                    response=route.fetch();posts.append((response.status,response.json()))
                    route.fulfill(response=response)
                elif path.startswith('/v1/sheet-vitrina-v1/operations/') or path in {web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH,source.PATH} or path.endswith('.css') or path.endswith('.js'):route.continue_()
                else:route.fulfill(status=200,content_type='application/json',body='{}')
            page.route(base+'/**',routes)
            page.goto(base+web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH+'?tab=settings')
            page.evaluate('async()=>await loadAutoanswersSettings()')
            before=app.autoanswers_repository.settings();calls=app.autoanswers_lifecycle.reconcile_calls
            # Maintenance starts after the open form was loaded: actual native
            # admission must reject before the settings writer is reached.
            acquire_barrier(app.runtime.runtime_dir,window_id='ai-refusal',window_kind='snapshot',plan_fingerprint='sha256:'+'a'*64,approval_reference='synthetic',actor='fixture',reason='synthetic maintenance')
            page.evaluate('async()=>await updateAutoanswersSettings({selector_state:"manual"})')
            self.assertEqual(len(posts),1);self.assertEqual(posts[0][0],423)
            self.assertEqual(posts[0][1]['code'],'business_data_maintenance')
            self.assertIsNone(page.evaluate('localStorage.getItem(aiSettingsFenceKey("selector"))'))
            self.assertEqual(app.autoanswers_repository.settings(),before)
            self.assertEqual(app.autoanswers_lifecycle.reconcile_calls,calls)
            scope=source.SettingsSurface.from_entrypoint(app,actor='local_operator')
            self.assertEqual(journal.journal(app.runtime.db_path,allowed_domains={source.DOMAIN},ai_settings_scope=scope)['total'],0)
            self.assertFalse(f.transport.write_calls)
            browser.close()

    def test_actual_held_native_POST_unknown400_failed_GET_retains_ID_across_tabs_reload(self):
        from apps.operator_feedback_forms_browser_smoke import native_server
        from packages.adapters import registry_upload_http_entrypoint as web
        from playwright.sync_api import sync_playwright
        with native_server() as (base,app,f,job),sync_playwright() as pw:
            (app.runtime.runtime_dir/'.auto-updates-policy.json').write_text(json.dumps(dict(master_desired=True,revision=1)))
            browser=pw.chromium.launch();context=browser.new_context();page=context.new_page();other=context.new_page()
            posts=[];reads=[];held={'route':None};failed={'GET':True}
            def routes(route):
                path=route.request.url[len(base):].split('?')[0]
                if path.startswith('/v1/sheet-vitrina-v1/operations/'):
                    reads.append(path)
                    if failed['GET']:route.abort('failed')
                    else:route.continue_()
                elif path in {web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH,source.PATH} or path.endswith('.css') or path.endswith('.js'):route.continue_()
                else:route.fulfill(status=200,content_type='application/json',body='{}')
            context.route(base+'/**',routes)
            def committed_then_unknown(route):
                if route.request.method!='POST':route.continue_();return
                posts.append(route.request.post_data_json)
                response=route.fetch()
                self.assertEqual(response.status,200,response.text())
                self.assertEqual(response.json()['acceptance']['operation_id'],posts[-1]['operation_id'])
                # The source is an actual native commit. Hold its response
                # until another tab waits; then simulate an unknown 400 at
                # the response/projection boundary. No fabricated acceptance.
                held['route']=route
            context.route('**/autoanswers/settings',committed_then_unknown)
            for tab in (page,other):
                tab.goto(base+web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH+'?tab=settings')
                tab.evaluate('async()=>await loadAutoanswersSettings()')
            page.evaluate('()=>{window.aiUnknown=updateAutoanswersSettings({selector_state:"manual"});}')
            page.wait_for_function('localStorage.getItem(aiSettingsFenceKey("selector"))!==null')
            other.evaluate('()=>{window.aiUnknown=updateAutoanswersSettings({selector_state:"manual"});}')
            other.wait_for_timeout(100)
            self.assertEqual(len(posts),1);self.assertIsNotNone(held['route'])
            identity=posts[0]['operation_id'];calls=app.autoanswers_lifecycle.reconcile_calls
            held['route'].fulfill(status=400,content_type='application/json',body=json.dumps(dict(error='postcommit projection unknown',code='unknown_native_readback')))
            page.evaluate('async()=>await window.aiUnknown');other.evaluate('async()=>await window.aiUnknown')
            self.assertEqual(len(posts),1)
            for tab in (page,other):
                self.assertEqual(tab.evaluate('JSON.parse(localStorage.getItem(aiSettingsFenceKey("selector"))).identity'),identity)
            # Repeated user action and reload are still exact GET-only when
            # the receipt read is lost. A new operation must not be admitted.
            other.evaluate('async()=>await updateAutoanswersSettings({selector_state:"manual"})')
            page.reload();page.evaluate('async()=>await loadAutoanswersSettings()')
            self.assertEqual(len(posts),1);self.assertEqual(app.autoanswers_lifecycle.reconcile_calls,calls)
            self.assertEqual(page.evaluate('JSON.parse(localStorage.getItem(aiSettingsFenceKey("selector"))).identity'),identity)
            # Positive allowlist cases, with exact status binding. Unknown
            # 4xx/projection codes and a valid code at the wrong status fail.
            proof=page.evaluate('''()=>({cas:aiSettingsPrecommitRejected({httpStatus:409,code:"policy_epoch_stale"}),
                auth:aiSettingsPrecommitRejected({httpStatus:401,code:"authentication_required"}),
                unknown:aiSettingsPrecommitRejected({httpStatus:400,code:"unknown_native_readback"}),
                projection:aiSettingsPrecommitRejected({httpStatus:422,code:"operator_ai_settings_source_proof_invalid"}),
                wrongStatus:aiSettingsPrecommitRejected({httpStatus:400,code:"policy_epoch_stale"})})''')
            self.assertEqual(proof,dict(cas=True,auth=True,unknown=False,projection=False,wrongStatus=False))
            failed['GET']=False
            page.evaluate('async()=>await loadAutoanswersSettings()')
            self.assertIsNone(page.evaluate('localStorage.getItem(aiSettingsFenceKey("selector"))'))
            self.assertEqual(page.locator('#operator-ai-settings-receipt [data-ff-operation-receipt]').count(),1)
            self.assertEqual(len(posts),1);self.assertEqual(app.autoanswers_lifecycle.reconcile_calls,calls)
            scope=source.SettingsSurface.from_entrypoint(app,actor='local_operator')
            self.assertEqual(journal.journal(app.runtime.db_path,allowed_domains={source.DOMAIN},ai_settings_scope=scope)['total'],1)
            self.assertTrue(reads);self.assertFalse(f.transport.write_calls)
            browser.close()

    def test_actual_queued_admission_preserves_clicked_operands_epoch_and_limits_revision(self):
        from apps.operator_feedback_forms_browser_smoke import native_server
        from packages.adapters import registry_upload_http_entrypoint as web
        from playwright.sync_api import sync_playwright
        with native_server() as (base,app,f,job),sync_playwright() as pw:
            (app.runtime.runtime_dir/'.auto-updates-policy.json').write_text(json.dumps(dict(master_desired=True,revision=1)))
            browser=pw.chromium.launch();page=browser.new_page();posts=[];errors=[]
            def routes(route):
                path=route.request.url[len(base):].split('?')[0]
                if path==source.PATH and route.request.method=='POST':
                    response=route.fetch();posts.append((route.request.post_data_json,response.status,response.json()))
                    route.fulfill(response=response)
                elif path.startswith('/v1/sheet-vitrina-v1/operations/') or path in {web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH,source.PATH} or path.endswith('.css') or path.endswith('.js'):route.continue_()
                else:route.fulfill(status=200,content_type='application/json',body='{}')
            page.on('pageerror',lambda error:errors.append(str(error)));page.route(base+'/**',routes)
            page.goto(base+web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH+'?tab=settings')
            page.evaluate('async()=>await loadAutoanswersSettings()')
            clicked=f.repo.settings();revision=autoanswers_settings_revision(clicked)
            page.evaluate('''()=>{window.lockHeld=false;window.blocker=navigator.locks.request(aiSettingsFenceKey("limits"),()=>new Promise(resolve=>{window.lockHeld=true;window.releaseLock=resolve;}));}''')
            page.wait_for_function('window.lockHeld')
            page.evaluate('''()=>{window.clickedChange={daily_cap_usd:6};window.clickedCurrent=state.feedbacks.server.settings;window.queuedAi=onceAiSettings(window.clickedChange,window.clickedCurrent).catch(error=>({error:error.code}));}''')
            # Another native editor changed the same source while admission waits.
            # Later GUI/source revision must not bless the old confirmed value.
            f.repo.update_settings(daily_cap_usd=8,actor_id='other-native-editor')
            late=autoanswers_settings_revision(f.repo.settings())
            page.evaluate('''revision=>{window.clickedChange.daily_cap_usd=99;window.clickedCurrent.policy_epoch=999;state.feedbacks.server.settingsRevision=revision;window.releaseLock();}''',late)
            result=page.evaluate('async()=>await window.queuedAi')
            self.assertEqual(result['error'],'settings_revision_stale');self.assertEqual(len(posts),1)
            self.assertEqual(posts[0][0]['daily_cap_usd'],6);self.assertEqual(posts[0][0]['expected_settings_revision'],revision)
            self.assertEqual(posts[0][0]['expected_policy_epoch'],clicked.policy_epoch);self.assertEqual(posts[0][1],409)
            self.assertEqual(f.repo.settings().daily_cap_usd,8);self.assertIsNone(page.evaluate('localStorage.getItem(aiSettingsFenceKey("limits"))'))
            self.assertEqual(app.autoanswers_lifecycle.reconcile_calls,0)
            page.evaluate('async()=>await loadAutoanswersSettings()')
            epoch=f.repo.settings().policy_epoch
            page.evaluate('''()=>{window.lockHeld=false;window.blocker=navigator.locks.request(aiSettingsFenceKey("selector"),()=>new Promise(resolve=>{window.lockHeld=true;window.releaseLock=resolve;}));}''')
            page.wait_for_function('window.lockHeld')
            page.evaluate('''()=>{window.clickedChange={selector_state:"manual"};window.clickedCurrent=state.feedbacks.server.settings;window.queuedAi=onceAiSettings(window.clickedChange,window.clickedCurrent);}''')
            page.evaluate('''()=>{window.clickedChange.selector_state="off";window.clickedCurrent.policy_epoch=999;window.releaseLock();}''')
            result=page.evaluate('async()=>await window.queuedAi')
            self.assertEqual(len(posts),2);self.assertEqual(posts[1][0]['selector_state'],'manual');self.assertEqual(posts[1][0]['expected_policy_epoch'],epoch)
            self.assertEqual(posts[1][1],200);self.assertEqual(result['acceptance']['request']['selector_state'],'manual')
            self.assertTrue(f.repo.settings().master_enabled);self.assertEqual(app.autoanswers_lifecycle.reconcile_calls,1)
            scope=source.SettingsSurface.from_entrypoint(app,actor='local_operator')
            self.assertEqual(journal.journal(app.runtime.db_path,allowed_domains={source.DOMAIN},ai_settings_scope=scope)['total'],1)
            self.assertFalse(f.transport.write_calls);self.assertFalse(errors,errors)
            browser.close()

    def test_actual_form_missing_weblock_fails_before_submit(self):
        from apps.operator_feedback_forms_browser_smoke import native_server
        from packages.adapters import registry_upload_http_entrypoint as web
        from playwright.sync_api import sync_playwright
        with native_server() as (base,app,f,job),sync_playwright() as pw:
            browser=pw.chromium.launch();page=browser.new_page();posts=[]
            def routes(route):
                path=route.request.url[len(base):].split('?')[0]
                if route.request.method=='POST':posts.append(path);route.abort('failed')
                elif path in {web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH,source.PATH} or path.endswith('.css') or path.endswith('.js'):route.continue_()
                else:route.fulfill(status=200,content_type='application/json',body='{}')
            initial=f.repo.settings()
            page.route(base+'/**',routes);page.goto(base+web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH+'?tab=settings')
            page.evaluate('async()=>{await loadAutoanswersSettings();Object.defineProperty(navigator,"locks",{value:undefined});await updateAutoanswersSettings({selector_state:"manual"});}')
            self.assertFalse(posts);self.assertEqual(f.repo.settings(),initial)
            self.assertIn('не отправлена',page.evaluate('state.feedbacks.server.error'))
            browser.close()

if __name__=='__main__':unittest.main()
