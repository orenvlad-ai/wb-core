"""Fail-closed recovery for one unselected pre-manifest Finance candidate.

This boundary is deliberately narrower than candidate creation.  It can only
release an exact partial generation while the implicit monolith is canonical,
no candidate/global manifest exists, no shadow is active, and the persisted
candidate bytes still match a reviewed dry-run plan.  A durable transaction
outside the candidate directory makes interruption recovery idempotent.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator, Mapping

from packages.application.business_data_write_barrier import barrier_status
from packages.application.finance_storage_migration import (
    PLAN_CONTRACT as CANDIDATE_PLAN_CONTRACT,
    _digest,
    _plan_fingerprint,
)
from packages.application.storage_registry import (
    MANIFEST_FILENAME,
    MONOLITH_FILENAME,
    StoreRegistry,
)


PLAN_CONTRACT = "wb_core_finance_storage_candidate_abort_plan_v1"
RESULT_CONTRACT = "wb_core_finance_storage_candidate_abort_result_v1"
TRANSACTION_CONTRACT = (
    "wb_core_finance_storage_candidate_abort_transaction_v1"
)
MODE = "candidate_abort_dry_run"
TRANSACTION_DIRECTORY = ".finance-storage-candidate-aborts"
AUDIT_FILENAME = "candidate_abort_audit.jsonl"
MIGRATION_LOCK_FILENAME = ".finance-storage-split.lock"
CANDIDATE_MANIFEST_FILENAME = "candidate_generation_manifest.json"
SAVED_PLAN_FILENAME = "migration_plan.json"
RAW_FILENAME = "finance_raw.sqlite3"
OPERATIONAL_FILENAME = "operational.sqlite3"
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}")
_GENERATION_RE = re.compile(r"[0-9a-f]{20}")
_ALLOWED_NAMES = {
    SAVED_PLAN_FILENAME,
    RAW_FILENAME,
    OPERATIONAL_FILENAME,
    f"{RAW_FILENAME}-wal",
    f"{RAW_FILENAME}-shm",
    f"{RAW_FILENAME}-journal",
    f"{OPERATIONAL_FILENAME}-wal",
    f"{OPERATIONAL_FILENAME}-shm",
    f"{OPERATIONAL_FILENAME}-journal",
}
_DELETE_ORDER = (
    f"{RAW_FILENAME}-shm",
    f"{RAW_FILENAME}-wal",
    f"{RAW_FILENAME}-journal",
    f"{OPERATIONAL_FILENAME}-shm",
    f"{OPERATIONAL_FILENAME}-wal",
    f"{OPERATIONAL_FILENAME}-journal",
    RAW_FILENAME,
    OPERATIONAL_FILENAME,
    SAVED_PLAN_FILENAME,
)


class FinanceStorageCandidateAbortError(ValueError):
    """The partial-candidate recovery boundary is unsafe or ambiguous."""


class InjectedCandidateAbortFault(RuntimeError):
    """Test-only crash point after a durable per-file transition."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical_json(value).encode("utf-8")
    ).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(8 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise FinanceStorageCandidateAbortError(
            "candidate abort transaction root is unsafe"
        )
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        data = (
            json.dumps(
                dict(payload),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _append_audit(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise FinanceStorageCandidateAbortError(
            "candidate abort audit path is unsafe"
        )
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o600,
    )
    try:
        os.write(
            descriptor,
            (_canonical_json(dict(payload)) + "\n").encode("utf-8"),
        )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)
    _fsync_directory(path.parent)


def _load_json(
    path: Path,
    *,
    label: str,
    require_private: bool = False,
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise FinanceStorageCandidateAbortError(
            f"{label} is missing or unsafe"
        )
    if require_private and path.stat().st_mode & 0o077:
        raise FinanceStorageCandidateAbortError(
            f"{label} permissions are not private"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FinanceStorageCandidateAbortError(
            f"{label} is unreadable"
        ) from exc
    if not isinstance(payload, dict):
        raise FinanceStorageCandidateAbortError(
            f"{label} must contain a JSON object"
        )
    return payload


def _file_identity(path: Path, *, include_sha256: bool = False) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise FinanceStorageCandidateAbortError(
            f"candidate file is missing or unsafe: {path}"
        )
    stat = path.stat()
    identity: dict[str, Any] = {
        "name": path.name,
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "size_bytes": int(stat.st_size),
        "allocated_bytes": int(stat.st_blocks) * 512,
        "mtime_ns": int(stat.st_mtime_ns),
        "mode": int(stat.st_mode & 0o777),
    }
    if include_sha256:
        identity["sha256"] = _sha256_file(path)
    return identity


def _same_file(path: Path, expected: Mapping[str, Any]) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    current = _file_identity(
        path,
        include_sha256="sha256" in expected,
    )
    return all(current.get(key) == expected.get(key) for key in expected)


def _sqlite_schema_digest(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        """SELECT type,name,tbl_name,COALESCE(sql,'')
           FROM sqlite_master
           WHERE name NOT LIKE 'sqlite_%'
           ORDER BY type,name,tbl_name,sql"""
    ).fetchall()
    return _fingerprint([list(row) for row in rows])


def _open_query_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path}?mode=ro",
        uri=True,
        timeout=10,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
        connection.close()
        raise FinanceStorageCandidateAbortError(
            f"query_only could not be enabled for {path.name}"
        )
    return connection


def _canonical_monolith_identity(runtime_dir: Path) -> dict[str, Any]:
    registry = StoreRegistry(runtime_dir)
    manifest = registry.load()
    if (
        manifest.state != "monolith"
        or manifest.canonical_source != "monolith"
        or manifest.generation_epoch != "monolith"
        or not manifest.implicit
    ):
        raise FinanceStorageCandidateAbortError(
            "candidate abort requires the implicit canonical monolith"
        )
    if (runtime_dir / MANIFEST_FILENAME).exists():
        raise FinanceStorageCandidateAbortError(
            "candidate abort requires the global manifest to be absent"
        )
    monolith = registry.resolve("operational", manifest=manifest)
    if (
        monolith != runtime_dir / MONOLITH_FILENAME
        or monolith.is_symlink()
        or not monolith.is_file()
    ):
        raise FinanceStorageCandidateAbortError(
            "canonical monolith identity is missing or unsafe"
        )
    stat = monolith.stat()
    connection = _open_query_only(monolith)
    try:
        schema_digest = _sqlite_schema_digest(connection)
    finally:
        connection.close()
    return {
        "path": str(monolith),
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "schema_digest": schema_digest,
        "registry_manifest_sha256": manifest.manifest_sha256,
        "canonical_source": manifest.canonical_source,
        "state": manifest.state,
        "generation_epoch": manifest.generation_epoch,
    }


def _same_monolith_identity(
    current: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> bool:
    keys = {
        "path",
        "device",
        "inode",
        "schema_digest",
        "registry_manifest_sha256",
        "canonical_source",
        "state",
        "generation_epoch",
    }
    return all(current.get(key) == expected.get(key) for key in keys)


def _snapshot_inventory(runtime_dir: Path) -> list[dict[str, Any]]:
    root = runtime_dir / "finance-storage-split-snapshots"
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        raise FinanceStorageCandidateAbortError(
            "Finance snapshot root is unsafe"
        )
    inventory: list[dict[str, Any]] = []
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if entry.is_symlink() or not entry.is_dir():
            raise FinanceStorageCandidateAbortError(
                "Finance snapshot inventory is unsafe"
            )
        stat = entry.stat()
        inventory.append(
            {
                "name": entry.name,
                "device": int(stat.st_dev),
                "inode": int(stat.st_ino),
                "files": [
                    {
                        "name": child.name,
                        "device": int(child.stat().st_dev),
                        "inode": int(child.stat().st_ino),
                        "size_bytes": int(child.stat().st_size),
                        "mtime_ns": int(child.stat().st_mtime_ns),
                    }
                    for child in sorted(
                        entry.iterdir(),
                        key=lambda item: item.name,
                    )
                    if (
                        not child.is_symlink()
                        and child.is_file()
                    )
                ],
            }
        )
        children = list(entry.iterdir())
        if any(child.is_symlink() or not child.is_file() for child in children):
            raise FinanceStorageCandidateAbortError(
                "Finance snapshot content inventory is unsafe"
            )
    return inventory


def _barrier_guard(runtime_dir: Path) -> dict[str, Any]:
    barrier = barrier_status(runtime_dir)
    if barrier.get("active") is True:
        raise FinanceStorageCandidateAbortError(
            "candidate abort is blocked by an active write barrier"
        )
    return {
        "active": False,
        "phase": str(barrier.get("phase") or ""),
        "window_id": str(barrier.get("window_id") or ""),
    }


def _shadow_guard(runtime_dir: Path, generation_epoch: str) -> dict[str, Any]:
    path = runtime_dir / ".finance-storage-shadow-ingest.json"
    if not path.exists():
        return {"path": str(path), "status": "absent", "active": False}
    state = _load_json(path, label="Finance shadow-ingest state")
    active = bool(state.get("active")) or str(state.get("status") or "") in {
        "active",
        "soaking",
    }
    if active or str(state.get("generation_epoch") or "") == generation_epoch:
        raise FinanceStorageCandidateAbortError(
            "candidate abort is blocked by shadow state for the target generation"
        )
    return {
        "path": str(path),
        "status": str(state.get("status") or ""),
        "active": active,
        "generation_epoch": str(state.get("generation_epoch") or ""),
    }


def _process_command(process: Path) -> list[str]:
    try:
        raw = (process / "cmdline").read_bytes()
    except OSError:
        return []
    return [
        item.decode("utf-8", errors="replace")
        for item in raw.split(b"\0")
        if item
    ]


def _active_candidate_workers() -> list[dict[str, Any]]:
    if not Path("/proc").is_dir():
        return []
    workers: list[dict[str, Any]] = []
    for process in Path("/proc").iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        command = _process_command(process)
        is_direct_apply = any(
            token.endswith("finance_storage_split.py")
            and index + 1 < len(command)
            and command[index + 1] == "apply"
            for index, token in enumerate(command)
        )
        is_hosted_apply = any(
            token == "finance-storage-split-apply" for token in command
        )
        if is_direct_apply or is_hosted_apply:
            workers.append(
                {
                    "pid": int(process.name),
                    "command": command,
                }
            )
    return sorted(workers, key=lambda item: item["pid"])


def _migration_lock_status(
    runtime_dir: Path,
    *,
    already_held: bool,
) -> dict[str, Any]:
    path = runtime_dir / MIGRATION_LOCK_FILENAME
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise FinanceStorageCandidateAbortError(
            "Finance storage migration lock path is unsafe"
        )
    if not already_held and path.exists():
        descriptor = os.open(path, os.O_RDWR)
        try:
            try:
                fcntl.flock(
                    descriptor,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError as exc:
                raise FinanceStorageCandidateAbortError(
                    "Finance storage migration lock is busy"
                ) from exc
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            os.close(descriptor)
    return {
        "path": str(path),
        "exclusive_available": True,
    }


def _openers_below(root: Path) -> list[dict[str, Any]]:
    if not Path("/proc").is_dir():
        return []
    exact_root = root.resolve()
    openers: list[dict[str, Any]] = []
    for process in Path("/proc").iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            descriptors = list((process / "fd").iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                target = descriptor.resolve(strict=True)
            except OSError:
                continue
            if target == exact_root or exact_root in target.parents:
                openers.append(
                    {
                        "pid": int(process.name),
                        "fd": descriptor.name,
                        "path": str(target),
                    }
                )
    return sorted(
        openers,
        key=lambda item: (item["pid"], item["fd"], item["path"]),
    )


def _generation_entries(runtime_dir: Path) -> list[str]:
    root = runtime_dir / "generations"
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        raise FinanceStorageCandidateAbortError(
            "Finance generations root is unsafe"
        )
    return sorted(entry.name for entry in root.iterdir())


def _meta_identity(
    path: Path,
    *,
    table: str,
    logical_store: str,
    generation_id: str,
    generation_epoch: str,
    source_fingerprint: str,
) -> dict[str, Any]:
    connection = _open_query_only(path)
    try:
        row = connection.execute(
            f"""SELECT schema_revision,logical_store,generation_id,
                       generation_epoch,source_fingerprint
                FROM {table} WHERE singleton=1"""
        ).fetchone()
        if row is None:
            raise FinanceStorageCandidateAbortError(
                f"{path.name} generation metadata is missing"
            )
        identity = {key: str(row[key] or "") for key in row.keys()}
        expected = {
            "logical_store": logical_store,
            "generation_id": generation_id,
            "generation_epoch": generation_epoch,
            "source_fingerprint": source_fingerprint,
        }
        if any(identity.get(key) != value for key, value in expected.items()):
            raise FinanceStorageCandidateAbortError(
                f"{path.name} generation identity does not match the saved plan"
            )
        identity["schema_digest"] = _sqlite_schema_digest(connection)
        return identity
    except sqlite3.Error as exc:
        raise FinanceStorageCandidateAbortError(
            f"{path.name} query-only identity readback failed"
        ) from exc
    finally:
        connection.close()


def _checkpoint_summary(
    raw_path: Path,
    operational_path: Path,
    *,
    saved_plan: Mapping[str, Any],
    generation_epoch: str,
) -> dict[str, Any]:
    chunks = list((saved_plan.get("chunks") or {}).get("manifest") or [])
    raw_expected = {
        str(item.get("chunk_id") or ""): dict(item)
        for item in chunks
        if isinstance(item, Mapping)
    }
    matrix = list(saved_plan.get("table_owner_read_write_matrix") or [])
    operational_expected = {
        f"table:{str(item.get('table') or '')}": dict(item)
        for item in matrix
        if isinstance(item, Mapping)
        and str(item.get("table") or "")
    }
    expected_batch_id = _digest(
        {
            "migration_id": generation_epoch,
            "source_fingerprint": str(
                (saved_plan.get("source") or {}).get("fingerprint") or ""
            ),
            "raw_digest": str(
                (saved_plan.get("raw") or {}).get("logical_digest") or ""
            ),
        }
    )
    raw = _open_query_only(raw_path)
    try:
        batches = raw.execute(
            """SELECT batch_id,row_count,rows_digest,status,committed_at
               FROM finance_raw_ingest_batches ORDER BY batch_id"""
        ).fetchall()
        if len(batches) != 1:
            raise FinanceStorageCandidateAbortError(
                "partial candidate raw batch inventory is ambiguous"
            )
        batch = dict(batches[0])
        if (
            str(batch.get("batch_id") or "") != expected_batch_id
            or int(batch.get("row_count") or 0)
            != int((saved_plan.get("raw") or {}).get("row_count") or 0)
            or str(batch.get("rows_digest") or "")
            != str((saved_plan.get("raw") or {}).get("logical_digest") or "")
            or str(batch.get("status") or "") not in {"loading", "committed"}
        ):
            raise FinanceStorageCandidateAbortError(
                "partial candidate raw batch does not match the saved plan"
            )
    except sqlite3.Error as exc:
        raise FinanceStorageCandidateAbortError(
            "partial candidate raw checkpoint readback failed"
        ) from exc
    finally:
        raw.close()
    operational = _open_query_only(operational_path)
    try:
        rows = operational.execute(
            """SELECT migration_id,store_name,chunk_id,source_row_count,
                      source_digest,destination_row_count,destination_digest,
                      status,error
               FROM finance_storage_migration_chunks
               ORDER BY store_name,chunk_id"""
        ).fetchall()
    except sqlite3.Error as exc:
        raise FinanceStorageCandidateAbortError(
            "partial candidate checkpoint readback failed"
        ) from exc
    finally:
        operational.close()
    raw_rows = 0
    raw_verified = 0
    operational_rows = 0
    operational_verified = 0
    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        store_name = str(item.get("store_name") or "")
        chunk_id = str(item.get("chunk_id") or "")
        if (
            str(item.get("migration_id") or "") != generation_epoch
            or str(item.get("status") or "") != "verified"
            or item.get("error") not in {None, ""}
        ):
            raise FinanceStorageCandidateAbortError(
                "partial candidate contains an unsupported checkpoint state"
            )
        if store_name == "finance_raw":
            expected = raw_expected.get(chunk_id)
            expected_count = int((expected or {}).get("row_count") or 0)
            expected_digest = str(
                (expected or {}).get("verification_digest") or ""
            )
            if (
                expected is None
                or int(item.get("source_row_count") or 0)
                != expected_count
                or int(item.get("destination_row_count") or 0)
                != expected_count
                or str(item.get("source_digest") or "")
                != expected_digest
                or str(item.get("destination_digest") or "")
                != expected_digest
            ):
                raise FinanceStorageCandidateAbortError(
                    "partial candidate raw checkpoint disagrees with the plan"
                )
            raw_rows += expected_count
            raw_verified += 1
        elif store_name == "operational":
            expected = operational_expected.get(chunk_id)
            expected_count = int((expected or {}).get("row_count") or 0)
            expected_digest = str(
                (expected or {}).get("logical_digest") or ""
            )
            if (
                expected is None
                or int(item.get("source_row_count") or 0)
                != expected_count
                or int(item.get("destination_row_count") or 0)
                != expected_count
                or str(item.get("source_digest") or "")
                != expected_digest
                or str(item.get("destination_digest") or "")
                != expected_digest
            ):
                raise FinanceStorageCandidateAbortError(
                    "partial candidate operational checkpoint disagrees with the plan"
                )
            operational_rows += expected_count
            operational_verified += 1
        else:
            raise FinanceStorageCandidateAbortError(
                "partial candidate contains an unknown checkpoint store"
            )
        items.append(
            {
                "store_name": store_name,
                "chunk_id": chunk_id,
                "row_count": int(item.get("destination_row_count") or 0),
                "digest": str(item.get("destination_digest") or ""),
                "status": "verified",
            }
        )
    if raw_verified > len(raw_expected) or operational_verified > len(
        operational_expected
    ):
        raise FinanceStorageCandidateAbortError(
            "partial candidate checkpoint counts are impossible"
        )
    return {
        "batch_id": expected_batch_id,
        "batch_status": str(batch.get("status") or ""),
        "batch_declared_rows": int(batch.get("row_count") or 0),
        "batch_declared_digest": str(batch.get("rows_digest") or ""),
        "raw_verified_chunks": raw_verified,
        "raw_total_chunks": len(raw_expected),
        "raw_verified_rows": raw_rows,
        "operational_verified_tables": operational_verified,
        "operational_total_tables": len(operational_expected),
        "operational_verified_rows": operational_rows,
        "checkpoint_fingerprint": _fingerprint(items),
    }


def _plan_fingerprint(payload: Mapping[str, Any]) -> str:
    stable = {
        key: value
        for key, value in payload.items()
        if key not in {"fingerprint", "created_at", "deploy_lease"}
    }
    return _fingerprint(stable)


class FinanceStorageCandidateAbort:
    """Plan, apply, resume, and read back one exact partial-candidate abort."""

    def __init__(
        self,
        runtime_dir: Path,
        *,
        deployed_sha: str,
        generation_epoch: str,
        candidate_plan_fingerprint: str,
        fault_after_unlinks: int = 0,
    ) -> None:
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.deployed_sha = str(deployed_sha or "").strip()
        self.generation_epoch = str(generation_epoch or "").strip()
        self.candidate_plan_fingerprint = str(
            candidate_plan_fingerprint or ""
        ).strip()
        self.fault_after_unlinks = max(0, int(fault_after_unlinks))
        if _SHA_RE.fullmatch(self.deployed_sha) is None:
            raise FinanceStorageCandidateAbortError(
                "exact current deployed SHA is required"
            )
        if _GENERATION_RE.fullmatch(self.generation_epoch) is None:
            raise FinanceStorageCandidateAbortError(
                "exact candidate generation epoch is invalid"
            )
        if _FINGERPRINT_RE.fullmatch(
            self.candidate_plan_fingerprint
        ) is None:
            raise FinanceStorageCandidateAbortError(
                "exact old candidate plan fingerprint is required"
            )
        self.generations_root = self.runtime_dir / "generations"
        self.candidate_root = (
            self.generations_root / self.generation_epoch
        ).resolve()
        if (
            self.candidate_root.parent
            != self.generations_root.resolve()
        ):
            raise FinanceStorageCandidateAbortError(
                "candidate generation escapes the canonical runtime"
            )

    @property
    def transaction_root(self) -> Path:
        return self.runtime_dir / TRANSACTION_DIRECTORY

    @property
    def transaction_path(self) -> Path:
        return self.transaction_root / f"{self.generation_epoch}.json"

    @property
    def result_path(self) -> Path:
        return self.transaction_root / f"{self.generation_epoch}.result.json"

    @property
    def audit_path(self) -> Path:
        return self.transaction_root / AUDIT_FILENAME

    @contextmanager
    def _migration_lock(self) -> Iterator[None]:
        lock_path = self.runtime_dir / MIGRATION_LOCK_FILENAME
        if lock_path.is_symlink() or not lock_path.is_file():
            raise FinanceStorageCandidateAbortError(
                "Finance storage migration lock is missing or unsafe"
            )
        descriptor = os.open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(
                descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            os.close(descriptor)
            raise FinanceStorageCandidateAbortError(
                "Finance storage migration lock is busy"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _saved_plan(self) -> tuple[dict[str, Any], dict[str, Any]]:
        path = self.candidate_root / SAVED_PLAN_FILENAME
        plan = _load_json(path, label="saved candidate migration plan")
        generation = dict(plan.get("target_generation") or {})
        candidate = dict(generation.get("candidate_manifest") or {})
        raw = dict(candidate.get("raw") or {})
        operational = dict(candidate.get("operational") or {})
        expected_raw = (
            self.candidate_root / RAW_FILENAME
        ).relative_to(self.runtime_dir)
        expected_operational = (
            self.candidate_root / OPERATIONAL_FILENAME
        ).relative_to(self.runtime_dir)
        saved_fingerprint = str(plan.get("fingerprint") or "")
        if (
            str(plan.get("contract_version") or "")
            != CANDIDATE_PLAN_CONTRACT
            or str(plan.get("mode") or "") != "dry_run"
            or saved_fingerprint != self.candidate_plan_fingerprint
            or _plan_fingerprint(plan) != saved_fingerprint
            or str(generation.get("generation_epoch") or "")
            != self.generation_epoch
            or Path(str(raw.get("relative_path") or ""))
            != expected_raw
            or Path(str(operational.get("relative_path") or ""))
            != expected_operational
            or str(raw.get("generation_epoch") or "")
            != self.generation_epoch
            or str(operational.get("generation_epoch") or "")
            != self.generation_epoch
            or _SHA_RE.fullmatch(
                str(plan.get("deployed_sha") or "")
            )
            is None
            or _FINGERPRINT_RE.fullmatch(
                str((plan.get("source") or {}).get("fingerprint") or "")
            )
            is None
        ):
            raise FinanceStorageCandidateAbortError(
                "saved candidate migration plan binding is invalid"
            )
        return plan, {
            "generation": generation,
            "candidate": candidate,
            "raw": raw,
            "operational": operational,
            "path": str(path),
            "sha256": _sha256_file(path),
        }

    def _candidate_inventory(self) -> list[dict[str, Any]]:
        if (
            self.candidate_root.is_symlink()
            or not self.candidate_root.is_dir()
        ):
            raise FinanceStorageCandidateAbortError(
                "candidate generation directory is missing or unsafe"
            )
        names = sorted(entry.name for entry in self.candidate_root.iterdir())
        unknown = sorted(set(names) - _ALLOWED_NAMES)
        if unknown:
            raise FinanceStorageCandidateAbortError(
                f"candidate generation has unknown files: {unknown}"
            )
        if CANDIDATE_MANIFEST_FILENAME in names:
            raise FinanceStorageCandidateAbortError(
                "candidate abort refuses a manifest-selected lifecycle"
            )
        required = {
            SAVED_PLAN_FILENAME,
            RAW_FILENAME,
            OPERATIONAL_FILENAME,
        }
        if not required.issubset(names):
            raise FinanceStorageCandidateAbortError(
                "partial candidate required files are incomplete"
            )
        return [
            _file_identity(
                self.candidate_root / name,
                include_sha256=name == SAVED_PLAN_FILENAME,
            )
            for name in names
        ]

    def _build_exact_state(
        self,
        *,
        migration_lock_already_held: bool = False,
    ) -> dict[str, Any]:
        entries = _generation_entries(self.runtime_dir)
        if entries != [self.generation_epoch]:
            raise FinanceStorageCandidateAbortError(
                "candidate abort requires exactly one target generation"
            )
        canonical = _canonical_monolith_identity(self.runtime_dir)
        barrier = _barrier_guard(self.runtime_dir)
        shadow = _shadow_guard(
            self.runtime_dir,
            self.generation_epoch,
        )
        workers = _active_candidate_workers()
        if workers:
            raise FinanceStorageCandidateAbortError(
                "candidate abort is blocked by an active candidate worker"
            )
        openers = _openers_below(self.candidate_root)
        if openers:
            raise FinanceStorageCandidateAbortError(
                "candidate abort is blocked by open candidate files"
            )
        plan, binding = self._saved_plan()
        source_fingerprint = str(
            (plan.get("source") or {}).get("fingerprint") or ""
        )
        generation = dict(binding["generation"])
        raw_identity = _meta_identity(
            self.candidate_root / RAW_FILENAME,
            table="finance_raw_schema_meta",
            logical_store="finance_raw",
            generation_id=str(
                generation.get("raw_generation_id") or ""
            ),
            generation_epoch=self.generation_epoch,
            source_fingerprint=source_fingerprint,
        )
        operational_identity = _meta_identity(
            self.candidate_root / OPERATIONAL_FILENAME,
            table="finance_operational_schema_meta",
            logical_store="operational",
            generation_id=str(
                generation.get("operational_generation_id") or ""
            ),
            generation_epoch=self.generation_epoch,
            source_fingerprint=source_fingerprint,
        )
        checkpoints = _checkpoint_summary(
            self.candidate_root / RAW_FILENAME,
            self.candidate_root / OPERATIONAL_FILENAME,
            saved_plan=plan,
            generation_epoch=self.generation_epoch,
        )
        inventory = self._candidate_inventory()
        return {
            "generation_entries": entries,
            "canonical_monolith": canonical,
            "barrier": barrier,
            "shadow": shadow,
            "candidate_workers": workers,
            "candidate_openers": openers,
            "migration_lock": _migration_lock_status(
                self.runtime_dir,
                already_held=migration_lock_already_held,
            ),
            "saved_candidate_plan": {
                "path": binding["path"],
                "sha256": binding["sha256"],
                "fingerprint": self.candidate_plan_fingerprint,
                "deployed_sha": str(plan.get("deployed_sha") or ""),
                "source_fingerprint": source_fingerprint,
            },
            "raw_identity": raw_identity,
            "operational_identity": operational_identity,
            "checkpoints": checkpoints,
            "candidate_files": inventory,
            "candidate_allocated_bytes": sum(
                int(item["allocated_bytes"]) for item in inventory
            ),
            "snapshots": _snapshot_inventory(self.runtime_dir),
        }

    def build_plan(self) -> dict[str, Any]:
        with self._migration_lock():
            state = self._build_exact_state(
                migration_lock_already_held=True,
            )
        plan: dict[str, Any] = {
            "contract_version": PLAN_CONTRACT,
            "mode": MODE,
            "deployed_sha": self.deployed_sha,
            "candidate_source_deployed_sha": state[
                "saved_candidate_plan"
            ]["deployed_sha"],
            "candidate_plan_fingerprint": (
                self.candidate_plan_fingerprint
            ),
            "generation_epoch": self.generation_epoch,
            "candidate_root": str(self.candidate_root),
            "transaction_path": str(self.transaction_path),
            "result_path": str(self.result_path),
            "audit_path": str(self.audit_path),
            "exact_state": state,
            "delete_allowlist": list(_DELETE_ORDER),
            "canonical_manifest_switch_planned": False,
            "canonical_source": "monolith",
            "candidate_abort_allowed_by_machine_preflight": True,
            "human_approval_required": True,
            "fail_closed": True,
            "query_only_contract": {
                "business_data_mutation_count": 0,
                "candidate_byte_mutation_count": 0,
                "manifest_mutation_count": 0,
            },
            "rollback_and_non_target": {
                "canonical_monolith_untouched": True,
                "global_manifest_untouched_and_absent": True,
                "snapshots_untouched": True,
                "other_generations_allowed": False,
                "resume_only_from_durable_transaction": True,
            },
        }
        plan["fingerprint"] = _plan_fingerprint(plan)
        plan["created_at"] = _utc_now()
        return plan

    def _validate_reviewed_plan(
        self,
        reviewed_plan: Mapping[str, Any],
        *,
        expected_fingerprint: str,
    ) -> dict[str, Any]:
        plan = dict(reviewed_plan)
        if (
            str(plan.get("contract_version") or "") != PLAN_CONTRACT
            or str(plan.get("mode") or "") != MODE
            or plan.get(
                "candidate_abort_allowed_by_machine_preflight"
            )
            is not True
            or str(plan.get("deployed_sha") or "") != self.deployed_sha
            or str(plan.get("generation_epoch") or "")
            != self.generation_epoch
            or str(plan.get("candidate_plan_fingerprint") or "")
            != self.candidate_plan_fingerprint
            or str(plan.get("fingerprint") or "")
            != expected_fingerprint
            or _plan_fingerprint(plan) != expected_fingerprint
            or list(plan.get("delete_allowlist") or [])
            != list(_DELETE_ORDER)
        ):
            raise FinanceStorageCandidateAbortError(
                "reviewed candidate abort plan is stale or invalid"
            )
        return plan

    def _transaction_binding(
        self,
        plan: Mapping[str, Any],
        *,
        approval_reference: str,
    ) -> dict[str, Any]:
        exact_state = dict(plan.get("exact_state") or {})
        return {
            "contract_version": TRANSACTION_CONTRACT,
            "status": "deleting",
            "deployed_sha": self.deployed_sha,
            "generation_epoch": self.generation_epoch,
            "candidate_root": str(self.candidate_root),
            "candidate_abort_plan_fingerprint": str(
                plan.get("fingerprint") or ""
            ),
            "candidate_plan_fingerprint": (
                self.candidate_plan_fingerprint
            ),
            "saved_candidate_plan_sha256": str(
                (
                    exact_state.get("saved_candidate_plan")
                    or {}
                ).get("sha256")
                or ""
            ),
            "approval_reference": approval_reference,
            "planned_files": list(exact_state.get("candidate_files") or []),
            "deleted_files": [],
            "pending_file": "",
            "canonical_monolith": dict(
                exact_state.get("canonical_monolith") or {}
            ),
            "snapshots": list(exact_state.get("snapshots") or []),
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
        }

    def _load_transaction(self) -> dict[str, Any] | None:
        if not self.transaction_path.exists():
            return None
        return _load_json(
            self.transaction_path,
            label="candidate abort transaction",
            require_private=True,
        )

    def _validate_transaction(
        self,
        transaction: Mapping[str, Any],
        plan: Mapping[str, Any],
    ) -> dict[str, Any]:
        state = dict(transaction)
        exact_state = dict(plan.get("exact_state") or {})
        if (
            str(state.get("contract_version") or "")
            != TRANSACTION_CONTRACT
            or str(state.get("status") or "")
            not in {"deleting", "completed"}
            or str(state.get("deployed_sha") or "")
            != self.deployed_sha
            or str(state.get("generation_epoch") or "")
            != self.generation_epoch
            or str(state.get("candidate_root") or "")
            != str(self.candidate_root)
            or str(
                state.get("candidate_abort_plan_fingerprint") or ""
            )
            != str(plan.get("fingerprint") or "")
            or str(state.get("candidate_plan_fingerprint") or "")
            != self.candidate_plan_fingerprint
            or str(state.get("saved_candidate_plan_sha256") or "")
            != str(
                (
                    exact_state.get("saved_candidate_plan")
                    or {}
                ).get("sha256")
                or ""
            )
            or list(state.get("planned_files") or [])
            != list(exact_state.get("candidate_files") or [])
            or dict(state.get("canonical_monolith") or {})
            != dict(exact_state.get("canonical_monolith") or {})
            or list(state.get("snapshots") or [])
            != list(exact_state.get("snapshots") or [])
        ):
            raise FinanceStorageCandidateAbortError(
                "candidate abort transaction binding is stale or ambiguous"
            )
        planned_names = {
            str(item.get("name") or "")
            for item in state.get("planned_files") or []
            if isinstance(item, Mapping)
        }
        deleted = list(state.get("deleted_files") or [])
        pending_file = str(state.get("pending_file") or "")
        if (
            len(deleted) != len(set(deleted))
            or not set(deleted).issubset(planned_names)
            or (
                pending_file
                and (
                    pending_file not in planned_names
                    or pending_file in deleted
                )
            )
        ):
            raise FinanceStorageCandidateAbortError(
                "candidate abort transaction deletion journal is invalid"
            )
        return state

    def _resume_guard(
        self,
        transaction: Mapping[str, Any],
    ) -> None:
        generation_entries = _generation_entries(self.runtime_dir)
        if generation_entries not in ([self.generation_epoch], []):
            raise FinanceStorageCandidateAbortError(
                "candidate generation inventory drifted during abort"
            )
        canonical = _canonical_monolith_identity(self.runtime_dir)
        if not _same_monolith_identity(
            canonical,
            transaction.get("canonical_monolith") or {},
        ):
            raise FinanceStorageCandidateAbortError(
                "canonical monolith identity drifted during candidate abort"
            )
        _barrier_guard(self.runtime_dir)
        _shadow_guard(self.runtime_dir, self.generation_epoch)
        if _active_candidate_workers():
            raise FinanceStorageCandidateAbortError(
                "candidate worker appeared during candidate abort"
            )
        if self.candidate_root.exists():
            if self.candidate_root.is_symlink() or not self.candidate_root.is_dir():
                raise FinanceStorageCandidateAbortError(
                    "candidate root became unsafe during abort"
                )
            names = {
                entry.name for entry in self.candidate_root.iterdir()
            }
            planned = {
                str(item.get("name") or "")
                for item in transaction.get("planned_files") or []
                if isinstance(item, Mapping)
            }
            unknown = sorted(names - planned)
            if unknown:
                raise FinanceStorageCandidateAbortError(
                    f"unknown candidate files appeared during abort: {unknown}"
                )
            deleted = set(transaction.get("deleted_files") or [])
            pending_file = str(transaction.get("pending_file") or "")
            identities = {
                str(item.get("name") or ""): dict(item)
                for item in transaction.get("planned_files") or []
                if isinstance(item, Mapping)
            }
            for name in planned:
                path = self.candidate_root / name
                if name in deleted:
                    if path.exists():
                        raise FinanceStorageCandidateAbortError(
                            "durably deleted candidate file reappeared"
                        )
                elif name == pending_file:
                    if path.exists() and not _same_file(
                        path,
                        identities[name],
                    ):
                        raise FinanceStorageCandidateAbortError(
                            "pending candidate file drifted"
                        )
                elif not _same_file(path, identities[name]):
                    raise FinanceStorageCandidateAbortError(
                        f"remaining candidate file drifted: {name}"
                    )
            openers = _openers_below(self.candidate_root)
            if openers:
                raise FinanceStorageCandidateAbortError(
                    "candidate files gained an opener during abort"
                )
        elif set(transaction.get("deleted_files") or []) != {
            str(item.get("name") or "")
            for item in transaction.get("planned_files") or []
            if isinstance(item, Mapping)
        }:
            raise FinanceStorageCandidateAbortError(
                "candidate root disappeared before durable file journal completion"
            )

    def _terminal_readback(
        self,
        transaction: Mapping[str, Any],
    ) -> dict[str, Any]:
        if self.candidate_root.exists():
            raise FinanceStorageCandidateAbortError(
                "candidate root remains after abort"
            )
        if _generation_entries(self.runtime_dir):
            raise FinanceStorageCandidateAbortError(
                "generation inventory is not empty after candidate abort"
            )
        canonical = _canonical_monolith_identity(self.runtime_dir)
        if not _same_monolith_identity(
            canonical,
            transaction.get("canonical_monolith") or {},
        ):
            raise FinanceStorageCandidateAbortError(
                "canonical monolith identity changed during candidate abort"
            )
        barrier = _barrier_guard(self.runtime_dir)
        shadow = _shadow_guard(self.runtime_dir, self.generation_epoch)
        snapshots = _snapshot_inventory(self.runtime_dir)
        if snapshots != list(transaction.get("snapshots") or []):
            raise FinanceStorageCandidateAbortError(
                "snapshot inventory changed during candidate abort"
            )
        return {
            "candidate_root_absent": True,
            "generation_entries": [],
            "global_manifest_absent": not (
                self.runtime_dir / MANIFEST_FILENAME
            ).exists(),
            "canonical_monolith": canonical,
            "barrier": barrier,
            "shadow": shadow,
            "snapshots": snapshots,
            "candidate_workers": _active_candidate_workers(),
            "candidate_openers": [],
            "non_target_unchanged": True,
        }

    def apply(
        self,
        *,
        reviewed_plan: Mapping[str, Any],
        expected_fingerprint: str,
        approval_reference: str,
    ) -> dict[str, Any]:
        if _FINGERPRINT_RE.fullmatch(expected_fingerprint or "") is None:
            raise FinanceStorageCandidateAbortError(
                "exact candidate abort plan fingerprint is required"
            )
        if not str(approval_reference or "").strip():
            raise FinanceStorageCandidateAbortError(
                "candidate abort requires an exact approval reference"
            )
        plan = self._validate_reviewed_plan(
            reviewed_plan,
            expected_fingerprint=expected_fingerprint,
        )
        with self._migration_lock():
            transaction = self._load_transaction()
            if transaction is None:
                fresh = self._build_exact_state(
                    migration_lock_already_held=True,
                )
                if fresh != dict(plan.get("exact_state") or {}):
                    raise FinanceStorageCandidateAbortError(
                        "candidate state drifted after plan review"
                    )
                transaction = self._transaction_binding(
                    plan,
                    approval_reference=str(approval_reference).strip(),
                )
                _atomic_write_json(self.transaction_path, transaction)
                _append_audit(
                    self.audit_path,
                    {
                        "event": "candidate_abort_started",
                        "at": _utc_now(),
                        "generation_epoch": self.generation_epoch,
                        "plan_fingerprint": expected_fingerprint,
                        "approval_reference": str(
                            approval_reference
                        ).strip(),
                    },
                )
            else:
                transaction = self._validate_transaction(
                    transaction,
                    plan,
                )
                if str(transaction.get("status") or "") == "completed":
                    return self.readback(
                        reviewed_plan=plan,
                        expected_fingerprint=expected_fingerprint,
                    )
            self._resume_guard(transaction)
            identities = {
                str(item.get("name") or ""): dict(item)
                for item in transaction.get("planned_files") or []
                if isinstance(item, Mapping)
            }
            deleted = list(transaction.get("deleted_files") or [])
            pending_file = str(transaction.get("pending_file") or "")
            if pending_file:
                pending_path = self.candidate_root / pending_file
                if pending_path.exists():
                    if not _same_file(
                        pending_path,
                        identities[pending_file],
                    ):
                        raise FinanceStorageCandidateAbortError(
                            "pending candidate file changed before resume"
                        )
                    if _openers_below(self.candidate_root):
                        raise FinanceStorageCandidateAbortError(
                            "candidate opener appeared before pending unlink"
                        )
                    pending_path.unlink()
                    _fsync_directory(self.candidate_root)
                deleted.append(pending_file)
                transaction["deleted_files"] = deleted
                transaction["pending_file"] = ""
                transaction["updated_at"] = _utc_now()
                _atomic_write_json(self.transaction_path, transaction)
            unlinks = 0
            for name in _DELETE_ORDER:
                if name not in identities or name in deleted:
                    continue
                path = self.candidate_root / name
                if not _same_file(path, identities[name]):
                    raise FinanceStorageCandidateAbortError(
                        f"candidate file changed before unlink: {name}"
                    )
                if _openers_below(self.candidate_root):
                    raise FinanceStorageCandidateAbortError(
                        "candidate opener appeared before unlink"
                    )
                transaction["pending_file"] = name
                transaction["updated_at"] = _utc_now()
                _atomic_write_json(self.transaction_path, transaction)
                path.unlink()
                _fsync_directory(self.candidate_root)
                deleted.append(name)
                transaction["deleted_files"] = deleted
                transaction["pending_file"] = ""
                transaction["updated_at"] = _utc_now()
                _atomic_write_json(self.transaction_path, transaction)
                unlinks += 1
                if (
                    self.fault_after_unlinks
                    and unlinks >= self.fault_after_unlinks
                ):
                    raise InjectedCandidateAbortFault(
                        f"after_unlinks:{unlinks}"
                    )
            planned_names = set(identities)
            if set(deleted) != planned_names:
                raise FinanceStorageCandidateAbortError(
                    "candidate abort delete allowlist was incomplete"
                )
            if self.candidate_root.exists():
                if any(self.candidate_root.iterdir()):
                    raise FinanceStorageCandidateAbortError(
                        "candidate root is not empty after exact deletion"
                    )
                self.candidate_root.rmdir()
                _fsync_directory(self.generations_root)
            readback = self._terminal_readback(transaction)
            result: dict[str, Any] = {
                "contract_version": RESULT_CONTRACT,
                "status": "completed",
                "deployed_sha": self.deployed_sha,
                "generation_epoch": self.generation_epoch,
                "candidate_abort_plan_fingerprint": expected_fingerprint,
                "candidate_plan_fingerprint": (
                    self.candidate_plan_fingerprint
                ),
                "approval_reference": str(approval_reference).strip(),
                "deleted_files": deleted,
                "reclaimed_allocated_bytes": sum(
                    int(item.get("allocated_bytes") or 0)
                    for item in identities.values()
                ),
                "transaction_path": str(self.transaction_path),
                "audit_path": str(self.audit_path),
                "readback": readback,
                "completed_at": _utc_now(),
                "idempotent": True,
                "fail_closed": True,
            }
            stable_result = dict(result)
            result["fingerprint"] = _fingerprint(stable_result)
            _atomic_write_json(self.result_path, result)
            _append_audit(
                self.audit_path,
                {
                    "event": "candidate_abort_completed",
                    "at": result["completed_at"],
                    "generation_epoch": self.generation_epoch,
                    "plan_fingerprint": expected_fingerprint,
                    "result_fingerprint": result["fingerprint"],
                    "deleted_files": deleted,
                    "reclaimed_allocated_bytes": result[
                        "reclaimed_allocated_bytes"
                    ],
                },
            )
            transaction["status"] = "completed"
            transaction["deleted_files"] = deleted
            transaction["pending_file"] = ""
            transaction["result_path"] = str(self.result_path)
            transaction["result_fingerprint"] = result["fingerprint"]
            transaction["updated_at"] = _utc_now()
            transaction["completed_at"] = result["completed_at"]
            _atomic_write_json(self.transaction_path, transaction)
            return result

    def readback(
        self,
        *,
        reviewed_plan: Mapping[str, Any],
        expected_fingerprint: str,
    ) -> dict[str, Any]:
        plan = self._validate_reviewed_plan(
            reviewed_plan,
            expected_fingerprint=expected_fingerprint,
        )
        transaction = self._load_transaction()
        if transaction is None:
            raise FinanceStorageCandidateAbortError(
                "candidate abort transaction is absent"
            )
        transaction = self._validate_transaction(transaction, plan)
        if str(transaction.get("status") or "") != "completed":
            raise FinanceStorageCandidateAbortError(
                "candidate abort transaction is not terminal"
            )
        result = _load_json(
            self.result_path,
            label="candidate abort durable result",
            require_private=True,
        )
        stable_result = dict(result)
        result_fingerprint = str(stable_result.pop("fingerprint", "") or "")
        if (
            str(result.get("contract_version") or "") != RESULT_CONTRACT
            or str(result.get("status") or "") != "completed"
            or str(
                result.get("candidate_abort_plan_fingerprint") or ""
            )
            != expected_fingerprint
            or str(result.get("generation_epoch") or "")
            != self.generation_epoch
            or result_fingerprint != _fingerprint(stable_result)
            or result_fingerprint
            != str(transaction.get("result_fingerprint") or "")
        ):
            raise FinanceStorageCandidateAbortError(
                "candidate abort durable result binding is invalid"
            )
        current = self._terminal_readback(transaction)
        audit_found = False
        try:
            audit_lines = self.audit_path.read_text(
                encoding="utf-8"
            ).splitlines()
        except OSError as exc:
            raise FinanceStorageCandidateAbortError(
                "candidate abort durable audit is unreadable"
            ) from exc
        for line in audit_lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise FinanceStorageCandidateAbortError(
                    "candidate abort durable audit is malformed"
                ) from exc
            if (
                isinstance(event, dict)
                and event.get("event") == "candidate_abort_completed"
                and event.get("generation_epoch")
                == self.generation_epoch
                and event.get("plan_fingerprint")
                == expected_fingerprint
                and event.get("result_fingerprint")
                == result_fingerprint
            ):
                audit_found = True
        if not audit_found:
            raise FinanceStorageCandidateAbortError(
                "candidate abort terminal audit evidence is missing"
            )
        readback: dict[str, Any] = {
            "contract_version": RESULT_CONTRACT,
            "status": "completed",
            "deployed_sha": self.deployed_sha,
            "generation_epoch": self.generation_epoch,
            "candidate_abort_plan_fingerprint": expected_fingerprint,
            "candidate_plan_fingerprint": (
                self.candidate_plan_fingerprint
            ),
            "durable_result_fingerprint": result_fingerprint,
            "transaction_path": str(self.transaction_path),
            "result_path": str(self.result_path),
            "audit_path": str(self.audit_path),
            "readback": current,
            "independent_readback_at": _utc_now(),
            "fail_closed": True,
        }
        readback["readback_fingerprint"] = _fingerprint(readback)
        return readback
