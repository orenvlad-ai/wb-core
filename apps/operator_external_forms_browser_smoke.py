"""Actual forms, real native previews/writer records, fake WB providers only."""
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json,os,socket,sys,threading
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from playwright.sync_api import sync_playwright
from apps import sku_management_smoke as sku
from apps.wb_prices_management_browser_smoke import _LocalPricesServer
from apps.wb_prices_management_smoke import SIZE_PRICE_NM,PRIMARY_NM
from packages.application.change_registry import ChangeRegistryRepository
from packages.application.change_registry_writer import InternalWriterRegistry
from packages.application.change_registry_observer import ChangeRegistryReadSurface
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig
from packages.adapters import registry_upload_http_entrypoint as web

@contextmanager
def sku_server():
    with TemporaryDirectory() as directory:
        path=Path(directory);runtime=sku._seed_runtime(path);source=sku.FakePrices();ads_source=sku.FakeAds()
        ChangeRegistryRepository(path).initialize_schema();writer=InternalWriterRegistry(runtime_dir=path,seller_id='fixture',account_scope='seller-portal-primary',timestamp_factory=lambda:'2026-07-13T08:00:00Z')
        prices=sku.WbPricesManagementBlock(runtime=runtime,runtime_dir=path,source=source,now_factory=lambda:sku.NOW,timestamp_factory=lambda:'2026-07-13T08:00:00Z',safety_config=sku.WbPricesSafetyConfig(True,300),writer_registry=writer,registry_source_surface="sku_management_price")
        ads=sku.SheetVitrinaV1AdsBlock(runtime=runtime,runtime_dir=path,source=ads_source,now_factory=lambda:sku.NOW,timestamp_factory=lambda:'2026-07-13T08:00:00Z',cache_ttl_seconds=0,safety_config=sku.AdsSafetyConfig(True,100000,Decimal('100'),100000,300),writer_registry=writer,registry_source_surface="sku_management_bid")
        block=sku.SkuManagementBlock(runtime=runtime,runtime_dir=path,prices_block=prices,ads_block=ads,stocks_block=sku.FakeStocksBlock(),sales_history=sku.FakeSalesHistory(),buyer_price_source=sku.FakeBuyer(),now_factory=lambda:sku.NOW,timestamp_factory=lambda:'2026-07-13T08:00:00Z',sleep=lambda _:None,readback_attempts=2,readback_delay_seconds=0)
        dedicated_ads=sku.SheetVitrinaV1AdsBlock(runtime=runtime,runtime_dir=path,source=ads_source,now_factory=lambda:sku.NOW,timestamp_factory=lambda:'2026-07-13T08:00:00Z',cache_ttl_seconds=0,safety_config=sku.AdsSafetyConfig(True,100000,Decimal('100'),100000,300),writer_registry=writer)
        entry=RegistryUploadHttpEntrypoint(runtime_dir=path,runtime=runtime,prices_block=prices,ads_block=dedicated_ads,sku_management_block=block,change_registry_read_surface=ChangeRegistryReadSurface(path,seller_id='fixture'),now_factory=lambda:sku.NOW,activated_at_factory=lambda:'2026-07-13T08:00:00Z')
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        config=RegistryUploadHttpEntrypointConfig(host='127.0.0.1',port=port,upload_path=web.DEFAULT_UPLOAD_PATH,sheet_plan_path=web.DEFAULT_SHEET_PLAN_PATH,sheet_refresh_path='/v1/sheet-vitrina-v1/refresh',sheet_status_path=web.DEFAULT_SHEET_STATUS_PATH,sheet_operator_ui_path=web.DEFAULT_SHEET_OPERATOR_UI_PATH,runtime_dir=path)
        server=web.build_registry_upload_http_server(config,entrypoint=entry);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:yield 'http://127.0.0.1:'+str(port),source,ads_source,block
        finally:server.shutdown();server.server_close();thread.join(timeout=5)

