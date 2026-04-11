"""Минимальный smoke-check для fixture-backed rule-source promo path."""

from dataclasses import asdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.promo_by_price_block import RuleBackedPromoByPriceSource
from packages.application.promo_by_price_block import PromoByPriceBlock
from packages.contracts.promo_by_price_block import PromoByPriceRequest

ARTIFACTS = ROOT / "artifacts" / "promo_by_price_block"


def main() -> None:
    source = RuleBackedPromoByPriceSource(ARTIFACTS)
    block = PromoByPriceBlock(source)
    result = asdict(
        block.execute(
            PromoByPriceRequest(
                snapshot_type="promo_by_price",
                date_from="2026-04-01",
                date_to="2026-04-04",
                nm_ids=[210183919, 210184534],
            )
        )
    )

    if result["result"]["kind"] != "success":
        raise AssertionError(f"expected success, got {result['result']['kind']}")
    if result["result"]["count"] != 8:
        raise AssertionError(f"expected 8 rows, got {result['result']['count']}")

    items = {
        (item["date"], item["nm_id"]): item for item in result["result"]["items"]
    }
    probe = items[("2026-04-03", 210184534)]
    if probe["promo_count_by_price"] != 2.0:
        raise AssertionError("expected promo_count_by_price=2.0 for 2026-04-03/210184534")
    if probe["promo_entry_price_best"] != 820.0:
        raise AssertionError("expected promo_entry_price_best=820.0 for 2026-04-03/210184534")
    if probe["promo_participation"] != 1.0:
        raise AssertionError("expected promo_participation=1.0 for 2026-04-03/210184534")

    print("normal: ok -> success")
    print("normal: count -> 8")
    print("rule-smoke-check passed")


if __name__ == "__main__":
    main()
