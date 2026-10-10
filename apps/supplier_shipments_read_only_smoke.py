"""TEMP actual supplier GETs and exact-target legacy invoice write ownership."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from apps.operator_supplier_contracts_smoke import setup,contract_bytes
from apps.operator_supplier_shipments_http_smoke import server_for,stop,request,PATH
from packages.application import operator_supplier_contracts as contracts
from packages.application import operator_supplier_shipments as source
from packages.application.web_vitrina_window_read_context import window_read_context
from packages.adapters import registry_upload_http_entrypoint as http


def snapshot(rt):
    with closing(source.readonly(rt.db_path)) as conn:
        schema=[tuple(row) for row in conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]
        tables=[row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        rows={name:sorted((tuple(row) for row in conn.execute('SELECT * FROM "'+name+'"')),key=repr) for name in tables}
        return schema,rows


def legacy(rt, original, sid):
    saved=rt.load_supplier_shipment(original)
    header={**saved['header'],'shipment_id':sid,'invoice_document_id':''}
    lines=[{**line,'line_id':sid+'-'+line['line_id']} for line in saved['lines']]
    rt.save_supplier_shipment(header=header,lines=lines)


def fixture(raw):
    rt,entry,modern=setup(raw)
    block=entry.supplier_shipments_block
    contract=block.create_trade_document_from_upload(document_type='contract',file_bytes=contract_bytes(),
        uploaded_filename='contract.xlsx',number='READ-ONLY-CONTRACT',document_date='2026-10-10')['document']
    legacy(rt,modern,'legacy-read-target');legacy(rt,modern,'legacy-unrelated')
    detail=rt.load_supplier_shipment('legacy-read-target')
    rt.save_supplier_shipment(header={**detail['header'],'contract_no':contract['number'],
        'contract_date':contract['document_date']},lines=detail['lines'])
    return rt,entry,modern,contract['document_id']


class ReadOnlyTests(unittest.TestCase):
    def test_actual_gets_are_query_only_and_keep_mixed_library_sources(self):
        with TemporaryDirectory(prefix='supplier-get-ro-') as raw:
            rt,entry,modern,cid=fixture(raw);block=entry.supplier_shipments_block
            with sqlite3.connect(rt.db_path) as conn:
                conn.execute('DELETE FROM sheet_vitrina_v1_sku_groups');conn.commit()
            saved_group=rt.save_sku_group({'group_key':'clean','label':'Saved custom clean','aliases':['saved-alias'],
                'is_active':False,'is_system':False,'display_order':20,
                'created_at':'2026-10-10T00:00:00Z','updated_at':'2026-10-10T00:00:00Z'})
            server,thread,base=server_for(entry)
            before=snapshot(rt);locks=set(rt.runtime_dir.glob('*.lock'));denied=[];opened=[]
            native_connect=sqlite3.connect
            forbidden={getattr(sqlite3,name) for name in ('SQLITE_INSERT','SQLITE_UPDATE','SQLITE_DELETE',
                'SQLITE_CREATE_TABLE','SQLITE_CREATE_INDEX','SQLITE_CREATE_TRIGGER','SQLITE_CREATE_VIEW',
                'SQLITE_DROP_TABLE','SQLITE_DROP_INDEX','SQLITE_DROP_TRIGGER','SQLITE_DROP_VIEW','SQLITE_ALTER_TABLE')}
            def connect(*args,**kwargs):
                conn=native_connect(*args,**kwargs)
                # connect_sqlite initializes its observed subclass after this
                # constructor returns; use the base API for this fixture fence.
                sqlite3.Connection.execute(conn,'PRAGMA query_only=ON')
                def authorize(action,arg1,arg2,db,trigger):
                    if action in forbidden:
                        denied.append((action,arg1));return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                conn.set_authorizer(authorize)
                opened.append(sqlite3.Connection.execute(conn,'PRAGMA query_only').fetchone()[0])
                return conn
            try:
                with patch.object(sqlite3,'connect',side_effect=connect), \
                    patch.object(block,'migrate_existing_supplier_shipments_into_trade_documents',side_effect=AssertionError('GET migration')), \
                    patch.object(block,'_ensure_sku_groups_ready',side_effect=AssertionError('GET group bootstrap')):
                    for _ in range(2):
                        code,listing=request(base,PATH);self.assertEqual(code,200,{'result':listing,'denied':denied})
                        self.assertEqual(len(listing['shipments']),3)
                        code,detail=request(base,PATH+'/legacy-read-target');self.assertEqual(code,200,detail)
                        self.assertFalse(detail['invoice_document_id'])
                        self.assertEqual(detail['contract_link_status'],'single_candidate')
                        self.assertTrue(detail['invoice_download_path'])
                        self.assertTrue(detail['lines'])
                        registry=next(row for row in listing['shipments'] if row['shipment_id']=='legacy-read-target')
                        for key in ('exact_cost_status','exact_landed_cost_total_rub','exact_landed_cost_per_unit_rub',
                                    'exact_currency_payment_cost_rub','exact_bank_fees_rub'):
                            self.assertEqual(registry[key],detail[key],key)
                        code,documents=request(base,http.DEFAULT_TRADE_DOCUMENTS_PATH)
                        self.assertEqual(code,200,documents)
                        self.assertEqual(len(documents['documents']),2)
                        self.assertTrue(any(doc['document_id']==cid for doc in documents['documents']))
                        code,groups=request(base,http.DEFAULT_SKU_GROUPS_PATH)
                        self.assertEqual(code,200,groups)
                        self.assertEqual(next(group for group in groups['groups'] if group['group_key']=='clean'),saved_group)
                        projected=[group for group in groups['groups'] if group['group_key']!='clean']
                        self.assertTrue(all(group['is_active'] and group['is_system'] for group in projected))
                        self.assertTrue(all('operator_source_revision' not in group for group in projected))
                        self.assertEqual(groups['groups'][-1]['group_key'],'clean')
                        self.assertFalse(any(group['group_key']=='clean' for group in block.list_sku_groups(include_inactive=False)['groups']))
                        from urllib.request import urlopen
                        with urlopen(base+PATH+'/legacy-read-target/invoice',timeout=10) as response:
                            self.assertEqual(response.status,200)
                            expected=(rt.runtime_dir/rt.load_supplier_shipment(modern)['header']['source_file_path']).read_bytes()
                            self.assertEqual(response.read(),expected)
                        with urlopen(base+http.DEFAULT_TRADE_DOCUMENTS_PATH+'/'+cid+'/file',timeout=10) as response:
                            self.assertEqual(response.status,200)
                            self.assertEqual(response.read(),contract_bytes())
                    self.assertEqual(request(base,PATH+'/missing-order')[0],404)
                self.assertTrue(opened);self.assertEqual(set(opened),{1});self.assertFalse(denied)
                self.assertEqual(snapshot(rt),before)
                self.assertEqual(set(rt.runtime_dir.glob('*.lock')),locks)
                with native_connect(rt.db_path) as conn:
                    conn.execute('DROP TABLE sheet_vitrina_v1_warehouse_functional_active');conn.commit()
                incomplete=snapshot(rt)
                code,error=request(base,PATH)
                self.assertEqual(code,500,error)
                self.assertIn('schema is incomplete',error['error'])
                self.assertEqual(snapshot(rt),incomplete,'GET repaired an incomplete source schema')
            finally:stop(server,thread)

    def test_actual_legacy_contract_mutation_materializes_only_exact_order_and_recovers(self):
        with TemporaryDirectory(prefix='supplier-target-write-') as raw:
            rt,entry,modern,cid=fixture(raw);block=entry.supplier_shipments_block
            unrelated=rt.load_supplier_shipment('legacy-unrelated');modern_id=rt.load_supplier_shipment(modern)['header']['invoice_document_id']
            server,thread,base=server_for(entry)
            try:
                with patch.object(block,'migrate_existing_supplier_shipments_into_trade_documents',side_effect=AssertionError('global mutation')):
                    code,result=request(base,PATH+'/legacy-read-target/contract','PATCH',{'contract_document_id':cid})
                self.assertEqual(code,200,result)
                iid=result['shipment']['invoice_document_id']
                self.assertEqual(result['shipment']['contract_document_id'],cid)
                self.assertEqual(rt.load_trade_document(iid)['source_shipment_id'],'legacy-read-target')
                self.assertEqual(rt.load_supplier_shipment('legacy-unrelated'),unrelated)
                self.assertEqual(rt.load_supplier_shipment(modern)['header']['invoice_document_id'],modern_id)
                receipt=contracts.materialize_invoice_for_legacy_mutation(block,'legacy-read-target')
                self.assertTrue(receipt['operation_id'].startswith('supplier_invoice_source_'))
                self.assertEqual(receipt['invoice_document_id'],iid)
                before=snapshot(rt)
                self.assertEqual(contracts.materialize_invoice_for_legacy_mutation(block,'legacy-read-target'),receipt)
                self.assertEqual(snapshot(rt),before)
                with closing(source.readonly(rt.db_path)) as conn:
                    proof=contracts._stage(conn,receipt['operation_id'],'invoice_source')
                    self.assertFalse(proof['before']['header']['invoice_document_id'])
                    self.assertEqual(proof['after']['header']['invoice_document_id'],iid)
                    self.assertEqual(source.digest(proof),receipt['source_digest'])
                with sqlite3.connect(rt.db_path) as conn:
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute('DELETE FROM '+contracts.STAGES+' WHERE operation_id=?',(receipt['operation_id'],))
            finally:stop(server,thread)

    def test_projected_default_has_no_fabricated_version_and_explicit_edit_keeps_native_receipt(self):
        with TemporaryDirectory(prefix='supplier-default-edit-') as raw:
            rt,entry,modern,cid=fixture(raw)
            with sqlite3.connect(rt.db_path) as conn:
                conn.execute('DELETE FROM sheet_vitrina_v1_sku_groups');conn.commit()
            server,thread,base=server_for(entry)
            try:
                code,result=request(base,http.DEFAULT_SKU_GROUPS_PATH)
                self.assertEqual(code,200,result)
                projected=next(group for group in result['groups'] if group['group_key']=='extra')
                self.assertNotIn('operator_source_revision',projected)
                payload={'label':'Explicit saved extra','_operator_request_id':'opsku_'+'a'*32}
                code,saved=request(base,http.DEFAULT_SKU_GROUPS_PATH+'/extra','PATCH',payload)
                self.assertEqual(code,200,saved)
                self.assertEqual(saved['group']['label'],'Explicit saved extra')
                self.assertEqual(saved['acceptance']['operation_id'],payload['_operator_request_id'])
                self.assertTrue(saved['acceptance']['durable_saved'])
                before=snapshot(rt)
                code,recovered=request(base,http.DEFAULT_SKU_GROUPS_PATH+'/extra','PATCH',payload)
                self.assertEqual(code,200,recovered)
                self.assertEqual(recovered['acceptance'],saved['acceptance'])
                self.assertEqual(snapshot(rt),before)
            finally:stop(server,thread)

    def test_targeted_materialization_refuses_source_cas_and_keeps_unrelated_order(self):
        with TemporaryDirectory(prefix='supplier-source-cas-') as raw:
            rt,entry,modern,cid=fixture(raw);block=entry.supplier_shipments_block
            count=len(rt.list_trade_documents());unrelated=rt.load_supplier_shipment('legacy-unrelated')
            materialize=contracts._materialize_invoice
            def race(block,ctx):
                saved=rt.load_supplier_shipment(ctx['shipment_id'])
                rt.save_supplier_shipment(header={**saved['header'],'invoice_no':'concurrent-source'},lines=saved['lines'])
                return materialize(block,ctx)
            with patch.object(contracts,'_materialize_invoice',side_effect=race):
                with self.assertRaisesRegex(ValueError,'changed before'):
                    contracts.materialize_invoice_for_legacy_mutation(block,'legacy-read-target')
            self.assertEqual(len(rt.list_trade_documents()),count)
            self.assertFalse(rt.load_supplier_shipment('legacy-read-target')['header']['invoice_document_id'])
            self.assertEqual(rt.load_supplier_shipment('legacy-read-target')['header']['invoice_no'],'concurrent-source')
            self.assertEqual(rt.load_supplier_shipment('legacy-unrelated'),unrelated)

    def test_concurrent_target_materialization_returns_one_exact_native_proof(self):
        with TemporaryDirectory(prefix='supplier-concurrent-source-') as raw:
            rt,entry,modern,cid=fixture(raw);count=len(rt.list_trade_documents())
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes=list(pool.map(lambda _:contracts.materialize_invoice_for_legacy_mutation(
                    entry.supplier_shipments_block,'legacy-read-target'),range(2)))
            self.assertEqual(outcomes[0],outcomes[1])
            self.assertTrue(outcomes[0]['operation_id'])
            self.assertEqual(len(rt.list_trade_documents()),count+1)
            self.assertFalse(rt.load_supplier_shipment('legacy-unrelated')['header']['invoice_document_id'])
            with closing(source.readonly(rt.db_path)) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM '+contracts.STAGES+
                    " WHERE operation_id=? AND stage='invoice_source'",(outcomes[0]['operation_id'],)).fetchone()[0],1)

    def test_missing_or_changed_file_never_receives_synthetic_sha(self):
        for missing in (True,False):
            with self.subTest(missing=missing),TemporaryDirectory(prefix='supplier-file-proof-') as raw:
                rt,entry,modern,cid=fixture(raw);count=len(rt.list_trade_documents())
                header=rt.load_supplier_shipment('legacy-read-target')['header'];path=rt.runtime_dir/header['source_file_path']
                if missing:path.unlink()
                else:path.write_bytes(b'changed retained invoice bytes')
                with self.assertRaisesRegex(ValueError,'source file is missing|SHA changed'):
                    contracts.materialize_invoice_for_legacy_mutation(entry.supplier_shipments_block,'legacy-read-target')
                self.assertEqual(len(rt.list_trade_documents()),count)
                self.assertFalse(rt.load_supplier_shipment('legacy-read-target')['header']['invoice_document_id'])

    def test_read_snapshot_refuses_materialization_before_lock_or_write(self):
        with TemporaryDirectory(prefix='supplier-borrowed-refusal-') as raw:
            rt,entry,modern,cid=fixture(raw);before=snapshot(rt);locks=set(rt.runtime_dir.glob('*.lock'))
            with window_read_context(rt.db_path,runtime_dir=rt.runtime_dir):
                with self.assertRaisesRegex(ValueError,'read snapshot'):
                    contracts.materialize_invoice_for_legacy_mutation(entry.supplier_shipments_block,'legacy-read-target')
            self.assertEqual(snapshot(rt),before);self.assertEqual(set(rt.runtime_dir.glob('*.lock')),locks)


if __name__=='__main__':unittest.main(verbosity=2)
