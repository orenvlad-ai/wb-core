"""Canonical six-warehouse state, functional cutover and bounded WB replay.

The immutable ``warehouse_opening_v1`` tables remain audit evidence.  This
module owns the only active warehouse read model.  A version is calculated
from a coherent source capture and published atomically; failed attempts never
replace the last good version.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterable, Mapping

from packages.application.calculation_parameters import CalculationParametersBlock
from packages.application.canonical_cost_engine import CanonicalCostEngine
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.stocks_block import StocksBlock
from packages.application.warehouse_stocks import (
    INACTIVE_SUPPLIER_STATUSES,
    WB_FINAL_ACCEPTED_STATUS_ID,
    WB_POST_SHIPMENT_GATE_STATUS_IDS,
    WarehouseOpeningSnapshotError,
    WarehouseStocksBlock,
    _is_doprinato,
    _normalized_wb_record,
    _validated_wb_goods,
)


FUNCTIONAL_CUTOVER_ID = "warehouse_functional_cutover_v1"
CONTRACT_NAME = "sheet_vitrina_v1_warehouse_functional"
CONTRACT_VERSION = "v2"
ZERO = Decimal("0")
ONE = Decimal("1")

STAGE_PRODUCTION = "production"
STAGE_CHINA_TO_FF = "china_to_ff"
STAGE_FF = "ff"
STAGE_FF_TO_WB = "ff_to_wb"
STAGE_WB = "wb"
STAGE_DISCREPANCY = "wb_acceptance_discrepancy"
STAGES = (
    STAGE_PRODUCTION,
    STAGE_CHINA_TO_FF,
    STAGE_FF,
    STAGE_FF_TO_WB,
    STAGE_WB,
    STAGE_DISCREPANCY,
)

STAGE_NAMES = {
    STAGE_PRODUCTION: "На производстве",
    STAGE_CHINA_TO_FF: "Китай → FF",
    STAGE_FF: "Склад FF",
    STAGE_FF_TO_WB: "FF → WB",
    STAGE_WB: "Склад WB",
    STAGE_DISCREPANCY: "Расхождения приёмки WB",
}

BANK_FEE_CATEGORIES = {
    "bank_transfer_fee",
    "currency_control_fee",
    "currency_control_vat",
    "other_bank_fee",
}
LOGISTICS_DOCUMENT_TYPE = "logistics_invoice"
CUSTOMS_DOCUMENT_TYPE = "customs_declaration"
CUSTOMS_BY_QUANTITY = {"customs_fee_1010"}
CUSTOMS_BY_VALUE = {"import_duty_2010", "import_vat_5010"}


class WarehouseFunctionalError(WarehouseOpeningSnapshotError):
    """Fail-closed functional warehouse invariant error."""


def enqueue_warehouse_targeted_recalculation(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    stable_source_id: str,
    source_revision: str,
    effective_date: str,
    affected_nm_ids: Iterable[int],
    requested_at: str | None = None,
) -> dict[str, Any]:
    """Coalesce one source revision for the next bounded atomic publication."""

    stable_id = str(stable_source_id or "").strip()
    revision = str(source_revision or "").strip()
    business_date = str(effective_date or "")[:10]
    nm_ids = sorted({int(item) for item in affected_nm_ids if int(item) > 0})
    if not stable_id or not revision or len(business_date) != 10:
        raise ValueError("stable source id, revision and effective date are required")
    now = requested_at or _now()
    queue_id = _stable_id(
        "whrq",
        {"stable_source_id": stable_id, "source_revision": revision},
    )
    runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
    with _connect(runtime.db_path) as conn:
        ensure_warehouse_functional_schema(conn)
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_warehouse_targeted_recalc_queue(
                   queue_id,stable_source_id,source_revision,effective_date,affected_nm_ids_json,
                   status,requested_at,started_at,finished_at,error
               ) VALUES(?,?,?,?,?,'queued',?,NULL,NULL,NULL)
               ON CONFLICT(stable_source_id,source_revision) DO UPDATE SET
                   affected_nm_ids_json=excluded.affected_nm_ids_json,
                   effective_date=MIN(effective_date,excluded.effective_date),
                   requested_at=excluded.requested_at,
                   status=CASE WHEN status='complete' THEN status ELSE 'queued' END,
                   error=NULL""",
            (queue_id, stable_id, revision, business_date, _json(nm_ids), now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue WHERE queue_id=?",
            (queue_id,),
        ).fetchone()
    return dict(row) if row else {"queue_id": queue_id, "status": "queued"}


