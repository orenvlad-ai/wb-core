"""Exact FBS opening backfill and owner-gated post-cutover lifecycle.

The official WB collector is a read-only evidence source.  This module never
writes WB.  It folds immutable local observations into exact INTEGER quantity
and Decimal-text capital movements only after an applied cutover manifest has
activated the reviewed ``complete/sorted`` handoff rule.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import sqlite3
from typing import Any, Mapping

from packages.application.ff_pool_foundation import (
    BALANCES_TABLE,
    FACILITIES_TABLE,
    FEATURE_EPOCHS_TABLE,
    LINES_TABLE,
    OPERATIONS_TABLE,
    canonical_decimal_text,
    evaluate_ff_pool_aggregate_parity,
    ensure_ff_pool_foundation_schema,
    record_ff_pool_parity_diagnostic,
)
from packages.application.ff_pool_fbs_applicability import (
    FbsApplicabilityError,
    fbs_physical_component,
    require_fbs_pair_writeable,
)
from packages.application.wb_fbs_orders import (
    IDENTITY_EVIDENCE_TABLE,
    IDENTITY_MAPPINGS_TABLE,
    OBSERVATIONS_TABLE,
    STATUS_OBSERVATIONS_TABLE,
    WAREHOUSE_MAPPINGS_TABLE,
    ensure_wb_fbs_orders_schema,
)


CONTRACT_NAME = "ff_pool_fbs_lifecycle_v1"
EVENTS_TABLE = "sheet_vitrina_v1_ff_pool_fbs_lifecycle_events"
CURRENT_TABLE = "sheet_vitrina_v1_ff_pool_fbs_lifecycle_current"
RECONCILIATION_TABLE = "sheet_vitrina_v1_ff_pool_fbs_reconciliation_lane"
DRAIN_STATE_TABLE = "sheet_vitrina_v1_ff_pool_fbs_drain_state"
LATE_EVIDENCE_TABLE = "sheet_vitrina_v1_ff_pool_fbs_late_evidence"
IDENTITY_PENDING_TABLE = "sheet_vitrina_v1_ff_pool_fbs_identity_pending"
IDENTITY_PENDING_RESOLUTIONS_TABLE = (
    "sheet_vitrina_v1_ff_pool_fbs_identity_pending_resolutions"
)
MAPPING_EXTENSIONS_TABLE = "sheet_vitrina_v1_ff_pool_fbs_mapping_extensions"
MAPPING_EXTENSION_ALLOCATIONS_TABLE = (
    "sheet_vitrina_v1_ff_pool_fbs_mapping_extension_allocations"
)
FORWARD_GENERATIONS_TABLE = "sheet_vitrina_v1_ff_pool_fbs_forward_generations"
FORWARD_STATE_TABLE = "sheet_vitrina_v1_ff_pool_fbs_forward_state"
BACKLOG_RECOVERY_RUNS_TABLE = "sheet_vitrina_v1_ff_pool_fbs_backlog_recovery_runs"
BACKLOG_RECOVERY_TARGETS_TABLE = (
    "sheet_vitrina_v1_ff_pool_fbs_backlog_recovery_targets"
)

HANDOFF_SUPPLIER_STATUS = "complete"
HANDOFF_WB_STATUS = "sorted"
CANCELLATION_SUPPLIER_STATUSES = frozenset({"cancel"})
CANCELLATION_WB_STATUSES = frozenset(
    {"canceled", "canceled_by_client", "declined_by_client", "defect"}
)
LATER_TERMINAL_WB_STATUSES = frozenset({"sold", "accepted_by_client"})
EVENT_TYPES = (
    "opening_reserve",
    "opening_handoff_debit",
    "opening_cancel_noop",
    "cancel_noop",
    "reserve",
    "release",
    "handoff_debit",
    "terminal_noop",
    "status_noop",
    "post_handoff_reconciliation",
    "late_pre_t_isolated",
)
STATES = (
    "reserved",
    "released",
    "fulfilled",
    "fulfilled_reconciliation",
    "cancelled_noop",
    "late_pre_t_isolated",
)
ZERO = Decimal("0")


class FfPoolFbsLifecycleError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details = details


def ensure_ff_pool_fbs_lifecycle_schema(conn: sqlite3.Connection) -> None:
    ensure_ff_pool_foundation_schema(conn)
    ensure_wb_fbs_orders_schema(conn)
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {EVENTS_TABLE}(
            event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            cutover_id TEXT NOT NULL,
            order_id INTEGER NOT NULL CHECK(typeof(order_id)='integer' AND order_id>0),
            episode_sequence INTEGER NOT NULL
                CHECK(typeof(episode_sequence)='integer' AND episode_sequence>0),
            event_type TEXT NOT NULL CHECK(event_type IN ({_sql_values(EVENT_TYPES)})),
            source_order_observation_sequence INTEGER NOT NULL DEFAULT 0
                CHECK(typeof(source_order_observation_sequence)='integer'
                      AND source_order_observation_sequence>=0),
            source_status_observation_sequence INTEGER NOT NULL DEFAULT 0
                CHECK(typeof(source_status_observation_sequence)='integer'
                      AND source_status_observation_sequence>=0),
            source_revision TEXT NOT NULL,
            status_digest TEXT NOT NULL,
            supplier_status TEXT NOT NULL,
            wb_status TEXT NOT NULL,
            source_observed_at TEXT NOT NULL,
            facility_id TEXT NOT NULL,
            pool TEXT NOT NULL DEFAULT 'FBS' CHECK(pool='FBS'),
            nm_id INTEGER NOT NULL CHECK(typeof(nm_id)='integer' AND nm_id>0),
            quantity INTEGER NOT NULL CHECK(typeof(quantity)='integer' AND quantity>0),
            physical_quantity_delta INTEGER NOT NULL
                CHECK(typeof(physical_quantity_delta)='integer'),
            capital_delta_rub TEXT NOT NULL CHECK({_decimal_check('capital_delta_rub')}),
            frozen_wac_rub TEXT NOT NULL CHECK({_decimal_check('frozen_wac_rub')}),
            evidence_digest TEXT NOT NULL,
            occurred_at TEXT NOT NULL
                CHECK(substr(occurred_at,-1,1)='Z' AND julianday(occurred_at) IS NOT NULL),
            details_json TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(details_json)),
            UNIQUE(cutover_id,order_id,episode_sequence,event_type,evidence_digest)
        );
        CREATE INDEX IF NOT EXISTS ff_pool_fbs_events_by_order
        ON {EVENTS_TABLE}(cutover_id,order_id,event_sequence);
        CREATE INDEX IF NOT EXISTS ff_pool_fbs_events_by_location
        ON {EVENTS_TABLE}(facility_id,pool,nm_id,event_sequence);

        CREATE TABLE IF NOT EXISTS {CURRENT_TABLE}(
            cutover_id TEXT NOT NULL,
            order_id INTEGER NOT NULL,
            state TEXT NOT NULL CHECK(state IN ({_sql_values(STATES)})),
            episode_sequence INTEGER NOT NULL,
            source_revision TEXT NOT NULL,
            status_digest TEXT NOT NULL,
            supplier_status TEXT NOT NULL,
            wb_status TEXT NOT NULL,
            facility_id TEXT NOT NULL,
            pool TEXT NOT NULL DEFAULT 'FBS' CHECK(pool='FBS'),
            nm_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL CHECK(typeof(quantity)='integer' AND quantity>0),
            frozen_wac_rub TEXT NOT NULL CHECK({_decimal_check('frozen_wac_rub')}),
            debit_event_id TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(cutover_id,order_id)
        );
        CREATE INDEX IF NOT EXISTS ff_pool_fbs_current_by_location
        ON {CURRENT_TABLE}(cutover_id,facility_id,pool,nm_id,state,order_id);

        CREATE TABLE IF NOT EXISTS {RECONCILIATION_TABLE}(
            reconciliation_id TEXT PRIMARY KEY,
            cutover_id TEXT NOT NULL,
            order_id INTEGER NOT NULL,
            event_id TEXT NOT NULL UNIQUE REFERENCES {EVENTS_TABLE}(event_id),
            reason_code TEXT NOT NULL,
            evidence_digest TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'open' CHECK(state='open'),
            created_at TEXT NOT NULL,
            UNIQUE(cutover_id,order_id,evidence_digest)
        );

        CREATE TABLE IF NOT EXISTS {DRAIN_STATE_TABLE}(
            cutover_id TEXT PRIMARY KEY REFERENCES
                sheet_vitrina_v1_ff_pool_cutover_manifests(cutover_id),
            frozen_order_observation_sequence INTEGER NOT NULL
                CHECK(typeof(frozen_order_observation_sequence)='integer'
                      AND frozen_order_observation_sequence>=0),
            frozen_status_observation_sequence INTEGER NOT NULL
                CHECK(typeof(frozen_status_observation_sequence)='integer'
                      AND frozen_status_observation_sequence>=0),
            frozen_status_transition_sequence INTEGER NOT NULL
                CHECK(typeof(frozen_status_transition_sequence)='integer'
                      AND frozen_status_transition_sequence>=0),
            last_status_observation_sequence INTEGER NOT NULL
                CHECK(typeof(last_status_observation_sequence)='integer'
                      AND last_status_observation_sequence>=0),
            drain_run_count INTEGER NOT NULL DEFAULT 0
                CHECK(typeof(drain_run_count)='integer' AND drain_run_count>=0),
            last_result_digest TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
                CHECK(substr(updated_at,-1,1)='Z' AND julianday(updated_at) IS NOT NULL)
        );

        CREATE TABLE IF NOT EXISTS {LATE_EVIDENCE_TABLE}(
            evidence_id TEXT PRIMARY KEY,
            cutover_id TEXT NOT NULL REFERENCES
                sheet_vitrina_v1_ff_pool_cutover_manifests(cutover_id),
            order_id INTEGER NOT NULL CHECK(typeof(order_id)='integer' AND order_id>0),
            source_order_observation_sequence INTEGER NOT NULL
                CHECK(typeof(source_order_observation_sequence)='integer'
                      AND source_order_observation_sequence>0),
            source_status_observation_sequence INTEGER NOT NULL
                CHECK(typeof(source_status_observation_sequence)='integer'
                      AND source_status_observation_sequence>0),
            source_revision TEXT NOT NULL,
            status_digest TEXT NOT NULL,
            supplier_status TEXT NOT NULL,
            wb_status TEXT NOT NULL,
            observed_at TEXT NOT NULL
                CHECK(substr(observed_at,-1,1)='Z' AND julianday(observed_at) IS NOT NULL),
            reason_code TEXT NOT NULL DEFAULT 'late_pre_t',
            evidence_digest TEXT NOT NULL,
            created_at TEXT NOT NULL
                CHECK(substr(created_at,-1,1)='Z' AND julianday(created_at) IS NOT NULL),
            UNIQUE(cutover_id,order_id,source_status_observation_sequence)
        );

        CREATE TABLE IF NOT EXISTS {IDENTITY_PENDING_TABLE}(
            pending_id TEXT PRIMARY KEY,
            cutover_id TEXT NOT NULL REFERENCES
                sheet_vitrina_v1_ff_pool_cutover_manifests(cutover_id),
            order_id INTEGER NOT NULL CHECK(typeof(order_id)='integer' AND order_id>0),
            source_status_observation_sequence INTEGER NOT NULL
                CHECK(typeof(source_status_observation_sequence)='integer'
                      AND source_status_observation_sequence>0),
            order_revision TEXT NOT NULL,
            status_digest TEXT NOT NULL,
            deferred_identity_evidence_sequence INTEGER NOT NULL
                CHECK(typeof(deferred_identity_evidence_sequence)='integer'
                      AND deferred_identity_evidence_sequence>=0),
            reason_code TEXT NOT NULL
                CHECK(reason_code='identity_evidence_missing_or_drifted'),
            reason_detail_code TEXT NOT NULL DEFAULT
                'identity_evidence_missing_or_drifted'
                CHECK(reason_detail_code IN(
                    'identity_evidence_missing_or_drifted','order_sku_unmapped'
                )),
            evidence_digest TEXT NOT NULL,
            created_at TEXT NOT NULL
                CHECK(substr(created_at,-1,1)='Z' AND julianday(created_at) IS NOT NULL),
            UNIQUE(cutover_id,source_status_observation_sequence)
        );
        CREATE INDEX IF NOT EXISTS ff_pool_fbs_identity_pending_by_order
        ON {IDENTITY_PENDING_TABLE}(cutover_id,order_id,source_status_observation_sequence);

        CREATE TABLE IF NOT EXISTS {IDENTITY_PENDING_RESOLUTIONS_TABLE}(
            resolution_id TEXT PRIMARY KEY,
            pending_id TEXT NOT NULL UNIQUE REFERENCES {IDENTITY_PENDING_TABLE}(pending_id),
            cutover_id TEXT NOT NULL,
            order_id INTEGER NOT NULL CHECK(typeof(order_id)='integer' AND order_id>0),
            source_status_observation_sequence INTEGER NOT NULL
                CHECK(typeof(source_status_observation_sequence)='integer'
                      AND source_status_observation_sequence>0),
            matched_identity_evidence_sequence INTEGER NOT NULL
                CHECK(typeof(matched_identity_evidence_sequence)='integer'
                      AND matched_identity_evidence_sequence>0),
            matched_identity_order_revision TEXT NOT NULL,
            resolution_kind TEXT NOT NULL CHECK(resolution_kind='matched_replay'),
            resolution_digest TEXT NOT NULL,
            resolved_at TEXT NOT NULL
                CHECK(substr(resolved_at,-1,1)='Z' AND julianday(resolved_at) IS NOT NULL),
            UNIQUE(cutover_id,source_status_observation_sequence)
        );

        CREATE TABLE IF NOT EXISTS {MAPPING_EXTENSIONS_TABLE}(
            extension_id TEXT PRIMARY KEY,
            cutover_id TEXT NOT NULL REFERENCES
                sheet_vitrina_v1_ff_pool_cutover_manifests(cutover_id),
            warehouse_mapping_id TEXT NOT NULL REFERENCES
                {WAREHOUSE_MAPPINGS_TABLE}(mapping_id),
            seller_warehouse_id INTEGER NOT NULL
                CHECK(typeof(seller_warehouse_id)='integer' AND seller_warehouse_id>0),
            official_office_id INTEGER NOT NULL
                CHECK(typeof(official_office_id)='integer' AND official_office_id>0),
            facility_id TEXT NOT NULL,
            source_receipt_document_id TEXT NOT NULL,
            source_receipt_root_document_id TEXT NOT NULL,
            source_receipt_digest TEXT NOT NULL,
            mapping_digest TEXT NOT NULL,
            official_evidence_digest TEXT NOT NULL,
            frozen_boundary_json TEXT NOT NULL CHECK(json_valid(frozen_boundary_json)),
            frozen_rows_digest TEXT NOT NULL,
            plan_fingerprint TEXT NOT NULL,
            deployed_sha TEXT NOT NULL CHECK(length(deployed_sha)=40),
            approval_reference TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL
                CHECK(substr(created_at,-1,1)='Z' AND julianday(created_at) IS NOT NULL),
            UNIQUE(cutover_id,seller_warehouse_id),
            UNIQUE(cutover_id,warehouse_mapping_id)
        );
        CREATE INDEX IF NOT EXISTS ff_pool_fbs_mapping_extension_by_facility
        ON {MAPPING_EXTENSIONS_TABLE}(cutover_id,facility_id,seller_warehouse_id);

        CREATE TABLE IF NOT EXISTS {MAPPING_EXTENSION_ALLOCATIONS_TABLE}(
            extension_id TEXT NOT NULL REFERENCES
                {MAPPING_EXTENSIONS_TABLE}(extension_id),
            nm_id INTEGER NOT NULL CHECK(typeof(nm_id)='integer' AND nm_id>0),
            opening_quantity INTEGER NOT NULL
                CHECK(typeof(opening_quantity)='integer' AND opening_quantity>=0),
            opening_capital_rub TEXT NOT NULL CHECK({_decimal_check('opening_capital_rub')}),
            frozen_wac_rub TEXT NOT NULL CHECK({_decimal_check('frozen_wac_rub')}),
            source_balance_watermark TEXT NOT NULL,
            allocation_digest TEXT NOT NULL,
            created_at TEXT NOT NULL
                CHECK(substr(created_at,-1,1)='Z' AND julianday(created_at) IS NOT NULL),
            PRIMARY KEY(extension_id,nm_id)
        );

        CREATE TABLE IF NOT EXISTS {FORWARD_GENERATIONS_TABLE}(
            generation_id TEXT PRIMARY KEY,
            cutover_id TEXT NOT NULL UNIQUE REFERENCES
                sheet_vitrina_v1_ff_pool_cutover_manifests(cutover_id),
            contract_version INTEGER NOT NULL CHECK(contract_version=1),
            deployed_sha TEXT NOT NULL CHECK(length(deployed_sha)=40),
            storage_generation_id TEXT NOT NULL,
            storage_schema_revision TEXT NOT NULL,
            sqlite_schema_version INTEGER NOT NULL
                CHECK(typeof(sqlite_schema_version)='integer'
                      AND sqlite_schema_version>0),
            cutoff_status_observation_sequence INTEGER NOT NULL
                CHECK(typeof(cutoff_status_observation_sequence)='integer'
                      AND cutoff_status_observation_sequence>=0),
            forward_start_status_observation_sequence INTEGER NOT NULL
                CHECK(typeof(forward_start_status_observation_sequence)='integer'
                      AND forward_start_status_observation_sequence=
                          cutoff_status_observation_sequence+1),
            old_cursor_status_observation_sequence INTEGER NOT NULL
                CHECK(typeof(old_cursor_status_observation_sequence)='integer'
                      AND old_cursor_status_observation_sequence>=0
                      AND old_cursor_status_observation_sequence<=
                          cutoff_status_observation_sequence),
            manifest_fingerprint TEXT NOT NULL UNIQUE,
            stable_target_digest TEXT NOT NULL,
            approval_reference TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL
                CHECK(substr(created_at,-1,1)='Z' AND julianday(created_at) IS NOT NULL)
        );

        CREATE TABLE IF NOT EXISTS {FORWARD_STATE_TABLE}(
            generation_id TEXT PRIMARY KEY REFERENCES {FORWARD_GENERATIONS_TABLE}(generation_id),
            last_status_observation_sequence INTEGER NOT NULL
                CHECK(typeof(last_status_observation_sequence)='integer'
                      AND last_status_observation_sequence>=0),
            run_count INTEGER NOT NULL DEFAULT 0
                CHECK(typeof(run_count)='integer' AND run_count>=0),
            last_result_digest TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
                CHECK(substr(updated_at,-1,1)='Z' AND julianday(updated_at) IS NOT NULL)
        );

        CREATE TABLE IF NOT EXISTS {BACKLOG_RECOVERY_RUNS_TABLE}(
            recovery_id TEXT PRIMARY KEY,
            generation_id TEXT NOT NULL UNIQUE REFERENCES
                {FORWARD_GENERATIONS_TABLE}(generation_id),
            cutover_id TEXT NOT NULL,
            manifest_fingerprint TEXT NOT NULL UNIQUE,
            stable_target_digest TEXT NOT NULL,
            result_digest TEXT NOT NULL,
            summary_json TEXT NOT NULL CHECK(json_valid(summary_json)),
            target_count INTEGER NOT NULL
                CHECK(typeof(target_count)='integer' AND target_count>=0),
            status TEXT NOT NULL CHECK(status='completed'),
            applied_at TEXT NOT NULL
                CHECK(substr(applied_at,-1,1)='Z' AND julianday(applied_at) IS NOT NULL)
        );

        CREATE TABLE IF NOT EXISTS {BACKLOG_RECOVERY_TARGETS_TABLE}(
            recovery_id TEXT NOT NULL REFERENCES {BACKLOG_RECOVERY_RUNS_TABLE}(recovery_id),
            source_status_observation_sequence INTEGER NOT NULL
                CHECK(typeof(source_status_observation_sequence)='integer'
                      AND source_status_observation_sequence>0),
            order_id INTEGER NOT NULL CHECK(typeof(order_id)='integer' AND order_id>0),
            stable_business_digest TEXT NOT NULL,
            before_state_digest TEXT NOT NULL,
            after_state_digest TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK(outcome IN(
                'event_applied','identity_quarantine','already_current','audit_noop'
            )),
            PRIMARY KEY(recovery_id,source_status_observation_sequence)
        );

        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_events_no_update
        BEFORE UPDATE ON {EVENTS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS lifecycle evidence is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_events_no_delete
        BEFORE DELETE ON {EVENTS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS lifecycle evidence is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_reconciliation_no_update
        BEFORE UPDATE ON {RECONCILIATION_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS reconciliation evidence is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_reconciliation_no_delete
        BEFORE DELETE ON {RECONCILIATION_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS reconciliation evidence is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_late_evidence_no_update
        BEFORE UPDATE ON {LATE_EVIDENCE_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS late evidence is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_late_evidence_no_delete
        BEFORE DELETE ON {LATE_EVIDENCE_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS late evidence is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_identity_pending_no_update
        BEFORE UPDATE ON {IDENTITY_PENDING_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS identity pending evidence is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_identity_pending_no_delete
        BEFORE DELETE ON {IDENTITY_PENDING_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS identity pending evidence is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_identity_resolution_no_update
        BEFORE UPDATE ON {IDENTITY_PENDING_RESOLUTIONS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS identity resolution evidence is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_identity_resolution_no_delete
        BEFORE DELETE ON {IDENTITY_PENDING_RESOLUTIONS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS identity resolution evidence is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_mapping_extension_no_update
        BEFORE UPDATE ON {MAPPING_EXTENSIONS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS mapping extension is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_mapping_extension_no_delete
        BEFORE DELETE ON {MAPPING_EXTENSIONS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS mapping extensions are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_extension_allocation_no_update
        BEFORE UPDATE ON {MAPPING_EXTENSION_ALLOCATIONS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS mapping extension allocation is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_extension_allocation_no_delete
        BEFORE DELETE ON {MAPPING_EXTENSION_ALLOCATIONS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS mapping extension allocations are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_forward_generation_no_update
        BEFORE UPDATE ON {FORWARD_GENERATIONS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS forward generation is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_forward_generation_no_delete
        BEFORE DELETE ON {FORWARD_GENERATIONS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS forward generations are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_recovery_run_no_update
        BEFORE UPDATE ON {BACKLOG_RECOVERY_RUNS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS backlog recovery result is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_recovery_run_no_delete
        BEFORE DELETE ON {BACKLOG_RECOVERY_RUNS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS backlog recovery results are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_recovery_target_no_update
        BEFORE UPDATE ON {BACKLOG_RECOVERY_TARGETS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS backlog recovery target is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_recovery_target_no_delete
        BEFORE DELETE ON {BACKLOG_RECOVERY_TARGETS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS backlog recovery targets are append-only'); END;
        """
    )
    _ensure_column(
        conn,
        table=EVENTS_TABLE,
        column="source_order_observation_sequence",
        declaration="INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        conn,
        table=IDENTITY_PENDING_TABLE,
        column="reason_detail_code",
        declaration=(
            "TEXT NOT NULL DEFAULT 'identity_evidence_missing_or_drifted' "
            "CHECK(reason_detail_code IN("
            "'identity_evidence_missing_or_drifted','order_sku_unmapped'))"
        ),
    )
    _ensure_column(
        conn,
        table=EVENTS_TABLE,
        column="source_status_observation_sequence",
        declaration="INTEGER NOT NULL DEFAULT 0",
    )
    conn.execute(
        f"""CREATE UNIQUE INDEX IF NOT EXISTS ff_pool_fbs_event_by_status_sequence
            ON {EVENTS_TABLE}(
                cutover_id,order_id,source_status_observation_sequence,event_type
            ) WHERE source_status_observation_sequence>0"""
    )


