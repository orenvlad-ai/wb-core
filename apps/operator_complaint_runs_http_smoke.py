#!/usr/bin/env python3
"""Actual native HTTP admission/read/grants/maintenance; synthetic provider only."""
from contextlib import contextmanager,closing
from pathlib import Path
from tempfile import TemporaryDirectory
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch
from threading import Thread
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import socket,json,sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.operator_complaint_runs_smoke import fixture,body,worker,hold,native,op
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.change_registry_observer import ChangeRegistryReadSurface
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig
from packages.application import operator_operations as journal
from packages.adapters import registry_upload_http_entrypoint as http
READ=http.DEFAULT_SHEET_FEEDBACKS_AUTO_COMPLAINTS_RUN_PATH

def request(base,path,method='GET',payload=None):
    try:response=urlopen(Request(base+path,method=method,data=json.dumps(payload).encode() if payload is not None else None,headers={'Content-Type':'application/json','Accept':'application/json'}),timeout=15)
    except HTTPError as exc:response=exc
    with closing(response):return response.status,json.loads(response.read())
@contextmanager
def server():
    with TemporaryDirectory(prefix='complaint-run-http-') as tmp,patch.object(http,'_web_auth_config',return_value={'configured':True,'enabled':False}):
        path=Path(tmp);app=RegistryUploadHttpEntrypoint(path,change_registry_read_surface=ChangeRegistryReadSurface(path,seller_id='fixture'))
        block,scope,feedbacks=fixture(path);app.feedbacks_auto_complaints_block=block
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        cfg=RegistryUploadHttpEntrypointConfig(host='127.0.0.1',port=port,runtime_dir=path,upload_path=http.DEFAULT_UPLOAD_PATH,sheet_plan_path=http.DEFAULT_SHEET_PLAN_PATH,sheet_refresh_path='/v1/sheet-vitrina-v1/refresh',sheet_status_path=http.DEFAULT_SHEET_STATUS_PATH,sheet_operator_ui_path=http.DEFAULT_SHEET_OPERATOR_UI_PATH)
        srv=http.build_registry_upload_http_server(cfg,entrypoint=app);thread=Thread(target=srv.serve_forever,daemon=True);thread.start()
        try:yield 'http://127.0.0.1:'+str(port),app,block,feedbacks
        finally:srv.shutdown();srv.server_close();thread.join(5)

def main():
    with server() as (base,app,block,feedbacks),patch.object(native,'admitted_thread',side_effect=hold) as spawned:
        # Parser refusal happens before payload exists and before native owner.
        before=block.store.path.read_bytes()
        with patch.object(app,'handle_sheet_feedbacks_auto_complaints_run_now_request',side_effect=AssertionError('invalid JSON reached owner')) as owner:
            for invalid in (b'{',b'\xff',b'{"x":\xff}',b'[]'):
                try: response=urlopen(Request(base+op.PATH,method='POST',data=invalid,headers={'Content-Type':'application/json'}),timeout=15)
                except HTTPError as error: response=error
                with closing(response):
                    refused=json.loads(response.read());assert response.status==422 and refused['status']=='unknown' and refused['operation_id'] is None,refused
            assert owner.call_count==0 and block.store.path.read_bytes()==before
        payload=body(block);alias=READ+'?operation_id='+payload['operation_id']
        with ThreadPoolExecutor(3) as pool:results=list(pool.map(lambda _:request(base,op.PATH,'POST',payload),range(3)))
        assert all(code==202 for code,_ in results),results
        saved=results[0][1];assert spawned.call_count==1 and not feedbacks.reads and saved['acceptance']['actor']=='local_operator'
        before=block.store.path.read_bytes()
        with patch.object(native.JsonFileFeedbacksAutoComplaintsStore,'__init__',side_effect=AssertionError('GET constructor writes')),patch.object(block,'_journal_by_feedback_id',side_effect=AssertionError('GET provider journal')):
            assert request(base,alias)==(200,saved)
        assert block.store.path.read_bytes()==before
        status,rejected=request(base,op.PATH,'POST',body(block,2));assert status==409 and rejected['status']=='not_saved' and rejected['code']=='complaint_run_native_busy'
        assert request(base,READ+'?operation_id='+body(block,2)['operation_id'])[1]['status']=='unknown'
        for user in ({'username':'reports','role':'operator','allowed_sections':['reports']},{'username':'cash','role':'operator','allowed_sections':['cash']},{'username':'supplier','role':'supplier','allowed_sections':[]}):
            with patch.object(http,'_web_auth_config',return_value={'configured':True,'enabled':True}),patch.object(http,'_authenticated_web_user',return_value=user),patch.object(app,'handle_operator_complaint_run_read',side_effect=AssertionError('denied before source')):
                assert request(base,alias)[0]==403
                assert request(base,op.PATH,'POST',payload)[0]==403
                assert request(base,'/v1/sheet-vitrina-v1/operations?domain='+op.DOMAIN)[1]['total']==0
                assert request(base,'/v1/sheet-vitrina-v1/operations/'+payload['operation_id'])[0]==404
        with patch.object(http,'_web_auth_config',return_value={'configured':True,'enabled':True}),patch.object(http,'_authenticated_web_user',return_value={'username':'foreign','role':'operator','allowed_sections':['feedbacks']}):
            assert request(base,alias)[1]['status']=='unknown'
            assert request(base,'/v1/sheet-vitrina-v1/operations?domain='+op.DOMAIN+'&search='+payload['operation_id'])[1]['total']==0
        scope=op.RunScope.from_entrypoint(app,actor='local_operator');assert journal.journal(app.runtime.db_path,allowed_domains={op.DOMAIN},complaint_runs_scope=scope)['total']==1
        worker(block,saved);done=request(base,alias)[1];assert done['acceptance']['state']=='completed' and feedbacks.reads==1
        assert request(base,op.PATH,'POST',payload)[1]['acceptance']==done['acceptance'] and spawned.call_count==1
        assert request(base,'/v1/sheet-vitrina-v1/operations/'+payload['operation_id'])[1]['operation']['state']=='completed'
        # Actual native barrier denies before native admission; GET remains RO.
        from packages.application.business_data_write_barrier import acquire_barrier
        acquire_barrier(block.runtime_dir,window_id='complaint-run-maintenance',window_kind='snapshot',plan_fingerprint='sha256:'+'a'*64,approval_reference='synthetic',actor='fixture',reason='synthetic maintenance')
        blocked=body(block,3);before=block.store.path.read_bytes()
        assert request(base,op.PATH,'POST',blocked)[0]==423
        assert request(base,READ+'?operation_id='+blocked['operation_id'])[1]['status']=='unknown'
        assert block.store.path.read_bytes()==before and spawned.call_count==1
    print('operator_complaint_runs_http_smoke: PASS actual concurrent POST/one native job/query-only GET/source grant+principal before count/detail/exact native terminal/maintenance zero source write')
if __name__=='__main__':main()
