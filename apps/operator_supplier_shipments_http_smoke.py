"""Disposable actual HTTP supplier request recovery and native source permissions."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from apps.operator_supplier_shipments_smoke import seed
from apps.sheet_vitrina_v1_supplier_shipments_http_smoke import _reserve_free_port
from packages.adapters import registry_upload_http_entrypoint as http
from packages.application import operator_supplier_shipments as receipts,supplier_preparation_intents as intents
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig

PATH=http.DEFAULT_SUPPLIER_SHIPMENTS_PATH


def server_for(entry):
    config=RegistryUploadHttpEntrypointConfig(host='127.0.0.1',port=_reserve_free_port(),runtime_dir=entry.runtime.runtime_dir,
        upload_path=http.DEFAULT_UPLOAD_PATH,sheet_plan_path=http.DEFAULT_SHEET_PLAN_PATH,
        sheet_refresh_path='/v1/sheet-vitrina-v1/refresh',sheet_status_path=http.DEFAULT_SHEET_STATUS_PATH,
        sheet_operator_ui_path=http.DEFAULT_SHEET_OPERATOR_UI_PATH)
    server=http.build_registry_upload_http_server(config,entrypoint=entry)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    return server,thread,'http://127.0.0.1:'+str(config.port)


def request(base,path,method='GET',payload=None):
    try: response=urlopen(Request(base+path,method=method,data=json.dumps(payload).encode() if payload is not None else None,headers={'Content-Type':'application/json','Accept':'application/json'}),timeout=15)
    except HTTPError as exc:response=exc
    with closing(response):return response.status,json.loads(response.read())


def stop(server,thread):
    server.shutdown();server.server_close();thread.join(timeout=5)


def main():
    with TemporaryDirectory(prefix='operator-supplier-http-') as raw:
        rt,entry,payload=seed(raw);server,thread,base=server_for(entry)
        try:
            with patch.object(intents,'resume_supplier_preparation',side_effect=AssertionError('heavy HTTP')), \
                 patch.object(entry.supplier_shipments_block,'get_shipment',side_effect=AssertionError('heavy postcommit GET')):
                with ThreadPoolExecutor(max_workers=3) as pool:
                    responses=list(pool.map(lambda _:request(base,PATH,'POST',payload),range(3)))
            assert all(status==200 for status,_ in responses),responses
            operations={value['acceptance']['operation_id'] for _,value in responses}
            assert len(operations)==1
            saved=responses[0][1];shipment=saved['shipment_id']
            assert saved['acceptance']['actor']=='local_operator'
            with closing(receipts.readonly(rt.db_path)) as conn:
                assert conn.execute('SELECT count(*) FROM sheet_vitrina_v1_supplier_shipments').fetchone()[0]==1
                assert conn.execute('SELECT count(*) FROM sheet_vitrina_v1_trade_documents').fetchone()[0]==1
                assert conn.execute(f'SELECT count(*) FROM {receipts.TABLE}').fetchone()[0]==1
            before=rt.db_path.read_bytes()
            status,recovered=request(base,PATH+'?request_id='+payload['request_id'])
            assert status==200 and recovered['acceptance']['operation_id']==saved['acceptance']['operation_id']
            assert recovered['shipment']['shipment_id']==shipment and before==rt.db_path.read_bytes()
            # Native supplier role keeps its safe route projection and principal
            # scope. Reports/cash roles gain no supplier read via receipt query.
            with patch.object(http,'_web_auth_config',return_value={'configured':True,'enabled':True}), \
                 patch.object(http,'_authenticated_web_user',return_value={'username':'reports-only','role':'operator','allowed_sections':['reports']}):
                assert request(base,PATH+'?request_id='+payload['request_id'])[0]==403
                assert request(base,PATH,'POST',payload)[0]==403
            with patch.object(http,'_web_auth_config',return_value={'configured':True,'enabled':True}), \
                 patch.object(http,'_authenticated_web_user',return_value={'username':'supplier','role':'supplier','allowed_sections':[]}):
                assert request(base,PATH+'?request_id='+payload['request_id'])[1]['status']=='unknown'
            status,archive=request(base,PATH+'/'+shipment+'?request_id=supplier-http-archive','DELETE')
            assert status==200 and archive['archived']
            archived=request(base,PATH+'?request_id=supplier-http-archive')[1]
            assert archived['shipment']['archived'] and archived['acceptance']['source_ref']['action']=='archive'
            # Re-reading/repeating the old exact request returns its immutable
            # earlier version, never resurrecting the archived supplier source.
            repeat=request(base,PATH,'POST',payload)[1]
            assert repeat['shipment_id']==shipment and rt.load_supplier_shipment(shipment)['header']['archived_at']
        finally:stop(server,thread)
    print('HTTP concurrent same request: one supplier/invoice/receipt; same-ID RO; actor/native grants; archive exact read/no resurrection: OK')


if __name__=='__main__':main()
