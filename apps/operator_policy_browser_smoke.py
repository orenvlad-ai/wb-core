"""Actual Chromium policy forms and exact-ID recovery; local synthetic HTTP only."""
from pathlib import Path
import json,sys
from urllib.parse import urlsplit
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from playwright.sync_api import sync_playwright
from playwright.sync_api import expect
from apps.sheet_vitrina_v1_proxy_v4_settings_browser_smoke import _v3_payload,_v4_payload
from packages.adapters.registry_upload_http_entrypoint import (_render_sheet_vitrina_settings_ui,_render_sheet_vitrina_web_vitrina_ui,DEFAULT_CALCULATION_PARAMETERS_PATH,DEFAULT_CALCULATION_PARAMETERS_PREVIEW_PATH,DEFAULT_PROXY_V4_PARAMETERS_PATH,DEFAULT_PROXY_V4_PARAMETERS_PREVIEW_PATH)
BASE='http://127.0.0.1:8199'
READ='/v1/sheet-vitrina-v1/settings/policy-operations/'

def close_verified_policy_receipt(page,identity):
    """Acknowledge only the recovered same-operation receipt before proceeding."""
    popup=page.locator('dialog.ff-operation-popup[open]')
    expect(popup.locator('#policyAcceptance [data-ff-operation-receipt]')).to_have_attribute('data-ff-operation-receipt',identity)
    expect(popup.locator('.ff-operation-check')).to_be_visible()
    popup.get_by_role('button',name='Закрыть',exact=True).click()
    expect(popup).to_have_count(0)

