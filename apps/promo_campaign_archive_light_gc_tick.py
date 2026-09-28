#!/usr/bin/env python3
"""Hourly bounded Promo debug GC, independent of sheet-vitrina refresh."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.promo_campaign_archive_gc import run_promo_campaign_archive_light_gc  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    summary = run_promo_campaign_archive_light_gc(runtime_dir=args.runtime_dir)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return int(summary["status"] == "warning" and
               summary.get("warning") != "time_budget_exhausted_with_pending_batch")


if __name__ == "__main__":
    raise SystemExit(main())
