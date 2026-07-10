"""Pure guarded transformation for historical proxy margin 3 ready snapshots."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import math
import re
from typing import Any, Iterable, Mapping, Sequence

from packages.application.sheet_vitrina_v1_our_wb_costs import (
    OUR_WB_COST_OPENING_DATE,
    OUR_WB_PROXY_MARGIN_3_PCT_LABEL,
    OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
    OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY,
    OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_LABEL,
    OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY,
)


DATA_SHEET_NAME = "DATA_VITRINA"
ROW_TIMESTAMP_MAP_KEY = "row_last_updated_at_by_row_id"
TOTAL_SCOPE = "TOTAL"
SKU_SCOPE_PREFIX = "SKU:"

TOTAL_ORDER_SUM_KEY = "total_orderSum"
SKU_ORDER_SUM_KEY = "orderSum"
TOTAL_MARGIN_2_KEY = "proxy_margin_2_pct_total"
SKU_MARGIN_2_KEY = "proxy_margin_2_pct"

TARGET_METRIC_KEYS = frozenset(
    {
        OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
        OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY,
    }
)

_ANCHOR_METRIC_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY, OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY}),
    frozenset({TOTAL_MARGIN_2_KEY, SKU_MARGIN_2_KEY}),
    frozenset({"total_proxy_profit_2_rub", "proxy_profit_2_rub"}),
    frozenset({"total_proxy_profit_rub", "proxy_profit_rub"}),
)
_CELL_TOLERANCE = 0.5e-6
_CELL_WIDTH_PREFIX = 2
_CELL_REF_RE = re.compile(r"^([A-Z]+)([1-9][0-9]*)$")


class BackfillPlanError(ValueError):
    """Raised when one persisted plan cannot be transformed safely."""


@dataclass(frozen=True)
class ReadySnapshotInput:
    bundle_version: str
    activated_at: str
    as_of_date: str
    snapshot_id: str
    plan_version: str
    refreshed_at: str
    plan_json: str


@dataclass
class SnapshotTransform:
    snapshot: ReadySnapshotInput
    new_plan_json: str | None
    date_columns: list[str]
    changed_rows: int = 0
    inserted_sku_rows: int = 0
    inserted_total_rows: int = 0
    changed_cells: int = 0
    existing_correct_cells: int = 0
    blank_operand_cells: int = 0
    zero_denominator_cells: int = 0
    pre_boundary_margin2_fallbacks: int = 0
    blank_operand_details: list[dict[str, Any]] = field(default_factory=list)
    fallback_details: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[dict[str, Any]] = field(default_factory=list)
    non_target_digest_before: str = ""
    non_target_digest_after: str = ""

    @property
    def changed(self) -> bool:
        return self.new_plan_json is not None and self.new_plan_json != self.snapshot.plan_json


@dataclass
class BackfillPreflight:
    snapshots: list[ReadySnapshotInput]
    transforms: list[SnapshotTransform]
    expected_fingerprint: str
    snapshot_identity_digest: str
    non_target_digest_before: str
    non_target_digest_after: str

    @property
    def blockers(self) -> list[dict[str, Any]]:
        return [blocker for item in self.transforms for blocker in item.blockers]

    @property
    def updates(self) -> list[SnapshotTransform]:
        return [item for item in self.transforms if item.changed]

    def summary(self) -> dict[str, Any]:
        unique_dates = sorted({date for item in self.transforms for date in item.date_columns})
        bundle_coverage: dict[str, dict[str, Any]] = {}
        for item in self.transforms:
            coverage = bundle_coverage.setdefault(
                item.snapshot.bundle_version,
                {
                    "bundle_version": item.snapshot.bundle_version,
                    "snapshots": 0,
                    "snapshots_with_date_columns": 0,
                    "unique_date_columns": set(),
                    "as_of_dates": [],
                },
            )
            coverage["snapshots"] += 1
            coverage["snapshots_with_date_columns"] += int(bool(item.date_columns))
            coverage["unique_date_columns"].update(item.date_columns)
            coverage["as_of_dates"].append(item.snapshot.as_of_date)

        normalized_coverage = []
        for coverage in bundle_coverage.values():
            dates = sorted(coverage.pop("unique_date_columns"))
            as_of_dates = sorted(coverage.pop("as_of_dates"))
            normalized_coverage.append(
                {
                    **coverage,
                    "as_of_date_from": as_of_dates[0] if as_of_dates else None,
                    "as_of_date_to": as_of_dates[-1] if as_of_dates else None,
                    "unique_date_columns": len(dates),
                    "date_column_from": dates[0] if dates else None,
                    "date_column_to": dates[-1] if dates else None,
                }
            )

        blank_details = [detail for item in self.transforms for detail in item.blank_operand_details]
        fallback_details = [detail for item in self.transforms for detail in item.fallback_details]
        blockers = self.blockers
        return {
            "schema_version": "sheet_vitrina_v1_proxy_margin_3_historical_backfill_v1",
            "first_available_date": unique_dates[0] if unique_dates else None,
            "last_available_date": unique_dates[-1] if unique_dates else None,
            "snapshots_scanned": len(self.transforms),
            "snapshots_with_date_columns": sum(bool(item.date_columns) for item in self.transforms),
            "snapshots_without_date_columns": sum(not item.date_columns for item in self.transforms),
            "bundle_count": len(normalized_coverage),
            "bundle_coverage": sorted(normalized_coverage, key=lambda item: item["bundle_version"]),
            "unique_date_columns": len(unique_dates),
            "changed_snapshots": len(self.updates),
            "changed_rows": sum(item.changed_rows for item in self.transforms),
            "sku_rows_inserted": sum(item.inserted_sku_rows for item in self.transforms),
            "total_rows_inserted": sum(item.inserted_total_rows for item in self.transforms),
            "changed_cells": sum(item.changed_cells for item in self.transforms),
            "existing_correct_noop_cells": sum(item.existing_correct_cells for item in self.transforms),
            "blank_operand_cells": sum(item.blank_operand_cells for item in self.transforms),
            "zero_denominator_cells": sum(item.zero_denominator_cells for item in self.transforms),
            "pre_boundary_margin2_fallbacks": sum(
                item.pre_boundary_margin2_fallbacks for item in self.transforms
            ),
            "blank_operand_details": blank_details,
            "pre_boundary_margin2_fallback_details": fallback_details,
            "conflicts": len(blockers),
            "blockers": blockers,
            "non_target_digest": self.non_target_digest_before,
            "non_target_digest_after": self.non_target_digest_after,
            "non_target_preserved": self.non_target_digest_before == self.non_target_digest_after,
            "snapshot_identity_digest": self.snapshot_identity_digest,
            "expected_fingerprint": self.expected_fingerprint,
        }


def build_backfill_preflight(snapshots: Sequence[ReadySnapshotInput]) -> BackfillPreflight:
    """Transform every snapshot in memory before any caller may write to SQLite."""

    ordered = sorted(snapshots, key=lambda item: (item.bundle_version, item.as_of_date))
    transforms = [transform_ready_snapshot(item) for item in ordered]
    before_entries = [
        [item.snapshot.bundle_version, item.snapshot.as_of_date, item.non_target_digest_before]
        for item in transforms
    ]
    after_entries = [
        [item.snapshot.bundle_version, item.snapshot.as_of_date, item.non_target_digest_after]
        for item in transforms
    ]
    return BackfillPreflight(
        snapshots=ordered,
        transforms=transforms,
        expected_fingerprint=ready_snapshot_fingerprint(ordered),
        snapshot_identity_digest=snapshot_identity_digest(ordered),
        non_target_digest_before=_sha256_json(before_entries),
        non_target_digest_after=_sha256_json(after_entries),
    )


def transform_ready_snapshot(snapshot: ReadySnapshotInput) -> SnapshotTransform:
    """Return one semantic plan update or blockers without mutating the input."""

    try:
        original = _strict_json_loads(snapshot.plan_json)
    except (TypeError, ValueError) as exc:
        return _blocked_transform(snapshot, f"invalid_plan_json: {exc}")
    if not isinstance(original, dict):
        return _blocked_transform(snapshot, "plan_json must contain an object")

    result = SnapshotTransform(snapshot=snapshot, new_plan_json=None, date_columns=[])
    try:
        plan = deepcopy(original)
        date_columns = _date_columns(plan)
        result.date_columns = date_columns
        before_digest = non_target_plan_digest(original)
        result.non_target_digest_before = before_digest
        if not date_columns:
            result.non_target_digest_after = before_digest
            return result

        data_sheet = _data_sheet(plan)
        rows = data_sheet.get("rows")
        if not isinstance(rows, list):
            raise BackfillPlanError("DATA_VITRINA.rows must be a list")
        _validate_header(data_sheet, date_columns)
        rows_by_id = _rows_by_id(rows)
        scopes = _historical_sku_scopes(rows)
        target_specs = _target_specs(scopes=scopes, rows_by_id=rows_by_id)
        expected_rows: dict[str, list[Any]] = {}
        inserted_ids: set[str] = set()
        changed_existing_ids: set[str] = set()

        for spec in target_specs:
            row_id = spec["row_id"]
            existing = rows_by_id.get(row_id)
            new_values: list[Any] = []
            for column_index, column_date in enumerate(date_columns):
                expected, evidence = _expected_margin_cell(
                    row_id=row_id,
                    scope=spec["scope"],
                    column_date=column_date,
                    column_index=column_index,
                    rows_by_id=rows_by_id,
                )
                detail_base = {
                    "bundle_version": snapshot.bundle_version,
                    "snapshot_as_of_date": snapshot.as_of_date,
                    "column_date": column_date,
                    "row_id": row_id,
                }
                if evidence["zero_denominator"]:
                    result.zero_denominator_cells += 1
                if evidence["fallback"]:
                    result.pre_boundary_margin2_fallbacks += 1
                    result.fallback_details.append({**detail_base, "source_row_id": evidence["source_row_id"]})
                if evidence["blank_operands"]:
                    result.blank_operand_cells += 1
                    result.blank_operand_details.append(
                        {**detail_base, "missing_operands": evidence["blank_operands"]}
                    )
                current = _row_cell(existing, column_index) if existing is not None else ""
                comparison = _compare_target_cell(current=current, expected=expected)
                if comparison == "conflict":
                    result.blockers.append(
                        {
                            **detail_base,
                            "reason": "existing_nonblank_target_value_mismatch",
                            "existing": current,
                            "expected": expected,
                        }
                    )
                elif comparison == "change":
                    result.changed_cells += 1
                    if existing is not None:
                        changed_existing_ids.add(row_id)
                else:
                    result.existing_correct_cells += int(existing is not None)
                new_values.append(expected)

            label = str(existing[0]) if existing is not None and existing else str(spec["label"])
            expected_rows[row_id] = [label, row_id, *new_values]
            if existing is None:
                inserted_ids.add(row_id)

        if result.blockers:
            result.non_target_digest_after = before_digest
            return result

        existing_target_ids = [
            str(row[1])
            for row in rows
            if isinstance(row, list) and len(row) > 1 and _metric_key(str(row[1])) in TARGET_METRIC_KEYS
        ]
        expected_target_ids = [str(spec["row_id"]) for spec in target_specs]
        anchor_index = _target_insert_index(rows)
        non_target_rows = [
            row
            for row in rows
            if not (isinstance(row, list) and len(row) > 1 and _metric_key(str(row[1])) in TARGET_METRIC_KEYS)
        ]
        target_block = [expected_rows[row_id] for row_id in expected_target_ids]
        final_rows = [*non_target_rows[:anchor_index], *target_block, *non_target_rows[anchor_index:]]

        structure_changed = rows != final_rows
        if structure_changed:
            data_sheet["rows"] = final_rows
            _update_data_dimensions(data_sheet)
            _extend_timestamp_map_for_inserted_rows(
                plan,
                inserted_ids=inserted_ids,
                rows_by_id=rows_by_id,
                refreshed_at=snapshot.refreshed_at,
            )

        result.inserted_sku_rows = sum(row_id.startswith(SKU_SCOPE_PREFIX) for row_id in inserted_ids)
        result.inserted_total_rows = sum(row_id.startswith(f"{TOTAL_SCOPE}|") for row_id in inserted_ids)
        result.changed_rows = len(inserted_ids | changed_existing_ids)
        after_digest = non_target_plan_digest(plan)
        result.non_target_digest_after = after_digest
        if before_digest != after_digest:
            result.blockers.append(
                {
                    "bundle_version": snapshot.bundle_version,
                    "snapshot_as_of_date": snapshot.as_of_date,
                    "reason": "non_target_preservation_digest_mismatch",
                    "before": before_digest,
                    "after": after_digest,
                }
            )
            return result

        if structure_changed:
            result.new_plan_json = json.dumps(
                plan,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        else:
            result.non_target_digest_after = before_digest
        if existing_target_ids and existing_target_ids != expected_target_ids and not structure_changed:
            raise AssertionError("target order validation must be reflected in structure_changed")
        return result
    except (BackfillPlanError, TypeError, ValueError) as exc:
        result.blockers.append(
            {
                "bundle_version": snapshot.bundle_version,
                "snapshot_as_of_date": snapshot.as_of_date,
                "reason": str(exc),
            }
        )
        result.non_target_digest_after = result.non_target_digest_before
        return result


def ready_snapshot_fingerprint(snapshots: Iterable[ReadySnapshotInput]) -> str:
    payload = [
        {
            "bundle_version": item.bundle_version,
            "activated_at": item.activated_at,
            "as_of_date": item.as_of_date,
            "snapshot_id": item.snapshot_id,
            "plan_version": item.plan_version,
            "refreshed_at": item.refreshed_at,
            "plan_json_sha256": hashlib.sha256(item.plan_json.encode("utf-8")).hexdigest(),
        }
        for item in sorted(snapshots, key=lambda value: (value.bundle_version, value.as_of_date))
    ]
    return _sha256_json(payload)


def snapshot_identity_digest(snapshots: Iterable[ReadySnapshotInput]) -> str:
    payload = [
        {
            "bundle_version": item.bundle_version,
            "activated_at": item.activated_at,
            "as_of_date": item.as_of_date,
            "snapshot_id": item.snapshot_id,
            "plan_version": item.plan_version,
            "refreshed_at": item.refreshed_at,
        }
        for item in sorted(snapshots, key=lambda value: (value.bundle_version, value.as_of_date))
    ]
    return _sha256_json(payload)


def non_target_plan_digest(plan: Mapping[str, Any]) -> str:
    semantic = deepcopy(dict(plan))
    sheets = semantic.get("sheets")
    if isinstance(sheets, list):
        for sheet in sheets:
            if not isinstance(sheet, dict) or sheet.get("sheet_name") != DATA_SHEET_NAME:
                continue
            rows = sheet.get("rows")
            if isinstance(rows, list):
                sheet["rows"] = [
                    row
                    for row in rows
                    if not (
                        isinstance(row, list)
                        and len(row) > 1
                        and _metric_key(str(row[1])) in TARGET_METRIC_KEYS
                    )
                ]
                _update_data_dimensions(sheet)
    metadata = semantic.get("metadata")
    if isinstance(metadata, dict):
        timestamp_map = metadata.get(ROW_TIMESTAMP_MAP_KEY)
        if isinstance(timestamp_map, dict):
            metadata[ROW_TIMESTAMP_MAP_KEY] = {
                row_id: value
                for row_id, value in timestamp_map.items()
                if _metric_key(str(row_id)) not in TARGET_METRIC_KEYS
            }
    return _sha256_json(semantic)


def _target_specs(*, scopes: Sequence[str], rows_by_id: Mapping[str, list[Any]]) -> list[dict[str, str]]:
    specs = [
        {
            "scope": TOTAL_SCOPE,
            "row_id": f"{TOTAL_SCOPE}|{OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY}",
            "label": f"Итого: {OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_LABEL}",
        }
    ]
    for scope in scopes:
        label_source = _first_existing_row(
            rows_by_id,
            (
                f"{scope}|{OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY}",
                f"{scope}|{SKU_MARGIN_2_KEY}",
                f"{scope}|{SKU_ORDER_SUM_KEY}",
            ),
        )
        prefix = _label_prefix(str(label_source[0])) if label_source else scope
        specs.append(
            {
                "scope": scope,
                "row_id": f"{scope}|{OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY}",
                "label": f"{prefix}: {OUR_WB_PROXY_MARGIN_3_PCT_LABEL}",
            }
        )
    return specs


def _expected_margin_cell(
    *,
    row_id: str,
    scope: str,
    column_date: str,
    column_index: int,
    rows_by_id: Mapping[str, list[Any]],
) -> tuple[Any, dict[str, Any]]:
    is_total = scope == TOTAL_SCOPE
    numerator_id = (
        f"{TOTAL_SCOPE}|{OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY}"
        if is_total
        else f"{scope}|{OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY}"
    )
    denominator_id = (
        f"{TOTAL_SCOPE}|{TOTAL_ORDER_SUM_KEY}"
        if is_total
        else f"{scope}|{SKU_ORDER_SUM_KEY}"
    )
    margin2_id = (
        f"{TOTAL_SCOPE}|{TOTAL_MARGIN_2_KEY}"
        if is_total
        else f"{scope}|{SKU_MARGIN_2_KEY}"
    )
    numerator = _operand_cell(rows_by_id.get(numerator_id), column_index, row_id=numerator_id)
    denominator = _operand_cell(rows_by_id.get(denominator_id), column_index, row_id=denominator_id)
    evidence = {
        "fallback": False,
        "source_row_id": numerator_id,
        "zero_denominator": False,
        "blank_operands": [],
    }
    if numerator is None and column_date < OUR_WB_COST_OPENING_DATE:
        margin2 = _operand_cell(rows_by_id.get(margin2_id), column_index, row_id=margin2_id)
        if margin2 is not None:
            evidence.update({"fallback": True, "source_row_id": margin2_id})
            return _to_sheet_value(margin2), evidence
    if numerator is None:
        evidence["blank_operands"].append(numerator_id)
    if denominator is None:
        evidence["blank_operands"].append(denominator_id)
    if evidence["blank_operands"]:
        return "", evidence
    if denominator == 0:
        evidence["zero_denominator"] = True
        return 0.0, evidence
    return _to_sheet_value(numerator / denominator), evidence


def _compare_target_cell(*, current: Any, expected: Any) -> str:
    if _is_blank(current):
        return "same" if _is_blank(expected) else "change"
    if _is_blank(expected):
        return "conflict"
    current_number = _finite_number(current, field_name="existing target cell")
    expected_number = _finite_number(expected, field_name="expected target cell")
    return "same" if abs(current_number - expected_number) <= _CELL_TOLERANCE else "conflict"


def _target_insert_index(rows: Sequence[Any]) -> int:
    non_target_rows = [
        row
        for row in rows
        if not (isinstance(row, list) and len(row) > 1 and _metric_key(str(row[1])) in TARGET_METRIC_KEYS)
    ]
    for metric_group in _ANCHOR_METRIC_GROUPS:
        indices = [
            index
            for index, row in enumerate(non_target_rows)
            if isinstance(row, list) and len(row) > 1 and _metric_key(str(row[1])) in metric_group
        ]
        if indices:
            return max(indices) + 1
    return 0


def _extend_timestamp_map_for_inserted_rows(
    plan: dict[str, Any],
    *,
    inserted_ids: set[str],
    rows_by_id: Mapping[str, list[Any]],
    refreshed_at: str,
) -> None:
    if not inserted_ids:
        return
    metadata = plan.get("metadata")
    if not isinstance(metadata, dict):
        return
    timestamp_map = metadata.get(ROW_TIMESTAMP_MAP_KEY)
    if not isinstance(timestamp_map, dict):
        return
    for row_id in sorted(inserted_ids):
        scope = row_id.split("|", 1)[0]
        anchors = (
            f"{scope}|{OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY}"
            if scope == TOTAL_SCOPE
            else f"{scope}|{OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY}",
            f"{scope}|{TOTAL_MARGIN_2_KEY if scope == TOTAL_SCOPE else SKU_MARGIN_2_KEY}",
            f"{scope}|{'total_proxy_profit_2_rub' if scope == TOTAL_SCOPE else 'proxy_profit_2_rub'}",
            f"{scope}|{'total_proxy_profit_rub' if scope == TOTAL_SCOPE else 'proxy_profit_rub'}",
        )
        timestamps = [str(timestamp_map.get(anchor) or "") for anchor in anchors if anchor in rows_by_id]
        timestamp_map[row_id] = max((value for value in timestamps if value), default=refreshed_at)


def _date_columns(plan: Mapping[str, Any]) -> list[str]:
    raw = plan.get("date_columns")
    if raw is None:
        data_sheet = _data_sheet(plan)
        header = data_sheet.get("header")
        raw = header[2:] if isinstance(header, list) else []
    if not isinstance(raw, list) or any(not isinstance(value, str) for value in raw):
        raise BackfillPlanError("date_columns must be a list of ISO dates")
    if any(not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) for value in raw):
        raise BackfillPlanError("date_columns contains a non-ISO date")
    if len(raw) != len(set(raw)):
        raise BackfillPlanError("date_columns contains duplicates")
    return list(raw)


def _data_sheet(plan: Mapping[str, Any]) -> dict[str, Any]:
    sheets = plan.get("sheets")
    if not isinstance(sheets, list):
        raise BackfillPlanError("plan.sheets must be a list")
    matches = [sheet for sheet in sheets if isinstance(sheet, dict) and sheet.get("sheet_name") == DATA_SHEET_NAME]
    if len(matches) != 1:
        raise BackfillPlanError("plan must contain exactly one DATA_VITRINA sheet")
    return matches[0]


def _validate_header(data_sheet: Mapping[str, Any], date_columns: Sequence[str]) -> None:
    header = data_sheet.get("header")
    if not isinstance(header, list) or len(header) != _CELL_WIDTH_PREFIX + len(date_columns):
        raise BackfillPlanError("DATA_VITRINA.header width does not match date_columns")
    if [str(value) for value in header[2:]] != list(date_columns):
        raise BackfillPlanError("DATA_VITRINA.header dates do not match date_columns")


def _rows_by_id(rows: Sequence[Any]) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 2:
            continue
        row_id = str(row[1] or "").strip()
        if not row_id:
            continue
        if row_id in result:
            raise BackfillPlanError(f"duplicate DATA_VITRINA row_id: {row_id}")
        result[row_id] = row
    return result


def _historical_sku_scopes(rows: Sequence[Any]) -> list[str]:
    scopes: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, list) or len(row) < 2:
            continue
        row_id = str(row[1] or "")
        scope = row_id.split("|", 1)[0]
        if scope.startswith(SKU_SCOPE_PREFIX) and scope not in seen:
            seen.add(scope)
            scopes.append(scope)
    if not scopes:
        raise BackfillPlanError("DATA_VITRINA has no historical SKU scope rows")
    return scopes


def _operand_cell(row: list[Any] | None, column_index: int, *, row_id: str) -> float | None:
    if row is None:
        return None
    value = _row_cell(row, column_index)
    if _is_blank(value):
        return None
    return _finite_number(value, field_name=f"operand {row_id}")


def _row_cell(row: list[Any] | None, column_index: int) -> Any:
    value_index = _CELL_WIDTH_PREFIX + column_index
    if row is None or value_index >= len(row):
        return ""
    return row[value_index]


def _finite_number(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise BackfillPlanError(f"{field_name} must not be boolean")
    try:
        number = float(str(value).strip().replace(",", ".")) if isinstance(value, str) else float(value)
    except (TypeError, ValueError) as exc:
        raise BackfillPlanError(f"{field_name} contains non-numeric value {value!r}") from exc
    if not math.isfinite(number):
        raise BackfillPlanError(f"{field_name} contains NaN/Infinity")
    return number


def _to_sheet_value(value: float | None) -> Any:
    if value is None:
        return ""
    number = _finite_number(value, field_name="derived margin 3")
    return round(number, 6)


def _is_blank(value: Any) -> bool:
    return value is None or value == ""


def _metric_key(row_id: str) -> str:
    return row_id.split("|", 1)[1] if "|" in row_id else ""


def _first_existing_row(rows_by_id: Mapping[str, list[Any]], row_ids: Sequence[str]) -> list[Any] | None:
    return next((rows_by_id[row_id] for row_id in row_ids if row_id in rows_by_id), None)


def _label_prefix(value: str) -> str:
    return value.split(": ", 1)[0] if ": " in value else value


def _update_data_dimensions(data_sheet: dict[str, Any]) -> None:
    rows = data_sheet.get("rows")
    header = data_sheet.get("header")
    if not isinstance(rows, list) or not isinstance(header, list):
        raise BackfillPlanError("DATA_VITRINA dimensions require list rows/header")
    row_count = len(rows)
    column_count = len(header)
    data_sheet["row_count"] = row_count
    data_sheet["write_rect"] = _build_write_rect(
        write_start_cell=str(data_sheet.get("write_start_cell") or "A1"),
        column_count=column_count,
        row_count=row_count,
    )


def _build_write_rect(*, write_start_cell: str, column_count: int, row_count: int) -> str:
    match = _CELL_REF_RE.fullmatch(write_start_cell.strip().upper())
    if match is None:
        raise BackfillPlanError(f"invalid DATA_VITRINA.write_start_cell: {write_start_cell!r}")
    start_column = _column_number(match.group(1))
    start_row = int(match.group(2))
    end_column = _column_letters(start_column + column_count - 1)
    end_row = start_row + row_count
    return f"{write_start_cell.strip().upper()}:{end_column}{end_row}"


def _column_number(letters: str) -> int:
    result = 0
    for char in letters:
        result = result * 26 + (ord(char) - ord("A") + 1)
    return result


def _column_letters(number: int) -> str:
    if number <= 0:
        raise BackfillPlanError("column number must be positive")
    letters: list[str] = []
    current = number
    while current:
        current, remainder = divmod(current - 1, 26)
        letters.append(chr(ord("A") + remainder))
    return "".join(reversed(letters))


def _strict_json_loads(raw: str) -> Any:
    def _reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant {value}")

    return json.loads(raw, parse_constant=_reject_constant)


def _sha256_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _blocked_transform(snapshot: ReadySnapshotInput, reason: str) -> SnapshotTransform:
    return SnapshotTransform(
        snapshot=snapshot,
        new_plan_json=None,
        date_columns=[],
        blockers=[
            {
                "bundle_version": snapshot.bundle_version,
                "snapshot_as_of_date": snapshot.as_of_date,
                "reason": reason,
            }
        ],
    )
