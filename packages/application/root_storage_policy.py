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
STORAGE_REGISTRY_CONTRACT = "wb_core_storage_registry_v1"
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
    _validate_storage_registry(payload)
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


def _validate_storage_registry(policy: Mapping[str, Any]) -> None:
    registry = policy.get("storage_registry")
    if (
        not isinstance(registry, Mapping)
        or registry.get("contract_version") != STORAGE_REGISTRY_CONTRACT
    ):
        raise RootStoragePolicyError("canonical storage registry is invalid")
    filesystems = registry.get("filesystems")
    if not isinstance(filesystems, Mapping) or set(filesystems) != {
        "root",
        "backup",
        "generation",
    }:
        raise RootStoragePolicyError("canonical storage filesystem registry is invalid")
    expected_paths = dict(policy.get("filesystems") or {})
    for role, raw in filesystems.items():
        if not isinstance(raw, Mapping):
            raise RootStoragePolicyError("canonical storage filesystem role is invalid")
        path = Path(str(raw.get("path") or ""))
        source = str(raw.get("source") or "")
        filesystem_uuid = str(raw.get("filesystem_uuid") or "")
        filesystem_type = str(raw.get("filesystem_type") or "")
        required_options = raw.get("required_mount_options")
        reserve_mode = str(raw.get("reserve_mode") or "")
        if (
            not path.is_absolute()
            or str(path) != str(expected_paths.get(role) or "")
            or not source.startswith("/dev/")
            or not filesystem_uuid
            or filesystem_type != "ext4"
            or not isinstance(required_options, list)
            or "rw" not in required_options
            or reserve_mode
            not in {
                "root_stage_0_normal",
                "finance_next_replacement_plus_emergency",
                "fixed",
            }
        ):
            raise RootStoragePolicyError("canonical storage filesystem role is invalid")
        if reserve_mode in {"root_stage_0_normal", "fixed"} and int(
            raw.get("reserve_bytes") or 0
        ) <= 0:
            raise RootStoragePolicyError("canonical storage reserve is invalid")
        if reserve_mode == "finance_next_replacement_plus_emergency" and int(
            raw.get("emergency_reserve_bytes") or 0
        ) != 8 * GIB:
            raise RootStoragePolicyError("canonical backup emergency reserve drift")
    lifecycle_policies = registry.get("lifecycle_policies")
    if not isinstance(lifecycle_policies, Mapping) or not lifecycle_policies:
        raise RootStoragePolicyError("canonical storage lifecycle registry is empty")
    for lifecycle_id, lifecycle in lifecycle_policies.items():
        if not str(lifecycle_id) or not isinstance(lifecycle, Mapping):
            raise RootStoragePolicyError("canonical storage lifecycle policy is invalid")
        for key in ("retention_rule", "hold_rule", "compression", "restore_path"):
            if not str(lifecycle.get(key) or "").strip():
                raise RootStoragePolicyError("canonical storage lifecycle policy is incomplete")
        for key in ("rpo_seconds", "rto_seconds"):
            value = lifecycle.get(key)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise RootStoragePolicyError("canonical storage lifecycle objective is invalid")
    storage_producers = registry.get("producers")
    if not isinstance(storage_producers, list) or not storage_producers:
        raise RootStoragePolicyError("canonical storage producer registry is empty")
    storage_owner_ids: set[str] = set()
    for producer in storage_producers:
        if not isinstance(producer, Mapping):
            raise RootStoragePolicyError("canonical storage producer is invalid")
        owner = str(producer.get("owner") or "").strip()
        destination_role = str(producer.get("destination_role") or "").strip()
        relative_roots = producer.get("relative_roots")
        lifecycle_id = str(producer.get("lifecycle_policy") or "").strip()
        capacity_mode = str(producer.get("capacity_mode") or "").strip()
        maximum = producer.get("max_single_write_bytes")
        if (
            not owner
            or owner in storage_owner_ids
            or not isinstance(producer.get("current"), bool)
            or not str(producer.get("data_class") or "").strip()
            or destination_role
            not in {"root", "backup", "generation", "canonical_store", "caller_bound", "ephemeral"}
            or not isinstance(relative_roots, list)
            or any(
                not isinstance(item, str)
                or item.startswith("/")
                or ".." in Path(item).parts
                for item in relative_roots
            )
            or lifecycle_id not in lifecycle_policies
            or not capacity_mode
            or not isinstance(maximum, int)
            or isinstance(maximum, bool)
            or maximum < 0
        ):
            raise RootStoragePolicyError("canonical storage producer is invalid")
        if destination_role in {"root", "backup", "generation"} and not relative_roots:
            raise RootStoragePolicyError("canonical storage producer has no destination root")
        if producer.get("current") is True and capacity_mode == "disabled":
            raise RootStoragePolicyError("current storage producer cannot be disabled")
        storage_owner_ids.add(owner)


