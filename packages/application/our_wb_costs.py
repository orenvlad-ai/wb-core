"""Management proxy WB cost layer materialization.

This module is intentionally separate from the existing 1C stock contour.  It
implements a management proxy model, not strict accounting FIFO/cost truth.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from packages.application.fulfillment_services import FulfillmentServicesBlock
from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
    _connect,
    _ensure_schema,
)
from packages.application.sheet_vitrina_v1_onec_stocks import ONEC_STOCKS_WB_UNIT_COST_RUB_METRIC_KEY
from packages.application.sheet_vitrina_v1_our_wb_costs import OUR_WB_COST_OPENING_DATE
from packages.contracts.supplier_shipments import ORDER_STATUS_ACCEPTED_FF


SUPPLIER_FF_STATUS_CONFIRMED = "confirmed"
SUPPLIER_FF_STATUS_ESTIMATED = "estimated"
SUPPLIER_FF_STATUS_PENDING_EXPENSES = "pending_expenses"
SUPPLIER_FF_STATUS_NEEDS_REVIEW = "needs_review"

TRANSIT_DIRECT_ZERO_CONFIRMED = "direct_zero_confirmed"
TRANSIT_CONFIRMED = "transit_confirmed"
TRANSIT_MISSING = "transit_missing"
TRANSIT_UNKNOWN_ROUTE = "unknown_route"

WB_COST_STATUS_CONFIRMED = "confirmed"
WB_COST_STATUS_ESTIMATED = "estimated"
WB_COST_STATUS_FALLBACK = "fallback"
WB_COST_STATUS_PENDING = "pending"
WB_COST_STATUS_NEEDS_REVIEW = "needs_review"
WB_SUPPLY_STATUS_ACCEPTED = 5

OPENING_SOURCE_CONFIRMED_SUPPLY = "opening_confirmed_supply"
OPENING_SOURCE_NEEDS_REVIEW = "needs_review"

SUPPLIER_FF_ALLOCATION_METHOD = "qty_based_common_pool"


@dataclass(frozen=True)
class TransitCostClassification:
    status: str
    amount_total: float | None
    per_unit: float | None
    evidence: str
    missing_reason: str | None = None


@dataclass(frozen=True)
class OurWbCostRebuildResult:
    supplier_layers_materialized: int
    wb_supply_layers_materialized: int
    opening_rows_materialized: int
    daily_state_rows_materialized: int


class OurWbCostBlock:
    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        timestamp_factory: Callable[[], str] | None = None,
    ) -> None:
        self.runtime = runtime
        self.timestamp_factory = timestamp_factory or _default_timestamp_factory

    def has_current_supplier_ff_cost_layer(self, shipment_id: str) -> bool:
        shipment_id = str(shipment_id or "").strip()
        if not shipment_id:
            return False
        with _connect(self.runtime.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT 1
                FROM sheet_vitrina_v1_supplier_ff_cost_layers
                WHERE supplier_shipment_id = ? AND is_current = 1
                LIMIT 1
                """,
                (shipment_id,),
            ).fetchone()
            return row is not None

    def materialize_existing_accepted_ff_shipments(self) -> int:
        count = 0
        for shipment in self.runtime.list_supplier_shipments():
            if str(shipment.get("order_status") or "") != ORDER_STATUS_ACCEPTED_FF:
                continue
            result = self.materialize_supplier_ff_cost_layer(str(shipment.get("shipment_id") or ""))
            if result is not None and bool(result.get("materialized")):
                count += 1
        return count

    def materialize_supplier_ff_cost_layer(self, shipment_id: str) -> dict[str, Any] | None:
        shipment_id = str(shipment_id or "").strip()
        if not shipment_id:
            raise ValueError("supplier_shipment_id is required")
        detail = self.runtime.load_supplier_shipment(shipment_id)
        if detail is None:
            raise ValueError(f"supplier shipment not found: {shipment_id}")
        header = dict(detail.get("header") or {})
        lines = [dict(item or {}) for item in (detail.get("lines") or [])]
        financial_documents = self.runtime.list_supplier_financial_documents(shipment_id)
        expense_lines = self.runtime.list_supplier_financial_expense_lines(shipment_id)
        calculation = self._calculate_supplier_ff_layer(
            header=header,
            lines=lines,
            financial_documents=financial_documents,
            expense_lines=expense_lines,
        )
        now = self.timestamp_factory()
        inputs_hash = _stable_hash(
            {
                "schema": "supplier_ff_cost_layer_v1",
                "header": _selected_supplier_header_inputs(header),
                "lines": [_selected_supplier_line_inputs(line) for line in lines],
                "financial_documents": [_selected_financial_document_inputs(item) for item in financial_documents],
                "expense_lines": [_selected_expense_line_inputs(item) for item in expense_lines],
            }
        )
        with _connect(self.runtime.db_path) as conn:
            _ensure_schema(conn)
            existing = conn.execute(
                """
                SELECT layer_id, inputs_hash, version
                FROM sheet_vitrina_v1_supplier_ff_cost_layers
                WHERE supplier_shipment_id = ? AND is_current = 1
                ORDER BY version DESC
                LIMIT 1
                """,
                (shipment_id,),
            ).fetchone()
            if existing is not None and str(existing["inputs_hash"]) == inputs_hash:
                return {
                    "layer_id": str(existing["layer_id"]),
                    "materialized": False,
                    "status": calculation["status"],
                    "inputs_hash": inputs_hash,
                }
            version_row = conn.execute(
                """
                SELECT COALESCE(MAX(version), 0) AS max_version
                FROM sheet_vitrina_v1_supplier_ff_cost_layers
                WHERE supplier_shipment_id = ?
                """,
                (shipment_id,),
            ).fetchone()
            version = int(version_row["max_version"] or 0) + 1
            layer_id = f"ffcost_{shipment_id}_{version}"
            supersedes_layer_id = str(existing["layer_id"]) if existing is not None else None
            if supersedes_layer_id:
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_supplier_ff_cost_layers
                    SET is_current = 0,
                        superseded_at = ?
                    WHERE layer_id = ?
                    """,
                    (now, supersedes_layer_id),
                )
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_supplier_ff_cost_layers (
                    layer_id,
                    supplier_shipment_id,
                    status,
                    accepted_ff_date,
                    calculated_at,
                    effective_cny_rate,
                    invoice_amount_total_cny,
                    invoice_extras_total_cny,
                    product_qty_total,
                    common_expense_pool_rub,
                    common_expense_per_unit_rub,
                    weighted_avg_ff_unit_cost_rub,
                    reconciliation_status,
                    reconciliation_delta_rub,
                    inputs_hash,
                    version,
                    is_current,
                    supersedes_layer_id,
                    superseded_at,
                    source_status_json,
                    component_status_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, NULL, ?, ?)
                """,
                (
                    layer_id,
                    shipment_id,
                    calculation["status"],
                    _optional_text(header.get("actual_ff_acceptance_date")),
                    now,
                    calculation["effective_cny_rate"],
                    calculation["invoice_amount_total_cny"],
                    calculation["invoice_extras_total_cny"],
                    calculation["product_qty_total"],
                    calculation["common_expense_pool_rub"],
                    calculation["common_expense_per_unit_rub"],
                    calculation["weighted_avg_ff_unit_cost_rub"],
                    calculation["reconciliation_status"],
                    calculation["reconciliation_delta_rub"],
                    inputs_hash,
                    version,
                    supersedes_layer_id,
                    _json_dumps(calculation["source_status"]),
                    _json_dumps(calculation["component_status"]),
                ),
            )
            for index, line in enumerate(calculation["lines"], start=1):
                supplier_line_id = line["supplier_line_id"] or f"missing_line_id_{index}"
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_supplier_ff_cost_layer_lines (
                        layer_line_id,
                        layer_id,
                        supplier_shipment_id,
                        supplier_line_id,
                        nm_id,
                        sku,
                        display_name,
                        qty,
                        invoice_unit_price_cny,
                        sku_purchase_cost_rub,
                        allocated_common_expenses_per_unit_rub,
                        sku_ff_unit_cost_rub,
                        line_total_cost_rub,
                        allocation_method,
                        source_status,
                        missing_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"ffcost_line_{layer_id}_{supplier_line_id}",
                        layer_id,
                        shipment_id,
                        supplier_line_id,
                        line["nm_id"],
                        line["sku"],
                        line["display_name"],
                        line["qty"],
                        line["invoice_unit_price_cny"],
                        line["sku_purchase_cost_rub"],
                        line["allocated_common_expenses_per_unit_rub"],
                        line["sku_ff_unit_cost_rub"],
                        line["line_total_cost_rub"],
                        SUPPLIER_FF_ALLOCATION_METHOD,
                        line["source_status"],
                        line["missing_reason"],
                    ),
                )
            return {
                "layer_id": layer_id,
                "materialized": True,
                "status": calculation["status"],
                "inputs_hash": inputs_hash,
            }

    def rebuild_all(self, *, opening_date: str = OUR_WB_COST_OPENING_DATE) -> OurWbCostRebuildResult:
        supplier_count = self.materialize_existing_accepted_ff_shipments()
        # After the guarded baseline apply, module 40 is a compatibility facade:
        # one canonical engine owns baseline, WB rolling state and capital views.
        from packages.application.canonical_cost_engine import CanonicalCostEngine

        canonical = None
        with _connect(self.runtime.db_path) as conn:
            table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sheet_vitrina_v1_canonical_cost_baseline_versions'"
            ).fetchone()
            baseline_active = bool(
                table_exists
                and conn.execute(
                    "SELECT 1 FROM sheet_vitrina_v1_canonical_cost_baseline_versions WHERE is_current=1"
                ).fetchone()
            )
        if baseline_active:
            canonical = CanonicalCostEngine(runtime=self.runtime, timestamp_factory=self.timestamp_factory)
            result = canonical.rebuild(date_from=opening_date)
            return OurWbCostRebuildResult(
                supplier_layers_materialized=supplier_count,
                wb_supply_layers_materialized=result.movement_rows_changed,
                opening_rows_materialized=0,
                daily_state_rows_materialized=result.daily_rows_changed,
            )
        with _connect(self.runtime.db_path) as conn:
            legacy_baseline_exists = conn.execute(
                "SELECT 1 FROM sheet_vitrina_v1_wb_opening_baseline WHERE as_of_date=? LIMIT 1",
                (opening_date,),
            ).fetchone() is not None
        if legacy_baseline_exists:
            # Freeze the already deployed legacy read-side until the separately
            # approved canonical baseline is applied.  No new forbidden fallback
            # may be selected in this transition window.
            return OurWbCostRebuildResult(
                supplier_layers_materialized=supplier_count,
                wb_supply_layers_materialized=0,
                opening_rows_materialized=0,
                daily_state_rows_materialized=0,
            )
        wb_supply_count = self.materialize_wb_supply_cost_layers(opening_date=opening_date)
        opening_count = self.materialize_opening_baseline(opening_date=opening_date)
        daily_count = self.materialize_daily_state(opening_date=opening_date)
        return OurWbCostRebuildResult(
            supplier_layers_materialized=supplier_count,
            wb_supply_layers_materialized=wb_supply_count,
            opening_rows_materialized=opening_count,
            daily_state_rows_materialized=daily_count,
        )

    def materialize_wb_supply_cost_layers(self, *, opening_date: str = OUR_WB_COST_OPENING_DATE) -> int:
        ff_overlay_block = FulfillmentServicesBlock(runtime=self.runtime, timestamp_factory=self.timestamp_factory)
        ff_overlays = ff_overlay_block.approved_overlay_by_supply()
        current_ff_lines = self._load_current_supplier_ff_cost_lines_by_nm()
        count = 0
        now = self.timestamp_factory()
        with _connect(self.runtime.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_wb_supplies
                WHERE COALESCE(supply_date, fact_date, updated_date, '') >= ?
                ORDER BY COALESCE(supply_date, fact_date, updated_date, '') ASC, supply_id ASC
                """,
                (opening_date,),
            ).fetchall()
            for row in rows:
                supply = _wb_supply_row_to_dict(row)
                goods = _parse_wb_goods(supply.get("raw_goods_json"))
                if not goods:
                    continue
                supply_id = str(supply.get("supply_id") or "")
                overlay = ff_overlays.get(supply_id)
                supply_qty = _sum_positive(_wb_good_quantity(item).qty for item in goods)
                denominator = _positive_number(supply.get("quantity_for_size_filter")) or supply_qty
                if denominator <= 0:
                    continue
                transit = classify_wb_supply_transit(
                    _normalized_wb_row(supply),
                    denominator=denominator,
                )
                services_total = _number_or_zero(
                    (overlay or {}).get("service_amount_with_vat_without_storage_total")
                )
                storage_total = _number_or_zero(
                    (overlay or {}).get("storage_allocated_amount_with_vat_total")
                )
                services_per_unit = services_total / denominator if denominator > 0 else 0.0
                storage_per_unit = storage_total / denominator if denominator > 0 else 0.0
                for good in goods:
                    nm_id = _optional_int(good.get("nmID") or good.get("nmId") or good.get("nm_id"))
                    quantity = _wb_good_quantity(good)
                    qty = _positive_number(quantity.qty)
                    if nm_id is None or qty <= 0:
                        continue
                    ff_line = current_ff_lines.get(nm_id)
                    layer_payload = self._build_wb_supply_cost_layer_payload(
                        supply=supply,
                        nm_id=nm_id,
                        accepted_qty=qty,
                        quantity_source=quantity.source,
                        quantity_is_final_accepted=_wb_good_quantity_is_final_accepted(
                            supply=supply,
                            quantity=quantity,
                        ),
                        denominator=denominator,
                        ff_line=ff_line,
                        transit=transit,
                        services_total=services_total,
                        services_per_unit=services_per_unit,
                        storage_total=storage_total,
                        storage_per_unit=storage_per_unit,
                        overlay=overlay,
                    )
                    inputs_hash = _stable_hash(layer_payload["input"])
                    if self._upsert_wb_supply_cost_layer(
                        conn=conn,
                        now=now,
                        payload=layer_payload,
                        inputs_hash=inputs_hash,
                    ):
                        count += 1
        return count

    def materialize_opening_baseline(self, *, opening_date: str = OUR_WB_COST_OPENING_DATE) -> int:
        if opening_date not in set(
            self.runtime.list_sheet_vitrina_ready_snapshot_dates_any_bundle(
                date_from=opening_date,
                date_to=opening_date,
                descending=False,
            )
        ):
            return 0
        current_ff_lines_by_nm = self._load_current_supplier_ff_cost_lines_grouped_by_nm()
        opening_stock = self._load_snapshot_sku_metric(opening_date, "stock_total")
        metric11 = self._load_snapshot_sku_metric(opening_date, ONEC_STOCKS_WB_UNIT_COST_RUB_METRIC_KEY)
        active_names = self._load_active_sku_names()
        now = self.timestamp_factory()
        count = 0
        with _connect(self.runtime.db_path) as conn:
            _ensure_schema(conn)
            for nm_id, display_name in active_names.items():
                stock_qty = _number_or_zero(opening_stock.get(nm_id))
                ff_line = _select_opening_ff_line_for_baseline(
                    current_ff_lines_by_nm.get(nm_id, []),
                    opening_date=opening_date,
                )
                component_estimate = 0.0
                baseline = self._select_opening_baseline(
                    nm_id=nm_id,
                    display_name=display_name,
                    opening_stock_qty=stock_qty,
                    ff_line=ff_line,
                    component_estimate=component_estimate,
                    metric11_value=metric11.get(nm_id),
                    opening_date=opening_date,
                )
                inputs_hash = _stable_hash({"schema": "opening_baseline_v1", **baseline})
                existing = conn.execute(
                    """
                    SELECT inputs_hash
                    FROM sheet_vitrina_v1_wb_opening_baseline
                    WHERE as_of_date = ? AND nm_id = ?
                    """,
                    (opening_date, nm_id),
                ).fetchone()
                if existing is not None and str(existing["inputs_hash"]) == inputs_hash:
                    continue
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_wb_opening_baseline (
                        as_of_date,
                        nm_id,
                        display_name,
                        opening_stock_qty,
                        opening_unit_cost_rub,
                        source_priority,
                        source_status,
                        supplier_ff_cost_layer_id,
                        supplier_ff_cost_layer_line_id,
                        metric11_value,
                        confirmed_qty,
                        estimated_qty,
                        fallback_qty,
                        missing_reason,
                        component_status_json,
                        calculated_at,
                        inputs_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(as_of_date, nm_id) DO UPDATE SET
                        display_name = excluded.display_name,
                        opening_stock_qty = excluded.opening_stock_qty,
                        opening_unit_cost_rub = excluded.opening_unit_cost_rub,
                        source_priority = excluded.source_priority,
                        source_status = excluded.source_status,
                        supplier_ff_cost_layer_id = excluded.supplier_ff_cost_layer_id,
                        supplier_ff_cost_layer_line_id = excluded.supplier_ff_cost_layer_line_id,
                        metric11_value = excluded.metric11_value,
                        confirmed_qty = excluded.confirmed_qty,
                        estimated_qty = excluded.estimated_qty,
                        fallback_qty = excluded.fallback_qty,
                        missing_reason = excluded.missing_reason,
                        component_status_json = excluded.component_status_json,
                        calculated_at = excluded.calculated_at,
                        inputs_hash = excluded.inputs_hash
                    """,
                    (
                        opening_date,
                        nm_id,
                        display_name,
                        stock_qty,
                        baseline["opening_unit_cost_rub"],
                        baseline["source_priority"],
                        baseline["source_status"],
                        baseline["supplier_ff_cost_layer_id"],
                        baseline["supplier_ff_cost_layer_line_id"],
                        baseline["metric11_value"],
                        baseline["confirmed_qty"],
                        baseline["estimated_qty"],
                        baseline["fallback_qty"],
                        baseline["missing_reason"],
                        _json_dumps(baseline["component_status"]),
                        now,
                        inputs_hash,
                    ),
                )
                count += 1
        return count

    def materialize_daily_state(self, *, opening_date: str = OUR_WB_COST_OPENING_DATE) -> int:
        snapshot_dates = self.runtime.list_sheet_vitrina_ready_snapshot_dates_any_bundle(
            date_from=opening_date,
            descending=False,
        )
        if not snapshot_dates:
            return 0
        stock_by_date = self._load_stock_metrics_by_date_column(
            snapshot_dates=snapshot_dates,
            opening_date=opening_date,
        )
        dates = sorted(stock_by_date)
        if not dates:
            return 0
        openings = self._load_opening_baseline(opening_date)
        wb_inbounds_by_date = self._load_wb_supply_cost_layers_by_date(opening_date=opening_date)
        previous_by_nm: dict[int, dict[str, float | str | None]] = {}
        count = 0
        now = self.timestamp_factory()
        with _connect(self.runtime.db_path) as conn:
            _ensure_schema(conn)
            for snapshot_date in dates:
                stock_by_nm = stock_by_date.get(snapshot_date, {})
                current_by_nm: dict[int, dict[str, float | str | None]] = {}
                nm_ids = set(stock_by_nm) | set(openings) | set(previous_by_nm)
                for nm_id in sorted(nm_ids):
                    stock_qty = _number_or_zero(stock_by_nm.get(nm_id))
                    if snapshot_date == opening_date or nm_id not in previous_by_nm:
                        opening = openings.get(nm_id)
                        if opening is None:
                            state = _empty_daily_state(stock_qty)
                        else:
                            state = {
                                "stock_qty": stock_qty,
                                "our_wb_unit_cost_rub": _optional_float(opening.get("opening_unit_cost_rub")),
                                "confirmed_qty": min(stock_qty, _number_or_zero(opening.get("confirmed_qty"))),
                                "estimated_qty": min(stock_qty, _number_or_zero(opening.get("estimated_qty"))),
                                "fallback_qty": min(stock_qty, _number_or_zero(opening.get("fallback_qty"))),
                                "source_status": str(opening.get("source_status") or ""),
                                "component_status": _json_loads(opening.get("component_status_json")),
                            }
                    else:
                        state = _roll_daily_state(
                            previous=previous_by_nm[nm_id],
                            stock_qty=stock_qty,
                            inbounds=wb_inbounds_by_date.get(snapshot_date, {}).get(nm_id, []),
                        )
                    confirmed_share = (
                        _number_or_zero(state.get("confirmed_qty")) / stock_qty if stock_qty > 0 else None
                    )
                    current_by_nm[nm_id] = state
                    inputs_hash = _stable_hash(
                        {
                            "schema": "wb_cost_daily_state_v1",
                            "as_of_date": snapshot_date,
                            "nm_id": nm_id,
                            "state": state,
                            "confirmed_share_pct": confirmed_share,
                        }
                    )
                    existing = conn.execute(
                        """
                        SELECT inputs_hash
                        FROM sheet_vitrina_v1_wb_cost_daily_state
                        WHERE as_of_date = ? AND nm_id = ?
                        """,
                        (snapshot_date, nm_id),
                    ).fetchone()
                    if existing is not None and str(existing["inputs_hash"]) == inputs_hash:
                        continue
                    conn.execute(
                        """
                        INSERT INTO sheet_vitrina_v1_wb_cost_daily_state (
                            as_of_date,
                            nm_id,
                            stock_qty,
                            our_wb_unit_cost_rub,
                            confirmed_qty,
                            estimated_qty,
                            fallback_qty,
                            confirmed_share_pct,
                            source_status,
                            component_status_json,
                            calculated_at,
                            inputs_hash
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(as_of_date, nm_id) DO UPDATE SET
                            stock_qty = excluded.stock_qty,
                            our_wb_unit_cost_rub = excluded.our_wb_unit_cost_rub,
                            confirmed_qty = excluded.confirmed_qty,
                            estimated_qty = excluded.estimated_qty,
                            fallback_qty = excluded.fallback_qty,
                            confirmed_share_pct = excluded.confirmed_share_pct,
                            source_status = excluded.source_status,
                            component_status_json = excluded.component_status_json,
                            calculated_at = excluded.calculated_at,
                            inputs_hash = excluded.inputs_hash
                        """,
                        (
                            snapshot_date,
                            nm_id,
                            stock_qty,
                            state.get("our_wb_unit_cost_rub"),
                            state.get("confirmed_qty"),
                            state.get("estimated_qty"),
                            state.get("fallback_qty"),
                            confirmed_share,
                            state.get("source_status"),
                            _json_dumps(state.get("component_status") or {}),
                            now,
                            inputs_hash,
                        ),
                    )
                    count += 1
                previous_by_nm = current_by_nm
        return count

    def status(self, *, opening_date: str = OUR_WB_COST_OPENING_DATE) -> dict[str, Any]:
        from packages.application.canonical_cost_engine import CanonicalCostEngine

        with _connect(self.runtime.db_path) as conn:
            table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sheet_vitrina_v1_canonical_cost_baseline_versions'"
            ).fetchone()
            baseline_active = bool(
                table_exists
                and conn.execute(
                    "SELECT 1 FROM sheet_vitrina_v1_canonical_cost_baseline_versions WHERE is_current=1"
                ).fetchone()
            )
        if baseline_active:
            return CanonicalCostEngine(
                runtime=self.runtime, timestamp_factory=self.timestamp_factory
            ).status()
        with _connect(self.runtime.db_path) as conn:
            _ensure_schema(conn)
            supplier_layers = conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM sheet_vitrina_v1_supplier_ff_cost_layers
                WHERE is_current = 1
                GROUP BY status
                """
            ).fetchall()
            transit_layers = conn.execute(
                """
                SELECT transit_cost_status, COUNT(*) AS supply_sku_lines, SUM(accepted_qty) AS qty
                FROM sheet_vitrina_v1_wb_supply_cost_layers
                WHERE is_current = 1
                GROUP BY transit_cost_status
                """
            ).fetchall()
            opening_rows = conn.execute(
                """
                SELECT source_status, COUNT(*) AS sku_count, SUM(opening_stock_qty) AS stock_qty
                FROM sheet_vitrina_v1_wb_opening_baseline
                WHERE as_of_date = ?
                GROUP BY source_status
                """,
                (opening_date,),
            ).fetchall()
            latest_daily = conn.execute(
                """
                SELECT as_of_date,
                       SUM(stock_qty) AS stock_qty,
                       SUM(our_wb_unit_cost_rub * stock_qty) AS weighted_cost_sum,
                       SUM(confirmed_qty) AS confirmed_qty
                FROM sheet_vitrina_v1_wb_cost_daily_state
                GROUP BY as_of_date
                ORDER BY as_of_date DESC
                LIMIT 1
                """
            ).fetchone()
        total_cost = None
        total_share = None
        if latest_daily is not None:
            stock_qty = _number_or_zero(latest_daily["stock_qty"])
            if stock_qty > 0:
                total_cost = _number_or_zero(latest_daily["weighted_cost_sum"]) / stock_qty
                total_share = _number_or_zero(latest_daily["confirmed_qty"]) / stock_qty
        return {
            "opening_date": opening_date,
            "supplier_layers": {str(row["status"]): int(row["count"] or 0) for row in supplier_layers},
            "transit_layers": {
                str(row["transit_cost_status"]): {
                    "supply_sku_lines": int(row["supply_sku_lines"] or 0),
                    "qty": _number_or_zero(row["qty"]),
                }
                for row in transit_layers
            },
            "opening_baseline": {
                str(row["source_status"]): {
                    "sku_count": int(row["sku_count"] or 0),
                    "stock_qty": _number_or_zero(row["stock_qty"]),
                }
                for row in opening_rows
            },
            "latest_daily_state": None
            if latest_daily is None
            else {
                "as_of_date": str(latest_daily["as_of_date"]),
                "stock_qty": _number_or_zero(latest_daily["stock_qty"]),
                "total_our_wb_unit_cost_rub": total_cost,
                "total_confirmed_share_pct": total_share,
            },
        }

    def _calculate_supplier_ff_layer(
        self,
        *,
        header: Mapping[str, Any],
        lines: Iterable[Mapping[str, Any]],
        financial_documents: Iterable[Mapping[str, Any]],
        expense_lines: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        product_lines = [dict(line) for line in lines if str(line.get("line_type") or "") == "product"]
        product_qty_total = _positive_number(header.get("product_qty_total"))
        if product_qty_total <= 0:
            product_qty_total = _sum_positive(_positive_number(line.get("qty")) for line in product_lines)
        invoice_product_amount_cny = _positive_number(header.get("product_amount_total"))
        invoice_extras_cny = _number_or_zero(header.get("extras_amount_total"))
        invoice_total_cny = _positive_number(header.get("invoice_amount_total"))
        if invoice_total_cny <= 0:
            invoice_total_cny = invoice_product_amount_cny + invoice_extras_cny
        cny_payment_rub = _positive_number(header.get("cny_payment_currency_rub_cost"))
        approx_rate = _positive_number(header.get("approx_yuan_rate"))
        if cny_payment_rub > 0 and invoice_total_cny > 0:
            effective_cny_rate = cny_payment_rub / invoice_total_cny
            fx_status = "confirmed"
        elif approx_rate > 0:
            effective_cny_rate = approx_rate
            fx_status = "estimated"
        else:
            effective_cny_rate = None
            fx_status = "missing"
        bank_fee_rub = _number_or_zero(header.get("cny_bank_fee_rub"))
        logistics_invoice_rub = 0.0
        customs_declaration_rub = 0.0
        document_types_by_id = {
            str(item.get("document_id") or ""): str(item.get("document_type") or "")
            for item in financial_documents
        }
        for line in expense_lines:
            document_type = document_types_by_id.get(str(line.get("financial_document_id") or ""))
            amount_rub = _number_or_zero(line.get("amount_rub"))
            if document_type == "logistics_invoice":
                logistics_invoice_rub += amount_rub
            elif document_type == "customs_declaration" and bool(int(line.get("included_in_customs_total") or 0)):
                customs_declaration_rub += amount_rub
        missing_reasons: list[str] = []
        if not product_lines:
            missing_reasons.append("missing_product_lines")
        if product_qty_total <= 0:
            missing_reasons.append("missing_product_qty")
        if effective_cny_rate is None:
            missing_reasons.append("missing_effective_cny_rate")
        valid_lines: list[dict[str, Any]] = []
        common_expense_pool_rub = None
        common_per_unit = None
        if effective_cny_rate is not None and product_qty_total > 0:
            common_expense_pool_rub = (
                invoice_extras_cny * effective_cny_rate
                + bank_fee_rub
                + logistics_invoice_rub
                + customs_declaration_rub
            )
            common_per_unit = common_expense_pool_rub / product_qty_total
        for line in product_lines:
            line_id = str(line.get("line_id") or "")
            qty = _positive_number(line.get("qty"))
            unit_price_cny = _positive_number(line.get("unit_price"))
            nm_id = _optional_int(line.get("internal_nm_id"))
            line_missing: list[str] = []
            if not line_id:
                line_missing.append("missing_supplier_line_id")
            if nm_id is None:
                line_missing.append("missing_nm_id")
            if qty <= 0:
                line_missing.append("missing_qty")
            if unit_price_cny <= 0:
                line_missing.append("missing_invoice_unit_price_cny")
            if effective_cny_rate is None or common_per_unit is None:
                line_missing.append("missing_cost_inputs")
            sku_purchase_cost_rub = None
            sku_ff_unit_cost_rub = None
            line_total_cost_rub = None
            if not line_missing:
                sku_purchase_cost_rub = unit_price_cny * float(effective_cny_rate)
                sku_ff_unit_cost_rub = sku_purchase_cost_rub + float(common_per_unit)
                line_total_cost_rub = sku_ff_unit_cost_rub * qty
            if line_missing:
                missing_reasons.extend(line_missing)
            valid_lines.append(
                {
                    "supplier_line_id": line_id,
                    "nm_id": nm_id,
                    "sku": _optional_text(line.get("internal_sku")),
                    "display_name": _optional_text(line.get("internal_name") or line.get("model_raw")),
                    "qty": qty,
                    "invoice_unit_price_cny": unit_price_cny,
                    "sku_purchase_cost_rub": sku_purchase_cost_rub,
                    "allocated_common_expenses_per_unit_rub": common_per_unit,
                    "sku_ff_unit_cost_rub": sku_ff_unit_cost_rub,
                    "line_total_cost_rub": line_total_cost_rub,
                    "source_status": SUPPLIER_FF_STATUS_NEEDS_REVIEW if line_missing else SUPPLIER_FF_STATUS_CONFIRMED,
                    "missing_reason": ",".join(sorted(set(line_missing))) if line_missing else None,
                }
            )
        weighted_avg = _weighted_avg(
            (
                (_optional_float(line.get("sku_ff_unit_cost_rub")), _number_or_zero(line.get("qty")))
                for line in valid_lines
            )
        )
        expected_avg = None
        if effective_cny_rate is not None and product_qty_total > 0:
            expected_avg = (
                invoice_total_cny * effective_cny_rate
                + bank_fee_rub
                + logistics_invoice_rub
                + customs_declaration_rub
            ) / product_qty_total
        reconciliation_delta = None
        reconciliation_status = "not_available"
        if weighted_avg is not None and expected_avg is not None:
            reconciliation_delta = weighted_avg - expected_avg
            reconciliation_status = "ok" if abs(reconciliation_delta) <= 0.000001 else "mismatch"
        has_factual_docs = logistics_invoice_rub > 0 or customs_declaration_rub > 0 or bank_fee_rub > 0
        if missing_reasons:
            status = SUPPLIER_FF_STATUS_NEEDS_REVIEW
        elif fx_status == "estimated":
            status = SUPPLIER_FF_STATUS_ESTIMATED
        elif not has_factual_docs:
            status = SUPPLIER_FF_STATUS_PENDING_EXPENSES
        else:
            status = SUPPLIER_FF_STATUS_CONFIRMED
        source_status = {
            "fx_status": fx_status,
            "allocation_method": SUPPLIER_FF_ALLOCATION_METHOD,
            "missing_reasons": sorted(set(missing_reasons)),
        }
        component_status = {
            "invoice_lines": "confirmed" if product_lines and not missing_reasons else "needs_review",
            "cny_rate": fx_status,
            "invoice_extras": "confirmed" if invoice_extras_cny >= 0 else "needs_review",
            "bank_fee_rub": "confirmed" if bank_fee_rub > 0 else "absent_or_zero",
            "logistics_invoice_rub": "confirmed" if logistics_invoice_rub > 0 else "absent_or_pending",
            "customs_declaration_rub": "confirmed" if customs_declaration_rub > 0 else "absent_or_pending",
        }
        return {
            "status": status,
            "effective_cny_rate": effective_cny_rate,
            "invoice_amount_total_cny": invoice_total_cny,
            "invoice_extras_total_cny": invoice_extras_cny,
            "product_qty_total": product_qty_total,
            "common_expense_pool_rub": common_expense_pool_rub,
            "common_expense_per_unit_rub": common_per_unit,
            "weighted_avg_ff_unit_cost_rub": weighted_avg,
            "reconciliation_status": reconciliation_status,
            "reconciliation_delta_rub": reconciliation_delta,
            "lines": valid_lines,
            "source_status": source_status,
            "component_status": component_status,
        }

    def _load_current_supplier_ff_cost_lines_by_nm(self) -> dict[int, dict[str, Any]]:
        grouped = self._load_current_supplier_ff_cost_lines_grouped_by_nm()
        return {nm_id: lines[0] for nm_id, lines in grouped.items() if lines}

    def _load_current_supplier_ff_cost_lines_grouped_by_nm(self) -> dict[int, list[dict[str, Any]]]:
        with _connect(self.runtime.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT line.*,
                       layer.status AS layer_status,
                       layer.accepted_ff_date,
                       layer.weighted_avg_ff_unit_cost_rub,
                       layer.component_status_json AS layer_component_status_json
                FROM sheet_vitrina_v1_supplier_ff_cost_layer_lines AS line
                JOIN sheet_vitrina_v1_supplier_ff_cost_layers AS layer
                  ON layer.layer_id = line.layer_id
                WHERE layer.is_current = 1
                  AND line.nm_id IS NOT NULL
                ORDER BY layer.accepted_ff_date DESC, layer.calculated_at DESC
                """
            ).fetchall()
        by_nm: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            nm_id = _optional_int(row["nm_id"])
            if nm_id is None:
                continue
            by_nm.setdefault(nm_id, []).append(dict(row))
        return by_nm

    def _load_wb_component_estimates_by_nm(self) -> dict[int, float]:
        with _connect(self.runtime.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT nm_id,
                       SUM((transit_per_unit_rub + ff_services_per_unit_rub + ff_storage_per_unit_rub) * accepted_qty)
                       / NULLIF(SUM(accepted_qty), 0) AS component_per_unit
                FROM sheet_vitrina_v1_wb_supply_cost_layers
                WHERE is_current = 1
                  AND source_status IN ('confirmed', 'estimated', 'pending')
                  AND accepted_qty > 0
                GROUP BY nm_id
                """
            ).fetchall()
        return {
            int(row["nm_id"]): _number_or_zero(row["component_per_unit"])
            for row in rows
            if row["nm_id"] is not None
        }

    def _load_active_sku_names(self) -> dict[int, str]:
        try:
            state = self.runtime.load_current_state()
        except Exception:
            return {}
        with _connect(self.runtime.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT nm_id, display_name
                FROM registry_upload_config_v2
                WHERE bundle_version = ? AND enabled = 1
                ORDER BY display_order ASC, nm_id ASC
                """,
                (state.bundle_version,),
            ).fetchall()
        return {int(row["nm_id"]): str(row["display_name"]) for row in rows}

    def _load_snapshot_sku_metric(self, as_of_date: str, metric_key: str) -> dict[int, float]:
        try:
            snapshot = self.runtime.load_sheet_vitrina_ready_snapshot_any_bundle(as_of_date=as_of_date)
        except Exception:
            return {}
        return _extract_snapshot_sku_metric(snapshot, column_date=as_of_date, metric_key=metric_key)

    def _load_stock_metrics_by_date_column(
        self,
        *,
        snapshot_dates: Iterable[str],
        opening_date: str,
    ) -> dict[str, dict[int, float]]:
        by_date: dict[str, dict[int, float]] = {}
        for snapshot_date in snapshot_dates:
            try:
                snapshot = self.runtime.load_sheet_vitrina_ready_snapshot_any_bundle(as_of_date=snapshot_date)
            except Exception:
                continue
            for column_date in snapshot.date_columns:
                date_key = str(column_date or "")
                if date_key < opening_date:
                    continue
                values = _extract_snapshot_sku_metric(snapshot, column_date=date_key, metric_key="stock_total")
                if values:
                    by_date[date_key] = values
        return by_date

    def _select_opening_baseline(
        self,
        *,
        nm_id: int,
        display_name: str,
        opening_stock_qty: float,
        ff_line: Mapping[str, Any] | None,
        component_estimate: float,
        metric11_value: float | None,
        opening_date: str,
    ) -> dict[str, Any]:
        if ff_line is not None and _optional_float(ff_line.get("sku_ff_unit_cost_rub")) is not None:
            unit_cost = _number_or_zero(ff_line.get("sku_ff_unit_cost_rub")) + component_estimate
            accepted_ff_date = str(ff_line.get("accepted_ff_date") or "")
            if "2026-06-21" <= accepted_ff_date <= "2026-06-24":
                source_priority = 1
                source_status = OPENING_SOURCE_CONFIRMED_SUPPLY
                confirmed_qty = opening_stock_qty
                estimated_qty = 0.0
            else:
                source_priority = 0
                source_status = ""
                confirmed_qty = 0.0
                estimated_qty = 0.0
            if source_priority:
                return {
                    "nm_id": nm_id,
                    "display_name": display_name,
                    "opening_unit_cost_rub": unit_cost,
                    "source_priority": source_priority,
                    "source_status": source_status,
                    "supplier_ff_cost_layer_id": str(ff_line.get("layer_id") or ""),
                    "supplier_ff_cost_layer_line_id": str(ff_line.get("layer_line_id") or ""),
                    "metric11_value": None,
                    "confirmed_qty": confirmed_qty,
                    "estimated_qty": estimated_qty,
                    "fallback_qty": 0.0,
                    "missing_reason": None,
                    "component_status": {
                        "supplier_ff_cost": str(ff_line.get("layer_status") or "confirmed"),
                        "supplier_ff_accepted_ff_date": accepted_ff_date,
                        "wb_component_estimate": "forbidden_for_opening_baseline",
                        "model": "management_proxy_opening_baseline",
                    },
                }
        return {
            "nm_id": nm_id,
            "display_name": display_name,
            "opening_unit_cost_rub": None,
            "source_priority": 4,
            "source_status": OPENING_SOURCE_NEEDS_REVIEW,
            "supplier_ff_cost_layer_id": None,
            "supplier_ff_cost_layer_line_id": None,
            "metric11_value": None,
            "confirmed_qty": 0.0,
            "estimated_qty": opening_stock_qty,
            "fallback_qty": 0.0,
            "missing_reason": "missing_known_cost_and_metric11_fallback",
            "component_status": {
                "model": "management_proxy_opening_baseline",
                "missing": "known_cost_and_metric11",
            },
        }

    def _load_opening_baseline(self, opening_date: str) -> dict[int, dict[str, Any]]:
        with _connect(self.runtime.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_wb_opening_baseline
                WHERE as_of_date = ?
                """,
                (opening_date,),
            ).fetchall()
        return {int(row["nm_id"]): dict(row) for row in rows if row["nm_id"] is not None}

    def _load_wb_supply_cost_layers_by_date(
        self,
        *,
        opening_date: str,
    ) -> dict[str, dict[int, list[dict[str, Any]]]]:
        with _connect(self.runtime.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_wb_supply_cost_layers
                WHERE is_current = 1
                  AND accepted_qty > 0
                ORDER BY accepted_date ASC, wb_supply_id ASC
                """,
            ).fetchall()
        by_date: dict[str, dict[int, list[dict[str, Any]]]] = {}
        for row in rows:
            row_dict = dict(row)
            if not _wb_supply_cost_layer_is_physical_inbound(row_dict):
                continue
            supply_date = _wb_supply_business_date_key(row_dict.get("accepted_date"))
            nm_id = _optional_int(row_dict.get("nm_id"))
            if not supply_date or supply_date < opening_date or nm_id is None:
                continue
            by_date.setdefault(supply_date, {}).setdefault(nm_id, []).append(row_dict)
        return by_date

    def _build_wb_supply_cost_layer_payload(
        self,
        *,
        supply: Mapping[str, Any],
        nm_id: int,
        accepted_qty: float,
        quantity_source: str,
        quantity_is_final_accepted: bool,
        denominator: float,
        ff_line: Mapping[str, Any] | None,
        transit: TransitCostClassification,
        services_total: float,
        services_per_unit: float,
        storage_total: float,
        storage_per_unit: float,
        overlay: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        sku_ff_cost = _optional_float((ff_line or {}).get("sku_ff_unit_cost_rub"))
        if sku_ff_cost is None:
            our_cost = None
            source_status = WB_COST_STATUS_NEEDS_REVIEW
            missing_reason = "missing_supplier_ff_cost_layer"
        else:
            transit_per_unit = transit.per_unit
            if transit_per_unit is None:
                transit_per_unit = 0.0
            our_cost = sku_ff_cost + transit_per_unit + services_per_unit + storage_per_unit
            if transit.status == TRANSIT_MISSING or transit.status == TRANSIT_UNKNOWN_ROUTE:
                source_status = WB_COST_STATUS_PENDING
                missing_reason = transit.missing_reason or "transit_cost_pending"
            elif not quantity_is_final_accepted:
                source_status = WB_COST_STATUS_ESTIMATED
                missing_reason = "wb_supply_quantity_not_final_accepted"
            elif ff_line is not None and str(ff_line.get("layer_status") or "") == SUPPLIER_FF_STATUS_CONFIRMED:
                source_status = WB_COST_STATUS_CONFIRMED
                missing_reason = None
            else:
                source_status = WB_COST_STATUS_ESTIMATED
                missing_reason = None
        supply_id = str(supply.get("supply_id") or supply.get("wb_supply_id") or "")
        supply_status_id = _wb_supply_status_id(supply)
        upload_ids = (overlay or {}).get("upload_ids") or []
        payload = {
            "wb_supply_id": supply_id,
            "cache_key": _optional_text(supply.get("cache_key")),
            "nm_id": nm_id,
            "accepted_qty": accepted_qty,
            "qty_denominator": denominator,
            "supply_date": _optional_text(supply.get("supply_date") or supply.get("fact_date")),
            "accepted_date": _optional_text(supply.get("fact_date")),
            "supplier_ff_cost_layer_id": _optional_text((ff_line or {}).get("layer_id")),
            "supplier_ff_cost_layer_line_id": _optional_text((ff_line or {}).get("layer_line_id")),
            "sku_ff_unit_cost_rub": sku_ff_cost,
            "transit_cost_status": transit.status,
            "transit_amount_total": transit.amount_total,
            "transit_per_unit_rub": transit.per_unit if transit.per_unit is not None else 0.0,
            "ff_upload_id": _optional_text(",".join(str(item) for item in upload_ids) if upload_ids else None),
            "ff_services_amount_total": services_total,
            "ff_services_per_unit_rub": services_per_unit,
            "ff_storage_amount_total": storage_total,
            "ff_storage_per_unit_rub": storage_per_unit,
            "our_wb_unit_cost_rub": our_cost,
            "source_status": source_status,
            "missing_reason": missing_reason,
            "component_status": {
                "supplier_ff_cost": str((ff_line or {}).get("layer_status") or "missing"),
                "transit": transit.status,
                "ff_services": "accepted_upload" if overlay else "missing_or_zero",
                "ff_storage": "accepted_upload_allocated_storage" if overlay else "missing_or_zero",
                "transit_evidence": transit.evidence,
                "wb_supply_status_id": supply_status_id,
                "wb_quantity_source": quantity_source,
                "wb_quantity_final_accepted": quantity_is_final_accepted,
            },
        }
        payload_input = dict(payload)
        payload["input"] = {
            "schema": "wb_supply_cost_layer_v1",
            "payload": payload_input,
            "supply": _selected_wb_supply_inputs(supply),
        }
        return payload

    def _upsert_wb_supply_cost_layer(
        self,
        *,
        conn: Any,
        now: str,
        payload: Mapping[str, Any],
        inputs_hash: str,
    ) -> bool:
        existing = conn.execute(
            """
            SELECT wb_supply_cost_layer_id, inputs_hash, version
            FROM sheet_vitrina_v1_wb_supply_cost_layers
            WHERE wb_supply_id = ? AND nm_id = ? AND is_current = 1
            ORDER BY version DESC
            LIMIT 1
            """,
            (payload["wb_supply_id"], payload["nm_id"]),
        ).fetchone()
        if existing is not None and str(existing["inputs_hash"]) == inputs_hash:
            return False
        version_row = conn.execute(
            """
            SELECT COALESCE(MAX(version), 0) AS max_version
            FROM sheet_vitrina_v1_wb_supply_cost_layers
            WHERE wb_supply_id = ? AND nm_id = ?
            """,
            (payload["wb_supply_id"], payload["nm_id"]),
        ).fetchone()
        version = int(version_row["max_version"] or 0) + 1
        layer_id = f"wbcost_{payload['wb_supply_id']}_{payload['nm_id']}_{version}"
        supersedes_id = str(existing["wb_supply_cost_layer_id"]) if existing is not None else None
        if supersedes_id:
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_supply_cost_layers
                SET is_current = 0,
                    superseded_at = ?
                WHERE wb_supply_cost_layer_id = ?
                """,
                (now, supersedes_id),
            )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_wb_supply_cost_layers (
                wb_supply_cost_layer_id,
                wb_supply_id,
                cache_key,
                nm_id,
                accepted_qty,
                qty_denominator,
                supply_date,
                accepted_date,
                supplier_ff_cost_layer_id,
                supplier_ff_cost_layer_line_id,
                sku_ff_unit_cost_rub,
                transit_cost_status,
                transit_amount_total,
                transit_per_unit_rub,
                ff_upload_id,
                ff_services_amount_total,
                ff_services_per_unit_rub,
                ff_storage_amount_total,
                ff_storage_per_unit_rub,
                our_wb_unit_cost_rub,
                source_status,
                component_status_json,
                missing_reason,
                calculated_at,
                inputs_hash,
                version,
                is_current,
                supersedes_id,
                superseded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, NULL)
            """,
            (
                layer_id,
                payload["wb_supply_id"],
                payload["cache_key"],
                payload["nm_id"],
                payload["accepted_qty"],
                payload["qty_denominator"],
                payload["supply_date"],
                payload["accepted_date"],
                payload["supplier_ff_cost_layer_id"],
                payload["supplier_ff_cost_layer_line_id"],
                payload["sku_ff_unit_cost_rub"],
                payload["transit_cost_status"],
                payload["transit_amount_total"],
                payload["transit_per_unit_rub"],
                payload["ff_upload_id"],
                payload["ff_services_amount_total"],
                payload["ff_services_per_unit_rub"],
                payload["ff_storage_amount_total"],
                payload["ff_storage_per_unit_rub"],
                payload["our_wb_unit_cost_rub"],
                payload["source_status"],
                _json_dumps(payload["component_status"]),
                payload["missing_reason"],
                now,
                inputs_hash,
                version,
                supersedes_id,
            ),
        )
        return True


