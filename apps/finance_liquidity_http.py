#!/usr/bin/env python3
"""Explicit-only Finance sidecar launcher; it never runs from main startup."""

from __future__ import annotations
import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.finance_liquidity_auth import (
    FinanceOperationalAuth,
    FixtureFinanceAuth,
)
from packages.adapters.finance_liquidity_access import (
    FinanceBootstrapAccessUnavailable,
    load_finance_bootstrap_access,
    validate_finance_bootstrap_store,
)
from packages.adapters.finance_liquidity_http import (
    FinanceHttpApp,
    build_finance_http_server,
)
from packages.application.finance_liquidity_cash import (
    FinanceCashService,
    bootstrap_finance_cash_store,
    migrate_finance_cash_store_v2,
)
from packages.contracts.finance_liquidity_cash import FINANCE_CASH_DEFAULT_PORT


def _loopback(host: str) -> bool:
    return host.strip().lower() in {"127.0.0.1", "localhost", "::1"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument("--migrate-v2", action="store_true")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=FINANCE_CASH_DEFAULT_PORT)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--auth-fixture", type=Path)
    args = parser.parse_args()
    if args.migrate_v2 and (args.bootstrap or args.backup is None):
        raise SystemExit("--migrate-v2 requires --backup and excludes --bootstrap")
    if args.backup is not None and not args.migrate_v2:
        raise SystemExit("--backup is only valid with --migrate-v2")
    try:
        bootstrap_access = load_finance_bootstrap_access()
    except FinanceBootstrapAccessUnavailable as exc:
        raise SystemExit("Finance bootstrap access config unavailable") from exc
    if bootstrap_access is not None:
        try:
            validate_finance_bootstrap_store(
                args.db,
                bootstrap_access,
                require_existing=not args.bootstrap,
            )
        except FinanceBootstrapAccessUnavailable as exc:
            raise SystemExit(str(exc)) from exc
    if args.migrate_v2:
        migrate_finance_cash_store_v2(args.db, args.backup)
        return
    if args.bootstrap:
        bootstrap_finance_cash_store(args.db)
        return
    if os.environ.get("FINANCE_LIQUIDITY_ENABLED") != "1":
        raise SystemExit("FINANCE_LIQUIDITY_ENABLED=1 is required")
    read = os.environ.get("FINANCE_LIQUIDITY_READ_ENABLED") == "1"
    write = os.environ.get("FINANCE_LIQUIDITY_WRITE_ENABLED") == "1"
    if args.auth_fixture:
        # Fixture identity intentionally cannot become a production auth route.
        if os.environ.get("FINANCE_LIQUIDITY_TEST_FIXTURE") != "1" or not _loopback(
            args.host
        ):
            raise SystemExit(
                "--auth-fixture requires FINANCE_LIQUIDITY_TEST_FIXTURE=1 on a loopback host"
            )
        auth = FixtureFinanceAuth(args.auth_fixture)
        csrf_secret = "fixture-csrf"
    else:
        if args.runtime_dir is None:
            raise SystemExit("--runtime-dir is required without --auth-fixture")
        auth = FinanceOperationalAuth(args.runtime_dir, finance_store_path=args.db)
        csrf_secret = str(os.environ.get("WB_CORE_WEB_AUTH_SESSION_SECRET") or "")
    if not csrf_secret:
        raise SystemExit("CSRF secret unavailable")
    origin = str(
        os.environ.get("FINANCE_LIQUIDITY_ORIGIN") or f"http://{args.host}:{args.port}"
    )
    app = FinanceHttpApp(
        FinanceCashService(args.db),
        auth,
        read_enabled=read,
        write_enabled=write,
        csrf_secret=csrf_secret,
        static_dir=ROOT / "packages/adapters/finance_liquidity_static",
        allowed_origin=origin,
        business_runtime_dir=args.runtime_dir,
        instance_label=(bootstrap_access.instance_label if bootstrap_access else ""),
        store_id=(bootstrap_access.store_id if bootstrap_access else ""),
        store_mode=(bootstrap_access.mode if bootstrap_access else ""),
    )
    build_finance_http_server(args.host, args.port, app).serve_forever()


if __name__ == "__main__":
    main()
