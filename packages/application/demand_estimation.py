"""Shared availability-adjusted demand estimation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median
from typing import Any


DEFAULT_SALES_AVG_PERIOD_DAYS = 14
DEMAND_ESTIMATION_MODE = "availability_adjusted"
DEMAND_LOOKUP_DAY_CAP = 120
DEMAND_VALID_DAY_BASELINE_RATIO = 0.45


@dataclass(frozen=True)
class DemandEstimate:
    daily_demand_total: float
    demand_estimation_mode: str
    sales_avg_period_days: int
    sales_lookup_days: int
    sales_calendar_day_count: int
    valid_sales_day_count: int
    excluded_low_sales_day_count: int
    baseline_daily_sales: float
    valid_day_threshold: float
    raw_recent_daily_demand: float
    earliest_used_sales_date: str
    latest_used_sales_date: str
    demand_warning: str
    demand_notes: tuple[str, ...]


@dataclass(frozen=True)
class WindowedDemandEstimate:
    """Availability-adjusted demand constrained to one exact calendar window."""

    daily_demand_total: float
    demand_estimation_mode: str
    date_from: str
    date_to: str
    calendar_day_count: int
    used_trading_day_count: int
    excluded_day_count: int
    included_dates: tuple[str, ...]
    excluded_dates: tuple[str, ...]
    baseline_daily_sales: float
    valid_day_threshold: float
    raw_window_daily_demand: float
    demand_warning: str
    demand_notes: tuple[str, ...]


def parse_sales_avg_period_days(value: Any) -> int:
    if value in ("", None):
        return DEFAULT_SALES_AVG_PERIOD_DAYS
    try:
        numeric = int(str(value).strip())
    except ValueError as exc:
        raise ValueError("Период усреднения продаж должен быть целым числом") from exc
    if numeric <= 0:
        return DEFAULT_SALES_AVG_PERIOD_DAYS
    return numeric


def sales_lookup_days(sales_avg_period_days: int) -> int:
    return min(
        DEMAND_LOOKUP_DAY_CAP,
        max(sales_avg_period_days, sales_avg_period_days * 4),
    )


def estimate_availability_adjusted_demand(
    samples_by_date: list[tuple[str, float]],
    *,
    report_date: date,
    sales_avg_period_days: int,
    sales_lookup_days: int,
) -> DemandEstimate:
    samples = sorted(
        [(str(snapshot_date), float(value)) for snapshot_date, value in samples_by_date],
        key=lambda item: item[0],
    )
    recent_from = report_date - timedelta(days=sales_avg_period_days)
    recent_samples = [
        value
        for snapshot_date, value in samples
        if recent_from <= date.fromisoformat(snapshot_date) < report_date
    ]
    raw_recent_daily_demand = (
        sum(recent_samples) / len(recent_samples)
        if recent_samples
        else 0.0
    )
    positive_samples = [value for _, value in samples if value > 0]
    if not positive_samples:
        warning = (
            f"Нет положительных orderCount samples в bounded lookup window "
            f"для {sales_avg_period_days} валидных дней."
        )
        return DemandEstimate(
            daily_demand_total=0.0,
            demand_estimation_mode=DEMAND_ESTIMATION_MODE,
            sales_avg_period_days=sales_avg_period_days,
            sales_lookup_days=sales_lookup_days,
            sales_calendar_day_count=len(samples),
            valid_sales_day_count=0,
            excluded_low_sales_day_count=0,
            baseline_daily_sales=0.0,
            valid_day_threshold=0.0,
            raw_recent_daily_demand=raw_recent_daily_demand,
            earliest_used_sales_date="",
            latest_used_sales_date="",
            demand_warning=warning,
            demand_notes=("no_positive_order_count_samples_in_lookup_window",),
        )

    baseline_daily_sales = float(median(positive_samples))
    valid_day_threshold = max(1.0, baseline_daily_sales * DEMAND_VALID_DAY_BASELINE_RATIO)
    valid_samples: list[tuple[str, float]] = []
    excluded_low_sales_day_count = 0
    sales_calendar_day_count = 0
    for snapshot_date, value in reversed(samples):
        if len(valid_samples) >= sales_avg_period_days:
            break
        sales_calendar_day_count += 1
        if value >= valid_day_threshold:
            valid_samples.append((snapshot_date, value))
        else:
            excluded_low_sales_day_count += 1

    valid_values = [value for _, value in valid_samples]
    daily_demand_total = sum(valid_values) / len(valid_values) if valid_values else 0.0
    used_dates = sorted(snapshot_date for snapshot_date, _ in valid_samples)
    warning = ""
    notes: list[str] = []
    if len(valid_samples) < sales_avg_period_days:
        warning = (
            f"Собрано {len(valid_samples)} валидных торговых дней из {sales_avg_period_days} "
            f"в lookup window {sales_lookup_days} дней; demand рассчитан по доступным valid days."
        )
        notes.append("insufficient_valid_sales_days")
    else:
        notes.append("collected_requested_valid_sales_days")
    if excluded_low_sales_day_count:
        notes.append("excluded_low_sales_days_below_threshold")
    return DemandEstimate(
        daily_demand_total=daily_demand_total,
        demand_estimation_mode=DEMAND_ESTIMATION_MODE,
        sales_avg_period_days=sales_avg_period_days,
        sales_lookup_days=sales_lookup_days,
        sales_calendar_day_count=sales_calendar_day_count,
        valid_sales_day_count=len(valid_samples),
        excluded_low_sales_day_count=excluded_low_sales_day_count,
        baseline_daily_sales=baseline_daily_sales,
        valid_day_threshold=valid_day_threshold,
        raw_recent_daily_demand=raw_recent_daily_demand,
        earliest_used_sales_date=used_dates[0] if used_dates else "",
        latest_used_sales_date=used_dates[-1] if used_dates else "",
        demand_warning=warning,
        demand_notes=tuple(notes),
    )


def estimate_availability_adjusted_demand_for_window(
    samples_by_date: list[tuple[str, float]],
    *,
    date_from: date,
    date_to: date,
) -> WindowedDemandEstimate:
    """Estimate demand without borrowing samples outside ``date_from..date_to``.

    Source coverage is validated by the authoritative history reader before this
    helper is called.  This function only applies the shared stockout/low-sales
    filter inside the already selected inclusive window.
    """

    if date_to < date_from:
        raise ValueError("date_to must be >= date_from")
    samples = sorted(
        (
            (str(snapshot_date), float(value))
            for snapshot_date, value in samples_by_date
            if date_from <= date.fromisoformat(str(snapshot_date)) <= date_to
        ),
        key=lambda item: item[0],
    )
    calendar_day_count = (date_to - date_from).days + 1
    raw_window_daily_demand = (
        sum(value for _, value in samples) / len(samples) if samples else 0.0
    )
    positive_samples = [value for _, value in samples if value > 0]
    if not positive_samples:
        return WindowedDemandEstimate(
            daily_demand_total=0.0,
            demand_estimation_mode=DEMAND_ESTIMATION_MODE,
            date_from=date_from.isoformat(),
            date_to=date_to.isoformat(),
            calendar_day_count=calendar_day_count,
            used_trading_day_count=0,
            excluded_day_count=len(samples),
            included_dates=(),
            excluded_dates=tuple(snapshot_date for snapshot_date, _ in samples),
            baseline_daily_sales=0.0,
            valid_day_threshold=0.0,
            raw_window_daily_demand=raw_window_daily_demand,
            demand_warning=(
                "В выбранном периоде нет положительных orderCount; "
                "расчёт спроса для SKU недоступен."
            ),
            demand_notes=("no_positive_order_count_samples_in_selected_window",),
        )

    baseline_daily_sales = float(median(positive_samples))
    valid_day_threshold = max(
        1.0,
        baseline_daily_sales * DEMAND_VALID_DAY_BASELINE_RATIO,
    )
    included = [
        (snapshot_date, value)
        for snapshot_date, value in samples
        if value >= valid_day_threshold
    ]
    excluded = [
        (snapshot_date, value)
        for snapshot_date, value in samples
        if value < valid_day_threshold
    ]
    daily_demand_total = (
        sum(value for _, value in included) / len(included) if included else 0.0
    )
    notes: list[str] = ["selected_window_only_no_external_day_backfill"]
    warning = ""
    if excluded:
        notes.append("excluded_low_sales_days_below_threshold")
        warning = (
            f"Использовано {len(included)} торговых дней из {calendar_day_count}; "
            f"{len(excluded)} подозрительно низких дней исключено только внутри "
            f"периода {date_from.isoformat()}..{date_to.isoformat()}."
        )
    else:
        notes.append("all_selected_window_days_used")
    return WindowedDemandEstimate(
        daily_demand_total=daily_demand_total,
        demand_estimation_mode=DEMAND_ESTIMATION_MODE,
        date_from=date_from.isoformat(),
        date_to=date_to.isoformat(),
        calendar_day_count=calendar_day_count,
        used_trading_day_count=len(included),
        excluded_day_count=len(excluded),
        included_dates=tuple(snapshot_date for snapshot_date, _ in included),
        excluded_dates=tuple(snapshot_date for snapshot_date, _ in excluded),
        baseline_daily_sales=baseline_daily_sales,
        valid_day_threshold=valid_day_threshold,
        raw_window_daily_demand=raw_window_daily_demand,
        demand_warning=warning,
        demand_notes=tuple(notes),
    )
