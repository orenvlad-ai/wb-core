"""Exact partial WB/WAC publication for 01--18 July 2026.

Only the immutable persisted daily WB rows are published.  The other five
warehouse stages and the all-stage totals remain explicitly unavailable.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
import sqlite3
from typing import Any, Mapping, Sequence

from packages.application.calculation_parameters import (
    DEFAULT_PROXY_PARAMETERS,
    PROXY_BLOCK_KEY,
    _parameters_from_row,
)
from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
)
from packages.application.sheet_vitrina_v1_own_product_capital import (
    OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS,
    OWN_PRODUCT_CAPITAL_TOTAL_METRIC_KEYS,
    own_stage_metric_key,
    own_stage_total_metric_key,
)
from packages.application.sheet_vitrina_v1_proxy_margin_3_historical_backfill import (
    _data_sheet,
    _date_columns,
)
from packages.application.warehouse_business_projection import (
    CURRENT_ROW_TABLE,
    REVISION_TABLE,
    ROW_TABLE,
    STATE_TABLE,
    _persist_projection_revision,
)
from packages.application.warehouse_functional import FUNCTIONAL_CUTOVER_ID
from packages.application.warehouse_functional_economics_backfill import (
    _transform_snapshot,
)
from packages.application.warehouse_functional_lock import (
    warehouse_functional_write_lock,
)
from packages.application.warehouse_historical_recovery import (
    _apply_ready_updates,
    _before_image,
    _connect,
    _configured_nm_ids as _active_configured_nm_ids,
    _fingerprint,
    _set_ready_cell,
    _set_ready_presentation,
    _sha,
    _verify_projection_readback,
)
from packages.application.warehouse_recovery_policy import (
    RecoveryState,
    WarehouseRecoveryRegistry,
    recovery_operation_id,
)


CONTRACT_NAME = "warehouse_early_wb_recovery_2026_07_v1"
DATE_FROM = "2026-07-01"
DATE_TO = "2026-07-18"
DATES = tuple(f"2026-07-{day:02d}" for day in range(1, 19))
EXPECTED_ROWS_BY_DATE = {
    **{f"2026-07-{day:02d}": 33 for day in range(1, 13)},
    **{f"2026-07-{day:02d}": 37 for day in range(13, 17)},
    "2026-07-17": 33,
    "2026-07-18": 33,
}
EXPECTED_PERSISTED_ROW_COUNT = 610
EXPECTED_CONFIGURED_SKU_COUNT = 33
EXPECTED_UNAVAILABLE_STAGE_CELL_COUNT = 10692
ZERO = Decimal("0")
TOLERANCE = Decimal("0.0000001")
UNAVAILABLE_REASON = (
    "Для 01–18.07 доказан только exact-date WB quantity/WAC/capital. "
    "Полный шестиступенчатый складской снимок не сохранялся; соседний или "
    "current snapshot назад не копируется."
)


class WarehouseEarlyWbRecoveryError(RuntimeError):
    """Fail-closed partial historical publication violation."""


def build_early_wb_recovery_plan(
    runtime: RegistryUploadDbBackedRuntime,
) -> dict[str, Any]:
    with _connect(runtime.db_path, read_only=True) as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_warehouse_wb_daily_cost
                WHERE cutover_id=? AND as_of_date BETWEEN ? AND ?
                ORDER BY as_of_date,nm_id
                """,
                (FUNCTIONAL_CUTOVER_ID, DATE_FROM, DATE_TO),
            ).fetchall()
        ]
        _validate_daily_rows(rows)
        configured_nm_ids = _configured_nm_ids(conn)
        source_nm_ids = sorted({int(row["nm_id"]) for row in rows})
        configured_source_nm_ids = sorted(set(source_nm_ids) & configured_nm_ids)
        if len(configured_source_nm_ids) != EXPECTED_CONFIGURED_SKU_COUNT:
            raise WarehouseEarlyWbRecoveryError(
                "configured early-July WB SKU closure is no longer exactly 33"
            )
        source_material = {
            "contract_name": CONTRACT_NAME,
            "date_from": DATE_FROM,
            "date_to": DATE_TO,
            "daily_rows": [
                {
                    key: row[key]
                    for key in (
                        "as_of_date",
                        "nm_id",
                        "quantity",
                        "wac_rub",
                        "capital_rub",
                        "quality",
                        "fingerprint",
                    )
                }
                for row in rows
            ],
            "configured_nm_ids": configured_source_nm_ids,
        }
        source_digest = _fingerprint(source_material)
        projection_rows = _projection_rows(
            rows,
            source_digest=source_digest,
        )
        snapshots = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_ready_snapshots "
                "ORDER BY bundle_version,as_of_date"
            ).fetchall()
        ]
        ready_updates = _ready_updates(
            conn,
            snapshots=snapshots,
            rows=rows,
            configured_nm_ids=configured_source_nm_ids,
            source_digest=source_digest,
        )
        non_target_digest = _non_target_digest(conn)
        manifest = {
            "contract_name": CONTRACT_NAME,
            "scope": {
                "date_from": DATE_FROM,
                "date_to": DATE_TO,
                "source_nm_ids": source_nm_ids,
                "configured_nm_ids": configured_source_nm_ids,
            },
            "source_digest": source_digest,
            "non_target_digest": non_target_digest,
            "rows_by_date": {
                day: sum(str(row["as_of_date"]) == day for row in rows)
                for day in DATES
            },
            "quantity_by_date": {
                day: str(
                    sum(
                        (
                            _decimal(row["quantity"])
                            for row in rows
                            if str(row["as_of_date"]) == day
                        ),
                        ZERO,
                    )
                )
                for day in DATES
            },
            "capital_by_date": {
                day: str(
                    sum(
                        (
                            _decimal(row["capital_rub"])
                            for row in rows
                            if str(row["as_of_date"]) == day
                        ),
                        ZERO,
                    )
                )
                for day in DATES
            },
            "projection_row_fingerprints": [
                {
                    "as_of_date": row["as_of_date"],
                    "nm_id": row["nm_id"],
                    "row_fingerprint": row["row_fingerprint"],
                }
                for row in projection_rows
            ],
        }
        fingerprint = _fingerprint(manifest)
        revision_id = "whbpr_early_" + fingerprint.removeprefix("sha256:")[:20]
        existing = conn.execute(
            f"SELECT 1 FROM {REVISION_TABLE} "
            "WHERE plan_fingerprint=? AND status='active'",
            (fingerprint,),
        ).fetchone()
        return {
            **manifest,
            "fingerprint": fingerprint,
            "mode": "dry_run",
            "would_change": existing is None,
            "already_applied": existing is not None,
            "expected": {
                "persisted_daily_row_count": len(rows),
                "configured_daily_pair_count": sum(
                    int(row["nm_id"]) in configured_nm_ids for row in rows
                ),
                "extra_exact_iphone_row_count": len(rows)
                - sum(int(row["nm_id"]) in configured_nm_ids for row in rows),
                "projection_row_count": len(projection_rows),
                "ready_snapshot_update_count": len(ready_updates),
                "unavailable_six_stage_cell_count": (
                    EXPECTED_UNAVAILABLE_STAGE_CELL_COUNT
                ),
                "functional_version_count": 0,
                "primary_rows_changed": 0,
            },
            "recovery": {
                "tier": "T1",
                "mutation_kind": "targeted_warehouse_publication",
                "closure_kind": "sku_date",
                "rollback": "exact projection/ready before images",
            },
            "second_run_criterion": {
                "tier": "T0",
                "changed_rows": 0,
                "changed_cells": 0,
                "mutations": 0,
                "recovery_bytes": 0,
            },
            "_apply_payload": {
                "projection_rows": projection_rows,
                "ready_updates": ready_updates,
                "revision_id": revision_id,
            },
        }


