"""Current-day, source-linked FBO dispatches; caller owns admission and commit.

This is an explicitly current-cost recovery, not a historical restatement.
Official WB stock is already physical truth and is never increased here.
"""
from decimal import Decimal, localcontext
import hashlib
import json
import sqlite3

from packages.application.ff_pool_foundation import (
    BALANCES_TABLE, OPERATIONS_TABLE, LINES_TABLE, FEATURE_EPOCHS_TABLE,
    FACILITIES_TABLE, evaluate_ff_pool_aggregate_parity, canonical_decimal_ratio_text,
)
from packages.application import ff_stock_ledger as ledger
from packages.application.registry_upload_db_backed_runtime import _wb_supply_record_from_row
from packages.application.warehouse_fbs_material_rematerialization import publish_fbs_pool_aggregate_revision
from packages.business_time import business_date_from_timestamp

CONTRACT = 'current_fbo_supply_close_v1'
RECEIPTS = 'sheet_vitrina_v1_ff_pool_current_supply_close_receipts'
AGGREGATE = 'sheet_vitrina_v1_ff_stock_operations'
AGGREGATE_LINES = 'sheet_vitrina_v1_ff_stock_operation_lines'
RESERVATIONS = 'sheet_vitrina_v1_ff_stock_reservation_operations'
RESERVATION_LINES = 'sheet_vitrina_v1_ff_stock_reservation_lines'
ACTIVE = 'sheet_vitrina_v1_warehouse_functional_active'
ACTIVE_BALANCES = 'sheet_vitrina_v1_warehouse_functional_balances'


def require(value, reason):
    if not value:
        raise ValueError('current_fbo_' + reason)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return 'sha256:' + hashlib.sha256(canonical(value).encode()).hexdigest()


def rows(conn, table, where='1', args=()):
    result = [dict(r) for r in conn.execute(f'SELECT * FROM {table} WHERE {where} ORDER BY rowid', args)]
    require(len(result) <= 20000, 'source_row_budget')
    return result


def ensure_receipts(conn):
    conn.execute(f'CREATE TABLE IF NOT EXISTS {RECEIPTS}(operation_id TEXT PRIMARY KEY, plan_digest TEXT NOT NULL, result_json TEXT NOT NULL)')


def allocate_capital(quantity, capital, debit):
    """Freeze a proportional current amount; full depletion spends exact capital."""
    capital = Decimal(str(capital))
    require(type(quantity) is int and type(debit) is int and 0 < debit <= quantity,
            'quantity_invalid')
    require(capital.is_finite() and capital > 0 and len(str(capital)) <= 100, 'capital_invalid')
    with localcontext() as ctx:
        ctx.prec = 50
        amount = capital if debit == quantity else capital * Decimal(debit) / Decimal(quantity)
        wac = amount / debit
    with localcontext() as ctx:
        ctx.prec = 160
        remainder = capital - amount
        require(amount > 0 and remainder >= 0 and (quantity == debit) == (remainder == 0)
                and amount + remainder == capital, 'capital_conservation')
    return dict(capital_delta_rub=format(amount.copy_negate(), 'f'), wac_rub=format(wac, 'f'),
                before_capital_rub=format(capital, 'f'), after_capital_rub=format(remainder, 'f'),
                before_quantity=quantity, after_quantity=quantity-debit,
                capital_policy='current_proportional_exact_remainder_v1')


def _parity(conn):
    values = [dict(r) for r in conn.execute(f"SELECT b.nm_id,b.quantity,b.capital_rub FROM {ACTIVE_BALANCES} b JOIN {ACTIVE} a USING(version_id) WHERE a.slot=1 AND b.warehouse_key='ff' ORDER BY b.nm_id")]
    proof = evaluate_ff_pool_aggregate_parity(conn, values)
    require(values and proof.status == 'pass', 'canonical_pool_parity')
    return values


