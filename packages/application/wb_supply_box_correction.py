"""Unique whole-box correction of declared WB supply composition."""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import sqlite3
from typing import Any, Iterable, Mapping


BOX_CORRECTION_TABLE = "sheet_vitrina_v1_wb_supply_box_corrections"


def ensure_wb_supply_box_correction_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {BOX_CORRECTION_TABLE}(
            correction_id TEXT PRIMARY KEY,
            supply_id TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            status TEXT NOT NULL,
            gross_shortage_json TEXT NOT NULL,
            gross_surplus_json TEXT NOT NULL,
            declared_composition_json TEXT NOT NULL,
            accepted_composition_json TEXT NOT NULL,
            corrected_composition_json TEXT NOT NULL,
            box_deltas_json TEXT NOT NULL,
            solution_count INTEGER NOT NULL,
            plan_fingerprint TEXT NOT NULL UNIQUE,
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL,
            applied_at TEXT,
            ff_adjustment_operation_id TEXT,
            rollback_manifest_json TEXT NOT NULL DEFAULT '{{}}',
            UNIQUE(supply_id,source_revision)
        )
        """
    )
    existing = {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({BOX_CORRECTION_TABLE})")
    }
    if "ff_adjustment_operation_id" not in existing:
        conn.execute(
            f"ALTER TABLE {BOX_CORRECTION_TABLE} "
            "ADD COLUMN ff_adjustment_operation_id TEXT"
        )
    if "rollback_manifest_json" not in existing:
        conn.execute(
            f"ALTER TABLE {BOX_CORRECTION_TABLE} "
            "ADD COLUMN rollback_manifest_json TEXT NOT NULL DEFAULT '{{}}'"
        )


def solve_unique_box_correction(
    *,
    declared: Mapping[int, int],
    accepted: Mapping[int, int],
    factory_box_sizes: Mapping[int, int],
    final_acceptance: bool,
) -> dict[str, Any]:
    declared_qty = {
        int(nm_id): int(quantity)
        for nm_id, quantity in declared.items()
        if int(nm_id) > 0 and int(quantity) >= 0
    }
    accepted_qty = {
        int(nm_id): int(quantity)
        for nm_id, quantity in accepted.items()
        if int(nm_id) > 0 and int(quantity) >= 0
    }
    scope = sorted(set(declared_qty) | set(accepted_qty))
    gross_shortage = {
        nm_id: max(declared_qty.get(nm_id, 0) - accepted_qty.get(nm_id, 0), 0)
        for nm_id in scope
        if declared_qty.get(nm_id, 0) > accepted_qty.get(nm_id, 0)
    }
    gross_surplus = {
        nm_id: max(accepted_qty.get(nm_id, 0) - declared_qty.get(nm_id, 0), 0)
        for nm_id in scope
        if accepted_qty.get(nm_id, 0) > declared_qty.get(nm_id, 0)
    }
    base = {
        "final_acceptance": bool(final_acceptance),
        "gross_shortage": gross_shortage,
        "gross_surplus": gross_surplus,
        "declared": declared_qty,
        "accepted": accepted_qty,
    }
    if not final_acceptance:
        return {
            **base,
            "status": "requires_matching",
            "reason": "acceptance_not_final",
            "solution_count": 0,
        }
    if not gross_surplus:
        return {
            **base,
            "status": "no_correction",
            "reason": "no_cross_sku_surplus",
            "solution_count": 0,
        }
    missing_box = [
        nm_id
        for nm_id in scope
        if int(factory_box_sizes.get(nm_id) or 0) <= 0
    ]
    if missing_box:
        return {
            **base,
            "status": "requires_matching",
            "reason": "factory_box_size_missing",
            "missing_box_size_nm_ids": missing_box,
            "solution_count": 0,
        }

    options: list[tuple[int, list[int]]] = []
    for nm_id in scope:
        box = int(factory_box_sizes[nm_id])
        removable = max(
            (declared_qty.get(nm_id, 0) - accepted_qty.get(nm_id, 0)) // box,
            0,
        )
        required_add = max(
            -(
                -(
                    accepted_qty.get(nm_id, 0)
                    - declared_qty.get(nm_id, 0)
                )
                // box
            ),
            0,
        )
        options.append((nm_id, list(range(-removable, required_add + 1))))

    candidates: list[dict[int, int]] = []

    def visit(index: int, deltas: dict[int, int], quantity_delta: int) -> None:
        if index == len(options):
            if quantity_delta != 0 or not any(deltas.values()):
                return
            corrected = {
                nm_id: declared_qty.get(nm_id, 0)
                + deltas.get(nm_id, 0) * int(factory_box_sizes[nm_id])
                for nm_id in scope
            }
            if sum(corrected.values()) != sum(declared_qty.values()):
                return
            if any(
                corrected[nm_id] < accepted_qty.get(nm_id, 0)
                for nm_id in scope
            ):
                return
            candidates.append(dict(deltas))
            return
        nm_id, values = options[index]
        box = int(factory_box_sizes[nm_id])
        for boxes in values:
            deltas[nm_id] = boxes
            visit(index + 1, deltas, quantity_delta + boxes * box)
        deltas.pop(nm_id, None)

    visit(0, {}, 0)
    if not candidates:
        return {
            **base,
            "status": "requires_matching",
            "reason": "no_whole_box_solution",
            "solution_count": 0,
        }
    score = min(sum(abs(value) for value in item.values()) for item in candidates)
    optimal = [
        item
        for item in candidates
        if sum(abs(value) for value in item.values()) == score
    ]
    if len(optimal) != 1:
        return {
            **base,
            "status": "requires_matching",
            "reason": "ambiguous_whole_box_solution",
            "solution_count": len(optimal),
            "candidate_box_deltas": optimal[:20],
        }
    box_deltas = optimal[0]
    corrected = {
        nm_id: declared_qty.get(nm_id, 0)
        + box_deltas.get(nm_id, 0) * int(factory_box_sizes[nm_id])
        for nm_id in scope
    }
    material = {
        **base,
        "factory_box_sizes": {
            nm_id: int(factory_box_sizes[nm_id]) for nm_id in scope
        },
        "corrected": corrected,
        "box_deltas": box_deltas,
    }
    return {
        **material,
        "status": "unique",
        "reason": "unique_minimum_whole_box_solution",
        "solution_count": 1,
        "replacement_count": score // 2,
        "plan_fingerprint": "sha256:"
        + hashlib.sha256(
            json.dumps(
                material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }


def corrected_goods(
    goods: Iterable[Mapping[str, Any]],
    corrected_composition: Mapping[int, int],
) -> list[dict[str, Any]]:
    result = [dict(item) for item in goods if isinstance(item, Mapping)]
    index: dict[int, dict[str, Any]] = {}
    for item in result:
        nm_id = int(
            item.get("nmID")
            or item.get("nmId")
            or item.get("nm_id")
            or 0
        )
        if nm_id > 0:
            index[nm_id] = item
    for raw_nm_id, raw_quantity in corrected_composition.items():
        nm_id = int(raw_nm_id)
        quantity = int(raw_quantity)
        item = index.get(nm_id)
        if item is None:
            item = {
                "nmID": nm_id,
                "acceptedQuantity": 0,
                "boxCorrectionAdded": True,
            }
            result.append(item)
            index[nm_id] = item
        if "quantity" in item or "qty" not in item:
            item["quantity"] = quantity
        else:
            item["qty"] = quantity
        item["boxCorrectionApplied"] = True
    return result


def apply_unique_box_correction(
    conn: sqlite3.Connection,
    *,
    supply_id: str,
    source_revision: str,
    solution: Mapping[str, Any],
    actor: str,
    created_at: str,
) -> dict[str, Any]:
    """Atomically publish one unique correction and its FF compensation."""

    ensure_wb_supply_box_correction_schema(conn)
    if str(solution.get("status") or "") != "unique":
        raise ValueError("only a unique whole-box solution can be applied")
    fingerprint = str(solution.get("plan_fingerprint") or "")
    if not fingerprint.startswith("sha256:"):
        raise ValueError("box correction plan fingerprint is missing")
    existing = conn.execute(
        f"SELECT * FROM {BOX_CORRECTION_TABLE} WHERE plan_fingerprint=?",
        (fingerprint,),
    ).fetchone()
    if existing is not None:
        return {
            "applied": False,
            "idempotent": True,
            "correction_id": str(existing["correction_id"]),
            "ff_adjustment_operation_id": str(
                existing["ff_adjustment_operation_id"] or ""
            ),
        }

    declared = {
        int(key): int(value)
        for key, value in dict(solution.get("declared") or {}).items()
    }
    corrected = {
        int(key): int(value)
        for key, value in dict(solution.get("corrected") or {}).items()
    }
    adjustment = {
        nm_id: declared.get(nm_id, 0) - corrected.get(nm_id, 0)
        for nm_id in sorted(set(declared) | set(corrected))
        if declared.get(nm_id, 0) != corrected.get(nm_id, 0)
    }
    debit_rows = conn.execute(
        """
        SELECT operation_id FROM sheet_vitrina_v1_ff_stock_operations
        WHERE operation_type='auto_writeoff' AND source_type='wb_supply'
          AND (source_object_id=? OR source_key=?)
        ORDER BY created_at,operation_id
        """,
        (supply_id, f"wb_supply_debit:{supply_id}"),
    ).fetchall()
    if len(debit_rows) > 1:
        raise ValueError("box correction found duplicate physical supply debits")
    adjustment_operation_id = ""
    rollback_manifest = {
        "supply_id": supply_id,
        "source_revision": source_revision,
        "plan_fingerprint": fingerprint,
        "declared": declared,
        "corrected": corrected,
        "physical_adjustment": adjustment,
        "prior_debit_operation_id": (
            str(debit_rows[0]["operation_id"]) if debit_rows else ""
        ),
    }
    if debit_rows:
        debit_operation_id = str(debit_rows[0]["operation_id"])
        debited = {
            int(row["nm_id"]): int(
                Decimal(str(row["quantity"])).copy_abs()
            )
            for row in conn.execute(
                """
                SELECT nm_id,SUM(quantity_delta) quantity
                FROM sheet_vitrina_v1_ff_stock_operation_lines
                WHERE operation_id=? GROUP BY nm_id ORDER BY nm_id
                """,
                (debit_operation_id,),
            ).fetchall()
        }
        if debited != declared:
            raise ValueError(
                "existing physical debit does not equal declared composition"
            )
        balances = {
            int(row["nm_id"]): Decimal(str(row["quantity"]))
            for row in conn.execute(
                """
                SELECT nm_id,SUM(quantity_delta) quantity
                FROM sheet_vitrina_v1_ff_stock_operation_lines
                GROUP BY nm_id
                """
            ).fetchall()
        }
        negative = {
            nm_id: str(balances.get(nm_id, Decimal("0")) + Decimal(delta))
            for nm_id, delta in adjustment.items()
            if balances.get(nm_id, Decimal("0")) + Decimal(delta) < 0
        }
        if negative:
            raise ValueError(
                "box correction has insufficient physical FF stock: "
                + json.dumps(negative, sort_keys=True)
            )
        adjustment_operation_id = (
            "ffso_box_" + fingerprint.removeprefix("sha256:")[:24]
        )
        source_key = (
            f"wb_supply_box_correction:{supply_id}:"
            + fingerprint.removeprefix("sha256:")[:16]
        )
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
                adjustment_operation_id,
                "box_correction",
                "wb_supply_box_correction",
                source_key,
                supply_id,
                supply_id,
                created_at,
                actor,
                len(adjustment),
                float(sum(adjustment.values())),
                float(sum(abs(value) for value in adjustment.values())),
                "[]",
                json.dumps(rollback_manifest, ensure_ascii=False, sort_keys=True),
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
                    adjustment_operation_id,
                    index,
                    nm_id,
                    "",
                    "",
                    "",
                    "whole-box supply composition correction",
                    "",
                    float(delta),
                    json.dumps(
                        {
                            "supply_id": supply_id,
                            "plan_fingerprint": fingerprint,
                            "declared_quantity": declared.get(nm_id, 0),
                            "corrected_quantity": corrected.get(nm_id, 0),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
                for index, (nm_id, delta) in enumerate(
                    sorted(adjustment.items()), start=1
                )
            ],
        )

    correction_id = (
        "wbbox_" + fingerprint.removeprefix("sha256:")[:24]
    )
    conn.execute(
        f"""
        INSERT INTO {BOX_CORRECTION_TABLE}(
            correction_id,supply_id,source_revision,status,
            gross_shortage_json,gross_surplus_json,
            declared_composition_json,accepted_composition_json,
            corrected_composition_json,box_deltas_json,solution_count,
            plan_fingerprint,actor,created_at,applied_at,
            ff_adjustment_operation_id,rollback_manifest_json
        ) VALUES(?,?,?,'applied',?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            correction_id,
            supply_id,
            source_revision,
            json.dumps(
                solution.get("gross_shortage") or {},
                ensure_ascii=False,
                sort_keys=True,
            ),
            json.dumps(
                solution.get("gross_surplus") or {},
                ensure_ascii=False,
                sort_keys=True,
            ),
            json.dumps(declared, ensure_ascii=False, sort_keys=True),
            json.dumps(
                solution.get("accepted") or {},
                ensure_ascii=False,
                sort_keys=True,
            ),
            json.dumps(corrected, ensure_ascii=False, sort_keys=True),
            json.dumps(
                solution.get("box_deltas") or {},
                ensure_ascii=False,
                sort_keys=True,
            ),
            int(solution.get("solution_count") or 0),
            fingerprint,
            actor,
            created_at,
            created_at,
            adjustment_operation_id or None,
            json.dumps(rollback_manifest, ensure_ascii=False, sort_keys=True),
        ),
    )
    return {
        "applied": True,
        "idempotent": False,
        "correction_id": correction_id,
        "ff_adjustment_operation_id": adjustment_operation_id,
        "physical_adjustment": adjustment,
        "rollback_manifest": rollback_manifest,
    }


def load_active_box_correction(
    conn: sqlite3.Connection,
    supply_id: str,
) -> dict[str, Any] | None:
    ensure_wb_supply_box_correction_schema(conn)
    row = conn.execute(
        f"""
        SELECT * FROM {BOX_CORRECTION_TABLE}
        WHERE supply_id=? AND status='applied'
        ORDER BY applied_at DESC,created_at DESC LIMIT 1
        """,
        (str(supply_id or ""),),
    ).fetchone()
    if row is None:
        return None
    item = dict(row)
    for key in (
        "gross_shortage",
        "gross_surplus",
        "declared_composition",
        "accepted_composition",
        "corrected_composition",
        "box_deltas",
    ):
        item[key] = json.loads(str(item.pop(key + "_json") or "{}"))
    return item
