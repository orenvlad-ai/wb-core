"""Explicitly activated, append-only FBO money admitted after WB acceptance.

The ordinary supply layer is frozen before redirecting a document.  A late
invoice allocates its whole value across its ORIGINAL packed SKUs; the resulting
SKU amount is then added to that SKU's current WB contour.  Zero stock is a
terminal unallocated expense, never a claim on a future receipt.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal, localcontext
from typing import Any

PREFIX = 'sheet_vitrina_v1_fbo_forward_cost_'
CONFIG = PREFIX + 'config'
BASELINES = PREFIX + 'baselines'
EFFECTS = PREFIX + 'effects'
POSTINGS = EFFECTS
ZERO = Decimal(0)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def number(value):
    result = Decimal(str(value if value is not None else 0))
    if not result.is_finite():
        raise ValueError('nonfinite_fbo_cost')
    return result


def exists(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def ensure_schema(conn):
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {CONFIG}(slot INTEGER PRIMARY KEY CHECK(slot=1), activated_at TEXT NOT NULL)''')
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {BASELINES}(kind TEXT NOT NULL, identity TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY(kind,identity))''')
    conn.execute(f'''CREATE TABLE IF NOT EXISTS {EFFECTS}(
        effect_id TEXT PRIMARY KEY,upload_id TEXT NOT NULL,source_revision TEXT NOT NULL,
        supply_id TEXT NOT NULL,nm_id INTEGER NOT NULL,amount_rub TEXT NOT NULL,
        status TEXT NOT NULL,reason TEXT NOT NULL,requested_at TEXT NOT NULL,
        posted_at TEXT,snapshot_json TEXT NOT NULL,
        UNIQUE(upload_id,source_revision,supply_id,nm_id))''')

    for action in ('UPDATE','DELETE'):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS fbo_forward_cost_terminal_{action.lower()}
            BEFORE {action} ON {EFFECTS} WHEN OLD.status IN ('posted','unallocated')
            BEGIN SELECT RAISE(ABORT,'immutable_fbo_forward_cost_effect'); END""")
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS fbo_forward_cost_config_{action.lower()}
            BEFORE {action} ON {CONFIG}
            BEGIN SELECT RAISE(ABORT,'immutable_fbo_forward_cost_policy'); END""")


def active(conn):
    # Read a stored column: a rowid-only probe emits a schema-less SQLite READ.
    return exists(conn, CONFIG) and conn.execute(f'SELECT activated_at FROM {CONFIG} WHERE slot=1').fetchone() is not None


def _lines(conn, upload_id):
    return [dict(r) for r in conn.execute('''SELECT line.*,upload.payment_validation_id,upload.uploaded_at
        FROM sheet_vitrina_v1_fulfillment_service_lines line
        JOIN sheet_vitrina_v1_fulfillment_service_uploads upload USING(upload_id)
        WHERE line.upload_id=? AND line.match_status='ok' AND line.is_storage_line=0
          AND upload.validation_status='ok' AND upload.deleted_at IS NULL ORDER BY line.row_index''', (upload_id,))]


def _economic_identity(rows):
    economic=[]
    for line in rows:
        economic.append({
            'supply':str(line.get('matched_wb_supply_id') or line.get('matched_wb_cache_key') or line.get('supply_id_input') or ''),
            'service_name':str(line.get('service_name') or '').strip(),
            **{key: str(number(line.get(key)).normalize()) if line.get(key) is not None else None for key in (
                'amount_without_vat','vat_amount','amount_with_vat',
                'service_amount_with_vat_without_storage','storage_allocated_amount_with_vat','service_amount_with_storage')}})
    return digest(sorted(economic,key=dumps)) if economic else ''


def _remember_economic_identity(conn, upload_id, rows):
    identity=_economic_identity(rows)
    if identity:
        conn.execute(f"INSERT INTO {BASELINES} VALUES('invoice_semantic',?,?) ON CONFLICT(kind,identity) DO UPDATE SET payload_json=excluded.payload_json",
            (upload_id+':'+identity,dumps({'upload_id':upload_id,'economic_digest':identity})))
    return identity


def _identity(line):
    return str(line.get('matched_wb_cache_key') or line.get('matched_wb_supply_id') or line.get('supply_id_input') or '')


def _packed_basis(conn, record):
    """Original dispatched composition, independently of mutable WB acceptance."""
    supply_id=str(record['supply_id'])
    lifecycle='sheet_vitrina_v1_fbo_supply_lifecycle_current'
    if exists(conn,lifecycle):
        saved=conn.execute(f'SELECT state_json FROM {lifecycle} WHERE supply_id=? OR cache_key=? ORDER BY cache_key LIMIT 1',
            (supply_id,str(record.get('cache_key') or 'supply:'+supply_id))).fetchone()
        if saved:
            state=json.loads(saved[0])
            packed={}
            for line in state.get('frozen_costs',[]):
                delta=number(line.get('quantity_delta'))
                if delta < ZERO:
                    nm=str(line['nm_id']);packed[nm]=str(number(packed.get(nm))-delta)
            if packed:
                return packed
            if state.get('operation_id') and state.get('composition'):
                return {str(nm):str(qty) for nm,qty in state['composition'].items() if number(qty)>ZERO}
    correction='sheet_vitrina_v1_wb_supply_box_corrections'
    if exists(conn,correction):
        saved=conn.execute(f"SELECT corrected_composition_json FROM {correction} WHERE supply_id=? AND status='applied' ORDER BY applied_at DESC,correction_id DESC LIMIT 1",(supply_id,)).fetchone()
        if saved:
            return {str(nm):str(qty) for nm,qty in json.loads(saved[0]).items() if number(qty)>ZERO}
    operations='sheet_vitrina_v1_ff_stock_operations'
    ledger='sheet_vitrina_v1_ff_stock_operation_lines'
    if exists(conn,operations) and exists(conn,ledger):
        packed={}
        for row in conn.execute(f"""SELECT line.nm_id,SUM(line.quantity_delta) quantity
            FROM {operations} operation JOIN {ledger} line USING(operation_id)
            WHERE operation.source_type IN ('wb_supply','wb_supply_targeted_reconciliation')
              AND operation.source_object_id IN (?,?,?) GROUP BY line.nm_id""",
                (supply_id,str(record.get('wb_supply_id') or ''),str(record.get('cache_key') or ''))):
            if number(row[1])<ZERO:
                packed[str(row[0])]=str(-number(row[1]))
        if packed:
            return packed
    # A saved preacceptance composition can establish the original packing.
    if int(record.get('status_id') or 0) != 5:
        from packages.application.warehouse_stocks import _validated_wb_goods
        packed={}
        for good in _validated_wb_goods(record):
            if number(good.get('quantity'))>ZERO:
                nm=str(good['nm_id']);packed[nm]=str(number(packed.get(nm))+number(good['quantity']))
        return packed
    return {}


def _sum_baseline_quantities(quantities):
    """Sum finite Decimal source quantities without ambient-context rounding."""
    if not quantities:
        return ZERO
    minimum_exponent = min(value.as_tuple().exponent for value in quantities)
    maximum_adjusted = max(value.adjusted() for value in quantities)
    with localcontext() as context:
        context.prec = max(28, maximum_adjusted - minimum_exponent + len(str(len(quantities))) + 2)
        return sum(quantities, ZERO)


def baseline_preview(conn: sqlite3.Connection) -> dict[str, Any]:
    """Plan exactly the activation baseline using SELECTs only, with no writes.

    Call inside the caller's coherent source-read transaction.  Both preview
    admission and the owned activation transaction use these same rows, so
    malformed historical inputs are rejected before an operation is claimed.
    """
    from packages.application.warehouse_stocks import _normalized_wb_record, _validated_wb_goods, _is_doprinato
    rows = []
    def append(kind, identity, payload):
        rows.append({'kind':kind, 'identity':identity, 'payload_json':dumps(payload)})
    uploads = conn.execute('SELECT upload_id FROM sheet_vitrina_v1_fulfillment_service_uploads ORDER BY upload_id').fetchall() if exists(conn,'sheet_vitrina_v1_fulfillment_service_uploads') else []
    for row in uploads:
        upload_id = str(row[0])
        saved_lines = _lines(conn, upload_id)
        append('upload', upload_id, saved_lines)
        economic_identity = _economic_identity(saved_lines)
        if economic_identity:
            append('invoice_semantic', upload_id+':'+economic_identity,
                {'upload_id':upload_id, 'economic_digest':economic_identity})
    for raw in conn.execute('SELECT * FROM sheet_vitrina_v1_wb_supplies ORDER BY supply_id'):
        record = _normalized_wb_record(dict(raw))
        supply_id = str(record['supply_id'])
        composition = _packed_basis(conn, record)
        if composition:
            append('supply_packed', supply_id, composition)
        if _is_doprinato(record):
            quantities_by_nm = {}
            for good in _validated_wb_goods(record):
                # Preserve the original per-barcode choice, then aggregate the
                # chosen quantities at the actual baseline key (supply, SKU).
                quantity = number(good.get('accepted_quantity')) or number(good.get('quantity'))
                if quantity < ZERO:
                    raise ValueError('negative_fbo_doprinato_baseline_quantity:'+supply_id+':'+str(good['nm_id']))
                quantities_by_nm.setdefault(int(good['nm_id']), []).append(quantity)
            for nm_id, quantities in sorted(quantities_by_nm.items()):
                append('doprinato', f'{supply_id}:{nm_id}', {'quantity':str(_sum_baseline_quantities(quantities))})
    rows.sort(key=lambda row:(row['kind'], row['identity']))
    keys = [(row['kind'],row['identity']) for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError('duplicate_fbo_forward_cost_baseline_identity')
    counts = {kind:sum(row['kind']==kind for row in rows)
              for kind in ('upload','invoice_semantic','supply_packed','doprinato')}
    plan = {'contract':'fbo_forward_cost_baseline_v1', 'rows':rows, 'counts':counts}
    return {**plan, 'plan_digest':digest(plan)}


def activate(conn: sqlite3.Connection, activated_at: str, *, expected_plan: dict[str, Any] | None = None):
    """Apply a validated baseline in the caller's activation transaction; no commit."""
    if active(conn):
        return {'status': 'already_active', 'activated_at': conn.execute(f'SELECT activated_at FROM {CONFIG}').fetchone()[0]}
    plan = baseline_preview(conn)
    if expected_plan is not None:
        if (not isinstance(expected_plan, dict)
                or expected_plan.get('contract') != plan['contract']
                or expected_plan.get('plan_digest') != digest({key:value for key,value in expected_plan.items() if key!='plan_digest'})):
            raise ValueError('invalid_fbo_forward_cost_baseline_plan')
        if expected_plan['plan_digest'] != plan['plan_digest']:
            raise ValueError('fbo_forward_cost_baseline_source_changed')
    # Even schema creation follows validation and source-drift rejection.
    if exists(conn, BASELINES) and conn.execute(f'SELECT 1 FROM {BASELINES} LIMIT 1').fetchone():
        raise ValueError('fbo_forward_cost_inactive_baseline_not_empty')
    ensure_schema(conn)
    conn.executemany(f'INSERT INTO {BASELINES}(kind,identity,payload_json) VALUES(?,?,?)',
        [(row['kind'],row['identity'],row['payload_json']) for row in plan['rows']])
    conn.execute(f'INSERT INTO {CONFIG} VALUES(1,?)', (activated_at,))
    return {'status': 'active', 'activated_at': activated_at,
            'baseline_plan_digest':plan['plan_digest'], 'baseline_counts':plan['counts']}


