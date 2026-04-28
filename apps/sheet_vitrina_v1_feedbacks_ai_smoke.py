"""Smoke-check feedbacks AI prompt storage and analyze routes."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from typing import Any, Mapping
from urllib import error, request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_FEEDBACKS_AI_ANALYZE_PATH,
    DEFAULT_SHEET_FEEDBACKS_AI_PROMPT_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application.sheet_vitrina_v1_feedbacks_ai import (  # noqa: E402
    SheetVitrinaV1FeedbacksAiBlock,
)
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402

NOW = datetime(2026, 4, 29, 9, 0, tzinfo=timezone.utc)


class FakeAiProvider:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def analyze_batch(
        self,
        *,
        prompt: str,
        rows: list[Mapping[str, Any]],
        schema: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        self.calls.append({"prompt_length": len(prompt), "row_count": len(rows), "schema_type": schema.get("type")})
        if self.fail:
            return [{"feedback_id": "unexpected", "complaint_fit": "yes"}], {"model": "fake"}
        results = []
        for index, row in enumerate(rows):
            fit = "yes" if index == 0 else ("review" if index == 1 else "no")
            category = "profanity_or_insult" if fit == "yes" else ("wb_delivery_or_pickup_point" if fit == "review" else "product_quality_claim")
            confidence = "high" if fit == "yes" else ("medium" if fit == "review" else "low")
            results.append(
                {
                    "feedback_id": str(row["feedback_id"]),
                    "complaint_fit": fit,
                    "complaint_fit_label": {"yes": "Да", "review": "Проверить", "no": "Нет"}[fit],
                    "category": category,
                    "category_label": {
                        "profanity_or_insult": "Мат, оскорбления или угрозы",
                        "wb_delivery_or_pickup_point": "Доставка, ПВЗ или логистика WB",
                        "product_quality_claim": "Претензия к товару",
                    }[category],
                    "reason": "Короткая причина",
                    "confidence": confidence,
                    "confidence_label": {"high": "Высокая", "medium": "Средняя", "low": "Низкая"}[confidence],
                    "evidence": "фрагмент",
                }
            )
        return results, {"model": "fake-feedbacks-ai", "response_id": "fake-response"}


def main() -> None:
    with TemporaryDirectory(prefix="sheet-vitrina-feedbacks-ai-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        fake_provider = FakeAiProvider()
        block = SheetVitrinaV1FeedbacksAiBlock(
            runtime_dir=runtime_dir,
            provider=fake_provider,
            now_factory=lambda: NOW,
            min_analyze_interval_seconds=0,
        )
        initial = block.get_prompt()
        if initial.get("status") != "missing" or not initial.get("starter_prompt"):
            raise AssertionError(f"initial prompt must expose missing status and starter prompt: {initial}")
        try:
            block.save_prompt({"prompt": ""})
        except ValueError:
            pass
        else:
            raise AssertionError("empty prompt must be rejected")
        saved = block.save_prompt({"prompt": "Разбери отзывы по правилам."})
        if saved.get("status") != "ready" or saved.get("prompt") != "Разбери отзывы по правилам.":
            raise AssertionError(f"saved prompt payload mismatch: {saved}")
        analysis = block.analyze({"rows": _rows()})
        if analysis.get("contract_name") != "sheet_vitrina_v1_feedbacks_ai_analysis":
            raise AssertionError(f"analysis contract mismatch: {analysis}")
        if [item["complaint_fit"] for item in analysis["results"]] != ["yes", "review", "no"]:
            raise AssertionError(f"analysis result order/mapping mismatch: {analysis}")
        if analysis["results"][0]["category_label"] != "Мат, оскорбления или угрозы":
            raise AssertionError(f"category label mismatch: {analysis}")

        failing = SheetVitrinaV1FeedbacksAiBlock(
            runtime_dir=runtime_dir,
            provider=FakeAiProvider(fail=True),
            now_factory=lambda: NOW,
            min_analyze_interval_seconds=0,
        )
        try:
            failing.analyze({"rows": _rows()[:1]})
        except Exception as exc:
            if "unexpected feedback_id" not in str(exc):
                raise AssertionError(f"invalid provider output must be surfaced clearly, got: {exc}") from exc
        else:
            raise AssertionError("invalid provider output must fail")

        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            now_factory=lambda: NOW,
            feedbacks_ai_block=block,
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
            prompt_status, prompt_payload = _get_json(f"{base_url}{DEFAULT_SHEET_FEEDBACKS_AI_PROMPT_PATH}")
            if prompt_status != 200 or prompt_payload.get("status") != "ready":
                raise AssertionError(f"GET prompt route mismatch: {prompt_status} {prompt_payload}")
            empty_status, empty_payload = _post_json(f"{base_url}{DEFAULT_SHEET_FEEDBACKS_AI_PROMPT_PATH}", {"prompt": ""})
            if empty_status != 422 or "prompt" not in str(empty_payload.get("error") or ""):
                raise AssertionError(f"empty prompt route must return 422, got: {empty_status} {empty_payload}")
            analyze_status, analyze_payload = _post_json(f"{base_url}{DEFAULT_SHEET_FEEDBACKS_AI_ANALYZE_PATH}", {"rows": _rows()})
            if analyze_status != 200 or len(analyze_payload.get("results") or []) != 3:
                raise AssertionError(f"AI analyze route mismatch: {analyze_status} {analyze_payload}")
        finally:
            server.shutdown()
            thread.join(timeout=5)

    print("sheet-vitrina-v1-feedbacks-ai-smoke passed")


def _rows() -> list[dict[str, Any]]:
    return [
        {
            "feedback_id": "fb-1",
            "created_at": "2026-04-29T07:00:00Z",
            "rating": 1,
            "text": "Мат и оскорбления",
            "pros": "",
            "cons": "",
            "nm_id": 123,
            "product_name": "Товар",
            "supplier_article": "A-1",
            "is_answered": False,
            "answer_text": "",
        },
        {
            "feedback_id": "fb-2",
            "created_at": "2026-04-28T07:00:00Z",
            "rating": 2,
            "text": "Доставили в мятый коробке",
            "pros": "",
            "cons": "Доставка",
            "nm_id": 124,
            "product_name": "Товар",
            "supplier_article": "A-2",
            "is_answered": False,
            "answer_text": "",
        },
        {
            "feedback_id": "fb-3",
            "created_at": "2026-04-27T07:00:00Z",
            "rating": 3,
            "text": "Не подошёл размер",
            "pros": "",
            "cons": "Размер",
            "nm_id": 125,
            "product_name": "Товар",
            "supplier_article": "A-3",
            "is_answered": True,
            "answer_text": "Ответ",
        },
    ]


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get_json(url: str) -> tuple[int, dict[str, Any]]:
    request = urllib_request.Request(url, method="GET", headers={"Accept": "application/json"})
    return _send_json(request)


def _post_json(url: str, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib_request.Request(
        url,
        method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json; charset=utf-8"},
        data=body,
    )
    return _send_json(request)


def _send_json(request: urllib_request.Request) -> tuple[int, dict[str, Any]]:
    try:
        with urllib_request.urlopen(request, timeout=10) as response:
            return int(response.status), json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return int(exc.code), json.loads(exc.read().decode("utf-8"))


if __name__ == "__main__":
    main()
