"""Rebuild an unfinished FBS suffix from documentary allocations and handoffs.

The closed prefix is immutable. Every quantity comes from its true dated WB
source; historical initial WB cost is explicitly provisional and never records
a physical return. Publication belongs to the existing owned Ready contour.
"""
from copy import deepcopy
from datetime import date,datetime,timedelta
from decimal import Decimal,localcontext
from pathlib import Path
import hashlib
import json
import sqlite3

from packages.application.fbs_snapshot_cost import fingerprint,canonical,evaluate_candidate,close_candidate_period,candidate_period_view
from packages.application import fbs_accounting_historical_stages as stages
from packages.application.fbs_historical_fbo_stage_sources import _Reader,_snapshot_quantity_authority,_parsed_balances
from packages.application.fbs_accounting_historical_sources import archived_quantity_snapshot,original_source_queries
from packages.application.fbs_snapshot_cost_sources import capture_current
from packages.application.fbs_inventory_presentation import FbsInventorySnapshot
from packages.application.shared_sku_cost import build_shared_cost_day
from packages.application import ready_publication as ready
from packages.application import warehouse_functional as wf
from packages.application import wb_historical_fbs_cost as initial
from packages.application import ff_pool_fbs_historical_handoff as handoff

CONTRACT='fbs_temporary_allocation_repair_stages_v1'
CLOSED_PREFIX_CONTRACT='temporary_fbs_closed_receipts_20261026_v1'
P=stages.P


def require(value,reason):stages.require(value,'temporary_fbs_repair_'+reason)


def _days(first,last):
    first,last=date.fromisoformat(first),date.fromisoformat(last)
    require(0<=(last-first).days<=31,'date_scope_invalid')
    return [(first+timedelta(days=n)).isoformat() for n in range((last-first).days+1)]


def _saved(reader, day, *, version_id):
    version=reader.one(f'SELECT * FROM {P}functional_versions WHERE version_id=?',(version_id,))
    require(version['business_effective_date']==day,'pre_native_version_date_changed')
    snapshot=reader.one(f'SELECT * FROM {P}wb_snapshots WHERE version_id=?',(version_id,))
    _snapshot_quantity_authority(version,snapshot)
    balances=_parsed_balances(reader.rows(f'SELECT * FROM {P}functional_balances WHERE version_id=? ORDER BY warehouse_key,nm_id',(version_id,)))
    models=reader.rows(f'SELECT * FROM {P}functional_read_models WHERE version_id=? ORDER BY warehouse_key',(version_id,))
    require(len(models)==6 and {m['warehouse_key'] for m in models}==set(stages.STAGES.values()),'complete_six_stages_required')
    require(len({(r['warehouse_key'],r['nm_id']) for r in balances})==len(balances),'duplicate_stage_balance')
    for model in models:
        payload=json.loads(model['payload_json'])
        expected={int(r['nm_id']):(Decimal(r['quantity']),None if wf._public_line_payload(r)['capital_rub'] is None else Decimal(r['capital_rub'])) for r in balances if r['warehouse_key']==model['warehouse_key']}
        actual={int(r['nm_id']):(Decimal(r['quantity']),None if r['capital_rub'] is None else Decimal(r['capital_rub'])) for r in payload.get('balances',[])}
        require(payload.get('status')=='ready' and actual==expected and len(actual)==len(payload.get('balances',[])),'saved_stage_payload_changed')
        with localcontext() as context:
            # Match the native compact model's Decimal policy; individual
            # immutable money operands retain all their original digits.
            context.prec=28
            raw=[r for r in balances if r['warehouse_key']==model['warehouse_key']]
            values=[Decimal(r[k]) for r in raw for k in ('quantity','capital_rub')]
            require(all(v.is_finite() and v>=0 for v in values),'saved_stage_operand_invalid')
            totals=payload.get('warehouse',{})
            quantity=sum((Decimal(r['quantity']) for r in raw),Decimal(0))
            capital=sum((Decimal(r['capital_rub']) for r in raw),Decimal(0))
            complete=all(value[1] is not None for value in expected.values())
            require(totals.get('total_quantity') is not None and totals.get('known_capital_rub') is not None
                and Decimal(str(totals['total_quantity']))==quantity
                and Decimal(str(totals['known_capital_rub']))==capital
                and ((totals.get('total_capital_rub') is not None and Decimal(str(totals['total_capital_rub']))==capital)
                     if complete else totals.get('total_capital_rub') is None),
                'saved_stage_totals_changed')
    companions={}
    for table,order in [('functional_ff_reservations','supply_id,nm_id'),('supplier_cost_states','shipment_id'),('unmatched_doprinato','unmatched_id')]:
        companions[table]=reader.rows(f'SELECT * FROM {P}{table} WHERE version_id=? ORDER BY {order}',(version_id,))
    return dict(version=version,snapshot=snapshot,balances=balances,read_models=models,companions=companions)


