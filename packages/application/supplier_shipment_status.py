"""Canonical supplier-shipment status derived from factual business dates."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any

from packages.business_time import business_date_from_timestamp, current_business_date_iso
from packages.contracts.supplier_shipments import (
    ORDER_STATUS_ACCEPTED_FF,
    ORDER_STATUS_IN_TRANSIT,
    ORDER_STATUS_LABELS_RU,
    ORDER_STATUS_PRODUCTION,
)


@dataclass(frozen=True)
class SupplierShipmentStatusResolution:
    order_status: str
    status_label: str
    status_date: str
    status_display: str
    business_today: str
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["warnings"] = list(self.warnings)
        return payload


def supplier_business_today(*, timestamp: str | None = None, now: datetime | None = None) -> str:
    if timestamp:
        return business_date_from_timestamp(timestamp)
    return current_business_date_iso(now)


def resolve_supplier_shipment_status(
    *,
    actual_shipment_date: Any,
    actual_ff_acceptance_date: Any,
    business_today: str | None = None,
    persisted_status: Any = "",
) -> SupplierShipmentStatusResolution:
    """Resolve one status for write, read-model and UI consumers.

    Invalid or future legacy evidence is not treated as a completed factual
    boundary.  The row remains readable and carries diagnostics instead.
    """

    today = _strict_iso_date(business_today or current_business_date_iso(), "business_today")
    shipment, shipment_warning = _legacy_factual_date(
        actual_shipment_date,
        field_name="actual_shipment_date",
        business_today=today,
    )
    acceptance, acceptance_warning = _legacy_factual_date(
        actual_ff_acceptance_date,
        field_name="actual_ff_acceptance_date",
        business_today=today,
    )
    warnings = [item for item in (shipment_warning, acceptance_warning) if item]
    if shipment and acceptance and acceptance < shipment:
        warnings.append("actual_ff_acceptance_date is earlier than actual_shipment_date")

    if acceptance:
        order_status = ORDER_STATUS_ACCEPTED_FF
        status_date = acceptance
    elif shipment:
        order_status = ORDER_STATUS_IN_TRANSIT
        status_date = shipment
    else:
        order_status = ORDER_STATUS_PRODUCTION
        status_date = ""

    persisted = str(persisted_status or "").strip()
    if persisted and persisted != order_status:
        warnings.append(
            f"persisted order_status={persisted} differs from derived status={order_status}"
        )
    label = ORDER_STATUS_LABELS_RU[order_status]
    display = label if not status_date else f"{label} с {_format_ru_date(status_date)}"
    return SupplierShipmentStatusResolution(
        order_status=order_status,
        status_label=label,
        status_date=status_date,
        status_display=display,
        business_today=today,
        warnings=tuple(warnings),
    )


def validate_supplier_factual_dates(
    *,
    actual_shipment_date: Any,
    actual_ff_acceptance_date: Any,
    business_today: str | None = None,
) -> SupplierShipmentStatusResolution:
    """Validate factual dates at the application/API boundary and resolve status."""

    today = _strict_iso_date(business_today or current_business_date_iso(), "business_today")
    shipment = _optional_strict_iso_date(actual_shipment_date, "actual_shipment_date")
    acceptance = _optional_strict_iso_date(actual_ff_acceptance_date, "actual_ff_acceptance_date")
    if shipment and shipment > today:
        raise ValueError(
            f"Фактическая дата отгрузки не может быть позже business today {today}."
        )
    if acceptance and acceptance > today:
        raise ValueError(
            f"Фактическая дата приёмки на ФФ не может быть позже business today {today}."
        )
    if shipment and acceptance and acceptance < shipment:
        raise ValueError(
            "Фактическая дата приёмки на ФФ не может быть раньше фактической даты отгрузки."
        )
    return resolve_supplier_shipment_status(
        actual_shipment_date=shipment,
        actual_ff_acceptance_date=acceptance,
        business_today=today,
    )


def apply_derived_supplier_status(
    payload: dict[str, Any],
    *,
    business_today: str | None = None,
) -> dict[str, Any]:
    """Return a read model whose status is derived, retaining cache diagnostics."""

    result = dict(payload)
    persisted = str(result.get("order_status") or "")
    resolution = resolve_supplier_shipment_status(
        actual_shipment_date=result.get("actual_shipment_date"),
        actual_ff_acceptance_date=result.get("actual_ff_acceptance_date"),
        business_today=business_today,
        persisted_status=persisted,
    )
    result["persisted_order_status"] = persisted
    result["order_status"] = resolution.order_status
    result["order_status_label"] = resolution.status_label
    result["order_status_date"] = resolution.status_date
    result["order_status_display"] = resolution.status_display
    result["order_status_business_today"] = resolution.business_today
    result["order_status_warnings"] = list(resolution.warnings)
    return result


def _legacy_factual_date(value: Any, *, field_name: str, business_today: str) -> tuple[str, str]:
    normalized = str(value or "").strip()
    if not normalized:
        return "", ""
    try:
        parsed = _strict_iso_date(normalized, field_name)
    except ValueError:
        return "", f"{field_name} is not a valid ISO date"
    if parsed > business_today:
        return "", f"{field_name}={parsed} is later than business_today={business_today}"
    return parsed, ""


def _optional_strict_iso_date(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    return _strict_iso_date(normalized, field_name)


def _strict_iso_date(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    try:
        parsed = date.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO date YYYY-MM-DD") from exc
    if parsed.isoformat() != normalized:
        raise ValueError(f"{field_name} must be an ISO date YYYY-MM-DD")
    return normalized


def _format_ru_date(value: str) -> str:
    parsed = date.fromisoformat(value)
    return f"{parsed.day:02d}.{parsed.month:02d}.{parsed.year:04d}"
