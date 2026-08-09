"""Read-side buyout-percent projection over accepted sales-funnel snapshots."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
)
from packages.business_time import CANONICAL_BUSINESS_TIMEZONE_NAME
from packages.contracts.registry_upload_bundle_v1 import MetricV2Item


SALES_FUNNEL_HISTORY_SOURCE_KEY = "sales_funnel_history"
BUYOUT_PERCENT_METRIC_KEY = "buyoutPercent"
LEGACY_AVG_BUYOUT_PERCENT_METRIC_KEY = "avg_buyoutPercent"
ORDER_COUNT_METRIC_KEY = "orderCount"
BUYOUT_PERCENT_LABEL_RU = "Процент выкупа"
THREE_CLOSED_WEEKS_BUYOUT_LABEL_RU = "Процент выкупа за 3 закрытые недели"
BUYOUT_PERCENT_AGGREGATION_RULE = (
    "SUM(buyoutPercent * orderCount) / SUM(orderCount)"
)


@dataclass(frozen=True)
class BuyoutPercentSnapshotValue:
    value: float
    captured_at: str


@dataclass(frozen=True)
class BuyoutPercentSnapshotMetrics:
    buyout_percent: Decimal | None
    order_count: Decimal | None
    captured_at: str


@dataclass(frozen=True)
class BuyoutPercentAggregation:
    value: Decimal | None
    order_count_weight: Decimal
    included_pair_count: int


def extend_metrics_with_buyout_percent(
    metrics: Iterable[MetricV2Item],
) -> list[MetricV2Item]:
    """Make the existing official-api SKU metric effective in the public catalog."""

    existing = list(metrics)
    result: list[MetricV2Item] = []
    found = False
    for metric in existing:
        if metric.metric_key == LEGACY_AVG_BUYOUT_PERCENT_METRIC_KEY:
            result.append(
                replace(
                    metric,
                    enabled=False,
                    show_in_data=False,
                )
            )
            continue
        if metric.metric_key != BUYOUT_PERCENT_METRIC_KEY:
            result.append(metric)
            continue
        found = True
        result.append(
            replace(
                metric,
                enabled=True,
                scope="SKU",
                show_in_data=True,
                format="percent",
            )
        )
    if not found:
        result.append(
            MetricV2Item(
                metric_key=BUYOUT_PERCENT_METRIC_KEY,
                enabled=True,
                scope="SKU",
                label_ru=BUYOUT_PERCENT_LABEL_RU,
                calc_type="metric",
                calc_ref=BUYOUT_PERCENT_METRIC_KEY,
                show_in_data=True,
                format="percent",
                display_order=610,
                section="Воронка",
            )
        )
    return result


def load_buyout_percent_snapshot_values(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    snapshot_dates: Sequence[str],
    nm_ids: Iterable[int] | None = None,
) -> dict[int, dict[str, BuyoutPercentSnapshotValue]]:
    """Read official normalized buyoutPercent values from exact-date snapshots."""

    metrics_by_nm_id = load_buyout_percent_snapshot_metrics(
        runtime=runtime,
        snapshot_dates=snapshot_dates,
        nm_ids=nm_ids,
    )
    return {
        nm_id: {
            snapshot_date: BuyoutPercentSnapshotValue(
                value=float(metrics.buyout_percent),
                captured_at=metrics.captured_at,
            )
            for snapshot_date, metrics in values_by_date.items()
            if metrics.buyout_percent is not None
        }
        for nm_id, values_by_date in metrics_by_nm_id.items()
        if any(metrics.buyout_percent is not None for metrics in values_by_date.values())
    }


def load_buyout_percent_snapshot_metrics(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    snapshot_dates: Sequence[str],
    nm_ids: Iterable[int] | None = None,
) -> dict[int, dict[str, BuyoutPercentSnapshotMetrics]]:
    """Read valid buyoutPercent/orderCount pairs from exact-date snapshots."""

    requested_nm_ids = (
        {int(nm_id) for nm_id in nm_ids}
        if nm_ids is not None
        else None
    )
    values: dict[int, dict[str, BuyoutPercentSnapshotMetrics]] = {}
    for snapshot_date in dict.fromkeys(str(item) for item in snapshot_dates):
        payload, captured_at = runtime.load_temporal_source_snapshot(
            source_key=SALES_FUNNEL_HISTORY_SOURCE_KEY,
            snapshot_date=snapshot_date,
        )
        for nm_id, metrics in _snapshot_metrics_by_nm_id(
            _successful_snapshot_items(payload),
            snapshot_date=snapshot_date,
            requested_nm_ids=requested_nm_ids,
        ).items():
            values.setdefault(nm_id, {})[snapshot_date] = BuyoutPercentSnapshotMetrics(
                buyout_percent=metrics.get(BUYOUT_PERCENT_METRIC_KEY),
                order_count=metrics.get(ORDER_COUNT_METRIC_KEY),
                captured_at=str(captured_at or ""),
            )
    return values


def aggregate_buyout_percent(
    pairs: Iterable[tuple[Any, Any]],
) -> BuyoutPercentAggregation:
    """Return the orderCount-weighted normalized fraction for valid pairs only."""

    weighted_sum = Decimal("0")
    order_count_sum = Decimal("0")
    included_pair_count = 0
    for raw_buyout_percent, raw_order_count in pairs:
        buyout_percent = _fraction(raw_buyout_percent)
        order_count = _positive_decimal(raw_order_count)
        if buyout_percent is None or order_count is None:
            continue
        weighted_sum += buyout_percent * order_count
        order_count_sum += order_count
        included_pair_count += 1
    return BuyoutPercentAggregation(
        value=(weighted_sum / order_count_sum if order_count_sum > 0 else None),
        order_count_weight=order_count_sum,
        included_pair_count=included_pair_count,
    )


def three_closed_week_keys(today: date) -> list[tuple[str, str]]:
    """Return the last three complete Monday-Sunday windows before today."""

    last_closed_sunday = today - timedelta(days=today.weekday() + 1)
    return [
        (
            (last_closed_sunday - timedelta(days=(2 - index) * 7 + 6)).isoformat(),
            (last_closed_sunday - timedelta(days=(2 - index) * 7)).isoformat(),
        )
        for index in range(3)
    ]


def build_three_closed_week_buyout_reference(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    today: date,
) -> dict[str, Any]:
    """Weight all available valid SKU-day buyout values by orderCount."""

    week_keys = three_closed_week_keys(today)
    requested_dates = list(_iter_iso_dates(week_keys[0][0], week_keys[-1][1]))
    pairs: list[tuple[Decimal | None, Decimal | None]] = []
    available_snapshot_dates: list[str] = []

    for snapshot_date in requested_dates:
        payload, _captured_at = runtime.load_temporal_source_snapshot(
            source_key=SALES_FUNNEL_HISTORY_SOURCE_KEY,
            snapshot_date=snapshot_date,
        )
        items = _successful_snapshot_items(payload)
        if not items:
            continue
        available_snapshot_dates.append(snapshot_date)
        pairs.extend(
            (
                metrics.get(BUYOUT_PERCENT_METRIC_KEY),
                metrics.get(ORDER_COUNT_METRIC_KEY),
            )
            for metrics in _snapshot_metrics_by_nm_id(
                items,
                snapshot_date=snapshot_date,
            ).values()
        )

    aggregation = aggregate_buyout_percent(pairs)
    weighted_average = aggregation.value
    included_sku_day_count = aggregation.included_pair_count
    order_count_sum = aggregation.order_count_weight
    return {
        "key": "buyout_percent_three_closed_weeks",
        "label": THREE_CLOSED_WEEKS_BUYOUT_LABEL_RU,
        "status": "ready" if weighted_average is not None else "unavailable",
        "weighted_average_pct": (
            _decimal_text(weighted_average * Decimal("100"))
            if weighted_average is not None
            else None
        ),
        "date_from": week_keys[0][0],
        "date_to": week_keys[-1][1],
        "weeks": [
            {"week_start": week_start, "week_end": week_end}
            for week_start, week_end in week_keys
        ],
        "business_timezone": CANONICAL_BUSINESS_TIMEZONE_NAME,
        "source_key": SALES_FUNNEL_HISTORY_SOURCE_KEY,
        "source_store": "temporal_source_snapshots",
        "value_metric": BUYOUT_PERCENT_METRIC_KEY,
        "weight_metric": ORDER_COUNT_METRIC_KEY,
        "aggregation_rule": BUYOUT_PERCENT_AGGREGATION_RULE,
        "included_sku_day_count": included_sku_day_count,
        "order_count_weight": _decimal_text(order_count_sum),
        "available_snapshot_day_count": len(available_snapshot_dates),
        "available_snapshot_dates": available_snapshot_dates,
        "status_message": (
            f"Рассчитано по {included_sku_day_count} доступным SKU-day значениям."
            if weighted_average is not None
            else "Нет SKU-day строк с валидными buyoutPercent и положительным orderCount."
        ),
    }


def _successful_snapshot_items(payload: Any) -> list[Any]:
    if payload is None:
        return []
    result = _item_value(payload, "result")
    candidate = result if result is not None else payload
    if _item_text(candidate, "kind") != "success":
        return []
    items = _item_value(candidate, "items")
    return list(items) if isinstance(items, (list, tuple)) else []


def _snapshot_metrics_by_nm_id(
    items: Iterable[Any],
    *,
    snapshot_date: str,
    requested_nm_ids: set[int] | None = None,
) -> dict[int, dict[str, Decimal]]:
    by_nm_id: dict[int, dict[str, Decimal]] = {}
    for item in items:
        if _item_text(item, "date") != snapshot_date:
            continue
        nm_id = _item_int(item, "nm_id")
        metric = _item_text(item, "metric")
        if (
            nm_id is None
            or (requested_nm_ids is not None and nm_id not in requested_nm_ids)
            or metric not in {BUYOUT_PERCENT_METRIC_KEY, ORDER_COUNT_METRIC_KEY}
        ):
            continue
        value = (
            _fraction(_item_value(item, "value"))
            if metric == BUYOUT_PERCENT_METRIC_KEY
            else _positive_decimal(_item_value(item, "value"))
        )
        if value is not None:
            by_nm_id.setdefault(nm_id, {})[metric] = value
    return by_nm_id


def _item_value(item: Any, field: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(field)
    return getattr(item, field, None)


def _item_text(item: Any, field: str) -> str:
    return str(_item_value(item, field) or "")


def _item_int(item: Any, field: str) -> int | None:
    value = _item_value(item, field)
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _finite_decimal(value: Any) -> Decimal | None:
    if value in {None, ""}:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _fraction(value: Any) -> Decimal | None:
    parsed = _finite_decimal(value)
    return parsed if parsed is not None and Decimal("0") <= parsed <= Decimal("1") else None


def _positive_decimal(value: Any) -> Decimal | None:
    parsed = _finite_decimal(value)
    return parsed if parsed is not None and parsed > 0 else None


def _iter_iso_dates(date_from: str, date_to: str) -> Iterable[str]:
    current = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    while current <= end:
        yield current.isoformat()
        current += timedelta(days=1)


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text
