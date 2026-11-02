"""Typed outgoing-only cost continuity; missing quantity days stay missing.

Only the already-admitted no-document gap can be revised. Native FBO dispatches
and one expense-free FBS-to-FBO distribution change its auxiliary cost book,
but do not establish official availability for any missing date. The resumed
period retains its actual official snapshot and every FBS value byte-for-byte.
"""
from copy import deepcopy
from decimal import localcontext

from packages.application.fbs_document_cost import resolve_document_costs
from packages.application.fbs_snapshot_cost import fingerprint
from packages.application.fbs_historical_fbo_sources import SOURCE_CONTRACT

CONTRACT = "fbs_historical_fbo_documentary_gap_v1"


def require(condition, code):
    if not condition:
        raise ValueError("historical_fbo_gap_" + code)


def _fold(opening, auxiliary, documents):
    previous = deepcopy(auxiliary)
    valuations = {}
    for day in sorted({d["business_date"] for d in documents}):
        own = [d for d in documents if d["business_date"] == day]
        _, previous, valuation = resolve_document_costs(
            {"baseline": {"document_cost_state": previous}, "periods": {}},
            {"business_date": day, "documents": own, "quantity_snapshot": {"rows": []}}, opening, {})
        valuations[day] = valuation
    return previous, valuations


def validate_documentary_gap(period, *, opening_day, opening, known, opening_auxiliary):
    """Validate a saved proof without fabricating a period at a missing date."""
    from packages.application.fbs_snapshot_cost import _day, _gap_mapping
    from datetime import timedelta
    proof = period["cost_continuity"]
    resumed = period["snapshot"]["date"]
    days = [(_day(opening_day) + timedelta(days=n)).isoformat()
            for n in range(1, (_day(resumed) - _day(opening_day)).days)]
    require(proof["contract"] == CONTRACT and proof["opening_date"] == opening_day
            and proof["resumed_date"] == resumed and proof["missing_dates"] == days and days,
            "date_scope_invalid")
    require(proof["document_manifest"] == known and proof["opening_rows_digest"] == fingerprint(opening)
            and proof["opening_auxiliary_digest"] == fingerprint(opening_auxiliary), "opening_authority_changed")
    require(proof["facility_mapping"] == _gap_mapping(period["snapshot"]), "facility_mapping_changed")
    original = proof["original_period"]
    require(fingerprint(original) == proof["original_period_digest"]
            and original["cost_continuity"] == {"contract": "fbs_no_document_gap_v1", "opening_date": opening_day,
                "resumed_date": resumed, "missing_dates": days, "opening_rows_digest": fingerprint(opening),
                "document_manifest": known, "facility_mapping": proof["facility_mapping"]}, "original_gap_changed")
    require(original["applied_documents"] == {} and original["opening_date"] == opening_day
            and original["opening_rows"] == opening and set(original["rows"]) == set(opening)
            and all(r["wac_rub"] == opening[k]["wac_rub"] for k, r in original["rows"].items()),
            "original_gap_not_outgoing_only")
    require(all(period[k] == original[k] for k in ("snapshot", "rows", "opening_date", "opening_rows")),
            "official_quantity_or_fbs_values_changed")
    documents = proof["documents"]
    require(len(documents) == 3 and len({d["document_id"] for d in documents}) == 3
            and sorted(d["kind"] for d in documents) == ["historical_wb_dispatch", "historical_wb_dispatch", "pool_reallocation"],
            "typed_cohort_invalid")
    require(all(d.get("historical_fbo_native", {}).get("contract") == SOURCE_CONTRACT
                and d["historical_fbo_native"]["plan_digest"] == proof["native_plan_digest"]
                and opening_day < d["business_date"] <= resumed
                and not d["cost_document"]["expense_lines"] for d in documents), "native_authority_missing")
    reallocations = [d for d in documents if d["kind"] == "pool_reallocation"]
    domain = reallocations[0]["cost_document"]["domain"]
    require(domain["source_pool"] == "FBS" and domain["destination_pool"] == "FBO"
            and reallocations[0]["business_date"] < resumed, "distribution_kind_invalid")
    manifest = {d["document_id"]: d["fingerprint"] for d in documents}
    require(proof["admitted_document_manifest"] == manifest and period["applied_documents"] == manifest
            and proof["document_material_digest"] == fingerprint(documents), "document_manifest_changed")
    auxiliary, valuations = _fold(opening, opening_auxiliary, documents)
    require(period["document_cost_state"] == auxiliary and period["document_valuation"] == valuations[resumed],
            "cost_replay_changed")
    return days


def document_manifest_for_gap(proof):
    return {**proof["document_manifest"], **proof.get("admitted_document_manifest", {})}


