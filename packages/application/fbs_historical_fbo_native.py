"""Bounded native append seam; deliberately not connected to HTTP or a job.

This posts honest foundation operations, not synthetic receipts or corrections.
Publication and typed historical-book admission remain separate obligations.
"""
from contextlib import closing
from decimal import Decimal, localcontext
import json

from packages.application.fbs_snapshot_cost import fingerprint, canonical
from packages.application.fbs_historical_fbo_dispatch import validate_historical_fbo_dispatch_plan
from packages.application.ff_pool_foundation import BALANCES_TABLE, OPERATIONS_TABLE, LINES_TABLE, FEATURE_EPOCHS_TABLE, FACILITIES_TABLE
from packages.application.warehouse_functional_lock import require_warehouse_job_owner, warehouse_functional_write_lock
from packages.application.fbs_accounting_runtime import writer_lock, load, SCHEMA

CONTRACT = 'historical_fbo_native_v1'
AGGREGATE = 'sheet_vitrina_v1_ff_stock_operations'
AGGREGATE_LINES = 'sheet_vitrina_v1_ff_stock_operation_lines'


def require(value, code):
    if not value:
        raise ValueError('historical_fbo_native_' + code)


def operation_id(plan, index):
    return 'historical_fbo:' + fingerprint([plan['plan_digest'], index])[7:]


def operation_metadata(plan, index):
    return dict(contract=CONTRACT, plan_digest=plan['plan_digest'], source_proof=plan['source_proof'],
                effect_index=index, effect=plan['effects'][index],
                protected_source_fingerprints=plan['protected_source_fingerprints'],
                **({'plan_manifest':plan} if index == 0 else {}))


def cost_snapshot(plan, effect, line):
    return dict(unit_cost_rub=line['wac_rub'], capital_delta_rub=line['capital_delta_rub'],
                quality='exact_historical_fbo_dispatch_wac', source_business_date=effect['business_date'],
                source_plan_fingerprint=plan['plan_digest'], source_version_id=plan['source_proof']['book_version'])


def movement_metadata(plan, index, line):
    return dict(contract=CONTRACT, plan_digest=plan['plan_digest'], effect_index=index,
                valuation_basis_id=line['valuation_basis_id'],
                cost_snapshot=cost_snapshot(plan, plan['effects'][index], line))


