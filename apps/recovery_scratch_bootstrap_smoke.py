#!/usr/bin/env python3
"""Focused fail-closed contract checks for the recovery scratch disk."""

from __future__ import annotations

import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import recovery_scratch_bootstrap as bootstrap
from apps import recovery_scratch_bootstrap_post_submit_reconcile as post_submit
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
    "filesystem_label": "wb-recovery-scra",
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


def _exercise_exact_operation_dry_run() -> None:
    deployed_sha = "a" * 40
    exact_operation = "wbc0035-026-recovery-scratch-a01"
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        target = root / "target.json"
        deployed = root / "deployed.sha"
        output = Path(
            "/opt/wb-core-runtime/state/backups/private-evidence/"
            f"recovery-scratch-bootstrap/plan-{deployed_sha}.json"
        )
        target.write_text(
            json.dumps(
                {
                    "runtime_env": {
                        "REGISTRY_UPLOAD_RUNTIME_DIR": "/opt/wb-core-runtime/state"
                    },
                    "recovery_scratch_filesystem": CONTRACT,
                }
            ),
            encoding="utf-8",
        )
        deployed.write_text(deployed_sha + "\n", encoding="utf-8")
        preflight_calls: list[str] = []
        written_plans: list[dict] = []
        original_collect = bootstrap.collect_blank_device_evidence
        original_fstab = bootstrap._fstab_identity
        original_write = bootstrap.write_plan
        original_digest = bootstrap._file_digest
        bootstrap.collect_blank_device_evidence = lambda contract: (
            preflight_calls.append(str(contract["parent_device_by_id"])) or _blank()
        )
        bootstrap._fstab_identity = lambda _: {
            "path": "/etc/fstab",
            "sha256": "sha256:" + "0" * 64,
            "size_bytes": 0,
            "mode": 0o644,
            "uid": 0,
            "gid": 0,
        }
        bootstrap.write_plan = lambda path, plan: written_plans.append(dict(plan))
        bootstrap._file_digest = lambda _: "sha256:" + "1" * 64
        try:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = bootstrap.main(
                    [
                        "--target-file",
                        str(target),
                        "--deployed-sha",
                        deployed_sha,
                        "--deployed-sha-file",
                        str(deployed),
                        "dry-run",
                        "--operation-id",
                        exact_operation,
                        "--approval-reference",
                        "WBC0035/031 exact operation binding regression",
                        "--output",
                        str(output),
                    ]
                )
            payload = json.loads(stdout.getvalue())
            assert result == 0
            assert payload["status"] == "ready_to_initialize"
            assert payload["operation_id"] == exact_operation
            assert written_plans[0]["operation_id"] == exact_operation
            assert preflight_calls == [CONTRACT["parent_device_by_id"]]

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = bootstrap.main(
                    [
                        "--target-file",
                        str(target),
                        "--deployed-sha",
                        deployed_sha,
                        "--deployed-sha-file",
                        str(deployed),
                        "dry-run",
                        "--operation-id",
                        "wbc0035-025-recovery-scratch-a01",
                        "--approval-reference",
                        "WBC0035/031 wrong operation regression",
                        "--output",
                        str(output),
                    ]
                )
            payload = json.loads(stdout.getvalue())
            assert result == 2
            assert payload["error"] == "recovery scratch operation/approval binding is invalid"
            assert preflight_calls == [CONTRACT["parent_device_by_id"]]
        finally:
            bootstrap.collect_blank_device_evidence = original_collect
            bootstrap._fstab_identity = original_fstab
            bootstrap.write_plan = original_write
            bootstrap._file_digest = original_digest


