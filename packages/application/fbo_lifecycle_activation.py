"""One-submit, held activation of addressed FBO supplies and forward costs.

Activation only appends policy/baseline records. Existing quantities, reservations,
costs and documents must be byte-for-byte unchanged. The private admission is the
before-image; any uncertain submit is resolved from the same immutable cutover.
"""
from contextlib import closing, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from apps.production_apply_contract import AdapterError, AmbiguousSubmit
from packages.application import fbs_scoped_lifecycle_production as shared
from packages.application import fbo_supply_lifecycle as lifecycle
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock, warehouse_functional_write_lock

CONTRACT = 'fbo_lifecycle_activation_v1'
_HELD = ContextVar('fbo_lifecycle_activation_owner', default=None)
RECOVERY_CONTRACT = 'fbo_activation_precommit_recovery_v1'
# Reviewed original transaction: CONFIG is inserted before all domain effects;
# its immutable row and every other policy effect commit in that transaction.
# This is one addressed recovery, not permission to retry arbitrary submissions.
RECOVERABLE = {
    'wbc0206-fbo-activation-20261110': {
        'claim_sha256': 'sha256:484d3a1a5ef52ffb4fafcde6d47cbb88dc96056d5ff4bb981cf24986683fc5f8',
        'prestate_sha256': 'sha256:d53b4cdcebc70d6b2c43b196a3eb0364ffee4ef80bc2a84ad01c335665a19eaf',
        'atomic_sources': {
            'fbo_lifecycle_activation.py': 'sha256:17556f8ff38010c5e970f39ae94b0f29ba41649b1ccf6ef03ec1a573da829873',
            'fbo_supply_lifecycle.py': 'sha256:078e152d7dc5b896007104e3c4e6e28eea9431b7167317968fbd4b05517556f5',
            'fbo_forward_costs.py': 'sha256:f1cc8d85e933a731767d0aa9e617fee4a567dee5d6bd58001e6d8582b4017895',
        },
    },
}


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


def _private_json(path):
    require(not any(parent.is_symlink() for parent in (path, *path.parents)), 'evidence_symlink')
    value = shared._read_saved(path)
    raw = path.read_bytes()
    require(len(raw) <= 32 * 1024**2 and json.loads(raw) == value, 'evidence_changed')
    return value, 'sha256:' + hashlib.sha256(raw).hexdigest()


def _original_recovery(root, db, request, operation_id):
    recovery = request.get('recovery')
    profile = RECOVERABLE.get(operation_id)
    require(profile and isinstance(recovery, dict) and set(recovery) == {
        'contract', 'original_claim_sha256', 'approval_reference'}, 'recovery_not_registered')
    require(recovery['contract'] == RECOVERY_CONTRACT and recovery['approval_reference']
            and recovery['original_claim_sha256'] == profile['claim_sha256'], 'recovery_authority_required')
    _, claim = paths(root, operation_id)
    saved, hashed = _private_json(claim)
    original = saved.get('preview') or {}
    require(hashed == profile['claim_sha256'] and saved.get('contract') == CONTRACT
            and original.get('operation_id') == operation_id and original.get('target') == str(db)
            and original.get('prestate_sha256') == profile['prestate_sha256'], 'recovery_original_claim_changed')
    material = original['prepared']
    require(digest(material) == original['prestate_sha256']
            and saved['request_digest'] == material['request_digest']
            and all(material['code'].get(name) == value for name, value in profile['atomic_sources'].items())
            and authority()['fbo_supply_lifecycle.py'] == profile['atomic_sources']['fbo_supply_lifecycle.py'],
            'recovery_original_atomicity_unproved')
    require(request.get('supply_ids') == original['scope']['supply_ids']
            and request.get('cutoff_at') == original['scope']['cutoff_at'], 'recovery_scope_changed')
    return dict(contract=RECOVERY_CONTRACT, original_claim_sha256=hashed,
                original_prestate_sha256=original['prestate_sha256'], atomic_sources=profile['atomic_sources'])