def ordinary_overlay_lines(conn, rows):
    """Replace redirected portions by immutable pre-late source lines."""
    if not active(conn):
        return rows
    frozen = {r[0]: json.loads(r[1]) for r in conn.execute(f"SELECT identity,payload_json FROM {BASELINES} WHERE kind='frozen_upload'")}
    result = [dict(r) for r in rows if str(r['upload_id']) not in frozen]
    for lines in frozen.values():
        result.extend(lines)
    return sorted(result, key=lambda r: (str(r.get('uploaded_at') or ''), int(r.get('row_index') or 0)))


def route_request(conn, upload_id: str, revision: str, requested_at: str):
    """Atomically redirect an accepted-supply document instead of replaying it.

    Each supply is routed independently: already accepted portions freeze their
    ordinary source; preacceptance portions continue through the native overlay.
    """
    from packages.application.fulfillment_recalc_intents import load_supply_in_connection
    from packages.application.warehouse_stocks import _normalized_wb_record, _validated_wb_goods, WB_FINAL_ACCEPTED_STATUS_ID
    if not active(conn):
        return None
    prior = conn.execute(f"SELECT payload_json FROM {BASELINES} WHERE kind='upload' AND identity=?", (upload_id,)).fetchone()
    old = json.loads(prior[0]) if prior else []
    current = _lines(conn, upload_id)
    economic_identity=_remember_economic_identity(conn,upload_id,current)
    duplicate_invoice=bool(economic_identity and any(json.loads(r[0]).get('economic_digest')==economic_identity and json.loads(r[0]).get('upload_id')!=upload_id for r in conn.execute(f"SELECT payload_json FROM {BASELINES} WHERE kind='invoice_semantic'")))
    grouped, old_grouped = {}, {}
    for rows, target in ((current, grouped), (old, old_grouped)):
        for line in rows:
            target.setdefault(_identity(line), []).append(line)
    latest = conn.execute(f"SELECT payload_json FROM {BASELINES} WHERE kind='latest_upload' AND identity=?", (upload_id,)).fetchone()
    previous_lines = json.loads(latest[0]) if latest else []
    identities = sorted(set(grouped) | set(old_grouped) | {_identity(line) for line in previous_lines})
    supplies = {identity: load_supply_in_connection(conn, identity) for identity in identities}
    late = any(r is not None and int(r['status_id'] or 0) == WB_FINAL_ACCEPTED_STATUS_ID for r in supplies.values())
    frozen = conn.execute(f"SELECT 1 FROM {BASELINES} WHERE kind='frozen_upload' AND identity=?", (upload_id,)).fetchone()
    if not late and not frozen and not duplicate_invoice:
        for supply in supplies.values():
            if supply is not None:
                packed = {}
                for good in _validated_wb_goods(_normalized_wb_record(dict(supply))):
                    key = str(good['nm_id'])
                    packed[key] = str(number(packed.get(key)) + number(good.get('quantity')))
                if packed:
                    conn.execute(f"INSERT OR IGNORE INTO {BASELINES} VALUES('supply_packed',?,?)",(str(supply['supply_id']),dumps(packed)))
        conn.execute(f"INSERT INTO {BASELINES} VALUES('upload',?,?) ON CONFLICT(kind,identity) DO UPDATE SET payload_json=excluded.payload_json", (upload_id,dumps(current)))
        return None
    ordinary_ids = {identity for identity,supply in supplies.items()
                    if supply is not None and int(supply['status_id'] or 0) != WB_FINAL_ACCEPTED_STATUS_ID and not duplicate_invoice}
    ordinary = [line for line in old if _identity(line) not in ordinary_ids] + [line for line in current if _identity(line) in ordinary_ids]
    conn.execute(f"INSERT INTO {BASELINES} VALUES('frozen_upload',?,?) ON CONFLICT(kind,identity) DO UPDATE SET payload_json=excluded.payload_json", (upload_id,dumps(ordinary)))
    conn.execute(f"INSERT INTO {BASELINES} VALUES('upload',?,?) ON CONFLICT(kind,identity) DO UPDATE SET payload_json=excluded.payload_json", (upload_id,dumps(ordinary)))
    conn.execute(f"UPDATE {EFFECTS} SET status='superseded',reason='source_revision_superseded' WHERE upload_id=? AND source_revision<>? AND status='pending'",(upload_id,revision))
    # Full invoice amount includes allocated storage; never multiply by accepted/packed.
    def amount(rows):
        total = ZERO
        for line in rows:
            value = line.get('service_amount_with_storage')
            if value is None:
                service = line.get('service_amount_with_vat_without_storage')
                value = number(service if service is not None else line.get('amount_with_vat')) + number(line.get('storage_allocated_amount_with_vat'))
            total += number(value)
        return total
    for identity in identities:
        if identity in ordinary_ids:
            continue
        supply = supplies[identity]
        supply_id = str(supply['supply_id']) if supply is not None else identity
        reason = ''
        if supply is None:
            goods, reason = [], 'matched_supply_missing'
        else:
            try:
                goods = _validated_wb_goods(_normalized_wb_record(dict(supply)))
            except ValueError:
                goods = []
            if int(supply['status_id'] or 0) != WB_FINAL_ACCEPTED_STATUS_ID:
                reason = 'mixed_invoice_acceptance_pending'
        original_key = upload_id + ':' + supply_id
        original = conn.execute(f"SELECT payload_json FROM {BASELINES} WHERE kind='packed' AND identity=?", (original_key,)).fetchone()
        if original:
            packed = json.loads(original[0])
        else:
            packed = _packed_basis(conn,_normalized_wb_record(dict(supply))) if supply is not None else {}
            if not packed:
                saved = conn.execute(f"SELECT payload_json FROM {BASELINES} WHERE kind='supply_packed' AND identity=?", (supply_id,)).fetchone()
                packed = json.loads(saved[0]) if saved else {}
            if packed:
                conn.execute(f"INSERT INTO {BASELINES} VALUES('packed',?,?)", (original_key,dumps(packed)))
        denominator = sum((number(q) for q in packed.values()), ZERO)
        if denominator <= ZERO:
            reason, packed, denominator = 'original_packed_composition_missing', {'0':'1'}, Decimal(1)
        delta = amount(grouped.get(identity, [])) - amount(old_grouped.get(identity, []))
        allocated = ZERO
        ordered = sorted(packed, key=int)
        for index,nm in enumerate(ordered):
            total = delta - allocated if index == len(ordered)-1 else delta * number(packed[nm]) / denominator
            allocated += total
            previous = sum((number(r[0]) for r in conn.execute(f'''SELECT amount_rub FROM {EFFECTS} WHERE upload_id=? AND supply_id=? AND nm_id=? AND source_revision<>? AND status IN ('posted','unallocated')''', (upload_id,supply_id,int(nm),revision))),ZERO)
            change = total - previous
            if change == ZERO:
                continue
            effect_id = 'fbofee_' + digest([upload_id,revision,supply_id,nm])[:24]
            blocker = 'semantic_invoice_duplicate_requires_review' if duplicate_invoice else reason or ('negative_revision_requires_review' if change < ZERO else '')
            conn.execute(f'''INSERT OR IGNORE INTO {EFFECTS} VALUES(?,?,?,?,?,?,'pending',?,?,NULL,?)''',
                (effect_id,upload_id,revision,supply_id,int(nm),str(change),blocker,requested_at,dumps({'original_packed_quantity':packed[nm],'original_total_packed_quantity':str(denominator),'allocation_total_rub':str(delta)})))
    conn.execute(f"INSERT INTO {BASELINES} VALUES('latest_upload',?,?) ON CONFLICT(kind,identity) DO UPDATE SET payload_json=excluded.payload_json",(upload_id,dumps(current)))
    return {**read_request(conn,upload_id,revision), 'ordinary_scope':sorted(ordinary_ids)}


