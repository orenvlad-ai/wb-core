"""Минимальный smoke-check для HTTP-backed sales funnel history adapter."""

from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.sales_funnel_history_block import HttpBackedSalesFunnelHistorySource
from packages.application.sales_funnel_history_block import SalesFunnelHistoryBlock
from packages.contracts.sales_funnel_history_block import SalesFunnelHistoryRequest


def _resolve_recent_window() -> tuple[str, str]:
    date_to = date.today() - timedelta(days=1)
    date_from = date_to - timedelta(days=1)
    return date_from.isoformat(), date_to.isoformat()


def main() -> None:
    date_from, date_to = _resolve_recent_window()
    source = HttpBackedSalesFunnelHistorySource()
    block = SalesFunnelHistoryBlock(source)
    result = asdict(
        block.execute(
            SalesFunnelHistoryRequest(
                snapshot_type="sales_funnel_history",
                date_from=date_from,
                date_to=date_to,
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
