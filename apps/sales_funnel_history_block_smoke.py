"""Минимальный smoke-check для artifact-backed sales funnel history adapter."""

from dataclasses import asdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.sales_funnel_history_block import ArtifactBackedSalesFunnelHistorySource
from packages.application.sales_funnel_history_block import SalesFunnelHistoryBlock
from packages.contracts.sales_funnel_history_block import SalesFunnelHistoryRequest

ARTIFACTS = ROOT / "artifacts" / "sales_funnel_history_block"


def _check_case(name: str, request: SalesFunnelHistoryRequest) -> None:
    source = ArtifactBackedSalesFunnelHistorySource(ARTIFACTS)
    block = SalesFunnelHistoryBlock(source)
    result = asdict(block.execute(request))
    print(f"{name}: ok -> {result['result']['kind']}")


def main() -> None:
    _check_case(
        "normal",
        SalesFunnelHistoryRequest(
            snapshot_type="sales_funnel_history",
            date_from="2026-03-30",
            date_to="2026-04-05",
            nm_ids=[210183919, 210184534],
        ),
    )
    _check_case(
        "empty",
        SalesFunnelHistoryRequest(
            snapshot_type="sales_funnel_history",
            date_from="2026-03-30",
            date_to="2026-04-05",
            nm_ids=[999000001],
            scenario="empty",
        ),
    )
    print("smoke-check passed")


if __name__ == "__main__":
    main()