def load_supplier_flow_cost_state(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    shipment_id: str,
) -> dict[str, Any]:
    """Return shipment-specific active production/China stage costs."""

    with _connect(runtime.db_path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "sheet_vitrina_v1_warehouse_functional_active" not in tables:
            return {}
        active = conn.execute(
            "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
        ).fetchone()
        if active is None:
            return {}
        rows = conn.execute(
            """SELECT warehouse_key,quantity,capital_rub,certified,quality,provenance_json
               FROM sheet_vitrina_v1_warehouse_functional_balances
               WHERE version_id=? AND warehouse_key IN (?,?)""",
            (active["version_id"], STAGE_PRODUCTION, STAGE_CHINA_TO_FF),
        ).fetchall()
    totals: defaultdict[str, dict[str, Any]] = defaultdict(
        lambda: {"quantity": ZERO, "capital": ZERO, "certified": True, "quality": set()}
    )
    for row in rows:
        for source in _loads(row["provenance_json"], {}).get("source_records") or []:
            if str(source.get("shipment_id") or "") != str(shipment_id or ""):
                continue
            stage = str(row["warehouse_key"])
            totals[stage]["quantity"] += _decimal(source.get("flow_quantity"))
            totals[stage]["capital"] += _decimal(source.get("flow_capital_rub"))
            totals[stage]["certified"] = totals[stage]["certified"] and bool(row["certified"])
            totals[stage]["quality"].add(str(row["quality"] or ""))
    result: dict[str, Any] = {}
    for stage, item in totals.items():
        qty = _decimal(item["quantity"])
        capital = _decimal(item["capital"])
        result[stage] = {
            "quantity": _text(qty),
            "capital_rub": _text(capital),
            "average_unit_cost_rub": _text(capital / qty) if qty > ZERO else None,
            "certified": bool(item["certified"]),
            "quality": sorted(item["quality"]),
        }
    return result


@dataclass(frozen=True)
class CostSeed:
    nm_id: int
    ff_unit_cost: Decimal
    wb_unit_cost: Decimal
    quality: str
    provenance: Mapping[str, Any]


@dataclass(frozen=True)
class WarehouseLine:
    warehouse_key: str
    nm_id: int
    quantity: Decimal
    capital: Decimal
    cost_covered_quantity: Decimal
    quality: str
    provenance: Mapping[str, Any]
    certified: bool = False
    wb_quantity: Decimal = ZERO
    wb_in_way_to_client: Decimal = ZERO
    wb_in_way_from_client: Decimal = ZERO

    @property
    def wac(self) -> Decimal | None:
        return self.capital / self.quantity if self.quantity > ZERO else None


def moving_weighted_average(
    *, quantity: Any, capital: Any, inbound_quantity: Any, inbound_capital: Any
) -> tuple[Decimal, Decimal, Decimal | None]:
    current_qty = _decimal(quantity)
    current_capital = _decimal(capital)
    inbound_qty = _decimal(inbound_quantity)
    inbound_cap = _decimal(inbound_capital)
    if min(current_qty, current_capital, inbound_qty, inbound_cap) < ZERO:
        raise ValueError("WAC inputs cannot be negative")
    result_qty = current_qty + inbound_qty
    result_capital = current_capital + inbound_cap
    return (
        result_qty,
        result_capital,
        result_capital / result_qty if result_qty > ZERO else None,
    )


def roll_periodic_wac(
    *, quantity: Any, capital: Any, quantity_delta: Any, capital_delta: Any
) -> tuple[Decimal, Decimal, Decimal | None]:
    """Roll a periodic cost pool, permitting a bounded source correction.

    Accepted-quantity corrections are signed deltas.  They may reverse a
    previously posted inbound layer, but they may never make the cost pool
    negative.  Ordinary warehouse movements continue to use the stricter
    positive-only ``moving_weighted_average`` helper.
    """

    current_qty = _decimal(quantity)
    current_capital = _decimal(capital)
    delta_qty = _decimal(quantity_delta)
    delta_capital = _decimal(capital_delta)
    if current_qty < ZERO or current_capital < ZERO:
        raise ValueError("periodic WAC opening inputs cannot be negative")
    result_qty = current_qty + delta_qty
    result_capital = current_capital + delta_capital
    if result_qty < ZERO or result_capital < ZERO:
        raise WarehouseFunctionalError("accepted source correction would make the WB cost pool negative")
    return (
        result_qty,
        result_capital,
        result_capital / result_qty if result_qty > ZERO else None,
    )


def accepted_quantity_delta(*, packed: Any, accepted: Any, previously_posted: Any) -> Decimal:
    current = min(max(_decimal(packed), ZERO), max(_decimal(accepted), ZERO))
    posted = _decimal(previously_posted)
    if posted < ZERO:
        raise ValueError("previously posted accepted quantity cannot be negative")
    return current - posted


def accepted_capital_delta(
    *, packed: Any, accepted: Any, unit_cost: Any, previously_posted_capital: Any
) -> Decimal:
    """Return the exact full-layer delta for late cost evidence or quantity correction."""

    current = min(max(_decimal(packed), ZERO), max(_decimal(accepted), ZERO))
    cost = _decimal(unit_cost)
    posted_capital = _decimal(previously_posted_capital)
    if min(cost, posted_capital) < ZERO:
        raise ValueError("accepted cost state cannot be negative")
    return current * cost - posted_capital


def allocate_capital(
    lines: Iterable[Mapping[str, Any]], *, total_capital: Any, method: str
) -> dict[int, Decimal]:
    """Allocate without intermediate rounding and conserve the exact total."""

    capital = _decimal(total_capital)
    if capital < ZERO:
        raise ValueError("capital cannot be negative")
    normalized: list[tuple[int, Decimal]] = []
    for raw in lines:
        nm_id = int(raw.get("nm_id") or raw.get("internal_nm_id") or 0)
        quantity = _decimal(raw.get("quantity") or raw.get("qty"))
        invoice_value = _decimal(raw.get("invoice_value") or raw.get("amount"))
        weight = quantity if method == "quantity" else invoice_value
        if nm_id <= 0 or quantity <= ZERO or weight <= ZERO:
            raise ValueError("allocation lines require positive nm_id, quantity and weight")
        normalized.append((nm_id, weight))
    denominator = sum((weight for _, weight in normalized), ZERO)
    if not normalized or denominator <= ZERO:
        raise ValueError("allocation denominator must be positive")
    result: defaultdict[int, Decimal] = defaultdict(Decimal)
    remainder = capital
    for index, (nm_id, weight) in enumerate(normalized):
        allocated = remainder if index == len(normalized) - 1 else capital * weight / denominator
        remainder -= allocated
        result[nm_id] += allocated
    return dict(result)


def reconcile_discrepancies(
    *,
    discrepancies: Iterable[Mapping[str, Any]],
    doprinato: Iterable[Mapping[str, Any]],
    audit: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pool positive discrepancies by SKU and quarantine unmatched doprinato."""

    pools: dict[int, dict[str, Any]] = {}
    for raw in discrepancies:
        nm_id = int(raw.get("nm_id") or 0)
        quantity = _decimal(raw.get("quantity"))
        capital = _decimal(raw.get("capital"))
        if nm_id <= 0 or quantity < ZERO or capital < ZERO:
            raise ValueError("invalid discrepancy receipt")
        if quantity == ZERO:
            continue
        pool = pools.setdefault(
            nm_id,
            {"nm_id": nm_id, "quantity": ZERO, "capital": ZERO, "receipts": [], "matches": []},
        )
        pool["quantity"] += quantity
        pool["capital"] += capital
        pool["receipts"].append(dict(raw))

    unmatched: list[dict[str, Any]] = []
    ordered = sorted(
        (dict(item) for item in doprinato),
        key=lambda item: (str(item.get("business_date") or ""), str(item.get("source_id") or "")),
    )
    for raw in ordered:
        nm_id = int(raw.get("nm_id") or 0)
        quantity = _decimal(raw.get("quantity"))
        if nm_id <= 0 or quantity < ZERO:
            raise ValueError("invalid doprinato")
        pool = pools.get(nm_id)
        available = _decimal((pool or {}).get("quantity"))
        matched = min(quantity, available)
        unmatched_qty = quantity - matched
        if matched > ZERO and pool is not None:
            wac = pool["capital"] / pool["quantity"]
            pool["quantity"] -= matched
            pool["capital"] -= matched * wac
            pool["matches"].append({**raw, "matched_quantity": _text(matched), "wac": _text(wac)})
        else:
            wac = None
        if unmatched_qty > ZERO:
            unmatched.append(
                {
                    **raw,
                    "quantity": _text(unmatched_qty),
                    "matched_quantity": _text(matched),
                    "reason": str(raw.get("reason") or "no_positive_discrepancy_for_sku"),
                }
            )
        if audit is not None:
            audit.append(
                {
                    **raw,
                    "matched_quantity": _text(matched),
                    "unmatched_quantity": _text(unmatched_qty),
                    "matched_wac_rub": _text(wac) if wac is not None else None,
                    "matched_capital_rub": _text(matched * wac) if wac is not None else "0",
                }
            )
    balances = [
        {
            **pool,
            "quantity": _text(pool["quantity"]),
            "capital": _text(pool["capital"]),
            "wac": _text(pool["capital"] / pool["quantity"]) if pool["quantity"] > ZERO else None,
        }
        for _, pool in sorted(pools.items())
        if pool["quantity"] > ZERO
    ]
    return balances, unmatched


def validate_cutover_ff_debit_coverage(capture: Mapping[str, Any]) -> dict[str, Any]:
    """Prove that every gated WB supply is already excluded from current FF.

    A supply is covered by an explicit append-only debit, by the immutable
    checkpoint membership, or by a business timestamp at/before the checkpoint
    boundary whose imported FF balance already absorbed it.
    """

    explicit_supply_ids = {
        str(row.get("source_object_id") or "")
        for row in capture.get("ff_operations") or []
        if str(row.get("source_type") or "")
        in {"wb_supply", "wb_supply_targeted_reconciliation"}
        and str(row.get("source_object_id") or "")
    }
    checkpoint_rows = list(capture.get("ff_auto_writeoff_checkpoint") or [])
    checkpoint = dict(checkpoint_rows[-1]) if checkpoint_rows else {}
    baseline_supply_ids = {
        str(item)
        for item in _loads(checkpoint.get("baseline_supply_ids_json"), [])
        if str(item or "")
    }
    checkpoint_date = str(checkpoint.get("created_at") or "")[:10]
    covered = 0
    checked = 0
    coverage_sources: defaultdict[str, int] = defaultdict(int)
    blockers: list[str] = []
    for raw in capture.get("wb_supplies") or []:
        record = _normalized_wb_record(raw)
        if _is_doprinato(record) or int(record.get("status_id") or 0) not in (
            WB_POST_SHIPMENT_GATE_STATUS_IDS | {WB_FINAL_ACCEPTED_STATUS_ID}
        ):
            continue
        if not any(_decimal(item.get("quantity")) > ZERO for item in _validated_wb_goods(record)):
            continue
        checked += 1
        supply_id = str(record.get("supply_id") or raw.get("supply_id") or "")
        wb_supply_id = str(record.get("wb_supply_id") or raw.get("wb_supply_id") or "")
        identities = {item for item in (supply_id, wb_supply_id) if item}
        if identities & explicit_supply_ids:
            coverage_sources["explicit_append_only_ff_debit"] += 1
            covered += 1
            continue
        if identities & baseline_supply_ids:
            coverage_sources["checkpoint_baseline_membership"] += 1
            covered += 1
            continue
        business_date = _supply_business_date(record, raw)
        if checkpoint_date and business_date and business_date <= checkpoint_date:
            coverage_sources["checkpoint_business_boundary"] += 1
            covered += 1
            continue
        blockers.append(supply_id or wb_supply_id or "missing_supply_id")
    if blockers:
        raise WarehouseFunctionalError(
            "WB supplies passed shipment gate without FF debit/checkpoint coverage: "
            + ",".join(sorted(blockers))
        )
    return {
        "checked_supply_count": checked,
        "covered_supply_count": covered,
        "coverage_sources": dict(sorted(coverage_sources.items())),
        "checkpoint_id": str(checkpoint.get("checkpoint_id") or ""),
        "checkpoint_created_at": str(checkpoint.get("created_at") or ""),
        "uncovered_supply_count": 0,
    }


def build_frozen_opening_cost_map(
    *,
    target_nm_ids: Iterable[int],
    primary_rows: Iterable[Mapping[str, Any]],
    purchase_price_by_nm: Mapping[int, Any],
    downstream_rows: Iterable[Mapping[str, Any]],
    primary_identity: Mapping[str, Any],
) -> dict[int, CostSeed]:
    """Build the frozen 24.06 map with explicit quality for every target SKU."""

    direct: dict[int, tuple[Decimal, Decimal, Decimal]] = {}
    bands: defaultdict[Decimal, list[tuple[Decimal, Decimal]]] = defaultdict(list)
    overall_qty = ZERO
    overall_capital = ZERO
    for raw in primary_rows:
        nm_id = int(raw.get("nm_id") or 0)
        quantity = _decimal(raw.get("qty") or raw.get("quantity"))
        price = _decimal(raw.get("invoice_unit_price_cny") or raw.get("purchase_price_cny"))
        cost = _decimal(raw.get("sku_ff_unit_cost_rub") or raw.get("ff_unit_cost_rub"))
        if nm_id <= 0 or min(quantity, price, cost) <= ZERO:
            continue
        old_qty, old_capital, _ = direct.get(nm_id, (ZERO, ZERO, ZERO))
        direct[nm_id] = (old_qty + quantity, old_capital + quantity * cost, price)
        bands[price].append((quantity, cost))
        overall_qty += quantity
        overall_capital += quantity * cost
    if overall_qty <= ZERO or not bands:
        raise WarehouseFunctionalError("frozen opening primary shipment has no positive cost bands")
    band_cost = {
        price: sum((qty * cost for qty, cost in rows), ZERO) / sum((qty for qty, _ in rows), ZERO)
        for price, rows in bands.items()
    }
    sorted_bands = sorted(band_cost)
    overall_cost = overall_capital / overall_qty

    downstream_rows = [dict(row) for row in downstream_rows]
    downstream_components = _supply_downstream_component_index(downstream_rows)
    downstream: defaultdict[int, list[tuple[Decimal, Decimal]]] = defaultdict(list)
    total_downstream_qty = ZERO
    total_downstream_capital = ZERO
    for raw in downstream_rows:
        nm_id = int(raw.get("nm_id") or 0)
        quantity = _decimal(raw.get("quantity") or raw.get("accepted_qty"))
        component = downstream_components.get((str(raw.get("wb_supply_id") or ""), nm_id))
        if nm_id <= 0 or quantity <= ZERO or component is None:
            continue
        downstream_unit_cost = component["pre_acceptance_addon"] + component["acceptance_addon"]
        downstream[nm_id].append((quantity, downstream_unit_cost))
        total_downstream_qty += quantity
        total_downstream_capital += quantity * downstream_unit_cost
    if total_downstream_qty <= ZERO:
        raise WarehouseFunctionalError("confirmed downstream FF to WB cost evidence is missing")
    weighted_downstream_unit_cost = total_downstream_capital / total_downstream_qty

    result: dict[int, CostSeed] = {}
    for nm_id in sorted({int(item) for item in target_nm_ids if int(item) > 0}):
        purchase_price = _optional_decimal(purchase_price_by_nm.get(nm_id))
        provenance: dict[str, Any] = {"primary": dict(primary_identity)}
        if nm_id in direct:
            qty, capital, _price = direct[nm_id]
            ff_cost = capital / qty
            quality = "direct_24_06"
        elif purchase_price is not None and purchase_price in band_cost:
            ff_cost = band_cost[purchase_price]
            quality = "same_purchase_price"
            provenance["purchase_price_cny"] = _text(purchase_price)
        elif purchase_price is not None and len(sorted_bands) >= 2:
            lower = max((price for price in sorted_bands if price <= purchase_price), default=None)
            upper = min((price for price in sorted_bands if price >= purchase_price), default=None)
            if lower is not None and upper is not None and lower != upper:
                ff_cost = _linear(purchase_price, lower, band_cost[lower], upper, band_cost[upper])
                quality = "interpolation"
                points = (lower, upper)
            elif lower is None:
                nearest = sorted_bands[0]
                points = (nearest,)
                ff_cost = purchase_price * band_cost[nearest] / nearest
                quality = "extrapolation"
            else:
                nearest = sorted_bands[-1]
                points = (nearest,)
                ff_cost = purchase_price * band_cost[nearest] / nearest
                quality = "extrapolation"
            provenance["purchase_price_cny"] = _text(purchase_price)
            provenance["price_band_points"] = [_text(item) for item in points]
        elif purchase_price is not None and len(sorted_bands) == 1:
            only = sorted_bands[0]
            ff_cost = purchase_price * band_cost[only] / only
            quality = "extrapolation"
            provenance["single_band_ratio"] = _text(band_cost[only] / only)
        else:
            ff_cost = overall_cost
            quality = "fallback_average"
            provenance["missing_purchase_price"] = True
        if ff_cost <= ZERO:
            raise WarehouseFunctionalError(f"non-positive frozen FF cost for nmId {nm_id}")
        direct_downstream = downstream.get(nm_id, [])
        if direct_downstream:
            downstream_unit_cost = sum((qty * cost for qty, cost in direct_downstream), ZERO) / sum(
                (qty for qty, _ in direct_downstream), ZERO
            )
            wb_cost = ff_cost + downstream_unit_cost
            downstream_quality = "direct_confirmed_downstream"
        else:
            downstream_unit_cost = weighted_downstream_unit_cost
            wb_cost = ff_cost + downstream_unit_cost
            downstream_quality = "confirmed_weighted_downstream_unit_cost"
        if wb_cost <= ZERO:
            raise WarehouseFunctionalError(f"non-positive frozen WB cost for nmId {nm_id}")
        result[nm_id] = CostSeed(
            nm_id=nm_id,
            ff_unit_cost=ff_cost,
            wb_unit_cost=wb_cost,
            quality=quality,
            provenance={
                **provenance,
                "quality": quality,
                "downstream_quality": downstream_quality,
                "downstream_unit_cost_rub": _text(downstream_unit_cost),
                "frozen": True,
            },
        )
    return result


def build_historical_wb_cost_projection(
    *,
    opening_cost_map: Iterable[Mapping[str, Any]],
    daily_quantity_rows: Iterable[Mapping[str, Any]],
    downstream_rows: Iterable[Mapping[str, Any]],
    cutover_date: str,
) -> list[dict[str, Any]]:
    """Build 01.07..cutover daily WAC without inventing historical stock.

    Quantity is reused only from persisted daily snapshot evidence.  Cost is
    replaced by the frozen opening map and then rolled with confirmed accepted
    supply layers on their effective dates.
    """

    seeds = {
        int(item["nm_id"]): {
            "ff_wac": _decimal(item["ff_unit_cost_rub"]),
            "wac": _decimal(item["wb_unit_cost_rub"]),
            "quality": str(item["quality"]),
            "provenance": dict(item.get("provenance") or {}),
        }
        for item in opening_cost_map
        if int(item.get("nm_id") or 0) > 0
    }
    quantities: defaultdict[str, dict[int, Decimal]] = defaultdict(dict)
    for row in daily_quantity_rows:
        day = str(row.get("as_of_date") or "")[:10]
        nm_id = int(row.get("nm_id") or 0)
        quantity = _decimal(row.get("physical_quantity") if row.get("physical_quantity") is not None else row.get("stock_qty"))
        if "2026-07-01" <= day < cutover_date and nm_id > 0 and quantity >= ZERO:
            quantities[day][nm_id] = quantity
    downstream_rows = [dict(row) for row in downstream_rows]
    downstream_components = _supply_downstream_component_index(downstream_rows)
    inbounds: defaultdict[str, defaultdict[int, list[tuple[Decimal, Decimal, str]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in downstream_rows:
        day = str(row.get("accepted_date") or row.get("supply_date") or "")[:10]
        nm_id = int(row.get("nm_id") or 0)
        quantity = _decimal(row.get("quantity") or row.get("accepted_qty"))
        seed = seeds.get(nm_id)
        component = downstream_components.get((str(row.get("wb_supply_id") or ""), nm_id))
        cost = (
            _decimal(seed.get("ff_wac"))
            + component["pre_acceptance_addon"]
            + component["acceptance_addon"]
            if seed is not None and component is not None
            else ZERO
        )
        if "2026-07-01" <= day < cutover_date and nm_id > 0 and quantity > ZERO and cost > ZERO:
            inbounds[day][nm_id].append((quantity, cost, str(row.get("wb_supply_id") or "")))
    last_qty: defaultdict[int, Decimal] = defaultdict(Decimal)
    last_wac = {nm_id: dict(seed) for nm_id, seed in seeds.items()}
    result: list[dict[str, Any]] = []
    for day in sorted(quantities):
        for nm_id, quantity in sorted(quantities[day].items()):
            seed = last_wac.get(nm_id)
            if seed is None or _decimal(seed.get("wac")) <= ZERO:
                if quantity > ZERO:
                    raise WarehouseFunctionalError(f"historical WB quantity has no frozen cost for {day}:{nm_id}")
                continue
            previous_wac = _decimal(seed["wac"])
            previous_qty = last_qty[nm_id]
            inbound_rows = inbounds[day].get(nm_id, [])
            inbound_qty = sum((item[0] for item in inbound_rows), ZERO)
            inbound_capital = sum((item[0] * item[1] for item in inbound_rows), ZERO)
            if inbound_qty > ZERO:
                basis_qty = previous_qty if previous_qty > ZERO else max(quantity - inbound_qty, ZERO)
                basis_capital = basis_qty * previous_wac
                _, _, rolled = moving_weighted_average(
                    quantity=basis_qty,
                    capital=basis_capital,
                    inbound_quantity=inbound_qty,
                    inbound_capital=inbound_capital,
                )
                wac = rolled or previous_wac
                quality = "periodic_snapshot_wac"
            else:
                wac = previous_wac
                quality = str(seed["quality"])
            provenance = {
                "source": "persisted_historical_daily_quantity",
                "frozen_opening": seed.get("provenance") or {},
                "previous_snapshot_quantity": _text(previous_qty),
                "inbound_quantity": _text(inbound_qty),
                "inbound_supply_ids": [item[2] for item in inbound_rows],
                "last_valid_wac_retained": quantity == ZERO,
            }
            last_qty[nm_id] = quantity
            last_wac[nm_id] = {"wac": wac, "quality": quality, "provenance": provenance}
            item = {
                "as_of_date": day,
                "nm_id": nm_id,
                "quantity": _text(quantity),
                "wac_rub": _text(wac),
                "capital_rub": _text(quantity * wac),
                "quality": quality,
                "provenance": provenance,
            }
            item["fingerprint"] = "sha256:" + _hash(item)
            result.append(item)
    return result


class WarehouseFunctionalBlock:
    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        stocks_block: StocksBlock | None = None,
        timestamp_factory: Callable[[], str] | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.runtime = runtime
        self.runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.timestamp_factory = timestamp_factory or _now
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self.opening = WarehouseStocksBlock(
            runtime=runtime,
            stocks_block=stocks_block,
            timestamp_factory=self.timestamp_factory,
            now_factory=self.now_factory,
        )
        self.canonical_cost = CanonicalCostEngine(
            runtime=runtime,
            timestamp_factory=self.timestamp_factory,
        )
        self.calculation_parameters = CalculationParametersBlock(runtime=runtime)
        with _connect(self.runtime.db_path) as conn:
            ensure_warehouse_functional_schema(conn)
            conn.commit()

    def build_cutover_plan(self) -> dict[str, Any]:
        existing = self.readback()
        if existing.get("status") == "ready":
            return {
                "status": "already_applied",
                "idempotent": True,
                "cutover": existing["cutover"],
                "plan_fingerprint": existing["cutover"]["plan_fingerprint"],
            }
        return self._build_plan(kind="functional_cutover")

    def build_sync_plan(self, *, use_external_api: bool = True) -> dict[str, Any]:
        if not use_external_api:
            raise WarehouseFunctionalError("bounded WB sync requires a fresh official snapshot")
        if self.readback().get("status") != "ready":
            raise WarehouseFunctionalError("functional cutover must be applied before hourly sync")
        return self._build_plan(kind="hourly_wb_sync")

    def build_emergency_rebuild_plan(self) -> dict[str, Any]:
        """Rebuild from persisted sources only; never call an external API."""

        if self.readback().get("status") != "ready":
            raise WarehouseFunctionalError("functional cutover must be applied before emergency rebuild")
        return self._build_plan(kind="emergency_rebuild", wb_payload=self._last_good_wb_payload())

    def _build_plan(
        self,
        *,
        kind: str,
        wb_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        captured_at = self.timestamp_factory()
        if wb_payload is None:
            nomenclature = self.opening._opening_nomenclature_request()  # noqa: SLF001
            wb_payload = self.opening._fetch_wb_stock_snapshot(nomenclature)  # noqa: SLF001
        capture = self._capture_sources(captured_at=captured_at, wb_payload=wb_payload)
        ff_debit_coverage = (
            validate_cutover_ff_debit_coverage(capture) if kind == "functional_cutover" else None
        )
        base_active_version_id = self._active_version_id()
        previous = self._active_lines()
        cutover = self._cutover_row()
        lines, unmatched, events, opening_cost_map, movement_documents = self._calculate_lines(
            capture=capture,
            previous=previous,
            cutover=cutover,
            cutover_mode=kind == "functional_cutover",
        )
        projection_cutoff = (
            captured_at[:10]
            if kind == "functional_cutover"
            else str((cutover or {}).get("cutover_at") or captured_at)[:10]
        )
        pre_cutover_wb_cost_projection = build_historical_wb_cost_projection(
            opening_cost_map=opening_cost_map,
            daily_quantity_rows=capture["historical_wb_daily_quantities"],
            downstream_rows=capture["downstream_cost_rows"],
            cutover_date=projection_cutoff,
        )
        post_cutover_wb_cost_projection = self._build_post_cutover_daily_cost_projection(
            captured_at=captured_at,
            candidate_lines=lines,
            candidate_snapshot=capture["wb_snapshot"],
            new_events=events,
            opening_cost_map=opening_cost_map,
            cutover_mode=kind == "functional_cutover",
        )
        historical_wb_cost_projection = (
            pre_cutover_wb_cost_projection + post_cutover_wb_cost_projection
        )
        lines = _replace_current_wb_costs(
            lines,
            daily_projection=post_cutover_wb_cost_projection,
            current_date=captured_at[:10],
        )
        summaries = _summaries(lines)
        positive = [line for line in lines if line.quantity > ZERO]
        gaps = [line for line in positive if line.wac is None or line.wac <= ZERO or line.capital <= ZERO]
        negatives = [line for line in lines if min(line.quantity, line.capital) < ZERO]
        if gaps:
            raise WarehouseFunctionalError(
                "positive warehouse balances have no positive cost coverage: "
                + ",".join(f"{line.warehouse_key}:{line.nm_id}" for line in gaps)
            )
        if negatives:
            raise WarehouseFunctionalError("negative warehouse quantity or capital is forbidden")
        plan = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "dry_run_ready",
            "kind": kind,
            "cutover_id": FUNCTIONAL_CUTOVER_ID,
            "captured_at": captured_at,
            "effective_date": captured_at[:10],
            "base_active_version_id": base_active_version_id,
            "local_source_digest": capture["local_source_digest"],
            "wb_supply_source_digest": capture["wb_supply_source_digest"],
            "source_watermarks": capture["watermarks"],
            "absorbed_supply_revisions": capture["supply_revisions"] if kind == "functional_cutover" else {},
            "wb_snapshot": capture["wb_snapshot"],
            "opening_cost_map": opening_cost_map if kind == "functional_cutover" else [],
            "historical_wb_cost_projection": historical_wb_cost_projection,
            "lines": [_line_payload(line) for line in lines],
            "summaries": summaries,
            "unmatched_doprinato": unmatched,
            "new_events": events,
            "movement_documents": movement_documents,
            "diff": _balance_diff(previous, lines),
            "invariants": {
                "warehouse_count": len(STAGES),
                "negative_balance_count": len(negatives),
                "positive_cost_gap_count": len(gaps),
                "historical_wb_cost_gap_count": sum(
                    1
                    for item in historical_wb_cost_projection
                    if _decimal(item["quantity"]) > ZERO and _decimal(item["wac_rub"]) <= ZERO
                ),
                "wb_quantity_source": "official_snapshot_only",
                "discrepancy_opening_zero": (
                    summaries[STAGE_DISCREPANCY]["quantity"] == "0"
                    if kind == "functional_cutover" else None
                ),
                "ff_debit_coverage": ff_debit_coverage,
            },
        }
        plan["calculation_digest"] = _calculation_digest(plan)
        plan["plan_fingerprint"] = _fingerprint(plan)
        return plan

    def apply_plan(
        self,
        plan: Mapping[str, Any],
        *,
        confirm_fingerprint: str,
        backup_dir: Path | None = None,
    ) -> dict[str, Any]:
        normalized = _clone(plan)
        fingerprint = str(normalized.get("plan_fingerprint") or "")
        if not fingerprint or fingerprint != str(confirm_fingerprint or ""):
            raise WarehouseFunctionalError("exact reviewed plan fingerprint is required")
        if fingerprint != _fingerprint({key: value for key, value in normalized.items() if key != "plan_fingerprint"}):
            raise WarehouseFunctionalError("functional plan fingerprint mismatch")
        kind = str(normalized.get("kind") or "")
        existing = self._cutover_row()
        if kind == "functional_cutover" and existing is not None:
            if existing["plan_fingerprint"] != fingerprint:
                raise WarehouseFunctionalError("functional cutover already exists with another fingerprint")
            return {**self.readback(), "idempotent": True}
        if self._version_exists(fingerprint):
            return {**self.readback(), "idempotent": True}
        if kind != "functional_cutover" and self._active_version_id() != str(
            normalized.get("base_active_version_id") or ""
        ):
            raise WarehouseFunctionalError(
                "active functional warehouse version drifted after bounded calculation"
            )
        backup = None
        if kind == "functional_cutover":
            if backup_dir is None or not Path(backup_dir).is_absolute():
                raise WarehouseFunctionalError("absolute backup_dir is required for functional cutover")
            Path(backup_dir).mkdir(parents=True, exist_ok=True)
            destination = Path(backup_dir) / (
                f"{FUNCTIONAL_CUTOVER_ID}-{self.timestamp_factory().replace(':', '').replace('-', '')}.sqlite3"
            )
            backup = self.runtime.backup_database(destination)
            destination.chmod(0o600)
            if str(backup.get("integrity_check") or "").lower() != "ok":
                raise WarehouseFunctionalError("pre-cutover backup integrity_check is not ok")

        current_digest = self._local_source_digest()
        if current_digest != str(normalized.get("local_source_digest") or ""):
            raise WarehouseFunctionalError("local sources drifted after dry-run")
        if kind != "functional_cutover" and self._wb_supply_source_digest() != str(
            normalized.get("wb_supply_source_digest") or ""
        ):
            raise WarehouseFunctionalError("WB supply sources drifted after bounded capture")
        now = self.timestamp_factory()
        publication_effective_at = now if kind == "functional_cutover" else str(normalized["captured_at"])
        version_id = "whfv_" + fingerprint.removeprefix("sha256:")[:24]
        with _connect(self.runtime.db_path) as conn:
            ensure_warehouse_functional_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                duplicate = conn.execute(
                    "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_versions WHERE plan_fingerprint=?",
                    (fingerprint,),
                ).fetchone()
                if duplicate is not None:
                    conn.rollback()
                    return {**self.readback(), "idempotent": True}
                if kind != "functional_cutover" and self._active_version_id(connection=conn) != str(
                    normalized.get("base_active_version_id") or ""
                ):
                    raise WarehouseFunctionalError(
                        "active functional warehouse version drifted while acquiring apply lock"
                    )
                if self._local_source_digest(connection=conn) != current_digest:
                    raise WarehouseFunctionalError("local sources drifted while acquiring apply lock")
                if kind != "functional_cutover" and self._wb_supply_source_digest(connection=conn) != str(
                    normalized.get("wb_supply_source_digest") or ""
                ):
                    raise WarehouseFunctionalError("WB supply sources drifted while acquiring apply lock")
                if kind == "functional_cutover":
                    conn.execute(
                        """INSERT INTO sheet_vitrina_v1_warehouse_functional_cutovers(
                               cutover_id,cutover_at,status,plan_fingerprint,source_watermarks_json,
                               absorbed_supply_revisions_json,backup_json,created_at,updated_at
                           ) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            FUNCTIONAL_CUTOVER_ID,
                            publication_effective_at,
                            "posted",
                            fingerprint,
                            _json(normalized.get("source_watermarks") or {}),
                            _json(normalized.get("absorbed_supply_revisions") or {}),
                            _json(backup or {}),
                            now,
                            now,
                        ),
                    )
                    self.calculation_parameters.ensure_initial_version(
                        connection=conn,
                        created_at=now,
                    )
                    for item in normalized.get("opening_cost_map") or []:
                        conn.execute(
                            """INSERT INTO sheet_vitrina_v1_warehouse_opening_cost_map(
                                   cutover_id,nm_id,ff_unit_cost_rub,wb_unit_cost_rub,quality,
                                   provenance_json,fingerprint,created_at
                               ) VALUES(?,?,?,?,?,?,?,?)""",
                            (
                                FUNCTIONAL_CUTOVER_ID,
                                int(item["nm_id"]),
                                str(item["ff_unit_cost_rub"]),
                                str(item["wb_unit_cost_rub"]),
                                str(item["quality"]),
                                _json(item["provenance"]),
                                str(item["fingerprint"]),
                                now,
                            ),
                        )
                if kind != "functional_cutover":
                    cutover_date_row = conn.execute(
                        "SELECT cutover_at FROM sheet_vitrina_v1_warehouse_functional_cutovers WHERE cutover_id=?",
                        (FUNCTIONAL_CUTOVER_ID,),
                    ).fetchone()
                    if cutover_date_row is None:
                        raise WarehouseFunctionalError("functional daily replay has no cutover row")
                    conn.execute(
                        """DELETE FROM sheet_vitrina_v1_warehouse_wb_daily_cost
                           WHERE cutover_id=? AND as_of_date>=?""",
                        (FUNCTIONAL_CUTOVER_ID, str(cutover_date_row["cutover_at"])[:10]),
                    )
                for item in normalized.get("historical_wb_cost_projection") or []:
                    conn.execute(
                        """INSERT INTO sheet_vitrina_v1_warehouse_wb_daily_cost(
                               cutover_id,as_of_date,nm_id,quantity,wac_rub,capital_rub,
                               quality,provenance_json,fingerprint,created_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(cutover_id,as_of_date,nm_id) DO UPDATE SET
                               quantity=excluded.quantity,wac_rub=excluded.wac_rub,
                               capital_rub=excluded.capital_rub,quality=excluded.quality,
                               provenance_json=excluded.provenance_json,
                               fingerprint=excluded.fingerprint,created_at=excluded.created_at""",
                        (
                            FUNCTIONAL_CUTOVER_ID,
                            str(item["as_of_date"]),
                            int(item["nm_id"]),
                            str(item["quantity"]),
                            str(item["wac_rub"]),
                            str(item["capital_rub"]),
                            str(item["quality"]),
                            _json(item.get("provenance") or {}),
                            str(item["fingerprint"]),
                            now,
                        ),
                    )
                self._upsert_supplier_flows(conn, normalized.get("lines") or [], created_at=now)
                conn.execute(
                    """INSERT INTO sheet_vitrina_v1_warehouse_functional_versions(
                           version_id,cutover_id,version_kind,effective_at,status,plan_fingerprint,
                           local_source_digest,source_watermarks_json,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        version_id,
                        FUNCTIONAL_CUTOVER_ID,
                        kind,
                        publication_effective_at,
                        "good",
                        fingerprint,
                        current_digest,
                        _json(normalized.get("source_watermarks") or {}),
                        now,
                    ),
                )
                self._insert_snapshot(conn, version_id=version_id, payload=normalized["wb_snapshot"])
                for item in normalized["lines"]:
                    conn.execute(
                        """INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                               version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                               cost_covered_quantity,quality,certified,wb_quantity,
                               wb_in_way_to_client,wb_in_way_from_client,provenance_json
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            version_id,
                            item["warehouse_key"],
                            int(item["nm_id"]),
                            item["quantity"],
                            item["wac_rub"],
                            item["capital_rub"],
                            item["cost_covered_quantity"],
                            item["quality"],
                            int(bool(item["certified"])),
                            item["wb_quantity"],
                            item["wb_in_way_to_client"],
                            item["wb_in_way_from_client"],
                            _json(item["provenance"]),
                        ),
                    )
                for item in normalized.get("unmatched_doprinato") or []:
                    conn.execute(
                        """INSERT INTO sheet_vitrina_v1_warehouse_unmatched_doprinato(
                               unmatched_id,version_id,source_id,business_date,nm_id,quantity,
                               matched_quantity,reason,provenance_json,created_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (
                            _stable_id("unmatched", item),
                            version_id,
                            str(item.get("source_id") or ""),
                            str(item.get("business_date") or ""),
                            int(item["nm_id"]),
                            str(item["quantity"]),
                            str(item.get("matched_quantity") or "0"),
                            str(item.get("reason") or ""),
                            _json(item),
                            now,
                        ),
                    )
                for event in normalized.get("new_events") or []:
                    conn.execute(
                        """INSERT OR IGNORE INTO sheet_vitrina_v1_warehouse_functional_events(
                               event_id,version_id,event_type,source_id,source_fingerprint,
                               business_date,nm_id,quantity,capital_rub,provenance_json,created_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            str(event["event_id"]),
                            version_id,
                            event["event_type"],
                            event["source_id"],
                            event["source_fingerprint"],
                            event["business_date"],
                            int(event["nm_id"]),
                            event["quantity"],
                            event["capital_rub"],
                            _json(event.get("provenance") or {}),
                            now,
                        ),
                    )
                conn.execute(
                    """INSERT INTO sheet_vitrina_v1_warehouse_functional_active(slot,version_id,updated_at)
                       VALUES(1,?,?) ON CONFLICT(slot) DO UPDATE SET version_id=excluded.version_id,
                       updated_at=excluded.updated_at""",
                    (version_id, now),
                )
                if kind == "emergency_rebuild":
                    conn.execute(
                        """INSERT INTO sheet_vitrina_v1_warehouse_wb_sync_status(
                               slot,last_attempt_at,last_success_at,last_error,active_version_id,updated_at
                           ) VALUES(1,NULL,NULL,NULL,?,?) ON CONFLICT(slot) DO UPDATE SET
                               active_version_id=excluded.active_version_id,updated_at=excluded.updated_at""",
                        (version_id, now),
                    )
                else:
                    conn.execute(
                        """INSERT INTO sheet_vitrina_v1_warehouse_wb_sync_status(
                               slot,last_attempt_at,last_success_at,last_error,active_version_id,updated_at
                           ) VALUES(1,?,?,NULL,?,?) ON CONFLICT(slot) DO UPDATE SET
                               last_attempt_at=excluded.last_attempt_at,last_success_at=excluded.last_success_at,
                               last_error=NULL,active_version_id=excluded.active_version_id,updated_at=excluded.updated_at""",
                        (now, now, version_id, now),
                    )
                self._insert_documents(conn, version_id=version_id, plan=normalized, created_at=now)
                _verify_version(conn, version_id=version_id, expected=normalized)
                conn.execute(
                    """UPDATE sheet_vitrina_v1_warehouse_targeted_recalc_queue
                       SET status='complete',finished_at=?,error=NULL
                       WHERE status IN ('queued','running')""",
                    (now,),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {**self.readback(), "idempotent": False, "backup": backup}

    def rollback_functional_cutover(
        self,
        *,
        confirm_fingerprint: str,
        backup_dir: Path,
    ) -> dict[str, Any]:
        """Remove only derived functional state after exact confirmation.

        Primary supplier, CNY, FF and WB records are never touched.  A coherent
        pre-rollback backup is retained for recovery and audit.
        """

        cutover = self._cutover_row()
        if cutover is None:
            return {"status": "not_initialized", "idempotent": True}
        if str(cutover["plan_fingerprint"]) != str(confirm_fingerprint or ""):
            raise WarehouseFunctionalError("functional rollback fingerprint mismatch")
        destination_dir = Path(backup_dir)
        if not destination_dir.is_absolute():
            raise WarehouseFunctionalError("absolute backup_dir is required for rollback")
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / (
            f"{FUNCTIONAL_CUTOVER_ID}-rollback-{self.timestamp_factory().replace(':', '').replace('-', '')}.sqlite3"
        )
        backup = self.runtime.backup_database(destination)
        destination.chmod(0o600)
        if str(backup.get("integrity_check") or "").lower() != "ok":
            raise WarehouseFunctionalError("pre-rollback backup integrity_check is not ok")
        with _connect(self.runtime.db_path) as conn:
            ensure_warehouse_functional_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                version_ids = [
                    str(row[0])
                    for row in conn.execute(
                        "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_versions WHERE cutover_id=?",
                        (FUNCTIONAL_CUTOVER_ID,),
                    ).fetchall()
                ]
                for version_id in version_ids:
                    conn.execute(
                        "DELETE FROM sheet_vitrina_v1_warehouse_functional_document_lines WHERE version_id=?",
                        (version_id,),
                    )
                    conn.execute(
                        "DELETE FROM sheet_vitrina_v1_warehouse_functional_balances WHERE version_id=?",
                        (version_id,),
                    )
                    conn.execute(
                        "DELETE FROM sheet_vitrina_v1_warehouse_wb_snapshots WHERE version_id=?",
                        (version_id,),
                    )
                    conn.execute(
                        "DELETE FROM sheet_vitrina_v1_warehouse_unmatched_doprinato WHERE version_id=?",
                        (version_id,),
                    )
                    conn.execute(
                        "DELETE FROM sheet_vitrina_v1_warehouse_functional_events WHERE version_id=?",
                        (version_id,),
                    )
                    conn.execute(
                        "DELETE FROM sheet_vitrina_v1_warehouse_functional_documents WHERE version_id=?",
                        (version_id,),
                    )
                conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1")
                conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_wb_sync_status WHERE slot=1")
                conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue")
                conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_functional_versions WHERE cutover_id=?", (FUNCTIONAL_CUTOVER_ID,))
                conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_wb_daily_cost WHERE cutover_id=?", (FUNCTIONAL_CUTOVER_ID,))
                conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_opening_cost_map WHERE cutover_id=?", (FUNCTIONAL_CUTOVER_ID,))
                conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_supplier_flows")
                conn.execute("DELETE FROM sheet_vitrina_v1_warehouse_functional_cutovers WHERE cutover_id=?", (FUNCTIONAL_CUTOVER_ID,))
                conn.execute(
                    """DELETE FROM sheet_vitrina_v1_calculation_parameter_versions
                       WHERE version_id=? AND source='functional_cutover_initial_version'
                         AND NOT EXISTS(
                           SELECT 1 FROM sheet_vitrina_v1_calculation_parameter_versions
                           WHERE block_key='proxy_profit_margin' AND version_id<>?
                         )""",
                    ("calculation_parameters_proxy_v1_20260701", "calculation_parameters_proxy_v1_20260701"),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            "status": "rolled_back",
            "idempotent": False,
            "cutover_id": FUNCTIONAL_CUTOVER_ID,
            "plan_fingerprint": confirm_fingerprint,
            "backup": backup,
            "primary_sources_changed": False,
        }

    def record_failed_sync(self, error: Exception) -> None:
        now = self.timestamp_factory()
        with _connect(self.runtime.db_path) as conn:
            ensure_warehouse_functional_schema(conn)
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_warehouse_wb_sync_status(
                       slot,last_attempt_at,last_success_at,last_error,active_version_id,updated_at
                   ) VALUES(1,?,NULL,?,NULL,?) ON CONFLICT(slot) DO UPDATE SET
                       last_attempt_at=excluded.last_attempt_at,last_error=excluded.last_error,
                       updated_at=excluded.updated_at""",
                (now, str(error)[:2000], now),
            )
            conn.commit()

    def overview(self) -> dict[str, Any]:
        readback = self.readback()
        if readback.get("status") != "ready":
            return readback
        lines = readback["balances"]
        summaries = _summaries([_line_from_payload(item) for item in lines])
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "ready",
            "cutover": readback["cutover"],
            "active_version": readback["active_version"],
            "sync": readback["sync"],
            "warehouses": [
                {
                    "warehouse_key": key,
                    "warehouse_name": STAGE_NAMES[key],
                    **summaries[key],
                    "updated_at": readback["active_version"]["effective_at"],
                    "status": _summary_status(lines, key, readback["sync"]),
                }
                for key in STAGES
            ],
            "total": _total_summary(summaries),
        }

    def warehouse_detail(self, warehouse_key: str) -> dict[str, Any]:
        if warehouse_key not in STAGES:
            raise WarehouseFunctionalError(f"unknown warehouse: {warehouse_key}")
        readback = self.readback()
        if readback.get("status") != "ready":
            return readback
        balances = [item for item in readback["balances"] if item["warehouse_key"] == warehouse_key]
        summary = _summaries([_line_from_payload(item) for item in balances])[warehouse_key]
        names = self._nomenclature_names()
        documents = self._warehouse_documents(warehouse_key)
        public_balances = []
        for item in balances:
            nm_id = int(item["nm_id"])
            public_balances.append(
                {
                    **item,
                    "line_id": f"{item['version_id']}:{warehouse_key}:{nm_id}",
                    "sku": names.get(nm_id, {}).get("sku") or str(nm_id),
                    "nomenclature_name": names.get(nm_id, {}).get("name") or "",
                    "barcode": names.get(nm_id, {}).get("barcode") or "",
                    "average_unit_cost_rub": item.get("wac_rub"),
                    "warning": "" if bool(item.get("certified")) else f"provisional · {item.get('quality') or 'quality unknown'}",
                }
            )
        public_documents = []
        for item in documents:
            document_lines = []
            for line in item.get("lines") or []:
                nm_id = int(line["nm_id"])
                identity = names.get(nm_id, {})
                document_lines.append(
                    {
                        **line,
                        "sku": identity.get("sku") or str(nm_id),
                        "nomenclature_name": identity.get("name") or "",
                        "barcode": identity.get("barcode") or "",
                        "average_unit_cost_rub": line.get("wac_rub"),
                    }
                )
            document_type = str(item.get("document_type") or "")
            labels = {
                "functional_cutover": "Функциональный cutover",
                "warehouse_sync": "Почасовая версия склада",
                "wb_final_acceptance_discrepancy": "Расхождение финальной приёмки",
                "wb_doprinato": "Доприёмка WB",
                "wb_unmatched_doprinato_audit": "Неразнесённая доприёмка",
                "wb_pre_cutover_unmatched_audit": "Доприёмка до границы учёта",
            }
            directions = {
                "wb_final_acceptance_discrepancy": (STAGE_FF_TO_WB, STAGE_DISCREPANCY),
                "wb_doprinato": (STAGE_DISCREPANCY, STAGE_WB),
                "wb_unmatched_doprinato_audit": ("transitional_audit", "non_stock"),
                "wb_pre_cutover_unmatched_audit": ("pre_cutover_audit", "non_stock"),
            }
            warehouse_from, warehouse_to = directions.get(document_type, ("source", warehouse_key))
            quantity = _decimal(item.get("quantity"))
            capital = _decimal(item.get("capital_rub"))
            public_documents.append(
                {
                    **item,
                    "document_number": str(item.get("document_id") or ""),
                    "document_type_label": labels.get(document_type, document_type),
                    "warehouse_name": STAGE_NAMES[warehouse_key],
                    "warehouse_from_key": warehouse_from,
                    "warehouse_to_key": warehouse_to,
                    "source_basis": str(item.get("source_id") or ""),
                    "sku_count": len(document_lines),
                    "total_quantity": _text(quantity),
                    "total_cost_rub": _text(capital / quantity) if quantity != ZERO else None,
                    "total_capital_rub": _text(capital),
                    "status_label": "Аудит · не склад" if document_type in {"wb_unmatched_doprinato_audit", "wb_pre_cutover_unmatched_audit"} else "Проведено",
                    "lines": document_lines,
                }
            )
        status = _summary_status(balances, warehouse_key, readback["sync"])
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "ready",
            "cutover": readback["cutover"],
            "active_version": readback["active_version"],
            "sync": readback["sync"],
            "warehouse": {
                "warehouse_key": warehouse_key,
                "warehouse_name": STAGE_NAMES[warehouse_key],
                **summary,
                "sku_count": summary["sku_count"],
                "total_quantity": summary["quantity"],
                "total_capital_rub": summary["capital_rub"],
                "average_unit_cost_rub": summary["wac_rub"],
                "updated_at": readback["active_version"]["effective_at"],
                "source_basis": "canonical functional warehouse projection",
                "status": status,
                "status_label": status.replace("stale_error", "Ошибка последней синхронизации · last good сохранён").replace("certified", "Сертифицировано").replace("provisional", "Рассчитано · provisional"),
                "wb_contour": {
                    "quantity": summary["wb_quantity"],
                    "in_way_to_client": summary["wb_in_way_to_client"],
                    "in_way_from_client": summary["wb_in_way_from_client"],
                    "total": summary["quantity"],
                } if warehouse_key == STAGE_WB else None,
            },
            "balances": public_balances,
            "documents": public_documents,
            "unmatched_doprinato": readback["unmatched_doprinato"] if warehouse_key == STAGE_DISCREPANCY else [],
            "document_type_catalog": [
                {"key": "wb_final_acceptance_discrepancy", "label": "Расхождение финальной приёмки", "enabled": True},
                {"key": "wb_doprinato", "label": "Доприёмка WB", "enabled": True},
                {"key": "wb_unmatched_doprinato_audit", "label": "Неразнесённая доприёмка", "enabled": True},
                {"key": "wb_pre_cutover_unmatched_audit", "label": "Доприёмка до границы учёта", "enabled": True},
                {"key": "wb_discrepancy_writeoff", "label": "Списание расхождения", "enabled": False},
            ] if warehouse_key == STAGE_DISCREPANCY else [],
            "legacy_ff_route": "/v1/sheet-vitrina-v1/supply/ff-stocks" if warehouse_key == STAGE_FF else None,
        }

    def _nomenclature_names(self) -> dict[int, dict[str, str]]:
        try:
            state = self.runtime.load_current_state()
        except Exception:
            return {}
        result: dict[int, dict[str, str]] = {}
        for item in state.config_v2:
            nm_id = int(item.nm_id)
            result[nm_id] = {
                "sku": str(getattr(item, "sku", "") or getattr(item, "display_name", "") or nm_id),
                "name": str(getattr(item, "display_name", "") or ""),
                "barcode": str(getattr(item, "barcode", "") or ""),
            }
        return result

    def _warehouse_documents(self, warehouse_key: str) -> list[dict[str, Any]]:
        with _connect(self.runtime.db_path) as conn:
            ensure_warehouse_functional_schema(conn)
            rows = conn.execute(
                """SELECT document.*
                   FROM sheet_vitrina_v1_warehouse_functional_documents document
                   JOIN sheet_vitrina_v1_warehouse_functional_versions version
                     ON version.version_id=document.version_id
                   WHERE version.cutover_id=? AND document.warehouse_key=?
                   ORDER BY document.occurred_at DESC,document.created_at DESC,document.document_id
                   LIMIT 200""",
                (FUNCTIONAL_CUTOVER_ID, warehouse_key),
            ).fetchall()
            result = []
            for row in rows:
                item = _document_public(row)
                line_rows = conn.execute(
                    """SELECT * FROM sheet_vitrina_v1_warehouse_functional_document_lines
                       WHERE document_id=? ORDER BY nm_id,line_id""",
                    (row["document_id"],),
                ).fetchall()
                item["lines"] = [
                    {**dict(line), "provenance": _loads(line["provenance_json"], {})}
                    for line in line_rows
                ]
                result.append(item)
        return result

    def readback(self) -> dict[str, Any]:
        with _connect(self.runtime.db_path) as conn:
            ensure_warehouse_functional_schema(conn)
            cutover_row = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_functional_cutovers WHERE cutover_id=?",
                (FUNCTIONAL_CUTOVER_ID,),
            ).fetchone()
            active = conn.execute(
                """SELECT version.* FROM sheet_vitrina_v1_warehouse_functional_active active
                   JOIN sheet_vitrina_v1_warehouse_functional_versions version
                     ON version.version_id=active.version_id WHERE active.slot=1"""
            ).fetchone()
            if cutover_row is None or active is None:
                return {
                    "contract_name": CONTRACT_NAME,
                    "contract_version": CONTRACT_VERSION,
                    "status": "not_initialized",
                    "cutover": None,
                    "balances": [],
                    "documents": [],
                }
            balances = [dict(row) for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances WHERE version_id=? ORDER BY warehouse_key,nm_id",
                (active["version_id"],),
            ).fetchall()]
            documents = [dict(row) for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_functional_documents WHERE version_id=? ORDER BY occurred_at,document_id",
                (active["version_id"],),
            ).fetchall()]
            unmatched = [dict(row) for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_unmatched_doprinato WHERE version_id=? ORDER BY business_date,unmatched_id",
                (active["version_id"],),
            ).fetchall()]
            historical_cost = conn.execute(
                """SELECT MIN(as_of_date) date_from,MAX(as_of_date) date_to,
                          COUNT(DISTINCT as_of_date) day_count,COUNT(*) row_count,
                          SUM(CASE WHEN CAST(quantity AS NUMERIC)>0 AND CAST(wac_rub AS NUMERIC)<=0 THEN 1 ELSE 0 END) gap_count
                   FROM sheet_vitrina_v1_warehouse_wb_daily_cost WHERE cutover_id=?""",
                (FUNCTIONAL_CUTOVER_ID,),
            ).fetchone()
            cutover_version = conn.execute(
                """SELECT version_id,effective_at FROM sheet_vitrina_v1_warehouse_functional_versions
                   WHERE cutover_id=? AND version_kind='functional_cutover'
                   ORDER BY created_at LIMIT 1""",
                (FUNCTIONAL_CUTOVER_ID,),
            ).fetchone()
            cutover_discrepancy_rows = (
                conn.execute(
                    """SELECT quantity,capital_rub FROM sheet_vitrina_v1_warehouse_functional_balances
                       WHERE version_id=? AND warehouse_key=?""",
                    (cutover_version["version_id"], STAGE_DISCREPANCY),
                ).fetchall()
                if cutover_version is not None
                else []
            )
            sync = conn.execute("SELECT * FROM sheet_vitrina_v1_warehouse_wb_sync_status WHERE slot=1").fetchone()
        public_balances = [_balance_public(item) for item in balances]
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "ready",
            "cutover": _cutover_public(cutover_row),
            "active_version": _version_public(active),
            "sync": dict(sync) if sync else {},
            "balances": public_balances,
            "documents": [_document_public(item) for item in documents],
            "unmatched_doprinato": [_unmatched_public(item) for item in unmatched],
            "historical_wb_cost_projection": dict(historical_cost) if historical_cost else {},
            "cutover_opening_discrepancy": {
                "quantity": _text(sum((_decimal(row["quantity"]) for row in cutover_discrepancy_rows), ZERO)),
                "capital_rub": _text(sum((_decimal(row["capital_rub"]) for row in cutover_discrepancy_rows), ZERO)),
                "version_id": str(cutover_version["version_id"]) if cutover_version is not None else "",
                "effective_at": str(cutover_version["effective_at"]) if cutover_version is not None else "",
            },
            "reconciliation": {
                "warehouse_count": len(STAGES),
                "negative_balance_count": sum(
                    1 for item in public_balances if _decimal(item["quantity"]) < ZERO
                ),
                "positive_cost_gap_count": sum(
                    1
                    for item in public_balances
                    if _decimal(item["quantity"]) > ZERO
                    and (_decimal(item["capital_rub"]) <= ZERO or _optional_decimal(item["wac_rub"]) is None)
                ),
            },
        }

    def _capture_sources(self, *, captured_at: str, wb_payload: Mapping[str, Any]) -> dict[str, Any]:
        with _connect(self.runtime.db_path) as conn:
            ensure_warehouse_functional_schema(conn)
            conn.execute("BEGIN")
            sources = _source_rows(conn)
            conn.commit()
        local_digest = "sha256:" + _hash(_guarded_local_sources(sources))
        wb_data = dict(wb_payload.get("data") or {})
        raw_rows = list(wb_data.get("raw_rows") or wb_data.get("rows") or [])
        wb_items = list(wb_payload.get("canonical_items") or [])
        wb_snapshot = {
            "snapshot_id": "wbsnap_" + _hash(
                {
                    "fetched_at": wb_data.get("fetched_at"),
                    "requested_nm_ids": wb_payload.get("requested_nm_ids"),
                    "rows_digest": wb_data.get("raw_rows_digest"),
                }
            )[:24],
            "fetched_at": str(wb_data.get("fetched_at") or captured_at),
            "snapshot_date": str(wb_payload.get("snapshot_date") or captured_at[:10]),
            "requested_nm_ids": list(wb_payload.get("requested_nm_ids") or []),
            "pagination_complete": bool(wb_data.get("pagination_complete")),
            "page_count": int(wb_data.get("page_count") or 0),
            "page_offsets": list(wb_data.get("page_offsets") or []),
            "raw_row_count": len(raw_rows),
            "raw_rows_digest": str(wb_data.get("raw_rows_digest") or ("sha256:" + _hash(raw_rows))),
            "raw_rows": raw_rows,
            "items": wb_items,
        }
        if not wb_snapshot["pagination_complete"]:
            raise WarehouseFunctionalError("official WB snapshot pagination is incomplete")
        supply_revisions = _supply_revisions(sources["wb_supplies"])
        return {
            **sources,
            "captured_at": captured_at,
            "local_source_digest": local_digest,
            "wb_supply_source_digest": "sha256:" + _hash(supply_revisions),
            "supply_revisions": supply_revisions,
            "wb_snapshot": wb_snapshot,
            "watermarks": {
                "captured_at": captured_at,
                "local_source_digest": local_digest,
                "supplier_shipments": _watermark(sources["shipments"], "updated_at"),
                "cny_ledger": _watermark(sources["cny_operations"], "updated_at"),
                "financial_documents": _watermark(sources["financial_documents"], "updated_at"),
                "nomenclature_purchase_prices": _watermark(
                    sources["nomenclature_purchase_prices"], "updated_at"
                ),
                "fulfillment_service_uploads": _watermark(
                    sources["fulfillment_service_uploads"], "updated_at"
                ),
                "ff_ledger": _watermark(sources["ff_operations"], "created_at"),
                "ff_auto_writeoff_checkpoint": _watermark(
                    sources["ff_auto_writeoff_checkpoint"], "created_at"
                ),
                "wb_supplies": _watermark(sources["wb_supplies"], "last_list_synced_at", "synced_at"),
                "wb_snapshot": {
                    "snapshot_id": wb_snapshot["snapshot_id"],
                    "fetched_at": wb_snapshot["fetched_at"],
                    "requested_count": len(wb_snapshot["requested_nm_ids"]),
                    "raw_row_count": wb_snapshot["raw_row_count"],
                    "digest": wb_snapshot["raw_rows_digest"],
                    "pagination_complete": True,
                },
            },
        }

    def _calculate_lines(
        self,
        *,
        capture: Mapping[str, Any],
        previous: Mapping[tuple[str, int], WarehouseLine],
        cutover: Mapping[str, Any] | None,
        cutover_mode: bool,
    ) -> tuple[
        list[WarehouseLine],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        captured_at = str(capture["captured_at"])
        wb_items = {int(item["nm_id"]): dict(item) for item in capture["wb_snapshot"]["items"]}
        target_nm_ids = set(wb_items)
        target_nm_ids.update(int(row["nm_id"]) for row in capture["primary_cost_rows"] if int(row["nm_id"] or 0) > 0)
        target_nm_ids.update(int(row["nm_id"]) for row in capture["ff_lines"] if int(row["nm_id"] or 0) > 0)
        target_nm_ids.update(
            int(row["nm_id"])
            for row in capture["historical_wb_daily_quantities"]
            if int(row.get("nm_id") or 0) > 0
        )
        for raw_supply in capture["wb_supplies"]:
            record = _normalized_wb_record(raw_supply)
            for good in _validated_wb_goods(record):
                target_nm_ids.add(int(good["nm_id"]))
        purchase_price = _nomenclature_purchase_prices(capture["nomenclature_purchase_prices"])
        if cutover_mode:
            cost_map = build_frozen_opening_cost_map(
                target_nm_ids=target_nm_ids,
                primary_rows=capture["primary_cost_rows"],
                purchase_price_by_nm=purchase_price,
                downstream_rows=capture["downstream_cost_rows"],
                primary_identity=capture["primary_identity"],
            )
        else:
            cost_map = self._load_opening_cost_map()

        buckets: defaultdict[tuple[str, int], dict[str, Any]] = defaultdict(
            lambda: {"quantity": ZERO, "capital": ZERO, "covered": ZERO, "quality": [], "provenance": []}
        )
        shipment_by_id = {str(row["shipment_id"]): row for row in capture["shipments"]}
        shipment_lines: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in capture["shipment_lines"]:
            if str(row.get("line_type") or "") == "product" and int(row.get("internal_nm_id") or 0) > 0:
                shipment_lines[str(row["shipment_id"])].append(dict(row))
        payments: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        transfer_fees: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in capture["cny_operations"]:
            if not _counted_cny_operation(row):
                continue
            operation_type = str(row.get("operation_type") or "")
            if operation_type == "supplier_payment_out":
                payments[str(row.get("source_order_id") or "")].append(dict(row))
            elif operation_type == "transfer_fee":
                transfer_fees[str(row.get("source_order_id") or "")].append(dict(row))
        docs = {str(row["document_id"]): row for row in capture["financial_documents"]}
        expense_lines: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in capture["financial_expense_lines"]:
            expense_lines[str(row["supplier_order_id"])].append(dict(row))
        supplier_flow_costs: dict[tuple[str, int], tuple[Decimal, Decimal, str, dict[str, Any]]] = {}

        for shipment_id, shipment in shipment_by_id.items():
            if str(shipment.get("order_status") or "").lower() in INACTIVE_SUPPLIER_STATUSES:
                continue
            lines = shipment_lines.get(shipment_id, [])
            if not lines or not payments.get(shipment_id):
                continue
            stage = STAGE_CHINA_TO_FF if str(shipment.get("actual_shipment_date") or "")[:10] else STAGE_PRODUCTION
            payment_capital = sum((abs(_decimal(row.get("rub_value_delta"))) for row in payments[shipment_id]), ZERO)
            cny_fee_capital = sum((abs(_decimal(row.get("rub_value_delta"))) for row in transfer_fees[shipment_id]), ZERO)
            direct_rub_fees = ZERO
            china_pools: list[tuple[Decimal, str, str]] = []
            for expense in expense_lines.get(shipment_id, []):
                document = docs.get(str(expense.get("financial_document_id") or ""), {})
                if not _validated_financial_expense(document=document, expense=expense):
                    continue
                doc_type = str(document.get("document_type") or "")
                currency = str(expense.get("currency") or "").upper()
                amount = _decimal(expense.get("amount_rub"))
                category = str(expense.get("category") or "")
                if doc_type == "bank_fee_statement" and category in BANK_FEE_CATEGORIES and currency == "RUB":
                    direct_rub_fees += amount
                elif stage == STAGE_CHINA_TO_FF and doc_type == LOGISTICS_DOCUMENT_TYPE and amount > ZERO:
                    china_pools.append((amount, "quantity", f"{document.get('document_id')}:{expense.get('line_id')}"))
                elif stage == STAGE_CHINA_TO_FF and doc_type == CUSTOMS_DOCUMENT_TYPE and amount > ZERO:
                    if category in CUSTOMS_BY_QUANTITY:
                        china_pools.append((amount, "quantity", f"{document.get('document_id')}:{expense.get('line_id')}"))
                    elif category in CUSTOMS_BY_VALUE:
                        china_pools.append((amount, "invoice_value", f"{document.get('document_id')}:{expense.get('line_id')}"))
            production_total = payment_capital + cny_fee_capital + direct_rub_fees
            allocation_lines = [
                {
                    "nm_id": int(row["internal_nm_id"]),
                    "quantity": _decimal(row.get("qty")),
                    "invoice_value": _line_value(row),
                }
                for row in lines
            ]
            capital_by_nm = allocate_capital(allocation_lines, total_capital=production_total, method="invoice_value")
            china_by_nm: defaultdict[int, Decimal] = defaultdict(Decimal)
            china_sources: list[str] = []
            for total, method, source_id in china_pools:
                for nm_id, allocated in allocate_capital(allocation_lines, total_capital=total, method=method).items():
                    china_by_nm[nm_id] += allocated
                china_sources.append(source_id)
            quantity_by_nm: defaultdict[int, Decimal] = defaultdict(Decimal)
            for row in allocation_lines:
                quantity_by_nm[int(row["nm_id"])] += _decimal(row["quantity"])
            flow_id = _supplier_flow_id(shipment_id)
            for nm_id, quantity in quantity_by_nm.items():
                capital = capital_by_nm.get(nm_id, ZERO) + china_by_nm.get(nm_id, ZERO)
                if quantity <= ZERO or capital <= ZERO:
                    raise WarehouseFunctionalError(f"activated supplier flow {flow_id} has incomplete capital")
                quality = (
                    "certified"
                    if bool(shipment.get("expenses_complete"))
                    else "confirmed_payments_provisional_expenses"
                )
                flow_provenance = {
                    "supplier_flow_id": flow_id,
                    "shipment_id": shipment_id,
                    "invoice_no": str(shipment.get("invoice_no") or ""),
                    "flow_quantity": _text(quantity),
                    "flow_capital_rub": _text(capital),
                    "expenses_complete_certification": bool(shipment.get("expenses_complete")),
                    "payment_operation_ids": [str(row["operation_id"]) for row in payments[shipment_id]],
                    "cny_fee_operation_ids": [str(row["operation_id"]) for row in transfer_fees[shipment_id]],
                    "direct_rub_bank_fees": _text(direct_rub_fees),
                    "china_expense_sources": china_sources,
                    "allocation": "supplier/payment/bank fee by invoice value; logistics/1010 by quantity; 2010/5010 by invoice value",
                }
                supplier_flow_costs[(shipment_id, nm_id)] = (quantity, capital, quality, flow_provenance)
                if str(shipment.get("actual_ff_acceptance_date") or "")[:10]:
                    continue
                _add_bucket(
                    buckets,
                    stage=stage,
                    nm_id=nm_id,
                    quantity=quantity,
                    capital=capital,
                    covered=quantity,
                    quality=quality,
                    provenance=flow_provenance,
                )

        ff_qty: defaultdict[int, Decimal] = defaultdict(Decimal)
        for row in capture["ff_lines"]:
            ff_qty[int(row["nm_id"])] += _decimal(row.get("quantity_delta"))
        ff_outbound_wac_by_supply_nm: dict[tuple[str, int], Decimal] = {}
        if cutover_mode:
            for nm_id, quantity in ff_qty.items():
                if quantity < ZERO:
                    raise WarehouseFunctionalError(f"canonical FF ledger is negative for nmId {nm_id}")
                if quantity == ZERO:
                    continue
                seed = cost_map[nm_id]
                _add_bucket(
                    buckets,
                    stage=STAGE_FF,
                    nm_id=nm_id,
                    quantity=quantity,
                    capital=quantity * seed.ff_unit_cost,
                    covered=quantity,
                    quality=seed.quality,
                    provenance={"source": "canonical_append_only_ff_ledger_cutover_opening", **dict(seed.provenance)},
                )
        else:
            ff_pools: dict[int, dict[str, Any]] = {
                nm_id: {
                    "quantity": line.quantity,
                    "capital": line.capital,
                    "operations": [],
                    "opening_version_id": line.provenance.get("version_id") or "",
                }
                for nm_id, line in self._cutover_stage_lines(STAGE_FF).items()
            }
            ff_lines_by_operation: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
            for row in capture["ff_lines"]:
                ff_lines_by_operation[str(row.get("operation_id") or "")].append(row)
            boundary = str((cutover or {}).get("cutover_at") or "")
            for operation in capture["ff_operations"]:
                if str(operation.get("created_at") or "") <= boundary:
                    continue
                operation_id = str(operation.get("operation_id") or "")
                source_type = str(operation.get("source_type") or "")
                source_object_id = str(operation.get("source_object_id") or "")
                for raw_line in ff_lines_by_operation.get(operation_id, []):
                    nm_id = int(raw_line.get("nm_id") or 0)
                    delta = _decimal(raw_line.get("quantity_delta"))
                    if nm_id <= 0 or delta == ZERO:
                        continue
                    pool = ff_pools.setdefault(
                        nm_id,
                        {"quantity": ZERO, "capital": ZERO, "operations": [], "opening_version_id": ""},
                    )
                    current_qty = _decimal(pool["quantity"])
                    current_capital = _decimal(pool["capital"])
                    current_wac = current_capital / current_qty if current_qty > ZERO else None
                    if delta > ZERO:
                        if source_type == "supplier_shipment":
                            flow = supplier_flow_costs.get((source_object_id, nm_id))
                            if flow is None:
                                raise WarehouseFunctionalError(
                                    f"FF supplier receipt {operation_id}:{nm_id} has no exact supplier-flow capital"
                                )
                            flow_qty, flow_capital, _quality, flow_provenance = flow
                            inbound_wac = flow_capital / flow_qty
                            inbound_provenance = flow_provenance
                        else:
                            if current_wac is None:
                                seed = cost_map.get(nm_id)
                                if seed is None:
                                    raise WarehouseFunctionalError(
                                        f"positive FF adjustment {operation_id}:{nm_id} has no prior or source cost"
                                    )
                                current_wac = seed.ff_unit_cost
                            inbound_wac = current_wac
                            inbound_provenance = {
                                "quality": "current_wac_adjustment",
                                "reason": "non_supplier_positive_FF_ledger_operation",
                            }
                        pool["quantity"] = current_qty + delta
                        pool["capital"] = current_capital + delta * inbound_wac
                    else:
                        if current_wac is None:
                            raise WarehouseFunctionalError(
                                f"FF outbound {operation_id}:{nm_id} has no positive cost pool"
                            )
                        outbound = abs(delta)
                        if outbound > current_qty:
                            raise WarehouseFunctionalError(
                                f"canonical FF replay would be negative for nmId {nm_id} at {operation_id}"
                            )
                        pool["quantity"] = current_qty - outbound
                        pool["capital"] = current_capital - outbound * current_wac
                        inbound_wac = current_wac
                        inbound_provenance = {"quality": "proportional_wac_outbound"}
                        if source_type in {"wb_supply", "wb_supply_targeted_reconciliation"} and source_object_id:
                            ff_outbound_wac_by_supply_nm[(source_object_id, nm_id)] = current_wac
                    pool["operations"].append(
                        {
                            "operation_id": operation_id,
                            "created_at": operation.get("created_at"),
                            "source_type": source_type,
                            "source_object_id": source_object_id,
                            "quantity_delta": _text(delta),
                            "unit_cost_rub": _text(inbound_wac),
                            "source": inbound_provenance,
                        }
                    )
            for nm_id, expected_quantity in ff_qty.items():
                actual_quantity = _decimal((ff_pools.get(nm_id) or {}).get("quantity"))
                if expected_quantity < ZERO or actual_quantity != expected_quantity:
                    raise WarehouseFunctionalError(
                        f"canonical FF replay mismatch for nmId {nm_id}: {actual_quantity} != {expected_quantity}"
                    )
            for nm_id, pool in ff_pools.items():
                quantity = _decimal(pool["quantity"])
                capital = _decimal(pool["capital"])
                if quantity == ZERO:
                    continue
                if capital <= ZERO:
                    raise WarehouseFunctionalError(f"FF replay has no capital for nmId {nm_id}")
                _add_bucket(
                    buckets,
                    stage=STAGE_FF,
                    nm_id=nm_id,
                    quantity=quantity,
                    capital=capital,
                    covered=quantity,
                    quality="moving_weighted_average",
                    provenance={
                        "source": "canonical_append_only_ff_ledger_replay",
                        "cutover_opening": True,
                        "operations": pool["operations"],
                    },
                )

        downstream_components = _supply_downstream_component_index(capture["downstream_cost_rows"])
        cutover_revisions = dict((cutover or {}).get("absorbed_supply_revisions") or {})
        discrepancy_receipts: list[dict[str, Any]] = []
        doprinato_rows: list[dict[str, Any]] = []
        transitional_unmatched: list[dict[str, Any]] = []
        new_events: list[dict[str, Any]] = []
        accepted_event_totals = self._accepted_event_totals()
        for raw in capture["wb_supplies"]:
            record = _normalized_wb_record(raw)
            supply_id = str(record.get("supply_id") or "")
            revision = _supply_revision(raw)
            absorbed = cutover_revisions.get(supply_id) == revision
            status_id = int(record.get("status_id") or 0)
            is_doprinato = _is_doprinato(record)
            for good in _validated_wb_goods(record):
                nm_id = int(good["nm_id"])
                packed = _decimal(good.get("quantity"))
                accepted = _decimal(good.get("accepted_quantity"))
                wb_supply_id = str(record.get("wb_supply_id") or "")
                provenance = {
                    "supply_id": supply_id,
                    "wb_supply_id": str(record.get("wb_supply_id") or ""),
                    "source_revision": revision,
                    "status_id": status_id,
                    "packed_quantity": _text(packed),
                    "accepted_quantity": _text(accepted),
                }
                business_date = _supply_business_date(record, raw)
                before_boundary = bool(
                    cutover
                    and business_date
                    and business_date < str(cutover["cutover_at"])[:10]
                )
                needs_supply_cost = bool(
                    not is_doprinato
                    and (
                        status_id in WB_POST_SHIPMENT_GATE_STATUS_IDS
                        or (
                            status_id == WB_FINAL_ACCEPTED_STATUS_ID
                            and not cutover_mode
                            and not absorbed
                            and not before_boundary
                        )
                    )
                )
                accepted_cost = ZERO
                pre_acceptance_cost = ZERO
                if needs_supply_cost:
                    component = downstream_components.get((wb_supply_id, nm_id))
                    if component is None:
                        component = downstream_components.get((supply_id, nm_id))
                    if component is None:
                        raise WarehouseFunctionalError(
                            f"WB supply {supply_id}:{nm_id} has no validated downstream cost state"
                        )
                    outbound_ff_wac = ff_outbound_wac_by_supply_nm.get((supply_id, nm_id))
                    if outbound_ff_wac is None:
                        outbound_ff_wac = ff_outbound_wac_by_supply_nm.get((wb_supply_id, nm_id))
                    if outbound_ff_wac is None and (cutover_mode or absorbed):
                        seed = cost_map.get(nm_id)
                        outbound_ff_wac = seed.ff_unit_cost if seed is not None else None
                    if outbound_ff_wac is None or outbound_ff_wac <= ZERO:
                        raise WarehouseFunctionalError(
                            f"WB supply {supply_id}:{nm_id} has no FF WAC at ledger debit"
                        )
                    pre_acceptance_cost, accepted_cost = compose_supply_costs(
                        outbound_ff_wac=outbound_ff_wac,
                        pre_acceptance_addon=component["pre_acceptance_addon"],
                        acceptance_addon=component["acceptance_addon"],
                    )
                    provenance.update(
                        {
                            "ff_wac_at_ledger_debit_rub": _text(outbound_ff_wac),
                            "downstream_pre_acceptance_addon_rub": _text(component["pre_acceptance_addon"]),
                            "wb_paid_acceptance_addon_rub": _text(component["acceptance_addon"]),
                            "downstream_cost_layer_fingerprint": component["inputs_hash"],
                        }
                    )
                source_fingerprint = _hash(
                    {
                        "revision": revision,
                        "nm_id": nm_id,
                        "good": good,
                        "accepted_unit_cost_rub": _text(accepted_cost),
                        "pre_acceptance_unit_cost_rub": _text(pre_acceptance_cost),
                    }
                )
                source_id = f"{supply_id}:{nm_id}"
                if not cutover_mode and not absorbed and before_boundary:
                    audit_quantity = (
                        accepted if is_doprinato and accepted > ZERO
                        else packed if is_doprinato
                        else max(packed - accepted, ZERO)
                    )
                    if audit_quantity > ZERO:
                        transitional_unmatched.append(
                            {
                                "source_id": source_id,
                                "source_fingerprint": source_fingerprint,
                                "business_date": business_date,
                                "nm_id": nm_id,
                                "quantity": _text(audit_quantity),
                                "matched_quantity": "0",
                                "reason": "pre_cutover_business_state_discovered_late",
                                "provenance": {
                                    **provenance,
                                    "non_stock_audit": True,
                                    "source_kind": "doprinato" if is_doprinato else "final_acceptance_discrepancy",
                                },
                            }
                        )
                    continue
                if status_id in WB_POST_SHIPMENT_GATE_STATUS_IDS and not is_doprinato:
                    open_qty = max(packed - accepted, ZERO)
                    if open_qty > ZERO:
                        _add_bucket(
                            buckets,
                            stage=STAGE_FF_TO_WB,
                            nm_id=nm_id,
                            quantity=open_qty,
                            capital=open_qty * pre_acceptance_cost,
                            covered=open_qty,
                            quality="supply_specific_downstream_cost",
                            provenance={**provenance, "formula": "max(packed-accepted,0)"},
                        )
                    continue
                if cutover_mode or absorbed:
                    continue
                if is_doprinato:
                    quantity = accepted if accepted > ZERO else packed
                    doprinato_rows.append(
                        {
                            "source_id": source_id,
                            "source_fingerprint": source_fingerprint,
                            "business_date": business_date,
                            "nm_id": nm_id,
                            "quantity": _text(quantity),
                            "reason": "pre_cutover_business_state_discovered_late" if before_boundary else "",
                            "provenance": provenance,
                        }
                    )
                elif status_id == WB_FINAL_ACCEPTED_STATUS_ID:
                    quantity = max(packed - accepted, ZERO)
                    if quantity > ZERO:
                        discrepancy_receipts.append(
                            {
                                "source_id": source_id,
                                "source_fingerprint": source_fingerprint,
                                "business_date": business_date,
                                "nm_id": nm_id,
                                "quantity": _text(quantity),
                                "capital": _text(quantity * pre_acceptance_cost),
                                "wac": _text(pre_acceptance_cost),
                                "provenance": {**provenance, "paid_acceptance_excluded": True},
                            }
                        )
                    previous_event = accepted_event_totals.get(
                        (source_id, nm_id),
                        {"quantity": ZERO, "capital": ZERO},
                    )
                    previously_posted = _decimal(previous_event["quantity"])
                    previously_posted_capital = _decimal(previous_event["capital"])
                    accepted_delta = accepted_quantity_delta(
                        packed=packed,
                        accepted=accepted,
                        previously_posted=previously_posted,
                    )
                    cumulative_accepted = previously_posted + accepted_delta
                    accepted_capital_delta_value = accepted_capital_delta(
                        packed=packed,
                        accepted=accepted,
                        unit_cost=accepted_cost,
                        previously_posted_capital=previously_posted_capital,
                    )
                    target_accepted_capital = previously_posted_capital + accepted_capital_delta_value
                    quantity_capital_delta = accepted_delta * accepted_cost
                    cost_correction_delta = accepted_capital_delta_value - quantity_capital_delta
                    previous_wb_quantity = (
                        previous[(STAGE_WB, nm_id)].quantity
                        if (STAGE_WB, nm_id) in previous
                        else ZERO
                    )
                    retained_ratio = (
                        min(previous_wb_quantity / previously_posted, Decimal("1"))
                        if previously_posted > ZERO
                        else ZERO
                    )
                    current_pool_capital_delta = (
                        quantity_capital_delta + cost_correction_delta * retained_ratio
                    )
                    event_key = ("wb_final_acceptance", source_fingerprint, nm_id, _text(accepted_delta))
                    if not before_boundary and (accepted_delta != ZERO or accepted_capital_delta_value != ZERO):
                        event_id = "whfe_" + _hash(event_key)[:24]
                        new_events.append(
                            {
                                "event_id": event_id,
                                "event_type": "wb_final_acceptance",
                                "source_id": source_id,
                                "source_fingerprint": source_fingerprint,
                                "business_date": business_date,
                                "nm_id": nm_id,
                                "quantity": _text(accepted_delta),
                                "capital_rub": _text(accepted_capital_delta_value),
                                "provenance": {
                                    **provenance,
                                    "cumulative_accepted_quantity": _text(cumulative_accepted),
                                    "previously_posted_accepted_quantity": _text(previously_posted),
                                    "accepted_quantity_delta": _text(accepted_delta),
                                    "previously_posted_accepted_capital_rub": _text(previously_posted_capital),
                                    "target_accepted_capital_rub": _text(target_accepted_capital),
                                    "accepted_capital_delta_rub": _text(accepted_capital_delta_value),
                                    "current_pool_retained_ratio": _text(retained_ratio),
                                    "current_pool_capital_delta_rub": _text(current_pool_capital_delta),
                                    "source_correction": accepted_delta < ZERO,
                                    "cost_source_correction": accepted_capital_delta_value != accepted_delta * accepted_cost,
                                },
                            }
                        )

        doprinato_audit: list[dict[str, Any]] = []
        discrepancy_balances, unmatched = reconcile_discrepancies(
            discrepancies=discrepancy_receipts,
            doprinato=doprinato_rows,
            audit=doprinato_audit,
        )
        unmatched.extend(transitional_unmatched)
        if cutover_mode and discrepancy_balances:
            raise WarehouseFunctionalError("functional cutover discrepancy opening must be zero")
        for item in discrepancy_balances:
            _add_bucket(
                buckets,
                stage=STAGE_DISCREPANCY,
                nm_id=int(item["nm_id"]),
                quantity=_decimal(item["quantity"]),
                capital=_decimal(item["capital"]),
                covered=_decimal(item["quantity"]),
                quality="pooled_final_acceptance_discrepancy",
                provenance={
                    "receipts": item["receipts"],
                    "doprinato_matches": item["matches"],
                    "paid_acceptance_excluded": True,
                },
            )

        inbound_by_nm: defaultdict[int, tuple[Decimal, Decimal]] = defaultdict(lambda: (ZERO, ZERO))
        for event in new_events:
            qty, capital = inbound_by_nm[int(event["nm_id"])]
            inbound_by_nm[int(event["nm_id"])] = (
                qty + _decimal(event["quantity"]),
                capital
                + _decimal(
                    (event.get("provenance") or {}).get("current_pool_capital_delta_rub")
                    if isinstance(event.get("provenance"), Mapping)
                    else event["capital_rub"]
                ),
            )
        for nm_id, item in wb_items.items():
            physical = _decimal(item.get("quantity"))
            to_client = _decimal(item.get("in_way_to_client"))
            from_client = _decimal(item.get("in_way_from_client"))
            contour = physical + to_client + from_client
            if contour == ZERO:
                continue
            if cutover_mode:
                wac = cost_map[nm_id].wb_unit_cost
                quality = cost_map[nm_id].quality
                provenance = dict(cost_map[nm_id].provenance)
            else:
                previous_line = previous.get((STAGE_WB, nm_id))
                previous_qty = previous_line.quantity if previous_line else ZERO
                previous_capital = previous_line.capital if previous_line else ZERO
                inbound_qty, inbound_capital = inbound_by_nm[nm_id]
                _, _, rolled = roll_periodic_wac(
                    quantity=previous_qty,
                    capital=previous_capital,
                    quantity_delta=inbound_qty,
                    capital_delta=inbound_capital,
                )
                if rolled is None:
                    seed = cost_map.get(nm_id)
                    if seed is None:
                        raise WarehouseFunctionalError(
                            f"official WB contour {nm_id} has neither prior nor inbound cost"
                        )
                    wac = seed.wb_unit_cost
                else:
                    wac = rolled
                quality = "periodic_snapshot_wac"
                provenance = {
                    "previous_quantity": _text(previous_qty),
                    "previous_capital": _text(previous_capital),
                    "new_accepted_quantity": _text(inbound_qty),
                    "new_accepted_capital": _text(inbound_capital),
                    "last_valid_wac_fallback": rolled is None,
                }
            _add_bucket(
                buckets,
                stage=STAGE_WB,
                nm_id=nm_id,
                quantity=contour,
                capital=contour * wac,
                covered=contour,
                quality=quality,
                provenance={
                    "source": "official_wb_snapshot",
                    "snapshot_id": capture["wb_snapshot"]["snapshot_id"],
                    **provenance,
                },
                wb_quantity=physical,
                wb_to_client=to_client,
                wb_from_client=from_client,
            )

        lines = [_bucket_line(key, value) for key, value in sorted(buckets.items()) if value["quantity"] > ZERO]
        opening_payload = [
            {
                "nm_id": seed.nm_id,
                "ff_unit_cost_rub": _text(seed.ff_unit_cost),
                "wb_unit_cost_rub": _text(seed.wb_unit_cost),
                "quality": seed.quality,
                "provenance": dict(seed.provenance),
                "fingerprint": "sha256:" + _hash(
                    {
                        "nm_id": seed.nm_id,
                        "ff": _text(seed.ff_unit_cost),
                        "wb": _text(seed.wb_unit_cost),
                        "quality": seed.quality,
                        "provenance": seed.provenance,
                    }
                ),
            }
            for seed in cost_map.values()
        ]
        movement_documents = [
            {
                "document_type": "wb_final_acceptance_discrepancy",
                "warehouse_key": STAGE_DISCREPANCY,
                "occurred_at": str(item.get("business_date") or captured_at),
                "source_id": str(item.get("source_id") or ""),
                "source_fingerprint": str(item.get("source_fingerprint") or ""),
                "quantity": str(item["quantity"]),
                "capital_rub": str(item["capital"]),
                "provenance": dict(item.get("provenance") or {}),
                "lines": [
                    {
                        "nm_id": int(item["nm_id"]),
                        "quantity": str(item["quantity"]),
                        "wac_rub": str(item["wac"]),
                        "capital_rub": str(item["capital"]),
                        "provenance": dict(item.get("provenance") or {}),
                    }
                ],
            }
            for item in discrepancy_receipts
        ]
        for item in doprinato_audit:
            matched = _decimal(item.get("matched_quantity"))
            unmatched_quantity = _decimal(item.get("unmatched_quantity"))
            if matched > ZERO:
                matched_capital = _decimal(item.get("matched_capital_rub"))
                movement_documents.append(
                    {
                        "document_type": "wb_doprinato",
                        "warehouse_key": STAGE_DISCREPANCY,
                        "occurred_at": str(item.get("business_date") or captured_at),
                        "source_id": str(item.get("source_id") or ""),
                        "source_fingerprint": str(item.get("source_fingerprint") or ""),
                        "quantity": _text(-matched),
                        "capital_rub": _text(-matched_capital),
                        "provenance": {**dict(item.get("provenance") or {}), "pooled_by_sku": True},
                        "lines": [
                            {
                                "nm_id": int(item["nm_id"]),
                                "quantity": _text(-matched),
                                "wac_rub": str(item.get("matched_wac_rub") or ""),
                                "capital_rub": _text(-matched_capital),
                                "provenance": {**dict(item.get("provenance") or {}), "pooled_by_sku": True},
                            }
                        ],
                    }
                )
            if unmatched_quantity > ZERO:
                movement_documents.append(
                    {
                        "document_type": "wb_unmatched_doprinato_audit",
                        "warehouse_key": STAGE_DISCREPANCY,
                        "occurred_at": str(item.get("business_date") or captured_at),
                        "source_id": str(item.get("source_id") or ""),
                        "source_fingerprint": str(item.get("source_fingerprint") or ""),
                        "quantity": _text(unmatched_quantity),
                        "capital_rub": "0",
                        "provenance": {
                            **dict(item.get("provenance") or {}),
                            "non_stock_audit": True,
                            "reason": str(item.get("reason") or "no_positive_discrepancy_for_sku"),
                        },
                        "lines": [
                            {
                                "nm_id": int(item["nm_id"]),
                                "quantity": _text(unmatched_quantity),
                                "wac_rub": None,
                                "capital_rub": "0",
                                "provenance": {"non_stock_audit": True},
                            }
                        ],
                    }
                )
        for item in transitional_unmatched:
            movement_documents.append(
                {
                    "document_type": "wb_pre_cutover_unmatched_audit",
                    "warehouse_key": STAGE_DISCREPANCY,
                    "occurred_at": str(item.get("business_date") or captured_at),
                    "source_id": str(item.get("source_id") or ""),
                    "source_fingerprint": str(item.get("source_fingerprint") or ""),
                    "quantity": str(item["quantity"]),
                    "capital_rub": "0",
                    "provenance": dict(item.get("provenance") or {}),
                    "lines": [
                        {
                            "nm_id": int(item["nm_id"]),
                            "quantity": str(item["quantity"]),
                            "wac_rub": None,
                            "capital_rub": "0",
                            "provenance": dict(item.get("provenance") or {}),
                        }
                    ],
                }
            )
        return lines, unmatched, new_events, opening_payload, movement_documents

    def _load_opening_cost_map(self) -> dict[int, CostSeed]:
        with _connect(self.runtime.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_opening_cost_map WHERE cutover_id=? ORDER BY nm_id",
                (FUNCTIONAL_CUTOVER_ID,),
            ).fetchall()
        return {
            int(row["nm_id"]): CostSeed(
                nm_id=int(row["nm_id"]),
                ff_unit_cost=_decimal(row["ff_unit_cost_rub"]),
                wb_unit_cost=_decimal(row["wb_unit_cost_rub"]),
                quality=str(row["quality"]),
                provenance=_loads(row["provenance_json"], {}),
            )
            for row in rows
        }

    def _build_post_cutover_daily_cost_projection(
        self,
        *,
        captured_at: str,
        candidate_lines: Iterable[WarehouseLine],
        candidate_snapshot: Mapping[str, Any],
        new_events: Iterable[Mapping[str, Any]],
        opening_cost_map: Iterable[Mapping[str, Any]],
        cutover_mode: bool,
    ) -> list[dict[str, Any]]:
        """Replay versioned post-cutover WB WAC through the current snapshot day.

        Snapshot quantities are periodic physical evidence.  Accepted supply
        events contribute signed quantity/capital layers on their effective
        business date, so a late expense or accepted-quantity correction
        deterministically rewrites only the derived daily cost history.
        """

        current_date = captured_at[:10]
        opening_cost_rows = [dict(item) for item in opening_cost_map]
        seed_wac = {
            int(item["nm_id"]): _decimal(item["wb_unit_cost_rub"])
            for item in opening_cost_rows
            if int(item.get("nm_id") or 0) > 0
        }
        seed_meta = {
            int(item["nm_id"]): {
                "quality": str(item.get("quality") or ""),
                "provenance": dict(item.get("provenance") or {}),
            }
            for item in opening_cost_rows
            if int(item.get("nm_id") or 0) > 0
        }
        candidate_quantities = _wb_snapshot_quantities(candidate_snapshot.get("items") or [])
        candidate_wac = {
            item.nm_id: item.wac
            for item in candidate_lines
            if item.warehouse_key == STAGE_WB and item.wac is not None
        }
        if cutover_mode:
            rows = []
            for nm_id in sorted(set(seed_wac) | set(candidate_quantities) | set(candidate_wac)):
                quantity = candidate_quantities.get(nm_id, ZERO)
                wac = candidate_wac.get(nm_id) or seed_wac.get(nm_id)
                if wac is None or wac <= ZERO:
                    if quantity > ZERO:
                        raise WarehouseFunctionalError(
                            f"cutover WB snapshot has no daily WAC for nmId {nm_id}"
                        )
                    continue
                rows.append(
                    _daily_wb_cost_row(
                        day=current_date,
                        nm_id=nm_id,
                        quantity=quantity,
                        wac=wac,
                        quality="periodic_snapshot_wac_provisional",
                        provenance={
                            "source": "functional_cutover_official_wb_snapshot",
                            "snapshot_id": str(candidate_snapshot.get("snapshot_id") or ""),
                            "opening_cost": seed_meta.get(nm_id, {}),
                            "last_valid_wac_retained": quantity == ZERO,
                        },
                    )
                )
            return rows

        with _connect(self.runtime.db_path) as conn:
            cutover = conn.execute(
                "SELECT cutover_at FROM sheet_vitrina_v1_warehouse_functional_cutovers WHERE cutover_id=?",
                (FUNCTIONAL_CUTOVER_ID,),
            ).fetchone()
            cutover_version = conn.execute(
                """SELECT version_id,effective_at,created_at
                   FROM sheet_vitrina_v1_warehouse_functional_versions
                   WHERE cutover_id=? AND version_kind='functional_cutover' AND status='good'
                   ORDER BY created_at LIMIT 1""",
                (FUNCTIONAL_CUTOVER_ID,),
            ).fetchone()
            version_snapshots = conn.execute(
                """SELECT version.version_id,version.effective_at,version.created_at,snapshot.items_json,
                          snapshot.snapshot_id
                   FROM sheet_vitrina_v1_warehouse_functional_versions version
                   JOIN sheet_vitrina_v1_warehouse_wb_snapshots snapshot
                     ON snapshot.version_id=version.version_id
                   WHERE version.cutover_id=? AND version.status='good'
                   ORDER BY version.effective_at,version.created_at,version.version_id""",
                (FUNCTIONAL_CUTOVER_ID,),
            ).fetchall()
            opening_rows = (
                conn.execute(
                    """SELECT nm_id,quantity,wac_rub FROM sheet_vitrina_v1_warehouse_functional_balances
                       WHERE version_id=? AND warehouse_key=? ORDER BY nm_id""",
                    (cutover_version["version_id"], STAGE_WB),
                ).fetchall()
                if cutover_version is not None
                else []
            )
            persisted_events = conn.execute(
                """SELECT event_id,business_date,nm_id,quantity,capital_rub,source_id,
                          source_fingerprint,provenance_json
                   FROM sheet_vitrina_v1_warehouse_functional_events
                   WHERE event_type='wb_final_acceptance'
                   ORDER BY business_date,created_at,event_id"""
            ).fetchall()
        if cutover is None or cutover_version is None:
            raise WarehouseFunctionalError("functional daily WAC replay has no cutover baseline")
        cutover_date = str(cutover["cutover_at"])[:10]
        if current_date < cutover_date:
            raise WarehouseFunctionalError("functional daily WAC replay date precedes cutover")

        opening_quantity = {int(row["nm_id"]): _decimal(row["quantity"]) for row in opening_rows}
        opening_wac = {
            int(row["nm_id"]): _decimal(row["wac_rub"])
            for row in opening_rows
            if row["wac_rub"] not in (None, "")
        }
        if not seed_wac:
            loaded_seeds = self._load_opening_cost_map()
            seed_wac = {nm_id: seed.wb_unit_cost for nm_id, seed in loaded_seeds.items()}
            seed_meta = {
                nm_id: {"quality": seed.quality, "provenance": dict(seed.provenance)}
                for nm_id, seed in loaded_seeds.items()
            }

        snapshots_by_day: dict[str, dict[str, Any]] = {}
        for row in version_snapshots:
            day = str(row["effective_at"])[:10]
            if not cutover_date <= day <= current_date:
                continue
            snapshots_by_day[day] = {
                "quantities": _wb_snapshot_quantities(_loads(row["items_json"], [])),
                "snapshot_id": str(row["snapshot_id"]),
                "version_id": str(row["version_id"]),
            }
        snapshots_by_day[current_date] = {
            "quantities": candidate_quantities,
            "snapshot_id": str(candidate_snapshot.get("snapshot_id") or ""),
            "version_id": "candidate",
        }

        events_by_day: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        seen_event_ids: set[str] = set()
        for row in persisted_events:
            item = dict(row)
            item["provenance"] = _loads(item.pop("provenance_json"), {})
            event_id = str(item.get("event_id") or "")
            seen_event_ids.add(event_id)
            day = str(item.get("business_date") or "")[:10]
            if not cutover_date <= day <= current_date:
                raise WarehouseFunctionalError(
                    f"functional acceptance event {event_id} has invalid replay date {day!r}"
                )
            events_by_day[day].append(item)
        for raw in new_events:
            item = dict(raw)
            event_id = str(item.get("event_id") or "")
            if event_id in seen_event_ids:
                continue
            seen_event_ids.add(event_id)
            day = str(item.get("business_date") or "")[:10]
            if not cutover_date <= day <= current_date:
                raise WarehouseFunctionalError(
                    f"new functional acceptance event {event_id} has invalid replay date {day!r}"
                )
            events_by_day[day].append(item)

        target_nm_ids = set(seed_wac) | set(opening_quantity) | set(candidate_quantities)
        for snapshot in snapshots_by_day.values():
            target_nm_ids.update(snapshot["quantities"])
        for rows in events_by_day.values():
            target_nm_ids.update(int(item.get("nm_id") or 0) for item in rows)
        target_nm_ids.discard(0)

        previous_quantity = {
            nm_id: opening_quantity.get(nm_id, ZERO) for nm_id in target_nm_ids
        }
        previous_wac = {
            nm_id: opening_wac.get(nm_id) or seed_wac.get(nm_id)
            for nm_id in target_nm_ids
        }
        result: list[dict[str, Any]] = []
        for day in _date_range(cutover_date, current_date):
            snapshot = snapshots_by_day.get(day)
            event_groups: defaultdict[int, list[Mapping[str, Any]]] = defaultdict(list)
            for event in events_by_day.get(day, []):
                event_groups[int(event.get("nm_id") or 0)].append(event)
            for nm_id in sorted(target_nm_ids):
                prior_qty = previous_quantity.get(nm_id, ZERO)
                prior_wac = previous_wac.get(nm_id)
                event_rows = event_groups.get(nm_id, [])
                quantity_delta = sum((_decimal(item.get("quantity")) for item in event_rows), ZERO)
                capital_delta = sum((_decimal(item.get("capital_rub")) for item in event_rows), ZERO)
                if quantity_delta != ZERO or capital_delta != ZERO:
                    if prior_wac is None:
                        if quantity_delta <= ZERO or capital_delta <= ZERO:
                            raise WarehouseFunctionalError(
                                f"daily WB replay has no opening WAC for correction {day}:{nm_id}"
                            )
                        rolled_qty = quantity_delta
                        rolled_capital = capital_delta
                    else:
                        rolled_qty = prior_qty + quantity_delta
                        rolled_capital = prior_qty * prior_wac + capital_delta
                    if rolled_qty < ZERO or rolled_capital < ZERO:
                        raise WarehouseFunctionalError(
                            f"daily WB replay correction makes pool negative for {day}:{nm_id}"
                        )
                    if rolled_qty > ZERO:
                        if rolled_capital <= ZERO:
                            raise WarehouseFunctionalError(
                                f"daily WB replay loses positive capital for {day}:{nm_id}"
                            )
                        prior_wac = rolled_capital / rolled_qty
                quantity = (
                    snapshot["quantities"].get(nm_id, ZERO)
                    if snapshot is not None
                    else prior_qty
                )
                if prior_wac is None or prior_wac <= ZERO:
                    if quantity > ZERO:
                        raise WarehouseFunctionalError(
                            f"daily WB snapshot has no WAC for {day}:{nm_id}"
                        )
                    continue
                quality = (
                    "periodic_snapshot_wac_provisional"
                    if day == current_date
                    else "periodic_snapshot_wac_closed"
                )
                result.append(
                    _daily_wb_cost_row(
                        day=day,
                        nm_id=nm_id,
                        quantity=quantity,
                        wac=prior_wac,
                        quality=quality,
                        provenance={
                            "source": "versioned_functional_wb_daily_replay",
                            "snapshot_id": str((snapshot or {}).get("snapshot_id") or "carried_last_good"),
                            "snapshot_version_id": str((snapshot or {}).get("version_id") or "carried_last_good"),
                            "previous_snapshot_quantity": _text(prior_qty),
                            "accepted_quantity_delta": _text(quantity_delta),
                            "accepted_capital_delta_rub": _text(capital_delta),
                            "accepted_event_ids": [str(item.get("event_id") or "") for item in event_rows],
                            "opening_cost": seed_meta.get(nm_id, {}),
                            "last_valid_wac_retained": quantity == ZERO,
                            "snapshot_carried_forward": snapshot is None,
                        },
                    )
                )
                previous_quantity[nm_id] = quantity
                previous_wac[nm_id] = prior_wac
        return result

    def _active_version_id(self, *, connection: sqlite3.Connection | None = None) -> str:
        if connection is not None:
            row = connection.execute(
                "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
            ).fetchone()
            return str(row["version_id"]) if row is not None else ""
        with _connect(self.runtime.db_path) as conn:
            return self._active_version_id(connection=conn)

    def _version_exists(self, plan_fingerprint: str) -> bool:
        with _connect(self.runtime.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM sheet_vitrina_v1_warehouse_functional_versions WHERE plan_fingerprint=?",
                (plan_fingerprint,),
            ).fetchone()
        return row is not None

    def _active_lines(self) -> dict[tuple[str, int], WarehouseLine]:
        readback = self.readback()
        return {
            (str(item["warehouse_key"]), int(item["nm_id"])): _line_from_payload(item)
            for item in readback.get("balances") or []
        }

    def _cutover_stage_lines(self, stage: str) -> dict[int, WarehouseLine]:
        with _connect(self.runtime.db_path) as conn:
            version = conn.execute(
                """SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_versions
                   WHERE cutover_id=? AND version_kind='functional_cutover'
                   ORDER BY created_at LIMIT 1""",
                (FUNCTIONAL_CUTOVER_ID,),
            ).fetchone()
            rows = (
                conn.execute(
                    """SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances
                       WHERE version_id=? AND warehouse_key=? ORDER BY nm_id""",
                    (version["version_id"], stage),
                ).fetchall()
                if version is not None
                else []
            )
        result: dict[int, WarehouseLine] = {}
        for row in rows:
            payload = _balance_public(dict(row))
            payload["provenance"] = {
                **dict(payload.get("provenance") or {}),
                "version_id": str(row["version_id"]),
            }
            result[int(row["nm_id"])] = _line_from_payload(payload)
        return result

    def _processed_event_fingerprints(self) -> set[tuple[str, str, int]]:
        with _connect(self.runtime.db_path) as conn:
            rows = conn.execute(
                "SELECT event_type,source_fingerprint,nm_id FROM sheet_vitrina_v1_warehouse_functional_events"
            ).fetchall()
        return {(str(row[0]), str(row[1]), int(row[2])) for row in rows}

    def _accepted_event_totals(self) -> dict[tuple[str, int], dict[str, Decimal]]:
        with _connect(self.runtime.db_path) as conn:
            rows = conn.execute(
                """SELECT source_id,nm_id,quantity,capital_rub
                   FROM sheet_vitrina_v1_warehouse_functional_events
                   WHERE event_type='wb_final_acceptance'
                   ORDER BY created_at,event_id"""
            ).fetchall()
        totals: defaultdict[tuple[str, int], dict[str, Decimal]] = defaultdict(
            lambda: {"quantity": ZERO, "capital": ZERO}
        )
        for row in rows:
            item = totals[(str(row["source_id"]), int(row["nm_id"]))]
            item["quantity"] += _decimal(row["quantity"])
            item["capital"] += _decimal(row["capital_rub"])
        return {key: dict(value) for key, value in totals.items()}

    def _cutover_row(self) -> dict[str, Any] | None:
        with _connect(self.runtime.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_functional_cutovers WHERE cutover_id=?",
                (FUNCTIONAL_CUTOVER_ID,),
            ).fetchone()
        return _cutover_public(row) if row else None

    def _local_source_digest(self, *, connection: sqlite3.Connection | None = None) -> str:
        if connection is not None:
            return "sha256:" + _hash(_guarded_local_sources(_source_rows(connection)))
        with _connect(self.runtime.db_path) as conn:
            return "sha256:" + _hash(_guarded_local_sources(_source_rows(conn)))

    def _wb_supply_source_digest(self, *, connection: sqlite3.Connection | None = None) -> str:
        if connection is not None:
            return "sha256:" + _hash(_supply_revisions(_source_rows(connection)["wb_supplies"]))
        with _connect(self.runtime.db_path) as conn:
            return "sha256:" + _hash(_supply_revisions(_source_rows(conn)["wb_supplies"]))

    def _last_good_wb_payload(self) -> dict[str, Any]:
        with _connect(self.runtime.db_path) as conn:
            row = conn.execute(
                """SELECT snapshot.* FROM sheet_vitrina_v1_warehouse_wb_snapshots snapshot
                   JOIN sheet_vitrina_v1_warehouse_functional_versions version
                     ON version.version_id=snapshot.version_id
                   WHERE version.status='good'
                   ORDER BY version.effective_at DESC,snapshot.created_at DESC LIMIT 1"""
            ).fetchone()
        if row is None:
            raise WarehouseFunctionalError("no last-good WB snapshot is available")
        return {
            "snapshot_date": str(row["snapshot_date"]),
            "requested_nm_ids": _loads(row["requested_nm_ids_json"], []),
            "canonical_items": _loads(row["items_json"], []),
            "data": {
                "fetched_at": str(row["fetched_at"]),
                "pagination_complete": bool(row["pagination_complete"]),
                "page_count": int(row["page_count"]),
                "page_offsets": _loads(row["page_offsets_json"], []),
                "raw_rows_digest": str(row["raw_rows_digest"]),
                "rows": _loads(row["raw_rows_json"], []),
            },
        }

    def _upsert_supplier_flows(
        self,
        conn: sqlite3.Connection,
        lines: Iterable[Mapping[str, Any]],
        *,
        created_at: str,
    ) -> None:
        flows: dict[str, dict[str, str]] = {}
        for line in lines:
            if str(line.get("warehouse_key") or "") not in {STAGE_PRODUCTION, STAGE_CHINA_TO_FF}:
                continue
            provenance = dict(line.get("provenance") or {})
            for source in provenance.get("source_records") or []:
                flow_id = str(source.get("supplier_flow_id") or "")
                shipment_id = str(source.get("shipment_id") or "")
                if not flow_id or not shipment_id:
                    continue
                flows[flow_id] = {
                    "shipment_id": shipment_id,
                    "invoice_no": str(source.get("invoice_no") or ""),
                    "source_fingerprint": "sha256:" + _hash(source),
                }
        for flow_id, item in sorted(flows.items()):
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_warehouse_supplier_flows(
                       supplier_flow_id,shipment_id,invoice_no,created_at,source_fingerprint
                   ) VALUES(?,?,?,?,?) ON CONFLICT(supplier_flow_id) DO UPDATE SET
                       invoice_no=excluded.invoice_no,source_fingerprint=excluded.source_fingerprint""",
                (
                    flow_id,
                    item["shipment_id"],
                    item["invoice_no"],
                    created_at,
                    item["source_fingerprint"],
                ),
            )

    def _insert_snapshot(self, conn: sqlite3.Connection, *, version_id: str, payload: Mapping[str, Any]) -> None:
        stored_snapshot_id = _stable_id(
            "wbsnapv",
            {"source_snapshot_id": payload["snapshot_id"], "version_id": version_id},
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_warehouse_wb_snapshots(
                   snapshot_id,version_id,fetched_at,snapshot_date,requested_nm_ids_json,
                   pagination_complete,page_count,page_offsets_json,raw_row_count,raw_rows_digest,
                   raw_rows_json,items_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                stored_snapshot_id,
                version_id,
                payload["fetched_at"],
                payload["snapshot_date"],
                _json(payload["requested_nm_ids"]),
                int(bool(payload["pagination_complete"])),
                int(payload["page_count"]),
                _json(payload["page_offsets"]),
                int(payload["raw_row_count"]),
                payload["raw_rows_digest"],
                _json(payload["raw_rows"]),
                _json(payload["items"]),
                self.timestamp_factory(),
            ),
        )

    def _insert_documents(
        self, conn: sqlite3.Connection, *, version_id: str, plan: Mapping[str, Any], created_at: str
    ) -> None:
        document_type = "functional_cutover" if plan["kind"] == "functional_cutover" else "warehouse_sync"
        for stage in STAGES:
            summary = plan["summaries"][stage]
            document_id = _stable_id(
                "whdoc",
                {"version_id": version_id, "warehouse_key": stage, "type": document_type},
            )
            source_fingerprint = "sha256:" + _hash(
                [item for item in plan["lines"] if item["warehouse_key"] == stage]
            )
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_warehouse_functional_documents(
                       document_id,version_id,warehouse_key,document_type,occurred_at,source_id,
                       source_fingerprint,quantity,capital_rub,provenance_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    document_id,
                    version_id,
                    stage,
                    document_type,
                    created_at if document_type == "functional_cutover" else plan["captured_at"],
                    FUNCTIONAL_CUTOVER_ID if document_type == "functional_cutover" else plan["wb_snapshot"]["snapshot_id"],
                    source_fingerprint,
                    summary["quantity"],
                    summary["capital_rub"],
                    _json({"source_watermarks": plan["source_watermarks"], "quality": summary["quality"]}),
                    created_at,
                ),
            )
            for item in plan["lines"]:
                if item["warehouse_key"] != stage:
                    continue
                self._insert_document_line(
                    conn,
                    document_id=document_id,
                    version_id=version_id,
                    item=item,
                    created_at=created_at,
                )
        for item in plan.get("movement_documents") or []:
            document_id = _stable_id(
                "whdoc",
                {
                    "warehouse_key": item["warehouse_key"],
                    "type": item["document_type"],
                    "source_id": item["source_id"],
                    "source_fingerprint": item["source_fingerprint"],
                },
            )
            inserted = conn.execute(
                """INSERT OR IGNORE INTO sheet_vitrina_v1_warehouse_functional_documents(
                       document_id,version_id,warehouse_key,document_type,occurred_at,source_id,
                       source_fingerprint,quantity,capital_rub,provenance_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    document_id,
                    version_id,
                    item["warehouse_key"],
                    item["document_type"],
                    item["occurred_at"],
                    item["source_id"],
                    item["source_fingerprint"],
                    item["quantity"],
                    item["capital_rub"],
                    _json(item.get("provenance") or {}),
                    created_at,
                ),
            ).rowcount
            if not inserted:
                continue
            for line in item.get("lines") or []:
                self._insert_document_line(
                    conn,
                    document_id=document_id,
                    version_id=version_id,
                    item=line,
                    created_at=created_at,
                )

    @staticmethod
    def _insert_document_line(
        conn: sqlite3.Connection,
        *,
        document_id: str,
        version_id: str,
        item: Mapping[str, Any],
        created_at: str,
    ) -> None:
        nm_id = int(item["nm_id"])
        line_id = _stable_id("whdocline", {"document_id": document_id, "nm_id": nm_id})
        conn.execute(
            """INSERT OR IGNORE INTO sheet_vitrina_v1_warehouse_functional_document_lines(
                   line_id,document_id,version_id,nm_id,quantity,wac_rub,capital_rub,
                   provenance_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                line_id,
                document_id,
                version_id,
                nm_id,
                str(item["quantity"]),
                item.get("wac_rub"),
                str(item["capital_rub"]),
                _json(item.get("provenance") or {}),
                created_at,
            ),
        )


