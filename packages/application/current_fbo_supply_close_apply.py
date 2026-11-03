"""Held production admission for a bounded current-day FBO supply batch."""
from contextlib import closing, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import os
import sqlite3
from types import SimpleNamespace, MethodType

from apps.production_apply_contract import AmbiguousSubmit
from packages.application import current_fbo_supply_close as core
from packages.application import fbs_scoped_lifecycle_production as shared
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock, warehouse_functional_write_lock, require_warehouse_job_owner
from packages.application.business_data_procedure_admission import already_admitted
from packages.application.fbs_accounting_runtime import writer_lock
from packages.application.fbs_accounting_runtime import load as load_book
from packages.application.warehouse_recovery_policy import WarehouseRecoveryRegistry, RecoveryState, _operation_id
from packages.business_time import business_date_from_timestamp

CONTRACT = core.CONTRACT
KIND = 'current_fbo_supply_close'
_OWNER = ContextVar('current_fbo_supply_close_owner', default=None)
_ADMISSION = ContextVar('current_fbo_supply_close_admission', default=None)


def require_held_current_fbo(root):
    value = _ADMISSION.get()
    core.require(value and value[0] == os.getpid() and value[1] == Path(root).resolve(), 'typed_held_owner_required')
    core.require(shared.require_held_scoped_recovery(root) == value[2], 'typed_held_owner_changed')
    return value[2]


@contextmanager
def held_admitted(root, request):
    from packages.application.business_data_procedure_admission import AdmissionLease
    with shared.held(root, request) as binding:
        token = _ADMISSION.set((os.getpid(), root, binding))
        try:
            lease = AdmissionLease.held_current_fbo_close(root)
            try:
                with lease.entered():
                    yield
            finally:
                lease.close()
        finally:
            _ADMISSION.reset(token)


def authority():
    names = ('current_fbo_supply_close.py', 'current_fbo_supply_close_apply.py',
        'ff_stock_ledger.py', 'registry_upload_db_backed_runtime.py', 'ff_pool_foundation.py',
        'warehouse_fbs_material_rematerialization.py', 'operator_warehouse_documents.py')
    names += ('current_fbo_supply_cost_source.py','fbs_snapshot_cost_sources.py','fbs_document_cost.py','warehouse_functional.py')
    return {**shared.code_authority(), **{n:'sha256:'+hashlib.sha256(Path(__file__).with_name(n).read_bytes()).hexdigest() for n in names}}


def paths(root, operation_id):
    shared._paths(root, operation_id)  # canonical bounded operation-id validator
    folder = root/'backups'/'private-evidence'/'current-fbo-supply-close'/operation_id
    return folder, folder/'submitted.json'


def require_core_admission(conn, runtime, plan):
    owner = _OWNER.get()
    core.require(owner and owner[0] == os.getpid(), 'typed_owner_required')
    root, db, plan_digest = owner[1:]
    core.require(Path(runtime.runtime_dir).resolve() == root and Path(runtime.db_path).resolve() == db
        and Path(conn.execute('PRAGMA database_list').fetchone()[2]).resolve() == db
        and plan['plan_digest'] == plan_digest and already_admitted(root), 'typed_owner_scope')
    require_held_current_fbo(root)
    require_warehouse_job_owner(root)


