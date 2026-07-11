#!/usr/bin/env python3
"""Bounded dry-run-first materialization runner for own product capital history."""

from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.cny_ledger import CnyLedgerBlock  # noqa: E402
from packages.application.ff_stock_ledger import FfStockLedgerBlock  # noqa: E402
from packages.application.own_product_capital import OwnProductCapitalBlock  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)


OWN_TABLE_PREFIX = "sheet_vitrina_v1_own_capital_"
_VOLATILE_DIGEST_COLUMNS = {
    "calculated_at",
    "certified_at",
    "created_at",
    "resolved_at",
    "updated_at",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint", default="")
    parser.add_argument("--backup-dir", default="")
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    date_from = _date(args.date_from, "date_from")
    date_to = _date(args.date_to, "date_to")
    if date_to < date_from:
        raise ValueError("date_to must be on or after date_from")
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(args.runtime_dir))
    source_db = runtime.db_path
    if not source_db.exists():
        raise ValueError("runtime SQLite database does not exist")
    preflight = _preflight(source_db, date_from=date_from, date_to=date_to)
    with tempfile.TemporaryDirectory(prefix="own-capital-backfill-") as tmp:
        candidate_dir = Path(tmp) / "runtime"
        candidate_dir.mkdir(parents=True, exist_ok=True)
        candidate_runtime = RegistryUploadDbBackedRuntime(runtime_dir=candidate_dir)
        _sqlite_backup(source_db, candidate_runtime.db_path)
        before_non_target = _non_target_digest(
            candidate_runtime.db_path, date_from=date_from, date_to=date_to
        )
        protected_non_target = _protected_non_target_digest(
            candidate_runtime.db_path
        )
        before_target = _target_digest(
            candidate_runtime.db_path, date_from=date_from, date_to=date_to
        )
        external_source_digest = _external_source_digest(
            candidate_runtime.db_path, date_from=date_from, date_to=date_to
        )
        cny_materialization = CnyLedgerBlock(
            runtime=candidate_runtime
        ).materialize_own_product_capital_history(
            date_to=date_to,
            recalculate=False,
        )
        candidate_block = OwnProductCapitalBlock(runtime=candidate_runtime)
        expense_materialization = candidate_block.materialize_persisted_expense_events(
            date_from=date_from,
            date_to=date_to,
            recalculate=False
        )
        wb_materialization = FfStockLedgerBlock(
            runtime=candidate_runtime
        ).materialize_own_product_capital_history(
            date_to=date_to,
            recalculate=False,
        )
        result = candidate_block.recalculate(
            date_from=date_from,
            date_to=date_to,
        )
        after_non_target = _non_target_digest(
            candidate_runtime.db_path, date_from=date_from, date_to=date_to
        )
        if before_non_target != after_non_target:
            raise ValueError("non-target preservation digest changed during candidate materialization")
        if protected_non_target != _protected_non_target_digest(
            candidate_runtime.db_path
        ):
            raise ValueError("1C/proxy2/proxy3 preservation digest changed during candidate materialization")
        candidate_target = _target_digest(
            candidate_runtime.db_path, date_from=date_from, date_to=date_to
        )
        candidate_preflight = _preflight(
            candidate_runtime.db_path,
            date_from=date_from,
            date_to=date_to,
        )
        candidate_second = OwnProductCapitalBlock(runtime=candidate_runtime).recalculate(
            date_from=date_from,
            date_to=date_to,
        )
        if candidate_second.daily_rows_changed != 0:
            raise ValueError("candidate second materialization was not idempotent")
        plan_fingerprint = _stable_hash(
            {
                "contract": "own_product_capital_backfill_v2",
                "scope": {"date_from": date_from, "date_to": date_to},
                "preflight": preflight,
                "external_source_digest": external_source_digest,
                "target_before_digest": before_target,
                "candidate_target_digest": candidate_target,
                "non_target_digest": before_non_target,
                "protected_non_target_digest": protected_non_target,
                "cny_materialization": cny_materialization,
                "expense_materialization": expense_materialization,
                "wb_materialization": wb_materialization,
                "candidate_preflight": candidate_preflight,
            }
        )
        payload: dict[str, Any] = {
            "contract_name": "own_product_capital_backfill_v2",
            "mode": "apply" if args.apply else "dry-run",
            "scope": {"date_from": date_from, "date_to": date_to},
            "preflight": preflight,
            "fingerprint": plan_fingerprint,
            "candidate": asdict(result),
            "non_target_preservation_digest": before_non_target,
            "protected_non_target_preservation_digest": protected_non_target,
            "target_before_digest": before_target,
            "candidate_target_digest": candidate_target,
            "external_source_digest": external_source_digest,
            "expense_materialization": expense_materialization,
            "cny_materialization": cny_materialization,
            "wb_materialization": wb_materialization,
            "candidate_preflight": candidate_preflight,
            "would_change": before_target != candidate_target,
            "applied": False,
            "backup": None,
            "post_run": None,
        }
        if not args.apply:
            return payload
        if (
            candidate_preflight["unresolved_blocker_count"]
            or cny_materialization["blocker_count"]
            or expense_materialization["blocker_count"]
            or wb_materialization["blocker_count"]
        ):
            raise ValueError("apply is blocked while unresolved own-product-capital invariants exist")
        if str(args.fingerprint or "") != plan_fingerprint:
            raise ValueError("apply requires the exact current dry-run fingerprint")
        if before_target == candidate_target:
            payload["post_run"] = {"changed": 0, "idempotent": True}
            return payload
        backup_dir = str(getattr(args, "backup_dir", "") or "").strip()
        if not backup_dir:
            raise ValueError("apply requires an explicit --backup-dir")
        backup_path = Path(backup_dir) / (
            f"{source_db.stem}.own-capital-backup-"
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3"
        )
        backup = _sqlite_backup(source_db, backup_path)
        with closing(sqlite3.connect(source_db, timeout=60)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            try:
                transactional_preflight = _preflight_conn(
                    conn, date_from=date_from, date_to=date_to
                )
                transactional_non_target = _non_target_digest_conn(
                    conn, date_from=date_from, date_to=date_to
                )
                transactional_target = _target_digest_conn(
                    conn, date_from=date_from, date_to=date_to
                )
                transactional_external = _external_source_digest_conn(
                    conn, date_from=date_from, date_to=date_to
                )
                transactional_protected_non_target = (
                    _protected_non_target_digest_conn(conn)
                )
                transactional_fingerprint = _stable_hash(
                    {
                        "contract": "own_product_capital_backfill_v2",
                        "scope": {"date_from": date_from, "date_to": date_to},
                        "preflight": transactional_preflight,
                        "external_source_digest": transactional_external,
                        "target_before_digest": transactional_target,
                        "candidate_target_digest": candidate_target,
                        "non_target_digest": transactional_non_target,
                        "protected_non_target_digest": transactional_protected_non_target,
                        "cny_materialization": cny_materialization,
                        "expense_materialization": expense_materialization,
                        "wb_materialization": wb_materialization,
                        "candidate_preflight": candidate_preflight,
                    }
                )
                if transactional_fingerprint != plan_fingerprint:
                    raise ValueError(
                        "optimistic source/target fingerprint changed before in-place apply"
                    )
                _copy_candidate_target_rows(
                    conn,
                    candidate_runtime.db_path,
                    date_from=date_from,
                    date_to=date_to,
                )
                if (
                    _target_digest_conn(
                        conn, date_from=date_from, date_to=date_to
                    )
                    != candidate_target
                ):
                    raise ValueError("transactional target digest mismatch")
                if (
                    _non_target_digest_conn(
                        conn, date_from=date_from, date_to=date_to
                    )
                    != before_non_target
                ):
                    raise ValueError("transactional non-target digest mismatch")
                if (
                    _protected_non_target_digest_conn(conn)
                    != protected_non_target
                ):
                    raise ValueError("transactional 1C/proxy2/proxy3 digest mismatch")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        payload["applied"] = True
        payload["backup"] = backup
        payload["post_apply_integrity_check"] = _integrity_check(source_db)
        second = OwnProductCapitalBlock(runtime=runtime).recalculate(
            date_from=date_from,
            date_to=date_to,
        )
        if second.daily_rows_changed != 0:
            raise ValueError("post-run second materialization was not idempotent")
        if (
            _non_target_digest(source_db, date_from=date_from, date_to=date_to)
            != before_non_target
        ):
            raise ValueError("post-run non-target preservation digest mismatch")
        if _protected_non_target_digest(source_db) != protected_non_target:
            raise ValueError("post-run 1C/proxy2/proxy3 preservation digest mismatch")
        if _target_digest(source_db, date_from=date_from, date_to=date_to) != candidate_target:
            raise ValueError("post-run target digest mismatch")
        payload["post_run"] = {
            "changed": second.daily_rows_changed,
            "idempotent": True,
            "reconciliation_fingerprint": second.fingerprint,
        }
        return payload


def _preflight(db_path: Path, *, date_from: str, date_to: str) -> dict[str, Any]:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        try:
            return _preflight_conn(conn, date_from=date_from, date_to=date_to)
        finally:
            conn.rollback()


def _preflight_conn(
    conn: sqlite3.Connection, *, date_from: str, date_to: str
) -> dict[str, Any]:
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    event_table = "sheet_vitrina_v1_own_capital_events"
    events: list[dict[str, Any]] = []
    unresolved_blockers: list[dict[str, Any]] = []
    if event_table in tables:
        events = [
            dict(row)
            for row in conn.execute(
                """
                SELECT event_id, event_type, effective_date, evidence_hash
                FROM sheet_vitrina_v1_own_capital_events
                WHERE effective_date <= ?
                ORDER BY effective_date, event_id
                """,
                (date_to,),
            ).fetchall()
        ]
    blocker_table = "sheet_vitrina_v1_own_capital_blockers"
    if blocker_table in tables:
        unresolved_blockers = [
            {
                "code": str(row["code"]),
                "source_identity": str(row["source_identity"]),
            }
            for row in conn.execute(
                """
                SELECT code, source_identity
                FROM sheet_vitrina_v1_own_capital_blockers
                WHERE resolved_at IS NULL
                ORDER BY code, source_identity
                """
            ).fetchall()
        ]
    return {
        "read_only": True,
        "bounded": True,
        "date_from": date_from,
        "date_to": date_to,
        "event_count": len(events),
        "event_fingerprint": _stable_hash(events),
        "unresolved_blocker_count": len(unresolved_blockers),
        "unresolved_blockers": unresolved_blockers,
        "warnings": [] if events else ["no persisted paid/movement events in bounded scope"],
    }


def _sqlite_backup(source: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source)) as source_conn, closing(sqlite3.connect(destination)) as destination_conn:
        source_conn.backup(destination_conn)
        destination_conn.commit()
    with closing(sqlite3.connect(destination)) as verify:
        result = str(verify.execute("PRAGMA integrity_check").fetchone()[0])
    if result.lower() != "ok":
        destination.unlink(missing_ok=True)
        raise ValueError("coherent SQLite backup integrity_check failed")
    destination.chmod(0o600)
    return {
        "created": True,
        "filename": destination.name,
        "sha256": _file_sha256(destination),
        "size_bytes": destination.stat().st_size,
        "integrity_check": "ok",
    }


