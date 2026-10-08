"""Native supplier cost dependencies, separately from receipt admission.

These observations authorize no physical write. Supplier allocation owns its
dated source components; posted receipt and debit money retain their owners.
"""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
import json

from packages.application import operator_supplier_shipments as source
from packages.application import operator_supplier_processing as processing
from packages.application import ready_publication as ready

CONTRACT = "supplier_cost_dated_source_v1"
MAX_DATES = 366
P = "sheet_vitrina_v1_"


class SupplierHistoryError(ValueError):
    """An expected typed refusal; unknown native/programming errors propagate."""


def require(value, reason):
    if not value:
        raise SupplierHistoryError("supplier_history_" + reason)


def dates_between(first, last):
    begin, end = date.fromisoformat(first), date.fromisoformat(last)
    require(begin <= end and (end - begin).days < MAX_DATES, "date_scope_unavailable")
    return [(begin + timedelta(days=n)).isoformat() for n in range((end-begin).days+1)]


def component_manifest(conn, *, shipment_id, allocation):
    """Use native canonical allocation and its actual business component dates.

    The same native historical helpers already evaluate this source tuple.
    Their special July recovery's write authority is not reused here.
    """
    require(allocation and not allocation["blockers"], "native_cost_unavailable")
    events = [dict(r) for r in conn.execute(
        f"SELECT * FROM {P}own_capital_events WHERE shipment_id=? "
        "AND event_type IN ('supplier_payment','cost_payment') "
        "ORDER BY effective_date,event_type,event_id", (shipment_id,))]
    operations = [dict(r) for r in conn.execute(
        f"SELECT * FROM {P}cny_ledger_operations WHERE source_order_id=? "
        "ORDER BY sequence_key,operation_id", (shipment_id,))]
    dates = {"cny_operation:" + r["operation_id"]: r["operation_date"][:10]
             for r in operations if r.get("operation_date")}
    for event in events:
        payload = json.loads(event["payload_json"])
        for identity in payload.get("provenance", {}).get("expense_line_ids", []):
            dates["expense_line:" + str(identity)] = event["effective_date"]
    lines = []
    for line in allocation["lines"]:
        components = []
        for raw in line["components"]:
            component = deepcopy(raw)
            day = dates.get(component["source_component_id"]) or component.get("document", {}).get("date")
            require(day and date.fromisoformat(day).isoformat() == day, "component_business_date_missing")
            component["business_effective_date"] = day
            components.append(component)
        require(sum((Decimal(str(c["amount_rub"])) for c in components), Decimal(0))
                == Decimal(str(line["capital_rub"])), "component_money_not_conserved")
        lines.append({"line_id": line["line_id"], "nm_id": int(line["nm_id"]),
                      "quantity": str(line["quantity"]), "current_components": components})
    # Current canonical preparation may intentionally omit legacy own-capital
    # supplier_payment events. The actual allocator's counted CNY components
    # carry the ledger dates; requiring the legacy mirror would deadlock it.
    first = min((c["business_effective_date"] for line in lines for c in line["current_components"]
                 if c["component_key"] == "supplier_payment"), default="")
    require(first and first == allocation["first_payment_date"], "payment_business_date_missing")
    return {"shipment_id": shipment_id, "invoice_no": allocation["invoice_no"],
            "first_payment_date": first, "actual_shipment_date": allocation["actual_shipment_date"],
            "actual_ff_acceptance_date": allocation["actual_ff_acceptance_date"],
            "expenses_complete": allocation["expenses_complete"],
            "source_fingerprint": allocation["source_fingerprint"],
            "calculation_fingerprint": allocation["calculation_fingerprint"], "lines": lines,
            "events": events, "cny_operations": operations}


