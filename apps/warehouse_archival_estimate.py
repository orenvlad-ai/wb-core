#!/usr/bin/env python3
"""Repo-owned guarded runner for the archival WB cost estimate correction."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.warehouse_archival_estimate import (  # noqa: E402
    DEFAULT_MANIFEST_PATH,
    apply_archival_estimate_plan,
    build_archival_estimate_plan,
    readback_archival_estimate,
    rollback_archival_estimate,
)
from packages.application.warehouse_functional_lock import (  # noqa: E402
    warehouse_functional_write_lock,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("dry-run", "apply", "readback", "rollback"),
    )
    parser.add_argument(
        "--runtime-dir",
        default=os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR", ".runtime/registry_upload"),
    )
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH))
    parser.add_argument("--plan-file", default="")
    parser.add_argument("--fingerprint", default="")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--backup-dir", default="")
    args = parser.parse_args(argv)

    runtime = RegistryUploadDbBackedRuntime(Path(args.runtime_dir))
    manifest_path = Path(args.manifest).resolve()
    if args.command == "dry-run":
        result = build_archival_estimate_plan(runtime, manifest_path=manifest_path)
    elif args.command == "readback":
        result = readback_archival_estimate(runtime)
    elif args.command == "apply":
        if not args.plan_file:
            parser.error("apply requires --plan-file")
        if not args.fingerprint:
            parser.error("apply requires --fingerprint")
        if not args.approval_reference:
            parser.error("apply requires --approval-reference")
        if not args.backup_dir:
            parser.error("apply requires --backup-dir")
        plan = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
        with warehouse_functional_write_lock(runtime.runtime_dir):
            result = apply_archival_estimate_plan(
                runtime,
                plan,
                confirm_fingerprint=args.fingerprint,
                approval_reference=args.approval_reference,
                backup_dir=Path(args.backup_dir).resolve(),
                manifest_path=manifest_path,
            )
    else:
        if not args.fingerprint:
            parser.error("rollback requires --fingerprint")
        if not args.reason:
            parser.error("rollback requires --reason")
        if not args.backup_dir:
            parser.error("rollback requires --backup-dir")
        with warehouse_functional_write_lock(runtime.runtime_dir):
            result = rollback_archival_estimate(
                runtime,
                plan_fingerprint=args.fingerprint,
                reason=args.reason,
                backup_dir=Path(args.backup_dir).resolve(),
            )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