def read_request(conn,upload_id,revision):
    if not active(conn):
        return None
    rows = [dict(r) for r in conn.execute(f'SELECT * FROM {EFFECTS} WHERE upload_id=? AND source_revision=? ORDER BY effect_id',(upload_id,revision))]
    if not rows and not conn.execute(f"SELECT 1 FROM {BASELINES} WHERE kind='frozen_upload' AND identity=?",(upload_id,)).fetchone():
        return None
    return {'stable_source_id':'fulfillment_upload:'+upload_id,'source_revision':revision,'status':'pending' if any(r['status']=='pending' for r in rows) else 'applied', 'durable_saved':True,
            'policy':'fbo_forward_cost_v1','reason':next((r['reason'] for r in rows if r['reason']), 'current_wb_posting_pending' if any(r['status']=='pending' for r in rows) else ''), 'effects':rows,
            'unallocated_amount_rub':str(sum((number(r['amount_rub']) for r in rows if r['status']=='unallocated'),ZERO))}


def capture(conn):
    if not active(conn):
        return {}
    config = dict(conn.execute(f'SELECT * FROM {CONFIG} WHERE slot=1').fetchone())
    from packages.application.fbo_forward_cost_consumers import capture_order_boundary
    boundaries = {str(r[0]):capture_order_boundary(conn,int(r[0])) for r in conn.execute(f"SELECT DISTINCT nm_id FROM {EFFECTS} WHERE status='pending' AND nm_id>0")}
    return {'config':config, 'order_boundaries':boundaries,
            'baselines':[dict(r) for r in conn.execute(f'SELECT * FROM {BASELINES} ORDER BY kind,identity')],
            'effects':[dict(r) for r in conn.execute(f'SELECT * FROM {EFFECTS} ORDER BY requested_at,effect_id')],
            'doprinato_events':[dict(r) for r in conn.execute("SELECT * FROM sheet_vitrina_v1_warehouse_functional_events WHERE event_type='wb_doprinato_forward' ORDER BY event_id")] if exists(conn,'sheet_vitrina_v1_warehouse_functional_events') else [],
            'lifecycle':[dict(r) for r in conn.execute('SELECT * FROM sheet_vitrina_v1_fbo_supply_lifecycle_current ORDER BY cache_key')] if exists(conn,'sheet_vitrina_v1_fbo_supply_lifecycle_current') else []}


