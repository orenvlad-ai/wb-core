"""Минимальный smoke-check для artifact-backed table projection bundle."""

from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.table_projection_bundle_block import ArtifactBackedTableProjectionBundleSource
from packages.application.table_projection_bundle_block import TableProjectionBundleBlock
from packages.contracts.table_projection_bundle_block import TableProjectionBundleRequest

ARTIFACTS = ROOT / "artifacts" / "table_projection_bundle_block"


def _expected_fixture(name: str) -> dict:
    path = ARTIFACTS / "target" / f"{name}__template__target__fixture.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _check_case(name: str, request: TableProjectionBundleRequest, expected_kind: str, expected_count: int) -> None:
    source = ArtifactBackedTableProjectionBundleSource(ARTIFACTS)
    block = TableProjectionBundleBlock(source)
    result = asdict(block.execute(request))
    if result != _expected_fixture(name):
        raise AssertionError(f"{name}: result differs from target fixture")
    kind = result["result"]["kind"]
    count = result["result"]["count"]
    if kind != expected_kind:
        raise AssertionError(f"{name}: expected kind={expected_kind}, got {kind}")
    if count != expected_count:
        raise AssertionError(f"{name}: expected count={expected_count}, got {count}")
    print(f"{name}: ok -> {kind}")
    print(f"{name}: count -> {count}")


def main() -> None:
    _check_case(
        "normal",
        TableProjectionBundleRequest(bundle_type="table_projection_bundle"),
        expected_kind="success",
        expected_count=3,
    )
    _check_case(
        "minimal",
        TableProjectionBundleRequest(bundle_type="table_projection_bundle", scenario="minimal"),
        expected_kind="empty",
        expected_count=0,
    )
    print("smoke-check passed")


if __name__ == "__main__":
    main()
