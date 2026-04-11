"""Минимальный smoke-check для safe CONFIG-fixture sku display bundle path."""

from dataclasses import asdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.sku_display_bundle_block import ConfigFixtureBackedSkuDisplayBundleSource
from packages.application.sku_display_bundle_block import SkuDisplayBundleBlock
from packages.contracts.sku_display_bundle_block import SkuDisplayBundleRequest

ARTIFACTS = ROOT / "artifacts" / "sku_display_bundle_block"


def main() -> None:
    source = ConfigFixtureBackedSkuDisplayBundleSource(ARTIFACTS)
    block = SkuDisplayBundleBlock(source)

    normal = asdict(block.execute(SkuDisplayBundleRequest(bundle_type="sku_display_bundle")))
    if normal["result"]["kind"] != "success":
        raise AssertionError(f"expected success, got {normal['result']['kind']}")
    if normal["result"]["count"] != 3:
        raise AssertionError(f"expected 3 rows, got {normal['result']['count']}")

    items = {item["nm_id"]: item for item in normal["result"]["items"]}
    if items[210183919]["display_name"] != "Худи basic":
        raise AssertionError("expected display_name=Худи basic for 210183919")
    if items[210184534]["display_order"] != 2:
        raise AssertionError("expected display_order=2 for 210184534")
    if items[210185771]["enabled"] is not False:
        raise AssertionError("expected enabled=false for inactive SKU 210185771")

    empty = asdict(
        block.execute(SkuDisplayBundleRequest(bundle_type="sku_display_bundle", scenario="empty"))
    )
    if empty["result"]["kind"] != "empty":
        raise AssertionError(f"expected empty, got {empty['result']['kind']}")
    if empty["result"]["count"] != 0:
        raise AssertionError(f"expected 0 rows, got {empty['result']['count']}")

    print("normal: ok -> success")
    print("normal: count -> 3")
    print("config-smoke-check passed")


if __name__ == "__main__":
    main()
