"""Exact human-gated FF facility/pool opening and frozen FBS checkpoint.

Normal deployment creates only additive schema and keeps the writer epoch off.
The production runner owns the canonical barriers, reviewed fingerprint and
backup; this module owns the one exact SQLite transaction and its readback.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re
import sqlite3
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from packages.application.ff_pool_documents import (
    DOCUMENTS_TABLE,
    REQUESTS_TABLE,
    _apply_plan,
    _build_posting_plan,
    ensure_ff_pool_document_schema,
)
from packages.application.ff_pool_foundation import (
    BALANCES_TABLE,
    FACILITIES_TABLE,
    FEATURE_EPOCHS_TABLE,
    LINES_TABLE,
    OPERATIONS_TABLE,
    PARITY_TABLE,
    canonical_decimal_text,
    evaluate_ff_pool_aggregate_parity,
    ensure_ff_pool_foundation_schema,
    record_ff_pool_parity_diagnostic,
)
from packages.application.ff_pool_fbs_lifecycle import (
    DRAIN_STATE_TABLE as FBS_DRAIN_STATE_TABLE,
    apply_opening_fbs_backfill,
    drain_post_checkpoint_fbs_lifecycle,
    ensure_ff_pool_fbs_lifecycle_schema,
)
from packages.application.ff_wb_supply_origins import (
    ASSIGNMENTS_TABLE,
    ensure_ff_wb_supply_origin_schema,
)
from packages.application.warehouse_domain_write_guard import (
    EVENTS_TABLE as WRITE_EPOCH_EVENTS_TABLE,
    ensure_warehouse_domain_write_guard_schema,
    install_warehouse_domain_table_guards,
    warehouse_domain_write_status,
)
from packages.application.wb_fbs_orders import (
    OBSERVATIONS_TABLE,
    STATE_TABLE as FBS_COLLECTOR_STATE_TABLE,
    STATUS_CURRENT_TABLE,
    STATUS_OBSERVATIONS_TABLE,
    STATUS_TRANSITIONS_TABLE,
    ensure_wb_fbs_orders_schema,
)


CONTRACT_NAME = "ff_facility_pool_cutover_v1"
PROPOSAL_CONTRACT = "ff_facility_pool_cutover_proposal_v1"
CONTRACT_VERSION = 2
MANIFESTS_TABLE = "sheet_vitrina_v1_ff_pool_cutover_manifests"
ALLOCATIONS_TABLE = "sheet_vitrina_v1_ff_pool_cutover_allocation_lines"
ORDERS_TABLE = "sheet_vitrina_v1_ff_pool_cutover_order_classifications"
STATUS_EVIDENCE_TABLE = "sheet_vitrina_v1_ff_pool_cutover_order_status_evidence"
FBW_ORIGINS_TABLE = "sheet_vitrina_v1_ff_pool_cutover_fbw_origins"
CHECKPOINTS_TABLE = "sheet_vitrina_v1_ff_pool_cutover_checkpoints"
OPENING_RESERVATIONS_TABLE = "sheet_vitrina_v1_ff_pool_cutover_opening_reservations"
LATE_PRE_T_TABLE = "sheet_vitrina_v1_ff_pool_cutover_late_pre_t_cases"
RECOVERY_EVENTS_TABLE = "sheet_vitrina_v1_ff_pool_cutover_recovery_events"
PENDING_SHIPMENTS_TABLE = "sheet_vitrina_v1_ff_pool_cutover_pending_shipments"
FIXTURE_MARKER_TABLE = "ff_pool_cutover_test_fixture_marker"

FUNCTIONAL_ACTIVE_TABLE = "sheet_vitrina_v1_warehouse_functional_active"
FUNCTIONAL_BALANCES_TABLE = "sheet_vitrina_v1_warehouse_functional_balances"
WB_SUPPLIES_TABLE = "sheet_vitrina_v1_wb_supplies"
FF_STAGE = "ff"
POOLS = ("FBS", "FBO")
ORDER_CLASSES = (
    "pre_t_handoff_debit",
    "pre_t_absorbed_closed",
    "pre_t_absorbed_reservation",
    "pre_t_cancelled_noop",
    "post_t_deferred",
    "unmatched",
)
LEGACY_ORDER_CLASSES = (
    "pre_t_absorbed_closed",
    "pre_t_absorbed_reservation",
    "post_t_deferred",
    "unmatched",
)
STATUS_EVIDENCE_CLASSES = (
    "active_pre_handoff",
    "closed_pre_handoff",
    "unmatched",
)
PRE_T_CLASSES = frozenset(
    {
        "pre_t_handoff_debit",
        "pre_t_absorbed_closed",
        "pre_t_absorbed_reservation",
        "pre_t_cancelled_noop",
        "unmatched",
    }
)
ACTIVE_FBW_STATUS_IDS = (1, 2, 3, 4)
MAX_ALLOCATIONS = 100_000
MAX_ORDERS = 100_000
MAX_FBW_ORIGINS = 10_000
MAX_CHINA_SHIPMENTS = 10_000
MAX_MAPPINGS = 100_000
POST_CHECKPOINT_DRAIN_BATCH_SIZE = 100_000
MAX_POST_CHECKPOINT_DRAIN_BATCHES = 100
RUB_QUANTUM = Decimal("0.01")
ZERO = Decimal("0")
SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,119}")
FORBIDDEN_PROPOSAL_KEYS = frozenset(
    {
        "address",
        "address_full",
        "comment",
        "customer_comment",
        "token",
        "authorization",
        "cookie",
        "cookies",
        "raw",
        "raw_json",
        "raw_payload",
        "debit_trigger",
        "physical_debit_trigger",
    }
)


class FfPoolCutoverError(ValueError):
    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details = details


class FfPoolCutoverAmbiguousCommit(RuntimeError):
    """Fixture-only signal: commit succeeded, exact readback is required."""


def _order_classifications_table_sql(
    *,
    table: str = ORDERS_TABLE,
    if_not_exists: bool = False,
) -> str:
    qualifier = "IF NOT EXISTS " if if_not_exists else ""
    return f"""
        CREATE TABLE {qualifier}{table}(
            cutover_id TEXT NOT NULL REFERENCES {MANIFESTS_TABLE}(cutover_id),
            order_id INTEGER NOT NULL CHECK(typeof(order_id)='integer' AND order_id>0),
            observation_sequence INTEGER NOT NULL DEFAULT 0,
            status_observation_sequence INTEGER NOT NULL DEFAULT 0,
            observation_id TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            source_created_at TEXT NOT NULL,
            observed_at TEXT NOT NULL
                CHECK(substr(observed_at,-1,1)='Z' AND julianday(observed_at) IS NOT NULL),
            classification TEXT NOT NULL CHECK(classification IN ({_sql_values(ORDER_CLASSES)})),
            facility_id TEXT REFERENCES {FACILITIES_TABLE}(facility_id),
            pool TEXT CHECK(pool IS NULL OR pool='FBS'),
            nm_id INTEGER NOT NULL CHECK(typeof(nm_id)='integer' AND nm_id>0),
            quantity INTEGER NOT NULL
                CHECK(typeof(quantity)='integer' AND quantity>=0),
            status_fingerprint TEXT NOT NULL,
            mapping_digest TEXT NOT NULL,
            PRIMARY KEY(cutover_id,order_id)
        );
    """


def ensure_ff_pool_cutover_schema(conn: sqlite3.Connection) -> None:
    """Create additive empty Stage 6 objects; never seed or activate them."""

    ensure_ff_pool_foundation_schema(conn)
    ensure_ff_pool_document_schema(conn)
    ensure_ff_pool_fbs_lifecycle_schema(conn)
    ensure_ff_wb_supply_origin_schema(conn)
    ensure_wb_fbs_orders_schema(conn)
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {MANIFESTS_TABLE}(
            cutover_id TEXT PRIMARY KEY,
            manifest_digest TEXT NOT NULL UNIQUE,
            deployed_sha TEXT NOT NULL,
            cutover_at TEXT NOT NULL
                CHECK(substr(cutover_at,-1,1)='Z' AND julianday(cutover_at) IS NOT NULL),
            business_date TEXT NOT NULL
                CHECK(length(business_date)=10 AND date(business_date)=business_date),
            feature_epoch INTEGER NOT NULL
                REFERENCES {FEATURE_EPOCHS_TABLE}(epoch),
            aggregate_revision TEXT NOT NULL,
            aggregate_digest TEXT NOT NULL,
            detail_digest TEXT NOT NULL,
            observation_watermark_sequence INTEGER NOT NULL
                CHECK(typeof(observation_watermark_sequence)='integer'
                      AND observation_watermark_sequence>=0),
            observation_watermark_digest TEXT NOT NULL,
            mapping_digest TEXT NOT NULL,
            fbw_origins_digest TEXT NOT NULL,
            control_evidence_digest TEXT NOT NULL,
            non_target_digest TEXT NOT NULL,
            opening_document_id TEXT NOT NULL,
            source_snapshot_digest TEXT NOT NULL,
            created_at TEXT NOT NULL
                CHECK(substr(created_at,-1,1)='Z' AND julianday(created_at) IS NOT NULL),
            manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),
            CHECK(length(trim(cutover_id)) BETWEEN 8 AND 120),
            CHECK(length(deployed_sha)=40 AND deployed_sha NOT GLOB '*[^0-9a-f]*')
        );
        CREATE INDEX IF NOT EXISTS ff_pool_cutover_manifest_by_time
        ON {MANIFESTS_TABLE}(cutover_at,cutover_id);

        CREATE TABLE IF NOT EXISTS {ALLOCATIONS_TABLE}(
            cutover_id TEXT NOT NULL REFERENCES {MANIFESTS_TABLE}(cutover_id),
            line_no INTEGER NOT NULL
                CHECK(typeof(line_no)='integer' AND line_no>0),
            facility_id TEXT NOT NULL REFERENCES {FACILITIES_TABLE}(facility_id),
            pool TEXT NOT NULL CHECK(pool IN ('FBS','FBO')),
            nm_id INTEGER NOT NULL CHECK(typeof(nm_id)='integer' AND nm_id>0),
            quantity INTEGER NOT NULL
                CHECK(typeof(quantity)='integer'),
            capital_rub TEXT NOT NULL CHECK({_decimal_check('capital_rub')}),
            wac_rub TEXT CHECK(wac_rub IS NULL OR {_decimal_check('wac_rub')}),
            allocation_digest TEXT NOT NULL,
            PRIMARY KEY(cutover_id,line_no),
            UNIQUE(cutover_id,facility_id,pool,nm_id)
        );
        CREATE INDEX IF NOT EXISTS ff_pool_cutover_allocations_by_nm
        ON {ALLOCATIONS_TABLE}(cutover_id,nm_id,facility_id,pool);

        {_order_classifications_table_sql(if_not_exists=True)}
        CREATE INDEX IF NOT EXISTS ff_pool_cutover_orders_by_class
        ON {ORDERS_TABLE}(cutover_id,classification,order_id);

        CREATE TABLE IF NOT EXISTS {STATUS_EVIDENCE_TABLE}(
            order_id INTEGER NOT NULL CHECK(typeof(order_id)='integer' AND order_id>0),
            source_revision TEXT NOT NULL,
            evidence_digest TEXT NOT NULL,
            lifecycle_class TEXT NOT NULL CHECK(lifecycle_class IN ({_sql_values(STATUS_EVIDENCE_CLASSES)})),
            quantity INTEGER NOT NULL CHECK(typeof(quantity)='integer' AND quantity>0),
            observed_at TEXT NOT NULL
                CHECK(substr(observed_at,-1,1)='Z' AND julianday(observed_at) IS NOT NULL),
            evidence_source TEXT NOT NULL DEFAULT 'official_wb_status_shadow'
                CHECK(evidence_source='official_wb_status_shadow'),
            PRIMARY KEY(order_id,source_revision,evidence_digest)
        );
        CREATE INDEX IF NOT EXISTS ff_pool_cutover_status_evidence_by_order
        ON {STATUS_EVIDENCE_TABLE}(order_id,source_revision,observed_at DESC);

        CREATE TABLE IF NOT EXISTS {FBW_ORIGINS_TABLE}(
            cutover_id TEXT NOT NULL REFERENCES {MANIFESTS_TABLE}(cutover_id),
            wb_supply_cache_key TEXT NOT NULL,
            wb_supply_id TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            facility_id TEXT NOT NULL REFERENCES {FACILITIES_TABLE}(facility_id),
            pool TEXT NOT NULL DEFAULT 'FBO' CHECK(pool='FBO'),
            evidence_digest TEXT NOT NULL,
            PRIMARY KEY(cutover_id,wb_supply_cache_key)
        );
        CREATE INDEX IF NOT EXISTS ff_pool_cutover_fbw_origins_by_facility
        ON {FBW_ORIGINS_TABLE}(cutover_id,facility_id,wb_supply_cache_key);

        CREATE TABLE IF NOT EXISTS {CHECKPOINTS_TABLE}(
            cutover_id TEXT PRIMARY KEY REFERENCES {MANIFESTS_TABLE}(cutover_id),
            cutover_at TEXT NOT NULL,
            accounting_boundary_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00Z',
            feature_epoch INTEGER NOT NULL,
            observation_watermark_sequence INTEGER NOT NULL,
            status_observation_watermark_sequence INTEGER NOT NULL DEFAULT 0,
            status_transition_watermark_sequence INTEGER NOT NULL DEFAULT 0,
            observation_watermark_digest TEXT NOT NULL,
            frozen_evidence_digest TEXT NOT NULL DEFAULT '',
            collector_window_from INTEGER NOT NULL,
            collector_window_to INTEGER NOT NULL,
            collector_next_cursor INTEGER NOT NULL,
            collector_complete INTEGER NOT NULL CHECK(collector_complete IN (0,1)),
            checkpoint_digest TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS {OPENING_RESERVATIONS_TABLE}(
            cutover_id TEXT NOT NULL REFERENCES {MANIFESTS_TABLE}(cutover_id),
            order_id INTEGER NOT NULL,
            facility_id TEXT NOT NULL REFERENCES {FACILITIES_TABLE}(facility_id),
            pool TEXT NOT NULL DEFAULT 'FBS' CHECK(pool='FBS'),
            nm_id INTEGER NOT NULL CHECK(typeof(nm_id)='integer' AND nm_id>0),
            reserved_quantity INTEGER NOT NULL
                CHECK(typeof(reserved_quantity)='integer' AND reserved_quantity>0),
            frozen_wac_rub TEXT NOT NULL CHECK({_decimal_check('frozen_wac_rub')}),
            frozen_capital_rub TEXT NOT NULL CHECK({_decimal_check('frozen_capital_rub')}),
            source_revision TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'opening_absorbed'
                CHECK(state='opening_absorbed'),
            PRIMARY KEY(cutover_id,order_id,nm_id),
            FOREIGN KEY(cutover_id,order_id)
                REFERENCES {ORDERS_TABLE}(cutover_id,order_id)
        );
        CREATE INDEX IF NOT EXISTS ff_pool_opening_reservations_by_location
        ON {OPENING_RESERVATIONS_TABLE}(cutover_id,facility_id,pool,nm_id,order_id);

        CREATE TABLE IF NOT EXISTS {LATE_PRE_T_TABLE}(
            case_id TEXT PRIMARY KEY,
            cutover_id TEXT NOT NULL REFERENCES {MANIFESTS_TABLE}(cutover_id),
            order_id INTEGER NOT NULL,
            observation_id TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            source_created_at TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'isolated' CHECK(state='isolated'),
            reason_code TEXT NOT NULL DEFAULT 'late_pre_t',
            display_reason TEXT NOT NULL DEFAULT 'Поздний заказ до границы',
            evidence_digest TEXT NOT NULL,
            UNIQUE(cutover_id,order_id,source_revision)
        );
        CREATE INDEX IF NOT EXISTS ff_pool_late_pre_t_by_cutover_time
        ON {LATE_PRE_T_TABLE}(cutover_id,detected_at,order_id);

        CREATE TABLE IF NOT EXISTS {RECOVERY_EVENTS_TABLE}(
            recovery_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            cutover_id TEXT NOT NULL REFERENCES {MANIFESTS_TABLE}(cutover_id),
            event_type TEXT NOT NULL CHECK(event_type IN(
                'applied','readback_passed','readback_failed',
                'forward_reconciliation_required','compensation_recorded'
            )),
            event_at TEXT NOT NULL,
            evidence_digest TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(details_json)),
            UNIQUE(cutover_id,event_type,evidence_digest)
        );
        CREATE INDEX IF NOT EXISTS ff_pool_cutover_recovery_by_cutover
        ON {RECOVERY_EVENTS_TABLE}(cutover_id,recovery_sequence,event_type);

        CREATE TABLE IF NOT EXISTS {PENDING_SHIPMENTS_TABLE}(
            cutover_id TEXT NOT NULL REFERENCES {MANIFESTS_TABLE}(cutover_id),
            shipment_id TEXT NOT NULL,
            invoice_no TEXT NOT NULL,
            classification TEXT NOT NULL CHECK(classification='excluded_pending_receipt'),
            facility_id TEXT NOT NULL REFERENCES {FACILITIES_TABLE}(facility_id),
            pools_json TEXT NOT NULL CHECK(json_valid(pools_json)),
            expected_quantity INTEGER NOT NULL
                CHECK(typeof(expected_quantity)='integer' AND expected_quantity>0),
            actual_ff_acceptance_date TEXT NOT NULL DEFAULT '',
            receipt_operation_count INTEGER NOT NULL CHECK(receipt_operation_count=0),
            cost_layer_count INTEGER NOT NULL CHECK(cost_layer_count=0),
            evidence_digest TEXT NOT NULL,
            post_cutover_state TEXT NOT NULL CHECK(post_cutover_state='in_transit'),
            guided_acceptance_required INTEGER NOT NULL
                CHECK(guided_acceptance_required=1),
            PRIMARY KEY(cutover_id,shipment_id)
        );
        CREATE INDEX IF NOT EXISTS ff_pool_cutover_pending_shipments_by_invoice
        ON {PENDING_SHIPMENTS_TABLE}(cutover_id,invoice_no,shipment_id);
        """
    )
    for table, column, declaration in (
        (ORDERS_TABLE, "observation_sequence", "INTEGER NOT NULL DEFAULT 0"),
        (ORDERS_TABLE, "status_observation_sequence", "INTEGER NOT NULL DEFAULT 0"),
        (
            CHECKPOINTS_TABLE,
            "accounting_boundary_at",
            "TEXT NOT NULL DEFAULT '1970-01-01T00:00:00Z'",
        ),
        (
            CHECKPOINTS_TABLE,
            "status_observation_watermark_sequence",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            CHECKPOINTS_TABLE,
            "status_transition_watermark_sequence",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (CHECKPOINTS_TABLE, "frozen_evidence_digest", "TEXT NOT NULL DEFAULT ''"),
    ):
        _ensure_column(conn, table=table, column=column, declaration=declaration)
    _ensure_order_classification_schema(conn)
    for table in (
        MANIFESTS_TABLE,
        ALLOCATIONS_TABLE,
        ORDERS_TABLE,
        STATUS_EVIDENCE_TABLE,
        FBW_ORIGINS_TABLE,
        CHECKPOINTS_TABLE,
        OPENING_RESERVATIONS_TABLE,
        LATE_PRE_T_TABLE,
        RECOVERY_EVENTS_TABLE,
        PENDING_SHIPMENTS_TABLE,
    ):
        suffix = table.removeprefix("sheet_vitrina_v1_")
        conn.execute(
            f"""CREATE TRIGGER IF NOT EXISTS {suffix}_no_update
                BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT,'FF pool cutover evidence is immutable'); END"""
        )
        conn.execute(
            f"""CREATE TRIGGER IF NOT EXISTS {suffix}_no_delete
                BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT,'FF pool cutover evidence is append-only'); END"""
        )
    ensure_warehouse_domain_write_guard_schema(conn)
    install_warehouse_domain_table_guards(conn)


def build_ff_pool_cutover_plan(
    conn: sqlite3.Connection,
    *,
    proposal: Mapping[str, Any],
    deployed_sha: str,
    cutover_at: str = "",
) -> dict[str, Any]:
    """Build one exact query-only plan; ``T`` is never chosen implicitly."""

    _require_commit_sha(deployed_sha)
    _reject_forbidden_keys(proposal)
    normalized = _normalize_proposal(proposal)
    blockers: list[dict[str, Any]] = []
    schema = _schema_status(conn)
    if not schema["available"]:
        blockers.append(
            {
                "code": "cutover_schema_absent",
                "missing_tables": schema["missing_tables"],
            }
        )
        return _blocked_plan(
            proposal=normalized,
            deployed_sha=deployed_sha,
            cutover_at=cutover_at,
            blockers=blockers,
            status="schema_absent",
        )
    if not cutover_at:
        blockers.append(
            {
                "code": "cutover_boundary_not_bound",
                "message": "T выбирается только под подтверждённым write barrier",
            }
        )
        return _blocked_plan(
            proposal=normalized,
            deployed_sha=deployed_sha,
            cutover_at="",
            blockers=blockers,
            status="awaiting_boundary",
        )
    boundary = _utc_timestamp(cutover_at, field="cutover_at")
    business_date = _business_date_yekaterinburg(boundary)
    if normalized["business_date"] != business_date:
        blockers.append(
            {
                "code": "business_date_mismatch",
                "expected": business_date,
                "actual": normalized["business_date"],
            }
        )
    barrier = warehouse_domain_write_status(conn)
    controls = normalized["control_evidence"]
    if not (
        barrier.get("phase") == "held"
        and barrier.get("active") is True
        and str(barrier.get("epoch_id") or "") == normalized["write_epoch_id"]
        and str(barrier.get("manifest_digest") or "")
        == normalized["control_manifest_digest"]
        and str(barrier.get("deployed_sha") or "") == deployed_sha
        and controls["maintenance_quiet"]
        and controls["http_write_barrier_active"]
        and controls["warehouse_timer_held"]
        and not controls["warehouse_lock_held"]
    ):
        blockers.append(
            {
                "code": "domain_write_boundary_not_held",
                "barrier": _safe_barrier(barrier),
            }
        )

    aggregate = _aggregate_snapshot(conn, blockers=blockers)
    target_epoch = _target_feature_epoch(conn, normalized, blockers=blockers)
    facilities = _active_facilities(conn)
    allocation = _allocation_snapshot(
        normalized["allocations"],
        aggregate_rows=aggregate["rows"],
        facilities=facilities,
        blockers=blockers,
    )
    _require_empty_detail(conn, blockers=blockers)

    mappings = _mapping_snapshot(
        normalized,
        facilities=facilities,
        blockers=blockers,
    )
    observations = _observation_snapshot(
        conn,
        normalized=normalized,
        accounting_boundary_at=str(
            normalized["collector_checkpoint"].get("accounting_boundary_at")
            or boundary
        ),
        mappings=mappings,
        blockers=blockers,
    )
    _validate_opening_reservation_sufficiency(
        observations["classifications"], allocation["rows"], blockers=blockers
    )
    fbw_origins = _fbw_origin_snapshot(
        conn,
        normalized=normalized,
        target_epoch=target_epoch,
        facilities=facilities,
        blockers=blockers,
    )
    china_shipments = _china_shipment_snapshot(
        conn,
        normalized["china_shipments"],
        facilities=facilities,
        blockers=blockers,
    )
    collector = _collector_checkpoint_snapshot(
        conn,
        normalized=normalized,
        observations=observations,
        blockers=blockers,
        fallback_boundary_at=boundary,
    )
    non_target = _bounded_non_target_snapshot(conn)
    requested_non_target = normalized["non_target_evidence_digest"]
    if requested_non_target != non_target["digest"]:
        blockers.append(
            {
                "code": "non_target_evidence_stale",
                "expected": requested_non_target,
                "actual": non_target["digest"],
            }
        )

    source_snapshot = {
        "aggregate": aggregate,
        "allocations": allocation,
        "observations": observations,
        "collector_checkpoint": collector,
        "mappings": {
            "seller_warehouse_mappings": mappings["seller_warehouse_mappings"],
            "sku_mappings": mappings["sku_mappings"],
            "digest": mappings["digest"],
        },
        "fbw_origins": fbw_origins,
        "china_shipments": china_shipments,
        "control_evidence": controls,
        "domain_write_epoch": _safe_barrier(barrier),
        "non_target": non_target,
    }
    source_snapshot_digest = _fingerprint(source_snapshot)
    opening_document_id = "ffpd_" + _fingerprint(
        {"cutover_id": normalized["cutover_id"], "source": source_snapshot_digest}
    ).removeprefix("sha256:")[:28]
    projected_aggregate = _project_post_backfill_aggregate(
        aggregate_rows=aggregate["rows"],
        allocations=allocation["rows"],
        classifications=observations["classifications"],
        blockers=blockers,
    )
    manifest = {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "cutover_id": normalized["cutover_id"],
        "proposal_digest": _fingerprint(normalized),
        "deployed_sha": deployed_sha,
        "cutover_at": boundary,
        "business_date": business_date,
        "feature_epoch": target_epoch,
        "write_epoch_id": normalized["write_epoch_id"],
        "control_manifest_digest": normalized["control_manifest_digest"],
        "aggregate_revision": aggregate["revision"],
        "aggregate_digest": aggregate["digest"],
        "aggregate_rows": aggregate["rows"],
        "post_backfill_aggregate_rows": projected_aggregate["rows"],
        "post_backfill_allocations": projected_aggregate["detail_rows"],
        "historical_fbs_backfill": projected_aggregate["summary"],
        "allocations": allocation["rows"],
        "detail_digest": allocation["digest"],
        "order_classifications": observations["classifications"],
        "accounting_boundary": collector["accounting_boundary"],
        "observation_watermark_sequence": observations["watermark_sequence"],
        "status_observation_watermark_sequence": collector[
            "accounting_boundary"
        ]["status_observation_watermark_sequence"],
        "status_transition_watermark_sequence": collector[
            "accounting_boundary"
        ]["status_transition_watermark_sequence"],
        "observation_watermark_digest": observations["digest"],
        "seller_warehouse_mappings": mappings["seller_warehouse_mappings"],
        "sku_mappings": mappings["sku_mappings"],
        "mapping_digest": mappings["digest"],
        "fbw_origin_assignments": fbw_origins["rows"],
        "fbw_origins_digest": fbw_origins["digest"],
        "china_shipments": china_shipments["rows"],
        "china_shipments_digest": china_shipments["digest"],
        "handoff_policy": {
            **normalized["handoff_policy"],
            "official_semantics": (
                "WB-controlled warehouse handoff is supplierStatus=complete "
                "and wbStatus=sorted; supplierStatus=complete alone is forbidden"
            ),
            "official_sources": [
                "https://dev.wildberries.ru/openapi/orders-fbs/",
                "https://dev.wildberries.ru/openapi-other/sandbox-environment",
            ],
            "supplier_status_complete_alone_forbidden": True,
        },
        "collector_checkpoint": collector,
        "control_evidence_digest": _fingerprint(controls),
        "non_target_digest": non_target["digest"],
        "non_target": non_target,
        "source_snapshot_digest": source_snapshot_digest,
        "opening_document_id": opening_document_id,
        "invariants": {
            "opening_source_aggregate_conserved": True,
            "opening_aggregate_detail_quantity_parity": allocation["quantity_matches"],
            "opening_aggregate_detail_capital_parity": allocation["capital_matches"],
            "post_backfill_aggregate_detail_parity": True,
            "no_new_warehouse_stage": True,
            "opening_absorption_once": True,
            "late_pre_t_isolated": True,
            "fbw_origin_explicit_not_destination_inferred": True,
            "supplier_status_complete_never_debits": True,
            "physical_debit_trigger_selected": bool(normalized["handoff_policy"]["approved"]),
            "physical_debit_trigger": (
                "supplierStatus=complete AND wbStatus=sorted"
                if normalized["handoff_policy"]["approved"]
                else "proposed_only"
            ),
            "wb_mutation": False,
            "journal_mode_change": False,
            "legacy_relation_backfill": False,
        },
    }
    manifest_digest = _fingerprint(manifest)
    counts = _classification_counts(observations["classifications"])
    ready = not blockers
    owner_policy_approved = bool(normalized["handoff_policy"]["approved"])
    return {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "status": "ready" if ready else "blocked",
        "dry_run": True,
        "apply_surface_available": True,
        "apply_allowed": ready and owner_policy_approved,
        "production_apply_contract_ready": True,
        "requires_exact_human_gate": True,
        "manifest_digest": manifest_digest if ready else "",
        "manifest": manifest,
        "source_snapshot_digest": source_snapshot_digest,
        "blockers": blockers,
        "observability": {
            "manifest_digest": manifest_digest if ready else "",
            "checkpoint_epoch": target_epoch,
            "aggregate_quantity": aggregate["total_quantity"],
            "detail_quantity": allocation["total_quantity"],
            "aggregate_capital_rub": aggregate["total_capital_rub"],
            "detail_capital_rub": allocation["total_capital_rub"],
            "unmatched_count": counts["unmatched"],
            "late_pre_t_count": 0,
            "opening_reservation_count": counts["pre_t_absorbed_reservation"],
            "post_t_deferred_count": counts["post_t_deferred"],
            "barrier": _safe_barrier(barrier),
            "next_action": (
                "apply_exact_fingerprint_under_confirmed_barrier"
                if ready and owner_policy_approved
                else "obtain_exact_owner_gate_for_proposed_complete_sorted_rule"
                if ready
                else "resolve_local_manifest_blockers"
            ),
        },
        "rebuild_input": {
            "proposal": normalized,
            "deployed_sha": deployed_sha,
            "cutover_at": boundary,
        },
    }


def read_ff_pool_cutover_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return bounded query-only checkpoint/barrier/recovery observability."""

    schema = _schema_status(conn)
    if not schema["available"]:
        return {
            "contract_name": CONTRACT_NAME,
            "status": "schema_absent",
            "apply_surface_available": True,
            "missing_tables": schema["missing_tables"],
        }
    manifest = conn.execute(
        f"""SELECT cutover_id,manifest_digest,deployed_sha,cutover_at,
                   business_date,feature_epoch,aggregate_revision,
                   aggregate_digest,detail_digest,observation_watermark_sequence,
                   observation_watermark_digest,mapping_digest,fbw_origins_digest,
                   control_evidence_digest,non_target_digest,opening_document_id,
                   source_snapshot_digest,created_at
            FROM {MANIFESTS_TABLE} ORDER BY cutover_at DESC,cutover_id DESC LIMIT 1"""
    ).fetchone()
    barrier = warehouse_domain_write_status(conn)
    feature = conn.execute(
        f"SELECT epoch,writer_enabled,reader_enabled,source_revision,created_at "
        f"FROM {FEATURE_EPOCHS_TABLE} ORDER BY epoch DESC LIMIT 1"
    ).fetchone()
    if manifest is None:
        return {
            "contract_name": CONTRACT_NAME,
            "status": "not_applied",
            "apply_surface_available": False,
            "manifest": None,
            "checkpoint": None,
            "feature_epoch": _feature_row(feature),
            "barrier": _safe_barrier(barrier),
            "counts": {
                "allocations": 0,
                "orders": 0,
                "opening_reservations": 0,
                "late_pre_t": 0,
                "recovery_events": 0,
                "excluded_pending_receipts": 0,
            },
            "next_action": "prepare_exact_manifest_under_later_human_gate",
        }
    cutover_id = str(manifest[0])
    checkpoint = conn.execute(
        f"SELECT * FROM {CHECKPOINTS_TABLE} WHERE cutover_id=?",
        (cutover_id,),
    ).fetchone()
    counts = {
        "allocations": _count(conn, ALLOCATIONS_TABLE, cutover_id),
        "orders": _count(conn, ORDERS_TABLE, cutover_id),
        "opening_reservations": _count(conn, OPENING_RESERVATIONS_TABLE, cutover_id),
        "late_pre_t": _count(conn, LATE_PRE_T_TABLE, cutover_id),
        "recovery_events": _count(conn, RECOVERY_EVENTS_TABLE, cutover_id),
        "excluded_pending_receipts": _count(conn, PENDING_SHIPMENTS_TABLE, cutover_id),
        "unmatched": int(
            conn.execute(
                f"SELECT COUNT(*) FROM {ORDERS_TABLE} "
                "WHERE cutover_id=? AND classification='unmatched'",
                (cutover_id,),
            ).fetchone()[0]
        ),
    }
    next_action = (
        "forward_reconciliation_required"
        if barrier.get("phase") == "recovery_required"
        else "exact_readback_and_human_reconciliation_required"
        if barrier.get("phase") == "readback_required"
        else "none"
        if barrier.get("phase") == "released"
        else "retain_barrier_and_investigate"
    )
    readback = read_ff_pool_cutover_readback(conn, cutover_id=cutover_id)
    return {
        "contract_name": CONTRACT_NAME,
        "status": "applied_unreleased" if barrier.get("active") else "applied",
        "apply_surface_available": True,
        "manifest": _manifest_row(manifest),
        "checkpoint": _row_dict(checkpoint),
        "feature_epoch": _feature_row(feature),
        "barrier": _safe_barrier(barrier),
        "counts": counts,
        "next_action": next_action,
        "readback": readback,
        "recovery_policy": {
            "before_commit": "transaction_rollback_no_partial_state",
            "ambiguous_after_commit": "retain_barrier_and_exact_readback",
            "after_live_events": "forward_reconciliation_or_compensating_documents",
            "delete_or_blind_replay": False,
        },
    }


def read_ff_pool_cutover_readback(
    conn: sqlite3.Connection, *, cutover_id: str
) -> dict[str, Any]:
    """Recompute exact immutable and accounting evidence after an ambiguous commit."""

    identity = _identifier(cutover_id, "cutover_id")
    row = conn.execute(
        f"SELECT manifest_digest,manifest_json,feature_epoch,opening_document_id FROM {MANIFESTS_TABLE} WHERE cutover_id=?",
        (identity,),
    ).fetchone()
    if row is None:
        return {"status": "not_applied", "cutover_id": identity, "mismatches": ["manifest_missing"]}
    manifest = json.loads(str(row[1]))
    mismatches: list[str] = []
    if _fingerprint(manifest) != str(row[0]):
        mismatches.append("manifest_digest")
    allocation_rows = [
        {
            "line_no": int(item[0]), "facility_id": str(item[1]), "pool": str(item[2]),
            "nm_id": int(item[3]), "quantity": int(item[4]), "capital_rub": str(item[5]),
            "wac_rub": None if item[6] is None else str(item[6]), "allocation_digest": str(item[7]),
        }
        for item in conn.execute(
            f"""SELECT line_no,facility_id,pool,nm_id,quantity,capital_rub,wac_rub,allocation_digest
                 FROM {ALLOCATIONS_TABLE} WHERE cutover_id=? ORDER BY line_no""",
            (identity,),
        ).fetchall()
    ]
    if allocation_rows != list(manifest.get("allocations") or []):
        mismatches.append("allocation_evidence")
    detail = {
        (str(item[0]), str(item[1]), int(item[2])): (int(item[3]), str(item[4]))
        for item in conn.execute(
            f"SELECT facility_id,pool,nm_id,quantity,capital_rub FROM {BALANCES_TABLE} WHERE projection_epoch=?",
            (int(row[2]),),
        ).fetchall()
    }
    expected_detail = {
        (str(item["facility_id"]), str(item["pool"]), int(item["nm_id"])): (
            int(item["quantity"]), Decimal(str(item["capital_rub"]))
        )
        for item in manifest.get("post_backfill_allocations") or manifest.get("allocations") or []
    }
    lifecycle_table = "sheet_vitrina_v1_ff_pool_fbs_lifecycle_events"
    if _table_exists(conn, lifecycle_table):
        for event in conn.execute(
            f"""SELECT facility_id,pool,nm_id,physical_quantity_delta,capital_delta_rub
                FROM {lifecycle_table}
                WHERE cutover_id=? AND event_type='handoff_debit'
                ORDER BY event_sequence""",
            (identity,),
        ).fetchall():
            key = (str(event[0]), str(event[1]), int(event[2]))
            quantity, capital = expected_detail.get(key, (0, ZERO))
            expected_detail[key] = (
                quantity + int(event[3]), capital + Decimal(str(event[4]))
            )
    document_movements = conn.execute(
        f"""SELECT line.facility_id,line.pool,line.nm_id,line.quantity_delta,
                   line.capital_delta_rub
            FROM {LINES_TABLE} AS line
            JOIN {OPERATIONS_TABLE} AS operation USING(operation_id)
            WHERE operation.idempotency_epoch=?
              AND operation.source_type LIKE 'ff_pool_document:%'
            ORDER BY operation.posted_at,operation.operation_id,line.line_no""",
        (int(row[2]),),
    ).fetchall()
    for movement in document_movements:
        key = (str(movement[0]), str(movement[1]), int(movement[2]))
        quantity, capital = expected_detail.get(key, (0, ZERO))
        expected_detail[key] = (
            quantity + int(movement[3]), capital + Decimal(str(movement[4]))
        )
    normalized_detail = {
        key: (quantity, canonical_decimal_text(capital))
        for key, (quantity, capital) in expected_detail.items()
    }
    if detail != normalized_detail:
        mismatches.append("detail_projection")
    aggregate_by_nm = {
        int(item["nm_id"]): (int(item["quantity"]), Decimal(str(item["capital_rub"])))
        for item in manifest.get("post_backfill_aggregate_rows") or manifest.get("aggregate_rows") or []
    }
    if _table_exists(conn, lifecycle_table):
        for event in conn.execute(
            f"""SELECT nm_id,physical_quantity_delta,capital_delta_rub
                FROM {lifecycle_table}
                WHERE cutover_id=? AND event_type='handoff_debit'
                ORDER BY event_sequence""",
            (identity,),
        ).fetchall():
            nm_id = int(event[0])
            quantity, capital = aggregate_by_nm.get(nm_id, (0, ZERO))
            aggregate_by_nm[nm_id] = (
                quantity + int(event[1]), capital + Decimal(str(event[2]))
            )
    for movement in document_movements:
        nm_id = int(movement[2])
        quantity, capital = aggregate_by_nm.get(nm_id, (0, ZERO))
        aggregate_by_nm[nm_id] = (
            quantity + int(movement[3]), capital + Decimal(str(movement[4]))
        )
    summed: dict[int, tuple[int, Decimal]] = {}
    for (_facility_id, _pool, nm_id), (item_quantity, item_capital) in expected_detail.items():
        quantity, capital = summed.get(nm_id, (0, ZERO))
        summed[nm_id] = (
            quantity + int(item_quantity), capital + Decimal(str(item_capital))
        )
    if summed != aggregate_by_nm:
        mismatches.append("aggregate_detail_parity")
    feature = conn.execute(
        f"SELECT writer_enabled,reader_enabled,source_revision FROM {FEATURE_EPOCHS_TABLE} WHERE epoch=?",
        (int(row[2]),),
    ).fetchone()
    if feature is None or (int(feature[0]), int(feature[1]), str(feature[2])) != (1, 1, str(row[0])):
        mismatches.append("feature_epoch")
    checkpoint = conn.execute(
        f"""SELECT cutover_at,accounting_boundary_at,feature_epoch,
                    observation_watermark_sequence,
                    status_observation_watermark_sequence,
                    status_transition_watermark_sequence,
                    observation_watermark_digest,frozen_evidence_digest,
                    collector_window_from,collector_window_to,
                    collector_next_cursor,collector_complete,checkpoint_digest
             FROM {CHECKPOINTS_TABLE} WHERE cutover_id=?""",
        (identity,),
    ).fetchone()
    expected_checkpoint = dict(manifest.get("collector_checkpoint") or {})
    if checkpoint is None:
        mismatches.append("checkpoint")
    else:
        if (
            str(checkpoint[0]) != str(manifest["cutover_at"])
            or str(checkpoint[1])
            != str(manifest["accounting_boundary"]["local_boundary_at"])
            or int(checkpoint[2]) != int(manifest["feature_epoch"])
            or int(checkpoint[3])
            != int(manifest["observation_watermark_sequence"])
            or int(checkpoint[4])
            != int(manifest["status_observation_watermark_sequence"])
            or int(checkpoint[5])
            != int(manifest["status_transition_watermark_sequence"])
            or str(checkpoint[6]) != str(manifest["observation_watermark_digest"])
            or str(checkpoint[7])
            != str(manifest["accounting_boundary"]["frozen_evidence_digest"])
            or str(checkpoint[12])
            != str(expected_checkpoint.get("checkpoint_digest") or "")
        ):
            mismatches.append("checkpoint")
    drain_state = conn.execute(
        f"""SELECT frozen_order_observation_sequence,
                   frozen_status_observation_sequence,
                   frozen_status_transition_sequence,
                   last_status_observation_sequence,drain_run_count,
                   last_result_digest,updated_at
            FROM {FBS_DRAIN_STATE_TABLE} WHERE cutover_id=?""",
        (identity,),
    ).fetchone()
    expected_boundary = dict(manifest.get("accounting_boundary") or {})
    if drain_state is None:
        mismatches.append("fbs_drain_checkpoint")
        drain = None
    else:
        drain = {
            "frozen_order_observation_sequence": int(drain_state[0]),
            "frozen_status_observation_sequence": int(drain_state[1]),
            "frozen_status_transition_sequence": int(drain_state[2]),
            "last_status_observation_sequence": int(drain_state[3]),
            "drain_run_count": int(drain_state[4]),
            "last_result_digest": str(drain_state[5]),
            "updated_at": str(drain_state[6]),
        }
        if (
            drain["frozen_order_observation_sequence"]
            != int(expected_boundary["order_observation_watermark_sequence"])
            or drain["frozen_status_observation_sequence"]
            != int(expected_boundary["status_observation_watermark_sequence"])
            or drain["frozen_status_transition_sequence"]
            != int(expected_boundary["status_transition_watermark_sequence"])
        ):
            mismatches.append("fbs_drain_checkpoint")
    persisted_orders = [
        {
            "order_id": int(item[0]), "observation_sequence": int(item[1]),
            "status_observation_sequence": int(item[2]),
            "observation_id": str(item[3]), "source_revision": str(item[4]),
            "source_created_at": str(item[5]), "observed_at": str(item[6]),
            "classification": str(item[7]),
            "facility_id": None if item[8] is None else str(item[8]),
            "pool": None if item[9] is None else str(item[9]), "nm_id": int(item[10]),
            "quantity": int(item[11]), "status_fingerprint": str(item[12]),
            "mapping_digest": str(item[13]),
        }
        for item in conn.execute(
            f"""SELECT order_id,observation_sequence,status_observation_sequence,
                        observation_id,source_revision,source_created_at,observed_at,
                        classification,facility_id,pool,nm_id,quantity,
                        status_fingerprint,mapping_digest
                 FROM {ORDERS_TABLE} WHERE cutover_id=? ORDER BY order_id""",
            (identity,),
        ).fetchall()
    ]
    manifest_orders = list(manifest.get("order_classifications") or [])
    persisted_projection_keys = (
        "order_id",
        "observation_sequence",
        "status_observation_sequence",
        "observation_id",
        "source_revision",
        "source_created_at",
        "observed_at",
        "classification",
        "facility_id",
        "pool",
        "nm_id",
        "quantity",
        "status_fingerprint",
        "mapping_digest",
    )
    manifest_order_projection = [
        {key: item.get(key) for key in persisted_projection_keys}
        for item in manifest_orders
    ]
    if persisted_orders != manifest_order_projection:
        mismatches.append("order_classifications")
    for item in manifest_orders:
        primary = _official_order_status_evidence(
            conn,
            order_id=int(item["order_id"]),
            cutover_at=str(manifest["accounting_boundary"]["local_boundary_at"]),
            classification=str(item["classification"]),
            max_observation_sequence=int(
                manifest["status_observation_watermark_sequence"]
            ),
        )
        reconciliation = _official_post_handoff_reconciliation_evidence(
            conn,
            order_id=int(item["order_id"]),
            cutover_at=str(manifest["accounting_boundary"]["local_boundary_at"]),
            classification=str(item["classification"]),
            max_observation_sequence=int(
                manifest["status_observation_watermark_sequence"]
            ),
        )
        if primary != item.get("status_evidence"):
            mismatches.append(f"order_status_evidence:{int(item['order_id'])}")
        if reconciliation != item.get("post_handoff_reconciliation"):
            mismatches.append(
                f"order_reconciliation_evidence:{int(item['order_id'])}"
            )
    persisted_fbw = [
        {
            "wb_supply_cache_key": str(item[0]), "wb_supply_id": str(item[1]),
            "source_revision": str(item[2]), "feature_epoch": int(manifest["feature_epoch"]),
            "facility_id": str(item[3]), "pool": str(item[4]), "evidence_digest": str(item[5]),
        }
        for item in conn.execute(
            f"""SELECT wb_supply_cache_key,wb_supply_id,source_revision,facility_id,pool,evidence_digest
                 FROM {FBW_ORIGINS_TABLE} WHERE cutover_id=? ORDER BY wb_supply_cache_key""",
            (identity,),
        ).fetchall()
    ]
    if persisted_fbw != list(manifest.get("fbw_origin_assignments") or []):
        mismatches.append("fbw_origins")
    opening = conn.execute(
        """SELECT source_revision,business_date,posted_manifest_json
             FROM sheet_vitrina_v1_ff_pool_documents
             WHERE document_id=? AND document_kind='facility_pool_opening'""",
        (str(row[3]),),
    ).fetchone()
    if opening is None:
        mismatches.append("opening_document")
    else:
        posted = json.loads(str(opening[2]))
        domain = dict(posted.get("domain") or {})
        expected_aggregate = {
            str(item["nm_id"]): {
                "quantity": int(item["quantity"]),
                "capital_rub": canonical_decimal_text(item["capital_rub"]),
            }
            for item in manifest.get("aggregate_rows") or []
        }
        if (
            str(opening[0]) != str(row[0])
            or str(opening[1]) != str(manifest["business_date"])
            or posted.get("feature_epoch") != int(manifest["feature_epoch"])
            or domain.get("aggregate_unchanged") is not True
            or domain.get("detail_parity") is not True
            or domain.get("aggregate_by_nm") != expected_aggregate
        ):
            mismatches.append("opening_document")
    aggregate_blockers: list[dict[str, Any]] = []
    current_aggregate = _aggregate_snapshot(conn, blockers=aggregate_blockers)
    expected_current_aggregate = [
        {
            "nm_id": nm_id,
            "quantity": quantity,
            "capital_rub": canonical_decimal_text(capital),
            "wac_rub": (
                None
                if quantity == 0
                else canonical_decimal_text(capital / Decimal(quantity))
            ),
        }
        for nm_id, (quantity, capital) in sorted(aggregate_by_nm.items())
    ]
    actual_aggregate_rows = list(current_aggregate["rows"])
    aggregate_unchanged = not aggregate_blockers and _exact_accounting_rows_equal(
        actual_aggregate_rows, expected_current_aggregate
    )
    if not aggregate_unchanged:
        mismatches.append("aggregate_post_backfill_drift")
    persisted_shipments = [
        {
            "shipment_id": str(item[0]),
            "invoice_no": str(item[1]),
            "classification": str(item[2]),
            "facility_id": str(item[3]),
            "pools": json.loads(str(item[4])),
            "expected_quantity": int(item[5]),
            "actual_ff_acceptance_date": str(item[6]),
            "receipt_operation_count": int(item[7]),
            "cost_layer_count": int(item[8]),
            "evidence_digest": str(item[9]),
            "post_cutover_state": str(item[10]),
            "guided_acceptance_required": bool(item[11]),
            "opening_quantity": 0,
            "opening_capital_rub": "0",
            "historical_fbs_debit_quantity": 0,
        }
        for item in conn.execute(
            f"""SELECT shipment_id,invoice_no,classification,facility_id,pools_json,
                       expected_quantity,actual_ff_acceptance_date,
                       receipt_operation_count,cost_layer_count,evidence_digest,
                       post_cutover_state,guided_acceptance_required
                FROM {PENDING_SHIPMENTS_TABLE}
                WHERE cutover_id=? ORDER BY shipment_id""",
            (identity,),
        ).fetchall()
    ]
    if persisted_shipments != list(manifest.get("china_shipments") or []):
        mismatches.append("excluded_pending_receipt_evidence")
    current_non_target = _bounded_non_target_snapshot(conn)
    if (
        current_non_target != dict(manifest.get("non_target") or {})
        or str(current_non_target["digest"]) != str(manifest["non_target_digest"])
    ) and not _known_post_cutover_non_target_growth(
        conn,
        cutover_id=identity,
        before=dict(manifest.get("non_target") or {}),
        current=current_non_target,
    ):
        mismatches.append("non_target_drift")
    return {
        "status": "pass" if not mismatches else "mismatch",
        "cutover_id": identity,
        "manifest_digest": str(row[0]),
        "feature_epoch": int(row[2]),
        "mismatches": mismatches,
        "aggregate_unchanged": aggregate_unchanged,
        "aggregate_expected": expected_current_aggregate,
        "aggregate_actual": actual_aggregate_rows,
        "fbs_drain": drain,
        "reader_enabled": True,
    }


def _exact_accounting_rows_equal(
    actual: list[dict[str, Any]], expected: list[dict[str, Any]]
) -> bool:
    """Compare exact accounting values without treating Decimal scale as drift."""

    if len(actual) != len(expected):
        return False
    for actual_row, expected_row in zip(actual, expected, strict=True):
        if int(actual_row["nm_id"]) != int(expected_row["nm_id"]):
            return False
        if int(actual_row["quantity"]) != int(expected_row["quantity"]):
            return False
        if Decimal(str(actual_row["capital_rub"])) != Decimal(
            str(expected_row["capital_rub"])
        ):
            return False
        actual_wac = actual_row.get("wac_rub")
        expected_wac = expected_row.get("wac_rub")
        if actual_wac is None or expected_wac is None:
            if actual_wac is not None or expected_wac is not None:
                return False
        elif Decimal(str(actual_wac)) != Decimal(str(expected_wac)):
            return False
    return True


def classify_late_pre_t_observations(
    conn: sqlite3.Connection, *, cutover_id: str, limit: int = 500
) -> dict[str, Any]:
    """Identify, without posting, observations that arrived after the checkpoint."""

    identity = _identifier(cutover_id, "cutover_id")
    bound = _exact_integer(limit, "limit", minimum=1)
    if bound > 500:
        raise FfPoolCutoverError("limit_too_large", "late_pre_t limit cannot exceed 500")
    manifest = conn.execute(
        f"SELECT manifest_json,observation_watermark_sequence FROM {MANIFESTS_TABLE} WHERE cutover_id=?",
        (identity,),
    ).fetchone()
    if manifest is None:
        raise FfPoolCutoverError("cutover_not_found", "cutover manifest was not found")
    manifest_json = json.loads(str(manifest[0]))
    boundary = _parse_timestamp(
        str(
            (manifest_json.get("accounting_boundary") or {}).get(
                "local_boundary_at"
            )
            or manifest_json["cutover_at"]
        ),
        "accounting_boundary_at",
    )
    rows = conn.execute(
        f"""SELECT observation_id,order_id,source_revision,source_created_at,observed_at
             FROM {OBSERVATIONS_TABLE} AS observation
             WHERE observation_sequence>?
               AND NOT EXISTS(
                 SELECT 1 FROM {ORDERS_TABLE} AS known
                 WHERE known.cutover_id=? AND known.order_id=observation.order_id
               )
             ORDER BY observation_sequence LIMIT ?""",
        (int(manifest[1]), identity, bound),
    ).fetchall()
    cases = []
    for row in rows:
        locally_observed = _parse_timestamp(str(row[4]), "observed_at")
        if locally_observed <= boundary:
            evidence = {
                "cutover_id": identity,
                "order_id": int(row[1]),
                "observation_id": str(row[0]),
                "source_revision": str(row[2]),
                "source_created_at": str(row[3]),
                "observed_at": str(row[4]),
            }
            cases.append(
                {
                    **evidence,
                    "state": "isolated",
                    "reason_code": "late_pre_t",
                    "display_reason": "Поздний заказ до границы",
                    "evidence_digest": _fingerprint(evidence),
                    "creates_debit": False,
                    "blocks_unrelated": False,
                }
            )
    return {
        "contract_name": CONTRACT_NAME,
        "cutover_id": identity,
        "count": len(cases),
        "cases": cases,
        "next_action": "exact_manual_reconciliation",
    }


def ff_pool_fbs_accounting_boundary_snapshot(
    conn: sqlite3.Connection,
    *,
    boundary_at: str,
    watermarks: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze or re-read the three independent append-only FBS streams.

    Order observations, status observations and status transitions each own an
    independent SQLite sequence.  ``W`` is therefore an exact vector, never a
    guessed scalar.  Rows appended above any component are deliberately outside
    the digest and cannot stale an already reviewed accounting boundary.
    """

    boundary = _utc_timestamp(boundary_at, field="accounting_boundary_at")
    requested = dict(watermarks or {})
    specs = (
        (
            "order_observation_watermark_sequence",
            OBSERVATIONS_TABLE,
            "observation_sequence",
            "observed_at",
        ),
        (
            "status_observation_watermark_sequence",
            STATUS_OBSERVATIONS_TABLE,
            "observation_sequence",
            "observed_at",
        ),
        (
            "status_transition_watermark_sequence",
            STATUS_TRANSITIONS_TABLE,
            "transition_sequence",
            "detected_at",
        ),
    )
    blockers: list[dict[str, Any]] = []
    vector: dict[str, int] = {}
    evidence: dict[str, Any] = {}
    for key, table, sequence_column, timestamp_column in specs:
        maximum = int(
            conn.execute(
                f"SELECT COALESCE(MAX({sequence_column}),0) FROM {table}"
            ).fetchone()[0]
        )
        watermark = (
            maximum
            if watermarks is None
            else _exact_integer(requested.get(key, 0), key, minimum=0)
        )
        vector[key] = watermark
        if watermark > maximum:
            blockers.append(
                {
                    "code": "fbs_frozen_watermark_ahead_of_source",
                    "stream": key,
                    "watermark": watermark,
                    "current_max": maximum,
                }
            )
        digest, count = _bounded_stream_digest(
            conn,
            table=table,
            sequence_column=sequence_column,
            watermark=watermark,
        )
        after_boundary = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table} "
                f"WHERE {sequence_column}<=? AND {timestamp_column}>?",
                (watermark, boundary),
            ).fetchone()[0]
        )
        if after_boundary:
            blockers.append(
                {
                    "code": "fbs_frozen_row_after_local_boundary",
                    "stream": key,
                    "count": after_boundary,
                }
            )
        evidence[key.removesuffix("_watermark_sequence")] = {
            "watermark_sequence": watermark,
            "row_count": count,
            "rows_digest": digest,
        }
    material = {
        "boundary_kind": "durable_local_observation_sequence_and_observed_at",
        "local_boundary_at": boundary,
        **vector,
        "frozen_streams": evidence,
    }
    return {
        **material,
        "frozen_evidence_digest": _fingerprint(material),
        "post_watermark_growth_invalidates_gate": False,
        "source_status_timestamp_available": False,
        "blockers": blockers,
    }


