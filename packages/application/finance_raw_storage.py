"""Finance raw/outbox and operational inbox boundary.

The classes here are inert unless explicitly invoked.  They implement the
replayable transactional-outbox seam needed for the storage split without
introducing a distributed transaction between SQLite files.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from packages.application.storage_registry import (
    OPERATIONAL_SCHEMA_REVISION,
    RAW_SCHEMA_REVISION,
    StoreRegistry,
    StorageRegistryError,
    explain_query_plan,
)


RAW_SCHEMA_TABLES = frozenset(
    {
        "finance_raw_schema_meta",
        "finance_raw_ingest_batches",
        "finance_raw_rows",
        "finance_raw_batch_rows",
        "finance_raw_outbox",
        "finance_raw_consumer_cursors",
        "finance_raw_bridge_cursors",
    }
)
OPERATIONAL_SCHEMA_TABLES = frozenset(
    {
        "finance_operational_schema_meta",
        "finance_operational_inbox",
        "finance_operational_receipts",
        "finance_operational_consumer_cursors",
        "finance_operational_dead_letters",
        "finance_storage_shadow_comparisons",
        "finance_storage_migration_chunks",
    }
)
OUTBOX_EVENT_TYPE = "finance.raw.batch_committed.v1"
CONSUMER_ID = "finance_operational_projection_v1"


class FinanceStorageError(ValueError):
    """Fail-closed Finance storage or replay error."""


class InjectedFinanceStorageFault(RuntimeError):
    """Test-only deterministic crash/fault injection marker."""


@dataclass(frozen=True)
class IngestResult:
    status: str
    batch_id: str
    row_count: int
    rows_digest: str
    event_id: str
    sequence_no: int


@dataclass(frozen=True)
class ConsumeResult:
    status: str
    consumer_id: str
    event_id: str
    sequence_no: int
    duplicate: bool


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _digest(value: Any) -> str:
    raw = value if isinstance(value, str) else _canonical_json(value)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def ensure_raw_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS finance_raw_schema_meta (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            schema_revision TEXT NOT NULL,
            logical_store TEXT NOT NULL DEFAULT 'finance_raw',
            generation_id TEXT NOT NULL DEFAULT 'unbound',
            generation_epoch TEXT NOT NULL DEFAULT 'unbound',
            source_fingerprint TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS finance_raw_ingest_batches (
            batch_id TEXT PRIMARY KEY,
            source_identity TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            report_period TEXT NOT NULL,
            seller_id TEXT NOT NULL DEFAULT '',
            week_start TEXT NOT NULL DEFAULT '',
            week_end TEXT NOT NULL DEFAULT '',
            row_count INTEGER NOT NULL,
            rows_digest TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('loading','committed','failed')),
            created_at TEXT NOT NULL,
            committed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS finance_raw_rows (
            raw_row_id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            batch_sequence_no INTEGER NOT NULL,
            seller_id TEXT NOT NULL,
            report_id TEXT NOT NULL,
            rrd_id TEXT NOT NULL,
            report_type INTEGER,
            week_start TEXT NOT NULL,
            week_end TEXT NOT NULL,
            nm_id TEXT,
            vendor_code TEXT,
            barcode TEXT,
            doc_type_name TEXT,
            seller_oper_name TEXT,
            row_hash TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            FOREIGN KEY(batch_id) REFERENCES finance_raw_ingest_batches(batch_id),
            UNIQUE(batch_id,batch_sequence_no),
            UNIQUE(batch_id,seller_id,report_id,rrd_id)
        );
        CREATE TABLE IF NOT EXISTS finance_raw_batch_rows (
            batch_id TEXT NOT NULL,
            batch_sequence_no INTEGER NOT NULL,
            raw_row_id TEXT NOT NULL,
            PRIMARY KEY(batch_id,batch_sequence_no),
            UNIQUE(batch_id,raw_row_id),
            FOREIGN KEY(batch_id) REFERENCES finance_raw_ingest_batches(batch_id),
            FOREIGN KEY(raw_row_id) REFERENCES finance_raw_rows(raw_row_id)
        );
        CREATE INDEX IF NOT EXISTS finance_raw_batch_rows_by_raw
        ON finance_raw_batch_rows(raw_row_id,batch_id);
        CREATE INDEX IF NOT EXISTS finance_raw_rows_by_business_identity
        ON finance_raw_rows(seller_id,report_id,rrd_id,batch_id);
        CREATE INDEX IF NOT EXISTS finance_raw_rows_by_week
        ON finance_raw_rows(seller_id,week_start,week_end,report_id,rrd_id);
        CREATE INDEX IF NOT EXISTS finance_raw_rows_by_sku_week
        ON finance_raw_rows(seller_id,nm_id,week_start,week_end,report_id,rrd_id);
        CREATE TABLE IF NOT EXISTS finance_raw_outbox (
            event_id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            sequence_no INTEGER NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            published_at TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            FOREIGN KEY(batch_id) REFERENCES finance_raw_ingest_batches(batch_id)
        );
        CREATE INDEX IF NOT EXISTS finance_raw_outbox_pending
        ON finance_raw_outbox(sequence_no,published_at);
        CREATE UNIQUE INDEX IF NOT EXISTS finance_raw_outbox_by_batch
        ON finance_raw_outbox(batch_id);
        CREATE TABLE IF NOT EXISTS finance_raw_consumer_cursors (
            consumer_id TEXT PRIMARY KEY,
            last_sequence_no INTEGER NOT NULL,
            last_event_id TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS finance_raw_bridge_cursors (
            bridge_id TEXT PRIMARY KEY,
            last_sequence_no INTEGER NOT NULL,
            last_event_id TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO finance_raw_schema_meta(
            singleton,schema_revision,logical_store,generation_id,
            generation_epoch,source_fingerprint,created_at
        )
        VALUES(
            1,'{RAW_SCHEMA_REVISION}','finance_raw','unbound','unbound','',
            '{_utc_now()}'
        )
        ON CONFLICT(singleton) DO UPDATE SET schema_revision=excluded.schema_revision;
        """
    )
    batch_columns = {
        str(row[1])
        for row in conn.execute(
            "PRAGMA table_info(finance_raw_ingest_batches)"
        ).fetchall()
    }
    for column in ("seller_id", "week_start", "week_end"):
        if column not in batch_columns:
            conn.execute(
                f"""ALTER TABLE finance_raw_ingest_batches
                    ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"""
            )
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS finance_raw_batches_by_scope_commit
        ON finance_raw_ingest_batches(
            status,seller_id,week_start,week_end,
            committed_at,created_at,batch_id
        );
        DROP VIEW IF EXISTS finance_raw_current_rows;
        CREATE VIEW finance_raw_current_rows AS
        SELECT
               rows.seller_id,rows.report_id,rows.rrd_id,rows.report_type,
               rows.week_start,rows.week_end,rows.nm_id,rows.vendor_code,
               rows.barcode,rows.doc_type_name,rows.seller_oper_name,
               rows.row_hash,rows.raw_json,rows.first_seen_at,
               COALESCE(
                   batch.committed_at,rows.first_seen_at
               ) AS updated_at
          FROM finance_raw_batch_rows AS links
          JOIN finance_raw_rows AS rows
            ON rows.raw_row_id=links.raw_row_id
          JOIN finance_raw_ingest_batches AS batch
            ON batch.batch_id=links.batch_id
          LEFT JOIN finance_raw_outbox AS event
            ON event.batch_id=batch.batch_id
         WHERE batch.status='committed'
           AND NOT EXISTS(
               SELECT 1
                 FROM finance_raw_ingest_batches AS newer
                 LEFT JOIN finance_raw_outbox AS newer_event
                   ON newer_event.batch_id=newer.batch_id
                WHERE newer.status='committed'
                  AND newer.seller_id IN ('*',rows.seller_id)
                  AND newer.week_start<=rows.week_start
                  AND newer.week_end>=rows.week_end
                  AND (
                      COALESCE(newer_event.sequence_no,0)
                        > COALESCE(event.sequence_no,0)
                      OR (
                          COALESCE(newer_event.sequence_no,0)
                            = COALESCE(event.sequence_no,0)
                          AND (
                              COALESCE(
                                  newer.committed_at,newer.created_at
                              ) > COALESCE(
                                  batch.committed_at,batch.created_at
                              )
                              OR (
                                  COALESCE(
                                      newer.committed_at,newer.created_at
                                  ) = COALESCE(
                                      batch.committed_at,batch.created_at
                                  )
                                  AND newer.batch_id>batch.batch_id
                              )
                          )
                      )
                  )
           );
        """
    )


def ensure_operational_schema(conn: sqlite3.Connection) -> None:
    if OPERATIONAL_SCHEMA_TABLES.issubset(_table_names(conn)):
        return
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS finance_operational_schema_meta (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            schema_revision TEXT NOT NULL,
            logical_store TEXT NOT NULL DEFAULT 'operational',
            generation_id TEXT NOT NULL DEFAULT 'unbound',
            generation_epoch TEXT NOT NULL DEFAULT 'unbound',
            source_fingerprint TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS finance_operational_inbox (
            event_id TEXT PRIMARY KEY,
            consumer_id TEXT NOT NULL,
            sequence_no INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            received_at TEXT NOT NULL,
            UNIQUE(consumer_id,sequence_no)
        );
        CREATE TABLE IF NOT EXISTS finance_operational_receipts (
            consumer_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            sequence_no INTEGER NOT NULL,
            source_revision TEXT NOT NULL,
            result_row_count INTEGER NOT NULL,
            result_digest TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            PRIMARY KEY(consumer_id,event_id),
            UNIQUE(consumer_id,sequence_no)
        );
        CREATE TABLE IF NOT EXISTS finance_operational_consumer_cursors (
            consumer_id TEXT PRIMARY KEY,
            last_sequence_no INTEGER NOT NULL,
            last_event_id TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS finance_operational_dead_letters (
            consumer_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            sequence_no INTEGER NOT NULL,
            payload_sha256 TEXT NOT NULL,
            attempt_count INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('retry','action_required','resolved')),
            last_error TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(consumer_id,event_id)
        );
        CREATE TABLE IF NOT EXISTS finance_storage_shadow_comparisons (
            comparison_id TEXT PRIMARY KEY,
            generation_epoch TEXT NOT NULL,
            scope_json TEXT NOT NULL,
            source_row_count INTEGER NOT NULL,
            shadow_row_count INTEGER NOT NULL,
            source_digest TEXT NOT NULL,
            shadow_digest TEXT NOT NULL,
            source_query_plan_json TEXT NOT NULL,
            shadow_query_plan_json TEXT NOT NULL,
            source_latency_ms INTEGER NOT NULL,
            shadow_latency_ms INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('match','mismatch','blocked')),
            detail_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS finance_storage_migration_chunks (
            migration_id TEXT NOT NULL,
            store_name TEXT NOT NULL,
            chunk_id TEXT NOT NULL,
            source_first_key TEXT NOT NULL,
            source_last_key TEXT NOT NULL,
            source_row_count INTEGER NOT NULL,
            source_digest TEXT NOT NULL,
            destination_row_count INTEGER NOT NULL,
            destination_digest TEXT NOT NULL,
            bytes_written INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('planned','copying','verified','error')),
            error TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(migration_id,store_name,chunk_id)
        );
        INSERT INTO finance_operational_schema_meta(
            singleton,schema_revision,logical_store,generation_id,
            generation_epoch,source_fingerprint,created_at
        )
        VALUES(
            1,'{OPERATIONAL_SCHEMA_REVISION}','operational','unbound','unbound','',
            '{_utc_now()}'
        )
        ON CONFLICT(singleton) DO UPDATE SET schema_revision=excluded.schema_revision;
        """
    )