def main():
    with sync_playwright() as pw:
        browser=pw.chromium.launch();context=browser.new_context();page=context.new_page()
        errors=[];page.on('pageerror',lambda err:errors.append(str(err)))
        posts=[];reads=[];saved={};mode={'lose':True,'foreign':False,'unknown':False,'reject':False}
        def receipt(identity,kind):return {'operation_id':identity,'domain':kind,'document_kind':kind,'accepted_at':'2026-08-26T12:00:00Z','state':'processing','durable_saved':True,'physical_applied':False,'primary_effect':'source_saved','title_ru':'Политика <img src=x>','journal_path':'/sheet-vitrina-v1/operations?operation_id='+identity}
        def route(r):
            req=r.request;path=urlsplit(req.url).path
            if path=='/settings':r.fulfill(content_type='text/html',body=_render_sheet_vitrina_settings_ui(operator_actor_scope='actor-A'));return
            if path=='/vitrina':r.fulfill(content_type='text/html',body=_render_sheet_vitrina_web_vitrina_ui(read_path='/data',operator_path='/operator',refresh_path='/refresh',job_path='/job',user_config_key='actor-A'));return
            if path.startswith(READ):
                reads.append(path);identity=path.rsplit('/',1)[1];value=saved.get(identity)
                if mode['unknown'] or not value:r.fulfill(status=404,body='{}');return
                r.fulfill(content_type='application/json',body=json.dumps({'operation':dict(value,domain='foreign') if mode['foreign'] else value}));return
            if req.method=='POST' and path in [DEFAULT_CALCULATION_PARAMETERS_PATH,DEFAULT_PROXY_V4_PARAMETERS_PATH,'/incident']:
                body=req.post_data_json;identity=body['_operator_request_id'];assert req.headers['x-operator-request-id']==identity;posts.append((path,body))
                if mode['reject']:r.fulfill(status=422,content_type='application/json',body=json.dumps({'error':'invalid source','operation_id':identity,'source_not_saved':True}));return
                kind='legacy_proxy' if path==DEFAULT_CALCULATION_PARAMETERS_PATH else 'proxy_v4_tax' if path==DEFAULT_PROXY_V4_PARAMETERS_PATH else 'wb_incident_policy'
                saved[identity]=receipt(identity,kind)
                if mode['lose']:r.abort('failed');return
                r.fulfill(content_type='application/json',body=json.dumps({'acceptance':dict(saved[identity],domain='foreign') if mode['foreign'] else saved[identity]}));return
            if path==DEFAULT_PROXY_V4_PARAMETERS_PREVIEW_PATH:payload={'status':'preview_ready','effective_date':'2026-08-26','before_tax_rate_pct':'6','after_tax_rate_pct':'7','changed':True,'preview_fingerprint':'sha256:fixture'}
            elif path==DEFAULT_CALCULATION_PARAMETERS_PREVIEW_PATH:payload={'preview_fingerprint':'sha256:fixture','diff':[{'label':'Налог','before_pct':'6','after_pct':'7'}]}
            elif path==DEFAULT_CALCULATION_PARAMETERS_PATH:payload=_v3_payload()
            elif path==DEFAULT_PROXY_V4_PARAMETERS_PATH:payload=_v4_payload(saved=False)
            else:payload={'items':[],'rows':[],'groups':[],'documents':[],'status':'ready','available_sections':[]}
            r.fulfill(content_type='application/json',body=json.dumps(payload))
        context.route('**/*',route)
        page.goto(BASE+'/settings#user-directory');page.wait_for_function("document.documentElement.dataset.settingsReady==='true'")
        page.locator('#proxyV4TaxRate').fill('7');page.locator('#previewProxyV4TaxButton').click();page.locator('#saveProxyV4TaxButton').click()
        page.wait_for_selector('#policyAcceptance .ff-operation-check');assert len(posts)==1 and reads[-1].endswith(posts[0][1]['_operator_request_id'])
        assert page.locator('#policyAcceptance .ff-operation-status').inner_text()=='Обрабатывается'
        assert page.locator('#saveProxyV4TaxButton').is_disabled();assert 'native' not in page.locator('#policyAcceptance').inner_text()
        assert page.locator('#proxyV4HistoryRows').inner_text().find('operator_tax')==-1
        first_id=posts[0][1]['_operator_request_id'];assert page.locator('#policyAcceptance .ff-operation-link').get_attribute('href')=='/sheet-vitrina-v1/operations?operation_id='+first_id;assert page.evaluate("JSON.parse(localStorage.getItem('wbc.policy.operations.actor-A.proxy_v4_tax'))")==[first_id]
        page.locator('#policyAcceptance button').filter(has_text='Закрыть').click();page.reload();page.wait_for_selector('#policyAcceptance .ff-operation-check');assert len(posts)==1
        close_verified_policy_receipt(page,first_id)
        # Foreign explicit domain in POST and GET cannot paint green or permit a new POST.
        context.clear_cookies();page.evaluate('localStorage.clear()');mode.update(lose=False,foreign=True)
        page.goto(BASE+'/settings#user-directory');page.wait_for_function("document.documentElement.dataset.settingsReady==='true'")
        page.locator('#proxyV4TaxRate').fill('7');page.locator('#previewProxyV4TaxButton').click();page.locator('#saveProxyV4TaxButton').click()
        page.get_by_role('button',name='Проверить сохранение',exact=True).wait_for();assert page.locator('#policyAcceptance .ff-operation-check').count()==0
        page.locator('#saveProxyV4TaxButton').click();page.wait_for_timeout(50);assert len(posts)==2
        mode['foreign']=False;page.get_by_role('button',name='Проверить сохранение',exact=True).click();page.wait_for_selector('#policyAcceptance .ff-operation-check');assert len(posts)==2
        close_verified_policy_receipt(page,posts[-1][1]['_operator_request_id'])
        # New host, URL routing hint, actor-bound GET only, and safe receipt text.
        second_id=posts[-1][1]['_operator_request_id'];page.evaluate('localStorage.clear()')
        page.goto(BASE+'/settings?policy_operation_id='+second_id+'&policy_kind=proxy_v4_tax#user-directory');page.wait_for_selector('#policyAcceptance .ff-operation-check');assert len(posts)==2
        assert page.locator('#policyAcceptance img').count()==0
        close_verified_policy_receipt(page,second_id)
        # Component path also covers legacy numeric operands, unknown reload, rejected source, and incident family.
        page.evaluate("""() => { window.box=document.createElement('div');document.body.appendChild(box);window.leaf=OperatorPolicy.create({policy_operations_path:'/v1/sheet-vitrina-v1/settings/policy-operations/',operator_policy_actor_scope:'isolated'},box,['legacy_proxy','wb_incident_policy']); }""")
        mode.update(lose=True,unknown=True)
        result=page.evaluate("async()=>{try{await leaf.submit('legacy_proxy','%s',{tax_rate:0.00001});return 'green';}catch(e){return e.message;}}"%DEFAULT_CALCULATION_PARAMETERS_PATH)
        assert 'Повторно' in result;before=len(posts);page.reload();assert len(posts)==before
        page.evaluate("""()=>{window.box=document.createElement('div');document.body.appendChild(box);window.leaf=OperatorPolicy.create({policy_operations_path:'/v1/sheet-vitrina-v1/settings/policy-operations/',operator_policy_actor_scope:'isolated'},box,['legacy_proxy','wb_incident_policy']);}""")
        page.evaluate('leaf.restore()');result=page.evaluate("async()=>{try{await leaf.submit('legacy_proxy','%s',{});}catch(e){return e.message;}}"%DEFAULT_CALCULATION_PARAMETERS_PATH);assert 'неизвестен' in result and len(posts)==before
        mode.update(lose=False,unknown=False);page.evaluate('leaf.restore()');assert page.locator('.ff-operation-check').count()>0
        mode['reject']=True;result=page.evaluate("async()=>{try{await leaf.submit('wb_incident_policy','/incident',{});}catch(e){return e.message;}}");assert result=='invalid source'
        assert page.evaluate("JSON.parse(localStorage.getItem('wbc.policy.operations.isolated.wb_incident_policy'))")==[]
        mode['reject']=False;page.evaluate("leaf.submit('wb_incident_policy','/incident',{})");assert page.locator('.ff-operation-check').count()>0
        # Ensure the actual Vitrina host initializes incident receipt leaf without script errors.
        page.goto(BASE+'/vitrina?tab=settings');page.wait_for_timeout(100)
        assert not errors,errors
        browser.close()
    print('operator_policy_browser_smoke: PASS actual forms/lost response/reload/foreign domain/unknown no-resubmit/URL/numeric/reject/incident host')
