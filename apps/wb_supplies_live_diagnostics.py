"""Sanitized live diagnostics for WB FBW supplies field normalization."""

from __future__ import annotations

import argparse
from collections.abc import Iterable
import json
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.wb_supplies import (  # noqa: E402
    HttpBackedWbSuppliesSource,
    WbSuppliesHttpStatusError,
    WbSuppliesListResult,
    WbSuppliesTransportError,
)
from packages.application.wb_supplies import _normalize_supply_row, _warehouse_map  # noqa: E402


TARGET_SUPPLY_IDS = [
    "39265492",
    "39265540",
    "39265590",
    "39265519",
    "39265571",
    "39238882",
    "38535188",
    "38350231",
    "38978468",
    "38978549",
    "38978323",
]

EXPECTED_UI_EVIDENCE = {
    "39265492": {
        "warehouse_display": "Склад Шушары → Обухово",
        "quantity_added": 7500,
        "packed_quantity": 7500,
        "accepted_quantity": 7483,
        "cost_total": 11543.52,
        "has_transit_cost_marker": True,
    },
    "39265540": {
        "warehouse_display": "Электросталь",
        "quantity_added": 9250,
        "packed_quantity": 9250,
        "accepted_quantity": 9237,
        "cost_total": 0,
    },
}

OFFICIAL_STATUS_IDS = [1, 2, 3, 4, 5, 6]
RELATED_KEY_PARTS = (
    "warehouse",
    "destination",
    "source",
    "route",
    "transit",
    "cost",
    "price",
    "tariff",
    "quantity",
    "amount",
    "coefficient",
    "coef",
    "accepted",
    "unloading",
    "readyforsale",
    "depersonalized",
    "box",
    "package",
)
SENSITIVE_KEY_PARTS = (
    "authorization",
    "token",
    "cookie",
    "phone",
    "email",
    "passport",
    "secret",
    "password",
)


def main() -> None:
    args = _parse_args()
    source = HttpBackedWbSuppliesSource(timeout_seconds=args.timeout_seconds)
    targets = [str(item).strip() for item in args.supply_id if str(item).strip()]
    report: dict[str, Any] = {
        "target_supply_ids": targets,
        "diagnostic_scope": {
            "list_limit": args.limit,
            "max_pages": args.max_pages,
            "package_endpoint": not args.skip_package,
            "transit_tariffs_endpoint": not args.skip_transit_tariffs,
            "raw_sensitive_values_printed": False,
        },
        "list_scan": {},
        "transit_tariffs": {},
        "supplies": {},
    }

    warehouses = _fetch_warehouses(source)
    warehouse_by_id = _warehouse_map(warehouses.get("rows", []))
    report["warehouses"] = {
        "status": warehouses["status"],
        "count": len(warehouses.get("rows", [])),
        "key_sets": _row_key_sets(warehouses.get("rows", [])),
    }
    transit_tariffs = {"status": "skipped", "rows": [], "error": ""}
    if not args.skip_transit_tariffs:
        transit_tariffs = _fetch_transit_tariffs(source)
    report["transit_tariffs"] = {
        "status": transit_tariffs["status"],
        "count": len(transit_tariffs.get("rows", [])),
        "error": transit_tariffs.get("error", ""),
    }

    found_rows, list_scan = _scan_list_pages(
        source=source,
        targets=set(targets),
        limit=args.limit,
        max_pages=args.max_pages,
        status_ids=[],
    )
    missing = [target for target in targets if target not in found_rows]
    status_scans: dict[str, Any] = {}
    if missing and args.scan_statuses:
        for status_id in OFFICIAL_STATUS_IDS:
            status_found, status_scan = _scan_list_pages(
                source=source,
                targets=set(missing),
                limit=args.limit,
                max_pages=args.status_max_pages,
                status_ids=[status_id],
            )
            found_rows.update(status_found)
            status_scans[str(status_id)] = status_scan
            missing = [target for target in targets if target not in found_rows]
            if not missing:
                break
    report["list_scan"] = {
        "unfiltered": list_scan,
        "by_status": status_scans,
        "missing_after_scan": missing,
    }

    for supply_id in targets:
        list_match = found_rows.get(supply_id)
        detail = _fetch_detail(source, supply_id)
        goods = _fetch_goods(source, supply_id)
        package = {"status": "skipped", "rows": [], "error": ""} if args.skip_package else _fetch_package(source, supply_id)
        raw_list = list_match.get("row") if list_match else None
        normalized = _normalize_for_report(
            supply_id=supply_id,
            raw_list=raw_list,
            detail=detail,
            goods=goods,
            package=package,
            warehouse_by_id=warehouse_by_id,
        )
        expected = EXPECTED_UI_EVIDENCE.get(supply_id, {})
        transit_tariff_matches = _transit_tariff_matches(normalized, transit_tariffs.get("rows", []))
        report["supplies"][supply_id] = {
            "list": {
                "found": bool(list_match),
                "page": list_match.get("page") if list_match else None,
                "offset": list_match.get("offset") if list_match else None,
                "status_filter": list_match.get("status_filter") if list_match else None,
                "keys": sorted((raw_list or {}).keys()),
                "related_fields": _related_fields(raw_list or {}),
            },
            "detail": _payload_report(detail),
            "goods": _rows_payload_report(goods),
            "package": _rows_payload_report(package),
            "normalized": normalized,
            "transit_tariff_matches": transit_tariff_matches,
            "expected_ui": expected,
            "expected_delta": _expected_delta(normalized, expected),
        }

    print(json.dumps(_sanitize(report), ensure_ascii=False, indent=2, sort_keys=True))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--supply-id", action="append", default=[], help="Supply ID to inspect. Defaults to task targets.")
    parser.add_argument("--limit", type=int, default=1000, help="POST /api/v1/supplies page size.")
    parser.add_argument("--max-pages", type=int, default=8, help="Unfiltered list pages to scan.")
    parser.add_argument("--status-max-pages", type=int, default=3, help="Per-status list pages to scan when targets are missing.")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--skip-package", action="store_true")
    parser.add_argument("--skip-transit-tariffs", action="store_true")
    parser.add_argument("--no-scan-statuses", dest="scan_statuses", action="store_false")
    parser.set_defaults(scan_statuses=True)
    args = parser.parse_args()
    if not args.supply_id:
        args.supply_id = TARGET_SUPPLY_IDS
    args.limit = min(max(int(args.limit or 1000), 1), 1000)
    args.max_pages = max(int(args.max_pages or 1), 1)
    args.status_max_pages = max(int(args.status_max_pages or 1), 1)
    return args


