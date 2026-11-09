"""One-submit, held activation of addressed FBO supplies and forward costs.

Activation only appends policy/baseline records. Existing quantities, reservations,
costs and documents must be byte-for-byte unchanged. The private admission is the
before-image; any uncertain submit is resolved from the same immutable cutover.
"""
from contextlib import closing, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path

from apps.production_apply_contract import AdapterError, AmbiguousSubmit
from packages.application import fbs_scoped_lifecycle_production as shared
from packages.application import fbo_supply_lifecycle as lifecycle
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock, warehouse_functional_write_lock

CONTRACT = 'fbo_lifecycle_activation_v1'
_HELD = ContextVar('fbo_lifecycle_activation_owner', default=None)


def require(value, reason):
    if not value:
        raise AdapterError('fbo_activation_' + reason)


def digest(value):
    return 'sha256:' + lifecycle.digest(value)


def require_held_activation(root):
    value = _HELD.get()
    require(value and value[0] == os.getpid() and value[1] == Path(root).resolve(), 'typed_owner_required')
    require(shared.require_held_scoped_recovery(root) == value[2], 'hold_changed')
    return value[2]


@contextmanager
def admitted(root, request):
    from packages.application.business_data_procedure_admission import AdmissionLease
    with shared.held(root, request) as binding:
        token = _HELD.set((os.getpid(), root, binding))
        try:
            lease = AdmissionLease.held_fbo_activation(root)
            try:
                with lease.entered():
                    yield
            finally:
                lease.close()
        finally:
            _HELD.reset(token)


def authority():
    names = ('fbo_lifecycle_activation.py', 'fbo_supply_lifecycle.py', 'fbo_forward_costs.py',
             'ff_wb_supply_origins.py', 'ff_stock_ledger.py', 'business_data_procedure_admission.py')
    return {name: 'sha256:' + hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in names}


def paths(root, operation_id):
    shared._paths(root, operation_id)
    folder = root / 'backups' / 'private-evidence' / 'fbo-lifecycle-activation' / operation_id
    return folder, folder / 'submitted.json'


def protected(conn):
    """Fingerprint existing operational business rows, excluding new policy state."""
    names = sorted(name for name in lifecycle._tables(conn)
                   if (name.startswith('sheet_vitrina_v1_ff_')
                       or name.startswith('sheet_vitrina_v1_fulfillment_')
                       or name.startswith('sheet_vitrina_v1_wb_suppl')
                       or name in {'sheet_vitrina_v1_warehouse_functional_active',
                                   'sheet_vitrina_v1_warehouse_functional_balances',
                                   'sheet_vitrina_v1_warehouse_wb_daily_cost',
                                   'sheet_vitrina_v1_warehouse_wb_daily_cost_corrections',
                                   'sheet_vitrina_v1_warehouse_functional_documents',
                                   'sheet_vitrina_v1_warehouse_functional_document_lines',
                                   'sheet_vitrina_v1_warehouse_business_operations',
                                   'sheet_vitrina_v1_warehouse_business_operation_relations'}))
    result = {}
    for name in names:
        hashed = hashlib.sha256()
        count = 0
        # All domain tables here are ordinary rowid SQLite tables. A schema
        # change refusing this query blocks activation instead of skipping it.
        for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid'):
            for value in row:
                payload = value if isinstance(value, bytes) else lifecycle.canonical(value).encode()
                hashed.update(len(payload).to_bytes(8, 'big'))
                hashed.update(payload)
            count += 1
        result[name] = dict(rows=count, sha256=hashed.hexdigest())
    return result


