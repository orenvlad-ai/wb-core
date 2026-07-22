"""Process-wide serialization for all functional warehouse writers."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
from pathlib import Path
import threading
from typing import Iterator


WAREHOUSE_FUNCTIONAL_LOCK_FILENAME = ".warehouse-functional-sync.lock"
_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[Path, threading.RLock] = {}
_LOCAL = threading.local()


def _process_lock(lock_path: Path) -> threading.RLock:
    with _LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(lock_path, threading.RLock())


@contextmanager
def warehouse_functional_write_lock(runtime_dir: Path) -> Iterator[None]:
    """Serialize every functional writer, including nested common-boundary calls."""

    lock_path = (Path(runtime_dir) / WAREHOUSE_FUNCTIONAL_LOCK_FILENAME).resolve()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    process_lock = _process_lock(lock_path)
    with process_lock:
        held = getattr(_LOCAL, "warehouse_functional_locks", None)
        if held is None:
            held = {}
            _LOCAL.warehouse_functional_locks = held
        state = held.get(lock_path)
        if state is not None:
            state["depth"] += 1
            try:
                yield
            finally:
                state["depth"] -= 1
            return

        handle = lock_path.open("a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        held[lock_path] = {"depth": 1, "handle": handle}
        try:
            yield
        finally:
            held.pop(lock_path, None)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
