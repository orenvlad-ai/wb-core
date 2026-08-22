#!/usr/bin/env python3
"""Focused acceptance smoke for the forward-only WB + FF inventory WAC."""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.inventory_cost_blend import (  # noqa: E402
    INVENTORY_COST_BLEND_EFFECTIVE_DATE,
    aggregate_inventory_cost_evidence,
    build_inventory_cost_blend_lookup,
)
from packages.application.calculation_parameters import (  # noqa: E402
    DEFAULT_PROXY_PARAMETERS,
    calculate_proxy_3,
)
from packages.application.calculation_parameters_v4 import (  # noqa: E402
    PROXY_V4_CONTRACT_VERSION,
    ProxyV4Parameters,
    calculate_proxy_4,
)
from packages.application.sheet_vitrina_v1_live_plan import (  # noqa: E402
    SlotLookups,
    TemporalLiveSources,
    _MetricEvaluator,
    _inventory_cost_evidence_reason,
)
from packages.application.sheet_vitrina_v1_our_wb_costs import (  # noqa: E402
    OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY,
    OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY,
    TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY,
    extend_metrics_with_our_wb_cost_metrics,
)
from packages.application.sheet_vitrina_v1_own_product_capital import (  # noqa: E402
    own_stage_metric_key,
)
from packages.application.own_product_capital import (  # noqa: E402
    _inventory_cost_stage_evidence,
)
from packages.application.sheet_vitrina_v1_proxy_v4 import (  # noqa: E402
    PROXY_V4_PROFIT_RUB_METRIC_KEY,
    PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY,
    PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY,
    extend_metrics_with_proxy_v4,
)
from packages.contracts.registry_upload_bundle_v1 import (  # noqa: E402
    ConfigV2Item,
    MetricV2Item,
)
from packages.contracts.sheet_vitrina_v1 import (  # noqa: E402
    SheetVitrinaV1TemporalSlot,
)


def main() -> None:
    _test_wb_only_ff_only_and_mixed_formula()
    _test_total_is_sum_capital_over_sum_quantity()
    _test_missing_location_cost_and_zero_quantity_fail_closed()
    _test_transfer_and_reserve_do_not_create_capital()
    _test_history_boundary_preserves_compatibility_rows()
    _test_proxy_3_and_4_use_per_sku_blend_not_sale_cogs()
    _test_exact_facility_provenance_and_float_projection_tolerance()
    print("inventory_cost_blend_wb_ff_formula: ok")
    print("inventory_cost_blend_location_coverage_fail_closed: ok")
    print("inventory_cost_blend_transfer_reserve_history: ok")
    print("inventory_cost_blend_proxy_3_4_per_sku_total: ok")
    print("inventory_cost_blend_exact_provenance_decimal_tolerance: ok")


def _test_wb_only_ff_only_and_mixed_formula() -> None:
    products = {
        101: _product(wb=("10", "100")),
        102: _product(
            ff=(
                "5",
                "100",
                [
                    _location("fff_moscow", "FBS", "2", "30"),
                    _location("fff_orenburg", "FBS", "2", "50"),
                    _location("fff_moscow", "FBO", "1", "20"),
                ],
            )
        ),
        103: _product(
            wb=("4", "40"),
            ff=("6", "90", [_location("fff_moscow", "FBS", "6", "90")]),
        ),
    }
    rows = build_inventory_cost_blend_lookup(
        as_of_date=INVENTORY_COST_BLEND_EFFECTIVE_DATE,
        wb_compat_lookup={},
        product_capital_lookup=products,
    )
    _equal(rows[101]["our_wb_unit_cost_rub"], Decimal("10"), "WB-only WAC")
    _equal(rows[102]["our_wb_unit_cost_rub"], Decimal("20"), "FF-only WAC")
    _equal(rows[103]["our_wb_unit_cost_rub"], Decimal("13"), "mixed SKU WAC")
    evidence = rows[102]["inventory_cost_evidence"]
    _assert(evidence["status"] == "resolved", "multiple facilities/pools resolve")
    _assert(
        {(item["facility_id"], item["pool"]) for item in evidence["stages"][1]["locations"]}
        == {
            ("fff_moscow", "FBO"),
            ("fff_moscow", "FBS"),
            ("fff_orenburg", "FBS"),
        },
        "facility and pool identities are retained",
    )
    disclosure = _inventory_cost_evidence_reason(evidence)
    _assert(
        "WB:" in disclosure
        and "FF:" in disclosure
        and "fff_orenburg/FBS" in disclosure
        and "WAC" in disclosure
        and "version whfv_exact_as_of" in disclosure
        and "published 2026-08-22T06:19:59Z" in disclosure,
        "cell evidence publishes split, facility/pool WAC, version and freshness",
    )


