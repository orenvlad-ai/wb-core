"""Bounded, source-preserving editions for native historical FBO dispatches.

The native WB daily replay owns valuation policy. Missing daily snapshots remain
carried evidence, never manufactured official quantities. The original foreign
sources and actual dated snapshots are immutable.
"""
from copy import deepcopy
from contextlib import closing
from decimal import Decimal, localcontext
import json
import sqlite3

from packages.application.fbs_snapshot_cost import canonical, fingerprint
from packages.application import fbs_accounting_historical_stages as stages
from packages.application.fbs_inventory_presentation import FbsInventorySnapshot, RETAINED_STAGES
from packages.application.shared_sku_cost import build_shared_cost_day
from packages.application import warehouse_functional as wf
from packages.application.fbs_historical_fbo_stage_sources import capture_historical_fbo_stages

CONTRACT = 'fbs_historical_fbo_native_stages_v1'
P = stages.P
ZERO = Decimal(0)


def require(value, code):
    stages.require(value, 'historical_fbo_stage_' + code)


def _decimal(value):
    require(value is not None and not isinstance(value, bool), 'operand_missing')
    result = Decimal(str(value))
    require(result.is_finite(), 'operand_invalid')
    return result


def _txt(value):
    return format(value, 'f')


def _sources(capture):
    """Use the same acceptance identity and cost composition as the owner."""
    from packages.application.warehouse_stocks import _normalized_wb_record
    plan = capture['native_plan']
    debits = {}
    for effect in plan['effects']:
        if effect['kind'] != 'historical_wb_dispatch':
            continue
        for movement in effect['pool_movements']:
            key = (str(movement['supply_id']), int(movement['nm_id']))
            require(key not in debits, 'duplicate_dispatch')
            debits[key] = {**movement, 'business_date': effect['business_date']}
    components = wf._supply_downstream_component_index(capture['cost_layers'])
    previous = {}
    for event in capture['acceptance_events']:
        key = (str(event['source_id']), int(event['nm_id']))
        q, c = previous.get(key, (ZERO, ZERO))
        previous[key] = q + _decimal(event['quantity']), c + _decimal(event['capital_rub'])
    sources, events = [], []
    for identity, supplied in sorted(capture['supplies'].items()):
        raw = supplied['rawsource']
        record = _normalized_wb_record(raw)
        supply_id = str(record.get('supply_id') or '')
        wb_id = str(record.get('wb_supply_id') or '')
        require(str(identity) in {supply_id, wb_id}, 'supply_identity_changed')
        require(int(record.get('status_id') or 0) == wf.WB_FINAL_ACCEPTED_STATUS_ID
                and not wf._is_doprinato(record), 'final_regular_supply_required')
        day = wf._supply_business_date(record, raw)
        require(len(day) == 10, 'acceptance_date_missing')
        revision = wf._supply_revision(raw)
        for good in wf._validated_wb_goods(record):
            nm = int(good['nm_id'])
            debit = debits.pop((supply_id, nm), None) or debits.pop((wb_id, nm), None)
            require(debit is not None, 'exact_dispatch_missing')
            packed, accepted = _decimal(good['quantity']), _decimal(good['accepted_quantity'])
            require(packed == -_decimal(debit['quantity_delta']) and day >= debit['business_date'],
                    'dispatch_acceptance_scope_changed')
            # Match the canonical FF ledger reader: immutable capital is
            # authority, and its ratio uses the WB owner's Decimal policy.
            base = abs(_decimal(debit['capital_delta_rub']) / _decimal(debit['quantity_delta']))
            component = components.get((wb_id, nm)) or components.get((supply_id, nm))
            pre, cost = (wf.compose_supply_costs(outbound_ff_wac=base,
                pre_acceptance_addon=component['pre_acceptance_addon'], acceptance_addon=component['acceptance_addon'])
                if component else (base, base))
            proof = dict(supply_id=supply_id, wb_supply_id=wb_id, source_revision=revision,
                status_id=int(record['status_id']), packed_quantity=wf._text(packed),
                accepted_quantity=wf._text(accepted), ff_wac_at_ledger_debit_rub=wf._text(base),
                historical_native_plan_digest=plan['plan_digest'],
                downstream_cost_status='available' if component else 'pending',
                cost_freshness='current' if component else 'preliminary')
            if component:
                proof.update(downstream_pre_acceptance_addon_rub=wf._text(component['pre_acceptance_addon']),
                    wb_paid_acceptance_addon_rub=wf._text(component['acceptance_addon']),
                    downstream_cost_layer_fingerprint=component['inputs_hash'])
                if component.get('missing_cost_components'):
                    proof.update(cost_freshness='preliminary',downstream_cost_status='partially_available',
                        missing_cost_components=list(component['missing_cost_components']))
            else:
                proof['missing_cost_components'] = ['transit_or_downstream_services']
            source_hash = wf._hash(dict(revision=revision, nm_id=nm, good=good,
                accepted_unit_cost_rub=wf._text(cost), pre_acceptance_unit_cost_rub=wf._text(pre)))
            source_id = f'{supply_id}:{nm}'
            prior_q, prior_c = previous.get((source_id, nm), (ZERO, ZERO))
            # This restoration admits missing sources only. A preexisting
            # selected acceptance requires its own correction review.
            require((source_id,nm) not in previous, 'selected_acceptance_already_posted')
            dq = wf.accepted_quantity_delta(packed=packed, accepted=accepted, previously_posted=prior_q)
            dc = wf.accepted_capital_delta(packed=packed, accepted=accepted, unit_cost=cost,
                                          previously_posted_capital=prior_c)
            if dq or dc:
                event_id = 'whfe_' + wf._hash(('wb_final_acceptance', source_hash, nm, wf._text(dq)))[:24]
                events.append(dict(event_id=event_id, event_type='wb_final_acceptance', source_id=source_id,
                    source_fingerprint=source_hash, business_date=day, nm_id=nm,
                    quantity=wf._text(dq), capital_rub=wf._text(dc), provenance={**proof,
                        'cumulative_accepted_quantity': wf._text(dq), 'previously_posted_accepted_quantity':'0',
                        'accepted_quantity_delta':wf._text(dq), 'previously_posted_accepted_capital_rub':'0',
                        'target_accepted_capital_rub':wf._text(dc), 'accepted_capital_delta_rub':wf._text(dc),
                        'current_pool_retained_ratio':'0', 'current_pool_capital_delta_rub':wf._text(dc),
                        'source_correction':False, 'cost_source_correction':False}))
            sources.append(dict(nm_id=nm, source_id=source_id, source_fingerprint=source_hash,
                dispatch_date=debit['business_date'], acceptance_date=day, packed=wf._text(packed),
                accepted=wf._text(accepted), pre_cost=wf._text(pre), accepted_cost=wf._text(cost),
                short_quantity=wf._text(max(packed-accepted,ZERO)),
                unattributed_overage_quantity=wf._text(max(accepted-packed,ZERO)), provenance=proof))
    require(not debits, 'dispatch_goods_unconsumed')
    return sources, events


