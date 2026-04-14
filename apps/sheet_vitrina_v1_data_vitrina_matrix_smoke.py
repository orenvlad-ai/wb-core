"""Targeted smoke-check для matrix-layout reverse-load в DATA_VITRINA."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    harness_result = json.loads(
        subprocess.check_output(
            [
                "node",
                str(ROOT / "apps" / "sheet_vitrina_v1_registry_upload_trigger_harness.js"),
                "--mode",
                "matrix_layout",
                "--scriptPath",
                str(ROOT / "gas" / "sheet_vitrina_v1" / "RegistryUploadTrigger.gs"),
            ],
            cwd=ROOT,
            text=True,
        )
    )

    flat_seed = harness_result["snapshots"]["after_flat_seed"]["values"]
    if flat_seed[0] != ["label", "key", "2026-04-12"]:
        raise AssertionError("flat seed header mismatch")

    migrated_sheet = harness_result["snapshots"]["after_migration"]["values"]
    migrated_state = harness_result["states"]["migrated"]["sheets"][0]
    migrated_presentation = harness_result["presentations"]["migrated"]["sheets"][0]

    if migrated_sheet[0] != ["дата", "key", "2026-04-12"]:
        raise AssertionError("matrix header mismatch after migration")
    expected_prefix = [
        ["ИТОГО", "TOTAL", ""],
        ["Показы в воронке", "view_count", 1000],
        ["CTR открытия карточки", "ctr", 0.25],
        ["Открытия карточки", "open_card_count", 250],
        ["Показы в поиске", "views_current", 850],
        ["CTR в поиске", "ctr_current", 0.23],
        ["Заказы в поиске", "orders_current", 75],
        ["Средняя позиция в поиске", "position_avg", 4.25],
        ["", "", ""],
        ["ГРУППА: Clean", "GROUP:Clean", ""],
    ]
    if migrated_sheet[1:11] != expected_prefix:
        raise AssertionError("unexpected matrix prefix after migration")

    if migrated_state["layout_mode"] != "date_matrix":
        raise AssertionError("DATA_VITRINA must switch to date_matrix mode")
    if migrated_state["metric_key_count"] != 7:
        raise AssertionError("matrix layout must keep the bounded 7-metric subset")
    if migrated_state["block_key_count"] != 5:
        raise AssertionError("matrix layout must keep stable logical blocks")
    if migrated_state["metric_row_count"] != 35:
        raise AssertionError("matrix layout metric_row_count mismatch")
    if migrated_state["section_row_count"] != 5 or migrated_state["separator_row_count"] != 4:
        raise AssertionError("matrix layout row partition mismatch")

    if migrated_presentation["frozen_columns"] != 2:
        raise AssertionError("DATA_VITRINA must keep frozen A:B columns")
    if migrated_presentation["header_style"]["background"] != "#ffffff":
        raise AssertionError("DATA_VITRINA header must not keep dark fill")
    if migrated_presentation["header_style"]["date_number_format"] != "dd.mm.yyyy":
        raise AssertionError("date header format mismatch")
    if migrated_presentation["column_widths"]["A"] != 280 or migrated_presentation["column_widths"]["B"] != 220:
        raise AssertionError("base DATA_VITRINA widths mismatch")
    if migrated_presentation["samples"]["section"]["number_format"] != "@":
        raise AssertionError("section rows must keep text format")
    if migrated_presentation["samples"]["percent"]["number_format"] != "0.0%":
        raise AssertionError("CTR rows must keep percent format")
    if migrated_presentation["samples"]["decimal"]["number_format"] != "#,##0.00":
        raise AssertionError("position_avg rows must keep decimal format")
    if migrated_presentation["samples"]["integer"]["number_format"] != "#,##0":
        raise AssertionError("count-like rows must keep integer format")

    overwrite_sheet = harness_result["snapshots"]["after_same_day_overwrite"]["values"]
    overwrite_state = harness_result["states"]["same_day_overwrite"]["sheets"][0]
    if overwrite_sheet[0] != ["дата", "key", "2026-04-12"]:
        raise AssertionError("same-day overwrite must not add a new date column")
    if overwrite_sheet[2][2] != 1500 or overwrite_sheet[3][2] != 0.5:
        raise AssertionError("same-day overwrite must refresh current date values")
    if overwrite_state["date_column_count"] != 1 or overwrite_state["data_row_count"] != 44:
        raise AssertionError("same-day overwrite must preserve matrix shape")

    appended_sheet = harness_result["snapshots"]["after_next_day_append"]["values"]
    appended_state = harness_result["states"]["next_day_append"]["sheets"][0]
    if appended_sheet[0] != ["дата", "key", "2026-04-12", "2026-04-13"]:
        raise AssertionError("next day must append a date column to the right")
    if appended_sheet[2][2:] != [1500, 2000]:
        raise AssertionError("append-right must preserve previous day and add next day value")
    if appended_sheet[3][2:] != [0.5, 0.625]:
        raise AssertionError("append-right percent values mismatch")
    if appended_state["date_column_count"] != 2 or appended_state["data_row_count"] != 44:
        raise AssertionError("append-right must not grow rows downward")

    print("flat_to_matrix_migration: ok -> DATA_VITRINA")
    print("same_day_overwrite: ok -> 2026-04-12")
    print("append_right_history: ok -> 2026-04-13")
    print("presentation_formats: ok -> section/integer/percent/decimal")
    print("smoke-check passed")


if __name__ == "__main__":
    main()
