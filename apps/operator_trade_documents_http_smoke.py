"""Actual library HTTP exact source identity and native operator permissions."""
from tempfile import TemporaryDirectory
from unittest.mock import patch
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.operator_supplier_shipments_smoke import seed
from apps.operator_supplier_shipments_http_smoke import server_for,stop,request
from apps.operator_trade_documents_smoke import upload,doc_id,contract_bytes
from packages.application import operator_trade_documents as receipts
from packages.adapters import registry_upload_http_entrypoint as http


def main():
    with TemporaryDirectory(prefix='operator-library-http-') as raw:
        rt,entry,_=seed(raw);iid=doc_id(upload(entry,'library-http-invoice'));cid=doc_id(upload(entry,'library-http-contract','contract',contract_bytes()))
        server,thread,base=server_for(entry)
        try:
            path=http.DEFAULT_TRADE_DOCUMENTS_PATH
            payload={'request_id':'library-http-link','contract_document_id':cid}
            status,linked=request(base,path+'/'+iid+'/contract','PATCH',payload)
            assert status==200 and linked['acceptance']['processing']['cost_applicable'] is False,(status,linked)
            oid=linked['acceptance']['operation_id'];before=rt.db_path.read_bytes()
            with patch.object(receipts,'ensure_schema',side_effect=AssertionError('GET bootstrap')),patch.object(entry.supplier_shipments_block,'link_invoice_to_contract',side_effect=AssertionError('resubmit')):
                assert request(base,path+'?request_id='+payload['request_id'])[1]==linked
                assert request(base,path+'?operation_id='+oid)[1]==linked
                assert request(base,path+'/'+iid+'/contract','PATCH',payload)[1]==linked
            with patch('packages.application.registry_upload_db_backed_runtime._connect',side_effect=AssertionError('library GET writer')):
                assert request(base,path)[0]==200
                assert entry.handle_trade_documents_file_request(iid)[0]==b'invoice-fixture'
            assert rt.db_path.read_bytes()==before
            status,refusal=request(base,path+'/'+cid,'DELETE',{'request_id':'library-http-blocked'})
            assert status==200 and refusal['status']=='rejected' and not refusal['acceptance']
            assert request(base,path+'?request_id=library-http-blocked')[1]==refusal
            status,unlink=request(base,path+'/'+iid+'/contract','DELETE',{'request_id':'library-http-unlink'})
            assert status==200 and unlink['acceptance']['source_ref']['action']=='unlink'
            assert not rt.load_invoice_contract_link(iid)
            # Role guard precedes alias/detail/list projection; Supply alone does not expand library access.
            user={'username':'foreign','role':'operator','allowed_sections':['settings']}
            with patch.object(http,'_web_auth_config',return_value={'enabled':True,'configured':True}),patch.object(http,'_authenticated_web_user',return_value=user):
                assert request(base,path+'?request_id='+payload['request_id'])[1]['status']=='unknown'
                assert request(base,path+'?operation_id='+oid)[1]['status']=='unknown'
            with patch.object(http,'_web_auth_config',return_value={'enabled':True,'configured':True}),patch.object(http,'_authenticated_web_user',return_value={'username':'supplier','role':'supplier'}),patch.object(entry,'handle_trade_operator_read',side_effect=AssertionError('guard after source read')):
                assert request(base,path+'?operation_id='+oid)[0]==403
                assert request(base,path+'?request_id='+payload['request_id'])[0]==403
            status,archived=request(base,path+'/'+cid,'DELETE',{'request_id':'library-http-archive'})
            assert status==200 and rt.load_trade_document(cid)['status']=='archived'
            assert request(base,path+'?request_id=library-http-archive')[1]==archived
        finally:stop(server,thread)
    print('Actual library HTTP link/unlink/archive, immutable same-ID/detail query-only, retained refusal and permission before actor read: OK')


if __name__=='__main__':main()