def _saved_once(reader,cache,day,*,version_id):
    """Reuse a validated edition only inside this capture's pinned RO read."""
    key=(day,version_id)
    if key not in cache:
        cache[key]=_saved(reader,day,version_id=version_id)
    return cache[key]


def _closed_prefix_receipts(conn,reader,*,book,accounting,native_plan,source_versions,retained_inputs,saved_cache=None):
    """Admit only two already-posted, confirmed receipts behind closed26.

    This is a source proof, never a posting or an ordinary-history bypass.
    The accepted26 quantities and WB/retained sources remain immutable.
    """
    from packages.application import fbs_accounting_historical_revision as historical
    from packages.application.ff_pool_documents import DOCUMENTS_TABLE,REQUESTS_TABLE
    from packages.application.fbs_accounting_historical_revision_writer import REQUEST_PROOF_FIELDS
    from packages.application.operator_warehouse_documents import TABLE,assert_source
    from packages.application.fbs_snapshot_cost import _last_closed
    query_start=len(reader.queries)
    probe=evaluate_candidate(book['state'],accounting)
    pending=probe['pending_documents']
    if not pending:return None
    day='2026-10-26';ids=sorted(p['document_id'] for p in pending)
    require(len(ids)==len(set(ids))==2 and all(p.get('reason')=='late_closed_period_document'
        and p.get('business_date')==day for p in pending),'closed_prefix_unapproved_pending')
    periods=book['state']['periods']
    require(periods.get(day,{}).get('status')=='closed'
        and max(d for d,p in periods.items() if p['status']=='closed')==day
        and _last_closed({**book['state'],'periods':{d:p for d,p in periods.items() if d<day}})[0]=='2026-10-25',
        'closed_prefix_boundary_changed')
    known=historical._manifest_known(book['state'])
    by_id={d['document_id']:d for d in accounting['documents']}
    require({d['document_id'] for d in accounting['documents'] if d['document_id'] not in known
        and d['business_date']<=day}==set(ids),'closed_prefix_unselected_late_document')
    require(all(k in book and day in book[k] for k in historical.BOOK_DAY_FIELDS),'closed_prefix_saved_day_missing')
    frozen=deepcopy(periods[day]['snapshot'])
    require(frozen.get('complete') is True and frozen['date']==day,'closed_prefix_quantity_incomplete')
    exact=archived_quantity_snapshot(conn,day=day,observed_at=accounting['captured_at'],generation_id=frozen['id'])
    require(exact==frozen,'closed_prefix_frozen_quantity_changed')
    from packages.application.wb_fbs_warehouse_registry import REGISTRY_RUNS_TABLE,STOCK_RUNS_TABLE,STOCK_ROWS_TABLE
    reader.rows(f'SELECT * FROM {REGISTRY_RUNS_TABLE} WHERE run_id=?',(frozen['id'],))
    reader.rows(f'SELECT * FROM {STOCK_RUNS_TABLE} WHERE registry_run_id=? ORDER BY seller_warehouse_id',(frozen['id'],))
    reader.rows(f'SELECT r.* FROM {STOCK_ROWS_TABLE} r JOIN {STOCK_RUNS_TABLE} s ON s.run_id=r.run_id WHERE s.registry_run_id=? ORDER BY r.run_id,r.chrt_id',(frozen['id'],))
    wb=book['wb_days'][day];retained=book['retained_days'][day]
    require(source_versions.get(day)==wb['version_id'] and retained_inputs.get(day)==retained
        and retained['source_digest']==fingerprint({k:v for k,v in retained.items() if k!='source_digest'}),
        'closed_prefix_frozen_stage_changed')
    saved=_saved_once(reader,saved_cache if saved_cache is not None else {},day,version_id=wb['version_id'])
    from packages.application.shared_sku_cost_sources import capture_wb_component
    actual_wb=capture_wb_component(None,day=day,nm_ids=wb['requested_nm_ids'],version_id=wb['version_id'],connection=conn)
    require(actual_wb==wb and wb['complete'] and wb['authority_complete'],'closed_prefix_frozen_wb_changed')
    selected={int(nm) for nm in native_plan['sources']}
    requested=json.loads(saved['snapshot']['requested_nm_ids_json'])
    items={int(r['nm_id']):r for r in json.loads(saved['snapshot']['items_json'])}
    require(len(selected)==4 and selected<=set(requested) and selected<=set(items)
        and all(Decimal(items[nm]['wb_contour_quantity'])==0 for nm in selected),
        'closed_prefix_selected_wb_not_explicit_zero')
    proof={};requests={};actors={};receipts=[]
    for identity in ids:
        doc=by_id[identity]
        require(doc['kind']=='china_acceptance' and doc['source_type'] in {'china_acceptance_form','china_acceptance_workbook'}
            and doc['business_date']==day and doc['cost_document']['domain'].get('facility_id')==native_plan['facility_id']
            and all(line['facility_id']==native_plan['facility_id'] and line['pool'] in {'FBS','FBO'}
                for line in doc['cost_document']['lines']),'closed_prefix_receipt_scope_changed')
        raw=reader.one(f'SELECT * FROM {DOCUMENTS_TABLE} WHERE document_id=?',(identity,))
        posted=json.loads(raw['posted_manifest_json']);proof[identity]=raw['posted_manifest_json']
        require(posted['primary_document_id']==identity and len(posted['documents'])==1,
                'closed_prefix_receipt_companion_unapproved')
        request=reader.one(f"SELECT {','.join(REQUEST_PROOF_FIELDS)} FROM {REQUESTS_TABLE} WHERE request_id=?",(raw['request_id'],))
        request['state']=conn.execute(f'SELECT state FROM {REQUESTS_TABLE} WHERE request_id=?',(raw['request_id'],)).fetchone()[0]
        requests[request['request_id']]=request;actors[identity]=raw['actor']
        confirmation=reader.one(f'SELECT request_id,source_json,source_digest,accepted_at,actor FROM {TABLE} WHERE request_id=?',(raw['request_id'],))
        source=assert_source(conn,request)
        require(fingerprint(source)==confirmation['source_digest'] and confirmation['actor']
            and historical._time(request['accepted_at'])<=historical._time(confirmation['accepted_at'])<=historical._time(doc['posted_at']),
            'closed_prefix_confirmation_changed')
        source_capture={**accounting,'posted_manifest_json_by_id':proof,'native_requests_by_id':requests,'posted_actors_by_id':actors}
        historical._verify_native_operands({k:doc[k] for k in historical.HEADER_FIELDS},doc['cost_document'],posted)
        historical._verify_request_operands(doc,source_capture);historical._verify_receipt(doc)
        # Pin canonical normalized monetary operands, not only request metadata.
        for suffix,where,order in [('ff_pool_document_lines','document_id=?','line_no'),
                ('ff_pool_document_expense_lines','document_id=?','expense_line_no'),
                ('ff_pool_document_relations','child_document_id=?','relation_type')]:
            reader.rows(f'SELECT * FROM sheet_vitrina_v1_{suffix} WHERE {where} ORDER BY {order}',(identity,))
        reader.rows('SELECT * FROM sheet_vitrina_v1_ff_pool_movement_lines WHERE operation_id=? ORDER BY line_no',(doc['operation_id'],))
        reader.rows('SELECT * FROM sheet_vitrina_v1_warehouse_business_operations WHERE operation_id=?',(doc['operation_id'],))
        receipts.append(dict(document_id=identity,fingerprint=doc['fingerprint'],source_id=doc['source_id'],
            request_id=request['request_id'],request_digest=fingerprint({k:v for k,v in request.items() if k!='state'}),
            posted_manifest_sha256=doc['posted_manifest_sha256'],confirmation_source_digest=confirmation['source_digest']))
    require(len({r['source_id'] for r in receipts})==2 and len(requests)==2,'closed_prefix_receipt_source_duplicate')
    result=dict(contract=CLOSED_PREFIX_CONTRACT,date=day,receipt_document_ids=ids,receipts=receipts,
        frozen_quantity_digest=fingerprint(frozen),frozen_wb_digest=fingerprint(wb),frozen_retained_digest=fingerprint(retained),
        immutable_prefix_digest=fingerprint(historical._prefix(book,day)),
        known_manifest_digest=fingerprint(known),pending_documents=deepcopy(pending),
        query_fence=json.loads(canonical(ready.pin_queries(conn,reader.queries[query_start:]))))
    result['source_digest']=fingerprint(result)
    return result


