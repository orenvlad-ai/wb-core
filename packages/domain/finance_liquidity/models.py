"""Exact money and stable-value helpers for the Finance cash slice."""

from __future__ import annotations

import re

MAX_MONEY_MINOR = 9_000_000_000_000_000

# The cash slice exposes RUB first, but an account owns its currency and scale.
# These are ledger values only; they do not enable FX, bank imports, or CNY flows.
CURRENCY_MINOR_UNIT_EXPONENTS = {
    "RUB": 2,
    "USD": 2,
    "EUR": 2,
    "CNY": 2,
    "JPY": 0,
}
# Kept as a compatibility name for callers that previously imported it.
SUPPORTED_CURRENCIES = CURRENCY_MINOR_UNIT_EXPONENTS


class MoneyError(ValueError):
    pass


def money_from_api(value: object, currency: str) -> int:
    """Parse a major-unit JSON string with no float or silent rounding."""

    if not isinstance(value, str):
        raise MoneyError("money must be a decimal string")
    code = str(currency or "").strip().upper()
    exponent = CURRENCY_MINOR_UNIT_EXPONENTS.get(code)
    if exponent is None:
        raise MoneyError("unsupported currency")
    text = value.strip()
    if not text or "e" in text.lower():
        raise MoneyError("money must be a plain decimal string")
    matched = re.fullmatch(r"([+-]?)(\d+)(?:\.(\d+))?", text)
    if matched is None:
        raise MoneyError("invalid money")
    sign, whole, fraction = matched.groups()
    fraction = fraction or ""
    if len(fraction) > exponent:
        raise MoneyError(f"money supports at most {exponent} fractional digits")
    minor = int(whole) * (10**exponent) + int(
        fraction.ljust(exponent, "0") if exponent else "0"
    )
    if sign == "-":
        minor = -minor
    if abs(minor) > MAX_MONEY_MINOR:
        raise MoneyError("money exceeds supported range")
    return minor


def money_to_api(minor: int, currency: str) -> str:
    exponent = CURRENCY_MINOR_UNIT_EXPONENTS.get(str(currency or "").strip().upper())
    if exponent is None or abs(int(minor)) > MAX_MONEY_MINOR:
        raise MoneyError("invalid stored money")
    sign = "-" if int(minor) < 0 else ""
    absolute = abs(int(minor))
    scale = 10**exponent
    if exponent == 0:
        return f"{sign}{absolute}"
    return f"{sign}{absolute // scale}.{absolute % scale:0{exponent}d}"