def apply_pending(lines, state, *, snapshot, posted_at, business_date=None):
    """Pure candidate construction. Updates are committed with warehouse CAS."""
    from dataclasses import replace
    result = list(lines)
    by_nm = {line.nm_id:index for index,line in enumerate(result) if line.warehouse_key=='wb'}
    quantities = {int(row['nm_id']):sum((number(row.get(k)) for k in ('quantity','in_way_to_client','in_way_from_client')),ZERO) for row in snapshot['items']}
    updates = []
    retryable = {'', 'forward_order_boundary_not_current', 'current_wb_cost_basis_missing', 'current_wb_quantity_missing'}
    def pending(effect, reason):
        if effect['reason'] != reason:
            updates.append({**effect,'reason':reason})
    posting_sequence = max((int(json.loads(effect['snapshot_json']).get('posting_sequence') or 0) for effect in state.get('effects',[]) if effect['status']=='posted'), default=0)
    for effect in state.get('effects',[]):
        if effect['status'] != 'pending' or effect['reason'] not in retryable:
            continue
        nm = int(effect['nm_id'])
        amount = number(effect['amount_rub'])
        if nm not in quantities:
            # Only complete requested SKU coverage proves zero, sparse omission doesn't.
            if nm not in {int(v) for v in snapshot.get('requested_nm_ids',[])}:
                pending(effect,'current_wb_quantity_missing')
                continue
            quantity = ZERO
        else:
            quantity = quantities[nm]
        saved = json.loads(effect['snapshot_json'])
        boundary = state.get('order_boundaries',{}).get(str(nm))
        if quantity > ZERO and (not boundary or boundary.get('business_date') != (business_date or posted_at[:10]) or boundary.get('status') != 'ready'):
            pending(effect,'forward_order_boundary_not_current')
            continue
        if boundary:
            saved['order_boundary'] = boundary
        saved.update(posted_at=posted_at,wb_snapshot_id=snapshot['snapshot_id'],wb_quantity=str(quantity))
        if quantity == ZERO:
            updates.append({**effect,'status':'unallocated','reason':'no_current_wb_stock','posted_at':posted_at,'snapshot_json':dumps(saved)})
            continue
        line = result[by_nm[nm]] if nm in by_nm else None
        if line is None or line.wac is None or line.quantity != quantity:
            pending(effect,'current_wb_cost_basis_missing')
            continue
        before = line.capital
        after = before + amount
        if after <= ZERO:
            continue
        posting_sequence += 1
        saved.update(posting_sequence=posting_sequence,before_capital_rub=str(before),after_capital_rub=str(after),before_wac_rub=str(line.wac),after_wac_rub=str(after/quantity))
        result[by_nm[nm]] = replace(line,capital=after,provenance={**line.provenance,'fbo_forward_cost_effect_ids':list(line.provenance.get('fbo_forward_cost_effect_ids',[]))+[effect['effect_id']]})
        updates.append({**effect,'status':'posted','reason':'','posted_at':posted_at,'snapshot_json':dumps(saved)})
    return result,updates