def classify_pre_t_status(
    conn: sqlite3.Connection,
    *,
    order_id: int,
    cutover_at: str,
    max_observation_sequence: int | None = None,
) -> dict[str, Any]:
    """Classify one order from immutable status observations visible by T."""

    rows = conn.execute(
        f"""SELECT observation_sequence,order_revision,status_digest,
                   supplier_status,wb_status,positive_quantity,observed_at
            FROM {STATUS_OBSERVATIONS_TABLE}
            WHERE order_id=? AND observed_at<=?
              AND (? IS NULL OR observation_sequence<=?)
            ORDER BY observation_sequence""",
        (
            int(order_id),
            str(cutover_at),
            max_observation_sequence,
            max_observation_sequence,
        ),
    ).fetchall()
    if not rows:
        return {"classification": "unmatched", "evidence": None}
    handoff_index = next(
        (
            index
            for index, row in enumerate(rows)
            if str(row[3]) == HANDOFF_SUPPLIER_STATUS
            and str(row[4]) == HANDOFF_WB_STATUS
        ),
        None,
    )
    handoff = None if handoff_index is None else rows[handoff_index]
    reconciliation = None
    if handoff is not None:
        classification = "pre_t_handoff_debit"
        evidence = handoff
        reconciliation = next(
            (
                row
                for row in reversed(rows[handoff_index + 1 :])
                if _is_cancellation(str(row[3]), str(row[4]))
            ),
            None,
        )
    else:
        latest = rows[-1]
        if _is_cancellation(str(latest[3]), str(latest[4])):
            classification = "pre_t_cancelled_noop"
        else:
            classification = "pre_t_absorbed_reservation"
        evidence = latest
    result = {
        "classification": classification,
        "evidence": {
            "observation_sequence": int(evidence[0]),
            "source_revision": str(evidence[1]),
            "status_digest": str(evidence[2]),
            "supplier_status": str(evidence[3]),
            "wb_status": str(evidence[4]),
            "quantity": int(evidence[5]),
            "observed_at": str(evidence[6]),
        },
    }
    if reconciliation is not None:
        result["post_handoff_reconciliation"] = {
            "observation_sequence": int(reconciliation[0]),
            "source_revision": str(reconciliation[1]),
            "status_digest": str(reconciliation[2]),
            "supplier_status": str(reconciliation[3]),
            "wb_status": str(reconciliation[4]),
            "quantity": int(reconciliation[5]),
            "observed_at": str(reconciliation[6]),
        }
    return result