def prepare(conn, *, operation_id, facility_id, supply_ids, business_date):
    require(conn.in_transaction and conn.row_factory is sqlite3.Row, 'bound_snapshot_required')
    require(isinstance(operation_id, str) and 0 < len(operation_id) <= 120, 'operation_identity')
    require(isinstance(supply_ids, list) and 0 < len(supply_ids) <= 3
            and len(set(supply_ids)) == len(supply_ids), 'supply_scope')
    epoch = conn.execute(f'SELECT * FROM {FEATURE_EPOCHS_TABLE} ORDER BY epoch DESC LIMIT 1').fetchone()
    require(epoch and epoch['writer_enabled'] == 1, 'writer_disabled')
    facility = conn.execute(f'SELECT * FROM {FACILITIES_TABLE} WHERE facility_id=?', (facility_id,)).fetchone()
    require(facility and facility['active'] == 1, 'facility_inactive')
    active = conn.execute(f'SELECT a.*,v.business_effective_date,v.status FROM {ACTIVE} a JOIN sheet_vitrina_v1_warehouse_functional_versions v USING(version_id) WHERE a.slot=1').fetchone()
    require(active and active['status'] == 'good' and active['business_effective_date'] == business_date,
            'current_active_date_required')
    aggregate = _parity(conn)
    catalog = rows(conn, 'sheet_vitrina_v1_nomenclature_items')
    nomenclature = {int(r['nm_id']): r for r in catalog if r.get('is_active', 1) and not r.get('is_hidden', 0)}
    balances = rows(conn, BALANCES_TABLE)
    simulated = {(r['facility_id'], r['pool'], r['nm_id']): dict(r) for r in balances}
    supplies, effects = [], []
    for supply_id in supply_ids:
        candidates = list(conn.execute('SELECT * FROM sheet_vitrina_v1_wb_supplies WHERE supply_id=? OR wb_supply_id=? OR cache_key=? ORDER BY supply_id',
            (supply_id,supply_id,'supply:'+supply_id)))
        require(len(candidates)==1, 'supply_missing_or_ambiguous')
        raw=candidates[0]
        record = _wb_supply_record_from_row(raw)
        normalized = record['normalized']
        cache_key, source_id, source_key = ledger._wb_supply_debit_identity(record=record, normalized=normalized)
        require(source_id == supply_id and source_key and cache_key, 'supply_identity')
        require(ledger._optional_int(normalized.get('status_id')) in ledger.WB_DEBIT_STATUS_IDS
                and ledger._optional_int(normalized.get('virtual_type_id')) != ledger.WB_SKIP_VIRTUAL_TYPE_ID
                and normalized.get('type_label') != ledger.WB_SKIP_TYPE_LABEL, 'ordinary_physical_supply_required')
        existing = conn.execute(f'SELECT 1 FROM {AGGREGATE} WHERE source_key IN (?,?,?) OR (source_type=\'wb_supply\' AND source_object_id=?)',
            (source_key, 'wb_supply_debit:supply:'+supply_id, 'wb_supply_debit:'+supply_id, supply_id)).fetchone()
        require(existing is None, 'source_already_debited')
        from packages.application.wb_supply_box_correction import load_active_box_correction
        require(load_active_box_correction(conn, supply_id) is None, 'box_corrected_supply_separate_review')
        goods = record.get('raw_goods')
        require(isinstance(goods, list) and 0 < len(goods) <= 500, 'goods_scope')
        lines, warnings = ledger._wb_supply_goods_lines(goods, nomenclature)
        require(lines and not warnings, 'goods_canonical_identity')
        require(len(lines) <= 100, 'sku_scope')
        revision = ledger._wb_supply_revision(record, normalized)
        amounts = {}
        frozen = []
        for item in sorted(lines, key=lambda r:r['nm_id']):
            nm = int(item['nm_id']); value = Decimal(str(item['quantity_delta']))
            require(value < 0 and value == value.to_integral_value(), 'whole_unit_required')
            debit = -int(value); amounts[nm] = debit
            key = (facility_id, 'FBO', nm); before = simulated.get(key)
            require(before is not None and before['projection_epoch'] == epoch['epoch'], 'pool_source_missing')
            money = allocate_capital(before['quantity'], before['capital_rub'], debit)
            frozen.append(dict(nm_id=nm, quantity_delta=-debit, facility_id=facility_id, pool='FBO',
                               source_revision=revision, **money))
            before.update(quantity=money['after_quantity'], capital_rub=money['after_capital_rub'])
        reservation = {}
        for r in conn.execute(f'SELECT l.nm_id,SUM(l.quantity_delta) FROM {RESERVATIONS} o JOIN {RESERVATION_LINES} l USING(operation_id) WHERE o.supply_id=? GROUP BY l.nm_id HAVING SUM(l.quantity_delta)<>0', (supply_id,)):
            value = Decimal(str(r[1]))
            require(value > 0 and value == value.to_integral_value(), 'reservation_quantity_invalid')
            reservation[int(r[0])] = int(value)
        require(not reservation or reservation == amounts, 'exact_supply_reservation_required')
        supplies.append(dict(row=dict(raw), revision=revision, source_key=source_key))
        effects.append(dict(supply_id=supply_id, source_key=source_key, revision=revision,
                            reservation={str(k):v for k,v in reservation.items()}, lines=frozen))
    nms = sorted({line['nm_id'] for effect in effects for line in effect['lines']})
    slots = ','.join('?' for _ in nms)
    # Freeze all outstanding reservation/origin evidence read by the ordinary guard.
    sources = dict(epoch=dict(epoch), facility=dict(facility), active=dict(active), aggregate=aggregate,
        active_balances=rows(conn, ACTIVE_BALANCES, 'version_id=?', (active['version_id'],)),
        active_snapshots=rows(conn, 'sheet_vitrina_v1_warehouse_wb_snapshots', 'version_id=?', (active['version_id'],)),
        balances=balances, nomenclature=catalog, supplies=supplies,
        reservations=rows(conn, RESERVATIONS), reservation_lines=rows(conn, RESERVATION_LINES),
        origins=rows(conn, 'sheet_vitrina_v1_wb_supply_ff_origin_assignments'),
        aggregate_lines=rows(conn, AGGREGATE_LINES, f'nm_id IN ({slots})', nms))
    plan = dict(contract=CONTRACT, operation_id=operation_id, facility_id=facility_id,
        supply_ids=supply_ids, business_date=business_date, epoch=epoch['epoch'],
        source_digest=digest(sources), source_active_version=active['version_id'], effects=effects,
        total_quantity=sum(-l['quantity_delta'] for e in effects for l in e['lines']))
    require(len(canonical(plan).encode()) <= 2*1024**2, 'plan_size_budget')
    return {**plan, 'plan_digest':digest(plan)}


