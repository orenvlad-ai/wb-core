"""Shared channel/location-aware resolver for realized sale COGS.

Finance and Partner Report use this contract: FBS resolves the exact
facility/pool/SKU WAC frozen by the durable handoff event, while FBO/WB keeps
the canonical daily WB WAC.  The Vitrina ``Себестоимость наша`` and indicative
Proxy 3/4 deliberately use the separate as-of WB+FF inventory blend; they do
not turn that informational average into transaction COGS.  No realized-cost
consumer may substitute a different facility, SKU, average, legacy value, or
zero.
"""

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
CANONICAL_COST_FORMULA_VERSION = "canonical_our_wb_cost_temporal_policy_v4"
CHANNEL_LOCATION_COST_FORMULA_VERSION = "canonical_our_cost_channel_location_v1"
FUNCTIONAL_CUTOVER_ID = "warehouse_functional_cutover_v1"
FUNCTIONAL_DAILY_TABLE = "sheet_vitrina_v1_warehouse_wb_daily_cost"
FBS_OBSERVATIONS_TABLE = "sheet_vitrina_v1_wb_supplies_fbs_order_observations"
FBS_CURRENT_TABLE = "sheet_vitrina_v1_ff_pool_fbs_lifecycle_current"
FBS_EVENTS_TABLE = "sheet_vitrina_v1_ff_pool_fbs_lifecycle_events"
FBS_CUTOVER_TABLE = "sheet_vitrina_v1_ff_pool_cutover_manifests"
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


