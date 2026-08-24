"""Shared channel-aware resolver for realized sale COGS.

Finance and Partner Report use one contract.  FBS uses the exact physical
capital divided by exact physical quantity pooled across every active
facility's FBS balance for the SKU and business date.  When that primary source
does not exist, the only fallback is the same-SKU, same-day published WB+FF
inventory cost.  A missing source stays missing.

The physical lifecycle keeps its order/facility frozen WAC and Proxy 3/4 keeps
the published WB+FF informational cost.  Neither consumer imports this Finance
resolver, so pooled FBS Finance cost cannot change inventory, capital, ledger,
fulfilled events, or Proxy formulas.
"""

from __future__ import annotations

from bisect import bisect_right
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
CHANNEL_LOCATION_COST_FORMULA_VERSION = "canonical_our_cost_channel_location_v2"
FUNCTIONAL_CUTOVER_ID = "warehouse_functional_cutover_v1"
FUNCTIONAL_DAILY_TABLE = "sheet_vitrina_v1_warehouse_wb_daily_cost"
FBS_OBSERVATIONS_TABLE = "sheet_vitrina_v1_wb_supplies_fbs_order_observations"
FF_FACILITIES_TABLE = "sheet_vitrina_v1_ff_facilities"
FF_OPERATIONS_TABLE = "sheet_vitrina_v1_warehouse_business_operations"
FF_LINES_TABLE = "sheet_vitrina_v1_ff_pool_movement_lines"
READY_SNAPSHOTS_TABLE = "sheet_vitrina_v1_ready_snapshots"
COMMON_INVENTORY_COST_FORMULA_VERSION = "our_inventory_wac_wb_ff_v1"
COMMON_INVENTORY_COST_METRIC = "our_wb_unit_cost_rub"
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
    """Coherent WB, FBS channel identities and daily pooled cost sources."""

    wb: CanonicalWbCostSnapshot
    fbs_order_ids_by_identity_hash: Mapping[str, tuple[int, ...]]
    fbs_pooled_cost_by_date_nm: Mapping[tuple[str, str], Mapping[str, Any]]
    fbs_pooled_state_dates_by_nm: Mapping[str, tuple[str, ...]]
    fbs_pooled_states_by_nm: Mapping[str, tuple[Mapping[str, Any], ...]]
    common_inventory_cost_by_date_nm: Mapping[tuple[str, str], Mapping[str, Any]]

    @classmethod
    def from_connection(cls, conn: sqlite3.Connection) -> "CanonicalChannelCostSnapshot":
        wb = CanonicalWbCostSnapshot.from_connection(conn)
        identity_index: dict[str, set[int]] = {}
        pooled_costs: dict[tuple[str, str], Mapping[str, Any]] = {}
        common_inventory_costs: dict[tuple[str, str], Mapping[str, Any]] = {}
        if FBS_OBSERVATIONS_TABLE in wb.table_names:
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
        physical_required = {
            FF_FACILITIES_TABLE,
            FF_OPERATIONS_TABLE,
            FF_LINES_TABLE,
        }
        if physical_required <= set(wb.table_names):
            daily_deltas: dict[tuple[str, str], tuple[int, Decimal, list[str]]] = {}
            for row in conn.execute(
                f"""SELECT operation.business_date,line.nm_id,line.quantity_delta,
                            line.capital_delta_rub,line.operation_id,line.line_no
                       FROM {FF_LINES_TABLE} AS line
                       JOIN {FF_OPERATIONS_TABLE} AS operation
                         ON operation.operation_id=line.operation_id
                       JOIN {FF_FACILITIES_TABLE} AS facility
                         ON facility.facility_id=line.facility_id
                      WHERE line.pool='FBS' AND facility.active=1
                      ORDER BY operation.business_date,line.operation_id,line.line_no"""
            ):
                key = (str(row[0]), str(row[1]))
                quantity, capital, identities = daily_deltas.get(
                    key, (0, Decimal("0"), [])
                )
                capital_delta = _decimal_or_none(row[3])
                if capital_delta is None:
                    continue
                daily_deltas[key] = (
                    quantity + int(row[2]),
                    capital + capital_delta,
                    [*identities, f"{row[4]}:{row[5]}"],
                )
            running: dict[str, tuple[int, Decimal]] = {}
            for (business_date, nm_id), (quantity_delta, capital_delta, identities) in sorted(
                daily_deltas.items()
            ):
                previous_quantity, previous_capital = running.get(
                    nm_id, (0, Decimal("0"))
                )
                quantity = previous_quantity + quantity_delta
                capital = previous_capital + capital_delta
                running[nm_id] = (quantity, capital)
                source_payload = {
                    "business_date": business_date,
                    "nm_id": nm_id,
                    "quantity": quantity,
                    "capital_rub": format(capital, "f"),
                    "daily_line_count": len(identities),
                    "daily_lines_digest": _digest({"identities": identities}),
                }
                pooled_costs[(business_date, nm_id)] = {
                    **source_payload,
                    "status": (
                        "available" if quantity > 0 and capital > 0 else "absent"
                    ),
                    "unit_cost_rub": (
                        format(capital / Decimal(quantity), "f")
                        if quantity > 0 and capital > 0
                        else None
                    ),
                    "source_digest": _digest(source_payload),
                }
        pooled_state_dates: dict[str, tuple[str, ...]] = {}
        pooled_states: dict[str, tuple[Mapping[str, Any], ...]] = {}
        pooled_rows_by_nm: dict[str, list[Mapping[str, Any]]] = {}
        for (_, nm_id), item in sorted(pooled_costs.items()):
            pooled_rows_by_nm.setdefault(nm_id, []).append(item)
        for nm_id, items in pooled_rows_by_nm.items():
            pooled_states[nm_id] = tuple(items)
            pooled_state_dates[nm_id] = tuple(
                str(item["business_date"]) for item in items
            )
        if READY_SNAPSHOTS_TABLE in wb.table_names:
            common_inventory_costs = _load_common_inventory_costs(conn)
        return cls(
            wb=wb,
            fbs_order_ids_by_identity_hash={
                key: tuple(sorted(values)) for key, values in identity_index.items()
            },
            fbs_pooled_cost_by_date_nm=pooled_costs,
            fbs_pooled_state_dates_by_nm=pooled_state_dates,
            fbs_pooled_states_by_nm=pooled_states,
            common_inventory_cost_by_date_nm=common_inventory_costs,
        )