def classify_wb_supply_transit(
    supply_row: Mapping[str, Any],
    *,
    denominator: float | int | None = None,
) -> TransitCostClassification:
    has_transit_marker = bool(
        supply_row.get("has_transit_cost_marker")
        or supply_row.get("transit_warehouse_id")
        or str(supply_row.get("transit_warehouse_name") or "").strip()
    )
    official_cost = _optional_float(supply_row.get("cost_total"))
    acceptance_cost = _optional_float(supply_row.get("acceptanceCost") or supply_row.get("acceptance_cost"))
    effective_cost = _optional_float(supply_row.get("effective_transit_cost_total"))
    seller_portal_cost = _optional_float(supply_row.get("seller_portal_transit_cost_total"))
    cost_source = str(supply_row.get("cost_evidence") or supply_row.get("effective_transit_cost_source") or "")
    qty = _positive_number(denominator)
    if has_transit_marker:
        amount = effective_cost if effective_cost is not None else seller_portal_cost
        if amount is None:
            amount = official_cost if official_cost is not None else acceptance_cost
        if amount is not None:
            return TransitCostClassification(
                status=TRANSIT_CONFIRMED,
                amount_total=amount,
                per_unit=amount / qty if qty > 0 else None,
                evidence=cost_source or "transit_marker_with_cost",
            )
        return TransitCostClassification(
            status=TRANSIT_MISSING,
            amount_total=None,
            per_unit=None,
            evidence="transit_marker_without_cost",
            missing_reason="transit_marker_present_but_cost_missing",
        )
    route_known = bool(
        str(supply_row.get("warehouseName") or supply_row.get("warehouse_name") or "").strip()
        or str(supply_row.get("warehouse_display") or "").strip()
        or supply_row.get("warehouse_id")
    )
    zero_cost_evidence = (
        official_cost == 0
        or acceptance_cost == 0
        or effective_cost == 0
        or seller_portal_cost == 0
    )
    if route_known and zero_cost_evidence:
        return TransitCostClassification(
            status=TRANSIT_DIRECT_ZERO_CONFIRMED,
            amount_total=0.0,
            per_unit=0.0,
            evidence=cost_source or "direct_route_zero_acceptance_cost",
        )
    return TransitCostClassification(
        status=TRANSIT_UNKNOWN_ROUTE,
        amount_total=None,
        per_unit=None,
        evidence="route_not_classifiable",
        missing_reason="cannot_determine_direct_or_transit_route",
    )


