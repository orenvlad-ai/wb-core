"""Минимальный smoke-check для HTTP-backed ads bids adapter."""

from dataclasses import asdict
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.ads_bids_block import HttpBackedAdsBidsSource
from packages.application.ads_bids_block import AdsBidsBlock
from packages.contracts.ads_bids_block import AdsBidsRequest


def main() -> None:
    source = HttpBackedAdsBidsSource()
    block = AdsBidsBlock(source)
    result = asdict(
        block.execute(
            AdsBidsRequest(
                snapshot_type="ads_bids",
                snapshot_date="2026-04-05",
                nm_ids=[210183919, 210184534],
                scenario="normal",
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
