#!/usr/bin/env python3
"""Executable contract for whole-box WB supply corrections."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.wb_supply_box_correction import (
    apply_unique_box_correction,
    solve_unique_box_correction,
)


SKU_SHORT = 497416559
SKU_SURPLUS = 497414624
SKU_ONE_A = 391662965
SKU_ONE_B = 428853741
SKU_ONE_C = 428854140


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE sheet_vitrina_v1_ff_stock_operations(
            operation_id TEXT PRIMARY KEY,operation_type TEXT NOT NULL,
            source_type TEXT NOT NULL,source_key TEXT NOT NULL,
            source_object_id TEXT NOT NULL,source_object_label TEXT NOT NULL,
            created_at TEXT NOT NULL,created_by TEXT NOT NULL,
            sku_count INTEGER NOT NULL,total_quantity_delta REAL NOT NULL,
            total_quantity_abs REAL NOT NULL,warnings_json TEXT NOT NULL,
            diagnostics_json TEXT NOT NULL,source_filename TEXT NOT NULL,
            source_content_type TEXT NOT NULL,source_file_sha256 TEXT NOT NULL,
            source_file_blob BLOB
        );
        CREATE TABLE sheet_vitrina_v1_ff_stock_operation_lines(
            operation_id TEXT NOT NULL,line_no INTEGER NOT NULL,
            nm_id INTEGER NOT NULL,barcode TEXT NOT NULL,sku TEXT NOT NULL,
            nomenclature_name TEXT NOT NULL,comment TEXT NOT NULL,
            group_name TEXT NOT NULL,quantity_delta REAL NOT NULL,
            raw_json TEXT NOT NULL,PRIMARY KEY(operation_id,line_no)
        );
        """
    )


def _operation(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    operation_type: str,
    source_type: str,
    source_key: str,
    source_object_id: str,
    lines: dict[int, int],
) -> None:
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_ff_stock_operations(
            operation_id,operation_type,source_type,source_key,
            source_object_id,source_object_label,created_at,created_by,
            sku_count,total_quantity_delta,total_quantity_abs,warnings_json,
            diagnostics_json,source_filename,source_content_type,
            source_file_sha256,source_file_blob
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
        """,
        (
            operation_id,
            operation_type,
            source_type,
            source_key,
            source_object_id,
            source_object_id,
            "2026-07-24T10:27:06Z",
            "smoke",
            len(lines),
            sum(lines.values()),
            sum(abs(value) for value in lines.values()),
            "[]",
            "{}",
            "",
            "",
            "",
        ),
    )
    conn.executemany(
        """
        INSERT INTO sheet_vitrina_v1_ff_stock_operation_lines(
            operation_id,line_no,nm_id,barcode,sku,nomenclature_name,
            comment,group_name,quantity_delta,raw_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                operation_id,
                line_no,
                nm_id,
                "",
                "",
                "",
                "",
                "",
                quantity,
                "{}",
            )
            for line_no, (nm_id, quantity) in enumerate(
                sorted(lines.items()), start=1
            )
        ],
    )


def main() -> None:
    declared = {
        SKU_SHORT: 750,
        SKU_SURPLUS: 1250,
        SKU_ONE_A: 500,
        SKU_ONE_B: 250,
        SKU_ONE_C: 500,
    }
    accepted = {
        SKU_SHORT: 500,
        SKU_SURPLUS: 1499,
        SKU_ONE_A: 499,
        SKU_ONE_B: 249,
        SKU_ONE_C: 499,
    }
    boxes = {nm_id: 250 for nm_id in declared}

    pending = solve_unique_box_correction(
        declared=declared,
        accepted=accepted,
        factory_box_sizes=boxes,
        final_acceptance=False,
    )
    assert pending["status"] == "requires_matching"
    assert sum(pending["gross_shortage"].values()) == 253
    assert sum(pending["gross_surplus"].values()) == 249

    solution = solve_unique_box_correction(
        declared=declared,
        accepted=accepted,
        factory_box_sizes=boxes,
        final_acceptance=True,
    )
    assert solution["status"] == "unique"
    assert solution["corrected"][SKU_SHORT] == 500
    assert solution["corrected"][SKU_SURPLUS] == 1500
    discrepancy = sum(
        max(solution["corrected"][nm_id] - accepted.get(nm_id, 0), 0)
        for nm_id in solution["corrected"]
    )
    assert discrepancy == 4

    ambiguous = solve_unique_box_correction(
        declared={101: 250, 102: 250, 103: 0},
        accepted={101: 0, 102: 0, 103: 250},
        factory_box_sizes={101: 250, 102: 250, 103: 250},
        final_acceptance=True,
    )
    assert ambiguous["status"] == "requires_matching"
    assert ambiguous["reason"] == "ambiguous_whole_box_solution"
    assert ambiguous["solution_count"] == 2

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _schema(conn)
    receipts = {nm_id: quantity + 500 for nm_id, quantity in declared.items()}
    _operation(
        conn,
        operation_id="receipt",
        operation_type="supplier_receipt",
        source_type="supplier_shipment",
        source_key="receipt",
        source_object_id="shipment",
        lines=receipts,
    )
    _operation(
        conn,
        operation_id="old_debit",
        operation_type="auto_writeoff",
        source_type="wb_supply",
        source_key="wb_supply_debit:supply:40985996",
        source_object_id="40985996",
        lines={nm_id: -quantity for nm_id, quantity in declared.items()},
    )
    conn.commit()

    conn.execute("BEGIN IMMEDIATE")
    applied = apply_unique_box_correction(
        conn,
        supply_id="40985996",
        source_revision="sha256:fresh-official-goods",
        solution=solution,
        actor="smoke",
        created_at="2026-07-25T12:00:00Z",
    )
    conn.commit()
    assert applied["applied"] is True
    assert applied["physical_adjustment"] == {
        SKU_SURPLUS: -250,
        SKU_SHORT: 250,
    }
    adjustment = {
        int(row["nm_id"]): int(row["quantity_delta"])
        for row in conn.execute(
            """
            SELECT nm_id,quantity_delta
            FROM sheet_vitrina_v1_ff_stock_operation_lines
            WHERE operation_id=?
            """,
            (applied["ff_adjustment_operation_id"],),
        )
    }
    assert adjustment == {SKU_SURPLUS: -250, SKU_SHORT: 250}

    second = apply_unique_box_correction(
        conn,
        supply_id="40985996",
        source_revision="sha256:fresh-official-goods",
        solution=solution,
        actor="smoke",
        created_at="2026-07-25T12:00:01Z",
    )
    assert second["idempotent"] is True
    assert conn.execute(
        "SELECT COUNT(*) FROM sheet_vitrina_v1_wb_supply_box_corrections"
    ).fetchone()[0] == 1
    assert conn.execute(
        """
        SELECT COUNT(*) FROM sheet_vitrina_v1_ff_stock_operations
        WHERE operation_type='box_correction'
        """
    ).fetchone()[0] == 1
    print("wb_supply_box_correction_smoke: OK")


if __name__ == "__main__":
    main()
