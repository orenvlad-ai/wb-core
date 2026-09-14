"""Public, account-scoped contracts for the keyword cleaner (no WB transport)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping, Protocol

RULES_VERSION = "1.0.0"
COVERAGE_NOTICE = "Проверены ключи, доступные через WB API; полный охват WB не подтверждён"
# Only identities present in the approved, restored profile corpus. A future
# catalogue extension is a code/rule change, never an inferred numeric range.
MODEL_CATALOG = frozenset({
    "13", "13 pro", "14", "14 pro", "14 promax", "15", "15 pro", "15 promax",
    "16", "16 e", "16 pro", "16 promax", "17", "17 e", "17 pro", "17 promax",
    "18 pro", "18 promax", "air",
})


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def query_hash(query: str) -> str:
    if not isinstance(query, str) or not query or len(query) > 2000 or any(ord(c) < 32 for c in query):
        raise CleanerError("invalid_query", "Некорректная точная строка кластера")
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class CleanerError(ValueError):
    def __init__(self, code: str, message: str, http_status: int = 422):
        super().__init__(message)
        self.code, self.http_status = code, http_status


@dataclass(frozen=True)
class Account:
    seller_id: str
    account_scope: str

    @property
    def key(self) -> str:
        if not self.seller_id or not self.account_scope:
            raise CleanerError("invalid_account", "Нет привязки рекламного аккаунта")
        return digest([self.seller_id, self.account_scope])


@dataclass(frozen=True)
class Principal:
    username: str
    authenticated: bool = False
    auth_enabled: bool = False
    ads_access: bool = False

    def require_read(self) -> None:
        if not self.authenticated or not self.auth_enabled or not self.ads_access:
            raise CleanerError("forbidden", "Нет доступа к рекламе", 403)

    def require_owner(self, owner_username: str) -> None:
        self.require_read()
        if not owner_username.strip() or self.username.strip().casefold() != owner_username.strip().casefold():
            raise CleanerError("owner_required", "Изменения доступны владельцу", 403)


@dataclass(frozen=True)
class Profile:
    nm_id: int
    version: int
    category: str
    models: tuple[str, ...]
    kind: str
    frame: str
    source: str
    verified_at: str

    @property
    def semantic_fingerprint(self) -> str:
        return digest([self.category, sorted(set(self.models)), self.kind, self.frame])

    def as_dict(self) -> dict[str, Any]:
        return dict(nm_id=self.nm_id, version=self.version, category=self.category, models=list(self.models), kind=self.kind, frame=self.frame, source=self.source, verified_at=self.verified_at, semantic_fingerprint=self.semantic_fingerprint)

    @classmethod
    def parse(cls, value: Mapping[str, Any]) -> Profile:
        try:
            nm, version = value["nm_id"], value["version"]
            models = value["models"]
            if type(nm) is not int or nm <= 0 or type(version) is not int or version <= 0:
                raise ValueError("identity")
            if not isinstance(models, (list, tuple)) or not models or any(m not in MODEL_CATALOG for m in models):
                raise ValueError("models")
            if value["category"] != "phone_screen_glass" or value["kind"] not in {"clean", "matte", "anti"} or value["frame"] not in {"black", "none"}:
                raise ValueError("semantics")
            if not isinstance(value["source"], str) or not value["source"].strip() or not isinstance(value["verified_at"], str) or not value["verified_at"].strip():
                raise ValueError("provenance")
            parsed = datetime.fromisoformat(value["verified_at"].replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("date timezone")
            return cls(nm, version, value["category"], tuple(sorted(set(models))), value["kind"], value["frame"], value["source"], value["verified_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CleanerError("profile_required", "Нужен подтверждённый профиль поддерживаемого товара") from exc


@dataclass(frozen=True)
class Target:
    advert_id: int
    nm_id: int
    payment_type: str = "cpm"
    bid_type: str = "manual"
    status: int = 9
    name: str = ""
    contract_verified: bool = False

    @property
    def key(self) -> str:
        if type(self.advert_id) is not int or self.advert_id <= 0 or type(self.nm_id) is not int or self.nm_id <= 0:
            raise CleanerError("invalid_target", "Некорректная пара кампания/артикул")
        return f"{self.advert_id}:{self.nm_id}"

    @property
    def unsupported_reason(self) -> str:
        if self.payment_type != "cpm": return "payment_type_not_cpm"
        if self.status not in {9, 11}: return "campaign_unavailable"
        if self.bid_type != "manual" or not self.contract_verified: return "contract_not_verified"
        return ""


@dataclass(frozen=True)
class Snapshot:
    """Validated per-target source union; complete means response contract, not all WB traffic."""
    target: Target
    observed_at: str
    queries: Mapping[str, str]
    minus: tuple[str, ...]
    sources: Mapping[str, tuple[str, ...]]
    complete: bool
    reasons: tuple[str, ...] = ()
    source_times: Mapping[str, str] = field(default_factory=dict)
    coverage: str = COVERAGE_NOTICE


class ReadSource(Protocol):
    """Worker-only port. Implementations must bound each attempt (120 s / <=3 reads)."""
    def catalog(self) -> tuple[list[Target], list[str]]: ...
    def snapshot(self, target: Target) -> Snapshot: ...
