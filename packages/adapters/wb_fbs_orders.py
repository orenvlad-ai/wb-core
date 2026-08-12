"""GET-only adapter for the official Wildberries FBS orders feed."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Mapping
from urllib import error, parse as urllib_parse, request as urllib_request

from packages.adapters.official_api_runtime import (
    DEFAULT_WB_API_TOKEN_ENV,
    load_runtime_config,
)


DEFAULT_WB_FBS_API_BASE_URL = "https://marketplace-api.wildberries.ru"
DEFAULT_WB_FBS_API_BASE_URL_ENV = "WB_FBS_API_BASE_URL"
MAX_PAGE_LIMIT = 1000
MAX_WINDOW_SECONDS = 30 * 24 * 60 * 60


class WbFbsOrdersHttpStatusError(RuntimeError):
    def __init__(self, status_code: int, body: str, *, content_type: str = "") -> None:
        self.status_code = int(status_code)
        self.content_type = str(content_type or "")
        self.body_prefix = _sanitize_body_prefix(body)
        message = f"WB FBS orders API returned status {self.status_code}"
        if self.content_type:
            message += f"; content-type={self.content_type}"
        if self.body_prefix:
            message += f"; body_prefix={self.body_prefix}"
        super().__init__(message)


class WbFbsOrdersTransportError(RuntimeError):
    pass


@dataclass(frozen=True)
class WbFbsOrdersPage:
    orders: list[Mapping[str, Any]]
    next_cursor: int
    limit: int
    date_from: int | None
    date_to: int | None


class HttpBackedWbFbsOrdersSource:
    """Reads only ``GET /api/v3/orders`` from the Marketplace API."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_WB_FBS_API_BASE_URL,
        token_env_var: str = DEFAULT_WB_API_TOKEN_ENV,
        base_url_env_var: str = DEFAULT_WB_FBS_API_BASE_URL_ENV,
        timeout_seconds: float = 30.0,
        opener: Any | None = None,
    ) -> None:
        self._default_base_url = str(base_url).rstrip("/")
        self._token_env_var = token_env_var
        self._base_url_env_var = base_url_env_var
        self._default_timeout_seconds = float(timeout_seconds)
        self._opener = opener or urllib_request.urlopen

    def list_orders(
        self,
        *,
        limit: int = MAX_PAGE_LIMIT,
        next_cursor: int = 0,
        date_from: int | None = None,
        date_to: int | None = None,
    ) -> WbFbsOrdersPage:
        normalized_limit = _bounded_int(limit, "limit", minimum=1, maximum=MAX_PAGE_LIMIT)
        normalized_cursor = _bounded_int(next_cursor, "next_cursor", minimum=0, maximum=2**63 - 1)
        normalized_from = _optional_unix_timestamp(date_from, "date_from")
        normalized_to = _optional_unix_timestamp(date_to, "date_to")
        if normalized_from is not None and normalized_to is not None:
            if normalized_to < normalized_from:
                raise ValueError("date_to must be greater than or equal to date_from")
            if normalized_to - normalized_from > MAX_WINDOW_SECONDS:
                raise ValueError("FBS order window must not exceed 30 calendar days")

        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        query: dict[str, str] = {
            "limit": str(normalized_limit),
            "next": str(normalized_cursor),
        }
        if normalized_from is not None:
            query["dateFrom"] = str(normalized_from)
        if normalized_to is not None:
            query["dateTo"] = str(normalized_to)
        url = f"{runtime.base_url}/api/v3/orders?{urllib_parse.urlencode(query)}"
        request = urllib_request.Request(
            url=url,
            data=None,
            headers={"Authorization": runtime.token, "Accept": "application/json"},
            method="GET",
        )
        try:
            with self._opener(request, timeout=runtime.timeout_seconds) as response:
                status_code = _response_status(response)
                content_type = _response_content_type(response)
                raw_body = response.read().decode("utf-8", errors="replace")
        except error.HTTPError as exc:
            raw_body = exc.read().decode("utf-8", errors="replace")
            raise WbFbsOrdersHttpStatusError(
                exc.code,
                raw_body,
                content_type=_headers_content_type(exc.headers),
            ) from exc
        except (error.URLError, OSError) as exc:
            raise WbFbsOrdersTransportError(f"WB FBS orders API transport failed: {exc}") from exc

        if status_code is not None and not 200 <= status_code < 300:
            raise WbFbsOrdersHttpStatusError(status_code, raw_body, content_type=content_type)
        if not raw_body.strip():
            raise WbFbsOrdersTransportError("WB FBS orders API returned an empty response")
        if content_type and "json" not in content_type.casefold() and raw_body.lstrip()[:1] != "{":
            raise WbFbsOrdersTransportError(
                f"WB FBS orders API returned non-JSON content: {_sanitize_body_prefix(raw_body)}"
            )
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise WbFbsOrdersTransportError(
                f"WB FBS orders API returned non-JSON content: {_sanitize_body_prefix(raw_body)}"
            ) from exc
        if not isinstance(payload, Mapping) or not isinstance(payload.get("orders"), list):
            raise WbFbsOrdersTransportError("WB FBS orders API returned an invalid orders page")
        try:
            response_cursor = int(payload.get("next") or 0)
        except (TypeError, ValueError) as exc:
            raise WbFbsOrdersTransportError("WB FBS orders API returned an invalid next cursor") from exc
        if response_cursor < 0:
            raise WbFbsOrdersTransportError("WB FBS orders API returned a negative next cursor")
        orders = [item for item in payload["orders"] if isinstance(item, Mapping)]
        return WbFbsOrdersPage(
            orders=orders,
            next_cursor=response_cursor,
            limit=normalized_limit,
            date_from=normalized_from,
            date_to=normalized_to,
        )


def _bounded_int(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if result < minimum or result > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def _optional_unix_timestamp(value: Any, name: str) -> int | None:
    if value in (None, ""):
        return None
    return _bounded_int(value, name, minimum=1, maximum=2**63 - 1)


def _response_status(response: Any) -> int | None:
    value = getattr(response, "status", None)
    if value is None and callable(getattr(response, "getcode", None)):
        value = response.getcode()
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _response_content_type(response: Any) -> str:
    return _headers_content_type(getattr(response, "headers", None))


def _headers_content_type(headers: Any) -> str:
    get = getattr(headers, "get", None)
    return str(get("Content-Type") or get("content-type") or "").strip() if callable(get) else ""


def _sanitize_body_prefix(body: str, *, limit: int = 420) -> str:
    text = str(body or "").replace("\x00", "")
    text = re.sub(
        r"(?i)([\"']?(?:authorization|token|cookie|password|secret|api[-_ ]?key)[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^,\s}]+)",
        r'\1"<redacted>"',
        text,
    )
    text = re.sub(r"\b\d{8,}\b", lambda match: "***" + match.group(0)[-4:], text)
    return re.sub(r"\s+", " ", text).strip()[:limit]
