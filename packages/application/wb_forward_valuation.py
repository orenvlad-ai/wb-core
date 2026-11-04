"""Dated supplier-component estimates; never retrospective cost certification.

Candidate-only preparation API. Admission/publication uses the existing reviewed
functional plan and source guards; no HTTP endpoint or automatic activation.
"""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json

EVENT = 'wb_forward_valuation'
POLICY = 'wb_forward_supplier_component_v1'
QUALITY = 'wb_forward_supplier_component_estimate'


def digest(value):
    return 'sha256:' + hashlib.sha256(json.dumps(value, sort_keys=True,
        ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def number(value):
    n = Decimal(str(value))
    if not n.is_finite():
        raise ValueError('forward_valuation_nonfinite')
    return n


def make_event(*, request_id, day, nm_id, opening_quantity, opening_wac,
               old_supplier_unit, new_supplier_unit, source_evidence, anchor):
    from packages.application.wb_initial_fbs_cost import valid_anchor
    if not request_id or not source_evidence or not valid_anchor(anchor):
        raise ValueError('forward_valuation_evidence_missing')
    qty, wac = number(opening_quantity), number(opening_wac)
    old, new = number(old_supplier_unit), number(new_supplier_unit)
    if qty <= 0 or wac <= 0 or min(old, new) <= 0 or int(anchor['nm_id']) != nm_id:
        raise ValueError('forward_valuation_opening_uncovered')
    if date.fromisoformat(day) <= date.fromisoformat(anchor['business_date']):
        raise ValueError('forward_valuation_must_be_forward')
    capital = (qty * (new - old)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    if qty*wac + capital <= 0:
        raise ValueError('forward_valuation_nonpositive_capital')
    proof = dict(policy=POLICY, quality=QUALITY, request_id=request_id,
        business_date=day, nm_id=nm_id, opening_quantity=str(qty), opening_wac_rub=str(wac),
        old_supplier_unit_rub=str(old), new_supplier_unit_rub=str(new),
        capital_delta_rub=str(capital), source_evidence=source_evidence,
        initial_anchor_digest=anchor['proof_digest'], expenses_complete=False,
        proves_physical_movement=False, certified=False,
        assumption='Replace original supplier component on opening stock; preserve downstream costs; sold units excluded.')
    proof['proof_digest'] = digest(proof)
    return dict(event_id='wbfv_'+digest([request_id, nm_id])[7:31], event_type=EVENT,
        source_id=request_id, source_fingerprint=proof['proof_digest'], business_date=day,
        nm_id=nm_id, quantity='0', capital_rub=str(capital), provenance={'forward_valuation':proof})


def validate_event(event, *, day, nm_id, opening_quantity, opening_wac, anchor):
    proof = (event.get('provenance') or {}).get('forward_valuation') or {}
    expected = make_event(request_id=proof.get('request_id'), day=day, nm_id=nm_id,
        opening_quantity=opening_quantity, opening_wac=opening_wac,
        old_supplier_unit=proof.get('old_supplier_unit_rub'),
        new_supplier_unit=proof.get('new_supplier_unit_rub'),
        source_evidence=proof.get('source_evidence'), anchor=anchor or {})
    if any(event.get(k) != v for k, v in expected.items()):
        raise ValueError('forward_valuation_proof_or_opening_changed')
    return proof


def prepare_events(conn, *, request_id, day, shipment_id, receipt_id, daily_rows, anchors):
    """Use complete native supplier allocation and immutable original receipt."""
    from packages.application.operator_supplier_cost_proof import numerical_supplier_state
    state, allocation = numerical_supplier_state(conn, shipment_id)
    if not allocation or allocation['blockers'] or not allocation['financial_readiness']['ready']:
        raise ValueError('forward_valuation_supplier_not_ready')
    receipt = conn.execute('SELECT * FROM sheet_vitrina_v1_ff_pool_documents WHERE document_id=?', (receipt_id,)).fetchone()
    if not receipt or receipt['document_kind'] != 'china_acceptance' or receipt['source_id'] != shipment_id or receipt['document_role'] != 'root':
        raise ValueError('forward_valuation_original_receipt_missing')
    original = [dict(r) for r in conn.execute('SELECT * FROM sheet_vitrina_v1_ff_pool_document_lines WHERE document_id=? ORDER BY line_no', (receipt_id,))]
    totals = defaultdict(lambda:[Decimal(0),Decimal(0)])
    for row in original:
        totals[int(row['nm_id'])][0] += number(row['quantity'])
        totals[int(row['nm_id'])][1] += number(row['capital_rub'])
    prior = (date.fromisoformat(day)-timedelta(days=1)).isoformat()
    opening = {int(r['nm_id']):r for r in daily_rows if r['as_of_date']==prior}
    evidence = dict(shipment_id=shipment_id, receipt_id=receipt_id,
        supplier_state=state, receipt_digest=digest(dict(receipt)), receipt_lines_digest=digest(original))
    events = []
    for row in allocation['lines']:
        nm = int(row['nm_id'])
        if nm not in anchors:
            raise ValueError('forward_valuation_anchor_scope_incomplete')
        qty, capital = totals[nm]
        if qty != number(row['quantity']) or qty <= 0 or nm not in opening:
            raise ValueError('forward_valuation_original_scope_mismatch')
        events.append(make_event(request_id=request_id, day=day, nm_id=nm,
            opening_quantity=opening[nm]['quantity'], opening_wac=opening[nm]['wac_rub'],
            old_supplier_unit=capital/qty, new_supplier_unit=row['unit_cost_rub'],
            source_evidence=evidence, anchor=anchors[nm]))
    if len(events) != len(totals) or len(events)!=len(anchors):
        raise ValueError('forward_valuation_sku_scope_mismatch')
    return sorted(events, key=lambda e:e['nm_id'])


def check_identity(existing, candidate):
    if any(existing.get(k) != candidate.get(k) for k in (
        'event_id','event_type','source_id','source_fingerprint','business_date',
        'nm_id','quantity','capital_rub','provenance')):
        raise ValueError('forward_valuation_identity_conflict')


def protected_history_floor(conn, projection, new_events):
    """Protect only the admitted cohort; unrelated native corrections still run."""
    events = [dict(r) for r in conn.execute(
        'SELECT business_date,nm_id FROM sheet_vitrina_v1_warehouse_functional_events WHERE event_type=?', (EVENT,))]
    events += [e for e in new_events if e.get('event_type')==EVENT]
    floors = {}
    for event in events:
        nm = int(event['nm_id'])
        floors[nm] = min(floors.get(nm,event['business_date']),event['business_date'])
    rows = {(r['as_of_date'],int(r['nm_id'])):r for r in projection}
    for nm, floor in floors.items():
        stored = list(conn.execute('SELECT * FROM sheet_vitrina_v1_warehouse_wb_daily_cost WHERE nm_id=? AND as_of_date<?', (nm,floor)))
        if {r['as_of_date'] for r in stored} != {day for day, row_nm in rows if row_nm==nm and day<floor}:
            raise ValueError('forward_valuation_old_daily_keys_changed')
        for old in stored:
            new = rows.get((old['as_of_date'],nm))
            if not new or any(number(old[k])!=number(new[k]) for k in ('quantity','capital_rub','wac_rub')):
                raise ValueError('forward_valuation_old_daily_money_changed')
    return floors


def prepare_plan(block, *, request, wb_payload):
    """Explicit offline candidate; caller supplies the captured official snapshot.

    This is not an automatic source collector or a production operation endpoint.
    """
    from packages.application import warehouse_functional as wf
    plan = block._build_plan(kind='hourly_wb_sync', wb_payload=wb_payload)
    if request['day'] != plan['effective_date']:
        raise ValueError('forward_valuation_effective_date_mismatch')
    with wf._connect_readonly(block.runtime.db_path) as conn:
        conn.execute('BEGIN')
        active = conn.execute('SELECT business_effective_date FROM sheet_vitrina_v1_warehouse_functional_versions WHERE version_id=?', (plan['base_active_version_id'],)).fetchone()
        prior = (date.fromisoformat(request['day'])-timedelta(days=1)).isoformat()
        if not active or active[0] != prior:
            raise ValueError('forward_valuation_requires_next_day_boundary')
        anchors = {int(r['nm_id']):wf._initial_valuation_anchor(r['provenance']) for r in plan['lines']
            if r['warehouse_key']=='wb' and int(r['nm_id']) in set(request['nm_ids'])}
        events = prepare_events(conn, request_id=request['request_id'], day=request['day'],
            shipment_id=request['shipment_id'], receipt_id=request['receipt_id'],
            daily_rows=plan['historical_wb_cost_projection'], anchors=anchors)
    lines = [wf._line_from_payload(r) for r in plan['lines']]
    plan['new_events'] += events
    projection = block._build_post_cutover_daily_cost_projection(captured_at=plan['captured_at'],
        candidate_lines=lines, candidate_snapshot=plan['wb_snapshot'], new_events=plan['new_events'],
        opening_cost_map=[], cutover_mode=False, allow_partial=True)
    cutover_day = min(r['as_of_date'] for r in projection)
    plan['historical_wb_cost_projection'] = [r for r in plan['historical_wb_cost_projection'] if r['as_of_date']<cutover_day]+projection
    lines = wf._replace_current_wb_costs(lines, daily_projection=projection, current_date=plan['effective_date'])
    plan['lines'] = [wf._line_payload(r) for r in lines]
    plan['summaries'] = wf._summaries(lines)
    plan['wb_valuation'] = wf._wb_valuation_report(lines)
    plan['source_watermarks']['wb_valuation'] = plan['wb_valuation']
    plan['diff'] = wf._balance_diff(block._active_lines(), lines)
    plan['forward_valuation_request'] = dict(request)
    plan['calculation_digest'] = wf._calculation_digest(plan)
    plan.pop('plan_fingerprint')
    plan['plan_fingerprint'] = wf._fingerprint(plan)
    return plan


def revalidate_plan(conn, plan):
    from packages.application import warehouse_functional as wf
    events = [e for e in plan.get('new_events',[]) if e.get('event_type')==EVENT]
    request = plan.get('forward_valuation_request')
    if not events and not request:
        return
    if not request or request['day'] != plan['effective_date']:
        raise ValueError('forward_valuation_request_missing')
    prior = (date.fromisoformat(request['day'])-timedelta(days=1)).isoformat()
    active = conn.execute('SELECT v.version_id,v.business_effective_date FROM sheet_vitrina_v1_warehouse_functional_active a JOIN sheet_vitrina_v1_warehouse_functional_versions v ON v.version_id=a.version_id WHERE a.slot=1').fetchone()
    if not active or active['business_effective_date'] != prior or active['version_id'] != plan['base_active_version_id']:
        raise ValueError('forward_valuation_admission_boundary_changed')
    wanted = set(request['nm_ids'])
    anchors = {int(r['nm_id']):wf._initial_valuation_anchor(json.loads(r['provenance_json'])) for r in conn.execute(
        "SELECT nm_id,provenance_json FROM sheet_vitrina_v1_warehouse_functional_balances WHERE version_id=? AND warehouse_key='wb'", (active['version_id'],)) if int(r['nm_id']) in wanted}
    persisted_daily = [dict(r) for r in conn.execute('SELECT * FROM sheet_vitrina_v1_warehouse_wb_daily_cost WHERE as_of_date=?', (prior,))]
    expected = prepare_events(conn, request_id=request['request_id'], day=request['day'],
        shipment_id=request['shipment_id'], receipt_id=request['receipt_id'],
        daily_rows=persisted_daily, anchors=anchors)
    if events != expected:
        raise ValueError('forward_valuation_source_changed')
