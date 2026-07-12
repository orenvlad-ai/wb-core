"""Read-only plan-execution report for the sheet_vitrina_v1 operator page."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import re
from typing import Any, Callable, Mapping

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.simple_xlsx import build_single_sheet_workbook_bytes, read_first_sheet_rows
from packages.business_time import (
    CANONICAL_BUSINESS_TIMEZONE_NAME,
    current_business_date_iso,
    default_business_as_of_date,
)

TEMPORAL_ROLE_ACCEPTED_CLOSED = "accepted_closed_day_snapshot"
FIN_SOURCE_KEY = "fin_report_daily"
ADS_SOURCE_KEY = "ads_compact"
EPS = 1e-9
MANUAL_MONTHLY_BASELINE_SOURCE_KIND = "manual_monthly_plan_report_baseline"
BASELINE_TEMPLATE_FILENAME = "sheet-vitrina-v1-plan-report-baseline-template.xlsx"
BASELINE_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
BASELINE_TEMPLATE_HEADERS = [
    "Месяц",
    "Выкуп, руб. / fin_buyout_rub",
    "Рекламные расходы, руб. / ads_sum",
]
BASELINE_TEMPLATE_MONTHS = ("2026-01", "2026-02")

PERIOD_LABELS = {
    "yesterday": "За вчера",
    "last_7_days": "За последние 7 дней",
    "last_30_days": "За последние 30 дней",
    "current_month": "За текущий месяц",
    "current_quarter": "За текущий квартал",
    "current_year": "За текущий год",
    "first_quarter": "За первый квартал",
    "second_quarter": "За второй квартал",
    "third_quarter": "За третий квартал",
    "fourth_quarter": "За четвертый квартал",
    "first_half": "За первое полугодие",
    "second_half": "За второе полугодие",
}
FIXED_PERIOD_BOUNDS = {
    "first_quarter": (1, 1, 3, 31),
    "second_quarter": (4, 1, 6, 30),
    "third_quarter": (7, 1, 9, 30),
    "fourth_quarter": (10, 1, 12, 31),
    "first_half": (1, 1, 6, 30),
    "second_half": (7, 1, 12, 31),
}
PERSISTENT_BLOCKS = (
    ("month_to_date", "С начала месяца"),
    ("quarter_to_date", "С начала квартала"),
    ("year_to_date", "С начала года"),
)
REPORT_NOTES = (
    "Отчёт остаётся server-side read-only path и не триггерит refresh/upstream fetch.",
    "Факт читается из persisted accepted closed-day runtime snapshots `fin_report_daily` + `ads_compact` по current active `config_v2`; manual monthly baseline используется только для plan-report aggregate blocks и не подменяет daily snapshots.",
    "План по выкупу считается посуточно: для fixed target periods используется полный целевой период, обрезанный по дате подписания; rolling/to-date blocks используют закрытое фактическое окно.",
    "Факт DRR считается как `ads_sum / fin_buyout_rub * 100`; plan_drr_pct — договорный минимум, поэтому значение выше него означает положительный запас.",
    "План рекламы по WB/VB = `max(плановый выкуп, фактический выкуп) * минимальный договорный DRR / 100`; при перевыполнении оборота рекламный план пересчитывается от факта.",
    "Отдельный прогноз к концу договорного периода использует факт за 2026-02-01..as_of_date и экстраполирует текущий дневной темп до 2026-12-31.",
)
CONTRACT_PERIOD_START = date(2026, 2, 1)
CONTRACT_PERIOD_END = date(2026, 12, 31)
USN_UPPER_LIMIT_RUB = 490_500_000.0


@dataclass(frozen=True)
class PeriodWindow:
    key: str
    label: str
    date_from: str
    date_to: str
    day_count: int
    effective_as_of_date: str
    period_state: str = "closed_day_window"
    requested_date_from: str | None = None
    requested_date_to: str | None = None
    original_date_from: str | None = None
    original_date_to: str | None = None
    contract_start_date: str | None = None
    contract_start_applied: bool = False
    contract_start_note: str | None = None
    target_date_from: str | None = None
    target_date_to: str | None = None
    target_day_count: int | None = None
    fact_date_from: str | None = None
    fact_date_to: str | None = None
    fact_day_count: int | None = None
    is_target_period: bool = False


@dataclass(frozen=True)
class BuyoutPlanCalculation:
    mode: str
    half_year_plans: Mapping[int, float]
    annual_plan_evenly_distributed: bool
    annual_buyout_plan_rub: float
    annual_denominator_date_from: str | None = None
    annual_denominator_date_to: str | None = None
    annual_denominator_day_count: int | None = None


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

    def build_baseline_template(self) -> tuple[bytes, str]:
        rows: list[list[Any]] = [BASELINE_TEMPLATE_HEADERS]
        rows.extend([[month, "", ""] for month in BASELINE_TEMPLATE_MONTHS])
        return build_single_sheet_workbook_bytes("План факт месяцы", rows), BASELINE_TEMPLATE_FILENAME

    def build_baseline_status(self) -> dict[str, Any]:
        return _build_baseline_status_payload(self.runtime.load_plan_report_monthly_baseline())

    def upload_baseline(
        self,
        workbook_bytes: bytes,
        *,
        uploaded_filename: str | None = None,
        uploaded_content_type: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        parsed_rows = _parse_baseline_workbook(workbook_bytes)
        uploaded_at = _timestamp_from_now(self.now_factory())
        normalized_filename = str(uploaded_filename or "").strip() or BASELINE_TEMPLATE_FILENAME
        normalized_content_type = str(uploaded_content_type or "").strip() or BASELINE_CONTENT_TYPE
        workbook_checksum = hashlib.sha256(workbook_bytes).hexdigest()
        self.runtime.save_plan_report_monthly_baseline(
            rows=parsed_rows,
            uploaded_at=uploaded_at,
            source_kind=MANUAL_MONTHLY_BASELINE_SOURCE_KIND,
            uploaded_filename=normalized_filename,
            uploaded_content_type=normalized_content_type,
            workbook_checksum=workbook_checksum,
            note=note,
        )
        return {
            "status": "accepted",
            "message": "Исторические данные для отчёта приняты.",
            "source_kind": MANUAL_MONTHLY_BASELINE_SOURCE_KIND,
            "uploaded_at": uploaded_at,
            "uploaded_filename": normalized_filename,
            "workbook_checksum": workbook_checksum,
            "accepted_months": [row["month"] for row in parsed_rows],
            "row_count": len(parsed_rows),
            "totals": {
                "fin_buyout_rub": sum(float(row["fin_buyout_rub"]) for row in parsed_rows),
                "ads_sum": sum(float(row["ads_sum"]) for row in parsed_rows),
            },
            "warnings": [
                "Данные являются агрегированными manual monthly facts и используются только в отчёте «Выполнение плана»."
            ],
            "baseline": self.build_baseline_status(),
        }

    def build(
        self,
        *,
        period: str,
        plan_drr_pct: float,
        h1_buyout_plan_rub: float | None = None,
        h2_buyout_plan_rub: float | None = None,
        q1_buyout_plan_rub: float | None = None,
        q2_buyout_plan_rub: float | None = None,
        q3_buyout_plan_rub: float | None = None,
        q4_buyout_plan_rub: float | None = None,
        as_of_date: str | None = None,
        use_contract_start_date: bool = False,
        contract_start_date: str | None = None,
        annual_plan_evenly_distributed: bool = False,
    ) -> dict[str, Any]:
        normalized_period = str(period or "").strip()
        if normalized_period not in PERIOD_LABELS:
            raise ValueError(
                "period must be one of: " + ", ".join(PERIOD_LABELS)
            )
        plan_inputs = _resolve_buyout_plan_inputs(
            h1_buyout_plan_rub=h1_buyout_plan_rub,
            h2_buyout_plan_rub=h2_buyout_plan_rub,
            q1_buyout_plan_rub=q1_buyout_plan_rub,
            q2_buyout_plan_rub=q2_buyout_plan_rub,
            q3_buyout_plan_rub=q3_buyout_plan_rub,
            q4_buyout_plan_rub=q4_buyout_plan_rub,
        )
        half_year_plans = plan_inputs["half_year_plans"]
        normalized_plan_drr_pct = _require_non_negative_number(plan_drr_pct, field_name="plan_drr_pct")
        current_business_date = current_business_date_iso(self.now_factory())
        default_closed_business_date = date.fromisoformat(default_business_as_of_date(self.now_factory()))
        reference_date = date.fromisoformat(as_of_date) if as_of_date else default_closed_business_date
        normalized_contract_start_date = _resolve_contract_start_date(
            use_contract_start_date=use_contract_start_date,
            contract_start_date=contract_start_date,
        )
        plan_calculation = _build_buyout_plan_calculation(
            half_year_plans=half_year_plans,
            reference_date=reference_date,
            contract_start_date=normalized_contract_start_date,
            annual_plan_evenly_distributed=annual_plan_evenly_distributed,
        )
        selected_window = _build_selected_window(reference_date=reference_date, period_key=normalized_period)
        fixed_windows = [
            _build_to_date_window(reference_date=reference_date, period_key=key, label=label)
            for key, label in PERSISTENT_BLOCKS
        ]
        if normalized_contract_start_date is not None:
            selected_window = _apply_contract_start_date(
                window=selected_window,
                contract_start_date=normalized_contract_start_date,
            )
            fixed_windows = [
                _apply_contract_start_date(
                    window=window,
                    contract_start_date=normalized_contract_start_date,
                )
                for window in fixed_windows
            ]
        all_windows = [selected_window, *fixed_windows]
        effective_windows = [window for window in all_windows if window.day_count > 0]
        source_windows = effective_windows or all_windows
        source_date_starts = [date.fromisoformat(window.date_from) for window in source_windows]
        if reference_date >= CONTRACT_PERIOD_START:
            source_date_starts.append(CONTRACT_PERIOD_START)
        date_from = min(source_date_starts).isoformat()
        date_to = reference_date.isoformat()
        base_payload = {
            "status": "unavailable",
            "reason": "",
            "business_timezone": CANONICAL_BUSINESS_TIMEZONE_NAME,
            "current_business_date": current_business_date,
            "reference_date": reference_date.isoformat(),
            "effective_as_of_date": reference_date.isoformat(),
            "default_closed_business_date": default_closed_business_date.isoformat(),
            "requested_as_of_date": str(as_of_date or "").strip() or None,
            "selected_period_key": selected_window.key,
            "selected_period_label": selected_window.label,
            "notes": _report_notes_for_plan_mode(plan_calculation.mode),
            "inputs": {
                "plan_model": plan_calculation.mode,
                "input_model": plan_inputs["input_model"],
                "h1_buyout_plan_rub": half_year_plans[1],
                "h2_buyout_plan_rub": half_year_plans[2],
                "legacy_quarter_inputs": plan_inputs.get("legacy_quarter_inputs"),
                "plan_drr_pct": normalized_plan_drr_pct,
                "annual_plan_evenly_distributed": plan_calculation.annual_plan_evenly_distributed,
                "annual_buyout_plan_rub": plan_calculation.annual_buyout_plan_rub,
                "annual_plan_denominator_date_from": plan_calculation.annual_denominator_date_from,
                "annual_plan_denominator_date_to": plan_calculation.annual_denominator_date_to,
                "annual_plan_denominator_day_count": plan_calculation.annual_denominator_day_count,
                "use_contract_start_date": normalized_contract_start_date is not None,
                "contract_start_date": (
                    normalized_contract_start_date.isoformat()
                    if normalized_contract_start_date is not None
                    else None
                ),
            },
            "source_of_truth": {
                "read_model": "persisted_temporal_source_slot_snapshots_plus_plan_report_monthly_baseline",
                "snapshot_role": TEMPORAL_ROLE_ACCEPTED_CLOSED,
                "daily_sources": [FIN_SOURCE_KEY, ADS_SOURCE_KEY],
                "manual_monthly_source": MANUAL_MONTHLY_BASELINE_SOURCE_KIND,
                "sources": [FIN_SOURCE_KEY, ADS_SOURCE_KEY, MANUAL_MONTHLY_BASELINE_SOURCE_KIND],
                "active_sku_source": "current_registry_config_v2",
                "plan_calculation_mode": plan_calculation.mode,
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

        baseline_rows = self.runtime.load_plan_report_monthly_baseline()
        baseline_by_month = {str(row["month"]): row for row in baseline_rows}
        expected_dates = [item.isoformat() for item in _iter_dates(date.fromisoformat(date_from), reference_date)]
        daily_buyout_facts: dict[str, float] = {}
        daily_ads_facts: dict[str, float] = {}
        active_nm_id_set = set(active_nm_ids)
        missing_dates_by_source: dict[str, list[str]] = {}
        invalid_dates_by_source: dict[str, dict[str, str]] = {}
        for snapshot_date in expected_dates:
            fin_snapshot, _fin_captured_at = self.runtime.load_temporal_source_slot_snapshot(
                source_key=FIN_SOURCE_KEY,
                snapshot_date=snapshot_date,
                snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
            )
            ads_snapshot, _ads_captured_at = self.runtime.load_temporal_source_slot_snapshot(
                source_key=ADS_SOURCE_KEY,
                snapshot_date=snapshot_date,
                snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
            )
            if fin_snapshot is None:
                missing_dates_by_source.setdefault(FIN_SOURCE_KEY, []).append(snapshot_date)
            if ads_snapshot is None:
                missing_dates_by_source.setdefault(ADS_SOURCE_KEY, []).append(snapshot_date)
            if fin_snapshot is not None:
                try:
                    daily_buyout_facts[snapshot_date] = _sum_snapshot_metric(
                        payload=fin_snapshot,
                        expected_snapshot_date=snapshot_date,
                        allowed_nm_ids=active_nm_id_set,
                        result_kinds={"success"},
                        field_name="fin_buyout_rub",
                    )
                except ValueError as exc:
                    invalid_dates_by_source.setdefault(FIN_SOURCE_KEY, {})[snapshot_date] = str(exc)
            if ads_snapshot is not None:
                try:
                    daily_ads_facts[snapshot_date] = _sum_snapshot_metric(
                        payload=ads_snapshot,
                        expected_snapshot_date=snapshot_date,
                        allowed_nm_ids=active_nm_id_set,
                        result_kinds={"success", "empty"},
                        field_name="ads_sum",
                    )
                except ValueError as exc:
                    invalid_dates_by_source.setdefault(ADS_SOURCE_KEY, {})[snapshot_date] = str(exc)

        period_blocks = {
            "selected_period": _build_period_block(
                window=selected_window,
                daily_buyout_facts=daily_buyout_facts,
                daily_ads_facts=daily_ads_facts,
                baseline_by_month=baseline_by_month,
                missing_dates_by_source=missing_dates_by_source,
                invalid_dates_by_source=invalid_dates_by_source,
                plan_calculation=plan_calculation,
                plan_drr_pct=normalized_plan_drr_pct,
            ),
        }
        for fixed_window in fixed_windows:
            period_blocks[fixed_window.key] = _build_period_block(
                window=fixed_window,
                daily_buyout_facts=daily_buyout_facts,
                daily_ads_facts=daily_ads_facts,
                baseline_by_month=baseline_by_month,
                missing_dates_by_source=missing_dates_by_source,
                invalid_dates_by_source=invalid_dates_by_source,
                plan_calculation=plan_calculation,
                plan_drr_pct=normalized_plan_drr_pct,
            )
        contract_period_projection = _build_contract_period_projection(
            reference_date=reference_date,
            daily_buyout_facts=daily_buyout_facts,
            daily_ads_facts=daily_ads_facts,
            baseline_by_month=baseline_by_month,
            missing_dates_by_source=missing_dates_by_source,
            invalid_dates_by_source=invalid_dates_by_source,
            plan_calculation=plan_calculation,
            plan_drr_pct=normalized_plan_drr_pct,
        )

        period_statuses = [str(block.get("status") or "unavailable") for block in period_blocks.values()]
        if all(status == "available" for status in period_statuses):
            report_status = "available"
            reason = ""
        elif any(status in {"available", "partial"} for status in period_statuses):
            report_status = "partial"
            reason = (
                "Отчёт выполнения плана рассчитан частично: часть accepted closed-day snapshots "
                f"для диапазона {date_from}..{date_to} отсутствует или невалидна."
            )
        else:
            report_status = "unavailable"
            reason = (
                "Отчёт выполнения плана пока недоступен: нет usable accepted closed-day snapshots "
                f"для диапазона {date_from}..{date_to}."
            )

        global_buyout_available_set = set(daily_buyout_facts)
        global_ads_available_set = set(daily_ads_facts)
        global_daily_fully_available_set = global_buyout_available_set & global_ads_available_set
        global_daily_any_available_set = global_buyout_available_set | global_ads_available_set
        global_baseline_months = _baseline_months_for_window(
            date_from=date.fromisoformat(date_from),
            date_to=reference_date,
            daily_available_dates=global_daily_fully_available_set,
            baseline_by_month=baseline_by_month,
        )
        global_baseline_covered_dates = [
            item.isoformat()
            for month in global_baseline_months
            for item in _iter_dates(_month_start(month), _month_end(month))
        ]
        global_baseline_covered_set = set(global_baseline_covered_dates)
        global_effective_buyout_covered_set = global_buyout_available_set - global_baseline_covered_set
        global_effective_ads_covered_set = global_ads_available_set - global_baseline_covered_set
        global_effective_daily_any_covered_set = global_daily_any_available_set - global_baseline_covered_set
        global_effective_daily_fully_covered_set = (
            global_effective_buyout_covered_set & global_effective_ads_covered_set
        )
        global_covered_dates = sorted(global_effective_daily_any_covered_set | global_baseline_covered_set)
        global_missing_dates = [
            item
            for item in expected_dates
            if item not in global_effective_daily_fully_covered_set and item not in global_baseline_covered_set
        ]

        return {
            **base_payload,
            "status": report_status,
            "reason": reason,
            "active_sku_count": len(active_nm_ids),
            "baseline": _build_baseline_status_payload(baseline_rows),
            "coverage": {
                **base_payload["coverage"],
                "expected_day_count": len(expected_dates),
                "covered_day_count": len(global_covered_dates),
                "covered_calendar_days": len(global_covered_dates),
                "daily_covered_day_count": len(global_effective_daily_fully_covered_set),
                "covered_by_daily_snapshot_days": len(global_effective_daily_fully_covered_set),
                "daily_any_source_covered_day_count": len(global_effective_daily_any_covered_set),
                "buyout_daily_covered_day_count": len(global_effective_buyout_covered_set),
                "ads_daily_covered_day_count": len(global_effective_ads_covered_set),
                "baseline_covered_day_count": len(global_baseline_covered_dates),
                "covered_by_monthly_baseline_days": len(global_baseline_covered_dates),
                "covered_dates": global_covered_dates,
                "daily_covered_dates": sorted(global_effective_daily_fully_covered_set),
                "daily_dates": sorted(global_effective_daily_fully_covered_set),
                "daily_any_source_dates": sorted(global_effective_daily_any_covered_set),
                "buyout_daily_dates": sorted(global_effective_buyout_covered_set),
                "ads_daily_dates": sorted(global_effective_ads_covered_set),
                "baseline_covered_months": global_baseline_months,
                "baseline_months": global_baseline_months,
                "missing_day_count": len(global_missing_dates),
                "missing_dates": global_missing_dates,
                "fact_is_partial": bool(global_missing_dates),
                "missing_dates_by_source": _filter_missing_dates_by_source(
                    missing_dates_by_source=missing_dates_by_source,
                    missing_dates=set(global_missing_dates),
                ),
                "invalid_dates_by_source": _filter_invalid_dates_by_source(
                    invalid_dates_by_source=invalid_dates_by_source,
                    dates=set(expected_dates),
                    covered_by_baseline=global_baseline_covered_set,
                ),
            },
            "source_breakdown": {
                "daily_dates": sorted(global_effective_daily_fully_covered_set),
                "daily_any_source_dates": sorted(global_effective_daily_any_covered_set),
                "buyout_daily_dates": sorted(global_effective_buyout_covered_set),
                "ads_daily_dates": sorted(global_effective_ads_covered_set),
                "daily_available_dates_before_monthly_baseline_precedence": sorted(global_daily_fully_available_set),
                "buyout_available_dates_before_monthly_baseline_precedence": sorted(global_buyout_available_set),
                "ads_available_dates_before_monthly_baseline_precedence": sorted(global_ads_available_set),
                "daily_excluded_by_monthly_baseline_dates": sorted(
                    global_daily_any_available_set & global_baseline_covered_set
                ),
                "baseline_months": global_baseline_months,
                "missing_dates": global_missing_dates,
                "covered_calendar_days": len(global_covered_dates),
                "covered_by_daily_snapshot_days": len(global_effective_daily_fully_covered_set),
                "daily_any_source_covered_day_count": len(global_effective_daily_any_covered_set),
                "buyout_daily_covered_day_count": len(global_effective_buyout_covered_set),
                "ads_daily_covered_day_count": len(global_effective_ads_covered_set),
                "covered_by_monthly_baseline_days": len(global_baseline_covered_dates),
                "fact_is_partial": bool(global_missing_dates),
            },
            "periods": period_blocks,
            "contract_period_projection": contract_period_projection,
        }


def _resolve_buyout_plan_inputs(
    *,
    h1_buyout_plan_rub: float | None,
    h2_buyout_plan_rub: float | None,
    q1_buyout_plan_rub: float | None,
    q2_buyout_plan_rub: float | None,
    q3_buyout_plan_rub: float | None,
    q4_buyout_plan_rub: float | None,
) -> dict[str, Any]:
    h1_provided = h1_buyout_plan_rub is not None
    h2_provided = h2_buyout_plan_rub is not None
    if h1_provided or h2_provided:
        if not h1_provided or not h2_provided:
            raise ValueError("h1_buyout_plan_rub and h2_buyout_plan_rub query parameters must be provided together")
        return {
            "input_model": "half_year",
            "half_year_plans": {
                1: _require_non_negative_number(h1_buyout_plan_rub, field_name="h1_buyout_plan_rub"),
                2: _require_non_negative_number(h2_buyout_plan_rub, field_name="h2_buyout_plan_rub"),
            },
            "legacy_quarter_inputs": None,
        }

    legacy_values = {
        1: q1_buyout_plan_rub,
        2: q2_buyout_plan_rub,
        3: q3_buyout_plan_rub,
        4: q4_buyout_plan_rub,
    }
    if any(value is not None for value in legacy_values.values()):
        missing = [f"q{quarter}_buyout_plan_rub" for quarter, value in legacy_values.items() if value is None]
        if missing:
            raise ValueError(
                "legacy quarterly plan params must be provided as a complete Q1..Q4 set; "
                f"missing: {', '.join(missing)}"
            )
        quarter_plans = {
            quarter: _require_non_negative_number(value, field_name=f"q{quarter}_buyout_plan_rub")
            for quarter, value in legacy_values.items()
        }
        return {
            "input_model": "legacy_quarter_params_summed_to_half_year",
            "half_year_plans": {
                1: quarter_plans[1] + quarter_plans[2],
                2: quarter_plans[3] + quarter_plans[4],
            },
            "legacy_quarter_inputs": {
                "q1_buyout_plan_rub": quarter_plans[1],
                "q2_buyout_plan_rub": quarter_plans[2],
                "q3_buyout_plan_rub": quarter_plans[3],
                "q4_buyout_plan_rub": quarter_plans[4],
            },
        }

    raise ValueError("h1_buyout_plan_rub and h2_buyout_plan_rub query parameters are required")


def _resolve_contract_start_date(*, use_contract_start_date: bool, contract_start_date: str | None) -> date | None:
    if not use_contract_start_date:
        return None
    normalized = str(contract_start_date or "").strip()
    if not normalized:
        raise ValueError("contract_start_date query parameter is required when use_contract_start_date=true")
    try:
        return date.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("contract_start_date query parameter must use YYYY-MM-DD format") from exc


def _build_buyout_plan_calculation(
    *,
    half_year_plans: Mapping[int, float],
    reference_date: date,
    contract_start_date: date | None,
    annual_plan_evenly_distributed: bool,
) -> BuyoutPlanCalculation:
    annual_buyout_plan_rub = float(half_year_plans[1]) + float(half_year_plans[2])
    if not annual_plan_evenly_distributed:
        return BuyoutPlanCalculation(
            mode="half_year",
            half_year_plans=half_year_plans,
            annual_plan_evenly_distributed=False,
            annual_buyout_plan_rub=annual_buyout_plan_rub,
        )

    denominator_start = reference_date.replace(month=1, day=1)
    denominator_end = reference_date.replace(month=12, day=31)
    if contract_start_date is not None:
        denominator_start = max(denominator_start, contract_start_date)
    denominator_day_count = max(0, (denominator_end - denominator_start).days + 1)
    return BuyoutPlanCalculation(
        mode="annual_evenly_distributed",
        half_year_plans=half_year_plans,
        annual_plan_evenly_distributed=True,
        annual_buyout_plan_rub=annual_buyout_plan_rub,
        annual_denominator_date_from=denominator_start.isoformat(),
        annual_denominator_date_to=denominator_end.isoformat(),
        annual_denominator_day_count=denominator_day_count,
    )


def _report_notes_for_plan_mode(plan_mode: str) -> list[str]:
    notes = list(REPORT_NOTES)
    if plan_mode == "annual_evenly_distributed":
        notes[2] = (
            "План по выкупу считается посуточно: общий годовой план H1+H2 распределяется "
            "равномерно по календарным дням года или contract-start расчётного окна; "
            "fixed target periods используют полный целевой период."
        )
    return notes


def _build_selected_window(*, reference_date: date, period_key: str) -> PeriodWindow:
    if period_key in FIXED_PERIOD_BOUNDS:
        return _build_fixed_period_window(reference_date=reference_date, period_key=period_key)
    if period_key == "current_year":
        return _build_current_year_target_window(reference_date=reference_date)
    start_date = _period_start_for_key(reference_date=reference_date, period_key=period_key)
    day_count = (reference_date - start_date).days + 1
    return PeriodWindow(
        key=period_key,
        label=PERIOD_LABELS[period_key],
        date_from=start_date.isoformat(),
        date_to=reference_date.isoformat(),
        day_count=day_count,
        effective_as_of_date=reference_date.isoformat(),
        requested_date_from=start_date.isoformat(),
        requested_date_to=reference_date.isoformat(),
        target_date_from=start_date.isoformat(),
        target_date_to=reference_date.isoformat(),
        target_day_count=day_count,
        fact_date_from=start_date.isoformat(),
        fact_date_to=reference_date.isoformat(),
        fact_day_count=day_count,
    )


def _build_to_date_window(*, reference_date: date, period_key: str, label: str) -> PeriodWindow:
    start_date = _period_start_for_key(reference_date=reference_date, period_key=period_key)
    day_count = (reference_date - start_date).days + 1
    return PeriodWindow(
        key=period_key,
        label=label,
        date_from=start_date.isoformat(),
        date_to=reference_date.isoformat(),
        day_count=day_count,
        effective_as_of_date=reference_date.isoformat(),
        requested_date_from=start_date.isoformat(),
        requested_date_to=reference_date.isoformat(),
        target_date_from=start_date.isoformat(),
        target_date_to=reference_date.isoformat(),
        target_day_count=day_count,
        fact_date_from=start_date.isoformat(),
        fact_date_to=reference_date.isoformat(),
        fact_day_count=day_count,
    )


def _build_fixed_period_window(*, reference_date: date, period_key: str) -> PeriodWindow:
    start_month, start_day, end_month, end_day = FIXED_PERIOD_BOUNDS[period_key]
    requested_start = reference_date.replace(month=start_month, day=start_day)
    requested_end = reference_date.replace(month=end_month, day=end_day)
    target_day_count = (requested_end - requested_start).days + 1
    if reference_date < requested_start:
        return PeriodWindow(
            key=period_key,
            label=PERIOD_LABELS[period_key],
            date_from=requested_start.isoformat(),
            date_to=requested_end.isoformat(),
            day_count=0,
            effective_as_of_date=reference_date.isoformat(),
            period_state="not_started",
            requested_date_from=requested_start.isoformat(),
            requested_date_to=requested_end.isoformat(),
            target_date_from=requested_start.isoformat(),
            target_date_to=requested_end.isoformat(),
            target_day_count=target_day_count,
            fact_date_from=requested_start.isoformat(),
            fact_date_to=requested_start.isoformat(),
            fact_day_count=0,
            is_target_period=True,
        )
    effective_end = min(reference_date, requested_end)
    fact_day_count = (effective_end - requested_start).days + 1
    period_state = "completed" if effective_end == requested_end else "in_progress"
    return PeriodWindow(
        key=period_key,
        label=PERIOD_LABELS[period_key],
        date_from=requested_start.isoformat(),
        date_to=effective_end.isoformat(),
        day_count=fact_day_count,
        effective_as_of_date=reference_date.isoformat(),
        period_state=period_state,
        requested_date_from=requested_start.isoformat(),
        requested_date_to=requested_end.isoformat(),
        target_date_from=requested_start.isoformat(),
        target_date_to=requested_end.isoformat(),
        target_day_count=target_day_count,
        fact_date_from=requested_start.isoformat(),
        fact_date_to=effective_end.isoformat(),
        fact_day_count=fact_day_count,
        is_target_period=True,
    )


def _build_current_year_target_window(*, reference_date: date) -> PeriodWindow:
    requested_start = reference_date.replace(month=1, day=1)
    requested_end = reference_date.replace(month=12, day=31)
    target_day_count = (requested_end - requested_start).days + 1
    fact_day_count = (reference_date - requested_start).days + 1
    return PeriodWindow(
        key="current_year",
        label=PERIOD_LABELS["current_year"],
        date_from=requested_start.isoformat(),
        date_to=reference_date.isoformat(),
        day_count=fact_day_count,
        effective_as_of_date=reference_date.isoformat(),
        period_state="completed" if reference_date == requested_end else "in_progress",
        requested_date_from=requested_start.isoformat(),
        requested_date_to=requested_end.isoformat(),
        target_date_from=requested_start.isoformat(),
        target_date_to=requested_end.isoformat(),
        target_day_count=target_day_count,
        fact_date_from=requested_start.isoformat(),
        fact_date_to=reference_date.isoformat(),
        fact_day_count=fact_day_count,
        is_target_period=True,
    )


def _apply_contract_start_date(*, window: PeriodWindow, contract_start_date: date) -> PeriodWindow:
    original_date_from = window.original_date_from or window.date_from
    original_date_to = window.original_date_to or window.date_to
    fact_start = date.fromisoformat(window.fact_date_from or window.date_from)
    fact_end = date.fromisoformat(window.fact_date_to or window.date_to)
    target_start = date.fromisoformat(window.target_date_from or window.requested_date_from or window.date_from)
    target_end = date.fromisoformat(window.target_date_to or window.requested_date_to or window.date_to)
    effective_fact_start = max(fact_start, contract_start_date)
    effective_target_start = max(target_start, contract_start_date)
    target_day_count = max(0, (target_end - effective_target_start).days + 1)
    contract_note = f"Период обрезан по дате подписания: {contract_start_date.isoformat()}."
    contract_start_applied = contract_start_date > target_start
    if effective_target_start > target_end:
        return replace(
            window,
            date_from=effective_target_start.isoformat(),
            date_to=target_end.isoformat(),
            day_count=0,
            period_state="not_started",
            original_date_from=original_date_from,
            original_date_to=original_date_to,
            contract_start_date=contract_start_date.isoformat(),
            contract_start_applied=True,
            contract_start_note=(
                f"Период {target_start.isoformat()}..{target_end.isoformat()} полностью раньше даты подписания "
                f"{contract_start_date.isoformat()}."
            ),
            target_date_from=effective_target_start.isoformat(),
            target_date_to=target_end.isoformat(),
            target_day_count=0,
            fact_date_from=effective_target_start.isoformat(),
            fact_date_to=target_end.isoformat(),
            fact_day_count=0,
        )
    if window.period_state == "not_started":
        return replace(
            window,
            date_from=effective_fact_start.isoformat(),
            day_count=0,
            original_date_from=original_date_from,
            original_date_to=original_date_to,
            contract_start_date=contract_start_date.isoformat(),
            contract_start_applied=contract_start_applied,
            contract_start_note=contract_note if contract_start_applied else None,
            target_date_from=effective_target_start.isoformat(),
            target_date_to=target_end.isoformat(),
            target_day_count=target_day_count,
            fact_date_from=effective_fact_start.isoformat(),
            fact_date_to=fact_end.isoformat(),
            fact_day_count=0,
        )
    if effective_fact_start > fact_end:
        fact_day_count = 0
        period_state = "not_started"
    else:
        fact_day_count = (fact_end - effective_fact_start).days + 1
        period_state = window.period_state
    return replace(
        window,
        date_from=effective_fact_start.isoformat(),
        day_count=fact_day_count,
        period_state=period_state,
        original_date_from=original_date_from,
        original_date_to=original_date_to,
        contract_start_date=contract_start_date.isoformat(),
        contract_start_applied=contract_start_applied,
        contract_start_note=contract_note if contract_start_applied else None,
        target_date_from=effective_target_start.isoformat(),
        target_date_to=target_end.isoformat(),
        target_day_count=target_day_count,
        fact_date_from=effective_fact_start.isoformat(),
        fact_date_to=fact_end.isoformat(),
        fact_day_count=fact_day_count,
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


def _baseline_months_for_window(
    *,
    date_from: date,
    date_to: date,
    daily_available_dates: set[str],
    baseline_by_month: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    months: list[str] = []
    for month in _full_months_inside_window(date_from=date_from, date_to=date_to):
        month_dates = [item.isoformat() for item in _iter_dates(_month_start(month), _month_end(month))]
        if all(item in daily_available_dates for item in month_dates):
            continue
        if month in baseline_by_month:
            months.append(month)
    return months


def _full_months_inside_window(*, date_from: date, date_to: date) -> list[str]:
    current = date_from.replace(day=1)
    result: list[str] = []
    while current <= date_to:
        month = current.strftime("%Y-%m")
        start = _month_start(month)
        end = _month_end(month)
        if start >= date_from and end <= date_to:
            result.append(month)
        current = _add_month(current)
    return result


def _month_start(month: str) -> date:
    return date.fromisoformat(f"{month}-01")


def _month_end(month: str) -> date:
    start = _month_start(month)
    return _add_month(start) - timedelta(days=1)


def _add_month(value: date) -> date:
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1, day=1)
    return value.replace(month=value.month + 1, day=1)


def _filter_missing_dates_by_source(
    *,
    missing_dates_by_source: Mapping[str, list[str]],
    missing_dates: set[str],
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for source_key, dates in missing_dates_by_source.items():
        filtered = [item for item in dates if item in missing_dates]
        if filtered:
            result[source_key] = filtered
    return result


def _filter_invalid_dates_by_source(
    *,
    invalid_dates_by_source: Mapping[str, Mapping[str, str]],
    dates: set[str],
    covered_by_baseline: set[str],
) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for source_key, errors in invalid_dates_by_source.items():
        filtered = {
            item: reason
            for item, reason in errors.items()
            if item in dates and item not in covered_by_baseline
        }
        if filtered:
            result[source_key] = filtered
    return result


def _build_period_block(
    *,
    window: PeriodWindow,
    daily_buyout_facts: Mapping[str, float],
    daily_ads_facts: Mapping[str, float],
    baseline_by_month: Mapping[str, Mapping[str, Any]],
    missing_dates_by_source: Mapping[str, list[str]],
    invalid_dates_by_source: Mapping[str, Mapping[str, str]],
    plan_calculation: BuyoutPlanCalculation,
    plan_drr_pct: float,
) -> dict[str, Any]:
    if window.period_state == "not_started":
        return _build_not_started_period_block(window=window)
    fact_date_from = window.fact_date_from or window.date_from
    fact_date_to = window.fact_date_to or window.date_to
    target_date_from = window.target_date_from or window.date_from
    target_date_to = window.target_date_to or window.date_to
    dates = [item.isoformat() for item in _iter_dates(date.fromisoformat(fact_date_from), date.fromisoformat(fact_date_to))]
    target_dates = [
        item.isoformat()
        for item in _iter_dates(date.fromisoformat(target_date_from), date.fromisoformat(target_date_to))
    ]
    buyout_available_dates = [item for item in dates if item in daily_buyout_facts]
    ads_available_dates = [item for item in dates if item in daily_ads_facts]
    buyout_available_set = set(buyout_available_dates)
    ads_available_set = set(ads_available_dates)
    daily_fully_available_set = buyout_available_set & ads_available_set
    daily_any_available_set = buyout_available_set | ads_available_set
    baseline_months = _baseline_months_for_window(
        date_from=date.fromisoformat(window.date_from),
        date_to=date.fromisoformat(window.date_to),
        daily_available_dates=daily_fully_available_set,
        baseline_by_month=baseline_by_month,
    )
    baseline_covered_dates = [
        item.isoformat()
        for month in baseline_months
        for item in _iter_dates(_month_start(month), _month_end(month))
    ]
    baseline_covered_set = set(baseline_covered_dates)
    buyout_daily_dates = [item for item in buyout_available_dates if item not in baseline_covered_set]
    ads_daily_dates = [item for item in ads_available_dates if item not in baseline_covered_set]
    buyout_daily_set = set(buyout_daily_dates)
    ads_daily_set = set(ads_daily_dates)
    daily_fully_covered_set = buyout_daily_set & ads_daily_set
    daily_any_covered_set = (daily_any_available_set - baseline_covered_set)
    daily_excluded_by_baseline_dates = [item for item in dates if item in daily_any_available_set and item in baseline_covered_set]
    covered_dates = sorted(daily_any_covered_set | baseline_covered_set)
    daily_fully_covered_dates = sorted(daily_fully_covered_set)
    fully_covered_dates = sorted(daily_fully_covered_set | baseline_covered_set)
    missing_dates = [item for item in dates if item not in daily_fully_covered_set and item not in baseline_covered_set]
    fact_buyout_rub = (
        sum(float(daily_buyout_facts[item]) for item in buyout_daily_dates)
        + sum(float(baseline_by_month[month]["fin_buyout_rub"]) for month in baseline_months)
        if buyout_daily_dates or baseline_months
        else None
    )
    fact_ads_sum_rub = (
        sum(float(daily_ads_facts[item]) for item in ads_daily_dates)
        + sum(float(baseline_by_month[month]["ads_sum"]) for month in baseline_months)
        if ads_daily_dates or baseline_months
        else None
    )
    fact_drr_pct = _compute_drr_pct(ads_sum_rub=fact_ads_sum_rub, buyout_rub=fact_buyout_rub)
    target_plan_buyout_rub = sum(
        _daily_buyout_plan_for_date(date.fromisoformat(item), plan_calculation)
        for item in target_dates
    )
    to_date_plan_buyout_rub = sum(
        _daily_buyout_plan_for_date(date.fromisoformat(item), plan_calculation)
        for item in dates
    )
    covered_plan_buyout_rub = (
        sum(_daily_buyout_plan_for_date(date.fromisoformat(item), plan_calculation) for item in covered_dates)
        if covered_dates
        else None
    )
    ads_plan_base_rub, ads_plan_base_mode = _resolve_ads_plan_base(
        plan_buyout_rub=target_plan_buyout_rub,
        fact_buyout_rub=fact_buyout_rub,
    )
    plan_ads_sum_rub = ads_plan_base_rub * (plan_drr_pct / 100.0)
    status = "available" if not missing_dates else "partial" if covered_dates else "unavailable"
    block_missing_by_source = _filter_missing_dates_by_source(
        missing_dates_by_source=missing_dates_by_source,
        missing_dates=set(missing_dates),
    )
    block_invalid_by_source = _filter_invalid_dates_by_source(
        invalid_dates_by_source=invalid_dates_by_source,
        dates=set(dates),
        covered_by_baseline=baseline_covered_set,
    )
    reason = ""
    if status == "partial":
        reason = (
            "Часть дат периода не имеет usable accepted closed-day snapshots или full-month baseline; "
            "факт частичный, основной план рассчитан по целевому периоду."
        )
    elif status == "unavailable":
        reason = "Для периода нет usable accepted closed-day snapshots или применимой full-month baseline; факт не рассчитывается."
    elif baseline_months:
        reason = "Факт периода включает manual monthly baseline для полных месяцев без daily coverage."
    if window.contract_start_note:
        reason = f"{window.contract_start_note} {reason}".strip()
    return {
        "label": window.label,
        "date_from": window.date_from,
        "date_to": window.date_to,
        "day_count": window.day_count,
        "fact_date_from": fact_date_from,
        "fact_date_to": fact_date_to,
        "fact_day_count": window.fact_day_count if window.fact_day_count is not None else window.day_count,
        "target_date_from": target_date_from,
        "target_date_to": target_date_to,
        "target_day_count": window.target_day_count if window.target_day_count is not None else len(target_dates),
        "effective_as_of_date": window.effective_as_of_date,
        "period_state": window.period_state,
        "is_target_period": window.is_target_period,
        "requested_date_from": window.requested_date_from or window.date_from,
        "requested_date_to": window.requested_date_to or window.date_to,
        "original_date_from": window.original_date_from or window.date_from,
        "original_date_to": window.original_date_to or window.date_to,
        "contract_start_date": window.contract_start_date,
        "contract_start_applied": window.contract_start_applied,
        "contract_start_note": window.contract_start_note,
        "status": status,
        "reason": reason,
        "source_of_truth": {
            "daily_sources": [FIN_SOURCE_KEY, ADS_SOURCE_KEY],
            "daily_snapshot_role": TEMPORAL_ROLE_ACCEPTED_CLOSED,
            "manual_monthly_source": MANUAL_MONTHLY_BASELINE_SOURCE_KIND,
            "plan_calculation_mode": plan_calculation.mode,
        },
        "source_mix": {
            "daily_accepted_snapshots": {
                "dates": daily_fully_covered_dates,
                "day_count": len(daily_fully_covered_dates),
                "source_keys": [FIN_SOURCE_KEY, ADS_SOURCE_KEY],
                "available_dates_before_monthly_baseline_precedence": sorted(daily_fully_available_set),
                "excluded_by_monthly_baseline_dates": daily_excluded_by_baseline_dates,
                "buyout_dates": buyout_daily_dates,
                "ads_dates": ads_daily_dates,
            },
            "manual_monthly_plan_report_baseline": {
                "months": baseline_months,
                "day_count": len(baseline_covered_dates),
                "source_kind": MANUAL_MONTHLY_BASELINE_SOURCE_KIND,
            },
        },
        "coverage": {
            "expected_day_count": len(dates),
            "covered_day_count": len(covered_dates),
            "covered_calendar_days": len(covered_dates),
            "daily_covered_day_count": len(daily_fully_covered_dates),
            "covered_by_daily_snapshot_days": len(daily_fully_covered_dates),
            "daily_any_source_covered_day_count": len(daily_any_covered_set),
            "buyout_daily_covered_day_count": len(buyout_daily_dates),
            "ads_daily_covered_day_count": len(ads_daily_dates),
            "baseline_covered_day_count": len(baseline_covered_dates),
            "covered_by_monthly_baseline_days": len(baseline_covered_dates),
            "missing_day_count": len(missing_dates),
            "covered_dates": covered_dates,
            "fully_covered_dates": fully_covered_dates,
            "daily_covered_dates": daily_fully_covered_dates,
            "daily_dates": daily_fully_covered_dates,
            "daily_any_source_dates": sorted(daily_any_covered_set),
            "buyout_daily_dates": buyout_daily_dates,
            "ads_daily_dates": ads_daily_dates,
            "baseline_covered_months": baseline_months,
            "baseline_months": baseline_months,
            "missing_dates": missing_dates,
            "missing_dates_by_source": block_missing_by_source,
            "invalid_dates_by_source": block_invalid_by_source,
            "fact_is_partial": bool(missing_dates),
        },
        "source_breakdown": {
            "daily_dates": daily_fully_covered_dates,
            "daily_any_source_dates": sorted(daily_any_covered_set),
            "buyout_daily_dates": buyout_daily_dates,
            "ads_daily_dates": ads_daily_dates,
            "daily_available_dates_before_monthly_baseline_precedence": sorted(daily_fully_available_set),
            "buyout_available_dates_before_monthly_baseline_precedence": buyout_available_dates,
            "ads_available_dates_before_monthly_baseline_precedence": ads_available_dates,
            "daily_excluded_by_monthly_baseline_dates": daily_excluded_by_baseline_dates,
            "baseline_months": baseline_months,
            "missing_dates": missing_dates,
            "covered_calendar_days": len(covered_dates),
            "covered_by_daily_snapshot_days": len(daily_fully_covered_dates),
            "daily_any_source_covered_day_count": len(daily_any_covered_set),
            "buyout_daily_covered_day_count": len(buyout_daily_dates),
            "ads_daily_covered_day_count": len(ads_daily_dates),
            "covered_by_monthly_baseline_days": len(baseline_covered_dates),
            "fact_is_partial": bool(missing_dates),
        },
        "metrics": {
            "buyout_rub": _build_buyout_metric(
                fact=fact_buyout_rub,
                plan=target_plan_buyout_rub,
                full_period_plan=target_plan_buyout_rub,
                target_period_plan=target_plan_buyout_rub,
                to_date_plan=to_date_plan_buyout_rub,
                covered_period_plan=covered_plan_buyout_rub,
            ),
            "drr_pct": _build_drr_metric(fact=fact_drr_pct, plan=plan_drr_pct),
            "ads_sum_rub": _build_ads_metric(
                fact=fact_ads_sum_rub,
                plan=plan_ads_sum_rub,
                full_period_plan=plan_ads_sum_rub,
                target_period_plan=plan_ads_sum_rub,
                to_date_plan=to_date_plan_buyout_rub * (plan_drr_pct / 100.0),
                covered_period_plan=None if covered_plan_buyout_rub is None else covered_plan_buyout_rub * (plan_drr_pct / 100.0),
                ads_plan_base_rub=ads_plan_base_rub,
                ads_plan_base_mode=ads_plan_base_mode,
            ),
        },
    }


def _build_contract_period_projection(
    *,
    reference_date: date,
    daily_buyout_facts: Mapping[str, float],
    daily_ads_facts: Mapping[str, float],
    baseline_by_month: Mapping[str, Mapping[str, Any]],
    missing_dates_by_source: Mapping[str, list[str]],
    invalid_dates_by_source: Mapping[str, Mapping[str, str]],
    plan_calculation: BuyoutPlanCalculation,
    plan_drr_pct: float,
) -> dict[str, Any]:
    total_day_count = (CONTRACT_PERIOD_END - CONTRACT_PERIOD_START).days + 1
    annual_buyout_plan_rub = float(plan_calculation.annual_buyout_plan_rub)
    annual_ads_plan_rub = annual_buyout_plan_rub * (plan_drr_pct / 100.0)
    base_payload: dict[str, Any] = {
        "label": "Прогноз к концу договорного периода при текущем темпе",
        "status": "unavailable",
        "reason": "",
        "period_date_from": CONTRACT_PERIOD_START.isoformat(),
        "period_date_to": CONTRACT_PERIOD_END.isoformat(),
        "total_contract_day_count": total_day_count,
        "elapsed_date_from": CONTRACT_PERIOD_START.isoformat(),
        "elapsed_date_to": None,
        "elapsed_day_count": 0,
        "effective_as_of_date": reference_date.isoformat(),
        "annual_buyout_plan_rub": annual_buyout_plan_rub,
        "usn_upper_limit_rub": USN_UPPER_LIMIT_RUB,
        "annual_ads_plan_rub": annual_ads_plan_rub,
        "plan_drr_pct": plan_drr_pct,
        "drr_minimum_pct": plan_drr_pct,
        "drr_requirement_type": "minimum",
        "fact_buyout_elapsed_rub": None,
        "fact_ads_elapsed_rub": None,
        "projected_buyout_rub": None,
        "projected_buyout_pct_of_annual_plan": None,
        "projected_buyout_pct_of_usn_upper_limit": None,
        "projected_buyout_remaining_to_usn_upper_limit_rub": None,
        "projected_buyout_exceeds_usn_upper_limit": None,
        "projected_ads_sum_rub": None,
        "projected_ads_pct_of_annual_ads_plan": None,
        "projected_drr_pct": None,
        "projected_drr_margin_to_minimum_pp": None,
        "projected_drr_minimum_met": None,
        "fact_is_partial": False,
        "source_of_truth": {
            "daily_sources": [FIN_SOURCE_KEY, ADS_SOURCE_KEY],
            "daily_snapshot_role": TEMPORAL_ROLE_ACCEPTED_CLOSED,
            "manual_monthly_source": MANUAL_MONTHLY_BASELINE_SOURCE_KIND,
            "formula": "elapsed_fact / elapsed_days * total_contract_days",
        },
        "source_mix": {
            "daily_accepted_snapshots": {
                "dates": [],
                "day_count": 0,
                "source_keys": [FIN_SOURCE_KEY, ADS_SOURCE_KEY],
                "available_dates_before_monthly_baseline_precedence": [],
                "excluded_by_monthly_baseline_dates": [],
                "buyout_dates": [],
                "ads_dates": [],
            },
            "manual_monthly_plan_report_baseline": {
                "months": [],
                "day_count": 0,
                "source_kind": MANUAL_MONTHLY_BASELINE_SOURCE_KIND,
            },
        },
        "coverage": {
            "expected_day_count": 0,
            "covered_day_count": 0,
            "covered_calendar_days": 0,
            "missing_day_count": 0,
            "missing_dates": [],
            "fact_is_partial": False,
        },
    }
    if reference_date < CONTRACT_PERIOD_START:
        return {
            **base_payload,
            "reason": (
                "Прогноз недоступен: последний закрытый день раньше начала договорного периода "
                f"{CONTRACT_PERIOD_START.isoformat()}."
            ),
        }

    elapsed_end = min(reference_date, CONTRACT_PERIOD_END)
    elapsed_day_count = (elapsed_end - CONTRACT_PERIOD_START).days + 1
    if elapsed_day_count <= 0:
        return {
            **base_payload,
            "reason": "Прогноз недоступен: нет закрытых дней договорного периода.",
        }

    elapsed_window = PeriodWindow(
        key="contract_period_projection",
        label="Прогноз к концу договорного периода при текущем темпе",
        date_from=CONTRACT_PERIOD_START.isoformat(),
        date_to=elapsed_end.isoformat(),
        day_count=elapsed_day_count,
        effective_as_of_date=reference_date.isoformat(),
        period_state="completed" if elapsed_end == CONTRACT_PERIOD_END else "in_progress",
        requested_date_from=CONTRACT_PERIOD_START.isoformat(),
        requested_date_to=CONTRACT_PERIOD_END.isoformat(),
        target_date_from=CONTRACT_PERIOD_START.isoformat(),
        target_date_to=CONTRACT_PERIOD_END.isoformat(),
        target_day_count=total_day_count,
        fact_date_from=CONTRACT_PERIOD_START.isoformat(),
        fact_date_to=elapsed_end.isoformat(),
        fact_day_count=elapsed_day_count,
        is_target_period=True,
    )
    fact_block = _build_period_block(
        window=elapsed_window,
        daily_buyout_facts=daily_buyout_facts,
        daily_ads_facts=daily_ads_facts,
        baseline_by_month=baseline_by_month,
        missing_dates_by_source=missing_dates_by_source,
        invalid_dates_by_source=invalid_dates_by_source,
        plan_calculation=plan_calculation,
        plan_drr_pct=plan_drr_pct,
    )
    metrics = fact_block.get("metrics") or {}
    buyout_metric = metrics.get("buyout_rub") or {}
    ads_metric = metrics.get("ads_sum_rub") or {}
    fact_buyout_rub = buyout_metric.get("fact")
    fact_ads_sum_rub = ads_metric.get("fact")
    projected_buyout_rub = (
        float(fact_buyout_rub) / float(elapsed_day_count) * float(total_day_count)
        if fact_buyout_rub is not None
        else None
    )
    projected_ads_sum_rub = (
        float(fact_ads_sum_rub) / float(elapsed_day_count) * float(total_day_count)
        if fact_ads_sum_rub is not None
        else None
    )
    projected_drr_pct = _compute_drr_pct(
        ads_sum_rub=projected_ads_sum_rub,
        buyout_rub=projected_buyout_rub,
    )
    projected_buyout_pct_of_usn_upper_limit = _safe_completion_pct(
        fact=projected_buyout_rub,
        plan=USN_UPPER_LIMIT_RUB,
    )
    projected_buyout_remaining_to_usn_upper_limit_rub = (
        USN_UPPER_LIMIT_RUB - float(projected_buyout_rub)
        if projected_buyout_rub is not None
        else None
    )
    projected_buyout_exceeds_usn_upper_limit = (
        float(projected_buyout_rub) > USN_UPPER_LIMIT_RUB
        if projected_buyout_rub is not None
        else None
    )
    projected_drr_margin_to_minimum_pp = (
        float(projected_drr_pct) - plan_drr_pct
        if projected_drr_pct is not None
        else None
    )
    projected_drr_minimum_met = (
        float(projected_drr_pct) >= plan_drr_pct
        if projected_drr_pct is not None
        else None
    )
    coverage = fact_block.get("coverage") or {}
    source_status = str(fact_block.get("status") or "unavailable")
    has_any_fact = fact_buyout_rub is not None or fact_ads_sum_rub is not None
    if not has_any_fact:
        status = "unavailable"
    elif source_status == "available" and fact_buyout_rub is not None and fact_ads_sum_rub is not None:
        status = "available"
    else:
        status = "partial"

    source_reason = str(fact_block.get("reason") or "").strip()
    if status == "available":
        reason = source_reason
    elif status == "partial":
        reason = (
            "Прогноз рассчитан частично: часть дат elapsed window не имеет usable facts. "
            + source_reason
        ).strip()
    else:
        reason = source_reason or "Прогноз недоступен: за elapsed window нет usable facts."

    return {
        **base_payload,
        "status": status,
        "reason": reason,
        "elapsed_date_to": elapsed_end.isoformat(),
        "elapsed_day_count": elapsed_day_count,
        "fact_buyout_elapsed_rub": fact_buyout_rub,
        "fact_ads_elapsed_rub": fact_ads_sum_rub,
        "projected_buyout_rub": projected_buyout_rub,
        "projected_buyout_pct_of_annual_plan": _safe_completion_pct(
            fact=projected_buyout_rub,
            plan=annual_buyout_plan_rub,
        ),
        "projected_buyout_pct_of_usn_upper_limit": projected_buyout_pct_of_usn_upper_limit,
        "projected_buyout_remaining_to_usn_upper_limit_rub": projected_buyout_remaining_to_usn_upper_limit_rub,
        "projected_buyout_exceeds_usn_upper_limit": projected_buyout_exceeds_usn_upper_limit,
        "projected_ads_sum_rub": projected_ads_sum_rub,
        "projected_ads_pct_of_annual_ads_plan": _safe_completion_pct(
            fact=projected_ads_sum_rub,
            plan=annual_ads_plan_rub,
        ),
        "projected_drr_pct": projected_drr_pct,
        "projected_drr_margin_to_minimum_pp": projected_drr_margin_to_minimum_pp,
        "projected_drr_minimum_met": projected_drr_minimum_met,
        "fact_is_partial": bool(coverage.get("fact_is_partial")) or status == "partial",
        "source_mix": fact_block.get("source_mix") or base_payload["source_mix"],
        "coverage": {
            **coverage,
            "fact_is_partial": bool(coverage.get("fact_is_partial")) or status == "partial",
        },
        "source_breakdown": fact_block.get("source_breakdown") or {},
    }


def _build_not_started_period_block(*, window: PeriodWindow) -> dict[str, Any]:
    reason = window.contract_start_note or (
        "Период ещё не начался относительно последнего закрытого дня "
        f"{window.effective_as_of_date}; факт и план не рассчитываются без закрытых дат периода."
    )
    return {
        "label": window.label,
        "date_from": window.date_from,
        "date_to": window.date_to,
        "day_count": 0,
        "fact_date_from": window.fact_date_from or window.date_from,
        "fact_date_to": window.fact_date_to or window.date_to,
        "fact_day_count": 0,
        "target_date_from": window.target_date_from or window.date_from,
        "target_date_to": window.target_date_to or window.date_to,
        "target_day_count": window.target_day_count or 0,
        "effective_as_of_date": window.effective_as_of_date,
        "period_state": "not_started",
        "is_target_period": window.is_target_period,
        "requested_date_from": window.requested_date_from or window.date_from,
        "requested_date_to": window.requested_date_to or window.date_to,
        "original_date_from": window.original_date_from or window.date_from,
        "original_date_to": window.original_date_to or window.date_to,
        "contract_start_date": window.contract_start_date,
        "contract_start_applied": window.contract_start_applied,
        "contract_start_note": window.contract_start_note,
        "status": "unavailable",
        "reason": reason,
        "source_of_truth": {
            "daily_sources": [FIN_SOURCE_KEY, ADS_SOURCE_KEY],
            "daily_snapshot_role": TEMPORAL_ROLE_ACCEPTED_CLOSED,
            "manual_monthly_source": MANUAL_MONTHLY_BASELINE_SOURCE_KIND,
        },
        "source_mix": {
            "daily_accepted_snapshots": {
                "dates": [],
                "day_count": 0,
                "source_keys": [FIN_SOURCE_KEY, ADS_SOURCE_KEY],
                "available_dates_before_monthly_baseline_precedence": [],
                "excluded_by_monthly_baseline_dates": [],
                "buyout_dates": [],
                "ads_dates": [],
            },
            "manual_monthly_plan_report_baseline": {
                "months": [],
                "day_count": 0,
                "source_kind": MANUAL_MONTHLY_BASELINE_SOURCE_KIND,
            },
        },
        "coverage": {
            "expected_day_count": 0,
            "covered_day_count": 0,
            "covered_calendar_days": 0,
            "daily_covered_day_count": 0,
            "covered_by_daily_snapshot_days": 0,
            "daily_any_source_covered_day_count": 0,
            "buyout_daily_covered_day_count": 0,
            "ads_daily_covered_day_count": 0,
            "baseline_covered_day_count": 0,
            "covered_by_monthly_baseline_days": 0,
            "missing_day_count": 0,
            "covered_dates": [],
            "fully_covered_dates": [],
            "daily_covered_dates": [],
            "daily_dates": [],
            "daily_any_source_dates": [],
            "buyout_daily_dates": [],
            "ads_daily_dates": [],
            "baseline_covered_months": [],
            "baseline_months": [],
            "missing_dates": [],
            "missing_dates_by_source": {},
            "invalid_dates_by_source": {},
            "fact_is_partial": False,
        },
        "source_breakdown": {
            "daily_dates": [],
            "daily_any_source_dates": [],
            "buyout_daily_dates": [],
            "ads_daily_dates": [],
            "daily_available_dates_before_monthly_baseline_precedence": [],
            "buyout_available_dates_before_monthly_baseline_precedence": [],
            "ads_available_dates_before_monthly_baseline_precedence": [],
            "daily_excluded_by_monthly_baseline_dates": [],
            "baseline_months": [],
            "missing_dates": [],
            "covered_calendar_days": 0,
            "covered_by_daily_snapshot_days": 0,
            "daily_any_source_covered_day_count": 0,
            "buyout_daily_covered_day_count": 0,
            "ads_daily_covered_day_count": 0,
            "covered_by_monthly_baseline_days": 0,
            "fact_is_partial": False,
        },
        "metrics": {
            "buyout_rub": _build_buyout_metric(
                fact=None,
                plan=None,
                full_period_plan=None,
                target_period_plan=None,
                to_date_plan=None,
                covered_period_plan=None,
            ),
            "drr_pct": _build_drr_metric(fact=None, plan=None),
            "ads_sum_rub": _build_ads_metric(
                fact=None,
                plan=None,
                full_period_plan=None,
                target_period_plan=None,
                to_date_plan=None,
                covered_period_plan=None,
                ads_plan_base_rub=None,
                ads_plan_base_mode=None,
            ),
        },
    }


def _build_buyout_metric(
    *,
    fact: float | None,
    plan: float | None,
    full_period_plan: float | None,
    target_period_plan: float | None = None,
    to_date_plan: float | None = None,
    covered_period_plan: float | None = None,
) -> dict[str, Any]:
    delta_abs = _safe_delta(fact=fact, plan=plan)
    return {
        "entity_key": "buyout_rub",
        "label": "Выкуп, руб.",
        "fact": fact,
        "plan": plan,
        "full_period_plan": full_period_plan,
        "target_period_plan": target_period_plan,
        "to_date_plan": to_date_plan,
        "covered_period_plan": covered_period_plan,
        "delta_abs": delta_abs,
        "delta_pct": _safe_relative_delta(fact=fact, plan=plan),
        "completion_pct": _safe_completion_pct(fact=fact, plan=plan),
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
        "label": "DRR, % (минимум по договору)",
        "fact": fact,
        "plan": plan,
        "delta_pp": _safe_delta(fact=fact, plan=plan),
        "delta_pct": _safe_relative_delta(fact=fact, plan=plan),
        **_status_payload(
            success=bool(fact is not None and plan is not None and fact >= plan - EPS),
            success_label="минимум выполнен",
            alert_label="ниже минимума",
            unknown=bool(fact is None or plan is None),
        ),
    }


def _build_ads_metric(
    *,
    fact: float | None,
    plan: float | None,
    full_period_plan: float | None,
    target_period_plan: float | None = None,
    to_date_plan: float | None = None,
    covered_period_plan: float | None = None,
    ads_plan_base_rub: float | None = None,
    ads_plan_base_mode: str | None = None,
) -> dict[str, Any]:
    delta_abs = _safe_delta(fact=fact, plan=plan)
    return {
        "entity_key": "ads_sum_rub",
        "label": "Рекламные расходы, руб.",
        "fact": fact,
        "plan": plan,
        "full_period_plan": full_period_plan,
        "target_period_plan": target_period_plan,
        "to_date_plan": to_date_plan,
        "covered_period_plan": covered_period_plan,
        "ads_plan_base_rub": ads_plan_base_rub,
        "ads_plan_base_mode": ads_plan_base_mode,
        "ads_plan_base_label": _ads_plan_base_label(ads_plan_base_mode),
        "delta_abs": delta_abs,
        "delta_pct": _safe_relative_delta(fact=fact, plan=plan),
        "completion_pct": _safe_completion_pct(fact=fact, plan=plan),
        **_status_payload(
            success=bool(fact is not None and plan is not None and fact >= plan - EPS),
            success_label="выполнен",
            alert_label="ниже плана",
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
    result = _get_temporal_snapshot_result(payload)
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


def _get_temporal_snapshot_result(payload: Any) -> Any:
    wrapped_result = _get_attr(payload, "result")
    if wrapped_result is not None:
        return wrapped_result
    return payload


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


def _resolve_ads_plan_base(*, plan_buyout_rub: float, fact_buyout_rub: float | None) -> tuple[float, str]:
    if fact_buyout_rub is not None and fact_buyout_rub > plan_buyout_rub + EPS:
        return float(fact_buyout_rub), "fact_turnover_overperformance_base"
    return float(plan_buyout_rub), "plan_turnover_base"


def _ads_plan_base_label(mode: str | None) -> str | None:
    if mode == "fact_turnover_overperformance_base":
        return "от фактического оборота при перевыполнении"
    if mode == "plan_turnover_base":
        return "от планового оборота"
    return None


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


def _safe_completion_pct(*, fact: float | None, plan: float | None) -> float | None:
    if fact is None or plan is None:
        return None
    if abs(plan) <= EPS:
        return 100.0 if abs(fact) <= EPS else None
    return (float(fact) / float(plan)) * 100.0


def _compute_drr_pct(*, ads_sum_rub: float | None, buyout_rub: float | None) -> float | None:
    if ads_sum_rub is None or buyout_rub is None:
        return None
    if abs(buyout_rub) <= EPS:
        return 0.0 if abs(ads_sum_rub) <= EPS else None
    return (float(ads_sum_rub) / float(buyout_rub)) * 100.0


def _daily_buyout_plan_for_date(value: date, plan_source: BuyoutPlanCalculation | Mapping[int, float]) -> float:
    if isinstance(plan_source, BuyoutPlanCalculation):
        if plan_source.mode == "annual_evenly_distributed":
            denominator_day_count = int(plan_source.annual_denominator_day_count or 0)
            if denominator_day_count <= 0:
                return 0.0
            return float(plan_source.annual_buyout_plan_rub) / float(denominator_day_count)
        plan_source = plan_source.half_year_plans
    half_year = _half_year_of_date(value)
    half_year_plan = float(plan_source[half_year])
    half_year_days = _days_in_half_year(value)
    return half_year_plan / half_year_days


def _half_year_of_date(value: date) -> int:
    return 1 if value.month <= 6 else 2


def _quarter_of_date(value: date) -> int:
    return ((value.month - 1) // 3) + 1


def _days_in_half_year(value: date) -> int:
    if _half_year_of_date(value) == 1:
        start = value.replace(month=1, day=1)
        next_half_year_start = value.replace(month=7, day=1)
    else:
        start = value.replace(month=7, day=1)
        next_half_year_start = value.replace(year=value.year + 1, month=1, day=1)
    return (next_half_year_start - start).days


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


def _parse_baseline_workbook(workbook_bytes: bytes) -> list[dict[str, Any]]:
    workbook_rows = read_first_sheet_rows(workbook_bytes)
    if not workbook_rows:
        raise ValueError("baseline XLSX must contain a header row")
    actual_headers = [_normalize_header(item) for item in workbook_rows[0][:3]]
    expected_headers = [_normalize_header(item) for item in BASELINE_TEMPLATE_HEADERS]
    if actual_headers != expected_headers:
        raise ValueError(
            "Неверные заголовки baseline XLSX. "
            f"Ожидались: {', '.join(BASELINE_TEMPLATE_HEADERS)}."
        )
    parsed_rows: list[dict[str, Any]] = []
    seen_months: set[str] = set()
    for row_index, row in enumerate(workbook_rows[1:], start=2):
        padded = list(row[:3]) + [None] * max(0, 3 - len(row))
        if _row_is_empty(padded):
            continue
        month = _parse_baseline_month(padded[0], row_index=row_index)
        if month in seen_months:
            raise ValueError(f"Строка {row_index}: месяц {month} повторяется")
        seen_months.add(month)
        parsed_rows.append(
            {
                "month": month,
                "fin_buyout_rub": _parse_non_negative_baseline_number(
                    padded[1],
                    row_index=row_index,
                    field_label="Выкуп, руб. / fin_buyout_rub",
                ),
                "ads_sum": _parse_non_negative_baseline_number(
                    padded[2],
                    row_index=row_index,
                    field_label="Рекламные расходы, руб. / ads_sum",
                ),
            }
        )
    if not parsed_rows:
        raise ValueError("baseline XLSX must contain at least one non-empty data row")
    return parsed_rows


def _build_baseline_status_payload(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "status": "missing",
            "source_kind": MANUAL_MONTHLY_BASELINE_SOURCE_KIND,
            "row_count": 0,
            "months": [],
            "totals": {"fin_buyout_rub": 0.0, "ads_sum": 0.0},
            "warning": "Manual monthly baseline не загружен; YTD до daily coverage может быть partial/unavailable.",
        }
    months = [str(row["month"]) for row in rows]
    uploaded_values = [str(row.get("uploaded_at") or "") for row in rows if row.get("uploaded_at")]
    latest_uploaded_at = max(uploaded_values) if uploaded_values else None
    return {
        "status": "uploaded",
        "source_kind": MANUAL_MONTHLY_BASELINE_SOURCE_KIND,
        "row_count": len(rows),
        "months": months,
        "uploaded_at": latest_uploaded_at,
        "uploaded_filename": _latest_metadata_value(rows, "uploaded_filename"),
        "workbook_checksum": _latest_metadata_value(rows, "workbook_checksum"),
        "totals": {
            "fin_buyout_rub": sum(float(row["fin_buyout_rub"]) for row in rows),
            "ads_sum": sum(float(row["ads_sum"]) for row in rows),
        },
        "rows": [
            {
                "month": row["month"],
                "fin_buyout_rub": float(row["fin_buyout_rub"]),
                "ads_sum": float(row["ads_sum"]),
                "uploaded_at": row.get("uploaded_at"),
                "source_kind": row.get("source_kind") or MANUAL_MONTHLY_BASELINE_SOURCE_KIND,
            }
            for row in rows
        ],
        "warning": "Это агрегированные manual monthly facts только для отчёта «Выполнение плана»; daily accepted snapshots не подменяются.",
    }


def _latest_metadata_value(rows: list[Mapping[str, Any]], key: str) -> str | None:
    latest_row = max(rows, key=lambda row: str(row.get("uploaded_at") or ""))
    value = latest_row.get(key)
    return str(value) if value else None


def _normalize_header(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _parse_baseline_month(value: Any, *, row_index: int) -> str:
    raw = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}", raw):
        try:
            date.fromisoformat(f"{raw}-01")
        except ValueError as exc:
            raise ValueError(f"Строка {row_index}: месяц должен быть в формате YYYY-MM") from exc
        return raw
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            return date.fromisoformat(raw).strftime("%Y-%m")
        except ValueError as exc:
            raise ValueError(f"Строка {row_index}: месяц должен быть в формате YYYY-MM") from exc
    raise ValueError(f"Строка {row_index}: месяц должен быть в формате YYYY-MM")


def _parse_non_negative_baseline_number(value: Any, *, row_index: int, field_label: str) -> float:
    if value in ("", None):
        raise ValueError(f"Строка {row_index}: заполните поле {field_label}")
    try:
        numeric = float(str(value).replace(",", "."))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Строка {row_index}: поле {field_label} должно быть числом") from exc
    if numeric < 0:
        raise ValueError(f"Строка {row_index}: поле {field_label} должно быть не меньше 0")
    return numeric


def _row_is_empty(row: list[Any]) -> bool:
    return all(str(item or "").strip() == "" for item in row)


def _timestamp_from_now(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
