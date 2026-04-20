"""Gravity-table-specific adapter payload over the library-agnostic web-vitrina view_model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WebVitrinaGravityTableAdapterMeta:
    snapshot_id: str
    generated_at: str
    library_name: str
    library_surface: str
    source_view_model_name: str
    source_view_model_version: str
    row_count: int
    column_count: int


@dataclass(frozen=True)
class WebVitrinaGravityTableColumnMeta:
    align: str
    pin: str | None
    default_cell_renderer_id: str
    uses_row_cell_renderers: bool
    sort_key: str | None
    filter_key: str | None
    view_column_kind: str
    value_type: str
    with_nesting_styles: bool
    show_tree_depth_indicators: bool


@dataclass(frozen=True)
class WebVitrinaGravityTableColumn:
    id: str
    accessor_key: str
    header: str
    size: int | None
    min_size: int | None
    enable_sorting: bool
    enable_column_filters: bool
    enable_resizing: bool
    meta: WebVitrinaGravityTableColumnMeta


@dataclass(frozen=True)
class WebVitrinaGravityTableCellValue:
    value: Any
    display_text: str
    cell_kind: str
    formatter_id: str | None
    renderer_id: str


@dataclass(frozen=True)
class WebVitrinaGravityTableRow:
    row_id: str
    row_kind: str
    section_id: str
    group_id: str
    depth: int
    parent_id: str | None
    search_text: str
    filter_tokens: dict[str, list[str]]
    values: dict[str, WebVitrinaGravityTableCellValue]


@dataclass(frozen=True)
class WebVitrinaGravityTableRenderer:
    renderer_id: str
    gravity_variant: str
    formatter_id: str | None
    align: str
    placeholder_text: str | None = None


@dataclass(frozen=True)
class WebVitrinaGravityTableGrouping:
    grouping_id: str
    section_id: str
    group_id: str
    title: str
    order: int
    row_ids: list[str]
    collapsed_by_default: bool


@dataclass(frozen=True)
class WebVitrinaGravityTableFilterBinding:
    filter_id: str
    column_id: str
    operators: list[str]
    multi_value: bool
    manual: bool


@dataclass(frozen=True)
class WebVitrinaGravityTableSortBinding:
    sort_id: str
    column_id: str
    directions: list[str]
    default_direction: str | None
    manual: bool


@dataclass(frozen=True)
class WebVitrinaGravityTableUseTableOptions:
    get_row_id_key: str
    enable_sorting: bool
    manual_sorting: bool
    enable_column_filters: bool
    manual_filtering: bool
    enable_column_resizing: bool
    enable_expanding: bool
    grouping_mode: str


@dataclass(frozen=True)
class WebVitrinaGravityTableProps:
    component: str
    with_outer_borders: bool
    empty_message: str
    loading_message: str
    error_message: str


@dataclass(frozen=True)
class WebVitrinaGravityTableStateSurface:
    current_state: str
    empty_message: str
    loading_message: str
    error_message: str


@dataclass(frozen=True)
class WebVitrinaGravityTableAdapterV1:
    adapter_name: str
    adapter_version: str
    meta: WebVitrinaGravityTableAdapterMeta
    columns: list[WebVitrinaGravityTableColumn]
    rows: list[WebVitrinaGravityTableRow]
    renderers: list[WebVitrinaGravityTableRenderer]
    groupings: list[WebVitrinaGravityTableGrouping]
    filters: list[WebVitrinaGravityTableFilterBinding]
    sorts: list[WebVitrinaGravityTableSortBinding]
    use_table_options: WebVitrinaGravityTableUseTableOptions
    table_props: WebVitrinaGravityTableProps
    state_surface: WebVitrinaGravityTableStateSurface