def _acceptance_audit(capture):
    """Preserve headline/identified goods differences without SKU attribution."""
    from packages.application.warehouse_stocks import _normalized_wb_record
    result=[]
    for supplied in capture['supplies'].values():
        raw=supplied['rawsource'];record=_normalized_wb_record(raw)
        goods=record['raw_goods']
        known=sum((_decimal(g.get('acceptedQuantity') if g.get('acceptedQuantity') is not None else g.get('accepted_quantity')) for g in goods),ZERO)
        headline=record.get('accepted_quantity')
        remainder=None if headline is None else _decimal(headline)-known
        require(remainder is None or remainder>=0,'acceptance_headline_less_than_identified_goods')
        result.append(dict(supply_id=record['supply_id'],source_digest=fingerprint(raw),
            accepted_headline_quantity=None if headline is None else _txt(_decimal(headline)),
            identified_accepted_quantity=_txt(known),unattributed_accepted_quantity=None if remainder is None else _txt(remainder),
            unattributed_cost='unavailable',goods=[{k:g.get(k) for k in ('nmID','barcode','quantity','acceptedQuantity','readyForSaleQuantity','unloadingQuantity')} for g in goods]))
    return result


def _validate_cost_layers(capture):
    """Bind actual refreshed compatibility rows to native money and raw fees."""
    plan=capture['native_plan'];expected={}
    for effect in plan['effects']:
        if effect['kind']=='historical_wb_dispatch':
            for movement in effect['pool_movements']:
                expected[(str(movement['supply_id']),int(movement['nm_id']))]=movement
    fees={sid:dict(services=ZERO,storage=ZERO,uploads=set()) for sid,_ in expected}
    for uid,source in capture['fee_sources'].items():
        upload=source['upload']
        require(upload['validation_status']=='ok' and not upload.get('deleted_at'),'fee_source_inactive')
        for line in source['lines']:
            if line.get('match_status')!='ok' or line.get('is_storage_line'):continue
            sid=str(line.get('matched_wb_supply_id') or '')
            if sid not in fees:
                sid=str(line.get('matched_wb_cache_key') or '').removeprefix('supply:')
            if sid not in fees:continue
            service=line.get('service_amount_with_vat_without_storage')
            service=line.get('amount_with_vat') if service is None else service
            fees[sid]['services']+=_decimal(service)
            fees[sid]['storage']+=_decimal(line.get('storage_allocated_amount_with_vat') or 0)
            fees[sid]['uploads'].add(uid)
    observed=set()
    for layer in capture['cost_layers']:
        key=(str(layer['wb_supply_id']),int(layer['nm_id']))
        require(key in expected and key not in observed,'cost_layer_scope_changed');observed.add(key)
        ref=json.loads(layer['component_status_json']).get('historical_fbo_native_source') or {}
        move=expected[key]
        require(ref.get('contract')=='historical_fbo_frozen_native_cost_layers_v1'
            and ref.get('plan_digest')==plan['plan_digest'] and ref.get('source_book')==capture['book_version']
            and int(ref.get('nm_id') or 0)==key[1]
            and ref.get('unit_cost_rub')==move['wac_rub']
            and ref.get('quantity_delta')==move['quantity_delta']
            and ref.get('capital_delta_rub')==move['capital_delta_rub'],'cost_layer_historical_basis_missing')
        fee=fees[key[0]];denominator=_decimal(layer['qty_denominator'])
        require(denominator>0 and set(str(layer.get('ff_upload_id') or '').split(','))==fee['uploads'],
                'cost_layer_fee_sources_stale')
        # Native compatibility columns use REAL. Exact originals remain in
        # upload sources; this tolerance covers only their binary conversion.
        for total,per,amount in (('ff_services_amount_total','ff_services_per_unit_rub',fee['services']),
                                ('ff_storage_amount_total','ff_storage_per_unit_rub',fee['storage'])):
            require(abs(_decimal(layer[total])-amount)<=Decimal('0.00000001') and
                abs(_decimal(layer[per])*denominator-amount)<=Decimal('0.00000001'),'cost_layer_fee_amount_stale')
    require(observed==set(expected),'cost_layer_coverage_incomplete')


