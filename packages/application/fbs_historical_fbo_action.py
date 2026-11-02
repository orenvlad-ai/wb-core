"""Registered accounting action; live sources, private preflight, owned phases.

The normal cycle remains the sole owner of History/Finance completion. A local
source interruption is recovered only by an explicit continuation of the same
immutable recipe. Readback never writes or submits anything.
"""
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import date, datetime, timezone
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time

from apps.production_apply_contract import AmbiguousSubmit
from packages.application import fbs_accounting_runtime as accounting
from packages.application import fbs_historical_fbo_publication as typed
from packages.application import fbs_historical_fbo_native as native
from packages.application import ready_publication as ready
from packages.application.fbs_snapshot_cost import canonical, fingerprint
from packages.application.fbs_snapshot_cost_sources import _documents
from packages.application.fbs_historical_fbo_dispatch import CAPTURE_CONTRACT, prepare_historical_fbo_dispatch_plan
from packages.application.ff_pool_documents import REQUESTS_TABLE, DOCUMENTS_TABLE
from packages.application.storage_registry import StoreRegistry
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock

RESTORE = 'restore_historical_fbo'
RECOVER = 'recover_historical_fbo'
MAX_BYTES = 512 * 1024**2
# Only disposable source copies; publication/JSON limits remain unchanged.
MAX_SOURCE_COPY_BYTES = 1024 * 1024**2
MAX_ROWS = 100000
RESERVE = 10 * 1024**3
P = 'sheet_vitrina_v1_'
EVIDENCE_OWNER = 'warehouse_fbs_historical_recovery_plan'


def require(value, code):
    if not value:
        raise ValueError('historical_fbo_action_' + code)


def _read_json(path, *, limit=MAX_BYTES):
    path = Path(path)
    require(not path.is_symlink() and path.is_file() and not path.stat().st_mode & 0o077 and path.stat().st_size <= limit,
            'private_artifact_invalid')
    return json.loads(path.read_text())


def _private(path, value):
    from packages.application.business_data_write_barrier import _atomic_write_private_json
    _admit(path,len(canonical(value).encode()))
    _atomic_write_private_json(path, value)


def _hosted():
    return Path('/opt/wb-core-runtime/app/.wb-core-runtime-sha').is_file()


def _admit(path, size):
    if _hosted():
        from packages.application.root_storage_policy import admit_root_write
        return admit_root_write(owner=EVIDENCE_OWNER,destination=path,predicted_output_bytes=size)


def _directory(root, operation_id):
    # The launcher validates IDs; direct domain use must have the same boundary.
    from apps.production_apply_launcher import validate_operation
    require(validate_operation(operation_id) == operation_id, 'operation_invalid')
    if _hosted():
        from packages.application.root_storage_policy import resolve_storage_destination
        result=resolve_storage_destination(EVIDENCE_OWNER,'historical-fbo',fingerprint(str(root))[7:23],operation_id)
    else:
        result = root / 'evidence' / 'historical-fbo' / operation_id
    require(not any(p.is_symlink() for p in (result, result.parent, result.parent.parent)), 'artifact_symlink')
    return result


def _inputs(request):
    """Only documentary authority is supplied; never accept calculated capture."""
    require(not any(k in request for k in ('capture','native_capture','native_plan','candidate_book')), 'pasted_capture_forbidden')
    retained=request.get('retained_files',{})
    require(type(retained) is dict and len(retained)<=32,'retained_file_scope_invalid')
    def retained_path(value):
        original=Path(value).resolve()
        return Path(retained.get(str(original),str(original)))
    file = retained_path(request['evidence_manifest_path'])
    data = file.read_bytes()
    require(len(data) <= 2 * 1024**2 and not file.is_symlink()
            and hashlib.sha256(data).hexdigest() == request['evidence_manifest_sha256'], 'documentary_manifest_changed')
    value = json.loads(data)
    require(set(value) == {'scope','opening_date','physical','restored','evidence_files','fees'}, 'documentary_fields_invalid')
    files = []
    for item in value['evidence_files']:
        path = retained_path(item['path'])
        require(not path.is_symlink() and path.is_file() and path.stat().st_size <= 16 * 1024**2,
                'documentary_file_invalid')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        require(digest == item['sha256'], 'documentary_file_changed')
        files.append(dict(path=str(Path(item['path']).resolve()),sha256=digest))
    require(files, 'documentary_files_required')
    proofs = fingerprint(files)
    # Physical observation and owner-restored attribution remain distinct.
    physical = deepcopy(value['physical']); restored = deepcopy(value['restored'])
    require(physical.get('source_digest') == proofs and restored.get('source_digest') == proofs,
            'documentary_proof_binding_missing')
    workbooks = {}; fees = []
    for item in value['fees']:
        path = retained_path(item['path'])
        require(not path.is_symlink() and path.is_file() and path.stat().st_size <= 16 * 1024**2,
                'workbook_invalid')
        data = path.read_bytes()
        require(set(item)=={'path','request_scope','request_id','file_sha256','wb_supply_id','amount_with_vat_rub','original_invoice'},'fee_documentary_fields_invalid')
        invoice=item['original_invoice']
        require(set(invoice)=={'path','sha256','invoice_no','payment_request_id','payment_source_sha256'},'invoice_reference_invalid')
        pdf=retained_path(invoice['path']);pdf_bytes=pdf.read_bytes()
        require(not pdf.is_symlink() and len(pdf_bytes)<=16*1024**2 and pdf_bytes.startswith(b'%PDF-')
            and hashlib.sha256(pdf_bytes).hexdigest()==invoice['sha256'],'original_invoice_changed')
        fee = {k:v for k,v in item.items() if k not in {'path','original_invoice'}}
        require(hashlib.sha256(data).hexdigest() == fee['file_sha256'], 'workbook_changed')
        key = (fee['request_scope'],fee['request_id'])
        require(key not in workbooks, 'workbook_identity_duplicate')
        workbooks[key] = data; fees.append(fee)
    return value, physical, restored, fees, workbooks


