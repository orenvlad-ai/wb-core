#!/usr/bin/env python3
"""One-shot bootstrap and fail-closed readback for the WBC0035 recovery scratch disk."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, BinaryIO, Mapping


CONTRACT_VERSION = "wb_core_recovery_scratch_filesystem_v1"
PLAN_CONTRACT = "wb_core_recovery_scratch_bootstrap_plan_v1"
RESULT_CONTRACT = "wb_core_recovery_scratch_bootstrap_result_v1"
GIB = 1024**3
LINUX_FILESYSTEM_GUID = "0fc63daf-8483-4772-8e79-3d69d8477de4"
UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
OPERATION_PATTERN = re.compile(r"wbc0035-025-recovery-scratch-[a-z0-9-]{1,48}")
REQUIRED_MOUNT_OPTIONS = frozenset({"rw", "noatime", "nodev", "nosuid", "noexec"})
ROLE_PATHS = {
    "root": Path("/"),
    "backup": Path("/opt/wb-core-runtime/state/backups"),
    "generation": Path("/opt/wb-core-runtime/state/generations"),
}


class RecoveryScratchError(RuntimeError):
    """The exact recovery-scratch identity or one-shot state failed closed."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def plan_fingerprint(plan: Mapping[str, Any]) -> str:
    material = json.loads(_canonical_json(dict(plan)))
    material.pop("created_at", None)
    material.pop("fingerprint", None)
    return _digest(material)


