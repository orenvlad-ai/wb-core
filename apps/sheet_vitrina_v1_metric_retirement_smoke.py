"""Proof that retired stock/cost metrics stay out of the active public catalog."""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_http_entrypoint import (  # noqa: E402
    _active_incident_metric_catalog,
    _active_web_vitrina_source_keys,
    _metric_keys_for_source_keys,
)
from packages.application.sheet_vitrina_v1_archived_metrics import (  # noqa: E402
    ARCHIVED_ONLY_SOURCE_KEYS,
    ARCHIVED_PUBLIC_METRIC_KEYS,
    LEGACY_COST_PROXY_1_ARCHIVED_METRIC_KEYS,
    filter_archived_public_metrics,
)
from packages.application.sheet_vitrina_v1_incident_stocks import (  # noqa: E402
    INCIDENT_STOCK_FACT_METRIC_KEYS,
    INCIDENT_STOCK_FIELDS,
    extend_metrics_with_incident_stock_metrics,
    incident_stock_metric_key,
    incident_stock_total_metric_key,
    incident_stock_value,
)
from packages.application.sheet_vitrina_v1_our_wb_costs import (  # noqa: E402
    OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
    OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_UNIT_COST_RUB_METRIC_KEY,
    extend_metrics_with_our_wb_cost_metrics,
)
from packages.application.sheet_vitrina_v1_own_product_capital import (  # noqa: E402
    OWN_TOTAL_CAPITAL_RUB_METRIC_KEY,
    extend_metrics_with_own_product_capital_metrics,
)
from packages.application.stocks_block import build_wb_warehouse_exclusion  # noqa: E402
from packages.contracts.registry_upload_bundle_v1 import MetricV2Item  # noqa: E402
from packages.contracts.stocks_block import StocksItem, StocksWarehouseRow  # noqa: E402

BUNDLE_FIXTURE = (
    ROOT
    / "artifacts"
    / "registry_upload_http_entrypoint"
    / "input"
    / "registry_upload_bundle__fixture.json"
)
RUNTIME_REGISTRY_FIXTURE = (
    ROOT
    / "artifacts"
    / "registry_upload_bundle_v1"
    / "input"
    / "metric_runtime_registry__fixture.json"
)


def main() -> None:
    _assert_stock_fact_is_duplicate_projection()
    _assert_public_catalog_retirement()
    _assert_legacy_cost_source_and_dependency_closure()
    print("sheet_vitrina_v1_metric_retirement: ok")


def _assert_stock_fact_is_duplicate_projection() -> None:
    items = [
        StocksItem(
            nm_id=101,
            stock_total=42,
            stock_ru_central=12,
            stock_ru_northwest=5,
            stock_ru_volga=6,
            stock_ru_ural=7,
            stock_ru_south_caucasus=4,
            stock_ru_far_siberia=8,
        ),
        StocksItem(
            nm_id=202,
            stock_total=21,
            stock_ru_central=2,
            stock_ru_northwest=3,
            stock_ru_volga=4,
            stock_ru_ural=5,
            stock_ru_south_caucasus=1,
            stock_ru_far_siberia=6,
        ),
    ]
    warehouse_rows = [
        StocksWarehouseRow(
            nm_id=101,
            warehouse_id=9001,
            warehouse_name="Fixture Central",
            region_name="Центральный",
            quantity=3,
            planning_zone_key=None,
            classification_status="mapped",
            classification_source="fixture",
        )
    ]
    projection = build_wb_warehouse_exclusion(
        items=items,
        warehouse_rows=warehouse_rows,
        excluded_warehouse_ids=(9001,),
        snapshot_date="2026-07-27",
        fetched_at="2026-07-27T08:00:00Z",
        pagination_complete=True,
        raw_rows_digest="sha256:fixture",
    )
    row = projection["by_nm_id"]["101"]
    for region, source_field, _suffix in INCIDENT_STOCK_FIELDS:
        expected = float(getattr(items[0], source_field))
        fact_key = incident_stock_metric_key("fact", region)
        if incident_stock_value(fact_key, row) != expected:
            raise AssertionError(
                f"{fact_key} must be the same StocksItem projection as {source_field}"
            )
        projected_total = sum(
            float(
                incident_stock_value(
                    fact_key,
                    projection["by_nm_id"][str(item.nm_id)],
                )
                or 0
            )
            for item in items
        )
        canonical_total = sum(float(getattr(item, source_field)) for item in items)
        if projected_total != canonical_total:
            raise AssertionError(
                f"{incident_stock_total_metric_key('fact', region)} must aggregate "
                f"the same values as canonical {source_field}"
            )
    if incident_stock_value(incident_stock_metric_key("incident"), row) != 3:
        raise AssertionError("incident stock must retain the excluded warehouse quantity")
    if incident_stock_value(incident_stock_metric_key("effective"), row) != 39:
        raise AssertionError("effective stock must retain fact-minus-incident semantics")


