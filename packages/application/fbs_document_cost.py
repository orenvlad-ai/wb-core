"""Document money and transfer references for the isolated FBS cost candidate.

This module never reads a ledger. FBS prices follow the fixed daily mass;
the auxiliary FBO book and in-transit references start at the accepted baseline
and subsequently advance from immutable documents only.
"""
from copy import deepcopy
from datetime import date
from decimal import Decimal, localcontext
import hashlib
import json

ZERO = Decimal(0)
DOCUMENT_POLICY = "snapshot_period_document_cost_v1"


def digest(value):
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                                separators=(",", ":")).encode()).hexdigest()


def number(value, *, signed=False):
    result = Decimal(str(value))
    if not result.is_finite() or (result < 0 and not signed):
        raise ValueError("invalid_document_cost_number")
    return result


def units(value):
    result = number(value)
    if result != result.to_integral_value():
        raise ValueError("fractional_document_quantity")
    return int(result)


def cents(value):
    result = number(value) * 100
    if result != result.to_integral_value():
        raise ValueError("document_expense_requires_kopecks")
    return int(result)


def text(value):
    return None if value is None else format(value, "f")


def key(facility, pool, nm):
    if not facility or ":" in facility or pool not in {"FBS", "FBO"} or type(nm) is not int or nm <= 0:
        raise ValueError("invalid_document_location")
    return f"{facility}:{pool}:{nm}"


def location(row):
    return key(str(row["facility_id"]), str(row["pool"]), int(row["nm_id"]))


