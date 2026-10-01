#!/usr/bin/env python3
"""Preview or submit a bounded inverse of one promo archive publication."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.promo_archive_publication import rollback_apply, rollback_preview


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preview", "apply"))
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--expected-after-target-sha", default="")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = (rollback_preview(args.runtime_dir, args.operation_id) if args.action == "preview"
                  else rollback_apply(args.runtime_dir, args.operation_id, args.expected_after_target_sha))
        code = 0
    except Exception as exc:
        result = {"operation_id": args.operation_id, "state": "blocked", "reason": str(exc)}
        code = 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"operation_id": args.operation_id, "state": result.get("state", "preview"), "reason": result.get("reason")}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