def _assert_public_catalog_retirement() -> None:
    payload = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    metrics = [_metric_from_dict(item) for item in payload["metrics_v2"]]
    full_catalog = extend_metrics_with_incident_stock_metrics(
        extend_metrics_with_own_product_capital_metrics(
            extend_metrics_with_our_wb_cost_metrics(metrics)
        )
    )
    active_keys = {
        item.metric_key
        for item in filter_archived_public_metrics(full_catalog)
        if item.enabled and item.show_in_data
    }
    retired = {
        *INCIDENT_STOCK_FACT_METRIC_KEYS,
        *LEGACY_COST_PROXY_1_ARCHIVED_METRIC_KEYS,
    }
    full_keys = {item.metric_key for item in full_catalog}
    missing_audit_definitions = sorted(retired - full_keys)
    if missing_audit_definitions:
        raise AssertionError(
            "retirement must preserve evaluator/catalog definitions for audit: "
            f"{missing_audit_definitions}"
        )
    leaked = sorted(active_keys & retired)
    if leaked:
        raise AssertionError(f"retired metrics leaked into active public catalog: {leaked}")

    required = {
        "stock_total",
        "total_stock_total",
        "stock_ru_central",
        "total_stock_ru_central",
        incident_stock_metric_key("incident"),
        incident_stock_total_metric_key("incident"),
        incident_stock_metric_key("effective"),
        incident_stock_total_metric_key("effective"),
        OUR_WB_UNIT_COST_RUB_METRIC_KEY,
        OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
        OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
        OWN_TOTAL_CAPITAL_RUB_METRIC_KEY,
    }
    missing = sorted(required - active_keys)
    if missing:
        raise AssertionError(f"canonical metrics disappeared with retired families: {missing}")

    picker_keys = {str(item["metric_key"]) for item in _active_incident_metric_catalog()}
    if picker_keys & retired:
        raise AssertionError("retired fact metrics leaked into the picker-only catalog")
    for variant in ("incident", "effective"):
        expected = {
            incident_stock_metric_key(variant, region)
            for region, _source, _suffix in INCIDENT_STOCK_FIELDS
        } | {
            incident_stock_total_metric_key(variant, region)
            for region, _source, _suffix in INCIDENT_STOCK_FIELDS
        }
        if not expected <= picker_keys:
            raise AssertionError(f"{variant} family must remain available in the picker")


def _assert_legacy_cost_source_and_dependency_closure() -> None:
    registry = json.loads(RUNTIME_REGISTRY_FIXTURE.read_text(encoding="utf-8"))
    items = {str(item["metric_key"]): item for item in registry["items"]}
    cost = items["cost_price_rub"]
    if (
        cost.get("source_family") != "group_rules"
        or cost.get("source_module") != "cogs_by_group_block"
        or cost.get("period_agg") != "last_snapshot"
    ):
        raise AssertionError(f"legacy cost source contract changed unexpectedly: {cost}")
    required_retirement = {
        "cost_price_rub",
        "avg_cost_price_rub",
        "proxy_profit_rub",
        "total_proxy_profit_rub",
        "proxy_margin_pct",
        "proxy_margin_pct_total",
    }
    if not required_retirement <= ARCHIVED_PUBLIC_METRIC_KEYS:
        raise AssertionError("legacy cost/Proxy1 dependency closure is only partially retired")
    if "cost_price" not in ARCHIVED_ONLY_SOURCE_KEYS:
        raise AssertionError("audit-only COST_PRICE must not affect public refresh status")

    payload = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    metrics = [_metric_from_dict(item) for item in payload["metrics_v2"]]
    public_cost_keys = _metric_keys_for_source_keys(metrics, source_keys=["cost_price"])
    if public_cost_keys:
        raise AssertionError(
            f"audit-only COST_PRICE still owns refreshable public metrics: {public_cost_keys}"
        )
    if "cost_price" in _active_web_vitrina_source_keys():
        raise AssertionError("audit-only COST_PRICE still appears as an active public source")


def _metric_from_dict(item: dict[str, object]) -> MetricV2Item:
    return MetricV2Item(
        metric_key=str(item["metric_key"]),
        enabled=bool(item["enabled"]),
        scope=str(item["scope"]),
        label_ru=str(item["label_ru"]),
        calc_type=item["calc_type"],
        calc_ref=str(item["calc_ref"]),
        show_in_data=bool(item["show_in_data"]),
        format=str(item["format"]),
        display_order=int(item["display_order"]),
        section=str(item["section"]),
    )


if __name__ == "__main__":
    main()
