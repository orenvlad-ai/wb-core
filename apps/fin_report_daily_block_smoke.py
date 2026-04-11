"""Минимальный smoke-check для artifact-backed fin report daily adapter."""

from dataclasses import asdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.fin_report_daily_block import ArtifactBackedFinReportDailySource
from packages.application.fin_report_daily_block import FinReportDailyBlock
from packages.contracts.fin_report_daily_block import FinReportDailyRequest

ARTIFACTS = ROOT / "artifacts" / "fin_report_daily_block"


def _check_case(name: str, request: FinReportDailyRequest) -> None:
    source = ArtifactBackedFinReportDailySource(ARTIFACTS)
    block = FinReportDailyBlock(source)
    result = asdict(block.execute(request))
    print(f"{name}: ok -> {result['result']['kind']}")


def main() -> None:
    _check_case(
        "normal",
        FinReportDailyRequest(
            snapshot_type="fin_report_daily",
            snapshot_date="2026-04-05",
            nm_ids=[210183919, 210184534],
        ),
    )
    _check_case(
        "storage_total",
        FinReportDailyRequest(
            snapshot_type="fin_report_daily",
            snapshot_date="2026-04-05",
            nm_ids=[210183919, 210184534],
            scenario="storage_total",
        ),
    )
    print("smoke-check passed")


if __name__ == "__main__":
    main()
