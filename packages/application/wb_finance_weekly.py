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

from packages.application.ads_snapshot_payload import resolve_ads_snapshot_payload
from packages.application.canonical_wb_cost_resolver import (
    CANONICAL_COST_FORMULA_VERSION,
    CANONICAL_COST_POLICY_DATE,
    resolve_finance_canonical_cost,
)
from packages.business_time import business_date_from_timestamp


FINANCE_URL = "https://finance-api.wildberries.ru/api/finance/v1/sales-reports/detailed"
CLASSIFIER_VERSION = "wb_finance_weekly_classifier_v2_agent_acquiring_split"
SKU_AGGREGATE_FORMULA_VERSION = "wb_finance_weekly_sku_aggregate_v2"
MOSCOW = ZoneInfo("Europe/Moscow")
ZERO = Decimal("0")
MONEY_QUANT = Decimal("0.0001")
FIRST_INCLUDED_DATE = date(2026, 1, 1)
OUR_WB_COST_OPENING_DATE = CANONICAL_COST_POLICY_DATE.isoformat()
OUR_WB_COST_CUTOVER_DATE = CANONICAL_COST_POLICY_DATE
OUR_WB_COST_CUTOVER_WEEK_START = OUR_WB_COST_CUTOVER_DATE - timedelta(
    days=OUR_WB_COST_CUTOVER_DATE.weekday()
)
RETRO_COST_PERIOD_START = date(2026, 5, 1)
RETRO_COST_PERIOD_END = date(2026, 6, 30)
RETRO_COST_REFERENCE_DATE = date(2026, 7, 1)
RETRO_COST_FIRST_WEEK_START = date(2026, 4, 27)
RETRO_COST_FORMULA_VERSION = CANONICAL_COST_FORMULA_VERSION
PROFIT_METHOD_VERSION = "wb_finance_profit_attributed_capitalization_v2"
COST_METHOD_VERSION = CANONICAL_COST_FORMULA_VERSION


