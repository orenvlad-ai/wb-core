"""Минимальный smoke-check для artifact-backed ads compact adapter."""

from dataclasses import asdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.ads_compact_block import ArtifactBackedAdsCompactSource
from packages.application.ads_compact_block import AdsCompactBlock
from packages.contracts.ads_compact_block import AdsCompactRequest

ARTIFACTS = ROOT / "artifacts" / "ads_compact_block"


def _check_case(name: str, request: AdsCompactRequest) -> None:
    source = ArtifactBackedAdsCompactSource(ARTIFACTS)
    block = AdsCompactBlock(source)
    result = asdict(block.execute(request))
    print(f"{name}: ok -> {result['result']['kind']}")


def main() -> None:
    _check_case(
        "normal",
        AdsCompactRequest(
            snapshot_type="ads_compact",
            snapshot_date="2026-04-05",
            nm_ids=[210183919, 210184534],
        ),
    )
    _check_case(
        "empty",
        AdsCompactRequest(
            snapshot_type="ads_compact",
            snapshot_date="2026-04-05",
            nm_ids=[999000001],
            scenario="empty",
        ),
    )
    print("smoke-check passed")


if __name__ == "__main__":
    main()
