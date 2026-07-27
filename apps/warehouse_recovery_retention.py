#!/usr/bin/env python3
"""Plan or apply bounded warehouse recovery retention on one exact runtime."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    DB_FILENAME,
)
from packages.application.warehouse_functional_lock import (  # noqa: E402
    warehouse_functional_write_lock,
)
from packages.application.warehouse_recovery_policy import (  # noqa: E402
    WarehouseRecoveryRegistry,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("dry-run", "apply", "status"))
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--fingerprint", default="")
    return parser


def run(args: argparse.Namespace) -> dict[str, object]:
    runtime_dir = Path(str(args.runtime_dir)).resolve()
    deployed_sha = str(args.deployed_sha or "").strip()
    if len(deployed_sha) != 40 or any(
        char not in "0123456789abcdef" for char in deployed_sha.lower()
    ):
        raise ValueError("retention requires an exact 40-hex deployed SHA")
    marker = (ROOT / ".wb-core-runtime-sha").resolve()
    if not marker.is_file():
        raise ValueError("deployed runtime marker is unavailable")
    actual_sha = marker.read_text(encoding="utf-8").strip()
    if actual_sha != deployed_sha:
        raise ValueError(
            "retention deployed SHA mismatch: "
            f"expected={deployed_sha}, actual={actual_sha or '<missing>'}"
        )
    registry = WarehouseRecoveryRegistry(
        runtime_dir=runtime_dir,
        db_path=runtime_dir / DB_FILENAME,
    )
    if args.mode == "status":
        return {
            "contract_name": "warehouse_recovery_retention_runner_v1",
            "mode": "status",
            "deployed_sha": deployed_sha,
            "status": registry.public_status(),
        }
    if args.mode == "dry-run":
        return {
            **registry.plan_retention(),
            "runner_contract": "warehouse_recovery_retention_runner_v1",
            "mode": "dry-run",
            "deployed_sha": deployed_sha,
        }
    fingerprint = str(args.fingerprint or "").strip()
    if not fingerprint:
        raise ValueError("retention apply requires the exact dry-run fingerprint")
    with warehouse_functional_write_lock(runtime_dir):
        result = registry.apply_retention(plan_fingerprint=fingerprint)
    return {
        **result,
        "runner_contract": "warehouse_recovery_retention_runner_v1",
        "mode": "apply",
        "deployed_sha": deployed_sha,
    }


def main() -> int:
    try:
        result = run(build_parser().parse_args())
    except Exception as exc:
        print(
            json.dumps(
                {"status": "error", "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if str(result.get("status") or "") != "partial_failure" else 2


if __name__ == "__main__":
    raise SystemExit(main())