def _bounded_stream_digest(
    conn: sqlite3.Connection,
    *,
    table: str,
    sequence_column: str,
    watermark: int,
) -> tuple[str, int]:
    """Hash complete frozen rows without embedding the full stream in a plan."""

    columns = [
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    ]
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute(
        f"SELECT * FROM {table} WHERE {sequence_column}<=? ORDER BY {sequence_column}",
        (int(watermark),),
    ):
        payload = json.dumps(
            dict(zip(columns, tuple(row), strict=True)),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
        count += 1
    return "sha256:" + digest.hexdigest(), count


def ff_pool_cutover_preflight_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return bounded source facts required to prepare an external proposal."""

    schema = _schema_status(conn)
    if not schema["available"]:
        return {"status": "schema_absent", **schema}
    blockers: list[dict[str, Any]] = []
    aggregate = _aggregate_snapshot(conn, blockers=blockers)
    non_target = _bounded_non_target_snapshot(conn)
    maximum_observation = int(
        conn.execute(
            f"SELECT COALESCE(MAX(observation_sequence),0) FROM {OBSERVATIONS_TABLE}"
        ).fetchone()[0]
    )
    return {
        "status": "ready" if not blockers else "blocked",
        "blockers": blockers,
        "aggregate": aggregate,
        "active_facilities": sorted(_active_facilities(conn)),
        "target_feature_epoch": int(
            conn.execute(f"SELECT COALESCE(MAX(epoch),0)+1 FROM {FEATURE_EPOCHS_TABLE}").fetchone()[0]
        ),
        "observation_watermark_sequence": maximum_observation,
        "active_fbw_supplies": [
            {
                "wb_supply_cache_key": str(row[0] or row[1]),
                "wb_supply_id": str(row[1] or ""),
                "source_revision": _wb_supply_revision(_row_dict(row)),
            }
            for row in conn.execute(
                f"""SELECT cache_key,wb_supply_id,raw_list_hash,raw_detail_hash,
                            raw_goods_hash,raw_package_hash
                     FROM {WB_SUPPLIES_TABLE}
                     WHERE status_id IN ({','.join('?' for _ in ACTIVE_FBW_STATUS_IDS)})
                       AND CAST(COALESCE(wb_supply_id,'') AS INTEGER)>0
                     ORDER BY COALESCE(cache_key,supply_id),wb_supply_id
                     LIMIT ?""",
                (*ACTIVE_FBW_STATUS_IDS, MAX_FBW_ORIGINS + 1),
            ).fetchall()
        ],
        "non_target": non_target,
        "apply_surface_available": False,
    }


def apply_ff_pool_cutover(
    conn: sqlite3.Connection,
    *,
    proposal: Mapping[str, Any],
    deployed_sha: str,
    cutover_at: str,
    expected_manifest_digest: str,
    approval_reference: str,
    actor: str,
    crash: str = "",
    frozen_source_revalidator: Any | None = None,
) -> dict[str, Any]:
    """Apply one exact owner-gated cutover under an already-held T barrier.

    The caller owns the external HTTP/maintenance barrier.  This transaction
    owns the SQLite-local write epoch, signed opening, immutable checkpoint,
    exact historical backfill and parity evidence.  No WB endpoint is called.
    """

    _require_commit_sha(deployed_sha)
    expected = _sha256(expected_manifest_digest, "expected_manifest_digest")
    normalized = _normalize_proposal(proposal)
    if not normalized["handoff_policy"]["approved"]:
        raise FfPoolCutoverError(
            "owner_gate_required", "Production apply requires the approved complete/sorted policy"
        )
    if str(normalized["handoff_policy"]["approval_reference"]) != str(
        approval_reference or ""
    ).strip():
        raise FfPoolCutoverError(
            "approval_reference_mismatch", "The exact owner gate reference does not match"
        )
    existing = conn.execute(
        f"SELECT manifest_digest,deployed_sha,cutover_at,manifest_json FROM {MANIFESTS_TABLE} WHERE cutover_id=?",
        (normalized["cutover_id"],),
    ).fetchone()
    if existing is not None:
        stored_manifest = json.loads(str(existing[3]))
        if (
            str(existing[0]) != expected
            or str(existing[1]) != deployed_sha
            or str(existing[2]) != _utc_timestamp(cutover_at, field="cutover_at")
            or str(stored_manifest.get("proposal_digest") or "") != _fingerprint(normalized)
        ):
            raise FfPoolCutoverError(
                "manifest_conflict", "cutover_id already owns another exact manifest"
            )
        return {
            "contract_name": CONTRACT_NAME,
            "status": "already_applied",
            "manifest_digest": str(existing[0]),
            "idempotent": True,
            "mutates_wb": False,
        }
    now = _utc_timestamp(cutover_at, field="cutover_at")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if frozen_source_revalidator is not None:
            frozen_source_revalidator(conn)
        plan = build_ff_pool_cutover_plan(
            conn,
            proposal=proposal,
            deployed_sha=deployed_sha,
            cutover_at=cutover_at,
        )
        if plan["status"] != "ready" or not plan["apply_allowed"]:
            raise FfPoolCutoverError(
                "plan_not_ready", "Exact production plan is not applyable", details=plan["blockers"]
            )
        if str(plan["manifest_digest"]) != expected:
            raise FfPoolCutoverError(
                "manifest_fingerprint_mismatch",
                "Live T revalidation changed the exact manifest fingerprint",
                details={"expected": expected, "actual": plan["manifest_digest"]},
            )
        manifest = plan["manifest"]
        epoch_id = normalized["write_epoch_id"]
        epoch_digest = normalized["control_manifest_digest"]
        conn.execute(
            f"""INSERT INTO {WRITE_EPOCH_EVENTS_TABLE}(
                    epoch_id,phase,manifest_digest,deployed_sha,event_at,actor,details_json
                ) VALUES(?,?,?,?,?,?,?)""",
            (
                epoch_id,
                "applying",
                epoch_digest,
                deployed_sha,
                now,
                str(actor),
                json.dumps(
                    {
                        "cutover_manifest_digest": expected,
                        "approval_reference": approval_reference,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
        conn.execute(
            f"""INSERT INTO {FEATURE_EPOCHS_TABLE}(
                    epoch,writer_enabled,reader_enabled,source_revision,created_at,metadata_json
                ) VALUES(?,?,?,?,?,?)""",
            (
                int(manifest["feature_epoch"]),
                1,
                1,
                expected,
                now,
                json.dumps(
                    {
                        "cutover_id": manifest["cutover_id"],
                        "handoff_policy": manifest["handoff_policy"],
                        "default_off_before_cutover": True,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
        _apply_exact_opening(conn, plan=plan, actor=str(actor), now=now)
        _insert_cutover_manifest(conn, plan=plan, now=now)
        backfill = apply_opening_fbs_backfill(conn, manifest=manifest, occurred_at=now)
        post_checkpoint_delta = _drain_atomic_post_checkpoint_suffix(
            conn, manifest=manifest, occurred_at=now
        )
        if crash == "before_commit":
            raise RuntimeError("production-shaped crash before commit")
        transaction_readback = read_ff_pool_cutover_readback(
            conn, cutover_id=str(manifest["cutover_id"])
        )
        if transaction_readback["status"] != "pass":
            raise FfPoolCutoverError(
                "post_checkpoint_transaction_readback_failed",
                "Opening plus post-W delta did not pass exact transaction readback",
                details=transaction_readback,
            )
        aggregate_after = _aggregate_snapshot(conn, blockers=[])
        parity = evaluate_ff_pool_aggregate_parity(
            conn, aggregate_after["rows"]
        )
        if parity.status != "pass":
            raise FfPoolCutoverError(
                "post_backfill_parity_failed",
                "Facility/pool and aggregate FF diverged after exact backfill",
                details={"mismatched_nm_ids": list(parity.mismatched_nm_ids)},
            )
        record_ff_pool_parity_diagnostic(
            conn,
            diagnostic_id="ffpar_" + expected.removeprefix("sha256:")[:28],
            aggregate_revision=str(manifest["aggregate_revision"]),
            checked_at=now,
            result=parity,
            details={
                "cutover_id": manifest["cutover_id"],
                "opening_source_conserved": True,
                "historical_backfill": backfill,
            },
        )
        conn.execute(
            f"""INSERT INTO {RECOVERY_EVENTS_TABLE}(
                    cutover_id,event_type,event_at,evidence_digest,details_json
                ) VALUES(?,?,?,?,?)""",
            (
                manifest["cutover_id"],
                "applied",
                now,
                expected,
                json.dumps(backfill, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ),
        )
        conn.execute(
            f"""INSERT INTO {WRITE_EPOCH_EVENTS_TABLE}(
                    epoch_id,phase,manifest_digest,deployed_sha,event_at,actor,details_json
                ) VALUES(?,?,?,?,?,?,?)""",
            (
                epoch_id,
                "readback_required",
                epoch_digest,
                deployed_sha,
                now,
                str(actor),
                json.dumps({"cutover_manifest_digest": expected}, separators=(",", ":")),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if crash == "after_commit":
        raise FfPoolCutoverAmbiguousCommit("commit succeeded; exact readback is mandatory")
    readback = read_ff_pool_cutover_readback(
        conn, cutover_id=str(normalized["cutover_id"])
    )
    if readback["status"] != "pass":
        raise FfPoolCutoverAmbiguousCommit(
            "commit completed but exact readback requires forward recovery"
        )
    return {
        "contract_name": CONTRACT_NAME,
        "status": "applied_readback_required",
        "manifest_digest": expected,
        "cutover_at": now,
        "backfill": backfill,
        "post_checkpoint_delta": post_checkpoint_delta,
        "readback": readback,
        "idempotent": False,
        "mutates_wb": False,
    }


def _drain_atomic_post_checkpoint_suffix(
    conn: sqlite3.Connection,
    *,
    manifest: Mapping[str, Any],
    occurred_at: str,
) -> dict[str, Any]:
    """Consume the finite suffix visible under the opening write lock."""

    aggregate_summary: dict[str, int] = {}
    first_sequence: int | None = None
    processed = 0
    result: dict[str, Any] | None = None
    for batch_no in range(1, MAX_POST_CHECKPOINT_DRAIN_BATCHES + 1):
        result = drain_post_checkpoint_fbs_lifecycle(
            conn,
            manifest=manifest,
            occurred_at=occurred_at,
            limit=POST_CHECKPOINT_DRAIN_BATCH_SIZE,
        )
        if first_sequence is None:
            first_sequence = int(result["from_status_observation_sequence"])
        processed += int(result["processed_count"])
        for key, value in dict(result["summary"]).items():
            aggregate_summary[str(key)] = aggregate_summary.get(str(key), 0) + int(value)
        if result["status"] == "caught_up":
            material = {
                "cutover_id": str(manifest["cutover_id"]),
                "from_status_observation_sequence": int(first_sequence),
                "last_status_observation_sequence": int(
                    result["last_status_observation_sequence"]
                ),
                "processed_count": processed,
                "pending_count": 0,
                "batch_count": batch_no,
                "summary": aggregate_summary,
            }
            return {
                "contract_name": str(result["contract_name"]),
                "status": "caught_up",
                **material,
                "result_digest": _fingerprint(material),
                "mutates_wb": False,
            }
    raise FfPoolCutoverError(
        "post_checkpoint_delta_not_drained",
        "Post-W FBS delta exceeded the bounded atomic drain",
        details={
            "batch_size": POST_CHECKPOINT_DRAIN_BATCH_SIZE,
            "batch_count": MAX_POST_CHECKPOINT_DRAIN_BATCHES,
            "processed_count": processed,
            "last_result": result,
        },
    )


def _apply_exact_opening(
    conn: sqlite3.Connection,
    *,
    plan: Mapping[str, Any],
    actor: str,
    now: str,
) -> None:
    manifest = dict(plan["manifest"])
    digest = str(plan["manifest_digest"])
    epoch = int(manifest["feature_epoch"])
    request_id = str(manifest["opening_document_id"])
    operation_id = "ffbo_open_" + digest.removeprefix("sha256:")[:28]
    posted_manifest = {
        "contract_name": "ff_pool_exact_opening_v1",
        "feature_epoch": epoch,
        "allocations": manifest["allocations"],
        "domain": {
            "aggregate_unchanged": True,
            "detail_parity": True,
            "aggregate_by_nm": {
                str(item["nm_id"]): {
                    "quantity": int(item["quantity"]),
                    "capital_rub": canonical_decimal_text(item["capital_rub"]),
                }
                for item in manifest["aggregate_rows"]
            },
            "signed_integer_quantity": True,
            "exact_decimal_text_capital": True,
        },
    }
    posted_json = json.dumps(
        posted_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    posted_digest = _fingerprint(posted_manifest)
    conn.execute(
        f"""INSERT INTO {REQUESTS_TABLE}(
                request_id,request_identity,client_request_id,document_kind,state,
                source_system,source_type,source_id,source_revision,idempotency_epoch,
                actor,business_date,source_filename,source_content_type,source_sha256,
                source_file_blob,template_fingerprint,request_payload_json,
                preview_manifest_json,posted_manifest_sha256,posted_document_id,
                accepted_at,started_at,ready_at,posted_at,completed_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            request_id,
            digest,
            request_id,
            "facility_pool_opening",
            "complete",
            "ff_pool_cutover",
            "cutover_manifest",
            str(manifest["cutover_id"]),
            digest,
            epoch,
            actor,
            str(manifest["business_date"]),
            "",
            "application/json",
            "",
            None,
            digest,
            posted_json,
            posted_json,
            posted_digest,
            request_id,
            now,
            now,
            now,
            now,
            now,
            now,
        ),
    )
    conn.execute(
        f"""INSERT INTO {OPERATIONS_TABLE}(
                operation_id,operation_type,source_system,source_type,source_id,
                source_revision,idempotency_epoch,business_date,posted_at,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            operation_id,
            "facility_pool_opening",
            "ff_pool_cutover",
            "cutover_manifest",
            str(manifest["cutover_id"]),
            digest,
            epoch,
            str(manifest["business_date"]),
            now,
            json.dumps(
                {"signed_integer_quantity": True, "exact_decimal_text_capital": True},
                separators=(",", ":"),
            ),
        ),
    )
    for line in manifest["allocations"]:
        conn.execute(
            f"""INSERT INTO {LINES_TABLE}(
                    operation_id,line_no,facility_id,pool,nm_id,quantity_delta,
                    capital_delta_rub,wac_snapshot_rub,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                operation_id,
                int(line["line_no"]),
                str(line["facility_id"]),
                str(line["pool"]),
                int(line["nm_id"]),
                int(line["quantity"]),
                canonical_decimal_text(line["capital_rub"]),
                None if line["wac_rub"] is None else canonical_decimal_text(line["wac_rub"]),
                json.dumps(
                    {"allocation_digest": line["allocation_digest"]}, separators=(",", ":")
                ),
            ),
        )
        conn.execute(
            f"""INSERT INTO {BALANCES_TABLE}(
                    facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,
                    wac_rub,source_watermark,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                str(line["facility_id"]),
                str(line["pool"]),
                int(line["nm_id"]),
                epoch,
                int(line["quantity"]),
                canonical_decimal_text(line["capital_rub"]),
                None if line["wac_rub"] is None else canonical_decimal_text(line["wac_rub"]),
                digest,
                now,
            ),
        )
    conn.execute(
        f"""INSERT INTO {DOCUMENTS_TABLE}(
                document_id,request_id,document_role,document_kind,root_document_id,
                operation_id,source_system,source_type,source_id,source_revision,
                idempotency_epoch,actor,business_date,source_filename,
                source_content_type,source_sha256,template_fingerprint,
                posted_manifest_sha256,posted_manifest_json,posted_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            request_id,
            request_id,
            "root",
            "facility_pool_opening",
            request_id,
            operation_id,
            "ff_pool_cutover",
            "cutover_manifest",
            str(manifest["cutover_id"]),
            digest,
            epoch,
            actor,
            str(manifest["business_date"]),
            "",
            "application/json",
            "",
            digest,
            posted_digest,
            posted_json,
            now,
        ),
    )


