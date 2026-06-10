"""Adapter boundary for official WB FBW Supplies endpoints."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping
from urllib import error, parse as urllib_parse, request as urllib_request

from packages.adapters.official_api_runtime import DEFAULT_WB_API_TOKEN_ENV, load_runtime_config


DEFAULT_WB_SUPPLIES_API_BASE_URL = "https://supplies-api.wildberries.ru"
DEFAULT_WB_SUPPLIES_API_BASE_URL_ENV = "WB_SUPPLIES_API_BASE_URL"


class WbSuppliesHttpStatusError(RuntimeError):
    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = int(status_code)
        self.body = body
        super().__init__(f"WB supplies API returned status {status_code}")


class WbSuppliesTransportError(RuntimeError):
    pass


@dataclass(frozen=True)
class WbSuppliesListResult:
    rows: list[Mapping[str, Any]]
    raw_count: int
    limit: int
    offset: int
    status_ids: list[int]
    dates: list[Mapping[str, Any]]


class HttpBackedWbSuppliesSource:
    """Read-only source for WB FBW supplies through the canonical official API token."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_WB_SUPPLIES_API_BASE_URL,
        token_env_var: str = DEFAULT_WB_API_TOKEN_ENV,
        base_url_env_var: str = DEFAULT_WB_SUPPLIES_API_BASE_URL_ENV,
        timeout_seconds: float = 30.0,
        opener: Any | None = None,
    ) -> None:
        self._default_base_url = base_url.rstrip("/")
        self._token_env_var = token_env_var
        self._base_url_env_var = base_url_env_var
        self._default_timeout_seconds = timeout_seconds
        self._opener = opener or urllib_request.urlopen

    def list_supplies(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        status_ids: list[int] | None = None,
        dates: list[Mapping[str, Any]] | None = None,
    ) -> WbSuppliesListResult:
        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        normalized_limit = _bounded_int(limit, default=100, minimum=1, maximum=1000)
        normalized_offset = max(0, int(offset or 0))
        normalized_status_ids = _normalize_status_ids(status_ids)
        normalized_dates = [dict(item) for item in dates or [] if isinstance(item, Mapping)]
        body: dict[str, Any] = {"dates": normalized_dates}
        if normalized_status_ids:
            body["statusIDs"] = normalized_status_ids
        query = urllib_parse.urlencode({"limit": str(normalized_limit), "offset": str(normalized_offset)})
        payload = self._request_json(
            method="POST",
            url=f"{runtime.base_url}/api/v1/supplies?{query}",
            token=runtime.token,
            timeout_seconds=runtime.timeout_seconds,
            body=body,
        )
        rows = _extract_list_rows(payload, row_name="supplies")
        return WbSuppliesListResult(
            rows=rows,
            raw_count=len(rows),
            limit=normalized_limit,
            offset=normalized_offset,
            status_ids=normalized_status_ids,
            dates=normalized_dates,
        )

    def fetch_supply_details(self, supply_id: int | str, *, is_preorder_id: bool = False) -> Mapping[str, Any]:
        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        query = urllib_parse.urlencode({"isPreorderID": "true" if is_preorder_id else "false"})
        payload = self._request_json(
            method="GET",
            url=f"{runtime.base_url}/api/v1/supplies/{urllib_parse.quote(str(supply_id), safe='')}?{query}",
            token=runtime.token,
            timeout_seconds=runtime.timeout_seconds,
        )
        if not isinstance(payload, Mapping):
            raise WbSuppliesTransportError("WB supplies details endpoint returned invalid JSON shape")
        return payload

    def fetch_supply_goods(
        self,
        supply_id: int | str,
        *,
        limit: int = 1000,
        offset: int = 0,
        is_preorder_id: bool = False,
    ) -> list[Mapping[str, Any]]:
        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        query = urllib_parse.urlencode(
            {
                "limit": str(_bounded_int(limit, default=1000, minimum=1, maximum=1000)),
                "offset": str(max(0, int(offset or 0))),
                "isPreorderID": "true" if is_preorder_id else "false",
            }
        )
        payload = self._request_json(
            method="GET",
            url=f"{runtime.base_url}/api/v1/supplies/{urllib_parse.quote(str(supply_id), safe='')}/goods?{query}",
            token=runtime.token,
            timeout_seconds=runtime.timeout_seconds,
        )
        return _extract_list_rows(payload, row_name="goods")

    def fetch_supply_package(self, supply_id: int | str) -> list[Mapping[str, Any]]:
        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        payload = self._request_json(
            method="GET",
            url=f"{runtime.base_url}/api/v1/supplies/{urllib_parse.quote(str(supply_id), safe='')}/package",
            token=runtime.token,
            timeout_seconds=runtime.timeout_seconds,
        )
        return _extract_list_rows(payload, row_name="package")

    def fetch_transit_tariffs(self) -> list[Mapping[str, Any]]:
        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        payload = self._request_json(
            method="GET",
            url=f"{runtime.base_url}/api/v1/transit-tariffs",
            token=runtime.token,
            timeout_seconds=runtime.timeout_seconds,
        )
        return _extract_list_rows(payload, row_name="transit_tariffs")

    def fetch_warehouses(self) -> list[Mapping[str, Any]]:
        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        payload = self._request_json(
            method="GET",
            url=f"{runtime.base_url}/api/v1/warehouses",
            token=runtime.token,
            timeout_seconds=runtime.timeout_seconds,
        )
        return _extract_list_rows(payload, row_name="warehouses")

    def _request_json(
        self,
        *,
        method: str,
        url: str,
        token: str,
        timeout_seconds: float,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        data = None
        headers = {"Authorization": token, "Accept": "application/json"}
        if body is not None:
            data = json.dumps(dict(body), ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib_request.Request(url=url, data=data, headers=headers, method=method)
        try:
            with self._opener(req, timeout=timeout_seconds) as response:
                raw_body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise WbSuppliesHttpStatusError(exc.code, body_text) from exc
        except error.URLError as exc:
            raise WbSuppliesTransportError(f"WB supplies API transport failed: {exc}") from exc
        except OSError as exc:
            raise WbSuppliesTransportError(f"WB supplies API transport failed: {exc}") from exc

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise WbSuppliesTransportError("WB supplies API returned non-JSON response") from exc
        if isinstance(payload, Mapping) and bool(payload.get("error")):
            detail = str(payload.get("errorText") or payload.get("message") or "unknown WB supplies API error")
            raise WbSuppliesTransportError(f"WB supplies API returned error payload: {detail}")
        return payload


def _normalize_status_ids(values: list[int] | None) -> list[int]:
    normalized: list[int] = []
    for value in values or []:
        try:
            status_id = int(value)
        except (TypeError, ValueError):
            continue
        if status_id > 0 and status_id not in normalized:
            normalized.append(status_id)
    return normalized


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = int(default)
    return min(max(normalized, minimum), maximum)


def _extract_list_rows(payload: Any, *, row_name: str) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        for key in (row_name, "data", "items", "rows", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, Mapping)]
            if isinstance(value, Mapping):
                nested = _extract_list_rows(value, row_name=row_name)
                if nested:
                    return nested
    raise WbSuppliesTransportError(f"WB supplies API returned invalid {row_name} shape")