def bind_generation_identity(
    conn: sqlite3.Connection,
    *,
    logical_store: str,
    generation_id: str,
    generation_epoch: str,
    source_fingerprint: str,
) -> None:
    if logical_store == "finance_raw":
        table = "finance_raw_schema_meta"
        expected_revision = RAW_SCHEMA_REVISION
    elif logical_store == "operational":
        table = "finance_operational_schema_meta"
        expected_revision = OPERATIONAL_SCHEMA_REVISION
    else:
        raise FinanceStorageError(f"unsupported logical store: {logical_store}")
    values = {
        "generation_id": str(generation_id or "").strip(),
        "generation_epoch": str(generation_epoch or "").strip(),
        "source_fingerprint": str(source_fingerprint or "").strip(),
    }
    if not all(values.values()):
        raise FinanceStorageError("complete generation identity is required")
    conn.execute(
        f"""UPDATE {table}
            SET schema_revision=?,logical_store=?,generation_id=?,
                generation_epoch=?,source_fingerprint=?
            WHERE singleton=1""",
        (
            expected_revision,
            logical_store,
            values["generation_id"],
            values["generation_epoch"],
            values["source_fingerprint"],
        ),
    )


def _row_value(row: Mapping[str, Any], camel: str, snake: str) -> Any:
    return row.get(camel) if camel in row else row.get(snake)


