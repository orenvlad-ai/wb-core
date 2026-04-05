"""Минимальный smoke-check для HTTP-backed prices adapter."""

from dataclasses import asdict
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.prices_snapshot_block import HttpBackedPricesSnapshotSource
from packages.application.prices_snapshot_block import PricesSnapshotBlock
from packages.contracts.prices_snapshot_block import PricesSnapshotRequest


def _check_case(name: str, request: PricesSnapshotRequest) -> None:
    source = HttpBackedPricesSnapshotSource()
    block = PricesSnapshotBlock(source)
    result = asdict(block.execute(request))
    print(f"{name}: ok -> {result['result']['kind']}")


def main() -> None:
    _check_case(
        "normal",
        PricesSnapshotRequest(
            snapshot_type="prices_snapshot",
            snapshot_date="2026-04-05",
            nm_ids=[210183919, 210184534],
            scenario="normal",
        ),
    )
    _check_case(
        "empty",
        PricesSnapshotRequest(
            snapshot_type="prices_snapshot",
            snapshot_date="2026-04-05",
            nm_ids=[999000001],
            scenario="empty",
        ),
    )
    print("http-smoke-check passed")


if __name__ == "__main__":
    main()
