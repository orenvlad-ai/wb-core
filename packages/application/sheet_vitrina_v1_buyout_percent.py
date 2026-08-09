"""Read-side buyout-percent projection over accepted sales-funnel snapshots."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Mapping, Sequence

from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
)
from packages.application.sales_funnel_history_block import SalesFunnelHistoryBlock
from packages.business_time import (
    CANONICAL_BUSINESS_TIMEZONE_NAME,
    business_date_from_timestamp,
    current_business_date_iso,
)
from packages.contracts.registry_upload_bundle_v1 import MetricV2Item
from packages.contracts.sales_funnel_history_block import (
    SalesFunnelHistoryItem,
    SalesFunnelHistoryRequest,
    SalesFunnelHistorySuccess,
)


SALES_FUNNEL_HISTORY_SOURCE_KEY = "sales_funnel_history"
BUYOUT_PERCENT_METRIC_KEY = "buyoutPercent"
LEGACY_AVG_BUYOUT_PERCENT_METRIC_KEY = "avg_buyoutPercent"
ORDER_COUNT_METRIC_KEY = "orderCount"
BUYOUT_PERCENT_LABEL_RU = "Процент выкупа"
CONFIRMED_BUYOUT_LABEL_RU = "Расчётный выкуп (подтверждённый)"
BUYOUT_PERCENT_AGGREGATION_RULE = (
    "SUM(buyoutPercent * orderCount) / SUM(orderCount)"
)
BUYOUT_PERCENT_MATURITY_DAYS = 6
BUYOUT_PERCENT_UPSTREAM_DEPTH_DAYS = 7


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


@dataclass(frozen=True)
class MatureBuyoutCaptureResult:
    status: str
    business_date: str
    trusted_cutoff: str
    inspected_dates: tuple[str, ...]
    requested_dates: tuple[str, ...]
    saved_dates: tuple[str, ...]
    failed_dates: tuple[str, ...]
    proof_dates: tuple[str, ...]
    requested_nm_id_count: int
    detail: str

    def public(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "business_date": self.business_date,
            "trusted_cutoff": self.trusted_cutoff,
            "maturity_days": BUYOUT_PERCENT_MATURITY_DAYS,
            "upstream_depth_days": BUYOUT_PERCENT_UPSTREAM_DEPTH_DAYS,
            "inspected_dates": list(self.inspected_dates),
            "requested_dates": list(self.requested_dates),
            "saved_dates": list(self.saved_dates),
            "failed_dates": list(self.failed_dates),
            "proof_dates": list(self.proof_dates),
            "requested_nm_id_count": self.requested_nm_id_count,
            "detail": self.detail,
        }


def trusted_buyout_cutoff(today: date) -> date:
    """Return the inclusive D-6 business-date maturity boundary."""

    return today - timedelta(days=BUYOUT_PERCENT_MATURITY_DAYS)


def buyout_snapshot_is_mature(*, snapshot_date: str, today: date) -> bool:
    return date.fromisoformat(snapshot_date) <= trusted_buyout_cutoff(today)


def capture_mature_buyout_percent_snapshots(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    sales_funnel_history_block: SalesFunnelHistoryBlock,
    enabled_nm_ids: Iterable[int],
    now: datetime,
    captured_at_factory: Callable[[], str] | None = None,
) -> MatureBuyoutCaptureResult:
    """Capture only the newly mature D-6 boundary plus bounded D-7 catch-up.

    A persisted snapshot proves completion only when its capture business date
    is at least six calendar days after the snapshot date and the exact payload
    covers every enabled SKU. This makes same-day manual refreshes idempotent
    without introducing another scheduler or orchestration state machine.
    """

    business_date = date.fromisoformat(current_business_date_iso(now))
    trusted_cutoff = trusted_buyout_cutoff(business_date)
    requested_nm_ids = sorted({int(nm_id) for nm_id in enabled_nm_ids})
    if not requested_nm_ids:
        return MatureBuyoutCaptureResult(
            status="skipped",
            business_date=business_date.isoformat(),
            trusted_cutoff=trusted_cutoff.isoformat(),
            inspected_dates=(),
            requested_dates=(),
            saved_dates=(),
            failed_dates=(),
            proof_dates=(),
            requested_nm_id_count=0,
            detail="No enabled SKU targets.",
        )

    earliest_fetchable = business_date - timedelta(
        days=BUYOUT_PERCENT_UPSTREAM_DEPTH_DAYS
    )
    inspected_dates = tuple(
        _iter_iso_dates(earliest_fetchable.isoformat(), trusted_cutoff.isoformat())
    )
    proof_dates: list[str] = []
    requested_dates: list[str] = []
    for snapshot_date in inspected_dates:
        payload, captured_at = runtime.load_temporal_source_snapshot(
            source_key=SALES_FUNNEL_HISTORY_SOURCE_KEY,
            snapshot_date=snapshot_date,
        )
        if mature_buyout_capture_proof(
            payload=payload,
            captured_at=captured_at,
            snapshot_date=snapshot_date,
            enabled_nm_ids=requested_nm_ids,
        ):
            proof_dates.append(snapshot_date)
        else:
            requested_dates.append(snapshot_date)

    if not requested_dates:
        return MatureBuyoutCaptureResult(
            status="already_captured",
            business_date=business_date.isoformat(),
            trusted_cutoff=trusted_cutoff.isoformat(),
            inspected_dates=inspected_dates,
            requested_dates=(),
            saved_dates=(),
            failed_dates=(),
            proof_dates=tuple(proof_dates),
            requested_nm_id_count=len(requested_nm_ids),
            detail="Persisted mature capture proof already covers the bounded window.",
        )

    result = sales_funnel_history_block.execute(
        SalesFunnelHistoryRequest(
            snapshot_type=SALES_FUNNEL_HISTORY_SOURCE_KEY,
            date_from=requested_dates[0],
            date_to=requested_dates[-1],
            nm_ids=requested_nm_ids,
        )
    ).result
    exact_payloads = split_sales_funnel_success_payload_by_date(result)
    captured_at = (
        captured_at_factory()
        if captured_at_factory is not None
        else now.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    saved_dates: list[str] = []
    failed_dates: list[str] = []
    for snapshot_date in requested_dates:
        exact_payload = exact_payloads.get(snapshot_date)
        if exact_payload is None or not buyout_snapshot_has_enabled_sku_coverage(
            exact_payload,
            snapshot_date=snapshot_date,
            enabled_nm_ids=set(requested_nm_ids),
        ):
            failed_dates.append(snapshot_date)
            continue
        runtime.save_temporal_source_snapshot(
            source_key=SALES_FUNNEL_HISTORY_SOURCE_KEY,
            snapshot_date=snapshot_date,
            captured_at=captured_at,
            payload=exact_payload,
        )
        saved_dates.append(snapshot_date)

    return MatureBuyoutCaptureResult(
        status="captured" if not failed_dates else "partial",
        business_date=business_date.isoformat(),
        trusted_cutoff=trusted_cutoff.isoformat(),
        inspected_dates=inspected_dates,
        requested_dates=tuple(requested_dates),
        saved_dates=tuple(saved_dates),
        failed_dates=tuple(failed_dates),
        proof_dates=tuple(proof_dates),
        requested_nm_id_count=len(requested_nm_ids),
        detail=(
            "Authoritative mature exact-date payload persisted."
            if not failed_dates
            else "Official payload did not fully cover every enabled SKU for all requested dates."
        ),
    )


def mature_buyout_capture_proof(
    *,
    payload: Any,
    captured_at: str | None,
    snapshot_date: str,
    enabled_nm_ids: Iterable[int],
) -> bool:
    """Prove that an exact snapshot was captured no earlier than its D-6 boundary."""

    if payload is None or not captured_at:
        return False
    try:
        capture_business_date = date.fromisoformat(
            business_date_from_timestamp(str(captured_at))
        )
    except (TypeError, ValueError):
        return False
    if capture_business_date < date.fromisoformat(snapshot_date) + timedelta(
        days=BUYOUT_PERCENT_MATURITY_DAYS
    ):
        return False
    return buyout_snapshot_has_enabled_sku_coverage(
        payload,
        snapshot_date=snapshot_date,
        enabled_nm_ids={int(nm_id) for nm_id in enabled_nm_ids},
    )


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
    require_mature_capture: bool = False,
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
        if require_mature_capture and not mature_buyout_capture_proof(
            payload=payload,
            captured_at=captured_at,
            snapshot_date=snapshot_date,
            enabled_nm_ids=requested_nm_ids or (),
        ):
            continue
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
    """Build fail-closed weekly and combined confirmed-buyout references."""

    week_keys = three_closed_week_keys(today)
    trusted_cutoff = trusted_buyout_cutoff(today)
    try:
        current_state = runtime.load_current_state()
    except ValueError:
        current_state = None
    enabled_nm_ids = {
        int(item.nm_id)
        for item in (current_state.config_v2 if current_state is not None else ())
        if item.enabled
    }
    combined_pairs: list[tuple[Decimal | None, Decimal | None]] = []
    available_snapshot_dates: list[str] = []
    week_results: list[dict[str, Any]] = []
    all_weeks_ready = True
    for week_start, week_end in week_keys:
        week_dates = list(_iter_iso_dates(week_start, week_end))
        week_pairs: list[tuple[Decimal | None, Decimal | None]] = []
        missing_dates: list[str] = []
        invalid_dates: list[str] = []
        immature = date.fromisoformat(week_end) > trusted_cutoff
        if immature:
            all_weeks_ready = False
            week_results.append(
                _buyout_week_result(
                    week_start=week_start,
                    week_end=week_end,
                    status="immature",
                    aggregation=aggregate_buyout_percent(()),
                    covered_day_count=0,
                    missing_dates=[],
                    invalid_dates=[],
                )
            )
            continue

        for snapshot_date in week_dates:
            payload, captured_at = runtime.load_temporal_source_snapshot(
                source_key=SALES_FUNNEL_HISTORY_SOURCE_KEY,
                snapshot_date=snapshot_date,
            )
            if payload is None:
                missing_dates.append(snapshot_date)
                continue
            if not mature_buyout_capture_proof(
                payload=payload,
                captured_at=captured_at,
                snapshot_date=snapshot_date,
                enabled_nm_ids=enabled_nm_ids,
            ):
                invalid_dates.append(snapshot_date)
                continue
            available_snapshot_dates.append(snapshot_date)
            metrics_by_nm_id = _snapshot_metrics_by_nm_id(
                _successful_snapshot_items(payload),
                snapshot_date=snapshot_date,
                requested_nm_ids=enabled_nm_ids,
            )
            week_pairs.extend(
                (
                    metrics.buyout_percent,
                    metrics.order_count,
                )
                for metrics in (
                    BuyoutPercentSnapshotMetrics(
                        buyout_percent=values.get(BUYOUT_PERCENT_METRIC_KEY),
                        order_count=values.get(ORDER_COUNT_METRIC_KEY),
                        captured_at=str(captured_at or ""),
                    )
                    for values in metrics_by_nm_id.values()
                )
            )

        aggregation = aggregate_buyout_percent(week_pairs)
        if missing_dates:
            status = "missing"
        elif invalid_dates or aggregation.value is None:
            status = "partial"
        else:
            status = "ready"
            combined_pairs.extend(week_pairs)
        if status != "ready":
            all_weeks_ready = False
        week_results.append(
            _buyout_week_result(
                week_start=week_start,
                week_end=week_end,
                status=status,
                aggregation=aggregation if status == "ready" else aggregate_buyout_percent(()),
                covered_day_count=(
                    len(week_dates) - len(missing_dates) - len(invalid_dates)
                ),
                missing_dates=missing_dates,
                invalid_dates=invalid_dates,
            )
        )

    aggregation = (
        aggregate_buyout_percent(combined_pairs)
        if all_weeks_ready
        else aggregate_buyout_percent(())
    )
    weighted_average = aggregation.value
    included_sku_day_count = aggregation.included_pair_count
    order_count_sum = aggregation.order_count_weight
    return {
        "key": "buyout_percent_three_closed_weeks",
        "label": CONFIRMED_BUYOUT_LABEL_RU,
        "status": "ready" if all_weeks_ready and weighted_average is not None else "unavailable",
        "weighted_average_pct": (
            _decimal_text(weighted_average * Decimal("100"))
            if weighted_average is not None
            else None
        ),
        "date_from": week_keys[0][0],
        "date_to": week_keys[-1][1],
        "weeks": week_results,
        "business_timezone": CANONICAL_BUSINESS_TIMEZONE_NAME,
        "maturity_days": BUYOUT_PERCENT_MATURITY_DAYS,
        "trusted_cutoff": trusted_cutoff.isoformat(),
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
            "Три последние закрытые недели подтверждены; используются только данные "
            f"возрастом не менее {BUYOUT_PERCENT_MATURITY_DAYS} дней."
            if all_weeks_ready and weighted_average is not None
            else "Итог не опубликован: каждая из трёх последних закрытых недель должна "
            f"быть полностью подтверждена данными возрастом не менее {BUYOUT_PERCENT_MATURITY_DAYS} дней."
        ),
    }


def _buyout_week_result(
    *,
    week_start: str,
    week_end: str,
    status: str,
    aggregation: BuyoutPercentAggregation,
    covered_day_count: int,
    missing_dates: list[str],
    invalid_dates: list[str],
) -> dict[str, Any]:
    return {
        "week_start": week_start,
        "week_end": week_end,
        "status": status,
        "weighted_average_pct": (
            _decimal_text(aggregation.value * Decimal("100"))
            if aggregation.value is not None
            else None
        ),
        "included_sku_day_count": aggregation.included_pair_count,
        "order_count_weight": _decimal_text(aggregation.order_count_weight),
        "covered_day_count": covered_day_count,
        "required_day_count": 7,
        "missing_dates": list(missing_dates),
        "invalid_dates": list(invalid_dates),
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
            else _nonnegative_decimal(_item_value(item, "value"))
        )
        if value is not None:
            by_nm_id.setdefault(nm_id, {})[metric] = value
    return by_nm_id


def buyout_snapshot_has_enabled_sku_coverage(
    payload: Any,
    *,
    snapshot_date: str,
    enabled_nm_ids: set[int],
) -> bool:
    if not enabled_nm_ids:
        return False
    metrics_by_nm_id = _snapshot_metrics_by_nm_id(
        _successful_snapshot_items(payload),
        snapshot_date=snapshot_date,
        requested_nm_ids=enabled_nm_ids,
    )
    if set(metrics_by_nm_id) != enabled_nm_ids:
        return False
    for metrics in metrics_by_nm_id.values():
        order_count = metrics.get(ORDER_COUNT_METRIC_KEY)
        if order_count is None:
            return False
        if order_count > 0 and metrics.get(BUYOUT_PERCENT_METRIC_KEY) is None:
            return False
    return True


def split_sales_funnel_success_payload_by_date(
    payload: Any,
) -> dict[str, SalesFunnelHistorySuccess]:
    if _item_text(payload, "kind") != "success":
        return {}
    items_by_date: dict[str, list[SalesFunnelHistoryItem]] = {}
    for item in _successful_snapshot_items(payload):
        snapshot_date = _item_text(item, "date")
        nm_id = _item_int(item, "nm_id")
        metric = _item_text(item, "metric")
        value = _finite_decimal(_item_value(item, "value"))
        if not snapshot_date or nm_id is None or not metric or value is None:
            continue
        items_by_date.setdefault(snapshot_date, []).append(
            SalesFunnelHistoryItem(
                date=snapshot_date,
                nm_id=nm_id,
                metric=metric,
                value=float(value),
            )
        )
    return {
        snapshot_date: SalesFunnelHistorySuccess(
            kind="success",
            date_from=snapshot_date,
            date_to=snapshot_date,
            count=len(items),
            items=sorted(items, key=lambda item: (item.nm_id, item.metric)),
        )
        for snapshot_date, items in sorted(items_by_date.items())
    }


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


def _nonnegative_decimal(value: Any) -> Decimal | None:
    parsed = _finite_decimal(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _iter_iso_dates(date_from: str, date_to: str) -> Iterable[str]:
    current = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    while current <= end:
        yield current.isoformat()
        current += timedelta(days=1)


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text
