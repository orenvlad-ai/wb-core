"""Контракты sheet-side scaffold для vitrina v1."""

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class SheetVitrinaV1Request:
    bundle_type: str
    scenario: Literal["normal"] = "normal"


@dataclass(frozen=True)
class SheetVitrinaWriteTarget:
    sheet_name: str
    write_start_cell: str
    write_rect: str
    clear_range: str
    write_mode: str
    partial_update_allowed: bool
    header: list[str]
    rows: list[list[Any]]
    row_count: int
    column_count: int


@dataclass(frozen=True)
class SheetVitrinaV1TemporalSlot:
    slot_key: str
    slot_label: str
    column_date: str


@dataclass(frozen=True)
class SheetVitrinaV1Envelope:
    plan_version: str
    snapshot_id: str
    as_of_date: str
    date_columns: list[str]
    temporal_slots: list[SheetVitrinaV1TemporalSlot]
    source_temporal_policies: dict[str, str]
    sheets: list[SheetVitrinaWriteTarget]


@dataclass(frozen=True)
class SheetVitrinaV1RefreshResult:
    status: str
    bundle_version: str
    activated_at: str
    refreshed_at: str
    as_of_date: str
    date_columns: list[str]
    temporal_slots: list[SheetVitrinaV1TemporalSlot]
    source_temporal_policies: dict[str, str]
    snapshot_id: str
    plan_version: str
    sheet_row_counts: dict[str, int]


@dataclass(frozen=True)
class SheetVitrinaV1AutoUpdateState:
    last_run_started_at: str | None = None
    last_run_finished_at: str | None = None
    last_run_status: str | None = None
    last_run_error: str | None = None
    last_run_snapshot_id: str | None = None
    last_run_as_of_date: str | None = None
    last_run_refreshed_at: str | None = None
    last_successful_auto_update_at: str | None = None
