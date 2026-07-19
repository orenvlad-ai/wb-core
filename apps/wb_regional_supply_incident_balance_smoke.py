from packages.application.stocks_block import build_elektrostal_stock_override
from packages.application.wb_regional_supply import allocate_start_distribution_boxes
from packages.contracts.stocks_block import StocksItem, StocksWarehouseRow


def main():
    item = StocksItem(1, 100, 60, 0, 0, 0, 0, 0)
    row = StocksWarehouseRow(1, 120762, "Электросталь", "ЦФО", 20, None, "classified", "smoke")
    assert build_elektrostal_stock_override(items=[item], warehouse_rows=[row], enabled=False)["excluded_elektrostal_stock"] == 0
    on = build_elektrostal_stock_override(items=[item], warehouse_rows=[row], enabled=True)
    assert on["excluded_elektrostal_stock"] == 20 and on["effective_central_stock"] == 40
    assert build_elektrostal_stock_override(items=[item], warehouse_rows=[], enabled=True)["excluded_elektrostal_stock"] == 0
    allocation = allocate_start_distribution_boxes(total_qty=2990, included_keys=("central_north", "central_east", "central_south"), order_batch_qty=10)
    assert sorted(allocation.values()) == [990, 1000, 1000]
    print("wb_regional_supply_incident_balance_smoke: ok")


if __name__ == "__main__":
    main()
