"""Минимальный локальный smoke-check для prices snapshot block."""

from dataclasses import asdict
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.prices_snapshot_block import ArtifactBackedPricesSnapshotSource
from packages.application.prices_snapshot_block import PricesSnapshotBlock
from packages.contracts.prices_snapshot_block import PricesSnapshotRequest


ARTIFACTS = ROOT / "artifacts" / "prices_snapshot_block"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _check_case(name: str, request: PricesSnapshotRequest, target_path: Path) -> None:
    expected_target = _load_json(target_path)
    source = ArtifactBackedPricesSnapshotSource(ARTIFACTS)
    block = PricesSnapshotBlock(source)
    actual_target = asdict(block.execute(request))
    if actual_target != expected_target:
        raise SystemExit(
            f"{name}: smoke-check failed\n"
            f"expected={json.dumps(expected_target, ensure_ascii=False, sort_keys=True)}\n"
            f"actual={json.dumps(actual_target, ensure_ascii=False, sort_keys=True)}"
        )
    print(f"{name}: ok")


def main() -> None:
    _check_case(
        "normal",
        PricesSnapshotRequest(
            snapshot_type="prices_snapshot",
            snapshot_date="2026-04-05",
            nm_ids=[210183919, 210184534],
            scenario="normal",
        ),
        ARTIFACTS / "target" / "normal__template__target__fixture.json",
    )
    _check_case(
        "empty",
        PricesSnapshotRequest(
            snapshot_type="prices_snapshot",
            snapshot_date="2026-04-05",
            nm_ids=[999000001],
            scenario="empty",
        ),
        ARTIFACTS / "target" / "empty__template__target__fixture.json",
    )
    print("smoke-check passed")


if __name__ == "__main__":
    main()