def _test_total_is_sum_capital_over_sum_quantity() -> None:
    products = {
        201: _product(wb=("1", "100")),
        202: _product(
            ff=("9", "90", [_location("fff_orenburg", "FBS", "9", "90")])
        ),
    }
    rows = build_inventory_cost_blend_lookup(
        as_of_date=INVENTORY_COST_BLEND_EFFECTIVE_DATE,
        wb_compat_lookup={},
        product_capital_lookup=products,
    )
    total = aggregate_inventory_cost_evidence(rows, nm_ids=[201, 202])
    _assert(total["status"] == "resolved", "TOTAL is covered")
    _assert(Decimal(total["capital_rub"]) == Decimal("190"), "TOTAL capital sums")
    _assert(Decimal(total["quantity"]) == Decimal("10"), "TOTAL quantity sums")
    _assert(Decimal(total["wac_rub"]) == Decimal("19"), "TOTAL is 190 / 10")
    _assert(
        Decimal(total["wac_rub"]) != Decimal("55"),
        "TOTAL is not the arithmetic mean of 100 and 10",
    )


def _test_missing_location_cost_and_zero_quantity_fail_closed() -> None:
    missing_location = _product(ff=("3", "30", []))
    missing_location["_inventory_cost_stages"]["FF"]["location_status"] = (
        "missing_facility_pool_evidence"
    )
    incomplete_cost = _product(wb=("5", "50"))
    incomplete_cost[own_stage_metric_key("WB", "cost_covered_qty")] = 4.0
    incomplete_cost["_inventory_cost_stages"]["WB"]["cost_covered_quantity"] = "4"
    products = {
        301: missing_location,
        302: incomplete_cost,
        303: _product(),
    }
    rows = build_inventory_cost_blend_lookup(
        as_of_date=INVENTORY_COST_BLEND_EFFECTIVE_DATE,
        wb_compat_lookup={},
        product_capital_lookup=products,
    )
    for nm_id in (301, 302, 303):
        _assert(
            rows[nm_id]["our_wb_unit_cost_rub"] is None,
            f"uncovered SKU {nm_id} is absent rather than zero",
        )
    _assert(
        "missing_facility_pool_evidence"
        in rows[301]["inventory_cost_evidence"]["reason_codes"],
        "missing FF mapping is explicit",
    )
    _assert(
        "wb_cost_coverage_incomplete"
        in rows[302]["inventory_cost_evidence"]["reason_codes"],
        "partial WB cost coverage is explicit",
    )
    total = aggregate_inventory_cost_evidence(rows, nm_ids=[301, 302, 303])
    _assert(total["status"] == "unresolved", "TOTAL rejects positive uncovered rows")
    _assert(total["missing_nm_ids"] == [301, 302], "zero inventory is not false missing")


def _test_transfer_and_reserve_do_not_create_capital() -> None:
    before = build_inventory_cost_blend_lookup(
        as_of_date=INVENTORY_COST_BLEND_EFFECTIVE_DATE,
        wb_compat_lookup={},
        product_capital_lookup={
            401: _product(
                ff=("10", "200", [_location("fff_moscow", "FBS", "10", "200")])
            )
        },
    )[401]
    after_product = _product(
        wb=("4", "80"),
        ff=("6", "120", [_location("fff_moscow", "FBS", "6", "120")]),
    )
    for stage, quantity, capital in (
        ("PRODUCTION", "100", "1000"),
        ("PRODUCTION_TO_FF", "200", "2000"),
        ("FF_TO_WB", "300", "3000"),
        ("WB_ACCEPTANCE_DISCREPANCY", "400", "4000"),
    ):
        after_product[own_stage_metric_key(stage, "qty")] = float(quantity)
        after_product[own_stage_metric_key(stage, "capital_rub")] = float(capital)
        after_product[own_stage_metric_key(stage, "cost_covered_qty")] = float(
            quantity
        )
    after = build_inventory_cost_blend_lookup(
        as_of_date=INVENTORY_COST_BLEND_EFFECTIVE_DATE,
        wb_compat_lookup={},
        product_capital_lookup={401: after_product},
    )[401]
    _equal(before["our_wb_unit_cost_rub"], Decimal("20"), "pre-transfer WAC")
    _equal(after["our_wb_unit_cost_rub"], Decimal("20"), "post-transfer WAC")
    _assert(
        Decimal(before["inventory_cost_evidence"]["capital_rub"])
        == Decimal(after["inventory_cost_evidence"]["capital_rub"])
        == Decimal("200"),
        "mutually exclusive FF/WB transfer preserves capital",
    )
    _assert(
        Decimal(after["inventory_cost_evidence"]["capital_rub"])
        == Decimal("200"),
        "production, China-to-FF, FF-to-WB and discrepancy stages are non-target",
    )
    _assert(
        after["inventory_cost_evidence"]["reserve_capital_rub"] == "0"
        and after["inventory_cost_evidence"]["quantity_basis"]
        == "physical_inventory_before_reservations",
        "reserve neither reduces physical inventory nor creates capital",
    )


