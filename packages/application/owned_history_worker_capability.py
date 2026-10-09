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
import sqlite3
import stat
import struct
import sys

CONTRACT = "owned_history_portion_fd_v1"
MAX_MESSAGE = 256 * 1024
MAX_JSON_FILE = 16 * 1024 * 1024


HISTORY_ERROR_CODES = frozenset({
    'policy_history_ack_not_supervised',
    'policy_history_ack_owner_or_target_changed',
    'policy_history_context_changed',
    'policy_history_native_anchor_changed',
    'policy_history_scope_changed',
    'policy_history_terminal_unproven',
    'policy_history_worker_binding_changed',
    'policy_history_worker_scope_changed',

    'history_multiple_source_authorities',
    'history_closed_exact_scope_changed',
    'supplier_history_context_changed',
    'supplier_history_scope_changed',
    'supplier_history_worker_binding_changed',
    'supplier_history_worker_scope_changed',
    'supplier_history_native_anchor_changed',
    'supplier_history_terminal_unproven',
    'supplier_history_ack_not_supervised',
    'supplier_history_ack_owner_or_target_changed',

    'historical_history_ack_not_supervised',
    'historical_history_ack_owner_or_target_changed',
    'historical_history_exact_scope_changed',
    'historical_history_native_anchor_changed',
    'historical_history_receipt_context_changed',
    'historical_history_terminal_unproven',
    'historical_history_worker_binding_changed',
    'historical_history_worker_scope_changed',
    'history_bound_file_oversized',
    'history_bound_file_unsafe',
    'history_budget_exhausted_before_spawn',
    'history_capability_binding_mismatch',
    'history_capability_channel_closed',
    'history_capability_cycle_marker_changed',
    'history_capability_cycle_owner_mismatch',
    'history_capability_lock_changed',
    'history_capability_locks_missing',
    'history_capability_message_oversized',
    'history_capability_peer_mismatch',
    'history_capability_socket_required',
    'history_capability_standalone_owner_mismatch',
    'history_capture_already_consumed',
    'history_capture_must_not_accept_anchor',
    'history_child_already_live',
    'history_child_terminal_unproven',
    'history_clock_requires_timezone',
    'history_closed_ack_not_supervised',
    'history_closed_ack_owner_or_target_changed',
    'history_closed_native_anchor_changed',
    'history_closed_receipt_changed',
    'history_closed_receipt_context_changed',
    'history_closed_receipt_required',
    'history_closed_terminal_unproven',
    'history_completion_already_consumed',
    'history_completion_capture_unproven',
    'history_completion_intent_unbound',
    'history_completion_limits_invalid',
    'history_completion_no_dated_progress',
    'history_completion_portion_failed',
    'history_completion_portion_limit',
    'history_completion_readback_unproven',
    'history_completion_target_changed',
    'history_completion_total_deadline',
    'history_cycle_backfill_scope_invalid',
    'history_dated_catalog_corrupt',
    'history_dated_cell_corrupt',
    'history_dated_cell_limit',
    'history_dated_metadata_invalid',
    'history_dated_object_corrupt',
    'history_dated_object_identity_invalid',
    'history_dated_object_unsafe',
    'history_dated_readback_deadline',
    'history_delegation_linux_required',
    'history_failure_unknown',
    'history_json_file_oversized',
    'history_json_file_unsafe',
    'history_lock_descriptor_unsafe',
    'history_lock_not_held_by_parent',
    'history_noncanonical_source',
    'history_parent_admission_busy_or_unknown',
    'history_parent_contract_invalid',
    'history_parent_death_signal_unavailable',
    'history_parent_lost_during_bootstrap',
    'history_parent_owner_changed',
    'history_portion_anchor_binding_mismatch',
    'history_portion_anchor_or_replay_refused',
    'history_process_generation_unavailable',
    'history_readback_changed_during_verify',
    'history_readback_current_superseded',
    'history_readback_current_unsafe',
    'history_readback_edition_corrupt',
    'history_readback_pending_target_changed',
    'history_source_changed_after_readback',
    'history_source_changed_during_readback',
    'history_source_stamp_unsafe',
    'history_spawn_children_proof_oversized',
    'history_stale_pending_catalog_corrupt',
    'history_stale_pending_dated_proof_invalid',
    'history_stale_pending_metadata_invalid',
    'history_standalone_backfill_outside_range',
    'history_standalone_limits_invalid',
    'history_supervisor_owner_mismatch',
    'history_transport_outcome_unknown',
    'history_worker_anchor_changed',
    'history_worker_base_catalog_changed',
    'history_worker_base_changed',
    'history_worker_binding_changed',
    'history_worker_catalog_context_changed',
    'history_worker_clock_invalid',
    'history_worker_contract_changed',
    'history_worker_cycle_date_scope_invalid',
    'history_worker_date_scope_invalid',
    'history_worker_exception',
    'history_worker_exception_connection',
    'history_worker_exception_index',
    'history_worker_exception_io',
    'history_worker_exception_key',
    'history_worker_exception_memory',
    'history_worker_exception_missing_file',
    'history_worker_exception_overflow',
    'history_worker_exception_permission',
    'history_worker_exception_sqlite',
    'history_worker_exception_sqlite_data',
    'history_worker_exception_sqlite_integrity',
    'history_worker_exception_sqlite_interface',
    'history_worker_exception_sqlite_operational',
    'history_worker_exception_sqlite_programming',
    'history_worker_exception_sqlite_unsupported',
    'history_worker_exception_timeout',
    'history_worker_exception_type',
    'history_worker_exception_value',
    'history_worker_formula_code_changed',
    'history_worker_invocation_already_submitted',
    'history_worker_job_changed',
    'history_worker_lock_changed',
    'history_worker_standalone_admission_changed',
    'history_worker_target_changed',
    'history_worker_terminal_proof_changed',
})

