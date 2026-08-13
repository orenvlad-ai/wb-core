"""Default-off official FBS order collection and query-only shadow reads.

Stage 7A persists privacy-minimized official order and lifecycle observations.
The status POST is an official read semantic only.  It never assigns an FF facility or creates an
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
    WbFbsOrderStatus,
    WbFbsOrdersTransportError,
)


CONTRACT_NAME = "wb_fbs_orders_readonly_shadow_v1"
CONTRACT_VERSION = 2
COLLECTOR_ENABLED_ENV = "WB_FBS_COLLECTOR_ENABLED"
OBSERVATIONS_TABLE = "sheet_vitrina_v1_wb_supplies_fbs_order_observations"
STATE_TABLE = "sheet_vitrina_v1_wb_supplies_fbs_collector_state"
STATUS_OBSERVATIONS_TABLE = "sheet_vitrina_v1_wb_supplies_fbs_status_observations"
WAREHOUSE_MAPPINGS_TABLE = "sheet_vitrina_v1_wb_supplies_fbs_warehouse_facility_mappings"
IDENTITY_MAPPINGS_TABLE = "sheet_vitrina_v1_wb_supplies_fbs_identity_mappings"
IDENTITY_EVIDENCE_TABLE = "sheet_vitrina_v1_wb_supplies_fbs_identity_evidence"
BACKFILL_REVIEW_FROM = "2026-08-01"
DEFAULT_LOOKBACK_SECONDS = 24 * 60 * 60
DEFAULT_MAX_PAGES = 10
MAX_PAGES = 50
MAX_PAGE_SIZE = 100
MAX_HISTORY_SIZE = 50
SAFE_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
REQUIRED_TABLES = frozenset({
    OBSERVATIONS_TABLE, STATE_TABLE, STATUS_OBSERVATIONS_TABLE,
    WAREHOUSE_MAPPINGS_TABLE, IDENTITY_MAPPINGS_TABLE, IDENTITY_EVIDENCE_TABLE,
})


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
            seller_sku TEXT NOT NULL DEFAULT '',
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

        CREATE TABLE IF NOT EXISTS {STATUS_OBSERVATIONS_TABLE}(
            observation_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            observation_id TEXT NOT NULL UNIQUE,
            order_id INTEGER NOT NULL CHECK(order_id > 0),
            order_revision TEXT NOT NULL,
            status_digest TEXT NOT NULL,
            supplier_status TEXT NOT NULL DEFAULT '',
            wb_status TEXT NOT NULL DEFAULT '',
            positive_quantity INTEGER NOT NULL CHECK(positive_quantity > 0),
            observed_at TEXT NOT NULL CHECK(substr(observed_at,-1,1)='Z' AND julianday(observed_at) IS NOT NULL),
            UNIQUE(order_id,order_revision,status_digest)
        );
        CREATE INDEX IF NOT EXISTS wb_fbs_status_by_order
        ON {STATUS_OBSERVATIONS_TABLE}(order_id,observation_sequence DESC);
        CREATE TRIGGER IF NOT EXISTS wb_fbs_status_no_update BEFORE UPDATE ON {STATUS_OBSERVATIONS_TABLE}
        BEGIN SELECT RAISE(ABORT,'WB FBS status observation is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS wb_fbs_status_no_delete BEFORE DELETE ON {STATUS_OBSERVATIONS_TABLE}
        BEGIN SELECT RAISE(ABORT,'WB FBS status observations are append-only'); END;

        CREATE TABLE IF NOT EXISTS {WAREHOUSE_MAPPINGS_TABLE}(
            mapping_id TEXT PRIMARY KEY,
            seller_warehouse_id INTEGER NOT NULL CHECK(seller_warehouse_id > 0),
            facility_id TEXT NOT NULL,
            mapping_digest TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            UNIQUE(seller_warehouse_id,mapping_digest)
        );
        CREATE TRIGGER IF NOT EXISTS wb_fbs_warehouse_mapping_no_update BEFORE UPDATE ON {WAREHOUSE_MAPPINGS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS warehouse mapping is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS wb_fbs_warehouse_mapping_no_delete BEFORE DELETE ON {WAREHOUSE_MAPPINGS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS warehouse mappings are append-only'); END;

        CREATE TABLE IF NOT EXISTS {IDENTITY_MAPPINGS_TABLE}(
            mapping_id TEXT PRIMARY KEY,
            source_nm_id INTEGER NOT NULL CHECK(source_nm_id > 0),
            source_chrt_id INTEGER NOT NULL CHECK(source_chrt_id > 0),
            source_barcode TEXT NOT NULL,
            source_sku TEXT NOT NULL,
            target_nm_id INTEGER NOT NULL CHECK(target_nm_id > 0),
            mapping_digest TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            UNIQUE(source_nm_id,source_chrt_id,source_barcode,source_sku,mapping_digest)
        );
        CREATE TRIGGER IF NOT EXISTS wb_fbs_identity_mapping_no_update BEFORE UPDATE ON {IDENTITY_MAPPINGS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS identity mapping is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS wb_fbs_identity_mapping_no_delete BEFORE DELETE ON {IDENTITY_MAPPINGS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS identity mappings are append-only'); END;

        CREATE TABLE IF NOT EXISTS {IDENTITY_EVIDENCE_TABLE}(
            evidence_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            evidence_id TEXT NOT NULL UNIQUE,
            order_id INTEGER NOT NULL CHECK(order_id > 0),
            order_revision TEXT NOT NULL,
            warehouse_id INTEGER,
            nm_id INTEGER NOT NULL CHECK(nm_id > 0),
            chrt_id INTEGER,
            barcode TEXT NOT NULL DEFAULT '',
            seller_sku TEXT NOT NULL DEFAULT '',
            outcome TEXT NOT NULL CHECK(outcome IN ('matched','unmatched_warehouse','unmatched_identity','deferred')),
            warehouse_mapping_id TEXT NOT NULL DEFAULT '',
            identity_mapping_id TEXT NOT NULL DEFAULT '',
            evidence_digest TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            UNIQUE(order_id,order_revision,evidence_digest)
        );
        CREATE INDEX IF NOT EXISTS wb_fbs_identity_evidence_by_outcome
        ON {IDENTITY_EVIDENCE_TABLE}(outcome,evidence_sequence DESC);
        CREATE TRIGGER IF NOT EXISTS wb_fbs_identity_evidence_no_update BEFORE UPDATE ON {IDENTITY_EVIDENCE_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS identity evidence is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS wb_fbs_identity_evidence_no_delete BEFORE DELETE ON {IDENTITY_EVIDENCE_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS identity evidence is append-only'); END;

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
    observation_columns = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({OBSERVATIONS_TABLE})").fetchall()
    }
    if "seller_sku" not in observation_columns:
        conn.execute(f"ALTER TABLE {OBSERVATIONS_TABLE} ADD COLUMN seller_sku TEXT NOT NULL DEFAULT ''")


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
            status_observations: list[dict[str, Any]] = []
            status_reader = getattr(self.source, "list_statuses", None)
            if callable(status_reader) and normalized:
                by_order = {int(item["order_id"]): item for item in normalized}
                for offset in range(0, len(by_order), 1000):
                    batch_ids = sorted(by_order)[offset : offset + 1000]
                    for status in status_reader(batch_ids):
                        status_observations.append(
                            _normalize_status(
                                status,
                                order=by_order[int(status.order_id)],
                                observed_at=attempted_at,
                            )
                        )
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
                status_observations=status_observations,
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
            "upstream_methods": ["GET /api/v3/orders"]
            + (["POST /api/v3/orders/status (read semantic)"] if status_observations else []),
            "status_observation_count": len(status_observations),
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
            shadow = _shadow_state(conn, state)
        payload = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "ready",
            "collector": self._collector_status(state),
            "shadow": shadow,
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
            status_rows = conn.execute(
                f"SELECT * FROM {STATUS_OBSERVATIONS_TABLE} WHERE order_id=? "
                "ORDER BY observation_sequence DESC LIMIT ?",
                (order_value, limit),
            ).fetchall()
            evidence_rows = conn.execute(
                f"SELECT * FROM {IDENTITY_EVIDENCE_TABLE} WHERE order_id=? "
                "ORDER BY evidence_sequence DESC LIMIT ?",
                (order_value, limit),
            ).fetchall()
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
                "status_history": [dict(row) for row in status_rows],
                "mapping_evidence": [dict(row) for row in evidence_rows],
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
        status_observations: list[Mapping[str, Any]],
    ) -> int:
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            ensure_wb_fbs_orders_schema(conn)
            before = conn.total_changes
            conn.executemany(
                f"""INSERT OR IGNORE INTO {OBSERVATIONS_TABLE}(
                       observation_id,order_id,source_revision,supply_id,delivery_type,
                       source_created_at,warehouse_id,office_id,nm_id,chrt_id,seller_sku,skus_json,
                       cargo_type,cross_border_type,is_zero_order,observed_at,
                       collector_date_from,collector_date_to,collector_cursor
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        row["observation_id"], row["order_id"], row["source_revision"],
                        row["supply_id"], row["delivery_type"], row["source_created_at"],
                        row["warehouse_id"], row["office_id"], row["nm_id"], row["chrt_id"], row["seller_sku"],
                        row["skus_json"], row["cargo_type"], row["cross_border_type"],
                        int(row["is_zero_order"]), row["observed_at"], row["collector_date_from"],
                        row["collector_date_to"], row["collector_cursor"],
                    )
                    for row in observations
                ],
            )
            new_count = conn.total_changes - before
            conn.executemany(
                f"""INSERT OR IGNORE INTO {STATUS_OBSERVATIONS_TABLE}(
                       observation_id,order_id,order_revision,status_digest,supplier_status,
                       wb_status,positive_quantity,observed_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                [
                    (
                        row["observation_id"], row["order_id"], row["order_revision"],
                        row["status_digest"], row["supplier_status"], row["wb_status"],
                        row["positive_quantity"], row["observed_at"],
                    )
                    for row in status_observations
                ],
            )
            self._persist_identity_evidence(conn, observations)
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

    def _persist_identity_evidence(
        self, conn: sqlite3.Connection, observations: list[Mapping[str, Any]]
    ) -> None:
        for row in observations:
            warehouse_id = row.get("warehouse_id")
            barcodes = json.loads(str(row["skus_json"]))
            barcode = str(barcodes[0] or "") if isinstance(barcodes, list) and len(barcodes) == 1 else ""
            seller_sku = str(row.get("seller_sku") or "")
            warehouse_mapping = conn.execute(
                f"""SELECT mapping_id FROM {WAREHOUSE_MAPPINGS_TABLE}
                    WHERE seller_warehouse_id=? AND active=1
                    ORDER BY created_at DESC,mapping_id DESC LIMIT 1""",
                (warehouse_id,),
            ).fetchone() if warehouse_id else None
            identity_mapping = conn.execute(
                f"""SELECT mapping_id FROM {IDENTITY_MAPPINGS_TABLE}
                    WHERE source_nm_id=? AND source_chrt_id=? AND source_barcode=?
                      AND source_sku=? AND active=1
                    ORDER BY created_at DESC,mapping_id DESC LIMIT 1""",
                (row["nm_id"], row.get("chrt_id") or 0, barcode, seller_sku),
            ).fetchone() if row.get("chrt_id") and barcode and seller_sku else None
            outcome = "unmatched_warehouse" if warehouse_mapping is None else (
                "deferred" if not row.get("chrt_id") or not barcode or not seller_sku else
                "unmatched_identity" if identity_mapping is None else "matched"
            )
            evidence = {
                "order_id": int(row["order_id"]),
                "order_revision": str(row["source_revision"]),
                "warehouse_id": warehouse_id,
                "nm_id": int(row["nm_id"]),
                "chrt_id": row.get("chrt_id"),
                "barcode": barcode,
                "seller_sku": seller_sku,
                "warehouse_mapping_id": str(warehouse_mapping[0]) if warehouse_mapping else "",
                "identity_mapping_id": str(identity_mapping[0]) if identity_mapping else "",
                "outcome": outcome,
            }
            digest = _fingerprint(evidence)
            conn.execute(
                f"""INSERT OR IGNORE INTO {IDENTITY_EVIDENCE_TABLE}(
                       evidence_id,order_id,order_revision,warehouse_id,nm_id,chrt_id,barcode,
                       seller_sku,outcome,warehouse_mapping_id,identity_mapping_id,evidence_digest,observed_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "fbs_map_" + digest.removeprefix("sha256:")[:32], evidence["order_id"],
                    evidence["order_revision"], warehouse_id, evidence["nm_id"], evidence["chrt_id"],
                    barcode, seller_sku, outcome, evidence["warehouse_mapping_id"],
                    evidence["identity_mapping_id"], digest, row["observed_at"],
                ),
            )

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
            "upstream_methods": ["GET /api/v3/orders", "POST /api/v3/orders/status"],
            "uses_status_post": True,
            "status_post_semantic": "official_read_only",
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
        "seller_sku": _safe_text(raw.get("article") or raw.get("vendorCode"), maximum=160),
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