def capture_native_guard(conn, plan):
    """Exact current row fence, including other facilities and reservations.

    Missing rows remain missing. The full selected aggregate line set is fenced,
    including operation identity and frozen cost; sums alone cannot hide drift.
    """
    require(conn.in_transaction, 'transaction_required')
    nms = sorted({l['nm_id'] for e in plan['effects'] for l in e['pool_movements']})
    require(0 < len(nms) <= 500, 'scope_invalid')
    slots = ','.join('?' for _ in nms)
    queries = [
        ('epoch', f'SELECT * FROM {FEATURE_EPOCHS_TABLE} ORDER BY epoch DESC LIMIT 1', ()),
        ('balances', f'SELECT * FROM {BALANCES_TABLE} WHERE nm_id IN ({slots}) ORDER BY facility_id,pool,nm_id', nms),
        ('aggregate', f'SELECT o.*,l.* FROM {AGGREGATE} o JOIN {AGGREGATE_LINES} l USING(operation_id) WHERE l.nm_id IN ({slots}) ORDER BY o.operation_id,l.line_no', nms),
        ('reservations', f'SELECT o.*,l.* FROM sheet_vitrina_v1_ff_stock_reservation_operations o JOIN sheet_vitrina_v1_ff_stock_reservation_lines l USING(operation_id) WHERE l.nm_id IN ({slots}) ORDER BY o.operation_id,l.line_no', nms),
    ]
    # Fence every authoritative input consulted by the normal pool reservation
    # guard, including negative evidence (no lifecycle, mapping or origin).
    from packages.application.wb_fbs_orders import OBSERVATIONS_TABLE, STATUS_CURRENT_TABLE, WAREHOUSE_MAPPINGS_TABLE
    from packages.application.ff_pool_fbs_lifecycle import CURRENT_TABLE, EVENTS_TABLE, RECONCILIATION_TABLE
    from packages.application.ff_pool_fbs_applicability import APPLICABILITY_EVENTS_TABLE
    from packages.application.ff_wb_supply_origins import ASSIGNMENTS_TABLE
    from packages.application.ff_pool_cutover import MANIFESTS_TABLE
    facilities = sorted({l['facility_id'] for e in plan['effects'] for l in e['pool_movements']})
    facility_slots = ','.join('?' for _ in facilities)
    selected_orders = f'SELECT order_id FROM {OBSERVATIONS_TABLE} WHERE nm_id IN ({slots})'
    selected_reservations = ('SELECT o.supply_id FROM sheet_vitrina_v1_ff_stock_reservation_operations o '
                             'JOIN sheet_vitrina_v1_ff_stock_reservation_lines l USING(operation_id) '
                             f'WHERE l.nm_id IN ({slots})')
    queries.extend([
        ('facilities', f'SELECT * FROM {FACILITIES_TABLE} WHERE facility_id IN ({facility_slots}) ORDER BY facility_id',facilities),
        ('nomenclature', f'SELECT * FROM sheet_vitrina_v1_nomenclature_items WHERE nm_id IN ({slots}) ORDER BY item_id',nms),
        ('fbs_lifecycle', f'SELECT * FROM {CURRENT_TABLE} WHERE nm_id IN ({slots}) ORDER BY cutover_id,order_id',nms),
        ('fbs_cutovers', f'SELECT * FROM {MANIFESTS_TABLE} WHERE cutover_id IN (SELECT cutover_id FROM {CURRENT_TABLE} WHERE nm_id IN ({slots})) ORDER BY cutover_id',nms),
        ('fbs_events', f'SELECT * FROM {EVENTS_TABLE} WHERE nm_id IN ({slots}) ORDER BY event_sequence',nms),
        ('fbs_reconciliation', f'SELECT lane.* FROM {RECONCILIATION_TABLE} lane JOIN {EVENTS_TABLE} event USING(event_id) WHERE event.nm_id IN ({slots}) ORDER BY lane.reconciliation_id',nms),
        ('fbs_observations', f'SELECT * FROM {OBSERVATIONS_TABLE} WHERE order_id IN ({selected_orders}) ORDER BY observation_sequence',nms),
        ('fbs_statuses', f'SELECT * FROM {STATUS_CURRENT_TABLE} WHERE order_id IN ({selected_orders}) ORDER BY order_id',nms),
        ('fbs_warehouse_mappings', f'SELECT * FROM {WAREHOUSE_MAPPINGS_TABLE} ORDER BY seller_warehouse_id,mapping_id',()),
        ('fbs_applicability', f'SELECT * FROM {APPLICABILITY_EVENTS_TABLE} WHERE nm_id IN ({slots}) ORDER BY event_sequence',nms),
        ('reservation_origins', f'SELECT * FROM {ASSIGNMENTS_TABLE} WHERE wb_supply_id IN ({selected_reservations}) OR wb_supply_cache_key IN ({selected_reservations}) ORDER BY assignment_sequence',[*nms,*nms]),
    ])
    result = {}
    for key,sql,args in queries:
        rows = [dict(r) for r in conn.execute(sql, args).fetchmany(100001)]
        require(len(rows) <= 100000, 'guard_row_limit')
        result[key] = rows
    require(len(canonical(result).encode('utf-8')) <= 64 * 1024**2, 'guard_size_limit')
    result['digest'] = fingerprint(result)
    return result


