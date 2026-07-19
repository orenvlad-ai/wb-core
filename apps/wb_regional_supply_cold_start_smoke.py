"""Targeted regression checks for Central cold-start and Elektrostal override."""

from packages.application.stocks_block import build_elektrostal_stock_override
from packages.application.wb_regional_supply import _rebalance_central_recommendations
from packages.contracts.stocks_block import StocksItem, StocksWarehouseRow
from packages.contracts.wb_supply_planning_zones import (
    PLANNING_ZONE_CENTRAL_EAST,
    PLANNING_ZONE_CENTRAL_NORTH,
    PLANNING_ZONE_CENTRAL_SOUTH,
)


def _item() -> StocksItem:
    return StocksItem(1, 100, 60, 0, 0, 0, 0, 0, stock_ru_central_north=20,
                      stock_ru_central_east=20, stock_ru_central_south=20)


def main() -> None:
    rows = [
        StocksWarehouseRow(1, 120762, "Электросталь", "ЦФО", 30, PLANNING_ZONE_CENTRAL_EAST, "classified", "wb"),
        StocksWarehouseRow(1, 1001, "Тверь", "ЦФО", 20, PLANNING_ZONE_CENTRAL_NORTH, "classified", "wb"),
    ]
    off = build_elektrostal_stock_override(items=[_item()], warehouse_rows=rows, enabled=False)
    on = build_elektrostal_stock_override(items=[_item()], warehouse_rows=rows, enabled=True)
    assert off["effective_central_stock"] == 60
    assert on["excluded_elektrostal_stock"] == 30
    assert on["effective_central_stock"] == 30
    assert build_elektrostal_stock_override(items=[StocksItem(1, 70, 30, 0, 0, 0, 0, 0)], warehouse_rows=rows, enabled=True)["effective_central_stock"] == 0

    raw = {k: 9.0 for k in (PLANNING_ZONE_CENTRAL_NORTH, PLANNING_ZONE_CENTRAL_EAST, PLANNING_ZONE_CENTRAL_SOUTH)}
    daily = {k: 30.0 for k in raw}
    full = dict(raw)
    _rebalance_central_recommendations(full_recommendation_by_key=full, raw_recommendation_by_key=raw,
                                       district_daily_demand_by_key=daily, included_district_keys=list(raw), order_batch_qty=10)
    assert full[PLANNING_ZONE_CENTRAL_NORTH] == 10
    assert sum(full.values()) == 30
    two = {PLANNING_ZONE_CENTRAL_EAST: 9.0, PLANNING_ZONE_CENTRAL_SOUTH: 9.0}
    _rebalance_central_recommendations(full_recommendation_by_key=two, raw_recommendation_by_key=two,
                                       district_daily_demand_by_key={k: 30.0 for k in two}, included_district_keys=list(two), order_batch_qty=10)
    assert sum(two.values()) == 20 and all(value > 0 for value in two.values())
    one = {PLANNING_ZONE_CENTRAL_SOUTH: 9.0}
    _rebalance_central_recommendations(full_recommendation_by_key=one, raw_recommendation_by_key=one,
                                       district_daily_demand_by_key=one, included_district_keys=list(one), order_batch_qty=10)
    assert one[PLANNING_ZONE_CENTRAL_SOUTH] == 10
    print("wb_regional_supply_cold_start_smoke: ok")


if __name__ == "__main__":
    main()
