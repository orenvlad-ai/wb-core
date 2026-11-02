"""Documentary initial WB valuation from verified historical FBS handoffs.

This is a cost anchor, not evidence that a particular order returned. It keeps
actual WB components and the real time of reconstruction. Ordinary same-day
initial-valuation admission is unchanged.
"""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, localcontext

from packages.application.wb_initial_fbs_cost import QUALITY, digest, decimal
from packages.application.ff_pool_foundation import canonical_decimal_ratio_text

CONTRACT = 'historical_documented_fbs_initial_wb_basis_v1'
POLICY = 'historical_documented_fbs_outbound_initial_wb_valuation_v1'


def _require(value, reason):
    if not value:
        raise ValueError('historical_initial_wb_' + reason)


def build_basis(*, native_plan, snapshot, previous_snapshot, previous_version, prepared_at,
                cost_interval_proof):
    """Pure part of an owner-verified native capture; never a current price."""
    _require(native_plan['plan_digest'] == digest({k:v for k,v in native_plan.items() if k!='plan_digest'}), 'native_plan_changed')
    day=snapshot['snapshot_date']
    _require(native_plan['allocation_date'] <= day and previous_snapshot['snapshot_date'] < day,
             'economic_dates_invalid')
    _require(datetime.fromisoformat(prepared_at.replace('Z','+00:00')) >= datetime.fromisoformat(snapshot['fetched_at'].replace('Z','+00:00')),
             'observation_time_invalid')
    _require(cost_interval_proof['complete'] is True and cost_interval_proof['unexpected_operations']==[]
             and cost_interval_proof['native_plan_digest']==native_plan['plan_digest'], 'cost_continuity_unproven')
    import json
    prior={int(r['nm_id']):r for r in json.loads(previous_snapshot['items_json'])}
    current={int(r['nm_id']):r for r in json.loads(snapshot['items_json'])}
    rows=[]
    for nm_text,source in sorted(native_plan['sources'].items()):
        nm=int(nm_text)
        _require(nm in prior and nm in current, 'official_quantity_scope_missing')
        if decimal(prior[nm]['wb_contour_quantity']) != 0:
            continue
        item=current[nm]
        contour=decimal(item['wb_contour_quantity'])
        if contour==0:continue
        _require(decimal(item['quantity'])==0 and contour==decimal(item['in_way_to_client'])+decimal(item['in_way_from_client']),
                 'physical_stock_source_unproved')
        handoffs=[r for r in native_plan['orders'] if r['order']['nm_id']==nm and 'dispatch' in r
                  and r['dispatch']['business_date']<=day]
        _require(handoffs and contour<=len(handoffs), 'quantity_exceeds_documented_handoffs')
        wac=canonical_decimal_ratio_text(source['capital_rub'],source['quantity'])
        _require(source['wac_rub']==wac and all(r['wac_rub']==wac for r in handoffs), 'frozen_native_cost_changed')
        rows.append(dict(nm_id=nm,facility_id=native_plan['facility_id'],wac_rub=wac,
            original_quantity=source['quantity'],original_capital_rub=source['capital_rub'],
            original_document_id=native_plan['original']['document']['document_id'],
            original_document_digest=digest(native_plan['original']),
            allocation_document_id=native_plan['allocation']['document']['document_id'],
            allocation_document_digest=digest(native_plan['allocation']),allocation_date=native_plan['allocation_date'],
            handoffs=[dict(order_id=r['order']['order_id'],business_date=r['dispatch']['business_date'],
                          frozen_wac_rub=r['wac_rub']) for r in handoffs],
            previous_snapshot_quantity='0',official_components=deepcopy(item)))
    result=dict(contract=CONTRACT,available=True,business_date=day,wb_version_id=previous_version,
        snapshot_id=snapshot['source_snapshot_id'],snapshot_digest=snapshot['raw_rows_digest'],
        fetched_at=snapshot['fetched_at'],prepared_at=prepared_at,source_book_version=native_plan['source_book_version'],
        native_plan_digest=native_plan['plan_digest'],previous_snapshot_id=previous_snapshot['snapshot_id'],
        previous_snapshot_digest=previous_snapshot['raw_rows_digest'],previous_snapshot_date=previous_snapshot['snapshot_date'],
        cost_interval_proof=deepcopy(cost_interval_proof),rows=rows)
    result['source_digest']=digest(result)
    return result


