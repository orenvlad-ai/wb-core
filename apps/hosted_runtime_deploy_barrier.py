#!/usr/bin/env python3
"""Apply managed systemd enable/restart without reopening an exact WBC0027 freeze."""

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

from apps.sqlite_hot_journal_recovery import (  # noqa: E402
    EXPECTED_PARTIAL_EPOCH_SCHEMA,
    RECOVERY_PAUSE_OWNED_TIMERS,
)
from packages.application.business_data_write_barrier import (  # noqa: E402
    barrier_status,
)


SERVICE_BY_TIMER = {
    "wb-core-finance-backup-rotation.timer":
        "wb-core-finance-backup-rotation.service",
    "wb-core-fbs-warehouse-registry.timer":
        "wb-core-fbs-warehouse-registry.service",
    "wb-core-sheet-vitrina-canary-restore.timer":
        "wb-core-sheet-vitrina-canary-restore.service",
    "wb-core-sheet-vitrina-health-candidate.timer":
        "wb-core-sheet-vitrina-health-candidate.service",
    "wb-core-sheet-vitrina-health-confirmation.timer":
        "wb-core-sheet-vitrina-health-confirmation.service",
    "wb-core-fbs-shadow-collector.timer": "wb-core-fbs-shadow-collector.service",
}
QUIESCENT = {"inactive", "failed"}
ACTIVE_BARRIER_ENABLE_UNITS = frozenset(
    {
        *RECOVERY_PAUSE_OWNED_TIMERS,
        "wb-core-registry-http.service",
        "wb-ai-api.service",
        "wb-core-change-registry-observer.timer",
        "wb-core-root-storage-policy.timer",
        "wb-core-data-mcp.service",
    }
)
ACTIVE_BARRIER_RESTART_UNITS = frozenset(
    {
        *RECOVERY_PAUSE_OWNED_TIMERS,
        "wb-ai-api.service",
        "wb-core-change-registry-observer.timer",
        "wb-core-root-storage-policy.service",
        "wb-core-root-storage-policy.timer",
        "wb-core-data-mcp.service",
    }
)
RECOVERY_SCRATCH_BRIDGE_SKIPPED_RESTART_UNIT = (
    "wb-core-root-storage-policy.service"
)


class DeployBarrierError(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise DeployBarrierError(f"required state is unavailable: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeployBarrierError(f"required state is invalid: {path}") from exc
    if not isinstance(value, dict):
        raise DeployBarrierError(f"required state is not an object: {path}")
    return value


def _unit_state(unit: str) -> dict[str, str]:
    completed = subprocess.run(
        [
            "systemctl", "show", unit, "--no-pager",
            "--property=LoadState", "--property=UnitFileState",
            "--property=ActiveState", "--property=SubState",
            "--property=MainPID",
        ],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise DeployBarrierError(f"systemd readback failed for {unit}")
    result: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            result[key] = value
    if result.get("LoadState") != "loaded":
        raise DeployBarrierError(f"managed unit is not loaded: {unit}")
    return result


def _preserved_units(runtime_dir: Path) -> set[str]:
    barrier = barrier_status(runtime_dir)
    if barrier.get("active") is False:
        return set()
    if (
        barrier.get("active") is not True
        or barrier.get("phase") != "acquiring"
        or barrier.get("hold_confirmed") is not False
    ):
        raise DeployBarrierError("external barrier state is ambiguous")
    state = _read_json(runtime_dir / ".business-data-maintenance.json")
    epoch = dict(
        state.get("prepared_abort_partial_restore_recovery_epoch") or {}
    )
    exact = sorted(RECOVERY_PAUSE_OWNED_TIMERS)
    if (
        state.get("phase") != "abort_quiescing"
        or epoch.get("schema_version") != EXPECTED_PARTIAL_EPOCH_SCHEMA
        or int(epoch.get("epoch") or 0) != 2
        or epoch.get("window_id") != barrier.get("window_id")
        or epoch.get("plan_fingerprint") != barrier.get("plan_fingerprint")
        or epoch.get("barrier_state_fingerprint")
        != barrier.get("state_fingerprint")
        or sorted(epoch.get("timer_units_to_disable") or []) != exact
        or sorted(epoch.get("disabled_timer_units") or []) != exact
        or str(epoch.get("pending_disable_unit") or "")
    ):
        raise DeployBarrierError("partial recovery state does not bind the barrier")
    for timer in exact:
        timer_state = _unit_state(timer)
        service_state = _unit_state(SERVICE_BY_TIMER[timer])
        if (
            timer_state.get("UnitFileState") != "disabled"
            or timer_state.get("ActiveState") != "inactive"
            or service_state.get("ActiveState") not in QUIESCENT
            or int(service_state.get("MainPID") or 0) != 0
        ):
            raise DeployBarrierError(
                f"pause-owned unit is not quiescent before deploy: {timer}"
            )
    return set(exact)


def reconcile(
    *,
    runtime_dir: Path,
    enable: list[str],
    restart: list[str],
    mutate: bool = True,
    recovery_scratch_release_bridge: bool = False,
) -> dict[str, Any]:
    preserved = _preserved_units(runtime_dir.resolve())
    expected = set(RECOVERY_PAUSE_OWNED_TIMERS)
    if preserved and (
        set(enable) != set(ACTIVE_BARRIER_ENABLE_UNITS)
        or set(restart) != set(ACTIVE_BARRIER_RESTART_UNITS)
        or not expected <= set(enable)
        or not expected <= set(restart)
    ):
        raise DeployBarrierError("managed unit inventory cannot preserve exact freeze")
    filtered_enable = [unit for unit in enable if unit not in preserved]
    filtered_restart = [unit for unit in restart if unit not in preserved]
    if recovery_scratch_release_bridge:
        if (
            not preserved
            or RECOVERY_SCRATCH_BRIDGE_SKIPPED_RESTART_UNIT not in restart
        ):
            raise DeployBarrierError(
                "recovery scratch release bridge cannot narrow this restart"
            )
        filtered_restart = [
            unit
            for unit in filtered_restart
            if unit != RECOVERY_SCRATCH_BRIDGE_SKIPPED_RESTART_UNIT
        ]
    # Every ambiguity is resolved above.  No systemctl mutation occurs earlier.
    if mutate and filtered_enable:
        subprocess.run(
            ["systemctl", "enable", *filtered_enable], timeout=120, check=True
        )
    if mutate and filtered_restart:
        subprocess.run(
            ["systemctl", "restart", *filtered_restart], timeout=300, check=True
        )
    return {
        "status": "applied" if mutate else "validated",
        "preserved_pause_owned_units": sorted(preserved),
        "enabled_units": filtered_enable,
        "restarted_units": filtered_restart,
        "recovery_scratch_bridge_skipped_restart_units": (
            [RECOVERY_SCRATCH_BRIDGE_SKIPPED_RESTART_UNIT]
            if recovery_scratch_release_bridge
            else []
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--enable", action="append", default=[])
    parser.add_argument("--restart", action="append", default=[])
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument(
        "--recovery-scratch-release-bridge",
        action="store_true",
    )
    args = parser.parse_args()
    print(json.dumps(reconcile(
        runtime_dir=args.runtime_dir,
        enable=list(args.enable),
        restart=list(args.restart),
        mutate=not bool(args.preflight_only),
        recovery_scratch_release_bridge=bool(
            args.recovery_scratch_release_bridge
        ),
    ), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
