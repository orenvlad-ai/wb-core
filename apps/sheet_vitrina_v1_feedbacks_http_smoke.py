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
    DEFAULT_SHEET_FEEDBACKS_AI_ANALYZE_PATH,
    DEFAULT_SHEET_FEEDBACKS_AI_PROMPT_PATH,
    DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_PATH,
    DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SYNC_STATUS_JOB_PATH,
    DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SYNC_STATUS_PATH,
    DEFAULT_SHEET_FEEDBACKS_EXPORT_PATH,
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
from packages.application.simple_xlsx import read_first_sheet_rows  # noqa: E402
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
        chunk_date = datetime.fromtimestamp(date_to_ts, tz=timezone.utc).date().isoformat()
        if is_answered:
            if chunk_date != "2026-04-28":
                return []
            return [
                {
                    "id": "answered-5",
                    "createdDate": f"{chunk_date}T10:00:00Z",
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
        if chunk_date == "2026-04-29":
            return [
                {
                    "id": "unanswered-1",
                    "createdDate": f"{chunk_date}T07:00:00Z",
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
                    "photoLinks": [{"fullSize": "https://example.test/photo.jpg"}],
                }
            ]
        if chunk_date == "2026-04-27":
            return [
                {
                    "id": "unanswered-4-filtered-out",
                    "createdDate": f"{chunk_date}T07:00:00Z",
                    "productValuation": 4,
                    "answer": None,
                    "productDetails": {"nmId": 330000002, "productName": "Товар C"},
                    "text": "Нормально",
                }
            ]
        return []


class PeriodAwareFakeFeedbacksSource:
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
        self.calls.append({"date_from_ts": date_from_ts, "date_to_ts": date_to_ts, "is_answered": is_answered})
        if is_answered:
            return []
        chunk_date = datetime.fromtimestamp(date_to_ts, tz=timezone.utc).date().isoformat()
        rows: list[dict[str, object]] = []
        if "2026-04-01" <= chunk_date <= "2026-04-24":
            rows.append(_feedback_row(f"one-star-{chunk_date}", chunk_date, 1))
        if chunk_date == "2026-04-10":
            rows.append(_feedback_row("filtered-two-star", chunk_date, 2))
        if chunk_date == "2026-03-31":
            rows.append(_feedback_row("outside-before", chunk_date, 1))
        return rows


class LargeFakeFeedbacksSource:
    def __init__(self, *, row_count: int) -> None:
        self.row_count = row_count
        self.calls: list[dict[str, object]] = []

    def fetch_feedbacks(
        self,
        *,
        date_from_ts: int,
        date_to_ts: int,
        is_answered: bool,
        request_timestamps: list[float],
    ) -> list[dict[str, object]]:
        self.calls.append({"date_from_ts": date_from_ts, "date_to_ts": date_to_ts, "is_answered": is_answered})
        if is_answered:
            return []
        return [
            _feedback_row(f"large-{index}", "2026-04-24", 1)
            for index in range(self.row_count)
        ]


def _feedback_row(feedback_id: str, day: str, star: int) -> dict[str, object]:
    return {
        "id": feedback_id,
        "createdDate": f"{day}T07:00:00Z",
        "productValuation": star,
        "answer": None,
        "productDetails": {
            "nmId": 330000001,
            "supplierArticle": f"SKU-{feedback_id}",
            "productName": "Товар",
            "brandName": "Brand",
        },
        "text": f"Отзыв {feedback_id}",
        "pros": "",
        "cons": "Минус",
    }


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
    if [call["is_answered"] for call in fake_source.calls] != [False, True, False, True, False, True]:
        raise AssertionError(f"default all must read unanswered and answered streams per day, got {fake_source.calls}")
    rows = payload.get("rows") or []
    if [row.get("feedback_id") for row in rows] != ["unanswered-1", "answered-5"]:
        raise AssertionError(f"feedback rows must be filtered by stars and sorted desc, got {rows}")
    summary = payload.get("summary") or {}
    if summary.get("total") != 2 or summary.get("answered") != 1 or summary.get("unanswered") != 1:
        raise AssertionError(f"feedback summary mismatch: {summary}")
    if (summary.get("by_star") or {}).get("1") != 1 or (summary.get("by_star") or {}).get("5") != 1:
        raise AssertionError(f"feedback star buckets mismatch: {summary}")
    meta = payload.get("meta") or {}
    if meta.get("chunk_count") != 3 or meta.get("upstream_page_count") != 6:
        raise AssertionError(f"feedback diagnostics must expose chunks/pages, got: {meta}")
    if meta.get("raw_fetched_count") != 3 or meta.get("deduped_count") != 3 or meta.get("final_filtered_count") != 2:
        raise AssertionError(f"feedback diagnostics counts mismatch: {meta}")
    if meta.get("earliest_feedback_date") != "2026-04-28" or meta.get("latest_feedback_date") != "2026-04-29":
        raise AssertionError(f"feedback diagnostics date range mismatch: {meta}")

    fake_source.calls.clear()
    only_unanswered = block.build(
        date_from="2026-04-27",
        date_to="2026-04-29",
        stars=[1, 2, 3, 4, 5],
        is_answered="false",
    )
    if [call["is_answered"] for call in fake_source.calls] != [False, False, False]:
        raise AssertionError(f"is_answered=false must read only unanswered stream, got {fake_source.calls}")
    if (only_unanswered.get("summary") or {}).get("answered") != 0:
        raise AssertionError(f"is_answered=false summary must not include answered rows: {only_unanswered}")

    period_source = PeriodAwareFakeFeedbacksSource()
    period_block = SheetVitrinaV1FeedbacksBlock(source=period_source, now_factory=lambda: NOW)
    period_a = period_block.build(date_from="2026-04-13", date_to="2026-04-24", stars=[1], is_answered="false")
    period_b = period_block.build(date_from="2026-04-01", date_to="2026-04-24", stars=[1], is_answered="false")
    count_a = int((period_a.get("summary") or {}).get("total") or 0)
    count_b = int((period_b.get("summary") or {}).get("total") or 0)
    if count_a != 12 or count_b != 24 or count_b <= count_a:
        raise AssertionError(f"longer 1-star period must be a truthful superset, got A={count_a}, B={count_b}")
    large_window = period_block.build(date_from="2026-03-24", date_to="2026-04-24", stars=[1], is_answered="false")
    if (large_window.get("summary") or {}).get("total") != 25 or (large_window.get("meta") or {}).get("chunk_count") != 32:
        raise AssertionError(f"feedbacks backend must allow bounded >31 day windows, got: {large_window.get('meta')}")
    for result, start, end in ((period_a, "2026-04-13", "2026-04-24"), (period_b, "2026-04-01", "2026-04-24")):
        for row in result.get("rows") or []:
            if not (start <= str(row.get("created_date")) <= end):
                raise AssertionError(f"row escaped requested date range {start}..{end}: {row}")
            if row.get("product_valuation") != 1:
                raise AssertionError(f"row escaped requested star filter: {row}")

    large_payload = SheetVitrinaV1FeedbacksBlock(
        source=LargeFakeFeedbacksSource(row_count=650),
        now_factory=lambda: NOW,
    ).build(date_from="2026-04-24", date_to="2026-04-24", stars=[1], is_answered="false")
    if (large_payload.get("summary") or {}).get("total") != 650:
        raise AssertionError(f"feedbacks route must not cap at 500 rows, got: {large_payload.get('summary')}")
    if (large_payload.get("meta") or {}).get("truncated"):
        raise AssertionError(f"large non-truncated fixture must not be marked truncated: {large_payload.get('meta')}")

    workbook_bytes, filename = block.build_export(
        {
            "date_from": "2026-04-27",
            "date_to": "2026-04-29",
            "rows": [
                {
                    **rows[0],
                    "ai_complaint_fit_label": "Да",
                    "ai_category_label": "Мат, оскорбления или угрозы",
                    "ai_reason": "Есть нарушение",
                    "ai_confidence_label": "Высокая",
                }
            ],
        }
    )
    if filename != "wb_feedbacks_2026-04-27_2026-04-29.xlsx":
        raise AssertionError(f"feedbacks export filename mismatch: {filename}")
    workbook_rows = read_first_sheet_rows(workbook_bytes)
    expected_headers = [
        "Дата", "Оценка", "nmId", "Артикул", "Товар", "Текст отзыва", "Плюсы", "Минусы",
        "Есть ответ", "Ответ продавца", "Фото", "Видео", "Подходит для жалобы", "Категория AI",
        "Причина AI", "Уверенность AI", "ID отзыва",
    ]
    if workbook_rows[0] != expected_headers or len(workbook_rows) != 2:
        raise AssertionError(f"feedbacks export workbook mismatch: {workbook_rows}")

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
                'data-feedbacks-ai-analyze',
                'data-feedbacks-export',
                'data-feedbacks-model',
                'data-feedbacks-subtab="prompt"',
                'data-feedbacks-subtab="complaints"',
                'data-feedbacks-complaints-sync',
                'data-feedbacks-range-toggle',
                '"feedbacks_path": "/v1/sheet-vitrina-v1/feedbacks"',
                f'"feedbacks_ai_prompt_path": "{DEFAULT_SHEET_FEEDBACKS_AI_PROMPT_PATH}"',
                f'"feedbacks_ai_analyze_path": "{DEFAULT_SHEET_FEEDBACKS_AI_ANALYZE_PATH}"',
                f'"feedbacks_export_path": "{DEFAULT_SHEET_FEEDBACKS_EXPORT_PATH}"',
                f'"feedbacks_complaints_path": "{DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_PATH}"',
                f'"feedbacks_complaints_sync_status_path": "{DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SYNC_STATUS_PATH}"',
                f'"feedbacks_complaints_sync_status_job_path": "{DEFAULT_SHEET_FEEDBACKS_COMPLAINTS_SYNC_STATUS_JOB_PATH}"',
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
            if route_payload.get("meta", {}).get("final_filtered_count") != 2:
                raise AssertionError(f"feedbacks route diagnostics mismatch: {route_payload}")

            export_status, export_headers, export_body = _post_binary(
                f"{base_url}{DEFAULT_SHEET_FEEDBACKS_EXPORT_PATH}",
                {
                    "date_from": "2026-04-27",
                    "date_to": "2026-04-29",
                    "rows": [
                        {
                            **(route_payload.get("rows") or [])[0],
                            "ai_complaint_fit_label": "Проверить",
                            "ai_category_label": "Доставка, ПВЗ или логистика WB",
                            "ai_reason": "Нужна ручная проверка",
                            "ai_confidence_label": "Средняя",
                        }
                    ],
                },
            )
            if export_status != 200 or "spreadsheetml.sheet" not in export_headers.get("Content-Type", ""):
                raise AssertionError(f"feedbacks export route mismatch: {export_status} {export_headers}")
            if len(read_first_sheet_rows(export_body)) != 2:
                raise AssertionError("feedbacks export route workbook must contain header + one row")

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


def _post_binary(url: str, payload: dict[str, object]) -> tuple[int, dict[str, str], bytes]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib_request.Request(
        url,
        method="POST",
        headers={"Accept": "*/*", "Content-Type": "application/json; charset=utf-8"},
        data=body,
    )
    try:
        with urllib_request.urlopen(req, timeout=10) as response:
            return int(response.status), dict(response.headers.items()), response.read()
    except error.HTTPError as exc:
        return int(exc.code), dict(exc.headers.items()), exc.read()


if __name__ == "__main__":
    main()
