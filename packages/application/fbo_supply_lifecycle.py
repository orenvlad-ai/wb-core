"""Addressed FBO reservation/dispatch, explicitly scoped at cutover.

GETs never bootstrap. The existing reservation ledger is the compatibility
projection (not a second obligation). All physical writes share its transaction.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from packages.application.ff_pool_foundation import (
    BALANCES_TABLE, OPERATIONS_TABLE, LINES_TABLE, FACILITIES_TABLE,
    read_ff_pool_feature_state, canonical_decimal_ratio_text,
)

CONTRACT = 'fbo_supply_lifecycle_v1'
CURRENT = 'sheet_vitrina_v1_fbo_supply_lifecycle_current'
EVENTS = 'sheet_vitrina_v1_fbo_supply_lifecycle_events'
CONFIG = 'sheet_vitrina_v1_fbo_supply_lifecycle_config'
BASELINE = 'sheet_vitrina_v1_fbo_supply_lifecycle_baseline'
SUPPLIES = 'sheet_vitrina_v1_wb_supplies'
AGGREGATE = 'sheet_vitrina_v1_ff_stock_operations'
AGGREGATE_LINES = 'sheet_vitrina_v1_ff_stock_operation_lines'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def ensure_schema(conn):
    # execute, never executescript: safe in a caller-owned transaction.
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {CONFIG}(
        slot INTEGER PRIMARY KEY CHECK(slot=1), cutoff_at TEXT NOT NULL,
        plan_digest TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL)''')
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {BASELINE}(
        cache_key TEXT NOT NULL, supply_id TEXT PRIMARY KEY, wb_supply_id TEXT NOT NULL, preorder_id TEXT NOT NULL)''')
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {CURRENT}(
        cache_key TEXT PRIMARY KEY, supply_id TEXT NOT NULL, admitted_at TEXT NOT NULL,
        admission TEXT NOT NULL, state_json TEXT NOT NULL)''')
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {EVENTS}(
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, cache_key TEXT NOT NULL,
        state_revision TEXT NOT NULL, state_json TEXT NOT NULL, occurred_at TEXT NOT NULL)''')
    for verb in ('UPDATE', 'DELETE'):
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS fbo_lifecycle_baseline_no_{verb.lower()}
            BEFORE {verb} ON {BASELINE} BEGIN SELECT RAISE(ABORT,'FBO baseline is immutable'); END''')
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS fbo_lifecycle_events_no_{verb.lower()}
            BEFORE {verb} ON {EVENTS} BEGIN SELECT RAISE(ABORT,'FBO lifecycle evidence is append-only'); END''')
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS fbo_lifecycle_config_no_{verb.lower()}
            BEFORE {verb} ON {CONFIG} BEGIN SELECT RAISE(ABORT,'FBO lifecycle cutover is immutable'); END''')


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def feature_enabled(conn):
    if 'sheet_vitrina_v1_ff_pool_feature_epochs' not in _tables(conn):
        return False
    return read_ff_pool_feature_state(conn).writer_effective


def owns_supply(conn, reference):
    if CURRENT not in _tables(conn):
        return False
    real=str(reference).removeprefix('supply:')
    return conn.execute(f'SELECT 1 FROM {CURRENT} WHERE supply_id IN (?,?) OR cache_key=?', (real,'supply:'+real,'supply:'+real)).fetchone() is not None


def _record(conn, reference):
    from packages.application.ff_wb_supply_origins import _resolve_supply
    from packages.application.registry_upload_db_backed_runtime import _wb_supply_record_from_row
    supply = _resolve_supply(conn, str(reference))
    if supply is None:
        raise ValueError('wb_supply_not_found')
    row = conn.execute(f'SELECT * FROM {SUPPLIES} WHERE supply_id=?', (supply['supply_id'],)).fetchone()
    result = _wb_supply_record_from_row(row)
    # Indexed status is authoritative for a freshly persisted observation.
    result['normalized']['status_id'] = row['status_id']
    return result


def _identity(record):
    return str(record['cache_key']), str(record['supply_id'])


def _existing(conn, record):
    cache, sid = _identity(record)
    real = str(record.get('wb_supply_id') or sid.removeprefix('supply:'))
    keys = [f'wb_supply_debit:{cache}', f'wb_supply_debit:supply:{real}', f'wb_supply_debit:{real}']
    # Historical targeted/native close documents keep source_object_id; metadata
    # identity is checked as well, rather than assuming one source-key format.
    for row in conn.execute(f'''SELECT * FROM {AGGREGATE} WHERE source_key IN (?,?,?)
          OR (source_object_id IN (?,?,?) AND source_type IN ('wb_supply','wb_supply_targeted_reconciliation'))''',
          (*keys, sid, real, cache)):
        if float(row['total_quantity_delta'] or 0) < 0:
            return dict(row)
    if OPERATIONS_TABLE in _tables(conn):
        for row in conn.execute(f"SELECT * FROM {OPERATIONS_TABLE} WHERE operation_type='wb_supply_dispatch'"):
            meta = json.loads(row['metadata_json'] or '{}')
            effect = meta.get('effect') or meta
            if str(effect.get('supply_id') or '') in {sid, real, cache} or row['source_id'] in keys:
                return dict(row)
    return None


def _prior(conn, cache):
    if CURRENT not in _tables(conn):
        return None
    row = conn.execute(f'SELECT state_json FROM {CURRENT} WHERE cache_key=?', (cache,)).fetchone()
    return json.loads(row[0]) if row else None


def _legacy(conn, sid):
    from packages.application.registry_upload_db_backed_runtime import _ff_stock_reservation_quantities
    # Cache identities can be numeric or supply:<id>. Preserve both proven aliases.
    result = {}
    for alias in {sid, sid.removeprefix('supply:'), 'supply:' + sid.removeprefix('supply:')}:
        values = _ff_stock_reservation_quantities(conn, alias)
        if values:
            result[alias] = values
    return result


def _goods(conn, record):
    from packages.application.wb_supply_box_correction import BOX_CORRECTION_TABLE, corrected_goods
    raw = record.get('raw_goods') or []
    correction = conn.execute(f"SELECT corrected_composition_json FROM {BOX_CORRECTION_TABLE} WHERE supply_id=? AND status='applied' ORDER BY applied_at DESC,created_at DESC LIMIT 1", (str(record.get('wb_supply_id') or record['supply_id']),)).fetchone() if BOX_CORRECTION_TABLE in _tables(conn) else None
    if correction:
        raw = corrected_goods(raw, {int(k): int(v) for k,v in json.loads(correction[0]).items()})
    result = {}
    if not raw:
        raise ValueError('supply_goods_missing')
    for row in raw:
        nm = row.get('nmID', row.get('nmId', row.get('nm_id')))
        qty = Decimal(str(row.get('quantity', row.get('qty', 0))))
        if not nm or qty <= 0 or qty != int(qty):
            raise ValueError('supply_goods_invalid')
        nm = int(nm)
        if not conn.execute('SELECT 1 FROM sheet_vitrina_v1_nomenclature_items WHERE nm_id=? AND is_active=1 AND is_hidden=0', (nm,)).fetchone():
            raise ValueError('supply_goods_nomenclature_missing')
        result[str(nm)] = result.get(str(nm), 0) + int(qty)
    return dict(sorted(result.items(), key=lambda p:int(p[0])))


def _creation_timestamp(record):
    """Creation evidence only: planned delivery/actual acceptance are not age."""
    from packages.application.ff_stock_ledger import _parse_datetime_like
    sources=[record.get('normalized') or {},record,record.get('raw_list') or {},record.get('raw_detail') or {}]
    candidates=[_parse_datetime_like(source.get(field)) for field in ('source_created_at','createDate','createdAt') for source in sources if isinstance(source,dict)]
    candidates=[value for value in candidates if value is not None]
    # Conflicting genuine creation evidence never promotes an old source.
    return min(candidates) if candidates else None


def _cache_baseline(conn):
    return [dict(row) for row in conn.execute(f"SELECT COALESCE(NULLIF(cache_key,''),supply_id) cache_key,supply_id,COALESCE(wb_supply_id,'') wb_supply_id,COALESCE(preorder_id,'') preorder_id FROM {SUPPLIES} ORDER BY cache_key,supply_id")]


def _scope(conn, record):
    cache, sid = _identity(record)
    real=str(record.get('wb_supply_id') or '')
    if not real.isdigit() or int(real)<=0 or int(record['normalized'].get('virtual_type_id') or 0)==5 or str(record['normalized'].get('type_label') or '')=='Допринято':
        return False
    if _prior(conn, cache) is not None:
        return True
    if not {CONFIG,BASELINE} <= _tables(conn):
        return False
    config = conn.execute(f'SELECT * FROM {CONFIG} WHERE slot=1').fetchone()
    if not config:
        return False
    # A persisted all-cache before-image is the admission boundary. Existing
    # history never becomes new because a date or source alias was refreshed.
    if conn.execute(f'SELECT 1 FROM {BASELINE} WHERE cache_key=? OR supply_id=? OR wb_supply_id=?',(cache,sid,real)).fetchone():
        return False
    from packages.application.ff_stock_ledger import _parse_datetime_like, _wb_supply_checkpoint_match_fields
    dt = _creation_timestamp(record)
    cutoff = _parse_datetime_like(config['cutoff_at'])
    if cutoff is None or (dt is not None and dt <= cutoff):
        return False
    # Date-less sources seen for the first time in shipping-permitted status
    # are genuinely new work. Late discovered acceptance/history needs proof.
    if dt is None and int(record['normalized'].get('status_id') or 0)!=3:
        return False
    checkpoint = conn.execute('SELECT * FROM sheet_vitrina_v1_ff_stock_wb_auto_writeoff_checkpoint WHERE slot=\'current\'').fetchone()
    if checkpoint is None:
        return False
    from packages.application.registry_upload_db_backed_runtime import _ff_stock_wb_auto_writeoff_checkpoint_to_dict
    checkpoint = _ff_stock_wb_auto_writeoff_checkpoint_to_dict(checkpoint)
    if _wb_supply_checkpoint_match_fields(checkpoint=checkpoint,cache_key=cache,supply_id=sid,source_key=f'wb_supply_debit:{cache}'):
        return False
    checkpoint_dt = _parse_datetime_like(checkpoint['created_at'])
    activation = conn.execute(f"SELECT created_at FROM {AGGREGATE} WHERE source_type <> 'wb_supply' AND total_quantity_delta > 0 ORDER BY created_at,operation_id LIMIT 1").fetchone()
    boundary=dt or cutoff
    return bool(checkpoint_dt and boundary >= checkpoint_dt and activation and boundary >= _parse_datetime_like(activation[0]))


def _origin(conn, cache):
    from packages.application.ff_wb_supply_origins import _current_assignment
    return _current_assignment(conn, cache)


def reservation_summary(conn, facility_id, *, exclude_cache_key=''):
    """Authoritative addressed obligations, counted once from the legacy ledger.

    CURRENT supplies the durable address, not another quantity operand. Pending
    availability is never a reservation and unresolved origins stay unassigned.
    """
    required={'sheet_vitrina_v1_ff_stock_reservation_operations','sheet_vitrina_v1_ff_stock_reservation_lines',
              'sheet_vitrina_v1_wb_supply_ff_origin_assignments'}
    if not required <= _tables(conn):
        return dict(quantity=0,by_nm_id={},updated_at='')
    states={}
    if CURRENT in _tables(conn):
        for row in conn.execute(f'SELECT cache_key,state_json FROM {CURRENT}'):
            states[str(row['cache_key']).removeprefix('supply:')]=json.loads(row['state_json'])
    totals={};updated=''
    for row in conn.execute('''SELECT operation.supply_id,line.nm_id,SUM(line.quantity_delta) quantity,MAX(operation.created_at) updated_at
        FROM sheet_vitrina_v1_ff_stock_reservation_operations operation JOIN sheet_vitrina_v1_ff_stock_reservation_lines line USING(operation_id)
        GROUP BY operation.supply_id,line.nm_id HAVING SUM(line.quantity_delta)>0'''):
        real=str(row['supply_id']).removeprefix('supply:')
        if exclude_cache_key and real==str(exclude_cache_key).removeprefix('supply:'):continue
        state=states.get(real) or {}
        origin=state.get('reservation_origin') or _origin(conn,'supply:'+real) or state.get('origin') or {}
        if origin.get('facility_id')!=facility_id:continue
        quantity=Decimal(str(row['quantity']))
        if quantity!=int(quantity):raise ValueError('fractional_fbo_reservation_requires_review')
        nm=int(row['nm_id']);totals[nm]=totals.get(nm,0)+int(quantity)
        updated=max(updated,str(row['updated_at'] or ''),str(origin.get('assigned_at') or ''))
    return dict(quantity=sum(totals.values()),by_nm_id=totals,updated_at=updated)


def _available(conn, facility, nm, cache, epoch):
    row = conn.execute(f"SELECT * FROM {BALANCES_TABLE} WHERE facility_id=? AND pool='FBO' AND nm_id=? AND projection_epoch=?", (facility, nm, epoch)).fetchone()
    physical = int(row['quantity']) if row else 0
    reserved=reservation_summary(conn,facility,exclude_cache_key=cache)['by_nm_id'].get(nm,0)
    return max(0, physical-reserved), dict(row) if row else None


def evaluate_owned(conn, record, *, origin_override=None):
    cache, sid = _identity(record)
    prior = _prior(conn, cache) or {}
    origin = origin_override if origin_override is not None else _origin(conn, cache)
    state = dict(contract=CONTRACT, supply_id=sid, wb_supply_id=str(record.get('wb_supply_id') or ''), cache_key=cache,
        status='disabled', reason='facility_pool_feature_off', origin=origin,
        reservation=prior.get('reservation', {}), reservation_origin=prior.get('reservation_origin'),
        reserved_qty=sum(prior.get('reservation', {}).values()), shortages=[], operation_id=prior.get('operation_id', ''),
        composition=prior.get('composition', {}), frozen_costs=prior.get('frozen_costs', []),
        supply=dict(record['normalized']), legacy_reserved_qty=sum(sum(v.values()) for v in _legacy(conn,sid).values()))
    if CONFIG in _tables(conn):
        config=conn.execute(f'SELECT cutoff_at,created_at FROM {CONFIG} WHERE slot=1').fetchone()
        if config: state.update(cutoff_at=config[0],activated_at=config[1])
    if not feature_enabled(conn):
        return state
    if not _scope(conn, record):
        historical=_existing(conn,record)
        state.update(status='out_of_scope',reason='explicit_lifecycle_cutover_required',operation_id=(historical or {}).get('operation_id',''))
        return state
    from packages.application.ff_stock_ledger import _record_is_confirmed_cancelled
    status = int(record['normalized'].get('status_id') or 0)
    cancelled = _record_is_confirmed_cancelled(record)
    try:
        goods = _goods(conn, record)
    except (ValueError, ArithmeticError) as exc:
        state.update(status='needs_correction',reason=str(exc))
        return state
    existing = _existing(conn, record)
    if existing:
        changed = bool(prior.get('composition') and prior['composition'] != goods)
        state.update(status='needs_correction' if cancelled or changed else 'debited',
            reason='cancelled_after_debit' if cancelled else 'composition_changed_after_debit' if changed else 'physical_debit_exists',
            operation_id=existing['operation_id'], composition=prior.get('composition') or goods)
        return state
    if cancelled:
        state.update(status='cancelled',reason='confirmed_cancelled_before_debit',composition=goods)
        return state
    if status not in {3,4,5,6}:
        state.update(status='waiting',reason='awaiting_shipping_permission',composition=goods)
        return state
    if not origin:
        state.update(status='missing_source',reason='needs_origin',composition=goods)
        return state
    facility = conn.execute(f'SELECT active FROM {FACILITIES_TABLE} WHERE facility_id=?',(origin['facility_id'],)).fetchone()
    if not facility or not facility[0]:
        state.update(status='needs_correction',reason='facility_inactive',composition=goods)
        return state
    epoch = read_ff_pool_feature_state(conn).epoch
    shortages=[]
    for nm,qty in goods.items():
        available,_ = _available(conn,origin['facility_id'],int(nm),cache,epoch)
        if available < qty:
            shortages.append(dict(nm_id=int(nm),required=qty,available=available,missing=qty-available))
    # A read can detect availability, but only a committed reservation is a
    # reservation. Keep pending work actionable until the write reconciles it.
    durable=state['reservation']==goods and (state.get('reservation_origin') or {}).get('facility_id')==origin['facility_id']
    actual={}
    for quantities in _legacy(conn,sid).values():
        for nm,qty in quantities.items(): actual[str(nm)]=actual.get(str(nm),0)+qty
    durable=durable and actual==goods
    state.update(status='shortage' if shortages else 'reserved' if durable else 'reservation_pending',reason='insufficient_fbo_stock' if shortages else 'reservation_reconciliation_required' if not durable else 'ready_for_dispatch' if status in {4,5,6} else 'awaiting_gate_shipment',shortages=shortages,composition=goods)
    return state


def _save(conn, state, now, *, admission='new_source'):
    cache,sid=state['cache_key'],state['supply_id']
    old = _prior(conn,cache)
    encoded=canonical(state)
    if old == state:
        return
    conn.execute(f'''INSERT INTO {CURRENT} VALUES(?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET state_json=excluded.state_json''',(cache,sid,now,admission,encoded))
    conn.execute(f'INSERT INTO {EVENTS}(cache_key,state_revision,state_json,occurred_at) VALUES(?,?,?,?)',(cache,digest(state),encoded,now))


def _replace_reservation(conn, state, desired, now):
    from packages.application.registry_upload_db_backed_runtime import _insert_ff_stock_reservation_operation
    aliases = _legacy(conn,state['supply_id'])
    # Consolidate alias ledgers without duplicating a migrated obligation.
    if state['supply_id'] not in aliases:
        aliases[state['supply_id']]={}
    for alias,current in aliases.items():
        target={int(k):v for k,v in desired.items()} if alias == state['supply_id'] else {}
        lines=[dict(nm_id=nm,quantity_delta=target.get(nm,0)-current.get(nm,0)) for nm in sorted(set(target)|set(current)) if target.get(nm,0)!=current.get(nm,0)]
        if not lines:
            continue
        sequence=conn.execute(f'SELECT COALESCE(MAX(sequence),0)+1 FROM {EVENTS}').fetchone()[0]
        key='fbores_'+digest(dict(cache=state['cache_key'],alias=alias,current=current,target=target,sequence=sequence))[:32]
        _insert_ff_stock_reservation_operation(conn,operation_id=key,source_key=key,supply_id=alias,supply_revision=digest(state['composition']),operation_type='adjust' if target else 'release',created_at=now,diagnostics=dict(contract=CONTRACT,origin=state.get('origin'),prior=current),lines=lines)
    state['reservation']=desired
    state['reservation_origin']=state['origin'] if desired else None
    state['reserved_qty']=sum(desired.values())
    state['legacy_reserved_qty']=sum(desired.values())


class FboSupplyLifecycle:
    def __init__(self, *, db_path: Path, timestamp_factory=None, runtime=None):
        self.db_path=Path(db_path)
        self.timestamp_factory=timestamp_factory or (lambda:datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z'))
        self.runtime=runtime

    @contextmanager
    def _connect(self, readonly=False):
        conn=sqlite3.connect(f'file:{self.db_path.resolve()}?mode={"ro" if readonly else "rw"}',uri=True,timeout=30)
        conn.row_factory=sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        if readonly: conn.execute('PRAGMA query_only=ON')
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def detail(self, supply_ref):
        with self._connect(True) as conn:
            if SUPPLIES not in _tables(conn): return dict(status='disabled',reason='schema_absent')
            return evaluate_owned(conn,_record(conn,supply_ref))

    def notifications(self):
        with self._connect(True) as conn:
            if CURRENT not in _tables(conn) or not feature_enabled(conn):
                return dict(status='disabled',notifications=[],items=[])
            refs=[r[0] for r in conn.execute(f'SELECT cache_key FROM {CURRENT} ORDER BY cache_key')]
            # Newly admitted sources can produce a notice before the next writer tick.
            refs += [r[0] for r in conn.execute(f"SELECT COALESCE(NULLIF(cache_key,''),supply_id) FROM {SUPPLIES} WHERE status_id IN (3,4,5,6)") if r[0] not in refs]
            notices=[]
            for ref in refs:
                state=evaluate_owned(conn,_record(conn,ref))
                if state['status'] not in {'missing_source','shortage','reservation_pending','needs_correction'}: continue
                state['id']='fbo:'+state['cache_key']+':'+state['reason']
                state['reason_revision']=digest({**{k:state[k] for k in ('cache_key','status','reason','shortages','composition')},'facility_id':(state['origin'] or {}).get('facility_id')})
                notices.append(state)
            return dict(status='ready',notifications=notices,items=notices)

    def migration_preview(self, *, supply_ids, cutoff_at):
        from packages.application.ff_stock_ledger import _parse_datetime_like
        if not supply_ids or len(supply_ids)>100 or _parse_datetime_like(cutoff_at) is None or not cutoff_at.endswith('Z'):
            raise ValueError('bounded_scope_and_utc_cutoff_required')
        with self._connect(True) as conn:
            effects=[]
            for ref in sorted(set(map(str,supply_ids))):
                record=_record(conn,ref);cache,sid=_identity(record)
                effects.append(dict(cache_key=cache,supply_id=sid,record_digest=digest(record),composition=_goods(conn,record),legacy_reservations=_legacy(conn,sid),existing_debit=_existing(conn,record),origin=_origin(conn,cache)))
            plan=dict(contract=CONTRACT,cutoff_at=cutoff_at,effects=effects,cache_baseline=_cache_baseline(conn))
            return {**plan,'plan_digest':digest(plan)}

    def migration_apply(self, plan, *, actor, on_activate=None):
        now=self.timestamp_factory()
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE');ensure_schema(conn)
            if not feature_enabled(conn): raise ValueError('facility_pool_feature_off')
            existing=conn.execute(f'SELECT * FROM {CONFIG} WHERE slot=1').fetchone()
            if existing:
                if existing['plan_digest'] != plan.get('plan_digest'): raise ValueError('lifecycle_cutover_already_applied')
                return dict(status='applied',idempotent=True,plan_digest=plan['plan_digest'])
            if digest({k:v for k,v in plan.items() if k!='plan_digest'}) != plan.get('plan_digest'): raise ValueError('migration_plan_changed')
            if _cache_baseline(conn)!=plan.get('cache_baseline'):
                raise ValueError('migration_cache_baseline_changed')
            for effect in plan['effects']:
                record=_record(conn,effect['cache_key'])
                if digest(record)!=effect['record_digest'] or canonical(_legacy(conn,record['supply_id']))!=canonical(effect['legacy_reservations']) or _existing(conn,record)!=effect['existing_debit'] or _origin(conn,record['cache_key'])!=effect['origin']:
                    raise ValueError('migration_source_changed')
            conn.execute(f'INSERT INTO {CONFIG} VALUES(1,?,?,?,?)',(plan['cutoff_at'],plan['plan_digest'],actor,now))
            conn.executemany(f'INSERT INTO {BASELINE} VALUES(?,?,?,?)',[(row['cache_key'],row['supply_id'],row['wb_supply_id'],row['preorder_id']) for row in plan['cache_baseline']])
            for effect in plan['effects']:
                # Admission is a boundary only: no origin guessed, no physical replay.
                initial=dict(status='missing_source',reason='needs_origin',reservation={},reservation_origin=None,composition=effect['composition'],operation_id=(effect['existing_debit'] or {}).get('operation_id',''),cache_key=effect['cache_key'],supply_id=effect['supply_id'])
                _save(conn,initial,now,admission='explicit_cutover')
                state=evaluate_owned(conn,_record(conn,effect['cache_key']))
                _save(conn,state,now,admission='explicit_cutover')
            if on_activate is not None:
                on_activate(conn, now)
            conn.commit()
            return dict(status='applied',idempotent=False,plan_digest=plan['plan_digest'],supply_count=len(plan['effects']))

    def reconcile(self, supply_ref):
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            result=self.reconcile_owned(conn,supply_ref)
            conn.commit()
            return result

    def reconcile_owned(self, conn, supply_ref, *, origin_change=False):
        if not conn.in_transaction: raise ValueError('owned_transaction_required')
        record=_record(conn,supply_ref)
        state=evaluate_owned(conn,record)
        prior_operation=state.get('operation_id')
        if state['status'] in {'disabled','out_of_scope'}: return state
        ensure_schema(conn)
        now=self.timestamp_factory()
        if origin_change and (state['status']=='needs_correction' or state.get('operation_id')):
            raise ValueError('origin_change_requires_explicit_correction')
        if origin_change and state['status']=='shortage' and state['reservation']:
            raise ValueError('origin_change_shortage_preserves_prior_reservation:'+canonical(state['shortages']))
        if state['status'] in {'reserved','reservation_pending'}:
            _replace_reservation(conn,state,state['composition'],now)
            state.update(status='reserved',reason='awaiting_gate_shipment')
            if int(record['normalized'].get('status_id') or 0) in {4,5,6}:
                self._dispatch(conn,record,state,now)
        elif state['status']=='shortage' and origin_change and not state.get('reservation_origin') and not state['reservation']:
            # Explicit source selection completes the unscoped migration. A
            # whole-supply shortage must not turn its old unscoped obligation
            # into an over-reservation at the newly selected facility.
            # The immutable release + unresolved shortage retain the evidence.
            _replace_reservation(conn,state,{},now)
        elif state['status'] in {'cancelled','debited'}:
            _replace_reservation(conn,state,{},now)
        _save(conn,state,now)
        return {**state, 'idempotent': bool(prior_operation)}

    def _dispatch(self, conn, record, state, now):
        from packages.application.current_fbo_supply_close import allocate_capital
        from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
        epoch=read_ff_pool_feature_state(conn).epoch
        facility=state['origin']['facility_id']
        lines=[]
        for nm,qty in state['composition'].items():
            available,balance=_available(conn,facility,int(nm),state['cache_key'],epoch)
            if available < qty: raise ValueError('fbo_dispatch_reservation_changed')
            if balance is None or Decimal(balance['capital_rub']) <= 0:
                state.update(status='needs_correction',reason='exact_selected_pool_cost_missing')
                return
            allocation=allocate_capital(balance['quantity'],balance['capital_rub'],qty)
            lines.append(dict(nm_id=int(nm),quantity_delta=-qty,facility_id=facility,pool='FBO',**allocation,
                raw=dict(cost_snapshot=dict(unit_cost_rub=allocation['wac_rub'],capital_delta_rub=allocation['capital_delta_rub'],quality='exact_selected_fbo_pool',provenance=dict(contract=CONTRACT,facility_id=facility,pool='FBO',allocation=allocation)))))
        from packages.business_time import business_date_from_timestamp
        business_date=business_date_from_timestamp(now)
        key=f"wb_supply_debit:{state['cache_key']}"
        op='fbod_'+digest(key)[:32]
        expected={l['nm_id']:conn.execute(f'SELECT COALESCE(SUM(quantity_delta),0) FROM {AGGREGATE_LINES} WHERE nm_id=?',(l['nm_id'],)).fetchone()[0] for l in lines}
        runtime=self.runtime or RegistryUploadDbBackedRuntime(runtime_dir=self.db_path.parent,operational_db_path=self.db_path)
        runtime.create_ff_stock_operation_guarded(connection=conn,operation_id=op,operation_type='auto_writeoff',source_type='wb_supply',source_key=key,source_object_id=state['supply_id'],source_object_label=state['wb_supply_id'],created_at=now,created_by='system',business_effective_date=business_date,warnings=[],diagnostics=dict(contract=CONTRACT,origin=state['origin'],composition=state['composition']),lines=lines,expected_balances=expected)
        conn.execute(f'INSERT INTO {OPERATIONS_TABLE} VALUES(?,?,?,?,?,?,?,?,?,?)',(op,'wb_supply_dispatch',CONTRACT,'wb_supply',key,digest(state['composition']),epoch,business_date,now,canonical(dict(supply_id=state['supply_id'],origin=state['origin'],composition=state['composition']))))
        for number,line in enumerate(lines,1):
            conn.execute(f'INSERT INTO {LINES_TABLE} VALUES(?,?,?,?,?,?,?,?,?)',(op,number,facility,'FBO',line['nm_id'],line['quantity_delta'],line['capital_delta_rub'],line['wac_rub'],canonical(line)))
            wac=canonical_decimal_ratio_text(line['after_capital_rub'],line['after_quantity']) if line['after_quantity'] else None
            changed=conn.execute(f"UPDATE {BALANCES_TABLE} SET quantity=?,capital_rub=?,wac_rub=?,source_watermark=?,updated_at=? WHERE facility_id=? AND pool='FBO' AND nm_id=? AND projection_epoch=? AND quantity=? AND capital_rub=?",(line['after_quantity'],line['after_capital_rub'],wac,op,now,facility,line['nm_id'],epoch,line['before_quantity'],line['before_capital_rub']))
            if changed.rowcount!=1: raise ValueError('fbo_pool_balance_changed')
        _replace_reservation(conn,state,{},now)
        state.update(status='debited',reason='gate_shipment_debited',operation_id=op,frozen_costs=lines)
