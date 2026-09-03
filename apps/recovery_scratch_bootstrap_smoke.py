#!/usr/bin/env python3
"""Focused fail-closed contract checks for the recovery scratch disk."""

from __future__ import annotations

import copy
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.recovery_scratch_bootstrap import (
    RecoveryScratchError,
    plan_fingerprint,
    validate_blank_device_evidence,
    validate_ready_evidence,
    validate_recovery_scratch_contract,
)


CONTRACT = {
    "contract_version": "wb_core_recovery_scratch_filesystem_v1",
    "path": "/opt/wb-core-runtime/state/recovery-scratch",
    "parent_device_by_id": "/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_vde",
    "partition_device_by_id": "/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_vde-part1",
    "parent_serial": "vde",
    "parent_model": "QEMU HARDDISK",
    "parent_size_bytes": 53687091200,
    "parent_major_minor": "8:48",
    "parent_hctl": "0:0:0:4",
    "partition_table": "gpt",
    "partition_number": 1,
    "disk_guid": "b19fe03c-84c7-438c-91db-2e57bbf2a06e",
    "partition_guid": "9a0f40dd-bb7d-4af1-82bc-40a0960dee85",
    "filesystem_uuid": "da019107-575c-4fe7-b698-e021b3fc83c8",
    "filesystem_label": "wb-recovery-scratch",
    "filesystem_type": "ext4",
    "required_mount_options": ["rw", "noatime", "nodev", "nosuid", "noexec"],
    "reserve_bytes": 8589934592,
    "completion_marker": "/opt/wb-core-runtime/state/backups/private-evidence/recovery-scratch-bootstrap/completed.json",
    "require_distinct_from_roles": ["root", "backup", "generation"],
}


def _expect_error(callable_, contains: str) -> None:
    try:
        callable_()
    except RecoveryScratchError as exc:
        assert contains in str(exc), (contains, str(exc))
    else:
        raise AssertionError(f"expected RecoveryScratchError containing {contains!r}")


def _blank() -> dict:
    return {
        "status": "blank_ready",
        "parent_device_by_id": CONTRACT["parent_device_by_id"],
        "resolved_parent_device": "/dev/sdd",
        "parent_major_minor": CONTRACT["parent_major_minor"],
        "parent_size_bytes": CONTRACT["parent_size_bytes"],
        "parent_serial": CONTRACT["parent_serial"],
        "parent_model": CONTRACT["parent_model"],
        "parent_hctl": CONTRACT["parent_hctl"],
        "parent_type": "disk",
        "read_only": False,
        "removable": False,
        "children": [],
        "signatures": [],
        "mounts": [],
        "holders": [],
        "slaves": [],
        "lvm_memberships": [],
        "md_memberships": [],
        "swap_memberships": [],
        "openers": [],
        "config_references": [],
    }


def _ready() -> dict:
    return {
        "status": "ready",
        "path": CONTRACT["path"],
        "parent_device_by_id": CONTRACT["parent_device_by_id"],
        "partition_device_by_id": CONTRACT["partition_device_by_id"],
        "resolved_parent_device": "/dev/sdd",
        "resolved_partition_device": "/dev/sdd1",
        "parent_major_minor": CONTRACT["parent_major_minor"],
        "parent_size_bytes": CONTRACT["parent_size_bytes"],
        "parent_serial": CONTRACT["parent_serial"],
        "parent_model": CONTRACT["parent_model"],
        "parent_hctl": CONTRACT["parent_hctl"],
        "partition_table": CONTRACT["partition_table"],
        "disk_guid": CONTRACT["disk_guid"],
        "partition_number": 1,
        "partition_guid": CONTRACT["partition_guid"],
        "filesystem_uuid": CONTRACT["filesystem_uuid"],
        "filesystem_label": CONTRACT["filesystem_label"],
        "filesystem_type": "ext4",
        "mount_options": ["rw", "noatime", "nodev", "nosuid", "noexec", "relatime"],
        "mountpoint_proven": True,
        "mount_root": "/",
        "directory_mode": 0o700,
        "distinct_from_roles": {"root": True, "backup": True, "generation": True},
        "available_bytes": 45000000000,
        "entries": [],
        "fstab_exact": True,
        "completion_marker_present": True,
    }


def main() -> int:
    contract = validate_recovery_scratch_contract(
        CONTRACT,
        runtime_dir=Path("/opt/wb-core-runtime/state"),
    )
    assert contract["path"] == CONTRACT["path"]
    assert contract["reserve_bytes"] == 8 * 1024**3

    for field, value in {
        "parent_device_by_id": "/dev/sdd",
        "parent_serial": "vdc",
        "parent_size_bytes": 1,
        "parent_major_minor": "8:49",
        "parent_hctl": "0:0:0:3",
        "filesystem_uuid": "bad",
    }.items():
        changed = copy.deepcopy(CONTRACT)
        changed[field] = value
        _expect_error(
            lambda changed=changed: validate_recovery_scratch_contract(
                changed,
                runtime_dir=Path("/opt/wb-core-runtime/state"),
            ),
            "contract",
        )

    validate_blank_device_evidence(contract, _blank())
    for field, value in {
        "parent_serial": "vdc",
        "parent_size_bytes": 2,
        "parent_major_minor": "8:32",
        "children": [{"name": "sdd1"}],
        "signatures": [{"type": "ext4"}],
        "holders": ["dm-0"],
        "openers": [{"pid": 1}],
        "config_references": ["/etc/fstab:1"],
    }.items():
        evidence = _blank()
        evidence[field] = value
        _expect_error(
            lambda evidence=evidence: validate_blank_device_evidence(contract, evidence),
            "blank",
        )

    validate_ready_evidence(contract, _ready())
    for field, value in {
        "filesystem_uuid": "bd3d563f-e5ea-4e4a-a76a-be45e7f94ec0",
        "disk_guid": "bd3d563f-e5ea-4e4a-a76a-be45e7f94ec0",
        "filesystem_type": "xfs",
        "mountpoint_proven": False,
        "directory_mode": 0o755,
        "entries": ["qualification.sqlite3"],
        "fstab_exact": False,
    }.items():
        evidence = _ready()
        evidence[field] = value
        _expect_error(
            lambda evidence=evidence: validate_ready_evidence(contract, evidence),
            "ready",
        )
    evidence = _ready()
    evidence["mount_options"].remove("noexec")
    _expect_error(lambda: validate_ready_evidence(contract, evidence), "ready")
    evidence = _ready()
    evidence["distinct_from_roles"]["backup"] = False
    _expect_error(lambda: validate_ready_evidence(contract, evidence), "ready")

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        lock = root / "allocation.lock"
        first = lock.open("a+b")
        second = lock.open("a+b")
        import fcntl

        fcntl.flock(first.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            _expect_error(
                lambda: __import__("apps.recovery_scratch_bootstrap", fromlist=["acquire_allocation_lock"]).acquire_allocation_lock(second),
                "allocation",
            )
        finally:
            fcntl.flock(first.fileno(), fcntl.LOCK_UN)
            first.close()
            second.close()

    material = {"contract_name": "x", "created_at": "volatile"}
    assert plan_fingerprint(material) == plan_fingerprint(dict(material))
    print("recovery scratch bootstrap smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