def _scratch_replay(capture, events):
    replay = capture['wb_replay']
    versions, snapshots, balances = {}, {}, {}
    def add(version, snapshot, rows=()):
        versions[version['version_id']] = version
        snapshots[snapshot['snapshot_id']] = snapshot
        for row in rows:
            balances[(row['version_id'],row['warehouse_key'],int(row['nm_id']))] = row
    add(replay['cutover_version'], replay['cutover_snapshot'], replay['opening_balances'])
    for entry in replay['snapshots_by_day'].values():
        add(entry['version'],entry['snapshot'])
    for anchor in replay['initial_anchors']:
        add(anchor['version'],anchor['snapshot'],[anchor['balance']])
    # Real pinned book snapshots must be the newest source for their own dates.
    for entry in capture['dates'].values():
        add(entry['version'],entry['snapshot'])
    fields = {
        'functional_cutovers':['cutover_id','cutover_at'],
        'functional_versions':['version_id','cutover_id','version_kind','status','effective_at','created_at'],
        'wb_snapshots':['snapshot_id','version_id','snapshot_date','items_json'],
        'functional_balances':['version_id','warehouse_key','nm_id','quantity','capital_rub','wac_rub','cost_covered_quantity','provenance_json'],
        'functional_events':['event_id','event_type','business_date','nm_id','quantity','capital_rub','source_id','source_fingerprint','provenance_json','created_at'],
    }
    rows = {'functional_cutovers':[replay['cutover']], 'functional_versions':list(versions.values()),
        'wb_snapshots':list(snapshots.values()),'functional_balances':list(balances.values()),
        'functional_events':replay['acceptance_events']}
    require(replay['opening_cost_map'], 'opening_cost_map_missing')
    final_day = max(capture['dates'])
    current = capture['dates'][final_day]['snapshot']
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.row_factory=sqlite3.Row
        for table, keys in fields.items():
            conn.execute(f'CREATE TABLE {P}{table}({",".join(keys)})')
            conn.executemany(f'INSERT INTO {P}{table} VALUES({",".join("?" for _ in keys)})',
                             [[row.get(k) for k in keys] for row in rows[table]])
        conn.commit();conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN')
        result = wf.WarehouseFunctionalBlock._build_post_cutover_daily_cost_projection(None,
            captured_at=capture['dates'][final_day]['version']['created_at'], candidate_lines=[],
            candidate_snapshot={**current,'items':json.loads(current['items_json'])},
            new_events=events,opening_cost_map=replay['opening_cost_map'],cutover_mode=False,
            allow_partial=True,connection=conn,replay_nm_ids={int(m["nm_id"]) for e in capture["native_plan"]["effects"] for m in e["pool_movements"]})
    return result