def _test_history_boundary_preserves_compatibility_rows() -> None:
    legacy = {
        501: {
            "our_wb_unit_cost_rub": 77.0,
            "stock_qty": 1.0,
            "source_status": "historical_wb_compatibility",
        }
    }
    rows = build_inventory_cost_blend_lookup(
        as_of_date="2026-08-21",
        wb_compat_lookup=legacy,
        product_capital_lookup={
            501: _product(
                ff=("1", "999", [_location("fff_moscow", "FBS", "1", "999")])
            )
        },
    )
    _assert(rows == legacy, "pre-boundary ready date remains byte-semantic compatibility")


def _test_proxy_3_and_4_use_per_sku_blend_not_sale_cogs() -> None:
    legacy = {
        601: {
            "daily_profit_coverage": {
                "covered_sales_units": "1",
                "covered_sales_cogs_rub": "9999",
            }
        },
        602: {
            "daily_profit_coverage": {
                "covered_sales_units": "1",
                "covered_sales_cogs_rub": "8888",
            }
        },
    }
    blended = build_inventory_cost_blend_lookup(
        as_of_date=INVENTORY_COST_BLEND_EFFECTIVE_DATE,
        wb_compat_lookup=legacy,
        product_capital_lookup={
            601: _product(wb=("1", "100")),
            602: _product(
                ff=("9", "90", [_location("fff_orenburg", "FBS", "9", "90")])
            ),
        },
    )
    base_metrics = [
        _metric("orderSum", "SKU"),
        _metric("orderCount", "SKU"),
        _metric("ads_sum", "SKU"),
    ]
    metrics = extend_metrics_with_proxy_v4(
        extend_metrics_with_our_wb_cost_metrics(base_metrics)
    )
    config = [
        ConfigV2Item(601, True, "A", "G", 1),
        ConfigV2Item(602, True, "B", "G", 2),
    ]
    slot = SheetVitrinaV1TemporalSlot(
        slot_key="current",
        slot_label="current",
        column_date=INVENTORY_COST_BLEND_EFFECTIVE_DATE,
    )
    lookups = SlotLookups(
        seller_funnel_lookup={},
        history_lookup={
            601: {"orderSum": 1000.0, "orderCount": 1.0},
            602: {"orderSum": 2000.0, "orderCount": 2.0},
        },
        web_lookup={},
        prices_lookup={},
        sf_period_lookup={},
        spp_lookup={},
        ads_bids_lookup={},
        stocks_lookup={},
        onec_stocks_lookup={},
        ads_compact_lookup={601: {"ads_sum": 0.0}, 602: {"ads_sum": 0.0}},
        fin_lookup={},
        fin_storage_fee_total=None,
        cost_price_lookup={},
        promo_lookup={},
        our_wb_cost_lookup=blended,
        column_date=INVENTORY_COST_BLEND_EFFECTIVE_DATE,
    )
    v4_parameters = _proxy_v4_parameters()
    evaluator = _MetricEvaluator(
        enabled_config=config,
        metrics_by_key={item.metric_key: item for item in metrics},
        formulas_by_id={},
        live_sources=TemporalLiveSources(
            temporal_slots=[slot],
            statuses=[],
            slot_lookups={"current": lookups},
            source_temporal_policies={},
        ),
        proxy_parameters_resolver=lambda _date: DEFAULT_PROXY_PARAMETERS,
        proxy_v4_parameters_resolver=lambda _date: v4_parameters,
    )
    expected_v3 = {
        601: calculate_proxy_3(
            order_sum=1000,
            order_count=1,
            canonical_wb_wac=100,
            ads_sum=0,
            parameters=DEFAULT_PROXY_PARAMETERS,
        )["proxy_profit_3"],
        602: calculate_proxy_3(
            order_sum=2000,
            order_count=2,
            canonical_wb_wac=10,
            ads_sum=0,
            parameters=DEFAULT_PROXY_PARAMETERS,
        )["proxy_profit_3"],
    }
    expected_v4 = {
        601: calculate_proxy_4(
            order_sum=1000,
            order_count=1,
            canonical_wb_wac=100,
            ads_sum=0,
            parameters=v4_parameters,
            business_date=INVENTORY_COST_BLEND_EFFECTIVE_DATE,
        )["proxy_profit_4"],
        602: calculate_proxy_4(
            order_sum=2000,
            order_count=2,
            canonical_wb_wac=10,
            ads_sum=0,
            parameters=v4_parameters,
            business_date=INVENTORY_COST_BLEND_EFFECTIVE_DATE,
        )["proxy_profit_4"],
    }
    for nm_id in (601, 602):
        _equal(
            evaluator.resolve_sku(
                OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY, nm_id, "current"
            ),
            expected_v3[nm_id],
            f"Proxy 3 SKU {nm_id} uses blended WAC",
        )
        _equal(
            evaluator.resolve_sku(PROXY_V4_PROFIT_RUB_METRIC_KEY, nm_id, "current"),
            expected_v4[nm_id],
            f"Proxy 4 SKU {nm_id} uses blended WAC",
        )
    _equal(
        evaluator.resolve_total(TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY, "current"),
        Decimal("19"),
        "visible TOTAL uses exact aggregate capital/quantity",
    )
    expected_total_v3 = expected_v3[601] + expected_v3[602]
    expected_total_v4 = expected_v4[601] + expected_v4[602]
    _equal(
        evaluator.resolve_total(OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY, "current"),
        expected_total_v3,
        "Proxy 3 TOTAL sums per-SKU calculations",
    )
    _equal(
        evaluator.resolve_total(PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY, "current"),
        expected_total_v4,
        "Proxy 4 TOTAL sums per-SKU calculations",
    )
    _equal(
        evaluator.resolve_total(OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY, "current"),
        expected_total_v3 / (Decimal("3000") * DEFAULT_PROXY_PARAMETERS.buyout_rate),
        "Proxy 3 TOTAL margin divides summed profit by summed proxy revenue",
    )
    _equal(
        evaluator.resolve_total(PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY, "current"),
        expected_total_v4 / Decimal("3000"),
        "Proxy 4 TOTAL margin divides summed profit by summed proxy revenue",
    )


