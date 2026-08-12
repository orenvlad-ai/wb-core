"""Safe planning contract for a later human-gated FF facility/pool cutover.

Stage 6 deliberately ships no production apply surface.  Normal deployment
creates empty additive tables and a query-only planner/readback.  The private
transactional implementation below is executable only against a database that
contains an explicit test-fixture marker; it exists to prove atomicity,
idempotency and recovery semantics before a later production-mutation PR owns
the canonical apply command.
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
    REQUESTS_TABLE,
    _apply_plan,
    _build_posting_plan,
    ensure_ff_pool_document_schema,
)
from packages.application.ff_pool_foundation import (
    BALANCES_TABLE,
    FACILITIES_TABLE,
    FEATURE_EPOCHS_TABLE,
    canonical_decimal_text,
    ensure_ff_pool_foundation_schema,
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
    ensure_wb_fbs_orders_schema,
)


CONTRACT_NAME = "ff_facility_pool_cutover_v1"
PROPOSAL_CONTRACT = "ff_facility_pool_cutover_proposal_v1"
CONTRACT_VERSION = 1
MANIFESTS_TABLE = "sheet_vitrina_v1_ff_pool_cutover_manifests"
ALLOCATIONS_TABLE = "sheet_vitrina_v1_ff_pool_cutover_allocation_lines"
ORDERS_TABLE = "sheet_vitrina_v1_ff_pool_cutover_order_classifications"
STATUS_EVIDENCE_TABLE = "sheet_vitrina_v1_ff_pool_cutover_order_status_evidence"
FBW_ORIGINS_TABLE = "sheet_vitrina_v1_ff_pool_cutover_fbw_origins"
CHECKPOINTS_TABLE = "sheet_vitrina_v1_ff_pool_cutover_checkpoints"
OPENING_RESERVATIONS_TABLE = "sheet_vitrina_v1_ff_pool_cutover_opening_reservations"
LATE_PRE_T_TABLE = "sheet_vitrina_v1_ff_pool_cutover_late_pre_t_cases"
RECOVERY_EVENTS_TABLE = "sheet_vitrina_v1_ff_pool_cutover_recovery_events"
FIXTURE_MARKER_TABLE = "ff_pool_cutover_test_fixture_marker"

FUNCTIONAL_ACTIVE_TABLE = "sheet_vitrina_v1_warehouse_functional_active"
FUNCTIONAL_BALANCES_TABLE = "sheet_vitrina_v1_warehouse_functional_balances"
WB_SUPPLIES_TABLE = "sheet_vitrina_v1_wb_supplies"
FF_STAGE = "ff"
POOLS = ("FBS", "FBO")
ORDER_CLASSES = (
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
    {"pre_t_absorbed_closed", "pre_t_absorbed_reservation", "unmatched"}
)
ACTIVE_FBW_STATUS_IDS = (1, 2, 3, 4)
MAX_ALLOCATIONS = 100_000
MAX_ORDERS = 100_000
MAX_FBW_ORIGINS = 10_000
MAX_CHINA_SHIPMENTS = 10_000
MAX_MAPPINGS = 100_000
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
        "supplier_status",
        "supplierstatus",
        "wb_status",
        "wbstatus",
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


def ensure_ff_pool_cutover_schema(conn: sqlite3.Connection) -> None:
    """Create additive empty Stage 6 objects; never seed or activate them."""

    ensure_ff_pool_foundation_schema(conn)
    ensure_ff_pool_document_schema(conn)
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

        CREATE TABLE IF NOT EXISTS {ORDERS_TABLE}(
            cutover_id TEXT NOT NULL REFERENCES {MANIFESTS_TABLE}(cutover_id),
            order_id INTEGER NOT NULL CHECK(typeof(order_id)='integer' AND order_id>0),
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
            feature_epoch INTEGER NOT NULL,
            observation_watermark_sequence INTEGER NOT NULL,
            observation_watermark_digest TEXT NOT NULL,
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
        """
    )
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
        cutover_at=boundary,
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
        normalized["china_shipments"], facilities=facilities, blockers=blockers
    )
    collector = _collector_checkpoint_snapshot(
        conn,
        normalized=normalized,
        observations=observations,
        blockers=blockers,
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
    manifest = {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "cutover_id": normalized["cutover_id"],
        "proposal_digest": _fingerprint(normalized),
        "deployed_sha": deployed_sha,
        "cutover_at": boundary,
        "business_date": business_date,
        "feature_epoch": target_epoch,
        "aggregate_revision": aggregate["revision"],
        "aggregate_digest": aggregate["digest"],
        "aggregate_rows": aggregate["rows"],
        "allocations": allocation["rows"],
        "detail_digest": allocation["digest"],
        "order_classifications": observations["classifications"],
        "observation_watermark_sequence": observations["watermark_sequence"],
        "observation_watermark_digest": observations["digest"],
        "seller_warehouse_mappings": mappings["seller_warehouse_mappings"],
        "sku_mappings": mappings["sku_mappings"],
        "mapping_digest": mappings["digest"],
        "fbw_origin_assignments": fbw_origins["rows"],
        "fbw_origins_digest": fbw_origins["digest"],
        "china_shipments": china_shipments["rows"],
        "china_shipments_digest": china_shipments["digest"],
        "collector_checkpoint": collector,
        "control_evidence_digest": _fingerprint(controls),
        "non_target_digest": non_target["digest"],
        "non_target": non_target,
        "source_snapshot_digest": source_snapshot_digest,
        "opening_document_id": opening_document_id,
        "invariants": {
            "aggregate_ff_unchanged": True,
            "aggregate_detail_quantity_parity": allocation["quantity_matches"],
            "aggregate_detail_capital_parity": allocation["capital_matches"],
            "no_new_warehouse_stage": True,
            "opening_absorption_once": True,
            "late_pre_t_isolated": True,
            "fbw_origin_explicit_not_destination_inferred": True,
            "supplier_status_complete_never_debits": True,
            "physical_debit_trigger_selected": False,
            "wb_mutation": False,
            "journal_mode_change": False,
            "legacy_relation_backfill": False,
        },
    }
    manifest_digest = _fingerprint(manifest)
    counts = _classification_counts(observations["classifications"])
    ready = not blockers
    stage2_opening_compatible = all(
        int(item["quantity"]) > 0
        and Decimal(str(item["capital_rub"])) > ZERO
        and Decimal(str(item["capital_rub"])).quantize(RUB_QUANTUM) == Decimal(str(item["capital_rub"]))
        for item in list(aggregate["rows"]) + list(allocation["rows"])
    )
    return {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "status": "ready" if ready else "blocked",
        "dry_run": True,
        "apply_surface_available": False,
        "apply_allowed": ready and stage2_opening_compatible,
        "production_apply_contract_ready": stage2_opening_compatible,
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
                "review_exact_manifest_and_open_separate_production_mutation_gate"
                if ready and stage2_opening_compatible
                else "implement_signed_exact_decimal_opening_contract"
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
            "apply_surface_available": False,
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
        "apply_surface_available": False,
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
            int(item["quantity"]), str(item["capital_rub"])
        )
        for item in manifest.get("allocations") or []
    }
    if detail != expected_detail:
        mismatches.append("detail_projection")
    aggregate_by_nm = {
        int(item["nm_id"]): (int(item["quantity"]), Decimal(str(item["capital_rub"])))
        for item in manifest.get("aggregate_rows") or []
    }
    summed: dict[int, tuple[int, Decimal]] = {}
    for item in manifest.get("allocations") or []:
        nm_id = int(item["nm_id"])
        quantity, capital = summed.get(nm_id, (0, ZERO))
        summed[nm_id] = (
            quantity + int(item["quantity"]), capital + Decimal(str(item["capital_rub"]))
        )
    if summed != aggregate_by_nm:
        mismatches.append("aggregate_detail_parity")
    feature = conn.execute(
        f"SELECT writer_enabled,reader_enabled,source_revision FROM {FEATURE_EPOCHS_TABLE} WHERE epoch=?",
        (int(row[2]),),
    ).fetchone()
    if feature is None or (int(feature[0]), int(feature[1]), str(feature[2])) != (1, 0, str(row[0])):
        mismatches.append("feature_epoch")
    checkpoint = conn.execute(
        f"""SELECT cutover_at,feature_epoch,observation_watermark_sequence,
                    observation_watermark_digest,collector_window_from,
                    collector_window_to,collector_next_cursor,collector_complete,
                    checkpoint_digest
             FROM {CHECKPOINTS_TABLE} WHERE cutover_id=?""",
        (identity,),
    ).fetchone()
    expected_checkpoint = dict(manifest.get("collector_checkpoint") or {})
    if checkpoint is None:
        mismatches.append("checkpoint")
    else:
        checkpoint_value = {
            "window_date_from": int(checkpoint[4]),
            "window_date_to": int(checkpoint[5]),
            "next_cursor": int(checkpoint[6]),
            "complete": int(checkpoint[7]),
            "observation_watermark_sequence": int(checkpoint[2]),
            "observation_watermark_digest": str(checkpoint[3]),
        }
        if (
            str(checkpoint[0]) != str(manifest["cutover_at"])
            or int(checkpoint[1]) != int(manifest["feature_epoch"])
            or _fingerprint(checkpoint_value) != str(checkpoint[8])
            or str(checkpoint[8]) != str(expected_checkpoint.get("checkpoint_digest") or "")
        ):
            mismatches.append("checkpoint")
    persisted_orders = [
        {
            "order_id": int(item[0]), "observation_id": str(item[1]), "source_revision": str(item[2]),
            "source_created_at": str(item[3]), "observed_at": str(item[4]), "classification": str(item[5]),
            "facility_id": None if item[6] is None else str(item[6]),
            "pool": None if item[7] is None else str(item[7]), "nm_id": int(item[8]),
            "quantity": int(item[9]), "status_fingerprint": str(item[10]), "mapping_digest": str(item[11]),
        }
        for item in conn.execute(
            f"""SELECT order_id,observation_id,source_revision,source_created_at,observed_at,
                        classification,facility_id,pool,nm_id,quantity,status_fingerprint,mapping_digest
                 FROM {ORDERS_TABLE} WHERE cutover_id=? ORDER BY order_id""",
            (identity,),
        ).fetchall()
    ]
    if persisted_orders != list(manifest.get("order_classifications") or []):
        mismatches.append("order_classifications")
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
                "capital_rub": f"{Decimal(str(item['capital_rub'])):.2f}",
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
    aggregate_unchanged = (
        not aggregate_blockers
        and str(current_aggregate["revision"]) == str(manifest["aggregate_revision"])
        and str(current_aggregate["digest"]) == str(manifest["aggregate_digest"])
    )
    if not aggregate_unchanged:
        mismatches.append("aggregate_source_drift")
    current_non_target = _bounded_non_target_snapshot(conn)
    if (
        current_non_target != dict(manifest.get("non_target") or {})
        or str(current_non_target["digest"]) != str(manifest["non_target_digest"])
    ):
        mismatches.append("non_target_drift")
    return {
        "status": "pass" if not mismatches else "mismatch",
        "cutover_id": identity,
        "manifest_digest": str(row[0]),
        "feature_epoch": int(row[2]),
        "mismatches": mismatches,
        "aggregate_unchanged": aggregate_unchanged,
        "reader_enabled": False,
    }