class FboLifecycleActivationAdapter:
    def capture(self, request, operation_id):
        root, db = shared.target(request)
        require(request.get('actor') and request.get('approval_reference'), 'actor_approval_required')
        plan = lifecycle.FboSupplyLifecycle(db_path=db).migration_preview(
            supply_ids=request['supply_ids'], cutoff_at=request['cutoff_at'])
        with closing(shared.readonly(db)) as conn:
            conn.execute('BEGIN')
            if lifecycle.CONFIG in lifecycle._tables(conn):
                require(conn.execute(f'SELECT 1 FROM {lifecycle.CONFIG}').fetchone() is None, 'already_active')
            before = protected(conn)
        material = dict(plan=plan, protected=before, held=shared._held_binding(root, request),
                        code=authority(), request_digest=digest(request))
        folder, _ = paths(root, operation_id)
        return dict(operation_id=operation_id, target=str(db),
                    scope=dict(action=CONTRACT, supply_ids=request['supply_ids'], cutoff_at=request['cutoff_at'],
                               quantity_mutation=False, historic_cost_mutation=False),
                    prestate_sha256=digest(material), candidate_sha256='sha256:' + plan['plan_digest'],
                    recovery=dict(kind='atomic_append_only_cutover', before_image=str(folder / 'submitted.json'),
                                  rollback='before any new activity only; otherwise forward correction'), prepared=material)

    def preview(self, request, operation_id):
        root, _ = shared.target(request)
        _, claim = paths(root, operation_id)
        if claim.exists():
            saved = shared._read_saved(claim)
            require(saved['request_digest'] == digest(request), 'request_conflict')
            return saved['preview']
        return self.capture(request, operation_id)

    def apply(self, request, operation_id, preview):
        root, db = shared.target(request)
        _, claim = paths(root, operation_id)
        require(not claim.exists(), 'prior_submit_readback_only')
        with admitted(root, request), warehouse_functional_job_lock(root), warehouse_functional_write_lock(root):
            fresh = self.capture(request, operation_id)
            require(all(fresh[key] == preview[key] for key in ('prestate_sha256', 'candidate_sha256')), 'preview_changed')
            shared._exclusive_json(claim, dict(contract=CONTRACT, request_digest=digest(request), preview=fresh))
            try:
                now = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
                from packages.application.fbo_forward_costs import activate
                def activate_policy(conn, activated_at):
                    activate(conn, activated_at)
                    require(protected(conn) == fresh['prepared']['protected'], 'protected_state_changed')
                    require(shared.target(request) == (root, db) and authority() == fresh['prepared']['code'], 'authority_changed')
                    require_held_activation(root)
                lifecycle.FboSupplyLifecycle(db_path=db, timestamp_factory=lambda: now).migration_apply(
                    fresh['prepared']['plan'], actor=request['actor'], on_activate=activate_policy)
                readback = self.readback(request, operation_id)
                require(readback['state'] == 'applied' and readback['protected_scope_unchanged_now'], 'readback_failed')
            except BaseException as exc:
                raise AmbiguousSubmit('fbo_activation_same_operation_readback_only') from exc
        return dict(operation_id=operation_id, disposition='submitted')

    def readback(self, request, operation_id):
        root, db = shared.target(request)
        _, claim = paths(root, operation_id)
        if not claim.exists():
            return dict(operation_id=operation_id, state='not_submitted')
        saved = shared._read_saved(claim)
        require(saved['request_digest'] == digest(request), 'request_conflict')
        material = saved['preview']['prepared']
        with closing(shared.readonly(db)) as conn:
            conn.execute('BEGIN')
            if lifecycle.CONFIG not in lifecycle._tables(conn):
                return dict(operation_id=operation_id, state='ambiguous')
            config = conn.execute(f'SELECT * FROM {lifecycle.CONFIG} WHERE slot=1').fetchone()
            if config is None or config['plan_digest'] != material['plan']['plan_digest']:
                return dict(operation_id=operation_id, state='ambiguous')
            # This flag describes the immediate activation proof. Subsequent
            # authorized operations may legitimately advance business rows.
            unchanged = protected(conn) == material['protected']
            from packages.application import fbo_forward_costs
            costs = fbo_forward_costs.active(conn)
        return dict(operation_id=operation_id, state='applied' if costs else 'ambiguous',
                    supply_ids=request['supply_ids'], protected_scope_unchanged_now=unchanged,
                    forward_cost_policy_active=bool(costs), plan_digest=config['plan_digest'])
