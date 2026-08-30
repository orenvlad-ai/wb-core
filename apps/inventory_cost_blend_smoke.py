#!/usr/bin/env python3
"""Focused acceptance smoke for the forward-only WB + FF inventory WAC."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import inspect
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
    OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
    OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY,
    OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_UNIT_COST_RUB_METRIC_KEY,
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
    PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY,
    PROXY_V4_MARGIN_PCT_METRIC_KEY,
    PROXY_V4_PROFIT_RUB_METRIC_KEY,
    PROXY_V4_TOTAL_MARGIN_PER_UNIT_RUB_METRIC_KEY,
    PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY,
    PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY,
    extend_metrics_with_proxy_v4,
)
from packages.application.warehouse_functional_economics_backfill import (  # noqa: E402
    HISTORICAL_REPAIR_METADATA_KEY,
    _transform_snapshot,
    build_functional_economics_backfill_plan,
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
    _test_ordinary_functional_economics_publication()
    print("inventory_cost_blend_wb_ff_formula: ok")
    print("inventory_cost_blend_location_coverage_fail_closed: ok")
    print("inventory_cost_blend_transfer_reserve_history: ok")
    print("inventory_cost_blend_proxy_3_4_per_sku_total: ok")
    print("inventory_cost_blend_exact_provenance_decimal_tolerance: ok")
    print("inventory_cost_blend_ordinary_publisher_before_after_noop: ok")


def _test_ordinary_functional_economics_publication() -> None:
    publisher_source = inspect.getsource(build_functional_economics_backfill_plan)
    _assert(
        publisher_source.index("warehouse_metrics =")
        < publisher_source.index("\n    costs = {")
        and "build_inventory_cost_blend_lookup(" in publisher_source
        and '"wb_compat_costs": wb_compat_costs' in publisher_source
        and '"proxy_v4_parameters": {' in publisher_source,
        "ordinary planning builds exact-date capital before one versioned blend dependency",
    )
    dates = ["2026-08-21", INVENTORY_COST_BLEND_EFFECTIVE_DATE]
    products = {
        801: _product(
            wb=("1", "100"),
            ff=(
                "9",
                "90",
                [_location("fff_moscow", "FBS", "9", "90")],
            ),
        ),
        802: _product(wb=("5", "250")),
        803: _product(),
    }
    wb_compat = {
        801: {"our_wb_unit_cost_rub": 100.0, "stock_qty": 1.0},
        802: {"our_wb_unit_cost_rub": 50.0, "stock_qty": 5.0},
    }
    blended = build_inventory_cost_blend_lookup(
        as_of_date=INVENTORY_COST_BLEND_EFFECTIVE_DATE,
        wb_compat_lookup=wb_compat,
        product_capital_lookup=products,
    )
    v3 = {day: DEFAULT_PROXY_PARAMETERS for day in dates}
    v4 = {day: _proxy_v4_parameters() for day in dates}
    snapshot = {
        "bundle_version": "ordinary-publisher-smoke",
        "as_of_date": INVENTORY_COST_BLEND_EFFECTIVE_DATE,
        "refreshed_at": "2026-08-22T06:20:00Z",
        "plan_json": json.dumps(
            {
                "date_columns": dates,
                "sheets": [
                    {
                        "sheet_name": "DATA_VITRINA",
                        "write_start_cell": "A1",
                        "header": ["Показатель", "row_id", *dates],
                        "rows": [
                            ["SKU 801", "SKU:801|orderSum", 1000, 1000],
                            ["SKU 801", "SKU:801|orderCount", 10, 10],
                            ["SKU 801", "SKU:801|ads_sum", 100, 100],
                            ["SKU 802", "SKU:802|orderSum", 500, 500],
                            ["SKU 802", "SKU:802|orderCount", 5, 5],
                            ["SKU 802", "SKU:802|ads_sum", 50, 50],
                            ["SKU 803", "SKU:803|orderSum", 0, 0],
                            ["SKU 803", "SKU:803|orderCount", 0, 0],
                            ["SKU 803", "SKU:803|ads_sum", 0, 0],
                            ["cost 801", "SKU:801|our_wb_unit_cost_rub", 100, 100],
                            ["cost 802", "SKU:802|our_wb_unit_cost_rub", 50, 50],
                            ["TOTAL cost", "TOTAL|total_our_wb_unit_cost_rub", 58.333333333333336, 58.333333333333336],
                            ["Proxy 4 801", "SKU:801|proxy_profit_4_rub", 111, 111],
                            ["Proxy 4 802", "SKU:802|proxy_profit_4_rub", 222, 222],
                            ["Proxy 4 margin 801", "SKU:801|proxy_margin_4_pct", 0.1, 0.1],
                            ["Proxy 4 margin 802", "SKU:802|proxy_margin_4_pct", 0.2, 0.2],
                            ["Proxy 4 unit 801", "SKU:801|proxy_margin_per_unit_rub", 11, 11],
                            ["Proxy 4 unit 802", "SKU:802|proxy_margin_per_unit_rub", 22, 22],
                            ["TOTAL Proxy 4", "TOTAL|total_proxy_profit_4_rub", 333, 333],
                            ["TOTAL Proxy 4 margin", "TOTAL|proxy_margin_4_pct_total", 0.15, 0.15],
                            ["TOTAL Proxy 4 unit", "TOTAL|proxy_margin_per_unit_rub_total", 16.5, 16.5],
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
    }
    result = _transform_snapshot(
        snapshot,
        costs={dates[0]: wb_compat, dates[1]: blended},
        warehouse_metrics={dates[0]: products, dates[1]: products},
        warehouse_exact_dates=set(dates),
        warehouse_covered_nm_ids={day: {801, 802, 803} for day in dates},
        warehouse_version_ids={day: "whfv_exact_as_of" for day in dates},
        parameters=v3,
        proxy_v4_parameters=v4,
        source_fingerprint="sha256:ordinary-publisher-shared-source",
        cutover_business_date="2026-07-18",
        operation_business_date=INVENTORY_COST_BLEND_EFFECTIVE_DATE,
    )
    payload = json.loads(result["after_plan_json"])
    rows = {row[1]: row for row in payload["sheets"][0]["rows"]}
    _equal(
        rows["SKU:801|our_wb_unit_cost_rub"][3],
        Decimal("19"),
        "ordinary publisher replaces WB-only cost with exact WB+FF SKU WAC",
    )
    _equal(
        rows["TOTAL|total_our_wb_unit_cost_rub"][3],
        Decimal("29.333333333333332"),
        "ordinary publisher TOTAL is sum capital / sum quantity",
    )
    expected_v3 = calculate_proxy_3(
        order_sum=1000,
        order_count=10,
        canonical_wb_wac=19,
        ads_sum=100,
        parameters=DEFAULT_PROXY_PARAMETERS,
    )
    expected_v4 = calculate_proxy_4(
        order_sum=1000,
        order_count=10,
        canonical_wb_wac=19,
        ads_sum=100,
        parameters=_proxy_v4_parameters(),
        business_date=INVENTORY_COST_BLEND_EFFECTIVE_DATE,
    )
    _equal(
        rows["SKU:801|proxy_profit_3_rub"][3],
        expected_v3["proxy_profit_3"],
        "ordinary Proxy 3 consumes the same SKU blend",
    )
    _equal(
        rows["SKU:801|proxy_profit_4_rub"][3],
        expected_v4["proxy_profit_4"],
        "ordinary Proxy 4 consumes the same SKU blend",
    )
    _equal(
        rows["TOTAL|total_proxy_profit_4_rub"][3],
        Decimal(str(rows["SKU:801|proxy_profit_4_rub"][3]))
        + Decimal(str(rows["SKU:802|proxy_profit_4_rub"][3])),
        "ordinary Proxy 4 TOTAL sums eligible SKU results",
    )
    _assert(
        abs(
            Decimal(str(rows["TOTAL|proxy_margin_4_pct_total"][3]))
            - Decimal(str(rows["TOTAL|total_proxy_profit_4_rub"][3]))
            / Decimal("1500")
        )
        < Decimal("0.000000000000001"),
        "ordinary Proxy 4 TOTAL margin uses summed eligible revenue",
    )
    _assert(
        abs(
            Decimal(str(rows["TOTAL|proxy_margin_per_unit_rub_total"][3]))
            - Decimal(str(rows["TOTAL|total_proxy_profit_4_rub"][3]))
            / Decimal("15")
        )
        < Decimal("0.00000000000001"),
        "ordinary Proxy 4 TOTAL unit margin uses summed eligible quantity",
    )
    marker = payload["metadata"]["functional_economics_backfill"]
    evidence = marker["inventory_cost_publication"]["date_evidence"][dates[1]]
    _assert(
        marker["source_fingerprint"]
        == "sha256:ordinary-publisher-shared-source"
        and marker["inventory_cost_publication"]["formula_version"]
        == "our_inventory_wac_wb_ff_v1"
        and evidence["functional_version_ids"] == ["whfv_exact_as_of"]
        and Decimal(evidence["capital_rub"]) == Decimal("440")
        and Decimal(evidence["quantity"]) == Decimal("15"),
        "cost, Proxy 3 and Proxy 4 publish one versioned dependency evidence",
    )
    _assert(
        rows["SKU:803|our_wb_unit_cost_rub"][3] == ""
        and rows["SKU:803|proxy_profit_3_rub"][3] == ""
        and rows["SKU:803|proxy_profit_4_rub"][3] == ""
        and rows["TOTAL|total_proxy_profit_4_rub"][3] != "",
        "zero-order/no-inventory SKU remains missing while eligible Proxy totals stay published",
    )
    presentation = payload["metadata"]["server_cell_presentation"][
        "TOTAL|total_our_wb_unit_cost_rub"
    ][dates[1]]
    _assert(
        presentation["source"] == "WebCore · WB+FF"
        and "version whfv_exact_as_of" in presentation["reason"],
        "ordinary cost presentation retains exact source version and freshness",
    )
    repeated = _transform_snapshot(
        {**snapshot, "plan_json": result["after_plan_json"]},
        costs={dates[0]: wb_compat, dates[1]: blended},
        warehouse_metrics={dates[0]: products, dates[1]: products},
        warehouse_exact_dates=set(dates),
        warehouse_covered_nm_ids={day: {801, 802, 803} for day in dates},
        warehouse_version_ids={day: "whfv_exact_as_of" for day in dates},
        parameters=v3,
        proxy_v4_parameters=v4,
        source_fingerprint="sha256:ordinary-publisher-shared-source",
        cutover_business_date="2026-07-18",
        operation_business_date=INVENTORY_COST_BLEND_EFFECTIVE_DATE,
    )
    _assert(
        repeated["changed_cells"] == 0
        and repeated["inserted_rows"] == 0
        and repeated["presentation_changes"] == 0
        and repeated["coverage_changes"] == 0,
        "ordinary publisher exact repeat is a no-op",
    )
    missing_positive_payload = json.loads(result["after_plan_json"])
    missing_positive_rows = {
        row[1]: row
        for row in missing_positive_payload["sheets"][0]["rows"]
    }
    missing_positive_rows["SKU:803|orderSum"][3] = 100
    missing_positive = _transform_snapshot(
        {
            **snapshot,
            "plan_json": json.dumps(
                missing_positive_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
        costs={dates[0]: wb_compat, dates[1]: blended},
        warehouse_metrics={dates[0]: products, dates[1]: products},
        warehouse_exact_dates=set(dates),
        warehouse_covered_nm_ids={day: {801, 802, 803} for day in dates},
        warehouse_version_ids={day: "whfv_exact_as_of" for day in dates},
        parameters=v3,
        proxy_v4_parameters=v4,
        source_fingerprint="sha256:ordinary-publisher-missing-positive",
        cutover_business_date="2026-07-18",
        operation_business_date=INVENTORY_COST_BLEND_EFFECTIVE_DATE,
    )
    missing_positive_after = {
        row[1]: row
        for row in json.loads(missing_positive["after_plan_json"])["sheets"][0][
            "rows"
        ]
    }
    _assert(
        missing_positive_after["TOTAL|total_proxy_profit_3_rub"][3] == ""
        and missing_positive_after["TOTAL|total_proxy_profit_4_rub"][3] == ""
        and missing_positive_after["TOTAL|proxy_margin_4_pct_total"][3] == "",
        "positive orders with missing blended cost fail closed for both Proxy totals",
    )

    late_payload = json.loads(result["after_plan_json"])
    current_day = "2026-08-23"
    late_payload["date_columns"].append(current_day)
    late_sheet = late_payload["sheets"][0]
    late_sheet["header"].append(current_day)
    for row in late_sheet["rows"]:
        row.append(deepcopy(row[3] if len(row) > 3 else ""))
    late_rows = {row[1]: row for row in late_sheet["rows"]}
    late_rows["SKU:801|proxy_margin_4_pct"][3] = 0
    protected_keys = {
        OUR_WB_UNIT_COST_RUB_METRIC_KEY,
        OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
        OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
        PROXY_V4_PROFIT_RUB_METRIC_KEY,
        PROXY_V4_MARGIN_PCT_METRIC_KEY,
        PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY,
    }
    protected_before = {
        row_id: deepcopy(row[3])
        for row_id, row in late_rows.items()
        if row_id.startswith("SKU:801|")
        and row_id.split("|", 1)[1] in protected_keys
    }
    non_target_before = {
        row_id: deepcopy(row[3:5])
        for row_id, row in late_rows.items()
        if row_id in {
            "SKU:801|orderSum",
            "SKU:801|orderCount",
            "SKU:801|ads_sum",
        }
    }
    mismatch_products = deepcopy(products)
    mismatch_products[801][own_stage_metric_key("FF", "qty")] = 8.0
    mismatch_blended = build_inventory_cost_blend_lookup(
        as_of_date=INVENTORY_COST_BLEND_EFFECTIVE_DATE,
        wb_compat_lookup=wb_compat,
        product_capital_lookup=mismatch_products,
    )
    current_products = deepcopy(products)
    for product in current_products.values():
        product["_warehouse_version_id"] = "whfv_current"
        product["_warehouse_effective_at"] = "2026-08-23T06:18:57Z"
        product["_warehouse_published_at"] = "2026-08-23T06:19:59Z"
    current_blended = build_inventory_cost_blend_lookup(
        as_of_date=current_day,
        wb_compat_lookup=wb_compat,
        product_capital_lookup=current_products,
    )
    late_snapshot = {
        **snapshot,
        "as_of_date": current_day,
        "refreshed_at": "2026-08-23T06:20:00Z",
        "plan_json": json.dumps(
            late_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    late_result = _transform_snapshot(
        late_snapshot,
        costs={
            dates[0]: wb_compat,
            dates[1]: mismatch_blended,
            current_day: current_blended,
        },
        warehouse_metrics={
            dates[0]: products,
            dates[1]: mismatch_products,
            current_day: current_products,
        },
        warehouse_exact_dates={*dates, current_day},
        warehouse_covered_nm_ids={
            day: {801, 802, 803} for day in [*dates, current_day]
        },
        warehouse_version_ids={
            dates[0]: "whfv_exact_as_of",
            dates[1]: "whfv_exact_as_of",
            current_day: "whfv_current",
        },
        parameters={**v3, current_day: DEFAULT_PROXY_PARAMETERS},
        proxy_v4_parameters={**v4, current_day: _proxy_v4_parameters()},
        source_fingerprint="sha256:late-warehouse-revision",
        cutover_business_date="2026-07-18",
        operation_business_date=current_day,
    )
    late_after = json.loads(late_result["after_plan_json"])
    late_after_rows = {row[1]: row for row in late_after["sheets"][0]["rows"]}
    _assert(
        {
            row_id: deepcopy(late_after_rows[row_id][3])
            for row_id in protected_before
        }
        == protected_before,
        "late warehouse revision cannot rewrite a closed cost/Proxy image",
    )
    _assert(
        late_after_rows["SKU:801|proxy_margin_4_pct"][3] == 0,
        "closed exact zero remains exact zero instead of becoming missing",
    )
    _assert(
        {
            row_id: deepcopy(late_after_rows[row_id][3:5])
            for row_id in non_target_before
        }
        == non_target_before,
        "orders and ads remain outside historical economics publication",
    )
    _equal(
        late_after_rows["SKU:801|our_wb_unit_cost_rub"][4],
        Decimal("19"),
        "current date continues through the ordinary publisher",
    )
    repair_registry = late_after["metadata"][HISTORICAL_REPAIR_METADATA_KEY]
    repair_day = repair_registry["dates"][INVENTORY_COST_BLEND_EFFECTIVE_DATE]
    cost_issues = [
        issue
        for issue in repair_day["issues"]
        if issue["scope"] == "SKU:801"
        and issue["family"] == "our_wb_cost_proxy_3_4"
    ]
    _assert(
        repair_day["status"] == "historical_repair_required"
        and repair_day["ordinary_publication_applied"] is False
        and any(
            "ff_stage_evidence_mismatch" in issue["reason_codes"]
            for issue in cost_issues
        )
        and all(issue["last_good_preserved"] for issue in cost_issues),
        "closed mismatch emits typed version-bound repair evidence",
    )
    _assert(
        not any(
            issue["scope"] == "SKU:803"
            and issue["family"] == "our_wb_cost_proxy_3_4"
            for issue in repair_day["issues"]
        ),
        "zero-denominator/no-inventory remains distinct from historical missing",
    )
    _assert(
        late_after["metadata"]["warehouse_history_coverage"][
            INVENTORY_COST_BLEND_EFFECTIVE_DATE
        ]["status"]
        == "historical_repair_required"
        and late_after["metadata"]["warehouse_history_coverage"][current_day][
            "status"
        ]
        == "live",
        "closed mismatch is non-green while the current date remains live",
    )
    late_repeat = _transform_snapshot(
        {**late_snapshot, "plan_json": late_result["after_plan_json"]},
        costs={
            dates[0]: wb_compat,
            dates[1]: mismatch_blended,
            current_day: current_blended,
        },
        warehouse_metrics={
            dates[0]: products,
            dates[1]: mismatch_products,
            current_day: current_products,
        },
        warehouse_exact_dates={*dates, current_day},
        warehouse_covered_nm_ids={
            day: {801, 802, 803} for day in [*dates, current_day]
        },
        warehouse_version_ids={
            dates[0]: "whfv_exact_as_of",
            dates[1]: "whfv_exact_as_of",
            current_day: "whfv_current",
        },
        parameters={**v3, current_day: DEFAULT_PROXY_PARAMETERS},
        proxy_v4_parameters={**v4, current_day: _proxy_v4_parameters()},
        source_fingerprint="sha256:late-warehouse-revision",
        cutover_business_date="2026-07-18",
        operation_business_date=current_day,
    )
    _assert(
        late_repeat["changed_cells"] == 0
        and late_repeat["presentation_changes"] == 0
        and late_repeat["coverage_changes"] == 0
        and late_repeat["repair_signal_changes"] == 0,
        "repeated closed-history protection is idempotent",
    )


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
    zero_cost = _product(wb=("2", "0"))
    products = {
        301: missing_location,
        302: incomplete_cost,
        303: _product(),
        304: zero_cost,
    }
    rows = build_inventory_cost_blend_lookup(
        as_of_date=INVENTORY_COST_BLEND_EFFECTIVE_DATE,
        wb_compat_lookup={},
        product_capital_lookup=products,
    )
    for nm_id in (301, 302, 303, 304):
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
    _assert(
        "wb_capital_nonpositive"
        in rows[304]["inventory_cost_evidence"]["reason_codes"],
        "positive physical inventory cannot resolve through zero capital",
    )
    total = aggregate_inventory_cost_evidence(rows, nm_ids=[301, 302, 303, 304])
    _assert(total["status"] == "unresolved", "TOTAL rejects positive uncovered rows")
    _assert(
        total["missing_nm_ids"] == [301, 302, 304],
        "zero inventory is not false missing and zero cost is not covered",
    )


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
        expected_qty = Decimal("1") if nm_id == 601 else Decimal("2")
        _equal(
            evaluator.resolve_sku(
                PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY, nm_id, "current"
            ),
            expected_v4[nm_id] / expected_qty,
            f"Proxy 4 unit margin SKU {nm_id} uses the same informational scope",
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
    _equal(
        evaluator.resolve_total(
            PROXY_V4_TOTAL_MARGIN_PER_UNIT_RUB_METRIC_KEY, "current"
        ),
        expected_total_v4 / Decimal("3"),
        "Proxy 4 TOTAL unit margin divides the same SKU profits by summed expected units",
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
