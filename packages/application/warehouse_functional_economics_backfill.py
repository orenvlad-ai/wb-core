"""Guarded targeted ready-snapshot publication of functional WB economics."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import sqlite3
from typing import Any, Mapping

from packages.application.calculation_parameters import (
    CalculationParametersBlock,
    calculate_proxy_3,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_our_wb_costs import (
    OUR_WB_PROXY_MARGIN_3_PCT_LABEL,
    OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
    OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_LABEL,
    OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY,
    OUR_WB_PROXY_PROFIT_3_RUB_LABEL,
    OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_UNIT_COST_RUB_LABEL,
    OUR_WB_UNIT_COST_RUB_METRIC_KEY,
    TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY,
)
from packages.application.sheet_vitrina_v1_archived_metrics import ARCHIVED_PUBLIC_METRIC_KEYS
from packages.application.sheet_vitrina_v1_proxy_margin_3_historical_backfill import (
    _data_sheet,
    _date_columns,
    _update_data_dimensions,
)
from packages.application.warehouse_functional import FUNCTIONAL_CUTOVER_ID


CONTRACT_NAME = "sheet_vitrina_v1_functional_economics_backfill"
TARGET_KEYS = {
    OUR_WB_UNIT_COST_RUB_METRIC_KEY,
    TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY,
    OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
    OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY,
}
ARCHIVED_READY_METRIC_KEYS = ARCHIVED_PUBLIC_METRIC_KEYS
MUTATED_READY_METRIC_KEYS = frozenset(TARGET_KEYS | set(ARCHIVED_READY_METRIC_KEYS))
ZERO = Decimal("0")


class FunctionalEconomicsBackfillError(RuntimeError):
    pass


def build_functional_economics_backfill_plan(
    runtime: RegistryUploadDbBackedRuntime,
) -> dict[str, Any]:
    with _connect(runtime.db_path) as conn:
        cutover = conn.execute(
            """SELECT cutover_at,plan_fingerprint FROM sheet_vitrina_v1_warehouse_functional_cutovers
               WHERE cutover_id=? AND status='posted'""",
            (FUNCTIONAL_CUTOVER_ID,),
        ).fetchone()
        if cutover is None:
            raise FunctionalEconomicsBackfillError("functional cutover is not posted")
        snapshots = [dict(row) for row in conn.execute(
            """SELECT bundle_version,as_of_date,plan_json,refreshed_at
               FROM sheet_vitrina_v1_ready_snapshots ORDER BY bundle_version,as_of_date"""
        ).fetchall()]
    dates = sorted({day for row in snapshots for day in _snapshot_dates(row["plan_json"]) if day >= "2026-07-01"})
    costs = {day: runtime.load_our_wb_cost_daily_state(as_of_date=day) for day in dates}
    parameters = CalculationParametersBlock(runtime=runtime)
    parameter_by_date = {day: parameters.parameters_for_date(day) for day in dates}
    source_fingerprint = "sha256:" + _hash(
        {
            "cutover_fingerprint": str(cutover["plan_fingerprint"]),
            "costs": costs,
            "parameters": {day: item.public() for day, item in parameter_by_date.items()},
        }
    )
    updates: list[dict[str, Any]] = []
    changed_cells = 0
    inserted_rows = 0
    archived_rows_removed = 0
    non_target_before: list[list[str]] = []
    non_target_after: list[list[str]] = []
    non_target_mismatches: list[dict[str, str]] = []
    for snapshot in snapshots:
        try:
            transformed = _transform_snapshot(
                snapshot,
                costs=costs,
                parameters=parameter_by_date,
                source_fingerprint=source_fingerprint,
            )
        except Exception as exc:
            raise FunctionalEconomicsBackfillError(
                "functional economics ready snapshot failed: "
                f"bundle_version={snapshot['bundle_version']} "
                f"as_of_date={snapshot['as_of_date']}: {exc}"
            ) from exc
        non_target_before.append([snapshot["bundle_version"], snapshot["as_of_date"], transformed["non_target_before"]])
        non_target_after.append([snapshot["bundle_version"], snapshot["as_of_date"], transformed["non_target_after"]])
        if transformed["non_target_before"] != transformed["non_target_after"]:
            non_target_mismatches.append(
                {
                    "bundle_version": str(snapshot["bundle_version"]),
                    "as_of_date": str(snapshot["as_of_date"]),
                    "before_digest": str(transformed["non_target_before"]),
                    "after_digest": str(transformed["non_target_after"]),
                }
            )
        changed_cells += int(transformed["changed_cells"])
        inserted_rows += int(transformed["inserted_rows"])
        archived_rows_removed += int(transformed["archived_rows_removed"])
        if transformed["after_plan_json"] != snapshot["plan_json"]:
            updates.append(
                {
                    "bundle_version": snapshot["bundle_version"],
                    "as_of_date": snapshot["as_of_date"],
                    "before_plan_sha256": "sha256:" + _sha(snapshot["plan_json"]),
                    "after_plan_json": transformed["after_plan_json"],
                    "changed_cells": transformed["changed_cells"],
                    "inserted_rows": transformed["inserted_rows"],
                    "archived_rows_removed": transformed["archived_rows_removed"],
                    "dates": transformed["dates"],
                }
            )
    before_digest = "sha256:" + _hash(non_target_before)
    after_digest = "sha256:" + _hash(non_target_after)
    if before_digest != after_digest or non_target_mismatches:
        raise FunctionalEconomicsBackfillError(
            "non-target ready-snapshot content changed: "
            + json.dumps(
                non_target_mismatches[:20],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    plan = {
        "contract_name": CONTRACT_NAME,
        "contract_version": "v1",
        "status": "dry_run_ready",
        "cutover_id": FUNCTIONAL_CUTOVER_ID,
        "cutover_at": str(cutover["cutover_at"]),
        "source_fingerprint": source_fingerprint,
        "snapshot_count": len(snapshots),
        "date_from": dates[0] if dates else None,
        "date_to": dates[-1] if dates else None,
        "changed_snapshot_count": len(updates),
        "changed_cell_count": changed_cells,
        "inserted_row_count": inserted_rows,
        "archived_row_count": archived_rows_removed,
        "archived_metric_keys": sorted(ARCHIVED_READY_METRIC_KEYS),
        "ready_snapshot_manifest_digest": _snapshot_manifest_digest(snapshots),
        "non_target_digest": before_digest,
        "updates": updates,
    }
    plan["plan_fingerprint"] = _plan_fingerprint(plan)
    return plan


def apply_functional_economics_backfill_plan(
    runtime: RegistryUploadDbBackedRuntime,
    plan: Mapping[str, Any],
    *,
    confirm_fingerprint: str,
    backup_dir: Any,
) -> dict[str, Any]:
    normalized = json.loads(json.dumps(dict(plan), ensure_ascii=False))
    fingerprint = str(normalized.get("plan_fingerprint") or "")
    if fingerprint != str(confirm_fingerprint or "") or fingerprint != _plan_fingerprint(
        {key: value for key, value in normalized.items() if key != "plan_fingerprint"}
    ):
        raise FunctionalEconomicsBackfillError("exact functional economics plan fingerprint is required")
    fresh = build_functional_economics_backfill_plan(runtime)
    if str(fresh["plan_fingerprint"]) != fingerprint:
        raise FunctionalEconomicsBackfillError(
            "functional cost/settings or ready snapshots drifted after dry-run"
        )
    if not normalized.get("updates"):
        return {**fresh, "status": "applied", "idempotent": True, "database_written": False}
    from pathlib import Path

    backup_root = Path(backup_dir)
    if not backup_root.is_absolute():
        raise FunctionalEconomicsBackfillError("absolute backup_dir is required")
    backup_root.mkdir(parents=True, exist_ok=True)
    destination = backup_root / f"functional-economics-{fingerprint.removeprefix('sha256:')[:16]}.sqlite3"
    if destination.exists():
        destination = backup_root / f"functional-economics-{fingerprint.removeprefix('sha256:')[:24]}.sqlite3"
    backup = runtime.backup_database(destination)
    destination.chmod(0o600)
    if str(backup.get("integrity_check") or "") != "ok":
        raise FunctionalEconomicsBackfillError("functional economics backup integrity_check failed")
    with _connect(runtime.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            locked_snapshots = [dict(row) for row in conn.execute(
                """SELECT bundle_version,as_of_date,plan_json,refreshed_at
                   FROM sheet_vitrina_v1_ready_snapshots ORDER BY bundle_version,as_of_date"""
            ).fetchall()]
            if _snapshot_manifest_digest(locked_snapshots) != str(
                normalized.get("ready_snapshot_manifest_digest") or ""
            ):
                raise FunctionalEconomicsBackfillError(
                    "ready snapshot manifest drifted before atomic backfill"
                )
            for item in normalized["updates"]:
                before = conn.execute(
                    """SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots
                       WHERE bundle_version=? AND as_of_date=?""",
                    (item["bundle_version"], item["as_of_date"]),
                ).fetchone()
                if before is None or "sha256:" + _sha(str(before["plan_json"])) != item["before_plan_sha256"]:
                    raise FunctionalEconomicsBackfillError("ready snapshot drifted before atomic backfill")
                cursor = conn.execute(
                    """UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?
                       WHERE bundle_version=? AND as_of_date=? AND plan_json=?""",
                    (
                        item["after_plan_json"],
                        item["bundle_version"],
                        item["as_of_date"],
                        before["plan_json"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise FunctionalEconomicsBackfillError("ready snapshot optimistic update conflict")
            for item in normalized["updates"]:
                stored = conn.execute(
                    """SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots
                       WHERE bundle_version=? AND as_of_date=?""",
                    (item["bundle_version"], item["as_of_date"]),
                ).fetchone()
                if stored is None or str(stored["plan_json"]) != str(item["after_plan_json"]):
                    raise FunctionalEconomicsBackfillError(
                        "functional economics in-transaction readback failed"
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    readback = build_functional_economics_backfill_plan(runtime)
    if readback.get("updates"):
        raise FunctionalEconomicsBackfillError("functional economics backfill is not idempotent")
    return {
        **readback,
        "status": "applied",
        "idempotent": False,
        "database_written": True,
        "applied_snapshot_count": len(normalized["updates"]),
        "backup": backup,
        "applied_plan_fingerprint": fingerprint,
    }


def _transform_snapshot(
    snapshot: Mapping[str, Any],
    *,
    costs: Mapping[str, Mapping[int, Mapping[str, Any]]],
    parameters: Mapping[str, Any],
    source_fingerprint: str,
) -> dict[str, Any]:
    original = json.loads(str(snapshot["plan_json"]))
    plan = deepcopy(original)
    sheet = _data_sheet(plan)
    rows = sheet.get("rows")
    if not isinstance(rows, list):
        raise FunctionalEconomicsBackfillError("DATA_VITRINA rows are missing")
    dates = _date_columns(plan)
    relevant_indices = [index for index, day in enumerate(dates) if day >= "2026-07-01"]
    before_digest = _non_target_digest(original)
    _validate_data_projection_layout(sheet, dates=dates)
    archived_rows_removed = _remove_archived_metric_rows(rows)
    metadata_present = "metadata" in plan
    metadata = plan.get("metadata") if metadata_present else {}
    if not isinstance(metadata, dict):
        raise FunctionalEconomicsBackfillError("ready snapshot metadata must be an object")
    timestamps = metadata.get("row_last_updated_at_by_row_id")
    if isinstance(timestamps, dict):
        for row_id in list(timestamps):
            if "|" in row_id and row_id.split("|", 1)[1] in ARCHIVED_READY_METRIC_KEYS:
                timestamps.pop(row_id, None)
    if not relevant_indices:
        if plan == original:
            return {
                "after_plan_json": str(snapshot["plan_json"]),
                "changed_cells": 0,
                "inserted_rows": 0,
                "archived_rows_removed": 0,
                "dates": [],
                "non_target_before": before_digest,
                "non_target_after": before_digest,
            }
        if archived_rows_removed:
            _update_data_dimensions(sheet)
        after = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return {
            "after_plan_json": after,
            "changed_cells": 0,
            "inserted_rows": 0,
            "archived_rows_removed": archived_rows_removed,
            "dates": [],
            "non_target_before": before_digest,
            "non_target_after": _non_target_digest(plan),
        }
    by_id = _rows_by_id(rows)
    scopes = sorted({row_id.split("|", 1)[0] for row_id in by_id if row_id.startswith("SKU:")})
    if not scopes:
        raise FunctionalEconomicsBackfillError("ready snapshot has no SKU scopes")
    if not metadata_present:
        plan["metadata"] = metadata
    inserted = _ensure_target_rows(rows, by_id=by_id, scopes=scopes, date_count=len(dates))
    by_id = _rows_by_id(rows)
    changed = 0
    sku_result: dict[tuple[str, int], dict[str, Decimal | None]] = {}
    for index in relevant_indices:
        day = dates[index]
        params = parameters[day]
        for scope in scopes:
            nm_id = int(scope.split(":", 1)[1])
            cost_state = costs.get(day, {}).get(nm_id)
            cost = _optional_decimal((cost_state or {}).get("our_wb_unit_cost_rub"))
            order_sum = _cell_decimal(by_id.get(f"{scope}|orderSum"), index)
            order_count = _cell_decimal(by_id.get(f"{scope}|orderCount"), index)
            ads_sum = _cell_decimal(by_id.get(f"{scope}|ads_sum"), index)
            calculated = calculate_proxy_3(
                order_sum=order_sum,
                order_count=order_count,
                canonical_wb_wac=cost,
                ads_sum=ads_sum,
                parameters=params,
            )
            values = {
                OUR_WB_UNIT_COST_RUB_METRIC_KEY: cost,
                OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY: calculated["proxy_profit_3"],
                OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY: calculated["proxy_margin_3"],
            }
            sku_result[(scope, index)] = calculated
            for metric_key, value in values.items():
                changed += _set_cell(by_id[f"{scope}|{metric_key}"], index, value)

        complete = [sku_result[(scope, index)] for scope in scopes]
        profits = [item["proxy_profit_3"] for item in complete]
        revenues = [item["expected_buyout_revenue"] for item in complete]
        total_profit = None if any(value is None for value in profits) else sum((value for value in profits if value is not None), ZERO)
        total_revenue = None if any(value is None for value in revenues) else sum((value for value in revenues if value is not None), ZERO)
        total_margin = None if total_revenue in (None, ZERO) or total_profit is None else total_profit / total_revenue
        # Public TOTAL WB cost is the whole official contour, not merely the
        # configured/visible SKU subset used for Proxy row aggregation.
        cost_states = list(costs.get(day, {}).values())
        quantity_cost_pairs = [
            (
                _optional_decimal((item or {}).get("stock_qty")) or ZERO,
                _optional_decimal((item or {}).get("our_wb_unit_cost_rub")),
            )
            for item in cost_states
        ]
        total_qty = sum((quantity for quantity, _ in quantity_cost_pairs), ZERO)
        missing_positive_cost = any(quantity > ZERO and cost is None for quantity, cost in quantity_cost_pairs)
        total_capital = sum(
            (quantity * cost for quantity, cost in quantity_cost_pairs if cost is not None),
            ZERO,
        )
        total_cost = total_capital / total_qty if total_qty > ZERO and not missing_positive_cost else None
        total_values = {
            TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY: total_cost,
            OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY: total_profit,
            OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY: total_margin,
        }
        for metric_key, value in total_values.items():
            changed += _set_cell(by_id[f"TOTAL|{metric_key}"], index, value)
    marker = {
        "cutover_id": FUNCTIONAL_CUTOVER_ID,
        "source_fingerprint": source_fingerprint,
        "date_from": dates[relevant_indices[0]],
        "date_to": dates[relevant_indices[-1]],
        "target_metric_keys": sorted(TARGET_KEYS),
        "archived_metric_keys": sorted(ARCHIVED_READY_METRIC_KEYS),
    }
    if metadata.get("functional_economics_backfill") != marker:
        metadata["functional_economics_backfill"] = marker
    timestamps = metadata.setdefault("row_last_updated_at_by_row_id", {})
    if isinstance(timestamps, dict):
        for row_id in by_id:
            if "|" in row_id and row_id.split("|", 1)[1] in TARGET_KEYS:
                timestamps[row_id] = str(snapshot.get("refreshed_at") or "")
    if inserted or archived_rows_removed:
        _update_data_dimensions(sheet)
    after = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    after_digest = _non_target_digest(plan)
    return {
        "after_plan_json": after,
        "changed_cells": changed,
        "inserted_rows": inserted,
        "archived_rows_removed": archived_rows_removed,
        "dates": [dates[index] for index in relevant_indices],
        "non_target_before": before_digest,
        "non_target_after": after_digest,
    }


def _ensure_target_rows(
    rows: list[Any],
    *,
    by_id: Mapping[str, list[Any]],
    scopes: list[str],
    date_count: int,
) -> int:
    specs = [
        ("TOTAL", TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY, OUR_WB_UNIT_COST_RUB_LABEL),
        ("TOTAL", OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY, OUR_WB_PROXY_PROFIT_3_RUB_LABEL),
        ("TOTAL", OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY, OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_LABEL),
    ]
    for scope in scopes:
        prefix = _scope_label_prefix(by_id, scope)
        specs.extend(
            [
                (scope, OUR_WB_UNIT_COST_RUB_METRIC_KEY, f"{prefix}: {OUR_WB_UNIT_COST_RUB_LABEL}"),
                (scope, OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY, f"{prefix}: {OUR_WB_PROXY_PROFIT_3_RUB_LABEL}"),
                (scope, OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY, f"{prefix}: {OUR_WB_PROXY_MARGIN_3_PCT_LABEL}"),
            ]
        )
    inserted = 0
    for scope, metric, label in specs:
        row_id = f"{scope}|{metric}"
        if row_id in by_id:
            continue
        rows.append([label, row_id, *([""] * date_count)])
        inserted += 1
    return inserted


def _remove_archived_metric_rows(rows: list[Any]) -> int:
    retained = []
    removed = 0
    for row in rows:
        row_id = str(row[1] or "") if isinstance(row, list) and len(row) > 1 else ""
        metric_key = row_id.split("|", 1)[1] if "|" in row_id else ""
        if metric_key in ARCHIVED_READY_METRIC_KEYS:
            removed += 1
            continue
        retained.append(row)
    if removed:
        rows[:] = retained
    return removed


def _scope_label_prefix(by_id: Mapping[str, list[Any]], scope: str) -> str:
    for suffix in ("orderSum", "proxy_profit_2_rub", "proxy_profit_rub"):
        row = by_id.get(f"{scope}|{suffix}")
        if row:
            label = str(row[0] or scope)
            return label.split(": ", 1)[0]
    return scope


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
        return abs(Decimal(str(current).replace(",", ".")) - Decimal(str(expected))) <= Decimal("0.0000005")
    except (InvalidOperation, ValueError):
        return False


def _cell_decimal(row: list[Any] | None, index: int) -> Decimal | None:
    if row is None or len(row) <= 2 + index or row[2 + index] in (None, ""):
        return None
    return _optional_decimal(row[2 + index])


def _optional_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _rows_by_id(rows: list[Any]) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 2 or not isinstance(row[1], str):
            continue
        row_id = row[1].strip()
        # Historical ready snapshots can retain presentation-only rows whose
        # second cell is a value rather than a stable projection key.  Public
        # vitrina reads already ignore those rows.  Preserve them byte-for-byte
        # and index only the same stable ``scope|metric`` contract here.
        if not _is_projection_row_id(row_id):
            continue
        if row_id in result:
            raise FunctionalEconomicsBackfillError(f"duplicate ready projection row id: {row_id}")
        result[row_id] = row
    return result


def _is_projection_row_id(value: str) -> bool:
    scope, separator, metric = str(value or "").partition("|")
    return bool(separator and scope.strip() and metric.strip())


def _validate_data_projection_layout(sheet: Mapping[str, Any], *, dates: list[str]) -> None:
    header = sheet.get("header")
    if not isinstance(header, list):
        raise FunctionalEconomicsBackfillError("DATA_VITRINA header is missing")
    if len(header) != 2 + len(dates):
        raise FunctionalEconomicsBackfillError(
            "DATA_VITRINA header width does not match date_columns"
        )
    if [str(value) for value in header[2:]] != dates:
        raise FunctionalEconomicsBackfillError(
            "DATA_VITRINA header dates do not match date_columns"
        )


def _snapshot_dates(plan_json: str) -> list[str]:
    try:
        payload = json.loads(str(plan_json))
        return _date_columns(payload)
    except Exception as exc:
        raise FunctionalEconomicsBackfillError(f"invalid ready snapshot plan: {exc}") from exc


def _non_target_digest(plan: Mapping[str, Any]) -> str:
    value = deepcopy(dict(plan))
    sheet = _data_sheet(value)
    rows = sheet.get("rows") or []
    sheet["rows"] = [
        row
        for row in rows
        if not (
            isinstance(row, list)
            and len(row) > 1
            and "|" in str(row[1])
            and str(row[1]).split("|", 1)[1] in MUTATED_READY_METRIC_KEYS
        )
    ]
    sheet.pop("row_count", None)
    sheet.pop("write_rect", None)
    metadata = value.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("functional_economics_backfill", None)
        timestamps = metadata.get("row_last_updated_at_by_row_id")
        if isinstance(timestamps, dict):
            for row_id in list(timestamps):
                if "|" in row_id and row_id.split("|", 1)[1] in MUTATED_READY_METRIC_KEYS:
                    timestamps.pop(row_id, None)
            if not timestamps:
                metadata.pop("row_last_updated_at_by_row_id", None)
        if not metadata:
            value.pop("metadata", None)
    return "sha256:" + _hash(value)


def _plan_fingerprint(plan: Mapping[str, Any]) -> str:
    return "sha256:" + _hash({key: value for key, value in plan.items() if key != "plan_fingerprint"})


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _snapshot_manifest_digest(rows: list[Mapping[str, Any]]) -> str:
    return "sha256:" + _hash(
        [
            [
                str(row.get("bundle_version") or ""),
                str(row.get("as_of_date") or ""),
                "sha256:" + _sha(str(row.get("plan_json") or "")),
                str(row.get("refreshed_at") or ""),
            ]
            for row in rows
        ]
    )


def _connect(path: Any) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn
