#!/usr/bin/env python3
"""System-owned daily due-check for the approved Finance backup policy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.finance_storage_backup_rotation import (
    scheduled_rotation,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--deployed-sha-file", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    deployed_sha = args.deployed_sha_file.read_text(encoding="utf-8").strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", deployed_sha) is None:
        raise SystemExit("Finance backup scheduler requires an exact deployed SHA")
    payload = scheduled_rotation(
        args.runtime_dir.expanduser().resolve(), deployed_sha=deployed_sha
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
