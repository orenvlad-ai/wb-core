#!/usr/bin/env python3
"""Preview or apply an offline isolated-TEST cash account retirement."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.finance_liquidity_access import load_finance_bootstrap_access
from packages.application.finance_liquidity_test_retirement import (
    apply_test_account_retirement,
    preview_test_account_retirement,
    readback_test_account_retirement,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preview", "apply", "readback"))
    parser.add_argument("--access-config", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--store-id", required=True)
    parser.add_argument("--account-id", action="append", required=True)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--fingerprint")
    parser.add_argument("--operation-id")
    parser.add_argument("--actor")
    parser.add_argument("--service-stopped", action="store_true")
    args = parser.parse_args()
    access = load_finance_bootstrap_access({"FINANCE_LIQUIDITY_ACCESS_CONFIG": str(args.access_config)})
    if access is None:
        parser.error("Enabled isolated TEST access binding required")
    account_ids = tuple(args.account_id)
    if args.action == "preview":
        result = preview_test_account_retirement(args.db, access, args.store_id, account_ids)
    elif args.action == "apply":
        if not all((args.backup, args.fingerprint, args.operation_id, args.actor)):
            parser.error("Apply requires backup, fingerprint, operation ID and actor")
        result = apply_test_account_retirement(
            args.db, access, args.store_id, account_ids, args.backup,
            args.fingerprint, args.operation_id, args.actor,
            service_stopped=args.service_stopped,
        )
    else:
        if not all((args.fingerprint, args.operation_id, args.actor)):
            parser.error("Readback requires fingerprint, operation ID and actor")
        result = readback_test_account_retirement(
            args.db, access, args.store_id, account_ids,
            args.fingerprint, args.operation_id, args.actor,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
