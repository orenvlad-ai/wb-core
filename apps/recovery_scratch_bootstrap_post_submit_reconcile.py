#!/usr/bin/env python3
"""Finish the exact WBC0035 recovery-scratch operation after its one disk submit."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping

from apps import recovery_scratch_bootstrap as bootstrap


CONTRACT = "wb_core_recovery_scratch_post_submit_reconcile_v1"
MANIFEST_CONTRACT = "wbc0035_recovery_scratch_post_submit_reconciliation/v1"
OPERATION_ID = "wbc0035-026-recovery-scratch-a01"
SOURCE_DEPLOYED_SHA = "2d4b1ac35c11c6569465dbd8db897f8541efc021"
SOURCE_PLAN_SHA256 = (
    "sha256:feb5e1c550a3af00d27c45a2bb29cc7f37fb99cd8c46dbc8e000454dc1c1deae"
)
SOURCE_PLAN_FINGERPRINT = (
    "sha256:6d94a1d28c1f48c9cea2522211e497baabd17ba3161eec006cbdfa1368f24ac5"
)
SOURCE_INTENT_SHA256 = (
    "sha256:8a609890910a2f77b9d7e1e95031eedee96bf11969f0d2ce2d4a1801bc83b24f"
)
SOURCE_FAILURE_SHA256 = (
    "sha256:f025680c726474d0ace9f3abb0764622c852ecefa05f90adddd928214703d737"
)
SOURCE_FSTAB_SHA256 = (
    "sha256:9ee7901aec59dd3aace4e8f6a644b2f10326c0466f9b8115607bc3f83dfcd7c2"
)
SOURCE_APPROVAL = (
    "/wb-core apply-v2 pr 1187 merge 2d4b1ac35c11c6569465dbd8db897f8541efc021 "
    "deployed 2d4b1ac35c11c6569465dbd8db897f8541efc021 manifest "
    "sha256:5fdc1f51dd92fa869c4adfb741024ff2a738b6fb55d307df36d175bf7849b245 "
    "operation wbc0035-026-recovery-scratch-a01"
)
SOURCE_ERROR = (
    "recovery scratch mounted-ready identity failed: mismatches="
    "{'filesystem_label': {'expected': 'wb-recovery-scratch', "
    "'observed': 'wb-recovery-scra'}}, missing_options=[], bad_roles=[]"
)
LINUX_FILESYSTEM_GUID = "0fc63daf-8483-4772-8e79-3d69d8477de4"
TIMER_STATE = {"is_active": "inactive", "is_enabled": "disabled"}
TIMER_UNITS = [
    "wb-core-autoanswers-readonly-sync.timer",
    "wb-core-autoanswers-worker.timer",
    "wb-core-fbs-shadow-collector.timer",
    "wb-core-fbs-warehouse-registry.timer",
    "wb-core-feedbacks-auto-complaints-tick.timer",
    "wb-core-finance-backup-rotation.timer",
    "wb-core-sheet-vitrina-canary-restore.timer",
    "wb-core-sheet-vitrina-closure-retry.timer",
    "wb-core-sheet-vitrina-health-candidate.timer",
    "wb-core-sheet-vitrina-health-confirmation.timer",
    "wb-core-sheet-vitrina-refresh.timer",
    "wb-core-warehouse-functional-sync.timer",
    "wb-core-wb-finance-weekly.timer",
]
SOURCE_DIR = Path(
    "/opt/wb-core-runtime/state/backups/private-evidence/recovery-scratch-bootstrap"
)


class PostSubmitReconcileError(RuntimeError):
    """The exact already-submitted continuation failed closed."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PostSubmitReconcileError(f"immutable JSON is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise PostSubmitReconcileError(f"immutable JSON is not an object: {path}")
    return payload


