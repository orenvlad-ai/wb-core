"""Actual native HTTP/browser settings acknowledgement; no production data."""
from contextlib import closing
from datetime import datetime,timezone
import json
from pathlib import Path
import socket
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
from unittest.mock import patch
from urllib.parse import urlparse

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from playwright.sync_api import sync_playwright
from packages.adapters import registry_upload_http_entrypoint as http
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.operator_partner_report import TABLE
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig


def main():
    with TemporaryDirectory(prefix='partner-operation-browser-') as directory:
        runtime=RegistryUploadDbBackedRuntime(runtime_dir=Path(directory))
        bundle=json.loads((ROOT/'artifacts/registry_upload_http_entrypoint/input/registry_upload_bundle__fixture.json').read_text())
        assert runtime.ingest_bundle(bundle,activated_at='2026-10-08T10:00:00Z').status=='accepted'
        with closing(socket.socket()) as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        config=RegistryUploadHttpEntrypointConfig(host='127.0.0.1',port=port,upload_path=http.DEFAULT_UPLOAD_PATH,
            sheet_plan_path=http.DEFAULT_SHEET_PLAN_PATH,sheet_refresh_path='/v1/sheet-vitrina-v1/refresh',
            sheet_status_path=http.DEFAULT_SHEET_STATUS_PATH,sheet_operator_ui_path=http.DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime.runtime_dir)
        entry=RegistryUploadHttpEntrypoint(runtime_dir=runtime.runtime_dir,runtime=runtime,
            now_factory=lambda:datetime(2026,10,8,10,tzinfo=timezone.utc),activated_at_factory=lambda:'2026-10-08T10:00:00Z')
        entry.partner_report_block.ensure_schema()
        with closing(sqlite3.connect(runtime.db_path)) as conn:
            conn.execute("""INSERT INTO sheet_vitrina_v1_nomenclature_items(
                item_id,is_active,nm_id,nomenclature_name,product_type,match_key,aliases_json,created_at,updated_at)
                VALUES('fixture-report-sku',1,101101,'Тестовый товар','other','fixture-report','[]',
                '2026-10-08T10:00:00Z','2026-10-08T10:00:00Z')""")
            conn.commit()
        user={'username':'report-user','role':'operator','allowed_sections':['reports']}
        with patch.object(http,'_web_auth_config',return_value={'configured':True,'enabled':True}), \
             patch.object(http,'_authenticated_web_user',side_effect=lambda *_:user):
            server=http.build_registry_upload_http_server(config,entrypoint=entry)
            worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch();context=browser.new_context();page=context.new_page()
                    base=f'http://127.0.0.1:{port}';url=base+http.DEFAULT_SHEET_OPERATOR_UI_PATH+'?embedded_tab=reports'
                    posts=[];controls={'lost':False}
                    def route(r):
                        path=urlparse(r.request.url).path
                        if path==http.DEFAULT_PARTNER_REPORT_SETTINGS_PATH and r.request.method=='POST':
                            response=r.fetch();payload=response.json();assert response.status==200,payload
                            identity=r.request.headers['x-operator-operation-id'];posts.append(identity)
                            assert payload['acceptance']['operation_id']==identity
                            if controls['lost']:r.abort();return
                            r.fulfill(response=response);return
                        if path.startswith('/v1/sheet-vitrina-v1/operations/') and controls['lost']:
                            r.abort();return
                        r.continue_()
                    context.route('**/*',route)
                    page.goto(url);page.locator('[data-report-section-button="partner"]').click()
                    page.locator('#partnerReportControls').wait_for(state='visible')
                    page.locator('#partnerReportNmId').select_option('101101')
                    for selector,value in {'Share':'40','Capital':'500000','Reserve':'20','Office':'10000','Tax':'6'}.items():
                        page.locator('#partnerReport'+selector).fill(value)
                    page.locator('#partnerReportSaveSettings').click()
                    page.locator('#operatorSourceReceipt .ff-operation-check').wait_for()
                    assert 'Изменение сохранено.' in page.locator('#operatorSourceReceipt').inner_text()
                    page.locator('#operatorSourceReceipt').get_by_role('button',name='Закрыть',exact=True).click()
                    controls['lost']=True
                    page.locator('#partnerReportShare').fill('41');page.locator('#partnerReportSaveSettings').click()
                    page.locator('#operatorSourceReceipt').get_by_role('button',name='Проверить сохранение').wait_for()
                    assert len(posts)==2
                    page.close();controls['lost']=False;page=context.new_page();page.goto(url)
                    page.locator('#operatorSourceReceipt .ff-operation-check').wait_for()
                    assert len(posts)==2
                    assert page.locator('[data-ff-operation-receipt]').get_attribute('data-ff-operation-receipt')==posts[-1]
                    page.locator('#operatorSourceReceipt').get_by_role('link',name='Журнал операций').click()
                    page.locator('#journal-detail .ff-operation-check').wait_for()
                    assert 'Настройки партнёрского отчёта' in page.locator('#journal-detail').inner_text()
                    api=base+'/v1/sheet-vitrina-v1/operations'
                    listed=context.request.get(api+'?domain=all').json();assert listed['total']==2,listed
                    user['allowed_sections']=['supply']
                    assert context.request.get(api+'/'+posts[-1]).status==404
                    assert context.request.get(api+'?domain=all').json()['total']==0
                    with closing(sqlite3.connect(runtime.db_path)) as conn:
                        assert conn.execute('SELECT count(*) FROM '+TABLE).fetchone()[0]==2
                        assert conn.execute('SELECT count(*) FROM partner_report_settings_versions').fetchone()[0]==2
                    context.close();browser.close()
            finally:
                server.shutdown();worker.join(timeout=5);server.server_close()
    print('operator partner report: exact native save, lost response/tab closure, common journal and grants PASS')


if __name__=='__main__':main()