def _select_opening_ff_line_for_baseline(
    lines: Iterable[Mapping[str, Any]],
    *,
    opening_date: str,
) -> Mapping[str, Any] | None:
    usable = [
        line
        for line in lines
        if _optional_float(line.get("sku_ff_unit_cost_rub")) is not None
    ]
    opening_window = [
        line
        for line in usable
        if "2026-06-21" <= str(line.get("accepted_ff_date") or "") <= "2026-06-24"
    ]
    if opening_window:
        return sorted(opening_window, key=lambda line: str(line.get("accepted_ff_date") or ""), reverse=True)[0]
    return None


def _extract_snapshot_sku_metric(snapshot: Any, *, column_date: str, metric_key: str) -> dict[int, float]:
    try:
        date_index = list(snapshot.date_columns).index(column_date)
        value_index = 2 + date_index
    except ValueError:
        value_index = -1
    for sheet in snapshot.sheets:
        if sheet.sheet_name != "DATA_VITRINA":
            continue
        result: dict[int, float] = {}
        for row in sheet.rows:
            if len(row) < 3 or (value_index >= 0 and len(row) <= value_index):
                continue
            row_id = str(row[1] or "")
            prefix = "SKU:"
            suffix = f"|{metric_key}"
            if not row_id.startswith(prefix) or not row_id.endswith(suffix):
                continue
            nm_raw = row_id[len(prefix) : -len(suffix)]
            nm_id = _optional_int(nm_raw)
            value = _optional_float(row[value_index])
            if nm_id is not None and value is not None:
                result[nm_id] = value
        return result
    return {}


