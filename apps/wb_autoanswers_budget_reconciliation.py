#!/usr/bin/env python3
"""Plan, apply and read back Autoanswers provider-cost uncertainty holds."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.wb_autoanswers_runtime import AutoanswersRepository  # noqa: E402
from apps.wb_autoanswers_lifecycle import _schema_readback  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("dry-run", "apply", "readback"))
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--fingerprint", default="")
    parser.add_argument("--actor", default="repo_owned_cli")
    args = parser.parse_args()
    runtime_dir = args.runtime_dir.expanduser().resolve()
    if not _schema_readback(runtime_dir)["ready"]:
        raise RuntimeError(
            "Autoanswers schema preparation is required before budget reconciliation"
        )
    repository = AutoanswersRepository(runtime_dir=runtime_dir)
    if args.action == "dry-run":
        result = repository.budget_reconciliation_plan()
    elif args.action == "apply":
        if not str(args.fingerprint or "").strip():
            raise ValueError("apply requires --fingerprint")
        result = repository.apply_budget_reconciliation(
            expected_fingerprint=str(args.fingerprint),
            actor_id=str(args.actor),
        )
    else:
        result = {
            "status": "confirmed"
            if repository.budget_reconciliation_status()["confirmed"]
            else "unconfirmed",
            "readback": repository.budget_reconciliation_status(),
            "budget": repository.budget_status(),
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