def apply_opening_fbs_backfill(
    conn: sqlite3.Connection,
    *,
    manifest: Mapping[str, Any],
    occurred_at: str,
) -> dict[str, Any]:
    """Fold the exact pre-T checkpoint once inside the cutover transaction."""

    _require_approved_policy(manifest)
    cutover_id = str(manifest["cutover_id"])
    counts = {"debit": 0, "reservation": 0, "cancel_noop": 0, "reconciliation": 0}
    quantity = {"debit": 0, "reservation": 0}
    capital = ZERO
    for order in sorted(
        (dict(item) for item in manifest.get("order_classifications") or []),
        key=lambda item: int(item["order_id"]),
    ):
        classification = str(order["classification"])
        if classification == "unmatched" or classification == "post_t_deferred":
            continue
        wac = _frozen_wac(conn, manifest, order)
        evidence = {
            "cutover_id": cutover_id,
            "order_id": int(order["order_id"]),
            "source_revision": str(order["source_revision"]),
            "status_digest": str(order["status_fingerprint"]),
            "classification": classification,
        }
        status_evidence = dict(order.get("status_evidence") or {})
        supplier_status = str(status_evidence.get("supplier_status") or "")
        wb_status = str(status_evidence.get("wb_status") or "")
        status_digest = str(
            status_evidence.get("status_digest") or order["status_fingerprint"]
        )
        status_revision = str(
            status_evidence.get("source_revision") or order["source_revision"]
        )
        status_observed_at = str(
            status_evidence.get("observed_at") or order["observed_at"]
        )
        order_observation_sequence = int(order.get("observation_sequence") or 0)
        status_observation_sequence = int(
            status_evidence.get("observation_sequence") or 0
        )
        if classification in {"pre_t_handoff_debit", "pre_t_absorbed_closed"}:
            event = _append_event(
                conn,
                manifest=manifest,
                order=order,
                episode_sequence=1,
                event_type="opening_handoff_debit",
                state="fulfilled",
                supplier_status=supplier_status,
                wb_status=wb_status,
                status_digest=status_digest,
                source_revision=status_revision,
                source_observed_at=status_observed_at,
                wac=wac,
                physical_delta=-int(order["quantity"]),
                occurred_at=occurred_at,
                evidence=evidence,
                source_order_observation_sequence=order_observation_sequence,
                source_status_observation_sequence=status_observation_sequence,
            )
            if not event["idempotent"]:
                _apply_exact_physical_delta(
                    conn,
                    manifest=manifest,
                    order=order,
                    event_id=str(event["event_id"]),
                    quantity_delta=-int(order["quantity"]),
                    wac=wac,
                    occurred_at=occurred_at,
                )
            counts["debit"] += 1
            quantity["debit"] += int(order["quantity"])
            capital = _decimal_sum(
                capital,
                _capital_delta(wac, int(order["quantity"])),
            )
            reconciliation_evidence = dict(
                order.get("post_handoff_reconciliation") or {}
            )
            if reconciliation_evidence:
                reconciliation_event = _append_event(
                    conn,
                    manifest=manifest,
                    order=order,
                    episode_sequence=1,
                    event_type="post_handoff_reconciliation",
                    state="fulfilled_reconciliation",
                    supplier_status=str(
                        reconciliation_evidence["supplier_status"]
                    ),
                    wb_status=str(reconciliation_evidence["wb_status"]),
                    status_digest=str(reconciliation_evidence["status_digest"]),
                    source_revision=str(
                        reconciliation_evidence["source_revision"]
                    ),
                    source_observed_at=str(
                        reconciliation_evidence["observed_at"]
                    ),
                    wac=wac,
                    physical_delta=0,
                    occurred_at=occurred_at,
                    evidence={
                        **evidence,
                        "lane": "post_handoff_cancellation_or_return",
                        "reconciliation_status": reconciliation_evidence,
                    },
                    source_order_observation_sequence=order_observation_sequence,
                    source_status_observation_sequence=int(
                        reconciliation_evidence.get("observation_sequence") or 0
                    ),
                )
                _persist_reconciliation(
                    conn,
                    manifest=manifest,
                    order=order,
                    event=reconciliation_event,
                    created_at=occurred_at,
                )
                counts["reconciliation"] += 1
        elif classification == "pre_t_absorbed_reservation":
            _append_event(
                conn,
                manifest=manifest,
                order=order,
                episode_sequence=1,
                event_type="opening_reserve",
                state="reserved",
                supplier_status=supplier_status,
                wb_status=wb_status,
                status_digest=status_digest,
                source_revision=status_revision,
                source_observed_at=status_observed_at,
                wac=wac,
                physical_delta=0,
                occurred_at=occurred_at,
                evidence=evidence,
                source_order_observation_sequence=order_observation_sequence,
                source_status_observation_sequence=status_observation_sequence,
            )
            counts["reservation"] += 1
            quantity["reservation"] += int(order["quantity"])
        elif classification == "pre_t_cancelled_noop":
            _append_event(
                conn,
                manifest=manifest,
                order=order,
                episode_sequence=1,
                event_type="opening_cancel_noop",
                state="cancelled_noop",
                supplier_status=supplier_status,
                wb_status=wb_status,
                status_digest=status_digest,
                source_revision=status_revision,
                source_observed_at=status_observed_at,
                wac=wac,
                physical_delta=0,
                occurred_at=occurred_at,
                evidence=evidence,
                source_order_observation_sequence=order_observation_sequence,
                source_status_observation_sequence=status_observation_sequence,
            )
            counts["cancel_noop"] += 1
        else:
            raise FfPoolFbsLifecycleError(
                "unsupported_opening_classification",
                f"Unsupported opening classification: {classification}",
            )
    return {
        "counts": counts,
        "quantity": quantity,
        "debit_capital_rub": canonical_decimal_text(capital),
    }


def process_post_t_fbs_lifecycle(
    conn: sqlite3.Connection,
    *,
    occurred_at: str | None = None,
    limit: int = 500,
    schema_ready: bool = False,
) -> dict[str, Any]:
    """Process a bounded exact suffix of the frozen checkpoint stream."""

    if not schema_ready:
        ensure_ff_pool_fbs_lifecycle_schema(conn)
    manifest_row = conn.execute(
        """SELECT manifest_json FROM sheet_vitrina_v1_ff_pool_cutover_manifests
           ORDER BY cutover_at DESC,cutover_id DESC LIMIT 1"""
    ).fetchone()
    if manifest_row is None:
        return {
            "contract_name": CONTRACT_NAME,
            "status": "disabled",
            "reason": "cutover_not_applied",
            "mutates_wb": False,
        }
    manifest = json.loads(str(manifest_row[0]))
    _require_approved_policy(manifest)
    feature = conn.execute(
        f"SELECT writer_enabled FROM {FEATURE_EPOCHS_TABLE} WHERE epoch=?",
        (int(manifest["feature_epoch"]),),
    ).fetchone()
    if feature is None or int(feature[0]) != 1:
        return {
            "contract_name": CONTRACT_NAME,
            "status": "disabled",
            "reason": "writer_epoch_not_active",
            "mutates_wb": False,
        }
    return drain_post_checkpoint_fbs_lifecycle(
        conn,
        manifest=manifest,
        occurred_at=_utc_now() if occurred_at is None else str(occurred_at),
        limit=limit,
    )