@dataclass(frozen=True)
class CanonicalChannelCostSnapshot:
    """Coherent WB plus privacy-safe exact FBS order/cost indexes."""

    wb: CanonicalWbCostSnapshot
    fbs_order_ids_by_identity_hash: Mapping[str, tuple[int, ...]]
    fbs_cost_by_order_id: Mapping[int, Mapping[str, Any]]

    @classmethod
    def from_connection(cls, conn: sqlite3.Connection) -> "CanonicalChannelCostSnapshot":
        wb = CanonicalWbCostSnapshot.from_connection(conn)
        identity_index: dict[str, set[int]] = {}
        costs: dict[int, Mapping[str, Any]] = {}
        required = {
            FBS_OBSERVATIONS_TABLE,
            FBS_CURRENT_TABLE,
            FBS_EVENTS_TABLE,
            FBS_CUTOVER_TABLE,
        }
        if required <= set(wb.table_names):
            observation_columns = {
                str(row[1])
                for row in conn.execute(
                    f"PRAGMA table_info({FBS_OBSERVATIONS_TABLE})"
                ).fetchall()
            }
            if {"rid_sha256", "order_uid_sha256"} <= observation_columns:
                for row in conn.execute(
                    f"""SELECT order_id,rid_sha256,order_uid_sha256
                         FROM {FBS_OBSERVATIONS_TABLE}
                         WHERE rid_sha256<>'' OR order_uid_sha256<>''
                         ORDER BY observation_sequence"""
                ).fetchall():
                    for value in (row[1], row[2]):
                        token = str(value or "")
                        if token:
                            identity_index.setdefault(token, set()).add(int(row[0]))
            latest = conn.execute(
                f"SELECT cutover_id FROM {FBS_CUTOVER_TABLE} "
                "ORDER BY cutover_at DESC,cutover_id DESC LIMIT 1"
            ).fetchone()
            if latest is not None:
                for row in conn.execute(
                    f"""SELECT current.order_id,current.facility_id,current.pool,
                                current.nm_id,current.quantity,current.frozen_wac_rub,
                                current.debit_event_id,event.event_type,
                                event.event_sequence,event.evidence_digest,event.occurred_at,
                                event.details_json,event.source_observed_at
                         FROM {FBS_CURRENT_TABLE} AS current
                         JOIN {FBS_EVENTS_TABLE} AS event
                           ON event.event_id=current.debit_event_id
                         WHERE current.cutover_id=? AND current.debit_event_id<>''
                         ORDER BY current.order_id""",
                    (str(latest[0]),),
                ).fetchall():
                    costs[int(row[0])] = {
                        "order_id": int(row[0]),
                        "facility_id": str(row[1]),
                        "pool": str(row[2]),
                        "nm_id": int(row[3]),
                        "quantity": int(row[4]),
                        "frozen_wac_rub": str(row[5]),
                        "debit_event_id": str(row[6]),
                        "event_type": str(row[7]),
                        "event_sequence": int(row[8]),
                        "evidence_digest": str(row[9]),
                        "occurred_at": str(row[10]),
                        "details_json": str(row[11] or "{}"),
                        "source_observed_at": str(row[12] or ""),
                    }
        return cls(
            wb=wb,
            fbs_order_ids_by_identity_hash={
                key: tuple(sorted(values)) for key, values in identity_index.items()
            },
            fbs_cost_by_order_id=costs,
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


def canonical_cost_source_date(operation_date: date) -> date:
    """Return the only source date permitted by the canonical temporal policy."""

    return (
        CANONICAL_COST_POLICY_DATE
        if operation_date < CANONICAL_COST_POLICY_DATE
        else operation_date
    )


def resolve_canonical_wb_cost(
    conn: sqlite3.Connection,
    *,
    nm_id: str,
    operation_date: date,
    snapshot: CanonicalWbCostSnapshot | None = None,
) -> dict[str, Any]:
    """Resolve canonical WB cost without an independent consumer value.

    Operations before 2026-07-01 project the exact canonical value from
    2026-07-01 backwards.  Operations on/after the boundary use the exact
    operation-date value.  Missing, non-positive, or warehouse fallback-average
    states are explicit blockers.
    """

    normalized_nm = str(nm_id or "").strip()
    source_date = canonical_cost_source_date(operation_date)
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
            "canonical_source_version": source_payload["fingerprint"],
            "source_row": source_payload,
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
            "canonical_source_version": source_payload["fingerprint"],
            "source_row": source_payload,
        }
    if unit_cost is None or unit_cost <= 0:
        return {
            **base,
            "status": "missing",
            "reason": "canonical_cost_non_positive_or_missing",
            "quality": quality,
            "canonical_source_identity": source_identity,
            "source_digest": source_digest,
            "canonical_source_version": source_payload["fingerprint"],
            "source_row": source_payload,
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


def resolve_channel_location_cost(
    conn: sqlite3.Connection,
    *,
    nm_id: str,
    operation_date: date,
    operation: Mapping[str, Any] | None = None,
    fbs_order_id: int | None = None,
    snapshot: CanonicalChannelCostSnapshot | None = None,
) -> dict[str, Any]:
    """Resolve one sale/return through the single channel-aware contract.

    A privacy-safe hash match or an explicit ``fbs_order_id`` selects FBS and
    makes the frozen handoff event mandatory.  An explicit FBS channel without
    one unique exact order also fails closed.  Only rows not identified as FBS
    may use the canonical WB/FBO daily resolver.
    """

    state = snapshot or CanonicalChannelCostSnapshot.from_connection(conn)
    raw = dict(operation or {})
    identity_hashes = {
        _identity_hash(raw.get(key))
        for key in ("srid", "rid", "orderUid", "order_uid")
        if str(raw.get(key) or "").strip()
    }
    identity_hashes.discard("")
    matched_order_ids: set[int] = set()
    for identity_hash in identity_hashes:
        matched_order_ids.update(
            state.fbs_order_ids_by_identity_hash.get(identity_hash, ())
        )
    if fbs_order_id is not None and int(fbs_order_id) > 0:
        matched_order_ids.add(int(fbs_order_id))
    channel_tokens = {
        str(raw.get(key) or "").strip().casefold()
        for key in ("deliveryType", "delivery_type", "orderType", "order_type")
        if str(raw.get(key) or "").strip()
    }
    explicit_fbs = any("fbs" in token for token in channel_tokens)
    base = {
        "nm_id": str(nm_id or "").strip(),
        "operation_date": operation_date.isoformat(),
        "formula_version": CHANNEL_LOCATION_COST_FORMULA_VERSION,
        "identity_hashes": sorted(identity_hashes),
    }
    if len(matched_order_ids) > 1:
        return {
            **base,
            "status": "missing",
            "reason": "fbs_order_identity_ambiguous",
            "channel": "FBS",
            "pool": "FBS",
        }
    if matched_order_ids or explicit_fbs:
        if not matched_order_ids:
            return {
                **base,
                "status": "missing",
                "reason": "fbs_order_identity_missing",
                "channel": "FBS",
                "pool": "FBS",
            }
        order_id = next(iter(matched_order_ids))
        source = state.fbs_cost_by_order_id.get(order_id)
        if source is None:
            return {
                **base,
                "status": "missing",
                "reason": "fbs_handoff_cost_missing",
                "channel": "FBS",
                "pool": "FBS",
                "fbs_order_id": order_id,
            }
        if str(source.get("pool") or "") != "FBS":
            return {
                **base,
                "status": "missing",
                "reason": "fbs_pool_identity_drift",
                "channel": "FBS",
                "pool": str(source.get("pool") or ""),
                "fbs_order_id": order_id,
            }
        if str(source.get("nm_id") or "") != str(nm_id or "").strip():
            return {
                **base,
                "status": "missing",
                "reason": "fbs_order_nm_id_mismatch",
                "channel": "FBS",
                "pool": "FBS",
                "facility_id": str(source.get("facility_id") or ""),
                "fbs_order_id": order_id,
            }
        unit_cost = _decimal_or_none(source.get("frozen_wac_rub"))
        if unit_cost is None or unit_cost <= 0:
            return {
                **base,
                "status": "missing",
                "reason": "fbs_handoff_cost_non_positive",
                "channel": "FBS",
                "pool": "FBS",
                "facility_id": str(source.get("facility_id") or ""),
                "fbs_order_id": order_id,
            }
        source_payload = {
            key: source.get(key)
            for key in (
                "order_id",
                "facility_id",
                "pool",
                "nm_id",
                "quantity",
                "frozen_wac_rub",
                "debit_event_id",
                "event_type",
                "event_sequence",
                "evidence_digest",
                "occurred_at",
                "source_observed_at",
            )
        }
        source_digest = _digest(source_payload)
        return {
            **base,
            "status": "resolved",
            "reason": "",
            "channel": "FBS",
            "pool": "FBS",
            "facility_id": str(source["facility_id"]),
            "fbs_order_id": order_id,
            "unit_cost_rub": format(unit_cost, "f"),
            "quality": "frozen_handoff_exact",
            "selection_method": "exact_fbs_order_handoff_event",
            "canonical_source_date": operation_date.isoformat(),
            "canonical_source_identity": str(source["debit_event_id"]),
            "canonical_source_version": str(source["evidence_digest"]),
            "source_table": FBS_EVENTS_TABLE,
            "source_digest": source_digest,
            "source_row": source_payload,
            "projection_quality": "exact_sale_handoff_frozen_wac",
        }
    wb = resolve_canonical_wb_cost(
        conn,
        nm_id=nm_id,
        operation_date=operation_date,
        snapshot=state.wb,
    )
    return {
        **wb,
        "formula_version": CHANNEL_LOCATION_COST_FORMULA_VERSION,
        "channel": "WB",
        "pool": "FBO",
        "facility_id": "wb",
        "identity_hashes": sorted(identity_hashes),
    }


def _identity_hash(value: Any) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def resolve_finance_canonical_cost(
    conn: sqlite3.Connection,
    *,
    nm_id: str,
    operation_date: date,
    snapshot: CanonicalWbCostSnapshot | None = None,
) -> dict[str, Any]:
    """Compatibility name for the shared resolver used by Finance callers."""

    return resolve_canonical_wb_cost(
        conn,
        nm_id=nm_id,
        operation_date=operation_date,
        snapshot=snapshot,
    )


def load_canonical_wb_cost_lookup(
    conn: sqlite3.Connection,
    *,
    as_of_date: date,
) -> dict[int, dict[str, Any]]:
    """Load a Vitrina-ready lookup through the shared resolver.

    Before the policy boundary the unit value and its lineage come from the
    exact same-nmID row on 2026-07-01.  The source row quantity is exposed only
    as weighting evidence; this function never creates inventory or capital.
    Missing/forbidden/non-positive values stay ``None`` with an explicit reason.
    """

    snapshot = CanonicalWbCostSnapshot.from_connection(conn)
    source_date = canonical_cost_source_date(as_of_date).isoformat()
    candidates = {
        str(nm_id)
        for day, nm_id in snapshot.daily_rows
        if day == source_date
    }
    candidates.update(str(nm_id) for nm_id in snapshot.archival_rows)
    daily_profit_coverage: dict[int, dict[str, Any]] = {}
    if "wb_finance_weekly_sku_aggregates" in snapshot.table_names:
        for row in conn.execute(
            """SELECT nm_id,coverage_json
                 FROM wb_finance_weekly_sku_aggregates
                WHERE nm_id<>'__account__' AND week_start<=? AND week_end>=?
                ORDER BY calculated_at,nm_id""",
            (as_of_date.isoformat(), as_of_date.isoformat()),
        ).fetchall():
            try:
                coverage = json.loads(str(row[1] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            exact_day = next(
                (
                    dict(item)
                    for item in coverage.get("daily_rows") or []
                    if str(item.get("operation_date") or "")
                    == as_of_date.isoformat()
                ),
                None,
            )
            if exact_day is None:
                continue
            nm_id = int(row[0])
            candidates.add(str(nm_id))
            daily_profit_coverage[nm_id] = exact_day
    result: dict[int, dict[str, Any]] = {}
    for nm_id in sorted(candidates, key=lambda value: int(value)):
        resolved = resolve_canonical_wb_cost(
            conn,
            nm_id=nm_id,
            operation_date=as_of_date,
            snapshot=snapshot,
        )
        source_row = dict(resolved.get("source_row") or {})
        quantity = _decimal_or_none(source_row.get("quantity")) or Decimal("0")
        unit_cost = _decimal_or_none(resolved.get("unit_cost_rub"))
        quality = str(resolved.get("quality") or "missing")
        is_resolved = str(resolved.get("status") or "") == "resolved"
        certified = quality.casefold() in {"certified", "confirmed"}
        result[int(nm_id)] = {
            "as_of_date": as_of_date.isoformat(),
            "canonical_source_date": str(resolved["canonical_source_date"]),
            "nm_id": int(nm_id),
            "stock_qty": float(quantity),
            "cost_covered_qty": float(quantity) if is_resolved else 0.0,
            "our_wb_unit_cost_rub": float(unit_cost) if unit_cost is not None else None,
            "confirmed_qty": float(quantity) if certified and is_resolved else 0.0,
            "estimated_qty": float(quantity) if is_resolved and not certified else 0.0,
            "fallback_qty": 0.0,
            "confirmed_share_pct": (
                1.0 if certified and is_resolved and quantity > 0 else 0.0
            ),
            "source_status": quality,
            "source_reason": str(resolved.get("reason") or ""),
            "selection_method": str(resolved.get("selection_method") or ""),
            "projection_quality": str(resolved.get("projection_quality") or ""),
            "source_digest": str(resolved.get("source_digest") or ""),
            "canonical_source_identity": str(
                resolved.get("canonical_source_identity") or ""
            ),
            "component_status_json": json.dumps(
                {
                    "status": resolved.get("status"),
                    "reason": resolved.get("reason"),
                    "quality": resolved.get("quality"),
                    "projection_quality": resolved.get("projection_quality"),
                    "selection_method": resolved.get("selection_method"),
                    "canonical_source_date": resolved.get("canonical_source_date"),
                    "canonical_source_identity": resolved.get(
                        "canonical_source_identity"
                    ),
                    "source_digest": resolved.get("source_digest"),
                    "formula_version": resolved.get("formula_version"),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "calculated_at": str(source_row.get("created_at") or ""),
            "inputs_hash": str(
                resolved.get("canonical_source_version")
                or resolved.get("source_digest")
                or ""
            ),
            "daily_profit_coverage": daily_profit_coverage.get(int(nm_id)),
            "sales_without_cost_rub": (
                daily_profit_coverage.get(int(nm_id), {}).get(
                    "uncovered_sales_revenue_rub"
                )
            ),
        }
    return result