def validate_recovery_scratch_contract(
    payload: Mapping[str, Any] | None,
    *,
    runtime_dir: Path,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise RecoveryScratchError("recovery scratch contract is required")
    raw = dict(payload)
    expected_keys = {
        "contract_version", "path", "parent_device_by_id", "partition_device_by_id",
        "parent_serial", "parent_model", "parent_size_bytes", "parent_major_minor",
        "parent_hctl",
        "partition_table", "partition_number", "disk_guid", "partition_guid",
        "filesystem_uuid", "filesystem_label", "filesystem_type",
        "required_mount_options", "reserve_bytes", "completion_marker",
        "require_distinct_from_roles",
    }
    path = Path(str(raw.get("path") or "")).resolve()
    expected_path = (Path(runtime_dir).resolve() / "recovery-scratch").resolve()
    parent_by_id = str(raw.get("parent_device_by_id") or "")
    partition_by_id = str(raw.get("partition_device_by_id") or "")
    options = sorted({str(item).strip().lower() for item in raw.get("required_mount_options") or []})
    marker = Path(str(raw.get("completion_marker") or ""))
    uuids = [
        str(raw.get("disk_guid") or "").lower(),
        str(raw.get("partition_guid") or "").lower(),
        str(raw.get("filesystem_uuid") or "").lower(),
    ]
    if (
        set(raw) != expected_keys
        or raw.get("contract_version") != CONTRACT_VERSION
        or path != expected_path
        or not parent_by_id.startswith("/dev/disk/by-id/")
        or partition_by_id != parent_by_id + "-part1"
        or str(raw.get("parent_serial") or "") != "vde"
        or str(raw.get("parent_model") or "") != "QEMU HARDDISK"
        or int(raw.get("parent_size_bytes") or 0) != 53_687_091_200
        or str(raw.get("parent_major_minor") or "") != "8:48"
        or str(raw.get("parent_hctl") or "") != "0:0:0:4"
        or raw.get("partition_table") != "gpt"
        or int(raw.get("partition_number") or 0) != 1
        or any(UUID_PATTERN.fullmatch(item) is None for item in uuids)
        or len(set(uuids)) != 3
        or not str(raw.get("filesystem_label") or "")
        or str(raw.get("filesystem_type") or "").lower() != "ext4"
        or not REQUIRED_MOUNT_OPTIONS.issubset(options)
        or int(raw.get("reserve_bytes") or 0) != 8 * GIB
        or not marker.is_absolute()
        or marker != Path("/opt/wb-core-runtime/state/backups/private-evidence/recovery-scratch-bootstrap/completed.json")
        or raw.get("require_distinct_from_roles") != ["root", "backup", "generation"]
    ):
        raise RecoveryScratchError("recovery scratch contract is invalid")
    return {
        **raw,
        "path": str(path),
        "disk_guid": uuids[0],
        "partition_guid": uuids[1],
        "filesystem_uuid": uuids[2],
        "filesystem_type": "ext4",
        "required_mount_options": options,
        "reserve_bytes": 8 * GIB,
    }


def validate_blank_device_evidence(
    contract: Mapping[str, Any], evidence: Mapping[str, Any]
) -> None:
    expected = {
        "parent_device_by_id": contract["parent_device_by_id"],
        "parent_major_minor": contract["parent_major_minor"],
        "parent_size_bytes": contract["parent_size_bytes"],
        "parent_serial": contract["parent_serial"],
        "parent_model": contract["parent_model"],
        "parent_hctl": contract["parent_hctl"],
        "parent_type": "disk",
        "read_only": False,
        "removable": False,
    }
    mismatches = {
        key: {"expected": value, "observed": evidence.get(key)}
        for key, value in expected.items()
        if evidence.get(key) != value
    }
    nonempty = {
        key: list(evidence.get(key) or [])
        for key in (
            "children", "signatures", "mounts", "holders", "slaves",
            "lvm_memberships", "md_memberships", "swap_memberships", "openers",
            "config_references",
        )
        if list(evidence.get(key) or [])
    }
    resolved = str(evidence.get("resolved_parent_device") or "")
    if (
        evidence.get("status") != "blank_ready"
        or not resolved.startswith("/dev/")
        or resolved.startswith("/dev/disk/by-id/")
        or mismatches
        or nonempty
        or evidence.get("partition_table") not in {None, ""}
    ):
        raise RecoveryScratchError(
            f"recovery scratch disk is not exact blank target: mismatches={mismatches}, used={sorted(nonempty)}"
        )


def validate_ready_evidence(
    contract: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    require_completion_marker: bool = True,
) -> None:
    exact = {
        "status": "ready",
        "path": contract["path"],
        "parent_device_by_id": contract["parent_device_by_id"],
        "partition_device_by_id": contract["partition_device_by_id"],
        "parent_major_minor": contract["parent_major_minor"],
        "parent_size_bytes": contract["parent_size_bytes"],
        "parent_serial": contract["parent_serial"],
        "parent_model": contract["parent_model"],
        "parent_hctl": contract["parent_hctl"],
        "partition_table": contract["partition_table"],
        "disk_guid": contract["disk_guid"],
        "partition_number": contract["partition_number"],
        "partition_guid": contract["partition_guid"],
        "filesystem_uuid": contract["filesystem_uuid"],
        "filesystem_label": contract["filesystem_label"],
        "filesystem_type": contract["filesystem_type"],
        "mountpoint_proven": True,
        "mount_root": "/",
        "directory_mode": 0o700,
        "fstab_exact": True,
    }
    mismatches = {
        key: {"expected": value, "observed": evidence.get(key)}
        for key, value in exact.items()
        if evidence.get(key) != value
    }
    options = {str(item).lower() for item in evidence.get("mount_options") or []}
    missing_options = sorted(set(contract["required_mount_options"]) - options)
    distinct = dict(evidence.get("distinct_from_roles") or {})
    bad_roles = sorted(
        role for role in contract["require_distinct_from_roles"] if distinct.get(role) is not True
    )
    if (
        mismatches
        or missing_options
        or "ro" in options
        or bad_roles
        or list(evidence.get("entries") or [])
        or int(evidence.get("available_bytes") or -1) < int(contract["reserve_bytes"])
        or (require_completion_marker and evidence.get("completion_marker_present") is not True)
    ):
        raise RecoveryScratchError(
            "recovery scratch mounted-ready identity failed: "
            f"mismatches={mismatches}, missing_options={missing_options}, bad_roles={bad_roles}"
        )


def acquire_allocation_lock(handle: BinaryIO) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise RecoveryScratchError("recovery scratch allocation is already active") from exc


def _run(
    argv: list[str],
    *,
    input_text: str | None = None,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        argv,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode not in allowed_returncodes:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise RecoveryScratchError(f"command failed closed: {shlex.join(argv)}: {detail[:1000]}")
    return completed


def _lsblk(device: str) -> dict[str, Any]:
    columns = (
        "NAME,KNAME,PATH,MAJ:MIN,SIZE,TYPE,FSTYPE,LABEL,UUID,MOUNTPOINTS,"
        "MODEL,SERIAL,HCTL,RO,RM,HOTPLUG,PKNAME,PARTUUID"
    )
    payload = json.loads(
        _run(["lsblk", "-b", "-J", "-p", "-o", columns, device]).stdout
    )
    rows = payload.get("blockdevices") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise RecoveryScratchError("recovery scratch lsblk identity is ambiguous")
    return dict(rows[0])


def _sysfs_names(kname: str, relation: str) -> list[str]:
    root = Path("/sys/class/block") / kname / relation
    if not root.is_dir():
        return []
    return sorted(item.name for item in root.iterdir())


def _config_references(contract: Mapping[str, Any], resolved: str) -> list[str]:
    candidates = [Path("/etc/fstab"), Path("/etc/crypttab"), Path("/etc/mdadm/mdadm.conf")]
    candidates.extend(sorted(Path("/etc/systemd/system").glob("*.mount")))
    needles = {
        str(contract["parent_device_by_id"]),
        str(contract["partition_device_by_id"]),
        str(contract["filesystem_uuid"]),
        str(contract["path"]),
        resolved,
        resolved + "1",
    }
    references: list[str] = []
    for path in candidates:
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                material = line.split("#", 1)[0]
                if any(needle and needle in material for needle in needles):
                    references.append(f"{path}:{number}")
        except OSError:
            continue
    return references


def collect_blank_device_evidence(contract: Mapping[str, Any]) -> dict[str, Any]:
    by_id = Path(str(contract["parent_device_by_id"]))
    if not by_id.is_symlink():
        raise RecoveryScratchError("recovery scratch parent by-id link is unavailable")
    resolved_path = by_id.resolve(strict=True)
    resolved_stat = resolved_path.stat()
    if not stat.S_ISBLK(resolved_stat.st_mode):
        raise RecoveryScratchError("recovery scratch parent by-id target is not block device")
    row = _lsblk(str(by_id))
    kname = str(row.get("kname") or "").removeprefix("/dev/")
    children = list(row.get("children") or [])
    mounts: list[str] = []
    for item in [row, *children]:
        mounts.extend(str(value) for value in item.get("mountpoints") or [] if value)
    signatures = [
        line.strip()
        for line in _run(
            ["wipefs", "--noheadings", "--output", "TYPE,UUID,LABEL,OFFSET", str(by_id)]
        ).stdout.splitlines()
        if line.strip()
    ]
    partition_table: str | None = None
    table = _run(["sfdisk", "--json", str(by_id)], allowed_returncodes=(0, 1))
    if table.returncode == 0:
        try:
            partition_table = str(json.loads(table.stdout).get("partitiontable", {}).get("label") or "")
        except json.JSONDecodeError:
            partition_table = "unknown"
    fuser = _run(["fuser", "-v", str(by_id)], allowed_returncodes=(0, 1))
    openers = [] if fuser.returncode == 1 else [line for line in (fuser.stdout + fuser.stderr).splitlines() if line.strip()]
    pvs = _run(
        ["pvs", "--noheadings", "--separator", "|", "-o", "pv_name"],
        allowed_returncodes=(0, 5),
    ) if Path("/usr/sbin/pvs").exists() or Path("/sbin/pvs").exists() else None
    lvm = [] if pvs is None else [line.strip() for line in pvs.stdout.splitlines() if str(resolved_path) in line]
    md_text = Path("/proc/mdstat").read_text(encoding="utf-8", errors="replace")
    swaps_text = Path("/proc/swaps").read_text(encoding="utf-8", errors="replace")
    evidence = {
        "status": "blank_ready",
        "parent_device_by_id": str(by_id),
        "resolved_parent_device": str(resolved_path),
        "parent_major_minor": str(row.get("maj:min") or ""),
        "parent_size_bytes": int(row.get("size") or 0),
        "parent_serial": str(row.get("serial") or ""),
        "parent_model": str(row.get("model") or "").strip(),
        "parent_type": str(row.get("type") or ""),
        "parent_hctl": str(row.get("hctl") or ""),
        "read_only": bool(row.get("ro")),
        "removable": bool(row.get("rm")),
        "children": children,
        "signatures": signatures,
        "partition_table": partition_table,
        "mounts": sorted(set(mounts)),
        "holders": _sysfs_names(kname, "holders"),
        "slaves": _sysfs_names(kname, "slaves"),
        "lvm_memberships": lvm,
        "md_memberships": [line for line in md_text.splitlines() if re.search(rf"\b{re.escape(kname)}\b", line)],
        "swap_memberships": [line for line in swaps_text.splitlines() if str(resolved_path) in line],
        "openers": openers,
        "config_references": _config_references(contract, str(resolved_path)),
    }
    validate_blank_device_evidence(contract, evidence)
    return evidence


def _unescape_mount(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def _mount_entry(path: Path) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if "-" not in fields:
            continue
        separator = fields.index("-")
        if _unescape_mount(fields[4]) != str(path.resolve()):
            continue
        matches.append(
            {
                "major_minor": fields[2],
                "root": _unescape_mount(fields[3]),
                "mount_options": fields[5].split(","),
                "filesystem_type": fields[separator + 1],
                "source": _unescape_mount(fields[separator + 2]),
                "super_options": fields[separator + 3].split(","),
            }
        )
    if len(matches) != 1:
        raise RecoveryScratchError("recovery scratch mount identity is ambiguous")
    return matches[0]


def _blkid_export(device: Path) -> dict[str, str]:
    result = _run(["blkid", "-o", "export", str(device)])
    payload: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            payload[key] = value
    return payload


def _fstab_line(contract: Mapping[str, Any]) -> str:
    options = ",".join(str(item) for item in contract["required_mount_options"])
    return (
        f"UUID={contract['filesystem_uuid']} {contract['path']} "
        f"{contract['filesystem_type']} {options} 0 2"
    )


def _fstab_exact(contract: Mapping[str, Any], fstab_path: Path = Path("/etc/fstab")) -> bool:
    expected = _fstab_line(contract)
    try:
        lines = [line.strip() for line in fstab_path.read_text(encoding="utf-8").splitlines()]
    except OSError:
        return False
    active = [line.split("#", 1)[0].strip() for line in lines if line.split("#", 1)[0].strip()]
    relevant = [
        line for line in active
        if str(contract["path"]) in line or str(contract["filesystem_uuid"]) in line
    ]
    return relevant == [expected]


def collect_ready_evidence(
    contract: Mapping[str, Any],
    *,
    role_paths: Mapping[str, Path] = ROLE_PATHS,
    require_completion_marker: bool = True,
) -> dict[str, Any]:
    parent = Path(str(contract["parent_device_by_id"]))
    partition = Path(str(contract["partition_device_by_id"]))
    if not parent.is_symlink() or not partition.is_symlink():
        raise RecoveryScratchError("recovery scratch stable by-id identity is unavailable")
    parent_resolved = parent.resolve(strict=True)
    partition_resolved = partition.resolve(strict=True)
    parent_row = _lsblk(str(parent))
    partition_row = _lsblk(str(partition))
    if str(partition_row.get("pkname") or "").removeprefix("/dev/") != str(parent_row.get("kname") or "").removeprefix("/dev/"):
        raise RecoveryScratchError("recovery scratch partition parent drifted")
    path = Path(str(contract["path"]))
    if path.is_symlink() or not path.is_dir() or not os.path.ismount(path):
        raise RecoveryScratchError("recovery scratch is missing exact mountpoint")
    mount = _mount_entry(path)
    parent_blkid = _blkid_export(parent)
    blkid = _blkid_export(partition)
    path_stat = path.stat()
    source_stat = Path(str(mount["source"])).stat()
    if not stat.S_ISBLK(source_stat.st_mode) or source_stat.st_rdev != partition_resolved.stat().st_rdev:
        raise RecoveryScratchError("recovery scratch mounted source differs from stable partition")
    role_devices = {role: Path(role_path).resolve().stat().st_dev for role, role_path in role_paths.items()}
    vfs = os.statvfs(path)
    evidence = {
        "status": "ready",
        "path": str(path),
        "parent_device_by_id": str(parent),
        "partition_device_by_id": str(partition),
        "resolved_parent_device": str(parent_resolved),
        "resolved_partition_device": str(partition_resolved),
        "parent_major_minor": str(parent_row.get("maj:min") or ""),
        "parent_size_bytes": int(parent_row.get("size") or 0),
        "parent_serial": str(parent_row.get("serial") or ""),
        "parent_model": str(parent_row.get("model") or "").strip(),
        "parent_hctl": str(parent_row.get("hctl") or ""),
        "partition_table": str(parent_blkid.get("PTTYPE") or "").lower(),
        "disk_guid": str(parent_blkid.get("PTUUID") or "").lower(),
        "partition_number": int(contract["partition_number"]),
        "partition_guid": str(blkid.get("PARTUUID") or "").lower(),
        "filesystem_uuid": str(blkid.get("UUID") or "").lower(),
        "filesystem_label": str(blkid.get("LABEL") or ""),
        "filesystem_type": str(mount.get("filesystem_type") or "").lower(),
        "mount_options": sorted(set(mount["mount_options"]) | set(mount["super_options"])),
        "mountpoint_proven": True,
        "mount_root": str(mount.get("root") or ""),
        "directory_mode": stat.S_IMODE(path_stat.st_mode),
        "distinct_from_roles": {role: path_stat.st_dev != device for role, device in role_devices.items()},
        "available_bytes": int(vfs.f_bavail * vfs.f_frsize),
        "total_bytes": int(vfs.f_blocks * vfs.f_frsize),
        "entries": sorted(item.name for item in path.iterdir()),
        "fstab_exact": _fstab_exact(contract),
        "completion_marker_present": Path(str(contract["completion_marker"])).is_file(),
    }
    validate_ready_evidence(
        contract,
        evidence,
        require_completion_marker=require_completion_marker,
    )
    return evidence


def _verify_deployed_sha(deployed_sha: str, deployed_sha_file: Path) -> str:
    normalized = str(deployed_sha or "").strip().lower()
    if SHA_PATTERN.fullmatch(normalized) is None:
        raise RecoveryScratchError("recovery scratch requires exact deployed SHA")
    try:
        observed = deployed_sha_file.read_text(encoding="utf-8").strip().lower()
    except OSError as exc:
        raise RecoveryScratchError("deployed SHA readback is unavailable") from exc
    if observed != normalized:
        raise RecoveryScratchError("deployed SHA drifted")
    return normalized


def _fstab_identity(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RecoveryScratchError("fstab identity is unavailable")
    stat_result = path.stat()
    return {
        "path": str(path),
        "sha256": _file_digest(path),
        "size_bytes": int(stat_result.st_size),
        "mode": stat.S_IMODE(stat_result.st_mode),
        "uid": int(stat_result.st_uid),
        "gid": int(stat_result.st_gid),
    }


def build_plan(
    *,
    contract_payload: Mapping[str, Any],
    runtime_dir: Path,
    deployed_sha: str,
    deployed_sha_file: Path,
    operation_id: str,
    approval_reference: str,
    fstab_path: Path = Path("/etc/fstab"),
) -> dict[str, Any]:
    contract = validate_recovery_scratch_contract(contract_payload, runtime_dir=runtime_dir)
    deployed = _verify_deployed_sha(deployed_sha, deployed_sha_file)
    operation = str(operation_id or "").strip().lower()
    approval = str(approval_reference or "").strip()
    if OPERATION_PATTERN.fullmatch(operation) is None or not approval or len(approval) > 500:
        raise RecoveryScratchError("recovery scratch operation/approval binding is invalid")
    blank = collect_blank_device_evidence(contract)
    fstab = _fstab_identity(fstab_path)
    material = {
        "contract_name": PLAN_CONTRACT,
        "status": "ready_to_initialize",
        "operation_id": operation,
        "deployed_sha": deployed,
        "approval_reference": approval,
        "target": contract,
        "blank_device": blank,
        "fstab_before": fstab,
        "layout": {
            "partition_table": "gpt",
            "partition_count": 1,
            "disk_guid": contract["disk_guid"],
            "partition_guid": contract["partition_guid"],
            "partition_type_guid": LINUX_FILESYSTEM_GUID,
            "filesystem_type": "ext4",
            "filesystem_uuid": contract["filesystem_uuid"],
            "filesystem_label": contract["filesystem_label"],
            "fstab_line": _fstab_line(contract),
        },
        "expected_effect": {
            "disk_initialized": True,
            "mount_persisted": True,
            "business_database_mutation": 0,
            "recovery_submit": 0,
            "barrier_change": False,
            "timer_change": False,
        },
        "submit_count": 0,
        "created_at": _now(),
    }
    material["fingerprint"] = plan_fingerprint(material)
    return material


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, payload: Mapping[str, Any], *, overwrite: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(_canonical_json(dict(payload)) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists() and not overwrite:
            raise RecoveryScratchError(f"immutable artifact already exists: {path}")
        os.replace(temp, path)
        _fsync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def write_plan(path: Path, plan: Mapping[str, Any]) -> None:
    expected_parent = Path(str(plan["target"]["completion_marker"])).parent
    if path.resolve().parent != expected_parent.resolve() or path.name != f"plan-{plan['deployed_sha']}.json":
        raise RecoveryScratchError("recovery scratch plan path is outside exact durable scope")
    _atomic_json(path, plan)


def _write_bytes_exclusive(path: Path, payload: bytes, *, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _partition_table_input(contract: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            "label: gpt",
            f"label-id: {str(contract['disk_guid']).upper()}",
            "unit: sectors",
            "first-lba: 2048",
            "",
            (
                f"start=2048, type={LINUX_FILESYSTEM_GUID}, "
                f"uuid={str(contract['partition_guid']).upper()}, name=wb-recovery-scratch"
            ),
            "",
        ]
    )


def _replace_fstab(path: Path, contract: Mapping[str, Any], before: bytes) -> None:
    current = path.read_bytes()
    if current != before:
        raise RecoveryScratchError("fstab drifted before persistent mount publication")
    line = _fstab_line(contract)
    normalized = current.rstrip(b"\n") + b"\n" + line.encode("utf-8") + b"\n"
    stat_result = path.stat()
    descriptor, raw = tempfile.mkstemp(prefix=".fstab.recovery-scratch.", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), stat.S_IMODE(stat_result.st_mode))
            os.fchown(handle.fileno(), stat_result.st_uid, stat_result.st_gid)
            handle.write(normalized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        _fsync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _restore_fstab(path: Path, before: bytes, identity: Mapping[str, Any]) -> None:
    descriptor, raw = tempfile.mkstemp(prefix=".fstab.recovery-scratch-rollback.", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), int(identity["mode"]))
            os.fchown(handle.fileno(), int(identity["uid"]), int(identity["gid"]))
            handle.write(before)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        _fsync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _wait_for_partition(path: Path, timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.is_symlink():
            try:
                if stat.S_ISBLK(path.resolve(strict=True).stat().st_mode):
                    return
            except OSError:
                pass
        time.sleep(0.25)
    raise RecoveryScratchError("recovery scratch partition by-id did not materialize")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryScratchError(f"immutable JSON is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise RecoveryScratchError(f"immutable JSON is not an object: {path}")
    return payload


def apply_plan(
    *,
    plan_path: Path,
    plan_sha256: str,
    fingerprint: str,
    deployed_sha_file: Path,
    fstab_path: Path = Path("/etc/fstab"),
) -> dict[str, Any]:
    if DIGEST_PATTERN.fullmatch(plan_sha256) is None or DIGEST_PATTERN.fullmatch(fingerprint) is None:
        raise RecoveryScratchError("reviewed recovery scratch plan identity is invalid")
    plan_path = plan_path.resolve()
    plan = _read_json(plan_path)
    if _file_digest(plan_path) != plan_sha256:
        raise RecoveryScratchError("reviewed recovery scratch plan bytes drifted")
    if (
        plan.get("contract_name") != PLAN_CONTRACT
        or plan.get("status") != "ready_to_initialize"
        or plan.get("fingerprint") != fingerprint
        or plan_fingerprint(plan) != fingerprint
        or int(plan.get("submit_count") or -1) != 0
    ):
        raise RecoveryScratchError("reviewed recovery scratch plan contract drifted")
    runtime_dir = Path(str(plan["target"]["path"])).parent
    contract = validate_recovery_scratch_contract(plan.get("target"), runtime_dir=runtime_dir)
    _verify_deployed_sha(str(plan["deployed_sha"]), deployed_sha_file)
    state_dir = Path(str(contract["completion_marker"])).parent
    if plan_path.parent != state_dir.resolve():
        raise RecoveryScratchError("reviewed plan is outside recovery scratch durable state")
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_dir, 0o700)
    completion = Path(str(contract["completion_marker"]))
    intent_path = state_dir / "intent.json"
    failure_path = state_dir / "failure.json"
    if completion.exists():
        result = _read_json(completion)
        if result.get("plan_sha256") != plan_sha256 or result.get("plan_fingerprint") != fingerprint:
            raise RecoveryScratchError("recovery scratch completion belongs to another plan")
        return {**result, "already_terminal": True, "apply_submit_count_this_call": 0}
    if intent_path.exists() or failure_path.exists():
        raise RecoveryScratchError("recovery scratch prior submit is ambiguous or failed; retry forbidden")

    lock_path = state_dir / "bootstrap.lock"
    lock = lock_path.open("a+b")
    os.chmod(lock_path, 0o600)
    acquire_allocation_lock(lock)
    try:
        before_bytes = fstab_path.read_bytes()
        if _fstab_identity(fstab_path) != dict(plan["fstab_before"]):
            raise RecoveryScratchError("fstab drifted since recovery scratch dry-run")
        fresh_blank = collect_blank_device_evidence(contract)
        if fresh_blank != plan["blank_device"]:
            raise RecoveryScratchError("recovery scratch blank device evidence drifted since dry-run")
        backup_path = state_dir / "fstab.before"
        _write_bytes_exclusive(backup_path, before_bytes, mode=0o600)
    except Exception:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
        raise
    intent = {
        "contract_name": RESULT_CONTRACT,
        "status": "submitted",
        "operation_id": plan["operation_id"],
        "deployed_sha": plan["deployed_sha"],
        "plan_sha256": plan_sha256,
        "plan_fingerprint": fingerprint,
        "approval_reference": plan["approval_reference"],
        "submit_count": 1,
        "submitted_at": _now(),
    }
    _atomic_json(intent_path, intent)
    mount_published = False
    mountpoint_created = False
    try:
        _run(
            [
                "sfdisk", "--lock=yes", "--wipe=never", "--wipe-partitions=never",
                str(contract["parent_device_by_id"]),
            ],
            input_text=_partition_table_input(contract),
        )
        _run(["udevadm", "settle"])
        _run(["partprobe", str(contract["parent_device_by_id"])])
        _run(["udevadm", "settle"])
        partition = Path(str(contract["partition_device_by_id"]))
        _wait_for_partition(partition)
        partition_row = _lsblk(str(partition))
        if (
            str(partition_row.get("type") or "") != "part"
            or str(partition_row.get("partuuid") or "").lower() != contract["partition_guid"]
            or partition_row.get("fstype") not in {None, ""}
        ):
            raise RecoveryScratchError("new recovery scratch partition identity drifted")
        _run(
            [
                "mkfs.ext4", "-F", "-U", str(contract["filesystem_uuid"]),
                "-L", str(contract["filesystem_label"]),
                "-E", "lazy_itable_init=0,lazy_journal_init=0", str(partition),
            ]
        )
        path = Path(str(contract["path"]))
        if path.exists():
            if path.is_symlink() or not path.is_dir() or any(path.iterdir()) or os.path.ismount(path):
                raise RecoveryScratchError("recovery scratch mountpoint is used or ambiguous")
        else:
            path.mkdir(mode=0o700)
            mountpoint_created = True
        os.chmod(path, 0o700)
        _replace_fstab(fstab_path, contract, before_bytes)
        mount_published = True
        _run(["mount", str(path)])
        os.chmod(path, 0o700)
        lost_found = path / "lost+found"
        if lost_found.is_dir() and not lost_found.is_symlink():
            lost_found.rmdir()
        ready = collect_ready_evidence(contract, require_completion_marker=False)
        result = {
            "contract_name": RESULT_CONTRACT,
            "status": "READY",
            "operation_id": plan["operation_id"],
            "deployed_sha": plan["deployed_sha"],
            "plan_sha256": plan_sha256,
            "plan_fingerprint": fingerprint,
            "approval_reference": plan["approval_reference"],
            "submit_count": 1,
            "business_database_mutation": 0,
            "recovery_submit": 0,
            "ready": ready,
            "completed_at": _now(),
        }
        result["result_fingerprint"] = _digest(result)
        _atomic_json(completion, result)
        final_ready = collect_ready_evidence(contract)
        return {**result, "ready": final_ready, "apply_submit_count_this_call": 1}
    except Exception as exc:
        rollback: dict[str, Any] = {"fstab_restored": False, "mount_unmounted": False}
        path = Path(str(contract["path"]))
        try:
            if path.exists() and os.path.ismount(path):
                _run(["umount", str(path)])
                rollback["mount_unmounted"] = True
        except Exception as rollback_exc:
            rollback["umount_error"] = str(rollback_exc)
        try:
            if mount_published:
                _restore_fstab(fstab_path, before_bytes, plan["fstab_before"])
                rollback["fstab_restored"] = True
        except Exception as rollback_exc:
            rollback["fstab_restore_error"] = str(rollback_exc)
        try:
            if mountpoint_created and path.is_dir() and not any(path.iterdir()) and not os.path.ismount(path):
                path.rmdir()
        except OSError:
            pass
        failure = {
            **intent,
            "status": "failed_after_submit",
            "failed_at": _now(),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "rollback": rollback,
            "retry_allowed": False,
        }
        _atomic_json(failure_path, failure)
        raise RecoveryScratchError(
            f"recovery scratch initialization failed after one submit: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            lock.close()


def readback(
    *,
    contract_payload: Mapping[str, Any],
    runtime_dir: Path,
    deployed_sha: str,
    deployed_sha_file: Path,
) -> dict[str, Any]:
    contract = validate_recovery_scratch_contract(contract_payload, runtime_dir=runtime_dir)
    deployed = _verify_deployed_sha(deployed_sha, deployed_sha_file)
    completion = Path(str(contract["completion_marker"]))
    result = _read_json(completion)
    if (
        result.get("contract_name") != RESULT_CONTRACT
        or result.get("status") != "READY"
        or result.get("deployed_sha") != deployed
        or int(result.get("submit_count") or 0) != 1
        or int(result.get("business_database_mutation") or -1) != 0
        or int(result.get("recovery_submit") or -1) != 0
    ):
        raise RecoveryScratchError("recovery scratch completion marker drifted")
    material = dict(result)
    observed = str(material.pop("result_fingerprint", ""))
    if observed != _digest(material):
        raise RecoveryScratchError("recovery scratch completion digest drifted")
    ready = collect_ready_evidence(contract)
    return {
        "contract_name": RESULT_CONTRACT,
        "status": "READY",
        "deployed_sha": deployed,
        "operation_id": result["operation_id"],
        "submit_count": 1,
        "business_database_mutation": 0,
        "recovery_submit": 0,
        "ready": ready,
        "result_fingerprint": observed,
    }


def _target_contract(path: Path) -> tuple[dict[str, Any], Path]:
    payload = _read_json(path)
    contract = payload.get("recovery_scratch_filesystem")
    runtime_dir = Path(str(dict(payload.get("runtime_env") or {}).get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""))
    if not runtime_dir.is_absolute():
        raise RecoveryScratchError("target runtime directory is invalid")
    return dict(contract or {}), runtime_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-file", type=Path, required=True)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--deployed-sha-file", type=Path, required=True)
    children = parser.add_subparsers(dest="command", required=True)
    dry_run = children.add_parser("dry-run")
    dry_run.add_argument("--operation-id", required=True)
    dry_run.add_argument("--approval-reference", required=True)
    dry_run.add_argument("--output", type=Path, required=True)
    apply = children.add_parser("apply")
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--plan-sha256", required=True)
    apply.add_argument("--fingerprint", required=True)
    children.add_parser("readback")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        contract, runtime_dir = _target_contract(args.target_file)
        if args.command == "dry-run":
            payload = build_plan(
                contract_payload=contract,
                runtime_dir=runtime_dir,
                deployed_sha=args.deployed_sha,
                deployed_sha_file=args.deployed_sha_file,
                operation_id=args.operation_id,
                approval_reference=args.approval_reference,
            )
            write_plan(args.output, payload)
            payload = {
                **payload,
                "plan_path": str(args.output.resolve()),
                "plan_sha256": _file_digest(args.output.resolve()),
            }
        elif args.command == "apply":
            payload = apply_plan(
                plan_path=args.plan,
                plan_sha256=args.plan_sha256,
                fingerprint=args.fingerprint,
                deployed_sha_file=args.deployed_sha_file,
            )
        else:
            payload = readback(
                contract_payload=contract,
                runtime_dir=runtime_dir,
                deployed_sha=args.deployed_sha,
                deployed_sha_file=args.deployed_sha_file,
            )
        print(_canonical_json(payload))
        return 0
    except (RecoveryScratchError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(_canonical_json({"contract_name": RESULT_CONTRACT, "status": "error", "error_type": type(exc).__name__, "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
