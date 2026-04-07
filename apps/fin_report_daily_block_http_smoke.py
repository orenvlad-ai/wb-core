"""Минимальный smoke-check для HTTP-backed fin report daily adapter."""

from dataclasses import asdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.fin_report_daily_block import HttpBackedFinReportDailySource
from packages.application.fin_report_daily_block import FinReportDailyBlock
from packages.contracts.fin_report_daily_block import FinReportDailyRequest


def main() -> None:
    source = HttpBackedFinReportDailySource()
    block = FinReportDailyBlock(source)
    result = asdict(
        block.execute(
            FinReportDailyRequest(
                snapshot_type="fin_report_daily",
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
    print(f"storage_total: ok -> {result['result']['storage_total']['fin_storage_fee_total']}")
    print("http-smoke-check passed")


if __name__ == "__main__":
    main()
