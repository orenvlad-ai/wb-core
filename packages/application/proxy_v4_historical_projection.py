"""Pure ready-snapshot projection helpers for guarded Proxy V4 initialization."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any, Callable, Mapping

from packages.application.calculation_parameters_v4 import (
    PROXY_V4_FIXED_BOUNDARY,
    ProxyV4Parameters,
    calculate_proxy_4,
)
from packages.application.sheet_vitrina_v1_our_wb_costs import (
    OUR_WB_UNIT_COST_RUB_METRIC_KEY,
)
from packages.application.sheet_vitrina_v1_proxy_margin_3_historical_backfill import (
    _data_sheet,
    _date_columns,
    _update_data_dimensions,
)
from packages.application.sheet_vitrina_v1_proxy_v4 import (
    PROXY_V4_MARGIN_LABEL_RU,
    PROXY_V4_MARGIN_PCT_METRIC_KEY,
    PROXY_V4_PROFIT_LABEL_RU,
    PROXY_V4_PROFIT_RUB_METRIC_KEY,
    PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY,
    PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY,
)


PROXY_V4_PROJECTION_METADATA_KEY = "proxy_v4_historical_initialization"
PROXY_V4_TARGET_KEYS = frozenset(
    {
        PROXY_V4_PROFIT_RUB_METRIC_KEY,
        PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY,
        PROXY_V4_MARGIN_PCT_METRIC_KEY,
        PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY,
    }
)


def project_proxy_v4_ready_snapshot(
    plan_json: str,
    *,
    parameters_for_date: Callable[[str], ProxyV4Parameters | None],
    materialized_at: str,
) -> dict[str, Any]:
    original = json.loads(str(plan_json))
    plan = deepcopy(original)
    sheet = _data_sheet(plan)
    rows = sheet.get("rows")
    if not isinstance(rows, list):
        raise ValueError("Proxy V4 initialization requires DATA_VITRINA rows")
    dates = _date_columns(plan)
    by_id = _rows_by_id(rows)
    scopes = sorted(
        {row_id.split("|", 1)[0] for row_id in by_id if row_id.startswith("SKU:")}
    )
    if not scopes:
        raise ValueError("Proxy V4 initialization requires at least one SKU scope")
    inserted_rows = _ensure_proxy_v4_rows(
        rows,
        by_id=by_id,
        scopes=scopes,
        date_count=len(dates),
    )
    by_id = _rows_by_id(rows)
    changed_cells = 0
    eligibility_by_date: dict[str, dict[str, Any]] = {}
    for index, business_date in enumerate(dates):
        parameters = parameters_for_date(business_date)
        sku_results: list[Mapping[str, Decimal | None]] = []
        eligible_nm_ids: list[int] = []
        for scope in scopes:
            nm_id = int(scope.split(":", 1)[1])
            calculated = calculate_proxy_4(
                order_sum=_cell_decimal(by_id.get(f"{scope}|orderSum"), index),
                order_count=_cell_decimal(by_id.get(f"{scope}|orderCount"), index),
                canonical_wb_wac=_cell_decimal(
                    by_id.get(f"{scope}|{OUR_WB_UNIT_COST_RUB_METRIC_KEY}"), index
                ),
                ads_sum=_cell_decimal(by_id.get(f"{scope}|ads_sum"), index),
                parameters=parameters,
                business_date=business_date,
            )
            sku_results.append(calculated)
            if calculated["proxy_profit_4"] is not None:
                eligible_nm_ids.append(nm_id)
            changed_cells += _set_cell(
                by_id[f"{scope}|{PROXY_V4_PROFIT_RUB_METRIC_KEY}"],
                index,
                calculated["proxy_profit_4"],
            )
            changed_cells += _set_cell(
                by_id[f"{scope}|{PROXY_V4_MARGIN_PCT_METRIC_KEY}"],
                index,
                calculated["proxy_margin_4"],
            )
        eligible = [
            item
            for item in sku_results
            if item["proxy_profit_4"] is not None
            and item["expected_buyout_revenue"] is not None
        ]
        total_profit = (
            sum((item["proxy_profit_4"] for item in eligible), Decimal("0"))  # type: ignore[arg-type]
            if eligible
            else None
        )
        total_revenue = (
            sum(
                (item["expected_buyout_revenue"] for item in eligible),  # type: ignore[arg-type]
                Decimal("0"),
            )
            if eligible
            else None
        )
        total_margin = (
            None
            if total_profit is None or total_revenue in (None, Decimal("0"))
            else total_profit / total_revenue
        )
        changed_cells += _set_cell(
            by_id[f"TOTAL|{PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY}"],
            index,
            total_profit,
        )
        changed_cells += _set_cell(
            by_id[f"TOTAL|{PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY}"],
            index,
            total_margin,
        )
        eligibility_by_date[business_date] = {
            "sku_scope_count": len(scopes),
            "eligible_sku_count": len(eligible_nm_ids),
            "blank_sku_count": len(scopes) - len(eligible_nm_ids),
            "version_id": parameters.version_id if parameters is not None else "",
            "before_boundary": business_date < PROXY_V4_FIXED_BOUNDARY,
        }

    metadata = plan.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("Proxy V4 initialization requires object metadata")
    marker = {
        "contract_version": "proxy_v4_historical_projection_v1",
        "fixed_boundary": PROXY_V4_FIXED_BOUNDARY,
        "materialized_at": materialized_at,
        "date_from": min(dates) if dates else "",
        "date_to": max(dates) if dates else "",
        "target_metric_keys": sorted(PROXY_V4_TARGET_KEYS),
        "eligibility_by_date": eligibility_by_date,
    }
    metadata[PROXY_V4_PROJECTION_METADATA_KEY] = marker
    timestamps = metadata.setdefault("row_last_updated_at_by_row_id", {})
    if isinstance(timestamps, dict):
        for row_id in by_id:
            if _metric_key(row_id) in PROXY_V4_TARGET_KEYS:
                timestamps[row_id] = materialized_at
    _update_data_dimensions(sheet)
    after = json.dumps(
        plan,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return {
        "after_plan_json": after,
        "changed_cells": changed_cells,
        "inserted_rows": inserted_rows,
        "dates": dates,
        "eligibility_by_date": eligibility_by_date,
        "target_digest": proxy_v4_target_digest(plan),
        "non_target_before": proxy_v4_non_target_digest(original),
        "non_target_after": proxy_v4_non_target_digest(plan),
    }


def proxy_v4_target_digest(plan_or_json: Mapping[str, Any] | str) -> str:
    plan = json.loads(plan_or_json) if isinstance(plan_or_json, str) else deepcopy(dict(plan_or_json))
    sheet = _data_sheet(plan)
    rows = sheet.get("rows") or []
    metadata = dict(plan.get("metadata") or {})
    timestamps = dict(metadata.get("row_last_updated_at_by_row_id") or {})
    return _digest(
        {
            "rows": [
                row
                for row in rows
                if isinstance(row, list) and len(row) > 1 and _metric_key(str(row[1])) in PROXY_V4_TARGET_KEYS
            ],
            "metadata": metadata.get(PROXY_V4_PROJECTION_METADATA_KEY),
            "timestamps": {
                row_id: value
                for row_id, value in sorted(timestamps.items())
                if _metric_key(str(row_id)) in PROXY_V4_TARGET_KEYS
            },
        }
    )


def proxy_v4_non_target_digest(plan_or_json: Mapping[str, Any] | str) -> str:
    plan = json.loads(plan_or_json) if isinstance(plan_or_json, str) else deepcopy(dict(plan_or_json))
    sheet = _data_sheet(plan)
    rows = sheet.get("rows")
    if isinstance(rows, list):
        sheet["rows"] = [
            row
            for row in rows
            if not (
                isinstance(row, list)
                and len(row) > 1
                and _metric_key(str(row[1])) in PROXY_V4_TARGET_KEYS
            )
        ]
        _update_data_dimensions(sheet)
    metadata = plan.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop(PROXY_V4_PROJECTION_METADATA_KEY, None)
        timestamps = metadata.get("row_last_updated_at_by_row_id")
        if isinstance(timestamps, dict):
            for row_id in list(timestamps):
                if _metric_key(str(row_id)) in PROXY_V4_TARGET_KEYS:
                    timestamps.pop(row_id, None)
            if not timestamps:
                metadata.pop("row_last_updated_at_by_row_id", None)
        if not metadata:
            plan.pop("metadata", None)
    return _digest(plan)


def _ensure_proxy_v4_rows(
    rows: list[Any],
    *,
    by_id: Mapping[str, list[Any]],
    scopes: list[str],
    date_count: int,
) -> int:
    specs: list[tuple[str, str, str]] = [
        ("TOTAL", PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY, PROXY_V4_PROFIT_LABEL_RU),
        ("TOTAL", PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY, PROXY_V4_MARGIN_LABEL_RU),
    ]
    for scope in scopes:
        prefix = _scope_label_prefix(by_id, scope)
        specs.extend(
            [
                (scope, PROXY_V4_PROFIT_RUB_METRIC_KEY, f"{prefix}: {PROXY_V4_PROFIT_LABEL_RU}"),
                (scope, PROXY_V4_MARGIN_PCT_METRIC_KEY, f"{prefix}: {PROXY_V4_MARGIN_LABEL_RU}"),
            ]
        )
    inserted = 0
    for scope, metric_key, label in specs:
        row_id = f"{scope}|{metric_key}"
        if row_id in by_id:
            continue
        rows.append([label, row_id, *([""] * date_count)])
        inserted += 1
    return inserted


def _rows_by_id(rows: list[Any]) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 2:
            continue
        row_id = str(row[1] or "")
        if row_id:
            result[row_id] = row
    return result


def _scope_label_prefix(by_id: Mapping[str, list[Any]], scope: str) -> str:
    for suffix in ("orderSum", "proxy_profit_3_rub", "proxy_profit_2_rub"):
        row = by_id.get(f"{scope}|{suffix}")
        if row:
            return str(row[0] or scope).split(": ", 1)[0]
    return scope


def _cell_decimal(row: list[Any] | None, index: int) -> Decimal | None:
    if row is None or len(row) <= 2 + index or row[2 + index] in (None, ""):
        return None
    try:
        value = Decimal(str(row[2 + index]).replace(",", "."))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return value if value.is_finite() else None


def _set_cell(row: list[Any], index: int, value: Decimal | None) -> int:
    target_index = 2 + index
    while len(row) <= target_index:
        row.append("")
    normalized: Any = "" if value is None else float(value)
    current = row[target_index]
    if _same_cell(current, normalized):
        return 0
    row[target_index] = normalized
    return 1


def _same_cell(current: Any, expected: Any) -> bool:
    if current in (None, "") or expected in (None, ""):
        return current in (None, "") and expected in (None, "")
    try:
        return abs(Decimal(str(current)) - Decimal(str(expected))) <= Decimal("0.0000005")
    except (InvalidOperation, ValueError):
        return False


def _metric_key(row_id: str) -> str:
    return str(row_id).split("|", 1)[1] if "|" in str(row_id) else ""


def _digest(value: Any) -> str:
    body = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()
