"""Server-side prompt storage and AI analysis for sheet_vitrina_v1 feedbacks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Mapping, Protocol

from packages.adapters.openai_feedbacks_ai import (
    OpenAiFeedbacksAnalysisError,
    OpenAiFeedbacksAnalysisProvider,
)


PROMPT_CONTRACT_NAME = "sheet_vitrina_v1_feedbacks_ai_prompt"
ANALYSIS_CONTRACT_NAME = "sheet_vitrina_v1_feedbacks_ai_analysis"
CONTRACT_VERSION = "v1"
MAX_PROMPT_LENGTH = 16000
MAX_ROWS_PER_RUN = 3
BATCH_SIZE = 3
MAX_TEXT_CHARS_PER_REVIEW = 1400
MIN_ANALYZE_INTERVAL_SECONDS = 3.0
DEFAULT_FEEDBACKS_AI_MODEL = "gpt-5-mini"
PREFERRED_FEEDBACKS_AI_MODELS = (
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5.2",
    "gpt-5.2-pro",
    "gpt-5",
    "gpt-5-mini",
    "gpt-5-nano",
)
FALLBACK_FEEDBACKS_AI_MODELS = ("gpt-5-mini", "gpt-5")
AVAILABLE_FEEDBACKS_AI_MODELS = FALLBACK_FEEDBACKS_AI_MODELS

COMPLAINT_FIT_LABELS = {
    "yes": "Да",
    "review": "Проверить",
    "no": "Нет",
}
CONFIDENCE_LABELS = {
    "high": "Высокая",
    "medium": "Средняя",
    "low": "Низкая",
}
CATEGORY_LABELS = {
    "profanity_or_insult": "Мат, оскорбления или угрозы",
    "links_contacts_ads": "Ссылки, контакты или реклама",
    "not_about_product": "Отзыв не про товар",
    "wrong_product_or_media": "Другой товар или медиа",
    "wb_delivery_or_pickup_point": "Доставка, ПВЗ или логистика WB",
    "competitor_suspicion": "Возможный конкурент",
    "product_quality_claim": "Претензия к товару",
    "too_little_information": "Недостаточно данных",
    "other": "Другое",
}

STARTER_PROMPT = """Ты помогаешь оператору Wildberries предварительно разобрать отзывы покупателей и понять, есть ли формальное основание для жалобы на отзыв.

Классифицируй каждый отзыв строго по переданной JSON schema.