def _add_source(result, *, stage, nm, quantity, capital, source, quality):
    if quantity == 0:return
    row=next((r for r in result if r['warehouse_key']==stage and int(r['nm_id'])==nm),None)
    if row is None:
        row=dict(warehouse_key=stage,nm_id=nm,quantity='0',capital_rub='0',cost_covered_quantity='0',
                 wac_rub=None,quality=quality,certified=0,wb_quantity='0',wb_in_way_to_client='0',
                 wb_in_way_from_client='0',provenance={'source_records':[]})
        result.append(row)
    records=row['provenance'].get('source_records')
    require(isinstance(records,list),'foreign_stage_sources_missing')
    records.append(source)
    q=_decimal(row['quantity'])+quantity;c=_decimal(row['capital_rub'])+capital
    row.update(quantity=_txt(q),capital_rub=_txt(c),wac_rub=_txt(c/q),
        cost_covered_quantity=_txt(_decimal(row['cost_covered_quantity'])+quantity),
        quality=quality if row.get('quality') in (None,quality) else 'mixed:'+','.join(sorted({row['quality'],quality})),certified=0)


def _stage_balances(saved, *, day, sources, daily, documents):
    result=deepcopy(saved['balances'])
    selected={s['source_id'] for s in sources}
    # Never remove, net, or match an unrelated discrepancy contribution.
    for row in result:
        if row['warehouse_key'] not in {'ff_to_wb','wb_acceptance_discrepancy'}:continue
        for record in row['provenance'].get('source_records',[]):
            require(record.get('source_id') not in selected and
                    all(r.get('source_id') not in selected for r in record.get('receipts',[])),
                    'selected_retained_source_already_present')
    ff, physical=stages._physical_ff_revision(saved,day=day,documents=documents)
    result=[r for r in result if r['warehouse_key']!='ff']+ff
    by_nm={int(r['nm_id']):r for r in daily if r['as_of_date']==day}
    selected_nms={s['nm_id'] for s in sources}
    for row in result:
        if row['warehouse_key']!='wb' or int(row['nm_id']) not in selected_nms:continue
        value=by_nm.get(int(row['nm_id']))
        require(value is not None,'selected_wb_valuation_unavailable')
        require(_decimal(value['quantity'])==_decimal(row['quantity']),'official_wb_quantity_changed')
        wac=_decimal(value['wac_rub']);q=_decimal(row['quantity'])
        row.update(wac_rub=_txt(wac),capital_rub=_txt(q*wac),cost_covered_quantity=_txt(q),
                   quality=value['quality'],certified=0)
        row['provenance'].update(daily_wac_replay=deepcopy(value['provenance']),
            daily_wac_fingerprint=value['fingerprint'], historical_fbo_cost_revision=CONTRACT)
        if any(s['nm_id']==int(row['nm_id']) and s['provenance']['cost_freshness']=='preliminary' for s in sources):
            row['quality']='periodic_snapshot_wac_provisional'
            row['provenance']['missing_cost_components']=['transit_or_downstream_services']
    for source in sources:
        if source['dispatch_date']>day:continue
        nm=source['nm_id'];pre=_decimal(source['pre_cost']);proof=deepcopy(source['provenance'])
        if day<source['acceptance_date']:
            q=_decimal(source['packed']);capital=q*pre
            _add_source(result,stage='ff_to_wb',nm=nm,quantity=q,capital=capital,
                quality='physical_movement_costs_pending' if proof['cost_freshness']=='preliminary' else 'supply_specific_downstream_cost',
                source={**proof,'source_id':source['source_id'],'business_date':source['dispatch_date'],
                    'flow_quantity':_txt(q),'flow_capital_rub':_txt(capital),'cost_covered_quantity':_txt(q),
                    'pre_acceptance_unit_cost_rub':_txt(pre),'dated_quantity_basis':'documentary_dispatch_pending_final_acceptance',
                    'historical_partial_acceptance_timing':'unavailable'})
        else:
            q=_decimal(source['short_quantity']);capital=q*pre
            receipt=dict(source_id=source['source_id'],source_fingerprint=source['source_fingerprint'],
                business_date=source['acceptance_date'],nm_id=nm,quantity=_txt(q),capital=_txt(capital),
                wac=_txt(pre),cost_covered_quantity=_txt(q),provenance={**proof,'paid_acceptance_excluded':True})
            _add_source(result,stage='wb_acceptance_discrepancy',nm=nm,quantity=q,capital=capital,
                quality='pooled_final_acceptance_discrepancy',source=dict(receipts=[receipt],doprinato_matches=[],paid_acceptance_excluded=True))
    return sorted(result,key=lambda r:(r['warehouse_key'],int(r['nm_id']))),physical