def commit_updates(conn,updates):
    for row in updates:
        changed = conn.execute(f'''UPDATE {EFFECTS} SET status=?,reason=?,posted_at=?,snapshot_json=? WHERE effect_id=? AND status='pending' ''',
            (row['status'],row['reason'],row['posted_at'],row['snapshot_json'],row['effect_id'])).rowcount
        if changed != 1:
            raise ValueError('fbo_forward_cost_effect_conflict')


def load_posted_effects(conn):
    if not active(conn):
        return []
    rows = [{**dict(r),'snapshot':json.loads(r['snapshot_json'])} for r in conn.execute(f"SELECT * FROM {EFFECTS} WHERE status='posted' ORDER BY posted_at,requested_at,effect_id")]
    return sorted(rows,key=lambda r:(r['posted_at'],int(r['snapshot'].get('posting_sequence') or 0),r['effect_id']))


def _aggregate_doprinato_audit(audit):
    """Match the SKU-level baseline key while retaining every barcode's proof."""
    grouped = {}
    for raw in audit:
        row = dict(raw)
        key = (str(row['source_id']), int(row['nm_id']))
        grouped.setdefault(key, []).append(row)
    result = []
    ordered = sorted(grouped.items(), key=lambda item:(
        min(str(row.get('business_date') or '') for row in item[1]), item[0]))
    for (source_id, nm_id), source_rows in ordered:
        source_rows.sort(key=dumps)
        quantities = [number(row['matched_quantity']) for row in source_rows]
        capitals = [number(row.get('matched_capital_rub')) for row in source_rows]
        unmatched = [number(row.get('unmatched_quantity')) for row in source_rows]
        if any(value < ZERO for value in quantities + capitals + unmatched):
            raise ValueError('negative_fbo_doprinato_match')
        matched_quantity = _sum_baseline_quantities(quantities)
        matched_capital = _sum_baseline_quantities(capitals)
        # Zero matched quantity has no cost operand. It must not suppress the
        # priced part of a partially matched supply, but ANY positive unknown
        # matched part prevents inventing a fully covered blended unit cost.
        cost_known = all(quantity == ZERO or (
            row.get('matched_wac_rub') is not None
            and row.get('matched_capital_rub') is not None
            and number(row['matched_wac_rub']) > ZERO
            and capital > ZERO)
            for row, quantity, capital in zip(source_rows, quantities, capitals))
        first_provenance = source_rows[0].get('provenance') or {}
        common_provenance = {key:value for key,value in first_provenance.items()
            if all((row.get('provenance') or {}).get(key)==value for row in source_rows)}
        provenance = dict(common_provenance)
        if len(source_rows) > 1:
            provenance['doprinato_barcode_matches'] = source_rows
        result.append({'source_id':source_id, 'nm_id':nm_id,
            'matched_quantity':str(matched_quantity),
            'unmatched_quantity':str(_sum_baseline_quantities(unmatched)),
            'matched_capital_rub':str(matched_capital),
            'matched_wac_rub':str(matched_capital / matched_quantity)
                if matched_quantity > ZERO and cost_known else None,
            'provenance':provenance})
    return result


