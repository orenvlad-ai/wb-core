"""Fail-closed retention for stale Finance coherent migration snapshots."""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable

from packages.application.business_data_write_barrier import barrier_status
from packages.application.finance_storage_migration import SNAPSHOT_CONTRACT
from packages.application.storage_registry import StoreRegistry


PLAN_CONTRACT = "wb_core_finance_storage_snapshot_retention_plan_v1"
RESULT_CONTRACT = "wb_core_finance_storage_snapshot_retention_result_v1"
ARCHIVE_CONTRACT = "wb_core_finance_storage_snapshot_archive_v1"
TRANSACTION_CONTRACT = (
    "wb_core_finance_storage_snapshot_retention_transaction_v1"
)
SNAPSHOT_DIRECTORY = "finance-storage-split-snapshots"
ARCHIVE_RELATIVE_ROOT = Path("backups") / SNAPSHOT_DIRECTORY
AUDIT_FILENAME = "retention_audit.jsonl"
LOCK_FILENAME = ".finance-storage-snapshot-retention.lock"
ARCHIVE_MANIFEST_FILENAME = "archive_manifest.json"
TRANSACTION_FILENAME = "retention_transaction.json"
DEFAULT_BACKUP_RESERVE_BYTES = 2 * 1024**3
DEFAULT_MINIMUM_ROOT_FREE_BYTES = 20 * 1024**3
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}")
_SNAPSHOT_ID_RE = re.compile(r"finance-split-[0-9a-f]{20}")
_ALLOWED_SNAPSHOT_FILES = {
    "monolith.sqlite3",
    "monolith.sqlite3-wal",
    "monolith.sqlite3-shm",
    "snapshot_manifest.json",
    "snapshot_capture_intent.json",
}


