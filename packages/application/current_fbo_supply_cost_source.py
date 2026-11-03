"""Verified current dispatch receipts as explicit independent FBO cost sources."""
import json
from packages.application import current_fbo_supply_close as owner

CONTRACT = 'current_fbo_supply_cost_source_v1'


def book_bridge(book,plan):
    """Read-only admission against the latest saved independent FBO book."""
    from decimal import Decimal
    from packages.application.canonical_rub_money import compare_canonical_rub_money
    owner.require(book and book.get('active') is True and not book.get('publication_error'),'healthy_book_required')
    state=book['state'];periods=state['periods']
    selected=periods[max(periods)] if periods else state['baseline']
    basis=selected['document_cost_state']['fbo_rows']
    seen=set();proof=[]
    for effect in plan['effects']:
        for line in effect['lines']:
            key=f"{plan['facility_id']}:FBO:{line['nm_id']}"
            if key in seen:continue
            seen.add(key);row=basis.get(key)
            owner.require(row and Decimal(str(row['quantity']))==line['before_quantity'],'saved_book_native_quantity_bridge_mismatch')
            comparison=compare_canonical_rub_money(line['before_capital_rub'],row['capital_rub'],left_field='native FBO',right_field='saved book FBO')
            owner.require(comparison.canonical_equal and comparison.residual_attributable,'saved_book_native_capital_bridge_mismatch')
            proof.append(dict(key=key,book_capital_rub=row['capital_rub'],native_capital_rub=line['before_capital_rub'],
                attributed_bridge_rub=format(comparison.raw_residual_rub,'f'),canonical_policy=comparison.policy))
    return dict(book_day=max(periods) if periods else state['baseline'].get('business_date'),bridges=proof)


def capture_documents(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(owner.RECEIPTS,)).fetchone():
        return []
    receipts=list(conn.execute(f'SELECT * FROM {owner.RECEIPTS} ORDER BY operation_id'))
    owner.require(len(receipts)<=100,'current_cost_source_budget')
    result=[]
    for receipt in receipts:
        saved=json.loads(receipt['result_json']);plan=saved['plan']
        owner.require(plan['plan_digest']==owner.digest({k:v for k,v in plan.items() if k!='plan_digest'}),'current_cost_source_plan_changed')
        # Native immutable money/identity survives ordinary snapshot retention;
        # source admission must not depend on old presentation versions forever.
        verified=owner.verify(conn,plan,check_publication=False)
        for index,effect in enumerate(plan['effects']):
            identity=owner.operation_id(plan,index)
            header=next(r for r in verified['immutable_rows'][owner.OPERATIONS_TABLE] if r['operation_id']==identity)
            lines=effect['lines']
            result.append(dict(document_id=identity,operation_id=identity,root_document_id=identity,
                kind='current_fbo_dispatch',document_kind='current_fbo_dispatch',business_date=plan['business_date'],
                posted_at=header['posted_at'],fingerprint=owner.digest(dict(header=header,effect=effect)),events=[],
                current_fbo_native=dict(contract=CONTRACT,batch_id=plan['operation_id'],plan_digest=plan['plan_digest'],effect_index=index),
                cost_document=dict(contract=CONTRACT,domain=dict(facility_id=plan['facility_id'],pool='FBO',
                    supply_id=effect['supply_id'],source_key=effect['source_key']),lines=lines,
                    expense_lines=[],movements=lines,relations=[])))
    return result


