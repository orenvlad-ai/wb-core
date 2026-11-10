"""Адаптерная граница блока stocks."""

from __future__ import annotations

import errno
import hashlib
import json
import math
import multiprocessing
import os
import random
import select
import socket
import ssl
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping, Protocol
from urllib import error, request as urllib_request

from packages.adapters.official_api_runtime import DEFAULT_WB_API_TOKEN_ENV, load_runtime_config
from packages.adapters.seller_analytics_csv_report import (
    SellerAnalyticsCsvReportTransport,
)
from packages.business_time import business_date_iso
from packages.contracts.stocks_block import StocksRequest
from packages.contracts.stocks_failure_diagnostics import (
    attach_stocks_failure, exception_type, sanitize_stocks_failure,
)


WB_OTHER_WAREHOUSE_NAME = "остальные"
WB_AGGREGATE_WAREHOUSE_ID = -999999
WB_AGGREGATE_WAREHOUSE_NAME = "склад wb"
WB_AGGREGATE_REGION_NAMES = frozenset({WB_AGGREGATE_WAREHOUSE_NAME, "россия"})
WB_RF_AGGREGATE_WAREHOUSE_NAME = "склад wb рф"

# Unix datagrams are atomic, so an interrupted child cannot leave the parent
# blocked halfway through a frame. Larger records are assembled with a hard cap.
_STOCKS_IPC_FRAME_BYTES = 1024
_STOCKS_IPC_RECORD_BYTES = 16 * 1024
_STOCKS_IPC_RESULT_BYTES = 256 * 1024 * 1024
_STOCKS_IPC_RESULT_ITEMS = 1_000_000
_STOCKS_HTTP_TOTAL_BYTES = 128 * 1024 * 1024
_STOCKS_SOURCE_TOTAL_ROWS = 499_000


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
    payload: Mapping[str, Any] | None = None
    request_key: tuple[str, str, tuple[int, ...]] | None = None


class StocksFetchIncompleteError(RuntimeError):
    """The full inventory was not proven inside its resource budget."""


class _StocksValidationError(RuntimeError):
    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


@dataclass
class _StocksDeadline:
    expires_at: float
    clock: Callable[[], float] = time.monotonic
    body_bytes: int = 0
    source_rows: int = 0
    started_at: float | None = None
    stage: str = "initialization"
    attempt: int | None = None  # None: parent cannot observe the worker's sends.
    attempt_started_at: float | None = None
    attempt_elapsed_ms: int | None = None
    http_status: int | None = None

    def __post_init__(self) -> None:
        if self.started_at is None:
            self.started_at = self.clock()

    def failure(self, exc: BaseException) -> dict[str, Any]:
        chain: list[BaseException] = []
        current: BaseException | None = exc
        while current is not None and all(current is not item for item in chain) and len(chain) < 6:
            chain.append(current)
            current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
        transport_reason = next((item.reason for item in chain
            if isinstance(item, error.URLError) and isinstance(item.reason, BaseException)), None)
        numeric_chain = chain + ([transport_reason] if transport_reason is not None else [])
        reason = "program_error"
        if isinstance(exc, _StocksValidationError):
            reason = exc.reason
        elif isinstance(exc, StocksFetchIncompleteError):
            # Only our fixed literals are classified; no provider text is copied.
            reason = {
                "official stocks fetch incomplete: total deadline exhausted": "deadline_exhausted",
                "official stocks fetch incomplete: rate-limit wait exceeds deadline": "deadline_exhausted",
                "official stocks fetch incomplete: aggregate response byte limit exceeded": "resource_limit",
                "official stocks fetch incomplete: response body size limit exceeded": "resource_limit",
                "official stocks fetch incomplete: aggregate source row limit exceeded": "resource_limit",
                "official stocks worker cancelled": "cancelled",
            }.get(str(exc), "incomplete")
        elif any(isinstance(item, _StocksHttpStatusError) for item in chain):
            reason = "http_status"
        elif isinstance(exc, json.JSONDecodeError):
            reason = "invalid_json"
        elif isinstance(exc, UnicodeDecodeError):
            reason = "invalid_encoding"
        elif any(isinstance(item, (error.URLError, OSError)) for item in chain):
            reason = "transport"
        now = self.clock()
        elapsed = self.attempt_elapsed_ms
        if elapsed is None and self.attempt_started_at is not None:
            elapsed = max(0, int((now - self.attempt_started_at) * 1000))
        return sanitize_stocks_failure(dict(
            origin_type=exception_type(exc), cause_type=exception_type(chain[1]) if len(chain) > 1 else None,
            root_cause_type=exception_type(chain[-1]) if len(chain) > 1 else None,
            transport_reason_type=exception_type(transport_reason), stage=self.stage, reason=reason,
            http_status=self.http_status,
            errno=next((item.errno for item in numeric_chain if isinstance(item, OSError) and type(item.errno) is int), None),
            tls_verify_code=next((getattr(item, "verify_code", None) for item in numeric_chain if isinstance(item, ssl.SSLCertVerificationError)), None),
            attempt=self.attempt, attempt_elapsed_ms=elapsed,
            total_elapsed_ms=max(0, int((now - self.started_at) * 1000)),
        ))

    def remaining(self) -> float:
        remaining = self.expires_at - self.clock()
        if remaining <= 0:
            raise StocksFetchIncompleteError("official stocks fetch incomplete: total deadline exhausted")
        return remaining


