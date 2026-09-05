#!/usr/bin/env python3
"""Build the query-only FBS lifecycle last-good cache for Web Vitrina."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.web_vitrina_fbs_lifecycle_last_good import (  # noqa: E402
    build_and_publish_cache,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            build_and_publish_cache(args.runtime_dir),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