def pooled_fbs_state_as_of(
    snapshot: CanonicalChannelCostSnapshot,
    *,
    business_date: str,
    nm_id: str,
) -> Mapping[str, Any] | None:
    """Return the last physical pooled state at or before the business date.

    The explicit ``absent`` state is retained when quantity/capital becomes
    unavailable so an older positive balance can never leak past depletion.
    """

    dates = snapshot.fbs_pooled_state_dates_by_nm.get(str(nm_id), ())
    position = bisect_right(dates, str(business_date)) - 1
    if position < 0:
        return None
    return snapshot.fbs_pooled_states_by_nm[str(nm_id)][position]


def classify_finance_channel(
    snapshot: CanonicalChannelCostSnapshot,
    *,
    operation: Mapping[str, Any] | None = None,
    fbs_order_id: int | None = None,
) -> str:
    """Classify Finance channel without retaining per-operation identity state.

    Exact privacy-safe hashes remain in the immutable source/dependency
    evidence.  The returned bounded category is sufficient for cost routing
    and lets multi-million-row Finance runs reuse one nm/date/channel result
    instead of caching one result per order identity.
    """

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
            snapshot.fbs_order_ids_by_identity_hash.get(identity_hash, ())
        )
    if fbs_order_id is not None and int(fbs_order_id) > 0:
        matched_order_ids.add(int(fbs_order_id))
    if len(matched_order_ids) > 1:
        return "fbs_exact_identity_ambiguous"
    if matched_order_ids:
        return "fbs_exact_identity"
    channel_tokens = {
        str(raw.get(key) or "").strip().casefold()
        for key in ("deliveryType", "delivery_type", "orderType", "order_type")
        if str(raw.get(key) or "").strip()
    }
    if "fbs" in channel_tokens:
        return "fbs_explicit_channel"
    return "wb_non_fbs"


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


