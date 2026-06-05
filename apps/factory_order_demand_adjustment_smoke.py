"""Focused smoke-check for factory-order availability-adjusted demand."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.factory_order_supply import (
    _estimate_availability_adjusted_demand,
    _sales_lookup_days,
)


REPORT_DATE = date(2026, 5, 29)


def main() -> None:
    _assert_stable_sku_matches_calendar_average()
    _assert_stockout_tail_uses_older_valid_days()
    _assert_gradual_stockout_excludes_tail_days()
    _assert_low_velocity_ones_are_valid()
    _assert_insufficient_history_warns_without_failing()
    _assert_no_positive_sales_returns_zero()
    print("factory_order_demand_adjustment_smoke: ok")


def _samples_ending(values: list[float], *, report_date: date = REPORT_DATE) -> list[tuple[str, float]]:
    start = report_date - timedelta(days=len(values))
    return [
        ((start + timedelta(days=index)).isoformat(), float(value))
        for index, value in enumerate(values)
    ]


def _estimate(values: list[float], *, period_days: int):
    return _estimate_availability_adjusted_demand(
        _samples_ending(values),
        report_date=REPORT_DATE,
        sales_avg_period_days=period_days,
        sales_lookup_days=_sales_lookup_days(period_days),
    )


def _assert_stable_sku_matches_calendar_average() -> None:
    estimate = _estimate([10, 11, 9, 10, 12, 9, 10], period_days=7)
    if round(estimate.daily_demand_total, 2) != round(sum([10, 11, 9, 10, 12, 9, 10]) / 7, 2):
        raise AssertionError("stable SKU must keep the same average as the old calendar method")
    if estimate.valid_sales_day_count != 7 or estimate.demand_warning:
        raise AssertionError("stable SKU must collect all valid days without warning")


def _assert_stockout_tail_uses_older_valid_days() -> None:
    older_valid = [10, 11, 9, 10, 12, 9, 10]
    recent_valid = [10, 11, 9, 10, 12, 9, 10]
    stockout_tail = [3, 1, 0, 0, 0, 0, 0]
    estimate = _estimate(older_valid + recent_valid + stockout_tail, period_days=14)
    if round(estimate.raw_recent_daily_demand, 2) != 5.36:
        raise AssertionError("raw_recent_daily_demand must expose the old depressed calendar average")
    if round(estimate.daily_demand_total, 2) != 10.14:
        raise AssertionError("stockout tail must be replaced by older valid demand days")
    if estimate.valid_sales_day_count != 14 or estimate.excluded_low_sales_day_count != 7:
        raise AssertionError("stockout tail must collect 14 valid days and exclude the 7 low tail days")
    if estimate.demand_warning:
        raise AssertionError("stockout tail must not warn when enough valid history exists")


def _assert_gradual_stockout_excludes_tail_days() -> None:
    estimate = _estimate([100, 95, 105, 90, 70, 15, 3, 0], period_days=5)
    if round(estimate.baseline_daily_sales, 2) != 90.0:
        raise AssertionError("gradual stockout baseline must be the median positive sales")
    if round(estimate.valid_day_threshold, 2) != 40.5:
        raise AssertionError("gradual stockout threshold must be 45% of baseline")
    if round(estimate.daily_demand_total, 2) != 92.0:
        raise AssertionError("gradual stockout must keep 70 as valid and exclude 15/3/0")
    if estimate.excluded_low_sales_day_count != 3 or estimate.demand_warning:
        raise AssertionError("gradual stockout must exclude exactly the low tail without warning")


def _assert_low_velocity_ones_are_valid() -> None:
    estimate = _estimate([2, 0, 1, 0, 1], period_days=3)
    if round(estimate.baseline_daily_sales, 2) != 1.0 or round(estimate.valid_day_threshold, 2) != 1.0:
        raise AssertionError("low-velocity baseline must make one-sale days valid")
    if round(estimate.daily_demand_total, 2) != 1.33 or estimate.valid_sales_day_count != 3:
        raise AssertionError("low-velocity SKU must collect 1-sale days as valid demand")
    if estimate.demand_warning:
        raise AssertionError("low-velocity SKU must not warn when enough valid days exist")


def _assert_insufficient_history_warns_without_failing() -> None:
    estimate = _estimate([0, 0, 12, 0], period_days=5)
    if estimate.valid_sales_day_count != 1 or round(estimate.daily_demand_total, 2) != 12.0:
        raise AssertionError("insufficient history must still calculate from found valid days")
    if "Собрано 1 валидных" not in estimate.demand_warning:
        raise AssertionError("insufficient history must return a truthful row-level warning")


def _assert_no_positive_sales_returns_zero() -> None:
    estimate = _estimate([0, 0, 0, 0], period_days=3)
    if estimate.daily_demand_total != 0.0 or estimate.valid_sales_day_count != 0:
        raise AssertionError("no positive sales must return zero demand without crashing")
    if "Нет положительных orderCount" not in estimate.demand_warning:
        raise AssertionError("no positive sales must explain the zero-demand diagnostic")


if __name__ == "__main__":
    main()
