"""Адаптерная граница блока stocks."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping, Protocol
from urllib import error, request as urllib_request

from packages.adapters.official_api_runtime import DEFAULT_WB_API_TOKEN_ENV, load_runtime_config
from packages.contracts.stocks_block import StocksRequest


class StocksSource(Protocol):
    """Источник snapshot-данных для application-слоя."""

    def fetch(self, request: StocksRequest) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")


class ArtifactBackedStocksSource:
    """Локальный adapter, читающий legacy artifacts вместо сети."""

    def __init__(self, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    def fetch(self, request: StocksRequest) -> Mapping[str, Any]:
        path = self._resolve_legacy_path(request.scenario)
        return json.loads(path.read_text(encoding="utf-8"))

    def _resolve_legacy_path(self, scenario: str) -> Path:
        if scenario == "normal":
            return self._artifacts_root / "legacy" / "normal__template__legacy__fixture.json"
        if scenario == "partial":
            return self._artifacts_root / "legacy" / "partial__template__legacy__fixture.json"
        raise ValueError(f"unsupported scenario: {scenario}")


@dataclass
class _StocksRateLimitState:
    next_allowed_monotonic: float = 0.0


@dataclass
class _StocksCacheEntry:
    payload: Mapping[str, Any]
    expires_at_monotonic: float


@dataclass
class _StocksSingleFlight:
    event: threading.Event = field(default_factory=threading.Event)
    error: Exception | None = None


class _StocksHttpStatusError(RuntimeError):
    def __init__(self, status_code: int, body: str, headers: Mapping[str, Any] | None = None) -> None:
        self.status_code = status_code
        self.body = body
        self.headers = headers or {}
        super().__init__(f"stocks http {status_code}")


class HttpBackedStocksSource:
    """HTTP adapter к batched WB warehouses inventory endpoint."""

    _shared_lock = threading.Lock()
    _rate_limit_states: dict[str, _StocksRateLimitState] = {}
    _single_flights: dict[tuple[str, str, tuple[int, ...]], _StocksSingleFlight] = {}
    _request_cache: dict[tuple[str, str, tuple[int, ...]], _StocksCacheEntry] = {}

    def __init__(
        self,
        base_url: str = "https://seller-analytics-api.wildberries.ru",
        token_env_var: str = DEFAULT_WB_API_TOKEN_ENV,
        base_url_env_var: str = "WB_SELLER_ANALYTICS_API_BASE_URL",
        timeout_seconds: float = 30.0,
        page_limit: int = 250000,
        min_request_interval_seconds: float = 20.0,
        max_retries_on_429: int = 2,
        reuse_ttl_seconds: float = 30.0,
        opener: Callable[..., Any] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        monotonic_fn: Callable[[], float] | None = None,
        time_fn: Callable[[], float] | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._default_base_url = base_url.rstrip("/")
        self._token_env_var = token_env_var
        self._base_url_env_var = base_url_env_var
        self._default_timeout_seconds = timeout_seconds
        self._page_limit = max(1, min(page_limit, 250000))
        self._min_request_interval_seconds = max(0.0, min_request_interval_seconds)
        self._max_retries_on_429 = max(0, max_retries_on_429)
        self._reuse_ttl_seconds = max(0.0, reuse_ttl_seconds)
        self._opener = opener or urllib_request.urlopen
        self._sleep = sleep_fn or time.sleep
        self._monotonic = monotonic_fn or time.monotonic
        self._time = time_fn or time.time
        self._now_factory = now_factory or _utc_now

    def fetch(self, request: StocksRequest) -> Mapping[str, Any]:
        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        normalized_nm_ids = sorted({int(nm_id) for nm_id in request.nm_ids})
        request_key = (
            _seller_cache_key(base_url=runtime.base_url, token=runtime.token),
            request.snapshot_date,
            tuple(normalized_nm_ids),
        )
        cached = self._read_cached_payload(request_key)
        if cached is not None:
            return cached

        is_leader, single_flight = self._enter_single_flight(request_key)
        if not is_leader:
            single_flight.event.wait()
            cached = self._read_cached_payload(request_key)
            if cached is not None:
                return cached
            if single_flight.error is not None:
                raise single_flight.error
            raise RuntimeError("official stocks request single-flight completed without payload")

        try:
            payload = self._fetch_current_inventory(
                base_url=runtime.base_url,
                token=runtime.token,
                requested_snapshot_date=request.snapshot_date,
                requested_nm_ids=normalized_nm_ids,
                timeout_seconds=runtime.timeout_seconds,
            )
            self._store_cached_payload(request_key, payload)
            self._leave_single_flight(request_key, error=None)
            return payload
        except Exception as exc:
            self._leave_single_flight(request_key, error=exc)
            raise

    def _fetch_current_inventory(
        self,
        *,
        base_url: str,
        token: str,
        requested_snapshot_date: str,
        requested_nm_ids: list[int],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        items: list[Mapping[str, Any]] = []
        offset = 0
        seller_key = _seller_cache_key(base_url=base_url, token=token)
        while True:
            page_payload = self._post_inventory_page_with_retry(
                seller_key=seller_key,
                base_url=base_url,
                token=token,
                nm_ids=requested_nm_ids,
                offset=offset,
                timeout_seconds=timeout_seconds,
            )
            page_items = self._extract_items(page_payload)
            items.extend(page_items)
            if len(page_items) < self._page_limit:
                break
            offset += self._page_limit

        snapshot_dt = self._now_factory().astimezone(timezone.utc).replace(microsecond=0)
        snapshot_date = snapshot_dt.date().isoformat()
        snapshot_ts = snapshot_dt.strftime("%Y-%m-%d %H:%M:%S")
        rows = self._parse_items_to_rows(
            items=items,
            snapshot_date=snapshot_date,
            snapshot_ts=snapshot_ts,
            requested_nm_ids=set(requested_nm_ids),
        )
        return {
            "snapshot_date": snapshot_date,
            "requested_nm_ids": requested_nm_ids,
            "data": {
                "rows": rows,
                "requested_snapshot_date": requested_snapshot_date,
            },
        }

    def _post_inventory_page_with_retry(
        self,
        *,
        seller_key: str,
        base_url: str,
        token: str,
        nm_ids: list[int],
        offset: int,
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        attempt = 0
        while True:
            self._wait_for_request_slot(seller_key)
            try:
                payload = self._post_inventory_page_once(
                    base_url=base_url,
                    token=token,
                    nm_ids=nm_ids,
                    offset=offset,
                    timeout_seconds=timeout_seconds,
                )
                self._mark_next_request_after(seller_key, self._min_request_interval_seconds)
                return payload
            except _StocksHttpStatusError as exc:
                if exc.status_code == 429:
                    wait_seconds = self._resolve_retry_wait_seconds(exc.headers)
                    self._mark_next_request_after(seller_key, wait_seconds)
                    if attempt < self._max_retries_on_429:
                        attempt += 1
                        self._sleep(wait_seconds)
                        continue
                raise RuntimeError(
                    f"official stocks request failed with status {exc.status_code}: {exc.body}"
                ) from exc

    def _post_inventory_page_once(
        self,
        *,
        base_url: str,
        token: str,
        nm_ids: list[int],
        offset: int,
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        req = urllib_request.Request(
            url=f"{base_url}/api/analytics/v1/stocks-report/wb-warehouses",
            data=json.dumps(
                {
                    "nmIds": nm_ids,
                    "chrtIds": [],
                    "limit": self._page_limit,
                    "offset": offset,
                }
            ).encode("utf-8"),
            method="POST",
            headers={"Authorization": token, "Content-Type": "application/json"},
        )
        try:
            with self._opener(req, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            raise _StocksHttpStatusError(exc.code, body, headers=exc.headers or {}) from exc
        except error.URLError as exc:
            raise RuntimeError(f"official stocks request transport failed: {exc}") from exc

        if not isinstance(payload, Mapping):
            raise RuntimeError("official stocks request returned non-object payload")
        return payload

    def _extract_items(self, payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return []
        items = data.get("items")
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, Mapping)]

    def _parse_items_to_rows(
        self,
        *,
        items: list[Mapping[str, Any]],
        snapshot_date: str,
        snapshot_ts: str,
        requested_nm_ids: set[int],
    ) -> list[Mapping[str, Any]]:
        rows: list[Mapping[str, Any]] = []
        for item in items:
            nm_id = item.get("nmId")
            quantity = item.get("quantity")
            if not isinstance(nm_id, int) or nm_id not in requested_nm_ids:
                continue
            if not isinstance(quantity, (int, float)):
                continue
            rows.append(
                {
                    "snapshot_date": snapshot_date,
                    "snapshot_ts": snapshot_ts,
                    "nmId": nm_id,
                    "regionName": str(item.get("regionName") or ""),
                    "stockCount": float(quantity),
                }
            )
        return rows

    def _wait_for_request_slot(self, seller_key: str) -> None:
        while True:
            with self._shared_lock:
                state = self._rate_limit_states.setdefault(seller_key, _StocksRateLimitState())
                wait_seconds = state.next_allowed_monotonic - self._monotonic()
            if wait_seconds <= 0:
                return
            self._sleep(wait_seconds)

    def _mark_next_request_after(self, seller_key: str, wait_seconds: float) -> None:
        bounded_wait = max(0.0, wait_seconds)
        with self._shared_lock:
            state = self._rate_limit_states.setdefault(seller_key, _StocksRateLimitState())
            next_allowed = self._monotonic() + bounded_wait
            if next_allowed > state.next_allowed_monotonic:
                state.next_allowed_monotonic = next_allowed

    def _resolve_retry_wait_seconds(self, headers: Mapping[str, Any]) -> float:
        retry_seconds = _parse_positive_float(headers.get("X-Ratelimit-Retry"))
        reset_seconds = _parse_reset_header_seconds(headers.get("X-Ratelimit-Reset"), now_epoch=self._time())
        fallback_seconds = _parse_positive_float(headers.get("Retry-After"))
        return max(
            self._min_request_interval_seconds,
            retry_seconds,
            reset_seconds,
            fallback_seconds,
        )

    def _read_cached_payload(
        self,
        request_key: tuple[str, str, tuple[int, ...]],
    ) -> Mapping[str, Any] | None:
        with self._shared_lock:
            entry = self._request_cache.get(request_key)
            if entry is None:
                return None
            if entry.expires_at_monotonic <= self._monotonic():
                self._request_cache.pop(request_key, None)
                return None
            return entry.payload

    def _store_cached_payload(
        self,
        request_key: tuple[str, str, tuple[int, ...]],
        payload: Mapping[str, Any],
    ) -> None:
        if self._reuse_ttl_seconds <= 0:
            return
        with self._shared_lock:
            self._request_cache[request_key] = _StocksCacheEntry(
                payload=payload,
                expires_at_monotonic=self._monotonic() + self._reuse_ttl_seconds,
            )

    def _enter_single_flight(
        self,
        request_key: tuple[str, str, tuple[int, ...]],
    ) -> tuple[bool, _StocksSingleFlight]:
        with self._shared_lock:
            existing = self._single_flights.get(request_key)
            if existing is not None:
                return False, existing
            single_flight = _StocksSingleFlight()
            self._single_flights[request_key] = single_flight
            return True, single_flight

    def _leave_single_flight(
        self,
        request_key: tuple[str, str, tuple[int, ...]],
        *,
        error: Exception | None,
    ) -> None:
        with self._shared_lock:
            single_flight = self._single_flights.pop(request_key, None)
            if single_flight is None:
                return
            single_flight.error = error
            single_flight.event.set()


def _seller_cache_key(*, base_url: str, token: str) -> str:
    digest = hashlib.sha256(f"{base_url}|{token}".encode("utf-8")).hexdigest()
    return digest[:16]


def _parse_positive_float(raw_value: Any) -> float:
    if raw_value in (None, ""):
        return 0.0
    try:
        value = float(str(raw_value).strip())
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, value)


def _parse_reset_header_seconds(raw_value: Any, *, now_epoch: float) -> float:
    parsed = _parse_positive_float(raw_value)
    if parsed <= 0:
        return 0.0
    if parsed > now_epoch + 1:
        return max(0.0, parsed - now_epoch)
    return parsed


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
