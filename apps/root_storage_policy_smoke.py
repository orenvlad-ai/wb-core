#!/usr/bin/env python3
"""Small offline checks for the root-storage command surface."""

from __future__ import annotations

import sys
from copy import deepcopy
import json
import stat
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import root_storage_policy as app  # noqa: E402
from packages.application import root_storage_policy as policy  # noqa: E402
from packages.application.warehouse_recovery_policy import (  # noqa: E402
    RecoveryPolicyError,
    WarehouseRecoveryRegistry,
)


def _backup_reserve_boundary(loaded: dict) -> None:
    # Finance supplies the complete next-copy requirement, including its own
    # 8 GiB hard reserve. The outer policy must add only another 4 GiB.
    next_copy = 28 * policy.GIB + 64 * policy.MIB + 8 * policy.GIB
    health = {
        "status": "healthy", "next_replacement_capacity": True,
        "next_replacement_required_bytes": next_copy, "blockers": [],
    }
    contract = loaded["storage_registry"]["filesystems"]["backup"]
    observed = {**contract, "device": 0, "mount_options": "rw"}
    with patch(
        "packages.application.finance_storage_backup_rotation.backup_rotation_health",
        return_value=health,
    ) as health_read, patch.object(policy, "_scan_unregistered_large_destinations", return_value=[]):
        floor = policy._finance_backup_floor(loaded)
        assert floor["required_reserve_bytes"] == next_copy + 4 * policy.GIB
        for difference, breached in ((-1, True), (0, False), (1, False)):
            observed["available_bytes"] = floor["required_reserve_bytes"] + difference
            result = policy._collect_storage_registry_status(
                loaded, observed_filesystems={"backup": observed}
            )
            assert result["roles"]["backup"]["reserve_breached"] is breached
        for unhealthy in (
            {**health, "status": "degraded"},
            {**health, "next_replacement_capacity": False},
            {**health, "blockers": ["current backup is not verified"]},
        ):
            health_read.return_value = unhealthy
            result = policy._collect_storage_registry_status(
                loaded, observed_filesystems={"backup": observed}
            )
            assert result["roles"]["backup"]["reserve_breached"] is True
            assert result["roles"]["backup"]["required_reserve_bytes"] == -1
    # A missing reserve or a stale policy must still fail closed.
    for invalid in (0, 8 * policy.GIB):
        changed = deepcopy(loaded)
        changed["storage_registry"]["filesystems"]["backup"]["emergency_reserve_bytes"] = invalid
        try:
            policy._validate_storage_registry(changed)
        except policy.RootStoragePolicyError:
            pass
        else:
            raise AssertionError("invalid backup reserve was accepted")


