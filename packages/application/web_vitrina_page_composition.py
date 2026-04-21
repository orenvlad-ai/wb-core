"""Compose a thin server-driven web-vitrina page payload over stable seams."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Mapping

from packages.contracts.web_vitrina_contract import WebVitrinaContractV1
from packages.contracts.web_vitrina_gravity_table_adapter import WebVitrinaGravityTableAdapterV1
from packages.contracts.web_vitrina_view_model import WebVitrinaViewModelV1

WEB_VITRINA_PAGE_COMPOSITION_NAME = "web_vitrina_page_composition"
WEB_VITRINA_PAGE_COMPOSITION_VERSION = "v1"
WEB_VITRINA_PAGE_STATE_NAMESPACE = "wb-core:sheet-vitrina-v1:web-vitrina:page-state:v1"
_ALL_OPTION_VALUE = "__all__"


def build_web_vitrina_page_composition(
    *,
    contract: WebVitrinaContractV1 | Mapping[str, Any],
    view_model: WebVitrinaViewModelV1 | Mapping[str, Any],
    adapter: WebVitrinaGravityTableAdapterV1 | Mapping[str, Any],
    page_route: str,
    read_route: str,
    operator_route: str,
    available_snapshot_dates: list[str],
    selected_as_of_date: str | None,
    selected_date_from: str | None,
    selected_date_to: str | None,
) -> dict[str, Any]:
    contract_payload = _to_payload(contract)
    view_model_payload = _to_payload(view_model)
    adapter_payload = _to_payload(adapter)

    rows = list(adapter_payload.get("rows") or [])
    columns = list(adapter_payload.get("columns") or [])
    current_state = str(adapter_payload["state_surface"]["current_state"])
    date_column_ids = [
        str(column["id"])
        for column in columns
        if str(column["id"]).startswith("date:")
    ]
    section_counts = _count_rows(rows, key="section_id")
    group_counts = _count_rows(rows, key="group_id")
    row_kind_counts = _count_rows(rows, key="row_kind")
    metric_counts = _count_metric_rows(rows)
    column_labels = {
        str(column["id"]): str(column["header"])
        for column in columns
    }

    return {
        "composition_name": WEB_VITRINA_PAGE_COMPOSITION_NAME,
        "composition_version": WEB_VITRINA_PAGE_COMPOSITION_VERSION,
        "meta": {
            "page_title": "Web-витрина",
            "page_route": page_route,
            "read_route": read_route,
            "operator_route": operator_route,
            "snapshot_id": str(contract_payload["meta"]["snapshot_id"]),
            "as_of_date": str(contract_payload["meta"]["as_of_date"]),
            "business_timezone": str(contract_payload["meta"]["business_timezone"]),
            "current_state": current_state,
            "state_message": _resolve_ready_state_message(adapter_payload),
            "source_contract_name": str(contract_payload["contract_name"]),
            "source_contract_version": str(contract_payload["contract_version"]),
            "source_view_model_name": str(view_model_payload["view_model_name"]),
            "source_view_model_version": str(view_model_payload["view_model_version"]),
            "source_adapter_name": str(adapter_payload["adapter_name"]),
            "source_adapter_version": str(adapter_payload["adapter_version"]),
            "grid_library_name": str(adapter_payload["meta"]["library_name"]),
            "state_namespace": WEB_VITRINA_PAGE_STATE_NAMESPACE,
            "browser_state_persistence": "none",
        },
        "historical_access": _build_historical_access(
            page_route=page_route,
            default_as_of_date=str(contract_payload["status_summary"]["default_as_of_date"]),
            available_snapshot_dates=available_snapshot_dates,
            selected_as_of_date=selected_as_of_date,
            selected_date_from=selected_date_from,
            selected_date_to=selected_date_to,
        ),
        "status_badge": {
            "label": _status_status_label(current_state=current_state, refresh_status=str(contract_payload["status_summary"]["refresh_status"])),
            "tone": _status_tone(current_state=current_state, refresh_status=str(contract_payload["status_summary"]["refresh_status"])),
            "detail": (
                f"{contract_payload['status_summary']['read_model']} / "
                f"{contract_payload['status_summary']['source_sheet_name']}"
            ),
        },
        "summary_cards": [
            {
                "card_id": "status",
                "label": "Статус",
                "value": _status_status_label(
                    current_state=current_state,
                    refresh_status=str(contract_payload["status_summary"]["refresh_status"]),
                ),
                "detail": (
                    f"{contract_payload['status_summary']['read_model']} / "
                    f"{contract_payload['status_summary']['source_sheet_name']}"
                ),
                "tone": _status_tone(
                    current_state=current_state,
                    refresh_status=str(contract_payload["status_summary"]["refresh_status"]),
                ),
            },
            {
                "card_id": "rows",
                "label": "Строки",
                "value": str(contract_payload["meta"]["row_count"]),
                "detail": f"date columns: {len(contract_payload['meta']['date_columns'])}",
                "tone": "neutral",
            },
            {
                "card_id": "freshness",
                "label": "Свежесть данных",
                "value": str(contract_payload["meta"]["refreshed_at"]),
                "detail": _freshness_detail(contract_payload),
                "tone": "neutral",
            },
        ],
        "filter_surface": {
            "state_namespace": WEB_VITRINA_PAGE_STATE_NAMESPACE,
            "browser_state_persistence": "none",
            "controls": [
                {
                    "control_id": "search",
                    "kind": "search",
                    "label": "Поиск",
                    "default_value": "",
                    "placeholder": "SKU, metric, group, nmId",
                    "options": [],
                },
                {
                    "control_id": "section",
                    "kind": "select",
                    "label": "Секция",
                    "default_value": _ALL_OPTION_VALUE,
                    "options": _build_labeled_options(
                        all_label="Все секции",
                        items=view_model_payload["sections"],
                        counts=section_counts,
                        id_key="section_id",
                    ),
                },
                {
                    "control_id": "group",
                    "kind": "select",
                    "label": "Группа",
                    "default_value": _ALL_OPTION_VALUE,
                    "options": _build_labeled_options(
                        all_label="Все группы",
                        items=view_model_payload["groups"],
                        counts=group_counts,
                        id_key="group_id",
                    ),
                },
                {
                    "control_id": "scope_kind",
                    "kind": "select",
                    "label": "Scope",
                    "default_value": _ALL_OPTION_VALUE,
                    "options": _build_scope_kind_options(row_kind_counts),
                },
                {
                    "control_id": "metric",
                    "kind": "select",
                    "label": "Метрика",
                    "default_value": _ALL_OPTION_VALUE,
                    "options": _build_metric_options(metric_counts),
                },
            ],
            "sort_options": _build_sort_options(adapter_payload, column_labels=column_labels),
            "default_sort_value": _resolve_default_sort_value(adapter_payload),
            "empty_result_message": "Фильтры не вернули ни одной строки.",
        },
        "table_surface": {
            "columns": columns,
            "rows": rows,
            "groupings": list(adapter_payload.get("groupings") or []),
            "renderers": list(adapter_payload.get("renderers") or []),
            "formatters": list(view_model_payload.get("formatters") or []),
            "sorts": list(adapter_payload.get("sorts") or []),
            "filters": list(adapter_payload.get("filters") or []),
            "state_surface": dict(adapter_payload["state_surface"]),
            "total_row_count": len(rows),
            "date_column_ids": date_column_ids,
            "column_labels": column_labels,
        },
        "status_summary": dict(contract_payload["status_summary"]),
        "capabilities": dict(contract_payload["capabilities"]),
    }


def build_web_vitrina_page_error_composition(
    *,
    page_route: str,
    read_route: str,
    operator_route: str,
    as_of_date: str,
    error_message: str,
    available_snapshot_dates: list[str],
    default_as_of_date: str,
    selected_as_of_date: str | None,
    selected_date_from: str | None,
    selected_date_to: str | None,
) -> dict[str, Any]:
    return {
        "composition_name": WEB_VITRINA_PAGE_COMPOSITION_NAME,
        "composition_version": WEB_VITRINA_PAGE_COMPOSITION_VERSION,
        "meta": {
            "page_title": "Web-витрина",
            "page_route": page_route,
            "read_route": read_route,
            "operator_route": operator_route,
            "snapshot_id": "",
            "as_of_date": as_of_date,
            "business_timezone": "",
            "current_state": "error",
            "state_message": error_message,
            "source_contract_name": "web_vitrina_contract",
            "source_contract_version": "v1",
            "source_view_model_name": "web_vitrina_view_model",
            "source_view_model_version": "v1",
            "source_adapter_name": "web_vitrina_gravity_table_adapter",
            "source_adapter_version": "v1",
            "grid_library_name": "@gravity-ui/table",
            "state_namespace": WEB_VITRINA_PAGE_STATE_NAMESPACE,
            "browser_state_persistence": "none",
        },
        "historical_access": _build_historical_access(
            page_route=page_route,
            default_as_of_date=default_as_of_date,
            available_snapshot_dates=available_snapshot_dates,
            selected_as_of_date=selected_as_of_date,
            selected_date_from=selected_date_from,
            selected_date_to=selected_date_to,
        ),
        "status_badge": {
            "label": "Ошибка",
            "tone": "error",
            "detail": error_message,
        },
        "summary_cards": [
            {
                "card_id": "status",
                "label": "Статус",
                "value": "Ошибка",
                "detail": error_message,
                "tone": "error",
            },
            {
                "card_id": "rows",
                "label": "Строки",
                "value": "0",
                "detail": "table payload is unavailable",
                "tone": "neutral",
            },
            {
                "card_id": "freshness",
                "label": "Свежесть данных",
                "value": "—",
                "detail": f"snapshot unavailable · as_of_date {as_of_date}",
                "tone": "neutral",
            },
        ],
        "filter_surface": {
            "state_namespace": WEB_VITRINA_PAGE_STATE_NAMESPACE,
            "browser_state_persistence": "none",
            "controls": [],
            "sort_options": [],
            "default_sort_value": "",
            "empty_result_message": "Фильтры не вернули ни одной строки.",
        },
        "table_surface": {
            "columns": [],
            "rows": [],
            "groupings": [],
            "renderers": [],
            "formatters": [],
            "sorts": [],
            "filters": [],
            "state_surface": {
                "current_state": "error",
                "empty_message": "The contract was accepted, but no rows are available to render.",
                "loading_message": "A future page shell may resolve the contract before mapping rows.",
                "error_message": error_message,
            },
            "total_row_count": 0,
            "date_column_ids": [],
            "column_labels": {},
        },
        "status_summary": {
            "refresh_status": "unavailable",
            "read_model": "persisted_ready_snapshot",
            "source_sheet_name": "DATA_VITRINA",
        },
        "capabilities": {
            "sortable": True,
            "filterable": True,
            "read_only": True,
        },
    }


def _to_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"unsupported web_vitrina page payload input: {type(value)!r}")


def _count_rows(rows: list[Mapping[str, Any]], *, key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "")
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    return counts


def _count_metric_rows(rows: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    counts: dict[str, dict[str, Any]] = {}
    for row in rows:
        metric_key = str(_cell_value(row, "metric_key") or "")
        if not metric_key:
            continue
        bucket = counts.setdefault(
            metric_key,
            {
                "label": str(_cell_display(row, "metric_label") or metric_key),
                "count": 0,
            },
        )
        bucket["count"] = int(bucket["count"]) + 1
    return counts


def _build_labeled_options(
    *,
    all_label: str,
    items: list[Mapping[str, Any]],
    counts: Mapping[str, int],
    id_key: str,
) -> list[dict[str, Any]]:
    total = sum(int(value) for value in counts.values())
    options = [
        {
            "value": _ALL_OPTION_VALUE,
            "label": all_label,
            "count": total,
        }
    ]
    for item in items:
        option_id = str(item[id_key])
        options.append(
            {
                "value": option_id,
                "label": str(item["label"]),
                "count": int(counts.get(option_id, 0)),
            }
        )
    return options


def _build_scope_kind_options(row_kind_counts: Mapping[str, int]) -> list[dict[str, Any]]:
    options = [
        {
            "value": _ALL_OPTION_VALUE,
            "label": "Все scope",
            "count": sum(int(value) for value in row_kind_counts.values()),
        }
    ]
    for row_kind in sorted(row_kind_counts):
        options.append(
            {
                "value": row_kind,
                "label": _scope_kind_label(row_kind),
                "count": int(row_kind_counts[row_kind]),
            }
        )
    return options


def _build_metric_options(metric_counts: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    options = [
        {
            "value": _ALL_OPTION_VALUE,
            "label": "Все метрики",
            "count": sum(int(item["count"]) for item in metric_counts.values()),
        }
    ]
    for metric_key, item in sorted(metric_counts.items(), key=lambda pair: str(pair[1]["label"])):
        options.append(
            {
                "value": metric_key,
                "label": str(item["label"]),
                "count": int(item["count"]),
            }
        )
    return options


def _build_sort_options(
    adapter_payload: Mapping[str, Any],
    *,
    column_labels: Mapping[str, str],
) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for item in adapter_payload.get("sorts") or []:
        column_id = str(item["column_id"])
        label = str(column_labels.get(column_id, column_id))
        for direction in item["directions"]:
            options.append(
                {
                    "value": _sort_value(str(item["sort_id"]), str(direction)),
                    "label": f"{label} {_direction_label(str(direction))}",
                    "sort_id": str(item["sort_id"]),
                    "column_id": column_id,
                    "direction": str(direction),
                }
            )
    return options


def _resolve_default_sort_value(adapter_payload: Mapping[str, Any]) -> str:
    for item in adapter_payload.get("sorts") or []:
        sort_id = str(item["sort_id"])
        default_direction = item.get("default_direction")
        if default_direction:
            return _sort_value(sort_id, str(default_direction))
    for item in adapter_payload.get("sorts") or []:
        if str(item["column_id"]) == "row_order" and item.get("directions"):
            return _sort_value(str(item["sort_id"]), str(item["directions"][0]))
    first = next(iter(adapter_payload.get("sorts") or []), None)
    if first is None or not first.get("directions"):
        return ""
    return _sort_value(str(first["sort_id"]), str(first["directions"][0]))


def _build_historical_access(
    *,
    page_route: str,
    default_as_of_date: str,
    available_snapshot_dates: list[str],
    selected_as_of_date: str | None,
    selected_date_from: str | None,
    selected_date_to: str | None,
) -> dict[str, Any]:
    selected_date = str(selected_as_of_date or "").strip()
    selected_from = str(selected_date_from or "").strip()
    selected_to = str(selected_date_to or "").strip()
    explicit_range = bool(selected_from and selected_to)
    explicit_single_date = bool(selected_date) and not explicit_range
    if selected_from and not selected_to:
        selected_to = selected_from
    if selected_to and not selected_from:
        selected_from = selected_to
    if explicit_single_date:
        selected_from = selected_date
        selected_to = selected_date
    options = [
        {
            "value": snapshot_date,
            "label": snapshot_date,
        }
        for snapshot_date in sorted({str(item) for item in available_snapshot_dates if item}, reverse=True)
    ]
    current_mode = "default"
    active_label = default_as_of_date
    if explicit_range:
        current_mode = "historical_period"
        active_label = f"{selected_from}..{selected_to}"
    elif explicit_single_date:
        current_mode = "historical_day"
        active_label = selected_date
    if current_mode == "historical_period":
        status_text = f"Открыт period window {active_label} через persisted ready snapshots."
    elif current_mode == "historical_day":
        status_text = f"Открыт historical snapshot на {active_label}."
    else:
        status_text = f"Открыт текущий cheap daily mode на {active_label} без explicit as_of_date."
    return {
        "state_namespace": WEB_VITRINA_PAGE_STATE_NAMESPACE,
        "browser_state_persistence": "none",
        "url_state_mode": "query_string",
        "supported_query_mode": "date_window",
        "page_route": page_route,
        "default_as_of_date": default_as_of_date,
        "selected_as_of_date": selected_date,
        "selected_date_from": selected_from,
        "selected_date_to": selected_to,
        "current_mode": current_mode,
        "status_text": status_text,
        "available_date_min": options[-1]["value"] if options else "",
        "available_date_max": options[0]["value"] if options else "",
        "options": options,
        "preset_options": [
            {"preset_id": "week", "label": "Неделя"},
            {"preset_id": "two_weeks", "label": "2 недели"},
            {"preset_id": "month", "label": "Месяц"},
            {"preset_id": "quarter", "label": "Квартал"},
            {"preset_id": "year", "label": "Год"},
        ],
        "empty_message": "Исторические ready snapshots пока не materialized.",
    }


def _resolve_ready_state_message(adapter_payload: Mapping[str, Any]) -> str:
    state_surface = adapter_payload["state_surface"]
    current_state = str(state_surface["current_state"])
    if current_state == "ready":
        return "Read-only page composition assembled server-side from persisted ready snapshot."
    if current_state == "empty":
        return str(state_surface["empty_message"])
    if current_state == "loading":
        return str(state_surface["loading_message"])
    return str(state_surface["error_message"])


def _freshness_detail(contract_payload: Mapping[str, Any]) -> str:
    meta = contract_payload["meta"]
    status_summary = contract_payload["status_summary"]
    return (
        f"snapshot {meta['snapshot_id']} · "
        f"as_of_date {meta['as_of_date']} · "
        f"{status_summary['read_model']}"
    )


def _status_status_label(*, current_state: str, refresh_status: str) -> str:
    if current_state == "ready":
        normalized = refresh_status.strip().lower()
        if normalized == "success":
            return "Успешно"
        if normalized in {"running", "pending", "loading"}:
            return "Загрузка"
        return "Ошибка"
    if current_state == "empty":
        return "Нет данных"
    if current_state == "loading":
        return "Загрузка"
    return "Ошибка"


def _status_tone(*, current_state: str, refresh_status: str) -> str:
    if current_state == "error":
        return "error"
    if current_state == "empty":
        return "warning"
    if refresh_status == "success":
        return "success"
    return "warning"


def _scope_kind_label(value: str) -> str:
    mapping = {
        "total": "Итого",
        "group": "Группа",
        "sku": "SKU",
        "other": "Другое",
    }
    return mapping.get(value, value)


def _direction_label(direction: str) -> str:
    return "↑" if direction == "asc" else "↓"


def _sort_value(sort_id: str, direction: str) -> str:
    return f"{sort_id}::{direction}"


def _cell_value(row: Mapping[str, Any], column_id: str) -> Any:
    cell = (row.get("values") or {}).get(column_id) or {}
    return cell.get("value")


def _cell_display(row: Mapping[str, Any], column_id: str) -> str:
    cell = (row.get("values") or {}).get(column_id) or {}
    return str(cell.get("display_text") or "")
