"""Detail-preserving adapter for WB Promotion API."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import time
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
    "x-ratelimit-retry",
    "x-rate-limit-limit",
    "x-rate-limit-remaining",
    "x-rate-limit-reset",
    "x-rate-limit-retry",
}


@dataclass(frozen=True)
class WbPromotionApiError(RuntimeError):
    """Structured non-secret WB Promotion API error."""

    method: str
    url: str
    http_status: int | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    body_summary: str = ""
    retry_after_seconds: float | None = None
    transport_error: str = ""

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self._message())

    def _message(self) -> str:
        endpoint = _safe_endpoint(self.url)
        if self.http_status is not None:
            detail = (
                f"WB Promotion API {self.method} {endpoint} failed with status "
                f"{self.http_status}"
            )
            if self.body_summary:
                detail = f"{detail}: {self.body_summary}"
            return detail
        return (
            f"WB Promotion API {self.method} {endpoint} transport failed: "
            f"{self.transport_error}"
        )

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


class WbPromotionSource(Protocol):
    """WB Promotion source boundary used by the SKU-first ads application block."""

    def fetch_campaign_count(self) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")

    def fetch_adverts(
        self,
        advert_ids: Sequence[int],
        *,
        statuses: Sequence[int] | None = None,
        payment_type: str = "",
    ) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")

    def fetch_min_bids(
        self,
        *,
        advert_id: int,
        nm_ids: Sequence[int],
        payment_type: str,
        placement_types: Sequence[str],
    ) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")

    def fetch_recommendations(self, *, advert_id: int, nm_id: int) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")

    def fetch_fullstats(
        self,
        advert_ids: Sequence[int],
        *,
        begin_date: str,
        end_date: str,
    ) -> Any:
        raise NotImplementedError("adapter skeleton only")

    def patch_bids(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")


class HttpBackedWbPromotionSource:
    """HTTP adapter to official WB Promotion API.

    The application layer is responsible for all safety checks before calling
    ``patch_bids``.
    """

    def __init__(
        self,
        *,
        base_url: str = "https://advert-api.wildberries.ru",
        token_env_var: str = DEFAULT_WB_API_TOKEN_ENV,
        base_url_env_var: str = "WB_ADVERT_API_BASE_URL",
        timeout_seconds: float = 30.0,
        max_ids_per_request: int = 50,
        batch_sleep_seconds: float = 0.25,
    ) -> None:
        self._default_base_url = base_url.rstrip("/")
        self._token_env_var = token_env_var
        self._base_url_env_var = base_url_env_var
        self._default_timeout_seconds = timeout_seconds
        self._max_ids_per_request = max_ids_per_request
        self._batch_sleep_seconds = batch_sleep_seconds

    def fetch_campaign_count(self) -> Mapping[str, Any]:
        runtime = self._runtime()
        payload = self._request_json(
            method="GET",
            url=f"{runtime.base_url}/adv/v1/promotion/count",
            token=runtime.token,
            timeout_seconds=runtime.timeout_seconds,
        )
        return payload if isinstance(payload, Mapping) else {}

    def fetch_adverts(
        self,
        advert_ids: Sequence[int],
        *,
        statuses: Sequence[int] | None = None,
        payment_type: str = "",
    ) -> Mapping[str, Any]:
        runtime = self._runtime()
        adverts: list[Mapping[str, Any]] = []
        ids = [int(value) for value in advert_ids if _is_int_like(value)]
        batches = _chunks(ids, self._max_ids_per_request)
        for index, batch in enumerate(batches):
            params: dict[str, str] = {"ids": ",".join(str(value) for value in batch)}
            if statuses:
                params["statuses"] = ",".join(str(int(value)) for value in statuses)
            if str(payment_type or "").strip():
                params["payment_type"] = str(payment_type).strip()
            payload = self._request_json(
                method="GET",
                url=f"{runtime.base_url}/api/advert/v2/adverts?{parse.urlencode(params)}",
                token=runtime.token,
                timeout_seconds=runtime.timeout_seconds,
            )
            raw_adverts = payload.get("adverts") if isinstance(payload, Mapping) else None
            if isinstance(raw_adverts, list):
                adverts.extend(item for item in raw_adverts if isinstance(item, Mapping))
            if index < len(batches) - 1:
                time.sleep(self._batch_sleep_seconds)
        return {"adverts": adverts}

    def fetch_min_bids(
        self,
        *,
        advert_id: int,
        nm_ids: Sequence[int],
        payment_type: str,
        placement_types: Sequence[str],
    ) -> Mapping[str, Any]:
        runtime = self._runtime()
        payload = {
            "advert_id": int(advert_id),
            "nm_ids": [int(value) for value in nm_ids if _is_int_like(value)],
            "payment_type": str(payment_type or "").strip(),
            "placement_types": [str(value) for value in placement_types],
        }
        result = self._request_json(
            method="POST",
            url=f"{runtime.base_url}/api/advert/v1/bids/min",
            token=runtime.token,
            timeout_seconds=runtime.timeout_seconds,
            payload=payload,
        )
        return result if isinstance(result, Mapping) else {}

    def fetch_recommendations(self, *, advert_id: int, nm_id: int) -> Mapping[str, Any]:
        runtime = self._runtime()
        query = parse.urlencode({"advertId": int(advert_id), "nmId": int(nm_id)})
        payload = self._request_json(
            method="GET",
            url=f"{runtime.base_url}/api/advert/v0/bids/recommendations?{query}",
            token=runtime.token,
            timeout_seconds=runtime.timeout_seconds,
        )
        return payload if isinstance(payload, Mapping) else {}

    def fetch_fullstats(
        self,
        advert_ids: Sequence[int],
        *,
        begin_date: str,
        end_date: str,
    ) -> Any:
        runtime = self._runtime()
        stats: list[Any] = []
        ids = [int(value) for value in advert_ids if _is_int_like(value)]
        batches = _chunks(ids, self._max_ids_per_request)
        for index, batch in enumerate(batches):
            query = parse.urlencode(
                {
                    "ids": ",".join(str(value) for value in batch),
                    "beginDate": begin_date,
                    "endDate": end_date,
                }
            )
            payload = self._request_json(
                method="GET",
                url=f"{runtime.base_url}/adv/v3/fullstats?{query}",
                token=runtime.token,
                timeout_seconds=runtime.timeout_seconds,
            )
            if isinstance(payload, list):
                stats.extend(payload)
            if index < len(batches) - 1:
                time.sleep(self._batch_sleep_seconds)
        return stats

    def patch_bids(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        runtime = self._runtime()
        result = self._request_json(
            method="PATCH",
            url=f"{runtime.base_url}/api/advert/v1/bids",
            token=runtime.token,
            timeout_seconds=runtime.timeout_seconds,
            payload=dict(payload),
        )
        return result if isinstance(result, Mapping) else {}

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
    ) -> Any:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Authorization": token}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        req = urllib_request.Request(url=url, data=body, headers=headers, method=method)
        try:
            with urllib_request.urlopen(req, timeout=timeout_seconds) as response:
                raw = response.read().decode("utf-8").strip()
                return json.loads(raw) if raw else {}
        except error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            safe_headers = _safe_headers(dict(exc.headers.items()))
            raise WbPromotionApiError(
                method=method,
                url=url,
                http_status=int(exc.code),
                headers=safe_headers,
                body_summary=_body_summary(body_text),
                retry_after_seconds=_retry_after_seconds(safe_headers),
            ) from exc
        except error.URLError as exc:
            raise WbPromotionApiError(
                method=method,
                url=url,
                transport_error=str(exc),
            ) from exc


def extract_advert_ids_from_count(payload: Mapping[str, Any]) -> list[int]:
    advert_ids: set[int] = set()
    groups = payload.get("adverts") if isinstance(payload, Mapping) else None
    if not isinstance(groups, list):
        return []
    for group in groups:
        if not isinstance(group, Mapping):
            continue
        advert_list = group.get("advert_list") or group.get("advertList")
        if not isinstance(advert_list, list):
            continue
        for advert in advert_list:
            if not isinstance(advert, Mapping):
                continue
            advert_id = advert.get("advertId") or advert.get("advert_id") or advert.get("id")
            if _is_int_like(advert_id):
                advert_ids.add(int(advert_id))
    return sorted(advert_ids)


def _chunks(values: Sequence[int], size: int) -> list[list[int]]:
    if size <= 0:
        return [list(values)]
    return [list(values[index : index + size]) for index in range(0, len(values), size)]


def _is_int_like(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        int(value)
    except (TypeError, ValueError):
        return False
    return True


def _safe_endpoint(url: str) -> str:
    parsed = parse.urlparse(url)
    path = parsed.path or "/"
    return path if not parsed.query else f"{path}?{parsed.query}"


def _safe_headers(headers: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in headers.items():
        normalized = str(key).strip().lower()
        if (
            normalized in SAFE_RESPONSE_HEADER_NAMES
            or "ratelimit" in normalized
            or "rate-limit" in normalized
        ):
            result[str(key)] = str(value)
    return result


def _body_summary(body: str) -> str:
    normalized = " ".join(str(body or "").replace("\x00", "").split())
    return normalized[:800]


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    normalized = {
        str(key).strip().lower(): str(value).strip()
        for key, value in headers.items()
    }
    for name in (
        "retry-after",
        "x-ratelimit-retry",
        "x-rate-limit-retry",
        "x-ratelimit-reset",
        "x-rate-limit-reset",
    ):
        raw_value = normalized.get(name, "")
        if not raw_value:
            continue
        try:
            parsed = float(raw_value)
        except (TypeError, ValueError):
            if name != "retry-after":
                continue
            try:
                retry_at = parsedate_to_datetime(raw_value)
            except (TypeError, ValueError, OverflowError):
                continue
            if retry_at.tzinfo is None or retry_at.utcoffset() is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        if parsed >= 0:
            return parsed
    return None
