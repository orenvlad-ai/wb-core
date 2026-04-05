"""Минимальный smoke-check для artifact-backed spp adapter."""

from dataclasses import asdict
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.spp_block import ArtifactBackedSppSource
from packages.application.spp_block import SppBlock
from packages.contracts.spp_block import SppRequest


ARTIFACTS = ROOT / "artifacts" / "spp_block"


def _check_case(name: str, request: SppRequest) -> None:
    source = ArtifactBackedSppSource(ARTIFACTS)
    block = SppBlock(source)
    result = asdict(block.execute(request))
    print(f"{name}: ok -> {result['result']['kind']}")


def main() -> None:
    _check_case(
        "normal",
        SppRequest(
            snapshot_type="spp",
            snapshot_date="2026-04-04",
            nm_ids=[210183919, 210184534],
            scenario="normal",
        ),
    )
    _check_case(
        "empty",
        SppRequest(
            snapshot_type="spp",
            snapshot_date="2026-04-04",
            nm_ids=[999000001],
            scenario="empty",
        ),
    )
    print("smoke-check passed")


if __name__ == "__main__":
    main()
