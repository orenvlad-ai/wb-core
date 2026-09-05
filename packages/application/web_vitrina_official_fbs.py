"""Current-day management estimate from complete official FBS observations.

This read side has no writer. It does not certify physical FF inventory or
change the six warehouse stages, sale COGS, or historical columns.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable

from packages.application.own_product_capital import _inventory_cost_stage_evidence
from packages.application.warehouse_functional import _watermark
from packages.application.wb_fbs_warehouse_registry import (
    _complete_source_generation, _connect_readonly, _freshness,
    REGISTRY_RUNS_TABLE, STOCK_RUNS_TABLE, STOCK_ROWS_TABLE,
    WAREHOUSE_MAPPINGS_TABLE, FACILITIES_TABLE,
)
from packages.business_time import current_business_date_iso
from packages.contracts.web_vitrina_contract import WebVitrinaContractRow
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope


SOURCE = "official_fbs_management_inventory_v1"
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
) -> dict[str, Any]:
    """Read one SQLite snapshot; all totals use the exact displayed SKU set."""
    universe = sorted(set(int(nm) for nm in nm_ids))
    day = current_business_date_iso(now)
    empty: dict[str, Any] = {"available": False, "date": day, "source": SOURCE}
    if not universe or not Path(db_path).exists():
        return empty
    try:
        with _connect_readonly(Path(db_path)) as conn, localcontext() as context:
            context.prec = 50
            conn.execute("BEGIN")
            return _build(conn, universe=universe, day=day, now=now)
    except (sqlite3.OperationalError, ValueError, TypeError, KeyError, InvalidOperation) as exc:
        return {**empty, "reason": str(exc)[:160]}


def _build(conn: sqlite3.Connection, *, universe: list[int], day: str,
           now: datetime) -> dict[str, Any]:
    generation = _complete_source_generation(conn)
    if not generation.get("complete"):
        raise ValueError("complete_official_generation_unavailable")
    run = conn.execute(f"SELECT * FROM {REGISTRY_RUNS_TABLE} WHERE run_id=?",
                       (generation["generation_id"],)).fetchone()
    scope = json.loads(run["warehouse_scope_json"])
    catalog = json.loads(run["catalog_scope_json"])
    if not scope.get("complete") or not catalog.get("complete"):
        raise ValueError("incomplete_scope")
    facilities = {str(w["facility_id"]): w for w in scope["warehouses"]}
    if len(facilities) != scope["warehouse_count"]:
        raise ValueError("ambiguous_facilities")
    stocks: dict[int, dict[str, Decimal]] = {nm: {} for nm in universe}
    identities = None
    captured = []
    for facility, warehouse in facilities.items():
        mapping = conn.execute(
            f"SELECT m.mapping_id FROM {WAREHOUSE_MAPPINGS_TABLE} m "
            f"JOIN {FACILITIES_TABLE} f ON f.facility_id=m.facility_id "
            "WHERE m.mapping_id=? AND m.facility_id=? AND m.active=1 AND f.active=1",
            (warehouse["mapping_id"], facility),
        ).fetchone()
        if mapping is None:
            raise ValueError("mapping_changed")
        stock_run = conn.execute(
            f"SELECT * FROM {STOCK_RUNS_TABLE} WHERE registry_run_id=? AND seller_warehouse_id=?",
            (run["run_id"], warehouse["seller_warehouse_id"]),
        ).fetchone()
        timestamp = str(stock_run["snapshot_at"])
        if (current_business_date_iso(datetime.fromisoformat(timestamp.replace("Z", "+00:00"))) != day
                or _freshness(timestamp, now.isoformat()) != "fresh"):
            raise ValueError("official_snapshot_not_fresh_current_day")
        captured.append(timestamp)
        stock_rows = conn.execute(
            f"SELECT chrt_id,nm_id,amount,provenance FROM {STOCK_ROWS_TABLE} WHERE run_id=?",
            (stock_run["run_id"],),
        ).fetchall()
        identity = {(int(r["chrt_id"]), int(r["nm_id"])) for r in stock_rows}
        if (len(identity) != int(catalog["requested_chrt_count"])
                or (identities is not None and identity != identities)):
            raise ValueError("dense_identity_mismatch")
        identities = identity
        for row in stock_rows:
            if row["provenance"] not in {"explicit_wb_row", "omitted_requested_zero"}:
                raise ValueError("unsupported_stock_evidence")
            quantity = _number(row["amount"])
            if row["provenance"] == "omitted_requested_zero" and quantity != ZERO:
                raise ValueError("invalid_omission_zero")
            nm = int(row["nm_id"])
            if nm in stocks:
                stocks[nm][facility] = stocks[nm].get(facility, ZERO) + quantity
    if any(set(value) != set(facilities) for value in stocks.values()):
        raise ValueError("displayed_sku_outside_complete_catalog")
    result: dict[str, Any] = {
        "available": True, "date": day, "source": SOURCE,
        "generation_id": run["run_id"], "generation_digest": run["generation_digest"],
        "captured_at": min(captured), "catalog_sku_count": catalog["active_nm_id_count"],
        "sku_count": len(universe), "facilities": sorted(facilities), "skus": {},
    }
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
    }
    if all(r.get("cost") is not None for r in result["skus"].values()):
        quantity = sum((r["quantity"] for r in result["skus"].values()), ZERO)
        capital = sum((r["capital"] for r in result["skus"].values()), ZERO)
        result["total"].update(quantity=quantity, capital=capital, cost=capital / quantity if quantity else None)
    return result


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


def apply_current_official_fbs_estimate(
    rows: Iterable[WebVitrinaContractRow], *, estimate: dict[str, Any],
) -> list[WebVitrinaContractRow]:
    """Map precomputed server operands to existing cells, only on their date."""
    if not estimate.get("available"):
        return list(rows)
    result = []
    day = estimate["date"]
    for row in rows:
        key = row.metric_key.removeprefix("total_")
        if day not in row.values_by_date or not (key in {FBS_TOTAL, COST, "stock_total"} or key.startswith(FBS_FACILITY)):
            result.append(row)
            continue
        item = estimate["total"] if row.scope_kind == "TOTAL" else estimate["skus"].get(row.nm_id)
        if item is None:
            result.append(row)
            continue
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
        result.append(replace(row, values_by_date={**row.values_by_date, day: float(value) if value is not None else ""},
                              presentation_by_date={**row.presentation_by_date, day: presentation}))
    return result


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
            if day in plan.date_columns and cell.get("source") == SOURCE:
                presentation.setdefault(row_id, {})[day] = dict(cell)
    if estimate.get("available") and estimate["date"] in plan.date_columns:
        day = estimate["date"]
        skeletons = []
        for nm in [None, *estimate["skus"]]:
            scope = "TOTAL" if nm is None else f"SKU:{nm}"
            for key in [COST, FBS_TOTAL, "stock_total", *(FBS_FACILITY + f for f in estimate["facilities"])]:
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
        new_rows = []
        for row in sheet.rows:
            updated = list(row)
            if sheet.sheet_name == "DATA_VITRINA" and len(row) > 1:
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
                if row_id in existing or not any(cell.get("source") == SOURCE for cell in by_date.values()):
                    continue
                values = []
                for day in plan.date_columns:
                    cell = by_date.get(day, {})
                    value = cell.get("management_value", "") if cell.get("source") == SOURCE else ""
                    values.append(float(value) if value != "" else "")
                new_rows.append(["Остатки и себестоимость: оценка по WB", row_id, *values])
        sheets.append(replace(sheet, rows=new_rows, row_count=len(new_rows),
                              write_rect=re.sub(r"\d+$", str(len(new_rows) + 1), sheet.write_rect)))
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
    rows: Iterable[WebVitrinaContractRow], *, presentation: dict[str, Any],
) -> list[WebVitrinaContractRow]:
    """Restore only frozen exact-date observations, never today's stock backward."""
    result = []
    for row in rows:
        values, cells = dict(row.values_by_date), dict(row.presentation_by_date)
        for day, cell in presentation.get(row.row_id, {}).items():
            if day in values and cell.get("source") == SOURCE and "management_value" in cell:
                value = cell["management_value"]
                values[day] = float(value) if value != "" else ""
                cells[day] = dict(cell)
        result.append(replace(row, values_by_date=values, presentation_by_date=cells))
    return result
