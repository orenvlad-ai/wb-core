"""Current-day management estimate from complete official FBS observations.

This read side has no writer. It does not certify physical FF inventory or
change the six warehouse stages, sale COGS, or historical columns.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable

from packages.application.official_fbs_stock_read import read_complete_official_fbs_stock
from packages.application.own_product_capital import _inventory_cost_stage_evidence
from packages.application.warehouse_functional import _hash, _watermark, _wb_snapshot_integrity
from packages.application.wb_fbs_warehouse_registry import _connect_readonly
from packages.business_time import current_business_date_iso
from packages.contracts.web_vitrina_contract import WebVitrinaContractRow
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope


SOURCE = "official_fbs_management_inventory_v1"
WB_TOTAL = "inventory_wb_total_qty_v1"
FBS_TOTAL = "inventory_fbs_total_qty_v1"
FBS_FACILITY = "inventory_fbs_facility_available_qty_v1:"
COST = "our_wb_unit_cost_rub"
ZERO = Decimal(0)


def _number(value: Any) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or result < ZERO:
        raise ValueError("invalid_nonnegative_operand")
    return result


def build_current_official_fbs_estimate(
    db_path: Path, *, nm_ids: Iterable[int], now: datetime,
    connection: sqlite3.Connection | None = None, quantity_only: bool = False,
) -> dict[str, Any]:
    """Read one SQLite snapshot; all totals use the exact displayed SKU set."""
    universe = sorted(set(int(nm) for nm in nm_ids))
    day = current_business_date_iso(now)
    empty: dict[str, Any] = {"available": False, "date": day, "source": SOURCE}
    if not universe or not Path(db_path).exists():
        return empty
    if connection is None:
        from packages.application.web_vitrina_window_read_context import borrowed_operational_connection
        connection = borrowed_operational_connection(db_path)
    try:
        if connection is not None:
            with localcontext() as context:
                context.prec = 50
                return _build(connection, universe=universe, day=day, now=now, quantity_only=quantity_only)
        with _connect_readonly(Path(db_path)) as conn, localcontext() as context:
            context.prec = 50
            conn.execute("BEGIN")
            return _build(conn, universe=universe, day=day, now=now, quantity_only=quantity_only)
    except (sqlite3.OperationalError, ValueError, TypeError, KeyError, InvalidOperation) as exc:
        from packages.application.web_vitrina_window_read_context import active_window_read_context
        if active_window_read_context() is not None and isinstance(exc, sqlite3.OperationalError):
            raise
        return {**empty, "reason": str(exc)[:160]}


def _build(conn: sqlite3.Connection, *, universe: list[int], day: str,
           now: datetime, quantity_only: bool = False) -> dict[str, Any]:
    if quantity_only:
        return _build_quantities(conn, universe=universe, day=day, now=now)
    result = read_complete_official_fbs_stock(conn, universe=universe, day=day, now=now)
    result["source"] = SOURCE
    stocks = {nm: item["facilities"] for nm, item in result["skus"].items()}
    facilities = result["facilities"]
    # The published exact-date immutable version owns both original operands.
    placeholders = ",".join("?" for _ in universe)
    projections = conn.execute(
        "SELECT nm_id,json_extract(provenance_json,'$.functional_version_id') AS version_id,"
        "json_extract(metrics_json,'$.own_capital_FF_qty') AS ff_quantity,"
        "json_extract(metrics_json,'$.own_capital_FF_capital_rub') AS ff_capital "
        "FROM sheet_vitrina_v1_warehouse_business_projection_current_rows "
        f"WHERE as_of_date=? AND nm_id IN ({placeholders})", (day, *universe),
    ).fetchall()
    versions = {r["version_id"] for r in projections}
    version_id = next(iter(versions)) if len(versions) == 1 and len(projections) == len(universe) else None
    balances = {}
    if version_id:
        version = conn.execute(
            "SELECT * FROM sheet_vitrina_v1_warehouse_functional_versions WHERE version_id=? "
            "AND status='good' AND business_effective_date=?", (version_id, day),
        ).fetchone()
        if version:
            # Read only the compact immutable location evidence, not document payloads.
            for row in conn.execute(
                "SELECT b.nm_id,b.warehouse_key,b.quantity,b.capital_rub,b.wac_rub,"
                "b.cost_covered_quantity,b.quality,b.certified,b.wb_quantity,"
                "(SELECT json_group_array(json_object('locations',json_extract(s.value,'$.locations'))) "
                " FROM json_each(b.provenance_json,'$.source_records') s "
                " WHERE json_type(s.value,'$.locations')='array') AS locations_json "
                "FROM sheet_vitrina_v1_warehouse_functional_balances b "
                f"WHERE b.version_id=? AND b.nm_id IN ({placeholders}) AND b.warehouse_key IN ('ff','wb')",
                (version_id, *universe),
            ):
                balances[(int(row["nm_id"]), row["warehouse_key"])] = dict(row)
            empty_wb_rows = _verified_empty_wb_rows(conn, dict(version)) if any(
                (nm, "wb") not in balances for nm in universe
            ) else {}
            for nm in universe:
                if (nm, "wb") not in balances and nm in empty_wb_rows:
                    balances[(nm, "wb")] = dict(quantity="0", capital_rub="0", cost_covered_quantity="0",
                        wb_quantity="0", wac_rub=None, quality="empty_exact_official_snapshot", certified=0,
                        zero_evidence=empty_wb_rows[nm])
            # Published stage defaults alone are not proof. Bind explicit pool
            # zero rows to the exact complete source captured by this version.
            empty_pool_rows = _verified_empty_pool_rows(conn, dict(version)) if any(
                (nm, "ff") not in balances for nm in universe
            ) else {}
            for projection in projections:
                nm = int(projection["nm_id"])
                if ((nm, "ff") not in balances and projection["ff_quantity"] is not None
                        and projection["ff_capital"] is not None
                        and _number(projection["ff_quantity"]) == ZERO
                        and _number(projection["ff_capital"]) == ZERO
                        and nm in empty_pool_rows and all(q == ZERO for q in stocks[nm].values())
                        and set(stocks[nm]) <= empty_pool_rows[nm]):
                    balances[(nm, "ff")] = dict(quantity="0", capital_rub="0", cost_covered_quantity="0",
                        wac_rub=None, quality="empty_exact_published_stage", certified=0, locations_json="[]")
    result["functional_version_id"] = version_id or ""
    for nm in universe:
        item: dict[str, Any] = {"facilities": stocks[nm], "fbs_quantity": sum(stocks[nm].values(), ZERO)}
        wb = balances.get((nm, "wb"))
        item["stock_quantity"] = (_number(wb["wb_quantity"]) + item["fbs_quantity"]) if wb else None
        if wb and wb.get("zero_evidence"):
            item["wb_zero_evidence"] = wb["zero_evidence"]
        try:
            item.update(_estimate_cost(balances[(nm, "wb")], balances[(nm, "ff")], stocks[nm]))
        except (KeyError, ValueError, TypeError, InvalidOperation) as exc:
            item.update(cost=None, reason=str(exc)[:160])
        result["skus"][nm] = item
    result["total"] = {
        "facilities": {f: sum((stocks[nm][f] for nm in universe), ZERO) for f in facilities},
        "fbs_quantity": sum((r["fbs_quantity"] for r in result["skus"].values()), ZERO),
        "cost": None,
        "stock_quantity": (sum((r["stock_quantity"] for r in result["skus"].values()), ZERO)
                           if all(r["stock_quantity"] is not None for r in result["skus"].values()) else None),
        "wb_zero_evidence": {str(nm): row["wb_zero_evidence"] for nm, row in result["skus"].items()
                             if row.get("wb_zero_evidence")},
    }
    if all(r.get("cost") is not None or (r.get("quantity") == ZERO and r.get("capital") == ZERO)
           for r in result["skus"].values()):
        quantity = sum((r["quantity"] for r in result["skus"].values()), ZERO)
        capital = sum((r["capital"] for r in result["skus"].values()), ZERO)
        result["total"].update(quantity=quantity, capital=capital, cost=capital / quantity if quantity else None)
    return result


def add_official_wb_quantities(estimate: dict, wb_source: dict) -> dict:
    """Join independently verified, same-date and same-roster physical WB."""
    result = deepcopy(estimate)
    if wb_source['date'] != result['date'] or set(wb_source['skus']) != {str(nm) for nm in result['skus']}:
        raise ValueError('wb_fbs_quantity_date_scope_mismatch')
    result.update(available=True,quantity_only=True,wb_source=deepcopy(wb_source))
    for nm,item in result['skus'].items():
        item['wb_quantity'] = _number(wb_source['skus'][str(nm)])
        if item.get('fbs_quantity') is not None:
            item['stock_quantity'] = item['wb_quantity'] + item['fbs_quantity']
    result['total']['wb_quantity'] = sum((item['wb_quantity'] for item in result['skus'].values()),ZERO)
    if all(item.get('stock_quantity') is not None for item in result['skus'].values()):
        result['total']['stock_quantity'] = sum((item['stock_quantity'] for item in result['skus'].values()),ZERO)
    return result


def _build_quantities(conn, *, universe, day, now):
    from packages.application.official_wb_physical_read import read_official_wb_physical_snapshot
    try:
        result = read_complete_official_fbs_stock(conn, universe=universe, day=day, now=now)
        result.update(fbs_available=True)
        for item in result['skus'].values():
            item['fbs_quantity'] = sum(item['facilities'].values(),ZERO)
        result['total'] = {
            'facilities': {f:sum((item['facilities'][f] for item in result['skus'].values()),ZERO) for f in result['facilities']},
            'fbs_quantity':sum((item['fbs_quantity'] for item in result['skus'].values()),ZERO)}
    except (sqlite3.OperationalError, ValueError, TypeError, KeyError, InvalidOperation):
        result = dict(available=False,date=day,facilities=[],skus={nm:{} for nm in universe},total={},
            generation_id='',generation_digest='',captured_at='',fbs_available=False)
    result.update(quantity_only=True,source=SOURCE,functional_version_id='')
    try:
        wb = read_official_wb_physical_snapshot(conn,universe=universe,day=day,now=now)
        result = add_official_wb_quantities(result,wb)
    except (sqlite3.OperationalError, ValueError, TypeError, KeyError, InvalidOperation):
        pass  # No WB operand is inferred from its cost or contour quantity.
    return result


def _verified_empty_wb_rows(conn: sqlite3.Connection, version: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """A missing balance is zero only within this version's complete dense WB capture."""
    try:
        rows = conn.execute(
            "SELECT * FROM sheet_vitrina_v1_warehouse_wb_snapshots WHERE version_id=?",
            (version["version_id"],),
        ).fetchall()
        if len(rows) != 1:
            return {}
        snapshot = dict(rows[0])
        watermarks = json.loads(version["source_watermarks_json"])
        expected = watermarks.get("wb_snapshot", {}) if isinstance(watermarks, dict) else {}
        if not isinstance(expected, dict):
            return {}
        requested = json.loads(snapshot["requested_nm_ids_json"])
        items = json.loads(snapshot["items_json"])
        raw = json.loads(snapshot["raw_rows_json"])
        offsets = json.loads(snapshot["page_offsets_json"])
        if not all(isinstance(value, list) for value in (requested, items, raw, offsets)):
            return {}
        if (not requested or any(type(nm) is not int or nm <= 0 for nm in requested)
                or len(set(requested)) != len(requested)
                or any(not isinstance(row, dict) for row in [*items, *raw])
                or not offsets or any(type(offset) is not int or offset < 0 for offset in offsets)
                or offsets[0] != 0 or len(offsets) != snapshot["page_count"]
                or snapshot["snapshot_date"] != version["business_effective_date"]
                or snapshot["pagination_complete"] != 1 or expected.get("pagination_complete") is not True
                or not expected.get("snapshot_id")
                or expected.get("fetched_at") != snapshot["fetched_at"]
                or expected.get("requested_count") != len(requested)
                or expected.get("raw_row_count") != len(raw) or snapshot["raw_row_count"] != len(raw)):
            return {}
        digest = "sha256:" + _hash(sorted(raw, key=lambda row: json.dumps(
            row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))
        if not (digest == snapshot["raw_rows_digest"] == expected.get("digest")):
            return {}
        canonical: dict[int, dict[str, Any]] = {}
        for item in items:
            nm = item["nm_id"]
            if type(nm) is not int or nm not in requested or nm in canonical:
                return {}
            quantities = [_number(item[key]) for key in ("quantity", "in_way_to_client", "in_way_from_client")]
            if sum(quantities, ZERO) != _number(item["wb_contour_quantity"]):
                return {}
            canonical[nm] = item
        if set(canonical) != set(requested):
            return {}
        for row in raw:
            if type(row.get("nmId")) is not int or row["nmId"] not in canonical:
                return {}
            for key in ("quantity", "inWayToClient", "inWayFromClient"):
                _number(row[key])
        integrity = _wb_snapshot_integrity(snapshot)
        if (not integrity["raw_to_canonical_mapping_matches"] or integrity["exact_duplicate_count"]
                or integrity["source_key_duplicate_count"]):
            return {}
        return {nm: {"snapshot_id": snapshot["snapshot_id"], "raw_rows_digest": digest,
                     "snapshot_date": snapshot["snapshot_date"], "fetched_at": snapshot["fetched_at"],
                     "basis": "requested_in_complete_dense_official_snapshot"}
                for nm, item in canonical.items() if _number(item["wb_contour_quantity"]) == ZERO}
    except (sqlite3.OperationalError, ValueError, TypeError, KeyError, InvalidOperation):
        return {}


def _verified_empty_pool_rows(conn: sqlite3.Connection, version: dict[str, Any]) -> dict[int, set[str]]:
    expected = json.loads(version["source_watermarks_json"]).get("ff_pool_detail", {})
    if not expected.get("digest"):
        return {}
    rows = [dict(row) for row in conn.execute(
        "SELECT facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,wac_rub,source_watermark,updated_at "
        "FROM sheet_vitrina_v1_ff_pool_balances ORDER BY facility_id,pool,nm_id"
    )]
    if _watermark(rows, "updated_at") != expected:
        return {}
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(int(row["nm_id"]), []).append(row)
    return {nm: {r["facility_id"] for r in items if r["pool"] == "FBS"}
            for nm, items in grouped.items()
            if all(_number(r["quantity"]) == ZERO and _number(r["capital_rub"]) == ZERO for r in items)}


def _estimate_cost(wb: dict[str, Any], ff: dict[str, Any], stocks: dict[str, Decimal]) -> dict[str, Any]:
    for stage in (wb, ff):
        q, k = _number(stage["quantity"]), _number(stage["capital_rub"])
        if (_number(stage["cost_covered_quantity"]) != q or (q == ZERO and k != ZERO)
                or (q > ZERO and k <= ZERO)):
            raise ValueError("incomplete_stage_cost_coverage")
    ff = {**ff, "provenance_json": json.dumps({"source_records": json.loads(ff["locations_json"])})}
    evidence = _inventory_cost_stage_evidence(ff, public_stage="FF")
    proven_empty = (_number(ff["quantity"]) == ZERO and _number(ff["capital_rub"]) == ZERO
                    and all(q == ZERO for q in stocks.values()))
    if evidence["location_status"] != "exact" and not proven_empty:
        raise ValueError("ff_location_parity_unproven")
    locations = {(r["facility_id"], r["pool"]): r for r in evidence["locations"]}
    if len(locations) != len(evidence["locations"]):
        raise ValueError("duplicate_ff_location")
    if any(pool == "FBS" and f not in stocks for f, pool in locations):
        raise ValueError("fbs_facility_outside_official_scope")
    quantity, capital = _number(wb["quantity"]), _number(wb["capital_rub"])
    fbs_capital = ZERO
    for facility, official_quantity in stocks.items():
        # A confirmed zero needs no invented cost basis.
        if official_quantity == ZERO:
            continue
        basis = locations.get((facility, "FBS"))
        if not basis or basis["wac_rub"] is None or _number(basis["wac_rub"]) <= ZERO:
            raise ValueError("positive_fbs_stock_without_same_facility_sku_basis")
        fbs_capital += official_quantity * _number(basis["wac_rub"])
    for (_, pool), location in locations.items():
        if pool == "FBO":
            quantity += _number(location["quantity"])
            capital += _number(location["capital_rub"])
    quantity += sum(stocks.values(), ZERO)
    capital += fbs_capital
    retained = _number(wb["wac_rub"]) if wb.get("wac_rub") is not None else None
    return {"quantity": quantity, "capital": capital, "fbs_capital": fbs_capital,
            "cost": capital / quantity if quantity else retained, "reason": ""}


def is_official_fbs_quantity_metric(metric: str) -> bool:
    key = metric.removeprefix("total_")
    return key == FBS_TOTAL or key.startswith(FBS_FACILITY)


def is_official_inventory_quantity_metric(metric: str) -> bool:
    return is_official_fbs_quantity_metric(metric) or metric.removeprefix('total_') in {WB_TOTAL,'stock_total'}


def is_frozen_official_fbs_quantity(metric: str, day: str, cell: dict) -> bool:
    """Read-side provenance for an already materialized exact-date quantity."""
    if not (is_official_fbs_quantity_metric(metric) and cell.get("source") == SOURCE
            and cell.get("source_as_of_date") == day
            and cell.get("quality_state") == "official_declared_stock"
            and cell.get("source_generation_id") and cell.get("source_digest")
            and cell.get("captured_at")):
        return False
    try:
        return _number(cell["management_value"]) >= ZERO
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return False


def is_frozen_official_inventory_quantity(metric: str, day: str, cell: dict) -> bool:
    if is_frozen_official_fbs_quantity(metric,day,cell):
        return True
    key = metric.removeprefix('total_')
    wb = cell.get('wb_source') or {}
    if not (key in {WB_TOTAL,'stock_total'} and cell.get('source') == SOURCE
            and cell.get('source_as_of_date') == day and wb.get('date') == day
            and wb.get('quantity_basis') == 'physical_quantity_only'
            and all(wb.get(k) for k in ('snapshot_id','source_digest','version_id','observed_at'))
            and cell.get('quality_state') == ('official_wb_physical_stock' if key == WB_TOTAL else 'official_combined_stock')):
        return False
    try:
        value = _number(cell['management_value'])
        if key == WB_TOTAL:
            return value == _number(cell['wb_component_value'])
        return (value == _number(cell['wb_component_value'])+_number(cell['fbs_component_value'])
            and bool(cell.get('source_generation_id') and cell.get('source_digest') and cell.get('captured_at')))
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return False


def _quantity_keys(estimate):
    keys = ([FBS_TOTAL,*(FBS_FACILITY+f for f in estimate['facilities'])]
            if estimate.get('fbs_available', bool(estimate.get('generation_id'))) else [])
    if estimate.get('wb_source'):
        keys.append(WB_TOTAL)
        if 'stock_quantity' in estimate['total']:
            keys.append('stock_total')
    return keys


def apply_current_official_fbs_estimate(
    rows: Iterable[WebVitrinaContractRow], *, estimate: dict[str, Any],
    compact_operands: dict[str, dict[str, Any]] | None = None,
) -> list[WebVitrinaContractRow]:
    """Map precomputed server operands to existing cells, only on their date."""
    if not estimate.get("available"):
        return list(rows)
    result = []
    day = estimate["date"]
    for row in rows:
        key = row.metric_key.removeprefix("total_")
        if day not in row.values_by_date or not (key in {FBS_TOTAL, COST, "stock_total"} or key.startswith(FBS_FACILITY)
                or (estimate.get("quantity_only") and key == WB_TOTAL)):
            result.append(row)
            continue
        if estimate.get("quantity_only") and key not in _quantity_keys(estimate):
            result.append(row)
            continue
        item = estimate["total"] if row.scope_kind == "TOTAL" else estimate["skus"].get(row.nm_id)
        if item is None:
            result.append(row)
            continue
        value, presentation = _official_estimated_cell(item, key=key, estimate=estimate, day=day)
        result.append(replace(row, values_by_date={**row.values_by_date, day: value},
                              presentation_by_date={**row.presentation_by_date, day: presentation}))
    if compact_operands is not None:
        for row_id, operand in compact_operands.items():
            if "|" not in row_id or day not in operand.get("values_by_date", {}):
                continue
            scope, metric = row_id.split("|", 1)
            key = metric.removeprefix("total_")
            if not (key in {FBS_TOTAL, COST, "stock_total"} or key.startswith(FBS_FACILITY)
                or (estimate.get("quantity_only") and key == WB_TOTAL)):
                continue
            if estimate.get("quantity_only") and key not in _quantity_keys(estimate):
                continue
            if scope == "TOTAL":
                item = estimate["total"]
            elif scope.startswith("SKU:") and scope[4:].isdigit():
                item = estimate["skus"].get(int(scope[4:]))
            else:
                continue
            if item is None:
                continue
            value, presentation = _official_estimated_cell(item, key=key, estimate=estimate, day=day)
            operand["values_by_date"][day] = value
            operand.setdefault("presentation_by_date", {})[day] = presentation
    return result


def _official_estimated_cell(
    item: dict[str, Any], *, key: str, estimate: dict[str, Any], day: str,
) -> tuple[Any, dict[str, Any]]:
    if estimate.get('quantity_only') and key in {WB_TOTAL,'stock_total'}:
        wb = estimate['wb_source']
        value = item['wb_quantity'] if key == WB_TOTAL else item['stock_quantity']
        reason = ('Физический остаток на складах WB; товар в пути не включён.' if key == WB_TOTAL else
                  'Физический остаток WB плюс заявленный в WB остаток FBS; датированные независимые снимки.')
        presentation = dict(source=SOURCE,source_as_of_date=day,state='unconfirmed',tone='warning',
            quality_state='official_wb_physical_stock' if key == WB_TOTAL else 'official_combined_stock',
            quality_label='Остаток по WB',reason=reason,quality_reason=reason,management_value=str(value),
            wb_source={k:wb[k] for k in ('date','snapshot_id','source_digest','version_id','observed_at','quantity_basis')},
            wb_component_value=str(item['wb_quantity']),source_generation_id=estimate.get('generation_id',''),
            source_digest=estimate.get('generation_digest',''),captured_at=estimate.get('captured_at','') or wb['observed_at'])
        if key == 'stock_total':
            presentation['fbs_component_value'] = str(item['fbs_quantity'])
        return float(value),presentation
    value = item.get("stock_quantity") if key == "stock_total" else item.get("cost") if key == COST else (
        item["fbs_quantity"] if key == FBS_TOTAL else item["facilities"].get(key[len(FBS_FACILITY):]))
    reason = ("Управленческая оценка: капитал WB и FBO FF плюс остатки FBS из WB × "
                  "складская себестоимость того же SKU; деление на их общее количество. "
                  "Учётные стадии товарного капитала используют физический складской журнал."
                  if key == COST else "Физический остаток WB плюс заявленные WB остатки FBS."
                  if key == "stock_total" else "Остаток, заявленный в WB; полный официальный снимок FBS.")
    if value is None:
        reason = "Недостаточно согласованных данных для оценки себестоимости; неизвестное не считается нулём."
    presentation = {
            "state": "unconfirmed" if value is not None else "unavailable", "tone": "warning",
            "source": SOURCE, "quality_state": "management_estimate" if key == COST else "official_declared_stock",
            "quality_label": "Управленческая оценка" if key == COST else "Остаток по WB",
            "reason": reason, "quality_reason": reason,
            "source_generation_id": estimate["generation_id"], "source_digest": estimate["generation_digest"],
            "source_as_of_date": day, "captured_at": estimate["captured_at"],
            "functional_version_id": estimate["functional_version_id"],
            "management_value": str(value) if value is not None else "",
        }
    if item.get("wb_zero_evidence"):
        presentation["wb_zero_evidence"] = item["wb_zero_evidence"]
    if key == "stock_total":
        presentation["wb_component_value"] = (
                str(item["stock_quantity"] - item["fbs_quantity"])
                if item.get("stock_quantity") is not None else ""
            )
        presentation["fbs_component_value"] = str(item["fbs_quantity"])
    return (float(value) if value is not None else ""), presentation


def materialize_current_official_fbs_estimate(
    plan: SheetVitrinaV1Envelope, *, estimate: dict[str, Any],
    previous_presentation: dict[str, Any] | None = None,
) -> SheetVitrinaV1Envelope:
    """Use the normal snapshot writer; freeze value + provenance together."""
    if not estimate.get("available") and not any(
        cell.get("source") == SOURCE for by_date in (previous_presentation or {}).values()
        for cell in by_date.values()
    ):
        return plan
    metadata = dict(plan.metadata or {})
    presentation = {key: dict(value) for key, value in metadata.get("server_cell_presentation", {}).items()}
    for row_id, by_date in (previous_presentation or {}).items():
        for day, cell in by_date.items():
            if (day in plan.date_columns and cell.get("source") == SOURCE
                    and (not estimate.get("quantity_only") or is_official_inventory_quantity_metric(row_id.split("|")[-1]))):
                presentation.setdefault(row_id, {})[day] = dict(cell)
    if estimate.get("available") and estimate["date"] in plan.date_columns:
        day = estimate["date"]
        skeletons = []
        for nm in [None, *estimate["skus"]]:
            scope = "TOTAL" if nm is None else f"SKU:{nm}"
            keys = (_quantity_keys(estimate) if estimate.get("quantity_only") else
                    [FBS_TOTAL, *(FBS_FACILITY + f for f in estimate["facilities"])])
            if not estimate.get("quantity_only"):
                keys.extend([COST, "stock_total"])
            for key in keys:
                metric = "total_" + key if nm is None else key
                skeletons.append(WebVitrinaContractRow(
                    row_id=f"{scope}|{metric}", row_order=0, scope_kind="TOTAL" if nm is None else "SKU",
                    scope_key=scope, scope_label="", metric_key=metric, metric_label="", row_last_updated_at="",
                    section="", group=None, nm_id=nm, format=None, values_by_date={day: ""},
                ))
        for row in apply_current_official_fbs_estimate(skeletons, estimate=estimate):
            presentation.setdefault(row.row_id, {})[day] = row.presentation_by_date[day]
    metadata["server_cell_presentation"] = presentation
    sheets = []
    for sheet in plan.sheets:
        if sheet.sheet_name != "DATA_VITRINA":
            sheets.append(sheet)
            continue
        new_rows = []
        for row in sheet.rows:
            updated = list(row)
            if (sheet.sheet_name == "DATA_VITRINA" and len(row) > 1
                    and (not estimate.get("quantity_only") or is_official_inventory_quantity_metric(str(row[1]).split("|")[-1]))):
                for day in plan.date_columns:
                    cell = presentation.get(str(row[1]), {}).get(day, {})
                    if cell.get("source") != SOURCE:
                        continue
                    index = plan.date_columns.index(day) + 2
                    if index < len(updated):
                        value = cell["management_value"]
                        updated[index] = float(value) if value != "" else ""
            new_rows.append(updated)
        if sheet.sheet_name == "DATA_VITRINA":
            existing = {str(row[1]) for row in new_rows if len(row) > 1}
            for row_id, by_date in presentation.items():
                if estimate.get("quantity_only") and not is_official_inventory_quantity_metric(row_id.split("|")[-1]):
                    continue
                if row_id in existing or not any(cell.get("source") == SOURCE for cell in by_date.values()):
                    continue
                values = []
                for day in plan.date_columns:
                    cell = by_date.get(day, {})
                    value = cell.get("management_value", "") if cell.get("source") == SOURCE else ""
                    values.append(float(value) if value != "" else "")
                new_rows.append(["Остатки и себестоимость: оценка по WB", row_id, *values])
        start = re.fullmatch(r"[A-Z]+([1-9][0-9]*)", sheet.write_start_cell)
        if start is None:
            raise ValueError("unsupported_data_sheet_start_cell")
        sheets.append(replace(sheet, rows=new_rows, row_count=len(new_rows),
                              write_rect=re.sub(r"\d+$", str(len(new_rows) + int(start[1])), sheet.write_rect)))
    return replace(plan, metadata=metadata, sheets=sheets)


def load_materialized_official_fbs_presentation(
    conn: sqlite3.Connection, *, bundle_version: str, dates: list[str],
) -> dict[str, Any]:
    """Carry exact column dates across changes to the outer snapshot key."""
    if not dates:
        return {}
    placeholders = ",".join("?" for _ in dates)
    rows = conn.execute(
        "SELECT cells.key,days.key,days.value FROM sheet_vitrina_v1_ready_snapshots snapshot,"
        "json_each(snapshot.plan_json,'$.metadata.server_cell_presentation') cells,"
        "json_each(cells.value) days WHERE snapshot.bundle_version=? "
        f"AND days.key IN ({placeholders}) AND json_extract(days.value,'$.source')=? "
        "ORDER BY snapshot.refreshed_at DESC,snapshot.as_of_date DESC",
        (bundle_version, *dates, SOURCE),
    )
    result: dict[str, Any] = {}
    for row_id, day, value in rows:
        result.setdefault(row_id, {}).setdefault(day, json.loads(value))
    return result


def restore_materialized_official_fbs_estimates(
    rows: Iterable[WebVitrinaContractRow], *, presentation: dict[str, Any], quantity_only: bool = False,
) -> list[WebVitrinaContractRow]:
    """Restore only frozen exact-date observations, never today's stock backward."""
    result = []
    for row in rows:
        values, cells = dict(row.values_by_date), dict(row.presentation_by_date)
        for day, cell in presentation.get(row.row_id, {}).items():
            if (day in values and cell.get("source") == SOURCE and "management_value" in cell
                    and (not quantity_only or is_frozen_official_inventory_quantity(row.metric_key, day, cell))):
                value = cell["management_value"]
                values[day] = float(value) if value != "" else ""
                cells[day] = dict(cell)
        result.append(replace(row, values_by_date=values, presentation_by_date=cells))
    return result


def restore_frozen_wb_components(rows, *, presentation):
    """Project a missing generated WB row from an already frozen physical component.

    Older ready envelopes can contain stock_total but no WB metric row. The
    complete combined observation already freezes both components and their
    source identities; no accounting or current-day source is consulted here.
    Explicit WB presentation, even invalid, is never replaced by this fallback.
    """
    result = []
    for row in rows:
        if row.metric_key.removeprefix('total_') != WB_TOTAL:
            result.append(row)
            continue
        scope = row.row_id.split('|', 1)[0]
        if scope != 'TOTAL' and not (scope.startswith('SKU:') and scope[4:].isdigit()):
            result.append(row)
            continue
        combined_key = 'total_stock_total' if scope == 'TOTAL' else 'stock_total'
        combined = presentation.get(scope + '|' + combined_key, {})
        values, cells = dict(row.values_by_date), dict(row.presentation_by_date)
        for day in values:
            if day in presentation.get(row.row_id, {}):
                continue
            proof = combined.get(day, {})
            if not is_frozen_official_inventory_quantity(combined_key, day, proof):
                continue
            try:
                wb_value = _number(proof['wb_component_value'])
                components = [wb_value, _number(proof['fbs_component_value']), _number(proof['management_value'])]
                if any(v != v.to_integral_value() for v in components):
                    continue
                for stamp in (proof['wb_source']['observed_at'], proof['captured_at']):
                    observed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                    if observed.tzinfo is None or current_business_date_iso(observed) != day:
                        raise ValueError('cross_date_quantity_component')
                if values[day] not in ('', None) and _number(values[day]) != wb_value:
                    continue
            except (KeyError, ValueError, TypeError, InvalidOperation, AttributeError):
                continue
            cell = deepcopy(proof)
            cell.pop('fbs_component_value', None)
            reason = 'Физический остаток на складах WB; товар в пути не включён.'
            cell.update(management_value=str(wb_value), quality_state='official_wb_physical_stock',
                        reason=reason, quality_reason=reason)
            values[day], cells[day] = float(wb_value), cell
        result.append(replace(row, values_by_date=values, presentation_by_date=cells))
    return result


def restore_official_quantity_groups(rows, *, groups, config, metrics, runtime, day):
    """Reproject only exact dated quantity GROUP cells after the accounting mask.

    Use the canonical group aggregator, but require exact official operands for
    every member. This cannot supply valuation or relax the accounting mask.
    """
    from packages.application.web_vitrina_group_blocks import include_group_rows, accepted_number
    quantities = [r for r in rows if is_official_inventory_quantity_metric(r.metric_key)]
    projected = include_group_rows(quantities, groups=groups, config=config, metrics=metrics,
        formulas={}, dates=[day], runtime=runtime, today=day,
        parameters3=lambda _: None, parameters4=lambda _: None)
    sku = {(r.nm_id, r.metric_key): r for r in quantities if r.scope_kind == 'SKU'}
    replacements = {}
    for row in projected:
        if row.scope_kind != 'GROUP':
            continue
        members = sorted({c.nm_id for c in config if c.enabled and c.group == row.group})
        key = row.metric_key.removeprefix('total_')
        operands = [sku.get((nm, key)) for nm in members]
        if not members or any(r is None or accepted_number(r, day) is None
                or not is_frozen_official_inventory_quantity(key, day, r.presentation_by_date.get(day, {}))
                for r in operands):
            continue
        if any(_number(accepted_number(r, day)) != _number(r.presentation_by_date[day]['management_value'])
                for r in operands):
            continue
        proof = {str(r.nm_id): deepcopy(r.presentation_by_date[day]) for r in operands}
        cell = {**row.presentation_by_date[day],
            'quantity_aggregation': 'complete_official_group_members_v1',
            'source_as_of_date': day, 'member_nm_ids': members, 'member_sources': proof}
        replacements[row.row_id] = (row.values_by_date[day], cell)
    return [replace(r, values_by_date={**r.values_by_date, day: replacements[r.row_id][0]},
                    presentation_by_date={**r.presentation_by_date, day: replacements[r.row_id][1]})
            if r.row_id in replacements and day in r.values_by_date else r for r in rows]
