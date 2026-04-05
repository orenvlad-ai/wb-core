"""Минимальный smoke-check для artifact-backed ads bids adapter."""

from dataclasses import asdict
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.ads_bids_block import ArtifactBackedAdsBidsSource
from packages.application.ads_bids_block import AdsBidsBlock
from packages.contracts.ads_bids_block import AdsBidsRequest


ARTIFACTS = ROOT / "artifacts" / "ads_bids_block"


def _check_case(name: str, request: AdsBidsRequest) -> None:
    source = ArtifactBackedAdsBidsSource(ARTIFACTS)
    block = AdsBidsBlock(source)
    result = asdict(block.execute(request))
    print(f"{name}: ok -> {result['result']['kind']}")


def main() -> None:
    _check_case(
        "normal",
        AdsBidsRequest(
            snapshot_type="ads_bids",
            snapshot_date="2026-04-05",
            nm_ids=[210183919, 210184534],
            scenario="normal",
        ),
    )
    _check_case(
        "empty",
        AdsBidsRequest(
            snapshot_type="ads_bids",
            snapshot_date="2026-04-05",
            nm_ids=[999000001],
            scenario="empty",
        ),
    )
    print("smoke-check passed")


if __name__ == "__main__":
    main()
