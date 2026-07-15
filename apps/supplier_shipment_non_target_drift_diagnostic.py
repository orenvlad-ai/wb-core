#!/usr/bin/env python3
"""Read-only row-level localization for supplier collateral snapshot drift."""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.supplier_shipment_factual_correction import (  # noqa: E402
    PROTECTED_COLLATERAL_TABLES,
)


WRITERS = {
    "sheet_vitrina_v1_ready_snapshots": "sheet-vitrina refresh/materialization/publication",
    "sheet_vitrina_v1_wb_supplies": "WB supplies sync/enrichment",
    "sheet_vitrina_v1_wb_supply_cost_layers": "WB cost-layer recalculation",
    "sheet_vitrina_v1_onec_stocks": "1C stock refresh",
    "sheet_vitrina_v1_nomenclature_items": "nomenclature sync/operator update",
    "sheet_vitrina_v1_own_capital_payment_layers": "own-capital recalculation",
    "sheet_vitrina_v1_own_capital_events": "own-capital audited event writer",
    "sheet_vitrina_v1_own_capital_wb_outstanding": "own-capital WB reconciliation",
}
SENSITIVE_FIELD_PARTS = (
    "blob",
    "json",
    "payload",
    "raw_",
    "file_path",
    "filename",
    "comment",
    "name",
)


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()


def _connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _table_info(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return list(conn.execute(f'PRAGMA table_info("{table}")'))


def _row_fingerprints(
    conn: sqlite3.Connection, table: str, info: list[sqlite3.Row]
) -> dict[tuple[Any, ...], str]:
    columns = [str(row[1]) for row in info]
    primary = [
        str(row[1])
        for row in sorted(info, key=lambda item: int(item[5] or 0))
        if int(row[5] or 0) > 0
    ]
    identity_columns = primary or columns
    selected = ",".join(f'"{column}"' for column in columns)
    order = ",".join(f'"{column}"' for column in identity_columns)
    result: dict[tuple[Any, ...], str] = {}
    for row in conn.execute(f'SELECT {selected} FROM "{table}" ORDER BY {order}'):
        values = dict(row)
        identity = tuple(values[column] for column in identity_columns)
        result[identity] = _hash([[column, values[column]] for column in columns])
    return result


def _read_identity_row(
    conn: sqlite3.Connection,
    table: str,
    info: list[sqlite3.Row],
    identity: tuple[Any, ...],
) -> dict[str, Any] | None:
    columns = [str(row[1]) for row in info]
    primary = [
        str(row[1])
        for row in sorted(info, key=lambda item: int(item[5] or 0))
        if int(row[5] or 0) > 0
    ]
    identity_columns = primary or columns
    if not primary:
        # The full row is the synthetic identity, so it is already available.
        return dict(zip(columns, identity))
    where = " AND ".join(f'"{column}" IS ?' for column in identity_columns)
    row = conn.execute(
        f'SELECT * FROM "{table}" WHERE {where}', tuple(identity)
    ).fetchone()
    return dict(row) if row is not None else None


def _safe_value(field: str, value: Any) -> Any:
    if value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, bytes):
        return {"sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)}
    text = str(value)
    if any(part in field.lower() for part in SENSITIVE_FIELD_PARTS) or len(text) > 120:
        return {
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "characters": len(text),
        }
    return text


def _extract_ints(value: Any, keys: set[str]) -> set[int]:
    result: set[int] = set()
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key) in keys:
                try:
                    result.add(int(nested))
                except (TypeError, ValueError):
                    pass
            result.update(_extract_ints(nested, keys))
    elif isinstance(value, list):
        for nested in value:
            result.update(_extract_ints(nested, keys))
    elif isinstance(value, str):
        result.update(int(match) for match in re.findall(r"\bSKU:(\d+)\b", value))
    return result


