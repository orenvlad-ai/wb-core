"""Actual Chromium/native CNY HTTP, lost reply and close-tab recovery."""
from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib,json,sqlite3,sys
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.operator_supplier_financial_native_smoke import setup
from apps.cny_ledger_smoke import _fixture_text_extractor
from apps.operator_supplier_shipments_http_smoke import server_for,stop,request
from packages.adapters import registry_upload_http_entrypoint as http
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application import operator_cny_documents as cny


def database_image(path):
    # A raw main-file hash is not a SQLite snapshot in WAL mode: an already
    # committed POST may checkpoint after the later GET. Compare one pinned,
    # query-only image of all native tables and schema instead.
    conn=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN')
        schema=conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        data=[]
        for kind,name,_,_ in schema:
            if kind=='table':
                rows=conn.execute('SELECT * FROM "'+name.replace('"','""')+'"').fetchall()
                data.append((name,sorted(repr(row) for row in rows)))
        return hashlib.sha256(repr((schema,data)).encode()).hexdigest()
    finally:conn.close()


def close_accepted(page,operation_id):
    popup=page.locator('dialog.ff-operation-popup[open]')
    expect(popup.locator('[data-ff-operation-receipt]')).to_have_attribute('data-ff-operation-receipt',operation_id)
    expect(popup.locator('.ff-operation-check')).to_be_visible(timeout=10000)
    expect(popup).to_contain_text('Документ сохранён.')
    popup.get_by_role('button',name='Закрыть',exact=True).click()
    expect(popup).to_have_count(0)


