"""Targeted smoke-check for the Gravity-table adapter over web_vitrina_view_model."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.web_vitrina_gravity_table_adapter import (
    build_web_vitrina_gravity_table_adapter,
)


def main() -> None:
    payload = _build_view_model_payload()
    adapter = build_web_vitrina_gravity_table_adapter(payload)

    if adapter.adapter_name != "web_vitrina_gravity_table_adapter" or adapter.adapter_version != "v1":
        raise AssertionError(f"adapter identity mismatch, got {adapter}")
    if adapter.meta.library_name != "@gravity-ui/table" or adapter.meta.library_surface != "Table/useTable + ColumnDef":
        raise AssertionError(f"library seam mismatch, got {adapter.meta}")
    if adapter.meta.source_view_model_name != "web_vitrina_view_model":
        raise AssertionError(f"source view_model seam mismatch, got {adapter.meta}")

    columns = {column.id: column for column in adapter.columns}
    money_column = columns["date:2026-04-20"]
    if money_column.accessor_key != "date:2026-04-20" or money_column.meta.align != "end":
        raise AssertionError(f"temporal column mapping mismatch, got {money_column}")
    if columns["scope_label"].meta.pin != "left" or columns["scope_label"].size != 110:
        raise AssertionError(f"sticky column mapping mismatch, got {columns['scope_label']}")
    if columns["row_order"].size != 40 or money_column.size != 84 or columns["metric_key"].size != 140:
        raise AssertionError(f"compact width sizing mismatch, got {columns['row_order']} / {money_column} / {columns['metric_key']}")
    if columns["section"].meta.default_cell_renderer_id != "renderer:badge:badge_default":
        raise AssertionError(f"badge renderer binding mismatch, got {columns['section']}")
    if columns["date:2026-04-20"].meta.uses_row_cell_renderers is not True:
        raise AssertionError(f"row-cell renderer mode mismatch, got {columns['date:2026-04-20']}")

    rows = {row.row_id: row for row in adapter.rows}
    price_row = rows["SKU:101|avg_price_seller_discounted"]
    percent_row = rows["SKU:101|avg_addToCartConversion"]
    if price_row.values["date:2026-04-20"].renderer_id != "renderer:money:money_rub":
        raise AssertionError(f"money renderer mismatch, got {price_row.values['date:2026-04-20']}")
    if percent_row.values["date:2026-04-20"].renderer_id != "renderer:percent:percent_default":
        raise AssertionError(f"percent renderer mismatch, got {percent_row.values['date:2026-04-20']}")
    if percent_row.values["date:2026-04-19"].renderer_id != "renderer:empty:empty_default":
        raise AssertionError(f"empty renderer mismatch, got {percent_row.values['date:2026-04-19']}")
    if price_row.depth != 0 or price_row.parent_id is not None:
        raise AssertionError(f"row depth contract mismatch, got {price_row}")

    renderer_ids = {renderer.renderer_id for renderer in adapter.renderers}
    expected_renderer_ids = {
        "renderer:badge:badge_default",
        "renderer:empty:empty_default",
        "renderer:money:money_rub",
        "renderer:number:number_default",
        "renderer:percent:percent_default",
        "renderer:text:text_default",
        "renderer:unknown:unknown_default",
    }
    if renderer_ids != expected_renderer_ids:
        raise AssertionError(f"renderer registry mismatch, got {adapter.renderers}")

    grouping = next(item for item in adapter.groupings if item.group_id == "group:Чехлы")
    if grouping.title != "Чехлы" or grouping.row_ids != ["SKU:101|avg_price_seller_discounted", "SKU:101|avg_addToCartConversion"]:
        raise AssertionError(f"grouping seam mismatch, got {grouping}")
    if grouping.section_id != "section:mixed":
        raise AssertionError(f"grouping section scope mismatch, got {grouping}")
    if adapter.use_table_options.manual_sorting is not True or adapter.use_table_options.manual_filtering is not True:
        raise AssertionError(f"useTable options mismatch, got {adapter.use_table_options}")
    if adapter.state_surface.current_state != "ready" or adapter.table_props.component != "Table":
        raise AssertionError(f"state/table props mismatch, got {adapter.state_surface} / {adapter.table_props}")

    print("web_vitrina_gravity_adapter_identity: ok ->", adapter.adapter_name, adapter.adapter_version)
    print("web_vitrina_gravity_adapter_columns: ok ->", columns["scope_label"].meta.pin, money_column.meta.default_cell_renderer_id)
    print("web_vitrina_gravity_adapter_rows: ok ->", price_row.row_id, percent_row.row_id)
    print("web_vitrina_gravity_adapter_groupings: ok ->", grouping.grouping_id, grouping.title)
    print("web_vitrina_gravity_adapter_use_table: ok ->", adapter.use_table_options.manual_sorting, adapter.use_table_options.manual_filtering)


def _build_view_model_payload() -> dict[str, object]:
    return {
        "view_model_name": "web_vitrina_view_model",
        "view_model_version": "v1",
        "meta": {
            "snapshot_id": "gravity-adapter-fixture",
            "as_of_date": "2026-04-20",
            "business_timezone": "Asia/Yekaterinburg",
            "source_contract_name": "web_vitrina_contract",
            "source_contract_version": "v1",
            "generated_at": "2026-04-21T09:00:00Z",
            "row_count": 3,
            "column_count": 11,
            "group_count": 2,
            "section_count": 2,
        },
        "columns": [
            {"id": "row_order", "label": "Row order", "kind": "identity", "value_type": "integer", "align": "end", "sticky": "left", "width_hint": 52, "sortable": True, "filterable": False, "sort_key": "row_order", "filter_key": None},
            {"id": "scope_kind", "label": "Scope kind", "kind": "dimension", "value_type": "string", "align": "center", "sticky": "none", "width_hint": 92, "sortable": True, "filterable": True, "sort_key": None, "filter_key": "scope_kind"},
            {"id": "scope_key", "label": "Scope key", "kind": "dimension", "value_type": "string", "align": "start", "sticky": "none", "width_hint": 140, "sortable": True, "filterable": True, "sort_key": None, "filter_key": None},
            {"id": "scope_label", "label": "Scope label", "kind": "dimension", "value_type": "string", "align": "start", "sticky": "left", "width_hint": 208, "sortable": True, "filterable": True, "sort_key": "scope_label", "filter_key": None},
            {"id": "group", "label": "Group", "kind": "dimension", "value_type": "string_or_null", "align": "start", "sticky": "none", "width_hint": 112, "sortable": True, "filterable": True, "sort_key": None, "filter_key": "group"},
            {"id": "nm_id", "label": "nmId", "kind": "dimension", "value_type": "integer_or_null", "align": "end", "sticky": "none", "width_hint": 112, "sortable": True, "filterable": True, "sort_key": None, "filter_key": "nm_id"},
            {"id": "metric_key", "label": "Metric key", "kind": "dimension", "value_type": "string", "align": "start", "sticky": "none", "width_hint": 140, "sortable": True, "filterable": True, "sort_key": None, "filter_key": "metric_key"},
            {"id": "metric_label", "label": "Metric label", "kind": "dimension", "value_type": "string", "align": "start", "sticky": "left", "width_hint": 176, "sortable": True, "filterable": True, "sort_key": "metric_label", "filter_key": None},
            {"id": "section", "label": "Section", "kind": "dimension", "value_type": "string", "align": "center", "sticky": "none", "width_hint": 92, "sortable": True, "filterable": True, "sort_key": None, "filter_key": "section"},
            {"id": "date:2026-04-19", "label": "2026-04-19", "kind": "temporal_measure", "value_type": "number_or_blank", "align": "end", "sticky": "none", "width_hint": 88, "sortable": True, "filterable": False, "sort_key": "date:2026-04-19", "filter_key": None},
            {"id": "date:2026-04-20", "label": "2026-04-20", "kind": "temporal_measure", "value_type": "number_or_blank", "align": "end", "sticky": "none", "width_hint": 88, "sortable": True, "filterable": False, "sort_key": "date:2026-04-20", "filter_key": None},
        ],
        "sections": [
            {"section_id": "section:Воронка", "label": "Воронка", "order": 1, "collapsed_by_default": False},
            {"section_id": "section:Цены", "label": "Цены", "order": 2, "collapsed_by_default": False},
        ],
        "groups": [
            {"group_id": "group:overview", "label": "ИТОГО", "order": 1, "collapsed_by_default": False},
            {"group_id": "group:Чехлы", "label": "Чехлы", "order": 2, "collapsed_by_default": False},
        ],
        "rows": [
            {
                "row_id": "TOTAL|total_view_count",
                "row_kind": "total",
                "section_id": "section:Воронка",
                "group_id": "group:overview",
                "cells": [
                    {"column_id": "row_order", "cell_kind": "number", "value_type": "integer", "value": 1, "display_text": "1", "formatter_id": "number_default"},
                    {"column_id": "scope_kind", "cell_kind": "badge", "value_type": "string", "value": "TOTAL", "display_text": "TOTAL", "formatter_id": "badge_default"},
                    {"column_id": "scope_key", "cell_kind": "text", "value_type": "string", "value": "TOTAL", "display_text": "TOTAL", "formatter_id": "text_default"},
                    {"column_id": "scope_label", "cell_kind": "text", "value_type": "string", "value": "ИТОГО", "display_text": "ИТОГО", "formatter_id": "text_default"},
                    {"column_id": "group", "cell_kind": "unknown", "value_type": "string_or_null", "value": None, "display_text": "—", "formatter_id": "unknown_default"},
                    {"column_id": "nm_id", "cell_kind": "unknown", "value_type": "integer_or_null", "value": None, "display_text": "—", "formatter_id": "unknown_default"},
                    {"column_id": "metric_key", "cell_kind": "text", "value_type": "string", "value": "total_view_count", "display_text": "total_view_count", "formatter_id": "text_default"},
                    {"column_id": "metric_label", "cell_kind": "text", "value_type": "string", "value": "Показы в воронке", "display_text": "Показы в воронке", "formatter_id": "text_default"},
                    {"column_id": "section", "cell_kind": "badge", "value_type": "string", "value": "Воронка", "display_text": "Воронка", "formatter_id": "badge_default"},
                    {"column_id": "date:2026-04-19", "cell_kind": "number", "value_type": "number_or_blank", "value": 100, "display_text": "100", "formatter_id": "number_default"},
                    {"column_id": "date:2026-04-20", "cell_kind": "number", "value_type": "number_or_blank", "value": 140, "display_text": "140", "formatter_id": "number_default"},
                ],
                "search_text": "ИТОГО Показы в воронке Воронка 100 140",
                "filter_tokens": {"scope_kind": ["TOTAL"], "group": [], "nm_id": [], "section": ["Воронка"], "metric_key": ["total_view_count"], "row_kind": ["total"], "group_id": ["group:overview"], "section_id": ["section:Воронка"]},
            },
            {
                "row_id": "SKU:101|avg_price_seller_discounted",
                "row_kind": "sku",
                "section_id": "section:Цены",
                "group_id": "group:Чехлы",
                "cells": [
                    {"column_id": "row_order", "cell_kind": "number", "value_type": "integer", "value": 2, "display_text": "2", "formatter_id": "number_default"},
                    {"column_id": "scope_kind", "cell_kind": "badge", "value_type": "string", "value": "SKU", "display_text": "SKU", "formatter_id": "badge_default"},
                    {"column_id": "scope_key", "cell_kind": "text", "value_type": "string", "value": "SKU:101", "display_text": "SKU:101", "formatter_id": "text_default"},
                    {"column_id": "scope_label", "cell_kind": "text", "value_type": "string", "value": "SKU Чехол 101", "display_text": "SKU Чехол 101", "formatter_id": "text_default"},
                    {"column_id": "group", "cell_kind": "text", "value_type": "string_or_null", "value": "Чехлы", "display_text": "Чехлы", "formatter_id": "text_default"},
                    {"column_id": "nm_id", "cell_kind": "number", "value_type": "integer_or_null", "value": 101, "display_text": "101", "formatter_id": "number_default"},
                    {"column_id": "metric_key", "cell_kind": "text", "value_type": "string", "value": "avg_price_seller_discounted", "display_text": "avg_price_seller_discounted", "formatter_id": "text_default"},
                    {"column_id": "metric_label", "cell_kind": "text", "value_type": "string", "value": "Цена продавца средняя", "display_text": "Цена продавца средняя", "formatter_id": "text_default"},
                    {"column_id": "section", "cell_kind": "badge", "value_type": "string", "value": "Цены", "display_text": "Цены", "formatter_id": "badge_default"},
                    {"column_id": "date:2026-04-19", "cell_kind": "money", "value_type": "number_or_blank", "value": 1200, "display_text": "1200", "formatter_id": "money_rub"},
                    {"column_id": "date:2026-04-20", "cell_kind": "money", "value_type": "number_or_blank", "value": 1250, "display_text": "1250", "formatter_id": "money_rub"},
                ],
                "search_text": "SKU Чехол 101 Цена продавца средняя Чехлы Цены 101 1200 1250",
                "filter_tokens": {"scope_kind": ["SKU"], "group": ["Чехлы"], "nm_id": ["101"], "section": ["Цены"], "metric_key": ["avg_price_seller_discounted"], "row_kind": ["sku"], "group_id": ["group:Чехлы"], "section_id": ["section:Цены"]},
            },
            {
                "row_id": "SKU:101|avg_addToCartConversion",
                "row_kind": "sku",
                "section_id": "section:Воронка",
                "group_id": "group:Чехлы",
                "cells": [
                    {"column_id": "row_order", "cell_kind": "number", "value_type": "integer", "value": 3, "display_text": "3", "formatter_id": "number_default"},
                    {"column_id": "scope_kind", "cell_kind": "badge", "value_type": "string", "value": "SKU", "display_text": "SKU", "formatter_id": "badge_default"},
                    {"column_id": "scope_key", "cell_kind": "text", "value_type": "string", "value": "SKU:101", "display_text": "SKU:101", "formatter_id": "text_default"},
                    {"column_id": "scope_label", "cell_kind": "text", "value_type": "string", "value": "SKU Чехол 101", "display_text": "SKU Чехол 101", "formatter_id": "text_default"},
                    {"column_id": "group", "cell_kind": "text", "value_type": "string_or_null", "value": "Чехлы", "display_text": "Чехлы", "formatter_id": "text_default"},
                    {"column_id": "nm_id", "cell_kind": "number", "value_type": "integer_or_null", "value": 101, "display_text": "101", "formatter_id": "number_default"},
                    {"column_id": "metric_key", "cell_kind": "text", "value_type": "string", "value": "avg_addToCartConversion", "display_text": "avg_addToCartConversion", "formatter_id": "text_default"},
                    {"column_id": "metric_label", "cell_kind": "text", "value_type": "string", "value": "Конверсия в корзину", "display_text": "Конверсия в корзину", "formatter_id": "text_default"},
                    {"column_id": "section", "cell_kind": "badge", "value_type": "string", "value": "Воронка", "display_text": "Воронка", "formatter_id": "badge_default"},
                    {"column_id": "date:2026-04-19", "cell_kind": "empty", "value_type": "number_or_blank", "value": "", "display_text": "—", "formatter_id": "empty_default"},
                    {"column_id": "date:2026-04-20", "cell_kind": "percent", "value_type": "number_or_blank", "value": 0.125, "display_text": "0.125", "formatter_id": "percent_default"},
                ],
                "search_text": "SKU Чехол 101 Конверсия в корзину Чехлы Воронка 101 0.125",
                "filter_tokens": {"scope_kind": ["SKU"], "group": ["Чехлы"], "nm_id": ["101"], "section": ["Воронка"], "metric_key": ["avg_addToCartConversion"], "row_kind": ["sku"], "group_id": ["group:Чехлы"], "section_id": ["section:Воронка"]},
            },
        ],
        "filters": [
            {"filter_id": "scope_kind", "field": "scope_kind", "label": "Scope kind", "operators": ["eq", "in"], "value_type": "string", "multi_value": True},
            {"filter_id": "group", "field": "group", "label": "Group", "operators": ["eq", "in"], "value_type": "string_or_null", "multi_value": True},
            {"filter_id": "nm_id", "field": "nm_id", "label": "nmId", "operators": ["eq", "in"], "value_type": "integer_or_null", "multi_value": True},
            {"filter_id": "section", "field": "section", "label": "Section", "operators": ["eq", "in"], "value_type": "string", "multi_value": True},
            {"filter_id": "metric_key", "field": "metric_key", "label": "Metric key", "operators": ["eq", "in"], "value_type": "string", "multi_value": True},
        ],
        "sorts": [
            {"sort_id": "row_order", "field": "row_order", "label": "Row order", "directions": ["asc", "desc"], "default_direction": "asc"},
            {"sort_id": "scope_label", "field": "scope_label", "label": "Scope label", "directions": ["asc", "desc"], "default_direction": None},
            {"sort_id": "metric_label", "field": "metric_label", "label": "Metric label", "directions": ["asc", "desc"], "default_direction": None},
            {"sort_id": "date:2026-04-19", "field": "date:2026-04-19", "label": "2026-04-19", "directions": ["asc", "desc"], "default_direction": None},
            {"sort_id": "date:2026-04-20", "field": "date:2026-04-20", "label": "2026-04-20", "directions": ["asc", "desc"], "default_direction": None},
        ],
        "formatters": [
            {"formatter_id": "badge_default", "cell_kind": "badge", "rule_kind": "badge", "decimals": None, "thousands_separator": False, "prefix": None, "suffix": None, "null_display": "—", "date_pattern": None, "value_multiplier": None},
            {"formatter_id": "empty_default", "cell_kind": "empty", "rule_kind": "empty", "decimals": None, "thousands_separator": False, "prefix": None, "suffix": None, "null_display": "—", "date_pattern": None, "value_multiplier": None},
            {"formatter_id": "money_rub", "cell_kind": "money", "rule_kind": "money", "decimals": 0, "thousands_separator": True, "prefix": None, "suffix": " ₽", "null_display": "—", "date_pattern": None, "value_multiplier": None},
            {"formatter_id": "number_default", "cell_kind": "number", "rule_kind": "number", "decimals": 0, "thousands_separator": True, "prefix": None, "suffix": None, "null_display": "—", "date_pattern": None, "value_multiplier": None},
            {"formatter_id": "percent_default", "cell_kind": "percent", "rule_kind": "percent", "decimals": 2, "thousands_separator": False, "prefix": None, "suffix": "%", "null_display": "—", "date_pattern": None, "value_multiplier": 100.0},
            {"formatter_id": "text_default", "cell_kind": "text", "rule_kind": "text", "decimals": None, "thousands_separator": False, "prefix": None, "suffix": None, "null_display": "—", "date_pattern": None, "value_multiplier": None},
            {"formatter_id": "unknown_default", "cell_kind": "unknown", "rule_kind": "unknown", "decimals": None, "thousands_separator": False, "prefix": None, "suffix": None, "null_display": "—", "date_pattern": None, "value_multiplier": None},
        ],
        "state_model": {
            "namespace": "web_vitrina.view_model",
            "current_state": "ready",
            "available_states": [
                {"state_id": "ready", "label": "Ready", "message": "Contract rows are available for adapter composition."},
                {"state_id": "empty", "label": "Empty", "message": "The contract was accepted, but no rows are available to render."},
                {"state_id": "loading", "label": "Loading", "message": "A future page shell may resolve the contract before mapping rows."},
                {"state_id": "error", "label": "Error", "message": "Page composition may expose contract/load failures without coupling to grid internals."},
            ],
        },
    }


if __name__ == "__main__":
    main()
