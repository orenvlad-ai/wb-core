"""Shared fail-closed root-filesystem status and write-admission contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatchcase
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Any, Mapping


GIB = 1024**3
MIB = 1024**2
NORMAL_AVAILABLE_BYTES = 25 * GIB
WARNING_AVAILABLE_BYTES = 20 * GIB
CRITICAL_AVAILABLE_BYTES = 15 * GIB
HARD_DENY_AVAILABLE_BYTES = 12 * GIB
LARGE_OUTPUT_BYTES = 256 * MIB
CONTRACT_VERSION = "wb_core_root_storage_policy_v1"
STATUS_ARTIFACT_MAX_AGE_SECONDS = 10 * 60
CLASS_DISCRETIONARY = "discretionary_root_writer"
CLASS_ESSENTIAL = "essential_bounded_business_writer"
CLASS_RETAINED = "retained_no_active_writer"
NON_TARGET_CAS_CONTRACT = "wb_core_non_target_cas_v3"
MUTABLE_STORE_FILESYSTEM_ROLES = frozenset({"root", "generation"})
MUTABLE_STORE_ACCESS_MODES = frozenset({"read_only", "read_write", "write_only"})
MUTABLE_STORE_ACCESS_ROLES = {
    "reader": frozenset({"read_only"}),
    "writer": frozenset({"read_write"}),
    "reader_writer": frozenset({"read_only", "read_write"}),
}
MUTABLE_STORE_SERVICE_UNITS = frozenset(
    {
        "wb-core-registry-http.service",
        "wb-core-data-mcp.service",
        "wb-core-sheet-vitrina-refresh.service",
        "wb-core-sheet-vitrina-closure-retry.service",
        "wb-core-feedbacks-auto-complaints-tick.service",
        "wb-core-wb-finance-weekly.service",
        "wb-core-finance-backup-rotation.service",
        "wb-core-warehouse-functional-sync.service",
        "wb-core-fbs-shadow-collector.service",
        "wb-core-fbs-warehouse-registry.service",
        "wb-core-autoanswers-readonly-sync.service",
        "wb-core-autoanswers-worker.service",
    }
)
DEFAULT_POLICY_PATH = (
    Path(__file__).resolve().parents[2]
    / "artifacts"
    / "registry_upload_http_entrypoint"
    / "root_storage_policy_v1.json"
)


class RootStoragePolicyError(RuntimeError):
    """A root-storage policy or admission invariant failed closed."""


@dataclass(frozen=True)
class AdmissionRequest:
    owner: str
    destination: Path
    predicted_output_bytes: int | None


def load_policy(path: Path | None = None) -> dict[str, Any]:
    policy_path = (path or DEFAULT_POLICY_PATH).resolve()
    payload = json.loads(policy_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RootStoragePolicyError("root storage policy must be a JSON object")
    if payload.get("contract_version") != CONTRACT_VERSION:
        raise RootStoragePolicyError("root storage policy contract version mismatch")
    thresholds = payload.get("thresholds_bytes")
    expected_thresholds = {
        "normal_available": NORMAL_AVAILABLE_BYTES,
        "warning_below": WARNING_AVAILABLE_BYTES,
        "critical_below": CRITICAL_AVAILABLE_BYTES,
        "hard_deny_below": HARD_DENY_AVAILABLE_BYTES,
        "large_output": LARGE_OUTPUT_BYTES,
        "large_predicted_free_after_floor": CRITICAL_AVAILABLE_BYTES,
    }
    if thresholds != expected_thresholds:
        raise RootStoragePolicyError("root storage policy threshold drift")
    status_artifact = payload.get("status_artifact")
    if not isinstance(status_artifact, dict):
        raise RootStoragePolicyError("root storage status artifact policy is missing")
    status_path = Path(str(status_artifact.get("path") or ""))
    max_age_seconds = status_artifact.get("max_age_seconds")
    if (
        not status_path.is_absolute()
        or not isinstance(max_age_seconds, int)
        or isinstance(max_age_seconds, bool)
        or max_age_seconds != STATUS_ARTIFACT_MAX_AGE_SECONDS
    ):
        raise RootStoragePolicyError("root storage status artifact policy drift")
    producers = payload.get("producers")
    if not isinstance(producers, list) or not producers:
        raise RootStoragePolicyError("root storage policy producers must be a non-empty array")
    owner_ids: set[str] = set()
    producers_by_owner: dict[str, dict[str, Any]] = {}
    for producer in producers:
        if not isinstance(producer, dict):
            raise RootStoragePolicyError("root storage producer must be a JSON object")
        owner = str(producer.get("owner") or "").strip()
        classification = str(producer.get("classification") or "").strip()
        patterns = producer.get("path_patterns")
        if (
            not owner
            or owner in owner_ids
            or classification not in {CLASS_DISCRETIONARY, CLASS_ESSENTIAL, CLASS_RETAINED}
            or not isinstance(patterns, list)
        ):
            raise RootStoragePolicyError("root storage producer registry is invalid")
        for pattern in patterns:
            if not str(pattern).startswith("/"):
                raise RootStoragePolicyError("root storage producer paths must be absolute")
        owner_ids.add(owner)
        producers_by_owner[owner] = producer
    non_target_cas = payload.get("non_target_cas")
    if (
        not isinstance(non_target_cas, dict)
        or non_target_cas.get("contract_version") != NON_TARGET_CAS_CONTRACT
    ):
        raise RootStoragePolicyError("root storage non-target CAS policy is invalid")
    bindings = non_target_cas.get("active_mutable_canonical_stores")
    if not isinstance(bindings, list) or not bindings:
        raise RootStoragePolicyError("root storage mutable canonical store registry is empty")
    binding_keys: set[str] = set()
    for binding in bindings:
        if not isinstance(binding, dict):
            raise RootStoragePolicyError("mutable canonical store binding must be an object")
        key = str(binding.get("key") or "").strip()
        owner = str(binding.get("owner") or "").strip()
        classification = str(binding.get("classification") or "").strip()
        filesystem_role = str(binding.get("filesystem_role") or "").strip()
        resolver = binding.get("resolver")
        access_roles = binding.get("access_roles")
        producer = producers_by_owner.get(owner)
        if (
            not key
            or key in binding_keys
            or producer is None
            or classification != CLASS_ESSENTIAL
            or producer.get("classification") != classification
            or filesystem_role not in MUTABLE_STORE_FILESYSTEM_ROLES
            or not isinstance(resolver, dict)
            or not isinstance(access_roles, list)
            or not access_roles
            or not isinstance(binding.get("allow_no_open_handles"), bool)
        ):
            raise RootStoragePolicyError("mutable canonical store binding is invalid")
        role_services: set[str] = set()
        for access_role in access_roles:
            if not isinstance(access_role, dict):
                raise RootStoragePolicyError(
                    "mutable canonical store access role is invalid"
                )
            service = str(access_role.get("service") or "").strip()
            declared_role = str(access_role.get("declared_role") or "").strip()
            allowed_modes = access_role.get("allowed_access_modes")
            canonical_modes = MUTABLE_STORE_ACCESS_ROLES.get(declared_role)
            if (
                service not in MUTABLE_STORE_SERVICE_UNITS
                or service in role_services
                or canonical_modes is None
                or not isinstance(allowed_modes, list)
                or len(set(allowed_modes)) != len(allowed_modes)
                or set(allowed_modes) != set(canonical_modes)
                or any(mode not in MUTABLE_STORE_ACCESS_MODES for mode in allowed_modes)
            ):
                raise RootStoragePolicyError(
                    "mutable canonical store access role is invalid"
                )
            role_services.add(service)
        resolver_type = str(resolver.get("type") or "")
        if resolver_type == "store_registry":
            if resolver.get("logical_store") not in {"finance_raw", "operational"}:
                raise RootStoragePolicyError("mutable StoreRegistry resolver is invalid")
            if filesystem_role != "generation":
                raise RootStoragePolicyError(
                    "mutable StoreRegistry filesystem role is invalid"
                )
        elif resolver_type == "literal":
            literal = Path(str(resolver.get("path") or ""))
            if not literal.is_absolute() or filesystem_role != "root":
                raise RootStoragePolicyError("mutable literal resolver path is invalid")
            matched = _producer_for_path(payload, literal)
            if (
                matched is None
                or matched.get("owner") != owner
                or matched.get("classification") != classification
            ):
                raise RootStoragePolicyError(
                    "mutable literal resolver lacks exact producer ownership"
                )
        else:
            raise RootStoragePolicyError("mutable canonical store resolver is unknown")
        binding_keys.add(key)
    return payload


def storage_level(available_bytes: int) -> str:
    available = int(available_bytes)
    if available < HARD_DENY_AVAILABLE_BYTES:
        return "hard"
    if available < CRITICAL_AVAILABLE_BYTES:
        return "critical"
    if available < WARNING_AVAILABLE_BYTES:
        return "warning"
    if available < NORMAL_AVAILABLE_BYTES:
        return "below_normal"
    return "normal"


def predict_sqlite_backup_bytes(source: Path) -> int:
    """Conservative no-write bound for a live SQLite main file plus WAL."""

    source = Path(source)
    main_bytes = int(source.stat().st_size)
    wal = Path(str(source) + "-wal")
    wal_bytes = int(wal.stat().st_size) if wal.is_file() else 0
    return main_bytes + wal_bytes


def admit_root_write(
    *,
    owner: str,
    destination: Path,
    predicted_output_bytes: int | None,
    policy: Mapping[str, Any] | None = None,
    root_path: Path = Path("/"),
) -> dict[str, Any]:
    """Admit one explicitly owned write, denying unsafe root discretionary work.

    Essential bounded business writes remain separately classified and continue
    to rely on their domain-specific capacity/transaction guard. This function
    never reserves space and is intentionally not a future reservation ledger.
    """

    resolved_policy = dict(policy or load_policy())
    normalized_owner = str(owner or "").strip()
    destination = Path(destination)
    if not normalized_owner:
        raise RootStoragePolicyError("large write owner is required")
    if not destination.is_absolute():
        raise RootStoragePolicyError("large write destination must be absolute")
    destination = destination.resolve(strict=False)
    if predicted_output_bytes is None:
        raise RootStoragePolicyError("large write predicted output is required")
    predicted = int(predicted_output_bytes)
    if predicted < 0:
        raise RootStoragePolicyError("large write predicted output must be non-negative")

    producer = _producer_by_owner(resolved_policy, normalized_owner)
    if producer is None:
        raise RootStoragePolicyError(
            f"unregistered large root writer owner: {normalized_owner}"
        )
    classification = str(producer["classification"])
    if classification == CLASS_RETAINED:
        raise RootStoragePolicyError(
            f"retained producer has no active write authority: {normalized_owner}"
        )

    existing_parent = _nearest_existing_parent(destination)
    root_identity = os.stat(root_path)
    destination_identity = os.stat(existing_parent)
    destination_on_root = destination_identity.st_dev == root_identity.st_dev
    filesystem = os.statvfs(existing_parent)
    available = int(filesystem.f_bavail * filesystem.f_frsize)
    predicted_free_after = available - predicted
    level = storage_level(available)
    allowed = True
    reason = "destination_not_on_root"
    if destination_on_root and classification == CLASS_ESSENTIAL:
        reason = "essential_bounded_business_write_domain_guards_remain_authoritative"
    elif destination_on_root:
        reason = "admitted"
        if available < HARD_DENY_AVAILABLE_BYTES:
            allowed = False
            reason = "root_available_below_hard_deny"
        elif predicted >= LARGE_OUTPUT_BYTES and predicted_free_after < CRITICAL_AVAILABLE_BYTES:
            allowed = False
            reason = "large_output_predicted_free_after_below_critical_floor"

    result = {
        "contract_version": CONTRACT_VERSION,
        "owner": normalized_owner,
        "classification": classification,
        "destination": str(destination),
        "destination_existing_parent": str(existing_parent),
        "destination_on_root": destination_on_root,
        "root_device": int(root_identity.st_dev),
        "destination_device": int(destination_identity.st_dev),
        "available_bytes": available,
        "predicted_output_bytes": predicted,
        "predicted_free_after_bytes": predicted_free_after,
        "large_output": predicted >= LARGE_OUTPUT_BYTES,
        "storage_level": level,
        "allowed": allowed,
        "reason": reason,
    }
    if not allowed:
        raise RootStoragePolicyError(
            "root storage admission denied: "
            f"owner={normalized_owner}, reason={reason}, available_bytes={available}, "
            f"predicted_output_bytes={predicted}, predicted_free_after_bytes={predicted_free_after}"
        )
    return result


def collect_root_storage_status(
    *,
    policy: Mapping[str, Any] | None = None,
    root_path: Path = Path("/"),
    now: datetime | None = None,
) -> dict[str, Any]:
    resolved_policy = dict(policy or load_policy())
    collected_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    filesystems = {
        name: _filesystem_status(Path(path))
        for name, path in dict(resolved_policy.get("filesystems") or {}).items()
    }
    if "root" not in filesystems:
        filesystems["root"] = _filesystem_status(root_path)
    root_status = filesystems["root"]
    level = storage_level(int(root_status["available_bytes"]))
    large_files = _scan_large_files(resolved_policy, root_device=int(root_status["device"]))
    unregistered = [item for item in large_files if not item.get("registered")]
    alerts: list[dict[str, Any]] = []
    if level in {"warning", "critical", "hard"}:
        alerts.append(
            {
                "code": f"root_storage_{level}",
                "severity": level,
                "available_bytes": int(root_status["available_bytes"]),
            }
        )
    if unregistered:
        alerts.append(
            {
                "code": "unregistered_large_root_producer",
                "severity": "critical",
                "count": len(unregistered),
            }
        )
    return {
        "contract_version": CONTRACT_VERSION,
        "collected_at": collected_at.isoformat().replace("+00:00", "Z"),
        "policy_sha256": _payload_digest(resolved_policy),
        "status": level,
        "thresholds_bytes": dict(resolved_policy["thresholds_bytes"]),
        "filesystems": filesystems,
        "large_root_files": large_files,
        "unregistered_large_root_files": unregistered,
        "alerts": alerts,
        "safe_for_discretionary_root_writes": level != "hard" and not unregistered,
    }


def root_storage_status_artifact_path(policy: Mapping[str, Any]) -> Path:
    artifact = policy.get("status_artifact")
    if not isinstance(artifact, Mapping):
        raise RootStoragePolicyError("root storage status artifact policy is missing")
    path = Path(str(artifact.get("path") or ""))
    if not path.is_absolute():
        raise RootStoragePolicyError("root storage status artifact path must be absolute")
    return path


def read_root_storage_status_artifact(
    *,
    policy: Mapping[str, Any] | None = None,
    artifact_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate the atomic server-owned status artifact without rescanning storage."""

    resolved_policy = dict(policy or load_policy())
    path = Path(artifact_path or root_storage_status_artifact_path(resolved_policy))
    if path.is_symlink() or not path.is_file():
        raise RootStoragePolicyError(f"root storage status artifact is unavailable: {path}")
    if stat.S_IMODE(path.stat().st_mode) != 0o644:
        raise RootStoragePolicyError("root storage status artifact mode must be 0644")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RootStoragePolicyError("root storage status artifact is unreadable") from exc
    if not isinstance(payload, dict) or payload.get("contract_version") != CONTRACT_VERSION:
        raise RootStoragePolicyError("root storage status artifact contract mismatch")
    if payload.get("policy_sha256") != _payload_digest(resolved_policy):
        raise RootStoragePolicyError("root storage status artifact policy binding drift")
    raw_collected_at = str(payload.get("collected_at") or "")
    try:
        collected_at = datetime.fromisoformat(raw_collected_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RootStoragePolicyError("root storage status artifact timestamp is invalid") from exc
    if collected_at.tzinfo is None:
        raise RootStoragePolicyError("root storage status artifact timestamp lacks timezone")
    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age_seconds = (observed_at - collected_at.astimezone(timezone.utc)).total_seconds()
    max_age_seconds = int(dict(resolved_policy["status_artifact"])["max_age_seconds"])
    if age_seconds < -5 or age_seconds > max_age_seconds:
        raise RootStoragePolicyError(
            "root storage status artifact is stale or future-dated: "
            f"age_seconds={age_seconds:.3f}, max_age_seconds={max_age_seconds}"
        )
    filesystems = payload.get("filesystems")
    if not isinstance(filesystems, dict) or not isinstance(filesystems.get("root"), dict):
        raise RootStoragePolicyError("root storage status artifact lacks root filesystem evidence")
    raw_root_available = filesystems["root"].get("available_bytes")
    if not isinstance(raw_root_available, int):
        raise RootStoragePolicyError("root storage status artifact available bytes are invalid")
    root_available = raw_root_available
    if root_available < 0 or payload.get("status") != storage_level(root_available):
        raise RootStoragePolicyError("root storage status artifact classification drift")
    unregistered = payload.get("unregistered_large_root_files")
    if not isinstance(unregistered, list):
        raise RootStoragePolicyError("root storage status artifact producer inventory is invalid")
    expected_safe = payload.get("status") != "hard" and not unregistered
    if payload.get("safe_for_discretionary_root_writes") is not expected_safe:
        raise RootStoragePolicyError("root storage status artifact safety flag drift")
    return {
        "ok": not unregistered,
        "fresh": True,
        "artifact_path": str(path),
        "age_seconds": round(age_seconds, 3),
        "max_age_seconds": max_age_seconds,
        "status": payload,
    }


def _producer_by_owner(policy: Mapping[str, Any], owner: str) -> dict[str, Any] | None:
    for producer in list(policy.get("producers") or []):
        if str(producer.get("owner") or "") == owner:
            return dict(producer)
    return None


def _producer_for_path(policy: Mapping[str, Any], path: Path) -> dict[str, Any] | None:
    value = str(path)
    matches: list[dict[str, Any]] = []
    for producer in list(policy.get("producers") or []):
        if any(fnmatchcase(value, str(pattern)) for pattern in producer.get("path_patterns") or []):
            matches.append(dict(producer))
    if len(matches) > 1:
        raise RootStoragePolicyError(f"large root file matches multiple producers: {value}")
    return matches[0] if matches else None


def registered_producer_for_path(
    policy: Mapping[str, Any], path: Path
) -> dict[str, Any] | None:
    """Return the unique explicit producer registration for one literal path."""

    return _producer_for_path(policy, Path(path))


def _nearest_existing_parent(path: Path) -> Path:
    candidate = path if path.exists() and path.is_dir() else path.parent
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise RootStoragePolicyError(f"large write destination parent is unavailable: {path}")
        candidate = parent
    if not candidate.is_dir():
        candidate = candidate.parent
    return candidate.resolve()


def _scan_large_files(policy: Mapping[str, Any], *, root_device: int) -> list[dict[str, Any]]:
    threshold = int(dict(policy["thresholds_bytes"])["large_output"])
    results: list[dict[str, Any]] = []
    for raw_root in list(policy.get("scan_roots") or []):
        scan_root = Path(str(raw_root))
        if not scan_root.exists() or scan_root.is_symlink():
            continue
        for directory, directory_names, file_names in os.walk(scan_root):
            directory_path = Path(directory)
            try:
                directory_stat = directory_path.stat()
            except FileNotFoundError:
                directory_names[:] = []
                continue
            if directory_stat.st_dev != root_device:
                directory_names[:] = []
                continue
            directory_names[:] = sorted(directory_names)
            for file_name in sorted(file_names):
                path = directory_path / file_name
                try:
                    stat = path.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not path.is_file() or stat.st_dev != root_device or stat.st_size < threshold:
                    continue
                producer = _producer_for_path(policy, path)
                results.append(
                    {
                        "path": str(path),
                        "inode": int(stat.st_ino),
                        "device": int(stat.st_dev),
                        "size_bytes": int(stat.st_size),
                        "mtime_ns": int(stat.st_mtime_ns),
                        "registered": producer is not None,
                        "owner": None if producer is None else producer["owner"],
                        "classification": None if producer is None else producer["classification"],
                    }
                )
    return sorted(results, key=lambda item: str(item["path"]))


def _filesystem_status(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    stat = resolved.stat()
    vfs = os.statvfs(resolved)
    mount = _mountinfo_for_path(resolved)
    return {
        "path": str(resolved),
        "device": int(stat.st_dev),
        "mount_id": mount.get("mount_id"),
        "mount_point": mount.get("mount_point"),
        "source": mount.get("source"),
        "filesystem_type": mount.get("filesystem_type"),
        "mount_options": mount.get("mount_options"),
        "filesystem_uuid": _filesystem_uuid(str(mount.get("source") or "")),
        "block_size": int(vfs.f_frsize),
        "total_bytes": int(vfs.f_blocks * vfs.f_frsize),
        "free_bytes": int(vfs.f_bfree * vfs.f_frsize),
        "available_bytes": int(vfs.f_bavail * vfs.f_frsize),
        "inode_total": int(vfs.f_files),
        "inode_free": int(vfs.f_ffree),
        "inode_available": int(vfs.f_favail),
    }


def _mountinfo_for_path(path: Path) -> dict[str, Any]:
    candidates: list[tuple[int, dict[str, Any]]] = []
    for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        try:
            separator = fields.index("-")
        except ValueError:
            continue
        mount_point = Path(_unescape_mountinfo(fields[4]))
        try:
            path.relative_to(mount_point)
        except ValueError:
            continue
        candidates.append(
            (
                len(str(mount_point)),
                {
                    "mount_id": int(fields[0]),
                    "mount_point": str(mount_point),
                    "mount_options": fields[5],
                    "filesystem_type": fields[separator + 1],
                    "source": _unescape_mountinfo(fields[separator + 2]),
                },
            )
        )
    if not candidates:
        raise RootStoragePolicyError(f"filesystem mount identity is unavailable: {path}")
    return max(candidates, key=lambda item: item[0])[1]


def _unescape_mountinfo(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _filesystem_uuid(source: str) -> str | None:
    if not source.startswith("/dev/"):
        return None
    completed = subprocess.run(
        ["blkid", "-s", "UUID", "-o", "value", source],
        text=True,
        capture_output=True,
        check=False,
    )
    value = completed.stdout.strip()
    return value or None


def _payload_digest(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()