def receipt_dependency(conn, shipment_id):
    """Classify the actual native acceptance, never a user-supplied kind."""
    from packages.application.ff_pool_documents import GUIDED_REPLAYS_TABLE
    from packages.application.fbs_snapshot_cost_sources import _documents
    from packages.application.warehouse_functional import _ff_ledger_line_cost_snapshot
    operations = [dict(r) for r in conn.execute(
        f"SELECT * FROM {P}ff_stock_operations WHERE source_type IN ('supplier_shipment','supplier_shipment_acceptance') "
        "AND source_object_id=? ORDER BY created_at,operation_id", (shipment_id,))]
    if not operations:
        return {"kind": "unaccepted", "operations": [], "lines": [], "documents": []}
    require(len(operations) == 1, "acceptance_identity_ambiguous")
    operation = operations[0]
    lines = [dict(r) for r in conn.execute(
        f"SELECT * FROM {P}ff_stock_operation_lines WHERE operation_id=? ORDER BY line_no",
        (operation["operation_id"],))]
    require(lines and all(Decimal(str(r["quantity_delta"])) > 0 for r in lines), "acceptance_quantity_invalid")
    snapshots = [_ff_ledger_line_cost_snapshot(r) for r in lines]
    if all(s is None for s in snapshots):
        cutover = conn.execute(f"SELECT * FROM {P}warehouse_functional_cutovers WHERE cutover_id='warehouse_functional_cutover_v1'").fetchone()
        return {"kind": "legacy_supplier_allocation", "operations": operations, "lines": lines,
                "documents": [], "cutover": dict(cutover) if cutover else None}
    require(all(s is not None for s in snapshots), "acceptance_partial_frozen_basis")
    require(source._exists(conn, GUIDED_REPLAYS_TABLE), "acceptance_frozen_relation_missing")
    relations = [dict(r) for r in conn.execute(
        f"SELECT * FROM {GUIDED_REPLAYS_TABLE} WHERE shipment_id=? AND legacy_operation_id=?",
        (shipment_id, operation["operation_id"]))]
    require(len(relations) == 1, "acceptance_frozen_relation_missing")
    documents = [d for d in _documents(conn) if d["document_id"] == relations[0]["document_id"]]
    require(len(documents) == 1 and documents[0]["kind"] == "china_acceptance"
            and documents[0]["source_id"] == shipment_id, "acceptance_frozen_document_mismatch")
    return {"kind": "immutable_posted_supplier_receipt", "operations": operations,
            "lines": lines, "documents": documents, "relations": relations,
            "snapshots": [{k: str(v) if isinstance(v, Decimal) else v for k, v in s.items()} for s in snapshots]}


def dated_supplier_rows(saved_balances, *, shipment, day):
    """Replace only exact pre-FF source contributions, preserving quantities.

    No current destination can erase a past production/China source. Native
    component dates decide money; immutable same-date contributions decide the
    physical roster. A physical correction is outside this cost authority.
    """
    from packages.application.warehouse_historical_recovery import (
        _supplier_stage, _supplier_capital_by_nm, _supplier_record,
    )
    from packages.application.warehouse_business_projection import _reproject_supplier_source
    destination = _supplier_stage(business_date=day, shipment=shipment)
    if destination not in {"production", "china_to_ff"}:
        return deepcopy(saved_balances), {"kind": "outside_pre_ff_source", "date": day}
    sid = shipment["shipment_id"]
    prior = {}
    for row in saved_balances:
        if row["warehouse_key"] not in {"production", "china_to_ff"}:
            continue
        records = row.get("provenance", {}).get("source_records")
        require(isinstance(records, list), "supplier_dated_source_records_missing")
        require(sum((Decimal(str(r["flow_quantity"])) for r in records), Decimal(0)) == Decimal(str(row["quantity"]))
                and sum((Decimal(str(r["flow_capital_rub"])) for r in records), Decimal(0)) == Decimal(str(row["capital_rub"])),
                "supplier_dated_source_not_conserved")
        for record in records:
            if record.get("shipment_id") == sid:
                require(row["warehouse_key"] == destination, "supplier_physical_stage_changed")
                nm = int(record.get("nm_id") or row["nm_id"])
                prior[nm] = prior.get(nm, Decimal(0)) + Decimal(str(record["flow_quantity"]))
    expected = {}
    for line in shipment["lines"]:
        nm = line["nm_id"]
        expected[nm] = expected.get(nm, Decimal(0)) + Decimal(line["quantity"])
    require(prior == expected, "supplier_physical_quantity_changed")
    capital = _supplier_capital_by_nm(shipment, business_date=day)
    records = [_supplier_record(shipment, line=line, business_date=day, capital=sum(
        (Decimal(str(c["amount_rub"])) for c in line["current_components"] if c["business_effective_date"] <= day), Decimal(0)))
        for line in shipment["lines"]]
    require(all(Decimal(r["flow_capital_rub"]) > 0 for r in records), "supplier_dated_positive_cost_missing")
    plan = {"shipment_id": sid, "affected_nm_ids": sorted(expected), "business_effective_date": day,
            "after_header": shipment, "target_rows_after": [
                {"nm_id": nm, "provenance": {"source_records": [r for r in records if r["nm_id"] == nm]}}
                for nm in sorted(expected)]}
    after = _reproject_supplier_source(saved_balances, plan=plan, as_of_date=day)
    require({(r["warehouse_key"], r["nm_id"]): Decimal(str(r["quantity"])) for r in after}
            == {(r["warehouse_key"], r["nm_id"]): Decimal(str(r["quantity"])) for r in saved_balances},
            "cost_only_quantity_changed")
    return after, {"kind": "native_dated_supplier_components", "date": day,
                   "source_records": records, "capital_by_nm": {str(k): str(v) for k, v in capital.items()}}


