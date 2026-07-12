"""SQLite backup/fingerprint/inode/idempotency checks for canonical backfill."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import apps.canonical_cost_engine_backfill as backfill_module  # noqa: E402
from apps.canonical_cost_engine_backfill import run  # noqa: E402
from apps.canonical_cost_engine_smoke import (  # noqa: E402
    _insert_fallback_production,
    _insert_ff_balance,
    _insert_primary,
    _insert_snapshot,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
    _connect,
    _ensure_schema,
)


def main() -> int:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        runtime_dir = root / "runtime"
        backup_dir = root / "backups"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(runtime.db_path) as conn:
            _ensure_schema(conn)
            conn.execute("PRAGMA journal_mode=WAL")
            _insert_primary(conn)
            _insert_fallback_production(conn, nm_id=222)
            _insert_ff_balance(conn, nm_id=111, quantity=6750)
            _insert_snapshot(conn, "2026-05-16", {222: {"onec_FF_STOCK_unit_cost_rub": 80}})
            _insert_snapshot(conn, "2026-07-01", {111: {"stock_total": 93250}, 222: {"stock_total": 0}})
            conn.execute("CREATE TABLE non_target_fixture(value TEXT NOT NULL)")
            conn.execute("INSERT INTO non_target_fixture VALUES('preserve-me')")
            conn.commit()
        inode = runtime.db_path.stat().st_ino
        dry = run(_args(runtime_dir, backup_dir))
        if dry["mode"] != "dry-run" or dry["applied"]:
            raise AssertionError("default runner mode must be non-mutating dry-run")
        if not dry["would_change"]:
            raise AssertionError("first candidate must report target changes")
        with _connect(runtime.db_path) as conn:
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name LIKE 'sheet_vitrina_v1_canonical_cost_%'"
            ).fetchone():
                raise AssertionError("dry-run must not create target schema in live SQLite")
        try:
            run(_args(runtime_dir, backup_dir, apply=True, fingerprint="wrong"))
        except ValueError as exc:
            if "exact current dry-run fingerprint" not in str(exc):
                raise
        else:
            raise AssertionError("wrong fingerprint must fail before mutation")
        original_replace = backfill_module._replace_canonical_tables

        def fail_after_copy(conn, materialized):
            original_replace(conn, materialized)
            raise RuntimeError("synthetic transactional failure")

        backfill_module._replace_canonical_tables = fail_after_copy
        try:
            try:
                run(
                    _args(
                        runtime_dir,
                        backup_dir,
                        apply=True,
                        fingerprint=dry["fingerprint"],
                    )
                )
            except RuntimeError as exc:
                if "synthetic" not in str(exc):
                    raise
            else:
                raise AssertionError("synthetic apply failure unexpectedly committed")
        finally:
            backfill_module._replace_canonical_tables = original_replace
        with _connect(runtime.db_path) as conn:
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name LIKE 'sheet_vitrina_v1_canonical_cost_%'"
            ).fetchone():
                raise AssertionError("failed transaction leaked canonical target schema")
        original_rebuild = backfill_module.CanonicalCostEngine.rebuild

        def fail_live_post_verify(self, *args, **kwargs):
            if self.runtime.db_path == runtime.db_path:
                raise RuntimeError("synthetic post-commit verification failure")
            return original_rebuild(self, *args, **kwargs)

        backfill_module.CanonicalCostEngine.rebuild = fail_live_post_verify
        try:
            try:
                run(
                    _args(
                        runtime_dir,
                        backup_dir,
                        apply=True,
                        fingerprint=dry["fingerprint"],
                    )
                )
            except RuntimeError as exc:
                if "post-commit" not in str(exc):
                    raise
            else:
                raise AssertionError("post-commit verification failure did not abort")
        finally:
            backfill_module.CanonicalCostEngine.rebuild = original_rebuild
        if runtime.db_path.stat().st_ino != inode:
            raise AssertionError("post-commit restore changed the live SQLite inode")
        with _connect(runtime.db_path) as conn:
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name LIKE 'sheet_vitrina_v1_canonical_cost_%'"
            ).fetchone():
                raise AssertionError("post-commit restore did not return the pre-apply schema")
        reader = sqlite3.connect(runtime.db_path)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_ready_snapshots").fetchone()
        applied = run(
            _args(
                runtime_dir,
                backup_dir,
                apply=True,
                fingerprint=dry["fingerprint"],
            )
        )
        reader.rollback()
        reader.close()
        if not applied["applied"] or applied["post_run"]["changed"] != 0:
            raise AssertionError("guarded apply and second zero-change run required")
        if runtime.db_path.stat().st_ino != inode:
            raise AssertionError("in-place apply must preserve SQLite inode")
        backup_path = backup_dir / applied["backup"]["filename"]
        if oct(backup_path.stat().st_mode & 0o777) != "0o600":
            raise AssertionError("backup mode must be 0600")
        with _connect(runtime.db_path) as conn:
            if conn.execute("SELECT value FROM non_target_fixture").fetchone()[0] != "preserve-me":
                raise AssertionError("non-target table changed")
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise AssertionError("post-apply integrity_check failed")
        repeated_dry = run(_args(runtime_dir, backup_dir))
        if repeated_dry["would_change"]:
            raise AssertionError("repeat dry-run must be zero-change")
        repeated_apply = run(
            _args(
                runtime_dir,
                backup_dir,
                apply=True,
                fingerprint=repeated_dry["fingerprint"],
            )
        )
        if repeated_apply["post_run"] != {"changed": 0, "idempotent": True}:
            raise AssertionError("repeat apply must perform no database write")
    _blocked_baseline_report()
    print("canonical_cost_engine_backfill_smoke: ok")
    return 0


def _blocked_baseline_report() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        runtime_dir = root / "runtime"
        backup_dir = root / "backups"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(runtime.db_path) as conn:
            _ensure_schema(conn)
            _insert_primary(conn)
            _insert_fallback_production(conn, nm_id=222)
            _insert_ff_balance(conn, nm_id=111, quantity=6750)
            _insert_snapshot(
                conn, "2026-07-01",
                {111: {"stock_total": 93250}, 222: {"stock_total": 0}},
            )
            conn.commit()
        blocked = run(_args(runtime_dir, backup_dir))
        if blocked.get("status") != "blocked":
            raise AssertionError("missing baseline SKU must return a blocked report")
        details = blocked["blocker"]["details"]
        if details["missing_nm_ids"] != [222] or details["cost_coverage"] == "1":
            raise AssertionError("blocked report must expose missing SKU and partial coverage")
        repeated = run(_args(runtime_dir, backup_dir))
        if repeated["fingerprint"] != blocked["fingerprint"]:
            raise AssertionError("blocked report fingerprint must be stable")
        try:
            run(
                _args(
                    runtime_dir, backup_dir, apply=True,
                    fingerprint=blocked["fingerprint"],
                )
            )
        except ValueError as exc:
            if "production apply blocked" not in str(exc):
                raise
        else:
            raise AssertionError("blocked baseline must never enter apply")


def _args(
    runtime_dir: Path,
    backup_dir: Path,
    *,
    apply: bool = False,
    fingerprint: str = "",
) -> Namespace:
    return Namespace(
        runtime_dir=str(runtime_dir),
        date_from="2026-07-01",
        date_to="2026-07-01",
        apply=apply,
        fingerprint=fingerprint,
        backup_dir=str(backup_dir),
    )


if __name__ == "__main__":
    raise SystemExit(main())
