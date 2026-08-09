"""Shared transport for official WB Seller Analytics CSV reports."""

from __future__ import annotations

import csv
from dataclasses import dataclass
import hashlib
import io
import json
import time
from typing import Any, Callable, Mapping
from urllib import error, request as urllib_request
import uuid
import zipfile


@dataclass(frozen=True)
class SellerAnalyticsCsvReport:
    download_id: str
    report_name: str
    created_at: str
    rows: list[dict[str, str]]
    csv_sha256: str


class SellerAnalyticsCsvHttpStatusError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        body: str,
        headers: Mapping[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.headers = headers or {}
        super().__init__(f"seller analytics csv http {status_code}")


class SellerAnalyticsCsvReportTransport:
    """Create, poll and download one official Seller Analytics CSV report."""

    def __init__(
        self,
        *,
        poll_interval_seconds: float = 2.0,
        max_poll_attempts: int = 120,
        max_retries_on_429: int = 3,
        opener: Callable[..., Any] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        time_fn: Callable[[], float] | None = None,
        uuid_factory: Callable[[], str] | None = None,
    ) -> None:
        self._poll_interval_seconds = max(0.1, poll_interval_seconds)
        self._max_poll_attempts = max(1, max_poll_attempts)
        self._max_retries_on_429 = max(0, max_retries_on_429)
        self._opener = opener or urllib_request.urlopen
        self._sleep = sleep_fn or time.sleep
        self._time = time_fn or time.time
        self._uuid_factory = uuid_factory or (lambda: str(uuid.uuid4()))

    def fetch(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float,
        report_type: str,
        report_name: str,
        params: Mapping[str, Any],
    ) -> SellerAnalyticsCsvReport:
        download_id = self._uuid_factory()
        effective_name = f"{report_name} [{download_id[:8]}]"
        self._create_report(
            base_url=base_url,
            token=token,
            timeout_seconds=timeout_seconds,
            download_id=download_id,
            report_type=report_type,
            report_name=effective_name,
            params=params,
        )
        report_meta = self._poll_report_ready(
            base_url=base_url,
            token=token,
            timeout_seconds=timeout_seconds,
            download_id=download_id,
        )
        csv_bytes = self._download_report(
            base_url=base_url,
            token=token,
            timeout_seconds=timeout_seconds,
            download_id=download_id,
        )
        return SellerAnalyticsCsvReport(
            download_id=download_id,
            report_name=str(report_meta.get("name") or effective_name),
            created_at=str(report_meta.get("createdAt") or ""),
            rows=parse_csv_dict_rows(decode_csv_bytes(csv_bytes)),
            csv_sha256="sha256:" + hashlib.sha256(csv_bytes).hexdigest(),
        )

    def _create_report(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float,
        download_id: str,
        report_type: str,
        report_name: str,
        params: Mapping[str, Any],
    ) -> None:
        req = urllib_request.Request(
            url=f"{base_url.rstrip('/')}/api/v2/nm-report/downloads",
            data=json.dumps(
                {
                    "id": download_id,
                    "reportType": report_type,
                    "userReportName": report_name,
                    "params": dict(params),
                }
            ).encode("utf-8"),
            method="POST",
            headers={"Authorization": token, "Content-Type": "application/json"},
        )
        body = self._open_with_429_retry(
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
            raise RuntimeError(f"seller analytics csv create-report failed: {payload}")

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
                url=f"{base_url.rstrip('/')}/api/v2/nm-report/downloads",
                method="GET",
                headers={"Authorization": token},
            )
            payload = json.loads(
                self._open_with_429_retry(
                    req,
                    timeout_seconds=timeout_seconds,
                    action_label="poll",
                ).decode("utf-8")
            )
            report = find_download_report(payload, download_id)
            if report is None:
                if attempt + 1 >= self._max_poll_attempts:
                    raise RuntimeError(f"seller analytics csv report {download_id} was not listed")
                self._sleep(self._poll_interval_seconds)
                continue
            status = str(report.get("status") or "").upper()
            if status == "SUCCESS":
                return report
            if status in {"FAILED", "ERROR", "CANCELLED", "CANCELED"}:
                raise RuntimeError(
                    f"seller analytics csv report {download_id} failed with status {status}: {report}"
                )
            if attempt + 1 >= self._max_poll_attempts:
                raise RuntimeError(
                    f"seller analytics csv report {download_id} did not finish within bounded polling window"
                )
            self._sleep(self._poll_interval_seconds)
        raise RuntimeError(f"seller analytics csv report {download_id} polling exhausted")

    def _download_report(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float,
        download_id: str,
    ) -> bytes:
        req = urllib_request.Request(
            url=f"{base_url.rstrip('/')}/api/v2/nm-report/downloads/file/{download_id}",
            method="GET",
            headers={"Authorization": token},
        )
        payload = self._open_with_429_retry(
            req,
            timeout_seconds=timeout_seconds,
            action_label="download",
        )
        return extract_csv_bytes(payload)

    def _open_with_429_retry(
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
                body = exc.read().decode("utf-8", errors="replace")
                wrapped = SellerAnalyticsCsvHttpStatusError(
                    exc.code,
                    body,
                    headers=exc.headers or {},
                )
                if wrapped.status_code == 429 and attempt < self._max_retries_on_429:
                    attempt += 1
                    self._sleep(self._resolve_retry_wait_seconds(wrapped.headers))
                    continue
                raise wrapped from exc
            except error.URLError as exc:
                raise RuntimeError(
                    f"seller analytics csv {action_label} transport failed: {exc}"
                ) from exc

    def _resolve_retry_wait_seconds(self, headers: Mapping[str, Any]) -> float:
        retry_seconds = _parse_positive_float(headers.get("X-Ratelimit-Retry"))
        reset_seconds = _parse_reset_header_seconds(
            headers.get("X-Ratelimit-Reset"),
            now_epoch=self._time(),
        )
        fallback_seconds = _parse_positive_float(headers.get("Retry-After"))
        return max(
            self._poll_interval_seconds,
            retry_seconds,
            reset_seconds,
            fallback_seconds,
        )


def find_download_report(payload: Any, download_id: str) -> Mapping[str, Any] | None:
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
        if isinstance(candidate, Mapping) and str(candidate.get("id") or "") == download_id:
            return candidate
    return None


def extract_csv_bytes(payload: bytes) -> bytes:
    buffer = io.BytesIO(payload)
    if not zipfile.is_zipfile(buffer):
        return payload
    with zipfile.ZipFile(buffer, "r") as archive:
        candidate_names = [name for name in archive.namelist() if not name.endswith("/")]
        if not candidate_names:
            raise RuntimeError("seller analytics csv report archive is empty")
        csv_names = [name for name in candidate_names if name.lower().endswith(".csv")]
        if len(csv_names) != 1:
            raise RuntimeError(
                f"seller analytics csv report must contain exactly one CSV file, got {len(csv_names)}"
            )
        return archive.read(csv_names[0])


def decode_csv_bytes(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise RuntimeError("seller analytics csv bytes could not be decoded")


def parse_csv_dict_rows(csv_text: str) -> list[dict[str, str]]:
    lines = csv_text.splitlines()
    if not lines:
        return []
    delimiter = ";" if lines[0].count(";") > lines[0].count(",") else ","
    reader = csv.DictReader(io.StringIO(csv_text), delimiter=delimiter)
    if not reader.fieldnames:
        raise RuntimeError("seller analytics csv header is missing")
    return [
        {str(key or "").strip(): str(value or "").strip() for key, value in row.items()}
        for row in reader
    ]


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
