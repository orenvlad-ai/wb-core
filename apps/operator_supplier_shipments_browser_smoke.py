"""Actual Chromium/native HTTP: lost response, closed tab, exact supplier recovery."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from contextlib import closing
from unittest.mock import patch
from playwright.sync_api import sync_playwright, expect
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from apps.operator_supplier_shipments_smoke import seed
from apps.operator_supplier_shipments_http_smoke import server_for,stop,PATH
from apps.sheet_vitrina_v1_supplier_shipments_http_smoke import _build_invoice_fixture,TARGET_FACILITY_ID
from packages.adapters.registry_upload_http_entrypoint import DEFAULT_SHEET_SUPPLIER_UI_PATH
from packages.application import operator_supplier_shipments as receipts
from packages.adapters import registry_upload_http_entrypoint as http


def wire_and_prewrite_refusal():
    """Actual numeric JSON transport and safe create guard, including lost replies."""
    with TemporaryDirectory(prefix='operator-supplier-wire-') as raw:
        rt,entry,payload=seed(raw);server,thread,base=server_for(entry)
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch();context=browser.new_context();page=context.new_page()
                payload.pop('request_id');payload['approx_yuan_rate']=0.00001
                numeric_writes=[]
                def lose_numeric(route):
                    if route.request.method!='POST':
                        route.continue_();return
                    numeric_writes.append(route.request.post_data_json['request_id'])
                    assert route.fetch().status==200
                    route.abort('failed')
                context.route('**/supplier-shipments',lose_numeric)
                page.goto(base+DEFAULT_SHEET_SUPPLIER_UI_PATH)
                result=page.evaluate('''async({path,payload})=>{
                    const node=document.createElement('div');document.body.appendChild(node);
                    let locked=false;
                    const component=SupplierSourceAcceptance.create({path,scope:'local_operator',container:node,onLock:v=>locked=v,onRecovered:async()=>{}});
                    const saved=await component.submit(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)},{action:'create',entity_id:''});
                    const read=await(await fetch(path+'?request_id='+saved.request_id)).json();
                    return {locked,pending:localStorage.getItem('wbc_supplier_source_pending_v1:local_operator'),read,green:node.querySelectorAll('.ff-operation-check').length};
                }''',{'path':PATH,'payload':payload})
                assert not result['locked'] and result['pending'] is None and result['green']==1,result
                assert result['read']['status']=='accepted' and result['read']['wire_digest']!=result['read']['payload_digest']
                # Preserve native rate quantization; this regression concerns
                # exact request recovery, not a change in source arithmetic.
                with closing(receipts.readonly(rt.db_path)) as conn:
                    row=conn.execute(f'SELECT source_json FROM {receipts.TABLE} WHERE request_id=?',(numeric_writes[0],)).fetchone()
                    assert result['read']['shipment']['approx_yuan_rate']==json.loads(row['source_json'])['header']['approx_yuan_rate']
                page.close();page=context.new_page();page.goto(base+DEFAULT_SHEET_SUPPLIER_UI_PATH)
                assert len(numeric_writes)==1
                context.close()
                with patch.object(http,'_web_auth_config',return_value={'configured':True,'enabled':True}), \
                     patch.object(http,'_authenticated_web_user',return_value={'username':'supplier-fixture','role':'supplier','allowed_sections':[]}):
                    context=browser.new_context();page=context.new_page();writes=[];statuses=[]
                    def lose_safe_reply(route):
                        if route.request.method=='POST':
                            writes.append(route.request.post_data_json['request_id'])
                            response=route.fetch();statuses.append(response.status);route.abort('failed')
                        else:route.continue_()
                    context.route('**/supplier-shipments',lose_safe_reply)
                    page.goto(base+DEFAULT_SHEET_SUPPLIER_UI_PATH)
                    page.locator('#addShipmentButton').click()
                    page.locator('#invoiceFileInput').set_input_files({'name':'safe-refusal.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':_build_invoice_fixture()})
                    expect(page.locator('#registryMessage')).to_contain_text('Invoice parsed.',timeout=10000)
                    page.locator('#shipmentDateInput').fill('2026-10-10')
                    page.locator('#actualFfAcceptanceDateInput').fill('2026-10-08')
                    page.locator('#saveShipmentButton').click()
                    expect(page.locator('#cardMessage')).to_contain_text('Заказ не принят.',timeout=10000)
                    expect(page.locator('#saveShipmentButton')).to_be_enabled()
                    expect(page.locator('#supplierSourceAcceptance')).to_be_hidden()
                    assert statuses==[400] and len(writes)==1
                    assert page.evaluate("Object.keys(localStorage).filter(k=>k.startsWith('wbc_supplier_source_pending_v1:')).length")==0
                    first=page.evaluate('async path=>(await(await fetch(path)).json())',PATH+'?request_id='+writes[0])
                    assert first['status']=='rejected' and first['wire_digest'] and first['acceptance'] is None
                    with closing(receipts.readonly(rt.db_path)) as conn:
                        assert conn.execute(f'SELECT count(*) FROM {receipts.TABLE}').fetchone()[0]==1
                        assert conn.execute(f'SELECT count(*) FROM {receipts.REJECTIONS}').fetchone()[0]==1
                    # Correct the existing form, new identity; refusal wrote no
                    # supplier/invoice, and the accepted retry is never resent.
                    page.locator('#actualFfAcceptanceDateInput').fill('')
                    page.locator('#saveShipmentButton').click()
                    expect(page.locator('#supplierSourceAcceptance .ff-operation-check')).to_be_visible(timeout=10000)
                    assert statuses==[400,200] and len(set(writes))==2
                    page.close();page=context.new_page();page.goto(base+DEFAULT_SHEET_SUPPLIER_UI_PATH)
                    expect(page.locator('#supplierRegistryTable')).to_be_visible()
                    assert len(writes)==2
                    with closing(receipts.readonly(rt.db_path)) as conn:
                        assert conn.execute(f'SELECT count(*) FROM {receipts.TABLE}').fetchone()[0]==2
                        assert conn.execute(f'SELECT count(*) FROM {receipts.REJECTIONS}').fetchone()[0]==1
                        assert conn.execute('SELECT count(*) FROM sheet_vitrina_v1_supplier_shipments').fetchone()[0]==2
                    context.close()
                browser.close()
        finally:stop(server,thread)


def embedded_native_confirm(screenshot_path=None):
    """Real supplier form/native one POST in two scrolled same-origin frames."""
    with TemporaryDirectory(prefix='supplier-modal-native-') as raw:
        rt,entry,_=seed(raw);server,thread,base=server_for(entry)
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch();context=browser.new_context(viewport={'width':1100,'height':800});page=context.new_page();writes=[];errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                context.route(base+'/modal-shell',lambda r:r.fulfill(content_type='text/html',body='<body style="height:7000px"><iframe id="middle" src="/modal-middle" style="height:5000px;width:1000px"></iframe></body>'))
                context.route(base+'/modal-middle',lambda r:r.fulfill(content_type='text/html',body='<body style="height:5000px"><iframe id="supplier" src="'+DEFAULT_SHEET_SUPPLIER_UI_PATH+'" style="height:4200px;width:950px"></iframe></body>'))
                page.goto(base+'/modal-shell',wait_until='domcontentloaded')
                page.add_style_tag(path=str(ROOT/'packages/adapters/templates/sheet_vitrina_v1_ui_system.css'))
                expect(page.frame_locator('#middle').frame_locator('#supplier').locator('#supplierRegistryTable')).to_be_visible(timeout=10000)
                frame=page.frame(url=base+DEFAULT_SHEET_SUPPLIER_UI_PATH);assert frame is not None
                expect(frame.locator('#supplierRegistryTable')).to_be_visible()
                frame.evaluate("document.body.style.minHeight='8500px'")
                frame.locator('#addShipmentButton').click()
                frame.locator('#invoiceFileInput').set_input_files({'name':'modal-synthetic.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':_build_invoice_fixture()})
                expect(frame.locator('#shipmentCard')).to_be_visible()
                expect(frame.locator('#cardMessage')).to_contain_text('Invoice распознан',timeout=10000)
                frame.locator('#shipmentDateInput').fill('2026-10-10');frame.locator('#targetFacilityInput').select_option(TARGET_FACILITY_ID)
                def confirm(route):
                    if route.request.method!='POST':route.continue_();return
                    writes.append(route.request.post_data_json['request_id']);response=route.fetch();assert response.status==200
                    frame.evaluate("window.scrollTo({top:1400,behavior:'instant'})");page.evaluate("window.scrollTo({top:2200,behavior:'instant'})")
                    route.fulfill(response=response)
                context.route('**/supplier-shipments',confirm)
                frame.locator('#saveShipmentButton').click()
                dialog=page.locator('dialog.ff-operation-popup[open]')
                expect(dialog).to_be_visible(timeout=10000);expect(dialog).to_contain_text('Документ сохранён.')
                expect(dialog.locator('.ff-operation-check')).to_be_visible()
                # The receipt is presented before the native caller finishes
                # card/registry reflow. Snapshot the completed source layout.
                expect(frame.locator('#saveShipmentButton')).to_be_enabled()
                expect(frame.locator('#shipmentRows')).to_have_attribute('data-registry-state','loaded_with_rows')
                expect(frame.locator('#cardMessage')).to_contain_text('Заказ сохранён.')
                box=dialog.bounding_box();assert box and abs(box['x']+box['width']/2-550)<2 and abs(box['y']+box['height']/2-400)<2,box
                # Native card recovery can reflow the source document; both
                # frames remain scrolled, and modal dismissal must not move them.
                parent_scroll=page.evaluate('scrollY');child_scroll=frame.evaluate('scrollY')
                print('embedded-scroll-baseline: '+str({'parent':parent_scroll,'child':child_scroll,'saveButtonDisabled':frame.locator('#saveShipmentButton').is_disabled(),'cardMessage':frame.locator('#cardMessage').inner_text(),'registryState':frame.locator('#shipmentRows').get_attribute('data-registry-state')}),flush=True)
                assert parent_scroll==2200 and child_scroll>1000
                if screenshot_path is not None:
                    page.screenshot(path=str(screenshot_path))
                assert frame.locator('#supplierSourceAcceptance').count()==1 and frame.locator('dialog.ff-operation-popup').count()==0
                assert len(writes)==1 and len(set(writes))==1
                with closing(receipts.readonly(rt.db_path)) as conn:
                    assert conn.execute(f'SELECT count(*) FROM {receipts.TABLE}').fetchone()[0]==1
                    assert conn.execute('SELECT count(*) FROM sheet_vitrina_v1_supplier_shipments').fetchone()[0]==1
                dialog.get_by_role('button',name='Закрыть').click();expect(dialog).to_have_count(0)
                print('embedded-scroll-after-close: '+str({'parent':page.evaluate('scrollY'),'child':frame.evaluate('scrollY'),'saveButtonDisabled':frame.locator('#saveShipmentButton').is_disabled(),'cardMessage':frame.locator('#cardMessage').inner_text(),'registryState':frame.locator('#shipmentRows').get_attribute('data-registry-state')}),flush=True)
                assert frame.evaluate("document.activeElement.id")=='saveShipmentButton'
                assert page.evaluate('scrollY')==parent_scroll and frame.evaluate('scrollY')==child_scroll
                expect(frame.locator('#saveShipmentButton')).to_be_enabled()
                assert len(writes)==1 and not errors,errors
                context.close();browser.close()
        finally:stop(server,thread)


def main(screenshot_path=None):
    with TemporaryDirectory(prefix='operator-supplier-browser-') as raw:
        rt,entry,_=seed(raw);server,thread,base=server_for(entry)
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch();context=browser.new_context(viewport={'width':1440,'height':1100})
                writes=[];foreign=True;errors=[];allow_rejection=False
                def lose_response(route):
                    if route.request.method in {'POST','PATCH','DELETE'}:
                        writes.append(route.request.method);response=route.fetch()
                        assert response.status==200 or (allow_rejection and response.status==400),response.text()
                        route.abort('failed')
                    else:route.continue_()
                def recovery(route):
                    response=route.fetch();payload=response.json()
                    if foreign:payload['request_id']='foreign-request-id'
                    route.fulfill(status=response.status,content_type='application/json',body=json.dumps(payload))
                context.route('**/supplier-shipments',lose_response)
                context.route('**/supplier-shipments/*/price-check',lose_response)
                context.route('**/supplier-shipments?request_id=*',recovery)
                page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                url=base+DEFAULT_SHEET_SUPPLIER_UI_PATH+'?embedded=operator'
                page.goto(url,wait_until='domcontentloaded')
                page.locator('#addShipmentButton').click()
                page.locator('#invoiceFileInput').set_input_files({'name':'synthetic.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':_build_invoice_fixture()})
                expect(page.locator('#shipmentCard')).to_be_visible(timeout=10000)
                expect(page.locator('#cardMessage')).to_contain_text('Invoice распознан',timeout=10000)
                page.locator('#shipmentDateInput').fill('2026-10-10')
                page.locator('#targetFacilityInput').select_option(TARGET_FACILITY_ID)
                page.locator('#saveShipmentButton').click()
                expect(page.locator('#supplierSourceAcceptance')).to_contain_text('Проверяем сохранение',timeout=10000)
                expect(page.locator('#saveShipmentButton')).to_be_disabled()
                assert page.locator('#supplierSourceAcceptance .ff-operation-check').count()==0
                assert writes==['POST']
                retained=page.evaluate("Object.keys(localStorage).filter(key=>key.startsWith('wbc_supplier_source_pending_v1:')).map(key=>JSON.parse(localStorage.getItem(key)))")
                assert len(retained)==1 and set(retained[0])=={'request_id','payload_digest','action','entity_id'}
                request_id=retained[0]['request_id']
                page.close()
                # A new tab in the same authenticated browser origin keeps the
                # opaque ID and only GETs. A foreign answer cannot unlock it.
                page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(url,wait_until='domcontentloaded')
                expect(page.locator('#supplierSourceAcceptance')).to_contain_text('Проверяем сохранение',timeout=10000)
                expect(page.locator('#saveShipmentButton')).to_be_disabled()
                assert writes==['POST']
                foreign=False
                page.locator('#supplierSourceAcceptance').get_by_role('button',name='Проверить статус').click()
                expect(page.locator('#supplierSourceAcceptance .ff-operation-check')).to_be_visible(timeout=10000)
                expect(page.locator('#supplierSourceAcceptance')).to_contain_text('Документ сохранён.')
                expect(page.locator('#supplierSourceAcceptance')).to_contain_text('Ожидает обработки')
                assert page.locator('#supplierSourceAcceptance').get_by_text('Обработано',exact=True).count()==0
                expect(page.locator('#shipmentCard')).to_be_visible()
                expect(page.locator('#saveShipmentButton')).to_be_enabled()
                assert writes==['POST']
                with closing(receipts.readonly(rt.db_path)) as conn:
                    first=conn.execute(f'SELECT * FROM {receipts.TABLE}').fetchone()
                    assert first['request_id']==request_id
                    assert conn.execute('SELECT count(*) FROM sheet_vitrina_v1_supplier_shipments').fetchone()[0]==1
                page.locator('dialog.ff-operation-popup').get_by_role('button',name='Закрыть').click()
                page.locator('#priceCheckButton').click()
                expect(page.locator('#supplierSourceAcceptance')).to_contain_text('Сохранение завершено. Расчёт не требуется.',timeout=10000)
                expect(page.locator('#supplierSourceAcceptance')).to_contain_text('Проверка цен заказа')
                assert writes==['POST','POST']
                with closing(receipts.readonly(rt.db_path)) as conn:
                    assert conn.execute(f'SELECT count(*) FROM {receipts.TABLE}').fetchone()[0]==2
                    assert conn.execute(f'SELECT count(DISTINCT request_id) FROM {receipts.TABLE}').fetchone()[0]==2
                page.locator('#supplierSourceAcceptance').get_by_role('button',name='Закрыть').focus()
                page.keyboard.press('Enter');expect(page.locator('#supplierSourceAcceptance')).to_be_hidden()
                # A definite native source refusal is recoverable even when
                # its HTTP400 reply is lost. It never clears arbitrary unknown.
                with closing(receipts.readonly(rt.db_path)) as conn:
                    shipment=conn.execute(f'SELECT shipment_id FROM {receipts.TABLE} LIMIT 1').fetchone()[0]
                rt.archive_supplier_shipment(shipment_id=shipment,archived_at='2026-10-08T11:00:00Z')
                allow_rejection=True
                page.locator('#priceCheckButton').click()
                expect(page.locator('#cardMessage')).to_contain_text('supplier shipment not found',timeout=10000)
                expect(page.locator('#supplierSourceAcceptance')).to_be_hidden()
                expect(page.locator('#saveShipmentButton')).to_be_enabled()
                assert writes==['POST','POST','POST']
                page.reload(wait_until='domcontentloaded')
                expect(page.locator('#supplierSourceAcceptance')).to_be_hidden()
                assert writes==['POST','POST','POST']
                with closing(receipts.readonly(rt.db_path)) as conn:
                    assert conn.execute(f'SELECT count(*) FROM {receipts.REJECTIONS}').fetchone()[0]==1
                    assert conn.execute(f'SELECT count(*) FROM {receipts.TABLE}').fetchone()[0]==2
                assert not errors,errors
                context.close()
                # The actual supplier-role form uses its native safe grants and
                # its own principal recovery key; it never receives cost scope.
                with patch.object(http,'_web_auth_config',return_value={'configured':True,'enabled':True}), \
                     patch.object(http,'_authenticated_web_user',return_value={'username':'supplier-fixture','role':'supplier','allowed_sections':[]}):
                    context=browser.new_context();page=context.new_page()
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    page.goto(base+DEFAULT_SHEET_SUPPLIER_UI_PATH,wait_until='domcontentloaded')
                    expect(page.locator('#supplierRegistryTable')).to_be_visible()
                    page.locator('#addShipmentButton').click()
                    page.locator('#invoiceFileInput').set_input_files({'name':'safe-synthetic.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':_build_invoice_fixture()})
                    expect(page.locator('#registryMessage')).to_contain_text('Invoice parsed.',timeout=10000)
                    page.locator('#shipmentDateInput').fill('2026-10-10')
                    page.locator('#saveShipmentButton').click()
                    expect(page.locator('#supplierSourceAcceptance .ff-operation-check')).to_be_visible(timeout=10000)
                    expect(page.locator('#shipmentCard')).to_be_visible()
                    assert not errors,errors
                browser.close()
            wire_and_prewrite_refusal()
            embedded_native_confirm(screenshot_path)
            print('Chromium actual supplier lost POST/foreign read/new-tab persistent same ID/no resend; shared green pending vs source-only price; numeric wire identity; prewrite safe refusal+corrected new-ID retry; exact native rejected GET releases form without green; keyboard close: OK')
        finally:stop(server,thread)


if __name__=='__main__':main()
