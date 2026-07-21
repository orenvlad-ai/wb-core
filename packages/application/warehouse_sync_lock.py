"""Cross-process lock shared by hourly and operator warehouse synchronization."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
from pathlib import Path
from typing import Iterator


class WarehouseSyncBusyError(RuntimeError):
    """Raised when an operator sync would overlap the bounded hourly pipeline."""


@contextmanager
def warehouse_sync_lock(runtime_dir: Path, *, blocking: bool = True) -> Iterator[None]:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    lock_path = runtime_dir / ".warehouse-functional-sync.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError as exc:
            raise WarehouseSyncBusyError(
                "Почасовое обновление уже выполняется; повторный параллельный запуск запрещён"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
