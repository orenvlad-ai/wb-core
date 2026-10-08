"""Actual old buttons -> modern blank draft -> one native confirmation in Chromium."""
import sys
import sqlite3
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from apps.operator_inventory_documents_smoke import InventoryDocuments
from apps.operator_facility_mappings_http_smoke import server_for, stop
from apps.operator_manual_ff_stock_smoke import cutover
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.adapters import registry_upload_http_entrypoint as http
from packages.application import operator_warehouse_documents as operations


def main():
    fixture=InventoryDocuments(methodName='runTest');fixture.setUp()
    server=thread=None
    try:
        fixture.set_catalog();target=fixture.base_document();cutover(fixture.runtime)
        entry=RegistryUploadHttpEntrypoint(runtime=fixture.runtime,runtime_dir=fixture.runtime.runtime_dir)
        entry.ff_pool_surface=fixture.surface
        server,thread,base=server_for(entry)
        with sync_playwright() as pw:
            browser=pw.chromium.launch();context=browser.new_context();page=context.new_page();posts=[];errors=[];final=[]
            context.on('request',lambda req:posts.append(req.url) if req.method=='POST' else None)
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(base+http.DEFAULT_SHEET_OPERATOR_UI_PATH+'?embedded_tab=factory-order')
            page.locator('[data-supply-mode-button="fulfillment"]').click()
            page.locator('[data-ff-section-button="operations"]').click()
            with page.expect_popup() as opened:page.locator('#ffStockWriteoffButton').click()
            modern=opened.value;modern.on('pageerror',lambda error:errors.append(str(error)))
            modal=modern.locator('[data-ff-pool-modal]');expect(modal).to_be_visible()
            expect(modern.locator('[data-ff-manual-draft]')).to_contain_text('Списание.')
            kind=modern.locator('[data-ff-pool-action-kind]');expect(kind).to_have_value('')
            expect(modern.locator('[data-ff-pool-preview]')).to_be_disabled()
            assert not posts,posts
            # A forged same-origin message from an unrelated window is not a draft owner.
            modern.evaluate('window.postMessage({type:"wbc_manual_ff_stock_draft",purpose:"manual_receipt",rows:[{nm_id:1,quantity_delta:999}]},location.origin)')
            expect(modern.locator('[data-ff-manual-draft]')).to_contain_text('Списание.')
            # Explicit document basis; no delta or money was guessed by the routing.
            kind.select_option('correction')
            expect(modern.locator('[data-ff-pool-facility]')).to_have_value('')
            expect(modern.locator('[data-ff-pool-items]')).to_have_value('')
            expect(modern.locator('[data-ff-pool-target]')).to_have_value('')
            expect(modern.locator('[data-ff-pool-source-pool]')).to_have_value('')
            modern.locator('[data-ff-pool-target]').fill(target)
            modern.locator('[data-ff-pool-facility]').select_option('A')
            modern.locator('[data-ff-pool-source-pool]').select_option('FBS')
            modern.locator('[data-ff-pool-business-date]').fill('2026-09-08')
            modern.locator('[data-ff-pool-items]').fill('1 -1 -10')
            modern.locator('[data-ff-pool-preview]').click()
            confirm=modern.get_by_role('button',name='Подтвердить проведение',exact=True)
            expect(confirm).to_be_enabled()
            assert not modern.locator('[data-ff-operation-receipt]').count()
            def lose(route):
                if route.request.method=='POST':
                    reply=route.fetch();assert reply.status==200;final.append(reply.json());route.abort('failed')
                else:route.continue_()
            context.route('**/facility-pools/requests/*/confirm',lose)
            confirm.evaluate('(button)=>{button.click();button.click();}')
            expect(modern.locator('[data-ff-pool-workflow-detail]')).to_contain_text('Принято')
            assert len(final)==1 and final[0]['acceptance']['durable_saved'] and final[0]['acceptance']['state']!='completed',final
            identity=final[0]['request_id']
            modern.close()
            with page.expect_popup() as reopened:page.locator('#ffStockReceiptButton').click()
            modern=reopened.value
            expect(modern.locator('[data-ff-operation-receipt]')).to_have_attribute('data-ff-operation-receipt',identity)
            assert len(final)==1 and not any('/ff-stocks/preview' in url or '/ff-stocks/confirm' in url for url in posts),posts
            with sqlite3.connect(fixture.runtime.db_path) as conn:
                assert not conn.execute("SELECT 1 FROM sheet_vitrina_v1_ff_stock_operations WHERE source_type='manual_excel'").fetchone()
            assert operations.read_acceptance(fixture.runtime.db_path,identity)['durable_saved']
            assert not errors,errors
            context.close()
            # The production embedded entry uses the same guarded draft-only bridge.
            embedded_context=browser.new_context();root_page=embedded_context.new_page();embedded_posts=[]
            embedded_context.on('request',lambda req:embedded_posts.append(req.url) if req.method=='POST' else None)
            root_page.goto(base+http.DEFAULT_SHEET_WEB_VITRINA_UI_PATH+'?tab=factory-order')
            root_page.locator('[data-unified-tab-button="factory-order"]').click()
            frame=root_page.frame_locator('[data-operator-embed-frame="factory-order"]')
            frame.locator('[data-supply-mode-button="fulfillment"]').click()
            frame.locator('[data-ff-section-button="operations"]').click()
            frame.locator('#ffStockReceiptButton').click()
            expect(root_page.locator('[data-ff-manual-draft]')).to_contain_text('Приход.')
            expect(root_page.locator('[data-ff-pool-action-kind]')).to_have_value('')
            # Transfer only a reference, with DOM text; quantity is never an inventory target.
            frame.locator('#ffStockReceiptButton').evaluate('button=>window.parent.postMessage({type:"wbc_manual_ff_stock_draft",purpose:"manual_receipt",rows:[{nm_id:1,quantity_delta:5},{nm_id:"<img src=x>",quantity_delta:9}]},location.origin)')
            expect(root_page.locator('[data-ff-manual-draft]')).to_contain_text('1: 5')
            assert not root_page.locator('[data-ff-manual-draft] img').count()
            root_page.locator('[data-ff-pool-action-kind]').select_option('pool_inventory')
            expect(root_page.locator('[data-ff-pool-facility]')).to_have_value('')
            expect(root_page.locator('[data-ff-pool-scope]')).to_have_value('')
            assert root_page.locator('[data-ff-pool-workbook]').input_value()=='' and not embedded_posts,embedded_posts
            embedded_context.close();browser.close()
    finally:
        if server:stop(server,thread)
        fixture.doCleanups()
    print('manual FF actual Chromium: old buttons open blank modern draft without writes; explicit basis/facility/pool, one native confirmation, double click + lost response + closed-tab same-ID recovery, no legacy POST: OK')

if __name__=='__main__':main()
