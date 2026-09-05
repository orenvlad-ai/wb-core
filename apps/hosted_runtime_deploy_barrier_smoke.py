#!/usr/bin/env python3
"""Regression checks for deploy-time timer preservation."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.business_data_maintenance import (  # noqa: E402
    FBS_SHADOW_TIMER_UNIT,
    POLICY_FILENAME,
    POLICY_SCHEMA_VERSION,
)
from apps.hosted_runtime_deploy_barrier import (  # noqa: E402
    DeployBarrierError,
    reconcile,
)


OTHER_UNIT = "wb-core-registry-http.service"


def _write_policy(runtime_dir: Path, *, desired: object) -> None:
    (runtime_dir / POLICY_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": POLICY_SCHEMA_VERSION,
                "revision": 61,
                "processes": {"fbs_shadow": {"desired": desired}},
            }
        ),
        encoding="utf-8",
    )


def _reconcile(runtime_dir: Path, *, mutate: bool) -> dict[str, object]:
    return reconcile(
        runtime_dir=runtime_dir,
        enable=[FBS_SHADOW_TIMER_UNIT, OTHER_UNIT],
        restart=[FBS_SHADOW_TIMER_UNIT, OTHER_UNIT],
        mutate=mutate,
    )


def main() -> None:
    with TemporaryDirectory() as directory:
        runtime_dir = Path(directory)
        _write_policy(runtime_dir, desired=False)
        with patch(
            "apps.hosted_runtime_deploy_barrier.barrier_status",
            return_value={"active": False},
        ):
            result = _reconcile(runtime_dir, mutate=False)
        assert result["preserved_owner_policy_timers"] == [FBS_SHADOW_TIMER_UNIT]
        assert result["enabled_units"] == [OTHER_UNIT]
        assert result["restarted_units"] == [OTHER_UNIT]

        calls: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch(
                "apps.hosted_runtime_deploy_barrier.barrier_status",
                return_value={"active": False},
            ),
            patch("apps.hosted_runtime_deploy_barrier.subprocess.run", side_effect=fake_run),
        ):
            _reconcile(runtime_dir, mutate=True)
        assert calls == [
            ["systemctl", "enable", OTHER_UNIT],
            ["systemctl", "restart", OTHER_UNIT],
        ]

        _write_policy(runtime_dir, desired=True)
        with patch(
            "apps.hosted_runtime_deploy_barrier.barrier_status",
            return_value={"active": False},
        ):
            enabled = _reconcile(runtime_dir, mutate=False)
        assert enabled["preserved_owner_policy_timers"] == []
        assert enabled["enabled_units"] == [FBS_SHADOW_TIMER_UNIT, OTHER_UNIT]

        _write_policy(runtime_dir, desired=None)
        with patch(
            "apps.hosted_runtime_deploy_barrier.barrier_status",
            return_value={"active": False},
        ):
            try:
                _reconcile(runtime_dir, mutate=False)
            except DeployBarrierError as exc:
                assert "FBS shadow owner policy is ambiguous" in str(exc)
            else:
                raise AssertionError("ambiguous FBS policy must block deploy")

    print("hosted_runtime_deploy_barrier_smoke: OK")


if __name__ == "__main__":
    main()
