"""Library-agnostic presentation-domain view_model for web-vitrina."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WebVitrinaViewModelMeta:
    snapshot_id: str
    as_of_date: str
    business_timezone: str
    source_contract_name: str
    source_contract_version: str
    generated_at: str
    row_count: int
    column_count: int
    group_count: int
    section_count: int


@dataclass(frozen=True)
class WebVitrinaViewModelColumn:
    id: str
    label: str
    kind: str
    value_type: str
    align: str
    sticky: str
    width_hint: int | None
    sortable: bool
    filterable: bool
    sort_key: str | None
    filter_key: str | None


@dataclass(frozen=True)
class WebVitrinaViewModelGroup:
    group_id: str
    label: str
    order: int
    collapsed_by_default: bool


@dataclass(frozen=True)
class WebVitrinaViewModelSection:
    section_id: str
    label: str
    order: int
    collapsed_by_default: bool


@dataclass(frozen=True)
class WebVitrinaViewModelCell:
    column_id: str
    cell_kind: str
    value_type: str
    value: Any
    display_text: str
    formatter_id: str | None = None


@dataclass(frozen=True)
class WebVitrinaViewModelRow:
    row_id: str
    row_kind: str
    section_id: str
    group_id: str
    cells: list[WebVitrinaViewModelCell]
    search_text: str
    filter_tokens: dict[str, list[str]]


@dataclass(frozen=True)
class WebVitrinaViewModelFilter:
    filter_id: str
    field: str
    label: str
    operators: list[str]
    value_type: str
    multi_value: bool


@dataclass(frozen=True)
class WebVitrinaViewModelSort:
    sort_id: str
    field: str
    label: str
    directions: list[str]
    default_direction: str | None = None


@dataclass(frozen=True)
class WebVitrinaViewModelFormatter:
    formatter_id: str
    cell_kind: str
    rule_kind: str
    decimals: int | None
    thousands_separator: bool
    prefix: str | None = None
    suffix: str | None = None
    null_display: str = "—"
    date_pattern: str | None = None
    value_multiplier: float | None = None


@dataclass(frozen=True)
class WebVitrinaViewModelStateDescriptor:
    state_id: str
    label: str
    message: str


@dataclass(frozen=True)
class WebVitrinaViewModelStateModel:
    namespace: str
    current_state: str
    available_states: list[WebVitrinaViewModelStateDescriptor]


@dataclass(frozen=True)
class WebVitrinaViewModelV1:
    view_model_name: str
    view_model_version: str
    meta: WebVitrinaViewModelMeta
    columns: list[WebVitrinaViewModelColumn]
    sections: list[WebVitrinaViewModelSection]
    groups: list[WebVitrinaViewModelGroup]
    rows: list[WebVitrinaViewModelRow]
    filters: list[WebVitrinaViewModelFilter]
    sorts: list[WebVitrinaViewModelSort]
    formatters: list[WebVitrinaViewModelFormatter]
    state_model: WebVitrinaViewModelStateModel