def _normalize_raw_row(
    row: Mapping[str, Any],
    *,
    seller_id: str,
    week_start: str,
    week_end: str,
) -> dict[str, Any]:
    raw = dict(row)
    report_id = str(_row_value(raw, "reportId", "report_id") or "").strip()
    rrd_id = str(_row_value(raw, "rrdId", "rrd_id") or "").strip()
    if not report_id or not rrd_id:
        raise FinanceStorageError("Finance row must contain reportId and rrdId")
    raw_json = _canonical_json(raw)
    row_hash = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
    identity = {
        "seller_id": seller_id,
        "report_id": report_id,
        "rrd_id": rrd_id,
        "row_hash": row_hash,
    }
    return {
        **identity,
        "raw_row_id": _digest(identity),
        "report_type": int(_row_value(raw, "reportType", "report_type") or 0),
        "week_start": week_start,
        "week_end": week_end,
        "nm_id": str(_row_value(raw, "nmId", "nm_id") or ""),
        "vendor_code": str(_row_value(raw, "vendorCode", "vendor_code") or ""),
        "barcode": str(_row_value(raw, "sku", "barcode") or ""),
        "doc_type_name": str(_row_value(raw, "docTypeName", "doc_type_name") or ""),
        "seller_oper_name": str(
            _row_value(raw, "sellerOperName", "seller_oper_name") or ""
        ),
        "raw_json": raw_json,
    }