def _stocks_ipc_send(channel: socket.socket, record: Mapping[str, Any], deadline: _StocksDeadline) -> None:
    encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _STOCKS_IPC_RECORD_BYTES:
        raise StocksFetchIncompleteError("official stocks fetch incomplete: result record size limit exceeded")
    for offset in range(0, len(encoded), _STOCKS_IPC_FRAME_BYTES - 1):
        deadline.remaining()
        chunk = encoded[offset:offset + _STOCKS_IPC_FRAME_BYTES - 1]
        final = offset + len(chunk) == len(encoded)
        channel.send((b"F" if final else b"C") + chunk)
        # One outstanding frame provides portable backpressure. macOS Unix
        # datagrams may report ENOBUFS instead of blocking on a full queue.
        if channel.recv(2) != b"A":
            raise StocksFetchIncompleteError("official stocks worker transfer cancelled")


def _stocks_inventory_worker(connection: socket.socket, config: dict[str, Any], kwargs: dict[str, Any], expires_at: float) -> None:
    """Spawn target: readonly HTTP/parse work; never owns cache or business writes."""
    deadline = _StocksDeadline(expires_at, attempt=0)
    try:
        deadline.started_at = config.pop("_started_at", deadline.started_at)
        captured_now = config.pop("_captured_now", None)
        source = HttpBackedStocksSource(**config)
        if captured_now is not None:
            source._now_factory = lambda: captured_now

        def slot(seller_key: str, retry_wait: float, reserve: bool) -> None:
            _stocks_ipc_send(connection, {"type": "slot" if reserve else "cooldown", "seller_key": seller_key, "wait": retry_wait}, deadline)
            if connection.recv(2) != b"G":
                raise StocksFetchIncompleteError("official stocks worker cancelled")
            deadline.remaining()

        source._slot_gate = slot
        payload = source._fetch_current_inventory(**kwargs, deadline=deadline)
        deadline.remaining()
        data = payload["data"]
        deadline.stage = "serialization"
        header = {key: value for key, value in data.items() if key not in {"rows", "raw_rows", "page_offsets", "batch_requested_nm_ids"}}
        _stocks_ipc_send(connection, {"type": "header", "snapshot_date": payload["snapshot_date"], "data": header}, deadline)
        for field in ("raw_rows", "rows"):
            for item in data[field]:
                _stocks_ipc_send(connection, {"type": field, "item": item}, deadline)
        for offset in data["page_offsets"]:
            _stocks_ipc_send(connection, {"type": "offset", "offset": offset}, deadline)
        _stocks_ipc_send(connection, {"type": "complete"}, deadline)
    except Exception as exc:
        # Only exact allowlisted scalar evidence crosses IPC. Exceptions can
        # retain response bodies, URLs, credentials and arbitrary provider text.
        try:
            try:
                evidence = deadline.failure(exc)
            except Exception:
                evidence = sanitize_stocks_failure({"origin_type": exception_type(exc)})
            _stocks_ipc_send(connection, {"type": "error", "diagnostic": evidence}, _StocksDeadline(expires_at))
        except Exception:
            pass
    finally:
        connection.close()


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


