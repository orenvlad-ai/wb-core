"""Unified cost, physical-stage and invested-capital projections.

The tables owned by this module are derived audit/projection tables.  Physical
quantity is always read from the supplier registry, FF ledger, persisted WB
supply evidence and the official WB stock snapshot.  Legacy module-40/45 rows
are publication targets only; they are never inputs to the engine.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import sqlite3
from typing import Any, Callable, Iterable, Mapping

from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
    _connect,
    _ensure_schema,
)
from packages.application.our_wb_costs import _extract_snapshot_sku_metric


CUTOVER_DATE = "2026-07-01"
ONEC_FALLBACK_LAST_DATE = "2026-05-16"
PRIMARY_ACCEPTED_DATE_FROM = "2026-06-21"
PRIMARY_ACCEPTED_DATE_TO = "2026-06-24"
EXPECTED_PRIMARY_AVG_RUB = Decimal("111.181389")
PRIMARY_AVG_TOLERANCE_RUB = Decimal("0.01")
PRIMARY_MIN_QUANTITY = Decimal("100000")

ZERO = Decimal("0")
ONE = Decimal("1")

STAGE_PRODUCTION = "PRODUCTION"
STAGE_PRODUCTION_TO_FF = "PRODUCTION_TO_FF"
STAGE_FF = "FF"
STAGE_FF_TO_WB = "FF_TO_WB"
STAGE_WB = "WB"
STAGES = (
    STAGE_PRODUCTION,
    STAGE_PRODUCTION_TO_FF,
    STAGE_FF,
    STAGE_FF_TO_WB,
    STAGE_WB,
)

PROJECTION_RECOGNIZED = "recognized"
PROJECTION_PAID = "paid"

BASELINE_PRIMARY = "primary_supplier_shipment"
BASELINE_ONEC = "legacy_1c_fallback"

ONEC_FF_UNIT_COST_METRIC = "onec_FF_STOCK_unit_cost_rub"
OFFICIAL_WB_STOCK_METRIC = "stock_total"

CANONICAL_TABLE_PREFIX = "sheet_vitrina_v1_canonical_cost_"


class CanonicalCostBlocked(ValueError):
    """A fail-closed source, cost-coverage or reconciliation failure."""

    def __init__(self, code: str, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(f"{code}: {_json_dumps(self.details)}")


@dataclass(frozen=True)
class CanonicalRebuildResult:
    cutover_date: str
    date_from: str
    date_to: str
    baseline_fingerprint: str
    component_rows_changed: int
    movement_rows_changed: int
    outstanding_rows_changed: int
    daily_rows_changed: int
    invalidated_from: str | None
    fingerprint: str


class CanonicalCostEngine:
    """Build both paid-capital and recognized-cost views from one source graph."""

    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        timestamp_factory: Callable[[], str] | None = None,
    ) -> None:
        self.runtime = runtime
        self.timestamp_factory = timestamp_factory or _now
        self.runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.runtime.db_path) as conn:
            _ensure_schema(conn)
            ensure_canonical_cost_schema(conn)

    def discover_primary_baseline_shipment(self) -> dict[str, Any]:
        """Find the one persisted fully calculated large June FF receipt."""
        with _connect(self.runtime.db_path) as conn:
            _ensure_schema(conn)
            ensure_canonical_cost_schema(conn)
            candidates = conn.execute(
                """
                SELECT shipment.shipment_id, shipment.actual_ff_acceptance_date,
                       shipment.product_qty_total, shipment.match_status,
                       shipment.expenses_complete, layer.layer_id,
                       layer.status AS layer_status, layer.product_qty_total AS layer_qty,
                       layer.weighted_avg_ff_unit_cost_rub, layer.reconciliation_status,
                       layer.inputs_hash
                FROM sheet_vitrina_v1_supplier_shipments AS shipment
                JOIN sheet_vitrina_v1_supplier_ff_cost_layers AS layer
                  ON layer.supplier_shipment_id = shipment.shipment_id
                 AND layer.is_current = 1
                WHERE shipment.order_status = 'accepted_ff'
                  AND shipment.actual_ff_acceptance_date BETWEEN ? AND ?
                  AND COALESCE(shipment.product_qty_total, 0) >= ?
                ORDER BY shipment.product_qty_total DESC, shipment.shipment_id
                """,
                (
                    PRIMARY_ACCEPTED_DATE_FROM,
                    PRIMARY_ACCEPTED_DATE_TO,
                    float(PRIMARY_MIN_QUANTITY),
                ),
            ).fetchall()
            valid: list[dict[str, Any]] = []
            rejected: list[dict[str, Any]] = []
            for row in candidates:
                shipment_id = str(row["shipment_id"])
                line_counts = conn.execute(
                    """
                    SELECT COUNT(*) AS product_count,
                           SUM(CASE WHEN internal_nm_id IS NOT NULL
                                         AND match_status IN ('matched','matched_by_compatibility')
                                    THEN 1 ELSE 0 END) AS matched_count,
                           COUNT(DISTINCT internal_nm_id) AS sku_count
                    FROM sheet_vitrina_v1_supplier_shipment_lines
                    WHERE shipment_id = ? AND line_type = 'product'
                    """,
                    (shipment_id,),
                ).fetchone()
                ff_line_counts = conn.execute(
                    """
                    SELECT COUNT(*) line_count,
                           SUM(CASE WHEN nm_id IS NOT NULL AND sku_ff_unit_cost_rub>0
                                         AND source_status='confirmed'
                                    THEN 1 ELSE 0 END) confirmed_count
                    FROM sheet_vitrina_v1_supplier_ff_cost_layer_lines
                    WHERE layer_id=?
                    """,
                    (str(row["layer_id"]),),
                ).fetchone()
                avg = _decimal(row["weighted_avg_ff_unit_cost_rub"])
                reasons: list[str] = []
                if int(row["expenses_complete"] or 0) != 1:
                    reasons.append("expenses_not_certified")
                if str(row["layer_status"] or "") != "confirmed":
                    reasons.append("ff_layer_not_confirmed")
                if str(row["reconciliation_status"] or "") != "ok":
                    reasons.append("ff_layer_reconciliation_not_ok")
                if int(line_counts["product_count"] or 0) == 0:
                    reasons.append("no_product_lines")
                if int(line_counts["matched_count"] or 0) != int(line_counts["product_count"] or 0):
                    reasons.append("sku_matching_incomplete")
                if int(ff_line_counts["line_count"] or 0) != int(line_counts["product_count"] or 0):
                    reasons.append("ff_cost_line_count_mismatch")
                if int(ff_line_counts["confirmed_count"] or 0) != int(ff_line_counts["line_count"] or 0):
                    reasons.append("ff_cost_lines_not_fully_confirmed")
                if avg <= ZERO or abs(avg - EXPECTED_PRIMARY_AVG_RUB) > PRIMARY_AVG_TOLERANCE_RUB:
                    reasons.append("weighted_average_outside_expected_tolerance")
                item = {
                    "shipment_id": shipment_id,
                    "accepted_ff_date": str(row["actual_ff_acceptance_date"]),
                    "quantity": _text(_decimal(row["product_qty_total"])),
                    "sku_count": int(line_counts["sku_count"] or 0),
                    "product_line_count": int(line_counts["product_count"] or 0),
                    "ff_cost_layer_id": str(row["layer_id"]),
                    "weighted_ff_unit_cost_rub": _text(avg),
                    "ff_cost_inputs_hash": str(row["inputs_hash"]),
                }
                if reasons:
                    rejected.append({**item, "reasons": reasons})
                else:
                    valid.append(item)
        if len(valid) != 1:
            raise CanonicalCostBlocked(
                "primary_baseline_shipment_not_unique",
                {"valid": valid, "rejected": rejected},
            )
        return {**valid[0], "rejected_candidate_count": len(rejected)}

    def build_baseline_plan(self, *, cutover_date: str = CUTOVER_DATE) -> dict[str, Any]:
        if cutover_date != CUTOVER_DATE:
            raise CanonicalCostBlocked("unsupported_cutover_date", {"cutover_date": cutover_date})
        primary = self.discover_primary_baseline_shipment()
        physical = self.physical_quantities_as_of(cutover_date)
        owned_nm_ids = sorted(
            nm_id
            for nm_id, stages in physical.items()
            if sum((stages.get(stage, ZERO) for stage in STAGES), ZERO) > ZERO
        )
        with _connect(self.runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            primary_rows = conn.execute(
                """
                SELECT line.nm_id, line.sku, line.display_name, line.sku_ff_unit_cost_rub,
                       line.layer_line_id, line.source_status
                FROM sheet_vitrina_v1_supplier_ff_cost_layer_lines AS line
                WHERE line.layer_id = ? AND line.nm_id IS NOT NULL
                ORDER BY line.nm_id
                """,
                (primary["ff_cost_layer_id"],),
            ).fetchall()
            primary_by_nm = {int(row["nm_id"]): dict(row) for row in primary_rows}
        fallbacks = self._nearest_onec_ff_fallbacks(
            nm_ids=[nm for nm in owned_nm_ids if nm not in primary_by_nm]
        )
        supplier_paid = self._supplier_payment_projection_as_of(cutover_date)
        missing = [nm for nm in owned_nm_ids if nm not in primary_by_nm and nm not in fallbacks]
        conflicting = [
            nm for nm, row in primary_by_nm.items()
            if nm in owned_nm_ids and _decimal(row.get("sku_ff_unit_cost_rub")) <= ZERO
        ]
        if missing or conflicting:
            covered_nm_ids = set(primary_by_nm) | set(fallbacks)
            total_quantity = sum(
                (
                    sum((stages.get(stage, ZERO) for stage in STAGES), ZERO)
                    for stages in physical.values()
                ),
                ZERO,
            )
            covered_quantity = sum(
                (
                    sum((physical[nm_id].get(stage, ZERO) for stage in STAGES), ZERO)
                    for nm_id in owned_nm_ids if nm_id in covered_nm_ids
                ),
                ZERO,
            )
            raise CanonicalCostBlocked(
                "baseline_cost_coverage_incomplete",
                {
                    "cutover_date": cutover_date,
                    "primary_shipment": primary,
                    "primary_sku_count": len(set(primary_by_nm) & set(owned_nm_ids)),
                    "primary_shipment_sku_count": len(primary_by_nm),
                    "fallbacks": [fallbacks[nm] for nm in sorted(fallbacks)],
                    "fallback_sku_count": len(fallbacks),
                    "missing_nm_ids": missing,
                    "missing_sku_count": len(missing),
                    "conflicting_nm_ids": conflicting,
                    "physical": _json_safe_physical(physical),
                    "stage_physical_quantities": {
                        stage: _text(sum(
                            (stages.get(stage, ZERO) for stages in physical.values()), ZERO
                        ))
                        for stage in STAGES
                    },
                    "physical_quantity": _text(total_quantity),
                    "cost_covered_quantity": _text(covered_quantity),
                    "cost_coverage": _text(_safe_ratio(covered_quantity, total_quantity)),
                },
            )
        lines: list[dict[str, Any]] = []
        for nm_id in owned_nm_ids:
            stages = physical[nm_id]
            if nm_id in primary_by_nm:
                source = primary_by_nm[nm_id]
                unit_cost = _decimal(source["sku_ff_unit_cost_rub"])
                source_type = BASELINE_PRIMARY
                source_identity = primary["shipment_id"]
                source_date = primary["accepted_ff_date"]
                provenance = {
                    "shipment_id": primary["shipment_id"],
                    "ff_cost_layer_id": primary["ff_cost_layer_id"],
                    "ff_cost_layer_line_id": str(source["layer_line_id"]),
                }
                confirmation = ONE
            else:
                source = fallbacks[nm_id]
                unit_cost = _decimal(source["unit_cost_rub"])
                source_type = BASELINE_ONEC
                source_identity = str(source["bundle_version"])
                source_date = str(source["as_of_date"])
                provenance = dict(source)
                confirmation = ZERO
            if source_date > ONEC_FALLBACK_LAST_DATE and source_type == BASELINE_ONEC:
                raise CanonicalCostBlocked(
                    "onec_fallback_after_cutoff",
                    {"nm_id": nm_id, "source_date": source_date},
                )
            if unit_cost <= ZERO:
                raise CanonicalCostBlocked("baseline_zero_cost_forbidden", {"nm_id": nm_id})
            for stage in STAGES:
                qty = stages.get(stage, ZERO)
                if qty <= ZERO:
                    continue
                paid_equivalent = qty
                paid_capital = qty * unit_cost
                paid_unit = unit_cost
                if stage in {STAGE_PRODUCTION, STAGE_PRODUCTION_TO_FF}:
                    payment = supplier_paid.get((nm_id, stage), {})
                    paid_equivalent = min(
                        _decimal(payment.get("paid_equivalent_quantity")), qty
                    )
                    paid_capital = _decimal(payment.get("paid_capital_rub"))
                    paid_unit = (
                        _safe_ratio(paid_capital, paid_equivalent)
                        if paid_equivalent > ZERO else ZERO
                    )
                lines.append(
                    {
                        "nm_id": nm_id,
                        "stage": stage,
                        "physical_quantity": _text(qty),
                        "paid_equivalent_quantity": _text(paid_equivalent),
                        "recognized_unit_cost_rub": _text(unit_cost),
                        "paid_unit_cost_rub": _text(paid_unit),
                        "recognized_capital_rub": _text(qty * unit_cost),
                        "paid_capital_rub": _text(paid_capital),
                        "cost_covered_quantity": _text(qty),
                        "confirmed_quantity": _text(qty * confirmation),
                        "source_type": source_type,
                        "source_identity": source_identity,
                        "source_date": source_date,
                        "provenance": provenance,
                    }
                )
        quantity = sum((_decimal(item["physical_quantity"]) for item in lines), ZERO)
        covered = sum((_decimal(item["cost_covered_quantity"]) for item in lines), ZERO)
        if quantity > ZERO and covered != quantity:
            raise CanonicalCostBlocked(
                "baseline_cost_coverage_not_100_pct",
                {"quantity": _text(quantity), "covered": _text(covered)},
            )
        stage_summary: dict[str, dict[str, str | None]] = {}
        for stage in STAGES:
            stage_lines = [item for item in lines if item["stage"] == stage]
            stage_qty = sum((_decimal(item["physical_quantity"]) for item in stage_lines), ZERO)
            stage_paid_equivalent = sum(
                (_decimal(item["paid_equivalent_quantity"]) for item in stage_lines), ZERO
            )
            stage_recognized = sum((_decimal(item["recognized_capital_rub"]) for item in stage_lines), ZERO)
            stage_paid = sum((_decimal(item["paid_capital_rub"]) for item in stage_lines), ZERO)
            stage_covered = sum((_decimal(item["cost_covered_quantity"]) for item in stage_lines), ZERO)
            stage_confirmed = sum((_decimal(item["confirmed_quantity"]) for item in stage_lines), ZERO)
            stage_summary[stage] = {
                "physical_quantity": _text(stage_qty),
                "paid_equivalent_quantity": _text(stage_paid_equivalent),
                "recognized_capital_rub": _text(stage_recognized),
                "paid_capital_rub": _text(stage_paid),
                "recognized_unit_cost_rub": _text(_safe_ratio(stage_recognized, stage_qty)) if stage_qty > ZERO else None,
                "paid_unit_cost_rub": (
                    _text(_safe_ratio(stage_paid, stage_paid_equivalent))
                    if stage_paid_equivalent > ZERO else None
                ),
                "cost_coverage": _text(_safe_ratio(stage_covered, stage_qty)) if stage_qty > ZERO else None,
                "confirmation_share": _text(_safe_ratio(stage_confirmed, stage_qty)) if stage_qty > ZERO else None,
            }
        payload = {
            "contract": "canonical_cost_baseline_v1",
            "cutover_date": cutover_date,
            "primary_shipment": primary,
            "primary_sku_ids": sorted(primary_by_nm),
            "primary_used_sku_ids": sorted(set(primary_by_nm) & set(owned_nm_ids)),
            "fallbacks": [fallbacks[nm] for nm in sorted(fallbacks)],
            "missing_nm_ids": missing,
            "conflicting_nm_ids": conflicting,
            "physical": _json_safe_physical(physical),
            "stage_summary": stage_summary,
            "lines": lines,
        }
        fingerprint = _stable_hash(payload)
        return {
            **payload,
            "primary_sku_count": len(set(primary_by_nm) & set(owned_nm_ids)),
            "primary_shipment_sku_count": len(primary_by_nm),
            "fallback_sku_count": len(fallbacks),
            "missing_sku_count": len(missing),
            "physical_quantity": _text(quantity),
            "recognized_capital_rub": _text(
                sum((_decimal(item["recognized_capital_rub"]) for item in lines), ZERO)
            ),
            "paid_capital_rub": _text(
                sum((_decimal(item["paid_capital_rub"]) for item in lines), ZERO)
            ),
            "cost_coverage": "1",
            "fingerprint": fingerprint,
        }

    def materialize_baseline_plan(self, plan: Mapping[str, Any]) -> str:
        fingerprint = str(plan.get("fingerprint") or "")
        if not fingerprint or fingerprint != _stable_hash(
            {key: value for key, value in plan.items() if key not in {
                "fingerprint", "primary_sku_count", "primary_shipment_sku_count", "fallback_sku_count",
                "missing_sku_count", "physical_quantity", "recognized_capital_rub",
                "paid_capital_rub", "cost_coverage",
            }}
        ):
            raise CanonicalCostBlocked("baseline_fingerprint_invalid")
        now = self.timestamp_factory()
        with _connect(self.runtime.db_path) as conn:
            _ensure_schema(conn)
            ensure_canonical_cost_schema(conn)
            existing = conn.execute(
                "SELECT fingerprint FROM sheet_vitrina_v1_canonical_cost_baseline_versions WHERE is_current=1"
            ).fetchone()
            if existing is not None and str(existing["fingerprint"]) == fingerprint:
                return fingerprint
            conn.execute(
                "UPDATE sheet_vitrina_v1_canonical_cost_baseline_versions SET is_current=0, superseded_at=? WHERE is_current=1",
                (now,),
            )
            version = int(conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM sheet_vitrina_v1_canonical_cost_baseline_versions"
            ).fetchone()[0])
            baseline_id = f"canonical_baseline_{version}_{fingerprint[:12]}"
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_canonical_cost_baseline_versions(
                    baseline_id, version, cutover_date, primary_shipment_id,
                    primary_accepted_ff_date, primary_quantity, primary_sku_count,
                    weighted_ff_unit_cost_rub, fallback_sku_count, fingerprint,
                    report_json, is_current, created_at, superseded_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,NULL)
                """,
                (
                    baseline_id, version, plan["cutover_date"],
                    plan["primary_shipment"]["shipment_id"],
                    plan["primary_shipment"]["accepted_ff_date"],
                    plan["primary_shipment"]["quantity"], plan["primary_sku_count"],
                    plan["primary_shipment"]["weighted_ff_unit_cost_rub"],
                    plan["fallback_sku_count"], fingerprint, _json_dumps(plan), now,
                ),
            )
            for item in plan["lines"]:
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_canonical_cost_baseline_lines(
                        baseline_id,nm_id,stage,physical_quantity,paid_equivalent_quantity,
                        recognized_unit_cost_rub,paid_unit_cost_rub,
                        recognized_capital_rub,paid_capital_rub,cost_covered_quantity,
                        confirmed_quantity,source_type,source_identity,source_date,
                        provenance_json,line_fingerprint
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        baseline_id, item["nm_id"], item["stage"],
                        item["physical_quantity"], item["paid_equivalent_quantity"],
                        item["recognized_unit_cost_rub"], item["paid_unit_cost_rub"],
                        item["recognized_capital_rub"], item["paid_capital_rub"],
                        item["cost_covered_quantity"], item["confirmed_quantity"],
                        item["source_type"], item["source_identity"], item["source_date"],
                        _json_dumps(item["provenance"]), _stable_hash(item),
                    ),
                )
            conn.commit()
        return fingerprint

    def current_baseline_report(self) -> dict[str, Any] | None:
        with _connect(self.runtime.db_path) as conn:
            ensure_canonical_cost_schema(conn)
            row = conn.execute(
                "SELECT report_json FROM sheet_vitrina_v1_canonical_cost_baseline_versions WHERE is_current=1"
            ).fetchone()
        return _json_loads(row["report_json"]) if row is not None else None

    def physical_quantities_as_of(self, as_of_date: str) -> dict[int, dict[str, Decimal]]:
        as_of_date = _iso_date(as_of_date)
        result: dict[int, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        with _connect(self.runtime.db_path) as conn:
            _ensure_schema(conn)
            supplier_rows = conn.execute(
                """
                SELECT shipment.shipment_id, shipment.created_at, shipment.shipment_date,
                       shipment.actual_shipment_date, shipment.actual_ff_acceptance_date,
                       line.internal_nm_id, line.qty
                FROM sheet_vitrina_v1_supplier_shipments AS shipment
                JOIN sheet_vitrina_v1_supplier_shipment_lines AS line
                  ON line.shipment_id=shipment.shipment_id AND line.line_type='product'
                WHERE line.internal_nm_id IS NOT NULL AND COALESCE(line.qty,0)>0
                ORDER BY shipment.shipment_id,line.sort_order
                """
            ).fetchall()
            for row in supplier_rows:
                registered = min(
                    value for value in (
                        str(row["shipment_date"] or "")[:10],
                        str(row["created_at"] or "")[:10],
                    ) if value
                )
                if registered > as_of_date:
                    continue
                shipped = str(row["actual_shipment_date"] or "")[:10]
                accepted = str(row["actual_ff_acceptance_date"] or "")[:10]
                if accepted and accepted <= as_of_date:
                    continue
                stage = (
                    STAGE_PRODUCTION_TO_FF
                    if shipped and shipped <= as_of_date
                    else STAGE_PRODUCTION
                )
                result[int(row["internal_nm_id"])][stage] += _decimal(row["qty"])

            operations = _ff_operation_rows(conn)
            for operation in operations:
                effective = _ff_operation_effective_date(conn, operation)
                if not effective or effective > as_of_date:
                    continue
                lines = conn.execute(
                    "SELECT nm_id,quantity_delta FROM sheet_vitrina_v1_ff_stock_operation_lines WHERE operation_id=?",
                    (operation["operation_id"],),
                ).fetchall()
                for line in lines:
                    result[int(line["nm_id"])][STAGE_FF] += _decimal(line["quantity_delta"])

            for movement in _wb_movement_evidence(conn, as_of_date=as_of_date):
                result[movement["nm_id"]][STAGE_FF_TO_WB] += movement["open_quantity"]

        wb_stock = self._snapshot_metric(as_of_date, OFFICIAL_WB_STOCK_METRIC)
        for nm_id, qty in wb_stock.items():
            result[nm_id][STAGE_WB] = max(_decimal(qty), ZERO)
        for nm_id in list(result):
            for stage in STAGES:
                value = result[nm_id].get(stage, ZERO)
                if value < ZERO:
                    raise CanonicalCostBlocked(
                        "negative_physical_quantity", {"nm_id": nm_id, "stage": stage, "quantity": _text(value)}
                    )
                result[nm_id][stage] = value
        return {nm: dict(stages) for nm, stages in result.items()}

    def rebuild(
        self,
        *,
        date_from: str = CUTOVER_DATE,
        date_to: str | None = None,
    ) -> CanonicalRebuildResult:
        start = _iso_date(date_from)
        end = _iso_date(date_to or date.today().isoformat())
        if start < CUTOVER_DATE:
            raise CanonicalCostBlocked("legacy_history_is_immutable", {"date_from": start})
        if end < start:
            raise ValueError("date_to must be on or after date_from")
        baseline = self.current_baseline_report()
        if baseline is None:
            raise CanonicalCostBlocked("canonical_baseline_not_materialized")
        baseline_fingerprint = str(baseline["fingerprint"])
        component_changed, invalidated = self._materialize_components(end)
        movement_changed = self._materialize_movement_cost_layers(end)
        outstanding_changed = self._materialize_outstanding_layers(end)
        daily_changed = self._materialize_daily_state(start, end)
        fingerprint = self._projection_fingerprint(start, end)
        return CanonicalRebuildResult(
            cutover_date=CUTOVER_DATE,
            date_from=start,
            date_to=end,
            baseline_fingerprint=baseline_fingerprint,
            component_rows_changed=component_changed,
            movement_rows_changed=movement_changed,
            outstanding_rows_changed=outstanding_changed,
            daily_rows_changed=daily_changed,
            invalidated_from=invalidated,
            fingerprint=fingerprint,
        )

    def load_daily_metric_lookup(self, as_of_date: str) -> dict[int, dict[str, Any]]:
        as_of_date = _iso_date(as_of_date)
        with _connect(self.runtime.db_path) as conn:
            ensure_canonical_cost_schema(conn)
            rows = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_canonical_cost_daily_state WHERE as_of_date=? ORDER BY nm_id,stage",
                (as_of_date,),
            ).fetchall()
        result: dict[int, dict[str, Any]] = {}
        for row in rows:
            target = result.setdefault(int(row["nm_id"]), {"stages": {}})
            target["stages"][str(row["stage"])] = dict(row)
        return result

    def status(self) -> dict[str, Any]:
        baseline = self.current_baseline_report()
        with _connect(self.runtime.db_path) as conn:
            ensure_canonical_cost_schema(conn)
            latest = conn.execute(
                """
                SELECT as_of_date,SUM(physical_quantity+0) physical_qty,
                       SUM(paid_capital_rub+0) paid_capital,
                       SUM(recognized_capital_rub+0) recognized_capital,
                       SUM(cost_covered_quantity+0) covered_qty,
                       SUM(confirmed_quantity+0) confirmed_qty
                FROM sheet_vitrina_v1_canonical_cost_daily_state
                GROUP BY as_of_date ORDER BY as_of_date DESC LIMIT 1
                """
            ).fetchone()
            outstanding = conn.execute(
                """
                SELECT SUM(open_quantity+0) qty,
                       SUM((open_quantity+0)*(cost_coverage_share+0)*(recognized_unit_cost_rub+0)) recognized,
                       SUM((paid_equivalent_quantity+0)*(paid_unit_cost_rub+0)) paid
                FROM sheet_vitrina_v1_canonical_cost_wb_outstanding_layers
                WHERE is_current=1
                """
            ).fetchone()
        out_qty = _decimal(outstanding["qty"]) if outstanding else ZERO
        return {
            "contract_name": "canonical_cost_engine_v1",
            "cutover_date": CUTOVER_DATE,
            "baseline": baseline,
            "latest": dict(latest) if latest else None,
            "underaccepted_wb": {
                "quantity": float(out_qty),
                "recognized_weighted_unit_cost_rub": (
                    float(_decimal(outstanding["recognized"]) / out_qty) if out_qty > ZERO else None
                ),
                "paid_weighted_unit_cost_rub": (
                    float(_decimal(outstanding["paid"]) / out_qty) if out_qty > ZERO else None
                ),
            },
        }

    def _nearest_onec_ff_fallbacks(self, *, nm_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
        missing = set(int(item) for item in nm_ids)
        result: dict[int, dict[str, Any]] = {}
        if not missing:
            return result
        with _connect(self.runtime.db_path) as conn:
            rows = conn.execute(
                """
                SELECT bundle_version,as_of_date,plan_json
                FROM sheet_vitrina_v1_ready_snapshots
                WHERE as_of_date <= ?
                ORDER BY as_of_date DESC,activated_at DESC,refreshed_at DESC,bundle_version DESC
                """,
                (ONEC_FALLBACK_LAST_DATE,),
            ).fetchall()
        seen_dates: set[str] = set()
        for row in rows:
            day = str(row["as_of_date"])
            # Only the newest persisted bundle for a given date participates.
            if day in seen_dates:
                continue
            seen_dates.add(day)
            try:
                snapshot = self.runtime.load_sheet_vitrina_ready_snapshot_any_bundle(as_of_date=day)
            except Exception:
                continue
            values = _extract_snapshot_sku_metric(
                snapshot, column_date=day, metric_key=ONEC_FF_UNIT_COST_METRIC
            )
            for nm_id in sorted(missing):
                value = _decimal(values.get(nm_id))
                if value <= ZERO:
                    continue
                result[nm_id] = {
                    "nm_id": nm_id,
                    "unit_cost_rub": _text(value),
                    "as_of_date": day,
                    "bundle_version": str(row["bundle_version"]),
                    "metric_key": ONEC_FF_UNIT_COST_METRIC,
                    "source_type": BASELINE_ONEC,
                }
            missing -= set(result)
            if not missing:
                break
        return result

    def _snapshot_metric(self, as_of_date: str, metric_key: str) -> dict[int, float]:
        snapshot_date = as_of_date
        try:
            snapshot = self.runtime.load_sheet_vitrina_ready_snapshot_any_bundle(as_of_date=snapshot_date)
        except Exception:
            candidates = self.runtime.list_sheet_vitrina_ready_snapshot_dates_any_bundle(
                date_to=as_of_date, descending=True
            )
            if not candidates:
                return {}
            snapshot_date = candidates[0]
            try:
                snapshot = self.runtime.load_sheet_vitrina_ready_snapshot_any_bundle(
                    as_of_date=snapshot_date
                )
            except Exception:
                return {}
        return _extract_snapshot_sku_metric(
            snapshot, column_date=snapshot_date, metric_key=metric_key
        )

    def _supplier_payment_projection_as_of(
        self, as_of_date: str
    ) -> dict[tuple[int, str], dict[str, Decimal]]:
        """Allocate factual CNY payments over every matched line, never selected SKUs."""
        result: dict[tuple[int, str], dict[str, Decimal]] = {}
        with _connect(self.runtime.db_path) as conn:
            shipments = conn.execute(
                """
                SELECT shipment_id,created_at,shipment_date,actual_shipment_date,
                       actual_ff_acceptance_date,invoice_amount_total,product_amount_total
                FROM sheet_vitrina_v1_supplier_shipments
                ORDER BY shipment_id
                """
            ).fetchall()
            for shipment in shipments:
                registered = min(
                    value for value in (
                        str(shipment["shipment_date"] or "")[:10],
                        str(shipment["created_at"] or "")[:10],
                    ) if value
                )
                if registered > as_of_date:
                    continue
                accepted = str(shipment["actual_ff_acceptance_date"] or "")[:10]
                if accepted and accepted <= as_of_date:
                    continue
                shipped = str(shipment["actual_shipment_date"] or "")[:10]
                stage = (
                    STAGE_PRODUCTION_TO_FF
                    if shipped and shipped <= as_of_date else STAGE_PRODUCTION
                )
                payments = conn.execute(
                    """
                    SELECT cny_delta,rub_value_delta
                    FROM sheet_vitrina_v1_cny_ledger_operations
                    WHERE source_order_id=? AND operation_type='supplier_payment_out'
                      AND status='posted' AND operation_date<=?
                    """,
                    (shipment["shipment_id"], as_of_date),
                ).fetchall()
                paid_cny = sum(
                    (abs(_decimal(item["cny_delta"])) for item in payments), ZERO
                )
                paid_rub = sum(
                    (abs(_decimal(item["rub_value_delta"])) for item in payments), ZERO
                )
                invoice_total = _decimal(shipment["invoice_amount_total"])
                product_total = _decimal(shipment["product_amount_total"])
                paid_share = min(_safe_ratio(paid_cny, invoice_total), ONE)
                for line in conn.execute(
                    """
                    SELECT internal_nm_id,qty,amount
                    FROM sheet_vitrina_v1_supplier_shipment_lines
                    WHERE shipment_id=? AND line_type='product'
                      AND internal_nm_id IS NOT NULL AND COALESCE(qty,0)>0
                    ORDER BY sort_order
                    """,
                    (shipment["shipment_id"],),
                ).fetchall():
                    key = (int(line["internal_nm_id"]), stage)
                    bucket = result.setdefault(
                        key,
                        {"paid_equivalent_quantity": ZERO, "paid_capital_rub": ZERO},
                    )
                    bucket["paid_equivalent_quantity"] += (
                        _decimal(line["qty"]) * paid_share
                    )
                    bucket["paid_capital_rub"] += paid_rub * _safe_ratio(
                        _decimal(line["amount"]), product_total
                    )
        return result

    def _baseline_costs(self) -> dict[int, dict[str, Decimal]]:
        with _connect(self.runtime.db_path) as conn:
            rows = conn.execute(
                """
                SELECT line.nm_id,line.recognized_unit_cost_rub,line.paid_unit_cost_rub,
                       line.confirmed_quantity,line.physical_quantity
                FROM sheet_vitrina_v1_canonical_cost_baseline_lines line
                JOIN sheet_vitrina_v1_canonical_cost_baseline_versions version
                  ON version.baseline_id=line.baseline_id AND version.is_current=1
                ORDER BY line.nm_id,line.stage
                """
            ).fetchall()
        result: dict[int, dict[str, Decimal]] = {}
        for row in rows:
            nm_id = int(row["nm_id"])
            result.setdefault(
                nm_id,
                {
                    "recognized": _decimal(row["recognized_unit_cost_rub"]),
                    "paid": _decimal(row["paid_unit_cost_rub"]),
                    "confirmation": ZERO,
                },
            )
            result[nm_id]["confirmation"] = max(
                result[nm_id]["confirmation"],
                _safe_ratio(_decimal(row["confirmed_quantity"]), _decimal(row["physical_quantity"])),
            )
        return result

    def _materialize_components(self, date_to: str) -> tuple[int, str | None]:
        """Version per-SKU recognized/paid components with factual effective dates."""
        plans: list[dict[str, Any]] = []
        baseline = self._baseline_costs()
        with _connect(self.runtime.db_path) as conn:
            shipments = conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_supplier_shipments
                WHERE shipment_date <= ?
                  AND (
                    shipment_date > ?
                    OR actual_ff_acceptance_date IS NULL
                    OR actual_ff_acceptance_date > ?
                  )
                ORDER BY shipment_id
                """,
                (date_to, CUTOVER_DATE, CUTOVER_DATE),
            ).fetchall()
            for shipment in shipments:
                shipment_id = str(shipment["shipment_id"])
                opening_carry = str(shipment["shipment_date"] or "")[:10] <= CUTOVER_DATE
                lines = conn.execute(
                    """
                    SELECT * FROM sheet_vitrina_v1_supplier_shipment_lines
                    WHERE shipment_id=? AND line_type='product' ORDER BY sort_order
                    """,
                    (shipment_id,),
                ).fetchall()
                if not lines:
                    continue
                invoice_total = _decimal(shipment["invoice_amount_total"])
                accepted_date = str(shipment["actual_ff_acceptance_date"] or "")[:10]
                ff_costs = {
                    int(row["nm_id"]): dict(row)
                    for row in conn.execute(
                        """
                        SELECT line.* FROM sheet_vitrina_v1_supplier_ff_cost_layer_lines line
                        JOIN sheet_vitrina_v1_supplier_ff_cost_layers layer ON layer.layer_id=line.layer_id
                        WHERE layer.supplier_shipment_id=? AND layer.is_current=1 AND line.nm_id IS NOT NULL
                        """,
                        (shipment_id,),
                    ).fetchall()
                }
                payments = conn.execute(
                    """
                    SELECT operation_id,operation_date,cny_delta,rub_value_delta,source_document_id
                    FROM sheet_vitrina_v1_cny_ledger_operations
                    WHERE source_order_id=? AND operation_type='supplier_payment_out' AND status='posted'
                    ORDER BY sequence_key,operation_id
                    """,
                    (shipment_id,),
                ).fetchall()
                payment_rub = sum((abs(_decimal(row["rub_value_delta"])) for row in payments), ZERO)
                payment_cny = sum((abs(_decimal(row["cny_delta"])) for row in payments), ZERO)
                product_value_total = sum((_decimal(line["amount"]) for line in lines), ZERO)
                product_qty_total = sum((_decimal(line["qty"]) for line in lines), ZERO)
                expenses = conn.execute(
                    """
                    SELECT expense.line_id,expense.financial_document_id,expense.category,
                           expense.amount_rub,document.document_type,document.document_date,
                           document.parse_status,document.file_sha256
                    FROM sheet_vitrina_v1_supplier_financial_expense_lines expense
                    JOIN sheet_vitrina_v1_supplier_financial_documents document
                      ON document.document_id=expense.financial_document_id
                    WHERE expense.supplier_order_id=? AND COALESCE(expense.amount_rub,0)>0
                      AND document.parse_status='confirmed'
                    ORDER BY document.document_date,document.document_id,expense.sort_order
                    """,
                    (shipment_id,),
                ).fetchall()
                for line in lines:
                    nm_id = int(line["internal_nm_id"] or 0)
                    qty = _decimal(line["qty"])
                    ff = ff_costs.get(nm_id)
                    if nm_id <= 0 or qty <= ZERO:
                        continue
                    recognized_unit = _decimal((ff or {}).get("sku_ff_unit_cost_rub"))
                    baseline_cost = baseline.get(nm_id)
                    if opening_carry and baseline_cost is not None:
                        recognized_unit = _decimal(baseline_cost["recognized"])
                    elif recognized_unit <= ZERO:
                        recognized_unit = _decimal(line["unit_price"]) * _safe_ratio(
                            payment_rub, payment_cny
                        )
                    recognized_total = recognized_unit * qty
                    expense_allocations: list[tuple[sqlite3.Row, Decimal]] = [
                        (expense, _decimal(expense["amount_rub"]) * _safe_ratio(qty, product_qty_total))
                        for expense in expenses
                        if not opening_carry
                        or str(expense["document_date"] or "")[:10] > CUTOVER_DATE
                    ]
                    recognized_expenses = sum((amount for _, amount in expense_allocations), ZERO)
                    invoice_recognized = (
                        recognized_total
                        if opening_carry
                        else max(recognized_total - recognized_expenses, ZERO)
                    )
                    plans.append(
                        {
                            "component_type": "supplier_invoice_and_cny_payment",
                            "shipment_id": shipment_id,
                            "supply_id": "",
                            "nm_id": nm_id,
                            "quantity": _text(qty),
                            "recognized_amount_rub": _text(invoice_recognized),
                            "recognized_date": (
                                CUTOVER_DATE if opening_carry
                                else str(shipment["invoice_date"] or accepted_date)[:10]
                            ),
                            "paid_amount_rub": "0",
                            "paid_equivalent_quantity": "0",
                            "paid_date": None,
                            "allocation_method": "supplier_line_invoice_value_plus_invoice_common_pool",
                            "source_document_id": str(shipment["invoice_document_id"] or ""),
                            "source_line_id": str(line["line_id"]),
                            "evidence": {
                                "ff_cost_layer_line_id": str((ff or {}).get("layer_line_id") or ""),
                                "payment_operation_ids": [str(row["operation_id"]) for row in payments],
                            },
                            "confirmation_status": (
                                "confirmed"
                                if opening_carry and baseline_cost is not None
                                and _decimal(baseline_cost["confirmation"]) == ONE
                                else str((ff or {}).get("source_status") or "needs_review")
                            ),
                        }
                    )
                    for payment in payments:
                        operation_cny = abs(_decimal(payment["cny_delta"]))
                        operation_rub = abs(_decimal(payment["rub_value_delta"]))
                        operation_date = str(payment["operation_date"] or "")[:10]
                        if operation_cny <= ZERO or operation_rub <= ZERO or not operation_date:
                            continue
                        plans.append(
                            {
                                "component_type": "supplier_invoice_payment",
                                "shipment_id": shipment_id,
                                "supply_id": "",
                                "nm_id": nm_id,
                                "quantity": _text(qty),
                                "recognized_amount_rub": "0",
                                "recognized_date": operation_date,
                                "paid_amount_rub": _text(
                                    operation_rub
                                    * _safe_ratio(_decimal(line["amount"]), product_value_total)
                                ),
                                "paid_equivalent_quantity": _text(
                                    qty * min(_safe_ratio(operation_cny, invoice_total), ONE)
                                ),
                                "paid_date": operation_date,
                                "allocation_method": "supplier_line_invoice_value_proportional",
                                "source_document_id": str(payment["source_document_id"] or payment["operation_id"]),
                                "source_line_id": f"{line['line_id']}:{payment['operation_id']}",
                                "evidence": {"cny_ledger_operation_id": str(payment["operation_id"])},
                                "confirmation_status": "confirmed",
                            }
                        )
                    for expense, allocated in expense_allocations:
                        plans.append(
                            {
                                "component_type": str(expense["document_type"] or expense["category"] or "factual_expense"),
                                "shipment_id": shipment_id,
                                "supply_id": "",
                                "nm_id": nm_id,
                                "quantity": _text(qty),
                                "recognized_amount_rub": _text(allocated),
                                "recognized_date": str(expense["document_date"] or accepted_date)[:10],
                                "paid_amount_rub": "0",
                                "paid_equivalent_quantity": "0",
                                "paid_date": None,
                                "allocation_method": "shipment_product_quantity_proportional",
                                "source_document_id": str(expense["financial_document_id"]),
                                "source_line_id": str(expense["line_id"]),
                                "evidence": {
                                    "category": str(expense["category"]),
                                    "file_sha256": str(expense["file_sha256"] or ""),
                                    "ff_cost_layer_line_id": str((ff or {}).get("layer_line_id") or ""),
                                },
                                "confirmation_status": "confirmed",
                            }
                        )
            wb_components = conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_wb_supply_cost_layers
                WHERE is_current=1 AND COALESCE(accepted_date,supply_date,'')>?
                  AND COALESCE(accepted_date,supply_date,'')<=?
                ORDER BY wb_supply_id,nm_id
                """,
                (CUTOVER_DATE, date_to),
            ).fetchall()
            for row in wb_components:
                qty = _decimal(row["accepted_qty"])
                if qty <= ZERO:
                    continue
                for component_type, per_unit_field, source_document in (
                    ("wb_transit", "transit_per_unit_rub", ""),
                    ("ff_services", "ff_services_per_unit_rub", str(row["ff_upload_id"] or "")),
                    ("ff_storage", "ff_storage_per_unit_rub", str(row["ff_upload_id"] or "")),
                ):
                    per_unit = _decimal(row[per_unit_field])
                    status = (
                        str(row["transit_cost_status"])
                        if component_type == "wb_transit"
                        else ("confirmed" if source_document else "missing_or_zero")
                    )
                    if per_unit <= ZERO and status not in {"direct_zero_confirmed", "confirmed"}:
                        continue
                    plans.append(
                        {
                            "component_type": component_type,
                            "shipment_id": "",
                            "supply_id": str(row["wb_supply_id"]),
                            "nm_id": int(row["nm_id"]),
                            "quantity": _text(qty),
                            "recognized_amount_rub": _text(qty * per_unit),
                            "recognized_date": str(row["accepted_date"] or row["supply_date"] or "")[:10],
                            "paid_amount_rub": "0",
                            "paid_equivalent_quantity": "0",
                            "paid_date": None,
                            "allocation_method": "wb_supply_accepted_quantity",
                            "source_document_id": source_document,
                            "source_line_id": str(row["wb_supply_cost_layer_id"]),
                            "evidence": {
                                "legacy_component_layer": str(row["wb_supply_cost_layer_id"]),
                                "status": status,
                            },
                            "confirmation_status": status,
                        }
                    )
        changed = 0
        invalidated: str | None = None
        now = self.timestamp_factory()
        with _connect(self.runtime.db_path) as conn:
            ensure_canonical_cost_schema(conn)
            had_components = conn.execute(
                "SELECT 1 FROM sheet_vitrina_v1_canonical_cost_components LIMIT 1"
            ).fetchone() is not None
            for plan in plans:
                identity = _stable_hash({
                    key: plan[key] for key in (
                        "component_type", "shipment_id", "supply_id", "nm_id",
                        "source_document_id", "source_line_id",
                    )
                })
                fingerprint = _stable_hash(plan)
                existing = conn.execute(
                    """
                    SELECT component_id,fingerprint,version,recognized_date,paid_date
                    FROM sheet_vitrina_v1_canonical_cost_components
                    WHERE component_identity=? AND is_current=1
                    """,
                    (identity,),
                ).fetchone()
                if existing is not None and str(existing["fingerprint"]) == fingerprint:
                    continue
                version = int(existing["version"] or 0) + 1 if existing else 1
                if existing is not None:
                    conn.execute(
                        "UPDATE sheet_vitrina_v1_canonical_cost_components SET is_current=0,superseded_at=? WHERE component_id=?",
                        (now, existing["component_id"]),
                    )
                    candidates = [
                        value for value in (
                            str(existing["recognized_date"] or ""), str(existing["paid_date"] or ""),
                            plan["recognized_date"], plan["paid_date"],
                        ) if value
                    ]
                    if candidates:
                        changed_from = min(candidates)
                        invalidated = min(invalidated, changed_from) if invalidated else changed_from
                elif had_components:
                    candidates = [
                        value for value in (plan["recognized_date"], plan["paid_date"])
                        if value
                    ]
                    if candidates:
                        changed_from = min(candidates)
                        invalidated = min(invalidated, changed_from) if invalidated else changed_from
                component_id = f"ccc_{identity[:16]}_{version}"
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_canonical_cost_components(
                        component_id,component_identity,component_type,shipment_id,supply_id,nm_id,
                        quantity,recognized_amount_rub,recognized_date,paid_amount_rub,paid_date,
                        paid_equivalent_quantity,
                        allocation_method,source_document_id,source_line_id,evidence_json,
                        confirmation_status,fingerprint,version,is_current,supersedes_id,
                        created_at,superseded_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        component_id, identity, plan["component_type"], plan["shipment_id"],
                        plan["supply_id"], plan["nm_id"], plan["quantity"],
                        plan["recognized_amount_rub"], plan["recognized_date"],
                        plan["paid_amount_rub"], plan["paid_date"], plan["paid_equivalent_quantity"],
                        plan["allocation_method"],
                        plan["source_document_id"], plan["source_line_id"],
                        _json_dumps(plan["evidence"]), plan["confirmation_status"], fingerprint,
                        version, 1, str(existing["component_id"]) if existing else None, now, None,
                    ),
                )
                changed += 1
            conn.commit()
        return changed, invalidated

    def _materialize_movement_cost_layers(self, date_to: str) -> int:
        baseline = self._baseline_costs()
        # physical quantity, recognized capital, cost-covered quantity,
        # primary-confirmed quantity
        recognized_wac: dict[int, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
        paid_wac: dict[int, tuple[Decimal, Decimal]] = {}
        physical_opening = self.physical_quantities_as_of(CUTOVER_DATE)
        for nm_id, costs in baseline.items():
            qty = physical_opening.get(nm_id, {}).get(STAGE_FF, ZERO)
            recognized_wac[nm_id] = (
                qty,
                qty * costs["recognized"],
                qty,
                qty * costs["confirmation"],
            )
            paid_wac[nm_id] = (qty, qty * costs["paid"])
        plans: list[dict[str, Any]] = []
        with _connect(self.runtime.db_path) as conn:
            baseline_open = {
                (item["supply_id"], item["nm_id"]): item
                for item in _wb_movement_evidence(conn, as_of_date=CUTOVER_DATE)
                if _decimal(item["open_quantity"]) > ZERO
            }
            for operation in _ff_operation_rows(conn):
                effective = _ff_operation_effective_date(conn, operation)
                if str(operation["operation_type"]) != "auto_writeoff" or not effective or effective > CUTOVER_DATE:
                    continue
                supply_id = str(operation["source_object_id"] or "")
                for line in conn.execute(
                    "SELECT nm_id,quantity_delta FROM sheet_vitrina_v1_ff_stock_operation_lines WHERE operation_id=?",
                    (operation["operation_id"],),
                ).fetchall():
                    nm_id = int(line["nm_id"])
                    sent = abs(min(_decimal(line["quantity_delta"]), ZERO))
                    if sent <= ZERO or (supply_id, nm_id) not in baseline_open:
                        continue
                    costs = baseline.get(nm_id)
                    if costs is None:
                        raise CanonicalCostBlocked(
                            "baseline_transit_cost_missing",
                            {"supply_id": supply_id, "nm_id": nm_id},
                        )
                    plans.append({
                        "operation_id": str(operation["operation_id"]),
                        "supply_id": supply_id,
                        "nm_id": nm_id,
                        "effective_date": CUTOVER_DATE,
                        "sent_quantity": _text(sent),
                        "paid_equivalent_quantity": _text(sent),
                        "cost_coverage_share": "1",
                        "confirmation_share": _text(costs["confirmation"]),
                        "recognized_unit_cost_rub": _text(costs["recognized"]),
                        "paid_unit_cost_rub": _text(costs["paid"]),
                        "recognized_capital_rub": _text(sent * costs["recognized"]),
                        "paid_capital_rub": _text(sent * costs["paid"]),
                        "ff_wac_quantity_before": "baseline",
                        "source_operation_key": str(operation["source_key"]),
                    })
            for operation in _ff_operation_rows(conn):
                effective = _ff_operation_effective_date(conn, operation)
                if not effective or effective <= CUTOVER_DATE or effective > date_to:
                    continue
                lines = conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_ff_stock_operation_lines WHERE operation_id=? ORDER BY line_no",
                    (operation["operation_id"],),
                ).fetchall()
                positive_lines = [line for line in lines if _decimal(line["quantity_delta"]) > ZERO]
                if positive_lines:
                    shipment_id = str(operation["source_object_id"] or "") if str(operation["source_type"]) == "supplier_shipment" else ""
                    component_costs: dict[int, dict[str, Decimal]] = {}
                    for row in conn.execute(
                            """
                            SELECT nm_id,quantity,recognized_amount_rub,paid_amount_rub,
                                   recognized_date,paid_date,paid_equivalent_quantity,
                                   confirmation_status
                            FROM sheet_vitrina_v1_canonical_cost_components
                            WHERE shipment_id=? AND is_current=1
                            """,
                            (shipment_id,),
                        ).fetchall():
                        nm_id = int(row["nm_id"])
                        bucket = component_costs.setdefault(
                            nm_id,
                            {"recognized_amount": ZERO, "paid_amount": ZERO,
                             "quantity": _decimal(row["quantity"]), "paid_quantity": ZERO,
                             "confirmed": ONE},
                        )
                        recognized_applicable = (
                            str(row["recognized_date"] or "") <= effective
                        )
                        paid_applicable = bool(row["paid_date"]) and (
                            str(row["paid_date"]) <= effective
                        )
                        if recognized_applicable:
                            bucket["recognized_amount"] += _decimal(row["recognized_amount_rub"])
                        if paid_applicable:
                            bucket["paid_amount"] += _decimal(row["paid_amount_rub"])
                            bucket["paid_quantity"] += _decimal(row["paid_equivalent_quantity"])
                        if recognized_applicable and str(row["confirmation_status"]) != "confirmed":
                            bucket["confirmed"] = ZERO
                    component_costs = {
                        nm_id: {
                            "recognized": _safe_ratio(costs["recognized_amount"], costs["quantity"]),
                            "paid": _safe_ratio(costs["paid_amount"], costs["paid_quantity"]),
                            "paid_quantity": costs["paid_quantity"],
                            "confirmed": costs["confirmed"],
                        }
                        for nm_id, costs in component_costs.items()
                    }
                    for line in positive_lines:
                        nm_id = int(line["nm_id"])
                        qty = _decimal(line["quantity_delta"])
                        costs = component_costs.get(nm_id)
                        rq, rc, covered, confirmed = recognized_wac.get(
                            nm_id, (ZERO, ZERO, ZERO, ZERO)
                        )
                        pq, pc = paid_wac.get(nm_id, (ZERO, ZERO))
                        if costs is None or costs["recognized"] <= ZERO:
                            recognized_wac[nm_id] = (rq + qty, rc, covered, confirmed)
                            continue
                        recognized_wac[nm_id] = (
                            rq + qty,
                            rc + qty * costs["recognized"],
                            covered + qty,
                            confirmed + qty * costs["confirmed"],
                        )
                        if costs["paid"] > ZERO and costs["paid_quantity"] > ZERO:
                            receipt_paid_qty = min(costs["paid_quantity"], qty)
                            paid_wac[nm_id] = (
                                pq + receipt_paid_qty,
                                pc + receipt_paid_qty * costs["paid"],
                            )
                for line in (line for line in lines if _decimal(line["quantity_delta"]) < ZERO):
                    nm_id = int(line["nm_id"])
                    sent = abs(min(_decimal(line["quantity_delta"]), ZERO))
                    rq, rc, covered, confirmed = recognized_wac.get(
                        nm_id, (ZERO, ZERO, ZERO, ZERO)
                    )
                    pq, pc = paid_wac.get(nm_id, (ZERO, ZERO))
                    if rq < sent:
                        raise CanonicalCostBlocked(
                            "ff_writeoff_exceeds_cost_inventory",
                            {"operation_id": operation["operation_id"], "nm_id": nm_id, "sent": _text(sent), "available": _text(rq)},
                        )
                    coverage_share = min(_safe_ratio(covered, rq), ONE)
                    confirmation_share = min(_safe_ratio(confirmed, rq), ONE)
                    covered_sent = sent * coverage_share
                    recognized_unit = _safe_ratio(rc, covered)
                    paid_unit = _safe_ratio(pc, pq) if pq > ZERO else ZERO
                    recognized_removed = covered_sent * recognized_unit
                    paid_share = min(_safe_ratio(pq, rq), ONE)
                    paid_equivalent_sent = sent * paid_share
                    paid_removed = paid_equivalent_sent * paid_unit
                    if str(operation["operation_type"]) == "auto_writeoff":
                        supply_cost_status = conn.execute(
                            """
                            SELECT source_status FROM sheet_vitrina_v1_wb_supply_cost_layers
                            WHERE wb_supply_id=? AND nm_id=? AND is_current=1
                            """,
                            (str(operation["source_object_id"] or ""), nm_id),
                        ).fetchone()
                        movement_confirmation_share = (
                            confirmation_share
                            if supply_cost_status is not None
                            and str(supply_cost_status["source_status"]) == "confirmed"
                            else ZERO
                        )
                        addons = conn.execute(
                            """
                            SELECT quantity,recognized_amount_rub,paid_amount_rub
                            FROM sheet_vitrina_v1_canonical_cost_components
                            WHERE supply_id=? AND nm_id=? AND is_current=1
                            """,
                            (str(operation["source_object_id"] or ""), nm_id),
                        ).fetchall()
                        addon_recognized = ZERO
                        addon_paid = ZERO
                        for addon in addons:
                            addon_qty = _decimal(addon["quantity"])
                            addon_recognized += sent * _safe_ratio(
                                _decimal(addon["recognized_amount_rub"]), addon_qty
                            )
                            addon_paid += sent * _safe_ratio(
                                _decimal(addon["paid_amount_rub"]), addon_qty
                            )
                        supply_id = str(operation["source_object_id"] or "")
                        movement_recognized_capital = recognized_removed + addon_recognized
                        movement_paid_capital = paid_removed + addon_paid
                        plans.append({
                            "operation_id": str(operation["operation_id"]),
                            "supply_id": supply_id,
                            "nm_id": nm_id,
                            "effective_date": effective,
                            "sent_quantity": _text(sent),
                            "paid_equivalent_quantity": _text(paid_equivalent_sent),
                            "cost_coverage_share": _text(coverage_share),
                            "confirmation_share": _text(movement_confirmation_share),
                            "recognized_unit_cost_rub": _text(
                                _safe_ratio(movement_recognized_capital, covered_sent)
                            ),
                            "paid_unit_cost_rub": _text(
                                _safe_ratio(movement_paid_capital, paid_equivalent_sent)
                            ),
                            "recognized_capital_rub": _text(movement_recognized_capital),
                            "paid_capital_rub": _text(movement_paid_capital),
                            "ff_wac_quantity_before": _text(rq),
                            "source_operation_key": str(operation["source_key"]),
                        })
                    recognized_wac[nm_id] = (
                        rq - sent,
                        rc - recognized_removed,
                        covered - covered_sent,
                        max(confirmed - sent * confirmation_share, ZERO),
                    )
                    if pq > ZERO:
                        paid_wac[nm_id] = (
                            pq - paid_equivalent_sent,
                            pc - paid_removed,
                        )
        return self._replace_versioned_movement_plans(plans)

    def _replace_versioned_movement_plans(self, plans: Iterable[Mapping[str, Any]]) -> int:
        changed = 0
        now = self.timestamp_factory()
        with _connect(self.runtime.db_path) as conn:
            ensure_canonical_cost_schema(conn)
            for plan in plans:
                identity = f"{plan['operation_id']}:{plan['nm_id']}"
                fingerprint = _stable_hash(plan)
                row = conn.execute(
                    "SELECT movement_layer_id,fingerprint,version FROM sheet_vitrina_v1_canonical_cost_movement_layers WHERE movement_identity=? AND is_current=1",
                    (identity,),
                ).fetchone()
                if row is not None and str(row["fingerprint"]) == fingerprint:
                    continue
                version = int(row["version"] or 0) + 1 if row else 1
                if row:
                    conn.execute(
                        "UPDATE sheet_vitrina_v1_canonical_cost_movement_layers SET is_current=0,superseded_at=? WHERE movement_layer_id=?",
                        (now, row["movement_layer_id"]),
                    )
                layer_id = f"ccm_{_stable_hash(identity)[:16]}_{version}"
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_canonical_cost_movement_layers(
                        movement_layer_id,movement_identity,operation_id,supply_id,nm_id,effective_date,
                        sent_quantity,paid_equivalent_quantity,cost_coverage_share,confirmation_share,
                        recognized_unit_cost_rub,paid_unit_cost_rub,
                        recognized_capital_rub,paid_capital_rub,ff_wac_quantity_before,
                        source_operation_key,fingerprint,version,is_current,supersedes_id,created_at,superseded_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        layer_id, identity, plan["operation_id"], plan["supply_id"], plan["nm_id"],
                        plan["effective_date"], plan["sent_quantity"], plan["paid_equivalent_quantity"],
                        plan["cost_coverage_share"], plan["confirmation_share"],
                        plan["recognized_unit_cost_rub"], plan["paid_unit_cost_rub"], plan["recognized_capital_rub"],
                        plan["paid_capital_rub"], plan["ff_wac_quantity_before"],
                        plan["source_operation_key"], fingerprint, version, 1,
                        str(row["movement_layer_id"]) if row else None, now, None,
                    ),
                )
                changed += 1
            conn.commit()
        return changed

    def _materialize_outstanding_layers(self, date_to: str) -> int:
        with _connect(self.runtime.db_path) as conn:
            movements = [dict(row) for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_canonical_cost_movement_layers WHERE is_current=1 AND effective_date<=? ORDER BY effective_date,supply_id,nm_id",
                (date_to,),
            ).fetchall()]
            evidence = _wb_supply_cache_evidence(conn, date_to=date_to)
        accepted = {(item["supply_id"], item["nm_id"]): item for item in evidence if not item["is_doprinato"]}
        open_layers: list[dict[str, Any]] = []
        for movement in movements:
            key = (str(movement["supply_id"]), int(movement["nm_id"]))
            fact = accepted.get(key, {})
            sent = _decimal(movement["sent_quantity"])
            accepted_qty = _decimal(fact.get("accepted_quantity"))
            if accepted_qty > sent:
                raise CanonicalCostBlocked(
                    "accepted_quantity_exceeds_sent",
                    {"supply_id": key[0], "nm_id": key[1]},
                )
            open_qty = sent - accepted_qty
            if open_qty < ZERO:
                raise CanonicalCostBlocked("accepted_quantity_exceeds_sent", {"supply_id": key[0], "nm_id": key[1]})
            if not bool(fact.get("is_final_accepted")):
                continue
            open_layers.append({
                "original_supply_id": key[0], "nm_id": key[1],
                "warehouse": str(fact.get("warehouse") or ""),
                "destination": str(fact.get("destination") or ""),
                "original_movement_layer_id": str(movement["movement_layer_id"]),
                "sent_quantity": _text(sent), "accepted_quantity": _text(accepted_qty),
                "open_quantity": _text(open_qty),
                "paid_equivalent_quantity": _text(
                    open_qty * _safe_ratio(_decimal(movement["paid_equivalent_quantity"]), sent)
                ),
                "paid_equivalent_total_quantity": _text(
                    open_qty * _safe_ratio(_decimal(movement["paid_equivalent_quantity"]), sent)
                ),
                "cost_coverage_share": str(movement["cost_coverage_share"]),
                "confirmation_share": str(movement["confirmation_share"]),
                "recognized_unit_cost_rub": str(movement["recognized_unit_cost_rub"]),
                "paid_unit_cost_rub": str(movement["paid_unit_cost_rub"]),
                "writeoff_date": str(movement["effective_date"]),
                "accepted_date": str(fact.get("accepted_date") or ""),
                "provenance": {"acceptance_source": fact.get("source_identity", "")},
            })
        open_layers = reconcile_outstanding_layers(
            open_layers,
            [
                item for item in evidence
                if item["is_doprinato"] and item["accepted_date"] > CUTOVER_DATE
            ],
        )
        changed = 0
        now = self.timestamp_factory()
        with _connect(self.runtime.db_path) as conn:
            ensure_canonical_cost_schema(conn)
            current_ids: set[str] = set()
            for plan in open_layers:
                identity = f"{plan['original_supply_id']}:{plan['nm_id']}"
                current_ids.add(identity)
                fingerprint = _stable_hash(plan)
                row = conn.execute(
                    "SELECT outstanding_layer_id,fingerprint,version FROM sheet_vitrina_v1_canonical_cost_wb_outstanding_layers WHERE outstanding_identity=? AND is_current=1",
                    (identity,),
                ).fetchone()
                if row is not None and str(row["fingerprint"]) == fingerprint:
                    continue
                version = int(row["version"] or 0) + 1 if row else 1
                if row:
                    conn.execute(
                        "UPDATE sheet_vitrina_v1_canonical_cost_wb_outstanding_layers SET is_current=0,superseded_at=? WHERE outstanding_layer_id=?",
                        (now, row["outstanding_layer_id"]),
                    )
                layer_id = f"cco_{_stable_hash(identity)[:16]}_{version}"
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_canonical_cost_wb_outstanding_layers(
                        outstanding_layer_id,outstanding_identity,original_supply_id,nm_id,warehouse,destination,
                        original_movement_layer_id,sent_quantity,accepted_quantity,open_quantity,
                        paid_equivalent_quantity,paid_equivalent_total_quantity,
                        cost_coverage_share,confirmation_share,
                        recognized_unit_cost_rub,paid_unit_cost_rub,writeoff_date,accepted_date,
                        provenance_json,fingerprint,version,is_current,supersedes_id,created_at,superseded_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,NULL)
                    """,
                    (
                        layer_id, identity, plan["original_supply_id"], plan["nm_id"], plan["warehouse"],
                        plan["destination"], plan["original_movement_layer_id"], plan["sent_quantity"],
                        plan["accepted_quantity"], plan["open_quantity"], plan["paid_equivalent_quantity"],
                        plan["paid_equivalent_total_quantity"],
                        plan["cost_coverage_share"], plan["confirmation_share"], plan["recognized_unit_cost_rub"],
                        plan["paid_unit_cost_rub"], plan["writeoff_date"], plan["accepted_date"],
                        _json_dumps(plan["provenance"]), fingerprint, version,
                        str(row["outstanding_layer_id"]) if row else None, now,
                    ),
                )
                changed += 1
            stale = conn.execute(
                "SELECT outstanding_layer_id,outstanding_identity FROM sheet_vitrina_v1_canonical_cost_wb_outstanding_layers WHERE is_current=1"
            ).fetchall()
            for row in stale:
                if str(row["outstanding_identity"]) not in current_ids:
                    conn.execute(
                        "UPDATE sheet_vitrina_v1_canonical_cost_wb_outstanding_layers SET is_current=0,superseded_at=? WHERE outstanding_layer_id=?",
                        (now, row["outstanding_layer_id"]),
                    )
                    changed += 1
            conn.commit()
        return changed

    def _supplier_stage_costs_as_of(
        self, as_of_date: str
    ) -> dict[tuple[int, str], dict[str, Decimal | str]]:
        """Full physical quantity plus date-bounded paid-equivalent allocation."""
        result: dict[tuple[int, str], dict[str, Decimal | str]] = {}
        baseline = self._baseline_costs()
        with _connect(self.runtime.db_path) as conn:
            rows = conn.execute(
                """
                SELECT shipment.*,line.internal_nm_id,line.qty,line.amount,line.unit_price
                FROM sheet_vitrina_v1_supplier_shipments shipment
                JOIN sheet_vitrina_v1_supplier_shipment_lines line
                  ON line.shipment_id=shipment.shipment_id AND line.line_type='product'
                WHERE line.internal_nm_id IS NOT NULL AND COALESCE(line.qty,0)>0
                ORDER BY shipment.shipment_id,line.sort_order
                """
            ).fetchall()
            for row in rows:
                registered = min(
                    value for value in (str(row["shipment_date"] or "")[:10], str(row["created_at"] or "")[:10])
                    if value
                )
                if registered > as_of_date:
                    continue
                shipped = str(row["actual_shipment_date"] or "")[:10]
                accepted = str(row["actual_ff_acceptance_date"] or "")[:10]
                if accepted and accepted <= as_of_date:
                    continue
                stage = STAGE_PRODUCTION_TO_FF if shipped and shipped <= as_of_date else STAGE_PRODUCTION
                shipment_id = str(row["shipment_id"])
                payments = conn.execute(
                    """
                    SELECT cny_delta,rub_value_delta FROM sheet_vitrina_v1_cny_ledger_operations
                    WHERE source_order_id=? AND operation_type='supplier_payment_out'
                      AND status='posted' AND operation_date<=?
                    """,
                    (shipment_id, as_of_date),
                ).fetchall()
                paid_cny = sum((abs(_decimal(item["cny_delta"])) for item in payments), ZERO)
                paid_rub = sum((abs(_decimal(item["rub_value_delta"])) for item in payments), ZERO)
                invoice_total = _decimal(row["invoice_amount_total"])
                paid_share = min(_safe_ratio(paid_cny, invoice_total), ONE)
                line_value = _decimal(row["amount"])
                qty = _decimal(row["qty"])
                product_total = _decimal(row["product_amount_total"])
                allocated_paid = paid_rub * _safe_ratio(line_value, product_total)
                paid_equivalent = qty * paid_share
                paid_unit = _safe_ratio(allocated_paid, paid_equivalent)
                ff_line = conn.execute(
                    """
                    SELECT cost.sku_ff_unit_cost_rub,cost.source_status
                    FROM sheet_vitrina_v1_supplier_ff_cost_layer_lines cost
                    JOIN sheet_vitrina_v1_supplier_ff_cost_layers layer ON layer.layer_id=cost.layer_id
                    WHERE layer.supplier_shipment_id=? AND layer.is_current=1 AND cost.nm_id=?
                      AND layer.accepted_ff_date<=?
                    ORDER BY cost.layer_line_id LIMIT 1
                    """,
                    (shipment_id, int(row["internal_nm_id"]), as_of_date),
                ).fetchone()
                recognized_unit = _decimal(ff_line["sku_ff_unit_cost_rub"]) if ff_line else ZERO
                if recognized_unit <= ZERO:
                    rate = _safe_ratio(paid_rub, paid_cny)
                    recognized_unit = _decimal(row["unit_price"]) * rate
                baseline_cost = baseline.get(int(row["internal_nm_id"]))
                baseline_owned = registered <= CUTOVER_DATE and baseline_cost is not None
                if baseline_owned and recognized_unit <= ZERO:
                    recognized_unit = _decimal(baseline_cost["recognized"])
                key = (int(row["internal_nm_id"]), stage)
                bucket = result.setdefault(
                    key,
                    {
                        "physical": ZERO, "paid_equivalent": ZERO,
                        "recognized_capital": ZERO, "paid_capital": ZERO,
                        "covered": ZERO, "confirmed": ZERO, "quality": "coverage_gap",
                    },
                )
                bucket["physical"] = _decimal(bucket["physical"]) + qty
                bucket["paid_equivalent"] = _decimal(bucket["paid_equivalent"]) + paid_equivalent
                bucket["recognized_capital"] = _decimal(bucket["recognized_capital"]) + qty * recognized_unit
                bucket["paid_capital"] = _decimal(bucket["paid_capital"]) + allocated_paid
                if recognized_unit > ZERO:
                    bucket["covered"] = _decimal(bucket["covered"]) + qty
                if ff_line is not None and str(ff_line["source_status"]) == "confirmed":
                    bucket["confirmed"] = _decimal(bucket["confirmed"]) + qty
                    bucket["quality"] = "primary_documents"
                elif recognized_unit > ZERO:
                    bucket["quality"] = (
                        "primary_documents"
                        if baseline_owned and _decimal(baseline_cost["confirmation"]) == ONE
                        else ("legacy_1c_fallback" if baseline_owned else "estimated_source")
                    )
                    if baseline_owned and _decimal(baseline_cost["confirmation"]) == ONE:
                        bucket["confirmed"] = _decimal(bucket["confirmed"]) + qty
        return result

    def _ff_costs_as_of(self, as_of_date: str) -> dict[int, dict[str, Decimal | str]]:
        baseline = self._baseline_costs()
        opening = self.physical_quantities_as_of(CUTOVER_DATE)
        state: dict[int, dict[str, Decimal | str]] = {
            nm_id: {
                "quantity": opening.get(nm_id, {}).get(STAGE_FF, ZERO),
                "recognized_capital": opening.get(nm_id, {}).get(STAGE_FF, ZERO) * costs["recognized"],
                "paid_quantity": opening.get(nm_id, {}).get(STAGE_FF, ZERO),
                "paid_capital": opening.get(nm_id, {}).get(STAGE_FF, ZERO) * costs["paid"],
                "covered_quantity": opening.get(nm_id, {}).get(STAGE_FF, ZERO),
                "confirmed_quantity": opening.get(nm_id, {}).get(STAGE_FF, ZERO) * costs["confirmation"],
                "quality": "primary_documents" if costs["confirmation"] == ONE else "legacy_1c_fallback",
            }
            for nm_id, costs in baseline.items()
        }
        with _connect(self.runtime.db_path) as conn:
            component_costs: dict[tuple[str, int], dict[str, Decimal | str]] = {}
            for row in conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_canonical_cost_components
                WHERE is_current=1 AND (recognized_date<=? OR (paid_date IS NOT NULL AND paid_date<=?))
                """,
                (as_of_date, as_of_date),
            ).fetchall():
                qty = _decimal(row["quantity"])
                key = (str(row["shipment_id"]), int(row["nm_id"]))
                bucket = component_costs.setdefault(
                    key,
                    {"recognized_amount": ZERO, "paid_amount": ZERO, "quantity": qty,
                     "paid_quantity": ZERO, "confirmed": ONE},
                )
                if str(row["recognized_date"] or "") <= as_of_date:
                    bucket["recognized_amount"] = _decimal(bucket["recognized_amount"]) + _decimal(row["recognized_amount_rub"])
                    if str(row["confirmation_status"]) != "confirmed":
                        bucket["confirmed"] = ZERO
                if row["paid_date"] and str(row["paid_date"]) <= as_of_date:
                    bucket["paid_amount"] = _decimal(bucket["paid_amount"]) + _decimal(row["paid_amount_rub"])
                    bucket["paid_quantity"] = _decimal(bucket["paid_quantity"]) + _decimal(row["paid_equivalent_quantity"])
            component_costs = {
                key: {
                    "recognized": _safe_ratio(_decimal(costs["recognized_amount"]), _decimal(costs["quantity"])),
                    "paid": _safe_ratio(_decimal(costs["paid_amount"]), _decimal(costs["paid_quantity"])),
                    "paid_quantity": _decimal(costs["paid_quantity"]),
                    "confirmed": _decimal(costs["confirmed"]),
                }
                for key, costs in component_costs.items()
            }
            for operation in _ff_operation_rows(conn):
                effective = _ff_operation_effective_date(conn, operation)
                if not effective or effective <= CUTOVER_DATE or effective > as_of_date:
                    continue
                for line in conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_ff_stock_operation_lines WHERE operation_id=? ORDER BY line_no",
                    (operation["operation_id"],),
                ).fetchall():
                    nm_id = int(line["nm_id"])
                    delta = _decimal(line["quantity_delta"])
                    bucket = state.setdefault(
                        nm_id,
                        {"quantity": ZERO, "recognized_capital": ZERO, "paid_quantity": ZERO,
                         "paid_capital": ZERO, "covered_quantity": ZERO,
                         "confirmed_quantity": ZERO, "quality": "coverage_gap"},
                    )
                    if delta > ZERO:
                        shipment_id = str(operation["source_object_id"] or "") if str(operation["source_type"]) == "supplier_shipment" else ""
                        costs = component_costs.get((shipment_id, nm_id))
                        if costs is None:
                            bucket["quantity"] = _decimal(bucket["quantity"]) + delta
                            bucket["quality"] = "coverage_gap"
                            continue
                        bucket["quantity"] = _decimal(bucket["quantity"]) + delta
                        bucket["recognized_capital"] = _decimal(bucket["recognized_capital"]) + delta * _decimal(costs["recognized"])
                        bucket["covered_quantity"] = _decimal(bucket["covered_quantity"]) + delta
                        if _decimal(costs["paid"]) > ZERO:
                            receipt_paid_qty = min(_decimal(costs["paid_quantity"]), delta)
                            bucket["paid_quantity"] = _decimal(bucket["paid_quantity"]) + receipt_paid_qty
                            bucket["paid_capital"] = _decimal(bucket["paid_capital"]) + receipt_paid_qty * _decimal(costs["paid"])
                        bucket["confirmed_quantity"] = _decimal(bucket["confirmed_quantity"]) + delta * _decimal(costs["confirmed"])
                        bucket["quality"] = "primary_documents" if costs["confirmed"] == ONE else "estimated_source"
                    elif delta < ZERO:
                        writeoff = abs(delta)
                        quantity = _decimal(bucket["quantity"])
                        if writeoff > quantity:
                            raise CanonicalCostBlocked("ff_quantity_replay_negative", {"nm_id": nm_id, "as_of_date": as_of_date})
                        covered_quantity = _decimal(bucket["covered_quantity"])
                        covered_removed = writeoff * _safe_ratio(covered_quantity, quantity)
                        rec_unit = _safe_ratio(
                            _decimal(bucket["recognized_capital"]), covered_quantity
                        )
                        bucket["quantity"] = quantity - writeoff
                        bucket["recognized_capital"] = (
                            _decimal(bucket["recognized_capital"])
                            - covered_removed * rec_unit
                        )
                        bucket["covered_quantity"] = covered_quantity - covered_removed
                        paid_qty = _decimal(bucket["paid_quantity"])
                        paid_removed = writeoff * min(_safe_ratio(paid_qty, quantity), ONE)
                        paid_unit = _safe_ratio(_decimal(bucket["paid_capital"]), paid_qty)
                        bucket["paid_quantity"] = paid_qty - paid_removed
                        bucket["paid_capital"] = _decimal(bucket["paid_capital"]) - paid_removed * paid_unit
                        confirmed_qty = _decimal(bucket["confirmed_quantity"])
                        bucket["confirmed_quantity"] = max(
                            confirmed_qty - writeoff * _safe_ratio(confirmed_qty, quantity), ZERO
                        )
        return state

    def _transit_costs_as_of(self, as_of_date: str) -> dict[int, dict[str, Decimal | str]]:
        with _connect(self.runtime.db_path) as conn:
            movements = {
                (str(row["supply_id"]), int(row["nm_id"])): dict(row)
                for row in conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_canonical_cost_movement_layers WHERE is_current=1 AND effective_date<=?",
                    (as_of_date,),
                ).fetchall()
            }
            evidence = _wb_movement_evidence(conn, as_of_date=as_of_date)
        result: dict[int, dict[str, Decimal | str]] = {}
        for fact in evidence:
            open_qty = _decimal(fact["open_quantity"])
            if open_qty <= ZERO:
                continue
            movement = movements.get((fact["supply_id"], fact["nm_id"]))
            bucket = result.setdefault(
                fact["nm_id"],
                {"physical": ZERO, "recognized_capital": ZERO, "paid_capital": ZERO,
                 "paid_equivalent": ZERO, "covered": ZERO, "confirmed": ZERO,
                 "quality": "coverage_gap"},
            )
            bucket["physical"] = _decimal(bucket["physical"]) + open_qty
            if movement is None:
                continue
            recognized = _decimal(movement["recognized_unit_cost_rub"])
            paid = _decimal(movement["paid_unit_cost_rub"])
            covered_qty = open_qty * _decimal(movement["cost_coverage_share"])
            paid_equivalent = open_qty * _safe_ratio(
                _decimal(movement["paid_equivalent_quantity"]),
                _decimal(movement["sent_quantity"]),
            )
            bucket["recognized_capital"] = _decimal(bucket["recognized_capital"]) + covered_qty * recognized
            bucket["paid_capital"] = _decimal(bucket["paid_capital"]) + paid_equivalent * paid
            bucket["covered"] = _decimal(bucket["covered"]) + covered_qty
            bucket["paid_equivalent"] = _decimal(bucket["paid_equivalent"]) + paid_equivalent
            bucket["confirmed"] = _decimal(bucket["confirmed"]) + open_qty * _decimal(movement["confirmation_share"])
            bucket["quality"] = "primary_documents"
        for bucket in result.values():
            if _decimal(bucket["covered"]) < _decimal(bucket["physical"]):
                bucket["quality"] = "coverage_gap"
            elif _decimal(bucket["confirmed"]) < _decimal(bucket["physical"]):
                bucket["quality"] = "estimated_source"
        return result

    def _wb_cost_states(self, dates: Iterable[str]) -> dict[str, dict[int, dict[str, Decimal | str]]]:
        ordered = sorted(dates)
        baseline = self._baseline_costs()
        previous: dict[int, dict[str, Decimal | str]] = {}
        result: dict[str, dict[int, dict[str, Decimal | str]]] = {}
        with _connect(self.runtime.db_path) as conn:
            movement_rows = [dict(row) for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_canonical_cost_movement_layers WHERE is_current=1"
            ).fetchall()]
            movement_by_key = {
                (str(row["supply_id"]), int(row["nm_id"])): row
                for row in movement_rows
            }
            movement_by_id = {
                str(row["movement_layer_id"]): row for row in movement_rows
            }
            acceptance = [
                item for item in _wb_supply_cache_evidence(conn, date_to=max(ordered))
                if not item["is_doprinato"] and item["is_final_accepted"]
            ]
            doprinato_inbounds: list[dict[str, Any]] = []
            for row in conn.execute(
                """
                SELECT nm_id,original_movement_layer_id,provenance_json
                FROM sheet_vitrina_v1_canonical_cost_wb_outstanding_layers
                WHERE is_current=1
                """
            ).fetchall():
                movement = movement_by_id.get(str(row["original_movement_layer_id"]))
                if movement is None:
                    continue
                provenance = _json_loads(row["provenance_json"])
                for item in provenance.get("doprinato") or []:
                    accepted_date = str(item.get("accepted_date") or "")
                    qty = _decimal(item.get("quantity"))
                    if accepted_date and qty > ZERO:
                        doprinato_inbounds.append({
                            "nm_id": int(row["nm_id"]),
                            "accepted_date": accepted_date,
                            "quantity": qty,
                            "movement": movement,
                        })
        previous_day = CUTOVER_DATE
        for day in ordered:
            stock = {nm_id: _decimal(qty) for nm_id, qty in self._snapshot_metric(day, OFFICIAL_WB_STOCK_METRIC).items()}
            current: dict[int, dict[str, Decimal | str]] = {}
            for nm_id in sorted(set(stock) | set(previous) | set(baseline)):
                stock_qty = stock.get(nm_id, ZERO)
                if day == CUTOVER_DATE:
                    cost = baseline.get(nm_id)
                    current[nm_id] = {
                        "quantity": stock_qty,
                        "recognized_capital": stock_qty * (cost["recognized"] if cost else ZERO),
                        "paid_quantity": stock_qty if cost and cost["paid"] > ZERO else ZERO,
                        "paid_capital": stock_qty * (cost["paid"] if cost else ZERO),
                        "covered": stock_qty if cost else ZERO,
                        "confirmed": stock_qty * (cost["confirmation"] if cost else ZERO),
                        "quality": "primary_documents" if cost and cost["confirmation"] == ONE else ("legacy_1c_fallback" if cost else "coverage_gap"),
                    }
                    continue
                prev = previous.get(
                    nm_id,
                    {
                        "quantity": ZERO, "recognized_capital": ZERO,
                        "paid_quantity": ZERO, "paid_capital": ZERO,
                        "covered": ZERO, "confirmed": ZERO,
                        "quality": "coverage_gap",
                    },
                )
                inbounds: list[dict[str, Decimal]] = []
                for fact in acceptance:
                    if (
                        fact["nm_id"] != nm_id
                        or not (previous_day < fact["accepted_date"] <= day)
                    ):
                        continue
                    movement = movement_by_key.get((fact["supply_id"], nm_id))
                    if movement is None:
                        continue
                    movement_sent = _decimal(movement["sent_quantity"])
                    qty = _decimal(fact["accepted_quantity"])
                    if qty > movement_sent:
                        raise CanonicalCostBlocked(
                            "accepted_quantity_exceeds_sent",
                            {"supply_id": fact["supply_id"], "nm_id": nm_id},
                        )
                    ratio = _safe_ratio(qty, movement_sent)
                    inbounds.append({
                        "quantity": qty,
                        "recognized_capital": _decimal(movement["recognized_capital_rub"]) * ratio,
                        "paid_quantity": _decimal(movement["paid_equivalent_quantity"]) * ratio,
                        "paid_capital": _decimal(movement["paid_capital_rub"]) * ratio,
                        "covered": qty * _decimal(movement["cost_coverage_share"]),
                        "confirmed": qty * _decimal(movement["confirmation_share"]),
                    })
                for fact in doprinato_inbounds:
                    if (
                        fact["nm_id"] != nm_id
                        or not (previous_day < fact["accepted_date"] <= day)
                    ):
                        continue
                    movement = fact["movement"]
                    qty = min(
                        _decimal(fact["quantity"]), _decimal(movement["sent_quantity"])
                    )
                    movement_sent = _decimal(movement["sent_quantity"])
                    ratio = _safe_ratio(qty, movement_sent)
                    inbounds.append({
                        "quantity": qty,
                        "recognized_capital": _decimal(movement["recognized_capital_rub"]) * ratio,
                        "paid_quantity": _decimal(movement["paid_equivalent_quantity"]) * ratio,
                        "paid_capital": _decimal(movement["paid_capital_rub"]) * ratio,
                        "covered": qty * _decimal(movement["cost_coverage_share"]),
                        "confirmed": qty * _decimal(movement["confirmation_share"]),
                    })
                inbound_qty = sum((item["quantity"] for item in inbounds), ZERO)
                prev_qty = _decimal(prev["quantity"])
                pool_qty = prev_qty + inbound_qty
                pool_recognized = _decimal(prev["recognized_capital"]) + sum(
                    (item["recognized_capital"] for item in inbounds), ZERO
                )
                pool_paid_quantity = _decimal(prev["paid_quantity"]) + sum(
                    (item["paid_quantity"] for item in inbounds), ZERO
                )
                pool_paid = _decimal(prev["paid_capital"]) + sum(
                    (item["paid_capital"] for item in inbounds), ZERO
                )
                pool_covered = _decimal(prev["covered"]) + sum(
                    (item["covered"] for item in inbounds), ZERO
                )
                pool_confirmed = _decimal(prev["confirmed"]) + sum(
                    (item["confirmed"] for item in inbounds), ZERO
                )
                unexplained_growth = max(stock_qty - pool_qty, ZERO)
                retained = min(_safe_ratio(stock_qty, pool_qty), ONE)
                recognized_capital = pool_recognized * retained
                paid_quantity = pool_paid_quantity * retained
                paid_capital = pool_paid * retained
                covered = pool_covered * retained
                confirmed = pool_confirmed * retained
                if unexplained_growth > ZERO and pool_qty > ZERO and pool_covered > ZERO:
                    recognized_capital += unexplained_growth * _safe_ratio(
                        pool_recognized, pool_covered
                    )
                    covered += unexplained_growth
                    paid_share = min(_safe_ratio(pool_paid_quantity, pool_qty), ONE)
                    growth_paid_quantity = unexplained_growth * paid_share
                    paid_quantity += growth_paid_quantity
                    paid_capital += growth_paid_quantity * _safe_ratio(
                        pool_paid, pool_paid_quantity
                    )
                quality = "primary_documents"
                if stock_qty > ZERO and covered <= ZERO:
                    quality = "coverage_gap"
                elif covered < stock_qty:
                    quality = "coverage_gap"
                elif unexplained_growth > ZERO:
                    quality = "unexplained_growth_existing_wac"
                elif confirmed < stock_qty:
                    quality = "estimated_source"
                current[nm_id] = {
                    "quantity": stock_qty, "recognized_capital": recognized_capital,
                    "paid_quantity": paid_quantity, "paid_capital": paid_capital,
                    "covered": min(covered, stock_qty), "confirmed": min(confirmed, stock_qty),
                    "quality": quality,
                }
            result[day] = current
            previous = current
            previous_day = day
        return result

    def _materialize_daily_state(self, start: str, end: str) -> int:
        baseline = self._baseline_costs()
        dates = sorted(set(
            self.runtime.list_sheet_vitrina_ready_snapshot_dates_any_bundle(
                date_from=start, date_to=end, descending=False
            ) + [end]
        ))
        wb_by_date = self._wb_cost_states(dates)
        changed = 0
        now = self.timestamp_factory()
        with _connect(self.runtime.db_path) as conn:
            ensure_canonical_cost_schema(conn)
            outstanding_rows = [dict(row) for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_canonical_cost_wb_outstanding_layers WHERE is_current=1"
            ).fetchall()]
            for day in dates:
                physical = self.physical_quantities_as_of(day)
                supplier_costs = self._supplier_stage_costs_as_of(day)
                ff_costs = self._ff_costs_as_of(day)
                transit_costs = self._transit_costs_as_of(day)
                for nm_id, stages in physical.items():
                    costs = baseline.get(nm_id)
                    for stage in STAGES:
                        qty = stages.get(stage, ZERO)
                        recognized_unit = costs["recognized"] if costs else ZERO
                        paid_unit = costs["paid"] if costs else ZERO
                        confirmation = costs["confirmation"] if costs else ZERO
                        paid_equivalent = qty if paid_unit > ZERO else ZERO
                        covered = qty if recognized_unit > ZERO else ZERO
                        quality = "primary_documents" if confirmation == ONE else ("legacy_1c_fallback" if costs else "coverage_gap")
                        recognized_capital = qty * recognized_unit
                        paid_capital = paid_equivalent * paid_unit
                        confirmed_quantity = qty * confirmation
                        if stage in {STAGE_PRODUCTION, STAGE_PRODUCTION_TO_FF}:
                            source = supplier_costs.get((nm_id, stage))
                            if source:
                                paid_equivalent = _decimal(source["paid_equivalent"])
                                recognized_capital = _decimal(source["recognized_capital"])
                                paid_capital = _decimal(source["paid_capital"])
                                covered = _decimal(source["covered"])
                                confirmed_quantity = _decimal(source["confirmed"])
                                quality = str(source["quality"])
                                recognized_unit = _safe_ratio(recognized_capital, qty)
                                paid_unit = _safe_ratio(paid_capital, paid_equivalent)
                            else:
                                recognized_capital = ZERO
                                paid_capital = ZERO
                                paid_equivalent = ZERO
                                covered = ZERO
                                confirmed_quantity = ZERO
                                quality = "coverage_gap"
                        elif stage == STAGE_FF:
                            source = ff_costs.get(nm_id)
                            if source:
                                recognized_capital = _decimal(source["recognized_capital"])
                                paid_capital = _decimal(source["paid_capital"])
                                paid_equivalent = _decimal(source["paid_quantity"])
                                covered = min(_decimal(source["covered_quantity"]), qty)
                                confirmed_quantity = min(_decimal(source["confirmed_quantity"]), qty)
                                quality = str(source["quality"])
                                recognized_unit = _safe_ratio(recognized_capital, qty)
                                paid_unit = _safe_ratio(paid_capital, paid_equivalent)
                            else:
                                recognized_capital = paid_capital = confirmed_quantity = ZERO
                                paid_equivalent = covered = ZERO
                                quality = "coverage_gap"
                        elif stage == STAGE_FF_TO_WB:
                            source = transit_costs.get(nm_id)
                            if source:
                                recognized_capital = _decimal(source["recognized_capital"])
                                paid_capital = _decimal(source["paid_capital"])
                                paid_equivalent = _decimal(source["paid_equivalent"])
                                covered = _decimal(source["covered"])
                                confirmed_quantity = _decimal(source["confirmed"])
                                quality = str(source["quality"])
                                recognized_unit = _safe_ratio(recognized_capital, qty)
                                paid_unit = _safe_ratio(paid_capital, paid_equivalent)
                            else:
                                recognized_capital = paid_capital = confirmed_quantity = ZERO
                                paid_equivalent = covered = ZERO
                                quality = "coverage_gap"
                        elif stage == STAGE_WB:
                            source = wb_by_date.get(day, {}).get(nm_id)
                            if source:
                                recognized_capital = _decimal(source["recognized_capital"])
                                paid_capital = _decimal(source["paid_capital"])
                                paid_equivalent = _decimal(source["paid_quantity"])
                                covered = _decimal(source["covered"])
                                confirmed_quantity = _decimal(source["confirmed"])
                                quality = str(source["quality"])
                                recognized_unit = _safe_ratio(recognized_capital, qty)
                                paid_unit = _safe_ratio(paid_capital, paid_equivalent)
                            else:
                                recognized_capital = paid_capital = confirmed_quantity = ZERO
                                paid_equivalent = covered = ZERO
                                quality = "coverage_gap"
                        under_qty = ZERO
                        under_rec_cap = ZERO
                        under_paid_cap = ZERO
                        if stage == STAGE_FF_TO_WB:
                            eligible = [
                                row for row in outstanding_rows
                                if int(row["nm_id"]) == nm_id
                                and str(row["accepted_date"] or row["writeoff_date"]) <= day
                            ]
                            historical_open: dict[str, Decimal] = {}
                            for row in eligible:
                                initial = max(
                                    _decimal(row["sent_quantity"]) - _decimal(row["accepted_quantity"]),
                                    ZERO,
                                )
                                provenance = _json_loads(row["provenance_json"])
                                closed = sum((
                                    _decimal(item.get("quantity"))
                                    for item in provenance.get("doprinato") or []
                                    if str(item.get("accepted_date") or "") <= day
                                ), ZERO)
                                historical_open[str(row["outstanding_layer_id"])] = max(initial - closed, ZERO)
                            under_qty = sum(historical_open.values(), ZERO)
                            under_rec_cap = sum((
                                historical_open[str(row["outstanding_layer_id"])]
                                * _decimal(row["cost_coverage_share"])
                                * _decimal(row["recognized_unit_cost_rub"])
                                for row in eligible
                            ), ZERO)
                            under_paid_cap = sum((
                                historical_open[str(row["outstanding_layer_id"])]
                                * _safe_ratio(
                                    _decimal(row["paid_equivalent_total_quantity"]),
                                    max(
                                        _decimal(row["sent_quantity"]) - _decimal(row["accepted_quantity"]),
                                        ZERO,
                                    ),
                                )
                                * _decimal(row["paid_unit_cost_rub"])
                                for row in eligible
                            ), ZERO)
                            # Exact layers are already included in physical FF->WB quantity.
                            if under_qty > qty:
                                raise CanonicalCostBlocked(
                                    "underaccepted_exceeds_ff_to_wb_stage",
                                    {"as_of_date": day, "nm_id": nm_id, "underaccepted": _text(under_qty), "stage": _text(qty)},
                                )
                            # The exact transit aggregate already contains underaccepted;
                            # submetrics are presentation-only and must never be re-added.
                        row_payload = {
                            "as_of_date": day, "nm_id": nm_id, "stage": stage,
                            "physical_quantity": _text(qty),
                            "paid_equivalent_quantity": _text(paid_equivalent),
                            "recognized_capital_rub": _text(recognized_capital),
                            "paid_capital_rub": _text(paid_capital),
                            "cost_covered_quantity": _text(covered),
                            "confirmed_quantity": _text(confirmed_quantity),
                            "recognized_unit_cost_rub": (
                                _text(_safe_ratio(recognized_capital, covered))
                                if covered > ZERO else None
                            ),
                            "paid_unit_cost_rub": (
                                _text(_safe_ratio(paid_capital, paid_equivalent))
                                if paid_equivalent > ZERO else None
                            ),
                            "underaccepted_quantity": _text(under_qty),
                            "underaccepted_recognized_capital_rub": _text(under_rec_cap),
                            "underaccepted_paid_capital_rub": _text(under_paid_cap),
                            "source_quality": quality,
                        }
                        fingerprint = _stable_hash(row_payload)
                        existing = conn.execute(
                            "SELECT fingerprint FROM sheet_vitrina_v1_canonical_cost_daily_state WHERE as_of_date=? AND nm_id=? AND stage=?",
                            (day, nm_id, stage),
                        ).fetchone()
                        if existing is not None and str(existing["fingerprint"]) == fingerprint:
                            continue
                        conn.execute(
                            """
                            INSERT INTO sheet_vitrina_v1_canonical_cost_daily_state(
                                as_of_date,nm_id,stage,physical_quantity,paid_equivalent_quantity,
                                recognized_capital_rub,paid_capital_rub,cost_covered_quantity,
                                confirmed_quantity,recognized_unit_cost_rub,paid_unit_cost_rub,
                                underaccepted_quantity,underaccepted_recognized_capital_rub,
                                underaccepted_paid_capital_rub,source_quality,diagnostics_json,
                                calculated_at,fingerprint
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(as_of_date,nm_id,stage) DO UPDATE SET
                                physical_quantity=excluded.physical_quantity,
                                paid_equivalent_quantity=excluded.paid_equivalent_quantity,
                                recognized_capital_rub=excluded.recognized_capital_rub,
                                paid_capital_rub=excluded.paid_capital_rub,
                                cost_covered_quantity=excluded.cost_covered_quantity,
                                confirmed_quantity=excluded.confirmed_quantity,
                                recognized_unit_cost_rub=excluded.recognized_unit_cost_rub,
                                paid_unit_cost_rub=excluded.paid_unit_cost_rub,
                                underaccepted_quantity=excluded.underaccepted_quantity,
                                underaccepted_recognized_capital_rub=excluded.underaccepted_recognized_capital_rub,
                                underaccepted_paid_capital_rub=excluded.underaccepted_paid_capital_rub,
                                source_quality=excluded.source_quality,
                                diagnostics_json=excluded.diagnostics_json,
                                calculated_at=excluded.calculated_at,fingerprint=excluded.fingerprint
                            """,
                            (
                                day,nm_id,stage,row_payload["physical_quantity"],row_payload["paid_equivalent_quantity"],
                                row_payload["recognized_capital_rub"],row_payload["paid_capital_rub"],
                                row_payload["cost_covered_quantity"],row_payload["confirmed_quantity"],
                                row_payload["recognized_unit_cost_rub"],row_payload["paid_unit_cost_rub"],
                                row_payload["underaccepted_quantity"],row_payload["underaccepted_recognized_capital_rub"],
                                row_payload["underaccepted_paid_capital_rub"],row_payload["source_quality"],
                                _json_dumps({"physical_source": _stage_source(stage)}),now,fingerprint,
                            ),
                        )
                        changed += 1
                        # Prevent one stage's local values leaking into the next stage.
                        del recognized_capital, paid_capital, confirmed_quantity
            conn.commit()
        return changed

    def _projection_fingerprint(self, start: str, end: str) -> str:
        with _connect(self.runtime.db_path) as conn:
            rows = conn.execute(
                """
                SELECT as_of_date,nm_id,stage,fingerprint
                FROM sheet_vitrina_v1_canonical_cost_daily_state
                WHERE as_of_date BETWEEN ? AND ? ORDER BY as_of_date,nm_id,stage
                """,
                (start, end),
            ).fetchall()
        return _stable_hash([list(row) for row in rows])


