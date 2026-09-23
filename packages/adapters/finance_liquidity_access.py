"""Shared explicit access configuration for the isolated Finance sidecar."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Mapping

from packages.contracts.finance_liquidity import (
    FINANCE_LIQUIDITY_EXPLICIT_ONLY_CAPABILITIES,
    expand_finance_capability_hierarchy,
)


FINANCE_LIQUIDITY_ACCESS_CONFIG_ENV = "FINANCE_LIQUIDITY_ACCESS_CONFIG"
FINANCE_LIQUIDITY_ACCESS_CONTRACT = "finance_liquidity_bootstrap_access_v1"


class FinanceBootstrapAccessUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class FinanceBootstrapAccess:
    username: str
    capability: str
    instance_label: str
    store_id: str
    store_path: Path
    mode: str

    @property
    def capabilities(self) -> tuple[str, ...]:
        return tuple(
            item
            for item in expand_finance_capability_hierarchy([self.capability])
            if item in FINANCE_LIQUIDITY_EXPLICIT_ONLY_CAPABILITIES
        )


def normalize_finance_bootstrap_username(value: object) -> str:
    return str(value or "").strip().lower()


def validate_finance_bootstrap_store(
    db_path: Path,
    access: FinanceBootstrapAccess,
    *,
    require_existing: bool,
) -> None:
    if not db_path.is_absolute() or db_path != access.store_path:
        raise FinanceBootstrapAccessUnavailable(
            "Finance database does not match access config store binding"
        )
    try:
        resolved = db_path.resolve(strict=require_existing)
    except OSError as exc:
        raise FinanceBootstrapAccessUnavailable(
            "Finance access-configured database is unavailable"
        ) from exc
    if resolved != db_path or db_path.is_symlink():
        raise FinanceBootstrapAccessUnavailable(
            "Finance access-configured database must not use an alias"
        )
    if require_existing and not db_path.is_file():
        raise FinanceBootstrapAccessUnavailable(
            "Finance access-configured database is unavailable"
        )


def load_finance_bootstrap_access(
    environment: Mapping[str, str] | None = None,
) -> FinanceBootstrapAccess | None:
    """Read one repo-owned, non-secret grant file on every authorization check.

    Missing configuration means no bootstrap grant. Once a path is configured,
    malformed or unreadable content is an availability failure rather than a
    role-based fallback.
    """

    source = os.environ if environment is None else environment
    raw_path = str(source.get(FINANCE_LIQUIDITY_ACCESS_CONFIG_ENV) or "").strip()
    if not raw_path:
        return None
    path = Path(raw_path)
    if not path.is_absolute():
        raise FinanceBootstrapAccessUnavailable(
            "Finance bootstrap access config path must be absolute"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FinanceBootstrapAccessUnavailable(
            "Finance bootstrap access config is unavailable"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {
        "contract_version",
        "enabled",
        "username",
        "capability",
        "instance_label",
        "store_id",
        "store_path",
        "mode",
    }:
        raise FinanceBootstrapAccessUnavailable(
            "Finance bootstrap access config has invalid shape"
        )
    if payload.get("contract_version") != FINANCE_LIQUIDITY_ACCESS_CONTRACT:
        raise FinanceBootstrapAccessUnavailable(
            "Finance bootstrap access config has invalid contract"
        )
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        raise FinanceBootstrapAccessUnavailable(
            "Finance bootstrap access config has invalid enabled flag"
        )
    if not enabled:
        return None
    username = normalize_finance_bootstrap_username(payload.get("username"))
    capability = str(payload.get("capability") or "").strip()
    instance_label = str(payload.get("instance_label") or "").strip()
    store_id = str(payload.get("store_id") or "").strip()
    store_path = Path(str(payload.get("store_path") or "").strip())
    mode = str(payload.get("mode") or "").strip()
    if not username or capability not in FINANCE_LIQUIDITY_EXPLICIT_ONLY_CAPABILITIES:
        raise FinanceBootstrapAccessUnavailable(
            "Finance bootstrap access config has invalid grant"
        )
    if not instance_label or len(instance_label) > 80:
        raise FinanceBootstrapAccessUnavailable(
            "Finance bootstrap access config has invalid instance label"
        )
    if not store_id or len(store_id) > 80 or not store_path.is_absolute():
        raise FinanceBootstrapAccessUnavailable(
            "Finance bootstrap access config has invalid store binding"
        )
    if mode != "isolated_test":
        raise FinanceBootstrapAccessUnavailable(
            "Finance bootstrap access config has invalid mode"
        )
    return FinanceBootstrapAccess(
        username=username,
        capability=capability,
        instance_label=instance_label,
        store_id=store_id,
        store_path=store_path,
        mode=mode,
    )