def _autoanswers_v11_backup_admission(loaded: dict) -> None:
    from packages.application.wb_autoanswers_runtime import SCHEMA_VERSION

    owner = "autoanswers_first_schema"
    relative = f"wb_autoanswers_schema_v{SCHEMA_VERSION}"
    contract = loaded["storage_registry"]["filesystems"]["backup"]
    assert policy.storage_destination_root(owner, relative_root=relative, policy=loaded) == (
        Path(contract["path"]) / relative
    ).resolve()
    # Offline filesystem/Finance facts; the native owner, destination, quota and
    # full reserve admission remain real, without an admission override.
    with TemporaryDirectory() as directory:
        backup = Path(directory) / "backups"
        backup.mkdir()
        offline = deepcopy(loaded)
        offline["storage_registry"]["filesystems"]["backup"]["path"] = str(backup)
        destination = policy.storage_destination_root(owner, relative_root=relative, policy=offline) / "fresh.sqlite3"
        next_copy = 28 * policy.GIB + 8 * policy.GIB
        floor = next_copy + int(contract["emergency_reserve_bytes"])
        peak = 4 * policy.MIB
        observed = {**contract, "mount_options": ",".join(contract["required_mount_options"])}
        health = {"status": "healthy", "next_replacement_capacity": True,
                  "next_replacement_required_bytes": next_copy, "blockers": []}
        with patch.object(policy, "_hosted_runtime_marker_present", return_value=True), patch.object(
            policy, "_filesystem_status", return_value=observed,
        ), patch("packages.application.finance_storage_backup_rotation.backup_rotation_health", return_value=health):
            for difference, allowed in ((0, True), (-1, False)):
                available = floor + peak + difference
                with patch.object(policy.os, "statvfs", return_value=SimpleNamespace(f_bavail=available, f_frsize=1)):
                    try:
                        receipt = policy.admit_root_write(
                            owner=owner, destination=destination, predicted_output_bytes=policy.MIB,
                            predicted_temporary_bytes=policy.MIB, predicted_readback_bytes=policy.MIB,
                            control_reserve_bytes=policy.MIB, policy=offline,
                        )
                    except policy.RootStoragePolicyError as exc:
                        assert not allowed and "backup_predicted_free_after_below_reserve" in str(exc)
                    else:
                        assert allowed and receipt["allowed"]
                        assert receipt["predicted_peak_bytes"] == peak
                        assert receipt["required_reserve_bytes"] == floor
                        assert receipt["predicted_free_after_bytes"] == floor
                        assert receipt["destination_role"] == "backup"
            try:
                policy.admit_root_write(
                    owner=owner, destination=Path(directory) / "foreign.sqlite3",
                    predicted_output_bytes=1, policy=offline,
                )
            except policy.RootStoragePolicyError as exc:
                assert "bypasses canonical storage registry" in str(exc)
            else:
                raise AssertionError("autoanswers backup accepted a foreign destination")


def _warehouse_placement_transition(loaded: dict) -> None:
    with TemporaryDirectory() as directory:
        base = Path(directory)
        changed = deepcopy(loaded)
        changed.pop("warehouse_recovery_placement_state", None)
        recovery = base / "backups" / "warehouse-recovery"
        recovery.mkdir(parents=True)
        config = changed["warehouse_recovery_placement"]
        config["artifact_root"] = str(recovery)
        config["outside_marker_path"] = str(recovery.parent / ".warehouse-recovery-extra100-active.json")
        changed["storage_registry"]["filesystems"]["warehouse_backup"]["path"] = str(recovery)
        policy_path = base / "policy.json"
        policy_path.write_text(json.dumps(changed), encoding="utf-8")
        legacy = policy.load_policy(policy_path)
        assert legacy["warehouse_recovery_placement_state"]["activated"] is False
        marker = {
            "contract_version": config["contract_version"],
            "artifact_root": str(recovery),
            "filesystem_uuid": config["filesystem_uuid"],
            "role": "warehouse_backup",
        }
        inside = recovery / config["inside_marker_name"]
        outside = Path(config["outside_marker_path"])
        inside.write_text(json.dumps(marker), encoding="utf-8")
        try:
            policy.load_policy(policy_path)
        except policy.RootStoragePolicyError:
            pass
        else:
            raise AssertionError("one placement marker was accepted")
        outside.write_text(json.dumps(marker), encoding="utf-8")
        active = policy.load_policy(policy_path)
        assert active["warehouse_recovery_placement_state"]["activated"] is True
        assert active["storage_registry"]["filesystems"]["warehouse_backup"]["active"] is True
        assert policy.storage_producer_policy(
            "warehouse_recovery_policy", policy=active
        )["destination_role"] == "warehouse_backup"
        assert policy.storage_producer_policy(
            "warehouse_functional_sync_artifacts", policy=active
        )["destination_role"] == "backup"
        assert policy.storage_destination_root("warehouse_recovery_policy", policy=active) == recovery.resolve()
        role_contract = active["storage_registry"]["filesystems"]["warehouse_backup"]
        block = SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=2096)
        with patch.object(policy.os, "stat", return_value=block):
            assert policy._storage_source_matches("/dev/sdd", role_contract["source"])
            assert policy._storage_source_matches("/dev/sde", role_contract["source"])
            assert not policy._storage_source_matches("/dev/sdc1", "/dev/sdb1")
        with patch.object(policy.os, "stat", side_effect=[block, SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=2097)]):
            assert not policy._storage_source_matches("/dev/sdd", role_contract["source"])
        with patch.object(policy.os, "stat", return_value=SimpleNamespace(st_mode=stat.S_IFREG, st_rdev=2096)):
            assert not policy._storage_source_matches("/dev/sdd", role_contract["source"])
        observed = {
            **role_contract,
            "path": str(recovery),
            "device": 77,
            "mount_options": "rw,noatime,nodev,nosuid,noexec",
            "available_bytes": 20 * policy.GIB,
        }
        with patch.object(policy, "_finance_backup_floor", return_value={
            "required_reserve_bytes": 10 * policy.GIB,
        }), patch.object(policy, "_scan_unregistered_large_destinations", return_value=[]):
            status = policy._collect_storage_registry_status(
                active, observed_filesystems={"warehouse_backup": observed}
            )
            assert status["roles"]["warehouse_backup"]["identity_ok"] is True
            wrong = {**observed, "filesystem_uuid": "wrong"}
            status = policy._collect_storage_registry_status(
                active, observed_filesystems={"warehouse_backup": wrong}
            )
            assert status["roles"]["warehouse_backup"]["identity_ok"] is False
            assert any(alert["code"] == "storage_filesystem_identity_violation" for alert in status["alerts"])
        outside.unlink()
        try:
            policy.load_policy(policy_path)
        except policy.RootStoragePolicyError:
            pass
        else:
            raise AssertionError("lost outside marker fell back to legacy")