def _exercise_existing_plan_continuation() -> None:
    deployed_sha = "b" * 40
    operation_id = "wbc0035-026-recovery-scratch-a01"
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        state_dir = root / "recovery-scratch-bootstrap"
        state_dir.mkdir()
        plan_path = state_dir / f"plan-{deployed_sha}.json"
        deployed = root / "deployed.sha"
        deployed.write_text(deployed_sha + "\n", encoding="utf-8")
        fstab = root / "fstab"
        fstab.write_text("# exact pre-submit fstab\n", encoding="utf-8")
        target = {
            **CONTRACT,
            "path": str(root / "runtime" / "recovery-scratch"),
            "completion_marker": str(state_dir / "completed.json"),
        }
        base_plan = {
            "contract_name": bootstrap.PLAN_CONTRACT,
            "status": "ready_to_initialize",
            "operation_id": operation_id,
            "deployed_sha": deployed_sha,
            "approval_reference": "WBC0035/032 existing-plan continuation regression",
            "target": target,
            "blank_device": _blank(),
            "fstab_before": bootstrap._fstab_identity(fstab),
            "layout": {},
            "expected_effect": {
                "disk_initialized": True,
                "mount_persisted": True,
                "business_database_mutation": 0,
                "recovery_submit": 0,
                "barrier_change": False,
                "timer_change": False,
            },
            "submit_count": 0,
            "created_at": "2026-09-03T00:00:00Z",
        }

        def persist_plan(payload: dict) -> tuple[str, str]:
            material = copy.deepcopy(payload)
            material["fingerprint"] = plan_fingerprint(material)
            plan_path.write_text(
                bootstrap._canonical_json(material) + "\n",
                encoding="utf-8",
            )
            return bootstrap._file_digest(plan_path), material["fingerprint"]

        preflight_calls: list[str] = []
        original_validate = bootstrap.validate_recovery_scratch_contract
        original_collect = bootstrap.collect_blank_device_evidence
        bootstrap.validate_recovery_scratch_contract = (
            lambda payload, *, runtime_dir: dict(payload)
        )

        def stop_at_exact_disk_preflight(contract: dict) -> dict:
            preflight_calls.append(str(contract["parent_device_by_id"]))
            raise RecoveryScratchError("mocked exact disk preflight reached")

        bootstrap.collect_blank_device_evidence = stop_at_exact_disk_preflight
        try:
            plan_sha256, fingerprint = persist_plan(base_plan)
            _expect_error(
                lambda: bootstrap.apply_plan(
                    plan_path=plan_path,
                    plan_sha256=plan_sha256,
                    fingerprint=fingerprint,
                    deployed_sha_file=deployed,
                    fstab_path=fstab,
                ),
                "mocked exact disk preflight reached",
            )
            assert preflight_calls == [CONTRACT["parent_device_by_id"]]
            assert not (state_dir / "intent.json").exists()
            assert not (state_dir / "failure.json").exists()
            assert not (state_dir / "completed.json").exists()

            for invalid in (None, "0", 1, 0.0, False):
                changed = {**base_plan, "submit_count": invalid}
                plan_sha256, fingerprint = persist_plan(changed)
                _expect_error(
                    lambda: bootstrap.apply_plan(
                        plan_path=plan_path,
                        plan_sha256=plan_sha256,
                        fingerprint=fingerprint,
                        deployed_sha_file=deployed,
                        fstab_path=fstab,
                    ),
                    "plan contract drifted",
                )
            changed = dict(base_plan)
            changed.pop("submit_count")
            plan_sha256, fingerprint = persist_plan(changed)
            _expect_error(
                lambda: bootstrap.apply_plan(
                    plan_path=plan_path,
                    plan_sha256=plan_sha256,
                    fingerprint=fingerprint,
                    deployed_sha_file=deployed,
                    fstab_path=fstab,
                ),
                "plan contract drifted",
            )
            assert preflight_calls == [CONTRACT["parent_device_by_id"]]

            plan_sha256, fingerprint = persist_plan(base_plan)
            (state_dir / "intent.json").write_text("{}\n", encoding="utf-8")
            _expect_error(
                lambda: bootstrap.apply_plan(
                    plan_path=plan_path,
                    plan_sha256=plan_sha256,
                    fingerprint=fingerprint,
                    deployed_sha_file=deployed,
                    fstab_path=fstab,
                ),
                "prior submit is ambiguous or failed; retry forbidden",
            )
            assert preflight_calls == [CONTRACT["parent_device_by_id"]]
        finally:
            bootstrap.validate_recovery_scratch_contract = original_validate
            bootstrap.collect_blank_device_evidence = original_collect


