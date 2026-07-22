"""Shared Finance consumer for canonical ``Our WB Cost`` history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import sqlite3
from typing import Any, Mapping

from packages.application.warehouse_archival_estimate import (
    QUALITY as BUSINESS_APPROVED_ARCHIVAL_ESTIMATE_QUALITY,
    active_archival_estimates,
    archival_estimate_for_nm_id,
)


CANONICAL_COST_POLICY_DATE = date(2026, 7, 1)
CANONICAL_COST_FORMULA_VERSION = "wb_finance_canonical_our_wb_cost_v3"
FUNCTIONAL_CUTOVER_ID = "warehouse_functional_cutover_v1"
FUNCTIONAL_DAILY_TABLE = "sheet_vitrina_v1_warehouse_wb_daily_cost"
FORBIDDEN_QUALITIES = frozenset(
    {"fallback", "fallback_average", "zero_quantity_without_cost_basis"}
)


@dataclass(frozen=True)
class CanonicalWbCostSnapshot:
    """Coherent read-only source snapshot for high-volume cost resolution.

    Finance rebuilds resolve millions of operations through a comparatively
    small canonical daily-cost surface.  Loading that surface and the active
    archival overlay once per SQLite connection preserves the same source
    semantics while avoiding a fresh archival/event scan for every SKU/date.
    A caller must never reuse this object with another connection.
    """

    table_names: frozenset[str]
    cutover: Mapping[str, Any] | None
    daily_rows: Mapping[tuple[str, str], Mapping[str, Any]]
    archival_rows: Mapping[int, Mapping[str, Any]]
    archival_first_factual_dates: Mapping[int, str]

    @classmethod
    def from_connection(cls, conn: sqlite3.Connection) -> "CanonicalWbCostSnapshot":
        table_names = frozenset(
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        )
        cutover: Mapping[str, Any] | None = None
        if "sheet_vitrina_v1_warehouse_functional_cutovers" in table_names:
            row = conn.execute(
                """SELECT status,plan_fingerprint
                   FROM sheet_vitrina_v1_warehouse_functional_cutovers
                   WHERE cutover_id=?""",
                (FUNCTIONAL_CUTOVER_ID,),
            ).fetchone()
            cutover = dict(row) if row is not None else None

        daily_rows: dict[tuple[str, str], Mapping[str, Any]] = {}
        if FUNCTIONAL_DAILY_TABLE in table_names:
            for row in conn.execute(
                f"""SELECT cutover_id,as_of_date,nm_id,quantity,wac_rub,capital_rub,
                            quality,provenance_json,fingerprint,created_at
                     FROM {FUNCTIONAL_DAILY_TABLE}
                     WHERE cutover_id=?
                     ORDER BY as_of_date,nm_id""",
                (FUNCTIONAL_CUTOVER_ID,),
            ):
                item = dict(row)
                daily_rows[(str(item["as_of_date"]), str(item["nm_id"]))] = item

        archival_rows = active_archival_estimates(conn)
        first_factual_dates: dict[int, str] = {}
        if (
            archival_rows
            and "sheet_vitrina_v1_warehouse_functional_events" in table_names
        ):
            nm_ids = sorted(archival_rows)
            placeholders = ",".join("?" for _ in nm_ids)
            first_factual_dates = {
                int(row["nm_id"]): str(row["first_business_date"])
                for row in conn.execute(
                    f"""SELECT nm_id,MIN(business_date) AS first_business_date
                         FROM sheet_vitrina_v1_warehouse_functional_events
                         WHERE event_type='wb_final_acceptance'
                           AND nm_id IN ({placeholders})
                           AND business_date IS NOT NULL AND business_date!=''
                           AND (CAST(quantity AS REAL)!=0 OR CAST(capital_rub AS REAL)!=0)
                         GROUP BY nm_id""",
                    tuple(nm_ids),
                ).fetchall()
            }
        return cls(
            table_names=table_names,
            cutover=cutover,
            daily_rows=daily_rows,
            archival_rows=archival_rows,
            archival_first_factual_dates=first_factual_dates,
        )

    def archival_estimate(
        self, *, nm_id: str, as_of_date: str
    ) -> Mapping[str, Any] | None:
        item = self.archival_rows.get(int(nm_id))
        if item is None or as_of_date < str(item["effective_date"]):
            return None
        if as_of_date >= self.archival_first_factual_dates.get(int(nm_id), "9999-12-31"):
            return None
        return item


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
    snapshot: CanonicalWbCostSnapshot | None = None,
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
    required_tables = (
        snapshot.table_names
        if snapshot is not None
        else {
            str(row["name"])
            for row in conn.execute(
                """SELECT name FROM sqlite_master
                   WHERE type='table' AND name IN (?,?)""",
                (
                    FUNCTIONAL_DAILY_TABLE,
                    "sheet_vitrina_v1_warehouse_functional_cutovers",
                ),
            ).fetchall()
        }
    )
    if FUNCTIONAL_DAILY_TABLE not in required_tables:
        return {**base, "status": "missing", "reason": "canonical_cost_table_missing"}
    if "sheet_vitrina_v1_warehouse_functional_cutovers" not in required_tables:
        return {**base, "status": "missing", "reason": "canonical_cost_cutover_table_missing"}
    cutover = (
        snapshot.cutover
        if snapshot is not None
        else conn.execute(
            """SELECT status,plan_fingerprint
               FROM sheet_vitrina_v1_warehouse_functional_cutovers
               WHERE cutover_id=?""",
            (FUNCTIONAL_CUTOVER_ID,),
        ).fetchone()
    )
    if cutover is None or str(cutover["status"] or "") != "posted":
        return {**base, "status": "missing", "reason": "canonical_cost_cutover_not_posted"}
    if snapshot is not None:
        row = snapshot.daily_rows.get((source_date.isoformat(), normalized_nm))
        estimate = snapshot.archival_estimate(
            nm_id=normalized_nm,
            as_of_date=source_date.isoformat(),
        )
    else:
        row = conn.execute(
            f"""SELECT cutover_id,as_of_date,nm_id,quantity,wac_rub,capital_rub,
                       quality,provenance_json,fingerprint,created_at
                FROM {FUNCTIONAL_DAILY_TABLE}
                WHERE cutover_id=? AND as_of_date=? AND nm_id=?""",
            (FUNCTIONAL_CUTOVER_ID, source_date.isoformat(), normalized_nm),
        ).fetchone()
        estimate = archival_estimate_for_nm_id(
            conn,
            nm_id=normalized_nm,
            as_of_date=source_date.isoformat(),
        )
    if row is None:
        if estimate is None:
            return {**base, "status": "missing", "reason": "canonical_cost_exact_date_missing"}
        source_payload = {
            "version_id": str(estimate["version_id"]),
            "effective_date": str(estimate["effective_date"]),
            "nm_id": normalized_nm,
            "unit_cost_rub": str(estimate["unit_cost_rub"]),
            "quality": str(estimate["quality"]),
            "owner_approval_reference": str(estimate["owner_approval_reference"]),
            "manifest_digest": str(estimate["manifest_digest"]),
            "production_dry_run_plan_sha256": str(
                estimate["production_dry_run_plan_sha256"]
            ),
            "source_digest": str(estimate["source_digest"]),
            "plan_fingerprint": str(estimate["plan_fingerprint"]),
            "row_fingerprint": str(estimate["row_fingerprint"]),
            "lineage": dict(estimate.get("lineage") or {}),
        }
        source_digest = _digest(source_payload)
        source_identity = (
            f"{BUSINESS_APPROVED_ARCHIVAL_ESTIMATE_QUALITY}:"
            f"{estimate['version_id']}:{source_date.isoformat()}:{normalized_nm}:"
            f"{estimate['row_fingerprint']}"
        )
        return {
            **base,
            "status": "resolved",
            "reason": "",
            "quality": BUSINESS_APPROVED_ARCHIVAL_ESTIMATE_QUALITY,
            "unit_cost_rub": format(_decimal_or_none(estimate["unit_cost_rub"]) or Decimal("0"), "f"),
            "source_table": "sheet_vitrina_v1_warehouse_archival_estimate_rows",
            "canonical_source_identity": source_identity,
            "canonical_source_version": str(estimate["row_fingerprint"]),
            "source_digest": source_digest,
            "source_row": source_payload,
            "projection_quality": (
                "business_approved_retro_projection"
                if operation_date < CANONICAL_COST_POLICY_DATE
                else "business_approved_archival_estimate"
            ),
        }
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
    if quality == BUSINESS_APPROVED_ARCHIVAL_ESTIMATE_QUALITY and estimate is None:
        source_digest = _digest(source_payload)
        return {
            **base,
            "status": "missing",
            "reason": "canonical_archival_estimate_superseded_pending_replay",
            "quality": quality,
            "canonical_source_identity": (
                f"{FUNCTIONAL_CUTOVER_ID}:{source_date.isoformat()}:{normalized_nm}:"
                f"{source_payload['fingerprint']}"
            ),
            "source_digest": source_digest,
        }
    if quality == BUSINESS_APPROVED_ARCHIVAL_ESTIMATE_QUALITY and estimate is not None:
        source_payload["business_approved_lineage"] = {
            key: estimate[key]
            for key in (
                "version_id",
                "effective_date",
                "owner_approval_reference",
                "manifest_digest",
                "production_dry_run_plan_sha256",
                "source_digest",
                "plan_fingerprint",
                "row_fingerprint",
            )
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