def _verify_existing(conn, plan):
    require(plan.get('status') == 'prepared_unconnected' and len(plan['effects']) == 4
            and fingerprint({k:v for k,v in plan.items() if k != 'plan_digest'}) == plan['plan_digest'],
            'manifest_digest_invalid')
    ids = [operation_id(plan, i) for i in range(len(plan['effects']))]
    found = [conn.execute(f'SELECT * FROM {OPERATIONS_TABLE} WHERE operation_id=?', (id_,)).fetchone() for id_ in ids]
    if not any(found):
        # Canonical aggregate aliases are checked independently before any write.
        for e in plan['effects']:
            if e['aggregate_movements']:
                require(conn.execute(f'SELECT 1 FROM {AGGREGATE} WHERE source_key=? OR operation_id=?',
                                     (e['source_key'], ids[plan['effects'].index(e)])).fetchone() is None,
                        'canonical_debit_already_exists')
        return False
    require(all(found), 'partial_existing_operation')
    for index, (row, effect) in enumerate(zip(found, plan['effects'])):
        id_ = ids[index]
        source = effect.get('source_key') or 'pool_reallocation:' + effect['source_digest']
        require(row['operation_type'] == effect['kind'] and row['source_system'] == 'historical_fbo_dispatch'
                and row['source_type'] == effect['kind'] and row['source_id'] == source
                and row['source_revision'] == plan['plan_digest'] and row['business_date'] == effect['business_date']
                and json.loads(row['metadata_json']) == operation_metadata(plan, index), 'existing_identity_conflict')
        actual = [dict(r) for r in conn.execute(f'SELECT * FROM {LINES_TABLE} WHERE operation_id=? ORDER BY line_no', (id_,))]
        expected = [dict(operation_id=id_, line_no=n, facility_id=l['facility_id'], pool=l['pool'], nm_id=l['nm_id'],
                         quantity_delta=l['quantity_delta'], capital_delta_rub=l['capital_delta_rub'],
                         wac_snapshot_rub=l['wac_rub'], metadata_json=canonical(movement_metadata(plan,index,l)))
                    for n,l in enumerate(effect['pool_movements'],1)]
        require(actual == expected, 'existing_pool_lines_conflict')
        agg = conn.execute(f'SELECT * FROM {AGGREGATE} WHERE operation_id=?', (id_,)).fetchone()
        if effect['aggregate_movements']:
            require(agg is not None and agg['source_key'] == effect['source_key'] and agg['source_type'] == 'wb_supply'
                    and agg['operation_type'] == 'writeoff' and agg['source_object_id'] == effect['source_key'].rsplit(':',1)[1]
                    and agg['business_effective_date'] == effect['business_date']
                    and json.loads(agg['diagnostics_json']) == operation_metadata(plan,index)
                    and agg['sku_count'] == len({l['nm_id'] for l in effect['aggregate_movements']})
                    and agg['total_quantity_delta'] == sum(l['quantity_delta'] for l in effect['aggregate_movements'])
                    and agg['total_quantity_abs'] == sum(abs(l['quantity_delta']) for l in effect['aggregate_movements'])
                    and agg['created_at'] == row['posted_at'], 'existing_aggregate_conflict')
            lines = [dict(r) for r in conn.execute(f'SELECT * FROM {AGGREGATE_LINES} WHERE operation_id=? ORDER BY line_no',(id_,))]
            require(len(lines) == len(effect['aggregate_movements']), 'existing_aggregate_lines_conflict')
            for n,(a,l) in enumerate(zip(lines,effect['aggregate_movements']),1):
                expected_raw = {**l, 'cost_snapshot':cost_snapshot(plan,effect,l)}
                require(a['line_no'] == n and a['nm_id'] == l['nm_id'] and a['quantity_delta'] == l['quantity_delta']
                        and json.loads(a['raw_json']) == expected_raw, 'existing_aggregate_lines_conflict')
        else:
            require(agg is None, 'unexpected_reallocation_aggregate')
    return True


