"""Read-only daily-report summary for the sheet_vitrina_v1 operator page."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.business_time import (
    CANONICAL_BUSINESS_TIMEZONE_NAME,
    current_business_date_iso,
    default_business_as_of_date,
)
from packages.contracts.registry_upload_bundle_v1 import ConfigV2Item, MetricV2Item
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope

TEMPORAL_SLOT_YESTERDAY_CLOSED = "yesterday_closed"
EPS = 1e-9
LOW_STOCK_THRESHOLD = 20.0
TOTAL_ORDER_SUM_KEY = "total_orderSum"
SKU_ORDER_SUM_KEY = "orderSum"
TOTAL_METRIC_POOL_KEYS = (
    "total_view_count",
    "total_views_current",
    "total_open_card_count",
    "avg_ctr_current",
    "avg_addToCartConversion",
    "avg_cartToOrderConversion",
    "avg_spp",
    "avg_ads_bid_search",
    "total_ads_views",
    "total_ads_sum",
    "avg_localizationPercent",
)
FACTOR_DIRECTIONAL_METRIC_KEYS = (
    "view_count",
    "views_current",
    "open_card_count",
    "ctr",
    "ctr_current",
    "addToCartConversion",
    "cartToOrderConversion",
)
LOW_STOCK_DISTRICTS = (
    ("stock_ru_central", "Центральный ФО"),
    ("stock_ru_northwest", "Северо-Западный ФО"),
    ("stock_ru_volga", "Приволжский ФО"),
    ("stock_ru_ural", "Уральский ФО"),
    ("stock_ru_south_caucasus", "Юг и СКФО"),
)
REPORT_NOTES = (
    "Сравнение строится только по двум последним closed business day через persisted yesterday_closed ready snapshots.",
    "В ранжировании метрик не участвует CTR открытия карточки: в canonical current truth нет отдельной total-level строки для двух closed days.",
    "В ranked common factors не участвуют SPP, рекламная ставка и локализация: current repo не фиксирует для них однозначный good/bad sign.",
    "В списках SKU показываются display_name и nmId из current registry config; article/vendor code в этом read path не стабилизирован.",
)


@dataclass(frozen=True)
class SnapshotSlotView:
    as_of_date: str
    closed_date: str
    total_values: dict[str, float | None]
    sku_values: dict[int, dict[str, float | None]]


class SheetVitrinaV1DailyReportBlock:
    """Build a compact operator-facing daily report from persisted ready snapshots."""

    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.runtime = runtime
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))

    def build(self) -> dict[str, Any]:
        business_date = date.fromisoformat(current_business_date_iso(self.now_factory()))
        newer_closed_date = (business_date - timedelta(days=1)).isoformat()
        older_closed_date = (business_date - timedelta(days=2)).isoformat()
        current_as_of_date = default_business_as_of_date(self.now_factory())
        previous_as_of_date = (date.fromisoformat(current_as_of_date) - timedelta(days=1)).isoformat()
        base_payload = {
            "status": "unavailable",
            "business_timezone": CANONICAL_BUSINESS_TIMEZONE_NAME,
            "current_business_date": business_date.isoformat(),
            "comparison_basis": "two_latest_closed_business_days",
            "current_as_of_date": current_as_of_date,
            "previous_as_of_date": previous_as_of_date,
            "newer_closed_date": newer_closed_date,
            "older_closed_date": older_closed_date,
            "notes": list(REPORT_NOTES),
        }

        try:
            current_state = self.runtime.load_current_state()
        except ValueError as exc:
            return {
                **base_payload,
                "reason": f"Ежедневный отчёт пока недоступен: {exc}",
            }

        try:
            newer_snapshot = self.runtime.load_sheet_vitrina_ready_snapshot(as_of_date=current_as_of_date)
        except ValueError as exc:
            return {
                **base_payload,
                "reason": f"Ежедневный отчёт пока недоступен: отсутствует ready snapshot для {current_as_of_date} ({exc})",
            }

        try:
            older_snapshot = self.runtime.load_sheet_vitrina_ready_snapshot(as_of_date=previous_as_of_date)
        except ValueError as exc:
            return {
                **base_payload,
                "reason": f"Ежедневный отчёт пока недоступен: отсутствует ready snapshot для {previous_as_of_date} ({exc})",
            }

        try:
            newer_view = _extract_closed_slot_view(newer_snapshot, expected_closed_date=newer_closed_date)
            older_view = _extract_closed_slot_view(older_snapshot, expected_closed_date=older_closed_date)
        except ValueError as exc:
            return {
                **base_payload,
                "reason": f"Ежедневный отчёт пока недоступен: {exc}",
            }

        config_by_nm_id = {
            item.nm_id: item
            for item in current_state.config_v2
            if item.enabled
        }
        metric_labels = {
            item.metric_key: item.label_ru
            for item in current_state.metrics_v2
            if item.enabled
        }

        comparable_metrics = [
            _build_numeric_change_item(
                metric_key=metric_key,
                label=metric_labels.get(metric_key, metric_key),
                newer_value=newer_view.total_values.get(metric_key),
                older_value=older_view.total_values.get(metric_key),
            )
            for metric_key in TOTAL_METRIC_POOL_KEYS
        ]
        comparable_metrics = [item for item in comparable_metrics if item is not None]

        sku_changes = []
        for nm_id, config_item in sorted(config_by_nm_id.items(), key=lambda item: item[1].display_order):
            change = _build_numeric_change_item(
                metric_key=SKU_ORDER_SUM_KEY,
                label=metric_labels.get(SKU_ORDER_SUM_KEY, SKU_ORDER_SUM_KEY),
                newer_value=newer_view.sku_values.get(nm_id, {}).get(SKU_ORDER_SUM_KEY),
                older_value=older_view.sku_values.get(nm_id, {}).get(SKU_ORDER_SUM_KEY),
            )
            if change is None:
                continue
            change["nm_id"] = nm_id
            change["display_name"] = config_item.display_name
            change["identity_label"] = f"{config_item.display_name} · nmId {nm_id}"
            sku_changes.append(change)

        top_decline_skus = _top_negative_items(sku_changes, limit=10)
        top_growth_skus = _top_positive_items(sku_changes, limit=10)

        payload = {
            **base_payload,
            "status": "available",
            "reason": "",
            "total_order_sum": _build_numeric_change_item(
                metric_key=TOTAL_ORDER_SUM_KEY,
                label=metric_labels.get(TOTAL_ORDER_SUM_KEY, TOTAL_ORDER_SUM_KEY),
                newer_value=newer_view.total_values.get(TOTAL_ORDER_SUM_KEY),
                older_value=older_view.total_values.get(TOTAL_ORDER_SUM_KEY),
                allow_zero_baseline=True,
            ),
            "top_metric_declines": _top_negative_items(comparable_metrics, limit=5),
            "top_metric_growth": _top_positive_items(comparable_metrics, limit=5),
            "top_sku_order_sum_declines": top_decline_skus,
            "top_sku_order_sum_growth": top_growth_skus,
            "top_negative_factors": _summarize_factors(
                sku_items=top_decline_skus,
                newer_view=newer_view,
                older_view=older_view,
                metric_labels=metric_labels,
                direction="negative",
            ),
            "top_positive_factors": _summarize_factors(
                sku_items=top_growth_skus,
                newer_view=newer_view,
                older_view=older_view,
                metric_labels=metric_labels,
                direction="positive",
            ),
            "comparable_metric_count": len(comparable_metrics),
            "comparable_sku_count": len(sku_changes),
        }
        return payload


def _extract_closed_slot_view(
    plan: SheetVitrinaV1Envelope,
    *,
    expected_closed_date: str,
) -> SnapshotSlotView:
    slot_index = None
    slot_date = ""
    for index, slot in enumerate(plan.temporal_slots):
        if slot.slot_key == TEMPORAL_SLOT_YESTERDAY_CLOSED:
            slot_index = index
            slot_date = slot.column_date
            break
    if slot_index is None:
        raise ValueError(f"ready snapshot {plan.as_of_date} does not contain yesterday_closed slot")
    if slot_date != expected_closed_date:
        raise ValueError(
            f"ready snapshot {plan.as_of_date} points yesterday_closed to {slot_date}, expected {expected_closed_date}"
        )

    data_sheet = next((item for item in plan.sheets if item.sheet_name == "DATA_VITRINA"), None)
    if data_sheet is None:
        raise ValueError(f"ready snapshot {plan.as_of_date} does not contain DATA_VITRINA")

    value_index = 2 + slot_index
    total_values: dict[str, float | None] = {}
    sku_values: dict[int, dict[str, float | None]] = {}
    for row in data_sheet.rows:
        if len(row) <= value_index:
            continue
        key = str(row[1] or "")
        value = _coerce_numeric(row[value_index])
        if key.startswith("TOTAL|"):
            metric_key = key.split("|", 1)[1]
            total_values[metric_key] = value
            continue
        if not key.startswith("SKU:") or "|" not in key:
            continue
        scope_token, metric_key = key.split("|", 1)
        try:
            nm_id = int(scope_token.split(":", 1)[1])
        except (IndexError, ValueError):
            continue
        sku_values.setdefault(nm_id, {})[metric_key] = value

    return SnapshotSlotView(
        as_of_date=plan.as_of_date,
        closed_date=slot_date,
        total_values=total_values,
        sku_values=sku_values,
    )


def _coerce_numeric(value: Any) -> float | None:
    if value in ("", None):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _build_numeric_change_item(
    *,
    metric_key: str,
    label: str,
    newer_value: float | None,
    older_value: float | None,
    allow_zero_baseline: bool = False,
) -> dict[str, Any] | None:
    if newer_value is None and older_value is None:
        return None
    newer = None if newer_value is None else float(newer_value)
    older = None if older_value is None else float(older_value)
    delta = None if newer is None or older is None else newer - older
    pct_change = _pct_change(newer=newer, older=older, allow_zero_baseline=allow_zero_baseline)
    direction = _direction_from_values(newer, older)
    return {
        "metric_key": metric_key,
        "label": label,
        "newer_value": newer,
        "older_value": older,
        "delta": delta,
        "pct_change": pct_change,
        "direction": direction,
    }


def _pct_change(*, newer: float | None, older: float | None, allow_zero_baseline: bool) -> float | None:
    if newer is None or older is None:
        return None
    if abs(older) <= EPS:
        if allow_zero_baseline and abs(newer) <= EPS:
            return 0.0
        return None
    return ((newer - older) / abs(older)) * 100.0


def _direction_from_values(newer: float | None, older: float | None) -> str:
    if newer is None or older is None:
        return "unknown"
    delta = newer - older
    if delta > EPS:
        return "up"
    if delta < -EPS:
        return "down"
    return "flat"


def _top_negative_items(items: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    comparable = [item for item in items if isinstance(item.get("pct_change"), (int, float)) and item["pct_change"] < -EPS]
    comparable.sort(
        key=lambda item: (
            float(item["pct_change"]),
            -abs(float(item.get("delta") or 0.0)),
            str(item.get("label") or item.get("identity_label") or ""),
        )
    )
    return comparable[:limit]


def _top_positive_items(items: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    comparable = [item for item in items if isinstance(item.get("pct_change"), (int, float)) and item["pct_change"] > EPS]
    comparable.sort(
        key=lambda item: (
            -float(item["pct_change"]),
            -abs(float(item.get("delta") or 0.0)),
            str(item.get("label") or item.get("identity_label") or ""),
        )
    )
    return comparable[:limit]


def _summarize_factors(
    *,
    sku_items: list[dict[str, Any]],
    newer_view: SnapshotSlotView,
    older_view: SnapshotSlotView,
    metric_labels: dict[str, str],
    direction: str,
) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    sample_size = len(sku_items)
    for item in sku_items:
        nm_id = int(item["nm_id"])
        factor_labels = _collect_factor_labels(
            nm_id=nm_id,
            newer_view=newer_view,
            older_view=older_view,
            metric_labels=metric_labels,
            direction=direction,
        )
        for label in factor_labels:
            counts[label] = counts.get(label, 0) + 1
    ranked = sorted(
        counts.items(),
        key=lambda item: (-item[1], _factor_priority(item[0], direction=direction), item[0]),
    )
    return [
        {
            "label": label,
            "count": count,
            "sample_size": sample_size,
        }
        for label, count in ranked[:5]
    ]


def _collect_factor_labels(
    *,
    nm_id: int,
    newer_view: SnapshotSlotView,
    older_view: SnapshotSlotView,
    metric_labels: dict[str, str],
    direction: str,
) -> list[str]:
    labels: list[str] = []
    newer_sku = newer_view.sku_values.get(nm_id, {})
    older_sku = older_view.sku_values.get(nm_id, {})
    for metric_key in FACTOR_DIRECTIONAL_METRIC_KEYS:
        newer_value = newer_sku.get(metric_key)
        older_value = older_sku.get(metric_key)
        if newer_value is None or older_value is None:
            continue
        if direction == "negative" and newer_value < older_value - EPS:
            labels.append(f"{metric_labels.get(metric_key, metric_key)} вниз")
        elif direction == "positive" and newer_value > older_value + EPS:
            labels.append(f"{metric_labels.get(metric_key, metric_key)} вверх")

    newer_price = newer_sku.get("price_seller_discounted")
    older_price = older_sku.get("price_seller_discounted")
    if newer_price is not None and older_price is not None:
        if direction == "negative" and newer_price > older_price + EPS:
            labels.append("Цена вверх")
        elif direction == "positive" and newer_price < older_price - EPS:
            labels.append("Цена вниз")

    if direction == "negative":
        newer_stock_total = newer_sku.get("stock_total")
        if newer_stock_total is not None and newer_stock_total <= EPS:
            labels.append("Нет остатков")
            return labels
        for metric_key, district_label in LOW_STOCK_DISTRICTS:
            district_stock = newer_sku.get(metric_key)
            if district_stock is not None and district_stock < LOW_STOCK_THRESHOLD:
                labels.append(f"Низкий остаток: {district_label} (<20)")
    return labels


def _factor_priority(label: str, *, direction: str) -> int:
    if label == "Нет остатков":
        return 0
    if label.startswith("Низкий остаток: "):
        return 1
    if direction == "negative" and label == "Цена вверх":
        return 2
    if direction == "positive" and label == "Цена вниз":
        return 2
    return 10
