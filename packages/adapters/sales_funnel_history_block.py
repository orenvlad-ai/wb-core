"""Адаптерная граница блока sales funnel history."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib import error, request as urllib_request

from packages.adapters.official_api_runtime import DEFAULT_WB_API_TOKEN_ENV, load_runtime_config
from packages.adapters.seller_analytics_csv_report import (
    SellerAnalyticsCsvReportTransport,
)
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


class DetailHistoryCsvBackedSalesFunnelHistorySource:
    """Historical daily funnel adapter backed by official DETAIL_HISTORY_REPORT CSV."""

    REPORT_TYPE = "DETAIL_HISTORY_REPORT"
    TIMEZONE = "Asia/Yekaterinburg"
    REQUIRED_COLUMNS = frozenset({"nmID", "dt", "ordersCount", "buyoutPercent"})

    def __init__(
        self,
        base_url: str = "https://seller-analytics-api.wildberries.ru",
        token_env_var: str = DEFAULT_WB_API_TOKEN_ENV,
        base_url_env_var: str = "WB_SELLER_ANALYTICS_API_BASE_URL",
        timeout_seconds: float = 60.0,
        poll_interval_seconds: float = 2.0,
        max_poll_attempts: int = 120,
        max_retries_on_429: int = 3,
        opener: Callable[..., Any] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        time_fn: Callable[[], float] | None = None,
        uuid_factory: Callable[[], str] | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._default_base_url = base_url.rstrip("/")
        self._token_env_var = token_env_var
        self._base_url_env_var = base_url_env_var
        self._default_timeout_seconds = timeout_seconds
        self._now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self._csv_transport = SellerAnalyticsCsvReportTransport(
            poll_interval_seconds=poll_interval_seconds,
            max_poll_attempts=max_poll_attempts,
            max_retries_on_429=max_retries_on_429,
            opener=opener,
            sleep_fn=sleep_fn,
            time_fn=time_fn,
            uuid_factory=uuid_factory,
        )
        self._last_fetch_evidence: dict[str, Any] = {}

    @property
    def last_fetch_evidence(self) -> Mapping[str, Any]:
        return dict(self._last_fetch_evidence)

    def fetch(self, request: SalesFunnelHistoryRequest) -> Mapping[str, Any]:
        self._last_fetch_evidence = {}
        start = date.fromisoformat(request.date_from)
        end = date.fromisoformat(request.date_to)
        if end < start:
            raise ValueError("date_to must be >= date_from")
        if (end - start).days + 1 > 365:
            raise ValueError("DETAIL_HISTORY_REPORT window must not exceed 365 days")
        requested_nm_ids = sorted({int(nm_id) for nm_id in request.nm_ids})
        if not requested_nm_ids:
            raise ValueError("DETAIL_HISTORY_REPORT requires at least one nmID")
        requested_dates = {
            (start + timedelta(days=offset)).isoformat()
            for offset in range((end - start).days + 1)
        }
        runtime = load_runtime_config(
            token_env_var=self._token_env_var,
            default_base_url=self._default_base_url,
            base_url_env_var=self._base_url_env_var,
            default_timeout_seconds=self._default_timeout_seconds,
        )
        report = self._csv_transport.fetch(
            base_url=runtime.base_url,
            token=runtime.token,
            timeout_seconds=runtime.timeout_seconds,
            report_type=self.REPORT_TYPE,
            report_name=f"wb-core buyout history {request.date_from}..{request.date_to}",
            params={
                "nmIDs": requested_nm_ids,
                "subjectIds": [],
                "brandNames": [],
                "tagIds": [],
                "startDate": request.date_from,
                "endDate": request.date_to,
                "timezone": self.TIMEZONE,
                "aggregationLevel": "day",
                "skipDeletedNm": False,
            },
        )
        parsed = self._parse_complete_rows(
            csv_rows=report.rows,
            requested_nm_ids=set(requested_nm_ids),
            requested_dates=requested_dates,
        )
        fetched_at = report.created_at or self._now_factory().astimezone(timezone.utc).replace(
            microsecond=0
        ).strftime("%Y-%m-%d %H:%M:%S")
        rows: list[list[Any]] = []
        for (snapshot_date, nm_id), metrics in sorted(parsed.items()):
            rows.append([fetched_at, snapshot_date, nm_id, "orderCount", metrics["orderCount"]])
            rows.append(
                [fetched_at, snapshot_date, nm_id, "buyoutPercent", metrics["buyoutPercent"]]
            )
        self._last_fetch_evidence = {
            "endpoint_chain": [
                "POST /api/v2/nm-report/downloads",
                "GET /api/v2/nm-report/downloads",
                "GET /api/v2/nm-report/downloads/file/{downloadId}",
            ],
            "report_type": self.REPORT_TYPE,
            "download_id": report.download_id,
            "report_name": report.report_name,
            "report_created_at": report.created_at,
            "csv_sha256": report.csv_sha256,
            "csv_row_count": len(report.rows),
            "covered_pair_count": len(parsed),
            "expected_pair_count": len(requested_nm_ids) * len(requested_dates),
            "enabled_nm_ids_sha256": "sha256:"
            + hashlib.sha256(
                json.dumps(requested_nm_ids, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "date_from": request.date_from,
            "date_to": request.date_to,
            "timezone": self.TIMEZONE,
            "aggregation_level": "day",
        }
        return {
            "date_from": request.date_from,
            "date_to": request.date_to,
            "requested_nm_ids": requested_nm_ids,
            "data": {"rows": rows},
        }

    def _parse_complete_rows(
        self,
        *,
        csv_rows: list[dict[str, str]],
        requested_nm_ids: set[int],
        requested_dates: set[str],
    ) -> dict[tuple[str, int], dict[str, float]]:
        if not csv_rows:
            raise RuntimeError("DETAIL_HISTORY_REPORT CSV contains no rows")
        missing_columns = self.REQUIRED_COLUMNS - set(csv_rows[0])
        if missing_columns:
            raise RuntimeError(
                "DETAIL_HISTORY_REPORT CSV is missing required columns: "
                + ", ".join(sorted(missing_columns))
            )
        parsed: dict[tuple[str, int], dict[str, float]] = {}
        for row_number, row in enumerate(csv_rows, start=2):
            try:
                nm_id = int(str(row.get("nmID") or "").strip())
            except ValueError as exc:
                raise RuntimeError(
                    f"DETAIL_HISTORY_REPORT row {row_number} has invalid nmID"
                ) from exc
            snapshot_date = str(row.get("dt") or "").strip()
            try:
                normalized_date = date.fromisoformat(snapshot_date).isoformat()
            except ValueError as exc:
                raise RuntimeError(
                    f"DETAIL_HISTORY_REPORT row {row_number} has invalid dt"
                ) from exc
            if nm_id not in requested_nm_ids or normalized_date not in requested_dates:
                raise RuntimeError(
                    f"DETAIL_HISTORY_REPORT row {row_number} is outside the requested SKU/date scope"
                )
            key = (normalized_date, nm_id)
            if key in parsed:
                raise RuntimeError(
                    f"DETAIL_HISTORY_REPORT contains duplicate SKU/date row at {row_number}"
                )
            order_count = _parse_detail_history_number(
                row.get("ordersCount"),
                field="ordersCount",
                row_number=row_number,
            )
            if order_count < 0 or not order_count.is_integer():
                raise RuntimeError(
                    f"DETAIL_HISTORY_REPORT row {row_number} ordersCount must be a nonnegative integer"
                )
            buyout_percent = _parse_detail_history_number(
                row.get("buyoutPercent"),
                field="buyoutPercent",
                row_number=row_number,
            )
            if not 0 <= buyout_percent <= 100:
                raise RuntimeError(
                    f"DETAIL_HISTORY_REPORT row {row_number} buyoutPercent must be in 0..100"
                )
            parsed[key] = {
                "orderCount": order_count,
                "buyoutPercent": buyout_percent,
            }
        expected = {
            (snapshot_date, nm_id)
            for snapshot_date in requested_dates
            for nm_id in requested_nm_ids
        }
        missing = expected - set(parsed)
        if missing:
            raise RuntimeError(
                "DETAIL_HISTORY_REPORT CSV has incomplete enabled-SKU/date coverage: "
                f"missing_pair_count={len(missing)}, expected_pair_count={len(expected)}"
            )
        return parsed


def _parse_detail_history_number(
    raw_value: Any,
    *,
    field: str,
    row_number: int,
) -> float:
    normalized = str(raw_value or "").strip().replace("\u00a0", "").replace(" ", "")
    if normalized.endswith("%"):
        normalized = normalized[:-1]
    normalized = normalized.replace(",", ".")
    try:
        value = Decimal(normalized)
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError(
            f"DETAIL_HISTORY_REPORT row {row_number} has invalid {field}"
        ) from exc
    if not value.is_finite():
        raise RuntimeError(f"DETAIL_HISTORY_REPORT row {row_number} has non-finite {field}")
    return float(value)


class _SalesFunnelHistoryHttpStatusError(RuntimeError):
    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"sales funnel history http {status_code}")
