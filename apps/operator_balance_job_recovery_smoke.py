#!/usr/bin/env python3
"""Actual native Balance/Change Registry + HTTP/forms; entirely synthetic WB.

Fixture authority: the existing live-apply smoke's real immutable calculation,
override, native job TX and FakeLiveAdapter. No WB credentials/provider calls.
"""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import concurrent.futures
import json
import os
import sqlite3
import sys
import threading
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from apps.sku_inventory_balance_live_apply_smoke import _build_runtime, _insert_calculation, _target, _state_target, FakeLiveAdapter, SELLER_ID, ACCOUNT_SCOPE
from packages.application import operator_balance_jobs as source, operator_operations as journal
from packages.application.sku_inventory_balance import SkuInventoryBalanceBlock, SkuInventoryBalanceError


class BalanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.runtime=Path(self.temp.name);self.adapter=FakeLiveAdapter()
        with patch.object(SkuInventoryBalanceBlock,'_start_apply_worker_if_needed'):
            self.block,_,_= _build_runtime(self.runtime,self.adapter)
        self.worker_patch=patch.object(self.block,'_start_apply_worker_if_needed');self.worker=self.worker_patch.start();self.addCleanup(self.worker_patch.stop)
        self.calc=_insert_calculation(self.block,2,'operator_request',targets=[_target(1),_state_target(2)])
        self.scope=source.BalanceScope(self.runtime/'registry_upload_runtime.sqlite3','native-user','human-user',SELLER_ID,ACCOUNT_SCOPE)
    def body(self,identity='balance-apply:fixture_identity_0001'):
        c=self.block.get_calculation(self.calc['calculation_id'])
        targets=[t for r in c['rows'] for t in r['campaign_recommendations']]
        return dict(request_id=identity,calculation_id=c['calculation_id'],apply_source_revision=c['apply_source_revision'],
                    nm_ids=[],target_keys=[targets[0]['target_key']],state_actions=[dict(nm_id=targets[1]['nm_id'],advert_id=targets[1]['advert_id'],action='pause')],mode='live_wb',confirmed=True)
    def start(self,body):return self.block.start_apply(body,actor=self.scope.native_actor,operator_actor=self.scope.actor)
    def read(self,identity='balance-apply:fixture_identity_0001',scope=None):
        return source.read(self.block.runtime.db_path,scope=scope or self.scope,request_id=identity,builder=self.block._apply_job_payload)
    def count(self):
        with sqlite3.connect(self.block.runtime.db_path) as conn:return conn.execute(f'SELECT count(*) FROM {source.TABLE}').fetchone()[0]

    def test_postcommit_worker_failure_and_retry_before_rebuild_live_checks_READONLY(self):
        body=self.body()
        self.worker.side_effect=OSError('synthetic native worker acknowledgment lost')
        with self.assertRaisesRegex(OSError,'acknowledgment lost'):self.start(body)
        job=self.read();self.assertTrue(job['acceptance']['durable_saved']);self.assertFalse(job['acceptance']['external_confirmed'])
        self.assertEqual(job['acceptance']['state'],'accepted');self.assertEqual(self.count(),1)
        self.adapter.external_writes_enabled=False
        with patch.object(self.block,'get_calculation',side_effect=AssertionError('no rebuild')),patch.object(self.block,'_connect',side_effect=AssertionError('no writable read')):
            self.assertEqual(self.start(body),job)
            self.assertEqual(self.block.get_apply_job('',actor='native-user',operator_actor='human-user',request_id=body['request_id']),job)
        self.assertEqual(self.worker.call_count,1);self.assertFalse(self.adapter.submit_attempts)

    def test_restart_same_request_reads_retained_job_without_second_admission(self):
        body=self.body();saved=self.start(body)
        with patch.object(SkuInventoryBalanceBlock,'_start_apply_worker_if_needed'):
            restarted,_,_=_build_runtime(self.runtime,self.adapter)
        with patch.object(restarted,'_start_apply_worker_if_needed',side_effect=AssertionError('no retry worker')),patch.object(restarted,'_connect',side_effect=AssertionError('query-only retry')):
            recovered=restarted.get_apply_job('',request_id=body['request_id'],actor=self.scope.native_actor,operator_actor=self.scope.actor)
            self.assertEqual(recovered,saved)
            self.assertEqual(restarted.start_apply(body,actor=self.scope.native_actor,operator_actor=self.scope.actor),saved)
        self.assertEqual(self.count(),1);self.assertFalse(self.adapter.submit_attempts)

    def test_scope_before_count_search_detail_and_identity_payload_conflict(self):
        body=self.body();self.start(body)
        for scope in (replace(self.scope,native_actor='foreign'),replace(self.scope,actor='foreign'),replace(self.scope,seller_id='foreign'),replace(self.scope,account_scope='foreign')):
            if scope.account_scope=='foreign':
                with self.assertRaisesRegex(ValueError,'binding_invalid'):self.read(scope=scope)
                continue
            self.assertIsNone(self.read(scope=scope))
            self.assertEqual(journal.journal(self.block.runtime.db_path,allowed_domains={source.DOMAIN},balance_scope=scope,search='fixture_identity')['total'],0)
            self.assertIsNone(journal.read_acceptance(self.block.runtime.db_path,body['request_id'],allowed_domains={source.DOMAIN},balance_scope=scope))
        with patch.object(source,'public',side_effect=AssertionError('no denied proof reader')):
            self.assertEqual(journal.journal(self.block.runtime.db_path,allowed_domains=set(),balance_scope=self.scope)['total'],0)
        with self.assertRaisesRegex(SkuInventoryBalanceError,'identity.*conflict'):self.start(dict(body,target_keys=[]))
        with self.assertRaisesRegex(SkuInventoryBalanceError,'identity.*conflict'):self.block.start_apply(body,actor='foreign',operator_actor='foreign')
        self.assertEqual(self.count(),1)
        with self.assertRaises(SkuInventoryBalanceError):self.block.get_apply_job(self.read()['job_id'],actor='foreign',operator_actor='foreign')

    def test_revision_ABA_and_CAS_recheck_before_queue(self):
        body=self.body();target=self.calc['rows'][0]['campaign_recommendations'][0]
        original=target['final_target_bid_rub'];self.block.timestamp_factory=lambda:'2026-10-01T00:00:00Z'
        for value in (original+1,original):self.block.save_override(self.calc['calculation_id'],dict(target_key=target['target_key'],manual_target_bid_rub=value),actor='native-user')
        with self.assertRaisesRegex(SkuInventoryBalanceError,'source changed'):self.start(body)
        self.assertEqual(self.count(),0)
        fresh=self.body();native=self.block.get_calculation
        # save_override itself reads calculation: install mutation after that
        # read without recursion to emulate the other writer before BEGIN.
        def race_once(identity):
            value=native(identity)
            with patch.object(self.block,'get_calculation',side_effect=native):
                self.block.save_override(identity,dict(target_key=target['target_key'],manual_target_bid_rub=original+2),actor='native-user')
            return value
        with patch.object(self.block,'get_calculation',side_effect=race_once):
            with self.assertRaisesRegex(SkuInventoryBalanceError,'source changed'):self.start(fresh)
        self.assertEqual(self.count(),0);self.assertFalse(self.adapter.submit_attempts)

    def test_simultaneous_same_request_ONE_native_job_and_one_admission(self):
        body=self.body();barrier=threading.Barrier(2);native=self.block.get_calculation
        def shown(identity):value=native(identity);barrier.wait(10);return value
        with patch.object(self.block,'get_calculation',side_effect=shown),concurrent.futures.ThreadPoolExecutor(2) as pool:
            jobs=list(pool.map(self.start,[body,body]))
        self.assertEqual(jobs[0]['job_id'],jobs[1]['job_id']);self.assertEqual(self.count(),1);self.assertEqual(self.worker.call_count,1)

    def test_native_worker_actual_registry_completion_and_foreign_registry_rejected(self):
        body=self.body();job=self.start(body);claim=self.block._claim_next_live_job();self.block._run_live_job(*claim)
        result=self.read();self.assertEqual(result['acceptance']['state'],'completed')
        self.assertTrue(result['acceptance']['external_confirmed']);self.assertEqual(result['acceptance']['confirmed_count'],2)
        self.assertEqual(len(self.adapter.submit_attempts),1);self.assertEqual(len(self.adapter.state_submit_attempts),1)
        submitted=list(self.adapter.submit_attempts);self.start(body);self.assertEqual(self.adapter.submit_attempts,submitted)
        # Native outcome flags cannot replace immutable registry provenance.
        with sqlite3.connect(self.block.runtime.db_path) as conn:conn.execute(f"UPDATE {source.ITEM_TABLE} SET registry_operation_id='foreign-operation' WHERE job_id=?",(job['job_id'],))
        self.assertEqual(self.read()['acceptance']['state'],'needs_attention');self.assertFalse(self.read()['acceptance']['external_confirmed'])

    def test_completed_job_flags_without_native_readback_are_not_complete(self):
        job=self.start(self.body())
        with sqlite3.connect(self.block.runtime.db_path) as conn:
            conn.execute(f"UPDATE {source.TABLE} SET state='completed' WHERE job_id=?",(job['job_id'],))
            conn.execute(f"UPDATE {source.ITEM_TABLE} SET state='succeeded',result_json=? WHERE job_id=?",(json.dumps(dict(readback_status='matching',confirmed_bid_minor=1)),job['job_id']))
        self.assertEqual(self.read()['acceptance']['state'],'needs_attention');self.assertFalse(self.read()['acceptance']['external_confirmed'])
        current=self.block.get_calculation(self.calc['calculation_id'])
        self.assertEqual(current['rows'][0]['campaign_recommendations'][0]['current_bid_rub'],self.calc['rows'][0]['campaign_recommendations'][0]['current_bid_rub'])

    def test_source_target_retention_and_capacity_BEFORE_worker(self):
        body=self.body()
        with patch.object(source,'MAX_PROOF_BYTES',1):
            with self.assertRaisesRegex(SkuInventoryBalanceError,'capacity'):self.start(body)
        self.assertEqual(self.count(),0);self.assertEqual(self.worker.call_count,0)
        job=self.start(body)
        with sqlite3.connect(self.block.runtime.db_path) as conn:
            for sql in (f"DELETE FROM {source.TABLE}",f"UPDATE {source.TABLE} SET operator_actor='foreign'",f"UPDATE {source.TABLE} SET apply_manifest_json='{{}}'",f"UPDATE {source.TABLE} SET client_request_id=NULL",f"DELETE FROM {source.ITEM_TABLE}",f"UPDATE {source.ITEM_TABLE} SET target_json='{{}}'"):
                with self.assertRaises(sqlite3.IntegrityError):conn.execute(sql)
        self.assertEqual(self.read()['job_id'],job['job_id']);self.assertEqual(self.count(),1)

    def test_legacy_job_without_manifest_receipt_and_typed_exact_selection_fail_before_admission(self):
        body=self.body();body['target_keys'].append('missing-target')
        with self.assertRaisesRegex(SkuInventoryBalanceError,'selected targets'):self.start(body)
        self.assertEqual(self.worker.call_count,0);self.assertEqual(self.count(),0)
        legacy={k:v for k,v in self.body().items() if k not in ('request_id','apply_source_revision')}
        job=self.block.start_apply(legacy,actor='native-user');self.assertNotIn('acceptance',job)
        self.assertEqual(journal.journal(self.block.runtime.db_path,allowed_domains={source.DOMAIN},balance_scope=self.scope)['total'],0)
        with sqlite3.connect(self.block.runtime.db_path) as conn:
            with self.assertRaises(sqlite3.IntegrityError):conn.execute(f'UPDATE {source.TABLE} SET client_request_id=?',(self.body()['request_id'],))

    def test_retained_invalid_proof_is_UNKNOWN_not_precommit_negative_and_foreign_is_invisible(self):
        body=self.body();self.start(body)
        # Synthetic corruption only: do not interpret a malformed already-saved
        # proof's validation/capacity error as evidence that POST did not commit.
        with sqlite3.connect(self.block.runtime.db_path) as conn:
            conn.execute('DROP TRIGGER inventory_balance_typed_job_immutable')
            value=json.loads(conn.execute(f'SELECT operator_proof_json FROM {source.TABLE}').fetchone()[0])
            value['command']['request']['mode']='unsupported'
            conn.execute(f'UPDATE {source.TABLE} SET operator_proof_json=?,operator_proof_digest=?',(json.dumps(value),source.digest(value)))
        self.assertIsNone(self.read(scope=replace(self.scope,actor='foreign')))
        with self.assertRaisesRegex(ValueError,'retained_proof_invalid'):self.start(body)
        self.assertEqual(self.worker.call_count,1);self.assertEqual(self.count(),1)

    def test_actual_authenticated_HTTP_grants_native_identity_and_READONLY_recovery(self):
        from apps.operator_feedback_forms_browser_smoke import native_server
        from apps.registry_upload_http_entrypoint_auth_smoke import _password_hash
        from packages.application.change_registry_observer import ChangeRegistryReadSurface
        import urllib.request,urllib.parse,urllib.error,http.cookiejar
        password='synthetic-Balance-only'
        with patch.dict(os.environ,dict(WB_CORE_WEB_AUTH_REQUIRED='1',WB_CORE_WEB_AUTH_USERNAME='owner',WB_CORE_WEB_AUTH_PASSWORD_HASH=_password_hash(password),WB_CORE_WEB_AUTH_SESSION_SECRET='synthetic-Balance-only')),native_server() as (base,app,f,_):
            adapter=FakeLiveAdapter()
            with patch.object(SkuInventoryBalanceBlock,'_start_apply_worker_if_needed'):
                block,_,_=_build_runtime(Path(app.runtime.runtime_dir),adapter)
            block.runtime.load_sheet_vitrina_user_config=app.runtime.load_sheet_vitrina_user_config
            app.sku_inventory_balance_block=block;app.change_registry_read_surface=ChangeRegistryReadSurface(app.runtime.runtime_dir,seller_id=SELLER_ID)
            calc=_insert_calculation(block,1,'actual_http')
            target=calc['rows'][0]['campaign_recommendations'][0]
            body=dict(request_id='balance-apply:actual_http_identity_001',calculation_id=calc['calculation_id'],apply_source_revision=calc['apply_source_revision'],nm_ids=[],target_keys=[target['target_key']],state_actions=[],mode='live_wb',confirmed=True)
            for name,sections in [('foreign',['sku_management']),('denied',['prices'])]:
                app.handle_sheet_vitrina_user_create_request(dict(user_id='fixture-'+name,username=name,display_name=name,role='operator',allowed_sections=sections,manage_users=False,password_hash=_password_hash(password),is_active=True,created_at='2026-07-20T12:00:00Z',updated_at='2026-07-20T12:00:00Z'))
            def login(name):
                opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                with opener.open(urllib.request.Request(base+'/login',data=urllib.parse.urlencode(dict(username=name,password=password,next='/sheet-vitrina-v1/operations')).encode(),headers={'Content-Type':'application/x-www-form-urlencoded'})) as response:response.read()
                return opener
            def request(opener,path,body=None):
                try:response=opener.open(urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'}))
                except urllib.error.HTTPError as exc:response=exc
                with response:return response.status,json.load(response)
            owner,foreign,denied=(login(n) for n in ('owner','foreign','denied'))
            with patch.object(block,'_start_apply_worker_if_needed') as worker:
                status,job=request(owner,source.PATH,body);self.assertEqual(status,200,job)
                self.assertEqual(job['acceptance']['actor'],'owner');self.assertNotEqual(job['created_by'],'owner')
                status,settings=request(owner,'/v1/sheet-vitrina-v1/sku-management/inventory-balance')
                self.assertEqual(status,200,settings);self.assertEqual(settings['settings']['operator_scope'],job['created_by'])
                status,foreign_settings=request(foreign,'/v1/sheet-vitrina-v1/sku-management/inventory-balance')
                self.assertEqual(status,200,foreign_settings);self.assertNotEqual(foreign_settings['settings']['operator_scope'],settings['settings']['operator_scope'])
                self.assertEqual(worker.call_count,1)
                for opener,status in ((foreign,404),(denied,403)):
                    self.assertEqual(request(opener,source.PATH+'?request_id='+body['request_id'])[0],status)
                    self.assertEqual(request(opener,source.PATH+'/'+job['job_id'])[0],status)
                    self.assertEqual(request(opener,source.PATH+'/'+job['job_id']+'/resume',{})[0],status)
                self.assertEqual(request(foreign,'/v1/sheet-vitrina-v1/operations?domain='+source.DOMAIN+'&search=actual_http')[1]['total'],0)
                with patch.object(block,'_connect',side_effect=AssertionError('no native writable GET')),patch.object(SkuInventoryBalanceBlock,'__init__',side_effect=AssertionError('no bootstrap GET')):
                    status,recovered=request(owner,source.PATH+'?request_id='+body['request_id']);self.assertEqual(status,200,recovered);self.assertEqual(job,recovered)
                    status,detail=request(owner,'/v1/sheet-vitrina-v1/operations/'+body['request_id']);self.assertEqual(status,200,detail)
                    self.assertEqual(detail['operation']['source_ref']['entity_id'],job['job_id'])
                    self.assertEqual(request(owner,source.PATH,body)[0],200)
                self.assertEqual(worker.call_count,1);self.assertFalse(adapter.submit_attempts);self.assertFalse(f.transport.write_calls)
                from packages.application.business_data_write_barrier import acquire_barrier
                acquire_barrier(app.runtime.runtime_dir,window_id='balance-maintenance',window_kind='snapshot',
                    plan_fingerprint='sha256:'+'a'*64,approval_reference='synthetic',actor='fixture',reason='synthetic maintenance')
                blocked_body=dict(body,request_id='balance-apply:maintenance_refusal_001')
                status,blocked=request(owner,source.PATH,blocked_body)
                self.assertEqual(status,423,blocked);self.assertEqual(blocked['code'],'business_data_maintenance')
                self.assertEqual(request(owner,source.PATH+'?request_id='+blocked_body['request_id'])[0],404)
                self.assertEqual(request(owner,source.PATH+'?request_id='+body['request_id'])[0],200)
                self.assertEqual(worker.call_count,1);self.assertFalse(adapter.submit_attempts)

    def test_actual_Chromium_confirmed_operands_survive_closed_modal_edit_during_fresh_GET(self):
        from apps.operator_feedback_forms_browser_smoke import native_server
        from packages.application.change_registry_observer import ChangeRegistryReadSurface
        from packages.adapters import registry_upload_http_entrypoint as web
        with patch.dict(os.environ,dict(WB_CORE_WEB_AUTH_REQUIRED='0')),native_server() as (base,app,f,_),sync_playwright() as pw:
            adapter=FakeLiveAdapter()
            with patch.object(SkuInventoryBalanceBlock,'_start_apply_worker_if_needed'):
                block,_,_=_build_runtime(Path(app.runtime.runtime_dir),adapter)
            app.sku_inventory_balance_block=block;app.change_registry_read_surface=ChangeRegistryReadSurface(app.runtime.runtime_dir,seller_id=SELLER_ID)
            calc=_insert_calculation(block,1,'confirmed_operand_race');target=calc['rows'][0]['campaign_recommendations'][0]
            browser=pw.chromium.launch()
            context=browser.new_context();page=context.new_page();held=[];posts=[];armed=[False]
            calcpath=web.DEFAULT_SKU_INVENTORY_BALANCE_CALCULATIONS_PREFIX+'/'+calc['calculation_id']
            def routes(route):
                path=route.request.url[len(base):].split('?')[0]
                if path==calcpath and route.request.method=='GET' and armed[0]:held.append(route);return
                if path==source.PATH and route.request.method=='POST':posts.append(route.request.post_data_json)
                if path==source.PATH or path.startswith(source.PATH+'/') or path.startswith(web.DEFAULT_SKU_INVENTORY_BALANCE_CALCULATIONS_PREFIX+'/') or path==web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH or path.endswith(('.js','.css')):route.continue_()
                else:route.fulfill(status=200,content_type='application/json',body='{}')
            context.route(base+'/**',routes)
            with patch.object(block,'_start_apply_worker_if_needed') as worker:
                page.goto(base+web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH+'?tab=sku-management')
                page.locator('[data-sku-management-subtab="inventory-balance"]').click(force=True);page.wait_for_timeout(300)
                page.evaluate('calc=>{state.inventoryBalance.operatorScope="synthetic-confirmed-operands";state.inventoryBalance.loaded=true;state.inventoryBalance.loadRequestToken++;installInventoryBalanceCalculation(calc);state.inventoryBalance.selectedNmIds.add(calc.rows[0].nm_id);state.inventoryBalance.explicitSelectionNmIds.add(calc.rows[0].nm_id);renderInventoryBalance();openInventoryBalanceConfirmation("live");}',calc)
                preview=page.evaluate('()=>inventoryBalanceConfirmBodyNode.textContent')
                armed[0]=True;page.locator('[data-inventory-balance-confirm-start]').click()
                for _ in range(200):
                    if held:break
                    page.wait_for_timeout(20)
                self.assertEqual(len(held),1)
                page.locator('[data-inventory-balance-confirm-close]').first.click()
                field=page.locator('[data-inventory-balance-override="'+target['target_key']+'"]').first
                self.assertTrue(field.is_enabled());self.assertTrue(page.evaluate('()=>state.inventoryBalance.applyRunning'))
                newprice=float(target['final_target_bid_rub'])+23
                field.fill(str(newprice));field.press('Enter')
                page.wait_for_function('()=>state.inventoryBalance.overrideSaving.size===0&&state.inventoryBalance.overrideDrafts.size===0')
                self.assertEqual(page.evaluate('()=>state.inventoryBalance.calculation.rows[0].campaign_recommendations[0].final_target_bid_rub'),newprice)
                route=held.pop();response=route.fetch();route.fulfill(response=response)
                page.wait_for_function('()=>!state.inventoryBalance.applyRunning')
                self.assertIn('отличаются от показанных',page.evaluate('()=>state.inventoryBalance.error'))
                self.assertFalse(posts);self.assertEqual(worker.call_count,0)
                self.assertFalse(page.evaluate('id=>!!localStorage.getItem("operator-balance-apply:synthetic-confirmed-operands:"+id)',calc['calculation_id']))
                with sqlite3.connect(block.runtime.db_path) as conn:self.assertEqual(conn.execute(f'SELECT count(*) FROM {source.TABLE}').fetchone()[0],0)
                self.assertIn('После подтверждения эти точные изменения',preview)
                self.assertFalse(adapter.submit_attempts);self.assertFalse(f.transport.write_calls);browser.close()

    def test_actual_Chromium_two_tab_lost_POST_unknown400_failed_GET_reload_sameID(self):
        from apps.operator_feedback_forms_browser_smoke import native_server
        from packages.application.change_registry_observer import ChangeRegistryReadSurface
        from packages.adapters import registry_upload_http_entrypoint as web
        with patch.dict(os.environ,dict(WB_CORE_WEB_AUTH_REQUIRED='0')),native_server() as (base,app,f,_),sync_playwright() as pw:
            adapter=FakeLiveAdapter()
            with patch.object(SkuInventoryBalanceBlock,'_start_apply_worker_if_needed'):
                block,_,_=_build_runtime(Path(app.runtime.runtime_dir),adapter)
            app.sku_inventory_balance_block=block;app.change_registry_read_surface=ChangeRegistryReadSurface(app.runtime.runtime_dir,seller_id=SELLER_ID)
            calc=_insert_calculation(block,1,'actual_browser');target=calc['rows'][0]['campaign_recommendations'][0]
            body=dict(calculation_id=calc['calculation_id'],apply_source_revision=calc['apply_source_revision'],nm_ids=[],target_keys=[target['target_key']],state_actions=[],mode='live_wb',confirmed=True)
            browser=pw.chromium.launch()
            context=browser.new_context();page=context.new_page();other=context.new_page();posts=[];held=[];errors=[]
            def routes(route):
                path=route.request.url[len(base):].split('?')[0]
                if path==source.PATH or path.startswith(source.PATH+'/') or path.startswith(web.DEFAULT_SKU_INVENTORY_BALANCE_CALCULATIONS_PREFIX+'/') or path.startswith('/v1/sheet-vitrina-v1/operations/') or path==web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH or path.endswith(('.js','.css')):route.continue_()
                else:route.fulfill(status=200,content_type='application/json',body='{}')
            context.route(base+'/**',routes)
            def lost(route):
                if route.request.method!='POST':route.continue_();return
                posts.append(route.request.post_data_json);response=route.fetch();self.assertEqual(response.status,200,response.text());held.append(route)
            def missing(route):route.abort('failed')
            context.route('**/inventory-balance/apply-jobs',lost);context.route('**/inventory-balance/apply-jobs?request_id=*',missing)
            with patch.object(block,'_start_apply_worker_if_needed') as worker:
                for tab in (page,other):
                    tab.on('pageerror',lambda e:errors.append(str(e)));tab.goto(base+web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH+'?tab=settings');tab.evaluate('()=>{state.inventoryBalance.operatorScope="synthetic-browser-native-scope";}');tab.evaluate('calc=>{state.inventoryBalance.loaded=true;state.inventoryBalance.loadRequestToken+=1;installInventoryBalanceCalculation(calc);}',calc)
                page.evaluate('body=>{window.saved=onceInventoryBalanceApply(body,false,state.inventoryBalance.calculation).catch(e=>({error:e.message}));}',body)
                while not held:page.wait_for_timeout(50)
                other.evaluate('body=>{window.saved=onceInventoryBalanceApply(body,false,state.inventoryBalance.calculation).catch(e=>({error:e.message}));}',body)
                page.wait_for_timeout(150);self.assertEqual(len(posts),1)
                held.pop().fulfill(status=400,content_type='application/json',body=json.dumps(dict(error='unknown after real native commit',code='unknown_native_proof')))
                self.assertIn('не подтверждено',page.evaluate('async()=>await window.saved')['error'])
                self.assertIn('не подтверждено',other.evaluate('async()=>await window.saved')['error']);self.assertEqual(len(posts),1)
                page.close();page=other;page.reload();page.evaluate('()=>{state.inventoryBalance.operatorScope="synthetic-browser-native-scope";}')
                result=page.evaluate('async body=>{try{return await onceInventoryBalanceApply(body,false);}catch(e){return {error:e.message};}}',body)
                self.assertIn('не подтверждено',result['error']);self.assertEqual(len(posts),1)
                context.unroute('**/inventory-balance/apply-jobs?request_id=*',missing)
                recovered=page.evaluate('async body=>await onceInventoryBalanceApply(body,true)',body)
                self.assertEqual(recovered['client_request_id'],posts[0]['request_id']);self.assertEqual(recovered['request']['calculation_id'],body['calculation_id']);self.assertEqual(len(posts),1);self.assertEqual(worker.call_count,1)
                self.assertFalse(page.evaluate('body=>!!localStorage.getItem("operator-balance-apply:synthetic-browser-native-scope:"+body.calculation_id)',body))
                # Stored succeeded flags alone are not WB proof, even in the
                # actual form's row label, counts or current-price projection.
                with sqlite3.connect(block.runtime.db_path) as conn:
                    conn.execute(f"UPDATE {source.TABLE} SET state='completed' WHERE job_id=?",(recovered['job_id'],))
                    conn.execute(f"UPDATE {source.ITEM_TABLE} SET state='succeeded',result_json=? WHERE job_id=?",(json.dumps(dict(readback_status='matching',confirmed_bid_minor=1)),recovered['job_id']))
                unproven=page.evaluate('async job=>await (await fetch(WEB_VITRINA_CONFIG.sku_inventory_balance_apply_jobs_path+"/"+job.job_id)).json()',recovered)
                page.evaluate('calc=>{state.inventoryBalance.loaded=true;state.inventoryBalance.loadRequestToken+=1;installInventoryBalanceCalculation(calc);}',calc)
                self.assertFalse(unproven['acceptance']['external_confirmed'])
                projected=page.evaluate('job=>{const old=state.inventoryBalance.calculation.rows[0].campaign_recommendations[0].current_bid_rub;applyInventoryBalanceJobObservations(job);state.inventoryBalance.applyJob=job;return {old:old,now:state.inventoryBalance.calculation.rows[0].campaign_recommendations[0].current_bid_rub,rows:inventoryBalanceProgressRows(job),counts:inventoryBalanceProgressCounts(job),last:inventoryBalanceLastApply(job.items[0].nm_id)};}',unproven)
                self.assertEqual(projected['old'],projected['now']);self.assertIn('Требуется проверка',projected['rows']);self.assertIn('0 применено',projected['counts']);self.assertIn('не подтверждено',projected['last']);self.assertNotIn('>применено<',projected['last'])
                with sqlite3.connect(block.runtime.db_path) as conn:
                    conn.execute(f"UPDATE {source.TABLE} SET state='pending' WHERE job_id=?",(recovered['job_id'],))
                    conn.execute(f"UPDATE {source.ITEM_TABLE} SET state='pending',result_json='{{}}' WHERE job_id=?",(recovered['job_id'],))
                claim=block._claim_next_live_job();block._run_live_job(*claim)
                finished=page.evaluate('async job=>await (await fetch(WEB_VITRINA_CONFIG.sku_inventory_balance_apply_jobs_path+"/"+job.job_id)).json()',recovered)
                self.assertTrue(finished['acceptance']['external_confirmed']);self.assertEqual(len(adapter.submit_attempts),1)
                summary=page.evaluate('job=>inventoryBalanceApplySummary(job)',finished);self.assertEqual(summary,'Все изменения подтверждены WB');self.assertIn('>применено<',page.evaluate('job=>{state.inventoryBalance.applyJob=job;return inventoryBalanceLastApply(job.items[0].nm_id);}',finished))
                # A new request must not absorb a changed selected operand.
                # The old displayed bid differs from actual confirmed native
                # current facts now; this fails before allocating another ID.
                page.evaluate('calc=>{state.inventoryBalance.loaded=true;state.inventoryBalance.loadRequestToken+=1;installInventoryBalanceCalculation(calc);}',calc)
                refused=page.evaluate('async body=>{try{return await onceInventoryBalanceApply(body,false,state.inventoryBalance.calculation);}catch(e){return {error:e.message};}}',body)
                self.assertIn('отличаются от показанных',refused['error']);self.assertEqual(len(posts),1)
                self.assertFalse(page.evaluate('body=>!!localStorage.getItem("operator-balance-apply:synthetic-browser-native-scope:"+body.calculation_id)',body))
                self.assertFalse(f.transport.write_calls);self.assertFalse(errors,errors);browser.close()


if __name__=='__main__':unittest.main(verbosity=2)
