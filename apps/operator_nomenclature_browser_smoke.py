"""Real local settings host, source HTTP and Chromium; no WB calls or real data."""
import json,os,sys,threading
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.ff_pool_surfaces_browser_smoke import _reserve_free_port
from apps.nomenclature_activation_intents_smoke import fixture
from apps.nomenclature_barcode_smoke import FakeBarcodeSource
from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_SETTINGS_UI_PATH,DEFAULT_NOMENCLATURE_PATH,DEFAULT_SKU_GROUPS_PATH,
    DEFAULT_UPLOAD_PATH,DEFAULT_SHEET_PLAN_PATH,DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,build_registry_upload_http_server)
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig


def run():
    with TemporaryDirectory(prefix='operator-sku-browser-') as tmp:
        runtime=fixture(Path(tmp),facilities=0)
        entry=RegistryUploadHttpEntrypoint(runtime_dir=runtime.runtime_dir,runtime=runtime,
            activated_at_factory=lambda:'2026-10-08T09:00:00Z')
        entry.supplier_shipments_block.barcode_source=FakeBarcodeSource()
        config=RegistryUploadHttpEntrypointConfig(host='127.0.0.1',port=_reserve_free_port(),upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,sheet_refresh_path='/v1/sheet-vitrina-v1/refresh',sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,runtime_dir=runtime.runtime_dir)
        server=build_registry_upload_http_server(config,entrypoint=entry)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with sync_playwright() as playwright:
                browser=playwright.chromium.launch()
                try:
                    scenario(browser,f'http://127.0.0.1:{config.port}',entry)
                    two_tabs_scenario(browser,f'http://127.0.0.1:{config.port}',entry)
                finally:browser.close()
        finally:server.shutdown();server.server_close();thread.join(timeout=5)
    print('operator_nomenclature_browser_smoke: OK (real HTTP source, tiny CNY, lost response, reload exact GET, foreign domain, no resubmit, actor scope, two-tab lock/closed-tab recovery/negative retry, queued CAS/options/FormData snapshot)')