def resolve_historical(nm_id,item,basis,*,business_date,snapshot_id,fetched_at,previous_version):
    def missing(reason):return dict(available=False,reason='historical_initial_wb_'+reason)
    try:
        _require(basis.get('contract')==CONTRACT and basis['available'] is True and
            basis['source_digest']==digest({k:v for k,v in basis.items() if k!='source_digest'}),'basis_changed')
        _require(basis['business_date']==business_date and basis['snapshot_id']==snapshot_id
            and basis['fetched_at']==fetched_at and basis['wb_version_id']==previous_version,'snapshot_binding_changed')
        rows=[r for r in basis['rows'] if int(r['nm_id'])==nm_id]
        if not rows:return missing('sku_source_missing')
        _require(len(rows)==1,'duplicate_sku')
        row=rows[0]
        _require(all(decimal(row['official_components'][k])==decimal(item[k]) for k in
            ('quantity','in_way_to_client','in_way_from_client','wb_contour_quantity')),'official_quantity_changed')
        _require(decimal(item['quantity'])==0 and decimal(item['wb_contour_quantity'])>0,'non_transit_quantity')
        _require(decimal(item['wb_contour_quantity'])<=len(row['handoffs']),'handoff_coverage_missing')
        wac=canonical_decimal_ratio_text(row['original_capital_rub'],row['original_quantity'])
        _require(row['wac_rub']==wac and all(r['frozen_wac_rub']==wac and r['business_date']<=business_date for r in row['handoffs']),
                 'native_price_changed')
        anchor=dict(policy=POLICY,nm_id=nm_id,business_date=business_date,snapshot_id=snapshot_id,
            wac_rub=wac,quality=QUALITY,prepared_at=basis['prepared_at'],fetched_at=fetched_at,
            predecessor_version_id=previous_version,book_version=basis['source_book_version'],
            native_plan_digest=basis['native_plan_digest'],source_digest=basis['source_digest'],
            documentary_basis=deepcopy(row),cost_interval_proof=deepcopy(basis['cost_interval_proof']),
            previous_zero_snapshot=dict(snapshot_id=basis['previous_snapshot_id'],digest=basis['previous_snapshot_digest'],business_date=basis['previous_snapshot_date']),
            proves_physical_movement=False,proves_specific_order_return=False,
            quantity_source='official_wb_snapshot_only',valuation_basis='immutable_documented_fbs_handoff_cost')
        anchor['proof_digest']=digest(anchor)
        _require(valid_historical_anchor(anchor),'anchor_invalid')
        return dict(available=True,wac_rub=wac,anchor=anchor)
    except (ValueError,TypeError,KeyError,ArithmeticError):
        return missing('documentary_proof_invalid')


def valid_historical_anchor(anchor):
    try:
        row=anchor['documentary_basis'];proof=anchor['cost_interval_proof']
        return (anchor.get('policy')==POLICY and anchor.get('quality')==QUALITY
            and anchor.get('proves_physical_movement') is False and anchor.get('proves_specific_order_return') is False
            and anchor['proof_digest']==digest({k:v for k,v in anchor.items() if k!='proof_digest'})
            and int(row['nm_id'])==int(anchor['nm_id'])
            and anchor['wac_rub']==row['wac_rub']==canonical_decimal_ratio_text(row['original_capital_rub'],row['original_quantity'])
            and decimal(anchor['wac_rub'])>0 and row['handoffs']
            and all(h['frozen_wac_rub']==anchor['wac_rub'] and row['allocation_date']<h['business_date']<=anchor['business_date'] for h in row['handoffs'])
            and proof['complete'] is True and proof['unexpected_operations']==[]
            and proof['native_plan_digest']==anchor['native_plan_digest']
            and anchor['previous_zero_snapshot']['business_date']<anchor['business_date']
            and datetime.fromisoformat(anchor['prepared_at'].replace('Z','+00:00'))>=datetime.fromisoformat(anchor['fetched_at'].replace('Z','+00:00')))
    except (ValueError,TypeError,KeyError,ArithmeticError):return False