def _warehouse_native_writer_mount_guard(loaded: dict) -> None:
    runtime = Path("/opt/wb-core-runtime/state")
    registry = WarehouseRecoveryRegistry(
        runtime_dir=runtime,
        db_path=runtime / "operational.sqlite3",
    )
    active = deepcopy(loaded)
    active["warehouse_recovery_placement_state"]["activated"] = True
    active["storage_registry"]["filesystems"]["warehouse_backup"]["active"] = True
    with patch.object(policy, "load_policy", return_value=active), patch.object(
        policy, "_assert_filesystem_identity",
        return_value={"mount_point": "/opt/wb-core-runtime/state/backups"},
    ), patch.object(Path, "is_dir", return_value=True):
        try:
            registry._assert_recovery_placement()
        except RecoveryPolicyError:
            pass
        else:
            raise AssertionError("writer accepted a missing warehouse bind")
    with patch.object(policy, "load_policy", return_value=active), patch.object(
        policy, "_assert_filesystem_identity",
        return_value={"mount_point": str(registry.recovery_root)},
    ), patch.object(Path, "is_dir", return_value=True):
        registry._assert_recovery_placement()


def main() -> int:
    loaded = policy.load_policy()
    assert set(loaded["storage_registry"]["filesystems"]) == {
        "root", "backup", "generation", "warehouse_backup"
    }
    assert loaded["storage_registry"]["filesystems"]["warehouse_backup"]["active"] is False
    assert loaded["warehouse_recovery_placement_state"]["activated"] is False
    parser = app.build_parser()
    assert parser.parse_args(["status"]).command == "status"
    assert parser.parse_args(["status-readback"]).command == "status-readback"
    assert policy.storage_level(11 * policy.GIB) == "hard"
    assert policy.storage_level(30 * policy.GIB) == "normal"
    _backup_reserve_boundary(loaded)
    _autoanswers_v11_backup_admission(loaded)
    _warehouse_placement_transition(loaded)
    _warehouse_native_writer_mount_guard(loaded)
    print("root_storage_policy_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
