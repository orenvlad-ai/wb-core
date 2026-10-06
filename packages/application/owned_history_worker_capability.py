"""Private Linux history-child transport. This conveys no business admission.

Only fixed history operations use this channel. Kernel process/descriptor
proofs are checked before source construction. Unsupported platforms refuse.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import struct
import sys

CONTRACT = "owned_history_portion_fd_v1"
MAX_MESSAGE = 256 * 1024
MAX_JSON_FILE = 16 * 1024 * 1024


class HistoryDelegationError(RuntimeError):
    pass


def linux_required():
    if (sys.platform != "linux" or sys.implementation.name != "cpython"
            or not hasattr(socket, "SO_PEERCRED")):
        raise HistoryDelegationError("history_delegation_linux_required")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def process_generation(pid):
    try:
        value = Path(f"/proc/{pid}/stat").read_text()
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + value[value.rfind(")") + 2:].split()[19]
    except (OSError, IndexError):
        raise HistoryDelegationError("history_process_generation_unavailable") from None


def read_json(path, *, limit=MAX_JSON_FILE):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise HistoryDelegationError("history_json_file_unsafe")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise HistoryDelegationError("history_json_file_oversized")
        return json.loads(data)
    finally:
        os.close(fd)


def file_digest(path, *, limit=1024 * 1024):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise HistoryDelegationError("history_bound_file_unsafe")
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise HistoryDelegationError("history_bound_file_oversized")
        return hashlib.sha256(data).hexdigest()
    finally:
        os.close(fd)


def send_message(channel, value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > MAX_MESSAGE:
        raise HistoryDelegationError("history_capability_message_oversized")
    channel.sendall(struct.pack("!I", len(data)) + data)


def receive_message(channel):
    def exact(size):
        result = bytearray()
        while len(result) < size:
            part = channel.recv(size - len(result))
            if not part:
                raise HistoryDelegationError("history_capability_channel_closed")
            result.extend(part)
        return bytes(result)
    size = struct.unpack("!I", exact(4))[0]
    if not 0 < size <= MAX_MESSAGE:
        raise HistoryDelegationError("history_capability_message_oversized")
    return json.loads(exact(size))


def lock_proof(fd, path, *, mode, parent_pid):
    """Read Linux fdinfo; never acquire, convert or unlock a supplied FD."""
    linux_required()
    path = Path(path)
    actual, opened = path.lstat(), os.fstat(fd)
    if (not stat.S_ISREG(opened.st_mode) or opened.st_mode & 0o007
            or (actual.st_dev, actual.st_ino) != (opened.st_dev, opened.st_ino)):
        raise HistoryDelegationError("history_lock_descriptor_unsafe")
    # FLOCK records are tied to this open file description (not just the inode).
    value = Path(f"/proc/self/fdinfo/{fd}").read_text()
    expected = f"{os.major(opened.st_dev):02x}:{os.minor(opened.st_dev):02x}:{opened.st_ino}"
    records = [line.split() for line in value.splitlines() if line.startswith("lock:")]
    if not any(len(row) == 9 and row[2:6] == ["FLOCK", "ADVISORY", mode, str(parent_pid)]
               and row[6:] == [expected, "0", "EOF"] for row in records):
        raise HistoryDelegationError("history_lock_not_held_by_parent")
    return {"path": str(path), "device": opened.st_dev, "inode": opened.st_ino, "mode": mode}


def child_bootstrap(fd):
    """Before imports/source work: authenticate the peer and arm parent death."""
    linux_required()
    channel = socket.socket(fileno=fd)
    if channel.family != socket.AF_UNIX or channel.type != socket.SOCK_STREAM:
        raise HistoryDelegationError("history_capability_socket_required")
    parent_pid, uid, _ = struct.unpack("3i", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if parent_pid != os.getppid() or uid != os.getuid():
        raise HistoryDelegationError("history_capability_peer_mismatch")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise HistoryDelegationError("history_parent_death_signal_unavailable")
    if os.getppid() != parent_pid:  # Covers death before/during prctl.
        raise HistoryDelegationError("history_parent_lost_during_bootstrap")
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
    channel.settimeout(10)
    cap = receive_message(channel)
    if (not isinstance(cap, dict) or cap.get("contract") != CONTRACT
            or cap.get("mode") not in {"capture", "portion", "verify"}
            or re.fullmatch(r"[0-9a-f]{32}", str(cap.get("invocation", ""))) is None
            or cap.get("parent_pid") != parent_pid or cap.get("child_pid") != os.getpid()
            or cap.get("parent_generation") != process_generation(parent_pid)
            or cap.get("child_generation") != process_generation(os.getpid())
            or os.getppid() != parent_pid):
        raise HistoryDelegationError("history_capability_binding_mismatch")
    locks = cap.get("locks", [])
    if len(locks) != 4 or len({item["fd"] for item in locks}) != 4:
        raise HistoryDelegationError("history_capability_locks_missing")
    runtime, root = Path(cap["runtime_dir"]), Path(cap["candidate_root"])
    paths = [runtime / ".business-data-procedure-admission.lock",
             runtime / ".business-data-heavy-admission.lock",
             runtime / ".web-vitrina-finished-builder.lock", root / "candidate.lock"]
    for item, path, mode in zip(locks, paths, ["READ", "WRITE", "WRITE", "WRITE"], strict=True):
        if item["proof"] != lock_proof(item["fd"], path, mode=mode, parent_pid=parent_pid):
            raise HistoryDelegationError("history_capability_lock_changed")
    owner = cap.get("cycle_owner")
    if cap.get("owner_kind", "cycle") == "cycle":
        # All four descriptor proofs precede this exact marker check.
        if (not isinstance(owner, dict) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(owner.get("job_id", ""))) is None
                or owner != {"pid": parent_pid, "identity": cap["parent_generation"],
                             "job_id": owner.get("job_id"), "operation": "cycle"}):
            raise HistoryDelegationError("history_capability_cycle_owner_mismatch")
        if (read_json(runtime / "finished-snapshot-api-jobs" / "ready.json", limit=4096)
                != {"pid": parent_pid, "identity": cap["parent_generation"]}
                or read_json(runtime / "finished-snapshot-api-jobs" / ("job-" + owner["job_id"] + ".json"), limit=4096) != owner):
            raise HistoryDelegationError("history_capability_cycle_marker_changed")
    elif cap.get("owner_kind") != "history" or owner is not None or cap.get("closed_receipt") is not None:
        raise HistoryDelegationError("history_capability_standalone_owner_mismatch")
    return channel, cap
