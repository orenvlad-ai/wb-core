"""Минимальный smoke-check для HTTP-backed adapter."""

from dataclasses import asdict
import json
import os
import ssl
import sys
from pathlib import Path
from urllib import error, request as urllib_request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.web_source_snapshot_block import HttpBackedWebSourceSnapshotSource
from packages.application.web_source_snapshot_block import WebSourceSnapshotBlock
from packages.contracts.web_source_snapshot_block import WebSourceSnapshotRequest


LATEST_SNAPSHOT_URL = "https://api.selleros.pro/v1/search-analytics/snapshot"


def _resolve_latest_window() -> tuple[str, str]:
    with _open_url(LATEST_SNAPSHOT_URL) as response:
        payload = json.loads(response.read().decode("utf-8"))

    date_from = payload.get("date_from")
    date_to = payload.get("date_to")
    count = payload.get("count")
    if not isinstance(date_from, str) or not isinstance(date_to, str):
        raise SystemExit("latest snapshot endpoint did not return valid date window")
    if not isinstance(count, int) or count < 1:
        raise SystemExit(f"latest snapshot endpoint returned unexpected count: {count!r}")
    return date_from, date_to


def _check_case(name: str, request: WebSourceSnapshotRequest, expected_kind: str) -> None:
    source = HttpBackedWebSourceSnapshotSource()
    block = WebSourceSnapshotBlock(source)
    result = asdict(block.execute(request))
    kind = result["result"]["kind"]
    if kind != expected_kind:
        raise SystemExit(f"{name}: unexpected result kind: {kind}")
    if expected_kind == "success" and result["result"]["count"] < 1:
        raise SystemExit(f"{name}: unexpected result count: {result['result']['count']}")
    print(f"{name}: ok -> {kind}")


def main() -> None:
    date_from, date_to = _resolve_latest_window()
    _check_case(
        "normal",
        WebSourceSnapshotRequest(
            snapshot_type="search_analytics_snapshot",
            date_from=date_from,
            date_to=date_to,
            scenario="normal",
        ),
        expected_kind="success",
    )
    _check_case(
        "not-found",
        WebSourceSnapshotRequest(
            snapshot_type="search_analytics_snapshot",
            date_from="1900-01-01",
            date_to="1900-01-01",
            scenario="not_found",
        ),
        expected_kind="not_found",
    )
    print("http-smoke-check passed")


def _open_url(url: str):
    try:
        return urllib_request.urlopen(url)
    except error.URLError as exc:
        ssl_reason = getattr(exc, "reason", None)
        if (
            os.environ.get("SELLEROS_HTTP_ALLOW_INSECURE_FALLBACK", "").strip() == "1"
            and isinstance(ssl_reason, ssl.SSLCertVerificationError)
        ):
            return urllib_request.urlopen(url, context=ssl._create_unverified_context())
        raise


if __name__ == "__main__":
    main()
