"""Process-wide serialization for all functional warehouse writers."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
from pathlib import Path
import threading
import time
from typing import Any, Iterator


WAREHOUSE_FUNCTIONAL_LOCK_FILENAME = ".warehouse-functional-sync.lock"
WAREHOUSE_FUNCTIONAL_JOB_LOCK_FILENAME = ".warehouse-functional-job.lock"
_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[Path, threading.RLock] = {}
_LOCAL = threading.local()


class WarehouseFunctionalBusyError(RuntimeError):
    """Raised when a non-blocking functional writer cannot acquire the lock."""


def _process_lock(lock_path: Path) -> threading.RLock:
    with _LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(lock_path, threading.RLock())


@contextmanager
def warehouse_functional_write_lock(
    runtime_dir: Path,
    *,
    blocking: bool = True,
    timeout_seconds: float | None = None,
    poll_interval_seconds: float = 0.1,
) -> Iterator[dict[str, Any]]:
    """Serialize every functional writer, including nested common-boundary calls."""

    started = time.monotonic()
    lock_path = (Path(runtime_dir) / WAREHOUSE_FUNCTIONAL_LOCK_FILENAME).resolve()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    process_lock = _process_lock(lock_path)
    if not blocking:
        process_lock_acquired = process_lock.acquire(blocking=False)
    elif timeout_seconds is None:
        process_lock_acquired = process_lock.acquire()
    else:
        process_lock_acquired = process_lock.acquire(
            timeout=max(float(timeout_seconds), 0.0)
        )
    if not process_lock_acquired:
        raise WarehouseFunctionalBusyError(
            "functional warehouse writer is already running"
        )
    try:
        held = getattr(_LOCAL, "warehouse_functional_locks", None)
        if held is None:
            held = {}
            _LOCAL.warehouse_functional_locks = held
        state = held.get(lock_path)
        if state is not None:
            state["depth"] += 1
            try:
                yield {
                    "wait_ms": round((time.monotonic() - started) * 1000, 3),
                    "reentrant": 1.0,
                }
            finally:
                state["depth"] -= 1
            return

        handle = lock_path.open("a+", encoding="utf-8")
        deadline = (
            None
            if timeout_seconds is None
            else started + max(float(timeout_seconds), 0.0)
        )
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if not blocking or (
                    deadline is not None and time.monotonic() >= deadline
                ):
                    handle.close()
                    raise WarehouseFunctionalBusyError(
                        "functional warehouse writer is already running"
                    ) from exc
                time.sleep(max(min(float(poll_interval_seconds), 1.0), 0.01))
        held[lock_path] = {"depth": 1, "handle": handle}
        try:
            yield {
                "wait_ms": round((time.monotonic() - started) * 1000, 3),
                "reentrant": 0.0,
            }
        finally:
            held.pop(lock_path, None)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
    finally:
        process_lock.release()


@contextmanager
def warehouse_functional_job_lock(
    runtime_dir: Path,
    *,
    blocking: bool = False,
    timeout_seconds: float | None = None,
) -> Iterator[dict[str, Any]]:
    """Serialize top-level sync jobs without blocking interactive writers.

    Heavy capture, planning and Finance work must not hold the shared
    ``.warehouse-functional-sync.lock``.  This separate process/file identity
    preserves hourly/manual single-flight while every actual warehouse write
    continues through its own short canonical writer/CAS boundary.
    """

    with _named_functional_lock(
        runtime_dir,
        filename=WAREHOUSE_FUNCTIONAL_JOB_LOCK_FILENAME,
        blocking=blocking,
        timeout_seconds=timeout_seconds,
    ) as evidence:
        yield evidence


@contextmanager
def _named_functional_lock(
    runtime_dir: Path,
    *,
    filename: str,
    blocking: bool,
    timeout_seconds: float | None,
    poll_interval_seconds: float = 0.1,
) -> Iterator[dict[str, Any]]:
    started = time.monotonic()
    lock_path = (Path(runtime_dir) / filename).resolve()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    process_lock = _process_lock(lock_path)
    if not blocking:
        process_lock_acquired = process_lock.acquire(blocking=False)
    elif timeout_seconds is None:
        process_lock_acquired = process_lock.acquire()
    else:
        process_lock_acquired = process_lock.acquire(
            timeout=max(float(timeout_seconds), 0.0)
        )
    if not process_lock_acquired:
        raise WarehouseFunctionalBusyError("functional warehouse job is already running")
    handle = None
    try:
        handle = lock_path.open("a+", encoding="utf-8")
        deadline = (
            None
            if timeout_seconds is None
            else started + max(float(timeout_seconds), 0.0)
        )
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if not blocking or (
                    deadline is not None and time.monotonic() >= deadline
                ):
                    raise WarehouseFunctionalBusyError(
                        "functional warehouse job is already running"
                    ) from exc
                time.sleep(max(min(float(poll_interval_seconds), 1.0), 0.01))
        acquired_at = time.monotonic()
        evidence = {
            "wait_ms": round((acquired_at - started) * 1000, 3),
            "hold_ms": 0.0,
            "lock_identity": filename,
        }
        try:
            yield evidence
        finally:
            evidence["hold_ms"] = round((time.monotonic() - acquired_at) * 1000, 3)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        if handle is not None:
            handle.close()
        process_lock.release()