class FinanceRawIngestor:
    def __init__(
        self,
        registry: StoreRegistry,
        *,
        seller_id: str = "canonical",
        now_factory: Callable[[], str] = _utc_now,
    ) -> None:
        self.registry = registry
        self.seller_id = seller_id or "canonical"
        self.now_factory = now_factory

    def ingest_batch(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        source_identity: str,
        source_sha256: str,
        week_start: date | str,
        week_end: date | str,
        connection: sqlite3.Connection | None = None,
        fault_at: str = "",
        allow_empty_snapshot: bool = False,
    ) -> IngestResult:
        period_start = (
            week_start.isoformat() if isinstance(week_start, date) else str(week_start)
        )
        period_end = week_end.isoformat() if isinstance(week_end, date) else str(week_end)
        normalized = [
            _normalize_raw_row(
                row,
                seller_id=self.seller_id,
                week_start=period_start,
                week_end=period_end,
            )
            for row in rows
        ]
        if not normalized and not allow_empty_snapshot:
            raise FinanceStorageError("Finance raw batch must contain at least one row")
        normalized.sort(key=lambda item: (item["report_id"], item["rrd_id"], item["row_hash"]))
        rows_digest = _digest(
            [
                [
                    item["seller_id"],
                    item["report_id"],
                    item["rrd_id"],
                    item["row_hash"],
                ]
                for item in normalized
            ]
        )
        batch_identity = {
            "source_identity": str(source_identity or "").strip(),
            "source_sha256": str(source_sha256 or "").strip(),
            "report_period": f"{period_start}/{period_end}",
            "rows_digest": rows_digest,
        }
        if not batch_identity["source_identity"] or not batch_identity["source_sha256"]:
            raise FinanceStorageError("source identity and SHA-256 are required")
        batch_id = _digest(batch_identity)
        event_id = _digest({"batch_id": batch_id, "event_type": OUTBOX_EVENT_TYPE})
        owned_connection = connection is None
        conn = connection or self.registry.connect(
            "finance_raw",
            mode="rw",
            operation="finance_raw_ingest",
            isolation_level=None,
        )
        try:
            ensure_raw_schema(conn)
            existing = conn.execute(
                "SELECT status,row_count,rows_digest FROM finance_raw_ingest_batches WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["status"]) != "committed"
                    or int(existing["row_count"]) != len(normalized)
                    or str(existing["rows_digest"]) != rows_digest
                ):
                    raise FinanceStorageError("existing Finance raw batch is inconsistent")
                event = conn.execute(
                    "SELECT event_id,sequence_no FROM finance_raw_outbox WHERE batch_id=?",
                    (batch_id,),
                ).fetchone()
                if event is None:
                    raise FinanceStorageError("committed Finance raw batch has no outbox event")
                return IngestResult(
                    status="no_op",
                    batch_id=batch_id,
                    row_count=len(normalized),
                    rows_digest=rows_digest,
                    event_id=str(event["event_id"]),
                    sequence_no=int(event["sequence_no"]),
                )
            if fault_at == "before_transaction":
                raise InjectedFinanceStorageFault(fault_at)
            started_transaction = not conn.in_transaction
            if started_transaction:
                conn.execute("BEGIN IMMEDIATE")
            created_at = self.now_factory()
            conn.execute(
                """INSERT INTO finance_raw_ingest_batches(
                   batch_id,source_identity,source_sha256,report_period,
                   seller_id,week_start,week_end,row_count,rows_digest,status,
                   created_at,committed_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,'loading',?,NULL)""",
                (
                    batch_id,
                    batch_identity["source_identity"],
                    batch_identity["source_sha256"],
                    batch_identity["report_period"],
                    self.seller_id,
                    period_start,
                    period_end,
                    len(normalized),
                    rows_digest,
                    created_at,
                ),
            )
            for index, item in enumerate(normalized, start=1):
                inserted = conn.execute(
                    """INSERT OR IGNORE INTO finance_raw_rows(
                       raw_row_id,batch_id,batch_sequence_no,seller_id,report_id,rrd_id,
                       report_type,week_start,week_end,nm_id,vendor_code,barcode,
                       doc_type_name,seller_oper_name,row_hash,raw_json,first_seen_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        item["raw_row_id"],
                        batch_id,
                        index,
                        item["seller_id"],
                        item["report_id"],
                        item["rrd_id"],
                        item["report_type"],
                        item["week_start"],
                        item["week_end"],
                        item["nm_id"],
                        item["vendor_code"],
                        item["barcode"],
                        item["doc_type_name"],
                        item["seller_oper_name"],
                        item["row_hash"],
                        item["raw_json"],
                        created_at,
                    ),
                )
                if inserted.rowcount == 0:
                    existing_row = conn.execute(
                        """SELECT seller_id,report_id,rrd_id,report_type,
                                  week_start,week_end,nm_id,vendor_code,barcode,
                                  doc_type_name,seller_oper_name,row_hash,raw_json
                           FROM finance_raw_rows WHERE raw_row_id=?""",
                        (item["raw_row_id"],),
                    ).fetchone()
                    expected_row = (
                        item["seller_id"],
                        item["report_id"],
                        item["rrd_id"],
                        item["report_type"],
                        item["week_start"],
                        item["week_end"],
                        item["nm_id"],
                        item["vendor_code"],
                        item["barcode"],
                        item["doc_type_name"],
                        item["seller_oper_name"],
                        item["row_hash"],
                        item["raw_json"],
                    )
                    if (
                        existing_row is None
                        or tuple(existing_row) != expected_row
                    ):
                        raise FinanceStorageError(
                            "existing Finance raw row conflicts with "
                            "immutable source identity"
                        )
                conn.execute(
                    """INSERT INTO finance_raw_batch_rows(
                       batch_id,batch_sequence_no,raw_row_id
                       ) VALUES(?,?,?)""",
                    (batch_id, index, item["raw_row_id"]),
                )
            if fault_at == "after_rows_before_outbox":
                raise InjectedFinanceStorageFault(fault_at)
            sequence_no = int(
                conn.execute(
                    "SELECT COALESCE(MAX(sequence_no),0)+1 FROM finance_raw_outbox"
                ).fetchone()[0]
            )
            event_payload = {
                "contract_version": OUTBOX_EVENT_TYPE,
                "event_id": event_id,
                "batch_id": batch_id,
                "seller_id": self.seller_id,
                "report_period": batch_identity["report_period"],
                "row_count": len(normalized),
                "rows_digest": rows_digest,
                "source_identity": batch_identity["source_identity"],
                "source_sha256": batch_identity["source_sha256"],
            }
            payload_json = _canonical_json(event_payload)
            conn.execute(
                """INSERT INTO finance_raw_outbox(
                   event_id,batch_id,sequence_no,event_type,payload_json,payload_sha256,
                   created_at,published_at,attempt_count,last_error
                   ) VALUES(?,?,?,?,?,?,?,NULL,0,NULL)""",
                (
                    event_id,
                    batch_id,
                    sequence_no,
                    OUTBOX_EVENT_TYPE,
                    payload_json,
                    _digest(payload_json),
                    created_at,
                ),
            )
            conn.execute(
                """UPDATE finance_raw_ingest_batches
                   SET status='committed',committed_at=? WHERE batch_id=?""",
                (created_at, batch_id),
            )
            if fault_at == "before_raw_commit":
                raise InjectedFinanceStorageFault(fault_at)
            if started_transaction:
                conn.commit()
            if fault_at == "after_raw_commit":
                raise InjectedFinanceStorageFault(fault_at)
            return IngestResult(
                status="committed",
                batch_id=batch_id,
                row_count=len(normalized),
                rows_digest=rows_digest,
                event_id=event_id,
                sequence_no=sequence_no,
            )
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            if owned_connection:
                conn.close()


ApplyEvent = Callable[[sqlite3.Connection, Mapping[str, Any]], tuple[int, str]]


class FinanceOutboxConsumer:
    def __init__(
        self,
        registry: StoreRegistry,
        *,
        consumer_id: str = CONSUMER_ID,
        apply_event: ApplyEvent | None = None,
        now_factory: Callable[[], str] = _utc_now,
        poison_threshold: int = 3,
    ) -> None:
        self.registry = registry
        self.consumer_id = consumer_id
        self.apply_event = apply_event or self._default_apply
        self.now_factory = now_factory
        self.poison_threshold = max(1, int(poison_threshold))

    @staticmethod
    def _default_apply(
        _conn: sqlite3.Connection, payload: Mapping[str, Any]
    ) -> tuple[int, str]:
        return int(payload.get("row_count") or 0), str(payload.get("rows_digest") or "")

    def consume_next(self, *, fault_at: str = "") -> ConsumeResult | None:
        with self.registry.session(
            "finance_raw",
            mode="ro",
            operation="finance_outbox_consumer_read",
        ) as raw:
            if not RAW_SCHEMA_TABLES.issubset(_table_names(raw)):
                return None
            cursor = raw.execute(
                "SELECT last_sequence_no FROM finance_raw_consumer_cursors WHERE consumer_id=?",
                (self.consumer_id,),
            ).fetchone()
            last_sequence = int(cursor["last_sequence_no"]) if cursor is not None else 0
            event = raw.execute(
                """SELECT event_id,batch_id,sequence_no,event_type,payload_json,payload_sha256
                   FROM finance_raw_outbox
                   WHERE sequence_no>? ORDER BY sequence_no LIMIT 1""",
                (last_sequence,),
            ).fetchone()
            if event is None:
                return None
            if int(event["sequence_no"]) != last_sequence + 1:
                raise FinanceStorageError(
                    "outbox sequence gap/reorder requires explicit recovery"
                )
            event_payload = dict(event)
            payload = json.loads(str(event_payload["payload_json"]))
            if _digest(str(event_payload["payload_json"])) != str(
                event_payload["payload_sha256"]
            ):
                raise FinanceStorageError("outbox payload digest mismatch")
        if fault_at == "after_outbox_read":
            raise InjectedFinanceStorageFault(fault_at)
        try:
            with self.registry.session(
                "operational",
                mode="rw",
                operation="finance_outbox_consumer_apply",
                isolation_level=None,
            ) as operational:
                ensure_operational_schema(operational)
                operational.execute("BEGIN IMMEDIATE")
                receipt = operational.execute(
                    """SELECT sequence_no,result_row_count,result_digest
                       FROM finance_operational_receipts
                       WHERE consumer_id=? AND event_id=?""",
                    (self.consumer_id, event_payload["event_id"]),
                ).fetchone()
                duplicate = receipt is not None
                if receipt is None:
                    operational.execute(
                        """INSERT INTO finance_operational_inbox(
                           event_id,consumer_id,sequence_no,event_type,payload_sha256,received_at
                           ) VALUES(?,?,?,?,?,?)""",
                        (
                            event_payload["event_id"],
                            self.consumer_id,
                            int(event_payload["sequence_no"]),
                            event_payload["event_type"],
                            event_payload["payload_sha256"],
                            self.now_factory(),
                        ),
                    )
                    if fault_at == "after_inbox_before_apply":
                        raise InjectedFinanceStorageFault(fault_at)
                    result_row_count, result_digest = self.apply_event(operational, payload)
                    if result_row_count < 0 or not str(result_digest):
                        raise FinanceStorageError("consumer apply returned invalid readback")
                    if fault_at == "after_apply_before_receipt":
                        raise InjectedFinanceStorageFault(fault_at)
                    operational.execute(
                        """INSERT INTO finance_operational_receipts(
                           consumer_id,event_id,sequence_no,source_revision,result_row_count,
                           result_digest,applied_at
                           ) VALUES(?,?,?,?,?,?,?)""",
                        (
                            self.consumer_id,
                            event_payload["event_id"],
                            int(event_payload["sequence_no"]),
                            str(payload.get("rows_digest") or ""),
                            int(result_row_count),
                            str(result_digest),
                            self.now_factory(),
                        ),
                    )
                current = operational.execute(
                    """SELECT last_sequence_no FROM finance_operational_consumer_cursors
                       WHERE consumer_id=?""",
                    (self.consumer_id,),
                ).fetchone()
                current_sequence = int(current["last_sequence_no"]) if current else 0
                if int(event_payload["sequence_no"]) < current_sequence:
                    raise FinanceStorageError("outbox event reorder would move the cursor backward")
                operational.execute(
                    """INSERT INTO finance_operational_consumer_cursors(
                       consumer_id,last_sequence_no,last_event_id,source_revision,updated_at
                       ) VALUES(?,?,?,?,?)
                       ON CONFLICT(consumer_id) DO UPDATE SET
                       last_sequence_no=excluded.last_sequence_no,
                       last_event_id=excluded.last_event_id,
                       source_revision=excluded.source_revision,
                       updated_at=excluded.updated_at
                       WHERE finance_operational_consumer_cursors.last_sequence_no<=excluded.last_sequence_no""",
                    (
                        self.consumer_id,
                        int(event_payload["sequence_no"]),
                        event_payload["event_id"],
                        str(payload.get("rows_digest") or ""),
                        self.now_factory(),
                    ),
                )
                operational.execute(
                    """UPDATE finance_operational_dead_letters
                       SET status='resolved',updated_at=?
                       WHERE consumer_id=? AND event_id=?""",
                    (self.now_factory(), self.consumer_id, event_payload["event_id"]),
                )
                if fault_at == "before_operational_commit":
                    raise InjectedFinanceStorageFault(fault_at)
                operational.commit()
            if fault_at == "after_operational_commit_before_ack":
                raise InjectedFinanceStorageFault(fault_at)
        except InjectedFinanceStorageFault:
            raise
        except Exception as exc:
            self._record_failure(event_payload, exc)
            raise
        with self.registry.session(
            "finance_raw",
            mode="rw",
            operation="finance_outbox_consumer_ack",
            isolation_level=None,
        ) as raw_write:
            # The read phase already proved the complete raw schema. Re-running
            # DDL here can require an exclusive schema lock and strand an
            # otherwise complete duplicate acknowledgement behind a long
            # query-only Finance reconciliation.
            raw_write.execute("BEGIN IMMEDIATE")
            raw_write.execute(
                """UPDATE finance_raw_outbox
                   SET published_at=?,attempt_count=attempt_count+1,last_error=NULL
                   WHERE event_id=?""",
                (self.now_factory(), event_payload["event_id"]),
            )
            raw_write.execute(
                """INSERT INTO finance_raw_consumer_cursors(
                   consumer_id,last_sequence_no,last_event_id,updated_at
                   ) VALUES(?,?,?,?)
                   ON CONFLICT(consumer_id) DO UPDATE SET
                   last_sequence_no=excluded.last_sequence_no,
                   last_event_id=excluded.last_event_id,
                   updated_at=excluded.updated_at
                   WHERE finance_raw_consumer_cursors.last_sequence_no<=excluded.last_sequence_no""",
                (
                    self.consumer_id,
                    int(event_payload["sequence_no"]),
                    event_payload["event_id"],
                    self.now_factory(),
                ),
            )
            if fault_at == "before_outbox_ack_commit":
                raise InjectedFinanceStorageFault(fault_at)
            raw_write.commit()
        return ConsumeResult(
            status="duplicate_acknowledged" if duplicate else "applied",
            consumer_id=self.consumer_id,
            event_id=str(event_payload["event_id"]),
            sequence_no=int(event_payload["sequence_no"]),
            duplicate=duplicate,
        )

    def _record_failure(self, event: Mapping[str, Any], exc: Exception) -> None:
        with self.registry.session(
            "operational",
            mode="rw",
            operation="finance_outbox_consumer_dead_letter",
            isolation_level=None,
        ) as operational:
            ensure_operational_schema(operational)
            existing = operational.execute(
                """SELECT attempt_count FROM finance_operational_dead_letters
                   WHERE consumer_id=? AND event_id=?""",
                (self.consumer_id, event["event_id"]),
            ).fetchone()
            attempts = (int(existing["attempt_count"]) if existing else 0) + 1
            status = "action_required" if attempts >= self.poison_threshold else "retry"
            operational.execute(
                """INSERT INTO finance_operational_dead_letters(
                   consumer_id,event_id,sequence_no,payload_sha256,attempt_count,
                   status,last_error,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(consumer_id,event_id) DO UPDATE SET
                   attempt_count=excluded.attempt_count,status=excluded.status,
                   last_error=excluded.last_error,updated_at=excluded.updated_at""",
                (
                    self.consumer_id,
                    event["event_id"],
                    int(event["sequence_no"]),
                    event["payload_sha256"],
                    attempts,
                    status,
                    f"{type(exc).__name__}: {str(exc)[:500]}",
                    self.now_factory(),
                ),
            )
            operational.commit()


class FinanceRawLiveTailBridge:
    """Idempotently mirror committed raw batches into an unselected candidate.

    Connections are supplied by the future human-gated runner so this class
    cannot select a generation or switch a manifest. The source must be
    query-only; the destination must be the reviewed shadow raw generation.
    """

    def __init__(
        self,
        *,
        bridge_id: str = "finance_raw_live_tail_v1",
        now_factory: Callable[[], str] = _utc_now,
    ) -> None:
        self.bridge_id = str(bridge_id or "").strip()
        self.now_factory = now_factory
        if not self.bridge_id:
            raise FinanceStorageError("live-tail bridge_id is required")

    def plan_next(
        self,
        *,
        source: sqlite3.Connection,
        destination: sqlite3.Connection,
    ) -> dict[str, Any] | None:
        if int(source.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise FinanceStorageError("live-tail source must be query_only")
        if not RAW_SCHEMA_TABLES.issubset(_table_names(source)):
            raise FinanceStorageError("live-tail source raw schema is incomplete")
        if not RAW_SCHEMA_TABLES.issubset(_table_names(destination)):
            raise FinanceStorageError("live-tail destination raw schema is incomplete")
        cursor = destination.execute(
            """SELECT last_sequence_no FROM finance_raw_bridge_cursors
               WHERE bridge_id=?""",
            (self.bridge_id,),
        ).fetchone()
        last_sequence = int(cursor["last_sequence_no"]) if cursor is not None else 0
        event = source.execute(
            """SELECT event_id,batch_id,sequence_no,event_type,payload_json,
                      payload_sha256,created_at
               FROM finance_raw_outbox
               WHERE sequence_no>? ORDER BY sequence_no LIMIT 1""",
            (last_sequence,),
        ).fetchone()
        if event is None:
            return None
        if int(event["sequence_no"]) != last_sequence + 1:
            raise FinanceStorageError(
                "live-tail sequence gap/reorder requires explicit recovery"
            )
        if str(event["event_type"]) != OUTBOX_EVENT_TYPE:
            raise FinanceStorageError("live-tail source event type is unsupported")
        if _digest(str(event["payload_json"])) != str(event["payload_sha256"]):
            raise FinanceStorageError("live-tail source payload digest mismatch")
        batch = source.execute(
            """SELECT batch_id,source_identity,source_sha256,report_period,
                      seller_id,week_start,week_end,row_count,rows_digest,
                      status,created_at,committed_at
               FROM finance_raw_ingest_batches WHERE batch_id=?""",
            (event["batch_id"],),
        ).fetchone()
        if batch is None or str(batch["status"]) != "committed":
            raise FinanceStorageError("live-tail source batch is not committed")
        rows = source.execute(
            """SELECT rows.raw_row_id,rows.batch_id,
                      links.batch_sequence_no,rows.seller_id,rows.report_id,
                      rrd_id,report_type,week_start,week_end,nm_id,vendor_code,
                      barcode,doc_type_name,seller_oper_name,row_hash,raw_json,
                      first_seen_at
               FROM finance_raw_batch_rows AS links
               JOIN finance_raw_rows AS rows
                 ON rows.raw_row_id=links.raw_row_id
               WHERE links.batch_id=?
               ORDER BY links.batch_sequence_no""",
            (event["batch_id"],),
        ).fetchall()
        digest = _digest(
            [
                [
                    str(row["seller_id"]),
                    str(row["report_id"]),
                    str(row["rrd_id"]),
                    str(row["row_hash"]),
                ]
                for row in rows
            ]
        )
        if (
            len(rows) != int(batch["row_count"])
            or digest != str(batch["rows_digest"])
        ):
            raise FinanceStorageError("live-tail source batch count/digest mismatch")
        return {
            "contract_version": "finance_raw_live_tail_plan_v1",
            "bridge_id": self.bridge_id,
            "event_id": str(event["event_id"]),
            "batch_id": str(event["batch_id"]),
            "sequence_no": int(event["sequence_no"]),
            "row_count": len(rows),
            "rows_digest": digest,
            "source_identity": str(batch["source_identity"]),
            "source_sha256": str(batch["source_sha256"]),
            "destination_manifest_switch": False,
        }

    def apply_next(
        self,
        *,
        source: sqlite3.Connection,
        destination: sqlite3.Connection,
        fault_at: str = "",
    ) -> dict[str, Any] | None:
        plan = self.plan_next(source=source, destination=destination)
        if plan is None:
            return None
        batch = source.execute(
            """SELECT batch_id,source_identity,source_sha256,report_period,
                      seller_id,week_start,week_end,row_count,rows_digest,
                      status,created_at,committed_at
               FROM finance_raw_ingest_batches WHERE batch_id=?""",
            (plan["batch_id"],),
        ).fetchone()
        event = source.execute(
            """SELECT event_id,batch_id,sequence_no,event_type,payload_json,
                      payload_sha256,created_at,published_at,attempt_count,last_error
               FROM finance_raw_outbox WHERE event_id=?""",
            (plan["event_id"],),
        ).fetchone()
        rows = source.execute(
            """SELECT rows.raw_row_id,rows.batch_id,
                      links.batch_sequence_no,rows.seller_id,rows.report_id,
                      rrd_id,report_type,week_start,week_end,nm_id,vendor_code,
                      barcode,doc_type_name,seller_oper_name,row_hash,raw_json,
                      first_seen_at
               FROM finance_raw_batch_rows AS links
               JOIN finance_raw_rows AS rows
                 ON rows.raw_row_id=links.raw_row_id
               WHERE links.batch_id=?
               ORDER BY links.batch_sequence_no""",
            (plan["batch_id"],),
        ).fetchall()
        if batch is None or event is None:
            raise FinanceStorageError("live-tail source changed after planning")
        destination.execute("BEGIN IMMEDIATE")
        try:
            destination.execute(
                """INSERT OR IGNORE INTO finance_raw_ingest_batches(
                   batch_id,source_identity,source_sha256,report_period,
                   seller_id,week_start,week_end,row_count,rows_digest,status,
                   created_at,committed_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                tuple(batch),
            )
            for row in rows:
                destination.execute(
                    """INSERT OR IGNORE INTO finance_raw_rows(
                       raw_row_id,batch_id,batch_sequence_no,seller_id,report_id,
                       rrd_id,report_type,week_start,week_end,nm_id,vendor_code,
                       barcode,doc_type_name,seller_oper_name,row_hash,raw_json,
                       first_seen_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    tuple(row),
                )
                destination.execute(
                    """INSERT OR IGNORE INTO finance_raw_batch_rows(
                       batch_id,batch_sequence_no,raw_row_id
                       ) VALUES(?,?,?)""",
                    (
                        plan["batch_id"],
                        int(row["batch_sequence_no"]),
                        row["raw_row_id"],
                    ),
                )
            if fault_at == "after_rows_before_outbox":
                raise InjectedFinanceStorageFault(fault_at)
            destination.execute(
                """INSERT OR IGNORE INTO finance_raw_outbox(
                   event_id,batch_id,sequence_no,event_type,payload_json,
                   payload_sha256,created_at,published_at,attempt_count,last_error
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                tuple(event),
            )
            destination_rows = []
            for source_row in rows:
                destination_row = destination.execute(
                    """SELECT seller_id,report_id,rrd_id,row_hash,raw_json
                       FROM finance_raw_rows WHERE raw_row_id=?""",
                    (source_row["raw_row_id"],),
                ).fetchone()
                if (
                    destination_row is None
                    or str(destination_row["raw_json"])
                    != str(source_row["raw_json"])
                ):
                    raise FinanceStorageError(
                        "live-tail immutable raw row readback mismatch"
                    )
                destination_rows.append(destination_row)
            destination_digest = _digest(
                [
                    [
                        str(row["seller_id"]),
                        str(row["report_id"]),
                        str(row["rrd_id"]),
                        str(row["row_hash"]),
                    ]
                    for row in destination_rows
                ]
            )
            destination_event = destination.execute(
                """SELECT sequence_no,payload_sha256 FROM finance_raw_outbox
                   WHERE event_id=?""",
                (plan["event_id"],),
            ).fetchone()
            if (
                len(destination_rows) != int(plan["row_count"])
                or destination_digest != str(plan["rows_digest"])
                or destination_event is None
                or int(destination_event["sequence_no"]) != int(plan["sequence_no"])
                or str(destination_event["payload_sha256"])
                != str(event["payload_sha256"])
            ):
                raise FinanceStorageError(
                    "live-tail destination count/digest/event mismatch"
                )
            destination.execute(
                """INSERT INTO finance_raw_bridge_cursors(
                   bridge_id,last_sequence_no,last_event_id,source_revision,updated_at
                   ) VALUES(?,?,?,?,?)
                   ON CONFLICT(bridge_id) DO UPDATE SET
                   last_sequence_no=excluded.last_sequence_no,
                   last_event_id=excluded.last_event_id,
                   source_revision=excluded.source_revision,
                   updated_at=excluded.updated_at
                   WHERE finance_raw_bridge_cursors.last_sequence_no<excluded.last_sequence_no""",
                (
                    self.bridge_id,
                    int(plan["sequence_no"]),
                    plan["event_id"],
                    plan["rows_digest"],
                    self.now_factory(),
                ),
            )
            destination.commit()
        except Exception:
            if destination.in_transaction:
                destination.rollback()
            raise
        if fault_at == "after_destination_commit":
            raise InjectedFinanceStorageFault(fault_at)
        return {
            **plan,
            "status": "mirrored",
            "canonical_source_unchanged": True,
        }


def storage_health(registry: StoreRegistry) -> dict[str, Any]:
    health = registry.status()
    raw_tables: set[str] = set()
    operational_tables: set[str] = set()
    raw_counts: dict[str, int] = {}
    operational_counts: dict[str, int] = {}
    raw_cursor = 0
    bridge_cursor = 0
    operational_cursor = 0
    latest_outbox = 0
    mismatch_count = 0
    actionable_dead_letters = 0
    try:
        with registry.session(
            "finance_raw",
            mode="ro",
            operation="finance_storage_health_raw",
        ) as raw:
            raw_tables = _table_names(raw)
            if RAW_SCHEMA_TABLES.issubset(raw_tables):
                raw_counts = {
                    "batches": int(
                        raw.execute(
                            "SELECT COUNT(*) FROM finance_raw_ingest_batches WHERE status='committed'"
                        ).fetchone()[0]
                    ),
                    "rows": int(raw.execute("SELECT COUNT(*) FROM finance_raw_rows").fetchone()[0]),
                    "outbox": int(
                        raw.execute("SELECT COUNT(*) FROM finance_raw_outbox").fetchone()[0]
                    ),
                    "pending_outbox": int(
                        raw.execute(
                            "SELECT COUNT(*) FROM finance_raw_outbox WHERE published_at IS NULL"
                        ).fetchone()[0]
                    ),
                }
                latest_outbox = int(
                    raw.execute(
                        "SELECT COALESCE(MAX(sequence_no),0) FROM finance_raw_outbox"
                    ).fetchone()[0]
                )
                row = raw.execute(
                    "SELECT last_sequence_no FROM finance_raw_consumer_cursors WHERE consumer_id=?",
                    (CONSUMER_ID,),
                ).fetchone()
                raw_cursor = int(row["last_sequence_no"]) if row else 0
                bridge_cursor = int(
                    raw.execute(
                        """SELECT COALESCE(MAX(last_sequence_no),0)
                           FROM finance_raw_bridge_cursors"""
                    ).fetchone()[0]
                )
    except (sqlite3.Error, StorageRegistryError) as exc:
        health["raw_health_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    try:
        with registry.session(
            "operational",
            mode="ro",
            operation="finance_storage_health_operational",
        ) as operational:
            operational_tables = _table_names(operational)
            if OPERATIONAL_SCHEMA_TABLES.issubset(operational_tables):
                operational_counts = {
                    "receipts": int(
                        operational.execute(
                            "SELECT COUNT(*) FROM finance_operational_receipts"
                        ).fetchone()[0]
                    ),
                    "dead_letters": int(
                        operational.execute(
                            "SELECT COUNT(*) FROM finance_operational_dead_letters"
                        ).fetchone()[0]
                    ),
                }
                actionable_dead_letters = int(
                    operational.execute(
                        """SELECT COUNT(*) FROM finance_operational_dead_letters
                           WHERE status='action_required'"""
                    ).fetchone()[0]
                )
                mismatch_count = int(
                    operational.execute(
                        """SELECT COUNT(*) FROM finance_storage_shadow_comparisons
                           WHERE status='mismatch'"""
                    ).fetchone()[0]
                )
                row = operational.execute(
                    """SELECT last_sequence_no FROM finance_operational_consumer_cursors
                       WHERE consumer_id=?""",
                    (CONSUMER_ID,),
                ).fetchone()
                operational_cursor = int(row["last_sequence_no"]) if row else 0
    except (sqlite3.Error, StorageRegistryError) as exc:
        health["operational_health_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    filesystem = registry.runtime_dir.stat()
    capacity = registry.runtime_dir.stat().st_dev
    statvfs = Path(registry.runtime_dir)
    vfs = __import__("os").statvfs(statvfs)
    free_bytes = int(vfs.f_bavail * vfs.f_frsize)
    health.update(
        {
            "raw_schema_ready": RAW_SCHEMA_TABLES.issubset(raw_tables),
            "operational_schema_ready": OPERATIONAL_SCHEMA_TABLES.issubset(
                operational_tables
            ),
            "raw_counts": raw_counts,
            "operational_counts": operational_counts,
            "latest_outbox_sequence": latest_outbox,
            "raw_ack_cursor": raw_cursor,
            "live_tail_cursor": bridge_cursor,
            "live_tail_lag_events": max(0, latest_outbox - bridge_cursor),
            "operational_cursor": operational_cursor,
            "consumer_lag_events": max(0, latest_outbox - operational_cursor),
            "cursor_mismatch": raw_cursor != operational_cursor,
            "shadow_mismatch_count": mismatch_count,
            "actionable_dead_letters": actionable_dead_letters,
            "filesystem": {
                "device": int(capacity),
                "inode": int(filesystem.st_ino),
                "free_bytes": free_bytes,
            },
            "rollback_ready": health["canonical_source"] == "monolith"
            and health["rollback_generation_id"] == "monolith",
            "cutover_ready": bool(
                health["state"] == "shadow"
                and RAW_SCHEMA_TABLES.issubset(raw_tables)
                and OPERATIONAL_SCHEMA_TABLES.issubset(operational_tables)
                and latest_outbox == operational_cursor
                and raw_cursor == operational_cursor
                and mismatch_count == 0
                and actionable_dead_letters == 0
            ),
        }
    )
    return health


def shadow_compare_week(
    *,
    source_conn: sqlite3.Connection,
    shadow_conn: sqlite3.Connection,
    seller_id: str,
    week_start: str,
    week_end: str,
) -> dict[str, Any]:
    source_sql = """SELECT report_id,rrd_id,row_hash
                    FROM wb_finance_weekly_raw_rows
                    WHERE seller_id=? AND week_start=? AND week_end=?
                    ORDER BY report_id,rrd_id,row_hash"""
    shadow_sql = """SELECT report_id,rrd_id,row_hash
                    FROM finance_raw_current_rows
                    WHERE seller_id=? AND week_start=? AND week_end=?
                    ORDER BY report_id,rrd_id,row_hash"""
    params = (seller_id, week_start, week_end)

    def read(conn: sqlite3.Connection, sql: str) -> tuple[int, str]:
        digest = hashlib.sha256()
        count = 0
        for row in conn.execute(sql, params):
            digest.update(
                (_canonical_json([str(row[0]), str(row[1]), str(row[2])]) + "\n").encode(
                    "utf-8"
                )
            )
            count += 1
        return count, "sha256:" + digest.hexdigest()

    source_started = time.monotonic()
    source_count, source_digest = read(source_conn, source_sql)
    source_latency_ms = round(
        (time.monotonic() - source_started) * 1000,
        3,
    )
    shadow_started = time.monotonic()
    shadow_count, shadow_digest = read(shadow_conn, shadow_sql)
    shadow_latency_ms = round(
        (time.monotonic() - shadow_started) * 1000,
        3,
    )
    return {
        "scope": {
            "seller_id": seller_id,
            "week_start": week_start,
            "week_end": week_end,
        },
        "source_row_count": source_count,
        "shadow_row_count": shadow_count,
        "source_digest": source_digest,
        "shadow_digest": shadow_digest,
        "source_query_plan": explain_query_plan(source_conn, source_sql, params),
        "shadow_query_plan": explain_query_plan(shadow_conn, shadow_sql, params),
        "source_latency_ms": source_latency_ms,
        "shadow_latency_ms": shadow_latency_ms,
        "status": (
            "match"
            if source_count == shadow_count and source_digest == shadow_digest
            else "mismatch"
        ),
    }