def _retain_inputs(directory, request, inputs):
    """Keep reviewed source bytes by hash, independent of operator file paths."""
    sources=[(Path(request['evidence_manifest_path']),request['evidence_manifest_sha256']),
             *[(Path(r['path']),r['sha256']) for r in inputs[0]['evidence_files']],
             *[(Path(r['path']),r['file_sha256']) for r in inputs[0]['fees']],
             *[(Path(r['original_invoice']['path']),r['original_invoice']['sha256']) for r in inputs[0]['fees']]]
    result={};folder=directory/'reviewed-sources';folder.mkdir(mode=0o700)
    for source,expected in sources:
        data=source.read_bytes();require(len(data)<=16*1024**2,'retained_source_size')
        require(hashlib.sha256(data).hexdigest()==expected,'retained_source_changed')
        target=folder/(hashlib.sha256(data).hexdigest()+'.source')
        if not target.exists():
            _admit(target,len(data))
            with os.fdopen(os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as handle:
                handle.write(data);handle.flush();os.fsync(handle.fileno())
        result[str(source.resolve())]=str(target)
    return result


def capture_native_sources(conn, *, book, scope, opening_date, physical, restored):
    """Re-read native authority on the caller's exact query-only snapshot."""
    require(conn.in_transaction and conn.row_factory is sqlite3.Row
            and conn.execute('PRAGMA query_only').fetchone()[0] == 1, 'pinned_read_required')
    typed._healthy(book)
    state = book['state']; opening = state['periods'].get(opening_date, state['baseline'])
    day = opening.get('business_date') or opening.get('snapshot',{}).get('date')
    require(day == opening_date and (opening is state['baseline'] or opening['status'] == 'closed'), 'closed_opening_required')
    documents = _documents(conn)
    known = dict(state['baseline']['absorbed_documents'])
    for d,p in sorted(state['periods'].items()):
        if d <= opening_date: known.update(p['applied_documents'])
    actual = {d['document_id']:d['fingerprint'] for d in documents}
    require(all(actual.get(k) == v for k,v in known.items()), 'opening_document_changed')
    last = max(d['business_date'] for d in scope['dispatches'])
    start = date.fromisoformat(opening_date).toordinal() + 1
    start = date.fromordinal(start).isoformat()
    queries = []
    def rows(sql,args=()):
        result=[dict(r) for r in conn.execute(sql,args).fetchmany(MAX_ROWS+1)]
        require(len(result)<=MAX_ROWS and len(canonical(result).encode())<=64*1024**2,'capture_budget')
        queries.append((sql,args));return result
    interval_docs=rows(f'SELECT * FROM {DOCUMENTS_TABLE} WHERE business_date>=? AND business_date<=? ORDER BY document_id',(start,last))
    interval_requests=rows(f'SELECT * FROM {REQUESTS_TABLE} WHERE business_date>=? AND business_date<=? ORDER BY request_id',(start,last))
    require(not interval_docs and not interval_requests,'cost_interval_not_empty')
    supplies={};nms={scope['reallocation']['nm_id']}
    from packages.application.warehouse_stocks import _normalized_wb_record, _validated_wb_goods
    for dispatch in scope['dispatches']:
        sid=dispatch['supply_id']
        raw=rows(f'SELECT * FROM {P}wb_supplies WHERE supply_id=? OR wb_supply_id=? OR cache_key=? ORDER BY supply_id',(sid,sid,'supply:'+sid))
        require(len(raw)==1 and raw[0]['supply_id']==raw[0]['wb_supply_id']==sid
                and raw[0]['cache_key']=='supply:'+sid,'canonical_supply_required')
        goods=_validated_wb_goods(_normalized_wb_record(raw[0]))
        items=[dict(nm_id=int(g['nm_id']),quantity=int(g['quantity'])) for g in goods]
        require(all(str(g['quantity']) in {str(i['quantity']),str(i['quantity'])+'.0'} for g,i in zip(goods,items)), 'packed_integer_required')
        debits=rows(f'SELECT * FROM {P}ff_stock_operations WHERE source_key=? OR (source_type=\'wb_supply\' AND source_object_id IN (?,?)) ORDER BY operation_id',
                    ('wb_supply_debit:supply:'+sid,sid,'supply:'+sid))
        supplies[sid]=dict(supply_id=sid,wb_supply_id=sid,cache_key='supply:'+sid,
            business_date=dispatch['business_date'],source_digest=fingerprint(raw[0]),existing_debits=debits,items=items)
        nms.update(i['nm_id'] for i in items)
    facility=scope['facility_id'];aux=opening['document_cost_state']['fbo_rows']
    fbo={k:deepcopy(r) for k,r in aux.items() if r['facility_id']==facility and int(r['nm_id']) in nms}
    fbs=deepcopy(opening['rows'][facility+':'+str(scope['reallocation']['nm_id'])])
    fence=json.loads(canonical(ready.pin_queries(conn,queries)))
    result=dict(contract=CAPTURE_CONTRACT,book_version=fingerprint(book),native_query_fence_digest=fingerprint(fence),
        opening=dict(business_date=opening_date,period_digest=opening.get('period_digest') or opening.get('id'),
            source_digest=opening['source_digest'],document_manifest=known,fbo_rows=fbo,fbs_reallocation_row=fbs),
        native_cost_document_coverage=dict(complete=True,date_from=start,date_to=last,documents=[],requests=[],query_fence_digest=fingerprint(fence)),
        known_document_manifest=known,protected_source_fingerprints={**actual,'native_document_set':fingerprint(actual)},
        reallocation_physical_source=deepcopy(physical),restored_pool_allocation=deepcopy(restored),supply_sources=supplies)
    result['source_digest']=fingerprint(result)
    return result


def _preflight_code():
    result={**typed.publication._code(),**typed._adapter_code()}
    for name in ('fbs_historical_fbo_action.py','fbs_accounting_rehearsal_sources.py',
                 'web_vitrina_window_v3.py','sheet_vitrina_v1_inventory_history.py','fbs_current_snapshot_policy.py'):
        result[name]='sha256:'+hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
    return result


def _check_preflight_code(preflight):
    require(preflight.get('code_authority')==_preflight_code(),'preflight_code_changed')


def _runtime(root):
    from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
    return RegistryUploadDbBackedRuntime(runtime_dir=root)


def _maintenance(root, request):
    from packages.application import business_data_maintenance_pause as pause
    from packages.application import business_data_write_barrier as barrier
    from apps.business_data_maintenance import RuntimeScheduleClient, SystemdClient, _read_env_file, _build_web_auth_cookie
    old=pause.load_state(root);held=barrier.barrier_status(root)
    require(old and old['phase']=='held' and old['window_id']==request['window_id']
        and held.get('active') is True and held.get('phase')=='held' and held.get('hold_confirmed') is True
        and held.get('window_kind')=='maintenance_pause' and held.get('window_id')==old['window_id']
        and held.get('plan_fingerprint')==old['baseline_fingerprint'],'held_pause_required')
    env=_read_env_file(Path(request['maintenance_env_file']))
    base=env.get('BUSINESS_DATA_MAINTENANCE_BASE_URL') or f"http://{env.get('REGISTRY_UPLOAD_HTTP_HOST','127.0.0.1')}:{env.get('REGISTRY_UPLOAD_HTTP_PORT','8765')}"
    client=RuntimeScheduleClient(base_url=base,cookie=_build_web_auth_cookie(env))
    current=pause.readback(root,systemd=SystemdClient(),activity_reader=lambda:client._request(pause.ACTIVITY_PATH))
    pause._unchanged(old,current)
    require(current['quiet'] is True,'quiet_pause_required')
    return dict(window_id=old['window_id'],baseline_fingerprint=old['baseline_fingerprint'])


@contextmanager
def _held(root, request):
    from apps.business_data_maintenance import _ExclusiveRestoreLock
    from packages.application.business_data_write_barrier import _BarrierLock
    with _ExclusiveRestoreLock(root), _BarrierLock(root):
        yield _maintenance(root,request)


def _snapshot(runtime, inputs):
    value,physical,restored,fees,_=inputs
    book,version=accounting.load(runtime.runtime_dir);typed._healthy(book)
    with ready.readonly(runtime.db_path) as conn:
        from packages.application.fbs_current_snapshot_policy import resolve_current_snapshot_policy
        freshness_policy=asdict(resolve_current_snapshot_policy(connection=conn))
        capture=capture_native_sources(conn,book=book,scope=value['scope'],opening_date=value['opening_date'],physical=physical,restored=restored)
        plan=prepare_historical_fbo_dispatch_plan(capture,value['scope'])
        require(plan['status']=='prepared_unconnected','native_candidate_blocked:'+str(plan.get('reason')))
        guard=native.capture_native_guard(conn,plan)
        from packages.application import fbs_accounting_historical_revision_writer as staging
        material=ready.capture_material(conn,tables=staging.SOURCE_TABLES)
        active=conn.execute('SELECT bundle_version FROM registry_upload_current_state WHERE slot=1').fetchone()
        require(active is not None,'active_bundle_required')
        day=max(book['state']['periods'])
        selected=conn.execute(f'''SELECT as_of_date FROM {ready.TABLE} WHERE bundle_version=?
            AND EXISTS(SELECT 1 FROM json_each(plan_json,'$.date_columns') WHERE value=?)
            ORDER BY (as_of_date=?) DESC,activated_at DESC,refreshed_at DESC,as_of_date DESC LIMIT 1''',
            (active[0],day,day)).fetchone()
        require(selected is not None,'real_ready_current_column_required')
        target=dict(bundle_version=active[0],as_of_date=selected[0])
        expected=ready.capture_expected(conn,**target)
        require(expected.exists,'real_current_ready_required')
        invoice_refs=[]
        for item in value['fees']:
            ref=item['original_invoice']
            payment=conn.execute(f'SELECT request_id,state,error_code,source_filename,source_sha256,source_file_blob,request_payload_json,posted_document_id FROM {REQUESTS_TABLE} WHERE request_id=?',(ref['payment_request_id'],)).fetchone()
            require(payment and payment['state']=='blocked' and payment['error_code']=='overhead_confirmation_cancelled'
                and not payment['posted_document_id'] and payment['source_sha256']==ref['payment_source_sha256']
                and isinstance(payment['source_file_blob'],bytes)
                and 'sha256:'+hashlib.sha256(payment['source_file_blob']).hexdigest()==payment['source_sha256'],
                'original_payment_reference_changed')
            invoice_refs.append(dict(wb_supply_id=item['wb_supply_id'],invoice_no=ref['invoice_no'],invoice_sha256=ref['sha256'],
                payment_request_id=payment['request_id'],payment_source_sha256=payment['source_sha256'],
                payment_source_filename=payment['source_filename'],payment_comment=json.loads(payment['request_payload_json']).get('comment',''),
                payment_detail_path='/v1/sheet-vitrina-v1/warehouses/ff/facility-pools/requests/'+payment['request_id']))
        require(len({r['payment_request_id'] for r in invoice_refs})==3,'payment_reference_duplicate')
    return dict(book_version=version,capture=capture,native_plan=plan,native_guard=guard,material=material,freshness_policy=freshness_policy,
                ready_target=target,ready_fingerprint=expected.fingerprint,documentary_digest=fingerprint(value),original_invoice_refs=invoice_refs)


def _backup(source, destination, *, budget, deadline=180):
    """One bounded coherent backup from RO; never copy SQLite/WAL file bytes."""
    require(not destination.exists(),'backup_exists')
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True,timeout=1)) as src:
        src.execute('PRAGMA query_only=ON')
        size=src.execute('PRAGMA page_count').fetchone()[0]*src.execute('PRAGMA page_size').fetchone()[0]
        require(size<=budget and shutil.disk_usage(destination.parent).free>=size+RESERVE,'backup_disk_budget')
        _admit(destination,size)
        fd=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        started=time.monotonic()
        def progress(status,remaining,total):
            require(time.monotonic()-started<=deadline,'backup_deadline')
        try:
            with closing(sqlite3.connect(destination)) as dst:
                src.backup(dst,pages=2048,progress=progress,sleep=.01)
                require(dst.execute('PRAGMA quick_check').fetchall()==[('ok',)],'backup_integrity')
        except BaseException:
            destination.unlink(missing_ok=True);raise
    return _file_proof(destination,budget=budget)


