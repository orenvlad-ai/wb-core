"""Минимальный smoke-check для HTTP-backed seller funnel adapter."""

from dataclasses import asdict
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.seller_funnel_snapshot_block import HttpBackedSellerFunnelSnapshotSource
from packages.application.seller_funnel_snapshot_block import SellerFunnelSnapshotBlock
from packages.contracts.seller_funnel_snapshot_block import SellerFunnelSnapshotRequest


def _check_case(name: str, request: SellerFunnelSnapshotRequest) -> None:
    source = HttpBackedSellerFunnelSnapshotSource()
    block = SellerFunnelSnapshotBlock(source)
    result = asdict(block.execute(request))
    print(f"{name}: ok -> {result['result']['kind']}")


def main() -> None:
    _check_case(
        "normal",
        SellerFunnelSnapshotRequest(
            snapshot_type="sales_funnel_daily",
            date="2026-04-04",
            scenario="normal",
        ),
    )
    _check_case(
        "not-found",
        SellerFunnelSnapshotRequest(
            snapshot_type="sales_funnel_daily",
            date="1900-01-01",
            scenario="not_found",
        ),
    )
    print("http-smoke-check passed")


if __name__ == "__main__":
    main()
