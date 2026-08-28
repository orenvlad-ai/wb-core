#!/usr/bin/env python3
"""Run or read back the repo-owned Change Registry observer."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.change_registry_observer import (  # noqa: E402
    ChangeRegistryObserver,
    ChangeRegistryReadSurface,
    DEFAULT_ACCOUNT_SCOPE,
)


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        raise RuntimeError("runtime environment file is missing")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        normalized = value.strip()
        if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {"'", '"'}:
            normalized = normalized[1:-1]
        os.environ[key] = normalized


def _enabled() -> bool:
    return os.environ.get("CHANGE_REGISTRY_OBSERVER_ENABLED", "").strip().casefold() in {
        "1", "true", "yes", "on",
    }


def _config() -> tuple[str, str]:
    seller_id = os.environ.get("SELLER_PORTAL_CANONICAL_SUPPLIER_ID", "").strip()
    account_scope = os.environ.get("CHANGE_REGISTRY_ACCOUNT_SCOPE", DEFAULT_ACCOUNT_SCOPE).strip()
    if not seller_id:
        raise RuntimeError("SELLER_PORTAL_CANONICAL_SUPPLIER_ID is missing")
    if account_scope != DEFAULT_ACCOUNT_SCOPE:
        raise RuntimeError("CHANGE_REGISTRY_ACCOUNT_SCOPE differs from the repo-owned fixed scope")
    return seller_id, account_scope


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--env-file", default="")
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan")
    scan.add_argument("--trigger", choices=("scheduled", "manual", "activation"), required=True)
    scan.add_argument("--scheduled-slot", default="")
    scan.add_argument("--requested-by", default="systemd")
    commands.add_parser("status")
    return parser


def run(args: argparse.Namespace) -> dict[str, object]:
    if str(args.env_file or "").strip():
        _load_env_file(Path(str(args.env_file)).resolve())
    seller_id, account_scope = _config()
    runtime_dir = Path(str(args.runtime_dir)).resolve()
    if args.command == "status":
        payload = ChangeRegistryReadSurface(
            runtime_dir,
            seller_id=seller_id,
            account_scope=account_scope,
        ).overview(limit=20)
        payload["activation"] = {"enabled": _enabled()}
        return payload
    if not _enabled():
        raise RuntimeError("Change Registry observer is disabled by repo-owned activation flag")
    return ChangeRegistryObserver(
        runtime_dir,
        seller_id=seller_id,
        account_scope=account_scope,
    ).run(
        trigger_kind=args.trigger,
        requested_by=args.requested_by,
        scheduled_slot_value=args.scheduled_slot,
    )


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = run(args)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": type(exc).__name__,
                    "error": "Сканирование Реестра изменений не завершено.",
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