def scenario(browser,base,entry):
    context=browser.new_context(viewport={'width':1400,'height':900});page=context.new_page();errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    writes=[];reads=[];control={'lose':True,'foreign':False,'unknown':False}
    def routes(route):
        request=route.request;path=urlparse(request.url).path
        if path.startswith(DEFAULT_NOMENCLATURE_PATH) or path.startswith(DEFAULT_SKU_GROUPS_PATH):
            if '/operations/' in path:
                reads.append(path.rsplit('/',1)[1])
                if control['unknown']:route.fulfill(status=404,content_type='application/json',body='{}');return
                response=route.fetch()
                if control['foreign']:
                    data=response.json();data['operation']['domain']='foreign';data['operation']['source_ref']['domain']='foreign'
                    route.fulfill(status=200,content_type='application/json',body=json.dumps(data));return
                route.fulfill(response=response);return
            if request.method in {'POST','PATCH','DELETE'}:
                writes.append(request.headers['x-operator-request-id'])
                response=route.fetch()  # Actual synthetic native source commit.
                if control['lose']:route.abort();return
                if control['foreign']:
                    data=response.json();data['acceptance']['domain']='foreign';data['acceptance']['source_ref']['domain']='foreign'
                    route.fulfill(status=200,content_type='application/json',body=json.dumps(data));return
                route.fulfill(response=response);return
            route.continue_();return
        if path.startswith('/v1/'):
            route.fulfill(status=200,content_type='application/json',body=json.dumps({'status':'ok','documents':[],'facilities':[],'jobs':[]}));return
        route.continue_()
    page.route('**/*',routes)
    page.goto(base+DEFAULT_SETTINGS_UI_PATH+'?embedded=1')
    page.wait_for_function("document.documentElement.dataset.settingsReady==='true'")
    page.wait_for_function("document.querySelector('#nomenclatureMessage').textContent.indexOf('Читаем')<0")
    page.wait_for_function("document.querySelector('#skuGroupsMessage').textContent.indexOf('Читаем')<0")
    page.click('#addItemButton');row=page.locator('#nomenclatureRows tr[data-item-id=""]').first
    row.locator('[data-field="is_active"]').uncheck()
    row.locator('[data-field="nomenclature_name"]').fill('Synthetic iPhone 16')
    row.locator('[data-field="nm_id"]').fill('101')
    row.locator('[data-field="barcode"]').fill('4600000000101')
    row.locator('[data-field="match_key"]').evaluate("element=>element.value='clean|browser'")
    row.locator('[data-field="purchase_price_yuan"]').fill('0.00001')
    row.locator('[data-save-item]').dblclick()
    page.wait_for_selector('#nomenclatureAcceptance .ff-operation-receipt')
    assert len(writes)==1 and reads==writes,(writes,reads)
    assert entry.runtime.list_nomenclature_items()[0]['purchase_price_yuan']==0.00001
    assert 'Принято' in page.locator('#nomenclatureAcceptance').inner_text()
    identity=writes[0]
    assert page.locator('#nomenclatureAcceptance [data-state="processing"]').count()==1
    page.get_by_role('button',name='Закрыть',exact=True).click();assert page.locator('#nomenclatureAcceptance').is_hidden()
    page.reload();page.wait_for_selector('#nomenclatureAcceptance .ff-operation-receipt')
    assert writes==[identity] and reads[-1]==identity
    stored=page.evaluate("Object.entries(localStorage).filter(([key])=>key.startsWith('wbc.nomenclature.operations.'))")
    assert len(stored)==1 and json.loads(stored[0][1])==[identity]
    assert '0.00001' not in stored[0][1] and 'Synthetic' not in stored[0][1]
    # Foreign receipt with right ID may not produce green; exact read only.
    control['lose']=False;control['foreign']=True
    row=page.locator('#nomenclatureRows tr[data-item-id]').first
    row.locator('[data-field="purchase_price_yuan"]').fill('0.00002');row.locator('[data-save-item]').click()
    page.wait_for_function("document.querySelector('#nomenclatureMessage').textContent.includes('Подтверждение ещё не получено')")
    page.wait_for_load_state('networkidle')
    assert page.locator('#nomenclatureAcceptance .ff-operation-receipt').count()==0
    second=writes[-1];assert len(writes)==2 and reads[-1]==second
    row.locator('[data-save-item]').click();page.wait_for_timeout(100)
    assert len(writes)==2
    # Close/reload and unknown GET must retain same identity and forbid mutation.
    control['unknown']=True;page.reload();page.wait_for_selector('#nomenclatureAcceptance [data-state="unknown"]')
    page.wait_for_load_state('networkidle')
    assert len(writes)==2 and reads[-1]==second
    page.click('#addItemButton');page.locator('#nomenclatureRows tr[data-item-id=""]').first.locator('[data-save-item]').click()
    assert len(writes)==2
    control['unknown']=False;control['foreign']=False
    page.get_by_role('button',name='Проверить сохранение',exact=True).click()
    page.wait_for_selector('#nomenclatureAcceptance .ff-operation-receipt')
    page.wait_for_load_state('networkidle')
    assert len(writes)==2
    # Explicit CAS rejection proves unsaved; it permits corrected new input,
    # and must clear the previous receipt instead of displaying false green.
    page.wait_for_function("document.querySelector('#nomenclatureMessage').textContent.indexOf('Читаем')<0")
    current=entry.runtime.list_nomenclature_items()[0]
    entry.handle_nomenclature_patch_request(current['item_id'],{'comment':'concurrent edit'},actor='local_operator')
    row=page.locator('#nomenclatureRows tr[data-item-id="'+current['item_id']+'"]')
    row.locator('[data-field="purchase_price_yuan"]').fill('0.00003');row.locator('[data-save-item]').click()
    page.wait_for_selector('#nomenclatureAcceptance',state='hidden')
    assert page.locator('#nomenclatureMessage').get_attribute('class').split().count('error')==1
    assert len(writes)==3
    assert writes[-1] not in json.loads(page.evaluate("Object.entries(localStorage).find(([key])=>key.startsWith('wbc.nomenclature.operations.'))[1]"))
    # Numeric wire JSON (not an input string) uses no JS/Python canonical handshake.
    numeric=page.evaluate("""async () => {
      const config=JSON.parse(document.getElementById('sheet-vitrina-v1-settings-config').textContent);
      const send=async(url,options)=>{const response=await fetch(url,options);if(!response.ok)throw new Error('HTTP '+response.status);return response.json();};
      const host=OperatorNomenclature.create(config,document.getElementById('nomenclatureAcceptance'),()=>undefined,send);
      await host.restore();
      return host.mutate(config.nomenclature_path,{method:'POST',body:JSON.stringify({is_active:false,nm_id:999,
        barcode:'4600000000999',nomenclature_name:'Numeric wire',product_type:'clean',match_key:'clean|numeric',purchase_price_yuan:0.00001})});
    }""")
    assert numeric['acceptance']['durable_saved'] and numeric['acceptance']['domain']=='nomenclature'
    assert next(item for item in entry.runtime.list_nomenclature_items() if item['nm_id']==999)['purchase_price_yuan']==0.00001
    # A delayed read for an older known ID cannot replace the newer receipt.
    race=page.evaluate("""async receipt => {
      const config={...JSON.parse(document.getElementById('sheet-vitrina-v1-settings-config').textContent),operator_actor_scope:'isolated_race_fixture'};
      let resolveRead;
      const send=async(url,options)=>{
        if (!options.method) return new Promise(resolve=>{resolveRead=resolve;});
        return {acceptance:{...receipt,operation_id:JSON.parse(options.body)._operator_request_id}};
      };
      const box=document.getElementById('nomenclatureAcceptance');
      const host=OperatorNomenclature.create(config,box,()=>undefined,send);
      const first=await host.mutate(config.nomenclature_path,{method:'POST',body:'{"comment":"first"}'});
      const oldRead=host.recover(first.acceptance.operation_id);
      const second=await host.mutate(config.nomenclature_path,{method:'POST',body:'{"comment":"second"}'});
      resolveRead({operation:first.acceptance});await oldRead;
      const shown=box.querySelector('[data-ff-operation-receipt]').dataset.ffOperationReceipt;
      localStorage.removeItem('wbc.nomenclature.operations.isolated_race_fixture');
      return {shown,expected:second.acceptance.operation_id};
    }""",numeric['acceptance'])
    assert race['shown']==race['expected']
    late_unknown=page.evaluate("""async receipt => {
      const config={...JSON.parse(document.getElementById('sheet-vitrina-v1-settings-config').textContent),operator_actor_scope:'isolated_unknown_race_fixture'};
      let resolvePost,resolveRead,identity,posted=0;
      const send=async(url,options)=>{
        if (!options.method) return new Promise(resolve=>{resolveRead=resolve;});
        identity=JSON.parse(options.body)._operator_request_id;posted++;
        if (posted===1) return new Promise(resolve=>{resolvePost=resolve;});
        return {acceptance:{...receipt,operation_id:identity}};
      };
      const host=OperatorNomenclature.create(config,document.getElementById('nomenclatureAcceptance'),()=>undefined,send);
      const posting=host.mutate(config.nomenclature_path,{method:'POST',body:'{"comment":"first"}'});
      while (!identity) await new Promise(resolve=>setTimeout(resolve,0));
      const reading=host.recover(identity);
      resolvePost({acceptance:{...receipt,operation_id:identity}});await posting;
      resolveRead({});await reading;
      await host.mutate(config.nomenclature_path,{method:'POST',body:'{"comment":"second"}'});
      localStorage.removeItem('wbc.nomenclature.operations.isolated_unknown_race_fixture');
      return posted;
    }""",numeric['acceptance'])
    assert late_unknown==2
    before=list(reads)
    page.evaluate("localStorage.setItem('wbc.nomenclature.operations.other_actor',JSON.stringify(['opsku_ffffffffffffffffffffffffffffffff']))")
    page.reload();page.wait_for_selector('#nomenclatureAcceptance .ff-operation-receipt')
    page.wait_for_function("document.querySelector('#nomenclatureMessage').textContent.indexOf('Читаем')<0")
    assert 'opsku_ffffffffffffffffffffffffffffffff' not in reads[len(before):]
    page.wait_for_load_state("networkidle")
    assert not errors,errors
    page.unroute_all(behavior="wait")
    context.close()


