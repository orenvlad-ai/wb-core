"""Bounded Finance backup priority and one durable deferred admission intent.

Reads only private metadata and source stat identities, never SQLite contents or
backup byte hashes. Priority is consumed by future integrated heavy actors; it
does not itself schedule a job or authorize an unapproved backup.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat

from packages.application.business_data_procedure_admission import admitted_write
from packages.application.business_data_write_barrier import _atomic_write_private_json
from packages.application.finance_storage_snapshot_retention import ARCHIVE_RELATIVE_ROOT, _fingerprint


STATE_FILENAME = ".finance-backup-admission.json"
LOCK_FILENAME = ".finance-backup-admission-intent.lock"
CONTRACT = "finance_backup_admission_v1"
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_TRANSACTION_COUNT = 128
MAX_TRANSACTION_BYTES = 16 * 1024 * 1024


class BackupAdmissionStateError(RuntimeError):
    pass


def _read(path: Path, *, optional: bool = False, private: bool = True) -> dict | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if optional:
            return None
        raise
    try:
        value = os.fstat(fd)
        if (not stat.S_ISREG(value.st_mode) or (private and value.st_mode & 0o077)
                or value.st_size > MAX_METADATA_BYTES):
            raise BackupAdmissionStateError("backup admission metadata is unsafe or exceeds bound")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_METADATA_BYTES + 1)
        if len(raw) > MAX_METADATA_BYTES:
            raise BackupAdmissionStateError("backup admission metadata grew beyond bound")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise BackupAdmissionStateError("backup admission metadata is not an object")
        return payload
    finally:
        os.close(fd)


def _signed(value: dict, contract: str) -> dict:
    stable = {key: item for key, item in value.items() if key != "fingerprint"}
    if value.get("contract_version") != contract or value.get("fingerprint") != _fingerprint(stable):
        raise BackupAdmissionStateError("backup admission metadata contract/fingerprint mismatch")
    return value


def _time(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise BackupAdmissionStateError("backup admission time lacks timezone")
    return value.astimezone(timezone.utc)


def _read_intent(runtime: Path) -> dict | None:
    intent = _read(runtime / STATE_FILENAME, optional=True)
    if intent is not None:
        _signed(intent, CONTRACT)
        if intent.get("status") not in {"waiting", "resolved"}:
            raise BackupAdmissionStateError("backup intent status is invalid")
    return intent


def backup_admission_request_id(runtime_dir: Path) -> str | None:
    """RO exact request to acknowledge; not a backup inventory authorization.

    Manual canonical proof/recovery must reach its detailed transaction guards,
    including safe pre-mutation supersession of multiple started transactions.
    """
    intent = _read_intent(Path(runtime_dir).resolve())
    return intent.get("request_id") if intent and intent["status"] == "waiting" else None


def backup_admission_priority(
    runtime_dir: Path, *, now: datetime | None = None,
    duration_budget_seconds: int | None = None, next_actor_budget_seconds: int = 0,
) -> dict:
    """RO priority hint; authoritative due/CAS is rebuilt under the heavy lease.

    RTO is recovery policy, not backup duration. An absent independent duration
    budget produces no latest-safe-start claim and timing_proven=False.
    """
    from packages.application import finance_storage_backup_rotation as rotation
    from packages.application.storage_registry import StoreRegistry, MANIFEST_FILENAME, parse_manifest

    runtime = Path(runtime_dir).resolve()
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None or next_actor_budget_seconds < 0:
        raise ValueError("aware time and nonnegative actor budget are required")
    if duration_budget_seconds is not None and duration_budget_seconds <= 0:
        raise ValueError("backup duration budget must be positive or unknown")
    result = {"contract_version": CONTRACT, "ready": True, "priority": False,
              "reason": "policy_inert", "timing_proven": False,
              "latest_safe_start_at": None, "duration_budget_seconds": duration_budget_seconds,
              "pending_transaction": None, "intent": None, "source_changed_hint": None}
    try:
        root = runtime / ARCHIVE_RELATIVE_ROOT
        if root.is_symlink():
            raise BackupAdmissionStateError("backup root is a symlink")
        intent = _read_intent(runtime)
        result["intent"] = intent
        pending = []
        transactions = root / rotation.TRANSACTIONS_DIRECTORY
        if transactions.exists():
            if transactions.is_symlink() or not transactions.is_dir():
                raise BackupAdmissionStateError("backup transaction directory is unsafe")
            total = 0
            with os.scandir(transactions) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_TRANSACTION_COUNT or not re.fullmatch(r"[0-9a-f]{64}\.json", entry.name):
                        raise BackupAdmissionStateError("backup transaction inventory exceeds bound or is unsafe")
                    value = _read(Path(entry.path))
                    total += entry.stat(follow_symlinks=False).st_size
                    if total > MAX_TRANSACTION_BYTES or value.get("contract_version") != rotation.TRANSACTION_CONTRACT:
                        raise BackupAdmissionStateError("backup transaction metadata exceeds bound or has unknown contract")
                    if value.get("phase") != "completed":
                        if (value.get("plan_fingerprint") != "sha256:" + entry.name[:-5]
                                or re.fullmatch(r"[0-9a-f]{40}", str(value.get("deployed_sha") or "")) is None):
                            raise BackupAdmissionStateError("pending backup producer identity is invalid")
                        pending.append({"plan_fingerprint": value.get("plan_fingerprint"),
                                        "deployed_sha": value.get("deployed_sha"), "phase": value.get("phase")})
            if len(pending) > 1:
                raise BackupAdmissionStateError("multiple pending backup transactions")
        if pending:
            result.update(priority=True, reason="pending_exact_transaction", pending_transaction=pending[0])
        policy = _read(root / rotation.POLICY_FILENAME, optional=True)
        if policy is None:
            return result
        _signed(policy, rotation.POLICY_CONTRACT)
        if policy.get("enabled") is not True or not str(policy.get("approval_reference") or "").strip():
            raise BackupAdmissionStateError("backup policy is not explicitly approved/enabled")
        settings = dict(policy.get("policy") or {})
        minimum = int(settings.get("minimum_interval_seconds", rotation.DEFAULT_MIN_REPLACEMENT_INTERVAL_SECONDS))
        rpo = int(settings.get("rpo_seconds", rotation.DEFAULT_MAX_AGE_SECONDS))
        age_cap = int(settings.get("age_cap_seconds", rotation.DEFAULT_MAX_AGE_SECONDS))
        if min(minimum, rpo, age_cap) < 0:
            raise BackupAdmissionStateError("backup policy timing is invalid")
        result.update(policy_fingerprint=policy["fingerprint"], policy_approval=policy["approval_reference"],
                      minimum_interval_seconds=minimum, rpo_seconds=rpo,
                      rto_seconds=int(settings.get("rto_seconds", 4 * 60 * 60)))
        selector = _read(root / rotation.CURRENT_FILENAME)
        _signed(selector, rotation.CURRENT_CONTRACT)
        backup_id = str(selector.get("backup_id") or "")
        if rotation._BACKUP_ID_RE.fullmatch(backup_id) is None:
            raise BackupAdmissionStateError("backup selector identity is invalid")
        selected = root / rotation.RETAINED_DIRECTORY / backup_id
        if selected.is_symlink() or selected.parent.is_symlink():
            raise BackupAdmissionStateError("backup selected directory is unsafe")
        manifest = _read(selected / rotation.BACKUP_MANIFEST_FILENAME)
        _signed(manifest, rotation.BACKUP_SET_CONTRACT)
        if (manifest.get("status") != "verified" or manifest.get("backup_id") != backup_id
                or selector.get("backup_manifest_fingerprint") != manifest["fingerprint"]):
            raise BackupAdmissionStateError("backup selected metadata is inconsistent")
        captured = _time(str(manifest["captured_at"]))
        eligible = captured + timedelta(seconds=minimum)
        deadline = captured + timedelta(seconds=min(rpo, age_cap))
        latest = deadline - timedelta(seconds=duration_budget_seconds) if duration_budget_seconds else None
        source_manifest = parse_manifest(_read(runtime / MANIFEST_FILENAME, private=False))
        if source_manifest.state != "cutover" or source_manifest.canonical_source != "split":
            raise BackupAdmissionStateError("backup source is not canonical split")
        source = {"manifest_sha256": source_manifest.manifest_sha256}
        changed = manifest.get("source_manifest_sha256") != source_manifest.manifest_sha256
        for logical, name in (("finance_raw", "raw"), ("operational", "operational")):
            path = StoreRegistry(runtime).resolve(logical, manifest=source_manifest)
            if not path.is_relative_to(runtime / "generations"):
                raise BackupAdmissionStateError("backup source escapes canonical generations")
            source[name] = rotation._sqlite_source_identity(path)
            expected = (manifest.get("source_identity") or {}).get(name) or {}
            changed = changed or any(source[name].get(key) != expected.get(key)
                                     for key in ("device", "inode", "size_bytes", "mtime_ns", "sidecars"))
        result.update(current_fingerprint=selector["fingerprint"], captured_at=captured.isoformat(),
                      eligible_at=eligible.isoformat(), deadline_at=deadline.isoformat(),
                      latest_safe_start_at=latest.isoformat() if latest else None,
                      timing_proven=latest is not None, source_changed_hint=bool(changed),
                      source_stat_fingerprint=_fingerprint(source))
        waiting = intent is not None and intent.get("status") == "waiting" and intent.get("current_fingerprint") == selector["fingerprint"]
        due = instant >= eligible and changed
        at_deadline = instant >= deadline
        reserved = latest is not None and instant + timedelta(seconds=next_actor_budget_seconds) >= latest
        if pending or waiting or due or at_deadline or reserved:
            result.update(priority=True, reason=("pending_exact_transaction" if pending else
                          "deferred_backup_request" if waiting else "rpo_deadline" if at_deadline else
                          "latest_safe_start" if reserved else "eligible_source_changed"))
        else:
            result["reason"] = "not_due_hint"
        return result
    except (OSError, ValueError, KeyError, TypeError, BackupAdmissionStateError,
            rotation.FinanceStorageBackupRotationError) as exc:
        result.update(ready=False, priority=True, reason="backup_metadata_unproven",
                      error=f"{type(exc).__name__}: {str(exc)[:200]}")
        return result


def _update(runtime: Path, update) -> dict | None:
    """Short serialized private metadata mutation; never wait under SH."""
    with admitted_write(runtime):
        fd = os.open(runtime / LOCK_FILENAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            value = os.fstat(fd)
            if not stat.S_ISREG(value.st_mode) or value.st_mode & 0o077:
                raise BackupAdmissionStateError("backup intent lock is unsafe")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BackupAdmissionStateError("backup intent metadata writer is busy") from exc
            previous = _read(runtime / STATE_FILENAME, optional=True)
            if previous is not None:
                _signed(previous, CONTRACT)
            result = update(previous)
            if result is not None:
                result.pop("fingerprint", None)
                result["fingerprint"] = _fingerprint(result)
                _atomic_write_private_json(runtime / STATE_FILENAME, result)
            return result
        finally:
            os.close(fd)


def defer_backup_admission(runtime_dir: Path, *, deployed_sha: str) -> dict:
    runtime = Path(runtime_dir).resolve()
    state = backup_admission_priority(runtime)
    if not state["ready"]:
        raise BackupAdmissionStateError(state["error"])
    if not state.get("policy_fingerprint") or not state["priority"]:
        return {"status": "deferred", "reason": "heavy_producer_running", "intent_recorded": False,
                "admission": state, "mutation_count": 0}
    def update(previous):
        if previous is not None and previous.get("status") == "waiting" and previous.get("current_fingerprint") == state["current_fingerprint"]:
            return previous  # Same due request survives contenders and restart.
        result = {"contract_version": CONTRACT, "status": "waiting", "deployed_sha": deployed_sha,
                  "requested_at": datetime.now(timezone.utc).isoformat(),
                  **{key: state.get(key) for key in ("current_fingerprint", "policy_fingerprint", "captured_at",
                     "eligible_at", "deadline_at", "latest_safe_start_at", "duration_budget_seconds")}}
        result["request_id"] = _fingerprint([str(runtime), result["current_fingerprint"], result["policy_fingerprint"]])
        return result
    intent = _update(runtime, update)
    return {"status": "deferred", "reason": "heavy_producer_running", "intent_recorded": True,
            "request_id": intent["request_id"], "admission": state, "mutation_count": 0}


def resolve_backup_admission(runtime_dir: Path, *, request_id: str | None, terminal_status: str) -> None:
    if not request_id or terminal_status not in {"completed", "not_due"}:
        return
    def update(previous):
        if previous is None or previous.get("request_id") != request_id or previous.get("status") != "waiting":
            return None
        return {**previous, "status": "resolved", "resolved_at": datetime.now(timezone.utc).isoformat(),
                "terminal_status": terminal_status}
    _update(Path(runtime_dir).resolve(), update)
