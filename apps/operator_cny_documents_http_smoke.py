"""Disposable actual CNY HTTP: source acknowledgement and permission before read."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import hashlib
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.operator_supplier_financial_native_smoke import setup
from apps.supplier_payment_advance_smoke import fixture as advance_fixture, allocate
from apps.operator_supplier_shipments_http_smoke import server_for,stop,request
from packages.adapters import registry_upload_http_entrypoint as http
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application import operator_cny_documents as cny,operator_supplier_financial as financial


def main():
    with TemporaryDirectory(prefix='operator-cny-http-') as raw:
        rt,_=setup(raw);entry=RegistryUploadHttpEntrypoint(runtime_dir=rt.runtime_dir,runtime=rt)
        server,thread,base=server_for(entry)
        try:
            path=http.DEFAULT_CNY_ACCOUNT_PATH
            payload={'request_id':'cny-http-opening','operation_date':'2026-07-24','cny_amount':'200','rub_value':'2000'}
            status,result=request(base,http.DEFAULT_CNY_ACCOUNT_OPENING_BALANCE_PATH,'POST',payload)
            assert status==200 and result['acceptance']['domain']==cny.DOMAIN,(status,result)
            identity=result['acceptance']['operation_id'];before=rt.db_path.read_bytes()
            with patch.object(entry.cny_ledger_block,'replay_ledger',side_effect=AssertionError('GET financial write')),patch.object(financial,'ensure_schema',side_effect=AssertionError('GET schema')):
                assert request(base,path+'?request_id=cny-http-opening')[1]['acceptance']['operation_id']==identity
                assert request(base,http.DEFAULT_SUPPLIER_SHIPMENTS_PATH+'?operation_id='+identity)[1]['domain']==cny.DOMAIN
                assert request(base,path)[1]['financial_authority']['current']
                request(base,http.DEFAULT_CNY_ACCOUNT_CONVERSIONS_PATH);request(base,http.DEFAULT_CNY_ACCOUNT_LEDGER_PATH)
            assert rt.db_path.read_bytes()==before
            with patch.object(entry.cny_ledger_block,'create_opening_balance',side_effect=AssertionError('same-ID resubmit')):
                assert request(base,http.DEFAULT_CNY_ACCOUNT_OPENING_BALANCE_PATH,'POST',payload)[1]['acceptance']['operation_id']==identity
            rejected=request(base,http.DEFAULT_CNY_ACCOUNT_OPENING_BALANCE_PATH,'POST',{'request_id':'cny-http-bad-date','operation_date':'bad'})[1]
            assert rejected['status']=='rejected' and not rejected['acceptance']
            assert request(base,path+'?request_id=cny-http-bad-date')[1]['status']=='rejected'
            document_id=result['results'][0]['document_id']
            deleted=request(base,http.DEFAULT_CNY_ACCOUNT_DOCUMENTS_PATH+'/'+document_id,'DELETE',{'request_id':'cny-http-exclude'})[1]
            assert deleted['acceptance']['domain']==cny.DOMAIN and rt.load_cny_document(document_id)['status']=='excluded'
            foreign={'username':'other','role':'operator','section_permissions':['supply']}
            with patch.object(http,'_web_auth_config',return_value={'enabled':True,'configured':True}),patch.object(http,'_authenticated_web_user',return_value=foreign),patch.object(http,'_user_has_section_access',return_value=True):
                assert request(base,path+'?request_id=cny-http-opening')[1]['status']=='unknown'
            with patch.object(http,'_web_auth_config',return_value={'enabled':True,'configured':True}),patch.object(http,'_authenticated_web_user',return_value=foreign),patch.object(http,'_user_has_section_access',return_value=False),patch.object(entry,'handle_cny_operator_request_read',side_effect=AssertionError('permission after source read')):
                assert request(base,path+'?request_id=cny-http-opening')[0]==403
        finally:stop(server,thread)
    with TemporaryDirectory(prefix='operator-cny-advance-http-') as raw:
        rt,block=advance_fixture(raw)
        allocate(rt,block)
        entry=RegistryUploadHttpEntrypoint(runtime_dir=rt.runtime_dir,runtime=rt)
        server,thread,base=server_for(entry)
        try:
            before=rt.db_path.read_bytes()
            status,result=request(base,http.DEFAULT_CNY_ACCOUNT_PATH)
            assert status==200 and result['financial_authority']['current'],(status,result)
            assert len(result['supplier_advances'])==1,result
            advance=result['supplier_advances'][0]
            assert advance['status']=='unapplied' and advance['amount_cny']=='25' and not advance['included_in_cogs'],advance
            assert rt.db_path.read_bytes()==before
            # Changed source invalidates the same authority flag used by the UI
            # to hide advance amounts while account preparation is pending.
            document=rt.load_cny_document('balance')
            rt.save_cny_document({**document,'parsed_payload':{**document['parsed_payload'],'review_note':'pending source revision'}})
            before=rt.db_path.read_bytes()
            with patch.object(entry.cny_ledger_block,'replay_ledger',side_effect=AssertionError('GET financial write')):
                status,pending=request(base,http.DEFAULT_CNY_ACCOUNT_PATH)
            assert status==200 and not pending['financial_authority']['current'],(status,pending)
            assert pending['supplier_advances'][0]['amount_cny']=='25'
            assert rt.db_path.read_bytes()==before
        finally:stop(server,thread)
    print('Actual CNY HTTP source/core, exact GET and actor domain, no bootstrap/financial write, retained refusal, exclude and permission before projection: OK')


if __name__=='__main__':main()