def _parity(conn, nms, epoch):
    for nm in nms:
        rows = list(conn.execute(f'SELECT quantity,projection_epoch FROM {BALANCES_TABLE} WHERE nm_id=?',(nm,)))
        require(rows and all(r['projection_epoch'] == epoch for r in rows), 'projection_scope_missing_or_stale')
        total = conn.execute(f'SELECT COALESCE(SUM(quantity_delta),0) FROM {AGGREGATE_LINES} WHERE nm_id=?',(nm,)).fetchone()[0]
        require(Decimal(str(total)) == sum(Decimal(r['quantity']) for r in rows), 'pool_aggregate_quantity_mismatch')


def _append(conn, runtime, plan, *, posted_at, actor, fault):
    epoch_row = conn.execute(f'SELECT * FROM {FEATURE_EPOCHS_TABLE} ORDER BY epoch DESC LIMIT 1').fetchone()
    require(epoch_row and epoch_row['writer_enabled'] == 1, 'writer_disabled')
    epoch = epoch_row['epoch']
    nms = sorted({l['nm_id'] for e in plan['effects'] for l in e['pool_movements']})
    _parity(conn,nms,epoch)
    from packages.application.operator_warehouse_documents import assert_reservations
    # Gross debits intentionally do not spend the later reallocation credit.
    # This conservative cohort check runs before the first durable append.
    assert_reservations(conn,plan={'documents':[{'movements':e['pool_movements']} for e in plan['effects']]},epoch=epoch)
    for facility in {l['facility_id'] for e in plan['effects'] for l in e['pool_movements']}:
        facility_row = conn.execute(f'SELECT active FROM {FACILITIES_TABLE} WHERE facility_id=?',(facility,)).fetchone()
        require(facility_row is not None and facility_row['active'] == 1, 'facility_inactive_or_missing')
    for index,effect in enumerate(plan['effects']):
        require(effect['expense_rub'] == '0', 'unexpected_expense')
        id_ = operation_id(plan,index)
        source = effect.get('source_key') or 'pool_reallocation:' + effect['source_digest']
        conn.execute(f'INSERT INTO {OPERATIONS_TABLE} VALUES(?,?,?,?,?,?,?,?,?,?)',
            (id_,effect['kind'],'historical_fbo_dispatch',effect['kind'],source,plan['plan_digest'],epoch,
             effect['business_date'],posted_at,canonical(operation_metadata(plan,index))))
        for n,line in enumerate(effect['pool_movements'],1):
            key = (line['facility_id'],line['pool'],line['nm_id'])
            row = conn.execute(f'SELECT * FROM {BALANCES_TABLE} WHERE facility_id=? AND pool=? AND nm_id=?',key).fetchone()
            require(row is not None and row['projection_epoch'] == epoch, 'selected_projection_missing')
            if line['pool'] == 'FBS':
                from packages.application.ff_pool_fbs_applicability import require_fbs_pair_writeable
                require_fbs_pair_writeable(conn,facility_id=line['facility_id'],nm_id=line['nm_id'],
                                          effective_date=effect['business_date'],projection_epoch=epoch)
            q = row['quantity'] + line['quantity_delta']
            # The daily valuation is precision 50; an existing native pool
            # may carry longer authoritative tails. Preserve its money when
            # adding the already-frozen effect, without repricing that effect.
            with localcontext() as current_context:
                current_context.prec = 160
                capital = Decimal(row['capital_rub']) + Decimal(line['capital_delta_rub'])
                require(q >= 0 and capital >= 0 and ((q == 0) == (capital == 0)), 'current_projection_would_be_invalid')
            # WAC is a derived ratio with the existing precision-50 policy;
            # authoritative capital itself stays exact.
            current_wac = format(capital / q, 'f') if q else None
            require(Decimal(line['wac_rub']) > 0
                    and Decimal(line['capital_delta_rub']) == Decimal(line['wac_rub']) * line['quantity_delta'], 'frozen_money_mismatch')
            conn.execute(f'INSERT INTO {LINES_TABLE} VALUES(?,?,?,?,?,?,?,?,?)',
                (id_,n,*key,line['quantity_delta'],line['capital_delta_rub'],line['wac_rub'],canonical(movement_metadata(plan,index,line))))
            conn.execute(f'UPDATE {BALANCES_TABLE} SET quantity=?,capital_rub=?,wac_rub=?,source_watermark=?,updated_at=? WHERE facility_id=? AND pool=? AND nm_id=? AND projection_epoch=?',
                (q,format(capital,'f'),current_wac,id_,posted_at,*key,epoch))
        if fault:
            fault('after_pool',index)
        if effect['aggregate_movements']:
            lines = [{**l,'raw':{**l,'cost_snapshot':cost_snapshot(plan,effect,l)}} for l in effect['aggregate_movements']]
            balances = {l['nm_id']:conn.execute(f'SELECT COALESCE(SUM(quantity_delta),0) FROM {AGGREGATE_LINES} WHERE nm_id=?',(l['nm_id'],)).fetchone()[0] for l in lines}
            supply = effect['source_key'].rsplit(':',1)[1]
            reservation = conn.execute('SELECT COALESCE(SUM(l.quantity_delta),0) FROM sheet_vitrina_v1_ff_stock_reservation_lines l JOIN sheet_vitrina_v1_ff_stock_reservation_operations o USING(operation_id) WHERE o.supply_id=?',(supply,)).fetchone()[0]
            require(reservation == 0, 'selected_supply_reservation_requires_transition')
            runtime.create_ff_stock_operation_guarded(connection=conn,operation_id=id_,operation_type='writeoff',
                source_type='wb_supply',source_key=effect['source_key'],source_object_id=supply,source_object_label=supply,
                created_at=posted_at,created_by=actor,business_effective_date=effect['business_date'],warnings=[],
                diagnostics=operation_metadata(plan,index),lines=lines,expected_balances=balances)
        if fault:
            fault('after_aggregate',index)
    _parity(conn,nms,epoch)
    require(_verify_existing(conn,plan), 'post_write_readback_failed')