class FinanceStorageSnapshotRetentionError(ValueError):
    """The stale snapshot retention boundary is unsafe or ambiguous."""


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


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        data = (
            json.dumps(
                payload,
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


def _append_audit(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise FinanceStorageSnapshotRetentionError(
            "snapshot retention audit path is unsafe"
        )
    line = (_canonical_json(payload) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, line)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)
    _fsync_directory(path.parent)


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise FinanceStorageSnapshotRetentionError(
            f"{label} is missing or unsafe"
        )
    if path.stat().st_mode & 0o077:
        raise FinanceStorageSnapshotRetentionError(
            f"{label} permissions are not private"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FinanceStorageSnapshotRetentionError(
            f"{label} is unreadable"
        ) from exc
    if not isinstance(payload, dict):
        raise FinanceStorageSnapshotRetentionError(
            f"{label} must contain a JSON object"
        )
    return payload


def _file_identity(path: Path, *, include_sha256: bool) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise FinanceStorageSnapshotRetentionError(
            f"snapshot file is missing or unsafe: {path}"
        )
    stat = path.stat()
    payload: dict[str, Any] = {
        "name": path.name,
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "size_bytes": int(stat.st_size),
        "allocated_bytes": int(stat.st_blocks) * 512,
        "mtime_ns": int(stat.st_mtime_ns),
        "mode": int(stat.st_mode & 0o777),
    }
    if include_sha256:
        payload["sha256"] = _sha256_file(path)
    return payload


def _same_planned_file(
    path: Path,
    expected: dict[str, Any],
    *,
    include_sha256: bool,
) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    current = _file_identity(path, include_sha256=include_sha256)
    keys = {
        "name",
        "device",
        "inode",
        "size_bytes",
        "allocated_bytes",
        "mtime_ns",
    }
    if include_sha256:
        keys.add("sha256")
    return all(current.get(key) == expected.get(key) for key in keys)


def _filesystem_capacity(path: Path) -> dict[str, int]:
    stats = os.statvfs(path)
    return {
        "available_bytes": int(stats.f_bavail) * int(stats.f_frsize),
        "block_size": int(stats.f_frsize),
    }


def _openers_below(roots: Iterable[Path]) -> list[dict[str, Any]]:
    exact_roots = tuple(path.resolve() for path in roots)
    if not exact_roots or not Path("/proc").is_dir():
        return []
    openers: list[dict[str, Any]] = []
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        fd_root = process / "fd"
        try:
            descriptors = list(fd_root.iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                target = descriptor.resolve(strict=True)
            except OSError:
                continue
            if any(
                target == root or root in target.parents
                for root in exact_roots
            ):
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


def _assert_archive_inventory(
    archive: Path,
    *,
    planned_names: set[str],
    transaction_exists: bool,
    allow_partial: bool,
) -> None:
    names = set(path.name for path in archive.iterdir())
    if not transaction_exists:
        if names:
            raise FinanceStorageSnapshotRetentionError(
                "snapshot archive appeared without a durable transaction"
            )
        return
    allowed = set(planned_names)
    allowed.update(
        {
            TRANSACTION_FILENAME,
            ARCHIVE_MANIFEST_FILENAME,
        }
    )
    if allow_partial:
        allowed.update(f".{name}.partial" for name in planned_names)
    unknown = sorted(names - allowed)
    if unknown:
        raise FinanceStorageSnapshotRetentionError(
            f"snapshot archive has unknown files: {unknown}"
        )


class FinanceStorageSnapshotRetention:
    """Archive stale snapshots on the dedicated backup device before release."""

    def __init__(
        self,
        runtime_dir: Path,
        *,
        deployed_sha: str,
        backup_root: Path | None = None,
        backup_reserve_bytes: int = DEFAULT_BACKUP_RESERVE_BYTES,
        minimum_root_free_bytes: int = DEFAULT_MINIMUM_ROOT_FREE_BYTES,
        require_distinct_device: bool = True,
    ) -> None:
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.deployed_sha = str(deployed_sha or "").strip()
        nominal_snapshot_root = self.runtime_dir / SNAPSHOT_DIRECTORY
        if nominal_snapshot_root.is_symlink():
            raise FinanceStorageSnapshotRetentionError(
                "canonical snapshot root cannot be a symlink"
            )
        self.snapshot_root = nominal_snapshot_root.resolve()
        nominal_backup_root = (
            Path(backup_root).expanduser()
            if backup_root is not None
            else self.runtime_dir / ARCHIVE_RELATIVE_ROOT
        )
        if nominal_backup_root.is_symlink():
            raise FinanceStorageSnapshotRetentionError(
                "Finance snapshot archive root cannot be a symlink"
            )
        self.backup_root = nominal_backup_root.resolve()
        self.backup_reserve_bytes = int(backup_reserve_bytes)
        self.minimum_root_free_bytes = int(minimum_root_free_bytes)
        self.require_distinct_device = bool(require_distinct_device)
        if _SHA_RE.fullmatch(self.deployed_sha) is None:
            raise FinanceStorageSnapshotRetentionError(
                "exact deployed SHA is required"
            )
        if self.backup_reserve_bytes < 0 or self.minimum_root_free_bytes < 0:
            raise FinanceStorageSnapshotRetentionError(
                "capacity reservations cannot be negative"
            )
        try:
            self.snapshot_root.relative_to(self.runtime_dir)
            self.backup_root.relative_to(self.runtime_dir)
        except ValueError as exc:
            raise FinanceStorageSnapshotRetentionError(
                "snapshot retention paths escape the canonical runtime"
            ) from exc
        expected_backup_root = (
            self.runtime_dir / ARCHIVE_RELATIVE_ROOT
        ).resolve()
        if backup_root is None and self.backup_root != expected_backup_root:
            raise FinanceStorageSnapshotRetentionError(
                "canonical Finance snapshot archive root is invalid"
            )

    @property
    def audit_path(self) -> Path:
        return self.backup_root / AUDIT_FILENAME

    def _canonical_guard(self) -> dict[str, Any]:
        manifest = StoreRegistry(self.runtime_dir).load()
        if (
            manifest.state != "monolith"
            or manifest.canonical_source != "monolith"
            or manifest.generation_epoch != "monolith"
        ):
            raise FinanceStorageSnapshotRetentionError(
                "snapshot retention requires the implicit canonical monolith"
            )
        generations_root = self.runtime_dir / "generations"
        generation_entries = (
            sorted(path.name for path in generations_root.iterdir())
            if generations_root.is_dir()
            else []
        )
        if generation_entries:
            raise FinanceStorageSnapshotRetentionError(
                "snapshot retention is blocked by an existing split generation"
            )
        barrier = barrier_status(self.runtime_dir)
        if barrier.get("active") is True:
            raise FinanceStorageSnapshotRetentionError(
                "snapshot retention is blocked by an active write barrier"
            )
        return {
            "state": manifest.state,
            "canonical_source": manifest.canonical_source,
            "generation_epoch": manifest.generation_epoch,
            "manifest_sha256": manifest.manifest_sha256,
            "barrier": {
                "active": bool(barrier.get("active")),
                "phase": str(barrier.get("phase") or ""),
                "window_id": str(barrier.get("window_id") or ""),
            },
            "generation_entries": generation_entries,
        }

    def _validate_backup_device(self) -> dict[str, Any]:
        if self.backup_root.is_symlink() or (
            self.backup_root.exists() and not self.backup_root.is_dir()
        ):
            raise FinanceStorageSnapshotRetentionError(
                "Finance snapshot archive root is unsafe"
            )
        if (
            not self.backup_root.parent.is_dir()
            or self.backup_root.parent.is_symlink()
        ):
            raise FinanceStorageSnapshotRetentionError(
                "dedicated backup mount is missing or unsafe"
            )
        runtime_device = int(self.runtime_dir.stat().st_dev)
        backup_device = int(self.backup_root.parent.stat().st_dev)
        if self.require_distinct_device and backup_device == runtime_device:
            raise FinanceStorageSnapshotRetentionError(
                "Finance snapshot archive must use a distinct backup device"
            )
        return {
            "runtime_device": runtime_device,
            "backup_device": backup_device,
            "distinct_device": backup_device != runtime_device,
            "backup_mount_path": str(self.backup_root.parent),
        }

    def _snapshot_candidate(
        self,
        snapshot_dir: Path,
        *,
        include_sha256: bool,
    ) -> dict[str, Any]:
        if (
            snapshot_dir.is_symlink()
            or not snapshot_dir.is_dir()
            or _SNAPSHOT_ID_RE.fullmatch(snapshot_dir.name) is None
        ):
            raise FinanceStorageSnapshotRetentionError(
                f"snapshot directory is unsafe: {snapshot_dir}"
            )
        names = sorted(path.name for path in snapshot_dir.iterdir())
        unknown = sorted(set(names) - _ALLOWED_SNAPSHOT_FILES)
        if unknown:
            raise FinanceStorageSnapshotRetentionError(
                f"snapshot {snapshot_dir.name} has unknown files: {unknown}"
            )
        required = {"monolith.sqlite3", "snapshot_manifest.json"}
        if not required.issubset(names):
            raise FinanceStorageSnapshotRetentionError(
                f"snapshot {snapshot_dir.name} is incomplete"
            )
        manifest_path = snapshot_dir / "snapshot_manifest.json"
        manifest = _load_json(
            manifest_path,
            label=f"snapshot {snapshot_dir.name} manifest",
        )
        stable_manifest = {
            key: value
            for key, value in manifest.items()
            if key != "evidence_fingerprint"
        }
        database_path = Path(
            str(manifest.get("database_path") or "")
        ).resolve()
        if (
            str(manifest.get("contract_version") or "") != SNAPSHOT_CONTRACT
            or str(manifest.get("snapshot_id") or "") != snapshot_dir.name
            or str(manifest.get("status") or "")
            not in {"captured_unverified", "integrity_verified"}
            or _SHA_RE.fullmatch(
                str(manifest.get("deployed_sha") or "")
            )
            is None
            or database_path != snapshot_dir / "monolith.sqlite3"
            or str(manifest.get("evidence_fingerprint") or "")
            != _fingerprint(stable_manifest)
        ):
            raise FinanceStorageSnapshotRetentionError(
                f"snapshot {snapshot_dir.name} manifest binding is invalid"
            )
        files = [
            _file_identity(
                snapshot_dir / name,
                include_sha256=include_sha256,
            )
            for name in names
        ]
        total_bytes = sum(int(item["size_bytes"]) for item in files)
        allocated_bytes = sum(
            int(item["allocated_bytes"]) for item in files
        )
        return {
            "snapshot_id": snapshot_dir.name,
            "snapshot_status": str(manifest["status"]),
            "snapshot_deployed_sha": str(manifest["deployed_sha"]),
            "snapshot_evidence_fingerprint": str(
                manifest["evidence_fingerprint"]
            ),
            "source_path": str(snapshot_dir),
            "archive_path": str(self.backup_root / snapshot_dir.name),
            "files": files,
            "total_bytes": total_bytes,
            "allocated_bytes": allocated_bytes,
            "stale_for_deployed_sha": (
                str(manifest["deployed_sha"]) != self.deployed_sha
            ),
        }

    def build_plan(self) -> dict[str, Any]:
        guard = self._canonical_guard()
        device = self._validate_backup_device()
        snapshot_dirs = (
            sorted(
                (
                    path
                    for path in self.snapshot_root.iterdir()
                    if path.is_dir() or path.is_symlink()
                ),
                key=lambda path: path.name,
            )
            if self.snapshot_root.is_dir()
            else []
        )
        candidates = [
            self._snapshot_candidate(path, include_sha256=True)
            for path in snapshot_dirs
        ]
        selected = [
            item for item in candidates if item["stale_for_deployed_sha"]
        ]
        protected = [
            item for item in candidates if not item["stale_for_deployed_sha"]
        ]
        blockers: list[dict[str, Any]] = []
        if not selected:
            blockers.append(
                {
                    "code": "no_stale_snapshots",
                    "detail": "no stale Finance snapshots require archival",
                }
            )
        openers = _openers_below(
            Path(str(item["source_path"])) for item in selected
        )
        if openers:
            blockers.append(
                {
                    "code": "snapshot_openers_present",
                    "openers": openers,
                }
            )
        root_capacity = _filesystem_capacity(self.runtime_dir)
        backup_capacity = _filesystem_capacity(self.backup_root.parent)
        source_bytes = sum(int(item["total_bytes"]) for item in selected)
        source_allocated = sum(
            int(item["allocated_bytes"]) for item in selected
        )
        archive_required = source_bytes + self.backup_reserve_bytes
        projected_root_free = (
            int(root_capacity["available_bytes"]) + source_allocated
        )
        if int(backup_capacity["available_bytes"]) < archive_required:
            blockers.append(
                {
                    "code": "backup_capacity_shortfall",
                    "available_bytes": int(
                        backup_capacity["available_bytes"]
                    ),
                    "required_bytes": archive_required,
                }
            )
        if projected_root_free < self.minimum_root_free_bytes:
            blockers.append(
                {
                    "code": "insufficient_projected_root_headroom",
                    "projected_free_bytes": projected_root_free,
                    "required_free_bytes": self.minimum_root_free_bytes,
                }
            )
        for item in selected:
            archive_path = Path(str(item["archive_path"]))
            if archive_path.exists():
                blockers.append(
                    {
                        "code": "archive_path_collision",
                        "snapshot_id": item["snapshot_id"],
                        "path": str(archive_path),
                    }
                )
        plan: dict[str, Any] = {
            "contract_version": PLAN_CONTRACT,
            "mode": "snapshot_retention_dry_run",
            "created_at": _utc_now(),
            "deployed_sha": self.deployed_sha,
            "runtime_dir": str(self.runtime_dir),
            "snapshot_root": str(self.snapshot_root),
            "archive_root": str(self.backup_root),
            "canonical_guard": guard,
            "device_boundary": device,
            "inventory_snapshot_ids": [
                str(item["snapshot_id"]) for item in candidates
            ],
            "selected_snapshots": selected,
            "protected_snapshots": [
                {
                    "snapshot_id": item["snapshot_id"],
                    "snapshot_deployed_sha": item[
                        "snapshot_deployed_sha"
                    ],
                    "source_path": item["source_path"],
                    "snapshot_evidence_fingerprint": item[
                        "snapshot_evidence_fingerprint"
                    ],
                }
                for item in protected
            ],
            "capacity": {
                "root_available_before_bytes": int(
                    root_capacity["available_bytes"]
                ),
                "root_allocated_bytes_to_release": source_allocated,
                "root_projected_free_after_bytes": projected_root_free,
                "minimum_root_free_bytes": self.minimum_root_free_bytes,
                "backup_available_before_bytes": int(
                    backup_capacity["available_bytes"]
                ),
                "archive_payload_bytes": source_bytes,
                "backup_reserve_bytes": self.backup_reserve_bytes,
                "archive_required_bytes": archive_required,
            },
            "openers": openers,
            "blockers": blockers,
            "query_only_contract": {
                "business_data_mutation_count": 0,
                "snapshot_byte_mutation_count": 0,
                "archive_byte_mutation_count": 0,
            },
            "apply_allowed_by_machine_preflight": not blockers,
            "source_release_policy": {
                "archive_first": True,
                "byte_sha256_readback_required": True,
                "durable_transaction_required": True,
                "source_manifest_removed_last": True,
                "live_monolith_touched": False,
                "split_generation_touched": False,
            },
        }
        plan["fingerprint"] = _fingerprint(plan)
        return plan

    def _validate_plan(
        self,
        reviewed_plan: dict[str, Any],
        *,
        expected_fingerprint: str,
    ) -> None:
        if (
            str(reviewed_plan.get("contract_version") or "")
            != PLAN_CONTRACT
            or str(reviewed_plan.get("mode") or "")
            != "snapshot_retention_dry_run"
            or str(reviewed_plan.get("deployed_sha") or "")
            != self.deployed_sha
            or str(reviewed_plan.get("runtime_dir") or "")
            != str(self.runtime_dir)
            or str(reviewed_plan.get("snapshot_root") or "")
            != str(self.snapshot_root)
            or str(reviewed_plan.get("archive_root") or "")
            != str(self.backup_root)
            or reviewed_plan.get("apply_allowed_by_machine_preflight")
            is not True
            or list(reviewed_plan.get("blockers") or [])
            or _FINGERPRINT_RE.fullmatch(expected_fingerprint) is None
            or str(reviewed_plan.get("fingerprint") or "")
            != expected_fingerprint
        ):
            raise FinanceStorageSnapshotRetentionError(
                "reviewed snapshot retention plan is invalid"
            )
        stable_plan = {
            key: value
            for key, value in reviewed_plan.items()
            if key not in {"fingerprint", "deploy_lease"}
        }
        if _fingerprint(stable_plan) != expected_fingerprint:
            raise FinanceStorageSnapshotRetentionError(
                "reviewed snapshot retention plan fingerprint is stale"
            )

    def _assert_inventory_boundary(
        self,
        reviewed_plan: dict[str, Any],
    ) -> None:
        self._canonical_guard()
        self._validate_backup_device()
        planned_ids = set(
            str(value)
            for value in reviewed_plan.get("inventory_snapshot_ids") or []
        )
        current_ids = (
            {
                path.name
                for path in self.snapshot_root.iterdir()
                if path.is_dir() or path.is_symlink()
            }
            if self.snapshot_root.is_dir()
            else set()
        )
        if not current_ids.issubset(planned_ids):
            raise FinanceStorageSnapshotRetentionError(
                "a new or unknown snapshot appeared after the reviewed plan"
            )
        for protected in reviewed_plan.get("protected_snapshots") or []:
            path = Path(str(protected.get("source_path") or ""))
            if not path.is_dir():
                raise FinanceStorageSnapshotRetentionError(
                    "a protected current snapshot disappeared"
                )

    def _copy_candidate(
        self,
        candidate: dict[str, Any],
        *,
        inventory_plan: dict[str, Any],
        plan_fingerprint: str,
        approval_reference: str,
        fault_after_archive_verified: bool,
        fault_after_source_removed: bool,
    ) -> dict[str, Any]:
        source_input = Path(str(candidate["source_path"]))
        archive_input = Path(str(candidate["archive_path"]))
        if source_input.is_symlink() or archive_input.is_symlink():
            raise FinanceStorageSnapshotRetentionError(
                "retention candidate path is a symlink"
            )
        source = source_input.resolve()
        archive = archive_input.resolve()
        try:
            source.relative_to(self.snapshot_root)
            archive.relative_to(self.backup_root)
        except ValueError as exc:
            raise FinanceStorageSnapshotRetentionError(
                "retention candidate paths escape their roots"
            ) from exc
        if archive.exists() and not archive.is_dir():
            raise FinanceStorageSnapshotRetentionError(
                "snapshot archive path is not a directory"
            )
        archive.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(archive, 0o700)
        transaction_path = archive / TRANSACTION_FILENAME
        planned_files = {
            str(item["name"]): dict(item)
            for item in candidate.get("files") or []
        }
        if (
            not planned_files
            or set(planned_files) - _ALLOWED_SNAPSHOT_FILES
        ):
            raise FinanceStorageSnapshotRetentionError(
                "reviewed snapshot files are empty or exceed the allowlist"
            )
        _assert_archive_inventory(
            archive,
            planned_names=set(planned_files),
            transaction_exists=transaction_path.is_file(),
            allow_partial=True,
        )
        if not source.exists() and not transaction_path.is_file():
            raise FinanceStorageSnapshotRetentionError(
                "snapshot source is absent without a durable transaction"
            )
        transaction = {
            "contract_version": TRANSACTION_CONTRACT,
            "snapshot_id": candidate["snapshot_id"],
            "plan_fingerprint": plan_fingerprint,
            "approval_reference": approval_reference,
            "deployed_sha": self.deployed_sha,
            "source_path": str(source),
            "archive_path": str(archive),
            "phase": "copying",
            "updated_at": _utc_now(),
        }
        if transaction_path.exists():
            existing = _load_json(
                transaction_path,
                label="snapshot retention transaction",
            )
            stable_keys = {
                "contract_version",
                "snapshot_id",
                "plan_fingerprint",
                "approval_reference",
                "deployed_sha",
                "source_path",
                "archive_path",
            }
            if any(
                existing.get(key) != transaction.get(key)
                for key in stable_keys
            ):
                raise FinanceStorageSnapshotRetentionError(
                    "existing snapshot retention transaction is ambiguous"
                )
            transaction = existing
        else:
            _atomic_write_json(transaction_path, transaction)
        if str(transaction.get("phase") or "") == "source_released":
            if source.exists():
                raise FinanceStorageSnapshotRetentionError(
                    "terminal retention transaction still has a source copy"
                )
            archive_manifest = _load_json(
                archive / ARCHIVE_MANIFEST_FILENAME,
                label="snapshot archive manifest",
            )
            stable_archive_manifest = {
                key: value
                for key, value in archive_manifest.items()
                if key != "fingerprint"
            }
            if (
                archive_manifest.get("source_release_completed") is not True
                or str(archive_manifest.get("plan_fingerprint") or "")
                != plan_fingerprint
                or str(archive_manifest.get("fingerprint") or "")
                != _fingerprint(stable_archive_manifest)
                or str(
                    transaction.get("archive_manifest_fingerprint") or ""
                )
                != str(archive_manifest.get("fingerprint") or "")
            ):
                raise FinanceStorageSnapshotRetentionError(
                    "terminal snapshot archive evidence is invalid"
                )
            _assert_archive_inventory(
                archive,
                planned_names=set(planned_files),
                transaction_exists=True,
                allow_partial=False,
            )
            for name, expected in planned_files.items():
                identity = _file_identity(
                    archive / name,
                    include_sha256=True,
                )
                if (
                    int(identity["size_bytes"])
                    != int(expected["size_bytes"])
                    or str(identity["sha256"])
                    != str(expected["sha256"])
                ):
                    raise FinanceStorageSnapshotRetentionError(
                        "terminal snapshot archive bytes drifted"
                    )
            return {
                "snapshot_id": candidate["snapshot_id"],
                "source_path": str(source),
                "archive_path": str(archive),
                "source_released": True,
                "archive_manifest_fingerprint": archive_manifest[
                    "fingerprint"
                ],
                "archived_bytes": int(candidate["total_bytes"]),
                "released_allocated_bytes": int(
                    candidate["allocated_bytes"]
                ),
                "idempotent": True,
            }
        if (
            not source.exists()
            and str(transaction.get("phase") or "")
            in {"archive_verified", "partial_source_release"}
        ):
            archive_manifest = _load_json(
                archive / ARCHIVE_MANIFEST_FILENAME,
                label="snapshot archive manifest",
            )
            stable_archive_manifest = {
                key: value
                for key, value in archive_manifest.items()
                if key != "fingerprint"
            }
            if (
                str(archive_manifest.get("status") or "")
                != "archive_verified"
                or str(archive_manifest.get("plan_fingerprint") or "")
                != plan_fingerprint
                or str(archive_manifest.get("fingerprint") or "")
                != _fingerprint(stable_archive_manifest)
            ):
                raise FinanceStorageSnapshotRetentionError(
                    "post-release snapshot archive evidence is invalid"
                )
            _assert_archive_inventory(
                archive,
                planned_names=set(planned_files),
                transaction_exists=True,
                allow_partial=False,
            )
            for name, expected in planned_files.items():
                identity = _file_identity(
                    archive / name,
                    include_sha256=True,
                )
                if (
                    int(identity["size_bytes"])
                    != int(expected["size_bytes"])
                    or str(identity["sha256"])
                    != str(expected["sha256"])
                ):
                    raise FinanceStorageSnapshotRetentionError(
                        "post-release snapshot archive bytes drifted"
                    )
            archive_manifest["source_release_completed"] = True
            archive_manifest["source_released_at"] = _utc_now()
            archive_manifest["fingerprint"] = _fingerprint(
                {
                    key: value
                    for key, value in archive_manifest.items()
                    if key != "fingerprint"
                }
            )
            _atomic_write_json(
                archive / ARCHIVE_MANIFEST_FILENAME,
                archive_manifest,
            )
            transaction.update(
                {
                    "phase": "source_released",
                    "archive_manifest_fingerprint": archive_manifest[
                        "fingerprint"
                    ],
                    "updated_at": _utc_now(),
                }
            )
            _atomic_write_json(transaction_path, transaction)
            return {
                "snapshot_id": candidate["snapshot_id"],
                "source_path": str(source),
                "archive_path": str(archive),
                "source_released": True,
                "archive_manifest_fingerprint": archive_manifest[
                    "fingerprint"
                ],
                "archived_bytes": int(candidate["total_bytes"]),
                "released_allocated_bytes": int(
                    candidate["allocated_bytes"]
                ),
                "idempotent": True,
                "continuity": "post_source_removal_finalized",
            }
        if not source.is_dir():
            raise FinanceStorageSnapshotRetentionError(
                "snapshot source is absent before terminal release"
            )
        archive_files: list[dict[str, Any]] = []
        for name in sorted(planned_files):
            expected = planned_files[name]
            source_file = source / name
            archive_file = archive / name
            if archive_file.exists():
                archive_identity = _file_identity(
                    archive_file,
                    include_sha256=True,
                )
                if (
                    archive_identity["size_bytes"]
                    != expected["size_bytes"]
                    or archive_identity["sha256"] != expected["sha256"]
                ):
                    raise FinanceStorageSnapshotRetentionError(
                        f"existing archive file is stale: {archive_file}"
                    )
                archive_files.append(archive_identity)
                continue
            if not _same_planned_file(
                source_file,
                expected,
                include_sha256=False,
            ):
                raise FinanceStorageSnapshotRetentionError(
                    f"source snapshot file drifted: {source_file}"
                )
            temporary = archive / f".{name}.partial"
            if temporary.exists():
                if temporary.is_symlink() or not temporary.is_file():
                    raise FinanceStorageSnapshotRetentionError(
                        f"partial archive path is unsafe: {temporary}"
                    )
                temporary.unlink()
            source_digest = hashlib.sha256()
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                with source_file.open("rb") as input_handle:
                    with os.fdopen(
                        descriptor,
                        "wb",
                        closefd=False,
                    ) as output_handle:
                        while True:
                            chunk = input_handle.read(8 * 1024 * 1024)
                            if not chunk:
                                break
                            source_digest.update(chunk)
                            output_handle.write(chunk)
                        output_handle.flush()
                        os.fsync(output_handle.fileno())
                os.close(descriptor)
                descriptor = -1
                copied_sha = "sha256:" + source_digest.hexdigest()
                if copied_sha != expected["sha256"]:
                    raise FinanceStorageSnapshotRetentionError(
                        f"source snapshot hash drifted: {source_file}"
                    )
                if not _same_planned_file(
                    source_file,
                    expected,
                    include_sha256=False,
                ):
                    raise FinanceStorageSnapshotRetentionError(
                        f"source snapshot stat drifted: {source_file}"
                    )
                os.replace(temporary, archive_file)
                os.chmod(archive_file, 0o600)
                _fsync_directory(archive)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                if temporary.exists():
                    temporary.unlink()
            archive_identity = _file_identity(
                archive_file,
                include_sha256=True,
            )
            if (
                archive_identity["size_bytes"] != expected["size_bytes"]
                or archive_identity["sha256"] != expected["sha256"]
            ):
                raise FinanceStorageSnapshotRetentionError(
                    f"archive readback mismatch: {archive_file}"
                )
            archive_files.append(archive_identity)
        archive_manifest: dict[str, Any] = {
            "contract_version": ARCHIVE_CONTRACT,
            "status": "archive_verified",
            "snapshot_id": candidate["snapshot_id"],
            "snapshot_status": candidate["snapshot_status"],
            "snapshot_deployed_sha": candidate["snapshot_deployed_sha"],
            "snapshot_evidence_fingerprint": candidate[
                "snapshot_evidence_fingerprint"
            ],
            "archived_by_deployed_sha": self.deployed_sha,
            "plan_fingerprint": plan_fingerprint,
            "approval_reference": approval_reference,
            "source_path": str(source),
            "archive_path": str(archive),
            "files": [
                {
                    "name": item["name"],
                    "size_bytes": item["size_bytes"],
                    "sha256": item["sha256"],
                }
                for item in sorted(
                    archive_files,
                    key=lambda value: str(value["name"]),
                )
            ],
            "verified_at": _utc_now(),
            "source_release_completed": False,
        }
        archive_manifest["fingerprint"] = _fingerprint(archive_manifest)
        _atomic_write_json(
            archive / ARCHIVE_MANIFEST_FILENAME,
            archive_manifest,
        )
        transaction.update(
            {
                "phase": "archive_verified",
                "archive_manifest_fingerprint": archive_manifest[
                    "fingerprint"
                ],
                "updated_at": _utc_now(),
            }
        )
        _atomic_write_json(transaction_path, transaction)
        _assert_archive_inventory(
            archive,
            planned_names=set(planned_files),
            transaction_exists=True,
            allow_partial=False,
        )
        if fault_after_archive_verified:
            raise RuntimeError("injected fault after archive verification")
        self._assert_inventory_boundary(inventory_plan)
        openers = _openers_below([source])
        if openers:
            raise FinanceStorageSnapshotRetentionError(
                "snapshot source has open file descriptors before release"
            )
        source_names = set(path.name for path in source.iterdir())
        unknown_source_names = sorted(source_names - set(planned_files))
        if unknown_source_names:
            raise FinanceStorageSnapshotRetentionError(
                "snapshot source has unknown files before release: "
                f"{unknown_source_names}"
            )
        transaction_phase = str(transaction.get("phase") or "")
        if transaction_phase == "archive_verified":
            if source_names != set(planned_files):
                raise FinanceStorageSnapshotRetentionError(
                    "snapshot source inventory drifted before release"
                )
        elif transaction_phase == "partial_source_release":
            if not source_names.issubset(set(planned_files)):
                raise FinanceStorageSnapshotRetentionError(
                    "partially released snapshot source is ambiguous"
                )
        else:
            raise FinanceStorageSnapshotRetentionError(
                "snapshot retention transaction phase is ambiguous"
            )
        for name in sorted(source_names):
            if not _same_planned_file(
                source / name,
                planned_files[name],
                include_sha256=True,
            ):
                raise FinanceStorageSnapshotRetentionError(
                    f"snapshot source drifted before release: {source / name}"
                )
        if transaction_phase == "archive_verified":
            transaction.update(
                {
                    "phase": "partial_source_release",
                    "updated_at": _utc_now(),
                }
            )
            _atomic_write_json(transaction_path, transaction)
        for name in sorted(
            planned_files,
            key=lambda value: (
                value == "snapshot_manifest.json",
                value,
            ),
        ):
            source_file = source / name
            if not source_file.exists():
                continue
            source_file.unlink()
            _fsync_directory(source)
        remaining = sorted(path.name for path in source.iterdir())
        if remaining:
            raise FinanceStorageSnapshotRetentionError(
                f"snapshot source release left unknown files: {remaining}"
            )
        source.rmdir()
        _fsync_directory(self.snapshot_root)
        if fault_after_source_removed:
            raise RuntimeError("injected fault after source removal")
        archive_manifest["source_release_completed"] = True
        archive_manifest["source_released_at"] = _utc_now()
        archive_manifest["fingerprint"] = _fingerprint(
            {
                key: value
                for key, value in archive_manifest.items()
                if key != "fingerprint"
            }
        )
        _atomic_write_json(
            archive / ARCHIVE_MANIFEST_FILENAME,
            archive_manifest,
        )
        transaction.update(
            {
                "phase": "source_released",
                "archive_manifest_fingerprint": archive_manifest[
                    "fingerprint"
                ],
                "updated_at": _utc_now(),
            }
        )
        _atomic_write_json(transaction_path, transaction)
        return {
            "snapshot_id": candidate["snapshot_id"],
            "source_path": str(source),
            "archive_path": str(archive),
            "source_released": True,
            "archive_manifest_fingerprint": archive_manifest["fingerprint"],
            "archived_bytes": int(candidate["total_bytes"]),
            "released_allocated_bytes": int(candidate["allocated_bytes"]),
        }

    def apply(
        self,
        *,
        reviewed_plan: dict[str, Any],
        expected_fingerprint: str,
        approval_reference: str,
        fault_after_archive_verified: bool = False,
        fault_after_source_removed: bool = False,
    ) -> dict[str, Any]:
        exact_approval = str(approval_reference or "").strip()
        if not exact_approval:
            raise FinanceStorageSnapshotRetentionError(
                "exact approval reference is required"
            )
        self._validate_plan(
            reviewed_plan,
            expected_fingerprint=expected_fingerprint,
        )
        lock_path = self.runtime_dir / LOCK_FILENAME
        if lock_path.is_symlink():
            raise FinanceStorageSnapshotRetentionError(
                "snapshot retention lock path is unsafe"
            )
        lock_descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        try:
            try:
                fcntl.flock(
                    lock_descriptor,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError as exc:
                raise FinanceStorageSnapshotRetentionError(
                    "another snapshot retention worker is active"
                ) from exc
            self._assert_inventory_boundary(reviewed_plan)
            selected = [
                dict(item)
                for item in reviewed_plan.get("selected_snapshots") or []
            ]
            if not selected:
                raise FinanceStorageSnapshotRetentionError(
                    "reviewed plan has no stale snapshots"
                )
            results = [
                self._copy_candidate(
                    candidate,
                    inventory_plan=reviewed_plan,
                    plan_fingerprint=expected_fingerprint,
                    approval_reference=exact_approval,
                    fault_after_archive_verified=(
                        fault_after_archive_verified and index == 0
                    ),
                    fault_after_source_removed=(
                        fault_after_source_removed and index == 0
                    ),
                )
                for index, candidate in enumerate(selected)
            ]
            self._assert_inventory_boundary(reviewed_plan)
            capacity = _filesystem_capacity(self.runtime_dir)
            capacity_sufficient = (
                int(capacity["available_bytes"])
                >= self.minimum_root_free_bytes
            )
            result: dict[str, Any] = {
                "contract_version": RESULT_CONTRACT,
                "status": "completed",
                "deployed_sha": self.deployed_sha,
                "plan_fingerprint": expected_fingerprint,
                "approval_reference": exact_approval,
                "completed_at": _utc_now(),
                "snapshots": results,
                "archived_snapshot_count": len(results),
                "archived_bytes": sum(
                    int(item["archived_bytes"]) for item in results
                ),
                "released_allocated_bytes": sum(
                    int(item["released_allocated_bytes"])
                    for item in results
                ),
                "root_available_after_bytes": int(
                    capacity["available_bytes"]
                ),
                "minimum_root_free_bytes": self.minimum_root_free_bytes,
                "capacity_sufficient": capacity_sufficient,
                "blockers": (
                    []
                    if capacity_sufficient
                    else [
                        {
                            "code": "root_capacity_still_insufficient",
                            "available_bytes": int(
                                capacity["available_bytes"]
                            ),
                            "required_bytes": (
                                self.minimum_root_free_bytes
                            ),
                        }
                    ]
                ),
                "live_monolith_touched": False,
                "split_generation_touched": False,
                "fail_closed": True,
            }
            result["fingerprint"] = _fingerprint(result)
            _append_audit(
                self.audit_path,
                {
                    "event": "snapshot_retention_completed",
                    "recorded_at": _utc_now(),
                    "result_fingerprint": result["fingerprint"],
                    "plan_fingerprint": expected_fingerprint,
                    "approval_reference": exact_approval,
                    "snapshot_ids": [
                        item["snapshot_id"] for item in results
                    ],
                    "deployed_sha": self.deployed_sha,
                },
            )
            return result
        finally:
            os.close(lock_descriptor)

    def readback(
        self,
        *,
        reviewed_plan: dict[str, Any],
        expected_fingerprint: str,
    ) -> dict[str, Any]:
        self._validate_plan(
            reviewed_plan,
            expected_fingerprint=expected_fingerprint,
        )
        self._canonical_guard()
        self._validate_backup_device()
        readbacks: list[dict[str, Any]] = []
        for candidate in reviewed_plan.get("selected_snapshots") or []:
            source = Path(str(candidate["source_path"])).resolve()
            archive = Path(str(candidate["archive_path"])).resolve()
            if source.exists():
                raise FinanceStorageSnapshotRetentionError(
                    f"snapshot source still exists after retention: {source}"
                )
            manifest = _load_json(
                archive / ARCHIVE_MANIFEST_FILENAME,
                label="snapshot archive manifest",
            )
            stable_manifest = {
                key: value
                for key, value in manifest.items()
                if key != "fingerprint"
            }
            if (
                str(manifest.get("contract_version") or "")
                != ARCHIVE_CONTRACT
                or str(manifest.get("status") or "")
                != "archive_verified"
                or manifest.get("source_release_completed") is not True
                or str(manifest.get("snapshot_id") or "")
                != str(candidate.get("snapshot_id") or "")
                or str(manifest.get("plan_fingerprint") or "")
                != expected_fingerprint
                or str(manifest.get("fingerprint") or "")
                != _fingerprint(stable_manifest)
            ):
                raise FinanceStorageSnapshotRetentionError(
                    "snapshot archive manifest readback is invalid"
                )
            expected_files = {
                str(item["name"]): item
                for item in candidate.get("files") or []
            }
            _assert_archive_inventory(
                archive,
                planned_names=set(expected_files),
                transaction_exists=True,
                allow_partial=False,
            )
            for name, expected in expected_files.items():
                archived = archive / name
                identity = _file_identity(
                    archived,
                    include_sha256=True,
                )
                if (
                    int(identity["size_bytes"])
                    != int(expected["size_bytes"])
                    or str(identity["sha256"]) != str(expected["sha256"])
                ):
                    raise FinanceStorageSnapshotRetentionError(
                        f"snapshot archive byte readback failed: {archived}"
                    )
            transaction = _load_json(
                archive / TRANSACTION_FILENAME,
                label="snapshot retention transaction",
            )
            if (
                str(transaction.get("phase") or "") != "source_released"
                or str(transaction.get("plan_fingerprint") or "")
                != expected_fingerprint
                or str(
                    transaction.get("archive_manifest_fingerprint") or ""
                )
                != str(manifest.get("fingerprint") or "")
            ):
                raise FinanceStorageSnapshotRetentionError(
                    "snapshot retention transaction is not terminal"
                )
            readbacks.append(
                {
                    "snapshot_id": candidate["snapshot_id"],
                    "source_absent": True,
                    "archive_verified": True,
                    "archive_manifest_fingerprint": manifest[
                        "fingerprint"
                    ],
                    "file_count": len(expected_files),
                    "bytes": int(candidate["total_bytes"]),
                }
            )
        capacity = _filesystem_capacity(self.runtime_dir)
        payload: dict[str, Any] = {
            "contract_version": RESULT_CONTRACT,
            "status": "readback_verified",
            "deployed_sha": self.deployed_sha,
            "plan_fingerprint": expected_fingerprint,
            "verified_at": _utc_now(),
            "snapshots": readbacks,
            "root_available_bytes": int(capacity["available_bytes"]),
            "minimum_root_free_bytes": self.minimum_root_free_bytes,
            "capacity_sufficient": (
                int(capacity["available_bytes"])
                >= self.minimum_root_free_bytes
            ),
            "live_monolith_touched": False,
            "split_generation_touched": False,
            "fail_closed": True,
        }
        if payload["capacity_sufficient"] is not True:
            raise FinanceStorageSnapshotRetentionError(
                "snapshot retention readback lacks root headroom"
            )
        payload["fingerprint"] = _fingerprint(payload)
        return payload
