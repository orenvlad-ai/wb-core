#!/usr/bin/env python3
"""Read-only exhaustive canonical source-anomaly preflight."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.canonical_cost_engine_backfill import (  # noqa: E402
    PROTECTED_TABLES,
    SOURCE_TABLES,
    _integrity_check,
    _legacy_digest,
    _sqlite_backup,
    _tables_digest,
)
from packages.application.canonical_cost_engine import (  # noqa: E402
    CUTOVER_DATE,
    CanonicalCostEngine,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--date-to", default=date.today().isoformat())
    return parser


def run(args: argparse.Namespace) -> dict[str, object]:
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(args.runtime_dir))
    source_db = runtime.db_path
    if not source_db.exists():
        raise ValueError("runtime SQLite database does not exist")
    source_inode = source_db.stat().st_ino
    integrity = _integrity_check(source_db)
    source_digest = _tables_digest(source_db, SOURCE_TABLES)
    protected_digest = _tables_digest(source_db, PROTECTED_TABLES)
    legacy_digest = _legacy_digest(source_db)
    with tempfile.TemporaryDirectory(prefix="canonical-cost-preflight-") as temp_dir:
        copy_runtime = RegistryUploadDbBackedRuntime(
            runtime_dir=Path(temp_dir) / "runtime"
        )
        copy_runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
        _sqlite_backup(source_db, copy_runtime.db_path)
        report = CanonicalCostEngine(runtime=copy_runtime).source_anomaly_preflight(
            date_to=args.date_to
        )
    if source_db.stat().st_ino != source_inode:
        raise ValueError("read-only preflight changed live SQLite inode")
    if source_digest != _tables_digest(source_db, SOURCE_TABLES):
        raise ValueError("read-only preflight changed authoritative source digest")
    if protected_digest != _tables_digest(source_db, PROTECTED_TABLES):
        raise ValueError("read-only preflight changed protected digest")
    if legacy_digest != _legacy_digest(source_db):
        raise ValueError("read-only preflight changed pre-cutover digest")
    return {
        **report,
        "scope": {"date_from": CUTOVER_DATE, "date_to": args.date_to},
        "integrity_check": integrity,
        "source_inode": source_inode,
        "source_digest": source_digest,
        "protected_non_target_digest": protected_digest,
        "legacy_pre_cutover_digest": legacy_digest,
        "production_mutation": False,
    }


def main(argv: list[str] | None = None) -> int:
    payload = run(build_parser().parse_args(argv))
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
