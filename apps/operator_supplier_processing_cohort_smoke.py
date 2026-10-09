"""Bounded native supplier completion cohorts and durable superseded outcomes."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.operator_supplier_processing_smoke import seed, prepare_source, edit
from packages.application import operator_supplier_processing as processing, operator_supplier_shipments as source
from packages.application.registry_upload_db_backed_runtime import _connect
from packages.application.business_data_heavy_admission import heavy_admitted
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock


def main():
    with TemporaryDirectory(prefix='supplier-cohort-') as raw:
        rt = seed(raw)
        header, lines = prepare_source(rt)
        operations = [edit(rt, header, lines, 'bounded-source-'+str(i), 'revision-'+str(i))['acceptance']['operation_id'] for i in range(13)]
        with _connect(rt.db_path) as conn:
            conn.execute(f"INSERT INTO {processing.ATTEMPTS} VALUES(?,?,?,?)", (operations[-1], 'processing', 'supplier_exact_queue_pending', '0000'))
            conn.commit()
        visited = set()
        retired_times = {}
        with patch.object(processing, 'COHORT_LIMIT', 3), heavy_admitted(rt.runtime_dir, operation='fixture'), warehouse_functional_job_lock(rt.runtime_dir):
            for cycle in range(7):
                result = processing.reconcile(rt)
                assert len(result['operations']) <= 3, result
                if cycle == 0:
                    assert operations[-1] in {item['operation_id'] for item in result['operations']}
                for item in result['operations']:
                    identity = item['operation_id']
                    if item.get('terminal'):
                        assert identity != operations[-1]
                        assert identity not in visited, 'retired operation retried'
                        assert item['reason_code'] == processing.SUPERSEDED
                        visited.add(identity)
                with closing(source.readonly(rt.db_path)) as conn:
                    for row in conn.execute(f'SELECT * FROM {processing.ATTEMPTS} WHERE reason=?', (processing.SUPERSEDED,)):
                        if row['operation_id'] in retired_times:
                            assert retired_times[row['operation_id']] == row['checked_at']
                        retired_times[row['operation_id']] = row['checked_at']
            assert visited == set(operations[:-1]), visited
            assert operations[-1] not in visited
            pending = source.read_acceptance(rt.db_path, operations[-1], request_scope='alice-key')
            assert not pending['processing']['complete'] and not pending['processing'].get('terminal')
            old = source.read_acceptance(rt.db_path, operations[0], request_scope='alice-key')
            assert old['durable_saved'] and old['processing']['terminal'] and not old['processing']['complete']
            # A changed/invalid native pointer is not proven monotonic source.
            # Even though revision is greater, its fingerprint does not match
            # actual native source and must never retire this accepted action.
            with _connect(rt.db_path) as conn:
                conn.execute(f"UPDATE {source.intents.TABLE} SET revision=revision+1,source_fingerprint='invalid-unverified-source' WHERE shipment_id='source'")
                conn.commit()
            retry = processing.reconcile(rt)
            assert len(retry['operations']) == 1 and retry['operations'][0]['operation_id'] == operations[-1]
            assert retry['operations'][0]['state'] == 'processing' and not retry['operations'][0]['terminal']
            unverified = source.read_acceptance(rt.db_path, operations[-1], request_scope='alice-key')
            assert not unverified['processing'].get('terminal') and unverified['reason_code'] != 'source_superseded'
            with closing(source.readonly(rt.db_path)) as conn:
                attempt = processing.public_completion(conn, operations[-1])
                assert not attempt['terminal'] and attempt['reason_code'] == 'supplier_completion_source_changed'
    print('bounded stable cohort progresses past pending; exact newer-source CAS retires once; retained old GET; unverified newer pointer stays retry: OK')


def finance_fixture(raw, *, rows=4, split=False):
    """Two actual independent documents sharing the same current Finance scope."""
    import json, sqlite3
    from datetime import date
    from apps.operator_supplier_processing_smoke import publish_functional, publish_accounting, NOW, MOMENT, DAY
    from apps.supplier_preparation_intents_smoke import HEADER, LINES
    from apps.cny_ledger_smoke import _save_payment
    from apps.wb_finance_weekly_cost_cutover_smoke import _row
    from packages.application.cny_ledger import CnyLedgerBlock
    from packages.application import operator_supplier_financial as financial, supplier_preparation_intents as intents
    from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
    rt = seed(raw)
    for owner in ('source','target'):
        rt.save_supplier_shipment(header={**HEADER,'shipment_id':owner,'created_at':NOW,'updated_at':NOW,'invoice_date':DAY,'shipment_date':DAY,'invoice_no':owner,'invoice_amount_total':100,'product_qty_total':10,'product_amount_total':100},lines=[{**LINES[0],'line_id':owner+'-line','internal_nm_id':1}])
    ledger = CnyLedgerBlock(runtime=rt,timestamp_factory=lambda:NOW)
    ledger.create_opening_balance({'operation_date':DAY,'cny_amount':200,'rub_value':2000})
    for owner in ('source','target'):_save_payment(rt,owner+'-payment',owner,NOW,'100')
    ledger.replay_ledger(reason='disposable-native')
    children = []
    for owner in ('source','target'):
        identity = owner+'-financial'
        def write(child,owner=owner,identity=identity):
            return rt.save_supplier_financial_document(document=dict(document_id=identity,supplier_order_id=owner,document_type='logistics_invoice',uploaded_at=NOW,updated_at=NOW,document_date=DAY,parse_status='confirmed',total_amount_rub=120,file_sha256='a'*64),expense_lines=[dict(line_id=identity+'-expense',amount=120,amount_rub=120,currency='RUB',category='domestic_transport',status='confirmed')])
        value = financial.execute(rt,action='confirm_upload',payload={'request_id':identity},shipment_id=owner,actor='alice',request_scope='alice-key',manifest=[{'child_key':identity,'kind':'financial','subject_id':identity}],validate=lambda owner=owner:[owner],write_child=write)
        children.append(value['results'][0]['acceptance']['operation_id'])
    intents.drain_supplier_preparation_intents(rt,shipment_ids=['source','target'])
    publish_functional(rt);publish_accounting(rt,opening=True)
    block = WbFinanceWeeklyBlock(rt.runtime_dir,seller_id='canonical',now_factory=lambda:MOMENT)
    block.ensure_schema();block.ingest_week(date(2026,7,20),date(2026,7,26),[{**_row(i,DAY,nm_id=1),'reportId':1} for i in range(1,rows+1)])
    raw_path = rt.db_path
    if split:
        from packages.application.storage_registry import atomic_write_manifest, build_manifest
        raw_path = rt.runtime_dir/'raw.sqlite3'
        with closing(sqlite3.connect(rt.db_path)) as main, closing(sqlite3.connect(raw_path)) as raw_conn:main.backup(raw_conn)
        for path,table,logical,revision,generation in ((rt.db_path,'finance_operational_schema_meta','operational','operational_v1','op-test'),(raw_path,'finance_raw_schema_meta','finance_raw','finance_raw_v1','raw-test')):
            with closing(sqlite3.connect(path)) as conn,conn:
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute(f'CREATE TABLE IF NOT EXISTS {table}(singleton INTEGER PRIMARY KEY,schema_revision TEXT,logical_store TEXT,generation_id TEXT,generation_epoch TEXT,source_fingerprint TEXT,created_at TEXT)')
                conn.execute(f'INSERT OR REPLACE INTO {table} VALUES(1,?,?,?,?,?,?)',(revision,logical,generation,'test','sha256:'+'a'*64,NOW))
                if path==raw_path:conn.execute('ALTER TABLE wb_finance_weekly_raw_rows RENAME TO finance_raw_current_rows')
        manifest=build_manifest(state='cutover',canonical_source='split',generation_epoch='test',raw_generation_id='raw-test',raw_relative_path=raw_path.name,raw_watermark='1',operational_generation_id='op-test',operational_relative_path=rt.db_path.name,operational_watermark='1',rollback_generation_id='monolith',source_fingerprint='sha256:'+'a'*64)
        atomic_write_manifest(block.store_registry.manifest_path,manifest)
    with closing(source.readonly(rt.db_path)) as conn:
        identities=[conn.execute(f'SELECT operation_id FROM {financial.SCOPES} WHERE parent_operation_id=?',(child,)).fetchone()[0] for child in children]
        scopes=[processing.queue_ref(processing.queue_for(conn,processing.operation(conn,identity))) for identity in identities]
        assert all(processing.current(conn,processing.operation(conn,identity)) for identity in identities)
    assert scopes[0]['affected_nm_ids']==scopes[1]['affected_nm_ids'] and scopes[0]['effective_date']==scopes[1]['effective_date']
    return rt, identities, scopes, raw_path


def _reconcile(rt):
    from apps.operator_supplier_processing_smoke import MOMENT
    with heavy_admitted(rt.runtime_dir,operation='fixture'),warehouse_functional_job_lock(rt.runtime_dir):
        return processing.reconcile(rt,now=MOMENT)


def finance_cohort_cases():
    import sqlite3,time,json
    from contextlib import ExitStack
    from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
    from packages.application import operator_supplier_cost_proof as cost, fbs_accounting_runtime as accounting
    original=WbFinanceWeeklyBlock._build_week_target_projection
    with TemporaryDirectory(prefix='supplier-finance-cohort-20k-') as raw:
        rt,identities,scopes,_=finance_fixture(raw,rows=20000);calls=[]
        def counted(block,*args,**kwargs):calls.append(kwargs['week_start']);return original(block,*args,**kwargs)
        started=time.perf_counter();cpu=time.process_time()
        with patch.object(WbFinanceWeeklyBlock,'_build_week_target_projection',counted):result=_reconcile(rt)
        elapsed=(time.perf_counter()-started)*1000;cpu_ms=(time.process_time()-cpu)*1000
        assert len(calls)==1 and all(r['complete'] for r in result['operations']), (calls,result)
        with closing(source.readonly(rt.db_path)) as conn:
            assert conn.execute(f'SELECT count(*) FROM {processing.COMPLETIONS}').fetchone()[0]==2
        with patch.object(WbFinanceWeeklyBlock,'_build_week_target_projection',side_effect=AssertionError('completed operation Finance replay')):
            assert _reconcile(rt)['status']=='no_op'
        print('actual two same-scope financial children/20k rows: '+json.dumps({'week_projection_rebuilds':len(calls),'full_reconcile_ms':elapsed,'process_cpu_ms':cpu_ms,'own_receipt_count':2}))
    # Different scope must have an independently derived source dependency;
    # a shared target week still has one native projection on one RO snapshot.
    with TemporaryDirectory(prefix='supplier-finance-scopes-') as raw:
        rt,identities,scopes,_=finance_fixture(raw);calls=[]
        with ExitStack() as stack:
            cohort=cost.FinanceProofCohort(rt,seller_id='canonical',stack=stack)
            from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
            book,_=accounting.load(rt.runtime_dir)
            shared=accounting.ActiveSharedCostSnapshot(list(book['shared_days'].values()),effective_date=book['effective_date']).metadata()['version_id']
            def counted(block,*args,**kwargs):calls.append(kwargs['week_start']);return original(block,*args,**kwargs)
            with patch.object(WbFinanceWeeklyBlock,'_build_week_target_projection',counted):
                one=cohort.read(queue_ref=scopes[0],shared_version=shared,handoff=[])
                two=cohort.read(queue_ref={**scopes[1],'affected_nm_ids':[1,999]},shared_version=shared,handoff=[])
                none=cohort.read(queue_ref={**scopes[1],'affected_nm_ids':[999]},shared_version=shared,handoff=[])
            cohort.seal();assert len(cohort.results)==3 and len(calls)==1 and one==two and none['status']=='not_applicable'
        print('different scope keys retain native selection; shared-week projection one: OK')
    # A malformed/stale source failure must not starve a valid sibling.
    with TemporaryDirectory(prefix='supplier-finance-sibling-') as raw:
        rt,identities,_,_=finance_fixture(raw)
        with _connect(rt.db_path) as conn:
            conn.execute('UPDATE sheet_vitrina_v1_supplier_financial_documents SET file_sha256=? WHERE document_id=?',('b'*64,'source-financial'));conn.commit()
        result=_reconcile(rt);states={r['operation_id']:r for r in result['operations']}
        assert not states[identities[0]]['complete'] and states[identities[1]]['complete'],result
        print('one stale financial source leaves valid sibling complete: OK')
    for store in ('raw','book'):
        with TemporaryDirectory(prefix='supplier-finance-late-'+store+'-') as raw:
            rt,identities,_,raw_path=finance_fixture(raw,split=True);insert=processing._insert_completion;changed=[]
            def late(conn,identity,proof):
                insert(conn,identity,proof)
                if not changed:
                    path=raw_path if store=='raw' else accounting.path(rt.runtime_dir)
                    with closing(sqlite3.connect(path)) as other,other:
                        if store=='book':other.execute("UPDATE accounting_revisions SET payload=payload || ' '")
                        else:other.execute('CREATE TABLE late_external_change(value)');other.execute('INSERT INTO late_external_change VALUES(1)')
                    changed.append(store)
            with patch.object(processing,'_insert_completion',late):result=_reconcile(rt)
            assert changed and not any(r['complete'] for r in result['operations']),result
            with closing(source.readonly(rt.db_path)) as conn:assert conn.execute(f'SELECT count(*) FROM {processing.COMPLETIONS}').fetchone()[0]==0
            assert all(r['complete'] for r in _reconcile(rt)['operations'])
            print('actual '+store+' commit after receipt DML rolls back whole uncommitted cohort; next fresh retry completes: OK')
    for drift in ('unrelated_main','manifest','new_financial_data'):
        with TemporaryDirectory(prefix='supplier-finance-pre-'+drift+'-') as raw:
            rt,identities,_,_=finance_fixture(raw,split=(drift=='manifest'));calls=[]
            def changed():
                if calls:return
                calls.append(drift)
                if drift=='manifest':
                    from dataclasses import replace
                    from packages.application.storage_registry import StoreRegistry, atomic_write_manifest, manifest_payload, parse_manifest, _sha256
                    registry=StoreRegistry(rt.runtime_dir)
                    payload=manifest_payload(replace(registry.load(),rollback_generation_id='different-parent'),include_digest=False)
                    payload['manifest_sha256']=_sha256(payload)
                    atomic_write_manifest(registry.manifest_path,parse_manifest(payload))
                elif drift=='new_financial_data':
                    from datetime import date
                    from apps.operator_supplier_processing_smoke import DAY,MOMENT
                    from apps.wb_finance_weekly_cost_cutover_smoke import _row
                    WbFinanceWeeklyBlock(rt.runtime_dir,seller_id='canonical',now_factory=lambda:MOMENT).ingest_week(date(2026,7,20),date(2026,7,26),[{**_row(i,DAY,nm_id=1),'reportId':1,'retailPriceWithDisc':'333'} for i in range(1,5)])
                else:
                    with _connect(rt.db_path) as conn:conn.execute('CREATE TABLE unrelated_source_commit(value)');conn.commit()
            with patch.object(processing,'_after_native_proof',changed):result=_reconcile(rt)
            assert calls and not any(r['complete'] for r in result['operations']),result
            with closing(source.readonly(rt.db_path)) as conn:assert conn.execute(f'SELECT count(*) FROM {processing.COMPLETIONS}').fetchone()[0]==0
            assert all(r['complete'] for r in _reconcile(rt)['operations'])
            print(drift+' during proof rejects stale cohort; fresh native retry complete: OK')
    with TemporaryDirectory(prefix='supplier-finance-own-commit-') as raw:
        rt,identities,_,_=finance_fixture(raw);calls=[]
        def counted(block,*args,**kwargs):calls.append(kwargs['week_start']);return original(block,*args,**kwargs)
        with patch.object(processing,'COMPLETION_COHORT_LIMIT',1),patch.object(WbFinanceWeeklyBlock,'_build_week_target_projection',counted):result=_reconcile(rt)
        assert len(calls)==2 and all(r['complete'] for r in result['operations']),result
        print('own committed receipts force next chunk fresh observer/projection; no token advancement: OK')
def large_proof_cases():
    # The native proof is real; a synthetic unused descriptor only forces
    # receipt serialization/cache spill. It supplies no completion authority.
    import sqlite3,time
    from packages.application import operator_supplier_cost_proof as cost
    from packages.application.sqlite_contention import ObservedSQLiteConnection
    real_connect=sqlite3.connect;real_proof=cost.read_native_proof
    for fallback in (False,True):
        with TemporaryDirectory(prefix='supplier-large-proof-') as raw:
            rt,identities,_,_=finance_fixture(raw);connections=[]
            import gc
            gc.collect()
            with closing(real_connect(rt.db_path)) as conn:
                assert conn.execute('PRAGMA journal_mode=DELETE').fetchone()[0]=='delete'
            class GuardedConnection(ObservedSQLiteConnection):
                marked_dml=False
                main_path=None
                def execute(self,sql,*args,**kwargs):
                    if sql.strip().lower() in ('pragma main.data_version','pragma data_version') and self.main_path==rt.db_path.resolve():
                        for other in connections:
                            if other is not self and other.marked_dml:
                                raise AssertionError('operational observer read after receipt DML/cache spill')
                    return super().execute(sql,*args,**kwargs)
                def commit(self):
                    result=super().commit();self.marked_dml=False;return result
                def rollback(self):
                    result=super().rollback();self.marked_dml=False;return result
                def close(self):
                    self.marked_dml=False;return super().close()
            def connected(*args,**kwargs):
                kwargs['factory']=GuardedConnection;conn=real_connect(*args,**kwargs)
                conn.configure_contention(timeout_ms=2000,priority='normal')
                conn.main_path=Path(conn.execute('PRAGMA database_list').fetchone()[2]).resolve();connections.append(conn)
                return conn
            def large(*args,**kwargs):
                value=real_proof(*args,**kwargs);value['fixture_attachment_descriptor']='p'*(256*1024);return value
            validate=processing._validate_completion;insert=processing._insert_completion
            def limited(conn,*args):
                conn.execute('PRAGMA cache_size=8');conn.execute('PRAGMA cache_spill=ON');return validate(conn,*args)
            def marked(conn,*args):
                result=insert(conn,*args);conn.marked_dml=True;return result
            started=time.perf_counter()
            with patch.object(sqlite3,'connect',connected),patch.object(cost,'read_native_proof',large),patch.object(processing,'_validate_completion',limited),patch.object(processing,'_insert_completion',marked),patch.object(processing,'COMPLETION_COHORT_MAX_BYTES',32*1024 if fallback else 8*1024**2):
                result=_reconcile(rt)
            elapsed=(time.perf_counter()-started)*1000
            assert all(r['complete'] for r in result['operations']) and elapsed<5000,result
            print('large synthetic proof descriptor/DELETE cache8pages, '+('single oversized fallback' if fallback else 'bounded cohort')+': no main observer read after DML; '+str(round(elapsed,2))+'ms: OK')


def failed_preparation_cases():
    import gc, sqlite3, time
    from contextlib import ExitStack
    from packages.application import operator_supplier_cost_proof as cost, fbs_accounting_runtime as accounting
    # Real native projection failures occur after the per-source RO BEGIN.
    # DELETE journal must still retain attempts and complete valid siblings.
    for failure in ('dated_all', 'source_sql', 'target_sql'):
        with TemporaryDirectory(prefix='supplier-failed-reader-'+failure+'-') as raw:
            rt, identities, _, _ = finance_fixture(raw)
            gc.collect()
            with closing(sqlite3.connect(rt.db_path)) as conn:
                assert conn.execute('PRAGMA journal_mode=DELETE').fetchone()[0] == 'delete'
                if failure == 'dated_all':
                    assert conn.execute("UPDATE sheet_vitrina_v1_warehouse_wb_daily_cost SET wac_rub='999'").rowcount > 0
                    failed = set(identities); reason = 'supplier_dated_cost_publication_changed'
                else:
                    failed = {identities[0 if failure == 'source_sql' else 1]}; reason = 'no such table'
                conn.commit()
            native = cost.read_native_proof
            def read(*args, **kwargs):
                if failure != 'dated_all' and args[1] in failed:
                    # Inject an actual SQLite read error after the real
                    # per-source BEGIN/SHARED lock. No authority is changed
                    # and the valid sibling uses its entire native proof.
                    observer = kwargs['connection']; assert observer.in_transaction
                    observer.execute('SELECT count(*) FROM sqlite_master').fetchone()
                    observer.execute('SELECT * FROM missing_supplier_fixture_operand')
                return native(*args, **kwargs)
            started = time.perf_counter()
            with patch.object(cost, 'read_native_proof', read):result = _reconcile(rt)
            elapsed = (time.perf_counter()-started)*1000
            states = {item['operation_id']: item for item in result['operations']}
            assert set(states) == set(identities) and result['status'] == 'pending', result
            with closing(source.readonly(rt.db_path)) as conn:
                attempts = {row['operation_id']: row['reason'] for row in conn.execute(f'SELECT * FROM {processing.ATTEMPTS}')}
                completed = {row[0] for row in conn.execute(f'SELECT operation_id FROM {processing.COMPLETIONS}')}
            assert attempts == {identity: reason for identity in failed} and completed == set(identities)-failed, (attempts, completed)
            assert all(not states[identity]['complete'] for identity in failed)
            assert all(states[identity]['complete'] for identity in completed)
            assert elapsed < 2000, elapsed
            print('native DELETE '+failure+': failed reader released, exact attempts '+str(len(attempts))+', valid siblings '+str(len(completed))+', '+str(round(elapsed,2))+'ms: OK')
    # A per-scope Finance refusal must not roll back the successful coherent
    # snapshot or discard its actual native projection/capitalization cache.
    with TemporaryDirectory(prefix='supplier-finance-scope-refusal-') as raw:
        rt, _, scopes, _ = finance_fixture(raw)
        book, _ = accounting.load(rt.runtime_dir)
        shared = accounting.ActiveSharedCostSnapshot(list(book['shared_days'].values()), effective_date=book['effective_date']).metadata()['version_id']
        with ExitStack() as stack:
            cohort = cost.FinanceProofCohort(rt, seller_id='canonical', stack=stack)
            first = cohort.read(queue_ref=scopes[0], shared_version=shared, handoff=[])
            observer = cohort.conn; projections = dict(cohort.projections)
            try:cohort.read(queue_ref=scopes[1], shared_version='sha256:'+'f'*64, handoff=[])
            except ValueError as exc:assert str(exc) == 'supplier_finance_accounting_version_mismatch', str(exc)
            else:raise AssertionError('expected native scope refusal')
            assert cohort.conn is observer and observer.in_transaction and cohort.projections == projections
            second = cohort.read(queue_ref={**scopes[1], 'affected_nm_ids':[1,999]}, shared_version=shared, handoff=[])
            assert first == second and cohort.projections == projections
            cohort.seal()
        print('native Finance scope refusal preserves successful shared snapshot/cache and valid sibling: OK')
    # Actual open-authority drift refuses both sources and closes the failed
    # native connection before the attempt writer. A new cohort can recover.
    with TemporaryDirectory(prefix='supplier-finance-opening-refusal-') as raw:
        from dataclasses import replace
        from packages.application.storage_registry import StoreRegistry, atomic_write_manifest, manifest_payload, parse_manifest, _sha256
        from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
        rt, identities, _, _ = finance_fixture(raw, split=True)
        gc.collect()
        with closing(sqlite3.connect(rt.db_path)) as conn:
            assert conn.execute('PRAGMA journal_mode=DELETE').fetchone()[0] == 'delete'
        opened = []; original = WbFinanceWeeklyBlock._connect_stale_cost_plan
        def changed_open(block, *args, **kwargs):
            conn = original(block, *args, **kwargs); opened.append(conn)
            registry = StoreRegistry(rt.runtime_dir)
            payload = manifest_payload(replace(registry.load(), rollback_generation_id='changed-opening-parent'), include_digest=False)
            payload['manifest_sha256'] = _sha256(payload)
            atomic_write_manifest(registry.manifest_path, parse_manifest(payload))
            return conn
        with patch.object(WbFinanceWeeklyBlock, '_connect_stale_cost_plan', changed_open):
            result = _reconcile(rt)
        assert len(opened) == 1 and not any(item['complete'] for item in result['operations']), result
        try:opened[0].execute('SELECT 1')
        except sqlite3.ProgrammingError:pass
        else:raise AssertionError('failed Finance opening observer retained')
        with closing(source.readonly(rt.db_path)) as conn:
            assert {row['operation_id']: row['reason'] for row in conn.execute(f'SELECT * FROM {processing.ATTEMPTS}')} == {identity:'supplier_finance_cohort_authority_changed' for identity in identities}
            assert conn.execute(f'SELECT count(*) FROM {processing.COMPLETIONS}').fetchone()[0] == 0
        assert all(item['complete'] for item in _reconcile(rt)['operations'])
        print('actual native opening manifest drift releases failed observer; exact attempts then fresh cohort completes: OK')


if __name__ == '__main__':
    main()
    finance_cohort_cases()
    large_proof_cases()
    failed_preparation_cases()
