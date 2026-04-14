"""Targeted smoke-check для server-driven materialization в DATA_VITRINA."""

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

    if first_sheet[0] != ["label", "key", "2026-04-12"]:
        raise AssertionError("DATA_VITRINA must keep flat server-driven header")
    expected_prefix = [
        ["Итого: Маржинальность прокси, %", "TOTAL|proxy_margin_pct_total", 0.2],
        ["Итого: Прокси-прибыль, ₽", "TOTAL|total_proxy_profit_rub", 10000],
        ["Итого: Показы в воронке", "TOTAL|total_view_count", 1000],
        ["Итого: Открытия карточки", "TOTAL|total_open_card_count", 250],
    ]
    if first_sheet[1:5] != expected_prefix:
        raise AssertionError("unexpected server-driven prefix after first load")

    if first_state["layout_mode"] != "flat_rows":
        raise AssertionError("DATA_VITRINA must stay in flat_rows mode")
    if first_state["metric_key_count"] <= 7:
        raise AssertionError("server-driven materialization must keep more than 7 metric keys")
    if "proxy_profit_rub" not in first_state["metric_keys"]:
        raise AssertionError("server-driven materialization lost proxy_profit_rub")
    if first_state["data_row_count"] != len(first_sheet) - 1:
        raise AssertionError("DATA_VITRINA data_row_count mismatch")
    if first_state["scope_row_counts"] != {"TOTAL": 9, "GROUP": 20, "SKU": 22, "OTHER": 0}:
        raise AssertionError("DATA_VITRINA scope_row_counts mismatch")

    if first_presentation["frozen_columns"] != 2:
        raise AssertionError("DATA_VITRINA must keep frozen A:B columns")
    if first_presentation["header_style"]["background"] != "#ffffff":
        raise AssertionError("DATA_VITRINA header must not keep dark fill")
    if first_presentation["header_style"]["date_number_format"] != "dd.mm.yyyy":
        raise AssertionError("date header format mismatch")
    if first_presentation["column_widths"]["A"] != 280 or first_presentation["column_widths"]["B"] != 220:
        raise AssertionError("base DATA_VITRINA widths mismatch")
    if first_presentation["samples"]["section"] is not None:
        raise AssertionError("flat server-driven view must not invent section rows")
    if first_presentation["samples"]["percent"]["number_format"] != "0.0%":
        raise AssertionError("CTR rows must keep percent format")
    if first_presentation["samples"]["decimal"]["number_format"] != "#,##0.00":
        raise AssertionError("position_avg rows must keep decimal format")
    if first_presentation["samples"]["integer"]["number_format"] != "#,##0":
        raise AssertionError("count-like rows must keep integer format")

    overwrite_sheet = harness_result["snapshots"]["after_same_day_overwrite"]["values"]
    overwrite_state = harness_result["states"]["same_day_overwrite"]["sheets"][0]
    if overwrite_sheet[0] != ["label", "key", "2026-04-12"]:
        raise AssertionError("same-day overwrite must keep flat header")
    if overwrite_sheet[1][2] != 0.25 or overwrite_sheet[2][2] != 10500:
        raise AssertionError("same-day overwrite must refresh current server-driven values")
    if overwrite_state["layout_mode"] != "flat_rows" or overwrite_state["data_row_count"] != len(overwrite_sheet) - 1:
        raise AssertionError("same-day overwrite must preserve flat_rows shape")

    next_day_sheet = harness_result["snapshots"]["after_next_day_overwrite"]["values"]
    next_day_state = harness_result["states"]["next_day_overwrite"]["sheets"][0]
    if next_day_sheet[0] != ["label", "key", "2026-04-13"]:
        raise AssertionError("next day load must overwrite header date from server plan")
    if len(next_day_sheet[0]) != 3:
        raise AssertionError("next day load must not invent presentation-side history columns")
    if next_day_sheet[1][2] != 0.3 or next_day_sheet[2][2] != 11000:
        raise AssertionError("next day overwrite must refresh server-driven values")
    if next_day_state["layout_mode"] != "flat_rows" or next_day_state["metric_key_count"] != first_state["metric_key_count"]:
        raise AssertionError("next day overwrite must preserve full metric set")

    print(f"first_load: ok -> metric_keys={first_state['metric_key_count']}")
    print("same_day_overwrite: ok -> 2026-04-12")
    print("next_day_overwrite: ok -> 2026-04-13")
    print("presentation_formats: ok -> integer/percent/decimal")
    print("smoke-check passed")


if __name__ == "__main__":
    main()