def drain_post_checkpoint_fbs_lifecycle(
    conn: sqlite3.Connection,
    *,
    manifest: Mapping[str, Any],
    occurred_at: str,
    limit: int = 500,
    pinned_status_observation_sequences: tuple[int, ...] | None = None,
) -> dict[str, Any]:
    """Drain immutable status observations above frozen ``W`` exactly once.

    The caller owns the transaction.  Progress advances atomically with every
    reservation/debit/reconciliation effect, so a crash replays no physical
    movement and a retry resumes from the same status sequence.
    """

    _require_approved_policy(manifest)
    now = str(occurred_at)
    _require_utc(now)
    bound = max(1, min(int(limit), 100_000))
    cutover_id = str(manifest["cutover_id"])
    boundary = dict(manifest.get("accounting_boundary") or {})
    order_w = _exact_int(
        boundary.get(
            "order_observation_watermark_sequence",
            manifest.get("observation_watermark_sequence", 0),
        ),
        "accounting_boundary.order_observation_watermark_sequence",
    )
    status_w = _exact_int(
        boundary.get("status_observation_watermark_sequence", 0),
        "accounting_boundary.status_observation_watermark_sequence",
    )
    transition_w = _exact_int(
        boundary.get("status_transition_watermark_sequence", 0),
        "accounting_boundary.status_transition_watermark_sequence",
    )
    boundary_at = str(boundary.get("local_boundary_at") or manifest["cutover_at"])
    _require_utc(boundary_at)
    pinned_sequences = (
        tuple(
            sorted(
                {
                    _exact_int(value, "pinned_status_observation_sequence")
                    for value in pinned_status_observation_sequences
                }
            )
        )
        if pinned_status_observation_sequences is not None
        else None
    )
    if pinned_sequences is not None and len(pinned_sequences) > bound:
        raise FfPoolFbsLifecycleError(
            "recovery_target_too_large",
            f"Pinned recovery target exceeds the bounded limit {bound}",
        )
    forward_generation = None
    if pinned_sequences is None:
        forward_generation = conn.execute(
            f"""SELECT generation_id,cutoff_status_observation_sequence,
                       forward_start_status_observation_sequence,
                       old_cursor_status_observation_sequence
                FROM {FORWARD_GENERATIONS_TABLE}
                WHERE cutover_id=?""",
            (cutover_id,),
        ).fetchone()
    lane = "backlog_recovery" if pinned_sequences is not None else "ordinary"
    generation_id = ""
    cutoff_sequence = 0
    if pinned_sequences is not None:
        if any(value <= status_w for value in pinned_sequences):
            raise FfPoolFbsLifecycleError(
                "recovery_target_before_frozen_boundary",
                "Pinned recovery statuses must be above the immutable Stage 7C watermark",
            )
        last_sequence = min(pinned_sequences, default=status_w) - 1
        starting_sequence = last_sequence
    elif forward_generation is not None:
        generation_id = str(forward_generation[0])
        cutoff_sequence = int(forward_generation[1])
        if int(forward_generation[2]) != cutoff_sequence + 1:
            raise FfPoolFbsLifecycleError(
                "forward_generation_boundary_drift",
                "Forward generation start is not the exact C+1 boundary",
            )
        state = conn.execute(
            f"""SELECT last_status_observation_sequence
                FROM {FORWARD_STATE_TABLE} WHERE generation_id=?""",
            (generation_id,),
        ).fetchone()
        if state is None or int(state[0]) < cutoff_sequence:
            raise FfPoolFbsLifecycleError(
                "forward_generation_state_missing",
                "Forward generation has no valid durable C+1 cursor",
            )
        lane = "forward"
        last_sequence = int(state[0])
        starting_sequence = last_sequence
    else:
        conn.execute(
            f"""INSERT OR IGNORE INTO {DRAIN_STATE_TABLE}(
                    cutover_id,frozen_order_observation_sequence,
                    frozen_status_observation_sequence,
                    frozen_status_transition_sequence,last_status_observation_sequence,
                    drain_run_count,last_result_digest,updated_at
                ) VALUES(?,?,?,?,?,0,'',?)""",
            (cutover_id, order_w, status_w, transition_w, status_w, now),
        )
        state = conn.execute(
            f"""SELECT frozen_order_observation_sequence,
                       frozen_status_observation_sequence,
                       frozen_status_transition_sequence,last_status_observation_sequence
                FROM {DRAIN_STATE_TABLE} WHERE cutover_id=?""",
            (cutover_id,),
        ).fetchone()
        if state is None or tuple(map(int, state[:3])) != (order_w, status_w, transition_w):
            raise FfPoolFbsLifecycleError(
                "drain_checkpoint_conflict",
                "Persisted FBS drain checkpoint differs from the immutable manifest",
            )
        last_sequence = int(state[3])
        starting_sequence = last_sequence
    pending_retry_limit = min(bound, 500)
    if pinned_sequences is not None:
        # A production recovery target can exceed SQLite's platform-specific
        # host-parameter limit.  Materialize only the reviewed integer
        # identities in a connection-local TEMP table, in bounded chunks, so
        # the same canonical query is portable at the released 100k ceiling.
        conn.execute(
            """CREATE TEMP TABLE IF NOT EXISTS ff_pool_fbs_pinned_status_targets(
                   observation_sequence INTEGER PRIMARY KEY
               ) WITHOUT ROWID"""
        )
        conn.execute("DELETE FROM temp.ff_pool_fbs_pinned_status_targets")
        conn.executemany(
            "INSERT INTO temp.ff_pool_fbs_pinned_status_targets(observation_sequence) VALUES(?)",
            ((value,) for value in pinned_sequences),
        )
        pinned_rows = (
            conn.execute(
                f"""SELECT status.observation_sequence,status.order_id,
                           status.order_revision,status.status_digest,
                           status.supplier_status,status.wb_status,
                           status.positive_quantity,status.observed_at,
                           source.observation_sequence,source.observation_id,
                           source.source_revision,source.source_created_at,
                           source.observed_at,source.warehouse_id,source.nm_id,
                           source.chrt_id,source.skus_json,source.office_id,
                           pending.deferred_identity_evidence_sequence
                    FROM {STATUS_OBSERVATIONS_TABLE} AS status
                    JOIN temp.ff_pool_fbs_pinned_status_targets AS target
                      ON target.observation_sequence=status.observation_sequence
                    LEFT JOIN {OBSERVATIONS_TABLE} AS source
                      ON source.order_id=status.order_id
                     AND source.source_revision=status.order_revision
                    LEFT JOIN {IDENTITY_PENDING_TABLE} AS pending
                      ON pending.cutover_id=?
                     AND pending.source_status_observation_sequence=
                         status.observation_sequence
                    LEFT JOIN {IDENTITY_PENDING_RESOLUTIONS_TABLE} AS resolution
                      ON resolution.pending_id=pending.pending_id
                    WHERE resolution.pending_id IS NULL
                    ORDER BY status.observation_sequence""",
                (cutover_id,),
            ).fetchall()
            if pinned_sequences
            else []
        )
        if len(pinned_rows) != len(pinned_sequences):
            raise FfPoolFbsLifecycleError(
                "recovery_target_status_drift",
                "One or more pinned recovery status identities are unavailable or resolved",
            )
        pending_rows = []
        new_rows = []
        rows = [(row, row[18] is not None) for row in pinned_rows]
    else:
        pending_floor_sql = (
            " AND pending.source_status_observation_sequence>?"
            if lane == "forward"
            else ""
        )
        pending_parameters: tuple[Any, ...] = (
            (cutover_id, cutoff_sequence, pending_retry_limit)
            if lane == "forward"
            else (cutover_id, pending_retry_limit)
        )
        pending_rows = conn.execute(
            f"""SELECT status.observation_sequence,status.order_id,
                       status.order_revision,status.status_digest,
                       status.supplier_status,status.wb_status,
                       status.positive_quantity,status.observed_at,
                       source.observation_sequence,source.observation_id,
                       source.source_revision,source.source_created_at,
                       source.observed_at,source.warehouse_id,source.nm_id,
                       source.chrt_id,source.skus_json,source.office_id,
                       pending.deferred_identity_evidence_sequence
                FROM {IDENTITY_PENDING_TABLE} AS pending
                JOIN {STATUS_OBSERVATIONS_TABLE} AS status
                  ON status.observation_sequence=pending.source_status_observation_sequence
                LEFT JOIN {OBSERVATIONS_TABLE} AS source
                  ON source.order_id=status.order_id
                 AND source.source_revision=status.order_revision
                LEFT JOIN {IDENTITY_PENDING_RESOLUTIONS_TABLE} AS resolution
                  ON resolution.pending_id=pending.pending_id
                WHERE pending.cutover_id=? AND resolution.pending_id IS NULL
                      {pending_floor_sql}
                ORDER BY EXISTS(
                    SELECT 1 FROM {IDENTITY_EVIDENCE_TABLE} AS later_identity
                    WHERE later_identity.order_id=pending.order_id
                      AND later_identity.evidence_sequence>
                          pending.deferred_identity_evidence_sequence
                      AND later_identity.outcome='matched'
                ) DESC,pending.source_status_observation_sequence
                LIMIT ?""",
            pending_parameters,
        ).fetchall()
        new_rows = conn.execute(
            f"""SELECT status.observation_sequence,status.order_id,
                       status.order_revision,status.status_digest,
                       status.supplier_status,status.wb_status,
                       status.positive_quantity,status.observed_at,
                       source.observation_sequence,source.observation_id,
                       source.source_revision,source.source_created_at,
                       source.observed_at,source.warehouse_id,source.nm_id,
                       source.chrt_id,source.skus_json,source.office_id
                FROM {STATUS_OBSERVATIONS_TABLE} AS status
                LEFT JOIN {OBSERVATIONS_TABLE} AS source
                  ON source.order_id=status.order_id
                 AND source.source_revision=status.order_revision
                WHERE status.observation_sequence>?
                ORDER BY status.observation_sequence
                LIMIT ?""",
            (last_sequence, bound),
        ).fetchall()
        rows = [(row, True) for row in pending_rows] + [
            (row, False) for row in new_rows
        ]
    summary = {
        "reserved": 0,
        "reservation_refreshed": 0,
        "released": 0,
        "fulfilled": 0,
        "terminal_noop": 0,
        "status_noop": 0,
        "cancel_noop": 0,
        "reconciliation": 0,
        "late_pre_t": 0,
        "identity_pending": 0,
        "identity_resolved": 0,
    }
    for row, is_pending_retry in rows:
        status_sequence = int(row[0])
        order_id = int(row[1])
        if row[8] is None:
            raise FfPoolFbsLifecycleError(
                "status_order_revision_missing",
                f"Status sequence {status_sequence} has no exact order revision",
            )
        order_sequence = int(row[8])
        known_at_boundary = conn.execute(
            """SELECT 1 FROM sheet_vitrina_v1_ff_pool_cutover_order_classifications
               WHERE cutover_id=? AND order_id=?""",
            (cutover_id, order_id),
        ).fetchone() is not None
        already_late = conn.execute(
            f"SELECT 1 FROM {LATE_EVIDENCE_TABLE} WHERE cutover_id=? AND order_id=? LIMIT 1",
            (cutover_id, order_id),
        ).fetchone() is not None
        locally_pre_boundary = (
            _parse_utc(str(row[7])) <= _parse_utc(boundary_at)
            or _parse_utc(str(row[12])) <= _parse_utc(boundary_at)
        )
        if not known_at_boundary and (already_late or locally_pre_boundary):
            if _persist_late_checkpoint_evidence(
                conn,
                manifest=manifest,
                row=row,
                detected_at=now,
            ):
                summary["late_pre_t"] += 1
            last_sequence = max(last_sequence, status_sequence)
            continue

        raw_order = (
            str(row[9]),
            order_id,
            str(row[10]),
            str(row[11]),
            str(row[12]),
            row[13],
            int(row[14]),
            row[15],
            str(row[16]),
            row[17],
        )
        try:
            mapped = _map_order(
                conn,
                manifest,
                raw_order,
                allow_compatible_identity=is_pending_retry,
                minimum_identity_evidence_sequence=(
                    int(row[18]) if is_pending_retry else 0
                ),
            )
        except FfPoolFbsLifecycleError as exc:
            if exc.code not in {
                "order_identity_evidence_missing_or_drifted",
                "order_sku_unmapped",
            }:
                raise
            # Installing the inert forward-recovery schema during deploy must
            # not silently drain the pre-existing suffix before its exact C
            # manifest has received the separate production-mutation gate.
            # The legacy lane therefore preserves its fail-closed rollback for
            # a known-facility/missing-SKU row.  Quarantine-and-continue is
            # activated only by the durable forward generation or by the
            # explicitly pinned recovery lane created in the same apply.
            if exc.code == "order_sku_unmapped" and lane == "ordinary":
                raise
            if not is_pending_retry:
                _persist_identity_pending(
                    conn,
                    manifest=manifest,
                    row=row,
                    created_at=now,
                    reason_detail_code=(
                        "identity_evidence_missing_or_drifted"
                        if exc.code == "order_identity_evidence_missing_or_drifted"
                        else exc.code
                    ),
                )
                last_sequence = max(last_sequence, status_sequence)
            summary["identity_pending"] += 1
            continue
        order = _order_payload(raw_order, mapped, quantity=int(row[6]))
        try:
            require_fbs_pair_writeable(
                conn,
                facility_id=str(order["facility_id"]),
                nm_id=int(order["nm_id"]),
                effective_date=str(row[7])[:10],
                projection_epoch=int(manifest["feature_epoch"]),
            )
        except FbsApplicabilityError as exc:
            raise FfPoolFbsLifecycleError(exc.code, str(exc)) from exc
        state_row = conn.execute(
            f"SELECT state,debit_event_id FROM {CURRENT_TABLE} WHERE cutover_id=? AND order_id=?",
            (cutover_id, order_id),
        ).fetchone()
        supplier_status = str(row[4])
        wb_status = str(row[5])
        wac = _frozen_wac(conn, manifest, order)
        evidence = {
            "order_id": order_id,
            "order_revision": str(row[2]),
            "status_digest": str(row[3]),
            "source_order_observation_sequence": order_sequence,
            "source_status_observation_sequence": status_sequence,
            "supplier_status": supplier_status,
            "wb_status": wb_status,
            "mapping_extension_id": str(
                order.get("mapping_extension_id") or ""
            ),
        }
        common = dict(
            conn=conn,
            manifest=manifest,
            order=order,
            episode_sequence=status_sequence,
            supplier_status=supplier_status,
            wb_status=wb_status,
            status_digest=str(row[3]),
            source_revision=str(row[2]),
            source_observed_at=str(row[7]),
            wac=wac,
            occurred_at=now,
            evidence=evidence,
            source_order_observation_sequence=order_sequence,
            source_status_observation_sequence=status_sequence,
        )
        state = "" if state_row is None else str(state_row[0])
        if not state:
            if _is_cancellation(supplier_status, wb_status):
                event = _append_event(
                    **common,
                    event_type="cancel_noop",
                    state="cancelled_noop",
                    physical_delta=0,
                )
                if not event["idempotent"]:
                    summary["cancel_noop"] += 1
                if is_pending_retry:
                    summary["identity_resolved"] += _resolve_identity_pending(
                        conn,
                        cutover_id=cutover_id,
                        source_status_observation_sequence=status_sequence,
                        matched_identity_evidence_sequence=int(
                            mapped["identity_evidence_sequence"]
                        ),
                        matched_identity_order_revision=str(
                            mapped["identity_evidence_order_revision"]
                        ),
                        resolved_at=now,
                    )
                last_sequence = max(last_sequence, status_sequence)
                continue
            reserve = _append_event(
                **common, event_type="reserve", state="reserved", physical_delta=0
            )
            if not reserve["idempotent"]:
                summary["reserved"] += 1
            state = "reserved"
        elif state in {"released", "cancelled_noop"} and not _is_cancellation(
            supplier_status, wb_status
        ):
            reserve = _append_event(
                **common, event_type="reserve", state="reserved", physical_delta=0
            )
            if not reserve["idempotent"]:
                summary["reserved"] += 1
            state = "reserved"

        if state == "reserved" and _is_cancellation(supplier_status, wb_status):
            event = _append_event(
                **common, event_type="release", state="released", physical_delta=0
            )
            if not event["idempotent"]:
                summary["released"] += 1
        elif state == "reserved" and _is_handoff(supplier_status, wb_status):
            handoff_wac, cost_basis = _current_handoff_wac(
                conn,
                manifest=manifest,
                order=order,
            )
            stable_cost_basis = {
                key: value
                for key, value in cost_basis.items()
                if key not in {
                    "balance_updated_at",
                    "source_operation_posted_at",
                }
            }
            handoff_common = {
                **common,
                "wac": handoff_wac,
                "evidence": {**evidence, "cost_basis": stable_cost_basis},
                "details_evidence": {
                    **evidence,
                    "cost_basis": cost_basis,
                    "technical_timestamp_fields_excluded_from_identity": [
                        "cost_basis.balance_updated_at",
                        "cost_basis.source_operation_posted_at",
                    ],
                },
            }
            event = _append_event(
                **handoff_common,
                event_type="handoff_debit",
                state="fulfilled",
                physical_delta=-int(order["quantity"]),
            )
            if not event["idempotent"]:
                _apply_exact_physical_delta(
                    conn,
                    manifest=manifest,
                    order=order,
                    event_id=str(event["event_id"]),
                    quantity_delta=-int(order["quantity"]),
                    wac=handoff_wac,
                    occurred_at=now,
                )
                summary["fulfilled"] += 1
        elif state == "reserved":
            refresh = _append_event(
                **common, event_type="reserve", state="reserved", physical_delta=0
            )
            if not refresh["idempotent"]:
                summary["reservation_refreshed"] += 1
        elif state in {"fulfilled", "fulfilled_reconciliation"} and _is_cancellation(
            supplier_status, wb_status
        ):
            event = _append_event(
                **common,
                event_type="post_handoff_reconciliation",
                state="fulfilled_reconciliation",
                physical_delta=0,
            )
            if not event["idempotent"]:
                _persist_reconciliation(
                    conn, manifest=manifest, order=order, event=event, created_at=now
                )
                summary["reconciliation"] += 1
        elif state in {"fulfilled", "fulfilled_reconciliation"}:
            event_type = (
                "terminal_noop" if wb_status in LATER_TERMINAL_WB_STATUSES else "status_noop"
            )
            event = _append_event(
                **common, event_type=event_type, state=state, physical_delta=0
            )
            if not event["idempotent"]:
                summary[event_type] += 1
        elif state in {"released", "cancelled_noop"}:
            event = _append_event(
                **common,
                event_type="cancel_noop",
                state=state,
                physical_delta=0,
            )
            if not event["idempotent"]:
                summary["cancel_noop"] += 1
        if is_pending_retry:
            summary["identity_resolved"] += _resolve_identity_pending(
                conn,
                cutover_id=cutover_id,
                source_status_observation_sequence=status_sequence,
                matched_identity_evidence_sequence=int(
                    mapped["identity_evidence_sequence"]
                ),
                matched_identity_order_revision=str(
                    mapped["identity_evidence_order_revision"]
                ),
                resolved_at=now,
            )
        last_sequence = max(last_sequence, status_sequence)

    if pinned_sequences is not None:
        pending = 0
        identity_pending = int(
            conn.execute(
                f"""SELECT COUNT(*)
                    FROM {IDENTITY_PENDING_TABLE} AS pending
                    JOIN temp.ff_pool_fbs_pinned_status_targets AS target
                      ON target.observation_sequence=
                         pending.source_status_observation_sequence
                    LEFT JOIN {IDENTITY_PENDING_RESOLUTIONS_TABLE} AS resolution
                      ON resolution.pending_id=pending.pending_id
                    WHERE pending.cutover_id=? AND resolution.pending_id IS NULL""",
                (cutover_id,),
            ).fetchone()[0]
            if pinned_sequences
            else 0
        )
        processed_count = len(pinned_rows)
    else:
        pending = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {STATUS_OBSERVATIONS_TABLE} WHERE observation_sequence>?",
                (last_sequence,),
            ).fetchone()[0]
        )
        pending_floor_sql = (
            " AND pending.source_status_observation_sequence>?"
            if lane == "forward"
            else ""
        )
        pending_parameters = (
            (cutover_id, cutoff_sequence)
            if lane == "forward"
            else (cutover_id,)
        )
        identity_pending = int(
            conn.execute(
                f"""SELECT COUNT(*)
                    FROM {IDENTITY_PENDING_TABLE} AS pending
                    LEFT JOIN {IDENTITY_PENDING_RESOLUTIONS_TABLE} AS resolution
                      ON resolution.pending_id=pending.pending_id
                    WHERE pending.cutover_id=? AND resolution.pending_id IS NULL
                          {pending_floor_sql}""",
                pending_parameters,
            ).fetchone()[0]
        )
        processed_count = len(new_rows)
    result_material = {
        "cutover_id": cutover_id,
        "lane": lane,
        "forward_generation_id": generation_id,
        "cutoff_status_observation_sequence": cutoff_sequence,
        "from_status_observation_sequence": starting_sequence,
        "last_status_observation_sequence": last_sequence,
        "processed_count": processed_count,
        "pending_count": pending,
        "identity_retry_count": (
            sum(1 for row in pinned_rows if row[18] is not None)
            if pinned_sequences is not None
            else len(pending_rows)
        ),
        "identity_pending_count": identity_pending,
        "summary": summary,
    }
    result_digest = _fingerprint(result_material)
    if pinned_sequences is not None:
        conn.execute("DELETE FROM temp.ff_pool_fbs_pinned_status_targets")
    if pinned_sequences is None and lane == "forward":
        conn.execute(
            f"""UPDATE {FORWARD_STATE_TABLE}
                SET last_status_observation_sequence=?,run_count=run_count+1,
                    last_result_digest=?,updated_at=?
                WHERE generation_id=?""",
            (last_sequence, result_digest, now, generation_id),
        )
    elif pinned_sequences is None:
        conn.execute(
            f"""UPDATE {DRAIN_STATE_TABLE}
                SET last_status_observation_sequence=?,drain_run_count=drain_run_count+1,
                    last_result_digest=?,updated_at=?
                WHERE cutover_id=?""",
            (last_sequence, result_digest, now, cutover_id),
        )
    if summary["fulfilled"]:
        _record_current_parity(conn, manifest=manifest, checked_at=now)
    return {
        "contract_name": CONTRACT_NAME,
        "status": (
            "recovered"
            if pinned_sequences is not None and identity_pending == 0
            else "recovered_identity_pending"
            if pinned_sequences is not None
            else "caught_up"
            if pending == 0 and identity_pending == 0
            else "caught_up_identity_pending"
            if pending == 0
            else "processed_partial"
        ),
        **result_material,
        "result_digest": result_digest,
        "mutates_wb": False,
    }