def _roll_daily_state(
    *,
    previous: Mapping[str, Any],
    stock_qty: float,
    inbounds: Iterable[Mapping[str, Any]],
) -> dict[str, float | str | None | dict[str, Any]]:
    prev_stock = _number_or_zero(previous.get("stock_qty"))
    inbound_rows = list(inbounds)
    inbound_qty = _sum_positive(_number_or_zero(row.get("accepted_qty")) for row in inbound_rows)
    base_stock_qty = max(stock_qty - inbound_qty, 0.0)
    previous_confirmed_qty = _number_or_zero(previous.get("_carry_confirmed_qty", previous.get("confirmed_qty")))
    previous_estimated_qty = _number_or_zero(previous.get("_carry_estimated_qty", previous.get("estimated_qty")))
    previous_fallback_qty = _number_or_zero(previous.get("_carry_fallback_qty", previous.get("fallback_qty")))
    previous_bucket_qty = previous_confirmed_qty + previous_estimated_qty + previous_fallback_qty
    previous_basis_qty = prev_stock if prev_stock > 0 else previous_bucket_qty
    preserved_base_qty = min(base_stock_qty, previous_basis_qty)
    scale = preserved_base_qty / previous_basis_qty if previous_basis_qty > 0 else 0.0
    unexplained_base_growth_qty = max(base_stock_qty - previous_basis_qty, 0.0)
    base_cost = _optional_float(previous.get("our_wb_unit_cost_rub"))
    confirmed_qty = previous_confirmed_qty * scale
    estimated_qty = previous_estimated_qty * scale + unexplained_base_growth_qty
    fallback_qty = previous_fallback_qty * scale
    weighted_cost_sum = (base_cost or 0.0) * base_stock_qty if base_cost is not None else 0.0
    cost_weight_qty = base_stock_qty if base_cost is not None else 0.0
    status = str(previous.get("source_status") or "")
    component_status = dict(previous.get("component_status") or {})
    for row in inbound_rows:
        qty = _number_or_zero(row.get("accepted_qty"))
        unit_cost = _optional_float(row.get("our_wb_unit_cost_rub"))
        if qty <= 0:
            continue
        source_status = str(row.get("source_status") or "")
        if unit_cost is not None:
            weighted_cost_sum += qty * unit_cost
            cost_weight_qty += qty
        if source_status == WB_COST_STATUS_CONFIRMED and unit_cost is not None:
            confirmed_qty += qty
        elif source_status == WB_COST_STATUS_FALLBACK and unit_cost is not None:
            fallback_qty += qty
        else:
            estimated_qty += qty
        status = source_status
        component_status = _json_loads(row.get("component_status_json"))
    unit_cost = weighted_cost_sum / cost_weight_qty if cost_weight_qty > 0 else base_cost
    carry_confirmed_qty = confirmed_qty
    carry_estimated_qty = estimated_qty
    carry_fallback_qty = fallback_qty
    total_buckets = confirmed_qty + estimated_qty + fallback_qty
    if stock_qty > 0 and total_buckets > stock_qty:
        scale_to_stock = stock_qty / total_buckets
        confirmed_qty *= scale_to_stock
        estimated_qty *= scale_to_stock
        fallback_qty *= scale_to_stock
        carry_confirmed_qty = confirmed_qty
        carry_estimated_qty = estimated_qty
        carry_fallback_qty = fallback_qty
    elif stock_qty <= 0:
        confirmed_qty = 0.0
        estimated_qty = 0.0
        fallback_qty = 0.0
    return {
        "stock_qty": stock_qty,
        "our_wb_unit_cost_rub": unit_cost,
        "confirmed_qty": confirmed_qty,
        "estimated_qty": estimated_qty,
        "fallback_qty": fallback_qty,
        "_carry_confirmed_qty": carry_confirmed_qty,
        "_carry_estimated_qty": carry_estimated_qty,
        "_carry_fallback_qty": carry_fallback_qty,
        "source_status": status,
        "component_status": component_status,
    }


