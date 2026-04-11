"""Минимальный smoke-check для HTTP-backed seller funnel adapter."""

from dataclasses import asdict
import json
import sys
from pathlib import Path
from urllib import request as urllib_request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.seller_funnel_snapshot_block import HttpBackedSellerFunnelSnapshotSource
from packages.application.seller_funnel_snapshot_block import SellerFunnelSnapshotBlock
from packages.contracts.seller_funnel_snapshot_block import SellerFunnelSnapshotRequest


LATEST_DAILY_URL = "https://api.selleros.pro/v1/sales-funnel/daily"


def _resolve_latest_date() -> str:
    with urllib_request.urlopen(LATEST_DAILY_URL) as response:
        payload = json.loads(response.read().decode("utf-8"))

    snapshot_date = payload.get("date")
    count = payload.get("count")
    if not isinstance(snapshot_date, str):
        raise SystemExit("latest sales funnel endpoint did not return valid date")
    if not isinstance(count, int) or count < 1:
        raise SystemExit(f"latest sales funnel endpoint returned unexpected count: {count!r}")
    return snapshot_date


def _check_case(name: str, request: SellerFunnelSnapshotRequest, expected_kind: str) -> None:
    source = HttpBackedSellerFunnelSnapshotSource()
    block = SellerFunnelSnapshotBlock(source)
    result = asdict(block.execute(request))
    kind = result["result"]["kind"]
    if kind != expected_kind:
        raise SystemExit(f"{name}: unexpected result kind: {kind}")
    if expected_kind == "success" and result["result"]["count"] < 1:
        raise SystemExit(f"{name}: unexpected result count: {result['result']['count']}")
    print(f"{name}: ok -> {kind}")


def main() -> None:
    snapshot_date = _resolve_latest_date()
    _check_case(
        "normal",
        SellerFunnelSnapshotRequest(
            snapshot_type="sales_funnel_daily",
            date=snapshot_date,
            scenario="normal",
        ),
        expected_kind="success",
    )
    _check_case(
        "not-found",
        SellerFunnelSnapshotRequest(
            snapshot_type="sales_funnel_daily",
            date="1900-01-01",
            scenario="not_found",
        ),
        expected_kind="not_found",
    )
    print("http-smoke-check passed")


if __name__ == "__main__":
    main()
