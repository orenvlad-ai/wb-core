"""Default-off official FBS order collection and query-only shadow reads.

Stage 5 persists a privacy-minimized official observation only.  It never
calls a WB mutation or status POST, assigns an FF facility, or creates an
inventory document, operation, reservation, movement, balance, or cutover.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Mapping, Protocol
from uuid import uuid4

from packages.adapters.wb_fbs_orders import (
    HttpBackedWbFbsOrdersSource,
    WbFbsOrdersHttpStatusError,
    WbFbsOrdersPage,
    WbFbsOrdersTransportError,
)


CONTRACT_NAME = "wb_fbs_orders_readonly_shadow_v1"
CONTRACT_VERSION = 1
COLLECTOR_ENABLED_ENV = "WB_FBS_COLLECTOR_ENABLED"
OBSERVATIONS_TABLE = "sheet_vitrina_v1_wb_supplies_fbs_order_observations"
STATE_TABLE = "sheet_vitrina_v1_wb_supplies_fbs_collector_state"
DEFAULT_LOOKBACK_SECONDS = 24 * 60 * 60
DEFAULT_MAX_PAGES = 10
MAX_PAGES = 50
MAX_PAGE_SIZE = 100
MAX_HISTORY_SIZE = 50
SAFE_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
REQUIRED_TABLES = frozenset({OBSERVATIONS_TABLE, STATE_TABLE})


class WbFbsOrdersError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Any = None,
        http_status: int = 422,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details = details
        self.http_status = int(http_status)


class WbFbsOrdersSource(Protocol):
    def list_orders(
        self,
        *,
        limit: int,
        next_cursor: int,
        date_from: int | None,
        date_to: int | None,
    ) -> WbFbsOrdersPage:
        raise NotImplementedError


def ensure_wb_fbs_orders_schema(conn: sqlite3.Connection) -> None:
    """Create empty additive Stage 5 observation/state schema."""

    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {OBSERVATIONS_TABLE}(
            observation_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            observation_id TEXT NOT NULL UNIQUE,
            order_id INTEGER NOT NULL CHECK(order_id > 0),
            source_revision TEXT NOT NULL,
            supply_id TEXT NOT NULL DEFAULT '',
            delivery_type TEXT NOT NULL CHECK(delivery_type='fbs'),
            source_created_at TEXT NOT NULL DEFAULT '',
            warehouse_id INTEGER,
            office_id INTEGER,
            nm_id INTEGER NOT NULL CHECK(nm_id > 0),
            chrt_id INTEGER,
            skus_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(skus_json)),
            cargo_type INTEGER,
            cross_border_type INTEGER,
            is_zero_order INTEGER NOT NULL DEFAULT 0 CHECK(is_zero_order IN (0,1)),
            observed_at TEXT NOT NULL
                CHECK(substr(observed_at,-1,1)='Z' AND julianday(observed_at) IS NOT NULL),
            collector_date_from INTEGER NOT NULL CHECK(collector_date_from > 0),
            collector_date_to INTEGER NOT NULL CHECK(collector_date_to >= collector_date_from),
            collector_cursor INTEGER NOT NULL CHECK(collector_cursor >= 0),
            UNIQUE(order_id,source_revision),
            CHECK(length(observation_id) BETWEEN 8 AND 120),
            CHECK(length(source_revision) BETWEEN 8 AND 120),
            CHECK(length(supply_id) <= 160),
            CHECK(length(source_created_at) <= 64)
        );
        CREATE INDEX IF NOT EXISTS wb_fbs_observations_by_order
        ON {OBSERVATIONS_TABLE}(order_id,observation_sequence DESC);
        CREATE INDEX IF NOT EXISTS wb_fbs_observations_by_supply
        ON {OBSERVATIONS_TABLE}(supply_id,observation_sequence DESC)
        WHERE supply_id<>'';
        CREATE INDEX IF NOT EXISTS wb_fbs_observations_by_nm
        ON {OBSERVATIONS_TABLE}(nm_id,observation_sequence DESC);
        CREATE TRIGGER IF NOT EXISTS wb_fbs_observations_no_update
        BEFORE UPDATE ON {OBSERVATIONS_TABLE}
        BEGIN
            SELECT RAISE(ABORT,'WB FBS order observation is immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS wb_fbs_observations_no_delete
        BEFORE DELETE ON {OBSERVATIONS_TABLE}
        BEGIN
            SELECT RAISE(ABORT,'WB FBS order observations are append-only');
        END;

        CREATE TABLE IF NOT EXISTS {STATE_TABLE}(
            state_id INTEGER PRIMARY KEY CHECK(state_id=1),
            last_run_id TEXT NOT NULL,
            last_status TEXT NOT NULL,
            last_attempt_at TEXT NOT NULL,
            last_success_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            window_date_from INTEGER NOT NULL,
            window_date_to INTEGER NOT NULL,
            next_cursor INTEGER NOT NULL DEFAULT 0,
            page_count INTEGER NOT NULL DEFAULT 0,
            raw_order_count INTEGER NOT NULL DEFAULT 0,
            accepted_order_count INTEGER NOT NULL DEFAULT 0,
            new_observation_count INTEGER NOT NULL DEFAULT 0,
            ignored_order_count INTEGER NOT NULL DEFAULT 0,
            complete INTEGER NOT NULL DEFAULT 0 CHECK(complete IN (0,1)),
            CHECK(length(last_run_id) BETWEEN 1 AND 120),
            CHECK(length(last_status) BETWEEN 1 AND 40),
            CHECK(length(last_error) <= 1000),
            CHECK(window_date_from > 0),
            CHECK(window_date_to >= window_date_from),
            CHECK(next_cursor >= 0)
        );
        """
    )


