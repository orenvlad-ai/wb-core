"""Actual supplier form in Chromium: exact lost-reply source and native continuation."""
import json,sys
from pathlib import Path
from tempfile import TemporaryDirectory
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.operator_supplier_contracts_smoke import setup,finish,contract_bytes
from apps.operator_supplier_shipments_http_smoke import server_for,stop,request,PATH
from packages.adapters import registry_upload_http_entrypoint as http


def main():
    with TemporaryDirectory(prefix='operator-contract-browser-') as raw:
        rt,entry,sid=setup(raw);server,thread,base=server_for(entry);endpoint=PATH+'/'+sid+'/contract'
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch();context=browser.new_context();page=context.new_page();writes=[];errors=[];foreign=True
                page.on('pageerror',lambda error:errors.append(str(error)))
                def lose(route):
                    if route.request.method in ('POST','PATCH'):
                        reply=route.fetch();assert reply.status==200;value=reply.json();writes.append(value);route.abort('failed')
                    else:route.continue_()
                def tamper(route):
                    reply=route.fetch();value=reply.json()
                    if foreign and value.get('acceptance'):value['domain']='foreign'
                    route.fulfill(status=reply.status,content_type='application/json',body=json.dumps(value))
                context.route('**'+endpoint,lose);context.route('**'+endpoint+'?request_id=*',tamper)
                url=base+http.DEFAULT_SHEET_SUPPLIER_UI_PATH+'?embedded=operator&shipment_id='+sid
                page.goto(url)
                expect(page.locator('#uploadContractButton')).to_be_visible(timeout=10000)
                with page.expect_file_chooser() as chooser:page.locator('#uploadContractButton').click()
                chooser.value.set_files({'name':'contract.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':contract_bytes()})
                expect(page.locator('#supplierContractAcceptance')).to_contain_text('Проверяем сохранение',timeout=10000)
                expect(page.locator('#uploadContractButton')).to_be_disabled()
                expect(page.locator('#cardMessage')).to_contain_text('Проверяем сохранение. Повторно отправлять',timeout=10000)
                assert len(writes)==1 and writes[0]['status']=='accepted' and not errors,errors
                expect(page.locator('#saveShipmentButton')).to_be_enabled()
                stored=page.evaluate('Object.entries(localStorage).find(([k])=>k.startsWith("wbc_supplier_contract_pending_v1:"))[1]')
                assert 'contract.xlsx' not in stored and 'file_sha256' not in stored and len(json.loads(stored))==4
                iid=writes[0]['acceptance']['source_ref']['invoice_document_id'];cid=writes[0]['acceptance']['source_ref']['contract_document_id']
                assert not rt.load_invoice_contract_link(iid)
                # Close-tab recovery: same GET, no new submission, green source is still pending.
                before=rt.db_path.read_bytes();page.close();foreign=False;page=context.new_page();page.goto(base+http.DEFAULT_SHEET_SUPPLIER_UI_PATH+'?embedded=operator')
                expect(page.locator('#supplierContractAcceptance .ff-operation-check')).to_be_visible(timeout=10000)
                expect(page.locator('#supplierContractAcceptance')).to_contain_text('Связь ожидает обработки')
                assert len(writes)==1
                # Existing registry GET is a legacy reader; exact receipt GET separately must be RO.
                assert request(base,endpoint+'?request_id='+writes[0]['request_id'])[1]['acceptance']['processing']['complete'] is False
                finish(rt,sid)
                page.goto(base+writes[0]['acceptance']['detail_path'])
                expect(page.locator('#supplierContractAcceptance')).to_contain_text('Связь с договором сохранена.',timeout=10000)
                assert page.locator('#supplierContractAcceptance .ff-operation-check').count()==1 and len(writes)==1
                page.locator('#supplierContractAcceptance .ff-operation-link').click()
                expect(page.locator('#supplierContractAcceptance')).to_contain_text('Связь с договором сохранена.',timeout=10000)
                page.locator('#supplierContractAcceptance button').focus();page.keyboard.press('Enter')
                expect(page.locator('#supplierContractAcceptance')).to_be_hidden()
                # Unknown actor/foreign operation cannot paint green or clear a pending ID.
                page.goto(url);expect(page.locator('#uploadContractButton')).to_be_visible()
                with page.expect_file_chooser() as chooser:page.locator('#uploadContractButton').click()
                chooser.value.set_files({'name':'bad.txt','mimeType':'text/plain','buffer':b'bad-contract'})
                expect(page.locator('#cardMessage')).to_contain_text('must be one of',timeout=10000)
                expect(page.locator('#uploadContractButton')).to_be_enabled()
                assert len(writes)==2 and writes[-1]['status']=='rejected' and page.locator('#supplierContractAcceptance .ff-operation-check').count()==0
                with page.expect_file_chooser() as chooser:page.locator('#uploadContractButton').click()
                chooser.value.set_files({'name':'corrected.pdf','mimeType':'application/pdf','buffer':b'contract-corrected'})
                expect(page.locator('#supplierContractAcceptance .ff-operation-check')).to_be_visible(timeout=10000)
                assert len(writes)==3 and len({r['request_id'] for r in writes})==3
                assert rt.load_invoice_contract_link(iid)['contract_document_id']==cid # new physical intent is not yet linked
                finish(rt,sid);assert rt.load_invoice_contract_link(iid)['contract_document_id']!=cid
                # Actual link double click: one PATCH, no second source write.
                page.goto(url);expect(page.locator('#contractCandidateSelect')).to_be_visible()
                page.locator('#contractCandidateSelect').select_option(cid)
                page.locator('#linkContractButton').evaluate('(button)=>{button.click();button.click();}')
                expect(page.locator('#supplierContractAcceptance .ff-operation-check')).to_be_visible(timeout=10000)
                assert len(writes)==4 and writes[-1]['action']=='link'
                page.locator('dialog.ff-operation-popup').get_by_role('button',name='Закрыть').click()
                # Cross-tab handshake never emits a mutation while the other tab owns this family lock.
                other=context.new_page();other.goto(url);expect(other.locator('#linkContractButton')).to_be_visible()
                # Scope is supplied by the actual page config; inspect the injected component's family lock via StorageEvent.
                key='wbc_supplier_contract_pending_v1:'+writes[0]['acceptance']['actor']
                page.evaluate('key=>{window.contractLockRelease=null;window.contractLockHeld=navigator.locks.request(key,async()=>{await new Promise(resolve=>window.contractLockRelease=resolve);});}',key)
                page.wait_for_function('window.contractLockRelease !== null')
                other.locator('#contractCandidateSelect').select_option(cid);other.locator('#linkContractButton').click()
                expect(other.locator('#cardMessage')).to_contain_text('другой вкладке',timeout=10000)
                assert len(writes)==4
                page.evaluate('async()=>{window.contractLockRelease();await window.contractLockHeld;}');other.close()
                from packages.application.business_data_write_barrier import acquire_barrier
                acquire_barrier(rt.runtime_dir,window_id='contract-browser-maintenance',window_kind='snapshot',plan_fingerprint='sha256:'+'a'*64,approval_reference='synthetic-fixture',actor='fixture',reason='synthetic maintenance')
                context.unroute('**'+endpoint,lose)
                before=rt.db_path.read_bytes()
                page.locator('#contractCandidateSelect').select_option(cid)
                page.locator('#linkContractButton').click()
                expect(page.locator('#supplierContractAcceptance')).to_contain_text('Режим обслуживания',timeout=10000)
                expect(page.locator('#linkContractButton')).to_be_enabled()
                assert page.locator('#supplierContractAcceptance .ff-operation-check').count()==0 and rt.db_path.read_bytes()==before
                def lose_blocked(route):
                    reply=route.fetch();assert reply.status==423;route.abort('failed')
                context.route('**'+endpoint,lose_blocked)
                page.locator('#linkContractButton').click()
                expect(page.locator('#supplierContractAcceptance')).to_contain_text('Проверяем сохранение',timeout=10000)
                expect(page.locator('#linkContractButton')).to_be_disabled()
                page.close();page=context.new_page();page.goto(url)
                expect(page.locator('#supplierContractAcceptance')).to_contain_text('Проверяем сохранение',timeout=10000)
                expect(page.locator('#linkContractButton')).to_be_disabled()
                assert len(writes)==4 and rt.db_path.read_bytes()==before
                assert page.locator('#supplierContractAcceptance .ff-operation-check').count()==0
                context.close();browser.close()
        finally:stop(server,thread)
    print('Actual Chromium supplier upload/link: durable pending vs physical processing, lost-reply closed-tab same-ID, retained refusal/corrected ID, double-click/cross-tab lock, source grants and actual/lost 423: OK')


if __name__=='__main__':main()
