"""Registered History-only completion of an already published receipt.

No collector, warehouse run, posting, Finance apply or cycle resume is imported.
The original cycle stays untouched. A durable, one-submit tail links its actual
History acknowledgment and read-only Finance proof to the original publication.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import fcntl
import gc
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from apps.production_apply_contract import AmbiguousSubmit
from packages.application import fbs_accounting_runtime as accounting
from packages.application import ready_publication as ready
from packages.application.fbs_accounting_historical_stages import require
from packages.application.fbs_snapshot_cost import canonical, fingerprint
from packages.application.owned_history_worker_capability import file_digest, read_json, process_generation

OPERATION = 'historical_receipt_completion'
CONTRACT = 'historical_receipt_completion_v1'
DIRECTORY = 'historical-receipt-completions'
_HELD = ContextVar('historical_receipt_completion_held',default=None)
STAGES = ('closed_sources','api_sources','finance_sources','fbs_generation','warehouse',
          'daily_projection','closed_ready','final_ready','rolling14')


def _path(root, operation_id):
    require(re.fullmatch(r'[a-z0-9][a-z0-9._-]{7,127}',operation_id) is not None,'tail_operation_invalid')
    return Path(root)/DIRECTORY/(operation_id+'.json')


def _claim_path(root,request):
    scope=[request['publication_operation_id'],request['publication_attempt_id']]
    return Path(root)/DIRECTORY/'claims'/(fingerprint(scope)[7:]+'.json')


def _unclaimed(root,request):
    require(not _claim_path(root,request).exists(),'tail_publication_already_submitted_readback_required')


def _exclusive_private(path,value):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    require(not path.parent.is_symlink() and not path.parent.stat().st_mode&0o077,'tail_private_directory_required')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:
        stream.write(canonical(value));stream.flush();os.fsync(stream.fileno())
    directory_fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(directory_fd)
    finally:os.close(directory_fd)


def _sha(path, expected):
    require('sha256:'+file_digest(path,limit=2*1024**2)==expected,'tail_file_changed')
    result=read_json(path,limit=2*1024**2)
    require('sha256:'+file_digest(path,limit=2*1024**2)==expected,'tail_file_changed')
    return result


def _held_binding(root, request):
    from packages.application import business_data_maintenance_pause as pause
    from packages.application.business_data_write_barrier import barrier_status
    state=pause.load_state(root);barrier=barrier_status(root)
    require(state and state['phase']=='held' and state['window_id']==request['window_id']
        and state['baseline_fingerprint']==request['expected_baseline_fingerprint']
        and barrier.get('active') is True and barrier.get('phase')=='held' and barrier.get('hold_confirmed') is True
        and barrier.get('window_kind')=='maintenance_pause' and barrier.get('window_id')==state['window_id']
        and barrier.get('plan_fingerprint')==state['baseline_fingerprint'],'tail_held_pause_required')
    return dict(window_id=state['window_id'],baseline_fingerprint=state['baseline_fingerprint'],
                pause_sha256='sha256:'+file_digest(root/pause.STATE_FILENAME,limit=2*1024**2),
                barrier_fingerprint=barrier['state_fingerprint'])


def check_live_owner(root, owner, *, parent_pid=None):
    """Authenticate the real tail marker; never convert it into cycle authority."""
    root=Path(root).resolve();pid=os.getpid() if parent_pid is None else parent_pid
    require(isinstance(owner,dict) and owner.get('contract')==CONTRACT and owner.get('operation')==OPERATION
        and owner.get('pid')==pid and owner.get('identity')==process_generation(pid),'tail_owner_invalid')
    saved=read_json(_path(root,owner['operation_id']),limit=2*1024**2)
    require(saved.get('phase')=='submitted' and saved.get('owner')==owner,'tail_owner_marker_changed')
    require(_held_binding(root,saved['request'])==owner['held'],'tail_maintenance_changed')
    return saved


@contextmanager
def _held(root,request):
    # These are the native resume/barrier mutexes. Retaining both prevents a
    # concurrent pause release; the HTTP barrier remains held throughout.
    from apps.business_data_maintenance import _ExclusiveRestoreLock
    from packages.application.business_data_write_barrier import _BarrierLock
    with _ExclusiveRestoreLock(root),_BarrierLock(root):
        from apps.business_data_maintenance import RuntimeScheduleClient,SystemdClient,_read_env_file,_build_web_auth_cookie
        from packages.application import business_data_formula_resume as formula
        env=_read_env_file(Path(request['maintenance_env_file']))
        base=env.get('BUSINESS_DATA_MAINTENANCE_BASE_URL') or f"http://{env.get('REGISTRY_UPLOAD_HTTP_HOST','127.0.0.1')}:{env.get('REGISTRY_UPLOAD_HTTP_PORT','8765')}"
        client=RuntimeScheduleClient(base_url=base,cookie=_build_web_auth_cookie(env))
        plan=formula.preview(root,operation_id=request['formula_transition_operation_id'],window_id=request['window_id'],
            expected_sha=request['runtime_sha'],app_dir=root.parent/'app',unit_directory=Path(request.get('unit_directory','/etc/systemd/system')),
            systemd=SystemdClient(),activity_reader=lambda:client._request('/v1/business-data-maintenance/activity'))
        require(plan['fingerprint']==request['formula_transition_plan_fingerprint']
            and plan['delta']['old_epoch']==request['source_formula_epoch'] and plan['delta']['new_epoch']==request['formula_epoch'],
            'tail_formula_transition_changed')
        before=_held_binding(root,request)
        token=_HELD.set((os.getpid(),Path(root).resolve(),request,before))
        try: yield before
        finally:
            _HELD.reset(token)
            require(_held_binding(root,request)==before,'tail_maintenance_changed')


def require_held_completion(root):
    """Only a live authenticated held scope may admit the registered tail."""
    value=_HELD.get()
    require(value and value[0]==os.getpid() and value[1]==Path(root).resolve(), 'tail_held_scope_required')
    require(_held_binding(Path(root),value[2])==value[3], 'tail_maintenance_changed')
    return value[3]


@contextmanager
def _admitted(root):
    from packages.application.business_data_procedure_admission import AdmissionLease
    lease=AdmissionLease.held_historical_receipt_completion(root)
    try:
        require(lease.fd is not None,'tail_admission_missing')
        with lease.entered():yield
    finally:lease.close()


def _publication_pin(conn, operation_id, attempt_id):
    row=conn.execute('SELECT rowid,kind,state,book_version,length(CAST(inputs_json AS BLOB)) FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',(operation_id,attempt_id)).fetchone()
    require(row is not None,'tail_publication_missing')
    hashed=hashlib.sha256()
    with conn.blobopen('sheet_vitrina_v1_ready_publications','inputs_json',row[0],readonly=True) as blob:
        require(len(blob)<=1024**3,'tail_publication_size')
        while chunk:=blob.read(64*1024):hashed.update(chunk)
    return dict(kind=row[1],state=row[2],book_version=row[3],inputs_sha256='sha256:'+hashed.hexdigest(),inputs_bytes=row[4])


def _config(request):
    from packages.application.sheet_vitrina_v1_cycle import CycleHistoryConfig
    source=_sha(Path(request['source_runtime_contract']),request['source_runtime_contract_sha256'])
    target=_sha(Path(request['runtime_contract']),request['runtime_contract_sha256'])
    require(source['formula_epoch']==request['source_formula_epoch'] and target['formula_epoch']==request['formula_epoch'],
            'tail_formula_transition_changed')
    require(target['candidate_root']==request['candidate_root'],'tail_candidate_root_changed')
    config=CycleHistoryConfig(Path(request['candidate_root']),Path(request['runtime_contract']),request['formula_epoch'])
    # Native deployed-code/runtime contract gate, same as owned History.
    from apps.web_vitrina_history_candidate_build import runtime_storage_admission
    runtime_storage_admission(config.candidate_root,config.runtime_contract,config.formula_epoch)
    return config


def _cycle(root,request):
    from packages.application.web_vitrina_snapshot_admission import process_identity
    value=_sha(root/'sheet-vitrina-cycles'/(request['original_cycle_id']+'.json'),request['original_cycle_sha256'])
    require(value['cycle_id']==request['original_cycle_id'] and process_identity(value['owner_pid'])!=value['process_identity'],
            'tail_original_cycle_owner_live')
    source_contract=Path(request['source_runtime_contract'])
    _sha(source_contract,request['source_runtime_contract_sha256'])
    source_config=dict(candidate_root=str(Path(request['candidate_root'])),
        runtime_contract=str(Path(request.get('source_runtime_contract_original_path',request['runtime_contract']))),
        formula_epoch=request['source_formula_epoch'],budget_seconds=request.get('source_history_budget_seconds',180),
        max_recomputes=request.get('source_history_max_recomputes',31),contract_digest=ready.digest(source_contract.read_text()))
    require(ready.digest(canonical(source_config))==value['history_config_fingerprint'],'tail_original_history_config_changed')
    stages=value['stages']
    require(tuple(s['stage'] for s in stages)==STAGES
        and all(s['status'] in {'complete','degraded'} and s.get('finished_at') and s.get('versions') for s in stages[:8])
        and stages[-1]['status'] in {'running','interrupted','failed'},'tail_original_prefix_unproven')
    from packages.business_time import current_business_date_iso
    require(value['business_date']==current_business_date_iso(datetime.now(timezone.utc)),'tail_cycle_business_date_changed')
    return value


def _verify_original_publication(runtime,conn,*,manifest,current_book,request):
    """Native original-stage proof with a streaming exact-byte receipt check.

    HistoricalReceipt already decoded and authenticated this very immutable
    manifest. The reviewed byte pin replaces only the redundant second JSON
    decode/equality; every original stage/model/companion/daily proof remains.
    """
    from decimal import Decimal,localcontext
    from packages.application import fbs_temporary_allocation_action as native
    stages=native.stages
    ready.check_pinned_authority(conn,manifest['authority'])
    pin=_publication_pin(conn,manifest['operation_id'],manifest['attempt_id'])
    row=conn.execute('SELECT kind,state,book_version,book_operation_id,expected_book,after_digest FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
        (manifest['operation_id'],manifest['attempt_id'])).fetchone()
    require(pin['inputs_sha256']==request['publication_inputs_sha256'] and row and row['kind']==native.CONTRACT
        and row['state']=='complete' and row['book_version']==manifest['after_book']
        and row['book_operation_id']==manifest['book_operation_id'] and row['expected_book']==manifest['expected_book']
        and row['after_digest']==ready.digest(manifest['targets'][-1]['after_json']),'tail_original_publication_changed')
    source_proof=native.validate_source_receipt(conn,manifest=manifest)
    for edition in manifest['candidate']['editions'].values():
        actual=conn.execute(f'SELECT * FROM {stages.P}functional_versions WHERE version_id=?',(edition['version_id'],)).fetchone()
        require(actual and dict(actual)==edition['version'],'original_stage_version_changed')
        snapshot=conn.execute(f'SELECT * FROM {stages.P}wb_snapshots WHERE version_id=?',(edition['version_id'],)).fetchone()
        require(snapshot and dict(snapshot)==edition['snapshot'],'original_stage_snapshot_changed')
        balances=stages._version_balances(conn,version_id=edition['version_id'])
        require(balances==edition['balances'],'original_stage_balances_changed')
        models=conn.execute(f'SELECT warehouse_key,payload_json FROM {stages.P}functional_read_models WHERE version_id=?',(edition['version_id'],)).fetchall()
        require({row[0] for row in models}==set(stages.STAGES.values()),'original_stage_models_missing')
        with localcontext() as context:
            context.prec=28
            for key,serialized in models:
                value=json.loads(serialized);expected=[r for r in balances if r['warehouse_key']==key]
                require(value.get('status')=='ready'
                    and [(r['nm_id'],r['quantity'],r['capital_rub']) for r in value.get('balances',[])]==[(r['nm_id'],r['quantity'],r['capital_rub']) for r in expected]
                    and Decimal(value['warehouse']['total_quantity'])==sum((Decimal(r['quantity']) for r in expected),Decimal(0))
                    and Decimal(value['warehouse']['total_capital_rub'])==sum((Decimal(r['capital_rub']) for r in expected),Decimal(0)),
                    'original_stage_models_changed')
        for table,old_rows in edition['companions'].items():
            expected=[]
            for old in old_rows:
                value={**old,'version_id':edition['version_id']}
                if table=='unmatched_doprinato':value['unmatched_id']='historical-unmatched:'+fingerprint([edition['version_id'],old['unmatched_id']])[7:]
                expected.append(value)
            actual=[dict(row) for row in conn.execute(f'SELECT * FROM {stages.P}{table} WHERE version_id=?',(edition['version_id'],))]
            require(sorted(actual,key=canonical)==sorted(expected,key=canonical),'original_stage_companions_changed')
    native._verify_closed_daily_projection(conn,manifest=manifest,current_book=current_book)
    return source_proof


def _preservation(conn,request):
    from packages.application.operator_ff_overhead import TABLE
    from packages.application.ff_pool_documents import REQUESTS_TABLE
    identities=request['preserved_confirmation_request_ids']
    require(isinstance(identities,list) and 0<len(identities)<=1000 and identities==sorted(set(identities)), 'tail_preservation_scope_invalid')
    proofs={}
    for identity in identities:
        confirmation=conn.execute(f'SELECT state FROM {TABLE} WHERE request_id=?',(identity,)).fetchone()
        native=conn.execute(f'SELECT state FROM {REQUESTS_TABLE} WHERE request_id=?',(identity,)).fetchone()
        require(confirmation and confirmation[0]=='completed' and native and native[0]=='complete','tail_completed_confirmation_missing')
        proofs[identity]=fingerprint(ready.pin_queries(conn,[(f'SELECT * FROM {TABLE} WHERE request_id=?',(identity,)),
            (f'SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?',(identity,))]))
    return proofs


def _predecessors(runtime,request,cycle):
    """Same native predecessor reads; no entrypoint, source fetch or cycle run."""
    from types import SimpleNamespace
    from packages.application.wb_finance_daily import WbFinanceDailyBlock
    from packages.application.sheet_vitrina_v1_cycle import finance_raw_proof
    from packages.application.sheet_vitrina_v1_closed_backlog import ClosedBacklog
    block=WbFinanceDailyBlock(runtime.runtime_dir,seller_id=request['seller_id'])
    source=finance_raw_proof(SimpleNamespace(runtime=runtime,wb_finance_daily_block=block,wb_finance_weekly_block=block))
    versions=cycle['final_versions'];expected=cycle['stages'][2]['versions']
    require(all(source[key]==expected[key] for key in source),'tail_finance_predecessor_changed')
    days=block.build_daily_payload()['days']
    require(ready.digest(canonical(days))==versions['daily_projection_digest'],'tail_daily_projection_changed')
    with ready.readonly(runtime.db_path) as conn:
        active=conn.execute('SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1').fetchone()
    require(active and active[0]==versions['functional_version'] and accounting.load(runtime.runtime_dir)[1]==versions['fbs_book'],
        'tail_material_predecessor_changed')
    saved=cycle['stages'][7]['versions']
    plan=runtime.load_sheet_vitrina_ready_snapshot(as_of_date=saved['as_of_date'])
    require(plan and plan.snapshot_id==saved['snapshot_id'] and plan.plan_version==saved['plan_version']
        and runtime.load_current_state().bundle_version==saved['bundle_version'],'tail_ready_predecessor_changed')
    current=runtime.prepare_sheet_vitrina_ready_publication(bundle_version=saved['bundle_version'],as_of_date=saved['as_of_date'])
    require(current.exists and current.fingerprint==saved['ready_fingerprint'],'tail_ready_predecessor_changed')
    publication=ready.publication_status(runtime.db_path,operation_id=saved['publication_operation_id'],attempt_id=saved['publication_attempt_id'])
    with ready.readonly(runtime.db_path) as conn:
        refreshed=conn.execute(f'SELECT refreshed_at FROM {ready.TABLE} WHERE bundle_version=? AND as_of_date=?',(saved['bundle_version'],saved['as_of_date'])).fetchone()
    require(refreshed and refreshed[0]==saved['refreshed_at'] and publication and publication['state']=='complete'
        and publication['after_digest']==ready.digest(current.plan_json),'tail_ready_publication_changed')
    closed=ClosedBacklog(SimpleNamespace(block=SimpleNamespace(runtime=runtime)))
    status=closed.status();closed_versions=cycle['stages'][6]['versions']
    require(ready.digest(canonical(status))==closed_versions['closed_receipt_digest'],'tail_closed_receipt_changed')
    dates=tuple(json.loads(closed_versions['closed_backfill_dates']))
    for day in dates:
        require(status and closed._ready_proof(status,day)==status['dates'][day].get('ready'),'tail_closed_ready_changed')
    return dict(finance_sources=source,functional_version=active[0],daily_projection_digest=versions['daily_projection_digest'],
        final_ready=saved,closed_receipt_digest=closed_versions['closed_receipt_digest'],closed_backfill_dates=list(dates))


def _snapshot(runtime,request,*,now):
    from packages.application.fbs_accounting_historical_history import HistoricalReceipt
    from packages.application import fbs_temporary_allocation_action as native
    from packages.application.web_vitrina_history_store import HistoryStore
    config=_config(request);cycle=_cycle(runtime.runtime_dir,request)
    predecessors=_predecessors(runtime,request,cycle)
    economics=accounting.current_publication_receipt(runtime,now=now)
    require(economics and economics['accounting_version']==request['expected_book_version'],'tail_current_economics_unproven')
    receipt=HistoricalReceipt(runtime,request['publication_operation_id'],request['publication_attempt_id']).freeze_scope(now)
    sources=receipt.validate_sources_readonly(now=now)
    require(set(predecessors['closed_backfill_dates'])<=set(receipt.dates),'tail_foreign_closed_scope_refused')
    manifest=receipt.manifest
    require(manifest['contract']==native.CONTRACT and manifest['adapter_code_authority']==native._code(), 'tail_native_contract_changed')
    book,version,books=native._completion_books(runtime,manifest=manifest)
    require(version==request['expected_book_version'],'tail_book_changed')
    with ready.readonly(runtime.db_path) as conn:
        proof=_verify_original_publication(runtime,conn,manifest=manifest,current_book=book,request=request)
        preserved=_preservation(conn,request)
        binding=manifest['plan']['effect_binding']
        parent=_publication_pin(conn,binding['operation_id'],binding['attempt_id'])
        child=_publication_pin(conn,receipt.operation_id,receipt.attempt_id)
        require(parent['kind']==native.EFFECT_CONTRACT and parent['state']=='prepared'
            and parent['inputs_sha256']==request['parent_inputs_sha256']
            and child['inputs_sha256']==request['publication_inputs_sha256'],'tail_original_intent_changed')
        # Native source-revision fence covers protected material; no target
        # publication diagnostics or History files participate in this fence.
        from packages.application.fbs_accounting_historical_revision_writer import SOURCE_TABLES
        material=ready.capture_material(conn,tables=SOURCE_TABLES)
    del book;gc.collect()
    store=HistoryStore(config.candidate_root/'history');pointer=store._current()
    result=dict(binding=receipt.binding(),sources=sources,predecessors=predecessors,preserved_confirmations=preserved,parent_binding=binding,parent=parent,child=child,
        book_version=version,completion_books=books,economics=economics,native_source_receipt=proof,material=material,
        original_cycle_sha256=request['original_cycle_sha256'],original_cycle_id=cycle['cycle_id'],
        source_history_current=pointer,source_formula_epoch=request['source_formula_epoch'],target_formula_epoch=config.formula_epoch,
        target_runtime_contract_sha256=request['runtime_contract_sha256'])
    # Do not retain manifest/book images while the supervised child runs.
    del manifest,receipt;gc.collect()
    return result


def _finance(runtime,request):
    from packages.application.finance_cost_completion_readonly import validate_finance_completion_readonly
    return validate_finance_completion_readonly(runtime.runtime_dir,expected_book_version=request['expected_book_version'],
        expected_storage_manifest_sha256=request['storage_manifest_sha256'],expected_weeks=[tuple(w) for w in request['expected_weeks']],seller_id=request['seller_id'])


def _finalize(runtime,request,*,config,finance,now,expected):
    """The original native metadata finalizer, with explicit read-only Finance."""
    from packages.application.business_data_heavy_admission import require_heavy_owner
    from packages.application.warehouse_functional_lock import require_warehouse_job_owner,warehouse_functional_write_lock
    from packages.application.fbs_accounting_historical_history import HistoricalReceipt
    from packages.application import fbs_temporary_allocation_action as native
    from packages.application import fbs_accounting_historical_sqlite as sql
    from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter
    from packages.application.web_vitrina_history_store import HistoryStore
    from packages.business_time import current_business_date_iso
    require(require_heavy_owner(runtime.runtime_dir).operation==OPERATION,'tail_owner_required')
    require_warehouse_job_owner(runtime.runtime_dir)
    require(finance.get('read_only_completion_proof') is True and finance.get('runtime_mutation') is False
        and finance.get('recalculated_week_count')==0 and finance.get('status')=='already_current'
        and finance.get('non_target_preserved') is True and finance.get('post_verify_stale_week_count')==0
        and finance.get('accounting_version_unchanged') is True
        and finance.get('accounting_version_before')==finance.get('accounting_version')==request['expected_book_version']
        and finance.get('source_advanced_after_apply') is False,'tail_readonly_finance_unproven')
    economics=accounting.current_publication_receipt(runtime,now=now)
    require(economics==expected['economics'],'tail_economics_changed')
    receipt=HistoricalReceipt(runtime,request['publication_operation_id'],request['publication_attempt_id']).freeze_scope(now)
    require(receipt.binding()==expected['binding'] and receipt.validate_sources_readonly(now=now)==expected['sources'],'tail_source_changed')
    # Native object verification after child reaping and before completion.
    today=current_business_date_iso(now);first=min(receipt.dates[0],(datetime.fromisoformat(today)-timedelta(days=13)).date().isoformat())
    adapter=LiveNativeAdapter(db_path=runtime.db_path,runtime_dir=runtime.runtime_dir,cache_dir=config.candidate_root/'proofs',
        now=now,date_from=first,date_to=today,formula_epoch=config.formula_epoch)
    observed=receipt.validate_native_readonly(adapter=adapter,store=HistoryStore(config.candidate_root/'history'),now=now)
    del adapter;gc.collect()
    manifest,diagnostics=receipt._read();ack=diagnostics.get('historical_history_ack')
    require(ack and ack['manifest_digest']==manifest['manifest_digest'] and ack['native']==observed['native'],'tail_history_ack_unproven')
    require(manifest['adapter_code_authority']==native._code(),'tail_native_code_changed')
    with warehouse_functional_write_lock(runtime.runtime_dir):
        book,version,books=native._completion_books(runtime,manifest=manifest)
        require(version==request['expected_book_version'] and books==expected['completion_books'],'tail_completion_book_changed')
        with ready.readonly(runtime.db_path) as conn:
            _verify_original_publication(runtime,conn,manifest=manifest,current_book=book,request=request)
            require(_publication_pin(conn,manifest['plan']['effect_binding']['operation_id'],manifest['plan']['effect_binding']['attempt_id'])==expected['parent'],'tail_parent_changed')
            ready.check_material(conn,expected['material'])
            require(_preservation(conn,request)==expected['preserved_confirmations'],'tail_confirmation_preservation_changed')
        del book;gc.collect()
        with sql.connection(runtime.db_path,phase_name='history_only_completion_connection') as conn:
            conn.row_factory=__import__('sqlite3').Row
            with sql.transaction(conn,db_path=runtime.db_path,phase_name='history_only_completion'):
                require(accounting.load(runtime.runtime_dir)[1]==version,'tail_book_changed')
                proof=native.validate_source_receipt(conn,manifest=manifest)
                row=conn.execute('SELECT diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=? AND state=\'complete\'',(receipt.operation_id,receipt.attempt_id)).fetchone()
                actual=json.loads(row[0]);require(actual.get('historical_history_ack')==ack,'tail_ack_changed')
                actual.update(temporary_allocation_scope_complete=True,temporary_allocation_finance=finance,
                    temporary_allocation_economics=economics,temporary_allocation_quality_proof=manifest['candidate']['quality_proof'],
                    temporary_allocation_completion_books=books,historical_receipt_completion_operation=expected['tail_operation_id'])
                require(conn.execute('UPDATE sheet_vitrina_v1_ready_publications SET diagnostics_json=? WHERE operation_id=? AND attempt_id=? AND diagnostics_json=?',
                    (canonical(actual),receipt.operation_id,receipt.attempt_id,row[0])).rowcount==1,'tail_ack_cas_failed')
                binding=manifest['plan']['effect_binding'];native._read_recipe(conn,binding)
                completion=dict(child_operation_id=receipt.operation_id,child_attempt_id=receipt.attempt_id,
                    manifest_digest=manifest['manifest_digest'],history_ack=ack,finance=finance,economics=economics,
                    historical_ready_digest=ready.digest(manifest['targets'][-1]['after_json']),current_ready_digest=economics['ready_digest'],
                    quality_proof=manifest['candidate']['quality_proof'],history_only_tail=expected['tail_operation_id'],
                    original_cycle_id=request['original_cycle_id'],original_cycle_replayed=False,**proof,**books)
                ready.complete_publication(conn,operation_id=binding['operation_id'],attempt_id=binding['attempt_id'],book_version=manifest['after_book'],
                    after_digest=ready.digest(manifest['targets'][-1]['after_json']),finished_at=now.isoformat())
                conn.execute('UPDATE sheet_vitrina_v1_ready_publications SET diagnostics_json=? WHERE operation_id=? AND attempt_id=?',
                    (canonical(dict(temporary_allocation_completion=completion)),binding['operation_id'],binding['attempt_id']))
    return completion

def _readback_native(runtime,request,attempt,ack):
    from packages.application.fbs_accounting_historical_history import HistoricalReceipt
    from packages.application.web_vitrina_history_live_adapter import LiveNativeAdapter
    from packages.application.web_vitrina_history_store import HistoryStore
    from packages.business_time import current_business_date_iso
    # This is a dated completion readback. Retain the submitted anchor clock;
    # observing it tomorrow never adopts a new rolling window or reruns work.
    now=datetime.fromisoformat(attempt['submitted_at']);expected=attempt['prestate'];config=_config(request)
    receipt=HistoricalReceipt(runtime,request['publication_operation_id'],request['publication_attempt_id']).freeze_scope(now)
    require(receipt.binding()==expected['binding'] and receipt.validate_sources_readonly(now=now)==expected['sources'],'tail_readback_source_changed')
    today=current_business_date_iso(now);first=min(receipt.dates[0],(datetime.fromisoformat(today)-timedelta(days=13)).date().isoformat())
    adapter=LiveNativeAdapter(db_path=runtime.db_path,runtime_dir=runtime.runtime_dir,cache_dir=config.candidate_root/'proofs',
        now=now,date_from=first,date_to=today,formula_epoch=config.formula_epoch)
    observed=receipt.validate_native_readonly(adapter=adapter,store=HistoryStore(config.candidate_root/'history'),now=now)
    require(ack and ack.get('manifest_digest')==expected['binding']['digest'] and ack.get('native')==observed['native'],'tail_readback_history_changed')
    del receipt,adapter;gc.collect()
    finance=_finance(runtime,request)
    require(finance.get('read_only_completion_proof') is True and finance.get('runtime_mutation') is False
        and finance.get('recalculated_week_count')==0,'tail_readback_finance_changed')
    return finance


class HistoricalReceiptCompletionAdapter:
    def preview(self,request,operation_id):
        from packages.application.fbs_accounting_apply import target
        from packages.application.fbs_historical_fbo_action import _runtime
        root=target(request)
        require(not _path(root,operation_id).exists(),'tail_already_submitted_readback_required')
        _unclaimed(root,request)
        with _held(root,request):
            snapshot=_snapshot(_runtime(root),request,now=datetime.now(timezone.utc))
        return dict(operation_id=operation_id,target=str(root),scope=dict(action=OPERATION,publication=snapshot['binding'],
            original_cycle_id=request['original_cycle_id'],full_cycle=False,finance_write=False,source_formula_epoch=request['source_formula_epoch'],
            target_formula_epoch=request['formula_epoch']),prestate_sha256=fingerprint(snapshot),candidate_sha256=fingerprint(dict(request=request,snapshot=snapshot)),
            prepared=snapshot,recovery=dict(kind='same_tail_readback_only',operation_id=operation_id,original_cycle_untouched=True))

    def apply(self,request,operation_id,preview):
        from packages.application.fbs_accounting_apply import target
        from packages.application.fbs_historical_fbo_action import _runtime,_private
        from packages.application.business_data_heavy_admission import heavy_admitted
        from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
        from packages.application.owned_history_worker import historical_receipt_history_worker
        from packages.application.fbs_accounting_historical_history import HistoricalReceipt
        root=target(request);runtime=_runtime(root);path=_path(root,operation_id)
        require(not path.exists(),'tail_already_submitted_readback_required')
        with _held(root,request) as held:
            _unclaimed(root,request)
            now=datetime.now(timezone.utc);config=_config(request)
            fresh=_snapshot(runtime,request,now=now)
            require(fingerprint(fresh)==preview['prestate_sha256'] and fingerprint(dict(request=request,snapshot=fresh))==preview['candidate_sha256'],
                'tail_prestate_changed')
            path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            owner=dict(contract=CONTRACT,operation=OPERATION,operation_id=operation_id,pid=os.getpid(),identity=process_generation(os.getpid()),held=held)
            attempt=dict(contract=CONTRACT,phase='submitted',owner=owner,request=request,prestate=fresh,submitted_at=now.isoformat())
            # A publication-scoped O_EXCL claim survives all uncertainty,
            # including a crash between claim and attempt. A new operation ID
            # cannot resubmit this same published contour.
            _exclusive_private(_claim_path(root,request),dict(contract=CONTRACT,operation_id=operation_id,request=request))
            _exclusive_private(path,attempt)
            error_phase='admission'
            try:
                with _admitted(root),heavy_admitted(root,operation=OPERATION):
                    receipt=HistoricalReceipt(runtime,request['publication_operation_id'],request['publication_attempt_id']).freeze_scope(now)
                    today=__import__('packages.business_time',fromlist=['current_business_date_iso']).current_business_date_iso(now)
                    old=tuple(d for d in receipt.dates if d<(datetime.fromisoformat(today)-timedelta(days=1)).date().isoformat())
                    error_phase='owned_history'
                    with historical_receipt_history_worker(runtime=runtime,config=config,owner=owner) as worker:
                        history=worker.complete(now,backfill_dates=old,historical_receipt=receipt)
                    del worker,receipt;gc.collect()
                    # Fresh native domain ownership after actual child reaping.
                    with warehouse_functional_job_lock(root),accounting.writer_lock(root):
                        error_phase='readonly_finance'
                        finance=_finance(runtime,request)
                        fresh['tail_operation_id']=operation_id
                        error_phase='metadata_finalization'
                        completion=_finalize(runtime,request,config=config,finance=finance,now=now,expected=fresh)
                    attempt.update(phase='complete',history=history,completion=completion,finished_at=datetime.now(timezone.utc).isoformat())
                    _private(path,attempt)
            except BaseException as exc:
                attempt['error_phase']=error_phase
                attempt['error_type']=type(exc).__name__
                code=str(exc).partition(':')[0]
                if re.fullmatch(r'[a-z][a-z0-9_]{0,127}',code) and code.startswith(('tail_','historical_','original_','finance_','current_accounting_','ready_')):
                    attempt['error_code']=code
                # Payload-free native diagnostic only; never persist exception text.
                if hasattr(exc,'diagnostic'):attempt['error_diagnostic']=exc.diagnostic()
                _private(path,attempt)
                # The durable marker remains submitted even if only startup
                # was attempted. No automatic resubmit is authorized.
                raise AmbiguousSubmit('historical_tail_readback_required') from exc
        return dict(operation_id=operation_id,disposition='submitted')

    def readback(self,request,operation_id):
        from packages.application.fbs_accounting_apply import target
        from packages.application.fbs_historical_fbo_action import _runtime
        root=target(request);path=_path(root,operation_id)
        claim_path=_claim_path(root,request)
        claim=read_json(claim_path,limit=2*1024**2) if claim_path.exists() else None
        if claim is not None:
            require(claim==dict(contract=CONTRACT,operation_id=operation_id,request=request),'tail_readback_claim_changed')
        if not path.exists():return dict(operation_id=operation_id,state='ambiguous' if claim else 'not_submitted',
            recovery_ref=operation_id,next_step='same_operation_readback_only' if claim else 'none')
        require(claim is not None,'tail_publication_claim_missing')
        attempt=read_json(path,limit=2*1024**2)
        require(attempt.get('contract')==CONTRACT and attempt.get('request')==request and attempt['owner']['operation_id']==operation_id,'tail_readback_identity_changed')
        runtime=_runtime(root);prestate=attempt['prestate'];binding=prestate['parent_binding']
        with ready.readonly(runtime.db_path) as conn:
            parent=_publication_pin(conn,binding['operation_id'],binding['attempt_id'])
            child=_publication_pin(conn,request['publication_operation_id'],request['publication_attempt_id'])
            row=conn.execute('SELECT diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',(binding['operation_id'],binding['attempt_id'])).fetchone()
            completion=json.loads(row[0]).get('temporary_allocation_completion',{})
            ack=conn.execute('SELECT diagnostics_json FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',(request['publication_operation_id'],request['publication_attempt_id'])).fetchone()
            child_diagnostics=json.loads(ack[0]);ready.check_material(conn,prestate['material'])
            require(_preservation(conn,request)==prestate['preserved_confirmations'],'tail_confirmation_preservation_changed')
        require(parent['inputs_sha256']==request['parent_inputs_sha256'] and child==prestate['child'],'tail_readback_source_changed')
        require(_held_binding(root,request)==attempt['owner']['held'],'tail_maintenance_changed')
        require('sha256:'+file_digest(root/'sheet-vitrina-cycles'/(request['original_cycle_id']+'.json'))==request['original_cycle_sha256'],'tail_original_cycle_changed')
        complete=bool(parent['state']=='complete' and completion.get('history_only_tail')==operation_id
            and completion.get('history_ack')==child_diagnostics.get('historical_history_ack')
            and child_diagnostics.get('temporary_allocation_scope_complete') is True
            and completion.get('finance',{}).get('read_only_completion_proof') is True
            and completion.get('finance',{}).get('runtime_mutation') is False)
        finance_current=_readback_native(runtime,request,attempt,completion.get('history_ack')) if complete else None
        require(_held_binding(root,request)==attempt['owner']['held'],'tail_maintenance_changed')
        return dict(operation_id=operation_id,state='applied' if complete else 'ambiguous',scope_complete=complete,
            original_cycle_id=request['original_cycle_id'],original_cycle_untouched=True,full_cycle=False,finance_write=False,
            history_ack=completion.get('history_ack'),finance=completion.get('finance'),finance_current=finance_current,
            maintenance_held=True,error_phase=attempt.get('error_phase'),error_type=attempt.get('error_type'),error_code=attempt.get('error_code'),error_diagnostic=attempt.get('error_diagnostic'),recovery_ref=operation_id,next_step='none' if complete else 'same_operation_readback_only')