def classify_late_pre_t_observations(
    conn: sqlite3.Connection, *, cutover_id: str, limit: int = 500
) -> dict[str, Any]:
    """Identify, without posting, observations that arrived after the checkpoint."""

    identity = _identifier(cutover_id, "cutover_id")
    bound = _exact_integer(limit, "limit", minimum=1)
    if bound > 500:
        raise FfPoolCutoverError("limit_too_large", "late_pre_t limit cannot exceed 500")
    manifest = conn.execute(
        f"SELECT cutover_at,observation_watermark_sequence FROM {MANIFESTS_TABLE} WHERE cutover_id=?",
        (identity,),
    ).fetchone()
    if manifest is None:
        raise FfPoolCutoverError("cutover_not_found", "cutover manifest was not found")
    boundary = _parse_timestamp(str(manifest[0]), "cutover_at")
    rows = conn.execute(
        f"""SELECT observation_id,order_id,source_revision,source_created_at,observed_at
             FROM {OBSERVATIONS_TABLE} AS observation
             WHERE observation_sequence>?
               AND NOT EXISTS(
                 SELECT 1 FROM {ORDERS_TABLE} AS known
                 WHERE known.cutover_id=? AND known.order_id=observation.order_id
                   AND known.source_revision=observation.source_revision
               )
             ORDER BY observation_sequence LIMIT ?""",
        (int(manifest[1]), identity, bound),
    ).fetchall()
    cases = []
    for row in rows:
        created = _parse_source_timestamp(str(row[3] or ""), "source_created_at")
        if created <= boundary:
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


