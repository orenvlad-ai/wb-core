"""Backfill trade document metadata and default supplier fields."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import DEFAULT_RUNTIME_DIR  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.supplier_shipments import SupplierShipmentsBlock  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill trade document supplier and contract metadata fields.")
    parser.add_argument("--runtime-dir", default=str(DEFAULT_RUNTIME_DIR), help="WebCore runtime dir with registry_upload.db")
    parser.add_argument("--apply", action="store_true", help="Actually update missing trade document fields")
    parser.add_argument("--include-archived", action="store_true", help="Also scan archived document rows")
    args = parser.parse_args()

    runtime_dir = Path(args.runtime_dir).expanduser()
    if not args.apply:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "dry_run_only_without_apply",
                    "runtime_dir": str(runtime_dir),
                    "apply_command": (
                        "python3 apps/sheet_vitrina_v1_trade_documents_backfill.py "
                        f"--runtime-dir {runtime_dir} --apply"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    block = SupplierShipmentsBlock(runtime=runtime)
    result = block.backfill_trade_document_metadata(include_archived=args.include_archived)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
