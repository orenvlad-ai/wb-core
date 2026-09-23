"""Dated quantities from an exact, verified accounting book (no current lookup).

The accounting book certifies the dense official snapshot on admission. Its
quantity evidence is independent of the preliminary monetary presentation.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from packages.application.fbs_snapshot_cost import fingerprint
from packages.business_time import business_date_from_timestamp

CONTRACT = "bound_inventory_quantity_v1"
POLICY_VERSION = "inventory_quantity_history_v1"
EFFECTIVE_DATE = "2026-09-08"
BOOK_SOURCE = "fbs_snapshot_inventory_presentation_v1"
OFFICIAL_SOURCE = "official_fbs_stock_snapshot_v1"
WB_KIND = "wb_physical_qty"
FBS_KIND = "fbs_available_qty"
REPAIR_REASON = "Требуется исправление истории"
# Reviewed incident identities, not inferred from coincident quantities. Keeping
# capture + evidence binding also masks a forward restoration of the old capture.
KNOWN_BAD_CAPTURES = {
    ("2026-09-08", "ivhc_baa9cd9dac5cd66b943a1a064a34",
     "sha256:96f07ea67d836ee149fa7e6b89a276e3fe371138563d7b4237b449eb8f064e76"),
    ("2026-09-09", "ivhc_536ef3888636c59e9fb6ce5aa5a3",
     "sha256:216ee6b057d6f6ab3ca8cd575e2a6f794dce33f926e7cbee937b60be664c8ee6"),
}


def quantity(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid_inventory_quantity") from exc
    if isinstance(value, bool) or not result.is_finite() or result < 0 or result != result.to_integral_value():
        raise ValueError("invalid_inventory_quantity")
    return int(result)


def bound_book_quantities(*, book: Mapping[str, Any], book_version: str,
                          binding: Mapping[str, Any], target: Mapping[str, Any],
                          day: str, require_closed: bool = False) -> dict[str, Any]:
    """Validate the bound retained book and return only typed quantity operands."""
    if (book.get("schema") != "fbs_active_snapshot_accounting_v1"
            or fingerprint(book) != book_version or binding.get("book_version") != book_version):
        raise ValueError("inventory_quantity_book_binding_mismatch")
    payload = book.get("presentations", {}).get(day)
    if (not payload or day < EFFECTIVE_DATE or day < str(book.get("effective_date", ""))
            or binding.get("date") != day or payload.get("date") != day
            or binding.get("source") != BOOK_SOURCE or payload.get("source") != BOOK_SOURCE
            or binding.get("effective_date") != book.get("effective_date")
            or binding.get("presentation_version") != payload.get("version_id")
            or binding.get("quality") != payload.get("quality")
            or not target or binding.get("ready_target") != target):
        raise ValueError("inventory_quantity_presentation_binding_mismatch")
    period = book.get("state", {}).get("periods", {}).get(day)
    if require_closed and (not period or period.get("status") != "closed"):
        raise ValueError("inventory_quantity_closed_period_required")
    snapshot = payload.get("quantity_snapshot", {})
    wb = book.get("wb_days", {}).get(day, {})
    if (snapshot.get("date") != day or not snapshot.get("id") or not snapshot.get("digest")
            or snapshot.get("source") != OFFICIAL_SOURCE
            or business_date_from_timestamp(snapshot.get("captured_at", "")) != day
            or wb.get("business_date") != day
            or wb.get("source", {}).get("snapshot_date") != day
            or business_date_from_timestamp(wb.get("source", {}).get("fetched_at", "")) != day):
        raise ValueError("inventory_quantity_source_date_or_identity_mismatch")
    if period is not None and period.get("snapshot") != snapshot:
        raise ValueError("inventory_quantity_period_snapshot_mismatch")
    identities = payload.get("rows", {})
    nms = sorted(int(nm) for nm in identities)
    if not nms or nms[0] <= 0 or len(set(nms)) != len(nms):
        raise ValueError("inventory_quantity_roster_invalid")
    facilities = snapshot.get("facility_evidence", {})
    if not facilities:
        raise ValueError("inventory_quantity_facility_evidence_missing")
    fbs_rows = {}
    for row in snapshot.get("rows", []):
        key = (int(row["nm_id"]), str(row["facility_id"]))
        if key in fbs_rows or key[0] not in nms or key[1] not in facilities:
            raise ValueError("inventory_quantity_duplicate_or_foreign_scope")
        fbs_rows[key] = quantity(row.get("quantity"))
    expected = {(nm, fid) for nm in nms for fid in facilities}
    fbs_complete = snapshot.get("complete") is True and set(fbs_rows) == expected
    wb_rows = {}
    for row in wb.get("rows", []):
        nm = int(row["nm_id"])
        if nm in wb_rows or nm not in nms:
            raise ValueError("inventory_quantity_wb_duplicate_or_foreign_scope")
        wb_rows[nm] = row
    wb_complete = (wb.get("authority_complete") is True and set(wb_rows) == set(nms)
                   and sorted(wb.get("requested_nm_ids", [])) == nms)
    refs = {"book_version": book_version, "binding": deepcopy(dict(binding)),
            "presentation_digest": fingerprint(payload), "presentation_version": payload["version_id"],
            "wb_digest": fingerprint(wb), "quantity_digest": fingerprint(snapshot),
            "period_digest": fingerprint(period) if period is not None else "",
            "roster_digest": fingerprint(nms), "facility_digest": fingerprint(facilities)}
    roster = [{"facility_id": fid, "name": evidence.get("facility_name", fid),
               "code": evidence.get("code", fid), "active": True, "applicable": True,
               "display_order": index, "effective_from": book["effective_date"],
               "evidence": deepcopy(evidence)}
              for index, (fid, evidence) in enumerate(sorted(facilities.items()), 1)]
    components = []
    def add(nm, fid, value, kind, observed, evidence):
        components.append({"scope_kind": "TOTAL" if nm is None else "SKU",
            "scope_key": "TOTAL" if nm is None else f"SKU:{nm}", "nm_id": nm,
            "component_kind": "WB" if kind == WB_KIND else "FBS_FACILITY",
            "component_id": fid, "component_label": "WB" if kind == WB_KIND else facilities[fid].get("facility_name", fid),
            "quantity": value, "state": "missing" if value is None else "exact_zero" if value == 0 else "exact",
            "source_revision": str(wb.get("version_id", "")) if kind == WB_KIND else snapshot["id"],
            "source_digest": refs["wb_digest"] if kind == WB_KIND else snapshot["digest"],
            "source_watermark": observed,
            "provenance": {"contract": CONTRACT, "semantic_kind": kind, "unit": "pcs",
                "business_date": day, "quantity_quality": "complete" if value is not None else "missing",
                "cost_quality": payload.get("quality", "unknown"), "source_observed_at": observed,
                "physical": None if kind == FBS_KIND else value, "reserved": None,
                "required_scope": len(nms), "covered_scope": len(nms) if value is not None else 0,
                "source_refs": refs, "zero_evidence": evidence if value == 0 else {},
                "identity": deepcopy(identities.get(str(nm), {}).get("identity", {}))}})
    for nm in nms:
        row = wb_rows.get(nm, {})
        physical = quantity(row.get("components", {}).get("physical")) if wb_complete else None
        if quantity(identities[str(nm)].get("wb_physical")) != physical and physical is not None:
            raise ValueError("inventory_quantity_wb_presentation_mismatch")
        add(nm, "WB", physical, WB_KIND, wb["source"]["fetched_at"], {"authority_complete": wb_complete, "requested_nm_id": nm})
        for fid in sorted(facilities):
            evidence = facilities[fid]
            if (not evidence.get("stock_run_id") or not evidence.get("stock_digest")
                    or not evidence.get("mapping_id")
                    or business_date_from_timestamp(evidence.get("captured_at", "")) != day):
                raise ValueError("inventory_quantity_facility_binding_missing")
            add(nm, fid, fbs_rows.get((nm, fid)) if fbs_complete else None, FBS_KIND,
                evidence["captured_at"], {"dense_admitted_snapshot": snapshot["id"], "facility_evidence": evidence, "nm_id": nm})
    for fid, kind in [("WB", WB_KIND), *((fid, FBS_KIND) for fid in sorted(facilities))]:
        selected = [c for c in components if c["component_id"] == fid]
        total = sum(c["quantity"] for c in selected) if all(c["quantity"] is not None for c in selected) else None
        add(None, fid, total, kind, selected[0]["source_watermark"], {"all_dated_sku_components": fingerprint(selected)})
    return {"contract": CONTRACT, "business_date": day, "facility_roster": roster,
            "components": components, "source_manifest": {"contract": CONTRACT, "source_refs": refs,
                "business_date": day, "nm_ids": nms, "facility_ids": sorted(facilities),
                "cost_quality": payload.get("quality", "unknown")}}


def resolve_plan_quantities(plan, *, day, runtime_dir=None, prepared_book=None, require_closed=False, ready_target=None, book_connection=None):
    binding = dict(plan.metadata or {}).get("fbs_accounting_bindings", {}).get(day)
    if not binding or day < EFFECTIVE_DATE:
        return None
    target = (ready_target or dict(plan.metadata or {}).get("fbs_accounting_targets", {}).get(day)
              or dict(plan.metadata or {}).get("ready_publication_target"))
    if prepared_book is not None and fingerprint(prepared_book) == binding.get("book_version"):
        book, version = prepared_book, fingerprint(prepared_book)
    else:
        if runtime_dir is None:
            raise ValueError("inventory_quantity_bound_book_required")
        from packages.application.fbs_accounting_runtime import load
        book, version = load(runtime_dir, version=binding["book_version"], connection=book_connection)
    return bound_book_quantities(book=book, book_version=version, binding=binding, target=target,
                                 day=day, require_closed=require_closed)