def recover_pinned_fbs_lifecycle(
    conn: sqlite3.Connection,
    *,
    manifest: Mapping[str, Any],
    status_observation_sequences: tuple[int, ...],
    occurred_at: str,
) -> dict[str, Any]:
    """Apply one immutable ``<= C`` target without touching either live cursor."""

    sequences = tuple(status_observation_sequences)
    return drain_post_checkpoint_fbs_lifecycle(
        conn,
        manifest=manifest,
        occurred_at=occurred_at,
        limit=max(1, len(sequences)),
        pinned_status_observation_sequences=sequences,
    )


def available_quantity(
    conn: sqlite3.Connection,
    *,
    cutover_id: str,
    facility_id: str,
    nm_id: int,
) -> dict[str, Any]:
    manifest = conn.execute(
        """SELECT business_date,feature_epoch
           FROM sheet_vitrina_v1_ff_pool_cutover_manifests
           WHERE cutover_id=?""",
        (str(cutover_id),),
    ).fetchone()
    if manifest is None:
        raise FfPoolFbsLifecycleError(
            "cutover_manifest_missing", "FBS availability cutover manifest is missing"
        )
    facility = conn.execute(
        f"SELECT active FROM {FACILITIES_TABLE} WHERE facility_id=?",
        (str(facility_id),),
    ).fetchone()
    component = fbs_physical_component(
        conn,
        facility_id=str(facility_id),
        nm_id=int(nm_id),
        as_of_date=str(manifest[0]),
        projection_epoch=int(manifest[1]),
        facility_active=bool(facility[0]) if facility is not None else False,
        sku_active=True,
    )
    if component["state"] in {"missing", "inapplicable"}:
        raise FfPoolFbsLifecycleError(
            "fbs_balance_missing"
            if component["state"] == "missing"
            else "fbs_pair_inapplicable",
            f"FBS availability is {component['state']} for {facility_id}/{nm_id}",
        )
    physical = int(component["quantity"])
    reserved = int(
        conn.execute(
            f"""SELECT COALESCE(SUM(quantity),0) FROM {CURRENT_TABLE}
                WHERE cutover_id=? AND facility_id=? AND pool='FBS' AND nm_id=?
                  AND state='reserved'""",
            (cutover_id, facility_id, int(nm_id)),
        ).fetchone()[0]
    )
    return {
        "contract_name": str(component["contract_name"]),
        "state": str(component["state"]),
        "physical": physical,
        "reserved": reserved,
        "available": physical - reserved,
        "reason": str(component["reason"]),
        "provenance": dict(component["provenance"]),
    }


