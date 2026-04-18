"""Targeted smoke-check для data-driven date-matrix presentation в DATA_VITRINA."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def _row_by_key(sheet: list[list[object]], key: str) -> list[object]:
    for row in sheet:
        if len(row) > 1 and row[1] == key:
            return row
    raise AssertionError(f"missing row for key={key}")


def main() -> None:
    harness_result = json.loads(
        subprocess.check_output(
            [
                "node",
                str(ROOT / "apps" / "sheet_vitrina_v1_registry_upload_trigger_harness.js"),
                "--mode",
                "server_driven_materialization",
                "--scriptPath",
                str(ROOT / "gas" / "sheet_vitrina_v1" / "RegistryUploadTrigger.gs"),
            ],
            cwd=ROOT,
            text=True,
        )
    )

    first_sheet = harness_result["snapshots"]["after_first_load"]["values"]
    first_state = harness_result["states"]["first_load"]["sheets"][0]
    first_presentation = harness_result["presentations"]["first_load"]["sheets"][0]

    if first_sheet[0] != ["дата", "key", "2026-04-12"]:
        raise AssertionError("DATA_VITRINA must expose date-matrix header")
    expected_prefix = [
        ["ИТОГО", "TOTAL", ""],
        ["Маржинальность прокси, %", "proxy_margin_pct_total", 0.2],
        ["Прокси-прибыль, ₽", "total_proxy_profit_rub", 10000],
        ["Показы в воронке", "total_view_count", 1000],
    ]
    if first_sheet[1:5] != expected_prefix:
        raise AssertionError("unexpected matrix prefix after first load")
    if first_sheet[12][:2] != ["ГРУППА: Clean", "GROUP:Clean"]:
        raise AssertionError("matrix layout lost the first group block header")
    first_sku_index = next(
        (index for index, row in enumerate(first_sheet) if len(row) > 1 and row[1] == "SKU:210183919"),
        -1,
    )
    if first_sku_index <= 0 or first_sheet[first_sku_index][:2] != ["clean iPhone 14", "SKU:210183919"]:
        raise AssertionError("matrix layout lost the first SKU block header")
    if first_sheet[first_sku_index + 1][:2] != ["Показы в воронке", "view_count"]:
        raise AssertionError("matrix layout lost the first SKU metric row")

    if first_state["layout_mode"] != "date_matrix":
        raise AssertionError("DATA_VITRINA must materialize date_matrix layout")
    if first_state["metric_key_count"] != 20:
        raise AssertionError("synthetic matrix must keep every incoming metric key")
    if first_state["block_key_count"] != 5:
        raise AssertionError("synthetic matrix must keep TOTAL + groups + SKUs as blocks")
    if first_state["date_column_count"] != 1:
        raise AssertionError("first load must keep a single date column")
    if first_state["metric_row_count"] != 51 or first_state["separator_row_count"] != 4:
        raise AssertionError("matrix layout row accounting mismatch")
    if "proxy_profit_rub" not in first_state["metric_keys"]:
        raise AssertionError("matrix layout lost proxy_profit_rub")
    if "localization_percent" not in first_state["metric_keys"]:
        raise AssertionError("matrix layout lost localization_percent")
    if first_state["data_row_count"] != len(first_sheet) - 1:
        raise AssertionError("DATA_VITRINA data_row_count mismatch")
    if first_state["scope_block_counts"] != {"TOTAL": 1, "GROUP": 2, "SKU": 2, "OTHER": 0}:
        raise AssertionError("DATA_VITRINA scope_block_counts mismatch")

    if first_presentation["frozen_columns"] != 2:
        raise AssertionError("DATA_VITRINA must keep frozen A:B columns")
    if first_presentation["header_style"]["background"] != "#ffffff":
        raise AssertionError("DATA_VITRINA header must not keep dark fill")
    if first_presentation["header_style"]["date_number_format"] != "dd.mm.yyyy":
        raise AssertionError("date header format mismatch")
    if first_presentation["column_widths"]["A"] != 280 or first_presentation["column_widths"]["B"] != 220:
        raise AssertionError("base DATA_VITRINA widths mismatch")
    if first_presentation["samples"]["section"] is None or first_presentation["samples"]["section"]["key"] != "TOTAL":
        raise AssertionError("matrix view must expose bold section rows")
    if first_presentation["samples"]["percent"]["number_format"] != "0.0%":
        raise AssertionError("CTR rows must keep percent format")
    if first_presentation["samples"]["decimal"]["number_format"] != "#,##0.00":
        raise AssertionError("position_avg rows must keep decimal format")
    if first_presentation["samples"]["integer"]["number_format"] != "#,##0":
        raise AssertionError("count-like rows must keep integer format")

    overwrite_sheet = harness_result["snapshots"]["after_same_day_overwrite"]["values"]
    overwrite_state = harness_result["states"]["same_day_overwrite"]["sheets"][0]
    if overwrite_sheet[0] != ["дата", "key", "2026-04-12"]:
        raise AssertionError("same-day overwrite must keep matrix header")
    if overwrite_sheet[2][2] != 0.25 or overwrite_sheet[3][2] != 10500:
        raise AssertionError("same-day overwrite must refresh current server-driven values")
    if overwrite_state["layout_mode"] != "date_matrix" or overwrite_state["data_row_count"] != len(overwrite_sheet) - 1:
        raise AssertionError("same-day overwrite must preserve date_matrix shape")

    blank_overwrite_sheet = harness_result["snapshots"]["after_same_day_blank_overwrite"]["values"]
    blank_overwrite_state = harness_result["states"]["same_day_blank_overwrite"]["sheets"][0]
    if blank_overwrite_sheet[0] != ["дата", "key", "2026-04-12"]:
        raise AssertionError("same-day blank overwrite must keep matrix header")
    if _row_by_key(blank_overwrite_sheet, "total_view_count")[2] != "":
        raise AssertionError("same-day blank overwrite must clear TOTAL total_view_count")
    if _row_by_key(blank_overwrite_sheet, "total_open_card_count")[2] != "":
        raise AssertionError("same-day blank overwrite must clear TOTAL total_open_card_count")
    if _row_by_key(blank_overwrite_sheet, "view_count")[2] != "":
        raise AssertionError("same-day blank overwrite must clear the targeted SKU/group view_count row")
    if _row_by_key(blank_overwrite_sheet, "open_card_count")[2] != "":
        raise AssertionError("same-day blank overwrite must clear the targeted SKU/group open_card_count row")
    if _row_by_key(blank_overwrite_sheet, "total_proxy_profit_rub")[2] != 10500:
        raise AssertionError("same-day blank overwrite must not erase unrelated metrics")
    if blank_overwrite_state["layout_mode"] != "date_matrix":
        raise AssertionError("same-day blank overwrite must preserve date_matrix shape")

    next_day_sheet = harness_result["snapshots"]["after_next_day_overwrite"]["values"]
    next_day_state = harness_result["states"]["next_day_overwrite"]["sheets"][0]
    if next_day_sheet[0] != ["дата", "key", "2026-04-12", "2026-04-13"]:
        raise AssertionError("next day load must append a new date column to the right")
    if next_day_sheet[2][2] != 0.25 or next_day_sheet[2][3] != 0.3:
        raise AssertionError("next day load must preserve history and append new values")
    if next_day_sheet[3][2] != 10500 or next_day_sheet[3][3] != 11000:
        raise AssertionError("next day overwrite must append refreshed money-like values")
    if _row_by_key(next_day_sheet, "total_view_count")[2] != "" or _row_by_key(next_day_sheet, "total_view_count")[3] != 2000:
        raise AssertionError("next day overwrite must preserve the blank-cleared day and append the new total")
    if _row_by_key(next_day_sheet, "total_open_card_count")[2] != "" or _row_by_key(next_day_sheet, "total_open_card_count")[3] != 1250:
        raise AssertionError("next day overwrite must preserve the blank-cleared open_card_count day")
    if next_day_state["layout_mode"] != "date_matrix" or next_day_state["metric_key_count"] != first_state["metric_key_count"]:
        raise AssertionError("next day overwrite must preserve the full metric set")
    if next_day_state["date_column_count"] != 2:
        raise AssertionError("next day overwrite must keep two date columns in matrix mode")

    print(f"first_load: ok -> blocks={first_state['block_key_count']} metric_keys={first_state['metric_key_count']}")
    print("same_day_overwrite: ok -> 2026-04-12")
    print("same_day_blank_overwrite: ok -> blank clears stale same-day cells")
    print("next_day_overwrite: ok -> 2026-04-12 + 2026-04-13")
    print("presentation_formats: ok -> integer/percent/decimal")
    print("smoke-check passed")


if __name__ == "__main__":
    main()
