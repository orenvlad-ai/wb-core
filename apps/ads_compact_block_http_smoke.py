"""Минимальный smoke-check для HTTP-backed ads compact adapter."""

from dataclasses import asdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.ads_compact_block import HttpBackedAdsCompactSource
from packages.application.ads_compact_block import AdsCompactBlock
from packages.contracts.ads_compact_block import AdsCompactRequest


def main() -> None:
    source = HttpBackedAdsCompactSource()
    block = AdsCompactBlock(source)
    result = asdict(
        block.execute(
            AdsCompactRequest(
                snapshot_type="ads_compact",
                snapshot_date="2026-04-05",
                nm_ids=[210183919, 210184534],
            )
        )
    )
    if result["result"]["kind"] != "success":
        raise SystemExit(f"unexpected result kind: {result['result']['kind']}")
    if result["result"]["count"] < 1:
        raise SystemExit(f"unexpected result count: {result['result']['count']}")
    print(f"normal: ok -> {result['result']['kind']}")
    print(f"normal: count -> {result['result']['count']}")
    print("http-smoke-check passed")


if __name__ == "__main__":
    main()