def _empty_daily_state(stock_qty: float) -> dict[str, float | str | None | dict[str, Any]]:
    return {
        "stock_qty": stock_qty,
        "our_wb_unit_cost_rub": None,
        "confirmed_qty": 0.0,
        "estimated_qty": stock_qty,
        "fallback_qty": 0.0,
        "source_status": WB_COST_STATUS_NEEDS_REVIEW,
        "component_status": {"missing": "opening_baseline"},
    }


@dataclass(frozen=True)
class _WbGoodQuantity:
    qty: float
    source: str
    is_final_accepted: bool


def _wb_supply_cost_layer_is_physical_inbound(row: Mapping[str, Any]) -> bool:
    component_status = _json_loads(row.get("component_status_json"))
    return bool(
        _optional_int(component_status.get("wb_supply_status_id")) == WB_SUPPLY_STATUS_ACCEPTED
        and component_status.get("wb_quantity_final_accepted") is True
        and str(component_status.get("wb_quantity_source") or "")
        in {"acceptedQuantity", "accepted_quantity"}
        and _wb_supply_business_date_key(row.get("accepted_date"))
    )


def _normalized_wb_row(supply: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _json_loads(supply.get("normalized_row_json"))
    if isinstance(normalized, dict):
        merged = {**normalized, **dict(supply)}
    else:
        merged = dict(supply)
    return merged


def _parse_wb_goods(raw_goods_json: Any) -> list[dict[str, Any]]:
    payload = _json_loads(raw_goods_json)
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        goods = payload.get("goods") or payload.get("data") or payload.get("items")
        if isinstance(goods, list):
            return [dict(item) for item in goods if isinstance(item, Mapping)]
    return []


def _wb_good_qty(item: Mapping[str, Any]) -> float:
    return _wb_good_quantity(item).qty


def _wb_good_quantity(item: Mapping[str, Any]) -> _WbGoodQuantity:
    for key in ("acceptedQuantity", "accepted_quantity"):
        if item.get(key) is not None:
            return _WbGoodQuantity(qty=_number_or_zero(item.get(key)), source=key, is_final_accepted=True)
    for key in ("quantity", "qty"):
        if item.get(key) is not None:
            return _WbGoodQuantity(qty=_number_or_zero(item.get(key)), source=key, is_final_accepted=False)
    return _WbGoodQuantity(qty=0.0, source="missing", is_final_accepted=False)


def _wb_supply_status_id(supply: Mapping[str, Any]) -> int | None:
    status_id = _optional_int(supply.get("status_id") or supply.get("statusID") or supply.get("statusId"))
    if status_id is not None:
        return status_id
    normalized = _json_loads(supply.get("normalized_row_json"))
    if isinstance(normalized, Mapping):
        return _optional_int(normalized.get("status_id") or normalized.get("statusID") or normalized.get("statusId"))
    return None


def _wb_good_quantity_is_final_accepted(*, supply: Mapping[str, Any], quantity: _WbGoodQuantity) -> bool:
    return quantity.is_final_accepted and _wb_supply_status_id(supply) == WB_SUPPLY_STATUS_ACCEPTED


def _wb_supply_row_to_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _selected_supplier_header_inputs(header: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "shipment_id",
        "actual_ff_acceptance_date",
        "order_status",
        "invoice_no",
        "invoice_date",
        "currency",
        "approx_yuan_rate",
        "cny_payment_currency_rub_cost",
        "cny_bank_fee_rub",
        "product_qty_total",
        "product_amount_total",
        "extras_amount_total",
        "invoice_amount_total",
    )
    return {key: header.get(key) for key in keys}


def _selected_supplier_line_inputs(line: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "line_id",
        "line_type",
        "internal_nm_id",
        "internal_sku",
        "internal_name",
        "qty",
        "unit_price",
        "amount",
        "currency",
    )
    return {key: line.get(key) for key in keys}


def _selected_financial_document_inputs(document: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "document_id",
        "document_type",
        "parse_status",
        "document_number",
        "document_date",
        "total_amount_rub",
    )
    return {key: document.get(key) for key in keys}


def _selected_expense_line_inputs(line: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "line_id",
        "financial_document_id",
        "category",
        "amount_rub",
        "included_in_logistics_efficiency",
        "included_in_customs_total",
        "status",
    )
    return {key: line.get(key) for key in keys}


def _selected_wb_supply_inputs(supply: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "supply_id",
        "cache_key",
        "supply_date",
        "fact_date",
        "quantity_for_size_filter",
        "normalized_row_json",
        "raw_goods_json",
    )
    return {key: supply.get(key) for key in keys}


def _stable_hash(payload: Mapping[str, Any]) -> str:
    normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)


