"""Shared official Wildberries Finance transport and server-owned rate gate."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Iterator, Mapping
import urllib.error
import urllib.request


FINANCE_URL = "https://finance-api.wildberries.ru/api/finance/v1/sales-reports/detailed"
FINANCE_ENDPOINT = "POST /api/finance/v1/sales-reports/detailed"
DEFAULT_MIN_INTERVAL_SECONDS = 60.0
DEFAULT_DEADLINE_SECONDS = 240.0
DEFAULT_MAX_PAGES = 200


@dataclass(frozen=True)
class FinanceHttpResult:
    status: int
    rows: list[dict[str, Any]]
    headers: Mapping[str, str]


@dataclass(frozen=True)
class FinanceFetchResult:
    rows: list[dict[str, Any]]
    pages: int
    rrd_id_end: int
    terminal_status: int
    source_digest: str


class FinanceApiError(RuntimeError):
    """Typed, privacy-safe Finance acquisition failure."""

    def __init__(
        self,
        code: str,
        *,
        date_from: str,
        date_to: str,
        period: str,
        cursor: int,
        pages: int,
        http_status: int | None = None,
        next_retry_at: str | None = None,
        retry_after_seconds: float | None = None,
        header_hints: Mapping[str, str] | None = None,
        detail: str = "",
    ) -> None:
        self.code = code
        self.date_from = date_from
        self.date_to = date_to
        self.period = period
        self.cursor = int(cursor)
        self.pages = int(pages)
        self.http_status = http_status
        self.next_retry_at = next_retry_at
        self.retry_after_seconds = retry_after_seconds
        self.header_hints = dict(header_hints or {})
        self.detail = detail
        fields = [
            f"code={code}",
            f"endpoint={FINANCE_ENDPOINT}",
            f"date_from={date_from}",
            f"date_to={date_to}",
            f"period={period}",
            f"cursor={int(cursor)}",
            f"pages={int(pages)}",
        ]
        if http_status is not None:
            fields.append(f"http_status={int(http_status)}")
        if retry_after_seconds is not None:
            fields.append(f"retry_after_seconds={float(retry_after_seconds):g}")
        if next_retry_at:
            fields.append(f"next_retry_at={next_retry_at}")
        if self.header_hints:
            fields.append(
                "header_hints="
                + ",".join(f"{key}:{value}" for key, value in sorted(self.header_hints.items()))
            )
        if detail:
            fields.append(f"detail={detail}")
        super().__init__("; ".join(fields))


class FinanceRateLimited(FinanceApiError):
    pass


class FinanceRateGate:
    """One interprocess single-flight lease for the seller Finance endpoint.

    The lease is held for one complete pagination session. Before every HTTP
    request the next allowed timestamp is durably reserved, so a process crash
    cannot create an immediate second request.
    """

    def __init__(
        self,
        root: Path,
        *,
        account_key: str,
        min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
        wall_time: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.root = Path(root)
        self.account_fingerprint = hashlib.sha256(account_key.encode("utf-8")).hexdigest()[:24]
        self.min_interval_seconds = max(
            DEFAULT_MIN_INTERVAL_SECONDS, float(min_interval_seconds)
        )
        self.wall_time = wall_time
        self.sleep = sleep
        self.lock_path = self.root / ".wb-finance-api-rate-gate.lock"
        self.state_path = self.root / ".wb-finance-api-rate-gate.json"

    @contextmanager
    def session(self) -> Iterator["FinanceRateGateSession"]:
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_path.touch(mode=0o600, exist_ok=True)
        with self.lock_path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield FinanceRateGateSession(self)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _load_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        if not isinstance(payload, dict):
            return {}
        if str(payload.get("account_fingerprint") or "") not in {
            "",
            self.account_fingerprint,
        }:
            # A second account in one runtime must not inherit or overwrite an
            # unrelated account's lease state.
            raise RuntimeError("Finance rate gate account identity mismatch")
        return payload

    def _store_state(self, payload: Mapping[str, Any]) -> None:
        stable = {
            "contract_version": "wb_finance_api_rate_gate_v1",
            "endpoint": FINANCE_ENDPOINT,
            "account_fingerprint": self.account_fingerprint,
            "next_allowed_at_epoch": float(payload.get("next_allowed_at_epoch") or 0.0),
            "last_status": int(payload.get("last_status") or 0),
            "updated_at": _iso_from_epoch(self.wall_time()),
        }
        temporary = self.state_path.with_name(
            self.state_path.name + f".tmp.{os.getpid()}.{time.time_ns()}"
        )
        encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
            directory_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary.exists():
                temporary.unlink()


class FinanceRateGateSession:
    def __init__(self, gate: FinanceRateGate) -> None:
        self.gate = gate

    def before_request(self, *, max_wait_seconds: float | None = None) -> None:
        state = self.gate._load_state()
        now = self.gate.wall_time()
        wait_seconds = max(0.0, float(state.get("next_allowed_at_epoch") or 0.0) - now)
        if max_wait_seconds is not None and wait_seconds > max(0.0, max_wait_seconds):
            raise TimeoutError("Finance rate-gate wait exceeds acquisition deadline")
        if wait_seconds:
            self.gate.sleep(wait_seconds)
            now = self.gate.wall_time()
        # Reserve the ordinary one-minute cadence before transport begins.
        self.gate._store_state(
            {
                "next_allowed_at_epoch": now + self.gate.min_interval_seconds,
                "last_status": 0,
            }
        )

    def after_response(self, response: FinanceHttpResult) -> tuple[float, str]:
        now = self.gate.wall_time()
        hinted = parse_finance_retry_hint_seconds(response.headers, now_epoch=now)
        delay = max(self.gate.min_interval_seconds, hinted or 0.0)
        state = self.gate._load_state()
        self.gate._store_state(
            {
                "next_allowed_at_epoch": max(
                    float(state.get("next_allowed_at_epoch") or 0.0), now + delay
                ),
                "last_status": response.status,
            }
        )
        return delay, _iso_from_epoch(now + delay)


def parse_finance_retry_hint_seconds(
    headers: Mapping[str, Any], *, now_epoch: float | None = None
) -> float | None:
    normalized = {str(key).casefold(): str(value).strip() for key, value in headers.items()}
    now = time.time() if now_epoch is None else float(now_epoch)
    values: list[float] = []
    retry_after = normalized.get("retry-after", "")
    if retry_after:
        try:
            values.append(max(0.0, float(retry_after)))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(retry_after)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                values.append(max(0.0, parsed.timestamp() - now))
            except (TypeError, ValueError, OverflowError):
                pass
    raw_retry = normalized.get("x-ratelimit-retry", "")
    if raw_retry:
        try:
            value = float(raw_retry)
            if value > 10_000:
                value /= 1_000.0
            values.append(max(0.0, value))
        except ValueError:
            pass
    raw_reset = normalized.get("x-ratelimit-reset", "")
    if raw_reset:
        try:
            value = float(raw_reset)
            if value > 10_000_000_000:
                value /= 1_000.0
            if value > now:
                value -= now
            elif value >= 10_000:
                # WB rate headers commonly express relative durations in ms.
                value /= 1_000.0
            values.append(max(0.0, value))
        except ValueError:
            pass
    return max(values) if values else None


def finance_rate_header_hints(headers: Mapping[str, Any]) -> dict[str, str]:
    allowed = {"retry-after", "x-ratelimit-retry", "x-ratelimit-reset"}
    return {
        str(key).casefold(): str(value).strip()[:120]
        for key, value in headers.items()
        if str(key).casefold() in allowed and str(value).strip()
    }


class WbFinanceApiClient:
    """Canonical POST client with terminal-204 pagination and shared cadence."""

    def __init__(
        self,
        token: str,
        *,
        url: str = FINANCE_URL,
        limit: int = 100_000,
        min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
        max_retries: int = 0,
        sleep: Callable[[float], None] = time.sleep,
        request: Callable[[dict[str, Any]], FinanceHttpResult] | None = None,
        rate_gate_root: Path | None = None,
        wall_time: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
        max_pages: int = DEFAULT_MAX_PAGES,
        account_key: str | None = None,
    ) -> None:
        if not token:
            raise ValueError("WB_API_TOKEN is required for Finance API")
        self._token = token
        self.url = url
        self.limit = min(max(1, int(limit)), 100_000)
        self.min_interval_seconds = max(
            DEFAULT_MIN_INTERVAL_SECONDS, float(min_interval_seconds)
        )
        # Kept only as a compatibility attribute. 429 is intentionally never
        # retried inside the acquisition session.
        self.max_retries = max(0, int(max_retries))
        self.sleep = sleep
        self._request_override = request
        self._monotonic = monotonic
        self.deadline_seconds = max(0.001, float(deadline_seconds))
        self.max_pages = max(1, int(max_pages))
        gate_root = rate_gate_root or Path(
            os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR", ".runtime/registry_upload")
        )
        self.rate_gate = FinanceRateGate(
            gate_root,
            account_key=(
                str(account_key or "").strip()
                or str(os.environ.get("SELLER_PORTAL_CANONICAL_SUPPLIER_ID") or "").strip()
                or "canonical-hosted-finance-account"
            ),
            min_interval_seconds=self.min_interval_seconds,
            wall_time=wall_time,
            sleep=sleep,
        )

    def fetch_week(self, date_from: date, date_to: date) -> list[dict[str, Any]]:
        return self.fetch_report(
            date_from=date_from.isoformat(),
            date_to=date_to.isoformat(),
            period="weekly",
        ).rows

    def fetch_report(
        self, *, date_from: str, date_to: str, period: str
    ) -> FinanceFetchResult:
        if period not in {"daily", "weekly"}:
            raise ValueError("Finance period must be daily or weekly")
        try:
            normalized_from = date.fromisoformat(str(date_from)).isoformat()
            normalized_to = date.fromisoformat(str(date_to)).isoformat()
        except ValueError as exc:
            raise ValueError("Finance date bounds must be YYYY-MM-DD") from exc
        if normalized_from > normalized_to:
            raise ValueError("Finance dateFrom must not be after dateTo")
        date_from = normalized_from
        date_to = normalized_to
        all_rows: list[dict[str, Any]] = []
        rrd_id = 0
        pages = 0
        seen_cursors: set[int] = set()
        started = self._monotonic()
        with self.rate_gate.session() as gate:
            while True:
                if self._monotonic() - started > self.deadline_seconds:
                    raise FinanceApiError(
                        "deadline",
                        date_from=date_from,
                        date_to=date_to,
                        period=period,
                        cursor=rrd_id,
                        pages=pages,
                    )
                if pages >= self.max_pages:
                    raise FinanceApiError(
                        "max_pages",
                        date_from=date_from,
                        date_to=date_to,
                        period=period,
                        cursor=rrd_id,
                        pages=pages,
                    )
                payload = {
                    "dateFrom": date_from,
                    "dateTo": date_to,
                    "limit": self.limit,
                    "rrdId": rrd_id,
                    "period": period,
                }
                remaining = self.deadline_seconds - (self._monotonic() - started)
                try:
                    gate.before_request(max_wait_seconds=remaining)
                except TimeoutError as exc:
                    raise FinanceApiError(
                        "deadline",
                        date_from=date_from,
                        date_to=date_to,
                        period=period,
                        cursor=rrd_id,
                        pages=pages,
                    ) from exc
                try:
                    response = self._request(payload)
                except FinanceApiError:
                    raise
                except Exception as exc:
                    raise FinanceApiError(
                        "transport_error",
                        date_from=date_from,
                        date_to=date_to,
                        period=period,
                        cursor=rrd_id,
                        pages=pages,
                        detail=type(exc).__name__,
                    ) from exc
                retry_after, next_retry_at = gate.after_response(response)
                if response.status == 429:
                    raise FinanceRateLimited(
                        "rate_limited",
                        date_from=date_from,
                        date_to=date_to,
                        period=period,
                        cursor=rrd_id,
                        pages=pages,
                        http_status=429,
                        next_retry_at=next_retry_at,
                        retry_after_seconds=retry_after,
                        header_hints=finance_rate_header_hints(response.headers),
                    )
                if response.status == 204:
                    return FinanceFetchResult(
                        rows=all_rows,
                        pages=pages,
                        rrd_id_end=rrd_id,
                        terminal_status=204,
                        source_digest=_rows_digest(all_rows),
                    )
                if response.status != 200:
                    raise FinanceApiError(
                        "http_error",
                        date_from=date_from,
                        date_to=date_to,
                        period=period,
                        cursor=rrd_id,
                        pages=pages,
                        http_status=response.status,
                    )
                if not response.rows:
                    raise FinanceApiError(
                        "partial_report",
                        date_from=date_from,
                        date_to=date_to,
                        period=period,
                        cursor=rrd_id,
                        pages=pages,
                        http_status=200,
                        detail="empty_200_without_terminal_204",
                    )
                pages += 1
                all_rows.extend(dict(row) for row in response.rows)
                try:
                    next_cursor = int(str(response.rows[-1].get("rrdId") or "0"))
                except ValueError as exc:
                    raise FinanceApiError(
                        "invalid_cursor",
                        date_from=date_from,
                        date_to=date_to,
                        period=period,
                        cursor=rrd_id,
                        pages=pages,
                    ) from exc
                if (
                    next_cursor <= rrd_id
                    or next_cursor in seen_cursors
                ):
                    raise FinanceApiError(
                        "stuck_cursor",
                        date_from=date_from,
                        date_to=date_to,
                        period=period,
                        cursor=rrd_id,
                        pages=pages,
                        detail=f"next_cursor={next_cursor}",
                    )
                seen_cursors.add(next_cursor)
                rrd_id = next_cursor

    def _request(self, payload: dict[str, Any]) -> FinanceHttpResult:
        if self._request_override is not None:
            return self._request_override(payload)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={
                "Authorization": self._token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                raw = response.read()
                parsed = json.loads(raw) if raw else []
                if parsed and not isinstance(parsed, list):
                    raise ValueError("Finance API expected an array payload")
                return FinanceHttpResult(
                    int(response.status),
                    [dict(row) for row in parsed if isinstance(row, Mapping)],
                    dict(response.headers.items()),
                )
        except urllib.error.HTTPError as exc:
            # Raw error bodies can contain provider detail and are deliberately
            # discarded at this boundary.
            exc.read()
            return FinanceHttpResult(
                int(exc.code), [], dict(exc.headers.items()) if exc.headers else {}
            )
        except urllib.error.URLError as exc:
            # The pagination owner attaches the exact page/cursor position so a
            # mid-report transport failure is never flattened to page zero.
            raise OSError(f"transport={type(exc.reason).__name__}") from exc

    def __repr__(self) -> str:
        return f"WbFinanceApiClient(url={self.url!r}, token=<redacted>)"


def _rows_digest(rows: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _iso_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
