"""Weekly Wildberries Finance report storage, aggregation and synchronization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Iterable, Mapping
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo


FINANCE_URL = "https://finance-api.wildberries.ru/api/finance/v1/sales-reports/detailed"
CLASSIFIER_VERSION = "wb_finance_weekly_classifier_v1"
MOSCOW = ZoneInfo("Europe/Moscow")
ZERO = Decimal("0")
MONEY_QUANT = Decimal("0.0001")
FIRST_INCLUDED_DATE = date(2026, 1, 1)


def _decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return ZERO
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return ZERO


def _money_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.quantize(MONEY_QUANT), "f")


def _ratio(numerator: Decimal | None, denominator: Decimal) -> Decimal | None:
    if numerator is None or denominator == ZERO:
        return None
    return numerator / denominator * Decimal("100")


def week_bounds(day: date) -> tuple[date, date]:
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def historical_week_bounds(today: date | None = None) -> list[tuple[date, date]]:
    now = today or datetime.now(MOSCOW).date()
    first_start, _ = week_bounds(FIRST_INCLUDED_DATE)
    current_start, _ = week_bounds(now)
    latest_closed_end = current_start - timedelta(days=1)
    result: list[tuple[date, date]] = []
    cursor = first_start
    while cursor + timedelta(days=6) <= latest_closed_end:
        result.append((cursor, cursor + timedelta(days=6)))
        cursor += timedelta(days=7)
    return result


@dataclass(frozen=True)
class FinanceHttpResult:
    status: int
    rows: list[dict[str, Any]]
    headers: Mapping[str, str]


class WbFinanceApiClient:
    """Official Finance API client with rrdId pagination and rate-limit handling."""

    def __init__(
        self,
        token: str,
        *,
        url: str = FINANCE_URL,
        limit: int = 100_000,
        min_interval_seconds: float = 60.0,
        max_retries: int = 8,
        sleep: Callable[[float], None] = time.sleep,
        request: Callable[[dict[str, Any]], FinanceHttpResult] | None = None,
    ) -> None:
        if not token:
            raise ValueError("WB_API_TOKEN is required for Finance API")
        self._token = token
        self.url = url
        self.limit = min(max(1, int(limit)), 100_000)
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self.max_retries = max(0, int(max_retries))
        self.sleep = sleep
        self._request_override = request
        self._last_request_at = 0.0

    def fetch_week(self, date_from: date, date_to: date) -> list[dict[str, Any]]:
        all_rows: list[dict[str, Any]] = []
        rrd_id = 0
        seen_cursors: set[int] = set()
        while True:
            payload = {
                "dateFrom": date_from.isoformat(),
                "dateTo": date_to.isoformat(),
                "limit": self.limit,
                "rrdId": rrd_id,
                "period": "weekly",
            }
            response = self._request_with_retry(payload)
            if response.status == 204:
                break
            if response.status != 200:
                raise RuntimeError(f"Finance API unexpected HTTP {response.status}")
            rows = response.rows
            if not rows:
                break
            all_rows.extend(rows)
            next_cursor = int(str(rows[-1].get("rrdId") or "0"))
            if next_cursor <= 0 or next_cursor == rrd_id or next_cursor in seen_cursors:
                raise RuntimeError("Finance API pagination cursor did not advance")
            seen_cursors.add(next_cursor)
            rrd_id = next_cursor
        return all_rows

    def _request_with_retry(self, payload: dict[str, Any]) -> FinanceHttpResult:
        attempt = 0
        while True:
            elapsed = time.monotonic() - self._last_request_at
            if self._last_request_at and elapsed < self.min_interval_seconds:
                self.sleep(self.min_interval_seconds - elapsed)
            self._last_request_at = time.monotonic()
            response = self._request(payload)
            if response.status != 429:
                return response
            if attempt >= self.max_retries:
                raise RuntimeError("Finance API rate limit retry budget exhausted")
            attempt += 1
            raw_retry = str(
                response.headers.get("X-Ratelimit-Retry")
                or response.headers.get("Retry-After")
                or "60"
            )
            try:
                retry_seconds = float(raw_retry)
            except ValueError:
                retry_seconds = 60.0
            if retry_seconds > 10_000:
                retry_seconds /= 1_000.0
            self.sleep(max(self.min_interval_seconds, retry_seconds, 1.0))

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
                rows = json.loads(raw) if raw else []
                return FinanceHttpResult(
                    int(response.status), list(rows), dict(response.headers.items())
                )
        except urllib.error.HTTPError as exc:
            exc.read()
            return FinanceHttpResult(
                int(exc.code), [], dict(exc.headers.items()) if exc.headers else {}
            )

    def __repr__(self) -> str:
        return f"WbFinanceApiClient(url={self.url!r}, token=<redacted>)"


def classify_deduction(row: Mapping[str, Any]) -> str:
    """Versioned, single-bucket classifier for deduction rows."""
    name = " ".join(
        str(row.get(key) or "")
        for key in ("bonusTypeName", "sellerOperName", "paymentProcessing")
    ).casefold()
    if any(
        token in name for token in ("wb продвиж", "продвижен", "реклам", "маркетинг")
    ):
        return "marketing"
    if "транзит" in name and any(token in name for token in ("логист", "достав")):
        return "transit_logistics"
    if any(token in name for token in ("подписк", "джем", "jamm")):
        return "subscriptions"
    if any(token in name for token in ("платн", "сервис")):
        return "paid_services"
    return "other_deductions"


class WbFinanceWeeklyBlock:
    def __init__(
        self,
        runtime_dir: Path,
        *,
        seller_id: str = "canonical",
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.db_path = self.runtime_dir / "registry_upload_runtime.sqlite3"
        self.seller_id = seller_id or "canonical"
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))

    def ensure_schema(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS wb_finance_weekly_reports (
                    seller_id TEXT NOT NULL, report_id TEXT NOT NULL, report_type INTEGER,
                    week_start TEXT NOT NULL, week_end TEXT NOT NULL, create_date TEXT,
                    currency TEXT, row_count INTEGER NOT NULL, content_hash TEXT NOT NULL,
                    first_loaded_at TEXT NOT NULL, last_synced_at TEXT NOT NULL,
                    PRIMARY KEY (seller_id, report_id)
                );
                CREATE TABLE IF NOT EXISTS wb_finance_weekly_raw_rows (
                    seller_id TEXT NOT NULL, report_id TEXT NOT NULL, rrd_id TEXT NOT NULL,
                    report_type INTEGER, week_start TEXT NOT NULL, week_end TEXT NOT NULL,
                    nm_id TEXT, vendor_code TEXT, barcode TEXT, doc_type_name TEXT,
                    seller_oper_name TEXT, row_hash TEXT NOT NULL, raw_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY (seller_id, report_id, rrd_id)
                );
                CREATE INDEX IF NOT EXISTS wb_finance_raw_by_week
                ON wb_finance_weekly_raw_rows(seller_id, week_start, week_end);
                CREATE TABLE IF NOT EXISTS wb_finance_weekly_sync (
                    seller_id TEXT NOT NULL, week_start TEXT NOT NULL, week_end TEXT NOT NULL,
                    status TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
                    first_loaded_at TEXT, last_synced_at TEXT, next_retry_at TEXT,
                    report_count INTEGER NOT NULL DEFAULT 0, raw_row_count INTEGER NOT NULL DEFAULT 0,
                    content_hash TEXT, unchanged_sync_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT, PRIMARY KEY (seller_id, week_start, week_end)
                );
                CREATE TABLE IF NOT EXISTS wb_finance_weekly_aggregates (
                    seller_id TEXT NOT NULL, week_start TEXT NOT NULL, week_end TEXT NOT NULL,
                    classifier_version TEXT NOT NULL, metrics_json TEXT NOT NULL,
                    report_ids_json TEXT NOT NULL, report_types_json TEXT NOT NULL,
                    unknown_reasons_json TEXT NOT NULL, calculated_at TEXT NOT NULL,
                    PRIMARY KEY (seller_id, week_start, week_end)
                );
                CREATE TABLE IF NOT EXISTS wb_finance_weekly_reconciliation (
                    seller_id TEXT NOT NULL, week_start TEXT NOT NULL, week_end TEXT NOT NULL,
                    status TEXT NOT NULL, difference_rub TEXT, detail_json TEXT NOT NULL,
                    checked_at TEXT NOT NULL, PRIMARY KEY (seller_id, week_start, week_end)
                );
                CREATE TABLE IF NOT EXISTS wb_finance_weekly_cost_coverage (
                    seller_id TEXT NOT NULL, week_start TEXT NOT NULL, week_end TEXT NOT NULL,
                    matched_units INTEGER NOT NULL, unmatched_units INTEGER NOT NULL,
                    coverage_pct TEXT, cogs_rub TEXT, problem_skus_json TEXT NOT NULL,
                    calculated_at TEXT NOT NULL, PRIMARY KEY (seller_id, week_start, week_end)
                );
                """
            )
            conn.commit()

    def sync_week(
        self, week_start: date, week_end: date, client: WbFinanceApiClient
    ) -> dict[str, Any]:
        self.ensure_schema()
        now = (
            self.now_factory()
            .astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        self._mark_loading(week_start, week_end, now)
        try:
            rows = client.fetch_week(week_start, week_end)
            if not rows:
                with self._connect() as conn:
                    conn.execute(
                        """UPDATE wb_finance_weekly_sync SET status='waiting',last_synced_at=?,next_retry_at=?,
                        report_count=0,raw_row_count=0,last_error=NULL WHERE seller_id=? AND week_start=? AND week_end=?""",
                        (
                            now,
                            (
                                self.now_factory().astimezone(timezone.utc)
                                + timedelta(hours=1)
                            )
                            .isoformat()
                            .replace("+00:00", "Z"),
                            self.seller_id,
                            week_start.isoformat(),
                            week_end.isoformat(),
                        ),
                    )
                    conn.commit()
                return {
                    "status": "waiting",
                    "week_start": week_start.isoformat(),
                    "week_end": week_end.isoformat(),
                    "report_count": 0,
                    "raw_row_count": 0,
                }
            result = self.ingest_week(week_start, week_end, rows, synced_at=now)
            return result
        except Exception as exc:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO wb_finance_weekly_sync
                    (seller_id,week_start,week_end,status,attempt_count,last_synced_at,last_error)
                    VALUES (?,?,?,'error_loading',1,?,?)
                    ON CONFLICT(seller_id,week_start,week_end) DO UPDATE SET
                    status='error_loading', attempt_count=attempt_count+1,
                    last_synced_at=excluded.last_synced_at,last_error=excluded.last_error""",
                    (
                        self.seller_id,
                        week_start.isoformat(),
                        week_end.isoformat(),
                        now,
                        str(exc)[:2000],
                    ),
                )
                conn.commit()
            raise

    def ingest_week(
        self,
        week_start: date,
        week_end: date,
        rows: Iterable[Mapping[str, Any]],
        *,
        synced_at: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_schema()
        synced = synced_at or self.now_factory().astimezone(
            timezone.utc
        ).isoformat().replace("+00:00", "Z")
        normalized_rows = [dict(row) for row in rows]
        by_report: dict[str, list[dict[str, Any]]] = {}
        for row in normalized_rows:
            report_id = str(row.get("reportId") or "")
            rrd_id = str(row.get("rrdId") or "")
            if not report_id or not rrd_id:
                raise ValueError("Finance row must contain reportId and rrdId")
            by_report.setdefault(report_id, []).append(row)
        full_hash = hashlib.sha256(
            "\n".join(sorted(self._row_hash(row) for row in normalized_rows)).encode(
                "utf-8"
            )
        ).hexdigest()
        with self._connect() as conn:
            previous = conn.execute(
                "SELECT content_hash,unchanged_sync_count,first_loaded_at FROM wb_finance_weekly_sync WHERE seller_id=? AND week_start=? AND week_end=?",
                (self.seller_id, week_start.isoformat(), week_end.isoformat()),
            ).fetchone()
            for report_id, report_rows in by_report.items():
                first = report_rows[0]
                report_hash = hashlib.sha256(
                    "\n".join(
                        sorted(self._row_hash(row) for row in report_rows)
                    ).encode("utf-8")
                ).hexdigest()
                conn.execute(
                    """INSERT INTO wb_finance_weekly_reports
                    (seller_id,report_id,report_type,week_start,week_end,create_date,currency,row_count,content_hash,first_loaded_at,last_synced_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(seller_id,report_id) DO UPDATE SET
                    report_type=excluded.report_type,week_start=excluded.week_start,week_end=excluded.week_end,
                    create_date=excluded.create_date,currency=excluded.currency,row_count=excluded.row_count,
                    content_hash=excluded.content_hash,last_synced_at=excluded.last_synced_at""",
                    (
                        self.seller_id,
                        report_id,
                        int(first.get("reportType") or 0),
                        week_start.isoformat(),
                        week_end.isoformat(),
                        str(first.get("createDate") or ""),
                        str(first.get("currency") or "RUB"),
                        len(report_rows),
                        report_hash,
                        synced,
                        synced,
                    ),
                )
                for row in report_rows:
                    raw_json = json.dumps(
                        row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                    conn.execute(
                        """INSERT INTO wb_finance_weekly_raw_rows
                        (seller_id,report_id,rrd_id,report_type,week_start,week_end,nm_id,vendor_code,barcode,doc_type_name,seller_oper_name,row_hash,raw_json,first_seen_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(seller_id,report_id,rrd_id) DO UPDATE SET
                        report_type=excluded.report_type,week_start=excluded.week_start,week_end=excluded.week_end,
                        nm_id=excluded.nm_id,vendor_code=excluded.vendor_code,barcode=excluded.barcode,
                        doc_type_name=excluded.doc_type_name,seller_oper_name=excluded.seller_oper_name,
                        row_hash=excluded.row_hash,raw_json=excluded.raw_json,updated_at=excluded.updated_at""",
                        (
                            self.seller_id,
                            report_id,
                            str(row.get("rrdId")),
                            int(row.get("reportType") or 0),
                            week_start.isoformat(),
                            week_end.isoformat(),
                            str(row.get("nmId") or ""),
                            str(row.get("vendorCode") or ""),
                            str(row.get("sku") or ""),
                            str(row.get("docTypeName") or ""),
                            str(row.get("sellerOperName") or ""),
                            self._row_hash(row),
                            raw_json,
                            synced,
                            synced,
                        ),
                    )
            if by_report:
                placeholders = ",".join("?" for _ in by_report)
                conn.execute(
                    f"DELETE FROM wb_finance_weekly_raw_rows WHERE seller_id=? AND week_start=? AND week_end=? AND report_id NOT IN ({placeholders})",
                    (
                        self.seller_id,
                        week_start.isoformat(),
                        week_end.isoformat(),
                        *by_report.keys(),
                    ),
                )
                conn.execute(
                    f"DELETE FROM wb_finance_weekly_reports WHERE seller_id=? AND week_start=? AND week_end=? AND report_id NOT IN ({placeholders})",
                    (
                        self.seller_id,
                        week_start.isoformat(),
                        week_end.isoformat(),
                        *by_report.keys(),
                    ),
                )
            unchanged = (
                int(previous["unchanged_sync_count"] or 0) + 1
                if previous and previous["content_hash"] == full_hash
                else 0
            )
            status = "completed" if unchanged >= 1 else "loaded_preliminary"
            first_loaded = (
                previous["first_loaded_at"]
                if previous and previous["first_loaded_at"]
                else synced
            )
            conn.execute(
                """INSERT INTO wb_finance_weekly_sync
                (seller_id,week_start,week_end,status,attempt_count,first_loaded_at,last_synced_at,next_retry_at,
                 report_count,raw_row_count,content_hash,unchanged_sync_count,last_error)
                VALUES (?,?,?,?,1,?,?,?, ?,?,?,?,NULL)
                ON CONFLICT(seller_id,week_start,week_end) DO UPDATE SET
                status=excluded.status,attempt_count=attempt_count+1,first_loaded_at=COALESCE(first_loaded_at,excluded.first_loaded_at),
                last_synced_at=excluded.last_synced_at,next_retry_at=excluded.next_retry_at,
                report_count=excluded.report_count,raw_row_count=excluded.raw_row_count,
                content_hash=excluded.content_hash,unchanged_sync_count=excluded.unchanged_sync_count,last_error=NULL""",
                (
                    self.seller_id,
                    week_start.isoformat(),
                    week_end.isoformat(),
                    status,
                    first_loaded,
                    synced,
                    (self.now_factory().astimezone(timezone.utc) + timedelta(hours=1))
                    .isoformat()
                    .replace("+00:00", "Z")
                    if status != "completed"
                    else None,
                    len(by_report),
                    len(normalized_rows),
                    full_hash,
                    unchanged,
                ),
            )
            conn.commit()
        aggregate = self.recalculate_week(week_start, week_end)
        return {
            "status": status,
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "report_count": len(by_report),
            "raw_row_count": len(normalized_rows),
            "aggregate": aggregate,
        }

    def recalculate_week(self, week_start: date, week_end: date) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as conn:
            db_rows = conn.execute(
                "SELECT raw_json FROM wb_finance_weekly_raw_rows WHERE seller_id=? AND week_start=? AND week_end=? ORDER BY report_id,rrd_id",
                (self.seller_id, week_start.isoformat(), week_end.isoformat()),
            ).fetchall()
            rows = [json.loads(row["raw_json"]) for row in db_rows]
            aggregate, coverage, unknown = self._aggregate_rows(conn, rows, week_start)
            reports = conn.execute(
                "SELECT report_id,report_type FROM wb_finance_weekly_reports WHERE seller_id=? AND week_start=? AND week_end=? ORDER BY report_id",
                (self.seller_id, week_start.isoformat(), week_end.isoformat()),
            ).fetchall()
            now = (
                self.now_factory()
                .astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
            conn.execute(
                """INSERT OR REPLACE INTO wb_finance_weekly_aggregates
                (seller_id,week_start,week_end,classifier_version,metrics_json,report_ids_json,report_types_json,unknown_reasons_json,calculated_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    self.seller_id,
                    week_start.isoformat(),
                    week_end.isoformat(),
                    CLASSIFIER_VERSION,
                    json.dumps(aggregate, ensure_ascii=False),
                    json.dumps([r["report_id"] for r in reports]),
                    json.dumps([int(r["report_type"] or 0) for r in reports]),
                    json.dumps(unknown, ensure_ascii=False),
                    now,
                ),
            )
            conn.execute(
                """INSERT OR REPLACE INTO wb_finance_weekly_cost_coverage
                (seller_id,week_start,week_end,matched_units,unmatched_units,coverage_pct,cogs_rub,problem_skus_json,calculated_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    self.seller_id,
                    week_start.isoformat(),
                    week_end.isoformat(),
                    coverage["matched_units"],
                    coverage["unmatched_units"],
                    coverage["coverage_pct"],
                    coverage["cogs_rub"],
                    json.dumps(coverage["problem_skus"], ensure_ascii=False),
                    now,
                ),
            )
            expected_for_pay = sum(
                (
                    _decimal(row.get("forPay"))
                    if str(row.get("docTypeName") or "").casefold() == "продажа"
                    else -_decimal(row.get("forPay"))
                    if str(row.get("docTypeName") or "").casefold() == "возврат"
                    else ZERO
                    for row in rows
                ),
                ZERO,
            )
            actual_for_pay = _decimal(aggregate["to_seller"])
            diff = actual_for_pay - expected_for_pay
            reconcile_status = "ok" if abs(diff) <= Decimal("0.01") else "error"
            conn.execute(
                """INSERT OR REPLACE INTO wb_finance_weekly_reconciliation
                (seller_id,week_start,week_end,status,difference_rub,detail_json,checked_at) VALUES (?,?,?,?,?,?,?)""",
                (
                    self.seller_id,
                    week_start.isoformat(),
                    week_end.isoformat(),
                    reconcile_status,
                    _money_text(diff),
                    json.dumps(
                        {
                            "raw_for_pay_sum": _money_text(expected_for_pay),
                            "aggregate_to_seller": aggregate["to_seller"],
                        }
                    ),
                    now,
                ),
            )
            if coverage["unmatched_units"] != 0:
                conn.execute(
                    "UPDATE wb_finance_weekly_sync SET status='incomplete_cost' WHERE seller_id=? AND week_start=? AND week_end=? AND status<>'error_loading'",
                    (self.seller_id, week_start.isoformat(), week_end.isoformat()),
                )
            else:
                conn.execute(
                    "UPDATE wb_finance_weekly_sync SET status='completed' WHERE seller_id=? AND week_start=? AND week_end=? AND status='incomplete_cost'",
                    (self.seller_id, week_start.isoformat(), week_end.isoformat()),
                )
            conn.commit()
            return aggregate

    def _aggregate_rows(
        self, conn: sqlite3.Connection, rows: list[dict[str, Any]], week_start: date
    ) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
        values: dict[str, Decimal] = {
            key: ZERO
            for key in (
                "sales_qty",
                "returns_qty",
                "revenue_before_returns",
                "returns_amount",
                "commission",
                "acquiring",
                "logistics",
                "storage",
                "acceptance",
                "marketing",
                "transit_logistics",
                "penalties",
                "subscriptions",
                "paid_services",
                "other_deductions",
                "positive_adjustments",
                "corrections",
                "to_seller",
            )
        }
        unknown: set[str] = set()
        for row in rows:
            doc = str(row.get("docTypeName") or "").casefold()
            quantity = _decimal(row.get("quantity"))
            revenue = _decimal(row.get("retailPriceWithDisc"))
            if doc == "продажа":
                values["sales_qty"] += quantity
                values["revenue_before_returns"] += revenue
                values["commission"] += revenue - _decimal(row.get("forPay"))
                values["acquiring"] += _decimal(row.get("acquiringFee"))
                values["to_seller"] += _decimal(row.get("forPay"))
            elif doc == "возврат":
                values["returns_qty"] += quantity
                values["returns_amount"] += revenue
                values["commission"] -= revenue - _decimal(row.get("forPay"))
                values["acquiring"] -= _decimal(row.get("acquiringFee"))
                values["to_seller"] -= _decimal(row.get("forPay"))
            values["logistics"] += _decimal(row.get("deliveryService"))
            values["storage"] += _decimal(row.get("paidStorage"))
            values["acceptance"] += _decimal(row.get("paidAcceptance"))
            values["penalties"] += _decimal(row.get("penalty"))
            deduction = _decimal(row.get("deduction"))
            if deduction:
                bucket = classify_deduction(row)
                values[bucket] += abs(deduction)
                if bucket == "other_deductions":
                    unknown.add(
                        str(
                            row.get("bonusTypeName")
                            or row.get("sellerOperName")
                            or "Неизвестное удержание"
                        )
                    )
            additional = _decimal(row.get("additionalPayment"))
            if additional >= ZERO:
                values["positive_adjustments"] += additional
            else:
                values["corrections"] += abs(additional)
        net_revenue = values["revenue_before_returns"] - values["returns_amount"]
        # Acquiring is disclosed separately but already included in the official commission control total.
        total_expenses = sum(
            (
                values[key]
                for key in (
                    "commission",
                    "logistics",
                    "storage",
                    "acceptance",
                    "marketing",
                    "transit_logistics",
                    "penalties",
                    "subscriptions",
                    "paid_services",
                    "other_deductions",
                    "corrections",
                )
            ),
            ZERO,
        )
        before_cogs = net_revenue - total_expenses + values["positive_adjustments"]
        coverage = self._calculate_cogs(conn, rows, week_start)
        cogs = (
            _decimal(coverage["cogs_rub"]) if coverage["cogs_rub"] is not None else None
        )
        profit = before_cogs - cogs if cogs is not None else None
        metrics: dict[str, Any] = {
            "sales_qty": int(values["sales_qty"]),
            "returns_qty": int(values["returns_qty"]),
            "net_sales_qty": int(values["sales_qty"] - values["returns_qty"]),
            "revenue_before_returns": _money_text(values["revenue_before_returns"]),
            "returns_amount": _money_text(values["returns_amount"]),
            "net_revenue": _money_text(net_revenue),
            "commission": _money_text(values["commission"]),
            "acquiring": _money_text(values["acquiring"]),
            "logistics": _money_text(values["logistics"]),
            "storage": _money_text(values["storage"]),
            "acceptance": _money_text(values["acceptance"]),
            "marketing": _money_text(values["marketing"]),
            "transit_logistics": _money_text(values["transit_logistics"]),
            "penalties": _money_text(values["penalties"]),
            "subscriptions": _money_text(values["subscriptions"]),
            "paid_services": _money_text(values["paid_services"]),
            "other_deductions": _money_text(values["other_deductions"]),
            "positive_adjustments": _money_text(values["positive_adjustments"]),
            "total_wb_expenses": _money_text(total_expenses),
            "wb_expenses_pct": _money_text(_ratio(total_expenses, net_revenue)),
            "to_seller": _money_text(values["to_seller"]),
            "before_cogs_profit": _money_text(before_cogs),
            "before_cogs_margin_pct": _money_text(_ratio(before_cogs, net_revenue)),
            "cogs": _money_text(cogs),
            "profit_after_cogs": _money_text(profit),
            "final_margin_pct": _money_text(_ratio(profit, net_revenue)),
            "acquiring_accounting_note": "included_in_commission_control_total",
        }
        return metrics, coverage, sorted(unknown)

    def _calculate_cogs(
        self, conn: sqlite3.Connection, rows: list[dict[str, Any]], week_start: date
    ) -> dict[str, Any]:
        group_by_nm = {
            str(row[0]): str(row[1])
            for row in conn.execute(
                """SELECT nm_id,group_name FROM registry_upload_config_v2
            WHERE bundle_version=(SELECT bundle_version FROM registry_upload_current_state WHERE slot=1)"""
            )
        }
        nomenclature = conn.execute(
            "SELECT nm_id,vendor_code,barcode,barcodes_json,product_type FROM sheet_vitrina_v1_nomenclature_items WHERE is_active=1"
        ).fetchall()
        alias_to_nm: dict[str, str] = {}
        nomenclature_group_by_nm: dict[str, str] = {}
        product_type_groups = {
            "clear": "Clean",
            "anti_spy": "Anti-Spy",
            "matte": "Matte",
        }
        for item in nomenclature:
            nm = str(item["nm_id"] or "")
            canonical_group = product_type_groups.get(
                str(item["product_type"] or "").casefold()
            )
            if nm and canonical_group:
                nomenclature_group_by_nm[nm] = canonical_group
            for value in (
                nm,
                str(item["vendor_code"] or ""),
                str(item["barcode"] or ""),
            ):
                if value:
                    alias_to_nm[value.casefold()] = nm
            try:
                for value in json.loads(item["barcodes_json"] or "[]"):
                    alias_to_nm[str(value).casefold()] = nm
            except json.JSONDecodeError:
                pass
        cost_rows = conn.execute(
            """SELECT group_name,cost_price_rub,effective_from FROM cost_price_upload_rows
            WHERE dataset_version=(SELECT dataset_version FROM cost_price_current_state WHERE slot=1)
            ORDER BY group_name,effective_from"""
        ).fetchall()
        costs: dict[str, list[tuple[date, Decimal]]] = {}
        for row in cost_rows:
            costs.setdefault(str(row["group_name"]), []).append(
                (
                    date.fromisoformat(row["effective_from"]),
                    _decimal(row["cost_price_rub"]),
                )
            )
        cogs = ZERO
        matched_movements: dict[str, int] = {}
        problems: dict[str, int] = {}
        for row in rows:
            doc = str(row.get("docTypeName") or "").casefold()
            if doc not in {"продажа", "возврат"}:
                continue
            sign = 1 if doc == "продажа" else -1
            qty = int(_decimal(row.get("quantity"))) * sign
            raw_keys = [
                str(row.get("nmId") or ""),
                str(row.get("vendorCode") or ""),
                str(row.get("sku") or ""),
            ]
            internal_nm = next(
                (
                    alias_to_nm[key.casefold()]
                    for key in raw_keys
                    if key and key.casefold() in alias_to_nm
                ),
                raw_keys[0],
            )
            group = group_by_nm.get(internal_nm) or nomenclature_group_by_nm.get(
                internal_nm, ""
            )
            operation_date = week_start
            for field in ("rrDate", "saleDt", "orderDt"):
                raw_date = str(row.get(field) or "")[:10]
                try:
                    if raw_date:
                        operation_date = date.fromisoformat(raw_date)
                        break
                except ValueError:
                    pass
            candidates = [
                cost
                for effective, cost in costs.get(group, [])
                if effective <= operation_date
            ]
            movement_key = (
                internal_nm
                or raw_keys[1]
                or raw_keys[2]
                or str(row.get("srid") or "")
                or str(row.get("orderUid") or "")
                or str(row.get("shkId") or "")
                or "unknown"
            )
            if not candidates:
                problems[movement_key] = problems.get(movement_key, 0) + qty
                continue
            matched_movements[movement_key] = (
                matched_movements.get(movement_key, 0) + qty
            )
            cogs += Decimal(qty) * candidates[-1]
        problems = {key: units for key, units in problems.items() if units != 0}
        matched = sum(abs(units) for units in matched_movements.values())
        unmatched = sum(abs(units) for units in problems.values())
        denominator = matched + unmatched
        coverage_pct = (
            Decimal(matched) / Decimal(denominator) * Decimal("100")
            if denominator
            else None
        )
        return {
            "matched_units": matched,
            "unmatched_units": unmatched,
            "coverage_pct": _money_text(coverage_pct),
            "cogs_rub": _money_text(cogs) if unmatched == 0 else None,
            "partial_cogs_rub": _money_text(cogs),
            "problem_skus": [
                {"sku": key, "net_units": value}
                for key, value in sorted(problems.items())
            ],
        }

    def build_payload(self) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT s.week_start,s.week_end,a.metrics_json,a.report_ids_json,a.report_types_json,
                a.unknown_reasons_json,a.classifier_version,s.status,s.first_loaded_at,s.last_synced_at,
                s.report_count,s.raw_row_count,s.last_error,c.matched_units,c.unmatched_units,c.coverage_pct,
                c.problem_skus_json,r.status reconciliation_status
                FROM wb_finance_weekly_sync s
                LEFT JOIN wb_finance_weekly_aggregates a USING(seller_id,week_start,week_end)
                LEFT JOIN wb_finance_weekly_cost_coverage c USING(seller_id,week_start,week_end)
                LEFT JOIN wb_finance_weekly_reconciliation r USING(seller_id,week_start,week_end)
                WHERE s.seller_id=? ORDER BY s.week_start""",
                (self.seller_id,),
            ).fetchall()
        weeks = []
        for row in rows:
            weeks.append(
                {
                    "week_start": row["week_start"],
                    "week_end": row["week_end"],
                    "status": row["status"],
                    "first_loaded_at": row["first_loaded_at"],
                    "last_synced_at": row["last_synced_at"],
                    "report_count": row["report_count"],
                    "raw_row_count": row["raw_row_count"],
                    "report_ids": json.loads(row["report_ids_json"] or "[]"),
                    "report_types": json.loads(row["report_types_json"] or "[]"),
                    "metrics": json.loads(row["metrics_json"] or "{}"),
                    "classifier_version": row["classifier_version"]
                    or CLASSIFIER_VERSION,
                    "unknown_reasons": json.loads(row["unknown_reasons_json"] or "[]"),
                    "last_error": row["last_error"],
                    "cost_coverage": {
                        "matched_units": row["matched_units"],
                        "unmatched_units": row["unmatched_units"],
                        "coverage_pct": row["coverage_pct"],
                        "problem_skus": json.loads(row["problem_skus_json"] or "[]"),
                    },
                    "reconciliation_status": row["reconciliation_status"] or "pending",
                }
            )
        return {
            "status": "ok",
            "contract_version": "wb_finance_weekly_v1",
            "weeks": weeks,
            "week_count": len(weeks),
            "classifier_version": CLASSIFIER_VERSION,
            "generated_at": self.now_factory()
            .astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }

    def run_backfill(
        self,
        client: WbFinanceApiClient,
        *,
        today: date | None = None,
        continue_on_error: bool = True,
    ) -> dict[str, Any]:
        results = []
        bounds = historical_week_bounds(today)
        for index, (start, end) in enumerate(bounds):
            try:
                item = self.sync_week(start, end, client)
                if index < max(0, len(bounds) - 2):
                    with self._connect() as conn:
                        conn.execute(
                            "UPDATE wb_finance_weekly_sync SET status='completed',unchanged_sync_count=1,next_retry_at=NULL WHERE seller_id=? AND week_start=? AND week_end=? AND status='loaded_preliminary'",
                            (self.seller_id, start.isoformat(), end.isoformat()),
                        )
                        conn.commit()
                    item["status"] = "completed"
                results.append(item)
            except Exception as exc:
                results.append(
                    {
                        "status": "error",
                        "week_start": start.isoformat(),
                        "week_end": end.isoformat(),
                        "error": str(exc),
                    }
                )
                if not continue_on_error:
                    raise
        for start, end in bounds[-2:]:
            try:
                stabilized = self.sync_week(start, end, client)
                results.append({**stabilized, "stabilization_resync": True})
            except Exception as exc:
                results.append(
                    {
                        "status": "error",
                        "week_start": start.isoformat(),
                        "week_end": end.isoformat(),
                        "error": str(exc),
                        "stabilization_resync": True,
                    }
                )
        return {
            "status": "completed_with_errors"
            if any(r["status"] in {"error", "waiting"} for r in results)
            else "completed",
            "weeks": results,
            "week_count": len(bounds),
        }

    def recalculate_all_weeks(self) -> dict[str, Any]:
        """Rebuild every stored week for the configured seller from raw rows."""
        self.ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT DISTINCT week_start,week_end
                FROM wb_finance_weekly_raw_rows
                WHERE seller_id=?
                ORDER BY week_start""",
                (self.seller_id,),
            ).fetchall()
        results = []
        for row in rows:
            start = date.fromisoformat(row["week_start"])
            end = date.fromisoformat(row["week_end"])
            results.append(
                {
                    "week_start": start.isoformat(),
                    "week_end": end.isoformat(),
                    "aggregate": self.recalculate_week(start, end),
                }
            )
        return {"status": "completed", "week_count": len(results), "weeks": results}

    def repair_orphan_derived_rows(self) -> dict[str, Any]:
        """Remove derived rows that have no matching seller/week sync boundary."""
        self.ensure_schema()
        tables = (
            "wb_finance_weekly_aggregates",
            "wb_finance_weekly_cost_coverage",
            "wb_finance_weekly_reconciliation",
        )
        deleted: dict[str, int] = {}
        with self._connect() as conn:
            for table in tables:
                cursor = conn.execute(
                    f"""DELETE FROM {table} AS derived
                    WHERE NOT EXISTS (
                        SELECT 1 FROM wb_finance_weekly_sync AS sync
                        WHERE sync.seller_id=derived.seller_id
                          AND sync.week_start=derived.week_start
                          AND sync.week_end=derived.week_end
                    )"""
                )
                deleted[table] = cursor.rowcount
            conn.commit()
        return {
            "status": "completed",
            "deleted": deleted,
            "deleted_total": sum(deleted.values()),
        }

    def due_tick_week(self, now: datetime | None = None) -> tuple[date, date] | None:
        moment = (now or self.now_factory()).astimezone(MOSCOW)
        closed = historical_week_bounds(moment.date())
        if not closed:
            return None
        latest = closed[-1]
        monday_after = latest[1] + timedelta(days=1)
        if moment.date() == monday_after and moment.hour < 5:
            return None
        self.ensure_schema()
        with self._connect() as conn:
            candidates = conn.execute(
                """SELECT week_start,week_end,status,last_synced_at FROM wb_finance_weekly_sync
                WHERE seller_id=? ORDER BY week_start DESC LIMIT 2""",
                (self.seller_id,),
            ).fetchall()
        by_start = {row["week_start"]: row for row in candidates}
        latest_row = by_start.get(latest[0].isoformat())
        if latest_row is None or latest_row["status"] in {
            "waiting",
            "loading",
            "loaded_preliminary",
            "error_loading",
            "resync_required",
        }:
            return latest
        for bounds in reversed(closed[-2:]):
            row = by_start.get(bounds[0].isoformat())
            last = (
                datetime.fromisoformat(
                    str(row["last_synced_at"]).replace("Z", "+00:00")
                )
                if row and row["last_synced_at"]
                else None
            )
            if last is None or (
                moment.astimezone(timezone.utc) - last.astimezone(timezone.utc)
            ) >= timedelta(hours=24):
                return bounds
        return None

    def _mark_loading(self, start: date, end: date, now: str) -> None:
        self.ensure_schema()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO wb_finance_weekly_sync(seller_id,week_start,week_end,status,attempt_count,last_synced_at)
                VALUES (?,?,?,'loading',1,?) ON CONFLICT(seller_id,week_start,week_end) DO UPDATE SET
                status='loading',attempt_count=attempt_count+1,last_synced_at=excluded.last_synced_at,last_error=NULL""",
                (self.seller_id, start.isoformat(), end.isoformat(), now),
            )
            conn.commit()

    @staticmethod
    def _row_hash(row: Mapping[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(
                dict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=60)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn


def block_from_env(runtime_dir: Path) -> WbFinanceWeeklyBlock:
    seller_id = os.environ.get("SELLER_PORTAL_CANONICAL_SUPPLIER_ID") or "canonical"
    return WbFinanceWeeklyBlock(runtime_dir, seller_id=seller_id)
