#!/usr/bin/env python3
"""Restart managed services without reopening an active data-write barrier."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.business_data_maintenance import (  # noqa: E402
    ALL_BUSINESS_TIMER_UNITS,
    FBS_SHADOW_TIMER_UNIT,
    POLICY_FILENAME,
    POLICY_SCHEMA_VERSION,
)
from packages.application.business_data_write_barrier import barrier_status  # noqa: E402


class DeployBarrierError(RuntimeError):
    pass


def unit_state(unit: str) -> dict[str, str]:
    completed = subprocess.run(
        [
            "systemctl", "show", unit, "--no-pager",
            "--property=LoadState", "--property=UnitFileState",
            "--property=ActiveState", "--property=MainPID",
        ],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise DeployBarrierError(f"systemd readback failed for {unit}")
    return dict(line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line)


def preserved_units(runtime_dir: Path, requested: set[str]) -> set[str]:
    barrier = barrier_status(runtime_dir.resolve())
    if barrier.get("active") is False:
        return set()
    if barrier.get("active") is not True:
        raise DeployBarrierError("data-write barrier state is ambiguous")
    preserved = requested & set(ALL_BUSINESS_TIMER_UNITS)
    for timer in sorted(preserved):
        state = unit_state(timer)
        if state.get("UnitFileState") != "disabled" or state.get("ActiveState") != "inactive":
            raise DeployBarrierError(f"protected timer is not quiescent: {timer}")
    return preserved


def owner_policy_preserved_units(runtime_dir: Path, requested: set[str]) -> set[str]:
    """Keep an explicitly disabled direct-control timer disabled on deploy."""

    policy_path = runtime_dir.resolve() / POLICY_FILENAME
    if not policy_path.is_file():
        return set()
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeployBarrierError("auto-updates owner policy is unreadable") from exc
    if not isinstance(policy, dict) or policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise DeployBarrierError("auto-updates owner policy schema is ambiguous")
    processes = policy.get("processes")
    if not isinstance(processes, dict):
        raise DeployBarrierError("auto-updates owner policy processes are ambiguous")
    fbs_shadow = processes.get("fbs_shadow")
    if fbs_shadow is None:
        return set()
    if not isinstance(fbs_shadow, dict) or not isinstance(fbs_shadow.get("desired"), bool):
        raise DeployBarrierError("FBS shadow owner policy is ambiguous")
    if fbs_shadow["desired"] is False and FBS_SHADOW_TIMER_UNIT in requested:
        return {FBS_SHADOW_TIMER_UNIT}
    return set()


def reconcile(
    *, runtime_dir: Path, enable: list[str], restart: list[str], mutate: bool = True
) -> dict[str, Any]:
    requested = set(enable) | set(restart)
    barrier_preserved = preserved_units(runtime_dir, requested)
    owner_policy_preserved = owner_policy_preserved_units(runtime_dir, requested)
    preserved = barrier_preserved | owner_policy_preserved
    filtered_enable = [unit for unit in enable if unit not in preserved]
    filtered_restart = [unit for unit in restart if unit not in preserved]
    if mutate and filtered_enable:
        subprocess.run(["systemctl", "enable", *filtered_enable], timeout=120, check=True)
    if mutate and filtered_restart:
        subprocess.run(["systemctl", "restart", *filtered_restart], timeout=300, check=True)
    return {
        "status": "applied" if mutate else "validated",
        "preserved_data_writer_timers": sorted(barrier_preserved),
        "preserved_owner_policy_timers": sorted(owner_policy_preserved),
        "enabled_units": filtered_enable,
        "restarted_units": filtered_restart,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--enable", action="append", default=[])
    parser.add_argument("--restart", action="append", default=[])
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    print(json.dumps(reconcile(
        runtime_dir=args.runtime_dir,
        enable=list(args.enable),
        restart=list(args.restart),
        mutate=not args.preflight_only,
    ), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
