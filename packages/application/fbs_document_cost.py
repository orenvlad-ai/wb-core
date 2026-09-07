"""Document money and transfer references for the isolated FBS cost candidate.

This module never reads a ledger. FBS prices follow the fixed daily mass;
the auxiliary FBO book and in-transit references start at the accepted baseline
and subsequently advance from immutable documents only.
"""
from copy import deepcopy
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
        if not raw(doc) or doc["document_id"] in reversed_ids:
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


def _solve(nodes, links):
    """Solve the small same-SKU daily transfer system, including round trips."""
    prices = {}
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


def resolve_document_costs(state, capture, opening, absorbed):
    with localcontext() as decimal_context:
        decimal_context.prec = 50
        return _resolve(state, capture, opening, absorbed)


def _resolve(state, capture, opening, absorbed):
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

    def effect(doc, k, kind, q=0, value=ZERO, reference=None):
        n = node(k)
        effects.setdefault(doc["document_id"], []).append({"key": k, "kind": kind, "quantity": q,
            "capital_rub": text(value), "source": {**source(doc), **(reference or {})}})
        if kind == "receipt":
            n["mass"] += q
            n["value"] += value
        elif kind == "expense":
            n["value"] += value
        elif kind == "outgoing":
            n["outgoing"] += q

    # Same-open-day reversals remove their source facts before any allocation.
    cancelled = set()
    for doc in current.values():
        if doc["kind"] == "storno" and raw(doc):
            target = raw(doc)["domain"].get("target_document_id")
            if target not in current or current[target]["kind"] == "storno":
                raise ValueError("closed_document_reversal_requires_separate_adjustment")
            cancelled.update({doc["document_id"], target})
    active = [d for i, d in current.items() if i not in cancelled]
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
        elif kind == "pool_inventory" and not data["movements"] and all(units(l["quantity"]) == 0 for l in data["lines"]):
            pass
        else:
            # Preserve explicit unsupported FBS handling. Never silently skip
            # an auxiliary FBO mutation that could later feed a FBS transfer.
            if any(m["pool"] == "FBO" for m in data["movements"]):
                raise ValueError("unsupported_auxiliary_fbo_document:" + kind)
            effects.pop(doc["document_id"], None)

    # Outcomes consume the saved shipment reference, not today's source price.
    for doc in ordered(active):
        data, kind = raw(doc), doc["kind"]
        if not data or kind not in {"transfer_receipt", "transfer_loss", "transfer_cancellation", "transfer_discrepancy", "late_expense"}:
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
        pools = {"FBS", "FBO"} if scope == "both" else {scope}
        weights = {k: n["mass"] for k, n in nodes.items()
                   if n["facility_id"] == facility and n["pool"] in pools and n["mass"] > 0}
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

    prices = _solve(nodes, links)
    basis_id = digest({"date": day, "opening": opening, "auxiliary_opening": prior,
                       "documents": {i: d["fingerprint"] for i, d in current.items()}, "allocations": allocations_evidence})
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
        auxiliary["fbo_rows"][k] = {"facility_id": n["facility_id"], "nm_id": n["nm_id"],
            "quantity": str(q), "wac_rub": text(wac), "capital_rub": text(q * wac) if wac is not None else "0",
            "source": {"policy": DOCUMENT_POLICY, "business_date": day, "valuation_basis_id": basis_id}}
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
    return documents, auxiliary, {"id": basis_id, "policy": DOCUMENT_POLICY, "allocations": allocations_evidence}