def _file_proof(path, *, budget):
    require(not path.is_symlink() and path.is_file() and not path.stat().st_mode & 0o077
            and path.stat().st_size<=budget,'before_image_invalid')
    digest=hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda:handle.read(8*1024**2),b''):digest.update(chunk)
    return dict(path=str(path),size_bytes=path.stat().st_size,sha256=digest.hexdigest())


def _check_source_copy_budget(*,table,count,size,elapsed):
    exceeded=('rows' if count>MAX_ROWS else 'bytes' if size>MAX_SOURCE_COPY_BYTES else 'time' if elapsed>=120 else '')
    require(not exceeded,f'subset_{exceeded}_budget:table={table}:rows={count}:bytes={size}:elapsed={elapsed:.3f}')


def _subset(conn, destination, *, book, snapshot, now):
    """Copy only explicitly scoped source rows; never clone the 16GB database.

    Schema is copied without source writes. Original rowids preserve native
    version tie-breakers. Source triggers are installed only after row copying.
    Every copied query is pinned, including negative evidence.
    """
    nms=sorted({m['nm_id'] for e in snapshot['native_plan']['effects'] for m in e['pool_movements']})
    marks=','.join('?' for _ in nms);scope=snapshot['native_plan'];dates=sorted(book['state']['periods'])
    # Saved editions, actual latest snapshots by day, first valuation anchors.
    versions={book['wb_days'][d]['version_id'] for d in dates if d in book['wb_days']}
    sql=(f"WITH ranked AS (SELECT v.version_id,ROW_NUMBER() OVER(PARTITION BY s.snapshot_date ORDER BY v.effective_at DESC,v.created_at DESC,v.rowid DESC) rank FROM {P}warehouse_functional_versions v JOIN {P}warehouse_wb_snapshots s USING(version_id) WHERE v.status='good' AND s.snapshot_date<=?) SELECT version_id FROM ranked WHERE rank=1")
    versions.update(r[0] for r in conn.execute(sql,(dates[-1],)))
    versions.update(r[0] for r in conn.execute(f"SELECT version_id FROM {P}warehouse_functional_versions WHERE version_kind='functional_cutover'"))
    sqlanchors=(f"WITH ranked AS (SELECT b.version_id,ROW_NUMBER() OVER(PARTITION BY b.nm_id ORDER BY v.effective_at,v.created_at,v.rowid) rank FROM {P}warehouse_functional_versions v JOIN {P}warehouse_functional_balances b USING(version_id) WHERE v.status='good' AND b.warehouse_key='wb' AND b.nm_id IN ({marks}) AND b.provenance_json LIKE '%initial_valuation_anchor%') SELECT version_id FROM ranked WHERE rank=1")
    versions.update(r[0] for r in conn.execute(sqlanchors,nms))
    require(0<len(versions)<=1000,'subset_version_budget')
    vm=','.join('?' for _ in versions);va=sorted(versions)
    days=[book['state']['baseline']['business_date'],*dates]
    gids={book['state']['baseline']['snapshot']['id']} if 'snapshot' in book['state']['baseline'] else set()
    gids.update(p['snapshot']['id'] for p in book['state']['periods'].values())
    # Current capture may have a fresher genuine official generation.
    latest=conn.execute(f"SELECT run_id FROM {P}wb_fbs_warehouse_registry_runs WHERE complete=1 AND status='success' ORDER BY run_sequence DESC LIMIT 1").fetchone()
    if latest:gids.add(latest[0])
    gm=','.join('?' for _ in gids);ga=sorted(gids)
    order=f'SELECT order_id FROM {P}wb_supplies_fbs_order_observations WHERE nm_id IN ({marks})'
    op=f'SELECT operation_id FROM {P}ff_stock_operation_lines WHERE nm_id IN ({marks})'
    reservation=f'SELECT operation_id FROM {P}ff_stock_reservation_lines WHERE nm_id IN ({marks})'
    nativeops=f'SELECT operation_id FROM {P}ff_pool_movement_lines WHERE nm_id IN ({marks}) UNION SELECT operation_id FROM {P}ff_pool_documents'
    filters={
        P+'warehouse_functional_versions':(f'version_id IN ({vm})',va),
        P+'warehouse_wb_snapshots':(f'version_id IN ({vm})',va),
        P+'warehouse_functional_balances':(f'version_id IN ({vm})',va),
        P+'warehouse_functional_read_models':(f'version_id IN ({vm})',va),
        P+'warehouse_functional_events':(f"event_type='wb_final_acceptance' AND nm_id IN ({marks}) AND business_date<=?",[*nms,dates[-1]]),
        P+'warehouse_wb_daily_cost':(f'nm_id IN ({marks}) AND as_of_date<=?',[*nms,dates[-1]]),
        P+'wb_fbs_warehouse_registry_runs':(f'run_id IN ({gm})',ga),
        P+'wb_fbs_warehouse_registry_rows':(f'run_id IN ({gm})',ga),
        P+'wb_fbs_stock_snapshot_runs':(f'registry_run_id IN ({gm})',ga),
        P+'wb_fbs_stock_snapshot_rows':(f'run_id IN (SELECT run_id FROM {P}wb_fbs_stock_snapshot_runs WHERE registry_run_id IN ({gm}))',ga),
        P+'wb_supplies_fbs_order_observations':(f'order_id IN ({order})',nms),
        P+'wb_supplies_fbs_status_current':(f'order_id IN ({order})',nms),
        P+'ff_pool_fbs_lifecycle_current':(f'nm_id IN ({marks})',nms),
        P+'ff_pool_fbs_lifecycle_events':(f'nm_id IN ({marks})',nms),
        P+'ff_pool_fbs_reconciliation':(f'event_id IN (SELECT event_id FROM {P}ff_pool_fbs_lifecycle_events WHERE nm_id IN ({marks}))',nms),
        P+'ff_pool_fbs_applicability_events':(f'nm_id IN ({marks})',nms),
        P+'ff_pool_balances':(f'nm_id IN ({marks})',nms),
        P+'ff_pool_movement_lines':(f'operation_id IN ({nativeops})',nms),
        P+'warehouse_business_operations':(f'operation_id IN ({nativeops})',nms),
        P+'ff_stock_operation_lines':(f'operation_id IN ({op})',nms),
        P+'ff_stock_operations':(f'operation_id IN ({op})',nms),
        P+'ff_stock_reservation_lines':(f'operation_id IN ({reservation})',nms),
        P+'ff_stock_reservation_operations':(f'operation_id IN ({reservation})',nms),
    }
    for companion in ('functional_ff_reservations','supplier_cost_states','unmatched_doprinato'):
        filters[P+'warehouse_'+companion]=(f'version_id IN ({vm})',va)
    # All other allowed tables are small native contracts. Unknown source tables
    # stay empty; a missing required source fails the real helper preflight.
    extras=('ff_stock_operations','ff_stock_operation_lines','ff_stock_reservation_operations','ff_stock_reservation_lines',
        'ff_pool_feature_epochs','ff_pool_cutover_manifests','ff_pool_fbs_lifecycle_current','ff_pool_fbs_lifecycle_events',
        'ff_pool_fbs_reconciliation','ff_pool_fbs_applicability_events','ff_wb_supply_origin_assignments',
        'wb_supplies_fbs_order_observations','wb_supplies_fbs_status_current','wb_supplies_fbs_warehouse_facility_mappings',
        'wb_supplies','wb_supply_cost_layers','wb_supply_transit_cost_enrichment','fulfillment_service_uploads','fulfillment_service_lines',
        'operator_fulfillment_service_requests','operator_fulfillment_service_operations','fulfillment_recalc_intents',
        'warehouse_opening_cost_map','ready_publications','ready_input_revisions','ready_snapshots')
    from packages.application import fbs_accounting_historical_revision_writer as staging
    allowed=set(staging.SOURCE_TABLES)|set(filters)|{P+x for x in extras}
    # Real constants, not guessed aliases, own upload/receipt schema names.
    from packages.application import operator_fulfillment_services as receipts, fulfillment_recalc_intents as intents
    allowed.update((receipts.REQUESTS,receipts.TABLE,receipts.COMPLETIONS,intents.TABLE,intents.UPLOADS,intents.LINES))
    from packages.application.ff_wb_supply_origins import ASSIGNMENTS_TABLE
    from packages.application.ff_pool_fbs_lifecycle import RECONCILIATION_TABLE,EVENTS_TABLE,CURRENT_TABLE
    from packages.application.ff_pool_fbs_applicability import APPLICABILITY_EVENTS_TABLE
    allowed.update((ASSIGNMENTS_TABLE,RECONCILIATION_TABLE,EVENTS_TABLE,CURRENT_TABLE,APPLICABILITY_EVENTS_TABLE))
    for table in (RECONCILIATION_TABLE,EVENTS_TABLE,CURRENT_TABLE,APPLICABILITY_EVENTS_TABLE):
        if table not in filters:
            filters[table]=(f'event_id IN (SELECT event_id FROM {EVENTS_TABLE} WHERE nm_id IN ({marks}))',nms) if table==RECONCILIATION_TABLE else (f'nm_id IN ({marks})',nms)
    sid=sorted(e['source_key'].rsplit(':',1)[1] for e in scope['effects'] if e['kind']=='historical_wb_dispatch');sm=','.join('?' for _ in sid)
    for table,column in ((P+'wb_supplies','supply_id'),(P+'wb_supply_cost_layers','wb_supply_id'),(P+'wb_supply_transit_cost_enrichment','supply_id')):
        filters[table]=(f'{column} IN ({sm})',sid)
    from packages.application.fbs_accounting_rehearsal_sources import select_rehearsal_sources
    selected_sources=select_rehearsal_sources(conn,dates=dates,now=now,
        extra_ready_keys=[(snapshot['ready_target']['bundle_version'],snapshot['ready_target']['as_of_date'])])
    filters.update(selected_sources['filters'])
    require(shutil.disk_usage(destination.parent).free >= MAX_SOURCE_COPY_BYTES+RESERVE,'simulation_disk_budget')
    _admit(destination,MAX_SOURCE_COPY_BYTES)
    fd=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    schema=list(conn.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY rowid"))
    size=0;queries=list(selected_sources['selection_queries']);copy_queries=[];counts={};started=time.monotonic()
    with closing(sqlite3.connect(destination)) as dst:
        dst.execute('PRAGMA temp_store=MEMORY')
        for row in schema:
            if row['type']=='table':dst.execute(row['sql'])
        for row in schema:
            table=row['name']
            if row['type']!='table' or table not in allowed:continue
            where,args=filters.get(table,('1',()))
            sql=f'SELECT rowid __source_rowid__,* FROM "{table}" WHERE {where} ORDER BY rowid'
            columns=[r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
            cursor=conn.execute(sql,args);count=0
            for source in cursor:
                count+=1;size+=sum(len(v) if isinstance(v,bytes) else len(canonical(v).encode()) for v in source)
                _check_source_copy_budget(table=table,count=count,size=size,elapsed=time.monotonic()-started)
                dst.execute(f'INSERT INTO "{table}"(rowid,{",".join(chr(34)+c+chr(34) for c in columns)}) VALUES({",".join("?" for _ in range(len(columns)+1))})',
                    [source['__source_rowid__'],*[source[c] for c in columns]])
            queries.append((sql,args));copy_queries.append((sql,args));counts[table]=count
        for row in schema:
            if row['type'] in {'index','trigger','view'}:
                dst.execute(row['sql'])
                _check_source_copy_budget(table=row['name'],count=0,
                    size=dst.execute('PRAGMA page_count').fetchone()[0]*dst.execute('PRAGMA page_size').fetchone()[0],elapsed=time.monotonic()-started)
        dst.commit()
        require(dst.execute('PRAGMA quick_check').fetchall()==[('ok',)],'subset_integrity')
    return dict(query_fence=json.loads(canonical(ready.pin_queries(conn,queries))),
        copy_fence=json.loads(canonical(ready.pin_queries(conn,copy_queries))),rows=counts,bytes=size)


def _numeric(manifest):
    """Compare monetary outcomes, excluding random native upload identities."""
    candidate=manifest['candidate']
    result={day:[{k:r.get(k) for k in ('warehouse_key','nm_id','quantity','capital_rub','wac_rub','cost_covered_quantity','quality')}
                 for r in edition['balances']] for day,edition in sorted(candidate['editions'].items())}
    ready_values=[]
    for target in manifest['targets']:
        envelope=json.loads(target['after_json'])
        ready_values.append(dict(bundle_version=target['expected']['bundle_version'],as_of_date=target['expected']['as_of_date'],
            dates=target['dates'],sheets=[{k:sheet.get(k) for k in ('sheet_name','header','rows')}
                for sheet in envelope.get('sheets',[]) if sheet.get('sheet_name')!='STATUS']))
    return dict(stages=result,ready=ready_values,missing_quantity_dates=candidate['quality_proof']['missing_official_quantity_dates'],
                native_plan_digest=manifest['plan']['native_plan']['plan_digest'],fee_total=manifest['plan']['fee_scope']['total_with_vat_rub'])


def _pipeline(runtime, *, recipe, binding, workbooks, recapture, owner_token, now):
    with ready.readonly(runtime.db_path) as conn:
        row=conn.execute('SELECT inputs_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
            (recipe['publication_operation_id'],recipe['publication_attempt_id'])).fetchone()
    if row:
        manifest=typed.publication.expand_manifest(json.loads(row[0]))
    else:
        typed.apply_native_from_effect_intent(binding,runtime=runtime,recapture=recapture,owner_token=owner_token)
        fees=typed.apply_fees_from_effect_intent(binding,runtime=runtime,workbooks=workbooks,owner_token=owner_token)
        from packages.application.our_wb_costs import OurWbCostBlock
        OurWbCostBlock(runtime=runtime,timestamp_factory=lambda:now.isoformat().replace('+00:00','Z')).materialize_wb_supply_cost_layers(historical_fbo_effect_binding=binding)
        manifest=typed.prepare_publication(recipe['native_plan'],fee_scope=fees['fee_scope'],effect_binding=binding,
            runtime_dir=runtime.runtime_dir,owner_generation=recipe['owner_generation'],operation_id=recipe['publication_operation_id'],
            attempt_id=recipe['publication_attempt_id'],now=now)
    return manifest


def _recipe(runtime, *, operation_id, snapshot, inputs, now, numeric_preflight_digest=None, original_documents=None):
    value,physical,restored,fees,_=inputs
    result=typed.prepare_effect_intent(snapshot['native_plan'],capture=snapshot['capture'],scope=value['scope'],
        expected_native_guard=snapshot['native_guard'],fee_requests=fees,runtime_dir=runtime.runtime_dir,
        owner_generation='registered-fbs:'+operation_id,operation_id=operation_id,ready_target=snapshot['ready_target'],now=now)
    if numeric_preflight_digest is not None:
        # The existing immutable effect recipe owns this domain preflight too;
        # a continuation cannot replace its approved monetary forecast.
        result['numeric_preflight_digest']=numeric_preflight_digest
        result['original_documents']=deepcopy(original_documents)
        result['recipe_digest']=fingerprint({k:v for k,v in result.items() if k!='recipe_digest'})
    return result


def _recapture(runtime, inputs, *, source_fence=None, code_authority=None):
    value,physical,restored,_,_=inputs
    def capture(conn):
        if code_authority is not None:require(code_authority==_preflight_code(),'preflight_code_changed')
        # The native writer passes a RW transaction; capture uses no mutation.
        query_only=conn.execute('PRAGMA query_only').fetchone()[0]
        conn.execute('PRAGMA query_only=ON')
        try:
            if source_fence is not None:
                # The effect recipe itself is the one expected new row; all
                # monetary and derived-stage source rows remain prestate.
                ready.check_queries(conn,[(sql,tuple(args),digest) for sql,args,digest in source_fence
                    if '"'+P+'ready_publications"' not in sql])
            book,_=accounting.load(runtime.runtime_dir)
            return capture_native_sources(conn,book=book,scope=value['scope'],opening_date=value['opening_date'],physical=physical,restored=restored)
        finally:
            if not query_only:conn.execute('PRAGMA query_only=OFF')
    return capture


def _preflight(runtime, *, request, operation_id, directory, snapshot, inputs, now):
    code_authority=_preflight_code()
    require(not directory.exists(),'preflight_directory_exists')
    directory.mkdir(parents=True,mode=0o700)
    retained=_retain_inputs(directory,request,inputs)
    sandbox=directory/'simulation';sandbox.mkdir(mode=0o700)
    source_book=accounting.path(runtime.runtime_dir)
    before_book=_backup(source_book,directory/'accounting.before.sqlite3',budget=128*1024**2)
    _backup(Path(before_book['path']),accounting.path(sandbox),budget=128*1024**2)
    book,_=accounting.load(runtime.runtime_dir)
    with ready.readonly(runtime.db_path) as conn:
        subset=_subset(conn,directory/'sources.before.sqlite3',book=book,snapshot=snapshot,now=now)
    _backup(directory/'sources.before.sqlite3',sandbox/'registry_upload_runtime.sqlite3',budget=MAX_SOURCE_COPY_BYTES)
    sim=_runtime(sandbox)
    from packages.application.fbs_current_snapshot_policy import rehearsal_snapshot_policy
    with ready.readonly(runtime.db_path) as source, rehearsal_snapshot_policy(source,
            runtime_dir=sandbox,db_path=sim.db_path,source_fence=subset['query_fence'],copy_fence=subset['copy_fence']):
        fresh=_snapshot(sim,inputs)
        require(fresh['capture']==snapshot['capture'] and fresh['native_guard']==snapshot['native_guard'] and fresh['freshness_policy']==snapshot['freshness_policy'], 'simulation_source_changed')
        with warehouse_functional_job_lock(sandbox) as owner:
            recipe=_recipe(sim,operation_id=operation_id,snapshot=fresh,inputs=inputs,now=now)
            recorded=typed.record_effect_intent(recipe,runtime=sim,recapture=_recapture(sim,inputs),owner_token=owner['owner_token'])
            manifest=_pipeline(sim,recipe=recipe,binding=recorded['binding'],workbooks=inputs[-1],recapture=_recapture(sim,inputs),owner_token=owner['owner_token'],now=now)
            # Numeric preview also proves the actual native Ready publication path.
            typed.publication.publish(manifest,runtime_dir=sandbox)
    require(code_authority==_preflight_code(),'preflight_code_changed')
    result=dict(code_authority=code_authority,snapshot=snapshot,subset=subset,numeric=_numeric(manifest),observed_at=now.isoformat(),retained_files=retained,
        documentary_digest=fingerprint(inputs[0]),backup_book=before_book,
        backup_sources=_file_proof(directory/'sources.before.sqlite3',budget=MAX_SOURCE_COPY_BYTES),
        simulation_book=manifest['after_book'])
    result['digest']=fingerprint(result)
    _private(directory/'preflight.json',result)
    return result


class HistoricalFboAction:
    def preview(self, request, operation_id, *, root):
        if request['action']==RECOVER:
            require(operation_id!=request['recovery_ref'],'new_continuation_identity_required')
            original=_directory(root,request['recovery_ref']);saved=_read_json(original/'attempt.json')
            require(saved['recipe_digest']==request['original_recipe_digest'],'recovery_binding_changed')
            _check_preflight_code(_read_json(original/'preflight.json'))
            with ready.readonly(StoreRegistry(root).resolve('operational')) as conn:
                recipe=typed.read_effect_intent(conn,binding=saved['binding'])
            status=self.readback(saved['request'],request['recovery_ref'],root=root)
            candidate=dict(recovery_ref=request['recovery_ref'],recipe_digest=recipe['recipe_digest'],status=status)
            return dict(operation_id=operation_id,target=str(root),scope=dict(action=RECOVER,original=request['recovery_ref']),
                prestate_sha256=fingerprint(status),candidate_sha256=fingerprint(candidate),prepared=candidate,
                recovery=dict(kind='same_native_recipe_no_resend',binding=saved['binding']))
        inputs=_inputs(request);runtime=_runtime(root);directory=_directory(root,operation_id)
        with _held(root,request) as held:
            snapshot=_snapshot(runtime,inputs)
            if (directory/'preflight.json').exists():
                preflight=_read_json(directory/'preflight.json')
                require(preflight['digest']==fingerprint({k:v for k,v in preflight.items() if k!='digest'}),'preflight_corrupt')
                _check_preflight_code(preflight)
                require(preflight['snapshot']==snapshot,'preflight_sources_changed')
            else:
                now=datetime.now(timezone.utc)
                preflight=_preflight(runtime,request=request,operation_id=operation_id,directory=directory,
                    snapshot=snapshot,inputs=inputs,now=now)
            require(0 <= (datetime.now(timezone.utc)-datetime.fromisoformat(preflight['observed_at'])).total_seconds() <= 600,
                'preflight_expired')
            with ready.readonly(runtime.db_path) as conn:
                ready.check_queries(conn,[(sql,tuple(args),digest) for sql,args,digest in preflight['subset']['query_fence']])
        return dict(operation_id=operation_id,target=str(runtime.db_path),scope=dict(action=RESTORE,supplies=[d['supply_id'] for d in inputs[0]['scope']['dispatches']],full_numeric_preflight=True),
            prestate_sha256=fingerprint(snapshot),candidate_sha256=preflight['digest'],prepared=preflight,
            recovery=dict(kind='immutable_recipe_and_coherent_backup',path=str(directory),held=held))

    def apply(self, request, operation_id, preview, *, root):
        continuation=request['action']==RECOVER
        original=request['recovery_ref'] if continuation else operation_id
        directory=_directory(root,original);runtime=_runtime(root)
        # Direct domain callers receive the same expiry/source/hash gate as the
        # registered launcher's preview; no pasted preview can authorize money.
        fresh=self.preview(request,operation_id,root=root)
        require(all(fresh[k]==preview[k] for k in ('prestate_sha256','candidate_sha256')),'preview_compare_failed')
        with _held(root,request), warehouse_functional_job_lock(root) as owner:
            preflight=_read_json(directory/'preflight.json')
            require(preflight['digest']==fingerprint({k:v for k,v in preflight.items() if k!='digest'}),'preflight_corrupt')
            _check_preflight_code(preflight)
            for key,budget in (('backup_book',128*1024**2),('backup_sources',MAX_SOURCE_COPY_BYTES)):
                require(_file_proof(Path(preflight[key]['path']),budget=budget)==preflight[key],'before_image_changed')
            if continuation:
                saved=_read_json(directory/'attempt.json')
                require(saved['recipe_digest']==request['original_recipe_digest'],'recovery_binding_changed')
                require(self.preview(request,operation_id,root=root)['candidate_sha256']==preview['candidate_sha256'],'recovery_state_changed')
                inputs=_inputs(saved['request']);now=datetime.fromisoformat(saved['observed_at'])
                with ready.readonly(runtime.db_path) as conn:recipe=typed.read_effect_intent(conn,binding=saved['binding'])
                require(recipe.get('numeric_preflight_digest')==preflight['digest'],'recovery_preflight_changed')
                invocation=_directory(root,operation_id);invocation.mkdir(mode=0o700,parents=True,exist_ok=False)
                _private(invocation/'continuation.json',dict(original=original,recipe_digest=recipe['recipe_digest']))
            else:
                inputs=_inputs(request);now=datetime.fromisoformat(preflight['observed_at'])
                require(preflight['digest']==preview['candidate_sha256'],'candidate_changed')
                require(_snapshot(runtime,inputs)==preflight['snapshot'],'prestate_changed')
                with ready.readonly(runtime.db_path) as conn:
                    ready.check_queries(conn,[(sql,tuple(args),digest) for sql,args,digest in preflight['subset']['query_fence']])
                invoice_by_supply={f['wb_supply_id']:f for f in inputs[0]['fees']}
                documents=[]
                for ref in preflight['snapshot']['original_invoice_refs']:
                    fee=invoice_by_supply[ref['wb_supply_id']]
                    original=Path(fee['original_invoice']['path']).resolve()
                    documents.append({**ref,'invoice_artifact_path':preflight['retained_files'][str(original)],
                        'xlsx_sha256':fee['file_sha256'],'xlsx_request_scope':fee['request_scope'],'xlsx_request_id':fee['request_id']})
                recipe=_recipe(runtime,operation_id=operation_id,snapshot=preflight['snapshot'],inputs=inputs,now=now,
                    numeric_preflight_digest=preflight['digest'],original_documents=documents)
                saved=dict(request={**deepcopy(request),'retained_files':preflight['retained_files']},observed_at=now.isoformat(),recipe_digest=recipe['recipe_digest'],
                    binding={k:recipe[k] for k in ('operation_id','attempt_id','recipe_digest')},
                    backup=dict(accounting=preflight['backup_book'],sources=preflight['backup_sources']))
                _private(directory/'attempt.json',saved)
            try:
                preflight=_read_json(directory/'preflight.json')
                recapture=_recapture(runtime,inputs,source_fence=preflight['subset']['query_fence'],code_authority=preflight['code_authority'])
                if not continuation:
                    typed.record_effect_intent(recipe,runtime=runtime,recapture=recapture,owner_token=owner['owner_token'])
                manifest=_pipeline(runtime,recipe=recipe,binding=saved['binding'],workbooks=inputs[-1],
                    recapture=recapture,owner_token=owner['owner_token'],now=now)
                require(_numeric(manifest)==preflight['numeric'],'postsource_numeric_preflight_changed')
                typed.publication.publish(manifest,runtime_dir=root)
            except Exception:
                # Any already-durable phases stay owned by the exact intent.
                raise AmbiguousSubmit('historical_fbo_same_recipe_readback_required') from None
        raise AmbiguousSubmit('historical_fbo_ready_published_pending_owned_cycle')

    def readback(self, request, operation_id, *, root):
        continuation=request['action']==RECOVER
        invocation=_directory(root,operation_id)
        if continuation:
            if not (invocation/'continuation.json').exists():return dict(operation_id=operation_id,state='not_submitted')
            link=_read_json(invocation/'continuation.json')
            require(link==dict(original=request['recovery_ref'],recipe_digest=request['original_recipe_digest']),'continuation_conflict')
            original=request['recovery_ref']
        else:original=operation_id
        directory=_directory(root,original)
        if not (directory/'attempt.json').exists():return dict(operation_id=operation_id,state='not_submitted')
        saved=_read_json(directory/'attempt.json')
        try:
            db=StoreRegistry(root).resolve('operational')
            with ready.readonly(db) as conn:
                recipe=typed.read_effect_intent(conn,binding=saved['binding'])
                present=native._verify_existing(conn,recipe['native_plan'])
                row=conn.execute('SELECT state,book_version,diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
                    (recipe['operation_id'],recipe['attempt_id'])).fetchone()
                child=conn.execute('SELECT state FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
                    (recipe['publication_operation_id'],recipe['publication_attempt_id'])).fetchone()
                fees=None
                if present:
                    try:fees=typed.capture_fee_scope(conn,native_plan=recipe['native_plan'],fee_requests=recipe['fee_requests'])
                    except (ValueError,sqlite3.Error):pass
                complete=row['state']=='complete'
                attempts=json.loads(row['diagnostics_json']).get('historical_fbo_fee_attempts',{})
                if complete:
                    require(present and fees is not None and child and child['state']=='complete','completion_sources_missing')
            return dict(operation_id=operation_id,state='applied' if complete else 'ambiguous',
                original_operation_id=original,native_complete=present,fee_sources_complete=fees is not None,
                ready_complete=bool(child and child['state']=='complete'),scope_complete=complete,
                recipe_digest=recipe['recipe_digest'],recovery_ref=original,
                next_step='none' if complete else 'owned_cycle_after_ready_or_explicit_bound_recovery',
                fee_attempts=list(attempts.values()),book_version=row['book_version'])
        except (ValueError,sqlite3.Error,KeyError):
            return dict(operation_id=operation_id,state='ambiguous',original_operation_id=original,scope_complete=False,
                recipe_digest=saved['recipe_digest'],next_step='same_recipe_manual_readonly_diagnosis_no_resend')