def _integrity_check(db_path: Path) -> str:
    with closing(sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)) as conn:
        result = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    if result.lower() != "ok":
        raise ValueError(f"SQLite integrity_check failed: {result}")
    return "ok"


def _non_target_digest(
    db_path: Path, *, date_from: str, date_to: str
) -> str:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return _non_target_digest_conn(
            conn, date_from=date_from, date_to=date_to
        )


def _non_target_digest_conn(
    conn: sqlite3.Connection, *, date_from: str, date_to: str
) -> str:
    target_table = f"{OWN_TABLE_PREFIX}daily_state"
    if not _table_exists(conn, target_table):
        return _stable_hash([])
    columns = [
        str(row[1])
        for row in conn.execute(f'PRAGMA table_info("{target_table}")').fetchall()
        if str(row[1]) not in _VOLATILE_DIGEST_COLUMNS
    ]
    column_sql = ",".join(f'"{column}"' for column in columns)
    rows = conn.execute(
        f'SELECT {column_sql} FROM "{target_table}" '
        "WHERE as_of_date < ? OR as_of_date > ? ORDER BY as_of_date, nm_id, stage",
        (date_from, date_to),
    ).fetchall()
    return _stable_hash(
        [[_digest_value(value) for value in row] for row in rows]
    )


def _target_digest(db_path: Path, *, date_from: str, date_to: str) -> str:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return _target_digest_conn(conn, date_from=date_from, date_to=date_to)


