#!/usr/bin/env python3
"""Small offline checks for the root-storage command surface."""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import root_storage_policy as app  # noqa: E402
from packages.application import root_storage_policy as policy  # noqa: E402


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


def main() -> int:
    loaded = policy.load_policy()
    assert set(loaded["storage_registry"]["filesystems"]) == {
        "root", "backup", "generation"
    }
    parser = app.build_parser()
    assert parser.parse_args(["status"]).command == "status"
    assert parser.parse_args(["status-readback"]).command == "status-readback"
    assert policy.storage_level(11 * policy.GIB) == "hard"
    assert policy.storage_level(30 * policy.GIB) == "normal"
    _backup_reserve_boundary(loaded)
    print("root_storage_policy_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
