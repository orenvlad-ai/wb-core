"""Query-only, exact-date WB operands for the isolated shared SKU cost book.

WB means stock at WB plus goods going to/from customers. FF-to-WB transit and
acceptance discrepancies are separate stages and are not added here. This
reader consumes saved WB valuations; it never rebuilds costs or reads FBS.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from packages.application.warehouse_functional import _wb_snapshot_integrity
from packages.business_time import business_date_from_timestamp

CONTRACT = "shared_sku_cost_wb_source_v1"
P = "sheet_vitrina_v1_"
COMPONENTS = ("quantity", "in_way_to_client", "in_way_from_client")
BALANCE_COMPONENTS = ("wb_quantity", "wb_in_way_to_client", "wb_in_way_from_client")
FORBIDDEN_QUALITIES = {"fallback", "fallback_average", "zero_quantity_without_cost_basis"}
ERRORS = (sqlite3.Error, ValueError, TypeError, KeyError, InvalidOperation)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None or value == "":
        raise ValueError("invalid_wb_operand")
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("invalid_wb_operand")
    return result


def _quantity(value: Any) -> int:
    result = _decimal(value)
    if result != result.to_integral_value():
        raise ValueError("non_integer_wb_quantity")
    return int(result)


def _list(value: str) -> list:
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        raise ValueError("invalid_wb_snapshot_array")
    return parsed


def _object(value: str) -> dict:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("invalid_wb_provenance")
    return parsed


def _missing(nm_id: int, reason: str, source: dict | None = None) -> dict:
    return {"nm_id": nm_id, "status": "missing", "reason": reason,
            "quantity": None, "capital_rub": None, "quality": "unavailable",
            "source": source or {}}


def capture_wb_component(
    db_path: Path, *, day: str, nm_ids: list[int], version_id: str | None = None,
) -> dict[str, Any]:
    """Read a single saved authority in one SQLite read-only transaction.

    A pinned version must belong to the exact date. Without a pin, select the
    latest good published version of that date, never a previous-day fallback.
    ``complete`` requires authority integrity and every requested SKU. Missing
    rows carry null operands; an unrelated missing SKU does not erase valid
    row evidence. The caller freezes source_digest with its own daily period.
    """
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError("exact_business_date_required")
    requested = sorted({_quantity(value) for value in nm_ids})
    if not requested or requested[0] <= 0:
        raise ValueError("positive_requested_nm_ids_required")
    result: dict[str, Any] = {
        "contract": CONTRACT, "business_date": day, "complete": False,
        "authority_complete": False, "reason": "", "version_id": version_id or "",
        "requested_nm_ids": requested, "rows": [],
        "quantity_basis": "wb_stock_plus_to_customer_plus_from_customer",
    }
    evidence: dict[str, Any] = {}
    conn = None
    try:
        conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        versions = [dict(row) for row in conn.execute(
            f"SELECT version_id,cutover_id,status,business_effective_date,published_at,"
            f"created_at,effective_at,plan_fingerprint,source_watermarks_json FROM {P}warehouse_functional_versions "
            "WHERE cutover_id='warehouse_functional_cutover_v1' AND status='good' "
            "AND business_effective_date=? "
            + ("AND version_id=? " if version_id is not None else "")
            + "ORDER BY COALESCE(NULLIF(published_at,''),created_at) DESC,created_at DESC,version_id DESC LIMIT 1",
            (day, version_id) if version_id is not None else (day,),
        )]
        if not versions:
            raise ValueError("exact_date_good_wb_version_missing")
        version = versions[0]
        result["version_id"] = version["version_id"]
        watermark = _object(version.pop("source_watermarks_json")).get("wb_snapshot")
        if not isinstance(watermark, dict):
            raise ValueError("wb_snapshot_watermark_missing")
        evidence["version"] = version
        evidence["wb_snapshot_watermark"] = watermark
        snapshots = [dict(row) for row in conn.execute(
            f"SELECT * FROM {P}warehouse_wb_snapshots WHERE version_id=? ORDER BY snapshot_id",
            (version["version_id"],),
        )]
        evidence["snapshots"] = snapshots
        if len(snapshots) != 1:
            raise ValueError("exact_version_wb_snapshot_missing_or_ambiguous")
        snapshot = snapshots[0]
        if (snapshot["snapshot_date"] != day
                or business_date_from_timestamp(snapshot["fetched_at"]) != day
                or snapshot["pagination_complete"] != 1):
            raise ValueError("wb_snapshot_date_or_completeness_mismatch")
        items, raw_rows = _list(snapshot["items_json"]), _list(snapshot["raw_rows_json"])
        snapshot_requested_list = [_quantity(value) for value in _list(snapshot["requested_nm_ids_json"])]
        snapshot_requested = set(snapshot_requested_list)
        if (not snapshot_requested or 0 in snapshot_requested
                or len(snapshot_requested) != len(snapshot_requested_list)):
            raise ValueError("invalid_wb_snapshot_scope")
        if (int(snapshot["raw_row_count"]) != len(raw_rows)
                or snapshot["raw_rows_digest"] != _digest(sorted(raw_rows, key=_json))):
            raise ValueError("wb_snapshot_raw_digest_mismatch")
        if (not watermark.get("snapshot_id")
                or watermark.get("digest") != snapshot["raw_rows_digest"]
                or watermark.get("fetched_at") != snapshot["fetched_at"]
                or watermark.get("pagination_complete") is not True
                or watermark.get("raw_row_count") != len(raw_rows)
                or watermark.get("requested_count") != len(snapshot_requested)):
            raise ValueError("wb_snapshot_version_watermark_mismatch")
        canonical: dict[int, tuple[int, int, int]] = {}
        for item in items:
            nm_id = _quantity(item["nm_id"])
            parts = tuple(_quantity(item[key]) for key in COMPONENTS)
            if (nm_id not in snapshot_requested or nm_id in canonical
                    or sum(parts) != _quantity(item["wb_contour_quantity"])):
                raise ValueError("invalid_wb_canonical_snapshot_item")
            canonical[nm_id] = parts
        if set(canonical) != snapshot_requested:
            raise ValueError("wb_snapshot_dense_scope_incomplete")
        integrity = _wb_snapshot_integrity(snapshot)
        if (not integrity["raw_to_canonical_mapping_matches"]
                or integrity["exact_duplicate_count"] or integrity["source_key_duplicate_count"]):
            raise ValueError("wb_snapshot_raw_canonical_mismatch")
        balances = [dict(row) for row in conn.execute(
            f"SELECT version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,"
            "cost_covered_quantity,quality,certified,wb_quantity,wb_in_way_to_client,"
            f"wb_in_way_from_client,provenance_json FROM {P}warehouse_functional_balances "
            "WHERE version_id=? AND warehouse_key='wb' ORDER BY nm_id", (version["version_id"],),
        )]
        evidence["balances"] = balances
        by_nm = {int(row["nm_id"]): row for row in balances}
        if len(by_nm) != len(balances) or not set(by_nm) <= snapshot_requested:
            raise ValueError("wb_balance_snapshot_scope_mismatch")
        source = {"version_id": version["version_id"], "snapshot_id": snapshot["snapshot_id"],
                  "source_snapshot_id": watermark["snapshot_id"], "snapshot_date": day,
                  "fetched_at": snapshot["fetched_at"], "raw_rows_digest": snapshot["raw_rows_digest"],
                  "published_at": version["published_at"] or version["created_at"]}
        result["source"] = source
        result["authority_complete"] = True
        for nm_id in requested:
            if nm_id not in canonical:
                result["rows"].append(_missing(nm_id, "sku_outside_exact_wb_snapshot", source))
                continue
            try:
                result["rows"].append(_operand(nm_id, canonical[nm_id], by_nm.get(nm_id), source))
            except ERRORS as exc:
                result["rows"].append(_missing(nm_id, str(exc), source))
        result["complete"] = all(row["status"] == "available" for row in result["rows"])
        result["reason"] = "" if result["complete"] else "wb_sku_components_incomplete"
    except ERRORS as exc:
        result.update(complete=False, authority_complete=False, reason=str(exc),
                      rows=[_missing(nm_id, str(exc)) for nm_id in requested])
    finally:
        if conn is not None:
            conn.rollback()
            conn.close()
    result["source_digest"] = _digest({"capture": result, "evidence": evidence})
    return result


def _operand(nm_id: int, parts: tuple[int, int, int], row: dict | None, source: dict) -> dict:
    quantity = sum(parts)
    row_source = {**source, "basis": "immutable_functional_wb_balance"}
    if row is None:
        if quantity:
            raise ValueError("positive_wb_snapshot_without_valuation")
        capital, quality, certified = Decimal(0), "complete_official_wb_zero", True
        row_source["basis"] = "explicit_dense_zero_in_linked_complete_wb_snapshot"
    else:
        if (_quantity(row["quantity"]) != quantity
                or tuple(_quantity(row[key]) for key in BALANCE_COMPONENTS) != parts):
            raise ValueError("wb_balance_snapshot_quantity_mismatch")
        capital = _decimal(row["capital_rub"])
        if _quantity(row["cost_covered_quantity"]) != quantity:
            raise ValueError("wb_cost_coverage_incomplete")
        if quantity == 0:
            if capital != 0:
                raise ValueError("zero_wb_quantity_with_capital")
        else:
            wac = _decimal(row["wac_rub"])
            if capital <= 0 or wac <= 0:
                raise ValueError("positive_wb_quantity_without_cost")
            # Published WB arithmetic uses Decimal precision 28. Preserve the
            # saved capital exactly and only validate its WAC representation.
            with localcontext() as context:
                context.prec = 28
                if quantity * wac != capital:
                    raise ValueError("wb_published_wac_capital_mismatch")
            provenance = _object(row["provenance_json"])
            records = provenance.get("source_records")
            if not isinstance(records, list) or not any(
                isinstance(item, dict) and item.get("source") == "official_wb_snapshot"
                and item.get("snapshot_id") == source["source_snapshot_id"]
                and item.get("snapshot_date") == source["snapshot_date"]
                and item.get("fetched_at") == source["fetched_at"] for item in records
            ):
                raise ValueError("wb_balance_snapshot_provenance_mismatch")
            row_source["balance_provenance_digest"] = _digest(provenance)
        quality, certified = str(row["quality"] or ""), bool(row["certified"])
        if not quality:
            raise ValueError("wb_quality_missing")
        quality_parts = quality.strip().casefold().removeprefix("mixed:").split(",")
        if quantity > 0 and FORBIDDEN_QUALITIES.intersection(part.strip() for part in quality_parts):
            raise ValueError("wb_forbidden_fallback_quality")
    return {"nm_id": nm_id, "status": "available", "reason": "", "quantity": quantity,
            "capital_rub": format(capital, "f"), "quality": quality, "certified": certified,
            "components": dict(zip(("physical", "to_customer", "from_customer"), parts)),
            "source": row_source}
