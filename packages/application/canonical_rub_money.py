"""Central canonical RUB minor-unit boundary for operational parity gates.

Authoritative accounting rows retain their exact Decimal text.  Operational
comparisons which gate posting, lifecycle drains, or publication use the same
ROUND_HALF_UP kopeck boundary as guided supplier acceptance.  The exact raw
residual remains diagnostic evidence and is never written back to either side.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from typing import Any


CANONICAL_RUB_MONEY_POLICY = "rub_minor_unit_round_half_up_v1"
RUB_MINOR_UNIT = Decimal("0.01")
RUB_MINOR_SCALE = 100
RUB_DECIMAL_PRECISION = 160
ZERO = Decimal("0")


@dataclass(frozen=True)
class CanonicalRubMoneyComparison:
    left_exact_rub: Decimal
    right_exact_rub: Decimal
    left_minor_units: int
    right_minor_units: int
    raw_residual_rub: Decimal
    canonical_equal: bool
    residual_attributable: bool
    policy: str = CANONICAL_RUB_MONEY_POLICY


def exact_rub_decimal(value: Any, *, field: str) -> Decimal:
    """Parse finite exact RUB text without applying a business rounding step."""

    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be finite Decimal-safe RUB") from exc
    if not amount.is_finite():
        raise ValueError(f"{field} must be finite Decimal-safe RUB")
    return amount


def canonical_rub_minor_units(value: Any, *, field: str) -> int:
    """Return deterministic signed kopecks using the document posting policy."""

    amount = exact_rub_decimal(value, field=field)
    try:
        with localcontext() as context:
            context.prec = RUB_DECIMAL_PRECISION
            canonical = amount.quantize(RUB_MINOR_UNIT, rounding=ROUND_HALF_UP)
            return int(canonical * RUB_MINOR_SCALE)
    except (InvalidOperation, OverflowError, ValueError) as exc:
        raise ValueError(f"{field} is outside the canonical RUB boundary") from exc


def rub_from_minor_units(value: int) -> Decimal:
    """Convert signed canonical kopecks back to exact Decimal RUB."""

    with localcontext() as context:
        context.prec = RUB_DECIMAL_PRECISION
        return Decimal(int(value)) / Decimal(RUB_MINOR_SCALE)


def compare_canonical_rub_money(
    left: Any,
    right: Any,
    *,
    left_field: str,
    right_field: str,
) -> CanonicalRubMoneyComparison:
    """Compare exact values at the canonical kopeck boundary.

    Equal canonical values may retain different sub-kopeck tails.  Such a raw
    residual is attributable only while it stays strictly inside one minor
    unit; a crossed boundary is never downgraded to a diagnostic.
    """

    left_exact = exact_rub_decimal(left, field=left_field)
    right_exact = exact_rub_decimal(right, field=right_field)
    left_minor_units = canonical_rub_minor_units(left_exact, field=left_field)
    right_minor_units = canonical_rub_minor_units(right_exact, field=right_field)
    with localcontext() as context:
        context.prec = RUB_DECIMAL_PRECISION
        residual = left_exact - right_exact
        residual_attributable = abs(residual) < RUB_MINOR_UNIT
    canonical_equal = left_minor_units == right_minor_units
    return CanonicalRubMoneyComparison(
        left_exact_rub=left_exact,
        right_exact_rub=right_exact,
        left_minor_units=left_minor_units,
        right_minor_units=right_minor_units,
        raw_residual_rub=residual,
        canonical_equal=canonical_equal,
        residual_attributable=bool(canonical_equal and residual_attributable),
    )
