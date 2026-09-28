"""Read-only Web Vitrina catalog for authenticated WB Buyer observations."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Iterable, Mapping

from packages.contracts.registry_upload_bundle_v1 import MetricV2Item

SOURCE_KEY = "wb_buyer_authenticated"
WALLET_PRICE_METRIC_KEY = "buyer_wallet_price_rub"
NONWALLET_PRICE_METRIC_KEY = "buyer_nonwallet_price_rub"
EFFECTIVE_DISCOUNT_METRIC_KEY = "effective_nonwallet_discount"
AUTHENTICATED_SPP_METRIC_KEY = "authenticated_spp"
METRIC_KEYS = (
    WALLET_PRICE_METRIC_KEY,
    NONWALLET_PRICE_METRIC_KEY,
    EFFECTIVE_DISCOUNT_METRIC_KEY,
    AUTHENTICATED_SPP_METRIC_KEY,
)


def extend_metrics_with_authenticated_buyer(metrics: Iterable[MetricV2Item]) -> list[MetricV2Item]:
    existing = list(metrics)
    keys = {item.metric_key for item in existing}
    additions = (
        (WALLET_PRICE_METRIC_KEY, "Цена покупателя с кошельком, ₽", "rub", 516),
        (NONWALLET_PRICE_METRIC_KEY, "Цена покупателя без кошелька, ₽", "rub", 517),
        (EFFECTIVE_DISCOUNT_METRIC_KEY, "Расчётное снижение цены без кошелька, %", "percent", 518),
        (AUTHENTICATED_SPP_METRIC_KEY, "СПП покупателя, %", "percent", 519),
    )
    return [
        *existing,
        *(
            MetricV2Item(
                metric_key=key,
                enabled=True,
                scope="SKU",
                label_ru=label,
                calc_type="metric",
                calc_ref=key,
                show_in_data=True,
                format=metric_format,
                display_order=order,
                section="Цены",
            )
            for key, label, metric_format, order in additions
            if key not in keys
        ),
    ]


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
