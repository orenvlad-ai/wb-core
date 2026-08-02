"""Минимальный smoke-check для artifact-backed stocks adapter."""

from dataclasses import asdict
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.stocks_block import ArtifactBackedStocksSource
from packages.application.stocks_block import (
    StocksBlock,
    build_wb_warehouse_exclusion,
    parse_excluded_wb_warehouse_ids,
)
from packages.contracts.stocks_block import (
    StocksItem,
    StocksRequest,
    StocksWarehouseRow,
)


ARTIFACTS = ROOT / "artifacts" / "stocks_block"


def _check_case(name: str, request: StocksRequest) -> None:
    source = ArtifactBackedStocksSource(ARTIFACTS)
    block = StocksBlock(source)
    result = asdict(block.execute(request))
    print(f"{name}: ok -> {result['result']['kind']}")


def _check_shared_warehouse_exclusion() -> None:
    items = [
        StocksItem(
            nm_id=1,
            stock_total=15,
            stock_ru_central=15,
            stock_ru_northwest=0,
            stock_ru_volga=0,
            stock_ru_ural=0,
            stock_ru_south_caucasus=0,
            stock_ru_far_siberia=0,
        )
    ]
    rows = [
        StocksWarehouseRow(
            nm_id=1,
            warehouse_id=120762,
            warehouse_name="Одинаковое имя",
            region_name="Центральный",
            quantity=10,
            planning_zone_key="central_north",
            classification_status="mapped",
            classification_source="registry",
        ),
        StocksWarehouseRow(
            nm_id=1,
            warehouse_id=999,
            warehouse_name="Одинаковое имя",
            region_name="Центральный",
            quantity=5,
            planning_zone_key="central_east",
            classification_status="mapped",
            classification_source="registry",
        ),
        StocksWarehouseRow(
            nm_id=1,
            warehouse_id=0,
            warehouse_name="Остальные",
            region_name="",
            quantity=0,
            in_way_to_client=4,
            in_way_from_client=2,
            planning_zone_key=None,
            classification_status="unmapped",
            classification_source="official",
        ),
    ]
    no_exclusion = build_wb_warehouse_exclusion(
        items=items,
        warehouse_rows=rows,
        excluded_warehouse_ids=(),
        snapshot_date="2026-07-23",
        fetched_at="2026-07-23T12:00:00Z",
        pagination_complete=False,
        raw_rows_digest="sha256:fixture",
    )
    assert no_exclusion["actual_stock_total_mp"] == 15
    assert no_exclusion["effective_stock_total_mp"] == 15
    selected = build_wb_warehouse_exclusion(
        items=items,
        warehouse_rows=rows,
        excluded_warehouse_ids=(0, 120762),
        snapshot_date="2026-07-23",
        fetched_at="2026-07-23T12:00:00Z",
        pagination_complete=True,
        raw_rows_digest="sha256:fixture",
    )
    assert selected["excluded_stock_total_mp"] == 10
    assert selected["effective_stock_total_mp"] == 5
    assert selected["reconciliation_difference"] == 0
    assert selected["by_nm_id"]["1"]["effective_stock_ru_central"] == 5
    assert selected["by_nm_id"]["1"]["excluded_stock_ru_central"] == 10
    assert {item["warehouse_id"] for item in selected["options"]} == {
        0,
        999,
        120762,
    }
    service_group = next(
        item for item in selected["options"] if item["warehouse_id"] == 0
    )
    assert service_group["warehouse_name"] == "Остальные — служебная группа WB"
    assert service_group["destination_eligible"] is False
    assert service_group["service_group"] is True
    assert "не привязал к конкретному складу" in service_group["message"]
    assert len(
        [item for item in selected["options"] if item["warehouse_name"] == "Одинаковое имя"]
    ) == 2
    assert [item["warehouse_id"] for item in selected["options"]] == [120762, 999, 0]
    missing = build_wb_warehouse_exclusion(
        items=items,
        warehouse_rows=rows,
        excluded_warehouse_ids=(777,),
        snapshot_date="2026-07-23",
        fetched_at="2026-07-23T12:00:00Z",
        pagination_complete=True,
        raw_rows_digest="sha256:fixture",
    )
    assert missing["temporarily_missing_selected_ids"] == [777]
    assert next(
        item for item in missing["options"] if item["warehouse_id"] == 777
    )["temporarily_missing"] is True
    assert missing["options"][-1]["warehouse_id"] == 777
    assert parse_excluded_wb_warehouse_ids(
        {"exclude_elektrostal_stock": True}
    ) == (120762,)
    assert parse_excluded_wb_warehouse_ids(
        {"excluded_wb_warehouse_ids": [0, "999"]}
    ) == (0, 999)
    try:
        parse_excluded_wb_warehouse_ids(
            {"excluded_wb_warehouse_ids": [999, "999"]}
        )
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate warehouseId must fail")
    try:
        build_wb_warehouse_exclusion(
            items=items,
            warehouse_rows=rows,
            excluded_warehouse_ids=(999,),
            snapshot_date="2026-07-23",
            fetched_at="2026-07-23T12:00:00Z",
            pagination_complete=False,
            raw_rows_digest="sha256:fixture",
        )
    except ValueError as exc:
        assert "неполный" in str(exc)
    else:
        raise AssertionError("incomplete selected snapshot must fail")
    print("warehouse exclusion: ok")


def main() -> None:
    _check_case(
        "normal",
        StocksRequest(
            snapshot_type="stocks",
            snapshot_date="2026-04-05",
            nm_ids=[210183919, 210184534],
            scenario="normal",
        ),
    )
    _check_shared_warehouse_exclusion()
    _check_case(
        "partial",
        StocksRequest(
            snapshot_type="stocks",
            snapshot_date="2026-04-05",
            nm_ids=[210183919, 210184534],
            scenario="partial",
        ),
    )
    print("smoke-check passed")


if __name__ == "__main__":
    main()
