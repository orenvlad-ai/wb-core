"""A short, pinned read boundary for one opt-in Vitrina window operation.

Legacy reads keep their existing connection behaviour. A window worker pins
the operational SQLite snapshot for its whole bounded operation; nested
readers borrow it without closing, committing, or starting a new transaction.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import os
from pathlib import Path
import sqlite3
from typing import Iterator


class WindowReadContextError(RuntimeError):
    pass


class _BorrowedConnection:
    def __init__(self, physical: sqlite3.Connection, *, operational_generation: tuple[object, ...] | None = None) -> None:
        object.__setattr__(self, "_physical", physical)
        object.__setattr__(self, "ready_header_cache_generation", operational_generation)

    def __enter__(self) -> "_BorrowedConnection":
        return self

    def __exit__(self, _type, _value, _traceback) -> bool:
        # An inner `with _connect()` must not commit/rollback the pinned read.
        return False

    def close(self) -> None:
        # Some readers explicitly close their logical connection.
        return None

    def commit(self) -> None:
        raise WindowReadContextError("nested window reader attempted to commit")

    def rollback(self) -> None:
        raise WindowReadContextError("nested window reader attempted to roll back")

    def execute(self, sql: str, parameters=()):
        keyword = str(sql).lstrip().split(None, 1)[0].upper() if str(sql).strip() else ""
        if keyword in {"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"}:
            raise WindowReadContextError("nested window reader attempted a transaction command")
        return self._physical.execute(sql, parameters)

    def executescript(self, _script: str):
        raise WindowReadContextError("nested window reader attempted a SQL script")

    def __setattr__(self, name: str, value) -> None:
        setattr(self._physical, name, value)

    def __getattr__(self, name: str):
        return getattr(self._physical, name)


class WindowReadContext:
    def __init__(self, operational_db_path: Path, *, runtime_dir: Path | None = None) -> None:
        self.operational_db_path = Path(operational_db_path).resolve()
        self.runtime_dir = Path(runtime_dir).resolve() if runtime_dir is not None else None
        self._physical: sqlite3.Connection | None = None
        self._operational_generation: tuple[object, ...] | None = None
        self._book: sqlite3.Connection | None = None
        self._book_path: Path | None = None
        self._book_missing = False
        self._file_bytes: dict[Path, bytes | None] = {}

    @property
    def operational_generation(self) -> tuple[object, ...]:
        if self._operational_generation is None:
            raise WindowReadContextError("window operational store is not pinned")
        return self._operational_generation

    def start(self) -> None:
        if not self.operational_db_path.is_file():
            raise WindowReadContextError("window operational store is missing")
        before_open = os.stat(self.operational_db_path)
        physical = sqlite3.connect(
            self.operational_db_path.as_uri() + "?mode=ro", uri=True,
        )
        try:
            physical.row_factory = sqlite3.Row
            physical.execute("PRAGMA query_only=ON")
            if physical.execute("PRAGMA query_only").fetchone()[0] != 1:
                raise WindowReadContextError("window operational store is not query-only")
            physical.execute("BEGIN")
            # BEGIN is deferred; this first read pins the WAL snapshot now.
            physical.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            after_pin = os.stat(self.operational_db_path)
            if (before_open.st_dev, before_open.st_ino) != (after_pin.st_dev, after_pin.st_ino):
                raise WindowReadContextError("window operational store changed while pinning")
            required = {
                "registry_upload_current_state",
                "sheet_vitrina_v1_ready_snapshots",
            }
            actual = {
                row[0] for row in physical.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?, ?)",
                    tuple(sorted(required)),
                )
            }
            if actual != required:
                raise WindowReadContextError("window operational schema is incomplete")
        except Exception:
            physical.close()
            raise
        self._physical = physical
        self._operational_generation = (
            "operational_file", str(self.operational_db_path), before_open.st_dev, before_open.st_ino,
        )
        if self.runtime_dir is not None:
            try:
                self._pin_book(self.runtime_dir / "fbs-snapshot-accounting.sqlite3")
            except Exception:
                self.close()
                raise

    def _pin_book(self, path: Path) -> None:
        target = Path(path).resolve()
        if self._book_path is not None:
            if target != self._book_path:
                raise WindowReadContextError("window reader attempted another FBS book")
            return
        self._book_path = target
        if not target.is_file():
            self._book_missing = True
            return
        book = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)
        try:
            book.execute("PRAGMA query_only=ON")
            if book.execute("PRAGMA query_only").fetchone()[0] != 1:
                raise WindowReadContextError("FBS book is not query-only")
            book.execute("BEGIN")
            book.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            actual = {row[0] for row in book.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            expected = {"accounting_revisions", "accounting_current", "accounting_blobs"}
            if actual != expected:
                raise WindowReadContextError("FBS accounting book schema is incomplete")
        except Exception:
            book.close()
            raise
        self._book = book

    def borrow_book(self, path: Path) -> _BorrowedConnection | None:
        self._pin_book(path)
        return _BorrowedConnection(self._book) if self._book is not None else None

    def read_file_once(self, path: Path) -> bytes | None:
        target = Path(path).resolve()
        if self.runtime_dir is not None and target.parent != self.runtime_dir:
            raise WindowReadContextError("window reader attempted an unscoped file")
        if target not in self._file_bytes:
            try:
                self._file_bytes[target] = target.read_bytes()
            except FileNotFoundError:
                self._file_bytes[target] = None
            except OSError as exc:
                raise WindowReadContextError(f"window file read failed: {target.name}") from exc
        return self._file_bytes[target]

    def file_content_digests(self) -> dict[str, str]:
        return {
            str(path): ("missing" if content is None else "sha256:" + hashlib.sha256(content).hexdigest())
            for path, content in self._file_bytes.items()
        }

    def borrow(self, path: Path) -> _BorrowedConnection:
        if Path(path).resolve() != self.operational_db_path or self._physical is None:
            raise WindowReadContextError("window reader attempted an unpinned operational store")
        return _BorrowedConnection(self._physical, operational_generation=self._operational_generation)

    def close(self) -> None:
        physical = self._physical
        self._physical = None
        self._operational_generation = None
        book = self._book
        self._book = None
        if book is not None:
            try:
                book.execute("ROLLBACK")
            finally:
                book.close()
        if physical is not None:
            try:
                physical.execute("ROLLBACK")
            finally:
                physical.close()


_ACTIVE: ContextVar[WindowReadContext | None] = ContextVar(
    "web_vitrina_window_read_context", default=None,
)


def active_window_read_context() -> WindowReadContext | None:
    return _ACTIVE.get()


def borrowed_operational_connection(path: Path) -> _BorrowedConnection | None:
    active = _ACTIVE.get()
    return active.borrow(path) if active is not None else None


@contextmanager
def window_read_context(
    operational_db_path: Path, *, runtime_dir: Path | None = None,
) -> Iterator[WindowReadContext]:
    if _ACTIVE.get() is not None:
        raise WindowReadContextError("nested window read contexts are not supported")
    context = WindowReadContext(operational_db_path, runtime_dir=runtime_dir)
    context.start()
    token = _ACTIVE.set(context)
    try:
        yield context
    finally:
        _ACTIVE.reset(token)
        context.close()
