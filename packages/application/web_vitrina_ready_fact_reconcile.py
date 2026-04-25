"""Bounded ready-snapshot -> accepted temporal fact reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Mapping

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_live_plan import (
    CLOSURE_STATE_SUCCESS,
    TEMPORAL_ROLE_ACCEPTED_CLOSED,
    TEMPORAL_SLOT_YESTERDAY_CLOSED,
)
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope, SheetVitrinaWriteTarget


READY_FACT_RECONCILE_SOURCE_KIND = "web_vitrina_ready_snapshot_to_temporal_accepted_fact_reconcile_v1"
READY_FACT_RECONCILE_NOTE = (
    "bounded one-off reconciliation from server-side web-vitrina ready snapshots; "
    "existing accepted snapshots are not overwritten"
)
DEFAULT_RECONCILE_DATE_FROM = "2026-03-01"
DEFAULT_RECONCILE_DATE_TO = "2026-04-24"
DEFAULT_METRICS = ("fin_buyout_rub", "ads_sum")
FLOAT_TOLERANCE = 0.05


@dataclass(frozen=True)
class ReadyFactMetricSpec:
    metric_key: str
    source_key: str
    payload_field: str


METRIC_SPECS: dict[str, ReadyFactMetricSpec] = {
    "fin_buyout_rub": ReadyFactMetricSpec(
        metric_key="fin_buyout_rub",
        source_key="fin_report_daily",
        payload_field="fin_buyout_rub",
    ),
    "ads_sum": ReadyFactMetricSpec(
        metric_key="ads_sum",
        source_key="ads_compact",
        payload_field="ads_sum",
    ),
}


def dry_run_ready_fact_reconcile(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    date_from: str = DEFAULT_RECONCILE_DATE_FROM,
    date_to: str = DEFAULT_RECONCILE_DATE_TO,
    metric_keys: tuple[str, ...] = DEFAULT_METRICS,
) -> dict[str, Any]:
    return _build_reconcile_result(
        runtime=runtime,
        date_from=date_from,
        date_to=date_to,
        metric_keys=metric_keys,
        apply=False,
        captured_at=None,
    )


def apply_ready_fact_reconcile(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    date_from: str = DEFAULT_RECONCILE_DATE_FROM,
    date_to: str = DEFAULT_RECONCILE_DATE_TO,
    metric_keys: tuple[str, ...] = DEFAULT_METRICS,
    captured_at: str | None = None,
) -> dict[str, Any]:
    effective_captured_at = captured_at or _utc_now()
    return _build_reconcile_result(
        runtime=runtime,
        date_from=date_from,
        date_to=date_to,
        metric_keys=metric_keys,
        apply=True,
        captured_at=effective_captured_at,
    )


def _build_reconcile_result(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    date_from: str,
    date_to: str,
    metric_keys: tuple[str, ...],
    apply: bool,
    captured_at: str | None,
) -> dict[str, Any]:
    _validate_window(date_from=date_from, date_to=date_to)
    specs = [_resolve_metric_spec(metric_key) for metric_key in metric_keys]
    current_state = runtime.load_current_state()
    active_nm_ids = sorted({int(item.nm_id) for item in current_state.config_v2 if item.enabled})
    if not active_nm_ids:
        raise ValueError("current active config_v2 is empty; cannot reconcile facts")

    actions: list[dict[str, Any]] = []
    inserts: list[tuple[ReadyFactMetricSpec, str, dict[str, Any]]] = []
    for snapshot_date in _iter_iso_dates(date_from=date_from, date_to=date_to):
        try:
            ready_snapshot = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=snapshot_date)
        except ValueError as exc:
            for spec in specs:
                actions.append(
                    _action(
                        date=snapshot_date,
                        spec=spec,
                        action="skip_no_ready_snapshot",
                        reason=str(exc),
                    )
                )
            continue

        data_sheet = _require_data_sheet(ready_snapshot)
        column_index = _resolve_value_column_index(ready_snapshot, snapshot_date=snapshot_date)
        row_by_id = _row_by_id(data_sheet)
        for spec in specs:
            ready_items = _extract_metric_items(
                row_by_id=row_by_id,
                column_index=column_index,
                metric_key=spec.metric_key,
                payload_field=spec.payload_field,
                active_nm_ids=active_nm_ids,
            )
            ready_total = sum(float(item[spec.payload_field]) for item in ready_items)
            ready_missing_nm_ids = [
                nm_id
                for nm_id in active_nm_ids
                if f"SKU:{nm_id}|{spec.metric_key}" not in row_by_id
                or _is_blank(_row_value(row_by_id[f"SKU:{nm_id}|{spec.metric_key}"], column_index))
            ]
            if not ready_items:
                actions.append(
                    _action(
                        date=snapshot_date,
                        spec=spec,
                        action="skip_no_ready_metric_values",
                        ready_total=0.0,
                        ready_item_count=0,
                        ready_missing_nm_ids=ready_missing_nm_ids,
                        source_snapshot_id=ready_snapshot.snapshot_id,
                        source_plan_version=ready_snapshot.plan_version,
                    )
                )
                continue

            existing_payload, existing_captured_at = runtime.load_temporal_source_slot_snapshot(
                source_key=spec.source_key,
                snapshot_date=snapshot_date,
                snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
            )
            existing_total = (
                _sum_existing_payload(existing_payload, field_name=spec.payload_field, active_nm_ids=set(active_nm_ids))
                if existing_payload is not None
                else None
            )
            payload = _build_reconciled_payload(
                spec=spec,
                snapshot_date=snapshot_date,
                ready_items=ready_items,
                ready_missing_nm_ids=ready_missing_nm_ids,
                ready_snapshot=ready_snapshot,
                date_from=date_from,
                date_to=date_to,
                captured_at=captured_at,
            )
            checksum = str(payload["reconcile_metadata"]["checksum"])
            if existing_payload is None:
                actions.append(
                    _action(
                        date=snapshot_date,
                        spec=spec,
                        action="insert_missing_accepted_snapshot",
                        ready_total=ready_total,
                        ready_item_count=len(ready_items),
                        ready_missing_nm_ids=ready_missing_nm_ids,
                        source_snapshot_id=ready_snapshot.snapshot_id,
                        source_plan_version=ready_snapshot.plan_version,
                        checksum=checksum,
                    )
                )
                inserts.append((spec, snapshot_date, payload))
                continue

            delta = None if existing_total is None else existing_total - ready_total
            if existing_total is not None and abs(delta or 0.0) <= FLOAT_TOLERANCE:
                actions.append(
                    _action(
                        date=snapshot_date,
                        spec=spec,
                        action="skip_existing_matches_ready_snapshot",
                        ready_total=ready_total,
                        existing_total=existing_total,
                        existing_captured_at=existing_captured_at,
                        delta=delta,
                        ready_item_count=len(ready_items),
                        ready_missing_nm_ids=ready_missing_nm_ids,
                        source_snapshot_id=ready_snapshot.snapshot_id,
                        source_plan_version=ready_snapshot.plan_version,
                    )
                )
            else:
                actions.append(
                    _action(
                        date=snapshot_date,
                        spec=spec,
                        action="block_existing_diff",
                        ready_total=ready_total,
                        existing_total=existing_total,
                        existing_captured_at=existing_captured_at,
                        delta=delta,
                        ready_item_count=len(ready_items),
                        ready_missing_nm_ids=ready_missing_nm_ids,
                        source_snapshot_id=ready_snapshot.snapshot_id,
                        source_plan_version=ready_snapshot.plan_version,
                    )
                )

    blocking_diffs = [item for item in actions if item["action"] == "block_existing_diff"]
    if apply and blocking_diffs:
        raise ValueError(f"accepted snapshot diffs found; refusing to overwrite: {len(blocking_diffs)}")
    applied_count = 0
    if apply:
        assert captured_at is not None
        for spec, snapshot_date, payload in inserts:
            runtime.save_temporal_source_slot_snapshot(
                source_key=spec.source_key,
                snapshot_date=snapshot_date,
                snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
                captured_at=captured_at,
                payload=payload,
            )
            runtime.save_temporal_source_closure_state(
                source_key=spec.source_key,
                target_date=snapshot_date,
                slot_kind=TEMPORAL_SLOT_YESTERDAY_CLOSED,
                state=CLOSURE_STATE_SUCCESS,
                attempt_count=0,
                next_retry_at=None,
                last_reason=READY_FACT_RECONCILE_SOURCE_KIND,
                last_attempt_at=captured_at,
                last_success_at=captured_at,
                accepted_at=captured_at,
            )
            applied_count += 1

    action_counts: dict[str, int] = {}
    totals_by_action: dict[str, dict[str, float]] = {}
    for item in actions:
        action = str(item["action"])
        action_counts[action] = action_counts.get(action, 0) + 1
        metric_key = str(item["metric_key"])
        totals_by_action.setdefault(action, {})
        totals_by_action[action][metric_key] = totals_by_action[action].get(metric_key, 0.0) + float(item.get("ready_total") or 0.0)

    return {
        "status": "applied" if apply else "dry_run",
        "source_kind": READY_FACT_RECONCILE_SOURCE_KIND,
        "requested_window": {"date_from": date_from, "date_to": date_to},
        "metric_keys": [spec.metric_key for spec in specs],
        "target_snapshot_role": TEMPORAL_ROLE_ACCEPTED_CLOSED,
        "active_sku_count": len(active_nm_ids),
        "captured_at": captured_at,
        "action_counts": action_counts,
        "totals_by_action": totals_by_action,
        "would_insert_count": len(inserts),
        "applied_insert_count": applied_count,
        "blocking_diff_count": len(blocking_diffs),
        "no_double_count_note": READY_FACT_RECONCILE_NOTE,
        "actions": actions,
    }


def _build_reconciled_payload(
    *,
    spec: ReadyFactMetricSpec,
    snapshot_date: str,
    ready_items: list[dict[str, Any]],
    ready_missing_nm_ids: list[int],
    ready_snapshot: SheetVitrinaV1Envelope,
    date_from: str,
    date_to: str,
    captured_at: str | None,
) -> dict[str, Any]:
    checksum_payload = {
        "source_kind": READY_FACT_RECONCILE_SOURCE_KIND,
        "source_snapshot_id": ready_snapshot.snapshot_id,
        "source_plan_version": ready_snapshot.plan_version,
        "source_as_of_date": ready_snapshot.as_of_date,
        "snapshot_date": snapshot_date,
        "metric_key": spec.metric_key,
        "source_key": spec.source_key,
        "items": ready_items,
    }
    checksum = hashlib.sha256(
        json.dumps(checksum_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "kind": "success",
        "snapshot_date": snapshot_date,
        "count": len(ready_items),
        "items": ready_items,
        "reconcile_metadata": {
            "source_kind": READY_FACT_RECONCILE_SOURCE_KIND,
            "source_period": {"date_from": date_from, "date_to": date_to},
            "source_read_model": "persisted_ready_snapshot",
            "source_snapshot_id": ready_snapshot.snapshot_id,
            "source_plan_version": ready_snapshot.plan_version,
            "source_as_of_date": ready_snapshot.as_of_date,
            "imported_metrics": [spec.metric_key],
            "imported_at": captured_at,
            "checksum": checksum,
            "ready_missing_nm_ids": ready_missing_nm_ids,
            "note": READY_FACT_RECONCILE_NOTE,
        },
    }


def _extract_metric_items(
    *,
    row_by_id: Mapping[str, list[Any]],
    column_index: int,
    metric_key: str,
    payload_field: str,
    active_nm_ids: list[int],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for nm_id in active_nm_ids:
        row = row_by_id.get(f"SKU:{nm_id}|{metric_key}")
        if row is None:
            continue
        raw_value = _row_value(row, column_index)
        if _is_blank(raw_value):
            continue
        items.append({"nm_id": nm_id, payload_field: _require_number(raw_value)})
    return items


def _sum_existing_payload(payload: Any, *, field_name: str, active_nm_ids: set[int]) -> float:
    result = getattr(payload, "result", None) or payload
    items = getattr(result, "items", []) or []
    total = 0.0
    for item in items:
        nm_id = int(getattr(item, "nm_id"))
        if nm_id in active_nm_ids:
            total += float(getattr(item, field_name, 0.0) or 0.0)
    return total


def _action(
    *,
    date: str,
    spec: ReadyFactMetricSpec,
    action: str,
    reason: str | None = None,
    ready_total: float | None = None,
    existing_total: float | None = None,
    existing_captured_at: str | None = None,
    delta: float | None = None,
    ready_item_count: int | None = None,
    ready_missing_nm_ids: list[int] | None = None,
    source_snapshot_id: str | None = None,
    source_plan_version: str | None = None,
    checksum: str | None = None,
) -> dict[str, Any]:
    return {
        "date": date,
        "source_key": spec.source_key,
        "metric_key": spec.metric_key,
        "payload_field": spec.payload_field,
        "action": action,
        "reason": reason,
        "ready_total": ready_total,
        "existing_total": existing_total,
        "existing_captured_at": existing_captured_at,
        "delta": delta,
        "ready_item_count": ready_item_count,
        "ready_missing_nm_ids": ready_missing_nm_ids or [],
        "source_snapshot_id": source_snapshot_id,
        "source_plan_version": source_plan_version,
        "checksum": checksum,
    }


def _resolve_metric_spec(metric_key: str) -> ReadyFactMetricSpec:
    normalized = str(metric_key or "").strip()
    if normalized not in METRIC_SPECS:
        raise ValueError(f"metric_key must be one of: {', '.join(sorted(METRIC_SPECS))}")
    return METRIC_SPECS[normalized]


def _require_data_sheet(snapshot: SheetVitrinaV1Envelope) -> SheetVitrinaWriteTarget:
    data_sheet = next((item for item in snapshot.sheets if item.sheet_name == "DATA_VITRINA"), None)
    if data_sheet is None:
        raise ValueError(f"ready snapshot {snapshot.as_of_date} does not contain DATA_VITRINA")
    return data_sheet


def _resolve_value_column_index(snapshot: SheetVitrinaV1Envelope, *, snapshot_date: str) -> int:
    if snapshot_date not in snapshot.date_columns:
        raise ValueError(
            f"ready snapshot {snapshot.as_of_date} does not contain date column {snapshot_date}; "
            f"date_columns={snapshot.date_columns}"
        )
    return 2 + snapshot.date_columns.index(snapshot_date)


def _row_by_id(data_sheet: SheetVitrinaWriteTarget) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {}
    for row in data_sheet.rows:
        if len(row) < 2:
            continue
        row_id = str(row[1] or "").strip()
        if row_id:
            result[row_id] = list(row)
    return result


def _row_value(row: list[Any], column_index: int) -> Any:
    return row[column_index] if column_index < len(row) else None


def _is_blank(value: Any) -> bool:
    return value is None or value == ""


def _require_number(value: Any) -> float:
    numeric = float(value)
    if numeric < 0:
        raise ValueError(f"ready fact value must be non-negative, got {value!r}")
    return numeric


def _validate_window(*, date_from: str, date_to: str) -> None:
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if end < start:
        raise ValueError("date_to must be >= date_from")


def _iter_iso_dates(*, date_from: str, date_to: str):
    current = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    while current <= end:
        yield current.isoformat()
        current += timedelta(days=1)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
