"""Read-only plan-execution report for the sheet_vitrina_v1 operator page."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.business_time import (
    CANONICAL_BUSINESS_TIMEZONE_NAME,
    current_business_date_iso,
    default_business_as_of_date,
)

TEMPORAL_ROLE_ACCEPTED_CLOSED = "accepted_closed_day_snapshot"
FIN_SOURCE_KEY = "fin_report_daily"
ADS_SOURCE_KEY = "ads_compact"
EPS = 1e-9

PERIOD_LABELS = {
    "yesterday": "За вчера",
    "last_7_days": "За последние 7 дней",
    "last_30_days": "За последние 30 дней",
    "current_month": "За текущий месяц",
    "current_quarter": "За текущий квартал",
    "current_year": "За текущий год",
}
PERSISTENT_BLOCKS = (
    ("month_to_date", "С начала месяца"),
    ("quarter_to_date", "С начала квартала"),
    ("year_to_date", "С начала года"),
)
REPORT_NOTES = (
    "Отчёт остаётся server-side read-only path и не триггерит refresh/upstream fetch.",
    "Факт читается только из persisted accepted closed-day runtime snapshots `fin_report_daily` + `ads_compact` по current active `config_v2`.",
    "План по выкупу считается посуточно: квартальный план делится на количество календарных дней квартала, затем дневные доли суммируются по диапазону.",
    "Факт DRR считается как `ads_sum / fin_buyout_rub * 100`; плановый DRR сравнивается именно как процент, без подмены на рекламный бюджет.",
    "План рекламных расходов = `план выкупа за период * плановый DRR / 100`.",
)


@dataclass(frozen=True)
class PeriodWindow:
    key: str
    label: str
    date_from: str
    date_to: str
    day_count: int


class SheetVitrinaV1PlanReportBlock:
    """Build a compact operator-facing plan-execution report from exact-date accepted snapshots."""

    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.runtime = runtime
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))

    def build(
        self,
        *,
        period: str,
        q1_buyout_plan_rub: float,
        q2_buyout_plan_rub: float,
        q3_buyout_plan_rub: float,
        q4_buyout_plan_rub: float,
        plan_drr_pct: float,
        as_of_date: str | None = None,
    ) -> dict[str, Any]:
        normalized_period = str(period or "").strip()
        if normalized_period not in PERIOD_LABELS:
            raise ValueError(
                "period must be one of: yesterday, last_7_days, last_30_days, current_month, current_quarter, current_year"
            )
        quarter_plans = {
            1: _require_non_negative_number(q1_buyout_plan_rub, field_name="q1_buyout_plan_rub"),
            2: _require_non_negative_number(q2_buyout_plan_rub, field_name="q2_buyout_plan_rub"),
            3: _require_non_negative_number(q3_buyout_plan_rub, field_name="q3_buyout_plan_rub"),
            4: _require_non_negative_number(q4_buyout_plan_rub, field_name="q4_buyout_plan_rub"),
        }
        normalized_plan_drr_pct = _require_non_negative_number(plan_drr_pct, field_name="plan_drr_pct")
        current_business_date = current_business_date_iso(self.now_factory())
        reference_date = date.fromisoformat(as_of_date or default_business_as_of_date(self.now_factory()))
        selected_window = _build_selected_window(reference_date=reference_date, period_key=normalized_period)
        fixed_windows = [
            PeriodWindow(
                key=key,
                label=label,
                date_from=_period_start_for_key(reference_date=reference_date, period_key=key).isoformat(),
                date_to=reference_date.isoformat(),
                day_count=(reference_date - _period_start_for_key(reference_date=reference_date, period_key=key)).days + 1,
            )
            for key, label in PERSISTENT_BLOCKS
        ]
        all_windows = [selected_window, *fixed_windows]
        date_from = min(date.fromisoformat(window.date_from) for window in all_windows).isoformat()
        date_to = reference_date.isoformat()
        base_payload = {
            "status": "unavailable",
            "reason": "",
            "business_timezone": CANONICAL_BUSINESS_TIMEZONE_NAME,
            "current_business_date": current_business_date,
            "reference_date": reference_date.isoformat(),
            "selected_period_key": selected_window.key,
            "selected_period_label": selected_window.label,
            "notes": list(REPORT_NOTES),
            "inputs": {
                "q1_buyout_plan_rub": quarter_plans[1],
                "q2_buyout_plan_rub": quarter_plans[2],
                "q3_buyout_plan_rub": quarter_plans[3],
                "q4_buyout_plan_rub": quarter_plans[4],
                "plan_drr_pct": normalized_plan_drr_pct,
            },
            "source_of_truth": {
                "read_model": "persisted_temporal_source_slot_snapshots",
                "snapshot_role": TEMPORAL_ROLE_ACCEPTED_CLOSED,
                "sources": [FIN_SOURCE_KEY, ADS_SOURCE_KEY],
                "active_sku_source": "current_registry_config_v2",
                "date_from": date_from,
                "date_to": date_to,
            },
            "coverage": {
                "date_from": date_from,
                "date_to": date_to,
                "missing_dates_by_source": {},
            },
        }

        try:
            current_state = self.runtime.load_current_state()
        except ValueError as exc:
            return {
                **base_payload,
                "reason": f"Отчёт выполнения плана пока недоступен: {exc}",
            }

        active_nm_ids = sorted({int(item.nm_id) for item in current_state.config_v2 if item.enabled})
        if not active_nm_ids:
            return {
                **base_payload,
                "reason": "Отчёт выполнения плана пока недоступен: current active config_v2 пуст.",
                "active_sku_count": 0,
            }

        fin_snapshots = self.runtime.load_temporal_source_slot_snapshots(
            source_key=FIN_SOURCE_KEY,
            date_from=date_from,
            date_to=date_to,
            snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
        )
        ads_snapshots = self.runtime.load_temporal_source_slot_snapshots(
            source_key=ADS_SOURCE_KEY,
            date_from=date_from,
            date_to=date_to,
            snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
        )

        expected_dates = [item.isoformat() for item in _iter_dates(date.fromisoformat(date_from), reference_date)]
        missing_fin_dates = [item for item in expected_dates if item not in fin_snapshots]
        missing_ads_dates = [item for item in expected_dates if item not in ads_snapshots]
        if missing_fin_dates or missing_ads_dates:
            missing_dates_by_source: dict[str, list[str]] = {}
            if missing_fin_dates:
                missing_dates_by_source[FIN_SOURCE_KEY] = missing_fin_dates
            if missing_ads_dates:
                missing_dates_by_source[ADS_SOURCE_KEY] = missing_ads_dates
            return {
                **base_payload,
                "reason": (
                    "Отчёт выполнения плана пока недоступен: отсутствуют accepted closed-day snapshots "
                    f"для диапазона {date_from}..{date_to}."
                ),
                "active_sku_count": len(active_nm_ids),
                "coverage": {
                    **base_payload["coverage"],
                    "missing_dates_by_source": missing_dates_by_source,
                },
            }

        daily_facts: dict[str, dict[str, float | None]] = {}
        active_nm_id_set = set(active_nm_ids)
        try:
            for snapshot_date in expected_dates:
                fin_snapshot = fin_snapshots[snapshot_date]["payload"]
                ads_snapshot = ads_snapshots[snapshot_date]["payload"]
                buyout_rub = _sum_snapshot_metric(
                    payload=fin_snapshot,
                    expected_snapshot_date=snapshot_date,
                    allowed_nm_ids=active_nm_id_set,
                    result_kinds={"success"},
                    field_name="fin_buyout_rub",
                )
                ads_sum_rub = _sum_snapshot_metric(
                    payload=ads_snapshot,
                    expected_snapshot_date=snapshot_date,
                    allowed_nm_ids=active_nm_id_set,
                    result_kinds={"success", "empty"},
                    field_name="ads_sum",
                )
                daily_facts[snapshot_date] = {
                    "buyout_rub": buyout_rub,
                    "ads_sum_rub": ads_sum_rub,
                }
        except ValueError as exc:
            return {
                **base_payload,
                "reason": f"Отчёт выполнения плана пока недоступен: {exc}",
                "active_sku_count": len(active_nm_ids),
            }

        period_blocks = {
            "selected_period": _build_period_block(
                window=selected_window,
                daily_facts=daily_facts,
                quarter_plans=quarter_plans,
                plan_drr_pct=normalized_plan_drr_pct,
            ),
        }
        for fixed_window in fixed_windows:
            period_blocks[fixed_window.key] = _build_period_block(
                window=fixed_window,
                daily_facts=daily_facts,
                quarter_plans=quarter_plans,
                plan_drr_pct=normalized_plan_drr_pct,
            )

        return {
            **base_payload,
            "status": "available",
            "active_sku_count": len(active_nm_ids),
            "coverage": {
                **base_payload["coverage"],
                "missing_dates_by_source": {},
            },
            "periods": period_blocks,
        }


def _build_selected_window(*, reference_date: date, period_key: str) -> PeriodWindow:
    start_date = _period_start_for_key(reference_date=reference_date, period_key=period_key)
    return PeriodWindow(
        key=period_key,
        label=PERIOD_LABELS[period_key],
        date_from=start_date.isoformat(),
        date_to=reference_date.isoformat(),
        day_count=(reference_date - start_date).days + 1,
    )


def _period_start_for_key(*, reference_date: date, period_key: str) -> date:
    if period_key == "yesterday":
        return reference_date
    if period_key == "last_7_days":
        return reference_date - timedelta(days=6)
    if period_key == "last_30_days":
        return reference_date - timedelta(days=29)
    if period_key in {"current_month", "month_to_date"}:
        return reference_date.replace(day=1)
    if period_key in {"current_quarter", "quarter_to_date"}:
        quarter_start_month = ((_quarter_of_date(reference_date) - 1) * 3) + 1
        return reference_date.replace(month=quarter_start_month, day=1)
    if period_key in {"current_year", "year_to_date"}:
        return reference_date.replace(month=1, day=1)
    raise ValueError(f"unsupported period key: {period_key}")


def _build_period_block(
    *,
    window: PeriodWindow,
    daily_facts: Mapping[str, Mapping[str, float | None]],
    quarter_plans: Mapping[int, float],
    plan_drr_pct: float,
) -> dict[str, Any]:
    dates = [item.isoformat() for item in _iter_dates(date.fromisoformat(window.date_from), date.fromisoformat(window.date_to))]
    fact_buyout_rub = sum(float(daily_facts[item]["buyout_rub"] or 0.0) for item in dates)
    fact_ads_sum_rub = sum(float(daily_facts[item]["ads_sum_rub"] or 0.0) for item in dates)
    fact_drr_pct = _compute_drr_pct(ads_sum_rub=fact_ads_sum_rub, buyout_rub=fact_buyout_rub)
    plan_buyout_rub = sum(_daily_buyout_plan_for_date(date.fromisoformat(item), quarter_plans) for item in dates)
    plan_ads_sum_rub = plan_buyout_rub * (plan_drr_pct / 100.0)
    return {
        "label": window.label,
        "date_from": window.date_from,
        "date_to": window.date_to,
        "day_count": window.day_count,
        "metrics": {
            "buyout_rub": _build_buyout_metric(fact=fact_buyout_rub, plan=plan_buyout_rub),
            "drr_pct": _build_drr_metric(fact=fact_drr_pct, plan=plan_drr_pct),
            "ads_sum_rub": _build_ads_metric(fact=fact_ads_sum_rub, plan=plan_ads_sum_rub),
        },
    }


def _build_buyout_metric(*, fact: float | None, plan: float | None) -> dict[str, Any]:
    delta_abs = _safe_delta(fact=fact, plan=plan)
    return {
        "entity_key": "buyout_rub",
        "label": "Выкуп, руб.",
        "fact": fact,
        "plan": plan,
        "delta_abs": delta_abs,
        "delta_pct": _safe_relative_delta(fact=fact, plan=plan),
        **_status_payload(
            success=bool(fact is not None and plan is not None and fact >= plan - EPS),
            success_label="выполнен",
            alert_label="ниже плана",
            unknown=bool(fact is None or plan is None),
        ),
    }


def _build_drr_metric(*, fact: float | None, plan: float | None) -> dict[str, Any]:
    return {
        "entity_key": "drr_pct",
        "label": "DRR, %",
        "fact": fact,
        "plan": plan,
        "delta_pp": _safe_delta(fact=fact, plan=plan),
        "delta_pct": _safe_relative_delta(fact=fact, plan=plan),
        **_status_payload(
            success=bool(fact is not None and plan is not None and fact <= plan + EPS),
            success_label="в пределах плана",
            alert_label="выше плана",
            unknown=bool(fact is None or plan is None),
        ),
    }


def _build_ads_metric(*, fact: float | None, plan: float | None) -> dict[str, Any]:
    delta_abs = _safe_delta(fact=fact, plan=plan)
    return {
        "entity_key": "ads_sum_rub",
        "label": "Рекламные расходы, руб.",
        "fact": fact,
        "plan": plan,
        "delta_abs": delta_abs,
        "delta_pct": _safe_relative_delta(fact=fact, plan=plan),
        **_status_payload(
            success=bool(fact is not None and plan is not None and fact <= plan + EPS),
            success_label="в пределах плана",
            alert_label="выше плана",
            unknown=bool(fact is None or plan is None),
        ),
    }


def _status_payload(*, success: bool, success_label: str, alert_label: str, unknown: bool) -> dict[str, str]:
    if unknown:
        return {
            "status": "unknown",
            "status_label": "н/д",
        }
    if success:
        return {
            "status": "ok",
            "status_label": success_label,
        }
    return {
        "status": "alert",
        "status_label": alert_label,
    }


def _sum_snapshot_metric(
    *,
    payload: Any,
    expected_snapshot_date: str,
    allowed_nm_ids: set[int],
    result_kinds: set[str],
    field_name: str,
) -> float:
    result = _get_attr(payload, "result")
    kind = str(_get_attr(result, "kind", "") or "").strip()
    snapshot_date = str(_get_attr(result, "snapshot_date", "") or "").strip()
    if kind not in result_kinds:
        raise ValueError(
            f"accepted closed-day snapshot for {field_name} has unexpected result kind {kind!r} at {expected_snapshot_date}"
        )
    if snapshot_date != expected_snapshot_date:
        raise ValueError(
            f"accepted closed-day snapshot for {field_name} points to {snapshot_date}, expected {expected_snapshot_date}"
        )
    items = _get_attr(result, "items", []) or []
    total = 0.0
    for item in items:
        nm_id = _get_int_attr(item, "nm_id")
        if nm_id not in allowed_nm_ids:
            continue
        total += float(_get_numeric_attr(item, field_name, default=0.0))
    return total


def _get_attr(value: Any, field_name: str, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, Mapping):
        return value.get(field_name, default)
    return getattr(value, field_name, default)


def _get_int_attr(value: Any, field_name: str) -> int:
    raw_value = _get_attr(value, field_name)
    try:
        return int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be int-like, got {raw_value!r}") from exc


def _get_numeric_attr(value: Any, field_name: str, *, default: float) -> float:
    raw_value = _get_attr(value, field_name, default)
    try:
        return float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric, got {raw_value!r}") from exc


def _safe_delta(*, fact: float | None, plan: float | None) -> float | None:
    if fact is None or plan is None:
        return None
    return float(fact) - float(plan)


def _safe_relative_delta(*, fact: float | None, plan: float | None) -> float | None:
    if fact is None or plan is None:
        return None
    if abs(plan) <= EPS:
        return 0.0 if abs(fact) <= EPS else None
    return ((float(fact) - float(plan)) / abs(float(plan))) * 100.0


def _compute_drr_pct(*, ads_sum_rub: float | None, buyout_rub: float | None) -> float | None:
    if ads_sum_rub is None or buyout_rub is None:
        return None
    if abs(buyout_rub) <= EPS:
        return 0.0 if abs(ads_sum_rub) <= EPS else None
    return (float(ads_sum_rub) / float(buyout_rub)) * 100.0


def _daily_buyout_plan_for_date(value: date, quarter_plans: Mapping[int, float]) -> float:
    quarter = _quarter_of_date(value)
    quarter_plan = float(quarter_plans[quarter])
    quarter_days = _days_in_quarter(value)
    return quarter_plan / quarter_days


def _quarter_of_date(value: date) -> int:
    return ((value.month - 1) // 3) + 1


def _days_in_quarter(value: date) -> int:
    quarter_start_month = ((_quarter_of_date(value) - 1) * 3) + 1
    quarter_start = value.replace(month=quarter_start_month, day=1)
    if quarter_start_month == 10:
        next_quarter_start = value.replace(year=value.year + 1, month=1, day=1)
    else:
        next_quarter_start = value.replace(month=quarter_start_month + 3, day=1)
    return (next_quarter_start - quarter_start).days


def _iter_dates(start_date: date, end_date: date):
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def _require_non_negative_number(raw_value: float, *, field_name: str) -> float:
    try:
        value = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if value < 0:
        raise ValueError(f"{field_name} must be >= 0")
    return value