def verify(conn, plan, *, check_publication=True):
    receipt = conn.execute(f'SELECT * FROM {RECEIPTS} WHERE operation_id=?', (plan['operation_id'],)).fetchone()
    require(receipt is not None and receipt['plan_digest'] == plan['plan_digest'], 'receipt_conflict')
    result = json.loads(receipt['result_json'])
    for table, saved in result['immutable_rows'].items():
        for value in saved:
            where = 'operation_id=?' + (' AND line_no=?' if 'line_no' in value else '')
            args = [value['operation_id']] + ([value['line_no']] if 'line_no' in value else [])
            require(rows(conn, table, where, args) == [value], 'durable_operation_changed')
    for index, effect in enumerate(plan['effects']):
        op = operation_id(plan, index)
        aggregate = next((r for r in result['immutable_rows'][AGGREGATE] if r['operation_id'] == op), None)
        require(aggregate and aggregate['source_key'] == effect['source_key']
                and aggregate['source_object_id'] == effect['supply_id']
                and aggregate['business_effective_date'] == plan['business_date'], 'source_receipt_binding')
        native = [r for r in result['immutable_rows'][LINES_TABLE] if r['operation_id'] == op]
        require(len(native) == len(effect['lines']) and all(
            r['nm_id'] == line['nm_id'] and r['quantity_delta'] == line['quantity_delta']
            and r['capital_delta_rub'] == line['capital_delta_rub']
            for r,line in zip(native,effect['lines'])), 'money_receipt_binding')
        reservation = [r for r in result['immutable_rows'][RESERVATION_LINES] if r['operation_id'] == op+':fulfill']
        require({r['nm_id']:-r['quantity_delta'] for r in reservation} == {int(k):v for k,v in effect['reservation'].items()}, 'reservation_receipt_binding')
    if check_publication:
        for table, saved in result['publication_rows'].items():
            require(rows(conn, table, 'version_id=?', (result['published_version_id'],)) == saved, 'publication_receipt_changed')
    return {**result, 'idempotent':True}