def _apply_ff_pool_cutover_fixture(
    conn: sqlite3.Connection,
    *,
    proposal: Mapping[str, Any],
    deployed_sha: str,
    cutover_at: str,
    crash: str = "",
) -> dict[str, Any]:
    """Exercise the production-shaped transaction only in marked test DBs."""

    marker = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (FIXTURE_MARKER_TABLE,)
    ).fetchone()
    if marker is None:
        raise FfPoolCutoverError(
            "fixture_marker_required", "The fixture wrapper requires its explicit marker"
        )
    normalized = _normalize_proposal(proposal)
    existing = conn.execute(
        f"SELECT manifest_digest FROM {MANIFESTS_TABLE} WHERE cutover_id=?",
        (normalized["cutover_id"],),
    ).fetchone()
    if existing is not None:
        result = apply_ff_pool_cutover(
            conn,
            proposal=proposal,
            deployed_sha=deployed_sha,
            cutover_at=cutover_at,
            expected_manifest_digest=str(existing[0]),
            approval_reference=str(normalized["handoff_policy"]["approval_reference"]),
            actor="stage7c_fixture",
            crash=crash,
        )
        return {**result, "status": "already_applied"}
    plan = build_ff_pool_cutover_plan(
        conn, proposal=proposal, deployed_sha=deployed_sha, cutover_at=cutover_at
    )
    if plan["status"] != "ready":
        raise FfPoolCutoverError(
            "plan_not_ready", "Fixture apply requires an exact ready plan", details=plan["blockers"]
        )
    result = apply_ff_pool_cutover(
        conn,
        proposal=proposal,
        deployed_sha=deployed_sha,
        cutover_at=cutover_at,
        expected_manifest_digest=str(plan["manifest_digest"]),
        approval_reference=str(
            normalized["handoff_policy"]["approval_reference"]
        ),
        actor="stage7c_fixture",
        crash=crash,
    )
    return {
        **result,
        "status": "applied_fixture" if not result["idempotent"] else "already_applied",
    }


