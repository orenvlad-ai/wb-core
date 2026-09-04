#!/usr/bin/env python3
"""Small offline checks for the root-storage command surface."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import root_storage_policy as app  # noqa: E402
from packages.application import root_storage_policy as policy  # noqa: E402


def main() -> int:
    loaded = policy.load_policy()
    assert set(loaded["storage_registry"]["filesystems"]) == {
        "root", "backup", "generation"
    }
    parser = app.build_parser()
    assert parser.parse_args(["status"]).command == "status"
    assert parser.parse_args(["status-readback"]).command == "status-readback"
    assert policy.storage_level(11 * policy.GIB) == "hard"
    assert policy.storage_level(30 * policy.GIB) == "normal"
    print("root_storage_policy_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
