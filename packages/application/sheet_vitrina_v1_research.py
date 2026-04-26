"""Read-only research surfaces for sheet_vitrina_v1."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable, Iterable, Mapping

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_web_vitrina import SheetVitrinaV1WebVitrinaBlock
from packages.contracts.registry_upload_bundle_v1 import ConfigV2Item, MetricV2Item
from packages.contracts.web_vitrina_contract import WebVitrinaContractV1

RESEARCH_OPTIONS_CONTRACT_NAME = "sheet_vitrina_v1_research_sku_group_comparison_options"
RESEARCH_CALCULATION_CONTRACT_NAME = "sheet_vitrina_v1_research_sku_group_comparison_result"
RESEARCH_CONTRACT_VERSION = "v1"
RESEARCH_SOURCE_TRUTH = "server_side_accepted_truth_ready_snapshots"

_FINANCIAL_SECTIONS = ("финанс", "эконом")
_FINANCIAL_TOKENS = (
    "profit",
    "margin",
    "buyout",
    "revenue",
    "выкуп",
    "выруч",
    "прибыл",
    "маржин",
    "себесто",
    "финанс",
    "drr",
    "дрр",
    "расход",
    "expense",
    "spend",
    "ads_sum",
    "cost_price",
    "cogs",
    "commission",
    "delivery_rub",
    "storage_fee",
    "acquiring",
    "penalty",
    "payout",
    "order_sum",
    "ordersum",
    "сумма заказ",
)
_OPERATIONAL_RUB_TOKENS = (
    "price",
    "цена",
    "bid",
    "ставк",
    "cpc",
    "promo",
    "акци",
)
_MEAN_TOKENS = (
    "avg",
    "average",
    "rate",
    "ratio",
    "percent",
    "pct",
    "conversion",
    "ctr",
    "cr",
    "position",
    "localization",
    "price",
    "spp",
    "bid",
    "cpc",
    "средн",
    "конверс",
    "процент",
    "позици",
    "цена",
    "ставк",
)


class SheetVitrinaV1ResearchBlock:
    """Build a bounded read-only retrospective SKU group comparison."""

    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        web_vitrina_block: SheetVitrinaV1WebVitrinaBlock,
        now_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.runtime = runtime
        self.web_vitrina_block = web_vitrina_block
        self.now_factory = now_factory

    def build_sku_group_comparison_options(
        self,
        *,
        page_route: str,
        read_route: str,
    ) -> dict[str, Any]:
        current_state = self.runtime.load_current_state()
        sku_options = _active_sku_options(current_state.config_v2)
        readable_dates = self.web_vitrina_block.list_readable_dates(descending=False)
        sku_metric_keys = self._current_sku_metric_keys(page_route=page_route, read_route=read_route)
        metric_options = _selectable_metric_options(
            current_state.metrics_v2,
            sku_metric_keys=sku_metric_keys,
        )
        default_metric_keys = [item["metric_key"] for item in metric_options[:5]]
        return {
            "contract_name": RESEARCH_OPTIONS_CONTRACT_NAME,
            "contract_version": RESEARCH_CONTRACT_VERSION,
            "source_truth": RESEARCH_SOURCE_TRUTH,
            "read_only": True,
            "causal_claim": False,
            "sku_options": sku_options,
            "metric_options": metric_options,
            "default_metric_keys": default_metric_keys,
            "date_capabilities": _date_capabilities(readable_dates),
            "notes": [
                "Расчёт читает только persisted ready snapshots / accepted truth.",
                "Финансовый блок метрик исключён из MVP.",
                "Результат является ретроспективным сравнением динамики, не causal proof.",
            ],
        }

    def calculate_sku_group_comparison(
        self,
        payload: Mapping[str, Any],
        *,
        page_route: str,
        read_route: str,
    ) -> dict[str, Any]:
        options = self.build_sku_group_comparison_options(page_route=page_route, read_route=read_route)
        context = _ValidatedResearchPayload.from_payload(payload, options=options)
        metrics_by_key = {item["metric_key"]: item for item in options["metric_options"]}
        values_by_date = self._load_values_by_date(
            page_route=page_route,
            read_route=read_route,
            date_from=min(context.baseline_dates[0], context.analysis_dates[0]),
            date_to=max(context.baseline_dates[-1], context.analysis_dates[-1]),
            metric_keys=context.metric_keys,
            sku_ids=context.all_sku_ids,
        )

        rows: list[dict[str, Any]] = []
        for metric_key in context.metric_keys:
            metric = metrics_by_key[metric_key]
            aggregation_method = _aggregation_method(metric)
            research_baseline = _aggregate_group_period(
                values_by_date=values_by_date,
                sku_ids=context.research_sku_ids,
                dates=context.baseline_dates,
                metric_key=metric_key,
                aggregation_method=aggregation_method,
            )
            research_analysis = _aggregate_group_period(
                values_by_date=values_by_date,
                sku_ids=context.research_sku_ids,
                dates=context.analysis_dates,
                metric_key=metric_key,
                aggregation_method=aggregation_method,
            )
            control_baseline = _aggregate_group_period(
                values_by_date=values_by_date,
                sku_ids=context.control_sku_ids,
                dates=context.baseline_dates,
                metric_key=metric_key,
                aggregation_method=aggregation_method,
            )
            control_analysis = _aggregate_group_period(
                values_by_date=values_by_date,
                sku_ids=context.control_sku_ids,
                dates=context.analysis_dates,
                metric_key=metric_key,
                aggregation_method=aggregation_method,
            )
            research_delta_abs = _delta(research_baseline["value"], research_analysis["value"])
            control_delta_abs = _delta(control_baseline["value"], control_analysis["value"])
            research_delta_pct = _delta_pct(research_baseline["value"], research_delta_abs)
            control_delta_pct = _delta_pct(control_baseline["value"], control_delta_abs)
            rows.append(
                {
                    "metric_key": metric_key,
                    "metric_label": metric["metric_label"],
                    "metric_unit": metric["metric_unit"],
                    "metric_format": metric["metric_format"],
                    "aggregation_method": aggregation_method,
                    "research_baseline_value": research_baseline["value"],
                    "research_analysis_value": research_analysis["value"],
                    "research_delta_abs": research_delta_abs,
                    "research_delta_pct": research_delta_pct,
                    "control_baseline_value": control_baseline["value"],
                    "control_analysis_value": control_analysis["value"],
                    "control_delta_abs": control_delta_abs,
                    "control_delta_pct": control_delta_pct,
                    "diff_in_diff_abs": _delta(control_delta_abs, research_delta_abs),
                    "diff_in_diff_pct_points": _delta(control_delta_pct, research_delta_pct),
                    "coverage": {
                        "research_baseline": research_baseline["coverage"],
                        "research_analysis": research_analysis["coverage"],
                        "control_baseline": control_baseline["coverage"],
                        "control_analysis": control_analysis["coverage"],
                    },
                    "notes": _row_notes(
                        research_baseline["coverage"],
                        research_analysis["coverage"],
                        control_baseline["coverage"],
                        control_analysis["coverage"],
                    ),
                }
            )

        return {
            "contract_name": RESEARCH_CALCULATION_CONTRACT_NAME,
            "contract_version": RESEARCH_CONTRACT_VERSION,
            "source_truth": RESEARCH_SOURCE_TRUTH,
            "read_only": True,
            "causal_claim": False,
            "wording": "ретроспективное сравнение групп; динамика и отличие изменений без causal effect claims",
            "inputs": {
                "research_sku_ids": context.research_sku_ids,
                "control_sku_ids": context.control_sku_ids,
                "metric_keys": context.metric_keys,
                "baseline_period": {
                    "date_from": context.baseline_dates[0],
                    "date_to": context.baseline_dates[-1],
                },
                "analysis_period": {
                    "date_from": context.analysis_dates[0],
                    "date_to": context.analysis_dates[-1],
                },
            },
            "rows": rows,
            "warnings": _result_warnings(rows),
        }

    def _current_sku_metric_keys(self, *, page_route: str, read_route: str) -> set[str]:
        try:
            contract = self.web_vitrina_block.build(page_route=page_route, read_route=read_route)
        except Exception:
            return set()
        return {
            str(row.metric_key)
            for row in contract.rows
            if str(row.scope_kind) == "SKU" and row.nm_id is not None
        }

    def _load_values_by_date(
        self,
        *,
        page_route: str,
        read_route: str,
        date_from: str,
        date_to: str,
        metric_keys: list[str],
        sku_ids: list[int],
    ) -> dict[str, dict[tuple[int, str], Any]]:
        readable_dates = set(
            self.web_vitrina_block.list_readable_dates(
                date_from=date_from,
                date_to=date_to,
                descending=False,
            )
        )
        requested_dates = _date_range(date_from, date_to)
        values_by_date: dict[str, dict[tuple[int, str], Any]] = {
            snapshot_date: {}
            for snapshot_date in requested_dates
        }
        for snapshot_date in requested_dates:
            if snapshot_date not in readable_dates:
                continue
            try:
                contract = self.web_vitrina_block.build(
                    page_route=page_route,
                    read_route=read_route,
                    date_from=snapshot_date,
                    date_to=snapshot_date,
                )
            except Exception:
                continue
            values_by_date[snapshot_date].update(
                _extract_research_values(
                    contract,
                    metric_keys=set(metric_keys),
                    sku_ids=set(sku_ids),
                )
            )
        return values_by_date


class _ValidatedResearchPayload:
    def __init__(
        self,
        *,
        research_sku_ids: list[int],
        control_sku_ids: list[int],
        metric_keys: list[str],
        baseline_dates: list[str],
        analysis_dates: list[str],
    ) -> None:
        self.research_sku_ids = research_sku_ids
        self.control_sku_ids = control_sku_ids
        self.metric_keys = metric_keys
        self.baseline_dates = baseline_dates
        self.analysis_dates = analysis_dates
        self.all_sku_ids = sorted({*research_sku_ids, *control_sku_ids})

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], *, options: Mapping[str, Any]) -> "_ValidatedResearchPayload":
        known_sku_ids = {int(item["nm_id"]) for item in options["sku_options"]}
        selectable_metric_keys = {str(item["metric_key"]) for item in options["metric_options"]}
        research_sku_ids = _normalize_int_list(payload.get("research_sku_ids"))
        control_sku_ids = _normalize_int_list(payload.get("control_sku_ids"))
        metric_keys = _normalize_str_list(payload.get("metric_keys"))
        if not research_sku_ids:
            raise ValueError("Выберите хотя бы один SKU в исследуемой группе")
        if not control_sku_ids:
            raise ValueError("Выберите хотя бы один SKU в контрольной группе")
        overlap = sorted(set(research_sku_ids).intersection(control_sku_ids))
        if overlap:
            raise ValueError(
                "Один SKU не может быть одновременно в исследуемой и контрольной группе: "
                + ", ".join(str(item) for item in overlap)
            )
        unknown_skus = sorted({*research_sku_ids, *control_sku_ids}.difference(known_sku_ids))
        if unknown_skus:
            raise ValueError("Неизвестные или неактивные SKU: " + ", ".join(str(item) for item in unknown_skus))
        if not metric_keys:
            raise ValueError("Выберите хотя бы одну метрику")
        unknown_metrics = sorted(set(metric_keys).difference(selectable_metric_keys))
        if unknown_metrics:
            raise ValueError("Метрики недоступны для MVP Исследования: " + ", ".join(unknown_metrics))
        baseline = _require_period(payload.get("baseline_period"), label="Базовый период")
        analysis = _require_period(payload.get("analysis_period"), label="Период анализа")
        return cls(
            research_sku_ids=research_sku_ids,
            control_sku_ids=control_sku_ids,
            metric_keys=metric_keys,
            baseline_dates=_date_range(baseline["date_from"], baseline["date_to"]),
            analysis_dates=_date_range(analysis["date_from"], analysis["date_to"]),
        )


def _active_sku_options(items: Iterable[ConfigV2Item]) -> list[dict[str, Any]]:
    return [
        {
            "nm_id": int(item.nm_id),
            "label": str(item.display_name or item.nm_id),
            "group": str(item.group or ""),
            "display_order": int(item.display_order),
        }
        for item in sorted(items, key=lambda row: (int(row.display_order), int(row.nm_id)))
        if item.enabled
    ]


def _selectable_metric_options(
    items: Iterable[MetricV2Item],
    *,
    sku_metric_keys: set[str],
) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda row: (int(row.display_order), str(row.metric_key))):
        if not item.enabled or not item.show_in_data:
            continue
        if sku_metric_keys and item.metric_key not in sku_metric_keys:
            continue
        if _is_financial_metric(item):
            continue
        metrics.append(
            {
                "metric_key": str(item.metric_key),
                "metric_label": str(item.label_ru or item.metric_key),
                "metric_unit": _metric_unit(item),
                "metric_format": str(item.format or ""),
                "section": str(item.section or ""),
                "calc_type": str(item.calc_type),
                "display_order": int(item.display_order),
                "aggregation_method": _aggregation_method(
                    {
                        "metric_key": str(item.metric_key),
                        "metric_label": str(item.label_ru or ""),
                        "metric_format": str(item.format or ""),
                        "section": str(item.section or ""),
                    }
                ),
            }
        )
    return metrics


def _is_financial_metric(item: MetricV2Item) -> bool:
    key = str(item.metric_key or "").lower()
    label = str(item.label_ru or "").lower()
    calc_ref = str(item.calc_ref or "").lower()
    section = str(item.section or "").lower()
    if any(token in section for token in _FINANCIAL_SECTIONS):
        return True
    combined = " ".join([key, label, calc_ref, section])
    if any(token in combined for token in _FINANCIAL_TOKENS):
        return True
    if str(item.format or "").lower() == "rub":
        if any(token in combined for token in _OPERATIONAL_RUB_TOKENS):
            return False
        if key.startswith("total_") or "руб" in label or "₽" in label:
            return True
    return False


def _metric_unit(metric: MetricV2Item) -> str:
    metric_format = str(metric.format or "").lower()
    if metric_format == "percent":
        return "%"
    if metric_format == "rub":
        return "rub"
    return metric_format or "number"


def _date_capabilities(readable_dates: list[str]) -> dict[str, Any]:
    default_baseline, default_analysis = _default_periods(readable_dates)
    return {
        "available_date_min": readable_dates[0] if readable_dates else "",
        "available_date_max": readable_dates[-1] if readable_dates else "",
        "readable_dates": readable_dates,
        "default_baseline_period": default_baseline,
        "default_analysis_period": default_analysis,
        "coverage_policy": "missing dates are surfaced as partial/unavailable, not zero-filled",
    }


def _default_periods(readable_dates: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    if not readable_dates:
        return {}, {}
    if len(readable_dates) >= 8:
        return (
            {"date_from": readable_dates[-8], "date_to": readable_dates[-5]},
            {"date_from": readable_dates[-4], "date_to": readable_dates[-1]},
        )
    midpoint = max(1, len(readable_dates) // 2)
    return (
        {"date_from": readable_dates[0], "date_to": readable_dates[midpoint - 1]},
        {"date_from": readable_dates[midpoint], "date_to": readable_dates[-1]}
        if midpoint < len(readable_dates)
        else {"date_from": readable_dates[0], "date_to": readable_dates[-1]},
    )


def _extract_research_values(
    contract: WebVitrinaContractV1,
    *,
    metric_keys: set[str],
    sku_ids: set[int],
) -> dict[tuple[int, str], Any]:
    extracted: dict[tuple[int, str], Any] = {}
    date_columns = list(contract.meta.date_columns)
    if not date_columns:
        return extracted
    column_date = date_columns[0]
    for row in contract.rows:
        if row.scope_kind != "SKU" or row.nm_id is None:
            continue
        if row.nm_id not in sku_ids or row.metric_key not in metric_keys:
            continue
        extracted[(int(row.nm_id), str(row.metric_key))] = row.values_by_date.get(column_date)
    return extracted


def _aggregation_method(metric: Mapping[str, Any]) -> str:
    metric_format = str(metric.get("metric_format") or metric.get("format") or "").lower()
    combined = " ".join(
        [
            str(metric.get("metric_key") or ""),
            str(metric.get("metric_label") or ""),
            str(metric.get("section") or ""),
        ]
    ).lower()
    if metric_format == "percent" or any(token in combined for token in _MEAN_TOKENS):
        return "mean_observed_values"
    return "sum_observed_values"


def _aggregate_group_period(
    *,
    values_by_date: Mapping[str, Mapping[tuple[int, str], Any]],
    sku_ids: list[int],
    dates: list[str],
    metric_key: str,
    aggregation_method: str,
) -> dict[str, Any]:
    expected_points = len(sku_ids) * len(dates)
    observed_values: list[float] = []
    missing_dates: list[str] = []
    for snapshot_date in dates:
        observed_on_date = 0
        values_for_date = values_by_date.get(snapshot_date) or {}
        for sku_id in sku_ids:
            value = _to_number(values_for_date.get((sku_id, metric_key)))
            if value is None:
                continue
            observed_values.append(value)
            observed_on_date += 1
        if observed_on_date < len(sku_ids):
            missing_dates.append(snapshot_date)
    observed_points = len(observed_values)
    missing_points = max(0, expected_points - observed_points)
    value = None
    if observed_values:
        value = (
            sum(observed_values) / len(observed_values)
            if aggregation_method == "mean_observed_values"
            else sum(observed_values)
        )
    coverage = {
        "status": _coverage_status(expected_points=expected_points, observed_points=observed_points),
        "expected_points": expected_points,
        "observed_points": observed_points,
        "missing_points": missing_points,
        "coverage_pct": (observed_points / expected_points if expected_points else None),
        "missing_dates": missing_dates[:31],
    }
    return {"value": value, "coverage": coverage}


def _coverage_status(*, expected_points: int, observed_points: int) -> str:
    if observed_points <= 0:
        return "unavailable"
    if observed_points < expected_points:
        return "partial"
    return "available"


def _row_notes(*coverages: Mapping[str, Any]) -> list[str]:
    statuses = {str(item.get("status") or "") for item in coverages}
    notes: list[str] = []
    if "partial" in statuses:
        notes.append("Есть частичное покрытие: часть SKU/date точек отсутствует.")
    if "unavailable" in statuses:
        notes.append("Есть недоступные периоды: observed_points = 0, значение не заменяется нулём.")
    return notes


def _result_warnings(rows: list[Mapping[str, Any]]) -> list[str]:
    if any("partial" in {coverage["status"] for coverage in row["coverage"].values()} for row in rows):
        return ["Часть строк имеет partial coverage; отсутствующие значения не заменялись нулями."]
    if any("unavailable" in {coverage["status"] for coverage in row["coverage"].values()} for row in rows):
        return ["Часть строк недоступна из-за отсутствия observed values."]
    return []


def _delta(baseline: float | None, analysis: float | None) -> float | None:
    if baseline is None or analysis is None:
        return None
    return analysis - baseline


def _delta_pct(baseline: float | None, delta_abs: float | None) -> float | None:
    if baseline is None or baseline == 0 or delta_abs is None:
        return None
    return delta_abs / abs(baseline)


def _to_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    normalized = str(value).strip().replace(" ", "").replace(",", ".")
    if not normalized or normalized in {"—", "-"}:
        return None
    try:
        return float(normalized)
    except ValueError:
        return None


def _normalize_int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    normalized: list[int] = []
    seen: set[int] = set()
    for item in value:
        try:
            parsed = int(item)
        except (TypeError, ValueError):
            continue
        if parsed not in seen:
            seen.add(parsed)
            normalized.append(parsed)
    return normalized


def _normalize_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        parsed = str(item or "").strip()
        if parsed and parsed not in seen:
            seen.add(parsed)
            normalized.append(parsed)
    return normalized


def _require_period(value: Any, *, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label}: укажите date_from и date_to")
    date_from = str(value.get("date_from") or "").strip()
    date_to = str(value.get("date_to") or "").strip()
    if not date_from or not date_to:
        raise ValueError(f"{label}: укажите date_from и date_to")
    try:
        parsed_from = date.fromisoformat(date_from)
        parsed_to = date.fromisoformat(date_to)
    except ValueError as exc:
        raise ValueError(f"{label}: даты должны быть в формате YYYY-MM-DD") from exc
    if parsed_to < parsed_from:
        raise ValueError(f"{label}: date_from должен быть не позже date_to")
    return {"date_from": date_from, "date_to": date_to}


def _date_range(date_from: str, date_to: str) -> list[str]:
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    return [
        (start + timedelta(days=offset)).isoformat()
        for offset in range((end - start).days + 1)
    ]
