"""Durable, fail-closed placement switch for warehouse recovery artifacts.

The two markers deliberately straddle the bind mount: one remains on the old
backup filesystem and one lives inside the mounted recovery directory.  A
missing bind exposes the old marker, so it can never make the writer interpret
an activated system as the legacy topology.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


PLACEMENT_CONTRACT = "wb_core_warehouse_recovery_extra100_v1"
ROLE = "warehouse_backup"
EXTRA100_UUID = "9fcfe929-827e-4f81-b2ef-186fd794e671"


class WarehousePlacementError(RuntimeError):
    """The warehouse recovery placement is absent, ambiguous, or unsafe."""


def _marker(path: Path, expected: Mapping[str, Any]) -> bool:
    if path.is_symlink():
        raise WarehousePlacementError(f"warehouse placement marker is a symlink: {path}")
    if not path.exists():
        return False
    if not path.is_file():
        raise WarehousePlacementError(f"warehouse placement marker is not a file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WarehousePlacementError(f"warehouse placement marker is unreadable: {path}") from exc
    if payload != dict(expected):
        raise WarehousePlacementError(f"warehouse placement marker drift: {path}")
    return True


def placement_state(policy: Mapping[str, Any]) -> dict[str, Any]:
    """Read the deployment-persistent switch without writing or probing data.

    Legacy is possible only while neither marker nor a nested mount exists.
    Once the marker is submitted, both copies must be present and the separate
    mount identity is enforced by the storage status and native writer.
    """

    config = policy.get("warehouse_recovery_placement")
    if not isinstance(config, Mapping) or config.get("contract_version") != PLACEMENT_CONTRACT:
        raise WarehousePlacementError("warehouse placement contract is missing")
    root = Path(str(config.get("artifact_root") or ""))
    outside = Path(str(config.get("outside_marker_path") or ""))
    inside_name = str(config.get("inside_marker_name") or "")
    uuid = str(config.get("filesystem_uuid") or "")
    if (
        not root.is_absolute()
        or not outside.is_absolute()
        or outside.parent != root.parent
        or inside_name != ".warehouse-recovery-extra100-active.json"
        or uuid != EXTRA100_UUID
        or config.get("role") != ROLE
        or root.is_symlink()
    ):
        raise WarehousePlacementError("warehouse placement configuration is invalid")
    expected = {
        "contract_version": PLACEMENT_CONTRACT,
        "artifact_root": str(root),
        "filesystem_uuid": uuid,
        "role": ROLE,
    }
    inside = root / inside_name
    outside_present = _marker(outside, expected)
    inside_present = _marker(inside, expected)
    nested_mount = (
        root.is_dir()
        and root.parent.is_dir()
        and root.stat().st_dev != root.parent.stat().st_dev
    )
    activated = outside_present or inside_present or nested_mount
    if activated and not (outside_present and inside_present):
        raise WarehousePlacementError(
            "warehouse placement activation markers are incomplete"
        )
    return {
        "activated": activated,
        "nested_mount": nested_mount,
        "artifact_root": str(root),
        "outside_marker_path": str(outside),
        "inside_marker_path": str(inside),
        "filesystem_uuid": uuid,
        "marker_payload": expected,
    }