def capture_temporary_allocation_repair(conn,*,runtime_dir,book,book_version,native_plan,evidence,now,source_versions,retained_inputs,targeted_version_ids=()):
    require(conn.in_transaction and conn.row_factory is sqlite3.Row and conn.execute('PRAGMA query_only').fetchone()[0]==1,'query_only_required')
    require(fingerprint(book)==book_version==native_plan['source_book_version'],'book_binding_changed')
    actual=handoff.read_historical_handoff_plan(conn,plan_digest=native_plan['plan_digest'])
    require(actual==native_plan and evidence==native_plan['evidence'],'native_handoff_source_changed')
    first=native_plan['allocation_date'];dates=[r['dispatch']['business_date'] for r in native_plan['orders'] if 'dispatch' in r]
    handoff._boundary(book,allocation_date=first,handoff_dates=dates)
    reader=_Reader(conn)
    saved_cache={}
    db=Path(conn.execute('PRAGMA database_list').fetchone()[2])
    timestamp=now.isoformat().replace('+00:00','Z') if isinstance(now,datetime) else now
    observed=datetime.fromisoformat(timestamp.replace('Z','+00:00'))
    accounting=capture_current(db,now=observed,include_baseline=False,connection=conn)
    require(accounting['documents_complete'] and accounting['quantity_snapshot']['complete'],'complete_current_sources_required')
    closed_prefix=_closed_prefix_receipts(conn,reader,book=book,accounting=accounting,native_plan=native_plan,
        source_versions=source_versions,retained_inputs=retained_inputs,saved_cache=saved_cache)
    repair_first=closed_prefix['date'] if closed_prefix else first
    targets=_days(repair_first,accounting['business_date'])
    documents=accounting['documents'];by_id={d['document_id']:d for d in documents}
    allocation_id=native_plan['allocation']['document']['document_id']
    require(allocation_id in by_id,'allocation_cost_document_missing')
    nms=sorted(int(n) for n in native_plan['sources']);marks=','.join('?' for _ in nms)
    candidates=[d for d in documents if d['kind']=='pool_reallocation' and d['business_date']==max(dates)
        and d['cost_document']['domain'].get('facility_id')==native_plan['facility_id']
        and d['cost_document']['domain'].get('source_pool')=='FBS' and d['cost_document']['domain'].get('destination_pool')=='FBO']
    require(len(candidates)==1,'single_return_allocation_required');returned=candidates[0]
    require(not returned['cost_document']['expense_lines'],'return_expense_unapproved')
    expected={nm:int(native_plan['sources'][str(nm)]['quantity'])-sum(r['order']['nm_id']==nm and 'dispatch' in r for r in native_plan['orders']) for nm in nms}
    destination=[m for m in returned['cost_document']['movements'] if m['pool']=='FBO']
    require({int(m['nm_id']):int(m['quantity_delta']) for m in destination}==expected and sum(expected.values())==5368,'return_quantity_changed')
    known={allocation_id,returned['document_id']}
    physical=[by_id[allocation_id],returned]
    for row in native_plan['orders']:
        if 'dispatch' not in row:continue
        event_id=handoff._event_id(native_plan,row)
        op_id='ffbo_'+hashlib.sha256(event_id.encode()).hexdigest()[:28];known.add(op_id)
        operation=reader.one(f'SELECT * FROM sheet_vitrina_v1_warehouse_business_operations WHERE operation_id=?',(op_id,))
        movements=reader.rows('SELECT * FROM sheet_vitrina_v1_ff_pool_movement_lines WHERE operation_id=? ORDER BY line_no',(op_id,))
        reader.rows(f'SELECT * FROM {handoff.lifecycle.EVENTS_TABLE} WHERE event_id=?',(event_id,))
        physical.append(dict(document_id=op_id,business_date=operation['business_date'],posted_at=operation['posted_at'],
            cost_document={'movements':movements},fingerprint=fingerprint([operation,movements])))
    order_ids=[r['order']['order_id'] for r in native_plan['orders']]
    order_marks=','.join('?' for _ in order_ids)
    for table in (handoff.orders.OBSERVATIONS_TABLE,handoff.orders.STATUS_OBSERVATIONS_TABLE):
        reader.rows(f'SELECT * FROM {table} WHERE order_id IN ({order_marks}) AND observed_at<=? ORDER BY observation_sequence',(*order_ids,timestamp))
    all_movements=reader.rows('SELECT o.operation_id,o.operation_type,o.business_date,o.source_id,o.source_revision,l.facility_id,l.pool,l.nm_id,l.quantity_delta,l.capital_delta_rub '
        'FROM sheet_vitrina_v1_warehouse_business_operations o JOIN sheet_vitrina_v1_ff_pool_movement_lines l USING(operation_id) '
        f'WHERE l.nm_id IN ({marks}) AND o.business_date>=? AND o.business_date<=? AND o.posted_at<=? ORDER BY o.business_date,o.operation_id,l.line_no',(*nms,first,targets[-1],timestamp))
    require(all(m['operation_id'] in known for m in all_movements),'unapproved_selected_cost_operation')
    # A supplier WB source would require its own shipment-specific cost; this
    # initial-FBS bridge cannot silently price or attribute such stock.
    fbo=reader.rows('SELECT s.* FROM sheet_vitrina_v1_wb_supplies s WHERE EXISTS(SELECT 1 FROM json_each(s.raw_goods_json) g '
        f"WHERE CAST(coalesce(json_extract(g.value,'$.nmID'),json_extract(g.value,'$.nmId')) AS INTEGER) IN ({marks})) ORDER BY s.supply_id",nms)
    require(not fbo,'selected_wb_supplier_source_present')
    acceptance=reader.rows(f"SELECT * FROM {P}functional_events WHERE event_type='wb_final_acceptance' AND nm_id IN ({marks}) ORDER BY event_id",nms)
    require(not acceptance,'selected_wb_acceptance_source_present')
    quantities={};saved={};retained={}
    for day in targets:
        quantities[day]=(deepcopy(book['state']['periods'][day]['snapshot']) if closed_prefix and day==repair_first
            else archived_quantity_snapshot(conn,day=day,observed_at=timestamp))
        require(day in source_versions,'pre_native_version_binding_missing')
        saved[day]=_saved_once(reader,saved_cache,day,version_id=source_versions[day])
        ids=sorted({int(r['nm_id']) for r in quantities[day]['rows']})
        require(day in retained_inputs,'pre_native_retained_binding_missing')
        retained[day]=deepcopy(retained_inputs[day])
        require(retained[day]['date']==day and retained[day]['wb_version_id']==source_versions[day]
            and retained[day]['source_digest']==fingerprint({k:v for k,v in retained[day].items() if k!='source_digest'})
            and set(retained[day]['rows'])=={str(nm) for nm in ids},'pre_native_retained_binding_changed')
    # Actual late-day zero, not a handoff-derived or fabricated available count.
    return_stock=quantities[returned['business_date']]
    require(all(int(r['quantity'])==0 for r in return_stock['rows'] if int(r['nm_id']) in nms),'actual_return_day_zero_required')
    predecessor=(date.fromisoformat(first)-timedelta(days=1)).isoformat()
    require(predecessor in source_versions,'pre_native_predecessor_binding_missing')
    previous=_saved_once(reader,saved_cache,predecessor,version_id=source_versions[predecessor])
    # The saved dated WB source predates some already-posted native documents.
    # Reconstruct these sources explicitly instead of taking a later current
    # snapshot (or discarding the late receipts and supplier corrections).
    from packages.application.fbs_temporary_allocation_foreign_sources import capture_foreign_suffix_sources
    all_physical={d['document_id']:d for d in documents}
    all_physical.update({d['document_id']:d for d in physical})
    physical=list(all_physical.values())
    foreign=capture_foreign_suffix_sources(conn,saved_by_day=saved,documents=physical,
        targeted_version_ids=list(targeted_version_ids),date_from=repair_first,date_to=targets[-1])
    accounting['native_requests_by_id']={}
    accounting['archived_dated_inputs']={d:{'quantity_snapshot':q} for d,q in quantities.items()}
    source_queries=original_source_queries(accounting)
    already_read={(sql,tuple(args)) for sql,args in reader.queries}
    for sql,args in source_queries:
        key=(sql,tuple(args))
        if key not in already_read:
            reader.rows(sql,args)
            already_read.add(key)
    # The entire reconstruction consumes only immutable raw/native operands.
    # The immutable original book is loaded by book_version for every rebuild;
    # retaining another full copy here adds no source authority.
    result=dict(contract=CONTRACT,book_version=book_version,native_plan=deepcopy(native_plan),
        accounting_capture=accounting,quantity_snapshots=quantities,dates=saved,retained=retained,previous=previous,
        physical_documents=physical,foreign_sources=foreign,closed_prefix_repair=closed_prefix,
        return_document_id=returned['document_id'],selected_nm_ids=nms,prepared_at=timestamp,
        cost_interval_proof=dict(complete=True,unexpected_operations=[],native_plan_digest=native_plan['plan_digest'],
            date_from=first,date_to=targets[-1],movements_digest=fingerprint(all_movements)),
        query_fence=json.loads(canonical(ready.pin_queries(conn,reader.queries)))+foreign['query_fence'])
    result['source_digest']=fingerprint(result)
    return result