def _json_loads(payload: Any) -> Any:
    if payload is None:
        return {}
    if isinstance(payload, (dict, list)):
        return payload
    try:
        return json.loads(str(payload))
    except Exception:
        return {}


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _wb_supply_business_date_key(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None or len(text) < 10:
        return None
    date_part = text[:10]
    try:
        return date.fromisoformat(date_part).isoformat()
    except ValueError:
        return None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
        if not text:
            return None
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if isinstance(value, str) and not value.strip():
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive_number(value: Any) -> float:
    number = _optional_float(value)
    if number is None or number <= 0:
        return 0.0
    return number


def _number_or_zero(value: Any) -> float:
    number = _optional_float(value)
    return 0.0 if number is None else number


def _sum_positive(values: Iterable[float]) -> float:
    return sum(value for value in values if value > 0)


def _weighted_avg(pairs: Iterable[tuple[float | None, float]]) -> float | None:
    numerator = 0.0
    denominator = 0.0
    for value, weight in pairs:
        if value is None or weight <= 0:
            continue
        numerator += value * weight
        denominator += weight
    if denominator <= 0:
        return None
    return numerator / denominator


def _default_timestamp_factory() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_cost_recalculation_request_id() -> str:
    return f"our_wb_cost_recalc_{date.today().isoformat()}_{uuid.uuid4().hex[:8]}"