def _rollback_proof(conn):
    """No committed cutover can lose its CONFIG or append-only event witness.

    Compare real schema/triggers too; a caller-provided success flag or a missing
    table does not establish rollback. Business rows may have advanced after the
    separately retained dated full-scope proof, so they enter the fresh preview.
    """
    tables = (lifecycle.CONFIG, lifecycle.BASELINE, lifecycle.CURRENT, lifecycle.EVENTS)
    require(set(tables) <= lifecycle._tables(conn), 'rollback_schema_missing')
    require(not any(name.startswith('sheet_vitrina_v1_fbo_forward_cost_')
                    for name in lifecycle._tables(conn)), 'rollback_forward_policy_present')
    placeholders = ','.join('?' for _ in tables)
    sql = f"SELECT type,name,sql FROM sqlite_master WHERE tbl_name IN ({placeholders}) AND type IN ('table','trigger') ORDER BY type,name"
    with closing(sqlite3.connect(':memory:')) as expected:
        lifecycle.ensure_schema(expected)
        canonical_schema = [list(row) for row in expected.execute(sql, tables)]
    schema = [list(row) for row in conn.execute(sql, tables)]
    require(schema == canonical_schema, 'rollback_schema_or_immutability_changed')
    counts = {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in tables}
    require(not any(counts.values()), 'rollback_policy_rows_present')
    sequence = conn.execute('SELECT seq FROM sqlite_sequence WHERE name=?', (lifecycle.EVENTS,)).fetchone()
    require(sequence is None or sequence[0] == 0, 'rollback_event_sequence_advanced')
    return dict(policy_counts=counts, schema_sha256=digest(schema), forward_tables_absent=True)


def _completed_deploy(root, request):
    from packages.application import business_data_deploy_protection as deploy
    owner = deploy.load(root)
    metadata, metadata_sha256 = _private_json(root.parent / 'app' / '.wb-core-deploy.json')
    require(owner and owner['phase'] == 'complete' and owner['expected_sha'] == request['runtime_sha']
            and metadata.get('schema_version') == 'wb_core_deploy_metadata_v2'
            and metadata.get('commit') == request['runtime_sha'] and metadata.get('deployment_complete') is True,
            'recovery_completed_deploy_required')
    return dict(owner=owner, metadata_sha256=metadata_sha256)