def allocate(total, weights):
    weights = {k: units(q) for k, q in weights.items() if units(q) > 0}
    denominator = sum(weights.values())
    if total and not denominator:
        raise ValueError("expense_allocation_without_new_quantity_basis")
    if not total:
        return {k: 0 for k in weights}
    values = {k: total * q // denominator for k, q in weights.items()}
    order = sorted(weights, key=lambda k: (-(total * weights[k] % denominator), str(k)))
    for k in order[:total - sum(values.values())]:
        values[k] += 1
    return values


def share(total, shipped, prior, quantity):
    if shipped <= 0 or prior < 0 or quantity < 0 or prior + quantity > shipped:
        raise ValueError("transfer_outcome_exceeds_new_reference")
    return total * (prior + quantity) // shipped - total * prior // shipped


def source(doc):
    return {"document_id": doc["document_id"], "document_fingerprint": doc["fingerprint"],
            "value_policy": DOCUMENT_POLICY}


def raw(doc):
    return doc.get("cost_document", {})


def ordered(documents):
    return sorted(documents, key=lambda d: (d["posted_at"], d["document_id"]))


def context(doc, documents):
    root = documents.get(doc["root_document_id"])
    domain = raw(root or {}).get("domain", {})
    if not domain.get("source") or not domain.get("destination"):
        domain = raw(doc).get("domain", {})
    if not domain.get("source") or not domain.get("destination"):
        raise ValueError("transfer_root_location_missing")
    return domain["source"], domain["destination"]


def ship_id(doc, nm):
    return f"{doc['root_document_id']}:{nm}"


def china_discrepancy_companion(doc, documents):
    data=raw(doc)
    if data.get('document_role') != 'china_discrepancy':
        return False
    parent=documents.get(doc['root_document_id'])
    relations=data.get('relations',[])
    if (doc['kind']!='transfer_discrepancy' or not parent or parent['kind']!='china_acceptance'
            or len(relations)!=1 or relations[0].get('relation_type')!='discrepancy_of'
            or relations[0].get('parent_document_id')!=parent['document_id']
            or relations[0].get('child_document_id')!=doc['document_id']
            or relations[0].get('root_document_id')!=parent['document_id']
            or data.get('movements') or not data.get('lines')):
        raise ValueError('china_discrepancy_companion_identity_invalid')
    for line in data['lines']:
        if (not line.get('line_role') or line.get('pool') is not None
                or not line.get('facility_id') or units(line['quantity'])<=0
                or number(line['capital_rub'])!=0 or number(line['expense_rub'])!=0):
            raise ValueError('china_discrepancy_companion_effect_invalid')
    return True


def initial_document_state(capture):
    """Accept existing FBO and open transit values once, without price replay."""
    book = {}
    for row in capture["baseline_costs"].get("fbo_rows", []):
        k = key(row["facility_id"], "FBO", row["nm_id"])
        if k in book:
            raise ValueError("duplicate_initial_fbo_basis")
        q, capital = units(row["quantity"]), number(row["capital_rub"])
        wac = None if row.get("wac_rub") is None else number(row["wac_rub"])
        if q > 0 and (wac is None or not row.get("source")):
            raise ValueError("initial_fbo_cost_missing")
        book[k] = {"facility_id": row["facility_id"], "nm_id": row["nm_id"],
                   "quantity": str(q), "capital_rub": text(capital), "wac_rub": text(wac),
                   "source": deepcopy(row.get("source", {}))}
    documents = {d["document_id"]: d for d in capture["documents"]}
    reversed_ids = {raw(d).get("domain", {}).get("target_document_id")
                    for d in documents.values() if d["kind"] == "storno"}
    transfers = {}
    # Shipment children may share the same second with their outcomes. Build
    # every source reference first, then apply the immutable terminal order.
    initial_order = sorted(ordered(documents.values()), key=lambda d: d["kind"] != "transfer_shipment")
    for doc in initial_order:
        if not raw(doc) or doc["document_id"] in reversed_ids or china_discrepancy_companion(doc,documents):
            continue
        if doc["kind"] == "transfer_shipment":
            src, dst = context(doc, documents)
            for line in raw(doc)["lines"]:
                if line["line_role"] != "shipped":
                    continue
                nm, q = int(line["nm_id"]), units(line["quantity"])
                identity = ship_id(doc, nm)
                if identity in transfers or q <= 0:
                    raise ValueError("ambiguous_initial_transfer")
                transfers[identity] = {
                    "shipment_document_id": doc["document_id"], "shipment_fingerprint": doc["fingerprint"],
                    "source_key": key(src["facility_id"], src["pool"], nm),
                    "destination_key": key(dst["facility_id"], dst["pool"], nm),
                    "quantity": q, "base_capital_rub": text(number(line["capital_rub"])),
                    "expense_components": {doc["document_id"]: cents(line["expense_rub"])},
                    "outcomes": [], "terminal_quantity": 0,
                    "cost_reference": {**source(doc), "policy": "accepted_initial_transit_value", "line_no": line["line_no"]},
                }
        elif doc["kind"] in {"transfer_receipt", "transfer_loss", "transfer_cancellation", "transfer_discrepancy"}:
            for line in raw(doc)["lines"]:
                if line["line_role"] not in {"received", "lost", "cancelled", "expected_not_sent"}:
                    continue
                transfer = transfers.get(ship_id(doc, int(line["nm_id"])))
                if transfer is None:
                    raise ValueError("initial_terminal_without_shipment")
                q = units(line["quantity"])
                share(0, transfer["quantity"], transfer["terminal_quantity"], q)
                transfer["outcomes"].append({"document_id": doc["document_id"], "role": line["line_role"],
                    "quantity": q, "prior_quantity": transfer["terminal_quantity"],
                    "destination_key": location(line)})
                transfer["terminal_quantity"] += q
        elif doc["kind"] == "late_expense":
            for line in raw(doc)["lines"]:
                transfer = transfers.get(ship_id(doc, int(line["nm_id"])))
                if transfer is None:
                    raise ValueError("initial_expense_without_shipment")
                transfer["expense_components"][doc["document_id"]] = cents(line["expense_rub"])
    return {"policy": DOCUMENT_POLICY, "fbo_rows": book, "transfers": transfers}


def correction_operands(data):
    """Validate signed documentary lines against their exact physical movements."""
    lines, movements = data['lines'], data['movements']
    if not lines or len(lines) != len(movements):
        raise ValueError('correction_operands_incomplete')
    result=[]
    for line, movement in zip(lines, movements):
        metadata=json.loads(line.get('metadata_json','{}')) if 'metadata_json' in line else line.get('metadata',{})
        q=number(metadata.get('signed_quantity_delta'),signed=True)
        value=number(metadata.get('signed_capital_rub'),signed=True)
        if (q != q.to_integral_value() or line.get('line_no')!=movement.get('line_no') or location(line)!=location(movement)
                or q!=number(movement['quantity_delta'],signed=True)
                or value!=number(movement['capital_delta_rub'],signed=True)
                or units(line['quantity'])!=abs(q) or number(line['capital_rub'])!=abs(value)
                or line['line_role']!='correction' or (q==0 and value==0)):
            raise ValueError('correction_signed_operand_mismatch')
        result.append((line,int(q),value))
    return result


def management_cost_acceptance(evidence, movements, *, business_date):
    """Bound the optional forward management decision to its exact correction.

    This is an aggregate supplier-component allocation, never certification of
    complete expenses or literal supplier cost per SKU. No historical rewrite
    or WB valuation authority is conferred by this evidence.
    """
    if not isinstance(evidence, dict):
        raise ValueError('management_cost_acceptance_invalid')
    cutoff = evidence.get('cutoff_date')
    if date.fromisoformat(str(cutoff)).isoformat() != cutoff or cutoff != business_date:
        raise ValueError('management_cost_acceptance_cutoff_mismatch')
    scope = evidence.get('supplier_scope_nm_ids')
    affected = evidence.get('affected_current_fbo')
    refs = evidence.get('source_refs')
    if (evidence.get('contract') != 'management_cost_acceptance_v1'
            or not isinstance(scope, list) or len(scope) != 34
            or any(type(nm) is not int or nm <= 0 for nm in scope) or len(set(scope)) != 34
            or not isinstance(affected, list) or len(affected) != 4
            or any(not isinstance(row, dict) for row in affected)
            or not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or not ref.strip() for ref in refs)
            or not isinstance(evidence.get('business_decision'), str) or not evidence['business_decision'].strip()):
        raise ValueError('management_cost_acceptance_scope_invalid')
    def effects(rows):
        result = {}
        for row in rows:
            nm = row.get('nm_id')
            value = number(row['capital_delta_rub'], signed=True)
            if (type(nm) is not int or nm not in scope or row.get('pool') != 'FBO'
                    or number(row['quantity_delta'], signed=True) != 0 or value == 0
                    or value * 100 != (value * 100).to_integral_value()):
                raise ValueError('management_cost_acceptance_effect_invalid')
            k = location(row)
            if k in result:
                raise ValueError('management_cost_acceptance_effect_duplicate')
            result[k] = value
        return result
    actual, declared = effects(movements), effects(affected)
    amount = number(evidence.get('total_supplier_component_delta_rub'), signed=True)
    if (actual != declared or len(actual) != 4 or len({row['nm_id'] for row in affected}) != 4
            or amount == 0 or sum(actual.values(), ZERO) != amount):
        raise ValueError('management_cost_acceptance_total_or_effect_mismatch')
    return {**{k: deepcopy(evidence[k]) for k in ('contract', 'cutoff_date',
                'total_supplier_component_delta_rub', 'affected_current_fbo', 'source_refs', 'business_decision')},
            'supplier_scope_nm_ids': sorted(scope),
            'sku_cost_accuracy': 'aggregate_allocation', 'prior_profit_history': 'unchanged',
            'fbs_quantity_basis': 'available_only'}