def _normalize_status(
    status: WbFbsOrderStatus,
    *,
    order: Mapping[str, Any],
    observed_at: str,
) -> dict[str, Any]:
    safe = {
        "order_id": int(status.order_id),
        "order_revision": str(order["source_revision"]),
        "supplier_status": _safe_text(status.supplier_status, maximum=80),
        "wb_status": _safe_text(status.wb_status, maximum=80),
        "positive_quantity": 1,
    }
    digest = _fingerprint(safe)
    return {
        **safe,
        "status_digest": digest,
        "observation_id": "fbs_status_" + hashlib.sha256(
            f"{safe['order_id']}:{safe['order_revision']}:{digest}".encode("utf-8")
        ).hexdigest()[:32],
        "observed_at": observed_at,
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
        "seller_sku": str(row["seller_sku"] or ""),
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


def _shadow_state(conn: sqlite3.Connection, state: Mapping[str, Any]) -> dict[str, Any]:
    earliest = conn.execute(
        f"SELECT MIN(source_created_at) FROM {OBSERVATIONS_TABLE} WHERE source_created_at<>''"
    ).fetchone()[0]
    status_count = int(conn.execute(f"SELECT COUNT(*) FROM {STATUS_OBSERVATIONS_TABLE}").fetchone()[0])
    evidence_counts = {
        str(row["outcome"]): int(row["count"])
        for row in conn.execute(
            f"SELECT outcome,COUNT(*) AS count FROM {IDENTITY_EVIDENCE_TABLE} GROUP BY outcome"
        ).fetchall()
    }
    warehouse_mappings = int(
        conn.execute(f"SELECT COUNT(*) FROM {WAREHOUSE_MAPPINGS_TABLE} WHERE active=1").fetchone()[0]
    )
    identity_mappings = int(
        conn.execute(f"SELECT COUNT(*) FROM {IDENTITY_MAPPINGS_TABLE} WHERE active=1").fetchone()[0]
    )
    return {
        "mode": "query_only_shadow",
        "collector_default_off": True,
        "backfill_execution_enabled": False,
        "backfill_plan": {
            "review_range_from": BACKFILL_REVIEW_FROM,
            "earliest_official_order_date": str(earliest or "")[:10],
            "earliest_date_is_computed": True,
            "guessed_start_date": False,
            "cursor": int(state.get("next_cursor") or 0),
            "last_error": str(state.get("error") or ""),
        },
        "status_observation_count": status_count,
        "warehouse_mapping_count": warehouse_mappings,
        "identity_mapping_count": identity_mappings,
        "mapping_outcomes": evidence_counts,
        "unmatched_count": int(evidence_counts.get("unmatched_warehouse", 0))
        + int(evidence_counts.get("unmatched_identity", 0)),
        "live_physical_trigger": None,
        "supplier_status_complete_triggers_debit": False,
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
        "upstream_get_only": False,
        "status_post_is_read_semantic": True,
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
        "supplier_status_complete_triggers_debit": False,
        "live_physical_trigger_selected": False,
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
