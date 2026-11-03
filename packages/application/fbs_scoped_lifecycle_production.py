"""One-submit production admission for the existing exact pinned FBS owner."""
from contextlib import closing, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from apps.production_apply_contract import AdapterError, AmbiguousSubmit
from packages.application import fbs_historical_fbo_scoped_recovery as core
from packages.application import fbs_historical_fbo_dependencies as dependencies
from packages.application import ready_publication as ready
from packages.application.storage_registry import StoreRegistry
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock, warehouse_functional_write_lock
from packages.application.warehouse_recovery_policy import WarehouseRecoveryRegistry, RecoveryState
from packages.application.fbs_accounting_runtime import writer_lock
from packages.business_time import business_date_from_timestamp

CONTRACT = "fbs_scoped_lifecycle_production_v1"
PRODUCTION_ROOT = Path('/opt/wb-core-runtime/state')
_HELD = ContextVar('held_scoped_fbs_recovery', default=None)
MAX_RECIPE_BYTES = 16 * 1024**2


def require(value, reason):
    if not value:
        raise AdapterError('fbs_scoped_' + reason)


def code_authority():
    names = ('fbs_scoped_lifecycle_production.py','fbs_historical_fbo_scoped_recovery.py',
        'fbs_historical_fbo_dependencies.py','ff_pool_fbs_lifecycle.py','ff_pool_foundation.py',
        'ff_fbs_mapping_extension_production.py','wb_fbs_orders.py','warehouse_fbs_material_rematerialization.py',
        'warehouse_business_projection.py','operator_warehouse_documents.py','storage_registry.py',
        'warehouse_recovery_policy.py','business_data_procedure_admission.py','ready_publication.py',
        'fbs_historical_fbo_action.py','business_data_held_formula.py')
    return {name:'sha256:'+hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in names}


def readonly(path):
    conn=sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True,timeout=5)
    conn.row_factory=sqlite3.Row;conn.execute('PRAGMA query_only=ON')
    return conn


def target(request):
    root=Path(request['runtime_dir']).resolve()
    require(root == PRODUCTION_ROOT.resolve(), 'canonical_runtime_required')
    require(re.fullmatch('[0-9a-f]{40}',str(request['runtime_sha'])) is not None,'exact_deployed_sha_required')
    marker=root.parent/'app'/'.wb-core-runtime-sha'
    require(marker.is_file() and marker.read_text().strip()==request['runtime_sha'],'deployed_sha_changed')
    registry=StoreRegistry(root);manifest=registry.load(require_files=True)
    require(manifest.manifest_sha256==request['storage_manifest_sha256'],'storage_manifest_changed')
    db=registry.resolve('operational',manifest=manifest)
    with closing(registry.connect('operational',mode='ro',operation='fbs_scoped_production_target')) as conn:
        conn.execute('BEGIN');conn.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchone()
    return root, db


def _held_binding(root,request):
    from packages.application.historical_receipt_completion import _held_binding as existing
    return existing(root,request)


def _quiet_activity(root,request):
    # Reuse the held owner which checks full units/controls, authenticated
    # activity, quiet state and the approved formula/deployment transition.
    from packages.application.fbs_historical_fbo_action import _maintenance
    return _maintenance(root,request)


@contextmanager
def held(root,request):
    from apps.business_data_maintenance import _ExclusiveRestoreLock
    from packages.application.business_data_write_barrier import _BarrierLock
    with _ExclusiveRestoreLock(root),_BarrierLock(root):
        before=_held_binding(root,request);_quiet_activity(root,request)
        token=_HELD.set((os.getpid(),str(root),dict(request),before))
        try:
            yield before
        finally:
            _HELD.reset(token)
            require(_held_binding(root,request)==before,'maintenance_changed')


def require_held_scoped_recovery(root):
    current=_HELD.get()
    require(current and current[0]==os.getpid() and current[1]==str(Path(root).resolve()),'held_owner_required')
    require(_held_binding(Path(root),current[2])==current[3],'held_owner_changed')
    return current[3]


