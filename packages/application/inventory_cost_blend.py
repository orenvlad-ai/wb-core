"""Forward-only informational WB + FF inventory-cost projection.

This module deliberately does not resolve realized sale COGS.  Finance and
Partner continue to use the channel/location resolver, including exact FBS
facility WAC frozen at durable handoff.  The blend is only the as-of inventory
cost shown by the Vitrina and consumed by indicative Proxy 3/4.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any, Mapping

from packages.application.sheet_vitrina_v1_own_product_capital import (
    own_stage_metric_key,
)


INVENTORY_COST_BLEND_EFFECTIVE_DATE = "2026-08-22"
INVENTORY_COST_BLEND_FORMULA_VERSION = "our_inventory_wac_wb_ff_v1"
INVENTORY_COST_BLEND_SELECTION_METHOD = (
    "exact_as_of_functional_version_wb_plus_ff_physical_inventory"
)
INCLUDED_STAGES = ("WB", "FF")
ZERO = Decimal("0")
PUBLIC_PROJECTION_TOLERANCE = Decimal("0.000001")


def build_inventory_cost_blend_lookup(
    *,
    as_of_date: str,
    wb_compat_lookup: Mapping[int, Mapping[str, Any]],
    product_capital_lookup: Mapping[int, Mapping[str, Any]],
) -> dict[int, dict[str, Any]]:
    """Return Vitrina-ready inventory WAC rows with exact evidence.

    Dates before the forward-only boundary keep the persisted WB compatibility
    projection.  This avoids turning an ordinary deployment into an implicit
    historical data rewrite.  On and after the boundary every positive WB/FF
    location must be cost-covered and facility evidence for FF must reconcile
    to its aggregate; otherwise the blended value is absent with a reason.
    """

    date_key = str(as_of_date or "")[:10]
    if date_key < INVENTORY_COST_BLEND_EFFECTIVE_DATE:
        return {
            int(nm_id): deepcopy(dict(row))
            for nm_id, row in wb_compat_lookup.items()
        }

    result: dict[int, dict[str, Any]] = {}
    candidates = sorted(
        {int(value) for value in wb_compat_lookup}
        | {int(value) for value in product_capital_lookup}
    )
    for nm_id in candidates:
        legacy = deepcopy(dict(wb_compat_lookup.get(nm_id) or {}))
        product = dict(product_capital_lookup.get(nm_id) or {})
        row = _preserved_profit_evidence(legacy)
        evidence = _inventory_cost_evidence(
            nm_id=nm_id,
            as_of_date=date_key,
            product=product,
        )
        quantity = _decimal(evidence.get("quantity")) or ZERO
        covered = _decimal(evidence.get("cost_covered_quantity")) or ZERO
        capital = _decimal(evidence.get("capital_rub")) or ZERO
        status = str(evidence.get("status") or "unresolved")
        resolved = status == "resolved"
        unit_cost = capital / quantity if resolved and quantity > ZERO else None
        confirmed_quantity = _decimal(evidence.get("certified_quantity")) or ZERO
        source_digest = _digest(evidence)
        row.update(
            {
                "as_of_date": date_key,
                "canonical_source_date": date_key,
                "nm_id": nm_id,
                "stock_qty": float(quantity),
                "cost_covered_qty": float(covered),
                "our_wb_unit_cost_rub": (
                    float(unit_cost) if unit_cost is not None else None
                ),
                "confirmed_qty": float(confirmed_quantity),
                "estimated_qty": float(max(covered - confirmed_quantity, ZERO)),
                "fallback_qty": 0.0,
                "confirmed_share_pct": (
                    float(confirmed_quantity / quantity) if quantity > ZERO else None
                ),
                "source_status": (
                    "blended_inventory_wac_confirmed"
                    if resolved and confirmed_quantity >= quantity
                    else (
                        "blended_inventory_wac_provisional"
                        if resolved
                        else "blended_inventory_wac_unavailable"
                    )
                ),
                "source_reason": str(evidence.get("reason") or ""),
                "selection_method": INVENTORY_COST_BLEND_SELECTION_METHOD,
                "projection_quality": str(evidence.get("quality") or status),
                "source_digest": source_digest,
                "canonical_source_identity": (
                    f"warehouse_functional:{evidence.get('functional_version_id')}:WB+FF"
                    if evidence.get("functional_version_id")
                    else ""
                ),
                "component_status_json": json.dumps(
                    evidence,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "calculated_at": str(evidence.get("published_at") or ""),
                "inputs_hash": source_digest,
                "inventory_cost_evidence": evidence,
                "inventory_cost_formula_version": (
                    INVENTORY_COST_BLEND_FORMULA_VERSION
                ),
            }
        )
        result[nm_id] = row
    return result


def aggregate_inventory_cost_evidence(
    rows: Mapping[int, Mapping[str, Any]],
    *,
    nm_ids: list[int],
) -> dict[str, Any]:
    """Aggregate exact SKU evidence for the public TOTAL cell."""

    quantity = ZERO
    covered = ZERO
    capital = ZERO
    wb_quantity = ZERO
    wb_capital = ZERO
    ff_quantity = ZERO
    ff_capital = ZERO
    facility_pools: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
    version_ids: set[str] = set()
    published_at: set[str] = set()
    missing: list[int] = []
    for nm_id in nm_ids:
        row = rows.get(int(nm_id)) or {}
        evidence = row.get("inventory_cost_evidence")
        if not isinstance(evidence, Mapping):
            missing.append(int(nm_id))
            continue
        item_quantity = _decimal(evidence.get("quantity")) or ZERO
        item_covered = _decimal(evidence.get("cost_covered_quantity")) or ZERO
        item_capital = _decimal(evidence.get("capital_rub")) or ZERO
        if item_quantity > ZERO and str(evidence.get("status") or "") != "resolved":
            missing.append(int(nm_id))
        quantity += item_quantity
        covered += item_covered
        capital += item_capital
        if evidence.get("functional_version_id"):
            version_ids.add(str(evidence["functional_version_id"]))
        if evidence.get("published_at"):
            published_at.add(str(evidence["published_at"]))
        for stage in evidence.get("stages") or []:
            if not isinstance(stage, Mapping):
                continue
            stage_quantity = _decimal(stage.get("quantity")) or ZERO
            stage_capital = _decimal(stage.get("capital_rub")) or ZERO
            if str(stage.get("warehouse_family") or "") == "WB":
                wb_quantity += stage_quantity
                wb_capital += stage_capital
            elif str(stage.get("warehouse_family") or "") == "FF":
                ff_quantity += stage_quantity
                ff_capital += stage_capital
                for location in stage.get("locations") or []:
                    if not isinstance(location, Mapping):
                        continue
                    key = (
                        str(location.get("facility_id") or ""),
                        str(location.get("pool") or ""),
                    )
                    current_quantity, current_capital = facility_pools.get(
                        key, (ZERO, ZERO)
                    )
                    facility_pools[key] = (
                        current_quantity
                        + (_decimal(location.get("quantity")) or ZERO),
                        current_capital
                        + (_decimal(location.get("capital_rub")) or ZERO),
                    )
    resolved = not missing and quantity > ZERO and covered == quantity
    return {
        "status": "resolved" if resolved else "unresolved",
        "reason": "" if resolved else (
            "missing_inventory_cost_evidence" if missing else "no_physical_inventory"
        ),
        "formula_version": INVENTORY_COST_BLEND_FORMULA_VERSION,
        "quantity": format(quantity, "f"),
        "cost_covered_quantity": format(covered, "f"),
        "capital_rub": format(capital, "f"),
        "wac_rub": format(capital / quantity, "f") if resolved else None,
        "wb": _operand_payload(wb_quantity, wb_capital),
        "ff": _operand_payload(ff_quantity, ff_capital),
        "facility_pools": [
            {
                "facility_id": facility_id,
                "pool": pool,
                **_operand_payload(item_quantity, item_capital),
            }
            for (facility_id, pool), (item_quantity, item_capital) in sorted(
                facility_pools.items()
            )
        ],
        "functional_version_ids": sorted(version_ids),
        "published_at": sorted(published_at),
        "covered_sku_count": len(nm_ids) - len(missing),
        "requested_sku_count": len(nm_ids),
        "missing_nm_ids": sorted(missing),
        "quantity_basis": "physical_inventory_before_reservations",
    }


def _inventory_cost_evidence(
    *,
    nm_id: int,
    as_of_date: str,
    product: Mapping[str, Any],
) -> dict[str, Any]:
    functional_version_id = str(product.get("_warehouse_version_id") or "")
    published_at = str(product.get("_warehouse_published_at") or "")
    effective_at = str(product.get("_warehouse_effective_at") or "")
    stages_by_name = product.get("_inventory_cost_stages")
    stages_by_name = stages_by_name if isinstance(stages_by_name, Mapping) else {}
    stages: list[dict[str, Any]] = []
    quantity = ZERO
    covered = ZERO
    capital = ZERO
    certified_quantity = ZERO
    blockers: list[str] = []
    for stage_name in INCLUDED_STAGES:
        public_quantity = _decimal(
            product.get(own_stage_metric_key(stage_name, "qty"))
        )
        public_capital = _decimal(
            product.get(own_stage_metric_key(stage_name, "capital_rub"))
        )
        public_covered = _decimal(
            product.get(own_stage_metric_key(stage_name, "cost_covered_qty"))
        )
        if public_quantity is None or public_capital is None or public_covered is None:
            blockers.append(f"missing_{stage_name.lower()}_stage")
            continue
        stage = dict(stages_by_name.get(stage_name) or {})
        if public_quantity > ZERO and not stage:
            blockers.append(f"missing_{stage_name.lower()}_stage_evidence")
        stage_quantity = _decimal(stage.get("quantity")) if stage else public_quantity
        stage_capital = _decimal(stage.get("capital_rub")) if stage else public_capital
        stage_covered = (
            _decimal(stage.get("cost_covered_quantity")) if stage else public_covered
        )
        if None in {stage_quantity, stage_capital, stage_covered}:
            blockers.append(f"invalid_{stage_name.lower()}_stage_evidence")
            continue
        # Exact blend arithmetic uses the immutable Decimal evidence.  These
        # historical public fields are floats, so compare the compatibility
        # projection at its published micro-ruble precision rather than by
        # textual Decimal scale.
        if (
            abs(stage_quantity - public_quantity) > PUBLIC_PROJECTION_TOLERANCE
            or abs(stage_capital - public_capital) > PUBLIC_PROJECTION_TOLERANCE
            or abs(stage_covered - public_covered) > PUBLIC_PROJECTION_TOLERANCE
        ):
            blockers.append(f"{stage_name.lower()}_stage_evidence_mismatch")
        if stage_quantity < ZERO or stage_covered < ZERO or stage_capital < ZERO:
            blockers.append(f"{stage_name.lower()}_stage_negative_operand")
        if stage_quantity > ZERO and stage_capital <= ZERO:
            blockers.append(f"{stage_name.lower()}_capital_nonpositive")
        if stage_quantity > ZERO and stage_covered != stage_quantity:
            blockers.append(f"{stage_name.lower()}_cost_coverage_incomplete")
        if stage_name == "FF" and stage_quantity > ZERO:
            if str(stage.get("location_status") or "") != "exact":
                blockers.append(
                    str(stage.get("location_status") or "missing_facility_pool_evidence")
                )
            if not stage.get("locations"):
                blockers.append("missing_facility_pool_evidence")
        quantity += stage_quantity
        covered += stage_covered
        capital += stage_capital
        if bool(stage.get("certified")):
            certified_quantity += stage_quantity
        stages.append(
            {
                "warehouse_family": stage_name,
                "quantity": format(stage_quantity, "f"),
                "capital_rub": format(stage_capital, "f"),
                "cost_covered_quantity": format(stage_covered, "f"),
                "wac_rub": (
                    format(stage_capital / stage_quantity, "f")
                    if stage_quantity > ZERO
                    else None
                ),
                "quality": str(stage.get("quality") or ""),
                "certified": bool(stage.get("certified")),
                "locations": list(stage.get("locations") or []),
            }
        )
    if not functional_version_id:
        blockers.append("missing_functional_version")
    if quantity <= ZERO:
        blockers.append("no_physical_inventory")
    status = "resolved" if not blockers else "unresolved"
    return {
        "status": status,
        "reason": "" if status == "resolved" else sorted(set(blockers))[0],
        "reason_codes": sorted(set(blockers)),
        "quality": (
            "confirmed" if certified_quantity >= quantity and quantity > ZERO
            else ("provisional" if status == "resolved" else "unavailable")
        ),
        "formula_version": INVENTORY_COST_BLEND_FORMULA_VERSION,
        "selection_method": INVENTORY_COST_BLEND_SELECTION_METHOD,
        "as_of_date": as_of_date,
        "nm_id": int(nm_id),
        "functional_version_id": functional_version_id,
        "effective_at": effective_at,
        "published_at": published_at,
        "source_watermarks": dict(product.get("_warehouse_source_watermarks") or {}),
        "quantity": format(quantity, "f"),
        "cost_covered_quantity": format(covered, "f"),
        "certified_quantity": format(certified_quantity, "f"),
        "capital_rub": format(capital, "f"),
        "wac_rub": (
            format(capital / quantity, "f")
            if status == "resolved" and quantity > ZERO
            else None
        ),
        "stages": stages,
        "quantity_basis": "physical_inventory_before_reservations",
        "reserve_capital_rub": "0",
    }


def _preserved_profit_evidence(legacy: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(legacy[key])
        for key in ("daily_profit_coverage", "sales_without_cost_rub")
        if key in legacy
    }


def _operand_payload(quantity: Decimal, capital: Decimal) -> dict[str, Any]:
    return {
        "quantity": format(quantity, "f"),
        "capital_rub": format(capital, "f"),
        "wac_rub": format(capital / quantity, "f") if quantity > ZERO else None,
    }


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
