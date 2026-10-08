"""Actual supplier contract HTTP: one-shot admission, exact reads and native grants."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from tempfile import TemporaryDirectory
from unittest.mock import patch
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request,urlopen
import json,sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.operator_supplier_contracts_smoke import setup,finish,contract_bytes
from apps.operator_supplier_shipments_http_smoke import server_for,stop,request,PATH
from packages.application import operator_supplier_contracts as receipts,supplier_preparation_intents as intents
from packages.adapters import registry_upload_http_entrypoint as http


def multipart(base,path,identity,body=None,filename='contract.xlsx'):
    boundary='operatorContractFixtureBoundary';fields={'request_id':identity,'number':'C-http'}
    parts=[]
    for key,value in fields.items():parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    parts.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode(),body or contract_bytes(),f'\r\n--{boundary}--\r\n'.encode()])
    try:response=urlopen(Request(base+path,method='POST',data=b''.join(parts),headers={'Content-Type':'multipart/form-data; boundary='+boundary,'Accept':'application/json'}),timeout=15)
    except HTTPError as exc:response=exc
    with closing(response):return response.status,json.loads(response.read())


def main():
    with TemporaryDirectory(prefix='operator-contract-http-') as raw:
        rt,entry,sid=setup(raw);server,thread,base=server_for(entry);path=PATH+'/'+sid+'/contract'
        try:
            with patch.object(entry.supplier_shipments_block,'get_shipment',side_effect=AssertionError('heavy HTTP detail')),patch.object(intents,'resume_supplier_preparation',side_effect=AssertionError('heavy HTTP')):
                with ThreadPoolExecutor(max_workers=3) as pool:responses=list(pool.map(lambda _:multipart(base,path,'contract-http-upload'),range(3)))
            assert all(status==200 and r['status']=='accepted' for status,r in responses),responses
            saved=responses[0][1];op=saved['acceptance']['operation_id'];assert len({r['acceptance']['operation_id'] for _,r in responses})==1
            data=rt.db_path.read_bytes()
            with patch.object(receipts,'ensure_schema',side_effect=AssertionError('GET bootstrap')),patch('packages.application.registry_upload_db_backed_runtime._connect',side_effect=AssertionError('GET writer')):
                assert request(base,path+'?request_id=contract-http-upload')[1]==saved
                assert request(base,PATH+'?operation_id='+op)[1]==saved
            assert rt.db_path.read_bytes()==data
            cid=saved['acceptance']['source_ref']['contract_document_id'];iid=saved['acceptance']['source_ref']['invoice_document_id']
            assert not rt.load_invoice_contract_link(iid)
            finish(rt,sid)
            status,read=request(base,path+'?request_id=contract-http-upload');assert status==200 and read['acceptance']['processing']['complete']
            # Exact alias survives actual source changes and no native upload may be resent.
            with patch.object(entry.supplier_shipments_block,'create_trade_document_from_upload',side_effect=AssertionError('duplicate write')):
                assert multipart(base,path,'contract-http-upload')[1]==read
            status,unlink=request(base,path,'PATCH',{'request_id':'contract-http-unlink','contract_document_id':''})
            assert status==200 and unlink['status']=='accepted' and rt.load_invoice_contract_link(iid)
            finish(rt,sid);assert not rt.load_invoice_contract_link(iid)
            assert request(base,PATH+'?operation_id='+unlink['acceptance']['operation_id'])[1]['acceptance']['processing']['physical_unlink_applied']
            status,rejected=multipart(base,path,'contract-http-rejected',b'bad','bad.txt')
            assert status==200 and rejected['status']=='rejected' and request(base,path+'?request_id=contract-http-rejected')[1]==rejected
            assert request(base,path+'?request_id=unknown-contract-id')[1]['status']=='unknown'
            # Native source permission is checked before source read; Supply does not grant supplier-role access to contracts.
            for user in ({'username':'reports','role':'operator','allowed_sections':['reports']},{'username':'cash','role':'operator','allowed_sections':['cash']},{'username':'supplier','role':'supplier','allowed_sections':[]}):
                with patch.object(http,'_web_auth_config',return_value={'configured':True,'enabled':True}),patch.object(http,'_authenticated_web_user',return_value=user),patch.object(entry,'handle_supplier_contract_read',side_effect=AssertionError('permission after source read')):
                    assert request(base,path+'?request_id=contract-http-upload')[0]==403,user
                    assert request(base,path,'PATCH',{'request_id':'contract-denied-id','contract_document_id':cid})[0]==403,user
            with patch.object(http,'_web_auth_config',return_value={'configured':True,'enabled':True}),patch.object(http,'_authenticated_web_user',return_value={'username':'supplier','role':'supplier','allowed_sections':[]}),patch.object(receipts,'read_operation',side_effect=AssertionError('supplier-safe operation read before native grant')):
                assert request(base,PATH+'?operation_id='+op)[1]['status']=='unknown'
            with patch.object(http,'_web_auth_config',return_value={'configured':True,'enabled':True}),patch.object(http,'_authenticated_web_user',return_value={'username':'foreign','role':'operator','allowed_sections':['supply']}):
                assert request(base,path+'?request_id=contract-http-upload')[1]['status']=='unknown'
                assert request(base,PATH+'?operation_id='+op)[1]['status']=='unknown'
            from packages.application.business_data_write_barrier import acquire_barrier
            acquire_barrier(rt.runtime_dir,window_id='contract-maintenance',window_kind='snapshot',plan_fingerprint='sha256:'+'a'*64,approval_reference='synthetic-fixture',actor='fixture',reason='synthetic maintenance')
            before=rt.db_path.read_bytes()
            status,blocked=request(base,path,'PATCH',{'request_id':'contract-http-blocked','contract_document_id':cid})
            assert status==423 and blocked['code']=='business_data_maintenance'
            assert request(base,path+'?request_id=contract-http-blocked')[1]['status']=='unknown' and before==rt.db_path.read_bytes()
        finally:stop(server,thread)
    print('Actual contract multipart/PATCH + exact request/operation GET, concurrent one source, own actor/native grants before read, RO no bootstrap, pending/applied and maintenance refusal: OK')


if __name__=='__main__':main()
