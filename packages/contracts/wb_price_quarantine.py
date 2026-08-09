"""Conservative WB seller-price quarantine risk contract."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


MONEY = Decimal("0.01")
WB_QUARANTINE_RATIO = Decimal("1.5")
WB_QUARANTINE_DROP_PERCENT_LABEL = "33.3"
WB_QUARANTINE_WARNING_CODE = "quarantine_risk_discounted_price_drop_at_least_33_3_percent"


@dataclass(frozen=True)
class WbPriceQuarantineTransition:
    """Exact discounted-price transition evaluated in kopecks."""

    previous_discounted_kopecks: int
    next_discounted_kopecks: int
    risky: bool
    drop_percent: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "previous_discounted_price": _kopecks_to_json(self.previous_discounted_kopecks),
            "next_discounted_price": _kopecks_to_json(self.next_discounted_kopecks),
            "drop_percent": float(self.drop_percent),
            "threshold_ratio": float(WB_QUARANTINE_RATIO),
            "threshold_drop_percent": WB_QUARANTINE_DROP_PERCENT_LABEL,
            "inclusive": True,
            "risky": self.risky,
            "warning_code": WB_QUARANTINE_WARNING_CODE if self.risky else "",
        }


def evaluate_wb_price_quarantine_transition(
    previous_discounted: Any,
    next_discounted: Any,
) -> WbPriceQuarantineTransition:
    """Evaluate ``next * 1.5 <= previous`` after exact kopeck normalization."""

    previous_kopecks = money_to_kopecks(previous_discounted)
    next_kopecks = money_to_kopecks(next_discounted)
    if previous_kopecks <= 0 or next_kopecks <= 0:
        raise ValueError("discounted prices must be positive")
    risky = next_kopecks * 15 <= previous_kopecks * 10
    drop_percent = (
        (Decimal(previous_kopecks - next_kopecks) * Decimal("100"))
        / Decimal(previous_kopecks)
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return WbPriceQuarantineTransition(
        previous_discounted_kopecks=previous_kopecks,
        next_discounted_kopecks=next_kopecks,
        risky=risky,
        drop_percent=drop_percent,
    )


def money_to_kopecks(value: Any) -> int:
    if value is None or isinstance(value, bool):
        raise ValueError("money value must be numeric")
    try:
        amount = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("money value must be numeric") from exc
    if not amount.is_finite():
        raise ValueError("money value must be finite")
    normalized = amount.quantize(MONEY, rounding=ROUND_HALF_UP)
    return int(normalized * 100)


def _kopecks_to_json(value: int) -> int | float:
    amount = (Decimal(int(value)) / Decimal("100")).quantize(MONEY)
    integral = amount.to_integral_value()
    return int(integral) if amount == integral else float(amount)