def _row_nm_ids(row: Mapping[str, Any] | None) -> set[int]:
    if not row:
        return set()
    result: set[int] = set()
    for key in ("nm_id", "internal_nm_id"):
        try:
            if row.get(key) is not None:
                result.add(int(row[key]))
        except (TypeError, ValueError):
            pass
    for field, value in row.items():
        if "json" not in field.lower() or not isinstance(value, str):
            continue
        try:
            result.update(
                _extract_ints(json.loads(value), {"nm_id", "nmId", "nmID"})
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return result


def _shipment_ids(row: Mapping[str, Any] | None) -> set[str]:
    if not row:
        return set()
    return {
        str(row[field])
        for field in (
            "shipment_id",
            "supplier_shipment_id",
            "source_object_id",
            "source_shipment_id",
            "source_order_id",
            "context_order_id",
        )
        if row.get(field)
    }


def _changed_fields(
    before: Mapping[str, Any] | None, after: Mapping[str, Any] | None
) -> list[str]:
    return sorted(
        field
        for field in set(before or {}) | set(after or {})
        if (before or {}).get(field) != (after or {}).get(field)
    )


def build_report(
    before_db: Path,
    after_db: Path,
    *,
    target_shipment_ids: Iterable[str],
    target_nm_ids: Iterable[int],
) -> dict[str, Any]:
    targets = {str(item) for item in target_shipment_ids if str(item)}
    nm_targets = {int(item) for item in target_nm_ids}
    changes: list[dict[str, Any]] = []
    with closing(_connect_ro(before_db)) as before_conn, closing(
        _connect_ro(after_db)
    ) as after_conn:
        before_tables = _tables(before_conn)
        after_tables = _tables(after_conn)
        for table in PROTECTED_COLLATERAL_TABLES:
            if table not in before_tables and table not in after_tables:
                continue
            info_conn = before_conn if table in before_tables else after_conn
            info = _table_info(info_conn, table)
            before_rows = (
                _row_fingerprints(before_conn, table, info)
                if table in before_tables
                else {}
            )
            after_rows = (
                _row_fingerprints(after_conn, table, info)
                if table in after_tables
                else {}
            )
            for identity in sorted(
                set(before_rows) | set(after_rows), key=lambda item: repr(item)
            ):
                before_fp = before_rows.get(identity)
                after_fp = after_rows.get(identity)
                if before_fp == after_fp:
                    continue
                before = (
                    _read_identity_row(before_conn, table, info, identity)
                    if before_fp
                    else None
                )
                after = (
                    _read_identity_row(after_conn, table, info, identity)
                    if after_fp
                    else None
                )
                fields = _changed_fields(before, after)
                row_nm_ids = _row_nm_ids(before) | _row_nm_ids(after)
                row_shipments = _shipment_ids(before) | _shipment_ids(after)
                target_related = bool(row_shipments & targets)
                sku_related = bool(row_nm_ids & nm_targets)
                relevant = target_related or sku_related
                identity_columns = [
                    str(row[1])
                    for row in sorted(info, key=lambda item: int(item[5] or 0))
                    if int(row[5] or 0) > 0
                ] or [str(row[1]) for row in info]
                changes.append(
                    {
                        "table": table,
                        "identity": {
                            field: _safe_value(field, value)
                            for field, value in zip(identity_columns, identity)
                        },
                        "change_kind": (
                            "added" if before is None else "removed" if after is None else "updated"
                        ),
                        "before_row_fingerprint": before_fp,
                        "after_row_fingerprint": after_fp,
                        "changed_fields": fields,
                        "values": {
                            field: {
                                "before": _safe_value(field, (before or {}).get(field)),
                                "after": _safe_value(field, (after or {}).get(field)),
                            }
                            for field in fields
                        },
                        "timestamps": {
                            field: {
                                "before": (before or {}).get(field),
                                "after": (after or {}).get(field),
                            }
                            for field in fields
                            if field.endswith("_at") or field.endswith("_date")
                        },
                        "writer": WRITERS.get(table, "repository-owned domain writer"),
                        "target_shipment_related": target_related,
                        "target_sku_dependency_related": sku_related,
                        "target_nm_ids": sorted(row_nm_ids & nm_targets),
                        "can_change_canonical_candidate": relevant,
                        "can_change_accounting_effects": relevant
                        and table != "sheet_vitrina_v1_ready_snapshots",
                        "classification": "relevant_dependency" if relevant else "unrelated_live_activity",
                    }
                )
    changes.sort(key=lambda item: (item["table"], json.dumps(item["identity"], sort_keys=True)))
    payload = {
        "contract_name": "supplier_non_target_drift_localization_v1",
        "read_only": True,
        "scope": {
            "target_shipment_ids": sorted(targets),
            "target_nm_ids": sorted(nm_targets),
            "tables": list(PROTECTED_COLLATERAL_TABLES),
        },
        "change_count": len(changes),
        "relevant_change_count": sum(
            item["classification"] == "relevant_dependency" for item in changes
        ),
        "unrelated_change_count": sum(
            item["classification"] == "unrelated_live_activity" for item in changes
        ),
        "changes": changes,
    }
    return {**payload, "fingerprint": _hash(payload)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before-db", required=True)
    parser.add_argument("--after-db", required=True)
    parser.add_argument("--target-shipment-id", action="append", required=True)
    parser.add_argument("--target-nm-id", action="append", type=int, required=True)
    parser.add_argument("--output")
    args = parser.parse_args()
    report = build_report(
        Path(args.before_db),
        Path(args.after_db),
        target_shipment_ids=args.target_shipment_id,
        target_nm_ids=args.target_nm_id,
    )
    text = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, default=str)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
