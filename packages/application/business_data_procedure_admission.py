"""Shared admission leases for business writers; reads never provision files."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
from functools import wraps
import json
import os
import stat
from pathlib import Path
import sys
from typing import Callable, Iterator

from packages.application.business_data_write_barrier import barrier_status


LOCK_FILENAME = ".business-data-procedure-admission.lock"
_ADMITTED: ContextVar[tuple[int, str] | None] = ContextVar("business_write_admission", default=None)


class MaintenanceAdmissionBlocked(RuntimeError):
    pass


def initialize_admission(runtime_dir: Path) -> None:
    """Explicit infrastructure startup only, never a status/read operation."""
    path = Path(runtime_dir) / LOCK_FILENAME
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_mode & 0o077:
            raise RuntimeError("procedure admission lock must be private")
    finally:
        os.close(fd)


def already_admitted(runtime_dir: Path) -> bool:
    return _ADMITTED.get() == (os.getpid(), str(Path(runtime_dir).resolve()))


def business_write_is_blocked(runtime_dir: Path) -> bool:
    return not already_admitted(runtime_dir) and bool(barrier_status(runtime_dir)["active"])


class AdmissionLease:
    """One independent open-file description; transferable to a worker thread."""

    def __init__(self, runtime_dir: Path, *, independent: bool = False) -> None:
        self.runtime_dir = Path(runtime_dir).resolve()
        self.pid = os.getpid()
        self.fd: int | None = None
        if business_write_is_blocked(self.runtime_dir):
            raise MaintenanceAdmissionBlocked("skipped_maintenance")
        inherited_authority = already_admitted(self.runtime_dir)
        if inherited_authority and not independent:
            return
        path = self.runtime_dir / LOCK_FILENAME
        # Preserve pre-infrastructure compatibility. A pause preflight requires
        # the explicitly initialized lock and the new HTTP readiness contract.
        if not path.exists():
            return
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_mode & 0o077:
                raise MaintenanceAdmissionBlocked("admission_lock_not_private")
            fcntl.flock(fd, fcntl.LOCK_SH)
            if not inherited_authority and bool(barrier_status(self.runtime_dir)["active"]):
                raise MaintenanceAdmissionBlocked("skipped_maintenance")
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd

    def close(self) -> None:
        if self.fd is not None:
            # No explicit LOCK_UN: forked copies must not unlock the parent's
            # shared open-file description. Closing drops only this reference.
            os.close(self.fd)
            self.fd = None

    @contextmanager
    def entered(self) -> Iterator[None]:
        if os.getpid() != self.pid:
            raise MaintenanceAdmissionBlocked("admission_lease_belongs_to_parent_process")
        token = _ADMITTED.set((self.pid, str(self.runtime_dir)))
        try:
            yield
        finally:
            _ADMITTED.reset(token)


@contextmanager
def admitted_write(runtime_dir: Path) -> Iterator[None]:
    lease = AdmissionLease(runtime_dir)
    try:
        with lease.entered():
            yield
    finally:
        lease.close()


def admission_idle(runtime_dir: Path) -> dict:
    """Read-only nonblocking drain proof; never creates an absent lock."""
    path = Path(runtime_dir) / LOCK_FILENAME
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        return {"ready": False, "idle": False, "reason": type(exc).__name__}
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_mode & 0o077:
            return {"ready": False, "idle": False, "reason": "lock_not_private"}
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"ready": True, "idle": False, "reason": "admitted_writer_running"}
        return {"ready": True, "idle": True, "reason": ""}
    finally:
        os.close(fd)


def guarded_cli(main: Callable, *, default_runtime: str = ".runtime/registry_upload", argv=None):
    """Guard a supported writer CLI before parsing constructs any writer."""
    args = sys.argv[1:] if argv is None else list(argv)
    if "--help" in args or "-h" in args:
        return main()
    runtime = os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR", default_runtime)
    for index, arg in enumerate(args):
        if arg == "--runtime-dir" and index + 1 < len(args):
            runtime = args[index + 1]
        elif arg.startswith("--runtime-dir="):
            runtime = arg.split("=", 1)[1]
    try:
        with admitted_write(Path(runtime)):
            return main()
    except MaintenanceAdmissionBlocked as exc:
        print(json.dumps({"status": "skipped_maintenance", "reason": str(exc)}))
        return 0


def guard_cli(*, default_runtime: str = ".runtime/registry_upload"):
    def decorate(function):
        @wraps(function)
        def guarded(*args, **kwargs):
            argv = kwargs.get("argv")
            if args and isinstance(args[0], (list, tuple)):
                argv = args[0]
            return guarded_cli(lambda: function(*args, **kwargs), default_runtime=default_runtime, argv=argv)
        return guarded
    return decorate


def admitted_thread(runtime_dir: Path, **options):
    """Retain an independently admitted async operation through target finally."""
    import threading
    lease = AdmissionLease(runtime_dir, independent=True)
    target = options.pop("target")
    args = options.pop("args", ())
    kwargs = options.pop("kwargs", {})
    class AdmittedThread(threading.Thread):
        def start(self):
            try:
                return super().start()
            except BaseException:
                lease.close()
                raise
    def run():
        try:
            with lease.entered():
                target(*args, **kwargs)
        finally:
            lease.close()
    return AdmittedThread(target=run, **options)


def admitted_status(runtime_dir: Path, *, live_reader: Callable, cached_reader: Callable):
    """Status remains readable; optional reconciliation owns a complete lease."""
    try:
        lease = AdmissionLease(runtime_dir)
    except (MaintenanceAdmissionBlocked, OSError):
        return cached_reader()
    try:
        with lease.entered():
            return live_reader()
    finally:
        lease.close()