def ensure_warehouse_functional_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_functional_cutovers(
            cutover_id TEXT PRIMARY KEY,cutover_at TEXT NOT NULL,status TEXT NOT NULL,
            plan_fingerprint TEXT NOT NULL UNIQUE,source_watermarks_json TEXT NOT NULL,
            absorbed_supply_revisions_json TEXT NOT NULL,backup_json TEXT NOT NULL,
            created_at TEXT NOT NULL,updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_opening_cost_map(
            cutover_id TEXT NOT NULL REFERENCES sheet_vitrina_v1_warehouse_functional_cutovers(cutover_id),
            nm_id INTEGER NOT NULL,ff_unit_cost_rub TEXT NOT NULL,wb_unit_cost_rub TEXT NOT NULL,
            quality TEXT NOT NULL,provenance_json TEXT NOT NULL,fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL,PRIMARY KEY(cutover_id,nm_id)
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_wb_daily_cost(
            cutover_id TEXT NOT NULL,as_of_date TEXT NOT NULL,nm_id INTEGER NOT NULL,
            quantity TEXT NOT NULL,wac_rub TEXT NOT NULL,capital_rub TEXT NOT NULL,
            quality TEXT NOT NULL,provenance_json TEXT NOT NULL,fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL,PRIMARY KEY(cutover_id,as_of_date,nm_id)
        );
        CREATE INDEX IF NOT EXISTS warehouse_wb_daily_cost_by_date
        ON sheet_vitrina_v1_warehouse_wb_daily_cost(as_of_date,nm_id);
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_functional_versions(
            version_id TEXT PRIMARY KEY,cutover_id TEXT NOT NULL,version_kind TEXT NOT NULL,
            effective_at TEXT NOT NULL,status TEXT NOT NULL,plan_fingerprint TEXT NOT NULL UNIQUE,
            local_source_digest TEXT NOT NULL,source_watermarks_json TEXT NOT NULL,created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_functional_active(
            slot INTEGER PRIMARY KEY CHECK(slot=1),version_id TEXT NOT NULL,updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_functional_balances(
            version_id TEXT NOT NULL,warehouse_key TEXT NOT NULL,nm_id INTEGER NOT NULL,
            quantity TEXT NOT NULL,wac_rub TEXT,capital_rub TEXT NOT NULL,cost_covered_quantity TEXT NOT NULL,
            quality TEXT NOT NULL,certified INTEGER NOT NULL,wb_quantity TEXT NOT NULL,
            wb_in_way_to_client TEXT NOT NULL,wb_in_way_from_client TEXT NOT NULL,
            provenance_json TEXT NOT NULL,PRIMARY KEY(version_id,warehouse_key,nm_id)
        );
        CREATE INDEX IF NOT EXISTS warehouse_functional_balance_stage
        ON sheet_vitrina_v1_warehouse_functional_balances(version_id,warehouse_key,nm_id);
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_wb_snapshots(
            snapshot_id TEXT PRIMARY KEY,version_id TEXT NOT NULL,fetched_at TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,requested_nm_ids_json TEXT NOT NULL,pagination_complete INTEGER NOT NULL,
            page_count INTEGER NOT NULL,page_offsets_json TEXT NOT NULL,raw_row_count INTEGER NOT NULL,
            raw_rows_digest TEXT NOT NULL,raw_rows_json TEXT NOT NULL,items_json TEXT NOT NULL,created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_unmatched_doprinato(
            unmatched_id TEXT PRIMARY KEY,version_id TEXT NOT NULL,source_id TEXT NOT NULL,
            business_date TEXT,nm_id INTEGER NOT NULL,quantity TEXT NOT NULL,matched_quantity TEXT NOT NULL,
            reason TEXT NOT NULL,provenance_json TEXT NOT NULL,created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_functional_events(
            event_id TEXT PRIMARY KEY,version_id TEXT NOT NULL,event_type TEXT NOT NULL,source_id TEXT NOT NULL,
            source_fingerprint TEXT NOT NULL,business_date TEXT,nm_id INTEGER NOT NULL,quantity TEXT NOT NULL,
            capital_rub TEXT NOT NULL,provenance_json TEXT NOT NULL,created_at TEXT NOT NULL,
            UNIQUE(event_type,source_fingerprint,nm_id)
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_functional_documents(
            document_id TEXT PRIMARY KEY,version_id TEXT NOT NULL,warehouse_key TEXT NOT NULL,
            document_type TEXT NOT NULL,occurred_at TEXT NOT NULL,source_id TEXT NOT NULL,
            source_fingerprint TEXT NOT NULL,quantity TEXT NOT NULL,capital_rub TEXT NOT NULL,
            provenance_json TEXT NOT NULL,created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_functional_document_lines(
            line_id TEXT PRIMARY KEY,document_id TEXT NOT NULL,version_id TEXT NOT NULL,
            nm_id INTEGER NOT NULL,quantity TEXT NOT NULL,wac_rub TEXT,capital_rub TEXT NOT NULL,
            provenance_json TEXT NOT NULL,created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS warehouse_functional_document_lines_document
        ON sheet_vitrina_v1_warehouse_functional_document_lines(document_id,nm_id);
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_supplier_flows(
            supplier_flow_id TEXT PRIMARY KEY,shipment_id TEXT NOT NULL UNIQUE,invoice_no TEXT,
            created_at TEXT NOT NULL,source_fingerprint TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_wb_sync_status(
            slot INTEGER PRIMARY KEY CHECK(slot=1),last_attempt_at TEXT,last_success_at TEXT,
            last_error TEXT,active_version_id TEXT,updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_targeted_recalc_queue(
            queue_id TEXT PRIMARY KEY,stable_source_id TEXT NOT NULL,source_revision TEXT NOT NULL,
            effective_date TEXT NOT NULL,affected_nm_ids_json TEXT NOT NULL,status TEXT NOT NULL,
            requested_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,error TEXT,
            UNIQUE(stable_source_id,source_revision)
        );
        """
    )


def _source_rows(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {
        "sheet_vitrina_v1_supplier_shipments",
        "sheet_vitrina_v1_supplier_shipment_lines",
        "sheet_vitrina_v1_cny_ledger_operations",
        "sheet_vitrina_v1_supplier_financial_documents",
        "sheet_vitrina_v1_supplier_financial_expense_lines",
        "sheet_vitrina_v1_ff_stock_operations",
        "sheet_vitrina_v1_ff_stock_operation_lines",
        "sheet_vitrina_v1_ff_stock_wb_auto_writeoff_checkpoint",
        "sheet_vitrina_v1_wb_supplies",
        "sheet_vitrina_v1_wb_supply_cost_layers",
        "sheet_vitrina_v1_fulfillment_service_uploads",
        "sheet_vitrina_v1_fulfillment_service_lines",
        "sheet_vitrina_v1_canonical_cost_baseline_versions",
        "sheet_vitrina_v1_canonical_cost_baseline_lines",
        "sheet_vitrina_v1_canonical_cost_daily_state",
        "sheet_vitrina_v1_supplier_ff_cost_layer_lines",
        "sheet_vitrina_v1_nomenclature_items",
    }
    missing = sorted(required - tables)
    if missing:
        raise WarehouseFunctionalError("required source tables are missing: " + ",".join(missing))
    baseline = conn.execute(
        "SELECT * FROM sheet_vitrina_v1_canonical_cost_baseline_versions WHERE is_current=1"
    ).fetchone()
    if baseline is None:
        raise WarehouseFunctionalError("frozen canonical baseline is not materialized")
    report = _loads(baseline["report_json"], {})
    primary_id = str(baseline["primary_shipment_id"])
    queries = {
        "shipments": "SELECT * FROM sheet_vitrina_v1_supplier_shipments ORDER BY shipment_id",
        "shipment_lines": "SELECT * FROM sheet_vitrina_v1_supplier_shipment_lines ORDER BY shipment_id,sort_order,line_id",
        "cny_operations": "SELECT * FROM sheet_vitrina_v1_cny_ledger_operations ORDER BY sequence_key,operation_id",
        "financial_documents": "SELECT * FROM sheet_vitrina_v1_supplier_financial_documents ORDER BY document_date,document_id",
        "financial_expense_lines": "SELECT * FROM sheet_vitrina_v1_supplier_financial_expense_lines ORDER BY supplier_order_id,financial_document_id,sort_order",
        "ff_operations": "SELECT * FROM sheet_vitrina_v1_ff_stock_operations ORDER BY created_at,operation_id",
        "ff_lines": "SELECT * FROM sheet_vitrina_v1_ff_stock_operation_lines ORDER BY operation_id,line_no",
        "ff_auto_writeoff_checkpoint": "SELECT * FROM sheet_vitrina_v1_ff_stock_wb_auto_writeoff_checkpoint ORDER BY slot",
        "wb_supplies": "SELECT * FROM sheet_vitrina_v1_wb_supplies ORDER BY supply_id",
        "fulfillment_service_uploads": "SELECT * FROM sheet_vitrina_v1_fulfillment_service_uploads ORDER BY upload_id",
        "fulfillment_service_lines": "SELECT * FROM sheet_vitrina_v1_fulfillment_service_lines ORDER BY upload_id,row_index,id",
        "nomenclature_purchase_prices": "SELECT item_id,nm_id,purchase_price_yuan,updated_at FROM sheet_vitrina_v1_nomenclature_items WHERE is_active=1 AND nm_id IS NOT NULL ORDER BY nm_id,item_id",
        "downstream_cost_rows": "SELECT wb_supply_id,nm_id,accepted_qty quantity,accepted_date,supply_date,sku_ff_unit_cost_rub ff_unit_cost_rub,transit_cost_status,transit_per_unit_rub,ff_services_per_unit_rub,ff_storage_per_unit_rub,pre_acceptance_unit_cost_rub,wb_acceptance_amount_total,wb_acceptance_per_accepted_unit_rub,our_wb_unit_cost_rub wb_unit_cost_rub,source_status,component_status_json,inputs_hash FROM sheet_vitrina_v1_wb_supply_cost_layers WHERE is_current=1 ORDER BY wb_supply_id,nm_id",
        "historical_wb_daily_quantities": "SELECT as_of_date,nm_id,physical_quantity FROM sheet_vitrina_v1_canonical_cost_daily_state WHERE stage='WB' AND as_of_date>='2026-07-01' ORDER BY as_of_date,nm_id",
    }
    result = {
        key: [dict(row) for row in conn.execute(sql).fetchall()]
        for key, sql in queries.items()
    }
    result["primary_cost_rows"] = [dict(row) for row in conn.execute(
        """SELECT line.nm_id,line.qty,line.invoice_unit_price_cny,line.sku_ff_unit_cost_rub,
                  line.layer_line_id,line.source_status
           FROM sheet_vitrina_v1_supplier_ff_cost_layer_lines line
           WHERE line.supplier_shipment_id=? AND line.nm_id IS NOT NULL ORDER BY line.nm_id""",
        (primary_id,),
    ).fetchall()]
    result["primary_identity"] = {
        "shipment_id": primary_id,
        "accepted_ff_date": str(baseline["primary_accepted_ff_date"]),
        "baseline_fingerprint": str(baseline["fingerprint"]),
        "ff_cost_layer_id": str((report.get("primary_shipment") or {}).get("ff_cost_layer_id") or ""),
    }
    return result


def _guarded_local_sources(sources: Mapping[str, Any]) -> dict[str, Any]:
    """Apply guard for production sources that cutover is not allowed to mutate.

    Fresh WB supply evidence is captured into the reviewed plan from a disposable
    coherent database copy.  Its own digest stays in the plan, while all other
    supplier/CNY/financial/FF/cost evidence is optimistically rechecked against
    production immediately before atomic apply.
    """

    return {
        key: value
        for key, value in sources.items()
        if key not in {"wb_supplies", "downstream_cost_rows"}
    }


def _supply_downstream_component_index(
    rows: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, int], dict[str, Any]]:
    """Return validated non-FF supply components using Decimal arithmetic."""

    result: dict[tuple[str, int], dict[str, Any]] = {}
    for row in rows:
        supply_id = str(row.get("wb_supply_id") or "")
        nm_id = int(row.get("nm_id") or 0)
        if not supply_id or nm_id <= 0:
            continue
        transit_status = str(row.get("transit_cost_status") or "")
        if transit_status not in {"transit_confirmed", "direct_zero_confirmed"}:
            continue
        transit = _decimal(row.get("transit_per_unit_rub"))
        services = _decimal(row.get("ff_services_per_unit_rub"))
        storage = _decimal(row.get("ff_storage_per_unit_rub"))
        acceptance = _decimal(row.get("wb_acceptance_per_accepted_unit_rub"))
        if min(transit, services, storage, acceptance) < ZERO:
            raise WarehouseFunctionalError(
                f"WB supply {supply_id}:{nm_id} has a negative downstream component"
            )
        result[(supply_id, nm_id)] = {
            "pre_acceptance_addon": transit + services + storage,
            "acceptance_addon": acceptance,
            "inputs_hash": str(row.get("inputs_hash") or ""),
        }
    return result


def compose_supply_costs(
    *,
    outbound_ff_wac: Any,
    pre_acceptance_addon: Any,
    acceptance_addon: Any,
) -> tuple[Decimal, Decimal]:
    """Compose supply cost without carrying a legacy FF-cost baseline forward."""

    ff_wac = _decimal(outbound_ff_wac)
    pre_addon = _decimal(pre_acceptance_addon)
    acceptance = _decimal(acceptance_addon)
    if ff_wac <= ZERO or min(pre_addon, acceptance) < ZERO:
        raise WarehouseFunctionalError("invalid FF WAC or downstream supply cost component")
    pre_acceptance = ff_wac + pre_addon
    return pre_acceptance, pre_acceptance + acceptance


def _nomenclature_purchase_prices(
    rows: Iterable[Mapping[str, Any]],
) -> dict[int, Decimal]:
    """Resolve price bands from the active nomenclature at cutover time."""

    candidates: defaultdict[int, set[Decimal]] = defaultdict(set)
    for row in rows:
        nm_id = int(row.get("nm_id") or 0)
        price = _optional_decimal(row.get("purchase_price_yuan"))
        if nm_id > 0 and price is not None and price > ZERO:
            candidates[nm_id].add(price)
    conflicts = {nm_id: values for nm_id, values in candidates.items() if len(values) > 1}
    if conflicts:
        raise WarehouseFunctionalError(
            "active nomenclature has conflicting CNY purchase prices for nmIds: "
            + ",".join(str(nm_id) for nm_id in sorted(conflicts))
        )
    return {nm_id: next(iter(values)) for nm_id, values in candidates.items()}


def _add_bucket(
    buckets: defaultdict[tuple[str, int], dict[str, Any]],
    *,
    stage: str,
    nm_id: int,
    quantity: Decimal,
    capital: Decimal,
    covered: Decimal,
    quality: str,
    provenance: Mapping[str, Any],
    wb_quantity: Decimal = ZERO,
    wb_to_client: Decimal = ZERO,
    wb_from_client: Decimal = ZERO,
) -> None:
    if stage not in STAGES or nm_id <= 0 or min(quantity, capital, covered) < ZERO:
        raise WarehouseFunctionalError("invalid warehouse bucket contribution")
    target = buckets[(stage, nm_id)]
    target["quantity"] += quantity
    target["capital"] += capital
    target["covered"] += min(covered, quantity)
    target["quality"].append(quality)
    target["provenance"].append(dict(provenance))
    target["wb_quantity"] = target.get("wb_quantity", ZERO) + wb_quantity
    target["wb_to_client"] = target.get("wb_to_client", ZERO) + wb_to_client
    target["wb_from_client"] = target.get("wb_from_client", ZERO) + wb_from_client


def _bucket_line(key: tuple[str, int], value: Mapping[str, Any]) -> WarehouseLine:
    stage, nm_id = key
    quality = sorted(set(value["quality"]))
    return WarehouseLine(
        warehouse_key=stage,
        nm_id=nm_id,
        quantity=_decimal(value["quantity"]),
        capital=_decimal(value["capital"]),
        cost_covered_quantity=min(_decimal(value["covered"]), _decimal(value["quantity"])),
        quality=quality[0] if len(quality) == 1 else "mixed:" + ",".join(quality),
        certified=all(item in {"direct_24_06", "primary_documents", "certified"} for item in quality),
        provenance={"source_records": list(value["provenance"])},
        wb_quantity=_decimal(value.get("wb_quantity")),
        wb_in_way_to_client=_decimal(value.get("wb_to_client")),
        wb_in_way_from_client=_decimal(value.get("wb_from_client")),
    )


def _line_payload(line: WarehouseLine) -> dict[str, Any]:
    return {
        "warehouse_key": line.warehouse_key,
        "nm_id": line.nm_id,
        "quantity": _text(line.quantity),
        "wac_rub": _text(line.wac) if line.wac is not None else None,
        "capital_rub": _text(line.capital),
        "cost_covered_quantity": _text(line.cost_covered_quantity),
        "coverage_share": _text(line.cost_covered_quantity / line.quantity) if line.quantity > ZERO else None,
        "quality": line.quality,
        "certified": line.certified,
        "wb_quantity": _text(line.wb_quantity),
        "wb_in_way_to_client": _text(line.wb_in_way_to_client),
        "wb_in_way_from_client": _text(line.wb_in_way_from_client),
        "provenance": dict(line.provenance),
    }


def _line_from_payload(item: Mapping[str, Any]) -> WarehouseLine:
    return WarehouseLine(
        warehouse_key=str(item["warehouse_key"]),
        nm_id=int(item["nm_id"]),
        quantity=_decimal(item["quantity"]),
        capital=_decimal(item["capital_rub"]),
        cost_covered_quantity=_decimal(item["cost_covered_quantity"]),
        quality=str(item["quality"]),
        certified=bool(item.get("certified")),
        provenance=dict(item.get("provenance") or {}),
        wb_quantity=_decimal(item.get("wb_quantity")),
        wb_in_way_to_client=_decimal(item.get("wb_in_way_to_client")),
        wb_in_way_from_client=_decimal(item.get("wb_in_way_from_client")),
    )


def _summaries(lines: Iterable[WarehouseLine]) -> dict[str, dict[str, Any]]:
    grouped: defaultdict[str, list[WarehouseLine]] = defaultdict(list)
    for line in lines:
        grouped[line.warehouse_key].append(line)
    result = {}
    for stage in STAGES:
        rows = grouped[stage]
        quantity = sum((item.quantity for item in rows), ZERO)
        capital = sum((item.capital for item in rows), ZERO)
        covered = sum((item.cost_covered_quantity for item in rows), ZERO)
        result[stage] = {
            "quantity": _text(quantity),
            "wac_rub": _text(capital / quantity) if quantity > ZERO else None,
            "capital_rub": _text(capital),
            "cost_covered_quantity": _text(covered),
            "coverage_share": _text(covered / quantity) if quantity > ZERO else None,
            "sku_count": len(rows),
            "quality": sorted(set(item.quality for item in rows)),
            "certified": bool(rows) and all(item.certified for item in rows),
            "wb_quantity": _text(sum((item.wb_quantity for item in rows), ZERO)),
            "wb_in_way_to_client": _text(sum((item.wb_in_way_to_client for item in rows), ZERO)),
            "wb_in_way_from_client": _text(sum((item.wb_in_way_from_client for item in rows), ZERO)),
        }
    return result


def _wb_snapshot_quantities(items: Iterable[Mapping[str, Any]]) -> dict[int, Decimal]:
    result: dict[int, Decimal] = {}
    for item in items:
        nm_id = int(item.get("nm_id") or 0)
        if nm_id <= 0:
            continue
        quantity = _decimal(item.get("wb_contour_quantity"))
        if quantity < ZERO:
            raise WarehouseFunctionalError(f"negative official WB contour quantity for nmId {nm_id}")
        result[nm_id] = quantity
    return result


def _daily_wb_cost_row(
    *,
    day: str,
    nm_id: int,
    quantity: Decimal,
    wac: Decimal,
    quality: str,
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    item = {
        "as_of_date": day,
        "nm_id": nm_id,
        "quantity": _text(quantity),
        "wac_rub": _text(wac),
        "capital_rub": _text(quantity * wac),
        "quality": quality,
        "provenance": dict(provenance),
    }
    item["fingerprint"] = "sha256:" + _hash(item)
    return item


def _date_range(start: str, end: str) -> list[str]:
    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    return [
        (first + timedelta(days=offset)).isoformat()
        for offset in range((last - first).days + 1)
    ]


def _replace_current_wb_costs(
    lines: Iterable[WarehouseLine],
    *,
    daily_projection: Iterable[Mapping[str, Any]],
    current_date: str,
) -> list[WarehouseLine]:
    current = {
        int(item["nm_id"]): dict(item)
        for item in daily_projection
        if str(item.get("as_of_date") or "") == current_date
    }
    result: list[WarehouseLine] = []
    for line in lines:
        if line.warehouse_key != STAGE_WB:
            result.append(line)
            continue
        daily = current.get(line.nm_id)
        if daily is None:
            raise WarehouseFunctionalError(
                f"current functional WB balance has no daily WAC replay for nmId {line.nm_id}"
            )
        wac = _decimal(daily["wac_rub"])
        result.append(
            WarehouseLine(
                warehouse_key=line.warehouse_key,
                nm_id=line.nm_id,
                quantity=line.quantity,
                capital=line.quantity * wac,
                cost_covered_quantity=line.cost_covered_quantity,
                quality=str(daily["quality"]),
                provenance={
                    **dict(line.provenance),
                    "daily_wac_replay": dict(daily.get("provenance") or {}),
                    "daily_wac_fingerprint": str(daily.get("fingerprint") or ""),
                },
                certified=line.certified,
                wb_quantity=line.wb_quantity,
                wb_in_way_to_client=line.wb_in_way_to_client,
                wb_in_way_from_client=line.wb_in_way_from_client,
            )
        )
    return result


def _total_summary(summaries: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    quantity = sum((_decimal(item["quantity"]) for item in summaries.values()), ZERO)
    capital = sum((_decimal(item["capital_rub"]) for item in summaries.values()), ZERO)
    return {
        "quantity": _text(quantity),
        "capital_rub": _text(capital),
        "wac_rub": _text(capital / quantity) if quantity > ZERO else None,
    }


def _balance_diff(
    previous: Mapping[tuple[str, int], WarehouseLine],
    current: Iterable[WarehouseLine],
) -> dict[str, Any]:
    current_lookup = {(item.warehouse_key, item.nm_id): item for item in current}
    changed: list[dict[str, Any]] = []
    for warehouse_key, nm_id in sorted(set(previous) | set(current_lookup)):
        before = previous.get((warehouse_key, nm_id))
        after = current_lookup.get((warehouse_key, nm_id))
        before_qty = before.quantity if before else ZERO
        before_capital = before.capital if before else ZERO
        after_qty = after.quantity if after else ZERO
        after_capital = after.capital if after else ZERO
        if before_qty == after_qty and before_capital == after_capital:
            continue
        changed.append(
            {
                "warehouse_key": warehouse_key,
                "nm_id": nm_id,
                "quantity_before": _text(before_qty),
                "quantity_after": _text(after_qty),
                "quantity_delta": _text(after_qty - before_qty),
                "capital_before": _text(before_capital),
                "capital_after": _text(after_capital),
                "capital_delta": _text(after_capital - before_capital),
            }
        )
    return {
        "changed_line_count": len(changed),
        "lines": changed,
    }


def _summary_status(rows: Iterable[Mapping[str, Any]], stage: str, sync: Mapping[str, Any]) -> str:
    selected = [row for row in rows if str(row.get("warehouse_key") or "") == stage]
    if sync.get("last_error"):
        return "stale_error"
    if selected and all(bool(row.get("certified")) for row in selected):
        return "certified"
    return "provisional"


def _balance_public(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **dict(item),
        "certified": bool(item.get("certified")),
        "provenance": _loads(item.get("provenance_json"), {}),
    }


def _cutover_public(row: Mapping[str, Any] | sqlite3.Row) -> dict[str, Any]:
    return {
        "cutover_id": str(row["cutover_id"]),
        "cutover_at": str(row["cutover_at"]),
        "status": str(row["status"]),
        "plan_fingerprint": str(row["plan_fingerprint"]),
        "source_watermarks": _loads(row["source_watermarks_json"], {}),
        "absorbed_supply_revisions": _loads(row["absorbed_supply_revisions_json"], {}),
        "backup": _loads(row["backup_json"], {}),
    }


def _version_public(row: Mapping[str, Any] | sqlite3.Row) -> dict[str, Any]:
    return {
        "version_id": str(row["version_id"]),
        "version_kind": str(row["version_kind"]),
        "effective_at": str(row["effective_at"]),
        "status": str(row["status"]),
        "plan_fingerprint": str(row["plan_fingerprint"]),
        "local_source_digest": str(row["local_source_digest"]),
        "source_watermarks": _loads(row["source_watermarks_json"], {}),
    }


def _document_public(item: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(item)
    return {**value, "provenance": _loads(value.get("provenance_json"), {})}


def _unmatched_public(item: Mapping[str, Any]) -> dict[str, Any]:
    return {**dict(item), "provenance": _loads(item.get("provenance_json"), {})}


def _verify_version(conn: sqlite3.Connection, *, version_id: str, expected: Mapping[str, Any]) -> None:
    stored = conn.execute(
        "SELECT warehouse_key,nm_id,quantity,wac_rub,capital_rub,cost_covered_quantity FROM sheet_vitrina_v1_warehouse_functional_balances WHERE version_id=? ORDER BY warehouse_key,nm_id",
        (version_id,),
    ).fetchall()
    actual = [list(row) for row in stored]
    wanted = [
        [item["warehouse_key"], int(item["nm_id"]), item["quantity"], item["wac_rub"], item["capital_rub"], item["cost_covered_quantity"]]
        for item in sorted(expected["lines"], key=lambda item: (item["warehouse_key"], int(item["nm_id"])))
    ]
    if actual != wanted:
        raise WarehouseFunctionalError("functional apply readback mismatch")
    if len(expected["summaries"]) != 6:
        raise WarehouseFunctionalError("functional apply did not publish six warehouses")


def _supply_revision(row: Mapping[str, Any]) -> str:
    return "sha256:" + _hash(
        {
            "supply_id": row.get("supply_id"),
            "status_id": row.get("status_id"),
            "normalized": _stable_supply_normalized(row.get("normalized_row_json")),
            "goods_hash": row.get("raw_goods_hash"),
            "goods": row.get("raw_goods_json"),
            "updated": row.get("updated_date"),
        }
    )


def _stable_supply_normalized(value: Any) -> Any:
    normalized = _loads(value, value)
    if not isinstance(normalized, Mapping):
        return normalized
    business_state = dict(normalized)
    for key in ("synced_at", "last_list_synced_at", "last_enriched_at"):
        business_state.pop(key, None)
    return business_state


def _supply_revisions(rows: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    return {
        str(row.get("supply_id") or ""): _supply_revision(row)
        for row in rows
        if str(row.get("supply_id") or "")
    }


def _supply_business_date(record: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    for key in (
        "actual_acceptance_date",
        "acceptance_date",
        "fact_date",
        "closed_at",
        "updated_date",
    ):
        value = str(record.get(key) or row.get(key) or "")
        if len(value) >= 10:
            return value[:10]
    return ""


def _validated_financial_expense(
    *,
    document: Mapping[str, Any],
    expense: Mapping[str, Any],
) -> bool:
    """Only reviewed parser output can enter canonical warehouse capital."""

    return (
        str(document.get("parse_status") or "") in {"parsed", "confirmed"}
        and str(expense.get("status") or "parsed") in {"parsed", "confirmed"}
    )


def _counted_cny_operation(operation: Mapping[str, Any]) -> bool:
    """Mirror counted ledger semantics without admitting review documents."""

    operation_status = str(operation.get("status") or "").strip().lower()
    document_status = str(operation.get("document_status") or "").strip().lower()
    return operation_status in {"posted", "needs_review"} and document_status in {"", "posted"}


def _line_value(row: Mapping[str, Any]) -> Decimal:
    amount = _decimal(row.get("amount"))
    if amount > ZERO:
        return amount
    return _decimal(row.get("qty")) * _decimal(row.get("unit_price"))


def _supplier_flow_id(shipment_id: str) -> str:
    return "supplier_flow_" + hashlib.sha256(str(shipment_id).encode("utf-8")).hexdigest()[:20]


def _linear(x: Decimal, x1: Decimal, y1: Decimal, x2: Decimal, y2: Decimal) -> Decimal:
    if x1 == x2:
        return y1
    return y1 + (x - x1) * (y2 - y1) / (x2 - x1)


def _watermark(rows: Iterable[Mapping[str, Any]], key: str, fallback: str = "") -> dict[str, Any]:
    values = [str(row.get(key) or row.get(fallback) or "") for row in rows]
    return {
        "row_count": len(values),
        "max": max(values, default=""),
        "digest": "sha256:" + _hash(list(rows)),
    }


def _stable_id(prefix: str, payload: Any) -> str:
    return f"{prefix}_" + _hash(payload)[:24]


def _fingerprint(payload: Mapping[str, Any]) -> str:
    normalized = {key: value for key, value in payload.items() if key != "plan_fingerprint"}
    return "sha256:" + _hash(normalized)


def _calculation_digest(plan: Mapping[str, Any]) -> str:
    """Hash derived business state without volatile source-capture identities.

    A fresh official WB fetch intentionally has a new ``snapshot_id`` and
    ``fetched_at`` even when every quantity and calculated rouble value is
    unchanged.  Those capture facts remain protected by the external source
    guards and the exact plan fingerprint, but must not make the independent
    semantic recheck report a false calculation drift.
    """

    projection = [
        {
            key: item.get(key)
            for key in (
                "as_of_date",
                "nm_id",
                "quantity",
                "wac_rub",
                "capital_rub",
                "quality",
            )
        }
        for item in plan.get("historical_wb_cost_projection") or []
    ]
    lines = [
        {
            key: item.get(key)
            for key in (
                "warehouse_key",
                "nm_id",
                "quantity",
                "wac_rub",
                "capital_rub",
                "cost_covered_quantity",
                "coverage_share",
                "quality",
                "certified",
                "wb_quantity",
                "wb_in_way_to_client",
                "wb_in_way_from_client",
            )
        }
        for item in plan.get("lines") or []
    ]
    unmatched = [
        {
            key: item.get(key)
            for key in (
                "source_id",
                "source_fingerprint",
                "business_date",
                "nm_id",
                "quantity",
                "matched_quantity",
                "reason",
            )
        }
        for item in plan.get("unmatched_doprinato") or []
    ]
    events = [
        {
            key: item.get(key)
            for key in (
                "event_type",
                "source_id",
                "source_fingerprint",
                "business_date",
                "nm_id",
                "quantity",
                "capital_rub",
            )
        }
        for item in plan.get("new_events") or []
    ]
    movement_documents = [
        {
            key: item.get(key)
            for key in (
                "document_type",
                "warehouse_key",
                "occurred_at",
                "source_id",
                "source_fingerprint",
                "quantity",
                "capital_rub",
            )
        }
        | {
            "lines": [
                {
                    key: line.get(key)
                    for key in ("nm_id", "quantity", "wac_rub", "capital_rub")
                }
                for line in item.get("lines") or []
            ]
        }
        for item in plan.get("movement_documents") or []
    ]
    payload = {
        # The opening map is itself frozen primary evidence; retain its exact
        # provenance and quality in the semantic guard.
        "opening_cost_map": plan.get("opening_cost_map") or [],
        "historical_wb_cost_projection": projection,
        "lines": lines,
        "summaries": plan.get("summaries") or {},
        "unmatched_doprinato": unmatched,
        "new_events": events,
        "movement_documents": movement_documents,
        "invariants": plan.get("invariants") or {},
    }
    return "sha256:" + _hash(payload)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _clone(value: Any) -> Any:
    return json.loads(_json(value))


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value in (None, ""):
        return ZERO
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise WarehouseFunctionalError(f"invalid decimal: {value!r}") from exc
    if not result.is_finite():
        raise WarehouseFunctionalError(f"non-finite decimal: {value!r}")
    return result


def _optional_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return _decimal(value)


def _text(value: Decimal | None) -> str:
    if value is None:
        return ""
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(ONE))
    return format(normalized, "f")


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
