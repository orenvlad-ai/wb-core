"""Адаптерная граница блока stocks."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping, Protocol
from urllib import error, request as urllib_request
import uuid
import zipfile

from packages.adapters.official_api_runtime import DEFAULT_WB_API_TOKEN_ENV, load_runtime_config
from packages.business_time import business_date_iso
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


@dataclass(frozen=True)
class HistoricalStocksWindowFetchResult:
    payloads: dict[str, Mapping[str, Any]]
    download_ids: list[str]
    report_names: list[str]
    row_count: int
    unique_nm_id_count: int
    notes_by_date: dict[str, str]


@dataclass(frozen=True)
class _HistoricalStocksBatchResult:
    payloads: dict[str, Mapping[str, Any]]
    download_id: str
    report_name: str
    row_count: int
    unique_nm_ids: set[int]
    notes_by_date: dict[str, str]


class _HistoricalStocksHttpStatusError(RuntimeError):
    def __init__(self, status_code: int, body: str, headers: Mapping[str, Any] | None = None) -> None:
        self.status_code = status_code
        self.body = body
        self.headers = headers or {}
        super().__init__(f"historical stocks http {status_code}")


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

    def fetch_warehouse_region_map(self, nm_ids: list[int]) -> Mapping[str, str]:
        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        requested_nm_ids = sorted({int(nm_id) for nm_id in nm_ids})
        if not requested_nm_ids:
            return {}
        items = self._fetch_current_inventory_items(
            base_url=runtime.base_url,
            token=runtime.token,
            requested_nm_ids=requested_nm_ids,
            timeout_seconds=runtime.timeout_seconds,
        )
        mapping: dict[str, str] = {}
        for item in items:
            warehouse_name = str(item.get("warehouseName") or "").strip()
            region_name = str(item.get("regionName") or "").strip()
            if not warehouse_name or not region_name:
                continue
            mapping.setdefault(warehouse_name, region_name)
        return mapping

    def _fetch_current_inventory(
        self,
        *,
        base_url: str,
        token: str,
        requested_snapshot_date: str,
        requested_nm_ids: list[int],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        items = self._fetch_current_inventory_items(
            base_url=base_url,
            token=token,
            requested_nm_ids=requested_nm_ids,
            timeout_seconds=timeout_seconds,
        )
        snapshot_dt = self._now_factory().astimezone(timezone.utc).replace(microsecond=0)
        snapshot_date = business_date_iso(snapshot_dt)
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

    def _fetch_current_inventory_items(
        self,
        *,
        base_url: str,
        token: str,
        requested_nm_ids: list[int],
        timeout_seconds: float,
    ) -> list[Mapping[str, Any]]:
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
        return items

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
                    "warehouseName": str(item.get("warehouseName") or ""),
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


class HistoricalCsvBackedStocksSource:
    """Historical closed-day stocks adapter backed by Seller Analytics CSV."""

    def __init__(
        self,
        base_url: str = "https://seller-analytics-api.wildberries.ru",
        token_env_var: str = DEFAULT_WB_API_TOKEN_ENV,
        base_url_env_var: str = "WB_SELLER_ANALYTICS_API_BASE_URL",
        timeout_seconds: float = 60.0,
        poll_interval_seconds: float = 2.0,
        max_poll_attempts: int = 120,
        max_days_per_report: int = 31,
        max_retries_on_429: int = 3,
        opener: Callable[..., Any] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        time_fn: Callable[[], float] | None = None,
        uuid_factory: Callable[[], str] | None = None,
        now_factory: Callable[[], datetime] | None = None,
        warehouse_region_resolver: Callable[[list[int]], Mapping[str, str]] | None = None,
        current_inventory_source: HttpBackedStocksSource | None = None,
    ) -> None:
        self._default_base_url = base_url.rstrip("/")
        self._token_env_var = token_env_var
        self._base_url_env_var = base_url_env_var
        self._default_timeout_seconds = timeout_seconds
        self._poll_interval_seconds = max(0.1, poll_interval_seconds)
        self._max_poll_attempts = max(1, max_poll_attempts)
        self._max_days_per_report = max(1, max_days_per_report)
        self._max_retries_on_429 = max(0, max_retries_on_429)
        self._opener = opener or urllib_request.urlopen
        self._sleep = sleep_fn or time.sleep
        self._time = time_fn or time.time
        self._uuid_factory = uuid_factory or (lambda: str(uuid.uuid4()))
        self._now_factory = now_factory or _utc_now
        self._warehouse_region_resolver = warehouse_region_resolver
        self._current_inventory_source = current_inventory_source or HttpBackedStocksSource(
            base_url=base_url,
            token_env_var=token_env_var,
            base_url_env_var=base_url_env_var,
            timeout_seconds=timeout_seconds,
            min_request_interval_seconds=0.0,
            reuse_ttl_seconds=0.0,
        )

    def fetch(self, request: StocksRequest) -> Mapping[str, Any]:
        result = self.fetch_window(
            date_from=request.snapshot_date,
            date_to=request.snapshot_date,
            nm_ids=request.nm_ids,
        )
        payload = result.payloads.get(request.snapshot_date)
        if payload is None:
            raise RuntimeError(
                f"historical stocks report did not materialize snapshot for {request.snapshot_date}"
            )
        return payload

    def fetch_window(
        self,
        *,
        date_from: str,
        date_to: str,
        nm_ids: list[int],
    ) -> HistoricalStocksWindowFetchResult:
        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        requested_nm_ids = sorted({int(nm_id) for nm_id in nm_ids})
        warehouse_region_map = self._resolve_warehouse_region_map(requested_nm_ids)
        payloads: dict[str, Mapping[str, Any]] = {}
        notes_by_date: dict[str, str] = {}
        download_ids: list[str] = []
        report_names: list[str] = []
        unique_nm_ids: set[int] = set()
        total_row_count = 0
        for batch_date_from, batch_date_to in _iter_date_windows(
            date_from=date_from,
            date_to=date_to,
            max_days=self._max_days_per_report,
        ):
            batch_result = self._fetch_window_batch(
                base_url=runtime.base_url,
                token=runtime.token,
                timeout_seconds=runtime.timeout_seconds,
                date_from=batch_date_from,
                date_to=batch_date_to,
                requested_nm_ids=requested_nm_ids,
                warehouse_region_map=warehouse_region_map,
            )
            payloads.update(batch_result.payloads)
            notes_by_date.update(batch_result.notes_by_date)
            download_ids.append(batch_result.download_id)
            report_names.append(batch_result.report_name)
            unique_nm_ids.update(batch_result.unique_nm_ids)
            total_row_count += batch_result.row_count

        return HistoricalStocksWindowFetchResult(
            payloads=payloads,
            download_ids=download_ids,
            report_names=report_names,
            row_count=total_row_count,
            unique_nm_id_count=len(unique_nm_ids),
            notes_by_date=notes_by_date,
        )

    def _resolve_warehouse_region_map(self, requested_nm_ids: list[int]) -> Mapping[str, str]:
        if self._warehouse_region_resolver is not None:
            return dict(self._warehouse_region_resolver(requested_nm_ids))
        return self._current_inventory_source.fetch_warehouse_region_map(requested_nm_ids)

    def _fetch_window_batch(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float,
        date_from: str,
        date_to: str,
        requested_nm_ids: list[int],
        warehouse_region_map: Mapping[str, str],
    ) -> _HistoricalStocksBatchResult:
        download_id = self._uuid_factory()
        report_name = f"wb-core stocks history {date_from}..{date_to} [{download_id[:8]}]"
        self._create_report(
            base_url=base_url,
            token=token,
            timeout_seconds=timeout_seconds,
            download_id=download_id,
            report_name=report_name,
            date_from=date_from,
            date_to=date_to,
            requested_nm_ids=requested_nm_ids,
        )
        report_meta = self._poll_report_ready(
            base_url=base_url,
            token=token,
            timeout_seconds=timeout_seconds,
            download_id=download_id,
        )
        csv_rows = self._download_report_rows(
            base_url=base_url,
            token=token,
            timeout_seconds=timeout_seconds,
            download_id=download_id,
        )
        return self._build_batch_payloads(
            csv_rows=csv_rows,
            requested_nm_ids=requested_nm_ids,
            warehouse_region_map=warehouse_region_map,
            date_from=date_from,
            date_to=date_to,
            download_id=download_id,
            report_name=str(report_meta.get("name") or report_name),
            snapshot_ts=str(report_meta.get("createdAt") or _format_csv_snapshot_ts(self._now_factory())),
        )

    def _create_report(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float,
        download_id: str,
        report_name: str,
        date_from: str,
        date_to: str,
        requested_nm_ids: list[int],
    ) -> None:
        req = urllib_request.Request(
            url=f"{base_url}/api/v2/nm-report/downloads",
            data=json.dumps(
                {
                    "id": download_id,
                    "reportType": "STOCK_HISTORY_DAILY_CSV",
                    "userReportName": report_name,
                    "params": {
                        "nmIds": requested_nm_ids,
                        "currentPeriod": {"start": date_from, "end": date_to},
                        "stockType": "wb",
                        "skipDeletedNm": False,
                    },
                }
            ).encode("utf-8"),
            method="POST",
            headers={"Authorization": token, "Content-Type": "application/json"},
        )
        body = self._open_report_request_with_429_retry(
            req,
            timeout_seconds=timeout_seconds,
            action_label="create-report",
        ).decode("utf-8")
        if not body:
            return
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return
        if isinstance(payload, Mapping) and str(payload.get("error", "")).strip():
            raise RuntimeError(f"historical stocks create-report failed: {payload}")

    def _poll_report_ready(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float,
        download_id: str,
        ) -> Mapping[str, Any]:
        for attempt in range(self._max_poll_attempts):
            req = urllib_request.Request(
                url=f"{base_url}/api/v2/nm-report/downloads",
                method="GET",
                headers={"Authorization": token},
            )
            payload = json.loads(
                self._open_report_request_with_429_retry(
                    req,
                    timeout_seconds=timeout_seconds,
                    action_label="poll",
                ).decode("utf-8")
            )
            report = _find_download_report(payload, download_id)
            if report is None:
                if attempt + 1 >= self._max_poll_attempts:
                    raise RuntimeError(f"historical stocks report {download_id} was not listed")
                self._sleep(self._poll_interval_seconds)
                continue
            status = str(report.get("status") or "").upper()
            if status == "SUCCESS":
                return report
            if status in {"FAILED", "ERROR", "CANCELLED", "CANCELED"}:
                raise RuntimeError(
                    f"historical stocks report {download_id} failed with status {status}: {report}"
                )
            if attempt + 1 >= self._max_poll_attempts:
                raise RuntimeError(
                    f"historical stocks report {download_id} did not finish within bounded polling window"
                )
            self._sleep(self._poll_interval_seconds)
        raise RuntimeError(f"historical stocks report {download_id} polling exhausted")

    def _download_report_rows(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float,
        download_id: str,
    ) -> list[dict[str, str]]:
        req = urllib_request.Request(
            url=f"{base_url}/api/v2/nm-report/downloads/file/{download_id}",
            method="GET",
            headers={"Authorization": token},
        )
        payload = self._open_report_request_with_429_retry(
            req,
            timeout_seconds=timeout_seconds,
            action_label="download",
        )
        csv_bytes = _extract_csv_bytes(payload)
        csv_text = _decode_csv_bytes(csv_bytes)
        return _iter_csv_dict_rows(csv_text)

    def _open_report_request_with_429_retry(
        self,
        req: urllib_request.Request,
        *,
        timeout_seconds: float,
        action_label: str,
    ) -> bytes:
        attempt = 0
        while True:
            try:
                with self._opener(req, timeout=timeout_seconds) as response:
                    return response.read()
            except error.HTTPError as exc:
                body = exc.read().decode("utf-8")
                wrapped = _HistoricalStocksHttpStatusError(exc.code, body, headers=exc.headers or {})
                if wrapped.status_code == 429 and attempt < self._max_retries_on_429:
                    attempt += 1
                    self._sleep(self._resolve_retry_wait_seconds(wrapped.headers))
                    continue
                raise wrapped from exc
            except error.URLError as exc:
                raise RuntimeError(
                    f"historical stocks {action_label} transport failed: {exc}"
                ) from exc

    def _resolve_retry_wait_seconds(self, headers: Mapping[str, Any]) -> float:
        retry_seconds = _parse_positive_float(headers.get("X-Ratelimit-Retry"))
        reset_seconds = _parse_reset_header_seconds(headers.get("X-Ratelimit-Reset"), now_epoch=self._time())
        fallback_seconds = _parse_positive_float(headers.get("Retry-After"))
        return max(
            self._poll_interval_seconds,
            retry_seconds,
            reset_seconds,
            fallback_seconds,
        )

    def _build_batch_payloads(
        self,
        *,
        csv_rows: list[dict[str, str]],
        requested_nm_ids: list[int],
        warehouse_region_map: Mapping[str, str],
        date_from: str,
        date_to: str,
        download_id: str,
        report_name: str,
        snapshot_ts: str,
    ) -> _HistoricalStocksBatchResult:
        requested_nm_ids_set = set(requested_nm_ids)
        requested_dates = {
            value.isoformat()
            for value in _iter_dates(date_from=date_from, date_to=date_to)
        }
        rows_by_date: dict[str, list[Mapping[str, Any]]] = {
            snapshot_date: []
            for snapshot_date in requested_dates
        }
        nonzero_rows_by_date: dict[str, int] = {
            snapshot_date: 0
            for snapshot_date in requested_dates
        }
        unique_nm_ids: set[int] = set()
        for csv_row in csv_rows:
            nm_id = _parse_csv_nm_id(csv_row.get("NmID"))
            if nm_id is None or nm_id not in requested_nm_ids_set:
                continue
            unique_nm_ids.add(nm_id)
            office_name = str(csv_row.get("OfficeName") or "").strip()
            region_name = str(warehouse_region_map.get(office_name) or office_name)
            for header, raw_value in csv_row.items():
                snapshot_date = _parse_csv_snapshot_date(header)
                if snapshot_date is None or snapshot_date not in requested_dates:
                    continue
                stock_count = _parse_csv_quantity(raw_value)
                if abs(stock_count) > 0:
                    nonzero_rows_by_date[snapshot_date] += 1
                rows_by_date[snapshot_date].append(
                    {
                        "snapshot_date": snapshot_date,
                        "snapshot_ts": snapshot_ts,
                        "nmId": nm_id,
                        "warehouseName": office_name,
                        "regionName": region_name,
                        "stockCount": stock_count,
                    }
                )
        payloads = {
            snapshot_date: {
                "snapshot_date": snapshot_date,
                "requested_nm_ids": requested_nm_ids,
                "source": {
                    "endpoint_chain": (
                        "POST /api/v2/nm-report/downloads + "
                        "GET /api/v2/nm-report/downloads + "
                        f"GET /api/v2/nm-report/downloads/file/{download_id}"
                    ),
                    "report_type": "STOCK_HISTORY_DAILY_CSV",
                    "report_name": report_name,
                    "download_id": download_id,
                },
                "data": {
                    "rows": rows_by_date[snapshot_date],
                    "requested_snapshot_date": snapshot_date,
                },
            }
            for snapshot_date in sorted(requested_dates)
        }
        notes_by_date = {
            snapshot_date: f"download_id={download_id}; nonzero_rows={nonzero_rows_by_date[snapshot_date]}"
            for snapshot_date in sorted(requested_dates)
        }
        return _HistoricalStocksBatchResult(
            payloads=payloads,
            download_id=download_id,
            report_name=report_name,
            row_count=len(csv_rows),
            unique_nm_ids=unique_nm_ids,
            notes_by_date=notes_by_date,
        )


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


def _find_download_report(payload: Any, download_id: str) -> Mapping[str, Any] | None:
    candidates: list[Any]
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, Mapping):
        data = payload.get("data")
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, Mapping):
            nested = data.get("reports") or data.get("items") or data.get("data")
            candidates = nested if isinstance(nested, list) else []
        else:
            candidates = []
    else:
        candidates = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        if str(candidate.get("id") or "") == download_id:
            return candidate
    return None


def _extract_csv_bytes(payload: bytes) -> bytes:
    buffer = io.BytesIO(payload)
    if not zipfile.is_zipfile(buffer):
        return payload
    with zipfile.ZipFile(buffer, "r") as archive:
        candidate_names = [name for name in archive.namelist() if not name.endswith("/")]
        if not candidate_names:
            raise RuntimeError("historical stocks report archive is empty")
        preferred_name = next(
            (name for name in candidate_names if name.lower().endswith(".csv")),
            candidate_names[0],
        )
        return archive.read(preferred_name)


def _decode_csv_bytes(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise RuntimeError("historical stocks csv bytes could not be decoded")


def _iter_csv_dict_rows(csv_text: str) -> list[dict[str, str]]:
    lines = csv_text.splitlines()
    if not lines:
        return []
    delimiter = ";" if lines[0].count(";") > lines[0].count(",") else ","
    reader = csv.DictReader(io.StringIO(csv_text), delimiter=delimiter)
    return [
        {str(key or "").strip(): str(value or "").strip() for key, value in row.items()}
        for row in reader
    ]


def _parse_csv_nm_id(raw_value: Any) -> int | None:
    value = str(raw_value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_csv_snapshot_date(header: Any) -> str | None:
    value = str(header or "").strip()
    parts = value.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    day, month, year = parts
    if len(day) != 2 or len(month) != 2 or len(year) != 4:
        return None
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def _parse_csv_quantity(raw_value: Any) -> float:
    value = str(raw_value or "").strip()
    if not value:
        return 0.0
    normalized = value.replace(" ", "").replace(",", ".")
    try:
        return float(normalized)
    except ValueError as exc:
        raise RuntimeError(f"historical stocks csv contains non-numeric quantity: {raw_value!r}") from exc


def _iter_date_windows(*, date_from: str, date_to: str, max_days: int) -> list[tuple[str, str]]:
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if end < start:
        raise ValueError("date_to must be >= date_from")
    windows: list[tuple[str, str]] = []
    current = start
    while current <= end:
        window_end = min(current + timedelta(days=max_days - 1), end)
        windows.append((current.isoformat(), window_end.isoformat()))
        current = window_end + timedelta(days=1)
    return windows


def _iter_dates(*, date_from: str, date_to: str) -> list[date]:
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if end < start:
        raise ValueError("date_to must be >= date_from")
    current = start
    values: list[date] = []
    while current <= end:
        values.append(current)
        current += timedelta(days=1)
    return values


def _format_csv_snapshot_ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
