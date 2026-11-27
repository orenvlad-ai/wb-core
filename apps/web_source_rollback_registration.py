#!/usr/bin/env python3
"""Verify an existing rollback image; --register publishes its exact metadata claim."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.application.web_source_rollback_registration import prepare_registration, write_registration


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--expected-image-sha256", required=True)
    parser.add_argument("--register", action="store_true", help="write owner-only sidecar after verification")
    args = parser.parse_args()
    receipt = json.loads(args.receipt.read_text())
    preview = receipt
    if (receipt.get("schema") != "wb-core.production-apply-receipt/v1"
        or receipt.get("adapter") != "web_source_publication_v1"
        or receipt.get("action") not in {"apply", "preview"}):
        raise ValueError("rollback registration requires the independent adapter receipt")
    registration = prepare_registration(image=args.image, runtime=args.runtime_dir, preview=preview,
                                        expected_image_sha256=args.expected_image_sha256)
    result = {"state": "verified_read_only", "operation_id": registration["operation_id"],
              "image_sha256": registration["image_sha256"], "image_bytes": registration["identity"]["size"]}
    if args.register:
        result["registration_path"] = str(write_registration(image=args.image, runtime=args.runtime_dir,
                                                             registration=registration))
        result["state"] = "registered"
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    from packages.procedure_journal import cli_scope

    with cli_scope("web_source_rollback_registration"):
        raise SystemExit(main())
