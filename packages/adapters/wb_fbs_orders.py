"""Read-only adapter for official WB FBS orders and status observations."""

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


@dataclass(frozen=True)
class WbFbsOrderStatus:
    order_id: int
    supplier_status: str
    wb_status: str


@dataclass(frozen=True)
class WbFbsSellerWarehouse:
    """Privacy-safe exact identity from the official seller warehouse list."""

    warehouse_id: int
    office_id: int
    name: str
    cargo_type: int | None
    delivery_type: int | None
    is_deleting: bool
    is_processing: bool


@dataclass(frozen=True)
class WbFbsOffice:
    """Exact official WB office identity used only for facility evidence."""

    office_id: int
    name: str
    city: str
    federal_district: str


class HttpBackedWbFbsOrdersSource:
    """Uses GET orders plus POST status strictly as official read semantics."""

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

    def list_seller_warehouses(self) -> list[WbFbsSellerWarehouse]:
        """Read the official seller warehouse registry without changing WB."""

        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        request = urllib_request.Request(
            url=f"{runtime.base_url}/api/v3/warehouses",
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
                exc.code, raw_body, content_type=_headers_content_type(exc.headers)
            ) from exc
        except (error.URLError, OSError) as exc:
            raise WbFbsOrdersTransportError(
                f"WB FBS seller warehouse API transport failed: {exc}"
            ) from exc
        if status_code is not None and not 200 <= status_code < 300:
            raise WbFbsOrdersHttpStatusError(status_code, raw_body, content_type=content_type)
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise WbFbsOrdersTransportError(
                "WB FBS seller warehouse API returned invalid JSON"
            ) from exc
        if not isinstance(payload, list):
            raise WbFbsOrdersTransportError(
                "WB FBS seller warehouse API returned an invalid warehouse list"
            )
        result: list[WbFbsSellerWarehouse] = []
        seen: set[int] = set()
        for item in payload:
            if not isinstance(item, Mapping):
                continue
            warehouse_id = _bounded_int(
                item.get("id"), "seller warehouse_id", minimum=1, maximum=2**63 - 1
            )
            office_id = _bounded_int(
                item.get("officeId"), "seller warehouse office_id", minimum=1, maximum=2**63 - 1
            )
            if warehouse_id in seen:
                raise WbFbsOrdersTransportError(
                    "WB FBS seller warehouse API returned a duplicate warehouse ID"
                )
            seen.add(warehouse_id)
            result.append(
                WbFbsSellerWarehouse(
                    warehouse_id=warehouse_id,
                    office_id=office_id,
                    name=str(item.get("name") or "")[:200],
                    cargo_type=_optional_int(item.get("cargoType")),
                    delivery_type=_optional_int(item.get("deliveryType")),
                    is_deleting=item.get("isDeleting") is True,
                    is_processing=item.get("isProcessing") is True,
                )
            )
        return sorted(result, key=lambda item: item.warehouse_id)

    def list_offices(self) -> list[WbFbsOffice]:
        """Read official offices so warehouse→city evidence remains ID-bound."""

        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        request = urllib_request.Request(
            url=f"{runtime.base_url}/api/v3/offices",
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
                exc.code, raw_body, content_type=_headers_content_type(exc.headers)
            ) from exc
        except (error.URLError, OSError) as exc:
            raise WbFbsOrdersTransportError(f"WB FBS offices API transport failed: {exc}") from exc
        if status_code is not None and not 200 <= status_code < 300:
            raise WbFbsOrdersHttpStatusError(status_code, raw_body, content_type=content_type)
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise WbFbsOrdersTransportError("WB FBS offices API returned invalid JSON") from exc
        if not isinstance(payload, list):
            raise WbFbsOrdersTransportError("WB FBS offices API returned an invalid office list")
        result: list[WbFbsOffice] = []
        seen: set[int] = set()
        for item in payload:
            if not isinstance(item, Mapping):
                continue
            office_id = _bounded_int(item.get("id"), "office_id", minimum=1, maximum=2**63 - 1)
            if office_id in seen:
                raise WbFbsOrdersTransportError("WB FBS offices API returned a duplicate office ID")
            seen.add(office_id)
            result.append(
                WbFbsOffice(
                    office_id=office_id,
                    name=str(item.get("name") or "")[:200],
                    city=str(item.get("city") or "")[:200],
                    federal_district=str(item.get("federalDistrict") or "")[:200],
                )
            )
        return sorted(result, key=lambda item: item.office_id)

    def list_statuses(self, order_ids: list[int]) -> list[WbFbsOrderStatus]:
        normalized = sorted({_bounded_int(item, "order_id", minimum=1, maximum=2**63 - 1) for item in order_ids})
        if not normalized:
            return []
        if len(normalized) > 1000:
            raise ValueError("status read batch must not exceed 1000 orders")
        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        body = json.dumps({"orders": normalized}, separators=(",", ":")).encode("utf-8")
        request = urllib_request.Request(
            url=f"{runtime.base_url}/api/v3/orders/status",
            data=body,
            headers={
                "Authorization": runtime.token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=runtime.timeout_seconds) as response:
                status_code = _response_status(response)
                content_type = _response_content_type(response)
                raw_body = response.read().decode("utf-8", errors="replace")
        except error.HTTPError as exc:
            raw_body = exc.read().decode("utf-8", errors="replace")
            raise WbFbsOrdersHttpStatusError(
                exc.code, raw_body, content_type=_headers_content_type(exc.headers)
            ) from exc
        except (error.URLError, OSError) as exc:
            raise WbFbsOrdersTransportError(f"WB FBS status API transport failed: {exc}") from exc
        if status_code is not None and not 200 <= status_code < 300:
            raise WbFbsOrdersHttpStatusError(status_code, raw_body, content_type=content_type)
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise WbFbsOrdersTransportError("WB FBS status API returned invalid JSON") from exc
        rows = payload.get("orders") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            raise WbFbsOrdersTransportError("WB FBS status API returned an invalid status page")
        result: list[WbFbsOrderStatus] = []
        requested = set(normalized)
        for item in rows:
            if not isinstance(item, Mapping):
                continue
            order_id = _bounded_int(item.get("id"), "status order_id", minimum=1, maximum=2**63 - 1)
            if order_id not in requested:
                raise WbFbsOrdersTransportError("WB FBS status response escaped requested order scope")
            result.append(
                WbFbsOrderStatus(
                    order_id=order_id,
                    supplier_status=str(item.get("supplierStatus") or "")[:80],
                    wb_status=str(item.get("wbStatus") or "")[:80],
                )
            )
        return result


def _bounded_int(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if result < minimum or result > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise WbFbsOrdersTransportError("WB FBS seller warehouse API returned invalid metadata") from exc


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
