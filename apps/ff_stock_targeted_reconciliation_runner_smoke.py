"""Smoke-check the bounded CLI dry-run/apply/backup/reversal contract."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.ff_stock_targeted_reconciliation_smoke import (  # noqa: E402
    SOURCE_KEY,
    SUPPLY_ID,
    _save_target_supply,
    _setup,
)


RUNNER = ROOT / "apps" / "ff_stock_targeted_reconciliation.py"


def main() -> None:
    with TemporaryDirectory(prefix="ff-stock-targeted-runner-") as tmp:
        root = Path(tmp)
        runtime, _block = _setup(root, status_id=2)
        _save_target_supply(runtime, status_id=3)
        runtime_dir = runtime.runtime_dir
        backup_dir = root / "backups"
        count_before = runtime.count_ff_stock_operations()

        dry_run = _run(runtime_dir)
        _assert(dry_run["status"] == "dry_run", f"runner dry-run failed: {dry_run}")
        fingerprint = dry_run["preflight"]["fingerprint"]
        _assert(runtime.count_ff_stock_operations() == count_before, "runner dry-run must not change the ledger")
        _assert(not backup_dir.exists(), "runner dry-run must not create a backup")

        applied = _run(
            runtime_dir,
            "--apply",
            "--confirm-fingerprint",
            fingerprint,
            "--backup-dir",
            str(backup_dir),
        )
        _assert(applied["status"] == "applied" and applied["runtime_mutation_performed"], f"runner apply failed: {applied}")
        backup = Path(applied["backup"]["path"])
        _assert(backup.is_file() and applied["backup"]["sha256"], "runner apply must create verified SQLite backup")
        _assert(applied["backup"]["integrity_check"] == "ok", "runner backup must pass SQLite integrity_check")
        _assert(runtime.load_ff_stock_operation_by_source_key(SOURCE_KEY) is not None, "runner apply must create canonical debit")

        repeat = _run(
            runtime_dir,
            "--apply",
            "--confirm-fingerprint",
            fingerprint,
            "--backup-dir",
            str(backup_dir),
        )
        _assert(repeat["status"] == "already_applied", "runner repeated apply must be idempotent")
        _assert("backup" not in repeat, "idempotent runner apply must not create a needless backup")

        reversal_dry_run = _run(runtime_dir, "--reversal")
        reversal_fingerprint = reversal_dry_run["preflight"]["fingerprint"]
        reversed_report = _run(
            runtime_dir,
            "--reversal",
            "--apply",
            "--confirm-fingerprint",
            reversal_fingerprint,
            "--backup-dir",
            str(backup_dir),
        )
        _assert(reversed_report["status"] == "reversed", f"runner reversal failed: {reversed_report}")
        _assert(Path(reversed_report["backup"]["path"]).is_file(), "runner reversal must also create a backup")
        _assert(runtime.load_ff_stock_operation_by_source_key(SOURCE_KEY) is not None, "runner reversal must preserve original debit")

    print("ff_stock_targeted_reconciliation_runner_smoke: ok")


def _run(runtime_dir: Path, *extra: str) -> dict[str, object]:
    result = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--runtime-dir",
            str(runtime_dir),
            "--supply-id",
            SUPPLY_ID,
            "--created-by",
            "smoke",
            *extra,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"runner exited {result.returncode}: stdout={result.stdout} stderr={result.stderr}")
    return json.loads(result.stdout)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


if __name__ == "__main__":
    main()