class _StreamingJsonArrayDigest:
    """Digest a deterministic JSON array without retaining every element."""

    def __init__(self) -> None:
        self._hash = hashlib.sha256()
        self._hash.update(b"[")
        self._count = 0
        self._finished = False

    def add(self, value: Any) -> None:
        if self._finished:
            raise RuntimeError("streaming JSON digest is already finalized")
        if self._count:
            self._hash.update(b",")
        self._hash.update(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
        self._count += 1

    @property
    def count(self) -> int:
        return self._count

    def finish(self) -> str:
        if not self._finished:
            self._hash.update(b"]")
            self._finished = True
        return "sha256:" + self._hash.hexdigest()


class _StreamingCostDependencyDigest:
    """Preserve the canonical cost-state JSON hash without a per-row list."""

    def __init__(self) -> None:
        self._hash = hashlib.sha256()
        self._hash.update(b'{"dependencies":[')
        self._count = 0

    def add(self, value: Mapping[str, Any]) -> None:
        if self._count:
            self._hash.update(b",")
        self._hash.update(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        self._count += 1

    def finish(self) -> str:
        self._hash.update(b'],"formula_version":')
        self._hash.update(
            json.dumps(COST_METHOD_VERSION, ensure_ascii=False).encode("utf-8")
        )
        self._hash.update(b"}")
        return self._hash.hexdigest()


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
    if numerator is None or denominator <= ZERO:
        return None
    return numerator / denominator * Decimal("100")


def _operation_date(row: Mapping[str, Any], fallback: date) -> tuple[date, str]:
    for field in ("rrDate", "saleDt", "orderDt"):
        raw_date = str(row.get(field) or "")[:10]
        try:
            if raw_date:
                return date.fromisoformat(raw_date), field
        except ValueError:
            continue
    return fallback, "week_start_fallback"


def _nomenclature_identity_index(
    conn: sqlite3.Connection,
) -> tuple[dict[str, str], set[str], dict[str, str], dict[str, dict[str, Any]]]:
    """Build deterministic active + historical identity without cross-SKU guessing."""

    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sheet_vitrina_v1_nomenclature_items'"
    ).fetchone()
    if table_exists is None:
        return {}, set(), {}, {}
    columns = {
        str(row["name"])
        for row in conn.execute(
            "PRAGMA table_info(sheet_vitrina_v1_nomenclature_items)"
        ).fetchall()
    }
    rows = conn.execute("SELECT * FROM sheet_vitrina_v1_nomenclature_items").fetchall()
    aliases: dict[str, set[str]] = {}
    groups: dict[str, str] = {}
    items: dict[str, dict[str, Any]] = {}
    product_type_groups = {
        "clear": "Clean",
        "anti_spy": "Anti-Spy",
        "matte": "Matte",
    }
    scalar_fields = (
        "nm_id",
        "vendor_code",
        "barcode",
        "our_sku",
        "match_key",
    )
    json_fields = ("barcodes_json", "aliases_json")
    for row in rows:
        nm = str(row["nm_id"] or "") if "nm_id" in columns else ""
        if not nm or nm == "0":
            continue
        item = {column: row[column] for column in columns}
        items.setdefault(nm, item)
        product_type = (
            str(row["product_type"] or "").casefold()
            if "product_type" in columns
            else ""
        )
        canonical_group = product_type_groups.get(product_type)
        if canonical_group:
            groups[nm] = canonical_group
        values: list[str] = []
        for field in scalar_fields:
            if field in columns and row[field] not in (None, ""):
                values.append(str(row[field]))
        for field in json_fields:
            if field not in columns:
                continue
            try:
                parsed = json.loads(row[field] or "[]")
            except (json.JSONDecodeError, TypeError):
                parsed = []
            if isinstance(parsed, list):
                values.extend(str(value) for value in parsed if value not in (None, ""))
        for value in values:
            aliases.setdefault(value.strip().casefold(), set()).add(nm)
    ambiguous = {alias for alias, owners in aliases.items() if len(owners) != 1}
    deterministic = {
        alias: next(iter(owners))
        for alias, owners in aliases.items()
        if len(owners) == 1
    }
    return deterministic, ambiguous, groups, items


def _resolve_finance_nm_id(
    row: Mapping[str, Any],
    *,
    alias_to_nm: Mapping[str, str],
    ambiguous_aliases: set[str],
) -> tuple[str, str, str]:
    raw_nm = str(row.get("nmId") or "").strip()
    if raw_nm and raw_nm != "0":
        return raw_nm, "direct_nm_id", ""
    for field in ("vendorCode", "sku"):
        raw = str(row.get(field) or "").strip()
        if not raw:
            continue
        key = raw.casefold()
        if key in ambiguous_aliases:
            return "", "ambiguous_alias", f"{field}:{raw}"
        resolved = alias_to_nm.get(key, "")
        if resolved:
            return resolved, f"canonical_{field}", ""
    return "", "unresolved", "nmId/vendorCode/sku"


def _functional_wb_cost_state(
    conn: sqlite3.Connection,
    *,
    as_of_date: str,
    nm_id: str,
) -> tuple[dict[str, Any] | None, bool]:
    """Return the active functional cost row and whether legacy fallback is forbidden."""

    required = {
        "sheet_vitrina_v1_warehouse_functional_cutovers",
        "sheet_vitrina_v1_warehouse_wb_daily_cost",
    }
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'sheet_vitrina_v1_warehouse_%'"
        ).fetchall()
    }
    if not required.issubset(tables) or as_of_date < OUR_WB_COST_OPENING_DATE:
        return None, False
    cutover = conn.execute(
        """SELECT cutover_at FROM sheet_vitrina_v1_warehouse_functional_cutovers
           WHERE cutover_id='warehouse_functional_cutover_v1' AND status='posted'"""
    ).fetchone()
    if cutover is None:
        return None, False
    cutover_date = business_date_from_timestamp(str(cutover["cutover_at"]))
    row = conn.execute(
        """SELECT * FROM sheet_vitrina_v1_warehouse_wb_daily_cost
           WHERE cutover_id='warehouse_functional_cutover_v1'
             AND as_of_date=? AND nm_id=?""",
        (as_of_date, nm_id),
    ).fetchone()
    if row is not None:
        quantity = max(_decimal(row["quantity"]), ZERO)
        quality = str(row["quality"] or "historical_provisional")
        fallback = quantity if quality == "fallback_average" else ZERO
        estimated = max(quantity - fallback, ZERO)
        return {
            "our_wb_unit_cost_rub": (
                None
                if quality == "zero_quantity_without_cost_basis"
                else row["wac_rub"]
            ),
            "confirmed_qty": "0",
            "estimated_qty": _money_text(estimated),
            "fallback_qty": _money_text(fallback),
            "confirmed_share_pct": "0",
            "source_status": quality,
            "component_status_json": row["provenance_json"],
            "inputs_hash": row["fingerprint"],
        }, True
    if as_of_date < cutover_date:
        return None, True
    # A current functional balance is not historical evidence.  Once the
    # functional boundary applies, a missing exact-day row must remain unknown
    # instead of inheriting the last published warehouse version.
    return None, True


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
        self._capitalization_cache_key = ""
        self._capitalization_cache: dict[tuple[str, str, str], dict[str, Any]] = {}

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
                CREATE INDEX IF NOT EXISTS wb_finance_raw_by_sku_week
                ON wb_finance_weekly_raw_rows(seller_id,nm_id,week_start,week_end);
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
                    quality_json TEXT NOT NULL DEFAULT '{}',
                    cost_state_hash TEXT NOT NULL DEFAULT '',
                    calculated_at TEXT NOT NULL, PRIMARY KEY (seller_id, week_start, week_end)
                );
                CREATE TABLE IF NOT EXISTS wb_finance_weekly_sku_aggregates (
                    seller_id TEXT NOT NULL,
                    week_start TEXT NOT NULL,
                    week_end TEXT NOT NULL,
                    nm_id TEXT NOT NULL,
                    formula_version TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    coverage_json TEXT NOT NULL,
                    raw_source_digest TEXT NOT NULL,
                    week_content_hash TEXT NOT NULL,
                    cost_state_hash TEXT NOT NULL,
                    raw_row_count INTEGER NOT NULL,
                    calculated_at TEXT NOT NULL,
                    PRIMARY KEY (seller_id,week_start,week_end,nm_id)
                );
                CREATE INDEX IF NOT EXISTS wb_finance_sku_aggregate_lookup
                ON wb_finance_weekly_sku_aggregates(seller_id,nm_id,week_start,week_end);
                CREATE TABLE IF NOT EXISTS wb_finance_projection_audit (
                    audit_id TEXT PRIMARY KEY,
                    seller_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            coverage_columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(wb_finance_weekly_cost_coverage)"
                ).fetchall()
            }
            if "quality_json" not in coverage_columns:
                conn.execute(
                    "ALTER TABLE wb_finance_weekly_cost_coverage ADD COLUMN quality_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "cost_state_hash" not in coverage_columns:
                conn.execute(
                    "ALTER TABLE wb_finance_weekly_cost_coverage ADD COLUMN cost_state_hash TEXT NOT NULL DEFAULT ''"
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
            aggregate = self._recalculate_week_in_connection(conn, week_start, week_end)
            conn.commit()
            return aggregate

    def _recalculate_week_in_connection(
        self,
        conn: sqlite3.Connection,
        week_start: date,
        week_end: date,
    ) -> dict[str, Any]:
        db_rows = conn.execute(
            "SELECT report_id,rrd_id,row_hash,raw_json FROM wb_finance_weekly_raw_rows WHERE seller_id=? AND week_start=? AND week_end=? ORDER BY report_id,rrd_id",
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
        self._rebuild_sku_week_aggregates(
            conn,
            week_start=week_start,
            week_end=week_end,
            raw_rows=db_rows,
            global_metrics=aggregate,
            calculated_at=now,
        )
        conn.execute(
            """INSERT OR REPLACE INTO wb_finance_weekly_cost_coverage
                (seller_id,week_start,week_end,matched_units,unmatched_units,coverage_pct,cogs_rub,
                 problem_skus_json,quality_json,cost_state_hash,calculated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                self.seller_id,
                week_start.isoformat(),
                week_end.isoformat(),
                coverage["matched_units"],
                coverage["unmatched_units"],
                coverage["coverage_pct"],
                coverage["cogs_rub"],
                json.dumps(coverage["problem_skus"], ensure_ascii=False),
                json.dumps(coverage["quality"], ensure_ascii=False),
                coverage["cost_state_hash"],
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
        return aggregate

    def _rebuild_sku_week_aggregates(
        self,
        conn: sqlite3.Connection,
        *,
        week_start: date,
        week_end: date,
        raw_rows: Iterable[sqlite3.Row],
        global_metrics: Mapping[str, Any],
        calculated_at: str,
        persist: bool = True,
    ) -> list[dict[str, Any]]:
        """Materialize an indexed, reproducible per-SKU Finance projection."""

        records = list(raw_rows)
        parsed = [json.loads(row["raw_json"]) for row in records]
        alias_to_nm, ambiguous_aliases, _groups, nomenclature = _nomenclature_identity_index(conn)
        by_nm: dict[str, list[tuple[dict[str, Any], sqlite3.Row]]] = {
            nm_id: [] for nm_id in nomenclature
        }
        account_rows: list[tuple[dict[str, Any], sqlite3.Row]] = []
        identity_blockers: list[dict[str, Any]] = []
        for raw, stored in zip(parsed, records, strict=True):
            nm_id, method, problem = _resolve_finance_nm_id(
                raw,
                alias_to_nm=alias_to_nm,
                ambiguous_aliases=ambiguous_aliases,
            )
            if nm_id:
                by_nm.setdefault(nm_id, []).append((raw, stored))
            elif method == "unresolved" and str(raw.get("nmId") or "").strip() in {"", "0"}:
                account_rows.append((raw, stored))
            else:
                identity_blockers.append(
                    {
                        "report_id": str(raw.get("reportId") or ""),
                        "rrd_id": str(raw.get("rrdId") or ""),
                        "identity_method": method,
                        "reason": problem,
                    }
                )
        week_content_hash = hashlib.sha256(
            "\n".join(sorted(str(row["row_hash"]) for row in records)).encode("utf-8")
        ).hexdigest()
        if persist:
            conn.execute(
                "DELETE FROM wb_finance_weekly_sku_aggregates WHERE seller_id=? AND week_start=? AND week_end=?",
                (self.seller_id, week_start.isoformat(), week_end.isoformat()),
            )
        projections: list[dict[str, Any]] = []

        def store_projection(
            nm_id: str,
            selected: list[tuple[dict[str, Any], sqlite3.Row]],
            *,
            row_kind: str,
        ) -> None:
            source_rows = [item[0] for item in selected]
            metrics, coverage, unknown = self._aggregate_rows(conn, source_rows, week_start)
            coverage = self._calculate_cogs(
                conn,
                source_rows,
                week_start,
                include_details=True,
            )
            unique_cost_dependencies: dict[tuple[str, str], dict[str, Any]] = {}
            for detail in coverage["detail_rows"]:
                key = (str(detail["operation_date"]), str(detail["source_digest"]))
                unique_cost_dependencies.setdefault(
                    key,
                    {
                        field: detail.get(field)
                        for field in (
                            "nm_id",
                            "operation_date",
                            "source_date",
                            "source_identity",
                            "source_digest",
                            "source_quality",
                            "projection_quality",
                            "selection_method",
                            "formula_version",
                        )
                    },
                )
            coverage["detail_rows"] = list(unique_cost_dependencies.values())
            source_manifest = [
                [str(item[1]["report_id"]), str(item[1]["rrd_id"]), str(item[1]["row_hash"])]
                for item in selected
            ]
            raw_source_digest = "sha256:" + hashlib.sha256(
                json.dumps(source_manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            coverage_payload = {
                **coverage,
                "row_kind": row_kind,
                "unknown_reasons": unknown,
                "identity_blockers": identity_blockers if row_kind == "account" else [],
                "global_net_revenue": global_metrics.get("net_revenue"),
                "global_week_content_hash": week_content_hash,
            }
            projection = {
                "seller_id": self.seller_id,
                "week_start": week_start.isoformat(),
                "week_end": week_end.isoformat(),
                "nm_id": nm_id,
                "formula_version": SKU_AGGREGATE_FORMULA_VERSION,
                "metrics_json": json.dumps(
                    metrics, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
                "coverage_json": json.dumps(
                    coverage_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "raw_source_digest": raw_source_digest,
                "week_content_hash": week_content_hash,
                "cost_state_hash": str(coverage["cost_state_hash"]),
                "raw_row_count": len(selected),
            }
            projections.append(projection)
            if persist:
                conn.execute(
                    """INSERT INTO wb_finance_weekly_sku_aggregates(
                       seller_id,week_start,week_end,nm_id,formula_version,metrics_json,
                       coverage_json,raw_source_digest,week_content_hash,cost_state_hash,
                       raw_row_count,calculated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        projection["seller_id"],
                        projection["week_start"],
                        projection["week_end"],
                        projection["nm_id"],
                        projection["formula_version"],
                        projection["metrics_json"],
                        projection["coverage_json"],
                        projection["raw_source_digest"],
                        projection["week_content_hash"],
                        projection["cost_state_hash"],
                        projection["raw_row_count"],
                        calculated_at,
                    ),
                )

        for nm_id in sorted(by_nm, key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value)):
            store_projection(nm_id, by_nm[nm_id], row_kind="sku")
        store_projection("__account__", account_rows, row_kind="account")
        return projections

    def _aggregate_rows(
        self,
        conn: sqlite3.Connection,
        rows: list[dict[str, Any]],
        week_start: date,
    ) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
        values: dict[str, Decimal] = {
            key: ZERO
            for key in (
                "sales_qty",
                "returns_qty",
                "revenue_before_returns",
                "returns_amount",
                "combined_commission_control",
                "agent_remuneration",
                "acquiring",
                "wb_remuneration_adjustment",
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
            combined = revenue - _decimal(row.get("forPay"))
            acquiring = _decimal(row.get("acquiringFee"))
            adjustment = _decimal(row.get("additionalPayment"))
            if doc == "продажа":
                values["sales_qty"] += quantity
                values["revenue_before_returns"] += revenue
                values["combined_commission_control"] += combined
                values["acquiring"] += acquiring
                values["agent_remuneration"] += combined - acquiring
                values["wb_remuneration_adjustment"] += adjustment
                values["to_seller"] += _decimal(row.get("forPay"))
            elif doc == "возврат":
                values["returns_qty"] += quantity
                values["returns_amount"] += revenue
                values["combined_commission_control"] -= combined
                values["acquiring"] -= acquiring
                values["agent_remuneration"] -= combined - acquiring
                values["wb_remuneration_adjustment"] -= adjustment
                values["to_seller"] -= _decimal(row.get("forPay"))
            values["logistics"] += _decimal(row.get("deliveryService"))
            values["storage"] += _decimal(row.get("paidStorage"))
            acceptance = _decimal(row.get("paidAcceptance"))
            values["acceptance"] += acceptance
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
            # ``additionalPayment`` is the official XLSX column
            # "Корректировка Вознаграждения Вайлдберриз (ВВ)".  Sale/return
            # values are already reflected in ``forPay`` and therefore in the
            # combined commission control.  Only a standalone adjustment row
            # (without sale/return sign) is applied separately.
            if doc not in {"продажа", "возврат"} and adjustment:
                values["wb_remuneration_adjustment"] += adjustment
                if adjustment >= ZERO:
                    values["positive_adjustments"] += adjustment
                else:
                    values["corrections"] += abs(adjustment)
        net_revenue = values["revenue_before_returns"] - values["returns_amount"]
        capitalization = self._capitalization_reconciliation(conn, rows)
        capitalized_acceptance = _decimal(capitalization["matched_acceptance_rub"])
        capitalized_transit = _decimal(capitalization["matched_transit_rub"])
        if capitalization["unmatched_expense_count"]:
            unknown.add("Неподтверждённая капитализация приёмки/транзита оставлена в расходах периода")
        total_expenses = sum(
            (
                values[key]
                for key in (
                    "agent_remuneration",
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
                    "corrections",
                )
            ),
            ZERO,
        )
        profit_period_expenses = (
            total_expenses - capitalized_acceptance - capitalized_transit
        )
        before_cogs = net_revenue - profit_period_expenses + values["positive_adjustments"]
        coverage = self._calculate_cogs(
            conn,
            rows,
            week_start,
        )
        cogs = (
            _decimal(coverage["cogs_rub"]) if coverage["cogs_rub"] is not None else None
        )
        profit = (
            before_cogs - cogs
            if before_cogs is not None and cogs is not None
            else None
        )
        metrics: dict[str, Any] = {
            "sales_qty": int(values["sales_qty"]),
            "returns_qty": int(values["returns_qty"]),
            "net_sales_qty": int(values["sales_qty"] - values["returns_qty"]),
            "revenue_before_returns": _money_text(values["revenue_before_returns"]),
            "returns_amount": _money_text(values["returns_amount"]),
            "net_revenue": _money_text(net_revenue),
            "agent_remuneration": _money_text(values["agent_remuneration"]),
            "commission": _money_text(values["agent_remuneration"]),
            "combined_commission_control": _money_text(values["combined_commission_control"]),
            "acquiring": _money_text(values["acquiring"]),
            "wb_remuneration_adjustment": _money_text(values["wb_remuneration_adjustment"]),
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
            "corrections": _money_text(values["corrections"]),
            "total_wb_expenses": _money_text(total_expenses),
            "wb_expenses_without_marketing": _money_text(
                total_expenses - values["marketing"]
            ),
            "wb_expenses_without_marketing_pct": _money_text(
                _ratio(total_expenses - values["marketing"], net_revenue)
            ),
            "profit_period_expenses": _money_text(profit_period_expenses),
            "capitalized_acceptance": _money_text(capitalized_acceptance),
            "capitalized_transit_logistics": _money_text(capitalized_transit),
            "to_seller": _money_text(values["to_seller"]),
            "before_cogs_profit": _money_text(before_cogs),
            "before_cogs_margin_pct": _money_text(_ratio(before_cogs, net_revenue)),
            "cogs": _money_text(cogs),
            "profit_after_cogs": _money_text(profit),
            "final_margin_pct": _money_text(_ratio(profit, net_revenue)),
            "commission_control_reconciliation_rub": _money_text(
                values["combined_commission_control"]
                - values["agent_remuneration"]
                - values["acquiring"]
            ),
            "acquiring_accounting_note": "separate_from_agent; agent_plus_acquiring_equals_combined_control",
            "acceptance_accounting_note": "addback_only_when_supply_cost_layer_lineage_is_matched_and_capped",
            "transit_accounting_note": "addback_only_when_supply_cost_layer_lineage_is_matched_and_capped",
            "capitalization_reconciliation": capitalization,
            "profit_method_version": PROFIT_METHOD_VERSION,
            "profit_semantics_complete": True,
        }
        return metrics, coverage, sorted(unknown)

    def _capitalization_reconciliation_legacy_deprecated(
        self,
        conn: sqlite3.Connection,
        rows: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Match Finance acceptance/transit only to proven canonical cost layers."""

        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sheet_vitrina_v1_wb_supply_cost_layers'"
        ).fetchone()
        alias_to_nm, ambiguous_aliases, _groups, _items = _nomenclature_identity_index(conn)
        candidates: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            nm_id, identity_method, identity_problem = _resolve_finance_nm_id(
                row,
                alias_to_nm=alias_to_nm,
                ambiguous_aliases=ambiguous_aliases,
            )
            supply_id = str(
                row.get("giId")
                or row.get("supplyId")
                or row.get("supplyID")
                or ""
            ).strip()
            amounts = (
                ("acceptance", abs(_decimal(row.get("paidAcceptance")))),
                (
                    "transit",
                    abs(_decimal(row.get("deduction")))
                    if classify_deduction(row) == "transit_logistics"
                    else ZERO,
                ),
            )
            for component, amount in amounts:
                if amount <= ZERO:
                    continue
                key = (component, supply_id, nm_id)
                item = candidates.setdefault(
                    key,
                    {
                        "component": component,
                        "wb_supply_id": supply_id,
                        "nm_id": nm_id,
                        "finance_amount_rub": ZERO,
                        "finance_row_count": 0,
                        "identity_method": identity_method,
                        "identity_problem": identity_problem,
                    },
                )
                item["finance_amount_rub"] += amount
                item["finance_row_count"] += 1
        lineage: list[dict[str, Any]] = []
        matched = {"acceptance": ZERO, "transit": ZERO}
        unmatched_count = 0
        for key in sorted(candidates):
            item = candidates[key]
            component, supply_id, nm_id = key
            layer = None
            if table is not None and supply_id and nm_id:
                layer = conn.execute(
                    """SELECT wb_supply_cost_layer_id,wb_supply_id,nm_id,accepted_qty,
                              transit_cost_status,transit_amount_total,
                              wb_acceptance_amount_total,source_status,component_status_json,
                              inputs_hash,version
                       FROM sheet_vitrina_v1_wb_supply_cost_layers
                       WHERE is_current=1 AND wb_supply_id=? AND nm_id=?""",
                    (supply_id, nm_id),
                ).fetchone()
            canonical_amount = ZERO
            reason = ""
            if layer is None:
                reason = (
                    "finance_supply_id_missing"
                    if not supply_id
                    else "finance_nm_id_unresolved"
                    if not nm_id
                    else "canonical_supply_cost_layer_missing"
                )
            else:
                raw_capitalized = (
                    layer["wb_acceptance_amount_total"]
                    if component == "acceptance"
                    else layer["transit_amount_total"]
                )
                canonical_amount = max(_decimal(raw_capitalized), ZERO)
                if not str(layer["inputs_hash"] or ""):
                    reason = "canonical_supply_cost_layer_missing_fingerprint"
                elif component == "transit" and str(layer["transit_cost_status"] or "") not in {
                    "confirmed",
                    "seller_portal_confirmed",
                    "official_confirmed",
                }:
                    reason = "canonical_transit_not_confirmed"
                elif canonical_amount <= ZERO:
                    reason = "canonical_capitalized_amount_not_positive"
            finance_amount = item["finance_amount_rub"]
            addback = min(finance_amount, canonical_amount) if not reason else ZERO
            matched[component] += addback
            residual = finance_amount - addback
            if residual > ZERO:
                unmatched_count += int(item["finance_row_count"])
            lineage.append(
                {
                    "component": component,
                    "wb_supply_id": supply_id,
                    "nm_id": nm_id,
                    "finance_amount_rub": _money_text(finance_amount),
                    "canonical_capitalized_amount_rub": _money_text(canonical_amount),
                    "addback_rub": _money_text(addback),
                    "unmatched_period_expense_rub": _money_text(residual),
                    "reason": reason or ("matched_and_capped" if residual == ZERO else "matched_but_capped"),
                    "canonical_layer_id": str(layer["wb_supply_cost_layer_id"] or "") if layer else "",
                    "canonical_layer_version": int(layer["version"] or 0) if layer else 0,
                    "canonical_inputs_hash": str(layer["inputs_hash"] or "") if layer else "",
                    "identity_method": item["identity_method"],
                    "identity_problem": item["identity_problem"],
                }
            )
        return {
            "status": "warning" if unmatched_count else "ok",
            "matched_acceptance_rub": _money_text(matched["acceptance"]),
            "matched_transit_rub": _money_text(matched["transit"]),
            "unmatched_expense_count": unmatched_count,
            "lineage": lineage,
            "policy": "exact Finance giId + canonical nmId; addback capped by current canonical cost layer",
        }

    def _capitalization_reconciliation(
        self,
        conn: sqlite3.Connection,
        rows: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Allocate each canonical supply-layer amount at most once globally."""

        allocations = self._global_capitalization_allocations(conn)
        selected: list[dict[str, Any]] = []
        for row in rows:
            report_id = str(row.get("reportId") or "")
            rrd_id = str(row.get("rrdId") or "")
            for component in ("acceptance", "transit"):
                item = allocations.get((report_id, rrd_id, component))
                if item is not None:
                    selected.append(item)
        grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        matched = {"acceptance": ZERO, "transit": ZERO}
        unmatched_count = 0
        for item in selected:
            component = str(item["component"])
            finance_amount = _decimal(item["finance_amount_rub"])
            addback = _decimal(item["addback_rub"])
            residual = finance_amount - addback
            matched[component] += addback
            if residual > ZERO:
                unmatched_count += 1
            key = (
                component,
                str(item["wb_supply_id"]),
                str(item["nm_id"]),
                str(item["canonical_layer_id"]),
            )
            group = grouped.setdefault(
                key,
                {
                    "component": component,
                    "wb_supply_id": str(item["wb_supply_id"]),
                    "nm_id": str(item["nm_id"]),
                    "finance_amount_rub": ZERO,
                    "canonical_capitalized_amount_rub": str(
                        item["canonical_capitalized_amount_rub"]
                    ),
                    "global_finance_amount_rub": str(item["global_finance_amount_rub"]),
                    "addback_rub": ZERO,
                    "unmatched_period_expense_rub": ZERO,
                    "reasons": set(),
                    "canonical_layer_id": str(item["canonical_layer_id"]),
                    "canonical_layer_version": int(item["canonical_layer_version"]),
                    "canonical_inputs_hash": str(item["canonical_inputs_hash"]),
                    "finance_row_count": 0,
                },
            )
            group["finance_amount_rub"] += finance_amount
            group["addback_rub"] += addback
            group["unmatched_period_expense_rub"] += residual
            group["reasons"].add(str(item["reason"]))
            group["finance_row_count"] += 1
        lineage = []
        for key in sorted(grouped):
            item = grouped[key]
            lineage.append(
                {
                    **{
                        field: item[field]
                        for field in (
                            "component",
                            "wb_supply_id",
                            "nm_id",
                            "canonical_capitalized_amount_rub",
                            "global_finance_amount_rub",
                            "canonical_layer_id",
                            "canonical_layer_version",
                            "canonical_inputs_hash",
                            "finance_row_count",
                        )
                    },
                    "finance_amount_rub": _money_text(item["finance_amount_rub"]),
                    "addback_rub": _money_text(item["addback_rub"]),
                    "unmatched_period_expense_rub": _money_text(
                        item["unmatched_period_expense_rub"]
                    ),
                    "reason": ",".join(sorted(item["reasons"])),
                }
            )
        return {
            "status": "warning" if unmatched_count else "ok",
            "matched_acceptance_rub": _money_text(matched["acceptance"]),
            "matched_transit_rub": _money_text(matched["transit"]),
            "unmatched_expense_count": unmatched_count,
            "lineage": lineage,
            "policy": (
                "exact Finance giId + canonical nmId; each current cost-layer amount "
                "is allocated chronologically across all Finance rows and capped globally"
            ),
        }

    def _global_capitalization_allocations(
        self,
        conn: sqlite3.Connection,
    ) -> dict[tuple[str, str, str], dict[str, Any]]:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        sync_manifest = (
            [
                list(row)
                for row in conn.execute(
                    """SELECT week_start,week_end,content_hash FROM wb_finance_weekly_sync
                       WHERE seller_id=? ORDER BY week_start,week_end""",
                    (self.seller_id,),
                ).fetchall()
            ]
            if "wb_finance_weekly_sync" in tables
            else []
        )
        layer_manifest: list[list[Any]] = []
        if "sheet_vitrina_v1_wb_supply_cost_layers" in tables:
            layer_manifest = [
                list(row)
                for row in conn.execute(
                    """SELECT wb_supply_cost_layer_id,wb_supply_id,nm_id,
                              transit_cost_status,transit_amount_total,
                              wb_acceptance_amount_total,inputs_hash,version,is_current
                       FROM sheet_vitrina_v1_wb_supply_cost_layers
                       ORDER BY wb_supply_cost_layer_id,version"""
                ).fetchall()
            ]
        cache_key = self._json_digest(
            {"sync": sync_manifest, "cost_layers": layer_manifest}
        )
        if cache_key == self._capitalization_cache_key:
            return self._capitalization_cache

        alias_to_nm, ambiguous_aliases, _groups, _items = _nomenclature_identity_index(conn)
        candidates: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        raw_rows = conn.execute(
            """SELECT report_id,rrd_id,week_start,raw_json
               FROM wb_finance_weekly_raw_rows WHERE seller_id=?
               ORDER BY week_start,report_id,rrd_id""",
            (self.seller_id,),
        )
        for stored in raw_rows:
            raw = json.loads(str(stored["raw_json"] or "{}"))
            nm_id, identity_method, identity_problem = _resolve_finance_nm_id(
                raw,
                alias_to_nm=alias_to_nm,
                ambiguous_aliases=ambiguous_aliases,
            )
            supply_id = str(
                raw.get("giId") or raw.get("supplyId") or raw.get("supplyID") or ""
            ).strip()
            operation_day, _operation_source = _operation_date(
                raw, date.fromisoformat(str(stored["week_start"]))
            )
            amounts = (
                ("acceptance", abs(_decimal(raw.get("paidAcceptance")))),
                (
                    "transit",
                    abs(_decimal(raw.get("deduction")))
                    if classify_deduction(raw) == "transit_logistics"
                    else ZERO,
                ),
            )
            for component, amount in amounts:
                if amount <= ZERO:
                    continue
                candidates.setdefault((component, supply_id, nm_id), []).append(
                    {
                        "component": component,
                        "report_id": str(stored["report_id"]),
                        "rrd_id": str(stored["rrd_id"]),
                        "week_start": str(stored["week_start"]),
                        "operation_date": operation_day.isoformat(),
                        "wb_supply_id": supply_id,
                        "nm_id": nm_id,
                        "finance_amount_rub": amount,
                        "identity_method": identity_method,
                        "identity_problem": identity_problem,
                    }
                )

        allocations: dict[tuple[str, str, str], dict[str, Any]] = {}
        for key in sorted(candidates):
            component, supply_id, nm_id = key
            items = sorted(
                candidates[key],
                key=lambda item: (
                    item["operation_date"],
                    item["week_start"],
                    item["report_id"],
                    item["rrd_id"],
                ),
            )
            layer = None
            if "sheet_vitrina_v1_wb_supply_cost_layers" in tables and supply_id and nm_id:
                layer = conn.execute(
                    """SELECT wb_supply_cost_layer_id,transit_cost_status,
                              transit_amount_total,wb_acceptance_amount_total,
                              inputs_hash,version
                       FROM sheet_vitrina_v1_wb_supply_cost_layers
                       WHERE is_current=1 AND wb_supply_id=? AND nm_id=?""",
                    (supply_id, nm_id),
                ).fetchone()
            reason = ""
            canonical_amount = ZERO
            if layer is None:
                reason = (
                    "finance_supply_id_missing"
                    if not supply_id
                    else "finance_nm_id_unresolved"
                    if not nm_id
                    else "canonical_supply_cost_layer_missing"
                )
            else:
                canonical_amount = max(
                    _decimal(
                        layer["wb_acceptance_amount_total"]
                        if component == "acceptance"
                        else layer["transit_amount_total"]
                    ),
                    ZERO,
                )
                if not str(layer["inputs_hash"] or ""):
                    reason = "canonical_supply_cost_layer_missing_fingerprint"
                elif component == "transit" and str(layer["transit_cost_status"] or "") not in {
                    "confirmed",
                    "seller_portal_confirmed",
                    "official_confirmed",
                }:
                    reason = "canonical_transit_not_confirmed"
                elif canonical_amount <= ZERO:
                    reason = "canonical_capitalized_amount_not_positive"
            remaining = canonical_amount if not reason else ZERO
            global_finance_amount = sum(
                (item["finance_amount_rub"] for item in items), ZERO
            )
            for item in items:
                amount = item["finance_amount_rub"]
                addback = min(amount, remaining)
                remaining -= addback
                residual = amount - addback
                allocations[(item["report_id"], item["rrd_id"], component)] = {
                    **item,
                    "finance_amount_rub": _money_text(amount),
                    "global_finance_amount_rub": _money_text(global_finance_amount),
                    "canonical_capitalized_amount_rub": _money_text(canonical_amount),
                    "addback_rub": _money_text(addback),
                    "reason": reason
                    or (
                        "matched_with_global_cap"
                        if residual == ZERO
                        else "matched_but_global_cap_exhausted_or_capped"
                    ),
                    "canonical_layer_id": str(layer["wb_supply_cost_layer_id"] or "") if layer else "",
                    "canonical_layer_version": int(layer["version"] or 0) if layer else 0,
                    "canonical_inputs_hash": str(layer["inputs_hash"] or "") if layer else "",
                }
        self._capitalization_cache_key = cache_key
        self._capitalization_cache = allocations
        return allocations

    def _load_retro_cost_map(
        self, conn: sqlite3.Connection
    ) -> dict[str, dict[str, Any]]:
        rows = conn.execute(
            """SELECT * FROM wb_finance_retro_cost_map
               WHERE seller_id=? AND formula_version=? AND status='business_approved_retro'
               ORDER BY nm_id""",
            (self.seller_id, RETRO_COST_FORMULA_VERSION),
        ).fetchall()
        return {str(row["nm_id"]): dict(row) for row in rows}

    def _calculate_cogs_legacy_deprecated(
        self,
        conn: sqlite3.Connection,
        rows: list[dict[str, Any]],
        week_start: date,
        *,
        retro_cost_map: Mapping[str, Mapping[str, Any]] | None = None,
        include_details: bool = False,
    ) -> dict[str, Any]:
        group_by_nm = {
            str(row[0]): str(row[1])
            for row in conn.execute(
                """SELECT nm_id,group_name FROM registry_upload_config_v2
            WHERE bundle_version=(SELECT bundle_version FROM registry_upload_current_state WHERE slot=1)"""
            )
        }
        (
            alias_to_nm,
            ambiguous_aliases,
            nomenclature_group_by_nm,
            _nomenclature_items,
        ) = _nomenclature_identity_index(conn)
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
        daily_state_available = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sheet_vitrina_v1_wb_cost_daily_state'"
            ).fetchone()
            is not None
        )
        daily_state_cache: dict[tuple[str, str], Mapping[str, Any] | None] = {}
        if retro_cost_map is None:
            retro_cost_map = self._load_retro_cost_map(conn)
        cogs = ZERO
        matched_movements: dict[str, dict[str, Any]] = {}
        problems: dict[str, int] = {}
        problem_gross_units: dict[str, int] = {}
        problem_meta: dict[str, dict[str, Any]] = {}
        dependency_evidence: set[str] = set()
        detail_rows: list[dict[str, Any]] = []
        operation_date_fallback_rows = 0
        operation_date_fallback_units = 0
        for row in rows:
            doc = str(row.get("docTypeName") or "").casefold()
            if doc not in {"продажа", "возврат"}:
                continue
            sign = 1 if doc == "продажа" else -1
            raw_qty = int(_decimal(row.get("quantity")))
            if raw_qty == 0:
                continue
            qty = raw_qty * sign
            raw_keys = [
                str(row.get("nmId") or ""),
                str(row.get("vendorCode") or ""),
                str(row.get("sku") or ""),
            ]
            internal_nm, identity_method, identity_problem = _resolve_finance_nm_id(
                row,
                alias_to_nm=alias_to_nm,
                ambiguous_aliases=ambiguous_aliases,
            )
            group = group_by_nm.get(internal_nm) or nomenclature_group_by_nm.get(
                internal_nm, ""
            )
            operation_date, operation_date_source = _operation_date(row, week_start)
            if operation_date_source == "week_start_fallback":
                operation_date_fallback_rows += 1
                operation_date_fallback_units += abs(qty)
            identity_key = (
                internal_nm
                or raw_keys[1]
                or raw_keys[2]
                or str(row.get("srid") or "")
                or str(row.get("orderUid") or "")
                or str(row.get("shkId") or "")
                or "unknown"
            )
            if (
                operation_date_source == "week_start_fallback"
                and week_start + timedelta(days=6) >= RETRO_COST_PERIOD_START
            ):
                source = "operation_date_missing"
                movement_key = f"{source}|{identity_key}"
                missing_reason = "operation_date_missing"
                dependency = {
                    "source": source,
                    "operation_date": "",
                    "operation_date_source": operation_date_source,
                    "raw_keys": raw_keys,
                    "internal_nm": internal_nm,
                    "identity_method": identity_method,
                    "identity_problem": identity_problem,
                    "group": group,
                    "missing": missing_reason,
                }
                dependency_evidence.add(
                    json.dumps(
                        dependency,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                problems[movement_key] = problems.get(movement_key, 0) + qty
                problem_gross_units[movement_key] = (
                    problem_gross_units.get(movement_key, 0) + abs(qty)
                )
                problem_meta[movement_key] = {
                    "sku": identity_key,
                    "operation_date": "",
                    "source": source,
                    "reason": missing_reason,
                    "operation_date_source": operation_date_source,
                }
                continue
            source = (
                "cost_price"
                if operation_date < RETRO_COST_PERIOD_START
                else "business_approved_retro"
                if operation_date <= RETRO_COST_PERIOD_END
                else "our_wb_cost_daily_state"
            )
            movement_key = (
                f"{source}|{identity_key}"
                if source == "cost_price"
                else f"{source}|{identity_key}|{operation_date.isoformat()}"
            )
            selected_cost: Decimal | None = None
            selected_state: Mapping[str, Any] | None = None
            quality_shares = (ZERO, ZERO, ZERO)
            missing_reason = ""
            dependency: dict[str, Any] = {
                "source": source,
                "operation_date": operation_date.isoformat(),
                "operation_date_source": operation_date_source,
                "raw_keys": raw_keys,
                "internal_nm": internal_nm,
                "identity_method": identity_method,
                "identity_problem": identity_problem,
                "group": group,
            }
            if source == "cost_price":
                candidates = [
                    (effective, cost)
                    for effective, cost in costs.get(group, [])
                    if effective <= operation_date
                ]
                if candidates:
                    effective, selected_cost = candidates[-1]
                    dependency.update(
                        {
                            "effective_from": effective.isoformat(),
                            "unit_cost_rub": _money_text(selected_cost),
                        }
                    )
                else:
                    missing_reason = "cost_price_missing"
                    dependency["missing"] = missing_reason
            elif source == "business_approved_retro":
                selected_retro = retro_cost_map.get(internal_nm) if internal_nm else None
                if selected_retro is not None:
                    selected_cost = _decimal(selected_retro.get("unit_cost_rub"))
                    dependency.update(
                        {
                            "unit_cost_rub": _money_text(selected_cost),
                            "source_date": str(selected_retro.get("source_date") or ""),
                            "source_table": str(selected_retro.get("source_table") or ""),
                            "source_row_sha256": str(selected_retro.get("source_row_sha256") or ""),
                            "source_calculation_fingerprint": str(
                                selected_retro.get("source_calculation_fingerprint") or ""
                            ),
                            "selection_method": str(selected_retro.get("selection_method") or ""),
                            "formula_version": str(selected_retro.get("formula_version") or ""),
                            "status": str(selected_retro.get("status") or ""),
                        }
                    )
                    if selected_cost <= ZERO:
                        selected_cost = None
                        missing_reason = "retro_cost_non_positive"
                        dependency["missing"] = missing_reason
                else:
                    missing_reason = (
                        "finance_identity_ambiguous"
                        if identity_method == "ambiguous_alias"
                        else "finance_identity_unresolved"
                        if not internal_nm
                        else "business_approved_retro_cost_missing"
                    )
                    dependency["missing"] = missing_reason
            else:
                cache_key = (operation_date.isoformat(), internal_nm)
                if cache_key not in daily_state_cache:
                    functional_state, functional_applies = _functional_wb_cost_state(
                        conn,
                        as_of_date=cache_key[0],
                        nm_id=cache_key[1],
                    )
                    if functional_applies:
                        daily_state_cache[cache_key] = functional_state
                    else:
                        daily_state_cache[cache_key] = (
                            conn.execute(
                                """SELECT * FROM sheet_vitrina_v1_wb_cost_daily_state
                                WHERE as_of_date=? AND nm_id=?""",
                                cache_key,
                            ).fetchone()
                            if daily_state_available and internal_nm
                            else None
                        )
                selected_state = daily_state_cache[cache_key]
                if selected_state is not None:
                    raw_unit_cost = selected_state["our_wb_unit_cost_rub"]
                    selected_cost = (
                        _decimal(raw_unit_cost) if raw_unit_cost is not None else None
                    )
                    confirmed_qty = max(_decimal(selected_state["confirmed_qty"]), ZERO)
                    estimated_qty = max(_decimal(selected_state["estimated_qty"]), ZERO)
                    fallback_qty = max(_decimal(selected_state["fallback_qty"]), ZERO)
                    bucket_total = confirmed_qty + estimated_qty + fallback_qty
                    if bucket_total > ZERO:
                        quality_shares = (
                            confirmed_qty / bucket_total,
                            estimated_qty / bucket_total,
                            fallback_qty / bucket_total,
                        )
                    else:
                        state_status = str(selected_state["source_status"] or "")
                        quality_shares = (
                            (Decimal("1"), ZERO, ZERO)
                            if state_status == "confirmed"
                            else (ZERO, ZERO, Decimal("1"))
                            if state_status == "fallback"
                            else (ZERO, Decimal("1"), ZERO)
                        )
                    dependency.update(
                        {
                            "unit_cost_rub": _money_text(selected_cost),
                            "confirmed_qty": _money_text(confirmed_qty),
                            "estimated_qty": _money_text(estimated_qty),
                            "fallback_qty": _money_text(fallback_qty),
                            "confirmed_share_pct": str(
                                selected_state["confirmed_share_pct"]
                            ),
                            "source_status": str(selected_state["source_status"] or ""),
                            "component_status_json": str(
                                selected_state["component_status_json"] or "{}"
                            ),
                            "inputs_hash": str(selected_state["inputs_hash"] or ""),
                        }
                    )
                    if selected_cost is None:
                        missing_reason = "our_wb_unit_cost_missing"
                        dependency["missing"] = missing_reason
                else:
                    missing_reason = "our_wb_daily_state_missing"
                    dependency["missing"] = missing_reason
            dependency_evidence.add(
                json.dumps(
                    dependency,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            if selected_cost is None:
                problems[movement_key] = problems.get(movement_key, 0) + qty
                problem_gross_units[movement_key] = (
                    problem_gross_units.get(movement_key, 0) + abs(qty)
                )
                problem_meta[movement_key] = {
                    "sku": identity_key,
                    "operation_date": operation_date.isoformat(),
                    "source": source,
                    "reason": missing_reason,
                    "operation_date_source": operation_date_source,
                }
                continue
            movement = matched_movements.setdefault(
                movement_key,
                {
                    "net_units": 0,
                    "gross_units": 0,
                    "source": source,
                    "quality_shares": quality_shares,
                },
            )
            movement["net_units"] = int(movement["net_units"]) + qty
            movement["gross_units"] = int(movement["gross_units"]) + abs(qty)
            cogs += Decimal(qty) * selected_cost
            if include_details:
                detail_rows.append(
                    {
                        "report_id": str(row.get("reportId") or ""),
                        "rrd_id": str(row.get("rrdId") or ""),
                        "nm_id": internal_nm,
                        "operation_date": operation_date.isoformat(),
                        "operation_date_source": operation_date_source,
                        "movement": "sale" if sign > 0 else "return",
                        "quantity": abs(qty),
                        "signed_quantity": qty,
                        "unit_cost_rub": _money_text(selected_cost),
                        "cost_source": source,
                        "source_date": str(dependency.get("source_date") or dependency.get("effective_from") or operation_date.isoformat()),
                        "formula_version": COST_METHOD_VERSION,
                        "signed_cogs_rub": _money_text(Decimal(qty) * selected_cost),
                    }
                )
        matched = sum(
            int(movement["gross_units"]) for movement in matched_movements.values()
        )
        unmatched = sum(problem_gross_units.values())
        denominator = matched + unmatched
        coverage_pct = (
            Decimal(matched) / Decimal(denominator) * Decimal("100")
            if denominator
            else None
        )
        source_units = {
            "cost_price": 0,
            "business_approved_retro": 0,
            "our_wb_cost_daily_state": 0,
        }
        confirmed_units = ZERO
        estimated_units = ZERO
        fallback_units = ZERO
        for movement in matched_movements.values():
            units = int(movement["gross_units"])
            source_name = str(movement["source"])
            source_units[source_name] += units
            if source_name != "our_wb_cost_daily_state":
                continue
            confirmed_share, estimated_share, fallback_share = movement[
                "quality_shares"
            ]
            decimal_units = Decimal(units)
            confirmed_units += decimal_units * confirmed_share
            estimated_units += decimal_units * estimated_share
            fallback_units += decimal_units * fallback_share
        our_wb_units = source_units["our_wb_cost_daily_state"]
        confirmed_share_pct = (
            confirmed_units / Decimal(our_wb_units) * Decimal("100")
            if our_wb_units
            else None
        )
        quality = {
            "cost_method_version": COST_METHOD_VERSION,
            "cutover_date": OUR_WB_COST_OPENING_DATE,
            "source_units": source_units,
            "confirmed_units": _money_text(confirmed_units),
            "estimated_units": _money_text(estimated_units),
            "fallback_units": _money_text(fallback_units),
            "estimated_fallback_units": _money_text(estimated_units + fallback_units),
            "business_approved_retro_units": source_units["business_approved_retro"],
            "confirmed_share_pct": _money_text(confirmed_share_pct),
            "operation_date_fallback_rows": operation_date_fallback_rows,
            "operation_date_fallback_units": operation_date_fallback_units,
        }
        cost_state_hash = hashlib.sha256(
            json.dumps(
                {
                    "cost_method_version": COST_METHOD_VERSION,
                    "dependencies": sorted(dependency_evidence),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "matched_units": matched,
            "unmatched_units": unmatched,
            "coverage_pct": _money_text(coverage_pct),
            "cogs_rub": _money_text(cogs) if unmatched == 0 else None,
            "partial_cogs_rub": _money_text(cogs),
            "problem_skus": [
                {
                    **problem_meta[key],
                    "net_units": problems[key],
                    "unmatched_units": gross_units,
                }
                for key, gross_units in sorted(problem_gross_units.items())
            ],
            "quality": quality,
            "cost_state_hash": cost_state_hash,
            "detail_rows": detail_rows if include_details else [],
        }

    def _calculate_cogs(
        self,
        conn: sqlite3.Connection,
        rows: list[dict[str, Any]],
        week_start: date,
        *,
        include_details: bool = False,
    ) -> dict[str, Any]:
        """Calculate signed COGS only through the shared canonical resolver."""

        alias_to_nm, ambiguous_aliases, _groups, _items = _nomenclature_identity_index(conn)
        cogs = ZERO
        matched_units = 0
        unmatched_units = 0
        problem_rows: list[dict[str, Any]] = []
        detail_rows: list[dict[str, Any]] = []
        dependency_digest = _StreamingCostDependencyDigest()
        resolution_cache: dict[tuple[str, str], dict[str, Any]] = {}
        source_units = {"projected_from_2026_07_01": 0, "canonical_exact_date": 0}
        operation_date_fallback_rows = 0
        operation_date_fallback_units = 0
        for row in rows:
            doc = str(row.get("docTypeName") or "").casefold()
            if doc not in {"продажа", "возврат"}:
                continue
            raw_qty = int(_decimal(row.get("quantity")))
            if raw_qty == 0:
                continue
            sign = 1 if doc == "продажа" else -1
            signed_qty = raw_qty * sign
            gross_qty = abs(signed_qty)
            nm_id, identity_method, identity_problem = _resolve_finance_nm_id(
                row,
                alias_to_nm=alias_to_nm,
                ambiguous_aliases=ambiguous_aliases,
            )
            operation_date, operation_date_source = _operation_date(row, week_start)
            if operation_date_source == "week_start_fallback":
                operation_date_fallback_rows += 1
                operation_date_fallback_units += gross_qty
                resolution = {
                    "status": "missing",
                    "reason": "operation_date_missing",
                    "nm_id": nm_id,
                    "operation_date": "",
                    "canonical_source_date": "",
                    "selection_method": "",
                    "formula_version": COST_METHOD_VERSION,
                }
            elif not nm_id:
                resolution = {
                    "status": "missing",
                    "reason": (
                        "finance_identity_ambiguous"
                        if identity_method == "ambiguous_alias"
                        else "finance_identity_unresolved"
                    ),
                    "nm_id": "",
                    "operation_date": operation_date.isoformat(),
                    "canonical_source_date": "",
                    "selection_method": "",
                    "formula_version": COST_METHOD_VERSION,
                }
            else:
                cache_key = (nm_id, operation_date.isoformat())
                resolution = resolution_cache.get(cache_key)
                if resolution is None:
                    resolution = resolve_finance_canonical_cost(
                        conn,
                        nm_id=nm_id,
                        operation_date=operation_date,
                    )
                    resolution_cache[cache_key] = resolution
            dependency = {
                **resolution,
                "report_id": str(row.get("reportId") or ""),
                "rrd_id": str(row.get("rrdId") or ""),
                "identity_method": identity_method,
                "identity_problem": identity_problem,
                "operation_date_source": operation_date_source,
            }
            dependency_digest.add(
                {
                    key: dependency.get(key)
                    for key in (
                        "report_id",
                        "rrd_id",
                        "nm_id",
                        "operation_date",
                        "canonical_source_date",
                        "canonical_source_identity",
                        "source_digest",
                        "quality",
                        "selection_method",
                        "status",
                        "reason",
                    )
                }
            )
            if resolution.get("status") != "resolved":
                unmatched_units += gross_qty
                problem_rows.append(
                    {
                        "sku": nm_id or str(row.get("nmId") or row.get("vendorCode") or row.get("sku") or "unknown"),
                        "nm_id": nm_id,
                        "operation_date": (
                            operation_date.isoformat()
                            if operation_date_source != "week_start_fallback"
                            else ""
                        ),
                        "source": "canonical_our_wb_cost",
                        "canonical_source_date": str(resolution.get("canonical_source_date") or ""),
                        "reason": str(resolution.get("reason") or "canonical_cost_missing"),
                        "operation_date_source": operation_date_source,
                        "unmatched_units": gross_qty,
                        "net_units": signed_qty,
                    }
                )
                continue
            unit_cost = _decimal(resolution["unit_cost_rub"])
            signed_cogs = Decimal(signed_qty) * unit_cost
            cogs += signed_cogs
            matched_units += gross_qty
            source_key = (
                "projected_from_2026_07_01"
                if operation_date < CANONICAL_COST_POLICY_DATE
                else "canonical_exact_date"
            )
            source_units[source_key] += gross_qty
            if include_details:
                detail_rows.append(
                    {
                        "report_id": str(row.get("reportId") or ""),
                        "rrd_id": str(row.get("rrdId") or ""),
                        "nm_id": nm_id,
                        "operation_date": operation_date.isoformat(),
                        "operation_date_source": operation_date_source,
                        "movement": "sale" if sign > 0 else "return",
                        "quantity": gross_qty,
                        "signed_quantity": signed_qty,
                        "unit_cost_rub": _money_text(unit_cost),
                        "cost_source": "canonical_our_wb_cost",
                        "source_date": str(resolution["canonical_source_date"]),
                        "source_identity": str(resolution["canonical_source_identity"]),
                        "source_digest": str(resolution["source_digest"]),
                        "source_quality": str(resolution["quality"]),
                        "projection_quality": str(resolution["projection_quality"]),
                        "selection_method": str(resolution["selection_method"]),
                        "formula_version": COST_METHOD_VERSION,
                        "signed_cogs_rub": _money_text(signed_cogs),
                    }
                )
        total_units = matched_units + unmatched_units
        coverage_pct = (
            Decimal(matched_units) / Decimal(total_units) * Decimal("100")
            if total_units
            else None
        )
        cost_state_hash = dependency_digest.finish()
        quality = {
            "cost_method_version": COST_METHOD_VERSION,
            "policy_date": CANONICAL_COST_POLICY_DATE.isoformat(),
            "source_units": source_units,
            "projected_units": source_units["projected_from_2026_07_01"],
            "exact_units": source_units["canonical_exact_date"],
            "fallback_units": "0.0000",
            "fallback_average_created": False,
            "silent_zero_created": False,
            "operation_date_fallback_rows": operation_date_fallback_rows,
            "operation_date_fallback_units": operation_date_fallback_units,
            "historical_projection_note": (
                "Operations before 2026-07-01 are a business-approved retrospective "
                "projection from the same SKU canonical cost on 2026-07-01."
            ),
        }
        return {
            "matched_units": matched_units,
            "unmatched_units": unmatched_units,
            "coverage_pct": _money_text(coverage_pct),
            "cogs_rub": _money_text(cogs) if unmatched_units == 0 else None,
            "partial_cogs_rub": _money_text(cogs),
            "problem_skus": problem_rows,
            "quality": quality,
            "cost_state_hash": cost_state_hash,
            "detail_rows": detail_rows if include_details else [],
        }

    def build_payload(self) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT s.week_start,s.week_end,a.metrics_json,a.report_ids_json,a.report_types_json,
                a.unknown_reasons_json,a.classifier_version,s.status,s.first_loaded_at,s.last_synced_at,
                s.report_count,s.raw_row_count,s.last_error,c.matched_units,c.unmatched_units,c.coverage_pct,
                c.problem_skus_json,c.quality_json,c.cost_state_hash,r.status reconciliation_status
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
                        "quality": json.loads(row["quality_json"] or "{}"),
                        "cost_state_hash": row["cost_state_hash"] or "",
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

    def plan_business_approved_backfill(
        self,
        *,
        date_from: date = RETRO_COST_FIRST_WEEK_START,
        date_to: date | None = None,
    ) -> dict[str, Any]:
        """Read-only production preflight for the Finance retro projection."""

        raise ValueError(
            "legacy business-approved Finance plan is permanently revoked; "
            "use plan_canonical_finance_backfill"
        )

        if not self.db_path.is_file():
            raise ValueError(f"Finance runtime SQLite does not exist: {self.db_path}")
        latest_closed = week_bounds(self.now_factory().astimezone(MOSCOW).date())[0] - timedelta(days=1)
        scope_end = date_to or latest_closed
        if scope_end < date_from:
            raise ValueError("date_to must not be earlier than date_from")
        if scope_end > latest_closed:
            raise ValueError(
                f"date_to must not exceed latest fully closed week end {latest_closed.isoformat()}"
            )
        with self._connect() as conn:
            conn.execute("BEGIN")
            try:
                return self._plan_business_approved_backfill_in_connection(
                    conn,
                    date_from=date_from,
                    date_to=scope_end,
                )
            finally:
                conn.rollback()

    def _plan_business_approved_backfill_in_connection(
        self,
        conn: sqlite3.Connection,
        *,
        date_from: date,
        date_to: date,
    ) -> dict[str, Any]:
        candidate_rows = conn.execute(
            """SELECT DISTINCT week_start,week_end
               FROM wb_finance_weekly_raw_rows
               WHERE seller_id=? AND week_end>=? AND week_end<=?
               ORDER BY week_start""",
            (self.seller_id, date_from.isoformat(), date_to.isoformat()),
        ).fetchall()
        candidates = [
            (date.fromisoformat(row["week_start"]), date.fromisoformat(row["week_end"]))
            for row in candidate_rows
        ]
        finance_weeks: list[dict[str, Any]] = []
        union_nm_ids: set[str] = set()
        identity_issues: list[dict[str, str]] = []
        finance_source_blockers: list[dict[str, str]] = []
        rows_without_nm_id = 0
        alias_to_nm, ambiguous_aliases, _groups, _items = _nomenclature_identity_index(conn)
        for start, end in candidates:
            key = (start.isoformat(), end.isoformat())
            raw_rows = conn.execute(
                """SELECT report_id,rrd_id,nm_id,row_hash,raw_json
                   FROM wb_finance_weekly_raw_rows
                   WHERE seller_id=? AND week_start=? AND week_end=?
                   ORDER BY report_id,rrd_id""",
                (self.seller_id, *key),
            ).fetchall()
            parsed = [json.loads(row["raw_json"]) for row in raw_rows]
            raw_week_nm_ids = {
                str(row.get("nmId") or "").strip()
                for row in parsed
                if str(row.get("docTypeName") or "").casefold()
                in {"продажа", "возврат"}
                and str(row.get("nmId") or "").strip() not in {"", "0"}
            }
            week_nm_ids: set[str] = set()
            week_identity_issues: list[dict[str, str]] = []
            zero_unit_identity_evidence: list[list[str]] = []
            for row in parsed:
                if str(row.get("docTypeName") or "").casefold() not in {
                    "продажа",
                    "возврат",
                }:
                    continue
                resolved_nm, method, reason = _resolve_finance_nm_id(
                    row,
                    alias_to_nm=alias_to_nm,
                    ambiguous_aliases=ambiguous_aliases,
                )
                if resolved_nm:
                    week_nm_ids.add(resolved_nm)
                else:
                    if int(_decimal(row.get("quantity"))) == 0:
                        zero_unit_identity_evidence.append(
                            [
                                str(row.get("reportId") or ""),
                                str(row.get("rrdId") or ""),
                                method,
                                reason,
                            ]
                        )
                        continue
                    issue = {
                        "week_start": key[0],
                        "report_id": str(row.get("reportId") or ""),
                        "rrd_id": str(row.get("rrdId") or ""),
                        "identity_method": method,
                        "reason": reason,
                    }
                    week_identity_issues.append(issue)
                    identity_issues.append(issue)
            union_nm_ids.update(week_nm_ids)
            no_nm_count = sum(
                1 for row in parsed if str(row.get("nmId") or "").strip() in {"", "0"}
            )
            rows_without_nm_id += no_nm_count
            report_rows = conn.execute(
                """SELECT report_id,report_type,row_count,content_hash
                   FROM wb_finance_weekly_reports
                   WHERE seller_id=? AND week_start=? AND week_end=?
                   ORDER BY report_id""",
                (self.seller_id, *key),
            ).fetchall()
            sync_row = conn.execute(
                """SELECT status,last_error FROM wb_finance_weekly_sync
                   WHERE seller_id=? AND week_start=? AND week_end=?""",
                (self.seller_id, *key),
            ).fetchone()
            sync_status = str(sync_row["status"] or "") if sync_row else "missing"
            if sync_status not in {"completed", "incomplete_cost"}:
                finance_source_blockers.append(
                    {
                        "code": "finance_source_week_not_completed",
                        "week_start": key[0],
                        "week_end": key[1],
                        "status": sync_status,
                        "reason": str(sync_row["last_error"] or "") if sync_row else "sync row missing",
                    }
                )
            raw_digest = hashlib.sha256(
                json.dumps(
                    [
                        [
                            row["report_id"],
                            row["rrd_id"],
                            row["nm_id"],
                            row["row_hash"],
                            hashlib.sha256(str(row["raw_json"]).encode("utf-8")).hexdigest(),
                        ]
                        for row in raw_rows
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            report_digest = hashlib.sha256(
                json.dumps(
                    [list(row) for row in report_rows],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            finance_weeks.append(
                {
                    "week_start": key[0],
                    "week_end": key[1],
                    "raw_row_count": len(raw_rows),
                    "report_count": len(report_rows),
                    "sale_return_nm_ids": sorted(week_nm_ids),
                    "raw_sale_return_nm_ids": sorted(raw_week_nm_ids),
                    "sale_return_identity_issues": week_identity_issues,
                    "zero_unit_unresolved_identity_count": len(
                        zero_unit_identity_evidence
                    ),
                    "zero_unit_unresolved_identity_digest": "sha256:"
                    + hashlib.sha256(
                        json.dumps(
                            sorted(zero_unit_identity_evidence),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                    "rows_without_nm_id": no_nm_count,
                    "sync_status": sync_status,
                    "sync_last_error": str(sync_row["last_error"] or "") if sync_row else "",
                    "raw_digest": f"sha256:{raw_digest}",
                    "report_digest": f"sha256:{report_digest}",
                }
            )
            del parsed, raw_rows

        proposed_rows, retro_blockers = self._build_retro_cost_rows(
            conn,
            rows=self._iter_retro_cost_movement_rows(conn, candidates=candidates),
        )
        persisted_map = self._load_retro_cost_map(conn)
        projected_map = {**persisted_map, **{row["nm_id"]: row for row in proposed_rows}}
        changed_weeks: list[dict[str, Any]] = []
        coverage_weeks: list[dict[str, Any]] = []
        blockers = [*retro_blockers, *finance_source_blockers]
        for start, end in candidates:
            key = (start.isoformat(), end.isoformat())
            parsed = [
                json.loads(row["raw_json"])
                for row in conn.execute(
                    """SELECT raw_json FROM wb_finance_weekly_raw_rows
                       WHERE seller_id=? AND week_start=? AND week_end=?
                       ORDER BY report_id,rrd_id""",
                    (self.seller_id, *key),
                ).fetchall()
            ]
            aggregate, coverage, unknown = self._aggregate_rows(
                conn,
                parsed,
                start,
                retro_cost_map=projected_map,
            )
            if int(coverage["unmatched_units"]):
                blockers.append(
                    {
                        "code": "finance_cost_coverage_incomplete",
                        "week_start": key[0],
                        "week_end": key[1],
                        "problem_skus": coverage["problem_skus"],
                    }
                )
            stored_aggregate_row = conn.execute(
                """SELECT metrics_json FROM wb_finance_weekly_aggregates
                   WHERE seller_id=? AND week_start=? AND week_end=?""",
                (self.seller_id, *key),
            ).fetchone()
            stored_coverage_row = conn.execute(
                """SELECT matched_units,unmatched_units,coverage_pct,cogs_rub,
                          problem_skus_json,quality_json,cost_state_hash
                   FROM wb_finance_weekly_cost_coverage
                   WHERE seller_id=? AND week_start=? AND week_end=?""",
                (self.seller_id, *key),
            ).fetchone()
            stored_metrics = (
                json.loads(stored_aggregate_row["metrics_json"])
                if stored_aggregate_row is not None
                else {}
            )
            stored_coverage = (
                {
                    "matched_units": stored_coverage_row["matched_units"],
                    "unmatched_units": stored_coverage_row["unmatched_units"],
                    "coverage_pct": stored_coverage_row["coverage_pct"],
                    "cogs_rub": stored_coverage_row["cogs_rub"],
                    "problem_skus": json.loads(stored_coverage_row["problem_skus_json"] or "[]"),
                    "quality": json.loads(stored_coverage_row["quality_json"] or "{}"),
                    "cost_state_hash": stored_coverage_row["cost_state_hash"] or "",
                }
                if stored_coverage_row is not None
                else {}
            )
            expected_coverage = {
                key_name: coverage[key_name]
                for key_name in (
                    "matched_units",
                    "unmatched_units",
                    "coverage_pct",
                    "cogs_rub",
                    "problem_skus",
                    "quality",
                    "cost_state_hash",
                )
            }
            quality = coverage["quality"]
            confirmed_share = quality.get("confirmed_share_pct")
            coverage_weeks.append(
                {
                    "week_start": key[0],
                    "week_end": key[1],
                    "before": stored_coverage,
                    "expected": expected_coverage,
                    "complete": int(coverage["unmatched_units"]) == 0,
                    "confirmed_share_interpretation": (
                        "complete_historical_frozen_projection"
                        if int(coverage["unmatched_units"]) == 0
                        and confirmed_share == "0.0000"
                        else "not_applicable"
                        if confirmed_share is None
                        else "canonical_quality_share"
                    ),
                    "unknown_reasons": unknown,
                }
            )
            changed = (
                stored_metrics != aggregate
                or stored_coverage != expected_coverage
            )
            if changed:
                changed_weeks.append(
                    {
                        "week_start": key[0],
                        "week_end": key[1],
                        "before": {
                            "cogs": stored_metrics.get("cogs"),
                            "profit_after_cogs": stored_metrics.get("profit_after_cogs"),
                            "final_margin_pct": stored_metrics.get("final_margin_pct"),
                            "matched_units": stored_coverage.get("matched_units"),
                            "unmatched_units": stored_coverage.get("unmatched_units"),
                            "cost_state_hash": stored_coverage.get("cost_state_hash"),
                        },
                        "expected": {
                            "cogs": aggregate.get("cogs"),
                            "profit_after_cogs": aggregate.get("profit_after_cogs"),
                            "final_margin_pct": aggregate.get("final_margin_pct"),
                            "total_wb_expenses": aggregate.get("total_wb_expenses"),
                            "profit_period_expenses": aggregate.get("profit_period_expenses"),
                            "wb_expenses_without_marketing_pct": aggregate.get(
                                "wb_expenses_without_marketing_pct"
                            ),
                            "matched_units": coverage["matched_units"],
                            "unmatched_units": coverage["unmatched_units"],
                            "coverage_pct": coverage["coverage_pct"],
                            "cost_state_hash": coverage["cost_state_hash"],
                            "problem_skus": coverage["problem_skus"],
                            "unknown_reasons": unknown,
                        },
                    }
                )
            del parsed

        target_keys = {
            (self.seller_id, item["week_start"], item["week_end"])
            for item in changed_weeks
        }
        ads_manifest = self._ads_coverage_manifest(
            conn,
            nm_ids=sorted(union_nm_ids),
            date_from=max(date_from, RETRO_COST_PERIOD_START),
            date_to=date_to,
        )
        finance_manifest = {
            "week_count": len(finance_weeks),
            "raw_row_count": sum(item["raw_row_count"] for item in finance_weeks),
            "report_count": sum(item["report_count"] for item in finance_weeks),
            "sale_return_nm_ids": sorted(union_nm_ids),
            "sale_return_nm_id_count": len(union_nm_ids),
            "sale_return_identity_issue_count": len(identity_issues),
            "sale_return_identity_issues": identity_issues,
            "zero_unit_unresolved_identity_count": sum(
                int(item["zero_unit_unresolved_identity_count"])
                for item in finance_weeks
            ),
            "zero_unit_unresolved_identity_digest": "sha256:"
            + hashlib.sha256(
                json.dumps(
                    [
                        [
                            item["week_start"],
                            item["zero_unit_unresolved_identity_count"],
                            item["zero_unit_unresolved_identity_digest"],
                        ]
                        for item in finance_weeks
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "rows_without_nm_id": rows_without_nm_id,
            "weeks": finance_weeks,
        }
        coverage_manifest = {
            "week_count": len(coverage_weeks),
            "complete_week_count": sum(1 for item in coverage_weeks if item["complete"]),
            "incomplete_week_count": sum(1 for item in coverage_weeks if not item["complete"]),
            "weeks": coverage_weeks,
        }
        coverage_manifest["digest"] = "sha256:" + hashlib.sha256(
            json.dumps(
                coverage_manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        cost_manifest = {
            "formula_version": RETRO_COST_FORMULA_VERSION,
            "period_start": RETRO_COST_PERIOD_START.isoformat(),
            "period_end": RETRO_COST_PERIOD_END.isoformat(),
            "reference_date": RETRO_COST_REFERENCE_DATE.isoformat(),
            "persisted_row_count": len(persisted_map),
            "proposed_row_count": len(proposed_rows),
            "rows": proposed_rows,
        }
        source_manifest = {
            "finance": finance_manifest,
            "cost": cost_manifest,
            "ads": ads_manifest,
            "coverage": coverage_manifest,
        }
        source_digest = "sha256:" + hashlib.sha256(
            json.dumps(
                source_manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        plan: dict[str, Any] = {
            "schema_version": "wb_finance_business_approved_backfill_v1",
            "status": "dry_run",
            "runtime_mutation": False,
            "apply_allowed": not blockers,
            "seller_id": self.seller_id,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "formula_versions": {
                "cost": COST_METHOD_VERSION,
                "retro_cost": RETRO_COST_FORMULA_VERSION,
                "profit": PROFIT_METHOD_VERSION,
            },
            "source_manifests": source_manifest,
            "source_digest": source_digest,
            "checked_week_count": len(candidates),
            "target_week_count": len(changed_weeks),
            "weeks": changed_weeks,
            "blockers": blockers,
            "target_before_digest": self._finance_state_digest(
                conn,
                target_keys=target_keys,
                target_only=True,
            ),
            "non_target_digest": self._finance_state_digest(
                conn,
                target_keys=target_keys,
                target_only=False,
            ),
            "backup_plan": {
                "method": "sqlite_online_backup",
                "integrity_check": "required_ok",
                "sha256": "required",
                "mode": "0600",
                "free_space_check": "required_before_backup",
            },
            "apply_plan": {
                "requires_exact_fingerprint": True,
                "single_immediate_transaction": True,
                "rollback_on_drift_or_error": True,
                "partial_or_force_modes": False,
            },
            "reconciliation_plan": {
                "target_readback": True,
                "full_cost_coverage": True,
                "non_target_invariants": True,
                "repeat_apply_noop": True,
                "audit_record": True,
            },
        }
        plan["fingerprint"] = "sha256:" + hashlib.sha256(
            json.dumps(
                plan,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return plan

    def _iter_retro_cost_movement_rows(
        self,
        conn: sqlite3.Connection,
        *,
        candidates: Iterable[tuple[date, date]],
    ) -> Iterable[dict[str, Any]]:
        """Stream only cost-bearing Finance movements one row at a time."""

        for start, end in candidates:
            for raw_row in conn.execute(
                """SELECT raw_json FROM wb_finance_weekly_raw_rows
                   WHERE seller_id=? AND week_start=? AND week_end=?
                   ORDER BY report_id,rrd_id""",
                (self.seller_id, start.isoformat(), end.isoformat()),
            ):
                row = json.loads(raw_row["raw_json"])
                if (
                    str(row.get("docTypeName") or "").casefold()
                    in {"продажа", "возврат"}
                    and int(_decimal(row.get("quantity"))) != 0
                ):
                    yield row

    def _build_retro_cost_rows(
        self,
        conn: sqlite3.Connection,
        *,
        rows: Iterable[Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        alias_to_nm, ambiguous_aliases, _groups, _items = _nomenclature_identity_index(conn)
        required: set[str] = set()
        blockers: list[dict[str, Any]] = []
        for row in rows:
            if str(row.get("docTypeName") or "").casefold() not in {"продажа", "возврат"}:
                continue
            if int(_decimal(row.get("quantity"))) == 0:
                continue
            operation_date, operation_source = _operation_date(row, RETRO_COST_PERIOD_START)
            if not (RETRO_COST_PERIOD_START <= operation_date <= RETRO_COST_PERIOD_END):
                continue
            nm_id, identity_method, identity_problem = _resolve_finance_nm_id(
                row,
                alias_to_nm=alias_to_nm,
                ambiguous_aliases=ambiguous_aliases,
            )
            if operation_source == "week_start_fallback":
                blockers.append(
                    {
                        "code": "retro_operation_date_missing",
                        "report_id": str(row.get("reportId") or ""),
                        "rrd_id": str(row.get("rrdId") or ""),
                    }
                )
            if not nm_id:
                blockers.append(
                    {
                        "code": "retro_finance_identity_unresolved",
                        "report_id": str(row.get("reportId") or ""),
                        "rrd_id": str(row.get("rrdId") or ""),
                        "identity_method": identity_method,
                        "reason": identity_problem,
                    }
                )
                continue
            required.add(nm_id)

        existing = self._load_retro_cost_map(conn)
        proposals: list[dict[str, Any]] = []
        for nm_id in sorted(required):
            candidate = self._select_retro_cost_source(conn, nm_id=nm_id)
            if candidate is None:
                blockers.append(
                    {
                        "code": "retro_canonical_cost_missing",
                        "nm_id": nm_id,
                        "reference_date": RETRO_COST_REFERENCE_DATE.isoformat(),
                    }
                )
                continue
            row_payload = {
                "seller_id": self.seller_id,
                "nm_id": nm_id,
                **candidate,
                "formula_version": RETRO_COST_FORMULA_VERSION,
                "status": "business_approved_retro",
            }
            fingerprint_payload = {
                key: row_payload[key]
                for key in (
                    "seller_id",
                    "nm_id",
                    "unit_cost_rub",
                    "source_date",
                    "source_table",
                    "source_row_sha256",
                    "selection_method",
                    "formula_version",
                    "status",
                )
            }
            row_payload["source_calculation_fingerprint"] = "sha256:" + hashlib.sha256(
                json.dumps(
                    fingerprint_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            persisted = existing.get(nm_id)
            if persisted is not None:
                if str(persisted.get("source_calculation_fingerprint") or "") != str(
                    row_payload["source_calculation_fingerprint"]
                ):
                    blockers.append(
                        {
                            "code": "retro_cost_immutable_conflict",
                            "nm_id": nm_id,
                            "persisted_fingerprint": str(
                                persisted.get("source_calculation_fingerprint") or ""
                            ),
                            "expected_fingerprint": row_payload[
                                "source_calculation_fingerprint"
                            ],
                        }
                    )
                continue
            proposals.append(row_payload)
        deduplicated = {
            json.dumps(item, ensure_ascii=False, sort_keys=True): item for item in blockers
        }
        return proposals, [deduplicated[key] for key in sorted(deduplicated)]

    def _select_retro_cost_source(
        self,
        conn: sqlite3.Connection,
        *,
        nm_id: str,
    ) -> dict[str, Any] | None:
        source_table = ""
        source_row: sqlite3.Row | None = None
        source_cost_column = ""
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        functional_active = False
        if {
            "sheet_vitrina_v1_warehouse_functional_cutovers",
            "sheet_vitrina_v1_warehouse_wb_daily_cost",
        }.issubset(tables):
            functional_active = (
                conn.execute(
                    """SELECT 1 FROM sheet_vitrina_v1_warehouse_functional_cutovers
                       WHERE cutover_id='warehouse_functional_cutover_v1' AND status='posted'"""
                ).fetchone()
                is not None
            )
        if functional_active:
            source_row = conn.execute(
                """SELECT * FROM sheet_vitrina_v1_warehouse_wb_daily_cost
                   WHERE cutover_id='warehouse_functional_cutover_v1'
                     AND nm_id=? AND as_of_date>=? AND wac_rub IS NOT NULL
                   ORDER BY CASE WHEN as_of_date=? THEN 0 ELSE 1 END, as_of_date
                   LIMIT 1""",
                (
                    nm_id,
                    RETRO_COST_REFERENCE_DATE.isoformat(),
                    RETRO_COST_REFERENCE_DATE.isoformat(),
                ),
            ).fetchone()
            if source_row is not None:
                source_table = "sheet_vitrina_v1_warehouse_wb_daily_cost"
                source_cost_column = "wac_rub"
        if (
            source_row is None
            and not functional_active
            and "sheet_vitrina_v1_wb_cost_daily_state" in tables
        ):
            source_row = conn.execute(
                """SELECT * FROM sheet_vitrina_v1_wb_cost_daily_state
                   WHERE nm_id=? AND as_of_date>=? AND our_wb_unit_cost_rub IS NOT NULL
                   ORDER BY CASE WHEN as_of_date=? THEN 0 ELSE 1 END, as_of_date
                   LIMIT 1""",
                (
                    nm_id,
                    RETRO_COST_REFERENCE_DATE.isoformat(),
                    RETRO_COST_REFERENCE_DATE.isoformat(),
                ),
            ).fetchone()
            if source_row is not None:
                source_table = "sheet_vitrina_v1_wb_cost_daily_state"
                source_cost_column = "our_wb_unit_cost_rub"
        if source_row is None:
            return None
        unit_cost = _decimal(source_row[source_cost_column])
        if unit_cost <= ZERO:
            return None
        source = {key: source_row[key] for key in source_row.keys()}
        source_row_json = json.dumps(
            source,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        source_date = str(source_row["as_of_date"])
        return {
            "unit_cost_rub": _money_text(unit_cost),
            "source_date": source_date,
            "source_table": source_table,
            "source_row_json": source_row_json,
            "source_row_sha256": "sha256:"
            + hashlib.sha256(source_row_json.encode("utf-8")).hexdigest(),
            "selection_method": (
                "exact_2026_07_01"
                if source_date == RETRO_COST_REFERENCE_DATE.isoformat()
                else "first_available_after_2026_07_01"
            ),
        }

    def _ads_coverage_manifest(
        self,
        conn: sqlite3.Connection,
        *,
        nm_ids: list[str],
        date_from: date,
        date_to: date,
    ) -> dict[str, Any]:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='temporal_source_slot_snapshots'"
        ).fetchone()
        wanted = {str(nm_id) for nm_id in nm_ids}
        cursor = date_from
        dates: list[dict[str, Any]] = []
        missing_pairs: list[dict[str, str]] = []
        invalid_pairs: list[dict[str, str]] = []
        evidence: list[Any] = []
        while cursor <= date_to:
            day = cursor.isoformat()
            row = (
                conn.execute(
                    """SELECT captured_at,payload_json
                       FROM temporal_source_slot_snapshots
                       WHERE source_key='ads_compact' AND snapshot_date=?
                         AND snapshot_role='accepted_closed_day_snapshot'""",
                    (day,),
                ).fetchone()
                if table_exists is not None
                else None
            )
            covered: set[str] = set()
            source_kind = "missing"
            envelope_origin = "missing"
            payload_digest = ""
            if row is not None:
                payload_json = str(row["payload_json"] or "")
                payload_digest = "sha256:" + hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
                try:
                    payload = json.loads(payload_json)
                except json.JSONDecodeError:
                    payload = {}
                result, envelope_origin = resolve_ads_snapshot_payload(payload)
                source_kind = str((result or {}).get("kind") or "invalid")
                items = (result or {}).get("items") or []
                for item in items if isinstance(items, list) else []:
                    if isinstance(item, dict):
                        value = str(item.get("nm_id", item.get("nmId", "")) or "")
                        if value in wanted:
                            raw_ads_sum = item.get("ads_sum")
                            try:
                                valid_ads_sum = (
                                    raw_ads_sum not in (None, "")
                                    and Decimal(str(raw_ads_sum)).is_finite()
                                )
                            except (InvalidOperation, ValueError, TypeError):
                                valid_ads_sum = False
                            if valid_ads_sum:
                                covered.add(value)
                            else:
                                invalid_pairs.append(
                                    {"date": day, "nm_id": value, "reason": "ads_sum_invalid"}
                                )
                if source_kind == "empty":
                    covered = set(wanted)
                if result is None:
                    invalid_pairs.extend(
                        {"date": day, "nm_id": nm_id, "reason": "ads_envelope_invalid"}
                        for nm_id in sorted(wanted)
                    )
            for nm_id in sorted(wanted - covered):
                missing_pairs.append({"date": day, "nm_id": nm_id})
            date_item = {
                "date": day,
                "source_kind": source_kind,
                "envelope_origin": envelope_origin,
                "covered_nm_id_count": len(covered),
                "missing_nm_id_count": len(wanted - covered),
                "captured_at": str(row["captured_at"] or "") if row is not None else "",
                "payload_digest": payload_digest,
            }
            dates.append(date_item)
            evidence.append(date_item)
            cursor += timedelta(days=1)
        return {
            "source": "ads_compact/fullstats accepted closed-day snapshots",
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "nm_id_count": len(wanted),
            "date_count": len(dates),
            "complete": not missing_pairs and not invalid_pairs,
            "missing_date_nm_id_count": len(missing_pairs),
            "missing_date_nm_ids": missing_pairs,
            "invalid_date_nm_id_count": len(invalid_pairs),
            "invalid_date_nm_ids": invalid_pairs,
            "dates": dates,
            "digest": "sha256:"
            + hashlib.sha256(
                json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        }

    def plan_canonical_finance_backfill(
        self,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> dict[str, Any]:
        """Read-only all-history Finance preflight bound to canonical cost truth."""

        uri = f"file:{self.db_path.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=60) as conn:
            conn.row_factory = sqlite3.Row
            return self._plan_canonical_finance_backfill_in_connection(
                conn,
                date_from=date_from,
                date_to=date_to,
            )

    def _plan_canonical_finance_backfill_in_connection(
        self,
        conn: sqlite3.Connection,
        *,
        date_from: date | None,
        date_to: date | None,
    ) -> dict[str, Any]:
        required_tables = {
            "wb_finance_weekly_raw_rows",
            "wb_finance_weekly_reports",
            "wb_finance_weekly_sync",
            "wb_finance_weekly_aggregates",
            "wb_finance_weekly_cost_coverage",
            "wb_finance_weekly_reconciliation",
            "wb_finance_weekly_sku_aggregates",
            "sheet_vitrina_v1_nomenclature_items",
            "sheet_vitrina_v1_warehouse_wb_daily_cost",
            "sheet_vitrina_v1_warehouse_functional_cutovers",
        }
        existing_tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        missing_schema = sorted(required_tables - existing_tables)
        if "wb_finance_weekly_raw_rows" not in existing_tables:
            raise ValueError("Finance raw schema is not deployed")
        bounds = conn.execute(
            """SELECT MIN(week_start) first_week,MAX(week_end) last_week,
                      COUNT(*) raw_row_count,COUNT(DISTINCT NULLIF(nm_id,'')) nm_id_count
               FROM wb_finance_weekly_raw_rows WHERE seller_id=?""",
            (self.seller_id,),
        ).fetchone()
        if bounds is None or not bounds["first_week"]:
            raise ValueError("Finance raw history is empty")
        scope_from = date_from or date.fromisoformat(str(bounds["first_week"]))
        scope_to = date_to or date.fromisoformat(str(bounds["last_week"]))
        week_rows = conn.execute(
            """SELECT DISTINCT week_start,week_end FROM wb_finance_weekly_raw_rows
               WHERE seller_id=? AND week_end>=? AND week_start<=?
               ORDER BY week_start""",
            (self.seller_id, scope_from.isoformat(), scope_to.isoformat()),
        ).fetchall()
        weeks: list[dict[str, Any]] = []
        matrix: list[dict[str, Any]] = []
        blockers: list[dict[str, Any]] = [
            {"code": "required_schema_missing", "tables": missing_schema}
        ] if missing_schema else []
        finance_manifest_digest = _StreamingJsonArrayDigest()
        cost_manifest: dict[str, dict[str, Any]] = {}
        union_nm_ids: set[str] = set()
        target_before_digest = _StreamingJsonArrayDigest()
        target_after_digest = _StreamingJsonArrayDigest()
        expected_sku_projection_row_count = 0
        for week in week_rows:
            start = date.fromisoformat(str(week["week_start"]))
            end = date.fromisoformat(str(week["week_end"]))
            stored_rows = conn.execute(
                """SELECT report_id,rrd_id,row_hash,raw_json
                   FROM wb_finance_weekly_raw_rows
                   WHERE seller_id=? AND week_start=? AND week_end=?
                   ORDER BY report_id,rrd_id""",
                (self.seller_id, start.isoformat(), end.isoformat()),
            ).fetchall()
            parsed = [json.loads(str(row["raw_json"])) for row in stored_rows]
            for row in stored_rows:
                finance_manifest_digest.add(
                    [
                        start.isoformat(),
                        str(row["report_id"]),
                        str(row["rrd_id"]),
                        str(row["row_hash"]),
                    ]
                )
            new_metrics, new_coverage, unknown = self._aggregate_rows(conn, parsed, start)
            detailed = self._calculate_cogs(conn, parsed, start, include_details=True)
            stored_aggregate = (
                conn.execute(
                    """SELECT classifier_version,metrics_json FROM wb_finance_weekly_aggregates
                       WHERE seller_id=? AND week_start=? AND week_end=?""",
                    (self.seller_id, start.isoformat(), end.isoformat()),
                ).fetchone()
                if "wb_finance_weekly_aggregates" in existing_tables
                else None
            )
            stored_coverage = (
                conn.execute(
                    """SELECT matched_units,unmatched_units,coverage_pct,cogs_rub,
                              problem_skus_json,quality_json,cost_state_hash
                       FROM wb_finance_weekly_cost_coverage
                       WHERE seller_id=? AND week_start=? AND week_end=?""",
                    (self.seller_id, start.isoformat(), end.isoformat()),
                ).fetchone()
                if "wb_finance_weekly_cost_coverage" in existing_tables
                else None
            )
            stored_sku_aggregates = (
                [
                    dict(row)
                    for row in conn.execute(
                        """SELECT seller_id,week_start,week_end,nm_id,formula_version,
                                  metrics_json,coverage_json,raw_source_digest,
                                  week_content_hash,cost_state_hash,raw_row_count
                           FROM wb_finance_weekly_sku_aggregates
                           WHERE seller_id=? AND week_start=? AND week_end=?
                           ORDER BY nm_id""",
                        (self.seller_id, start.isoformat(), end.isoformat()),
                    ).fetchall()
                ]
                if "wb_finance_weekly_sku_aggregates" in existing_tables
                else []
            )
            old_metrics = (
                json.loads(str(stored_aggregate["metrics_json"] or "{}"))
                if stored_aggregate is not None
                else {}
            )
            old_cogs = self._nullable_decimal(old_metrics.get("cogs"))
            new_cogs = self._nullable_decimal(new_metrics.get("cogs"))
            old_profit = self._nullable_decimal(old_metrics.get("profit_after_cogs"))
            new_profit = self._nullable_decimal(new_metrics.get("profit_after_cogs"))
            old_margin = self._nullable_decimal(old_metrics.get("final_margin_pct"))
            new_margin = self._nullable_decimal(new_metrics.get("final_margin_pct"))
            old_before_cogs = self._nullable_decimal(old_metrics.get("before_cogs_profit"))
            new_before_cogs = self._nullable_decimal(new_metrics.get("before_cogs_profit"))
            cogs_delta = self._delta_text(new_cogs, old_cogs)
            profit_delta = self._delta_text(new_profit, old_profit)
            margin_delta = self._delta_text(new_margin, old_margin)
            before_cogs_delta = self._delta_text(new_before_cogs, old_before_cogs)
            cogs_profit_effect = (
                _money_text(-(new_cogs - old_cogs))
                if new_cogs is not None and old_cogs is not None
                else None
            )
            profit_component_keys = (
                "net_revenue",
                "agent_remuneration",
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
                "corrections",
                "capitalized_acceptance",
                "capitalized_transit_logistics",
                "positive_adjustments",
                "profit_period_expenses",
                "before_cogs_profit",
                "cogs",
                "profit_after_cogs",
                "final_margin_pct",
            )
            component_reconciliation: dict[str, dict[str, str | None]] = {}
            changed_components: list[str] = []
            for key in profit_component_keys:
                old_value = self._nullable_decimal(old_metrics.get(key))
                new_value = self._nullable_decimal(new_metrics.get(key))
                delta_value = self._delta_text(new_value, old_value)
                component_reconciliation[key] = {
                    "before": _money_text(old_value),
                    "after": _money_text(new_value),
                    "delta": delta_value,
                }
                if delta_value is not None and _decimal(delta_value) != ZERO:
                    changed_components.append(key)
            profit_difference_explanation = (
                "profit delta equals the negative COGS delta; before-COGS profit is unchanged"
                if profit_delta is not None
                and cogs_profit_effect is not None
                and _decimal(profit_delta) == _decimal(cogs_profit_effect)
                and before_cogs_delta is not None
                and _decimal(before_cogs_delta) == ZERO
                else (
                    "profit delta = before-COGS profit delta "
                    f"{before_cogs_delta or 'NULL'} + COGS effect {cogs_profit_effect or 'NULL'}; "
                    "changed measured inputs: "
                    + (", ".join(changed_components) if changed_components else "legacy inputs unavailable")
                )
            )
            week_item = {
                "week_start": start.isoformat(),
                "week_end": end.isoformat(),
                "raw_row_count": len(stored_rows),
                "before": {
                    "cogs_rub": _money_text(old_cogs),
                    "profit_after_cogs_rub": _money_text(old_profit),
                    "margin_pct": _money_text(old_margin),
                    "before_cogs_profit_rub": _money_text(old_before_cogs),
                },
                "after": {
                    "cogs_rub": _money_text(new_cogs),
                    "profit_after_cogs_rub": _money_text(new_profit),
                    "margin_pct": _money_text(new_margin),
                    "before_cogs_profit_rub": _money_text(new_before_cogs),
                    "agent_remuneration_rub": new_metrics.get("agent_remuneration"),
                    "acquiring_rub": new_metrics.get("acquiring"),
                    "combined_commission_control_rub": new_metrics.get("combined_commission_control"),
                    "commission_control_reconciliation_rub": new_metrics.get("commission_control_reconciliation_rub"),
                    "capitalized_acceptance_rub": new_metrics.get("capitalized_acceptance"),
                    "capitalized_transit_rub": new_metrics.get("capitalized_transit_logistics"),
                },
                "delta": {
                    "cogs_rub": cogs_delta,
                    "profit_after_cogs_rub": profit_delta,
                    "margin_pct_points": margin_delta,
                    "before_cogs_profit_rub": before_cogs_delta,
                    "cogs_profit_effect_rub": cogs_profit_effect,
                },
                "profit_delta_inputs": {
                    "raw_fields": [
                        "docTypeName",
                        "retailPriceWithDisc",
                        "forPay",
                        "acquiringFee",
                        "additionalPayment",
                        "deliveryService",
                        "paidStorage",
                        "paidAcceptance",
                        "penalty",
                        "deduction",
                        "bonusTypeName",
                        "quantity",
                        "nmId/vendorCode/sku",
                        "giId",
                        "rrDate/saleDt/orderDt",
                    ],
                    "derived_fields": [
                        "agent_remuneration",
                        "acquiring",
                        "profit_period_expenses",
                        "capitalized_acceptance",
                        "capitalized_transit_logistics",
                        "positive_adjustments",
                        "cogs",
                    ],
                    "source_contracts": [
                        CLASSIFIER_VERSION,
                        PROFIT_METHOD_VERSION,
                        COST_METHOD_VERSION,
                        "sheet_vitrina_v1_wb_supply_cost_layers",
                        "sheet_vitrina_v1_warehouse_wb_daily_cost",
                    ],
                    "component_reconciliation": component_reconciliation,
                    "profit_identity": (
                        "profit_after_cogs_delta = before_cogs_profit_delta - cogs_delta"
                    ),
                },
                "profit_delta_explanation": profit_difference_explanation,
                "coverage": {
                    key: detailed.get(key)
                    for key in (
                        "matched_units",
                        "unmatched_units",
                        "coverage_pct",
                        "problem_skus",
                        "cost_state_hash",
                    )
                },
                "capitalization_reconciliation": new_metrics.get("capitalization_reconciliation"),
                "unknown_reasons": unknown,
            }
            weeks.append(week_item)
            if int(detailed["unmatched_units"]):
                blockers.append(
                    {
                        "code": "canonical_cost_coverage_incomplete",
                        "week_start": start.isoformat(),
                        "problem_skus": detailed["problem_skus"],
                    }
                )
            grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
            for detail in detailed["detail_rows"]:
                nm_id = str(detail["nm_id"])
                union_nm_ids.add(nm_id)
                key = (
                    nm_id,
                    str(detail["operation_date"]),
                    str(detail["source_date"]),
                    str(detail["unit_cost_rub"]),
                )
                item = grouped.setdefault(
                    key,
                    {
                        "week_start": start.isoformat(),
                        "week_end": end.isoformat(),
                        "nm_id": nm_id,
                        "operation_date": detail["operation_date"],
                        "canonical_source_date": detail["source_date"],
                        "unit_cost_rub": detail["unit_cost_rub"],
                        "source": detail["cost_source"],
                        "source_quality": detail["source_quality"],
                        "projection_quality": detail["projection_quality"],
                        "selection_method": detail["selection_method"],
                        "source_identity": detail["source_identity"],
                        "source_digest": detail["source_digest"],
                        "sales_qty": 0,
                        "returns_qty": 0,
                        "signed_cogs_rub": ZERO,
                        "reason": "canonical_exact_match",
                    },
                )
                if detail["movement"] == "sale":
                    item["sales_qty"] += int(detail["quantity"])
                else:
                    item["returns_qty"] += int(detail["quantity"])
                item["signed_cogs_rub"] += _decimal(detail["signed_cogs_rub"])
                cost_manifest[str(detail["source_identity"])] = {
                    "source_identity": detail["source_identity"],
                    "source_date": detail["source_date"],
                    "nm_id": nm_id,
                    "quality": detail["source_quality"],
                    "unit_cost_rub": detail["unit_cost_rub"],
                    "source_digest": detail["source_digest"],
                }
            for item in grouped.values():
                item["signed_cogs_rub"] = _money_text(item["signed_cogs_rub"])
                matrix.append(item)
            for problem in detailed["problem_skus"]:
                if problem.get("nm_id"):
                    union_nm_ids.add(str(problem["nm_id"]))
                matrix.append(
                    {
                        "week_start": start.isoformat(),
                        "week_end": end.isoformat(),
                        "nm_id": str(problem.get("nm_id") or problem.get("sku") or ""),
                        "operation_date": str(problem.get("operation_date") or ""),
                        "canonical_source_date": str(problem.get("canonical_source_date") or ""),
                        "sales_qty": None,
                        "returns_qty": None,
                        "unit_cost_rub": None,
                        "source": "canonical_our_wb_cost",
                        "source_quality": "missing",
                        "projection_quality": "missing",
                        "selection_method": "",
                        "signed_cogs_rub": None,
                        "reason": str(problem.get("reason") or "canonical_cost_missing"),
                    }
                )
            target_before_digest.add(
                {
                    "week_start": start.isoformat(),
                    "aggregate": dict(stored_aggregate) if stored_aggregate is not None else None,
                    "coverage": dict(stored_coverage) if stored_coverage is not None else None,
                    "sku_aggregates": stored_sku_aggregates,
                }
            )
            # The matrix and cost manifest above are the durable operation-level
            # evidence. Release duplicate parsed/detail objects before the
            # independently rebuilt per-SKU projection parses this week again.
            del detailed
            del parsed
            expected_sku_projections = self._rebuild_sku_week_aggregates(
                conn,
                week_start=start,
                week_end=end,
                raw_rows=stored_rows,
                global_metrics=new_metrics,
                calculated_at="",
                persist=False,
            )
            expected_sku_projection_row_count += len(expected_sku_projections)
            target_after_digest.add(
                {
                    "week_start": start.isoformat(),
                    "metrics": new_metrics,
                    "coverage": new_coverage,
                    "unknown": unknown,
                    "sku_aggregates": expected_sku_projections,
                }
            )
        finance_digest = finance_manifest_digest.finish()
        cost_rows = [cost_manifest[key] for key in sorted(cost_manifest)]
        canonical_july_first_manifest: list[dict[str, Any]] = []
        for nm_id in sorted(
            union_nm_ids,
            key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value),
        ):
            resolution = resolve_finance_canonical_cost(
                conn,
                nm_id=nm_id,
                operation_date=CANONICAL_COST_POLICY_DATE,
            )
            canonical_july_first_manifest.append(
                {
                    "nm_id": nm_id,
                    "status": str(resolution.get("status") or "missing"),
                    "reason": str(resolution.get("reason") or ""),
                    "source_date": str(resolution.get("canonical_source_date") or ""),
                    "unit_cost_rub": resolution.get("unit_cost_rub"),
                    "quality": str(resolution.get("quality") or "missing"),
                    "source_identity": str(
                        resolution.get("canonical_source_identity") or ""
                    ),
                    "source_digest": str(resolution.get("source_digest") or ""),
                }
            )
        cost_digest = self._json_digest(
            {
                "operation_sources": cost_rows,
                "canonical_2026_07_01": canonical_july_first_manifest,
            }
        )
        missing_canonical_cost_nm_ids = sorted(
            {
                str(item.get("nm_id") or "")
                for item in matrix
                if item.get("source_quality") == "missing" and item.get("nm_id")
            },
            key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value),
        )
        ads_manifest = self._ads_coverage_manifest(
            conn,
            nm_ids=sorted(union_nm_ids),
            date_from=scope_from,
            date_to=scope_to,
        )
        non_target_manifest = self._canonical_non_target_manifest(
            conn,
            date_from=scope_from,
            date_to=scope_to,
        )
        plan: dict[str, Any] = {
            "status": "blocked" if blockers else "ready",
            "schema_version": "wb_finance_canonical_cost_backfill_v2",
            "dry_run": True,
            "seller_id": self.seller_id,
            "date_from": scope_from.isoformat(),
            "date_to": scope_to.isoformat(),
            "week_count": len(weeks),
            "finance_row_count": finance_manifest_digest.count,
            "finance_nm_id_count": len(union_nm_ids),
            "weeks": weeks,
            "week_nm_operation_date_matrix": matrix,
            "source_manifests": {
                "finance": {"row_count": finance_manifest_digest.count, "digest": finance_digest},
                "cost": {
                    "source": "canonical Our WB Cost",
                    "row_count": len(cost_rows),
                    "digest": cost_digest,
                    "rows": cost_rows,
                    "canonical_2026_07_01_rows": [
                        row for row in cost_rows if row["source_date"] == "2026-07-01"
                    ],
                    "canonical_2026_07_01_manifest": {
                        "row_count": len(canonical_july_first_manifest),
                        "resolved_count": sum(
                            item["status"] == "resolved"
                            for item in canonical_july_first_manifest
                        ),
                        "missing_nm_ids": [
                            item["nm_id"]
                            for item in canonical_july_first_manifest
                            if item["status"] != "resolved"
                        ],
                        "digest": self._json_digest(canonical_july_first_manifest),
                        "rows": canonical_july_first_manifest,
                    },
                    "exact_date_rows_after_2026_07_01": [
                        row for row in cost_rows if row["source_date"] > "2026-07-01"
                    ],
                    "missing_nm_id_count": len(missing_canonical_cost_nm_ids),
                    "missing_nm_ids": missing_canonical_cost_nm_ids,
                },
                "ads": ads_manifest,
            },
            "target_before_digest": target_before_digest.finish(),
            "expected_target_after_digest": target_after_digest.finish(),
            "non_target_manifest": non_target_manifest,
            "non_target_digest": self._json_digest(non_target_manifest),
            "write_set": {
                "tables": [
                    "wb_finance_weekly_aggregates",
                    "wb_finance_weekly_cost_coverage",
                    "wb_finance_weekly_reconciliation",
                    "wb_finance_weekly_sku_aggregates",
                    "wb_finance_weekly_sync.status",
                    "wb_finance_projection_audit",
                ],
                "week_count": len(weeks),
                "retro_cost_map_rows": 0,
                "weeks": [
                    {"week_start": item["week_start"], "week_end": item["week_end"]}
                    for item in weeks
                ],
                "expected_sku_projection_row_count": expected_sku_projection_row_count,
            },
            "invariants": {
                "raw_finance_rows_immutable": True,
                "ads_rows_immutable": True,
                "ads_missing_never_written_as_zero": True,
                "canonical_cost_rows_immutable": True,
                "exact_date_cost_values_from_2026_07_01_unchanged": True,
                "fallback_average_created": False,
                "silent_zero_created": False,
                "legacy_cost_price_used": False,
                "retro_cost_map_read_or_written": False,
                "sku_aggregate_bound_to_target_readback": True,
            },
            "backup_recovery_plan": {
                "required_before_apply": True,
                "coherent_sqlite_backup": True,
                "integrity_check": "ok required",
                "permissions": "0600",
                "sha256": "required",
                "transaction": "single BEGIN IMMEDIATE with rollback on drift/error",
            },
            "blockers": blockers,
            "apply_allowed": not blockers,
            "human_approval_required": True,
            "revoked_fingerprints": [
                "sha256:621323d6f03759cb8685dfffe20639fa18a16c7b5f6a5b1685205a579c6bbf2d"
            ],
        }
        plan["fingerprint"] = self._json_digest(plan)
        return plan

    @staticmethod
    def _nullable_decimal(value: Any) -> Decimal | None:
        if value in (None, ""):
            return None
        try:
            result = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
        return result if result.is_finite() else None

    @staticmethod
    def _delta_text(after: Decimal | None, before: Decimal | None) -> str | None:
        return _money_text(after - before) if after is not None and before is not None else None

    @staticmethod
    def _json_digest(value: Any) -> str:
        return "sha256:" + hashlib.sha256(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()

    def _canonical_non_target_manifest(
        self,
        conn: sqlite3.Connection,
        *,
        date_from: date,
        date_to: date,
    ) -> dict[str, Any]:
        manifests: dict[str, Any] = {}
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        queries = {
            "finance_raw": (
                "SELECT report_id,rrd_id,row_hash FROM wb_finance_weekly_raw_rows WHERE seller_id=? AND week_end>=? AND week_start<=? ORDER BY report_id,rrd_id",
                (self.seller_id, date_from.isoformat(), date_to.isoformat()),
            ),
        }
        if "sheet_vitrina_v1_warehouse_wb_daily_cost" in tables:
            queries["canonical_cost"] = (
                "SELECT cutover_id,as_of_date,nm_id,fingerprint FROM sheet_vitrina_v1_warehouse_wb_daily_cost WHERE as_of_date>=? AND as_of_date<=? ORDER BY as_of_date,nm_id",
                (CANONICAL_COST_POLICY_DATE.isoformat(), date_to.isoformat()),
            )
        else:
            manifests["canonical_cost"] = {
                "row_count": 0,
                "digest": self._json_digest([]),
                "missing_table": True,
            }
        if "temporal_source_slot_snapshots" in tables:
            queries["ads_snapshots"] = (
                "SELECT snapshot_date,captured_at,payload_json FROM temporal_source_slot_snapshots WHERE source_key='ads_compact' AND snapshot_date>=? AND snapshot_date<=? ORDER BY snapshot_date",
                (date_from.isoformat(), date_to.isoformat()),
            )
        if "sheet_vitrina_v1_wb_supply_cost_layers" in tables:
            queries["supply_cost_layers"] = (
                "SELECT wb_supply_cost_layer_id,wb_supply_id,nm_id,inputs_hash,version,is_current FROM sheet_vitrina_v1_wb_supply_cost_layers ORDER BY wb_supply_cost_layer_id",
                (),
            )
        for name, (query, params) in queries.items():
            digest = _StreamingJsonArrayDigest()
            for row in conn.execute(query, params):
                digest.add(dict(row))
            manifests[name] = {"row_count": digest.count, "digest": digest.finish()}
        return manifests

    def apply_canonical_finance_backfill(
        self,
        *,
        expected_fingerprint: str,
        approval_reference: str,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> dict[str, Any]:
        revoked = "sha256:621323d6f03759cb8685dfffe20639fa18a16c7b5f6a5b1685205a579c6bbf2d"
        if expected_fingerprint == revoked:
            raise ValueError("the former Finance plan fingerprint is permanently revoked")
        if not str(approval_reference or "").strip():
            raise ValueError("canonical Finance apply requires approval_reference")
        with self._connect() as conn:
            existing = conn.execute(
                """SELECT scope_json,result_json FROM wb_finance_projection_audit
                   WHERE seller_id=? AND action='apply_canonical_finance_backfill'
                     AND fingerprint=? ORDER BY created_at DESC LIMIT 1""",
                (self.seller_id, expected_fingerprint),
            ).fetchone()
            if existing is not None:
                result = json.loads(str(existing["result_json"] or "{}"))
                prior_scope = json.loads(str(existing["scope_json"] or "{}"))
                current = self._plan_canonical_finance_backfill_in_connection(
                    conn,
                    date_from=date_from,
                    date_to=date_to,
                )
                current_scope = {
                    "date_from": current["date_from"],
                    "date_to": current["date_to"],
                }
                if current_scope != prior_scope:
                    raise ValueError(
                        "previously applied canonical Finance fingerprint belongs to a different scope"
                    )
                if (
                    not result.get("post_apply_fingerprint")
                    or str(current["fingerprint"]) != str(result["post_apply_fingerprint"])
                ):
                    raise ValueError(
                        "previously applied canonical Finance state has drifted; a new dry-run and approval are required"
                    )
                return {**result, "status": "no_op_already_applied", "idempotent": True}
            conn.execute("BEGIN IMMEDIATE")
            try:
                plan = self._plan_canonical_finance_backfill_in_connection(
                    conn,
                    date_from=date_from,
                    date_to=date_to,
                )
                if str(plan["fingerprint"]) != expected_fingerprint:
                    raise ValueError("canonical Finance plan fingerprint drifted before apply")
                if not bool(plan["apply_allowed"]):
                    raise ValueError("canonical Finance plan contains blockers")
                non_target_before = str(plan["non_target_digest"])
                for week in plan["weeks"]:
                    self._recalculate_week_in_connection(
                        conn,
                        date.fromisoformat(str(week["week_start"])),
                        date.fromisoformat(str(week["week_end"])),
                    )
                target_readback_digest = _StreamingJsonArrayDigest()
                for week in plan["weeks"]:
                    start = date.fromisoformat(str(week["week_start"]))
                    end = date.fromisoformat(str(week["week_end"]))
                    aggregate_row = conn.execute(
                        """SELECT metrics_json,unknown_reasons_json FROM wb_finance_weekly_aggregates
                           WHERE seller_id=? AND week_start=? AND week_end=?""",
                        (self.seller_id, start.isoformat(), end.isoformat()),
                    ).fetchone()
                    raw = [
                        json.loads(str(row["raw_json"]))
                        for row in conn.execute(
                            """SELECT raw_json FROM wb_finance_weekly_raw_rows
                               WHERE seller_id=? AND week_start=? AND week_end=?
                               ORDER BY report_id,rrd_id""",
                            (self.seller_id, start.isoformat(), end.isoformat()),
                        ).fetchall()
                    ]
                    sku_aggregates = [
                        dict(row)
                        for row in conn.execute(
                            """SELECT seller_id,week_start,week_end,nm_id,formula_version,
                                      metrics_json,coverage_json,raw_source_digest,
                                      week_content_hash,cost_state_hash,raw_row_count
                               FROM wb_finance_weekly_sku_aggregates
                               WHERE seller_id=? AND week_start=? AND week_end=?
                               ORDER BY nm_id""",
                            (self.seller_id, start.isoformat(), end.isoformat()),
                        ).fetchall()
                    ]
                    target_readback_digest.add(
                        {
                            "week_start": start.isoformat(),
                            "metrics": json.loads(str(aggregate_row["metrics_json"] or "{}")),
                            "coverage": self._calculate_cogs(
                                conn, raw, start
                            ),
                            "unknown": json.loads(
                                str(aggregate_row["unknown_reasons_json"] or "[]")
                            ),
                            "sku_aggregates": sku_aggregates,
                        }
                    )
                target_after_digest = target_readback_digest.finish()
                if target_after_digest != str(plan["expected_target_after_digest"]):
                    raise ValueError("target Finance readback digest differs from reviewed plan")
                non_target_after_manifest = self._canonical_non_target_manifest(
                    conn,
                    date_from=date.fromisoformat(str(plan["date_from"])),
                    date_to=date.fromisoformat(str(plan["date_to"])),
                )
                non_target_after = self._json_digest(non_target_after_manifest)
                if non_target_after != non_target_before:
                    raise ValueError("non-target Finance/ads/cost invariants changed during apply")
                post_apply_plan = self._plan_canonical_finance_backfill_in_connection(
                    conn,
                    date_from=date.fromisoformat(str(plan["date_from"])),
                    date_to=date.fromisoformat(str(plan["date_to"])),
                )
                if post_apply_plan["blockers"] or any(
                    any(
                        value not in {None, "0.0000"}
                        for value in week["delta"].values()
                    )
                    for week in post_apply_plan["weeks"]
                ):
                    raise ValueError("post-apply canonical Finance plan is not reconciled to zero")
                now = self.now_factory().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                result = {
                    "status": "applied",
                    "fingerprint": expected_fingerprint,
                    "approval_reference": str(approval_reference),
                    "week_count": int(plan["week_count"]),
                    "non_target_digest_before": non_target_before,
                    "non_target_digest_after": non_target_after,
                    "target_digest": target_after_digest,
                    "post_apply_fingerprint": post_apply_plan["fingerprint"],
                    "idempotent": True,
                    "retro_cost_map_rows_written": 0,
                    "applied_at": now,
                }
                audit_id = hashlib.sha256(
                    f"{self.seller_id}|canonical|{expected_fingerprint}|{now}".encode("utf-8")
                ).hexdigest()
                conn.execute(
                    """INSERT INTO wb_finance_projection_audit(
                       audit_id,seller_id,action,fingerprint,scope_json,result_json,created_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        audit_id,
                        self.seller_id,
                        "apply_canonical_finance_backfill",
                        expected_fingerprint,
                        json.dumps(
                            {"date_from": plan["date_from"], "date_to": plan["date_to"]},
                            separators=(",", ":"),
                        ),
                        json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                        now,
                    ),
                )
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    def canonical_finance_fingerprint_applied(self, *, fingerprint: str) -> bool:
        """Return whether this exact reviewed canonical plan already committed."""

        self.ensure_schema()
        with self._connect() as conn:
            return (
                conn.execute(
                    """SELECT 1 FROM wb_finance_projection_audit
                       WHERE seller_id=? AND action='apply_canonical_finance_backfill'
                         AND fingerprint=? LIMIT 1""",
                    (self.seller_id, str(fingerprint)),
                ).fetchone()
                is not None
            )

    def apply_business_approved_backfill(
        self,
        *,
        expected_fingerprint: str,
        approval_reference: str,
        date_from: date = RETRO_COST_FIRST_WEEK_START,
        date_to: date | None = None,
    ) -> dict[str, Any]:
        raise ValueError(
            "legacy business-approved Finance apply and all former fingerprints are permanently revoked"
        )
        approval_reference = str(approval_reference or "").strip()
        if not approval_reference:
            raise ValueError("business-approved Finance apply requires approval_reference")
        latest_closed = week_bounds(self.now_factory().astimezone(MOSCOW).date())[0] - timedelta(days=1)
        scope_end = date_to or latest_closed
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                prior_apply = conn.execute(
                    """SELECT audit_id,scope_json,result_json
                       FROM wb_finance_projection_audit
                       WHERE seller_id=? AND action='apply_business_approved_backfill'
                         AND fingerprint=? ORDER BY created_at DESC LIMIT 1""",
                    (self.seller_id, expected_fingerprint),
                ).fetchone()
                plan = self._plan_business_approved_backfill_in_connection(
                    conn,
                    date_from=date_from,
                    date_to=scope_end,
                )
                if prior_apply is not None:
                    prior_scope = json.loads(str(prior_apply["scope_json"] or "{}"))
                    expected_scope = {
                        "date_from": date_from.isoformat(),
                        "date_to": scope_end.isoformat(),
                    }
                    if prior_scope != expected_scope:
                        raise ValueError("reviewed fingerprint was applied for a different scope")
                    if plan["blockers"] or int(plan["target_week_count"]) != 0:
                        raise ValueError(
                            "previously applied Finance projection no longer reconciles; new dry-run is required"
                        )
                    prior_result = json.loads(str(prior_apply["result_json"] or "{}"))
                    conn.rollback()
                    return {
                        "status": "already_current",
                        "runtime_mutation": False,
                        "fingerprint": expected_fingerprint,
                        "retro_cost_rows_inserted": 0,
                        "recalculated_week_count": 0,
                        "weeks": [],
                        "target_before_digest": prior_result.get("target_after_digest", ""),
                        "target_after_digest": prior_result.get("target_after_digest", ""),
                        "non_target_digest_before": plan["non_target_digest"],
                        "non_target_digest_after": plan["non_target_digest"],
                        "non_target_preserved": True,
                        "post_apply_target_week_count": 0,
                        "audit_id": str(prior_apply["audit_id"]),
                        "approval_reference": str(
                            prior_result.get("approval_reference") or approval_reference
                        ),
                    }
                if str(plan["fingerprint"]) != expected_fingerprint:
                    raise ValueError(
                        "business-approved Finance plan fingerprint changed before apply"
                    )
                if not bool(plan["apply_allowed"]):
                    raise ValueError("business-approved Finance plan contains blockers")
                now = self.now_factory().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                for row in plan["source_manifests"]["cost"]["rows"]:
                    conn.execute(
                        """INSERT INTO wb_finance_retro_cost_map(
                           seller_id,nm_id,unit_cost_rub,source_date,source_table,
                           source_row_json,source_row_sha256,source_calculation_fingerprint,
                           selection_method,formula_version,status,created_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            row["seller_id"],
                            row["nm_id"],
                            row["unit_cost_rub"],
                            row["source_date"],
                            row["source_table"],
                            row["source_row_json"],
                            row["source_row_sha256"],
                            row["source_calculation_fingerprint"],
                            row["selection_method"],
                            row["formula_version"],
                            row["status"],
                            now,
                        ),
                    )
                target_keys = {
                    (self.seller_id, item["week_start"], item["week_end"])
                    for item in plan["weeks"]
                }
                recalculated: list[dict[str, Any]] = []
                for item in plan["weeks"]:
                    start = date.fromisoformat(item["week_start"])
                    end = date.fromisoformat(item["week_end"])
                    metrics = self._recalculate_week_in_connection(conn, start, end)
                    recalculated.append(
                        {
                            "week_start": item["week_start"],
                            "week_end": item["week_end"],
                            "cogs": metrics["cogs"],
                            "profit_after_cogs": metrics["profit_after_cogs"],
                            "final_margin_pct": metrics["final_margin_pct"],
                        }
                    )
                non_target_after = self._finance_state_digest(
                    conn,
                    target_keys=target_keys,
                    target_only=False,
                )
                if non_target_after != plan["non_target_digest"]:
                    raise ValueError("non-target Finance state changed during backfill")
                readback = self._plan_business_approved_backfill_in_connection(
                    conn,
                    date_from=date_from,
                    date_to=scope_end,
                )
                if int(readback["target_week_count"]) != 0:
                    raise ValueError("post-apply Finance readback still contains changed weeks")
                if readback["blockers"]:
                    raise ValueError("post-apply Finance readback contains blockers")
                target_after_digest = self._finance_state_digest(
                    conn,
                    target_keys=target_keys,
                    target_only=True,
                )
                audit_result = {
                    "recalculated": recalculated,
                    "target_before_digest": plan["target_before_digest"],
                    "target_after_digest": target_after_digest,
                    "non_target_digest": non_target_after,
                    "post_apply_target_week_count": 0,
                    "approval_reference": approval_reference,
                }
                audit_id = hashlib.sha256(
                    f"{self.seller_id}|{expected_fingerprint}|{now}".encode("utf-8")
                ).hexdigest()
                conn.execute(
                    """INSERT INTO wb_finance_projection_audit(
                       audit_id,seller_id,action,fingerprint,scope_json,result_json,created_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        audit_id,
                        self.seller_id,
                        "apply_business_approved_backfill",
                        expected_fingerprint,
                        json.dumps(
                            {"date_from": date_from.isoformat(), "date_to": scope_end.isoformat()},
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        json.dumps(audit_result, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                        now,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            "status": "already_current" if not recalculated else "applied",
            "runtime_mutation": bool(recalculated),
            "fingerprint": expected_fingerprint,
            "retro_cost_rows_inserted": len(plan["source_manifests"]["cost"]["rows"]),
            "recalculated_week_count": len(recalculated),
            "weeks": recalculated,
            "target_before_digest": plan["target_before_digest"],
            "target_after_digest": target_after_digest,
            "non_target_digest_before": plan["non_target_digest"],
            "non_target_digest_after": non_target_after,
            "non_target_preserved": True,
            "post_apply_target_week_count": 0,
            "audit_id": audit_id,
            "approval_reference": approval_reference,
        }

    def business_approved_fingerprint_applied(
        self,
        *,
        fingerprint: str,
        date_from: date,
        date_to: date,
    ) -> bool:
        """Return whether the exact reviewed fingerprint already passed this scope."""

        self.ensure_schema()
        expected_scope = {
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
        }
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT scope_json FROM wb_finance_projection_audit
                   WHERE seller_id=? AND action='apply_business_approved_backfill'
                     AND fingerprint=? ORDER BY created_at DESC""",
                (self.seller_id, fingerprint),
            ).fetchall()
        return any(
            json.loads(str(row["scope_json"] or "{}")) == expected_scope for row in rows
        )

    def recalculate_stale_cost_weeks(
        self, *, date_from: date = date.min
    ) -> dict[str, Any]:
        """Atomically rebuild loaded weeks whose canonical derived state changed."""
        self.ensure_schema()
        plan = self.plan_stale_cost_weeks(date_from=date_from)
        return self.apply_stale_cost_weeks(
            expected_fingerprint=str(plan["fingerprint"]),
            date_from=date_from,
        )

    def plan_stale_cost_weeks(
        self,
        *,
        date_from: date = date.min,
        date_to: date | None = None,
    ) -> dict[str, Any]:
        """Build a read-only, fingerprinted plan for stale derived Finance weeks."""
        if not self.db_path.is_file():
            raise ValueError(f"Finance runtime SQLite does not exist: {self.db_path}")
        if date_to is not None and date_to < date_from:
            raise ValueError("date_to must not be earlier than date_from")
        with self._connect() as conn:
            conn.execute("BEGIN")
            try:
                return self._plan_stale_cost_weeks_in_connection(
                    conn, date_from=date_from, date_to=date_to
                )
            finally:
                conn.rollback()

    def _plan_stale_cost_weeks_in_connection(
        self,
        conn: sqlite3.Connection,
        *,
        date_from: date,
        date_to: date | None,
    ) -> dict[str, Any]:
        candidates = conn.execute(
            """SELECT DISTINCT raw.week_start,raw.week_end,
                       COALESCE(coverage.cost_state_hash,'') AS stored_cost_state_hash,
                       COALESCE(aggregate.classifier_version,'') AS stored_classifier_version,
                       COALESCE(aggregate.metrics_json,'') AS stored_metrics_json
                FROM wb_finance_weekly_raw_rows AS raw
                LEFT JOIN wb_finance_weekly_cost_coverage AS coverage
                 ON coverage.seller_id=raw.seller_id
                 AND coverage.week_start=raw.week_start
                 AND coverage.week_end=raw.week_end
                LEFT JOIN wb_finance_weekly_aggregates AS aggregate
                  ON aggregate.seller_id=raw.seller_id
                 AND aggregate.week_start=raw.week_start
                 AND aggregate.week_end=raw.week_end
                WHERE raw.seller_id=? AND raw.week_end>=?
                  AND (? IS NULL OR raw.week_start<=?)
                ORDER BY raw.week_start""",
            (
                self.seller_id,
                date_from.isoformat(),
                date_to.isoformat() if date_to is not None else None,
                date_to.isoformat() if date_to is not None else None,
            ),
        ).fetchall()
        stale: list[dict[str, Any]] = []
        for candidate in candidates:
            start = date.fromisoformat(candidate["week_start"])
            end = date.fromisoformat(candidate["week_end"])
            raw_rows = conn.execute(
                """SELECT report_id,rrd_id,row_hash,raw_json
                    FROM wb_finance_weekly_raw_rows
                    WHERE seller_id=? AND week_start=? AND week_end=?
                    ORDER BY report_id,rrd_id""",
                (self.seller_id, start.isoformat(), end.isoformat()),
            ).fetchall()
            rows = [json.loads(row["raw_json"]) for row in raw_rows]
            aggregate, current, unknown = self._aggregate_rows(conn, rows, start)
            try:
                stored_metrics = json.loads(str(candidate["stored_metrics_json"] or "{}"))
            except json.JSONDecodeError:
                stored_metrics = {}
            stored_metrics_digest = self._json_digest(stored_metrics)
            expected_metrics_digest = self._json_digest(aggregate)
            if (
                current["cost_state_hash"] == candidate["stored_cost_state_hash"]
                and str(candidate["stored_classifier_version"]) == CLASSIFIER_VERSION
                and stored_metrics_digest == expected_metrics_digest
            ):
                continue
            raw_digest = hashlib.sha256(
                json.dumps(
                    [
                        [
                            row["report_id"],
                            row["rrd_id"],
                            row["row_hash"],
                            hashlib.sha256(
                                str(row["raw_json"]).encode("utf-8")
                            ).hexdigest(),
                        ]
                        for row in raw_rows
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            report_rows = conn.execute(
                """SELECT report_id,report_type,content_hash,row_count
                FROM wb_finance_weekly_reports
                WHERE seller_id=? AND week_start=? AND week_end=?
                ORDER BY report_id""",
                (self.seller_id, start.isoformat(), end.isoformat()),
            ).fetchall()
            report_digest = hashlib.sha256(
                json.dumps(
                    [list(row) for row in report_rows],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            stale.append(
                {
                    "week_start": start.isoformat(),
                    "week_end": end.isoformat(),
                    "stored_cost_state_hash": candidate["stored_cost_state_hash"],
                    "expected_cost_state_hash": current["cost_state_hash"],
                    "stored_classifier_version": str(candidate["stored_classifier_version"]),
                    "expected_classifier_version": CLASSIFIER_VERSION,
                    "stored_metrics_digest": stored_metrics_digest,
                    "expected_metrics_digest": expected_metrics_digest,
                    "raw_digest": f"sha256:{raw_digest}",
                    "raw_row_count": len(raw_rows),
                    "report_digest": f"sha256:{report_digest}",
                    "report_count": len(report_rows),
                    "expected": {
                        "cogs": aggregate["cogs"],
                        "profit_after_cogs": aggregate["profit_after_cogs"],
                        "final_margin_pct": aggregate["final_margin_pct"],
                        "matched_units": current["matched_units"],
                        "unmatched_units": current["unmatched_units"],
                        "problem_skus": current["problem_skus"],
                        "quality": current["quality"],
                        "unknown_reasons": unknown,
                    },
                }
            )
        target_keys = {
            (self.seller_id, str(item["week_start"]), str(item["week_end"]))
            for item in stale
        }
        plan: dict[str, Any] = {
            "schema_version": "wb_finance_stale_cost_recalculation_v1",
            "status": "dry_run",
            "runtime_mutation": False,
            "apply_allowed": True,
            "blockers": [],
            "seller_id": self.seller_id,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat() if date_to is not None else None,
            "checked_week_count": len(candidates),
            "stale_week_count": len(stale),
            "weeks": stale,
            "target_before_digest": self._finance_state_digest(
                conn, target_keys=target_keys, target_only=True
            ),
            "non_target_digest": self._finance_state_digest(
                conn, target_keys=target_keys, target_only=False
            ),
        }
        plan["fingerprint"] = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    plan,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )
        return plan

    def apply_stale_cost_weeks(
        self,
        *,
        expected_fingerprint: str,
        date_from: date = date.min,
        date_to: date | None = None,
    ) -> dict[str, Any]:
        """Apply an exact stale-cost plan in one optimistic SQLite transaction."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                plan = self._plan_stale_cost_weeks_in_connection(
                    conn, date_from=date_from, date_to=date_to
                )
                if str(plan["fingerprint"]) != expected_fingerprint:
                    raise ValueError(
                        "stale Finance cost plan fingerprint changed before apply"
                    )
                target_keys = {
                    (
                        self.seller_id,
                        str(item["week_start"]),
                        str(item["week_end"]),
                    )
                    for item in plan["weeks"]
                }
                recalculated: list[dict[str, Any]] = []
                for item in plan["weeks"]:
                    start = date.fromisoformat(str(item["week_start"]))
                    end = date.fromisoformat(str(item["week_end"]))
                    metrics = self._recalculate_week_in_connection(conn, start, end)
                    recalculated.append(
                        {
                            "week_start": start.isoformat(),
                            "week_end": end.isoformat(),
                            "cost_state_hash": item["expected_cost_state_hash"],
                            "cogs": metrics["cogs"],
                        }
                    )
                non_target_after = self._finance_state_digest(
                    conn, target_keys=target_keys, target_only=False
                )
                if non_target_after != plan["non_target_digest"]:
                    raise ValueError(
                        "non-target Finance state changed during recalculation"
                    )
                verification = self._plan_stale_cost_weeks_in_connection(
                    conn, date_from=date_from, date_to=date_to
                )
                if int(verification["stale_week_count"]) != 0:
                    raise ValueError(
                        "post-recalculation verification still contains stale weeks"
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            "status": "already_current" if not recalculated else "applied",
            "runtime_mutation": bool(recalculated),
            "fingerprint": expected_fingerprint,
            "checked_week_count": plan["checked_week_count"],
            "recalculated_week_count": len(recalculated),
            "weeks": recalculated,
            "non_target_digest_before": plan["non_target_digest"],
            "non_target_digest_after": non_target_after,
            "non_target_preserved": True,
            "post_verify_stale_week_count": 0,
        }

    def _finance_state_digest(
        self,
        conn: sqlite3.Connection,
        *,
        target_keys: set[tuple[str, str, str]],
        target_only: bool,
    ) -> str:
        evidence: list[list[Any]] = []
        for table in (
            "wb_finance_weekly_aggregates",
            "wb_finance_weekly_cost_coverage",
            "wb_finance_weekly_reconciliation",
            "wb_finance_weekly_sku_aggregates",
            "wb_finance_weekly_sync",
        ):
            columns = [
                str(row["name"])
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            ]
            order_columns = ["seller_id", "week_start", "week_end"]
            order_columns.extend(
                name for name in ("nm_id", "formula_version") if name in columns
            )
            rows = conn.execute(
                f"SELECT * FROM {table} ORDER BY {','.join(order_columns)}"
            ).fetchall()
            for row in rows:
                key = (
                    str(row["seller_id"]),
                    str(row["week_start"]),
                    str(row["week_end"]),
                )
                if (key in target_keys) != target_only:
                    continue
                evidence.append([table, *[row[column] for column in columns]])
        return (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    evidence,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )

    def repair_orphan_derived_rows(self) -> dict[str, Any]:
        """Remove derived rows that have no matching seller/week sync boundary."""
        self.ensure_schema()
        tables = (
            "wb_finance_weekly_aggregates",
            "wb_finance_weekly_cost_coverage",
            "wb_finance_weekly_reconciliation",
            "wb_finance_weekly_sku_aggregates",
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