def _solve(nodes, links):
    """Solve the small same-SKU daily transfer system, including round trips."""
    prices = {}
    if any(n['mass']<0 or n['value']<0 for n in nodes.values()):
        raise ValueError('negative_document_cost_basis')
    if any(n["mass"] == 0 and n["value"] != 0 for n in nodes.values()):
        raise ValueError("expense_without_cost_mass")
    for nm in sorted({node["nm_id"] for node in nodes.values()}):
        keys = sorted(k for k, node in nodes.items() if node["nm_id"] == nm)
        unknown = {k for k in keys if nodes[k]["opening_wac"] is None
                   and (nodes[k]["opening_q"] > 0 or nodes[k]["mass"] == 0)}
        while True:
            expanded = unknown | {dst for dst, src, _q in links if src in unknown}
            if expanded == unknown:
                break
            unknown = expanded
        for k in unknown:
            prices[k] = None
        known = [k for k in keys if k not in unknown]
        matrix = []
        for k in known:
            node = nodes[k]
            coefficients = {j: ZERO for j in known}
            if node["mass"] == 0:
                prices[k] = node["opening_wac"]
                coefficients[k] = Decimal(1)
                value = node["opening_wac"] or ZERO
                if node["value"]:
                    raise ValueError("expense_without_cost_mass")
            else:
                coefficients[k] = Decimal(node["mass"])
                value = node["value"]
                for dst, src, q in links:
                    if dst == k and src in coefficients:
                        coefficients[src] -= q
            matrix.append([coefficients[j] for j in known] + [value])
        for col in range(len(known)):
            pivot = next((i for i in range(col, len(known)) if matrix[i][col] != 0), None)
            if pivot is None:
                raise ValueError("transfer_cycle_without_independent_cost_anchor")
            matrix[col], matrix[pivot] = matrix[pivot], matrix[col]
            divisor = matrix[col][col]
            matrix[col] = [v / divisor for v in matrix[col]]
            for i in range(len(known)):
                if i != col:
                    factor = matrix[i][col]
                    matrix[i] = [a - factor * b for a, b in zip(matrix[i], matrix[col])]
        for i, k in enumerate(known):
            if nodes[k]["mass"] == 0 and nodes[k]["opening_wac"] is None:
                prices[k] = None
            else:
                prices[k] = number(matrix[i][-1])
    return prices


def resolve_document_costs(state, capture, opening, absorbed, *, historical_fbo_outgoings=()):
    """Price native documents and optional unconnected historical FBO drafts.

    The explicit keyword is a pure planning seam, not document admission. Its
    output carries no native posting or publication authority. Ordinary callers
    keep their existing contract and document-kind allowlist.
    """
    with localcontext() as decimal_context:
        decimal_context.prec = 50
        return _resolve(state, capture, opening, absorbed, historical_fbo_outgoings)


