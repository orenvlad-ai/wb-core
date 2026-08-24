#!/usr/bin/env python3
"""Run independent official WB FBS warehouse and stock readback."""

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
from packages.application.wb_fbs_warehouse_registry import (  # noqa: E402
    WbFbsWarehouseRegistry,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--env-file", default="")
    parser.add_argument("command", choices=("collect", "readback"))
    return parser


def run(args: argparse.Namespace) -> dict[str, object]:
    runtime_dir = Path(str(args.runtime_dir)).resolve()
    if str(args.env_file or "").strip():
        _load_env_file(Path(str(args.env_file)).resolve())
    db_path = StoreRegistry(runtime_dir).resolve("operational")
    registry = WbFbsWarehouseRegistry(db_path=db_path)
    return registry.collect() if args.command == "collect" else registry.read_model()


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
        if (
            len(normalized) >= 2
            and normalized[0] == normalized[-1]
            and normalized[0] in {"'", '"'}
        ):
            normalized = normalized[1:-1]
        os.environ[key] = normalized


def main() -> int:
    try:
        result = run(build_parser().parse_args())
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "error": " ".join(str(exc).split())[:1000]},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
