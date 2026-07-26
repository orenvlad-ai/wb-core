"""Guarded one-off backfill of proxy margin 3 rows in persisted ready snapshots."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import DB_FILENAME  # noqa: E402
from packages.application.sheet_vitrina_v1_proxy_margin_3_historical_backfill import (  # noqa: E402
    BackfillPreflight,
    ReadySnapshotInput,
    build_backfill_preflight,
)


BACKUP_SUBDIR = "sheet_vitrina_v1_proxy_margin_3_historical_backfill"


class BackfillExecutionError(RuntimeError):
    """A guarded apply condition failed before commit."""


def main() -> None:
    args = _parse_args()
    try:
        payload = run_backfill(
            runtime_dir=Path(args.runtime_dir).expanduser().resolve(),
            all_available=bool(args.all_available),
            apply=bool(args.apply),
            expected_fingerprint=args.expected_fingerprint,
        )
    except BackfillExecutionError as exc:
        payload = {
            "schema_version": "sheet_vitrina_v1_proxy_margin_3_historical_backfill_v1",
            "mode": "apply" if args.apply else "dry-run",
            "status": "blocked",
            "blocker": str(exc),
            "database_written": False,
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
    if payload.get("status") == "blocked":
        raise SystemExit(2)


def run_backfill(
    *,
    runtime_dir: Path,
    all_available: bool,
    apply: bool,
    expected_fingerprint: str | None,
    _test_fail_after_updates: int | None = None,
) -> dict[str, Any]:
    """Run full preflight and optionally atomically apply every planned update."""

    if not all_available:
        raise BackfillExecutionError("--all-available is mandatory; partial apply is forbidden")
    db_path = runtime_dir / DB_FILENAME
    if not db_path.is_file():
        raise BackfillExecutionError("runtime SQLite DB is missing")

    snapshots = _load_ready_snapshots_ro(db_path)
    preflight = build_backfill_preflight(snapshots)
    summary = {
        **preflight.summary(),
        "mode": "apply" if apply else "dry-run",
        "status": "blocked" if preflight.blockers else "success",
        "all_available": True,
        "database_written": False,
        "backup_path": None,
    }
    if apply:
        raise BackfillExecutionError(
            "legacy Proxy margin 3 mutation entrypoint is disabled; "
            "use the recovery-policy targeted economics publication"
        )
    return summary

    # Historical apply implementation remains below as migration evidence only.
    # The unconditional return above keeps every backup/write path unreachable.
    if preflight.blockers:
        return summary
    if not expected_fingerprint:
        raise BackfillExecutionError("--expected-fingerprint from a fresh dry-run is mandatory")
    if expected_fingerprint != preflight.expected_fingerprint:
        raise BackfillExecutionError(
            "expected fingerprint mismatch before backup; run a new full dry-run"
        )
    if not preflight.updates:
        return {
            **summary,
            "status": "success",
            "idempotent_noop": True,
            "post_apply_fingerprint": preflight.expected_fingerprint,
        }

    backup_path = _backup_and_verify(db_path, expected_fingerprint=preflight.expected_fingerprint)
    summary["backup_path"] = str(backup_path)

    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        transactional_snapshots = _load_ready_snapshots(conn)
        transactional_preflight = build_backfill_preflight(transactional_snapshots)
        _validate_transactional_preflight(
            initial=preflight,
            transactional=transactional_preflight,
            expected_fingerprint=expected_fingerprint,
        )

        update_count = 0
        for item in transactional_preflight.updates:
            cursor = conn.execute(
                """
                UPDATE sheet_vitrina_v1_ready_snapshots
                SET plan_json = ?
                WHERE bundle_version = ?
                  AND as_of_date = ?
                  AND plan_json = ?
                """,
                (
                    item.new_plan_json,
                    item.snapshot.bundle_version,
                    item.snapshot.as_of_date,
                    item.snapshot.plan_json,
                ),
            )
            if cursor.rowcount != 1:
                raise BackfillExecutionError(
                    "optimistic ready snapshot update conflict; transaction rolled back"
                )
            update_count += 1
            if _test_fail_after_updates is not None and update_count >= _test_fail_after_updates:
                raise BackfillExecutionError("injected transaction failure for rollback smoke")

        post_snapshots = _load_ready_snapshots(conn)
        post_preflight = build_backfill_preflight(post_snapshots)
        _validate_post_apply(
            before=transactional_preflight,
            after=post_preflight,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        **summary,
        "status": "success",
        "database_written": True,
        "backup_path": str(backup_path),
        "applied_snapshots": len(preflight.updates),
        "post_apply_fingerprint": post_preflight.expected_fingerprint,
        "post_apply": {
            "changed_snapshots": len(post_preflight.updates),
            "changed_rows": sum(item.changed_rows for item in post_preflight.transforms),
            "changed_cells": sum(item.changed_cells for item in post_preflight.transforms),
            "conflicts": len(post_preflight.blockers),
            "non_target_preserved": post_preflight.non_target_digest_before
            == post_preflight.non_target_digest_after,
        },
        "idempotent_noop": False,
    }


def _validate_transactional_preflight(
    *,
    initial: BackfillPreflight,
    transactional: BackfillPreflight,
    expected_fingerprint: str,
) -> None:
    if transactional.expected_fingerprint != expected_fingerprint:
        raise BackfillExecutionError(
            "expected fingerprint mismatch inside transaction; source changed after backup"
        )
    if transactional.snapshot_identity_digest != initial.snapshot_identity_digest:
        raise BackfillExecutionError("snapshot identity changed after dry-run; transaction rolled back")
    if transactional.blockers:
        raise BackfillExecutionError("transactional preflight found blockers; transaction rolled back")
    if transactional.non_target_digest_before != transactional.non_target_digest_after:
        raise BackfillExecutionError("non-target preservation preflight failed; transaction rolled back")


def _validate_post_apply(*, before: BackfillPreflight, after: BackfillPreflight) -> None:
    if after.blockers:
        raise BackfillExecutionError("post-apply precommit verification found conflicts")
    if after.updates:
        raise BackfillExecutionError("post-apply precommit verification is not idempotent")
    if after.snapshot_identity_digest != before.snapshot_identity_digest:
        raise BackfillExecutionError("snapshot identity changed during apply")
    if after.non_target_digest_before != before.non_target_digest_before:
        raise BackfillExecutionError("non-target content changed during apply")
    if after.non_target_digest_before != after.non_target_digest_after:
        raise BackfillExecutionError("post-apply non-target digest mismatch")


def _load_ready_snapshots_ro(db_path: Path) -> list[ReadySnapshotInput]:
    uri = f"file:{quote(str(db_path))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise BackfillExecutionError("read-only preflight failed to enable query_only")
        return _load_ready_snapshots(conn)
    finally:
        conn.close()


def _load_ready_snapshots(conn: sqlite3.Connection) -> list[ReadySnapshotInput]:
    rows = conn.execute(
        """
        SELECT bundle_version,
               activated_at,
               as_of_date,
               snapshot_id,
               plan_version,
               refreshed_at,
               plan_json
        FROM sheet_vitrina_v1_ready_snapshots
        ORDER BY bundle_version, as_of_date
        """
    ).fetchall()
    return [
        ReadySnapshotInput(
            bundle_version=str(row["bundle_version"]),
            activated_at=str(row["activated_at"]),
            as_of_date=str(row["as_of_date"]),
            snapshot_id=str(row["snapshot_id"]),
            plan_version=str(row["plan_version"]),
            refreshed_at=str(row["refreshed_at"]),
            plan_json=str(row["plan_json"]),
        )
        for row in rows
    ]


def _backup_and_verify(db_path: Path, *, expected_fingerprint: str) -> Path:
    backup_dir = db_path.parent / "backups" / BACKUP_SUBDIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_dir / f"{db_path.stem}__{stamp}__{expected_fingerprint[:12]}.sqlite3"

    source_uri = f"file:{quote(str(db_path))}?mode=ro"
    source = sqlite3.connect(source_uri, uri=True)
    target = sqlite3.connect(str(backup_path))
    try:
        source.execute("PRAGMA query_only=ON")
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()

    verify = sqlite3.connect(f"file:{quote(str(backup_path))}?mode=ro", uri=True)
    verify.row_factory = sqlite3.Row
    try:
        verify.execute("PRAGMA query_only=ON")
        integrity = str(verify.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise BackfillExecutionError(f"backup integrity_check failed: {integrity}")
        backup_fingerprint = build_backfill_preflight(_load_ready_snapshots(verify)).expected_fingerprint
        if backup_fingerprint != expected_fingerprint:
            raise BackfillExecutionError(
                "backup fingerprint differs from dry-run fingerprint; apply aborted"
            )
    finally:
        verify.close()
    return backup_path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument(
        "--all-available",
        action="store_true",
        required=True,
        help="Scan every persisted ready snapshot and every existing date column.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Read-only preflight (default).")
    mode.add_argument("--apply", action="store_true", help="Atomically apply the complete guarded update.")
    parser.add_argument("--expected-fingerprint")
    args = parser.parse_args()
    if args.apply and not args.expected_fingerprint:
        parser.error("--apply requires --expected-fingerprint from a fresh dry-run")
    if not args.apply:
        args.dry_run = True
    return args


if __name__ == "__main__":
    main()
