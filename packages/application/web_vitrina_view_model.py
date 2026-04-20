"""Map stable web_vitrina_contract v1 into a library-agnostic view_model."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Mapping

from packages.contracts.web_vitrina_contract import WebVitrinaContractV1
from packages.contracts.web_vitrina_view_model import (
    WebVitrinaViewModelCell,
    WebVitrinaViewModelColumn,
    WebVitrinaViewModelFilter,
    WebVitrinaViewModelFormatter,
    WebVitrinaViewModelGroup,
    WebVitrinaViewModelMeta,
    WebVitrinaViewModelRow,
    WebVitrinaViewModelSection,
    WebVitrinaViewModelSort,
    WebVitrinaViewModelStateDescriptor,
    WebVitrinaViewModelStateModel,
    WebVitrinaViewModelV1,
)

WEB_VITRINA_VIEW_MODEL_NAME = "web_vitrina_view_model"
WEB_VITRINA_VIEW_MODEL_VERSION = "v1"
WEB_VITRINA_STATE_NAMESPACE = "web_vitrina.view_model"

_DIMENSION_VALUE_KEYS = {
    "row_order": "row_order",
    "scope_kind": "scope_kind",
    "scope_key": "scope_key",
    "scope_label": "scope_label",
    "group": "group",
    "nm_id": "nm_id",
    "metric_key": "metric_key",
    "metric_label": "metric_label",
    "section": "section",
}

_FORMATTER_LIBRARY: dict[str, WebVitrinaViewModelFormatter] = {
    "text_default": WebVitrinaViewModelFormatter(
        formatter_id="text_default",
        cell_kind="text",
        rule_kind="text",
        decimals=None,
        thousands_separator=False,
    ),
    "number_default": WebVitrinaViewModelFormatter(
        formatter_id="number_default",
        cell_kind="number",
        rule_kind="number",
        decimals=0,
        thousands_separator=True,
    ),
    "money_rub": WebVitrinaViewModelFormatter(
        formatter_id="money_rub",
        cell_kind="money",
        rule_kind="money",
        decimals=0,
        thousands_separator=True,
        suffix=" ₽",
    ),
    "percent_default": WebVitrinaViewModelFormatter(
        formatter_id="percent_default",
        cell_kind="percent",
        rule_kind="percent",
        decimals=2,
        thousands_separator=False,
        suffix="%",
    ),
    "badge_default": WebVitrinaViewModelFormatter(
        formatter_id="badge_default",
        cell_kind="badge",
        rule_kind="badge",
        decimals=None,
        thousands_separator=False,
    ),
    "empty_default": WebVitrinaViewModelFormatter(
        formatter_id="empty_default",
        cell_kind="empty",
        rule_kind="empty",
        decimals=None,
        thousands_separator=False,
        null_display="—",
    ),
    "unknown_default": WebVitrinaViewModelFormatter(
        formatter_id="unknown_default",
        cell_kind="unknown",
        rule_kind="unknown",
        decimals=None,
        thousands_separator=False,
        null_display="—",
    ),
}


class WebVitrinaViewModelMapper:
    """Project a stable web_vitrina_contract into a thin presentation-domain schema."""

    def build(self, contract: WebVitrinaContractV1 | Mapping[str, Any]) -> WebVitrinaViewModelV1:
        payload = _to_contract_payload(contract)
        rows_payload = list(payload.get("rows") or [])

        sections, section_labels = _build_sections(rows_payload)
        groups, group_labels = _build_groups(rows_payload)
        columns = _build_columns(payload)
        filters = _build_filters(payload, columns)
        sorts = _build_sorts(payload)
        rows = _build_rows(
            rows_payload,
            columns=columns,
            section_labels=section_labels,
            group_labels=group_labels,
        )
        formatters = _build_formatters(rows)
        current_state = "empty" if not rows else "ready"

        return WebVitrinaViewModelV1(
            view_model_name=WEB_VITRINA_VIEW_MODEL_NAME,
            view_model_version=WEB_VITRINA_VIEW_MODEL_VERSION,
            meta=WebVitrinaViewModelMeta(
                snapshot_id=str(payload["meta"]["snapshot_id"]),
                as_of_date=str(payload["meta"]["as_of_date"]),
                business_timezone=str(payload["meta"]["business_timezone"]),
                source_contract_name=str(payload["contract_name"]),
                source_contract_version=str(payload["contract_version"]),
                generated_at=str(payload["meta"]["generated_at"]),
                row_count=len(rows),
                column_count=len(columns),
                group_count=len(groups),
                section_count=len(sections),
            ),
            columns=columns,
            sections=sections,
            groups=groups,
            rows=rows,
            filters=filters,
            sorts=sorts,
            formatters=formatters,
            state_model=WebVitrinaViewModelStateModel(
                namespace=WEB_VITRINA_STATE_NAMESPACE,
                current_state=current_state,
                available_states=[
                    WebVitrinaViewModelStateDescriptor(
                        state_id="ready",
                        label="Ready",
                        message="Contract rows are available for adapter composition.",
                    ),
                    WebVitrinaViewModelStateDescriptor(
                        state_id="empty",
                        label="Empty",
                        message="The contract was accepted, but no rows are available to render.",
                    ),
                    WebVitrinaViewModelStateDescriptor(
                        state_id="loading",
                        label="Loading",
                        message="A future page shell may resolve the contract before mapping rows.",
                    ),
                    WebVitrinaViewModelStateDescriptor(
                        state_id="error",
                        label="Error",
                        message="Page composition may expose contract/load failures without coupling to grid internals.",
                    ),
                ],
            ),
        )


def build_web_vitrina_view_model(contract: WebVitrinaContractV1 | Mapping[str, Any]) -> WebVitrinaViewModelV1:
    return WebVitrinaViewModelMapper().build(contract)


def _to_contract_payload(contract: WebVitrinaContractV1 | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(contract, Mapping):
        return dict(contract)
    if is_dataclass(contract):
        return asdict(contract)
    raise TypeError(f"unsupported web_vitrina contract input: {type(contract)!r}")


def _build_columns(payload: Mapping[str, Any]) -> list[WebVitrinaViewModelColumn]:
    filter_by_field = {
        str(item["field"]): str(item["filter_id"])
        for item in payload["schema"]["filters"]
    }
    sort_by_field = {
        str(item["field"]): str(item["sort_id"])
        for item in payload["schema"]["sorts"]
    }
    columns: list[WebVitrinaViewModelColumn] = []
    for item in payload["schema"]["columns"]:
        column_id = str(item["column_id"])
        columns.append(
            WebVitrinaViewModelColumn(
                id=column_id,
                label=str(item["label"]),
                kind=_map_column_kind(str(item["kind"])),
                value_type=str(item["value_type"]),
                align=_column_align(str(item["value_type"]), column_id=column_id),
                sticky=_column_sticky(column_id),
                width_hint=_column_width_hint(column_id),
                sortable=bool(item["sortable"]),
                filterable=bool(item["filterable"]),
                sort_key=sort_by_field.get(column_id),
                filter_key=filter_by_field.get(column_id),
            )
        )
    return columns


def _build_sections(rows_payload: list[Mapping[str, Any]]) -> tuple[list[WebVitrinaViewModelSection], dict[str, str]]:
    sections: list[WebVitrinaViewModelSection] = []
    labels_to_ids: dict[str, str] = {}
    for row in rows_payload:
        label = _section_label(row)
        if label in labels_to_ids:
            continue
        section_id = _section_id(label)
        labels_to_ids[label] = section_id
        sections.append(
            WebVitrinaViewModelSection(
                section_id=section_id,
                label=label,
                order=len(sections) + 1,
                collapsed_by_default=False,
            )
        )
    if not sections:
        label = "Без секции"
        section_id = _section_id(label)
        labels_to_ids[label] = section_id
        sections.append(
            WebVitrinaViewModelSection(
                section_id=section_id,
                label=label,
                order=1,
                collapsed_by_default=False,
            )
        )
    return sections, labels_to_ids


def _build_groups(rows_payload: list[Mapping[str, Any]]) -> tuple[list[WebVitrinaViewModelGroup], dict[str, str]]:
    groups: list[WebVitrinaViewModelGroup] = []
    labels_to_ids: dict[str, str] = {}
    for row in rows_payload:
        label = _group_label(row)
        if label in labels_to_ids:
            continue
        group_id = _group_id(label)
        labels_to_ids[label] = group_id
        groups.append(
            WebVitrinaViewModelGroup(
                group_id=group_id,
                label=label,
                order=len(groups) + 1,
                collapsed_by_default=False,
            )
        )
    if not groups:
        label = "Без группы"
        group_id = _group_id(label)
        labels_to_ids[label] = group_id
        groups.append(
            WebVitrinaViewModelGroup(
                group_id=group_id,
                label=label,
                order=1,
                collapsed_by_default=False,
            )
        )
    return groups, labels_to_ids


def _build_filters(
    payload: Mapping[str, Any],
    columns: list[WebVitrinaViewModelColumn],
) -> list[WebVitrinaViewModelFilter]:
    value_type_by_field = {
        column.id: column.value_type
        for column in columns
    }
    filters: list[WebVitrinaViewModelFilter] = []
    for item in payload["schema"]["filters"]:
        operators = [str(operator) for operator in item["operators"]]
        filters.append(
            WebVitrinaViewModelFilter(
                filter_id=str(item["filter_id"]),
                field=str(item["field"]),
                label=str(item["label"]),
                operators=operators,
                value_type=value_type_by_field.get(str(item["field"]), "string"),
                multi_value="in" in operators,
            )
        )
    return filters


def _build_sorts(payload: Mapping[str, Any]) -> list[WebVitrinaViewModelSort]:
    return [
        WebVitrinaViewModelSort(
            sort_id=str(item["sort_id"]),
            field=str(item["field"]),
            label=str(item["label"]),
            directions=[str(direction) for direction in item["directions"]],
            default_direction=(
                str(item["default_direction"])
                if item.get("default_direction") is not None
                else None
            ),
        )
        for item in payload["schema"]["sorts"]
    ]


def _build_rows(
    rows_payload: list[Mapping[str, Any]],
    *,
    columns: list[WebVitrinaViewModelColumn],
    section_labels: Mapping[str, str],
    group_labels: Mapping[str, str],
) -> list[WebVitrinaViewModelRow]:
    rows: list[WebVitrinaViewModelRow] = []
    presentation_row_order_by_id = _build_presentation_row_order(rows_payload)
    ordered_rows_payload = sorted(
        rows_payload,
        key=lambda row: presentation_row_order_by_id[str(row["row_id"])],
    )
    for row in ordered_rows_payload:
        row_id = str(row["row_id"])
        presentation_row = dict(row)
        presentation_row["row_order"] = presentation_row_order_by_id[row_id]
        section_label = _section_label(row)
        group_label = _group_label(row)
        cells = [
            _build_cell(column, presentation_row)
            for column in columns
        ]
        search_terms = [
            str(presentation_row.get("scope_label") or ""),
            str(presentation_row.get("metric_label") or ""),
            str(presentation_row.get("group") or ""),
            str(presentation_row.get("section") or ""),
            str(presentation_row.get("nm_id") or ""),
            *(cell.display_text for cell in cells if cell.display_text not in {"", "—"}),
        ]
        rows.append(
            WebVitrinaViewModelRow(
                row_id=row_id,
                row_kind=str(presentation_row.get("scope_kind") or "OTHER").lower(),
                section_id=section_labels[section_label],
                group_id=group_labels[group_label],
                cells=cells,
                search_text=" ".join(term for term in search_terms if term),
                filter_tokens=_build_filter_tokens(
                    presentation_row,
                    group_id=group_labels[group_label],
                    section_id=section_labels[section_label],
                ),
            )
        )
    return rows


def _build_presentation_row_order(rows_payload: list[Mapping[str, Any]]) -> dict[str, int]:
    if not rows_payload:
        return {}

    metric_order: dict[str, int] = {}
    group_order: dict[str, int] = {}
    sku_order_by_group: dict[str, dict[str, int]] = {}

    for row in rows_payload:
        metric_key = str(row.get("metric_key") or "")
        if metric_key and metric_key not in metric_order:
            metric_order[metric_key] = len(metric_order) + 1

        group_label = _group_label(row)
        if group_label not in group_order:
            group_order[group_label] = len(group_order) + 1

        scope_kind = str(row.get("scope_kind") or "OTHER")
        if scope_kind != "SKU":
            continue
        sku_key = _sku_identity(row)
        if not sku_key:
            continue
        sku_order = sku_order_by_group.setdefault(group_label, {})
        if sku_key not in sku_order:
            sku_order[sku_key] = len(sku_order) + 1

    def presentation_key(row: Mapping[str, Any]) -> tuple[int, int, int, int, int]:
        scope_kind = str(row.get("scope_kind") or "OTHER")
        metric_rank = metric_order.get(str(row.get("metric_key") or ""), len(metric_order) + 1)
        raw_row_order = int(row.get("row_order") or 0)
        group_label = _group_label(row)
        group_rank = group_order.get(group_label, len(group_order) + 1)

        if scope_kind == "TOTAL":
            return (0, 0, 0, metric_rank, raw_row_order)
        if scope_kind == "GROUP":
            return (1, group_rank, 0, metric_rank, raw_row_order)
        if scope_kind == "SKU":
            sku_rank = sku_order_by_group.get(group_label, {}).get(_sku_identity(row), 0)
            return (2, group_rank, sku_rank, metric_rank, raw_row_order)
        return (3, group_rank, 0, metric_rank, raw_row_order)

    ordered_rows = sorted(rows_payload, key=presentation_key)
    return {
        str(row["row_id"]): index
        for index, row in enumerate(ordered_rows, start=1)
    }


def _build_cell(column: WebVitrinaViewModelColumn, row: Mapping[str, Any]) -> WebVitrinaViewModelCell:
    value = _row_value(row, column.id)
    row_format = str(row.get("format") or "")
    cell_kind, formatter_id = _resolve_cell_kind_and_formatter(
        column_id=column.id,
        value=value,
        row_format=row_format,
    )
    return WebVitrinaViewModelCell(
        column_id=column.id,
        cell_kind=cell_kind,
        value_type=column.value_type,
        value=value,
        display_text=_display_text(value),
        formatter_id=formatter_id,
    )


def _build_filter_tokens(row: Mapping[str, Any], *, group_id: str, section_id: str) -> dict[str, list[str]]:
    tokens = {
        "scope_kind": [str(row.get("scope_kind") or "")],
        "group": ([str(row["group"])] if row.get("group") not in {None, ""} else []),
        "nm_id": ([str(row["nm_id"])] if row.get("nm_id") not in {None, ""} else []),
        "section": ([str(row["section"])] if row.get("section") not in {None, ""} else []),
        "metric_key": [str(row.get("metric_key") or "")],
        "row_kind": [str(row.get("scope_kind") or "OTHER").lower()],
        "group_id": [group_id],
        "section_id": [section_id],
    }
    return {
        key: [value for value in values if value]
        for key, values in tokens.items()
    }


def _build_formatters(rows: list[WebVitrinaViewModelRow]) -> list[WebVitrinaViewModelFormatter]:
    used_ids = {
        cell.formatter_id
        for row in rows
        for cell in row.cells
        if cell.formatter_id is not None
    }
    if not used_ids:
        used_ids = {"text_default", "empty_default"}
    return [
        _FORMATTER_LIBRARY[formatter_id]
        for formatter_id in sorted(used_ids)
    ]


def _row_value(row: Mapping[str, Any], column_id: str) -> Any:
    if column_id.startswith("date:"):
        return (row.get("values_by_date") or {}).get(column_id.split(":", 1)[1])
    return row.get(_DIMENSION_VALUE_KEYS[column_id])


def _map_column_kind(kind: str) -> str:
    if kind == "temporal_value":
        return "temporal_measure"
    return kind


def _column_align(value_type: str, *, column_id: str) -> str:
    if column_id in {"scope_kind", "section"}:
        return "center"
    if value_type.startswith("integer") or value_type.startswith("number") or value_type == "decimal":
        return "end"
    return "start"


def _column_sticky(column_id: str) -> str:
    if column_id in {"row_order", "scope_label", "metric_label"}:
        return "left"
    return "none"


def _column_width_hint(column_id: str) -> int | None:
    if column_id == "row_order":
        return 96
    if column_id == "scope_label":
        return 280
    if column_id == "metric_label":
        return 220
    if column_id.startswith("date:"):
        return 120
    if column_id in {"scope_key", "metric_key"}:
        return 180
    if column_id in {"scope_kind", "section"}:
        return 132
    if column_id in {"group", "nm_id"}:
        return 160
    return 180


def _resolve_cell_kind_and_formatter(
    *,
    column_id: str,
    value: Any,
    row_format: str,
) -> tuple[str, str | None]:
    if value == "":
        return "empty", "empty_default"
    if value is None:
        return "unknown", "unknown_default"
    if column_id in {"scope_kind", "section"}:
        return "badge", "badge_default"
    if column_id.startswith("date:"):
        if row_format == "rub":
            return "money", "money_rub"
        if row_format == "percent":
            return "percent", "percent_default"
        return "number", "number_default"
    if column_id in {"row_order", "nm_id"}:
        return "number", "number_default"
    return "text", "text_default"


def _display_text(value: Any) -> str:
    if value == "" or value is None:
        return "—"
    return str(value)


def _group_label(row: Mapping[str, Any]) -> str:
    scope_kind = str(row.get("scope_kind") or "")
    if scope_kind == "TOTAL":
        return "ИТОГО"
    if row.get("group") not in {None, ""}:
        return str(row["group"])
    return "Без группы"


def _group_id(label: str) -> str:
    if label == "ИТОГО":
        return "group:overview"
    if label == "Без группы":
        return "group:ungrouped"
    return f"group:{label}"


def _section_label(row: Mapping[str, Any]) -> str:
    value = str(row.get("section") or "").strip()
    return value or "Без секции"


def _section_id(label: str) -> str:
    if label == "Без секции":
        return "section:unsectioned"
    return f"section:{label}"


def _sku_identity(row: Mapping[str, Any]) -> str:
    if row.get("nm_id") not in {None, ""}:
        return str(row["nm_id"])
    scope_key = str(row.get("scope_key") or "")
    if scope_key:
        return scope_key
    return str(row.get("scope_label") or "")
