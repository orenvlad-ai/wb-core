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


HISTORICAL_STATUS_EXCEPTION_LEGACY_FF_ACCEPTED_WITHOUT_DATE = (
    "legacy_ff_accepted_without_date"
)
SUPPORTED_HISTORICAL_STATUS_EXCEPTIONS = frozenset(
    {HISTORICAL_STATUS_EXCEPTION_LEGACY_FF_ACCEPTED_WITHOUT_DATE}
)


@dataclass(frozen=True)
class SupplierShipmentStatusResolution:
    order_status: str
    status_label: str
    status_date: str
    status_display: str
    status_tooltip: str
    status_source: str
    status_exception: str
    status_date_known: bool
    business_today: str
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["warnings"] = list(self.warnings)
        return payload


@dataclass(frozen=True)
class SupplierShipmentDeadlineResolution:
    state: str
    tone: str
    days_until: int | None
    display: str
    display_zh: str
    display_en: str
    display_ru: str
    business_today: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
    historical_status_exception: Any = "",
) -> SupplierShipmentStatusResolution:
    """Resolve one status for write, read-model and UI consumers.

    Invalid or future legacy evidence is not treated as a completed factual
    boundary.  The row remains readable and carries diagnostics instead.
    """

    today = _strict_iso_date(business_today or current_business_date_iso(), "business_today")
    raw_shipment = str(actual_shipment_date or "").strip()
    raw_acceptance = str(actual_ff_acceptance_date or "").strip()
    exception = str(historical_status_exception or "").strip()
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
    supported_exception = exception in SUPPORTED_HISTORICAL_STATUS_EXCEPTIONS
    if exception and not supported_exception:
        warnings.append(f"unsupported historical_status_exception={exception}")
    exception_applies = bool(
        supported_exception and not raw_shipment and not raw_acceptance
    )
    if supported_exception and not exception_applies:
        warnings.append(
            "historical_status_exception is ignored because factual date evidence is present"
        )

    if acceptance:
        order_status = ORDER_STATUS_ACCEPTED_FF
        status_date = acceptance
        status_source = "actual_ff_acceptance_date"
    elif exception_applies:
        order_status = ORDER_STATUS_ACCEPTED_FF
        status_date = ""
        status_source = "historical_status_exception"
    elif shipment:
        order_status = ORDER_STATUS_IN_TRANSIT
        status_date = shipment
        status_source = "actual_shipment_date"
    else:
        order_status = ORDER_STATUS_PRODUCTION
        status_date = ""
        status_source = "no_occurred_factual_boundary"

    persisted = str(persisted_status or "").strip()
    if persisted and persisted != order_status:
        warnings.append(
            f"persisted order_status={persisted} differs from derived status={order_status}"
        )
    label = ORDER_STATUS_LABELS_RU[order_status]
    if exception_applies:
        display = f"{label} · дата неизвестна"
        tooltip = (
            "Исторический статус подтверждён без фактической даты; "
            "исключение не создаёт движение или капитал."
        )
    else:
        display = label if not status_date else f"{label} с {_format_ru_date(status_date)}"
        tooltip = "Статус вычислен из фактических дат"
    return SupplierShipmentStatusResolution(
        order_status=order_status,
        status_label=label,
        status_date=status_date,
        status_display=display,
        status_tooltip=tooltip,
        status_source=status_source,
        status_exception=exception if exception_applies else "",
        status_date_known=bool(status_date),
        business_today=today,
        warnings=tuple(warnings),
    )


