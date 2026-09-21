"""Pinned, query-only authorization seam for the optional Finance process."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Mapping

from packages.application.storage_registry import StoreRegistry, StorageRegistryError
from packages.contracts.finance_liquidity import expand_finance_capability_hierarchy


class FinanceAuthUnavailable(ValueError):
    pass


class FinanceAuthDenied(ValueError):
    pass


def _decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _cookie(headers: Mapping[str, str], name: str) -> str:
    for part in str(headers.get("Cookie") or "").split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == name:
            return value
    return ""


def _identity(path: Path) -> tuple[int, int, int]:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_ctime_ns)


class FinanceOperationalAuth:
    """Checks the normal signed session, then exact active grants from current store.

    It intentionally never calls the main runtime's user loader because that
    loader can initialise schema/write.  Pin/revalidation rejects manifest or
    same-path file drift, and query-only is set by StoreRegistry.connect. SQLite
    exposes only the opened pathname here, so this is deliberately a path-identity
    guard, not an fstat-level proof of the opened descriptor identity.
    """

    def __init__(self, runtime_dir: Path, session_secret: str | None = None) -> None:
        self.registry = StoreRegistry(Path(runtime_dir))
        self.session_secret = (
            session_secret
            if session_secret is not None
            else str(os.environ.get("WB_CORE_WEB_AUTH_SESSION_SECRET") or "")
        )

    def authenticate(self, headers: Mapping[str, str]) -> dict[str, Any]:
        if not self.session_secret:
            raise FinanceAuthUnavailable("session secret unavailable")
        raw = _cookie(headers, "wb_core_web_session")
        if not raw or "." not in raw:
            raise FinanceAuthDenied("authentication required")
        payload_b64, signature = raw.rsplit(".", 1)
        expected = (
            base64.urlsafe_b64encode(
                hmac.new(
                    self.session_secret.encode(),
                    payload_b64.encode("ascii"),
                    hashlib.sha256,
                ).digest()
            )
            .rstrip(b"=")
            .decode()
        )
        if not hmac.compare_digest(expected, signature):
            raise FinanceAuthDenied("invalid session")
        try:
            payload = json.loads(_decode(payload_b64).decode("utf-8"))
            username, role, expires = (
                str(payload["u"]),
                str(payload["r"]),
                int(payload["exp"]),
            )
        except (KeyError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FinanceAuthDenied("invalid session") from exc
        if expires < int(time.time()) or role == "supplier":
            raise FinanceAuthDenied("expired or forbidden session")
        try:
            pinned = self.registry.load(require_files=True)
            path = self.registry.resolve("operational", manifest=pinned)
            expected_path = path.resolve()
            before_identity = _identity(expected_path)
            with self.registry.connect(
                "operational",
                mode="ro",
                operation="finance_liquidity_authorization",
                manifest=pinned,
            ) as conn:
                database_path = Path(
                    conn.execute("PRAGMA database_list").fetchone()[2]
                ).resolve()
                if (
                    database_path != expected_path
                    or _identity(database_path) != before_identity
                ):
                    raise FinanceAuthUnavailable("operational authorization path drift")
                row = conn.execute(
                    "SELECT username,role,allowed_sections_json,is_active FROM sheet_vitrina_v1_users WHERE username=?",
                    (username.strip().lower(),),
                ).fetchone()
            after_identity = _identity(expected_path)
            revalidated = self.registry.load(require_files=True)
        except (StorageRegistryError, sqlite3.DatabaseError, OSError) as exc:
            raise FinanceAuthUnavailable(
                "operational authorization store unavailable"
            ) from exc
        if revalidated != pinned or before_identity != after_identity:
            raise FinanceAuthUnavailable("operational authorization generation drift")
        if row is None or not bool(row["is_active"]) or str(row["role"]) != role:
            raise FinanceAuthDenied("active session principal not found")
        try:
            grants = json.loads(str(row["allowed_sections_json"]))
        except json.JSONDecodeError as exc:
            raise FinanceAuthUnavailable("stored grants are corrupt") from exc
        capabilities = [
            item
            for item in expand_finance_capability_hierarchy(
                grants if isinstance(grants, list) else []
            )
            if item in {"finance", "finance_operate", "finance_admin"}
        ]
        return {"username": username, "role": role, "capabilities": capabilities}


class FixtureFinanceAuth:
    """Test-only explicit actor map; never selected without --auth-fixture."""

    def __init__(self, source: Path) -> None:
        loaded = json.loads(Path(source).read_text(encoding="utf-8"))
        self.actors = loaded.get("actors", {}) if isinstance(loaded, dict) else {}

    def authenticate(self, headers: Mapping[str, str]) -> dict[str, Any]:
        # Deliberately a fixture cookie, never a trustable header identity.
        value = _cookie(headers, "finance_fixture_session")
        actor = self.actors.get(value)
        if not isinstance(actor, dict):
            raise FinanceAuthDenied("authentication required")
        role = str(actor.get("role") or "operator")
        if role == "supplier":
            raise FinanceAuthDenied("supplier is forbidden")
        capabilities = [
            item
            for item in expand_finance_capability_hierarchy(
                actor.get("capabilities")
                if isinstance(actor.get("capabilities"), list)
                else []
            )
            if item in {"finance", "finance_operate", "finance_admin"}
        ]
        return {
            "username": str(actor.get("username") or value),
            "role": role,
            "capabilities": capabilities,
        }
