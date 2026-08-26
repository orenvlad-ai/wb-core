"""Durable default-off facility × pool business-document service.

This Stage 2 module is deliberately not imported by any HTTP/UI route or by a
current aggregate FF producer/consumer.  When a fixture or a future reviewed
cutover enables a pool writer epoch, immutable documents post only through the
Stage 1 operation and movement tables and update their bounded pool projection.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from packages.application.canonical_rub_money import (
    RUB_MINOR_UNIT as RUB_QUANTUM,
    canonical_rub_minor_units,
)
from packages.application.ff_pool_foundation import (
    BALANCES_TABLE,
    FACILITIES_TABLE,
    FEATURE_EPOCHS_TABLE,
    LINES_TABLE,
    OPERATIONS_TABLE,
    RELATIONS_TABLE,
    canonical_decimal_text,
    evaluate_ff_pool_aggregate_parity,
    ensure_ff_pool_foundation_schema,
    record_ff_pool_parity_diagnostic,
)
from packages.application.ff_pool_fbs_applicability import (
    FbsApplicabilityError,
    require_fbs_pair_writeable,
)
from packages.application.warehouse_recovery_policy import (
    RecoveryPolicyError,
    RecoveryState,
    WarehouseRecoveryRegistry,
)
from packages.application.warehouse_functional_lock import (
    warehouse_functional_write_lock,
)
from packages.contracts.ff_pool_documents import (
    DocumentIdentity,
    ExpenseLine,
    OVERHEAD_EXPENSE_CATEGORIES,
    OVERHEAD_EXPENSE_CATEGORY_LABELS_RU,
    OVERHEAD_SOURCE_MODES,
    PoolLocation,
)
from packages.contracts.russian_payment_orders import (
    RUSSIAN_PAYMENT_ORDER_CURRENCY,
    RUSSIAN_PAYMENT_ORDER_EXECUTION_EXECUTED,
    RUSSIAN_PAYMENT_ORDER_FINGERPRINT_VERSION,
    RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_PARSED,
    RUSSIAN_PAYMENT_ORDER_PARSER_VERSION,
)


CONTRACT_NAME = "ff_pool_business_documents_v1"
WORKFLOW_CONTRACT = "ff_document_workflow_v1"
REQUESTS_TABLE = "sheet_vitrina_v1_ff_pool_document_requests"
ALIASES_TABLE = "sheet_vitrina_v1_ff_pool_document_request_aliases"
DOCUMENTS_TABLE = "sheet_vitrina_v1_ff_pool_documents"
DOCUMENT_LINES_TABLE = "sheet_vitrina_v1_ff_pool_document_lines"
EXPENSE_LINES_TABLE = "sheet_vitrina_v1_ff_pool_document_expense_lines"
DOCUMENT_RELATIONS_TABLE = "sheet_vitrina_v1_ff_pool_document_relations"
WORKFLOW_EVENTS_TABLE = "sheet_vitrina_v1_ff_workflow_events"
GUIDED_REPLAYS_TABLE = "sheet_vitrina_v1_ff_guided_acceptance_replays"
GUIDED_RECOVERIES_TABLE = "sheet_vitrina_v1_ff_guided_acceptance_recoveries"
OVERHEAD_PAYMENT_EVIDENCE_TABLE = "sheet_vitrina_v1_ff_pool_overhead_payment_evidence"
TARGETED_RECALC_QUEUE_TABLE = "sheet_vitrina_v1_warehouse_targeted_recalc_queue"

WORKFLOW_STATES = (
    "accepted",
    "processing",
    "blocked",
    "ready",
    "posted",
    "replay",
    "complete",
    "error",
)
DOCUMENT_KINDS = (
    "facility_pool_opening",
    "china_acceptance",
    "transfer_root",
    "transfer_shipment",
    "transfer_receipt",
    "transfer_loss",
    "transfer_discrepancy",
    "transfer_cancellation",
    "pool_reallocation",
    "pool_inventory",
    "inventory_surplus",
    "inventory_shortage",
    "pool_overhead",
    "correction",
    "storno",
    "late_expense",
)
RELATION_TYPES = (
    "shipment_of",
    "receipt_of",
    "loss_of",
    "discrepancy_of",
    "cancellation_of",
    "inventory_surplus_of",
    "inventory_shortage_of",
    "correction_of",
    "storno_of",
    "late_expense_for",
)
RELATION_CHILD_KINDS = {
    "shipment_of": "transfer_shipment",
    "receipt_of": "transfer_receipt",
    "loss_of": "transfer_loss",
    "discrepancy_of": "transfer_discrepancy",
    "cancellation_of": "transfer_cancellation",
    "inventory_surplus_of": "inventory_surplus",
    "inventory_shortage_of": "inventory_shortage",
    "correction_of": "correction",
    "storno_of": "storno",
    "late_expense_for": "late_expense",
}
STAGE1_RELATION_TYPES = frozenset({"correction_of", "storno_of", "late_expense_for"})
POOLS = ("FBS", "FBO")
ZERO = Decimal("0")
POST_RETRY_LIMIT = 4
GUIDED_BUSINESS_EFFECT_CONTRACT = "ff_guided_acceptance_business_effect_v1"
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,119}")
MAX_SQLITE_INTEGER = 9_223_372_036_854_775_807


class FfPoolDocumentError(ValueError):
    """Machine-readable fail-closed domain error."""

    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def ensure_ff_pool_document_schema(conn: sqlite3.Connection) -> None:
    """Create only additive empty Stage 2 tables, indexes and guards."""

    ensure_ff_pool_foundation_schema(conn)
    _ensure_targeted_recalc_queue_schema(conn)
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {REQUESTS_TABLE}(
            request_id TEXT PRIMARY KEY,
            request_identity TEXT NOT NULL UNIQUE,
            client_request_id TEXT NOT NULL,
            document_kind TEXT NOT NULL CHECK(document_kind IN ({_sql_values(DOCUMENT_KINDS)})),
            state TEXT NOT NULL CHECK(state IN ({_sql_values(WORKFLOW_STATES)})),
            source_system TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            idempotency_epoch INTEGER NOT NULL
                CHECK(typeof(idempotency_epoch)='integer' AND idempotency_epoch > 0),
            actor TEXT NOT NULL,
            business_date TEXT NOT NULL
                CHECK(length(business_date)=10 AND date(business_date)=business_date),
            source_filename TEXT NOT NULL DEFAULT '',
            source_content_type TEXT NOT NULL DEFAULT '',
            source_sha256 TEXT NOT NULL DEFAULT '',
            source_file_blob BLOB,
            template_fingerprint TEXT NOT NULL DEFAULT '',
            request_payload_json TEXT NOT NULL,
            preview_manifest_json TEXT NOT NULL DEFAULT '{{}}',
            posted_manifest_sha256 TEXT NOT NULL DEFAULT '',
            posted_document_id TEXT NOT NULL DEFAULT '',
            recovery_operation_id TEXT NOT NULL DEFAULT '',
            accepted_at TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT '',
            ready_at TEXT NOT NULL DEFAULT '',
            posted_at TEXT NOT NULL DEFAULT '',
            replay_at TEXT NOT NULL DEFAULT '',
            completed_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            error_code TEXT NOT NULL DEFAULT '',
            error_details_json TEXT NOT NULL DEFAULT 'null',
            CHECK(length(trim(request_id)) BETWEEN 1 AND 120),
            CHECK(length(trim(request_identity)) BETWEEN 1 AND 80),
            CHECK(length(trim(source_system)) BETWEEN 1 AND 80),
            CHECK(length(trim(source_type)) BETWEEN 1 AND 80),
            CHECK(length(trim(source_id)) BETWEEN 1 AND 240),
            CHECK(length(trim(source_revision)) BETWEEN 1 AND 240),
            CHECK(length(trim(actor)) BETWEEN 1 AND 160)
        );
        CREATE INDEX IF NOT EXISTS ff_pool_document_requests_by_state_time
        ON {REQUESTS_TABLE}(state,updated_at,request_id);
        CREATE INDEX IF NOT EXISTS ff_pool_document_requests_by_source
        ON {REQUESTS_TABLE}(source_system,source_type,source_id,idempotency_epoch);
        CREATE UNIQUE INDEX IF NOT EXISTS ff_pool_document_requests_external_revision
        ON {REQUESTS_TABLE}(
            source_system,source_type,source_id,source_revision,idempotency_epoch
        );

        CREATE TABLE IF NOT EXISTS {ALIASES_TABLE}(
            client_request_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL REFERENCES {REQUESTS_TABLE}(request_id),
            request_identity TEXT NOT NULL,
            accepted_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ff_pool_document_aliases_by_request
        ON {ALIASES_TABLE}(request_id,accepted_at,client_request_id);

        CREATE TABLE IF NOT EXISTS {DOCUMENTS_TABLE}(
            document_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL REFERENCES {REQUESTS_TABLE}(request_id),
            document_role TEXT NOT NULL,
            document_kind TEXT NOT NULL CHECK(document_kind IN ({_sql_values(DOCUMENT_KINDS)})),
            root_document_id TEXT NOT NULL,
            operation_id TEXT NOT NULL UNIQUE REFERENCES {OPERATIONS_TABLE}(operation_id),
            source_system TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            idempotency_epoch INTEGER NOT NULL
                CHECK(typeof(idempotency_epoch)='integer' AND idempotency_epoch > 0),
            actor TEXT NOT NULL,
            business_date TEXT NOT NULL
                CHECK(length(business_date)=10 AND date(business_date)=business_date),
            source_filename TEXT NOT NULL DEFAULT '',
            source_content_type TEXT NOT NULL DEFAULT '',
            source_sha256 TEXT NOT NULL DEFAULT '',
            template_fingerprint TEXT NOT NULL DEFAULT '',
            posted_manifest_sha256 TEXT NOT NULL,
            posted_manifest_json TEXT NOT NULL,
            posted_at TEXT NOT NULL
                CHECK(substr(posted_at,-1,1)='Z' AND julianday(posted_at) IS NOT NULL),
            UNIQUE(request_id,document_role),
            CHECK(length(trim(document_id)) BETWEEN 1 AND 120),
            CHECK(length(trim(root_document_id)) BETWEEN 1 AND 120),
            CHECK(length(trim(posted_manifest_sha256)) BETWEEN 1 AND 80)
        );
        CREATE INDEX IF NOT EXISTS ff_pool_documents_by_root_time
        ON {DOCUMENTS_TABLE}(root_document_id,posted_at,document_id);
        CREATE INDEX IF NOT EXISTS ff_pool_documents_by_kind_date
        ON {DOCUMENTS_TABLE}(document_kind,business_date,document_id);
        CREATE INDEX IF NOT EXISTS ff_pool_documents_by_source
        ON {DOCUMENTS_TABLE}(source_system,source_type,source_id,idempotency_epoch);

        CREATE TABLE IF NOT EXISTS {DOCUMENT_LINES_TABLE}(
            document_id TEXT NOT NULL REFERENCES {DOCUMENTS_TABLE}(document_id),
            line_no INTEGER NOT NULL
                CHECK(typeof(line_no)='integer' AND line_no > 0),
            root_document_id TEXT NOT NULL,
            line_role TEXT NOT NULL,
            facility_id TEXT REFERENCES {FACILITIES_TABLE}(facility_id),
            pool TEXT CHECK(pool IS NULL OR pool IN ('FBS','FBO')),
            nm_id INTEGER NOT NULL
                CHECK(typeof(nm_id)='integer' AND nm_id > 0),
            quantity INTEGER NOT NULL
                CHECK(typeof(quantity)='integer' AND quantity >= 0),
            capital_rub TEXT NOT NULL CHECK({_decimal_check('capital_rub')}),
            expense_rub TEXT NOT NULL CHECK({_decimal_check('expense_rub')}),
            metadata_json TEXT NOT NULL DEFAULT '{{}}',
            PRIMARY KEY(document_id,line_no)
        );
        CREATE INDEX IF NOT EXISTS ff_pool_document_lines_by_root_nm
        ON {DOCUMENT_LINES_TABLE}(root_document_id,nm_id,line_role,document_id,line_no);
        CREATE INDEX IF NOT EXISTS ff_pool_document_lines_by_location
        ON {DOCUMENT_LINES_TABLE}(facility_id,pool,nm_id,document_id,line_no);

        CREATE TABLE IF NOT EXISTS {EXPENSE_LINES_TABLE}(
            document_id TEXT NOT NULL REFERENCES {DOCUMENTS_TABLE}(document_id),
            expense_line_no INTEGER NOT NULL
                CHECK(typeof(expense_line_no)='integer' AND expense_line_no > 0),
            amount_rub TEXT NOT NULL CHECK({_decimal_check('amount_rub')}),
            basis TEXT NOT NULL,
            source_file_sha256 TEXT NOT NULL DEFAULT '',
            source_filename TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{{}}',
            PRIMARY KEY(document_id,expense_line_no),
            CHECK(length(trim(basis)) BETWEEN 1 AND 1000)
        );

        CREATE TABLE IF NOT EXISTS {DOCUMENT_RELATIONS_TABLE}(
            parent_document_id TEXT NOT NULL REFERENCES {DOCUMENTS_TABLE}(document_id),
            child_document_id TEXT NOT NULL REFERENCES {DOCUMENTS_TABLE}(document_id),
            root_document_id TEXT NOT NULL,
            relation_type TEXT NOT NULL CHECK(relation_type IN ({_sql_values(RELATION_TYPES)})),
            created_at TEXT NOT NULL
                CHECK(substr(created_at,-1,1)='Z' AND julianday(created_at) IS NOT NULL),
            PRIMARY KEY(parent_document_id,child_document_id,relation_type),
            UNIQUE(child_document_id),
            CHECK(parent_document_id <> child_document_id)
        );
        CREATE INDEX IF NOT EXISTS ff_pool_document_relations_by_root_parent
        ON {DOCUMENT_RELATIONS_TABLE}(root_document_id,parent_document_id,relation_type,child_document_id);

        CREATE TABLE IF NOT EXISTS {WORKFLOW_EVENTS_TABLE}(
            event_id TEXT PRIMARY KEY,
            action_type TEXT NOT NULL,
            identity TEXT NOT NULL,
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            duration_ms INTEGER NOT NULL,
            details_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ff_workflow_events_identity
        ON {WORKFLOW_EVENTS_TABLE}(action_type,identity,occurred_at);

        CREATE TABLE IF NOT EXISTS {GUIDED_REPLAYS_TABLE}(
            request_id TEXT PRIMARY KEY REFERENCES {REQUESTS_TABLE}(request_id),
            document_id TEXT NOT NULL UNIQUE REFERENCES {DOCUMENTS_TABLE}(document_id),
            shipment_id TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            legacy_operation_id TEXT NOT NULL UNIQUE,
            cost_layer_id TEXT NOT NULL,
            cost_inputs_hash TEXT NOT NULL,
            affected_nm_ids_json TEXT NOT NULL,
            accepted_quantities_json TEXT NOT NULL,
            capital_normalization_json TEXT NOT NULL,
            replayed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ff_guided_replays_by_shipment
        ON {GUIDED_REPLAYS_TABLE}(shipment_id,replayed_at,request_id);

        CREATE TABLE IF NOT EXISTS {GUIDED_RECOVERIES_TABLE}(
            recovery_request_id TEXT PRIMARY KEY REFERENCES {REQUESTS_TABLE}(request_id),
            target_request_id TEXT NOT NULL UNIQUE REFERENCES {GUIDED_REPLAYS_TABLE}(request_id),
            target_document_id TEXT NOT NULL,
            shipment_id TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            affected_nm_ids_json TEXT NOT NULL,
            recovered_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ff_guided_recoveries_by_shipment
        ON {GUIDED_RECOVERIES_TABLE}(shipment_id,recovered_at,recovery_request_id);

        CREATE TABLE IF NOT EXISTS {OVERHEAD_PAYMENT_EVIDENCE_TABLE}(
            payment_fingerprint TEXT PRIMARY KEY,
            fingerprint_version TEXT NOT NULL,
            request_id TEXT NOT NULL UNIQUE REFERENCES {REQUESTS_TABLE}(request_id),
            file_sha256 TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            normalized_parser_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            CHECK(payment_fingerprint GLOB 'sha256:[0-9a-f]*' AND length(payment_fingerprint)=71),
            CHECK(file_sha256 GLOB 'sha256:[0-9a-f]*' AND length(file_sha256)=71)
        );
        CREATE INDEX IF NOT EXISTS ff_pool_overhead_payment_by_request
        ON {OVERHEAD_PAYMENT_EVIDENCE_TABLE}(request_id);

        CREATE TRIGGER IF NOT EXISTS ff_pool_documents_no_update
        BEFORE UPDATE ON {DOCUMENTS_TABLE}
        BEGIN SELECT RAISE(ABORT,'posted FF pool document is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_documents_no_delete
        BEFORE DELETE ON {DOCUMENTS_TABLE}
        BEGIN SELECT RAISE(ABORT,'posted FF pool document is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_document_lines_no_update
        BEFORE UPDATE ON {DOCUMENT_LINES_TABLE}
        BEGIN SELECT RAISE(ABORT,'posted FF pool document line is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_document_lines_no_delete
        BEFORE DELETE ON {DOCUMENT_LINES_TABLE}
        BEGIN SELECT RAISE(ABORT,'posted FF pool document line is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_expense_lines_positive
        BEFORE INSERT ON {EXPENSE_LINES_TABLE}
        WHEN CAST(NEW.amount_rub AS NUMERIC) <= 0
        BEGIN SELECT RAISE(ABORT,'FF pool expense must be positive'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_expense_lines_no_update
        BEFORE UPDATE ON {EXPENSE_LINES_TABLE}
        BEGIN SELECT RAISE(ABORT,'posted FF pool expense line is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_expense_lines_no_delete
        BEFORE DELETE ON {EXPENSE_LINES_TABLE}
        BEGIN SELECT RAISE(ABORT,'posted FF pool expense line is append-only'); END;

        CREATE TRIGGER IF NOT EXISTS ff_pool_document_relation_forward
        BEFORE INSERT ON {DOCUMENT_RELATIONS_TABLE}
        BEGIN
            SELECT CASE WHEN (
                SELECT julianday(posted_at) FROM {DOCUMENTS_TABLE}
                WHERE document_id=NEW.parent_document_id
            ) > (
                SELECT julianday(posted_at) FROM {DOCUMENTS_TABLE}
                WHERE document_id=NEW.child_document_id
            ) THEN RAISE(ABORT,'FF pool document relation must point forward') END;
            SELECT CASE WHEN NOT EXISTS(
                SELECT 1 FROM {DOCUMENTS_TABLE} AS parent
                JOIN {DOCUMENTS_TABLE} AS child
                  ON child.document_id=NEW.child_document_id
                WHERE parent.document_id=NEW.parent_document_id
                  AND parent.root_document_id=NEW.root_document_id
                  AND child.root_document_id=NEW.root_document_id
            ) THEN RAISE(ABORT,'FF pool relation root mismatch') END;
            SELECT CASE WHEN NOT EXISTS(
                SELECT 1 FROM {DOCUMENTS_TABLE} AS child
                WHERE child.document_id=NEW.child_document_id
                  AND child.document_kind=CASE NEW.relation_type
                    WHEN 'shipment_of' THEN 'transfer_shipment'
                    WHEN 'receipt_of' THEN 'transfer_receipt'
                    WHEN 'loss_of' THEN 'transfer_loss'
                    WHEN 'discrepancy_of' THEN 'transfer_discrepancy'
                    WHEN 'cancellation_of' THEN 'transfer_cancellation'
                    WHEN 'inventory_surplus_of' THEN 'inventory_surplus'
                    WHEN 'inventory_shortage_of' THEN 'inventory_shortage'
                    WHEN 'correction_of' THEN 'correction'
                    WHEN 'storno_of' THEN 'storno'
                    WHEN 'late_expense_for' THEN 'late_expense'
                  END
            ) THEN RAISE(ABORT,'FF pool relation child type mismatch') END;
        END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_document_relation_no_cycle
        BEFORE INSERT ON {DOCUMENT_RELATIONS_TABLE}
        BEGIN
            SELECT CASE WHEN EXISTS(
                WITH RECURSIVE descendants(document_id) AS (
                    SELECT child_document_id FROM {DOCUMENT_RELATIONS_TABLE}
                    WHERE parent_document_id=NEW.child_document_id
                    UNION
                    SELECT relation.child_document_id
                    FROM {DOCUMENT_RELATIONS_TABLE} AS relation
                    JOIN descendants
                      ON relation.parent_document_id=descendants.document_id
                )
                SELECT 1 FROM descendants WHERE document_id=NEW.parent_document_id
            ) THEN RAISE(ABORT,'FF pool document relation cycle') END;
        END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_document_relations_no_update
        BEFORE UPDATE ON {DOCUMENT_RELATIONS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FF pool document relation is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_document_relations_no_delete
        BEFORE DELETE ON {DOCUMENT_RELATIONS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FF pool document relation is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS ff_guided_replays_no_update
        BEFORE UPDATE ON {GUIDED_REPLAYS_TABLE}
        BEGIN SELECT RAISE(ABORT,'guided acceptance replay is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ff_guided_replays_no_delete
        BEFORE DELETE ON {GUIDED_REPLAYS_TABLE}
        BEGIN SELECT RAISE(ABORT,'guided acceptance replay is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS ff_guided_recoveries_no_update
        BEFORE UPDATE ON {GUIDED_RECOVERIES_TABLE}
        BEGIN SELECT RAISE(ABORT,'guided acceptance recovery is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ff_guided_recoveries_no_delete
        BEFORE DELETE ON {GUIDED_RECOVERIES_TABLE}
        BEGIN SELECT RAISE(ABORT,'guided acceptance recovery is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_overhead_payment_evidence_no_update
        BEFORE UPDATE ON {OVERHEAD_PAYMENT_EVIDENCE_TABLE}
        BEGIN SELECT RAISE(ABORT,'overhead payment evidence is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_overhead_payment_evidence_no_delete
        BEFORE DELETE ON {OVERHEAD_PAYMENT_EVIDENCE_TABLE}
        BEGIN SELECT RAISE(ABORT,'overhead payment evidence is append-only'); END;
        """
    )