def apply_historical_fbo_native(runtime, *, plan, capture, scope, expected_native_guard,
                                recapture, owner_token, posted_at, actor, fault=None, pre_write_guard=None):
    """Post all four effects or none, using a fresh capture on the same connection.

    `recapture(conn)` must perform the bounded immutable source queries and return
    the complete planner capture. There is no default or current-cost fallback.
    On uncertain response call this SAME identity: exact durable readback is
    returned without a new append. This function does not publish any book/day.
    """
    validate_historical_fbo_dispatch_plan(plan,capture=capture,scope=scope)
    require(len(plan['effects']) == 4, 'four_effects_required')
    require(type(actor) is str and 0 < len(actor.strip()) <= 160, 'actor_invalid')
    require_warehouse_job_owner(runtime.runtime_dir,owner_token)
    from packages.application.registry_upload_db_backed_runtime import _connect
    with warehouse_functional_write_lock(runtime.runtime_dir), writer_lock(runtime.runtime_dir), closing(_connect(runtime.db_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            existing = _verify_existing(conn,plan)
            if not existing:
                book, book_version = load(runtime.runtime_dir)
                require(book_version == plan['source_proof']['book_version'], 'book_compare_and_swap_failed')
                require(isinstance(book,dict) and book.get('schema') == SCHEMA
                        and book.get('active') is True and not book.get('publication_error'),
                        'healthy_active_book_required')
                fresh = recapture(conn)
                validate_historical_fbo_dispatch_plan(plan,capture=fresh,scope=scope)
                require(capture_native_guard(conn,plan) == expected_native_guard, 'current_source_compare_and_swap_failed')
                if pre_write_guard is not None:
                    pre_write_guard(conn)
                with localcontext() as context:
                    context.prec = 50
                    _append(conn,runtime,plan,posted_at=posted_at,actor=actor,fault=fault)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return dict(contract=CONTRACT,status='native_committed_pending_publication',idempotent=existing,
                plan_digest=plan['plan_digest'],operation_ids=[operation_id(plan,i) for i in range(4)],
                publication_complete=False,expense_rub='0')
