"""Bounded derived Web Vitrina incident-metric rematerialization."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Mapping, Sequence

from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
    _sheet_vitrina_plan_digest,
)
from packages.application.sheet_vitrina_v1_incident_stocks import (
    INCIDENT_STOCK_FIELDS,
    INCIDENT_STOCK_METRIC_KEYS,
    incident_stock_metric_key,
    incident_stock_total_metric_key,
    incident_stock_value,
)
from packages.application.wb_incident_policy import (
    VITRINA_PROVISIONAL_QUALITY_MESSAGE_RU,
    build_vitrina_incident_stock_projection,
)
from packages.contracts.sheet_vitrina_v1 import (
    SheetVitrinaV1Envelope,
    SheetVitrinaWriteTarget,
)


CONTRACT_NAME = "vitrina_incident_rematerialization"
CONTRACT_VERSION = 1
DEFAULT_MAX_DATES = 14
DATA_SHEET_NAME = "DATA_VITRINA"


def _iso_date(value: str, *, field_name: str) -> str:
    normalized = str(value or "").strip()
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field_name} must be YYYY-MM-DD") from exc


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _generated_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _data_sheet(snapshot: SheetVitrinaV1Envelope) -> SheetVitrinaWriteTarget:
    sheet = next(
        (item for item in snapshot.sheets if item.sheet_name == DATA_SHEET_NAME),
        None,
    )
    if sheet is None:
        raise ValueError(
            f"ready snapshot {snapshot.as_of_date} does not contain {DATA_SHEET_NAME}"
        )
    return sheet


def _projection_presentation(
    *,
    projection: Mapping[str, Any],
    metric_key: str,
    value: float | None,
    fact: float | None,
    incident: float | None,
    effective: float | None,
    total: bool,
) -> dict[str, str] | None:
    policy = dict(projection.get("policy") or {})
    quality = dict(projection.get("quality") or {})
    provisional = str(quality.get("state") or "") == "provisional_received_rows"
    if value is None:
        return {
            "state": "unavailable",
            "tone": "neutral",
            "reason": (
                "Недостаточно фактически сохранённых строк для расчёта; "
                "нулевое значение не предполагается."
            ),
            "source": "WebCore incident projection",
            **(
                {
                    "quality_state": "provisional_received_rows",
                    "quality_label": str(
                        quality.get("label_ru")
                        or "Полнота WB не подтверждена"
                    ),
                    "quality_reason": str(
                        quality.get("message_ru")
                        or VITRINA_PROVISIONAL_QUALITY_MESSAGE_RU
                    ),
                }
                if provisional
                else {}
            ),
        }
    adjusted_metric = "incident" in metric_key or "effective" in metric_key
    adjusted = bool(adjusted_metric and incident is not None and incident > 0)
    if not provisional and not adjusted:
        return None
    names = [
        str(item.get("warehouse_name") or f"warehouseId {item.get('warehouse_id')}")
        for item in policy.get("warehouse_identities") or []
    ]
    arithmetic_reason = (
        f"Факт: {float(fact or 0):g} шт; на инцидентных складах: "
        f"{float(incident or 0):g} шт; operational остаток: "
        f"{float(effective or 0):g} шт. Склады: "
        f"{', '.join(names) or 'не указаны'}; начало: "
        f"{policy.get('effective_from') or 'не указано'}; "
        f"revision: {int(policy.get('revision') or 0)}"
    )
    return {
        "state": "incident_adjusted" if adjusted else "",
        "tone": "blue_violet" if adjusted else "neutral",
        "reason": arithmetic_reason if adjusted else VITRINA_PROVISIONAL_QUALITY_MESSAGE_RU,
        "source": "WebCore incident policy",
        **(
            {
                "quality_state": "provisional_received_rows",
                "quality_label": str(
                    quality.get("label_ru") or "Полнота WB не подтверждена"
                ),
                "quality_reason": str(
                    quality.get("message_ru")
                    or VITRINA_PROVISIONAL_QUALITY_MESSAGE_RU
                ),
            }
            if provisional
            else {}
        ),
        "scope": "TOTAL" if total else "SKU",
    }


def _incident_target_manifest(
    snapshot: SheetVitrinaV1Envelope,
    *,
    target_dates: Sequence[str],
) -> dict[str, Any]:
    target_set = set(target_dates)
    sheet = _data_sheet(snapshot)
    date_indices = {
        column_date: sheet.header.index(column_date)
        for column_date in target_dates
        if column_date in sheet.header
    }
    values: dict[str, dict[str, Any]] = {}
    for row in sheet.rows:
        row_id = str(row[1] or "") if len(row) > 1 else ""
        metric_key = row_id.split("|", 1)[1] if "|" in row_id else ""
        if metric_key not in set(INCIDENT_STOCK_METRIC_KEYS):
            continue
        values[row_id] = {
            column_date: row[index] if len(row) > index else None
            for column_date, index in date_indices.items()
        }
    metadata = dict(snapshot.metadata or {})
    presentation = metadata.get("server_cell_presentation")
    presentation_manifest: dict[str, dict[str, Any]] = {}
    if isinstance(presentation, Mapping):
        for row_id, by_date in presentation.items():
            metric_key = str(row_id).split("|", 1)[1] if "|" in str(row_id) else ""
            if metric_key not in set(INCIDENT_STOCK_METRIC_KEYS) or not isinstance(
                by_date, Mapping
            ):
                continue
            selected = {
                str(column_date): deepcopy(value)
                for column_date, value in by_date.items()
                if str(column_date) in target_set
            }
            if selected:
                presentation_manifest[str(row_id)] = selected
    quality_by_date = metadata.get("incident_projection_quality_by_date")
    quality_manifest = (
        {
            str(column_date): deepcopy(value)
            for column_date, value in quality_by_date.items()
            if str(column_date) in target_set
        }
        if isinstance(quality_by_date, Mapping)
        else {}
    )
    return {
        "values": values,
        "presentation": presentation_manifest,
        "quality_by_date": quality_manifest,
    }


def _non_target_digest(
    snapshot: SheetVitrinaV1Envelope,
    *,
    target_dates: Sequence[str],
) -> str:
    payload = asdict(snapshot)
    target_set = set(target_dates)
    for sheet in payload.get("sheets") or []:
        if sheet.get("sheet_name") != DATA_SHEET_NAME:
            continue
        header = list(sheet.get("header") or [])
        target_indices = {
            header.index(column_date)
            for column_date in target_dates
            if column_date in header
        }
        for row in sheet.get("rows") or []:
            row_id = str(row[1] or "") if len(row) > 1 else ""
            metric_key = row_id.split("|", 1)[1] if "|" in row_id else ""
            if metric_key not in set(INCIDENT_STOCK_METRIC_KEYS):
                continue
            for index in target_indices:
                if len(row) > index:
                    row[index] = "<incident-target>"
    metadata = payload.get("metadata") or {}
    presentation = metadata.get("server_cell_presentation")
    if isinstance(presentation, dict):
        for row_id in list(presentation):
            metric_key = str(row_id).split("|", 1)[1] if "|" in str(row_id) else ""
            by_date = presentation.get(row_id)
            if metric_key not in set(INCIDENT_STOCK_METRIC_KEYS) or not isinstance(
                by_date, dict
            ):
                continue
            for column_date in target_set:
                by_date.pop(column_date, None)
            if not by_date:
                presentation.pop(row_id, None)
        if not presentation:
            metadata.pop("server_cell_presentation", None)
    quality_by_date = metadata.get("incident_projection_quality_by_date")
    if isinstance(quality_by_date, dict):
        for column_date in target_set:
            quality_by_date.pop(column_date, None)
        if not quality_by_date:
            metadata.pop("incident_projection_quality_by_date", None)
    metadata.pop("incident_rematerialization", None)
    return _digest(payload)


def _rematerialize_snapshot(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    snapshot: SheetVitrinaV1Envelope,
    target_dates: Sequence[str],
    generated_at: str,
    seller_id: str | None,
) -> tuple[SheetVitrinaV1Envelope, dict[str, Any]]:
    sheet = _data_sheet(snapshot)
    rows = [list(row) for row in sheet.rows]
    row_by_id = {
        str(row[1] or ""): row
        for row in rows
        if len(row) > 1 and "|" in str(row[1] or "")
    }
    metadata = deepcopy(dict(snapshot.metadata or {}))
    presentation = metadata.setdefault("server_cell_presentation", {})
    if not isinstance(presentation, dict):
        raise ValueError("ready snapshot server_cell_presentation must be an object")
    quality_by_date = metadata.setdefault("incident_projection_quality_by_date", {})
    if not isinstance(quality_by_date, dict):
        raise ValueError(
            "ready snapshot incident_projection_quality_by_date must be an object"
        )
    projection_evidence: dict[str, Any] = {}

    for target_date in target_dates:
        if target_date not in sheet.header:
            continue
        payload, captured_at = runtime.load_temporal_source_snapshot(
            source_key="stocks",
            snapshot_date=target_date,
        )
        if payload is None or str(getattr(payload, "kind", "") or "") != "success":
            projection_evidence[target_date] = {
                "status": "skipped",
                "reason": "accepted stocks success snapshot is missing",
            }
            continue
        projection = build_vitrina_incident_stock_projection(
            runtime,
            items=list(getattr(payload, "items", []) or []),
            warehouse_rows=list(getattr(payload, "warehouse_rows", []) or []),
            snapshot_date=str(
                getattr(payload, "snapshot_date", "") or target_date
            ),
            fetched_at=str(getattr(payload, "fetched_at", "") or captured_at or ""),
            pagination_complete=bool(
                getattr(payload, "pagination_complete", False)
            ),
            raw_rows_digest=str(
                getattr(payload, "raw_rows_digest", "") or ""
            ),
            seller_id=seller_id,
            cache_enabled=False,
        )
        policy = dict(projection.get("policy") or {})
        if not policy.get("materialize_incident_metrics"):
            projection_evidence[target_date] = {
                "status": "skipped",
                "reason": "incident policy has not started for this date",
            }
            continue
        column_index = sheet.header.index(target_date)
        sku_values: dict[str, list[float]] = {}
        for row_id, row in row_by_id.items():
            scope_token, metric_key = row_id.split("|", 1)
            if metric_key not in set(INCIDENT_STOCK_METRIC_KEYS):
                continue
            if scope_token.startswith("SKU:"):
                try:
                    nm_id = int(scope_token.split(":", 1)[1])
                except ValueError:
                    continue
                projection_row = dict(projection.get("by_nm_id") or {}).get(str(nm_id))
                value = incident_stock_value(metric_key, projection_row)
                row[column_index] = "" if value is None else value
                if value is not None:
                    sku_values.setdefault(metric_key, []).append(float(value))
        for row_id, row in row_by_id.items():
            scope_token, metric_key = row_id.split("|", 1)
            if scope_token != "TOTAL" or metric_key not in set(
                INCIDENT_STOCK_METRIC_KEYS
            ):
                continue
            sku_metric_key = metric_key.removeprefix("total_")
            values = sku_values.get(sku_metric_key) or []
            row[column_index] = sum(values) if values else ""

        for row_id, row in row_by_id.items():
            scope_token, metric_key = row_id.split("|", 1)
            if metric_key not in set(INCIDENT_STOCK_METRIC_KEYS):
                continue
            if scope_token.startswith("SKU:"):
                nm_id = int(scope_token.split(":", 1)[1])
                projection_row = dict(projection.get("by_nm_id") or {}).get(str(nm_id))
                fact_key = metric_key.replace("_incident_", "_fact_").replace(
                    "_effective_", "_fact_"
                )
                incident_key = metric_key.replace("_fact_", "_incident_").replace(
                    "_effective_", "_incident_"
                )
                effective_key = metric_key.replace("_fact_", "_effective_").replace(
                    "_incident_", "_effective_"
                )
                fact = incident_stock_value(fact_key, projection_row)
                incident = incident_stock_value(incident_key, projection_row)
                effective = incident_stock_value(effective_key, projection_row)
                value = incident_stock_value(metric_key, projection_row)
                cell_presentation = _projection_presentation(
                    projection=projection,
                    metric_key=metric_key,
                    value=value,
                    fact=fact,
                    incident=incident,
                    effective=effective,
                    total=False,
                )
            else:
                sku_metric_key = metric_key.removeprefix("total_")
                value_list = sku_values.get(sku_metric_key) or []
                value = sum(value_list) if value_list else None
                fact_values = sku_values.get(
                    sku_metric_key.replace("_incident_", "_fact_").replace(
                        "_effective_", "_fact_"
                    )
                ) or []
                incident_values = sku_values.get(
                    sku_metric_key.replace("_fact_", "_incident_").replace(
                        "_effective_", "_incident_"
                    )
                ) or []
                effective_values = sku_values.get(
                    sku_metric_key.replace("_fact_", "_effective_").replace(
                        "_incident_", "_effective_"
                    )
                ) or []
                cell_presentation = _projection_presentation(
                    projection=projection,
                    metric_key=metric_key,
                    value=value,
                    fact=sum(fact_values) if fact_values else None,
                    incident=sum(incident_values) if incident_values else None,
                    effective=sum(effective_values) if effective_values else None,
                    total=True,
                )
            by_date = presentation.setdefault(row_id, {})
            if not isinstance(by_date, dict):
                by_date = {}
                presentation[row_id] = by_date
            if cell_presentation is None:
                by_date.pop(target_date, None)
                if not by_date:
                    presentation.pop(row_id, None)
            else:
                by_date[target_date] = cell_presentation

        quality_by_date[target_date] = deepcopy(dict(projection.get("quality") or {}))
        projection_evidence[target_date] = {
            "status": "ready",
            "policy_revision": int(projection.get("policy_revision") or 0),
            "quality_state": str(
                dict(projection.get("quality") or {}).get("state") or ""
            ),
            "accepted_payload_digest": str(
                dict(projection.get("quality") or {}).get(
                    "accepted_payload_digest"
                )
                or ""
            ),
            "accepted_item_count": int(
                dict(projection.get("quality") or {}).get("accepted_item_count")
                or 0
            ),
            "accepted_warehouse_row_count": int(
                dict(projection.get("quality") or {}).get(
                    "accepted_warehouse_row_count"
                )
                or 0
            ),
            "invariants": deepcopy(dict(projection.get("invariants") or {})),
        }

    if not any(
        str(item.get("status") or "") == "ready"
        for item in projection_evidence.values()
    ):
        return snapshot, projection_evidence
    metadata["incident_rematerialization"] = {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "generated_at": generated_at,
        "target_dates": list(target_dates),
        "projection_evidence": projection_evidence,
    }
    replacement_sheet = replace(
        sheet,
        rows=rows,
        row_count=len(rows),
        column_count=len(sheet.header),
    )
    sheets = [
        replacement_sheet if item.sheet_name == DATA_SHEET_NAME else item
        for item in snapshot.sheets
    ]
    return replace(snapshot, sheets=sheets, metadata=metadata), projection_evidence


def plan_vitrina_incident_rematerialization(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    date_from: str,
    date_to: str,
    max_dates: int = DEFAULT_MAX_DATES,
    seller_id: str | None = None,
    generated_at: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    requested_from = _iso_date(date_from, field_name="date_from")
    requested_to = _iso_date(date_to, field_name="date_to")
    if requested_from > requested_to:
        raise ValueError("date_from cannot be later than date_to")
    bounded_max_dates = min(max(int(max_dates), 1), DEFAULT_MAX_DATES)
    effective_from = max(
        date.fromisoformat(requested_from),
        date.fromisoformat(requested_to)
        - timedelta(days=bounded_max_dates - 1),
    ).isoformat()
    timestamp = str(generated_at or _generated_at())
    current_state = runtime.load_current_state()
    snapshot_dates = runtime.list_sheet_vitrina_ready_snapshot_dates(
        date_from=effective_from,
        date_to=requested_to,
    )
    reviewed_snapshots: list[dict[str, Any]] = []
    public_snapshots: list[dict[str, Any]] = []
    for as_of_date in snapshot_dates:
        snapshot = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=as_of_date)
        target_dates = [
            column_date
            for column_date in snapshot.date_columns
            if effective_from <= column_date <= requested_to
        ]
        if not target_dates:
            continue
        before_manifest = _incident_target_manifest(
            snapshot,
            target_dates=target_dates,
        )
        before_non_target = _non_target_digest(
            snapshot,
            target_dates=target_dates,
        )
        after_plan, evidence = _rematerialize_snapshot(
            runtime,
            snapshot=snapshot,
            target_dates=target_dates,
            generated_at=timestamp,
            seller_id=seller_id,
        )
        after_manifest = _incident_target_manifest(
            after_plan,
            target_dates=target_dates,
        )
        after_non_target = _non_target_digest(
            after_plan,
            target_dates=target_dates,
        )
        if before_non_target != after_non_target:
            raise ValueError(
                "incident rematerialization changed a non-target ready-snapshot field"
            )
        changed_cells = sum(
            1
            for row_id, by_date in after_manifest["values"].items()
            for column_date, value in by_date.items()
            if before_manifest["values"].get(row_id, {}).get(column_date) != value
        )
        snapshot_plan = {
            "bundle_version": current_state.bundle_version,
            "as_of_date": snapshot.as_of_date,
            "snapshot_id": snapshot.snapshot_id,
            "target_dates": target_dates,
            "before_plan_digest": _sheet_vitrina_plan_digest(snapshot),
            "after_plan_digest": _sheet_vitrina_plan_digest(after_plan),
            "non_target_digest": before_non_target,
            "changed_cells": changed_cells,
            "before_manifest": before_manifest,
            "after_manifest": after_manifest,
            "projection_evidence": evidence,
        }
        public_snapshots.append(snapshot_plan)
        reviewed_snapshots.append({**snapshot_plan, "after_plan": after_plan})

    plan_without_fingerprint = {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "mode": "dry_run",
        "generated_at": timestamp,
        "date_from_requested": requested_from,
        "date_from_effective": effective_from,
        "date_to": requested_to,
        "max_dates": bounded_max_dates,
        "bundle_version": current_state.bundle_version,
        "snapshot_count": len(public_snapshots),
        "changed_snapshot_count": sum(
            item["before_plan_digest"] != item["after_plan_digest"]
            for item in public_snapshots
        ),
        "changed_cells": sum(
            int(item["changed_cells"]) for item in public_snapshots
        ),
        "snapshots": public_snapshots,
        "raw_stock_truth_mutated": False,
        "non_target_invariant": "unchanged",
        "reversibility": "target before/after images persisted in runtime audit",
    }
    fingerprint = _digest(plan_without_fingerprint)
    plan = {
        **plan_without_fingerprint,
        "fingerprint": fingerprint,
        "operation_id": (
            f"vitrina-incident-{effective_from}-{requested_to}-"
            f"{fingerprint.removeprefix('sha256:')[:12]}"
        ),
        "apply_allowed": bool(public_snapshots),
    }
    return plan, reviewed_snapshots


def apply_vitrina_incident_rematerialization(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    reviewed_plan: Mapping[str, Any],
    fingerprint: str,
    approval_reference: str,
    actor: str,
    seller_id: str | None = None,
    applied_at: str | None = None,
) -> dict[str, Any]:
    expected_fingerprint = str(reviewed_plan.get("fingerprint") or "")
    if str(fingerprint or "") != expected_fingerprint:
        raise ValueError(
            "incident rematerialization reviewed plan and fingerprint do not match"
        )
    plan_without_fingerprint = {
        key: deepcopy(value)
        for key, value in reviewed_plan.items()
        if key not in {"fingerprint", "operation_id", "apply_allowed"}
    }
    if _digest(plan_without_fingerprint) != expected_fingerprint:
        raise ValueError("incident rematerialization reviewed plan fingerprint is invalid")
    recomputed_plan, recomputed_snapshots = plan_vitrina_incident_rematerialization(
        runtime,
        date_from=str(reviewed_plan.get("date_from_requested") or ""),
        date_to=str(reviewed_plan.get("date_to") or ""),
        max_dates=int(reviewed_plan.get("max_dates") or DEFAULT_MAX_DATES),
        seller_id=seller_id,
        generated_at=str(reviewed_plan.get("generated_at") or ""),
    )
    if recomputed_plan != dict(reviewed_plan):
        raise ValueError(
            "incident rematerialization sources or ready snapshots changed after review"
        )
    timestamp = str(applied_at or _generated_at())
    result = runtime.apply_sheet_vitrina_incident_rematerialization(
        operation_id=str(reviewed_plan.get("operation_id") or ""),
        plan_fingerprint=expected_fingerprint,
        approval_reference=str(approval_reference or ""),
        actor=str(actor or ""),
        applied_at=timestamp,
        snapshots=recomputed_snapshots,
    )
    readback_plan, _ = plan_vitrina_incident_rematerialization(
        runtime,
        date_from=str(reviewed_plan.get("date_from_requested") or ""),
        date_to=str(reviewed_plan.get("date_to") or ""),
        max_dates=int(reviewed_plan.get("max_dates") or DEFAULT_MAX_DATES),
        seller_id=seller_id,
        generated_at=str(reviewed_plan.get("generated_at") or ""),
    )
    if int(readback_plan.get("changed_cells") or 0) != 0:
        raise ValueError(
            "incident rematerialization readback is not idempotent"
        )
    return {
        **result,
        "readback_status": "ok",
        "readback_changed_cells": 0,
        "raw_stock_truth_mutated": False,
        "non_target_invariant": "unchanged",
    }