class CurrentFboSupplyCloseAdapter:
    def capture(self, request, operation_id, now):
        root, db = shared.target(request)
        held = shared._held_binding(root, request)
        with closing(shared.readonly(db)) as conn:
            conn.execute('BEGIN')
            day = shared._current_day(conn, now)
            plan = core.prepare(conn, operation_id=operation_id, facility_id=request['facility_id'],
                supply_ids=request['supply_ids'], business_date=day)
            core.require(plan['total_quantity'] == request['expected_quantity'], 'reviewed_quantity_changed')
            protected = shared.protected_scope(conn, root)
            from packages.application.current_fbo_supply_cost_source import book_bridge
            book,book_version=load_book(root)
            bridge=book_bridge(book,plan)
            core.require(book_version==protected['accounting_version'],'book_capture_drift')
        material = dict(plan=plan, protected=protected, book_bridge=bridge, held=held, code=authority(), request_digest=core.digest(request))
        return dict(operation_id=operation_id, target=str(db),
            scope=dict(action=CONTRACT, facility_id=request['facility_id'], supply_ids=request['supply_ids'],
                quantity=plan['total_quantity'], business_date=day, cost_policy='approved_current_cost_recovery'),
            prestate_sha256=core.digest(material), candidate_sha256=plan['plan_digest'],
            recovery=dict(kind='native_T2_warehouse_domain', capacity_admission='native_prepare_t2_before_mutation'),
            prepared=material)

    def preview(self, request, operation_id):
        root, _ = shared.target(request)
        _, claim = paths(root, operation_id)
        if claim.exists():
            saved = shared._read_saved(claim)
            core.require(saved['request_digest'] == core.digest(request), 'existing_request_conflict')
            return saved['preview']
        core.require(request.get('approval_reference') and request.get('actor'), 'approval_actor_required')
        return self.capture(request, operation_id, datetime.now(timezone.utc).isoformat().replace('+00:00','Z'))

    def apply(self, request, operation_id, preview):
        root, db = shared.target(request)
        _, claim = paths(root, operation_id)
        if claim.exists():
            core.require(self.readback(request, operation_id)['state'] == 'applied', 'prior_submit_readback_only')
            return dict(operation_id=operation_id, disposition='already_applied')
        recovery = None
        with held_admitted(root, request), warehouse_functional_job_lock(root), warehouse_functional_write_lock(root), writer_lock(root):
            now = datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
            fresh = self.capture(request, operation_id, now)
            core.require(all(fresh[k] == preview[k] for k in ('prestate_sha256','candidate_sha256')), 'preview_compare_and_swap')
            shared._exclusive_json(claim, dict(contract=CONTRACT, request_digest=core.digest(request), preview=fresh))
            registry = WarehouseRecoveryRegistry(runtime_dir=root, db_path=db)
            try:
                with sqlite3.connect(db) as schema:
                    core.ensure_receipts(schema)
                    schema.commit()
                plan = fresh['prepared']['plan']
                recovery = registry.prepare_t2(mutation_kind=KIND, plan_fingerprint=fresh['prestate_sha256'],
                    scope=fresh['scope'], source_digest=plan['source_digest'],
                    non_target_digest=core.digest(fresh['prepared']['protected']),
                    source_watermarks=dict(day=plan['business_date'], code_authority=fresh['prepared']['code']), schema_revision=CONTRACT)
                core.require(recovery['lifecycle'] == RecoveryState.VERIFIED.value, 'recovery_not_verified')
                recovery = registry.begin_mutation(recovery['operation_id'], expected_source_digest=plan['source_digest'])
                runtime = SimpleNamespace(runtime_dir=root, db_path=db)
                runtime.create_ff_stock_operation_guarded = MethodType(RegistryUploadDbBackedRuntime.create_ff_stock_operation_guarded, runtime)
                with closing(sqlite3.connect(db, timeout=5)) as conn:
                    conn.row_factory = sqlite3.Row
                    conn.execute('BEGIN IMMEDIATE')
                    owner = _OWNER.set((os.getpid(),root,db,plan['plan_digest']))
                    try:
                        core.require(shared.target(request) == (root,db) and authority() == fresh['prepared']['code'], 'code_or_target_changed')
                        core.require(shared.protected_scope(conn,root) == fresh['prepared']['protected'], 'protected_source_changed')
                        result = core.apply_owned(conn, runtime, plan=plan, occurred_at=now, actor=request['actor'])
                        core.require(shared.protected_scope(conn,root) == fresh['prepared']['protected'], 'protected_source_changed')
                        core.require(business_date_from_timestamp(datetime.now(timezone.utc).isoformat()) == plan['business_date'], 'business_day_changed')
                        shared.require_held_scoped_recovery(root)
                        conn.commit()
                    except BaseException:
                        conn.rollback()
                        raise
                    finally:
                        _OWNER.reset(owner)
                registry.retain(recovery['operation_id'], after_digest=core.digest(result), non_target_digest=core.digest(fresh['prepared']['protected']))
            except BaseException as exc:
                if recovery is not None:
                    registry.fail_recoverable(recovery['operation_id'], error=type(exc).__name__, next_action='same_operation_readback_only')
                raise AmbiguousSubmit('current_fbo_submit_readback_only') from exc
        return dict(operation_id=operation_id, disposition='submitted')

    def readback(self, request, operation_id):
        root, db = shared.target(request)
        _, claim = paths(root, operation_id)
        if not claim.exists():
            return dict(operation_id=operation_id, state='not_submitted')
        saved = shared._read_saved(claim)
        core.require(saved['request_digest'] == core.digest(request), 'existing_request_conflict')
        preview = saved['preview']; plan = preview['prepared']['plan']
        with closing(shared.readonly(db)) as conn:
            conn.execute('BEGIN')
            exists = conn.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?', (core.RECEIPTS,)).fetchone()
            receipt = conn.execute(f'SELECT 1 FROM {core.RECEIPTS} WHERE operation_id=?', (operation_id,)).fetchone() if exists else None
            if receipt is None:
                return dict(operation_id=operation_id, state='ambiguous')
            result = core.verify(conn, plan, check_publication=False)
            publication_retained = all(core.rows(conn,table,'version_id=?',(result['published_version_id'],)) == saved
                for table,saved in result['publication_rows'].items())
            protected = shared.protected_scope(conn,root) == preview['prepared']['protected']
        recovery = WarehouseRecoveryRegistry(runtime_dir=root,db_path=db).get_operation(_operation_id(KIND,preview['prestate_sha256']))
        return dict(operation_id=operation_id, state='applied', total_quantity=result['total_quantity'],
            supply_ids=result['supply_ids'], published_version_id=result['published_version_id'],
            publication_retained=publication_retained,
            protected_scope_unchanged_now=protected, recovery=recovery,
            recovery_pending=recovery is None or recovery['lifecycle'] != RecoveryState.RETAINED.value)
