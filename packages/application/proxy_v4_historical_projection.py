"""Pure ready-snapshot projection helpers for guarded Proxy V4 initialization."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
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
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope


PROXY_V4_PROJECTION_METADATA_KEY = "proxy_v4_historical_initialization"
PROXY_V4_PRESERVATION_METADATA_KEY = "proxy_v4_history_preservation"
PROXY_V4_RECONCILIATION_METADATA_KEY = "proxy_v4_historical_reconciliation"
PROXY_V4_TARGET_KEYS = frozenset(
    {
        PROXY_V4_PROFIT_RUB_METRIC_KEY,
        PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY,
        PROXY_V4_MARGIN_PCT_METRIC_KEY,
        PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY,
    }
)


def preserve_proxy_v4_historical_cells(
    plan: SheetVitrinaV1Envelope,
    *,
    previous_plan: SheetVitrinaV1Envelope,
    business_date: str,
) -> tuple[SheetVitrinaV1Envelope, dict[str, Any]]:
    """Freeze already-published V4 history during an ordinary full refresh.

    Only the current business date may be recalculated by the regular refresh.
    A missing prior V4 row/cell is preserved as missing instead of being
    retroactively invented; historical repairs remain an explicit guarded
    reconciliation.
    """

    current_business_date = str(business_date or "")[:10]
    if not current_business_date:
        raise ValueError("Proxy V4 history preservation requires business_date")
    data_sheet = _envelope_data_sheet(plan)
    previous_data = _envelope_data_sheet(previous_plan)
    if data_sheet is None or previous_data is None:
        return plan, _empty_preservation_summary(current_business_date)

    current_dates = {
        day
        for day in plan.date_columns
        if str(day)[:10] < current_business_date
    }
    previous_dates = {str(day)[:10] for day in previous_plan.date_columns}
    preserved_dates = sorted(current_dates & previous_dates)
    if not preserved_dates:
        return plan, _empty_preservation_summary(current_business_date)

    current_indexes = _header_indexes_by_date(data_sheet.header)
    previous_indexes = _header_indexes_by_date(previous_data.header)
    previous_rows = {
        _row_identifier(row): list(row)
        for row in previous_data.rows
        if _row_identifier(row)
    }
    merged_rows: list[list[Any]] = []
    preserved_row_ids: set[str] = set()
    preserved_cell_count = 0
    changed_cell_count = 0
    for raw_row in data_sheet.rows:
        row = list(raw_row)
        row_id = _row_identifier(row)
        if _metric_key(row_id) not in PROXY_V4_TARGET_KEYS:
            merged_rows.append(row)
            continue
        previous_row = previous_rows.get(row_id)
        for day in preserved_dates:
            target_indexes = current_indexes.get(day) or []
            source_indexes = previous_indexes.get(day) or []
            for offset, target_index in enumerate(target_indexes):
                frozen_value: Any = ""
                if previous_row is not None and source_indexes:
                    source_index = source_indexes[min(offset, len(source_indexes) - 1)]
                    if source_index < len(previous_row):
                        frozen_value = previous_row[source_index]
                while target_index >= len(row):
                    row.append("")
                if row[target_index] != frozen_value:
                    row[target_index] = frozen_value
                    changed_cell_count += 1
                preserved_cell_count += 1
                preserved_row_ids.add(row_id)
        merged_rows.append(row)

    merged_sheets = [
        replace(
            sheet,
            rows=merged_rows,
            row_count=len(merged_rows),
            column_count=len(sheet.header),
        )
        if sheet.sheet_name == "DATA_VITRINA"
        else sheet
        for sheet in plan.sheets
    ]
    metadata = deepcopy(dict(getattr(plan, "metadata", {}) or {}))
    previous_metadata = dict(getattr(previous_plan, "metadata", {}) or {})
    previous_marker = previous_metadata.get(PROXY_V4_PROJECTION_METADATA_KEY)
    if isinstance(previous_marker, Mapping):
        marker = deepcopy(dict(previous_marker))
        eligibility = marker.get("eligibility_by_date")
        if isinstance(eligibility, Mapping):
            marker["eligibility_by_date"] = {
                day: deepcopy(value)
                for day, value in eligibility.items()
                if str(day)[:10] in preserved_dates
            }
            marker_dates = sorted(marker["eligibility_by_date"])
            marker["date_from"] = marker_dates[0] if marker_dates else ""
            marker["date_to"] = marker_dates[-1] if marker_dates else ""
        marker["preserved_before_business_date"] = current_business_date
        metadata[PROXY_V4_PROJECTION_METADATA_KEY] = marker
    previous_reconciliation = previous_metadata.get(
        PROXY_V4_RECONCILIATION_METADATA_KEY
    )
    if isinstance(previous_reconciliation, Mapping):
        metadata[PROXY_V4_RECONCILIATION_METADATA_KEY] = deepcopy(
            dict(previous_reconciliation)
        )
    summary = {
        "contract_version": "proxy_v4_ordinary_refresh_history_preservation_v1",
        "business_date": current_business_date,
        "preserved_dates": preserved_dates,
        "preserved_row_ids": sorted(preserved_row_ids),
        "preserved_row_count": len(preserved_row_ids),
        "preserved_cell_count": preserved_cell_count,
        "changed_cell_count": changed_cell_count,
    }
    metadata[PROXY_V4_PRESERVATION_METADATA_KEY] = summary
    return replace(plan, sheets=merged_sheets, metadata=metadata), summary


def _empty_preservation_summary(business_date: str) -> dict[str, Any]:
    return {
        "contract_version": "proxy_v4_ordinary_refresh_history_preservation_v1",
        "business_date": business_date,
        "preserved_dates": [],
        "preserved_row_ids": [],
        "preserved_row_count": 0,
        "preserved_cell_count": 0,
        "changed_cell_count": 0,
    }


def _envelope_data_sheet(plan: SheetVitrinaV1Envelope):
    return next((sheet for sheet in plan.sheets if sheet.sheet_name == "DATA_VITRINA"), None)


def _header_indexes_by_date(header: list[Any]) -> dict[str, list[int]]:
    result: dict[str, list[int]] = {}
    for index, value in enumerate(header):
        normalized = str(value or "")[:10]
        if len(normalized) == 10 and normalized[4:5] == "-" and normalized[7:8] == "-":
            result.setdefault(normalized, []).append(index)
    return result


def _row_identifier(row: list[Any]) -> str:
    return str(row[1] or "") if len(row) > 1 else ""


def reconcile_proxy_v4_target_window(
    current_plan_json: str,
    *,
    reference_plan_json: str,
    date_from: str,
    date_to: str,
    reconciled_at: str,
) -> dict[str, Any]:
    """Restore only reviewed V4 cells from an initialization reference plan."""

    current = json.loads(str(current_plan_json))
    reference = json.loads(str(reference_plan_json))
    plan = deepcopy(current)
    sheet = _data_sheet(plan)
    reference_sheet = _data_sheet(reference)
    rows = sheet.get("rows")
    reference_rows = reference_sheet.get("rows")
    if not isinstance(rows, list) or not isinstance(reference_rows, list):
        raise ValueError("Proxy V4 reconciliation requires DATA_VITRINA rows")
    current_dates = _date_columns(plan)
    reference_dates = _date_columns(reference)
    scoped_dates = sorted(
        day
        for day in set(current_dates) & set(reference_dates)
        if str(date_from)[:10] <= day <= str(date_to)[:10]
    )
    if not scoped_dates:
        raise ValueError("Proxy V4 reconciliation reference has no dates in the bounded window")

    by_id = _rows_by_id(rows)
    reference_by_id = _rows_by_id(reference_rows)
    reference_target_ids = sorted(
        row_id
        for row_id in reference_by_id
        if _metric_key(row_id) in PROXY_V4_TARGET_KEYS
    )
    if not reference_target_ids:
        raise ValueError("Proxy V4 reconciliation reference contains no V4 target rows")
    inserted_rows = 0
    changed_cells = 0
    for row_id in reference_target_ids:
        reference_row = reference_by_id[row_id]
        row = by_id.get(row_id)
        if row is None:
            row = [reference_row[0], row_id, *([""] * len(current_dates))]
            rows.append(row)
            by_id[row_id] = row
            inserted_rows += 1
        for day in scoped_dates:
            current_index = current_dates.index(day) + 2
            reference_index = reference_dates.index(day) + 2
            while current_index >= len(row):
                row.append("")
            reference_value = (
                reference_row[reference_index]
                if reference_index < len(reference_row)
                else ""
            )
            if row[current_index] != reference_value:
                row[current_index] = reference_value
                changed_cells += 1

    metadata = plan.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("Proxy V4 reconciliation requires object metadata")
    reference_metadata = reference.get("metadata")
    reference_marker = (
        reference_metadata.get(PROXY_V4_PROJECTION_METADATA_KEY)
        if isinstance(reference_metadata, Mapping)
        else None
    )
    if isinstance(reference_marker, Mapping):
        marker = deepcopy(dict(reference_marker))
        eligibility = marker.get("eligibility_by_date")
        if isinstance(eligibility, Mapping):
            marker["eligibility_by_date"] = {
                day: deepcopy(value)
                for day, value in eligibility.items()
                if str(day)[:10] in scoped_dates
            }
        marker["date_from"] = scoped_dates[0]
        marker["date_to"] = scoped_dates[-1]
        marker["reconciled_at"] = reconciled_at
        metadata[PROXY_V4_PROJECTION_METADATA_KEY] = marker
    metadata[PROXY_V4_RECONCILIATION_METADATA_KEY] = {
        "contract_version": "proxy_v4_historical_reconciliation_v1",
        "date_from": scoped_dates[0],
        "date_to": scoped_dates[-1],
        "reconciled_at": reconciled_at,
        "target_metric_keys": sorted(PROXY_V4_TARGET_KEYS),
    }
    timestamps = metadata.setdefault("row_last_updated_at_by_row_id", {})
    if isinstance(timestamps, dict):
        for row_id in reference_target_ids:
            timestamps[row_id] = reconciled_at
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
        "dates": scoped_dates,
        "target_row_ids": reference_target_ids,
        "target_before": proxy_v4_target_digest(current),
        "target_after": proxy_v4_target_digest(plan),
        "non_target_before": proxy_v4_non_target_digest(current),
        "non_target_after": proxy_v4_non_target_digest(plan),
    }


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
    payload = {
        "rows": [
            row
            for row in rows
            if isinstance(row, list)
            and len(row) > 1
            and _metric_key(str(row[1])) in PROXY_V4_TARGET_KEYS
        ],
        # Preserve the historical initialization digest shape. New
        # preservation/reconciliation metadata is appended only when present,
        # so already-reviewed initialization manifests remain reproducible.
        "metadata": metadata.get(PROXY_V4_PROJECTION_METADATA_KEY),
        "timestamps": {
            row_id: value
            for row_id, value in sorted(timestamps.items())
            if _metric_key(str(row_id)) in PROXY_V4_TARGET_KEYS
        },
    }
    preservation_metadata = metadata.get(PROXY_V4_PRESERVATION_METADATA_KEY)
    if preservation_metadata is not None:
        payload["preservation_metadata"] = preservation_metadata
    reconciliation_metadata = metadata.get(PROXY_V4_RECONCILIATION_METADATA_KEY)
    if reconciliation_metadata is not None:
        payload["reconciliation_metadata"] = reconciliation_metadata
    return _digest(payload)


def proxy_v4_window_digest(
    plan_or_json: Mapping[str, Any] | str,
    *,
    date_from: str,
    date_to: str,
) -> str:
    """Digest V4 row values only inside one explicit business-date window."""

    plan = json.loads(plan_or_json) if isinstance(plan_or_json, str) else deepcopy(dict(plan_or_json))
    sheet = _data_sheet(plan)
    rows = sheet.get("rows") or []
    dates = _date_columns(plan)
    indexes = [
        index + 2
        for index, day in enumerate(dates)
        if str(date_from)[:10] <= day <= str(date_to)[:10]
    ]
    return _digest(
        [
            [
                str(row[1]),
                *[
                    row[index] if index < len(row) else ""
                    for index in indexes
                ],
            ]
            for row in sorted(
                (
                    list(item)
                    for item in rows
                    if isinstance(item, list)
                    and len(item) > 1
                    and _metric_key(str(item[1])) in PROXY_V4_TARGET_KEYS
                ),
                key=lambda item: str(item[1]),
            )
        ]
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
        metadata.pop(PROXY_V4_PRESERVATION_METADATA_KEY, None)
        metadata.pop(PROXY_V4_RECONCILIATION_METADATA_KEY, None)
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
