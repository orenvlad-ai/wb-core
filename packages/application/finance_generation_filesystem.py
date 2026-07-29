"""Fail-closed identity contract for the Finance generation filesystem."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any, Mapping


CONTRACT_VERSION = "wb_core_finance_generation_filesystem_v1"
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}"
)
_MOUNT_ESCAPE_RE = re.compile(r"\\([0-7]{3})")


class FinanceGenerationFilesystemError(ValueError):
    """The configured generation mount is missing, drifted, or ambiguous."""


def validate_generation_filesystem_contract(
    payload: Mapping[str, Any] | None,
    *,
    runtime_dir: Path,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise FinanceGenerationFilesystemError(
            "Finance generation filesystem contract is required"
        )
    contract = dict(payload)
    expected_path = (Path(runtime_dir).resolve() / "generations").resolve()
    path = Path(str(contract.get("path") or "")).expanduser().resolve()
    filesystem_uuid = str(contract.get("filesystem_uuid") or "").strip().lower()
    filesystem_label = str(contract.get("filesystem_label") or "").strip()
    filesystem_type = str(
        contract.get("filesystem_type") or ""
    ).strip().lower()
    required_options = sorted(
        {
            str(item).strip().lower()
            for item in contract.get("required_mount_options") or []
            if str(item).strip()
        }
    )
    if (
        str(contract.get("contract_version") or "") != CONTRACT_VERSION
        or path != expected_path
        or _UUID_RE.fullmatch(filesystem_uuid) is None
        or not filesystem_label
        or filesystem_type != "ext4"
        or not {"rw", "noatime", "nodev", "nosuid", "noexec"}.issubset(
            required_options
        )
        or contract.get("require_distinct_device") is not True
    ):
        raise FinanceGenerationFilesystemError(
            "Finance generation filesystem contract is invalid"
        )
    return {
        "contract_version": CONTRACT_VERSION,
        "path": str(path),
        "filesystem_uuid": filesystem_uuid,
        "filesystem_label": filesystem_label,
        "filesystem_type": filesystem_type,
        "required_mount_options": required_options,
        "require_distinct_device": True,
    }


def _unescape_mountinfo(value: str) -> str:
    return _MOUNT_ESCAPE_RE.sub(
        lambda match: chr(int(match.group(1), 8)),
        value,
    )


def _mountinfo_entry(
    path: Path,
    *,
    mountinfo_path: Path,
) -> dict[str, Any]:
    expected = str(path.resolve())
    matches: list[dict[str, Any]] = []
    try:
        lines = mountinfo_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise FinanceGenerationFilesystemError(
            "mountinfo is unavailable for Finance generation filesystem"
        ) from exc
    for line in lines:
        fields = line.split()
        if "-" not in fields or len(fields) < 10:
            continue
        separator = fields.index("-")
        if separator < 6 or len(fields) <= separator + 3:
            continue
        mount_point = _unescape_mountinfo(fields[4])
        if mount_point != expected:
            continue
        matches.append(
            {
                "major_minor": fields[2],
                "root": _unescape_mountinfo(fields[3]),
                "mount_point": mount_point,
                "mount_options": sorted(
                    {
                        item.strip().lower()
                        for item in fields[5].split(",")
                        if item.strip()
                    }
                ),
                "filesystem_type": fields[separator + 1].lower(),
                "source": _unescape_mountinfo(fields[separator + 2]),
                "super_options": sorted(
                    {
                        item.strip().lower()
                        for item in fields[separator + 3].split(",")
                        if item.strip()
                    }
                ),
            }
        )
    if len(matches) != 1:
        raise FinanceGenerationFilesystemError(
            "Finance generation filesystem mount identity is ambiguous"
        )
    return matches[0]


def _same_block_device(left: Path, right: Path) -> bool:
    try:
        return int(left.stat().st_rdev) == int(right.stat().st_rdev)
    except OSError:
        return False


def inspect_generation_filesystem(
    runtime_dir: Path,
    contract_payload: Mapping[str, Any] | None,
    *,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
    by_uuid_root: Path = Path("/dev/disk/by-uuid"),
    by_label_root: Path = Path("/dev/disk/by-label"),
) -> dict[str, Any]:
    runtime = Path(runtime_dir).expanduser().resolve()
    contract = validate_generation_filesystem_contract(
        contract_payload,
        runtime_dir=runtime,
    )
    path = Path(contract["path"])
    if (
        not path.exists()
        or not path.is_dir()
        or path.is_symlink()
        or not os.path.ismount(path)
    ):
        raise FinanceGenerationFilesystemError(
            "Finance generation filesystem is not an exact mount point"
        )
    runtime_stat = runtime.stat()
    mount_stat = path.stat()
    if int(runtime_stat.st_dev) == int(mount_stat.st_dev):
        raise FinanceGenerationFilesystemError(
            "Finance generation filesystem is not on a distinct device"
        )
    mount = _mountinfo_entry(
        path,
        mountinfo_path=mountinfo_path,
    )
    if mount["filesystem_type"] != contract["filesystem_type"]:
        raise FinanceGenerationFilesystemError(
            "Finance generation filesystem type drifted"
        )
    effective_options = set(mount["mount_options"]) | set(
        mount["super_options"]
    )
    missing_options = sorted(
        set(contract["required_mount_options"]) - effective_options
    )
    if missing_options:
        raise FinanceGenerationFilesystemError(
            "Finance generation filesystem mount options drifted: "
            + ",".join(missing_options)
        )
    source = Path(str(mount["source"] or ""))
    uuid_link = by_uuid_root / contract["filesystem_uuid"]
    label_link = by_label_root / contract["filesystem_label"]
    if (
        not source.is_block_device()
        or not uuid_link.exists()
        or not label_link.exists()
        or not _same_block_device(source, uuid_link)
        or not _same_block_device(source, label_link)
    ):
        raise FinanceGenerationFilesystemError(
            "Finance generation filesystem UUID/label/source identity drifted"
        )
    vfs = os.statvfs(path)
    return {
        **contract,
        "status": "ready",
        "source": str(source.resolve()),
        "device": int(mount_stat.st_dev),
        "major_minor": str(mount["major_minor"]),
        "mount_root": str(mount["root"]),
        "mount_options": sorted(effective_options),
        "filesystem_block_size": int(vfs.f_frsize),
        "total_bytes": int(vfs.f_blocks * vfs.f_frsize),
        "available_bytes": int(vfs.f_bavail * vfs.f_frsize),
        "runtime_device": int(runtime_stat.st_dev),
        "distinct_device": True,
        "mountpoint_proven": True,
    }


def stable_generation_filesystem_identity(
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    stable = json.loads(
        json.dumps(
            dict(identity),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    stable.pop("available_bytes", None)
    return stable
