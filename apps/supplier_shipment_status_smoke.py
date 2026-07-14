"""Domain checks for the single server-owned supplier shipment status resolver."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.supplier_shipment_status import (  # noqa: E402
    HISTORICAL_STATUS_EXCEPTION_LEGACY_FF_ACCEPTED_WITHOUT_DATE,
    resolve_supplier_shipment_status,
    validate_supplier_factual_dates,
)


TODAY = "2026-07-14"


def main() -> int:
    production = resolve_supplier_shipment_status(
        actual_shipment_date="",
        actual_ff_acceptance_date="",
        business_today=TODAY,
        persisted_status="in_transit",
    )
    _equal(production.order_status, "production", "empty factual dates")
    _equal(production.status_display, "На производстве", "production display")
    _assert(production.warnings, "stale persisted cache warning")

    transit = validate_supplier_factual_dates(
        actual_shipment_date=TODAY,
        actual_ff_acceptance_date="",
        business_today=TODAY,
    )
    _equal(transit.order_status, "in_transit", "shipment today")
    _equal(transit.status_display, "В пути с 14.07.2026", "transit display")

    accepted = validate_supplier_factual_dates(
        actual_shipment_date="2026-06-25",
        actual_ff_acceptance_date="2026-07-01",
        business_today=TODAY,
    )
    _equal(accepted.order_status, "accepted_ff", "acceptance wins")
    _equal(accepted.status_display, "Принято на ФФ с 01.07.2026", "acceptance display")

    accepted_without_shipment = validate_supplier_factual_dates(
        actual_shipment_date="",
        actual_ff_acceptance_date="2026-07-01",
        business_today=TODAY,
    )
    _equal(accepted_without_shipment.order_status, "accepted_ff", "cleared shipment keeps accepted")

    historical = validate_supplier_factual_dates(
        actual_shipment_date="",
        actual_ff_acceptance_date="",
        business_today=TODAY,
        historical_status_exception=(
            HISTORICAL_STATUS_EXCEPTION_LEGACY_FF_ACCEPTED_WITHOUT_DATE
        ),
    )
    _equal(historical.order_status, "accepted_ff", "historical terminal signal")
    _equal(
        historical.status_display,
        "Принято на ФФ · дата неизвестна",
        "historical unknown-date display",
    )
    _equal(historical.status_date, "", "historical signal does not fabricate date")
    _assert(not historical.status_date_known, "historical date remains unknown")

    factual_precedence = resolve_supplier_shipment_status(
        actual_shipment_date="2026-06-25",
        actual_ff_acceptance_date="2026-07-01",
        business_today=TODAY,
        historical_status_exception=(
            HISTORICAL_STATUS_EXCEPTION_LEGACY_FF_ACCEPTED_WITHOUT_DATE
        ),
    )
    _equal(
        factual_precedence.status_source,
        "actual_ff_acceptance_date",
        "factual acceptance takes precedence over stale exception",
    )
    _assert(factual_precedence.warnings, "conflicting legacy state fails closed with warning")

    _rejects(
        lambda: validate_supplier_factual_dates(
            actual_shipment_date="2026-07-15",
            actual_ff_acceptance_date="",
            business_today=TODAY,
        ),
        "Фактическая дата отгрузки",
    )
    _rejects(
        lambda: validate_supplier_factual_dates(
            actual_shipment_date="2026-06-25",
            actual_ff_acceptance_date="2026-07-15",
            business_today=TODAY,
        ),
        "Фактическая дата приёмки",
    )
    _rejects(
        lambda: validate_supplier_factual_dates(
            actual_shipment_date="2026-07-02",
            actual_ff_acceptance_date="2026-07-01",
            business_today=TODAY,
        ),
        "раньше",
    )
    _rejects(
        lambda: validate_supplier_factual_dates(
            actual_shipment_date="14.07.2026",
            actual_ff_acceptance_date="",
            business_today=TODAY,
        ),
        "ISO",
    )
    _rejects(
        lambda: validate_supplier_factual_dates(
            actual_shipment_date="2026-06-25",
            actual_ff_acceptance_date="",
            business_today=TODAY,
            historical_status_exception=(
                HISTORICAL_STATUS_EXCEPTION_LEGACY_FF_ACCEPTED_WITHOUT_DATE
            ),
        ),
        "requires empty factual",
    )

    legacy = resolve_supplier_shipment_status(
        actual_shipment_date="2026-07-25",
        actual_ff_acceptance_date="not-a-date",
        business_today=TODAY,
        persisted_status="accepted_ff",
    )
    _equal(legacy.order_status, "production", "future/invalid legacy evidence ignored")
    _assert(len(legacy.warnings) == 3, "legacy diagnostics include both dates and cache mismatch")
    print("supplier_shipment_status_smoke: ok")
    return 0


def _rejects(callback, expected: str) -> None:
    try:
        callback()
    except ValueError as exc:
        _assert(expected in str(exc), f"expected validation text {expected!r}, got {exc!s}")
    else:
        raise AssertionError(f"expected validation failure containing {expected!r}")


def _equal(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def _assert(condition: object, label: str) -> None:
    if not condition:
        raise AssertionError(label)


if __name__ == "__main__":
    raise SystemExit(main())
