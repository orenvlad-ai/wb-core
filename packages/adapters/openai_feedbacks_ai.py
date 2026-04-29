"""OpenAI adapter for feedback complaint-fit analysis."""

from __future__ import annotations

import json
import os
from typing import Any, Mapping
from urllib import error, request as urllib_request


DEFAULT_OPENAI_MODEL = "gpt-5-mini"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com"
DEFAULT_OPENAI_TIMEOUT_SECONDS = 60.0


class OpenAiFeedbacksAnalysisError(RuntimeError):
    pass


class OpenAiFeedbacksAnalysisProvider:
    """Narrow HTTP client for the feedbacks AI flow.

    The adapter intentionally avoids SDK/runtime dependency changes. It reads the
    OpenAI key only at request time and never logs or returns it.
    """

    def __init__(
        self,
        *,
        api_key_env_var: str = "OPENAI_API_KEY",
        model_env_var: str = "OPENAI_MODEL",
        base_url_env_var: str = "OPENAI_API_BASE_URL",
        timeout_env_var: str = "OPENAI_TIMEOUT_SECONDS",
        default_model: str = DEFAULT_OPENAI_MODEL,
        default_base_url: str = DEFAULT_OPENAI_BASE_URL,
        default_timeout_seconds: float = DEFAULT_OPENAI_TIMEOUT_SECONDS,
    ) -> None:
        self.api_key_env_var = api_key_env_var
        self.model_env_var = model_env_var
        self.base_url_env_var = base_url_env_var
        self.timeout_env_var = timeout_env_var
        self.default_model = default_model
        self.default_base_url = default_base_url.rstrip("/")
        self.default_timeout_seconds = default_timeout_seconds

    def analyze_batch(
        self,
        *,
        prompt: str,
        model: str | None = None,
        rows: list[Mapping[str, Any]],
        schema: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        api_key = self._api_key()
        resolved_model = str(model or "").strip() or self._model()
        payload = self._build_payload(
            prompt=prompt,
            rows=rows,
            schema=schema,
            model=resolved_model,
            include_temperature=True,
        )
        try:
            response_payload = self._post_response(payload, api_key=api_key)
        except OpenAiFeedbacksAnalysisError as exc:
            if "temperature" not in str(exc).lower():
                raise
            response_payload = self._post_response(
                self._build_payload(
                    prompt=prompt,
                    rows=rows,
                    schema=schema,
                    model=resolved_model,
                    include_temperature=False,
                ),
                api_key=api_key,
            )
        text = _extract_response_text(response_payload)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OpenAiFeedbacksAnalysisError("OpenAI returned non-JSON analysis output") from exc
        if not isinstance(parsed, Mapping) or not isinstance(parsed.get("results"), list):
            raise OpenAiFeedbacksAnalysisError("OpenAI analysis output did not match expected shape")
        return [dict(item) for item in parsed["results"] if isinstance(item, Mapping)], {
            "model": resolved_model,
            "response_id": str(response_payload.get("id") or ""),
            "usage": response_payload.get("usage") if isinstance(response_payload.get("usage"), Mapping) else None,
        }

    def list_models(self) -> list[str]:
        api_key = self._api_key()
        request = urllib_request.Request(
            url=f"{self._base_url()}/v1/models",
            method="GET",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib_request.urlopen(request, timeout=self._timeout_seconds()) as response:
                decoded = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = _friendly_openai_http_error(exc.code, exc.read().decode("utf-8", errors="replace"))
            raise OpenAiFeedbacksAnalysisError(detail) from exc
        except error.URLError as exc:
            raise OpenAiFeedbacksAnalysisError(f"OpenAI API transport failed: {exc}") from exc
        try:
            payload = json.loads(decoded)
        except json.JSONDecodeError as exc:
            raise OpenAiFeedbacksAnalysisError("OpenAI models endpoint returned non-JSON response") from exc
        if not isinstance(payload, Mapping):
            raise OpenAiFeedbacksAnalysisError("OpenAI models endpoint returned invalid response shape")
        if payload.get("error"):
            raise OpenAiFeedbacksAnalysisError(_friendly_openai_error_payload(payload.get("error")))
        data = payload.get("data")
        if not isinstance(data, list):
            raise OpenAiFeedbacksAnalysisError("OpenAI models endpoint did not return a model list")
        model_ids = sorted(
            {
                str(item.get("id") or "").strip()
                for item in data
                if isinstance(item, Mapping) and str(item.get("id") or "").strip()
            }
        )
        if not model_ids:
            raise OpenAiFeedbacksAnalysisError("OpenAI models endpoint returned an empty model list")
        return model_ids

    def _build_payload(
        self,
        *,
        prompt: str,
        rows: list[Mapping[str, Any]],
        schema: Mapping[str, Any],
        model: str,
        include_temperature: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "input": [
                {
                    "role": "developer",
                    "content": prompt,
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "task": "classify_feedbacks_for_possible_marketplace_complaint",
                            "reviews": rows,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "feedbacks_complaint_fit_analysis",
                    "strict": True,
                    "schema": schema,
                }
            },
            "max_output_tokens": 6000,
        }
        if include_temperature:
            payload["temperature"] = 0
        return payload

    def _post_response(self, payload: Mapping[str, Any], *, api_key: str) -> Mapping[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib_request.Request(
            url=f"{self._base_url()}/v1/responses",
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
            },
            data=body,
        )
        try:
            with urllib_request.urlopen(request, timeout=self._timeout_seconds()) as response:
                decoded = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = _friendly_openai_http_error(exc.code, exc.read().decode("utf-8", errors="replace"))
            raise OpenAiFeedbacksAnalysisError(detail) from exc
        except error.URLError as exc:
            raise OpenAiFeedbacksAnalysisError(f"OpenAI API transport failed: {exc}") from exc
        try:
            payload = json.loads(decoded)
        except json.JSONDecodeError as exc:
            raise OpenAiFeedbacksAnalysisError("OpenAI API returned non-JSON response") from exc
        if not isinstance(payload, Mapping):
            raise OpenAiFeedbacksAnalysisError("OpenAI API returned invalid response shape")
        if payload.get("error"):
            raise OpenAiFeedbacksAnalysisError(_friendly_openai_error_payload(payload.get("error")))
        return payload

    def _api_key(self) -> str:
        value = os.environ.get(self.api_key_env_var, "").strip()
        if not value:
            raise OpenAiFeedbacksAnalysisError(f"required env {self.api_key_env_var} is not set")
        return value

    def _model(self) -> str:
        return os.environ.get(self.model_env_var, "").strip() or self.default_model

    def _base_url(self) -> str:
        return (os.environ.get(self.base_url_env_var, "").strip() or self.default_base_url).rstrip("/")

    def _timeout_seconds(self) -> float:
        raw = os.environ.get(self.timeout_env_var, "").strip()
        if not raw:
            return self.default_timeout_seconds
        try:
            timeout = float(raw)
        except ValueError as exc:
            raise OpenAiFeedbacksAnalysisError(f"{self.timeout_env_var} must be numeric seconds") from exc
        if timeout <= 0:
            raise OpenAiFeedbacksAnalysisError(f"{self.timeout_env_var} must be > 0")
        return timeout


def _extract_response_text(payload: Mapping[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    fragments: list[str] = []
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, Mapping):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for content_item in content:
                if not isinstance(content_item, Mapping):
                    continue
                text = content_item.get("text")
                if isinstance(text, str):
                    fragments.append(text)
    text = "".join(fragments).strip()
    if not text:
        raise OpenAiFeedbacksAnalysisError("OpenAI response did not contain output text")
    return text


def _friendly_openai_http_error(status_code: int, body: str) -> str:
    if status_code == 401:
        return "OpenAI API rejected OPENAI_API_KEY: unauthorized"
    if status_code == 403:
        return "OpenAI API access is forbidden for the configured key/model"
    if status_code == 429:
        return "OpenAI API rate limit returned 429; retry later"
    if status_code >= 500:
        return f"OpenAI API is unavailable: status {status_code}"
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return f"OpenAI API request failed with status {status_code}"
    return _friendly_openai_error_payload(payload.get("error")) if isinstance(payload, Mapping) else f"OpenAI API request failed with status {status_code}"


def _friendly_openai_error_payload(error_payload: Any) -> str:
    if isinstance(error_payload, Mapping):
        message = str(error_payload.get("message") or "").strip()
        code = str(error_payload.get("code") or error_payload.get("type") or "").strip()
        if message and code:
            return f"OpenAI API error ({code}): {message}"
        if message:
            return f"OpenAI API error: {message}"
    return "OpenAI API returned an error"
