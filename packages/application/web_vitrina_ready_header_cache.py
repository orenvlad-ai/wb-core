"""Bounded ready-header reuse inside pinned Web Vitrina read transactions.

Only cheap ready-row identity fields and revision counters are queried on a
hit. The caller's decoder remains responsible for validating full headers and
for failing closed when a ready revision is missing.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
import sqlite3
import sys
from threading import Lock
from typing import Callable, TypeVar


T = TypeVar("T")

_IDENTITY_SQL = """
SELECT s.bundle_version,s.as_of_date,s.snapshot_id,s.activated_at,
       s.refreshed_at,r.revision
FROM sheet_vitrina_v1_ready_snapshots AS s
LEFT JOIN sheet_vitrina_v1_ready_revisions AS r
  ON r.bundle_version=s.bundle_version AND r.as_of_date=s.as_of_date
ORDER BY s.activated_at DESC,s.refreshed_at DESC,s.as_of_date DESC,
         s.bundle_version DESC
"""


def _retained_size(value: object, seen: set[int]) -> int:
    identity = id(value)
    if identity in seen:
        return 0
    seen.add(identity)
    size = sys.getsizeof(value)
    if is_dataclass(value) and not isinstance(value, type):
        return size + sum(_retained_size(getattr(value, field.name), seen) for field in fields(value))
    if isinstance(value, dict):
        return size + sum(_retained_size(key, seen) + _retained_size(item, seen)
                          for key, item in value.items())
    if isinstance(value, (tuple, list, set, frozenset)):
        return size + sum(_retained_size(item, seen) for item in value)
    return size


def _database_identity(conn: sqlite3.Connection) -> tuple[object, ...] | None:
    generation = getattr(conn, "ready_header_cache_generation", None)
    if generation is not None:
        # The pinned read context captured this file generation before connect
        # and checked it again after BEGIN. Path stat at load time is unsafe:
        # os.replace could have detached the still-open SQLite connection.
        return tuple(generation)
    # A plain sqlite connection has no handle-backed file identity in stdlib.
    # Retain its object in the cache key, so only this physical connection hits.
    return ("physical_connection", conn)


class ReadyHeaderCache:
    """Cache one immutable header tuple, returning a fresh list to each caller."""

    def __init__(self, max_retained_bytes: int = 8 * 1024 * 1024) -> None:
        if max_retained_bytes < 1:
            raise ValueError("max_retained_bytes must be positive")
        self.max_retained_bytes = max_retained_bytes
        self._lock = Lock()
        self._database_key: tuple[object, ...] | None = None
        self._identity: tuple[tuple[object, ...], ...] | None = None
        self._headers: tuple[object, ...] | None = None
        self.retained_bytes = 0

    def load(self, conn: sqlite3.Connection, decoder: Callable[[sqlite3.Connection], list[T]]) -> list[T]:
        """Reuse headers only when every ordered ready-row identity is unchanged."""
        with self._lock:
            database_key = _database_identity(conn)
            has_revisions = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sheet_vitrina_v1_ready_revisions'"
            ).fetchone() is not None
            if not has_revisions:
                # Let the decoder raise its domain-specific fail-closed error.
                self._database_key = None
                self._identity = None
                self._headers = None
                self.retained_bytes = 0
                return decoder(conn)
            identity = tuple(tuple(row) for row in conn.execute(_IDENTITY_SQL))
            if any(row[-1] is None for row in identity):
                self._database_key = None
                self._identity = None
                self._headers = None
                self.retained_bytes = 0
                return decoder(conn)
            if database_key is not None and database_key == self._database_key \
                    and identity == self._identity and self._headers is not None:
                return list(self._headers)  # type: ignore[return-value]

            decoded = decoder(conn)
            headers = tuple(decoded)
            # getsizeof recursively counts retained Python objects; add margin
            # for allocator overhead rather than treating JSON bytes as heap.
            estimated = int(_retained_size((database_key, identity, headers), set()) * 1.25) + 1024
            if database_key is not None and estimated <= self.max_retained_bytes:
                self._database_key = database_key
                self._identity = identity
                self._headers = headers
                self.retained_bytes = estimated
            else:
                self._database_key = None
                self._identity = None
                self._headers = None
                self.retained_bytes = 0
            return list(headers)