def _append_event(
    conn: sqlite3.Connection,
    *,
    manifest: Mapping[str, Any],
    order: Mapping[str, Any],
    episode_sequence: int,
    event_type: str,
    state: str,
    supplier_status: str,
    wb_status: str,
    status_digest: str,
    source_revision: str,
    source_observed_at: str,
    wac: Decimal,
    physical_delta: int,
    occurred_at: str,
    evidence: Mapping[str, Any],
    details_evidence: Mapping[str, Any] | None = None,
    source_order_observation_sequence: int = 0,
    source_status_observation_sequence: int = 0,
) -> dict[str, Any]:
    cutover_id = str(manifest["cutover_id"])
    quantity = int(order["quantity"])
    capital_delta = _capital_delta(wac, physical_delta)
    evidence_digest = _fingerprint(evidence)
    identity = {
        "cutover_id": cutover_id,
        "order_id": int(order["order_id"]),
        "episode_sequence": int(episode_sequence),
        "event_type": event_type,
        "source_order_observation_sequence": int(
            source_order_observation_sequence
        ),
        "source_status_observation_sequence": int(
            source_status_observation_sequence
        ),
        "evidence_digest": evidence_digest,
    }
    event_id = "ffbf_" + _fingerprint(identity).removeprefix("sha256:")[:28]
    inserted = conn.execute(
        f"""INSERT OR IGNORE INTO {EVENTS_TABLE}(
                event_id,cutover_id,order_id,episode_sequence,event_type,
                source_order_observation_sequence,
                source_status_observation_sequence,
                source_revision,status_digest,supplier_status,wb_status,
                source_observed_at,facility_id,pool,nm_id,quantity,
                physical_quantity_delta,capital_delta_rub,frozen_wac_rub,
                evidence_digest,occurred_at,details_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            cutover_id,
            int(order["order_id"]),
            int(episode_sequence),
            event_type,
            int(source_order_observation_sequence),
            int(source_status_observation_sequence),
            source_revision,
            status_digest,
            supplier_status,
            wb_status,
            source_observed_at,
            str(order["facility_id"]),
            "FBS",
            int(order["nm_id"]),
            quantity,
            int(physical_delta),
            canonical_decimal_text(capital_delta),
            canonical_decimal_text(wac),
            evidence_digest,
            occurred_at,
            _json(details_evidence if details_evidence is not None else evidence),
        ),
    ).rowcount
    if inserted:
        debit_event_id = event_id if physical_delta < 0 else ""
        conn.execute(
            f"""INSERT INTO {CURRENT_TABLE}(
                    cutover_id,order_id,state,episode_sequence,source_revision,
                    status_digest,supplier_status,wb_status,facility_id,pool,nm_id,
                    quantity,frozen_wac_rub,debit_event_id,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(cutover_id,order_id) DO UPDATE SET
                    state=excluded.state,
                    episode_sequence=excluded.episode_sequence,
                    source_revision=excluded.source_revision,
                    status_digest=excluded.status_digest,
                    supplier_status=excluded.supplier_status,
                    wb_status=excluded.wb_status,
                    facility_id=excluded.facility_id,
                    pool=excluded.pool,
                    nm_id=excluded.nm_id,
                    quantity=excluded.quantity,
                    frozen_wac_rub=excluded.frozen_wac_rub,
                    debit_event_id=CASE WHEN excluded.debit_event_id<>''
                        THEN excluded.debit_event_id ELSE {CURRENT_TABLE}.debit_event_id END,
                    updated_at=excluded.updated_at""",
            (
                cutover_id,
                int(order["order_id"]),
                state,
                int(episode_sequence),
                source_revision,
                status_digest,
                supplier_status,
                wb_status,
                str(order["facility_id"]),
                "FBS",
                int(order["nm_id"]),
                quantity,
                canonical_decimal_text(wac),
                debit_event_id,
                occurred_at,
            ),
        )
    return {"event_id": event_id, "idempotent": not bool(inserted)}


def _apply_exact_physical_delta(
    conn: sqlite3.Connection,
    *,
    manifest: Mapping[str, Any],
    order: Mapping[str, Any],
    event_id: str,
    quantity_delta: int,
    wac: Decimal,
    occurred_at: str,
) -> None:
    facility_id = str(order["facility_id"])
    nm_id = int(order["nm_id"])
    epoch = int(manifest["feature_epoch"])
    balance = conn.execute(
        f"""SELECT quantity,capital_rub FROM {BALANCES_TABLE}
            WHERE facility_id=? AND pool='FBS' AND nm_id=? AND projection_epoch=?""",
        (facility_id, nm_id, epoch),
    ).fetchone()
    if balance is None:
        raise FfPoolFbsLifecycleError(
            "fbs_balance_missing",
            f"FBS balance is missing for {facility_id}/{nm_id}",
        )
    capital_delta = _capital_delta(wac, quantity_delta)
    new_quantity = int(balance[0]) + int(quantity_delta)
    new_capital = _decimal_sum(Decimal(str(balance[1])), capital_delta)
    operation_id = "ffbo_" + hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:28]
    conn.execute(
        f"""INSERT INTO {OPERATIONS_TABLE}(
                operation_id,operation_type,source_system,source_type,source_id,
                source_revision,idempotency_epoch,business_date,posted_at,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            operation_id,
            "fbs_physical_handoff",
            "official_wb_status_shadow",
            "fbs_order_lifecycle_event",
            str(order["order_id"]),
            event_id,
            epoch,
            str(manifest["business_date"]),
            occurred_at,
            _json({"event_id": event_id, "mutates_wb": False}),
        ),
    )
    conn.execute(
        f"""INSERT INTO {LINES_TABLE}(
                operation_id,line_no,facility_id,pool,nm_id,quantity_delta,
                capital_delta_rub,wac_snapshot_rub,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            operation_id,
            1,
            facility_id,
            "FBS",
            nm_id,
            int(quantity_delta),
            canonical_decimal_text(capital_delta),
            canonical_decimal_text(wac),
            _json({"order_id": int(order["order_id"]), "event_id": event_id}),
        ),
    )
    conn.execute(
        f"""UPDATE {BALANCES_TABLE}
            SET quantity=?,capital_rub=?,wac_rub=?,source_watermark=?,updated_at=?
            WHERE facility_id=? AND pool='FBS' AND nm_id=? AND projection_epoch=?""",
        (
            new_quantity,
            canonical_decimal_text(new_capital),
            canonical_decimal_text(wac),
            event_id,
            occurred_at,
            facility_id,
            nm_id,
            epoch,
        ),
    )
    _apply_exact_aggregate_projection(
        conn,
        nm_id=nm_id,
        quantity_delta=quantity_delta,
        capital_delta=capital_delta,
    )


def _apply_exact_aggregate_projection(
    conn: sqlite3.Connection,
    *,
    nm_id: int,
    quantity_delta: int,
    capital_delta: Decimal,
) -> None:
    active = conn.execute(
        "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
    ).fetchone()
    if active is None:
        raise FfPoolFbsLifecycleError("aggregate_active_missing", "Aggregate FF version is missing")
    row = conn.execute(
        """SELECT quantity,capital_rub FROM sheet_vitrina_v1_warehouse_functional_balances
           WHERE version_id=? AND warehouse_key='ff' AND nm_id=?""",
        (str(active[0]), int(nm_id)),
    ).fetchone()
    if row is None:
        raise FfPoolFbsLifecycleError(
            "aggregate_sku_missing", f"Aggregate FF SKU is missing: {nm_id}"
        )
    quantity = _exact_int(row[0], "aggregate.quantity") + int(quantity_delta)
    capital = _decimal_sum(Decimal(str(row[1])), capital_delta)
    columns = {
        str(item[1])
        for item in conn.execute(
            "PRAGMA table_info(sheet_vitrina_v1_warehouse_functional_balances)"
        ).fetchall()
    }
    if "wac_rub" in columns:
        wac = (
            None
            if quantity == 0
            else canonical_decimal_text(_decimal_ratio(capital, quantity))
        )
        conn.execute(
            """UPDATE sheet_vitrina_v1_warehouse_functional_balances
               SET quantity=?,capital_rub=?,wac_rub=?
               WHERE version_id=? AND warehouse_key='ff' AND nm_id=?""",
            (canonical_decimal_text(Decimal(quantity)), canonical_decimal_text(capital), wac, str(active[0]), int(nm_id)),
        )
    else:
        conn.execute(
            """UPDATE sheet_vitrina_v1_warehouse_functional_balances
               SET quantity=?,capital_rub=?
               WHERE version_id=? AND warehouse_key='ff' AND nm_id=?""",
            (canonical_decimal_text(Decimal(quantity)), canonical_decimal_text(capital), str(active[0]), int(nm_id)),
        )


def _record_current_parity(
    conn: sqlite3.Connection, *, manifest: Mapping[str, Any], checked_at: str
) -> None:
    active = conn.execute(
        "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
    ).fetchone()
    if active is None:
        raise FfPoolFbsLifecycleError("aggregate_active_missing", "Aggregate FF version is missing")
    aggregate_rows = [
        {
            "nm_id": int(row[0]),
            "quantity": _exact_int(row[1], "aggregate.quantity"),
            "capital_rub": canonical_decimal_text(row[2]),
        }
        for row in conn.execute(
            """SELECT nm_id,quantity,capital_rub
               FROM sheet_vitrina_v1_warehouse_functional_balances
               WHERE version_id=? AND warehouse_key='ff' ORDER BY nm_id""",
            (str(active[0]),),
        ).fetchall()
    ]
    parity = evaluate_ff_pool_aggregate_parity(conn, aggregate_rows)
    if parity.status != "pass":
        raise FfPoolFbsLifecycleError(
            "post_t_parity_failed",
            "Post-T FBS debit diverged from aggregate FF",
            details={
                "money_parity_policy": parity.money_parity_policy,
                "quantity_mismatched_nm_ids": list(
                    parity.quantity_mismatched_nm_ids
                ),
                "canonical_capital_mismatched_nm_ids": list(
                    parity.canonical_capital_mismatched_nm_ids
                ),
                "raw_capital_mismatched_nm_ids": list(
                    parity.raw_capital_mismatched_nm_ids
                ),
                "raw_capital_residuals_by_nm": {
                    str(nm_id): canonical_decimal_text(residual)
                    for nm_id, residual in parity.raw_capital_residuals_by_nm
                },
                "mismatched_nm_ids": list(parity.mismatched_nm_ids),
            },
        )
    sequence = int(conn.execute(f"SELECT COALESCE(MAX(event_sequence),0) FROM {EVENTS_TABLE}").fetchone()[0])
    record_ff_pool_parity_diagnostic(
        conn,
        diagnostic_id="ffpar_fbs_" + _fingerprint(
            {"cutover_id": manifest["cutover_id"], "event_sequence": sequence}
        ).removeprefix("sha256:")[:24],
        aggregate_revision=str(manifest["aggregate_revision"]),
        checked_at=checked_at,
        result=parity,
        details={"source": "post_t_fbs_lifecycle", "event_sequence": sequence},
    )


def _map_order(
    conn: sqlite3.Connection,
    manifest: Mapping[str, Any],
    row: sqlite3.Row,
    *,
    allow_compatible_identity: bool = False,
    minimum_identity_evidence_sequence: int = 0,
) -> dict[str, Any]:
    warehouse_id = int(row[5] or 0)
    # Mapping values in the cutover manifest are necessary but not sufficient:
    # a live suffix row requires its own immutable matched identity proof.  A
    # previously isolated row may use a later proof only for the same order and
    # exact warehouse/nm/chrt tuple; the original status is still replayed.
    if allow_compatible_identity:
        identity_rows = conn.execute(
            f"""SELECT evidence_sequence,evidence_id,order_revision,outcome,
                       warehouse_id,nm_id,chrt_id,warehouse_mapping_id,
                       identity_mapping_id
                FROM {IDENTITY_EVIDENCE_TABLE}
                WHERE order_id=? AND evidence_sequence>?
                ORDER BY (order_revision=?) DESC,evidence_sequence DESC""",
            (
                int(row[1]),
                int(minimum_identity_evidence_sequence),
                str(row[2]),
            ),
        ).fetchall()
    else:
        identity_rows = conn.execute(
            f"""SELECT evidence_sequence,evidence_id,order_revision,outcome,
                       warehouse_id,nm_id,chrt_id,warehouse_mapping_id,
                       identity_mapping_id
                FROM {IDENTITY_EVIDENCE_TABLE}
                WHERE order_id=? AND order_revision=?
                ORDER BY evidence_sequence DESC""",
            (int(row[1]), str(row[2])),
        ).fetchall()
    identity = next(
        (
            item
            for item in identity_rows
            if str(item[3]) == "matched"
            and int(item[4] or 0) == warehouse_id
            and int(item[5] or 0) == int(row[6])
            and int(item[6] or 0) == int(row[7] or 0)
            and bool(str(item[7] or ""))
            and bool(str(item[8] or ""))
        ),
        None,
    )
    if identity is None:
        raise FfPoolFbsLifecycleError(
            "order_identity_evidence_missing_or_drifted",
            f"Order {int(row[1])} lacks exact matched identity evidence",
        )
    facility_by_warehouse = {
        int(item["warehouse_id"]): str(item["facility_id"])
        for item in manifest.get("seller_warehouse_mappings") or []
    }
    facility_id = facility_by_warehouse.get(warehouse_id)
    source_nm_id = int(row[6])
    chrt_id = int(row[7] or 0)
    extension_id = ""
    if facility_id:
        mapping = next(
            (
                item
                for item in manifest.get("sku_mappings") or []
                if int(item["nm_id"]) == source_nm_id
                and int(item["chrt_id"]) == chrt_id
            ),
            None,
        )
    else:
        office_id = int(row[9] or 0)
        extension_rows = conn.execute(
            f"""SELECT extension.extension_id,extension.facility_id,
                       extension.warehouse_mapping_id
                FROM {MAPPING_EXTENSIONS_TABLE} AS extension
                JOIN {WAREHOUSE_MAPPINGS_TABLE} AS warehouse
                  ON warehouse.mapping_id=extension.warehouse_mapping_id
                JOIN {FACILITIES_TABLE} AS facility
                  ON facility.facility_id=extension.facility_id
                WHERE extension.cutover_id=?
                  AND extension.seller_warehouse_id=?
                  AND extension.official_office_id=?
                  AND warehouse.seller_warehouse_id=extension.seller_warehouse_id
                  AND warehouse.facility_id=extension.facility_id
                  AND warehouse.mapping_digest=extension.mapping_digest
                  AND warehouse.official_office_id=extension.official_office_id
                  AND warehouse.official_evidence_digest=
                      extension.official_evidence_digest
                  AND warehouse.active=1
                  AND facility.active=1
                ORDER BY extension.extension_id""",
            (str(manifest["cutover_id"]), warehouse_id, office_id),
        ).fetchall()
        if len(extension_rows) != 1:
            raise FfPoolFbsLifecycleError(
                "order_identity_evidence_missing_or_drifted",
                f"Order {int(row[1])} warehouse/office is unmapped or ambiguous",
            )
        extension = extension_rows[0]
        if str(identity[7]) != str(extension[2]):
            raise FfPoolFbsLifecycleError(
                "order_identity_evidence_missing_or_drifted",
                f"Order {int(row[1])} warehouse evidence predates the active extension",
            )
        identity_mapping = conn.execute(
            f"""SELECT target_nm_id FROM {IDENTITY_MAPPINGS_TABLE}
                WHERE mapping_id=? AND source_nm_id=? AND source_chrt_id=?
                  AND active=1""",
            (str(identity[8]), source_nm_id, chrt_id),
        ).fetchall()
        if len(identity_mapping) != 1:
            raise FfPoolFbsLifecycleError(
                "order_identity_evidence_missing_or_drifted",
                f"Order {int(row[1])} exact SKU identity is unmapped or ambiguous",
            )
        facility_id = str(extension[1])
        extension_id = str(extension[0])
        target_nm_id = int(identity_mapping[0][0])
        allocation_count = int(
            conn.execute(
                f"""SELECT COUNT(*)
                    FROM {MAPPING_EXTENSION_ALLOCATIONS_TABLE}
                    WHERE extension_id=? AND nm_id=?""",
                (extension_id, target_nm_id),
            ).fetchone()[0]
        )
        if allocation_count != 1:
            raise FfPoolFbsLifecycleError(
                "order_identity_evidence_missing_or_drifted",
                f"Order {int(row[1])} has no exact extension allocation/WAC",
            )
        mapping = {
            "target_nm_id": target_nm_id,
            "nm_id": source_nm_id,
            "chrt_id": chrt_id,
        }
    if mapping is None:
        raise FfPoolFbsLifecycleError(
            "order_sku_unmapped",
            f"Order {int(row[1])} exact known-facility SKU is unmapped",
        )
    return {
        "facility_id": facility_id,
        "nm_id": int(mapping.get("target_nm_id", mapping.get("nm_id", source_nm_id))),
        "mapping_extension_id": extension_id,
        "identity_evidence_sequence": int(identity[0]),
        "identity_evidence_order_revision": str(identity[2]),
    }


def _order_payload(
    row: sqlite3.Row, mapped: Mapping[str, Any], *, quantity: int
) -> dict[str, Any]:
    return {
        "order_id": int(row[1]),
        "observation_id": str(row[0]),
        "source_revision": str(row[2]),
        "source_created_at": str(row[3]),
        "observed_at": str(row[4]),
        "facility_id": str(mapped["facility_id"]),
        "pool": "FBS",
        "nm_id": int(mapped["nm_id"]),
        "quantity": int(quantity),
        "mapping_extension_id": str(mapped.get("mapping_extension_id") or ""),
    }


def _frozen_wac(
    conn: sqlite3.Connection,
    manifest: Mapping[str, Any],
    order: Mapping[str, Any],
) -> Decimal:
    match = next(
        (
            item
            for item in manifest.get("allocations") or []
            if str(item["facility_id"]) == str(order["facility_id"])
            and str(item["pool"]) == "FBS"
            and int(item["nm_id"]) == int(order["nm_id"])
        ),
        None,
    )
    if match is not None and match.get("wac_rub") is not None:
        return Decimal(str(match["wac_rub"]))
    extension_id = str(order.get("mapping_extension_id") or "")
    extension_rows = (
        conn.execute(
            f"""SELECT frozen_wac_rub
                FROM {MAPPING_EXTENSION_ALLOCATIONS_TABLE}
                WHERE extension_id=? AND nm_id=?""",
            (extension_id, int(order["nm_id"])),
        ).fetchall()
        if extension_id
        else []
    )
    if len(extension_rows) != 1:
        raise FfPoolFbsLifecycleError(
            "frozen_wac_missing",
            f"Opening WAC is missing for {order['facility_id']}/{order['nm_id']}",
        )
    return Decimal(str(extension_rows[0][0]))


def _current_handoff_wac(
    conn: sqlite3.Connection,
    *,
    manifest: Mapping[str, Any],
    order: Mapping[str, Any],
) -> tuple[Decimal, dict[str, Any]]:
    """Resolve exact facility FBS WAC at the serialized debit boundary.

    The lifecycle drain and every overhead posting share the same SQLite
    writer order.  Reading the balance from this writer transaction means a
    committed overhead operation is visible before the handoff, while a later
    operation cannot retroactively change this immutable event.  ``rowid`` is
    retained as the durable tie-breaker when wall-clock timestamps are equal.
    """

    facility_id = str(order["facility_id"])
    nm_id = int(order["nm_id"])
    epoch = int(manifest["feature_epoch"])
    row = conn.execute(
        f"""SELECT quantity,capital_rub,wac_rub,source_watermark,updated_at
              FROM {BALANCES_TABLE}
             WHERE facility_id=? AND pool='FBS' AND nm_id=?
               AND projection_epoch=?""",
        (facility_id, nm_id, epoch),
    ).fetchone()
    if row is None:
        raise FfPoolFbsLifecycleError(
            "handoff_current_wac_missing",
            f"Current FBS balance is missing for {facility_id}/{nm_id}",
        )
    quantity = int(row[0])
    requested = int(order["quantity"])
    capital = Decimal(str(row[1]))
    stored_wac = Decimal(str(row[2]))
    if quantity <= 0 or requested <= 0 or quantity < requested or capital <= ZERO:
        raise FfPoolFbsLifecycleError(
            "handoff_current_wac_unavailable",
            f"Positive current FBS cost is unavailable for {facility_id}/{nm_id}",
            details={
                "facility_id": facility_id,
                "pool": "FBS",
                "nm_id": nm_id,
                "current_quantity": quantity,
                "requested_quantity": requested,
            },
        )
    current_wac = _decimal_ratio(capital, quantity)
    if current_wac <= ZERO:
        raise FfPoolFbsLifecycleError(
            "handoff_current_wac_non_positive",
            f"Current FBS WAC is non-positive for {facility_id}/{nm_id}",
        )
    source_watermark = str(row[3] or "")
    operation = conn.execute(
        f"""SELECT rowid,operation_id,operation_type,source_revision,posted_at
              FROM {OPERATIONS_TABLE}
             WHERE operation_id=? OR source_revision=?
             ORDER BY rowid DESC LIMIT 1""",
        (source_watermark, source_watermark),
    ).fetchone()
    return current_wac, {
        "contract": "fbs_handoff_current_facility_wac_v1",
        "facility_id": facility_id,
        "pool": "FBS",
        "nm_id": nm_id,
        "projection_epoch": epoch,
        "quantity_before_handoff": quantity,
        "capital_before_handoff_rub": canonical_decimal_text(capital),
        "wac_rub": canonical_decimal_text(current_wac),
        "stored_wac_rub": canonical_decimal_text(stored_wac),
        "stored_wac_matches_capital_ratio": stored_wac == current_wac,
        "balance_source_watermark": source_watermark,
        "balance_updated_at": str(row[4] or ""),
        "source_operation_rowid": int(operation[0]) if operation else 0,
        "source_operation_id": str(operation[1]) if operation else "",
        "source_operation_type": str(operation[2]) if operation else "",
        "source_operation_revision": str(operation[3]) if operation else "",
        "source_operation_posted_at": str(operation[4]) if operation else "",
    }


def _persist_late_checkpoint_evidence(
    conn: sqlite3.Connection,
    *,
    manifest: Mapping[str, Any],
    row: sqlite3.Row | tuple[Any, ...],
    detected_at: str,
) -> bool:
    evidence = {
        "cutover_id": str(manifest["cutover_id"]),
        "order_id": int(row[1]),
        "source_order_observation_sequence": int(row[8]),
        "source_status_observation_sequence": int(row[0]),
        "source_revision": str(row[2]),
        "status_digest": str(row[3]),
        "supplier_status": str(row[4]),
        "wb_status": str(row[5]),
        "observed_at": str(row[7]),
        "reason_code": "late_pre_t",
    }
    digest = _fingerprint(evidence)
    evidence_id = "fflatev_" + digest.removeprefix("sha256:")[:28]
    inserted = conn.execute(
        f"""INSERT OR IGNORE INTO {LATE_EVIDENCE_TABLE}(
                evidence_id,cutover_id,order_id,
                source_order_observation_sequence,
                source_status_observation_sequence,source_revision,status_digest,
                supplier_status,wb_status,observed_at,reason_code,evidence_digest,
                created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,'late_pre_t',?,?)""",
        (
            evidence_id,
            str(manifest["cutover_id"]),
            int(row[1]),
            int(row[8]),
            int(row[0]),
            str(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]),
            str(row[7]),
            digest,
            detected_at,
        ),
    ).rowcount
    conn.execute(
        """INSERT OR IGNORE INTO sheet_vitrina_v1_ff_pool_cutover_late_pre_t_cases(
                case_id,cutover_id,order_id,observation_id,source_revision,
                source_created_at,observed_at,detected_at,state,reason_code,
                display_reason,evidence_digest
            ) VALUES(?,?,?,?,?,?,?,?,'isolated','late_pre_t',
                     'Поздний заказ до границы',?)""",
        (
            "fflate_" + digest.removeprefix("sha256:")[:28],
            str(manifest["cutover_id"]),
            int(row[1]),
            str(row[9]),
            str(row[2]),
            str(row[11]),
            str(row[7]),
            detected_at,
            digest,
        ),
    )
    return bool(inserted)


def _persist_identity_pending(
    conn: sqlite3.Connection,
    *,
    manifest: Mapping[str, Any],
    row: sqlite3.Row | tuple[Any, ...],
    created_at: str,
    reason_detail_code: str,
) -> bool:
    detail_code = str(reason_detail_code)
    if detail_code not in {
        "identity_evidence_missing_or_drifted",
        "order_sku_unmapped",
    }:
        raise FfPoolFbsLifecycleError(
            "identity_pending_reason_invalid",
            "Identity pending reason is outside the durable quarantine contract",
        )
    evidence = {
        "cutover_id": str(manifest["cutover_id"]),
        "order_id": int(row[1]),
        "source_status_observation_sequence": int(row[0]),
        "order_revision": str(row[2]),
        "status_digest": str(row[3]),
        "reason_code": "identity_evidence_missing_or_drifted",
        "reason_detail_code": detail_code,
    }
    digest = _fingerprint(evidence)
    pending_id = "ffidp_" + digest.removeprefix("sha256:")[:28]
    deferred_identity_evidence_sequence = int(
        conn.execute(
            f"SELECT COALESCE(MAX(evidence_sequence),0) "
            f"FROM {IDENTITY_EVIDENCE_TABLE} WHERE order_id=?",
            (int(row[1]),),
        ).fetchone()[0]
    )
    return bool(
        conn.execute(
            f"""INSERT OR IGNORE INTO {IDENTITY_PENDING_TABLE}(
                    pending_id,cutover_id,order_id,
                    source_status_observation_sequence,order_revision,
                    status_digest,deferred_identity_evidence_sequence,
                    reason_code,reason_detail_code,evidence_digest,created_at
                ) VALUES(?,?,?,?,?,?,?,'identity_evidence_missing_or_drifted',?,?,?)""",
            (
                pending_id,
                str(manifest["cutover_id"]),
                int(row[1]),
                int(row[0]),
                str(row[2]),
                str(row[3]),
                deferred_identity_evidence_sequence,
                detail_code,
                digest,
                created_at,
            ),
        ).rowcount
    )


def _resolve_identity_pending(
    conn: sqlite3.Connection,
    *,
    cutover_id: str,
    source_status_observation_sequence: int,
    matched_identity_evidence_sequence: int,
    matched_identity_order_revision: str,
    resolved_at: str,
) -> int:
    pending = conn.execute(
        f"""SELECT pending_id,order_id,evidence_digest
            FROM {IDENTITY_PENDING_TABLE}
            WHERE cutover_id=? AND source_status_observation_sequence=?""",
        (str(cutover_id), int(source_status_observation_sequence)),
    ).fetchone()
    if pending is None:
        raise FfPoolFbsLifecycleError(
            "identity_pending_missing",
            "An identity-pending replay has no immutable pending evidence",
        )
    material = {
        "pending_id": str(pending[0]),
        "pending_evidence_digest": str(pending[2]),
        "matched_identity_evidence_sequence": int(
            matched_identity_evidence_sequence
        ),
        "matched_identity_order_revision": str(matched_identity_order_revision),
        "resolution_kind": "matched_replay",
    }
    digest = _fingerprint(material)
    resolution_id = "ffidr_" + digest.removeprefix("sha256:")[:28]
    return int(
        conn.execute(
            f"""INSERT OR IGNORE INTO {IDENTITY_PENDING_RESOLUTIONS_TABLE}(
                    resolution_id,pending_id,cutover_id,order_id,
                    source_status_observation_sequence,
                    matched_identity_evidence_sequence,
                    matched_identity_order_revision,resolution_kind,
                    resolution_digest,resolved_at
                ) VALUES(?,?,?,?,?,?,?,'matched_replay',?,?)""",
            (
                resolution_id,
                str(pending[0]),
                str(cutover_id),
                int(pending[1]),
                int(source_status_observation_sequence),
                int(matched_identity_evidence_sequence),
                str(matched_identity_order_revision),
                digest,
                resolved_at,
            ),
        ).rowcount
    )


def _persist_late_pre_t(
    conn: sqlite3.Connection,
    *,
    manifest: Mapping[str, Any],
    order: Mapping[str, Any],
    event: Mapping[str, Any],
    detected_at: str,
) -> None:
    evidence = {
        "cutover_id": str(manifest["cutover_id"]),
        "order_id": int(order["order_id"]),
        "source_revision": str(order["source_revision"]),
        "event_id": str(event["event_id"]),
    }
    conn.execute(
        """INSERT OR IGNORE INTO sheet_vitrina_v1_ff_pool_cutover_late_pre_t_cases(
               case_id,cutover_id,order_id,observation_id,source_revision,
               source_created_at,observed_at,detected_at,state,reason_code,
               display_reason,evidence_digest
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "late_" + _fingerprint(evidence).removeprefix("sha256:")[:28],
            str(manifest["cutover_id"]),
            int(order["order_id"]),
            str(order["observation_id"]),
            str(order["source_revision"]),
            str(order["source_created_at"]),
            str(order["observed_at"]),
            detected_at,
            "isolated",
            "late_pre_t",
            "Поздний заказ до границы",
            _fingerprint(evidence),
        ),
    )


def _persist_reconciliation(
    conn: sqlite3.Connection,
    *,
    manifest: Mapping[str, Any],
    order: Mapping[str, Any],
    event: Mapping[str, Any],
    created_at: str,
) -> None:
    evidence = {
        "event_id": str(event["event_id"]),
        "reason": "post_handoff_cancellation_or_return",
    }
    conn.execute(
        f"""INSERT OR IGNORE INTO {RECONCILIATION_TABLE}(
               reconciliation_id,cutover_id,order_id,event_id,reason_code,
               evidence_digest,state,created_at
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            "ffbr_" + _fingerprint(evidence).removeprefix("sha256:")[:28],
            str(manifest["cutover_id"]),
            int(order["order_id"]),
            str(event["event_id"]),
            "post_handoff_cancellation_or_return",
            _fingerprint(evidence),
            "open",
            created_at,
        ),
    )


def _require_approved_policy(manifest: Mapping[str, Any]) -> None:
    policy = dict(manifest.get("handoff_policy") or {})
    if not (
        policy.get("approved") is True
        and str(policy.get("supplier_status") or "") == HANDOFF_SUPPLIER_STATUS
        and str(policy.get("wb_status") or "") == HANDOFF_WB_STATUS
        and str(policy.get("approval_reference") or "")
        and policy.get("supplier_status_complete_alone_forbidden") is True
    ):
        raise FfPoolFbsLifecycleError(
            "handoff_policy_not_approved",
            "FBS lifecycle is hard-off until the exact complete/sorted owner gate is applied",
        )


def _is_handoff(supplier_status: str, wb_status: str) -> bool:
    return supplier_status == HANDOFF_SUPPLIER_STATUS and wb_status == HANDOFF_WB_STATUS


def _is_cancellation(supplier_status: str, wb_status: str) -> bool:
    return (
        supplier_status in CANCELLATION_SUPPLIER_STATUSES
        or wb_status in CANCELLATION_WB_STATUSES
    )


def _exact_int(value: Any, field: str) -> int:
    decimal = Decimal(str(value))
    if not decimal.is_finite() or decimal != decimal.to_integral_value():
        raise FfPoolFbsLifecycleError("non_integral_quantity", f"{field} is not exact INTEGER")
    return int(decimal)


def _capital_delta(wac: Decimal, quantity: int) -> Decimal:
    # Capital text can carry authoritative fractional-kopeck tails close to
    # the schema's 80-character boundary.  The process-default precision (28)
    # is not an accounting boundary and must not round an event delta.
    with localcontext() as context:
        context.prec = 160
        return Decimal(wac) * Decimal(int(quantity))


def _decimal_sum(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 160
        return Decimal(left) + Decimal(right)


def _decimal_ratio(capital: Decimal, quantity: int) -> Decimal:
    with localcontext() as context:
        context.prec = 160
        return Decimal(capital) / Decimal(int(quantity))


def _require_utc(value: str) -> None:
    parsed = _parse_utc(value)
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise FfPoolFbsLifecycleError("timestamp_not_utc", "timestamp must be UTC")


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise FfPoolFbsLifecycleError("timezone_required", "timestamp requires timezone")
    return parsed.astimezone(timezone.utc)


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decimal_check(column: str) -> str:
    return (
        f"typeof({column})='text' AND length({column}) BETWEEN 1 AND 80 "
        f"AND {column} NOT GLOB '*[^0-9.-]*' AND instr(substr({column},2),'-')=0 "
        f"AND length({column})-length(replace({column},'.',''))<=1 "
        f"AND {column} NOT IN ('','-','.','-.') AND substr({column},-1,1)<>'.'"
    )


def _ensure_column(
    conn: sqlite3.Connection,
    *,
    table: str,
    column: str,
    declaration: str,
) -> None:
    columns = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _sql_values(values: tuple[str, ...]) -> str:
    return ",".join("'" + value.replace("'", "''") + "'" for value in values)
