"""Минимальный smoke-check для HTTP-backed spp adapter."""

from dataclasses import asdict
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.spp_block import HttpBackedSppSource
from packages.application.spp_block import SppBlock
from packages.contracts.spp_block import SppRequest


def _check_case(name: str, request: SppRequest) -> None:
    source = HttpBackedSppSource()
    block = SppBlock(source)
    result = asdict(block.execute(request))
    if result["result"]["kind"] == "success" and result["result"]["count"] < 1:
        raise SystemExit(f"unexpected result count: {result['result']['count']}")
    print(f"{name}: ok -> {result['result']['kind']}")
    if result["result"]["kind"] == "success":
        print(f"{name}: count -> {result['result']['count']}")


def main() -> None:
    _check_case(
        "normal",
        SppRequest(
            snapshot_type="spp",
            snapshot_date="2026-04-04",
            nm_ids=[210183919, 210184534],
            scenario="normal",
        ),
    )
    print("http-smoke-check passed")


if __name__ == "__main__":
    main()