def resolve_supplier_shipment_deadline(
    *,
    planned_shipment_date: Any,
    actual_shipment_date: Any,
    actual_ff_acceptance_date: Any,
    business_today: str | None = None,
    historical_status_exception: Any = "",
) -> SupplierShipmentDeadlineResolution:
    """Build the supplier-facing deadline badge from server-owned business time."""

    today = _strict_iso_date(business_today or current_business_date_iso(), "business_today")
    status = resolve_supplier_shipment_status(
        actual_shipment_date=actual_shipment_date,
        actual_ff_acceptance_date=actual_ff_acceptance_date,
        business_today=today,
        historical_status_exception=historical_status_exception,
    )
    if status.order_status in {ORDER_STATUS_IN_TRANSIT, ORDER_STATUS_ACCEPTED_FF}:
        return _deadline_resolution(
            state="shipped",
            tone="success",
            days_until=None,
            zh="已出货",
            en="Shipped",
            ru="Отгружено",
            business_today=today,
        )

    planned = str(planned_shipment_date or "").strip()
    try:
        planned_date = date.fromisoformat(planned)
    except ValueError:
        planned_date = None
    if planned_date is None or planned_date.isoformat() != planned:
        return _deadline_resolution(
            state="missing",
            tone="neutral",
            days_until=None,
            zh="—",
            en="—",
            ru="—",
            business_today=today,
        )

    days_until = (planned_date - date.fromisoformat(today)).days
    if days_until >= 10:
        return _deadline_resolution(
            state="safe",
            tone="green",
            days_until=days_until,
            zh=f"{days_until} 天",
            en=_english_days(days_until),
            ru=_russian_days(days_until),
            business_today=today,
        )
    if days_until > 0:
        return _deadline_resolution(
            state="warning",
            tone="yellow",
            days_until=days_until,
            zh=f"{days_until} 天",
            en=_english_days(days_until),
            ru=_russian_days(days_until),
            business_today=today,
        )
    if days_until == 0:
        return _deadline_resolution(
            state="today",
            tone="yellow",
            days_until=0,
            zh="今天",
            en="Today",
            ru="Сегодня",
            business_today=today,
        )

    overdue_days = abs(days_until)
    return _deadline_resolution(
        state="overdue",
        tone="red",
        days_until=days_until,
        zh=f"逾期 {overdue_days} 天",
        en=f"Overdue by {_english_days(overdue_days)}",
        ru=f"Просрочка {_russian_days(overdue_days)}",
        business_today=today,
    )


def _deadline_resolution(
    *,
    state: str,
    tone: str,
    days_until: int | None,
    zh: str,
    en: str,
    ru: str,
    business_today: str,
) -> SupplierShipmentDeadlineResolution:
    return SupplierShipmentDeadlineResolution(
        state=state,
        tone=tone,
        days_until=days_until,
        display=f"{zh} / {en} / {ru}" if zh != "—" else "—",
        display_zh=zh,
        display_en=en,
        display_ru=ru,
        business_today=business_today,
    )


def _english_days(value: int) -> str:
    return f"{value} {'day' if value == 1 else 'days'}"


def _russian_days(value: int) -> str:
    absolute = abs(int(value))
    last_two = absolute % 100
    last = absolute % 10
    if 11 <= last_two <= 14:
        suffix = "дней"
    elif last == 1:
        suffix = "день"
    elif last in {2, 3, 4}:
        suffix = "дня"
    else:
        suffix = "дней"
    return f"{absolute} {suffix}"


def validate_supplier_factual_dates(
    *,
    actual_shipment_date: Any,
    actual_ff_acceptance_date: Any,
    business_today: str | None = None,
    historical_status_exception: Any = "",
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
    exception = str(historical_status_exception or "").strip()
    if exception and exception not in SUPPORTED_HISTORICAL_STATUS_EXCEPTIONS:
        raise ValueError(f"unsupported historical_status_exception: {exception}")
    if exception and (shipment or acceptance):
        raise ValueError(
            "historical_status_exception requires empty factual shipment and FF acceptance dates"
        )
    return resolve_supplier_shipment_status(
        actual_shipment_date=shipment,
        actual_ff_acceptance_date=acceptance,
        business_today=today,
        historical_status_exception=exception,
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
        historical_status_exception=result.get("historical_status_exception"),
    )
    result["persisted_order_status"] = persisted
    result["order_status"] = resolution.order_status
    result["order_status_label"] = resolution.status_label
    result["order_status_date"] = resolution.status_date
    result["order_status_display"] = resolution.status_display
    result["order_status_tooltip"] = resolution.status_tooltip
    result["order_status_source"] = resolution.status_source
    result["order_status_exception"] = resolution.status_exception
    result["order_status_date_known"] = resolution.status_date_known
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
