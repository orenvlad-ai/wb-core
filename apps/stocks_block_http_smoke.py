"""Минимальный smoke-check для HTTP-backed stocks adapter."""

from dataclasses import asdict
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.stocks_block import HttpBackedStocksSource
from packages.application.stocks_block import StocksBlock
from packages.contracts.stocks_block import StocksRequest


def main() -> None:
    source = HttpBackedStocksSource()
    block = StocksBlock(source)
    result = asdict(
        block.execute(
            StocksRequest(
                snapshot_type="stocks",
                snapshot_date="2026-04-05",
                nm_ids=[210183919, 210184534],
                scenario="normal",
            )
        )
    )
    if result["result"]["kind"] != "success":
        raise SystemExit(f"unexpected result kind: {result['result']['kind']}")
    if result["result"]["count"] != 2:
        raise SystemExit(f"unexpected result count: {result['result']['count']}")
    print(f"normal: ok -> {result['result']['kind']}")
    print(f"normal: count -> {result['result']['count']}")
    print("http-smoke-check passed")


if __name__ == "__main__":
    main()
