"""Process-wide serialization for all functional warehouse writers."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import logging
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Iterator
from uuid import uuid4


WAREHOUSE_FUNCTIONAL_LOCK_FILENAME = ".warehouse-functional-sync.lock"
WAREHOUSE_FUNCTIONAL_JOB_LOCK_FILENAME = ".warehouse-functional-job.lock"
_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[Path, threading.RLock] = {}
_LOCAL = threading.local()


class WarehouseFunctionalBusyError(RuntimeError):
    """Raised when a non-blocking functional writer cannot acquire the lock."""


class WarehouseJobOwnershipError(RuntimeError):
    """No live admission belonging to this process, thread and scope."""


def require_warehouse_job_owner(runtime_dir: Path, token: str | None = None) -> str:
    path = (Path(runtime_dir) / WAREHOUSE_FUNCTIONAL_JOB_LOCK_FILENAME).resolve()
    state = getattr(_LOCAL, "warehouse_job_owner", None)
    if (state is None or state["path"] != path or state["pid"] != os.getpid()
            or state["thread"] != threading.get_ident() or state["handle"].closed
            or (token is not None and token != state["token"])):
        raise WarehouseJobOwnershipError("warehouse journal requires its live admission owner")
    return str(state["token"])


def _record_writer_metrics(evidence: dict[str, Any]) -> None:
    state = getattr(_LOCAL, "warehouse_job_owner", None)
    if state is not None and state["pid"] == os.getpid():
        metrics = state["metrics"]
        metrics["writer_count"] += int(evidence.get("outcome") != "busy")
        if evidence.get("outcome") == "busy":
            metrics["writer_busy_count"] += 1
        elif evidence.get("outcome") == "error":
            metrics["writer_error_count"] += 1
        for key in ("wait_ms", "hold_ms"):
            metrics["writer_" + key] = round(metrics["writer_" + key] + evidence[key], 3)


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
        _record_writer_metrics({"outcome": "busy", "wait_ms": round((time.monotonic() - started) * 1000, 3), "hold_ms": 0.0})
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
                    _record_writer_metrics({"outcome": "busy", "wait_ms": round((time.monotonic() - started) * 1000, 3), "hold_ms": 0.0})
                    raise WarehouseFunctionalBusyError(
                        "functional warehouse writer is already running"
                    ) from exc
                time.sleep(max(min(float(poll_interval_seconds), 1.0), 0.01))
        held[lock_path] = {"depth": 1, "handle": handle}
        acquired_at = time.monotonic()
        evidence = {"wait_ms": round((acquired_at - started) * 1000, 3),
                    "hold_ms": None, "reentrant": 0.0, "outcome": "running"}
        try:
            yield evidence
            evidence["outcome"] = "success"
        except BaseException:
            evidence["outcome"] = "error"
            raise
        finally:
            held.pop(lock_path, None)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            process_lock.release()
            process_lock_acquired = False
            evidence["hold_ms"] = round((time.monotonic() - acquired_at) * 1000, 3)
            _record_writer_metrics(evidence)
    finally:
        if process_lock_acquired:
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

    # Nested top-level admission is a caller error, never a second descriptor.
    current = getattr(_LOCAL, "warehouse_job_owner", None)
    if current is not None and current["pid"] == os.getpid():
        raise WarehouseJobOwnershipError("nested warehouse job admission is forbidden")
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
        _job_diagnostic({"outcome": "busy", "wait_ms": round((time.monotonic() - started) * 1000, 3), "hold_ms": 0.0})
        raise WarehouseFunctionalBusyError("functional warehouse job is already running")
    handle = None
    evidence = None
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
            "hold_ms": None,
            "lock_identity": filename,
            "owner_token": uuid4().hex,
            "writer_count": 0, "writer_wait_ms": 0.0, "writer_hold_ms": 0.0,
            "writer_busy_count": 0, "writer_error_count": 0,
            "outcome": "running",
        }
        _LOCAL.warehouse_job_owner = {
            "path": lock_path, "pid": os.getpid(), "thread": threading.get_ident(),
            "handle": handle, "token": evidence["owner_token"], "metrics": evidence,
        }
        try:
            yield evidence
            evidence["outcome"] = "success"
        except BaseException:
            evidence["outcome"] = "error"
            raise
        finally:
            _LOCAL.warehouse_job_owner = None
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except WarehouseFunctionalBusyError:
        if evidence is None:
            _job_diagnostic({"outcome": "busy", "wait_ms": round((time.monotonic() - started) * 1000, 3), "hold_ms": 0.0})
        raise
    finally:
        if handle is not None:
            handle.close()
        process_lock.release()
        if evidence is not None:
            evidence["hold_ms"] = round((time.monotonic() - acquired_at) * 1000, 3)
            _job_diagnostic(evidence)


def _job_diagnostic(evidence: dict[str, Any]) -> None:
    # Warning is captured by both the existing CLI stderr and HTTP service
    # journal without depending on an application-wide INFO configuration.
    logging.getLogger(__name__).warning("warehouse_job_lock_diagnostic %s", json.dumps(evidence, sort_keys=True))
