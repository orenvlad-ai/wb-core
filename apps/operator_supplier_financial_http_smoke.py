"""Native parsers/source transactions through actual local financial HTTP."""
from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import closing
from unittest.mock import patch
import sys,json
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.operator_supplier_financial_native_smoke import setup
from apps.operator_supplier_processing_smoke import NOW
from apps.operator_supplier_shipments_http_smoke import server_for,stop,request,PATH
from apps.supplier_financial_documents_smoke import TEXT_BY_FILENAME,_packing_list_workbook_bytes,_post_multipart
from apps.cny_ledger_smoke import BANK_STATEMENT_TEXT,PAYMENT_TEXT
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.supplier_financial_documents import StaticUsdRateProvider
from packages.application import operator_supplier_financial as financial,operator_supplier_shipments as source,supplier_preparation_intents as intents
from packages.application.own_product_capital import OwnProductCapitalBlock
from packages.adapters import registry_upload_http_entrypoint as http


def fixture(raw):
    rt,ledger=setup(raw)
    entry=RegistryUploadHttpEntrypoint(runtime=rt,runtime_dir=rt.runtime_dir,activated_at_factory=lambda:NOW)
    texts={**TEXT_BY_FILENAME,'bank-transfer.pdf':PAYMENT_TEXT,'fees.pdf':BANK_STATEMENT_TEXT}
    extractor=lambda body,name:(texts.get(name,''),{'method':'synthetic text layer'},[])
    entry.cny_ledger_block.pdf_text_extractor=extractor
    entry.supplier_financial_documents_block.pdf_text_extractor=extractor
    entry.supplier_financial_documents_block.usd_rate_provider=StaticUsdRateProvider({'2026-06-02':'80','2026-06-03':'80','2026-06-05':'80','2026-06-29':'80'})
    return rt,entry


def preview(entry,filename):
    return entry.handle_supplier_financial_documents_upload_request('source',_packing_list_workbook_bytes() if filename.endswith('.xlsx') else ('synthetic '+filename).encode(),uploaded_filename=filename,actor='local_operator')


