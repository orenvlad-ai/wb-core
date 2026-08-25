#!/usr/bin/env python3
"""Run one deterministic parallel group from an immutable test plan."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ci.test_planner import verify_plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--group", required=True)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    verify_plan(plan)
    if args.group not in plan["groups"]:
        raise ValueError(f"group is not selected by the test plan: {args.group}")
    suites = [
        (suite_id, suite)
        for suite_id, suite in plan["execution"].items()
        if suite["group"] == args.group
    ]
    if not suites:
        raise ValueError(f"selected group has no suites: {args.group}")
    for suite_id, suite in sorted(suites):
        for command in suite["commands"]:
            print(json.dumps({"suite": suite_id, "command": command}))
            subprocess.run(command, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
