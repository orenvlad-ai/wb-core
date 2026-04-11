"""Минимальный smoke-check для composed-source table projection bundle."""

from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.table_projection_bundle_block import ComposedReferenceTableProjectionBundleSource
from packages.application.table_projection_bundle_block import TableProjectionBundleBlock
from packages.contracts.table_projection_bundle_block import TableProjectionBundleRequest

ARTIFACTS = ROOT / "artifacts" / "table_projection_bundle_block"


def _expected_fixture(name: str) -> dict:
    path = ARTIFACTS / "target" / f"{name}__template__target__fixture.json"
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    source = ComposedReferenceTableProjectionBundleSource(ROOT, ARTIFACTS)
    block = TableProjectionBundleBlock(source)

    normal = asdict(block.execute(TableProjectionBundleRequest(bundle_type="table_projection_bundle")))
    if normal != _expected_fixture("normal"):
        raise AssertionError("normal result differs from target fixture")
    if normal["result"]["kind"] != "success":
        raise AssertionError(f"expected success, got {normal['result']['kind']}")
    if normal["result"]["count"] != 3:
        raise AssertionError(f"expected 3 rows, got {normal['result']['count']}")

    items = {item["nm_id"]: item for item in normal["result"]["items"]}
    if items[210183919]["official_api"]["prices"]["price_seller_discounted"] != 999.0:
        raise AssertionError("expected price_seller_discounted=999.0 for 210183919")
    if items[210184534]["history_summary"]["metric_count"] != 10:
        raise AssertionError("expected 10 history metrics for 210184534")
    if items[210185771]["web_source"]["search_analytics"]["kind"] != "missing":
        raise AssertionError("expected missing search_analytics for 210185771")

    minimal = asdict(
        block.execute(TableProjectionBundleRequest(bundle_type="table_projection_bundle", scenario="minimal"))
    )
    if minimal != _expected_fixture("minimal"):
        raise AssertionError("minimal result differs from target fixture")
    if minimal["result"]["kind"] != "empty":
        raise AssertionError(f"expected empty, got {minimal['result']['kind']}")
    if minimal["result"]["count"] != 0:
        raise AssertionError(f"expected 0 rows, got {minimal['result']['count']}")

    print("normal: ok -> success")
    print("normal: count -> 3")
    print("bundle-composition-smoke passed")


if __name__ == "__main__":
    main()
