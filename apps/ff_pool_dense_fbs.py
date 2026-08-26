#!/usr/bin/env python3
"""Query-only future Orenburg repair plan over the general dense FBS service."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.ff_pool_dense_fbs import DenseFbsService  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)


ORENBURG_FACILITY_ID = "fff_2579bb2741ed4ab23b11bb4c4183"
ORENBURG_TARGET_NM_IDS = (
    259466031,
    391660889,
    391661710,
    391662410,
    391662965,
    391663632,
    428849827,
    428854502,
    497413772,
    497415593,
    497416559,
    497416931,
)
ORENBURG_EXPECTED_EXISTING_NON_TARGET_FBS_ROWS = 21


def run(args: argparse.Namespace) -> int:
    runtime = RegistryUploadDbBackedRuntime(
        runtime_dir=Path(args.runtime_dir).resolve()
    )
    plan = DenseFbsService(
        db_path=runtime.db_path,
        runtime_dir=runtime.runtime_dir,
    ).build_zero_repair_plan(
        facility_id=ORENBURG_FACILITY_ID,
        nm_ids=ORENBURG_TARGET_NM_IDS,
        expected_existing_non_target_count=ORENBURG_EXPECTED_EXISTING_NON_TARGET_FBS_ROWS,
    )
    if args.output:
        _write_private(Path(args.output), plan)
    print(json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if plan.get("apply_allowed") else 2


def _write_private(path: Path, payload: dict[str, object]) -> None:
    output = path.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build the exact query-only future Orenburg dense-FBS zero repair plan; "
            "this command exposes no apply path."
        )
    )
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--output", default="")
    try:
        return run(parser.parse_args())
    except (OSError, RuntimeError, ValueError) as exc:
        print(
            json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
