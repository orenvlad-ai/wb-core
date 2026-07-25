"""One deterministic chronology for every FF replay contour."""

from __future__ import annotations

from typing import Any, Mapping


def is_supplier_receipt(operation: Mapping[str, Any]) -> bool:
    return (
        str(operation.get("source_type") or "") == "supplier_shipment"
        and str(operation.get("operation_type") or "") == "auto_receipt"
    )


def ff_operation_replay_sort_key(
    operation: Mapping[str, Any],
    *,
    business_date: str = "",
) -> tuple[str, int, str, str]:
    """Order by immutable ingestion time, with receipt first at one second.

    Business date remains in the key as a deterministic tie-break/evidence
    field.  It cannot place a dependent outbound before a supplier receipt
    created in the same second.
    """

    return (
        str(operation.get("created_at") or ""),
        0 if is_supplier_receipt(operation) else 1,
        str(business_date or ""),
        str(operation.get("operation_id") or ""),
    )
