"""HTTP smoke-check for sheet_vitrina_v1 feedbacks read-only MVP route and tab."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from urllib import error, parse as urllib_parse, request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_FEEDBACKS_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application.sheet_vitrina_v1_feedbacks import SheetVitrinaV1FeedbacksBlock  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402

NOW = datetime(2026, 4, 29, 9, 0, tzinfo=timezone.utc)


class FakeFeedbacksSource:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def fetch_feedbacks(
        self,
        *,
        date_from_ts: int,
        date_to_ts: int,
        is_answered: bool,
        request_timestamps: list[float],
    ) -> list[dict[str, object]]:
        self.calls.append(
            {
                "date_from_ts": date_from_ts,
                "date_to_ts": date_to_ts,
                "is_answered": is_answered,
                "request_counter_seen": len(request_timestamps),
            }
        )
        if is_answered:
            return [
                {
                    "id": "answered-5",
                    "createdDate": "2026-04-28T10:00:00Z",
                    "productValuation": 5,
                    "answer": {"text": "Спасибо за отзыв"},
                    "productDetails": {
                        "nmId": 210183919,
                        "supplierArticle": "WB-1",
                        "productName": "Товар A",
                        "brandName": "Brand",
                    },
                    "text": "Отлично",
                    "pros": "Качество",
                    "cons": "",
                }
            ]
        return [
            {
                "id": "unanswered-1",
                "createdDate": "2026-04-29T07:00:00Z",
                "productValuation": 1,
                "answer": None,
                "productDetails": {
                    "nmId": 330000001,
                    "supplierArticle": "WB-2",
                    "productName": "Товар B",
                    "brandName": "Brand",
                },
                "text": "Проблема с упаковкой",
                "pros": "",
                "cons": "Упаковка",
            },
            {
                "id": "unanswered-4-filtered-out",
                "createdDate": "2026-04-27T07:00:00Z",
                "productValuation": 4,
                "answer": None,
                "productDetails": {"nmId": 330000002, "productName": "Товар C"},
                "text": "Нормально",
            },
        ]


def main() -> None:
    fake_source = FakeFeedbacksSource()
    block = SheetVitrinaV1FeedbacksBlock(source=fake_source, now_factory=lambda: NOW)
    payload = block.build(
        date_from="2026-04-27",
        date_to="2026-04-29",
        stars=[1, 5],
        is_answered="all",
    )
    if payload.get("contract_name") != "sheet_vitrina_v1_feedbacks":
        raise AssertionError(f"feedbacks contract identity mismatch: {payload}")
    if [call["is_answered"] for call in fake_source.calls] != [False, True]:
        raise AssertionError(f"default all must read unanswered and answered streams, got {fake_source.calls}")
    rows = payload.get("rows") or []
    if [row.get("feedback_id") for row in rows] != ["unanswered-1", "answered-5"]:
        raise AssertionError(f"feedback rows must be filtered by stars and sorted desc, got {rows}")
    summary = payload.get("summary") or {}
    if summary.get("total") != 2 or summary.get("answered") != 1 or summary.get("unanswered") != 1:
        raise AssertionError(f"feedback summary mismatch: {summary}")
    if (summary.get("by_star") or {}).get("1") != 1 or (summary.get("by_star") or {}).get("5") != 1:
        raise AssertionError(f"feedback star buckets mismatch: {summary}")

    fake_source.calls.clear()
    only_unanswered = block.build(
        date_from="2026-04-27",
        date_to="2026-04-29",
        stars=[1, 2, 3, 4, 5],
        is_answered="false",
    )
    if [call["is_answered"] for call in fake_source.calls] != [False]:
        raise AssertionError(f"is_answered=false must read only unanswered stream, got {fake_source.calls}")
    if (only_unanswered.get("summary") or {}).get("answered") != 0:
        raise AssertionError(f"is_answered=false summary must not include answered rows: {only_unanswered}")

    with TemporaryDirectory(prefix="sheet-vitrina-feedbacks-http-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        http_source = FakeFeedbacksSource()
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            now_factory=lambda: NOW,
            feedbacks_block=SheetVitrinaV1FeedbacksBlock(source=http_source, now_factory=lambda: NOW),
        )
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=_reserve_free_port(),
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path="/v1/sheet-vitrina-v1/refresh",
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime_dir,
        )
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{config.port}"
            ui_status, ui_html = _get_text(f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}")
            if ui_status != 200:
                raise AssertionError(f"web vitrina UI route must return 200, got {ui_status}")
            for expected in (
                'data-unified-tab-button="feedbacks"',
                'data-unified-tab-panel="feedbacks"',
                'data-feedbacks-load',
                'data-feedbacks-range-toggle',
                '"feedbacks_path": "/v1/sheet-vitrina-v1/feedbacks"',
            ):
                if expected not in ui_html:
                    raise AssertionError(f"feedbacks UI must contain {expected!r}")

            url = (
                f"{base_url}{DEFAULT_SHEET_FEEDBACKS_PATH}?"
                + urllib_parse.urlencode(
                    {
                        "date_from": "2026-04-27",
                        "date_to": "2026-04-29",
                        "stars": "1,5",
                        "is_answered": "all",
                    }
                )
            )
            route_status, route_payload = _get_json(url)
            if route_status != 200:
                raise AssertionError(f"feedbacks route must return 200, got {route_status}: {route_payload}")
            if route_payload.get("meta", {}).get("source") != "WB API / feedbacks":
                raise AssertionError(f"feedbacks route source meta mismatch: {route_payload}")
            if route_payload.get("summary", {}).get("total") != 2:
                raise AssertionError(f"feedbacks route summary mismatch: {route_payload}")

            invalid_status, invalid_payload = _get_json(
                f"{base_url}{DEFAULT_SHEET_FEEDBACKS_PATH}?date_from=2026-04-27&date_to=2026-04-29&stars=6"
            )
            if invalid_status != 422 or "stars" not in str(invalid_payload.get("error") or ""):
                raise AssertionError(f"invalid stars must return 422, got {invalid_status}: {invalid_payload}")
        finally:
            server.shutdown()
            thread.join(timeout=5)

    print("sheet-vitrina-v1-feedbacks-http-smoke passed")


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get_json(url: str) -> tuple[int, dict[str, object]]:
    status, text = _get_text(url)
    try:
        return status, json.loads(text)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"non-json response from {url}: {text[:300]}") from exc


def _get_text(url: str) -> tuple[int, str]:
    req = urllib_request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urllib_request.urlopen(req, timeout=10) as response:
            return int(response.status), response.read().decode("utf-8")
    except error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8")


if __name__ == "__main__":
    main()
