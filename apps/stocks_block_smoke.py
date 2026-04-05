"""Минимальный smoke-check для artifact-backed stocks adapter."""

from dataclasses import asdict
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.stocks_block import ArtifactBackedStocksSource
from packages.application.stocks_block import StocksBlock
from packages.contracts.stocks_block import StocksRequest


ARTIFACTS = ROOT / "artifacts" / "stocks_block"


def _check_case(name: str, request: StocksRequest) -> None:
    source = ArtifactBackedStocksSource(ARTIFACTS)
    block = StocksBlock(source)
    result = asdict(block.execute(request))
    print(f"{name}: ok -> {result['result']['kind']}")


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
