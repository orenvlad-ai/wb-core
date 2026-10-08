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
                try:scenario(browser,f'http://127.0.0.1:{config.port}',entry)
                finally:browser.close()
        finally:server.shutdown();server.server_close();thread.join(timeout=5)
    print('operator_nomenclature_browser_smoke: OK (real HTTP source, tiny CNY, lost response, reload exact GET, foreign domain, no resubmit, actor scope)')


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

if __name__=='__main__':run()