# Only known payload-free source codes may cross the child/parent boundary.
HISTORY_SOURCE_REASONS = frozenset({
    'accepted_ready_source_disappeared',
    'book_blob_content_unknown',
    'book_source_resource_limit',
    'bound_book_blob_missing',
    'bound_book_version_missing',
    'current_bundle_missing',
    'history_backfill_outside_source_range',
    'history_builder_busy',
    'history_catalog_columns_incompatible',
    'history_catalog_corrupt',
    'history_catalog_limit',
    'history_cell_corrupt',
    'history_cell_limit',
    'history_date_unavailable',
    'history_day_context_mismatch',
    'history_day_members_invalid',
    'history_day_rows_missing',
    'history_edition_corrupt',
    'history_future_date_unsupported',
    'history_group_candidate_catalog',
    'history_group_candidate_dates_missing',
    'history_group_candidate_deadline',
    'history_group_candidate_mixed_catalog',
    'history_group_candidate_object_corrupt',
    'history_group_candidate_pending',
    'history_group_candidate_preview_mismatch',
    'history_group_candidate_proof_incomplete',
    'history_group_candidate_same_root',
    'history_group_candidate_source_changed',
    'history_group_candidate_superseded',
    'history_not_ready',
    'history_read_deadline',
    'history_reply_limit',
    'history_row_identity_incompatible',
    'history_storage_limit',
    'invalid_dated_dependency',
    'invalid_dependency_vector',
    'lifecycle_quality_pin_closed',
    'live_bootstrap_deadline',
    'live_components_bootstrap_pending',
    'live_dated_bootstrap_pending',
    'live_inventory_capture_manifest_invalid',
    'live_ready_bootstrap_pending',
    'live_source_read_incomplete',
    'live_source_replaced_while_pinning',
    'live_source_resource_limit',
    'live_source_unavailable',
    'live_sqlite_family_unavailable',
    'live_sqlite_header_unknown',
    'live_sqlite_journal_unknown',
    'live_sqlite_versions_unknown',
    'live_sqlite_wal_family_unknown',
    'live_temporal_bootstrap_pending',
    'native_revision_trigger_unknown',
    'native_source_revision_missing',
    'ready_revision_missing',
    'snapshot_expired',
    'source_cache_resource_limit',
    'source_dependency_coverage_unknown',
    'temporal_revision_schema_missing',
})
HISTORY_MODES = frozenset({"parent", "capture", "verify", "portion"})

