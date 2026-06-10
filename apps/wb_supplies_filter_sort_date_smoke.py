"""Focused smoke for WB supplies filters, date display, and empty-date sorting."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.wb_supplies import (  # noqa: E402
    _format_ru_supply_date,
    _format_ru_supply_date_range,
    _normalize_list_request,
    _row_matches_size_filter,
    _sort_rows,
)
from packages.business_time import current_business_date_iso  # noqa: E402


def main() -> None:
    interface_year = int(current_business_date_iso()[:4])
    previous_year = interface_year - 1

    current_date = date(interface_year, 5, 15)
    previous_date = date(previous_year, 5, 15)
    if _format_ru_supply_date(current_date, interface_year=interface_year) != "15 мая":
        raise AssertionError("current-year display must omit year")
    if _format_ru_supply_date(previous_date, interface_year=interface_year) != f"15 мая {previous_year}":
        raise AssertionError("previous-year display must include year")
    if _format_ru_supply_date_range(
        date(previous_year, 12, 31),
        date(interface_year, 1, 1),
        interface_year=interface_year,
    ) != f"31 декабря {previous_year} → 1 января {interface_year}":
        raise AssertionError("cross-year range must include years on both sides")
    if _format_ru_supply_date_range(
        date(previous_year, 5, 15),
        date(previous_year, 5, 16),
        interface_year=interface_year,
    ) != f"15 мая {previous_year} → 16 мая {previous_year}":
        raise AssertionError("old same-year range must include year on both sides")

    rows = [
        {
            "visible_number": "status1-no-date",
            "status_id": 1,
            "updated_date": f"{interface_year}-06-11T12:00:00+03:00",
        },
        {
            "visible_number": "planned-dated",
            "status_id": 2,
            "supply_date": f"{interface_year}-06-30T00:00:00+03:00",
        },
        {
            "visible_number": "accepted-dated",
            "status_id": 5,
            "supply_date": f"{interface_year}-05-15T00:00:00+03:00",
        },
        {
            "visible_number": "accepted-no-date",
            "status_id": 5,
            "updated_date": f"{interface_year}-06-12T12:00:00+03:00",
        },
    ]
    desc_ids = [row["visible_number"] for row in _sort_rows(rows, "supply_date", "desc")]
    if desc_ids != ["planned-dated", "accepted-dated", "accepted-no-date", "status1-no-date"]:
        raise AssertionError(f"empty-date rows must sort after dated rows for desc, got {desc_ids}")
    asc_ids = [row["visible_number"] for row in _sort_rows(rows, "supply_date", "asc")]
    if asc_ids != ["accepted-dated", "planned-dated", "accepted-no-date", "status1-no-date"]:
        raise AssertionError(f"empty-date rows must sort after dated rows for asc, got {asc_ids}")

    request = _normalize_list_request({"status_ids": "2,5", "status_id": "6"})
    if request["status_ids"] != [2, 5, 6]:
        raise AssertionError(f"status_ids parser must combine comma list and legacy status_id, got {request}")

    planned_qty_1 = {"status_id": 2, "quantity_for_size_filter": 1}
    planned_qty_300 = {"status_id": 2, "quantity_for_size_filter": 300}
    planned_unknown = {"status_id": 2, "quantity_for_size_filter": None}
    if not _row_matches_size_filter(planned_qty_1, "all"):
        raise AssertionError("planned qty=1 must be visible in all")
    if not _row_matches_size_filter(planned_qty_1, "small_lt_250"):
        raise AssertionError("planned qty=1 must be visible in small_lt_250")
    if _row_matches_size_filter(planned_qty_1, "main_250"):
        raise AssertionError("planned qty=1 must be hidden from main_250")
    if not _row_matches_size_filter(planned_qty_300, "main_250") or _row_matches_size_filter(planned_qty_300, "small_lt_250"):
        raise AssertionError("planned qty=300 must be main only, not small")
    if not _row_matches_size_filter(planned_unknown, "all"):
        raise AssertionError("planned unknown quantity must be visible in all")
    if _row_matches_size_filter(planned_unknown, "main_250") or _row_matches_size_filter(planned_unknown, "small_lt_250"):
        raise AssertionError("planned unknown quantity must be hidden from numeric size filters")

    print("wb_supplies_filter_sort_date_smoke: OK")


if __name__ == "__main__":
    main()