class HttpBackedStocksSource:
    """HTTP adapter к batched WB warehouses inventory endpoint."""

    _max_nm_ids_per_request = 1000
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
        total_timeout_seconds: float = 180.0,
        max_success_body_bytes: int = 128 * 1024 * 1024,
        max_error_body_bytes: int = 1024 * 1024,
        jitter_fn: Callable[[], float] | None = None,
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
        self._has_custom_now_factory = now_factory is not None
        raw_budget = os.environ.get("WB_STOCKS_FETCH_DEADLINE_SECONDS", str(total_timeout_seconds))
        self._total_timeout_seconds = float(raw_budget)
        if not math.isfinite(self._total_timeout_seconds) or not 0 < self._total_timeout_seconds <= 1800:
            raise ValueError("WB stocks total deadline must be finite and in (0, 1800] seconds")
        self._max_success_body_bytes = max(1, int(max_success_body_bytes))
        self._max_error_body_bytes = max(1, int(max_error_body_bytes))
        self._jitter = jitter_fn or random.random
        # Only explicit test dependencies use the synchronous path. Production
        # urllib is isolated using spawn, including DNS, body, JSON and hashing.
        self._isolate_fetch = all(value is None for value in (opener, monotonic_fn, sleep_fn, time_fn, jitter_fn))
        self._slot_gate: Callable[[str, float, bool], None] | None = None

    def fetch(self, request: StocksRequest) -> Mapping[str, Any]:
        deadline = _StocksDeadline(self._monotonic() + self._total_timeout_seconds, self._monotonic)
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
            deadline.remaining()
            return cached

        is_leader, single_flight = self._enter_single_flight(request_key)
        if not is_leader:
            if not single_flight.event.wait(timeout=deadline.remaining()):
                raise StocksFetchIncompleteError("official stocks fetch incomplete: single-flight deadline exhausted")
            deadline.remaining()
            if single_flight.error is not None:
                raise single_flight.error
            if single_flight.request_key == request_key and single_flight.payload is not None:
                if single_flight.payload.get("data", {}).get("pagination_complete") is True:
                    return single_flight.payload
            raise RuntimeError("official stocks request single-flight completed without payload")

        failure: Exception | None = None
        payload: Mapping[str, Any] | None = None
        try:
            kwargs = dict(
                base_url=runtime.base_url,
                token=runtime.token,
                requested_snapshot_date=request.snapshot_date,
                requested_nm_ids=normalized_nm_ids,
                timeout_seconds=runtime.timeout_seconds,
            )
            payload = (
                self._fetch_isolated(kwargs, deadline)
                if self._isolate_fetch
                else self._fetch_current_inventory(**kwargs, deadline=deadline)
            )
            deadline.remaining()
            self._store_cached_payload(request_key, payload, deadline=deadline)
            return payload
        except BaseException as exc:
            try:
                attach_stocks_failure(exc, deadline.failure(exc))
            except Exception:
                pass
            failure = exc if isinstance(exc, Exception) else StocksFetchIncompleteError("official stocks leader interrupted")
            raise
        finally:
            self._leave_single_flight(request_key, error=failure, payload=payload if failure is None else None)

    def _fetch_isolated(self, kwargs: dict[str, Any], deadline: _StocksDeadline) -> Mapping[str, Any]:
        context = multiprocessing.get_context("spawn")
        parent, child = socket.socketpair(type=socket.SOCK_DGRAM)
        parent.setblocking(False)
        config = dict(
            _started_at=deadline.started_at,
            page_limit=self._page_limit,
            min_request_interval_seconds=self._min_request_interval_seconds,
            max_retries_on_429=self._max_retries_on_429,
            max_success_body_bytes=self._max_success_body_bytes,
            max_error_body_bytes=self._max_error_body_bytes,
            total_timeout_seconds=self._total_timeout_seconds,
        )
        if self._has_custom_now_factory:
            config["_captured_now"] = self._now_factory()
        process = context.Process(target=_stocks_inventory_worker, args=(child, config, kwargs, deadline.expires_at), daemon=True)
        started = False
        record_bytes = bytearray()
        total_bytes = 0
        total_items = 0
        payload: dict[str, Any] | None = None
        seller_key = _seller_cache_key(base_url=kwargs["base_url"], token=kwargs["token"])
        try:
            deadline.stage = "worker_wait"
            deadline.remaining()
            process.start()
            started = True
            child.close()
            while True:
                # No receiver thread and no pickle.loads: only bounded atomic
                # datagrams are read, so a stalled partial result stays cancellable.
                readable, _, _ = select.select([parent], [], [], min(0.05, deadline.remaining()))
                deadline.remaining()
                if not readable:
                    if not process.is_alive():
                        raise StocksFetchIncompleteError("official stocks fetch incomplete: readonly worker ended without complete result")
                    continue
                packet = parent.recv(_STOCKS_IPC_FRAME_BYTES + 1)
                if not packet or len(packet) > _STOCKS_IPC_FRAME_BYTES or packet[:1] not in {b"C", b"F"}:
                    raise StocksFetchIncompleteError("official stocks fetch incomplete: invalid result frame")
                total_bytes += len(packet)
                if total_bytes > _STOCKS_IPC_RESULT_BYTES:
                    raise StocksFetchIncompleteError("official stocks fetch incomplete: aggregate result byte limit exceeded")
                record_bytes.extend(packet[1:])
                if len(record_bytes) > _STOCKS_IPC_RECORD_BYTES:
                    raise StocksFetchIncompleteError("official stocks fetch incomplete: result record size limit exceeded")
                parent.send(b"A")
                if packet[:1] == b"C":
                    continue
                # Each indivisible decode is <=16KiB, never a whole inventory.
                # Check both sides: a late decode cannot publish/cache success.
                deadline.remaining()
                message = json.loads(record_bytes)
                deadline.remaining()
                record_bytes.clear()
                if not isinstance(message, dict):
                    raise StocksFetchIncompleteError("official stocks fetch incomplete: invalid result record")
                kind = message.get("type")
                if kind in {"slot", "cooldown"}:
                    if payload is not None or message.get("seller_key") != seller_key:
                        raise StocksFetchIncompleteError("official stocks fetch incomplete: invalid rate-slot identity")
                    retry_wait = message["wait"]
                    if not isinstance(retry_wait, (int, float)) or not math.isfinite(retry_wait) or retry_wait < 0:
                        raise StocksFetchIncompleteError("official stocks fetch incomplete: invalid rate-slot wait")
                    if retry_wait > 0:
                        self._mark_next_request_after(seller_key, retry_wait)
                    if kind == "slot":
                        self._wait_for_request_slot(seller_key, deadline=deadline)
                    deadline.remaining()
                    parent.send(b"G")
                elif kind == "error":
                    evidence = sanitize_stocks_failure(message.get("diagnostic"))
                    kind = evidence["origin_type"]
                    text = "official stocks worker failed: " + evidence["reason"]
                    exc = (StocksFetchIncompleteError(text) if kind == "StocksFetchIncompleteError" else
                        ValueError(text) if kind in {"ValueError", "JSONDecodeError", "UnicodeDecodeError"} else
                        RuntimeError(text))
                    attach_stocks_failure(exc, evidence)
                    raise exc
                elif kind == "header":
                    if payload is not None or not isinstance(message.get("data"), dict):
                        raise StocksFetchIncompleteError("official stocks fetch incomplete: invalid result header")
                    data = message["data"]
                    data.update(rows=[], raw_rows=[], page_offsets=[])
                    nm_ids = kwargs["requested_nm_ids"]
                    data["batch_requested_nm_ids"] = [nm_ids[index:index + self._max_nm_ids_per_request] for index in range(0, len(nm_ids), self._max_nm_ids_per_request)] or [[]]
                    payload = {"snapshot_date": message["snapshot_date"], "requested_nm_ids": nm_ids, "data": data}
                elif kind in {"raw_rows", "rows", "offset"}:
                    if payload is None:
                        raise StocksFetchIncompleteError("official stocks fetch incomplete: result item before header")
                    total_items += 1
                    if total_items > _STOCKS_IPC_RESULT_ITEMS:
                        raise StocksFetchIncompleteError("official stocks fetch incomplete: aggregate result item limit exceeded")
                    if kind == "offset":
                        value = message["offset"]
                        if not isinstance(value, int) or value < 0:
                            raise StocksFetchIncompleteError("official stocks fetch incomplete: invalid page offset")
                        payload["data"]["page_offsets"].append(value)
                    else:
                        if not isinstance(message.get("item"), dict):
                            raise StocksFetchIncompleteError("official stocks fetch incomplete: invalid result item")
                        payload["data"][kind].append(message["item"])
                elif kind == "complete":
                    if payload is None:
                        raise StocksFetchIncompleteError("official stocks fetch incomplete: result missing header")
                    data = payload["data"]
                    if (data.get("pagination_complete") is not True
                        or len(data["raw_rows"]) != data.get("raw_row_count")
                        or len(data["rows"]) != data.get("normalization", {}).get("normalized_row_count")
                        or len(data["page_offsets"]) != data.get("page_count")
                        or len(data["batch_requested_nm_ids"]) != data.get("batch_count")):
                        raise StocksFetchIncompleteError("official stocks fetch incomplete: result count mismatch")
                    deadline.remaining()
                    return payload
                else:
                    raise StocksFetchIncompleteError("official stocks fetch incomplete: unknown result record")
        finally:
            child.close()
            if started:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=0.2)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=0.2)
                if not process.is_alive():
                    process.close()
            parent.close()

    def fetch_warehouse_region_map(self, nm_ids: list[int]) -> Mapping[str, str]:
        deadline = _StocksDeadline(self._monotonic() + self._total_timeout_seconds, self._monotonic)
        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        requested_nm_ids = sorted({int(nm_id) for nm_id in nm_ids})
        if not requested_nm_ids:
            return {}
        kwargs = dict(
            base_url=runtime.base_url,
            token=runtime.token,
            requested_nm_ids=requested_nm_ids,
            timeout_seconds=runtime.timeout_seconds,
            requested_snapshot_date="",
        )
        try:
            payload = self._fetch_isolated(kwargs, deadline) if self._isolate_fetch else self._fetch_current_inventory(**kwargs, deadline=deadline)
        except Exception as exc:
            try:
                attach_stocks_failure(exc, deadline.failure(exc))
            except Exception:
                pass
            raise
        items = payload["data"]["raw_rows"]
        mapping: dict[str, str] = {}
        for index, item in enumerate(items):
            if index % 256 == 0:
                deadline.remaining()
            warehouse_id = item.get("warehouseId")
            if (
                not isinstance(warehouse_id, int)
                or isinstance(warehouse_id, bool)
                or warehouse_id <= 0
            ):
                # Historical metadata fallback may reuse only concrete warehouse
                # identities. Service/aggregate buckets never prove a region.
                continue
            warehouse_name = str(item.get("warehouseName") or "").strip()
            region_name = str(item.get("regionName") or "").strip()
            if not warehouse_name or not region_name:
                continue
            mapping.setdefault(warehouse_name, region_name)
        deadline.remaining()
        return mapping

    def _fetch_current_inventory(
        self,
        *,
        base_url: str,
        token: str,
        requested_snapshot_date: str,
        requested_nm_ids: list[int],
        timeout_seconds: float,
        deadline: _StocksDeadline,
    ) -> Mapping[str, Any]:
        raw_items, pagination = self._fetch_current_inventory_capture(
            base_url=base_url,
            token=token,
            requested_nm_ids=requested_nm_ids,
            timeout_seconds=timeout_seconds,
            deadline=deadline,
        )
        deadline.stage = "normalization"
        normalized_items = [
            self._canonicalize_official_item(item)
            for item in raw_items
        ]
        aggregate_sentinel_row_count = sum(
            1 for item in raw_items if _is_exact_wb_aggregate_sentinel(item)
        )
        snapshot_dt = self._now_factory().astimezone(timezone.utc).replace(microsecond=0)
        snapshot_date = business_date_iso(snapshot_dt)
        snapshot_ts = snapshot_dt.strftime("%Y-%m-%d %H:%M:%S")
        rows = self._parse_items_to_rows(
            items=normalized_items,
            snapshot_date=snapshot_date,
            snapshot_ts=snapshot_ts,
            requested_nm_ids=set(requested_nm_ids),
        )
        deadline.remaining()
        return {
            "snapshot_date": snapshot_date,
            "requested_nm_ids": requested_nm_ids,
            "data": {
                "rows": rows,
                # Source evidence remains byte-semantic WB truth. Canonicalized
                # service rows live only in data.rows and the explicit
                # normalization envelope below.
                "raw_rows": raw_items,
                "requested_snapshot_date": requested_snapshot_date,
                "fetched_at": snapshot_dt.isoformat().replace("+00:00", "Z"),
                "pagination_complete": True,
                "missing_nm_ids_are_zero": True,
                "page_count": pagination["page_count"],
                "page_offsets": pagination["page_offsets"],
                "batch_count": pagination["batch_count"],
                "batch_requested_nm_ids": pagination["batch_requested_nm_ids"],
                "raw_row_count": len(raw_items),
                "raw_rows_digest": "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        sorted(
                            raw_items,
                            key=lambda item: json.dumps(
                                item,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        ),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                "warehouse_granularity_complete": aggregate_sentinel_row_count == 0,
                "normalization": {
                    "contract_name": "wb_stocks_source_normalization",
                    "contract_version": 1,
                    "aggregate_sentinel_row_count": aggregate_sentinel_row_count,
                    "aggregate_sentinel_normalized_to_service_bucket": (
                        aggregate_sentinel_row_count > 0
                    ),
                    "normalized_row_count": len(normalized_items),
                    "normalized_rows_digest": "sha256:"
                    + hashlib.sha256(
                        json.dumps(
                            rows,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                },
            },
        }

    def _fetch_current_inventory_items(
        self,
        *,
        base_url: str,
        token: str,
        requested_nm_ids: list[int],
        timeout_seconds: float,
        deadline: _StocksDeadline,
    ) -> list[Mapping[str, Any]]:
        items, _pagination = self._fetch_current_inventory_capture(
            base_url=base_url,
            token=token,
            requested_nm_ids=requested_nm_ids,
            timeout_seconds=timeout_seconds,
            deadline=deadline,
        )
        return items

    def _fetch_current_inventory_capture(
        self,
        *,
        base_url: str,
        token: str,
        requested_nm_ids: list[int],
        timeout_seconds: float,
        deadline: _StocksDeadline,
    ) -> tuple[list[Mapping[str, Any]], dict[str, Any]]:
        batches = [
            requested_nm_ids[index : index + self._max_nm_ids_per_request]
            for index in range(0, len(requested_nm_ids), self._max_nm_ids_per_request)
        ]
        if not batches:
            batches = [[]]
        items: list[Mapping[str, Any]] = []
        page_offsets: list[int] = []
        for batch_nm_ids in batches:
            deadline.remaining()
            batch_items, batch_pagination = self._fetch_current_inventory_batch_capture(
                base_url=base_url,
                token=token,
                requested_nm_ids=batch_nm_ids,
                timeout_seconds=timeout_seconds,
                deadline=deadline,
            )
            items.extend(batch_items)
            page_offsets.extend(batch_pagination["page_offsets"])
        deadline.remaining()
        return items, {
            "batch_count": len(batches),
            "batch_requested_nm_ids": batches,
            "page_count": len(page_offsets),
            "page_offsets": page_offsets,
        }

    def _fetch_current_inventory_batch_capture(
        self,
        *,
        base_url: str,
        token: str,
        requested_nm_ids: list[int],
        timeout_seconds: float,
        deadline: _StocksDeadline,
    ) -> tuple[list[Mapping[str, Any]], dict[str, Any]]:
        items: list[Mapping[str, Any]] = []
        offset = 0
        page_offsets: list[int] = []
        full_page_digests: set[str] = set()
        row_identities: set[tuple[int, int, int, str, str]] = set()
        requested_set = set(requested_nm_ids)
        seller_key = _seller_cache_key(base_url=base_url, token=token)
        while True:
            deadline.remaining()
            page_payload = self._post_inventory_page_with_retry(
                seller_key=seller_key,
                base_url=base_url,
                token=token,
                nm_ids=requested_nm_ids,
                offset=offset,
                timeout_seconds=timeout_seconds,
                deadline=deadline,
            )
            deadline.stage = "validation"
            page_items = self._extract_items(page_payload)
            deadline.remaining()
            if len(page_items) > self._page_limit:
                raise _StocksValidationError("page_limit", "official stocks request returned more rows than the page limit")
            deadline.source_rows += len(page_items)
            if deadline.source_rows > _STOCKS_SOURCE_TOTAL_ROWS:
                raise StocksFetchIncompleteError("official stocks fetch incomplete: aggregate source row limit exceeded")
            for index, item in enumerate(page_items):
                if index % 256 == 0:
                    deadline.remaining()
                identity = self._validate_official_item(item, requested_nm_ids=requested_set)
                if identity in row_identities:
                    raise _StocksValidationError("duplicate_row",
                        "official stocks request returned duplicate size/warehouse row "
                        f"for nmId {identity[0]}"
                    )
                row_identities.add(identity)
            page_offsets.append(offset)
            if len(page_offsets) > 1000:
                raise _StocksValidationError("pagination_limit", "official stocks pagination exceeded the safety page limit")
            if len(page_items) == self._page_limit:
                page_digest = hashlib.sha256(
                    json.dumps(
                        page_items,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                if page_digest in full_page_digests:
                    raise _StocksValidationError("repeated_page", "official stocks pagination repeated a full page")
                full_page_digests.add(page_digest)
            items.extend(page_items)
            if len(page_items) < self._page_limit:
                break
            offset += self._page_limit
        deadline.remaining()
        return items, {"page_count": len(page_offsets), "page_offsets": page_offsets}

    @staticmethod
    def _canonicalize_official_item(item: Mapping[str, Any]) -> Mapping[str, Any]:
        """Normalize only WB's exact aggregate sentinel for calculations."""

        if not _is_exact_wb_aggregate_sentinel(item):
            return item
        warehouse_name = str(item.get("warehouseName") or "").strip()
        region_name = str(item.get("regionName") or "").strip()
        return {
            **dict(item),
            "warehouseId": 0,
            "warehouseName": "Остальные",
            "regionName": region_name,
            "sourceWarehouseId": WB_AGGREGATE_WAREHOUSE_ID,
            "sourceWarehouseName": warehouse_name,
            "sourceRegionName": region_name,
            "normalizationReason": "exact_wb_aggregate_sentinel",
        }

    @staticmethod
    def _validate_official_item(
        item: Mapping[str, Any],
        *,
        requested_nm_ids: set[int],
    ) -> tuple[int, int, int, str, str]:
        nm_id = item.get("nmId")
        chrt_id = item.get("chrtId")
        warehouse_id = item.get("warehouseId")
        if not isinstance(nm_id, int) or isinstance(nm_id, bool) or nm_id <= 0:
            raise _StocksValidationError("invalid_nm_id", "official stocks request returned invalid nmId")
        if nm_id not in requested_nm_ids:
            raise _StocksValidationError("unexpected_nm_id", f"official stocks request returned unexpected nmId {nm_id}")
        if not isinstance(chrt_id, int) or isinstance(chrt_id, bool) or chrt_id <= 0:
            raise _StocksValidationError("invalid_chrt_id", f"official stocks request returned invalid chrtId for nmId {nm_id}")
        warehouse_name = str(item.get("warehouseName") or "").strip()
        region_name = str(item.get("regionName") or "").strip()
        warehouse_id_is_integer = isinstance(warehouse_id, int) and not isinstance(warehouse_id, bool)
        is_other_bucket = (
            warehouse_id_is_integer
            and warehouse_id == 0
            and warehouse_name.casefold() == WB_OTHER_WAREHOUSE_NAME
            and bool(region_name)
        )
        is_aggregate_sentinel = _is_exact_wb_aggregate_sentinel(item)
        if (
            not warehouse_id_is_integer
            or (warehouse_id < 0 and not is_aggregate_sentinel)
            or (warehouse_id == 0 and not is_other_bucket)
        ):
            row_digest = hashlib.sha256(
                json.dumps(
                    item,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:16]
            diagnostic_value = repr(warehouse_id)
            if len(diagnostic_value) > 80:
                diagnostic_value = f"{diagnostic_value[:77]}..."
            diagnostic_context = json.dumps(
                {
                    field: item.get(field)
                    for field in (
                        "warehouseName",
                        "regionName",
                        "quantity",
                        "inWayToClient",
                        "inWayFromClient",
                    )
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if len(diagnostic_context) > 240:
                diagnostic_context = f"{diagnostic_context[:237]}..."
            raise _StocksValidationError("invalid_warehouse_id",
                "official stocks request returned invalid warehouseId "
                f"for nmId {nm_id} "
                f"(present={str('warehouseId' in item).lower()}, "
                f"type={type(warehouse_id).__name__}, value={diagnostic_value}, "
                f"context={diagnostic_context}, "
                f"row_digest={row_digest})"
            )
        for field in ("quantity", "inWayToClient", "inWayFromClient"):
            value = item.get(field)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise _StocksValidationError("invalid_quantity",
                    f"official stocks request returned invalid {field} for nmId {nm_id}"
                )
        if is_aggregate_sentinel:
            # All accepted labels identify the same aggregate bucket. A mixed
            # old/new response must not count the same SKU/size twice.
            return nm_id, chrt_id, warehouse_id, WB_AGGREGATE_WAREHOUSE_NAME, "россия"
        if is_other_bucket:
            return nm_id, chrt_id, warehouse_id, warehouse_name.casefold(), region_name.casefold()
        return nm_id, chrt_id, warehouse_id, "", ""

    def _post_inventory_page_with_retry(
        self,
        *,
        seller_key: str,
        base_url: str,
        token: str,
        nm_ids: list[int],
        offset: int,
        timeout_seconds: float,
        deadline: _StocksDeadline,
    ) -> Mapping[str, Any]:
        retries_429 = 0
        retry_wait = 0.0
        deadline.attempt = 0
        deadline.attempt_started_at = None
        deadline.attempt_elapsed_ms = None
        deadline.http_status = None
        for attempt in range(3):
            deadline.stage = "rate_limit"
            deadline.remaining()
            if self._slot_gate is not None:
                self._slot_gate(seller_key, retry_wait, True)
            else:
                if retry_wait > 0:
                    self._mark_next_request_after(seller_key, retry_wait)
                self._wait_for_request_slot(seller_key, deadline=deadline)
            try:
                return self._post_inventory_page_once(
                    base_url=base_url,
                    token=token,
                    nm_ids=nm_ids,
                    offset=offset,
                    timeout_seconds=min(timeout_seconds, deadline.remaining()),
                    deadline=deadline,
                )
            except _StocksHttpStatusError as exc:
                if exc.status_code == 429:
                    retry_wait = self._resolve_retry_wait_seconds(exc.headers)
                    # Keep the shared cooldown even when this page exhausted its
                    # attempt budget, so the next fetch cannot bypass Retry-After.
                    if self._slot_gate is not None and retry_wait > 0:
                        # Parent handles a cooldown without reserving another send.
                        self._slot_gate(seller_key, retry_wait, False)
                    else:
                        self._mark_next_request_after(seller_key, retry_wait)
                    retryable = retries_429 < self._max_retries_on_429
                    retries_429 += 1
                else:
                    retryable = exc.status_code in {500, 502, 503, 504}
                    retry_wait = (2 ** attempt) + self._jitter()
                if not retryable or attempt == 2:
                    raise RuntimeError(f"official stocks request failed with status {exc.status_code}") from exc
            except (error.URLError, OSError) as exc:
                if not _stocks_transient_transport(exc) or attempt == 2:
                    raise RuntimeError("official stocks request transport failed") from exc
                retry_wait = (2 ** attempt) + self._jitter()
            finally:
                try:
                    if deadline.attempt_started_at is not None:
                        deadline.attempt_elapsed_ms = max(0, int((deadline.clock() - deadline.attempt_started_at) * 1000))
                except Exception:
                    pass  # Optional timing must never replace a source error.
        raise AssertionError("unreachable stocks retry state")

    def _read_bounded_body(self, response: Any, *, limit: int, deadline: _StocksDeadline) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while True:
            deadline.remaining()
            # read1 avoids filling a 64KiB request before a deadline check when
            # available. The isolated worker also cancels a read/DNS/TLS stall.
            reader = getattr(response, "read1", response.read)
            chunk = reader(min(65536, limit + 1 - size))
            deadline.remaining()
            if not chunk:
                return b"".join(chunks)
            size += len(chunk)
            deadline.body_bytes += len(chunk)
            if deadline.body_bytes > _STOCKS_HTTP_TOTAL_BYTES:
                raise StocksFetchIncompleteError("official stocks fetch incomplete: aggregate response byte limit exceeded")
            if size > limit:
                raise StocksFetchIncompleteError("official stocks fetch incomplete: response body size limit exceeded")
            chunks.append(chunk)

    def _post_inventory_page_once(
        self,
        *,
        base_url: str,
        token: str,
        nm_ids: list[int],
        offset: int,
        timeout_seconds: float,
        deadline: _StocksDeadline,
    ) -> Mapping[str, Any]:
        deadline.stage = "request"
        req = urllib_request.Request(
            url=f"{base_url}/api/analytics/v1/stocks-report/wb-warehouses",
            data=json.dumps({"nmIds": nm_ids, "chrtIds": [], "limit": self._page_limit, "offset": offset}).encode("utf-8"),
            method="POST",
            headers={"Authorization": token, "Content-Type": "application/json"},
        )
        try:
            request_timeout = min(timeout_seconds, deadline.remaining())
            deadline.attempt = (deadline.attempt or 0) + 1
            deadline.attempt_started_at = deadline.clock()
            deadline.attempt_elapsed_ms = None
            deadline.http_status = None
            with self._opener(req, timeout=request_timeout) as response:
                status = getattr(response, "status", None)
                deadline.http_status = status if type(status) is int else None
                deadline.stage = "body_read"
                body = self._read_bounded_body(response, limit=self._max_success_body_bytes, deadline=deadline)
                deadline.stage = "decode"
                payload = json.loads(body.decode("utf-8"))
                deadline.remaining()
        except error.HTTPError as exc:
            deadline.http_status = exc.code
            deadline.stage = "body_read"
            with exc:
                body = self._read_bounded_body(exc, limit=self._max_error_body_bytes, deadline=deadline)
            deadline.stage = "request"
            raise _StocksHttpStatusError(exc.code, body.decode("utf-8", errors="replace"), headers=exc.headers or {}) from exc
        deadline.stage = "validation"
        if not isinstance(payload, Mapping):
            raise _StocksValidationError("invalid_payload", "official stocks request returned non-object payload")
        return payload

    def _extract_items(self, payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise _StocksValidationError("invalid_data", "official stocks request returned invalid data object")
        items = data.get("items")
        if not isinstance(items, list):
            raise _StocksValidationError("invalid_items", "official stocks request returned invalid data.items list")
        if not all(isinstance(item, Mapping) for item in items):
            raise _StocksValidationError("invalid_item", "official stocks request returned a non-object item")
        return list(items)

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
            in_way_to_client = item.get("inWayToClient")
            in_way_from_client = item.get("inWayFromClient")
            rows.append(
                {
                    "snapshot_date": snapshot_date,
                    "snapshot_ts": snapshot_ts,
                    "nmId": nm_id,
                    "chrtId": item.get("chrtId"),
                    "warehouseId": _optional_int(item.get("warehouseId")),
                    "warehouseName": str(item.get("warehouseName") or ""),
                    "regionName": str(item.get("regionName") or ""),
                    "stockCount": float(quantity),
                    "inWayToClient": float(in_way_to_client),
                    "inWayFromClient": float(in_way_from_client),
                    **(
                        {
                            "sourceWarehouseId": item.get("sourceWarehouseId"),
                            "sourceWarehouseName": str(
                                item.get("sourceWarehouseName") or ""
                            ),
                            "sourceRegionName": str(
                                item.get("sourceRegionName") or ""
                            ),
                            "normalizationReason": str(
                                item.get("normalizationReason") or ""
                            ),
                        }
                        if item.get("normalizationReason")
                        else {}
                    ),
                }
            )
        return rows

    def _wait_for_request_slot(
        self, seller_key: str, *, deadline: _StocksDeadline,
        cancelled: threading.Event | None = None,
    ) -> None:
        while True:
            if cancelled is not None and cancelled.is_set():
                raise StocksFetchIncompleteError("official stocks worker cancelled")
            remaining = deadline.remaining()
            with self._shared_lock:
                state = self._rate_limit_states.setdefault(seller_key, _StocksRateLimitState())
                now = self._monotonic()
                wait_seconds = state.next_allowed_monotonic - now
                if wait_seconds <= 0:
                    # Reserve before every send, including failing attempts;
                    # distinct concurrent fetch keys cannot take the same slot.
                    deadline.remaining()
                    state.next_allowed_monotonic = now + self._min_request_interval_seconds
                    return
            if wait_seconds >= remaining:
                raise StocksFetchIncompleteError("official stocks fetch incomplete: rate-limit wait exceeds deadline")
            if cancelled is not None:
                if cancelled.wait(wait_seconds):
                    raise StocksFetchIncompleteError("official stocks worker cancelled")
            else:
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
        *,
        deadline: _StocksDeadline,
    ) -> None:
        deadline.remaining()
        if self._reuse_ttl_seconds <= 0:
            return
        with self._shared_lock:
            deadline.remaining()
            now = self._monotonic()
            if now >= deadline.expires_at:
                raise StocksFetchIncompleteError("official stocks fetch incomplete: deadline before cache publication")
            self._request_cache[request_key] = _StocksCacheEntry(
                payload=payload,
                expires_at_monotonic=now + self._reuse_ttl_seconds,
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
        payload: Mapping[str, Any] | None,
    ) -> None:
        with self._shared_lock:
            single_flight = self._single_flights.pop(request_key, None)
            if single_flight is None:
                return
            single_flight.error = error
            single_flight.payload = payload
            single_flight.request_key = request_key
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
        self._max_days_per_report = max(1, max_days_per_report)
        self._now_factory = now_factory or _utc_now
        self._csv_transport = SellerAnalyticsCsvReportTransport(
            poll_interval_seconds=poll_interval_seconds,
            max_poll_attempts=max_poll_attempts,
            max_retries_on_429=max_retries_on_429,
            opener=opener,
            sleep_fn=sleep_fn,
            time_fn=time_fn,
            uuid_factory=uuid_factory,
        )
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
        persisted = (
            dict(self._warehouse_region_resolver(requested_nm_ids))
            if self._warehouse_region_resolver is not None
            else {}
        )
        try:
            current = dict(
                self._current_inventory_source.fetch_warehouse_region_map(
                    requested_nm_ids
                )
            )
        except Exception:
            if persisted:
                return persisted
            raise
        return {**persisted, **current}

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
        report = self._csv_transport.fetch(
            base_url=base_url,
            token=token,
            timeout_seconds=timeout_seconds,
            report_type="STOCK_HISTORY_DAILY_CSV",
            report_name=f"wb-core stocks history {date_from}..{date_to}",
            params={
                "nmIds": requested_nm_ids,
                "currentPeriod": {"start": date_from, "end": date_to},
                "stockType": "wb",
                "skipDeletedNm": False,
            },
        )
        return self._build_batch_payloads(
            csv_rows=report.rows,
            requested_nm_ids=requested_nm_ids,
            warehouse_region_map=warehouse_region_map,
            date_from=date_from,
            date_to=date_to,
            download_id=report.download_id,
            report_name=report.report_name,
            snapshot_ts=report.created_at or _format_csv_snapshot_ts(self._now_factory()),
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
        aggregate_warehouse_names_by_date: dict[str, set[str]] = {
            snapshot_date: set()
            for snapshot_date in requested_dates
        }
        concrete_warehouse_names_by_date: dict[str, set[str]] = {
            snapshot_date: set()
            for snapshot_date in requested_dates
        }
        unique_nm_ids: set[int] = set()
        for csv_row in csv_rows:
            nm_id = _parse_csv_nm_id(csv_row.get("NmID"))
            if nm_id is None or nm_id not in requested_nm_ids_set:
                continue
            unique_nm_ids.add(nm_id)
            office_name = str(csv_row.get("OfficeName") or "").strip()
            is_aggregate_office = (
                office_name.casefold() == WB_AGGREGATE_WAREHOUSE_NAME
            )
            region_name = (
                office_name
                if is_aggregate_office
                else str(warehouse_region_map.get(office_name) or office_name)
            )
            for header, raw_value in csv_row.items():
                snapshot_date = _parse_csv_snapshot_date(header)
                if snapshot_date is None or snapshot_date not in requested_dates:
                    continue
                stock_count = _parse_csv_quantity(raw_value)
                if is_aggregate_office:
                    aggregate_warehouse_names_by_date[snapshot_date].add(office_name)
                elif office_name:
                    concrete_warehouse_names_by_date[snapshot_date].add(office_name)
                if abs(stock_count) > 0:
                    nonzero_rows_by_date[snapshot_date] += 1
                rows_by_date[snapshot_date].append(
                    {
                        "snapshot_date": snapshot_date,
                        "snapshot_ts": snapshot_ts,
                        "nmId": nm_id,
                        "warehouseId": None,
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
                    "warehouse_granularity_complete": not bool(
                        aggregate_warehouse_names_by_date[snapshot_date]
                    ),
                    "warehouse_granularity": {
                        "contract_name": "historical_stocks_warehouse_granularity",
                        "contract_version": 1,
                        "aggregate_office_names": sorted(
                            aggregate_warehouse_names_by_date[snapshot_date]
                        ),
                        "concrete_office_name_count": len(
                            concrete_warehouse_names_by_date[snapshot_date]
                        ),
                        "mixed_aggregate_and_concrete": bool(
                            aggregate_warehouse_names_by_date[snapshot_date]
                            and concrete_warehouse_names_by_date[snapshot_date]
                        ),
                    },
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


def _is_exact_wb_aggregate_sentinel(item: Mapping[str, Any]) -> bool:
    warehouse_id = item.get("warehouseId")
    if (
        not isinstance(warehouse_id, int)
        or isinstance(warehouse_id, bool)
        or warehouse_id != WB_AGGREGATE_WAREHOUSE_ID
    ):
        return False
    warehouse_name = str(item.get("warehouseName") or "").strip().casefold()
    region_name = str(item.get("regionName") or "").strip().casefold()
    return (
        (warehouse_name == WB_AGGREGATE_WAREHOUSE_NAME
         and region_name in WB_AGGREGATE_REGION_NAMES)
        or (warehouse_name == WB_RF_AGGREGATE_WAREHOUSE_NAME
            and region_name == "россия")
    )


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


def _parse_csv_nm_id(raw_value: Any) -> int | None:
    value = str(raw_value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _optional_int(raw_value: Any) -> int | None:
    if raw_value in (None, "") or isinstance(raw_value, bool):
        return None
    try:
        return int(str(raw_value).strip())
    except (TypeError, ValueError):
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


def _stocks_transient_transport(exc: BaseException) -> bool:
    reason = exc.reason if isinstance(exc, error.URLError) else exc
    if isinstance(reason, ssl.SSLError):
        return False
    return isinstance(reason, (TimeoutError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError)) or (
        isinstance(reason, OSError) and reason.errno in {errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE}
    )