def _scan_list_pages(
    *,
    source: HttpBackedWbSuppliesSource,
    targets: set[str],
    limit: int,
    max_pages: int,
    status_ids: list[int],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    pages: list[dict[str, Any]] = []
    for page in range(max_pages):
        offset = page * limit
        try:
            result = source.list_supplies(limit=limit, offset=offset, status_ids=status_ids)
        except Exception as exc:  # noqa: BLE001 - diagnostic script must summarize upstream failures.
            pages.append({"page": page + 1, "offset": offset, "status": "error", "error": _safe_error(exc)})
            break
        rows = result.rows if isinstance(result, WbSuppliesListResult) else []
        page_matches: list[str] = []
        for row_index, row in enumerate(rows):
            row_id = _row_supply_id(row)
            if row_id in targets and row_id not in found:
                found[row_id] = {
                    "row": dict(row),
                    "page": page + 1,
                    "offset": offset + row_index,
                    "status_filter": list(status_ids),
                }
                page_matches.append(row_id)
        pages.append(
            {
                "page": page + 1,
                "offset": offset,
                "status": "ok",
                "count": len(rows),
                "matched_supply_ids": page_matches,
                "key_sets": _row_key_sets(rows),
            }
        )
        if targets.issubset(found.keys()) or len(rows) < limit:
            break
    return found, {
        "status_ids": status_ids,
        "pages": pages,
        "found_supply_ids": sorted(found),
    }


def _fetch_warehouses(source: HttpBackedWbSuppliesSource) -> dict[str, Any]:
    try:
        return {"status": "ok", "rows": source.fetch_warehouses(), "error": ""}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "rows": [], "error": _safe_error(exc)}


def _fetch_detail(source: HttpBackedWbSuppliesSource, supply_id: str) -> dict[str, Any]:
    try:
        return {"status": "ok", "payload": dict(source.fetch_supply_details(supply_id)), "error": ""}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "payload": None, "error": _safe_error(exc)}


def _fetch_goods(source: HttpBackedWbSuppliesSource, supply_id: str) -> dict[str, Any]:
    try:
        return {"status": "ok", "rows": source.fetch_supply_goods(supply_id, limit=1000, offset=0), "error": ""}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "rows": [], "error": _safe_error(exc)}


