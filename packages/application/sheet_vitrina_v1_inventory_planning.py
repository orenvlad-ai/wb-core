"""Date-aware inventory rows for the main Web Vitrina table.

The overlay never mutates ready snapshots.  It reads compact server-owned
component revisions and reuses the canonical ``stock_total`` row identity for
the unified total.  Legacy historical ``stock_total`` facts remain WB evidence
and are exposed only through the explicit WB row until a unified revision has
been materialized.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Sequence

from packages.application.inventory_planning_read_model import FORMULA_VERSION
from packages.contracts.registry_upload_bundle_v1 import ConfigV2Item
from packages.contracts.web_vitrina_contract import WebVitrinaContractRow


INVENTORY_PLANNING_SECTION_RU = "Остатки WB и FBS"
INVENTORY_WB_TOTAL_KEY = "inventory_wb_total_qty_v1"
INVENTORY_WB_EFFECTIVE_KEY = "inventory_wb_effective_qty_v1"
INVENTORY_FBS_TOTAL_KEY = "inventory_fbs_total_qty_v1"
INVENTORY_FBS_FACILITY_PREFIX = "inventory_fbs_facility_available_qty_v1:"
COMBINED_EFFECTIVE_ALIAS_KEY = "wb_stock_effective_qty"
COMBINED_TOTAL_ALIAS_KEY = "stock_total"
INVENTORY_PLANNING_HISTORY_REASON_RU = (
    "История общей формулы ещё не материализована из exact persisted components."
)
INVENTORY_PLANNING_LEGACY_HISTORY_REASON_RU = (
    "Историческое значение сохранено по прежней формуле; "
    "inventory_planning_v1 не применён задним числом."
)


@dataclass(frozen=True)
class _MetricSpec:
    sku_key: str
    total_key: str
    label_ru: str
    value_field: str
    reason_field: str
    facility_id: str = ""


def inventory_planning_total_metric_key(sku_key: str) -> str:
    return f"total_{sku_key}"


INVENTORY_PLANNING_LEGACY_SKU_METRIC_KEYS = frozenset(
    {INVENTORY_WB_EFFECTIVE_KEY, COMBINED_EFFECTIVE_ALIAS_KEY}
)
INVENTORY_PLANNING_LEGACY_METRIC_KEYS = frozenset(
    {
        *INVENTORY_PLANNING_LEGACY_SKU_METRIC_KEYS,
        *(
            inventory_planning_total_metric_key(key)
            for key in INVENTORY_PLANNING_LEGACY_SKU_METRIC_KEYS
        ),
    }
)


def inventory_planning_facility_metric_key(facility_id: str) -> str:
    return f"{INVENTORY_FBS_FACILITY_PREFIX}{facility_id}"


def is_inventory_planning_presentation_metric_key(metric_key: str) -> bool:
    normalized = str(metric_key or "").removeprefix("total_")
    return normalized in {
        INVENTORY_WB_TOTAL_KEY,
        INVENTORY_FBS_TOTAL_KEY,
        COMBINED_TOTAL_ALIAS_KEY,
    } or normalized.startswith(INVENTORY_FBS_FACILITY_PREFIX)


def apply_fbs_last_good_presentation(
    rows: Iterable[WebVitrinaContractRow],
    *,
    reason_ru: str,
    last_good_at: str,
    source_as_of_date: str,
) -> list[WebVitrinaContractRow]:
    """Label only non-empty FBS-dependent cells as incident last-good values."""

    result: list[WebVitrinaContractRow] = []
    for row in rows:
        normalized_key = str(row.metric_key).removeprefix("total_")
        fbs_dependent = (
            normalized_key in {COMBINED_TOTAL_ALIAS_KEY, INVENTORY_FBS_TOTAL_KEY}
            or normalized_key.startswith(INVENTORY_FBS_FACILITY_PREFIX)
        )
        if not fbs_dependent:
            result.append(row)
            continue
        presentation = dict(row.presentation_by_date)
        changed = False
        for business_date, value in row.values_by_date.items():
            if value in {None, ""}:
                continue
            current = dict(presentation.get(business_date) or {})
            existing_reason = str(
                current.get("quality_reason") or current.get("reason") or ""
            ).strip()
            combined_reason = reason_ru
            if existing_reason and existing_reason not in combined_reason:
                combined_reason = f"{combined_reason} {existing_reason}"
            current.update(
                {
                    "state": "",
                    "tone": "warning",
                    "reason": combined_reason,
                    "source": FORMULA_VERSION,
                    "quality_state": "fbs_last_good_owner_paused",
                    "quality_label": "Последние подтверждённые данные",
                    "quality_reason": combined_reason,
                    "last_good_at": last_good_at,
                    "last_good_source_as_of_date": source_as_of_date,
                }
            )
            presentation[business_date] = current
            changed = True
        result.append(
            replace(row, presentation_by_date=presentation) if changed else row
        )
    return result


def apply_fbs_unavailable_presentation(
    rows: Iterable[WebVitrinaContractRow],
    *,
    reason_ru: str,
) -> list[WebVitrinaContractRow]:
    """Keep an unadmitted FBS source missing even after legacy overlays."""

    result: list[WebVitrinaContractRow] = []
    for row in rows:
        normalized_key = str(row.metric_key).removeprefix("total_")
        fbs_dependent = (
            normalized_key in {COMBINED_TOTAL_ALIAS_KEY, INVENTORY_FBS_TOTAL_KEY}
            or normalized_key.startswith(INVENTORY_FBS_FACILITY_PREFIX)
        )
        if not fbs_dependent:
            result.append(row)
            continue
        values = dict(row.values_by_date)
        presentation = dict(row.presentation_by_date)
        for business_date in values:
            values[business_date] = ""
            presentation[business_date] = {
                "state": "unavailable",
                "tone": "warning",
                "reason": reason_ru,
                "source": FORMULA_VERSION,
                "quality_state": "fbs_unavailable_owner_paused",
                "quality_label": "Нет данных",
                "quality_reason": reason_ru,
            }
        result.append(
            replace(
                row,
                values_by_date=values,
                presentation_by_date=presentation,
            )
        )
    return result


def inventory_planning_sku_metric_keys(planning: Mapping[str, Any]) -> list[str]:
    return [spec.sku_key for spec in _public_metric_specs(planning)]


def extend_rows_with_inventory_planning(
    rows: Iterable[WebVitrinaContractRow],
    *,
    planning: Mapping[str, Any],
    history: Mapping[str, Any] | None = None,
    date_columns: list[str],
    enabled_config: list[ConfigV2Item],
) -> list[WebVitrinaContractRow]:
    """Materialize current planning rows while preserving exact-date history."""

    source_rows = list(rows)
    history_payload = dict(history or {})
    current_applies = _planning_applies(planning, date_columns=date_columns)
    history_applies = bool(dict(history_payload.get("dates") or {}))
    legacy_wb_applies = _has_legacy_wb_history(
        source_rows,
        date_columns=date_columns,
    )
    if not current_applies and not history_applies and not legacy_wb_applies:
        return source_rows

    current_date = str((planning.get("wb") or {}).get("snapshot_date") or "")
    specs = _public_metric_specs(
        planning,
        history=history_payload,
        include_facilities=current_applies or history_applies,
    )
    planning_keys = {
        key
        for spec in specs
        for key in (spec.sku_key, spec.total_key)
    }
    planning_by_nm_id = {
        int(item["nm_id"]): item
        for item in list(planning.get("skus") or [])
        if isinstance(item, Mapping) and str(item.get("nm_id") or "").isdigit()
    }
    config_by_nm_id = {
        int(item.nm_id): item for item in enabled_config if bool(item.enabled)
    }

    scope_order: list[str] = []
    rows_by_scope: dict[str, list[WebVitrinaContractRow]] = {}
    for row in source_rows:
        scope_id = _scope_id(row)
        if scope_id not in rows_by_scope:
            scope_order.append(scope_id)
            rows_by_scope[scope_id] = []
        rows_by_scope[scope_id].append(row)
    if "TOTAL" not in rows_by_scope:
        scope_order.insert(0, "TOTAL")
        rows_by_scope["TOTAL"] = []
    for item in sorted(
        config_by_nm_id.values(),
        key=lambda config: (int(config.display_order), int(config.nm_id)),
    ):
        scope_id = f"SKU:{int(item.nm_id)}"
        if scope_id not in rows_by_scope:
            scope_order.append(scope_id)
            rows_by_scope[scope_id] = []

    result: list[WebVitrinaContractRow] = []
    for scope_id in scope_order:
        cluster = rows_by_scope[scope_id]
        if scope_id == "TOTAL":
            result.extend(
                _replace_planning_cluster(
                    cluster,
                    specs=specs,
                    planning_keys=planning_keys,
                    current_date=current_date,
                    date_columns=date_columns,
                    scope_kind="TOTAL",
                    scope_key="TOTAL",
                    scope_label="ИТОГО",
                    group=None,
                    nm_id=None,
                    value_source=_aggregate_value_source(planning),
                    history_by_date=_history_scopes(
                        history_payload,
                        scope_key="TOTAL",
                    ),
                    row_updated_at=_planning_updated_at(planning),
                )
            )
            continue
        if scope_id.startswith("SKU:"):
            try:
                nm_id = int(scope_id.split(":", 1)[1])
            except ValueError:
                result.extend(cluster)
                continue
            config = config_by_nm_id.get(nm_id)
            if config is None:
                result.extend(cluster)
                continue
            result.extend(
                _replace_planning_cluster(
                    cluster,
                    specs=specs,
                    planning_keys=planning_keys,
                    current_date=current_date,
                    date_columns=date_columns,
                    scope_kind="SKU",
                    scope_key=scope_id,
                    scope_label=str(config.display_name),
                    group=str(config.group or "") or None,
                    nm_id=nm_id,
                    value_source=_sku_value_source(
                        planning_by_nm_id.get(nm_id),
                        planning=planning,
                    ),
                    history_by_date=_history_scopes(
                        history_payload,
                        scope_key=scope_id,
                    ),
                    row_updated_at=_planning_updated_at(planning),
                )
            )
            continue
        result.extend(cluster)

    return [replace(row, row_order=index) for index, row in enumerate(result, start=1)]


def _metric_specs(planning: Mapping[str, Any]) -> list[_MetricSpec]:
    specs = [
        _MetricSpec(
            sku_key=INVENTORY_WB_TOTAL_KEY,
            total_key=inventory_planning_total_metric_key(INVENTORY_WB_TOTAL_KEY),
            label_ru="Остаток WB: всего",
            value_field="wb_total",
            reason_field="wb_total_reason_ru",
        ),
        _MetricSpec(
            sku_key=INVENTORY_WB_EFFECTIVE_KEY,
            total_key=inventory_planning_total_metric_key(INVENTORY_WB_EFFECTIVE_KEY),
            label_ru="Остаток WB без инц.: всего",
            value_field="wb_effective_total",
            reason_field="wb_effective_total_reason_ru",
        ),
        _MetricSpec(
            sku_key=INVENTORY_FBS_TOTAL_KEY,
            total_key=inventory_planning_total_metric_key(INVENTORY_FBS_TOTAL_KEY),
            label_ru="Остаток FBS: всего",
            value_field="fbs_total",
            reason_field="fbs_total_reason_ru",
        ),
    ]
    active_facilities = [
        facility
        for facility in list((planning.get("fbs") or {}).get("facilities") or [])
        if isinstance(facility, Mapping) and bool(facility.get("active"))
    ]
    facility_name_counts: dict[str, int] = {}
    for facility in active_facilities:
        name = str(facility.get("name") or facility.get("facility_id") or "").strip()
        facility_name_counts[name.casefold()] = facility_name_counts.get(name.casefold(), 0) + 1
    for facility in active_facilities:
        facility_id = str(facility.get("facility_id") or "").strip()
        name = str(facility.get("name") or facility_id).strip()
        if not facility_id:
            continue
        display_name = (
            f"{name} ({str(facility.get('code') or facility_id).strip()})"
            if facility_name_counts.get(name.casefold(), 0) > 1
            else name
        )
        sku_key = inventory_planning_facility_metric_key(facility_id)
        specs.append(
            _MetricSpec(
                sku_key=sku_key,
                total_key=inventory_planning_total_metric_key(sku_key),
                label_ru=f"Остаток FBS: {display_name}",
                value_field="facility_available",
                reason_field="facility_available_reason_ru",
                facility_id=facility_id,
            )
        )
    specs.extend(
        (
            _MetricSpec(
                sku_key=COMBINED_EFFECTIVE_ALIAS_KEY,
                total_key=inventory_planning_total_metric_key(COMBINED_EFFECTIVE_ALIAS_KEY),
                label_ru="Остаток без инц.: всего",
                value_field="effective_total",
                reason_field="effective_total_reason_ru",
            ),
            _MetricSpec(
                sku_key=COMBINED_TOTAL_ALIAS_KEY,
                total_key=inventory_planning_total_metric_key(COMBINED_TOTAL_ALIAS_KEY),
                label_ru="Остаток: всего",
                value_field="total",
                reason_field="total_reason_ru",
            ),
        )
    )
    return specs


def _public_metric_specs(
    planning: Mapping[str, Any],
    *,
    history: Mapping[str, Any] | None = None,
    include_facilities: bool = True,
) -> list[_MetricSpec]:
    """Return the approved public order without a duplicate FBS aggregate row."""

    combined = _MetricSpec(
        sku_key=COMBINED_TOTAL_ALIAS_KEY,
        total_key=inventory_planning_total_metric_key(COMBINED_TOTAL_ALIAS_KEY),
        label_ru="Остатки общие",
        value_field="total",
        reason_field="total_reason_ru",
    )
    wb = _MetricSpec(
        sku_key=INVENTORY_WB_TOTAL_KEY,
        total_key=inventory_planning_total_metric_key(INVENTORY_WB_TOTAL_KEY),
        label_ru="Остатки WB",
        value_field="wb_total",
        reason_field="wb_total_reason_ru",
    )
    facilities_by_id: dict[str, Mapping[str, Any]] = {}
    for raw in [
        *list((planning.get("fbs") or {}).get("facilities") or []),
        *list((history or {}).get("facilities") or []),
    ]:
        if not isinstance(raw, Mapping):
            continue
        facility_id = str(raw.get("facility_id") or "").strip()
        if facility_id:
            facilities_by_id.setdefault(facility_id, raw)
    ordered_facilities = sorted(
        facilities_by_id.values(),
        key=_public_facility_order_key,
    )
    facility_specs = [
        _MetricSpec(
            sku_key=inventory_planning_facility_metric_key(str(item["facility_id"])),
            total_key=inventory_planning_total_metric_key(
                inventory_planning_facility_metric_key(str(item["facility_id"]))
            ),
            label_ru=f"Остатки FBS {_public_facility_name(item)}",
            value_field="facility_available",
            reason_field="facility_available_reason_ru",
            facility_id=str(item["facility_id"]),
        )
        for item in ordered_facilities
        if include_facilities
    ]
    return [combined, wb, *facility_specs]


def _public_facility_name(facility: Mapping[str, Any]) -> str:
    """Return a public name without repeating an existing facility-type prefix."""

    raw_name = str(facility.get("name") or facility.get("facility_id") or "").strip()
    public_name = re.sub(
        r"^(?:FBS|FF|ФБС|ФФ)(?=$|[\s:·-])[\s:·-]*",
        "",
        raw_name,
        count=1,
        flags=re.IGNORECASE,
    ).strip()
    return public_name or raw_name


def _public_facility_order_key(facility: Mapping[str, Any]) -> tuple[int, int, str, str]:
    """Pin the approved Moscow/Orenburg lead rows, then retain roster order."""

    priority = {
        "москва": 0,
        "оренбург": 1,
    }.get(_public_facility_name(facility).casefold(), 2)
    return (
        priority,
        int(facility.get("display_order") or 0),
        str(facility.get("code") or ""),
        str(facility.get("facility_id") or ""),
    )


def _planning_applies(planning: Mapping[str, Any], *, date_columns: list[str]) -> bool:
    if str(planning.get("status") or "") != "ready":
        return False
    formula = planning.get("formula") or {}
    if str(formula.get("version") or "") != FORMULA_VERSION:
        return False
    snapshot_date = str((planning.get("wb") or {}).get("snapshot_date") or "")
    effective_from = str(formula.get("effective_from") or "")
    return bool(
        snapshot_date
        and snapshot_date in set(date_columns)
        and (not effective_from or snapshot_date >= effective_from)
    )


def _has_legacy_wb_history(
    rows: Sequence[WebVitrinaContractRow],
    *,
    date_columns: Sequence[str],
) -> bool:
    legacy_keys = {COMBINED_TOTAL_ALIAS_KEY, f"total_{COMBINED_TOTAL_ALIAS_KEY}"}
    return any(
        row.metric_key in legacy_keys
        and any(row.values_by_date.get(column_date) not in {None, ""} for column_date in date_columns)
        for row in rows
    )


def _replace_planning_cluster(
    cluster: list[WebVitrinaContractRow],
    *,
    specs: list[_MetricSpec],
    planning_keys: set[str],
    current_date: str,
    date_columns: list[str],
    scope_kind: str,
    scope_key: str,
    scope_label: str,
    group: str | None,
    nm_id: int | None,
    value_source: Mapping[str, Any],
    history_by_date: Mapping[str, Mapping[str, Any]],
    row_updated_at: str,
) -> list[WebVitrinaContractRow]:
    existing_by_key = {
        row.metric_key: row for row in cluster if row.metric_key in planning_keys
    }
    legacy_combined_key = (
        inventory_planning_total_metric_key(COMBINED_TOTAL_ALIAS_KEY)
        if scope_kind == "TOTAL"
        else COMBINED_TOTAL_ALIAS_KEY
    )
    legacy_wb_row = existing_by_key.get(legacy_combined_key)
    insert_at = min(
        (
            index
            for index, row in enumerate(cluster)
            if row.metric_key in planning_keys
        ),
        default=len(cluster),
    )
    retained = [row for row in cluster if row.metric_key not in planning_keys]
    insert_at = min(insert_at, len(retained))
    materialized = [
        _planning_row(
            existing=existing_by_key.get(
                spec.total_key if scope_kind == "TOTAL" else spec.sku_key
            ),
            metric_key=spec.total_key if scope_kind == "TOTAL" else spec.sku_key,
            spec=spec,
            current_date=current_date,
            date_columns=date_columns,
            scope_kind=scope_kind,
            scope_key=scope_key,
            scope_label=scope_label,
            group=group,
            nm_id=nm_id,
            value_source=value_source,
            history_by_date=history_by_date,
            legacy_wb_row=legacy_wb_row,
            row_updated_at=row_updated_at,
        )
        for spec in specs
    ]
    return [*retained[:insert_at], *materialized, *retained[insert_at:]]


def _planning_row(
    *,
    existing: WebVitrinaContractRow | None,
    metric_key: str,
    spec: _MetricSpec,
    current_date: str,
    date_columns: list[str],
    scope_kind: str,
    scope_key: str,
    scope_label: str,
    group: str | None,
    nm_id: int | None,
    value_source: Mapping[str, Any],
    history_by_date: Mapping[str, Mapping[str, Any]],
    legacy_wb_row: WebVitrinaContractRow | None,
    row_updated_at: str,
) -> WebVitrinaContractRow:
    values = {column_date: "" for column_date in date_columns}
    presentation = {
        column_date: _history_unavailable_presentation()
        for column_date in date_columns
        if column_date != current_date
    }
    for column_date in date_columns:
        if column_date == current_date:
            continue
        historical_scope = history_by_date.get(column_date)
        if historical_scope is not None:
            historical_value, historical_presentation = _historical_metric_value(
                spec,
                historical_scope,
            )
            values[column_date] = "" if historical_value is None else historical_value
            presentation[column_date] = historical_presentation
            continue
        if spec.sku_key == INVENTORY_WB_TOTAL_KEY and legacy_wb_row is not None:
            legacy_value = legacy_wb_row.values_by_date.get(column_date)
            if legacy_value is not None and legacy_value != "":
                values[column_date] = legacy_value
                presentation[column_date] = _legacy_wb_presentation()
    if current_date in date_columns:
        value, reason, quality, missing_components = _metric_value(spec, value_source)
        values[current_date] = value if value is not None else ""
        presentation[current_date] = _current_presentation(
            reason=reason,
            quality=quality,
            missing_components=missing_components,
        )
    return WebVitrinaContractRow(
        row_id=f"{scope_key}|{metric_key}",
        row_order=existing.row_order if existing is not None else 0,
        scope_kind=scope_kind,
        scope_key=scope_key,
        scope_label=scope_label,
        metric_key=metric_key,
        metric_label=spec.label_ru,
        row_last_updated_at=row_updated_at or (
            existing.row_last_updated_at if existing is not None else ""
        ),
        section=INVENTORY_PLANNING_SECTION_RU,
        group=group,
        nm_id=nm_id,
        format="number",
        values_by_date=values,
        presentation_by_date=presentation,
    )


def _metric_value(
    spec: _MetricSpec,
    source: Mapping[str, Any],
) -> tuple[int | None, str, str, list[str]]:
    if spec.facility_id:
        facility = next(
            (
                item
                for item in list(source.get("fbs_facilities") or [])
                if str(item.get("facility_id") or "") == spec.facility_id
            ),
            None,
        )
        if facility is None:
            return None, "", "inapplicable", []
        value = facility.get("available")
        return (
            None if value is None else int(value),
            str(facility.get("reason_ru") or ""),
            str(facility.get("state") or facility.get("quality") or "missing"),
            [str(facility.get("name") or spec.facility_id)]
            if facility.get("applicable") and value is None
            else [],
        )
    value = source.get(spec.value_field)
    return (
        None if value is None else int(value),
        str((source.get("quality") or {}).get(spec.reason_field) or ""),
        str((source.get("quality") or {}).get(spec.value_field) or "exact"),
        list((source.get("quality") or {}).get("missing_components") or []),
    )


def _aggregate_value_source(planning: Mapping[str, Any]) -> dict[str, Any]:
    metrics = {
        str(item.get("metric_key") or ""): item
        for item in list(planning.get("metrics") or [])
        if isinstance(item, Mapping)
    }
    facilities = [
        {
            "facility_id": str(item.get("facility_id") or ""),
            "available": item.get("available"),
            "active": bool(item.get("active")),
            "applicable": bool(item.get("applicable")),
            "state": str(item.get("state") or "missing"),
            "name": str(item.get("name") or item.get("facility_id") or ""),
            "reason_ru": (
                ""
                if item.get("available") is not None
                else "Недоступно: для active facility нет строки physical FBS ledger."
            ),
        }
        for item in list((planning.get("fbs") or {}).get("facilities") or [])
        if isinstance(item, Mapping)
    ]
    return {
        "wb_total": (metrics.get("wb_total") or {}).get("value"),
        "wb_effective_total": (metrics.get("wb_effective_total") or {}).get("value"),
        "fbs_total": (metrics.get("fbs_total") or {}).get("value"),
        "effective_total": (metrics.get("effective_total") or {}).get("value"),
        "total": (metrics.get("total") or {}).get("value"),
        "fbs_facilities": facilities,
        "quality": {
            "wb_total_reason_ru": str((metrics.get("wb_total") or {}).get("reason_ru") or ""),
            "wb_effective_total_reason_ru": str(
                (metrics.get("wb_effective_total") or {}).get("reason_ru") or ""
            ),
            "fbs_total_reason_ru": str((metrics.get("fbs_total") or {}).get("reason_ru") or ""),
            "effective_total_reason_ru": str(
                (metrics.get("effective_total") or {}).get("reason_ru") or ""
            ),
            "total_reason_ru": str((metrics.get("total") or {}).get("reason_ru") or ""),
            "total": str((metrics.get("total") or {}).get("quality") or "exact"),
            "missing_components": list((planning.get("fbs") or {}).get("missing_components") or []),
        },
    }


def _sku_value_source(
    sku: Mapping[str, Any] | None,
    *,
    planning: Mapping[str, Any],
) -> Mapping[str, Any]:
    if sku is not None:
        return sku
    incident_reason = str(
        ((planning.get("wb") or {}).get("incident_evidence") or {}).get("reason_ru")
        or ""
    )
    wb_reason = "Недоступно: официальный WB aggregate не содержит exact SKU quantity."
    fbs_reason = "Недоступно: для SKU нет exact physical FBS ledger row по всем active facility."
    return {
        "fbs_facilities": [],
        "quality": {
            "wb_total_reason_ru": wb_reason,
            "wb_effective_total_reason_ru": wb_reason or incident_reason,
            "fbs_total_reason_ru": fbs_reason,
            "effective_total_reason_ru": wb_reason or incident_reason or fbs_reason,
            "total_reason_ru": wb_reason or fbs_reason,
        },
    }


def _current_presentation(
    *,
    reason: str,
    quality: str,
    missing_components: list[str],
) -> dict[str, str]:
    if quality == "inapplicable":
        return _inapplicable_presentation()
    if quality == "partial":
        missing = ", ".join(missing_components)
        quality_reason = reason or f"Отсутствуют компоненты: {missing}"
        return {
            "state": "",
            "tone": "neutral",
            "reason": quality_reason,
            "source": FORMULA_VERSION,
            "quality_state": "inventory_history_partial",
            "quality_label": "Частичные данные",
            "quality_reason": quality_reason,
            "missing_components": missing,
        }
    if quality == "missing":
        return {
            "state": "unavailable",
            "tone": "warning",
            "reason": reason,
            "source": FORMULA_VERSION,
            "quality_state": "inventory_history_component_missing",
            "quality_label": "Компонент отсутствует",
            "quality_reason": reason,
        }
    if reason and quality == "unavailable":
        return {
            "state": "unavailable",
            "tone": "warning",
            "reason": reason,
            "source": FORMULA_VERSION,
            "quality_state": "inventory_planning_unavailable",
            "quality_label": "Недоступно",
            "quality_reason": reason,
        }
    return {
        "state": "",
        "tone": "success",
        "reason": "",
        "source": FORMULA_VERSION,
        "quality_state": FORMULA_VERSION,
        "quality_label": "Точное значение",
        "quality_reason": f"{FORMULA_VERSION}: exact persisted operands",
    }


def _history_scopes(
    history: Mapping[str, Any],
    *,
    scope_key: str,
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for business_date, raw_date in dict(history.get("dates") or {}).items():
        if not isinstance(raw_date, Mapping):
            continue
        scope = dict(raw_date.get("scopes") or {}).get(scope_key)
        if isinstance(scope, Mapping):
            result[str(business_date)] = scope
    return result


def _historical_metric_value(
    spec: _MetricSpec,
    scope: Mapping[str, Any],
) -> tuple[int | None, dict[str, Any]]:
    if spec.sku_key == COMBINED_TOTAL_ALIAS_KEY:
        value = scope.get("total")
        quality = str(scope.get("quality") or "unavailable")
        missing = [str(item) for item in list(scope.get("missing_components") or [])]
        if quality == "partial" and value is not None:
            reason = "Отсутствуют компоненты: " + ", ".join(missing)
            return int(value), {
                "state": "",
                "tone": "neutral",
                "reason": reason,
                "source": FORMULA_VERSION,
                "quality_state": "inventory_history_partial",
                "quality_label": "Частичные данные",
                "quality_reason": reason,
                "missing_components": ", ".join(missing),
            }
        if value is None:
            return None, _history_unavailable_presentation()
        return int(value), _exact_history_presentation()
    if spec.sku_key == INVENTORY_WB_TOTAL_KEY:
        component = scope.get("wb") or {}
    elif spec.facility_id:
        component = dict(scope.get("facilities") or {}).get(spec.facility_id) or {
            "state": "inapplicable",
            "value": None,
        }
    else:
        return None, _history_unavailable_presentation()
    state = str(component.get("state") or "missing")
    value = component.get("value")
    if state == "inapplicable":
        return None, _inapplicable_presentation()
    if state == "missing":
        reason = f"Отсутствует exact component: {spec.label_ru}."
        return None, {
            "state": "unavailable",
            "tone": "warning",
            "reason": reason,
            "source": FORMULA_VERSION,
            "quality_state": "inventory_history_component_missing",
            "quality_label": "Компонент отсутствует",
            "quality_reason": reason,
        }
    return int(value), _exact_history_presentation()


def _exact_history_presentation() -> dict[str, str]:
    return {
        "state": "",
        "tone": "success",
        "reason": "",
        "source": FORMULA_VERSION,
        "quality_state": "inventory_history_full",
        "quality_label": "Точное значение",
        "quality_reason": "Exact finalized component revision.",
    }


def _inapplicable_presentation() -> dict[str, str]:
    return {
        "state": "unavailable",
        "tone": "neutral",
        "reason": "",
        "source": FORMULA_VERSION,
        "quality_state": "inventory_history_inapplicable",
        "quality_label": "Не применимо",
        "quality_reason": "",
    }


def _legacy_wb_presentation() -> dict[str, str]:
    return {
        "state": "",
        "tone": "success",
        "reason": "",
        "source": "ready_snapshot.stock_total.wb_only",
        "quality_state": "inventory_history_legacy_wb_exact",
        "quality_label": "Остатки WB",
        "quality_reason": INVENTORY_PLANNING_LEGACY_HISTORY_REASON_RU,
    }


def _history_unavailable_presentation() -> dict[str, str]:
    return {
        "state": "unavailable",
        "tone": "neutral",
        "reason": INVENTORY_PLANNING_HISTORY_REASON_RU,
        "source": FORMULA_VERSION,
        "quality_state": "inventory_planning_history_unavailable",
        "quality_label": "История не материализована",
        "quality_reason": INVENTORY_PLANNING_HISTORY_REASON_RU,
    }


def _planning_updated_at(planning: Mapping[str, Any]) -> str:
    freshness = planning.get("freshness") or {}
    return max(
        str(freshness.get("wb_fetched_at") or ""),
        str(freshness.get("fbs_updated_at") or ""),
    )


def _scope_id(row: WebVitrinaContractRow) -> str:
    return str(row.scope_key or row.scope_kind or row.row_id.split("|", 1)[0])
