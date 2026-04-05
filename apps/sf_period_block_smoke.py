"""Минимальный smoke-check для artifact-backed sf_period adapter."""

from dataclasses import asdict
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.sf_period_block import ArtifactBackedSfPeriodSource
from packages.application.sf_period_block import SfPeriodBlock
from packages.contracts.sf_period_block import SfPeriodRequest


ARTIFACTS = ROOT / "artifacts" / "sf_period_block"


def main() -> None:
    source = ArtifactBackedSfPeriodSource(ARTIFACTS)
    block = SfPeriodBlock(source)
    result = asdict(
        block.execute(
            SfPeriodRequest(
                snapshot_type="sf_period",
                snapshot_date="2026-04-05",
                nm_ids=[210183919, 210184534],
            )
        )
    )
    if result["result"]["kind"] != "success":
        raise SystemExit(f"unexpected result kind: {result['result']['kind']}")
    if result["result"]["count"] != 2:
        raise SystemExit(f"unexpected result count: {result['result']['count']}")
    print("normal: ok")
    print("smoke-check passed")


if __name__ == "__main__":
    main()