def capture_warehouse_closures(conn):
    """Verified current FF closeout is already covered by official WB stock.

    It cannot become a newly discovered historical WB acceptance/cost layer.
    Revisions of these exact sources require explicit review, not silent
    retrospective valuation of an approved current-cost estimate.
    """
    closures=[]
    for doc in capture_documents(conn):
        data=doc['cost_document'];domain=data['domain'];supply=domain['supply_id']
        candidates=list(conn.execute('SELECT * FROM sheet_vitrina_v1_wb_supplies WHERE supply_id=? OR wb_supply_id=? OR cache_key=?',
            (supply,supply,'supply:'+supply)))
        owner.require(len(candidates)==1,'current_warehouse_close_source_missing_or_ambiguous')
        record=owner._wb_supply_record_from_row(candidates[0]);normalized=record['normalized']
        _,source_id,source_key=owner.ledger._wb_supply_debit_identity(record=record,normalized=normalized)
        revision=owner.ledger._wb_supply_revision(record,normalized)
        expected={line['source_revision'] for line in data['lines']}
        owner.require(source_id==supply and source_key==domain['source_key'] and expected=={revision},
            'current_warehouse_close_source_revision_changed')
        from packages.application.wb_supply_box_correction import load_active_box_correction
        owner.require(load_active_box_correction(conn,supply) is None,'current_warehouse_close_box_correction_requires_review')
        closures.append(dict(supply_id=supply,source_revision=revision,source_key=source_key,
            business_date=doc['business_date'],operation_id=doc['operation_id'],
            receipt_fingerprint=doc['fingerprint'],plan_digest=doc['current_fbo_native']['plan_digest'],
            wb_policy='already_covered_by_official_stock_no_retrospective_acceptance'))
    owner.require(len({r['supply_id'] for r in closures})==len(closures),'current_warehouse_close_duplicate_supply')
    return closures


def apply_opening_dispatches(nodes, active, node, key):
    """Consume only this day's pinned current boundary before later documents.

    Earlier overlapping FBO documents need an ordinary book refresh first.
    Later legitimate receipts/transfers retain the normal document semantics.
    """
    from decimal import Decimal, localcontext
    from packages.application.canonical_rub_money import compare_canonical_rub_money
    typed=[d for d in active if d['kind']=='current_fbo_dispatch']
    bridges=[];boundaries={}
    if not typed:
        return bridges,boundaries
    for doc in typed:
        data=doc['cost_document'];proof=doc.get('current_fbo_native') or {}
        owner.require(data.get('contract')==CONTRACT and proof.get('contract')==CONTRACT,'typed_current_cost_source_required')
        facility=data['domain']['facility_id']
        previous=boundaries.get(facility)
        owner.require(previous is None or previous['batch_id']==proof['batch_id'],'multiple_current_boundaries_require_separate_period')
        boundaries[facility]=dict(batch_id=proof['batch_id'],posted_at=doc['posted_at'])
    for doc in active:
        if doc in typed:
            continue
        data=doc.get('cost_document') or {};domain=data.get('domain') or {}
        for facility,boundary in boundaries.items():
            if str(doc.get('posted_at') or '')>boundary['posted_at']:
                continue
            affected=any(r.get('facility_id')==facility and r.get('pool')=='FBO'
                for r in [*data.get('lines',[]),*data.get('movements',[])])
            affected=affected or (domain.get('facility_id')==facility and domain.get('scope') in ('FBO','both'))
            owner.require(not affected,'earlier_same_day_fbo_documents_require_book_refresh')
    for doc in sorted(typed,key=lambda d:(d['posted_at'],d['current_fbo_native']['effect_index'])):
        for line in doc['cost_document']['lines']:
            k=key(line['facility_id'],'FBO',int(line['nm_id']));n=node(k)
            owner.require(n['mass']==line['before_quantity'] and not n['outgoing'],'book_native_quantity_bridge_mismatch')
            comparison=compare_canonical_rub_money(line['before_capital_rub'],n['value'],left_field='native current FBO',right_field='book FBO')
            owner.require(comparison.canonical_equal and comparison.residual_attributable,'book_native_capital_bridge_mismatch')
            with localcontext() as ctx:
                ctx.prec=160
                before=Decimal(line['before_capital_rub']);delta=Decimal(line['capital_delta_rub'])
                after=before+delta
                owner.require(after==Decimal(line['after_capital_rub']) and line['before_quantity']+line['quantity_delta']==line['after_quantity'],'current_cost_conservation')
                bridges.append(dict(document_id=doc['document_id'],key=k,policy='explicit_current_native_capital_boundary_v1',
                    book_capital_rub=format(n['value'],'f'),native_capital_rub=format(before,'f'),
                    attributed_bridge_rub=format(comparison.raw_residual_rub,'f'),canonical_policy=comparison.policy,
                    quantity_delta=line['quantity_delta'],capital_delta_rub=line['capital_delta_rub'],after_capital_rub=format(after,'f')))
                n['mass']=line['after_quantity'];n['value']=after
                n['current_fbo_boundary']=dict(batch_id=doc['current_fbo_native']['batch_id'],posted_at=doc['posted_at'],
                    policy='explicit_current_native_capital_boundary_v1')
    return bridges,boundaries
