"""Pure bounded FBO dispatch preparation across a documented cost-only gap.

The capture owner must supply immutable opening rows, a complete empty native
cost-document interval, canonical WB goods and independent physical evidence
for the one FBS-to-FBO distribution. Available FBS accounting stock is never
used as physical stock. No missing official quantity period is manufactured.

This is the numeric seam for a future owned native/publication adapter, not
authority to write. It deliberately returns ``prepared_unconnected`` even when
all numeric checks pass. Normal document admission and receipt recovery remain
unchanged. The native writer must re-read the same capture under its own lock.
"""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, localcontext

from packages.application.fbs_document_cost import (
    DOCUMENT_POLICY, key, number, resolve_document_costs, text, units,
)
from packages.application.fbs_snapshot_cost import fingerprint

CONTRACT = "historical_fbo_dispatch_cost_preview_v1"
CAPTURE_CONTRACT = "historical_fbo_dispatch_cost_capture_v1"
MAX_DAYS = 31
MAX_SKUS = 500
OBLIGATIONS = (
    "locked_native_source_and_book_revalidation",
    "atomic_pool_and_canonical_aggregate_debit",
    "typed_cost_gap_admission_at_real_resumed_period",
    "source_preserving_wb_and_transit_revision",
    "owned_ready_history_finance_publication",
)


class HistoricalFboDispatchError(ValueError):
    def __init__(self, code, **details):
        super().__init__(code)
        self.code, self.details = code, details


def require(condition, code, **details):
    if not condition:
        raise HistoricalFboDispatchError(code, **details)


def _digest(value):
    return (type(value) is str and len(value) == 71 and value.startswith("sha256:")
            and all(c in "0123456789abcdef" for c in value[7:]))


def _day(value):
    require(type(value) is str and date.fromisoformat(value).isoformat() == value,
            "historical_fbo_date_invalid")
    return date.fromisoformat(value)


def _positive(value):
    require(type(value) is int and value > 0, "historical_fbo_positive_integer_required")
    return value


def _rows(capture, scope):
    opening = capture["opening"]
    require(_digest(opening["period_digest"]) and _digest(opening["source_digest"]),
            "historical_fbo_opening_authority_missing")
    rows = deepcopy(opening["fbo_rows"])
    require(0 < len(rows) <= MAX_SKUS, "historical_fbo_opening_scope_invalid")
    for identity, row in rows.items():
        require(identity == key(scope["facility_id"], "FBO", row["nm_id"])
                and row["facility_id"] == scope["facility_id"], "historical_fbo_opening_location_mismatch")
        q, capital = units(row["quantity"]), number(row["capital_rub"])
        require((q > 0 and row["wac_rub"] is not None and number(row["wac_rub"]) > 0)
                or (q == 0 and capital == 0), "historical_fbo_opening_cost_missing", nm_id=row["nm_id"])
        # Existing daily books use precision 50; reproduce that policy rather
        # than rounding to kopecks or substituting a native current pool price.
        if q:
            require(capital == q * number(row["wac_rub"]),
                    "historical_fbo_opening_conservation_failed", nm_id=row["nm_id"])
    return rows


