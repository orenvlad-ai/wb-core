"""Actual local HTTP/browser acknowledgement and permission tests; synthetic data."""
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
from unittest.mock import patch
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playwright.sync_api import sync_playwright
from packages.adapters import registry_upload_http_entrypoint as http
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.operator_report_source_versions import TABLE
from packages.application.sheet_vitrina_v1_plan_report import BASELINE_TEMPLATE_HEADERS
from packages.application.simple_xlsx import build_single_sheet_workbook_bytes
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig

NOW = datetime(2026,10,8,10,tzinfo=timezone.utc)
STAMP = "2026-10-08T10:00:00Z"


def main():
    with TemporaryDirectory(prefix="operator-reports-browser-") as directory:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(directory))
        bundle = json.loads((ROOT/'artifacts/registry_upload_http_entrypoint/input/registry_upload_bundle__fixture.json').read_text())
        assert runtime.ingest_bundle(bundle,activated_at=STAMP).status=='accepted'
        # A hidden supply source proves counts and exact reads are filtered before projection.
        hidden = runtime.save_factory_order_dataset_state(dataset_type='stock_ff',uploaded_at=STAMP,
            rows=[{'nm_id':1,'quantity':10}],uploaded_filename='hidden.xlsx',uploaded_content_type='xlsx',
            workbook_bytes=b'synthetic hidden file',operation_id='ors_hidden',actor='supply-operator')
        with closing(socket.socket()) as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        config=RegistryUploadHttpEntrypointConfig(host='127.0.0.1',port=port,upload_path=http.DEFAULT_UPLOAD_PATH,
            sheet_plan_path=http.DEFAULT_SHEET_PLAN_PATH,sheet_refresh_path='/v1/sheet-vitrina-v1/refresh',
            sheet_status_path=http.DEFAULT_SHEET_STATUS_PATH,sheet_operator_ui_path=http.DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime.runtime_dir)
        entry=RegistryUploadHttpEntrypoint(runtime_dir=runtime.runtime_dir,runtime=runtime,now_factory=lambda:NOW,
            activated_at_factory=lambda:STAMP)
        user={'username':'report-operator','role':'operator','allowed_sections':['reports']}
        with patch.object(http,'_web_auth_config',return_value={'configured':True,'enabled':True}), \
             patch.object(http,'_authenticated_web_user',side_effect=lambda *_:user):
            server=http.build_registry_upload_http_server(config,entrypoint=entry)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with sync_playwright() as p:
                    browser=p.chromium.launch()
                    context=browser.new_context(viewport={'width':1180,'height':900})
                    page=context.new_page()
                    base=f'http://127.0.0.1:{port}'
                    api='/v1/sheet-vitrina-v1/operations'
                    upload=http.DEFAULT_SHEET_PLAN_REPORT_BASELINE_UPLOAD_PATH
                    controls={'lose_post':False,'lose_read':False,'wrong_domain':False}
                    posts=[]
                    def route(request):
                        path=urlparse(request.request.url).path
                        if path==upload and request.request.method=='POST':
                            response=request.fetch();payload=response.json()
                            identity=request.request.headers['x-operator-operation-id']
                            assert response.status in (200,400,422),payload
                            if response.status==200:
                                assert payload['acceptance']['operation_id']==identity
                                assert payload['acceptance']['actor']=='report-operator'
                            posts.append(identity)
                            if controls['lose_post']:request.abort();return
                            request.fulfill(response=response);return
                        if path.startswith(api+'/'):
                            if controls['lose_read']:request.abort();return
                            if controls['wrong_domain']:
                                response=request.fetch();payload=response.json()
                                if response.status==200:
                                    payload['operation']['domain']='ff_pool_document'
                                    request.fulfill(status=200,content_type='application/json',body=json.dumps(payload));return
                        request.continue_()
                    page.route('**/*',route)
                    navigation=page.goto(base+http.DEFAULT_SHEET_OPERATOR_UI_PATH+'?embedded_tab=reports')
                    assert navigation.status==200,page.locator('body').inner_text()[:300]
                    assert page.locator('#planReportBaselineFileInput').count()==1
                    def upload_file(amount):
                        file=build_single_sheet_workbook_bytes('План',[BASELINE_TEMPLATE_HEADERS,['2026-01',amount,10]])
                        page.locator('#planReportBaselineFileInput').set_input_files({'name':f'baseline-{amount}.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':file})
                    upload_file(100)
                    page.locator('#operatorSourceReceipt .ff-operation-receipt').wait_for()
                    assert page.locator('#operatorSourceReceipt').inner_text().count('Принято')==1
                    assert 'Изменение сохранено.' in page.locator('#operatorSourceReceipt').inner_text()
                    assert len(posts)==1
                    artifacts = os.environ.get('WBC_OPERATOR_BROWSER_ARTIFACT_DIR')
                    if artifacts:
                        Path(artifacts).mkdir(parents=True,exist_ok=True)
                        page.screenshot(path=str(Path(artifacts)/'report-source-accepted.png'))
                    page.locator('#operatorSourceReceipt').get_by_role('button',name='Закрыть',exact=True).click()
                    # Explicit validation refusal plus a lost verification read
                    # survives reload and does not strand the corrected upload.
                    controls['lose_read']=True
                    page.locator('#planReportBaselineFileInput').set_input_files({'name':'bad.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':b'not a workbook'})
                    page.locator('#operatorSourceReceipt').get_by_role('button',name='Проверить сохранение').wait_for()
                    assert len(posts)==2
                    controls['lose_read']=False
                    page.reload()
                    page.locator('#operatorSourceReceipt').get_by_text('Изменение не принято. Проверьте файл и загрузите исправленный вариант.',exact=True).wait_for()
                    assert len(posts)==2
                    assert page.locator('#operatorSourceReceipt .ff-operation-check').count()==0
                    page.locator('#operatorSourceReceipt').get_by_role('button',name='Закрыть',exact=True).click()
                    upload_file(150)
                    page.locator('#operatorSourceReceipt .ff-operation-receipt').wait_for()
                    assert len(posts)==3 and len(set(posts))==3
                    page.locator('#operatorSourceReceipt').get_by_role('button',name='Закрыть',exact=True).click()
                    controls.update(lose_post=True,lose_read=True)
                    upload_file(200)
                    page.locator('#operatorSourceReceipt').get_by_role('button',name='Проверить сохранение').wait_for()
                    assert page.locator('#operatorSourceReceipt .ff-operation-receipt').count()==0
                    assert len(posts)==4
                    controls.update(lose_post=False,lose_read=False,wrong_domain=True)
                    page.locator('#operatorSourceReceipt').get_by_role('button',name='Проверить сохранение').click()
                    page.wait_for_function("!document.querySelector('#operatorSourceReceipt button').disabled")
                    assert page.locator('#operatorSourceReceipt .ff-operation-receipt').count()==0
                    controls['wrong_domain']=False
                    page.reload()  # Durable identity survives a lost response and a page restart.
                    page.locator('#operatorSourceReceipt .ff-operation-receipt').wait_for()
                    assert len(posts)==4
                    assert page.locator('#operatorSourceReceipt [data-ff-operation-receipt]').get_attribute('data-ff-operation-receipt')==posts[-1]
                    page.locator('#operatorSourceReceipt').get_by_role('link',name='Журнал операций').click()
                    page.get_by_role('heading',name='Журнал операций',exact=True).wait_for()
                    page.locator('#journal-detail .ff-operation-receipt').wait_for()
                    page.wait_for_function("document.getElementById('journal-message').textContent==='Всего операций: 3'")
                    assert len(posts)==4
                    assert 'hidden.xlsx' not in page.locator('body').inner_text()
                    if artifacts:
                        page.screenshot(path=str(Path(artifacts)/'operator-journal-desktop.png'))
                        page.set_viewport_size({'width':390,'height':844})
                        page.screenshot(path=str(Path(artifacts)/'operator-journal-mobile.png'))
                        assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
                    denied=context.request.get(base+api+'/'+hidden['operation_id']);assert denied.status==404
                    listed=context.request.get(base+api+'?domain=all').json()
                    assert listed['total']==3 and {v['domain'] for v in listed['items']}=={'plan_report_baseline'}
                    user['allowed_sections']=['supply']
                    listed=context.request.get(base+api+'?domain=all').json()
                    assert listed['total']==1 and listed['items'][0]['operation_id']=='ors_hidden'
                    assert context.request.get(base+api+'/'+posts[0]).status==404
                    user['allowed_sections']=[]
                    assert context.request.get(base+api+'?domain=all').status==403
                    user['role']='supplier';user['allowed_sections']=['supply','reports']
                    assert context.request.get(base+api).status==403
                    with closing(sqlite3.connect(runtime.db_path)) as conn:
                        assert conn.execute(f'SELECT count(*) FROM {TABLE}').fetchone()[0]==4
                    context.close();browser.close()
            finally:
                server.shutdown();thread.join(timeout=5);server.server_close()
    print('operator report sources browser: native upload, lost response/reload GET-only, wrong domain, journal and grants PASS')


if __name__=='__main__':main()