Правила:
- Негативная оценка сама по себе не основание для жалобы.
- Честные претензии к товару, качеству, размеру, цвету, комплектации или ожиданиям покупателя обычно классифицируй как no.
- Жалобы на доставку, ПВЗ, логистику, упаковку или сервис Wildberries обычно классифицируй как review, не auto-yes.
- Категорию competitor_suspicion выбирай только при явных признаках конкурента, не выдумывай мотивы.
- Если данных мало, выбирай review или no, не yes.
- yes ставь только когда есть явное формальное основание: мат/оскорбления/угрозы, контакты/ссылки/реклама, отзыв явно не про товар, явно другой товар/медиа или похожее нарушение правил площадки.
- reason и evidence пиши коротко на русском, без пересказа всего отзыва.
- confidence отражает уверенность: high, medium или low.
- Верни только JSON по schema, без markdown и дополнительных пояснений."""


class FeedbacksAiProvider(Protocol):
    def analyze_batch(
        self,
        *,
        prompt: str,
        model: str,
        rows: list[Mapping[str, Any]],
        schema: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        raise NotImplementedError

    def list_models(self) -> list[str]:
        raise NotImplementedError


class SheetVitrinaV1FeedbacksAiError(RuntimeError):
    def __init__(self, message: str, *, http_status: int = 502) -> None:
        self.http_status = http_status
        super().__init__(message)


@dataclass(frozen=True)
class FeedbacksAiPromptState:
    prompt: str
    model: str
    updated_at: str | None


@dataclass(frozen=True)
class FeedbacksAiModelCatalog:
    available_models: tuple[str, ...]
    preferred_models: tuple[str, ...]
    unavailable_models: tuple[dict[str, str], ...]
    discovery_status: str
    discovery_error: str | None = None


class JsonFileFeedbacksAiPromptStore:
    def __init__(self, runtime_dir: Path, *, filename: str = "sheet_vitrina_v1_feedbacks_ai_prompt.json") -> None:
        self.path = runtime_dir / filename
        self._lock = threading.Lock()

    def read(self) -> FeedbacksAiPromptState:
        if not self.path.exists():
            return FeedbacksAiPromptState(prompt="", model=_configured_default_feedbacks_ai_model(), updated_at=None)
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SheetVitrinaV1FeedbacksAiError("saved AI prompt storage is not readable", http_status=500) from exc
        if not isinstance(payload, Mapping):
            raise SheetVitrinaV1FeedbacksAiError("saved AI prompt storage has invalid shape", http_status=500)
        return FeedbacksAiPromptState(
            prompt=str(payload.get("prompt") or ""),
            model=str(payload.get("model") or _configured_default_feedbacks_ai_model()).strip(),
            updated_at=str(payload.get("updated_at") or "") or None,
        )

    def write(
        self,
        *,
        prompt: str,
        model: str,
        updated_at: str,
        catalog: FeedbacksAiModelCatalog | None = None,
    ) -> FeedbacksAiPromptState:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "contract_name": PROMPT_CONTRACT_NAME,
                "contract_version": CONTRACT_VERSION,
                "prompt": prompt,
                "model": model,
                "available_models": list(catalog.available_models if catalog else FALLBACK_FEEDBACKS_AI_MODELS),
                "preferred_models": list(PREFERRED_FEEDBACKS_AI_MODELS),
                "unavailable_models": list(catalog.unavailable_models if catalog else ()),
                "model_discovery_status": catalog.discovery_status if catalog else "fallback",
                "updated_at": updated_at,
            }
            temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temp_path.replace(self.path)
        return FeedbacksAiPromptState(prompt=prompt, model=model, updated_at=updated_at)


class SheetVitrinaV1FeedbacksAiBlock:
    """Bounded feedbacks AI block. It stores prompt config, not analysis truth."""

    def __init__(
        self,
        *,
        runtime_dir: Path,
        provider: FeedbacksAiProvider | None = None,
        prompt_store: JsonFileFeedbacksAiPromptStore | None = None,
        now_factory: Any | None = None,
        min_analyze_interval_seconds: float = MIN_ANALYZE_INTERVAL_SECONDS,
    ) -> None:
        self.provider = provider or OpenAiFeedbacksAnalysisProvider()
        self.prompt_store = prompt_store or JsonFileFeedbacksAiPromptStore(runtime_dir)
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self.min_analyze_interval_seconds = max(0.0, float(min_analyze_interval_seconds))
        self._rate_lock = threading.Lock()
        self._last_analyze_started_at = 0.0

    def get_prompt(self) -> dict[str, Any]:
        state = self.prompt_store.read()
        catalog = _discover_model_catalog(self.provider)
        resolved_state, model_source = _resolve_prompt_state_model(state, catalog)
        return _prompt_payload(resolved_state, catalog=catalog, model_source=model_source)

    def save_prompt(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        prompt = _normalize_prompt(payload.get("prompt"))
        catalog = _discover_model_catalog(self.provider)
        model = _normalize_model(payload.get("model"), catalog)
        updated_at = self.now_factory().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        state = self.prompt_store.write(prompt=prompt, model=model, updated_at=updated_at, catalog=catalog)
        return _prompt_payload(state, catalog=catalog, model_source="saved")

    def analyze(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        prompt_state = self.prompt_store.read()
        if not prompt_state.prompt.strip():
            raise SheetVitrinaV1FeedbacksAiError("Сначала сохраните промпт разбора", http_status=409)
        catalog = _discover_model_catalog(self.provider)
        prompt_state, model_source = _resolve_prompt_state_model(prompt_state, catalog)
        rows = _normalize_input_rows(payload.get("rows"))
        self._enforce_rate_limit()
        batch_results: list[dict[str, Any]] = []
        provider_meta: list[dict[str, Any]] = []
        try:
            for batch_start in range(0, len(rows), BATCH_SIZE):
                batch = rows[batch_start : batch_start + BATCH_SIZE]
                raw_results, meta = self.provider.analyze_batch(
                    prompt=prompt_state.prompt,
                    model=prompt_state.model,
                    rows=batch,
                    schema=analysis_json_schema(),
                )
                batch_results.extend(_validated_results(raw_results, expected_rows=batch))
                provider_meta.append(_safe_provider_meta(meta, batch_size=len(batch)))
        except OpenAiFeedbacksAnalysisError as exc:
            raise SheetVitrinaV1FeedbacksAiError(str(exc), http_status=502) from exc
        analyzed_at = self.now_factory().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        result_by_id = {str(item["feedback_id"]): item for item in batch_results}
        ordered_results = [result_by_id[str(row["feedback_id"])] for row in rows]
        return {
            "contract_name": ANALYSIS_CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "meta": {
                "analyzed_at": analyzed_at,
                "row_count": len(rows),
                "batch_size": BATCH_SIZE,
                "batch_count": len(provider_meta),
                "prompt_updated_at": prompt_state.updated_at,
                "model": prompt_state.model,
                "model_source": model_source,
                "provider_batches": provider_meta,
                "persistence": "not_persisted",
            },
            "results": ordered_results,
        }

    def _enforce_rate_limit(self) -> None:
        if self.min_analyze_interval_seconds <= 0:
            return
        with self._rate_lock:
            now = time.monotonic()
            elapsed = now - self._last_analyze_started_at
            if elapsed < self.min_analyze_interval_seconds:
                retry_after = round(self.min_analyze_interval_seconds - elapsed, 1)
                raise SheetVitrinaV1FeedbacksAiError(
                    f"AI-разбор уже запускался недавно; повторите через {retry_after} сек.",
                    http_status=429,
                )
            self._last_analyze_started_at = now


def _prompt_payload(
    state: FeedbacksAiPromptState,
    *,
    catalog: FeedbacksAiModelCatalog,
    model_source: str,
) -> dict[str, Any]:
    return {
        "contract_name": PROMPT_CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "prompt": state.prompt,
        "model": state.model,
        "available_models": list(catalog.available_models),
        "preferred_models": list(catalog.preferred_models),
        "unavailable_models": list(catalog.unavailable_models),
        "model_source": model_source,
        "model_discovery_status": catalog.discovery_status,
        "model_discovery_error": catalog.discovery_error or "",
        "starter_prompt": STARTER_PROMPT,
        "updated_at": state.updated_at,
        "status": "ready" if state.prompt.strip() else "missing",
        "max_length": MAX_PROMPT_LENGTH,
        "analysis_limits": {
            "max_rows_per_run": MAX_ROWS_PER_RUN,
            "batch_size": BATCH_SIZE,
            "max_text_chars_per_review": MAX_TEXT_CHARS_PER_REVIEW,
        },
    }


def _normalize_prompt(value: Any) -> str:
    prompt = str(value or "").strip()
    if not prompt:
        raise ValueError("prompt must not be empty")
    if len(prompt) > MAX_PROMPT_LENGTH:
        raise ValueError(f"prompt must be at most {MAX_PROMPT_LENGTH} characters")
    return prompt


def _configured_default_feedbacks_ai_model() -> str:
    raw = os.environ.get("OPENAI_MODEL", "").strip()
    return raw or DEFAULT_FEEDBACKS_AI_MODEL


def _discover_model_catalog(provider: FeedbacksAiProvider) -> FeedbacksAiModelCatalog:
    preferred = tuple(PREFERRED_FEEDBACKS_AI_MODELS)
    list_models = getattr(provider, "list_models", None)
    if callable(list_models):
        try:
            model_ids = {str(model_id).strip() for model_id in list_models() if str(model_id).strip()}
        except Exception as exc:
            return _fallback_model_catalog(
                discovery_status="fallback",
                discovery_error="models endpoint unavailable: " + _short_error(exc),
            )
        available = tuple(model for model in preferred if model in model_ids)
        configured_default = _configured_default_feedbacks_ai_model()
        if configured_default in model_ids and configured_default not in available:
            available = (configured_default, *available)
        unavailable = tuple(
            {"model": model, "reason": "not returned by /v1/models for current key"}
            for model in preferred
            if model not in model_ids
        )
        if available:
            return FeedbacksAiModelCatalog(
                available_models=available,
                preferred_models=preferred,
                unavailable_models=unavailable,
                discovery_status="available",
            )
        return _fallback_model_catalog(
            discovery_status="fallback_no_preferred",
            discovery_error="models endpoint returned no preferred feedbacks AI models",
            unavailable_models=unavailable,
        )
    return _fallback_model_catalog(
        discovery_status="fallback",
        discovery_error="provider does not expose model discovery",
    )


def _fallback_model_catalog(
    *,
    discovery_status: str,
    discovery_error: str,
    unavailable_models: tuple[dict[str, str], ...] | None = None,
) -> FeedbacksAiModelCatalog:
    fallback = tuple(
        dict.fromkeys(
            [
                _configured_default_feedbacks_ai_model(),
                *FALLBACK_FEEDBACKS_AI_MODELS,
            ]
        )
    )
    unavailable = unavailable_models or tuple(
        {"model": model, "reason": discovery_error}
        for model in PREFERRED_FEEDBACKS_AI_MODELS
        if model not in fallback
    )
    return FeedbacksAiModelCatalog(
        available_models=fallback,
        preferred_models=tuple(PREFERRED_FEEDBACKS_AI_MODELS),
        unavailable_models=unavailable,
        discovery_status=discovery_status,
        discovery_error=discovery_error,
    )


def _resolve_prompt_state_model(
    state: FeedbacksAiPromptState,
    catalog: FeedbacksAiModelCatalog,
) -> tuple[FeedbacksAiPromptState, str]:
    raw_model = str(state.model or "").strip()
    if raw_model and raw_model in catalog.available_models:
        return state, "saved" if state.updated_at else "default"
    default_model = _configured_default_feedbacks_ai_model()
    if default_model in catalog.available_models:
        return FeedbacksAiPromptState(prompt=state.prompt, model=default_model, updated_at=state.updated_at), "default"
    if DEFAULT_FEEDBACKS_AI_MODEL in catalog.available_models:
        return FeedbacksAiPromptState(prompt=state.prompt, model=DEFAULT_FEEDBACKS_AI_MODEL, updated_at=state.updated_at), "default"
    fallback = catalog.available_models[0]
    return FeedbacksAiPromptState(prompt=state.prompt, model=fallback, updated_at=state.updated_at), "fallback"


def _normalize_model(value: Any, catalog: FeedbacksAiModelCatalog) -> str:
    model = str(value or "").strip()
    if not model:
        return _resolve_prompt_state_model(
            FeedbacksAiPromptState(prompt="", model="", updated_at=None),
            catalog,
        )[0].model
    if model not in catalog.available_models:
        raise ValueError(
            "model is not available for the current OpenAI key; available models: "
            + ", ".join(catalog.available_models)
        )
    return model


def _short_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text[:240]


def _normalize_input_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("rows must be an array")
    if not value:
        raise ValueError("rows must not be empty")
    if len(value) > MAX_ROWS_PER_RUN:
        raise ValueError(f"rows must contain at most {MAX_ROWS_PER_RUN} items")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_row in enumerate(value):
        if not isinstance(raw_row, Mapping):
            raise ValueError(f"rows[{index}] must be an object")
        feedback_id = str(raw_row.get("feedback_id") or "").strip()
        if not feedback_id:
            raise ValueError(f"rows[{index}].feedback_id is required")
        if feedback_id in seen:
            raise ValueError(f"duplicate feedback_id {feedback_id!r}")
        seen.add(feedback_id)
        rows.append(
            {
                "feedback_id": feedback_id,
                "created_at": _short_string(raw_row.get("created_at"), 64),
                "rating": _safe_int(raw_row.get("rating", raw_row.get("product_valuation"))),
                "text": _short_string(raw_row.get("text"), MAX_TEXT_CHARS_PER_REVIEW),
                "pros": _short_string(raw_row.get("pros"), MAX_TEXT_CHARS_PER_REVIEW // 2),
                "cons": _short_string(raw_row.get("cons"), MAX_TEXT_CHARS_PER_REVIEW // 2),
                "nm_id": _safe_int(raw_row.get("nm_id")),
                "product_name": _short_string(raw_row.get("product_name"), 240),
                "supplier_article": _short_string(raw_row.get("supplier_article"), 120),
                "is_answered": bool(raw_row.get("is_answered")),
                "answer_text": _short_string(raw_row.get("answer_text"), MAX_TEXT_CHARS_PER_REVIEW // 2),
            }
        )
    return rows


def _validated_results(raw_results: list[dict[str, Any]], *, expected_rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    expected_ids = [str(row["feedback_id"]) for row in expected_rows]
    expected_id_set = set(expected_ids)
    by_id: dict[str, dict[str, Any]] = {}
    for raw in raw_results:
        feedback_id = str(raw.get("feedback_id") or "").strip()
        if feedback_id not in expected_id_set:
            raise OpenAiFeedbacksAnalysisError("OpenAI returned analysis for an unexpected feedback_id")
        if feedback_id in by_id:
            raise OpenAiFeedbacksAnalysisError("OpenAI returned duplicate feedback_id in analysis")
        complaint_fit = _enum_value(raw.get("complaint_fit"), COMPLAINT_FIT_LABELS, "complaint_fit")
        category = _enum_value(raw.get("category"), CATEGORY_LABELS, "category")
        confidence = _enum_value(raw.get("confidence"), CONFIDENCE_LABELS, "confidence")
        by_id[feedback_id] = {
            "feedback_id": feedback_id,
            "complaint_fit": complaint_fit,
            "complaint_fit_label": COMPLAINT_FIT_LABELS[complaint_fit],
            "category": category,
            "category_label": CATEGORY_LABELS[category],
            "reason": _short_string(raw.get("reason"), 360) or "—",
            "confidence": confidence,
            "confidence_label": CONFIDENCE_LABELS[confidence],
            "evidence": _short_string(raw.get("evidence"), 240),
        }
    missing = [feedback_id for feedback_id in expected_ids if feedback_id not in by_id]
    if missing:
        raise OpenAiFeedbacksAnalysisError("OpenAI did not return analysis for every feedback_id")
    return [by_id[feedback_id] for feedback_id in expected_ids]


def analysis_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["results"],
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "feedback_id",
                        "complaint_fit",
                        "complaint_fit_label",
                        "category",
                        "category_label",
                        "reason",
                        "confidence",
                        "confidence_label",
                        "evidence",
                    ],
                    "properties": {
                        "feedback_id": {"type": "string"},
                        "complaint_fit": {"type": "string", "enum": list(COMPLAINT_FIT_LABELS)},
                        "complaint_fit_label": {"type": "string", "enum": list(COMPLAINT_FIT_LABELS.values())},
                        "category": {"type": "string", "enum": list(CATEGORY_LABELS)},
                        "category_label": {"type": "string", "enum": list(CATEGORY_LABELS.values())},
                        "reason": {"type": "string"},
                        "confidence": {"type": "string", "enum": list(CONFIDENCE_LABELS)},
                        "confidence_label": {"type": "string", "enum": list(CONFIDENCE_LABELS.values())},
                        "evidence": {"type": "string"},
                    },
                },
            }
        },
    }


def _enum_value(value: Any, labels: Mapping[str, str], field_name: str) -> str:
    normalized = str(value or "").strip()
    if normalized not in labels:
        raise OpenAiFeedbacksAnalysisError(f"OpenAI returned invalid {field_name}")
    return normalized


def _safe_provider_meta(meta: Mapping[str, Any], *, batch_size: int) -> dict[str, Any]:
    return {
        "model": str(meta.get("model") or ""),
        "response_id": str(meta.get("response_id") or ""),
        "batch_size": batch_size,
        "usage": meta.get("usage") if isinstance(meta.get("usage"), Mapping) else None,
    }


def _short_string(value: Any, max_length: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_length:
        return text
    return text[: max(0, max_length - 1)].rstrip() + "…"


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
