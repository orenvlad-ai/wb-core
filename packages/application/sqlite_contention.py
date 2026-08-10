"""Shared bounded SQLite contention handling and sanitized observability.

The module deliberately retries only SQLite statements that SQLite itself
reports as not executed because of ``BUSY``/``LOCKED``.  Business operations
remain responsible for transaction boundaries and idempotency.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import os
from pathlib import Path
import random
import re
import sqlite3
import sys
import time
from typing import Any, Callable, Iterator, Mapping, TypeVar


DEFAULT_INTERACTIVE_TIMEOUT_MS = 30_000
DEFAULT_BACKGROUND_TIMEOUT_MS = 10_000
SQLITE_ATTEMPT_BUSY_TIMEOUT_MS = 250
SQLITE_RETRY_INITIAL_MS = 20
SQLITE_RETRY_MAX_MS = 500
SQLITE_SLOW_TRANSACTION_MS = 1_000


@dataclass(frozen=True)
class SQLiteOperationContext:
    endpoint: str = ""
    operation: str = ""
    phase: str = ""
    priority: str = "normal"
    owner: str = ""


@dataclass(frozen=True)
class SQLiteContentionState:
    wait_ms: int
    retries: int
    phase: str
    exhausted: bool


class SQLiteContentionExhausted(sqlite3.OperationalError):
    """Bounded, sanitized replacement for a raw SQLite BUSY/LOCKED error."""

    def __init__(
        self,
        *,
        wait_ms: int,
        retries: int,
        phase: str,
    ) -> None:
        super().__init__("sqlite_contention_exhausted")
        self.wait_ms = int(wait_ms)
        self.retries = int(retries)
        self.phase = str(phase or "statement")
        self.retryable = True
        self.code = "sqlite_contention_exhausted"


_OPERATION_CONTEXT: ContextVar[SQLiteOperationContext] = ContextVar(
    "wb_core_sqlite_operation_context",
    default=SQLiteOperationContext(),
)
_LAST_CONTENTION: ContextVar[SQLiteContentionState | None] = ContextVar(
    "wb_core_sqlite_last_contention",
    default=None,
)
_T = TypeVar("_T")


def set_sqlite_operation_context(
    *,
    endpoint: str = "",
    operation: str = "",
    phase: str = "",
    priority: str = "normal",
    owner: str = "",
) -> None:
    """Set request/process-local metadata without retaining query strings."""

    _OPERATION_CONTEXT.set(
        SQLiteOperationContext(
            endpoint=str(endpoint or "").split("?", 1)[0][:240],
            operation=str(operation or "")[:120],
            phase=str(phase or "")[:120],
            priority=_normalize_priority(priority),
            owner=_safe_owner(owner),
        )
    )
    _LAST_CONTENTION.set(None)


@contextmanager
def sqlite_operation_context(
    *,
    endpoint: str = "",
    operation: str = "",
    phase: str = "",
    priority: str = "normal",
    owner: str = "",
) -> Iterator[None]:
    token = _OPERATION_CONTEXT.set(
        SQLiteOperationContext(
            endpoint=str(endpoint or "").split("?", 1)[0][:240],
            operation=str(operation or "")[:120],
            phase=str(phase or "")[:120],
            priority=_normalize_priority(priority),
            owner=_safe_owner(owner),
        )
    )
    contention_token = _LAST_CONTENTION.set(None)
    try:
        yield
    finally:
        _LAST_CONTENTION.reset(contention_token)
        _OPERATION_CONTEXT.reset(token)


def current_sqlite_contention_state() -> SQLiteContentionState | None:
    return _LAST_CONTENTION.get()


def is_sqlite_contention_error(value: object) -> bool:
    if isinstance(value, SQLiteContentionExhausted):
        return True
    if isinstance(value, sqlite3.Error):
        code = getattr(value, "sqlite_errorcode", None)
        if code is not None and int(code) & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            return True
    text = str(value or "").lower()
    return any(
        marker in text
        for marker in (
            "database is locked",
            "database table is locked",
            "database schema is locked",
            "sqlite_busy",
            "sqlite_locked",
            "sqlite_contention_exhausted",
        )
    )


def connect_sqlite(
    db_path: str | Path,
    *,
    timeout_ms: int | None = None,
    priority: str | None = None,
    isolation_level: str | None = "",
    uri: bool = False,
) -> sqlite3.Connection:
    """Open an observed connection with bounded statement retry/backoff."""

    context = _OPERATION_CONTEXT.get()
    requested_priority = _normalize_priority(priority or context.priority)
    normalized_priority = (
        _process_default_priority()
        if requested_priority == "normal"
        else requested_priority
    )
    effective_timeout_ms = int(
        timeout_ms
        if timeout_ms is not None
        else (
            DEFAULT_BACKGROUND_TIMEOUT_MS
            if normalized_priority == "background"
            else DEFAULT_INTERACTIVE_TIMEOUT_MS
        )
    )
    if effective_timeout_ms <= 0:
        raise ValueError("SQLite contention timeout must be positive")
    conn = sqlite3.connect(
        db_path,
        timeout=SQLITE_ATTEMPT_BUSY_TIMEOUT_MS / 1000,
        isolation_level=isolation_level,
        uri=uri,
        factory=ObservedSQLiteConnection,
    )
    assert isinstance(conn, ObservedSQLiteConnection)
    conn.configure_contention(
        timeout_ms=effective_timeout_ms,
        priority=normalized_priority,
    )
    return conn


class ObservedSQLiteConnection(sqlite3.Connection):
    """Connection subclass that adds safe statement-level BUSY recovery."""

    _contention_timeout_ms: int
    _contention_priority: str
    _transaction_started_at: float | None
    _transaction_write_phase_at: float | None
    _transaction_retry_count: int
    _transaction_wait_ms: int

    def configure_contention(self, *, timeout_ms: int, priority: str) -> None:
        self._contention_timeout_ms = int(timeout_ms)
        self._contention_priority = _normalize_priority(priority)
        self._transaction_started_at = None
        self._transaction_write_phase_at = None
        self._transaction_retry_count = 0
        self._transaction_wait_ms = 0
        sqlite3.Connection.execute(
            self,
            f"PRAGMA busy_timeout = {SQLITE_ATTEMPT_BUSY_TIMEOUT_MS}",
        )

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        was_in_transaction = self.in_transaction
        phase = _sql_phase(sql)
        cursor = self._retry(
            phase=phase,
            action=lambda: sqlite3.Connection.execute(self, sql, parameters),
        )
        self._track_transaction_after_statement(was_in_transaction)
        self._track_write_phase(phase, sql=sql)
        return cursor

    def executemany(self, sql: str, seq_of_parameters: Any, /) -> sqlite3.Cursor:
        was_in_transaction = self.in_transaction
        phase = _sql_phase(sql)
        cursor: sqlite3.Cursor | None = None
        # sqlite3.executemany() may have executed earlier parameter sets before
        # a later one reports BUSY.  Retrying the whole batch could therefore
        # duplicate business mutations.  Retry each atomic SQLite statement
        # separately and leave all-or-nothing semantics to the caller's
        # explicit transaction.
        for parameters in seq_of_parameters:
            cursor = self._retry(
                phase=phase,
                action=lambda parameters=parameters: sqlite3.Connection.execute(
                    self,
                    sql,
                    parameters,
                ),
            )
        if cursor is None:
            cursor = sqlite3.Connection.executemany(self, sql, ())
        self._track_transaction_after_statement(was_in_transaction)
        self._track_write_phase(phase, sql=sql)
        return cursor

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:
        was_in_transaction = self.in_transaction
        # executescript() can contain implicit commits and several already
        # applied statements.  Replaying it after BUSY is not generally safe.
        # Schema migrations are serialized by their repo-owned file locks.
        cursor = sqlite3.Connection.executescript(self, sql_script)
        self._track_transaction_after_statement(was_in_transaction)
        self._track_write_phase("schema", sql=sql_script)
        return cursor

    def commit(self) -> None:
        started_at = self._transaction_started_at
        try:
            self._retry(
                phase="commit",
                action=lambda: sqlite3.Connection.commit(self),
            )
        finally:
            self._finish_transaction_observation(started_at, outcome="commit")

    def rollback(self) -> None:
        started_at = self._transaction_started_at
        try:
            sqlite3.Connection.rollback(self)
        finally:
            self._finish_transaction_observation(started_at, outcome="rollback")

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool | None:
        try:
            return sqlite3.Connection.__exit__(self, exc_type, exc, tb)
        finally:
            self.close()

    def _retry(self, *, phase: str, action: Callable[[], _T]) -> _T:
        started_at = time.monotonic()
        deadline = started_at + self._contention_timeout_ms / 1000
        retries = 0
        delay_ms = (
            100
            if self._contention_priority == "background"
            else SQLITE_RETRY_INITIAL_MS
        )
        while True:
            try:
                result = action()
            except sqlite3.OperationalError as exc:
                if not is_sqlite_contention_error(exc):
                    raise
                retries += 1
                elapsed_ms = max(0, int((time.monotonic() - started_at) * 1000))
                # A deferred WAL read transaction cannot ever upgrade after a
                # newer writer committed (SQLITE_BUSY_SNAPSHOT). Retrying the
                # same statement inside that stale snapshot only prolongs the
                # transaction; fail immediately so the caller can rollback and
                # rebuild from a fresh source revision.
                busy_snapshot = int(getattr(sqlite3, "SQLITE_BUSY_SNAPSHOT", 517))
                if self.in_transaction and int(getattr(exc, "sqlite_errorcode", -1)) == busy_snapshot:
                    state = SQLiteContentionState(
                        wait_ms=elapsed_ms,
                        retries=retries,
                        phase=phase,
                        exhausted=True,
                    )
                    _LAST_CONTENTION.set(state)
                    self._record_retry(state)
                    _emit_contention_event(
                        event="sqlite_contention_snapshot_restart_required",
                        state=state,
                        transaction_duration_ms=self._transaction_duration_ms(),
                        context=_effective_connection_context(self._contention_priority),
                    )
                    raise SQLiteContentionExhausted(
                        wait_ms=elapsed_ms,
                        retries=retries,
                        phase=phase,
                    ) from exc
                remaining_ms = int(max(0.0, deadline - time.monotonic()) * 1000)
                if remaining_ms <= 0:
                    state = SQLiteContentionState(
                        wait_ms=elapsed_ms,
                        retries=retries,
                        phase=phase,
                        exhausted=True,
                    )
                    _LAST_CONTENTION.set(state)
                    self._record_retry(state)
                    _emit_contention_event(
                        event="sqlite_contention_exhausted",
                        state=state,
                        transaction_duration_ms=self._transaction_duration_ms(),
                        context=_effective_connection_context(
                            self._contention_priority
                        ),
                    )
                    raise SQLiteContentionExhausted(
                        wait_ms=elapsed_ms,
                        retries=retries,
                        phase=phase,
                    ) from exc
                jitter_ceiling = min(delay_ms, remaining_ms)
                sleep_ms = min(
                    remaining_ms,
                    max(1, int(jitter_ceiling * (0.5 + random.random() * 0.5))),
                )
                time.sleep(sleep_ms / 1000)
                delay_ms = min(SQLITE_RETRY_MAX_MS, delay_ms * 2)
                continue
            elapsed_ms = max(0, int((time.monotonic() - started_at) * 1000))
            if retries:
                state = SQLiteContentionState(
                    wait_ms=elapsed_ms,
                    retries=retries,
                    phase=phase,
                    exhausted=False,
                )
                _LAST_CONTENTION.set(state)
                self._record_retry(state)
                _emit_contention_event(
                    event="sqlite_contention_recovered",
                    state=state,
                    transaction_duration_ms=self._transaction_duration_ms(),
                    context=_effective_connection_context(
                        self._contention_priority
                    ),
                )
            return result

    def _track_transaction_after_statement(self, was_in_transaction: bool) -> None:
        if not was_in_transaction and self.in_transaction:
            self._transaction_started_at = time.monotonic()
            self._transaction_write_phase_at = None
            self._transaction_retry_count = 0
            self._transaction_wait_ms = 0

    def _track_write_phase(self, phase: str, *, sql: str) -> None:
        begins_with_writer_lock = bool(
            phase == "begin"
            and re.match(
                r"\s*BEGIN\s+(?:IMMEDIATE|EXCLUSIVE)\b",
                str(sql),
                re.IGNORECASE,
            )
        )
        if (
            self.in_transaction
            and self._transaction_write_phase_at is None
            and (phase in {"write_statement", "schema"} or begins_with_writer_lock)
        ):
            self._transaction_write_phase_at = time.monotonic()

    def _record_retry(self, state: SQLiteContentionState) -> None:
        if self.in_transaction:
            self._transaction_retry_count += state.retries
            self._transaction_wait_ms += state.wait_ms

    def _transaction_duration_ms(self) -> int | None:
        if self._transaction_started_at is None:
            return None
        return max(0, int((time.monotonic() - self._transaction_started_at) * 1000))

    def _finish_transaction_observation(
        self,
        started_at: float | None,
        *,
        outcome: str,
    ) -> None:
        write_started_at = self._transaction_write_phase_at
        duration_ms = (
            max(0, int((time.monotonic() - write_started_at) * 1000))
            if write_started_at is not None
            else None
        )
        if (
            duration_ms is not None
            and (
                duration_ms >= SQLITE_SLOW_TRANSACTION_MS
                or self._transaction_retry_count > 0
            )
        ):
            _emit_contention_event(
                event="sqlite_write_transaction",
                state=SQLiteContentionState(
                    wait_ms=self._transaction_wait_ms,
                    retries=self._transaction_retry_count,
                    phase=outcome,
                    exhausted=False,
                ),
                transaction_duration_ms=duration_ms,
                context=_effective_connection_context(
                    self._contention_priority
                ),
            )
        self._transaction_started_at = None
        self._transaction_write_phase_at = None
        self._transaction_retry_count = 0
        self._transaction_wait_ms = 0


def emit_controlled_contention_response_event(
    *,
    endpoint: str,
    operation: str,
    wait_ms: int,
    retries: int,
) -> None:
    context = _OPERATION_CONTEXT.get()
    _emit_contention_event(
        event="sqlite_contention_http_retryable",
        state=SQLiteContentionState(
            wait_ms=max(0, int(wait_ms)),
            retries=max(0, int(retries)),
            phase="http_response",
            exhausted=True,
        ),
        transaction_duration_ms=None,
        context=SQLiteOperationContext(
            endpoint=str(endpoint or context.endpoint).split("?", 1)[0][:240],
            operation=str(operation or context.operation)[:120],
            phase="http_response",
            priority=context.priority,
            owner=context.owner,
        ),
    )


def _effective_connection_context(priority: str) -> SQLiteOperationContext:
    current = _OPERATION_CONTEXT.get()
    return SQLiteOperationContext(
        endpoint=current.endpoint,
        operation=current.operation,
        phase=current.phase,
        priority=_normalize_priority(priority),
        owner=current.owner,
    )


def _emit_contention_event(
    *,
    event: str,
    state: SQLiteContentionState,
    transaction_duration_ms: int | None,
    context: SQLiteOperationContext | None = None,
) -> None:
    current = context or _OPERATION_CONTEXT.get()
    payload: dict[str, Any] = {
        "event": event,
        "endpoint": current.endpoint,
        "operation": current.operation,
        "phase": state.phase or current.phase,
        "wait_ms": int(state.wait_ms),
        "retry_count": int(state.retries),
        "exhausted": bool(state.exhausted),
        "priority": current.priority,
        "owner": current.owner or _default_owner(),
    }
    if current.phase and current.phase != state.phase:
        payload["context_phase"] = current.phase
    if transaction_duration_ms is not None:
        payload["write_transaction_ms"] = int(transaction_duration_ms)
    print(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        file=sys.stderr,
        flush=True,
    )


def _sql_phase(sql: str) -> str:
    token = str(sql or "").lstrip().split(None, 1)
    if not token:
        return "statement"
    normalized = token[0].lower()
    if normalized in {"begin", "commit", "rollback"}:
        return normalized
    if normalized in {"insert", "update", "delete", "replace"}:
        return "write_statement"
    if normalized in {"create", "alter", "drop", "pragma"}:
        return "schema"
    return "read_statement"


def _normalize_priority(value: str) -> str:
    normalized = str(value or "normal").strip().lower()
    if normalized not in {"interactive", "normal", "background"}:
        return "normal"
    return normalized


def _safe_owner(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    return Path(normalized).name[:120]


def _default_owner() -> str:
    explicit = _safe_owner(os.environ.get("WB_CORE_SQLITE_OWNER", ""))
    if explicit:
        return explicit
    return _safe_owner(sys.argv[0]) or "python"


def _process_default_priority() -> str:
    owner = _default_owner().casefold()
    background_markers = (
        "worker",
        "readonly",
        "sync",
        "refresh",
        "maintenance",
        "scheduler",
        "recovery",
        "release_train",
    )
    return (
        "background"
        if any(marker in owner for marker in background_markers)
        else "interactive"
    )
