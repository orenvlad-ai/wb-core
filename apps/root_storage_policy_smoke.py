#!/usr/bin/env python3
"""Synthetic/fixture checks for the root-storage and journald contracts."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import root_storage_policy as app
from packages.application import root_storage_policy as policy_module
from packages.application.root_storage_policy import (
    GIB,
    MUTABLE_STORE_SERVICE_UNITS,
    RootStoragePolicyError,
    admit_root_write,
    collect_root_storage_status,
    load_policy,
    predict_sqlite_backup_bytes,
    read_root_storage_status_artifact,
    storage_destination_root,
    storage_level,
)


def main() -> int:
    policy = load_policy()
    legacy_policy = _legacy_activation_policy(policy)
    _assert_thresholds(policy)
    _assert_non_target_cas_registry(policy)
    _assert_storage_registry(policy)
    _assert_admission(policy)
    _assert_unregistered_detection(policy)
    _assert_unregistered_destination_detection(policy)
    _assert_status_artifact(policy)
    _assert_journal_manifest(legacy_policy)
    _assert_one_shot_activation(legacy_policy)
    _assert_reconciliation(legacy_policy)
    _assert_one_shot_correction(policy)
    _assert_corrective_reconciliation(policy)
    _assert_static_safety()
    _assert_recovery_scratch_release_bridge_is_manifest_bound()
    _assert_recovery_scratch_finance_exception_is_exact()
    _assert_recovery_scratch_post_submit_pending_is_exact()
    print("root_storage_policy_smoke: ok")
    return 0


def _assert_recovery_scratch_release_bridge_is_manifest_bound() -> None:
    from apps import business_data_maintenance as maintenance
    from apps import github_release_runner as release_runner
    from packages.application import business_data_write_barrier as barrier_module
    from packages.application import finance_storage_backup_rotation as finance_module

    manifest_path = (
        ROOT
        / "release/production-mutations/wbc0035_recovery_scratch_bootstrap.json"
    )
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    release_sha = "a" * 40
    bridge = release_runner.build_recovery_scratch_release_bridge(
        {
            "path": str(manifest_path.relative_to(ROOT)),
            "sha256": release_runner.sha256(manifest_raw),
        },
        manifest,
        release_sha,
    )
    assert bridge is not None
    policy = deepcopy(load_policy())
    expected = manifest["release_bridge"]
    health = {
        "status": "degraded",
        "blockers": [expected["finance"]["only_allowed_blocker"]],
        "retained_backup_id": expected["finance"]["retained_backup_id"],
        "canonical_source_bytes": expected["finance"]["canonical_source_bytes"],
        "next_replacement_required_bytes": expected["finance"][
            "next_replacement_required_bytes"
        ],
        "capacity_basis": expected["finance"]["capacity_basis"],
        "next_replacement_capacity": True,
        "available_bytes": 49_479_995_392,
    }
    barrier = {
        **expected["barrier"],
        "state_fingerprint": "sha256:" + "b" * 64,
    }
    timer_state = {"is_enabled": "disabled", "is_active": "inactive"}
    systemd = SimpleNamespace(unit_state=lambda _: dict(timer_state))
    with tempfile.TemporaryDirectory() as temporary:
        repository_root = Path(temporary) / "repo"
        fake_module = (
            repository_root / "packages/application/root_storage_policy.py"
        )
        fake_module.parent.mkdir(parents=True)
        fake_module.write_text("# fixture\n", encoding="utf-8")
        bound_manifest = (
            repository_root / policy_module.RECOVERY_SCRATCH_MANIFEST_PATH
        )
        bound_manifest.parent.mkdir(parents=True)
        bound_manifest.write_bytes(manifest_raw)
        (repository_root / ".wb-core-runtime-sha").write_text(
            release_sha + "\n", encoding="utf-8"
        )
        runtime = Path(temporary) / "runtime"
        runtime.mkdir()
        (runtime / maintenance.STATE_FILENAME).write_text(
            json.dumps({"phase": expected["barrier"]["maintenance_phase"]}),
            encoding="utf-8",
        )
        (runtime / maintenance.POLICY_FILENAME).write_text(
            json.dumps(
                {
                    "revision": expected["barrier"]["owner_policy_revision"],
                    "master_desired": False,
                }
            ),
            encoding="utf-8",
        )
        policy["storage_registry"]["filesystems"]["backup"]["path"] = str(
            runtime / "backups"
        )
        with mock.patch.object(
            policy_module, "__file__", str(fake_module)
        ), mock.patch.object(
            barrier_module, "barrier_status", return_value=barrier
        ), mock.patch.object(
            maintenance, "SystemdClient", return_value=systemd
        ), mock.patch.object(
            maintenance, "_writer_processes", return_value=[]
        ), mock.patch.object(
            finance_module, "backup_rotation_health", return_value=health
        ):
            validated = policy_module._validate_recovery_scratch_release_bridge(
                policy, bridge
            )
            assert validated["manifest_sha256"] == release_runner.sha256(manifest_raw)
            assert validated["release_sha"] == release_sha
            assert validated["target"] == manifest["target"]
            assert validated["operation_id"] == manifest["operation_id"]
            assert validated["live_preconditions"]["writer_processes"] == []
            drifted = {**bridge, "manifest_sha256": "not-a-sha256"}
            try:
                policy_module._validate_recovery_scratch_release_bridge(
                    policy, drifted
                )
            except RootStoragePolicyError as exc:
                assert "identity drifted" in str(exc)
            else:
                raise AssertionError("invalid release bridge digest was accepted")


def _assert_recovery_scratch_finance_exception_is_exact() -> None:
    expected = {
        "only_allowed_blocker": "retained backup exceeded RPO age",
        "retained_backup_id": "finance-backup-459a091d48326c9be224",
        "canonical_source_bytes": 26_567_401_472,
        "next_replacement_required_bytes": 35_224_444_928,
        "capacity_basis": "canonical_current_split_source_size_plus_copy_overhead_plus_hard_reserve",
    }
    health = {
        "status": "degraded",
        "blockers": [expected["only_allowed_blocker"]],
        "retained_backup_id": expected["retained_backup_id"],
        "canonical_source_bytes": expected["canonical_source_bytes"],
        "next_replacement_required_bytes": expected[
            "next_replacement_required_bytes"
        ],
        "capacity_basis": expected["capacity_basis"],
        "next_replacement_capacity": True,
        "available_bytes": 49_479_995_392,
    }
    policy_module._validate_recovery_scratch_finance_exception(health, expected)
    invalid = [
        {**health, "next_replacement_required_bytes": 33_608_519_680},
        {**health, "blockers": [*health["blockers"], "foreign violation"]},
        {**health, "retained_backup_id": "finance-backup-00000000000000000000"},
        {**health, "next_replacement_capacity": False},
        {**health, "available_bytes": expected["next_replacement_required_bytes"] - 1},
    ]
    for candidate in invalid:
        with mock.patch.object(policy_module, "_payload_digest"):
            try:
                policy_module._validate_recovery_scratch_finance_exception(
                    candidate, expected
                )
            except RootStoragePolicyError:
                pass
            else:
                raise AssertionError("invalid Finance release bridge was accepted")


def _assert_recovery_scratch_post_submit_pending_is_exact() -> None:
    from apps import recovery_scratch_bootstrap as bootstrap
    from apps import recovery_scratch_bootstrap_post_submit_reconcile as post_submit

    policy = deepcopy(load_policy())
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "recovery-scratch"
        evidence = {
            "contract_name": post_submit.CONTRACT,
            "status": "READY_TO_RECONCILE",
            "source_submit_count": 1,
            "continuation_submit_count": 0,
            "total_submit_count": 1,
        }
        with mock.patch.object(
            bootstrap,
            "validate_recovery_scratch_contract",
            return_value={
                **policy["storage_registry"]["filesystems"]["recovery_scratch"],
                "filesystem_label": "wb-recovery-scra",
            },
        ), mock.patch.object(
            post_submit,
            "collect_pre_change_evidence",
            return_value=evidence,
        ):
            observed = policy_module._recovery_scratch_pending_status(
                policy,
                path=path,
                recovery_scratch_release_bridge={"post_submit_manifest": {}},
            )
        assert observed["bootstrap_status"] == (
            "failed_after_submit_reconciliation_pending"
        )
        assert observed["partial_state"] == evidence
        with mock.patch.object(
            bootstrap,
            "validate_recovery_scratch_contract",
            return_value=policy["storage_registry"]["filesystems"][
                "recovery_scratch"
            ],
        ), mock.patch.object(
            post_submit,
            "collect_pre_change_evidence",
            side_effect=post_submit.PostSubmitReconcileError("source drift"),
        ):
            try:
                policy_module._recovery_scratch_pending_status(
                    policy,
                    path=path,
                    recovery_scratch_release_bridge={"post_submit_manifest": {}},
                )
            except RootStoragePolicyError as exc:
                assert "bootstrap-pending evidence is invalid" in str(exc)
            else:
                raise AssertionError("drifted post-submit state was admitted")


def _assert_thresholds(policy: dict[str, object]) -> None:
    assert storage_level(25 * GIB) == "normal"
    assert storage_level(24 * GIB) == "below_normal"
    assert storage_level(19 * GIB) == "warning"
    assert storage_level(14 * GIB) == "critical"
    assert storage_level(11 * GIB) == "hard"
    assert policy["thresholds_bytes"] == {
        "normal_available": 25 * GIB,
        "warning_below": 20 * GIB,
        "critical_below": 15 * GIB,
        "hard_deny_below": 12 * GIB,
        "large_output": 256 * 1024**2,
        "large_predicted_free_after_floor": 15 * GIB,
    }


def _assert_non_target_cas_registry(policy: dict[str, object]) -> None:
    bindings = policy["non_target_cas"]["active_mutable_canonical_stores"]
    assert [item["key"] for item in bindings] == [
        "finance_raw_current",
        "operational_current",
        "autoanswers_current",
    ]
    autoanswers = bindings[-1]
    assert autoanswers["owner"] == "autoanswers_operational_store"
    assert [item["filesystem_role"] for item in bindings] == [
        "generation",
        "generation",
        "root",
    ]
    assert autoanswers["resolver"] == {
        "type": "literal",
        "path": "/opt/wb-core-runtime/state/wb_autoanswers_runtime.sqlite3",
    }
    assert autoanswers["access_roles"] == [
        {
            "service": "wb-core-registry-http.service",
            "declared_role": "reader_writer",
            "allowed_access_modes": ["read_only", "read_write"],
        },
        {
            "service": "wb-core-autoanswers-readonly-sync.service",
            "declared_role": "reader_writer",
            "allowed_access_modes": ["read_only", "read_write"],
        },
        {
            "service": "wb-core-autoanswers-worker.service",
            "declared_role": "reader_writer",
            "allowed_access_modes": ["read_only", "read_write"],
        },
    ]
    declared_services = {
        role["service"]
        for binding in bindings
        for role in binding["access_roles"]
    }
    assert declared_services <= MUTABLE_STORE_SERVICE_UNITS
    assert "wb-ai-api.service" not in declared_services
    unit_root = (
        ROOT / "artifacts" / "registry_upload_http_entrypoint" / "systemd"
    )
    for service in declared_services:
        assert "*" not in service and "?" not in service and "[" not in service
        unit_path = unit_root / service
        assert unit_path.is_file(), f"mutable-store service has no repo-owned unit: {service}"
        unit_text = unit_path.read_text(encoding="utf-8")
        assert "ExecStart=" in unit_text
        assert "/opt/wb-core-runtime/app" in unit_text or " apps/" in unit_text
    broken = deepcopy(policy)
    broken["non_target_cas"]["active_mutable_canonical_stores"][-1][
        "owner"
    ] = "unknown-owner"
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "policy.json"
        path.write_text(json.dumps(broken), encoding="utf-8")
        try:
            load_policy(path)
        except RootStoragePolicyError as exc:
            assert "mutable canonical store binding is invalid" in str(exc)
        else:
            raise AssertionError("unknown mutable canonical owner did not fail closed")
    invalid_role = deepcopy(policy)
    invalid_role["non_target_cas"]["active_mutable_canonical_stores"][-1][
        "access_roles"
    ][0]["allowed_access_modes"] = ["read_only", "write_only"]
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "policy.json"
        path.write_text(json.dumps(invalid_role), encoding="utf-8")
        try:
            load_policy(path)
        except RootStoragePolicyError as exc:
            assert "access role is invalid" in str(exc)
        else:
            raise AssertionError("invalid mutable access mode did not fail closed")
    invalid_filesystem_role = deepcopy(policy)
    invalid_filesystem_role["non_target_cas"]["active_mutable_canonical_stores"][0][
        "filesystem_role"
    ] = "root"
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "policy.json"
        path.write_text(json.dumps(invalid_filesystem_role), encoding="utf-8")
        try:
            load_policy(path)
        except RootStoragePolicyError as exc:
            assert "filesystem role is invalid" in str(exc)
        else:
            raise AssertionError("wrong mutable filesystem role did not fail closed")


def _assert_storage_registry(policy: dict[str, object]) -> None:
    registry = policy["storage_registry"]
    assert registry["contract_version"] == "wb_core_storage_registry_v1"
    assert registry["filesystems"]["root"]["reserve_bytes"] == 25 * GIB
    assert registry["filesystems"]["backup"]["emergency_reserve_bytes"] == 8 * GIB
    assert registry["filesystems"]["generation"]["reserve_bytes"] == 8 * GIB
    scratch = registry["filesystems"]["recovery_scratch"]
    assert scratch["reserve_bytes"] == 8 * GIB
    assert scratch["parent_device_by_id"].endswith("QEMU_HARDDISK_vde")
    assert scratch["filesystem_uuid"] == "da019107-575c-4fe7-b698-e021b3fc83c8"
    producers = {item["owner"]: item for item in registry["producers"]}
    admission_producers = {item["owner"]: item for item in policy["producers"]}
    assert storage_destination_root(
        "ads_historical_recovery",
        policy=policy,
    ) == Path("/opt/wb-core-runtime/state/backups/ads-historical")
    assert producers["production_apply_evidence"]["destination_role"] == "backup"
    assert admission_producers["production_apply_evidence"] == {
        "owner": "production_apply_evidence",
        "classification": "discretionary_root_writer",
        "path_patterns": [],
    }
    assert producers["finance_storage_split_coherent_source"]["destination_role"] == "generation"
    assert producers["sqlite_hot_journal_reconcile_qualification"] == {
        "owner": "sqlite_hot_journal_reconcile_qualification",
        "current": True,
        "data_class": "ephemeral_recovery_verification",
        "destination_role": "recovery_scratch",
        "relative_roots": [""],
        "lifecycle_policy": "temporary_candidate",
        "capacity_mode": "source_size_plus_fixed_reserve",
        "max_single_write_bytes": 32 * GIB,
    }
    assert not [
        item["owner"]
        for item in producers.values()
        if item["current"] is True
        and item["destination_role"] == "root"
        and item["data_class"]
        not in {"canonical_business_store", "protected_excluded_promo_artifact"}
    ]
    lifecycle = registry["lifecycle_policies"]
    for item in producers.values():
        row = lifecycle[item["lifecycle_policy"]]
        assert row["retention_rule"]
        assert row["hold_rule"]
        assert row["compression"]
        assert row["restore_path"]
    broken = deepcopy(policy)
    broken["storage_registry"]["producers"][0]["lifecycle_policy"] = "unknown"
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "policy.json"
        path.write_text(json.dumps(broken), encoding="utf-8")
        try:
            load_policy(path)
        except RootStoragePolicyError as exc:
            assert "canonical storage producer is invalid" in str(exc)
        else:
            raise AssertionError("unknown storage lifecycle policy did not fail closed")


def _assert_admission(policy: dict[str, object]) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        destination = root / "out" / "backup.sqlite3"
        (root / "out").mkdir()
        sqlite_source = root / "source.sqlite3"
        sqlite_source.write_bytes(b"m" * 10)
        Path(str(sqlite_source) + "-wal").write_bytes(b"w" * 7)
        assert predict_sqlite_backup_bytes(sqlite_source) == 17

        with mock.patch.object(
            policy_module.os,
            "statvfs",
            return_value=SimpleNamespace(f_bavail=11 * GIB, f_frsize=1),
        ):
            try:
                admit_root_write(
                    owner="ads_historical_recovery",
                    destination=destination,
                    predicted_output_bytes=1,
                    policy=policy,
                    root_path=root,
                )
            except RootStoragePolicyError as exc:
                assert "below_hard_deny" in str(exc)
            else:
                raise AssertionError("hard root threshold admitted discretionary output")
            essential = admit_root_write(
                owner="registry_operational_store",
                destination=destination,
                predicted_output_bytes=1,
                policy=policy,
                root_path=root,
            )
            assert essential["allowed"] is True
            assert essential["storage_level"] == "hard"

        with mock.patch.object(
            policy_module.os,
            "statvfs",
            return_value=SimpleNamespace(f_bavail=16 * GIB, f_frsize=1),
        ):
            try:
                admit_root_write(
                    owner="buyout_mature_backfill",
                    destination=destination,
                    predicted_output_bytes=2 * GIB,
                    policy=policy,
                    root_path=root,
                )
            except RootStoragePolicyError as exc:
                assert "predicted_free_after_below_critical_floor" in str(exc)
            else:
                raise AssertionError("large predicted-free floor admitted unsafe output")

        with mock.patch.object(
            policy_module.os,
            "statvfs",
            return_value=SimpleNamespace(f_bavail=14 * GIB, f_frsize=1),
        ):
            bounded = admit_root_write(
                owner="ads_historical_recovery",
                destination=destination,
                predicted_output_bytes=1,
                policy=policy,
                root_path=root,
            )
            assert bounded["allowed"] is True
            assert bounded["storage_level"] == "critical"

        with mock.patch.object(
            policy_module.os,
            "statvfs",
            return_value=SimpleNamespace(f_bavail=30 * GIB, f_frsize=1),
        ):
            large_bounded = admit_root_write(
                owner="buyout_mature_backfill",
                destination=destination,
                predicted_output_bytes=1 * GIB,
                policy=policy,
                root_path=root,
            )
            assert large_bounded["allowed"] is True
            assert large_bounded["predicted_free_after_bytes"] == 29 * GIB

        for owner, predicted, destination_value in (
            ("", 1, destination),
            ("unknown-owner", 1, destination),
            ("ads_historical_recovery", None, destination),
            ("ads_historical_recovery", 1, Path("relative.sqlite3")),
        ):
            try:
                admit_root_write(
                    owner=owner,
                    destination=destination_value,
                    predicted_output_bytes=predicted,
                    policy=policy,
                    root_path=root,
                )
            except RootStoragePolicyError:
                pass
            else:
                raise AssertionError("unknown admission identity/output/destination did not fail closed")

        with mock.patch.object(
            policy_module,
            "_hosted_runtime_marker_present",
            return_value=True,
        ):
            try:
                admit_root_write(
                    owner="finance_legacy_helper",
                    destination=destination,
                    predicted_output_bytes=1,
                    policy=policy,
                    root_path=root,
                )
            except RootStoragePolicyError as exc:
                assert "no current write authority" in str(exc)
            else:
                raise AssertionError("retired producer acquired hosted write authority")

        production_plan = (
            storage_destination_root("production_apply_evidence", policy=policy)
            / "production-goals"
            / ("production-goal-v1-" + "7" * 32)
            / "wbc0013-a-plan-20260828T120000Z.json"
        )
        deployed_defect = deepcopy(policy)
        deployed_defect["producers"] = [
            item
            for item in deployed_defect["producers"]
            if item["owner"] != "production_apply_evidence"
        ]
        try:
            admit_root_write(
                owner="production_apply_evidence",
                destination=production_plan,
                predicted_output_bytes=108_853,
                policy=deployed_defect,
            )
        except RootStoragePolicyError as exc:
            assert str(exc) == (
                "unregistered large root writer owner: production_apply_evidence"
            )
        else:
            raise AssertionError("deployed production-plan storage defect was not reproduced")

        with (
            mock.patch.object(
                policy_module.os,
                "statvfs",
                return_value=SimpleNamespace(f_bavail=100 * GIB, f_frsize=1),
            ),
            mock.patch.object(
                policy_module,
                "_assert_filesystem_identity",
                return_value={"fixture": True},
            ),
            mock.patch.object(
                policy_module,
                "_required_reserve_bytes",
                return_value=8 * GIB,
            ),
        ):
            admitted_plan = admit_root_write(
                owner="production_apply_evidence",
                destination=production_plan,
                predicted_output_bytes=108_853,
                policy=policy,
            )
        assert admitted_plan["allowed"] is True
        assert admitted_plan["destination"] == str(production_plan)
        assert admitted_plan["destination_role"] == "backup"
        assert admitted_plan["predicted_output_bytes"] == 108_853


def _assert_unregistered_detection(policy: dict[str, object]) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        scan = root / "scan"
        scan.mkdir()
        registered = scan / "registered" / "known.bin"
        registered.parent.mkdir()
        unregistered = scan / "new-producer" / "unknown.bin"
        unregistered.parent.mkdir()
        _sparse_file(registered, 256 * 1024**2)
        _sparse_file(unregistered, 256 * 1024**2)
        fixture = deepcopy(policy)
        fixture["filesystems"] = {"root": str(root)}
        fixture["storage_registry"]["filesystems"]["root"].update(
            {
                "path": str(root),
                "source": "/dev/fixture",
                "filesystem_uuid": "fixture-uuid",
                "filesystem_type": "ext4",
                "required_mount_options": ["rw"],
            }
        )
        fixture["scan_roots"] = [str(scan)]
        fixture["producers"] = [
            {
                "owner": "known",
                "classification": "discretionary_root_writer",
                "path_patterns": [str(scan / "registered" / "**")],
            }
        ]
        with mock.patch.object(
            policy_module,
            "_mountinfo_for_path",
            return_value={
                "mount_id": 1,
                "mount_point": str(root),
                "source": "/dev/fixture",
                "filesystem_type": "ext4",
                "mount_options": "rw",
            },
        ), mock.patch.object(policy_module, "_filesystem_uuid", return_value="fixture-uuid"), mock.patch.object(
            policy_module.os,
            "statvfs",
            return_value=SimpleNamespace(
                f_bavail=30 * GIB,
                f_bfree=30 * GIB,
                f_blocks=40 * GIB,
                f_frsize=1,
                f_files=1000,
                f_ffree=900,
                f_favail=900,
            ),
        ):
            status = collect_root_storage_status(policy=fixture, root_path=root)
        assert len(status["large_root_files"]) == 2
        assert [item["path"] for item in status["unregistered_large_root_files"]] == [
            str(unregistered)
        ]
        assert status["alerts"] == [
            {"code": "unregistered_large_root_producer", "severity": "critical", "count": 1}
        ]


def _assert_unregistered_destination_detection(policy: dict[str, object]) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        backup = Path(temporary)
        known = backup / "ads-historical" / "known.sqlite3"
        unknown = backup / "new-producer" / "unknown.sqlite3"
        known.parent.mkdir()
        unknown.parent.mkdir()
        _sparse_file(known, 256 * 1024**2)
        _sparse_file(unknown, 256 * 1024**2)
        violations = policy_module._scan_unregistered_large_destinations(
            policy,
            role="backup",
            filesystem_root=backup,
            filesystem_device=backup.stat().st_dev,
        )
        assert violations == [
            {
                "role": "backup",
                "path": str(unknown.resolve()),
                "size_bytes": 256 * 1024**2,
                "device": unknown.stat().st_dev,
                "reason": "no_registered_destination_root",
            }
        ], violations


def _assert_status_artifact(policy: dict[str, object]) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        fixed_now = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)
        fixture = deepcopy(policy)
        fixture["filesystems"] = {"root": str(root)}
        fixture["storage_registry"]["filesystems"]["root"].update(
            {
                "path": str(root),
                "source": "/dev/fixture",
                "filesystem_uuid": "fixture-uuid",
                "filesystem_type": "ext4",
                "required_mount_options": ["rw"],
            }
        )
        fixture["scan_roots"] = [str(root)]
        with mock.patch.object(
            policy_module,
            "_mountinfo_for_path",
            return_value={
                "mount_id": 1,
                "mount_point": str(root),
                "source": "/dev/fixture",
                "filesystem_type": "ext4",
                "mount_options": "rw",
            },
        ), mock.patch.object(policy_module, "_filesystem_uuid", return_value="fixture-uuid"), mock.patch.object(
            policy_module.os,
            "statvfs",
            return_value=SimpleNamespace(
                f_bavail=30 * GIB,
                f_bfree=30 * GIB,
                f_blocks=40 * GIB,
                f_frsize=1,
                f_files=1000,
                f_ffree=900,
                f_favail=900,
            ),
        ):
            status = collect_root_storage_status(
                policy=fixture,
                root_path=root,
                now=fixed_now,
            )
        artifact = root / "status.json"
        app._write_json_atomic(artifact, status, mode=0o644)
        readback = read_root_storage_status_artifact(
            policy=fixture,
            artifact_path=artifact,
            now=fixed_now + timedelta(seconds=300),
        )
        assert readback["ok"] is True
        assert readback["fresh"] is True
        assert readback["age_seconds"] == 300

        unregistered = deepcopy(status)
        unregistered["unregistered_large_root_files"] = [{"path": str(root / "unknown.bin")}]
        unregistered["safe_for_discretionary_root_writes"] = False
        app._write_json_atomic(artifact, unregistered, mode=0o644)
        blocked = read_root_storage_status_artifact(
            policy=fixture,
            artifact_path=artifact,
            now=fixed_now + timedelta(seconds=300),
        )
        assert blocked["ok"] is False

        try:
            read_root_storage_status_artifact(
                policy=fixture,
                artifact_path=artifact,
                now=fixed_now + timedelta(seconds=601),
            )
        except RootStoragePolicyError as exc:
            assert "stale" in str(exc)
        else:
            raise AssertionError("stale root storage status artifact did not fail closed")


def _assert_journal_manifest(policy: dict[str, object]) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        machine_id = "a" * 32
        machine_path = root / "machine-id"
        machine_path.write_text(machine_id + "\n", encoding="utf-8")
        journal_root = root / "journal"
        current_dir = journal_root / machine_id
        foreign_dir = journal_root / ("b" * 32)
        current_dir.mkdir(parents=True)
        foreign_dir.mkdir(parents=True)
        eligible = current_dir / "system@eligible.journal"
        held = current_dir / "system@held.journal"
        opened = current_dir / "system@opened.journal"
        younger = current_dir / "system@younger.journal"
        current = current_dir / "system.journal"
        foreign = foreign_dir / "system.journal"
        non_journal = current_dir / "corrupt.journal~"
        for path in (eligible, held, opened, younger, current, foreign, non_journal):
            path.write_bytes(path.name.encode("utf-8"))
        now = datetime(2026, 8, 26, tzinfo=timezone.utc)
        cutoff = int(now.timestamp() * 1_000_000) - 14 * 24 * 60 * 60 * 1_000_000
        old_tail = cutoff - 1
        new_tail = cutoff + 1
        headers = {
            eligible: _header("ARCHIVED", machine_id, old_tail),
            held: _header("ARCHIVED", machine_id, old_tail),
            opened: _header("ARCHIVED", machine_id, old_tail),
            younger: _header("ARCHIVED", machine_id, new_tail),
            current: _header("ONLINE", machine_id, new_tail),
            foreign: _header("ONLINE", "b" * 32, old_tail),
        }
        opened_stat = opened.stat()
        hold_registry = root / "holds.json"
        hold_registry.write_text(
            json.dumps(
                {
                    "schema_version": app.JOURNAL_HOLD_CONTRACT,
                    "holds": [
                        {
                            "hold_id": "incident-1",
                            "kind": "incident",
                            "active": True,
                            "paths": [str(held)],
                            "reference": "INC-1",
                            "reason": "preserve exact incident evidence",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        fixture = deepcopy(policy)
        fixture["journald"]["hold_registry"] = str(hold_registry)
        manifest = app.build_journal_preflight(
            fixture,
            now=now,
            machine_id_path=machine_path,
            journal_root=journal_root,
            header_reader=lambda path: headers[path],
            opener_reader=lambda: {
                (int(opened_stat.st_dev), int(opened_stat.st_ino)): [
                    {"pid": 10, "comm": "systemd-journald", "fd": "7", "target": str(opened)}
                ]
            },
        )
        assert [item["path"] for item in manifest["eligible_entries"]] == [
            str(eligible),
            str(opened),
        ]
        assert [item["path"] for item in manifest["held_eligible_entries"]] == [str(held)]
        classifications = {item["path"]: item["archive_classification"] for item in manifest["entries"]}
        assert classifications[str(opened)] == "eligible_expired_archived_journal"
        assert classifications[str(younger)] == "archived_younger_than_retention"
        assert classifications[str(current)] == "current_or_online_journal"
        assert classifications[str(foreign)] == "foreign_machine_journal"
        assert classifications[str(non_journal)] == "non_journal_file"
        assert manifest["manifest_digest"].startswith("sha256:")
        app._assert_preflight_fresh(fixture, manifest, now=now)


def _assert_one_shot_activation(policy: dict[str, object]) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "source.conf"
        destination = root / "etc" / "journald.conf.d" / "60-wb.conf"
        evidence = root / "evidence"
        source.write_text("[Journal]\nSystemMaxUse=2G\nSystemKeepFree=15G\nMaxRetentionSec=14day\n", encoding="utf-8")
        fixture = deepcopy(policy)
        fixture["journald"]["configuration_source"] = "fixture.conf"
        fixture["journald"]["configuration_destination"] = str(destination)
        fixture["journald"]["evidence_directory"] = str(evidence)
        preflight = {
            "contract_version": app.JOURNAL_ACTIVATION_CONTRACT,
            "eligible_entries": [],
            "held_eligible_entries": [],
            "protected_entries": [],
            "eligible_count": 0,
            "eligible_bytes": 0,
            "protected_non_target_digest": app._protected_digest([]),
        }
        preflight["manifest_digest"] = app._digest_payload(preflight)
        completed = subprocess.CompletedProcess(
            ["systemctl", "restart", "systemd-journald.service"], 0, "", ""
        )
        readback = {"ok": True, "operation_id": "fixture", "service_after": {"main_pid": 2}}
        with mock.patch.object(app, "_resolve_repo_path", return_value=source), mock.patch.object(
            app, "_systemd_semantics_evidence", return_value={"systemd_version": "systemd 255"}
        ), mock.patch.object(app, "build_journal_preflight", return_value=preflight), mock.patch.object(
            app, "_assert_preflight_fresh"
        ), mock.patch.object(
            app, "_journald_service_identity", return_value={"main_pid": 1, "exec_main_start_timestamp": "before"}
        ), mock.patch.object(app.subprocess, "run", return_value=completed) as runner, mock.patch.object(
            app, "_wait_for_journald_readback", return_value=readback
        ), mock.patch.object(app, "readback_journald_retention", return_value=readback):
            first = app.activate_journald_retention(fixture)
            second = app.activate_journald_retention(fixture)
        restart_calls = [
            call
            for call in runner.call_args_list
            if call.args and call.args[0] == ["systemctl", "restart", "systemd-journald.service"]
        ]
        assert len(restart_calls) == 1
        assert first["activation_retried"] is False
        assert second["idempotent"] is True


def _assert_reconciliation(policy: dict[str, object]) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "source.conf"
        destination = root / "destination.conf"
        config = b"[Journal]\nSystemMaxUse=2G\nSystemKeepFree=15G\nMaxRetentionSec=14day\n"
        source.write_bytes(config)
        destination.write_bytes(config)
        desired_digest = app._sha256_file(source)
        operation_id = f"journald-retention-{desired_digest.removeprefix('sha256:')[:24]}"
        evidence = root / "evidence"
        operation_dir = evidence / "activations" / operation_id
        operation_dir.mkdir(parents=True)
        deleted_path = root / "deleted.journal"
        protected_path = root / "protected.journal"
        protected_path.write_bytes(b"protected")
        protected_stat = protected_path.stat()
        eligible = {
            "path": str(deleted_path),
            "device": int(protected_stat.st_dev),
            "inode": 999999,
            "size_bytes": 123,
            "mtime_ns": 1,
            "tail_realtime_epoch_us": 1,
            "archive_classification": "eligible_expired_archived_journal",
        }
        protected = {
            "path": str(protected_path),
            "device": int(protected_stat.st_dev),
            "inode": int(protected_stat.st_ino),
            "size_bytes": int(protected_stat.st_size),
            "mtime_ns": int(protected_stat.st_mtime_ns),
            "tail_realtime_epoch_us": 2,
            "archive_classification": "archived_younger_than_retention",
        }
        manifest = {
            "contract_version": app.JOURNAL_ACTIVATION_CONTRACT,
            "journal_root": str(root),
            "eligible_entries": [eligible],
            "held_eligible_entries": [],
            "protected_entries": [protected],
            "eligible_count": 1,
            "eligible_bytes": 123,
            "protected_non_target_digest": app._protected_digest([protected]),
        }
        manifest["manifest_digest"] = app._digest_payload(manifest)
        state = {
            "contract_version": app.JOURNAL_ACTIVATION_CONTRACT,
            "operation_id": operation_id,
            "phase": "restart_submit_intent",
            "desired_config_sha256": desired_digest,
            "manifest_digest": manifest["manifest_digest"],
            "restart_submit_count": 1,
            "service_before": {"main_pid": 1, "exec_main_start_timestamp": "before"},
        }
        app._write_json_atomic(operation_dir / "preflight-manifest.json", manifest, mode=0o600)
        app._write_json_atomic(operation_dir / "state.json", state, mode=0o600)
        fixture = deepcopy(policy)
        fixture["journald"]["configuration_source"] = "fixture.conf"
        fixture["journald"]["configuration_destination"] = str(destination)
        fixture["journald"]["evidence_directory"] = str(evidence)
        with mock.patch.object(app, "_resolve_repo_path", return_value=source), mock.patch.object(
            app,
            "_effective_journald_config",
            return_value={"matches_expected": True},
        ), mock.patch.object(
            app,
            "_journald_service_identity",
            return_value={"main_pid": 2, "exec_main_start_timestamp": "after"},
        ), mock.patch.object(
            app,
            "collect_root_storage_status",
            return_value={"status": "hard"},
        ), mock.patch.object(
            app.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(["journalctl", "--disk-usage"], 0, "Archived journals take up 1G", ""),
        ):
            readback = app.readback_journald_retention(fixture)
        assert readback["ok"] is True
        assert readback["deleted_count"] == 1
        assert readback["deleted_bytes"] == 123
        assert readback["protected_non_target_digest_matches"] is True
        assert not readback["protected_missing"]


def _assert_one_shot_correction(policy: dict[str, object]) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        destination = root / "etc" / "journald.conf.d" / "60-wb-core-root-retention.conf"
        destination.parent.mkdir(parents=True)
        source = (
            ROOT
            / "artifacts"
            / "registry_upload_http_entrypoint"
            / "journald"
            / "60-wb-core-root-retention.conf"
        )
        destination.write_bytes(source.read_bytes())
        evidence = root / "evidence"
        journal_root = root / "journal"
        journal_root.mkdir()
        fixture = deepcopy(policy)
        fixture["journald"]["configuration_destination"] = str(destination)
        fixture["journald"]["evidence_directory"] = str(evidence)
        fixture["journald"]["journal_root"] = str(journal_root)
        service_before = {
            "main_pid": 10,
            "exec_main_start_timestamp": "before",
            "active_state": "active",
            "sub_state": "running",
        }
        preflight = {
            "contract_version": app.JOURNAL_CORRECTION_CONTRACT,
            "legacy_activation_operation_id": fixture["journald"][
                "legacy_activation_operation_id"
            ],
            "configuration_destination": str(destination),
            "legacy_configuration_sha256": fixture["journald"][
                "legacy_configuration_sha256"
            ],
            "effective_config_before": {"matches_expected": True},
            "journal_root": str(journal_root),
            "journal_entries": [],
            "journal_entry_count": 0,
            "journal_file_count": 0,
            "non_journal_file_count": 0,
            "journal_total_bytes": 0,
            "journal_inventory_digest": app._journal_inventory_digest([]),
            "protected_identity_digest": app._journal_identity_digest([]),
            "service_before": service_before,
            "root_storage_status_before": {"status": "hard"},
        }
        preflight["manifest_digest"] = app._digest_payload(preflight)
        readback = {
            "ok": True,
            "service_after_attributed": {
                "main_pid": 11,
                "exec_main_start_timestamp": "after",
                "active_state": "active",
                "sub_state": "running",
            },
        }
        completed = subprocess.CompletedProcess(
            ["systemctl", "restart", "systemd-journald.service"], 0, "", ""
        )
        with mock.patch.object(
            app, "build_journald_correction_preflight", return_value=preflight
        ), mock.patch.object(
            app, "_assert_journald_correction_preflight_fresh"
        ), mock.patch.object(
            app, "_wait_for_journald_correction_readback", return_value=readback
        ), mock.patch.object(
            app, "readback_journald_correction", return_value=readback
        ), mock.patch.object(
            app.subprocess, "run", return_value=completed
        ) as runner:
            first = app.remove_journald_retention_dropin(fixture)
            second = app.remove_journald_retention_dropin(fixture)
        restart_calls = [
            call
            for call in runner.call_args_list
            if call.args
            and call.args[0] == ["systemctl", "restart", "systemd-journald.service"]
        ]
        assert len(restart_calls) == 1
        assert destination.exists() is False
        assert first["operation_retried"] is False
        assert second["idempotent"] is True


def _assert_corrective_reconciliation(policy: dict[str, object]) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        journal_root = root / "journal"
        journal_root.mkdir()
        archived = journal_root / "system@archived.journal"
        current = journal_root / "system.journal"
        non_journal = journal_root / "note.txt"
        archived.write_bytes(b"archived")
        current.write_bytes(b"current")
        non_journal.write_bytes(b"note")
        before = [
            _correction_entry(archived, mutable=False, state="ARCHIVED"),
            _correction_entry(current, mutable=True, state="ONLINE"),
            _correction_entry(non_journal, mutable=False, state=None),
        ]
        rotated = journal_root / "system@rotated.journal"
        current.rename(rotated)
        new_current = journal_root / "system.journal"
        new_current.write_bytes(b"new-current")
        after = [
            _correction_entry(archived, mutable=False, state="ARCHIVED"),
            _correction_entry(rotated, mutable=False, state="ARCHIVED"),
            _correction_entry(non_journal, mutable=False, state=None),
            _correction_entry(new_current, mutable=True, state="ONLINE"),
        ]
        fixture = deepcopy(policy)
        destination = root / "etc" / "journald.conf.d" / "60-wb-core-root-retention.conf"
        evidence = root / "evidence"
        fixture["journald"]["configuration_destination"] = str(destination)
        fixture["journald"]["evidence_directory"] = str(evidence)
        fixture["journald"]["journal_root"] = str(journal_root)
        correction = app._journald_correction_policy(fixture)
        correction_digest = app._digest_payload(correction)
        operation_id = f"journald-correction-{correction_digest.removeprefix('sha256:')[:24]}"
        operation_dir = evidence / "corrections" / operation_id
        operation_dir.mkdir(parents=True)
        service_before = {
            "main_pid": 10,
            "exec_main_start_timestamp": "before",
            "active_state": "active",
            "sub_state": "running",
        }
        service_after = {
            "main_pid": 11,
            "exec_main_start_timestamp": "after",
            "active_state": "active",
            "sub_state": "running",
        }
        manifest = {
            "contract_version": app.JOURNAL_CORRECTION_CONTRACT,
            "journal_entries": before,
            "journal_entry_count": len(before),
            "journal_file_count": 2,
            "non_journal_file_count": 1,
            "journal_total_bytes": sum(item["size_bytes"] for item in before),
            "journal_inventory_digest": app._journal_inventory_digest(before),
            "protected_identity_digest": app._journal_identity_digest(before),
            "service_before": service_before,
            "root_storage_status_before": {"status": "hard"},
        }
        manifest["manifest_digest"] = app._digest_payload(manifest)
        state = {
            "contract_version": app.JOURNAL_CORRECTION_CONTRACT,
            "operation_id": operation_id,
            "phase": "restart_submit_intent",
            "correction_digest": correction_digest,
            "manifest_digest": manifest["manifest_digest"],
            "dropin_unlink_submit_count": 1,
            "dropin_removed_at": "2026-08-26T09:00:00Z",
            "restart_submit_count": 1,
            "restart_submit_recorded_at": "2026-08-26T09:00:01Z",
            "service_before": service_before,
            "service_after": service_after,
        }
        app._write_json_atomic(operation_dir / "preflight-manifest.json", manifest, mode=0o600)
        app._write_json_atomic(operation_dir / "state.json", state, mode=0o600)
        with mock.patch.object(
            app,
            "_effective_journald_config",
            return_value={"matches_expected": True, "values": {}, "expected": {}},
        ), mock.patch.object(
            app, "_journald_service_identity", return_value=service_after
        ), mock.patch.object(
            app, "_collect_correction_journal_inventory", return_value=after
        ), mock.patch.object(
            app, "collect_root_storage_status", return_value={"status": "hard"}
        ), mock.patch.object(
            app.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["journalctl", "--disk-usage"], 0, "Archived journals take up 1G", ""
            ),
        ):
            readback = app.readback_journald_correction(fixture)
        assert readback["ok"] is True
        assert readback["deleted_count"] == 0
        assert len(readback["moved_current_entries"]) == 1
        assert len(readback["new_entries"]) == 1
        assert readback["protected_identity_digest_matches"] is True
        assert readback["pid_transition_count"] == 1
        done_state = {
            **state,
            "phase": "done",
            "service_after": service_after,
            "completion_readback": readback,
            "readback_digest": app._digest_payload(readback),
        }
        app._write_json_atomic(operation_dir / "state.json", done_state, mode=0o600)
        with mock.patch.object(
            app,
            "_effective_journald_config",
            return_value={"matches_expected": True, "values": {}, "expected": {}},
        ), mock.patch.object(
            app, "_journald_service_identity", return_value=service_after
        ), mock.patch.object(
            app,
            "_collect_correction_journal_inventory",
            side_effect=AssertionError("durable completion must not replay old inventory"),
        ), mock.patch.object(
            app, "collect_root_storage_status", return_value={"status": "hard"}
        ), mock.patch.object(
            app.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["journalctl", "--disk-usage"], 0, "Archived journals take up 1G", ""
            ),
        ):
            durable = app.readback_journald_correction(fixture)
        assert durable["ok"] is True
        assert durable["durable_completion_reused"] is True
        assert durable["journal_inventory_before"] == readback["journal_inventory_before"]


def _assert_static_safety() -> None:
    source = Path(app.__file__).read_text(encoding="utf-8")
    app._validate_journald_config_bytes(
        b"[Journal]\nSystemMaxUse=2G\nSystemKeepFree=15G\nMaxRetentionSec=14day\n"
    )
    try:
        app._validate_journald_config_bytes(
            b"[Journal]\nSystemMaxUse=3G\nSystemKeepFree=15G\nMaxRetentionSec=14day\n"
        )
    except RootStoragePolicyError:
        pass
    else:
        raise AssertionError("journald setting drift did not fail before activation")
    assert "journalctl\", \"--vacuum" not in source
    assert "journalctl\", \"--rotate" not in source
    assert "unlink" not in source.split("def activate_journald_retention", 1)[1].split("def build_journal_preflight", 1)[0]
    correction_source = source.split("def remove_journald_retention_dropin", 1)[1].split(
        "def build_journald_correction_preflight", 1
    )[0]
    assert correction_source.count("destination.unlink()") == 1
    assert correction_source.count('["systemctl", "restart", "systemd-journald.service"]') == 1
    policy = load_policy()
    assert policy["journald"]["mode"] == app.JOURNAL_CORRECTION_MODE
    assert "configuration_source" not in policy["journald"]
    active_target = json.loads(
        (
            ROOT
            / "artifacts"
            / "registry_upload_http_entrypoint"
            / "input"
            / "hosted_runtime_target__europe_api.json"
        ).read_text(encoding="utf-8")
    )
    managed = {
        item["name"]: item for item in active_target["managed_systemd_units"]
    }
    assert managed["wb-core-root-storage-policy.service"] == {
        "name": "wb-core-root-storage-policy.service",
        "enable": False,
        "restart": True,
    }
    assert managed["wb-core-root-storage-policy.timer"] == {
        "name": "wb-core-root-storage-policy.timer",
        "enable": True,
        "restart": True,
    }
    unit = (
        ROOT
        / "artifacts"
        / "registry_upload_http_entrypoint"
        / "systemd"
        / "wb-core-root-storage-policy.service"
    ).read_text(encoding="utf-8")
    assert "StateDirectory=wb-core-root-storage-policy" in unit
    assert "status.json --fail-on-unregistered" in unit
    assert "--allow-recovery-scratch-bootstrap-pending" in unit
    assert "journald" not in unit.lower()
    sanitation_unit = (
        ROOT
        / "artifacts"
        / "registry_upload_http_entrypoint"
        / "systemd"
        / "wb-core-storage-recovery-sanitation@.service"
    ).read_text(encoding="utf-8")
    assert "ConditionPathIsMountPoint=/opt/wb-core-runtime/state/recovery-scratch" in sanitation_unit
    assert "ReadWritePaths=" in sanitation_unit
    assert "/opt/wb-core-runtime/state/recovery-scratch" in sanitation_unit


def _legacy_activation_policy(policy: dict[str, object]) -> dict[str, object]:
    fixture = deepcopy(policy)
    fixture["journald"] = {
        "expected_systemd_major": 255,
        "configuration_source": "artifacts/registry_upload_http_entrypoint/journald/60-wb-core-root-retention.conf",
        "configuration_destination": "/etc/systemd/journald.conf.d/60-wb-core-root-retention.conf",
        "evidence_directory": "/var/lib/wb-core/root-storage-policy",
        "hold_registry": "/etc/wb-core/journal-retention-holds.json",
        "system_max_use_bytes": 2 * GIB,
        "system_keep_free_bytes": 15 * GIB,
        "max_retention_seconds": 14 * 24 * 60 * 60,
    }
    return fixture


def _correction_entry(path: Path, *, mutable: bool, state: str | None) -> dict[str, object]:
    stat = path.stat()
    is_journal = path.name.endswith(".journal")
    return {
        "path": str(path),
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "size_bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "is_journal_file": is_journal,
        "mutable_current": mutable,
        "journal_state": state,
        "journal_file_id": "fixture" if is_journal else None,
        "journal_machine_id": "a" * 32 if is_journal else None,
        "head_realtime_epoch_us": 1 if is_journal else None,
        "tail_realtime_epoch_us": 2 if is_journal else None,
        "opener_evidence": {"method": "fixture", "openers": [], "no_openers": True},
    }


def _header(state: str, machine_id: str, tail: int) -> dict[str, object]:
    return {
        "state": state,
        "file_id": f"file-{tail}",
        "machine_id": machine_id,
        "head_realtime_epoch_us": tail - 1,
        "tail_realtime_epoch_us": tail,
    }


def _sparse_file(path: Path, size: int) -> None:
    with path.open("wb") as handle:
        handle.seek(size - 1)
        handle.write(b"\0")


if __name__ == "__main__":
    raise SystemExit(main())