def source_queries(op):
    """Exact source owner + native operands repeated in the final writer CAS."""
    sid = op["shipment_id"]
    fields = (("supplier_shipments", "shipment_id", "shipment_id"),
              ("supplier_shipment_lines", "shipment_id", "sort_order,line_id"),
              ("supplier_financial_documents", "supplier_order_id", "document_id"),
              ("supplier_financial_expense_lines", "supplier_order_id", "financial_document_id,sort_order,line_id"),
              ("cny_documents", "source_order_id", "document_id"),
              ("cny_ledger_operations", "source_order_id", "sequence_key,operation_id"),
              ("supplier_bank_operation_assignments", "supplier_order_id", "semantic_operation_id"),
              ("supplier_payment_fee_confirmations", "supplier_order_id", "payment_document_id,confirmation_id"),
              ("own_capital_events", "shipment_id", "effective_date,event_type,event_id"))
    return [(f"SELECT * FROM {P}{table} WHERE {field}=? ORDER BY {order}", (sid,))
            for table, field, order in fields]


def _dated_discrepancy(row, *, outbound, day):
    """Reconcile saved quantities with the native debit basis and native fold."""
    from packages.application import warehouse_functional as native
    receipts=deepcopy(row['provenance'].get('receipts'))
    matches=deepcopy(row['provenance'].get('doprinato_matches'))
    require(isinstance(receipts,list) and isinstance(matches,list),'dated_discrepancy_sources_missing')
    original,_=native.reconcile_discrepancies(discrepancies=receipts,doprinato=matches)
    require(len(original)==1 and Decimal(original[0]['quantity'])==Decimal(row['quantity']) and Decimal(original[0]['capital'])==Decimal(row['capital_rub']) and Decimal(original[0]['cost_covered_quantity'])==Decimal(row['cost_covered_quantity']),'dated_discrepancy_source_not_conserved')
    for receipt in receipts:
        provenance=receipt['provenance'];nm=int(receipt['nm_id'])
        supply=provenance.get('supply_id') or provenance.get('wb_supply_id')
        base=outbound.get((str(supply),nm))
        require(base is not None,'dated_discrepancy_debit_missing')
        old=Decimal(str(provenance['ff_wac_at_ledger_debit_rub']))
        if base==old:continue
        require('downstream_pre_acceptance_addon_rub' in provenance or provenance.get('downstream_cost_status')=='pending','dated_discrepancy_addon_missing')
        addon=Decimal(str(provenance.get('downstream_pre_acceptance_addon_rub') or 0));quantity=Decimal(receipt['quantity'])
        before,_=native.compose_supply_costs(outbound_ff_wac=old,pre_acceptance_addon=addon,acceptance_addon=Decimal(0))
        require(Decimal(receipt['capital'])==quantity*before and Decimal(receipt['cost_covered_quantity'])==quantity and provenance.get('paid_acceptance_excluded') is True,'dated_discrepancy_basis_changed')
        after,_=native.compose_supply_costs(outbound_ff_wac=base,pre_acceptance_addon=addon,acceptance_addon=Decimal(0))
        receipt.update(capital=native._text(quantity*after),wac=native._text(after));provenance['ff_wac_at_ledger_debit_rub']=native._text(base)
    revised,unmatched=native.reconcile_discrepancies(discrepancies=receipts,doprinato=matches)
    require(len(revised)==1 and Decimal(revised[0]['quantity'])==Decimal(row['quantity']) and Decimal(revised[0]['cost_covered_quantity'])==Decimal(row['cost_covered_quantity']) and [(r['source_id'],r['matched_quantity']) for r in revised[0]['matches']]==[(r['source_id'],r['matched_quantity']) for r in matches],'dated_discrepancy_quantity_changed')
    pool=revised[0];q,c=Decimal(pool['quantity']),Decimal(pool['capital'])
    row.update(capital_rub=native._text(c),wac_rub=native._text(c/q) if q else None)
    row['provenance'].update(receipts=receipts,doprinato_matches=pool['matches'],supplier_dated_discrepancy={'date':day,'contract':CONTRACT})