def apply_early_wb_recovery_plan(
    runtime: RegistryUploadDbBackedRuntime,
    plan: Mapping[str, Any],
    *,
    confirm_fingerprint: str,
    approval_reference: str,
    batch_a_fingerprint: str,
) -> dict[str, Any]:
    fingerprint = str(plan.get("fingerprint") or "")
    if not fingerprint or fingerprint != str(confirm_fingerprint or ""):
        raise WarehouseEarlyWbRecoveryError(
            "apply requires the exact current Batch B fingerprint"
        )
    if not str(approval_reference or "").strip():
        raise WarehouseEarlyWbRecoveryError(
            "exact bounded human-gate provenance is required"
        )
    prerequisite = _batch_a_prerequisite(runtime, batch_a_fingerprint)
    registry = WarehouseRecoveryRegistry(
        runtime_dir=runtime.runtime_dir,
        db_path=runtime.db_path,
    )
    operation_id = recovery_operation_id(
        "targeted_warehouse_publication", fingerprint
    )
    existing = registry.get_operation(operation_id)
    if existing and str(existing.get("lifecycle")) == RecoveryState.RETAINED.value:
        readback = readback_early_wb_recovery(runtime, plan=plan)
        return {
            **_public_plan(plan),
            "mode": "apply",
            "applied": False,
            "idempotent": True,
            "prerequisite": prerequisite,
            "readback": readback,
            "second_run": {
                "tier": "T0",
                "changed_rows": 0,
                "changed_cells": 0,
                "mutations": 0,
                "recovery_bytes": 0,
            },
        }
    if not bool(plan.get("would_change")):
        return {
            **_public_plan(plan),
            "mode": "apply",
            "applied": False,
            "idempotent": True,
            "prerequisite": prerequisite,
        }
    payload = dict(plan["_apply_payload"])
    before_images = _before_images(runtime.db_path, plan=plan, payload=payload)
    recovery = registry.prepare_t1(
        mutation_kind="targeted_warehouse_publication",
        closure_kind="sku_date",
        plan_fingerprint=fingerprint,
        scope={
            **dict(plan["scope"]),
            "approval_reference": str(approval_reference),
            "batch_a_fingerprint": str(batch_a_fingerprint),
        },
        before_images=before_images,
        source_digest=str(plan["source_digest"]),
        non_target_digest=str(plan["non_target_digest"]),
        read_bytes=sum(
            len(_canonical_json(item).encode("utf-8"))
            for item in before_images
        ),
    )
    if str(recovery.get("lifecycle")) == RecoveryState.VERIFIED.value:
        recovery = registry.begin_mutation(
            str(recovery["operation_id"]),
            expected_source_digest=str(plan["source_digest"]),
        )
    applied_at = _now()
    try:
        with warehouse_functional_write_lock(
            runtime.runtime_dir, timeout_seconds=300
        ):
            fresh = build_early_wb_recovery_plan(runtime)
            if str(fresh["fingerprint"]) != fingerprint:
                raise WarehouseEarlyWbRecoveryError(
                    "Batch B source or target fingerprint changed after dry-run"
                )
            with _connect(runtime.db_path, read_only=False) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    projection = _persist_projection_revision(
                        conn,
                        revision_id=str(payload["revision_id"]),
                        stable_source_id=(
                            "historical_wb_daily:2026-07-01:2026-07-18"
                        ),
                        source_revision=str(plan["source_digest"]),
                        business_effective_date=DATE_FROM,
                        published_at=applied_at,
                        plan_fingerprint=fingerprint,
                        base_version_id="",
                        published_version_id="",
                        affected_nm_ids=list(plan["scope"]["source_nm_ids"]),
                        source_kind="historical_wb_daily_partial",
                        rows=list(payload["projection_rows"]),
                        diagnostics={
                            "affected_dates": list(DATES),
                            "contract_name": CONTRACT_NAME,
                            "six_stage_history": "unavailable",
                            "approval_reference": str(approval_reference),
                            "batch_a_fingerprint": str(batch_a_fingerprint),
                        },
                    )
                    _apply_ready_updates(
                        conn, updates=list(payload["ready_updates"])
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
    except Exception as exc:
        registry.fail_recoverable(
            str(recovery["operation_id"]),
            error=str(exc),
            next_action="rollback_or_replan_early_wb_recovery",
        )
        raise
    readback = readback_early_wb_recovery(runtime, plan=plan)
    recovery = registry.retain(
        str(recovery["operation_id"]),
        after_digest=str(readback["after_digest"]),
        non_target_digest=str(plan["non_target_digest"]),
    )
    return {
        **_public_plan(plan),
        "mode": "apply",
        "applied": True,
        "idempotent": False,
        "projection": projection,
        "readback": readback,
        "prerequisite": prerequisite,
        "recovery_policy": recovery,
    }


def rollback_early_wb_recovery(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    fingerprint: str,
    reason: str,
) -> dict[str, Any]:
    registry = WarehouseRecoveryRegistry(
        runtime_dir=runtime.runtime_dir,
        db_path=runtime.db_path,
    )
    return registry.rollback_t1(
        recovery_operation_id(
            "targeted_warehouse_publication", str(fingerprint)
        ),
        reason=str(reason),
    )


def readback_early_wb_recovery(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    payload = dict(plan["_apply_payload"])
    with _connect(runtime.db_path, read_only=True) as conn:
        revision = conn.execute(
            f"SELECT * FROM {REVISION_TABLE} WHERE plan_fingerprint=?",
            (str(plan["fingerprint"]),),
        ).fetchone()
        if revision is None or str(revision["status"]) != "active":
            raise WarehouseEarlyWbRecoveryError(
                "Batch B projection revision is not active"
            )
        for update in payload["ready_updates"]:
            row = conn.execute(
                "SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots "
                "WHERE bundle_version=? AND as_of_date=?",
                (update["bundle_version"], update["as_of_date"]),
            ).fetchone()
            if row is None or "sha256:" + _sha(str(row["plan_json"])) != str(
                update["after_plan_sha256"]
            ):
                raise WarehouseEarlyWbRecoveryError(
                    "Batch B ready snapshot readback mismatch"
                )
        row_count = _verify_projection_readback(
            conn,
            revision_id=str(payload["revision_id"]),
            expected_rows=payload["projection_rows"],
        )
        non_target_digest = _non_target_digest(conn)
        if non_target_digest != str(plan["non_target_digest"]):
            raise WarehouseEarlyWbRecoveryError(
                "Batch B non-target digest changed during apply"
            )
        after_digest = _fingerprint(
            {
                "revision": dict(revision),
                "projection_row_count": row_count,
                "ready": [
                    {
                        "bundle_version": item["bundle_version"],
                        "as_of_date": item["as_of_date"],
                        "sha256": item["after_plan_sha256"],
                    }
                    for item in payload["ready_updates"]
                ],
            }
        )
    return {
        "status": "verified",
        "projection_current_row_count": row_count,
        "ready_snapshot_update_count": len(payload["ready_updates"]),
        "non_target_digest": non_target_digest,
        "non_target_unchanged": True,
        "after_digest": after_digest,
    }


def _validate_daily_rows(rows: Sequence[Mapping[str, Any]]) -> None:
    counts = {
        day: sum(str(row["as_of_date"]) == day for row in rows)
        for day in DATES
    }
    if len(rows) != EXPECTED_PERSISTED_ROW_COUNT or counts != EXPECTED_ROWS_BY_DATE:
        raise WarehouseEarlyWbRecoveryError(
            "early-July persisted WB row closure drifted: "
            + _canonical_json({"row_count": len(rows), "counts": counts})
        )
    seen: set[tuple[str, int]] = set()
    for row in rows:
        key = (str(row["as_of_date"]), int(row["nm_id"]))
        if key in seen:
            raise WarehouseEarlyWbRecoveryError(
                f"duplicate persisted WB daily row: {key}"
            )
        seen.add(key)
        quantity = _decimal(row["quantity"])
        wac = _decimal(row["wac_rub"])
        capital = _decimal(row["capital_rub"])
        if quantity < ZERO or wac <= ZERO or capital < ZERO:
            raise WarehouseEarlyWbRecoveryError(
                f"invalid persisted WB daily economics: {key}"
            )
        if abs(capital - quantity * wac) > TOLERANCE:
            raise WarehouseEarlyWbRecoveryError(
                f"persisted WB quantity/WAC/capital mismatch: {key}"
            )
        if key[0] in {"2026-07-17", "2026-07-18"}:
            provenance = json.loads(str(row["provenance_json"] or "{}"))
            evidence = _find_quantity_evidence(provenance, day=key[0])
            if evidence is None:
                raise WarehouseEarlyWbRecoveryError(
                    f"17–18 July row lacks exact stock_total column evidence: {key}"
                )


def _find_quantity_evidence(value: Any, *, day: str) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        if (
            str(value.get("source") or "")
            == "persisted_ready_snapshot_exact_column"
            and str(value.get("column_date") or "") == day
            and str(value.get("metric_key") or "") == "stock_total"
        ):
            return value
        for nested in value.values():
            found = _find_quantity_evidence(nested, day=day)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_quantity_evidence(nested, day=day)
            if found is not None:
                return found
    return None


def _projection_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    source_digest: str,
) -> list[dict[str, Any]]:
    by_date: dict[str, list[Mapping[str, Any]]] = {day: [] for day in DATES}
    for row in rows:
        by_date[str(row["as_of_date"])].append(row)
    result: list[dict[str, Any]] = []
    for day in DATES:
        day_rows = by_date[day]
        for row in day_rows:
            item = _partial_projection_item(
                day=day,
                nm_id=int(row["nm_id"]),
                quantity=_decimal(row["quantity"]),
                wac=_decimal(row["wac_rub"]),
                capital=_decimal(row["capital_rub"]),
                source_digest=source_digest,
                source_fingerprint=str(row["fingerprint"]),
            )
            result.append(item)
        total_quantity = sum(
            (_decimal(row["quantity"]) for row in day_rows), ZERO
        )
        total_capital = sum(
            (_decimal(row["capital_rub"]) for row in day_rows), ZERO
        )
        result.append(
            _partial_projection_item(
                day=day,
                nm_id=0,
                quantity=total_quantity,
                wac=(
                    total_capital / total_quantity
                    if total_quantity > ZERO
                    else ZERO
                ),
                capital=total_capital,
                source_digest=source_digest,
                source_fingerprint=_fingerprint(
                    [str(row["fingerprint"]) for row in day_rows]
                ),
            )
        )
    return result


def _partial_projection_item(
    *,
    day: str,
    nm_id: int,
    quantity: Decimal,
    wac: Decimal,
    capital: Decimal,
    source_digest: str,
    source_fingerprint: str,
) -> dict[str, Any]:
    total = nm_id == 0
    metrics = {
        key: None
        for key in (
            OWN_PRODUCT_CAPITAL_TOTAL_METRIC_KEYS
            if total
            else OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS
        )
    }
    presentation = {
        key: {
            "state": "unavailable",
            "tone": "neutral",
            "reason": UNAVAILABLE_REASON,
            "source": "WebCore exact partial WB history",
        }
        for key in metrics
    }
    if total:
        keys = (
            own_stage_total_metric_key("WB", "qty"),
            own_stage_total_metric_key("WB", "unit_cost_rub"),
            own_stage_total_metric_key("WB", "capital_rub"),
        )
    else:
        keys = (
            own_stage_metric_key("WB", "qty"),
            own_stage_metric_key("WB", "unit_cost_rub"),
            own_stage_metric_key("WB", "capital_rub"),
        )
    for key, value in zip(keys, (quantity, wac, capital), strict=True):
        metrics[key] = float(value)
        presentation.pop(key, None)
    provenance = {
        "contract_name": CONTRACT_NAME,
        "source_digest": source_digest,
        "source_fingerprint": source_fingerprint,
        "as_of_date": day,
        "scope": "wb_only",
        "other_stages": "unavailable",
        "owned_projection": True,
    }
    item = {
        "as_of_date": day,
        "nm_id": nm_id,
        "metrics": metrics,
        "presentation": presentation,
        "provenance": provenance,
    }
    item["row_fingerprint"] = _fingerprint(item)
    return item


def _ready_updates(
    conn: sqlite3.Connection,
    *,
    snapshots: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    configured_nm_ids: Sequence[int],
    source_digest: str,
) -> list[dict[str, Any]]:
    by_date = {
        day: {
            int(row["nm_id"]): dict(row)
            for row in rows
            if str(row["as_of_date"]) == day
        }
        for day in DATES
    }
    costs = {
        day: {
            nm_id: {
                "stock_qty": row["quantity"],
                "our_wb_unit_cost_rub": row["wac_rub"],
                "source_status": row["quality"],
                "inputs_hash": row["fingerprint"],
            }
            for nm_id, row in day_rows.items()
        }
        for day, day_rows in by_date.items()
    }
    partial_metrics = {
        day: {
            nm_id: dict(
                _partial_projection_item(
                    day=day,
                    nm_id=nm_id,
                    quantity=_decimal(row["quantity"]),
                    wac=_decimal(row["wac_rub"]),
                    capital=_decimal(row["capital_rub"]),
                    source_digest=source_digest,
                    source_fingerprint=str(row["fingerprint"]),
                )["metrics"]
            )
            for nm_id, row in day_rows.items()
        }
        for day, day_rows in by_date.items()
    }
    parameters = {}
    for day in DATES:
        row = conn.execute(
            """
            SELECT * FROM sheet_vitrina_v1_calculation_parameter_versions
            WHERE block_key=? AND effective_date<=?
            ORDER BY effective_date DESC,revision DESC,created_at DESC LIMIT 1
            """,
            (PROXY_BLOCK_KEY, day),
        ).fetchone()
        parameters[day] = (
            _parameters_from_row(row)
            if row is not None
            else DEFAULT_PROXY_PARAMETERS
        )
    version_ids = {
        day: "wb_daily:" + source_digest.removeprefix("sha256:")[:16] + ":" + day
        for day in DATES
    }
    updates: list[dict[str, Any]] = []
    for snapshot in snapshots:
        transformed = _transform_snapshot(
            snapshot,
            costs=costs,
            warehouse_metrics=partial_metrics,
            warehouse_exact_dates=set(DATES),
            warehouse_covered_nm_ids={
                day: set(by_date[day]) for day in DATES
            },
            warehouse_version_ids=version_ids,
            parameters=parameters,
            source_fingerprint=source_digest,
            cutover_business_date=DATE_FROM,
            affected_nm_ids=sorted(set(configured_nm_ids)),
            earliest_business_date=DATE_FROM,
            latest_business_date=DATE_TO,
        )
        plan = json.loads(str(transformed["after_plan_json"]))
        sheet = _data_sheet(plan)
        plan_rows = sheet.get("rows")
        if not isinstance(plan_rows, list):
            raise WarehouseEarlyWbRecoveryError(
                "ready snapshot DATA_VITRINA rows are missing"
            )
        dates = _date_columns(plan)
        by_id = {
            str(row[1]).strip(): row
            for row in plan_rows
            if isinstance(row, list)
            and len(row) >= 2
            and isinstance(row[1], str)
            and "|" in str(row[1])
        }
        metadata = plan.setdefault("metadata", {})
        changed_cells = int(transformed["changed_cells"])
        presentation_changes = int(transformed["presentation_changes"])
        touched: list[str] = []
        for index, day in enumerate(dates):
            if day not in by_date:
                continue
            touched.append(day)
            for nm_id in configured_nm_ids:
                scope = f"SKU:{nm_id}"
                exact = by_date[day].get(int(nm_id))
                if exact is None:
                    continue
                item = _partial_projection_item(
                    day=day,
                    nm_id=int(nm_id),
                    quantity=_decimal(exact["quantity"]),
                    wac=_decimal(exact["wac_rub"]),
                    capital=_decimal(exact["capital_rub"]),
                    source_digest=source_digest,
                    source_fingerprint=str(exact["fingerprint"]),
                )
                for metric_key, value in item["metrics"].items():
                    row = by_id.get(f"{scope}|{metric_key}")
                    if row is None:
                        continue
                    changed_cells += _set_ready_cell(
                        row, index=index, value=value
                    )
                    presentation_changes += _set_ready_presentation(
                        metadata,
                        row_id=f"{scope}|{metric_key}",
                        day=day,
                        value=item["presentation"].get(metric_key),
                    )
            total = _partial_projection_item(
                day=day,
                nm_id=0,
                quantity=sum(
                    (_decimal(row["quantity"]) for row in by_date[day].values()),
                    ZERO,
                ),
                wac=(
                    sum(
                        (_decimal(row["capital_rub"]) for row in by_date[day].values()),
                        ZERO,
                    )
                    / sum(
                        (_decimal(row["quantity"]) for row in by_date[day].values()),
                        ZERO,
                    )
                ),
                capital=sum(
                    (_decimal(row["capital_rub"]) for row in by_date[day].values()),
                    ZERO,
                ),
                source_digest=source_digest,
                source_fingerprint=_fingerprint(
                    [row["fingerprint"] for row in by_date[day].values()]
                ),
            )
            for metric_key, value in total["metrics"].items():
                row = by_id.get(f"TOTAL|{metric_key}")
                if row is None:
                    continue
                changed_cells += _set_ready_cell(row, index=index, value=value)
                presentation_changes += _set_ready_presentation(
                    metadata,
                    row_id=f"TOTAL|{metric_key}",
                    day=day,
                    value=total["presentation"].get(metric_key),
                )
            coverage = metadata.setdefault("warehouse_history_coverage", {})
            coverage[day] = {
                "status": "partial",
                "reason_ru": UNAVAILABLE_REASON,
                "covered_nm_id_count": len(by_date[day]),
                "uncovered_scope_nm_ids": [],
                "functional_version_id": version_ids[day],
                "available_stages": ["wb"],
            }
        if not touched:
            continue
        marker = {
            "contract_name": CONTRACT_NAME,
            "source_digest": source_digest,
            "date_from": min(touched),
            "date_to": max(touched),
            "available_stages": ["wb"],
            "unavailable_stage_cell_count": EXPECTED_UNAVAILABLE_STAGE_CELL_COUNT,
        }
        metadata["warehouse_early_wb_recovery"] = marker
        after = _canonical_json(plan)
        before = str(snapshot["plan_json"])
        if "sha256:" + _sha(after) == "sha256:" + _sha(before):
            continue
        updates.append(
            {
                "bundle_version": str(snapshot["bundle_version"]),
                "as_of_date": str(snapshot["as_of_date"]),
                "before_plan_sha256": "sha256:" + _sha(before),
                "after_plan_sha256": "sha256:" + _sha(after),
                "after_plan_json": after,
                "changed_cells": changed_cells,
                "presentation_changes": presentation_changes,
                "coverage_changes": 1,
            }
        )
    return updates


def _before_images(
    db_path: Any,
    *,
    plan: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    with _connect(db_path, read_only=True) as conn:
        revision = conn.execute(
            f"SELECT * FROM {REVISION_TABLE} WHERE revision_id=?",
            (str(payload["revision_id"]),),
        ).fetchone()
        images.append(
            _before_image(
                REVISION_TABLE,
                {"revision_id": str(payload["revision_id"])},
                dict(revision) if revision is not None else None,
            )
        )
        for row in payload["projection_rows"]:
            projection_key = {
                "revision_id": str(payload["revision_id"]),
                "as_of_date": str(row["as_of_date"]),
                "nm_id": int(row["nm_id"]),
            }
            projection = conn.execute(
                f"SELECT * FROM {ROW_TABLE} "
                "WHERE revision_id=? AND as_of_date=? AND nm_id=?",
                tuple(projection_key.values()),
            ).fetchone()
            images.append(
                _before_image(
                    ROW_TABLE,
                    projection_key,
                    dict(projection) if projection is not None else None,
                )
            )
        state = conn.execute(
            f"SELECT * FROM {STATE_TABLE} WHERE slot=1"
        ).fetchone()
        images.append(
            _before_image(
                STATE_TABLE,
                {"slot": 1},
                dict(state) if state is not None else None,
            )
        )
        for projection_row in payload["projection_rows"]:
            current = conn.execute(
                f"SELECT * FROM {CURRENT_ROW_TABLE} "
                "WHERE as_of_date=? AND nm_id=?",
                (
                    str(projection_row["as_of_date"]),
                    int(projection_row["nm_id"]),
                ),
            ).fetchone()
            images.append(
                _before_image(
                    CURRENT_ROW_TABLE,
                    {
                        "as_of_date": str(projection_row["as_of_date"]),
                        "nm_id": int(projection_row["nm_id"]),
                    },
                    dict(current) if current is not None else None,
                )
            )
        for update in payload["ready_updates"]:
            row = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_ready_snapshots "
                "WHERE bundle_version=? AND as_of_date=?",
                (update["bundle_version"], update["as_of_date"]),
            ).fetchone()
            images.append(
                _before_image(
                    "sheet_vitrina_v1_ready_snapshots",
                    {
                        "bundle_version": update["bundle_version"],
                        "as_of_date": update["as_of_date"],
                    },
                    dict(row) if row is not None else None,
                )
            )
    return images


def _batch_a_prerequisite(
    runtime: RegistryUploadDbBackedRuntime,
    fingerprint: str,
) -> dict[str, Any]:
    normalized = str(fingerprint or "").strip()
    if not normalized:
        raise WarehouseEarlyWbRecoveryError(
            "Batch B apply requires reconciled Batch A fingerprint"
        )
    registry = WarehouseRecoveryRegistry(
        runtime_dir=runtime.runtime_dir,
        db_path=runtime.db_path,
    )
    operation = registry.get_operation(
        recovery_operation_id("targeted_warehouse_publication", normalized)
    )
    if operation is None or str(operation.get("lifecycle")) != RecoveryState.RETAINED.value:
        raise WarehouseEarlyWbRecoveryError(
            "Batch A is not retained/reconciled for the supplied fingerprint"
        )
    return {
        "batch_a_fingerprint": normalized,
        "operation_id": str(operation["operation_id"]),
        "lifecycle": str(operation["lifecycle"]),
    }


def _configured_nm_ids(conn: sqlite3.Connection) -> set[int]:
    return _active_configured_nm_ids(conn)


def _non_target_digest(conn: sqlite3.Connection) -> str:
    material = {
        "daily_rows": [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_wb_daily_cost "
                "ORDER BY as_of_date,nm_id"
            ).fetchall()
        ],
        "projection_19_plus": [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM {CURRENT_ROW_TABLE} "
                "WHERE as_of_date>? ORDER BY as_of_date,nm_id",
                (DATE_TO,),
            ).fetchall()
        ],
    }
    return _fingerprint(material)


def _decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return ZERO
    return Decimal(str(value).replace(",", "."))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _public_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(value)
        for key, value in plan.items()
        if not str(key).startswith("_")
    }


def public_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    return _public_plan(plan)
