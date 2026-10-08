"""Actual native HTTP/Chromium factual confirmation with lost reply and new tab."""
from contextlib import closing
from pathlib import Path
import json,sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.operator_supplier_factual_dates_smoke import fixture,SHIPMENT_ID
from apps.operator_supplier_shipments_http_smoke import server_for,stop,request,PATH
from packages.adapters import registry_upload_http_entrypoint as http
from packages.application import operator_supplier_factual_dates as facts,operator_supplier_shipments as source


def main():
    with TemporaryDirectory(prefix='operator-factual-browser-') as raw:
        runtime,entry=fixture(raw);server,thread,base=server_for(entry)
        status_path=PATH+'/'+SHIPMENT_ID+'/factual-dates/status'
        confirm_path=PATH+'/'+SHIPMENT_ID+'/factual-dates/confirm'
        try:
            status,preview=request(base,PATH+'/'+SHIPMENT_ID+'/factual-dates/preview','POST',{'actual_shipment_date':'2026-06-26'})
            assert status==200
            with sync_playwright() as pw:
                browser=pw.chromium.launch();context=browser.new_context();page=context.new_page();writes=[];foreign=True
                def lose_reply(route):
                    writes.append(route.request.post_data_json['request_id'])
                    assert route.fetch().status==202
                    route.abort('failed')
                def exact_read(route):
                    response=route.fetch();payload=response.json()
                    if foreign and payload.get('acceptance'):
                        payload['acceptance']['source_ref']['native_id']='foreign-correction'
                    route.fulfill(status=response.status,content_type='application/json',body=json.dumps(payload))
                context.route('**/factual-dates/confirm',lose_reply)
                context.route('**/factual-dates/status?request_id=*',exact_read)
                page.goto(base+http.DEFAULT_SHEET_SUPPLIER_UI_PATH)
                page.evaluate('''async({path,url,token,shipment})=>{
                    const node=document.createElement('div');node.id='factual-test';document.body.appendChild(node);
                    window.factLocked=false;
                    window.factComponent=SupplierSourceAcceptance.create({path,scope:JSON.parse(document.getElementById('sheet-vitrina-v1-supplier-config').textContent).user_config_key || 'local_operator',container:node,onLock:v=>window.factLocked=v,onRecovered:async()=>{}});
                    try{await factComponent.submit(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirmation_token:token})},{action:'factual_date',entity_id:shipment});}catch(e){}
                }''',{'path':PATH,'url':confirm_path,'token':preview['confirmation_token'],'shipment':SHIPMENT_ID})
                expect(page.locator('#factual-test')).to_contain_text('Проверяем сохранение')
                assert page.evaluate('factLocked') and len(writes)==1
                page.close();page=context.new_page();foreign=False
                page.goto(base+http.DEFAULT_SHEET_SUPPLIER_UI_PATH)
                expect(page.locator('#supplierSourceAcceptance .ff-operation-check')).to_be_visible(timeout=10000)
                expect(page.locator('#supplierSourceAcceptance')).to_contain_text('Изменение фактической даты')
                assert len(writes)==1
                assert runtime.load_supplier_shipment(SHIPMENT_ID)['header']['actual_shipment_date']=='2026-06-25'
                with closing(source.readonly(runtime.db_path)) as conn:
                    assert conn.execute(f'SELECT count(*) FROM {facts.TABLE}').fetchone()[0]==1
                    assert conn.execute(f'SELECT count(*) FROM {facts.JOB}').fetchone()[0]==1
                response=request(base,status_path+'?request_id='+writes[0])[1]
                assert response['acceptance']['durable_saved'] and not response['acceptance']['physical_applied']
                native_id=response['acceptance']['operation_id']
                facts.consume(runtime,block=entry.supplier_shipment_factual_correction_block)
                complete=request(base,status_path+'?request_id='+writes[0])[1]
                assert complete['acceptance']['operation_id']==native_id and complete['acceptance']['physical_applied']
                assert not complete['acceptance']['processing']['complete']
                assert complete['shipment']['actual_shipment_date']=='2026-06-26'
                page.goto(base+http.DEFAULT_SHEET_SUPPLIER_UI_PATH+'?operation_id='+native_id)
                expect(page.locator('#supplierSourceAcceptance .ff-operation-check')).to_be_visible()
                assert request(base,PATH+'?operation_id='+native_id)[1]['acceptance']['operation_id']==native_id
                # Factual operation has its own native Supply grant; Supplier
                # role cannot read this adapter through general source grants.
                with patch.object(http,'_web_auth_config',return_value={'configured':True,'enabled':True}), \
                     patch.object(http,'_authenticated_web_user',return_value={'username':'supplier-fixture','role':'supplier','allowed_sections':[]}):
                    with patch.object(entry,'handle_supplier_factual_dates_status_request',side_effect=AssertionError('permission checked too late')):
                        assert request(base,status_path+'?request_id='+writes[0])[0]==403
                    assert request(base,confirm_path,'POST',{'confirmation_token':preview['confirmation_token'],'request_id':'forbidden-factual-id'})[0]==403
                    with patch.object(entry,'handle_supplier_operator_operation_read',side_effect=AssertionError('operation permission checked too late')):
                        assert request(base,PATH+'?operation_id='+native_id)[0]==403
                context.close();browser.close()
        finally:stop(server,thread)
    superseded_link()
    print('Actual Chromium/native factual lost reply, exact native-ID guard, new-tab GET/no resend; native worker same job apply; Supply grant before read: OK')


def superseded_link():
    from apps.operator_supplier_shipments_smoke import seed
    with TemporaryDirectory(prefix='supplier-operation-link-') as raw:
        runtime,entry,payload=seed(raw);server,thread,base=server_for(entry)
        try:
            first=request(base,PATH,'POST',payload)[1]
            shipment=first['shipment_id'];old=first['acceptance']['operation_id']
            second=request(base,PATH+'/'+shipment,'PATCH',{'request_id':'supplier-operation-link-new','shipment_date':'2026-10-11'})[1]
            new=second['acceptance']['operation_id']
            native=request(base,PATH+'?operation_id='+old)[1]
            assert native['acceptance']['state']=='needs_attention'
            assert native['acceptance']['processing']['terminal'] and not native['acceptance']['processing']['complete']
            assert native['acceptance']['processing']['superseded_by']['operation_id']==new
            with sync_playwright() as pw:
                browser=pw.chromium.launch();page=browser.new_page();writes=[]
                page.on('request',lambda r:writes.append(r.method) if r.method in {'POST','PATCH','DELETE'} else None)
                page.goto(base+http.DEFAULT_SHEET_SUPPLIER_UI_PATH+'?operation_id='+old)
                expect(page.locator('#supplierSourceAcceptance')).to_contain_text('Эту версию заменили последующими изменениями.')
                page.locator('#supplierSourceAcceptance').get_by_role('link',name='Следующая операция').click()
                expect(page.locator('#supplierSourceAcceptance [data-ff-operation-receipt]')).to_have_attribute('data-ff-operation-receipt',new)
                assert not writes
                browser.close()
            with patch.object(http,'_web_auth_config',return_value={'configured':True,'enabled':True}),patch.object(http,'_authenticated_web_user',return_value={'username':'other-supplier','role':'supplier','allowed_sections':[]}):
                assert request(base,PATH+'?operation_id='+new)[1]['status']=='unknown'
        finally:stop(server,thread)
    print('Actual Chromium superseded terminal + exact next-operation link; GET-only/no mutation; foreign actor cannot read: OK')


if __name__=='__main__':main()