def _insert_cutover_manifest(conn: sqlite3.Connection, *, plan: Mapping[str, Any], now: str) -> None:
    manifest = plan["manifest"]
    collector = manifest["collector_checkpoint"]
    conn.execute(
        f"""INSERT INTO {MANIFESTS_TABLE}(
            cutover_id,manifest_digest,deployed_sha,cutover_at,business_date,feature_epoch,
            aggregate_revision,aggregate_digest,detail_digest,observation_watermark_sequence,
            observation_watermark_digest,mapping_digest,fbw_origins_digest,control_evidence_digest,
            non_target_digest,opening_document_id,source_snapshot_digest,created_at,manifest_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            manifest["cutover_id"], plan["manifest_digest"], manifest["deployed_sha"], manifest["cutover_at"],
            manifest["business_date"], manifest["feature_epoch"], manifest["aggregate_revision"],
            manifest["aggregate_digest"], manifest["detail_digest"], manifest["observation_watermark_sequence"],
            manifest["observation_watermark_digest"], manifest["mapping_digest"], manifest["fbw_origins_digest"],
            manifest["control_evidence_digest"], manifest["non_target_digest"], manifest["opening_document_id"],
            manifest["source_snapshot_digest"], now, json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ),
    )
    for row in manifest["allocations"]:
        conn.execute(
            f"INSERT INTO {ALLOCATIONS_TABLE} VALUES(?,?,?,?,?,?,?,?,?)",
            (
                manifest["cutover_id"], row["line_no"], row["facility_id"], row["pool"], row["nm_id"],
                row["quantity"], row["capital_rub"], row["wac_rub"], row["allocation_digest"],
            ),
        )
    for row in manifest["order_classifications"]:
        conn.execute(
            f"""INSERT INTO {ORDERS_TABLE}(
                    cutover_id,order_id,observation_sequence,
                    status_observation_sequence,observation_id,source_revision,
                    source_created_at,observed_at,classification,facility_id,pool,
                    nm_id,quantity,status_fingerprint,mapping_digest
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                manifest["cutover_id"], row["order_id"], row["observation_sequence"],
                row["status_observation_sequence"], row["observation_id"], row["source_revision"],
                row["source_created_at"], row["observed_at"], row["classification"], row["facility_id"],
                row["pool"], row["nm_id"], row["quantity"], row["status_fingerprint"], row["mapping_digest"],
            ),
        )
        if row["classification"] == "pre_t_absorbed_reservation":
            matching = next(
                item for item in manifest["allocations"]
                if item["facility_id"] == row["facility_id"] and item["pool"] == "FBS" and item["nm_id"] == row["nm_id"]
            )
            wac = str(matching["wac_rub"] or "0")
            capital = canonical_decimal_text(Decimal(wac) * Decimal(int(row["quantity"])))
            conn.execute(
                f"INSERT INTO {OPENING_RESERVATIONS_TABLE} VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    manifest["cutover_id"], row["order_id"], row["facility_id"], "FBS", row["nm_id"],
                    row["quantity"], wac, capital, row["source_revision"], "opening_absorbed",
                ),
            )
        lifecycle_class = (
            "active_pre_handoff"
            if row["classification"] == "pre_t_absorbed_reservation"
            else "closed_pre_handoff"
        )
        for status_evidence in (
            row.get("status_evidence"),
            row.get("post_handoff_reconciliation"),
        ):
            if not status_evidence:
                continue
            conn.execute(
                f"""INSERT OR IGNORE INTO {STATUS_EVIDENCE_TABLE}(
                        order_id,source_revision,evidence_digest,lifecycle_class,
                        quantity,observed_at,evidence_source
                    ) VALUES(?,?,?,?,?,?,'official_wb_status_shadow')""",
                (
                    int(row["order_id"]),
                    str(status_evidence["source_revision"]),
                    str(status_evidence["status_digest"]),
                    lifecycle_class,
                    int(status_evidence["quantity"]),
                    str(status_evidence["observed_at"]),
                ),
            )
    for index, row in enumerate(manifest["fbw_origin_assignments"], 1):
        conn.execute(
            f"INSERT INTO {FBW_ORIGINS_TABLE} VALUES(?,?,?,?,?,?,?)",
            (
                manifest["cutover_id"], row["wb_supply_cache_key"], row["wb_supply_id"],
                row["source_revision"], row["facility_id"], "FBO", row["evidence_digest"],
            ),
        )
        conn.execute(
            f"""INSERT INTO {ASSIGNMENTS_TABLE}(
                assignment_id,request_id,request_fingerprint,wb_supply_cache_key,wb_supply_id,
                source_revision,feature_epoch,facility_id,pool,supersedes_assignment_id,actor,reason,assigned_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"cutover_origin_{index}_{manifest['cutover_id']}", f"cutover_origin_request_{index}_{manifest['cutover_id']}",
                row["evidence_digest"], row["wb_supply_cache_key"], row["wb_supply_id"], row["source_revision"],
                manifest["feature_epoch"], row["facility_id"], "FBO", None, "stage7c_cutover", "opening manifest", now,
            ),
        )
    for row in manifest["china_shipments"]:
        conn.execute(
            f"""INSERT INTO {PENDING_SHIPMENTS_TABLE}(
                    cutover_id,shipment_id,invoice_no,classification,facility_id,
                    pools_json,expected_quantity,actual_ff_acceptance_date,
                    receipt_operation_count,cost_layer_count,evidence_digest,
                    post_cutover_state,guided_acceptance_required
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                manifest["cutover_id"], row["shipment_id"], row["invoice_no"],
                "excluded_pending_receipt", row["facility_id"],
                json.dumps(row["pools"], separators=(",", ":")),
                row["expected_quantity"], row["actual_ff_acceptance_date"],
                row["receipt_operation_count"], row["cost_layer_count"],
                row["evidence_digest"], "in_transit", 1,
            ),
        )
    conn.execute(
        f"""INSERT INTO {CHECKPOINTS_TABLE}(
                cutover_id,cutover_at,accounting_boundary_at,feature_epoch,
                observation_watermark_sequence,
                status_observation_watermark_sequence,
                status_transition_watermark_sequence,
                observation_watermark_digest,frozen_evidence_digest,
                collector_window_from,collector_window_to,collector_next_cursor,
                collector_complete,checkpoint_digest,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            manifest["cutover_id"], manifest["cutover_at"],
            manifest["accounting_boundary"]["local_boundary_at"],
            manifest["feature_epoch"], manifest["observation_watermark_sequence"],
            manifest["status_observation_watermark_sequence"],
            manifest["status_transition_watermark_sequence"],
            manifest["observation_watermark_digest"],
            manifest["accounting_boundary"]["frozen_evidence_digest"],
            0,
            0,
            0,
            0,
            collector["checkpoint_digest"], now,
        ),
    )


def _normalize_proposal(proposal: Mapping[str, Any]) -> dict[str, Any]:
    if str(proposal.get("contract_name") or "") != PROPOSAL_CONTRACT:
        raise FfPoolCutoverError("invalid_contract", f"contract_name must be {PROPOSAL_CONTRACT}")
    cutover_id = _identifier(proposal.get("cutover_id"), "cutover_id")
    business_date = str(proposal.get("business_date") or "")
    try:
        if datetime.fromisoformat(business_date).date().isoformat() != business_date:
            raise ValueError
    except ValueError as exc:
        raise FfPoolCutoverError("invalid_business_date", "business_date must be YYYY-MM-DD") from exc
    target_epoch = _positive_int(proposal.get("target_feature_epoch"), "target_feature_epoch")
    write_epoch_id = _identifier(proposal.get("write_epoch_id"), "write_epoch_id")
    control_manifest_digest = _sha256(proposal.get("control_manifest_digest"), "control_manifest_digest")
    non_target_digest = _sha256(proposal.get("non_target_evidence_digest"), "non_target_evidence_digest")
    controls = _mapping(proposal.get("control_evidence"), "control_evidence")
    control_evidence = {
        "maintenance_quiet": _boolean(controls.get("maintenance_quiet"), "maintenance_quiet"),
        "http_write_barrier_active": _boolean(
            controls.get("http_write_barrier_active"), "http_write_barrier_active"
        ),
        "warehouse_timer_held": _boolean(
            controls.get("warehouse_timer_held"), "warehouse_timer_held"
        ),
        "warehouse_lock_held": _boolean(
            controls.get("warehouse_lock_held"), "warehouse_lock_held"
        ),
        "evidence_digest": _sha256(controls.get("evidence_digest"), "control_evidence.evidence_digest"),
    }
    raw_policy = _mapping(proposal.get("handoff_policy"), "handoff_policy")
    if str(raw_policy.get("supplier_status") or "") != "complete":
        raise FfPoolCutoverError(
            "invalid_handoff_supplier_status",
            "The reviewed handoff policy requires supplierStatus=complete",
        )
    if str(raw_policy.get("wb_status") or "") != "sorted":
        raise FfPoolCutoverError(
            "invalid_handoff_wb_status",
            "The reviewed WB-controlled handoff policy requires wbStatus=sorted",
        )
    decision = str(raw_policy.get("decision") or "")
    if decision not in {"proposed", "approved"}:
        raise FfPoolCutoverError(
            "invalid_handoff_decision", "handoff_policy.decision must be proposed or approved"
        )
    approval_reference = str(raw_policy.get("approval_reference") or "").strip()
    if decision == "approved" and not approval_reference:
        raise FfPoolCutoverError(
            "handoff_approval_reference_required",
            "Approved complete/sorted policy requires the exact owner gate reference",
        )
    handoff_policy = {
        "decision": decision,
        "approved": decision == "approved",
        "supplier_status": "complete",
        "wb_status": "sorted",
        "approval_reference": approval_reference,
        "observed_complete_waiting_to_complete_sorted_distinct_orders": _exact_integer(
            raw_policy.get("observed_complete_waiting_to_complete_sorted_distinct_orders", 0),
            "handoff_policy.observed_complete_waiting_to_complete_sorted_distinct_orders",
            minimum=0,
        ),
        "automatic_without_owner_gate": False,
    }
    return {
        "contract_name": PROPOSAL_CONTRACT,
        "cutover_id": cutover_id,
        "business_date": business_date,
        "target_feature_epoch": target_epoch,
        "write_epoch_id": write_epoch_id,
        "control_manifest_digest": control_manifest_digest,
        "control_evidence": control_evidence,
        "handoff_policy": handoff_policy,
        "allocations": _bounded_mapping_list(proposal.get("allocations"), "allocations", MAX_ALLOCATIONS),
        "order_classifications": _bounded_mapping_list(
            proposal.get("order_classifications"), "order_classifications", MAX_ORDERS
        ),
        "seller_warehouse_mappings": _bounded_mapping_list(
            proposal.get("seller_warehouse_mappings"), "seller_warehouse_mappings", MAX_MAPPINGS
        ),
        "sku_mappings": _bounded_mapping_list(
            proposal.get("sku_mappings"), "sku_mappings", MAX_MAPPINGS
        ),
        "fbw_origin_assignments": _bounded_mapping_list(
            proposal.get("fbw_origin_assignments"), "fbw_origin_assignments", MAX_FBW_ORIGINS
        ),
        "china_shipments": _bounded_mapping_list(
            proposal.get("china_shipments", []), "china_shipments", MAX_CHINA_SHIPMENTS
        ),
        "collector_checkpoint": dict(_mapping(proposal.get("collector_checkpoint"), "collector_checkpoint")),
        "non_target_evidence_digest": non_target_digest,
    }


def _schema_status(conn: sqlite3.Connection) -> dict[str, Any]:
    required = {
        MANIFESTS_TABLE,
        ALLOCATIONS_TABLE,
        ORDERS_TABLE,
        STATUS_EVIDENCE_TABLE,
        FBW_ORIGINS_TABLE,
        CHECKPOINTS_TABLE,
        OPENING_RESERVATIONS_TABLE,
        LATE_PRE_T_TABLE,
        RECOVERY_EVENTS_TABLE,
        WRITE_EPOCH_EVENTS_TABLE,
        FACILITIES_TABLE,
        FEATURE_EPOCHS_TABLE,
        BALANCES_TABLE,
        OBSERVATIONS_TABLE,
        STATUS_OBSERVATIONS_TABLE,
        STATUS_CURRENT_TABLE,
        FBS_COLLECTOR_STATE_TABLE,
        WB_SUPPLIES_TABLE,
        FUNCTIONAL_ACTIVE_TABLE,
        FUNCTIONAL_BALANCES_TABLE,
    }
    existing = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    missing = sorted(required - existing)
    return {"available": not missing, "missing_tables": missing}


def _aggregate_snapshot(
    conn: sqlite3.Connection, *, blockers: list[dict[str, Any]]
) -> dict[str, Any]:
    active = conn.execute(
        f"SELECT version_id FROM {FUNCTIONAL_ACTIVE_TABLE} WHERE slot=1"
    ).fetchone()
    if active is None:
        blockers.append({"code": "aggregate_functional_version_absent"})
        return {
            "revision": "",
            "rows": [],
            "digest": _fingerprint([]),
            "total_quantity": 0,
            "total_capital_rub": "0",
        }
    revision = str(active[0])
    rows: list[dict[str, Any]] = []
    total_quantity = 0
    total_capital = ZERO
    for row in conn.execute(
        f"""SELECT nm_id,quantity,capital_rub,wac_rub
             FROM {FUNCTIONAL_BALANCES_TABLE}
             WHERE version_id=? AND warehouse_key=? ORDER BY nm_id""",
        (revision, FF_STAGE),
    ).fetchall():
        nm_id = _positive_int(row[0], "aggregate.nm_id")
        quantity = _exact_integer(row[1], f"aggregate[{nm_id}].quantity", minimum=None)
        capital = _decimal(row[2], f"aggregate[{nm_id}].capital_rub")
        expected_wac = None if quantity == 0 else canonical_decimal_text(capital / Decimal(quantity))
        source_wac = None if row[3] in (None, "") else canonical_decimal_text(_decimal(row[3], "wac"))
        if source_wac is not None and expected_wac is not None:
            if abs(Decimal(source_wac) - Decimal(expected_wac)) > Decimal("0.000001"):
                blockers.append({"code": "aggregate_wac_inconsistent", "nm_id": nm_id})
        item = {
            "nm_id": nm_id,
            "quantity": quantity,
            "capital_rub": canonical_decimal_text(capital),
            "wac_rub": expected_wac,
        }
        rows.append(item)
        total_quantity += quantity
        total_capital += capital
    return {
        "revision": revision,
        "rows": rows,
        "digest": _fingerprint({"revision": revision, "rows": rows}),
        "total_quantity": total_quantity,
        "total_capital_rub": canonical_decimal_text(total_capital),
    }


def _allocation_snapshot(
    values: list[dict[str, Any]],
    *,
    aggregate_rows: list[dict[str, Any]],
    facilities: set[str],
    blockers: list[dict[str, Any]],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    keys: set[tuple[str, str, int]] = set()
    for index, raw in enumerate(values, 1):
        facility_id = _identifier(raw.get("facility_id"), f"allocations[{index}].facility_id", maximum=80)
        pool = str(raw.get("pool") or "")
        if pool not in POOLS:
            raise FfPoolCutoverError("invalid_pool", f"allocations[{index}].pool must be FBS or FBO")
        nm_id = _positive_int(raw.get("nm_id"), f"allocations[{index}].nm_id")
        quantity = _exact_integer(raw.get("quantity"), f"allocations[{index}].quantity", minimum=None)
        capital = _decimal(raw.get("capital_rub"), f"allocations[{index}].capital_rub")
        if quantity == 0:
            raise FfPoolCutoverError(
                "zero_allocation_not_materialized",
                "Zero allocation rows must be omitted from the exact opening manifest",
            )
        if facility_id not in facilities:
            blockers.append({"code": "allocation_facility_inactive_or_missing", "facility_id": facility_id})
        key = (facility_id, pool, nm_id)
        if key in keys:
            raise FfPoolCutoverError("duplicate_allocation", f"duplicate allocation {key}")
        keys.add(key)
        rows.append(
            {
                "line_no": index,
                "facility_id": facility_id,
                "pool": pool,
                "nm_id": nm_id,
                "quantity": quantity,
                "capital_rub": canonical_decimal_text(capital),
                "wac_rub": None if quantity == 0 else canonical_decimal_text(capital / Decimal(quantity)),
                "allocation_digest": _fingerprint(raw),
            }
        )
    rows.sort(key=lambda item: (item["nm_id"], item["facility_id"], item["pool"]))
    for index, item in enumerate(rows, 1):
        item["line_no"] = index
    aggregate_by_nm = {int(row["nm_id"]): row for row in aggregate_rows}
    detail_by_nm: dict[int, tuple[int, Decimal]] = {}
    for row in rows:
        quantity, capital = detail_by_nm.get(int(row["nm_id"]), (0, ZERO))
        detail_by_nm[int(row["nm_id"])] = (
            quantity + int(row["quantity"]),
            capital + Decimal(str(row["capital_rub"])),
        )
    for nm_id in sorted(set(aggregate_by_nm) | set(detail_by_nm)):
        aggregate = aggregate_by_nm.get(nm_id, {"quantity": 0, "capital_rub": "0"})
        detail = detail_by_nm.get(nm_id, (0, ZERO))
        if int(aggregate["quantity"]) != detail[0]:
            blockers.append({"code": "allocation_quantity_mismatch", "nm_id": nm_id})
        if Decimal(str(aggregate["capital_rub"])) != detail[1]:
            blockers.append({"code": "allocation_capital_mismatch", "nm_id": nm_id})
    aggregate_qty = sum(int(row["quantity"]) for row in aggregate_rows)
    detail_qty = sum(int(row["quantity"]) for row in rows)
    aggregate_capital = sum((Decimal(str(row["capital_rub"])) for row in aggregate_rows), ZERO)
    detail_capital = sum((Decimal(str(row["capital_rub"])) for row in rows), ZERO)
    return {
        "rows": rows,
        "digest": _fingerprint(rows),
        "total_quantity": detail_qty,
        "total_capital_rub": canonical_decimal_text(detail_capital),
        "quantity_matches": aggregate_qty == detail_qty,
        "capital_matches": aggregate_capital == detail_capital,
    }


def _mapping_snapshot(
    normalized: Mapping[str, Any], *, facilities: set[str], blockers: list[dict[str, Any]]
) -> dict[str, Any]:
    warehouses: list[dict[str, Any]] = []
    warehouse_ids: set[int] = set()
    for raw in normalized["seller_warehouse_mappings"]:
        warehouse_id = _positive_int(raw.get("warehouse_id"), "seller_warehouse_mappings.warehouse_id")
        facility_id = _identifier(raw.get("facility_id"), "seller_warehouse_mappings.facility_id", maximum=80)
        evidence = _sha256(raw.get("evidence_digest"), "seller_warehouse_mappings.evidence_digest")
        if warehouse_id in warehouse_ids:
            raise FfPoolCutoverError("duplicate_warehouse_mapping", str(warehouse_id))
        warehouse_ids.add(warehouse_id)
        if facility_id not in facilities:
            blockers.append({"code": "warehouse_mapping_facility_missing", "warehouse_id": warehouse_id})
        warehouses.append({"warehouse_id": warehouse_id, "facility_id": facility_id, "evidence_digest": evidence})
    skus: list[dict[str, Any]] = []
    sku_keys: set[tuple[int, int]] = set()
    for raw in normalized["sku_mappings"]:
        nm_id = _positive_int(
            raw.get("source_nm_id", raw.get("nm_id")), "sku_mappings.source_nm_id"
        )
        target_nm_id = _positive_int(
            raw.get("target_nm_id", nm_id), "sku_mappings.target_nm_id"
        )
        chrt_id = _positive_int(raw.get("chrt_id"), "sku_mappings.chrt_id")
        identity = _sha256(raw.get("identity_digest"), "sku_mappings.identity_digest")
        key = (nm_id, chrt_id)
        if key in sku_keys:
            raise FfPoolCutoverError("duplicate_sku_mapping", str(key))
        sku_keys.add(key)
        skus.append(
            {
                "nm_id": nm_id,
                "source_nm_id": nm_id,
                "target_nm_id": target_nm_id,
                "chrt_id": chrt_id,
                "identity_digest": identity,
            }
        )
    warehouses.sort(key=lambda row: row["warehouse_id"])
    skus.sort(key=lambda row: (row["nm_id"], row["chrt_id"]))
    return {
        "seller_warehouse_mappings": warehouses,
        "sku_mappings": skus,
        "warehouse_map": {int(row["warehouse_id"]): row for row in warehouses},
        "sku_map": {(int(row["nm_id"]), int(row["chrt_id"])): row for row in skus},
        "digest": _fingerprint({"warehouses": warehouses, "skus": skus}),
    }


def _observation_snapshot(
    conn: sqlite3.Connection,
    *,
    normalized: Mapping[str, Any],
    accounting_boundary_at: str,
    mappings: Mapping[str, Any],
    blockers: list[dict[str, Any]],
) -> dict[str, Any]:
    checkpoint = normalized["collector_checkpoint"]
    watermark = _exact_integer(
        checkpoint.get("observation_watermark_sequence", 0),
        "collector_checkpoint.observation_watermark_sequence",
        minimum=0,
    )
    status_watermark = _exact_integer(
        checkpoint.get(
            "status_observation_watermark_sequence",
            conn.execute(
                f"SELECT COALESCE(MAX(observation_sequence),0) FROM {STATUS_OBSERVATIONS_TABLE}"
            ).fetchone()[0],
        ),
        "collector_checkpoint.status_observation_watermark_sequence",
        minimum=0,
    )
    actual_max = int(conn.execute(f"SELECT COALESCE(MAX(observation_sequence),0) FROM {OBSERVATIONS_TABLE}").fetchone()[0])
    if watermark > actual_max:
        blockers.append(
            {
                "code": "observation_watermark_ahead_of_source",
                "expected_max": actual_max,
                "actual": watermark,
            }
        )
    rows = conn.execute(
        f"""SELECT observation_sequence,observation_id,order_id,source_revision,
                    source_created_at,observed_at,warehouse_id,nm_id,chrt_id,skus_json
             FROM {OBSERVATIONS_TABLE} AS source
             WHERE observation_sequence<=?
               AND observation_sequence=(
                 SELECT MAX(latest.observation_sequence) FROM {OBSERVATIONS_TABLE} AS latest
                 WHERE latest.order_id=source.order_id AND latest.observation_sequence<=?
               )
             ORDER BY order_id""",
        (watermark, watermark),
    ).fetchall()
    proposed = {int(item.get("order_id")): item for item in normalized["order_classifications"]}
    if len(proposed) != len(normalized["order_classifications"]):
        raise FfPoolCutoverError("duplicate_order_classification", "order_id must be unique")
    classifications: list[dict[str, Any]] = []
    seen: set[int] = set()
    boundary_dt = _parse_timestamp(
        accounting_boundary_at, "accounting_boundary_at"
    )
    for row in rows:
        order_id = int(row[2])
        seen.add(order_id)
        raw = proposed.get(order_id)
        if raw is None:
            blockers.append({"code": "order_unclassified", "order_id": order_id})
            continue
        classification = str(raw.get("classification") or "")
        if classification not in ORDER_CLASSES:
            raise FfPoolCutoverError("invalid_order_classification", str(order_id))
        observed = _parse_timestamp(
            str(row[5] or ""), f"order[{order_id}].observed_at"
        )
        is_pre_t = observed <= boundary_dt
        if is_pre_t and classification not in PRE_T_CLASSES:
            blockers.append({"code": "pre_t_order_not_absorbed", "order_id": order_id})
        if not is_pre_t and classification != "post_t_deferred":
            blockers.append({"code": "post_t_order_wrong_class", "order_id": order_id})
        facility_id = str(raw.get("facility_id") or "")
        if classification == "unmatched":
            facility_id = ""
        warehouse_id = int(row[6]) if row[6] is not None else 0
        warehouse_mapping = mappings["warehouse_map"].get(warehouse_id)
        if classification != "unmatched":
            if warehouse_mapping is None or warehouse_mapping["facility_id"] != facility_id:
                blockers.append({"code": "order_warehouse_mapping_mismatch", "order_id": order_id})
            sku_mapping = mappings["sku_map"].get((int(row[7]), int(row[8] or 0)))
            expected_identity = _fingerprint(
                {"nm_id": int(row[7]), "chrt_id": int(row[8] or 0), "skus": json.loads(str(row[9]))}
            )
            if sku_mapping is None or sku_mapping["identity_digest"] != expected_identity:
                blockers.append({"code": "order_sku_mapping_mismatch", "order_id": order_id})
        quantity = _exact_integer(raw.get("quantity", 1), f"order[{order_id}].quantity", minimum=1)
        status_fp = _sha256(raw.get("status_fingerprint"), f"order[{order_id}].status_fingerprint")
        evidence = _official_order_status_evidence(
            conn,
            order_id=order_id,
            cutover_at=accounting_boundary_at,
            classification=classification,
            max_observation_sequence=status_watermark,
        )
        if evidence is None:
            blockers.append({"code": "official_status_evidence_missing", "order_id": order_id})
        else:
            if str(evidence["status_digest"]) != status_fp:
                blockers.append({"code": "official_status_evidence_stale", "order_id": order_id})
            if int(evidence["quantity"]) != quantity:
                blockers.append({"code": "official_status_quantity_mismatch", "order_id": order_id})
        proposed_status_evidence = raw.get("status_evidence")
        if evidence is not None and proposed_status_evidence != evidence:
            blockers.append({"code": "official_status_evidence_payload_stale", "order_id": order_id})
        reconciliation = _official_post_handoff_reconciliation_evidence(
            conn,
            order_id=order_id,
            cutover_at=accounting_boundary_at,
            classification=classification,
            max_observation_sequence=status_watermark,
        )
        if raw.get("post_handoff_reconciliation") != reconciliation:
            blockers.append({
                "code": "post_handoff_reconciliation_evidence_stale",
                "order_id": order_id,
            })
        mapping_digest = _sha256(raw.get("mapping_digest"), f"order[{order_id}].mapping_digest")
        if mapping_digest != mappings["digest"]:
            blockers.append({"code": "order_mapping_digest_stale", "order_id": order_id})
        classifications.append(
            {
                "order_id": order_id,
                "observation_sequence": int(row[0]),
                "status_observation_sequence": (
                    0 if evidence is None else int(evidence["observation_sequence"])
                ),
                "observation_id": str(row[1]),
                "source_revision": str(row[3]),
                "source_created_at": str(row[4]),
                "observed_at": str(row[5]),
                "classification": classification,
                "facility_id": facility_id or None,
                "pool": None if classification == "unmatched" else "FBS",
                "nm_id": int(
                    (
                        mappings["sku_map"].get((int(row[7]), int(row[8] or 0)))
                        or {"target_nm_id": int(row[7])}
                    )["target_nm_id"]
                ),
                "quantity": quantity,
                "status_fingerprint": status_fp,
                "status_evidence": evidence,
                "post_handoff_reconciliation": reconciliation,
                "mapping_digest": mapping_digest,
            }
        )
    extra = sorted(set(proposed) - seen)
    if extra:
        blockers.append({"code": "classification_without_observation", "order_ids": extra[:20], "count": len(extra)})
    digest = _fingerprint({"watermark": watermark, "classifications": classifications})
    requested = _sha256(checkpoint.get("observation_watermark_digest"), "collector_checkpoint.observation_watermark_digest")
    if requested != digest:
        blockers.append({"code": "observation_watermark_digest_stale", "expected": digest, "actual": requested})
    return {"watermark_sequence": watermark, "digest": digest, "classifications": classifications}


def _official_order_status_evidence(
    conn: sqlite3.Connection,
    *,
    order_id: int,
    cutover_at: str,
    classification: str,
    max_observation_sequence: int | None = None,
) -> dict[str, Any] | None:
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
        return None
    if classification in {"pre_t_handoff_debit", "pre_t_absorbed_closed"}:
        selected = next(
            (
                row
                for row in rows
                if str(row[3]) == "complete" and str(row[4]) == "sorted"
            ),
            None,
        )
    else:
        selected = rows[-1]
    if selected is None:
        return None
    supplier_status = str(selected[3])
    wb_status = str(selected[4])
    cancellation = supplier_status == "cancel" or wb_status in {
        "canceled",
        "canceled_by_client",
        "declined_by_client",
        "defect",
    }
    handoff = supplier_status == "complete" and wb_status == "sorted"
    if classification == "pre_t_absorbed_reservation" and (cancellation or handoff):
        return None
    if classification == "pre_t_cancelled_noop" and not cancellation:
        return None
    if classification in {"pre_t_handoff_debit", "pre_t_absorbed_closed"} and not handoff:
        return None
    return {
        "observation_sequence": int(selected[0]),
        "source_revision": str(selected[1]),
        "status_digest": str(selected[2]),
        "supplier_status": supplier_status,
        "wb_status": wb_status,
        "quantity": int(selected[5]),
        "observed_at": str(selected[6]),
    }


def _official_post_handoff_reconciliation_evidence(
    conn: sqlite3.Connection,
    *,
    order_id: int,
    cutover_at: str,
    classification: str,
    max_observation_sequence: int | None = None,
) -> dict[str, Any] | None:
    if classification not in {"pre_t_handoff_debit", "pre_t_absorbed_closed"}:
        return None
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
    handoff_index = next(
        (
            index
            for index, row in enumerate(rows)
            if str(row[3]) == "complete" and str(row[4]) == "sorted"
        ),
        None,
    )
    if handoff_index is None:
        return None
    selected = next(
        (
            row
            for row in reversed(rows[handoff_index + 1 :])
            if str(row[3]) == "cancel"
            or str(row[4])
            in {"canceled", "canceled_by_client", "declined_by_client", "defect"}
        ),
        None,
    )
    if selected is None:
        return None
    return {
        "observation_sequence": int(selected[0]),
        "source_revision": str(selected[1]),
        "status_digest": str(selected[2]),
        "supplier_status": str(selected[3]),
        "wb_status": str(selected[4]),
        "quantity": int(selected[5]),
        "observed_at": str(selected[6]),
    }


def _validate_opening_reservation_sufficiency(
    classifications: list[dict[str, Any]],
    allocations: list[dict[str, Any]],
    *,
    blockers: list[dict[str, Any]],
) -> None:
    available = {
        (str(row["facility_id"]), int(row["nm_id"])): int(row["quantity"])
        for row in allocations
        if row["pool"] == "FBS"
    }
    reserved: dict[tuple[str, int], int] = {}
    for row in classifications:
        if row["classification"] != "pre_t_absorbed_reservation":
            continue
        key = (str(row["facility_id"] or ""), int(row["nm_id"]))
        reserved[key] = reserved.get(key, 0) + int(row["quantity"])
    for key, quantity in sorted(reserved.items()):
        capacity = available.get(key)
        if capacity is None:
            blockers.append(
                {"code": "opening_reservation_location_missing", "facility_id": key[0], "nm_id": key[1]}
            )
        # Reservations reduce availability but never fabricate a physical
        # debit.  An unsecured/negative available balance is explicit
        # operational evidence, not a reason to lose the exact checkpoint.


def _project_post_backfill_aggregate(
    *,
    aggregate_rows: list[dict[str, Any]],
    allocations: list[dict[str, Any]],
    classifications: list[dict[str, Any]],
    blockers: list[dict[str, Any]],
) -> dict[str, Any]:
    projected = {
        int(row["nm_id"]): {
            "nm_id": int(row["nm_id"]),
            "quantity": int(row["quantity"]),
            "capital_rub": Decimal(str(row["capital_rub"])),
        }
        for row in aggregate_rows
    }
    projected_detail = {
        (str(row["facility_id"]), str(row["pool"]), int(row["nm_id"])): {
            **dict(row),
            "capital_rub": Decimal(str(row["capital_rub"])),
        }
        for row in allocations
    }
    debit_count = 0
    debit_quantity = 0
    debit_capital = ZERO
    for order in classifications:
        if order["classification"] not in {
            "pre_t_handoff_debit",
            "pre_t_absorbed_closed",
        }:
            continue
        allocation = next(
            (
                row
                for row in allocations
                if row["facility_id"] == order["facility_id"]
                and row["pool"] == "FBS"
                and int(row["nm_id"]) == int(order["nm_id"])
            ),
            None,
        )
        if allocation is None or allocation.get("wac_rub") is None:
            blockers.append(
                {
                    "code": "historical_debit_wac_missing",
                    "order_id": int(order["order_id"]),
                }
            )
            continue
        nm_id = int(order["nm_id"])
        aggregate = projected.get(nm_id)
        if aggregate is None:
            blockers.append(
                {"code": "historical_debit_aggregate_sku_missing", "nm_id": nm_id}
            )
            continue
        quantity = int(order["quantity"])
        capital = Decimal(str(allocation["wac_rub"])) * Decimal(quantity)
        aggregate["quantity"] = int(aggregate["quantity"]) - quantity
        aggregate["capital_rub"] = Decimal(str(aggregate["capital_rub"])) - capital
        detail_key = (str(order["facility_id"]), "FBS", nm_id)
        detail = projected_detail[detail_key]
        detail["quantity"] = int(detail["quantity"]) - quantity
        detail["capital_rub"] = Decimal(str(detail["capital_rub"])) - capital
        detail["wac_rub"] = canonical_decimal_text(Decimal(str(allocation["wac_rub"])))
        debit_count += 1
        debit_quantity += quantity
        debit_capital += capital
    rows = [
        {
            "nm_id": nm_id,
            "quantity": int(item["quantity"]),
            "capital_rub": canonical_decimal_text(item["capital_rub"]),
            "wac_rub": (
                None
                if int(item["quantity"]) == 0
                else canonical_decimal_text(
                    Decimal(str(item["capital_rub"])) / Decimal(int(item["quantity"]))
                )
            ),
        }
        for nm_id, item in sorted(projected.items())
    ]
    detail_rows = []
    for _key, item in sorted(
        projected_detail.items(), key=lambda pair: (pair[0][2], pair[0][0], pair[0][1])
    ):
        detail_rows.append(
            {
                **item,
                "capital_rub": canonical_decimal_text(item["capital_rub"]),
            }
        )
    return {
        "rows": rows,
        "detail_rows": detail_rows,
        "summary": {
            "handoff_order_count": debit_count,
            "debit_quantity": debit_quantity,
            "debit_capital_rub": canonical_decimal_text(debit_capital),
            "approximate_accounting": False,
        },
    }


def _fbw_origin_snapshot(
    conn: sqlite3.Connection,
    *,
    normalized: Mapping[str, Any],
    target_epoch: int,
    facilities: set[str],
    blockers: list[dict[str, Any]],
) -> dict[str, Any]:
    supplies = conn.execute(
        f"""SELECT cache_key,wb_supply_id,raw_list_hash,raw_detail_hash,
                    raw_goods_hash,raw_package_hash
             FROM {WB_SUPPLIES_TABLE}
             WHERE status_id IN ({','.join('?' for _ in ACTIVE_FBW_STATUS_IDS)})
               AND CAST(COALESCE(wb_supply_id,'') AS INTEGER)>0
             ORDER BY COALESCE(cache_key,supply_id),wb_supply_id
             LIMIT ?""",
        (*ACTIVE_FBW_STATUS_IDS, MAX_FBW_ORIGINS + 1),
    ).fetchall()
    if len(supplies) > MAX_FBW_ORIGINS:
        blockers.append({"code": "active_fbw_supply_bound_exceeded"})
        supplies = supplies[:MAX_FBW_ORIGINS]
    proposed = {
        str(item.get("wb_supply_cache_key") or ""): item
        for item in normalized["fbw_origin_assignments"]
    }
    if len(proposed) != len(normalized["fbw_origin_assignments"]):
        raise FfPoolCutoverError("duplicate_fbw_origin", "wb_supply_cache_key must be unique")
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in supplies:
        supply = _row_dict(row)
        key = str(supply.get("cache_key") or supply.get("wb_supply_id") or "")
        seen.add(key)
        raw = proposed.get(key)
        if raw is None:
            blockers.append({"code": "active_fbw_origin_unassigned", "wb_supply_cache_key": key})
            continue
        facility_id = _identifier(raw.get("facility_id"), "fbw_origin.facility_id", maximum=80)
        if facility_id not in facilities:
            blockers.append({"code": "fbw_origin_facility_missing", "wb_supply_cache_key": key})
        if raw.get("pool", "FBO") != "FBO":
            raise FfPoolCutoverError("invalid_fbw_origin_pool", "FBW origin pool is fixed to FBO")
        revision = _wb_supply_revision(supply)
        if str(raw.get("source_revision") or "") != revision:
            blockers.append({"code": "fbw_origin_source_stale", "wb_supply_cache_key": key})
        output.append(
            {
                "wb_supply_cache_key": key,
                "wb_supply_id": str(supply.get("wb_supply_id") or ""),
                "source_revision": revision,
                "feature_epoch": target_epoch,
                "facility_id": facility_id,
                "pool": "FBO",
                "evidence_digest": _sha256(raw.get("evidence_digest"), "fbw_origin.evidence_digest"),
            }
        )
    extra = sorted(set(proposed) - seen)
    if extra:
        blockers.append({"code": "fbw_origin_without_active_supply", "count": len(extra), "keys": extra[:20]})
    return {"rows": output, "digest": _fingerprint(output)}


def _china_shipment_snapshot(
    conn: sqlite3.Connection,
    values: list[dict[str, Any]],
    *,
    facilities: set[str],
    blockers: list[dict[str, Any]],
) -> dict[str, Any]:
    required = {
        "sheet_vitrina_v1_supplier_shipments",
        "sheet_vitrina_v1_supplier_shipment_lines",
        "sheet_vitrina_v1_ff_stock_operations",
        "sheet_vitrina_v1_supplier_ff_cost_layers",
    }
    existing = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if not required.issubset(existing):
        blockers.append(
            {
                "code": "pending_shipment_evidence_schema_incomplete",
                "missing_tables": sorted(required - existing),
            }
        )
        return {"rows": [], "digest": _fingerprint([])}
    columns = {
        str(row[1])
        for row in conn.execute(
            "PRAGMA table_info(sheet_vitrina_v1_supplier_shipments)"
        ).fetchall()
    }
    shipment_required_columns = {
        "shipment_id",
        "invoice_no",
        "actual_shipment_date",
        "actual_ff_acceptance_date",
        "product_qty_total",
        "archived_at",
    }
    if not shipment_required_columns.issubset(columns):
        blockers.append(
            {
                "code": "pending_shipment_evidence_columns_incomplete",
                "missing_columns": sorted(shipment_required_columns - columns),
            }
        )
        return {"rows": [], "digest": _fingerprint([])}
    proposed: dict[str, tuple[int, dict[str, Any]]] = {}
    for index, raw in enumerate(values):
        shipment_id = _identifier(
            raw.get("shipment_id"), f"china_shipments[{index}].shipment_id"
        )
        if shipment_id in proposed:
            raise FfPoolCutoverError(
                "china_shipment_multiple_facilities",
                "One China shipment may target exactly one geographic facility",
            )
        proposed[shipment_id] = (index, raw)
    source_rows = conn.execute(
        """SELECT shipment_id,COALESCE(invoice_no,''),COALESCE(actual_shipment_date,''),
                  COALESCE(actual_ff_acceptance_date,''),product_qty_total
           FROM sheet_vitrina_v1_supplier_shipments
           WHERE COALESCE(actual_shipment_date,'')<>''
             AND COALESCE(actual_ff_acceptance_date,'')=''
             AND COALESCE(archived_at,'')=''
           ORDER BY actual_shipment_date,shipment_id"""
    ).fetchall()
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in source_rows:
        shipment_id = str(source[0])
        seen.add(shipment_id)
        proposed_item = proposed.get(shipment_id)
        if proposed_item is None:
            blockers.append(
                {"code": "pending_shipment_not_classified", "shipment_id": shipment_id}
            )
            continue
        index, raw = proposed_item
        if str(raw.get("classification") or "") != "excluded_pending_receipt":
            raise FfPoolCutoverError(
                "invalid_china_shipment_classification",
                "A clean in-transit shipment must be pinned as excluded_pending_receipt",
            )
        facility_id = _identifier(
            raw.get("facility_id"), f"china_shipments[{index}].facility_id", maximum=80
        )
        pools_value = raw.get("pools")
        if not isinstance(pools_value, list) or not pools_value:
            raise FfPoolCutoverError("china_shipment_pools_required", "China shipment requires FBS, FBO or both")
        pools = sorted({str(value) for value in pools_value})
        if any(value not in POOLS for value in pools) or len(pools) != len(pools_value):
            raise FfPoolCutoverError("invalid_china_shipment_pools", "China shipment pools must be unique FBS/FBO")
        if facility_id not in facilities:
            blockers.append({"code": "china_shipment_facility_missing", "shipment_id": shipment_id})
        shipment_total = _exact_integer(
            source[4], f"china_shipments[{index}].product_qty_total", minimum=1
        )
        line_row = conn.execute(
            """SELECT COUNT(*),COALESCE(SUM(qty),0)
               FROM sheet_vitrina_v1_supplier_shipment_lines
               WHERE shipment_id=? AND line_type='product'""",
            (shipment_id,),
        ).fetchone()
        line_count = int(line_row[0])
        line_total = _exact_integer(
            line_row[1], f"china_shipments[{index}].product_line_quantity", minimum=1
        )
        receipt_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_stock_operations WHERE source_key=?",
                (f"supplier_shipment_acceptance:{shipment_id}",),
            ).fetchone()[0]
        )
        cost_layer_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_supplier_ff_cost_layers "
                "WHERE supplier_shipment_id=?",
                (shipment_id,),
            ).fetchone()[0]
        )
        evidence = {
            "shipment_id": shipment_id,
            "invoice_no": str(source[1]),
            "actual_shipment_date": str(source[2]),
            "actual_ff_acceptance_date": str(source[3]),
            "shipment_quantity": shipment_total,
            "product_line_count": line_count,
            "product_line_quantity": line_total,
            "receipt_operation_count": receipt_count,
            "cost_layer_count": cost_layer_count,
        }
        evidence_digest = _fingerprint(evidence)
        requested_evidence = _sha256(
            raw.get("evidence_digest"), f"china_shipments[{index}].evidence_digest"
        )
        if requested_evidence != evidence_digest:
            blockers.append(
                {
                    "code": "pending_shipment_evidence_stale",
                    "shipment_id": shipment_id,
                    "expected": evidence_digest,
                    "actual": requested_evidence,
                }
            )
        if shipment_total != line_total or line_count <= 0:
            blockers.append(
                {
                    "code": "pending_shipment_quantity_ambiguous",
                    "shipment_id": shipment_id,
                    "shipment_quantity": shipment_total,
                    "product_line_quantity": line_total,
                }
            )
        if str(source[3]) or receipt_count or cost_layer_count:
            blockers.append(
                {
                    "code": "pending_shipment_partially_or_concurrently_posted",
                    "shipment_id": shipment_id,
                    "actual_ff_acceptance_date": str(source[3]),
                    "receipt_operation_count": receipt_count,
                    "cost_layer_count": cost_layer_count,
                }
            )
        rows.append(
            {
                "shipment_id": shipment_id,
                "invoice_no": str(source[1]),
                "classification": "excluded_pending_receipt",
                "facility_id": facility_id,
                "pools": pools,
                "expected_quantity": shipment_total,
                "actual_ff_acceptance_date": str(source[3]),
                "receipt_operation_count": receipt_count,
                "cost_layer_count": cost_layer_count,
                "evidence_digest": evidence_digest,
                "post_cutover_state": "in_transit",
                "guided_acceptance_required": True,
                "opening_quantity": 0,
                "opening_capital_rub": "0",
                "historical_fbs_debit_quantity": 0,
            }
        )
    extra = sorted(set(proposed) - seen)
    if extra:
        blockers.append(
            {
                "code": "excluded_pending_receipt_not_currently_pending",
                "shipment_ids": extra[:20],
                "count": len(extra),
            }
        )
    rows.sort(key=lambda row: row["shipment_id"])
    return {"rows": rows, "digest": _fingerprint(rows)}


def _collector_checkpoint_snapshot(
    conn: sqlite3.Connection,
    *,
    normalized: Mapping[str, Any],
    observations: Mapping[str, Any],
    blockers: list[dict[str, Any]],
    fallback_boundary_at: str,
) -> dict[str, Any]:
    raw = normalized["collector_checkpoint"]
    accounting_boundary_at = _utc_timestamp(
        str(raw.get("accounting_boundary_at") or fallback_boundary_at),
        field="collector_checkpoint.accounting_boundary_at",
    )
    watermarks = {
        "order_observation_watermark_sequence": _exact_integer(
            raw.get("observation_watermark_sequence", 0),
            "collector_checkpoint.observation_watermark_sequence",
            minimum=0,
        ),
        "status_observation_watermark_sequence": _exact_integer(
            raw.get(
                "status_observation_watermark_sequence",
                conn.execute(
                    f"SELECT COALESCE(MAX(observation_sequence),0) FROM {STATUS_OBSERVATIONS_TABLE}"
                ).fetchone()[0],
            ),
            "collector_checkpoint.status_observation_watermark_sequence",
            minimum=0,
        ),
        "status_transition_watermark_sequence": _exact_integer(
            raw.get(
                "status_transition_watermark_sequence",
                conn.execute(
                    f"SELECT COALESCE(MAX(transition_sequence),0) FROM {STATUS_TRANSITIONS_TABLE}"
                ).fetchone()[0],
            ),
            "collector_checkpoint.status_transition_watermark_sequence",
            minimum=0,
        ),
    }
    accounting_boundary = ff_pool_fbs_accounting_boundary_snapshot(
        conn,
        boundary_at=accounting_boundary_at,
        watermarks=watermarks,
    )
    blockers.extend(list(accounting_boundary.get("blockers") or []))
    requested_frozen_digest = str(
        raw.get("frozen_evidence_digest")
        or accounting_boundary["frozen_evidence_digest"]
    )
    _sha256(requested_frozen_digest, "collector_checkpoint.frozen_evidence_digest")
    if requested_frozen_digest != accounting_boundary["frozen_evidence_digest"]:
        blockers.append(
            {
                "code": "fbs_frozen_evidence_digest_stale",
                "expected": accounting_boundary["frozen_evidence_digest"],
                "actual": requested_frozen_digest,
            }
        )
    state = conn.execute(
        f"""SELECT window_date_from,window_date_to,next_cursor,complete,last_status,last_success_at
             FROM {FBS_COLLECTOR_STATE_TABLE} WHERE state_id=1"""
    ).fetchone()
    if state is None:
        blockers.append({"code": "collector_checkpoint_missing"})
        current = {
            "window_date_from": 0,
            "window_date_to": 0,
            "next_cursor": 0,
            "complete": 0,
            "last_status": "",
            "last_success_at": "",
        }
    else:
        current = {
            "window_date_from": int(state[0]),
            "window_date_to": int(state[1]),
            "next_cursor": int(state[2]),
            "complete": int(state[3]),
            "last_status": str(state[4]),
            "last_success_at": str(state[5]),
        }
        if str(state[4]) != "success" or not str(state[5]):
            blockers.append({"code": "collector_not_fresh_success"})
    result = {
        "accounting_boundary_at": accounting_boundary_at,
        "observation_watermark_sequence": int(observations["watermark_sequence"]),
        "observation_watermark_digest": str(observations["digest"]),
        "status_observation_watermark_sequence": watermarks[
            "status_observation_watermark_sequence"
        ],
        "status_transition_watermark_sequence": watermarks[
            "status_transition_watermark_sequence"
        ],
        "frozen_evidence_digest": accounting_boundary["frozen_evidence_digest"],
        "accounting_boundary": {
            key: value
            for key, value in accounting_boundary.items()
            if key != "blockers"
        },
        "collector_operational_readiness_checked": True,
        "post_watermark_growth_invalidates_gate": False,
    }
    result["checkpoint_digest"] = _fingerprint(result)
    return result


def _bounded_non_target_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = (
        "sheet_vitrina_v1_ff_stock_operations",
        "sheet_vitrina_v1_ff_stock_operation_lines",
        "sheet_vitrina_v1_ff_stock_reservation_operations",
        "sheet_vitrina_v1_ff_inventory_reconciliations",
        "sheet_vitrina_v1_ff_overhead_documents",
    )
    existing = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    watermarks = {
        table: int(conn.execute(f"SELECT COALESCE(MAX(rowid),0) FROM {table}").fetchone()[0])
        for table in tables
        if table in existing
    }
    value = {
        "append_watermarks": watermarks,
        "journal_mode": str(conn.execute("PRAGMA journal_mode").fetchone()[0]),
    }
    return {**value, "digest": _fingerprint(value)}


def _known_post_cutover_non_target_growth(
    conn: sqlite3.Connection,
    *,
    cutover_id: str,
    before: Mapping[str, Any],
    current: Mapping[str, Any],
) -> bool:
    if str(before.get("journal_mode") or "") != str(current.get("journal_mode") or ""):
        return False
    before_marks = dict(before.get("append_watermarks") or {})
    current_marks = dict(current.get("append_watermarks") or {})
    operations = "sheet_vitrina_v1_ff_stock_operations"
    lines = "sheet_vitrina_v1_ff_stock_operation_lines"
    for table, watermark in current_marks.items():
        if table in {operations, lines}:
            continue
        if int(watermark) != int(before_marks.get(table, 0)):
            return False
    operation_before = int(before_marks.get(operations, 0))
    operation_after = int(current_marks.get(operations, 0))
    line_before = int(before_marks.get(lines, 0))
    line_after = int(current_marks.get(lines, 0))
    if operation_after == operation_before and line_after == line_before:
        return True
    operation_columns = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({operations})").fetchall()
    }
    line_columns = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({lines})").fetchall()
    }
    if not {"operation_id", "source_type", "source_object_id"}.issubset(
        operation_columns
    ) or "operation_id" not in line_columns:
        return False
    allowed_shipments = {
        str(row[0])
        for row in conn.execute(
            f"SELECT shipment_id FROM {PENDING_SHIPMENTS_TABLE} WHERE cutover_id=?",
            (cutover_id,),
        ).fetchall()
    }
    new_operations = conn.execute(
        f"""SELECT operation_id,source_type,source_object_id
            FROM {operations} WHERE rowid>? ORDER BY rowid""",
        (operation_before,),
    ).fetchall()
    if not new_operations:
        return False
    allowed_operation_ids = {
        str(row[0])
        for row in new_operations
        if str(row[1]) in {
            "supplier_shipment_acceptance",
            "supplier_shipment_acceptance_recovery",
        }
        and str(row[2]) in allowed_shipments
    }
    if len(allowed_operation_ids) != len(new_operations):
        return False
    new_line_operations = {
        str(row[0])
        for row in conn.execute(
            f"SELECT operation_id FROM {lines} WHERE rowid>? ORDER BY rowid",
            (line_before,),
        ).fetchall()
    }
    return bool(new_line_operations) and new_line_operations.issubset(allowed_operation_ids)


def _require_empty_detail(conn: sqlite3.Connection, *, blockers: list[dict[str, Any]]) -> None:
    for table, code in (
        (BALANCES_TABLE, "detail_balances_not_empty"),
        ("sheet_vitrina_v1_ff_pool_movement_lines", "detail_movements_not_empty"),
        (MANIFESTS_TABLE, "cutover_already_exists"),
    ):
        if int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]):
            blockers.append({"code": code})


def _target_feature_epoch(
    conn: sqlite3.Connection, normalized: Mapping[str, Any], *, blockers: list[dict[str, Any]]
) -> int:
    expected = int(conn.execute(f"SELECT COALESCE(MAX(epoch),0)+1 FROM {FEATURE_EPOCHS_TABLE}").fetchone()[0])
    actual = int(normalized["target_feature_epoch"])
    if actual != expected:
        blockers.append({"code": "feature_epoch_stale", "expected": expected, "actual": actual})
    return actual


def _active_facilities(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute(f"SELECT facility_id FROM {FACILITIES_TABLE} WHERE active=1").fetchall()}


def _blocked_plan(
    *, proposal: Mapping[str, Any], deployed_sha: str, cutover_at: str, blockers: list[dict[str, Any]], status: str
) -> dict[str, Any]:
    return {
        "contract_name": CONTRACT_NAME,
        "status": status,
        "dry_run": True,
        "apply_surface_available": False,
        "apply_allowed": False,
        "requires_exact_human_gate": True,
        "manifest_digest": "",
        "blockers": blockers,
        "rebuild_input": {"proposal": proposal, "deployed_sha": deployed_sha, "cutover_at": cutover_at},
    }


def _classification_counts(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts = {value: 0 for value in ORDER_CLASSES}
    for row in rows:
        counts[str(row["classification"])] += 1
    return counts


def _safe_barrier(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in ("status", "active", "phase", "epoch_id", "manifest_digest", "deployed_sha", "event_sequence", "event_at")
    }


def _manifest_row(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    names = (
        "cutover_id", "manifest_digest", "deployed_sha", "cutover_at", "business_date", "feature_epoch",
        "aggregate_revision", "aggregate_digest", "detail_digest", "observation_watermark_sequence",
        "observation_watermark_digest", "mapping_digest", "fbw_origins_digest", "control_evidence_digest",
        "non_target_digest", "opening_document_id", "source_snapshot_digest", "created_at",
    )
    return {name: row[index] for index, name in enumerate(names)}


def _feature_row(row: sqlite3.Row | tuple[Any, ...] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "epoch": int(row[0]), "writer_enabled": bool(row[1]), "reader_enabled": bool(row[2]),
        "source_revision": str(row[3]), "created_at": str(row[4]),
    }


def _count(conn: sqlite3.Connection, table: str, cutover_id: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE cutover_id=?", (cutover_id,)).fetchone()[0])


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _row_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, sqlite3.Row):
        return dict(row)
    if hasattr(row, "keys"):
        return {str(key): row[key] for key in row.keys()}
    return {}


def _wb_supply_revision(supply: Mapping[str, Any]) -> str:
    return _fingerprint(
        {
            "cache_key": supply.get("cache_key"),
            "wb_supply_id": supply.get("wb_supply_id"),
            "raw_list_hash": supply.get("raw_list_hash"),
            "raw_detail_hash": supply.get("raw_detail_hash"),
            "raw_goods_hash": supply.get("raw_goods_hash"),
            "raw_package_hash": supply.get("raw_package_hash"),
        }
    )


def _reject_forbidden_keys(value: Any, *, path: str = "proposal") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).replace("-", "_").lower()
            if normalized in FORBIDDEN_PROPOSAL_KEYS:
                raise FfPoolCutoverError("forbidden_sensitive_or_debit_field", f"{path}.{key} is forbidden")
            _reject_forbidden_keys(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_forbidden_keys(item, path=f"{path}[{index}]")


def _bounded_mapping_list(value: Any, field: str, maximum: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise FfPoolCutoverError("invalid_list", f"{field} must be a list")
    if len(value) > maximum:
        raise FfPoolCutoverError("list_bound_exceeded", f"{field} exceeds {maximum}")
    return [dict(_mapping(item, f"{field}[{index}]")) for index, item in enumerate(value)]


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FfPoolCutoverError("invalid_object", f"{field} must be an object")
    return value


def _identifier(value: Any, field: str, *, maximum: int = 120) -> str:
    result = str(value or "").strip()
    if len(result) > maximum or not IDENTIFIER_RE.fullmatch(result):
        raise FfPoolCutoverError("invalid_identifier", f"{field} must contain 8-{maximum} safe characters")
    return result


def _sha256(value: Any, field: str) -> str:
    result = str(value or "")
    if not SHA256_RE.fullmatch(result):
        raise FfPoolCutoverError("invalid_digest", f"{field} must be sha256:<64 lowercase hex>")
    return result


def _require_commit_sha(value: str) -> None:
    if not COMMIT_SHA_RE.fullmatch(str(value or "")):
        raise FfPoolCutoverError("invalid_deployed_sha", "deployed_sha must be exact lowercase 40-char SHA")


def _positive_int(value: Any, field: str) -> int:
    return _exact_integer(value, field, minimum=1)


def _exact_integer(value: Any, field: str, *, minimum: int | None) -> int:
    if isinstance(value, bool):
        raise FfPoolCutoverError("invalid_integer", f"{field} must be exact integer")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise FfPoolCutoverError("invalid_integer", f"{field} must be exact integer") from exc
    if (
        not decimal.is_finite()
        or decimal != decimal.to_integral_value()
        or (minimum is not None and decimal < minimum)
    ):
        suffix = "" if minimum is None else f" >= {minimum}"
        raise FfPoolCutoverError("invalid_integer", f"{field} must be exact integer{suffix}")
    return int(decimal)


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise FfPoolCutoverError("invalid_decimal", f"{field} must be decimal") from exc
    if not result.is_finite():
        raise FfPoolCutoverError("invalid_decimal", f"{field} must be finite")
    return result


def _money(value: Any, field: str, *, minimum: Decimal) -> Decimal:
    result = _decimal(value, field)
    if result < minimum or result.quantize(RUB_QUANTUM, rounding=ROUND_HALF_UP) != result:
        raise FfPoolCutoverError("invalid_money", f"{field} must be exact RUB cents >= {minimum}")
    return result


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise FfPoolCutoverError("invalid_boolean", f"{field} must be boolean")
    return value


def _utc_timestamp(value: str, *, field: str) -> str:
    parsed = _parse_timestamp(value, field)
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise FfPoolCutoverError("timestamp_not_utc", f"{field} must be UTC")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise FfPoolCutoverError("invalid_timestamp", f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise FfPoolCutoverError("timezone_required", f"{field} requires timezone")
    return parsed


def _parse_source_timestamp(value: str, field: str) -> datetime:
    return _parse_timestamp(value, field).astimezone(timezone.utc)


def _business_date_yekaterinburg(value: str) -> str:
    return _parse_timestamp(value, "cutover_at").astimezone(ZoneInfo("Asia/Yekaterinburg")).date().isoformat()


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


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


def _ensure_order_classification_schema(conn: sqlite3.Connection) -> None:
    """Atomically widen the exact legacy classification CHECK constraint."""

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (ORDERS_TABLE,),
    ).fetchone()
    if row is None or not str(row[0] or "").strip():
        raise FfPoolCutoverError(
            "order_classification_schema_missing",
            "FF pool cutover order-classification table is absent",
        )
    actual_classes = _classification_check_values(str(row[0]))
    if actual_classes == ORDER_CLASSES:
        return
    if actual_classes != LEGACY_ORDER_CLASSES:
        raise FfPoolCutoverError(
            "order_classification_schema_ambiguous",
            "FF pool cutover order-classification CHECK constraint is not a known version",
            details={"classification_values": list(actual_classes)},
        )

    expected_columns = {
        "cutover_id": ("TEXT", 1, None, 1),
        "order_id": ("INTEGER", 1, None, 2),
        "observation_sequence": ("INTEGER", 1, "0", 0),
        "status_observation_sequence": ("INTEGER", 1, "0", 0),
        "observation_id": ("TEXT", 1, None, 0),
        "source_revision": ("TEXT", 1, None, 0),
        "source_created_at": ("TEXT", 1, None, 0),
        "observed_at": ("TEXT", 1, None, 0),
        "classification": ("TEXT", 1, None, 0),
        "facility_id": ("TEXT", 0, None, 0),
        "pool": ("TEXT", 0, None, 0),
        "nm_id": ("INTEGER", 1, None, 0),
        "quantity": ("INTEGER", 1, None, 0),
        "status_fingerprint": ("TEXT", 1, None, 0),
        "mapping_digest": ("TEXT", 1, None, 0),
    }
    allowed_objects = {
        "ff_pool_cutover_orders_by_class",
        "ff_pool_cutover_order_classifications_no_update",
        "ff_pool_cutover_order_classifications_no_delete",
        "warehouse_domain_guard_ff_pool_cutover_order_classifications_insert",
        "warehouse_domain_guard_ff_pool_cutover_order_classifications_update",
        "warehouse_domain_guard_ff_pool_cutover_order_classifications_delete",
    }
    if conn.in_transaction:
        raise FfPoolCutoverError(
            "order_classification_schema_transaction_active",
            "Legacy schema upgrade requires an isolated schema transaction",
        )
    if int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise FfPoolCutoverError(
            "order_classification_foreign_keys_disabled",
            "Legacy schema upgrade requires foreign_keys=ON before it starts",
        )
    legacy_table = ORDERS_TABLE + "__legacy_classification_check"
    legacy_alter_table = int(conn.execute("PRAGMA legacy_alter_table").fetchone()[0])
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        conn.execute("BEGIN IMMEDIATE")
        locked_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (ORDERS_TABLE,),
        ).fetchone()
        locked_classes = _classification_check_values(str(locked_row[0] if locked_row else ""))
        if locked_classes not in (ORDER_CLASSES, LEGACY_ORDER_CLASSES):
            raise FfPoolCutoverError(
                "order_classification_schema_ambiguous",
                "Locked order-classification CHECK is not a known version",
                details={"classification_values": list(locked_classes)},
            )
        if locked_classes == LEGACY_ORDER_CLASSES:
            columns = conn.execute(f"PRAGMA table_info({ORDERS_TABLE})").fetchall()
            actual_columns = {
                str(item[1]): (
                    str(item[2]).upper(),
                    int(item[3]),
                    None if item[4] is None else str(item[4]).strip("()"),
                    int(item[5]),
                )
                for item in columns
            }
            if actual_columns != expected_columns:
                raise FfPoolCutoverError(
                    "order_classification_schema_ambiguous",
                    "FF pool cutover order-classification columns are not the exact legacy shape",
                    details={"columns": sorted(actual_columns)},
                )
            objects = conn.execute(
                "SELECT type,name FROM sqlite_master "
                "WHERE tbl_name=? AND type IN ('index','trigger')",
                (ORDERS_TABLE,),
            ).fetchall()
            unexpected_objects = sorted(
                str(item[1])
                for item in objects
                if not str(item[1]).startswith("sqlite_autoindex_")
                and str(item[1]) not in allowed_objects
            )
            if unexpected_objects:
                raise FfPoolCutoverError(
                    "order_classification_schema_ambiguous",
                    "FF pool cutover order-classification table has unknown dependent objects",
                    details={"objects": unexpected_objects},
                )
            external_dependencies = conn.execute(
                "SELECT type,name FROM sqlite_master "
                "WHERE type IN ('view','trigger') AND tbl_name<>? AND instr(sql,?)>0",
                (ORDERS_TABLE, ORDERS_TABLE),
            ).fetchall()
            if external_dependencies:
                raise FfPoolCutoverError(
                    "order_classification_schema_ambiguous",
                    "FF pool cutover order-classification table has unknown external dependencies",
                    details={
                        "objects": sorted(
                            f"{str(item[0])}:{str(item[1])}" for item in external_dependencies
                        )
                    },
                )
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (legacy_table,)
            ).fetchone() is not None:
                raise FfPoolCutoverError(
                    "order_classification_legacy_table_present",
                    "A prior legacy order-classification schema migration is incomplete",
                )
            before_fk = _foreign_key_rows(conn, OPENING_RESERVATIONS_TABLE)
            preexisting_violations = (
                conn.execute(f"PRAGMA foreign_key_check({ORDERS_TABLE})").fetchmany(1)
                or conn.execute(
                    f"PRAGMA foreign_key_check({OPENING_RESERVATIONS_TABLE})"
                ).fetchmany(1)
            )
            if preexisting_violations:
                raise FfPoolCutoverError(
                    "order_classification_foreign_key_violation",
                    "Legacy order-classification foreign keys are already inconsistent",
                )
            column_names = [str(item[1]) for item in columns]
            quoted_columns = ",".join(
                '"' + value.replace('"', '""') + '"' for value in column_names
            )
            conn.execute(f"ALTER TABLE {ORDERS_TABLE} RENAME TO {legacy_table}")
            conn.execute(_order_classifications_table_sql())
            conn.execute(
                f"INSERT INTO {ORDERS_TABLE}({quoted_columns}) "
                f"SELECT {quoted_columns} FROM {legacy_table}"
            )
            mismatch = conn.execute(
                f"SELECT 1 FROM ("
                f"SELECT {quoted_columns} FROM {ORDERS_TABLE} "
                f"EXCEPT SELECT {quoted_columns} FROM {legacy_table}"
                f") UNION ALL SELECT 1 FROM ("
                f"SELECT {quoted_columns} FROM {legacy_table} "
                f"EXCEPT SELECT {quoted_columns} FROM {ORDERS_TABLE}"
                f") LIMIT 1"
            ).fetchone()
            if mismatch is not None:
                raise FfPoolCutoverError(
                    "order_classification_copy_mismatch",
                    "Legacy order-classification rows did not copy exactly",
                )
            conn.execute(f"DROP TABLE {legacy_table}")
            conn.execute(
                f"CREATE INDEX ff_pool_cutover_orders_by_class "
                f"ON {ORDERS_TABLE}(cutover_id,classification,order_id)"
            )
            after_fk = _foreign_key_rows(conn, OPENING_RESERVATIONS_TABLE)
            if after_fk != before_fk:
                raise FfPoolCutoverError(
                    "order_classification_foreign_key_drift",
                    "Opening-reservation foreign key changed during legacy schema upgrade",
                )
            violations = (
                conn.execute(f"PRAGMA foreign_key_check({ORDERS_TABLE})").fetchmany(1)
                or conn.execute(
                    f"PRAGMA foreign_key_check({OPENING_RESERVATIONS_TABLE})"
                ).fetchmany(1)
            )
            if violations:
                raise FfPoolCutoverError(
                    "order_classification_foreign_key_violation",
                    "Legacy order-classification schema upgrade would violate a foreign key",
                )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        try:
            conn.execute(f"PRAGMA legacy_alter_table={legacy_alter_table}")
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
    if int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise FfPoolCutoverError(
            "order_classification_foreign_keys_not_restored",
            "Legacy schema upgrade did not restore foreign key enforcement",
        )


def _classification_check_values(table_sql: str) -> tuple[str, ...]:
    match = re.search(
        r"CHECK\s*\(\s*classification\s+IN\s*\(([^)]*)\)\s*\)",
        table_sql,
        re.IGNORECASE,
    )
    if match is None:
        return ()
    body = str(match.group(1))
    values = tuple(
        value.replace("''", "'")
        for value in re.findall(r"'((?:''|[^'])*)'", body)
    )
    remainder = re.sub(r"'(?:''|[^'])*'", "", body)
    if re.sub(r"[\s,]", "", remainder):
        return ()
    return values


def _foreign_key_rows(
    conn: sqlite3.Connection,
    table: str,
) -> tuple[tuple[Any, ...], ...]:
    return tuple(tuple(row) for row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall())


def _sql_values(values: Iterable[str]) -> str:
    return ",".join("'" + str(value).replace("'", "''") + "'" for value in values)
