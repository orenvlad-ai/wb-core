"""Stable module and authorization contract for Finance Liquidity.

This module deliberately contains no storage, HTTP, UI, or posting logic.  It
only names the future treasury module and defines its explicit-only capability
hierarchy so every later adapter uses the same vocabulary.
"""

from __future__ import annotations

from collections.abc import Iterable


FINANCE_LIQUIDITY_MODULE_ID = "finance_liquidity"
FINANCE_LIQUIDITY_CI_GROUP = "finance_liquidity"

FINANCE_CAPABILITY_READ = "finance"
FINANCE_CAPABILITY_OPERATE = "finance_operate"
FINANCE_CAPABILITY_ADMIN = "finance_admin"

FINANCE_LIQUIDITY_CAPABILITIES = (
    FINANCE_CAPABILITY_READ,
    FINANCE_CAPABILITY_OPERATE,
    FINANCE_CAPABILITY_ADMIN,
)
FINANCE_LIQUIDITY_EXPLICIT_ONLY_CAPABILITIES = frozenset(
    FINANCE_LIQUIDITY_CAPABILITIES
)
FINANCE_LIQUIDITY_CAPABILITY_DEFINITIONS = (
    (FINANCE_CAPABILITY_READ, "Финансы: просмотр"),
    (FINANCE_CAPABILITY_OPERATE, "Финансы: операции"),
    (FINANCE_CAPABILITY_ADMIN, "Финансы: администрирование"),
)
FINANCE_LIQUIDITY_CAPABILITY_HIERARCHY = {
    FINANCE_CAPABILITY_READ: (FINANCE_CAPABILITY_READ,),
    FINANCE_CAPABILITY_OPERATE: (
        FINANCE_CAPABILITY_READ,
        FINANCE_CAPABILITY_OPERATE,
    ),
    FINANCE_CAPABILITY_ADMIN: FINANCE_LIQUIDITY_CAPABILITIES,
}


def expand_finance_capability_hierarchy(values: Iterable[object]) -> tuple[str, ...]:
    """Return stable unique grants with explicitly requested Finance closure.

    No Finance grant is added when the input contains no Finance capability.
    When a higher capability is explicitly present, its lower capabilities are
    emitted once at the position of the first Finance grant.
    """

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        normalized.append(item)

    implied: set[str] = set()
    for item in normalized:
        implied.update(FINANCE_LIQUIDITY_CAPABILITY_HIERARCHY.get(item, ()))
    if not implied:
        return tuple(normalized)

    expanded: list[str] = []
    finance_emitted = False
    for item in normalized:
        if item in FINANCE_LIQUIDITY_EXPLICIT_ONLY_CAPABILITIES:
            if not finance_emitted:
                expanded.extend(
                    capability
                    for capability in FINANCE_LIQUIDITY_CAPABILITIES
                    if capability in implied
                )
                finance_emitted = True
            continue
        expanded.append(item)
    return tuple(expanded)


def without_finance_explicit_only_capabilities(
    values: Iterable[object],
) -> tuple[str, ...]:
    """Remove Finance grants from role/default expansion paths."""

    return tuple(
        item
        for item in expand_finance_capability_hierarchy(values)
        if item not in FINANCE_LIQUIDITY_EXPLICIT_ONLY_CAPABILITIES
    )


def has_finance_capability(values: Iterable[object], required: str) -> bool:
    """Evaluate the canonical hierarchy without role-based fallback."""

    if required not in FINANCE_LIQUIDITY_EXPLICIT_ONLY_CAPABILITIES:
        raise ValueError(f"unknown Finance capability: {required}")
    return required in expand_finance_capability_hierarchy(values)
