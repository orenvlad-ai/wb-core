"""Smoke-check read-only supplier expense allocation status projections."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.supplier_expense_allocation import (  # noqa: E402
    project_supplier_document_expense_allocation,
    project_supplier_order_expense_allocation,
)


def main() -> None:
    informational = {"document_id": "contract", "document_type": "contract"}
    if project_supplier_document_expense_allocation(informational, {})["status"] != "not_applicable":
        raise AssertionError("contract must not affect the expense allocation aggregate")
    packing = {"document_id": "packing", "document_type": "packing_list"}
    if project_supplier_document_expense_allocation(packing, {})["status"] != "not_applicable":
        raise AssertionError("packing list must not affect the expense allocation aggregate")

    none_breakdown = _breakdown(
        controls=[_document_control("customs", eligible=2, allocated=0, conserved=False, reasons=["parse needs review"])],
        certified=False,
    )
    none_projection = project_supplier_document_expense_allocation(
        {"document_id": "customs", "document_type": "customs_declaration"},
        none_breakdown,
    )
    if none_projection["status"] != "none" or none_projection["diagnostics"]["eligible_components"] != 2:
        raise AssertionError(f"document none diagnostics mismatch: {none_projection}")
    if project_supplier_order_expense_allocation(none_breakdown)["status"] != "none":
        raise AssertionError("order with zero allocated canonical cost must be none")

    partial_breakdown = _breakdown(
        controls=[_document_control("customs", eligible=2, allocated=1, conserved=False, reasons=["one component ambiguous"])],
        certified=False,
    )
    partial_projection = project_supplier_document_expense_allocation(
        {"document_id": "customs", "document_type": "customs_declaration"},
        partial_breakdown,
    )
    if partial_projection["status"] != "partial" or partial_projection["diagnostics"]["allocated_components"] != 1:
        raise AssertionError(f"document partial diagnostics mismatch: {partial_projection}")

    uncertified_breakdown = _breakdown(
        controls=[_document_control("customs", eligible=2, allocated=2, conserved=True)],
        certified=False,
    )
    full_document = project_supplier_document_expense_allocation(
        {"document_id": "customs", "document_type": "customs_declaration"},
        uncertified_breakdown,
    )
    order_without_gate = project_supplier_order_expense_allocation(uncertified_breakdown)
    if full_document["status"] != "all" or order_without_gate["status"] != "partial":
        raise AssertionError(
            f"document may be fully allocated while order stays non-green before certification: {full_document} {order_without_gate}"
        )

    certified_breakdown = _breakdown(
        controls=[_document_control("customs", eligible=2, allocated=2, conserved=True)],
        certified=True,
    )
    certified_order = project_supplier_order_expense_allocation(certified_breakdown)
    if certified_order["status"] != "all" or not certified_order["diagnostics"]["fingerprints_match"]:
        raise AssertionError(f"certified all projection mismatch: {certified_order}")

    fingerprint_drift = _breakdown(
        controls=[_document_control("customs", eligible=2, allocated=2, conserved=True)],
        certified=True,
    )
    fingerprint_drift["certification"]["certified_calculation_fingerprint"] = "sha256:stale"
    drift_projection = project_supplier_order_expense_allocation(fingerprint_drift)
    if drift_projection["status"] != "partial" or drift_projection["diagnostics"]["fingerprints_match"]:
        raise AssertionError(f"fingerprint drift must remove green order status: {drift_projection}")
    print("supplier_expense_allocation_smoke: OK")


def _document_control(
    document_id: str,
    *,
    eligible: int,
    allocated: int,
    conserved: bool,
    reasons: list[str] | None = None,
) -> dict[str, object]:
    return {
        "document_id": document_id,
        "document_type": "customs_declaration",
        "cost_affecting": True,
        "eligible_component_count": eligible,
        "allocated_component_count": allocated,
        "eligible_amount_rub": str(eligible * 100),
        "allocated_amount_rub": str(allocated * 100),
        "conserved": conserved,
        "incomplete_reasons": reasons or [],
    }


def _breakdown(*, controls: list[dict[str, object]], certified: bool) -> dict[str, object]:
    source = "sha256:source"
    calculation = "sha256:calculation"
    return {
        "document_controls": controls,
        "blockers": [],
        "controls": {
            "document_allocation_conserved": True,
            "document_counted_once": True,
            "line_components_equal_capital": True,
            "shipment_lines_equal_capital": True,
        },
        "certification": {
            "certified": certified,
            "source_fingerprint_matches": certified,
            "source_fingerprint": source,
            "calculation_fingerprint": calculation,
            "certified_source_fingerprint": source if certified else None,
            "certified_calculation_fingerprint": calculation if certified else None,
        },
    }


if __name__ == "__main__":
    main()
