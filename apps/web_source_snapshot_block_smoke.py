"""Минимальный локальный smoke-check для web-source snapshot block."""

from dataclasses import asdict
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.web_source_snapshot_block import ArtifactBackedWebSourceSnapshotSource
from packages.application.web_source_snapshot_block import WebSourceSnapshotBlock
from packages.contracts.web_source_snapshot_block import WebSourceSnapshotRequest


ARTIFACTS = ROOT / "artifacts" / "web_source_snapshot_block"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _check_case(name: str, target_path: Path) -> None:
    expected_target = _load_json(target_path)
    source = ArtifactBackedWebSourceSnapshotSource(ARTIFACTS)
    block = WebSourceSnapshotBlock(source)
    request = WebSourceSnapshotRequest(
        snapshot_type="search_analytics_snapshot",
        date_from="2026-04-04",
        date_to="2026-04-04",
        scenario=name.replace("-", "_"),
    )
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
        ARTIFACTS / "target" / "normal__template__target__fixture.json",
    )
    _check_case(
        "not-found",
        ARTIFACTS / "target" / "not-found__template__target__fixture.json",
    )
    print("smoke-check passed")


if __name__ == "__main__":
    main()
