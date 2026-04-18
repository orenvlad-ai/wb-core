"""Repo-owned live runner for sheet_vitrina_v1 closed-day retry / corrective re-closure."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import load_registry_upload_http_entrypoint_config
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Retry pending closed-day bot/web-source closures and optionally corrective re-close explicit dates.",
    )
    parser.add_argument(
        "--date",
        dest="dates",
        action="append",
        default=[],
        help="Explicit as_of_date / closed-day date to refresh and re-close (repeatable, YYYY-MM-DD).",
    )
    parser.add_argument(
        "--skip-auto-load-visible",
        action="store_true",
        help="Do not push the visible default sheet after retrying due/default dates.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_registry_upload_http_entrypoint_config()
    activated_at_override = os.environ.get("REGISTRY_UPLOAD_ACTIVATED_AT_OVERRIDE", "").strip()
    entrypoint = RegistryUploadHttpEntrypoint(
        runtime_dir=config.runtime_dir,
        activated_at_factory=((lambda: activated_at_override) if activated_at_override else None),
    )
    payload = entrypoint.run_sheet_temporal_closure_retry_cycle(
        target_dates=args.dates,
        auto_load_visible=not args.skip_auto_load_visible,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
