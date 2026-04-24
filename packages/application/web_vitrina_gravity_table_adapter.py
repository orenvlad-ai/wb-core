"""Build Gravity-table-specific config/data/render hints from web_vitrina_view_model."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Mapping

from packages.contracts.web_vitrina_gravity_table_adapter import (
    WebVitrinaGravityTableAdapterMeta,
    WebVitrinaGravityTableAdapterV1,
    WebVitrinaGravityTableCellValue,
    WebVitrinaGravityTableColumn,
    WebVitrinaGravityTableColumnMeta,
    WebVitrinaGravityTableFilterBinding,
    WebVitrinaGravityTableGrouping,
    WebVitrinaGravityTableProps,
    WebVitrinaGravityTableRenderer,
    WebVitrinaGravityTableRow,
    WebVitrinaGravityTableSortBinding,
    WebVitrinaGravityTableStateSurface,
    WebVitrinaGravityTableUseTableOptions,
)
from packages.contracts.web_vitrina_view_model import WebVitrinaViewModelV1

WEB_VITRINA_GRAVITY_TABLE_ADAPTER_NAME = "web_vitrina_gravity_table_adapter"
WEB_VITRINA_GRAVITY_TABLE_ADAPTER_VERSION = "v1"
WEB_VITRINA_GRAVITY_TABLE_LIBRARY_NAME = "@gravity-ui/table"
WEB_VITRINA_GRAVITY_TABLE_LIBRARY_SURFACE = "Table/useTable + ColumnDef"


class WebVitrinaGravityTableAdapter:
    """Project a swap-friendly Gravity-specific adapter from a stable view_model seam."""

    def build(
        self,
        view_model: WebVitrinaViewModelV1 | Mapping[str, Any],
    ) -> WebVitrinaGravityTableAdapterV1:
        payload = _to_view_model_payload(view_model)
        renderers = _build_renderer_registry(payload)
        renderer_ids = {renderer.renderer_id for renderer in renderers}
        columns = _build_columns(payload, renderer_ids=renderer_ids)
        rows = _build_rows(payload, columns=columns)
        groupings = _build_groupings(payload)
        filters = _build_filters(payload)
        sorts = _build_sorts(payload)
        current_state = str(payload["state_model"]["current_state"])
        empty_message = _state_message(payload, state_id="empty")
        loading_message = _state_message(payload, state_id="loading")
        error_message = _state_message(payload, state_id="error")

        return WebVitrinaGravityTableAdapterV1(
            adapter_name=WEB_VITRINA_GRAVITY_TABLE_ADAPTER_NAME,
            adapter_version=WEB_VITRINA_GRAVITY_TABLE_ADAPTER_VERSION,
            meta=WebVitrinaGravityTableAdapterMeta(
                snapshot_id=str(payload["meta"]["snapshot_id"]),
                generated_at=str(payload["meta"]["generated_at"]),
                library_name=WEB_VITRINA_GRAVITY_TABLE_LIBRARY_NAME,
                library_surface=WEB_VITRINA_GRAVITY_TABLE_LIBRARY_SURFACE,
                source_view_model_name=str(payload["view_model_name"]),
                source_view_model_version=str(payload["view_model_version"]),
                row_count=len(rows),
                column_count=len(columns),
            ),
            columns=columns,
            rows=rows,
            renderers=renderers,
            groupings=groupings,
            filters=filters,
            sorts=sorts,
            use_table_options=WebVitrinaGravityTableUseTableOptions(
                get_row_id_key="row_id",
                enable_sorting=bool(sorts),
                manual_sorting=True,
                enable_column_filters=bool(filters),
                manual_filtering=True,
                enable_column_resizing=True,
                enable_expanding=False,
                grouping_mode="flat",
            ),
            table_props=WebVitrinaGravityTableProps(
                component="Table",
                with_outer_borders=True,
                empty_message=empty_message,
                loading_message=loading_message,
                error_message=error_message,
            ),
            state_surface=WebVitrinaGravityTableStateSurface(
                current_state=current_state,
                empty_message=empty_message,
                loading_message=loading_message,
                error_message=error_message,
            ),
        )


def build_web_vitrina_gravity_table_adapter(
    view_model: WebVitrinaViewModelV1 | Mapping[str, Any],
) -> WebVitrinaGravityTableAdapterV1:
    return WebVitrinaGravityTableAdapter().build(view_model)


def _to_view_model_payload(view_model: WebVitrinaViewModelV1 | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(view_model, Mapping):
        return dict(view_model)
    if is_dataclass(view_model):
        return asdict(view_model)
    raise TypeError(f"unsupported web_vitrina view_model input: {type(view_model)!r}")


def _build_renderer_registry(payload: Mapping[str, Any]) -> list[WebVitrinaGravityTableRenderer]:
    renderer_specs: dict[str, WebVitrinaGravityTableRenderer] = {}
    formatter_by_id = {
        item["formatter_id"]: item
        for item in payload["formatters"]
    }
    align_by_column = {
        item["id"]: item["align"]
        for item in payload["columns"]
    }
    for row in payload["rows"]:
        for cell in row["cells"]:
            formatter_id = cell.get("formatter_id")
            renderer_id = _renderer_id(
                cell_kind=str(cell["cell_kind"]),
                formatter_id=(str(formatter_id) if formatter_id is not None else None),
            )
            formatter = formatter_by_id.get(formatter_id) if formatter_id is not None else None
            renderer_specs.setdefault(
                renderer_id,
                WebVitrinaGravityTableRenderer(
                    renderer_id=renderer_id,
                    gravity_variant=_gravity_variant(str(cell["cell_kind"])),
                    formatter_id=(str(formatter_id) if formatter_id is not None else None),
                    align=align_by_column.get(str(cell["column_id"]), "start"),
                    placeholder_text=(
                        str(formatter["null_display"])
                        if formatter is not None and formatter.get("null_display") is not None
                        else None
                    ),
                ),
            )
    return list(sorted(renderer_specs.values(), key=lambda item: item.renderer_id))


def _build_columns(
    payload: Mapping[str, Any],
    *,
    renderer_ids: set[str],
) -> list[WebVitrinaGravityTableColumn]:
    filters_by_column = {
        item["field"]: item
        for item in payload["filters"]
    }
    sorts_by_column = {
        item["field"]: item
        for item in payload["sorts"]
    }
    columns: list[WebVitrinaGravityTableColumn] = []
    for item in payload["columns"]:
        renderer_id = _column_renderer_id(item, rows=payload["rows"])
        if renderer_id not in renderer_ids:
            raise ValueError(f"renderer {renderer_id!r} was not materialized for column {item['id']!r}")
        filter_binding = filters_by_column.get(item["id"])
        sort_binding = sorts_by_column.get(item["id"])
        resolved_width = _resolve_column_width(item, rows=payload["rows"])
        columns.append(
            WebVitrinaGravityTableColumn(
                id=str(item["id"]),
                accessor_key=str(item["id"]),
                header=str(item["label"]),
                size=resolved_width,
                min_size=_min_size(resolved_width) if resolved_width is not None else None,
                enable_sorting=bool(item["sortable"]),
                enable_column_filters=bool(item["filterable"]),
                enable_resizing=True,
                meta=WebVitrinaGravityTableColumnMeta(
                    align=str(item["align"]),
                    pin=_pin_value(str(item["sticky"])),
                    default_cell_renderer_id=renderer_id,
                    uses_row_cell_renderers=True,
                    sort_key=(str(sort_binding["sort_id"]) if sort_binding is not None else None),
                    filter_key=(str(filter_binding["filter_id"]) if filter_binding is not None else None),
                    view_column_kind=str(item["kind"]),
                    value_type=str(item["value_type"]),
                    with_nesting_styles=False,
                    show_tree_depth_indicators=False,
                ),
            )
        )
    return columns


def _build_rows(
    payload: Mapping[str, Any],
    *,
    columns: list[WebVitrinaGravityTableColumn],
) -> list[WebVitrinaGravityTableRow]:
    column_by_id = {column.id: column for column in columns}
    rows: list[WebVitrinaGravityTableRow] = []
    for row in payload["rows"]:
        values = {
            str(cell["column_id"]): WebVitrinaGravityTableCellValue(
                value=cell.get("value"),
                display_text=str(cell["display_text"]),
                cell_kind=str(cell["cell_kind"]),
                formatter_id=(str(cell["formatter_id"]) if cell.get("formatter_id") is not None else None),
                renderer_id=_renderer_id(
                    cell_kind=str(cell["cell_kind"]),
                    formatter_id=(str(cell["formatter_id"]) if cell.get("formatter_id") is not None else None),
                ),
            )
            for cell in row["cells"]
        }
        for column_id in values:
            if column_id not in column_by_id:
                raise ValueError(f"row {row['row_id']!r} contains unknown column {column_id!r}")
        rows.append(
            WebVitrinaGravityTableRow(
                row_id=str(row["row_id"]),
                row_kind=str(row["row_kind"]),
                section_id=str(row["section_id"]),
                group_id=str(row["group_id"]),
                depth=0,
                parent_id=None,
                search_text=str(row["search_text"]),
                filter_tokens={
                    str(key): [str(value) for value in values_list]
                    for key, values_list in row["filter_tokens"].items()
                },
                values=values,
            )
        )
    return rows


def _build_groupings(payload: Mapping[str, Any]) -> list[WebVitrinaGravityTableGrouping]:
    group_by_id = {
        item["group_id"]: item
        for item in payload["groups"]
    }
    row_ids_by_group: dict[str, list[str]] = {}
    section_ids_by_group: dict[str, set[str]] = {}
    for row in payload["rows"]:
        group_id = str(row["group_id"])
        row_ids_by_group.setdefault(group_id, []).append(str(row["row_id"]))
        section_ids_by_group.setdefault(group_id, set()).add(str(row["section_id"]))

    groupings: list[WebVitrinaGravityTableGrouping] = []
    for order, group_id in enumerate(sorted(row_ids_by_group, key=lambda key: _group_order(group_by_id, key)), start=1):
        group_label = str(group_by_id[group_id]["label"])
        groupings.append(
            WebVitrinaGravityTableGrouping(
                grouping_id=group_id,
                section_id=_grouping_section_id(section_ids_by_group.get(group_id) or set()),
                group_id=group_id,
                title=group_label,
                order=order,
                row_ids=row_ids_by_group[group_id],
                collapsed_by_default=bool(group_by_id[group_id]["collapsed_by_default"]),
            )
        )
    return groupings


def _build_filters(payload: Mapping[str, Any]) -> list[WebVitrinaGravityTableFilterBinding]:
    return [
        WebVitrinaGravityTableFilterBinding(
            filter_id=str(item["filter_id"]),
            column_id=str(item["field"]),
            operators=[str(operator) for operator in item["operators"]],
            multi_value=bool(item["multi_value"]),
            manual=True,
        )
        for item in payload["filters"]
    ]


def _build_sorts(payload: Mapping[str, Any]) -> list[WebVitrinaGravityTableSortBinding]:
    return [
        WebVitrinaGravityTableSortBinding(
            sort_id=str(item["sort_id"]),
            column_id=str(item["field"]),
            directions=[str(direction) for direction in item["directions"]],
            default_direction=(
                str(item["default_direction"])
                if item.get("default_direction") is not None
                else None
            ),
            manual=True,
        )
        for item in payload["sorts"]
    ]


def _state_message(payload: Mapping[str, Any], *, state_id: str) -> str:
    state = next(
        (
            item
            for item in payload["state_model"]["available_states"]
            if str(item["state_id"]) == state_id
        ),
        None,
    )
    if state is None:
        raise ValueError(f"state {state_id!r} is missing from view_model state_model")
    return str(state["message"])


def _column_renderer_id(column: Mapping[str, Any], *, rows: list[Mapping[str, Any]]) -> str:
    column_id = str(column["id"])
    for row in rows:
        for cell in row["cells"]:
            if str(cell["column_id"]) != column_id:
                continue
            formatter_id = cell.get("formatter_id")
            return _renderer_id(
                cell_kind=str(cell["cell_kind"]),
                formatter_id=(str(formatter_id) if formatter_id is not None else None),
            )
    return _renderer_id(cell_kind="text", formatter_id="text_default")


def _renderer_id(*, cell_kind: str, formatter_id: str | None) -> str:
    if formatter_id is None:
        return f"renderer:{cell_kind}"
    return f"renderer:{cell_kind}:{formatter_id}"


def _gravity_variant(cell_kind: str) -> str:
    if cell_kind == "badge":
        return "label"
    if cell_kind in {"empty", "unknown"}:
        return "placeholder"
    return "text"


def _pin_value(sticky: str) -> str | None:
    if sticky == "left":
        return "left"
    if sticky == "right":
        return "right"
    return None


def _resolve_column_width(
    column: Mapping[str, Any],
    *,
    rows: list[Mapping[str, Any]],
) -> int | None:
    width_hint = column.get("width_hint")
    if width_hint is None:
        return None
    column_id = str(column["id"])
    width_cap = int(width_hint)
    observed_width = _observed_column_width(column, rows=rows)
    return max(_column_floor_width(column_id), min(width_cap, observed_width))


def _observed_column_width(
    column: Mapping[str, Any],
    *,
    rows: list[Mapping[str, Any]],
) -> int:
    column_id = str(column["id"])
    header_text = str(column["label"])
    value_type = str(column.get("value_type") or "")
    max_text_length = _header_measure_length(column_id, header_text)
    for row in rows:
        for cell in row["cells"]:
            if str(cell["column_id"]) != column_id:
                continue
            max_text_length = max(
                max_text_length,
                len(_observed_cell_text(cell, value_type=value_type)),
            )
            break
    return int(max_text_length * _column_char_width(column_id, value_type=value_type) + _column_horizontal_padding(column_id))


def _column_floor_width(column_id: str) -> int:
    if column_id == "row_order":
        return 40
    if column_id.startswith("date:"):
        return 84
    if column_id in {"scope_kind", "section"}:
        return 76
    if column_id == "group":
        return 72
    if column_id == "nm_id":
        return 82
    if column_id == "scope_key":
        return 90
    if column_id == "scope_label":
        return 110
    if column_id == "metric_key":
        return 108
    if column_id == "metric_label":
        return 122
    if column_id == "row_last_updated_at":
        return 112
    return 72


def _min_size(width_hint: int) -> int:
    return max(44, min(width_hint, 84))


def _header_measure_length(column_id: str, header_text: str) -> int:
    header_length = len(header_text)
    if column_id == "row_order":
        return min(header_length, 4)
    if column_id.startswith("date:"):
        return header_length
    if column_id in {"scope_kind", "section"}:
        return min(header_length, 8)
    if column_id in {"group", "nm_id"}:
        return min(header_length, 7)
    if column_id in {"scope_key", "metric_key"}:
        return min(header_length, 12)
    if column_id in {"scope_label", "metric_label"}:
        return min(header_length, 14)
    if column_id == "row_last_updated_at":
        return min(header_length, 12)
    return min(header_length, 12)


def _observed_cell_text(cell: Mapping[str, Any], *, value_type: str) -> str:
    display_text = str(cell.get("display_text") or "")
    number_value = _coerce_number(cell.get("value"))
    if number_value is None:
        return display_text
    cell_kind = str(cell.get("cell_kind") or "")
    if cell_kind == "percent":
        return _estimate_number_text(number_value * 100.0, decimals=2, use_grouping=False, suffix="%")
    if cell_kind == "money":
        return _estimate_number_text(number_value, decimals=0, use_grouping=True, suffix=" ₽")
    if cell_kind == "number" or value_type.startswith("integer") or value_type.startswith("number") or value_type == "decimal":
        return _estimate_number_text(number_value, decimals=0, use_grouping=True, suffix="")
    return display_text


def _coerce_number(value: Any) -> float | None:
    try:
        number_value = float(value)
    except (TypeError, ValueError):
        return None
    return number_value if number_value == number_value and number_value not in {float("inf"), float("-inf")} else None


def _estimate_number_text(
    value: float,
    *,
    decimals: int,
    use_grouping: bool,
    suffix: str,
) -> str:
    number_pattern = f",.{decimals}f" if use_grouping else f".{decimals}f"
    return format(value, number_pattern) + suffix


def _column_char_width(column_id: str, *, value_type: str) -> float:
    if column_id.startswith("date:"):
        return 5.8
    if value_type.startswith("integer") or value_type.startswith("number") or value_type == "decimal":
        return 5.8
    return 5.9


def _column_horizontal_padding(column_id: str) -> int:
    if column_id == "row_order":
        return 10
    if column_id.startswith("date:"):
        return 14
    return 16


def _group_order(group_by_id: Mapping[str, Mapping[str, Any]], group_id: str) -> int:
    return int(group_by_id[group_id]["order"])


def _grouping_section_id(section_ids: set[str]) -> str:
    if not section_ids:
        return "section:unsectioned"
    if len(section_ids) == 1:
        return next(iter(section_ids))
    return "section:mixed"
