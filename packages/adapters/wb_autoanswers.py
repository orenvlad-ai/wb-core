"""Read-only and write-port boundaries for server-native WB autoanswers.

The concrete read adapter implements only official Feedbacks API GET calls.
The writer is an interface implemented separately so ordinary sync/UI code has
no capability to publish an answer.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any, Mapping, Protocol
from urllib import error, parse as urllib_parse, request as urllib_request

from packages.adapters.official_api_runtime import DEFAULT_WB_API_TOKEN_ENV, load_runtime_config


OFFICIAL_FEEDBACKS_API_BASE_URL = "https://feedbacks-api.wildberries.ru"


def _retry_after_seconds(headers: Mapping[str, Any] | None) -> int | None:
    if not headers:
        return None
    values: list[float] = []
    for name in ("Retry-After", "X-Ratelimit-Retry"):
        try:
            value = float(str(headers.get(name) or "").strip())
        except ValueError:
            continue
        if value > 0:
            values.append(value)
    try:
        reset_at = float(str(headers.get("X-Ratelimit-Reset") or "").strip())
    except ValueError:
        reset_at = 0
    if reset_at > 0:
        import time

        values.append(max(0.0, reset_at - time.time()))
    return int(math.ceil(max(values))) if values else None


class WbAutoanswersHttpError(RuntimeError):
    def __init__(self, status_code: int, body: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(f"WB Feedbacks API returned HTTP {status_code}")
        self.status_code = int(status_code)
        self.body = body
        self.retry_after_seconds = retry_after_seconds


class WbAutoanswersTransportError(RuntimeError):
    pass


@dataclass(frozen=True)
class FeedbackPage:
    rows: list[Mapping[str, Any]]
    take: int
    skip: int
    has_more: bool


class WbFeedbackReadPort(Protocol):
    def fetch_feedbacks_page(
        self,
        *,
        date_from_ts: int,
        date_to_ts: int,
        is_answered: bool,
        take: int,
        skip: int,
    ) -> FeedbackPage: ...

    def fetch_archive_page(self, *, take: int, skip: int) -> FeedbackPage: ...

    def fetch_detail(self, feedback_id: str) -> Mapping[str, Any] | None: ...

    def count_unanswered(self) -> int: ...


class WbAnswerWritePort(Protocol):
    """Capability boundary; implementations may perform exactly POST answer + GET detail."""

    def create_answer(self, *, feedback_id: str, text: str) -> int: ...

    def fetch_detail(self, feedback_id: str) -> Mapping[str, Any] | None: ...


class HttpBackedWbAutoanswersReadAdapter:
    """Official WB Feedbacks API GET adapter.  It contains no write method."""

    def __init__(
        self,
        *,
        base_url: str = OFFICIAL_FEEDBACKS_API_BASE_URL,
        token_env_var: str = DEFAULT_WB_API_TOKEN_ENV,
        base_url_env_var: str = "WB_FEEDBACKS_API_BASE_URL",
        timeout_seconds: float = 30.0,
    ) -> None:
        self.default_base_url = base_url.rstrip("/")
        self.token_env_var = token_env_var
        self.base_url_env_var = base_url_env_var
        self.timeout_seconds = timeout_seconds

    def fetch_feedbacks_page(
        self,
        *,
        date_from_ts: int,
        date_to_ts: int,
        is_answered: bool,
        take: int,
        skip: int,
    ) -> FeedbackPage:
        bounded_take = min(5000, max(1, int(take)))
        bounded_skip = max(0, int(skip))
        payload = self._get(
            "/api/v1/feedbacks",
            {
                "isAnswered": "true" if is_answered else "false",
                "take": bounded_take,
                "skip": bounded_skip,
                "order": "dateDesc",
                "dateFrom": int(date_from_ts),
                "dateTo": int(date_to_ts),
            },
        )
        rows = _feedback_rows(payload)
        return FeedbackPage(rows=rows, take=bounded_take, skip=bounded_skip, has_more=len(rows) == bounded_take)

    def fetch_archive_page(self, *, take: int, skip: int) -> FeedbackPage:
        bounded_take = min(5000, max(1, int(take)))
        bounded_skip = max(0, int(skip))
        payload = self._get(
            "/api/v1/feedbacks/archive",
            {"take": bounded_take, "skip": bounded_skip, "order": "dateDesc"},
        )
        rows = _feedback_rows(payload)
        return FeedbackPage(rows=rows, take=bounded_take, skip=bounded_skip, has_more=len(rows) == bounded_take)

    def fetch_detail(self, feedback_id: str) -> Mapping[str, Any] | None:
        payload = self._get("/api/v1/feedback", {"id": str(feedback_id).strip()})
        data = payload.get("data")
        record = data.get("feedback") if isinstance(data, Mapping) else None
        if record is None and isinstance(data, Mapping) and data.get("id"):
            record = data
        if record is None:
            record = payload.get("feedback")
        return dict(record) if isinstance(record, Mapping) else None

    def count_unanswered(self) -> int:
        payload = self._get("/api/v1/feedbacks/count-unanswered", {})
        data = payload.get("data")
        if isinstance(data, Mapping):
            value = data.get("countUnanswered") if data.get("countUnanswered") is not None else data.get("count")
        else:
            value = payload.get("countUnanswered") or payload.get("count")
        return int(value or 0)

    def _get(self, path: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        runtime = load_runtime_config(
            token_env_var=self.token_env_var,
            default_base_url=self.default_base_url,
            base_url_env_var=self.base_url_env_var,
            default_timeout_seconds=self.timeout_seconds,
        )
        query = urllib_parse.urlencode({key: value for key, value in params.items() if value is not None})
        url = f"{runtime.base_url}{path}" + (f"?{query}" if query else "")
        req = urllib_request.Request(
            url=url,
            method="GET",
            headers={"Authorization": runtime.token, "Accept": "application/json"},
        )
        try:
            with urllib_request.urlopen(req, timeout=runtime.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise WbAutoanswersHttpError(
                exc.code,
                body,
                retry_after_seconds=_retry_after_seconds(exc.headers),
            ) from exc
        except (error.URLError, TimeoutError) as exc:
            raise WbAutoanswersTransportError("WB Feedbacks API transport failed") from exc
        except json.JSONDecodeError as exc:
            raise WbAutoanswersTransportError("WB Feedbacks API returned non-JSON response") from exc
        if not isinstance(payload, Mapping):
            raise WbAutoanswersTransportError("WB Feedbacks API returned invalid JSON shape")
        if bool(payload.get("error")):
            raise WbAutoanswersTransportError(str(payload.get("errorText") or "WB error payload"))
        return payload


class HttpBackedWbAnswerWriter(HttpBackedWbAutoanswersReadAdapter):
    """Narrow write capability: POST create-answer plus inherited GET detail.

    PATCH is deliberately absent in v1.
    """

    def create_answer(self, *, feedback_id: str, text: str) -> int:
        runtime = load_runtime_config(
            token_env_var=self.token_env_var,
            default_base_url=self.default_base_url,
            base_url_env_var=self.base_url_env_var,
            default_timeout_seconds=self.timeout_seconds,
        )
        body = json.dumps(
            {"id": str(feedback_id).strip(), "text": str(text)},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        req = urllib_request.Request(
            url=f"{runtime.base_url}/api/v1/feedbacks/answer",
            method="POST",
            headers={
                "Authorization": runtime.token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            data=body,
        )
        try:
            with urllib_request.urlopen(req, timeout=runtime.timeout_seconds) as response:
                response.read()
                return int(response.status)
        except error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            raise WbAutoanswersHttpError(
                exc.code,
                response_body,
                retry_after_seconds=_retry_after_seconds(exc.headers),
            ) from exc
        except (error.URLError, TimeoutError) as exc:
            raise WbAutoanswersTransportError("WB answer transport result is ambiguous") from exc


def _feedback_rows(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    data = payload.get("data")
    values = data.get("feedbacks") if isinstance(data, Mapping) else None
    if values is None:
        values = payload.get("feedbacks")
    if values is None:
        return []
    if not isinstance(values, list):
        raise WbAutoanswersTransportError("WB feedback collection has invalid shape")
    return [dict(item) for item in values if isinstance(item, Mapping)]
