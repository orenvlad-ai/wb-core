"""Минимальный smoke-check для artifact-backed promo by price adapter."""

from dataclasses import asdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.promo_by_price_block import ArtifactBackedPromoByPriceSource
from packages.application.promo_by_price_block import PromoByPriceBlock
from packages.contracts.promo_by_price_block import PromoByPriceRequest

ARTIFACTS = ROOT / "artifacts" / "promo_by_price_block"


def _check_case(name: str, request: PromoByPriceRequest, expected_kind: str, expected_count: int) -> None:
    source = ArtifactBackedPromoByPriceSource(ARTIFACTS)
    block = PromoByPriceBlock(source)
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
        PromoByPriceRequest(
            snapshot_type="promo_by_price",
            date_from="2026-04-01",
            date_to="2026-04-04",
            nm_ids=[210183919, 210184534],
        ),
        expected_kind="success",
        expected_count=8,
    )
    _check_case(
        "empty",
        PromoByPriceRequest(
            snapshot_type="promo_by_price",
            date_from="2026-04-01",
            date_to="2026-04-04",
            nm_ids=[999000001],
            scenario="empty",
        ),
        expected_kind="empty",
        expected_count=0,
    )
    print("smoke-check passed")


if __name__ == "__main__":
    main()
