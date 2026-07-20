"""Shared Finance consumer for canonical ``Our WB Cost`` history."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import sqlite3
from typing import Any, Mapping


CANONICAL_COST_POLICY_DATE = date(2026, 7, 1)
CANONICAL_COST_FORMULA_VERSION = "wb_finance_canonical_our_wb_cost_v2"
FUNCTIONAL_CUTOVER_ID = "warehouse_functional_cutover_v1"
FUNCTIONAL_DAILY_TABLE = "sheet_vitrina_v1_warehouse_wb_daily_cost"
FORBIDDEN_QUALITIES = frozenset(
    {"fallback", "fallback_average", "zero_quantity_without_cost_basis"}
)


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _digest(payload: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def resolve_finance_canonical_cost(
    conn: sqlite3.Connection,
    *,
    nm_id: str,
    operation_date: date,
) -> dict[str, Any]:
    """Resolve Finance cost without maintaining an independent business value.

    Operations before 2026-07-01 project the exact canonical value from
    2026-07-01 backwards.  Operations on/after the boundary use the exact
    operation-date value.  Missing, non-positive, or warehouse fallback-average
    states are explicit blockers.
    """

    normalized_nm = str(nm_id or "").strip()
    source_date = (
        CANONICAL_COST_POLICY_DATE
        if operation_date < CANONICAL_COST_POLICY_DATE
        else operation_date
    )
    method = (
        "canonical_2026_07_01_projected_backwards"
        if operation_date < CANONICAL_COST_POLICY_DATE
        else "canonical_exact_operation_date"
    )
    base = {
        "nm_id": normalized_nm,
        "operation_date": operation_date.isoformat(),
        "canonical_source_date": source_date.isoformat(),
        "selection_method": method,
        "formula_version": CANONICAL_COST_FORMULA_VERSION,
        "source_table": FUNCTIONAL_DAILY_TABLE,
    }
    if not normalized_nm or not normalized_nm.isdigit() or int(normalized_nm) <= 0:
        return {**base, "status": "missing", "reason": "canonical_nm_id_unresolved"}
    required_tables = {
        str(row["name"])
        for row in conn.execute(
            """SELECT name FROM sqlite_master
               WHERE type='table' AND name IN (?,?)""",
            (FUNCTIONAL_DAILY_TABLE, "sheet_vitrina_v1_warehouse_functional_cutovers"),
        ).fetchall()
    }
    if FUNCTIONAL_DAILY_TABLE not in required_tables:
        return {**base, "status": "missing", "reason": "canonical_cost_table_missing"}
    if "sheet_vitrina_v1_warehouse_functional_cutovers" not in required_tables:
        return {**base, "status": "missing", "reason": "canonical_cost_cutover_table_missing"}
    cutover = conn.execute(
        """SELECT status,plan_fingerprint FROM sheet_vitrina_v1_warehouse_functional_cutovers
           WHERE cutover_id=?""",
        (FUNCTIONAL_CUTOVER_ID,),
    ).fetchone()
    if cutover is None or str(cutover["status"] or "") != "posted":
        return {**base, "status": "missing", "reason": "canonical_cost_cutover_not_posted"}
    row = conn.execute(
        f"""SELECT cutover_id,as_of_date,nm_id,quantity,wac_rub,capital_rub,
                   quality,provenance_json,fingerprint,created_at
            FROM {FUNCTIONAL_DAILY_TABLE}
            WHERE cutover_id=? AND as_of_date=? AND nm_id=?""",
        (FUNCTIONAL_CUTOVER_ID, source_date.isoformat(), normalized_nm),
    ).fetchone()
    if row is None:
        return {**base, "status": "missing", "reason": "canonical_cost_exact_date_missing"}
    quality = str(row["quality"] or "missing")
    unit_cost = _decimal_or_none(row["wac_rub"])
    source_payload = {
        "cutover_id": str(row["cutover_id"]),
        "as_of_date": str(row["as_of_date"]),
        "nm_id": str(row["nm_id"]),
        "quantity": str(row["quantity"]),
        "wac_rub": str(row["wac_rub"]),
        "capital_rub": str(row["capital_rub"]),
        "quality": quality,
        "provenance_json": str(row["provenance_json"] or "{}"),
        "fingerprint": str(row["fingerprint"] or ""),
        "created_at": str(row["created_at"] or ""),
        "cutover_plan_fingerprint": str(cutover["plan_fingerprint"] or ""),
    }
    source_digest = _digest(source_payload)
    source_identity = (
        f"{FUNCTIONAL_CUTOVER_ID}:{source_date.isoformat()}:{normalized_nm}:"
        f"{source_payload['fingerprint']}"
    )
    if quality.casefold() in FORBIDDEN_QUALITIES:
        return {
            **base,
            "status": "missing",
            "reason": "canonical_cost_forbidden_fallback_quality",
            "quality": quality,
            "canonical_source_identity": source_identity,
            "source_digest": source_digest,
        }
    if unit_cost is None or unit_cost <= 0:
        return {
            **base,
            "status": "missing",
            "reason": "canonical_cost_non_positive_or_missing",
            "quality": quality,
            "canonical_source_identity": source_identity,
            "source_digest": source_digest,
        }
    return {
        **base,
        "status": "resolved",
        "reason": "",
        "quality": quality,
        "unit_cost_rub": format(unit_cost, "f"),
        "canonical_source_identity": source_identity,
        "canonical_source_version": source_payload["fingerprint"],
        "source_digest": source_digest,
        "source_row": source_payload,
        "projection_quality": (
            "business_approved_retro_projection"
            if operation_date < CANONICAL_COST_POLICY_DATE
            else "canonical_exact"
        ),
    }