def _target_digest_conn(
    conn: sqlite3.Connection, *, date_from: str, date_to: str
) -> str:
    tables = sorted(
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        if str(row[0]).startswith(OWN_TABLE_PREFIX)
    )
    daily_table = f"{OWN_TABLE_PREFIX}daily_state"
    evidence: list[Any] = []
    for table in tables:
        columns = [
            str(row[1])
            for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            if str(row[1]) not in _VOLATILE_DIGEST_COLUMNS
        ]
        if not columns:
            continue
        column_sql = ",".join(f'"{column}"' for column in columns)
        if table == daily_table:
            rows = conn.execute(
                f'SELECT {column_sql} FROM "{table}" '
                "WHERE as_of_date BETWEEN ? AND ? ORDER BY as_of_date, nm_id, stage",
                (date_from, date_to),
            ).fetchall()
        else:
            rows = conn.execute(
                f'SELECT {column_sql} FROM "{table}" ORDER BY rowid'
            ).fetchall()
        evidence.append(
            {
                "table": table,
                "columns": columns,
                "rows": [[_digest_value(value) for value in row] for row in rows],
            }
        )
    return _stable_hash(evidence)


def _protected_non_target_digest(db_path: Path) -> str:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return _protected_non_target_digest_conn(conn)


def _protected_non_target_digest_conn(conn: sqlite3.Connection) -> str:
    evidence: dict[str, Any] = {}
    for table in (
        "sheet_vitrina_v1_onec_stocks",
        "sheet_vitrina_v1_our_wb_costs",
        "sheet_vitrina_v1_supplier_ff_cost_layers",
        "sheet_vitrina_v1_supplier_ff_cost_layer_lines",
        "sheet_vitrina_v1_wb_opening_baseline",
        "sheet_vitrina_v1_wb_supply_cost_layers",
        "sheet_vitrina_v1_wb_cost_daily_state",
    ):
        if not _table_exists(conn, table):
            continue
        rows = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
        evidence[table] = [
            [_digest_value(value) for value in row] for row in rows
        ]
    return _stable_hash(evidence)