def operation_id(plan, index):
    return 'fbo_current:' + hashlib.sha256((plan['plan_digest']+':'+str(index)).encode()).hexdigest()[:40]


def apply_owned(conn, runtime, *, plan, occurred_at, actor, fault=None):
    from packages.application.current_fbo_supply_close_apply import require_core_admission
    require_core_admission(conn, runtime, plan)
    require(conn.in_transaction and conn.row_factory is sqlite3.Row, 'owned_transaction_required')
    require(business_date_from_timestamp(occurred_at) == plan['business_date'], 'publication_day_changed')
    require(isinstance(actor, str) and 0 < len(actor.strip()) <= 160, 'actor_required')
    require(plan['plan_digest'] == digest({k:v for k,v in plan.items() if k != 'plan_digest'}), 'plan_changed')
    ensure_receipts(conn)
    if conn.execute(f'SELECT 1 FROM {RECEIPTS} WHERE operation_id=?', (plan['operation_id'],)).fetchone():
        return verify(conn, plan)
    require(prepare(conn, operation_id=plan['operation_id'], facility_id=plan['facility_id'],
        supply_ids=plan['supply_ids'], business_date=plan['business_date']) == plan, 'source_compare_and_swap')
    ids, reservation_ids = [], []
    for index, effect in enumerate(plan['effects']):
        op = operation_id(plan, index)
        ids.append(op)
        metadata = dict(contract=CONTRACT, plan_digest=plan['plan_digest'], effect=effect,
                        valuation='approved_current_cost_recovery', historical_repricing=False)
        transition = None
        if effect['reservation']:
            rid = op + ':fulfill'; reservation_ids.append(rid)
            expected = {int(k):v for k,v in effect['reservation'].items()}
            transition = dict(operation_id=rid, source_key=rid, supply_id=effect['supply_id'],
                supply_revision=effect['revision'], operation_type='fulfill', created_at=occurred_at,
                diagnostics=dict(contract=CONTRACT, physical_source_key=effect['source_key']), expected_current=expected,
                lines=[dict(nm_id=nm, quantity_delta=-q, raw=dict(reason=CONTRACT)) for nm,q in sorted(expected.items())])
        lines = [{**line, 'raw':dict(cost_snapshot=dict(unit_cost_rub=line['wac_rub'],
            capital_delta_rub=line['capital_delta_rub'], quality='approved_current_fbo_dispatch_estimate',
            provenance=dict(contract=CONTRACT, source_revision=effect['revision'], allocation=line)))} for line in effect['lines']]
        expected = {l['nm_id']:conn.execute(f'SELECT COALESCE(SUM(quantity_delta),0) FROM {AGGREGATE_LINES} WHERE nm_id=?', (l['nm_id'],)).fetchone()[0] for l in lines}
        runtime.create_ff_stock_operation_guarded(connection=conn, operation_id=op, operation_type='writeoff',
            source_type='wb_supply', source_key=effect['source_key'], source_object_id=effect['supply_id'],
            source_object_label=effect['supply_id'], created_at=occurred_at, created_by=actor,
            business_effective_date=plan['business_date'], warnings=[], diagnostics=metadata,
            lines=lines, expected_balances=expected, reservation_transition=transition)
        if fault: fault('aggregate', index)
    from packages.application.operator_warehouse_documents import assert_reservations
    assert_reservations(conn, plan={'documents':[{'movements':e['lines']} for e in plan['effects']]}, epoch=plan['epoch'])
    for index, (effect, op) in enumerate(zip(plan['effects'], ids)):
        conn.execute(f'INSERT INTO {OPERATIONS_TABLE} VALUES(?,?,?,?,?,?,?,?,?,?)',
            (op, 'wb_supply_dispatch', CONTRACT, 'wb_supply', effect['source_key'], plan['plan_digest'],
             plan['epoch'], plan['business_date'], occurred_at, canonical(effect)))
        for number, line in enumerate(effect['lines'], 1):
            conn.execute(f'INSERT INTO {LINES_TABLE} VALUES(?,?,?,?,?,?,?,?,?)',
                (op, number, plan['facility_id'], 'FBO', line['nm_id'], line['quantity_delta'],
                 line['capital_delta_rub'], line['wac_rub'], canonical(line)))
            # Stored pool WAC follows the existing canonical precision-38
            # ratio policy; authoritative capital itself remains exact.
            wac = canonical_decimal_ratio_text(line['after_capital_rub'],line['after_quantity']) if line['after_quantity'] else None
            changed = conn.execute(f'UPDATE {BALANCES_TABLE} SET quantity=?,capital_rub=?,wac_rub=?,source_watermark=?,updated_at=? WHERE facility_id=? AND pool=\'FBO\' AND nm_id=? AND projection_epoch=? AND quantity=? AND capital_rub=?',
                (line['after_quantity'], line['after_capital_rub'], wac, op, occurred_at, plan['facility_id'],
                 line['nm_id'], plan['epoch'], line['before_quantity'], line['before_capital_rub']))
            require(changed.rowcount == 1, 'balance_compare_and_swap')
        if fault: fault('pool', index)
    before_wb = [{k:v for k,v in r.items() if k != 'version_id'} for r in rows(conn, ACTIVE_BALANCES,
        "version_id=? AND warehouse_key='wb'", (plan['source_active_version'],))]
    snapshot_table = 'sheet_vitrina_v1_warehouse_wb_snapshots'
    snapshot_data = lambda version:[{k:v for k,v in r.items() if k not in ('version_id','snapshot_id','created_at')}
        for r in rows(conn,snapshot_table,'version_id=?',(version,))]
    before_snapshot = snapshot_data(plan['source_active_version'])
    publication = publish_fbs_pool_aggregate_revision(conn, affected_nm_ids=sorted({l['nm_id'] for e in plan['effects'] for l in e['lines']}),
        source_kind=CONTRACT, source_id=plan['operation_id'], business_date=plan['business_date'], published_at=occurred_at)
    _parity(conn)
    active = conn.execute(f'SELECT version_id FROM {ACTIVE} WHERE slot=1').fetchone()[0]
    require([{k:v for k,v in r.items() if k != 'version_id'} for r in rows(conn, ACTIVE_BALANCES,
        "version_id=? AND warehouse_key='wb'", (active,))] == before_wb, 'official_wb_balance_changed')
    require(before_snapshot and snapshot_data(active) == before_snapshot, 'official_wb_snapshot_changed')
    publication_rows = {table:rows(conn,table,'version_id=?',(active,)) for table in
        ('sheet_vitrina_v1_warehouse_functional_versions','sheet_vitrina_v1_warehouse_wb_snapshots',ACTIVE_BALANCES)}
    require(len(publication_rows['sheet_vitrina_v1_warehouse_functional_versions']) == 1
        and publication_rows['sheet_vitrina_v1_warehouse_functional_versions'][0]['status'] == 'good', 'publication_missing')
    immutable = {}
    for table, keys in ((OPERATIONS_TABLE, ids), (LINES_TABLE, ids), (AGGREGATE, ids), (AGGREGATE_LINES, ids),
                         (RESERVATIONS, reservation_ids), (RESERVATION_LINES, reservation_ids)):
        immutable[table] = [r for key in keys for r in rows(conn, table, 'operation_id=?', (key,))]
    result = dict(contract=CONTRACT, operation_id=plan['operation_id'], plan_digest=plan['plan_digest'],
        plan=plan,
        operation_ids=ids, published_version_id=active, publication=publication, immutable_rows=immutable,
        publication_rows=publication_rows,
        total_quantity=plan['total_quantity'], supply_ids=plan['supply_ids'], idempotent=False,
        official_wb_stock_changed=False, historical_book_changed=False)
    conn.execute(f'INSERT INTO {RECEIPTS} VALUES(?,?,?)', (plan['operation_id'], plan['plan_digest'], canonical(result)))
    # Additive evidence lives outside the immutable plan/receipt and money rows.
    # The same transaction pins the complete source before any later WB refresh.
    from packages.application.current_fbo_closure_evidence import capture_native
    capture_native(conn, plan)
    if fault: fault('publication', 0)
    return result
