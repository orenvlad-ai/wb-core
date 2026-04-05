"""Минимальный smoke-check для HTTP-backed sales funnel history adapter."""

from dataclasses import asdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.sales_funnel_history_block import HttpBackedSalesFunnelHistorySource
from packages.application.sales_funnel_history_block import SalesFunnelHistoryBlock
from packages.contracts.sales_funnel_history_block import SalesFunnelHistoryRequest


def main() -> None:
    source = HttpBackedSalesFunnelHistorySource()
    block = SalesFunnelHistoryBlock(source)
    result = asdict(
        block.execute(
            SalesFunnelHistoryRequest(
                snapshot_type="sales_funnel_history",
                date_from="2026-03-30",
                date_to="2026-04-05",
                nm_ids=[210183919, 210184534],
            )
        )
    )
    if result["result"]["kind"] != "success":
        raise SystemExit(f"unexpected result kind: {result['result']['kind']}")
    if result["result"]["count"] < 1:
        raise SystemExit(f"unexpected result count: {result['result']['count']}")
    print(f"normal: ok -> {result['result']['kind']}")
    print(f"normal: count -> {result['result']['count']}")
    print("http-smoke-check passed")


if __name__ == "__main__":
    main()
