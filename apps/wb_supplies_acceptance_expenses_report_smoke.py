"""Fixture smoke for WB Acceptance Expenses report matching."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.wb_supplies_acceptance_expenses_diagnostics import summarize_acceptance_expenses  # noqa: E402


def main() -> None:
    rows = [
        {
            "incomeId": 39265492,
            "nmID": 210183142,
            "count": 20,
            "total": 5000.00,
            "giCreateDate": "2026-05-14",
            "shkCreateDate": "2026-05-15",
        },
        {
            "incomeId": 39265492,
            "nmID": 210183143,
            "count": 30,
            "total": 6543.52,
            "giCreateDate": "2026-05-14",
            "shkCreateDate": "2026-05-15",
        },
        {
            "incomeId": 39265519,
            "nmID": 210183144,
            "count": 40,
            "total": 15523.72,
            "giCreateDate": "2026-05-15",
            "shkCreateDate": "2026-05-16",
        },
    ]
    summary = summarize_acceptance_expenses(
        rows,
        target_values=(15523.72, 11543.52, 14062.54, 10726.11),
        target_income_ids=("39265519", "39265492", "39265590", "39265571"),
    )
    if summary["target_income_totals"]["39265492"]["total"] != 11543.52:
        raise AssertionError(f"incomeId aggregate must match target value, got {summary}")
    if summary["target_income_totals"]["39265519"]["target_value_match"] is not True:
        raise AssertionError(f"direct row target value must match, got {summary}")
    if not summary["row_value_matches"] or summary["row_value_matches"][0]["incomeId"] != 39265519:
        raise AssertionError(f"row value match must preserve compact evidence, got {summary}")
    if not any(item["incomeId"] == "39265492" for item in summary["aggregate_value_matches"]):
        raise AssertionError(f"aggregate match must expose incomeId join evidence, got {summary}")
    print("wb_supplies_acceptance_expenses_report_smoke: OK")


if __name__ == "__main__":
    main()