def doprinato_events(audit, state, *, posted_at, business_date, accepted_events=()):
    """Forward-only matched capital; old full quantities are an immutable floor."""
    baseline = {row['identity']:number(json.loads(row['payload_json'])['quantity']) for row in state.get('baselines',[]) if row['kind']=='doprinato'}
    posted = {}
    posted_capital = {}
    ordinary_increase = {}
    for row in accepted_events:
        nm = int(row["nm_id"])
        ordinary_increase[nm] = ordinary_increase.get(nm,ZERO) + max(number(row["quantity"]),ZERO)
    for row in state.get('doprinato_events',[]):
        key = (row['source_id'],int(row['nm_id']))
        posted[key] = posted.get(key,ZERO) + number(row['quantity'])
        posted_capital[key] = posted_capital.get(key,ZERO) + number(row['capital_rub'])
    result = []
    if not state:
        return result
    for row in _aggregate_doprinato_audit(audit):
        source_id,nm = str(row['source_id']),int(row['nm_id'])
        matched = number(row['matched_quantity'])
        previously = posted.get((source_id,nm),ZERO)
        delta = max(matched - baseline.get(source_id,ZERO),ZERO) - previously
        if delta == ZERO:
            continue
        if delta < ZERO:
            # WB sometimes folds an independent additional acceptance into the
            # original accepted total later. Move its exact already posted
            # capital out of the supplemental category in the SAME new version.
            # An unexplained source deletion never authorizes a stock reversal.
            if ordinary_increase.get(nm,ZERO) < -delta or previously <= ZERO:
                continue
            ordinary_increase[nm] += delta
            capital = posted_capital[(source_id,nm)] * delta / previously
        else:
            if row.get('matched_wac_rub') is None:
                continue
            # Exact matched buffer basis, never the current WB average.
            capital = number(row['matched_capital_rub']) * delta / matched
        identity = [source_id,nm,str(matched),str(baseline.get(source_id,ZERO)),str(posted.get((source_id,nm),ZERO))]
        result.append({'event_id':'whfd_' + digest(identity)[:24], 'event_type':'wb_doprinato_forward',
            'source_id':source_id,'source_fingerprint':digest(identity),'business_date':business_date,
            'nm_id':nm,'quantity':str(delta),'capital_rub':str(capital),
            'provenance':{**row.get('provenance',{}),'posted_at':posted_at,'matched_buffer_wac_rub':str(capital/delta),
                'matched_quantity':str(matched),'current_pool_capital_delta_rub':str(capital),
                'forward_only':True}})
    return result