def _fetch_package(source: HttpBackedWbSuppliesSource, supply_id: str) -> dict[str, Any]:
    try:
        return {"status": "ok", "rows": source.fetch_supply_package(supply_id), "error": ""}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "rows": [], "error": _safe_error(exc)}


def _fetch_transit_tariffs(source: HttpBackedWbSuppliesSource) -> dict[str, Any]:
    try:
        return {"status": "ok", "rows": source.fetch_transit_tariffs(), "error": ""}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "rows": [], "error": _safe_error(exc)}


def _normalize_for_report(
    *,
    supply_id: str,
    raw_list: Mapping[str, Any] | None,
    detail: Mapping[str, Any],
    goods: Mapping[str, Any],
    package: Mapping[str, Any],
    warehouse_by_id: Mapping[str, str],
) -> dict[str, Any]:
    detail_payload = detail.get("payload") if detail.get("status") == "ok" else None
    goods_rows = goods.get("rows") if goods.get("status") == "ok" else None
    package_rows = package.get("rows") if package.get("status") == "ok" else None
    base_row: Mapping[str, Any] = raw_list or detail_payload or {"supplyID": supply_id}
    try:
        row = _normalize_supply_row(
            raw_list=base_row,
            raw_detail=detail_payload if isinstance(detail_payload, Mapping) else None,
            raw_goods=goods_rows if isinstance(goods_rows, list) else None,
            raw_package=package_rows if isinstance(package_rows, list) else None,
            warehouse_by_id=warehouse_by_id,
            synced_at="diagnostic",
            warnings=[],
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": _safe_error(exc)}
    return {
        "status": "ok",
        "supply_id": row.get("supply_id"),
        "wb_supply_id": row.get("wb_supply_id"),
        "status_id": row.get("status_id"),
        "status_label": row.get("status_label"),
        "type_label": row.get("type_label"),
        "box_type_id": row.get("box_type_id"),
        "warehouse_id": row.get("warehouse_id"),
        "warehouse_name": row.get("warehouse_name"),
        "warehouse_from_name": row.get("warehouse_from_name"),
        "warehouse_to_name": row.get("warehouse_to_name"),
        "warehouse_actual_name": row.get("warehouse_actual_name"),
        "actual_warehouse_name": row.get("actual_warehouse_name"),
        "transit_warehouse_name": row.get("transit_warehouse_name"),
        "warehouse_display": row.get("warehouse_display"),
        "warehouse_fact_line": row.get("warehouse_fact_line"),
        "warehouse_evidence": row.get("warehouse_evidence"),
        "route_evidence": row.get("route_evidence"),
        "quantity_added": row.get("quantity_added"),
        "packed_quantity": row.get("packed_quantity"),
        "accepted_quantity": row.get("accepted_quantity"),
        "quantity_evidence": row.get("quantity_evidence"),
        "packed_quantity_evidence": row.get("packed_quantity_evidence"),
        "cost_total": row.get("cost_total"),
        "cost_display": row.get("cost_display"),
        "acceptance_cost": row.get("acceptance_cost"),
        "transit_cost": row.get("transit_cost"),
        "cost_evidence": row.get("cost_evidence"),
        "has_transit_cost_marker": row.get("has_transit_cost_marker"),
    }


def _payload_report(payload: Mapping[str, Any]) -> dict[str, Any]:
    data = payload.get("payload")
    result: dict[str, Any] = {"status": payload.get("status"), "error": payload.get("error", "")}
    if isinstance(data, Mapping):
        result.update(
            {
                "keys": sorted(data.keys()),
                "related_fields": _related_fields(data),
            }
        )
    return result


def _rows_payload_report(payload: Mapping[str, Any]) -> dict[str, Any]:
    rows = payload.get("rows")
    result: dict[str, Any] = {
        "status": payload.get("status"),
        "error": payload.get("error", ""),
        "count": len(rows) if isinstance(rows, list) else 0,
    }
    if isinstance(rows, list):
        result["key_sets"] = _row_key_sets(rows)
        result["related_fields"] = _related_fields({"rows": rows})
        result["quantity_totals"] = {
            "quantity": _sum_rows(rows, "quantity"),
            "acceptedQuantity": _sum_rows(rows, "acceptedQuantity"),
            "unloadingQuantity": _sum_rows(rows, "unloadingQuantity"),
            "readyForSaleQuantity": _sum_rows(rows, "readyForSaleQuantity"),
            "depersonalizedQuantity": _sum_rows(rows, "depersonalizedQuantity"),
            "supplierBoxAmount": _sum_rows(rows, "supplierBoxAmount"),
        }
    return result


def _expected_delta(normalized: Mapping[str, Any], expected: Mapping[str, Any]) -> dict[str, Any]:
    if not expected or normalized.get("status") != "ok":
        return {}
    delta = {}
    for key, expected_value in expected.items():
        actual_value = normalized.get(key)
        if _normalize_compare_value(actual_value) != _normalize_compare_value(expected_value):
            delta[key] = {"actual": actual_value, "expected": expected_value}
    return delta


def _transit_tariff_matches(normalized: Mapping[str, Any], rows: list[Any]) -> list[dict[str, Any]]:
    if normalized.get("status") != "ok":
        return []
    transit_name = str(normalized.get("warehouse_to_name") or normalized.get("transit_warehouse_name") or "").strip()
    destination_name = str(normalized.get("warehouse_from_name") or normalized.get("warehouse_name") or "").strip()
    if not transit_name or not destination_name:
        return []
    matches: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if row.get("transitWarehouseName") != transit_name or row.get("destinationWarehouseName") != destination_name:
            continue
        matches.append(
            {
                "transitWarehouseName": row.get("transitWarehouseName"),
                "destinationWarehouseName": row.get("destinationWarehouseName"),
                "activeFrom": row.get("activeFrom"),
                "boxTariff": _compact_value(row.get("boxTariff")),
                "palletTariff": row.get("palletTariff"),
            }
        )
    return matches[:5]


def _normalize_compare_value(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, int):
        return float(value)
    return value


def _row_supply_id(row: Mapping[str, Any]) -> str:
    for key in ("supplyID", "supplyId", "supply_id", "ID", "id"):
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _row_key_sets(rows: Iterable[Mapping[str, Any]]) -> list[list[str]]:
    seen: set[tuple[str, ...]] = set()
    key_sets: list[list[str]] = []
    for row in rows:
        keys = tuple(sorted(str(key) for key in row.keys()))
        if keys not in seen:
            seen.add(keys)
            key_sets.append(list(keys))
    return key_sets[:10]


def _related_fields(value: Any, *, prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    if isinstance(value, Mapping):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            normalized_key = _normalize_key(path)
            if _is_sensitive_key(normalized_key):
                result[path] = "***"
                continue
            if _is_related_key(normalized_key):
                result[path] = _compact_value(item)
            if isinstance(item, (Mapping, list)):
                result.update(_related_fields(item, prefix=path))
    elif isinstance(value, list):
        for index, item in enumerate(value[:5]):
            result.update(_related_fields(item, prefix=f"{prefix}[{index}]"))
        if len(value) > 5:
            result[f"{prefix}.truncated_count"] = len(value) - 5
    return result


def _is_related_key(normalized_key: str) -> bool:
    return any(part in normalized_key for part in RELATED_KEY_PARTS)


def _is_sensitive_key(normalized_key: str) -> bool:
    return any(part in normalized_key for part in SENSITIVE_KEY_PARTS)


def _normalize_key(value: str) -> str:
    return "".join(char for char in value.casefold() if char.isalnum())


def _compact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {"type": "object", "keys": sorted(str(key) for key in value.keys())[:30]}
    if isinstance(value, list):
        return {"type": "list", "count": len(value)}
    return value


def _sum_rows(rows: list[Any], key: str) -> float | None:
    found = False
    total = 0.0
    snake_key = _camel_to_snake(key)
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        value = row.get(key)
        if value is None:
            value = row.get(snake_key)
        number = _optional_number(value)
        if number is None:
            continue
        found = True
        total += number
    return total if found else None


def _optional_number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _camel_to_snake(value: str) -> str:
    result = []
    for char in value:
        if char.isupper() and result:
            result.append("_")
        result.append(char.lower())
    return "".join(result)


def _safe_error(exc: BaseException) -> str:
    if isinstance(exc, WbSuppliesHttpStatusError):
        return f"{type(exc).__name__}: status {exc.status_code}"
    if isinstance(exc, WbSuppliesTransportError):
        return f"{type(exc).__name__}: {exc}"
    return f"{type(exc).__name__}: {exc}"


def _sanitize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): ("***" if _is_sensitive_key(_normalize_key(str(key))) else _sanitize(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return _mask_phone_like(value)
    return value


def _mask_phone_like(value: str) -> str:
    digits = [char for char in value if char.isdigit()]
    if len(digits) < 10:
        return value
    masked = value
    for digit in digits[3:-2]:
        masked = masked.replace(digit, "*", 1)
    return masked


if __name__ == "__main__":
    main()