def _test_exact_facility_provenance_and_float_projection_tolerance() -> None:
    exact_capital = Decimal("12975660.23477445249682971490")
    row = {
        "quantity": "114854",
        "capital_rub": format(exact_capital, "f"),
        "cost_covered_quantity": "114854",
        "quality": "moving_weighted_average",
        "certified": 1,
        "provenance_json": json.dumps(
            {
                "source_records": [
                    {
                        "locations": [
                            {
                                "facility_id": "fff_moscow",
                                "pool": "FBS",
                                "quantity": "88123",
                                "capital_rub": "10043396.44535487673925395733",
                            },
                            {
                                "facility_id": "fff_orenburg",
                                "pool": "FBS",
                                "quantity": "26731",
                                "capital_rub": "2932263.789419575757575757576",
                            },
                        ]
                    }
                ]
            },
            sort_keys=True,
        ),
    }
    stage = _inventory_cost_stage_evidence(row, public_stage="FF")
    _assert(stage["location_status"] == "exact", "exact FF provenance resolves")
    _assert(
        sum(Decimal(item["quantity"]) for item in stage["locations"])
        == Decimal("114854"),
        "facility quantities reconcile",
    )
    product = _product()
    product[own_stage_metric_key("FF", "qty")] = float(Decimal("114854"))
    product[own_stage_metric_key("FF", "capital_rub")] = float(exact_capital)
    product[own_stage_metric_key("FF", "cost_covered_qty")] = float(
        Decimal("114854")
    )
    product["_inventory_cost_stages"]["FF"] = stage  # type: ignore[index]
    blended = build_inventory_cost_blend_lookup(
        as_of_date=INVENTORY_COST_BLEND_EFFECTIVE_DATE,
        wb_compat_lookup={},
        product_capital_lookup={701: product},
    )[701]
    _assert(
        blended["inventory_cost_evidence"]["status"] == "resolved",
        "exact Decimal evidence tolerates its micro-ruble float projection",
    )
    _assert(
        Decimal(blended["inventory_cost_evidence"]["capital_rub"])
        == exact_capital,
        "blended capital retains the exact Decimal source rather than float text",
    )


