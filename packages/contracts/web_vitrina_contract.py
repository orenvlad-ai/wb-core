"""Stable server-owned contract for the phase-1 web-vitrina read side."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1TemporalSlot


@dataclass(frozen=True)
class WebVitrinaContractMeta:
    snapshot_id: str
    bundle_version: str
    as_of_date: str
    business_timezone: str
    date_columns: list[str]
    temporal_slots: list[SheetVitrinaV1TemporalSlot]
    generated_at: str
    refreshed_at: str
    row_count: int


@dataclass(frozen=True)
class WebVitrinaContractStatusSummary:
    refresh_status: str
    read_model: str
    source_sheet_name: str
    bundle_version: str
    activated_at: str
    refreshed_at: str
    business_now: str
    current_business_date: str
    default_as_of_date: str
    last_auto_run_status: str
    last_auto_run_started_at: str | None
    last_auto_run_finished_at: str | None
    last_successful_auto_update_at: str | None
    last_successful_manual_refresh_at: str | None
    last_successful_manual_load_at: str | None
    source_policy_counts: dict[str, int]
    source_count: int
    data_sheet_row_count: int


@dataclass(frozen=True)
class WebVitrinaContractSchemaColumn:
    column_id: str
    label: str
    kind: str
    value_type: str
    sortable: bool
    filterable: bool
    column_date: str | None = None
    temporal_slot_key: str | None = None


@dataclass(frozen=True)
class WebVitrinaContractSchemaFilter:
    filter_id: str
    field: str
    label: str
    operators: list[str]


@dataclass(frozen=True)
class WebVitrinaContractSchemaSort:
    sort_id: str
    field: str
    label: str
    directions: list[str]
    default_direction: str | None = None


@dataclass(frozen=True)
class WebVitrinaContractSchema:
    row_identity_fields: list[str]
    columns: list[WebVitrinaContractSchemaColumn]
    filters: list[WebVitrinaContractSchemaFilter]
    sorts: list[WebVitrinaContractSchemaSort]


@dataclass(frozen=True)
class WebVitrinaContractRow:
    row_id: str
    row_order: int
    scope_kind: str
    scope_key: str
    scope_label: str
    metric_key: str
    metric_label: str
    section: str
    group: str | None
    nm_id: int | None
    format: str | None
    values_by_date: dict[str, Any]


@dataclass(frozen=True)
class WebVitrinaContractCapabilities:
    sortable: bool
    filterable: bool
    exportable: bool
    read_only: bool
    grid_library_agnostic: bool
    thin_page_shell: bool


@dataclass(frozen=True)
class WebVitrinaContractV1:
    contract_name: str
    contract_version: str
    page_route: str
    read_route: str
    meta: WebVitrinaContractMeta
    status_summary: WebVitrinaContractStatusSummary
    schema: WebVitrinaContractSchema
    rows: list[WebVitrinaContractRow]
    capabilities: WebVitrinaContractCapabilities
