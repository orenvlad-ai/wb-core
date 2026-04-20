"""Targeted smoke-check for the library-agnostic web_vitrina_view_model mapper."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.web_vitrina_view_model import build_web_vitrina_view_model


def main() -> None:
    payload = _build_contract_payload()
    view_model = build_web_vitrina_view_model(payload)

    if view_model.view_model_name != "web_vitrina_view_model" or view_model.view_model_version != "v1":
        raise AssertionError(f"view_model identity mismatch, got {view_model}")
    if view_model.meta.source_contract_name != "web_vitrina_contract" or view_model.meta.source_contract_version != "v1":
        raise AssertionError(f"source contract seam mismatch, got {view_model.meta}")
    if view_model.meta.column_count != 11 or view_model.meta.group_count != 2 or view_model.meta.section_count != 2:
        raise AssertionError(f"view_model meta counts mismatch, got {view_model.meta}")

    columns = {column.id: column for column in view_model.columns}
    if columns["scope_label"].sticky != "left" or columns["scope_label"].width_hint != 280:
        raise AssertionError(f"scope_label column intent mismatch, got {columns['scope_label']}")
    if columns["date:2026-04-20"].kind != "temporal_measure" or columns["date:2026-04-20"].align != "end":
        raise AssertionError(f"temporal column mapping mismatch, got {columns['date:2026-04-20']}")
    if columns["scope_kind"].align != "center" or columns["scope_kind"].filter_key != "scope_kind":
        raise AssertionError(f"scope_kind column mapping mismatch, got {columns['scope_kind']}")

    groups = {group.group_id: group for group in view_model.groups}
    if groups["group:overview"].label != "ИТОГО" or groups["group:Чехлы"].order != 2:
        raise AssertionError(f"group mapping mismatch, got {view_model.groups}")
    sections = {section.section_id: section for section in view_model.sections}
    if sorted(sections) != ["section:Воронка", "section:Цены"]:
        raise AssertionError(f"section mapping mismatch, got {view_model.sections}")

    rows = {row.row_id: row for row in view_model.rows}
    price_row = rows["SKU:101|avg_price_seller_discounted"]
    percent_row = rows["SKU:101|avg_addToCartConversion"]
    second_price_row = rows["SKU:102|avg_price_seller_discounted"]
    second_percent_row = rows["SKU:102|avg_addToCartConversion"]
    total_row = rows["TOTAL|total_view_count"]
    if total_row.row_kind != "total" or total_row.group_id != "group:overview":
        raise AssertionError(f"total row mapping mismatch, got {total_row}")
    if price_row.group_id != "group:Чехлы" or price_row.section_id != "section:Цены":
        raise AssertionError(f"price row grouping mismatch, got {price_row}")
    if "SKU Чехол 101" not in price_row.search_text or price_row.filter_tokens["nm_id"] != ["101"]:
        raise AssertionError(f"search/filter tokens mismatch, got {price_row}")

    price_cell = _cell(price_row, "date:2026-04-20")
    percent_cell = _cell(percent_row, "date:2026-04-20")
    empty_cell = _cell(percent_row, "date:2026-04-19")
    if price_cell.cell_kind != "money" or price_cell.formatter_id != "money_rub":
        raise AssertionError(f"money cell mapping mismatch, got {price_cell}")
    if percent_cell.cell_kind != "percent" or percent_cell.formatter_id != "percent_default":
        raise AssertionError(f"percent cell mapping mismatch, got {percent_cell}")
    if empty_cell.cell_kind != "empty" or empty_cell.display_text != "—":
        raise AssertionError(f"empty cell mapping mismatch, got {empty_cell}")

    ordered_row_ids = [row.row_id for row in view_model.rows]
    expected_order = [
        "TOTAL|total_view_count",
        "SKU:101|avg_price_seller_discounted",
        "SKU:101|avg_addToCartConversion",
        "SKU:102|avg_price_seller_discounted",
        "SKU:102|avg_addToCartConversion",
    ]
    if ordered_row_ids != expected_order:
        raise AssertionError(f"canonical row ordering mismatch, got {ordered_row_ids}")
    if _cell(price_row, "row_order").value != 2 or _cell(percent_row, "row_order").value != 3:
        raise AssertionError(f"first SKU ordering mismatch, got {_cell(price_row, 'row_order').value} / {_cell(percent_row, 'row_order').value}")
    if _cell(second_price_row, "row_order").value != 4 or _cell(second_percent_row, "row_order").value != 5:
        raise AssertionError(f"second SKU ordering mismatch, got {_cell(second_price_row, 'row_order').value} / {_cell(second_percent_row, 'row_order').value}")

    filters = {item.filter_id: item for item in view_model.filters}
    sorts = {item.sort_id: item for item in view_model.sorts}
    if not filters["metric_key"].multi_value or filters["metric_key"].value_type != "string":
        raise AssertionError(f"filter mapping mismatch, got {filters['metric_key']}")
    if sorts["row_order"].default_direction != "asc" or "desc" not in sorts["date:2026-04-20"].directions:
        raise AssertionError(f"sort mapping mismatch, got {view_model.sorts}")

    formatter_ids = {item.formatter_id for item in view_model.formatters}
    if formatter_ids != {"badge_default", "empty_default", "money_rub", "number_default", "percent_default", "text_default", "unknown_default"}:
        raise AssertionError(f"formatter library mismatch, got {view_model.formatters}")
    if view_model.state_model.current_state != "ready":
        raise AssertionError(f"state model mismatch, got {view_model.state_model}")

    empty_payload = deepcopy(payload)
    empty_payload["rows"] = []
    empty_view_model = build_web_vitrina_view_model(empty_payload)
    if empty_view_model.state_model.current_state != "empty" or empty_view_model.meta.row_count != 0:
        raise AssertionError(f"empty state mapping mismatch, got {empty_view_model}")

    print("web_vitrina_view_model_identity: ok ->", view_model.view_model_name, view_model.view_model_version)
    print("web_vitrina_view_model_columns: ok ->", columns["scope_label"].id, columns["date:2026-04-20"].id)
    print("web_vitrina_view_model_groups: ok ->", len(view_model.groups), len(view_model.sections))
    print("web_vitrina_view_model_cells: ok ->", price_cell.formatter_id, percent_cell.formatter_id, empty_cell.cell_kind)
    print("web_vitrina_view_model_state: ok ->", view_model.state_model.current_state, empty_view_model.state_model.current_state)


def _cell(row: object, column_id: str) -> object:
    return next(cell for cell in row.cells if cell.column_id == column_id)


def _build_contract_payload() -> dict[str, object]:
    return {
        "contract_name": "web_vitrina_contract",
        "contract_version": "v1",
        "page_route": "/sheet-vitrina-v1/vitrina",
        "read_route": "/v1/sheet-vitrina-v1/web-vitrina",
        "meta": {
            "snapshot_id": "web-vitrina-view-model-fixture",
            "bundle_version": "bundle-fixture-v1",
            "as_of_date": "2026-04-20",
            "business_timezone": "Asia/Yekaterinburg",
            "date_columns": ["2026-04-19", "2026-04-20"],
            "temporal_slots": [
                {"slot_key": "yesterday_closed", "slot_label": "Yesterday closed", "column_date": "2026-04-19"},
                {"slot_key": "today_current", "slot_label": "Today current", "column_date": "2026-04-20"},
            ],
            "generated_at": "2026-04-21T08:00:00Z",
            "refreshed_at": "2026-04-21T07:55:00Z",
            "row_count": 5,
        },
        "status_summary": {
            "refresh_status": "success",
            "read_model": "persisted_ready_snapshot",
            "source_sheet_name": "DATA_VITRINA",
            "bundle_version": "bundle-fixture-v1",
            "activated_at": "2026-04-21T07:40:00Z",
            "refreshed_at": "2026-04-21T07:55:00Z",
            "business_now": "2026-04-21T13:00:00+05:00",
            "current_business_date": "2026-04-21",
            "default_as_of_date": "2026-04-20",
            "last_auto_run_status": "success",
            "last_auto_run_started_at": "2026-04-21T06:00:00Z",
            "last_auto_run_finished_at": "2026-04-21T06:05:00Z",
            "last_successful_auto_update_at": "2026-04-21T06:05:00Z",
            "last_successful_manual_refresh_at": "2026-04-20T18:00:00Z",
            "last_successful_manual_load_at": "2026-04-20T18:03:00Z",
            "source_policy_counts": {"dual_day_capable": 2},
            "source_count": 2,
            "data_sheet_row_count": 3,
        },
        "schema": {
            "row_identity_fields": ["row_id"],
            "columns": [
                {"column_id": "row_order", "label": "Row order", "kind": "identity", "value_type": "integer", "sortable": True, "filterable": False, "column_date": None, "temporal_slot_key": None},
                {"column_id": "scope_kind", "label": "Scope kind", "kind": "dimension", "value_type": "string", "sortable": True, "filterable": True, "column_date": None, "temporal_slot_key": None},
                {"column_id": "scope_key", "label": "Scope key", "kind": "dimension", "value_type": "string", "sortable": True, "filterable": True, "column_date": None, "temporal_slot_key": None},
                {"column_id": "scope_label", "label": "Scope label", "kind": "dimension", "value_type": "string", "sortable": True, "filterable": True, "column_date": None, "temporal_slot_key": None},
                {"column_id": "group", "label": "Group", "kind": "dimension", "value_type": "string_or_null", "sortable": True, "filterable": True, "column_date": None, "temporal_slot_key": None},
                {"column_id": "nm_id", "label": "nmId", "kind": "dimension", "value_type": "integer_or_null", "sortable": True, "filterable": True, "column_date": None, "temporal_slot_key": None},
                {"column_id": "metric_key", "label": "Metric key", "kind": "dimension", "value_type": "string", "sortable": True, "filterable": True, "column_date": None, "temporal_slot_key": None},
                {"column_id": "metric_label", "label": "Metric label", "kind": "dimension", "value_type": "string", "sortable": True, "filterable": True, "column_date": None, "temporal_slot_key": None},
                {"column_id": "section", "label": "Section", "kind": "dimension", "value_type": "string", "sortable": True, "filterable": True, "column_date": None, "temporal_slot_key": None},
                {"column_id": "date:2026-04-19", "label": "2026-04-19", "kind": "temporal_value", "value_type": "number_or_blank", "sortable": True, "filterable": False, "column_date": "2026-04-19", "temporal_slot_key": "yesterday_closed"},
                {"column_id": "date:2026-04-20", "label": "2026-04-20", "kind": "temporal_value", "value_type": "number_or_blank", "sortable": True, "filterable": False, "column_date": "2026-04-20", "temporal_slot_key": "today_current"},
            ],
            "filters": [
                {"filter_id": "scope_kind", "field": "scope_kind", "label": "Scope kind", "operators": ["eq", "in"]},
                {"filter_id": "group", "field": "group", "label": "Group", "operators": ["eq", "in"]},
                {"filter_id": "nm_id", "field": "nm_id", "label": "nmId", "operators": ["eq", "in"]},
                {"filter_id": "section", "field": "section", "label": "Section", "operators": ["eq", "in"]},
                {"filter_id": "metric_key", "field": "metric_key", "label": "Metric key", "operators": ["eq", "in"]},
            ],
            "sorts": [
                {"sort_id": "row_order", "field": "row_order", "label": "Row order", "directions": ["asc", "desc"], "default_direction": "asc"},
                {"sort_id": "scope_label", "field": "scope_label", "label": "Scope label", "directions": ["asc", "desc"], "default_direction": None},
                {"sort_id": "metric_label", "field": "metric_label", "label": "Metric label", "directions": ["asc", "desc"], "default_direction": None},
                {"sort_id": "date:2026-04-19", "field": "date:2026-04-19", "label": "2026-04-19", "directions": ["asc", "desc"], "default_direction": None},
                {"sort_id": "date:2026-04-20", "field": "date:2026-04-20", "label": "2026-04-20", "directions": ["asc", "desc"], "default_direction": None},
            ],
        },
        "rows": [
            {
                "row_id": "TOTAL|total_view_count",
                "row_order": 1,
                "scope_kind": "TOTAL",
                "scope_key": "TOTAL",
                "scope_label": "ИТОГО",
                "metric_key": "total_view_count",
                "metric_label": "Показы в воронке",
                "section": "Воронка",
                "group": None,
                "nm_id": None,
                "format": "integer",
                "values_by_date": {"2026-04-19": 100, "2026-04-20": 140},
            },
            {
                "row_id": "SKU:101|avg_price_seller_discounted",
                "row_order": 2,
                "scope_kind": "SKU",
                "scope_key": "SKU:101",
                "scope_label": "SKU Чехол 101",
                "metric_key": "avg_price_seller_discounted",
                "metric_label": "Цена продавца средняя",
                "section": "Цены",
                "group": "Чехлы",
                "nm_id": 101,
                "format": "rub",
                "values_by_date": {"2026-04-19": 1200, "2026-04-20": 1250},
            },
            {
                "row_id": "SKU:102|avg_price_seller_discounted",
                "row_order": 3,
                "scope_kind": "SKU",
                "scope_key": "SKU:102",
                "scope_label": "SKU Чехол 102",
                "metric_key": "avg_price_seller_discounted",
                "metric_label": "Цена продавца средняя",
                "section": "Цены",
                "group": "Чехлы",
                "nm_id": 102,
                "format": "rub",
                "values_by_date": {"2026-04-19": 1300, "2026-04-20": 1350},
            },
            {
                "row_id": "SKU:101|avg_addToCartConversion",
                "row_order": 4,
                "scope_kind": "SKU",
                "scope_key": "SKU:101",
                "scope_label": "SKU Чехол 101",
                "metric_key": "avg_addToCartConversion",
                "metric_label": "Конверсия в корзину",
                "section": "Воронка",
                "group": "Чехлы",
                "nm_id": 101,
                "format": "percent",
                "values_by_date": {"2026-04-19": "", "2026-04-20": 12.5},
            },
            {
                "row_id": "SKU:102|avg_addToCartConversion",
                "row_order": 5,
                "scope_kind": "SKU",
                "scope_key": "SKU:102",
                "scope_label": "SKU Чехол 102",
                "metric_key": "avg_addToCartConversion",
                "metric_label": "Конверсия в корзину",
                "section": "Воронка",
                "group": "Чехлы",
                "nm_id": 102,
                "format": "percent",
                "values_by_date": {"2026-04-19": "", "2026-04-20": 13.5},
            },
        ],
        "capabilities": {
            "sortable": True,
            "filterable": True,
            "exportable": False,
            "read_only": True,
            "grid_library_agnostic": True,
            "thin_page_shell": True,
        },
    }


if __name__ == "__main__":
    main()