def _revise_retained(original,*,wb,selected,version_id,saved_version):
    retained=deepcopy(original);retained['wb_version_id']=version_id
    revised_wb={int(r['nm_id']):r for r in wb['rows']}
    for nm,row in retained['rows'].items():
        if int(nm) not in selected:continue
        value=revised_wb[int(nm)]
        require(value['status']=='available','selected_retained_wb_unavailable')
        q=Decimal(str(value['quantity']));c=Decimal(value['capital_rub'])
        with localcontext() as context:
            context.prec=50
            row['stages']['WB'].update(quantity=str(value['quantity']),capital_rub=value['capital_rub'],
                wac_rub=format(c/q,'f') if q else None,
                revision_proof=dict(contract=CONTRACT,saved_version=saved_version,revised_version=version_id))
        # Other stage values and any preexisting diagnostic remain exact.
    retained['source_digest']=fingerprint({k:v for k,v in retained.items() if k!='source_digest'})
    return retained


def _rebuild_retained(original,*,wb,balances,version_id,saved_version,source_proof):
    """Derive operands from the authenticated full six-stage reconstruction.

    A superseded presentation binding may be replaced by that proof. Unknown
    missing values on an otherwise bound row remain a blocking incident.
    """
    from packages.application.fbs_inventory_presentation import RETAINED_STAGES
    retained=deepcopy(original);retained['wb_version_id']=version_id
    by_key={(r['warehouse_key'],int(r['nm_id'])):r for r in balances}
    wb_rows={int(r['nm_id']):r for r in wb['rows']}
    replaced={'stage_projection_missing','projection_functional_version_mismatch',
              'projection_date_binding_mismatch','projection_exact_version_missing'}
    for nm,row in retained['rows'].items():
        reason=row.get('reason','')
        require(not reason or reason in replaced,'shared_cost_incomplete:unknown_retained_incident')
        for stage in RETAINED_STAGES:
            old=row['stages'][stage]
            if stage!='WB' and not reason:
                require(old['quantity'] is not None and old['capital_rub'] is not None,
                        'shared_cost_incomplete:unproved_retained_operand')
            value=by_key.get((stages.STAGES[stage],int(nm)))
            if stage=='WB':
                exact=wb_rows[int(nm)]
                require(exact['status']=='available','shared_cost_incomplete:wb_operand')
                q=Decimal(str(exact['quantity']));c=Decimal(exact['capital_rub'])
            elif value:
                require(wf._public_line_payload(value)['capital_rub'] is not None,'shared_cost_incomplete:stage_cost')
                q=Decimal(value['quantity']);c=Decimal(value['capital_rub'])
            else:
                # _saved authenticates the complete six stage payloads; the
                # pure source rebuild preserves their closed-world coverage.
                q=c=Decimal(0)
            with localcontext() as context:
                context.prec=160
                old_zero=old.pop('zero_evidence',None)
                old.update(quantity=format(q,'f'),capital_rub=format(c,'f'),wac_rub=format(c/q,'f') if q else None,
                    revision_proof=dict(contract=CONTRACT,saved_version=saved_version,revised_version=version_id,
                        source_set_digest=source_proof,previous_presentation_reason=reason,
                        previous_presentation_zero_evidence=old_zero))
        row['reason']=''
    retained['source_digest']=fingerprint({k:v for k,v in retained.items() if k!='source_digest'})
    return retained


