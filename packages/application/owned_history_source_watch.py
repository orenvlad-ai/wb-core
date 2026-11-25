"""Observe local Linux file changes across the authenticated History verification."""
from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import struct
import sys

from packages.application.owned_history_worker_capability import HistoryDelegationError
from packages.application.storage_registry import MANIFEST_FILENAME


def source_paths(runtime_dir: Path, db_path: Path) -> tuple[Path, ...]:
    root = Path(runtime_dir).resolve()
    db = Path(db_path).resolve()
    book = root / "fbs-snapshot-accounting.sqlite3"
    return (db, Path(str(db) + "-wal"), book, Path(str(book) + "-wal"),
            root / MANIFEST_FILENAME, root / ".auto-updates-policy.json",
            root / ".web-vitrina-fbs-lifecycle-last-good.json")


# linux/inotify.h. Do not subscribe to accesses, opens, or unmodified closes.
_MODIFY, _ATTRIB = 0x2, 0x4
_PATH_CHANGES = 0x40 | 0x80 | 0x100 | 0x200  # move from/to, create, delete
_SELF_CHANGES = 0x400 | 0x800  # delete/move self
_UNMOUNT, _OVERFLOW, _IGNORED = 0x2000, 0x4000, 0x8000
_ONLYDIR, _DONT_FOLLOW = 0x01000000, 0x02000000
_EVENTS = _MODIFY | _ATTRIB | _PATH_CHANGES | _SELF_CHANGES
_HEADER = struct.Struct("=iIII")


class HistorySourceWatch:
    """The parent owns one queue. The verify child arms and drains its inherited copy."""

    def __init__(self, runtime_dir, db_path, *, inherited=None):
        self.paths = source_paths(runtime_dir, db_path)
        self.directories = tuple(dict.fromkeys(path.parent for path in self.paths))
        self.directory_names = {}
        self.file_roles = {}
        self.invalid = False
        self.fd = None
        # Native child delegation requires Linux. In-process portable readers
        # retain their existing stat, source revision and digest checks.
        if sys.platform != "linux":
            return
        self.lib = ctypes.CDLL(None, use_errno=True)
        if inherited is None:
            fd = self.lib.inotify_init1(os.O_CLOEXEC | os.O_NONBLOCK)
            if fd < 0:
                raise HistoryDelegationError("history_source_stamp_unsafe")
        else:
            if (type(inherited) is not int or inherited < 0
                    or os.readlink(f"/proc/self/fd/{inherited}") != "anon_inode:inotify"
                    or os.get_blocking(inherited)):
                raise HistoryDelegationError("history_source_stamp_unsafe")
            fd = os.dup(inherited)
        self.fd = fd

    def _add(self, path, mask, *, allow_absent=False):
        wd = self.lib.inotify_add_watch(self.fd, os.fsencode(path), mask | _DONT_FOLLOW)
        if wd < 0:
            if allow_absent and ctypes.get_errno() == errno.ENOENT:
                return None
            raise HistoryDelegationError("history_source_stamp_unsafe")
        return wd

    def bind_directories(self, ids):
        if self.fd is None:
            return
        if (not isinstance(ids, list) or len(ids) != len(self.directories)
                or any(type(wd) is not int or wd < 1 for wd in ids) or len(set(ids)) != len(ids)):
            raise HistoryDelegationError("history_source_stamp_unsafe")
        self.directory_names = {
            wd: {os.fsencode(path.name): role for role, path in enumerate(self.paths) if path.parent == directory}
            for directory, wd in zip(self.directories, ids, strict=True)
        }

    def arm(self):
        if self.fd is None:
            return []
        ids = [self._add(directory, _EVENTS | _ONLYDIR) for directory in self.directories]
        self.bind_directories(ids)
        self.watch_current_files()
        return ids

    def watch_current_files(self):
        if self.fd is None:
            return
        for role, path in enumerate(self.paths):
            wd = self._add(path, _MODIFY | _ATTRIB | _SELF_CHANGES, allow_absent=True)
            if wd is not None:
                if self.file_roles.setdefault(wd, role) != role:
                    raise HistoryDelegationError("history_source_stamp_unsafe")

    def check(self, *, allow_operational_writes=False, error="history_source_changed_during_readback"):
        if self.invalid:
            raise HistoryDelegationError(error)
        if self.fd is None:
            return
        # Fail closed on a flooded queue instead of starving the native deadline.
        for _ in range(64):
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                return
            if not data:
                break
            offset = 0
            while offset < len(data):
                wd, mask, _, size = _HEADER.unpack_from(data, offset)
                offset += _HEADER.size
                name = data[offset:offset + size].rstrip(b"\0")
                offset += size
                if mask & (_UNMOUNT | _OVERFLOW):
                    self.invalid = True
                    raise HistoryDelegationError(error)
                if wd in self.directory_names:
                    if not name:  # The directory itself was removed, moved or changed.
                        self.invalid = True
                        raise HistoryDelegationError(error)
                    role = self.directory_names[wd].get(name)
                    if role is None:
                        continue
                else:
                    # After handoff the parent needs no inode map: every event
                    # outside the named directory watches is a strict refusal.
                    role = self.file_roles.get(wd)
                if allow_operational_writes:
                    if role == 1:  # The operational WAL may also rotate.
                        continue
                    if role == 0 and not (mask & (_PATH_CHANGES | _SELF_CHANGES | _IGNORED)):
                        continue
                self.invalid = True
                raise HistoryDelegationError(error)
        self.invalid = True
        raise HistoryDelegationError(error)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
