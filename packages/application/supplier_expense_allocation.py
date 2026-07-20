"""Read-only UI projections over the canonical supplier cost proof."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping


STATUS_LABELS_RU = {
    "all": "Все расходы распределены",
    "partial": "Расходы распределены частично",
    "none": "Расходы не распределены",
    "not_applicable": "Не требует распределения",
}


def project_supplier_document_expense_allocation(
    document: Mapping[str, Any],
    canonical_breakdown: Mapping[str, Any],
) -> dict[str, Any]:
    """Project one document status without calculating or reallocating cost."""

    document_id = str(document.get("document_id") or "").strip()
    document_type = str(document.get("document_type") or "").strip()
    controls = [
        dict(item)
        for item in canonical_breakdown.get("document_controls") or []
        if isinstance(item, Mapping)
    ]
    control = next(
        (
            item
            for item in controls
            if document_id and str(item.get("document_id") or "") == document_id
        ),
        None,
    )
    if control is None:
        control = next(
            (
                item
                for item in controls
                if not str(item.get("document_id") or "")
                and document_type
                and str(item.get("document_type") or "") == document_type
            ),
            None,
        )
    if control is None:
        cost_affecting_types = {
            str(value or "")
            for value in canonical_breakdown.get("cost_affecting_document_types") or []
        }
        if document_type in cost_affecting_types:
            return _projection(
                "none",
                eligible_documents=1,
                eligible_components=0,
                allocated_components=0,
                eligible_amount_rub=None,
                allocated_amount_rub=None,
                reasons=["Расходный документ не загружен или не дал распознанных cost-компонентов"],
            )
        return _projection(
            "not_applicable",
            eligible_documents=0,
            eligible_components=0,
            allocated_components=0,
            eligible_amount_rub=None,
            allocated_amount_rub=None,
            reasons=[],
        )
    if not bool(control.get("cost_affecting")):
        return _projection(
            "not_applicable",
            eligible_documents=0,
            eligible_components=0,
            allocated_components=0,
            eligible_amount_rub=None,
            allocated_amount_rub=None,
            reasons=[],
        )

    eligible_components = int(control.get("eligible_component_count") or 0)
    allocated_components = int(control.get("allocated_component_count") or 0)
    eligible_amount = _optional_decimal(control.get("eligible_amount_rub"))
    allocated_amount = _optional_decimal(control.get("allocated_amount_rub"))
    reasons = _bounded_reasons(control.get("incomplete_reasons") or [])
    fully_allocated = bool(
        eligible_components > 0
        and allocated_components == eligible_components
        and bool(control.get("conserved"))
        and not reasons
    )
    status = (
        "all"
        if fully_allocated
        else "partial"
        if allocated_components > 0 or (allocated_amount is not None and allocated_amount > 0)
        else "none"
    )
    return _projection(
        status,
        eligible_documents=1,
        eligible_components=eligible_components,
        allocated_components=allocated_components,
        eligible_amount_rub=eligible_amount,
        allocated_amount_rub=allocated_amount,
        reasons=reasons,
    )


def project_supplier_order_expense_allocation(
    canonical_breakdown: Mapping[str, Any],
) -> dict[str, Any]:
    """Aggregate canonical document controls and retain the certification gate."""

    controls = [
        dict(item)
        for item in canonical_breakdown.get("document_controls") or []
        if isinstance(item, Mapping) and bool(item.get("cost_affecting"))
    ]
    eligible_documents = len(controls)
    eligible_components = sum(int(item.get("eligible_component_count") or 0) for item in controls)
    allocated_components = sum(int(item.get("allocated_component_count") or 0) for item in controls)
    eligible_amounts = [_optional_decimal(item.get("eligible_amount_rub")) for item in controls]
    allocated_amounts = [_optional_decimal(item.get("allocated_amount_rub")) for item in controls]
    eligible_amount = _sum_known(eligible_amounts)
    allocated_amount = _sum_known(allocated_amounts)
    certification = dict(canonical_breakdown.get("certification") or {})
    controls_summary = dict(canonical_breakdown.get("controls") or {})
    reasons: list[str] = []
    for item in controls:
        reasons.extend(_reason_texts(item.get("incomplete_reasons") or []))
    for blocker in canonical_breakdown.get("blockers") or []:
        reasons.extend(_reason_texts([blocker]))
    fingerprints_match = bool(
        certification.get("source_fingerprint_matches")
        and str(certification.get("source_fingerprint") or "")
        == str(certification.get("certified_source_fingerprint") or "")
        and str(certification.get("calculation_fingerprint") or "")
        == str(certification.get("certified_calculation_fingerprint") or "")
    )
    if eligible_documents and not bool(certification.get("certified")):
        reasons.append("Полнота расходов ещё не сертифицирована")
    if eligible_documents and not fingerprints_match:
        reasons.append("Текущие source/calculation fingerprints не совпадают с сертифицированными")
    conservation_ok = bool(
        controls_summary.get("document_allocation_conserved")
        and controls_summary.get("document_counted_once")
        and controls_summary.get("line_components_equal_capital")
        and controls_summary.get("shipment_lines_equal_capital")
    )
    if eligible_documents and not conservation_ok:
        reasons.append("Canonical conservation proof не пройден")
    documents_all = bool(
        eligible_documents
        and all(
            int(item.get("eligible_component_count") or 0) > 0
            and int(item.get("allocated_component_count") or 0)
            == int(item.get("eligible_component_count") or 0)
            and bool(item.get("conserved"))
            and not _reason_texts(item.get("incomplete_reasons") or [])
            for item in controls
        )
    )
    status = (
        "all"
        if documents_all
        and conservation_ok
        and fingerprints_match
        and bool(certification.get("certified"))
        and not list(canonical_breakdown.get("blockers") or [])
        else "partial"
        if allocated_components > 0 or (allocated_amount is not None and allocated_amount > 0)
        else "none"
    )
    return _projection(
        status,
        eligible_documents=eligible_documents,
        eligible_components=eligible_components,
        allocated_components=allocated_components,
        eligible_amount_rub=eligible_amount,
        allocated_amount_rub=allocated_amount,
        reasons=_bounded_reasons(reasons),
        extra={
            "certified": bool(certification.get("certified")),
            "fingerprints_match": fingerprints_match,
            "conservation_ok": conservation_ok,
        },
    )


def attach_supplier_document_expense_allocations(
    documents: Iterable[Mapping[str, Any]],
    canonical_breakdown: Mapping[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            **dict(document),
            "expense_allocation": project_supplier_document_expense_allocation(
                document,
                canonical_breakdown,
            ),
        }
        for document in documents
    ]


def _projection(
    status: str,
    *,
    eligible_documents: int,
    eligible_components: int,
    allocated_components: int,
    eligible_amount_rub: Decimal | None,
    allocated_amount_rub: Decimal | None,
    reasons: list[str],
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    unallocated_amount = None
    if eligible_amount_rub is not None and allocated_amount_rub is not None:
        unallocated_amount = max(eligible_amount_rub - allocated_amount_rub, Decimal("0"))
    return {
        "status": status,
        "label": STATUS_LABELS_RU[status],
        "diagnostics": {
            "eligible_documents": eligible_documents,
            "eligible_components": eligible_components,
            "allocated_components": allocated_components,
            "eligible_amount_rub": _decimal_text(eligible_amount_rub),
            "allocated_amount_rub": _decimal_text(allocated_amount_rub),
            "unallocated_amount_rub": _decimal_text(unallocated_amount),
            **dict(extra or {}),
        },
        "reasons": reasons,
    }


def _reason_texts(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, Mapping):
            text = str(value.get("reason_ru") or value.get("reason") or value.get("code") or "").strip()
        else:
            text = str(value or "").strip()
        if text:
            result.append(text)
    return result


def _bounded_reasons(values: Iterable[Any], *, limit: int = 8) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for text in _reason_texts(values):
        if text not in seen:
            seen.add(text)
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _optional_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _sum_known(values: Iterable[Decimal | None]) -> Decimal | None:
    parsed = [value for value in values if value is not None]
    return sum(parsed, Decimal("0")) if parsed else None


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text