def _prepare(capture, scope):
    require(capture.get("contract") == CAPTURE_CONTRACT, "historical_fbo_capture_contract_invalid")
    require(_digest(capture["book_version"]) and _digest(capture["native_query_fence_digest"]),
            "historical_fbo_source_authority_missing")
    require(capture["source_digest"] == fingerprint({k: v for k, v in capture.items() if k != "source_digest"}),
            "historical_fbo_capture_digest_mismatch")
    dispatches = scope["dispatches"]
    require(len(dispatches) == 3 and len({v["supply_id"] for v in dispatches}) == 3,
            "historical_fbo_dispatch_scope_invalid")
    transfer = scope["reallocation"]
    require(transfer["source_pool"] == "FBS" and transfer["destination_pool"] == "FBO",
            "historical_fbo_reallocation_direction_invalid")
    _positive(transfer["quantity"]); _positive(transfer["nm_id"])
    opening_day = _day(capture["opening"]["business_date"])
    first = min(_day(v["business_date"]) for v in [*dispatches, transfer])
    last = max(_day(v["business_date"]) for v in [*dispatches, transfer])
    require(opening_day < first <= last and (last - opening_day).days <= MAX_DAYS,
            "historical_fbo_date_scope_invalid")
    coverage = capture["native_cost_document_coverage"]
    require(coverage["complete"] is True
            and coverage["date_from"] == (opening_day + timedelta(days=1)).isoformat()
            and coverage["date_to"] == last.isoformat()
            and coverage["documents"] == [] and coverage["requests"] == []
            and _digest(coverage["query_fence_digest"]), "historical_fbo_cost_gap_not_empty")
    require(capture["known_document_manifest"] == capture["opening"]["document_manifest"],
            "historical_fbo_absorbed_source_drift")
    require(all(_digest(v) for v in capture["protected_source_fingerprints"].values())
            and capture["protected_source_fingerprints"], "historical_fbo_preservation_proof_missing")
    rows = _rows(capture, scope)
    facility = scope["facility_id"]
    fbs = deepcopy(capture["opening"]["fbs_reallocation_row"])
    require(fbs["nm_id"] == transfer["nm_id"] and fbs["facility_id"] == facility
            and fbs["wac_rub"] is not None and number(fbs["wac_rub"]) > 0,
            "historical_fbo_reallocation_cost_missing")
    require(units(fbs["quantity"]) > 0, "historical_fbo_reallocation_cost_mass_missing")
    physical = capture["reallocation_physical_source"]
    require(physical["business_date"] == transfer["business_date"]
            and physical["facility_id"] == facility and physical["nm_id"] == transfer["nm_id"]
            and physical["complete"] is True
            and _digest(physical["source_digest"])
            and units(physical["quantity_before"]) >= transfer["quantity"],
            "historical_fbo_reallocation_physical_basis_missing")
    allocation = capture["restored_pool_allocation"]
    require(allocation["basis"] == "owner_authorized_documentary_restoration"
            and allocation["business_date"] == transfer["business_date"]
            and allocation["source_pool"] == "FBS" and allocation["destination_pool"] == "FBO"
            and allocation["nm_id"] == transfer["nm_id"] and allocation["quantity"] == transfer["quantity"]
            and allocation["facility_id"] == facility and _digest(allocation["source_digest"])
            and _digest(allocation["native_ledger_basis_digest"]),
            "historical_fbo_restored_pool_allocation_missing")
    require(_digest(transfer["documentary_source_digest"]), "historical_fbo_reallocation_source_missing")
    outgoing = []
    required_nms = {transfer["nm_id"]}
    for dispatch in sorted(dispatches, key=lambda d: (d["business_date"], d["supply_id"])):
        supply = dispatch["supply_id"]
        require(type(supply) is str and supply.isdecimal() and str(int(supply)) == supply,
                "historical_fbo_supply_identity_invalid")
        source = capture["supply_sources"][supply]
        require(source["supply_id"] == supply and source["cache_key"] == "supply:" + supply
                and source["wb_supply_id"] == supply and source["business_date"] == dispatch["business_date"]
                and _digest(source["source_digest"]), "historical_fbo_supply_alias_or_date_mismatch")
        require(source["existing_debits"] == [], "historical_fbo_native_debit_already_exists")
        items = source["items"]
        require(0 < len(items) <= MAX_SKUS and len({v["nm_id"] for v in items}) == len(items),
                "historical_fbo_goods_scope_invalid")
        for item in items:
            required_nms.add(_positive(item["nm_id"]))
            _positive(item["quantity"])
        require(sum(v["quantity"] for v in items) == _positive(dispatch["expected_quantity"]),
                "historical_fbo_packed_quantity_mismatch")
        outgoing.append({"supply_id": supply, "source_key": "wb_supply_debit:supply:" + supply,
                         "business_date": dispatch["business_date"], "facility_id": facility,
                         "source_digest": source["source_digest"], "items": deepcopy(items)})
    require(required_nms <= {r["nm_id"] for r in rows.values()}, "historical_fbo_opening_sku_missing")
    require(set(capture["supply_sources"]) == {v["supply_id"] for v in dispatches},
            "historical_fbo_unselected_supply_source")
    # This exact source digest is intentionally retained even for an unchanged
    # numerical result. Rebinding post-recovery produces a different candidate.
    source_proof = {"book_version": capture["book_version"], "capture_digest": capture["source_digest"],
                    "scope_digest": fingerprint(scope), "opening_period_digest": capture["opening"]["period_digest"]}
    auxiliary = {"policy": DOCUMENT_POLICY, "fbo_rows": rows, "transfers": {}}
    effects, valuations = [], {}
    for day in sorted({v["business_date"] for v in [*dispatches, transfer]}):
        documents = []
        reallocation = day == transfer["business_date"]
        if reallocation:
            identity = "historical-draft:reallocation:" + fingerprint([source_proof, transfer])[7:]
            document = {"document_id": identity, "root_document_id": identity, "kind": "pool_reallocation",
                        "business_date": day, "posted_at": "", "events": [],
                        "cost_document": {"domain": {"facility_id": facility, "source_pool": "FBS", "destination_pool": "FBO"},
                            "lines": [{"nm_id": transfer["nm_id"], "quantity": transfer["quantity"]}],
                            "expense_lines": [], "movements": []}}
            document["fingerprint"] = fingerprint(document)
            documents.append(document)
        day_outgoing = [v for v in outgoing if v["business_date"] == day]
        # The solver consumes location identities here, not an official stock
        # snapshot. Nothing in this scratch capture becomes a saved period.
        _, auxiliary, valuation = resolve_document_costs(
            {"baseline": {"document_cost_state": auxiliary}, "periods": {}},
            {"business_date": day, "documents": documents, "quantity_snapshot": {"rows": []}},
            {facility + ":" + str(fbs["nm_id"]): fbs}, {}, historical_fbo_outgoings=day_outgoing)
        valuations[day] = valuation
        if reallocation:
            value = number(fbs["wac_rub"]) * transfer["quantity"]
            movements = [{"facility_id": facility, "pool": pool, "nm_id": transfer["nm_id"],
                "quantity_delta": sign * transfer["quantity"], "capital_delta_rub": text(sign * value),
                "wac_rub": fbs["wac_rub"], "valuation_basis_id": valuation["id"]}
                for pool, sign in (("FBS", -1), ("FBO", 1))]
            effects.append({"kind": "pool_reallocation", "business_date": day, "source_digest": transfer["documentary_source_digest"],
                            "pool_movements": movements, "aggregate_movements": [], "expense_rub": "0"})
        for dispatch in day_outgoing:
            lines = [v for v in valuation["historical_fbo_outgoings"] if v["supply_id"] == dispatch["supply_id"]]
            effects.append({"kind": "historical_wb_dispatch", "business_date": day, "source_key": dispatch["source_key"],
                            "pool_movements": deepcopy(lines), "aggregate_movements": deepcopy(lines), "expense_rub": "0"})
    result = {"contract": CONTRACT, "status": "prepared_unconnected", "database_written": False,
              "source_proof": source_proof, "effects": effects, "valuations": valuations,
              "fbo_rows_after": auxiliary["fbo_rows"], "official_quantity_periods_created": [],
              "protected_source_fingerprints": deepcopy(capture["protected_source_fingerprints"]),
              "reallocation_evidence": {"physical": deepcopy(physical), "pool_attribution": deepcopy(allocation)},
              "unresolved_obligations": list(OBLIGATIONS)}
    result["plan_digest"] = fingerprint(result)
    return result


def prepare_historical_fbo_dispatch_plan(capture, scope):
    """Return a bounded numeric preview or a refusal with no partial effects."""
    try:
        with localcontext() as context:
            context.prec = 50
            return _prepare(capture, scope)
    except (ValueError, KeyError, TypeError, ArithmeticError) as error:
        return {"contract": CONTRACT, "status": "blocked", "database_written": False,
                "reason": error.code if isinstance(error, HistoricalFboDispatchError) else str(error),
                "details": error.details if isinstance(error, HistoricalFboDispatchError) else {}}


def validate_historical_fbo_dispatch_plan(plan, *, capture, scope):
    fresh = prepare_historical_fbo_dispatch_plan(capture, scope)
    require(fresh["status"] == "prepared_unconnected", "historical_fbo_fresh_capture_blocked", reason=fresh.get("reason"))
    require(plan == fresh, "historical_fbo_plan_or_source_changed")
    return fresh