def main():
    with TemporaryDirectory(prefix='operator-cny-browser-') as raw:
        rt,_=setup(raw);entry=RegistryUploadHttpEntrypoint(runtime_dir=rt.runtime_dir,runtime=rt)
        entry.cny_ledger_block.pdf_text_extractor=_fixture_text_extractor
        server,thread,base=server_for(entry)
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch();context=browser.new_context();page=context.new_page();writes=[];foreign=True
                def lose(route):
                    writes.append(route.request.post_data_json['request_id']);reply=route.fetch();assert reply.status==200;route.abort('failed')
                def read(route):
                    reply=route.fetch();result=reply.json()
                    if foreign and result.get('acceptance'):result['domain']='foreign_domain'
                    route.fulfill(status=reply.status,content_type='application/json',body=json.dumps(result))
                context.route('**/cny-account/opening-balance',lose)
                context.route('**/cny-account?request_id=*',read)
                page.goto(base+http.DEFAULT_SHEET_OPERATOR_UI_PATH+'?embedded_tab=factory-order');page.locator('[data-supply-mode-button="cny-account"]').click()
                assert page.evaluate('typeof CnySourceAcceptance')=='object'
                def create():
                    page.evaluate('''() => {const box=document.createElement('div');box.id='cnyTest';document.body.appendChild(box);
                        window.testLocked=false;window.cnyTest=CnySourceAcceptance.create({container:box,path:'/v1/sheet-vitrina-v1/supply/cny-account',
                        scope:JSON.parse(document.getElementById('sheet-vitrina-v1-operator-config').textContent).user_config_key,
                        onLock:v=>testLocked=v,onError:()=>{},onRecovered:async()=>{}});}''')
                create()
                def submit(day='2026-07-24',amount='200'):
                    return page.evaluate('''async ({day,amount}) => {try {return await window.cnyTest.mutate('/v1/sheet-vitrina-v1/supply/cny-account/opening-balance',
                        'cny_opening',{operation_date:day,cny_amount:amount,rub_value:'2000'},'POST');}catch(e){return {error:e.message};}}''',{'day':day,'amount':amount})
                assert submit().get('error') and page.evaluate('testLocked')
                expect(page.locator('#cnyTest')).to_contain_text('Проверяем сохранение')
                assert len(writes)==1
                stored=page.evaluate('Object.entries(localStorage).find(([k])=>k.startsWith("wbc_cny_source_pending_v1:"))[1]')
                assert '2000' not in stored and 'rub_value' not in stored
                page.close();foreign=False;page=context.new_page();page.goto(base+http.DEFAULT_SHEET_OPERATOR_UI_PATH+'?embedded_tab=factory-order')
                known=request(base,http.DEFAULT_CNY_ACCOUNT_PATH+'?request_id='+writes[0])[1]
                assert known['domain']==cny.DOMAIN
                close_accepted(page,known['acceptance']['operation_id'])
                expect(page.locator('#cnySourceReceipt')).to_be_hidden()
                page.locator('[data-supply-mode-button="cny-account"]').click()
                assert len(writes)==1
                create();refusal=submit('bad');assert refusal.get('error') and not page.evaluate('testLocked')
                assert request(base,http.DEFAULT_CNY_ACCOUNT_PATH+'?request_id='+writes[-1])[1]['status']=='rejected'
                corrected=submit(amount='250');assert corrected['acceptance']['durable_saved'] and writes[-1]!=writes[-2]
                assert len(writes)==3 and len(rt.list_cny_documents())==1
                close_accepted(page,corrected['acceptance']['operation_id'])
                page.goto(base+corrected['acceptance']['detail_path'])
                close_accepted(page,corrected['acceptance']['operation_id'])
                expect(page.locator('#cnySourceReceipt')).to_be_hidden()
                assert len(writes)==3
                create()
                uploaded=page.evaluate('''async () => {
                    const file=new File(['conversion-fixture'],'conversion.pdf',{type:'application/pdf'});
                    const hash=await crypto.subtle.digest('SHA-256',await file.arrayBuffer());
                    const sha=Array.from(new Uint8Array(hash),b=>b.toString(16).padStart(2,'0')).join('');
                    return window.cnyTest.mutate('/v1/sheet-vitrina-v1/supply/cny-account/documents','cny_upload',
                        {filename:file.name,file_sha256:sha,payment_date:''},'POST',file);
                }''')
                assert uploaded['acceptance']['domain']==cny.DOMAIN and len(rt.list_cny_documents())==2
                close_accepted(page,uploaded['acceptance']['operation_id'])
                from packages.application.business_data_write_barrier import acquire_barrier
                acquire_barrier(rt.runtime_dir,window_id='cny-smoke-maintenance',window_kind='snapshot',plan_fingerprint='sha256:'+'a'*64,
                    approval_reference='synthetic-fixture',actor='fixture',reason='synthetic maintenance regression')
                # Matching received 423 is definitive, and does not create a DB receipt.
                context.unroute('**/cny-account/opening-balance',lose)
                before=database_image(rt.db_path)
                refused=submit(amount='300');assert refused.get('error') and not page.evaluate('testLocked')
                assert database_image(rt.db_path)==before
                def lose_blocked(route):
                    writes.append(route.request.post_data_json['request_id']);reply=route.fetch();assert reply.status==423;route.abort('failed')
                context.route('**/cny-account/opening-balance',lose_blocked)
                uncertain=submit(amount='350');assert uncertain.get('error') and page.evaluate('testLocked')
                expect(page.locator('#cnyTest')).to_contain_text('Проверяем сохранение')
                count=len(writes);page.close();page=context.new_page();page.goto(base+http.DEFAULT_SHEET_OPERATOR_UI_PATH+'?embedded_tab=factory-order')
                page.locator('[data-supply-mode-button="cny-account"]').click()
                expect(page.locator('#cnySourceReceipt')).to_contain_text('Проверяем сохранение')
                expect(page.locator('#cnyOpeningSaveButton')).to_be_disabled()
                assert len(writes)==count, ('unexpected resend', count, len(writes))
                assert database_image(rt.db_path)==before, 'reload changed native DB after maintenance refusal'
                browser.close()
        finally:stop(server,thread)
    print('Actual Chromium CNY lost reply, foreign domain non-green, close-tab same-ID GET-only, definitive refusal/corrected new identity and no duplicate source: OK')


if __name__=='__main__':main()
