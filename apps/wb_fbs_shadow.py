#!/usr/bin/env python3
"""Run the dedicated official FBS shadow poll or query-only readiness report."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.storage_registry import StoreRegistry  # noqa: E402
from packages.application.wb_fbs_shadow_polling import (  # noqa: E402
    WbFbsShadowPollingService,
    build_readiness_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--env-file", default="")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("poll")
    commands.add_parser("readiness")
    return parser


def run(args: argparse.Namespace) -> dict[str, object]:
    runtime_dir = Path(str(args.runtime_dir)).resolve()
    if str(args.env_file or "").strip():
        _load_env_file(Path(str(args.env_file)).resolve())
    db_path = StoreRegistry(runtime_dir).resolve("operational")
    if args.command == "poll":
        return WbFbsShadowPollingService(
            runtime_dir=runtime_dir,
            db_path=db_path,
        ).poll_once()
    return build_readiness_report(db_path=db_path, runtime_dir=runtime_dir)


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        raise RuntimeError("runtime environment file is missing")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        normalized = value.strip()
        if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {"'", '"'}:
            normalized = normalized[1:-1]
        os.environ[key] = normalized


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = run(args)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)[:1000]}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
