"""WB Prices and Discounts API adapter for the operator prices block."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence
from urllib import error, parse, request as urllib_request

from packages.adapters.official_api_runtime import DEFAULT_WB_API_TOKEN_ENV, load_runtime_config


SAFE_RESPONSE_HEADER_NAMES = {
    "content-type",
    "date",
    "retry-after",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "x-rate-limit-limit",
    "x-rate-limit-remaining",
    "x-rate-limit-reset",
}


@dataclass(frozen=True)
class WbPricesApiError(RuntimeError):
    """Structured non-secret WB Prices API error."""

    method: str
    url: str
    http_status: int | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    body_summary: str = ""
    retry_after_seconds: int | None = None
    transport_error: str = ""

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self._message())

    def _message(self) -> str:
        endpoint = _safe_endpoint(self.url)
        if self.http_status is not None:
            detail = f"WB Prices API {self.method} {endpoint} failed with status {self.http_status}"
            if self.body_summary:
                detail = f"{detail}: {self.body_summary}"
            return detail
        return f"WB Prices API {self.method} {endpoint} transport failed: {self.transport_error}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "endpoint": _safe_endpoint(self.url),
            "http_status": self.http_status,
            "headers": dict(self.headers),
            "body_summary": self.body_summary,
            "retry_after_seconds": self.retry_after_seconds,
            "transport_error": self.transport_error,
        }


class WbPricesManagementSource(Protocol):
    """Source boundary for WB prices management application logic."""

    def fetch_goods(
        self,
        *,
        limit: int,
        offset: int,
        filter_nm_id: int | None = None,
    ) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")

    def fetch_goods_by_nm_ids(self, nm_ids: Sequence[int]) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")

    def upload_task(self, goods: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")

    def fetch_upload_status(self, upload_id: int) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")

    def fetch_upload_goods(self, *, upload_id: int, limit: int, offset: int) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")

    def fetch_quarantine_goods(self, *, limit: int, offset: int) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")


class HttpBackedWbPricesManagementSource:
    """HTTP source for the official WB Prices and Discounts API."""

    def __init__(
        self,
        *,
        base_url: str = "https://discounts-prices-api.wildberries.ru",
        token_env_var: str = DEFAULT_WB_API_TOKEN_ENV,
        base_url_env_var: str = "WB_PRICES_API_BASE_URL",
        timeout_seconds: float = 15.0,
    ) -> None:
        self._default_base_url = base_url.rstrip("/")
        self._token_env_var = token_env_var
        self._base_url_env_var = base_url_env_var
        self._default_timeout_seconds = timeout_seconds

    def fetch_goods(
        self,
        *,
        limit: int,
        offset: int,
        filter_nm_id: int | None = None,
    ) -> Mapping[str, Any]:
        runtime = self._runtime()
        query: dict[str, str] = {"limit": str(int(limit)), "offset": str(int(offset))}
        if filter_nm_id is not None:
            query["filterNmID"] = str(int(filter_nm_id))
        return self._request_json(
            method="GET",
            url=f"{runtime.base_url}/api/v2/list/goods/filter?{parse.urlencode(query)}",
            token=runtime.token,
            timeout_seconds=runtime.timeout_seconds,
        )

    def fetch_goods_by_nm_ids(self, nm_ids: Sequence[int]) -> Mapping[str, Any]:
        runtime = self._runtime()
        return self._request_json(
            method="POST",
            url=f"{runtime.base_url}/api/v2/list/goods/filter",
            token=runtime.token,
            timeout_seconds=runtime.timeout_seconds,
            payload={"nmList": [int(value) for value in nm_ids]},
        )

    def upload_task(self, goods: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        runtime = self._runtime()
        return self._request_json(
            method="POST",
            url=f"{runtime.base_url}/api/v2/upload/task",
            token=runtime.token,
            timeout_seconds=runtime.timeout_seconds,
            payload={"data": [dict(item) for item in goods]},
        )

    def fetch_upload_status(self, upload_id: int) -> Mapping[str, Any]:
        runtime = self._runtime()
        query = parse.urlencode({"uploadID": int(upload_id)})
        return self._request_json(
            method="GET",
            url=f"{runtime.base_url}/api/v2/history/tasks?{query}",
            token=runtime.token,
            timeout_seconds=runtime.timeout_seconds,
        )

    def fetch_upload_goods(self, *, upload_id: int, limit: int, offset: int) -> Mapping[str, Any]:
        runtime = self._runtime()
        query = parse.urlencode({"uploadID": int(upload_id), "limit": int(limit), "offset": int(offset)})
        return self._request_json(
            method="GET",
            url=f"{runtime.base_url}/api/v2/history/goods/task?{query}",
            token=runtime.token,
            timeout_seconds=runtime.timeout_seconds,
        )

    def fetch_quarantine_goods(self, *, limit: int, offset: int) -> Mapping[str, Any]:
        runtime = self._runtime()
        query = parse.urlencode({"limit": int(limit), "offset": int(offset)})
        return self._request_json(
            method="GET",
            url=f"{runtime.base_url}/api/v2/quarantine/goods?{query}",
            token=runtime.token,
            timeout_seconds=runtime.timeout_seconds,
        )

    def _runtime(self):
        return load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )

    def _request_json(
        self,
        *,
        method: str,
        url: str,
        token: str,
        timeout_seconds: float,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Authorization": token}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        req = urllib_request.Request(url=url, data=body, headers=headers, method=method)
        try:
            with urllib_request.urlopen(req, timeout=timeout_seconds) as response:
                raw = response.read().decode("utf-8").strip()
                parsed = json.loads(raw) if raw else {}
                if isinstance(parsed, Mapping):
                    result = dict(parsed)
                    result["_http_status"] = int(response.status)
                    return result
                return {"data": parsed, "_http_status": int(response.status)}
        except error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            headers = _safe_headers(dict(exc.headers.items()))
            raise WbPricesApiError(
                method=method,
                url=url,
                http_status=int(exc.code),
                headers=headers,
                body_summary=_body_summary(body_text),
                retry_after_seconds=_retry_after_seconds(headers),
            ) from exc
        except error.URLError as exc:
            raise WbPricesApiError(method=method, url=url, transport_error=str(exc)) from exc


def _safe_endpoint(url: str) -> str:
    parsed = parse.urlparse(url)
    path = parsed.path or "/"
    return path if not parsed.query else f"{path}?{parsed.query}"


def _safe_headers(headers: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in headers.items():
        normalized = str(key).strip().lower()
        if normalized in SAFE_RESPONSE_HEADER_NAMES or "ratelimit" in normalized or "rate-limit" in normalized:
            result[str(key)] = str(value)
    return result


def _body_summary(body: str) -> str:
    normalized = " ".join(str(body or "").replace("\x00", "").split())
    return normalized[:800]


def _retry_after_seconds(headers: Mapping[str, str]) -> int | None:
    for key, value in headers.items():
        if str(key).strip().lower() != "retry-after":
            continue
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return max(0, parsed)
    return None
