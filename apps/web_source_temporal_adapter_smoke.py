"""Targeted smoke-check for web-source latest-match adapter resolution."""

from __future__ import annotations

from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from urllib import parse as urllib_parse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.seller_funnel_snapshot_block import HttpBackedSellerFunnelSnapshotSource
from packages.adapters.web_source_snapshot_block import HttpBackedWebSourceSnapshotSource
from packages.application.seller_funnel_snapshot_block import SellerFunnelSnapshotBlock
from packages.application.web_source_snapshot_block import WebSourceSnapshotBlock
from packages.contracts.seller_funnel_snapshot_block import SellerFunnelSnapshotRequest
from packages.contracts.web_source_snapshot_block import WebSourceSnapshotRequest

MATCH_DATE = "2026-04-13"
MISMATCH_DATE = "2026-04-14"


def main() -> None:
    with _MockSellerosServer() as server:
        web_block = WebSourceSnapshotBlock(HttpBackedWebSourceSnapshotSource(base_url=server.base_url))
        seller_block = SellerFunnelSnapshotBlock(HttpBackedSellerFunnelSnapshotSource(base_url=server.base_url))

        server.set_latest_date(MATCH_DATE)
        _assert_success(
            "web_latest_match",
            asdict(
                web_block.execute(
                    WebSourceSnapshotRequest(
                        snapshot_type="web_source_snapshot",
                        date_from=MATCH_DATE,
                        date_to=MATCH_DATE,
                    )
                )
            ),
        )
        _assert_success(
            "seller_latest_match",
            asdict(
                seller_block.execute(
                    SellerFunnelSnapshotRequest(
                        snapshot_type="seller_funnel_snapshot",
                        date=MATCH_DATE,
                    )
                )
            ),
        )

        server.set_latest_date(MISMATCH_DATE)
        _assert_not_found_with_resolution_note(
            "web_latest_mismatch",
            asdict(
                web_block.execute(
                    WebSourceSnapshotRequest(
                        snapshot_type="web_source_snapshot",
                        date_from=MATCH_DATE,
                        date_to=MATCH_DATE,
                    )
                )
            ),
            expected_detail_fragment=f"latest_available_window={MISMATCH_DATE}..{MISMATCH_DATE}",
        )
        _assert_not_found_with_resolution_note(
            "seller_latest_mismatch",
            asdict(
                seller_block.execute(
                    SellerFunnelSnapshotRequest(
                        snapshot_type="seller_funnel_snapshot",
                        date=MATCH_DATE,
                    )
                )
            ),
            expected_detail_fragment=f"latest_available_date={MISMATCH_DATE}",
        )

        before_web_latest_hits = server.latest_hits["web"]
        before_seller_latest_hits = server.latest_hits["seller"]
        _assert_not_found_plain(
            "web_not_found",
            asdict(
                web_block.execute(
                    WebSourceSnapshotRequest(
                        snapshot_type="web_source_snapshot",
                        date_from="1900-01-01",
                        date_to="1900-01-01",
                        scenario="not_found",
                    )
                )
            ),
        )
        _assert_not_found_plain(
            "seller_not_found",
            asdict(
                seller_block.execute(
                    SellerFunnelSnapshotRequest(
                        snapshot_type="seller_funnel_snapshot",
                        date="1900-01-01",
                        scenario="not_found",
                    )
                )
            ),
        )
        if server.latest_hits["web"] != before_web_latest_hits:
            raise AssertionError("web not_found scenario must not probe latest endpoint")
        if server.latest_hits["seller"] != before_seller_latest_hits:
            raise AssertionError("seller not_found scenario must not probe latest endpoint")

        print(f"web_latest_match: ok -> latest_hits={server.latest_hits['web']}")
        print(f"seller_latest_match: ok -> latest_hits={server.latest_hits['seller']}")
        print("smoke-check passed")


def _assert_success(name: str, result: dict[str, object]) -> None:
    payload = result["result"]
    if payload["kind"] != "success":
        raise AssertionError(f"{name}: expected success, got {payload}")
    if payload["count"] != 1:
        raise AssertionError(f"{name}: unexpected count {payload['count']}")


def _assert_not_found_with_resolution_note(
    name: str,
    result: dict[str, object],
    *,
    expected_detail_fragment: str,
) -> None:
    payload = result["result"]
    if payload["kind"] != "not_found":
        raise AssertionError(f"{name}: expected not_found, got {payload}")
    detail = str(payload["detail"])
    if "resolution_rule=explicit_or_latest_date_match" not in detail:
        raise AssertionError(f"{name}: resolution rule missing in detail {detail!r}")
    if expected_detail_fragment not in detail:
        raise AssertionError(f"{name}: expected {expected_detail_fragment!r} in detail {detail!r}")


def _assert_not_found_plain(name: str, result: dict[str, object]) -> None:
    payload = result["result"]
    if payload["kind"] != "not_found":
        raise AssertionError(f"{name}: expected not_found, got {payload}")
    if "explicit not found" not in str(payload["detail"]):
        raise AssertionError(f"{name}: expected plain explicit not_found detail, got {payload['detail']!r}")


class _MockSellerosServer:
    def __init__(self) -> None:
        self.latest_date = MATCH_DATE
        self.latest_hits = {"web": 0, "seller": 0}
        self._server = HTTPServer(("127.0.0.1", _reserve_free_port()), self._build_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def set_latest_date(self, snapshot_date: str) -> None:
        self.latest_date = snapshot_date

    def __enter__(self) -> "_MockSellerosServer":
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)
        self._server.server_close()

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urllib_parse.urlparse(self.path)
                if parsed.path == "/v1/search-analytics/snapshot":
                    self._write_web(parsed.query)
                    return
                if parsed.path == "/v1/sales-funnel/daily":
                    self._write_seller(parsed.query)
                    return
                self.send_error(404)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A003
                return

            def _write_web(self, query: str) -> None:
                if query:
                    self._write_json(404, {"detail": "explicit not found"})
                    return
                parent.latest_hits["web"] += 1
                self._write_json(
                    200,
                    {
                        "date_from": parent.latest_date,
                        "date_to": parent.latest_date,
                        "count": 1,
                        "items": [
                            {
                                "nm_id": 1001,
                                "views_current": 10,
                                "ctr_current": 12,
                                "orders_current": 3,
                                "position_avg": 7,
                            }
                        ],
                    },
                )

            def _write_seller(self, query: str) -> None:
                if query:
                    self._write_json(404, {"detail": "explicit not found"})
                    return
                parent.latest_hits["seller"] += 1
                self._write_json(
                    200,
                    {
                        "date": parent.latest_date,
                        "count": 1,
                        "items": [
                            {
                                "nm_id": 1001,
                                "name": "Test",
                                "vendor_code": "VC-1001",
                                "view_count": 20,
                                "open_card_count": 5,
                                "ctr": 25,
                            }
                        ],
                    },
                )

            def _write_json(self, status: int, payload: dict[str, object]) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