def build_temporary_allocation_repair(book,*,capture,native_plan):
    require(capture['contract']==CONTRACT and capture['source_digest']==fingerprint({k:v for k,v in capture.items() if k!='source_digest'}),'capture_changed')
    require(fingerprint(book)==capture['book_version']==native_plan['source_book_version'] and native_plan==capture['native_plan'],'book_changed')
    closed_prefix=capture.get('closed_prefix_repair')
    if closed_prefix:
        from packages.application.fbs_accounting_historical_revision import _prefix
        require(closed_prefix['contract']==CLOSED_PREFIX_CONTRACT and closed_prefix['date']=='2026-10-26'
            and len(set(closed_prefix['receipt_document_ids']))==len(closed_prefix['receipt_document_ids'])==2
            and closed_prefix['source_digest']==fingerprint({k:v for k,v in closed_prefix.items() if k!='source_digest'})
            and closed_prefix['immutable_prefix_digest']==fingerprint(_prefix(book,'2026-10-26'))
            and closed_prefix['frozen_quantity_digest']==fingerprint(book['state']['periods']['2026-10-26']['snapshot'])
            and closed_prefix['frozen_wb_digest']==fingerprint(book['wb_days']['2026-10-26'])
            and closed_prefix['frozen_retained_digest']==fingerprint(book['retained_days']['2026-10-26']),
            'closed_prefix_proof_changed')
    candidate=deepcopy(book);state=deepcopy(book['state']);first=min(capture['dates']);last=max(capture['dates'])
    state['periods']={d:p for d,p in state['periods'].items() if d<first}
    state['pending_documents']=[]
    state['last_attempt']={}
    editions={};anchors={};daily=[];previous=capture['previous']['snapshot']
    for day,saved in sorted(capture['dates'].items()):
        dated=deepcopy(capture['accounting_capture']);dated['business_date']=day
        dated['quantity_snapshot']=deepcopy(capture['quantity_snapshots'][day])
        dated['source_digest']=fingerprint([capture['accounting_capture']['source_digest'],day,dated['quantity_snapshot']])
        state=evaluate_candidate(state,dated)
        require(state['last_attempt']['status']=='evaluated' and not state['pending_documents'],
            'fbs_suffix_incomplete:'+canonical(dict(day=day,pending=[{k:p.get(k) for k in ('document_id','reason','business_date')}
                for p in state['pending_documents'][:8]],attempt=state['last_attempt']['status'])))
        if day<last:state=close_candidate_period(state,day,today=last)
        require(candidate_period_view(state,day)['quality']!='incomplete','fbs_period_unpriced')
        snapshot=deepcopy(saved['snapshot'])
        watermark=json.loads(saved['version']['source_watermarks_json'])['wb_snapshot']
        snapshot['source_snapshot_id']=watermark['snapshot_id']
        basis=(None if closed_prefix and day==closed_prefix['date'] else initial.build_basis(native_plan=native_plan,snapshot=snapshot,previous_snapshot=previous,
            previous_version=saved['version']['version_id'],prepared_at=capture['prepared_at'],cost_interval_proof=capture['cost_interval_proof']))
        items={int(r['nm_id']):r for r in json.loads(snapshot['items_json'])}
        from packages.application.fbs_temporary_allocation_foreign_sources import rebuild_foreign_suffix_day
        rebuilt=rebuild_foreign_suffix_day(saved,day=day,documents=capture['physical_documents'],
            foreign_capture=capture['foreign_sources'])
        balances=deepcopy(rebuilt['balances'])
        for row in balances:
            nm=int(row['nm_id'])
            if row['warehouse_key']!='wb' or nm not in capture['selected_nm_ids']:continue
            item=items[nm];q=Decimal(item['wb_contour_quantity'])
            require(q==Decimal(row['quantity']),'raw_wb_quantity_changed')
            if closed_prefix and day==closed_prefix['date']:
                require(q==0,'closed_prefix_selected_wb_not_explicit_zero')
                continue
            handoff_quantity=sum(1 for order in native_plan['orders'] if order['order']['nm_id']==nm
                and 'dispatch' in order and order['dispatch']['business_date']<=day)
            require(q<=handoff_quantity,'wb_quantity_exceeds_documented_handoffs')
            if not q and nm not in anchors:continue
            if nm not in anchors:
                resolved=initial.resolve_historical(nm,item,basis,business_date=day,snapshot_id=watermark['snapshot_id'],
                    fetched_at=snapshot['fetched_at'],previous_version=saved['version']['version_id'])
                require(resolved['available'],'historical_initial_wb_basis_unavailable')
                anchors[nm]=resolved['anchor']
            with localcontext() as context:
                context.prec=28
                wac=Decimal(anchors[nm]['wac_rub']);capital=q*wac
                row.update(wac_rub=wf._text(capital/q if q else wac),capital_rub=wf._text(capital),cost_covered_quantity=wf._text(q),
                    quality=initial.QUALITY,certified=0)
                row['provenance'].update(initial_valuation_anchor=deepcopy(anchors[nm]),historical_cost_reconstruction=CONTRACT,
                    specific_order_return_identity='unproved',proves_physical_ff_return=False)
                daily.append(wf._daily_wb_cost_row(day=day,nm_id=nm,quantity=q,wac=wac,quality=initial.QUALITY,
                    provenance=dict(source='documented_historical_fbs_handoff_initial_cost',snapshot_id=watermark['snapshot_id'],
                        initial_valuation_anchor=deepcopy(anchors[nm]),snapshot_carried_forward=False)))
        balances.sort(key=lambda row:(row['warehouse_key'],int(row['nm_id'])))
        version_id='temporary-fbs:'+fingerprint([CONTRACT,capture['source_digest'],day,balances])[7:]
        for row in balances:row['version_id']=version_id
        version,cloned=stages._clone_rows(saved,version_id=version_id,day=day,published_at=capture['prepared_at'],balances=balances,source_digest=capture['source_digest'])
        version['version_kind']='temporary_fbs_allocation_revision'
        cloned['snapshot_id']=wf._stable_id('wbsnapv',dict(source_snapshot_id=watermark['snapshot_id'],version_id=version_id))
        ids=sorted({int(r['nm_id']) for r in dated['quantity_snapshot']['rows']})
        wb=stages._cloned_wb_capture(version,cloned,balances,day=day,nm_ids=ids)
        retained=_rebuild_retained(capture['retained'][day],wb=wb,balances=balances,
            version_id=version_id,saved_version=saved['version']['version_id'],source_proof=capture['foreign_sources']['source_digest'])
        candidate['wb_days'][day]=wb;candidate['retained_days'][day]=retained
        candidate['shared_days'][day]=build_shared_cost_day(state,wb,day)
        candidate['presentations'][day]=FbsInventorySnapshot(fbs_state=state,wb_capture=wb,retained=retained,day=day).payload()
        require(candidate['shared_days'][day]['quality']=='complete' and candidate['presentations'][day]['totals']['total']['capital_rub'] is not None,'shared_cost_incomplete')
        # Original edition operands remain in capture['dates'] for recovery.
        # The published candidate needs only the derived edition and its proof.
        editions[day]=dict(version_id=version_id,version=version,snapshot=cloned,balances=balances,
            companions=deepcopy(rebuilt['companions']),full_physical_ff_proof=rebuilt['proof'])
        previous=snapshot
    require(anchors,'initial_wb_anchor_scope_missing')
    quality=dict(initial_wb_quality=initial.QUALITY,source_cost='immutable_documentary_fbs_handoff',
        initial_anchors={str(nm):a for nm,a in anchors.items()},specific_order_returns_proven=False,physical_ff_returns_created=0,
        pending_post_handoff_cancellations=[r['order']['order_id'] for r in native_plan['orders'] if 'dispatch' in r and r['status']['wb_status']=='canceled_by_client'])
    if closed_prefix:
        quality['closed_prefix_repair']=deepcopy(closed_prefix)
    candidate.update(state=state,prepared_at=capture['prepared_at'],source_digest=capture['accounting_capture']['source_digest'])
    if closed_prefix:
        require(fingerprint(_prefix(candidate,'2026-10-26'))==closed_prefix['immutable_prefix_digest'],'closed_prefix_changed')
    candidate.pop('publication_error',None)
    candidate['historical_revision']=dict(contract=CONTRACT,date_from=first,date_to=last,previous_revision=deepcopy(book.get('historical_revision')),
        native_stage_editions={d:e['version_id'] for d,e in editions.items()},quality_proof=quality)
    result=dict(contract=CONTRACT,candidate_book=candidate,after_book=fingerprint(candidate),editions=editions,
        query_fence=capture['query_fence'],dated_source_digest=capture['source_digest'],quality_proof=quality,
        foreign_source_fence=capture['foreign_sources']['query_fence']+(closed_prefix['query_fence'] if closed_prefix else []),
        native_handoff_plan_digest=native_plan['plan_digest'],daily_wb_costs=daily,initial_anchors=deepcopy(quality['initial_anchors']),
        source_document_manifest={d['document_id']:d['fingerprint'] for d in capture['accounting_capture']['documents']})
    result['manifest_digest']=fingerprint(result)
    return result


