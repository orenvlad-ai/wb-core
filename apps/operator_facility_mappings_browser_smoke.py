"""Actual FF forms in Chromium, native source recovery and delayed activation."""
import json,sys
from pathlib import Path
from tempfile import TemporaryDirectory
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.operator_facility_mappings_http_smoke import setup,server_for,stop,PATH,NOW
from packages.application.ff_pool_dense_fbs import DenseFbsService
from packages.adapters import registry_upload_http_entrypoint as http


def open_modal(page,base):
    page.goto(base+http.DEFAULT_SHEET_WEB_VITRINA_UI_PATH+'?tab=warehouses&warehouse=ff')
    page.locator('[data-unified-tab-button="warehouses"]').click()
    page.locator('[data-warehouse-key="ff"]').click()
    page.locator('[data-open-warehouse-costs]').click()
    page.locator('[data-ff-pool-open]').click()
    expect(page.locator('[data-ff-pool-modal]')).to_be_visible()


def edit(page,name):
    page.locator('[data-ff-pool-facilities] .ff-pool-list-item').filter(has_text=name).get_by_role('button',name='Открыть',exact=True).click()
    page.locator('[data-ff-pool-facility-detail]').get_by_role('button',name='Изменить склад',exact=True).click()
    return page.locator('[data-ff-facility-form]')


