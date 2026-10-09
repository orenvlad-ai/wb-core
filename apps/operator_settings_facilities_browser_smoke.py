"""Actual Settings warehouses Chromium: one source POST and closed-tab recovery."""
import sys,json
from pathlib import Path
from tempfile import TemporaryDirectory
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.operator_facility_mappings_http_smoke import setup,server_for,stop,PATH,NOW
from packages.adapters import registry_upload_http_entrypoint as http
from packages.application.ff_pool_dense_fbs import DenseFbsService
from packages.application.business_data_write_barrier import acquire_barrier


def open_settings(page,base):
    page.goto(base+http.DEFAULT_SETTINGS_UI_PATH+'?embedded=1#warehouses')
    expect(page.locator('#warehousesGroupPanel')).to_be_visible()
    page.wait_for_function("document.documentElement.dataset.settingsReady==='true'")


def main():
    with TemporaryDirectory(prefix='settings-facility-browser-') as raw:
        rt,entry=setup(raw);server,thread,base=server_for(entry)
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch();ctx=browser.new_context();page=ctx.new_page();writes=[];errors=[];foreign=True
                ctx.on('page',lambda page:page.on('pageerror',lambda error:errors.append(str(error))))
                page.on('pageerror',lambda error:errors.append(str(error)))
                def lost(route):
                    if route.request.method=='POST' and not route.request.url.endswith('/preview'):
                        reply=route.fetch();assert reply.status==200;writes.append(reply.json());route.abort('failed')
                    else:route.continue_()
                def readback(route):
                    reply=route.fetch();result=reply.json()
                    if foreign and result.get('acceptance'):result['domain']='foreign'
                    route.fulfill(status=reply.status,content_type='application/json',body=json.dumps(result))
                ctx.route('**'+PATH+'/**',lost);ctx.route('**'+PATH+'/facility-operations?request_id=*',readback)
                open_settings(page,base)
                name='Settings <img src=x onerror="window.bad=true">'
                page.locator('#warehouseNameInput').fill(name)
                page.locator('#createWarehouseButton').click()
                expect(page.locator('#createWarehouseButton')).to_have_text('Подтвердить создание')
                assert not writes and not page.locator('#warehouseSourceReceipt .ff-operation-check').count()
                page.locator('#editWarehouseDraftButton').click();expect(page.locator('#warehouseNameInput')).to_be_enabled()
                assert not writes
                page.locator('#createWarehouseButton').click();expect(page.locator('#createWarehouseButton')).to_have_text('Подтвердить создание')
                page.locator('#createWarehouseButton').evaluate('(b)=>{b.click();b.click();}')
                receipt=page.locator('#warehouseSourceReceipt');expect(receipt).to_contain_text('Проверяем сохранение')
                expect(page.locator('#createWarehouseButton')).to_be_disabled()
                expect(page.locator('#warehousesActivationMessage')).to_contain_text('Повторно отправлять')
                assert len(writes)==1
                marker=page.evaluate('Object.entries(localStorage).find(([k])=>k.startsWith("wbc_facility_source_pending_v1:"))')
                assert name not in marker[1] and set(json.loads(marker[1]))=={'request_id','action','entity_id','digest'}
                page.evaluate('localStorage.setItem("wb-core:sheet-vitrina-v1:settings-active-group:v1","directories")')
                page.close();foreign=False;page=ctx.new_page();page.goto(base+http.DEFAULT_SETTINGS_UI_PATH+'?embedded=1')
                expect(page.locator('#warehousesGroupPanel')).to_be_visible()
                receipt=page.locator('#warehouseSourceReceipt');expect(receipt.locator('.ff-operation-check')).to_be_visible()
                assert len(writes)==1 and page.evaluate('window.bad') is None
                fid=writes[0]['acceptance']['source_ref']['entity_id']
                card=page.locator('[data-facility-id="'+fid+'"]');expect(card).to_contain_text('Неактивен')
                card.get_by_role('button',name='Активировать',exact=True).evaluate('(b)=>{b.click();b.click();}')
                expect(receipt).to_contain_text('Склад ожидает активации');assert len(writes)==2
                assert not entry.ff_pool_surface.facility_detail(fid)['facility']['active']
                drain=DenseFbsService(db_path=rt.db_path,runtime_dir=rt.runtime_dir,timestamp_factory=lambda:NOW)
                assert drain.drain_facility_activations()['active']==1
                receipt.locator('.ff-operation-link').click();expect(receipt).to_contain_text('Склад активирован.')
                assert len(writes)==2
                page.locator('#reloadWarehousesButton').click();expect(card.get_by_role('button',name='Деактивировать',exact=True)).to_be_enabled()
                previous=receipt.locator('[data-ff-operation-receipt]').get_attribute('data-ff-operation-receipt')
                card.locator('[data-warehouse-name]').fill('Settings renamed');card.get_by_role('button',name='Сохранить имя',exact=True).click()
                expect(receipt.locator('[data-ff-operation-receipt]')).not_to_have_attribute('data-ff-operation-receipt',previous)
                expect(card.locator('[data-warehouse-name]')).to_have_value('Settings renamed');assert len(writes)==3
                previous=receipt.locator('[data-ff-operation-receipt]').get_attribute('data-ff-operation-receipt')
                expect(card.get_by_role('button',name='Деактивировать',exact=True)).to_be_enabled()
                card.get_by_role('button',name='Деактивировать',exact=True).click()
                expect(receipt.locator('[data-ff-operation-receipt]')).not_to_have_attribute('data-ff-operation-receipt',previous)
                expect(card.get_by_role('button',name='Активировать',exact=True)).to_be_enabled();assert len(writes)==4
                assert not entry.ff_pool_surface.facility_detail(fid)['facility']['active']
                # Current whole-source metadata drift blocks before source mutation.
                current=entry.ff_pool_surface.facility_detail(fid)['facility']
                entry.ff_pool_surface.update_facility(fid,{'request_id':'settings-external-edit','name':'External name','expected_updated_at':current['updated_at']},actor='fixture')
                card.locator('[data-warehouse-name]').fill('Stale edit');card.get_by_role('button',name='Сохранить имя',exact=True).click()
                expect(page.locator('#warehousesActivationMessage')).to_contain_text('Склад изменился');assert len(writes)==4
                page.locator('#reloadWarehousesButton').click();expect(card.locator('[data-warehouse-name]')).to_have_value('External name')
                # A shared other-tab WebLock fences this writer too.
                key=marker[0];page.evaluate('key=>{window.unlock=null;window.lock=navigator.locks.request(key,async()=>await new Promise(resolve=>window.unlock=resolve));}',key)
                page.wait_for_function('window.unlock!==null');card.locator('[data-warehouse-name]').fill('Busy edit');card.get_by_role('button',name='Сохранить имя',exact=True).click()
                expect(page.locator('#warehousesActivationMessage')).to_contain_text('другой вкладке');assert len(writes)==4
                page.evaluate('async()=>{window.unlock();await window.lock;}')
                acquire_barrier(rt.runtime_dir,window_id='settings-maintenance',window_kind='snapshot',plan_fingerprint='sha256:'+'a'*64,approval_reference='synthetic-fixture',actor='fixture',reason='fixture')
                ctx.unroute('**'+PATH+'/**',lost);before=rt.db_path.read_bytes()
                card.get_by_role('button',name='Сохранить имя',exact=True).click()
                expect(receipt).to_contain_text('Режим обслуживания');expect(card.get_by_role('button',name='Сохранить имя',exact=True)).to_be_enabled()
                assert not receipt.locator('.ff-operation-check').count() and rt.db_path.read_bytes()==before
                def lose_blocked(route):
                    if route.request.method=='POST':reply=route.fetch();assert reply.status==423;route.abort('failed')
                    else:route.continue_()
                ctx.route('**'+PATH+'/facilities/*',lose_blocked)
                card.get_by_role('button',name='Сохранить имя',exact=True).click()
                expect(receipt).to_contain_text('Проверяем сохранение');expect(card.get_by_role('button',name='Сохранить имя',exact=True)).to_be_disabled()
                page.close();page=ctx.new_page();open_settings(page,base)
                expect(page.locator('#warehouseSourceReceipt')).to_contain_text('Проверяем сохранение')
                expect(page.locator('#createWarehouseButton')).to_be_disabled()
                assert len(writes)==4 and rt.db_path.read_bytes()==before and not errors,errors
                ctx.close();browser.close()
        finally:stop(server,thread)
    print('Settings Chromium preview/onePOST/restart/same-ID/activation/native publication/rename/deactivate/stale/cross-tab/423unknown: PASS')
if __name__=='__main__':main()
