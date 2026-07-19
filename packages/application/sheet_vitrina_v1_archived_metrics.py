"""Central active-vitrina filter for superseded metric definitions."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from packages.application.sheet_vitrina_v1_onec_stocks import (
    ONEC_STOCKS_ARCHIVED_METRIC_KEYS,
    ONEC_STOCKS_SOURCE_KEY,
)
from packages.application.sheet_vitrina_v1_our_wb_costs import OUR_WB_ARCHIVED_METRIC_KEYS
from packages.application.sheet_vitrina_v1_own_product_capital import (
    OWN_PRODUCT_CAPITAL_ARCHIVED_METRIC_KEYS,
)
from packages.contracts.registry_upload_bundle_v1 import MetricV2Item


ARCHIVED_PUBLIC_METRIC_KEYS = frozenset(
    (
        *ONEC_STOCKS_ARCHIVED_METRIC_KEYS,
        *OUR_WB_ARCHIVED_METRIC_KEYS,
        *OWN_PRODUCT_CAPITAL_ARCHIVED_METRIC_KEYS,
    )
)
ARCHIVED_ONLY_SOURCE_KEYS = frozenset({ONEC_STOCKS_SOURCE_KEY})


def filter_archived_public_metrics(metrics: Iterable[MetricV2Item]) -> list[MetricV2Item]:
    """Hide superseded rows without deleting their source/evaluator contracts."""

    return [item for item in metrics if item.metric_key not in ARCHIVED_PUBLIC_METRIC_KEYS]


def active_refresh_summary(refresh_status: Any) -> dict[str, Any]:
    """Reduce public refresh state over sources that still own active metrics."""

    all_outcomes = [
        dict(item)
        for item in (getattr(refresh_status, "source_outcomes", []) or [])
        if isinstance(item, Mapping)
    ]
    if not all_outcomes:
        return {
            "status": str(getattr(refresh_status, "semantic_status", "") or "warning"),
            "label": str(getattr(refresh_status, "semantic_label", "") or "Нужно проверить"),
            "tone": str(getattr(refresh_status, "semantic_tone", "") or "warning"),
            "reason": str(getattr(refresh_status, "semantic_reason", "") or ""),
            "counts": dict(getattr(refresh_status, "source_outcome_counts", {}) or {}),
            "outcomes": [],
        }
    active = [
        item
        for item in all_outcomes
        if str(item.get("source_key") or "") not in ARCHIVED_ONLY_SOURCE_KEYS
    ]
    counts = {"success": 0, "warning": 0, "error": 0}
    for item in active:
        status = str(item.get("status") or "warning")
        counts[status if status in counts else "warning"] += 1
    if counts["error"]:
        status = "error"
        reason = (
            f"Ошибки по {counts['error']} из {len(active)} активных источников; "
            f"ещё {counts['warning']} требуют внимания."
        )
    elif counts["warning"]:
        status = "warning"
        reason = f"Внимания требуют {counts['warning']} из {len(active)} активных источников."
    elif active:
        status = "success"
        reason = f"Все {len(active)} активных источников подтверждены без warning/error."
    else:
        status = "warning"
        reason = "Активные источники в сохранённом статусе не найдены."
        counts["warning"] = 1
    label = "Успешно" if status == "success" else ("Ошибка" if status == "error" else "Внимание")
    return {
        "status": status,
        "label": label,
        "tone": status,
        "reason": reason,
        "counts": counts,
        "outcomes": active,
    }