def notification_items(conn):
    """Read-only durable exceptions for the existing global notification bell."""
    if not active(conn):
        return []
    result=[]
    for raw in conn.execute(f"SELECT * FROM {EFFECTS} WHERE status IN ('pending','unallocated') ORDER BY requested_at,effect_id"):
        row=dict(raw)
        reason=row['reason'] or 'current_wb_posting_pending'
        labels={
            'no_current_wb_stock':'У этого артикула нет остатка в контуре WB. Сумма не распределена и не будет перенесена на будущие поступления.',
            'negative_revision_requires_review':'Уменьшение ранее учтённого расхода требует отдельной проверки; прежняя себестоимость сохранена.',
            'forward_order_boundary_not_current':'Ожидается подтверждённая граница уже учтённых заказов текущего дня. Прежняя прибыль сохранена.',
            'current_wb_cost_basis_missing':'Не подтверждена стоимость текущего остатка этого артикула на WB.',
            'current_wb_quantity_missing':'В текущем полном снимке не подтверждён остаток этого артикула на WB.',
            'current_wb_posting_pending':'Ожидается актуальный остаток WB с подтверждённой стоимостью и границей уже учтённых заказов за день.',
            'original_packed_composition_missing':'Не подтверждён исходный состав упакованной поставки.',
            'matched_supply_missing':'Связанная поставка пока не найдена.',
            'semantic_invoice_duplicate_requires_review':'Документ с теми же поставками, услугами и суммами уже сохранён. Возможный повтор требует проверки; повторный расход не начислен.'}
        result.append({'id':row['effect_id'],'reason_revision':digest([row['effect_id'],row['status'],reason]),
            'type':'fbo_cost_unallocated','supply_id':row['supply_id'],
            'upload_id':row['upload_id'],'nm_id':row['nm_id'],'amount_rub':row['amount_rub'],
            'status':row['status'],'reason':reason,
            'title':'Расход ФФ не распределён' if row['status']=='unallocated' else 'Расход ФФ ожидает учёта',
            'detail':f"Артикул {row['nm_id']}: {row['amount_rub']} ₽. " + labels.get(reason,reason)})
    return result