def _resolve(state, capture, opening, absorbed, historical_fbo_outgoings=()):
    day = capture["business_date"]
    prior = state["baseline"]["document_cost_state"]
    for _date, period in sorted(state["periods"].items()):
        if period["status"] == "closed":
            prior = period["document_cost_state"]
    auxiliary = deepcopy(prior)
    transfers = auxiliary["transfers"]
    documents = {d["document_id"]: deepcopy(d) for d in capture["documents"]}
    current = {i: d for i, d in documents.items() if i not in absorbed and d["business_date"] == day}
    nodes, effects, links, bindings, overheads = {}, {}, [], [], []
    historical_fbo_outgoings = list(historical_fbo_outgoings)
    native_fbo_costs = {}

    def node(k):
        if k not in nodes:
            facility, pool, nm = k.split(":")
            nodes[k] = {"facility_id": facility, "pool": pool, "nm_id": int(nm),
                        "opening_q": 0, "opening_wac": None, "mass": 0,
                        "outgoing": 0, "value": ZERO}
        return nodes[k]

    for row in opening.values():
        k = key(row["facility_id"], "FBS", row["nm_id"])
        q, wac = units(row["quantity"]), None if row["wac_rub"] is None else number(row["wac_rub"])
        node(k).update(opening_q=q, opening_wac=wac, mass=q, value=q * (wac or ZERO))
    for row in capture["quantity_snapshot"]["rows"]:
        node(key(row["facility_id"], "FBS", row["nm_id"]))
    for k, row in prior["fbo_rows"].items():
        q, wac = units(row["quantity"]), None if row["wac_rub"] is None else number(row["wac_rub"])
        node(k).update(opening_q=q, opening_wac=wac, mass=q, value=number(row["capital_rub"]))
        if (row.get('source') or {}).get('current_fbo_boundary'):
            node(k)['current_fbo_boundary']=deepcopy(row['source']['current_fbo_boundary'])

    def effect(doc, k, kind, q=0, value=ZERO, reference=None):
        n = node(k)
        effects.setdefault(doc["document_id"], []).append({"key": k, "kind": kind, "quantity": q,
            "capital_rub": text(value), "source": {**source(doc), **(reference or {})}})
        if kind == "receipt":
            n["mass"] += q
            with localcontext() as context:
                context.prec=160 if n.get('current_fbo_boundary') else context.prec
                n["value"] += value
        elif kind == "expense":
            with localcontext() as context:
                context.prec=160 if n.get('current_fbo_boundary') else context.prec
                n["value"] += value
        elif kind == "outgoing":
            n["outgoing"] += q
        elif kind == 'adjustment':
            n['mass']+=q
            with localcontext() as context:
                context.prec=160 if n.get('current_fbo_boundary') else context.prec
                n['value']+=value

    # Same-open-day reversals remove their source facts before any allocation.
    cancelled = set()
    for doc in current.values():
        if doc["kind"] == "storno" and raw(doc):
            target = raw(doc)["domain"].get("target_document_id")
            if target not in current or current[target]["kind"] == "storno":
                raise ValueError("closed_document_reversal_requires_separate_adjustment")
            cancelled.update({doc["document_id"], target})
    active = [d for i, d in current.items() if i not in cancelled]
    from packages.application.current_fbo_supply_cost_source import apply_opening_dispatches
    current_fbo_bridges, current_fbo_boundaries = apply_opening_dispatches(nodes, active, node, key)
    companions={d['document_id'] for d in active if china_discrepancy_companion(d,documents)}
    for identity in cancelled:
        effects[identity] = []

    for doc in active:
        data, kind = raw(doc), doc["kind"]
        if not data:
            for event in doc["events"]:
                if event["kind"] in {"receipt", "expense", "outgoing"}:
                    effect(doc, key(event["facility_id"], "FBS", event["nm_id"]),
                           event["kind"], units(event["quantity"]), number(event["capital_rub"], signed=event["kind"] == "expense"))
            continue
        effects[doc["document_id"]] = []
        if kind == 'current_fbo_dispatch':
            continue  # exact owned current boundary was consumed before daily mass
        if kind == 'fbo_lifecycle_dispatch':
            from packages.application.fbo_dispatch_cost_source import apply_dispatch
            apply_dispatch(doc, effect=effect, node=node, key=key)
            continue
        if doc['document_id'] in companions:
            continue
        if kind == "china_acceptance":
            for line in data["lines"]:
                if line["line_role"] != "accepted_pool_allocation":
                    raise ValueError("unsupported_receipt_line_role")
                effect(doc, location(line), "receipt", units(line["quantity"]),
                       number(line["capital_rub"]) + number(line["expense_rub"]))
        elif kind == "pool_overhead":
            domain = data["domain"]
            total = sum(cents(e["amount_rub"]) for e in data["expense_lines"])
            if not total or total != cents(domain["amount_rub"]) or domain["scope"] not in {"FBS", "FBO", "both"}:
                raise ValueError("overhead_source_header_mismatch")
            overheads.append((doc, domain["facility_id"], domain["scope"], total))
        elif kind == "transfer_shipment":
            src, dst = context(doc, documents)
            weights = {int(line["nm_id"]): units(line["quantity"]) for line in data["lines"] if line["line_role"] == "shipped"}
            allocations = allocate(sum(cents(e["amount_rub"]) for e in data["expense_lines"]), weights)
            for line in data["lines"]:
                nm, q = int(line["nm_id"]), units(line["quantity"])
                identity = ship_id(doc, nm)
                if line["line_role"] != "shipped" or q <= 0 or identity in transfers:
                    raise ValueError("ambiguous_new_shipment")
                src_key, dst_key = key(src["facility_id"], src["pool"], nm), key(dst["facility_id"], dst["pool"], nm)
                effect(doc, src_key, "outgoing", q)
                node(dst_key)
                transfers[identity] = {"shipment_document_id": doc["document_id"], "shipment_fingerprint": doc["fingerprint"],
                    "source_key": src_key, "destination_key": dst_key, "quantity": q,
                    "base_capital_rub": None, "expense_components": {doc["document_id"]: allocations.get(nm, 0)},
                    "outcomes": [], "terminal_quantity": 0, "new_source_key": src_key,
                    "cost_reference": {**source(doc), "business_date": day, "source_key": src_key}}
        elif kind == "historical_wb_dispatch":
            if data.get("contract") != "historical_fbo_native_cost_source_v1" or not doc.get("historical_fbo_native"):
                raise ValueError("historical_fbo_native_cost_source_required")
            domain = data["domain"]
            outgoing = {"supply_id": domain["supply_id"], "source_key": domain["source_key"],
                "business_date": day, "facility_id": domain["facility_id"], "source_digest": doc["fingerprint"],
                "items": [{"nm_id": line["nm_id"], "quantity": units(line["quantity"])} for line in data["lines"]]}
            if data["expense_lines"] or len(data["movements"]) != len(outgoing["items"]):
                raise ValueError("historical_fbo_native_cost_operands_invalid")
            for movement in data["movements"]:
                native_fbo_costs[(domain["source_key"], movement["nm_id"])] = movement
            historical_fbo_outgoings.append(outgoing)
        elif kind == "pool_reallocation":
            domain = data["domain"]
            weights = {int(line["nm_id"]): units(line["quantity"]) for line in data["lines"]}
            allocations = allocate(sum(cents(e["amount_rub"]) for e in data["expense_lines"]), weights)
            for line in data["lines"]:
                nm, q = int(line["nm_id"]), units(line["quantity"])
                src = key(domain["facility_id"], domain["source_pool"], nm)
                dst = key(domain["facility_id"], domain["destination_pool"], nm)
                if src == dst or q <= 0:
                    raise ValueError("invalid_reallocation")
                effect(doc, src, "outgoing", q)
                effect(doc, dst, "receipt", q, Decimal(allocations.get(nm, 0)) / 100)
                links.append((dst, src, q))
                bindings.append({"document_id": doc["document_id"], "destination_key": dst,
                    "source_key": src, "quantity": q, "effect_index": len(effects[doc["document_id"]]) - 1,
                    "reference": {**source(doc), "business_date": day, "source_key": src}})
        elif kind in {"transfer_root", "transfer_receipt", "transfer_loss", "transfer_cancellation", "transfer_discrepancy", "late_expense"}:
            pass
        elif kind == 'pool_inventory':
            if data['movements'] or any(l['line_role']!='absolute_target' or number(l['capital_rub'])!=0 or number(l['expense_rub'])!=0 for l in data['lines']):
                raise ValueError('inventory_parent_has_direct_cost_effect')
        elif kind in {'inventory_surplus','inventory_shortage'}:
            expected_role=kind
            for line in data['lines']:
                q=units(line['quantity']);value=number(line['capital_rub'])
                if line['line_role']!=expected_role or q<=0 or (kind=='inventory_surplus' and value<=0):
                    raise ValueError('inventory_child_cost_operand_invalid')
                effect(doc,location(line),'receipt' if kind=='inventory_surplus' else 'outgoing',q,value if kind=='inventory_surplus' else ZERO,
                    {'policy':'immutable_positive_inventory_basis' if kind=='inventory_surplus' else 'daily_wac_inventory_outgoing'})
        elif kind=='correction':
            for line,q,value in correction_operands(data):
                effect(doc,location(line),'adjustment',q,value,{'policy':'explicit_signed_document_adjustment'})
            if 'management_cost_acceptance' in data.get('domain', {}):
                accepted = management_cost_acceptance(data['domain']['management_cost_acceptance'],
                    data['movements'], business_date=doc['business_date'])
                if (data.get('contract') != 'ff_pool_posted_cost_document_v1'
                        or not data.get('posted_manifest_sha256')):
                    raise ValueError('management_cost_acceptance_posted_evidence_required')
                auxiliary.setdefault('management_cost_acceptances', {})[doc['document_id']] = {
                    **accepted, **source(doc), 'posted_manifest_sha256': data['posted_manifest_sha256']}
        else:
            # Preserve explicit unsupported FBS handling. Never silently skip
            # an auxiliary FBO mutation that could later feed a FBS transfer.
            if any(m["pool"] == "FBO" for m in data["movements"]):
                raise ValueError("unsupported_auxiliary_fbo_document:" + kind)
            effects.pop(doc["document_id"], None)

    # Outcomes consume the saved shipment reference, not today's source price.
    for doc in ordered(active):
        data, kind = raw(doc), doc["kind"]
        if doc['document_id'] in companions or not data or kind not in {"transfer_receipt", "transfer_loss", "transfer_cancellation", "transfer_discrepancy", "late_expense"}:
            continue
        if kind == "late_expense":
            selected = {identity: tr for identity, tr in transfers.items()
                        if identity.startswith(str(doc["root_document_id"]) + ":")}
            if not selected:
                raise ValueError("late_expense_without_saved_shipment")
            allocations = allocate(sum(cents(e["amount_rub"]) for e in data["expense_lines"]),
                                   {identity: tr["quantity"] for identity, tr in selected.items()})
            for identity, tr in selected.items():
                amount = allocations.get(identity, 0)
                for outcome in tr["outcomes"]:
                    if outcome["role"] == "received":
                        value = Decimal(share(amount, tr["quantity"], outcome["prior_quantity"], outcome["quantity"])) / 100
                        effect(doc, outcome["destination_key"], "expense", value=value,
                               reference={"shipment_cost_reference": tr["cost_reference"]})
                tr["expense_components"][doc["document_id"]] = amount
            continue
        displaced_expense = 0
        for line in data["lines"]:
            nm, q, role = int(line["nm_id"]), units(line["quantity"]), line["line_role"]
            allowed = {"transfer_receipt": {"received"}, "transfer_loss": {"lost"},
                       "transfer_cancellation": {"cancelled"}, "transfer_discrepancy": {"expected_not_sent", "unexpected"}}
            if role not in allowed[kind] or q <= 0:
                raise ValueError("transfer_outcome_role_mismatch")
            if role == "unexpected":
                continue
            tr = transfers.get(ship_id(doc, nm))
            if tr is None:
                raise ValueError("receipt_without_saved_shipment_cost")
            previous_q = tr["terminal_quantity"]
            share(0, tr["quantity"], previous_q, q)
            dst = tr["source_key"] if role in {"cancelled", "expected_not_sent"} else tr["destination_key"]
            if location(line) != dst and role != "lost":
                raise ValueError("receipt_location_differs_from_shipment")
            expense = sum(share(v, tr["quantity"], previous_q, q) for v in tr["expense_components"].values()) if role == "received" else 0
            if role == "expected_not_sent":
                displaced_expense += sum(share(v, tr["quantity"], previous_q, q) for v in tr["expense_components"].values())
            if role in {"received", "cancelled", "expected_not_sent"}:
                value = Decimal(expense) / 100
                if "new_source_key" in tr:
                    effect(doc, dst, "receipt", q, value)
                    links.append((dst, tr["source_key"], q))
                    bindings.append({"document_id": doc["document_id"], "destination_key": dst,
                        "source_key": tr["source_key"], "quantity": q,
                        "effect_index": len(effects[doc["document_id"]]) - 1, "reference": tr["cost_reference"]})
                else:
                    if tr["base_capital_rub"] is None:
                        raise ValueError("saved_transfer_price_unavailable")
                    if tr["cost_reference"].get("policy") == "accepted_initial_transit_value":
                        # Pre-baseline terminal documents already consumed
                        # cumulative kopeck shares. Carry their exact remainder.
                        value += Decimal(share(cents(tr["base_capital_rub"]), tr["quantity"], previous_q, q)) / 100
                    else:
                        value += number(tr["base_capital_rub"]) * q / tr["quantity"]
                    effect(doc, dst, "receipt", q, value, {"shipment_cost_reference": tr["cost_reference"]})
            elif role != "lost":
                raise ValueError("unsupported_transfer_outcome")
            tr["outcomes"].append({"document_id": doc["document_id"], "role": role, "quantity": q,
                                   "prior_quantity": previous_q, "destination_key": dst})
            tr["terminal_quantity"] += q
        if kind == "transfer_discrepancy":
            src, dst = context(doc, documents)
            unexpected = [l for l in data["lines"] if l["line_role"] == "unexpected"]
            amounts = allocate(displaced_expense, {int(l["nm_id"]): units(l["quantity"]) for l in unexpected}) if unexpected else {}
            for line in unexpected:
                nm, q = int(line["nm_id"]), units(line["quantity"])
                src_key, dst_key = key(src["facility_id"], src["pool"], nm), key(dst["facility_id"], dst["pool"], nm)
                if location(line) != dst_key:
                    raise ValueError("unexpected_receipt_location_mismatch")
                effect(doc, src_key, "outgoing", q)
                effect(doc, dst_key, "receipt", q, Decimal(amounts.get(nm, 0)) / 100)
                links.append((dst_key, src_key, q))
                bindings.append({"document_id": doc["document_id"], "destination_key": dst_key,
                    "source_key": src_key, "quantity": q, "effect_index": len(effects[doc["document_id"]]) - 1,
                    "reference": {**source(doc), "business_date": day, "source_key": src_key}})

    allocations_evidence = []
    for doc, facility, scope, total in overheads:
        boundary = current_fbo_boundaries.get(facility)
        if boundary and scope == 'FBO' and doc['posted_at'] >= boundary['posted_at']:
            # The actual posted document owns the remaining-stock allocation.
            # Later same-day receipts must not redistribute an earlier fee.
            amounts={};weights={}
            for line in raw(doc)['lines']:
                k=location(line)
                if line['facility_id'] != facility or line['pool'] != 'FBO' or line['line_role'] != 'overhead_allocation' or k in amounts:
                    raise ValueError('current_fbo_overhead_allocation_invalid')
                amount=cents(line['expense_rub'])
                if amount!=cents(line['capital_rub']) or units(line['quantity'])<=0:
                    raise ValueError('current_fbo_overhead_allocation_invalid')
                amounts[k]=amount;weights[k]=units(line['quantity'])
            if sum(amounts.values())!=total:
                raise ValueError('current_fbo_overhead_allocation_total')
            basis=dict(document_id=doc['document_id'],document_fingerprint=doc['fingerprint'],business_date=day,
                policy='native_posted_current_fbo_allocation_v1',weights=weights,amounts_kopecks=amounts,total_kopecks=total,
                dispatch_boundary=boundary)
            basis['id']=digest(basis);allocations_evidence.append(basis)
            for k,amount in amounts.items():
                if amount:
                    node(k)['current_fbo_boundary']=dict(boundary,policy='native_posted_current_fbo_allocation_v1')
                    effect(doc,k,'expense',value=Decimal(amount)/100,reference={'allocation_basis_id':basis['id']})
            continue
        pools = {"FBS", "FBO"} if scope == "both" else {scope}
        weights = {k: n["mass"] + n.get('frozen_dispatch_quantity', 0) for k, n in nodes.items()
                   if n["facility_id"] == facility and n["pool"] in pools
                   and n["mass"] + n.get('frozen_dispatch_quantity', 0) > 0}
        pool_weights = {pool: sum(q for k, q in weights.items() if nodes[k]["pool"] == pool) for pool in pools}
        pool_amounts = allocate(total, pool_weights)
        amounts = {}
        for pool, amount in pool_amounts.items():
            amounts.update(allocate(amount, {k: q for k, q in weights.items() if nodes[k]["pool"] == pool}))
        basis = {"document_id": doc["document_id"], "document_fingerprint": doc["fingerprint"],
                 "business_date": day, "policy": "opening_plus_period_receipts", "weights": weights,
                 "pool_amounts_kopecks": pool_amounts, "amounts_kopecks": amounts, "total_kopecks": total}
        basis["id"] = digest(basis)
        allocations_evidence.append(basis)
        for k, amount in amounts.items():
            if amount:
                effect(doc, k, "expense", value=Decimal(amount) / 100,
                       reference={"allocation_basis_id": basis["id"]})

    # Outgoing stock does not enter the daily cost mass. These typed operands
    # deliberately cannot masquerade as a receipt, correction or native row.
    outgoing_sources = set()
    for outgoing in historical_fbo_outgoings:
        supply = outgoing.get("supply_id")
        if (type(supply) is not str or not supply.isdecimal() or int(supply) <= 0
                or outgoing.get("source_key") != "wb_supply_debit:supply:" + supply
                or outgoing.get("business_date") != day
                or not outgoing.get("source_digest")
                or supply in outgoing_sources or not outgoing.get("items")):
            raise ValueError("historical_fbo_outgoing_identity_invalid")
        outgoing_sources.add(supply)
        seen = set()
        for line in outgoing["items"]:
            nm, q = line["nm_id"], units(line["quantity"])
            if type(nm) is not int or nm <= 0 or nm in seen or q <= 0:
                raise ValueError("historical_fbo_outgoing_line_invalid")
            seen.add(nm)
            node(key(outgoing["facility_id"], "FBO", nm))["outgoing"] += q
    prices = _solve(nodes, links)
    basis = {"date": day, "opening": opening, "auxiliary_opening": prior,
             "documents": {i: d["fingerprint"] for i, d in current.items()}, "allocations": allocations_evidence}
    if current_fbo_bridges:
        basis['current_fbo_dispatch_bridges']=current_fbo_bridges
    if historical_fbo_outgoings:
        basis["historical_fbo_outgoings"] = list(historical_fbo_outgoings)
    basis_id = digest(basis)
    outgoing_values = []
    for outgoing in historical_fbo_outgoings:
        for line in outgoing["items"]:
            k = key(outgoing["facility_id"], "FBO", line["nm_id"])
            wac = prices[k]
            if wac is None:
                raise ValueError("historical_fbo_outgoing_cost_missing")
            outgoing_values.append({"source_key": outgoing["source_key"], "supply_id": outgoing["supply_id"],
                "source_digest": outgoing["source_digest"], "business_date": day,
                "facility_id": outgoing["facility_id"], "pool": "FBO", "nm_id": line["nm_id"],
                "quantity_delta": -units(line["quantity"]), "capital_delta_rub": text(-wac * units(line["quantity"])),
                "wac_rub": text(wac), "valuation_basis_id": basis_id})
            native = native_fbo_costs.get((outgoing["source_key"], line["nm_id"]))
            if native is not None and (number(native["capital_delta_rub"], signed=True) != -wac * units(line["quantity"])
                    or native["quantity_delta"] != -units(line["quantity"])):
                raise ValueError("historical_fbo_native_frozen_cost_drift")
    for binding in bindings:
        event = effects[binding["document_id"]][binding["effect_index"]]
        wac = prices[binding["source_key"]]
        reference = {**binding["reference"], "valuation_basis_id": basis_id, "wac_rub": text(wac)}
        event["source"]["shipment_cost_reference"] = reference
        if wac is None:
            event.update(kind="unsupported", capital_rub="0", reason="new_transfer_source_cost_missing")
        else:
            event["capital_rub"] = text(number(event["capital_rub"]) + wac * binding["quantity"])
    for tr in transfers.values():
        if "new_source_key" in tr:
            wac = prices[tr.pop("new_source_key")]
            tr["base_capital_rub"] = None if wac is None else text(wac * tr["quantity"])
            tr["cost_reference"].update(valuation_basis_id=basis_id, wac_rub=text(wac))
            if wac is None:
                raise ValueError("new_shipment_source_cost_missing")
    auxiliary["fbo_rows"] = {}
    for k, n in nodes.items():
        if n["pool"] != "FBO":
            continue
        q = n["mass"] - n["outgoing"]
        if q < 0:
            raise ValueError("negative_independent_fbo_quantity")
        wac = prices[k]
        if q and wac is None:
            raise ValueError("independent_fbo_cost_missing")
        # Preserve authoritative capital when no outgoing/link valuation is
        # needed, including on the next quiet day after a current boundary.
        exact_carry = bool(n.get('current_fbo_boundary')) and not n['outgoing'] and not any(dst==k for dst,_src,_q in links)
        legacy_capital = q*wac if wac is not None else ZERO
        capital = n['value'] if exact_carry and n['value'] != legacy_capital else legacy_capital
        auxiliary["fbo_rows"][k] = {"facility_id": n["facility_id"], "nm_id": n["nm_id"],
            "quantity": str(q), "wac_rub": text(wac), "capital_rub": text(q * wac) if wac is not None else "0",
            "source": {"policy": DOCUMENT_POLICY, "business_date": day, "valuation_basis_id": basis_id}}
        auxiliary['fbo_rows'][k]['capital_rub']=text(capital)
        if n.get('current_fbo_boundary'):
            auxiliary['fbo_rows'][k]['source']['current_fbo_boundary']=deepcopy(n['current_fbo_boundary'])
    for identity, items in effects.items():
        if not raw(documents[identity]):
            continue  # existing normalized fixtures keep their own validation
        events = []
        for item in items:
            n = nodes[item["key"]]
            if n["pool"] == "FBS":
                events.append({k: v for k, v in item.items() if k != "key"} | {
                    "facility_id": n["facility_id"], "nm_id": n["nm_id"]})
        documents[identity]["events"] = events
    valuation = {"id": basis_id, "policy": DOCUMENT_POLICY, "allocations": allocations_evidence}
    if current_fbo_bridges:
        valuation['current_fbo_dispatch_bridges']=current_fbo_bridges
    if historical_fbo_outgoings:
        valuation["historical_fbo_outgoings"] = outgoing_values
    return documents, auxiliary, valuation