def _metric(metric_key: str, scope: str) -> MetricV2Item:
    return MetricV2Item(
        metric_key=metric_key,
        enabled=True,
        scope=scope,
        label_ru=metric_key,
        calc_type="metric",
        calc_ref=metric_key,
        show_in_data=True,
        format="decimal",
        display_order=1,
        section="smoke",
    )


def _proxy_v4_parameters() -> ProxyV4Parameters:
    return ProxyV4Parameters(
        effective_date=INVENTORY_COST_BLEND_EFFECTIVE_DATE,
        buyout_rate=Decimal("1"),
        tax_rate=Decimal("0"),
        agent_remuneration_rate=Decimal("0"),
        acquiring_rate=Decimal("0"),
        wb_logistics_rate=Decimal("0"),
        wb_storage_rate=Decimal("0"),
        penalties_adjustments_rate=Decimal("0"),
        other_expense_rate=Decimal("0"),
        source_window_from="2026-08-10",
        source_window_to="2026-08-16",
        source_window_fingerprint="sha256:synthetic",
        source_week_ranges=(("2026-08-10", "2026-08-16"),),
        source_slot_from="2026-08-10",
        source_slot_to="2026-08-16",
        buyout_order_count_weight=Decimal("1"),
        finance_net_revenue_weight=Decimal("1"),
        formula_version=PROXY_V4_CONTRACT_VERSION,
    )


def _product(
    *,
    wb: tuple[str, str] | None = None,
    ff: tuple[str, str, list[dict[str, str]]] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "_warehouse_version_id": "whfv_exact_as_of",
        "_warehouse_effective_at": "2026-08-22T06:18:57Z",
        "_warehouse_published_at": "2026-08-22T06:19:59Z",
        "_warehouse_source_watermarks": {"ff_ledger_revision": "rev-1"},
        "_inventory_cost_stages": {},
    }
    for stage in ("WB", "FF"):
        result[own_stage_metric_key(stage, "qty")] = 0.0
        result[own_stage_metric_key(stage, "capital_rub")] = 0.0
        result[own_stage_metric_key(stage, "cost_covered_qty")] = 0.0
    if wb is not None:
        quantity, capital = wb
        result[own_stage_metric_key("WB", "qty")] = float(quantity)
        result[own_stage_metric_key("WB", "capital_rub")] = float(capital)
        result[own_stage_metric_key("WB", "cost_covered_qty")] = float(quantity)
        result["_inventory_cost_stages"]["WB"] = _stage(  # type: ignore[index]
            "WB",
            quantity,
            capital,
            [_location("", "WB", quantity, capital)],
        )
    if ff is not None:
        quantity, capital, locations = ff
        result[own_stage_metric_key("FF", "qty")] = float(quantity)
        result[own_stage_metric_key("FF", "capital_rub")] = float(capital)
        result[own_stage_metric_key("FF", "cost_covered_qty")] = float(quantity)
        result["_inventory_cost_stages"]["FF"] = _stage(  # type: ignore[index]
            "FF", quantity, capital, locations
        )
    return result


def _stage(
    family: str,
    quantity: str,
    capital: str,
    locations: list[dict[str, str]],
) -> dict[str, object]:
    return {
        "warehouse_family": family,
        "quantity": quantity,
        "capital_rub": capital,
        "cost_covered_quantity": quantity,
        "quality": "moving_weighted_average",
        "certified": True,
        "location_status": "exact",
        "locations": locations,
    }


def _location(
    facility_id: str,
    pool: str,
    quantity: str,
    capital: str,
) -> dict[str, str]:
    return {
        "warehouse_family": "WB" if pool == "WB" else "FF",
        "facility_id": facility_id,
        "pool": pool,
        "quantity": quantity,
        "capital_rub": capital,
        "wac_rub": format(Decimal(capital) / Decimal(quantity), "f"),
    }


def _equal(actual: object, expected: Decimal, label: str) -> None:
    _assert(Decimal(str(actual)) == expected, label)


def _assert(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)


if __name__ == "__main__":
    main()
