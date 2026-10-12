"""Synthetic native writer receipts; no external provider call or source writes by journal."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import sqlite3
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from packages.application.change_registry import ChangeRegistryRepository,canonical_digest
from packages.application.change_registry_writer import InternalWriterRegistry
from packages.application.change_registry_observer import ChangeRegistryReadSurface
from packages.application import operator_operations as journal
from packages.adapters import registry_upload_http_entrypoint as http

STAMP='2026-10-08T12:00:00Z'
BEFORE=dict(original_price_minor=10000,discount_bps=1000,seller_price_minor=9000)
AFTER=dict(original_price_minor=12000,discount_bps=1000,seller_price_minor=10800)

class ExternalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory(prefix='operator-external-native-');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.repo=ChangeRegistryRepository(self.root);self.repo.initialize_schema()
        self.db=self.root/'registry_upload_runtime.sqlite3'
        self.writer=InternalWriterRegistry(runtime_dir=self.root,seller_id='seller',account_scope='seller-portal-primary',timestamp_factory=lambda:STAMP)
        self.scope=ChangeRegistryReadSurface(self.root,seller_id='seller')

    def price(self,surface='prices_upload',identity='one',writer=None):
        return (writer or self.writer).prepare_price(source_surface=surface,actor='alice',native_operation_id=identity,nm_id=101,
            before=BEFORE,requested=AFTER,explicit_fields=('original_price_minor',),requested_at=STAMP)

    def read(self,op,allowed={'wb_prices'},**kw):
        return journal.read_acceptance(self.db,op.operation_id,allowed_domains=allowed,external_scope=self.scope,**kw)

    def test_delete_journal_pending_writer_does_not_deadlock_nested_receipt_reads(self):
        import threading,time
        from unittest.mock import patch
        from packages.application.operator_external_operations import read_native
        from packages.application.sqlite_contention import connect_sqlite
        op=self.price()
        with closing(sqlite3.connect(self.db)) as setup, setup:
            self.assertEqual(setup.execute('PRAGMA journal_mode=DELETE').fetchone()[0],'delete')
            setup.execute('CREATE TABLE journal_writer_probe(id INTEGER PRIMARY KEY,value INTEGER NOT NULL)')
            setup.execute('INSERT INTO journal_writer_probe VALUES(1,0)')
        original=ChangeRegistryRepository.read_operation
        readers=(
            lambda:journal.journal(self.db,allowed_domains={'wb_prices'},external_scope=self.scope)['items'][0],
            lambda:self.read(op),
            lambda:read_native(self.db,domain='wb_prices',native_id='one',allowed_domains={'wb_prices'},scope=self.scope),
        )
        for index,reader in enumerate(readers,start=1):
            with self.subTest(reader=index):
                started=threading.Event();outcomes=[];threads=[]
                def write():
                    conn=connect_sqlite(self.db,timeout_ms=1500)
                    try:
                        conn.execute('UPDATE journal_writer_probe SET value=value+1 WHERE id=1')
                        started.set()
                        conn.commit();outcomes.append('committed')
                    except BaseException as exc:
                        outcomes.append(type(exc).__name__);conn.rollback()
                    finally:conn.close()
                def read_after_writer_pending(repository,identity,**kwargs):
                    worker=threading.Thread(target=write);threads.append(worker);worker.start()
                    self.assertTrue(started.wait(2))
                    deadline=time.monotonic()+1;pending=False
                    while time.monotonic()<deadline:
                        with closing(sqlite3.connect(self.db.as_uri()+'?mode=ro',uri=True,timeout=0)) as probe:
                            probe.execute('PRAGMA query_only=ON')
                            try:probe.execute('SELECT value FROM journal_writer_probe').fetchone()
                            except sqlite3.OperationalError as exc:
                                if 'locked' not in str(exc):raise
                                pending=True;break
                        time.sleep(.005)
                    self.assertTrue(pending,'writer never reached pending COMMIT')
                    return original(repository,identity,**kwargs)
                try:
                    with patch.object(ChangeRegistryRepository,'read_operation',read_after_writer_pending):
                        value=reader()
                    self.assertEqual(value['operation_id'],op.operation_id)
                    self.assertEqual(value['state'],'accepted')
                finally:
                    for worker in threads:worker.join(3)
                self.assertEqual(outcomes,['committed'])
                with closing(sqlite3.connect(self.db.as_uri()+'?mode=ro',uri=True)) as check:
                    check.execute('PRAGMA query_only=ON')
                    self.assertEqual(check.execute('SELECT value FROM journal_writer_probe').fetchone()[0],index)

    def test_supplier_journal_families_reuse_outer_reader_with_pending_commit(self):
        import threading, time
        from contextlib import contextmanager
        from unittest.mock import patch
        from apps.operator_supplier_financial_native_smoke import setup, document, execute
        from apps.operator_supplier_factual_dates_smoke import fixture, SHIPMENT_ID
        from packages.application import operator_supplier_shipments as source
        from packages.application import operator_supplier_financial as financial
        from packages.application import operator_cny_documents as cny
        from packages.application import operator_supplier_factual_dates as facts
        from packages.application.sqlite_contention import connect_sqlite

        def check(runtime, identity, domain):
            db=runtime.db_path
            # Legacy fixture constructors leave context-managed connections for GC.
            # Close those before switching the disposable database to production DELETE.
            import gc
            gc.collect()
            with closing(sqlite3.connect(db)) as setup_conn, setup_conn:
                self.assertEqual(setup_conn.execute('PRAGMA journal_mode=DELETE').fetchone()[0], 'delete')
                setup_conn.execute('CREATE TABLE IF NOT EXISTS journal_writer_probe(value INTEGER NOT NULL)')
                setup_conn.execute('DELETE FROM journal_writer_probe')
                setup_conn.execute('INSERT INTO journal_writer_probe VALUES(0)')
            args=dict(allowed_domains={domain}, request_scope='alice-key', runtime_dir=runtime.runtime_dir)
            readers=(lambda: journal.journal(db, **args)['items'][0],
                     lambda: journal.read_acceptance(db, identity, **args))
            original=source.read_session
            for index, reader in enumerate(readers, 1):
                started=threading.Event(); outcomes=[]; workers=[]; borrowed=[]
                def write():
                    conn=connect_sqlite(db,timeout_ms=1500)
                    try:
                        conn.execute('UPDATE journal_writer_probe SET value=value+1')
                        started.set();conn.commit();outcomes.append('committed')
                    except BaseException as exc:
                        outcomes.append(type(exc).__name__);conn.rollback()
                    finally:conn.close()
                @contextmanager
                def observe(path, connection=None):
                    self.assertIsNotNone(connection, 'journal opens a second source reader')
                    borrowed.append(connection)
                    if not workers:
                        worker=threading.Thread(target=write);workers.append(worker);worker.start()
                        self.assertTrue(started.wait(2))
                        deadline=time.monotonic()+1; pending=False
                        while time.monotonic()<deadline:
                            with closing(sqlite3.connect(db.as_uri()+'?mode=ro',uri=True,timeout=0)) as probe:
                                probe.execute('PRAGMA query_only=ON')
                                try:probe.execute('SELECT value FROM journal_writer_probe').fetchone()
                                except sqlite3.OperationalError as exc:
                                    if 'locked' not in str(exc):raise
                                    pending=True;break
                            time.sleep(.005)
                        self.assertTrue(pending, 'writer never reached pending COMMIT')
                    with original(path, connection) as conn:
                        yield conn
                    self.assertTrue(connection.in_transaction, 'borrower reset the journal snapshot')
                try:
                    with patch.object(source,'read_session',observe):
                        receipt=reader()
                    self.assertEqual(receipt['operation_id'],identity)
                    self.assertTrue(borrowed)
                    self.assertTrue(all(conn is borrowed[0] for conn in borrowed))
                finally:
                    for worker in workers:worker.join(3)
                self.assertEqual(outcomes,['committed'])
                with closing(sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)) as readback:
                    readback.execute('PRAGMA query_only=ON')
                    self.assertEqual(readback.execute('SELECT value FROM journal_writer_probe').fetchone()[0],index)

        with TemporaryDirectory() as raw:
            runtime,ledger=setup(raw)
            result=execute(runtime,'pending-financial-reader',
                [dict(child_key='one',kind='financial',subject_id='one')],lambda child:document(runtime,child))
            check(runtime,result['acceptance']['operation_id'],financial.DOMAIN)
            payload=dict(request_id='pending-cny-reader',operation_date='2026-07-24',cny_amount='100',rub_value='1400')
            result=cny.execute(ledger,action='cny_opening',payload=payload,request_scope='alice-key',actor='alice',
                native_write=lambda:ledger.create_opening_balance(payload))
            check(runtime,result['acceptance']['operation_id'],cny.DOMAIN)
        with TemporaryDirectory() as raw:
            runtime,entry=fixture(raw)
            preview=entry.handle_supplier_factual_dates_preview_request(SHIPMENT_ID,{'actual_shipment_date':'2026-06-26'})
            result=entry.handle_supplier_factual_dates_confirm_request(SHIPMENT_ID,
                dict(request_id='pending-factual-reader',confirmation_token=preview['confirmation_token']),
                actor='alice',request_scope='alice-key')
            check(runtime,result['acceptance']['operation_id'],facts.DOMAIN)

    def test_borrowed_financial_view_never_infers_missing_outcome_is_not_saved(self):
        from apps.operator_supplier_financial_native_smoke import setup, document, execute
        from packages.application import operator_supplier_financial as financial
        from packages.application import operator_supplier_shipments as source
        with TemporaryDirectory() as raw:
            runtime,_=setup(raw)
            def write(child):
                if child['child_key']=='two':raise RuntimeError('lost before outcome')
                return document(runtime,child)
            result=execute(runtime,'partial-pinned-reader',[dict(child_key=k,kind='financial',subject_id=k) for k in ('one','two')],write)
            self.assertTrue(result['settled'])
            with source.read_session(runtime.db_path) as conn:
                retained=financial.read_operation(runtime.runtime_dir,runtime.db_path,result['acceptance']['operation_id'],
                    request_scope='alice-key',connection=conn)
                self.assertEqual(retained['results'][1]['status'],'processing')
                self.assertFalse(retained['settled'])
                self.assertTrue(conn.in_transaction)
            latest=financial.read_request(runtime.runtime_dir,runtime.db_path,'partial-pinned-reader',request_scope='alice-key')
            self.assertEqual(latest['results'][1]['status'],'not_saved')
            self.assertTrue(latest['settled'])

    def test_borrowed_receipt_connection_requires_exact_query_only_transaction(self):
        from packages.application.change_registry import ChangeRegistryError
        op=self.price()
        with closing(sqlite3.connect(self.db.as_uri()+'?mode=ro',uri=True)) as conn:
            conn.row_factory=sqlite3.Row
            conn.execute('PRAGMA query_only=ON')
            with self.assertRaises(ChangeRegistryError):self.repo.read_operation(op.operation_id,connection=conn)
            conn.execute('BEGIN')
            conn.execute('SELECT 1 FROM sqlite_master').fetchone()
            value=self.repo.read_operation(op.operation_id,connection=conn)
            self.assertEqual(value['operation']['operation_id'],op.operation_id)
            self.assertTrue(conn.in_transaction)
            self.assertEqual(conn.execute('PRAGMA query_only').fetchone()[0],1)
            conn.rollback()
        with closing(sqlite3.connect(self.db)) as writable:
            writable.execute('BEGIN')
            with self.assertRaises(ChangeRegistryError):self.repo.read_operation(op.operation_id,connection=writable)
            self.assertTrue(writable.in_transaction)
        other=self.root/'foreign.sqlite3'
        with closing(sqlite3.connect(other)) as setup:setup.execute('CREATE TABLE marker(id INTEGER)')
        with closing(sqlite3.connect(other.as_uri()+'?mode=ro',uri=True)) as foreign:
            foreign.row_factory=sqlite3.Row;foreign.execute('PRAGMA query_only=ON');foreign.execute('BEGIN')
            with self.assertRaises(ChangeRegistryError):self.repo.read_operation(op.operation_id,connection=foreign)
            self.assertTrue(foreign.in_transaction)

    def test_created_submitted_unknown_and_confirmed_are_distinct(self):
        op=self.price();value=self.read(op);self.assertEqual(value['state'],'accepted');self.assertFalse(value['external_confirmed'])
        self.writer.submitted(op,receipt_reference='native-upload:1',receipt_basis={'id':1})
        self.assertEqual(self.read(op)['state'],'processing')
        self.writer.ambiguous(op,error_code='transport_unknown',error_message='Unknown response')
        self.assertEqual(self.read(op)['state'],'needs_attention')
        self.writer.confirm_price(op,confirmed=AFTER,readback_basis={'exact_tuple':AFTER},receipt_reference='native-upload:1')
        value=self.read(op);self.assertEqual(value['state'],'completed');self.assertTrue(value['external_confirmed'])
        self.assertEqual(value['confirmed_count'],3);self.assertTrue(all(c['readback_digest'] for c in value['children']))
        self.assertFalse(value['resubmit_allowed'])

    def test_scope_surface_permissions_precede_counts_search_page_and_detail(self):
        op=self.price();hidden=self.price('sku_management_price','hidden')
        other=InternalWriterRegistry(runtime_dir=self.root,seller_id='foreign-seller',account_scope='seller-portal-primary',timestamp_factory=lambda:STAMP)
        foreign=self.price(identity='foreign',writer=other)
        page=journal.journal(self.db,allowed_domains={'wb_prices'},external_scope=self.scope,search='hidden',limit=1)
        self.assertEqual(page['total'],0)
        page=journal.journal(self.db,allowed_domains={'wb_prices'},external_scope=self.scope,limit=1)
        self.assertEqual(page['total'],1);self.assertEqual(page['items'][0]['operation_id'],op.operation_id);self.assertFalse(page['has_more'])
        self.assertIsNone(self.read(hidden));self.assertIsNone(self.read(foreign));self.assertIsNone(self.read(op,allowed=set()))
        self.assertEqual(journal.journal(self.db,allowed_domains={'wb_prices'})['total'],0)
        self.assertEqual(journal.journal(self.db,allowed_domains={'wb_prices'},external_scope=self.scope,page=2,limit=1)['items'],[])

    def test_partial_and_resolved_require_each_exact_item_readback(self):
        op=self.price();self.writer.submitted(op,receipt_reference='native:partial',receipt_basis={'id':'partial'})
        keys=list(op.change_item_ids.values())
        self.repo.append_writer_operation_state(operation_id=op.operation_id,state='confirmed',occurred_at=STAMP,
            change_item_ids=keys[:1],readback_proof_kind='wb_readback',readback_digest=canonical_digest({'item':keys[0]}))
        value=self.read(op);self.assertTrue(value['partial']);self.assertEqual(value['state'],'processing');self.assertEqual(value['confirmed_count'],1)
        self.repo.append_writer_operation_state(operation_id=op.operation_id,state='ambiguous',occurred_at=STAMP,change_item_ids=keys[1:])
        self.assertEqual(self.read(op)['state'],'needs_attention')
        self.repo.append_writer_operation_state(operation_id=op.operation_id,state='resolved',resolution_state='confirmed',occurred_at=STAMP,
            change_item_ids=keys[1:],readback_proof_kind='wb_readback',readback_digest=canonical_digest({'items':keys[1:]}))
        self.assertEqual(self.read(op)['state'],'completed')

    def test_confirmed_without_native_proof_is_not_external_completed(self):
        op=self.price();self.writer.submitted(op,receipt_reference='native:unproven',receipt_basis={'id':2})
        self.repo.append_writer_operation_state(operation_id=op.operation_id,state='confirmed',occurred_at=STAMP)
        self.assertEqual(self.read(op)['state'],'needs_attention');self.assertFalse(self.read(op)['external_confirmed'])

    def test_native_read_does_not_write_or_disclose_raw_annotation_paths(self):
        op=self.price();before=self.db.read_bytes()
        value=self.read(op);journal.journal(self.db,allowed_domains={'wb_prices'},external_scope=self.scope,search='alice')
        self.assertEqual(self.db.read_bytes(),before)
        self.assertNotIn('annotations',value);self.assertNotIn('source_file_path',str(value))
        self.assertEqual(value['source_ref']['account_scope'],'seller-portal-primary')

    def test_exact_native_alias_never_rebinds_to_other_surface_or_account(self):
        from packages.application.operator_external_operations import read_native,decorate
        op=self.price();receipt=read_native(self.db,domain='wb_prices',native_id='one',allowed_domains={'wb_prices'},scope=self.scope)
        self.assertEqual(receipt['operation_id'],op.operation_id)
        self.assertIsNone(read_native(self.db,domain='wb_ads',native_id='one',allowed_domains={'wb_ads'},scope=self.scope))
        self.assertIsNone(read_native(self.db,domain='wb_prices',native_id='one',allowed_domains=set(),scope=self.scope))
        value=decorate({'registry_operation_id':op.operation_id,'status':'submitted'},db_path=self.db,scope=self.scope,domain='wb_prices')
        self.assertEqual(value['acceptance']['operation_id'],op.operation_id)
        value=decorate({'status':'success'},db_path=self.db,scope=self.scope,domain='wb_prices')
        self.assertNotIn('acceptance',value);self.assertEqual(value['operator_projection']['status'],'not_tracked')

    def test_actual_http_alias_and_native_source_grants(self):
        import json,threading
        from unittest.mock import patch
        from urllib.request import urlopen
        from urllib.error import HTTPError
        from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
        from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig
        op=self.price()
        entry=RegistryUploadHttpEntrypoint(runtime_dir=self.root,change_registry_read_surface=self.scope)
        config=RegistryUploadHttpEntrypointConfig(host='127.0.0.1',port=0,upload_path=http.DEFAULT_UPLOAD_PATH,
            sheet_plan_path=http.DEFAULT_SHEET_PLAN_PATH,sheet_refresh_path=http.DEFAULT_SHEET_REFRESH_PATH,
            sheet_status_path=http.DEFAULT_SHEET_STATUS_PATH,sheet_operator_ui_path=http.DEFAULT_SHEET_OPERATOR_UI_PATH,runtime_dir=self.root)
        server=http.build_registry_upload_http_server(config,entrypoint=entry);worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        def get(path):
            try:
                with urlopen('http://127.0.0.1:'+str(server.server_address[1])+path) as response:return response.status,json.load(response)
            except HTTPError as exc:return exc.code,json.load(exc)
        try:
            with patch.object(http,'_web_auth_config',return_value={'enabled':True,'configured':True}),patch.object(http,'_authenticated_web_user') as auth:
                auth.return_value={'username':'alice','role':'operator','allowed_sections':['prices']}
                path='/v1/sheet-vitrina-v1/operations/external?domain=wb_prices&native_id=one'
                code,value=get(path);self.assertEqual(code,200);self.assertEqual(value['operation']['operation_id'],op.operation_id)
                self.assertEqual(get(path+'&native_id=other')[0],422)
                auth.return_value={'username':'bob','role':'operator','allowed_sections':['ads']}
                self.assertEqual(get(path)[0],404)
                self.assertEqual(get('/v1/sheet-vitrina-v1/operations?domain=wb_prices')[1]['total'],0)
                self.assertEqual(get('/v1/sheet-vitrina-v1/operations/'+op.operation_id)[0],404)
        finally:server.shutdown();worker.join(5);server.server_close()

    def test_original_journal_grants_and_sku_boundary_are_preserved(self):
        user={'role':'operator','username':'alice','allowed_sections':['settings']}
        allowed=http._operator_domains_for_user(user)
        self.assertIn('nomenclature',allowed);self.assertNotIn('wb_prices',allowed);self.assertNotIn('keyword_cleaner',allowed)
        user['allowed_sections']=['prices'];allowed=http._operator_domains_for_user(user)
        self.assertTrue({'wb_prices','spp_test'}<=allowed);self.assertNotIn('wb_ads',allowed);self.assertNotIn('ff_pool_document',allowed)
        user['allowed_sections']=['sku_management'];allowed=http._operator_domains_for_user(user)
        self.assertTrue({'sku_prices','sku_ads','inventory_balance'}<=allowed)
        user['allowed_sections']=['supply'];self.assertTrue({'ff_pool_document','factory_order_dataset','fulfillment_services'}<=http._operator_domains_for_user(user))

if __name__=='__main__':unittest.main()