def _ensure_targeted_recalc_queue_schema(conn: sqlite3.Connection) -> None:
    """Keep the canonical queue writable inside the document commit."""

    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {TARGETED_RECALC_QUEUE_TABLE}(
            queue_id TEXT PRIMARY KEY,stable_source_id TEXT NOT NULL,
            source_revision TEXT NOT NULL,effective_date TEXT NOT NULL,
            affected_nm_ids_json TEXT NOT NULL,status TEXT NOT NULL,
            requested_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,error TEXT,
            economics_status TEXT NOT NULL DEFAULT '',economics_started_at TEXT,
            economics_finished_at TEXT,economics_error TEXT,
            finance_status TEXT NOT NULL DEFAULT '',finance_source_fingerprint TEXT NOT NULL DEFAULT '',
            finance_started_at TEXT,finance_finished_at TEXT,finance_error TEXT,
            UNIQUE(stable_source_id,source_revision)
        )"""
    )
    existing = {
        str(row[1])
        for row in conn.execute(
            f"PRAGMA table_info({TARGETED_RECALC_QUEUE_TABLE})"
        ).fetchall()
    }
    for name, definition in {
        "economics_status": "TEXT NOT NULL DEFAULT ''",
        "economics_started_at": "TEXT",
        "economics_finished_at": "TEXT",
        "economics_error": "TEXT",
        "finance_status": "TEXT NOT NULL DEFAULT ''",
        "finance_source_fingerprint": "TEXT NOT NULL DEFAULT ''",
        "finance_started_at": "TEXT",
        "finance_finished_at": "TEXT",
        "finance_error": "TEXT",
    }.items():
        if name not in existing:
            conn.execute(
                f"ALTER TABLE {TARGETED_RECALC_QUEUE_TABLE} "
                f"ADD COLUMN {name} {definition}"
            )


class FfPoolDocumentService:
    """Synchronous domain service with durable restart/idempotency state."""

    def __init__(
        self,
        *,
        db_path: Path,
        runtime_dir: Path,
        timestamp_factory: Any | None = None,
        resume: bool = True,
    ) -> None:
        self.db_path = Path(db_path)
        self.runtime_dir = Path(runtime_dir)
        self.timestamp_factory = timestamp_factory or _utc_now
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            ensure_ff_pool_document_schema(conn)
            now = self._now()
            conn.execute(
                f"UPDATE {REQUESTS_TABLE} SET state='accepted',started_at='',updated_at=? "
                "WHERE state='processing'",
                (now,),
            )
            conn.commit()
        if resume:
            self.resume_incomplete()

    def accept_preview(
        self,
        *,
        identity: DocumentIdentity,
        document_kind: str,
        manifest: Mapping[str, Any],
        source_bytes: bytes = b"",
        source_filename: str = "",
        source_content_type: str = "",
        template_fingerprint: str = "",
    ) -> dict[str, Any]:
        """Persist accepted evidence, validate it and produce a durable preview."""

        normalized_kind = _document_kind(document_kind)
        normalized_manifest = _json_object(manifest)
        canonical_request_id, inserted = self._accept(
            identity=identity,
            document_kind=normalized_kind,
            manifest=normalized_manifest,
            source_bytes=source_bytes,
            source_filename=source_filename,
            source_content_type=source_content_type,
            template_fingerprint=template_fingerprint,
        )
        if inserted:
            self.process_request(canonical_request_id)
        elif self._retry_legacy_money_block(canonical_request_id):
            self.process_request(canonical_request_id)
        elif self._retry_guided_source_revision_contract_block(
            canonical_request_id,
            expected_source_revision=str(identity.source_revision),
        ):
            self.process_request(canonical_request_id)
        elif self._retry_guided_parity_block(canonical_request_id):
            self.process_request(canonical_request_id)
        elif self._retry_ready_guided_plan_preview(canonical_request_id):
            self.process_request(canonical_request_id)
        elif (
            normalized_kind == "pool_overhead"
            and self._retry_pool_overhead_preview(canonical_request_id)
        ):
            self.process_request(canonical_request_id)
        return {
            **self.status(request_id=identity.request_id),
            "idempotent": not inserted,
            "payment_duplicate": bool(
                normalized_kind == "pool_overhead"
                and _payment_fingerprint_from_manifest(normalized_manifest)
                and not inserted
            ),
        }

    def _retry_pool_overhead_preview(self, request_id: str) -> bool:
        """Refresh one existing non-posted overhead after a transient basis blocker."""

        retryable_blockers = {
            "overhead_positive_quantity_required",
            "overhead_positive_quantity_cost_basis_missing",
            "overhead_preview_stale",
        }
        now = self._now()
        with _connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT document_kind,state,error_code FROM {REQUESTS_TABLE} WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if (
                row is None
                or str(row["document_kind"]) != "pool_overhead"
                or (
                    str(row["state"]) != "ready"
                    and not (
                        str(row["state"]) == "blocked"
                        and str(row["error_code"]) in retryable_blockers
                    )
                )
            ):
                conn.rollback()
                return False
            changed = conn.execute(
                f"UPDATE {REQUESTS_TABLE} SET state='accepted',started_at='',ready_at='',"
                "updated_at=?,error_code='',error_details_json='null' "
                "WHERE request_id=? AND state IN ('ready','blocked')",
                (now, request_id),
            ).rowcount
            if changed:
                self._event(
                    conn,
                    request_id=request_id,
                    stage="overhead_preview_revalidation",
                    status="complete",
                )
            conn.commit()
            return bool(changed)

    def _retry_legacy_money_block(self, request_id: str) -> bool:
        """Revalidate the one pre-fix fail-closed state without creating a duplicate.

        The original immutable workbook and source revision remain the request
        identity.  Only the historical fractional-kopeck rejection is eligible;
        every other blocked/error state remains terminal and unchanged.
        """

        now = self._now()
        with _connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT document_kind,state,error_code,request_payload_json "
                f"FROM {REQUESTS_TABLE} WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if (
                row is None
                or str(row["document_kind"]) != "china_acceptance"
                or str(row["state"]) != "blocked"
                or str(row["error_code"]) != "money_minor_unit_required"
            ):
                conn.rollback()
                return False
            manifest = _json_object(_loads(row["request_payload_json"], {}))
            _validate_manifest("china_acceptance", manifest)
            # This bounded validation proves the replacement monetary boundary
            # accepts the same immutable evidence before reopening the request.
            _normalize_acceptance_capital(manifest.get("allocations") or [])
            changed = conn.execute(
                f"UPDATE {REQUESTS_TABLE} SET state='accepted',started_at='',ready_at='',"
                "updated_at=?,error_code='',error_details_json='null' "
                "WHERE request_id=? AND state='blocked' AND error_code='money_minor_unit_required'",
                (now, request_id),
            ).rowcount
            if changed:
                self._event(
                    conn,
                    request_id=request_id,
                    stage="money_boundary_revalidation",
                    status="complete",
                    details={"policy": "header_half_up_then_largest_remainder"},
                )
            conn.commit()
            return bool(changed)

    def _retry_guided_source_revision_contract_block(
        self,
        request_id: str,
        *,
        expected_source_revision: str,
    ) -> bool:
        """Reprocess the one exact request blocked by the raw/combined bug.

        The HTTP surface binds a guided request to both the raw supplier
        revision carried by the workbook and the workbook bytes.  The first
        posting-plan readiness release compared that combined revision to the
        current raw supplier revision and therefore blocked every otherwise
        current request.  Reopening remains fail closed: the caller must have
        reproduced the same immutable request revision, and the stored value
        must independently recompute from the manifest raw revision plus the
        stored workbook digest.  Processing then rechecks the live raw source
        again before making the request ready.
        """

        now = self._now()
        with _connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT document_kind,source_type,state,error_code,source_revision,"
                f"source_sha256,request_payload_json FROM {REQUESTS_TABLE} "
                "WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if (
                row is None
                or str(row["document_kind"]) != "china_acceptance"
                or str(row["source_type"]) != "china_acceptance_workbook"
                or str(row["state"]) != "blocked"
                or str(row["error_code"]) != "supplier_source_revision_changed"
            ):
                conn.rollback()
                return False
            manifest = _json_object(_loads(row["request_payload_json"], {}))
            _validate_manifest("china_acceptance", manifest)
            stored_revision = str(row["source_revision"] or "")
            recomputed_revision = _guided_request_source_revision(
                supplier_source_revision=str(manifest.get("source_revision") or ""),
                source_sha256=str(row["source_sha256"] or ""),
            )
            if (
                stored_revision != str(expected_source_revision or "")
                or stored_revision != recomputed_revision
            ):
                conn.rollback()
                return False
            changed = conn.execute(
                f"UPDATE {REQUESTS_TABLE} SET state='accepted',started_at='',ready_at='',"
                "updated_at=?,error_code='',error_details_json='null' "
                "WHERE request_id=? AND state='blocked' "
                "AND error_code='supplier_source_revision_changed' AND source_revision=?",
                (now, request_id, stored_revision),
            ).rowcount
            if changed:
                self._event(
                    conn,
                    request_id=request_id,
                    stage="source_revision_contract_revalidation",
                    status="complete",
                    details={"immutable_request_reused": True},
                )
            conn.commit()
            return bool(changed)

    def _retry_ready_guided_plan_preview(self, request_id: str) -> bool:
        """Upgrade one identical ready guided request to the stronger preview.

        Requests made before posting-plan validation did not prove that every
        current aggregate/pool/source guard was constructible.  An exact
        idempotent retry may reprocess that same immutable request in place;
        posted requests and all blocked/error states remain untouched.
        """

        now = self._now()
        with _connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT document_kind,source_type,state,preview_manifest_json "
                f"FROM {REQUESTS_TABLE} WHERE request_id=?",
                (request_id,),
            ).fetchone()
            preview = (
                _json_object(_loads(row["preview_manifest_json"], {}))
                if row is not None
                else {}
            )
            readiness = _json_object(preview.get("posting_plan_preview") or {})
            if (
                row is None
                or str(row["document_kind"]) != "china_acceptance"
                or str(row["source_type"]) != "china_acceptance_workbook"
                or str(row["state"]) != "ready"
                or (
                    bool(readiness.get("confirm_plan_ready"))
                    and readiness.get("business_effect_contract")
                    == GUIDED_BUSINESS_EFFECT_CONTRACT
                    and re.fullmatch(
                        r"sha256:[0-9a-f]{64}",
                        str(readiness.get("business_effect_sha256") or ""),
                    )
                )
            ):
                conn.rollback()
                return False
            changed = conn.execute(
                f"UPDATE {REQUESTS_TABLE} SET state='accepted',started_at='',ready_at='',"
                "updated_at=?,error_code='',error_details_json='null' "
                "WHERE request_id=? AND state='ready'",
                (now, request_id),
            ).rowcount
            if changed:
                self._event(
                    conn,
                    request_id=request_id,
                    stage="posting_plan_preview_upgrade",
                    status="complete",
                    details={"immutable_request_reused": True},
                )
            conn.commit()
            return bool(changed)

    def _retry_guided_parity_block(self, request_id: str) -> bool:
        """Reopen one identical request after current global parity is restored.

        A failed confirm never creates a document or movement because the
        aggregate/detail parity error rolls back the business transaction.  A
        later exact retry may reuse that immutable request only after the
        current active aggregate once again equals the facility/pool detail.
        """

        now = self._now()
        with _connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT document_kind,source_type,state,error_code "
                f"FROM {REQUESTS_TABLE} WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if (
                row is None
                or str(row["document_kind"]) != "china_acceptance"
                or str(row["source_type"]) != "china_acceptance_workbook"
                or str(row["state"]) != "blocked"
                or str(row["error_code"])
                not in {
                    "guided_acceptance_parity_failed",
                    "guided_acceptance_parity_not_current",
                    "guided_acceptance_posting_plan_drift",
                }
            ):
                conn.rollback()
                return False
            try:
                proof = _guided_current_aggregate_parity_proof(conn)
            except FfPoolDocumentError as exc:
                if exc.code != "guided_acceptance_parity_not_current":
                    raise
                conn.rollback()
                return False
            changed = conn.execute(
                f"UPDATE {REQUESTS_TABLE} SET state='accepted',started_at='',ready_at='',"
                "updated_at=?,error_code='',error_details_json='null' "
                "WHERE request_id=? AND state='blocked' AND error_code=?",
                (now, request_id, str(row["error_code"])),
            ).rowcount
            if changed:
                self._event(
                    conn,
                    request_id=request_id,
                    stage="aggregate_parity_revalidation",
                    status="complete",
                    details=proof,
                )
            conn.commit()
            return bool(changed)

    def active_facilities(self) -> list[dict[str, Any]]:
        with _connect(self.db_path, query_only=True) as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"SELECT facility_id,code,name,active,display_timezone FROM {FACILITIES_TABLE} "
                    "WHERE active=1 ORDER BY code,facility_id"
                ).fetchall()
            ]

    def generate_china_acceptance_template(
        self,
        *,
        shipment_lines: Iterable[Mapping[str, Any]],
        source_revision: str,
        selected_facility_id: str = "",
    ) -> bytes:
        from packages.application.ff_pool_documents_xlsx import (
            generate_china_acceptance_workbook,
        )

        return generate_china_acceptance_workbook(
            facilities=self.active_facilities(),
            shipment_lines=shipment_lines,
            source_revision=source_revision,
            selected_facility_id=selected_facility_id,
        )

    def generate_inventory_template(
        self,
        *,
        facility_id: str,
        scope: str,
        catalog: Iterable[Mapping[str, Any]],
        source_revision: str,
        targets: Mapping[tuple[int, str], Any] | None = None,
    ) -> bytes:
        from packages.application.ff_pool_documents_xlsx import generate_inventory_workbook

        return generate_inventory_workbook(
            facilities=self.active_facilities(),
            facility_id=facility_id,
            scope=scope,
            catalog=catalog,
            source_revision=source_revision,
            targets=targets,
        )

    def preview_china_acceptance_workbook(
        self,
        *,
        identity: DocumentIdentity,
        source_bytes: bytes,
        source_filename: str,
        source_content_type: str,
        shipment_lines: Iterable[Mapping[str, Any]],
        expenses: Iterable[Mapping[str, Any] | ExpenseLine] = (),
        template_source_revision: str = "",
    ) -> dict[str, Any]:
        from packages.application.ff_pool_documents_xlsx import (
            FfPoolXlsxError,
            parse_china_acceptance_workbook,
        )

        lines = [dict(item) for item in shipment_lines]
        try:
            manifest = parse_china_acceptance_workbook(
                source_bytes,
                filename=source_filename,
                content_type=source_content_type,
                facilities=self.active_facilities(),
                shipment_lines=lines,
                source_revision=str(template_source_revision or identity.source_revision),
            )
            manifest["expenses"] = [
                asdict(item) if isinstance(item, ExpenseLine) else dict(item)
                for item in expenses
            ]
            return self.accept_preview(
                identity=identity,
                document_kind="china_acceptance",
                manifest=manifest,
                source_bytes=source_bytes,
                source_filename=source_filename,
                source_content_type=source_content_type,
                template_fingerprint=str(manifest["template_fingerprint"]),
            )
        except FfPoolXlsxError as exc:
            return self.accept_blocked(
                identity=identity,
                document_kind="china_acceptance",
                source_bytes=source_bytes,
                source_filename=source_filename,
                source_content_type=source_content_type,
                template_fingerprint="",
                error_code=exc.code,
                error_details=exc.details,
            )

    def preview_inventory_workbook(
        self,
        *,
        identity: DocumentIdentity,
        source_bytes: bytes,
        source_filename: str,
        source_content_type: str,
        catalog: Iterable[Mapping[str, Any]],
        cost_basis_by_nm: Mapping[Any, Any] | None = None,
    ) -> dict[str, Any]:
        from packages.application.ff_pool_documents_xlsx import (
            FfPoolXlsxError,
            parse_inventory_workbook,
        )

        rows = [dict(item) for item in catalog]
        try:
            manifest = parse_inventory_workbook(
                source_bytes,
                filename=source_filename,
                content_type=source_content_type,
                facilities=self.active_facilities(),
                catalog=rows,
                source_revision=identity.source_revision,
            )
            manifest["cost_basis_by_nm"] = dict(cost_basis_by_nm or {})
            return self.accept_preview(
                identity=identity,
                document_kind="pool_inventory",
                manifest=manifest,
                source_bytes=source_bytes,
                source_filename=source_filename,
                source_content_type=source_content_type,
                template_fingerprint=str(manifest["template_fingerprint"]),
            )
        except FfPoolXlsxError as exc:
            return self.accept_blocked(
                identity=identity,
                document_kind="pool_inventory",
                source_bytes=source_bytes,
                source_filename=source_filename,
                source_content_type=source_content_type,
                template_fingerprint="",
                error_code=exc.code,
                error_details=exc.details,
            )

    def accept_blocked(
        self,
        *,
        identity: DocumentIdentity,
        document_kind: str,
        source_bytes: bytes,
        source_filename: str,
        source_content_type: str,
        template_fingerprint: str,
        error_code: str,
        error_details: Any,
        manifest: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist rejected original bytes; a blocked preview can never post."""

        canonical_request_id, inserted = self._accept(
            identity=identity,
            document_kind=_document_kind(document_kind),
            manifest=_json_object(manifest or {}),
            source_bytes=source_bytes,
            source_filename=source_filename,
            source_content_type=source_content_type,
            template_fingerprint=template_fingerprint,
        )
        if inserted:
            now = self._now()
            with _connect(self.db_path) as conn:
                conn.execute(
                    f"UPDATE {REQUESTS_TABLE} SET state='blocked',updated_at=?,"
                    "error_code=?,error_details_json=? WHERE request_id=? AND state='accepted'",
                    (now, str(error_code), _json(error_details), canonical_request_id),
                )
                self._event(
                    conn,
                    request_id=canonical_request_id,
                    stage="validation",
                    status="blocked",
                    details={"error_code": str(error_code)},
                )
                conn.commit()
        return {**self.status(request_id=identity.request_id), "idempotent": not inserted}

    def process_request(self, request_id: str) -> dict[str, Any]:
        canonical = self._resolve_request_id(request_id)
        now = self._now()
        with _connect(self.db_path) as conn:
            changed = conn.execute(
                f"UPDATE {REQUESTS_TABLE} SET state='processing',started_at=?,updated_at=?,"
                "error_code='',error_details_json='null' WHERE request_id=? AND state='accepted'",
                (now, now, canonical),
            ).rowcount
            conn.commit()
        if not changed:
            return self.status(request_id=canonical)
        try:
            with _connect(self.db_path, query_only=True) as conn:
                row = conn.execute(
                    f"SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?",
                    (canonical,),
                ).fetchone()
            if row is None:
                raise FfPoolDocumentError("request_not_found", "Document request disappeared")
            manifest = _json_object(_loads(row["request_payload_json"], {}))
            _validate_manifest(str(row["document_kind"]), manifest)
            if str(row["document_kind"]) == "china_acceptance":
                normalization = _normalize_acceptance_capital(
                    manifest.get("allocations") or []
                )
                manifest["capital_normalization"] = {
                    "policy": normalization["policy"],
                    "exact_total_rub": normalization["exact_total_rub"],
                    "canonical_total_rub": normalization["canonical_total_rub"],
                    "total_residual_rub": normalization["total_residual_rub"],
                    "residual_owner_nm_ids": normalization["residual_owner_nm_ids"],
                    "capital_cents_by_nm": normalization["capital_cents_by_nm"],
                    "normalization_residual_rub_by_nm": normalization[
                        "normalization_residual_rub_by_nm"
                    ],
                }
            if _is_guided_china_request(row):
                try:
                    with _connect(self.db_path, query_only=True) as conn:
                        epoch = _writer_epoch(conn)
                        _require_guided_acceptance_activation(conn)
                        plan = _build_posting_plan(
                            conn,
                            request=row,
                            manifest=manifest,
                            epoch=epoch,
                        )
                except FfPoolDocumentError as exc:
                    if exc.code not in {
                        "feature_writer_disabled",
                        "guided_acceptance_opening_not_active",
                    }:
                        raise
                    manifest["posting_plan_preview"] = {
                        "confirm_plan_ready": False,
                        "business_mutation_applied": False,
                        "blocker_code": exc.code,
                    }
                else:
                    from packages.application.ff_pool_surfaces import FfPoolSurface

                    _shipment, _lines, current_source_revision = FfPoolSurface(
                        db_path=self.db_path,
                        runtime_dir=self.runtime_dir,
                        timestamp_factory=self.timestamp_factory,
                    ).supplier_shipment_source(str(row["source_id"]))
                    manifest_source_revision = str(
                        manifest.get("source_revision") or ""
                    )
                    expected_request_revision = _guided_request_source_revision(
                        supplier_source_revision=manifest_source_revision,
                        source_sha256=str(row["source_sha256"] or ""),
                    )
                    if (
                        current_source_revision != manifest_source_revision
                        or str(row["source_revision"]) != expected_request_revision
                    ):
                        raise FfPoolDocumentError(
                            "supplier_source_revision_changed",
                            "Supplier composition or cost inputs changed during preview",
                        )
                    manifest["posting_plan_preview"] = _posting_plan_preview(
                        plan=plan,
                        epoch=epoch,
                    )
            elif str(row["document_kind"]) == "pool_overhead":
                with _connect(self.db_path, query_only=True) as conn:
                    epoch = _writer_epoch(conn)
                    plan = _build_posting_plan(
                        conn,
                        request=row,
                        manifest=manifest,
                        epoch=epoch,
                    )
                manifest["posting_plan_preview"] = _pool_overhead_plan_preview(
                    plan=plan,
                    epoch=epoch,
                )
            finished = self._now()
            with _connect(self.db_path) as conn:
                updated = conn.execute(
                    f"UPDATE {REQUESTS_TABLE} SET state='ready',preview_manifest_json=?,"
                    "ready_at=?,updated_at=? WHERE request_id=? AND state='processing'",
                    (_json(manifest), finished, finished, canonical),
                ).rowcount
                if updated:
                    self._event(conn, request_id=canonical, stage="validation", status="complete")
                conn.commit()
        except FfPoolDocumentError as exc:
            self._finish_error(canonical, state="blocked", code=exc.code, details=exc.details)
        except Exception as exc:
            self._finish_error(
                canonical,
                state="error",
                code="preview_processing_failed",
                details={"error": str(exc).replace("\n", " ")[:1000]},
            )
        return self.status(request_id=canonical)

    def post(self, request_id: str) -> dict[str, Any]:
        """Post one ready request with T0/T1 recovery and bounded drift retry."""

        canonical = self._resolve_request_id(request_id)
        lock_path = self.runtime_dir / ".ff-pool-document-posting.lock"
        with lock_path.open("a+b") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                return self._post_with_retries(canonical)
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def _post_with_retries(self, canonical: str) -> dict[str, Any]:
        for attempt in range(POST_RETRY_LIMIT):
            try:
                result = self._post_once(canonical)
            except FfPoolDocumentError as exc:
                if exc.code == "concurrent_pool_balance_drift" and attempt + 1 < POST_RETRY_LIMIT:
                    continue
                self._finish_error(
                    canonical,
                    state="blocked",
                    code=exc.code,
                    details=exc.details,
                )
                return self.status(request_id=canonical)
            if result.get("state") != "error" or result.get("error", {}).get("code") != "concurrent_pool_balance_drift":
                return result
            if attempt + 1 >= POST_RETRY_LIMIT:
                return result
        return self.status(request_id=canonical)

    def status(self, *, request_id: str) -> dict[str, Any]:
        canonical = self._resolve_request_id(request_id, required=False)
        if not canonical:
            return {
                "contract_name": WORKFLOW_CONTRACT,
                "service_contract": CONTRACT_NAME,
                "state": "not_found",
                "confirm_allowed": False,
            }
        with _connect(self.db_path, query_only=True) as conn:
            row = conn.execute(
                f"SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?",
                (canonical,),
            ).fetchone()
            if row is None:
                return {
                    "contract_name": WORKFLOW_CONTRACT,
                    "service_contract": CONTRACT_NAME,
                    "state": "not_found",
                    "confirm_allowed": False,
                }
            document = None
            if str(row["posted_document_id"] or ""):
                document = conn.execute(
                    f"SELECT document_id,root_document_id,document_kind,posted_manifest_sha256,posted_at "
                    f"FROM {DOCUMENTS_TABLE} WHERE document_id=?",
                    (str(row["posted_document_id"]),),
                ).fetchone()
            posted_document_id = str(row["posted_document_id"] or "")
            publication_row = (
                conn.execute(
                    f"SELECT 1 FROM {TARGETED_RECALC_QUEUE_TABLE} "
                    "WHERE stable_source_id=? LIMIT 1",
                    (f"pool_overhead:{posted_document_id}",),
                ).fetchone()
                if posted_document_id
                else None
            )
            publication = (
                _pool_overhead_publication_state(
                    conn,
                    document_id=posted_document_id,
                )
                if publication_row is not None
                else None
            )
            preview_manifest = _loads(row["preview_manifest_json"], {})
            confirm_allowed = str(row["state"]) == "ready"
            if _is_guided_china_request(row):
                readiness = (
                    dict(preview_manifest.get("posting_plan_preview") or {})
                    if isinstance(preview_manifest, Mapping)
                    else {}
                )
                confirm_allowed = confirm_allowed and bool(
                    readiness.get("confirm_plan_ready")
                    and readiness.get("business_effect_contract")
                    == GUIDED_BUSINESS_EFFECT_CONTRACT
                    and re.fullmatch(
                        r"sha256:[0-9a-f]{64}",
                        str(readiness.get("business_effect_sha256") or ""),
                    )
                )
            return {
                "contract_name": WORKFLOW_CONTRACT,
                "service_contract": CONTRACT_NAME,
                "action_type": "facility_pool_document",
                "request_id": canonical,
                "client_request_id": str(row["client_request_id"]),
                "document_kind": str(row["document_kind"]),
                "state": str(row["state"]),
                "confirm_allowed": confirm_allowed,
                "source": {
                    "system": str(row["source_system"]),
                    "type": str(row["source_type"]),
                    "id": str(row["source_id"]),
                    "revision": str(row["source_revision"]),
                    "idempotency_epoch": int(row["idempotency_epoch"]),
                    "filename": str(row["source_filename"]),
                    "sha256": str(row["source_sha256"]),
                },
                "business_date": str(row["business_date"]),
                "actor": str(row["actor"]),
                "template_fingerprint": str(row["template_fingerprint"]),
                "preview_manifest": preview_manifest,
                "posted_manifest_sha256": str(row["posted_manifest_sha256"]),
                "document": dict(document) if document is not None else None,
                "publication": publication,
                "recovery_operation_id": str(row["recovery_operation_id"]),
                "accepted_at": str(row["accepted_at"]),
                "updated_at": str(row["updated_at"]),
                "error": {
                    "code": str(row["error_code"]),
                    "details": _loads(row["error_details_json"], None),
                },
            }

    def resume_incomplete(self) -> dict[str, int]:
        """Recover request state after restart without duplicating movements."""

        reset = 0
        finalized = 0
        with _connect(self.db_path) as conn:
            now = self._now()
            reset = int(
                conn.execute(
                    f"UPDATE {REQUESTS_TABLE} SET state='accepted',started_at='',updated_at=? "
                    "WHERE state='processing'",
                    (now,),
                ).rowcount
            )
            rows = conn.execute(
                f"SELECT request_id FROM {REQUESTS_TABLE} WHERE state IN ('posted','replay') "
                "ORDER BY updated_at,request_id LIMIT 500"
            ).fetchall()
            conn.commit()
        for row in rows:
            self._finalize_posted(str(row["request_id"]))
            finalized += 1
        return {"processing_reset": reset, "posted_finalized": finalized}

    def open_transfer_projection(self, root_document_id: str) -> dict[str, Any]:
        """Derive bounded in-flight balance from immutable shipment/children."""

        with _connect(self.db_path, query_only=True) as conn:
            root = _load_document(conn, root_document_id)
            if root is None or str(root["document_kind"]) != "transfer_root":
                raise FfPoolDocumentError("transfer_root_not_found", "Transfer root was not found")
            state = _transfer_state(conn, str(root["document_id"]))
        lines = []
        for nm_id, item in sorted(state.items()):
            open_quantity = int(item["shipped_quantity"]) - int(item["terminal_quantity"])
            open_capital = _component_share(
                int(item["capital_cents"]),
                int(item["shipped_quantity"]),
                int(item["terminal_quantity"]),
                open_quantity,
            )
            open_expense = sum(
                _component_share(
                    int(component["amount_cents"]),
                    int(item["shipped_quantity"]),
                    int(item["terminal_quantity"]),
                    open_quantity,
                )
                for component in item["expense_components"]
            )
            lines.append(
                {
                    "nm_id": nm_id,
                    "shipped_quantity": int(item["shipped_quantity"]),
                    "terminal_quantity": int(item["terminal_quantity"]),
                    "open_quantity": open_quantity,
                    "open_capital_rub": _cents_text(open_capital),
                    "open_expense_rub": _cents_text(open_expense),
                }
            )
        return {
            "contract_name": CONTRACT_NAME,
            "root_document_id": root_document_id,
            "state": "open" if any(item["open_quantity"] for item in lines) else "closed",
            "lines": lines,
            "quantity_conserved": all(
                item["shipped_quantity"] == item["terminal_quantity"] + item["open_quantity"]
                for item in lines
            ),
        }

    def _accept(
        self,
        *,
        identity: DocumentIdentity,
        document_kind: str,
        manifest: Mapping[str, Any],
        source_bytes: bytes,
        source_filename: str,
        source_content_type: str,
        template_fingerprint: str,
    ) -> tuple[str, bool]:
        _validate_identity(identity)
        client_request_id = _client_request_id(identity.request_id)
        source_sha256 = _sha256(source_bytes) if source_bytes else ""
        semantic_manifest = {
            key: value for key, value in manifest.items() if key != "source_filename"
        }
        request_identity = _fingerprint(
            {
                "document_kind": document_kind,
                "source_system": identity.source_system,
                "source_type": identity.source_type,
                "source_id": identity.source_id,
                "source_revision": identity.source_revision,
                "idempotency_epoch": identity.idempotency_epoch,
                "business_date": identity.business_date,
                "source_sha256": source_sha256,
                "template_fingerprint": template_fingerprint,
                "manifest": semantic_manifest,
            }
        )
        canonical = "ffpdr_" + request_identity.removeprefix("sha256:")[:28]
        now = self._now()
        with _connect(self.db_path) as conn:
            ensure_ff_pool_document_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            existing_alias = conn.execute(
                f"SELECT request_identity,request_id FROM {ALIASES_TABLE} WHERE client_request_id=?",
                (client_request_id,),
            ).fetchone()
            if existing_alias is not None and str(existing_alias["request_identity"]) != request_identity:
                raise FfPoolDocumentError(
                    "request_id_identity_conflict",
                    "Client request_id was already used for another semantic document",
                )
            payment_fingerprint = _payment_fingerprint_from_manifest(manifest)
            if document_kind == "pool_overhead" and payment_fingerprint:
                existing_payment = conn.execute(
                    f"""SELECT evidence.request_id,request.request_identity
                        FROM {OVERHEAD_PAYMENT_EVIDENCE_TABLE} AS evidence
                        JOIN {REQUESTS_TABLE} AS request ON request.request_id=evidence.request_id
                        WHERE evidence.payment_fingerprint=?""",
                    (payment_fingerprint,),
                ).fetchone()
                if existing_payment is not None:
                    existing_request_id = str(existing_payment["request_id"])
                    existing_identity = str(existing_payment["request_identity"])
                    conn.execute(
                        f"INSERT OR IGNORE INTO {ALIASES_TABLE}(client_request_id,request_id,request_identity,accepted_at) "
                        "VALUES(?,?,?,?)",
                        (client_request_id, existing_request_id, existing_identity, now),
                    )
                    alias = conn.execute(
                        f"SELECT request_id,request_identity FROM {ALIASES_TABLE} WHERE client_request_id=?",
                        (client_request_id,),
                    ).fetchone()
                    if (
                        alias is None
                        or str(alias["request_id"]) != existing_request_id
                        or str(alias["request_identity"]) != existing_identity
                    ):
                        raise FfPoolDocumentError(
                            "request_id_identity_conflict",
                            "Client request_id was already used for another semantic document",
                        )
                    conn.commit()
                    return existing_request_id, False
            existing_source = conn.execute(
                f"SELECT request_id,request_identity FROM {REQUESTS_TABLE} "
                "WHERE source_system=? AND source_type=? AND source_id=? "
                "AND source_revision=? AND idempotency_epoch=?",
                (
                    str(identity.source_system),
                    str(identity.source_type),
                    str(identity.source_id),
                    str(identity.source_revision),
                    int(identity.idempotency_epoch),
                ),
            ).fetchone()
            if existing_source is not None and str(existing_source["request_identity"]) != request_identity:
                raise FfPoolDocumentError(
                    "source_revision_identity_conflict",
                    "One immutable source revision cannot describe two semantic documents",
                    details={"request_id": str(existing_source["request_id"])},
                )
            inserted = conn.execute(
                f"""INSERT INTO {REQUESTS_TABLE}(
                    request_id,request_identity,client_request_id,document_kind,state,
                    source_system,source_type,source_id,source_revision,idempotency_epoch,
                    actor,business_date,source_filename,source_content_type,source_sha256,
                    source_file_blob,template_fingerprint,request_payload_json,
                    accepted_at,updated_at
                ) VALUES(?,?,?,?,'accepted',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(request_identity) DO NOTHING""",
                (
                    canonical,
                    request_identity,
                    client_request_id,
                    document_kind,
                    str(identity.source_system),
                    str(identity.source_type),
                    str(identity.source_id),
                    str(identity.source_revision),
                    int(identity.idempotency_epoch),
                    str(identity.actor),
                    str(identity.business_date),
                    str(source_filename or ""),
                    str(source_content_type or ""),
                    source_sha256,
                    sqlite3.Binary(source_bytes) if source_bytes else None,
                    str(template_fingerprint or ""),
                    _json(manifest),
                    now,
                    now,
                ),
            ).rowcount
            actual = conn.execute(
                f"SELECT request_id FROM {REQUESTS_TABLE} WHERE request_identity=?",
                (request_identity,),
            ).fetchone()
            if actual is None:
                raise FfPoolDocumentError("request_persist_failed", "Document request was not persisted")
            canonical = str(actual["request_id"])
            conn.execute(
                f"INSERT OR IGNORE INTO {ALIASES_TABLE}(client_request_id,request_id,request_identity,accepted_at) "
                "VALUES(?,?,?,?)",
                (client_request_id, canonical, request_identity, now),
            )
            alias = conn.execute(
                f"SELECT request_id,request_identity FROM {ALIASES_TABLE} WHERE client_request_id=?",
                (client_request_id,),
            ).fetchone()
            if (
                alias is None
                or str(alias["request_identity"]) != request_identity
                or str(alias["request_id"]) != canonical
            ):
                raise FfPoolDocumentError(
                    "request_id_identity_conflict",
                    "Client request_id was concurrently bound to another semantic document",
                )
            canonical = str(alias["request_id"])
            if inserted:
                self._event(conn, request_id=canonical, stage="file_accepted", status="complete")
            if payment_fingerprint:
                evidence = _payment_evidence_from_manifest(manifest)
                conn.execute(
                    f"""INSERT OR IGNORE INTO {OVERHEAD_PAYMENT_EVIDENCE_TABLE}(
                           payment_fingerprint,fingerprint_version,request_id,file_sha256,
                           parser_version,normalized_parser_json,created_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        payment_fingerprint,
                        str(evidence.get("fingerprint_version") or ""),
                        canonical,
                        str(evidence.get("file_sha256") or source_sha256),
                        str(evidence.get("parser_version") or ""),
                        _json(evidence),
                        now,
                    ),
                )
                stored_evidence = conn.execute(
                    f"SELECT request_id FROM {OVERHEAD_PAYMENT_EVIDENCE_TABLE} WHERE payment_fingerprint=?",
                    (payment_fingerprint,),
                ).fetchone()
                if stored_evidence is None or str(stored_evidence["request_id"]) != canonical:
                    raise FfPoolDocumentError(
                        "overhead_payment_duplicate",
                        "Payment order is already linked to another overhead document request",
                        details={"request_id": str(stored_evidence["request_id"]) if stored_evidence else ""},
                    )
            conn.commit()
        return canonical, bool(inserted)

    def _post_once(self, request_id: str) -> dict[str, Any]:
        # The shared warehouse writer lock is the short confirm barrier.  The
        # live plan, T1 before-image and business commit are all produced while
        # functional publication, pool documents and the normal FBS suffix
        # drain are excluded.  Final replay happens after release so newly
        # collected FBS observations are delayed only for this bounded window.
        with warehouse_functional_write_lock(self.runtime_dir):
            result = self._post_once_under_writer_lock(request_id)
        if result is None:
            return self._finalize_posted(request_id)
        return result

    def _post_once_under_writer_lock(
        self, request_id: str
    ) -> dict[str, Any] | None:
        with _connect(self.db_path, query_only=True) as conn:
            request = conn.execute(
                f"SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if request is None:
                raise FfPoolDocumentError("request_not_found", "Document request was not found")
            if str(request["state"]) in {"posted", "replay", "complete"}:
                return None
            if str(request["state"]) != "ready":
                raise FfPoolDocumentError(
                    "request_not_ready",
                    "Only a durable ready preview may be posted",
                    details={"state": str(request["state"])},
                )
            epoch = _writer_epoch(conn)
            if _is_guided_china_request(request):
                _require_guided_acceptance_activation(conn)
            manifest = _json_object(_loads(request["preview_manifest_json"], {}))
            plan = _build_posting_plan(conn, request=request, manifest=manifest, epoch=epoch)
            if _is_guided_china_request(request):
                stored_plan_preview = _json_object(
                    manifest.get("posting_plan_preview") or {}
                )
                live_plan_preview = _posting_plan_preview(plan=plan, epoch=epoch)
                if not _guided_business_effect_matches(
                    stored=stored_plan_preview,
                    live=live_plan_preview,
                ):
                    raise FfPoolDocumentError(
                        "guided_acceptance_posting_plan_drift",
                        "Guided acceptance business effect changed after preview",
                        details={
                            "stored_business_effect_sha256": str(
                                stored_plan_preview.get("business_effect_sha256")
                                or ""
                            ),
                            "live_business_effect_sha256": str(
                                live_plan_preview.get("business_effect_sha256")
                                or ""
                            ),
                        },
                    )
            elif str(request["document_kind"]) == "pool_overhead":
                _assert_pool_overhead_preview_current(
                    stored=_json_object(manifest.get("posting_plan_preview") or {}),
                    live=_pool_overhead_plan_preview(plan=plan, epoch=epoch),
                )
            before_digest = _balance_digest(conn, plan["balance_keys"])
            plan_fingerprint = _fingerprint(
                {
                    "request_identity": str(request["request_identity"]),
                    "epoch": epoch,
                    "before_digest": before_digest,
                    "plan": plan,
                }
            )
            before_images = _before_images(conn, request=request, plan=plan)
        if _is_guided_china_request(request):
            from packages.application.ff_pool_surfaces import FfPoolSurface

            _shipment, _lines, current_source_revision = FfPoolSurface(
                db_path=self.db_path,
                runtime_dir=self.runtime_dir,
                timestamp_factory=self.timestamp_factory,
            ).supplier_shipment_source(str(request["source_id"]))
            manifest_source_revision = str(manifest.get("source_revision") or "")
            expected_request_revision = _guided_request_source_revision(
                supplier_source_revision=manifest_source_revision,
                source_sha256=str(request["source_sha256"] or ""),
            )
            if (
                current_source_revision != manifest_source_revision
                or str(request["source_revision"]) != expected_request_revision
            ):
                raise FfPoolDocumentError(
                    "supplier_source_revision_changed",
                    "Supplier composition or cost inputs changed after preview",
                )
        recovery_registry = WarehouseRecoveryRegistry(
            runtime_dir=self.runtime_dir,
            db_path=self.db_path,
        )
        for recovery_attempt in range(POST_RETRY_LIMIT):
            try:
                recovery = recovery_registry.prepare_t1(
                    mutation_kind="ff_pool_document_posting",
                    closure_kind="document",
                    plan_fingerprint=plan_fingerprint,
                    scope={
                        "request_id": request_id,
                        "document_kind": str(request["document_kind"]),
                        "balance_keys": [list(item) for item in plan["balance_keys"]],
                    },
                    before_images=before_images,
                    source_digest=str(request["request_identity"]),
                    non_target_digest=before_digest,
                )
                break
            except sqlite3.IntegrityError as exc:
                if "recovery_operations.operation_id" not in str(exc) or recovery_attempt + 1 >= POST_RETRY_LIMIT:
                    raise
        else:  # pragma: no cover - loop always breaks or raises
            raise FfPoolDocumentError("recovery_prepare_failed", "T1 recovery could not be prepared")
        if recovery.get("lifecycle") == RecoveryState.VERIFIED.value:
            try:
                recovery = recovery_registry.begin_mutation(
                    str(recovery["operation_id"]),
                    expected_source_digest=str(request["request_identity"]),
                )
            except RecoveryPolicyError:
                concurrent = recovery_registry.get_operation(str(recovery["operation_id"]))
                if concurrent is None or str(concurrent["lifecycle"]) not in {
                    RecoveryState.MUTATION_RUNNING.value,
                    RecoveryState.RETAINED.value,
                }:
                    raise
                recovery = concurrent
        recovery_id = str(recovery["operation_id"])
        try:
            with _connect(self.db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                current_request = conn.execute(
                    f"SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                if current_request is None:
                    raise FfPoolDocumentError("request_not_found", "Document request disappeared")
                if str(current_request["state"]) in {"posted", "replay", "complete"}:
                    conn.rollback()
                    return None
                if _writer_epoch(conn) != epoch or _balance_digest(conn, plan["balance_keys"]) != before_digest:
                    conn.rollback()
                    raise FfPoolDocumentError(
                        "concurrent_pool_balance_drift",
                        "Pool balances changed after the posting plan was prepared",
                    )
                guided_acceptance = _is_guided_china_request(current_request)
                guided_recovery = bool(
                    (plan.get("domain_manifest") or {}).get("guided_acceptance_recovery")
                )
                if guided_acceptance:
                    locked_manifest = _json_object(
                        _loads(current_request["preview_manifest_json"], {})
                    )
                    locked_plan = _build_posting_plan(
                        conn,
                        request=current_request,
                        manifest=locked_manifest,
                        epoch=epoch,
                    )
                    locked_plan_preview = _posting_plan_preview(
                        plan=locked_plan,
                        epoch=epoch,
                    )
                    stored_plan_preview = _json_object(
                        locked_manifest.get("posting_plan_preview") or {}
                    )
                    if not _guided_business_effect_matches(
                        stored=stored_plan_preview,
                        live=locked_plan_preview,
                    ) or _fingerprint(locked_plan) != _fingerprint(plan):
                        raise FfPoolDocumentError(
                            "guided_acceptance_posting_plan_drift",
                            "Guided acceptance posting plan drifted while acquiring the apply lock",
                            details={
                                "stored_business_effect_sha256": str(
                                    stored_plan_preview.get(
                                        "business_effect_sha256"
                                    )
                                    or ""
                                ),
                                "locked_business_effect_sha256": str(
                                    locked_plan_preview.get(
                                        "business_effect_sha256"
                                    )
                                    or ""
                                ),
                            },
                        )
                elif str(current_request["document_kind"]) == "pool_overhead":
                    locked_manifest = _json_object(
                        _loads(current_request["preview_manifest_json"], {})
                    )
                    locked_plan = _build_posting_plan(
                        conn,
                        request=current_request,
                        manifest=locked_manifest,
                        epoch=epoch,
                    )
                    _assert_pool_overhead_preview_current(
                        stored=_json_object(
                            locked_manifest.get("posting_plan_preview") or {}
                        ),
                        live=_pool_overhead_plan_preview(
                            plan=locked_plan,
                            epoch=epoch,
                        ),
                    )
                    if _fingerprint(locked_plan) != _fingerprint(plan):
                        raise FfPoolDocumentError(
                            "overhead_preview_stale",
                            "Pool overhead basis changed after preview; run preview again",
                        )
                posted_at = self._now()
                if guided_acceptance:
                    _apply_guided_acceptance_legacy(
                        conn,
                        request=current_request,
                        manifest=_json_object(_loads(current_request["preview_manifest_json"], {})),
                        posted_at=posted_at,
                    )
                if guided_recovery:
                    _apply_guided_acceptance_recovery_legacy(
                        conn,
                        request=current_request,
                        plan=plan,
                        posted_at=posted_at,
                    )
                _apply_plan(
                    conn,
                    request=current_request,
                    plan=plan,
                    epoch=epoch,
                    posted_at=posted_at,
                )
                if guided_acceptance or guided_recovery:
                    _apply_guided_aggregate_projection(
                        conn, plan=plan, request=current_request, posted_at=posted_at
                    )
                elif (
                    str(current_request["document_kind"]) == "pool_overhead"
                    or bool(
                        _json_object(plan.get("domain_manifest") or {}).get(
                            "overhead_evidence_link"
                        )
                    )
                ):
                    _apply_overhead_current_aggregate_projection(
                        conn,
                        plan=plan,
                        request=current_request,
                        posted_at=posted_at,
                    )
                root_document_id = str(plan["primary_document_id"])
                manifest_sha = _fingerprint(plan["posted_manifest"])
                now = self._now()
                changed = conn.execute(
                    f"UPDATE {REQUESTS_TABLE} SET state='posted',posted_document_id=?,"
                    "posted_manifest_sha256=?,recovery_operation_id=?,posted_at=?,updated_at=? "
                    "WHERE request_id=? AND state='ready'",
                    (root_document_id, manifest_sha, recovery_id, now, now, request_id),
                ).rowcount
                if not changed:
                    raise FfPoolDocumentError("concurrent_duplicate", "Request was concurrently posted")
                self._event(conn, request_id=request_id, stage="document_committed", status="complete")
                conn.commit()
        except FfPoolDocumentError as exc:
            recovery_registry.fail_recoverable(
                recovery_id,
                error=str(exc),
                next_action="retry_exact_pool_document_posting",
            )
            if exc.code == "concurrent_pool_balance_drift":
                raise
            raise
        except Exception as exc:
            recovery_registry.fail_recoverable(
                recovery_id,
                error=str(exc),
                next_action="resume_or_append_storno_pool_document",
            )
            raise
        if (
            str(request["document_kind"]) == "pool_overhead"
            or bool(
                _json_object(plan.get("domain_manifest") or {}).get(
                    "overhead_evidence_link"
                )
            )
        ):
            return self.status(request_id=request_id)
        return None

    def _finalize_posted(self, request_id: str) -> dict[str, Any]:
        with _connect(self.db_path) as conn:
            request = conn.execute(
                f"SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if request is None:
                raise FfPoolDocumentError("request_not_found", "Document request was not found")
            state = str(request["state"])
            if state == "complete":
                return self.status(request_id=request_id)
            if state not in {"posted", "replay"}:
                return self.status(request_id=request_id)
            document_id = str(request["posted_document_id"] or "")
            document = _load_document(conn, document_id)
            if document is None:
                self._finish_error(
                    request_id,
                    state="error",
                    code="posted_readback_missing",
                    details={"document_id": document_id},
                )
                return self.status(request_id=request_id)
            publication_row = conn.execute(
                f"SELECT 1 FROM {TARGETED_RECALC_QUEUE_TABLE} "
                "WHERE stable_source_id=? LIMIT 1",
                (f"pool_overhead:{document_id}",),
            ).fetchone()
            if publication_row is not None:
                publication = _pool_overhead_publication_state(
                    conn, document_id=document_id
                )
                public_state = str(publication.get("status") or "missing")
                now = self._now()
                if state == "posted":
                    conn.execute(
                        f"UPDATE {REQUESTS_TABLE} SET state='replay',replay_at=?,updated_at=? "
                        "WHERE request_id=? AND state='posted'",
                        (now, now, request_id),
                    )
                    self._event(
                        conn,
                        request_id=request_id,
                        stage="replay",
                        status="running",
                        details=publication,
                    )
                    state = "replay"
                if public_state != "complete":
                    error_code = (
                        "overhead_publication_failed"
                        if public_state in {"error", "missing"}
                        else ""
                    )
                    conn.execute(
                        f"UPDATE {REQUESTS_TABLE} SET updated_at=?,error_code=?,"
                        "error_details_json=? WHERE request_id=? AND state IN ('posted','replay')",
                        (
                            now,
                            error_code,
                            _json(publication) if error_code else "null",
                            request_id,
                        ),
                    )
                    conn.commit()
                    return self.status(request_id=request_id)
            now = self._now()
            if state == "posted":
                conn.execute(
                    f"UPDATE {REQUESTS_TABLE} SET state='replay',replay_at=?,updated_at=? "
                    "WHERE request_id=? AND state='posted'",
                    (now, now, request_id),
                )
                self._event(conn, request_id=request_id, stage="replay", status="running")
            conn.commit()
        if _is_guided_china_request(request):
            self._replay_guided_acceptance(request)
        elif self._guided_recovery_target(request):
            self._replay_guided_recovery(request)
        readback = self._verify_posted_readback(request_id)
        with _connect(self.db_path, query_only=True) as conn:
            recovery_row = conn.execute(
                f"SELECT recovery_operation_id FROM {REQUESTS_TABLE} WHERE request_id=?",
                (request_id,),
            ).fetchone()
            recovery_id = str(recovery_row[0] or "") if recovery_row is not None else ""
        if not recovery_id:
            raise RecoveryPolicyError("posted FF pool document has no recovery operation")
        registry = WarehouseRecoveryRegistry(runtime_dir=self.runtime_dir, db_path=self.db_path)
        operation = registry.get_operation(recovery_id)
        if operation is None:
            raise RecoveryPolicyError("posted FF pool document recovery operation is missing")
        lifecycle = str(operation["lifecycle"])
        if lifecycle == RecoveryState.MUTATION_RUNNING.value:
            try:
                registry.retain(recovery_id, after_digest=_fingerprint(readback))
            except RecoveryPolicyError:
                concurrent = registry.get_operation(recovery_id)
                if concurrent is None or str(concurrent["lifecycle"]) != RecoveryState.RETAINED.value:
                    raise
        elif lifecycle != RecoveryState.RETAINED.value:
            raise RecoveryPolicyError(
                f"posted FF pool document recovery is not retainable: {lifecycle}"
            )
        with _connect(self.db_path) as conn:
            now = self._now()
            changed = conn.execute(
                f"UPDATE {REQUESTS_TABLE} SET state='complete',completed_at=?,updated_at=?,"
                "error_code='',error_details_json='null' WHERE request_id=? AND state IN ('posted','replay')",
                (now, now, request_id),
            ).rowcount
            if changed:
                self._event(conn, request_id=request_id, stage="replay", status="complete", details=readback)
            conn.commit()
        return self.status(request_id=request_id)

    def _replay_guided_acceptance(self, request: Mapping[str, Any]) -> None:
        shipment_id = str(request["source_id"])
        from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
        from packages.application.our_wb_costs import OurWbCostBlock
        from packages.application.warehouse_functional import enqueue_warehouse_targeted_recalculation

        runtime = RegistryUploadDbBackedRuntime(runtime_dir=self.runtime_dir)
        manifest = _json_object(_loads(request["preview_manifest_json"], {}))
        accepted_quantities = {
            int(item["nm_id"]): int(item.get("accepted_quantity") or 0)
            for item in manifest.get("allocations") or []
            if int(item.get("accepted_quantity") or 0) > 0
        }
        normalization = _normalize_acceptance_capital(manifest.get("allocations") or [])
        capital_cents = {
            int(key): int(value)
            for key, value in dict(normalization["capital_cents_by_nm"]).items()
            if int(key) in accepted_quantities
        }
        materialized = OurWbCostBlock(
            runtime=runtime,
            timestamp_factory=self.timestamp_factory,
        ).materialize_supplier_ff_cost_layer(
            shipment_id,
            accepted_quantities_by_nm=accepted_quantities,
            accepted_capital_cents_by_nm=capital_cents,
        )
        if materialized is None or not str(materialized.get("layer_id") or ""):
            raise FfPoolDocumentError(
                "guided_cost_layer_missing",
                "Guided acceptance did not materialize its exact supplier cost layer",
            )
        replayed_at = self._now()
        replay_payload = {
            "policy": normalization["policy"],
            "exact_total_rub": normalization["exact_total_rub"],
            "canonical_total_rub": normalization["canonical_total_rub"],
            "total_residual_rub": normalization["total_residual_rub"],
            "exact_capital_rub_by_nm": normalization["exact_capital_rub_by_nm"],
            "capital_cents_by_nm": capital_cents,
            "normalization_residual_rub_by_nm": normalization[
                "normalization_residual_rub_by_nm"
            ],
            "residual_owner_nm_ids": normalization["residual_owner_nm_ids"],
        }
        with _connect(self.db_path) as conn:
            conn.execute(
                f"""INSERT OR IGNORE INTO {GUIDED_REPLAYS_TABLE}(
                       request_id,document_id,shipment_id,source_revision,legacy_operation_id,cost_layer_id,
                       cost_inputs_hash,affected_nm_ids_json,accepted_quantities_json,
                       capital_normalization_json,replayed_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(request["request_id"]),
                    str(request["posted_document_id"]),
                    shipment_id,
                    str(request["source_revision"]),
                    str(
                        conn.execute(
                            """SELECT operation_id FROM sheet_vitrina_v1_ff_stock_operations
                               WHERE source_type='supplier_shipment_acceptance'
                                 AND source_object_id=?
                                 AND json_extract(diagnostics_json,'$.request_id')=?""",
                            (shipment_id, str(request["request_id"])),
                        ).fetchone()[0]
                    ),
                    str(materialized["layer_id"]),
                    str(materialized["inputs_hash"]),
                    _json(sorted(accepted_quantities)),
                    _json(accepted_quantities),
                    _json(replay_payload),
                    replayed_at,
                ),
            )
            stored = conn.execute(
                f"SELECT * FROM {GUIDED_REPLAYS_TABLE} WHERE request_id=?",
                (str(request["request_id"]),),
            ).fetchone()
            if (
                stored is None
                or str(stored["cost_layer_id"]) != str(materialized["layer_id"])
                or str(stored["capital_normalization_json"]) != _json(replay_payload)
            ):
                raise FfPoolDocumentError(
                    "guided_replay_identity_conflict",
                    "Guided acceptance replay evidence conflicts with immutable state",
                )
            conn.commit()
        enqueue_warehouse_targeted_recalculation(
            runtime=runtime,
            stable_source_id=f"supplier_shipment:{shipment_id}",
            source_revision=str(request["request_identity"]),
            effective_date=str(request["business_date"]),
            affected_nm_ids=sorted(accepted_quantities),
            requested_at=replayed_at,
        )

    def _guided_recovery_target(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if str(request["document_kind"] or "") != "storno":
            return {}
        manifest = _json_object(_loads(request["preview_manifest_json"], {}))
        target_document_id = str(manifest.get("target_document_id") or "")
        if not target_document_id:
            return {}
        with _connect(self.db_path, query_only=True) as conn:
            row = conn.execute(
                f"""SELECT replay.request_id AS target_request_id,replay.shipment_id,
                           replay.source_revision,replay.affected_nm_ids_json
                    FROM {DOCUMENTS_TABLE} AS document
                    JOIN {GUIDED_REPLAYS_TABLE} AS replay
                      ON replay.document_id=document.document_id
                    WHERE document.document_id=?""",
                (target_document_id,),
            ).fetchone()
        return dict(row) if row is not None else {}

    def _replay_guided_recovery(self, request: Mapping[str, Any]) -> None:
        target = self._guided_recovery_target(request)
        if not target:
            return
        from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
        from packages.application.warehouse_functional import enqueue_warehouse_targeted_recalculation

        affected = [int(value) for value in _loads(target["affected_nm_ids_json"], [])]
        recovered_at = self._now()
        with _connect(self.db_path) as conn:
            conn.execute(
                f"""INSERT OR IGNORE INTO {GUIDED_RECOVERIES_TABLE}(
                       recovery_request_id,target_request_id,target_document_id,shipment_id,
                       source_revision,affected_nm_ids_json,recovered_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    str(request["request_id"]),
                    str(target["target_request_id"]),
                    str(_json_object(_loads(request["preview_manifest_json"], {})).get("target_document_id") or ""),
                    str(target["shipment_id"]),
                    str(target["source_revision"]),
                    _json(sorted(affected)),
                    recovered_at,
                ),
            )
            stored = conn.execute(
                f"SELECT recovery_request_id FROM {GUIDED_RECOVERIES_TABLE} WHERE target_request_id=?",
                (str(target["target_request_id"]),),
            ).fetchone()
            if stored is None or str(stored["recovery_request_id"]) != str(request["request_id"]):
                raise FfPoolDocumentError(
                    "guided_recovery_identity_conflict",
                    "Guided recovery evidence conflicts with immutable state",
                )
            conn.commit()
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=self.runtime_dir)
        enqueue_warehouse_targeted_recalculation(
            runtime=runtime,
            stable_source_id=f"supplier_shipment:{target['shipment_id']}",
            source_revision=str(request["request_identity"]),
            effective_date=str(request["business_date"]),
            affected_nm_ids=sorted(affected),
            requested_at=recovered_at,
        )

    def _verify_posted_readback(self, request_id: str) -> dict[str, Any]:
        with _connect(self.db_path, query_only=True) as conn:
            request = conn.execute(
                f"SELECT posted_document_id,posted_manifest_sha256 FROM {REQUESTS_TABLE} WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if request is None:
                raise FfPoolDocumentError("request_not_found", "Request readback is missing")
            document = _load_document(conn, str(request["posted_document_id"] or ""))
            if document is None:
                raise FfPoolDocumentError("posted_readback_missing", "Posted document readback is missing")
            operation = conn.execute(
                f"SELECT operation_id FROM {OPERATIONS_TABLE} WHERE operation_id=?",
                (str(document["operation_id"]),),
            ).fetchone()
            if operation is None:
                raise FfPoolDocumentError("posted_operation_missing", "Stage 1 operation readback is missing")
            return {
                "document_id": str(document["document_id"]),
                "root_document_id": str(document["root_document_id"]),
                "operation_id": str(document["operation_id"]),
                "manifest_sha256": str(document["posted_manifest_sha256"]),
            }

    def _finish_error(self, request_id: str, *, state: str, code: str, details: Any) -> None:
        now = self._now()
        with _connect(self.db_path) as conn:
            conn.execute(
                f"UPDATE {REQUESTS_TABLE} SET state=?,updated_at=?,error_code=?,error_details_json=? "
                "WHERE request_id=? AND state NOT IN ('posted','replay','complete')",
                (state, now, str(code), _json(details), request_id),
            )
            self._event(
                conn,
                request_id=request_id,
                stage="validation" if state == "blocked" else "posting",
                status=state,
                details={"error_code": str(code)},
            )
            conn.commit()

    def _resolve_request_id(self, value: str, *, required: bool = True) -> str:
        token = str(value or "").strip()
        with _connect(self.db_path, query_only=True) as conn:
            direct = conn.execute(
                f"SELECT request_id FROM {REQUESTS_TABLE} WHERE request_id=?",
                (token,),
            ).fetchone()
            if direct is not None:
                return str(direct["request_id"])
            alias = conn.execute(
                f"SELECT request_id FROM {ALIASES_TABLE} WHERE client_request_id=?",
                (token,),
            ).fetchone()
            if alias is not None:
                return str(alias["request_id"])
        if required:
            raise FfPoolDocumentError("request_not_found", "Document request was not found")
        return ""

    def _event(
        self,
        conn: sqlite3.Connection,
        *,
        request_id: str,
        stage: str,
        status: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        now = self._now()
        event_id = "ffwe_" + _fingerprint(
            {"request_id": request_id, "stage": stage, "status": status, "at": now}
        ).removeprefix("sha256:")[:24]
        conn.execute(
            f"INSERT OR IGNORE INTO {WORKFLOW_EVENTS_TABLE}(event_id,action_type,identity,stage,status,"
            "occurred_at,duration_ms,details_json) VALUES(?,?,?,?,?,?,0,?)",
            (
                event_id,
                "facility_pool_document",
                request_id,
                stage,
                status,
                now,
                _json(dict(details or {})),
            ),
        )

    def _now(self) -> str:
        value = str(self.timestamp_factory())
        _require_utc(value)
        return value


def _build_posting_plan(
    conn: sqlite3.Connection,
    *,
    request: Mapping[str, Any],
    manifest: Mapping[str, Any],
    epoch: int,
) -> dict[str, Any]:
    kind = str(request["document_kind"])
    builders = {
        "facility_pool_opening": _plan_opening,
        "china_acceptance": _plan_china_acceptance,
        "transfer_root": _plan_transfer_root,
        "transfer_shipment": _plan_transfer_shipment,
        "transfer_receipt": _plan_transfer_receipt,
        "transfer_loss": _plan_transfer_loss,
        "transfer_discrepancy": _plan_transfer_discrepancy,
        "transfer_cancellation": _plan_transfer_cancellation,
        "pool_reallocation": _plan_pool_reallocation,
        "pool_inventory": _plan_pool_inventory,
        "pool_overhead": _plan_pool_overhead,
        "storno": _plan_storno,
        "correction": _plan_correction,
        "late_expense": _plan_late_expense,
    }
    builder = builders.get(kind)
    if builder is None:
        raise FfPoolDocumentError(
            "posting_kind_not_supported",
            "Document kind cannot be posted directly",
            details={"document_kind": kind},
        )
    plan = builder(conn, request=request, manifest=manifest, epoch=epoch)
    plan["balance_keys"] = sorted(
        {
            (str(item["facility_id"]), str(item["pool"]), int(item["nm_id"]))
            for document in plan["documents"]
            for item in document.get("movements", [])
        }
        | {
            (str(item[0]), str(item[1]), int(item[2]))
            for item in plan.get("additional_balance_keys", [])
        }
    )
    plan["posted_manifest"] = {
        "contract_name": CONTRACT_NAME,
        "request_id": str(request["request_id"]),
        "document_kind": kind,
        "business_date": str(request["business_date"]),
        "source": {
            "system": str(request["source_system"]),
            "type": str(request["source_type"]),
            "id": str(request["source_id"]),
            "revision": str(request["source_revision"]),
            "idempotency_epoch": int(request["idempotency_epoch"]),
            "file_sha256": str(request["source_sha256"] or ""),
        },
        "feature_epoch": epoch,
        "primary_document_id": str(plan["primary_document_id"]),
        "root_document_id": str(plan["root_document_id"]),
        "documents": [
            {
                "document_id": str(item["document_id"]),
                "document_kind": str(item["document_kind"]),
                "document_role": str(item["document_role"]),
                "root_document_id": str(item["root_document_id"]),
                "relation": item.get("relation"),
                "line_count": len(item.get("lines", [])),
                "movement_count": len(item.get("movements", [])),
                "expense_line_count": len(item.get("expenses", [])),
            }
            for item in plan["documents"]
        ],
        "domain": plan.get("domain_manifest", {}),
    }
    return plan


def _posting_plan_preview(*, plan: Mapping[str, Any], epoch: int) -> dict[str, Any]:
    """Return compact durable proof that the current guided plan is buildable."""

    documents = list(plan.get("documents") or [])
    movements = [
        dict(movement)
        for document in documents
        for movement in document.get("movements") or []
    ]
    quantity_by_pool: dict[str, int] = {}
    capital_cents_by_pool: dict[str, int] = {}
    for movement in movements:
        pool = str(movement["pool"])
        quantity_by_pool[pool] = quantity_by_pool.get(pool, 0) + int(
            movement["quantity_delta"]
        )
        capital_cents_by_pool[pool] = capital_cents_by_pool.get(pool, 0) + int(
            movement["capital_delta_cents"]
        )
    domain = _json_object(plan.get("domain_manifest") or {})
    before = _json_object(domain.get("guided_acceptance_before_state") or {})
    aggregate_before = list(before.get("aggregate_balances") or [])
    aggregate_pool_parity = _json_object(
        before.get("aggregate_pool_parity") or {}
    )
    business_effect = _guided_business_effect(plan=plan, epoch=epoch)
    return {
        "confirm_plan_ready": True,
        "business_mutation_applied": False,
        "feature_epoch": int(epoch),
        "primary_document_id": str(plan["primary_document_id"]),
        "root_document_id": str(plan["root_document_id"]),
        "document_count": len(documents),
        "line_count": sum(len(document.get("lines") or []) for document in documents),
        "movement_count": len(movements),
        "balance_key_count": len(plan.get("balance_keys") or []),
        "quantity_delta": sum(int(item["quantity_delta"]) for item in movements),
        "capital_delta_rub": _cents_text(
            sum(int(item["capital_delta_cents"]) for item in movements)
        ),
        "quantity_delta_by_pool": dict(sorted(quantity_by_pool.items())),
        "capital_delta_rub_by_pool": {
            pool: _cents_text(value)
            for pool, value in sorted(capital_cents_by_pool.items())
        },
        "aggregate_semantic_zero_nm_ids": sorted(
            int(item["nm_id"])
            for item in aggregate_before
            if not bool(item.get("row_present"))
        ),
        "pool_before_state_digest": _fingerprint(
            list(before.get("pool_balances") or [])
        ),
        "aggregate_before_state_digest": _fingerprint(aggregate_before),
        "aggregate_pool_parity": aggregate_pool_parity,
        "aggregate_pool_parity_digest": _fingerprint(aggregate_pool_parity),
        "dependent_before_state_digest": str(
            (_json_object(before.get("dependent_reservation_debit_state") or {})).get(
                "digest"
            )
            or ""
        ),
        "posted_manifest_sha256": _fingerprint(plan["posted_manifest"]),
        "business_effect_contract": GUIDED_BUSINESS_EFFECT_CONTRACT,
        "business_effect_sha256": _fingerprint(business_effect),
        "confirm_rebase_boundary": "shared_warehouse_writer_lock",
        "source_rechecked": True,
    }


def _pool_overhead_plan_preview(
    *, plan: Mapping[str, Any], epoch: int
) -> dict[str, Any]:
    domain = _json_object(plan.get("domain_manifest") or {})
    evidence = _json_object(domain.get("payment_evidence") or {})
    return {
        "confirm_plan_ready": True,
        "business_mutation_applied": False,
        "feature_epoch": int(epoch),
        "plan_sha256": _fingerprint(plan),
        "primary_document_id": str(plan["primary_document_id"]),
        "facility_id": str(domain.get("facility_id") or ""),
        "scope": str(domain.get("scope") or ""),
        "category": str(domain.get("category") or ""),
        "category_label_ru": str(domain.get("category_label_ru") or ""),
        "comment": str(domain.get("comment") or ""),
        "source_mode": str(domain.get("source_mode") or ""),
        "amount_rub": str(domain.get("amount_rub") or ""),
        "denominator_quantity": int(domain.get("denominator_quantity") or 0),
        "denominator_sku_count": int(domain.get("denominator_sku_count") or 0),
        "affected_sku_count": int(domain.get("affected_sku_count") or 0),
        "allocation_total_rub": str(domain.get("allocation_total_rub") or ""),
        "pool_allocations_rub": dict(domain.get("pool_allocations_rub") or {}),
        "basis_digest": str(domain.get("basis_digest") or ""),
        "payment_evidence": evidence,
        "payment_fingerprint": str(evidence.get("payment_fingerprint") or ""),
        "file_sha256": str(evidence.get("file_sha256") or ""),
        "parser_version": str(evidence.get("parser_version") or ""),
        "amount_conserved": bool(domain.get("amount_conserved")),
        "quantity_delta": 0,
    }


def _assert_pool_overhead_preview_current(
    *, stored: Mapping[str, Any], live: Mapping[str, Any]
) -> None:
    if (
        stored.get("confirm_plan_ready") is not True
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(stored.get("plan_sha256") or ""))
        or str(stored.get("plan_sha256")) != str(live.get("plan_sha256"))
    ):
        raise FfPoolDocumentError(
            "overhead_preview_stale",
            "Pool overhead basis, evidence or duplicate state changed after preview; run preview again",
            details={
                "stored_plan_sha256": str(stored.get("plan_sha256") or ""),
                "live_plan_sha256": str(live.get("plan_sha256") or ""),
            },
        )


def _guided_business_effect(
    *, plan: Mapping[str, Any], epoch: int
) -> dict[str, Any]:
    """Return the owner-gated receipt effect without volatile before-state.

    Normal reservations, releases and FBS physical debits may legitimately
    advance after preview.  They change the diagnostic before-state and full
    posted manifest, but not the immutable receipt that the owner authorizes.
    The confirm path therefore binds this exact effect and rebases only the T1
    before-image while holding the shared warehouse writer lock.
    """

    posted = _json_object(plan.get("posted_manifest") or {})
    domain = _json_object(plan.get("domain_manifest") or {})
    domain.pop("guided_acceptance_before_state", None)
    return {
        "contract_name": GUIDED_BUSINESS_EFFECT_CONTRACT,
        "feature_epoch": int(epoch),
        "request_id": str(posted.get("request_id") or ""),
        "document_kind": str(posted.get("document_kind") or ""),
        "business_date": str(posted.get("business_date") or ""),
        "source": _json_object(posted.get("source") or {}),
        "primary_document_id": str(plan.get("primary_document_id") or ""),
        "root_document_id": str(plan.get("root_document_id") or ""),
        "documents": [dict(item) for item in plan.get("documents") or []],
        "domain": domain,
    }


def _guided_business_effect_matches(
    *, stored: Mapping[str, Any], live: Mapping[str, Any]
) -> bool:
    stored_digest = str(stored.get("business_effect_sha256") or "")
    live_digest = str(live.get("business_effect_sha256") or "")
    return bool(
        stored.get("business_effect_contract")
        == GUIDED_BUSINESS_EFFECT_CONTRACT
        and live.get("business_effect_contract")
        == GUIDED_BUSINESS_EFFECT_CONTRACT
        and re.fullmatch(r"sha256:[0-9a-f]{64}", stored_digest)
        and stored_digest == live_digest
    )


def _guided_current_aggregate_parity_proof(
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    """Require exact quantities and canonical-money aggregate/detail parity."""

    active = conn.execute(
        "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
    ).fetchone()
    if active is None or not str(active[0] or ""):
        raise FfPoolDocumentError(
            "guided_acceptance_parity_not_current",
            "Guided acceptance requires a current active aggregate FF version",
            details={"reason": "aggregate_active_missing"},
        )
    aggregate_version_id = str(active[0])
    aggregate_rows = [
        {
            "nm_id": int(row[0]),
            "quantity": _signed_int(row[1], field="aggregate quantity"),
            "capital_rub": canonical_decimal_text(row[2]),
        }
        for row in conn.execute(
            """SELECT nm_id,quantity,capital_rub
               FROM sheet_vitrina_v1_warehouse_functional_balances
               WHERE version_id=? AND warehouse_key='ff' ORDER BY nm_id""",
            (aggregate_version_id,),
        ).fetchall()
    ]
    parity = evaluate_ff_pool_aggregate_parity(conn, aggregate_rows)
    details = {
        "status": str(parity.status),
        "aggregate_version_id": aggregate_version_id,
        "feature_epoch": int(parity.feature_epoch),
        "detail_row_count": int(parity.detail_row_count),
        "aggregate_row_count": int(parity.aggregate_row_count),
        "detail_quantity": int(parity.detail_quantity),
        "aggregate_quantity": int(parity.aggregate_quantity),
        "detail_capital_rub": canonical_decimal_text(parity.detail_capital_rub),
        "aggregate_capital_rub": canonical_decimal_text(
            parity.aggregate_capital_rub
        ),
        "money_parity_policy": parity.money_parity_policy,
        "detail_canonical_capital_minor_units": (
            parity.detail_canonical_capital_minor_units
        ),
        "aggregate_canonical_capital_minor_units": (
            parity.aggregate_canonical_capital_minor_units
        ),
        "raw_capital_residual_rub": canonical_decimal_text(
            parity.raw_capital_residual_rub
        ),
        "raw_residual_conserved": parity.raw_residual_conserved,
        "raw_capital_mismatched_nm_ids": list(
            parity.raw_capital_mismatched_nm_ids[:100]
        ),
        "raw_capital_residuals_by_nm": {
            str(nm_id): canonical_decimal_text(residual)
            for nm_id, residual in parity.raw_capital_residuals_by_nm[:100]
        },
        "mismatched_nm_id_count": len(parity.mismatched_nm_ids),
        "mismatched_nm_ids": list(parity.mismatched_nm_ids[:100]),
        "detail_fingerprint": str(parity.detail_fingerprint),
        "aggregate_fingerprint": str(parity.aggregate_fingerprint),
    }
    if parity.status != "pass":
        raise FfPoolDocumentError(
            "guided_acceptance_parity_not_current",
            "Current aggregate FF fails canonical facility/pool parity",
            details=details,
        )
    return details


def _require_guided_acceptance_activation(conn: sqlite3.Connection) -> None:
    from packages.application.ff_pool_cutover import read_ff_pool_cutover_status

    status = read_ff_pool_cutover_status(conn)
    if str(status.get("status") or "") != "applied":
        raise FfPoolDocumentError(
            "guided_acceptance_opening_not_active",
            "Guided China acceptance cannot post before exact opening/cutover activation",
            details={"opening_status": str(status.get("status") or "not_applied")},
        )


def _is_guided_china_request(request: Mapping[str, Any]) -> bool:
    return (
        str(request["document_kind"] or "") == "china_acceptance"
        and str(request["source_type"] or "") == "china_acceptance_workbook"
    )


def _guided_dependent_state(
    conn: sqlite3.Connection,
    *,
    facility_id: str,
    nm_ids: Iterable[int],
) -> dict[str, Any]:
    """Capture bounded reservation/debit evidence for recovery drift guards."""

    selected = sorted({int(value) for value in nm_ids})
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    placeholders = ",".join("?" for _value in selected)
    reservations: list[dict[str, Any]] = []
    lifecycle_current: list[dict[str, Any]] = []
    lifecycle_events: list[dict[str, Any]] = []
    if selected and {
        "sheet_vitrina_v1_ff_stock_reservation_operations",
        "sheet_vitrina_v1_ff_stock_reservation_lines",
    }.issubset(tables):
        reservations = [
            dict(row)
            for row in conn.execute(
                f"""SELECT operation.operation_id,operation.source_key,operation.supply_id,
                           operation.supply_revision,operation.operation_type,operation.created_at,
                           line.line_no,line.nm_id,line.quantity_delta,line.raw_json
                    FROM sheet_vitrina_v1_ff_stock_reservation_operations AS operation
                    JOIN sheet_vitrina_v1_ff_stock_reservation_lines AS line
                      ON line.operation_id=operation.operation_id
                    WHERE line.nm_id IN ({placeholders})
                    ORDER BY operation.created_at,operation.operation_id,line.line_no""",
                selected,
            ).fetchall()
        ]
    if selected and "sheet_vitrina_v1_ff_pool_fbs_lifecycle_current" in tables:
        lifecycle_current = [
            dict(row)
            for row in conn.execute(
                f"""SELECT * FROM sheet_vitrina_v1_ff_pool_fbs_lifecycle_current
                    WHERE facility_id=? AND pool='FBS' AND nm_id IN ({placeholders})
                    ORDER BY cutover_id,order_id""",
                (facility_id, *selected),
            ).fetchall()
        ]
    if selected and "sheet_vitrina_v1_ff_pool_fbs_lifecycle_events" in tables:
        lifecycle_events = [
            dict(row)
            for row in conn.execute(
                f"""SELECT * FROM sheet_vitrina_v1_ff_pool_fbs_lifecycle_events
                    WHERE facility_id=? AND pool='FBS' AND nm_id IN ({placeholders})
                    ORDER BY event_sequence""",
                (facility_id, *selected),
            ).fetchall()
        ]
    value = {
        "facility_id": facility_id,
        "nm_ids": selected,
        "reservation_row_count": len(reservations),
        "reservation_rows_digest": _fingerprint(reservations),
        "reservation_net_quantity_by_nm": {
            nm_id: canonical_decimal_text(
                sum(
                    (
                        Decimal(str(row["quantity_delta"]))
                        for row in reservations
                        if int(row["nm_id"]) == nm_id
                    ),
                    ZERO,
                )
            )
            for nm_id in selected
        },
        "fbs_lifecycle_current_row_count": len(lifecycle_current),
        "fbs_lifecycle_current_digest": _fingerprint(lifecycle_current),
        "fbs_lifecycle_event_row_count": len(lifecycle_events),
        "fbs_lifecycle_event_digest": _fingerprint(lifecycle_events),
        "fbs_lifecycle_max_event_sequence": max(
            (int(row["event_sequence"]) for row in lifecycle_events),
            default=0,
        ),
    }
    return {**value, "digest": _fingerprint(value)}


def _apply_guided_acceptance_legacy(
    conn: sqlite3.Connection,
    *,
    request: Mapping[str, Any],
    manifest: Mapping[str, Any],
    posted_at: str,
) -> None:
    """Atomically own factual date + aggregate FF receipt before pool detail."""

    shipment_id = str(request["source_id"] or "").strip()
    shipment = conn.execute(
        """SELECT shipment_id,actual_ff_acceptance_date,archived_at,order_status
           FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?""",
        (shipment_id,),
    ).fetchone()
    if shipment is None or str(shipment["archived_at"] or ""):
        raise FfPoolDocumentError("supplier_shipment_not_found", "Source shipment is missing or archived")
    base_source_key = f"supplier_shipment_acceptance:{shipment_id}"
    prior = conn.execute(
        "SELECT operation_id FROM sheet_vitrina_v1_ff_stock_operations WHERE source_key=?",
        (base_source_key,),
    ).fetchone()
    active_guided = conn.execute(
        f"""SELECT replay.request_id
            FROM {GUIDED_REPLAYS_TABLE} AS replay
            LEFT JOIN {GUIDED_RECOVERIES_TABLE} AS recovery
              ON recovery.target_request_id=replay.request_id
            WHERE replay.shipment_id=? AND recovery.target_request_id IS NULL
            LIMIT 1""",
        (shipment_id,),
    ).fetchone()
    if str(shipment["actual_ff_acceptance_date"] or "") or active_guided is not None:
        raise FfPoolDocumentError(
            "supplier_shipment_already_accepted",
            "Shipment already has a factual FF acceptance; double posting is forbidden",
            details={"shipment_id": shipment_id},
        )
    source_key = (
        base_source_key
        if prior is None
        else base_source_key + ":" + str(request["request_id"])
    )
    allocations = [
        dict(item) for item in manifest.get("allocations") or []
        if int(item.get("accepted_quantity") or 0) > 0
    ]
    if not allocations:
        raise FfPoolDocumentError("empty_actual_acceptance", "Actual acceptance has no positive rows")
    operation_id = "ffop_guided_" + _fingerprint(
        {"request_identity": str(request["request_identity"]), "shipment_id": shipment_id}
    ).removeprefix("sha256:")[:28]
    normalization = _normalize_acceptance_capital(manifest.get("allocations") or [])
    capital_cents_by_nm = {
        int(key): int(value)
        for key, value in dict(normalization["capital_cents_by_nm"]).items()
    }
    total = sum(int(item["accepted_quantity"]) for item in allocations)
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_ff_stock_operations(
               operation_id,operation_type,source_type,source_key,source_object_id,
               source_object_label,created_at,business_effective_date,created_by,
               sku_count,total_quantity_delta,total_quantity_abs,warnings_json,diagnostics_json,
               source_filename,source_content_type,source_file_sha256,source_file_blob
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            operation_id, "receipt", "supplier_shipment_acceptance", source_key,
            shipment_id, shipment_id, posted_at, str(request["business_date"]),
            str(request["actor"]), len(allocations), total, total, "[]",
            _json({
                "single_owner": "guided_china_acceptance",
                "request_id": str(request["request_id"]),
                "facility_id": str(manifest.get("facility_id") or ""),
            }),
            str(request["source_filename"] or ""), str(request["source_content_type"] or ""),
            str(request["source_sha256"] or ""), request["source_file_blob"],
        ),
    )
    for line_no, item in enumerate(allocations, start=1):
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_ff_stock_operation_lines(
                   operation_id,line_no,nm_id,barcode,sku,nomenclature_name,comment,
                   group_name,quantity_delta,raw_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                operation_id, line_no, int(item["nm_id"]), str(item.get("barcode") or ""),
                str(item.get("sku") or ""), "", str(item.get("comment") or ""), "",
                int(item["accepted_quantity"]),
                _json(
                    {
                        **item,
                        "cost_snapshot": {
                            "unit_cost_rub": canonical_decimal_text(
                                (Decimal(capital_cents_by_nm[int(item["nm_id"])]) / Decimal(100))
                                / Decimal(int(item["accepted_quantity"]))
                            ),
                            "capital_delta_rub": _cents_text(
                                capital_cents_by_nm[int(item["nm_id"])]
                            ),
                            "quality": "guided_acceptance_minor_unit",
                            "provenance": {
                                "request_id": str(request["request_id"]),
                                "source_revision": str(request["source_revision"]),
                                "money_boundary": normalization["policy"],
                                "exact_source_capital_rub": normalization[
                                    "exact_capital_rub_by_nm"
                                ][int(item["nm_id"])],
                                "normalization_residual_rub": normalization[
                                    "normalization_residual_rub_by_nm"
                                ][int(item["nm_id"])],
                            },
                        },
                    }
                ),
            ),
        )
    changed = conn.execute(
        """UPDATE sheet_vitrina_v1_supplier_shipments
           SET actual_ff_acceptance_date=?,order_status='accepted_ff',updated_at=?
           WHERE shipment_id=? AND COALESCE(actual_ff_acceptance_date,'')='' AND archived_at IS NULL""",
        (str(request["business_date"]), posted_at, shipment_id),
    ).rowcount
    if changed != 1:
        raise FfPoolDocumentError("supplier_acceptance_concurrent_drift", "Shipment acceptance changed concurrently")


def _apply_guided_acceptance_recovery_legacy(
    conn: sqlite3.Connection,
    *,
    request: Mapping[str, Any],
    plan: Mapping[str, Any],
    posted_at: str,
) -> None:
    """Append the legacy compensation and restore the guarded supplier state."""

    recovery = _json_object(
        (plan.get("domain_manifest") or {}).get("guided_acceptance_recovery") or {}
    )
    if not recovery:
        return
    shipment_id = str(recovery["shipment_id"])
    original_operation = conn.execute(
        "SELECT * FROM sheet_vitrina_v1_ff_stock_operations WHERE operation_id=?",
        (str(recovery["legacy_operation_id"]),),
    ).fetchone()
    original_lines = conn.execute(
        """SELECT * FROM sheet_vitrina_v1_ff_stock_operation_lines
           WHERE operation_id=? ORDER BY line_no""",
        (str(recovery["legacy_operation_id"]),),
    ).fetchall()
    if original_operation is None or not original_lines:
        raise FfPoolDocumentError(
            "guided_recovery_legacy_receipt_missing",
            "The original aggregate receipt cannot be compensated",
        )
    operation_id = "ffop_guided_recovery_" + _fingerprint(
        {
            "request_identity": str(request["request_identity"]),
            "target_request_id": str(recovery["target_request_id"]),
        }
    ).removeprefix("sha256:")[:22]
    source_key = (
        f"supplier_shipment_acceptance_recovery:{shipment_id}:"
        f"{recovery['target_request_id']}"
    )
    total = sum(int(Decimal(str(line["quantity_delta"]))) for line in original_lines)
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_ff_stock_operations(
               operation_id,operation_type,source_type,source_key,source_object_id,
               source_object_label,created_at,business_effective_date,created_by,
               sku_count,total_quantity_delta,total_quantity_abs,warnings_json,diagnostics_json,
               source_filename,source_content_type,source_file_sha256,source_file_blob
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            operation_id,
            "storno",
            "supplier_shipment_acceptance_recovery",
            source_key,
            shipment_id,
            shipment_id,
            posted_at,
            str(request["business_date"]),
            str(request["actor"]),
            len(original_lines),
            -total,
            total,
            "[]",
            _json(
                {
                    "append_only_compensation": True,
                    "request_id": str(request["request_id"]),
                    "target_request_id": str(recovery["target_request_id"]),
                    "before_state_digest": str(recovery["before_state_digest"]),
                }
            ),
            str(request["source_filename"] or ""),
            str(request["source_content_type"] or ""),
            str(request["source_sha256"] or ""),
            request["source_file_blob"],
        ),
    )
    for source in original_lines:
        raw = _json_object(_loads(source["raw_json"], {}))
        original_snapshot = _json_object(raw.get("cost_snapshot") or {})
        original_capital = Decimal(str(original_snapshot.get("capital_delta_rub") or "0"))
        snapshot_provenance = _json_object(original_snapshot.get("provenance") or {})
        original_snapshot.update(
            {
                "capital_delta_rub": canonical_decimal_text(-original_capital),
                "quality": "guided_acceptance_recovery",
                "provenance": {
                    **snapshot_provenance,
                    "append_only_compensation": True,
                    "recovery_request_id": str(request["request_id"]),
                    "target_request_id": str(recovery["target_request_id"]),
                },
            }
        )
        raw.update(
            {
                "append_only_compensation": True,
                "target_request_id": str(recovery["target_request_id"]),
                "cost_snapshot": original_snapshot,
            }
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_ff_stock_operation_lines(
                   operation_id,line_no,nm_id,barcode,sku,nomenclature_name,comment,
                   group_name,quantity_delta,raw_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                operation_id,
                int(source["line_no"]),
                int(source["nm_id"]),
                str(source["barcode"] or ""),
                str(source["sku"] or ""),
                str(source["nomenclature_name"] or ""),
                str(source["comment"] or ""),
                str(source["group_name"] or ""),
                -int(Decimal(str(source["quantity_delta"]))),
                _json(raw),
            ),
        )
    restore = _json_object(recovery.get("restore_supplier") or {})
    changed = conn.execute(
        """UPDATE sheet_vitrina_v1_supplier_shipments
           SET actual_ff_acceptance_date=?,order_status=?,updated_at=?
           WHERE shipment_id=? AND actual_ff_acceptance_date=? AND order_status='accepted_ff'""",
        (
            str(restore.get("actual_ff_acceptance_date") or "") or None,
            str(restore.get("order_status") or ""),
            posted_at,
            shipment_id,
            str(recovery["accepted_business_date"]),
        ),
    ).rowcount
    if changed != 1:
        raise FfPoolDocumentError(
            "guided_recovery_supplier_drift",
            "Supplier factual acceptance changed before compensation",
        )
    layer = conn.execute(
        """SELECT layer_id,supersedes_layer_id FROM sheet_vitrina_v1_supplier_ff_cost_layers
           WHERE layer_id=? AND supplier_shipment_id=? AND is_current=1""",
        (str(recovery["cost_layer_id"]), shipment_id),
    ).fetchone()
    if layer is None:
        raise FfPoolDocumentError(
            "guided_recovery_cost_layer_drift",
            "Supplier cost layer changed before compensation",
        )
    conn.execute(
        """UPDATE sheet_vitrina_v1_supplier_ff_cost_layers
           SET is_current=0,superseded_at=? WHERE layer_id=? AND is_current=1""",
        (posted_at, str(layer["layer_id"])),
    )
    previous_layer_id = str(layer["supersedes_layer_id"] or "")
    if previous_layer_id:
        restored = conn.execute(
            """UPDATE sheet_vitrina_v1_supplier_ff_cost_layers
               SET is_current=1,superseded_at=NULL WHERE layer_id=? AND is_current=0""",
            (previous_layer_id,),
        ).rowcount
        if restored != 1:
            raise FfPoolDocumentError(
                "guided_recovery_previous_cost_layer_drift",
                "Previous supplier cost layer cannot be restored exactly",
            )
    conn.execute(
        f"""INSERT INTO {GUIDED_RECOVERIES_TABLE}(
               recovery_request_id,target_request_id,target_document_id,shipment_id,
               source_revision,affected_nm_ids_json,recovered_at
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            str(request["request_id"]),
            str(recovery["target_request_id"]),
            str(recovery["target_document_id"]),
            shipment_id,
            str(recovery["source_revision"]),
            _json(list(recovery.get("affected_nm_ids") or [])),
            posted_at,
        ),
    )


def _guided_new_aggregate_provenance(request: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source": "guided_china_acceptance",
        "request_id": str(request["request_id"]),
        "shipment_id": str(request["source_id"]),
        "source_revision": str(request["source_revision"]),
        "business_date": str(request["business_date"]),
        "materialized_from_semantic_zero": True,
        "append_only_recovery_keeps_zero_row": True,
    }


def _apply_guided_aggregate_projection(
    conn: sqlite3.Connection,
    *,
    plan: Mapping[str, Any],
    request: Mapping[str, Any],
    posted_at: str,
) -> None:
    """Keep the existing aggregate FF projection exact with the pool receipt."""

    deltas: dict[int, tuple[int, Decimal]] = {}
    for document in plan.get("documents") or []:
        for movement in document.get("movements") or []:
            nm_id = int(movement["nm_id"])
            quantity, capital = deltas.get(nm_id, (0, Decimal("0")))
            deltas[nm_id] = (
                quantity + int(movement["quantity_delta"]),
                capital
                + Decimal(int(movement["capital_delta_cents"])) / Decimal(100),
            )
    active = conn.execute(
        "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
    ).fetchone()
    if active is None:
        raise FfPoolDocumentError(
            "aggregate_active_missing", "Guided acceptance requires the active aggregate FF version"
        )
    version_id = str(active[0])
    recovery = _json_object(
        (plan.get("domain_manifest") or {}).get("guided_acceptance_recovery") or {}
    )
    restore_by_nm = {
        int(item["nm_id"]): dict(item)
        for item in recovery.get("restore_aggregate_balances") or []
    }
    for nm_id, (quantity_delta, capital_delta) in sorted(deltas.items()):
        row = conn.execute(
            """SELECT quantity,capital_rub,cost_covered_quantity
               FROM sheet_vitrina_v1_warehouse_functional_balances
               WHERE version_id=? AND warehouse_key='ff' AND nm_id=?""",
            (version_id, nm_id),
        ).fetchone()
        # The active aggregate is the exact sum of facility/pool rows and may
        # carry a long fractional-kopeck tail.  Keep the arithmetic outside
        # process-default Decimal precision just like the pool writer and the
        # ordinary functional publisher.
        with localcontext() as context:
            context.prec = 160
            if row is None:
                if recovery or quantity_delta <= 0 or capital_delta <= ZERO:
                    raise FfPoolDocumentError(
                        "aggregate_sku_missing",
                        "Guided acceptance aggregate SKU disappeared before apply",
                        details={"nm_id": nm_id},
                    )
                quantity = Decimal(quantity_delta)
                capital = capital_delta
                conn.execute(
                    """INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                           version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                           cost_covered_quantity,quality,certified,wb_quantity,
                           wb_in_way_to_client,wb_in_way_from_client,provenance_json
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        version_id,
                        "ff",
                        nm_id,
                        canonical_decimal_text(quantity),
                        canonical_decimal_text(capital / quantity),
                        canonical_decimal_text(capital),
                        canonical_decimal_text(quantity),
                        "guided_acceptance_minor_unit",
                        0,
                        "0",
                        "0",
                        "0",
                        _json(_guided_new_aggregate_provenance(request)),
                    ),
                )
            else:
                quantity = Decimal(str(row[0])) + Decimal(quantity_delta)
                capital = Decimal(str(row[1])) + capital_delta
                covered = Decimal(str(row[2])) + Decimal(quantity_delta)
                if (
                    quantity < ZERO
                    or capital < ZERO
                    or covered < ZERO
                    or (quantity == ZERO) != (capital == ZERO)
                ):
                    raise FfPoolDocumentError(
                        "guided_acceptance_aggregate_invalid",
                        "Guided acceptance would create invalid aggregate quantity/capital coverage",
                        details={"nm_id": nm_id},
                    )
                wac = (
                    None
                    if quantity == ZERO
                    else canonical_decimal_text(capital / quantity)
                )
                conn.execute(
                    """UPDATE sheet_vitrina_v1_warehouse_functional_balances
                       SET quantity=?,capital_rub=?,wac_rub=?,cost_covered_quantity=?
                       WHERE version_id=? AND warehouse_key='ff' AND nm_id=?""",
                    (
                        canonical_decimal_text(quantity),
                        canonical_decimal_text(capital),
                        wac,
                        canonical_decimal_text(covered),
                        version_id,
                        nm_id,
                    ),
                )
    if recovery:
        if set(restore_by_nm) != set(deltas):
            raise FfPoolDocumentError(
                "guided_recovery_before_state_missing",
                "Recovery does not contain every affected aggregate before-state",
            )
        for nm_id, before in sorted(restore_by_nm.items()):
            row = conn.execute(
                """SELECT quantity,wac_rub,capital_rub,cost_covered_quantity
                   FROM sheet_vitrina_v1_warehouse_functional_balances
                   WHERE version_id=? AND warehouse_key='ff' AND nm_id=?""",
                (version_id, nm_id),
            ).fetchone()
            if row is None:
                raise FfPoolDocumentError(
                    "guided_recovery_aggregate_drift",
                    "Recovered aggregate projection row disappeared",
                    details={"nm_id": nm_id},
                )
            if bool(before.get("row_present")):
                conn.execute(
                    """UPDATE sheet_vitrina_v1_warehouse_functional_balances
                       SET quantity=?,wac_rub=?,capital_rub=?,cost_covered_quantity=?,
                           quality=?,certified=?,wb_quantity=?,wb_in_way_to_client=?,
                           wb_in_way_from_client=?,provenance_json=?
                       WHERE version_id=? AND warehouse_key='ff' AND nm_id=?""",
                    (
                        str(before["quantity"]),
                        before.get("wac_rub"),
                        str(before["capital_rub"]),
                        str(before["cost_covered_quantity"]),
                        str(before["quality"]),
                        int(before["certified"]),
                        str(before["wb_quantity"]),
                        str(before["wb_in_way_to_client"]),
                        str(before["wb_in_way_from_client"]),
                        str(before["provenance_json"]),
                        version_id,
                        nm_id,
                    ),
                )
            elif any(
                Decimal(str(value or "0")) != ZERO
                for value in (row[0], row[2], row[3])
            ) or row[1] is not None:
                raise FfPoolDocumentError(
                    "guided_recovery_aggregate_drift",
                    "A semantic-zero aggregate SKU did not recover to zero",
                    details={"nm_id": nm_id},
                )
    aggregate_rows = [
        {
            "nm_id": int(row[0]),
            "quantity": _signed_int(row[1], field="aggregate quantity"),
            "capital_rub": canonical_decimal_text(row[2]),
        }
        for row in conn.execute(
            """SELECT nm_id,quantity,capital_rub
               FROM sheet_vitrina_v1_warehouse_functional_balances
               WHERE version_id=? AND warehouse_key='ff' ORDER BY nm_id""",
            (version_id,),
        ).fetchall()
    ]
    parity = evaluate_ff_pool_aggregate_parity(conn, aggregate_rows)
    if parity.status != "pass":
        raise FfPoolDocumentError(
            "guided_acceptance_parity_failed",
            "Guided acceptance diverged from aggregate FF",
            details={"mismatched_nm_ids": list(parity.mismatched_nm_ids)},
        )
    record_ff_pool_parity_diagnostic(
        conn,
        diagnostic_id="ffpar_guided_" + _fingerprint(
            {"request_id": str(request["request_id"]), "posted_at": posted_at}
        ).removeprefix("sha256:")[:22],
        aggregate_revision=version_id,
        checked_at=posted_at,
        result=parity,
        details={
            "source": (
                "guided_china_acceptance_recovery"
                if (plan.get("domain_manifest") or {}).get("guided_acceptance_recovery")
                else "guided_china_acceptance"
            ),
            "shipment_id": str(
                ((plan.get("domain_manifest") or {}).get("guided_acceptance_recovery") or {}).get(
                    "shipment_id"
                )
                or request["source_id"]
            ),
        },
    )


def _plan_opening(
    conn: sqlite3.Connection,
    *,
    request: Mapping[str, Any],
    manifest: Mapping[str, Any],
    epoch: int,
) -> dict[str, Any]:
    existing_detail = conn.execute(
        f"SELECT 1 FROM {BALANCES_TABLE} WHERE projection_epoch=? "
        "AND (quantity<>0 OR CAST(capital_rub AS NUMERIC)<>0) LIMIT 1",
        (epoch,),
    ).fetchone()
    if existing_detail is not None:
        raise FfPoolDocumentError(
            "opening_requires_empty_detail",
            "Facility × pool opening can only initialize an empty detail contour",
        )
    allocations = list(manifest.get("allocations") or [])
    aggregate_rows = list(manifest.get("aggregate_rows") or manifest.get("aggregate") or [])
    if not allocations or not aggregate_rows:
        raise FfPoolDocumentError(
            "opening_composition_required",
            "Opening requires aggregate and facility × pool allocations",
        )
    aggregate: dict[int, tuple[int, int]] = {}
    for item in aggregate_rows:
        nm_id = _positive_int(item.get("nm_id"), field="aggregate nm_id")
        if nm_id in aggregate:
            raise FfPoolDocumentError("duplicate_nm_id", "Aggregate opening contains duplicate nmId")
        aggregate[nm_id] = (
            _nonnegative_int(item.get("quantity"), field="aggregate quantity"),
            _money_cents(item.get("capital_rub"), field="aggregate capital"),
        )
    detail: dict[int, tuple[int, int]] = {}
    movements: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    keys: set[tuple[str, str, int]] = set()
    for item in allocations:
        facility_id, pool = _location_fields(conn, item, require_active=True)
        nm_id = _positive_int(item.get("nm_id"), field="allocation nm_id")
        quantity = _nonnegative_int(item.get("quantity"), field="allocation quantity")
        capital_cents = _money_cents(item.get("capital_rub"), field="allocation capital")
        if (quantity == 0) != (capital_cents == 0):
            raise FfPoolDocumentError(
                "opening_zero_mismatch",
                "Opening quantity and capital must be zero together",
                details={"nm_id": nm_id, "facility_id": facility_id, "pool": pool},
            )
        key = (facility_id, pool, nm_id)
        if key in keys:
            raise FfPoolDocumentError("duplicate_balance_key", "Opening duplicates a facility/pool/SKU")
        keys.add(key)
        prior_q, prior_c = detail.get(nm_id, (0, 0))
        detail[nm_id] = (prior_q + quantity, prior_c + capital_cents)
        if quantity:
            movements.append(
                _movement(
                    facility_id=facility_id,
                    pool=pool,
                    nm_id=nm_id,
                    quantity_delta=quantity,
                    capital_delta_cents=capital_cents,
                    wac_snapshot=_ratio_text(capital_cents, quantity),
                    metadata={"opening_contract": "future_cutover_only"},
                )
            )
        lines.append(
            _document_line(
                role="opening_allocation",
                facility_id=facility_id,
                pool=pool,
                nm_id=nm_id,
                quantity=quantity,
                capital_cents=capital_cents,
            )
        )
    mismatches = sorted(nm_id for nm_id in set(aggregate) | set(detail) if aggregate.get(nm_id, (0, 0)) != detail.get(nm_id, (0, 0)))
    if mismatches:
        raise FfPoolDocumentError(
            "opening_aggregate_parity_mismatch",
            "Opening allocations do not exactly decompose aggregate FF",
            details={"nm_ids": mismatches[:100]},
        )
    document_id = _request_document_id(request)
    document = _document_blueprint(
        document_id=document_id,
        document_kind="facility_pool_opening",
        document_role="root",
        root_document_id=document_id,
        lines=lines,
        movements=movements,
    )
    return {
        "primary_document_id": document_id,
        "root_document_id": document_id,
        "documents": [document],
        "domain_manifest": {
            "aggregate_unchanged": True,
            "aggregate_by_nm": {
                str(nm_id): {"quantity": value[0], "capital_rub": _cents_text(value[1])}
                for nm_id, value in sorted(aggregate.items())
            },
            "detail_parity": True,
        },
    }


def _plan_china_acceptance(
    conn: sqlite3.Connection,
    *,
    request: Mapping[str, Any],
    manifest: Mapping[str, Any],
    epoch: int,
) -> dict[str, Any]:
    facility_id = _facility(conn, str(manifest.get("facility_id") or ""), require_active=True)
    allocations = list(manifest.get("allocations") or [])
    if not allocations:
        raise FfPoolDocumentError("acceptance_allocations_required", "Acceptance allocation is empty")
    expenses = _expense_lines(manifest.get("expenses") or [])
    weights: list[tuple[tuple[int, str], int]] = []
    source_capital: dict[tuple[int, str], int] = {}
    evidence: dict[int, str] = {}
    total_accepted_quantity = 0
    normalization = _normalize_acceptance_capital(allocations)
    capital_by_nm = {
        int(key): int(value)
        for key, value in dict(normalization["capital_cents_by_nm"]).items()
    }
    exact_capital_by_nm = {
        int(key): str(value)
        for key, value in dict(normalization["exact_capital_rub_by_nm"]).items()
    }
    residual_by_nm = {
        int(key): str(value)
        for key, value in dict(normalization["normalization_residual_rub_by_nm"]).items()
    }
    total_accepted_capital = int(normalization["canonical_total_cents"])
    seen_nm_ids: set[int] = set()
    for item in allocations:
        nm_id = _positive_int(item.get("nm_id"), field="acceptance nm_id")
        if nm_id in seen_nm_ids:
            raise FfPoolDocumentError(
                "duplicate_acceptance_nm_id",
                "Acceptance contains the same nm_id more than once",
                details={"nm_id": nm_id},
            )
        seen_nm_ids.add(nm_id)
        fbo = _nonnegative_int(item.get("quantity_fbo"), field="FBO quantity")
        fbs = _nonnegative_int(item.get("quantity_fbs"), field="FBS quantity")
        accepted = _nonnegative_int(item.get("accepted_quantity"), field="accepted quantity")
        if fbo + fbs != accepted:
            raise FfPoolDocumentError(
                "allocation_quantity_mismatch",
                "FBO + FBS must equal accepted quantity",
                details={"nm_id": nm_id},
            )
        capital = capital_by_nm[nm_id]
        if accepted == 0 and capital != 0:
            raise FfPoolDocumentError(
                "zero_quantity_with_capital",
                "Zero accepted quantity must have zero accepted capital",
                details={"nm_id": nm_id},
            )
        identity_digest = str(item.get("identity_evidence_digest") or "").strip()
        if not identity_digest.startswith("sha256:"):
            raise FfPoolDocumentError(
                "exact_identity_evidence_missing",
                "Acceptance requires exact server-owned SKU identity evidence",
                details={"nm_id": nm_id},
            )
        evidence[nm_id] = identity_digest
        split = _allocate_cents(capital, [((nm_id, "FBO"), fbo), ((nm_id, "FBS"), fbs)])
        for pool, quantity in (("FBO", fbo), ("FBS", fbs)):
            if quantity:
                if int(split[(nm_id, pool)]) <= 0:
                    raise FfPoolDocumentError(
                        "pool_capital_rounds_to_zero",
                        "Positive pool allocation would become synthetic zero",
                        details={"nm_id": nm_id, "pool": pool},
                    )
                weights.append(((nm_id, pool), quantity))
                source_capital[(nm_id, pool)] = int(split[(nm_id, pool)])
        total_accepted_quantity += accepted
    expense_total = sum(int(item["amount_cents"]) for item in expenses)
    expense_allocations = {key: 0 for key, _quantity in weights}
    for expense in expenses:
        scope = str((expense.get("metadata") or {}).get("allocation_scope") or "both").strip().upper()
        if scope not in {"FBS", "FBO", "BOTH"}:
            raise FfPoolDocumentError(
                "invalid_expense_allocation_scope",
                "China acceptance expense scope must be FBS, FBO or both",
            )
        scoped_weights = [
            (key, quantity) for key, quantity in weights if scope == "BOTH" or key[1] == scope
        ]
        if not scoped_weights:
            raise FfPoolDocumentError(
                "expense_allocation_scope_empty",
                "Expense scope has no accepted quantity",
                details={"allocation_scope": scope},
            )
        allocated = _allocate_cents(int(expense["amount_cents"]), scoped_weights)
        for key, amount in allocated.items():
            expense_allocations[key] += int(amount)
    movements: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    for (nm_id, pool), quantity in sorted(weights):
        base_capital = source_capital[(nm_id, pool)]
        expense = int(expense_allocations[(nm_id, pool)])
        capital = base_capital + expense
        movements.append(
            _movement(
                facility_id=facility_id,
                pool=pool,
                nm_id=nm_id,
                quantity_delta=quantity,
                capital_delta_cents=capital,
                wac_snapshot=_ratio_text(capital, quantity),
                metadata={
                    "exact_source_capital_rub": exact_capital_by_nm[nm_id],
                    "source_capital_rub": _cents_text(base_capital),
                    "sku_normalization_residual_rub": residual_by_nm[nm_id],
                    "money_boundary": "header_half_up_then_largest_remainder",
                    "expense_rub": _cents_text(expense),
                    "identity_evidence_digest": evidence[nm_id],
                },
            )
        )
        lines.append(
            _document_line(
                role="accepted_pool_allocation",
                facility_id=facility_id,
                pool=pool,
                nm_id=nm_id,
                quantity=quantity,
                capital_cents=base_capital,
                expense_cents=expense,
                metadata={
                    "identity_evidence_digest": evidence[nm_id],
                    "exact_source_capital_rub": exact_capital_by_nm[nm_id],
                    "sku_normalization_residual_rub": residual_by_nm[nm_id],
                    "money_boundary": "header_half_up_then_largest_remainder",
                },
            )
        )
    document_id = _request_document_id(request)
    document = _document_blueprint(
        document_id=document_id,
        document_kind="china_acceptance",
        document_role="root",
        root_document_id=document_id,
        lines=lines,
        movements=movements,
        expenses=expenses,
    )
    documents = [document]
    discrepancies = [
        item for item in allocations if int(item.get("discrepancy_quantity") or 0) > 0
    ]
    if discrepancies:
        discrepancy_id = document_id + "_discrepancy"
        discrepancy_lines = [
            _document_line(
                role=str(item.get("discrepancy_type") or "discrepancy"),
                facility_id=facility_id,
                pool=None,
                nm_id=_positive_int(item.get("nm_id"), field="discrepancy nm_id"),
                quantity=_nonnegative_int(item.get("discrepancy_quantity"), field="discrepancy quantity"),
                capital_cents=0,
                metadata={
                    "expected_quantity": int(item.get("expected_quantity") or 0),
                    "accepted_quantity": int(item.get("accepted_quantity") or 0),
                    "comment": str(item.get("comment") or "")[:500],
                    "identity_evidence_digest": str(item.get("identity_evidence_digest") or ""),
                },
            )
            for item in discrepancies
        ]
        documents.append(
            _document_blueprint(
                document_id=discrepancy_id,
                document_kind="transfer_discrepancy",
                document_role="china_discrepancy",
                root_document_id=document_id,
                lines=discrepancy_lines,
                movements=[],
                relation={
                    "parent_document_id": document_id,
                    "relation_type": "discrepancy_of",
                },
            )
        )
    guided_before: dict[str, Any] = {}
    if _is_guided_china_request(request):
        shipment = conn.execute(
            """SELECT shipment_id,actual_ff_acceptance_date,order_status,updated_at
               FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?""",
            (str(request["source_id"]),),
        ).fetchone()
        if shipment is None:
            raise FfPoolDocumentError(
                "supplier_shipment_not_found", "Source shipment is missing"
            )
        pool_before = []
        for movement in movements:
            row = conn.execute(
                f"SELECT quantity,capital_rub FROM {BALANCES_TABLE} "
                "WHERE projection_epoch=? AND facility_id=? AND pool=? AND nm_id=?",
                (
                    epoch,
                    str(movement["facility_id"]),
                    str(movement["pool"]),
                    int(movement["nm_id"]),
                ),
            ).fetchone()
            pool_before.append(
                {
                    "facility_id": str(movement["facility_id"]),
                    "pool": str(movement["pool"]),
                    "nm_id": int(movement["nm_id"]),
                    "quantity": int(row["quantity"]) if row is not None else 0,
                    "capital_rub": canonical_decimal_text(row["capital_rub"])
                    if row is not None else "0",
                }
            )
        aggregate_pool_parity = _guided_current_aggregate_parity_proof(conn)
        aggregate_version_id = str(
            aggregate_pool_parity["aggregate_version_id"]
        )
        aggregate_before = []
        for nm_id in sorted(capital_by_nm):
            row = conn.execute(
                """SELECT quantity,wac_rub,capital_rub,cost_covered_quantity,
                          quality,certified,wb_quantity,wb_in_way_to_client,
                          wb_in_way_from_client,provenance_json
                   FROM sheet_vitrina_v1_warehouse_functional_balances
                   WHERE version_id=? AND warehouse_key='ff' AND nm_id=?""",
                (aggregate_version_id, nm_id),
            ).fetchone()
            if row is None:
                aggregate_before.append(
                    {
                        "nm_id": nm_id,
                        "row_present": False,
                        "quantity": "0",
                        "wac_rub": None,
                        "capital_rub": "0",
                        "cost_covered_quantity": "0",
                        "quality": None,
                        "certified": None,
                        "wb_quantity": None,
                        "wb_in_way_to_client": None,
                        "wb_in_way_from_client": None,
                        "provenance_json": None,
                    }
                )
            else:
                aggregate_before.append(
                    {
                        "nm_id": nm_id,
                        "row_present": True,
                        "quantity": canonical_decimal_text(row["quantity"]),
                        "wac_rub": canonical_decimal_text(row["wac_rub"])
                        if row["wac_rub"] is not None else None,
                        "capital_rub": canonical_decimal_text(row["capital_rub"]),
                        "cost_covered_quantity": canonical_decimal_text(
                            row["cost_covered_quantity"]
                        ),
                        "quality": str(row["quality"]),
                        "certified": int(row["certified"]),
                        "wb_quantity": canonical_decimal_text(row["wb_quantity"]),
                        "wb_in_way_to_client": canonical_decimal_text(
                            row["wb_in_way_to_client"]
                        ),
                        "wb_in_way_from_client": canonical_decimal_text(
                            row["wb_in_way_from_client"]
                        ),
                        "provenance_json": str(row["provenance_json"]),
                    }
                )
        supplier_before = dict(shipment)
        dependent_state = _guided_dependent_state(
            conn,
            facility_id=facility_id,
            nm_ids=capital_by_nm,
        )
        guided_before = {
            "supplier": supplier_before,
            "supplier_digest": _fingerprint(supplier_before),
            "pool_balances": sorted(
                pool_before,
                key=lambda item: (item["facility_id"], item["pool"], item["nm_id"]),
            ),
            "aggregate_version_id": aggregate_version_id,
            "aggregate_balances": aggregate_before,
            "aggregate_pool_parity": aggregate_pool_parity,
            "dependent_reservation_debit_state": dependent_state,
        }
    return {
        "primary_document_id": document_id,
        "root_document_id": document_id,
        "documents": documents,
        "domain_manifest": {
            "facility_id": facility_id,
            "accepted_quantity": total_accepted_quantity,
            "accepted_capital_rub": _cents_text(total_accepted_capital),
            "exact_accepted_capital_rub": str(normalization["exact_total_rub"]),
            "capital_normalization_residual_rub": str(normalization["total_residual_rub"]),
            "capital_normalization_policy": str(normalization["policy"]),
            "capital_normalization_residual_owner_nm_ids": list(
                normalization["residual_owner_nm_ids"]
            ),
            "capital_normalization_by_nm": [
                {
                    "nm_id": nm_id,
                    "exact_capital_rub": exact_capital_by_nm[nm_id],
                    "canonical_capital_rub": _cents_text(capital_by_nm[nm_id]),
                    "normalization_residual_rub": residual_by_nm[nm_id],
                }
                for nm_id in sorted(capital_by_nm)
            ],
            "expense_rub": _cents_text(expense_total),
            "quantity_conserved": sum(int(item["quantity"]) for item in lines) == total_accepted_quantity,
            "capital_conserved": sum(int(item["capital_cents"]) for item in lines) == total_accepted_capital,
            "discrepancy_document_id": documents[1]["document_id"] if len(documents) > 1 else "",
            "actual_ff_acceptance_single_owner": "guided_china_acceptance",
            "guided_acceptance_before_state": guided_before,
        },
    }


def _plan_transfer_root(
    conn: sqlite3.Connection,
    *,
    request: Mapping[str, Any],
    manifest: Mapping[str, Any],
    epoch: int,
) -> dict[str, Any]:
    source = _location(conn, manifest.get("source"), require_active=True)
    destination = _location(conn, manifest.get("destination"), require_active=True)
    if source == destination:
        raise FfPoolDocumentError("transfer_same_location", "Transfer source and destination must differ")
    document_id = _request_document_id(request)
    document = _document_blueprint(
        document_id=document_id,
        document_kind="transfer_root",
        document_role="root",
        root_document_id=document_id,
        lines=[],
        movements=[],
    )
    return {
        "primary_document_id": document_id,
        "root_document_id": document_id,
        "documents": [document],
        "domain_manifest": {
            "source": {"facility_id": source[0], "pool": source[1]},
            "destination": {"facility_id": destination[0], "pool": destination[1]},
            "in_flight_projection": "derived_from_immutable_children",
            "transit_warehouse": False,
            "transit_reservation": False,
        },
    }


def _plan_transfer_shipment(
    conn: sqlite3.Connection,
    *,
    request: Mapping[str, Any],
    manifest: Mapping[str, Any],
    epoch: int,
) -> dict[str, Any]:
    root_id = str(manifest.get("root_document_id") or "").strip()
    root, source, destination = _transfer_root_context(conn, root_id)
    prior = conn.execute(
        f"SELECT child_document_id FROM {DOCUMENT_RELATIONS_TABLE} "
        "WHERE parent_document_id=? AND relation_type='shipment_of' LIMIT 1",
        (root_id,),
    ).fetchone()
    if prior is not None:
        raise FfPoolDocumentError(
            "transfer_shipment_exists",
            "Transfer root already has an immutable shipment child",
            details={"document_id": str(prior["child_document_id"])},
        )
    items = list(manifest.get("items") or [])
    if not items:
        raise FfPoolDocumentError("shipment_lines_required", "Transfer shipment is empty")
    expenses = _expense_lines(manifest.get("expenses") or [])
    weights: list[tuple[int, int]] = []
    frozen: dict[int, tuple[int, int, str]] = {}
    for item in items:
        nm_id = _positive_int(item.get("nm_id"), field="shipment nm_id")
        if nm_id in frozen:
            raise FfPoolDocumentError("duplicate_nm_id", "Shipment contains duplicate nmId")
        quantity = _positive_int(item.get("quantity"), field="shipment quantity")
        balance = _balance_row(conn, (source[0], source[1], nm_id), epoch=epoch, required=True)
        before_quantity = int(balance["quantity"])
        before_capital = _money_cents(balance["capital_rub"], field="source capital")
        if quantity > before_quantity or before_quantity <= 0 or before_capital <= 0:
            raise FfPoolDocumentError(
                "insufficient_source_balance",
                "Transfer source has insufficient positive quantity/capital",
                details={"nm_id": nm_id, "available": before_quantity, "requested": quantity},
            )
        frozen_capital = _proportional_cents(before_capital, quantity, before_quantity)
        frozen[nm_id] = (quantity, frozen_capital, _ratio_text(before_capital, before_quantity))
        weights.append((nm_id, quantity))
    expense_total = sum(int(item["amount_cents"]) for item in expenses)
    expense_allocations = _allocate_cents(expense_total, weights)
    lines: list[dict[str, Any]] = []
    movements: list[dict[str, Any]] = []
    for nm_id, quantity in sorted(weights):
        frozen_capital = frozen[nm_id][1]
        expense = int(expense_allocations[nm_id])
        lines.append(
            _document_line(
                role="shipped",
                facility_id=source[0],
                pool=source[1],
                nm_id=nm_id,
                quantity=quantity,
                capital_cents=frozen_capital,
                expense_cents=expense,
                metadata={
                    "destination_facility_id": destination[0],
                    "destination_pool": destination[1],
                    "frozen_source_wac_rub": frozen[nm_id][2],
                },
            )
        )
        movements.append(
            _movement(
                facility_id=source[0],
                pool=source[1],
                nm_id=nm_id,
                quantity_delta=-quantity,
                capital_delta_cents=-frozen_capital,
                wac_snapshot=frozen[nm_id][2],
                metadata={"root_document_id": root_id, "frozen_source_capital": True},
            )
        )
    document_id = _request_document_id(request)
    document = _document_blueprint(
        document_id=document_id,
        document_kind="transfer_shipment",
        document_role="shipment",
        root_document_id=root_id,
        relation={"parent_document_id": root_id, "relation_type": "shipment_of"},
        lines=lines,
        movements=movements,
        expenses=expenses,
    )
    return {
        "primary_document_id": document_id,
        "root_document_id": root_id,
        "documents": [document],
        "domain_manifest": {
            "source": {"facility_id": source[0], "pool": source[1]},
            "destination": {"facility_id": destination[0], "pool": destination[1]},
            "quantity": sum(quantity for _nm_id, quantity in weights),
            "frozen_source_capital_rub": _cents_text(sum(value[1] for value in frozen.values())),
            "expense_rub": _cents_text(expense_total),
            "open_balance_derived": True,
        },
    }


def _plan_transfer_receipt(
    conn: sqlite3.Connection,
    *,
    request: Mapping[str, Any],
    manifest: Mapping[str, Any],
    epoch: int,
) -> dict[str, Any]:
    return _plan_transfer_terminal(
        conn,
        request=request,
        manifest=manifest,
        epoch=epoch,
        document_kind="transfer_receipt",
        relation_type="receipt_of",
        line_role="received",
        destination_movement=True,
    )


def _plan_transfer_loss(
    conn: sqlite3.Connection,
    *,
    request: Mapping[str, Any],
    manifest: Mapping[str, Any],
    epoch: int,
) -> dict[str, Any]:
    return _plan_transfer_terminal(
        conn,
        request=request,
        manifest=manifest,
        epoch=epoch,
        document_kind="transfer_loss",
        relation_type="loss_of",
        line_role="lost",
        destination_movement=False,
    )


def _plan_transfer_terminal(
    conn: sqlite3.Connection,
    *,
    request: Mapping[str, Any],
    manifest: Mapping[str, Any],
    epoch: int,
    document_kind: str,
    relation_type: str,
    line_role: str,
    destination_movement: bool,
) -> dict[str, Any]:
    root_id = str(manifest.get("root_document_id") or "").strip()
    _root, source, destination = _transfer_root_context(conn, root_id)
    state = _transfer_state(conn, root_id)
    items = list(manifest.get("items") or [])
    if not items:
        raise FfPoolDocumentError("transfer_outcome_lines_required", "Transfer outcome is empty")
    seen: set[int] = set()
    lines: list[dict[str, Any]] = []
    movements: list[dict[str, Any]] = []
    for item in items:
        nm_id = _positive_int(item.get("nm_id"), field="outcome nm_id")
        if nm_id in seen:
            raise FfPoolDocumentError("duplicate_nm_id", "Transfer outcome contains duplicate nmId")
        seen.add(nm_id)
        quantity = _positive_int(item.get("quantity"), field="outcome quantity")
        shipment = state.get(nm_id)
        if shipment is None:
            raise FfPoolDocumentError("sku_not_shipped", "Outcome SKU was not shipped", details={"nm_id": nm_id})
        prior_terminal = int(shipment["terminal_quantity"])
        shipped_quantity = int(shipment["shipped_quantity"])
        if prior_terminal + quantity > shipped_quantity:
            raise FfPoolDocumentError(
                "transfer_outcome_exceeds_open",
                "Transfer outcome exceeds the remaining shipped quantity",
                details={"nm_id": nm_id, "open": shipped_quantity - prior_terminal, "requested": quantity},
            )
        capital = _component_share(
            int(shipment["capital_cents"]), shipped_quantity, prior_terminal, quantity
        )
        expense = sum(
            _component_share(
                int(component["amount_cents"]), shipped_quantity, prior_terminal, quantity
            )
            for component in shipment["expense_components"]
        )
        lines.append(
            _document_line(
                role=line_role,
                facility_id=destination[0] if destination_movement else source[0],
                pool=destination[1] if destination_movement else source[1],
                nm_id=nm_id,
                quantity=quantity,
                capital_cents=capital,
                expense_cents=expense,
                metadata={
                    "frozen_source_capital": True,
                    "terminal_quantity_before": prior_terminal,
                    "destination_facility_id": destination[0],
                    "destination_pool": destination[1],
                },
            )
        )
        if destination_movement:
            movements.append(
                _movement(
                    facility_id=destination[0],
                    pool=destination[1],
                    nm_id=nm_id,
                    quantity_delta=quantity,
                    capital_delta_cents=capital + expense,
                    wac_snapshot=_ratio_text(capital + expense, quantity),
                    metadata={
                        "root_document_id": root_id,
                        "frozen_source_capital_rub": _cents_text(capital),
                        "transfer_expense_rub": _cents_text(expense),
                    },
                )
            )
    document_id = _request_document_id(request)
    document = _document_blueprint(
        document_id=document_id,
        document_kind=document_kind,
        document_role=line_role,
        root_document_id=root_id,
        relation={"parent_document_id": root_id, "relation_type": relation_type},
        lines=lines,
        movements=movements,
    )
    return {
        "primary_document_id": document_id,
        "root_document_id": root_id,
        "documents": [document],
        "domain_manifest": {
            "source": {"facility_id": source[0], "pool": source[1]},
            "destination": {"facility_id": destination[0], "pool": destination[1]},
            "outcome": line_role,
            "quantity": sum(int(item["quantity"]) for item in lines),
            "frozen_source_capital_rub": _cents_text(sum(int(item["capital_cents"]) for item in lines)),
            "expense_rub": _cents_text(sum(int(item["expense_cents"]) for item in lines)),
        },
    }


def _plan_transfer_cancellation(
    conn: sqlite3.Connection,
    *,
    request: Mapping[str, Any],
    manifest: Mapping[str, Any],
    epoch: int,
) -> dict[str, Any]:
    root_id = str(manifest.get("root_document_id") or "").strip()
    _root, source, _destination = _transfer_root_context(conn, root_id)
    state = _transfer_state(conn, root_id)
    lines: list[dict[str, Any]] = []
    movements: list[dict[str, Any]] = []
    for nm_id, shipment in sorted(state.items()):
        shipped = int(shipment["shipped_quantity"])
        prior = int(shipment["terminal_quantity"])
        remaining = shipped - prior
        if remaining <= 0:
            continue
        capital = _component_share(int(shipment["capital_cents"]), shipped, prior, remaining)
        expense = sum(
            _component_share(int(component["amount_cents"]), shipped, prior, remaining)
            for component in shipment["expense_components"]
        )
        lines.append(
            _document_line(
                role="cancelled",
                facility_id=source[0],
                pool=source[1],
                nm_id=nm_id,
                quantity=remaining,
                capital_cents=capital,
                expense_cents=expense,
                metadata={"expense_terminalized_not_capitalized": True},
            )
        )
        movements.append(
            _movement(
                facility_id=source[0],
                pool=source[1],
                nm_id=nm_id,
                quantity_delta=remaining,
                capital_delta_cents=capital,
                wac_snapshot=_ratio_text(capital, remaining),
                metadata={"root_document_id": root_id, "return_frozen_source_capital": True},
            )
        )
    if not lines:
        raise FfPoolDocumentError("transfer_already_closed", "Transfer has no open quantity to cancel")
    document_id = _request_document_id(request)
    document = _document_blueprint(
        document_id=document_id,
        document_kind="transfer_cancellation",
        document_role="cancellation",
        root_document_id=root_id,
        relation={"parent_document_id": root_id, "relation_type": "cancellation_of"},
        lines=lines,
        movements=movements,
    )
    return {
        "primary_document_id": document_id,
        "root_document_id": root_id,
        "documents": [document],
        "domain_manifest": {"returned_quantity": sum(int(item["quantity"]) for item in lines)},
    }


def _plan_transfer_discrepancy(
    conn: sqlite3.Connection,
    *,
    request: Mapping[str, Any],
    manifest: Mapping[str, Any],
    epoch: int,
) -> dict[str, Any]:
    root_id = str(manifest.get("root_document_id") or "").strip()
    _root, source, destination = _transfer_root_context(conn, root_id)
    state = _transfer_state(conn, root_id)
    expected = list(manifest.get("expected_not_sent") or [])
    unexpected = list(manifest.get("unexpected") or [])
    if not expected and not unexpected:
        raise FfPoolDocumentError("discrepancy_lines_required", "Discrepancy child is empty")
    lines: list[dict[str, Any]] = []
    movements: list[dict[str, Any]] = []
    displaced_expense_cents = 0
    seen_expected: set[int] = set()
    for item in expected:
        nm_id = _positive_int(item.get("nm_id"), field="expected nm_id")
        if nm_id in seen_expected:
            raise FfPoolDocumentError("duplicate_nm_id", "Expected-not-sent contains duplicate nmId")
        seen_expected.add(nm_id)
        quantity = _positive_int(item.get("quantity"), field="expected-not-sent quantity")
        shipment = state.get(nm_id)
        if shipment is None:
            raise FfPoolDocumentError("sku_not_shipped", "Expected-not-sent SKU was not shipped")
        prior = int(shipment["terminal_quantity"])
        shipped = int(shipment["shipped_quantity"])
        if prior + quantity > shipped:
            raise FfPoolDocumentError("transfer_outcome_exceeds_open", "Expected-not-sent exceeds open quantity")
        capital = _component_share(int(shipment["capital_cents"]), shipped, prior, quantity)
        expense = sum(
            _component_share(int(component["amount_cents"]), shipped, prior, quantity)
            for component in shipment["expense_components"]
        )
        displaced_expense_cents += expense
        lines.append(
            _document_line(
                role="expected_not_sent",
                facility_id=source[0],
                pool=source[1],
                nm_id=nm_id,
                quantity=quantity,
                capital_cents=capital,
                expense_cents=expense,
            )
        )
        movements.append(
            _movement(
                facility_id=source[0],
                pool=source[1],
                nm_id=nm_id,
                quantity_delta=quantity,
                capital_delta_cents=capital,
                wac_snapshot=_ratio_text(capital, quantity),
                metadata={"mis_sort_expected_not_sent": True, "root_document_id": root_id},
            )
        )
    seen_unexpected: set[int] = set()
    unexpected_specs: list[dict[str, Any]] = []
    for item in unexpected:
        nm_id = _positive_int(item.get("nm_id"), field="unexpected nm_id")
        if nm_id in seen_unexpected:
            raise FfPoolDocumentError("duplicate_nm_id", "Unexpected lines contain duplicate nmId")
        seen_unexpected.add(nm_id)
        quantity = _positive_int(item.get("quantity"), field="unexpected quantity")
        balance = _balance_row(conn, (source[0], source[1], nm_id), epoch=epoch, required=True)
        before_q = int(balance["quantity"])
        before_c = _money_cents(balance["capital_rub"], field="unexpected source capital")
        if quantity > before_q or before_q <= 0 or before_c <= 0:
            raise FfPoolDocumentError(
                "mis_sort_insufficient_source",
                "Unexpected SKU has insufficient positive source quantity/capital",
                details={"nm_id": nm_id, "available": before_q, "requested": quantity},
            )
        capital = _proportional_cents(before_c, quantity, before_q)
        wac = _ratio_text(before_c, before_q)
        unexpected_specs.append(
            {
                "nm_id": nm_id,
                "quantity": quantity,
                "capital_cents": capital,
                "source_wac_rub": wac,
            }
        )
    unexpected_expense_allocations = _allocate_cents(
        displaced_expense_cents,
        [(int(item["nm_id"]), int(item["quantity"])) for item in unexpected_specs],
    ) if unexpected_specs else {}
    for item in unexpected_specs:
        nm_id = int(item["nm_id"])
        quantity = int(item["quantity"])
        capital = int(item["capital_cents"])
        wac = str(item["source_wac_rub"])
        expense = int(unexpected_expense_allocations.get(nm_id, 0))
        lines.append(
            _document_line(
                role="unexpected",
                facility_id=destination[0],
                pool=destination[1],
                nm_id=nm_id,
                quantity=quantity,
                capital_cents=capital,
                expense_cents=expense,
                metadata={
                    "source_current_wac_rub": wac,
                    "reallocated_transfer_expense_rub": _cents_text(expense),
                },
            )
        )
        movements.extend(
            [
                _movement(
                    facility_id=source[0],
                    pool=source[1],
                    nm_id=nm_id,
                    quantity_delta=-quantity,
                    capital_delta_cents=-capital,
                    wac_snapshot=wac,
                    metadata={"mis_sort_unexpected_debit": True, "root_document_id": root_id},
                ),
                _movement(
                    facility_id=destination[0],
                    pool=destination[1],
                    nm_id=nm_id,
                    quantity_delta=quantity,
                    capital_delta_cents=capital + expense,
                    wac_snapshot=_ratio_text(capital + expense, quantity),
                    metadata={
                        "mis_sort_unexpected_receipt": True,
                        "root_document_id": root_id,
                        "source_current_wac_rub": wac,
                        "reallocated_transfer_expense_rub": _cents_text(expense),
                    },
                ),
            ]
        )
    document_id = _request_document_id(request)
    document = _document_blueprint(
        document_id=document_id,
        document_kind="transfer_discrepancy",
        document_role="discrepancy",
        root_document_id=root_id,
        relation={"parent_document_id": root_id, "relation_type": "discrepancy_of"},
        lines=lines,
        movements=movements,
    )
    return {
        "primary_document_id": document_id,
        "root_document_id": root_id,
        "documents": [document],
        "domain_manifest": {
            "expected_not_sent_quantity": sum(int(item["quantity"]) for item in lines if item["line_role"] == "expected_not_sent"),
            "unexpected_quantity": sum(int(item["quantity"]) for item in lines if item["line_role"] == "unexpected"),
            "reallocated_transfer_expense_rub": _cents_text(
                sum(int(item["expense_cents"]) for item in lines if item["line_role"] == "unexpected")
            ),
            "zero_or_synthetic_cost": False,
        },
    }


def _plan_pool_reallocation(
    conn: sqlite3.Connection,
    *,
    request: Mapping[str, Any],
    manifest: Mapping[str, Any],
    epoch: int,
) -> dict[str, Any]:
    facility_id = _facility(conn, str(manifest.get("facility_id") or ""), require_active=True)
    source_pool = _pool(str(manifest.get("source_pool") or ""))
    destination_pool = _pool(str(manifest.get("destination_pool") or ""))
    if source_pool == destination_pool:
        raise FfPoolDocumentError("reallocation_same_pool", "Reallocation pools must differ")
    items = list(manifest.get("items") or [])
    if not items:
        raise FfPoolDocumentError("reallocation_lines_required", "Pool reallocation is empty")
    expenses = _expense_lines(manifest.get("expenses") or [])
    weights: list[tuple[int, int]] = []
    frozen: dict[int, tuple[int, str]] = {}
    seen: set[int] = set()
    physical_before = _facility_quantity(conn, facility_id=facility_id, epoch=epoch)
    for item in items:
        nm_id = _positive_int(item.get("nm_id"), field="reallocation nm_id")
        if nm_id in seen:
            raise FfPoolDocumentError("duplicate_nm_id", "Reallocation contains duplicate nmId")
        seen.add(nm_id)
        quantity = _positive_int(item.get("quantity"), field="reallocation quantity")
        balance = _balance_row(conn, (facility_id, source_pool, nm_id), epoch=epoch, required=True)
        before_q = int(balance["quantity"])
        before_c = _money_cents(balance["capital_rub"], field="source capital")
        if quantity > before_q or before_q <= 0 or before_c <= 0:
            raise FfPoolDocumentError(
                "insufficient_source_balance",
                "Source pool has insufficient positive quantity/capital",
                details={"nm_id": nm_id, "available": before_q, "requested": quantity},
            )
        capital = _proportional_cents(before_c, quantity, before_q)
        frozen[nm_id] = (capital, _ratio_text(before_c, before_q))
        weights.append((nm_id, quantity))
    expense_total = sum(int(item["amount_cents"]) for item in expenses)
    expense_allocations = _allocate_cents(expense_total, weights)
    lines: list[dict[str, Any]] = []
    movements: list[dict[str, Any]] = []
    for nm_id, quantity in sorted(weights):
        capital, wac = frozen[nm_id]
        expense = int(expense_allocations[nm_id])
        lines.append(
            _document_line(
                role="reallocated",
                facility_id=facility_id,
                pool=destination_pool,
                nm_id=nm_id,
                quantity=quantity,
                capital_cents=capital,
                expense_cents=expense,
                metadata={"source_pool": source_pool, "destination_pool": destination_pool},
            )
        )
        movements.extend(
            [
                _movement(
                    facility_id=facility_id,
                    pool=source_pool,
                    nm_id=nm_id,
                    quantity_delta=-quantity,
                    capital_delta_cents=-capital,
                    wac_snapshot=wac,
                    metadata={"pool_reallocation_source": True},
                ),
                _movement(
                    facility_id=facility_id,
                    pool=destination_pool,
                    nm_id=nm_id,
                    quantity_delta=quantity,
                    capital_delta_cents=capital + expense,
                    wac_snapshot=_ratio_text(capital + expense, quantity),
                    metadata={"pool_reallocation_destination": True, "expense_rub": _cents_text(expense)},
                ),
            ]
        )
    document_id = _request_document_id(request)
    document = _document_blueprint(
        document_id=document_id,
        document_kind="pool_reallocation",
        document_role="root",
        root_document_id=document_id,
        lines=lines,
        movements=movements,
        expenses=expenses,
    )
    return {
        "primary_document_id": document_id,
        "root_document_id": document_id,
        "documents": [document],
        "domain_manifest": {
            "facility_id": facility_id,
            "source_pool": source_pool,
            "destination_pool": destination_pool,
            "facility_physical_quantity_before": physical_before,
            "facility_physical_quantity_after": physical_before,
            "physical_total_unchanged": True,
            "expense_rub": _cents_text(expense_total),
        },
    }


def _dense_fbs_initialization(
    request: Mapping[str, Any], manifest: Mapping[str, Any]
) -> dict[str, Any]:
    raw = manifest.get("dense_fbs_initialization")
    if raw is None:
        return {}
    dense = _json_object(raw)
    if (
        dense.get("contract_name") != "ff_pool_dense_fbs_initialization_v1"
        or str(request["source_system"] or "") != "wb_core_dense_fbs"
        or str(request["source_type"] or "")
        != "ff_pool_dense_fbs_initialization_v1"
        or str(dense.get("plan_fingerprint") or "")
        != str(request["source_revision"] or "")
        or str(dense.get("effective_from") or "")
        != str(request["business_date"] or "")
        or str(manifest.get("scope") or "") != "FBS"
        or not str(dense.get("intent_id") or "")
        or not str(dense.get("roster_fingerprint") or "").startswith("sha256:")
        or not str(dense.get("effective_from") or "")
    ):
        raise FfPoolDocumentError(
            "dense_fbs_initialization_contract_invalid",
            "Dense FBS inventory initialization contract is invalid",
        )
    return dense


def _dense_fbs_balance_cas_row(
    conn: sqlite3.Connection,
    *,
    facility_id: str,
    nm_id: int,
    epoch: int,
) -> dict[str, Any]:
    row = _balance_row(
        conn,
        (str(facility_id), "FBS", int(nm_id)),
        epoch=epoch,
        required=False,
    )
    if row is None:
        return {"nm_id": int(nm_id), "row_present": False}
    return {
        "nm_id": int(nm_id),
        "row_present": True,
        "projection_epoch": int(row["projection_epoch"]),
        "quantity": int(row["quantity"]),
        "capital_rub": str(row["capital_rub"]),
        "wac_rub": row["wac_rub"],
        "source_watermark": str(row["source_watermark"]),
        "updated_at": str(row["updated_at"]),
    }


def _plan_pool_inventory(
    conn: sqlite3.Connection,
    *,
    request: Mapping[str, Any],
    manifest: Mapping[str, Any],
    epoch: int,
) -> dict[str, Any]:
    dense_initialization = _dense_fbs_initialization(request, manifest)
    facility_id = _facility(
        conn,
        str(manifest.get("facility_id") or ""),
        require_active=not bool(dense_initialization),
    )
    scope = _scope(str(manifest.get("scope") or ""))
    selected_pools = POOLS if scope == "both" else (scope,)
    targets = list(manifest.get("targets") or [])
    if not targets:
        raise FfPoolDocumentError("inventory_targets_required", "Inventory target table is empty")
    by_nm: dict[int, Mapping[str, Any]] = {}
    for item in targets:
        nm_id = _positive_int(item.get("nm_id"), field="inventory nm_id")
        if nm_id in by_nm:
            raise FfPoolDocumentError("duplicate_nm_id", "Inventory contains duplicate resolved SKU")
        by_nm[nm_id] = item
    if dense_initialization:
        expected_nm_ids = sorted(
            int(value)
            for value in dense_initialization.get("applicable_nm_ids") or []
        )
        if sorted(by_nm) != expected_nm_ids:
            raise FfPoolDocumentError(
                "dense_fbs_roster_drift",
                "Dense FBS inventory targets differ from the pinned applicable roster",
            )
        expected_rows = {
            int(item["nm_id"]): dict(item)
            for item in dense_initialization.get("expected_balance_rows") or []
            if isinstance(item, Mapping)
        }
        if sorted(expected_rows) != sorted(by_nm):
            raise FfPoolDocumentError(
                "dense_fbs_balance_cas_invalid",
                "Dense FBS balance CAS does not cover the complete target roster",
            )
        live_rows = {
            nm_id: _dense_fbs_balance_cas_row(
                conn,
                facility_id=facility_id,
                nm_id=nm_id,
                epoch=epoch,
            )
            for nm_id in sorted(by_nm)
        }
        if live_rows != expected_rows:
            raise FfPoolDocumentError(
                "dense_fbs_balance_drift",
                "Dense FBS physical rows changed after the staged plan",
                details={
                    "expected_fingerprint": _fingerprint(expected_rows),
                    "current_fingerprint": _fingerprint(live_rows),
                },
            )
    cost_bases = _json_object(manifest.get("cost_basis_by_nm") or {})
    root_document_id = _request_document_id(request)
    parent_lines: list[dict[str, Any]] = []
    surplus_lines: list[dict[str, Any]] = []
    surplus_movements: list[dict[str, Any]] = []
    shortage_lines: list[dict[str, Any]] = []
    shortage_movements: list[dict[str, Any]] = []
    explicit_zero_balances: list[dict[str, Any]] = []
    unselected = tuple(pool for pool in POOLS if pool not in selected_pools)
    unselected_keys = [(facility_id, pool, nm_id) for nm_id in sorted(by_nm) for pool in unselected]
    unselected_digest = _balance_digest(conn, unselected_keys)
    for nm_id, item in sorted(by_nm.items()):
        for pool in selected_pools:
            field = "target_fbs" if pool == "FBS" else "target_fbo"
            target = _nonnegative_int(item.get(field), field=f"{pool} target")
            balance = _balance_row(conn, (facility_id, pool, nm_id), epoch=epoch, required=False)
            before_q = int(balance["quantity"]) if balance is not None else 0
            before_c = _money_cents(balance["capital_rub"], field="inventory before capital") if balance is not None else 0
            parent_lines.append(
                _document_line(
                    role="absolute_target",
                    facility_id=facility_id,
                    pool=pool,
                    nm_id=nm_id,
                    quantity=target,
                    capital_cents=0,
                    metadata={
                        "before_quantity": before_q,
                        "selected_pool": True,
                        **(
                            {"explicit_physical_zero": True}
                            if pool == "FBS" and balance is None and target == 0
                            else {}
                        ),
                    },
                )
            )
            delta = target - before_q
            if dense_initialization and delta != 0:
                raise FfPoolDocumentError(
                    "dense_fbs_nonzero_delta_blocked",
                    "Dense FBS initialization may only retain quantity or materialize zero",
                    details={
                        "facility_id": facility_id,
                        "nm_id": nm_id,
                        "before_quantity": before_q,
                        "target_quantity": target,
                    },
                )
            if pool == "FBS" and balance is None and target == 0:
                explicit_zero_balances.append(
                    {"facility_id": facility_id, "pool": pool, "nm_id": nm_id}
                )
            if delta > 0:
                unit_cost = _inventory_cost_basis(
                    conn,
                    facility_id=facility_id,
                    nm_id=nm_id,
                    epoch=epoch,
                    explicit=cost_bases.get(str(nm_id), cost_bases.get(nm_id)),
                )
                capital = _decimal_to_cents(unit_cost * Decimal(delta), field="inventory surplus capital", positive=True)
                surplus_lines.append(
                    _document_line(
                        role="inventory_surplus",
                        facility_id=facility_id,
                        pool=pool,
                        nm_id=nm_id,
                        quantity=delta,
                        capital_cents=capital,
                        metadata={"positive_same_sku_cost_basis_rub": canonical_decimal_text(unit_cost)},
                    )
                )
                surplus_movements.append(
                    _movement(
                        facility_id=facility_id,
                        pool=pool,
                        nm_id=nm_id,
                        quantity_delta=delta,
                        capital_delta_cents=capital,
                        wac_snapshot=canonical_decimal_text(unit_cost),
                        metadata={"inventory_surplus": True, "parent_document_id": root_document_id},
                    )
                )
            elif delta < 0:
                quantity = -delta
                if before_q <= 0 or before_c <= 0:
                    raise FfPoolDocumentError(
                        "inventory_shortage_cost_missing",
                        "Inventory shortage requires positive same-SKU source capital",
                        details={"facility_id": facility_id, "pool": pool, "nm_id": nm_id},
                    )
                capital = _proportional_cents(before_c, quantity, before_q)
                shortage_lines.append(
                    _document_line(
                        role="inventory_shortage",
                        facility_id=facility_id,
                        pool=pool,
                        nm_id=nm_id,
                        quantity=quantity,
                        capital_cents=capital,
                        metadata={"positive_same_sku_cost_basis_rub": _ratio_text(before_c, before_q)},
                    )
                )
                shortage_movements.append(
                    _movement(
                        facility_id=facility_id,
                        pool=pool,
                        nm_id=nm_id,
                        quantity_delta=-quantity,
                        capital_delta_cents=-capital,
                        wac_snapshot=_ratio_text(before_c, before_q),
                        metadata={"inventory_shortage": True, "parent_document_id": root_document_id},
                    )
                )
    documents = [
        _document_blueprint(
            document_id=root_document_id,
            document_kind="pool_inventory",
            document_role="root",
            root_document_id=root_document_id,
            lines=parent_lines,
            movements=[],
        )
    ]
    if surplus_lines:
        child_id = root_document_id + "_surplus"
        documents.append(
            _document_blueprint(
                document_id=child_id,
                document_kind="inventory_surplus",
                document_role="surplus",
                root_document_id=root_document_id,
                relation={"parent_document_id": root_document_id, "relation_type": "inventory_surplus_of"},
                lines=surplus_lines,
                movements=surplus_movements,
            )
        )
    if shortage_lines:
        child_id = root_document_id + "_shortage"
        documents.append(
            _document_blueprint(
                document_id=child_id,
                document_kind="inventory_shortage",
                document_role="shortage",
                root_document_id=root_document_id,
                relation={"parent_document_id": root_document_id, "relation_type": "inventory_shortage_of"},
                lines=shortage_lines,
                movements=shortage_movements,
            )
        )
    result = {
        "primary_document_id": root_document_id,
        "root_document_id": root_document_id,
        "documents": documents,
        "unselected_keys": unselected_keys,
        "unselected_digest": unselected_digest,
        "domain_manifest": {
            "facility_id": facility_id,
            "scope": scope,
            "selected_pools": list(selected_pools),
            "unselected_pools": list(unselected),
            "unselected_digest": unselected_digest,
            "target_count": len(parent_lines),
            "surplus_count": len(surplus_lines),
            "shortage_count": len(shortage_lines),
            "explicit_zero_fbs_balance_count": len(explicit_zero_balances),
            "explicit_zero_fbs_nm_ids": [
                int(item["nm_id"]) for item in explicit_zero_balances
            ],
            "zero_or_synthetic_cost": False,
            **(
                {
                    "dense_fbs_initialization": dense_initialization,
                    "dense_fbs_roster_fingerprint": str(
                        dense_initialization.get("roster_fingerprint") or ""
                    ),
                }
                if dense_initialization
                else {}
            ),
        },
    }
    if explicit_zero_balances:
        result["additional_balance_keys"] = [
            [item["facility_id"], item["pool"], item["nm_id"]]
            for item in explicit_zero_balances
        ]
        result["explicit_zero_balances"] = explicit_zero_balances
    return result


def _plan_pool_overhead(
    conn: sqlite3.Connection,
    *,
    request: Mapping[str, Any],
    manifest: Mapping[str, Any],
    epoch: int,
) -> dict[str, Any]:
    facility_id = _facility(conn, str(manifest.get("facility_id") or ""), require_active=True)
    scope = _scope(str(manifest.get("scope") or ""))
    selected_pools = POOLS if scope == "both" else (scope,)
    amount_cents = _money_cents(manifest.get("amount_rub"), field="overhead amount", positive=True)
    category, category_label, comment, source_mode, payment_evidence = _overhead_metadata(
        conn,
        request=request,
        manifest=manifest,
        amount_cents=amount_cents,
    )
    reason = category_label + (f": {comment}" if comment else "")
    eligible: dict[str, list[tuple[int, int]]] = {pool: [] for pool in selected_pools}
    basis_rows: list[dict[str, Any]] = []
    missing_cost_rows: list[dict[str, Any]] = []
    for pool in selected_pools:
        rows = conn.execute(
            f"SELECT nm_id,quantity,capital_rub FROM {BALANCES_TABLE} "
            "WHERE projection_epoch=? AND facility_id=? AND pool=? AND quantity>0 "
            "ORDER BY nm_id",
            (epoch, facility_id, pool),
        ).fetchall()
        for row in rows:
            quantity = int(row["quantity"])
            try:
                capital = Decimal(str(row["capital_rub"]))
            except (InvalidOperation, ValueError):
                capital = Decimal("NaN")
            basis = {
                "pool": pool,
                "nm_id": int(row["nm_id"]),
                "quantity": quantity,
                "capital_rub": (
                    canonical_decimal_text(capital) if capital.is_finite() else "unavailable"
                ),
            }
            basis_rows.append(basis)
            if not capital.is_finite() or capital <= ZERO:
                missing_cost_rows.append(basis)
                continue
            eligible[pool].append((int(row["nm_id"]), quantity))
    if missing_cost_rows:
        raise FfPoolDocumentError(
            "overhead_positive_quantity_cost_basis_missing",
            "Every positive physical SKU in the selected scope requires positive capital",
            details={
                "facility_id": facility_id,
                "scope": scope,
                "rows": missing_cost_rows[:100],
                "row_count": len(missing_cost_rows),
            },
        )
    pool_weights = [(pool, sum(quantity for _nm_id, quantity in eligible[pool])) for pool in selected_pools]
    if not any(weight > 0 for _pool_name, weight in pool_weights):
        raise FfPoolDocumentError(
            "overhead_positive_quantity_required",
            "Scoped overhead requires positive physical quantity",
        )
    pool_allocations = _allocate_cents(amount_cents, pool_weights)
    allocations: dict[tuple[str, int], int] = {}
    for pool, _weight in pool_weights:
        pool_amount = int(pool_allocations.get(pool, 0))
        if pool_amount:
            sku_allocations = _allocate_cents(pool_amount, eligible[pool])
            for nm_id, _quantity in eligible[pool]:
                allocations[(pool, nm_id)] = int(sku_allocations[nm_id])
    lines: list[dict[str, Any]] = []
    movements: list[dict[str, Any]] = []
    for pool in selected_pools:
        for nm_id, quantity in eligible[pool]:
            allocation = int(allocations.get((pool, nm_id), 0))
            if not allocation:
                continue
            lines.append(
                _document_line(
                    role="overhead_allocation",
                    facility_id=facility_id,
                    pool=pool,
                    nm_id=nm_id,
                    quantity=quantity,
                    capital_cents=allocation,
                    expense_cents=allocation,
                    metadata={"positive_physical_denominator": quantity, "reason": reason},
                )
            )
            movements.append(
                _movement(
                    facility_id=facility_id,
                    pool=pool,
                    nm_id=nm_id,
                    quantity_delta=0,
                    capital_delta_cents=allocation,
                    wac_snapshot=None,
                    metadata={"pool_scoped_overhead": True, "basis_quantity": quantity, "reason": reason},
                )
            )
    if sum(int(item["capital_cents"]) for item in lines) != amount_cents:
        raise FfPoolDocumentError("overhead_allocation_does_not_conserve", "Overhead allocation failed conservation")
    document_id = _request_document_id(request)
    expenses = [
        {
            "amount_cents": amount_cents,
            "basis": reason,
            "source_file_sha256": str(request["source_sha256"] or ""),
            "source_filename": str(request["source_filename"] or ""),
            "metadata": {
                "category": category,
                "category_label_ru": category_label,
                "comment": comment,
                "source_mode": source_mode,
                "payment_fingerprint": str(payment_evidence.get("payment_fingerprint") or ""),
                "fingerprint_version": str(payment_evidence.get("fingerprint_version") or ""),
                "parser_version": str(payment_evidence.get("parser_version") or ""),
            },
        }
    ]
    document = _document_blueprint(
        document_id=document_id,
        document_kind="pool_overhead",
        document_role="root",
        root_document_id=document_id,
        lines=lines,
        movements=movements,
        expenses=expenses,
    )
    return {
        "primary_document_id": document_id,
        "root_document_id": document_id,
        "documents": [document],
        "additional_balance_keys": [
            (facility_id, str(item["pool"]), int(item["nm_id"]))
            for item in basis_rows
        ],
        "domain_manifest": {
            "facility_id": facility_id,
            "scope": scope,
            "category": category,
            "category_label_ru": category_label,
            "comment": comment,
            "source_mode": source_mode,
            "amount_rub": _cents_text(amount_cents),
            "denominator_quantity": sum(weight for _pool, weight in pool_weights),
            "denominator_sku_count": len(basis_rows),
            "affected_sku_count": len(lines),
            "allocation_total_rub": _cents_text(
                sum(int(item["capital_cents"]) for item in lines)
            ),
            "pool_allocations_rub": {
                pool: _cents_text(int(pool_allocations.get(pool, 0)))
                for pool in selected_pools
            },
            "basis_digest": _fingerprint(basis_rows),
            "payment_evidence": payment_evidence,
            "positive_quantity_only": True,
            "reservation_excluded": True,
            "amount_conserved": True,
        },
    }


def _overhead_metadata(
    conn: sqlite3.Connection,
    *,
    request: Mapping[str, Any],
    manifest: Mapping[str, Any],
    amount_cents: int,
) -> tuple[str, str, str, str, dict[str, Any]]:
    category = str(manifest.get("category") or "").strip()
    if category not in OVERHEAD_EXPENSE_CATEGORIES:
        raise FfPoolDocumentError(
            "invalid_overhead_category",
            "Overhead category must be one stable supported code",
            details={"allowed": list(OVERHEAD_EXPENSE_CATEGORIES)},
        )
    comment = _overhead_comment(manifest.get("comment"))
    if category == "other" and not comment:
        raise FfPoolDocumentError(
            "overhead_other_comment_required",
            "Comment is required when overhead category is other",
        )
    source_mode = str(manifest.get("source_mode") or "").strip()
    if source_mode not in OVERHEAD_SOURCE_MODES:
        raise FfPoolDocumentError(
            "invalid_overhead_source_mode",
            "Overhead source_mode must be manual or payment_order_pdf",
        )
    evidence = _payment_evidence_from_manifest(manifest)
    source_sha256 = str(request["source_sha256"] or "")
    if source_mode == "manual":
        if evidence or source_sha256 or str(request["source_filename"] or ""):
            raise FfPoolDocumentError(
                "manual_overhead_has_file_evidence",
                "Manual overhead must not contain payment-order file evidence",
            )
        return (
            category,
            OVERHEAD_EXPENSE_CATEGORY_LABELS_RU[category],
            comment,
            source_mode,
            {},
        )
    required = {
        "parse_status": RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_PARSED,
        "execution_status": RUSSIAN_PAYMENT_ORDER_EXECUTION_EXECUTED,
        "currency": RUSSIAN_PAYMENT_ORDER_CURRENCY,
        "parser_version": RUSSIAN_PAYMENT_ORDER_PARSER_VERSION,
        "fingerprint_version": RUSSIAN_PAYMENT_ORDER_FINGERPRINT_VERSION,
    }
    mismatches = {
        key: {"expected": expected, "actual": evidence.get(key)}
        for key, expected in required.items()
        if evidence.get(key) != expected
    }
    if evidence.get("posting_eligible") is not True:
        mismatches["posting_eligible"] = {
            "expected": True,
            "actual": evidence.get("posting_eligible"),
        }
    payment_fingerprint = _payment_fingerprint_from_manifest(manifest)
    file_sha256 = str(evidence.get("file_sha256") or "")
    source_blob = request["source_file_blob"]
    source_type = str(request["source_type"] or "")
    source_filename = str(request["source_filename"] or "")
    source_content_type = str(request["source_content_type"] or "").split(";", 1)[0].lower()
    if source_type != "ff_pool_overhead_payment_order_pdf":
        mismatches["source_type"] = {
            "expected": "ff_pool_overhead_payment_order_pdf",
            "actual": source_type,
        }
    if not source_filename.lower().endswith(".pdf"):
        mismatches["source_filename"] = {
            "expected": "*.pdf",
            "actual": source_filename,
        }
    if source_content_type not in {"application/pdf", "application/octet-stream"}:
        mismatches["source_content_type"] = {
            "expected": "application/pdf",
            "actual": source_content_type,
        }
    if source_blob is None or _sha256(bytes(source_blob)) != source_sha256:
        mismatches["source_blob"] = {
            "expected": source_sha256,
            "actual": _sha256(bytes(source_blob)) if source_blob is not None else "",
        }
    if not payment_fingerprint:
        mismatches["payment_fingerprint"] = {"expected": "sha256:<hex>", "actual": ""}
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", file_sha256) or file_sha256 != source_sha256:
        mismatches["file_sha256"] = {"expected": source_sha256, "actual": file_sha256}
    if _money_cents(evidence.get("amount"), field="payment order amount", positive=True) != amount_cents:
        mismatches["amount"] = {
            "expected": _cents_text(amount_cents),
            "actual": str(evidence.get("amount") or ""),
        }
    if mismatches:
        raise FfPoolDocumentError(
            "overhead_payment_evidence_not_eligible",
            "Payment-order evidence is not eligible for overhead posting",
            details={"mismatches": mismatches},
        )
    stored = conn.execute(
        f"""SELECT request_id,file_sha256,parser_version,fingerprint_version,
                   normalized_parser_json
            FROM {OVERHEAD_PAYMENT_EVIDENCE_TABLE}
            WHERE payment_fingerprint=?""",
        (payment_fingerprint,),
    ).fetchone()
    if (
        stored is None
        or str(stored["request_id"]) != str(request["request_id"])
        or str(stored["file_sha256"]) != file_sha256
        or str(stored["parser_version"]) != RUSSIAN_PAYMENT_ORDER_PARSER_VERSION
        or str(stored["fingerprint_version"]) != RUSSIAN_PAYMENT_ORDER_FINGERPRINT_VERSION
        or _json_object(_loads(stored["normalized_parser_json"], {})) != evidence
    ):
        raise FfPoolDocumentError(
            "overhead_payment_evidence_drift",
            "Payment-order evidence or duplicate ownership changed after acceptance",
            details={"payment_fingerprint": payment_fingerprint},
        )
    return (
        category,
        OVERHEAD_EXPENSE_CATEGORY_LABELS_RU[category],
        comment,
        source_mode,
        evidence,
    )


def _plan_storno(
    conn: sqlite3.Connection,
    *,
    request: Mapping[str, Any],
    manifest: Mapping[str, Any],
    epoch: int,
) -> dict[str, Any]:
    target_id = str(manifest.get("target_document_id") or "").strip()
    target = _load_document(conn, target_id)
    if target is None:
        raise FfPoolDocumentError("target_document_not_found", "Storno target document was not found")
    existing = conn.execute(
        f"SELECT child_document_id FROM {DOCUMENT_RELATIONS_TABLE} "
        "WHERE parent_document_id=? AND relation_type='storno_of' LIMIT 1",
        (target_id,),
    ).fetchone()
    if existing is not None:
        raise FfPoolDocumentError(
            "storno_exists",
            "Target document already has an immutable storno",
            details={"document_id": str(existing["child_document_id"])},
        )
    if str(target["document_kind"]) == "transfer_shipment":
        terminal = conn.execute(
            f"SELECT 1 FROM {DOCUMENT_LINES_TABLE} WHERE root_document_id=? "
            "AND line_role IN ('received','lost','cancelled','expected_not_sent') LIMIT 1",
            (str(target["root_document_id"]),),
        ).fetchone()
        if terminal is not None:
            raise FfPoolDocumentError(
                "shipment_storno_has_outcomes",
                "Shipment with terminal children must be corrected append-only by root outcomes",
            )
    if str(target["document_kind"]) == "transfer_receipt":
        active_late_expense_rows = conn.execute(
            f"""SELECT line.metadata_json
                FROM {LINES_TABLE} AS line
                JOIN {DOCUMENTS_TABLE} AS document
                  ON document.operation_id=line.operation_id
                WHERE document.root_document_id=?
                  AND document.document_kind='late_expense'
                  AND NOT EXISTS(
                    SELECT 1 FROM {DOCUMENT_RELATIONS_TABLE} AS relation
                    WHERE relation.parent_document_id=document.document_id
                      AND relation.relation_type='storno_of'
                  )""",
            (str(target["root_document_id"]),),
        ).fetchall()
        if any(
            str(_loads(row["metadata_json"], {}).get("received_document_id") or "") == target_id
            for row in active_late_expense_rows
        ):
            raise FfPoolDocumentError(
                "receipt_storno_has_active_late_expense",
                "Storno linked late-expense documents before reversing this receipt",
                details={"target_document_id": target_id},
            )
    source_lines = conn.execute(
        f"SELECT * FROM {LINES_TABLE} WHERE operation_id=? ORDER BY line_no",
        (str(target["operation_id"]),),
    ).fetchall()
    if str(target["document_kind"]) == "pool_overhead":
        _assert_overhead_storno_has_no_dependent_handoff(
            conn,
            target=target,
            source_lines=source_lines,
        )
    guided_recovery = _guided_acceptance_recovery_context(
        conn,
        target=target,
        source_lines=source_lines,
    )
    movements: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    for source in source_lines:
        quantity_delta = -int(source["quantity_delta"])
        capital_cents = -_money_cents(source["capital_delta_rub"], field="storno capital")
        movements.append(
            _movement(
                facility_id=str(source["facility_id"]),
                pool=str(source["pool"]),
                nm_id=int(source["nm_id"]),
                quantity_delta=quantity_delta,
                capital_delta_cents=capital_cents,
                wac_snapshot=str(source["wac_snapshot_rub"] or "") or None,
                metadata={"exact_original_movement_storno": True, "target_document_id": target_id},
            )
        )
        lines.append(
            _document_line(
                role="storno",
                facility_id=str(source["facility_id"]),
                pool=str(source["pool"]),
                nm_id=int(source["nm_id"]),
                quantity=abs(int(source["quantity_delta"])),
                capital_cents=abs(_money_cents(source["capital_delta_rub"], field="storno capital")),
                metadata={"target_line_no": int(source["line_no"])},
            )
        )
    if not source_lines:
        if str(target["document_kind"]) not in {"transfer_loss", "late_expense"}:
            raise FfPoolDocumentError(
                "storno_target_has_no_direct_effect",
                "This parent document has no direct movement to reverse; storno its effective children instead",
                details={"target_document_id": target_id, "document_kind": str(target["document_kind"])},
            )
        evidence_lines = conn.execute(
            f"SELECT * FROM {DOCUMENT_LINES_TABLE} WHERE document_id=? ORDER BY line_no",
            (target_id,),
        ).fetchall()
        if not evidence_lines:
            raise FfPoolDocumentError(
                "storno_target_has_no_effect",
                "Target document has no movement or immutable outcome to reverse",
            )
        for source in evidence_lines:
            lines.append(
                _document_line(
                    role="storno",
                    facility_id=str(source["facility_id"]) if source["facility_id"] else None,
                    pool=str(source["pool"]) if source["pool"] else None,
                    nm_id=int(source["nm_id"]),
                    quantity=int(source["quantity"]),
                    capital_cents=_money_cents(source["capital_rub"], field="storno capital"),
                    expense_cents=_money_cents(source["expense_rub"], field="storno expense"),
                    metadata={"target_line_no": int(source["line_no"]), "evidence_only": True},
                )
            )
    document_id = _request_document_id(request)
    root_id = str(target["root_document_id"])
    target_overhead = (
        _json_object(
            _json_object(_loads(target["posted_manifest_json"], {})).get("domain")
            or {}
        )
        if str(target["document_kind"]) == "pool_overhead"
        else {}
    )
    document = _document_blueprint(
        document_id=document_id,
        document_kind="storno",
        document_role="storno",
        root_document_id=root_id,
        relation={"parent_document_id": target_id, "relation_type": "storno_of"},
        lines=lines,
        movements=movements,
    )
    return {
        "primary_document_id": document_id,
        "root_document_id": root_id,
        "documents": [document],
        "domain_manifest": {
            "target_document_id": target_id,
            "exact_original_movement_reversal": True,
            "guided_acceptance_recovery": guided_recovery,
            "overhead_evidence_link": target_overhead,
        },
    }


def _assert_overhead_storno_has_no_dependent_handoff(
    conn: sqlite3.Connection,
    *,
    target: Mapping[str, Any],
    source_lines: Sequence[Mapping[str, Any]],
) -> None:
    """Fail closed when a handoff already froze the posted overhead WAC."""

    event_table = "sheet_vitrina_v1_ff_pool_fbs_lifecycle_events"
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (event_table,),
    ).fetchone() is None:
        return
    operation = conn.execute(
        f"SELECT rowid FROM {OPERATIONS_TABLE} WHERE operation_id=?",
        (str(target["operation_id"]),),
    ).fetchone()
    if operation is None:
        raise FfPoolDocumentError(
            "overhead_storno_operation_missing",
            "Overhead operation identity disappeared before storno",
        )
    operation_rowid = int(operation[0])
    dependent: list[dict[str, Any]] = []
    for source in source_lines:
        if str(source["pool"]) != "FBS":
            continue
        rows = conn.execute(
            f"""SELECT event_id,order_id,facility_id,nm_id,occurred_at
                  FROM {event_table}
                 WHERE event_type='handoff_debit'
                   AND facility_id=? AND pool='FBS' AND nm_id=?
                   AND CAST(COALESCE(json_extract(
                         details_json,'$.cost_basis.source_operation_rowid'
                       ),0) AS INTEGER)>=?
                 ORDER BY event_sequence LIMIT 20""",
            (
                str(source["facility_id"]),
                int(source["nm_id"]),
                operation_rowid,
            ),
        ).fetchall()
        dependent.extend(
            {
                "event_id": str(row[0]),
                "order_id": int(row[1]),
                "facility_id": str(row[2]),
                "nm_id": int(row[3]),
                "occurred_at": str(row[4]),
            }
            for row in rows
        )
    if dependent:
        raise FfPoolDocumentError(
            "overhead_storno_dependent_handoff",
            "Overhead cannot be reversed after a dependent FBS handoff froze its WAC",
            details={
                "target_document_id": str(target["document_id"]),
                "source_operation_rowid": operation_rowid,
                "dependent_handoffs": dependent,
                "recovery": "append-only reviewed correction required",
            },
        )


def _guided_acceptance_recovery_context(
    conn: sqlite3.Connection,
    *,
    target: Mapping[str, Any],
    source_lines: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Prove an immediate guided acceptance can be compensated exactly."""

    if str(target["document_kind"]) != "china_acceptance":
        return {}
    target_request = conn.execute(
        f"SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?",
        (str(target["request_id"]),),
    ).fetchone()
    if target_request is None or not _is_guided_china_request(target_request):
        return {}
    replay = conn.execute(
        f"SELECT * FROM {GUIDED_REPLAYS_TABLE} WHERE request_id=?",
        (str(target_request["request_id"]),),
    ).fetchone()
    if replay is None:
        raise FfPoolDocumentError(
            "guided_recovery_replay_missing",
            "Guided acceptance cannot be recovered without immutable replay evidence",
        )
    prior_recovery = conn.execute(
        f"SELECT recovery_request_id FROM {GUIDED_RECOVERIES_TABLE} WHERE target_request_id=?",
        (str(target_request["request_id"]),),
    ).fetchone()
    if prior_recovery is not None:
        raise FfPoolDocumentError(
            "guided_recovery_exists",
            "Guided acceptance already has an immutable recovery",
            details={"request_id": str(prior_recovery["recovery_request_id"])},
        )
    posted_manifest = _json_object(_loads(target["posted_manifest_json"], {}))
    domain = _json_object(posted_manifest.get("domain") or {})
    before = _json_object(domain.get("guided_acceptance_before_state") or {})
    supplier_before = _json_object(before.get("supplier") or {})
    aggregate_before = list(before.get("aggregate_balances") or [])
    required_aggregate_fields = {
        "nm_id",
        "row_present",
        "quantity",
        "wac_rub",
        "capital_rub",
        "cost_covered_quantity",
        "quality",
        "certified",
        "wb_quantity",
        "wb_in_way_to_client",
        "wb_in_way_from_client",
        "provenance_json",
    }
    if (
        not supplier_before
        or not before.get("pool_balances")
        or not aggregate_before
        or any(
            not required_aggregate_fields.issubset(dict(item))
            for item in aggregate_before
        )
    ):
        raise FfPoolDocumentError(
            "guided_recovery_before_state_missing",
            "Guided acceptance lacks the exact immutable before-state required for recovery",
        )
    shipment_id = str(target_request["source_id"])
    shipment = conn.execute(
        """SELECT shipment_id,actual_ff_acceptance_date,order_status,updated_at
           FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?""",
        (shipment_id,),
    ).fetchone()
    if (
        shipment is None
        or str(shipment["actual_ff_acceptance_date"] or "")
        != str(target_request["business_date"])
        or str(shipment["order_status"] or "") != "accepted_ff"
    ):
        raise FfPoolDocumentError(
            "guided_recovery_supplier_drift",
            "Supplier factual acceptance changed after the target document",
        )
    legacy_operation = conn.execute(
        "SELECT operation_id FROM sheet_vitrina_v1_ff_stock_operations WHERE operation_id=?",
        (str(replay["legacy_operation_id"]),),
    ).fetchone()
    if legacy_operation is None:
        raise FfPoolDocumentError(
            "guided_recovery_legacy_receipt_missing",
            "The append-only aggregate receipt evidence is missing",
        )
    current_layer = conn.execute(
        """SELECT layer_id,inputs_hash FROM sheet_vitrina_v1_supplier_ff_cost_layers
           WHERE supplier_shipment_id=? AND is_current=1
           ORDER BY version DESC LIMIT 1""",
        (shipment_id,),
    ).fetchone()
    if (
        current_layer is None
        or str(current_layer["layer_id"]) != str(replay["cost_layer_id"])
        or str(current_layer["inputs_hash"]) != str(replay["cost_inputs_hash"])
    ):
        raise FfPoolDocumentError(
            "guided_recovery_cost_layer_drift",
            "Supplier cost layer changed after guided acceptance",
        )
    deltas: dict[tuple[str, str, int], tuple[int, Decimal]] = {}
    aggregate_deltas: dict[int, tuple[int, Decimal]] = {}
    with localcontext() as context:
        context.prec = 160
        for line in source_lines:
            key = (
                str(line["facility_id"]),
                str(line["pool"]),
                int(line["nm_id"]),
            )
            quantity, capital = deltas.get(key, (0, ZERO))
            capital_delta = Decimal(str(line["capital_delta_rub"]))
            deltas[key] = (
                quantity + int(line["quantity_delta"]),
                capital + capital_delta,
            )
            aq, ac = aggregate_deltas.get(int(line["nm_id"]), (0, ZERO))
            aggregate_deltas[int(line["nm_id"])] = (
                aq + int(line["quantity_delta"]),
                ac + capital_delta,
            )
    for item in before.get("pool_balances") or []:
        key = (str(item["facility_id"]), str(item["pool"]), int(item["nm_id"]))
        row = conn.execute(
            f"SELECT quantity,capital_rub FROM {BALANCES_TABLE} "
            "WHERE facility_id=? AND pool=? AND nm_id=?",
            key,
        ).fetchone()
        delta = deltas.get(key, (0, ZERO))
        expected_quantity = int(item["quantity"]) + delta[0]
        with localcontext() as context:
            context.prec = 160
            expected_capital = Decimal(str(item["capital_rub"])) + delta[1]
        if (
            row is None
            or int(row["quantity"]) != expected_quantity
            or Decimal(str(row["capital_rub"])) != expected_capital
        ):
            raise FfPoolDocumentError(
                "guided_recovery_pool_drift",
                "Affected pool balance changed after guided acceptance",
                details={"facility_id": key[0], "pool": key[1], "nm_id": key[2]},
            )
    aggregate_version = str(before["aggregate_version_id"])
    active_version = conn.execute(
        "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
    ).fetchone()
    if active_version is None or str(active_version[0]) != aggregate_version:
        raise FfPoolDocumentError(
            "guided_recovery_projection_drift",
            "Active warehouse projection changed after guided acceptance",
        )
    for item in aggregate_before:
        nm_id = int(item["nm_id"])
        row = conn.execute(
            """SELECT quantity,wac_rub,capital_rub,cost_covered_quantity,
                      quality,certified,wb_quantity,wb_in_way_to_client,
                      wb_in_way_from_client,provenance_json
               FROM sheet_vitrina_v1_warehouse_functional_balances
               WHERE version_id=? AND warehouse_key='ff' AND nm_id=?""",
            (aggregate_version, nm_id),
        ).fetchone()
        delta = aggregate_deltas.get(nm_id, (0, ZERO))
        with localcontext() as context:
            context.prec = 160
            expected_quantity = Decimal(str(item["quantity"])) + delta[0]
            expected_capital = Decimal(str(item["capital_rub"])) + delta[1]
            expected_covered = (
                Decimal(str(item["cost_covered_quantity"])) + delta[0]
            )
            expected_wac = (
                None
                if expected_quantity == ZERO
                else canonical_decimal_text(expected_capital / expected_quantity)
            )
        accounting_matches = bool(
            row is not None
            and Decimal(str(row["quantity"])) == expected_quantity
            and Decimal(str(row["capital_rub"])) == expected_capital
            and Decimal(str(row["cost_covered_quantity"])) == expected_covered
            and (
                (row["wac_rub"] is None and expected_wac is None)
                or (
                    row["wac_rub"] is not None
                    and expected_wac is not None
                    and Decimal(str(row["wac_rub"])) == Decimal(expected_wac)
                )
            )
        )
        if not accounting_matches:
            raise FfPoolDocumentError(
                "guided_recovery_aggregate_drift",
                "Aggregate FF state changed after guided acceptance",
                details={"nm_id": nm_id},
            )
        if bool(item.get("row_present")):
            metadata_matches = bool(
                str(row["quality"]) == str(item["quality"])
                and int(row["certified"]) == int(item["certified"])
                and Decimal(str(row["wb_quantity"]))
                == Decimal(str(item["wb_quantity"]))
                and Decimal(str(row["wb_in_way_to_client"]))
                == Decimal(str(item["wb_in_way_to_client"]))
                and Decimal(str(row["wb_in_way_from_client"]))
                == Decimal(str(item["wb_in_way_from_client"]))
                and str(row["provenance_json"]) == str(item["provenance_json"])
            )
        else:
            metadata_matches = bool(
                str(row["quality"]) == "guided_acceptance_minor_unit"
                and int(row["certified"]) == 0
                and Decimal(str(row["wb_quantity"])) == ZERO
                and Decimal(str(row["wb_in_way_to_client"])) == ZERO
                and Decimal(str(row["wb_in_way_from_client"])) == ZERO
                and str(row["provenance_json"])
                == _json(_guided_new_aggregate_provenance(target_request))
            )
        if not metadata_matches:
            raise FfPoolDocumentError(
                "guided_recovery_aggregate_drift",
                "Aggregate FF metadata changed after guided acceptance",
                details={"nm_id": nm_id},
            )
    dependent_before = _json_object(
        before.get("dependent_reservation_debit_state") or {}
    )
    pool_before_rows = list(before.get("pool_balances") or [])
    dependent_current = _guided_dependent_state(
        conn,
        facility_id=str(pool_before_rows[0]["facility_id"]),
        nm_ids=aggregate_deltas,
    )
    if (
        not dependent_before
        or str(dependent_current["digest"]) != str(dependent_before.get("digest") or "")
        or _json(dependent_current) != _json(dependent_before)
    ):
        raise FfPoolDocumentError(
            "guided_recovery_dependent_state_drift",
            "Affected reservation or FBS debit state changed after guided acceptance",
            details={"before": dependent_before, "current": dependent_current},
        )
    return {
        "target_request_id": str(target_request["request_id"]),
        "target_document_id": str(target["document_id"]),
        "shipment_id": shipment_id,
        "source_revision": str(target_request["source_revision"]),
        "accepted_business_date": str(target_request["business_date"]),
        "legacy_operation_id": str(legacy_operation["operation_id"]),
        "cost_layer_id": str(current_layer["layer_id"]),
        "restore_supplier": {
            "actual_ff_acceptance_date": str(
                supplier_before.get("actual_ff_acceptance_date") or ""
            ),
            "order_status": str(supplier_before.get("order_status") or ""),
        },
        "affected_nm_ids": sorted(aggregate_deltas),
        "restore_aggregate_balances": [
            dict(item) for item in aggregate_before
        ],
        "before_state_digest": _fingerprint(before),
        "fail_closed_on_any_affected_state_drift": True,
    }


def _plan_correction(
    conn: sqlite3.Connection,
    *,
    request: Mapping[str, Any],
    manifest: Mapping[str, Any],
    epoch: int,
) -> dict[str, Any]:
    target_id = str(manifest.get("target_document_id") or "").strip()
    target = _load_document(conn, target_id)
    if target is None:
        raise FfPoolDocumentError("target_document_not_found", "Correction target document was not found")
    root_id = str(target["root_document_id"])
    root = _load_document(conn, root_id)
    if root is None:
        raise FfPoolDocumentError(
            "root_document_not_found",
            "Correction target root document was not found",
            details={"target_document_id": target_id, "root_document_id": root_id},
        )
    if (
        str(root["document_kind"]).startswith("transfer_")
        or str(target["document_kind"]) == "late_expense"
    ):
        raise FfPoolDocumentError(
            "transfer_correction_requires_typed_outcome",
            "Transfer corrections must use a typed outcome or storno plus replacement",
            details={
                "target_document_id": target_id,
                "document_kind": str(target["document_kind"]),
                "root_document_id": root_id,
                "root_document_kind": str(root["document_kind"]),
            },
        )
    raw_movements = list(manifest.get("movements") or [])
    if not raw_movements:
        raise FfPoolDocumentError("correction_movements_required", "Correction requires explicit signed movements")
    movements: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    for item in raw_movements:
        facility_id, pool = _location_fields(conn, item, require_active=True)
        nm_id = _positive_int(item.get("nm_id"), field="correction nm_id")
        quantity_delta = _signed_int(item.get("quantity_delta"), field="correction quantity")
        capital_cents = _signed_money_cents(item.get("capital_delta_rub"), field="correction capital")
        if quantity_delta == 0 and capital_cents == 0:
            raise FfPoolDocumentError("empty_correction_line", "Correction line has no effect")
        movements.append(
            _movement(
                facility_id=facility_id,
                pool=pool,
                nm_id=nm_id,
                quantity_delta=quantity_delta,
                capital_delta_cents=capital_cents,
                wac_snapshot=str(item.get("wac_snapshot_rub") or "") or None,
                metadata={"target_document_id": target_id, "append_only_correction": True},
            )
        )
        lines.append(
            _document_line(
                role="correction",
                facility_id=facility_id,
                pool=pool,
                nm_id=nm_id,
                quantity=abs(quantity_delta),
                capital_cents=abs(capital_cents),
                metadata={"signed_quantity_delta": quantity_delta, "signed_capital_rub": _cents_text(capital_cents)},
            )
        )
    document_id = _request_document_id(request)
    document = _document_blueprint(
        document_id=document_id,
        document_kind="correction",
        document_role="correction",
        root_document_id=root_id,
        relation={"parent_document_id": target_id, "relation_type": "correction_of"},
        lines=lines,
        movements=movements,
    )
    return {
        "primary_document_id": document_id,
        "root_document_id": root_id,
        "documents": [document],
        "domain_manifest": {"target_document_id": target_id, "append_only": True},
    }


def _plan_late_expense(
    conn: sqlite3.Connection,
    *,
    request: Mapping[str, Any],
    manifest: Mapping[str, Any],
    epoch: int,
) -> dict[str, Any]:
    root_id = str(manifest.get("root_document_id") or manifest.get("target_document_id") or "").strip()
    _root, _source, destination = _transfer_root_context(conn, root_id)
    state = _transfer_state(conn, root_id)
    if not state:
        raise FfPoolDocumentError("shipment_not_found", "Late expense requires a posted shipment")
    expenses = _expense_lines(manifest.get("expenses") or [])
    if not expenses:
        raise FfPoolDocumentError("expense_lines_required", "Late expense document is empty")
    expense_total = sum(int(item["amount_cents"]) for item in expenses)
    weights = [(nm_id, int(item["shipped_quantity"])) for nm_id, item in sorted(state.items())]
    allocations = _allocate_cents(expense_total, weights)
    lines: list[dict[str, Any]] = []
    movements: list[dict[str, Any]] = []
    for nm_id, shipment in sorted(state.items()):
        allocation = int(allocations[nm_id])
        shipped_quantity = int(shipment["shipped_quantity"])
        prior_outcomes = list(shipment["terminal_lines"])
        prior_quantity = 0
        received_share = 0
        lost_share = 0
        for outcome in prior_outcomes:
            quantity = int(outcome["quantity"])
            share = _component_share(allocation, shipped_quantity, prior_quantity, quantity)
            prior_quantity += quantity
            if str(outcome["line_role"]) == "received":
                received_share += share
                movements.append(
                    _movement(
                        facility_id=str(outcome["facility_id"] or destination[0]),
                        pool=str(outcome["pool"] or destination[1]),
                        nm_id=nm_id,
                        quantity_delta=0,
                        capital_delta_cents=share,
                        wac_snapshot=None,
                        metadata={"late_expense_for": root_id, "received_document_id": str(outcome["document_id"])},
                    )
                )
            else:
                lost_share += share
        lines.append(
            _document_line(
                role="late_expense_component",
                facility_id=destination[0],
                pool=destination[1],
                nm_id=nm_id,
                quantity=shipped_quantity,
                capital_cents=0,
                expense_cents=allocation,
                metadata={
                    "terminal_quantity_at_post": prior_quantity,
                    "received_share_rub": _cents_text(received_share),
                    "non_received_terminal_share_rub": _cents_text(lost_share),
                    "open_share_rub": _cents_text(allocation - received_share - lost_share),
                },
            )
        )
    document_id = _request_document_id(request)
    document = _document_blueprint(
        document_id=document_id,
        document_kind="late_expense",
        document_role="late_expense",
        root_document_id=root_id,
        relation={"parent_document_id": root_id, "relation_type": "late_expense_for"},
        lines=lines,
        movements=movements,
        expenses=expenses,
    )
    return {
        "primary_document_id": document_id,
        "root_document_id": root_id,
        "documents": [document],
        "domain_manifest": {
            "root_document_id": root_id,
            "amount_rub": _cents_text(expense_total),
            "append_only": True,
            "parent_updated": False,
        },
    }


def _apply_plan(
    conn: sqlite3.Connection,
    *,
    request: Mapping[str, Any],
    plan: Mapping[str, Any],
    epoch: int,
    posted_at: str,
) -> None:
    _require_utc(posted_at)
    manifest_sha = _fingerprint(plan["posted_manifest"])
    for document in plan["documents"]:
        document_id = str(document["document_id"])
        operation_type = _operation_type(str(document["document_kind"]))
        operation_source_type = f"ff_pool_document:{document['document_role']}"
        operation_source_id = document_id
        conn.execute(
            f"""INSERT INTO {OPERATIONS_TABLE}(
                operation_id,operation_type,source_system,source_type,source_id,
                source_revision,idempotency_epoch,business_date,posted_at,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                document_id,
                operation_type,
                str(request["source_system"]),
                operation_source_type,
                operation_source_id,
                str(request["source_revision"]),
                int(request["idempotency_epoch"]),
                str(request["business_date"]),
                posted_at,
                _json(
                    {
                        "contract_name": CONTRACT_NAME,
                        "request_id": str(request["request_id"]),
                        "document_id": document_id,
                        "root_document_id": str(document["root_document_id"]),
                        "posted_manifest_sha256": manifest_sha,
                    }
                ),
            ),
        )
        document_manifest = {
            **dict(plan["posted_manifest"]),
            "document_id": document_id,
            "document_kind": str(document["document_kind"]),
            "document_role": str(document["document_role"]),
            "root_document_id": str(document["root_document_id"]),
            "lines": [
                {
                    **{key: value for key, value in item.items() if key not in {"capital_cents", "expense_cents"}},
                    "capital_rub": _cents_text(int(item["capital_cents"])),
                    "expense_rub": _cents_text(int(item["expense_cents"])),
                }
                for item in document.get("lines", [])
            ],
        }
        document_manifest_sha = _fingerprint(document_manifest)
        conn.execute(
            f"""INSERT INTO {DOCUMENTS_TABLE}(
                document_id,request_id,document_role,document_kind,root_document_id,
                operation_id,source_system,source_type,source_id,source_revision,
                idempotency_epoch,actor,business_date,source_filename,
                source_content_type,source_sha256,template_fingerprint,
                posted_manifest_sha256,posted_manifest_json,posted_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                document_id,
                str(request["request_id"]),
                str(document["document_role"]),
                str(document["document_kind"]),
                str(document["root_document_id"]),
                document_id,
                str(request["source_system"]),
                str(request["source_type"]),
                str(request["source_id"]),
                str(request["source_revision"]),
                int(request["idempotency_epoch"]),
                str(request["actor"]),
                str(request["business_date"]),
                str(request["source_filename"] or ""),
                str(request["source_content_type"] or ""),
                str(request["source_sha256"] or ""),
                str(request["template_fingerprint"] or ""),
                document_manifest_sha,
                _json(document_manifest),
                posted_at,
            ),
        )
        for line_no, line in enumerate(document.get("lines", []), start=1):
            conn.execute(
                f"""INSERT INTO {DOCUMENT_LINES_TABLE}(
                    document_id,line_no,root_document_id,line_role,facility_id,pool,
                    nm_id,quantity,capital_rub,expense_rub,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    document_id,
                    line_no,
                    str(document["root_document_id"]),
                    str(line["line_role"]),
                    str(line["facility_id"]) if line.get("facility_id") else None,
                    str(line["pool"]) if line.get("pool") else None,
                    int(line["nm_id"]),
                    int(line["quantity"]),
                    _cents_text(int(line["capital_cents"])),
                    _cents_text(int(line["expense_cents"])),
                    _json(dict(line.get("metadata") or {})),
                ),
            )
        for expense_no, expense in enumerate(document.get("expenses", []), start=1):
            conn.execute(
                f"""INSERT INTO {EXPENSE_LINES_TABLE}(
                    document_id,expense_line_no,amount_rub,basis,source_file_sha256,
                    source_filename,metadata_json
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    document_id,
                    expense_no,
                    _cents_text(int(expense["amount_cents"])),
                    str(expense["basis"]),
                    str(expense.get("source_file_sha256") or ""),
                    str(expense.get("source_filename") or ""),
                    _json(dict(expense.get("metadata") or {})),
                ),
            )
        for line_no, movement in enumerate(document.get("movements", []), start=1):
            _apply_balance_movement(
                conn,
                movement=movement,
                operation_id=document_id,
                line_no=line_no,
                epoch=epoch,
                posted_at=posted_at,
                business_date=str(request["business_date"]),
                allow_missing_fbs=str(request["document_kind"])
                == "facility_pool_opening",
            )
    _materialize_explicit_zero_balances(
        conn,
        plan=plan,
        epoch=epoch,
        posted_at=posted_at,
    )
    for document in plan["documents"]:
        relation = document.get("relation")
        if not relation:
            continue
        parent_id = str(relation["parent_document_id"])
        child_id = str(document["document_id"])
        relation_type = str(relation["relation_type"])
        conn.execute(
            f"INSERT INTO {DOCUMENT_RELATIONS_TABLE}(parent_document_id,child_document_id,"
            "root_document_id,relation_type,created_at) VALUES(?,?,?,?,?)",
            (parent_id, child_id, str(document["root_document_id"]), relation_type, posted_at),
        )
        if relation_type in STAGE1_RELATION_TYPES:
            parent = _load_document(conn, parent_id)
            if parent is None:
                raise FfPoolDocumentError("relation_parent_missing", "Relation parent disappeared")
            conn.execute(
                f"INSERT INTO {RELATIONS_TABLE}(parent_id,child_id,relation_type,created_at) "
                "VALUES(?,?,?,?)",
                (str(parent["operation_id"]), child_id, relation_type, posted_at),
            )
    if plan.get("unselected_keys"):
        current = _balance_digest(conn, plan["unselected_keys"])
        if current != str(plan.get("unselected_digest") or ""):
            raise FfPoolDocumentError(
                "inventory_unselected_pool_changed",
                "Unselected inventory pool changed during posting",
            )
    if str(request["document_kind"]) == "pool_overhead":
        _enqueue_pool_overhead_publication(
            conn,
            request=request,
            plan=plan,
            posted_at=posted_at,
        )
    elif bool(
        _json_object(plan.get("domain_manifest") or {}).get(
            "overhead_evidence_link"
        )
    ):
        _enqueue_pool_overhead_reversal_publication(
            conn,
            request=request,
            plan=plan,
            posted_at=posted_at,
        )


def _enqueue_pool_overhead_publication(
    conn: sqlite3.Connection,
    *,
    request: Mapping[str, Any],
    plan: Mapping[str, Any],
    posted_at: str,
) -> dict[str, Any]:
    """Atomically bind one overhead document to one canonical replay identity."""

    document_id = str(plan["primary_document_id"])
    domain = _json_object(plan.get("domain_manifest") or {})
    movements = [
        dict(item)
        for document in plan.get("documents") or []
        for item in document.get("movements") or []
    ]
    nm_ids = sorted({int(item["nm_id"]) for item in movements})
    event_revision = _fingerprint(
        {
            "contract": "pool_overhead_targeted_publication_v1",
            "document_id": document_id,
            "request_source_revision": str(request["source_revision"]),
            "facility_id": str(domain.get("facility_id") or ""),
            "pools": sorted({str(item["pool"]) for item in movements}),
            "nm_ids": nm_ids,
            "basis_digest": str(domain.get("basis_digest") or ""),
            "amount_rub": str(domain.get("amount_rub") or ""),
            "posted_manifest_sha256": _fingerprint(plan["posted_manifest"]),
        }
    )
    stable_source_id = f"pool_overhead:{document_id}"
    queue_id = "whrq_" + hashlib.sha256(
        _json(
            {
                "stable_source_id": stable_source_id,
                "source_revision": event_revision,
            }
        ).encode("utf-8")
    ).hexdigest()[:24]
    conn.execute(
        f"""INSERT OR IGNORE INTO {TARGETED_RECALC_QUEUE_TABLE}(
               queue_id,stable_source_id,source_revision,effective_date,
               affected_nm_ids_json,status,requested_at,started_at,finished_at,error,
               economics_status,economics_started_at,economics_finished_at,economics_error
           ) VALUES(?,?,?,?,?,'queued',?,NULL,NULL,NULL,'',NULL,NULL,NULL)""",
        (
            queue_id,
            stable_source_id,
            event_revision,
            str(request["business_date"]),
            _json(nm_ids),
            posted_at,
        ),
    )
    stored = conn.execute(
        f"SELECT * FROM {TARGETED_RECALC_QUEUE_TABLE} "
        "WHERE stable_source_id=? AND source_revision=?",
        (stable_source_id, event_revision),
    ).fetchone()
    if (
        stored is None
        or str(stored["queue_id"]) != queue_id
        or _loads(stored["affected_nm_ids_json"], []) != nm_ids
    ):
        raise FfPoolDocumentError(
            "overhead_publication_queue_identity_conflict",
            "Posted overhead could not bind its exact publication queue",
        )
    return dict(stored)


def _enqueue_pool_overhead_reversal_publication(
    conn: sqlite3.Connection,
    *,
    request: Mapping[str, Any],
    plan: Mapping[str, Any],
    posted_at: str,
) -> dict[str, Any]:
    """Bind an overhead storno to its own exact replay identity once."""

    document_id = str(plan["primary_document_id"])
    domain = _json_object(plan.get("domain_manifest") or {})
    target = _json_object(domain.get("overhead_evidence_link") or {})
    movements = [
        dict(item)
        for document in plan.get("documents") or []
        for item in document.get("movements") or []
    ]
    nm_ids = sorted({int(item["nm_id"]) for item in movements})
    event_revision = _fingerprint(
        {
            "contract": "pool_overhead_storno_targeted_publication_v1",
            "document_id": document_id,
            "target_document_id": str(domain.get("target_document_id") or ""),
            "target_evidence": target,
            "request_source_revision": str(request["source_revision"]),
            "movement_digest": _fingerprint(movements),
            "posted_manifest_sha256": _fingerprint(plan["posted_manifest"]),
        }
    )
    stable_source_id = f"pool_overhead:{document_id}"
    queue_id = "whrq_" + hashlib.sha256(
        _json(
            {
                "stable_source_id": stable_source_id,
                "source_revision": event_revision,
            }
        ).encode("utf-8")
    ).hexdigest()[:24]
    conn.execute(
        f"""INSERT OR IGNORE INTO {TARGETED_RECALC_QUEUE_TABLE}(
               queue_id,stable_source_id,source_revision,effective_date,
               affected_nm_ids_json,status,requested_at,started_at,finished_at,error,
               economics_status,economics_started_at,economics_finished_at,economics_error
           ) VALUES(?,?,?,?,?,'queued',?,NULL,NULL,NULL,'',NULL,NULL,NULL)""",
        (
            queue_id,
            stable_source_id,
            event_revision,
            str(request["business_date"]),
            _json(nm_ids),
            posted_at,
        ),
    )
    stored = conn.execute(
        f"SELECT * FROM {TARGETED_RECALC_QUEUE_TABLE} "
        "WHERE stable_source_id=? AND source_revision=?",
        (stable_source_id, event_revision),
    ).fetchone()
    if (
        stored is None
        or str(stored["queue_id"]) != queue_id
        or _loads(stored["affected_nm_ids_json"], []) != nm_ids
    ):
        raise FfPoolDocumentError(
            "overhead_storno_publication_queue_identity_conflict",
            "Overhead storno could not bind its exact publication queue",
        )
    return dict(stored)


def _apply_overhead_current_aggregate_projection(
    conn: sqlite3.Connection,
    *,
    plan: Mapping[str, Any],
    request: Mapping[str, Any],
    posted_at: str,
) -> None:
    """Keep current aggregate FF capital atomic with overhead or its storno.

    Pool document fixtures can intentionally run without the aggregate
    projection.  In an activated warehouse runtime, however, a handoff may
    follow immediately after the overhead commit and must never observe a
    pool/aggregate split.  Only the exact affected capital and WAC columns are
    changed here; heavy Finance publication remains in the durable queue.
    """

    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    required = {
        "sheet_vitrina_v1_warehouse_functional_active",
        "sheet_vitrina_v1_warehouse_functional_balances",
    }
    if not required.issubset(tables):
        return
    active = conn.execute(
        "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
    ).fetchone()
    if active is None:
        raise FfPoolDocumentError(
            "overhead_aggregate_active_missing",
            "Activated pool overhead requires the current aggregate FF projection",
        )
    version_id = str(active[0])
    capital_deltas: dict[int, Decimal] = {}
    for document in plan.get("documents") or []:
        for movement in document.get("movements") or []:
            if int(movement["quantity_delta"]) != 0:
                raise FfPoolDocumentError(
                    "overhead_aggregate_quantity_delta",
                    "Overhead aggregate synchronization cannot change quantity",
                )
            nm_id = int(movement["nm_id"])
            capital_deltas[nm_id] = capital_deltas.get(nm_id, ZERO) + (
                Decimal(int(movement["capital_delta_cents"])) / Decimal(100)
            )
    for nm_id, capital_delta in sorted(capital_deltas.items()):
        row = conn.execute(
            """SELECT quantity,capital_rub,provenance_json
               FROM sheet_vitrina_v1_warehouse_functional_balances
               WHERE version_id=? AND warehouse_key='ff' AND nm_id=?""",
            (version_id, nm_id),
        ).fetchone()
        if row is None:
            raise FfPoolDocumentError(
                "overhead_aggregate_sku_missing",
                "Exact overhead SKU is absent from the current aggregate FF projection",
                details={"nm_id": nm_id, "version_id": version_id},
            )
        with localcontext() as context:
            context.prec = 160
            quantity = Decimal(str(row["quantity"]))
            capital = Decimal(str(row["capital_rub"])) + capital_delta
            if quantity <= ZERO or capital <= ZERO:
                raise FfPoolDocumentError(
                    "overhead_aggregate_invalid",
                    "Overhead or storno would make current aggregate cost unavailable",
                    details={"nm_id": nm_id},
                )
            wac = canonical_decimal_text(capital / quantity)
        provenance = {
            "source": "pool_overhead_current_projection_v1",
            "request_id": str(request["request_id"]),
            "document_id": str(plan["primary_document_id"]),
            "source_revision": str(request["source_revision"]),
            "posted_at": posted_at,
            "previous_provenance_sha256": _fingerprint(
                _loads(row["provenance_json"], {})
            ),
        }
        conn.execute(
            """UPDATE sheet_vitrina_v1_warehouse_functional_balances
               SET capital_rub=?,wac_rub=?,provenance_json=?
               WHERE version_id=? AND warehouse_key='ff' AND nm_id=?""",
            (
                canonical_decimal_text(capital),
                wac,
                _json(provenance),
                version_id,
                nm_id,
            ),
        )
    aggregate_rows = [
        {
            "nm_id": int(row[0]),
            "quantity": _signed_int(row[1], field="aggregate quantity"),
            "capital_rub": canonical_decimal_text(row[2]),
        }
        for row in conn.execute(
            """SELECT nm_id,quantity,capital_rub
               FROM sheet_vitrina_v1_warehouse_functional_balances
               WHERE version_id=? AND warehouse_key='ff' ORDER BY nm_id""",
            (version_id,),
        ).fetchall()
    ]
    parity = evaluate_ff_pool_aggregate_parity(conn, aggregate_rows)
    if parity.status != "pass":
        raise FfPoolDocumentError(
            "overhead_aggregate_parity_failed",
            "Overhead current projection diverged from facility/pool detail",
            details={"mismatched_nm_ids": list(parity.mismatched_nm_ids)},
        )
    record_ff_pool_parity_diagnostic(
        conn,
        diagnostic_id="ffpar_overhead_"
        + _fingerprint(
            {"request_id": str(request["request_id"]), "posted_at": posted_at}
        ).removeprefix("sha256:")[:20],
        aggregate_revision=version_id,
        checked_at=posted_at,
        result=parity,
        details={
            "source": "pool_overhead_current_projection_v1",
            "request_id": str(request["request_id"]),
            "document_id": str(plan["primary_document_id"]),
            "finance_publication_deferred": True,
        },
    )


def _pool_overhead_publication_state(
    conn: sqlite3.Connection,
    *,
    document_id: str,
) -> dict[str, Any]:
    stable_source_id = f"pool_overhead:{document_id}"
    row = conn.execute(
        f"SELECT * FROM {TARGETED_RECALC_QUEUE_TABLE} "
        "WHERE stable_source_id=? ORDER BY requested_at DESC,queue_id DESC LIMIT 1",
        (stable_source_id,),
    ).fetchone()
    if row is None:
        return {
            "status": "missing",
            "stage": "document_posted",
            "label_ru": "Документ проведён; очередь пересчёта отсутствует",
            "stable_source_id": stable_source_id,
            "queue_id": "",
            "retry_required": True,
        }
    queue_status = str(row["status"] or "")
    economics_status = str(row["economics_status"] or "")
    finance_status = str(row["finance_status"] or "")
    if (
        queue_status == "complete"
        and economics_status == "complete"
        and finance_status == "complete"
    ):
        status = "complete"
        stage = "cost_published"
        label = "Себестоимость опубликована"
    elif (
        queue_status in {"error", "failed"}
        or economics_status == "error"
        or finance_status == "error"
    ):
        status = "error"
        stage = "recalculation_error"
        label = "Ошибка пересчёта; документ повторять не требуется"
    elif queue_status in {"running", "complete"}:
        status = "running"
        stage = "recalculation_running"
        label = "Пересчёт выполняется"
    else:
        status = "queued"
        stage = "document_posted"
        label = "Документ проведён; пересчёт поставлен в очередь"
    return {
        "status": status,
        "stage": stage,
        "label_ru": label,
        "stable_source_id": stable_source_id,
        "queue_id": str(row["queue_id"] or ""),
        "source_revision": str(row["source_revision"] or ""),
        "affected_nm_ids": _loads(row["affected_nm_ids_json"], []),
        "queue_status": queue_status,
        "economics_status": economics_status or "pending",
        "finance_status": finance_status or "pending",
        "finance_source_fingerprint": str(
            row["finance_source_fingerprint"] or ""
        ),
        "requested_at": str(row["requested_at"] or ""),
        "started_at": str(row["started_at"] or ""),
        "finished_at": str(
            row["finance_finished_at"]
            or row["economics_finished_at"]
            or row["finished_at"]
            or ""
        ),
        "error": str(
            row["finance_error"]
            or row["economics_error"]
            or row["error"]
            or ""
        ),
        "retry_required": status == "error",
        "repeat_business_document": False,
    }


def _materialize_explicit_zero_balances(
    conn: sqlite3.Connection,
    *,
    plan: Mapping[str, Any],
    epoch: int,
    posted_at: str,
) -> None:
    """Materialize an audited FBS physical zero without inventing a movement.

    The immutable ``pool_inventory`` absolute-target lines are the accounting
    evidence.  A zero-to-zero physical delta is deliberately not a movement,
    but a missing row must still become an exact facility/pool/SKU read-model
    row when a routine inventory document confirms an absolute target of zero.
    """

    rows = list(plan.get("explicit_zero_balances") or [])
    if not rows:
        return
    root_document_id = str(plan["primary_document_id"])
    for item in rows:
        facility_id = str(item["facility_id"])
        pool = _pool(str(item["pool"]))
        nm_id = int(item["nm_id"])
        existing = _balance_row(
            conn,
            (facility_id, pool, nm_id),
            epoch=epoch,
            required=False,
        )
        if existing is not None:
            raise FfPoolDocumentError(
                "explicit_zero_balance_drift",
                "Inventory zero target row appeared after the posting plan",
                details={"facility_id": facility_id, "pool": pool, "nm_id": nm_id},
            )
        conn.execute(
            f"""INSERT INTO {BALANCES_TABLE}(
                facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,wac_rub,
                source_watermark,updated_at
            ) VALUES(?,?,?,?,0,'0',NULL,?,?)""",
            (facility_id, pool, nm_id, epoch, root_document_id, posted_at),
        )


def _apply_balance_movement(
    conn: sqlite3.Connection,
    *,
    movement: Mapping[str, Any],
    operation_id: str,
    line_no: int,
    epoch: int,
    posted_at: str,
    business_date: str = "",
    allow_missing_fbs: bool = False,
) -> None:
    facility_id = str(movement["facility_id"])
    pool = _pool(str(movement["pool"]))
    nm_id = int(movement["nm_id"])
    quantity_delta = int(movement["quantity_delta"])
    capital_delta = int(movement["capital_delta_cents"])
    key = (facility_id, pool, nm_id)
    row = _balance_row(conn, key, epoch=epoch, required=False)
    if row is None and pool == "FBS" and not allow_missing_fbs:
        raise FfPoolDocumentError(
            "applicable_fbs_balance_missing",
            "FBS receipts, writeoffs and transfers cannot implicitly create a physical row",
            details={"facility_id": facility_id, "nm_id": nm_id},
        )
    if row is not None and pool == "FBS":
        try:
            require_fbs_pair_writeable(
                conn,
                facility_id=facility_id,
                nm_id=nm_id,
                effective_date=str(business_date or posted_at)[:10],
                projection_epoch=epoch,
            )
        except FbsApplicabilityError as exc:
            raise FfPoolDocumentError(
                exc.code,
                str(exc),
                details=exc.details,
            ) from exc
    before_quantity = int(row["quantity"]) if row is not None else 0
    try:
        before_capital = Decimal(str(row["capital_rub"])) if row is not None else ZERO
    except (InvalidOperation, ValueError) as exc:
        raise FfPoolDocumentError(
            "invalid_money", "balance capital must be Decimal-safe"
        ) from exc
    if not before_capital.is_finite() or before_capital < ZERO:
        raise FfPoolDocumentError(
            "invalid_money", "balance capital is outside the allowed non-negative range"
        )
    after_quantity = before_quantity + quantity_delta
    # Balance capital is an exact Decimal with up to 80 stored characters.
    # Keep the arithmetic outside the process-wide Decimal precision so a
    # signed kopeck movement cannot trim an authoritative opening tail.
    with localcontext() as context:
        context.prec = 160
        capital_delta_rub = Decimal(capital_delta) / Decimal(100)
        after_capital = before_capital + capital_delta_rub
    if after_quantity < 0 or after_capital < ZERO:
        raise FfPoolDocumentError(
            "negative_pool_balance",
            "Pool movement would create negative quantity or capital",
            details={
                "facility_id": facility_id,
                "pool": pool,
                "nm_id": nm_id,
                "after_quantity": after_quantity,
                "after_capital_rub": canonical_decimal_text(after_capital),
            },
        )
    if (after_quantity == 0) != (after_capital == ZERO):
        raise FfPoolDocumentError(
            "pool_quantity_capital_zero_mismatch",
            "Zero pool quantity and capital must close together",
            details={"facility_id": facility_id, "pool": pool, "nm_id": nm_id},
        )
    if quantity_delta > 0 and capital_delta <= 0:
        raise FfPoolDocumentError("positive_quantity_without_cost", "Positive pool receipt requires positive capital")
    if quantity_delta < 0 and capital_delta >= 0:
        raise FfPoolDocumentError("negative_quantity_without_cost", "Pool debit requires negative capital")
    if quantity_delta == 0 and capital_delta == 0:
        raise FfPoolDocumentError("empty_movement", "Pool movement line has no effect")
    conn.execute(
        f"""INSERT INTO {LINES_TABLE}(
            operation_id,line_no,facility_id,pool,nm_id,quantity_delta,
            capital_delta_rub,wac_snapshot_rub,metadata_json
        ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            operation_id,
            line_no,
            facility_id,
            pool,
            nm_id,
            quantity_delta,
            _cents_text(capital_delta),
            str(movement.get("wac_snapshot") or "") or None,
            _json(dict(movement.get("metadata") or {})),
        ),
    )
    wac = _decimal_ratio_text(after_capital, after_quantity) if after_quantity else None
    if row is None:
        conn.execute(
            f"""INSERT INTO {BALANCES_TABLE}(
                facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,wac_rub,
                source_watermark,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                facility_id,
                pool,
                nm_id,
                epoch,
                after_quantity,
                canonical_decimal_text(after_capital),
                wac,
                operation_id,
                posted_at,
            ),
        )
    else:
        conn.execute(
            f"UPDATE {BALANCES_TABLE} SET quantity=?,capital_rub=?,wac_rub=?,"
            "source_watermark=?,updated_at=? WHERE facility_id=? AND pool=? AND nm_id=? "
            "AND projection_epoch=?",
            (
                after_quantity,
                canonical_decimal_text(after_capital),
                wac,
                operation_id,
                posted_at,
                facility_id,
                pool,
                nm_id,
                epoch,
            ),
        )


def _transfer_state(conn: sqlite3.Connection, root_document_id: str) -> dict[int, dict[str, Any]]:
    shipment_rows = conn.execute(
        f"""SELECT line.*,document.posted_at,document.document_id
            FROM {DOCUMENT_LINES_TABLE} AS line
            JOIN {DOCUMENTS_TABLE} AS document ON document.document_id=line.document_id
            WHERE line.root_document_id=? AND line.line_role='shipped'
              AND NOT EXISTS(
                SELECT 1 FROM {DOCUMENT_RELATIONS_TABLE} AS relation
                WHERE relation.parent_document_id=document.document_id
                  AND relation.relation_type='storno_of'
              )
            ORDER BY line.nm_id,document.posted_at,document.document_id,line.line_no""",
        (root_document_id,),
    ).fetchall()
    result: dict[int, dict[str, Any]] = {}
    for row in shipment_rows:
        nm_id = int(row["nm_id"])
        if nm_id in result:
            raise FfPoolDocumentError("multiple_transfer_shipments", "Transfer root has multiple shipment lines for one SKU")
        result[nm_id] = {
            "shipped_quantity": int(row["quantity"]),
            "capital_cents": _money_cents(row["capital_rub"], field="shipped capital"),
            "expense_components": [
                {
                    "document_id": str(row["document_id"]),
                    "amount_cents": _money_cents(row["expense_rub"], field="shipment expense"),
                }
            ],
            "terminal_quantity": 0,
            "terminal_lines": [],
        }
    if not result:
        return result
    late_rows = conn.execute(
        f"""SELECT line.*,document.posted_at
            FROM {DOCUMENT_LINES_TABLE} AS line
            JOIN {DOCUMENTS_TABLE} AS document ON document.document_id=line.document_id
            WHERE line.root_document_id=? AND line.line_role='late_expense_component'
              AND NOT EXISTS(
                SELECT 1 FROM {DOCUMENT_RELATIONS_TABLE} AS relation
                WHERE relation.parent_document_id=document.document_id
                  AND relation.relation_type='storno_of'
              )
            ORDER BY document.posted_at,document.document_id,line.line_no""",
        (root_document_id,),
    ).fetchall()
    for row in late_rows:
        nm_id = int(row["nm_id"])
        if nm_id in result:
            result[nm_id]["expense_components"].append(
                {
                    "document_id": str(row["document_id"]),
                    "amount_cents": _money_cents(row["expense_rub"], field="late expense"),
                }
            )
    terminal_rows = conn.execute(
        f"""SELECT line.*,document.posted_at
            FROM {DOCUMENT_LINES_TABLE} AS line
            JOIN {DOCUMENTS_TABLE} AS document ON document.document_id=line.document_id
            WHERE line.root_document_id=?
              AND line.line_role IN ('received','lost','cancelled','expected_not_sent')
              AND NOT EXISTS(
                SELECT 1 FROM {DOCUMENT_RELATIONS_TABLE} AS relation
                WHERE relation.parent_document_id=document.document_id
                  AND relation.relation_type='storno_of'
              )
            ORDER BY document.posted_at,document.document_id,line.line_no""",
        (root_document_id,),
    ).fetchall()
    for row in terminal_rows:
        nm_id = int(row["nm_id"])
        if nm_id not in result:
            raise FfPoolDocumentError("terminal_without_shipment", "Transfer child exists without shipment line")
        result[nm_id]["terminal_quantity"] += int(row["quantity"])
        result[nm_id]["terminal_lines"].append(dict(row))
    for nm_id, item in result.items():
        if int(item["terminal_quantity"]) > int(item["shipped_quantity"]):
            raise FfPoolDocumentError(
                "transfer_conservation_broken",
                "Immutable transfer children exceed shipped quantity",
                details={"nm_id": nm_id},
            )
    return result


def _transfer_root_context(
    conn: sqlite3.Connection,
    root_document_id: str,
) -> tuple[sqlite3.Row, tuple[str, str], tuple[str, str]]:
    root = _load_document(conn, root_document_id)
    if root is None or str(root["document_kind"]) != "transfer_root":
        raise FfPoolDocumentError("transfer_root_not_found", "Transfer root was not found")
    manifest = _loads(root["posted_manifest_json"], {})
    domain = dict(manifest.get("domain") or {}) if isinstance(manifest, Mapping) else {}
    source = _location_from_public(domain.get("source"))
    destination = _location_from_public(domain.get("destination"))
    return root, source, destination


def _before_images(
    conn: sqlite3.Connection,
    *,
    request: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = [
        {
            "table": REQUESTS_TABLE,
            "key": {"request_id": str(request["request_id"])},
            "before": dict(request),
            "after": None,
        }
    ]
    if _is_guided_china_request(request):
        shipment_id = str(request["source_id"])
        shipment = conn.execute(
            "SELECT * FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?",
            (shipment_id,),
        ).fetchone()
        source_key = f"supplier_shipment_acceptance:{shipment_id}"
        operation = conn.execute(
            "SELECT * FROM sheet_vitrina_v1_ff_stock_operations WHERE source_key=?",
            (source_key,),
        ).fetchone()
        images.extend(
            [
                {
                    "table": "sheet_vitrina_v1_supplier_shipments",
                    "key": {"shipment_id": shipment_id},
                    "before": dict(shipment) if shipment is not None else None,
                    "after": None,
                },
                {
                    "table": "sheet_vitrina_v1_ff_stock_operations",
                    "key": {"source_key": source_key},
                    "before": dict(operation) if operation is not None else None,
                    "after": None,
                },
            ]
        )
        active = conn.execute(
            "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
        ).fetchone()
        if active is not None:
            for nm_id in sorted(
                {
                    int(movement["nm_id"])
                    for document in plan.get("documents") or []
                    for movement in document.get("movements") or []
                }
            ):
                row = conn.execute(
                    """SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances
                       WHERE version_id=? AND warehouse_key='ff' AND nm_id=?""",
                    (str(active[0]), nm_id),
                ).fetchone()
                images.append(
                    {
                        "table": "sheet_vitrina_v1_warehouse_functional_balances",
                        "key": {"version_id": str(active[0]), "warehouse_key": "ff", "nm_id": nm_id},
                        "before": dict(row) if row is not None else None,
                        "after": None,
                    }
                )
        current_layer = conn.execute(
            """SELECT * FROM sheet_vitrina_v1_supplier_ff_cost_layers
               WHERE supplier_shipment_id=? AND is_current=1 ORDER BY version DESC LIMIT 1""",
            (shipment_id,),
        ).fetchone()
        images.append(
            {
                "table": "sheet_vitrina_v1_supplier_ff_cost_layers",
                "key": {"supplier_shipment_id": shipment_id, "is_current": 1},
                "before": dict(current_layer) if current_layer is not None else None,
                "after": None,
            }
        )
    guided_recovery = _json_object(
        (plan.get("domain_manifest") or {}).get("guided_acceptance_recovery") or {}
    )
    if guided_recovery:
        shipment_id = str(guided_recovery["shipment_id"])
        shipment = conn.execute(
            "SELECT * FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?",
            (shipment_id,),
        ).fetchone()
        layer = conn.execute(
            "SELECT * FROM sheet_vitrina_v1_supplier_ff_cost_layers WHERE layer_id=?",
            (str(guided_recovery["cost_layer_id"]),),
        ).fetchone()
        recovery_source_key = (
            f"supplier_shipment_acceptance_recovery:{shipment_id}:"
            f"{guided_recovery['target_request_id']}"
        )
        images.extend(
            [
                {
                    "table": "sheet_vitrina_v1_supplier_shipments",
                    "key": {"shipment_id": shipment_id},
                    "before": dict(shipment) if shipment is not None else None,
                    "after": None,
                },
                {
                    "table": "sheet_vitrina_v1_supplier_ff_cost_layers",
                    "key": {"layer_id": str(guided_recovery["cost_layer_id"])},
                    "before": dict(layer) if layer is not None else None,
                    "after": None,
                },
                {
                    "table": "sheet_vitrina_v1_ff_stock_operations",
                    "key": {"source_key": recovery_source_key},
                    "before": None,
                    "after": None,
                },
                {
                    "table": GUIDED_RECOVERIES_TABLE,
                    "key": {"recovery_request_id": str(request["request_id"])},
                    "before": None,
                    "after": None,
                },
            ]
        )
    for facility_id, pool, nm_id in plan["balance_keys"]:
        row = conn.execute(
            f"SELECT * FROM {BALANCES_TABLE} WHERE facility_id=? AND pool=? AND nm_id=?",
            (facility_id, pool, nm_id),
        ).fetchone()
        images.append(
            {
                "table": BALANCES_TABLE,
                "key": {"facility_id": facility_id, "pool": pool, "nm_id": nm_id},
                "before": dict(row) if row is not None else None,
                "after": None,
            }
        )
    for document in plan["documents"]:
        document_id = str(document["document_id"])
        images.extend(
            [
                {
                    "table": OPERATIONS_TABLE,
                    "key": {"operation_id": document_id},
                    "before": None,
                    "after": None,
                },
                {
                    "table": DOCUMENTS_TABLE,
                    "key": {"document_id": document_id},
                    "before": None,
                    "after": None,
                },
            ]
        )
        for line_no, _line in enumerate(document.get("lines", []), start=1):
            images.append(
                {
                    "table": DOCUMENT_LINES_TABLE,
                    "key": {"document_id": document_id, "line_no": line_no},
                    "before": None,
                    "after": None,
                }
            )
        for expense_line_no, _expense in enumerate(document.get("expenses", []), start=1):
            images.append(
                {
                    "table": EXPENSE_LINES_TABLE,
                    "key": {
                        "document_id": document_id,
                        "expense_line_no": expense_line_no,
                    },
                    "before": None,
                    "after": None,
                }
            )
        for line_no, _movement_line in enumerate(document.get("movements", []), start=1):
            images.append(
                {
                    "table": LINES_TABLE,
                    "key": {"operation_id": document_id, "line_no": line_no},
                    "before": None,
                    "after": None,
                }
            )
        relation = document.get("relation")
        if relation:
            parent_id = str(relation["parent_document_id"])
            relation_type = str(relation["relation_type"])
            images.append(
                {
                    "table": DOCUMENT_RELATIONS_TABLE,
                    "key": {
                        "parent_document_id": parent_id,
                        "child_document_id": document_id,
                        "relation_type": relation_type,
                    },
                    "before": None,
                    "after": None,
                }
            )
            if relation_type in STAGE1_RELATION_TYPES:
                parent = _load_document(conn, parent_id)
                if parent is None:
                    raise FfPoolDocumentError(
                        "relation_parent_missing",
                        "Relation parent is missing while preparing recovery evidence",
                    )
                images.append(
                    {
                        "table": RELATIONS_TABLE,
                        "key": {
                            "parent_id": str(parent["operation_id"]),
                            "child_id": document_id,
                            "relation_type": relation_type,
                        },
                        "before": None,
                        "after": None,
                    }
                )
    return images


def _balance_digest(conn: sqlite3.Connection, keys: Iterable[Sequence[Any]]) -> str:
    normalized = sorted({(str(item[0]), str(item[1]), int(item[2])) for item in keys})
    rows = []
    for facility_id, pool, nm_id in normalized:
        row = conn.execute(
            f"SELECT facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,wac_rub,"
            f"source_watermark,updated_at FROM {BALANCES_TABLE} "
            "WHERE facility_id=? AND pool=? AND nm_id=?",
            (facility_id, pool, nm_id),
        ).fetchone()
        rows.append(list(row) if row is not None else [facility_id, pool, nm_id, None])
    return _fingerprint(rows)


def _balance_row(
    conn: sqlite3.Connection,
    key: tuple[str, str, int],
    *,
    epoch: int,
    required: bool,
) -> sqlite3.Row | None:
    row = conn.execute(
        f"SELECT * FROM {BALANCES_TABLE} WHERE facility_id=? AND pool=? AND nm_id=?",
        key,
    ).fetchone()
    if row is not None and int(row["projection_epoch"]) != epoch:
        raise FfPoolDocumentError(
            "balance_epoch_mismatch",
            "Pool balance belongs to a different feature epoch",
            details={"facility_id": key[0], "pool": key[1], "nm_id": key[2]},
        )
    if required and row is None:
        raise FfPoolDocumentError(
            "source_balance_missing",
            "Required pool balance does not exist",
            details={"facility_id": key[0], "pool": key[1], "nm_id": key[2]},
        )
    return row


def _load_document(conn: sqlite3.Connection, document_id: str) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT * FROM {DOCUMENTS_TABLE} WHERE document_id=?",
        (str(document_id),),
    ).fetchone()


def _writer_epoch(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        f"SELECT epoch,writer_enabled FROM {FEATURE_EPOCHS_TABLE} ORDER BY epoch DESC LIMIT 1"
    ).fetchone()
    if row is None or not bool(row["writer_enabled"]):
        raise FfPoolDocumentError(
            "feature_writer_disabled",
            "Facility × pool posting is default-off until a reviewed writer epoch exists",
        )
    return int(row["epoch"])


def _document_blueprint(
    *,
    document_id: str,
    document_kind: str,
    document_role: str,
    root_document_id: str,
    lines: Sequence[Mapping[str, Any]],
    movements: Sequence[Mapping[str, Any]],
    relation: Mapping[str, Any] | None = None,
    expenses: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    return {
        "document_id": str(document_id),
        "document_kind": _document_kind(document_kind),
        "document_role": str(document_role),
        "root_document_id": str(root_document_id),
        "relation": dict(relation) if relation else None,
        "lines": [dict(item) for item in lines],
        "movements": [dict(item) for item in movements],
        "expenses": [dict(item) for item in expenses],
    }


def _document_line(
    *,
    role: str,
    facility_id: str | None,
    pool: str | None,
    nm_id: int,
    quantity: int,
    capital_cents: int,
    expense_cents: int = 0,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "line_role": str(role),
        "facility_id": str(facility_id) if facility_id else None,
        "pool": _pool(pool) if pool else None,
        "nm_id": int(nm_id),
        "quantity": int(quantity),
        "capital_cents": int(capital_cents),
        "expense_cents": int(expense_cents),
        "metadata": dict(metadata or {}),
    }


def _movement(
    *,
    facility_id: str,
    pool: str,
    nm_id: int,
    quantity_delta: int,
    capital_delta_cents: int,
    wac_snapshot: str | None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "facility_id": str(facility_id),
        "pool": _pool(pool),
        "nm_id": int(nm_id),
        "quantity_delta": int(quantity_delta),
        "capital_delta_cents": int(capital_delta_cents),
        "wac_snapshot": str(wac_snapshot) if wac_snapshot else None,
        "metadata": dict(metadata or {}),
    }


def _expense_lines(value: Iterable[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, ExpenseLine):
            raw = asdict(item)
        elif isinstance(item, Mapping):
            raw = dict(item)
        else:
            raise FfPoolDocumentError("invalid_expense_line", "Expense line must be a mapping")
        result.append(
            {
                "amount_cents": _money_cents(raw.get("amount_rub"), field="expense amount", positive=True),
                "basis": _safe_basis(raw.get("basis")),
                "source_file_sha256": _optional_sha256(raw.get("source_file_sha256")),
                "source_filename": str(raw.get("source_filename") or "").strip()[:240],
                "metadata": _json_object(raw.get("metadata") or {}),
            }
        )
    return result


def _inventory_cost_basis(
    conn: sqlite3.Connection,
    *,
    facility_id: str,
    nm_id: int,
    epoch: int,
    explicit: Any,
) -> Decimal:
    if explicit not in (None, "", {}):
        if isinstance(explicit, Mapping):
            amount = explicit.get("unit_cost_rub", explicit.get("amount_rub"))
            source_digest = str(explicit.get("source_digest") or "")
            if not source_digest.startswith("sha256:"):
                raise FfPoolDocumentError(
                    "inventory_cost_evidence_missing",
                    "Explicit inventory basis requires immutable source digest",
                    details={"nm_id": nm_id},
                )
        else:
            amount = explicit
            source_digest = ""
        basis = _positive_decimal(amount, field="inventory unit cost")
        if source_digest or isinstance(explicit, Mapping):
            return basis
    rows = conn.execute(
        f"SELECT pool,quantity,capital_rub FROM {BALANCES_TABLE} "
        "WHERE projection_epoch=? AND facility_id=? AND nm_id=? AND quantity>0 "
        "ORDER BY pool",
        (epoch, facility_id, nm_id),
    ).fetchall()
    candidates = []
    for row in rows:
        quantity = int(row["quantity"])
        capital = _money_cents(row["capital_rub"], field="inventory cost basis capital")
        if quantity > 0 and capital > 0:
            candidates.append(Decimal(capital) / Decimal(100) / Decimal(quantity))
    if not candidates:
        raise FfPoolDocumentError(
            "inventory_positive_cost_basis_missing",
            "Inventory surplus requires a positive same-SKU cost basis",
            details={"facility_id": facility_id, "nm_id": nm_id},
        )
    return candidates[0]


def _facility_quantity(conn: sqlite3.Connection, *, facility_id: str, epoch: int) -> int:
    row = conn.execute(
        f"SELECT COALESCE(SUM(quantity),0) FROM {BALANCES_TABLE} "
        "WHERE projection_epoch=? AND facility_id=?",
        (epoch, facility_id),
    ).fetchone()
    return int(row[0] or 0)


def _location(
    conn: sqlite3.Connection,
    value: Any,
    *,
    require_active: bool,
) -> tuple[str, str]:
    if isinstance(value, PoolLocation):
        facility_id = value.facility_id
        pool = value.pool
    elif isinstance(value, Mapping):
        facility_id = str(value.get("facility_id") or "")
        pool = str(value.get("pool") or "")
    else:
        raise FfPoolDocumentError("invalid_location", "Pool location must include facility_id and pool")
    return _facility(conn, facility_id, require_active=require_active), _pool(pool)


def _location_fields(
    conn: sqlite3.Connection,
    value: Mapping[str, Any],
    *,
    require_active: bool,
) -> tuple[str, str]:
    return (
        _facility(conn, str(value.get("facility_id") or ""), require_active=require_active),
        _pool(str(value.get("pool") or "")),
    )


def _location_from_public(value: Any) -> tuple[str, str]:
    if not isinstance(value, Mapping):
        raise FfPoolDocumentError("transfer_location_missing", "Transfer root location evidence is missing")
    return str(value.get("facility_id") or ""), _pool(str(value.get("pool") or ""))


def _facility(conn: sqlite3.Connection, facility_id: str, *, require_active: bool) -> str:
    token = str(facility_id or "").strip()
    row = conn.execute(
        f"SELECT facility_id,active FROM {FACILITIES_TABLE} WHERE facility_id=?",
        (token,),
    ).fetchone()
    if row is None or require_active and not bool(row["active"]):
        raise FfPoolDocumentError(
            "unknown_or_inactive_facility",
            "Facility does not exist or is inactive",
            details={"facility_id": token},
        )
    return str(row["facility_id"])


def _pool(value: str | None) -> str:
    token = str(value or "").strip().upper()
    if token not in POOLS:
        raise FfPoolDocumentError("invalid_pool", "Pool must be exact FBS or FBO")
    return token


def _scope(value: str) -> str:
    token = str(value or "").strip()
    if token not in {"FBS", "FBO", "both"}:
        raise FfPoolDocumentError("invalid_pool_scope", "Scope must be FBS, FBO or both")
    return token


def _validate_manifest(document_kind: str, manifest: Mapping[str, Any]) -> None:
    if not manifest:
        raise FfPoolDocumentError("empty_preview_manifest", "Document preview manifest is empty")
    required: dict[str, tuple[str, ...]] = {
        "facility_pool_opening": ("allocations",),
        "china_acceptance": ("facility_id", "allocations"),
        "transfer_root": ("source", "destination"),
        "transfer_shipment": ("root_document_id", "items"),
        "transfer_receipt": ("root_document_id", "items"),
        "transfer_loss": ("root_document_id", "items"),
        "transfer_discrepancy": ("root_document_id",),
        "transfer_cancellation": ("root_document_id",),
        "pool_reallocation": ("facility_id", "source_pool", "destination_pool", "items"),
        "pool_inventory": ("facility_id", "scope", "targets"),
        "pool_overhead": (
            "facility_id",
            "scope",
            "amount_rub",
            "category",
            "source_mode",
        ),
        "storno": ("target_document_id",),
        "correction": ("target_document_id", "movements"),
        "late_expense": ("expenses",),
    }
    missing = [key for key in required.get(document_kind, ()) if manifest.get(key) in (None, "", [])]
    if missing:
        raise FfPoolDocumentError(
            "preview_manifest_incomplete",
            "Document preview manifest is incomplete",
            details={"missing": missing},
        )


def _validate_identity(identity: DocumentIdentity) -> None:
    _client_request_id(identity.request_id)
    for name in ("source_system", "source_type", "source_id", "source_revision", "actor"):
        value = str(getattr(identity, name) or "").strip()
        if not value:
            raise FfPoolDocumentError("identity_field_missing", f"{name} is required")
    if isinstance(identity.idempotency_epoch, bool) or int(identity.idempotency_epoch) <= 0:
        raise FfPoolDocumentError("invalid_idempotency_epoch", "idempotency_epoch must be positive")
    try:
        parsed = datetime.fromisoformat(str(identity.business_date))
    except ValueError as exc:
        raise FfPoolDocumentError("invalid_business_date", "business_date must be YYYY-MM-DD") from exc
    if parsed.date().isoformat() != str(identity.business_date):
        raise FfPoolDocumentError("invalid_business_date", "business_date must be YYYY-MM-DD")


def _client_request_id(value: str) -> str:
    token = str(value or "").strip()
    if not REQUEST_ID_RE.fullmatch(token):
        raise FfPoolDocumentError("invalid_request_id", "request_id has an invalid format")
    return token


def _request_document_id(request: Mapping[str, Any]) -> str:
    return "ffpd_" + str(request["request_identity"]).removeprefix("sha256:")[:28]


def _document_kind(value: str) -> str:
    token = str(value or "").strip()
    if token not in DOCUMENT_KINDS:
        raise FfPoolDocumentError("invalid_document_kind", "Unsupported FF pool document kind")
    return token


def _operation_type(document_kind: str) -> str:
    return {
        "correction": "correction",
        "storno": "storno",
        "late_expense": "late_expense",
    }.get(document_kind, document_kind)


def _allocate_cents(total_cents: int, weighted_keys: Iterable[tuple[Any, int]]) -> dict[Any, int]:
    rows = [(key, int(weight)) for key, weight in weighted_keys if int(weight) > 0]
    if total_cents < 0:
        raise FfPoolDocumentError("negative_allocation", "Allocation total cannot be negative")
    if total_cents == 0:
        return {key: 0 for key, _weight in rows}
    denominator = sum(weight for _key, weight in rows)
    if denominator <= 0:
        raise FfPoolDocumentError("allocation_denominator_zero", "Positive allocation denominator is required")
    base = {key: total_cents * weight // denominator for key, weight in rows}
    remainder = total_cents - sum(base.values())
    order = sorted(
        rows,
        key=lambda item: (
            -((total_cents * item[1]) % denominator),
            _stable_key(item[0]),
        ),
    )
    for index in range(remainder):
        base[order[index % len(order)][0]] += 1
    if sum(base.values()) != total_cents:
        raise FfPoolDocumentError("allocation_does_not_conserve", "Kopeck allocation failed conservation")
    return base


def _normalize_acceptance_capital(allocations: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Normalize one acceptance header to RUB minor units without row loss.

    Supplier costing is allowed to produce exact Decimal fractions of a
    kopeck.  The business-document ledger is minor-unit based, so the exact
    header is rounded once and its kopecks are distributed over SKUs by the
    largest fractional remainder with nm_id as the stable tie-breaker.
    """

    exact_by_nm: dict[int, Decimal] = {}
    accepted_by_nm: dict[int, int] = {}
    with localcontext() as context:
        context.prec = 160
        for item in allocations:
            nm_id = _positive_int(item.get("nm_id"), field="acceptance nm_id")
            if nm_id in exact_by_nm:
                raise FfPoolDocumentError(
                    "duplicate_acceptance_nm_id",
                    "Acceptance contains the same nm_id more than once",
                    details={"nm_id": nm_id},
                )
            accepted = _nonnegative_int(
                item.get("accepted_quantity"), field="accepted quantity"
            )
            try:
                amount = Decimal(str(item.get("accepted_capital_rub")))
            except (InvalidOperation, ValueError) as exc:
                raise FfPoolDocumentError(
                    "invalid_money", "accepted capital must be Decimal-safe"
                ) from exc
            if not amount.is_finite() or amount < ZERO:
                raise FfPoolDocumentError(
                    "invalid_money", "accepted capital is outside the allowed range"
                )
            if accepted == 0 and amount != ZERO:
                raise FfPoolDocumentError(
                    "zero_quantity_with_capital",
                    "Zero accepted quantity must have zero accepted capital",
                    details={"nm_id": nm_id},
                )
            if accepted > 0 and amount <= ZERO:
                raise FfPoolDocumentError(
                    "invalid_money",
                    "Positive accepted quantity requires positive exact capital",
                    details={"nm_id": nm_id},
                )
            exact_by_nm[nm_id] = amount
            accepted_by_nm[nm_id] = accepted

        exact_total = sum(exact_by_nm.values(), ZERO)
        canonical_total_cents = canonical_rub_minor_units(
            exact_total, field="acceptance exact total capital_rub"
        )
        canonical_total = Decimal(canonical_total_cents) / Decimal(100)
        base_cents = {
            nm_id: int(amount * 100)
            for nm_id, amount in exact_by_nm.items()
        }
        remainder = canonical_total_cents - sum(base_cents.values())
        eligible = [nm_id for nm_id, quantity in accepted_by_nm.items() if quantity > 0]
        if remainder < 0 or remainder > len(eligible):
            raise FfPoolDocumentError(
                "capital_normalization_failed",
                "Exact supplier capital could not be normalized conservatively",
                details={"remainder_cents": remainder, "eligible_sku_count": len(eligible)},
            )
        order = sorted(
            eligible,
            key=lambda nm_id: (
                -((exact_by_nm[nm_id] * 100) - Decimal(base_cents[nm_id])),
                nm_id,
            ),
        )
        residual_owners = order[:remainder]
        for nm_id in residual_owners:
            base_cents[nm_id] += 1
        if sum(base_cents.values()) != canonical_total_cents:
            raise FfPoolDocumentError(
                "capital_normalization_failed",
                "Normalized SKU capital does not conserve the canonical header",
            )
        for nm_id, quantity in accepted_by_nm.items():
            if quantity > 0 and base_cents[nm_id] <= 0:
                raise FfPoolDocumentError(
                    "source_capital_rounds_to_zero",
                    "Positive accepted SKU capital would become synthetic zero",
                    details={"nm_id": nm_id},
                )
        residual_by_nm = {
            nm_id: (Decimal(base_cents[nm_id]) / Decimal(100)) - amount
            for nm_id, amount in exact_by_nm.items()
        }
        return {
            "policy": "header_round_half_up_then_largest_fractional_remainder_nm_id",
            "exact_total_rub": canonical_decimal_text(exact_total),
            "canonical_total_cents": canonical_total_cents,
            "canonical_total_rub": _cents_text(canonical_total_cents),
            "total_residual_rub": canonical_decimal_text(canonical_total - exact_total),
            "exact_capital_rub_by_nm": {
                nm_id: canonical_decimal_text(value)
                for nm_id, value in sorted(exact_by_nm.items())
            },
            "capital_cents_by_nm": dict(sorted(base_cents.items())),
            "normalization_residual_rub_by_nm": {
                nm_id: canonical_decimal_text(value)
                for nm_id, value in sorted(residual_by_nm.items())
            },
            "residual_owner_nm_ids": residual_owners,
        }


def _component_share(total_cents: int, total_quantity: int, prior_quantity: int, quantity: int) -> int:
    if quantity < 0 or prior_quantity < 0 or prior_quantity + quantity > total_quantity or total_quantity <= 0:
        raise FfPoolDocumentError("invalid_component_share", "Transfer component share is outside shipped quantity")
    return (total_cents * (prior_quantity + quantity) // total_quantity) - (
        total_cents * prior_quantity // total_quantity
    )


def _proportional_cents(total_cents: int, quantity: int, denominator_quantity: int) -> int:
    if quantity <= 0 or denominator_quantity <= 0 or quantity > denominator_quantity or total_cents <= 0:
        raise FfPoolDocumentError("invalid_proportional_cost", "Positive proportional cost basis is required")
    if quantity == denominator_quantity:
        return total_cents
    result = total_cents * quantity // denominator_quantity
    if result <= 0:
        raise FfPoolDocumentError("proportional_cost_rounds_to_zero", "Movement cost would become synthetic zero")
    return result


def _money_cents(value: Any, *, field: str, positive: bool = False) -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise FfPoolDocumentError("invalid_money", f"{field} must be Decimal-safe") from exc
    if not amount.is_finite() or amount < ZERO or positive and amount <= ZERO:
        raise FfPoolDocumentError("invalid_money", f"{field} is outside the allowed positive range")
    quantized = amount.quantize(RUB_QUANTUM)
    if quantized != amount:
        raise FfPoolDocumentError("money_minor_unit_required", f"{field} must use kopeck precision")
    return int(quantized * 100)


def _signed_money_cents(value: Any, *, field: str) -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise FfPoolDocumentError("invalid_money", f"{field} must be Decimal-safe") from exc
    if not amount.is_finite() or amount.quantize(RUB_QUANTUM) != amount:
        raise FfPoolDocumentError("money_minor_unit_required", f"{field} must use kopeck precision")
    return int(amount * 100)


def _decimal_to_cents(value: Decimal, *, field: str, positive: bool) -> int:
    cents = canonical_rub_minor_units(value, field=field)
    if positive and cents <= 0:
        raise FfPoolDocumentError("positive_cost_required", f"{field} must be positive")
    return cents


def _positive_decimal(value: Any, *, field: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise FfPoolDocumentError("invalid_decimal", f"{field} must be Decimal-safe") from exc
    if not amount.is_finite() or amount <= ZERO:
        raise FfPoolDocumentError("invalid_decimal", f"{field} must be positive")
    return amount


def _ratio_text(capital_cents: int, quantity: int) -> str:
    if capital_cents <= 0 or quantity <= 0:
        raise FfPoolDocumentError("positive_wac_required", "Positive quantity/capital are required for WAC")
    with localcontext() as context:
        context.prec = 38
        return canonical_decimal_text(Decimal(capital_cents) / Decimal(100) / Decimal(quantity))


def _decimal_ratio_text(capital_rub: Decimal, quantity: int) -> str:
    if capital_rub <= ZERO or quantity <= 0:
        raise FfPoolDocumentError(
            "positive_wac_required", "Positive quantity/capital are required for WAC"
        )
    with localcontext() as context:
        context.prec = 38
        return canonical_decimal_text(capital_rub / Decimal(quantity))


def _cents_text(value: int) -> str:
    sign = "-" if int(value) < 0 else ""
    absolute = abs(int(value))
    return f"{sign}{absolute // 100}.{absolute % 100:02d}"


def _nonnegative_int(value: Any, *, field: str) -> int:
    result = _signed_int(value, field=field)
    if result < 0:
        raise FfPoolDocumentError("invalid_integer", f"{field} must be non-negative")
    return result


def _positive_int(value: Any, *, field: str) -> int:
    result = _signed_int(value, field=field)
    if result <= 0:
        raise FfPoolDocumentError("invalid_integer", f"{field} must be positive")
    return result


def _signed_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise FfPoolDocumentError("invalid_integer", f"{field} must be an exact integer")
    if isinstance(value, int):
        result = value
        if abs(result) > MAX_SQLITE_INTEGER:
            raise FfPoolDocumentError("invalid_integer", f"{field} exceeds the SQLite integer range")
        return result
    token = str(value if value is not None else "").strip()
    if len(token.removeprefix("-")) > 19 or not re.fullmatch(r"-?(?:0|[1-9][0-9]*)", token):
        raise FfPoolDocumentError("invalid_integer", f"{field} must be an exact integer")
    result = int(token)
    if abs(result) > MAX_SQLITE_INTEGER:
        raise FfPoolDocumentError("invalid_integer", f"{field} exceeds the SQLite integer range")
    return result


def _safe_basis(value: Any) -> str:
    token = " ".join(str(value or "").split())
    if not token or len(token) > 1000 or any(ord(char) < 32 and char not in "\t" for char in token):
        raise FfPoolDocumentError("invalid_expense_basis", "Expense basis must be safe non-empty text")
    return token


def _overhead_comment(value: Any) -> str:
    token = " ".join(str(value or "").split())
    if len(token) > 1000 or any(ord(char) < 32 and char not in "\t" for char in token):
        raise FfPoolDocumentError(
            "invalid_overhead_comment",
            "Overhead comment must contain at most 1000 safe characters",
        )
    return token


def _payment_evidence_from_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    value = manifest.get("payment_evidence")
    return dict(value) if isinstance(value, Mapping) else {}


def _payment_fingerprint_from_manifest(manifest: Mapping[str, Any]) -> str:
    evidence = _payment_evidence_from_manifest(manifest)
    token = str(evidence.get("payment_fingerprint") or "").strip()
    return (
        token
        if evidence.get("parse_status") == RUSSIAN_PAYMENT_ORDER_PARSE_STATUS_PARSED
        and evidence.get("posting_eligible") is True
        and re.fullmatch(r"sha256:[0-9a-f]{64}", token)
        else ""
    )


def _optional_sha256(value: Any) -> str:
    token = str(value or "").strip()
    if token and not re.fullmatch(r"sha256:[0-9a-f]{64}", token):
        raise FfPoolDocumentError("invalid_source_digest", "Expense source digest must be sha256:<hex>")
    return token


def _stable_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sql_values(values: Iterable[str]) -> str:
    return ",".join("'" + str(value).replace("'", "''") + "'" for value in values)


def _decimal_check(column: str) -> str:
    return f"""typeof({column})='text'
        AND length({column}) BETWEEN 1 AND 80
        AND {column} NOT GLOB '*[^0-9.-]*'
        AND instr(substr({column},2),'-')=0
        AND length({column})-length(replace({column},'.','')) <= 1
        AND {column} NOT IN ('','-','.','-.')
        AND substr({column},-1,1) <> '.'
        AND (
            substr({column},1,1) BETWEEN '0' AND '9'
            OR (substr({column},1,1)='-' AND substr({column},2,1) BETWEEN '0' AND '9')
        )"""


def _require_utc(value: str) -> None:
    if not str(value).endswith("Z"):
        raise FfPoolDocumentError("invalid_utc_timestamp", "Audit time must use UTC Z suffix")
    try:
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise FfPoolDocumentError("invalid_utc_timestamp", "Audit time must be ISO 8601 UTC") from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _guided_request_source_revision(
    *,
    supplier_source_revision: str,
    source_sha256: str,
) -> str:
    """Bind one guided request to raw supplier truth and exact workbook bytes."""

    raw_revision = str(supplier_source_revision or "").strip()
    workbook_sha256 = str(source_sha256 or "").strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", raw_revision) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", workbook_sha256
    ):
        raise FfPoolDocumentError(
            "supplier_source_revision_invalid",
            "Guided acceptance requires raw supplier and workbook SHA-256 revisions",
        )
    return _fingerprint(
        {
            "source_revision": raw_revision,
            "source_sha256": workbook_sha256,
        }
    )


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    raise FfPoolDocumentError("invalid_json_object", "Expected a JSON object")


def _connect(path: Path, *, query_only: bool = False) -> sqlite3.Connection:
    mode = "ro" if query_only else "rwc"
    conn = sqlite3.connect(f"file:{Path(path)}?mode={mode}", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    if query_only:
        conn.execute("PRAGMA query_only=ON")
    return conn