def _exercise_post_submit_reconciliation_apply_path() -> None:
    forbidden = {"sfdisk", "mkfs.ext4", "mkfs", "wipefs", "e2label", "tune2fs"}

    def run_case(*, fail_after_mount: bool) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_dir = root / "state"
            state_dir.mkdir()
            target_path = root / "runtime" / "recovery-scratch"
            target_path.parent.mkdir()
            completion = state_dir / "completed.json"
            deployed = root / "deployed.sha"
            deployed.write_text("c" * 40 + "\n", encoding="utf-8")
            fstab = root / "fstab"
            fstab.write_text("# preserved fstab\n", encoding="utf-8")
            backup = state_dir / "fstab.before"
            backup.write_bytes(fstab.read_bytes())
            plan = state_dir / "plan.json"
            plan.write_text(
                json.dumps({"fstab_before": bootstrap._fstab_identity(fstab)}),
                encoding="utf-8",
            )
            failure = state_dir / "failure.json"
            failure.write_text('{"status":"failed_after_submit"}\n', encoding="utf-8")
            failure_before = failure.read_bytes()
            manifest = {
                "post_submit_reconciliation": {
                    "source": {
                        "fstab_backup_path": str(backup),
                        "plan_path": str(plan),
                    },
                    "predecessor_receipts": {
                        "apply_receipt_sha256": "sha256:" + "4" * 64,
                    },
                }
            }
            contract = {
                **CONTRACT,
                "path": str(target_path),
                "completion_marker": str(completion),
            }
            commands: list[list[str]] = []
            mounted = False
            ready_calls = 0
            original_preflight = post_submit.collect_pre_change_evidence
            original_verify = bootstrap._verify_deployed_sha
            original_ready = bootstrap.collect_ready_evidence
            original_run = bootstrap._run
            original_ismount = post_submit.os.path.ismount
            original_source_fstab_sha = post_submit.SOURCE_FSTAB_SHA256
            original_readback = post_submit.readback

            def fake_run(argv: list[str], **_kwargs):
                nonlocal mounted
                commands.append(list(argv))
                if argv[0] == "mount":
                    mounted = True
                elif argv[0] == "umount":
                    mounted = False
                return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            def fake_ready(_contract: dict, *, require_completion_marker: bool = True):
                nonlocal ready_calls
                ready_calls += 1
                if fail_after_mount and ready_calls == 1:
                    raise RecoveryScratchError("forced ready failure")
                return {
                    **_ready(),
                    "path": str(target_path),
                    "completion_marker_present": require_completion_marker,
                }

            post_submit.collect_pre_change_evidence = lambda **_kwargs: {
                "pre_change_digest_value": "sha256:" + "5" * 64,
            }
            bootstrap._verify_deployed_sha = lambda sha, _path: sha
            bootstrap.collect_ready_evidence = fake_ready
            bootstrap._run = fake_run
            post_submit.os.path.ismount = lambda path: mounted and Path(path) == target_path
            post_submit.SOURCE_FSTAB_SHA256 = bootstrap._file_digest(fstab)
            try:
                if fail_after_mount:
                    _expect_error(
                        lambda: post_submit.apply_reconciliation(
                            contract=contract,
                            manifest=manifest,
                            deployed_sha="c" * 40,
                            deployed_sha_file=deployed,
                            approval_reference="exact correction authorization",
                            fstab_path=fstab,
                        ),
                        "forced ready failure",
                    )
                    assert [item[0] for item in commands] == ["mount", "umount"]
                    assert fstab.read_bytes() == backup.read_bytes()
                    assert not target_path.exists()
                    assert not completion.exists()
                else:
                    result = post_submit.apply_reconciliation(
                        contract=contract,
                        manifest=manifest,
                        deployed_sha="c" * 40,
                        deployed_sha_file=deployed,
                        approval_reference="exact correction authorization",
                        fstab_path=fstab,
                    )
                    assert [item[0] for item in commands] == ["mount"]
                    assert result["failure_disposition"] == "reconciled_preserved"
                    assert result["source_submit_count"] == 1
                    assert result["continuation_submit_count"] == 0
                    assert result["total_submit_count"] == 1
                    assert completion.is_file()
                    commands.clear()
                    post_submit.readback = lambda **_kwargs: {
                        "status": "READY",
                        "source_submit_count": 1,
                        "continuation_submit_count": 0,
                        "total_submit_count": 1,
                    }
                    repeated = post_submit.apply_reconciliation(
                        contract=contract,
                        manifest=manifest,
                        deployed_sha="c" * 40,
                        deployed_sha_file=deployed,
                        approval_reference="exact correction authorization",
                        fstab_path=fstab,
                    )
                    assert repeated["already_terminal"] is True
                    assert repeated["continuation_apply_count_this_call"] == 0
                    assert commands == []
                assert failure.read_bytes() == failure_before
                flattened = {part for command in commands for part in command}
                assert not forbidden.intersection(flattened), commands
                assert not (state_dir / "intent.json").exists()
            finally:
                post_submit.collect_pre_change_evidence = original_preflight
                bootstrap._verify_deployed_sha = original_verify
                bootstrap.collect_ready_evidence = original_ready
                bootstrap._run = original_run
                post_submit.os.path.ismount = original_ismount
                post_submit.SOURCE_FSTAB_SHA256 = original_source_fstab_sha
                post_submit.readback = original_readback

    run_case(fail_after_mount=False)
    run_case(fail_after_mount=True)


def _exercise_post_submit_manifest_binding() -> None:
    manifest = json.loads(
        (
            ROOT
            / "release/production-mutations/wbc0035_recovery_scratch_bootstrap.json"
        ).read_text(encoding="utf-8")
    )
    binding = post_submit.validate_manifest_contract(manifest)
    assert manifest["pre_change_digest_value"] == bootstrap._digest(binding)
    for mutate in (
        lambda item: item["post_submit_reconciliation"]["source"].__setitem__(
            "intent_sha256", "sha256:" + "0" * 64
        ),
        lambda item: item["post_submit_reconciliation"]["partial_state"].__setitem__(
            "effective_filesystem_label", "wb-recovery-scratch"
        ),
        lambda item: item["post_submit_reconciliation"]["predecessor_receipts"].__setitem__(
            "apply_count", 0
        ),
    ):
        changed = copy.deepcopy(manifest)
        mutate(changed)
        try:
            post_submit.validate_manifest_contract(changed)
        except post_submit.PostSubmitReconcileError:
            pass
        else:
            raise AssertionError("expected exact post-submit manifest rejection")


def main() -> int:
    _exercise_exact_operation_dry_run()
    _exercise_existing_plan_continuation()
    _exercise_post_submit_reconciliation_apply_path()
    _exercise_post_submit_manifest_binding()
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