def require_production_core_admission(conn,recipe):
    from packages.application.business_data_procedure_admission import already_admitted
    from packages.application.warehouse_functional_lock import require_warehouse_job_owner
    current=_HELD.get();require(current,'held_owner_required')
    root,db=target(current[2]);require_held_scoped_recovery(root)
    require(already_admitted(root),'procedure_admission_required')
    require_warehouse_job_owner(root)
    actual=Path(conn.execute('PRAGMA database_list').fetchone()[2]).resolve()
    require(actual==db and recipe['facility_id']==current[2]['facility_id']
        and recipe['nm_id']==current[2]['nm_id'],'core_target_scope_changed')


@contextmanager
def admitted(root):
    from packages.application.business_data_procedure_admission import AdmissionLease
    lease=AdmissionLease.held_scoped_fbs_recovery(root)
    try:
        with lease.entered():yield
    finally:lease.close()


def protected_scope(conn,root):
    tables=('sheet_vitrina_v1_ff_pool_documents','sheet_vitrina_v1_ff_pool_document_requests',
        'sheet_vitrina_v1_ff_pool_document_expense_lines','sheet_vitrina_v1_fulfillment_service_uploads',
        'sheet_vitrina_v1_fulfillment_service_lines','wb_finance_weekly_aggregates','wb_finance_weekly_sku_aggregates')
    result={}
    for table in tables:
        hashed=hashlib.sha256();count=0
        for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
            hashed.update(ready.canonical(list(row)).encode()+b'\n');count+=1
        result[table]=dict(rows=count,sha256=hashed.hexdigest())
    book=Path(root)/'fbs-snapshot-accounting.sqlite3'
    require(book.is_file(),'accounting_book_missing')
    with closing(readonly(book)) as accounting:
        result['accounting_version']=accounting.execute('SELECT version FROM accounting_current WHERE singleton=1').fetchone()[0]
        # Fence stored blobs/journal too: matching only the current version
        # would not prove that the seven retained expense sources survived.
        hashed=hashlib.sha256()
        for table in ('accounting_current','accounting_revisions','accounting_blobs'):
            for row in accounting.execute(f'SELECT * FROM {table} ORDER BY rowid'):
                for value in row:
                    payload=value if isinstance(value,bytes) else ready.canonical(value).encode()
                    hashed.update(len(payload).to_bytes(8,'big'));hashed.update(payload)
        result['accounting_journal_sha256']=hashed.hexdigest()
    return result


def _current_day(conn,now):
    row=conn.execute('''SELECT version.business_effective_date,snapshot.snapshot_date,snapshot.fetched_at
        FROM sheet_vitrina_v1_warehouse_functional_active active
        JOIN sheet_vitrina_v1_warehouse_functional_versions version USING(version_id)
        JOIN sheet_vitrina_v1_warehouse_wb_snapshots snapshot USING(version_id)
        WHERE active.slot=1 AND version.status='good' ORDER BY snapshot.created_at DESC LIMIT 1''').fetchone()
    require(row is not None,'current_functional_snapshot_missing')
    day=business_date_from_timestamp(now)
    require(row[0]==row[1]==day,'current_business_date_required')
    fetched=datetime.fromisoformat(str(row[2]).replace('Z','+00:00'))
    age=(datetime.fromisoformat(now.replace('Z','+00:00'))-fetched).total_seconds()
    # The native functional owner admits one coherent current business day.
    # An unrelated FBS-stock TTL must not invalidate its reviewed WB snapshot.
    require(fetched.utcoffset() is not None and age>=0,'official_snapshot_time_invalid')
    return day


def _file_authority(db):
    item=Path(db).stat()
    return dict(path=str(Path(db).resolve()),device=item.st_dev,inode=item.st_ino)


def _paths(root,operation_id):
    require(re.fullmatch('[a-z0-9][a-z0-9._-]{7,127}',operation_id) is not None,'operation_id_invalid')
    folder=root/'backups'/'private-evidence'/'fbs-scoped-lifecycle'/operation_id
    return folder, folder/'submitted.json'


def _read_saved(path):
    require(path.is_file() and not path.is_symlink() and path.stat().st_size<=32*1024**2,'saved_admission_invalid')
    return json.loads(path.read_text())


def _exclusive_json(path,value):
    payload=ready.canonical(value).encode();require(len(payload)<=32*1024**2,'admission_size_budget')
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    require(not path.parent.is_symlink(),'admission_directory_invalid')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as handle:handle.write(payload);handle.flush();os.fsync(handle.fileno())
    directory_fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(directory_fd)
    finally:os.close(directory_fd)


