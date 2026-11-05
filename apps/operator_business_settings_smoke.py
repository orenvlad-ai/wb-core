#!/usr/bin/env python3
"""Native source CAS/immutable versions and actual forms on temporary fixtures."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote
import json,sqlite3,sys,time,unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application import operator_business_settings as source,operator_operations as journal


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.runtime=RegistryUploadDbBackedRuntime(runtime_dir=Path(self.temp.name))
    def save(self,identity='business-settings:fixture0001',base=0,value=5,actor='one',user='user-one',config='sku_management',**kwargs):
        field=source.FIELDS[config]
        return self.runtime.save_sheet_vitrina_user_config(user_key=user,config_key=config,schema_version=1,
            payload={field:{'period':value},'table':{'private_search':'private preference'}},
            updated_at='2026-07-20T12:00:00Z',expected_revision=base,
            operator_command=dict(operation_id=identity,actor=actor,seller_id='fixture'),**kwargs)
    def scope(self,actor='one',user='user-one',configs=None,seller='fixture'):
        return source.SettingsScope(actor,user,seller,frozenset(source.FIELDS if configs is None else configs))
    def test_exact_old_version_repeat_ABA_and_foreign_identity_conflict(self):
        first=self.save();second=self.save('business-settings:fixture0002',base=1,value=8)
        old=self.save();self.assertEqual(old['revision'],1);self.assertEqual(old['config']['forecast']['period'],5)
        current=self.runtime.load_sheet_vitrina_user_config(user_key='user-one',config_key='sku_management')
        self.assertEqual(current['revision'],2);self.assertEqual(current['config']['forecast']['period'],8)
        for changed in ({'value':8},{'actor':'foreign'},{'user':'foreign-user'}):
            with self.subTest(changed=changed),self.assertRaisesRegex(ValueError,'identity_conflict'):self.save(**changed)
        aba=self.save('business-settings:fixture0003',base=2,value=5)
        self.assertEqual(aba['revision'],3);self.assertEqual(self.save()['acceptance'],first['acceptance'])
        with sqlite3.connect(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM '+source.TABLE).fetchone()[0],3)
            with self.assertRaisesRegex(sqlite3.IntegrityError,'immutable'):conn.execute('UPDATE '+source.TABLE+" SET actor='foreign'")
            with self.assertRaisesRegex(sqlite3.IntegrityError,'immutable'):conn.execute('DELETE FROM '+source.TABLE)
    def test_config_and_version_are_atomic_rollback_and_personal_preferences_excluded(self):
        first=self.save()
        with patch.object(source,'save',side_effect=RuntimeError('synthetic proof commit failure')):
            with self.assertRaisesRegex(RuntimeError,'proof commit failure'):self.save('business-settings:fixture0002',base=1,value=7)
        current=self.runtime.load_sheet_vitrina_user_config(user_key='user-one',config_key='sku_management')
        self.assertEqual(current['revision'],1)
        saved=self.runtime.save_sheet_vitrina_user_config(user_key='user-one',config_key='sku_management',schema_version=1,
            payload={'forecast':{'period':5},'table':{'private_search':'another private preference'}},
            expected_revision=1,updated_at='2026-07-20T12:01:00Z')
        self.assertNotIn('acceptance',saved)
        with sqlite3.connect(self.runtime.db_path) as conn:
            row=conn.execute('SELECT before_json,after_json FROM '+source.TABLE).fetchone()
            self.assertNotIn('private',str(row));self.assertEqual(conn.execute('SELECT count(*) FROM '+source.TABLE).fetchone()[0],1)
        self.assertEqual(first['acceptance']['primary_effect'],'source_saved');self.assertFalse(first['acceptance']['calculation_completed'])
    def test_serial_CAS_one_of_concurrent_saves_and_no_partial_history(self):
        self.save()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda i:self.save('business-settings:parallel000'+str(i),base=1,value=i),[2,3]))
        self.assertEqual(sorted(result['status'] for result in results),['conflict','ok'])
        with sqlite3.connect(self.runtime.db_path) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM '+source.TABLE).fetchone()[0],2)
    def test_explicit_invalid_identity_rejected_before_native_source_write(self):
        self.save()
        before=self.runtime.db_path.read_bytes()
        for identity in ('',None,'foreign:fixture0001'):
            with self.subTest(identity=identity),self.assertRaisesRegex(ValueError,'identity_invalid'):
                self.save(identity,base=1)
        self.assertEqual(self.runtime.db_path.read_bytes(),before)
    def test_grants_actor_user_account_config_before_count_search_page_exact_RO(self):
        first=self.save();self.save('business-settings:otheruser001',actor='two',user='user-two')
        self.save('business-settings:balance00001',config='sku_inventory_balance')
        before=self.runtime.db_path.read_bytes()
        def listing(scope,**kw):return journal.journal(self.runtime.db_path,allowed_domains={source.DOMAIN},settings_scope=scope,**kw)
        self.assertEqual(listing(self.scope(configs={'sku_management'}))['total'],1)
        self.assertEqual(listing(self.scope(configs={'sku_management'}),search='balance00001')['total'],0)
        for scope in (self.scope(actor='foreign'),self.scope(user='foreign-user'),self.scope(seller='foreign'),self.scope(configs=set())):
            self.assertEqual(listing(scope)['total'],0)
            self.assertIsNone(journal.read_acceptance(self.runtime.db_path,first['acceptance']['operation_id'],
                allowed_domains={source.DOMAIN},settings_scope=scope))
        self.assertEqual(journal.journal(self.runtime.db_path,allowed_domains=set(),settings_scope=self.scope())['total'],0)
        self.assertFalse(listing(self.scope(),page=100)['items'])
        self.assertEqual(self.runtime.db_path.read_bytes(),before)

    def test_actual_native_forms_lost_response_reload_only_same_GET(self):
        from apps.operator_business_settings_fixture import native_server
        from packages.adapters import registry_upload_http_entrypoint as web
        from playwright.sync_api import sync_playwright
        with native_server() as (base,app,f,job),sync_playwright() as pw:
            browser=pw.chromium.launch()
            try:
                page=browser.new_page();posts=[];reads=[];lost_reads={'value':True};errors=[]
                def routes(route):
                    path=route.request.url[len(base):].split('?')[0]
                    allowed={web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH,web.DEFAULT_SKU_MANAGEMENT_SETTINGS_PATH,
                        web.DEFAULT_SKU_INVENTORY_BALANCE_SETTINGS_PATH}
                    if path.startswith('/v1/sheet-vitrina-v1/operations/'):
                        reads.append(route.request.url)
                        if lost_reads['value']:route.abort('failed')
                        else:route.continue_()
                    elif path in allowed or path.endswith('.js') or path.endswith('.css'):route.continue_()
                    else:route.fulfill(status=200,content_type='application/json',body='{}')
                page.route(base+'/**',routes)
                def lose_post(route):
                    if route.request.method!='POST':route.continue_();return
                    posts.append(route.request.post_data_json);response=route.fetch()
                    if response.status!=200:raise AssertionError(response.text())
                    self.assertEqual(response.json()['acceptance']['actor'],'local_operator')
                    route.abort('failed')
                page.route('**/sku-management/settings',lose_post)
                page.route('**/inventory-balance/settings',lose_post)
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(base+web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH+'?tab=settings')
                for config,path in source.NATIVE_PATHS.items():
                    if config=='sku_management':
                        settings=page.evaluate('async path=>{const r=await fetch(path);return r.json();}',path)
                        native_user_key=settings['operator_scope']
                    else:
                        # Balance settings are included by its native main GET;
                        # the existing dedicated settings endpoint is POST-only.
                        # Read the actual owner method to seed this bounded form
                        # fixture, avoiding unrelated current calculation/WB work.
                        settings=app.sku_inventory_balance_block.get_settings(user_key=native_user_key)
                    field=source.FIELDS[config]
                    self.assertIn('revision',settings,settings)
                    body=dict(base_revision=settings['revision'],table=settings['table'],**{field:settings[field]})
                    args=[config,path,body,settings['operator_scope']]
                    result=page.evaluate('async args=>{try{return await saveNativeBusinessSettings(...args);}catch(e){return {error:e.message};}}',args)
                    self.assertIn('error',result);self.assertEqual(len(posts),1 if config=='sku_management' else 2)
                    identity=posts[-1]['operation_id'];page.reload();lost_reads['value']=False
                    # Recover through the actual parent form, not just the
                    # shared exact-ID helper. The retired forecast controls stay
                    # hidden; invoking their compatibility function adds no UI.
                    if config=='sku_management':
                        recovered=page.evaluate('''async settings=>{state.skuManagement.settings=settings.forecast;state.skuManagement.table=settings.table;state.skuManagement.revision=settings.revision;state.skuManagement.operatorScope=settings.operator_scope;syncSkuManagementSettingsInputs();loadSkuManagement=async()=>{};await saveSkuManagementSettings();return {revision:state.skuManagement.revision,error:state.skuManagement.error};}''',settings)
                    else:
                        recovered=page.evaluate('''async settings=>{state.inventoryBalance.settings=settings.calculation;state.inventoryBalance.table=settings.table;state.inventoryBalance.revision=settings.revision;state.inventoryBalance.operatorScope=settings.operator_scope;syncInventoryBalanceInputs();await saveInventoryBalanceSettings();return {revision:state.inventoryBalance.revision,error:state.inventoryBalance.error};}''',settings)
                    self.assertFalse(recovered['error'],recovered)
                    self.assertEqual(recovered['revision'],settings['revision']+1)
                    self.assertEqual(page.locator('#operator-business-settings-receipt [data-ff-operation-receipt]').get_attribute('data-ff-operation-receipt'),identity)
                    self.assertEqual(page.locator('#operator-business-settings-receipt .ff-operation-status').inner_text(),'Сохранено')
                    page.locator('#operator-business-settings-receipt').get_by_role('button',name='Закрыть').click()
                    self.assertEqual(len(posts),1 if config=='sku_management' else 2)
                    lost_reads['value']=True
                self.assertEqual(len(posts),2);self.assertGreaterEqual(len(reads),4);self.assertFalse(errors,errors)
            finally:browser.close()

    def test_actual_two_tabs_lock_pending_native_commit_and_no_lock_fail_before_submit(self):
        from apps.operator_business_settings_fixture import native_server
        from packages.adapters import registry_upload_http_entrypoint as web
        from playwright.sync_api import sync_playwright
        with native_server() as (base,app,f,job),sync_playwright() as pw:
            browser=pw.chromium.launch();context=browser.new_context()
            try:
                first=context.new_page();second=context.new_page();posts=[];held=[];reads=[]
                path=source.NATIVE_PATHS['sku_management']
                def routes(route):
                    requested=route.request.url[len(base):].split('?')[0]
                    if requested.startswith('/v1/sheet-vitrina-v1/operations/'):
                        reads.append(route.request.url)
                        if route.request.frame.page==first:route.abort('failed')
                        else:route.continue_()
                    elif requested==path and route.request.method=='POST':
                        posts.append(route.request.post_data_json)
                        response=route.fetch();self.assertEqual(response.status,200,response.text())
                        held.append(route)  # committed source, browser response held
                    elif requested in {web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH,path} or requested.endswith(('.js','.css')):route.continue_()
                    else:route.fulfill(status=200,content_type='application/json',body='{}')
                context.route(base+'/**',routes)
                for page in (first,second):page.goto(base+web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH+'?tab=settings')
                settings=first.evaluate('async path=>(await fetch(path)).json()',path)
                args=['sku_management',path,dict(base_revision=settings['revision'],forecast=settings['forecast'],table=settings['table']),settings['operator_scope']]
                run='args=>{window.settingsOutcome=null;saveNativeBusinessSettings(...args).then(value=>window.settingsOutcome={ok:true,value},error=>window.settingsOutcome={error:error.message});}'
                first.evaluate(run,args);first.wait_for_function('window.settingsOutcome===null')
                # Wait for the native commit, not a fixed browser scheduling delay.
                deadline=time.monotonic()+30
                while not held and time.monotonic()<deadline:first.wait_for_timeout(25)
                self.assertEqual(len(held),1);identity=posts[0]['operation_id']
                second.evaluate(run,args);second.wait_for_timeout(100)
                self.assertIsNone(second.evaluate('window.settingsOutcome'));self.assertEqual(len(posts),1)
                key=first.evaluate('args=>businessSettingsFenceKey(args[0],args[3])',args)
                self.assertEqual(second.evaluate('key=>JSON.parse(localStorage.getItem(key)).identity',key),identity)
                held[0].abort('failed')
                first.wait_for_function('window.settingsOutcome!==null');second.wait_for_function('window.settingsOutcome!==null')
                self.assertIn('error',first.evaluate('window.settingsOutcome'))
                outcome=second.evaluate('window.settingsOutcome');self.assertTrue(outcome['ok']);self.assertEqual(outcome['value']['acceptance']['operation_id'],identity)
                self.assertEqual(len(posts),1);self.assertEqual(len(reads),2);self.assertTrue(all(unquote(url.rsplit('/',1)[-1])==identity for url in reads))
                self.assertIsNone(first.evaluate('key=>localStorage.getItem(key)',key))
                # A later caller must fail before POST without cross-tab lock.
                second.evaluate("Object.defineProperty(navigator,'locks',{value:undefined,configurable:true})")
                denied=second.evaluate('async args=>{try{await saveNativeBusinessSettings(...args);return null;}catch(e){return e.message;}}',args)
                self.assertIn('Запись не отправлена',denied);self.assertEqual(len(posts),1)
            finally:context.close();browser.close()

    def test_actual_HTTP_bound_negative_and_postcommit_unknown(self):
        from apps.operator_business_settings_fixture import native_server
        from packages.adapters import registry_upload_http_entrypoint as web
        from urllib.request import Request,urlopen
        from urllib.error import HTTPError
        with native_server() as (base,app,_,__):
            path=source.NATIVE_PATHS['sku_management']
            def request(path,body=None):
                data=None if body is None else json.dumps(body).encode()
                try:
                    with urlopen(Request(base+path,data=data,headers={'Content-Type':'application/json'})) as response:return response.status,json.load(response)
                except HTTPError as error:return error.code,json.load(error)
            settings=request(path)[1];identity='business-settings:http_bound0001'
            body=dict(base_revision=settings['revision'],forecast=settings['forecast'],table=settings['table'],operation_id=identity)
            original=app.handle_sku_management_settings_save_request
            def commit_then_error(*args,**kwargs):original(*args,**kwargs);raise ValueError('synthetic postcommit reply failure')
            with patch.object(app,'handle_sku_management_settings_save_request',side_effect=commit_then_error):
                status,result=request(path,body)
            self.assertEqual(status,400);self.assertNotIn('source_not_saved',result)
            self.assertEqual(request('/v1/sheet-vitrina-v1/operations/'+identity)[1]['operation']['operation_id'],identity)
            stale='business-settings:http_bound0002';status,result=request(path,dict(body,operation_id=stale))
            self.assertEqual(status,409);self.assertTrue(result['source_not_saved']);self.assertEqual(result['operation_id'],stale)
            self.assertEqual(request('/v1/sheet-vitrina-v1/operations/'+stale)[0],404)
            foreign=dict(body,operation_id=identity,forecast=dict(settings['forecast'],unknown='foreign'))
            status,result=request(path,foreign);self.assertNotIn('source_not_saved',result)

    def test_actual_HTTP_native_grants_actor_scope_before_journal_counts(self):
        from apps.operator_business_settings_fixture import native_server
        from packages.adapters import registry_upload_http_entrypoint as web
        from urllib.request import Request,urlopen
        from urllib.error import HTTPError
        with native_server() as (base,app,_,__):
            def request(path,body=None):
                try:
                    with urlopen(Request(base+path,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'})) as response:return response.status,json.load(response)
                except HTTPError as error:return error.code,json.load(error)
            user={'username':'alice','role':web.WEB_AUTH_ROLE_OPERATOR,'allowed_sections':[web.WEB_AUTH_SECTION_SKU_MANAGEMENT]}
            with patch.object(web,'_web_auth_config',return_value={'enabled':True,'configured':True}),patch.object(web,'_authenticated_web_user',return_value=user):
                path=source.NATIVE_PATHS['sku_management'];settings=request(path)[1]
                identity='business-settings:http_grants001';status,result=request(path,dict(base_revision=settings['revision'],forecast=settings['forecast'],table=settings['table'],operation_id=identity))
                self.assertEqual(status,200,result);self.assertEqual(result['acceptance']['actor'],'alice')
                route='/v1/sheet-vitrina-v1/operations?domain=business_settings&search='+identity
                before=app.runtime.db_path.read_bytes();self.assertEqual(request(route)[1]['total'],1)
                self.assertEqual(request('/v1/sheet-vitrina-v1/operations/'+identity)[0],200)
                with patch.object(web,'_current_web_user_actor',return_value='foreign'):
                    self.assertEqual(request(route)[1]['total'],0);self.assertEqual(request('/v1/sheet-vitrina-v1/operations/'+identity)[0],404)
                user['allowed_sections']=[web.WEB_AUTH_SECTION_SUPPLY]
                self.assertEqual(request(route)[1]['total'],0);self.assertEqual(request('/v1/sheet-vitrina-v1/operations/'+identity)[0],404)
                user['allowed_sections']=[web.WEB_AUTH_SECTION_SKU_MANAGEMENT];user['username']='another-user'
                self.assertEqual(request(route)[1]['total'],0);self.assertEqual(request('/v1/sheet-vitrina-v1/operations/'+identity)[0],404)
                self.assertEqual(app.runtime.db_path.read_bytes(),before)

    def test_actual_browser_generic_4xx_retains_ID_and_preawait_payload(self):
        from apps.operator_business_settings_fixture import native_server
        from packages.adapters import registry_upload_http_entrypoint as web
        from playwright.sync_api import sync_playwright
        with native_server() as (base,app,_,__),sync_playwright() as pw:
            browser=pw.chromium.launch();context=browser.new_context();page=context.new_page();posts=[];gets=[]
            path=source.NATIVE_PATHS['sku_management'];mode={'generic':True,'hide':True}
            def routes(route):
                target=route.request.url[len(base):].split('?')[0]
                if target.startswith('/v1/sheet-vitrina-v1/operations/'):
                    gets.append(target)
                    if mode['hide']:route.abort('failed');return
                    route.continue_();return
                if target==path and route.request.method=='POST':
                    posts.append(route.request.post_data_json);response=route.fetch()
                    if mode['generic']:route.fulfill(status=422,content_type='application/json',body='{"error":"synthetic ambiguous proxy reply"}');return
                    route.fulfill(response=response);return
                if target in {web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH,path} or target.endswith(('.js','.css')):route.continue_()
                else:route.fulfill(status=200,content_type='application/json',body='{}')
            context.route(base+'/**',routes)
            try:
                page.goto(base+web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH+'?tab=settings')
                settings=page.evaluate('async path=>(await fetch(path)).json()',path)
                body=dict(base_revision=settings['revision'],forecast=settings['forecast'],table=settings['table'])
                args=['sku_management',path,body,settings['operator_scope']]
                call='async args=>{try{return await saveNativeBusinessSettings(...args);}catch(error){return {error:error.message};}}'
                result=page.evaluate(call,args);self.assertIn('error',result);self.assertEqual(len(posts),1)
                identity=posts[0]['operation_id'];key=page.evaluate('args=>businessSettingsFenceKey(args[0],args[3])',args)
                self.assertEqual(page.evaluate('key=>JSON.parse(localStorage.getItem(key)).identity',key),identity)
                page.reload();mode.update(generic=False,hide=False)
                recovered=page.evaluate(call,args);self.assertEqual(recovered['acceptance']['operation_id'],identity);self.assertEqual(len(posts),1)
                # A true native stale-CAS negative drops only its own fence.
                rejected=page.evaluate(call,args);self.assertIn('error',rejected);self.assertEqual(len(posts),2)
                self.assertIsNone(page.evaluate('key=>localStorage.getItem(key)',key))
                page.locator('#operator-business-settings-receipt').get_by_role('button',name='Закрыть').click()
                holder=context.new_page();holder.goto(base+web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH+'?tab=settings')
                holder.evaluate('key=>{window.lockHeld=false;window.hold=navigator.locks.request(key,()=>new Promise(resolve=>{window.release=resolve;window.lockHeld=true;}));}',key)
                holder.wait_for_function('lockHeld')
                fresh=page.evaluate('async path=>(await fetch(path)).json()',path)
                newbody=dict(base_revision=fresh['revision'],forecast=fresh['forecast'],table=fresh['table'])
                page.evaluate('args=>{window.mutable=args[2];window.outcome=saveNativeBusinessSettings(...args);mutable.forecast={};mutable.table={foreign_after_await:true};}', ['sku_management',path,newbody,fresh['operator_scope']])
                holder.evaluate('release()');saved=page.evaluate('outcome')
                self.assertEqual(posts[-1]['forecast'],newbody['forecast']);self.assertEqual(posts[-1]['table'],newbody['table'])
                self.assertEqual(saved['acceptance']['source_ref']['after'],newbody['forecast']);self.assertEqual(len(posts),3)
            finally:context.close();browser.close()


if __name__=='__main__':unittest.main()
