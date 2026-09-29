"""Read-only Web Vitrina catalog for authenticated WB Buyer observations."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Iterable, Mapping

from packages.contracts.registry_upload_bundle_v1 import MetricV2Item

SOURCE_KEY = "wb_buyer_authenticated"
WALLET_PRICE_METRIC_KEY = "buyer_wallet_price_rub"
NONWALLET_PRICE_METRIC_KEY = "buyer_nonwallet_price_rub"
EFFECTIVE_DISCOUNT_METRIC_KEY = "effective_nonwallet_discount"
AVG_EFFECTIVE_DISCOUNT_METRIC_KEY = "avg_effective_nonwallet_discount"
AUTHENTICATED_SPP_METRIC_KEY = "authenticated_spp"
PUBLIC_SPP_LABEL = "СПП без авторизации"
AUTHENTICATED_DISCOUNT_LABEL = "СПП с авторизацией"
METRIC_KEYS = (
    WALLET_PRICE_METRIC_KEY,
    NONWALLET_PRICE_METRIC_KEY,
    EFFECTIVE_DISCOUNT_METRIC_KEY,
    AVG_EFFECTIVE_DISCOUNT_METRIC_KEY,
    AUTHENTICATED_SPP_METRIC_KEY,
)


def extend_metrics_with_authenticated_buyer(metrics: Iterable[MetricV2Item]) -> list[MetricV2Item]:
    existing = [
        replace(item, label_ru=PUBLIC_SPP_LABEL)
        if item.metric_key in {"spp_proxy", "avg_spp_proxy"} else item
        for item in metrics
    ]
    keys = {item.metric_key for item in existing}
    additions = (
        (WALLET_PRICE_METRIC_KEY, "Цена покупателя с кошельком, ₽", "rub", 516, "SKU"),
        (NONWALLET_PRICE_METRIC_KEY, "Цена покупателя без кошелька, ₽", "rub", 517, "SKU"),
        (EFFECTIVE_DISCOUNT_METRIC_KEY, AUTHENTICATED_DISCOUNT_LABEL, "percent", 518, "SKU"),
        (AVG_EFFECTIVE_DISCOUNT_METRIC_KEY, AUTHENTICATED_DISCOUNT_LABEL, "percent", 518, "TOTAL"),
        (AUTHENTICATED_SPP_METRIC_KEY, "СПП покупателя, %", "percent", 519, "SKU"),
    )
    return [
        *existing,
        *(
            MetricV2Item(
                metric_key=key,
                enabled=True,
                scope=scope,
                label_ru=label,
                calc_type="metric",
                calc_ref=(EFFECTIVE_DISCOUNT_METRIC_KEY if key == AVG_EFFECTIVE_DISCOUNT_METRIC_KEY else key),
                show_in_data=True,
                format=metric_format,
                display_order=order,
                section="Цены",
            )
            for key, label, metric_format, order, scope in additions
            if key not in keys
        ),
    ]


def visible_authenticated_buyer_metrics(metrics: Iterable[MetricV2Item]) -> list[MetricV2Item]:
    """Keep the unproven pure-SPP column in stored plans, but hide it from UI catalogs."""
    return [item for item in metrics if item.metric_key != AUTHENTICATED_SPP_METRIC_KEY]


def projection_payload(projection: Mapping[str, Any], *, business_date: str) -> SimpleNamespace:
    """Adapt the persisted daily projection to the existing source status reader."""

    diagnostics = dict(projection.get("diagnostics") or {})
    first_run = str(diagnostics.get("first_run_business_date") or "")
    pre_cutover = not first_run or business_date < first_run
    scope_unknown = diagnostics.get("eligible_scope_source") == "unknown_historical_roster"
    kind = "not_available" if pre_cutover or scope_unknown else str(projection.get("kind") or "empty")
    if pre_cutover:
        diagnostics["reason"] = "pre_cutover"
    elif scope_unknown:
        diagnostics["reason"] = "eligible_roster_unknown"
    items = [
        SimpleNamespace(**dict(item))
        for item in ([] if scope_unknown else projection.get("items", []))
        if isinstance(item, Mapping) and type(item.get("nm_id")) is int
    ]
    latest_measured_at = max((str(item.measured_at or "") for item in items), default="")
    detail = "pre_cutover" if pre_cutover else "eligible_roster_unknown" if scope_unknown else "persisted_authenticated_buyer_observations"
    if kind == "empty" and str(diagnostics.get("current_auth_run_reference") or ""):
        detail += "; account_context_reset=true"
    if latest_measured_at:
        detail += f"; latest_measured_at={latest_measured_at}"
    detail += "; expected_account_match=unknown; spp_evidence=components_unknown"
    return SimpleNamespace(
        kind=kind,
        snapshot_date=str(projection.get("snapshot_date") or business_date),
        requested_count=0 if scope_unknown else int(projection.get("requested_count") or 0),
        covered_count=0 if scope_unknown else int(projection.get("covered_count") or 0),
        missing_nm_ids=[] if scope_unknown else list(projection.get("missing_nm_ids") or []),
        items=items,
        diagnostics=diagnostics,
        detail=detail,
    )