def storage_producer_policy(
    owner: str, *, policy: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    resolved = dict(policy or load_policy())
    normalized = str(owner or "").strip()
    registry = dict(resolved.get("storage_registry") or {})
    matches = [
        dict(item)
        for item in list(registry.get("producers") or [])
        if str(item.get("owner") or "") == normalized
    ]
    if len(matches) != 1:
        raise RootStoragePolicyError(f"storage producer owner is unregistered: {normalized}")
    return matches[0]


def storage_destination_root(
    owner: str,
    *,
    relative_root: str | None = None,
    policy: Mapping[str, Any] | None = None,
) -> Path:
    """Resolve one producer-owned artifact root from the canonical registry."""

    resolved = dict(policy or load_policy())
    producer = storage_producer_policy(owner, policy=resolved)
    if producer.get("current") is not True:
        raise RootStoragePolicyError(f"storage producer has no current write authority: {owner}")
    role = str(producer.get("destination_role") or "")
    if role not in {"root", "backup", "generation"}:
        raise RootStoragePolicyError(
            f"storage producer does not own a persistent destination root: {owner}"
        )
    roots = [str(item) for item in producer.get("relative_roots") or []]
    chosen = roots[0] if relative_root is None else str(relative_root)
    if chosen not in roots:
        raise RootStoragePolicyError(
            f"storage producer destination root is not registered: {owner}:{chosen}"
        )
    registry = dict(resolved["storage_registry"])
    filesystem = dict(dict(registry["filesystems"])[role])
    base = Path(str(filesystem["path"]))
    destination = (base / chosen).resolve(strict=False)
    _assert_descendant(destination, base.resolve(strict=False))
    return destination


def resolve_storage_destination(
    owner: str,
    *relative_parts: str,
    relative_root: str | None = None,
    policy: Mapping[str, Any] | None = None,
) -> Path:
    root = storage_destination_root(
        owner,
        relative_root=relative_root,
        policy=policy,
    )
    destination = root.joinpath(*(str(item) for item in relative_parts)).resolve(strict=False)
    _assert_descendant(destination, root)
    return destination


def resolve_runtime_storage_destination(
    owner: str,
    runtime_dir: Path,
    *relative_parts: str,
    relative_root: str | None = None,
    policy: Mapping[str, Any] | None = None,
) -> Path:
    """Resolve hosted storage while preserving isolated test-runtime topology."""

    runtime = Path(runtime_dir).resolve(strict=False)
    canonical_runtime = Path("/opt/wb-core-runtime/state")
    if runtime == canonical_runtime:
        return resolve_storage_destination(
            owner,
            *relative_parts,
            relative_root=relative_root,
            policy=policy,
        )
    resolved = dict(policy or load_policy())
    producer = storage_producer_policy(owner, policy=resolved)
    role = str(producer.get("destination_role") or "")
    roots = [str(item) for item in producer.get("relative_roots") or []]
    chosen = roots[0] if relative_root is None else str(relative_root)
    if chosen not in roots or role not in {"root", "backup", "generation"}:
        raise RootStoragePolicyError(
            f"isolated runtime storage destination is not registered: {owner}:{chosen}"
        )
    role_base = {
        "root": runtime,
        "backup": runtime / "backups",
        "generation": runtime / "generations",
    }[role]
    root = (role_base / chosen).resolve(strict=False)
    destination = root.joinpath(*(str(item) for item in relative_parts)).resolve(strict=False)
    _assert_descendant(destination, root)
    return destination


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
    predicted_temporary_bytes: int = 0,
    predicted_readback_bytes: int = 0,
    control_reserve_bytes: int = 0,
    policy: Mapping[str, Any] | None = None,
    root_path: Path = Path("/"),
) -> dict[str, Any]:
    """Admit one explicitly owned write on its registry-bound filesystem.

    Essential bounded business writes remain separately classified and continue
    to rely on their domain-specific capacity/transaction guard. Discretionary
    backup/evidence writers must use the canonical producer destination and
    preserve the shared Finance-plus-8-GiB backup floor. This function records
    no reservation: all current discretionary full-copy producers are reviewed
    one-shot contours, while scheduled/domain writers retain their existing
    serializing reservation or replacement state machines.
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
    predicted_temporary = int(predicted_temporary_bytes)
    predicted_readback = int(predicted_readback_bytes)
    control_reserve = int(control_reserve_bytes)
    if min(predicted_temporary, predicted_readback, control_reserve) < 0:
        raise RootStoragePolicyError("large write capacity components must be non-negative")
    predicted_peak = (
        predicted
        + predicted_temporary
        + predicted_readback
        + control_reserve
    )

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

    storage_producer = storage_producer_policy(
        normalized_owner,
        policy=resolved_policy,
    )
    destination_role = str(storage_producer.get("destination_role") or "")
    hosted_destination = _is_descendant(
        destination,
        Path("/opt/wb-core-runtime"),
    ) or _is_descendant(destination, Path("/var/backups"))
    enforce_canonical_destination = _hosted_runtime_marker_present() or hosted_destination
    current_storage_authority = storage_producer.get("current") is True
    if not current_storage_authority and enforce_canonical_destination:
        raise RootStoragePolicyError(
            f"storage producer has no current write authority: {normalized_owner}"
        )
    maximum = int(storage_producer.get("max_single_write_bytes") or 0)
    if current_storage_authority and (maximum <= 0 or predicted_peak > maximum):
        raise RootStoragePolicyError(
            "large write exceeds registered producer quota: "
            f"owner={normalized_owner}, predicted_peak_bytes={predicted_peak}, "
            f"max_single_write_bytes={maximum}"
        )
    if destination_role in {"root", "backup", "generation"} and enforce_canonical_destination:
        allowed_roots = [
            storage_destination_root(
                normalized_owner,
                relative_root=str(relative_root),
                policy=resolved_policy,
            )
            for relative_root in storage_producer.get("relative_roots") or []
        ]
        if not any(_is_descendant(destination, root) for root in allowed_roots):
            raise RootStoragePolicyError(
                "large write destination bypasses canonical storage registry: "
                f"owner={normalized_owner}, destination={destination}, "
                f"destination_role={destination_role}"
            )
    elif destination_role == "caller_bound":
        raise RootStoragePolicyError(
            "caller-bound backup primitive requires the concrete caller owner"
        )

    existing_parent = _nearest_existing_parent(destination)
    root_identity = os.stat(root_path)
    destination_identity = os.stat(existing_parent)
    destination_on_root = destination_identity.st_dev == root_identity.st_dev
    filesystem = os.statvfs(existing_parent)
    available = int(filesystem.f_bavail * filesystem.f_frsize)
    predicted_free_after = available - predicted_peak
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
        elif predicted_peak >= LARGE_OUTPUT_BYTES and predicted_free_after < CRITICAL_AVAILABLE_BYTES:
            allowed = False
            reason = "large_output_predicted_free_after_below_critical_floor"
    reserve_bytes = 0
    reserve_mode = "domain_guard"
    if destination_role in {"root", "backup", "generation"} and enforce_canonical_destination:
        role_policy = dict(
            dict(dict(resolved_policy["storage_registry"])["filesystems"])[
                destination_role
            ]
        )
        _assert_filesystem_identity(
            existing_parent,
            role=destination_role,
            contract=role_policy,
        )
        reserve_mode = str(role_policy.get("reserve_mode") or "")
        if classification != CLASS_ESSENTIAL:
            reserve_bytes = _required_reserve_bytes(
                destination_role,
                resolved_policy,
                destination_path=existing_parent,
            )
            if predicted_free_after < reserve_bytes:
                allowed = False
                reason = f"{destination_role}_predicted_free_after_below_reserve"

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
        "predicted_temporary_bytes": predicted_temporary,
        "predicted_readback_bytes": predicted_readback,
        "control_reserve_bytes": control_reserve,
        "predicted_peak_bytes": predicted_peak,
        "predicted_free_after_bytes": predicted_free_after,
        "required_reserve_bytes": reserve_bytes,
        "reserve_mode": reserve_mode,
        "destination_role": destination_role,
        "producer_quota_bytes": maximum,
        "isolated_retired_compatibility": not current_storage_authority,
        "large_output": predicted_peak >= LARGE_OUTPUT_BYTES,
        "storage_level": level,
        "allowed": allowed,
        "reason": reason,
    }
    if not allowed:
        raise RootStoragePolicyError(
            "root storage admission denied: "
            f"owner={normalized_owner}, reason={reason}, available_bytes={available}, "
            f"predicted_output_bytes={predicted}, predicted_peak_bytes={predicted_peak}, "
            f"predicted_free_after_bytes={predicted_free_after}"
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
    storage_status = _collect_storage_registry_status(
        resolved_policy,
        observed_filesystems=filesystems,
    )
    alerts.extend(storage_status["alerts"])
    return {
        "contract_version": CONTRACT_VERSION,
        "collected_at": collected_at.isoformat().replace("+00:00", "Z"),
        "policy_sha256": _payload_digest(resolved_policy),
        "status": level,
        "thresholds_bytes": dict(resolved_policy["thresholds_bytes"]),
        "filesystems": filesystems,
        "large_root_files": large_files,
        "unregistered_large_root_files": unregistered,
        "storage_registry": storage_status,
        "alerts": alerts,
        "safe_for_discretionary_root_writes": (
            level != "hard"
            and not unregistered
            and not storage_status["current_root_producer_violations"]
        ),
    }


def _collect_storage_registry_status(
    policy: Mapping[str, Any],
    *,
    observed_filesystems: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    registry = dict(policy.get("storage_registry") or {})
    role_contracts = dict(registry.get("filesystems") or {})
    alerts: list[dict[str, Any]] = []
    role_status: dict[str, dict[str, Any]] = {}
    finance_floor: dict[str, Any] | None = None
    for role in ("root", "backup", "generation"):
        if role not in observed_filesystems:
            continue
        contract = dict(role_contracts[role])
        observed = dict(observed_filesystems[role])
        required_options = {str(item) for item in contract.get("required_mount_options") or []}
        observed_options = {
            item.strip()
            for item in str(observed.get("mount_options") or "").split(",")
            if item.strip()
        }
        identity_errors: list[str] = []
        for key in ("source", "filesystem_uuid", "filesystem_type"):
            if str(observed.get(key) or "") != str(contract.get(key) or ""):
                identity_errors.append(key)
        missing_options = sorted(required_options - observed_options)
        if missing_options:
            identity_errors.append("required_mount_options")
        if "ro" in observed_options:
            identity_errors.append("read_only")
        reserve_error = ""
        try:
            if role == "backup":
                finance_floor = _finance_backup_floor(policy)
                reserve_bytes = int(finance_floor["required_reserve_bytes"])
            else:
                reserve_bytes = _required_reserve_bytes(
                    role,
                    policy,
                    destination_path=Path(str(observed["path"])),
                )
        except RootStoragePolicyError as exc:
            reserve_bytes = -1
            reserve_error = str(exc)
        available = int(observed.get("available_bytes") or 0)
        reserve_breached = reserve_bytes < 0 or available < reserve_bytes
        if identity_errors:
            alerts.append(
                {
                    "code": "storage_filesystem_identity_violation",
                    "severity": "critical",
                    "role": role,
                    "fields": identity_errors,
                }
            )
        if reserve_breached:
            alerts.append(
                {
                    "code": "storage_reserve_breach",
                    "severity": "critical",
                    "role": role,
                    "available_bytes": available,
                    "required_reserve_bytes": reserve_bytes,
                    "reserve_error": reserve_error,
                }
            )
        role_status[role] = {
            "identity_ok": not identity_errors,
            "identity_error_fields": identity_errors,
            "reserve_mode": str(contract.get("reserve_mode") or ""),
            "required_reserve_bytes": reserve_bytes,
            "available_bytes": available,
            "available_after_reserve_bytes": (
                available - reserve_bytes if reserve_bytes >= 0 else None
            ),
            "reserve_breached": reserve_breached,
        }
    producers = [dict(item) for item in list(registry.get("producers") or [])]
    current_root_violations = [
        {
            "owner": str(item["owner"]),
            "data_class": str(item["data_class"]),
        }
        for item in producers
        if item.get("current") is True
        and item.get("destination_role") == "root"
        and item.get("data_class")
        not in {"canonical_business_store", "protected_excluded_promo_artifact"}
    ]
    unregistered_destination_violations: list[dict[str, Any]] = []
    for role in ("backup", "generation"):
        observed = observed_filesystems.get(role)
        if observed is None:
            continue
        unregistered_destination_violations.extend(
            _scan_unregistered_large_destinations(
                policy,
                role=role,
                filesystem_root=Path(str(observed["path"])),
                filesystem_device=int(observed["device"]),
            )
        )
    if current_root_violations:
        alerts.append(
            {
                "code": "current_large_artifact_producer_targets_root",
                "severity": "critical",
                "count": len(current_root_violations),
            }
        )
    if unregistered_destination_violations:
        alerts.append(
            {
                "code": "unregistered_large_storage_destination",
                "severity": "critical",
                "count": len(unregistered_destination_violations),
            }
        )
    lifecycle_matrix = []
    lifecycle_policies = dict(registry.get("lifecycle_policies") or {})
    for producer in producers:
        lifecycle = dict(lifecycle_policies[str(producer["lifecycle_policy"])])
        lifecycle_matrix.append(
            {
                "owner": str(producer["owner"]),
                "current": bool(producer["current"]),
                "data_class": str(producer["data_class"]),
                "destination_role": str(producer["destination_role"]),
                "relative_roots": list(producer["relative_roots"]),
                "lifecycle_policy": str(producer["lifecycle_policy"]),
                "retention_rule": str(lifecycle["retention_rule"]),
                "hold_rule": str(lifecycle["hold_rule"]),
                "compression": str(lifecycle["compression"]),
                "restore_path": str(lifecycle["restore_path"]),
                "rpo_seconds": lifecycle.get("rpo_seconds"),
                "rto_seconds": lifecycle.get("rto_seconds"),
            }
        )
    return {
        "contract_version": STORAGE_REGISTRY_CONTRACT,
        "roles": role_status,
        "finance_backup_floor": finance_floor,
        "current_producer_count": sum(1 for item in producers if item.get("current") is True),
        "current_root_producer_violations": current_root_violations,
        "unregistered_destination_violations": unregistered_destination_violations,
        "active_generic_reservations": [],
        "stale_generic_reservations": [],
        "generic_reservation_mode": "not_required_current_producers_are_serialized_or_domain_guarded",
        "lifecycle_matrix": lifecycle_matrix,
        "alerts": alerts,
        "ok": not alerts,
    }


def _scan_unregistered_large_destinations(
    policy: Mapping[str, Any],
    *,
    role: str,
    filesystem_root: Path,
    filesystem_device: int,
) -> list[dict[str, Any]]:
    """Find large files outside every versioned destination root for one role."""

    root = filesystem_root.resolve()
    threshold = int(dict(policy["thresholds_bytes"])["large_output"])
    registry = dict(policy.get("storage_registry") or {})
    registered_roots: list[tuple[str, Path]] = []
    for producer in list(registry.get("producers") or []):
        if producer.get("destination_role") != role:
            continue
        for relative_root in producer.get("relative_roots") or []:
            candidate = (root / str(relative_root)).resolve(strict=False)
            _assert_descendant(candidate, root)
            registered_roots.append((str(producer["owner"]), candidate))
    violations: list[dict[str, Any]] = []
    for directory, directory_names, file_names in os.walk(root):
        directory_path = Path(directory)
        try:
            directory_stat = directory_path.stat()
        except FileNotFoundError:
            directory_names[:] = []
            continue
        if directory_stat.st_dev != filesystem_device:
            directory_names[:] = []
            continue
        directory_names[:] = sorted(directory_names)
        for file_name in sorted(file_names):
            path = directory_path / file_name
            try:
                file_stat = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_dev != filesystem_device
                or file_stat.st_size < threshold
            ):
                continue
            candidate_owners = sorted(
                {
                    owner
                    for owner, registered_root in registered_roots
                    if _is_descendant(path, registered_root)
                }
            )
            if candidate_owners:
                continue
            violations.append(
                {
                    "role": role,
                    "path": str(path),
                    "size_bytes": int(file_stat.st_size),
                    "device": int(file_stat.st_dev),
                    "reason": "no_registered_destination_root",
                }
            )
    return sorted(violations, key=lambda item: str(item["path"]))


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
    storage_registry = payload.get("storage_registry")
    if (
        not isinstance(storage_registry, dict)
        or storage_registry.get("contract_version") != STORAGE_REGISTRY_CONTRACT
        or not isinstance(storage_registry.get("roles"), dict)
        or not isinstance(storage_registry.get("alerts"), list)
    ):
        raise RootStoragePolicyError(
            "root storage status artifact lacks canonical storage registry evidence"
        )
    expected_safe = (
        payload.get("status") != "hard"
        and not unregistered
        and not storage_registry.get("current_root_producer_violations")
    )
    if payload.get("safe_for_discretionary_root_writes") is not expected_safe:
        raise RootStoragePolicyError("root storage status artifact safety flag drift")
    return {
        "ok": not unregistered and bool(storage_registry.get("ok")),
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


def _is_descendant(path: Path, root: Path) -> bool:
    try:
        Path(path).relative_to(Path(root))
    except ValueError:
        return False
    return True


def _hosted_runtime_marker_present() -> bool:
    return Path("/opt/wb-core-runtime/app/.wb-core-runtime-sha").is_file()


def _assert_descendant(path: Path, root: Path) -> None:
    if not _is_descendant(Path(path), Path(root)):
        raise RootStoragePolicyError(
            f"storage destination escapes registered root: path={path}, root={root}"
        )


def _assert_filesystem_identity(
    path: Path,
    *,
    role: str,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    observed = _filesystem_status(Path(path))
    required_options = {str(item) for item in contract.get("required_mount_options") or []}
    observed_options = {
        item.strip()
        for item in str(observed.get("mount_options") or "").split(",")
        if item.strip()
    }
    expected = {
        "source": str(contract.get("source") or ""),
        "filesystem_uuid": str(contract.get("filesystem_uuid") or ""),
        "filesystem_type": str(contract.get("filesystem_type") or ""),
    }
    mismatches = {
        key: {"expected": value, "observed": observed.get(key)}
        for key, value in expected.items()
        if str(observed.get(key) or "") != value
    }
    missing_options = sorted(required_options - observed_options)
    if mismatches or missing_options or "ro" in observed_options:
        raise RootStoragePolicyError(
            "canonical storage filesystem identity mismatch: "
            f"role={role}, mismatches={mismatches}, missing_options={missing_options}"
        )
    return observed


def _finance_backup_floor(policy: Mapping[str, Any]) -> dict[str, Any]:
    registry = dict(policy.get("storage_registry") or {})
    role = dict(dict(registry.get("filesystems") or {})["backup"])
    backup_root = Path(str(role["path"]))
    try:
        from packages.application.finance_storage_backup_rotation import (
            backup_rotation_health,
        )

        health = backup_rotation_health(backup_root.parent)
    except Exception as exc:  # pragma: no cover - fail-closed environment boundary
        raise RootStoragePolicyError(
            f"Finance backup reserve evidence is unavailable: {type(exc).__name__}: {exc}"
        ) from exc
    next_replacement = int(health.get("next_replacement_required_bytes") or 0)
    emergency = int(role.get("emergency_reserve_bytes") or 0)
    blockers = list(health.get("blockers") or [])
    if (
        health.get("status") != "healthy"
        or health.get("next_replacement_capacity") is not True
        or blockers
        or next_replacement <= 0
        or emergency != 8 * GIB
    ):
        raise RootStoragePolicyError(
            "Finance backup reserve evidence is not healthy: "
            f"status={health.get('status')}, blockers={blockers}"
        )
    return {
        "finance_next_replacement_required_bytes": next_replacement,
        "emergency_reserve_bytes": emergency,
        "required_reserve_bytes": next_replacement + emergency,
        "retained_backup_id": str(health.get("retained_backup_id") or ""),
        "retained_count": int(health.get("retained_count") or 0),
        "retained_bytes": int(health.get("retained_bytes") or 0),
        "rpo_seconds": int(health.get("rpo_seconds") or 0),
        "rto_seconds": int(health.get("rto_seconds") or 0),
    }


def _required_reserve_bytes(
    role: str,
    policy: Mapping[str, Any],
    *,
    destination_path: Path,
) -> int:
    del destination_path
    registry = dict(policy.get("storage_registry") or {})
    role_policy = dict(dict(registry.get("filesystems") or {})[role])
    reserve_mode = str(role_policy.get("reserve_mode") or "")
    if reserve_mode == "finance_next_replacement_plus_emergency":
        return int(_finance_backup_floor(policy)["required_reserve_bytes"])
    if reserve_mode in {"fixed", "root_stage_0_normal"}:
        return int(role_policy.get("reserve_bytes") or 0)
    raise RootStoragePolicyError(f"unknown storage reserve mode: {role}:{reserve_mode}")


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
