#!/usr/bin/env python3
"""Guarded baseline/cost-engine backfill.  Dry-run is the default and only pre-gate mode."""

from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.canonical_cost_engine import (  # noqa: E402
    CANONICAL_TABLE_PREFIX,
    CUTOVER_DATE,
    STAGES,
    CanonicalCostEngine,
    ensure_canonical_cost_schema,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)


VOLATILE_COLUMNS = {"calculated_at", "created_at", "superseded_at"}
SOURCE_TABLES = (
    "sheet_vitrina_v1_supplier_shipments",
    "sheet_vitrina_v1_supplier_shipment_lines",
    "sheet_vitrina_v1_supplier_financial_documents",
    "sheet_vitrina_v1_supplier_financial_expense_lines",
    "sheet_vitrina_v1_supplier_ff_cost_layers",
    "sheet_vitrina_v1_supplier_ff_cost_layer_lines",
    "sheet_vitrina_v1_cny_documents",
    "sheet_vitrina_v1_cny_ledger_operations",
    "sheet_vitrina_v1_ff_stock_operations",
    "sheet_vitrina_v1_ff_stock_operation_lines",
    "sheet_vitrina_v1_wb_supplies",
    "sheet_vitrina_v1_nomenclature_items",
    "sheet_vitrina_v1_ready_snapshots",
)
PROTECTED_TABLES = (
    "sheet_vitrina_v1_onec_stocks",
    "sheet_vitrina_v1_own_capital_payment_layers",
    "sheet_vitrina_v1_own_capital_events",
    "sheet_vitrina_v1_own_capital_wb_outstanding",
    "sheet_vitrina_v1_wb_opening_baseline",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--date-from", default=CUTOVER_DATE)
    parser.add_argument("--date-to", default=date.today().isoformat())
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint", default="")
    parser.add_argument("--backup-dir", default="")
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    date_from = _date(args.date_from)
    date_to = _date(args.date_to)
    if date_from != CUTOVER_DATE:
        raise ValueError(f"scope must start exactly at {CUTOVER_DATE}")
    if date_to < date_from:
        raise ValueError("date_to must be on or after date_from")
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(args.runtime_dir))
    source_db = runtime.db_path
    if not source_db.exists():
        raise ValueError("runtime SQLite database does not exist")
    source_inode = source_db.stat().st_ino
    integrity_before = _integrity_check(source_db)
    source_digest = _tables_digest(source_db, SOURCE_TABLES)
    protected_digest = _tables_digest(source_db, PROTECTED_TABLES)
    legacy_digest = _legacy_digest(source_db)
    target_before = _canonical_digest(source_db, date_from=date_from, date_to=date_to)

    with tempfile.TemporaryDirectory(prefix="canonical-cost-candidate-") as temp_dir:
        candidate_runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(temp_dir) / "runtime")
        candidate_runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
        _sqlite_backup(source_db, candidate_runtime.db_path)
        engine = CanonicalCostEngine(runtime=candidate_runtime)
        baseline = engine.build_baseline_plan(cutover_date=date_from)
        engine.materialize_baseline_plan(baseline)
        rebuild = engine.rebuild(date_from=date_from, date_to=date_to)
        first_target = _canonical_digest(
            candidate_runtime.db_path, date_from=date_from, date_to=date_to
        )
        second = engine.rebuild(date_from=date_from, date_to=date_to)
        second_target = _canonical_digest(
            candidate_runtime.db_path, date_from=date_from, date_to=date_to
        )
        if second_target != first_target or any(
            (
                second.component_rows_changed,
                second.movement_rows_changed,
                second.outstanding_rows_changed,
                second.daily_rows_changed,
            )
        ):
            raise ValueError("candidate second run is not zero-change idempotent")
        if source_digest != _tables_digest(candidate_runtime.db_path, SOURCE_TABLES):
            raise ValueError("candidate changed authoritative source tables")
        if protected_digest != _tables_digest(candidate_runtime.db_path, PROTECTED_TABLES):
            raise ValueError("candidate changed protected 1C/legacy tables")
        if legacy_digest != _legacy_digest(candidate_runtime.db_path):
            raise ValueError("candidate changed dates before cutover")
        reconciliation = _candidate_reconciliation(candidate_runtime.db_path, date_to)
        report = {
            "contract_name": "canonical_cost_engine_backfill_v1",
            "scope": {"date_from": date_from, "date_to": date_to},
            "baseline": baseline,
            "rebuild": asdict(rebuild),
            "reconciliation": reconciliation,
            "affected_finance_periods": _finance_periods(date_from, date_to),
            "source_digest": source_digest,
            "protected_non_target_digest": protected_digest,
            "legacy_pre_cutover_digest": legacy_digest,
            "target_before_digest": target_before,
            "candidate_target_digest": first_target,
        }
        fingerprint = _hash(report)
        payload = {
            **report,
            "mode": "apply" if args.apply else "dry-run",
            "fingerprint": fingerprint,
            "would_change": target_before != first_target,
            "integrity_check": integrity_before,
            "source_inode": source_inode,
            "applied": False,
            "backup": None,
            "post_run": None,
        }
        if not args.apply:
            return payload
        if str(args.fingerprint or "") != fingerprint:
            raise ValueError("apply requires the exact current dry-run fingerprint")
        if not str(args.backup_dir or "").strip():
            raise ValueError("apply requires an explicit --backup-dir")
        if not payload["would_change"]:
            payload["post_run"] = {"changed": 0, "idempotent": True}
            return payload
        backup_path = Path(args.backup_dir) / (
            f"{source_db.stem}.canonical-cost-backup-"
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3"
        )
        backup = _sqlite_backup(source_db, backup_path)
        materialized = _read_canonical_tables(candidate_runtime.db_path)
        with closing(sqlite3.connect(source_db, timeout=60)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            try:
                if source_digest != _tables_digest_conn(conn, SOURCE_TABLES):
                    raise ValueError("optimistic authoritative-source digest drift")
                if protected_digest != _tables_digest_conn(conn, PROTECTED_TABLES):
                    raise ValueError("optimistic protected-table digest drift")
                if legacy_digest != _legacy_digest_conn(conn):
                    raise ValueError("optimistic pre-cutover digest drift")
                if target_before != _canonical_digest_conn(
                    conn, date_from=date_from, date_to=date_to
                ):
                    raise ValueError("optimistic target digest drift")
                ensure_canonical_cost_schema(conn)
                _replace_canonical_tables(conn, materialized)
                if first_target != _canonical_digest_conn(
                    conn, date_from=date_from, date_to=date_to
                ):
                    raise ValueError("candidate target digest mismatch in transaction")
                if source_digest != _tables_digest_conn(conn, SOURCE_TABLES):
                    raise ValueError("authoritative source changed in transaction")
                if protected_digest != _tables_digest_conn(conn, PROTECTED_TABLES):
                    raise ValueError("protected target changed in transaction")
                if legacy_digest != _legacy_digest_conn(conn):
                    raise ValueError("pre-cutover rows changed in transaction")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        try:
            if source_db.stat().st_ino != source_inode:
                raise ValueError("live SQLite inode changed; in-place apply contract violated")
            if _integrity_check(source_db) != "ok":
                raise ValueError("post-apply integrity check failed")
            post = CanonicalCostEngine(runtime=runtime).rebuild(
                date_from=date_from, date_to=date_to
            )
            if any((
                post.component_rows_changed,
                post.movement_rows_changed,
                post.outstanding_rows_changed,
                post.daily_rows_changed,
            )):
                raise ValueError("post-apply second run was not zero-change")
            if source_digest != _tables_digest(source_db, SOURCE_TABLES):
                raise ValueError("post-apply authoritative source digest mismatch")
            if protected_digest != _tables_digest(source_db, PROTECTED_TABLES):
                raise ValueError("post-apply protected digest mismatch")
            if legacy_digest != _legacy_digest(source_db):
                raise ValueError("post-apply pre-cutover digest mismatch")
        except Exception:
            _restore_backup_in_place(backup_path, source_db)
            if source_db.stat().st_ino != source_inode:
                raise ValueError(
                    "post-apply rollback could not restore the original SQLite inode"
                )
            if _integrity_check(source_db) != "ok":
                raise ValueError("post-apply rollback integrity check failed")
            raise
        payload["applied"] = True
        payload["backup"] = backup
        payload["post_run"] = {"changed": 0, "idempotent": True, "fingerprint": post.fingerprint}
        return payload


def _candidate_reconciliation(db_path: Path, as_of_date: str) -> dict[str, Any]:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        stage_rows = conn.execute(
            """
            SELECT stage,SUM(physical_quantity+0) physical_quantity,
                   SUM(paid_equivalent_quantity+0) paid_equivalent_quantity,
                   SUM(recognized_capital_rub+0) recognized_capital_rub,
                   SUM(paid_capital_rub+0) paid_capital_rub,
                   SUM(cost_covered_quantity+0) covered_quantity,
                   SUM(confirmed_quantity+0) confirmed_quantity
            FROM sheet_vitrina_v1_canonical_cost_daily_state
            WHERE as_of_date=? GROUP BY stage ORDER BY stage
            """,
            (as_of_date,),
        ).fetchall()
        ff_ledger = conn.execute(
            """
            SELECT COALESCE(SUM(line.quantity_delta),0)
            FROM sheet_vitrina_v1_ff_stock_operation_lines line
            """
        ).fetchone()[0]
        wb = conn.execute(
            """
            SELECT physical_quantity,paid_equivalent_quantity,cost_covered_quantity,
                   recognized_capital_rub,paid_capital_rub,
                   recognized_unit_cost_rub,paid_unit_cost_rub
            FROM sheet_vitrina_v1_canonical_cost_daily_state
            WHERE as_of_date=? AND stage='WB'
            """,
            (as_of_date,),
        ).fetchall()
        outstanding = conn.execute(
            """
            SELECT COALESCE(SUM(open_quantity+0),0) qty,
                   COALESCE(SUM((open_quantity+0)*(cost_coverage_share+0)*(recognized_unit_cost_rub+0)),0) recognized,
                   COALESCE(SUM((paid_equivalent_quantity+0)*(paid_unit_cost_rub+0)),0) paid
            FROM sheet_vitrina_v1_canonical_cost_wb_outstanding_layers WHERE is_current=1
            """
        ).fetchone()
        existing_tables = {
            str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        legacy_stage_rows = (
            conn.execute(
                """
                SELECT stage,SUM(quantity+0) physical_quantity,SUM(capital_rub+0) paid_capital_rub
                FROM sheet_vitrina_v1_own_capital_daily_state
                WHERE as_of_date=? GROUP BY stage
                """,
                (as_of_date,),
            ).fetchall()
            if "sheet_vitrina_v1_own_capital_daily_state" in existing_tables else []
        )
        legacy_wb = (
            conn.execute(
                """
                SELECT as_of_date,SUM(stock_qty) quantity,
                       SUM(stock_qty*our_wb_unit_cost_rub) recognized_capital
                FROM sheet_vitrina_v1_wb_cost_daily_state
                WHERE as_of_date=(SELECT MAX(as_of_date) FROM sheet_vitrina_v1_wb_cost_daily_state WHERE as_of_date<=?)
                """,
                (as_of_date,),
            ).fetchone()
            if "sheet_vitrina_v1_wb_cost_daily_state" in existing_tables else None
        )
    stages = {str(row["stage"]): dict(row) for row in stage_rows}
    ff_projected = float(stages.get("FF", {}).get("physical_quantity") or 0)
    if abs(ff_projected - float(ff_ledger or 0)) > 0.000001:
        raise ValueError("candidate FF quantity does not reconcile to ff_stock_ledger")
    wb_qty = sum(float(row["physical_quantity"] or 0) for row in wb)
    wb_covered = sum(float(row["cost_covered_quantity"] or 0) for row in wb)
    wb_paid_equivalent = sum(float(row["paid_equivalent_quantity"] or 0) for row in wb)
    wb_rec_cap = sum(float(row["recognized_capital_rub"] or 0) for row in wb)
    wb_paid_cap = sum(float(row["paid_capital_rub"] or 0) for row in wb)
    out_qty = float(outstanding["qty"] or 0)
    legacy_stages = {str(row["stage"]): dict(row) for row in legacy_stage_rows}
    current_vs_candidate = {}
    for stage in STAGES:
        candidate = stages.get(stage, {})
        current = legacy_stages.get(stage, {})
        current_vs_candidate[stage] = {
            "physical_quantity_delta": float(candidate.get("physical_quantity") or 0)
            - float(current.get("physical_quantity") or 0),
            "paid_capital_rub_delta": float(candidate.get("paid_capital_rub") or 0)
            - float(current.get("paid_capital_rub") or 0),
        }
    return {
        "as_of_date": as_of_date,
        "stages": stages,
        "ff_ledger_quantity": float(ff_ledger or 0),
        "ff_candidate_quantity": ff_projected,
        "wb": {
            "quantity": wb_qty,
            "recognized_unit_cost_rub": wb_rec_cap / wb_covered if wb_covered else None,
            "paid_unit_cost_rub": wb_paid_cap / wb_paid_equivalent if wb_paid_equivalent else None,
            "cost_coverage": wb_covered / wb_qty if wb_qty else None,
        },
        "underaccepted": {
            "quantity": out_qty,
            "recognized_weighted_unit_cost_rub": float(outstanding["recognized"] or 0) / out_qty if out_qty else None,
            "paid_weighted_unit_cost_rub": float(outstanding["paid"] or 0) / out_qty if out_qty else None,
        },
        "current_vs_candidate_delta": current_vs_candidate,
        "legacy_wb_comparison": None if legacy_wb is None else {
            "as_of_date": str(legacy_wb["as_of_date"] or ""),
            "quantity": float(legacy_wb["quantity"] or 0),
            "recognized_unit_cost_rub": (
                float(legacy_wb["recognized_capital"] or 0) / float(legacy_wb["quantity"] or 0)
                if float(legacy_wb["quantity"] or 0) else None
            ),
        },
    }


def _finance_periods(start: str, end: str) -> list[str]:
    cursor = date.fromisoformat(start) - timedelta(days=date.fromisoformat(start).weekday())
    last = date.fromisoformat(end)
    result: list[str] = []
    while cursor <= last:
        result.append(f"{cursor.isoformat()}..{(cursor + timedelta(days=6)).isoformat()}")
        cursor += timedelta(days=7)
    return result


def _read_canonical_tables(db_path: Path) -> dict[str, tuple[list[str], list[tuple[Any, ...]]]]:
    with closing(sqlite3.connect(db_path)) as conn:
        tables = [
            str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ? ORDER BY name",
                (CANONICAL_TABLE_PREFIX + "%",),
            ).fetchall()
        ]
        result: dict[str, tuple[list[str], list[tuple[Any, ...]]]] = {}
        for table in tables:
            columns = [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')]
            rows = [tuple(row) for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
            result[table] = (columns, rows)
        return result


def _replace_canonical_tables(
    conn: sqlite3.Connection,
    materialized: Mapping[str, tuple[list[str], list[tuple[Any, ...]]]],
) -> None:
    order = (
        "sheet_vitrina_v1_canonical_cost_daily_state",
        "sheet_vitrina_v1_canonical_cost_wb_outstanding_layers",
        "sheet_vitrina_v1_canonical_cost_movement_layers",
        "sheet_vitrina_v1_canonical_cost_components",
        "sheet_vitrina_v1_canonical_cost_baseline_lines",
        "sheet_vitrina_v1_canonical_cost_baseline_versions",
    )
    for table in order:
        conn.execute(f'DELETE FROM "{table}"')
    for table in reversed(order):
        columns, rows = materialized[table]
        if not rows:
            continue
        column_sql = ",".join(f'"{column}"' for column in columns)
        placeholders = ",".join("?" for _ in columns)
        conn.executemany(
            f'INSERT INTO "{table}"({column_sql}) VALUES({placeholders})', rows
        )


def _sqlite_backup(source: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source)) as source_conn, closing(sqlite3.connect(destination)) as dest_conn:
        source_conn.backup(dest_conn)
        dest_conn.commit()
    if _integrity_check(destination) != "ok":
        destination.unlink(missing_ok=True)
        raise ValueError("coherent SQLite backup integrity_check failed")
    destination.chmod(0o600)
    if destination.stat().st_mode & 0o077:
        raise ValueError("backup mode must be 0600")
    return {
        "filename": destination.name,
        "sha256": _file_hash(destination),
        "size_bytes": destination.stat().st_size,
        "mode": "0600",
        "integrity_check": "ok",
    }


def _restore_backup_in_place(backup: Path, destination: Path) -> None:
    """Restore through SQLite's online backup API without replacing the live inode."""
    with closing(sqlite3.connect(backup)) as source_conn, closing(
        sqlite3.connect(destination, timeout=60)
    ) as destination_conn:
        # sqlite3_backup owns the destination transaction internally and takes
        # the required write lock; wrapping it in BEGIN makes SQLite reject the
        # destination as already in use.
        source_conn.backup(destination_conn)
        destination_conn.commit()


def _integrity_check(db_path: Path) -> str:
    with closing(sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)) as conn:
        value = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    if value.lower() != "ok":
        raise ValueError(f"SQLite integrity_check failed: {value}")
    return "ok"


def _canonical_digest(db_path: Path, *, date_from: str, date_to: str) -> str:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return _canonical_digest_conn(conn, date_from=date_from, date_to=date_to)


def _canonical_digest_conn(conn: sqlite3.Connection, *, date_from: str, date_to: str) -> str:
    tables = [
        str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ? ORDER BY name",
            (CANONICAL_TABLE_PREFIX + "%",),
        ).fetchall()
    ]
    evidence: list[Any] = []
    for table in tables:
        columns = [
            str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')
            if str(row[1]) not in VOLATILE_COLUMNS
        ]
        if not columns:
            continue
        sql = ",".join(f'"{column}"' for column in columns)
        if table.endswith("daily_state"):
            rows = conn.execute(
                f'SELECT {sql} FROM "{table}" WHERE as_of_date BETWEEN ? AND ? ORDER BY as_of_date,nm_id,stage',
                (date_from, date_to),
            ).fetchall()
        else:
            rows = conn.execute(f'SELECT {sql} FROM "{table}" ORDER BY rowid').fetchall()
        evidence.append({"table": table, "columns": columns, "rows": [list(row) for row in rows]})
    return _hash(evidence)


def _tables_digest(db_path: Path, tables: Iterable[str]) -> str:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return _tables_digest_conn(conn, tables)


def _tables_digest_conn(conn: sqlite3.Connection, tables: Iterable[str]) -> str:
    existing = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    evidence: dict[str, Any] = {}
    for table in tables:
        if table not in existing:
            continue
        evidence[table] = [list(row) for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
    return _hash(evidence)


def _legacy_digest(db_path: Path) -> str:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return _legacy_digest_conn(conn)


def _legacy_digest_conn(conn: sqlite3.Connection) -> str:
    evidence: dict[str, Any] = {}
    for table in (
        "sheet_vitrina_v1_ready_snapshots",
        "sheet_vitrina_v1_wb_cost_daily_state",
        "sheet_vitrina_v1_own_capital_daily_state",
    ):
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            continue
        date_column = "as_of_date"
        evidence[table] = [
            list(row) for row in conn.execute(
                f'SELECT * FROM "{table}" WHERE {date_column} < ? ORDER BY rowid',
                (CUTOVER_DATE,),
            )
        ]
    return _hash(evidence)


def _date(value: Any) -> str:
    return date.fromisoformat(str(value)).isoformat()


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    payload = run(build_parser().parse_args())
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
