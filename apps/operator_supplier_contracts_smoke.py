"""Actual native contract source + preparation post-action + immutable recovery."""
from contextlib import closing
from functools import lru_cache
import json,sqlite3,sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.operator_supplier_shipments_smoke import seed
from apps.operator_trade_documents_smoke import contract_bytes as build_contract_bytes
from packages.application import operator_supplier_contracts as receipts,supplier_preparation_intents as intents
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint


@lru_cache(maxsize=1)
def contract_bytes():return build_contract_bytes()


def setup(raw):
    rt,entry,payload=seed(raw)
    saved=entry.handle_supplier_shipments_create_request(payload)
    return rt,entry,saved['shipment_id']


def upload_contract(entry,sid,identity,body=None,filename='contract.xlsx'):
    return entry.handle_supplier_shipments_contract_upload_request(sid,body or contract_bytes(),
        uploaded_filename=filename,fields={'request_id':identity},actor='fixture')


def native(rt,sid):
    with closing(receipts.source.readonly(rt.db_path)) as conn:
        return dict(conn.execute(f'SELECT * FROM {intents.TABLE} WHERE shipment_id=?',(sid,)).fetchone())


def finish(rt,sid):
    result=intents.drain_supplier_preparation_intents(rt,shipment_ids=[sid])
    assert result['status']=='queued',result
    return result


def read(rt,sid,identity):return receipts.read(rt.db_path,identity,shipment_id=sid,request_scope='local_operator')