def ensure_canonical_cost_schema(conn: sqlite3.Connection) -> None:
    _execute_schema_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_canonical_cost_baseline_versions(
            baseline_id TEXT PRIMARY KEY, version INTEGER NOT NULL, cutover_date TEXT NOT NULL,
            primary_shipment_id TEXT NOT NULL, primary_accepted_ff_date TEXT NOT NULL,
            primary_quantity TEXT NOT NULL, primary_sku_count INTEGER NOT NULL,
            weighted_ff_unit_cost_rub TEXT NOT NULL, fallback_sku_count INTEGER NOT NULL,
            fingerprint TEXT NOT NULL UNIQUE, report_json TEXT NOT NULL, is_current INTEGER NOT NULL,
            created_at TEXT NOT NULL, superseded_at TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS canonical_cost_baseline_current
        ON sheet_vitrina_v1_canonical_cost_baseline_versions(is_current) WHERE is_current=1;
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_canonical_cost_baseline_lines(
            baseline_id TEXT NOT NULL REFERENCES sheet_vitrina_v1_canonical_cost_baseline_versions(baseline_id),
            nm_id INTEGER NOT NULL, stage TEXT NOT NULL, physical_quantity TEXT NOT NULL,
            paid_equivalent_quantity TEXT NOT NULL, recognized_unit_cost_rub TEXT NOT NULL,
            paid_unit_cost_rub TEXT NOT NULL, recognized_capital_rub TEXT NOT NULL,
            paid_capital_rub TEXT NOT NULL, cost_covered_quantity TEXT NOT NULL,
            confirmed_quantity TEXT NOT NULL, source_type TEXT NOT NULL,
            source_identity TEXT NOT NULL, source_date TEXT NOT NULL,
            provenance_json TEXT NOT NULL, line_fingerprint TEXT NOT NULL,
            PRIMARY KEY(baseline_id,nm_id,stage)
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_canonical_cost_components(
            component_id TEXT PRIMARY KEY, component_identity TEXT NOT NULL,
            component_type TEXT NOT NULL, shipment_id TEXT, supply_id TEXT, nm_id INTEGER NOT NULL,
            quantity TEXT NOT NULL, recognized_amount_rub TEXT NOT NULL, recognized_date TEXT NOT NULL,
            paid_amount_rub TEXT NOT NULL, paid_date TEXT,
            paid_equivalent_quantity TEXT NOT NULL, allocation_method TEXT NOT NULL,
            source_document_id TEXT, source_line_id TEXT, evidence_json TEXT NOT NULL,
            confirmation_status TEXT NOT NULL, fingerprint TEXT NOT NULL, version INTEGER NOT NULL,
            is_current INTEGER NOT NULL, supersedes_id TEXT, created_at TEXT NOT NULL, superseded_at TEXT,
            UNIQUE(component_identity,version)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS canonical_cost_components_current
        ON sheet_vitrina_v1_canonical_cost_components(component_identity) WHERE is_current=1;
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_canonical_cost_movement_layers(
            movement_layer_id TEXT PRIMARY KEY, movement_identity TEXT NOT NULL,
            operation_id TEXT NOT NULL, supply_id TEXT NOT NULL, nm_id INTEGER NOT NULL,
            effective_date TEXT NOT NULL, sent_quantity TEXT NOT NULL,
            paid_equivalent_quantity TEXT NOT NULL, cost_coverage_share TEXT NOT NULL,
            confirmation_share TEXT NOT NULL,
            recognized_unit_cost_rub TEXT, paid_unit_cost_rub TEXT,
            recognized_capital_rub TEXT NOT NULL, paid_capital_rub TEXT NOT NULL,
            ff_wac_quantity_before TEXT NOT NULL, source_operation_key TEXT NOT NULL,
            fingerprint TEXT NOT NULL, version INTEGER NOT NULL, is_current INTEGER NOT NULL,
            supersedes_id TEXT, created_at TEXT NOT NULL, superseded_at TEXT,
            UNIQUE(movement_identity,version)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS canonical_cost_movements_current
        ON sheet_vitrina_v1_canonical_cost_movement_layers(movement_identity) WHERE is_current=1;
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_canonical_cost_wb_outstanding_layers(
            outstanding_layer_id TEXT PRIMARY KEY, outstanding_identity TEXT NOT NULL,
            original_supply_id TEXT NOT NULL, nm_id INTEGER NOT NULL, warehouse TEXT NOT NULL,
            destination TEXT NOT NULL, original_movement_layer_id TEXT NOT NULL,
            sent_quantity TEXT NOT NULL, accepted_quantity TEXT NOT NULL, open_quantity TEXT NOT NULL,
            paid_equivalent_quantity TEXT NOT NULL, paid_equivalent_total_quantity TEXT NOT NULL,
            cost_coverage_share TEXT NOT NULL,
            confirmation_share TEXT NOT NULL,
            recognized_unit_cost_rub TEXT NOT NULL, paid_unit_cost_rub TEXT NOT NULL,
            writeoff_date TEXT NOT NULL, accepted_date TEXT, provenance_json TEXT NOT NULL,
            fingerprint TEXT NOT NULL, version INTEGER NOT NULL, is_current INTEGER NOT NULL,
            supersedes_id TEXT, created_at TEXT NOT NULL, superseded_at TEXT,
            UNIQUE(outstanding_identity,version)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS canonical_cost_outstanding_current
        ON sheet_vitrina_v1_canonical_cost_wb_outstanding_layers(outstanding_identity) WHERE is_current=1;
        CREATE INDEX IF NOT EXISTS canonical_cost_outstanding_fifo
        ON sheet_vitrina_v1_canonical_cost_wb_outstanding_layers(
            warehouse,destination,nm_id,accepted_date,original_supply_id
        ) WHERE is_current=1;
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_canonical_cost_daily_state(
            as_of_date TEXT NOT NULL, nm_id INTEGER NOT NULL, stage TEXT NOT NULL,
            physical_quantity TEXT NOT NULL, paid_equivalent_quantity TEXT NOT NULL,
            recognized_capital_rub TEXT NOT NULL, paid_capital_rub TEXT NOT NULL,
            cost_covered_quantity TEXT NOT NULL, confirmed_quantity TEXT NOT NULL,
            recognized_unit_cost_rub TEXT, paid_unit_cost_rub TEXT,
            underaccepted_quantity TEXT NOT NULL, underaccepted_recognized_capital_rub TEXT NOT NULL,
            underaccepted_paid_capital_rub TEXT NOT NULL, source_quality TEXT NOT NULL,
            diagnostics_json TEXT NOT NULL, calculated_at TEXT NOT NULL, fingerprint TEXT NOT NULL,
            PRIMARY KEY(as_of_date,nm_id,stage)
        );
        CREATE INDEX IF NOT EXISTS canonical_cost_daily_by_date_stage
        ON sheet_vitrina_v1_canonical_cost_daily_state(as_of_date,stage,nm_id);
        """
    )


def _execute_schema_script(conn: sqlite3.Connection, script: str) -> None:
    """Execute simple DDL without sqlite3.executescript's implicit COMMIT."""
    for statement in script.split(";"):
        sql = statement.strip()
        if sql:
            conn.execute(sql)


def allocate_partial_payment(
    product_lines: Iterable[Mapping[str, Any]], *, paid_share: Any, paid_rub: Any
) -> list[dict[str, Decimal | int]]:
    """Deterministically allocate a partial payment to every matched SKU line."""
    share = _decimal(paid_share)
    amount = _decimal(paid_rub)
    if share <= ZERO or share > ONE or amount <= ZERO:
        raise ValueError("paid_share must be in (0,1] and paid_rub must be positive")
    lines = [dict(item) for item in product_lines]
    if not lines:
        raise ValueError("product lines are required")
    values: list[tuple[int, Decimal, Decimal]] = []
    for item in lines:
        nm_id = int(item.get("nm_id") or item.get("internal_nm_id") or 0)
        qty = _decimal(item.get("qty"))
        invoice_value = _decimal(item.get("invoice_value") or item.get("amount"))
        if nm_id <= 0 or qty <= ZERO or invoice_value <= ZERO:
            raise ValueError("all payment allocation lines require matched nm_id, qty and invoice value")
        values.append((nm_id, qty, invoice_value))
    total_value = sum((item[2] for item in values), ZERO)
    remaining = amount
    result: list[dict[str, Decimal | int]] = []
    for index, (nm_id, qty, invoice_value) in enumerate(values):
        allocated = remaining if index == len(values) - 1 else amount * invoice_value / total_value
        remaining -= allocated
        result.append({
            "nm_id": nm_id,
            "physical_quantity": qty,
            "paid_equivalent_quantity": qty * share,
            "paid_capital_rub": allocated,
        })
    return result


def roll_wac(
    *, quantity: Any, capital: Any, receipt_quantity: Any = ZERO,
    receipt_unit_cost: Any = ZERO, writeoff_quantity: Any = ZERO,
) -> tuple[Decimal, Decimal, Decimal]:
    qty = _decimal(quantity)
    cap = _decimal(capital)
    receipt = _decimal(receipt_quantity)
    unit = _decimal(receipt_unit_cost)
    writeoff = _decimal(writeoff_quantity)
    if min(qty, cap, receipt, unit, writeoff) < ZERO:
        raise ValueError("WAC inputs cannot be negative")
    qty += receipt
    cap += receipt * unit
    if writeoff > qty:
        raise ValueError("writeoff exceeds WAC quantity")
    wac = _safe_ratio(cap, qty) if qty > ZERO else ZERO
    qty -= writeoff
    cap -= writeoff * wac
    return qty, cap, wac


def reconcile_outstanding_layers(
    layers: Iterable[Mapping[str, Any]],
    reconciliations: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Apply direct-identity/FIFO doprinato without inventing or mixing layers."""
    result = [
        {
            **dict(item),
            "provenance": dict(item.get("provenance") or {}),
            "open_quantity": _text(_decimal(item.get("open_quantity"))),
        }
        for item in layers
    ]
    seen: dict[str, str] = {}
    ordered = sorted(
        (dict(item) for item in reconciliations),
        key=lambda item: (str(item.get("accepted_date") or ""), str(item.get("supply_id") or ""), int(item.get("nm_id") or 0)),
    )
    for fact in ordered:
        supply_id = str(fact.get("supply_id") or "")
        identity = f"{supply_id}:{int(fact.get('nm_id') or 0)}"
        fingerprint = _stable_hash(fact)
        if identity in seen:
            if seen[identity] != fingerprint:
                raise CanonicalCostBlocked("doprinato_identity_conflict", {"supply_id": supply_id})
            continue
        seen[identity] = fingerprint
        remaining = _decimal(fact.get("accepted_quantity"))
        if remaining <= ZERO:
            raise CanonicalCostBlocked("doprinato_nonpositive_quantity", {"supply_id": supply_id})
        nm_id = int(fact.get("nm_id") or 0)
        accepted_date = str(fact.get("accepted_date") or "")
        original = str(fact.get("original_supply_id") or "")
        candidates = [
            item for item in result
            if int(item.get("nm_id") or 0) == nm_id
            and _decimal(item.get("open_quantity")) > ZERO
            and str(item.get("accepted_date") or item.get("writeoff_date") or "") <= accepted_date
            and (
                (original and str(item.get("original_supply_id") or "") == original)
                or (
                    not original
                    and str(item.get("warehouse") or "") == str(fact.get("warehouse") or "")
                    and str(item.get("destination") or "") == str(fact.get("destination") or "")
                )
            )
        ]
        candidates.sort(
            key=lambda item: (
                str(item.get("accepted_date") or item.get("writeoff_date") or ""),
                str(item.get("original_supply_id") or ""),
            )
        )
        for item in candidates:
            if remaining <= ZERO:
                break
            open_before = _decimal(item["open_quantity"])
            close = min(remaining, open_before)
            item["open_quantity"] = _text(open_before - close)
            if "paid_equivalent_quantity" in item:
                paid_before = _decimal(item.get("paid_equivalent_quantity"))
                item["paid_equivalent_quantity"] = _text(
                    max(paid_before - close * _safe_ratio(paid_before, open_before), ZERO)
                )
            item["provenance"].setdefault("doprinato", []).append(
                {
                    "supply_id": supply_id,
                    "quantity": _text(close),
                    "accepted_date": accepted_date,
                }
            )
            remaining -= close
        if remaining > ZERO:
            raise CanonicalCostBlocked(
                "doprinato_unmatched_surplus",
                {"supply_id": supply_id, "nm_id": nm_id, "surplus": _text(remaining)},
            )
    return result


def _ff_operation_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = [dict(row) for row in conn.execute(
        "SELECT * FROM sheet_vitrina_v1_ff_stock_operations"
    ).fetchall()]
    return sorted(
        rows,
        key=lambda row: (
            _ff_operation_effective_date(conn, row),
            str(row.get("created_at") or ""),
            str(row.get("operation_id") or ""),
        ),
    )


def _ff_operation_effective_date(conn: sqlite3.Connection, operation: Mapping[str, Any]) -> str:
    source_type = str(operation.get("source_type") or "")
    if source_type == "supplier_shipment":
        row = conn.execute(
            "SELECT actual_ff_acceptance_date FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?",
            (str(operation.get("source_object_id") or ""),),
        ).fetchone()
        if row and row[0]:
            return str(row[0])[:10]
    diagnostics = _json_loads(operation.get("diagnostics_json"))
    source_timestamp = str(diagnostics.get("source_timestamp") or "")
    if source_timestamp:
        return source_timestamp[:10]
    return str(operation.get("created_at") or "")[:10]


def _wb_movement_evidence(conn: sqlite3.Connection, *, as_of_date: str) -> list[dict[str, Any]]:
    movements: list[dict[str, Any]] = []
    for operation in _ff_operation_rows(conn):
        if str(operation.get("operation_type")) != "auto_writeoff":
            continue
        effective = _ff_operation_effective_date(conn, operation)
        if not effective or effective > as_of_date:
            continue
        supply_id = str(operation.get("source_object_id") or "")
        supply = conn.execute(
            "SELECT * FROM sheet_vitrina_v1_wb_supplies WHERE supply_id=? LIMIT 1",
            (supply_id,),
        ).fetchone()
        accepted_by_nm: dict[int, Decimal] = {}
        accepted_date = ""
        warehouse = ""
        destination = ""
        is_final_accepted = False
        if supply is not None:
            normalized = _json_loads(supply["normalized_row_json"])
            status_id = int(normalized.get("status_id") or normalized.get("statusID") or supply["status_id"] or 0)
            is_final_accepted = status_id == 5
            accepted_date = _wb_accepted_date(normalized, supply)
            warehouse = str(normalized.get("warehouse_name") or normalized.get("warehouseName") or supply["warehouse_id"] or "")
            destination = str(normalized.get("destination_name") or normalized.get("target_warehouse_name") or warehouse)
            if accepted_date and accepted_date <= as_of_date:
                for item in _goods(supply["raw_goods_json"]):
                    nm_id = int(item.get("nmID") or item.get("nmId") or item.get("nm_id") or 0)
                    qty = _decimal(item.get("acceptedQuantity") or item.get("accepted_quantity") or 0)
                    if nm_id > 0:
                        accepted_by_nm[nm_id] = qty
        for line in conn.execute(
            "SELECT nm_id,quantity_delta FROM sheet_vitrina_v1_ff_stock_operation_lines WHERE operation_id=?",
            (operation["operation_id"],),
        ).fetchall():
            nm_id = int(line["nm_id"])
            sent = abs(min(_decimal(line["quantity_delta"]), ZERO))
            accepted_quantity = accepted_by_nm.get(nm_id, ZERO)
            if accepted_quantity > sent:
                raise CanonicalCostBlocked(
                    "accepted_quantity_exceeds_sent",
                    {"supply_id": supply_id, "nm_id": nm_id},
                )
            movements.append({
                "supply_id": supply_id,
                "nm_id": nm_id,
                "sent_quantity": sent,
                "accepted_quantity": accepted_quantity,
                "open_quantity": sent - accepted_quantity,
                "accepted_date": accepted_date,
                "writeoff_date": effective,
                "warehouse": warehouse,
                "destination": destination,
                "is_final_accepted": is_final_accepted,
            })
    for fact in sorted(
        (item for item in _wb_supply_cache_evidence(conn, date_to=as_of_date) if item["is_doprinato"]),
        key=lambda item: (item["accepted_date"], item["supply_id"], item["nm_id"]),
    ):
        remaining = _decimal(fact["accepted_quantity"])
        candidates = [
            item for item in movements
            if item["nm_id"] == fact["nm_id"]
            and item["open_quantity"] > ZERO
            and item["is_final_accepted"]
            and (item["accepted_date"] or item["writeoff_date"]) <= fact["accepted_date"]
            and (
                (fact["original_supply_id"] and item["supply_id"] == fact["original_supply_id"])
                or (
                    not fact["original_supply_id"]
                    and item["warehouse"] == fact["warehouse"]
                    and item["destination"] == fact["destination"]
                )
            )
        ]
        candidates.sort(key=lambda item: (item["accepted_date"] or item["writeoff_date"], item["supply_id"]))
        for item in candidates:
            if remaining <= ZERO:
                break
            closed = min(remaining, item["open_quantity"])
            item["open_quantity"] -= closed
            remaining -= closed
        if remaining > ZERO:
            # The cutover baseline absorbs legacy history.  An orphan
            # doprinato absorbed by the opening snapshot cannot be safely
            # reconstructed and therefore stays source evidence only: it
            # creates neither a movement nor a zero-cost buffer.  New-contour
            # evidence remains strict and fail-closed.
            if str(fact["accepted_date"] or "") <= CUTOVER_DATE:
                continue
            raise CanonicalCostBlocked(
                "doprinato_unmatched_surplus",
                {
                    "supply_id": fact["supply_id"],
                    "nm_id": fact["nm_id"],
                    "accepted_date": fact["accepted_date"],
                    "surplus": _text(remaining),
                },
            )
    return movements


def _wb_supply_cache_evidence(conn: sqlite3.Connection, *, date_to: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM sheet_vitrina_v1_wb_supplies ORDER BY COALESCE(fact_date,supply_date,updated_date),supply_id"
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        normalized = _json_loads(row["normalized_row_json"])
        accepted_date = _wb_accepted_date(normalized, row)
        if not accepted_date or accepted_date > date_to:
            continue
        is_doprinato = int(normalized.get("virtual_type_id") or 0) == 5 or str(normalized.get("type_label") or "").strip() == "Допринято"
        status_id = int(normalized.get("status_id") or normalized.get("statusID") or row["status_id"] or 0)
        warehouse = str(normalized.get("warehouse_name") or normalized.get("warehouseName") or row["warehouse_id"] or "")
        destination = str(normalized.get("destination_name") or normalized.get("target_warehouse_name") or warehouse)
        original = str(normalized.get("original_supply_id") or normalized.get("originalSupplyID") or normalized.get("parent_supply_id") or "")
        for item in _goods(row["raw_goods_json"]):
            nm_id = int(item.get("nmID") or item.get("nmId") or item.get("nm_id") or 0)
            accepted = _decimal(item.get("acceptedQuantity") or item.get("accepted_quantity") or (item.get("quantity") if is_doprinato else 0))
            if nm_id <= 0 or accepted < ZERO:
                continue
            result.append({
                "supply_id": str(row["supply_id"]), "nm_id": nm_id,
                "accepted_quantity": accepted, "accepted_date": accepted_date,
                "warehouse": warehouse, "destination": destination,
                "original_supply_id": original, "is_doprinato": is_doprinato,
                "is_final_accepted": status_id == 5,
                "source_identity": str(row["cache_key"]),
            })
    return result


def _wb_accepted_date(normalized: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    for key in (
        "actual_acceptance_date", "actualAcceptanceDate", "acceptance_date",
        "acceptanceDate", "fact_date", "factDate", "closed_at", "closedAt",
    ):
        value = str(normalized.get(key) or "").strip()
        if len(value) >= 10:
            return value[:10]
    try:
        value = str(row["fact_date"] or "").strip()
    except (KeyError, IndexError, TypeError):
        value = ""
    return value[:10] if len(value) >= 10 else ""


def _goods(raw: Any) -> list[dict[str, Any]]:
    payload = _json_loads(raw)
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        rows = payload.get("goods") or payload.get("items") or payload.get("data") or []
        return [dict(item) for item in rows if isinstance(item, Mapping)] if isinstance(rows, list) else []
    return []


def _stage_source(stage: str) -> str:
    return {
        STAGE_PRODUCTION: "supplier_registry.production",
        STAGE_PRODUCTION_TO_FF: "supplier_registry.actual_shipment_without_ff_acceptance",
        STAGE_FF: "ff_stock_ledger",
        STAGE_FF_TO_WB: "ff_debit_plus_persisted_wb_acceptance",
        STAGE_WB: "official_wb_stock_ready_snapshot",
    }[stage]


def _json_safe_physical(value: Mapping[int, Mapping[str, Decimal]]) -> dict[str, dict[str, str]]:
    return {
        str(nm_id): {stage: _text(stages.get(stage, ZERO)) for stage in STAGES}
        for nm_id, stages in sorted(value.items())
    }


def _safe_ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    return numerator / denominator if denominator > ZERO else ZERO


def _decimal(value: Any) -> Decimal:
    if value in {None, ""}:
        return ZERO
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return ZERO


def _text(value: Decimal) -> str:
    normalized = value.quantize(Decimal("0.000001"))
    text = format(normalized, "f").rstrip("0").rstrip(".")
    return text or "0"


def _iso_date(value: Any) -> str:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"invalid ISO date: {value}") from exc


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(_json_dumps(value).encode("utf-8")).hexdigest()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _json_loads(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