def _apply_ff_pool_cutover_fixture(
    conn: sqlite3.Connection,
    *,
    proposal: Mapping[str, Any],
    deployed_sha: str,
    cutover_at: str,
    crash: str = "",
) -> dict[str, Any]:
    """Exercise the exact transaction only in explicitly marked test databases.

    This function is intentionally private, is not imported by the query-only
    CLI, and refuses every normal operational store because the fixture marker
    table is never part of production schema ensure.
    """

    marker = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (FIXTURE_MARKER_TABLE,)
    ).fetchone()
    if marker is None:
        raise FfPoolCutoverError("fixture_marker_required", "production apply surface is not shipped")
    normalized = _normalize_proposal(proposal)
    existing = conn.execute(
        f"SELECT manifest_digest,deployed_sha,cutover_at,manifest_json FROM {MANIFESTS_TABLE} WHERE cutover_id=?",
        (normalized["cutover_id"],),
    ).fetchone()
    if existing is not None:
        stored = json.loads(str(existing[3]))
        if (
            str(existing[1]) != deployed_sha
            or str(existing[2]) != _utc_timestamp(cutover_at, field="cutover_at")
            or str(stored.get("proposal_digest") or "") != _fingerprint(normalized)
        ):
            raise FfPoolCutoverError("manifest_conflict", "cutover_id already has another digest")
        return {"status": "already_applied", "manifest_digest": str(existing[0]), "idempotent": True}
    conn.execute("BEGIN IMMEDIATE")
    try:
        plan = build_ff_pool_cutover_plan(
            conn, proposal=proposal, deployed_sha=deployed_sha, cutover_at=cutover_at
        )
        if plan["status"] != "ready":
            raise FfPoolCutoverError("plan_not_ready", "fixture apply requires exact ready plan", details=plan["blockers"])
        manifest = plan["manifest"]
        if any(
            int(item["quantity"]) < 0
            or Decimal(str(item["capital_rub"])).quantize(RUB_QUANTUM) != Decimal(str(item["capital_rub"]))
            or Decimal(str(item["capital_rub"])) < ZERO
            for item in list(manifest["aggregate_rows"]) + list(manifest["allocations"])
        ):
            raise FfPoolCutoverError(
                "fixture_stage2_opening_precision_limit",
                "fixture-only Stage 2 posting covers nonnegative RUB-cent openings; "
                "the later production runner must preserve signed exact Decimal manifest values",
            )
        now = _utc_timestamp(cutover_at, field="cutover_at")
        epoch_id = normalized["write_epoch_id"]
        epoch_digest = normalized["control_manifest_digest"]
        conn.execute(
            f"""INSERT INTO {WRITE_EPOCH_EVENTS_TABLE}(
                    epoch_id,phase,manifest_digest,deployed_sha,event_at,actor,details_json
                ) VALUES(?,?,?,?,?,?,?)""",
            (epoch_id, "applying", epoch_digest, deployed_sha, now, "stage6_fixture", "{}"),
        )
        conn.execute(
            f"""INSERT INTO {FEATURE_EPOCHS_TABLE}(
                    epoch,writer_enabled,reader_enabled,source_revision,created_at,metadata_json
                ) VALUES(?,?,?,?,?,?)""",
            (
                int(manifest["feature_epoch"]), 1, 0, str(plan["manifest_digest"]), now,
                json.dumps({"cutover_id": manifest["cutover_id"], "reader_default_off": True}, separators=(",", ":")),
            ),
        )
        request_id = str(manifest["opening_document_id"])
        request_identity = "sha256:" + request_id.removeprefix("ffpd_")
        request = {
            "request_id": request_id,
            "request_identity": request_identity,
            "document_kind": "facility_pool_opening",
            "source_system": "ff_pool_cutover",
            "source_type": "cutover_manifest",
            "source_id": str(manifest["cutover_id"]),
            "source_revision": str(plan["manifest_digest"]),
            "idempotency_epoch": int(manifest["feature_epoch"]),
            "business_date": str(manifest["business_date"]),
            "actor": "stage6_fixture",
            "source_filename": "",
            "source_content_type": "application/json",
            "source_sha256": "",
            "template_fingerprint": str(plan["manifest_digest"]),
        }
        opening_input = {
            "aggregate_rows": manifest["aggregate_rows"],
            "allocations": manifest["allocations"],
        }
        conn.execute(
            f"""INSERT INTO {REQUESTS_TABLE}(
                request_id,request_identity,client_request_id,document_kind,state,
                source_system,source_type,source_id,source_revision,idempotency_epoch,
                actor,business_date,source_filename,source_content_type,source_sha256,
                source_file_blob,template_fingerprint,request_payload_json,preview_manifest_json,
                accepted_at,ready_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                request_id, request_identity, request_id, "facility_pool_opening", "ready",
                request["source_system"], request["source_type"], request["source_id"],
                request["source_revision"], request["idempotency_epoch"], request["actor"],
                request["business_date"], "", "application/json", "", None,
                request["template_fingerprint"], json.dumps(opening_input, separators=(",", ":")),
                json.dumps(opening_input, separators=(",", ":")), now, now, now,
            ),
        )
        posting = _build_posting_plan(
            conn,
            request=request,
            manifest=opening_input,
            epoch=int(manifest["feature_epoch"]),
        )
        _apply_plan(
            conn,
            request=request,
            plan=posting,
            epoch=int(manifest["feature_epoch"]),
            posted_at=now,
        )
        conn.execute(
            f"UPDATE {REQUESTS_TABLE} SET state='complete',posted_document_id=?,"
            "posted_manifest_sha256=?,posted_at=?,completed_at=?,updated_at=? WHERE request_id=?",
            (request_id, _fingerprint(posting["posted_manifest"]), now, now, now, request_id),
        )
        _insert_fixture_manifest(conn, plan=plan, now=now)
        if crash == "before_commit":
            raise RuntimeError("fixture crash before commit")
        conn.execute(
            f"""INSERT INTO {WRITE_EPOCH_EVENTS_TABLE}(
                    epoch_id,phase,manifest_digest,deployed_sha,event_at,actor,details_json
                ) VALUES(?,?,?,?,?,?,?)""",
            (epoch_id, "readback_required", epoch_digest, deployed_sha, now, "stage6_fixture", "{}"),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if crash == "after_commit":
        raise FfPoolCutoverAmbiguousCommit("commit succeeded; exact readback is mandatory")
    return {"status": "applied_fixture", "manifest_digest": plan["manifest_digest"], "idempotent": False}


def _insert_fixture_manifest(conn: sqlite3.Connection, *, plan: Mapping[str, Any], now: str) -> None:
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
            f"INSERT INTO {ORDERS_TABLE} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                manifest["cutover_id"], row["order_id"], row["observation_id"], row["source_revision"],
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
                manifest["feature_epoch"], row["facility_id"], "FBO", None, "stage6_fixture", "opening manifest", now,
            ),
        )
    conn.execute(
        f"INSERT INTO {CHECKPOINTS_TABLE} VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            manifest["cutover_id"], manifest["cutover_at"], manifest["feature_epoch"],
            manifest["observation_watermark_sequence"], manifest["observation_watermark_digest"],
            collector["window_date_from"], collector["window_date_to"], collector["next_cursor"],
            collector["complete"], collector["checkpoint_digest"], now,
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
    return {
        "contract_name": PROPOSAL_CONTRACT,
        "cutover_id": cutover_id,
        "business_date": business_date,
        "target_feature_epoch": target_epoch,
        "write_epoch_id": write_epoch_id,
        "control_manifest_digest": control_manifest_digest,
        "control_evidence": control_evidence,
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
        nm_id = _positive_int(raw.get("nm_id"), "sku_mappings.nm_id")
        chrt_id = _positive_int(raw.get("chrt_id"), "sku_mappings.chrt_id")
        identity = _sha256(raw.get("identity_digest"), "sku_mappings.identity_digest")
        key = (nm_id, chrt_id)
        if key in sku_keys:
            raise FfPoolCutoverError("duplicate_sku_mapping", str(key))
        sku_keys.add(key)
        skus.append({"nm_id": nm_id, "chrt_id": chrt_id, "identity_digest": identity})
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
    cutover_at: str,
    mappings: Mapping[str, Any],
    blockers: list[dict[str, Any]],
) -> dict[str, Any]:
    checkpoint = normalized["collector_checkpoint"]
    watermark = _exact_integer(
        checkpoint.get("observation_watermark_sequence", 0),
        "collector_checkpoint.observation_watermark_sequence",
        minimum=0,
    )
    actual_max = int(conn.execute(f"SELECT COALESCE(MAX(observation_sequence),0) FROM {OBSERVATIONS_TABLE}").fetchone()[0])
    if watermark != actual_max:
        blockers.append({"code": "observation_watermark_stale", "expected": actual_max, "actual": watermark})
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
    boundary_dt = _parse_timestamp(cutover_at, "cutover_at")
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
        created = _parse_source_timestamp(str(row[4] or ""), f"order[{order_id}].source_created_at")
        is_pre_t = created <= boundary_dt
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
        evidence = conn.execute(
            f"""SELECT lifecycle_class,quantity FROM {STATUS_EVIDENCE_TABLE}
                 WHERE order_id=? AND source_revision=? AND evidence_digest=?
                 ORDER BY observed_at DESC LIMIT 1""",
            (order_id, str(row[3]), status_fp),
        ).fetchone()
        if evidence is None:
            blockers.append({"code": "official_status_evidence_missing", "order_id": order_id})
        else:
            lifecycle = str(evidence[0])
            expected_lifecycle = {
                "pre_t_absorbed_closed": "closed_pre_handoff",
                "pre_t_absorbed_reservation": "active_pre_handoff",
                "unmatched": "unmatched",
            }.get(classification)
            if expected_lifecycle is not None and lifecycle != expected_lifecycle:
                blockers.append({"code": "official_status_class_mismatch", "order_id": order_id})
            if int(evidence[1]) != quantity:
                blockers.append({"code": "official_status_quantity_mismatch", "order_id": order_id})
        mapping_digest = _sha256(raw.get("mapping_digest"), f"order[{order_id}].mapping_digest")
        if mapping_digest != mappings["digest"]:
            blockers.append({"code": "order_mapping_digest_stale", "order_id": order_id})
        classifications.append(
            {
                "order_id": order_id,
                "observation_id": str(row[1]),
                "source_revision": str(row[3]),
                "source_created_at": str(row[4]),
                "observed_at": str(row[5]),
                "classification": classification,
                "facility_id": facility_id or None,
                "pool": None if classification == "unmatched" else "FBS",
                "nm_id": int(row[7]),
                "quantity": quantity,
                "status_fingerprint": status_fp,
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
        elif capacity < quantity:
            blockers.append(
                {
                    "code": "opening_reservation_exceeds_fbs_allocation",
                    "facility_id": key[0], "nm_id": key[1],
                    "reserved_quantity": quantity, "fbs_quantity": capacity,
                }
            )


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
    values: list[dict[str, Any]], *, facilities: set[str], blockers: list[dict[str, Any]]
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    shipment_ids: set[str] = set()
    for index, raw in enumerate(values):
        shipment_id = _identifier(raw.get("shipment_id"), f"china_shipments[{index}].shipment_id")
        facility_id = _identifier(
            raw.get("facility_id"), f"china_shipments[{index}].facility_id", maximum=80
        )
        pools_value = raw.get("pools")
        if not isinstance(pools_value, list) or not pools_value:
            raise FfPoolCutoverError("china_shipment_pools_required", "China shipment requires FBS, FBO or both")
        pools = sorted({str(value) for value in pools_value})
        if any(value not in POOLS for value in pools) or len(pools) != len(pools_value):
            raise FfPoolCutoverError("invalid_china_shipment_pools", "China shipment pools must be unique FBS/FBO")
        if shipment_id in shipment_ids:
            raise FfPoolCutoverError(
                "china_shipment_multiple_facilities",
                "One China shipment may target exactly one geographic facility",
            )
        shipment_ids.add(shipment_id)
        if facility_id not in facilities:
            blockers.append({"code": "china_shipment_facility_missing", "shipment_id": shipment_id})
        rows.append(
            {
                "shipment_id": shipment_id,
                "facility_id": facility_id,
                "pools": pools,
                "evidence_digest": _sha256(
                    raw.get("evidence_digest"), f"china_shipments[{index}].evidence_digest"
                ),
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
) -> dict[str, Any]:
    raw = normalized["collector_checkpoint"]
    state = conn.execute(
        f"""SELECT window_date_from,window_date_to,next_cursor,complete,last_status,last_success_at
             FROM {FBS_COLLECTOR_STATE_TABLE} WHERE state_id=1"""
    ).fetchone()
    if state is None:
        expected = {"window_date_from": 0, "window_date_to": 0, "next_cursor": 0, "complete": 0}
    else:
        expected = {
            "window_date_from": int(state[0]),
            "window_date_to": int(state[1]),
            "next_cursor": int(state[2]),
            "complete": int(state[3]),
        }
        if str(state[4]) != "success" or not str(state[5]):
            blockers.append({"code": "collector_not_fresh_success"})
    actual = {
        "window_date_from": _exact_integer(raw.get("window_date_from", 0), "collector_checkpoint.window_date_from", minimum=0),
        "window_date_to": _exact_integer(raw.get("window_date_to", 0), "collector_checkpoint.window_date_to", minimum=0),
        "next_cursor": _exact_integer(raw.get("next_cursor", 0), "collector_checkpoint.next_cursor", minimum=0),
        "complete": 1 if _boolean(raw.get("complete", False), "collector_checkpoint.complete") else 0,
    }
    if actual != expected:
        blockers.append({"code": "collector_checkpoint_stale", "expected": expected, "actual": actual})
    result = {
        **actual,
        "observation_watermark_sequence": int(observations["watermark_sequence"]),
        "observation_watermark_digest": str(observations["digest"]),
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


def _sql_values(values: Iterable[str]) -> str:
    return ",".join("'" + str(value).replace("'", "''") + "'" for value in values)
