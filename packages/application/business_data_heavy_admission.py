"""Nonblocking heavy-producer exclusion, separate from maintenance drain.

Only explicitly integrated producers are covered. This is not global SQLite
serialization, a scheduler, or authority conveyed by a caller's job identifier.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import os
from pathlib import Path
import re
import stat
import threading
from typing import Iterator

from packages.application.business_data_procedure_admission import (
    AdmissionLease, initialize_admission, thread_start_is_proven_absent,
)


LOCK_FILENAME = ".business-data-heavy-admission.lock"
_OWNER: ContextVar[HeavyAdmissionLease | None] = ContextVar("business_heavy_owner", default=None)


class HeavyAdmissionBusy(RuntimeError):
    """No heavy work was admitted; callers may defer outside all leases."""


def _private_descriptor(path: Path, *, create: bool) -> int:
    flags = os.O_RDWR if create else os.O_RDONLY
    flags |= os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT
    fd = os.open(path, flags, 0o600)
    try:
        value = os.fstat(fd)
        if not stat.S_ISREG(value.st_mode) or value.st_mode & 0o077:
            raise RuntimeError("heavy admission lock must be a private regular file")
        actual = path.lstat()
        if (value.st_dev, value.st_ino) != (actual.st_dev, actual.st_ino):
            raise RuntimeError("heavy admission lock identity changed")
    except BaseException:
        os.close(fd)
        raise
    return fd


def current_heavy_owner(runtime_dir: Path) -> HeavyAdmissionLease | None:
    """Return only live in-process, current-thread authority, never a token lookup."""
    owner = _OWNER.get()
    if owner is not None and owner._is_current(Path(runtime_dir).resolve()):
        return owner
    return None


def require_heavy_owner(runtime_dir: Path) -> HeavyAdmissionLease:
    owner = current_heavy_owner(runtime_dir)
    if owner is None:
        raise RuntimeError("live heavy producer ownership is required")
    return owner


class HeavyAdmissionLease:
    """One transferable lease; concurrent entry by distinct threads is rejected.

    Creation acquires independent maintenance SH, then heavy EX without waiting.
    Enter the lease in its sole async worker and close in that worker's finally.
    After an uncertain Thread.start, only close_if_unstarted may abandon it.
    """

    def __init__(self, runtime_dir: Path, *, operation: str, independent: bool = False):
        if re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", operation) is None:
            raise ValueError("heavy operation name is invalid")
        self.runtime_dir = Path(runtime_dir).resolve()
        self.operation = operation
        self.pid = os.getpid()
        self.fd: int | None = None
        self._maintenance: AdmissionLease | None = None
        self._borrowed: HeavyAdmissionLease | None = None
        self._closed = False
        self._thread: int | None = None
        self._bound_thread: threading.Thread | None = None
        self._depth = 0
        self._state_lock = threading.RLock()
        if not independent:
            self._borrowed = current_heavy_owner(self.runtime_dir)
            if self._borrowed is not None:
                return
        # Writer admission is an explicit infrastructure mutation. Unlike the
        # older compatibility guard, never claim drain-visible SH if absent.
        # Read/status functions do not call this initializer.
        initialize_admission(self.runtime_dir)
        maintenance = AdmissionLease(self.runtime_dir, independent=True)
        self._maintenance = maintenance
        try:
            fd = _private_descriptor(self.runtime_dir / LOCK_FILENAME, create=True)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise HeavyAdmissionBusy("heavy_producer_running") from exc
            self.fd = fd
        except BaseException:
            if "fd" in locals():
                os.close(fd)
            maintenance.close()
            self._maintenance = None
            raise

    def _is_current(self, runtime_dir: Path) -> bool:
        if self.pid != os.getpid() or self._closed or self.fd is None:
            return False
        with self._state_lock:
            if (self.runtime_dir != runtime_dir or self._bound_thread is not threading.current_thread()
                    or self._thread != threading.get_ident() or not self._depth):
                return False
            try:
                value = os.fstat(self.fd)
                actual = (self.runtime_dir / LOCK_FILENAME).lstat()
                return (value.st_dev, value.st_ino) == (actual.st_dev, actual.st_ino)
            except OSError:
                return False

    @contextmanager
    def entered(self) -> Iterator[HeavyAdmissionLease]:
        if self.pid != os.getpid() or self._closed:
            raise RuntimeError("heavy lease is closed or belongs to a parent process")
        if self._borrowed is not None:
            if not self._borrowed._is_current(self.runtime_dir):
                raise RuntimeError("nested heavy owner is no longer current")
            yield self._borrowed
            return
        with self._state_lock:
            if self.fd is None:
                raise RuntimeError("heavy lease has no live descriptor")
            if self._thread is not None and self._thread != threading.get_ident():
                raise HeavyAdmissionBusy("heavy_lease_entered_by_another_thread")
            if self._bound_thread is not None and self._bound_thread is not threading.current_thread():
                raise HeavyAdmissionBusy("heavy_lease_is_bound_to_another_thread")
            self._bound_thread = threading.current_thread()
            self._thread = threading.get_ident()
            self._depth += 1
        token = _OWNER.set(self)
        try:
            assert self._maintenance is not None
            with self._maintenance.entered():
                if not self._is_current(self.runtime_dir):
                    raise RuntimeError("heavy lock identity is no longer current")
                yield self
        finally:
            _OWNER.reset(token)
            with self._state_lock:
                self._depth -= 1
                if not self._depth:
                    self._thread = None

    def close(self) -> None:
        # Closing a fork's duplicate must never LOCK_UN the shared description.
        if self.pid != os.getpid():
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            if self._maintenance is not None:
                self._maintenance.close()
                self._maintenance = None
            self._closed = True
            return
        with self._state_lock:
            if self._thread is not None and self._thread != threading.get_ident():
                raise RuntimeError("cannot close another thread's entered heavy lease")
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            if self._maintenance is not None:
                self._maintenance.close()
                self._maintenance = None
            self._closed = True

    def close_if_unstarted(self, thread) -> bool:
        if not thread_start_is_proven_absent(thread):
            return False
        self.close()
        return True


@contextmanager
def heavy_admitted(runtime_dir: Path, *, operation: str) -> Iterator[HeavyAdmissionLease]:
    lease = HeavyAdmissionLease(runtime_dir, operation=operation)
    try:
        with lease.entered() as owner:
            yield owner
    finally:
        lease.close()


def heavy_admission_status(runtime_dir: Path) -> dict:
    """Read-only nonblocking proof. Missing or unsafe infrastructure is not idle."""
    runtime = Path(runtime_dir).resolve()
    owner = current_heavy_owner(runtime)
    if owner is not None:
        return {"ready": True, "idle": False, "owned_here": True, "operation": owner.operation}
    try:
        fd = _private_descriptor(runtime / LOCK_FILENAME, create=False)
    except (OSError, RuntimeError) as exc:
        return {"ready": False, "idle": False, "reason": type(exc).__name__}
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"ready": True, "idle": False, "reason": "heavy_producer_running"}
        return {"ready": True, "idle": True, "reason": ""}
    finally:
        os.close(fd)