def native_two_tab_recovery():
    """Real native HTTP writes; only reply-loss and unavailable GET are routed."""
    from tempfile import TemporaryDirectory
    from datetime import datetime,timezone
    import sqlite3,threading
    from apps.operator_policy_http_smoke import _seed,_free_port,DAY,NOW
    from packages.adapters import registry_upload_http_entrypoint as web
    from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
    from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig
    from packages.application import operator_policy as op
    with TemporaryDirectory(prefix='policy-native-two-tabs-') as tmp:
        runtime=_seed(Path(tmp),mixed=False)
        entry=RegistryUploadHttpEntrypoint(runtime_dir=runtime.runtime_dir,runtime=runtime,activated_at_factory=lambda:NOW,now_factory=lambda:datetime(2026,8,26,12,tzinfo=timezone.utc))
        config=RegistryUploadHttpEntrypointConfig(host='127.0.0.1',port=_free_port(),upload_path=web.DEFAULT_UPLOAD_PATH,sheet_plan_path=web.DEFAULT_SHEET_PLAN_PATH,sheet_refresh_path=web.DEFAULT_SHEET_REFRESH_PATH,sheet_status_path=web.DEFAULT_SHEET_STATUS_PATH,sheet_operator_ui_path=web.DEFAULT_SHEET_OPERATOR_UI_PATH,runtime_dir=runtime.runtime_dir)
        server=web.build_registry_upload_http_server(config,entrypoint=entry);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(config.port);writes=[];gets=[];responses=[];mode={'hide':True}
        payload={'effective_date':DAY,'buyout_rate':'1','tax_rate':'0.1'}
        payload['preview_fingerprint']=entry.calculation_parameters_block.preview_version(payload)['preview_fingerprint']
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch();context=browser.new_context()
                def route(r):
                    path=urlsplit(r.request.url).path
                    if path.startswith(op.PATH):
                        gets.append(path)
                        if mode['hide']:r.fulfill(status=404,content_type='application/json',body='{}');return
                        r.continue_();return
                    if r.request.method=='POST' and path in (web.DEFAULT_CALCULATION_PARAMETERS_PATH,web.DEFAULT_PROXY_V4_PARAMETERS_PATH):
                        writes.append(r.request.post_data_json);response=r.fetch();responses.append(response.json())
                        if len(writes)==1:r.abort();return
                        r.fulfill(response=response);return
                    if path.startswith('/v1/'):
                        r.fulfill(content_type='application/json',body=json.dumps({'status':'ready','items':[],'rows':[],'groups':[],'documents':[]}));return
                    r.continue_()
                context.route('**/*',route);a=context.new_page();b=context.new_page()
                scope='native_policy_race';key='wbc.policy.operations.'+scope+'.legacy_proxy'
                def load(page,url=None):
                    page.goto(url or base+web.DEFAULT_SETTINGS_UI_PATH+'?embedded=1');page.wait_for_function('Boolean(window.OperatorPolicy)')
                    page.evaluate("""scope=>{window.box=document.createElement('div');document.body.appendChild(box);window.leaf=OperatorPolicy.create({operator_policy_actor_scope:scope,policy_operations_path:'/v1/sheet-vitrina-v1/settings/policy-operations/'},box,['legacy_proxy','proxy_v4_tax','wb_incident_policy']);}""",scope)
                def submit(page,kind='legacy_proxy',data=None):
                    return page.evaluate("""async args=>{try{return await leaf.submit(args.kind,args.url,args.payload);}catch(e){return {error:e.message};}}""",{'kind':kind,'url':web.DEFAULT_CALCULATION_PARAMETERS_PATH,'payload':data or payload})
                load(a);load(b);first=submit(a);identity=writes[0]['_operator_request_id']
                assert 'error' in first and len(writes)==1 and responses[0]['acceptance']['state']=='processing'
                second=submit(b);assert 'неизвестен' in second['error'] and len(writes)==1
                assert b.evaluate('(key)=>JSON.parse(localStorage.getItem(key))',key)==[identity]
                a.close();before=len(gets);load(b);b.evaluate('leaf.restore()')
                assert op.PATH+'legacy_proxy/'+identity in gets[before:] and len(writes)==1
                # A URL adds a read hint only; it cannot erase another durable ID.
                hint='oppolicy_'+'c'*32
                load(b,base+web.DEFAULT_SETTINGS_UI_PATH+'?embedded=1&policy_kind=legacy_proxy&policy_operation_id='+hint)
                b.evaluate('leaf.restore()');assert b.evaluate('(key)=>JSON.parse(localStorage.getItem(key))',key)==[identity]
                load(b);mode['hide']=False;b.evaluate('leaf.restore()')
                rejected=submit(b);assert rejected['error']=='operator_policy_pending_command_conflict'
                assert responses[-1]['source_not_saved'] and len(writes)==2
                assert b.evaluate('(key)=>JSON.parse(localStorage.getItem(key))',key)==[identity]
                # Hold the account lock, then mutate the caller object after its
                # click. The real queued native command must keep clicked operands.
                holder=context.new_page();load(holder)
                holder.evaluate("""()=>{window.holding=false;window.held=navigator.locks.request('wbc.policy.operations.native_policy_race:mutation',()=>new Promise(resolve=>{window.release=resolve;window.holding=true;}));}""")
                holder.wait_for_function('holding')
                v4={'tax_rate':'0.1'};v4['preview_fingerprint']=entry.proxy_v4_parameters_block.preview_tax_version(v4)['preview_fingerprint']
                b.evaluate("""args=>{window.mutable=args;window.waiting=leaf.submit('proxy_v4_tax','/v1/sheet-vitrina-v1/settings/calculation-parameters-v4',mutable).catch(e=>({error:e.message}));mutable.tax_rate='0.9';}""",v4)
                holder.evaluate('release()');result=b.evaluate('waiting');assert 'acceptance' in result,result
                assert writes[-1]['tax_rate']=='0.1' and len(writes)==3
                v4id=writes[-1]['_operator_request_id']
                assert b.evaluate('(key)=>JSON.parse(localStorage.getItem(key))',key)==[identity]
                assert b.evaluate('JSON.parse(localStorage.getItem("wbc.policy.operations.native_policy_race.proxy_v4_tax"))')==[v4id]
                conn=sqlite3.connect('file:'+str(runtime.db_path)+'?mode=ro',uri=True);conn.execute('PRAGMA query_only=ON')
                rows=conn.execute('SELECT operation_id,state,source_json FROM '+op.TABLE).fetchall();conn.close()
                assert {row[0] for row in rows}=={identity,v4id} and all(row[1]=='pending' for row in rows),rows
                assert json.loads(next(row[2] for row in rows if row[0]==v4id))['payload']['tax_rate']=='0.1'
                before=len(writes);b.reload();load(b);b.evaluate('leaf.restore()');assert len(writes)==before
                # Invalid durable IDs and unavailable Web Locks both refuse
                # admission before generating or submitting another identity.
                b.evaluate('localStorage.setItem("wbc.policy.operations.native_policy_race.wb_incident_policy", "broken")')
                invalid=submit(b);assert 'прочитать' in invalid['error'] and len(writes)==before
                b.evaluate('localStorage.removeItem("wbc.policy.operations.native_policy_race.wb_incident_policy")')
                b.evaluate('Object.defineProperty(navigator,"locks",{value:null,configurable:true})')
                unavailable=submit(b);assert 'между вкладками' in unavailable['error'] and len(writes)==before
                browser.close()
        finally:server.shutdown();server.server_close();thread.join(timeout=5)
    print('operator_policy_browser_smoke: PASS actual native two-tab lost reply, GET-only fencing, close/reload, URL preserves IDs, own rejection, all kinds retained, captured operands')

if __name__=='__main__':main();native_two_tab_recovery()
