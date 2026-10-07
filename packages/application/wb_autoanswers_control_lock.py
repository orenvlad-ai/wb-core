"""Serialize scheduler work and explicit recovery without stopping read-only GETs."""
from contextlib import contextmanager
import fcntl
from pathlib import Path


@contextmanager
def autoanswers_control_lock(runtime_dir: Path):
    path = Path(runtime_dir) / ".wb-autoanswers-control.lock"
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
