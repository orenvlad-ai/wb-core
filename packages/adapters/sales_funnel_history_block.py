"""Адаптерная граница блока sales funnel history."""

from datetime import date, timedelta
import json
import time
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib import error, request as urllib_request

from packages.adapters.official_api_runtime import DEFAULT_WB_API_TOKEN_ENV, load_runtime_config
from packages.contracts.sales_funnel_history_block import SalesFunnelHistoryRequest


class SalesFunnelHistorySource(Protocol):
    def fetch(self, request: SalesFunnelHistoryRequest) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")


class ArtifactBackedSalesFunnelHistorySource:
    def __init__(self, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    def fetch(self, request: SalesFunnelHistoryRequest) -> Mapping[str, Any]:
        path = self._resolve_legacy_path(request.scenario)
        return json.loads(path.read_text(encoding="utf-8"))

    def _resolve_legacy_path(self, scenario: str) -> Path:
        if scenario == "normal":
            return self._artifacts_root / "legacy" / "normal__template__legacy__fixture.json"
        if scenario == "empty":
            return self._artifacts_root / "legacy" / "empty__template__legacy__fixture.json"
        raise ValueError(f"unsupported scenario: {scenario}")


class HttpBackedSalesFunnelHistorySource:
    def __init__(
        self,
        base_url: str = "https://seller-analytics-api.wildberries.ru",
        token_env_var: str = DEFAULT_WB_API_TOKEN_ENV,
        base_url_env_var: str = "WB_SELLER_ANALYTICS_API_BASE_URL",
        timeout_seconds: float = 30.0,
        max_nm_ids_per_request: int = 20,
        max_days_per_request: int = 7,
        max_requests_per_window: int = 3,
        rate_limit_window_seconds: float = 60.0,
        max_retries_on_429: int = 2,
        retry_backoff_seconds: float = 5.0,
    ) -> None:
        self._default_base_url = base_url.rstrip("/")
        self._token_env_var = token_env_var
        self._base_url_env_var = base_url_env_var
        self._default_timeout_seconds = timeout_seconds
        self._max_nm_ids_per_request = max_nm_ids_per_request
        self._max_days_per_request = max_days_per_request
        self._max_requests_per_window = max_requests_per_window
        self._rate_limit_window_seconds = rate_limit_window_seconds
        self._max_retries_on_429 = max_retries_on_429
        self._retry_backoff_seconds = retry_backoff_seconds

    def fetch(self, request: SalesFunnelHistoryRequest) -> Mapping[str, Any]:
        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        payload = self._fetch_batched_history(
            base_url=runtime.base_url,
            token=runtime.token,
            date_from=request.date_from,
            date_to=request.date_to,
            nm_ids=request.nm_ids,
            timeout_seconds=runtime.timeout_seconds,
        )
        rows: list[list[Any]] = []
        fetched_at = f"{request.date_to} 21:30:00"
        if isinstance(payload, list):
            for item in payload:
                if not isinstance(item, Mapping):
                    continue
                product = item.get("product")
                nm_id = product.get("nmId") if isinstance(product, Mapping) else None
                history = item.get("history")
                if not isinstance(nm_id, int) or not isinstance(history, list):
                    continue
                for point in history:
                    if not isinstance(point, Mapping):
                        continue
                    date = point.get("date")
                    if not isinstance(date, str):
                        continue
                    for metric, value in point.items():
                        if metric == "date" or isinstance(value, Mapping) or value is None:
                            continue
                        rows.append([fetched_at, date, nm_id, metric, value])
        return {
            "date_from": request.date_from,
            "date_to": request.date_to,
            "requested_nm_ids": request.nm_ids,
            "data": {"rows": rows},
        }

    def _fetch_batched_history(
        self,
        *,
        base_url: str,
        token: str,
        date_from: str,
        date_to: str,
        nm_ids: list[int],
        timeout_seconds: float,
    ) -> Any:
        merged_payload: list[Any] = []
        request_timestamps: list[float] = []
        for batch_date_from, batch_date_to in self._iter_date_batches(date_from, date_to):
            for batch_nm_ids in self._iter_nm_id_batches(nm_ids):
                payload = self._post_history_batch_with_retry(
                    base_url=base_url,
                    token=token,
                    date_from=batch_date_from,
                    date_to=batch_date_to,
                    nm_ids=batch_nm_ids,
                    timeout_seconds=timeout_seconds,
                    request_timestamps=request_timestamps,
                )
                if isinstance(payload, list):
                    merged_payload.extend(payload)
        return merged_payload

    def _post_history_batch_with_retry(
        self,
        *,
        base_url: str,
        token: str,
        date_from: str,
        date_to: str,
        nm_ids: list[int],
        timeout_seconds: float,
        request_timestamps: list[float],
    ) -> Any:
        attempt = 0
        while True:
            self._wait_for_request_slot(request_timestamps)
            request_timestamps.append(self._monotonic())
            try:
                return self._post_history_once(
                    base_url=base_url,
                    token=token,
                    date_from=date_from,
                    date_to=date_to,
                    nm_ids=nm_ids,
                    timeout_seconds=timeout_seconds,
                )
            except _SalesFunnelHistoryHttpStatusError as exc:
                if exc.status_code == 429 and attempt < self._max_retries_on_429:
                    attempt += 1
                    self._sleep(self._retry_backoff_seconds)
                    continue
                raise RuntimeError(
                    f"official sales funnel history request failed with status {exc.status_code}: {exc.body}"
                ) from exc

    def _post_history_once(
        self,
        *,
        base_url: str,
        token: str,
        date_from: str,
        date_to: str,
        nm_ids: list[int],
        timeout_seconds: float,
    ) -> Any:
        req = urllib_request.Request(
            url=f"{base_url}/api/analytics/v3/sales-funnel/products/history",
            data=json.dumps(
                {
                    "selectedPeriod": {"start": date_from, "end": date_to},
                    "nmIds": nm_ids,
                    "skipDeletedNm": True,
                    "aggregationLevel": "day",
                }
            ).encode(),
            method="POST",
            headers={"Authorization": token, "Content-Type": "application/json"},
        )
        try:
            with urllib_request.urlopen(req, timeout=timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            raise _SalesFunnelHistoryHttpStatusError(exc.code, body) from exc
        except error.URLError as exc:
            raise RuntimeError(
                f"official sales funnel history request transport failed: {exc}"
            ) from exc

    def _iter_nm_id_batches(self, nm_ids: list[int]) -> list[list[int]]:
        if self._max_nm_ids_per_request <= 0 or len(nm_ids) <= self._max_nm_ids_per_request:
            return [list(nm_ids)]
        return [
            list(nm_ids[index : index + self._max_nm_ids_per_request])
            for index in range(0, len(nm_ids), self._max_nm_ids_per_request)
        ]

    def _iter_date_batches(self, date_from: str, date_to: str) -> list[tuple[str, str]]:
        start = date.fromisoformat(date_from)
        end = date.fromisoformat(date_to)
        if end < start:
            raise ValueError("date_to must be >= date_from")
        if self._max_days_per_request <= 0:
            return [(date_from, date_to)]
        batches: list[tuple[str, str]] = []
        current = start
        while current <= end:
            batch_end = min(current + timedelta(days=self._max_days_per_request - 1), end)
            batches.append((current.isoformat(), batch_end.isoformat()))
            current = batch_end + timedelta(days=1)
        return batches

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


class _SalesFunnelHistoryHttpStatusError(RuntimeError):
    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"sales funnel history http {status_code}")
