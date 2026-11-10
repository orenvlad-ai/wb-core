"""Actual Chromium payment attachment, lost responses and reload without re-POST."""
from contextlib import closing
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from apps.fulfillment_payment_proof_http_smoke import PDF
from apps.fulfillment_payment_proof_smoke import upload, business_rows
from apps.operator_fulfillment_services_native_smoke import seed
from apps.operator_fulfillment_services_browser_smoke import open_form
from apps.operator_fulfillment_services_integration_smoke import server_for,stop
from packages.application import fulfillment_payment_proof as proof
from packages.application import operator_fulfillment_services as receipts


def run():
    with TemporaryDirectory(prefix='ff-payment-browser-') as raw:
        rt,block=seed(raw);uid=upload(block)
        baseline=business_rows(rt)
        entry,server,thread,base=server_for(rt)
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1440,'height':1100})
                errors=[];page.on('pageerror',lambda exc:errors.append(str(exc)))
                calls=[];rejections=['maintenance','validation']
                def lose_response(route):
                    if route.request.method=='POST':
                        if route.request.url.endswith('/confirm') and rejections:
                            mode=rejections.pop(0)
                            route.fulfill(status=423 if mode=='maintenance' else 400,content_type='application/json',body='{"code":"business_data_maintenance","error":"Обслуживание"}' if mode=='maintenance' else '{"code":"fulfillment_payment_preview_stale","error":"Проверка не сохранена","source_not_saved":true}')
                            return
                        calls.append(route.request.url.rsplit('/',1)[-1]);response=route.fetch()
                        assert response.status==200, response.text()
                        route.abort('failed')
                    else:route.continue_()
                page.route('**/payment-proof/preview',lose_response)
                page.route('**/payment-proof/confirm',lose_response)
                open_form(page,base)
                page.locator('[data-fulfillment-payment-proof]').click()
                page.locator('#fulfillmentProofInvoice').fill('syn-101')
                page.locator('#fulfillmentProofFile').set_input_files({'name':'bank.pdf','mimeType':'application/pdf','buffer':PDF})
                page.locator('#fulfillmentProofCheck').click()
                panel=page.locator('#fulfillmentPaymentProof')
                expect(panel).to_contain_text('Проверьте документ перед прикреплением',timeout=10000)
                expect(panel).to_contain_text('Поставка WB 1001')
                expect(panel).to_contain_text('коробов:')
                expect(panel).to_contain_text('SYN-101')
                expect(panel).to_contain_text('Синтетический Получатель')
                expect(panel).to_contain_text('Назначение платежа:')
                expect(panel).to_contain_text('Оплата тестовых услуг')
                expect(panel).to_contain_text('2026-08-19')
                expect(panel).to_contain_text('расчёты себестоимости этим действием не меняются')
                with closing(receipts.readonly(rt.db_path)) as conn:
                    assert conn.execute('SELECT count(*) FROM '+proof.CONFIRMATIONS).fetchone()[0]==0
                # Preview is durable across reload. No PDF resend or implicit attach.
                page.reload(wait_until='domcontentloaded');open_form(page,base)
                expect(page.locator('#fulfillmentProofConfirm')).to_be_visible(timeout=10000)
                assert calls==['preview']
                page.locator('#fulfillmentProofConfirm').click()
                expect(page.locator('#fulfillmentServicesMessage')).to_contain_text('Обслуживание')
                expect(page.locator('#fulfillmentProofConfirm')).to_be_visible()
                # The known pre-save maintenance refusal leaves the same durable preview available after reload.
                page.reload(wait_until='domcontentloaded');open_form(page,base)
                expect(page.locator('#fulfillmentProofConfirm')).to_be_visible(timeout=10000)
                page.locator('#fulfillmentProofConfirm').click()
                expect(page.locator('#fulfillmentServicesMessage')).to_contain_text('Проверка не сохранена')
                expect(page.locator('#fulfillmentProofConfirm')).to_be_visible()
                page.locator('#fulfillmentProofConfirm').click()
                expect(page.locator('#fulfillmentPaymentProof')).to_contain_text('Оплата подтверждена документом',timeout=10000)
                expect(page.locator('#fulfillmentUploadsBody')).to_contain_text('Документ, подтверждающий оплату')
                page.reload(wait_until='domcontentloaded');open_form(page,base)
                expect(page.locator('#fulfillmentUploadsBody')).to_contain_text('Оплата подтверждена документом')
                assert calls==['preview','confirm'],calls
                assert page.locator('[data-fulfillment-payment-proof]').count()==0
                assert not errors,errors
                with closing(receipts.readonly(rt.db_path)) as conn:
                    assert conn.execute('SELECT count(*) FROM '+proof.PREVIEWS).fetchone()[0]==1
                    assert conn.execute('SELECT count(*) FROM '+proof.CONFIRMATIONS).fetchone()[0]==1
                assert business_rows(rt)==baseline
                browser.close()
            print('Chromium: exact preview details, explicit attach, lost preview+confirm recovery and reload issue no duplicate POST; unchanged expense/cost source: OK')
        finally:stop(server,thread)


def test_cancel_preview_and_different_pdf():
    with TemporaryDirectory(prefix='ff-payment-cancel-') as raw:
        rt,block=seed(raw);uid=upload(block);baseline=business_rows(rt)
        entry,server,thread,base=server_for(rt)
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1440,'height':1100})
                calls=[];page.on('request',lambda req:calls.append(req.url.rsplit('/',1)[-1]) if req.method=='POST' and '/payment-proof/' in req.url else None)
                open_form(page,base)
                def choose(data):
                    page.locator('[data-fulfillment-payment-proof]').click()
                    page.locator('#fulfillmentProofInvoice').fill('SYN-101')
                    page.locator('#fulfillmentProofFile').set_input_files({'name':'bank.pdf','mimeType':'application/pdf','buffer':data})
                    page.locator('#fulfillmentProofCheck').click()
                choose(PDF)
                expect(page.locator('#fulfillmentProofCancel')).to_be_visible(timeout=10000)
                page.locator('#fulfillmentPaymentProof').screenshot(path='/tmp/wbc0206-payment-proof-preview.png')
                page.locator('#fulfillmentProofCancel').click()
                expect(page.locator('#fulfillmentPaymentProof')).to_be_hidden()
                choose(PDF+b'\n')
                expect(page.locator('#fulfillmentServicesMessage')).to_contain_text('Платёж уже проверен по другому PDF',timeout=10000)
                expect(page.locator('#fulfillmentProofCheck')).to_be_visible()
                assert calls==['preview','preview']
                with closing(receipts.readonly(rt.db_path)) as conn:
                    assert conn.execute('SELECT count(*) FROM '+proof.PREVIEWS).fetchone()[0]==1
                    assert conn.execute('SELECT count(*) FROM '+proof.CONFIRMATIONS).fetchone()[0]==0
                assert business_rows(rt)==baseline
                browser.close()
            print('Chromium: cancel preview never attaches; different-PDF semantic duplicate gives recoverable explicit refusal: OK')
        finally:stop(server,thread)


if __name__=='__main__':
    run()
    test_cancel_preview_and_different_pdf()