def publish_temporary_allocation_stages(conn,*,manifest):
    require(conn.in_transaction,'owned_transaction_required')
    receipts=stages.publish_dated_stages(conn,manifest=manifest)
    for item in manifest['daily_wb_costs']:
        row=dict(cutover_id=wf.FUNCTIONAL_CUTOVER_ID,as_of_date=item['as_of_date'],nm_id=item['nm_id'],quantity=item['quantity'],
            wac_rub=item['wac_rub'],capital_rub=item['capital_rub'],quality=item['quality'],provenance_json=canonical(item['provenance']),
            fingerprint=item['fingerprint'],created_at=manifest['candidate_book']['prepared_at'])
        updates=[k for k in row if k not in {'cutover_id','as_of_date','nm_id'}]
        conn.execute(f'INSERT INTO {P}wb_daily_cost({",".join(row)}) VALUES({",".join("?" for _ in row)}) '
            f'ON CONFLICT(cutover_id,as_of_date,nm_id) DO UPDATE SET {",".join(k+"=excluded."+k for k in updates)}',tuple(row.values()))
    return receipts


def verify_temporary_allocation_stage_readback(conn,*,candidate):
    for item in candidate['daily_wb_costs']:
        row=conn.execute(f'SELECT * FROM {P}wb_daily_cost WHERE cutover_id=? AND as_of_date=? AND nm_id=?',
                         (wf.FUNCTIONAL_CUTOVER_ID,item['as_of_date'],item['nm_id'])).fetchone()
        require(row and all(row[k]==item[k] for k in ('quantity','wac_rub','capital_rub','quality','fingerprint'))
                and json.loads(row['provenance_json'])==item['provenance'],'daily_readback_changed')
    for edition in candidate['editions'].values():
        actual=_parsed_balances([dict(r) for r in conn.execute(f'SELECT * FROM {P}functional_balances WHERE version_id=? ORDER BY warehouse_key,nm_id',(edition['version_id'],))])
        expected=sorted(edition['balances'],key=lambda r:(r['warehouse_key'],r['nm_id']))
        require(actual==expected,'native_balance_readback_changed')
    return True