def main():
    with TemporaryDirectory(prefix='supplier-financial-http-') as raw:
        rt,entry=fixture(raw);server,thread,base=server_for(entry);path=PATH+'/source/financial-documents'
        try:
            names=('quote.pdf','invoice-103.pdf','customs.pdf','bank-control.pdf','packing.xlsx','bank-transfer.pdf','fees.pdf')
            previews=[preview(entry,name) for name in names]
            assert all(p.get('confirmation_token') and not p.get('active_saved') for p in previews),previews
            assert not rt.list_supplier_financial_documents('source')
            body={'request_id':'financial-http-seven-types','confirmation_tokens':[p['confirmation_token'] for p in previews]}
            with patch.object(OwnProductCapitalBlock,'recalculate',side_effect=AssertionError('HTTP heavy capital')),patch.object(intents,'resume_supplier_preparation',side_effect=AssertionError('HTTP heavy preparation')):
                code,result=request(base,path+'/confirm-upload','POST',body)
            assert code==200 and result['status']=='accepted',result
            assert [r['status'] for r in result['results']]==['accepted']*6+['preview'],result
            assert result['acceptance']['partial'] and len(result['acceptance']['children'])==6
            assert all(r['acceptance']['domain']==financial.DOMAIN for r in result['results'][:6])
            before=rt.db_path.read_bytes()
            with patch.object(financial,'ensure_schema',side_effect=AssertionError('read bootstrap')),patch.object(entry,'handle_supplier_financial_documents_list_request',side_effect=AssertionError('mutable collection projection')):
                read=request(base,path+'?request_id='+body['request_id'])[1]
                assert read['acceptance']['operation_id']==result['acceptance']['operation_id']
                assert request(base,PATH+'?operation_id='+result['acceptance']['operation_id'])[1]['acceptance']['operation_id']==result['acceptance']['operation_id']
            assert before==rt.db_path.read_bytes()
            with patch.object(entry,'_confirm_supplier_upload_token',side_effect=AssertionError('same-ID source repeat')):
                assert request(base,path+'/confirm-upload','POST',body)[1]['acceptance']['operation_id']==result['acceptance']['operation_id']
            # One native CNY final document, and one durable bank-fee preview
            # requiring selection: its preview is never a green final receipt.
            assert len([d for d in rt.list_cny_documents() if d['source_order_id']=='source'])==1
            assert not result['results'][-1].get('acceptance')
            fee=result['results'][-1]
            import_preview=fee.get('import_preview') or fee.get('preview') or {}
            candidates=import_preview.get('matched_fee_rows') or []
            assert candidates,import_preview
            selected=[candidates[0]['semantic_operation_id']]
            with patch.object(OwnProductCapitalBlock,'recalculate',side_effect=AssertionError('HTTP fee capital')):
                imported=request(base,path+'/'+fee['document_id']+'/confirm-import','POST',{'request_id':'financial-http-fee-selected','selected_operation_ids':selected,'source_sha256':fee.get('source_sha256') or fee.get('file_sha256'),'target_revision':import_preview.get('target_revision') or fee.get('target_revision')})[1]
            assert imported['acceptance'] and imported['results'][0]['acceptance']['durable_saved'],imported
            with closing(source.readonly(rt.db_path)) as conn:
                financial_row=conn.execute('SELECT * FROM sheet_vitrina_v1_supplier_financial_documents WHERE document_id=?',(fee['document_id'],)).fetchone()
                companions=conn.execute('SELECT * FROM sheet_vitrina_v1_cny_documents WHERE linked_financial_document_id=?',(fee['document_id'],)).fetchall()
                children=conn.execute(f'SELECT * FROM {financial.CHILDREN} WHERE subject_id=?',(fee['document_id'],)).fetchall()
                assert financial_row and companions and len(children)==1
                assert all(c['source_order_id']=='source' for c in companions)
            # Atomic source exclusion/restoration applies to the statement's
            # native companions; it never independently re-submits their fees.
            deletion=request(base,path+'/'+fee['document_id']+'/delete-preview','POST',{})[1]
            archived=request(base,path+'/'+fee['document_id']+'/delete-confirm','POST',{'request_id':'financial-http-fee-exclude','confirmation_token':deletion['confirmation_token']})[1]
            assert archived['acceptance']['durable_saved'],archived
            assert all(d['status']=='excluded' for d in rt.list_cny_documents() if d.get('linked_financial_document_id')==fee['document_id'])
            restored_fee=request(base,path+'/'+fee['document_id'],'PATCH',{'request_id':'financial-http-fee-restore','parse_status':'confirmed'})[1]
            assert restored_fee['acceptance']['durable_saved'],restored_fee
            assert all(d['status']!='excluded' for d in rt.list_cny_documents() if d.get('linked_financial_document_id')==fee['document_id'])
            from apps.cny_ledger_smoke import _save_payment
            _save_payment(rt,'zero-fee-payment','source',NOW,'1')
            before_ledger=rt.list_cny_ledger_operations()
            zero=request(base,PATH+'/source/documents/payments/zero-fee-payment/zero-fee','POST',{'request_id':'financial-http-zero-fee','reason':'Банк подтвердил отсутствие комиссии'})[1]
            assert zero['acceptance']['durable_saved'],zero
            assert len(rt.list_supplier_payment_fee_confirmations('source'))==1
            assert rt.list_cny_ledger_operations()==before_ledger
            saved=result['results'][1]['document_id']
            deletion=request(base,path+'/'+saved+'/delete-preview','POST',{})[1]
            excluded=request(base,path+'/'+saved+'/delete-confirm','POST',{'request_id':'financial-http-exclude','confirmation_token':deletion['confirmation_token']})[1]
            assert excluded['acceptance']['durable_saved'],excluded
            assert rt.load_supplier_financial_document(supplier_order_id='source',document_id=saved)['parse_status']=='excluded'
            restored=request(base,path+'/'+saved,'PATCH',{'request_id':'financial-http-restore','parse_status':'confirmed'})[1]
            assert restored['acceptance']['durable_saved'],restored
            assert restored['results'][0]['acceptance']['source_ref']['revision']>excluded['results'][0]['acceptance']['source_ref']['revision']
            refused=request(base,path+'/confirm-upload','POST',{'request_id':'financial-http-invalid-token','confirmation_token':'foreign'})[1]
            assert refused['status']=='rejected' and refused['acceptance'] is None
            assert request(base,path+'?request_id=financial-http-invalid-token')[1]['status']=='rejected'
            with patch.object(http,'_web_auth_config',return_value={'configured':True,'enabled':True}),patch.object(http,'_authenticated_web_user',return_value={'username':'supplier','role':'supplier','allowed_sections':[]}):
                with patch.object(entry,'handle_supplier_financial_operator_request_read',side_effect=AssertionError('late native Supply permission')):
                    assert request(base,path+'?request_id='+body['request_id'])[0]==403
                assert request(base,PATH+'?operation_id='+result['acceptance']['operation_id'])[0]==403
            print('Seven native parsers actual HTTP: six final children plus non-green fee preview; exact RO GET/restart/same-ID; exclusion/restoration version; retained refusal; Supply before read: OK')
        finally:stop(server,thread)


if __name__=='__main__':main()
