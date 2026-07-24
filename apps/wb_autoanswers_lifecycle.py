#!/usr/bin/env python3
"""Reconcile feature-owned Autoanswers intent with its two systemd timers."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.business_data_maintenance import POLICY_FILENAME  # noqa: E402
from packages.application.wb_autoanswers_lifecycle import (  # noqa: E402
    AutoanswersLifecycle,
)
from packages.application.wb_autoanswers_runtime import (  # noqa: E402
    AutoanswersRepository,
    SCHEMA_VERSION,
)


def _schema_readback(runtime_dir: Path) -> dict[str, Any]:
    database = runtime_dir / "registry_upload_runtime.sqlite3"
    if not database.is_file():
        return {"ready": False, "database_exists": False, "versions": []}
    try:
        with sqlite3.connect(
            f"file:{database.resolve()}?mode=ro",
            uri=True,
            timeout=10,
        ) as conn:
            conn.execute("PRAGMA query_only=ON")
            table = conn.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type='table'
                  AND name='sheet_vitrina_v1_wb_autoanswers_schema_migrations'
                """
            ).fetchone()
            versions = (
                [
                    int(row[0])
                    for row in conn.execute(
                        """
                        SELECT version
                        FROM sheet_vitrina_v1_wb_autoanswers_schema_migrations
                        ORDER BY version
                        """
                    ).fetchall()
                ]
                if table
                else []
            )
    except sqlite3.Error as exc:
        return {
            "ready": False,
            "database_exists": True,
            "versions": [],
            "error": str(exc),
        }
    return {
        "ready": SCHEMA_VERSION in versions,
        "database_exists": True,
        "versions": versions,
    }


def _master_suspended(runtime_dir: Path) -> tuple[bool, dict[str, Any]]:
    path = runtime_dir / POLICY_FILENAME
    if not path.is_file():
        raise RuntimeError("global auto-updates owner policy is not confirmed")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping) or "master_desired" not in value:
        raise RuntimeError("global auto-updates owner policy is incomplete")
    return not bool(value.get("master_desired")), {
        "revision": int(value.get("revision") or 0),
        "master_desired": bool(value.get("master_desired")),
    }


def run(
    *,
    action: str,
    runtime_dir: Path,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    schema = _schema_readback(runtime_dir)
    if not schema["ready"]:
        if action != "status":
            raise RuntimeError(
                "Autoanswers schema preparation is required before lifecycle mutation"
            )
        return {
            "status": "schema_preparation_required",
            "action": action,
            "schema": schema,
            "master_policy": {"confirmed": False},
            "lifecycle": {
                "process_key": "autoanswers",
                "display_name": "Autoanswers",
                "control_owner": "feature",
                "control_location": "Отзывы → Отзывы",
                "control_capability": "monitor",
                "desired_source": "autoanswers_feature_settings",
                "desired": None,
                "actual": False,
                "lifecycle_state": "unconfirmed",
                "drift_status": "unknown",
                "suspended_by_master": None,
                "last_error": "schema_preparation_required",
                "components": {},
                "component_states": {},
            },
            "settings": {},
            "reconciliation": None,
        }
    repository = AutoanswersRepository(runtime_dir=runtime_dir)
    lifecycle = AutoanswersLifecycle(
        runtime_dir=runtime_dir,
        repository=repository,
    )
    if action == "suspend":
        suspended = True
        master = {"master_desired": False, "source": "explicit_cross_writer_hold"}
    else:
        suspended, master = _master_suspended(runtime_dir)
    if action == "status":
        readback = lifecycle.status(suspended_by_master=suspended)
    elif action in {"reconcile", "suspend"}:
        sweep = repository.reconciliation_status() or {}
        readback = lifecycle.reconcile(
            suspended_by_master=suspended,
            actor=actor,
            reason=reason,
            transition_run_id=str(sweep.get("transition_run_id") or "") or None,
        )
    else:
        raise ValueError(f"unsupported lifecycle action: {action}")
    return {
        "status": str(readback.get("lifecycle_state") or "unconfirmed"),
        "action": action,
        "master_policy": master,
        "lifecycle": readback,
        "settings": {
            "mode": repository.settings().mode,
            "master_enabled": repository.settings().master_enabled,
            "policy_epoch": repository.settings().policy_epoch,
        },
        "reconciliation": repository.reconciliation_status(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "reconcile", "suspend"))
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--actor", default="repo_owned_cli")
    parser.add_argument("--reason", default="canonical lifecycle reconciliation")
    args = parser.parse_args()
    result = run(
        action=str(args.action),
        runtime_dir=args.runtime_dir.expanduser().resolve(),
        actor=str(args.actor),
        reason=str(args.reason),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
