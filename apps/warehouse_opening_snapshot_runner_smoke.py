#!/usr/bin/env python3
"""Regression smoke for exact opening-plan apply streamed through stdin."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.warehouse_stocks_smoke import _block, _seed_runtime  # noqa: E402


def main() -> None:
    with TemporaryDirectory(prefix="warehouse-opening-runner-smoke-") as temp_dir:
        root = Path(temp_dir)
        runtime = _seed_runtime(root / "runtime")
        plan = _block(runtime).build_opening_plan()
        plan_text = json.dumps(plan, ensure_ascii=False)
        command = [
            sys.executable,
            str(ROOT / "apps" / "warehouse_opening_snapshot.py"),
            "--runtime-dir",
            str(runtime.runtime_dir),
            "apply",
            "--plan-file",
            "/dev/stdin",
            "--fingerprint",
            str(plan["plan_fingerprint"]),
            "--backup-dir",
            str(root / "backups"),
        ]
        first = subprocess.run(
            command,
            input=plan_text,
            text=True,
            capture_output=True,
            cwd=ROOT,
            check=False,
        )
        _assert(first.returncode == 0, first.stderr or first.stdout)
        first_payload = json.loads(first.stdout)
        _assert(first_payload.get("status") == "ready", "streamed apply status")
        _assert(first_payload.get("idempotent") is False, "first streamed apply changes state")

        second = subprocess.run(
            command,
            input=plan_text,
            text=True,
            capture_output=True,
            cwd=ROOT,
            check=False,
        )
        _assert(second.returncode == 0, second.stderr or second.stdout)
        second_payload = json.loads(second.stdout)
        _assert(second_payload.get("idempotent") is True, "second streamed apply is idempotent")

        with sqlite3.connect(runtime.db_path) as conn:
            _assert(
                conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_cutovers").fetchone()[0] == 1,
                "one stored cutover",
            )
            _assert(
                conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_documents").fetchone()[0] == 6,
                "six stored documents",
            )
        _assert(
            first_payload["recovery_policy"]["tier"] == "T2"
            and first_payload["recovery_policy"]["lifecycle"] == "retained",
            "opening apply retains a domain checkpoint",
        )
        _assert(
            second_payload["recovery_policy"]["tier"] == "T0"
            and second_payload["recovery_policy"]["actual_bytes"] == 0,
            "idempotent retry is a zero-byte T0 no-op",
        )
    print("warehouse opening snapshot runner smoke: ok")


def _assert(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)


if __name__ == "__main__":
    main()
