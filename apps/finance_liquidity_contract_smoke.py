#!/usr/bin/env python3
"""Deterministic K01 smoke for the Finance Liquidity module/auth contract."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    WEB_AUTH_ROLE_ADMIN,
    WEB_AUTH_ROLE_OPERATOR,
    WEB_AUTH_ROLE_SUPPLIER,
    WEB_AUTH_ROLE_SUPPLY_OPERATOR,
    WEB_AUTH_SECTION_DEFINITIONS,
    _default_allowed_sections_for_role,
    _ensure_runtime_access_consistent,
    _env_principal_user_records,
    _normalize_public_allowed_sections,
    _user_has_section_access,
    _validate_runtime_allowed_sections,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    _SHEET_VITRINA_USER_SECTION_IDS,
    _default_sheet_vitrina_sections_for_role,
    _normalize_sheet_vitrina_user_sections,
)
from packages.contracts.finance_liquidity import (  # noqa: E402
    FINANCE_CAPABILITY_ADMIN,
    FINANCE_CAPABILITY_OPERATE,
    FINANCE_CAPABILITY_READ,
    FINANCE_LIQUIDITY_CAPABILITIES,
    FINANCE_LIQUIDITY_CAPABILITY_HIERARCHY,
    FINANCE_LIQUIDITY_CI_GROUP,
    FINANCE_LIQUIDITY_EXPLICIT_ONLY_CAPABILITIES,
    FINANCE_LIQUIDITY_MODULE_ID,
    expand_finance_capability_hierarchy,
    has_finance_capability,
)


def main() -> None:
    expected = ("finance", "finance_operate", "finance_admin")
    named_capabilities = (
        FINANCE_CAPABILITY_READ,
        FINANCE_CAPABILITY_OPERATE,
        FINANCE_CAPABILITY_ADMIN,
    )
    if named_capabilities != expected:
        raise AssertionError(named_capabilities)
    if FINANCE_LIQUIDITY_MODULE_ID != "finance_liquidity":
        raise AssertionError(FINANCE_LIQUIDITY_MODULE_ID)
    if FINANCE_LIQUIDITY_CI_GROUP != FINANCE_LIQUIDITY_MODULE_ID:
        raise AssertionError(FINANCE_LIQUIDITY_CI_GROUP)
    if FINANCE_LIQUIDITY_CAPABILITIES != expected:
        raise AssertionError(FINANCE_LIQUIDITY_CAPABILITIES)
    if FINANCE_LIQUIDITY_EXPLICIT_ONLY_CAPABILITIES != frozenset(expected):
        raise AssertionError(FINANCE_LIQUIDITY_EXPLICIT_ONLY_CAPABILITIES)
    if FINANCE_LIQUIDITY_CAPABILITY_HIERARCHY != {
        FINANCE_CAPABILITY_READ: (FINANCE_CAPABILITY_READ,),
        FINANCE_CAPABILITY_OPERATE: (
            FINANCE_CAPABILITY_READ,
            FINANCE_CAPABILITY_OPERATE,
        ),
        FINANCE_CAPABILITY_ADMIN: expected,
    }:
        raise AssertionError(FINANCE_LIQUIDITY_CAPABILITY_HIERARCHY)

    cases = (
        ([], ()),
        ([FINANCE_CAPABILITY_READ], (FINANCE_CAPABILITY_READ,)),
        (
            [FINANCE_CAPABILITY_OPERATE],
            (FINANCE_CAPABILITY_READ, FINANCE_CAPABILITY_OPERATE),
        ),
        ([FINANCE_CAPABILITY_ADMIN], expected),
        (
            ["reports", FINANCE_CAPABILITY_ADMIN, FINANCE_CAPABILITY_READ],
            ("reports", *expected),
        ),
    )
    for raw, wanted in cases:
        actual = expand_finance_capability_hierarchy(raw)
        if actual != wanted:
            raise AssertionError((raw, wanted, actual))

    if not has_finance_capability([FINANCE_CAPABILITY_ADMIN], FINANCE_CAPABILITY_READ):
        raise AssertionError("finance_admin must imply finance")
    if has_finance_capability([FINANCE_CAPABILITY_OPERATE], FINANCE_CAPABILITY_ADMIN):
        raise AssertionError("finance_operate must not imply finance_admin")

    definition_ids = tuple(
        str(item["section_id"])
        for item in WEB_AUTH_SECTION_DEFINITIONS
        if str(item["section_id"]) in FINANCE_LIQUIDITY_EXPLICIT_ONLY_CAPABILITIES
    )
    if definition_ids != expected:
        raise AssertionError(definition_ids)
    if tuple(
        item
        for item in _SHEET_VITRINA_USER_SECTION_IDS
        if item in FINANCE_LIQUIDITY_EXPLICIT_ONLY_CAPABILITIES
    ) != expected:
        raise AssertionError(_SHEET_VITRINA_USER_SECTION_IDS)

    for role in (
        WEB_AUTH_ROLE_ADMIN,
        WEB_AUTH_ROLE_OPERATOR,
        WEB_AUTH_ROLE_SUPPLY_OPERATOR,
        WEB_AUTH_ROLE_SUPPLIER,
        "",
    ):
        public_defaults = _default_allowed_sections_for_role(role)
        storage_defaults = _default_sheet_vitrina_sections_for_role(role)
        if FINANCE_LIQUIDITY_EXPLICIT_ONLY_CAPABILITIES.intersection(public_defaults):
            raise AssertionError((role, public_defaults))
        if FINANCE_LIQUIDITY_EXPLICIT_ONLY_CAPABILITIES.intersection(storage_defaults):
            raise AssertionError((role, storage_defaults))

    bootstrap = _env_principal_user_records(
        {
            "enabled": True,
            "operator": {"username": "bootstrap-owner", "display_name": "Owner"},
        }
    )
    if len(bootstrap) != 1 or bootstrap[0]["role"] != WEB_AUTH_ROLE_ADMIN:
        raise AssertionError(bootstrap)
    if FINANCE_LIQUIDITY_EXPLICIT_ONLY_CAPABILITIES.intersection(
        bootstrap[0]["allowed_sections"]
    ):
        raise AssertionError(bootstrap)

    for normalizer in (
        _normalize_public_allowed_sections,
        _normalize_sheet_vitrina_user_sections,
    ):
        for role in ("admin", "operator", "supply_operator"):
            for index, capability in enumerate(expected):
                wanted = ["reports", *expected[:index + 1]]
                for grants in (["reports", capability], f'["reports", "{capability}"]'):
                    actual = normalizer(grants, role=role)
                    if actual != wanted:
                        raise AssertionError((normalizer.__name__, role, actual))
            for raw in (None, "", "invalid-json", {}, [], "[]", "null"):
                defaults = normalizer(raw, role=role)
                if FINANCE_LIQUIDITY_EXPLICIT_ONLY_CAPABILITIES.intersection(defaults):
                    raise AssertionError((normalizer.__name__, role, raw, defaults))

    validated = _validate_runtime_allowed_sections(
        [FINANCE_CAPABILITY_OPERATE],
        role=WEB_AUTH_ROLE_OPERATOR,
    )
    if validated != [FINANCE_CAPABILITY_READ, FINANCE_CAPABILITY_OPERATE]:
        raise AssertionError(validated)
    for capability in expected:
        try:
            _ensure_runtime_access_consistent(
                WEB_AUTH_ROLE_SUPPLIER, [capability], False,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("supplier must not receive Finance capability")
        if _user_has_section_access(
            {"role": WEB_AUTH_ROLE_SUPPLIER, "allowed_sections": list(expected)},
            capability,
        ):
            raise AssertionError("supplier must be denied even with a stored grant")
        if not _user_has_section_access(
            {"role": WEB_AUTH_ROLE_OPERATOR, "allowed_sections": [FINANCE_CAPABILITY_ADMIN]},
            capability,
        ):
            raise AssertionError("explicit finance_admin must grant its hierarchy")

    print("finance_liquidity_contract_smoke: ok")


if __name__ == "__main__":
    main()
