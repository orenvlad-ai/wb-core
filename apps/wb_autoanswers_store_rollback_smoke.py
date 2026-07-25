#!/usr/bin/env python3
"""Deterministic fixture acceptance for the isolated-store rollback path."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.wb_autoanswers_store_rollback import (  # noqa: E402
    apply_rollback_export,
    build_plan,
)
from packages.application.wb_autoanswers_runtime import (  # noqa: E402
    AUTOANSWERS_DB_FILENAME,
    LEGACY_RUNTIME_DB_FILENAME,
    AutoanswersRepository,
)


def main() -> None:
    with TemporaryDirectory() as temp:
        runtime_dir = Path(temp)
        repository = AutoanswersRepository(runtime_dir=runtime_dir, env={})
        isolated = runtime_dir / AUTOANSWERS_DB_FILENAME
        legacy = runtime_dir / LEGACY_RUNTIME_DB_FILENAME
        with sqlite3.connect(isolated) as source, sqlite3.connect(legacy) as target:
            source.backup(target)
        with sqlite3.connect(legacy) as conn:
            conn.execute(
                "CREATE TABLE registry_non_target_sentinel(value TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO registry_non_target_sentinel VALUES('preserved')"
            )

        repository.update_settings(
            master_enabled=True,
            mode="manual",
            actor_id="rollback-smoke",
        )
        plan = build_plan(runtime_dir)
        if (
            not plan["capacity_ok"]
            or "sheet_vitrina_v1_wb_autoanswers_settings"
            not in plan["changed_tables"]
        ):
            raise AssertionError(f"rollback plan changed: {plan}")

        with patch.dict(
            os.environ,
            {
                "WB_AUTOANSWERS_FORCE_OFF": "true",
                "WB_AUTOANSWERS_DEPLOY_SERVICE_QUIESCE": "true",
            },
            clear=False,
        ):
            result = apply_rollback_export(
                runtime_dir,
                expected_fingerprint=str(plan["fingerprint"]),
            )
        if (
            result["status"] != "applied"
            or not result["source_preserved"]
            or result["readback"]["foreign_key_check_rows"] != 0
            or result["backup"]["integrity_check"] != "ok"
        ):
            raise AssertionError(f"rollback export changed: {result}")
        with sqlite3.connect(legacy) as conn:
            sentinel = conn.execute(
                "SELECT value FROM registry_non_target_sentinel"
            ).fetchone()
            settings = conn.execute(
                """
                SELECT master_enabled, mode
                FROM sheet_vitrina_v1_wb_autoanswers_settings
                WHERE singleton=1
                """
            ).fetchone()
        if sentinel != ("preserved",) or settings != (1, "manual"):
            raise AssertionError(
                f"rollback non-target/source readback changed: {sentinel} {settings}"
            )
        repeated_plan = build_plan(runtime_dir)
        if repeated_plan["changed_tables"]:
            raise AssertionError(
                f"rollback export must reconcile exact source: {repeated_plan}"
            )
    print("wb_autoanswers_store_rollback_smoke: OK")


if __name__ == "__main__":
    main()