def dated_ff_rows(conn, saved, *, shipment, dependency, day, cohort=None):
    """Replay the real dated append-only prefix with the unchanged native fold.

    Opening money is frozen. Other mutable supplier operands must match the
    saved native calculation fingerprints; this source cannot authorize a
    foreign revision. Modern physical location money remains receipt-owned.
    """
    from packages.application import warehouse_functional as native
    from packages.application.operator_supplier_cost_proof import numerical_supplier_state
    from types import SimpleNamespace
    from packages.application.fbs_snapshot_cost import fingerprint
    cohort=cohort or [dict(shipment=shipment,receipt_dependency=dependency)]
    own={m['shipment']['shipment_id']:m['shipment'] for m in cohort}
    legacy=[m for m in cohort if m['receipt_dependency']['kind']=='legacy_supplier_allocation']
    if not legacy:
        return deepcopy(saved['balances']), {'kind':dependency['kind'], 'date':day}, []
    # Use the common saved native opening; immutable receipts remain frozen
    # in the fold even when a legacy source shares the SKU/prefix.
    dependency=legacy[0]['receipt_dependency']
    cutover=dependency['cutover'];require(cutover and cutover['cutover_id']==saved['version']['cutover_id'],'legacy_opening_identity_changed')
    if all(o['created_at']<=cutover['cutover_at'] for m in legacy for o in m['receipt_dependency']['operations']):
        return deepcopy(saved['balances']), {'kind':'immutable_cutover_opening','date':day}, []
    # Native rows are immutable physical facts. Both source time and the saved
    # edition frontier restrict the prefix; a newly backdated receipt belongs
    # to its separate physical authority, never this supplier cost revision.
    queries=[]
    def rows(sql,args=()):
        result=[dict(r) for r in conn.execute(sql,args)];require(len(result)<=100000,'ff_prefix_size_limit');queries.append((sql,args));return result
    boundary=cutover['cutover_at'];frontier=saved['version']['published_at'] or saved['version']['created_at']
    versions=rows(f"SELECT * FROM {P}warehouse_functional_versions WHERE cutover_id=? AND version_kind='functional_cutover' ORDER BY created_at,version_id",(cutover['cutover_id'],))
    require(versions,'legacy_opening_missing');opening_version=versions[0]['version_id']
    initial=rows(f"SELECT * FROM {P}warehouse_functional_balances WHERE version_id=? AND warehouse_key='ff' ORDER BY nm_id",(opening_version,))
    opening={r['nm_id']:{'quantity':Decimal(r['quantity']),'capital':Decimal(r['capital_rub']),'operations':[], 'opening_version_id':opening_version} for r in initial}
    ledger=rows(f"SELECT * FROM {P}ff_stock_operations WHERE created_at>? AND created_at<=? AND substr(coalesce(nullif(business_effective_date,''),created_at),1,10)<=? ORDER BY created_at,operation_id",(boundary,frontier,day))
    ledger=sorted(ledger,key=native._ff_operation_replay_sort_key)
    ids=[r['operation_id'] for r in ledger]
    lines=rows(f"SELECT * FROM {P}ff_stock_operation_lines WHERE operation_id IN ({','.join('?' for _ in ids)}) ORDER BY operation_id,line_no",tuple(ids)) if ids else []
    cost_rows=rows(f"SELECT * FROM {P}warehouse_opening_cost_map WHERE cutover_id=? ORDER BY nm_id",(cutover['cutover_id'],))
    cost_map={r['nm_id']:SimpleNamespace(ff_unit_cost=Decimal(r['ff_unit_cost_rub'])) for r in cost_rows}
    expected={nm:p['quantity'] for nm,p in opening.items()}
    for line in lines:expected[line['nm_id']]=expected.get(line['nm_id'],Decimal(0))+Decimal(line['quantity_delta'])
    by_operation={o['operation_id']:o for o in ledger};flows={}
    for line in lines:
        operation=by_operation[line['operation_id']]
        if operation['source_type']!='supplier_shipment' or Decimal(line['quantity_delta'])<=0 or native._ff_ledger_line_cost_snapshot(line) is not None:continue
        sid=operation['source_object_id'];nm=line['nm_id']
        if sid in own:
            member=own[sid];operands=[r for r in member['lines'] if r['nm_id']==nm]
            q=sum((Decimal(r['quantity']) for r in operands),Decimal(0))
            c=sum((Decimal(str(comp['amount_rub'])) for r in operands for comp in r['current_components'] if comp['business_effective_date']<=day),Decimal(0))
            provenance={'contract':CONTRACT,'shipment_id':sid,'date':day,'source_fingerprint':member['source_fingerprint']}
        else:
            state,allocation=numerical_supplier_state(conn,sid)
            prior=next((r for r in saved['companions']['supplier_cost_states'] if r['shipment_id']==sid),None)
            require(state and prior and state['source_fingerprint']==prior['source_fingerprint'] and state['calculation_fingerprint']==prior['calculation_fingerprint'],'foreign_supplier_operand_changed')
            foreign=component_manifest(conn,shipment_id=sid,allocation=allocation)
            operands=[r for r in foreign['lines'] if r['nm_id']==nm]
            q=sum((Decimal(r['quantity']) for r in operands),Decimal(0))
            c=sum((Decimal(str(comp['amount_rub'])) for r in operands for comp in r['current_components'] if comp['business_effective_date']<=day),Decimal(0))
            provenance={'saved_calculation_fingerprint':prior['calculation_fingerprint'],'shipment_id':sid,'date':day}
            queries.extend(source_queries({'shipment_id':sid}))
        require(q>0 and c>0,'dated_ff_supplier_cost_missing');flows[(sid,nm)]=(q,c,'native',provenance)
    try:
        pools,outbound=native.replay_ff_cost_pools(opening_pools=opening,operations=ledger,lines=lines,supplier_flow_costs=flows,cost_map=cost_map,boundary=boundary,expected_quantities=expected)
    except native.WarehouseFunctionalError as exc:
        # These are the native fold's explicit business/data guards, not an
        # unknown evaluator exception. Retain the intent and let other exact
        # sources progress; never publish a substitute zero or current pool.
        raise SupplierHistoryError('supplier_history_native_prefix_invalid') from exc
    result=deepcopy(saved['balances']);affected={r['nm_id'] for m in cohort for r in m['shipment']['lines']};evidence=[]
    for row in result:
        nm=row['nm_id']
        if nm not in affected:continue
        if row['warehouse_key']=='ff':
            records=row['provenance'].get('source_records',[])
            if any(r.get('physical_projection_source')=='current_facility_pool_exact_projection' for r in records):
                evidence.append({'nm_id':nm,'kind':'immutable_native_physical_locations'});continue
            pool=pools.get(nm);require(pool and Decimal(row['quantity'])==pool['quantity'],'dated_ff_quantity_changed')
            q,c=pool['quantity'],pool['capital'];row.update(capital_rub=native._text(c),wac_rub=native._text(c/q) if q else None)
            row['provenance']['supplier_dated_ff_prefix']={'date':day,'opening_version':opening_version,'operations':deepcopy(pool['operations'])}
            evidence.append({'nm_id':nm,'kind':'native_decimal_prefix','quantity':native._text(q),'capital_rub':native._text(c)})
        elif row['warehouse_key']=='wb_acceptance_discrepancy':
            _dated_discrepancy(row,outbound=outbound,day=day)
        elif row['warehouse_key']=='ff_to_wb':
            records=row['provenance'].get('source_records');require(isinstance(records,list),'dated_transit_source_missing')
            for record in records:
                supply=record.get('supply_id') or record.get('wb_supply_id');new=outbound.get((str(supply),nm))
                require(new is not None,'dated_transit_debit_missing')
                old=Decimal(str(record['ff_wac_at_ledger_debit_rub']))
                if new==old:continue
                require('downstream_pre_acceptance_addon_rub' in record or record.get('downstream_cost_status')=='pending','dated_transit_addon_missing')
                addon=Decimal(str(record.get('downstream_pre_acceptance_addon_rub') or 0))
                old_pre,_=native.compose_supply_costs(outbound_ff_wac=old,pre_acceptance_addon=addon,acceptance_addon=Decimal(0))
                require(Decimal(record['flow_capital_rub'])==Decimal(record['flow_quantity'])*old_pre,'dated_transit_money_not_conserved')
                new_pre,_=native.compose_supply_costs(outbound_ff_wac=new,pre_acceptance_addon=addon,acceptance_addon=Decimal(0))
                record.update(ff_wac_at_ledger_debit_rub=native._text(new),flow_capital_rub=native._text(Decimal(record['flow_quantity'])*new_pre))
            q=sum((Decimal(r['flow_quantity']) for r in records),Decimal(0));c=sum((Decimal(r['flow_capital_rub']) for r in records),Decimal(0))
            require(q==Decimal(row['quantity']) and (q>0 and c>0 or q==0 and c==0),'dated_transit_quantity_changed')
            row.update(capital_rub=native._text(c),wac_rub=native._text(c/q) if q else None)
    return result,{'kind':'native_dated_ff_prefix','date':day,'opening_version':opening_version,'prefix_digest':fingerprint([ledger,lines]),'outputs':evidence},ready.pin_queries(conn,queries)
