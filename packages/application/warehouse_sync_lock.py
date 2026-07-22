"""Cross-process lock shared by hourly and operator warehouse synchronization."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from packages.application.warehouse_functional_lock import (
    WarehouseFunctionalBusyError,
    warehouse_functional_write_lock,
)


class WarehouseSyncBusyError(RuntimeError):
    """Raised when an operator sync would overlap the bounded hourly pipeline."""


@contextmanager
def warehouse_sync_lock(runtime_dir: Path, *, blocking: bool = True) -> Iterator[None]:
    try:
        with warehouse_functional_write_lock(runtime_dir, blocking=blocking):
            yield
    except WarehouseFunctionalBusyError as exc:
        raise WarehouseSyncBusyError(
            "Почасовое обновление уже выполняется; повторный параллельный запуск запрещён"
        ) from exc
