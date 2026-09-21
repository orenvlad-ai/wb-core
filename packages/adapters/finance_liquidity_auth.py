"""Pinned, query-only authorization seam for the optional Finance process."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
from pathlib import Path
import time
from typing import Any, Mapping

import apsw

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
    loader can initialise schema/write. Manifest/path revalidation surrounds a
    read-only APSW connection, and SQLITE_FCNTL_HAS_MOVED is checked on that exact
    grant-reading connection before and after the read. A renamed, moved, deleted,
    or replaced operational owner therefore fails closed.
    """

    def __init__(
        self,
        runtime_dir: Path,
        session_secret: str | None = None,
        *,
        operational_timeout_ms: int = 5_000,
    ) -> None:
        self.registry = StoreRegistry(Path(runtime_dir))
        self.operational_timeout_ms = int(operational_timeout_ms)
        if self.operational_timeout_ms <= 0:
            raise ValueError("operational_timeout_ms must be positive")
        self.session_secret = (
            session_secret
            if session_secret is not None
            else str(os.environ.get("WB_CORE_WEB_AUTH_SESSION_SECRET") or "")
        )

    def _open_operational_connection(self, path: Path) -> apsw.Connection:
        connection = apsw.Connection(str(path), flags=apsw.SQLITE_OPEN_READONLY)
        connection.set_busy_timeout(self.operational_timeout_ms)
        connection.execute("PRAGMA query_only=ON")
        query_only = next(connection.execute("PRAGMA query_only"), None)
        if (
            query_only is None
            or int(query_only[0]) != 1
            or not connection.readonly("main")
        ):
            connection.close()
            raise FinanceAuthUnavailable("operational authorization is not query-only")
        return connection

    @staticmethod
    def _assert_connection_not_moved(connection: Any) -> None:
        moved = ctypes.c_int(-1)
        understood = connection.file_control(
            "main",
            apsw.SQLITE_FCNTL_HAS_MOVED,
            ctypes.addressof(moved),
        )
        if not understood or moved.value != 0:
            raise FinanceAuthUnavailable(
                "operational authorization descriptor moved"
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
            generation = self.registry.generation("operational", manifest=pinned)
            expected_path = self.registry.resolve(
                "operational", manifest=pinned
            ).resolve()
            before_identity = _identity(expected_path)
            conn = self._open_operational_connection(expected_path)
            try:
                database_path = Path(conn.filename).resolve()
                if (
                    database_path != expected_path
                    or _identity(database_path) != before_identity
                ):
                    raise FinanceAuthUnavailable("operational authorization path drift")
                self._assert_connection_not_moved(conn)
                conn.execute("BEGIN")
                if not pinned.implicit and pinned.state != "monolith":
                    identity = next(
                        conn.execute(
                            "SELECT schema_revision,logical_store,generation_id,"
                            "generation_epoch,source_fingerprint "
                            "FROM finance_operational_schema_meta WHERE singleton=1"
                        ),
                        None,
                    )
                    expected_identity = (
                        generation.schema_revision,
                        "operational",
                        generation.generation_id,
                        generation.generation_epoch,
                        pinned.source_fingerprint,
                    )
                    if identity is None or tuple(identity) != expected_identity:
                        raise FinanceAuthUnavailable(
                            "operational authorization generation mismatch"
                        )
                raw_row = next(
                    conn.execute(
                        "SELECT username,role,allowed_sections_json,is_active "
                        "FROM sheet_vitrina_v1_users WHERE username=?",
                        (username.strip().lower(),),
                    ),
                    None,
                )
                self._assert_connection_not_moved(conn)
            finally:
                conn.close()
            after_identity = _identity(expected_path)
            revalidated = self.registry.load(require_files=True)
        except FinanceAuthUnavailable:
            raise
        except (StorageRegistryError, apsw.Error, OSError) as exc:
            raise FinanceAuthUnavailable(
                "operational authorization store unavailable"
            ) from exc
        if revalidated != pinned or before_identity != after_identity:
            raise FinanceAuthUnavailable("operational authorization generation drift")
        row = (
            {
                "username": raw_row[0],
                "role": raw_row[1],
                "allowed_sections_json": raw_row[2],
                "is_active": raw_row[3],
            }
            if raw_row is not None
            else None
        )
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
