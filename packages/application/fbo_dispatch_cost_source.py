"""Immutable addressed dispatch as an exact prospective FBO cost operand.

Unlike historical closeout this is an ordinary live shipment. It neither
closes WB acceptance nor introduces retrospective WB-cost evidence.
"""
from decimal import Decimal, localcontext
import json

from packages.application import fbo_supply_lifecycle as owner

CONTRACT='fbo_dispatch_cost_source_v1'
KIND='fbo_lifecycle_dispatch'


def capture_documents(conn):
    if not {owner.OPERATIONS_TABLE, owner.CURRENT} <= owner._tables(conn):
        return []
    result=[]
    for row in conn.execute(f'SELECT * FROM {owner.OPERATIONS_TABLE} WHERE source_system=? AND operation_type=? ORDER BY posted_at,operation_id',(owner.CONTRACT,'wb_supply_dispatch')):
        header=dict(row)
        native=[dict(r) for r in conn.execute(f'SELECT * FROM {owner.LINES_TABLE} WHERE operation_id=? ORDER BY line_no',(header['operation_id'],))]
        aggregate=conn.execute(f'SELECT * FROM {owner.AGGREGATE} WHERE operation_id=? AND source_key=?',(header['operation_id'],header['source_id'])).fetchone()
        compat=[dict(r) for r in conn.execute(f'SELECT * FROM {owner.AGGREGATE_LINES} WHERE operation_id=? ORDER BY line_no',(header['operation_id'],))]
        if not aggregate or not native or len(native)!=len(compat):
            raise ValueError('fbo_dispatch_native_compat_proof_missing')
        lines=[]
        for movement,legacy in zip(native,compat):
            evidence=json.loads(movement['metadata_json'])
            snapshot=json.loads(legacy['raw_json']).get('cost_snapshot') or {}
            if (movement['pool']!='FBO' or movement['quantity_delta']>=0
                or movement['nm_id']!=legacy['nm_id'] or movement['quantity_delta']!=legacy['quantity_delta']
                or snapshot.get('capital_delta_rub')!=movement['capital_delta_rub']
                or snapshot.get('unit_cost_rub')!=movement['wac_snapshot_rub']
                or evidence.get('capital_delta_rub')!=movement['capital_delta_rub']):
                raise ValueError('fbo_dispatch_native_compat_proof_changed')
            with localcontext() as ctx:
                ctx.prec=160
                if (Decimal(evidence['before_capital_rub'])+Decimal(movement['capital_delta_rub'])!=Decimal(evidence['after_capital_rub'])
                    or evidence['before_quantity']+movement['quantity_delta']!=evidence['after_quantity']):
                    raise ValueError('fbo_dispatch_frozen_conservation')
            lines.append(evidence)
        from packages.application.fbo_dispatch_valuation import resolve
        native_digest=owner.digest(dict(header=header,lines=native,compat=compat))
        valuation=resolve(conn,header['operation_id'],lines,native_digest=native_digest)
        if valuation:
            lines=valuation['lines']
        metadata=json.loads(header['metadata_json'])
        document=dict(document_id=header['operation_id'],operation_id=header['operation_id'],root_document_id=header['operation_id'],
            kind=KIND,document_kind=KIND,business_date=header['business_date'],posted_at=header['posted_at'],
            fingerprint=owner.digest(dict(native_digest=native_digest,valuation_digest=valuation['valuation_digest'])) if valuation else native_digest,events=[],
            fbo_lifecycle_native=dict(contract=CONTRACT,operation_id=header['operation_id']),
            cost_document=dict(contract=CONTRACT,domain=dict(facility_id=metadata['origin']['facility_id'],pool='FBO',supply_id=metadata['supply_id'],source_key=header['source_id']),
                lines=lines,expense_lines=[],movements=lines,relations=[]))
        if valuation:
            document['dispatch_valuation']={key:value for key,value in valuation.items() if key!='lines'}
        result.append(document)
    return result


def apply_dispatch(doc, *, effect, node, key):
    data=doc['cost_document']
    if data.get('contract')!=CONTRACT or (doc.get('fbo_lifecycle_native') or {}).get('contract')!=CONTRACT:
        raise ValueError('typed_fbo_lifecycle_dispatch_required')
    for line in data['lines']:
        location=key(line['facility_id'],'FBO',int(line['nm_id']))
        n=node(location)
        # A frozen debit reduces the remaining stock, not the approved daily
        # opening-plus-receipts allocation weight for already posted expenses.
        if doc.get('dispatch_valuation'):
            n['frozen_dispatch_quantity']=n.get('frozen_dispatch_quantity',0)-line['quantity_delta']
        # Retain exact carried capital across quiet periods and later receipts.
        # This is a fixed debit; no daily average may reprice shipped goods.
        n['current_fbo_boundary']=dict(operation_id=doc['operation_id'],posted_at=doc['posted_at'],policy=CONTRACT)
        effect(doc,location,'adjustment',q=line['quantity_delta'],value=Decimal(line['capital_delta_rub']))