def _manifest_binding(manifest: Mapping[str, Any]) -> dict[str, Any]:
    if (
        manifest.get("contract") != "wbc0035_recovery_scratch_bootstrap_passport/v1"
        or manifest.get("operation_id") != OPERATION_ID
        or dict(manifest.get("recovery_contract") or {})
        != {
            "mode": "bounded-recovery",
            "id": "wbc0035-recovery-scratch-post-submit-reconcile-v1",
        }
    ):
        raise PostSubmitReconcileError("post-submit manifest identity drifted")
    binding = manifest.get("post_submit_reconciliation")
    if not isinstance(binding, Mapping):
        raise PostSubmitReconcileError("post-submit manifest binding is unavailable")
    result = dict(binding)
    source = dict(result.get("source") or {})
    partial = dict(result.get("partial_state") or {})
    receipts = dict(result.get("predecessor_receipts") or {})
    expected_source = {
        "deployed_sha": SOURCE_DEPLOYED_SHA,
        "plan_path": str(SOURCE_DIR / f"plan-{SOURCE_DEPLOYED_SHA}.json"),
        "plan_sha256": SOURCE_PLAN_SHA256,
        "plan_fingerprint": SOURCE_PLAN_FINGERPRINT,
        "intent_path": str(SOURCE_DIR / "intent.json"),
        "intent_sha256": SOURCE_INTENT_SHA256,
        "failure_path": str(SOURCE_DIR / "failure.json"),
        "failure_sha256": SOURCE_FAILURE_SHA256,
        "fstab_backup_path": str(SOURCE_DIR / "fstab.before"),
        "fstab_backup_sha256": SOURCE_FSTAB_SHA256,
        "source_submit_count": 1,
        "retry_allowed": False,
    }
    expected_partial = {
        "parent_device": "/dev/sdd",
        "parent_device_by_id": "/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_vde",
        "partition_device": "/dev/sdd1",
        "partition_device_by_id": "/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_vde-part1",
        "parent_serial": "vde",
        "parent_model": "QEMU HARDDISK",
        "parent_size_bytes": 53_687_091_200,
        "parent_major_minor": "8:48",
        "parent_hctl": "0:0:0:4",
        "disk_guid": "b19fe03c-84c7-438c-91db-2e57bbf2a06e",
        "partition_start_sector": 2048,
        "partition_size_sectors": 104_853_504,
        "partition_type_guid": LINUX_FILESYSTEM_GUID,
        "partition_guid": "9a0f40dd-bb7d-4af1-82bc-40a0960dee85",
        "filesystem_uuid": "da019107-575c-4fe7-b698-e021b3fc83c8",
        "filesystem_type": "ext4",
        "effective_filesystem_label": "wb-recovery-scra",
        "filesystem_state": "clean",
        "empty_root": True,
        "mounted": False,
        "target_present": False,
        "completion_present": False,
        "fstab_sha256": SOURCE_FSTAB_SHA256,
        "fstab_matches_source_backup": True,
        "barrier_state_fingerprint": "sha256:4c797873e01d49ede5fca25657ef20a63ed6636b35d1280c7eee67903f3ff235",
        "business_timer_units": TIMER_UNITS,
        "business_timers": TIMER_STATE,
        "writers": [],
        "finance": {
            "status": "degraded",
            "blockers": ["retained backup exceeded RPO age"],
            "retained_backup_id": "finance-backup-459a091d48326c9be224",
            "canonical_source_bytes": 26_567_401_472,
            "next_replacement_required_bytes": 35_224_444_928,
            "capacity_basis": "canonical_current_split_source_size_plus_copy_overhead_plus_hard_reserve",
            "next_replacement_capacity": True,
        },
        "non_target_filesystem_uuids": {
            "root": "d77f6a25-e90f-4292-a85d-9bcc1cecf9e2",
            "backup": "bd3d563f-e5ea-4e4a-a76a-be45e7f94ec0",
            "generation": "284b3362-b890-431d-a7da-7f0fcd2ee0a6",
        },
    }
    expected_receipts = {
        "pull_request": 1187,
        "merge_sha": SOURCE_DEPLOYED_SHA,
        "release_operation_id": "release-v2-0f4d7b5f045479b76a2fe414746ec254",
        "release_runner_run_id": 33761914522,
        "release_source_workflow_run_id": 33760940637,
        "release_comment_id": 5526679517,
        "release_artifact_name": "release-receipt-33760940637",
        "release_receipt_sha256": "sha256:b7c186b6de268a58da26e03be5111665959847c0587ec51e962b47aebf98fbbc",
        "authorization_comment_id": 5526687892,
        "apply_runner_run_id": 33762528337,
        "apply_comment_id": 5526695696,
        "apply_artifact_name": "production-apply-receipt-pr-1187-run-33762528337",
        "apply_receipt_sha256": "sha256:49893afe89a9808176fdb107599f18d207b119fe84802104881ec48871377dce",
        "apply_state": "blocked",
        "apply_count": 1,
    }
    if (
        result.get("contract") != MANIFEST_CONTRACT
        or set(result) != {
            "contract", "source", "partial_state", "predecessor_receipts"
        }
        or source != expected_source
        or partial != expected_partial
        or receipts != expected_receipts
    ):
        raise PostSubmitReconcileError("post-submit manifest source binding drifted")
    expected_digest = bootstrap._digest(result)
    if manifest.get("pre_change_digest_value") != expected_digest:
        raise PostSubmitReconcileError("post-submit pre-change digest drifted")
    return result


