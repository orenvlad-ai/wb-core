"""Read-only adapter for official WB FBS orders and status observations."""

from __future__ import annotations

from dataclasses import dataclass
import json
import random
import re
import threading
import time
from typing import Any, Mapping
from urllib import error, parse as urllib_parse, request as urllib_request

from packages.adapters.official_api_runtime import (
    DEFAULT_WB_API_TOKEN_ENV,
    load_runtime_config,
)


DEFAULT_WB_FBS_API_BASE_URL = "https://marketplace-api.wildberries.ru"
DEFAULT_WB_FBS_API_BASE_URL_ENV = "WB_FBS_API_BASE_URL"
MAX_PAGE_LIMIT = 1000
MAX_STOCK_CHRT_IDS = 1000
MAX_WINDOW_SECONDS = 30 * 24 * 60 * 60


class WbFbsOrdersHttpStatusError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        body: str,
        *,
        content_type: str = "",
        headers: Mapping[str, Any] | None = None,
    ) -> None:
        self.status_code = int(status_code)
        self.content_type = str(content_type or "")
        self.headers = {
            str(key): str(value) for key, value in dict(headers or {}).items()
        }
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


@dataclass(frozen=True)
class WbFbsStock:
    """One explicit WB-declared stock value; never physical inventory truth."""

    chrt_id: int
    amount: int


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
        rate_budget: Any | None = None,
        max_retries: int = 3,
        retry_base_seconds: float = 0.5,
        retry_max_seconds: float = 30.0,
        sleep_fn: Any | None = None,
        random_fn: Any | None = None,
    ) -> None:
        self._default_base_url = str(base_url).rstrip("/")
        self._token_env_var = token_env_var
        self._base_url_env_var = base_url_env_var
        self._default_timeout_seconds = float(timeout_seconds)
        self._opener = opener or urllib_request.urlopen
        self._rate_budget = rate_budget
        self._max_retries = max(0, min(int(max_retries), 5))
        self._retry_base_seconds = max(0.01, min(float(retry_base_seconds), 30.0))
        self._retry_max_seconds = max(
            self._retry_base_seconds, min(float(retry_max_seconds), 15 * 60.0)
        )
        self._sleep = sleep_fn or time.sleep
        self._random = random_fn or random.random
        self._telemetry_lock = threading.Lock()
        self._telemetry = self._empty_telemetry()

    @staticmethod
    def _empty_telemetry() -> dict[str, float | int]:
        return {
            "request_count": 0,
            "retry_count": 0,
            "rate_limited_count": 0,
            "server_error_count": 0,
            "transport_error_count": 0,
            "rate_budget_wait_ms": 0.0,
            "retry_wait_ms": 0.0,
        }

    def reset_telemetry(self) -> None:
        with self._telemetry_lock:
            self._telemetry = self._empty_telemetry()

    def telemetry_snapshot(self) -> dict[str, float | int]:
        with self._telemetry_lock:
            return dict(self._telemetry)

    def _increment_telemetry(self, key: str, value: float | int = 1) -> None:
        with self._telemetry_lock:
            self._telemetry[key] = self._telemetry.get(key, 0) + value

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
        status_code, content_type, raw_body = self._open_with_retry(
            request,
            timeout_seconds=runtime.timeout_seconds,
            transport_label="WB FBS orders API transport failed",
        )

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
        status_code, content_type, raw_body = self._open_with_retry(
            request,
            timeout_seconds=runtime.timeout_seconds,
            transport_label="WB FBS seller warehouse API transport failed",
        )
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
        status_code, content_type, raw_body = self._open_with_retry(
            request,
            timeout_seconds=runtime.timeout_seconds,
            transport_label="WB FBS offices API transport failed",
        )
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

    def list_stocks(
        self, *, warehouse_id: int, chrt_ids: list[int]
    ) -> list[WbFbsStock]:
        """Read seller-warehouse stock without invoking any WB mutation API."""

        normalized_warehouse_id = _bounded_int(
            warehouse_id, "warehouse_id", minimum=1, maximum=2**63 - 1
        )
        if not chrt_ids or len(chrt_ids) > MAX_STOCK_CHRT_IDS:
            raise ValueError(
                f"chrt_ids must contain 1..{MAX_STOCK_CHRT_IDS} identities"
            )
        normalized_chrt_ids = sorted(
            {
                _bounded_int(item, "chrt_id", minimum=1, maximum=2**63 - 1)
                for item in chrt_ids
            }
        )
        if len(normalized_chrt_ids) != len(chrt_ids):
            raise ValueError("chrt_ids must not contain duplicates")
        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        request_body = json.dumps(
            {"chrtIds": normalized_chrt_ids}, separators=(",", ":")
        ).encode("utf-8")
        request = urllib_request.Request(
            url=f"{runtime.base_url}/api/v3/stocks/{normalized_warehouse_id}",
            data=request_body,
            headers={
                "Authorization": runtime.token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        status_code, content_type, raw_body = self._open_with_retry(
            request,
            timeout_seconds=runtime.timeout_seconds,
            transport_label="WB FBS stock readback API transport failed",
        )
        if status_code is not None and not 200 <= status_code < 300:
            raise WbFbsOrdersHttpStatusError(
                status_code, raw_body, content_type=content_type
            )
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise WbFbsOrdersTransportError(
                "WB FBS stock readback API returned invalid JSON"
            ) from exc
        stocks = payload.get("stocks") if isinstance(payload, Mapping) else None
        if not isinstance(stocks, list):
            raise WbFbsOrdersTransportError(
                "WB FBS stock readback API returned an invalid stock list"
            )
        result: list[WbFbsStock] = []
        seen: set[int] = set()
        requested = set(normalized_chrt_ids)
        for item in stocks:
            if not isinstance(item, Mapping):
                continue
            chrt_id = _bounded_int(
                item.get("chrtId"), "stock chrt_id", minimum=1, maximum=2**63 - 1
            )
            amount = _bounded_int(
                item.get("amount"), "stock amount", minimum=0, maximum=2**63 - 1
            )
            if chrt_id not in requested:
                raise WbFbsOrdersTransportError(
                    "WB FBS stock readback returned an unrequested chrtId"
                )
            if chrt_id in seen:
                raise WbFbsOrdersTransportError(
                    "WB FBS stock readback returned a duplicate chrtId"
                )
            seen.add(chrt_id)
            result.append(WbFbsStock(chrt_id=chrt_id, amount=amount))
        return sorted(result, key=lambda item: item.chrt_id)

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
        status_code, content_type, raw_body = self._open_with_retry(
            request,
            timeout_seconds=runtime.timeout_seconds,
            transport_label="WB FBS status API transport failed",
        )
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
        seen: set[int] = set()
        for item in rows:
            if not isinstance(item, Mapping):
                continue
            order_id = _bounded_int(item.get("id"), "status order_id", minimum=1, maximum=2**63 - 1)
            if order_id not in requested:
                raise WbFbsOrdersTransportError("WB FBS status response escaped requested order scope")
            if order_id in seen:
                raise WbFbsOrdersTransportError(
                    "WB FBS status response duplicated an order ID"
                )
            seen.add(order_id)
            result.append(
                WbFbsOrderStatus(
                    order_id=order_id,
                    supplier_status=str(item.get("supplierStatus") or "")[:80],
                    wb_status=str(item.get("wbStatus") or "")[:80],
                )
            )
        return result

    def _open_with_retry(
        self,
        request: urllib_request.Request,
        *,
        timeout_seconds: float,
        transport_label: str,
    ) -> tuple[int | None, str, str]:
        attempt = 0
        while True:
            if self._rate_budget is not None:
                reservation = self._rate_budget.acquire()
                self._increment_telemetry(
                    "rate_budget_wait_ms",
                    float(reservation.get("wait_seconds") or 0.0) * 1000.0,
                )
            self._increment_telemetry("request_count")
            try:
                with self._opener(request, timeout=timeout_seconds) as response:
                    status_code = _response_status(response)
                    content_type = _response_content_type(response)
                    raw_body = response.read().decode("utf-8", errors="replace")
                    headers = _headers_mapping(getattr(response, "headers", None))
            except error.HTTPError as exc:
                raw_body = exc.read().decode("utf-8", errors="replace")
                status_code = int(exc.code)
                content_type = _headers_content_type(exc.headers)
                headers = _headers_mapping(exc.headers)
            except (error.URLError, OSError) as exc:
                self._increment_telemetry("transport_error_count")
                if attempt < self._max_retries:
                    delay = self._retry_delay(attempt=attempt, headers={})
                    attempt += 1
                    self._increment_telemetry("retry_count")
                    self._defer_retry(delay)
                    continue
                raise WbFbsOrdersTransportError(f"{transport_label}: {exc}") from exc

            if status_code is None or 200 <= status_code < 300:
                return status_code, content_type, raw_body
            if status_code == 429:
                self._increment_telemetry("rate_limited_count")
            elif status_code >= 500:
                self._increment_telemetry("server_error_count")
            retryable = status_code == 429 or status_code >= 500
            if retryable and attempt < self._max_retries:
                delay = self._retry_delay(attempt=attempt, headers=headers)
                attempt += 1
                self._increment_telemetry("retry_count")
                self._defer_retry(delay)
                continue
            if status_code == 409 and self._rate_budget is not None:
                # WB counts one 409 as ten FBS-family requests.
                self._rate_budget.defer(9 * 0.22)
            raise WbFbsOrdersHttpStatusError(
                status_code,
                raw_body,
                content_type=content_type,
                headers=headers,
            )

    def _retry_delay(self, *, attempt: int, headers: Mapping[str, Any]) -> float:
        exponential = min(
            self._retry_max_seconds,
            self._retry_base_seconds * (2**max(int(attempt), 0)),
        )
        server_hint = max(
            _positive_float(_header_value(headers, "Retry-After")),
            _positive_float(_header_value(headers, "X-Ratelimit-Retry")),
        )
        jitter = min(exponential * 0.25, 1.0) * max(0.0, min(float(self._random()), 1.0))
        return min(self._retry_max_seconds, max(exponential, server_hint) + jitter)

    def _defer_retry(self, delay: float) -> None:
        bounded = max(0.0, min(float(delay), self._retry_max_seconds))
        self._increment_telemetry("retry_wait_ms", bounded * 1000.0)
        if self._rate_budget is not None:
            self._rate_budget.defer(bounded)
        else:
            self._sleep(bounded)


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


def _headers_mapping(headers: Any) -> dict[str, str]:
    items = getattr(headers, "items", None)
    if not callable(items):
        return {}
    allowlist = {"content-type", "retry-after", "x-ratelimit-retry"}
    return {
        str(key): str(value)
        for key, value in items()
        if str(key).casefold() in allowlist
    }


def _header_value(headers: Mapping[str, Any], name: str) -> Any:
    lowered = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == lowered:
            return value
    return None


def _positive_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if result > 0 else 0.0


def _sanitize_body_prefix(body: str, *, limit: int = 420) -> str:
    text = str(body or "").replace("\x00", "")
    text = re.sub(
        r"(?i)([\"']?(?:authorization|token|cookie|password|secret|api[-_ ]?key)[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^,\s}]+)",
        r'\1"<redacted>"',
        text,
    )
    text = re.sub(r"\b\d{8,}\b", lambda match: "***" + match.group(0)[-4:], text)
    return re.sub(r"\s+", " ", text).strip()[:limit]
