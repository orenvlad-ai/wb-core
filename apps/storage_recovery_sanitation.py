#!/usr/bin/env python3
"""Inventory and sanitize exact legacy backup families without broad deletion."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sqlite_backup_archive import (  # noqa: E402
    DEFAULT_RESERVED_FREE_BYTES,
    apply_archive,
    build_plan as build_archive_plan,
    verify_archive_manifest,
)


CONTRACT_NAME = "storage_recovery_sanitation_v1"
DEFAULT_RUNTIME_DIR = Path("/opt/wb-core-runtime/state")
DEFAULT_ROOT_BACKUPS = Path("/opt/wb-core-runtime/backups")
AUDIT_DIRECTORY_NAME = "storage-recovery-sanitation"

# Every mutable family is named explicitly. Directories not present here remain
# visible in inventory as foreign_non_target and can never be selected by apply.
FAMILY_POLICIES: dict[str, dict[str, dict[str, Any]]] = {
    "root": {
        "ads-historical": {"retain_verified": 1},
        "ff-stock-targeted-reconciliation": {"retain_verified": 1},
        "warehouse-archival-estimate": {"retain_verified": 1},
        "warehouse-functional": {"retain_verified": 1},
        "warehouse-functional-economics": {"retain_verified": 1},
        "warehouse-functional-recovery": {"retain_verified": 1},
        "warehouse-functional-sync": {"retain_verified": 1},
        "warehouse-opening": {"retain_verified": 1},
        "warehouse-supplier-certification-replay": {"retain_verified": 1},
        "wb-finance-canonical": {"retain_verified": 1},
    },
    "backup": {
        "calculation-parameters": {"retain_verified": 3},
        "canonical-cost-engine": {"retain_verified": 1},
        "canonical-vitrina-publication": {"retain_verified": 1},
        "promo_metric_eligibility_recompute": {"retain_verified": 1},
        # WBC0008 block 006 is populated only by the dedicated exact-six
        # cross-filesystem warm-archive contract.  It is registered for
        # inventory/retention ownership, but the generic family-at-a-time
        # mutator must never act on it.
        "root-warm-archive-wbc0008-006": {
            "retain_verified": 6,
            "managed_by": "root_storage_warm_archive_wbc0008_006_v1",
        },
        "sheet_vitrina_v1_proxy_margin_3_historical_backfill": {
            "retain_verified": 1
        },
        # Three independent exact recovery points are retained, but only in
        # verified compressed form.
        "supplier-26gn390-recovery": {"retain_verified": 3},
        "supplier-26gn527-vtb-recovery": {"retain_verified": 1},
        "supplier-cny-payment-10-recovery": {"retain_verified": 1},
        "supplier-factual-date-corrections": {"retain_verified": 1},
        "supplier_factual_date_corrections": {"retain_verified": 1},
        "warehouse-functional-sync": {"retain_verified": 1},
    },
}


class SanitationError(RuntimeError):
    """Fail-closed sanitation contract violation."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-dir",
        default=str(DEFAULT_RUNTIME_DIR),
        help="Canonical runtime state directory.",
    )
    parser.add_argument(
        "--root-backups",
        default=str(DEFAULT_ROOT_BACKUPS),
        help="Canonical root-filesystem backup directory.",
    )
    parser.add_argument(
        "--deployed-sha",
        default="",
        help="Exact deployed SHA required by production apply.",
    )
    parser.add_argument(
        "--deployed-sha-file",
        default=str(ROOT / ".wb-core-runtime-sha"),
        help="Canonical deployed SHA marker.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("inventory")
    for name in ("plan", "apply"):
        child = subparsers.add_parser(name)
        child.add_argument("--root", choices=("root", "backup"), required=True)
        child.add_argument("--family", required=True)
        child.add_argument("--fingerprint", default="")
        child.add_argument(
            "--reserved-free-bytes",
            type=int,
            default=DEFAULT_RESERVED_FREE_BYTES,
        )
    return parser


def inventory(
    *,
    runtime_dir: Path,
    root_backups: Path,
) -> dict[str, Any]:
    roots = _canonical_roots(
        runtime_dir=runtime_dir,
        root_backups=root_backups,
    )
    result: list[dict[str, Any]] = []
    for root_name, root_path in roots.items():
        policies = FAMILY_POLICIES[root_name]
        for path in sorted(root_path.iterdir(), key=lambda item: item.name):
            if not path.is_dir() or path.is_symlink():
                classification = "foreign_non_target"
            else:
                classification = (
                    "managed_exact_allowlist"
                    if path.name in policies
                    else "foreign_non_target"
                )
            files = (
                _inventory_files(path)
                if path.is_dir()
                else [_path_summary(path)]
            )
            result.append(
                {
                    "root": root_name,
                    "root_path": str(root_path),
                    "family": path.name,
                    "family_path": str(path),
                    "classification": classification,
                    "policy": policies.get(path.name),
                    "file_count": len(files),
                    "size_bytes": sum(
                        int(item["size_bytes"]) for item in files
                    ),
                    "newest_mtime_ns": max(
                        (int(item["mtime_ns"]) for item in files),
                        default=0,
                    ),
                    "files": files,
                }
            )
    material = {
        "contract_name": CONTRACT_NAME,
        "roots": {key: str(value) for key, value in roots.items()},
        "filesystems": {
            key: _filesystem_inventory(value)
            for key, value in roots.items()
        },
        "families": result,
    }
    return {
        **material,
        "status": "inventory_ready",
        "read_only": True,
        "fingerprint": _fingerprint(material),
        "total_bytes": sum(int(item["size_bytes"]) for item in result),
        "managed_bytes": sum(
            int(item["size_bytes"])
            for item in result
            if item["classification"] == "managed_exact_allowlist"
        ),
        "foreign_non_target_bytes": sum(
            int(item["size_bytes"])
            for item in result
            if item["classification"] == "foreign_non_target"
        ),
    }


def plan_family(
    *,
    runtime_dir: Path,
    root_backups: Path,
    root_name: str,
    family: str,
    reserved_free_bytes: int = DEFAULT_RESERVED_FREE_BYTES,
) -> dict[str, Any]:
    roots = _canonical_roots(
        runtime_dir=runtime_dir,
        root_backups=root_backups,
    )
    family_path, policy = _resolve_family(
        roots=roots,
        root_name=root_name,
        family=family,
    )
    if policy.get("managed_by"):
        raise SanitationError(
            "family is owned by a dedicated exact lifecycle contract"
        )
    if int(reserved_free_bytes) < 0:
        raise SanitationError("reserved free bytes must be non-negative")
    analysis = _analyze_family(family_path)
    base = {
        "contract_name": CONTRACT_NAME,
        "root": root_name,
        "root_path": str(roots[root_name]),
        "family": family,
        "family_path": str(family_path),
        "policy": policy,
        "family_size_bytes": analysis["family_size_bytes"],
        "verified_archive_count": len(analysis["verified"]),
        "raw_sqlite_count": len(analysis["raw_sqlite"]),
        "foreign_file_count": len(analysis["foreign"]),
        "corrupt": analysis["corrupt"],
    }
    if analysis["corrupt"]:
        material = {
            **base,
            "action": "critical_stop",
            "reason": "archive_or_manifest_integrity_mismatch",
        }
        return {
            **material,
            "status": "critical_stop",
            "read_only": True,
            "would_change": False,
            "fingerprint": _plan_fingerprint(material),
        }

    raw_sqlite = sorted(
        analysis["raw_sqlite"],
        key=lambda item: (int(item["mtime_ns"]), str(item["path"])),
        reverse=True,
    )
    if raw_sqlite:
        source = Path(str(raw_sqlite[0]["path"]))
        archive_plan = build_archive_plan(
            source=source,
            archive=None,
            staging_directory=source.parent,
            reserved_free_bytes=int(reserved_free_bytes),
        )
        material = {
            **base,
            "action": "archive_raw_sqlite",
            "target_paths": [
                str(source),
                str(archive_plan["archive_path"]),
                str(archive_plan["archive_path"]) + ".manifest.json",
            ],
            "archive_plan": archive_plan,
        }
        return {
            **material,
            "status": "dry_run_ready",
            "read_only": True,
            "would_change": True,
            "expected_freed_bytes": max(
                0,
                int(archive_plan["source_size_bytes"])
                - int(
                    (archive_plan.get("capacity_requirement") or {}).get(
                        "projected_archive_size_bytes"
                    )
                    or 0
                ),
            ),
            "fingerprint": _plan_fingerprint(material),
        }

    verified = sorted(
        analysis["verified"],
        key=lambda item: (
            _source_generation_mtime_ns(item),
            str(item["archive_path"]),
        ),
        reverse=True,
    )
    retain_count = int(policy["retain_verified"])
    superseded = verified[retain_count:]
    if superseded:
        targets = [
            target
            for bundle in superseded
            for target in _bundle_targets(bundle, include_sidecars=True)
        ]
        material = _cleanup_material(
            base=base,
            family_path=family_path,
            action="remove_superseded_verified_generation",
            targets=targets,
            retained=[
                _archive_identity(item) for item in verified[:retain_count]
            ],
        )
        return {
            **material,
            "status": "dry_run_ready",
            "read_only": True,
            "would_change": True,
            "expected_freed_bytes": sum(
                int(item["size_bytes"]) for item in targets
            ),
            "fingerprint": _plan_fingerprint(material),
        }

    sidecars = [
        sidecar
        for bundle in verified
        for sidecar in _owned_source_sidecars(bundle)
    ]
    if sidecars:
        material = _cleanup_material(
            base=base,
            family_path=family_path,
            action="remove_verified_owned_sidecars",
            targets=sidecars,
            retained=[_archive_identity(item) for item in verified],
        )
        return {
            **material,
            "status": "dry_run_ready",
            "read_only": True,
            "would_change": True,
            "expected_freed_bytes": sum(
                int(item["size_bytes"]) for item in sidecars
            ),
            "fingerprint": _plan_fingerprint(material),
        }

    material = {
        **base,
        "action": "none",
        "retained": [_archive_identity(item) for item in verified],
        "foreign_files": analysis["foreign"],
    }
    return {
        **material,
        "status": "no_change",
        "read_only": True,
        "would_change": False,
        "expected_freed_bytes": 0,
        "fingerprint": _plan_fingerprint(material),
    }


def apply_family(
    *,
    runtime_dir: Path,
    root_backups: Path,
    root_name: str,
    family: str,
    fingerprint: str,
    deployed_sha: str,
    deployed_sha_file: Path | None = None,
    reserved_free_bytes: int = DEFAULT_RESERVED_FREE_BYTES,
) -> dict[str, Any]:
    approved = str(fingerprint or "").strip()
    if not approved:
        raise SanitationError("apply requires the exact dry-run fingerprint")
    _verify_deployed_sha(
        deployed_sha=deployed_sha,
        deployed_sha_file=(
            deployed_sha_file
            if deployed_sha_file is not None
            else runtime_dir.expanduser().resolve() / ".wb-core-runtime-sha"
        ),
    )
    roots = _canonical_roots(
        runtime_dir=runtime_dir,
        root_backups=root_backups,
    )
    family_path, _policy = _resolve_family(
        roots=roots,
        root_name=root_name,
        family=family,
    )
    audit_path = _audit_path(
        runtime_dir=runtime_dir,
        fingerprint=approved,
    )
    if audit_path.is_file():
        run_record = _read_audit(audit_path)
        if str(run_record.get("fingerprint") or "") != approved:
            raise SanitationError("sanitation audit fingerprint mismatch")
        if str(run_record.get("status") or "") == "applied":
            return {
                **run_record,
                "idempotent": True,
                "applied": False,
            }
        plan = dict(run_record["plan"])
    else:
        plan = plan_family(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            root_name=root_name,
            family=family,
            reserved_free_bytes=reserved_free_bytes,
        )
        if approved != str(plan["fingerprint"]):
            raise SanitationError(
                "apply requires the exact current dry-run fingerprint"
            )
        if not bool(plan.get("would_change")):
            return {
                **plan,
                "status": "no_change",
                "applied": False,
                "idempotent": True,
            }
        run_record = {
            "contract_name": CONTRACT_NAME,
            "fingerprint": approved,
            "status": "applying",
            "deployed_sha": deployed_sha,
            "started_at": _now(),
            "plan": plan,
        }
        _write_audit(audit_path, run_record)

    action = str(plan.get("action") or "")
    if action == "archive_raw_sqlite":
        result = _apply_archive_action(
            plan=plan,
            reserved_free_bytes=reserved_free_bytes,
        )
    elif action in {
        "remove_superseded_verified_generation",
        "remove_verified_owned_sidecars",
    }:
        result = _apply_cleanup_action(
            plan=plan,
            family_path=family_path,
        )
    else:
        raise SanitationError(f"unsupported sanitation action: {action}")
    completed = {
        **run_record,
        "status": "applied",
        "completed_at": _now(),
        "result": result,
        "applied": True,
        "idempotent": False,
        "filesystem_available_bytes_after": int(
            os.statvfs(family_path).f_bavail
            * os.statvfs(family_path).f_frsize
        ),
    }
    _write_audit(audit_path, completed)
    return completed


def _apply_archive_action(
    *,
    plan: dict[str, Any],
    reserved_free_bytes: int,
) -> dict[str, Any]:
    archive_plan = dict(plan["archive_plan"])
    source = Path(str(archive_plan["source_path"]))
    manifest = Path(
        str(archive_plan["archive_path"]) + ".manifest.json"
    )
    if not source.exists():
        verified = verify_archive_manifest(manifest)
        if not bool(verified.get("source_removed")):
            raise SanitationError(
                "archive exists but source removal lifecycle is incomplete"
            )
        return {
            "status": "archived",
            "resume_reconciled": True,
            "archive": _archive_identity(verified),
            "freed_bytes": max(
                0,
                int(verified["source_size_bytes"])
                - int(verified["archive_size_bytes"]),
            ),
        }
    result = apply_archive(
        source=source,
        archive=Path(str(archive_plan["archive_path"])),
        staging_directory=Path(str(archive_plan["staging_directory"])),
        fingerprint=str(archive_plan["fingerprint"]),
        reserved_free_bytes=int(reserved_free_bytes),
    )
    verified = dict(result["archive"])
    return {
        "status": "archived",
        "resume_reconciled": False,
        "archive": _archive_identity(verified),
        "restore_probe": {
            "zstd_test": verified["zstd_test"],
            "decompressed_sha256": verified[
                "actual_decompressed_sha256"
            ],
            "decompressed_size_bytes": verified[
                "actual_decompressed_size_bytes"
            ],
        },
        "freed_bytes": max(
            0,
            int(verified["source_size_bytes"])
            - int(verified["archive_size_bytes"]),
        ),
        "non_target_digest": verified[
            "directory_non_target_digest_after"
        ],
    }


def _apply_cleanup_action(
    *,
    plan: dict[str, Any],
    family_path: Path,
) -> dict[str, Any]:
    target_rows = list(plan.get("target_identities") or [])
    target_paths = {
        Path(str(item["path"])).resolve() for item in target_rows
    }
    for path in target_paths:
        if path.parent != family_path:
            raise SanitationError("cleanup target escaped exact family")
    for retained in plan.get("retained") or []:
        verify_archive_manifest(
            Path(str(retained["manifest_path"]))
        )
    removed: list[dict[str, Any]] = []
    for item in target_rows:
        path = Path(str(item["path"]))
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise SanitationError("cleanup target is no longer a regular file")
        if _file_identity(path) != item:
            raise SanitationError("cleanup target exact identity drifted")
        path.unlink()
        removed.append(item)
        _fsync_directory(family_path)
    non_target_after = _non_target_digest(
        family_path=family_path,
        excluded=target_paths,
    )
    if non_target_after != str(plan["non_target_digest"]):
        raise SanitationError(
            "non-target family digest changed during exact cleanup"
        )
    return {
        "status": "cleaned",
        "removed": removed,
        "removed_bytes": sum(int(item["size_bytes"]) for item in removed),
        "non_target_digest_before": plan["non_target_digest"],
        "non_target_digest_after": non_target_after,
        "restore_probes": [
            {
                "archive_path": retained["archive_path"],
                "source_sha256": retained["source_sha256"],
                "decompressed_sha256": verify_archive_manifest(
                    Path(str(retained["manifest_path"]))
                )["actual_decompressed_sha256"],
            }
            for retained in plan.get("retained") or []
        ],
    }


def _analyze_family(family_path: Path) -> dict[str, Any]:
    raw_sqlite: list[dict[str, Any]] = []
    verified: list[dict[str, Any]] = []
    corrupt: list[dict[str, Any]] = []
    foreign: list[dict[str, Any]] = []
    manifest_paths = sorted(family_path.glob("*.zst.manifest.json"))
    paired: set[Path] = set()
    for manifest_path in manifest_paths:
        archive_path = Path(
            str(manifest_path)[: -len(".manifest.json")]
        )
        paired.update({manifest_path.resolve(), archive_path.resolve()})
        try:
            manifest = verify_archive_manifest(manifest_path)
            source_path = Path(str(manifest.get("source_path") or ""))
            if not bool(manifest.get("source_removed", not source_path.exists())):
                raise ValueError("archive source-removal lifecycle is incomplete")
            verified.append(manifest)
        except Exception as exc:
            corrupt.append(
                {
                    "manifest_path": str(manifest_path),
                    "archive_path": str(archive_path),
                    "error": str(exc),
                }
            )
    for path in sorted(family_path.iterdir(), key=lambda item: item.name):
        resolved = path.resolve()
        if path.is_symlink() or not path.is_file():
            foreign.append(_path_summary(path))
            continue
        if resolved in paired:
            continue
        if path.name.endswith(".sqlite3"):
            if path.name == "registry_upload_runtime.sqlite3":
                corrupt.append(
                    {
                        "path": str(path),
                        "error": "live canonical database name is forbidden",
                    }
                )
            else:
                raw_sqlite.append(_file_identity(path))
            continue
        if path.name.endswith(".zst"):
            corrupt.append(
                {
                    "archive_path": str(path),
                    "error": "archive has no exact manifest pair",
                }
            )
            continue
        foreign.append(_path_summary(path))
    return {
        "family_size_bytes": sum(
            int(item["size_bytes"]) for item in _inventory_files(family_path)
        ),
        "raw_sqlite": raw_sqlite,
        "verified": verified,
        "corrupt": corrupt,
        "foreign": foreign,
    }


def _cleanup_material(
    *,
    base: dict[str, Any],
    family_path: Path,
    action: str,
    targets: list[dict[str, Any]],
    retained: list[dict[str, Any]],
) -> dict[str, Any]:
    excluded = {
        Path(str(item["path"])).resolve() for item in targets
    }
    return {
        **base,
        "action": action,
        "target_identities": targets,
        "retained": retained,
        "non_target_digest": _non_target_digest(
            family_path=family_path,
            excluded=excluded,
        ),
    }


def _bundle_targets(
    bundle: dict[str, Any],
    *,
    include_sidecars: bool,
) -> list[dict[str, Any]]:
    paths = [
        Path(str(bundle["archive_path"])),
        Path(str(bundle["archive_path"]) + ".manifest.json"),
    ]
    rows = [_file_identity(path) for path in paths]
    if include_sidecars:
        rows.extend(_owned_source_sidecars(bundle))
    return rows


def _owned_source_sidecars(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    source_path = Path(str(bundle.get("source_path") or ""))
    if not bool(bundle.get("source_removed", not source_path.exists())):
        return []
    result = []
    recorded_paths: set[Path] = set()
    for expected in bundle.get("source_sidecars") or []:
        path = Path(str(expected.get("path") or ""))
        recorded_paths.add(path)
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise SanitationError("owned archive sidecar is not a regular file")
        actual = _file_identity(path)
        if (
            int(actual["size_bytes"]) != int(expected["size_bytes"])
            or int(actual["inode"]) != int(expected["inode"])
            or int(actual["mtime_ns"]) != int(expected["mtime_ns"])
        ):
            raise SanitationError("owned archive sidecar identity drifted")
        if str(expected.get("suffix") or "") == "-wal" and int(
            actual["size_bytes"]
        ):
            raise SanitationError("owned archive WAL is non-empty")
        result.append(actual)
    # Older standard archive manifests predate explicit owned-sidecar fields.
    # A same-basename SHM or empty WAL cannot contain restore data once the
    # source is absent and its archive has passed decompressed SHA readback.
    for suffix in ("-wal", "-shm"):
        path = Path(str(source_path) + suffix)
        if path in recorded_paths or not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise SanitationError("derived archive sidecar is not a regular file")
        actual = _file_identity(path)
        if suffix == "-wal" and int(actual["size_bytes"]):
            raise SanitationError("derived archive WAL is non-empty")
        result.append(actual)
    return result


def _archive_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    archive = Path(str(manifest["archive_path"]))
    source_mtime_ns = _source_generation_mtime_ns(manifest)
    return {
        "archive_path": str(archive),
        "manifest_path": str(
            archive.with_name(archive.name + ".manifest.json")
        ),
        "archive_size_bytes": int(manifest["archive_size_bytes"]),
        "archive_sha256": str(manifest["archive_sha256"]),
        "source_path": str(manifest["source_path"]),
        "source_size_bytes": int(manifest["source_size_bytes"]),
        "source_sha256": str(manifest["source_sha256"]),
        "source_mtime_ns": source_mtime_ns,
        "source_mtime_origin": (
            "manifest"
            if manifest.get("source_mtime_ns") is not None
            else "legacy_archived_at"
        ),
        "decompressed_size_bytes": int(
            manifest["actual_decompressed_size_bytes"]
        ),
        "decompressed_sha256": str(
            manifest["actual_decompressed_sha256"]
        ),
        "restore_probe": "verified",
    }


def _source_generation_mtime_ns(manifest: dict[str, Any]) -> int:
    """Return a stable generation order for current and legacy manifests."""

    explicit = manifest.get("source_mtime_ns")
    if explicit is not None:
        return int(explicit)
    archived_at = str(manifest.get("archived_at") or "").strip()
    if not archived_at:
        raise SanitationError(
            "verified legacy archive has neither source_mtime_ns nor archived_at"
        )
    try:
        parsed = datetime.fromisoformat(archived_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SanitationError(
            "verified legacy archive has an invalid archived_at"
        ) from exc
    if parsed.tzinfo is None:
        raise SanitationError(
            "verified legacy archive archived_at must include timezone"
        )
    utc = parsed.astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = utc - epoch
    return (
        delta.days * 86_400 * 1_000_000_000
        + delta.seconds * 1_000_000_000
        + delta.microseconds * 1_000
    )


def _canonical_roots(
    *,
    runtime_dir: Path,
    root_backups: Path,
) -> dict[str, Path]:
    runtime_input = runtime_dir.expanduser()
    root_input = root_backups.expanduser()
    if runtime_input.is_symlink() or root_input.is_symlink():
        raise SanitationError("canonical sanitation roots must not be symlinks")
    runtime_dir = runtime_input.resolve()
    root_backups = root_input.resolve()
    backup_root = (runtime_dir / "backups").resolve()
    if (runtime_dir / "backups").is_symlink():
        raise SanitationError("canonical runtime backup root must not be a symlink")
    for path in (runtime_dir, root_backups, backup_root):
        if path.is_symlink() or not path.is_dir():
            raise SanitationError(
                f"canonical sanitation directory is unavailable: {path}"
            )
    if root_backups == backup_root:
        raise SanitationError("canonical backup roots must be distinct")
    return {"root": root_backups, "backup": backup_root}


def _resolve_family(
    *,
    roots: dict[str, Path],
    root_name: str,
    family: str,
) -> tuple[Path, dict[str, Any]]:
    if root_name not in roots:
        raise SanitationError("unknown sanitation root")
    policies = FAMILY_POLICIES[root_name]
    if family not in policies:
        raise SanitationError("family is outside the exact sanitation allowlist")
    family_path = roots[root_name] / family
    if (
        family_path.is_symlink()
        or not family_path.is_dir()
        or family_path.resolve().parent != roots[root_name]
    ):
        raise SanitationError("exact sanitation family is unavailable")
    return family_path.resolve(), dict(policies[family])


def _verify_deployed_sha(
    *,
    deployed_sha: str,
    deployed_sha_file: Path,
) -> None:
    approved = str(deployed_sha or "").strip()
    if len(approved) != 40 or any(
        character not in "0123456789abcdef" for character in approved.lower()
    ):
        raise SanitationError("apply requires an exact deployed SHA")
    marker_input = deployed_sha_file.expanduser()
    if marker_input.is_symlink():
        raise SanitationError("deployed SHA marker must not be a symlink")
    marker = marker_input.resolve()
    if not marker.is_file():
        raise SanitationError("deployed SHA marker is unavailable")
    actual = marker.read_text(encoding="utf-8").strip()
    if actual != approved:
        raise SanitationError(
            f"deployed SHA mismatch: expected={approved}, actual={actual}"
        )


def _audit_path(*, runtime_dir: Path, fingerprint: str) -> Path:
    digest = fingerprint.removeprefix("sha256:")
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest.lower()
    ):
        raise SanitationError("invalid sanitation fingerprint")
    audit_dir = runtime_dir.expanduser().resolve() / AUDIT_DIRECTORY_NAME
    if audit_dir.is_symlink():
        raise SanitationError("sanitation audit directory must not be a symlink")
    return audit_dir / f"{digest}.json"


def _read_audit(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise SanitationError("sanitation audit record is unavailable")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_audit(path: Path, payload: dict[str, Any]) -> None:
    if path.parent.is_symlink():
        raise SanitationError("sanitation audit directory must not be a symlink")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp-{os.getpid()}")
    data = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        with temp.open("xb") as handle:
            os.chmod(temp, 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        _fsync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _inventory_files(path: Path) -> list[dict[str, Any]]:
    if not path.is_dir() or path.is_symlink():
        return []
    result = []
    for item in sorted(path.rglob("*"), key=lambda child: str(child)):
        if item.is_file() and not item.is_symlink():
            result.append(_path_summary(item, relative_to=path))
    return result


def _filesystem_inventory(path: Path) -> dict[str, Any]:
    file_stat = path.stat()
    statvfs = os.statvfs(path)
    usage = shutil.disk_usage(path)
    mount = _mount_identity(path)
    free_including_reserved = int(statvfs.f_bfree * statvfs.f_frsize)
    available = int(statvfs.f_bavail * statvfs.f_frsize)
    return {
        "path": str(path),
        "device_id": int(file_stat.st_dev),
        "mount": mount,
        "capacity_bytes": int(usage.total),
        "used_bytes": int(usage.used),
        "available_bytes": int(usage.free),
        "free_including_reserved_bytes": free_including_reserved,
        "reserved_gap_bytes": max(0, free_including_reserved - available),
        "inode_total": int(statvfs.f_files),
        "inode_free": int(statvfs.f_ffree),
        "inode_available": int(statvfs.f_favail),
    }


def _mount_identity(path: Path) -> dict[str, str]:
    mountinfo = Path("/proc/self/mountinfo")
    if not mountinfo.is_file():
        return {
            "mount_point": "",
            "filesystem_type": "",
            "source": "",
        }
    matches = []
    for line in mountinfo.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if "-" not in fields or len(fields) < 10:
            continue
        separator = fields.index("-")
        mount_point = Path(
            fields[4]
            .replace("\\040", " ")
            .replace("\\011", "\t")
            .replace("\\134", "\\")
        )
        try:
            path.relative_to(mount_point)
        except ValueError:
            continue
        matches.append(
            (
                len(mount_point.parts),
                {
                    "mount_point": str(mount_point),
                    "filesystem_type": fields[separator + 1],
                    "source": fields[separator + 2],
                },
            )
        )
    if not matches:
        return {
            "mount_point": "",
            "filesystem_type": "",
            "source": "",
        }
    return max(matches, key=lambda item: item[0])[1]


def _path_summary(
    path: Path,
    *,
    relative_to: Path | None = None,
) -> dict[str, Any]:
    path_stat = path.lstat()
    return {
        "path": str(path),
        "relative_path": (
            str(path.relative_to(relative_to)) if relative_to else path.name
        ),
        "size_bytes": int(path_stat.st_size)
        if stat.S_ISREG(path_stat.st_mode)
        else 0,
        "mtime_ns": int(path_stat.st_mtime_ns),
        "mode": oct(path_stat.st_mode & 0o777),
        "kind": (
            "symlink"
            if stat.S_ISLNK(path_stat.st_mode)
            else "file"
            if stat.S_ISREG(path_stat.st_mode)
            else "other"
        ),
    }


def _file_identity(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise SanitationError(f"exact target is not a regular file: {path}")
    path_stat = path.stat()
    return {
        "path": str(path.resolve()),
        "size_bytes": int(path_stat.st_size),
        "inode": int(path_stat.st_ino),
        "mtime_ns": int(path_stat.st_mtime_ns),
        "mode": oct(path_stat.st_mode & 0o777),
        "sha256": _file_hash(path),
    }


def _non_target_digest(
    *,
    family_path: Path,
    excluded: set[Path],
) -> str:
    rows = []
    for path in sorted(family_path.iterdir(), key=lambda item: item.name):
        resolved = path.resolve()
        if resolved in excluded:
            continue
        rows.append(_path_summary(path))
    return _fingerprint(rows)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _plan_fingerprint(plan: dict[str, Any]) -> str:
    """Hash exact identities while excluding volatile free-space observations."""

    material = {
        key: plan.get(key)
        for key in (
            "contract_name",
            "root",
            "root_path",
            "family",
            "family_path",
            "policy",
            "action",
            "reason",
            "corrupt",
            "target_identities",
            "retained",
            "non_target_digest",
            "foreign_files",
        )
        if key in plan
    }
    archive_plan = plan.get("archive_plan")
    if isinstance(archive_plan, dict):
        material["archive_plan_fingerprint"] = archive_plan.get("fingerprint")
        material["archive_source_identity"] = {
            key: archive_plan.get(key)
            for key in (
                "source_path",
                "archive_path",
                "source_size_bytes",
                "source_sha256",
                "source_inode",
                "source_mtime_ns",
                "directory_non_target_digest",
            )
        }
    return _fingerprint(material)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    runtime_dir = Path(args.runtime_dir)
    root_backups = Path(args.root_backups)
    if args.command == "inventory":
        return inventory(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
        )
    if args.command == "plan":
        return plan_family(
            runtime_dir=runtime_dir,
            root_backups=root_backups,
            root_name=args.root,
            family=args.family,
            reserved_free_bytes=args.reserved_free_bytes,
        )
    return apply_family(
        runtime_dir=runtime_dir,
        root_backups=root_backups,
        root_name=args.root,
        family=args.family,
        fingerprint=args.fingerprint,
        deployed_sha=args.deployed_sha,
        deployed_sha_file=Path(args.deployed_sha_file),
        reserved_free_bytes=args.reserved_free_bytes,
    )


def main() -> int:
    try:
        payload = run(build_parser().parse_args())
    except Exception as exc:
        print(
            json.dumps(
                {
                    "contract_name": CONTRACT_NAME,
                    "status": "failed",
                    "error": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
