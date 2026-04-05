"""Минимальный smoke-check для HTTP-backed adapter."""

from dataclasses import asdict
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.web_source_snapshot_block import HttpBackedWebSourceSnapshotSource
from packages.application.web_source_snapshot_block import WebSourceSnapshotBlock
from packages.contracts.web_source_snapshot_block import WebSourceSnapshotRequest


def _check_case(name: str, request: WebSourceSnapshotRequest) -> None:
    source = HttpBackedWebSourceSnapshotSource()
    block = WebSourceSnapshotBlock(source)
    result = asdict(block.execute(request))
    print(f"{name}: ok -> {result['result']['kind']}")


def main() -> None:
    _check_case(
        "normal",
        WebSourceSnapshotRequest(
            snapshot_type="search_analytics_snapshot",
            date_from="2026-04-04",
            date_to="2026-04-04",
            scenario="normal",
        ),
    )
    _check_case(
        "not-found",
        WebSourceSnapshotRequest(
            snapshot_type="search_analytics_snapshot",
            date_from="1900-01-01",
            date_to="1900-01-01",
            scenario="not_found",
        ),
    )
    print("http-smoke-check passed")


if __name__ == "__main__":
    main()