def main():
    with TemporaryDirectory(prefix='facility-browser-') as raw:
        rt,entry=setup(raw);server,thread,base=server_for(entry)
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch();context=browser.new_context();page=context.new_page();writes=[];errors=[];foreign=True;foreign_operation=False;lose_response=True
                page.on('pageerror',lambda error:errors.append(str(error)))
                def lose(route):
                    if route.request.method=='POST' and not route.request.url.endswith('/preview'):
                        reply=route.fetch();assert reply.status==200;result=reply.json();writes.append(result)
                        if lose_response:route.abort('failed')
                        else:route.fulfill(status=reply.status,content_type='application/json',body=json.dumps(result))
                    else:route.continue_()
                def tamper(route):
                    reply=route.fetch();value=reply.json()
                    if foreign and value.get('acceptance'):value['domain']='foreign'
                    if foreign_operation and value.get('acceptance'):value['acceptance']['operation_id']='ff_directory_'+'0'*32
                    route.fulfill(status=reply.status,content_type='application/json',body=json.dumps(value))
                context.route('**'+PATH+'/**',lose);context.route('**'+PATH+'/facility-operations?request_id=*',tamper)
                open_modal(page,base)
                page.locator('[data-ff-pool-facility-new]').click();form=page.locator('[data-ff-facility-form]')
                hostile='Browser warehouse <img src=x onerror="window.__facilityXss=1">'
                form.get_by_label('Название').fill(hostile);form.get_by_role('button',name='Проверить создание',exact=True).click()
                expect(form.get_by_role('button',name='Подтвердить создание',exact=True)).to_be_visible()
                assert not page.locator('[data-ff-facility-acceptance] .ff-operation-check').count()
                form.get_by_role('button',name='Подтвердить создание',exact=True).evaluate('(button)=>{button.click();button.click();}')
                receipt=page.locator('[data-ff-facility-acceptance]')
                expect(receipt).to_contain_text('Проверяем сохранение')
                expect(form.locator('[data-ff-directory-final]')).to_be_disabled()
                expect(form).to_contain_text('Проверяем сохранение. Повторно отправлять')
                assert len(writes)==1 and writes[0]['status']=='accepted'
                storage_key=page.evaluate('Object.keys(localStorage).find(k=>k.startsWith("wbc_facility_source_pending_v1:"))')
                expected=json.loads(page.evaluate('key=>localStorage.getItem(key)',storage_key))
                assert set(expected)=={'request_id','action','entity_id','digest'} and hostile not in json.dumps(expected)
                page.close();foreign=False;page=context.new_page();open_modal(page,base)
                receipt=page.locator('[data-ff-facility-acceptance]');expect(receipt.locator('.ff-operation-check')).to_be_visible()
                assert len(writes)==1 and page.evaluate('window.__facilityXss') is None
                fid=writes[0]['acceptance']['source_ref']['entity_id'];assert not entry.ff_pool_surface.facility_detail(fid)['facility']['active']
                form=edit(page,hostile);form.get_by_label('Статус').select_option('true')
                form.get_by_role('button',name='Сохранить',exact=True).evaluate('(button)=>{button.click();button.click();}')
                expect(receipt).to_contain_text('Склад ожидает активации')
                assert len(writes)==2 and not entry.ff_pool_surface.facility_detail(fid)['facility']['active']
                assert writes[-1]['acceptance']['state']=='processing'
                native=DenseFbsService(db_path=rt.db_path,runtime_dir=rt.runtime_dir,timestamp_factory=lambda:NOW)
                assert native.drain_facility_activations()['active']==1
                receipt.locator('.ff-operation-link').click();expect(receipt).to_contain_text('Склад активирован.')
                assert len(writes)==2
                close=receipt.get_by_role('button',name='Закрыть',exact=True);close.focus();page.keyboard.press('Enter');expect(receipt).to_be_hidden()
                # Exact source metadata change, lost reply and same-ID after closure.
                form=edit(page,hostile);form.get_by_label('Название').fill('Browser renamed');previous=writes[-1]['acceptance']['operation_id'];form.get_by_role('button',name='Сохранить',exact=True).click()
                expect(receipt.locator('[data-ff-operation-receipt]')).not_to_have_attribute('data-ff-operation-receipt',previous);assert len(writes)==3
                # Native full-source stale guard refuses the old editor. Correcting requires a fresh form and new identity.
                form.get_by_label('Название').fill('Must not save');form.get_by_role('button',name='Сохранить',exact=True).click()
                expect(form).to_contain_text('source changed');expect(form.get_by_role('button',name='Сохранить',exact=True)).to_be_enabled()
                assert len(writes)==4 and writes[-1]['status']=='rejected' and not receipt.locator('.ff-operation-check').count()
                form=edit(page,'Browser renamed');form.get_by_label('Название').fill('Corrected browser');form.get_by_role('button',name='Сохранить',exact=True).click()
                expect(receipt.locator('[data-ff-operation-receipt]')).not_to_have_attribute('data-ff-operation-receipt',previous);assert len(writes)==5 and writes[-1]['request_id']!=writes[-2]['request_id']
                # Mapping preview is not accepted; actual confirmation has one exact immutable native source receipt.
                wb=page.locator('[data-ff-pool-wb-warehouses] .ff-pool-list-item').filter(has_text='Official A')
                wb.get_by_role('button',name='Привязать',exact=True).click();binding=page.locator('[data-ff-binding-form]')
                binding.get_by_label('Внутренний склад FF').select_option(fid)
                binding.get_by_role('button',name='Проверить привязку',exact=True).click()
                expect(binding.get_by_role('button',name='Подтвердить привязку',exact=True)).to_be_visible()
                binding.get_by_role('button',name='Подтвердить привязку',exact=True).evaluate('(button)=>{button.click();button.click();}')
                expect(receipt).to_contain_text('Точная связь со складом WB сохранена')
                assert len(writes)==6 and writes[-1]['action']=='binding'
                # A live other-tab mutation lock prevents source mutation.
                other=context.new_page();open_modal(other,base);other_form=edit(other,'Corrected browser');other_form.get_by_label('Название').fill('Other tab')
                key=storage_key
                page.evaluate('key=>{window.unlockFacility=null;window.lockFacility=navigator.locks.request(key,async()=>{await new Promise(resolve=>window.unlockFacility=resolve);});}',key)
                page.wait_for_function('window.unlockFacility!==null');other_form.get_by_role('button',name='Сохранить',exact=True).click()
                expect(other_form).to_contain_text('другой вкладке');assert len(writes)==6
                page.evaluate('async()=>{window.unlockFacility();await window.lockFacility;}');other.close()
                # A received native operation ID is pinned before GET. A foreign
                # operation in the exact-alias answer cannot clear or paint green.
                lose_response=False;foreign_operation=True
                form=edit(page,'Corrected browser');form.get_by_label('Название').fill('Known-op warehouse');form.get_by_role('button',name='Сохранить',exact=True).click()
                expect(form).to_contain_text('Подтверждение этой операции пока не получено')
                expect(form.get_by_role('button',name='Сохранить',exact=True)).to_be_disabled()
                stored=json.loads(page.evaluate('key=>localStorage.getItem(key)',storage_key))
                assert len(writes)==7 and stored['operation_id']==writes[-1]['acceptance']['operation_id']
                assert not receipt.locator('.ff-operation-check').count()
                page.close();foreign_operation=False;page=context.new_page();open_modal(page,base);receipt=page.locator('[data-ff-facility-acceptance]')
                expect(receipt.locator('[data-ff-operation-receipt]')).to_have_attribute('data-ff-operation-receipt',stored['operation_id'])
                assert len(writes)==7
                # Received maintenance is not_saved; lost maintenance stays unknown across closing the tab.
                from packages.application.business_data_write_barrier import acquire_barrier
                acquire_barrier(rt.runtime_dir,window_id='facility-browser-maintenance',window_kind='snapshot',plan_fingerprint='sha256:'+'a'*64,approval_reference='synthetic',actor='fixture',reason='synthetic maintenance')
                context.unroute('**'+PATH+'/**',lose)
                form=edit(page,'Known-op warehouse');form.get_by_label('Название').fill('Maintenance blocked')
                before=rt.db_path.read_bytes();form.get_by_role('button',name='Сохранить',exact=True).click()
                expect(receipt).to_contain_text('Режим обслуживания');expect(form.get_by_role('button',name='Сохранить',exact=True)).to_be_enabled()
                assert not receipt.locator('.ff-operation-check').count() and before==rt.db_path.read_bytes()
                def lose_blocked(route):
                    if route.request.method=='POST':reply=route.fetch();assert reply.status==423;route.abort('failed')
                    else:route.continue_()
                context.route('**'+PATH+'/facilities/*',lose_blocked)
                form.get_by_role('button',name='Сохранить',exact=True).click();expect(receipt).to_contain_text('Проверяем сохранение')
                expect(form.get_by_role('button',name='Сохранить',exact=True)).to_be_disabled()
                page.close();page=context.new_page();open_modal(page,base);receipt=page.locator('[data-ff-facility-acceptance]')
                expect(receipt).to_contain_text('Проверяем сохранение');page.locator('[data-ff-pool-facility-new]').click()
                expect(page.locator('[data-ff-facility-form] [data-ff-directory-final]')).to_be_disabled()
                assert len(writes)==7 and before==rt.db_path.read_bytes() and not receipt.locator('.ff-operation-check').count()
                assert not errors,errors
                context.close();browser.close()
        finally:stop(server,thread)
    print('Actual FF Chromium: preview vs accepted, same-ID closed-tab recovery, delayed/native activation, hostile text, source refusal/new corrected identity, mapping, double click/other-tab, received/lost maintenance: OK')
if __name__=='__main__':main()