def main():
    with TemporaryDirectory(prefix='operator-contract-native-') as raw:
        rt,entry,sid=setup(raw);iid=rt.load_supplier_shipment(sid)['header']['invoice_document_id'];before=native(rt,sid)
        with patch.object(entry.supplier_shipments_block,'get_shipment',side_effect=AssertionError('heavy detail')),patch.object(intents,'resume_supplier_preparation',side_effect=AssertionError('heavy HTTP')):
            saved=upload_contract(entry,sid,'contract-native-upload')
        receipt=saved['acceptance'];cid=receipt['source_ref']['contract_document_id'];op=receipt['operation_id']
        assert saved['status']=='accepted' and receipt['state']=='processing' and not receipt['processing']['complete']
        assert rt.load_invoice_contract_link(iid) is None
        pending=native(rt,sid);assert pending['revision']==before['revision']+1 and pending['status']=='pending' and pending['costs_required']==1
        assert pending['affected_nm_ids_json']==before['affected_nm_ids_json'] and pending['effective_date']==before['effective_date']
        assert json.loads(pending['post_actions_json'])['invoice_contract'][receipts.CORRELATION]==op
        data=rt.db_path.read_bytes();restarted=RegistryUploadHttpEntrypoint(runtime_dir=rt.runtime_dir,runtime=rt)
        with patch.object(restarted.supplier_shipments_block,'create_trade_document_from_upload',side_effect=AssertionError('resubmit')),patch.object(receipts,'ensure_schema',side_effect=AssertionError('RO bootstrap')):
            assert upload_contract(restarted,sid,'contract-native-upload')==saved
            assert read(rt,sid,'contract-native-upload')==saved
            assert receipts.read_operation(rt.db_path,op,request_scope='foreign') is None
        assert rt.db_path.read_bytes()==data
        finish(rt,sid);processed=read(rt,sid,'contract-native-upload')['acceptance']
        assert processed['processing']['complete'] and processed['processing']['physical_link_applied'] and processed['processing']['effect']=='linked'
        assert rt.load_invoice_contract_link(iid)['contract_document_id']==cid
        # A SHA duplicate creates a new admitted intent, never another native file.
        count=len(rt.list_trade_documents());duplicate=upload_contract(entry,sid,'contract-native-duplicate')
        assert duplicate['acceptance']['source_ref']['contract_document_id']==cid and len(rt.list_trade_documents())==count
        assert not duplicate['acceptance']['processing']['complete']
        finish(rt,sid);assert read(rt,sid,'contract-native-duplicate')['acceptance']['processing']['effect']=='unchanged'
        # Exact unlink source accepted now, physical unlink later.
        unlink=entry.handle_supplier_shipments_contract_patch_request(sid,{'request_id':'contract-native-unlink','contract_document_id':''})
        assert unlink['status']=='accepted' and rt.load_invoice_contract_link(iid)
        finish(rt,sid);removed=read(rt,sid,'contract-native-unlink')['acceptance'];assert removed['processing']['physical_unlink_applied'] and not rt.load_invoice_contract_link(iid)
        again=entry.handle_supplier_shipments_contract_patch_request(sid,{'request_id':'contract-native-no-effect','contract_document_id':''})
        finish(rt,sid);assert read(rt,sid,'contract-native-no-effect')['acceptance']['processing']['effect']=='unchanged'
        # Queue delivery flags alone do not prove the physical source effect.
        stale=entry.handle_supplier_shipments_contract_patch_request(sid,{'request_id':'contract-native-flag','contract_document_id':cid})
        with sqlite3.connect(rt.db_path) as conn:conn.execute(f"UPDATE {intents.TABLE} SET status='delivered' WHERE shipment_id=?",(sid,));conn.commit()
        assert not read(rt,sid,'contract-native-flag')['acceptance']['processing']['complete']
        # Explicit newer action supersedes the old post-action but keeps cost demand.
        newer=entry.handle_supplier_shipments_contract_patch_request(sid,{'request_id':'contract-native-newer','contract_document_id':''})
        superseded=read(rt,sid,'contract-native-flag')['acceptance'];assert superseded['state']=='needs_attention' and superseded['processing']['terminal'] and superseded['processing']['superseded_by']==newer['acceptance']['operation_id']
        finish(rt,sid);assert not rt.load_invoice_contract_link(iid)
        assert read(rt,sid,'contract-native-upload')['acceptance']==processed
        refused=upload_contract(entry,sid,'contract-native-bad',body=b'bad',filename='bad.txt')
        assert refused['status']=='rejected' and not refused['acceptance'] and read(rt,sid,'contract-native-bad')==refused
        with sqlite3.connect(rt.db_path) as conn:
            try:conn.execute(f'DELETE FROM {receipts.STAGES}')
            except sqlite3.IntegrityError:pass
            else:raise AssertionError('mutable contract proof')
    with TemporaryDirectory(prefix='operator-contract-atomic-') as raw:
        rt,entry,sid=setup(raw);before=native(rt,sid);count=len(rt.list_trade_documents())
        with patch.object(receipts,'_accept_link_intent',side_effect=RuntimeError('stop-inside-native-source')):
            try:upload_contract(entry,sid,'contract-native-interrupted')
            except RuntimeError:pass
            else:raise AssertionError('lost source exception')
        assert len(rt.list_trade_documents())==count and native(rt,sid)==before and read(rt,sid,'contract-native-interrupted')['status']=='unknown'
    with TemporaryDirectory(prefix='operator-contract-materialize-') as raw:
        rt,entry,sid=setup(raw);original=rt.load_supplier_shipment(sid)['header']['invoice_document_id']
        other=rt.load_supplier_shipment(sid)
        rt.save_supplier_shipment(header={**other['header'],'shipment_id':'other-unmaterialized','invoice_document_id':''},lines=[{**line,'line_id':'other-'+line['line_id']} for line in other['lines']])
        with sqlite3.connect(rt.db_path) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_supplier_shipments SET invoice_document_id='' WHERE shipment_id=?",(sid,))
            conn.execute("DELETE FROM sheet_vitrina_v1_trade_documents WHERE document_id=?",(original,));conn.commit()
        # No global migration or parser, just the exact retained invoice SHA/order binding.
        with patch.object(entry.supplier_shipments_block,'migrate_existing_supplier_shipments_into_trade_documents',side_effect=AssertionError('global materialization')),patch.object(entry.supplier_shipments_block,'get_shipment',side_effect=AssertionError('heavy getter')):
            saved=upload_contract(entry,sid,'contract-native-materialize')
        materialized=saved['acceptance']['source_ref']['invoice_document_id']
        assert saved['status']=='accepted' and materialized!=original and rt.load_trade_document(materialized)['source_shipment_id']==sid
        assert not rt.load_supplier_shipment('other-unmaterialized')['header']['invoice_document_id']
        with closing(receipts.source.readonly(rt.db_path)) as conn:assert receipts._stage(conn,saved['acceptance']['operation_id'],'invoice_source')
        finish(rt,sid)
    with TemporaryDirectory(prefix='operator-contract-race-') as raw:
        rt,entry,sid=setup(raw);saved=upload_contract(entry,sid,'contract-native-race');iid=saved['acceptance']['source_ref']['invoice_document_id']
        replacement=[];original=rt.save_invoice_contract_link
        def race(*args,**kwargs):
            if not replacement:
                replacement.append(entry.handle_supplier_shipments_contract_patch_request(sid,{'request_id':'contract-native-race-newer','contract_document_id':''}))
            return original(*args,**kwargs)
        with patch.object(rt,'save_invoice_contract_link',side_effect=race):
            result=intents.drain_supplier_preparation_intents(rt,shipment_ids=[sid])
        assert result['status']=='pending' and not rt.load_invoice_contract_link(iid)
        assert read(rt,sid,'contract-native-race')['acceptance']['processing']['reason_code']=='superseded'
        assert native(rt,sid)['status']=='pending' and native(rt,sid)['costs_required']==1
        finish(rt,sid);assert read(rt,sid,'contract-native-race-newer')['acceptance']['processing']['complete']
    with TemporaryDirectory(prefix='operator-contract-carried-') as raw:
        rt,entry,sid=setup(raw);saved=upload_contract(entry,sid,'contract-native-carried');accepted_revision=saved['acceptance']['processing']['native_intent_revision']
        initial=native(rt,sid)
        rt.save_supplier_financial_document(document={'document_id':'contract-carried-expense','supplier_order_id':sid,'document_type':'logistics_invoice',
            'uploaded_at':'2026-10-08T10:00:00Z','updated_at':'2026-10-08T10:00:00Z','document_date':'2026-04-01','parse_status':'confirmed','total_amount_rub':200},
            expense_lines=[{'line_id':'contract-carried-line','amount':200,'amount_rub':200,'currency':'RUB','category':'logistics','status':'confirmed'}])
        carried=native(rt,sid)
        assert carried['revision']>accepted_revision and carried['costs_required']==1 and carried['effective_date']=='2026-04-01'
        assert json.loads(carried['post_actions_json'])['invoice_contract'][receipts.CORRELATION]==saved['acceptance']['operation_id']
        assert carried['affected_nm_ids_json']==initial['affected_nm_ids_json']
        finish(rt,sid);applied=read(rt,sid,'contract-native-carried')['acceptance']
        assert applied['processing']['native_intent_revision']==accepted_revision and applied['processing']['applied_revision']==carried['revision']
        assert applied['processing']['complete'] and applied['processing']['cost_applicable'] is False
        # Queue remains awaiting cost publication; link completion cannot certify it.
        with closing(receipts.source.readonly(rt.db_path)) as conn:
            assert conn.execute("SELECT status FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue WHERE queue_id=?",(native(rt,sid)['queue_id'],)).fetchone()[0]=='queued'
    with TemporaryDirectory(prefix='operator-contract-source-cas-') as raw:
        rt,entry,sid=setup(raw);saved=upload_contract(entry,sid,'contract-native-contract-cas');cid=saved['acceptance']['source_ref']['contract_document_id'];iid=saved['acceptance']['source_ref']['invoice_document_id']
        entry.handle_trade_documents_patch_request(cid,{'request_id':'contract-native-metadata-newer','number':'updated-after-intent'})
        result=intents.drain_supplier_preparation_intents(rt,shipment_ids=[sid]);assert result['status']=='pending' and not rt.load_invoice_contract_link(iid)
        pending=read(rt,sid,'contract-native-contract-cas')['acceptance'];assert pending['durable_saved'] and pending['state']=='needs_attention' and not pending['processing']['complete']
    with TemporaryDirectory(prefix='operator-contract-applied-atomic-') as raw:
        rt,entry,sid=setup(raw);saved=upload_contract(entry,sid,'contract-native-applied-atomic');iid=saved['acceptance']['source_ref']['invoice_document_id']
        with patch.object(receipts,'record_link_applied',side_effect=RuntimeError('physical-receipt-interrupted')):
            assert intents.drain_supplier_preparation_intents(rt,shipment_ids=[sid])['status']=='pending'
        assert not rt.load_invoice_contract_link(iid) and not read(rt,sid,'contract-native-applied-atomic')['acceptance']['processing']['complete']
        finish(rt,sid);assert read(rt,sid,'contract-native-applied-atomic')['acceptance']['processing']['complete']
    with TemporaryDirectory(prefix='operator-contract-duplicate-cas-') as raw:
        rt,entry,sid=setup(raw);saved=upload_contract(entry,sid,'contract-native-duplicate-first');cid=saved['acceptance']['source_ref']['contract_document_id']
        original=rt.find_settings_trade_document_duplicate
        def change_duplicate(**kwargs):
            captured=original(**kwargs)
            with sqlite3.connect(rt.db_path) as conn:conn.execute("UPDATE sheet_vitrina_v1_trade_documents SET number='concurrent-native-metadata' WHERE document_id=?",(cid,));conn.commit()
            return captured
        with patch.object(rt,'find_settings_trade_document_duplicate',side_effect=change_duplicate):
            refused=upload_contract(entry,sid,'contract-native-duplicate-raced')
        assert refused['status']=='rejected' and rt.load_trade_document(cid)['number']=='concurrent-native-metadata'
    with TemporaryDirectory(prefix='operator-contract-foundation-atomic-') as raw:
        rt,entry,sid=setup(raw);iid=rt.load_supplier_shipment(sid)['header']['invoice_document_id']
        with sqlite3.connect(rt.db_path) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_supplier_shipments SET invoice_document_id='' WHERE shipment_id=?",(sid,))
            conn.execute("DELETE FROM sheet_vitrina_v1_trade_documents WHERE document_id=?",(iid,));conn.commit()
        with patch.object(receipts,'_save_stage',side_effect=RuntimeError('invoice-receipt-interrupted')):
            try:upload_contract(entry,sid,'contract-native-foundation-atomic')
            except RuntimeError:pass
            else:raise AssertionError('foundation interruption was swallowed')
        assert not rt.load_supplier_shipment(sid)['header']['invoice_document_id'] and not rt.list_trade_documents()
        assert read(rt,sid,'contract-native-foundation-atomic')['status']=='unknown'
    print('Contract actual native source+post-action atomicity, SHA adoption, exact RO restart, no false completion, own link/unlink, invoice materialization and worker/source CAS race: OK')


if __name__=='__main__':main()
