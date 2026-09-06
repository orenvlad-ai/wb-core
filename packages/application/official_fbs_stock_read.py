"""Read-only complete official FBS quantities, independent of lifecycle and costing."""
from datetime import datetime
from decimal import Decimal
import json
import sqlite3
from typing import Any
from packages.business_time import current_business_date_iso
from packages.application.wb_fbs_warehouse_registry import (
    _complete_source_generation, _freshness, REGISTRY_RUNS_TABLE, STOCK_RUNS_TABLE,
    STOCK_ROWS_TABLE, WAREHOUSE_MAPPINGS_TABLE, FACILITIES_TABLE,
)
OFFICIAL_STOCK_SOURCE = "official_fbs_stock_snapshot_v1"
ZERO = Decimal(0)
def _number(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < ZERO or result != result.to_integral_value():
        raise ValueError("invalid_nonnegative_integer_stock")
    return result

def read_complete_official_fbs_stock(conn: sqlite3.Connection, *, universe: list[int] | None,
                                    day: str, now: datetime) -> dict:
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
    stocks: dict[int, dict[str, Decimal]] = {nm: {} for nm in universe or []}
    identities = None
    captured = []
    facility_evidence = {}
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
        facility_evidence[facility] = {**warehouse, "captured_at": timestamp,
            "stock_run_id": stock_run["run_id"], "stock_digest": stock_run["source_digest"]}
        stock_rows = conn.execute(
            f"SELECT chrt_id,nm_id,amount,provenance FROM {STOCK_ROWS_TABLE} WHERE run_id=?",
            (stock_run["run_id"],),
        ).fetchall()
        if universe is None and identities is None:
            stocks = {nm: {} for nm in sorted({int(r["nm_id"]) for r in stock_rows})}
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
        "available": True, "date": day, "source": OFFICIAL_STOCK_SOURCE,
        "generation_id": run["run_id"], "generation_digest": run["generation_digest"],
        "captured_at": min(captured), "catalog_sku_count": catalog["active_nm_id_count"],
        "sku_count": len(stocks), "facilities": sorted(facilities), "skus": {},
        "facility_evidence": facility_evidence,
    }
    result["skus"] = {nm: {"facilities": values} for nm, values in stocks.items()}
    return result


def current_official_fbs_facilities(db_path, *, requested_nm_ids, now):
    """Freeze quantities and source identity; None requests the full admitted catalog."""
    from decimal import InvalidOperation
    from packages.application.wb_fbs_warehouse_registry import _connect_readonly
    from packages.application.ff_pool_foundation import FACILITY_PROFILES_TABLE
    universe = sorted(set(int(nm) for nm in requested_nm_ids)) if requested_nm_ids is not None else None
    day = current_business_date_iso(now)
    result = {"source": OFFICIAL_STOCK_SOURCE, "date": day, "facilities": []}
    try:
        with _connect_readonly(db_path) as conn:
            conn.execute("BEGIN")
            facilities = [dict(row) for row in conn.execute(
                f"SELECT f.facility_id,f.code,f.name,f.active,COALESCE(p.city,'') AS city "
                f"FROM {FACILITIES_TABLE} f LEFT JOIN {FACILITY_PROFILES_TABLE} p "
                "ON p.facility_id=f.facility_id WHERE f.active=1 ORDER BY f.code,f.facility_id")]
            try:
                if universe == []:
                    raise ValueError("empty_active_catalog")
                stock = read_complete_official_fbs_stock(conn, universe=universe, day=day, now=now)
            except (sqlite3.Error, ValueError, TypeError, KeyError, InvalidOperation) as exc:
                stock = {"available": False, "reason": str(exc)}
            result["requested_nm_ids"] = sorted(stock.get("skus", {})) if universe is None else universe
            result["catalog_sku_count"] = stock.get("catalog_sku_count")
            for facility in facilities:
                fid = facility["facility_id"]
                available = stock.get("available") and fid in stock["facilities"]
                evidence = {"source": OFFICIAL_STOCK_SOURCE, "date": day,
                            "generation_id": stock.get("generation_id"),
                            "generation_digest": stock.get("generation_digest"),
                            **stock.get("facility_evidence", {}).get(fid, {})}
                values = [{"nm_id": nm, "available": int(stock["skus"][nm]["facilities"][fid]),
                           "state": "official_declared_stock"} for nm in result["requested_nm_ids"]] if available else []
                reason = stock.get("reason", "facility_outside_complete_scope")
                reason_ru = (
                    "Официальный снимок остатков FBS устарел: нужен снимок за сегодня не старше 30 минут."
                    if reason == "official_snapshot_not_fresh_current_day" else
                    "Изменилась привязка склада WB к фулфилменту; нужен новый полный снимок."
                    if reason == "mapping_changed" else
                    "Нет полного официального снимка остатков FBS для всех активных товаров выбранного ФФ.")
                result["facilities"].append({**facility, "sku_values": values,
                    "available": sum(v["available"] for v in values) if available else None,
                    "stock_source": evidence, "source_blocker": "" if available else reason_ru})
    except sqlite3.Error:
        result["reason"] = "Официальный источник остатков FBS недоступен."
    return result
