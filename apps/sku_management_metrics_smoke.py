"""Metric registry/evaluator smoke for SKU action deltas and observed buyer price."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.sheet_vitrina_v1_live_plan import SlotLookups, TemporalLiveSources, _MetricEvaluator
from packages.application.sheet_vitrina_v1_sku_actions import (
    ADVERTISING_BID_CHANGE_RUB_METRIC_KEY,
    BUYER_PRICE_RUB_METRIC_KEY,
    SELLER_PRICE_CHANGE_RUB_METRIC_KEY,
    extend_metrics_with_sku_action_metrics,
)
from packages.contracts.registry_upload_bundle_v1 import ConfigV2Item
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1TemporalSlot


def main() -> None:
    metrics = extend_metrics_with_sku_action_metrics([])
    by_key = {item.metric_key: item for item in metrics}
    if set(by_key) != {SELLER_PRICE_CHANGE_RUB_METRIC_KEY, ADVERTISING_BID_CHANGE_RUB_METRIC_KEY, BUYER_PRICE_RUB_METRIC_KEY}:
        raise AssertionError(by_key)
    if any(not item.show_in_data or item.scope != "SKU" for item in metrics):
        raise AssertionError("all new metrics must be selectable SKU rows")

    nm_id = 101
    lookups = SlotLookups(
        seller_funnel_lookup={}, history_lookup={}, web_lookup={}, prices_lookup={}, sf_period_lookup={}, spp_lookup={}, ads_bids_lookup={}, stocks_lookup={}, onec_stocks_lookup={}, ads_compact_lookup={}, fin_lookup={}, fin_storage_fee_total=None, cost_price_lookup={}, promo_lookup={},
        spp_proxy_lookup={nm_id: SimpleNamespace(public_buyer_price=777.25, spp_proxy=0.12)},
        sku_action_lookup={nm_id: {SELLER_PRICE_CHANGE_RUB_METRIC_KEY: -60.0, ADVERTISING_BID_CHANGE_RUB_METRIC_KEY: 3.0}},
        column_date="2026-07-13",
    )
    config = [ConfigV2Item(nm_id=nm_id, enabled=True, display_name="SKU", group="Test", display_order=1)]
    evaluator = _MetricEvaluator(
        enabled_config=config,
        metrics_by_key=by_key,
        formulas_by_id={},
        live_sources=TemporalLiveSources(
            temporal_slots=[SheetVitrinaV1TemporalSlot(slot_key="today_current", slot_label="Сегодня", column_date="2026-07-13")],
            statuses=[], slot_lookups={"today_current": lookups}, source_temporal_policies={},
        ),
    )
    if evaluator.resolve_sku(SELLER_PRICE_CHANGE_RUB_METRIC_KEY, nm_id, "today_current") != -60.0:
        raise AssertionError("daily seller price delta must be the confirmed daily sum")
    if evaluator.resolve_sku(ADVERTISING_BID_CHANGE_RUB_METRIC_KEY, nm_id, "today_current") != 3.0:
        raise AssertionError("daily bid delta must preserve all-event aggregation")
    if evaluator.resolve_sku(BUYER_PRICE_RUB_METRIC_KEY, nm_id, "today_current") != 777.25:
        raise AssertionError("buyer price metric must use observed public-card value")
    if evaluator.resolve_sku(SELLER_PRICE_CHANGE_RUB_METRIC_KEY, 999, "today_current") is not None:
        raise AssertionError("missing change must remain blank, not zero")

    registry = json.loads((ROOT / "registry" / "pilot_bundle" / "metric_runtime_registry.json").read_text(encoding="utf-8"))
    runtime_items = {item["metric_key"]: item for item in registry["items"]}
    for key in by_key:
        if key not in runtime_items or runtime_items[key]["missing_policy"] != "null":
            raise AssertionError(f"runtime metric registry missing/invalid: {key}")
    if runtime_items[BUYER_PRICE_RUB_METRIC_KEY]["source_module"] != "spp_proxy_block":
        raise AssertionError("buyer price provenance must remain the existing public-card contour")
    template = (ROOT / "packages" / "adapters" / "templates" / "sheet_vitrina_v1_web_vitrina.html").read_text(encoding="utf-8")
    default_collapsed_contract = 'new Set(["seller_price_change_rub", "advertising_bid_change_rub"])'
    if default_collapsed_contract not in template:
        raise AssertionError("the two action metrics must default to collapsed in the metric selector")
    if 'new Set(["seller_price_change_rub", "advertising_bid_change_rub", "buyer_price_rub"])' in template:
        raise AssertionError("observed buyer price must not be hidden by the action-metric default")
    print("sku_management_metrics_smoke: OK")


if __name__ == "__main__":
    main()