def open_sku(page):
    page.evaluate('nm=>openVitrinaSkuModal(nm,null,"Native fixture")',sku.NM_ID)
    page.wait_for_selector('[data-sku-modal-state="quick_ready"]')

def main():
    with sync_playwright() as pw:
        browser=pw.chromium.launch()
        try:
            with sku_server() as (base,source,ads_source,block):
                page=browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                previews=[];sent=[];controls={'reject':False}
                def preview(route):
                    response=route.fetch();data=response.json();previews.append(data['preview']);route.fulfill(response=response)
                def lose(route):
                    sent.append(route.request.post_data_json)
                    if controls['reject']:
                        # Actual native pre-submit guard: current tuple changed after preview.
                        source.price+=1;route.fulfill(response=route.fetch());return
                    response=route.fetch();assert response.status in (200,409,422,503),response.text();route.abort('failed')
                page.route('**/sku-management/price/preview',preview);page.route('**/sku-management/bid/preview',preview)
                page.route('**/sku-management/price/commit',lose);page.route('**/sku-management/bid/commit',lose)
                page.goto(base+web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH+'?tab=settings');open_sku(page)
                page.locator('[data-quick-sku-price]').fill('850');page.locator('[data-quick-sku-price-preview]').click();page.wait_for_selector('[data-sku-modal-state="preview_ready"]')
                page.locator('[data-sku-modal-confirm]').click();page.wait_for_selector('[data-sku-modal-state="native_receipt"]')
                modal=page.locator('[data-sku-management-modal]');assert modal.locator('.ff-operation-status').inner_text()=='Подтверждено WB',modal.inner_text()
                assert 'не подтверждено' not in modal.inner_text() and 'Controlled error' not in modal.inner_text()
                assert source.upload_calls==1 and len(sent)==1 and previews[-1]['operation_id']
                assert page.evaluate('state.skuManagement.pendingConfirmedUpdate') is None
                assert page.evaluate('state.skuManagement.error')==''
                page.locator('[data-sku-modal-confirm]').click();page.wait_for_function('state.skuManagement.loaded && !state.skuManagement.loading')
                assert page.evaluate('nm=>state.skuManagement.rows.find(r=>r.nm_id===nm).seller_price',sku.NM_ID)==850
                # Lost response for exact native bid has a partial/mismatched receipt,
                # never a completed label or invented local requested value.
                open_sku(page);ads_source.ignore_patch=True
                page.locator('[data-quick-sku-bid]').fill('16');page.locator('[data-quick-sku-bid-preview]').click();page.wait_for_selector('[data-sku-modal-state="preview_ready"]')
                page.locator('[data-sku-modal-confirm]').click();page.wait_for_selector('[data-sku-modal-state="native_receipt"]')
                assert modal.locator('.ff-operation-status').inner_text()!='Подтверждено WB',modal.inner_text()
                assert page.evaluate('state.skuManagement.pendingConfirmedUpdate') is None
                count=len(sent);page.locator('[data-sku-modal-confirm]').click();assert len(sent)==count
                # Definitive native 409 diagnostics preserved; old identity stays fenced.
                open_sku(page);controls['reject']=True
                page.locator('[data-quick-sku-price]').fill('840');page.locator('[data-quick-sku-price-preview]').click();page.wait_for_selector('[data-sku-modal-state="preview_ready"]')
                page.locator('[data-sku-modal-confirm]').click();page.wait_for_selector('[data-sku-modal-state="controlled_error"]')
                assert 'current WB price differs from preview' in modal.inner_text(),modal.inner_text()
                assert source.upload_calls==1
                assert not errors,errors
                # Dedicated Ads actual modal uses a real native preview/operation ID.
                page.locator('[data-sku-modal-confirm]').click();page.locator('[data-sku-modal-cancel]').first.click()
                ads_source.ignore_patch=False
                page.route('**/ads/bid/commit',lose)
                controls['reject']=False
                ads_preview=page.evaluate("""async nm=>{
                  const r=await fetch(WEB_VITRINA_CONFIG.ads_bid_preview_path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({nm_id:nm,advert_id:77,placement:'search',requested_bid_rub:17})});
                  const data=await r.json();if(!r.ok)throw Error(JSON.stringify(data));
                  state.ads.preview=data.preview;state.ads.commitResult=null;document.querySelector('[data-ads-modal]').hidden=false;renderAdsModal();return data.preview;
                }""",sku.NM_ID)
                assert ads_preview['operation_id'];page.locator('[data-ads-commit]').click()
                ads_modal=page.locator('[data-ads-modal]');ads_modal.locator('.ff-operation-status').wait_for()
                assert ads_modal.locator('.ff-operation-status').inner_text()=='Подтверждено WB',ads_modal.inner_text()
                assert page.locator('[data-ads-commit]').is_disabled()
            with patch.dict(os.environ,{'SELLER_PORTAL_CANONICAL_SUPPLIER_ID':'fixture'}):
                fixture=_LocalPricesServer(write_enabled=True,with_registry=True)
                with fixture as base:
                    original=fixture.source.fetch_goods_by_nm_ids
                    def partial_goods(nm_ids):
                        payload=original(nm_ids)
                        changes=fixture.source.upload_payloads[-1] if fixture.source.upload_payloads else []
                        applied=next((r for r in changes if int(r['nmID'])==PRIMARY_NM),None)
                        for good in payload['data']['listGoods']:
                            if int(good['nmID'])==PRIMARY_NM and applied:
                                good['discount']=int(applied.get('discount',good['discount']))
                                for size in good['sizes']:
                                    size['price']=int(applied['price']);size['discountedPrice']=int(applied['price'])*(100-int(good['discount']))/100
                        return payload
                    fixture.source.fetch_goods_by_nm_ids=partial_goods
                    page=browser.new_page();sent=[];previews=[]
                    def prices_preview(route):
                        response=route.fetch();previews.append(response.json()['preview']);route.fulfill(response=response)
                    def prices_lost(route):
                        sent.append(route.request.post_data_json);response=route.fetch();assert response.status==200
                        # Actual native status/readback owner records a partial tuple before the POST reply is lost.
                        upload=response.json()['uploadID'];checked=route.fetch(url=route.request.url+'/'+str(upload),method='GET',post_data=None);assert checked.status==200
                        route.abort('failed')
                    page.route('**/prices/preview',prices_preview);page.route('**/prices/upload-task',prices_lost)
                    page.goto(base+web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH);page.locator('[data-unified-tab-button="sku-management"]').click();page.locator('[data-sku-management-subtab="prices"]').click()
                    page.locator(f'[data-prices-row="{SIZE_PRICE_NM}"]').wait_for();page.locator(f'[data-prices-edit-nm="{PRIMARY_NM}"][data-prices-edit-field="price"]').fill('1100');page.locator(f'[data-prices-edit-nm="{SIZE_PRICE_NM}"][data-prices-edit-field="discount"]').fill('25');page.locator('[data-prices-preview]').click();page.locator('[data-prices-commit]').click()
                    modal=page.locator('[data-prices-modal]');modal.locator('.ff-operation-status').wait_for()
                    assert modal.locator('.ff-operation-status').inner_text()!='Подтверждено WB' and len(sent)==1,modal.inner_text()
                    assert previews[-1]['operation_id'] and len(fixture.source.upload_payloads)==1
                    assert page.evaluate('state.prices.commitResult.acceptance.partial') is True,page.evaluate('state.prices.commitResult')
                    assert page.locator('[data-prices-commit]').is_disabled()
        finally:browser.close()
    print('operator_external_forms_browser_smoke: PASS real native SKU price completed/bid mismatch/409 and dedicated Ads completed/Prices lost-response partial, no duplicate/client price fabrication')
if __name__=='__main__':main()