def build_historical_fbo_state_revision(book, capture):
    """Rebuild the resumed and real suffix states from verified native sources.

    Derived WB/retained/ready/Finance editions are intentionally not generated
    here: their independent source-preserving adapter must consume this exact
    state. This function performs no IO and never alters the supplied book.
    """
    from packages.application.fbs_snapshot_cost import (
        _capture, cost_continuity_missing_dates, evaluate_candidate, close_candidate_period,
    )
    require(book.get("schema") == "fbs_active_snapshot_accounting_v1" and book.get("active") is True
            and not book.get("publication_error"), "active_healthy_book_required")
    original = book["state"]
    cost_continuity_missing_dates(original)
    _, _, documents = _capture(capture)
    require(capture["source_digest"] == fingerprint({k: capture[k] for k in
        ("contract", "business_date", "quantity_snapshot", "documents_complete", "documents", "documents_reason")}),
        "capture_digest_mismatch")
    typed = [d for d in documents.values() if d.get("historical_fbo_native", {}).get("contract") == SOURCE_CONTRACT]
    require(len(typed) == 4 and len({d["historical_fbo_native"]["plan_digest"] for d in typed}) == 1,
            "complete_native_cohort_required")
    require(all(d["historical_fbo_native"]["source_proof"]["book_version"] == fingerprint(book) for d in typed),
            "original_book_compare_and_swap_failed")
    first = min(d["business_date"] for d in typed)
    resumed = [day for day, period in original["periods"].items()
               if period.get("cost_continuity", {}).get("contract") == "fbs_no_document_gap_v1"
               and first in period["cost_continuity"]["missing_dates"]]
    require(len(resumed) == 1, "original_no_document_gap_required")
    resumed = resumed[0]
    saved = original["periods"][resumed]
    require(saved["status"] == "closed", "closed_resumed_period_required")
    opening_day = saved["opening_date"]
    opening_period = original["periods"].get(opening_day, original["baseline"])
    prefix_manifest = dict(original["baseline"]["absorbed_documents"])
    entire_manifest = dict(prefix_manifest)
    for day, period in sorted(original["periods"].items()):
        entire_manifest.update(period["applied_documents"])
        if day < resumed:
            prefix_manifest.update(period["applied_documents"])
    require(all(identity in documents and documents[identity]["fingerprint"] == value
                for identity, value in entire_manifest.items()), "absorbed_document_changed")
    require(set(documents) - set(entire_manifest) == {d["document_id"] for d in typed}, "unapproved_new_document")
    require(all(d["document_id"] in prefix_manifest or d in typed or d["business_date"] > max(v["business_date"] for v in typed)
                for d in documents.values()), "unexpected_cost_interval_document")
    gap_docs = sorted([d for d in typed if d["business_date"] <= resumed], key=lambda d: (d["business_date"], d["document_id"]))
    auxiliary, valuations = _fold(saved["opening_rows"], opening_period["document_cost_state"], gap_docs)
    replacement = deepcopy(saved)
    replacement["document_cost_state"] = auxiliary
    replacement["document_valuation"] = valuations[resumed]
    replacement["applied_documents"] = {d["document_id"]: d["fingerprint"] for d in gap_docs}
    replacement["source_digest"] = fingerprint([capture["source_digest"], resumed, saved["snapshot"]])
    proof = {**deepcopy(saved["cost_continuity"]), "contract": CONTRACT,
        "original_period": deepcopy(saved), "original_period_digest": fingerprint(saved),
        "opening_auxiliary_digest": fingerprint(opening_period["document_cost_state"]),
        "native_plan_digest": typed[0]["historical_fbo_native"]["plan_digest"], "documents": deepcopy(gap_docs),
        "document_material_digest": fingerprint(gap_docs), "admitted_document_manifest": replacement["applied_documents"]}
    replacement["cost_continuity"] = proof
    validate_documentary_gap(replacement, opening_day=opening_day, opening=saved["opening_rows"],
                             known=prefix_manifest, opening_auxiliary=opening_period["document_cost_state"])
    state = deepcopy(original)
    state["periods"] = {d: deepcopy(p) for d, p in original["periods"].items() if d < resumed}
    state["periods"][resumed] = replacement
    state["last_document_capture_at"] = capture["captured_at"]
    for day, period in sorted(original["periods"].items()):
        if day <= resumed:
            continue
        dated = deepcopy(capture)
        dated["business_date"] = day
        dated["quantity_snapshot"] = deepcopy(period["snapshot"])
        dated["source_digest"] = fingerprint([capture["source_digest"], day, period["snapshot"]])
        state = evaluate_candidate(state, dated)
        require(state["last_attempt"]["status"] == "evaluated" and not state["pending_documents"], "suffix_replay_incomplete")
        if period["status"] == "closed":
            state = close_candidate_period(state, day, today=capture["business_date"])
    cost_continuity_missing_dates(state)
    require(set(state["periods"]) == set(original["periods"]), "missing_quantity_period_manufactured")
    require(all(state["periods"][d]["snapshot"] == p["snapshot"] for d, p in original["periods"].items()),
            "official_quantity_snapshot_changed")
    return {"contract": CONTRACT, "status": "state_prepared_pending_stage_revision", "database_written": False,
            "expected_book": fingerprint(book), "source_digest": capture["source_digest"], "candidate_state": state,
            "derived_dates_to_rebuild": sorted(d for d in state["periods"] if d >= resumed),
            "unavailable_quantity_dates": sorted(cost_continuity_missing_dates(state))}
