#!/usr/bin/env python3
"""Historical WBC0027 static-manifest wrapper; superseded and non-runnable."""

from __future__ import annotations

import argparse
import json


def blocked_receipt() -> dict[str, object]:
    return {
        "status": "blocked",
        "reason": "historical_superseded_non_runnable",
        "replacement_profile": "product-capital-qualified-economics",
        "legacy_manifest_reusable": False,
        "legacy_operation_reusable": False,
        "production_mutation_submit_count": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("dry-run", "apply", "readback", "reconcile")
    )
    parser.parse_args()
    print(json.dumps(blocked_receipt(), ensure_ascii=False, sort_keys=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
