#!/usr/bin/env python3
"""Read root-storage health or admit one bounded write."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.root_storage_policy import (  # noqa: E402
    RootStoragePolicyError,
    admit_root_write,
    collect_root_storage_status,
    load_policy,
    read_root_storage_status_artifact,
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_json_atomic(path: Path, value: Any) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy-file", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser("status")
    status.add_argument("--output", type=Path)
    status.add_argument("--fail-on-unregistered", action="store_true")
    commands.add_parser("status-readback")

    admission = commands.add_parser("admission")
    admission.add_argument("--owner", required=True)
    admission.add_argument("--destination", type=Path, required=True)
    admission.add_argument("--predicted-output-bytes", type=int, required=True)
    admission.add_argument("--predicted-temporary-bytes", type=int, default=0)
    admission.add_argument("--predicted-readback-bytes", type=int, default=0)
    admission.add_argument("--control-reserve-bytes", type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        policy = load_policy(args.policy_file)
        if args.command == "status":
            result = collect_root_storage_status(policy=policy)
            if args.output:
                write_json_atomic(args.output, result)
            print(canonical_json(result))
            return 2 if args.fail_on_unregistered and result["unregistered_large_root_files"] else 0
        if args.command == "status-readback":
            result = read_root_storage_status_artifact(policy=policy)
            print(canonical_json(result))
            return 0 if result.get("ok") else 3
        if args.command == "admission":
            result = admit_root_write(
                owner=args.owner,
                destination=args.destination,
                predicted_output_bytes=args.predicted_output_bytes,
                predicted_temporary_bytes=args.predicted_temporary_bytes,
                predicted_readback_bytes=args.predicted_readback_bytes,
                control_reserve_bytes=args.control_reserve_bytes,
                policy=policy,
            )
            print(canonical_json(result))
            return 0
    except (RootStoragePolicyError, ValueError) as exc:
        print(canonical_json({"ok": False, "command": args.command, "error": str(exc)}))
        return 2
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
