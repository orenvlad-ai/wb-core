"""Actual Chromium/native HTTP financial lost reply, close-tab and exact refusal."""
from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import closing
import json,sys
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.operator_supplier_financial_http_smoke import fixture,preview
from apps.operator_supplier_shipments_http_smoke import server_for,stop,request,PATH
from packages.adapters import registry_upload_http_entrypoint as http
from packages.application import operator_supplier_financial as financial,operator_supplier_shipments as source


def main():
    with TemporaryDirectory(prefix='financial-browser-') as raw:
        rt,entry=fixture(raw);server,thread,base=server_for(entry)
        try:
            native=preview(entry,'invoice-103.pdf');url=PATH+'/source/financial-documents/confirm-upload'
            with sync_playwright() as pw:
                browser=pw.chromium.launch();context=browser.new_context();page=context.new_page();writes=[];foreign=True;lose_maintenance=False
                def lose_reply(route):
                    writes.append(route.request.post_data_json['request_id']);response=route.fetch();assert response.status in (200,423);
                    if response.status==423 and not lose_maintenance:route.fulfill(response=response)
                    else:route.abort('failed')
                def exact(route):
                    response=route.fetch();result=response.json()
                    if foreign and result.get('acceptance'):result['acceptance']['source_ref']['entity_id']='foreign-shipment'
                    route.fulfill(status=response.status,content_type='application/json',body=json.dumps(result))
                context.route('**/financial-documents/confirm-upload',lose_reply)
                context.route('**/financial-documents?request_id=*',exact)
                page.goto(base+http.DEFAULT_SHEET_SUPPLIER_UI_PATH)
                def submit(token):
                    return page.evaluate('''async({path,url,token})=>{
                        const action=window.testComponent.classify(url,'POST');
                        try{return await testComponent.submit(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirmation_token:token})},action);}catch(e){return {error:e.message};}
                    }''',{'path':PATH,'url':url,'token':token})
                page.evaluate('''path=>{const node=document.createElement('div');node.id='financial-test';document.body.appendChild(node);
                    window.testLocked=false;window.testComponent=SupplierSourceAcceptance.create({path,scope:JSON.parse(document.getElementById('sheet-vitrina-v1-supplier-config').textContent).user_config_key || 'local_operator',container:node,onLock:v=>testLocked=v,onRecovered:async()=>{throw Error('financial recovery replaced editable card');}});}
                ''',PATH)
                assert submit(native['confirmation_token']).get('error')
                expect(page.locator('#financial-test')).to_contain_text('Проверяем сохранение')
                assert page.evaluate('testLocked') and len(writes)==1
                retained=page.evaluate('Object.entries(localStorage).find(([k])=>k.startsWith("wbc_supplier_source_pending_v1:"))[1]')
                assert 'confirmation_token' not in retained and 'invoice' not in retained
                page.close();page=context.new_page();foreign=False;page.goto(base+http.DEFAULT_SHEET_SUPPLIER_UI_PATH)
                expect(page.locator('#supplierSourceAcceptance .ff-operation-check')).to_be_visible(timeout=10000)
                expect(page.locator('#supplierSourceAcceptance')).to_contain_text('Добавление финансовых документов')
                expect(page.locator('#supplierSourceAcceptance')).to_contain_text('Документ сохранён.')
                assert len(writes)==1 and len(rt.list_supplier_financial_documents('source'))==1
                known=request(base,PATH+'/source/financial-documents?request_id='+writes[0])[1]
                page.goto(base+http.DEFAULT_SHEET_SUPPLIER_UI_PATH+'?operation_id='+known['acceptance']['operation_id'])
                expect(page.locator('#supplierSourceAcceptance .ff-operation-check')).to_be_visible()
                page.evaluate('''path=>{const node=document.createElement('div');node.id='financial-test';document.body.appendChild(node);
                    window.testLocked=false;window.testComponent=SupplierSourceAcceptance.create({path,scope:JSON.parse(document.getElementById('sheet-vitrina-v1-supplier-config').textContent).user_config_key || 'local_operator',container:node,onLock:v=>testLocked=v,onRecovered:async()=>{}});}
                ''',PATH)
                refused=submit('foreign-token');assert refused.get('error') and not page.evaluate('testLocked')
                assert len(writes)==2 and request(base,PATH+'/source/financial-documents?request_id='+writes[-1])[1]['status']=='rejected'
                corrected=preview(entry,'customs.pdf');saved=submit(corrected['confirmation_token'])
                assert saved['acceptance']['durable_saved'] and len(writes)==3 and writes[-1]!=writes[-2],saved
                assert len(rt.list_supplier_financial_documents('source'))==2
                with closing(source.readonly(rt.db_path)) as conn:
                    assert conn.execute(f'SELECT count(*) FROM {financial.CHILDREN}').fetchone()[0]==2
                from packages.application.business_data_write_barrier import acquire_barrier
                acquire_barrier(rt.runtime_dir,window_id='financial-smoke-maintenance',window_kind='snapshot',plan_fingerprint='sha256:'+'a'*64,approval_reference='synthetic-fixture',actor='fixture',reason='synthetic maintenance regression')
                before=rt.db_path.read_bytes()
                refused=submit('maintenance-fixture-token')
                assert refused.get('error') and not page.evaluate('testLocked')
                expect(page.locator('#financial-test .ff-operation-check')).to_have_count(0)
                expect(page.locator('#financial-test')).to_contain_text('обслуживания')
                assert rt.db_path.read_bytes()==before
                assert request(base,PATH+'/source/financial-documents?request_id='+writes[-1])[1]['status']=='unknown'
                lose_maintenance=True
                ambiguous=submit('maintenance-lost-token')
                assert ambiguous.get('error') and page.evaluate('testLocked')
                expect(page.locator('#financial-test')).to_contain_text('Проверяем сохранение')
                before_attempts=len(writes);page.close();page=context.new_page();page.goto(base+http.DEFAULT_SHEET_SUPPLIER_UI_PATH)
                expect(page.locator('#supplierSourceAcceptance')).to_contain_text('Проверяем сохранение')
                expect(page.locator('#supplierSourceAcceptance .ff-operation-check')).to_have_count(0)
                assert len(writes)==before_attempts and rt.db_path.read_bytes()==before
                context.close();browser.close()
        finally:stop(server,thread)
    print('Actual Chromium/native financial one-shot lost reply; foreign identity non-green; close-tab actor localStorage GET-only recovery; exact refusal unlock; corrected new identity no duplicate: OK')


if __name__=='__main__':main()
