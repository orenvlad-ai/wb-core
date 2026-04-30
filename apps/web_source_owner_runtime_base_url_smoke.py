"""Smoke-check local owner-runtime base URL defaults for bot-backed sources."""

from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.seller_funnel_snapshot_block import (  # noqa: E402
    HttpBackedSellerFunnelSnapshotSource,
)
from packages.adapters.web_source_snapshot_block import HttpBackedWebSourceSnapshotSource  # noqa: E402


SHARED_ENV = "SHEET_VITRINA_WEBSOURCE_CURRENT_SYNC_API_BASE_URL"
WEB_ENV = "SHEET_VITRINA_WEB_SOURCE_SNAPSHOT_BASE_URL"
SELLER_ENV = "SHEET_VITRINA_SELLER_FUNNEL_SNAPSHOT_BASE_URL"


def main() -> None:
    previous = {key: os.environ.get(key) for key in (SHARED_ENV, WEB_ENV, SELLER_ENV)}
    try:
        for key in previous:
            os.environ.pop(key, None)
        _assert_base(HttpBackedWebSourceSnapshotSource(), "http://127.0.0.1:8000")
        _assert_base(HttpBackedSellerFunnelSnapshotSource(), "http://127.0.0.1:8000")

        os.environ[SHARED_ENV] = "http://127.0.0.1:8010"
        _assert_base(HttpBackedWebSourceSnapshotSource(), "http://127.0.0.1:8010")
        _assert_base(HttpBackedSellerFunnelSnapshotSource(), "http://127.0.0.1:8010")

        os.environ[WEB_ENV] = "http://127.0.0.1:8020"
        os.environ[SELLER_ENV] = "http://127.0.0.1:8030"
        _assert_base(HttpBackedWebSourceSnapshotSource(), "http://127.0.0.1:8020")
        _assert_base(HttpBackedSellerFunnelSnapshotSource(), "http://127.0.0.1:8030")
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    print("owner_runtime_base_url: ok -> local default and env overrides")


def _assert_base(source: object, expected: str) -> None:
    actual = getattr(source, "_base_url")
    if actual != expected:
        raise AssertionError(f"unexpected owner runtime base URL: {actual!r}, expected {expected!r}")


if __name__ == "__main__":
    main()
