"""Phase-1 web-vitrina read contract built from the existing ready snapshot seam."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.business_time import (
    CANONICAL_BUSINESS_TIMEZONE_NAME,
    current_business_date_iso,
    default_business_as_of_date,
    to_business_datetime,
)
from packages.contracts.registry_upload_bundle_v1 import ConfigV2Item, MetricV2Item
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope, SheetVitrinaWriteTarget
from packages.contracts.web_vitrina_contract import (
    WebVitrinaContractCapabilities,
    WebVitrinaContractMeta,
    WebVitrinaContractRow,
    WebVitrinaContractSchema,
    WebVitrinaContractSchemaColumn,
    WebVitrinaContractSchemaFilter,
    WebVitrinaContractSchemaSort,
    WebVitrinaContractStatusSummary,
    WebVitrinaContractV1,
)

WEB_VITRINA_CONTRACT_NAME = "web_vitrina_contract"
WEB_VITRINA_CONTRACT_VERSION = "v1"
WEB_VITRINA_READ_MODEL = "persisted_ready_snapshot"
WEB_VITRINA_PERIOD_READ_MODEL = "persisted_ready_snapshot_window"
WEB_VITRINA_SOURCE_SHEET_NAME = "DATA_VITRINA"
WEB_VITRINA_PERIOD_PLAN_VERSION = "delivery_contract_v1__web_vitrina_period_window_v1"


@dataclass(frozen=True)
class _ScopeDescriptor:
    scope_kind: str
    scope_key: str
    scope_label: str
    group: str | None
    nm_id: int | None


class SheetVitrinaV1WebVitrinaBlock:
    """Project a stable, grid-library-agnostic contract from the existing ready snapshot."""

    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.runtime = runtime
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))

    def build(
        self,
        *,
        page_route: str,
        read_route: str,
        as_of_date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> WebVitrinaContractV1:
        now = self.now_factory()
        current_state = self.runtime.load_current_state()
        _validate_period_request(as_of_date=as_of_date, date_from=date_from, date_to=date_to)
        read_model = WEB_VITRINA_READ_MODEL
        if date_from and date_to:
            snapshot = _build_period_snapshot(
                runtime=self.runtime,
                current_state=current_state,
                date_from=date_from,
                date_to=date_to,
            )
            refreshed_at = _resolve_period_refreshed_at(
                runtime=self.runtime,
                date_columns=snapshot.date_columns,
            )
            refresh_status_value = "success"
            data_sheet_row_count = len(snapshot.sheets[0].rows) if snapshot.sheets else 0
            read_model = WEB_VITRINA_PERIOD_READ_MODEL
        else:
            requested_as_of_date = as_of_date or default_business_as_of_date(now)
            try:
                snapshot = self.runtime.load_sheet_vitrina_ready_snapshot(as_of_date=requested_as_of_date)
            except ValueError:
                if as_of_date:
                    raise
                snapshot = self.runtime.load_sheet_vitrina_ready_snapshot()
            refresh_status = self.runtime.load_sheet_vitrina_refresh_status(as_of_date=snapshot.as_of_date)
            refreshed_at = refresh_status.refreshed_at
            refresh_status_value = refresh_status.status
            data_sheet_row_count = refresh_status.sheet_row_counts.get(WEB_VITRINA_SOURCE_SHEET_NAME, 0)
        auto_update_state = self.runtime.load_sheet_vitrina_auto_update_state()
        manual_state = self.runtime.load_sheet_vitrina_manual_operator_state()
        data_sheet = _require_data_sheet(snapshot)

        config_by_nm_id = {
            int(item.nm_id): item
            for item in current_state.config_v2
        }
        metrics_by_key = {
            str(item.metric_key): item
            for item in current_state.metrics_v2
        }
        rows = _normalize_rows(
            data_sheet.rows,
            date_columns=snapshot.date_columns,
            config_by_nm_id=config_by_nm_id,
            metrics_by_key=metrics_by_key,
        )

        return WebVitrinaContractV1(
            contract_name=WEB_VITRINA_CONTRACT_NAME,
            contract_version=WEB_VITRINA_CONTRACT_VERSION,
            page_route=page_route,
            read_route=read_route,
            meta=WebVitrinaContractMeta(
                snapshot_id=snapshot.snapshot_id,
                bundle_version=current_state.bundle_version,
                as_of_date=snapshot.as_of_date,
                business_timezone=CANONICAL_BUSINESS_TIMEZONE_NAME,
                date_columns=list(snapshot.date_columns),
                temporal_slots=list(snapshot.temporal_slots),
                generated_at=_to_utc_timestamp(now),
                refreshed_at=refreshed_at,
                row_count=len(rows),
            ),
            status_summary=WebVitrinaContractStatusSummary(
                refresh_status=refresh_status_value,
                read_model=read_model,
                source_sheet_name=WEB_VITRINA_SOURCE_SHEET_NAME,
                bundle_version=current_state.bundle_version,
                activated_at=current_state.activated_at,
                refreshed_at=refreshed_at,
                business_now=to_business_datetime(now).replace(microsecond=0).isoformat(),
                current_business_date=current_business_date_iso(now),
                default_as_of_date=default_business_as_of_date(now),
                last_auto_run_status=auto_update_state.last_run_status or "never",
                last_auto_run_started_at=auto_update_state.last_run_started_at,
                last_auto_run_finished_at=auto_update_state.last_run_finished_at,
                last_successful_auto_update_at=auto_update_state.last_successful_auto_update_at,
                last_successful_manual_refresh_at=manual_state.last_successful_manual_refresh_at,
                last_successful_manual_load_at=manual_state.last_successful_manual_load_at,
                source_policy_counts=_count_values(snapshot.source_temporal_policies),
                source_count=len(snapshot.source_temporal_policies),
                data_sheet_row_count=data_sheet_row_count or len(rows),
            ),
            schema=_build_schema(snapshot),
            rows=rows,
            capabilities=WebVitrinaContractCapabilities(
                sortable=True,
                filterable=True,
                exportable=False,
                read_only=True,
                grid_library_agnostic=True,
                thin_page_shell=True,
            ),
        )


def _validate_period_request(
    *,
    as_of_date: str | None,
    date_from: str | None,
    date_to: str | None,
) -> None:
    normalized_as_of_date = str(as_of_date or "").strip()
    normalized_date_from = str(date_from or "").strip()
    normalized_date_to = str(date_to or "").strip()
    if normalized_as_of_date and (normalized_date_from or normalized_date_to):
        raise ValueError("as_of_date is mutually exclusive with date_from/date_to")
    if bool(normalized_date_from) != bool(normalized_date_to):
        raise ValueError("date_from and date_to must be provided together")
    if not normalized_date_from:
        return
    start = date.fromisoformat(normalized_date_from)
    end = date.fromisoformat(normalized_date_to)
    if end < start:
        raise ValueError("date_to must be >= date_from")


def _build_period_snapshot(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    current_state: Any,
    date_from: str,
    date_to: str,
) -> SheetVitrinaV1Envelope:
    selected_dates = _resolve_period_dates(runtime=runtime, date_from=date_from, date_to=date_to)
    snapshots = [
        runtime.load_sheet_vitrina_ready_snapshot(as_of_date=snapshot_date)
        for snapshot_date in selected_dates
    ]
    template_sheet = _require_data_sheet(snapshots[0])
    template_rows = list(template_sheet.rows)
    value_maps = {
        snapshot_date: _extract_snapshot_values_by_row_id(
            _require_data_sheet(snapshot),
            expected_date=snapshot_date,
        )
        for snapshot_date, snapshot in zip(selected_dates, snapshots, strict=True)
    }

    combined_rows: list[list[Any]] = []
    for row in template_rows:
        row_id = str(row[1] or "").strip()
        if not row_id:
            continue
        combined_row = [row[0], row_id]
        for snapshot_date in selected_dates:
            values_by_row_id = value_maps[snapshot_date]
            if row_id not in values_by_row_id:
                raise ValueError(
                    f"period window row universe mismatch for {row_id!r} on {snapshot_date}"
                )
            combined_row.append(values_by_row_id[row_id])
        combined_rows.append(combined_row)

    return SheetVitrinaV1Envelope(
        plan_version=WEB_VITRINA_PERIOD_PLAN_VERSION,
        snapshot_id=f"{date_from}__{date_to}__web_vitrina_period_window_v1__ready",
        as_of_date=date_to,
        date_columns=selected_dates,
        temporal_slots=[
            _build_period_temporal_slot(snapshot_date)
            for snapshot_date in selected_dates
        ],
        source_temporal_policies={},
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name=WEB_VITRINA_SOURCE_SHEET_NAME,
                write_start_cell=template_sheet.write_start_cell,
                write_rect=template_sheet.write_rect,
                clear_range=template_sheet.clear_range,
                write_mode=template_sheet.write_mode,
                partial_update_allowed=template_sheet.partial_update_allowed,
                header=[template_sheet.header[0], template_sheet.header[1], *selected_dates],
                rows=combined_rows,
                row_count=len(combined_rows),
                column_count=2 + len(selected_dates),
            )
        ],
    )


def _resolve_period_dates(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    date_from: str,
    date_to: str,
) -> list[str]:
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    expected_dates = [
        (start + timedelta(days=offset)).isoformat()
        for offset in range((end - start).days + 1)
    ]
    available_dates = set(
        runtime.list_sheet_vitrina_ready_snapshot_dates(
            date_from=date_from,
            date_to=date_to,
        )
    )
    missing_dates = [snapshot_date for snapshot_date in expected_dates if snapshot_date not in available_dates]
    if missing_dates:
        if len(missing_dates) == 1:
            detail = missing_dates[0]
        else:
            detail = f"{missing_dates[0]}..{missing_dates[-1]} ({len(missing_dates)} days)"
        raise ValueError(
            "web_vitrina period window missing ready snapshots: "
            f"{detail}"
        )
    return expected_dates


def _extract_snapshot_values_by_row_id(
    data_sheet: SheetVitrinaWriteTarget,
    *,
    expected_date: str,
) -> dict[str, Any]:
    try:
        column_index = data_sheet.header.index(expected_date)
    except ValueError as exc:
        raise ValueError(
            f"ready snapshot DATA_VITRINA does not contain expected date column {expected_date}"
        ) from exc
    values_by_row_id: dict[str, Any] = {}
    for row in data_sheet.rows:
        row_id = str(row[1] or "").strip() if len(row) > 1 else ""
        if not row_id:
            continue
        values_by_row_id[row_id] = row[column_index] if column_index < len(row) else None
    return values_by_row_id


def _resolve_period_refreshed_at(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    date_columns: list[str],
) -> str:
    refreshed_values = [
        runtime.load_sheet_vitrina_refresh_status(as_of_date=snapshot_date).refreshed_at
        for snapshot_date in date_columns
    ]
    return max(refreshed_values)


def _build_period_temporal_slot(snapshot_date: str):
    from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1TemporalSlot

    return SheetVitrinaV1TemporalSlot(
        slot_key=f"period_window:{snapshot_date}",
        slot_label=snapshot_date,
        column_date=snapshot_date,
    )


def _require_data_sheet(snapshot: SheetVitrinaV1Envelope) -> SheetVitrinaWriteTarget:
    data_sheet = next((item for item in snapshot.sheets if item.sheet_name == WEB_VITRINA_SOURCE_SHEET_NAME), None)
    if data_sheet is None:
        raise ValueError(f"ready snapshot {snapshot.as_of_date} does not contain {WEB_VITRINA_SOURCE_SHEET_NAME}")
    return data_sheet


def _normalize_rows(
    rows: list[list[Any]],
    *,
    date_columns: list[str],
    config_by_nm_id: Mapping[int, ConfigV2Item],
    metrics_by_key: Mapping[str, MetricV2Item],
) -> list[WebVitrinaContractRow]:
    normalized: list[WebVitrinaContractRow] = []
    for row_order, row in enumerate(rows, start=1):
        if len(row) < 2:
            continue
        row_id = str(row[1] or "").strip()
        if not row_id or "|" not in row_id:
            continue
        scope_token, metric_key = row_id.split("|", 1)
        metric = metrics_by_key.get(metric_key)
        scope = _parse_scope(scope_token, row_label=str(row[0] or ""), config_by_nm_id=config_by_nm_id)
        values_by_date = {
            column_date: row[index]
            for index, column_date in enumerate(date_columns, start=2)
        }
        normalized.append(
            WebVitrinaContractRow(
                row_id=row_id,
                row_order=row_order,
                scope_kind=scope.scope_kind,
                scope_key=scope.scope_key,
                scope_label=scope.scope_label,
                metric_key=metric_key,
                metric_label=metric.label_ru if metric is not None else metric_key,
                section=metric.section if metric is not None else "",
                group=scope.group,
                nm_id=scope.nm_id,
                format=metric.format if metric is not None else None,
                values_by_date=values_by_date,
            )
        )
    return normalized


def _parse_scope(
    scope_token: str,
    *,
    row_label: str,
    config_by_nm_id: Mapping[int, ConfigV2Item],
) -> _ScopeDescriptor:
    if scope_token == "TOTAL":
        return _ScopeDescriptor(
            scope_kind="TOTAL",
            scope_key="TOTAL",
            scope_label="ИТОГО",
            group=None,
            nm_id=None,
        )

    if scope_token.startswith("GROUP:"):
        group_name = scope_token.split(":", 1)[1]
        return _ScopeDescriptor(
            scope_kind="GROUP",
            scope_key=scope_token,
            scope_label=group_name,
            group=group_name,
            nm_id=None,
        )

    if scope_token.startswith("SKU:"):
        raw_nm_id = scope_token.split(":", 1)[1]
        nm_id = None
        try:
            nm_id = int(raw_nm_id)
        except ValueError:
            nm_id = None
        config_item = config_by_nm_id.get(nm_id) if nm_id is not None else None
        return _ScopeDescriptor(
            scope_kind="SKU",
            scope_key=scope_token,
            scope_label=(config_item.display_name if config_item is not None else _label_prefix(row_label)),
            group=(config_item.group if config_item is not None else None),
            nm_id=nm_id,
        )

    return _ScopeDescriptor(
        scope_kind="OTHER",
        scope_key=scope_token,
        scope_label=_label_prefix(row_label),
        group=None,
        nm_id=None,
    )


def _build_schema(snapshot: SheetVitrinaV1Envelope) -> WebVitrinaContractSchema:
    temporal_slot_by_date = {
        slot.column_date: slot.slot_key
        for slot in snapshot.temporal_slots
    }
    columns = [
        WebVitrinaContractSchemaColumn(
            column_id="row_order",
            label="Row order",
            kind="identity",
            value_type="integer",
            sortable=True,
            filterable=False,
        ),
        WebVitrinaContractSchemaColumn(
            column_id="scope_kind",
            label="Scope kind",
            kind="dimension",
            value_type="string",
            sortable=True,
            filterable=True,
        ),
        WebVitrinaContractSchemaColumn(
            column_id="scope_key",
            label="Scope key",
            kind="dimension",
            value_type="string",
            sortable=True,
            filterable=True,
        ),
        WebVitrinaContractSchemaColumn(
            column_id="scope_label",
            label="Scope label",
            kind="dimension",
            value_type="string",
            sortable=True,
            filterable=True,
        ),
        WebVitrinaContractSchemaColumn(
            column_id="group",
            label="Group",
            kind="dimension",
            value_type="string_or_null",
            sortable=True,
            filterable=True,
        ),
        WebVitrinaContractSchemaColumn(
            column_id="nm_id",
            label="nmId",
            kind="dimension",
            value_type="integer_or_null",
            sortable=True,
            filterable=True,
        ),
        WebVitrinaContractSchemaColumn(
            column_id="metric_key",
            label="Metric key",
            kind="dimension",
            value_type="string",
            sortable=True,
            filterable=True,
        ),
        WebVitrinaContractSchemaColumn(
            column_id="metric_label",
            label="Metric label",
            kind="dimension",
            value_type="string",
            sortable=True,
            filterable=True,
        ),
        WebVitrinaContractSchemaColumn(
            column_id="section",
            label="Section",
            kind="dimension",
            value_type="string",
            sortable=True,
            filterable=True,
        ),
    ]
    columns.extend(
        WebVitrinaContractSchemaColumn(
            column_id=f"date:{column_date}",
            label=column_date,
            kind="temporal_value",
            value_type="number_or_blank",
            sortable=True,
            filterable=False,
            column_date=column_date,
            temporal_slot_key=temporal_slot_by_date.get(column_date),
        )
        for column_date in snapshot.date_columns
    )

    filters = [
        WebVitrinaContractSchemaFilter(
            filter_id="scope_kind",
            field="scope_kind",
            label="Scope kind",
            operators=["eq", "in"],
        ),
        WebVitrinaContractSchemaFilter(
            filter_id="group",
            field="group",
            label="Group",
            operators=["eq", "in"],
        ),
        WebVitrinaContractSchemaFilter(
            filter_id="nm_id",
            field="nm_id",
            label="nmId",
            operators=["eq", "in"],
        ),
        WebVitrinaContractSchemaFilter(
            filter_id="section",
            field="section",
            label="Section",
            operators=["eq", "in"],
        ),
        WebVitrinaContractSchemaFilter(
            filter_id="metric_key",
            field="metric_key",
            label="Metric key",
            operators=["eq", "in"],
        ),
    ]

    sorts = [
        WebVitrinaContractSchemaSort(
            sort_id="row_order",
            field="row_order",
            label="Row order",
            directions=["asc", "desc"],
            default_direction="asc",
        ),
        WebVitrinaContractSchemaSort(
            sort_id="scope_label",
            field="scope_label",
            label="Scope label",
            directions=["asc", "desc"],
        ),
        WebVitrinaContractSchemaSort(
            sort_id="metric_label",
            field="metric_label",
            label="Metric label",
            directions=["asc", "desc"],
        ),
    ]
    sorts.extend(
        WebVitrinaContractSchemaSort(
            sort_id=f"date:{column_date}",
            field=f"date:{column_date}",
            label=column_date,
            directions=["asc", "desc"],
        )
        for column_date in snapshot.date_columns
    )

    return WebVitrinaContractSchema(
        row_identity_fields=["row_id"],
        columns=columns,
        filters=filters,
        sorts=sorts,
    )


def _count_values(items: Mapping[str, str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in items.values():
        counts[value] = counts.get(value, 0) + 1
    return counts


def _label_prefix(value: str) -> str:
    return str(value).split(": ", 1)[0] if ": " in str(value) else str(value)


def _to_utc_timestamp(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
