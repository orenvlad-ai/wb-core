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
class SheetVitrinaV1Envelope:
    plan_version: str
    snapshot_id: str
    as_of_date: str
    sheets: list[SheetVitrinaWriteTarget]


@dataclass(frozen=True)
class SheetVitrinaV1RefreshResult:
    status: str
    bundle_version: str
    activated_at: str
    refreshed_at: str
    as_of_date: str
    snapshot_id: str
    plan_version: str
    sheet_row_counts: dict[str, int]
