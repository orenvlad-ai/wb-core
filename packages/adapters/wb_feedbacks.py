"""Adapter boundary for official WB feedbacks endpoint."""

from __future__ import annotations

import json
import time
from typing import Any, Mapping
from urllib import error, parse as urllib_parse, request as urllib_request

from packages.adapters.official_api_runtime import DEFAULT_WB_API_TOKEN_ENV, load_runtime_config


class WbFeedbacksHttpStatusError(RuntimeError):
    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"WB feedbacks API returned status {status_code}")


class WbFeedbacksTransportError(RuntimeError):
    pass


class HttpBackedWbFeedbacksSource:
    """Read-only source for WB feedbacks through the canonical official API token."""

    def __init__(
        self,
        base_url: str = "https://feedbacks-api.wildberries.ru",
        token_env_var: str = DEFAULT_WB_API_TOKEN_ENV,
        base_url_env_var: str = "WB_FEEDBACKS_API_BASE_URL",
        timeout_seconds: float = 30.0,
        take: int = 100,
        max_pages_per_stream: int = 5,
        max_requests_per_window: int = 3,
        rate_limit_window_seconds: float = 1.0,
    ) -> None:
        self._default_base_url = base_url.rstrip("/")
        self._token_env_var = token_env_var
        self._base_url_env_var = base_url_env_var
        self._default_timeout_seconds = timeout_seconds
        self._take = take
        self._max_pages_per_stream = max_pages_per_stream
        self._max_requests_per_window = max_requests_per_window
        self._rate_limit_window_seconds = rate_limit_window_seconds

    def fetch_feedbacks(
        self,
        *,
        date_from_ts: int,
        date_to_ts: int,
        is_answered: bool,
        request_timestamps: list[float],
    ) -> list[Mapping[str, Any]]:
        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        rows: list[Mapping[str, Any]] = []
        take = max(1, int(self._take))
        max_pages = max(1, int(self._max_pages_per_stream))
        for page_index in range(max_pages):
            skip = page_index * take
            payload = self._fetch_once(
                base_url=runtime.base_url,
                token=runtime.token,
                timeout_seconds=runtime.timeout_seconds,
                date_from_ts=date_from_ts,
                date_to_ts=date_to_ts,
                is_answered=is_answered,
                take=take,
                skip=skip,
                request_timestamps=request_timestamps,
            )
            page_rows = _extract_feedback_rows(payload)
            for row in page_rows:
                row_copy = dict(row)
                row_copy["_wb_is_answered"] = is_answered
                rows.append(row_copy)
            if len(page_rows) < take:
                break
        return rows

    def _fetch_once(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float,
        date_from_ts: int,
        date_to_ts: int,
        is_answered: bool,
        take: int,
        skip: int,
        request_timestamps: list[float],
    ) -> Mapping[str, Any]:
        params = urllib_parse.urlencode(
            {
                "isAnswered": "true" if is_answered else "false",
                "take": str(take),
                "skip": str(skip),
                "order": "dateDesc",
                "dateFrom": str(date_from_ts),
                "dateTo": str(date_to_ts),
            }
        )
        req = urllib_request.Request(
            url=f"{base_url}/api/v1/feedbacks?{params}",
            method="GET",
            headers={"Authorization": token, "Accept": "application/json"},
        )
        self._wait_for_request_slot(request_timestamps)
        request_timestamps.append(self._monotonic())
        try:
            with urllib_request.urlopen(req, timeout=timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise WbFeedbacksHttpStatusError(exc.code, body) from exc
        except error.URLError as exc:
            raise WbFeedbacksTransportError(f"WB feedbacks API transport failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise WbFeedbacksTransportError("WB feedbacks API returned non-JSON response") from exc

    def _wait_for_request_slot(self, request_timestamps: list[float]) -> None:
        if self._max_requests_per_window <= 0 or self._rate_limit_window_seconds <= 0:
            return
        while True:
            now = self._monotonic()
            request_timestamps[:] = [
                timestamp
                for timestamp in request_timestamps
                if now - timestamp < self._rate_limit_window_seconds
            ]
            if len(request_timestamps) < self._max_requests_per_window:
                return
            wait_seconds = self._rate_limit_window_seconds - (now - request_timestamps[0])
            if wait_seconds <= 0:
                continue
            self._sleep(wait_seconds)

    def _sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))

    def _monotonic(self) -> float:
        return time.monotonic()


def _extract_feedback_rows(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if bool(payload.get("error")):
        detail = str(payload.get("errorText") or "unknown WB feedbacks API error")
        raise WbFeedbacksTransportError(f"WB feedbacks API returned error payload: {detail}")
    data = payload.get("data")
    if isinstance(data, Mapping):
        feedbacks = data.get("feedbacks") or data.get("items") or data.get("rows")
    else:
        feedbacks = payload.get("feedbacks") or payload.get("items") or payload.get("rows")
    if feedbacks is None:
        return []
    if not isinstance(feedbacks, list):
        raise WbFeedbacksTransportError("WB feedbacks API returned invalid feedbacks shape")
    return [item for item in feedbacks if isinstance(item, Mapping)]