class FbsScopedLifecycleProductionAdapter:
    def _capture(self,request,operation_id,now):
        root,db=target(request);binding=_held_binding(root,request)
        file_authority=_file_authority(db)
        with closing(readonly(db)) as conn:
            conn.execute('BEGIN');day=_current_day(conn,now)
            recipe=core.prepare_scoped_fbs_recovery(conn,operation_id=operation_id,
                facility_id=request['facility_id'],nm_id=request['nm_id'])
            from packages.application.ff_pool_foundation import evaluate_ff_pool_aggregate_parity
            parity=evaluate_ff_pool_aggregate_parity(conn,[row for row in recipe['source_active_rows'] if row['warehouse_key']=='ff'])
            require(parity.status=='pass' and parity.feature_epoch==recipe['manifest']['feature_epoch'],
                'initial_pool_aggregate_parity_unproved')
            require((recipe['diagnostic']['order_count'],len(recipe['statuses']),len(recipe['predicted_handoffs']))==
                (request['expected_order_count'],request['expected_status_count'],request['expected_handoff_count']),'reviewed_scope_changed')
            protected=protected_scope(conn,root)
        require(_file_authority(db)==file_authority,'operational_file_changed')
        require(len(ready.canonical(recipe).encode())<=MAX_RECIPE_BYTES,'recipe_size_budget')
        material=dict(recipe=recipe,protected=protected,held=binding,day=day,code_authority=code_authority(),
            file_authority=file_authority,
            runtime_sha=request['runtime_sha'],storage_manifest_sha256=request['storage_manifest_sha256'],
            request_digest=dependencies._digest(request))
        folder,_=_paths(root,operation_id)
        return dict(operation_id=operation_id,target=str(db),scope=dict(action=CONTRACT,facility_id=request['facility_id'],
            nm_id=request['nm_id'],orders=request['expected_order_count'],statuses=request['expected_status_count'],
            handoffs=request['expected_handoff_count'],business_date=day),prestate_sha256=dependencies._digest(material),
            candidate_sha256=dependencies._digest(recipe['predicted_after']),
            recovery=dict(kind='native_T2_warehouse_domain',capacity_admission='native_prepare_t2_before_mutation',path=str(folder)),prepared=material)

    def preview(self,request,operation_id):
        root,db=target(request);folder,claim=_paths(root,operation_id)
        if claim.exists():
            saved=_read_saved(claim)
            require(saved['request_digest']==dependencies._digest(request),'existing_request_conflict')
            return saved['preview']
        require(request.get('approval_reference') and request.get('actor'),'approval_actor_required')
        return self._capture(request,operation_id,datetime.now(timezone.utc).isoformat().replace('+00:00','Z'))

    def apply(self,request,operation_id,preview):
        root,db=target(request);folder,claim=_paths(root,operation_id)
        if claim.exists():
            status=self.readback(request,operation_id)
            require(status['state']=='applied','prior_submit_readback_only')
            return dict(operation_id=operation_id,disposition='already_applied')
        submitted=False;recovery=None
        with held(root,request),admitted(root),warehouse_functional_job_lock(root),warehouse_functional_write_lock(root),writer_lock(root):
            now=datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
            fresh=self._capture(request,operation_id,now)
            require(all(fresh[k]==preview[k] for k in ('prestate_sha256','candidate_sha256')),'preview_compare_and_swap_failed')
            _exclusive_json(claim,dict(contract=CONTRACT,request_digest=dependencies._digest(request),preview=fresh))
            submitted=True
            registry=WarehouseRecoveryRegistry(runtime_dir=root,db_path=db)
            try:
                with sqlite3.connect(db) as schema:
                    core.ensure_scoped_recovery_receipts(schema);schema.commit()
                recovery=registry.prepare_t2(mutation_kind='fbs_scoped_lifecycle_recovery',plan_fingerprint=fresh['prestate_sha256'],
                    scope=fresh['scope'],source_digest=fresh['prepared']['recipe']['recipe_digest'],
                    non_target_digest=dependencies._digest(fresh['prepared']['protected']),
                    source_watermarks=dict(day=fresh['prepared']['day'],code_authority=fresh['prepared']['code_authority']),schema_revision=CONTRACT)
                require(recovery['lifecycle']==RecoveryState.VERIFIED.value,'recovery_not_verified')
                recovery=registry.begin_mutation(recovery['operation_id'],expected_source_digest=fresh['prepared']['recipe']['recipe_digest'])
                conn=sqlite3.connect(db,timeout=5);conn.row_factory=sqlite3.Row
                try:
                    conn.execute('BEGIN IMMEDIATE')
                    mutation_now=datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
                    require(business_date_from_timestamp(mutation_now)==fresh['prepared']['day'],'business_date_changed')
                    require(target(request)==(root,db) and code_authority()==fresh['prepared']['code_authority'],'apply_authority_changed')
                    require(_file_authority(db)==fresh['prepared']['file_authority'],'operational_file_changed')
                    require(_held_binding(root,request)==fresh['prepared']['held'],'maintenance_changed')
                    _current_day(conn,mutation_now)
                    require(protected_scope(conn,root)==fresh['prepared']['protected'],'protected_scope_changed')
                    result=core.apply_scoped_fbs_recovery_owned(conn,recipe=fresh['prepared']['recipe'],occurred_at=mutation_now,
                        actor=request['actor'],receipt_table=core.PRODUCTION_RECEIPTS)
                    require(protected_scope(conn,root)==fresh['prepared']['protected'],'protected_scope_changed')
                    require(business_date_from_timestamp(datetime.now(timezone.utc).isoformat())==fresh['prepared']['day'],'business_date_changed')
                    require_held_scoped_recovery(root)
                    conn.commit()
                except BaseException:
                    conn.rollback();raise
                finally:conn.close()
                registry.retain(recovery['operation_id'],after_digest=dependencies._digest(result),non_target_digest=dependencies._digest(fresh['prepared']['protected']))
            except BaseException as exc:
                if recovery is not None:
                    registry.fail_recoverable(recovery['operation_id'],error=type(exc).__name__,next_action='same_operation_readback_only')
                if submitted:raise AmbiguousSubmit('fbs_scoped_submit_readback_only') from exc
                raise
        return dict(operation_id=operation_id,disposition='submitted')

    def readback(self,request,operation_id):
        root,db=target(request);_,claim=_paths(root,operation_id)
        if not claim.exists():return dict(operation_id=operation_id,state='not_submitted')
        saved=_read_saved(claim);require(saved['request_digest']==dependencies._digest(request),'existing_request_conflict')
        preview=saved['preview'];recipe=preview['prepared']['recipe']
        with closing(readonly(db)) as conn:
            conn.execute('BEGIN')
            exists=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(core.PRODUCTION_RECEIPTS,)).fetchone()
            row=conn.execute(f'SELECT * FROM {core.PRODUCTION_RECEIPTS} WHERE operation_id=?',(operation_id,)).fetchone() if exists else None
            if row is None:return dict(operation_id=operation_id,state='ambiguous',recovery=preview['recovery'])
            require(row['recipe_digest']==recipe['recipe_digest'],'durable_recipe_changed')
            # Shared core's retry branch is query-only once its receipt exists;
            # avoid its CREATE IF NOT EXISTS on this strict read-only connection.
            result=core.verify_scoped_recovery_receipt(conn,recipe=recipe,receipt_table=core.PRODUCTION_RECEIPTS,check_current_dependencies=False)
            protected_now_unchanged=protected_scope(conn,root)==preview['prepared']['protected']
            dependency_now=dependencies.prepare_fbs_dependencies(conn,facility_id=recipe['facility_id'],nm_id=recipe['nm_id'])
        registry=WarehouseRecoveryRegistry(runtime_dir=root,db_path=db)
        from packages.application.warehouse_recovery_policy import _operation_id
        recovery=registry.get_operation(_operation_id('fbs_scoped_lifecycle_recovery',preview['prestate_sha256']))
        pending=recovery is None or recovery['lifecycle']!=RecoveryState.RETAINED.value
        return dict(operation_id=operation_id,state='applied',recipe_digest=recipe['recipe_digest'],
            orders=result['order_count'],statuses=result['status_count'],handoffs=len(result['physical_handoffs']),
            final_balance=result['final_balance'],protected_scope_unchanged_now=protected_now_unchanged,
            current_dependency_status=dependency_now.get('status'),recovery_pending=pending,
            recovery=recovery or preview['recovery'])