def _failure(exc, operation_id, stage):
    # Never persist an arbitrary exception string: it can contain provider data,
    # credentials or user input. SQLite's symbolic code is sufficient here.
    result = dict(operation_id=operation_id, stage=stage, exception_type=type(exc).__name__)
    if isinstance(exc, sqlite3.Error):
        result['sqlite_errorname'] = getattr(exc, 'sqlite_errorname', 'SQLITE_ERROR')
    code = str(exc).split(':', 1)[0]
    if code in {
        'facility_pool_feature_off', 'lifecycle_cutover_already_applied', 'migration_plan_changed',
        'migration_cache_baseline_changed', 'migration_source_changed', 'nonfinite_fbo_cost',
        'negative_fbo_doprinato_baseline_quantity', 'duplicate_fbo_forward_cost_baseline_identity',
        'invalid_fbo_forward_cost_baseline_plan', 'fbo_forward_cost_baseline_source_changed',
        'fbo_forward_cost_inactive_baseline_not_empty', 'fbo_activation_protected_state_changed',
        'fbo_activation_authority_changed', 'fbo_activation_recovery_deployment_changed',
        'fbo_activation_recovery_original_changed_before_commit', 'fbo_activation_readback_failed',
    }:
        result['error_code'] = code
    return result


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
        recovery = _original_recovery(root, db, request, operation_id) if 'recovery' in request else None
        plan = lifecycle.FboSupplyLifecycle(db_path=db).migration_preview(
            supply_ids=request['supply_ids'], cutoff_at=request['cutoff_at'])
        with closing(shared.readonly(db)) as conn:
            conn.execute('BEGIN')
            if lifecycle.CONFIG in lifecycle._tables(conn):
                require(conn.execute(f'SELECT 1 FROM {lifecycle.CONFIG}').fetchone() is None, 'already_active')
            from packages.application.fbo_forward_costs import baseline_preview
            costs = baseline_preview(conn)
            if recovery:
                recovery = {**recovery, 'rollback': _rollback_proof(conn),
                            'completed_deploy': _completed_deploy(root, request)}
            before = protected(conn)
        material = dict(plan=plan, protected=before, held=shared._held_binding(root, request),
                        code=authority(), request_digest=digest(request), forward_cost_baseline=costs)
        if recovery:
            material['recovery'] = recovery
        folder, _ = paths(root, operation_id)
        return dict(operation_id=operation_id, target=str(db),
                    scope=dict(action=CONTRACT, supply_ids=request['supply_ids'], cutoff_at=request['cutoff_at'],
                               quantity_mutation=False, historic_cost_mutation=False,
                               submission_phase='precommit_recovery' if recovery else 'original'),
                    prestate_sha256=digest(material), candidate_sha256=digest(dict(
                        lifecycle_plan=plan['plan_digest'], forward_cost_baseline=costs['plan_digest'])),
                    recovery=dict(kind='atomic_append_only_cutover', before_image=str(folder / 'submitted.json'),
                                  rollback='before any new activity only; otherwise forward correction'), prepared=material)

    def preview(self, request, operation_id):
        root, db = shared.target(request)
        folder, claim = paths(root, operation_id)
        if 'recovery' in request:
            _original_recovery(root, db, request, operation_id)
            claim = folder / 'recovery-submitted.json'
            if not claim.exists():
                return self.capture(request, operation_id)
        if claim.exists():
            saved = shared._read_saved(claim)
            require(saved['request_digest'] == digest(request), 'request_conflict')
            return saved['preview']
        return self.capture(request, operation_id)

    def apply(self, request, operation_id, preview):
        root, db = shared.target(request)
        folder, claim = paths(root, operation_id)
        recovery = 'recovery' in request
        if recovery:
            _original_recovery(root, db, request, operation_id)
            claim = folder / 'recovery-submitted.json'
        require(not claim.exists(), 'prior_submit_readback_only')
        with admitted(root, request), warehouse_functional_job_lock(root), warehouse_functional_write_lock(root):
            fresh = self.capture(request, operation_id)
            require(all(fresh[key] == preview[key] for key in ('prestate_sha256', 'candidate_sha256')), 'preview_changed')
            shared._exclusive_json(claim, dict(contract=RECOVERY_CONTRACT if recovery else CONTRACT,
                                               request_digest=digest(request), preview=fresh))
            stage = 'migration_apply'
            try:
                now = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
                from packages.application.fbo_forward_costs import activate
                def activate_policy(conn, activated_at):
                    activate(conn, activated_at, expected_plan=fresh['prepared']['forward_cost_baseline'])
                    require(protected(conn) == fresh['prepared']['protected'], 'protected_state_changed')
                    require(shared.target(request) == (root, db) and authority() == fresh['prepared']['code'], 'authority_changed')
                    if recovery:
                        require(_original_recovery(root, db, request, operation_id) == {
                            key: fresh['prepared']['recovery'][key] for key in (
                                'contract', 'original_claim_sha256', 'original_prestate_sha256', 'atomic_sources')},
                                'recovery_original_changed_before_commit')
                        require(_completed_deploy(root, request) == fresh['prepared']['recovery']['completed_deploy'],
                                'recovery_deployment_changed')
                    require_held_activation(root)
                lifecycle.FboSupplyLifecycle(db_path=db, timestamp_factory=lambda: now).migration_apply(
                    fresh['prepared']['plan'], actor=request['actor'], on_activate=activate_policy)
                stage = 'post_commit_readback'
                readback = self.readback(request, operation_id)
                require(readback['state'] == 'applied' and readback['protected_scope_unchanged_now'], 'readback_failed')
                if recovery:
                    shared._exclusive_json(folder / 'recovery-receipt.json', dict(
                        contract=RECOVERY_CONTRACT, operation_id=operation_id, request_digest=digest(request),
                        prestate_sha256=fresh['prestate_sha256'], candidate_sha256=fresh['candidate_sha256'],
                        recovery_intent_sha256=_private_json(claim)[1],
                        original_claim_sha256=fresh['prepared']['recovery']['original_claim_sha256'],
                        readback=readback))
            except BaseException as exc:
                # This receipt is diagnostic only and grants no retry authority.
                # A crash before it is durable remains readback-only too.
                try:
                    shared._exclusive_json(folder / ('recovery-failure.json' if recovery else 'failure.json'),
                                           _failure(exc, operation_id, stage))
                except Exception:
                    pass
                raise AmbiguousSubmit('fbo_activation_same_operation_readback_only') from exc
        return dict(operation_id=operation_id, disposition='submitted')

    def readback(self, request, operation_id):
        root, db = shared.target(request)
        folder, claim = paths(root, operation_id)
        recovery = 'recovery' in request
        original = _original_recovery(root, db, request, operation_id) if recovery else None
        if recovery:
            claim = folder / 'recovery-submitted.json'
            if not claim.exists():
                with closing(shared.readonly(db)) as conn:
                    conn.execute('BEGIN')
                    rollback = _rollback_proof(conn)
                # State describes this explicitly requested recovery submission,
                # not the already claimed original activation. Launcher never
                # sees not_submitted for the ambiguous original request.
                return dict(operation_id=operation_id, state='not_submitted',
                            submission_phase='precommit_recovery', original_submission_state='proven_rolled_back',
                            original=original, rollback=rollback)
        if not claim.exists():
            return dict(operation_id=operation_id, state='not_submitted')
        saved = shared._read_saved(claim)
        require(saved['request_digest'] == digest(request)
                and saved['contract'] == (RECOVERY_CONTRACT if recovery else CONTRACT), 'request_conflict')
        material = saved['preview']['prepared']
        if recovery:
            require(digest(material) == saved['preview']['prestate_sha256']
                    and material['request_digest'] == digest(request)
                    and saved['preview']['operation_id'] == operation_id
                    and saved['preview']['target'] == str(db)
                    and saved['preview']['candidate_sha256'] == digest(dict(
                        lifecycle_plan=material['plan']['plan_digest'],
                        forward_cost_baseline=material['forward_cost_baseline']['plan_digest']))
                    and all(material['recovery'].get(key) == value for key, value in original.items()),
                    'recovery_intent_changed')
        extra = dict(submission_phase='precommit_recovery' if recovery else 'original')
        if recovery:
            receipt = folder / 'recovery-receipt.json'
            extra['recovery_receipt_recorded'] = receipt.exists()
            if receipt.exists():
                recorded, _ = _private_json(receipt)
                require(all(recorded.get(key) == value for key, value in {
                    'contract': RECOVERY_CONTRACT, 'operation_id': operation_id,
                    'request_digest': digest(request), 'prestate_sha256': saved['preview']['prestate_sha256'],
                    'candidate_sha256': saved['preview']['candidate_sha256'],
                    'original_claim_sha256': original['original_claim_sha256'],
                    'recovery_intent_sha256': _private_json(claim)[1],
                }.items()) and recorded.get('readback', {}).get('state') == 'applied'
                    and recorded['readback'].get('operation_id') == operation_id
                    and recorded['readback'].get('plan_digest') == material['plan']['plan_digest'],
                    'recovery_receipt_changed')
        failure = folder / ('recovery-failure.json' if recovery else 'failure.json')
        if failure.exists():
            extra['failure'] = shared._read_saved(failure)
        with closing(shared.readonly(db)) as conn:
            conn.execute('BEGIN')
            if lifecycle.CONFIG not in lifecycle._tables(conn):
                return dict(operation_id=operation_id, state='ambiguous', **extra)
            config = conn.execute(f'SELECT * FROM {lifecycle.CONFIG} WHERE slot=1').fetchone()
            if config is None or config['plan_digest'] != material['plan']['plan_digest']:
                return dict(operation_id=operation_id, state='ambiguous', **extra)
            # This flag describes the immediate activation proof. Subsequent
            # authorized operations may legitimately advance business rows.
            unchanged = protected(conn) == material['protected']
            from packages.application import fbo_forward_costs
            costs = fbo_forward_costs.active(conn)
        return dict(operation_id=operation_id, state='applied' if costs else 'ambiguous',
                    supply_ids=request['supply_ids'], protected_scope_unchanged_now=unchanged,
                    forward_cost_policy_active=bool(costs), plan_digest=config['plan_digest'], **extra)