def validate_manifest_contract(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the correction-only manifest extension without reading live state."""

    return _manifest_binding(manifest)


def _source_artifacts(binding: Mapping[str, Any]) -> dict[str, Any]:
    source = dict(binding["source"])
    paths = {
        name: Path(str(source[f"{name}_path"]))
        for name in ("plan", "intent", "failure")
    }
    paths["fstab_backup"] = Path(str(source["fstab_backup_path"]))
    observed_hashes = {
        name: bootstrap._file_digest(path) for name, path in paths.items()
    }
    expected_hashes = {
        name: str(source[f"{name}_sha256"]) for name in paths
    }
    if observed_hashes != expected_hashes:
        raise PostSubmitReconcileError("post-submit immutable source bytes drifted")
    plan = _read_json(paths["plan"])
    intent = _read_json(paths["intent"])
    failure = _read_json(paths["failure"])
    if (
        plan.get("contract_name") != bootstrap.PLAN_CONTRACT
        or plan.get("status") != "ready_to_initialize"
        or plan.get("operation_id") != OPERATION_ID
        or plan.get("deployed_sha") != SOURCE_DEPLOYED_SHA
        or plan.get("approval_reference") != SOURCE_APPROVAL
        or plan.get("plan_fingerprint", plan.get("fingerprint"))
        not in {None, SOURCE_PLAN_FINGERPRINT}
        or plan.get("fingerprint") != SOURCE_PLAN_FINGERPRINT
        or bootstrap.plan_fingerprint(plan) != SOURCE_PLAN_FINGERPRINT
        or type(plan.get("submit_count")) is not int
        or plan.get("submit_count") != 0
        or dict(plan.get("target") or {}).get("filesystem_label")
        != "wb-recovery-scratch"
        or dict(plan.get("layout") or {}).get("filesystem_label")
        != "wb-recovery-scratch"
    ):
        raise PostSubmitReconcileError("post-submit source plan drifted")
    common = {
        "contract_name": bootstrap.RESULT_CONTRACT,
        "operation_id": OPERATION_ID,
        "deployed_sha": SOURCE_DEPLOYED_SHA,
        "plan_sha256": SOURCE_PLAN_SHA256,
        "plan_fingerprint": SOURCE_PLAN_FINGERPRINT,
        "approval_reference": SOURCE_APPROVAL,
        "submit_count": 1,
    }
    if any(intent.get(key) != value for key, value in common.items()) or intent.get(
        "status"
    ) != "submitted":
        raise PostSubmitReconcileError("post-submit source intent drifted")
    if (
        any(failure.get(key) != value for key, value in common.items())
        or failure.get("status") != "failed_after_submit"
        or failure.get("retry_allowed") is not False
        or failure.get("error_type") != "RecoveryScratchError"
        or failure.get("error") != SOURCE_ERROR
        or dict(failure.get("rollback") or {})
        != {"fstab_restored": True, "mount_unmounted": True}
    ):
        raise PostSubmitReconcileError("post-submit source failure drifted")
    return {
        "plan": {"path": str(paths["plan"]), "sha256": observed_hashes["plan"]},
        "intent": {"path": str(paths["intent"]), "sha256": observed_hashes["intent"]},
        "failure": {"path": str(paths["failure"]), "sha256": observed_hashes["failure"]},
        "fstab_backup": {
            "path": str(paths["fstab_backup"]),
            "sha256": observed_hashes["fstab_backup"],
        },
        "source_submit_count": 1,
        "retry_allowed": False,
    }


def _timer_and_business_guards(binding: Mapping[str, Any]) -> dict[str, Any]:
    from apps import business_data_maintenance as maintenance
    from packages.application.business_data_write_barrier import barrier_status
    from packages.application.finance_storage_backup_rotation import (
        backup_rotation_health,
    )

    partial = dict(binding["partial_state"])
    runtime = Path("/opt/wb-core-runtime/state")
    barrier = barrier_status(runtime)
    state = _read_json(runtime / maintenance.STATE_FILENAME)
    owner_policy = _read_json(runtime / maintenance.POLICY_FILENAME)
    systemd = maintenance.SystemdClient()
    timer_states = {
        unit: {
            "is_active": systemd.unit_state(unit).get("is_active"),
            "is_enabled": systemd.unit_state(unit).get("is_enabled"),
        }
        for unit in maintenance.ALL_BUSINESS_TIMER_UNITS
    }
    expected_timers = sorted(partial.get("business_timer_units") or [])
    finance = backup_rotation_health(runtime)
    expected_finance = dict(partial.get("finance") or {})
    if (
        barrier.get("active") is not True
        or barrier.get("phase") != "acquiring"
        or barrier.get("hold_confirmed") is not False
        or barrier.get("window_id")
        != "wbc0027-s047-live-last-good-freeze-v2-896b02c0"
        or barrier.get("plan_fingerprint")
        != "sha256:0d680ca758c1699fe2a9025b01d71f0fa4f8c5bcf7555a7945b5b930cdc5285f"
        or barrier.get("state_fingerprint")
        != partial.get("barrier_state_fingerprint")
        or state.get("phase") != "abort_quiescing"
        or int(owner_policy.get("revision") or 0) != 59
        or owner_policy.get("master_desired") is not False
        or sorted(timer_states) != expected_timers
        or any(item != TIMER_STATE for item in timer_states.values())
        or maintenance._writer_processes(Path("/proc"))
        or finance.get("status") != "degraded"
        or list(finance.get("blockers") or [])
        != ["retained backup exceeded RPO age"]
        or any(finance.get(key) != value for key, value in expected_finance.items())
        or int(finance.get("available_bytes") or 0)
        < int(finance.get("next_replacement_required_bytes") or 0)
    ):
        raise PostSubmitReconcileError("post-submit business guard drifted")
    return {
        "barrier": {
            key: barrier.get(key)
            for key in (
                "active", "phase", "hold_confirmed", "window_id",
                "plan_fingerprint", "state_fingerprint",
            )
        },
        "maintenance_phase": state.get("phase"),
        "owner_policy_revision": int(owner_policy.get("revision") or 0),
        "business_timers": timer_states,
        "writers": [],
        "finance": {
            **expected_finance,
            "available_bytes": int(finance.get("available_bytes") or 0),
        },
    }


def _non_target_guards(binding: Mapping[str, Any]) -> dict[str, str]:
    partial = dict(binding["partial_state"])
    expected = dict(partial.get("non_target_filesystem_uuids") or {})
    observed: dict[str, str] = {}
    for role, path in bootstrap.ROLE_PATHS.items():
        mount = bootstrap._mount_entry(path)
        observed[role] = str(
            bootstrap._blkid_export(Path(str(mount["source"]))).get("UUID") or ""
        ).lower()
    if observed != expected:
        raise PostSubmitReconcileError("post-submit non-target filesystem drifted")
    return observed


def _partial_device(contract: Mapping[str, Any]) -> dict[str, Any]:
    parent = Path(str(contract["parent_device_by_id"]))
    partition = Path(str(contract["partition_device_by_id"]))
    if not parent.is_symlink() or not partition.is_symlink():
        raise PostSubmitReconcileError("post-submit stable device identity is unavailable")
    parent_resolved = parent.resolve(strict=True)
    partition_resolved = partition.resolve(strict=True)
    parent_row = bootstrap._lsblk(str(parent))
    partition_row = bootstrap._lsblk(str(partition))
    children = list(parent_row.get("children") or [])
    blkid_parent = bootstrap._blkid_export(parent)
    blkid_partition = bootstrap._blkid_export(partition)
    mount_major_minors = {
        fields[2]
        for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
        if len(fields := line.split()) > 4
        and fields[2] in {
            str(parent_row.get("maj:min") or ""),
            str(partition_row.get("maj:min") or ""),
        }
    }
    openers: list[str] = []
    for device in (parent, partition):
        result = bootstrap._run(["fuser", "-v", str(device)], allowed_returncodes=(0, 1))
        if result.returncode == 0:
            openers.extend(
                line for line in (result.stdout + result.stderr).splitlines() if line.strip()
            )
    kname_parent = str(parent_row.get("kname") or "").removeprefix("/dev/")
    kname_partition = str(partition_row.get("kname") or "").removeprefix("/dev/")
    pvs = (
        bootstrap._run(
            ["pvs", "--noheadings", "--separator", "|", "-o", "pv_name"],
            allowed_returncodes=(0, 5),
        )
        if Path("/usr/sbin/pvs").exists() or Path("/sbin/pvs").exists()
        else None
    )
    lvm = [] if pvs is None else [
        line.strip()
        for line in pvs.stdout.splitlines()
        if str(parent_resolved) in line or str(partition_resolved) in line
    ]
    md = [
        line
        for line in Path("/proc/mdstat").read_text(encoding="utf-8").splitlines()
        if re.search(rf"\b(?:{re.escape(kname_parent)}|{re.escape(kname_partition)})\b", line)
    ]
    swaps = [
        line
        for line in Path("/proc/swaps").read_text(encoding="utf-8").splitlines()
        if str(parent_resolved) in line or str(partition_resolved) in line
    ]
    refs = bootstrap._config_references(contract, str(parent_resolved))
    superblock = bootstrap._run(["dumpe2fs", "-h", str(partition)]).stdout
    filesystem_state = ""
    for line in superblock.splitlines():
        if line.startswith("Filesystem state:"):
            filesystem_state = line.split(":", 1)[1].strip().lower()
            break
    check = bootstrap._run(["e2fsck", "-fn", str(partition)])
    debug = bootstrap._run(["debugfs", "-R", "ls -p /", str(partition)])
    root_names = []
    for line in debug.stdout.splitlines():
        match = re.match(r"^/\d+/\d+/\d+/\d+/([^/]+)/", line.strip())
        if match:
            root_names.append(match.group(1))
    used = {
        "mount_major_minors": sorted(mount_major_minors),
        "openers": openers,
        "parent_holders": bootstrap._sysfs_names(kname_parent, "holders"),
        "parent_slaves": bootstrap._sysfs_names(kname_parent, "slaves"),
        "partition_holders": bootstrap._sysfs_names(kname_partition, "holders"),
        "partition_slaves": bootstrap._sysfs_names(kname_partition, "slaves"),
        "lvm_memberships": lvm,
        "md_memberships": md,
        "swap_memberships": swaps,
        "config_references": refs,
    }
    mismatches = {
        "parent_resolved": str(parent_resolved) != "/dev/sdd",
        "partition_resolved": str(partition_resolved) != "/dev/sdd1",
        "parent_major_minor": parent_row.get("maj:min") != contract["parent_major_minor"],
        "parent_size": int(parent_row.get("size") or 0) != contract["parent_size_bytes"],
        "parent_serial": str(parent_row.get("serial") or "") != contract["parent_serial"],
        "parent_model": str(parent_row.get("model") or "").strip() != contract["parent_model"],
        "parent_hctl": str(parent_row.get("hctl") or "") != contract["parent_hctl"],
        "parent_type": parent_row.get("type") != "disk",
        "parent_children": len(children) != 1,
        "partition_type": partition_row.get("type") != "part",
        "partition_parent": str(partition_row.get("pkname") or "").removeprefix("/dev/")
        != kname_parent,
        "partition_start": int(partition_row.get("start") or -1) != 2048,
        "partition_size": int(partition_row.get("size") or -1) // 512
        != 104_853_504,
        "partition_type_guid": str(partition_row.get("parttype") or "").lower()
        != LINUX_FILESYSTEM_GUID,
        "partition_guid": str(partition_row.get("partuuid") or "").lower()
        != contract["partition_guid"],
        "disk_guid": str(blkid_parent.get("PTUUID") or "").lower()
        != contract["disk_guid"],
        "partition_table": str(blkid_parent.get("PTTYPE") or "").lower()
        != "gpt",
        "filesystem_uuid": str(blkid_partition.get("UUID") or "").lower()
        != contract["filesystem_uuid"],
        "filesystem_label": str(blkid_partition.get("LABEL") or "")
        != contract["filesystem_label"],
        "filesystem_type": str(blkid_partition.get("TYPE") or "").lower() != "ext4",
        "filesystem_state": filesystem_state != "clean",
        "empty_root": sorted(root_names) != [".", ".."],
        "parent_ptuuid": str(blkid_parent.get("PTUUID") or "").lower()
        != contract["disk_guid"],
        "parent_pttype": str(blkid_parent.get("PTTYPE") or "").lower() != "gpt",
    }
    if any(mismatches.values()) or any(used.values()) or check.returncode != 0:
        raise PostSubmitReconcileError(
            "post-submit disk state drifted: "
            f"mismatches={sorted(key for key, value in mismatches.items() if value)}, "
            f"used={sorted(key for key, value in used.items() if value)}"
        )
    return {
        "status": "failed_after_submit_reconciliation_pending",
        "parent_device_by_id": str(parent),
        "resolved_parent_device": str(parent_resolved),
        "partition_device_by_id": str(partition),
        "resolved_partition_device": str(partition_resolved),
        "parent_major_minor": str(parent_row.get("maj:min") or ""),
        "parent_size_bytes": int(parent_row.get("size") or 0),
        "parent_serial": str(parent_row.get("serial") or ""),
        "parent_model": str(parent_row.get("model") or "").strip(),
        "parent_hctl": str(parent_row.get("hctl") or ""),
        "partition_table": "gpt",
        "disk_guid": str(blkid_parent["PTUUID"]).lower(),
        "partition_start_sector": int(partition_row["start"]),
        "partition_size_sectors": int(partition_row["size"]) // 512,
        "partition_type_guid": str(partition_row["parttype"]).lower(),
        "partition_guid": str(partition_row["partuuid"]).lower(),
        "filesystem_uuid": str(blkid_partition["UUID"]).lower(),
        "filesystem_label": str(blkid_partition["LABEL"]),
        "filesystem_type": str(blkid_partition["TYPE"]).lower(),
        "filesystem_state": filesystem_state,
        "filesystem_check_read_only": True,
        "empty_root": True,
        "mounted": False,
        "usage": used,
    }


def collect_pre_change_evidence(
    *,
    contract: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    binding = _manifest_binding(manifest)
    source = _source_artifacts(binding)
    partial = _partial_device(contract)
    expected = dict(binding["partial_state"])
    target = Path(str(contract["path"]))
    completion = Path(str(contract["completion_marker"]))
    fstab = Path("/etc/fstab")
    backup = Path(str(dict(binding["source"])["fstab_backup_path"]))
    refs = bootstrap._config_references(contract, str(Path(str(contract["parent_device_by_id"])).resolve()))
    if (
        target.exists()
        or completion.exists()
        or bootstrap._file_digest(fstab) != expected["fstab_sha256"]
        or fstab.read_bytes() != backup.read_bytes()
        or refs
        or partial["filesystem_label"] != expected["effective_filesystem_label"]
    ):
        raise PostSubmitReconcileError("post-submit restored pre-change state drifted")
    non_targets = _non_target_guards(binding)
    business = _timer_and_business_guards(binding)
    return {
        "contract_name": CONTRACT,
        "status": "READY_TO_RECONCILE",
        "operation_id": OPERATION_ID,
        "source_deployed_sha": SOURCE_DEPLOYED_SHA,
        "source_submit_count": 1,
        "continuation_submit_count": 0,
        "total_submit_count": 1,
        "pre_change_digest_value": bootstrap._digest(binding),
        "source_artifacts": source,
        "partial_device": partial,
        "fstab": {
            "path": str(fstab),
            "sha256": bootstrap._file_digest(fstab),
            "matches_source_backup": True,
            "target_references": [],
        },
        "target_present": False,
        "completion_present": False,
        "non_target_filesystem_uuids": non_targets,
        "business_guards": business,
        "predecessor_receipts": dict(binding["predecessor_receipts"]),
    }


def apply_reconciliation(
    *,
    contract: Mapping[str, Any],
    manifest: Mapping[str, Any],
    deployed_sha: str,
    deployed_sha_file: Path,
    approval_reference: str,
    fstab_path: Path = Path("/etc/fstab"),
) -> dict[str, Any]:
    deployed = bootstrap._verify_deployed_sha(deployed_sha, deployed_sha_file)
    approval = str(approval_reference or "").strip()
    if not approval or len(approval) > 500:
        raise PostSubmitReconcileError("post-submit approval binding is invalid")
    completion = Path(str(contract["completion_marker"]))
    if completion.exists():
        result = readback(
            contract=contract,
            manifest=manifest,
            deployed_sha=deployed,
            deployed_sha_file=deployed_sha_file,
        )
        return {**result, "already_terminal": True, "continuation_apply_count_this_call": 0}
    preflight = collect_pre_change_evidence(contract=contract, manifest=manifest)
    state_dir = completion.parent
    lock_path = state_dir / "bootstrap.lock"
    lock = lock_path.open("a+b")
    os.chmod(lock_path, 0o600)
    try:
        bootstrap.acquire_allocation_lock(lock)
    except Exception:
        lock.close()
        raise
    target = Path(str(contract["path"]))
    created_target = False
    fstab_published = False
    mounted_this_call = False
    completion_published = False
    try:
        before = fstab_path.read_bytes()
        source_fstab = dict(
            dict(manifest["post_submit_reconciliation"])["source"]
        )
        if (
            bootstrap._file_digest(fstab_path) != SOURCE_FSTAB_SHA256
            or before
            != Path(str(source_fstab["fstab_backup_path"])).read_bytes()
        ):
            raise PostSubmitReconcileError(
                "post-submit fstab drifted before continuation"
            )
        second = collect_pre_change_evidence(contract=contract, manifest=manifest)
        if second["pre_change_digest_value"] != preflight["pre_change_digest_value"]:
            raise PostSubmitReconcileError("post-submit pre-change witness drifted")
        target.mkdir(mode=0o700)
        created_target = True
        os.chmod(target, 0o700)
        bootstrap._replace_fstab(fstab_path, contract, before)
        fstab_published = True
        bootstrap._run(["mount", str(target)])
        mounted_this_call = True
        os.chmod(target, 0o700)
        ready = bootstrap.collect_ready_evidence(
            contract, require_completion_marker=False
        )
        result = {
            "contract_name": bootstrap.RESULT_CONTRACT,
            "reconciliation_contract": CONTRACT,
            "status": "READY",
            "operation_id": OPERATION_ID,
            "deployed_sha": deployed,
            "source_deployed_sha": SOURCE_DEPLOYED_SHA,
            "source_plan_sha256": SOURCE_PLAN_SHA256,
            "source_plan_fingerprint": SOURCE_PLAN_FINGERPRINT,
            "source_intent_sha256": SOURCE_INTENT_SHA256,
            "source_failure_sha256": SOURCE_FAILURE_SHA256,
            "source_apply_receipt_sha256": dict(
                dict(manifest["post_submit_reconciliation"])["predecessor_receipts"]
            )["apply_receipt_sha256"],
            "approval_reference": approval,
            "failure_disposition": "reconciled_preserved",
            "source_submit_count": 1,
            "continuation_submit_count": 0,
            "submit_count": 1,
            "total_submit_count": 1,
            "business_database_mutation": 0,
            "recovery_submit": 0,
            "pre_change_digest_value": preflight["pre_change_digest_value"],
            "ready": ready,
            "completed_at": bootstrap._now(),
        }
        result["result_fingerprint"] = bootstrap._digest(result)
        bootstrap._atomic_json(completion, result)
        completion_published = True
        final_ready = bootstrap.collect_ready_evidence(contract)
        return {
            **result,
            "ready": final_ready,
            "continuation_apply_count_this_call": 1,
        }
    except Exception as exc:
        if completion_published or completion.exists():
            raise
        rollback_errors: list[str] = []
        try:
            if mounted_this_call and target.exists() and os.path.ismount(target):
                bootstrap._run(["umount", str(target)])
        except Exception as rollback_exc:
            rollback_errors.append(f"umount={rollback_exc}")
        try:
            if fstab_published:
                plan = _read_json(Path(str(source_fstab["plan_path"])))
                bootstrap._restore_fstab(
                    fstab_path, before, dict(plan["fstab_before"])
                )
        except Exception as rollback_exc:
            rollback_errors.append(f"fstab={rollback_exc}")
        try:
            if (
                created_target
                and target.is_dir()
                and not os.path.ismount(target)
                and not any(target.iterdir())
            ):
                target.rmdir()
        except Exception as rollback_exc:
            rollback_errors.append(f"target={rollback_exc}")
        if rollback_errors:
            raise PostSubmitReconcileError(
                "post-submit reconciliation rollback failed: "
                + "; ".join(rollback_errors)
            ) from exc
        raise
    finally:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            lock.close()


def readback(
    *,
    contract: Mapping[str, Any],
    manifest: Mapping[str, Any],
    deployed_sha: str,
    deployed_sha_file: Path,
) -> dict[str, Any]:
    binding = _manifest_binding(manifest)
    deployed = bootstrap._verify_deployed_sha(deployed_sha, deployed_sha_file)
    source = _source_artifacts(binding)
    _non_target_guards(binding)
    business = _timer_and_business_guards(binding)
    completion = Path(str(contract["completion_marker"]))
    result = _read_json(completion)
    material = dict(result)
    fingerprint = str(material.pop("result_fingerprint", ""))
    required = {
        "contract_name": bootstrap.RESULT_CONTRACT,
        "reconciliation_contract": CONTRACT,
        "status": "READY",
        "operation_id": OPERATION_ID,
        "deployed_sha": deployed,
        "source_deployed_sha": SOURCE_DEPLOYED_SHA,
        "source_plan_sha256": SOURCE_PLAN_SHA256,
        "source_plan_fingerprint": SOURCE_PLAN_FINGERPRINT,
        "source_intent_sha256": SOURCE_INTENT_SHA256,
        "source_failure_sha256": SOURCE_FAILURE_SHA256,
        "source_apply_receipt_sha256": dict(binding["predecessor_receipts"])[
            "apply_receipt_sha256"
        ],
        "failure_disposition": "reconciled_preserved",
        "source_submit_count": 1,
        "continuation_submit_count": 0,
        "submit_count": 1,
        "total_submit_count": 1,
        "business_database_mutation": 0,
        "recovery_submit": 0,
        "pre_change_digest_value": bootstrap._digest(binding),
    }
    if (
        any(result.get(key) != value for key, value in required.items())
        or fingerprint != bootstrap._digest(material)
        or not str(result.get("approval_reference") or "").strip()
    ):
        raise PostSubmitReconcileError("post-submit completion marker drifted")
    ready = bootstrap.collect_ready_evidence(contract)
    return {
        **required,
        "ready": ready,
        "source_artifacts": source,
        "business_guards": business,
        "result_fingerprint": fingerprint,
    }


def _load_contract(target_file: Path) -> tuple[dict[str, Any], Path]:
    target = _read_json(target_file)
    runtime = Path(str(dict(target.get("runtime_env") or {}).get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""))
    contract = bootstrap.validate_recovery_scratch_contract(
        dict(target.get("recovery_scratch_filesystem") or {}), runtime_dir=runtime
    )
    return contract, runtime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-file", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--deployed-sha-file", type=Path, required=True)
    children = parser.add_subparsers(dest="command", required=True)
    children.add_parser("dry-run")
    apply = children.add_parser("apply")
    apply.add_argument("--approval-reference", required=True)
    children.add_parser("readback")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        contract, _runtime = _load_contract(args.target_file)
        manifest = _read_json(args.manifest)
        if args.command == "dry-run":
            bootstrap._verify_deployed_sha(args.deployed_sha, args.deployed_sha_file)
            payload = collect_pre_change_evidence(contract=contract, manifest=manifest)
        elif args.command == "apply":
            payload = apply_reconciliation(
                contract=contract,
                manifest=manifest,
                deployed_sha=args.deployed_sha,
                deployed_sha_file=args.deployed_sha_file,
                approval_reference=args.approval_reference,
            )
        else:
            payload = readback(
                contract=contract,
                manifest=manifest,
                deployed_sha=args.deployed_sha,
                deployed_sha_file=args.deployed_sha_file,
            )
        print(bootstrap._canonical_json(payload))
        return 0
    except (
        PostSubmitReconcileError,
        bootstrap.RecoveryScratchError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(
            bootstrap._canonical_json(
                {
                    "contract_name": CONTRACT,
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