def _external_source_digest(
    db_path: Path, *, date_from: str, date_to: str
) -> str:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return _external_source_digest_conn(
            conn, date_from=date_from, date_to=date_to
        )


def _external_source_digest_conn(
    conn: sqlite3.Connection, *, date_from: str, date_to: str
) -> str:
    evidence: dict[str, Any] = {}
    wb_table = "sheet_vitrina_v1_wb_cost_daily_state"
    if _table_exists(conn, wb_table):
        rows = conn.execute(
            f'SELECT * FROM "{wb_table}" WHERE as_of_date BETWEEN ? AND ? '
            "ORDER BY as_of_date, nm_id",
            (date_from, date_to),
        ).fetchall()
        evidence[wb_table] = [
            [_digest_value(value) for value in row] for row in rows
        ]
    for table in (
        "sheet_vitrina_v1_cny_documents",
        "sheet_vitrina_v1_cny_ledger_operations",
        "sheet_vitrina_v1_ff_stock_operations",
        "sheet_vitrina_v1_ff_stock_operation_lines",
        "sheet_vitrina_v1_nomenclature_items",
        "sheet_vitrina_v1_wb_supplies",
        "sheet_vitrina_v1_supplier_shipments",
        "sheet_vitrina_v1_supplier_shipment_lines",
        "sheet_vitrina_v1_supplier_financial_documents",
        "sheet_vitrina_v1_supplier_financial_expense_lines",
    ):
        if not _table_exists(conn, table):
            continue
        rows = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
        evidence[table] = [
            [_digest_value(value) for value in row] for row in rows
        ]
    return _stable_hash(evidence)


def _copy_candidate_target_rows(
    conn: sqlite3.Connection,
    candidate_db: Path,
    *,
    date_from: str,
    date_to: str,
) -> None:
    daily_table = f"{OWN_TABLE_PREFIX}daily_state"
    with closing(sqlite3.connect(candidate_db)) as candidate:
        candidate.row_factory = sqlite3.Row
        tables = sorted(
            str(row[0])
            for row in candidate.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if str(row[0]).startswith(OWN_TABLE_PREFIX)
        )
        materialized: dict[str, tuple[list[str], list[sqlite3.Row]]] = {}
        for table in tables:
            columns = [
                str(row[1])
                for row in candidate.execute(
                    f'PRAGMA table_info("{table}")'
                ).fetchall()
            ]
            if table == daily_table:
                rows = candidate.execute(
                    f'SELECT * FROM "{table}" WHERE as_of_date BETWEEN ? AND ? '
                    "ORDER BY as_of_date, nm_id, stage",
                    (date_from, date_to),
                ).fetchall()
            else:
                rows = candidate.execute(
                    f'SELECT * FROM "{table}" ORDER BY rowid'
                ).fetchall()
            materialized[table] = (columns, rows)
    for table, (columns, rows) in materialized.items():
        if table == daily_table:
            conn.execute(
                f'DELETE FROM "{table}" WHERE as_of_date BETWEEN ? AND ?',
                (date_from, date_to),
            )
        else:
            conn.execute(f'DELETE FROM "{table}"')
        _insert_rows(conn, table, columns, rows)


def _insert_rows(
    conn: sqlite3.Connection,
    table: str,
    columns: list[str],
    rows: Iterable[sqlite3.Row],
) -> None:
    materialized = list(rows)
    if not materialized:
        return
    placeholders = ",".join("?" for _ in columns)
    column_sql = ",".join(f'"{column}"' for column in columns)
    conn.executemany(
        f'INSERT INTO "{table}" ({column_sql}) VALUES ({placeholders})',
        [tuple(row[column] for column in columns) for row in materialized],
    )


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _digest_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _date(value: str, field: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc


def main() -> None:
    payload = run(build_parser().parse_args())
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
