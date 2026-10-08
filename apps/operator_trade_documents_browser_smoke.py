"""Actual Chromium library forms: persistent lost-reply recovery, source guards."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json,sys
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.operator_supplier_shipments_smoke import seed
from apps.operator_supplier_shipments_http_smoke import server_for,stop,request
from apps.operator_trade_documents_smoke import contract_bytes
from packages.adapters import registry_upload_http_entrypoint as http
from packages.application import operator_trade_documents as receipts


def main():
    with TemporaryDirectory(prefix='operator-library-browser-') as raw:
        rt,entry,_=seed(raw);server,thread,base=server_for(entry)
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch();context=browser.new_context();page=context.new_page();writes=[];patches=[];foreign=True;errors=[]
                context.on('request',lambda req: patches.append(req.url) if req.method=='PATCH' and http.DEFAULT_TRADE_DOCUMENTS_PATH in req.url else None)
                page.on('pageerror',lambda error:errors.append(str(error)))
                def lose(route):
                    if route.request.method=='POST':
                        reply=route.fetch();assert reply.status==200;value=reply.json();writes.append(value['request_id']);route.abort('failed')
                    else:route.continue_()
                def tamper(route):
                    reply=route.fetch();value=reply.json()
                    if foreign and value.get('acceptance'):value['domain']='foreign_domain'
                    route.fulfill(status=reply.status,content_type='application/json',body=json.dumps(value))
                context.route('**'+http.DEFAULT_TRADE_DOCUMENTS_PATH,lose);context.route('**'+http.DEFAULT_TRADE_DOCUMENTS_PATH+'?request_id=*',tamper)
                url=base+http.DEFAULT_SETTINGS_UI_PATH+'?embedded=1'
                page.goto(url);page.locator('[data-settings-tab-button="invoices"]').click()
                page.locator('#invoiceNumberInput').fill('I-browser')
                page.locator('#documentFileInput').set_input_files({'name':'invoice.pdf','mimeType':'application/pdf','buffer':b'library-browser-invoice'})
                # Native file chooser sets the document type; use the actual button first.
                # set_input_files before choosing type above is harmless (no write).
                with page.expect_file_chooser() as chooser:page.locator('#addInvoiceButton').click()
                with page.expect_event('requestfailed', predicate=lambda req: req.method=='POST' and req.url==base+http.DEFAULT_TRADE_DOCUMENTS_PATH):
                    chooser.value.set_files({'name':'invoice.pdf','mimeType':'application/pdf','buffer':b'library-browser-invoice'})
                expect(page.locator('#tradeSourceReceipt')).to_contain_text('Проверяем сохранение')
                expect(page.locator('#addInvoiceButton')).to_be_disabled()
                assert len(writes)==1 and not errors,(writes,errors)
                stored=page.evaluate('Object.entries(localStorage).find(([k])=>k.startsWith("wbc_trade_source_pending_v1:"))[1]')
                assert 'I-browser' not in stored and 'file_sha256' not in stored
                before=rt.db_path.read_bytes();page.close();foreign=False;page=context.new_page();page.goto(url)
                expect(page.locator('#tradeSourceReceipt .ff-operation-check')).to_be_visible(timeout=10000)
                expect(page.locator('#tradeSourceReceipt')).to_contain_text('Документ сохранён.')
                assert len(writes)==1 and rt.db_path.read_bytes()==before
                # Actual unsupported file refusal clears the alias; a corrected file uses a distinct ID.
                page.locator('#invoiceDateInput').fill('')
                with page.expect_file_chooser() as chooser:page.locator('#addInvoiceButton').click()
                chooser.value.set_files({'name':'bad.txt','mimeType':'text/plain','buffer':b'library-bad-extension'})
                expect(page.locator('#invoicesMessage')).to_contain_text('must be one of',timeout=10000)
                expect(page.locator('#addInvoiceButton')).to_be_enabled(timeout=10000)
                assert len(writes)==2 and request(base,http.DEFAULT_TRADE_DOCUMENTS_PATH+'?request_id='+writes[-1])[1]['status']=='rejected'
                page.locator('#invoiceDateInput').fill('2026-07-24')
                with page.expect_file_chooser() as chooser:page.locator('#addInvoiceButton').click()
                chooser.value.set_files({'name':'corrected.pdf','mimeType':'application/pdf','buffer':b'library-corrected'})
                expect(page.locator('#tradeSourceReceipt .ff-operation-check')).to_be_visible(timeout=10000)
                assert len(writes)==3 and len(set(writes))==3 and len(rt.list_trade_documents())==2
                # Real contract form, metadata edit and linked-contract refusal.
                page.locator('[data-settings-tab-button="contracts"]').click()
                with page.expect_file_chooser() as chooser:page.locator('#addContractButton').click()
                chooser.value.set_files({'name':'contract.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':contract_bytes()})
                expect(page.locator('#tradeSourceReceipt .ff-operation-check')).to_be_visible(timeout=10000)
                expect(page.locator('#contractRows [data-contract-edit]')).to_be_visible()
                page.locator('#contractRows [data-contract-edit]').click()
                page.locator('[data-contract-edit-field="number"]').fill('<img src=x onerror="window.bad=true">')
                edit_count=len(patches)
                page.locator('[data-contract-save]').evaluate('(button) => {button.click();button.click();}')
                expect(page.locator('#tradeSourceReceipt')).to_contain_text('<img src=x')
                assert len(patches)==edit_count+1
                assert not page.evaluate('window.bad || false')
                cid=next(r['document_id'] for r in rt.list_trade_documents() if r['document_type']=='contract')
                iid=next(r['document_id'] for r in rt.list_trade_documents() if r['number']=='I-browser')
                page.locator('[data-settings-tab-button="invoices"]').click()
                row=page.locator('tr[data-document-id="'+iid+'"]');row.locator('[data-contract-select]').select_option(cid);row.locator('[data-document-link]').click()
                expect(row.locator('[data-document-unlink]')).to_be_visible(timeout=10000)
                page.locator('[data-settings-tab-button="contracts"]').click();page.on('dialog',lambda dialog:dialog.accept())
                page.locator('#contractRows [data-document-archive]').click()
                expect(page.locator('#contractsMessage')).to_contain_text('cannot be archived')
                expect(page.locator('#addContractButton')).to_be_enabled()
                assert rt.load_trade_document(cid)['status']=='active'
                page.locator('[data-settings-tab-button="invoices"]').click();row.locator('[data-document-archive]').click()
                expect(row).to_have_count(0,timeout=10000)
                assert rt.load_trade_document(iid)['status']=='archived' and not rt.load_invoice_contract_link(iid)
                last=request(base,http.DEFAULT_TRADE_DOCUMENTS_PATH+'?request_id='+writes[0])[1]
                page.goto(base+last['acceptance']['detail_path']+'&embedded=1')
                expect(page.locator('#tradeSourceReceipt .ff-operation-check')).to_be_visible(timeout=10000)
                assert len(writes)==4
                from packages.application.business_data_write_barrier import acquire_barrier
                acquire_barrier(rt.runtime_dir,window_id='library-maintenance-smoke',window_kind='snapshot',
                    plan_fingerprint='sha256:'+'a'*64,approval_reference='synthetic-fixture',actor='fixture',reason='synthetic maintenance')
                context.unroute('**'+http.DEFAULT_TRADE_DOCUMENTS_PATH,lose)
                before=rt.db_path.read_bytes()
                with page.expect_file_chooser() as chooser:page.locator('#addInvoiceButton').click()
                chooser.value.set_files({'name':'blocked.pdf','mimeType':'application/pdf','buffer':b'library-blocked'})
                expect(page.locator('#tradeSourceReceipt')).to_contain_text('Режим обслуживания',timeout=10000)
                expect(page.locator('#addInvoiceButton')).to_be_enabled()
                assert page.locator('#tradeSourceReceipt .ff-operation-check').count()==0 and rt.db_path.read_bytes()==before
                def lose_blocked(route):
                    if route.request.method=='POST':
                        reply=route.fetch();assert reply.status==423;writes.append(reply.json()['request_id']);route.abort('failed')
                    else:route.continue_()
                context.route('**'+http.DEFAULT_TRADE_DOCUMENTS_PATH,lose_blocked)
                with page.expect_file_chooser() as chooser:page.locator('#addInvoiceButton').click()
                with page.expect_event('requestfailed', predicate=lambda req: req.method=='POST' and req.url==base+http.DEFAULT_TRADE_DOCUMENTS_PATH):
                    chooser.value.set_files({'name':'uncertain.pdf','mimeType':'application/pdf','buffer':b'library-uncertain'})
                expect(page.locator('#tradeSourceReceipt')).to_contain_text('Проверяем сохранение')
                expect(page.locator('#addInvoiceButton')).to_be_disabled()
                count=len(writes);page.close();page=context.new_page();page.goto(url)
                expect(page.locator('#tradeSourceReceipt')).to_contain_text('Проверяем сохранение')
                expect(page.locator('#addInvoiceButton')).to_be_disabled()
                assert len(writes)==count and rt.db_path.read_bytes()==before
                browser.close()
        finally:stop(server,thread)
    print('Actual library Chromium forms, lost reply/closed tab same-ID GET, refusal/corrected identity, metadata escaping/doubleclick, native archive guard, actual423/lost423 unknown: OK')


if __name__=='__main__':main()
