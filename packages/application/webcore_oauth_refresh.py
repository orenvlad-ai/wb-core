"""Opaque OAuth refresh grants with transactional, single-use rotation.

Use a dedicated path such as <runtime>/oauth-refresh/tokens.sqlite3. Its parent
must be private (0700), its database 0600; unsafe existing paths fail closed.
Each family has an absolute 30-day lifetime, never extended by rotation. Only
SHA256 token hashes are stored. Reuse of a consumed token revokes its family;
an unknown token or a request with the wrong binding cannot revoke a family.
The caller must pass a fingerprint of its current signing secret as generation,
and authorize the returned subject/scopes before issuing an access token.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time
from typing import Callable, Iterable, Iterator

REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60


class RefreshTokenError(ValueError):
    def __init__(self, code: str = "invalid_grant") -> None:
        self.code = code
        super().__init__(code)


class RefreshStorageError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("OAuth refresh storage unavailable")


@dataclass(frozen=True)
class RefreshGrant:
    refresh_token: str = field(repr=False)
    subject: str
    client_id: str
    resource: str
    scopes: tuple[str, ...]
    expires_at: int


def _scopes(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        raise RefreshTokenError("invalid_scope")
    try:
        result = tuple(sorted(set(values)))
    except (TypeError, ValueError):
        raise RefreshTokenError("invalid_scope") from None
    if not result or any(not isinstance(v, str) or not v or len(v) > 256
                         or any(ord(c) < 33 or ord(c) > 126 or c in '\\"' for c in v)
                         for v in result):
        raise RefreshTokenError("invalid_scope")
    return result


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _new_token() -> str:
    return "wr1." + secrets.token_urlsafe(48)


class RefreshTokenStore:
    def __init__(self, path: Path, generation: str, *, now: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        if not self.path.is_absolute() or not generation or len(generation) > 256:
            raise RefreshStorageError()
        self.generation, self.now = generation, now

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = None
        try:
            parent = self.path.parent
            if parent.resolve() != parent:
                raise RefreshStorageError()
            parent.mkdir(mode=0o700, exist_ok=True)
            directory = parent.lstat()
            if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid()
                    or stat.S_IMODE(directory.st_mode) != 0o700):
                raise RefreshStorageError()
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
            before = self.path.lstat()
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                    or stat.S_IMODE(before.st_mode) != 0o600):
                raise RefreshStorageError()
            conn = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                                   isolation_level=None, timeout=5)
            after = self.path.lstat()
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise RefreshStorageError()
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=FULL")
            if conn.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise RefreshStorageError()
            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                    raise RefreshStorageError()
                conn.execute("""CREATE TABLE refresh_families (
                    family_id TEXT PRIMARY KEY, subject TEXT NOT NULL,
                    client_id TEXT NOT NULL, resource TEXT NOT NULL,
                    generation TEXT NOT NULL, expires_at INTEGER NOT NULL,
                    revoked_at INTEGER)""")
                conn.execute("""CREATE TABLE refresh_tokens (
                    token_hash TEXT PRIMARY KEY,
                    family_id TEXT NOT NULL REFERENCES refresh_families(family_id),
                    scopes_json TEXT NOT NULL, used_at INTEGER)""")
                conn.execute("PRAGMA user_version=1")
            elif version != 1:
                raise RefreshStorageError()
            # Expired families cannot be refreshed or usefully replayed. Delete
            # their hashes inside the same lock rather than retaining forever.
            now = int(self.now())
            conn.execute("DELETE FROM refresh_tokens WHERE family_id IN (SELECT family_id FROM refresh_families WHERE expires_at<=?)", (now,))
            conn.execute("DELETE FROM refresh_families WHERE expires_at<=?", (now,))
            yield conn
            conn.commit()
        except (RefreshTokenError, RefreshStorageError):
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError):
            raise RefreshStorageError() from None
        finally:
            if conn is not None:
                if conn.in_transaction:
                    conn.rollback()
                conn.close()

    def mint(self, *, subject: str, client_id: str, resource: str,
             scopes: Iterable[str]) -> RefreshGrant:
        if any(not isinstance(v, str) or not v or len(v) > 2048
               for v in (subject, client_id, resource)):
            raise RefreshTokenError()
        selected = _scopes(scopes)
        token = _new_token()
        expiry = int(self.now()) + REFRESH_TOKEN_TTL_SECONDS
        with self._connection() as conn:
            family = secrets.token_hex(24)
            conn.execute("INSERT INTO refresh_families VALUES(?,?,?,?,?,?,NULL)",
                         (family, subject, client_id, resource, self.generation, expiry))
            conn.execute("INSERT INTO refresh_tokens VALUES(?,?,?,NULL)",
                         (_hash(token), family, json.dumps(selected)))
        return RefreshGrant(token, subject, client_id, resource, selected, expiry)

    def rotate(self, token: str, *, client_id: str, resource: str,
               allowed_scopes: Iterable[str], expected_subject: str,
               requested_scopes: Iterable[str] | None = None) -> RefreshGrant:
        allowed = _scopes(allowed_scopes)
        if (not isinstance(token, str) or len(token) != 68 or not token.startswith("wr1.")
                or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in token[4:])):
            raise RefreshTokenError()
        replay = False
        with self._connection() as conn:
            row = conn.execute("""SELECT f.*,t.used_at,t.scopes_json
                FROM refresh_tokens t JOIN refresh_families f USING(family_id)
                WHERE t.token_hash=?""", (_hash(token),)).fetchone()
            now = int(self.now())
            if (row is None or row["client_id"] != client_id or row["resource"] != resource
                    or row["subject"] != expected_subject or row["generation"] != self.generation
                    or row["revoked_at"] is not None or row["expires_at"] <= now):
                raise RefreshTokenError()
            if row["used_at"] is not None:
                conn.execute("UPDATE refresh_families SET revoked_at=? WHERE family_id=?",
                             (now, row["family_id"]))
                replay = True
            else:
                original = _scopes(json.loads(row["scopes_json"]))
                eligible = set(original) & set(allowed)
                selected = (_scopes(requested_scopes) if requested_scopes is not None
                            else tuple(sorted(eligible)))
                if not selected or not set(selected) <= eligible:
                    raise RefreshTokenError("invalid_scope")
                replacement = _new_token()
                conn.execute("UPDATE refresh_tokens SET used_at=? WHERE token_hash=?", (now, _hash(token)))
                conn.execute("INSERT INTO refresh_tokens VALUES(?,?,?,NULL)",
                             (_hash(replacement), row["family_id"], json.dumps(selected)))
                grant = RefreshGrant(replacement, row["subject"], client_id, resource,
                                     selected, row["expires_at"])
        # Commit revocation before reporting the replay. Exceptions inside the
        # transaction would roll it back and leave the stolen successor usable.
        if replay:
            raise RefreshTokenError()
        return grant

    def revoke(self, token: str) -> None:
        """Revoke a known family; unknown/malformed credentials are a no-op."""
        if not isinstance(token, str) or not token.isascii() or len(token) > 256:
            return
        with self._connection() as conn:
            conn.execute("""UPDATE refresh_families SET revoked_at=COALESCE(revoked_at,?)
                WHERE family_id IN (SELECT family_id FROM refresh_tokens WHERE token_hash=?)""",
                         (int(self.now()), _hash(token)))
