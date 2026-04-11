"""Минимальный smoke-check для artifact-backed sku display bundle adapter."""

from dataclasses import asdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.sku_display_bundle_block import ArtifactBackedSkuDisplayBundleSource
from packages.application.sku_display_bundle_block import SkuDisplayBundleBlock
from packages.contracts.sku_display_bundle_block import SkuDisplayBundleRequest

ARTIFACTS = ROOT / "artifacts" / "sku_display_bundle_block"


def _check_case(name: str, request: SkuDisplayBundleRequest, expected_kind: str, expected_count: int) -> None:
    source = ArtifactBackedSkuDisplayBundleSource(ARTIFACTS)
    block = SkuDisplayBundleBlock(source)
    result = asdict(block.execute(request))
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
        SkuDisplayBundleRequest(bundle_type="sku_display_bundle"),
        expected_kind="success",
        expected_count=3,
    )
    _check_case(
        "empty",
        SkuDisplayBundleRequest(bundle_type="sku_display_bundle", scenario="empty"),
        expected_kind="empty",
        expected_count=0,
    )
    print("smoke-check passed")


if __name__ == "__main__":
    main()