def two_tabs_scenario(browser,base,entry):
    """Two stale real form hosts share one opaque registry and native authority."""
    context=browser.new_context(viewport={'width':1400,'height':900})
    writes=[];reads=[];errors=[];requests=[];control={'first':None,'unknown':True}
    baseline=len(entry.runtime.list_nomenclature_items())
    def routes(route):
        request=route.request;path=urlparse(request.url).path
        if path.startswith(DEFAULT_NOMENCLATURE_PATH) or path.startswith(DEFAULT_SKU_GROUPS_PATH):
            if '/operations/' in path:
                identity=path.rsplit('/',1)[1];reads.append(identity)
                if control['unknown'] and identity==control['first']:
                    route.fulfill(status=404,content_type='application/json',body='{}');return
                route.continue_();return
            if request.method in {'POST','PATCH','DELETE'}:
                identity=request.headers['x-operator-request-id'];writes.append(identity)
                requests.append({'method':request.method,'body':request.post_data_json,'headers':request.headers})
                first=control['first'] is None
                if first:control['first']=identity
                response=route.fetch()  # Real temporary native source/receipt TX.
                if first:
                    assert response.status==200,response.text()
                    route.abort();return
                route.fulfill(response=response);return
            route.continue_();return
        if path.startswith('/v1/'):
            route.fulfill(status=200,content_type='application/json',body=json.dumps({'status':'ok','documents':[],'facilities':[],'jobs':[]}));return
        route.continue_()
    context.route('**/*',routes)
    a=context.new_page();b=context.new_page()
    for page in (a,b):page.on('pageerror',lambda error:errors.append(str(error)))
    def loaded(page):
        page.goto(base+DEFAULT_SETTINGS_UI_PATH+'?embedded=1')
        page.wait_for_function("document.documentElement.dataset.settingsReady==='true'")
        page.wait_for_function("document.querySelector('#nomenclatureMessage').textContent.indexOf('Читаем')<0")
        page.wait_for_function("document.querySelector('#skuGroupsMessage').textContent.indexOf('Читаем')<0")
    def fill(page,nm,price='1'):
        page.click('#addItemButton');row=page.locator('#nomenclatureRows tr[data-item-id=""]').first
        row.locator('[data-field="is_active"]').uncheck()
        row.locator('[data-field="nomenclature_name"]').fill('Synthetic two tabs '+str(nm))
        row.locator('[data-field="nm_id"]').fill(str(nm))
        row.locator('[data-field="barcode"]').fill('4600000000'+str(nm))
        row.locator('[data-field="match_key"]').evaluate("(element,value)=>element.value=value",'clean|two-tabs'+str(nm))
        row.locator('[data-field="purchase_price_yuan"]').fill(price)
        return row
    def stored(page):
        return json.loads(page.evaluate("Object.entries(localStorage).find(([key])=>key.startsWith('wbc.nomenclature.operations.'))[1]"))
    loaded(a);loaded(b)  # Both instances initially read an empty registry.
    fill(a,201).locator('[data-save-item]').click()
    a.wait_for_function("document.querySelector('#nomenclatureMessage').textContent.includes('Подтверждение ещё не получено')")
    first=writes[0];assert len(writes)==1 and len(entry.runtime.list_nomenclature_items())==baseline+1
    assert stored(a)==[first]
    row=fill(b,202);row.locator('[data-save-item]').click()
    b.wait_for_function("document.querySelector('#nomenclatureMessage').classList.contains('error')")
    assert 'Результат предыдущей операции пока неизвестен' in b.locator('#nomenclatureMessage').inner_text(),(b.locator('#nomenclatureMessage').inner_text(),writes,stored(b),errors)
    b.wait_for_load_state('networkidle')
    assert writes==[first] and stored(b)==[first]
    assert len(entry.runtime.list_nomenclature_items())==baseline+1
    # Storage, pageshow/focus and visible-tab recovery are GET-only.
    before=list(writes)
    b.evaluate("window.dispatchEvent(new Event('pageshow'));window.dispatchEvent(new Event('focus'));document.dispatchEvent(new Event('visibilitychange'))")
    b.wait_for_load_state('networkidle');assert writes==before
    a.close();before_reads=len(reads);loaded(b)
    b.wait_for_selector('#nomenclatureAcceptance [data-state="unknown"]')
    b.wait_for_load_state('networkidle')
    assert first in reads[before_reads:] and writes==[first] and stored(b)==[first]
    # Actual exact GET resolves A; only an explicit new click admits B.
    control['unknown']=False
    b.get_by_role('button',name='Проверить сохранение',exact=True).click()
    b.wait_for_selector('#nomenclatureAcceptance .ff-operation-receipt');b.wait_for_load_state('networkidle')
    assert writes==[first]
    fill(b,202).locator('[data-save-item]').click()
    b.wait_for_function("JSON.parse(Object.entries(localStorage).find(([key])=>key.startsWith('wbc.nomenclature.operations.'))[1]).length===2")
    b.wait_for_function("document.querySelector('#nomenclatureMessage').classList.contains('success')")
    b.wait_for_load_state('networkidle')
    second=writes[-1];assert len(writes)==2 and second!=first and stored(b)==[first,second]
    assert len(entry.runtime.list_nomenclature_items())==baseline+2
    # Definitive native validation removes only its own refused ID; correction
    # is an explicit new identity, without replaying any accepted command.
    current=next(item for item in entry.runtime.list_nomenclature_items() if item['nm_id']==202)
    entry.handle_nomenclature_patch_request(current['item_id'],{'comment':'concurrent native edit'},actor='local_operator')
    row=b.locator('#nomenclatureRows tr[data-item-id="'+current['item_id']+'"]')
    row.locator('[data-field="purchase_price_yuan"]').fill('2');row.locator('[data-save-item]').click()
    b.wait_for_function("document.querySelector('#nomenclatureMessage').classList.contains('error')")
    b.wait_for_load_state('networkidle');rejected=writes[-1]
    assert len(writes)==3 and stored(b)==[first,second]
    assert len(entry.runtime.list_nomenclature_items())==baseline+2
    loaded(b);b.wait_for_selector('#nomenclatureAcceptance .ff-operation-receipt')
    row=b.locator('#nomenclatureRows tr[data-item-id="'+current['item_id']+'"]')
    row.locator('[data-field="purchase_price_yuan"]').fill('2');row.locator('[data-save-item]').click()
    b.wait_for_function("document.querySelector('#nomenclatureMessage').classList.contains('success')")
    b.wait_for_load_state('networkidle');third=writes[-1]
    assert len(writes)==4 and third!=rejected and stored(b)==[first,second,third]
    assert len(entry.runtime.list_nomenclature_items())==baseline+2
    # Wait behind an actor WebLock, then change both native source and GUI-like
    # revision/options. The old click must retain its exact old CAS/body/header.
    before=entry.runtime.load_nomenclature_item(current['item_id'])
    b.evaluate("""args=>{
      const config={...JSON.parse(document.getElementById('sheet-vitrina-v1-settings-config').textContent),operator_actor_scope:'isolated_snapshot_fixture'};
      window.snapshotConfig=config;window.snapshotRevision=args.revision;
      window.snapshotOptions={method:'PATCH',headers:{'Content-Type':'application/json','X-Snapshot':'CLICKED'},body:JSON.stringify({comment:'clicked before lock'})};
      const send=async(url,options)=>{
        const response=await fetch(url,options);const value=await response.json();
        if(!response.ok){const error=new Error(value.error);error.httpStatus=response.status;error.sourceNotSaved=value.source_not_saved===true;error.operationId=value.operation_id;throw error;}
        return value;
      };
      window.snapshotHost=OperatorNomenclature.create(config,document.getElementById('nomenclatureAcceptance'),()=>window.snapshotRevision,send);
      window.snapshotLock=navigator.locks.request('wbc.nomenclature.operations.'+config.operator_actor_scope+':mutation',async()=>{await new Promise(resolve=>window.releaseSnapshotLock=resolve);});
    }""",{'revision':before['operator_source_revision']})
    b.wait_for_function('Boolean(window.releaseSnapshotLock)')
    b.evaluate("""url=>{window.snapshotPost=window.snapshotHost.mutate(url,window.snapshotOptions).then(()=>window.snapshotResult={accepted:true},error=>window.snapshotResult={error:error.message});}""",DEFAULT_NOMENCLATURE_PATH+'/'+current['item_id'])
    assert len(writes)==4
    entry.handle_nomenclature_patch_request(current['item_id'],{'comment':'native concurrent queued edit'},actor='local_operator')
    after=entry.runtime.load_nomenclature_item(current['item_id'])
    b.evaluate("""revision=>{window.snapshotRevision=revision;window.snapshotOptions.method='POST';window.snapshotOptions.headers['X-Snapshot']='LATE';window.snapshotOptions.body=JSON.stringify({comment:'late options'});window.releaseSnapshotLock();}""",after['operator_source_revision'])
    b.wait_for_function('Boolean(window.snapshotResult)');b.wait_for_load_state('networkidle')
    result=b.evaluate('window.snapshotResult');assert not result.get('accepted') and 'source' in result['error'].lower(),result
    assert len(writes)==5 and requests[-1]['method']=='PATCH' and requests[-1]['headers']['x-snapshot']=='CLICKED'
    assert requests[-1]['body']['_operator_expected_revision']==before['operator_source_revision']
    assert requests[-1]['body']['comment']=='clicked before lock'
    assert entry.runtime.load_nomenclature_item(current['item_id'])['comment']=='native concurrent queued edit'
    assert b.locator('#nomenclatureAcceptance').is_hidden()
    assert stored(b)==[first,second,third]
    # FormData/File and a nested revision map are also snapshotted before the
    # first await. This isolated send checks browser wire data only, no import.
    multipart=b.evaluate("""async receipt=>{
      const config={...window.snapshotConfig,operator_actor_scope:'isolated_multipart_snapshot_fixture'};
      let release,ready;const acquired=new Promise(resolve=>ready=resolve);
      const held=navigator.locks.request('wbc.nomenclature.operations.'+config.operator_actor_scope+':mutation',async()=>{ready();await new Promise(resolve=>release=resolve);});
      await acquired;
      const revision={row:'BEFORE'},form=new FormData();form.append('file',new File(['clicked bytes'],'clicked.xlsx'));form.append('intent','CLICKED');
      const options={method:'POST',headers:{'X-Snapshot':'CLICKED'},body:form};let wire;
      const host=OperatorNomenclature.create(config,document.getElementById('nomenclatureAcceptance'),()=>revision,async(url,opts)=>{
        wire={revision:JSON.parse(opts.body.get('operator_expected_revision')),file:opts.body.get('file').name,bytes:await opts.body.get('file').text(),intent:opts.body.get('intent'),header:opts.headers['X-Snapshot']};
        return {acceptance:{...receipt,operation_id:opts.headers['X-Operator-Request-ID']}};
      });
      const posting=host.mutate(config.nomenclature_path+'/import',options);
      revision.row='LATE';form.set('file',new File(['late bytes'],'late.xlsx'));form.set('intent','LATE');options.headers['X-Snapshot']='LATE';
      release();await held;await posting;
      localStorage.removeItem('wbc.nomenclature.operations.'+config.operator_actor_scope);
      localStorage.removeItem('wbc.nomenclature.operations.'+window.snapshotConfig.operator_actor_scope);
      return wire;
    }""",entry.handle_nomenclature_operation_request(third,actor='local_operator')['operation'])
    assert multipart=={'revision':{'row':'BEFORE'},'file':'clicked.xlsx','bytes':'clicked bytes','intent':'CLICKED','header':'CLICKED'},multipart
    before_reads=len(reads);loaded(b);b.wait_for_selector('#nomenclatureAcceptance .ff-operation-receipt');b.wait_for_load_state('networkidle')
    assert stored(b)==[first,second,third] and {first,second,third}<=set(reads[before_reads:]) and len(writes)==5
    assert not errors,errors
    print(json.dumps({'two_tabs_native':{'source_POSTs_while_A_unknown':1,'retained_accepted_IDs_after_close_reload':3,'total_source_POSTs':len(writes),'accepted_new_rows':2,'received_negative_retries':True},
        'queued_native_CAS':{'clicked_revision_preserved':True,'late_revision_not_used':True,'native_rejected_old_source':True,'clicked_body_and_header_preserved':True},
        'multipart_browser_only_snapshot':multipart,'pageerrors':errors},sort_keys=True))
    context.unroute_all(behavior='wait');context.close()

if __name__=='__main__':run()