def build_historical_fbo_stage_revision(state_revision, *, capture):
    require(capture['source_digest']==fingerprint({k:v for k,v in capture.items() if k!='source_digest'}),'capture_digest_changed')
    require(capture['book_version']==state_revision['expected_book']==fingerprint(capture['book'])
        and capture['accounting_capture_digest']==state_revision['source_digest'],'book_binding_changed')
    require(set(capture['dates'])==set(state_revision['derived_dates_to_rebuild']),'dated_scope_incomplete')
    # Match the native supply and WB replay owner's Decimal policy, then use
    # wider precision only for exact retained source sums and physical money.
    with localcontext() as context:
        context.prec=28
        _validate_cost_layers(capture)
        sources,events=_sources(capture)
        require(events,'acceptance_events_missing')
        daily=_scratch_replay(capture,events)
    with localcontext() as context:
        context.prec=160
        candidate=deepcopy(capture['book']);candidate['state']=deepcopy(state_revision['candidate_state'])
        editions={}
        for day,saved in sorted(capture['dates'].items()):
            balances,physical=_stage_balances(saved,day=day,sources=sources,daily=daily,documents=capture['native_documents'])
            version_id='historical-fbo:'+fingerprint([CONTRACT,capture['source_digest'],day,balances])[7:]
            for row in balances:row['version_id']=version_id
            version,snapshot=stages._clone_rows(saved,version_id=version_id,day=day,
                published_at=candidate['state']['last_document_capture_at'],balances=balances,source_digest=capture['source_digest'])
            version['version_kind']='historical_fbo_dispatch_revision'
            wb=stages._cloned_wb_capture(version,snapshot,balances,day=day,nm_ids=candidate['wb_days'][day]['requested_nm_ids'])
            retained=deepcopy(candidate['retained_days'][day]);retained['wb_version_id']=version_id
            by_key={(r['warehouse_key'],str(r['nm_id'])):r for r in balances}
            for nm,row in retained['rows'].items():
                for stage in RETAINED_STAGES:
                    value=by_key.get((stages.STAGES[stage],nm))
                    row['stages'][stage].update(quantity=value['quantity'] if value else '0',
                        capital_rub=value['capital_rub'] if value else '0',wac_rub=value['wac_rub'] if value else None,
                        revision_proof=dict(contract=CONTRACT,saved_version=saved['version']['version_id'],
                            revised_version=version_id,source_set_digest=capture['source_digest']))
            retained['source_digest']=fingerprint({k:v for k,v in retained.items() if k!='source_digest'})
            candidate['wb_days'][day]=wb;candidate['retained_days'][day]=retained
            candidate['shared_days'][day]=build_shared_cost_day(candidate['state'],wb,day)
            candidate['presentations'][day]=FbsInventorySnapshot(fbs_state=candidate['state'],wb_capture=wb,retained=retained,day=day).payload()
            editions[day]=dict(version_id=version_id,version=version,snapshot=snapshot,saved=saved,
                balances=balances,companions=deepcopy(saved['companions']),full_physical_ff_proof=physical)
        quality=dict(native_source_cost='exact_historical_documentary_basis',
            missing_official_quantity_dates=state_revision['unavailable_quantity_dates'],
            wb_cost='provisional',fee_scope_digest=fingerprint(capture['fee_scope']),
            per_sku_acceptance=[{k:s[k] for k in ('source_id','nm_id','packed','accepted','short_quantity','unattributed_overage_quantity')} for s in sources],
            downstream_pending_sources=[s['source_id'] for s in sources if s['provenance']['cost_freshness']=='preliminary'],
            missing_quantity_periods_created=[],supply_acceptance_audit=_acceptance_audit(capture))
        candidate['historical_revision']=dict(date_from=min(editions),date_to=max(editions),previous_revision=deepcopy(candidate.get('historical_revision')),contract=CONTRACT,native_plan_digest=capture['native_plan']['plan_digest'],
            native_stage_editions={d:e['version_id'] for d,e in editions.items()},quality_proof=quality)
        result=dict(contract=CONTRACT,candidate_book=candidate,after_book=fingerprint(candidate),editions=editions,
            query_fence=capture['query_fence'],dated_source_digest=capture['source_digest'],new_acceptance_events=events,
            daily_wb_costs=[r for r in daily if int(r['nm_id']) in {s['nm_id'] for s in sources} and r['as_of_date']>=min(s['dispatch_date'] for s in sources)],quality_proof=quality)
        result['manifest_digest']=fingerprint(result)
        return result


