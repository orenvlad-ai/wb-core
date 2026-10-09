"""Actual native library source/receipt atomicity, guards and immutable recovery."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import hashlib
import sqlite3
import sys
from openpyxl import Workbook
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.operator_supplier_shipments_smoke import seed
from packages.application import operator_trade_documents as receipts, supplier_preparation_intents as intents


def contract_bytes():
    wb=Workbook();wb.active.append(['Contract No C-1']);wb.active.append(['2026-07-24'])
    out=BytesIO();wb.save(out);wb.close();return out.getvalue()


def upload(entry, identity, kind='invoice', body=b'invoice-fixture'):
    return entry.handle_trade_documents_create_request(body,uploaded_filename='invoice.pdf' if kind=='invoice' else 'contract.xlsx',
        fields={'request_id':identity,'document_type':kind,'number':'I-1' if kind=='invoice' else 'C-1'},actor='fixture')


def doc_id(result):
    assert result['status']=='accepted',result
    return result['acceptance']['source_ref']['document_id']


def main():
    with TemporaryDirectory(prefix='operator-library-native-') as raw:
        rt,entry,_=seed(raw)
        with ThreadPoolExecutor(max_workers=3) as pool:
            concurrent=list(pool.map(lambda _:upload(entry,'library-native-invoice'),range(3)))
        assert len({r['acceptance']['operation_id'] for r in concurrent})==1
        invoice=concurrent[0];iid=doc_id(invoice)
        contract=upload(entry,'library-native-contract','contract',contract_bytes());cid=doc_id(contract)
        files=list((rt.runtime_dir/'trade_documents').rglob('*.*'));before=rt.db_path.read_bytes()
        with patch.object(receipts,'ensure_schema',side_effect=AssertionError('GET bootstrap')):
            read=receipts.read_request(rt.db_path,'library-native-invoice',request_scope='local_operator')
            assert read==invoice and receipts.read_operation(rt.db_path,invoice['acceptance']['operation_id'],request_scope='foreign') is None
        assert rt.db_path.read_bytes()==before and 'file_path' not in str(invoice) and 'cost_applicable' in str(invoice)
        with patch.object(entry.supplier_shipments_block,'create_trade_document_from_upload',side_effect=AssertionError('resubmit')):
            assert upload(entry,'library-native-invoice')==invoice
        assert files==list((rt.runtime_dir/'trade_documents').rglob('*.*'))
        duplicate=upload(entry,'library-native-duplicate');assert doc_id(duplicate)==iid and duplicate['acceptance']['source_ref']['revision']==2
        # Actual native metadata remains restricted; a refusal is retained under its exact alias.
        refused=entry.handle_trade_documents_patch_request(cid,{'request_id':'library-native-bad','amount_total':5})
        assert refused['status']=='rejected' and not refused['acceptance']
        assert receipts.read_request(rt.db_path,'library-native-bad',request_scope='local_operator')==refused
        edited=entry.handle_trade_documents_patch_request(cid,{'request_id':'library-native-edit','number':'C-new','document_date':'2026-07-25'})
        assert doc_id(edited)==cid and rt.load_trade_document(cid)['number']=='C-new'
        # A separate native writer changes metadata between captured intent and source CAS.
        original=entry.supplier_shipments_block.update_trade_document
        def concurrent_edit(document_id, operands):
            with sqlite3.connect(rt.db_path) as conn:
                conn.execute("UPDATE sheet_vitrina_v1_trade_documents SET number='concurrent-owner' WHERE document_id=?",(document_id,));conn.commit()
            return original(document_id,operands)
        with patch.object(entry.supplier_shipments_block,'update_trade_document',side_effect=concurrent_edit):
            stale=entry.handle_trade_documents_patch_request(cid,{'request_id':'library-native-cas','number':'stale-overwrite'})
        assert stale['status']=='rejected' and rt.load_trade_document(cid)['number']=='concurrent-owner'
        # The exact native file SHA remains an operand even for a metadata change.
        file=rt.runtime_dir/rt.load_trade_document(cid)['file_path'];body=file.read_bytes();file.write_bytes(b'changed-outside-source')
        failed=entry.handle_trade_documents_patch_request(cid,{'request_id':'library-native-file-cas','number':'bad-file'})
        assert failed['status']=='rejected' and rt.load_trade_document(cid)['number']=='concurrent-owner'
        file.write_bytes(body)
        linked=entry.handle_trade_documents_contract_patch_request(iid,{'request_id':'library-native-link','contract_document_id':cid})
        assert doc_id(linked)==iid and rt.load_invoice_contract_link(iid)['contract_document_id']==cid
        blocked=entry.handle_trade_documents_archive_request(cid,{'request_id':'library-native-blocked'})
        assert blocked['status']=='rejected' and rt.load_trade_document(cid)['status']=='active'
        # Receipt failure rolls back BOTH invoice archive and unlink in the actual native source transaction.
        with patch.object(receipts,'record_saved',side_effect=RuntimeError('receipt interrupted')):
            try:entry.handle_trade_documents_archive_request(iid,{'request_id':'library-native-interrupted'})
            except RuntimeError:pass
            else:raise AssertionError('interruption swallowed')
        assert rt.load_trade_document(iid)['status']=='active' and rt.load_invoice_contract_link(iid)
        assert receipts.read_request(rt.db_path,'library-native-interrupted',request_scope='local_operator')['status']=='unknown'
        archived=entry.handle_trade_documents_archive_request(iid,{'request_id':'library-native-archive'})
        assert doc_id(archived)==iid and rt.load_trade_document(iid)['status']=='archived' and not rt.load_invoice_contract_link(iid)
        assert all(p.exists() for p in files)
        with closing(receipts.readonly(rt.db_path)) as conn:
            row=conn.execute(f'SELECT * FROM {receipts.TABLE} WHERE request_id=?',('library-native-archive',)).fetchone()
            assert 'contract_document_id' in row['before_json'] and '"links":[]' in row['source_json']
        archived_contract=entry.handle_trade_documents_archive_request(cid,{'request_id':'library-native-corrected'})
        assert doc_id(archived_contract)==cid and archived_contract['acceptance']['processing']['complete']
        # Immutable historic source is recovered even after later native changes.
        assert receipts.read_request(rt.db_path,'library-native-invoice',request_scope='local_operator')==invoice
        with sqlite3.connect(rt.db_path) as conn:
            try:conn.execute(f'DELETE FROM {receipts.TABLE}')
            except sqlite3.IntegrityError:pass
            else:raise AssertionError('mutable receipt')
    with TemporaryDirectory(prefix='operator-library-native-intent-') as raw:
        rt,entry,payload=seed(raw)
        created=entry.handle_trade_documents_create_request(contract_bytes(),uploaded_filename='contract.xlsx',fields={
            'request_id':'library-native-matching','document_type':'contract','number':'CNT-2026-0513','document_date':'2026-05-13'})
        assert doc_id(created)
        supplier=entry.handle_supplier_shipments_create_request(payload)
        sid=supplier['shipment_id'];iid=rt.load_supplier_shipment(sid)['header']['invoice_document_id']
        with closing(receipts.readonly(rt.db_path)) as conn:
            old=dict(conn.execute(f'SELECT * FROM {intents.TABLE} WHERE shipment_id=?',(sid,)).fetchone())
        assert 'invoice_contract' in old['post_actions_json'] and old['status']=='pending' and old['costs_required']==1
        unlinked=entry.handle_trade_documents_contract_delete_request(iid,{'request_id':'library-native-cancel-pending'})
        assert doc_id(unlinked)==iid
        with closing(receipts.readonly(rt.db_path)) as conn:
            current=dict(conn.execute(f'SELECT * FROM {intents.TABLE} WHERE shipment_id=?',(sid,)).fetchone())
            saved=conn.execute(f'SELECT * FROM {receipts.TABLE} WHERE request_id=?',('library-native-cancel-pending',)).fetchone()
        assert current['revision']==old['revision']+1 and 'invoice_contract' not in current['post_actions_json']
        assert current['status']=='pending' and current['costs_required']==1
        assert current['affected_nm_ids_json']==old['affected_nm_ids_json'] and current['effective_date']==old['effective_date']
        assert 'invoice_contract' in saved['before_json'] and 'native_link_intents' in saved['source_json']
    print('Actual library source/receipt, restart RO exact ID, SHA duplicate, metadata refusal, link guard, atomic archive+unlink and retained history: OK')


if __name__=='__main__':main()