class WbFbsOrdersCollector:
    """Bounded collector plus cache-only read model for official FBS orders."""

    def __init__(
        self,
        *,
        db_path: Path,
        timestamp_factory: Any,
        source: WbFbsOrdersSource | None = None,
        enabled: bool | None = None,
        unix_time_factory: Any | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.timestamp_factory = timestamp_factory
        self.source = source or HttpBackedWbFbsOrdersSource()
        self.enabled = _env_enabled(COLLECTOR_ENABLED_ENV) if enabled is None else bool(enabled)
        self.unix_time_factory = unix_time_factory or time.time

    def collect_default_window(self) -> dict[str, Any]:
        return self.collect()

    def collect(
        self,
        *,
        date_from: int | None = None,
        date_to: int | None = None,
        next_cursor: int = 0,
        page_limit: int = 1000,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> dict[str, Any]:
        if not self.enabled:
            return {
                "contract_name": CONTRACT_NAME,
                "contract_version": CONTRACT_VERSION,
                "status": "disabled",
                "reason": "collector_default_off",
                "upstream_requests": 0,
                "writes": 0,
                "mutates_wb": False,
            }
        window_to = _bounded_int(
            int(self.unix_time_factory()) if date_to is None else date_to,
            "date_to",
            minimum=1,
            maximum=2**63 - 1,
        )
        window_from = _bounded_int(
            window_to - DEFAULT_LOOKBACK_SECONDS if date_from is None else date_from,
            "date_from",
            minimum=1,
            maximum=window_to,
        )
        if window_to - window_from > 30 * 24 * 60 * 60:
            raise WbFbsOrdersError("window_too_wide", "FBS collector window must not exceed 30 days")
        cursor = _bounded_int(next_cursor, "next_cursor", minimum=0, maximum=2**63 - 1)
        limit = _bounded_int(page_limit, "page_limit", minimum=1, maximum=1000)
        pages_bound = _bounded_int(max_pages, "max_pages", minimum=1, maximum=MAX_PAGES)
        run_id = "fbs_run_" + uuid4().hex
        attempted_at = str(self.timestamp_factory())
        pages: list[tuple[int, WbFbsOrdersPage]] = []
        seen_cursors = {cursor}
        complete = False
        try:
            for _ in range(pages_bound):
                page_cursor = cursor
                page = self.source.list_orders(
                    limit=limit,
                    next_cursor=page_cursor,
                    date_from=window_from,
                    date_to=window_to,
                )
                pages.append((page_cursor, page))
                if page.next_cursor == 0:
                    cursor = 0
                    complete = True
                    break
                if page.next_cursor in seen_cursors:
                    raise WbFbsOrdersError(
                        "cursor_did_not_advance",
                        "Official FBS pagination cursor did not advance",
                        http_status=502,
                    )
                cursor = int(page.next_cursor)
                seen_cursors.add(cursor)
            normalized: list[dict[str, Any]] = []
            ignored = 0
            raw_count = 0
            seen_observations: set[tuple[int, str]] = set()
            for page_cursor, page in pages:
                raw_count += len(page.orders)
                for raw in page.orders:
                    row = _normalize_order(
                        raw,
                        observed_at=attempted_at,
                        date_from=window_from,
                        date_to=window_to,
                        cursor=page_cursor,
                    )
                    if row is None:
                        ignored += 1
                        continue
                    identity = (int(row["order_id"]), str(row["source_revision"]))
                    if identity in seen_observations:
                        continue
                    seen_observations.add(identity)
                    normalized.append(row)
            new_count = self._persist_success(
                run_id=run_id,
                attempted_at=attempted_at,
                window_from=window_from,
                window_to=window_to,
                next_cursor=cursor,
                pages=len(pages),
                raw_count=raw_count,
                accepted_count=len(normalized),
                ignored_count=ignored,
                complete=complete,
                observations=normalized,
            )
        except Exception as exc:
            self._persist_failure(
                run_id=run_id,
                attempted_at=attempted_at,
                window_from=window_from,
                window_to=window_to,
                next_cursor=cursor,
                pages=len(pages),
                error=_safe_error(exc),
            )
            if isinstance(exc, WbFbsOrdersError):
                raise
            if isinstance(exc, (WbFbsOrdersHttpStatusError, WbFbsOrdersTransportError)):
                raise WbFbsOrdersError(
                    "official_fbs_read_failed", _safe_error(exc), http_status=502
                ) from exc
            raise
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "success" if complete else "bounded_partial",
            "run_id": run_id,
            "window": {"date_from": window_from, "date_to": window_to},
            "page_count": len(pages),
            "raw_order_count": raw_count,
            "accepted_order_count": len(normalized),
            "ignored_order_count": ignored,
            "new_observation_count": new_count,
            "complete": complete,
            "next_cursor": cursor,
            "upstream_method": "GET",
            "mutates_wb": False,
            "creates_inventory_movement": False,
        }

    def orders_page(
        self,
        *,
        page: int = 1,
        limit: int = 25,
        search: str = "",
        nm_id: int | str | None = None,
        supply_id: str = "",
    ) -> dict[str, Any]:
        page_number = _bounded_int(page, "page", minimum=1, maximum=1_000_000)
        page_size = _bounded_int(limit, "limit", minimum=1, maximum=MAX_PAGE_SIZE)
        search_value = _safe_search(search)
        nm_value = _optional_positive_int(nm_id, "nm_id")
        supply_value = _optional_identifier(supply_id, "supply_id")
        with _connect_readonly(self.db_path) as conn:
            schema = _schema(conn)
            if not schema["available"]:
                return _with_etag(self._schema_absent(schema["missing_tables"]))
            where, params = _current_filters(
                search=search_value, nm_id=nm_value, supply_id=supply_value
            )
            current_cte = _current_cte()
            total = int(
                conn.execute(
                    current_cte + f" SELECT COUNT(*) FROM current_order WHERE {where}",
                    params,
                ).fetchone()[0]
            )
            offset = (page_number - 1) * page_size
            rows = conn.execute(
                current_cte
                + f""" SELECT * FROM current_order WHERE {where}
                         ORDER BY source_created_at DESC,order_id DESC
                         LIMIT ? OFFSET ?""",
                (*params, page_size, offset),
            ).fetchall()
            state = _state(conn)
        payload = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "ready",
            "collector": self._collector_status(state),
            "policy": _policy(),
            "filters": {"search": search_value, "nm_id": nm_value, "supply_id": supply_value},
            "page": _page(page_number, page_size, total),
            "rows": [_public_order(row) for row in rows],
        }
        return _with_etag(payload)

    def order_detail(self, order_id: int | str, *, history_limit: int = 20) -> dict[str, Any]:
        order_value = _bounded_int(order_id, "order_id", minimum=1, maximum=2**63 - 1)
        limit = _bounded_int(
            history_limit, "history_limit", minimum=1, maximum=MAX_HISTORY_SIZE
        )
        with _connect_readonly(self.db_path) as conn:
            schema = _schema(conn)
            if not schema["available"]:
                return _with_etag(self._schema_absent(schema["missing_tables"]))
            rows = conn.execute(
                f"""SELECT * FROM {OBSERVATIONS_TABLE}
                    WHERE order_id=? ORDER BY observation_sequence DESC LIMIT ?""",
                (order_value, limit),
            ).fetchall()
            state = _state(conn)
        if not rows:
            raise WbFbsOrdersError(
                "fbs_order_not_found", "Official FBS order was not found in the cache", http_status=404
            )
        return _with_etag(
            {
                "contract_name": CONTRACT_NAME,
                "contract_version": CONTRACT_VERSION,
                "status": "ready",
                "collector": self._collector_status(state),
                "policy": _policy(),
                "current": _public_order(rows[0]),
                "history": [_public_order(row) for row in rows],
            }
        )

    def _persist_success(
        self,
        *,
        run_id: str,
        attempted_at: str,
        window_from: int,
        window_to: int,
        next_cursor: int,
        pages: int,
        raw_count: int,
        accepted_count: int,
        ignored_count: int,
        complete: bool,
        observations: list[Mapping[str, Any]],
    ) -> int:
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            ensure_wb_fbs_orders_schema(conn)
            before = conn.total_changes
            conn.executemany(
                f"""INSERT OR IGNORE INTO {OBSERVATIONS_TABLE}(
                       observation_id,order_id,source_revision,supply_id,delivery_type,
                       source_created_at,warehouse_id,office_id,nm_id,chrt_id,skus_json,
                       cargo_type,cross_border_type,is_zero_order,observed_at,
                       collector_date_from,collector_date_to,collector_cursor
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        row["observation_id"], row["order_id"], row["source_revision"],
                        row["supply_id"], row["delivery_type"], row["source_created_at"],
                        row["warehouse_id"], row["office_id"], row["nm_id"], row["chrt_id"],
                        row["skus_json"], row["cargo_type"], row["cross_border_type"],
                        int(row["is_zero_order"]), row["observed_at"], row["collector_date_from"],
                        row["collector_date_to"], row["collector_cursor"],
                    )
                    for row in observations
                ],
            )
            new_count = conn.total_changes - before
            _upsert_state(
                conn,
                run_id=run_id,
                status="success" if complete else "bounded_partial",
                attempted_at=attempted_at,
                success_at=attempted_at,
                error="",
                window_from=window_from,
                window_to=window_to,
                next_cursor=next_cursor,
                pages=pages,
                raw_count=raw_count,
                accepted_count=accepted_count,
                new_count=new_count,
                ignored_count=ignored_count,
                complete=complete,
            )
            conn.commit()
        return int(new_count)

    def _persist_failure(
        self,
        *,
        run_id: str,
        attempted_at: str,
        window_from: int,
        window_to: int,
        next_cursor: int,
        pages: int,
        error: str,
    ) -> None:
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            ensure_wb_fbs_orders_schema(conn)
            _upsert_state(
                conn,
                run_id=run_id,
                status="failed",
                attempted_at=attempted_at,
                success_at="",
                error=error,
                window_from=window_from,
                window_to=window_to,
                next_cursor=next_cursor,
                pages=pages,
                raw_count=0,
                accepted_count=0,
                new_count=0,
                ignored_count=0,
                complete=False,
            )
            conn.commit()

    def _schema_absent(self, missing_tables: list[str]) -> dict[str, Any]:
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "schema_absent",
            "collector": self._collector_status({}),
            "policy": _policy(),
            "page": _page(1, 25, 0),
            "rows": [],
            "missing_tables": missing_tables,
        }

    def _collector_status(self, state: Mapping[str, Any]) -> dict[str, Any]:
        configured = bool(self.enabled)
        return {
            "configured": configured,
            "effective": configured,
            "reason": "collector_enabled" if configured else "collector_default_off",
            "upstream": "WB Marketplace API / FBS Orders",
            "upstream_method": "GET",
            "uses_status_post": False,
            "last_run": dict(state),
        }


def _normalize_order(
    raw: Mapping[str, Any],
    *,
    observed_at: str,
    date_from: int,
    date_to: int,
    cursor: int,
) -> dict[str, Any] | None:
    delivery_type = str(raw.get("deliveryType") or raw.get("delivery_type") or "").strip().casefold()
    if delivery_type != "fbs":
        return None
    order_id = _safe_positive_int(raw.get("id"))
    nm_id = _safe_positive_int(raw.get("nmId") or raw.get("nm_id"))
    if order_id is None or nm_id is None:
        return None
    supply_id = _safe_optional_identifier(raw.get("supplyId") or raw.get("supplyID"))
    source_created_at = _safe_text(raw.get("createdAt"), maximum=64)
    skus = []
    for value in raw.get("skus") or []:
        text = _safe_text(value, maximum=80)
        if text and text not in skus:
            skus.append(text)
        if len(skus) >= 20:
            break
    safe = {
        "order_id": order_id,
        "supply_id": supply_id,
        "delivery_type": "fbs",
        "source_created_at": source_created_at,
        "warehouse_id": _safe_nonnegative_int(raw.get("warehouseId")),
        "office_id": _safe_nonnegative_int(raw.get("officeId")),
        "nm_id": nm_id,
        "chrt_id": _safe_nonnegative_int(raw.get("chrtId")),
        "skus": skus,
        "cargo_type": _safe_nonnegative_int(raw.get("cargoType")),
        "cross_border_type": _safe_nonnegative_int(raw.get("crossBorderType")),
        "is_zero_order": bool(raw.get("isZeroOrder") is True),
    }
    revision = _fingerprint(safe)
    observation_id = "fbs_obs_" + hashlib.sha256(
        f"{order_id}:{revision}".encode("utf-8")
    ).hexdigest()[:32]
    return {
        **safe,
        "source_revision": revision,
        "observation_id": observation_id,
        "skus_json": json.dumps(skus, ensure_ascii=False, separators=(",", ":")),
        "observed_at": observed_at,
        "collector_date_from": date_from,
        "collector_date_to": date_to,
        "collector_cursor": cursor,
    }


def _upsert_state(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    status: str,
    attempted_at: str,
    success_at: str,
    error: str,
    window_from: int,
    window_to: int,
    next_cursor: int,
    pages: int,
    raw_count: int,
    accepted_count: int,
    new_count: int,
    ignored_count: int,
    complete: bool,
) -> None:
    prior = conn.execute(
        f"SELECT last_success_at FROM {STATE_TABLE} WHERE state_id=1"
    ).fetchone()
    retained_success_at = str(success_at or (prior[0] if prior else "") or "")
    conn.execute(
        f"""INSERT INTO {STATE_TABLE}(
               state_id,last_run_id,last_status,last_attempt_at,last_success_at,last_error,
               window_date_from,window_date_to,next_cursor,page_count,raw_order_count,
               accepted_order_count,new_observation_count,ignored_order_count,complete
           ) VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(state_id) DO UPDATE SET
               last_run_id=excluded.last_run_id,last_status=excluded.last_status,
               last_attempt_at=excluded.last_attempt_at,last_success_at=excluded.last_success_at,
               last_error=excluded.last_error,window_date_from=excluded.window_date_from,
               window_date_to=excluded.window_date_to,next_cursor=excluded.next_cursor,
               page_count=excluded.page_count,raw_order_count=excluded.raw_order_count,
               accepted_order_count=excluded.accepted_order_count,
               new_observation_count=excluded.new_observation_count,
               ignored_order_count=excluded.ignored_order_count,complete=excluded.complete""",
        (
            run_id, status, attempted_at, retained_success_at, str(error or "")[:1000],
            window_from, window_to, next_cursor, pages, raw_count, accepted_count,
            new_count, ignored_count, int(bool(complete)),
        ),
    )


def _current_cte() -> str:
    return f"""WITH ranked AS (
        SELECT observation.*,
               ROW_NUMBER() OVER(
                   PARTITION BY order_id ORDER BY observation_sequence DESC
               ) AS current_rank
        FROM {OBSERVATIONS_TABLE} AS observation
    ), current_order AS (SELECT * FROM ranked WHERE current_rank=1)"""


def _current_filters(
    *, search: str, nm_id: int | None, supply_id: str
) -> tuple[str, tuple[Any, ...]]:
    clauses = ["1=1"]
    params: list[Any] = []
    if search:
        clauses.append("(CAST(order_id AS TEXT) LIKE ? OR supply_id LIKE ? OR CAST(nm_id AS TEXT) LIKE ?)")
        term = f"%{search}%"
        params.extend([term, term, term])
    if nm_id is not None:
        clauses.append("nm_id=?")
        params.append(nm_id)
    if supply_id:
        clauses.append("supply_id=?")
        params.append(supply_id)
    return " AND ".join(clauses), tuple(params)


def _public_order(row: sqlite3.Row) -> dict[str, Any]:
    try:
        skus = json.loads(str(row["skus_json"] or "[]"))
    except json.JSONDecodeError:
        skus = []
    return {
        "observation_id": str(row["observation_id"]),
        "order_id": int(row["order_id"]),
        "source_revision": str(row["source_revision"]),
        "supply_id": str(row["supply_id"] or ""),
        "delivery_type": "fbs",
        "source_created_at": str(row["source_created_at"] or ""),
        "warehouse_id": row["warehouse_id"],
        "office_id": row["office_id"],
        "nm_id": int(row["nm_id"]),
        "chrt_id": row["chrt_id"],
        "skus": skus if isinstance(skus, list) else [],
        "cargo_type": row["cargo_type"],
        "cross_border_type": row["cross_border_type"],
        "is_zero_order": bool(row["is_zero_order"]),
        "observed_at": str(row["observed_at"]),
    }


def _state(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(f"SELECT * FROM {STATE_TABLE} WHERE state_id=1").fetchone()
    if row is None:
        return {}
    return {
        "run_id": str(row["last_run_id"]),
        "status": str(row["last_status"]),
        "attempted_at": str(row["last_attempt_at"]),
        "success_at": str(row["last_success_at"]),
        "error": str(row["last_error"]),
        "window": {
            "date_from": int(row["window_date_from"]),
            "date_to": int(row["window_date_to"]),
        },
        "next_cursor": int(row["next_cursor"]),
        "page_count": int(row["page_count"]),
        "raw_order_count": int(row["raw_order_count"]),
        "accepted_order_count": int(row["accepted_order_count"]),
        "new_observation_count": int(row["new_observation_count"]),
        "ignored_order_count": int(row["ignored_order_count"]),
        "complete": bool(row["complete"]),
    }


def _schema(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    missing = sorted(REQUIRED_TABLES - tables)
    return {"available": not missing, "missing_tables": missing}


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    resolved = Path(db_path).resolve()
    if not resolved.is_file():
        raise WbFbsOrdersError(
            "runtime_store_missing", "Operational runtime store is missing", http_status=503
        )
    conn = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _policy() -> dict[str, Any]:
    return {
        "official_source": True,
        "upstream_get_only": True,
        "stores_customer_address": False,
        "stores_customer_comment": False,
        "append_only_observations": True,
        "assigns_ff_origin": False,
        "creates_document": False,
        "creates_operation": False,
        "creates_reservation": False,
        "creates_movement": False,
        "materializes_balance": False,
        "mutates_wb": False,
        "switches_writer_or_reader": False,
    }


def _page(page: int, limit: int, total: int) -> dict[str, Any]:
    offset = (page - 1) * limit
    return {
        "number": page,
        "limit": limit,
        "total": total,
        "has_previous": page > 1,
        "has_next": offset + limit < total,
    }


def _with_etag(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result["etag"] = '"' + _fingerprint(result) + '"'
    return result


def _fingerprint(value: Any) -> str:
    material = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _bounded_int(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise WbFbsOrdersError("invalid_integer", f"{name} must be an integer") from exc
    if result < minimum or result > maximum:
        raise WbFbsOrdersError(
            "integer_out_of_range", f"{name} must be between {minimum} and {maximum}"
        )
    return result


def _optional_positive_int(value: Any, name: str) -> int | None:
    return None if value in (None, "") else _bounded_int(value, name, minimum=1, maximum=2**63 - 1)


def _optional_identifier(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not SAFE_IDENTIFIER_RE.fullmatch(text):
        raise WbFbsOrdersError("invalid_identifier", f"{name} has invalid characters")
    return text


def _safe_search(value: Any) -> str:
    text = _safe_text(value, maximum=120)
    if "%" in text or "_" in text:
        raise WbFbsOrdersError("invalid_search", "search must not contain SQL wildcard characters")
    return text


def _safe_positive_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if 0 < result <= 2**63 - 1 else None


def _safe_nonnegative_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if 0 <= result <= 2**63 - 1 else None


def _safe_optional_identifier(value: Any) -> str:
    text = str(value or "").strip()
    return text if not text or SAFE_IDENTIFIER_RE.fullmatch(text) else ""


def _safe_text(value: Any, *, maximum: int) -> str:
    text = CONTROL_RE.sub("", str(value or "")).strip()
    return text[:maximum]


def _env_enabled(name: str) -> bool:
    return str(os.environ.get(name, "") or "").strip().casefold() in {"1", "true", "yes", "on"}


def _safe_error(exc: Exception) -> str:
    return CONTROL_RE.sub("", str(exc or "")).strip()[:1000]