def publish_historical_fbo_stages(conn, *, manifest):
    require(conn.in_transaction,'publication_transaction_required')
    require(manifest['manifest_digest']==fingerprint({k:v for k,v in manifest.items() if k!='manifest_digest'}),'manifest_corrupt')
    stages.check_query_fence(conn,manifest['query_fence'])
    # This owner publishes every derived edition in the same transaction as
    # the new immutable acceptance sources; no intermediate stage is visible.
    receipts=stages.publish_dated_stages(conn,manifest=manifest)
    first=manifest['editions'][min(manifest['editions'])]
    for event in manifest['new_acceptance_events']:
        row={k:v for k,v in event.items() if k!='provenance'}
        row.update(version_id=first['version_id'],created_at=first['version']['created_at'],
                   provenance_json=canonical(event['provenance']))
        conn.execute(f'INSERT INTO {P}functional_events({",".join(row)}) VALUES({",".join("?" for _ in row)})',tuple(row.values()))
    for item in manifest['daily_wb_costs']:
        row=dict(cutover_id=wf.FUNCTIONAL_CUTOVER_ID,as_of_date=item['as_of_date'],nm_id=item['nm_id'],
            quantity=item['quantity'],wac_rub=item['wac_rub'],capital_rub=item['capital_rub'],quality=item['quality'],
            provenance_json=canonical(item['provenance']),fingerprint=item['fingerprint'],created_at=first['version']['created_at'])
        columns=list(row)
        changed=[k for k in columns if k not in {'cutover_id','as_of_date','nm_id'}]
        conn.execute(f'INSERT INTO {P}wb_daily_cost({",".join(columns)}) VALUES({",".join("?" for _ in columns)}) '
            f'ON CONFLICT(cutover_id,as_of_date,nm_id) DO UPDATE SET {",".join(k+"=excluded."+k for k in changed)}',tuple(row.values()))
    return receipts