def _load_common_inventory_costs(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], Mapping[str, Any]]:
    """Read only exact-day published WB+FF cells with versioned evidence.

    Plans are consumed one at a time and reduced to the one metric needed by
    the fallback.  This avoids retaining ready-snapshot documents or unrelated
    metrics in the high-volume Finance projection.
    """

    result: dict[tuple[str, str], Mapping[str, Any]] = {}
    rows = conn.execute(
        f"""SELECT bundle_version,as_of_date,snapshot_id,refreshed_at,plan_json
              FROM {READY_SNAPSHOTS_TABLE}
             ORDER BY refreshed_at,bundle_version,as_of_date,snapshot_id"""
    )
    for snapshot in rows:
        try:
            plan = json.loads(str(snapshot["plan_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        dates = plan.get("date_columns")
        sheets = plan.get("sheets")
        metadata = plan.get("metadata")
        if not isinstance(dates, list) or not isinstance(sheets, list):
            continue
        normalized_dates = [str(item or "") for item in dates]
        eligible_dates: set[str] = set()
        if isinstance(metadata, Mapping):
            for marker_key in (
                "functional_economics_backfill",
                "functional_economics_targeted_replay",
            ):
                marker = metadata.get(marker_key)
                publication = (
                    marker.get("inventory_cost_publication")
                    if isinstance(marker, Mapping)
                    else None
                )
                if (
                    not isinstance(publication, Mapping)
                    or str(publication.get("formula_version") or "")
                    != COMMON_INVENTORY_COST_FORMULA_VERSION
                ):
                    continue
                evidence = publication.get("date_evidence")
                if isinstance(evidence, Mapping):
                    eligible_dates.update(
                        str(day)
                        for day, item in evidence.items()
                        if isinstance(item, Mapping)
                    )
        if not eligible_dates:
            continue
        data_sheet = next(
            (
                item
                for item in sheets
                if isinstance(item, Mapping)
                and str(item.get("name") or "") == "DATA_VITRINA"
            ),
            None,
        )
        if not isinstance(data_sheet, Mapping):
            continue
        for row in data_sheet.get("rows") or []:
            if not isinstance(row, list) or len(row) < 2:
                continue
            row_id = str(row[1] or "")
            scope, separator, metric = row_id.partition("|")
            if (
                not separator
                or not scope.startswith("SKU:")
                or metric != COMMON_INVENTORY_COST_METRIC
            ):
                continue
            nm_id = scope.removeprefix("SKU:").strip()
            if not nm_id.isdigit() or int(nm_id) <= 0:
                continue
            for index, business_date in enumerate(normalized_dates):
                if business_date not in eligible_dates or len(row) <= index + 2:
                    continue
                unit_cost = _decimal_or_none(row[index + 2])
                if unit_cost is None or unit_cost <= 0:
                    continue
                source_payload = {
                    "bundle_version": str(snapshot["bundle_version"]),
                    "snapshot_id": str(snapshot["snapshot_id"]),
                    "refreshed_at": str(snapshot["refreshed_at"]),
                    "business_date": business_date,
                    "nm_id": nm_id,
                    "metric": COMMON_INVENTORY_COST_METRIC,
                    "formula_version": COMMON_INVENTORY_COST_FORMULA_VERSION,
                    "unit_cost_rub": format(unit_cost, "f"),
                }
                result[(business_date, nm_id)] = {
                    **source_payload,
                    "source_digest": _digest(source_payload),
                }
    return result


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

    A privacy-safe hash match, explicit ``fbs_order_id`` or explicit FBS token
    selects the channel.  Order identity is classification evidence only: the
    Finance value never depends on an order or facility.  FBS first uses the
    pooled physical balance and only then the exact same-day common inventory
    cost.  Non-FBS rows retain the canonical WB/FBO daily resolver.
    """

    state = snapshot or CanonicalChannelCostSnapshot.from_connection(conn)
    classification = classify_finance_channel(
        state,
        operation=operation,
        fbs_order_id=fbs_order_id,
    )
    base = {
        "nm_id": str(nm_id or "").strip(),
        "operation_date": operation_date.isoformat(),
        "formula_version": CHANNEL_LOCATION_COST_FORMULA_VERSION,
        "channel_classification": classification,
    }
    if classification.startswith("fbs_"):
        key = (operation_date.isoformat(), str(nm_id or "").strip())
        pooled_state = pooled_fbs_state_as_of(
            state,
            business_date=key[0],
            nm_id=key[1],
        )
        source = (
            pooled_state
            if pooled_state is not None
            and str(pooled_state.get("status") or "") == "available"
            else None
        )
        fallback_used = source is None
        if source is None:
            source = state.common_inventory_cost_by_date_nm.get(key)
        if source is None:
            return {
                **base,
                "status": "missing",
                "reason": "fbs_pooled_and_common_inventory_cost_missing",
                "channel": "FBS",
                "pool": "FBS",
                "primary_reason": "fbs_pooled_physical_balance_absent",
                "fallback_reason": "same_day_common_inventory_cost_absent",
            }
        unit_cost = _decimal_or_none(source.get("unit_cost_rub"))
        if unit_cost is None or unit_cost <= 0:
            return {
                **base,
                "status": "missing",
                "reason": "fbs_cost_source_non_positive",
                "channel": "FBS",
                "pool": "FBS",
            }
        source_digest = str(source.get("source_digest") or _digest(dict(source)))
        source_identity = (
            f"{COMMON_INVENTORY_COST_FORMULA_VERSION}:{key[0]}:{key[1]}:"
            f"{source.get('snapshot_id')}"
            if fallback_used
            else (
                f"pooled-fbs:{source.get('business_date')}:{key[1]}:"
                f"{source_digest}"
            )
        )
        return {
            **base,
            "status": "resolved",
            "reason": "",
            "channel": "FBS",
            "pool": "FBS",
            "facility_id": "",
            "unit_cost_rub": format(unit_cost, "f"),
            "quality": (
                "same_day_common_inventory_fallback"
                if fallback_used
                else "pooled_fbs_physical_exact"
            ),
            "selection_method": (
                "same_nm_same_day_common_inventory_cost_fallback"
                if fallback_used
                else "sum_fbs_physical_capital_divided_by_quantity"
            ),
            "canonical_source_date": operation_date.isoformat(),
            "physical_balance_as_of_date": (
                "" if fallback_used else str(source.get("business_date") or "")
            ),
            "canonical_source_identity": source_identity,
            "canonical_source_version": source_digest,
            "source_table": (
                READY_SNAPSHOTS_TABLE if fallback_used else FF_LINES_TABLE
            ),
            "source_digest": source_digest,
            "source_row": dict(source),
            "projection_quality": (
                "exact_same_day_common_inventory"
                if fallback_used
                else "exact_same_day_pooled_fbs_wac"
            ),
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
        "channel_classification": classification,
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