# Exact standard types only: custom exception class names are untrusted text.
HISTORY_EXCEPTION_REASONS = {
    ValueError: "history_worker_exception_value",
    TypeError: "history_worker_exception_type",
    KeyError: "history_worker_exception_key",
    IndexError: "history_worker_exception_index",
    OverflowError: "history_worker_exception_overflow",
    MemoryError: "history_worker_exception_memory",
    TimeoutError: "history_worker_exception_timeout",
    OSError: "history_worker_exception_io",
    FileNotFoundError: "history_worker_exception_missing_file",
    PermissionError: "history_worker_exception_permission",
    ConnectionError: "history_worker_exception_connection",
    BrokenPipeError: "history_worker_exception_connection",
    ConnectionResetError: "history_worker_exception_connection",
    ConnectionAbortedError: "history_worker_exception_connection",
    ConnectionRefusedError: "history_worker_exception_connection",
    sqlite3.Error: "history_worker_exception_sqlite",
    sqlite3.DatabaseError: "history_worker_exception_sqlite",
    sqlite3.OperationalError: "history_worker_exception_sqlite_operational",
    sqlite3.IntegrityError: "history_worker_exception_sqlite_integrity",
    sqlite3.ProgrammingError: "history_worker_exception_sqlite_programming",
    sqlite3.InterfaceError: "history_worker_exception_sqlite_interface",
    sqlite3.DataError: "history_worker_exception_sqlite_data",
    sqlite3.NotSupportedError: "history_worker_exception_sqlite_unsupported",
}

def safe_history_reason(value):
    return value if isinstance(value, str) and value in HISTORY_ERROR_CODES | HISTORY_SOURCE_REASONS else "history_failure_unknown"


class HistoryDelegationError(RuntimeError):
    """Payload-free typed diagnostics, revalidated at the durable boundary."""
    def __init__(self, code, *, mode="parent", reason_code=None, outcome="failed"):
        self.code = code if isinstance(code, str) and code in HISTORY_ERROR_CODES else "history_failure_unknown"
        self.mode = mode if isinstance(mode, str) and mode in HISTORY_MODES else "parent"
        self.reason_code = safe_history_reason(self.code if reason_code is None else reason_code)
        self.outcome = "outcome_unknown" if outcome == "outcome_unknown" else "failed"
        super().__init__(self.code)

    def diagnostic(self, *, mode=None):
        # Do not trust mutable exception attributes or a deserialized child dict.
        return {"error_code": self.code if isinstance(self.code, str) and self.code in HISTORY_ERROR_CODES else "history_failure_unknown",
                "mode": mode if isinstance(mode, str) and mode in HISTORY_MODES else self.mode if isinstance(self.mode, str) and self.mode in HISTORY_MODES else "parent",
                "reason_code": safe_history_reason(self.reason_code),
                "outcome": "outcome_unknown" if self.outcome == "outcome_unknown" else "failed"}


def history_result_error(code, result, *, mode):
    """Preserve a safe inner failure; transport ambiguity remains ambiguity."""
    unknown = result.get("status") == "outcome_unknown"
    payload = result.get("result", {})
    payload = payload if isinstance(payload, dict) else {}
    diagnostic = payload.get("diagnostic", {})
    diagnostic = diagnostic if isinstance(diagnostic, dict) else {}
    reason = "history_transport_outcome_unknown" if unknown else diagnostic.get("reason_code", payload.get("reason", code))
    return HistoryDelegationError(code, mode=mode, reason_code=reason,
                                  outcome="outcome_unknown" if unknown else "failed")


def history_child_failure(exc, *, mode, source_error=False):
    """A source exception's suffix may contain data; only its known prefix is used."""
    if isinstance(exc, HistoryDelegationError):
        diagnostic = exc.diagnostic(mode=mode)
    else:
        reason = HISTORY_EXCEPTION_REASONS.get(type(exc), "history_worker_exception")
        if source_error:
            source_reason = str(exc).partition(":")[0]
            if source_reason in HISTORY_SOURCE_REASONS:
                reason = source_reason
        diagnostic = HistoryDelegationError("history_worker_exception", mode=mode, reason_code=reason).diagnostic()
    return {"status": "failed", "reason": diagnostic["reason_code"], "diagnostic": diagnostic}


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
