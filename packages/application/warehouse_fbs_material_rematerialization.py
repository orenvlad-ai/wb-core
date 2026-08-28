"""Version-coherent FBS aggregate publication and bounded incident recovery.

The facility/pool ledger remains the physical authority.  This module never
calls WB or another external producer.  It turns an already-committed pool
effect into a new immutable functional version and, for an explicitly planned
single-SKU incident, switches the exact ready-snapshot dependency closure in
the same SQLite transaction.
"""

from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import sqlite3
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

from packages.application.ff_pool_foundation import (
    BALANCES_TABLE,
    FACILITIES_TABLE,
    FEATURE_EPOCHS_TABLE,
    LINES_TABLE,
    OPERATIONS_TABLE,
    canonical_decimal_text,
    canonical_decimal_ratio_text,
)
from packages.application.sheet_vitrina_v1_our_wb_costs import (
    OUR_WB_UNIT_COST_RUB_METRIC_KEY,
    OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY,
    OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY,
    TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY,
)
from packages.application.sheet_vitrina_v1_own_product_capital import (
    OWN_TOTAL_CONFIRMED_SHARE_PCT_METRIC_KEY,
    OWN_TOTAL_PAID_EQUIVALENT_QTY_METRIC_KEY,
    OWN_TOTAL_QTY_METRIC_KEY,
    own_stage_metric_key,
)
from packages.application.sheet_vitrina_v1_proxy_v4 import (
    PROXY_V4_TOTAL_MARGIN_PER_UNIT_RUB_METRIC_KEY,
    PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY,
    PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY,
)
from packages.application.warehouse_functional_lock import (
    warehouse_functional_write_lock,
)
from packages.application.business_data_write_barrier import barrier_status
from packages.business_time import current_business_date_iso

if TYPE_CHECKING:
    from packages.application.registry_upload_db_backed_runtime import (
        RegistryUploadDbBackedRuntime,
    )


CONTRACT_NAME = "warehouse_fbs_material_rematerialization_v1"
INTENTS_TABLE = "sheet_vitrina_v1_warehouse_fbs_material_intents"
EVENTS_TABLE = "sheet_vitrina_v1_warehouse_fbs_material_intent_events"
MAX_REPAIR_TARGETS = 1
MAX_FUNCTIONAL_BALANCE_ROWS = 4_096
MAX_AUXILIARY_ROWS = 4_096
MAX_READY_SNAPSHOTS = 4
MAX_RETRY_ATTEMPTS = 3
MAX_WB_SNAPSHOT_BYTES = 8_000_000
MAX_READY_CLOSURE_BYTES = 8_000_000
MAX_PERSISTED_PLAN_BYTES = 10_000_000
ZERO = Decimal("0")
FUNCTIONAL_CUTOVER_ID = "warehouse_functional_cutover_v1"
STAGE_FF = "ff"
HISTORICAL_MANIFEST_SCHEMA = "warehouse_fbs_historical_recovery_manifest_v2"

REPAIRABLE = "repairable"
REPAIRING = "repairing"
REPAIRED = "repaired"
RETRY_EXHAUSTED = "retry_exhausted"
HISTORICAL_RECOVERY_REQUIRED = "historical_recovery_required"
UNSAFE_AMBIGUOUS = "unsafe_ambiguous"

CRITICAL_TOTAL_METRIC_KEYS = (
    TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY,
    OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY,
    PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY,
    PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY,
    PROXY_V4_TOTAL_MARGIN_PER_UNIT_RUB_METRIC_KEY,
)


class WarehouseFbsMaterialError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details = details


def ensure_warehouse_fbs_material_schema(conn: sqlite3.Connection) -> None:
    from packages.application.warehouse_functional import (
        ensure_warehouse_functional_schema,
    )

    ensure_warehouse_functional_schema(conn)
    ensure_warehouse_fbs_material_intent_schema(conn)


def ensure_warehouse_fbs_material_intent_schema(conn: sqlite3.Connection) -> None:
    """Install only the additive intent/outcome tables on an existing store."""

    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {INTENTS_TABLE}(
            operation_id TEXT PRIMARY KEY,
            business_date TEXT NOT NULL,
            facility_id TEXT NOT NULL,
            pool TEXT NOT NULL CHECK(pool='FBS'),
            nm_id INTEGER NOT NULL,
            source_version_id TEXT NOT NULL,
            target_version_id TEXT NOT NULL,
            plan_fingerprint TEXT NOT NULL UNIQUE,
            plan_json TEXT NOT NULL,
            source_material_digest TEXT NOT NULL,
            roster_digest TEXT NOT NULL,
            provenance_digest TEXT NOT NULL,
            ready_before_digest TEXT NOT NULL,
            ready_after_digest TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN(
                'repairable','repairing','repaired','retry_exhausted',
                'historical_recovery_required','unsafe_ambiguous'
            )),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            typed_evidence_json TEXT NOT NULL,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS {EVENTS_TABLE}(
            event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_id TEXT NOT NULL,
            status TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS warehouse_fbs_material_intent_status
        ON {INTENTS_TABLE}(status,business_date,nm_id,operation_id);
        CREATE TRIGGER IF NOT EXISTS warehouse_fbs_material_event_update_forbidden
        BEFORE UPDATE ON {EVENTS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS material intent evidence is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS warehouse_fbs_material_event_delete_forbidden
        BEFORE DELETE ON {EVENTS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS material intent evidence is append-only'); END;
        """
    )
    columns = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({INTENTS_TABLE})")
    }
    if "plan_json" not in columns:
        conn.execute(
            f"ALTER TABLE {INTENTS_TABLE} "
            "ADD COLUMN plan_json TEXT NOT NULL DEFAULT '{}'"
        )


def _require_material_schema(conn: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    required = {
        INTENTS_TABLE,
        EVENTS_TABLE,
        "sheet_vitrina_v1_warehouse_functional_versions",
        "sheet_vitrina_v1_warehouse_functional_active",
        "sheet_vitrina_v1_warehouse_functional_balances",
        "sheet_vitrina_v1_warehouse_wb_snapshots",
        BALANCES_TABLE,
        FEATURE_EPOCHS_TABLE,
    }
    missing = sorted(required - tables)
    if missing:
        raise WarehouseFbsMaterialError(
            "fbs_material_schema_missing",
            "FBS material schema is not installed",
            details={"missing_tables": missing},
        )


def publish_fbs_pool_aggregate_revision(
    conn: sqlite3.Connection,
    *,
    affected_nm_ids: Iterable[int],
    source_kind: str,
    source_id: str,
    business_date: str,
    published_at: str,
) -> dict[str, Any]:
    """Publish pool detail and every functional operand as one new version.

    The caller owns the shared writer lock and SQLite transaction.  This seam
    is used by lifecycle debit, guided receipt/recovery and pool overhead.
    """

    nm_ids = sorted({int(value) for value in affected_nm_ids if int(value) > 0})
    if not nm_ids:
        raise WarehouseFbsMaterialError(
            "fbs_material_target_missing", "FBS material publication has no SKU closure"
        )
    if len(nm_ids) > MAX_FUNCTIONAL_BALANCE_ROWS:
        raise WarehouseFbsMaterialError(
            "fbs_material_scope_too_broad",
            "FBS material publication exceeds the bounded SKU closure",
            details={"target_count": len(nm_ids)},
        )
    candidate = _build_candidate(
        conn,
        affected_nm_ids=nm_ids,
        source_kind=source_kind,
        source_id=source_id,
        business_date=_iso_date(business_date),
        published_at=str(published_at),
        allow_source_mismatch=False,
    )
    return _persist_candidate_and_switch(
        conn,
        candidate=candidate,
        ready_updates=(),
        expected_active_version_id=str(candidate["source_version_id"]),
    )


class WarehouseFbsMaterialRematerializer:
    """Internal bounded planner/applicator; intentionally has no CLI/HTTP hook."""

    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        timestamp_factory: Callable[[], str],
    ) -> None:
        self.runtime = runtime
        self.timestamp_factory = timestamp_factory

    def build_plan(
        self,
        *,
        business_date: str,
        facility_id: str,
        pool: str,
        nm_ids: Iterable[int],
    ) -> dict[str, Any]:
        target_date = _iso_date(business_date)
        target_pool = str(pool or "").upper()
        targets = sorted({int(value) for value in nm_ids if int(value) > 0})
        if target_pool != "FBS":
            return _blocked_plan(
                UNSAFE_AMBIGUOUS,
                "fbo_wb_outside_fbs_material_scope",
                business_date=target_date,
                facility_id=facility_id,
                pool=target_pool,
                nm_ids=targets,
            )
        if len(targets) != MAX_REPAIR_TARGETS:
            return _blocked_plan(
                UNSAFE_AMBIGUOUS,
                "broad_or_unknown_mismatch",
                business_date=target_date,
                facility_id=facility_id,
                pool=target_pool,
                nm_ids=targets,
            )
        with _connect_readonly(self.runtime.db_path) as conn:
            _require_material_schema(conn)
            active = _active_version(conn)
            if active is None:
                return _blocked_plan(
                    UNSAFE_AMBIGUOUS,
                    "active_functional_version_missing",
                    business_date=target_date,
                    facility_id=facility_id,
                    pool=target_pool,
                    nm_ids=targets,
                )
            if str(active["business_effective_date"] or "") != target_date:
                return _blocked_plan(
                    HISTORICAL_RECOVERY_REQUIRED,
                    "target_is_not_active_business_date",
                    business_date=target_date,
                    facility_id=facility_id,
                    pool=target_pool,
                    nm_ids=targets,
                    source_version_id=str(active["version_id"]),
                )
            facility = conn.execute(
                f"SELECT facility_id,active FROM {FACILITIES_TABLE} WHERE facility_id=?",
                (str(facility_id),),
            ).fetchone()
            if facility is None or not bool(facility["active"]):
                return _blocked_plan(
                    UNSAFE_AMBIGUOUS,
                    "facility_identity_missing_or_inactive",
                    business_date=target_date,
                    facility_id=facility_id,
                    pool=target_pool,
                    nm_ids=targets,
                )
            target_nm_id = targets[0]
            target_location = conn.execute(
                f"""SELECT quantity,capital_rub,wac_rub,source_watermark,projection_epoch
                    FROM {BALANCES_TABLE}
                    WHERE facility_id=? AND pool='FBS' AND nm_id=?""",
                (str(facility_id), target_nm_id),
            ).fetchone()
            if target_location is None:
                return _blocked_plan(
                    UNSAFE_AMBIGUOUS,
                    "target_facility_pool_evidence_missing",
                    business_date=target_date,
                    facility_id=facility_id,
                    pool=target_pool,
                    nm_ids=targets,
                    source_version_id=str(active["version_id"]),
                )
            target_source_evidence = _canonical_target_source_evidence(
                conn,
                facility_id=str(facility_id),
                pool="FBS",
                nm_id=target_nm_id,
                source_watermark=str(target_location["source_watermark"]),
            )
            if target_source_evidence is None:
                return _blocked_plan(
                    UNSAFE_AMBIGUOUS,
                    "target_source_evidence_missing_or_ambiguous",
                    business_date=target_date,
                    facility_id=facility_id,
                    pool=target_pool,
                    nm_ids=targets,
                    source_version_id=str(active["version_id"]),
                )
            if not _target_location_changed_since_version(
                conn,
                version_id=str(active["version_id"]),
                facility_id=str(facility_id),
                pool="FBS",
                nm_id=target_nm_id,
                current=target_location,
            ):
                return _blocked_plan(
                    UNSAFE_AMBIGUOUS,
                    "target_facility_pool_not_mismatched",
                    business_date=target_date,
                    facility_id=facility_id,
                    pool=target_pool,
                    nm_ids=targets,
                    source_version_id=str(active["version_id"]),
                )
            mismatches = _functional_pool_mismatches(conn, str(active["version_id"]))
            if mismatches != [target_nm_id]:
                return _blocked_plan(
                    UNSAFE_AMBIGUOUS,
                    "broad_or_unknown_mismatch",
                    business_date=target_date,
                    facility_id=facility_id,
                    pool=target_pool,
                    nm_ids=targets,
                    source_version_id=str(active["version_id"]),
                    details={"mismatched_nm_ids": mismatches[:20]},
                )
            snapshots = _ready_snapshots_for_date(conn, target_date)
            if not snapshots or len(snapshots) > MAX_READY_SNAPSHOTS:
                return _blocked_plan(
                    UNSAFE_AMBIGUOUS,
                    "ready_snapshot_scope_missing_or_broad",
                    business_date=target_date,
                    facility_id=facility_id,
                    pool=target_pool,
                    nm_ids=targets,
                    source_version_id=str(active["version_id"]),
                    details={"snapshot_count": len(snapshots)},
                )
            ready_before_bytes = sum(int(item["plan_bytes"]) for item in snapshots)
            if ready_before_bytes > MAX_READY_CLOSURE_BYTES:
                return _blocked_plan(
                    UNSAFE_AMBIGUOUS,
                    "ready_snapshot_scope_missing_or_broad",
                    business_date=target_date,
                    facility_id=facility_id,
                    pool=target_pool,
                    nm_ids=targets,
                    source_version_id=str(active["version_id"]),
                    details={
                        "snapshot_count": len(snapshots),
                        "ready_before_bytes": ready_before_bytes,
                        "max_ready_closure_bytes": MAX_READY_CLOSURE_BYTES,
                    },
                )
            candidate = _build_candidate(
                conn,
                affected_nm_ids=targets,
                source_kind="fbs_material_incident_recovery",
                source_id=(
                    f"{target_date}:{facility_id}:FBS:{target_nm_id}:"
                    f"{target_location['source_watermark']}"
                ),
                business_date=target_date,
                published_at=str(self.timestamp_factory()),
                allow_source_mismatch=True,
            )
            candidate_rows = list(candidate["lines"])
            roster_nm_ids = sorted(
                {
                    int(item["nm_id"])
                    for item in candidate_rows
                    if int(item["nm_id"]) > 0
                }
            )
            warehouse_metrics = _warehouse_metric_lookup(
                candidate_rows,
                version_id=str(candidate["target_version_id"]),
                published_at=str(candidate["published_at"]),
                source_watermarks=dict(candidate["source_watermarks"]),
                requested_nm_ids=roster_nm_ids,
            )
            from packages.application.calculation_parameters import (
                CalculationParametersBlock,
            )
            from packages.application.calculation_parameters_v4 import (
                load_proxy_v4_parameters_for_date,
            )
            from packages.application.inventory_cost_blend import (
                build_inventory_cost_blend_lookup,
            )
            from packages.application.warehouse_functional_economics_backfill import (
                _transform_snapshot,
            )

            wb_compat = self.runtime.load_our_wb_cost_daily_state(
                as_of_date=target_date
            )
            costs = build_inventory_cost_blend_lookup(
                as_of_date=target_date,
                wb_compat_lookup=wb_compat,
                product_capital_lookup=warehouse_metrics,
            )
            params = CalculationParametersBlock(
                runtime=self.runtime
            ).parameters_for_date(target_date)
            proxy_v4_params = load_proxy_v4_parameters_for_date(
                runtime=self.runtime,
                effective_date=target_date,
            )
            source_fingerprint = _fingerprint(
                {
                    "candidate_fingerprint": candidate["candidate_fingerprint"],
                    "costs": costs,
                    "calculation_parameters": params.public(),
                    "proxy_v4_parameters": (
                        proxy_v4_params.public()
                        if proxy_v4_params is not None
                        else None
                    ),
                }
            )
            updates: list[dict[str, Any]] = []
            before_missing: set[str] = set()
            after_missing: set[str] = set()
            affected_positive_order_skus: set[int] = set()
            for snapshot in snapshots:
                try:
                    before_payload = json.loads(str(snapshot["plan_json"]))
                except json.JSONDecodeError:
                    return _blocked_plan(
                        UNSAFE_AMBIGUOUS,
                        "ready_snapshot_shape_invalid",
                        business_date=target_date,
                        facility_id=facility_id,
                        pool=target_pool,
                        nm_ids=targets,
                        source_version_id=str(active["version_id"]),
                        details={
                            "bundle_version": str(snapshot["bundle_version"]),
                            "as_of_date": str(snapshot["as_of_date"]),
                        },
                    )
                before_cells = _ready_cells(before_payload, target_date)
                if not before_cells:
                    return _blocked_plan(
                        UNSAFE_AMBIGUOUS,
                        "ready_snapshot_shape_invalid",
                        business_date=target_date,
                        facility_id=facility_id,
                        pool=target_pool,
                        nm_ids=targets,
                        source_version_id=str(active["version_id"]),
                        details={
                            "bundle_version": str(snapshot["bundle_version"]),
                            "as_of_date": str(snapshot["as_of_date"]),
                        },
                    )
                if (
                    _cell_decimal(before_cells.get(f"SKU:{target_nm_id}|orderSum"))
                    > ZERO
                ):
                    affected_positive_order_skus.add(target_nm_id)
                before_missing.update(
                    key
                    for key in CRITICAL_TOTAL_METRIC_KEYS
                    if before_cells.get(f"TOTAL|{key}") in {None, ""}
                )
                transformed = _transform_snapshot(
                    snapshot,
                    costs={target_date: costs},
                    warehouse_metrics={target_date: warehouse_metrics},
                    warehouse_exact_dates={target_date},
                    warehouse_covered_nm_ids={target_date: set(roster_nm_ids)},
                    warehouse_version_ids={
                        target_date: str(candidate["target_version_id"])
                    },
                    parameters={target_date: params},
                    proxy_v4_parameters={target_date: proxy_v4_params},
                    source_fingerprint=source_fingerprint,
                    cutover_business_date=target_date,
                    operation_business_date=current_business_date_iso(),
                    affected_nm_ids=targets,
                    earliest_business_date=target_date,
                    latest_business_date=target_date,
                )
                after_payload = json.loads(str(transformed["after_plan_json"]))
                after_cells = _ready_cells(after_payload, target_date)
                after_missing.update(
                    key
                    for key in CRITICAL_TOTAL_METRIC_KEYS
                    if after_cells.get(f"TOTAL|{key}") in {None, ""}
                )
                updates.append(
                    {
                        "bundle_version": str(snapshot["bundle_version"]),
                        "as_of_date": str(snapshot["as_of_date"]),
                        "before_plan_sha256": _sha_text(str(snapshot["plan_json"])),
                        "after_plan_sha256": _sha_text(
                            str(transformed["after_plan_json"])
                        ),
                        "after_plan_json": str(transformed["after_plan_json"]),
                        "changed_cells": int(transformed["changed_cells"]),
                        "non_target_digest": str(transformed["non_target_after"]),
                    }
                )
            ready_after_bytes = sum(
                len(str(item["after_plan_json"]).encode("utf-8")) for item in updates
            )
            if ready_after_bytes > MAX_READY_CLOSURE_BYTES:
                return _blocked_plan(
                    UNSAFE_AMBIGUOUS,
                    "ready_snapshot_scope_missing_or_broad",
                    business_date=target_date,
                    facility_id=facility_id,
                    pool=target_pool,
                    nm_ids=targets,
                    source_version_id=str(active["version_id"]),
                    details={
                        "snapshot_count": len(snapshots),
                        "ready_after_bytes": ready_after_bytes,
                        "max_ready_closure_bytes": MAX_READY_CLOSURE_BYTES,
                    },
                )
            if after_missing:
                return _blocked_plan(
                    UNSAFE_AMBIGUOUS,
                    "critical_total_dependency_still_missing",
                    business_date=target_date,
                    facility_id=facility_id,
                    pool=target_pool,
                    nm_ids=targets,
                    source_version_id=str(active["version_id"]),
                    details={"missing_metric_keys": sorted(after_missing)},
                )
            source_material = _source_material(
                conn, str(active["version_id"]), nm_ids=targets
            )
            ready_before_digest = _fingerprint(
                [
                    [
                        item["bundle_version"],
                        item["as_of_date"],
                        item["before_plan_sha256"],
                    ]
                    for item in updates
                ]
            )
            ready_after_digest = _fingerprint(
                [
                    [
                        item["bundle_version"],
                        item["as_of_date"],
                        item["after_plan_sha256"],
                    ]
                    for item in updates
                ]
            )
            typed_evidence = {
                "affected_positive_order_sku_count": len(affected_positive_order_skus),
                "affected_positive_order_nm_ids": sorted(affected_positive_order_skus),
                "missing_critical_total_dependencies_before": sorted(before_missing),
                "missing_critical_total_dependencies_after": sorted(after_missing),
                "invariant_mismatch": {
                    "source_functional_version_id": str(active["version_id"]),
                    "mismatched_nm_ids": mismatches,
                    "reason_codes": _mismatch_reason_codes(
                        conn, str(active["version_id"]), target_nm_id
                    ),
                },
                "repairability": REPAIRABLE,
                "candidate_identity": {
                    "version_id": str(candidate["target_version_id"]),
                    "candidate_fingerprint": str(candidate["candidate_fingerprint"]),
                    "business_date": target_date,
                    "facility_id": str(facility_id),
                    "pool": "FBS",
                    "nm_id": target_nm_id,
                    "canonical_source_evidence": target_source_evidence,
                },
                "readback_identity": {
                    "active_version_id": str(candidate["target_version_id"]),
                    "ready_snapshot_digest": ready_after_digest,
                },
            }
            plan = {
                "contract_name": CONTRACT_NAME,
                "status": REPAIRABLE,
                "business_date": target_date,
                "facility_id": str(facility_id),
                "pool": "FBS",
                "nm_ids": targets,
                "source_version_id": str(active["version_id"]),
                "target_version_id": str(candidate["target_version_id"]),
                "source_material_digest": _fingerprint(source_material),
                "roster_digest": str(candidate["roster_digest"]),
                "provenance_digest": str(candidate["provenance_digest"]),
                "candidate": candidate,
                "ready_updates": updates,
                "ready_before_digest": ready_before_digest,
                "ready_after_digest": ready_after_digest,
                "typed_evidence": typed_evidence,
                "bounds": {
                    "target_count": 1,
                    "ready_snapshot_count": len(updates),
                    "ready_before_bytes": ready_before_bytes,
                    "ready_after_bytes": ready_after_bytes,
                    "functional_balance_rows": len(candidate_rows),
                    "max_persisted_plan_bytes": MAX_PERSISTED_PLAN_BYTES,
                    "full_database_copy": False,
                    "external_source_calls": 0,
                    "full_day_reload": False,
                },
            }
            plan["plan_fingerprint"] = _fingerprint(plan)
            plan["operation_id"] = "whfbsm_" + str(plan["plan_fingerprint"])[7:31]
            plan_bytes = len(_json(plan).encode("utf-8"))
            if plan_bytes > MAX_PERSISTED_PLAN_BYTES:
                return _blocked_plan(
                    UNSAFE_AMBIGUOUS,
                    "material_plan_scope_too_broad",
                    business_date=target_date,
                    facility_id=facility_id,
                    pool=target_pool,
                    nm_ids=targets,
                    source_version_id=str(active["version_id"]),
                    details={
                        "plan_bytes": plan_bytes,
                        "max_persisted_plan_bytes": MAX_PERSISTED_PLAN_BYTES,
                    },
                )
            return plan

    def build_historical_plan(
        self,
        manifest: Mapping[str, Any],
        *,
        dependency_conn: sqlite3.Connection,
    ) -> dict[str, Any]:
        """Build one immutable historical repair without reading current pool as source."""

        if not bool(int(dependency_conn.execute("PRAGMA query_only").fetchone()[0])):
            raise WarehouseFbsMaterialError(
                "historical_dependency_session_not_query_only",
                "Historical qualification requires the explicitly bound query-only dependency session",
            )
        dependency_conn.row_factory = sqlite3.Row

        bound = deepcopy(dict(manifest))
        required_manifest_fields = {
            "schema",
            "business_date",
            "facility_id",
            "pool",
            "nm_ids",
            "accepted_version_id",
            "accepted_version_plan_digest",
            "accepted_version_row_digest",
            "accepted_target_row_digest",
            "accepted_provenance_digest",
            "accepted_effective_at",
            "accepted_published_at",
            "expected_current_active_version_id",
            "expected_current_sync_version_id",
            "expected_current_pool_digest",
            "event_id",
            "event_source_digest",
            "event_status_digest",
            "event_evidence_digest",
            "event_row_digest",
            "event_quantity_delta",
            "event_capital_delta_rub",
            "event_wac_rub",
            "event_occurred_at",
            "accepted_quantity",
            "accepted_cost_covered_quantity",
            "accepted_capital_rub",
            "canonical_target",
            "storage_generation",
        }
        actual_manifest_fields = set(bound)
        if actual_manifest_fields != required_manifest_fields:
            return _blocked_plan(
                UNSAFE_AMBIGUOUS,
                "historical_manifest_fields_invalid",
                business_date=str(bound.get("business_date") or ""),
                facility_id=str(bound.get("facility_id") or ""),
                pool=str(bound.get("pool") or "").upper(),
                nm_ids=bound.get("nm_ids") or [],
                source_version_id=str(bound.get("accepted_version_id") or ""),
                details={
                    "missing_fields": sorted(
                        required_manifest_fields - actual_manifest_fields
                    ),
                    "extra_fields": sorted(
                        actual_manifest_fields - required_manifest_fields
                    ),
                },
            )
        if bound.get("schema") != HISTORICAL_MANIFEST_SCHEMA:
            return _blocked_plan(
                UNSAFE_AMBIGUOUS,
                "historical_manifest_schema_invalid",
                business_date=str(bound.get("business_date") or ""),
                facility_id=str(bound.get("facility_id") or ""),
                pool=str(bound.get("pool") or "").upper(),
                nm_ids=bound.get("nm_ids") or [],
                source_version_id=str(bound.get("accepted_version_id") or ""),
            )
        missing_manifest_fields = sorted(
            key
            for key in required_manifest_fields
            if bound.get(key) is None
            or (isinstance(bound.get(key), (str, list, tuple)) and not bound.get(key))
        )
        if missing_manifest_fields:
            return _blocked_plan(
                UNSAFE_AMBIGUOUS,
                "historical_manifest_binding_missing",
                business_date=str(bound.get("business_date") or ""),
                facility_id=str(bound.get("facility_id") or ""),
                pool=str(bound.get("pool") or "").upper(),
                nm_ids=bound.get("nm_ids") or [],
                source_version_id=str(bound.get("accepted_version_id") or ""),
                details={"missing_fields": missing_manifest_fields},
            )
        typed_digest_fields = (
            "accepted_version_plan_digest",
            "accepted_version_row_digest",
            "accepted_target_row_digest",
            "accepted_provenance_digest",
            "expected_current_pool_digest",
            "event_source_digest",
            "event_status_digest",
            "event_evidence_digest",
            "event_row_digest",
        )
        invalid_digest_fields = [
            key for key in typed_digest_fields if not _is_typed_sha256(bound.get(key))
        ]
        if invalid_digest_fields:
            return _blocked_plan(
                UNSAFE_AMBIGUOUS,
                "historical_manifest_digest_invalid",
                business_date=str(bound.get("business_date") or ""),
                facility_id=str(bound.get("facility_id") or ""),
                pool=str(bound.get("pool") or "").upper(),
                nm_ids=bound.get("nm_ids") or [],
                source_version_id=str(bound.get("accepted_version_id") or ""),
                details={"invalid_digest_fields": invalid_digest_fields},
            )
        target_date = _iso_date(bound.get("business_date"))
        facility_id = str(bound.get("facility_id") or "")
        pool = str(bound.get("pool") or "").upper()
        raw_targets = bound.get("nm_ids") or []
        targets = sorted(int(value) for value in raw_targets)
        source_version_id = str(bound.get("accepted_version_id") or "")
        event_id = str(bound.get("event_id") or "")
        if (
            pool != "FBS"
            or len(targets) != 1
            or len(targets) != len(set(targets))
            or targets[0] <= 0
            or not facility_id
            or not bool(dict(bound.get("canonical_target") or {}).get("accepted"))
            or bool(dict(bound.get("storage_generation") or {}).get("implicit"))
            or not bool(dict(bound.get("storage_generation") or {}).get("query_only"))
        ):
            return _blocked_plan(
                UNSAFE_AMBIGUOUS,
                "historical_manifest_scope_invalid",
                business_date=target_date,
                facility_id=facility_id,
                pool=pool,
                nm_ids=targets,
                source_version_id=source_version_id,
            )
        # The caller owns this one StoreRegistry-bound dependency session.  The
        # planner neither resolves storage again nor opens a hidden connection.
        with nullcontext(dependency_conn) as conn:
            _require_material_schema(conn)
            active = _active_version(conn)
            if active is None or str(active["version_id"]) != str(
                bound.get("expected_current_active_version_id") or ""
            ):
                return _blocked_plan(
                    UNSAFE_AMBIGUOUS,
                    "current_active_version_manifest_drift",
                    business_date=target_date,
                    facility_id=facility_id,
                    pool=pool,
                    nm_ids=targets,
                    source_version_id=source_version_id,
                )
            sync = conn.execute(
                "SELECT active_version_id FROM sheet_vitrina_v1_warehouse_wb_sync_status "
                "WHERE slot=1"
            ).fetchone()
            current_pool_digest = _fingerprint(
                _source_material(
                    conn,
                    str(active["version_id"]),
                    nm_ids=targets,
                )["pool_rows"]
            )
            if (
                sync is None
                or str(sync[0])
                != str(bound.get("expected_current_sync_version_id") or "")
                or current_pool_digest
                != str(bound.get("expected_current_pool_digest") or "")
            ):
                return _blocked_plan(
                    UNSAFE_AMBIGUOUS,
                    "current_sync_or_pool_manifest_drift",
                    business_date=target_date,
                    facility_id=facility_id,
                    pool=pool,
                    nm_ids=targets,
                    source_version_id=source_version_id,
                )
            source = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_functional_versions "
                "WHERE version_id=? AND status='good'",
                (source_version_id,),
            ).fetchone()
            if (
                source is None
                or str(source["business_effective_date"] or "") != target_date
            ):
                return _blocked_plan(
                    UNSAFE_AMBIGUOUS,
                    "accepted_historical_version_missing_or_drifted",
                    business_date=target_date,
                    facility_id=facility_id,
                    pool=pool,
                    nm_ids=targets,
                    source_version_id=source_version_id,
                )
            version_field_bindings = {
                "accepted_effective_at": str(source["effective_at"]),
                "accepted_published_at": str(source["published_at"]),
            }
            if any(
                bound.get(key) and str(bound[key]) != actual
                for key, actual in version_field_bindings.items()
            ):
                return _blocked_plan(
                    UNSAFE_AMBIGUOUS,
                    "accepted_historical_version_timestamp_drift",
                    business_date=target_date,
                    facility_id=facility_id,
                    pool=pool,
                    nm_ids=targets,
                    source_version_id=source_version_id,
                    details=version_field_bindings,
                )
            target_row = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances "
                "WHERE version_id=? AND warehouse_key='ff' AND nm_id=?",
                (source_version_id, targets[0]),
            ).fetchone()
            target_provenance = (
                _loads(target_row["provenance_json"], {})
                if target_row is not None
                else {}
            )
            version_digests = {
                "accepted_version_plan_digest": str(source["plan_fingerprint"]),
                "accepted_version_row_digest": _fingerprint(dict(source)),
                "accepted_target_row_digest": (
                    _fingerprint(dict(target_row)) if target_row is not None else ""
                ),
                "accepted_provenance_digest": _fingerprint(target_provenance),
            }
            if any(
                str(bound[key]) != actual for key, actual in version_digests.items()
            ):
                return _blocked_plan(
                    UNSAFE_AMBIGUOUS,
                    "accepted_historical_typed_digest_drift",
                    business_date=target_date,
                    facility_id=facility_id,
                    pool=pool,
                    nm_ids=targets,
                    source_version_id=source_version_id,
                    details=version_digests,
                )
            event = conn.execute(
                """SELECT event_id,cutover_id,order_id,episode_sequence,event_type,
                          source_order_observation_sequence,
                          source_status_observation_sequence,source_revision,
                          status_digest,supplier_status,wb_status,source_observed_at,
                          facility_id,pool,nm_id,quantity,physical_quantity_delta,
                          capital_delta_rub,frozen_wac_rub,evidence_digest,occurred_at
                     FROM sheet_vitrina_v1_ff_pool_fbs_lifecycle_events
                    WHERE event_id=?""",
                (event_id,),
            ).fetchone()
            target_nm_id = targets[0]
            if (
                event is None
                or str(event["event_type"]) != "handoff_debit"
                or str(event["facility_id"]) != facility_id
                or str(event["pool"]).upper() != "FBS"
                or int(event["nm_id"]) != target_nm_id
                or int(event["physical_quantity_delta"]) >= 0
                or Decimal(str(event["capital_delta_rub"])) >= ZERO
            ):
                return _blocked_plan(
                    UNSAFE_AMBIGUOUS,
                    "immutable_historical_event_missing_or_drifted",
                    business_date=target_date,
                    facility_id=facility_id,
                    pool=pool,
                    nm_ids=targets,
                    source_version_id=source_version_id,
                )
            event_value_bindings = {
                "event_quantity_delta": str(event["physical_quantity_delta"]),
                "event_capital_delta_rub": str(event["capital_delta_rub"]),
                "event_wac_rub": str(event["frozen_wac_rub"]),
                "event_occurred_at": str(event["occurred_at"]),
            }
            if any(
                bound.get(key) is not None and str(bound[key]) != actual
                for key, actual in event_value_bindings.items()
            ):
                return _blocked_plan(
                    UNSAFE_AMBIGUOUS,
                    "immutable_historical_event_value_drift",
                    business_date=target_date,
                    facility_id=facility_id,
                    pool=pool,
                    nm_ids=targets,
                    source_version_id=source_version_id,
                    details=event_value_bindings,
                )
            event_checks = {
                "event_source_digest": _fingerprint(str(event["source_revision"])),
                "event_status_digest": str(event["status_digest"]),
                "event_evidence_digest": str(event["evidence_digest"]),
                "event_row_digest": _fingerprint(dict(event)),
            }
            if any(str(bound[key]) != actual for key, actual in event_checks.items()):
                return _blocked_plan(
                    UNSAFE_AMBIGUOUS,
                    "immutable_historical_event_digest_drift",
                    business_date=target_date,
                    facility_id=facility_id,
                    pool=pool,
                    nm_ids=targets,
                    source_version_id=source_version_id,
                    details=event_checks,
                )
            try:
                aggregate = _historical_event_aggregate(
                    conn,
                    source_version_id=source_version_id,
                    facility_id=facility_id,
                    nm_id=target_nm_id,
                    event=dict(event),
                )
                accepted_value_bindings = {
                    "accepted_quantity": str(aggregate["accepted_quantity"]),
                    "accepted_cost_covered_quantity": str(
                        aggregate["accepted_cost_covered_quantity"]
                    ),
                    "accepted_capital_rub": str(aggregate["capital_rub"]),
                }
                if any(
                    bound.get(key) is not None and str(bound[key]) != actual
                    for key, actual in accepted_value_bindings.items()
                ):
                    raise WarehouseFbsMaterialError(
                        "accepted_historical_balance_value_drift",
                        "Accepted target quantity/capital/coverage changed",
                        details=accepted_value_bindings,
                    )
                candidate = _build_candidate(
                    conn,
                    affected_nm_ids=targets,
                    source_kind="fbs_historical_material_recovery",
                    source_id=f"{source_version_id}:{event_id}",
                    business_date=target_date,
                    published_at=str(self.timestamp_factory()),
                    allow_source_mismatch=True,
                    source_version_id_override=source_version_id,
                    target_aggregates_override={target_nm_id: aggregate},
                    version_kind="fbs_historical_material_revision",
                )
                updates, closure = _ready_updates_for_candidate(
                    runtime=self.runtime,
                    conn=conn,
                    candidate=candidate,
                    target_date=target_date,
                    targets=targets,
                )
            except WarehouseFbsMaterialError as exc:
                return _blocked_plan(
                    UNSAFE_AMBIGUOUS,
                    exc.code,
                    business_date=target_date,
                    facility_id=facility_id,
                    pool=pool,
                    nm_ids=targets,
                    source_version_id=source_version_id,
                    details=exc.details,
                )
            source_material = _source_material(conn, source_version_id, nm_ids=targets)
            ready_before_digest = _fingerprint(
                [
                    [
                        item["bundle_version"],
                        item["as_of_date"],
                        item["before_plan_sha256"],
                    ]
                    for item in updates
                ]
            )
            ready_after_digest = _fingerprint(
                [
                    [
                        item["bundle_version"],
                        item["as_of_date"],
                        item["after_plan_sha256"],
                    ]
                    for item in updates
                ]
            )
            candidate_target = next(
                item
                for item in candidate["lines"]
                if str(item["warehouse_key"]) == STAGE_FF
                and int(item["nm_id"]) == target_nm_id
            )
            candidate_target_semantic_row = {
                key: candidate_target.get(key)
                for key in (
                    "quantity",
                    "wac_rub",
                    "capital_rub",
                    "cost_covered_quantity",
                    "provenance_json",
                )
            }
            ready_closure = [
                {
                    "bundle_version": str(item["bundle_version"]),
                    "as_of_date": str(item["as_of_date"]),
                    "after_plan_sha256": str(item["after_plan_sha256"]),
                    "non_target_digest": str(item["non_target_digest"]),
                }
                for item in updates
            ]
            typed_evidence = {
                "repair_mode": "historical",
                "accepted_version_id": source_version_id,
                "immutable_event": {"event_id": event_id, **event_checks},
                "before": {
                    "quantity": aggregate["accepted_quantity"],
                    "cost_covered_quantity": aggregate[
                        "accepted_cost_covered_quantity"
                    ],
                },
                "after": {
                    "quantity": aggregate["quantity"],
                    "cost_covered_quantity": aggregate["quantity"],
                    "wac_rub": aggregate["wac_rub"],
                },
                "economics_dependency_closure": closure,
                "current_preservation": {
                    "active_version_id": str(active["version_id"]),
                    "sync_version_id": str(sync[0]),
                    "pool_rows_digest": current_pool_digest,
                },
                "readback_identity": {
                    "active_version_id": str(active["version_id"]),
                    "historical_version_id": str(candidate["target_version_id"]),
                    "ready_snapshot_digest": ready_after_digest,
                    "sync_version_id": str(sync[0]),
                    "current_pool_digest": current_pool_digest,
                    "historical_target_row_digest": _fingerprint(
                        candidate_target_semantic_row
                    ),
                    "historical_target_provenance_digest": _fingerprint(
                        _loads(candidate_target["provenance_json"], {})
                    ),
                    "business_projection_revision_digest": _fingerprint(
                        {
                            "status": "active",
                            "source_revision": str(candidate["candidate_fingerprint"]),
                            "business_effective_date": target_date,
                        }
                    ),
                    "ready_target_total_non_target_closure_digest": _fingerprint(
                        ready_closure
                    ),
                },
            }
            plan = {
                "contract_name": CONTRACT_NAME,
                "mode": "historical",
                "status": REPAIRABLE,
                "business_date": target_date,
                "facility_id": facility_id,
                "pool": "FBS",
                "nm_ids": targets,
                "source_version_id": source_version_id,
                "expected_active_version_id": str(active["version_id"]),
                "target_version_id": str(candidate["target_version_id"]),
                "source_material_digest": _fingerprint(source_material),
                "roster_digest": str(candidate["roster_digest"]),
                "provenance_digest": str(candidate["provenance_digest"]),
                "candidate": candidate,
                "ready_updates": updates,
                "ready_before_digest": ready_before_digest,
                "ready_after_digest": ready_after_digest,
                "typed_evidence": typed_evidence,
                "historical_manifest": bound,
                "bounds": {
                    "target_count": 1,
                    "ready_snapshot_count": len(updates),
                    "functional_balance_rows": len(candidate["lines"]),
                    "full_database_copy": False,
                    "external_source_calls": 0,
                    "full_day_reload": False,
                    "current_pool_rows_used_as_candidate_source": False,
                },
            }
            plan["plan_fingerprint"] = _fingerprint(plan)
            plan["operation_id"] = "whfbsm_" + str(plan["plan_fingerprint"])[7:31]
            if len(_json(plan).encode("utf-8")) > MAX_PERSISTED_PLAN_BYTES:
                return _blocked_plan(
                    UNSAFE_AMBIGUOUS,
                    "material_plan_scope_too_broad",
                    business_date=target_date,
                    facility_id=facility_id,
                    pool=pool,
                    nm_ids=targets,
                    source_version_id=source_version_id,
                )
            return plan

    def apply_plan(
        self,
        plan: Mapping[str, Any],
        *,
        confirm_fingerprint: str,
        approval_reference: str = "",
        actor: str = "",
        transport_hook: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        normalized = deepcopy(dict(plan))
        fingerprint = str(normalized.get("plan_fingerprint") or "")
        if (
            normalized.get("status") != REPAIRABLE
            or not fingerprint
            or fingerprint != str(confirm_fingerprint)
            or fingerprint
            != _fingerprint(
                {
                    key: value
                    for key, value in normalized.items()
                    if key not in {"plan_fingerprint", "operation_id"}
                }
            )
        ):
            raise WarehouseFbsMaterialError(
                "repair_plan_fingerprint_mismatch",
                "Exact repairable material plan fingerprint is required",
            )
        expected_operation_id = "whfbsm_" + fingerprint[7:31]
        if str(normalized.get("operation_id") or "") != expected_operation_id:
            raise WarehouseFbsMaterialError(
                "repair_operation_identity_mismatch",
                "Material repair orchestration identity is not deterministic",
            )
        historical = str(normalized.get("mode") or "") == "historical"
        if historical and (
            not str(approval_reference).strip() or not str(actor).strip()
        ):
            raise WarehouseFbsMaterialError(
                "historical_repair_owner_gate_missing",
                "Historical repair requires an approval reference and exact actor",
            )
        ready_updates = list(normalized.get("ready_updates") or [])
        candidate_payload = normalized.get("candidate")
        candidate_lines = list(
            candidate_payload.get("lines") or []
            if isinstance(candidate_payload, Mapping)
            else []
        )
        ready_bytes = sum(
            len(str(item.get("after_plan_json") or "").encode("utf-8"))
            if isinstance(item, Mapping)
            else MAX_READY_CLOSURE_BYTES + 1
            for item in ready_updates
        )
        if (
            str(normalized.get("pool") or "").upper() != "FBS"
            or len(list(normalized.get("nm_ids") or [])) != MAX_REPAIR_TARGETS
            or not ready_updates
            or len(ready_updates) > MAX_READY_SNAPSHOTS
            or not candidate_lines
            or len(candidate_lines) > MAX_FUNCTIONAL_BALANCE_ROWS
            or ready_bytes > MAX_READY_CLOSURE_BYTES
            or len(_json(normalized).encode("utf-8")) > MAX_PERSISTED_PLAN_BYTES
        ):
            raise WarehouseFbsMaterialError(
                "repair_plan_scope_too_broad",
                "Material repair plan exceeds the exact bounded closure",
            )
        now = str(self.timestamp_factory())
        before_barrier = barrier_status(self.runtime.runtime_dir)
        if before_barrier.get("active") is not False:
            raise WarehouseFbsMaterialError(
                "historical_write_barrier_active",
                "Historical repair requires one inactive read-only barrier readback",
            )
        with warehouse_functional_write_lock(self.runtime.runtime_dir):
            under_lock_barrier = barrier_status(self.runtime.runtime_dir)
            if under_lock_barrier.get("active") is not False or under_lock_barrier.get(
                "status"
            ) != before_barrier.get("status"):
                raise WarehouseFbsMaterialError(
                    "historical_write_barrier_drift",
                    "Historical write barrier changed at the shared-lock boundary",
                )
            pre_existing = self._intent(expected_operation_id)
            if pre_existing and str(pre_existing["status"]) == REPAIRED:
                return self._repaired_readback(normalized, idempotent=True)
            if pre_existing and str(pre_existing["status"]) in {
                RETRY_EXHAUSTED,
                HISTORICAL_RECOVERY_REQUIRED,
                UNSAFE_AMBIGUOUS,
            }:
                return _intent_public(pre_existing)
            # The durable intent is a business mutation.  It is created only
            # after the first under-lock CAS qualification; planning and any
            # pre-lock failure remain byte-for-byte inert.
            with _connect(self.runtime.db_path) as intent_conn:
                ensure_warehouse_fbs_material_schema(intent_conn)
                intent_conn.execute("BEGIN IMMEDIATE")
                self._revalidate_plan_cas(intent_conn, normalized)
                self._ensure_intent(
                    intent_conn,
                    normalized,
                    created_at=now,
                    owner_gate=(
                        {
                            "approval_reference_digest": _fingerprint(
                                str(approval_reference)
                            ),
                            "actor": str(actor),
                        }
                        if historical
                        else None
                    ),
                )
                intent_conn.commit()
            if not self._begin_attempt(normalized, started_at=now):
                return _intent_public(self._intent(expected_operation_id))  # type: ignore[arg-type]
            try:
                with _connect(self.runtime.db_path) as conn:
                    ensure_warehouse_fbs_material_schema(conn)
                    conn.execute("BEGIN IMMEDIATE")
                    self._revalidate_plan_cas(conn, normalized)
                    result = _persist_candidate_and_switch(
                        conn,
                        candidate=dict(normalized["candidate"]),
                        ready_updates=list(normalized["ready_updates"]),
                        expected_active_version_id=str(
                            normalized.get("expected_active_version_id")
                            or normalized["source_version_id"]
                        ),
                        switch_active=not historical,
                    )
                    readback = _readback_identity(conn, normalized)
                    if (
                        readback
                        != dict(normalized["typed_evidence"])["readback_identity"]
                    ):
                        raise WarehouseFbsMaterialError(
                            "repair_readback_mismatch",
                            "Candidate material publication readback differs from the plan",
                            details={"actual": readback},
                        )
                    _transition_intent(
                        conn,
                        operation_id=expected_operation_id,
                        status=REPAIRED,
                        evidence={"phase": "exact_readback", "readback": readback},
                        created_at=now,
                    )
                    if transport_hook is not None:
                        transport_hook("before_commit")
                    conn.commit()
                if transport_hook is not None:
                    transport_hook("after_commit")
                return {
                    **self._repaired_readback(normalized, idempotent=False),
                    "publication": result,
                }
            except Exception as exc:
                # A response lost after commit is reconciled by the exact
                # target identities.  No publication statement is repeated.
                reconciled = self._reconcile_after_error(normalized)
                if reconciled is not None:
                    return reconciled
                return self._record_recoverable_failure(
                    normalized,
                    error=exc,
                    failed_at=str(self.timestamp_factory()),
                )

    def resume(
        self,
        *,
        operation_id: str,
        approval_reference: str = "",
        actor: str = "",
        transport_hook: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Resume one durable bounded plan after process/transport loss."""

        row = self._intent(str(operation_id))
        if row is None:
            raise WarehouseFbsMaterialError(
                "repair_intent_missing", "Durable material repair intent is missing"
            )
        plan = _loads(row["plan_json"], {})
        if not isinstance(plan, Mapping) or not plan:
            raise WarehouseFbsMaterialError(
                "repair_intent_plan_missing",
                "Durable material repair plan is missing or invalid",
            )
        if str(plan.get("operation_id") or "") != str(operation_id) or str(
            plan.get("plan_fingerprint") or ""
        ) != str(row["plan_fingerprint"]):
            raise WarehouseFbsMaterialError(
                "repair_intent_plan_identity_conflict",
                "Durable material repair plan differs from its intent identity",
            )
        return self.apply_plan(
            plan,
            confirm_fingerprint=str(row["plan_fingerprint"]),
            approval_reference=str(approval_reference),
            actor=str(actor),
            transport_hook=transport_hook,
        )

    def readback(self, *, operation_id: str) -> dict[str, Any]:
        """Query-only durable-intent and exact target reconciliation."""

        with _connect_readonly(self.runtime.db_path) as conn:
            _require_material_schema(conn)
            row = conn.execute(
                f"SELECT * FROM {INTENTS_TABLE} WHERE operation_id=?",
                (str(operation_id),),
            ).fetchone()
            if row is None:
                return {"contract_name": CONTRACT_NAME, "status": "not_found"}
            result = _intent_public(row)
            if str(row["status"]) == REPAIRED:
                plan = _loads(row["plan_json"], {})
                result["readback_identity"] = _readback_identity(conn, plan)
            result["query_only"] = True
            return result

    def _begin_attempt(self, plan: Mapping[str, Any], *, started_at: str) -> bool:
        """Persist the retry identity before the possibly ambiguous commit."""

        with _connect(self.runtime.db_path) as conn:
            ensure_warehouse_fbs_material_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT status,attempt_count FROM {INTENTS_TABLE} WHERE operation_id=?",
                (str(plan["operation_id"]),),
            ).fetchone()
            if row is None:
                raise WarehouseFbsMaterialError(
                    "repair_intent_missing",
                    "Durable material repair intent disappeared",
                )
            if str(row["status"]) == REPAIRED:
                conn.rollback()
                return False
            if int(row["attempt_count"]) >= MAX_RETRY_ATTEMPTS:
                _transition_intent(
                    conn,
                    operation_id=str(plan["operation_id"]),
                    status=RETRY_EXHAUSTED,
                    evidence={"phase": "retry_budget_exhausted"},
                    created_at=started_at,
                )
                conn.commit()
                return False
            _transition_intent(
                conn,
                operation_id=str(plan["operation_id"]),
                status=REPAIRING,
                evidence={"phase": "bounded_writer_started"},
                created_at=started_at,
                increment_attempt=True,
            )
            conn.commit()
            return True

    def _ensure_intent(
        self,
        conn: sqlite3.Connection,
        plan: Mapping[str, Any],
        *,
        created_at: str,
        owner_gate: Mapping[str, Any] | None = None,
    ) -> None:
        existing = conn.execute(
            f"SELECT * FROM {INTENTS_TABLE} WHERE operation_id=?",
            (str(plan["operation_id"]),),
        ).fetchone()
        if existing is None:
            conn.execute(
                f"""INSERT INTO {INTENTS_TABLE}(
                           operation_id,business_date,facility_id,pool,nm_id,
                           source_version_id,target_version_id,plan_fingerprint,plan_json,
                           source_material_digest,roster_digest,provenance_digest,
                           ready_before_digest,ready_after_digest,status,attempt_count,
                           typed_evidence_json,last_error,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'repairable',0,?,NULL,?,?)""",
                (
                    str(plan["operation_id"]),
                    str(plan["business_date"]),
                    str(plan["facility_id"]),
                    "FBS",
                    int(plan["nm_ids"][0]),
                    str(plan["source_version_id"]),
                    str(plan["target_version_id"]),
                    str(plan["plan_fingerprint"]),
                    _json(plan),
                    str(plan["source_material_digest"]),
                    str(plan["roster_digest"]),
                    str(plan["provenance_digest"]),
                    str(plan["ready_before_digest"]),
                    str(plan["ready_after_digest"]),
                    _json(plan["typed_evidence"]),
                    created_at,
                    created_at,
                ),
            )
            _append_intent_event(
                conn,
                operation_id=str(plan["operation_id"]),
                status=REPAIRABLE,
                evidence={
                    "phase": "durable_plan",
                    **(
                        {"owner_gate": dict(owner_gate)}
                        if owner_gate is not None
                        else {}
                    ),
                },
                created_at=created_at,
            )
        elif str(existing["plan_fingerprint"]) != str(
            plan["plan_fingerprint"]
        ) or _fingerprint(_loads(existing["plan_json"], {})) != _fingerprint(plan):
            raise WarehouseFbsMaterialError(
                "repair_orchestration_identity_conflict",
                "Existing material intent has another exact plan identity",
            )
        elif owner_gate is not None:
            first_event = conn.execute(
                f"SELECT evidence_json FROM {EVENTS_TABLE} "
                "WHERE operation_id=? AND status=? "
                "ORDER BY event_sequence LIMIT 1",
                (str(plan["operation_id"]), REPAIRABLE),
            ).fetchone()
            persisted_gate = (
                dict(_loads(first_event[0], {}).get("owner_gate") or {})
                if first_event is not None
                else {}
            )
            if persisted_gate != dict(owner_gate):
                raise WarehouseFbsMaterialError(
                    "historical_repair_owner_gate_conflict",
                    "Historical repair resume owner evidence changed",
                )

    def _intent(self, operation_id: str) -> sqlite3.Row | None:
        with _connect_readonly(self.runtime.db_path) as conn:
            _require_material_schema(conn)
            return conn.execute(
                f"SELECT * FROM {INTENTS_TABLE} WHERE operation_id=?",
                (operation_id,),
            ).fetchone()

    def _revalidate_plan_cas(
        self, conn: sqlite3.Connection, plan: Mapping[str, Any]
    ) -> None:
        active = _active_version(conn)
        expected_active = str(
            plan.get("expected_active_version_id") or plan["source_version_id"]
        )
        if active is None or str(active["version_id"]) != expected_active:
            raise WarehouseFbsMaterialError(
                "repair_active_version_cas_drift",
                "Active functional version changed after incident planning",
            )
        if str(plan.get("mode") or "") == "historical":
            manifest = dict(plan.get("historical_manifest") or {})
            sync = conn.execute(
                "SELECT active_version_id FROM sheet_vitrina_v1_warehouse_wb_sync_status "
                "WHERE slot=1"
            ).fetchone()
            current_pool_digest = _fingerprint(
                _source_material(conn, expected_active, nm_ids=plan["nm_ids"])[
                    "pool_rows"
                ]
            )
            if (
                sync is None
                or str(sync[0])
                != str(manifest.get("expected_current_sync_version_id") or "")
                or current_pool_digest
                != str(manifest.get("expected_current_pool_digest") or "")
            ):
                raise WarehouseFbsMaterialError(
                    "historical_current_preservation_cas_drift",
                    "Current sync or physical pool changed after historical planning",
                )
        material_source_version_id = (
            str(plan["source_version_id"])
            if str(plan.get("mode") or "") == "historical"
            else str(active["version_id"])
        )
        live_source_material_digest = _fingerprint(
            _source_material(conn, material_source_version_id, nm_ids=plan["nm_ids"])
        )
        if live_source_material_digest != str(plan["source_material_digest"]):
            raise WarehouseFbsMaterialError(
                "repair_source_material_cas_drift",
                "Functional/pool source material changed after incident planning",
                details={
                    "expected": str(plan["source_material_digest"]),
                    "actual": live_source_material_digest,
                },
            )
        candidate = dict(plan["candidate"])
        if str(plan.get("mode") or "") == "historical":
            manifest = dict(plan.get("historical_manifest") or {})
            source = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_functional_versions "
                "WHERE version_id=? AND status='good'",
                (str(plan["source_version_id"]),),
            ).fetchone()
            target_row = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances "
                "WHERE version_id=? AND warehouse_key='ff' AND nm_id=?",
                (str(plan["source_version_id"]), int(plan["nm_ids"][0])),
            ).fetchone()
            if source is None or target_row is None:
                raise WarehouseFbsMaterialError(
                    "historical_source_cas_drift",
                    "Accepted historical source disappeared after planning",
                )
            source_checks = {
                "accepted_version_plan_digest": str(source["plan_fingerprint"]),
                "accepted_version_row_digest": _fingerprint(dict(source)),
                "accepted_target_row_digest": _fingerprint(dict(target_row)),
                "accepted_provenance_digest": _fingerprint(
                    _loads(target_row["provenance_json"], {})
                ),
            }
            if any(
                str(manifest.get(key) or "") != value
                for key, value in source_checks.items()
            ):
                raise WarehouseFbsMaterialError(
                    "historical_source_cas_drift",
                    "Accepted historical row or provenance changed after planning",
                )
            event = conn.execute(
                """SELECT event_id,cutover_id,order_id,episode_sequence,event_type,
                          source_order_observation_sequence,
                          source_status_observation_sequence,source_revision,
                          status_digest,supplier_status,wb_status,source_observed_at,
                          facility_id,pool,nm_id,quantity,physical_quantity_delta,
                          capital_delta_rub,frozen_wac_rub,evidence_digest,occurred_at
                     FROM sheet_vitrina_v1_ff_pool_fbs_lifecycle_events
                    WHERE event_id=?""",
                (str(manifest.get("event_id") or ""),),
            ).fetchone()
            if event is None:
                raise WarehouseFbsMaterialError(
                    "historical_event_cas_drift",
                    "Immutable historical event disappeared after planning",
                )
            event_checks = {
                "event_source_digest": _fingerprint(str(event["source_revision"])),
                "event_status_digest": str(event["status_digest"]),
                "event_evidence_digest": str(event["evidence_digest"]),
                "event_row_digest": _fingerprint(dict(event)),
            }
            if any(
                manifest.get(key) and str(manifest[key]) != actual
                for key, actual in event_checks.items()
            ):
                raise WarehouseFbsMaterialError(
                    "historical_event_cas_drift",
                    "Immutable historical event digest changed after planning",
                )
            target_nm_id = int(plan["nm_ids"][0])
            aggregate = _historical_event_aggregate(
                conn,
                source_version_id=str(plan["source_version_id"]),
                facility_id=str(plan["facility_id"]),
                nm_id=target_nm_id,
                event=dict(event),
            )
            fresh = _build_candidate(
                conn,
                affected_nm_ids=plan["nm_ids"],
                source_kind=str(candidate["source_kind"]),
                source_id=str(candidate["source_id"]),
                business_date=str(plan["business_date"]),
                published_at=str(candidate["published_at"]),
                allow_source_mismatch=True,
                source_version_id_override=str(plan["source_version_id"]),
                target_aggregates_override={target_nm_id: aggregate},
                version_kind=str(candidate["version_kind"]),
            )
        else:
            fresh = _build_candidate(
                conn,
                affected_nm_ids=plan["nm_ids"],
                source_kind=str(candidate["source_kind"]),
                source_id=str(candidate["source_id"]),
                business_date=str(plan["business_date"]),
                published_at=str(candidate["published_at"]),
                allow_source_mismatch=True,
            )
        if (
            str(fresh["candidate_fingerprint"])
            != str(candidate["candidate_fingerprint"])
            or str(fresh["roster_digest"]) != str(plan["roster_digest"])
            or str(fresh["provenance_digest"]) != str(plan["provenance_digest"])
        ):
            raise WarehouseFbsMaterialError(
                "repair_candidate_cas_drift",
                "Candidate roster or provenance changed after incident planning",
            )
        current_ready = _fingerprint(
            [
                [
                    item["bundle_version"],
                    item["as_of_date"],
                    _sha_text(
                        str(
                            conn.execute(
                                """SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots
                                   WHERE bundle_version=? AND as_of_date=?""",
                                (item["bundle_version"], item["as_of_date"]),
                            ).fetchone()[0]
                        )
                    ),
                ]
                for item in plan["ready_updates"]
            ]
        )
        if current_ready != str(plan["ready_before_digest"]):
            raise WarehouseFbsMaterialError(
                "repair_ready_snapshot_cas_drift",
                "Ready-snapshot dependency closure changed after incident planning",
            )

    def _reconcile_after_error(self, plan: Mapping[str, Any]) -> dict[str, Any] | None:
        """Query-only same-operation reconciliation after an ambiguous response."""

        with _connect_readonly(self.runtime.db_path) as conn:
            _require_material_schema(conn)
            intent = conn.execute(
                f"SELECT * FROM {INTENTS_TABLE} WHERE operation_id=?",
                (str(plan["operation_id"]),),
            ).fetchone()
            if intent is not None and str(intent["status"]) == REPAIRED:
                if (
                    _readback_identity(conn, plan)
                    == dict(plan["typed_evidence"])["readback_identity"]
                ):
                    return self._repaired_readback(plan, idempotent=True)
            candidate = conn.execute(
                "SELECT status FROM sheet_vitrina_v1_warehouse_functional_versions WHERE version_id=?",
                (str(plan["target_version_id"]),),
            ).fetchone()
            if candidate is not None:
                return {
                    **(_intent_public(intent) if intent is not None else {}),
                    "contract_name": CONTRACT_NAME,
                    "operation_id": str(plan["operation_id"]),
                    "status": UNSAFE_AMBIGUOUS,
                    "query_only": True,
                    "candidate_present": True,
                    "reason": "ambiguous_candidate_readback",
                }
        return None

    def _record_recoverable_failure(
        self,
        plan: Mapping[str, Any],
        *,
        error: Exception,
        failed_at: str,
    ) -> dict[str, Any]:
        code = getattr(error, "code", "")
        with _connect(self.runtime.db_path) as conn:
            ensure_warehouse_fbs_material_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT attempt_count FROM {INTENTS_TABLE} WHERE operation_id=?",
                (str(plan["operation_id"]),),
            ).fetchone()
            attempts = int(row[0] if row is not None else 0)
            recoverable_transport = _recoverable_transport_error(error)
            # Only exact transport/contention classes may consume the bounded
            # retry budget. Every identity, semantic, resource or unknown
            # failure is terminal fail-closed.
            status = (
                UNSAFE_AMBIGUOUS
                if not recoverable_transport
                else RETRY_EXHAUSTED
                if attempts >= MAX_RETRY_ATTEMPTS
                else REPAIRABLE
            )
            _transition_intent(
                conn,
                operation_id=str(plan["operation_id"]),
                status=status,
                evidence={
                    "phase": "publication_error",
                    "code": code or type(error).__name__,
                },
                created_at=failed_at,
                error=str(error)[:1000],
            )
            conn.commit()
        return _intent_public(self._intent(str(plan["operation_id"])))  # type: ignore[arg-type]

    def _repaired_readback(
        self, plan: Mapping[str, Any], *, idempotent: bool
    ) -> dict[str, Any]:
        with _connect_readonly(self.runtime.db_path) as conn:
            identity = _readback_identity(conn, plan)
        return {
            "contract_name": CONTRACT_NAME,
            "status": REPAIRED,
            "idempotent": bool(idempotent),
            "operation_id": str(plan["operation_id"]),
            "plan_fingerprint": str(plan["plan_fingerprint"]),
            "typed_evidence": dict(plan["typed_evidence"]),
            "readback_identity": identity,
        }


def _build_candidate(
    conn: sqlite3.Connection,
    *,
    affected_nm_ids: Iterable[int],
    source_kind: str,
    source_id: str,
    business_date: str,
    published_at: str,
    allow_source_mismatch: bool,
    source_version_id_override: str = "",
    target_aggregates_override: Mapping[int, Mapping[str, Any]] | None = None,
    version_kind: str = "fbs_material_revision",
) -> dict[str, Any]:
    active = _active_version(conn)
    if active is None:
        raise WarehouseFbsMaterialError(
            "fbs_material_active_missing",
            "One exact good active functional version is required",
        )
    source_version_id = str(source_version_id_override or active["version_id"])
    source = conn.execute(
        "SELECT * FROM sheet_vitrina_v1_warehouse_functional_versions WHERE version_id=?",
        (source_version_id,),
    ).fetchone()
    if source is None or str(source["status"]) != "good":
        raise WarehouseFbsMaterialError(
            "fbs_material_source_missing",
            "One exact good source functional version is required",
        )
    if str(source["business_effective_date"] or "") != business_date:
        raise WarehouseFbsMaterialError(
            "fbs_material_business_date_drift",
            "FBS material effect and source functional version use different business dates",
        )
    raw_lines = [
        dict(row)
        for row in conn.execute(
            """SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances
               WHERE version_id=? ORDER BY warehouse_key,nm_id""",
            (source_version_id,),
        ).fetchall()
    ]
    if not raw_lines or len(raw_lines) > MAX_FUNCTIONAL_BALANCE_ROWS:
        raise WarehouseFbsMaterialError(
            "fbs_material_functional_roster_broad",
            "Active functional roster is missing or exceeds bounded publication",
            details={"row_count": len(raw_lines)},
        )
    targets = sorted({int(value) for value in affected_nm_ids if int(value) > 0})
    target_aggregates = (
        {int(key): dict(value) for key, value in target_aggregates_override.items()}
        if target_aggregates_override is not None
        else _pool_aggregates(conn, targets)
    )
    by_key = {
        (str(item["warehouse_key"]), int(item["nm_id"])): item for item in raw_lines
    }
    provenance_rows: list[dict[str, Any]] = []
    for nm_id in targets:
        aggregate = target_aggregates[nm_id]
        current = by_key.get((STAGE_FF, nm_id))
        if current is not None:
            current_quantity = Decimal(str(current["quantity"]))
            current_covered = Decimal(str(current["cost_covered_quantity"]))
            if not allow_source_mismatch and current_covered != current_quantity:
                raise WarehouseFbsMaterialError(
                    "fbs_material_cost_coverage_ambiguous",
                    "Current FF aggregate is not fully cost covered before the pool effect",
                    details={"nm_id": nm_id},
                )
            quality = str(current["quality"])
            certified = int(current["certified"])
            wb_quantity = str(current["wb_quantity"])
            wb_to_client = str(current["wb_in_way_to_client"])
            wb_from_client = str(current["wb_in_way_from_client"])
            prior_provenance = _loads(current["provenance_json"], {})
        else:
            if (
                not allow_source_mismatch
                and Decimal(str(aggregate["quantity"])) <= ZERO
            ):
                raise WarehouseFbsMaterialError(
                    "fbs_material_aggregate_sku_missing",
                    "Current FF aggregate SKU is absent before a non-positive pool effect",
                    details={"nm_id": nm_id},
                )
            quality = "fbs_pool_exact"
            certified = 0
            wb_quantity = "0"
            wb_to_client = "0"
            wb_from_client = "0"
            prior_provenance = {}
        provenance = {
            **prior_provenance,
            "source_records": [
                *[
                    dict(item)
                    for item in prior_provenance.get("source_records") or []
                    if isinstance(item, Mapping)
                ],
                {
                    "source": CONTRACT_NAME,
                    "source_kind": str(source_kind),
                    "source_id": str(source_id),
                    "business_date": business_date,
                    "flow_quantity": str(aggregate["quantity"]),
                    "flow_capital_rub": str(aggregate["capital_rub"]),
                    "cost_freshness": "exact",
                    "quality": quality,
                    "expenses_complete_certification": bool(certified),
                    "locations": list(aggregate["locations"]),
                },
            ],
            "fbs_material_revision": {
                "source_version_id": source_version_id,
                "source_kind": str(source_kind),
                "source_id": str(source_id),
                "business_date": business_date,
                "published_at": published_at,
                "pool_digest": str(aggregate["digest"]),
            },
        }
        replacement = {
            "version_id": "",
            "warehouse_key": STAGE_FF,
            "nm_id": nm_id,
            "quantity": str(aggregate["quantity"]),
            "wac_rub": aggregate["wac_rub"],
            "capital_rub": str(aggregate["capital_rub"]),
            "cost_covered_quantity": str(aggregate["quantity"]),
            "quality": quality,
            "certified": certified,
            "wb_quantity": wb_quantity,
            "wb_in_way_to_client": wb_to_client,
            "wb_in_way_from_client": wb_from_client,
            "provenance_json": _json(provenance),
        }
        by_key[(STAGE_FF, nm_id)] = replacement
        provenance_rows.append(
            {
                "nm_id": nm_id,
                "pool_digest": aggregate["digest"],
                "provenance": provenance,
            }
        )
    source_watermarks = _loads(source["source_watermarks_json"], {})
    source_watermarks["fbs_material_revision"] = {
        "source_kind": str(source_kind),
        "source_id": str(source_id),
        "business_date": business_date,
        "target_nm_ids": targets,
        "pool_digests": [target_aggregates[nm_id]["digest"] for nm_id in targets],
    }
    lines = [by_key[key] for key in sorted(by_key)]
    auxiliary_material = _version_auxiliary_material(conn, source_version_id)
    roster_digest = _fingerprint(
        [[item["warehouse_key"], int(item["nm_id"])] for item in lines]
    )
    provenance_digest = _fingerprint(provenance_rows)
    material = {
        "contract_name": CONTRACT_NAME,
        "source_version_id": source_version_id,
        "source_plan_fingerprint": str(source["plan_fingerprint"]),
        "source_kind": str(source_kind),
        "source_id": str(source_id),
        "business_date": business_date,
        "published_at": published_at,
        "target_nm_ids": targets,
        "roster_digest": roster_digest,
        "provenance_digest": provenance_digest,
        "auxiliary_digest": _fingerprint(auxiliary_material),
        "lines": [
            {
                key: item.get(key)
                for key in (
                    "warehouse_key",
                    "nm_id",
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
                )
            }
            for item in lines
        ],
        "source_watermarks": source_watermarks,
    }
    candidate_fingerprint = _fingerprint(material)
    target_version_id = "whfv_fbs_" + candidate_fingerprint[7:31]
    for item in lines:
        item["version_id"] = target_version_id
    return {
        **material,
        "candidate_fingerprint": candidate_fingerprint,
        "target_version_id": target_version_id,
        "version_kind": str(version_kind),
        "effective_at": published_at,
        "local_source_digest": _fingerprint(
            {"source_version_id": source_version_id, "pool": provenance_rows}
        ),
    }


def _persist_candidate_and_switch(
    conn: sqlite3.Connection,
    *,
    candidate: Mapping[str, Any],
    ready_updates: Iterable[Mapping[str, Any]],
    expected_active_version_id: str,
    switch_active: bool = True,
) -> dict[str, Any]:
    from packages.application.warehouse_business_projection import (
        ensure_functional_version_business_time_schema,
        publish_functional_version_business_projection,
    )
    from packages.application.warehouse_functional import (
        _materialize_compact_warehouse_read_models,
    )

    source_version_id = str(candidate["source_version_id"])
    target_version_id = str(candidate["target_version_id"])
    active = _active_version(conn)
    if active is None or str(active["version_id"]) != expected_active_version_id:
        raise WarehouseFbsMaterialError(
            "fbs_material_active_cas_drift",
            "Active functional version changed before candidate publication",
        )
    duplicate = conn.execute(
        "SELECT status FROM sheet_vitrina_v1_warehouse_functional_versions WHERE version_id=?",
        (target_version_id,),
    ).fetchone()
    if duplicate is not None:
        exact_readback = (
            str(active["version_id"]) == target_version_id
            if switch_active
            else str(active["version_id"]) == expected_active_version_id
        )
        if exact_readback and str(duplicate["status"]) == "good":
            return {
                "status": REPAIRED,
                "idempotent": True,
                "source_version_id": source_version_id,
                "target_version_id": target_version_id,
            }
        raise WarehouseFbsMaterialError(
            "fbs_material_candidate_identity_conflict",
            "Candidate functional version exists without exact active readback",
        )
    ensure_functional_version_business_time_schema(conn)
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_warehouse_functional_versions(
               version_id,cutover_id,version_kind,effective_at,
               business_effective_date,published_at,status,plan_fingerprint,
               local_source_digest,source_watermarks_json,created_at
           ) VALUES(?,?,?,?,?,?,'candidate',?,?,?,?)""",
        (
            target_version_id,
            FUNCTIONAL_CUTOVER_ID,
            str(candidate["version_kind"]),
            str(candidate["effective_at"]),
            str(candidate["business_date"]),
            str(candidate["published_at"]),
            str(candidate["candidate_fingerprint"]),
            str(candidate["local_source_digest"]),
            _json(candidate["source_watermarks"]),
            str(candidate["published_at"]),
        ),
    )
    for item in candidate["lines"]:
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                   version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                   cost_covered_quantity,quality,certified,wb_quantity,
                   wb_in_way_to_client,wb_in_way_from_client,provenance_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                target_version_id,
                str(item["warehouse_key"]),
                int(item["nm_id"]),
                str(item["quantity"]),
                item.get("wac_rub"),
                str(item["capital_rub"]),
                str(item["cost_covered_quantity"]),
                str(item["quality"]),
                int(item["certified"]),
                str(item["wb_quantity"]),
                str(item["wb_in_way_to_client"]),
                str(item["wb_in_way_from_client"]),
                str(item["provenance_json"]),
            ),
        )
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_warehouse_functional_ff_reservations(
               version_id,supply_id,nm_id,quantity)
           SELECT ?,supply_id,nm_id,quantity
           FROM sheet_vitrina_v1_warehouse_functional_ff_reservations
           WHERE version_id=?""",
        (target_version_id, source_version_id),
    )
    for state in _effective_supplier_state_rows(conn, source_version_id):
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_warehouse_supplier_cost_states(
                   version_id,shipment_id,source_fingerprint,calculation_fingerprint,
                   expenses_complete,calculation_available,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (
                target_version_id,
                str(state["shipment_id"]),
                str(state["source_fingerprint"]),
                str(state["calculation_fingerprint"]),
                int(state["expenses_complete"]),
                int(state["calculation_available"]),
                str(candidate["published_at"]),
            ),
        )
    snapshot = conn.execute(
        """SELECT * FROM sheet_vitrina_v1_warehouse_wb_snapshots
           WHERE version_id=? ORDER BY created_at DESC LIMIT 1""",
        (source_version_id,),
    ).fetchone()
    if snapshot is None or str(snapshot["snapshot_date"]) != str(
        candidate["business_date"]
    ):
        raise WarehouseFbsMaterialError(
            "fbs_material_snapshot_missing",
            "Candidate requires the exact official snapshot of its business date",
        )
    snapshot_id = "whwbs_fbs_" + str(candidate["candidate_fingerprint"])[7:29]
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_warehouse_wb_snapshots(
               snapshot_id,version_id,fetched_at,snapshot_date,requested_nm_ids_json,
               pagination_complete,page_count,page_offsets_json,raw_row_count,
               raw_rows_digest,raw_rows_json,items_json,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            snapshot_id,
            target_version_id,
            str(snapshot["fetched_at"]),
            str(snapshot["snapshot_date"]),
            str(snapshot["requested_nm_ids_json"]),
            int(snapshot["pagination_complete"]),
            int(snapshot["page_count"]),
            str(snapshot["page_offsets_json"]),
            int(snapshot["raw_row_count"]),
            str(snapshot["raw_rows_digest"]),
            str(snapshot["raw_rows_json"]),
            str(snapshot["items_json"]),
            str(candidate["published_at"]),
        ),
    )
    option = conn.execute(
        """SELECT etag,payload_json,payload_bytes
           FROM sheet_vitrina_v1_warehouse_wb_option_read_models
           WHERE version_id=?""",
        (source_version_id,),
    ).fetchone()
    if option is not None:
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_warehouse_wb_option_read_models(
                   version_id,etag,payload_json,payload_bytes,created_at)
               VALUES(?,?,?,?,?)""",
            (
                target_version_id,
                str(option["etag"]),
                str(option["payload_json"]),
                int(option["payload_bytes"]),
                str(candidate["published_at"]),
            ),
        )
    reservations = [
        dict(row)
        for row in conn.execute(
            """SELECT supply_id,nm_id,quantity
               FROM sheet_vitrina_v1_warehouse_functional_ff_reservations
               WHERE version_id=? ORDER BY supply_id,nm_id""",
            (target_version_id,),
        ).fetchall()
    ]
    plan_lines = [
        {**dict(item), "provenance": _loads(item["provenance_json"], {})}
        for item in candidate["lines"]
    ]
    unmatched = _copy_unmatched_projection(
        conn,
        source_version_id=source_version_id,
        target_version_id=target_version_id,
        created_at=str(candidate["published_at"]),
    )
    compact_plan = {
        "plan_kind": str(candidate["version_kind"]),
        "plan_fingerprint": str(candidate["candidate_fingerprint"]),
        "lines": plan_lines,
        "ff_reservations": reservations,
        "unmatched_doprinato": unmatched,
    }
    _materialize_compact_warehouse_read_models(
        conn,
        version_id=target_version_id,
        plan=compact_plan,
        created_at=str(candidate["published_at"]),
        effective_at=str(candidate["effective_at"]),
        business_effective_date=str(candidate["business_date"]),
    )
    _insert_material_revision_documents(
        conn,
        candidate=candidate,
        version_id=target_version_id,
    )
    for update in ready_updates:
        changed = conn.execute(
            """UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?,refreshed_at=?
               WHERE bundle_version=? AND as_of_date=?
                 AND plan_json=?""",
            (
                str(update["after_plan_json"]),
                str(candidate["published_at"]),
                str(update["bundle_version"]),
                str(update["as_of_date"]),
                str(
                    conn.execute(
                        """SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots
                           WHERE bundle_version=? AND as_of_date=?""",
                        (str(update["bundle_version"]), str(update["as_of_date"])),
                    ).fetchone()[0]
                ),
            ),
        )
        if changed.rowcount != 1:
            raise WarehouseFbsMaterialError(
                "fbs_material_ready_snapshot_cas_drift",
                "Ready-snapshot target changed before atomic candidate switch",
            )
    marked_good = conn.execute(
        "UPDATE sheet_vitrina_v1_warehouse_functional_versions SET status='good' WHERE version_id=? AND status='candidate'",
        (target_version_id,),
    )
    if marked_good.rowcount != 1:
        raise WarehouseFbsMaterialError(
            "fbs_material_candidate_status_cas_drift",
            "Candidate version could not become one exact good version",
        )
    projection = publish_functional_version_business_projection(
        conn,
        published_version_id=target_version_id,
        business_effective_date=str(candidate["business_date"]),
        published_at=str(candidate["published_at"]),
        source_revision=str(candidate["candidate_fingerprint"]),
    )
    if switch_active:
        switched = conn.execute(
            """UPDATE sheet_vitrina_v1_warehouse_functional_active
               SET version_id=?,updated_at=? WHERE slot=1 AND version_id=?""",
            (target_version_id, str(candidate["published_at"]), source_version_id),
        )
        if switched.rowcount != 1:
            raise WarehouseFbsMaterialError(
                "fbs_material_active_switch_cas_drift",
                "Active version changed before atomic candidate switch",
            )
        sync_status = conn.execute(
            """UPDATE sheet_vitrina_v1_warehouse_wb_sync_status
               SET active_version_id=?,updated_at=? WHERE slot=1""",
            (target_version_id, str(candidate["published_at"])),
        )
        if sync_status.rowcount != 1:
            raise WarehouseFbsMaterialError(
                "fbs_material_sync_status_missing",
                "Canonical functional sync status is missing during active switch",
            )
    elif str(_active_version(conn)["version_id"]) != expected_active_version_id:
        raise WarehouseFbsMaterialError(
            "fbs_material_active_preservation_drift",
            "Historical publication changed the current active version",
        )
    _verify_candidate_readback(conn, candidate)
    return {
        "status": REPAIRED,
        "idempotent": False,
        "source_version_id": source_version_id,
        "target_version_id": target_version_id,
        "candidate_fingerprint": str(candidate["candidate_fingerprint"]),
        "business_projection": projection,
        "functional_balance_rows": len(candidate["lines"]),
    }


def _version_auxiliary_material(
    conn: sqlite3.Connection, version_id: str
) -> dict[str, Any]:
    reservations = [
        dict(row)
        for row in conn.execute(
            """SELECT supply_id,nm_id,quantity
               FROM sheet_vitrina_v1_warehouse_functional_ff_reservations
               WHERE version_id=? ORDER BY supply_id,nm_id""",
            (version_id,),
        ).fetchall()
    ]
    unmatched = [
        dict(row)
        for row in conn.execute(
            """SELECT source_id,business_date,nm_id,quantity,matched_quantity,
                      reason,provenance_json
               FROM sheet_vitrina_v1_warehouse_unmatched_doprinato
               WHERE version_id=? ORDER BY business_date,unmatched_id""",
            (version_id,),
        ).fetchall()
    ]
    base_states = [
        dict(row)
        for row in conn.execute(
            """SELECT shipment_id,source_fingerprint,calculation_fingerprint,
                      expenses_complete,calculation_available
               FROM sheet_vitrina_v1_warehouse_supplier_cost_states
               WHERE version_id=? ORDER BY shipment_id""",
            (version_id,),
        ).fetchall()
    ]
    corrections = [
        dict(row)
        for row in conn.execute(
            """SELECT correction.shipment_id,correction.source_fingerprint,
                      correction.calculation_fingerprint,
                      correction.expenses_complete,correction.calculation_available,
                      correction.state_fingerprint,replay.sequence_no,
                      CASE WHEN rollback.replay_id IS NULL THEN 0 ELSE 1 END rolled_back
               FROM sheet_vitrina_v1_warehouse_supplier_cost_state_corrections correction
               JOIN sheet_vitrina_v1_warehouse_supplier_cost_state_replays replay
                 ON replay.replay_id=correction.replay_id
               LEFT JOIN sheet_vitrina_v1_warehouse_supplier_cost_state_replay_rollbacks rollback
                 ON rollback.replay_id=replay.replay_id
               WHERE correction.version_id=?
               ORDER BY correction.shipment_id,replay.sequence_no""",
            (version_id,),
        ).fetchall()
    ]
    option = conn.execute(
        """SELECT etag,payload_bytes,payload_json
           FROM sheet_vitrina_v1_warehouse_wb_option_read_models
           WHERE version_id=?""",
        (version_id,),
    ).fetchone()
    snapshot = conn.execute(
        """SELECT snapshot_id,fetched_at,snapshot_date,requested_nm_ids_json,
                  pagination_complete,page_count,page_offsets_json,raw_row_count,
                  raw_rows_digest,length(CAST(raw_rows_json AS BLOB)) raw_rows_bytes,
                  length(CAST(items_json AS BLOB)) items_bytes
           FROM sheet_vitrina_v1_warehouse_wb_snapshots
           WHERE version_id=? ORDER BY created_at DESC LIMIT 1""",
        (version_id,),
    ).fetchone()
    row_count = len(reservations) + len(unmatched) + len(base_states) + len(corrections)
    if row_count > MAX_AUXILIARY_ROWS:
        raise WarehouseFbsMaterialError(
            "fbs_material_auxiliary_scope_broad",
            "Active functional auxiliary closure exceeds the bounded publication",
            details={"row_count": row_count},
        )
    if option is not None and int(option["payload_bytes"]) > 2_000_000:
        raise WarehouseFbsMaterialError(
            "fbs_material_wb_option_scope_broad",
            "Active WB option projection exceeds the bounded publication",
            details={"payload_bytes": int(option["payload_bytes"])},
        )
    if snapshot is not None:
        snapshot_bytes = int(snapshot["raw_rows_bytes"] or 0) + int(
            snapshot["items_bytes"] or 0
        )
        if snapshot_bytes > MAX_WB_SNAPSHOT_BYTES:
            raise WarehouseFbsMaterialError(
                "fbs_material_wb_snapshot_scope_broad",
                "Active official WB snapshot exceeds bounded material publication",
                details={
                    "snapshot_bytes": snapshot_bytes,
                    "max_snapshot_bytes": MAX_WB_SNAPSHOT_BYTES,
                },
            )
    return {
        "reservations": reservations,
        "unmatched": unmatched,
        "supplier_cost_states": base_states,
        "supplier_cost_corrections": corrections,
        "wb_option": dict(option) if option is not None else None,
        "wb_snapshot": dict(snapshot) if snapshot is not None else None,
    }


def _effective_supplier_state_rows(
    conn: sqlite3.Connection, version_id: str
) -> list[dict[str, Any]]:
    by_shipment = {
        str(row["shipment_id"]): dict(row)
        for row in conn.execute(
            """SELECT shipment_id,source_fingerprint,calculation_fingerprint,
                      expenses_complete,calculation_available
               FROM sheet_vitrina_v1_warehouse_supplier_cost_states
               WHERE version_id=? ORDER BY shipment_id""",
            (version_id,),
        ).fetchall()
    }
    for row in conn.execute(
        """SELECT correction.shipment_id,correction.source_fingerprint,
                  correction.calculation_fingerprint,correction.expenses_complete,
                  correction.calculation_available,replay.sequence_no
           FROM sheet_vitrina_v1_warehouse_supplier_cost_state_corrections correction
           JOIN sheet_vitrina_v1_warehouse_supplier_cost_state_replays replay
             ON replay.replay_id=correction.replay_id
           LEFT JOIN sheet_vitrina_v1_warehouse_supplier_cost_state_replay_rollbacks rollback
             ON rollback.replay_id=replay.replay_id
           WHERE correction.version_id=? AND rollback.replay_id IS NULL
             AND correction.expenses_complete=1
             AND correction.calculation_available=1
           ORDER BY correction.shipment_id,replay.sequence_no""",
        (version_id,),
    ).fetchall():
        by_shipment[str(row["shipment_id"])] = dict(row)
    return [
        {
            key: by_shipment[shipment_id][key]
            for key in (
                "shipment_id",
                "source_fingerprint",
                "calculation_fingerprint",
                "expenses_complete",
                "calculation_available",
            )
        }
        for shipment_id in sorted(by_shipment)
    ]


def _copy_unmatched_projection(
    conn: sqlite3.Connection,
    *,
    source_version_id: str,
    target_version_id: str,
    created_at: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in conn.execute(
        """SELECT source_id,business_date,nm_id,quantity,matched_quantity,
                  reason,provenance_json
           FROM sheet_vitrina_v1_warehouse_unmatched_doprinato
           WHERE version_id=? ORDER BY business_date,unmatched_id""",
        (source_version_id,),
    ).fetchall():
        identity = {
            "version_id": target_version_id,
            "source_id": str(row["source_id"]),
            "business_date": str(row["business_date"] or ""),
            "nm_id": int(row["nm_id"]),
            "reason": str(row["reason"]),
        }
        unmatched_id = "whum_fbs_" + _fingerprint(identity)[7:31]
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_warehouse_unmatched_doprinato(
                   unmatched_id,version_id,source_id,business_date,nm_id,quantity,
                   matched_quantity,reason,provenance_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                unmatched_id,
                target_version_id,
                str(row["source_id"]),
                row["business_date"],
                int(row["nm_id"]),
                str(row["quantity"]),
                str(row["matched_quantity"]),
                str(row["reason"]),
                str(row["provenance_json"]),
                created_at,
            ),
        )
        result.append(
            {
                **identity,
                "unmatched_id": unmatched_id,
                "quantity": str(row["quantity"]),
                "matched_quantity": str(row["matched_quantity"]),
                "provenance": _loads(row["provenance_json"], {}),
            }
        )
    return result


def _insert_material_revision_documents(
    conn: sqlite3.Connection,
    *,
    candidate: Mapping[str, Any],
    version_id: str,
) -> None:
    by_stage: dict[str, list[Mapping[str, Any]]] = {}
    for line in candidate["lines"]:
        by_stage.setdefault(str(line["warehouse_key"]), []).append(line)
    for stage, lines in sorted(by_stage.items()):
        with localcontext() as context:
            context.prec = 160
            quantity = sum((Decimal(str(line["quantity"])) for line in lines), ZERO)
            capital = sum((Decimal(str(line["capital_rub"])) for line in lines), ZERO)
        document_id = (
            "whdoc_fbs_"
            + _fingerprint({"version_id": version_id, "warehouse_key": stage})[7:31]
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_warehouse_functional_documents(
                   document_id,version_id,warehouse_key,document_type,occurred_at,
                   source_id,source_fingerprint,quantity,capital_rub,
                   provenance_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                document_id,
                version_id,
                stage,
                "fbs_material_revision",
                str(candidate["published_at"]),
                str(candidate["source_id"]),
                str(candidate["candidate_fingerprint"]),
                canonical_decimal_text(quantity),
                canonical_decimal_text(capital),
                _json(
                    {
                        "contract_name": CONTRACT_NAME,
                        "source_version_id": str(candidate["source_version_id"]),
                        "target_nm_ids": list(candidate["target_nm_ids"]),
                    }
                ),
                str(candidate["published_at"]),
            ),
        )
        for line in sorted(lines, key=lambda item: int(item["nm_id"])):
            line_id = (
                "whdocline_fbs_"
                + _fingerprint(
                    {"document_id": document_id, "nm_id": int(line["nm_id"])}
                )[7:31]
            )
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_warehouse_functional_document_lines(
                       line_id,document_id,version_id,nm_id,quantity,wac_rub,
                       capital_rub,provenance_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    line_id,
                    document_id,
                    version_id,
                    int(line["nm_id"]),
                    str(line["quantity"]),
                    line.get("wac_rub"),
                    str(line["capital_rub"]),
                    str(line["provenance_json"]),
                    str(candidate["published_at"]),
                ),
            )


def _historical_event_aggregate(
    conn: sqlite3.Connection,
    *,
    source_version_id: str,
    facility_id: str,
    nm_id: int,
    event: Mapping[str, Any],
) -> dict[str, Any]:
    row = conn.execute(
        """SELECT quantity,capital_rub,wac_rub,cost_covered_quantity,provenance_json
             FROM sheet_vitrina_v1_warehouse_functional_balances
            WHERE version_id=? AND warehouse_key='ff' AND nm_id=?""",
        (str(source_version_id), int(nm_id)),
    ).fetchone()
    if row is None:
        raise WarehouseFbsMaterialError(
            "historical_target_balance_missing",
            "Accepted historical version lacks the exact FF target row",
        )
    with localcontext() as arithmetic:
        arithmetic.prec = 38
        quantity = +Decimal(str(row["quantity"]))
        capital = +Decimal(str(row["capital_rub"]))
        covered = +Decimal(str(row["cost_covered_quantity"]))
        quantity_delta = +Decimal(str(event["physical_quantity_delta"]))
        capital_delta = +Decimal(str(event["capital_delta_rub"]))
        frozen_wac = +Decimal(str(event["frozen_wac_rub"]))
        event_capital = +(quantity_delta * frozen_wac)
    if (
        quantity <= ZERO
        or quantity != quantity.to_integral_value()
        or capital <= ZERO
        or quantity_delta >= ZERO
        or capital_delta >= ZERO
        or capital_delta != event_capital
    ):
        raise WarehouseFbsMaterialError(
            "historical_event_arithmetic_invalid",
            "Historical handoff debit is not an exact finite quantity/capital effect",
        )
    canonical_wac = canonical_decimal_ratio_text(capital, quantity)
    try:
        accepted_wac = Decimal(str(row["wac_rub"]))
    except (InvalidOperation, TypeError, ValueError):
        accepted_wac = ZERO
    if not accepted_wac.is_finite() or accepted_wac <= ZERO:
        raise WarehouseFbsMaterialError(
            "historical_accepted_wac_invalid",
            "Accepted historical WAC must be one finite positive stored value",
        )
    prior_matches: list[dict[str, Any]] = []
    provenance = _loads(row["provenance_json"], {})
    for record in provenance.get("source_records") or []:
        if not isinstance(record, Mapping):
            continue
        locations = [
            dict(location)
            for location in record.get("locations") or []
            if isinstance(location, Mapping)
        ]
        target_positions = [
            index
            for index, location in enumerate(locations)
            if str(location.get("facility_id") or "") == str(facility_id)
            and str(location.get("pool") or "").upper() == "FBS"
        ]
        if len(target_positions) != 1 or not locations:
            continue
        try:
            with localcontext() as arithmetic:
                arithmetic.prec = 38
                prior_quantity = sum(
                    (
                        Decimal(str(location.get("quantity") or "0"))
                        for location in locations
                    ),
                    ZERO,
                )
                prior_capital = sum(
                    (
                        Decimal(str(location.get("capital_rub") or "0"))
                        for location in locations
                    ),
                    ZERO,
                )
                target_position = target_positions[0]
                target_prior_quantity = Decimal(
                    str(locations[target_position].get("quantity") or "0")
                )
                target_prior_capital = Decimal(
                    str(locations[target_position].get("capital_rub") or "0")
                )
                target_after_quantity = target_prior_quantity + quantity_delta
                target_after_capital = target_prior_capital + capital_delta
        except (InvalidOperation, TypeError, ValueError):
            continue
        with localcontext() as arithmetic:
            arithmetic.prec = 38
            exact_prior_match = bool(
                prior_quantity + quantity_delta == quantity
                and prior_capital + capital_delta == capital
            )
        if (
            not exact_prior_match
            or target_after_quantity < ZERO
            or target_after_capital < ZERO
        ):
            continue
        transformed_locations: list[dict[str, Any]] = []
        for index, location in enumerate(locations):
            item = dict(location)
            item["pool"] = str(item.get("pool") or "").upper()
            item_quantity = (
                target_after_quantity
                if index == target_position
                else Decimal(str(item.get("quantity") or "0"))
            )
            item_capital = (
                target_after_capital
                if index == target_position
                else Decimal(str(item.get("capital_rub") or "0"))
            )
            item["quantity"] = canonical_decimal_text(item_quantity)
            item["capital_rub"] = canonical_decimal_text(item_capital)
            item["wac_rub"] = (
                canonical_decimal_ratio_text(item_capital, item_quantity)
                if item_quantity > ZERO
                else None
            )
            if index == target_position:
                item["source_watermark"] = str(event["event_id"])
            transformed_locations.append(item)
        prior_matches.append(
            {
                "prior_quantity": canonical_decimal_text(prior_quantity),
                "prior_capital_rub": canonical_decimal_text(prior_capital),
                "target_prior_quantity": canonical_decimal_text(target_prior_quantity),
                "target_prior_capital_rub": canonical_decimal_text(
                    target_prior_capital
                ),
                "locations": transformed_locations,
            }
        )
    unique_prior = {_json(item): item for item in prior_matches}
    if len(unique_prior) != 1:
        raise WarehouseFbsMaterialError(
            "historical_event_prior_operand_ambiguous",
            "Immutable accepted provenance does not prove one exact pre-debit location",
            details={"matching_location_count": len(unique_prior)},
        )
    selected_prior = next(iter(unique_prior.values()))
    locations = list(selected_prior["locations"])
    material = {
        "nm_id": int(nm_id),
        "quantity": canonical_decimal_text(quantity),
        "capital_rub": canonical_decimal_text(capital),
        "wac_rub": canonical_wac,
        "locations": locations,
        "accepted_quantity": canonical_decimal_text(quantity),
        "accepted_cost_covered_quantity": canonical_decimal_text(covered),
        "event_id": str(event["event_id"]),
        "accepted_wac_rub_text": str(row["wac_rub"]),
        "prior_location_set": selected_prior,
    }
    return {**material, "digest": _fingerprint(material)}


def _ready_updates_for_candidate(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    conn: sqlite3.Connection,
    candidate: Mapping[str, Any],
    target_date: str,
    targets: Iterable[int],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    snapshots = _ready_snapshots_for_date(conn, target_date)
    if not snapshots or len(snapshots) > MAX_READY_SNAPSHOTS:
        raise WarehouseFbsMaterialError(
            "ready_snapshot_scope_missing_or_broad",
            "Historical recovery requires one bounded ready-snapshot closure",
            details={"snapshot_count": len(snapshots)},
        )
    selected = sorted({int(value) for value in targets})
    candidate_rows = list(candidate["lines"])
    roster_nm_ids = sorted({int(item["nm_id"]) for item in candidate_rows})
    warehouse_metrics = _warehouse_metric_lookup(
        candidate_rows,
        version_id=str(candidate["target_version_id"]),
        published_at=str(candidate["published_at"]),
        source_watermarks=dict(candidate["source_watermarks"]),
        requested_nm_ids=roster_nm_ids,
    )
    from packages.application.calculation_parameters import (
        DEFAULT_PROXY_PARAMETERS,
        PROXY_BLOCK_KEY,
        _parameters_from_row as _proxy3_parameters_from_row,
    )
    from packages.application.calculation_parameters_v4 import (
        PROXY_V4_BLOCK_KEY,
        PROXY_V4_FIXED_BOUNDARY,
        _parameters_from_row as _proxy4_parameters_from_row,
    )
    from packages.application.canonical_wb_cost_resolver import (
        load_canonical_wb_cost_lookup,
    )
    from packages.application.inventory_cost_blend import (
        build_inventory_cost_blend_lookup,
    )
    from packages.application.warehouse_functional_economics_backfill import (
        _transform_snapshot,
    )

    # Every dependency is loaded through the caller's already-bound
    # query_only connection.  No runtime method may re-resolve storage or run
    # an implicit schema ensure during historical qualification.
    wb_compat = load_canonical_wb_cost_lookup(
        conn,
        as_of_date=date.fromisoformat(target_date),
    )
    costs = build_inventory_cost_blend_lookup(
        as_of_date=target_date,
        wb_compat_lookup=wb_compat,
        product_capital_lookup=warehouse_metrics,
    )
    params_row = conn.execute(
        "SELECT * FROM sheet_vitrina_v1_calculation_parameter_versions "
        "WHERE block_key=? AND effective_date<=? "
        "ORDER BY effective_date DESC,revision DESC,created_at DESC LIMIT 1",
        (PROXY_BLOCK_KEY, target_date),
    ).fetchone()
    params = (
        DEFAULT_PROXY_PARAMETERS
        if params_row is None
        else _proxy3_parameters_from_row(params_row)
    )
    proxy_v4_params = None
    if target_date >= PROXY_V4_FIXED_BOUNDARY:
        proxy_v4_row = conn.execute(
            "SELECT * FROM sheet_vitrina_v1_proxy_v4_parameter_versions "
            "WHERE block_key=? AND effective_date<=? "
            "ORDER BY effective_date DESC,revision DESC,created_at DESC LIMIT 1",
            (PROXY_V4_BLOCK_KEY, target_date),
        ).fetchone()
        proxy_v4_params = (
            None if proxy_v4_row is None else _proxy4_parameters_from_row(proxy_v4_row)
        )
    source_fingerprint = _fingerprint(
        {
            "candidate_fingerprint": candidate["candidate_fingerprint"],
            "costs": costs,
            "calculation_parameters": params.public(),
            "proxy_v4_parameters": (
                proxy_v4_params.public() if proxy_v4_params is not None else None
            ),
        }
    )
    updates: list[dict[str, Any]] = []
    before_missing: set[str] = set()
    after_missing: set[str] = set()
    before_missing_target_cost: set[int] = set()
    after_missing_target_cost: set[int] = set()
    positive_order_targets: set[int] = set()
    for snapshot in snapshots:
        before_payload = _loads(snapshot["plan_json"], {})
        before_cells = _ready_cells(before_payload, target_date)
        if not before_cells:
            raise WarehouseFbsMaterialError(
                "ready_snapshot_shape_invalid",
                "Historical ready snapshot has no exact date column",
            )
        for nm_id in selected:
            if _cell_decimal(before_cells.get(f"SKU:{nm_id}|orderSum")) > ZERO:
                positive_order_targets.add(nm_id)
            if before_cells.get(f"SKU:{nm_id}|{OUR_WB_UNIT_COST_RUB_METRIC_KEY}") in {
                None,
                "",
            }:
                before_missing_target_cost.add(nm_id)
        before_missing.update(
            key
            for key in CRITICAL_TOTAL_METRIC_KEYS
            if before_cells.get(f"TOTAL|{key}") in {None, ""}
        )
        transformed = _transform_snapshot(
            snapshot,
            costs={target_date: costs},
            warehouse_metrics={target_date: warehouse_metrics},
            warehouse_exact_dates={target_date},
            warehouse_covered_nm_ids={target_date: set(roster_nm_ids)},
            warehouse_version_ids={target_date: str(candidate["target_version_id"])},
            parameters={target_date: params},
            proxy_v4_parameters={target_date: proxy_v4_params},
            source_fingerprint=source_fingerprint,
            cutover_business_date=target_date,
            operation_business_date=current_business_date_iso(),
            affected_nm_ids=selected,
            earliest_business_date=target_date,
            latest_business_date=target_date,
        )
        after_payload = _loads(transformed["after_plan_json"], {})
        after_cells = _ready_cells(after_payload, target_date)
        after_missing.update(
            key
            for key in CRITICAL_TOTAL_METRIC_KEYS
            if after_cells.get(f"TOTAL|{key}") in {None, ""}
        )
        for nm_id in selected:
            if after_cells.get(f"SKU:{nm_id}|{OUR_WB_UNIT_COST_RUB_METRIC_KEY}") in {
                None,
                "",
            }:
                after_missing_target_cost.add(nm_id)
        if str(transformed["non_target_before"]) != str(
            transformed["non_target_after"]
        ):
            raise WarehouseFbsMaterialError(
                "historical_non_target_ready_drift",
                "Historical target replay changed a non-target ready-snapshot cell",
            )
        updates.append(
            {
                "bundle_version": str(snapshot["bundle_version"]),
                "as_of_date": str(snapshot["as_of_date"]),
                "before_plan_sha256": _sha_text(str(snapshot["plan_json"])),
                "after_plan_sha256": _sha_text(str(transformed["after_plan_json"])),
                "after_plan_json": str(transformed["after_plan_json"]),
                "changed_cells": int(transformed["changed_cells"]),
                "non_target_digest": str(transformed["non_target_after"]),
            }
        )
    if positive_order_targets != set(selected):
        raise WarehouseFbsMaterialError(
            "historical_positive_order_evidence_missing",
            "Historical recovery target is not one exact positive-order SKU",
            details={"positive_order_nm_ids": sorted(positive_order_targets)},
        )
    if before_missing_target_cost != set(selected):
        raise WarehouseFbsMaterialError(
            "historical_target_cost_shape_drift",
            "Historical recovery target does not have the expected blank own cost",
            details={"blank_own_cost_nm_ids": sorted(before_missing_target_cost)},
        )
    if before_missing != set(CRITICAL_TOTAL_METRIC_KEYS):
        raise WarehouseFbsMaterialError(
            "historical_total_dependency_shape_drift",
            "Historical recovery does not bind the exact six missing TOTAL dependencies",
            details={"missing_metric_keys": sorted(before_missing)},
        )
    if after_missing or after_missing_target_cost:
        raise WarehouseFbsMaterialError(
            "critical_total_dependency_still_missing",
            "Historical target economics closure remains incomplete",
            details={
                "missing_metric_keys": sorted(after_missing),
                "missing_target_cost_nm_ids": sorted(after_missing_target_cost),
            },
        )
    closure = {
        "affected_positive_order_nm_ids": sorted(positive_order_targets),
        "missing_critical_total_dependencies_before": sorted(before_missing),
        "missing_critical_total_dependencies_after": sorted(after_missing),
        "critical_total_metric_keys": list(CRITICAL_TOTAL_METRIC_KEYS),
        "target_and_total_only": True,
        "ready_snapshot_count": len(updates),
    }
    return updates, closure


def historical_ready_shape_evidence(
    conn: sqlite3.Connection,
    *,
    business_date: str,
    nm_id: int,
) -> dict[str, Any]:
    """Classify the immutable ready-side prerequisites before event expansion."""

    target_date = _iso_date(business_date)
    target_nm_id = int(nm_id)
    snapshots = _ready_snapshots_for_date(conn, target_date)
    observations: list[dict[str, Any]] = []
    blockers: list[str] = []
    if not snapshots or len(snapshots) > MAX_READY_SNAPSHOTS:
        blockers.append("ready_snapshot_scope_missing_or_broad")
    for snapshot in snapshots[:MAX_READY_SNAPSHOTS]:
        cells = _ready_cells(_loads(snapshot["plan_json"], {}), target_date)
        positive_order = (
            _cell_decimal(cells.get(f"SKU:{target_nm_id}|orderSum")) > ZERO
            if cells
            else False
        )
        blank_own_cost = (
            cells.get(f"SKU:{target_nm_id}|{OUR_WB_UNIT_COST_RUB_METRIC_KEY}")
            in {None, ""}
            if cells
            else False
        )
        missing_total = sorted(
            key
            for key in CRITICAL_TOTAL_METRIC_KEYS
            if cells.get(f"TOTAL|{key}") in {None, ""}
        )
        observation = {
            "bundle_version": str(snapshot["bundle_version"]),
            "as_of_date": str(snapshot["as_of_date"]),
            "plan_sha256": _sha_text(str(snapshot["plan_json"])),
            "plan_bytes": int(snapshot["plan_bytes"] or 0),
            "exact_date_cells": bool(cells),
            "positive_order": positive_order,
            "blank_own_cost": blank_own_cost,
            "missing_total_metric_keys": missing_total,
            "missing_total_metric_digest": _fingerprint(missing_total),
        }
        observations.append(observation)
        if not cells:
            blockers.append("ready_snapshot_exact_date_cells_missing")
        if not positive_order:
            blockers.append("historical_positive_order_evidence_missing")
        if not blank_own_cost:
            blockers.append("historical_target_cost_shape_drift")
        if missing_total != sorted(CRITICAL_TOTAL_METRIC_KEYS):
            blockers.append("historical_total_dependency_shape_drift")
    result = {
        "business_date": target_date,
        "nm_id": target_nm_id,
        "snapshot_count": len(snapshots),
        "snapshot_observations": observations,
        "snapshot_observations_digest": _fingerprint(observations),
        "critical_total_metric_keys": list(CRITICAL_TOTAL_METRIC_KEYS),
        "blockers": sorted(set(blockers)),
        "eligible": bool(observations) and not blockers,
    }
    result["fingerprint"] = _fingerprint(result)
    return result


def _pool_aggregates(
    conn: sqlite3.Connection, nm_ids: Iterable[int]
) -> dict[int, dict[str, Any]]:
    epoch = conn.execute(
        f"SELECT epoch,writer_enabled FROM {FEATURE_EPOCHS_TABLE} ORDER BY epoch DESC LIMIT 1"
    ).fetchone()
    if epoch is None or not bool(epoch["writer_enabled"]):
        raise WarehouseFbsMaterialError(
            "fbs_material_writer_epoch_missing",
            "FBS material publication requires the exact enabled pool writer epoch",
        )
    feature_epoch = int(epoch["epoch"])
    result: dict[int, dict[str, Any]] = {}
    for nm_id in sorted({int(value) for value in nm_ids if int(value) > 0}):
        rows = [
            dict(row)
            for row in conn.execute(
                f"""SELECT facility_id,pool,quantity,capital_rub,wac_rub,
                           source_watermark,updated_at,projection_epoch
                    FROM {BALANCES_TABLE}
                    WHERE nm_id=? AND projection_epoch=? AND pool IN('FBS','FBO')
                    ORDER BY facility_id,pool""",
                (nm_id, feature_epoch),
            ).fetchall()
        ]
        if not rows:
            raise WarehouseFbsMaterialError(
                "fbs_material_pool_roster_missing",
                "Target SKU has no current facility/pool physical rows",
                details={"nm_id": nm_id},
            )
        quantity = ZERO
        capital = ZERO
        locations: list[dict[str, Any]] = []
        with localcontext() as context:
            context.prec = 160
            for row in rows:
                item_quantity = Decimal(str(row["quantity"]))
                item_capital = Decimal(str(row["capital_rub"]))
                if (
                    item_quantity < ZERO
                    or item_quantity != item_quantity.to_integral_value()
                    or item_capital < ZERO
                    or (item_quantity == ZERO) != (item_capital == ZERO)
                ):
                    raise WarehouseFbsMaterialError(
                        "fbs_material_pool_shape_invalid",
                        "Facility/pool operand has non-canonical quantity/capital shape",
                        details={"nm_id": nm_id, "facility_id": row["facility_id"]},
                    )
                if item_quantity > ZERO:
                    expected_wac = canonical_decimal_ratio_text(
                        item_capital, item_quantity
                    )
                    if row["wac_rub"] is None or str(row["wac_rub"]) != expected_wac:
                        raise WarehouseFbsMaterialError(
                            "fbs_material_pool_wac_invalid",
                            "Facility/pool WAC differs from exact capital/quantity",
                            details={"nm_id": nm_id, "facility_id": row["facility_id"]},
                        )
                elif row["wac_rub"] is not None:
                    raise WarehouseFbsMaterialError(
                        "fbs_material_pool_zero_wac_invalid",
                        "Canonical zero facility/pool row must have NULL WAC",
                        details={"nm_id": nm_id, "facility_id": row["facility_id"]},
                    )
                quantity += item_quantity
                capital += item_capital
                if item_quantity > ZERO:
                    locations.append(
                        {
                            "facility_id": str(row["facility_id"]),
                            "pool": str(row["pool"]),
                            "quantity": canonical_decimal_text(item_quantity),
                            "capital_rub": canonical_decimal_text(item_capital),
                            "wac_rub": canonical_decimal_ratio_text(
                                item_capital, item_quantity
                            ),
                            "source_watermark": str(row["source_watermark"]),
                        }
                    )
        material = {
            "nm_id": nm_id,
            "feature_epoch": feature_epoch,
            "quantity": canonical_decimal_text(quantity),
            "capital_rub": canonical_decimal_text(capital),
            "wac_rub": (
                canonical_decimal_ratio_text(capital, quantity)
                if quantity > ZERO
                else None
            ),
            "locations": locations,
            "rows": rows,
        }
        result[nm_id] = {**material, "digest": _fingerprint(material)}
    return result


def _functional_pool_mismatches(conn: sqlite3.Connection, version_id: str) -> list[int]:
    rows = conn.execute(
        """SELECT nm_id,quantity,capital_rub,wac_rub,cost_covered_quantity
           FROM sheet_vitrina_v1_warehouse_functional_balances
           WHERE version_id=? AND warehouse_key='ff' ORDER BY nm_id""",
        (version_id,),
    ).fetchall()
    epoch = conn.execute(
        f"SELECT epoch FROM {FEATURE_EPOCHS_TABLE} ORDER BY epoch DESC LIMIT 1"
    ).fetchone()
    pool_only_positive = (
        [
            int(row[0])
            for row in conn.execute(
                f"""SELECT nm_id FROM {BALANCES_TABLE}
                    WHERE projection_epoch=?
                    GROUP BY nm_id
                    HAVING SUM(quantity)<>0
                       OR SUM(CAST(capital_rub AS NUMERIC))<>0
                    ORDER BY nm_id LIMIT ?""",
                (int(epoch[0]), MAX_FUNCTIONAL_BALANCE_ROWS + 1),
            ).fetchall()
        ]
        if epoch is not None
        else []
    )
    if len(pool_only_positive) > MAX_FUNCTIONAL_BALANCE_ROWS:
        raise WarehouseFbsMaterialError(
            "fbs_material_pool_roster_broad",
            "Positive facility/pool roster exceeds the bounded mismatch proof",
        )
    by_nm = {int(row["nm_id"]): row for row in rows}
    result: list[int] = []
    for nm_id in sorted(set(by_nm) | set(pool_only_positive)):
        row = by_nm.get(nm_id)
        if row is None:
            result.append(nm_id)
            continue
        try:
            aggregate = _pool_aggregates(conn, [nm_id])[nm_id]
        except WarehouseFbsMaterialError:
            result.append(nm_id)
            continue
        expected_wac = aggregate["wac_rub"]
        actual_wac = row["wac_rub"]
        if (
            Decimal(str(row["quantity"])) != Decimal(str(aggregate["quantity"]))
            or Decimal(str(row["capital_rub"]))
            != Decimal(str(aggregate["capital_rub"]))
            or Decimal(str(row["cost_covered_quantity"]))
            != Decimal(str(aggregate["quantity"]))
            or (
                (actual_wac is None) != (expected_wac is None)
                or (
                    actual_wac is not None
                    and expected_wac is not None
                    and Decimal(str(actual_wac)) != Decimal(str(expected_wac))
                )
            )
        ):
            result.append(nm_id)
    return result


def _canonical_target_source_evidence(
    conn: sqlite3.Connection,
    *,
    facility_id: str,
    pool: str,
    nm_id: int,
    source_watermark: str,
) -> dict[str, Any] | None:
    """Resolve the current target row to one immutable physical effect."""

    resolved: list[dict[str, Any]] = []
    operation = conn.execute(
        f"""SELECT operation.operation_id,operation.business_date,
                   operation.source_system,operation.source_type,
                   operation.source_id,operation.source_revision
            FROM {OPERATIONS_TABLE} operation
            WHERE operation.operation_id=?
              AND EXISTS(
                  SELECT 1 FROM {LINES_TABLE} line
                  WHERE line.operation_id=operation.operation_id
                    AND line.facility_id=? AND line.pool=? AND line.nm_id=?
              )""",
        (source_watermark, facility_id, pool, nm_id),
    ).fetchone()
    if operation is not None:
        resolved.append(
            {
                "kind": "pool_document_operation",
                "source_watermark": source_watermark,
                "business_date": str(operation["business_date"]),
                "identity_digest": _fingerprint(dict(operation)),
            }
        )
    lifecycle_table = "sheet_vitrina_v1_ff_pool_fbs_lifecycle_events"
    if (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (lifecycle_table,),
        ).fetchone()
        is not None
    ):
        event = conn.execute(
            f"""SELECT event_id,cutover_id,order_id,episode_sequence,event_type,
                       source_revision,status_digest,facility_id,pool,nm_id,
                       physical_quantity_delta,capital_delta_rub,frozen_wac_rub,
                       evidence_digest,occurred_at
                FROM {lifecycle_table}
                WHERE event_id=? AND facility_id=? AND pool=? AND nm_id=?
                  AND physical_quantity_delta<>0""",
            (source_watermark, facility_id, pool, nm_id),
        ).fetchone()
        if event is not None:
            resolved.append(
                {
                    "kind": "fbs_lifecycle_event",
                    "source_watermark": source_watermark,
                    "event_type": str(event["event_type"]),
                    "occurred_at": str(event["occurred_at"]),
                    "identity_digest": _fingerprint(dict(event)),
                }
            )
    return resolved[0] if len(resolved) == 1 else None


def _target_location_changed_since_version(
    conn: sqlite3.Connection,
    *,
    version_id: str,
    facility_id: str,
    pool: str,
    nm_id: int,
    current: Mapping[str, Any],
) -> bool:
    row = conn.execute(
        """SELECT provenance_json
           FROM sheet_vitrina_v1_warehouse_functional_balances
           WHERE version_id=? AND warehouse_key='ff' AND nm_id=?""",
        (version_id, nm_id),
    ).fetchone()
    if row is None:
        return True
    current_quantity = Decimal(str(current["quantity"]))
    current_capital = Decimal(str(current["capital_rub"]))
    evidence: list[tuple[Decimal, Decimal]] = []
    provenance = _loads(row["provenance_json"], {})
    for record in provenance.get("source_records") or []:
        if not isinstance(record, Mapping):
            continue
        for location in record.get("locations") or []:
            if not isinstance(location, Mapping):
                continue
            if (
                str(location.get("facility_id") or "") == facility_id
                and str(location.get("pool") or "").upper() == pool
            ):
                evidence.append(
                    (
                        Decimal(str(location.get("quantity") or 0)),
                        Decimal(str(location.get("capital_rub") or 0)),
                    )
                )
    return not evidence or all(
        quantity != current_quantity or capital != current_capital
        for quantity, capital in evidence
    )


def _mismatch_reason_codes(
    conn: sqlite3.Connection, version_id: str, nm_id: int
) -> list[str]:
    row = conn.execute(
        """SELECT quantity,capital_rub,wac_rub,cost_covered_quantity,provenance_json
           FROM sheet_vitrina_v1_warehouse_functional_balances
           WHERE version_id=? AND warehouse_key='ff' AND nm_id=?""",
        (version_id, nm_id),
    ).fetchone()
    if row is None:
        return ["missing_functional_ff_row"]
    aggregate = _pool_aggregates(conn, [nm_id])[nm_id]
    reasons: list[str] = []
    if Decimal(str(row["cost_covered_quantity"])) != Decimal(str(row["quantity"])):
        reasons.append("ff_cost_coverage_incomplete")
    if Decimal(str(row["quantity"])) != Decimal(str(aggregate["quantity"])) or Decimal(
        str(row["capital_rub"])
    ) != Decimal(str(aggregate["capital_rub"])):
        reasons.append("ff_stage_evidence_mismatch")
    provenance = _loads(row["provenance_json"], {})
    exact_location_sets: list[list[Mapping[str, Any]]] = []
    for item in provenance.get("source_records") or []:
        locations = item.get("locations") if isinstance(item, Mapping) else None
        if not isinstance(locations, list) or not locations:
            continue
        location_quantity = sum(
            (Decimal(str(location.get("quantity") or 0)) for location in locations),
            ZERO,
        )
        location_capital = sum(
            (Decimal(str(location.get("capital_rub") or 0)) for location in locations),
            ZERO,
        )
        if location_quantity == Decimal(
            str(row["quantity"])
        ) and location_capital == Decimal(str(row["capital_rub"])):
            exact_location_sets.append(locations)
    if not exact_location_sets:
        reasons.append("ff_stage_evidence_mismatch")
        reasons.append("missing_facility_pool_evidence")
    return sorted(set(reasons))


def _warehouse_metric_lookup(
    rows: list[Mapping[str, Any]],
    *,
    version_id: str,
    published_at: str,
    source_watermarks: Mapping[str, Any],
    requested_nm_ids: Iterable[int],
) -> dict[int, dict[str, Any]]:
    from packages.application.own_product_capital import (
        _inventory_cost_stage_evidence,
    )
    from packages.application.warehouse_business_projection import _metric_rows

    normalized = [
        {**dict(row), "provenance": _loads(row.get("provenance_json"), {})}
        for row in rows
    ]
    metric_rows = _metric_rows(normalized, affected_nm_ids=requested_nm_ids)
    result: dict[int, dict[str, Any]] = {}
    raw_by_key = {
        (str(row["warehouse_key"]), int(row["nm_id"])): row for row in normalized
    }
    stage_map = {
        "production": "PRODUCTION",
        "china_to_ff": "PRODUCTION_TO_FF",
        "ff": "FF",
        "ff_to_wb": "FF_TO_WB",
        "wb": "WB",
        "wb_acceptance_discrepancy": "WB_ACCEPTANCE_DISCREPANCY",
    }
    for nm_id in sorted({int(value) for value in requested_nm_ids if int(value) > 0}):
        item = deepcopy(dict(metric_rows[nm_id]["metrics"]))
        presentation_reasons: list[str] = []
        inventory_stages: dict[str, Any] = {}
        confirmed_total = ZERO
        for source_stage, public_stage in stage_map.items():
            row = raw_by_key.get((source_stage, nm_id))
            quantity = Decimal(str(row["quantity"])) if row is not None else ZERO
            covered = (
                Decimal(str(row["cost_covered_quantity"])) if row is not None else ZERO
            )
            item[own_stage_metric_key(public_stage, "paid_equivalent_qty")] = float(
                quantity
            )
            item[own_stage_metric_key(public_stage, "cost_coverage_pct")] = (
                float(covered / quantity) if quantity > ZERO else None
            )
            item[own_stage_metric_key(public_stage, "confirmed_share_pct")] = (
                1.0
                if row is not None and quantity > ZERO and bool(row["certified"])
                else 0.0
                if quantity > ZERO
                else None
            )
            confirmed = quantity if row is not None and bool(row["certified"]) else ZERO
            confirmed_total += confirmed
            item[own_stage_metric_key(public_stage, "confirmed_qty")] = float(confirmed)
            item[own_stage_metric_key(public_stage, "cost_covered_qty")] = float(
                covered
            )
            if row is not None and quantity > ZERO and not bool(row["certified"]):
                presentation_reasons.append(str(row["quality"]))
            if row is not None and public_stage in {"WB", "FF"}:
                inventory_stages[public_stage] = _inventory_cost_stage_evidence(
                    row, public_stage=public_stage
                )
        total_quantity = Decimal(str(item.get(OWN_TOTAL_QTY_METRIC_KEY) or 0))
        item[OWN_TOTAL_PAID_EQUIVALENT_QTY_METRIC_KEY] = float(total_quantity)
        item[OWN_TOTAL_CONFIRMED_SHARE_PCT_METRIC_KEY] = (
            float(confirmed_total / total_quantity) if total_quantity > ZERO else None
        )
        item["presentation_state"] = (
            "confirmed"
            if total_quantity <= ZERO or confirmed_total >= total_quantity
            else "unconfirmed"
        )
        item["presentation_reason"] = "; ".join(sorted(set(presentation_reasons)))
        item["presentation_reasons"] = sorted(set(presentation_reasons))
        item["_inventory_cost_stages"] = inventory_stages
        item["_warehouse_version_id"] = version_id
        item["_warehouse_version_is_active"] = True
        item["_warehouse_effective_at"] = published_at
        item["_warehouse_published_at"] = published_at
        item["_warehouse_source_watermarks"] = dict(source_watermarks)
        result[nm_id] = item
    return result


def _active_version(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT version.* FROM sheet_vitrina_v1_warehouse_functional_active active
           JOIN sheet_vitrina_v1_warehouse_functional_versions version
             ON version.version_id=active.version_id WHERE active.slot=1"""
    ).fetchone()


def _ready_snapshots_for_date(
    conn: sqlite3.Connection, business_date: str
) -> list[dict[str, Any]]:
    rows = [
        dict(row)
        for row in conn.execute(
            """SELECT bundle_version,as_of_date,plan_json,refreshed_at,
                      length(CAST(plan_json AS BLOB)) AS plan_bytes
               FROM sheet_vitrina_v1_ready_snapshots
               WHERE as_of_date=? ORDER BY bundle_version,as_of_date LIMIT ?""",
            (business_date, MAX_READY_SNAPSHOTS + 1),
        ).fetchall()
    ]
    return rows


def _ready_cells(payload: Mapping[str, Any], business_date: str) -> dict[str, Any]:
    dates = [str(value)[:10] for value in payload.get("date_columns") or []]
    if business_date not in dates:
        return {}
    index = dates.index(business_date) + 2
    sheets = [
        item
        for item in payload.get("sheets") or []
        if item.get("sheet_name") == "DATA_VITRINA"
    ]
    if len(sheets) != 1:
        return {}
    result: dict[str, Any] = {}
    for row in sheets[0].get("rows") or []:
        if isinstance(row, list) and len(row) > index:
            result[str(row[1])] = row[index]
    return result


def _readback_identity(
    conn: sqlite3.Connection, plan: Mapping[str, Any]
) -> dict[str, Any]:
    active = conn.execute(
        "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
    ).fetchone()
    ready = []
    for update in plan["ready_updates"]:
        row = conn.execute(
            """SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots
               WHERE bundle_version=? AND as_of_date=?""",
            (str(update["bundle_version"]), str(update["as_of_date"])),
        ).fetchone()
        ready.append(
            [
                str(update["bundle_version"]),
                str(update["as_of_date"]),
                _sha_text(str(row[0])) if row is not None else "missing",
            ]
        )
    result = {
        "active_version_id": str(active[0]) if active is not None else "",
        "ready_snapshot_digest": _fingerprint(ready),
    }
    if str(plan.get("mode") or "") == "historical":
        active_version_id = str(active[0]) if active is not None else ""
        sync = conn.execute(
            "SELECT active_version_id FROM sheet_vitrina_v1_warehouse_wb_sync_status "
            "WHERE slot=1"
        ).fetchone()
        candidate = conn.execute(
            "SELECT * FROM sheet_vitrina_v1_warehouse_functional_versions "
            "WHERE version_id=?",
            (str(plan["target_version_id"]),),
        ).fetchone()
        target_row = conn.execute(
            "SELECT quantity,wac_rub,capital_rub,cost_covered_quantity,"
            "provenance_json FROM sheet_vitrina_v1_warehouse_functional_balances "
            "WHERE version_id=? AND warehouse_key='ff' AND nm_id=?",
            (str(plan["target_version_id"]), int(plan["nm_ids"][0])),
        ).fetchone()
        source_material = _source_material(
            conn, active_version_id, nm_ids=plan["nm_ids"]
        )
        projection = conn.execute(
            "SELECT status,source_revision,business_effective_date "
            "FROM sheet_vitrina_v1_warehouse_business_projection_revisions "
            "WHERE published_version_id=?",
            (str(plan["target_version_id"]),),
        ).fetchone()
        ready_closure = [
            {
                "bundle_version": str(item["bundle_version"]),
                "as_of_date": str(item["as_of_date"]),
                "after_plan_sha256": str(item["after_plan_sha256"]),
                "non_target_digest": str(item["non_target_digest"]),
            }
            for item in plan["ready_updates"]
        ]
        result.update(
            {
                "historical_version_id": (
                    str(plan["target_version_id"])
                    if candidate is not None and str(candidate["status"]) == "good"
                    else ""
                ),
                "sync_version_id": str(sync[0]) if sync is not None else "",
                "current_pool_digest": _fingerprint(source_material["pool_rows"]),
                "historical_target_row_digest": (
                    _fingerprint(dict(target_row)) if target_row is not None else ""
                ),
                "historical_target_provenance_digest": (
                    _fingerprint(_loads(target_row["provenance_json"], {}))
                    if target_row is not None
                    else ""
                ),
                "business_projection_revision_digest": (
                    _fingerprint(dict(projection)) if projection is not None else ""
                ),
                "ready_target_total_non_target_closure_digest": _fingerprint(
                    ready_closure
                ),
            }
        )
    return result


def _verify_candidate_readback(
    conn: sqlite3.Connection, candidate: Mapping[str, Any]
) -> None:
    stored = [
        [
            str(row["warehouse_key"]),
            int(row["nm_id"]),
            str(row["quantity"]),
            row["wac_rub"],
            str(row["capital_rub"]),
            str(row["cost_covered_quantity"]),
        ]
        for row in conn.execute(
            """SELECT warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                      cost_covered_quantity
               FROM sheet_vitrina_v1_warehouse_functional_balances
               WHERE version_id=? ORDER BY warehouse_key,nm_id""",
            (str(candidate["target_version_id"]),),
        ).fetchall()
    ]
    expected = [
        [
            str(item["warehouse_key"]),
            int(item["nm_id"]),
            str(item["quantity"]),
            item.get("wac_rub"),
            str(item["capital_rub"]),
            str(item["cost_covered_quantity"]),
        ]
        for item in candidate["lines"]
    ]
    if stored != expected:
        raise WarehouseFbsMaterialError(
            "fbs_material_candidate_readback_mismatch",
            "Candidate functional rows differ from exact planned operands",
        )
    source_auxiliary = _semantic_auxiliary_material(
        conn, str(candidate["source_version_id"])
    )
    target_auxiliary = _semantic_auxiliary_material(
        conn, str(candidate["target_version_id"])
    )
    if source_auxiliary != target_auxiliary:
        raise WarehouseFbsMaterialError(
            "fbs_material_auxiliary_readback_mismatch",
            "Candidate reservation/WB/audit closure differs from its source version",
        )
    document_count = int(
        conn.execute(
            """SELECT COUNT(DISTINCT document.warehouse_key)
               FROM sheet_vitrina_v1_warehouse_functional_documents document
               WHERE document.version_id=? AND document.document_type='fbs_material_revision'""",
            (str(candidate["target_version_id"]),),
        ).fetchone()[0]
    )
    if document_count != len(
        {str(item["warehouse_key"]) for item in candidate["lines"]}
    ):
        raise WarehouseFbsMaterialError(
            "fbs_material_document_readback_mismatch",
            "Candidate version-owned document projection is incomplete",
        )
    projection = conn.execute(
        """SELECT status,source_revision,business_effective_date
           FROM sheet_vitrina_v1_warehouse_business_projection_revisions
           WHERE published_version_id=?""",
        (str(candidate["target_version_id"]),),
    ).fetchone()
    if (
        projection is None
        or str(projection["status"]) != "active"
        or str(projection["source_revision"]) != str(candidate["candidate_fingerprint"])
        or str(projection["business_effective_date"]) != str(candidate["business_date"])
    ):
        raise WarehouseFbsMaterialError(
            "fbs_material_business_projection_readback_mismatch",
            "Candidate business projection identity is incomplete",
        )


def _semantic_auxiliary_material(
    conn: sqlite3.Connection, version_id: str
) -> dict[str, Any]:
    reservations = [
        dict(row)
        for row in conn.execute(
            """SELECT supply_id,nm_id,quantity
               FROM sheet_vitrina_v1_warehouse_functional_ff_reservations
               WHERE version_id=? ORDER BY supply_id,nm_id""",
            (version_id,),
        ).fetchall()
    ]
    unmatched = [
        dict(row)
        for row in conn.execute(
            """SELECT source_id,business_date,nm_id,quantity,matched_quantity,
                      reason,provenance_json
               FROM sheet_vitrina_v1_warehouse_unmatched_doprinato
               WHERE version_id=? ORDER BY business_date,source_id,nm_id,reason""",
            (version_id,),
        ).fetchall()
    ]
    option = conn.execute(
        """SELECT etag,payload_json,payload_bytes
           FROM sheet_vitrina_v1_warehouse_wb_option_read_models
           WHERE version_id=?""",
        (version_id,),
    ).fetchone()
    snapshot = conn.execute(
        """SELECT fetched_at,snapshot_date,requested_nm_ids_json,
                  pagination_complete,page_count,page_offsets_json,raw_row_count,
                  raw_rows_digest,raw_rows_json,items_json
           FROM sheet_vitrina_v1_warehouse_wb_snapshots
           WHERE version_id=? ORDER BY created_at DESC LIMIT 1""",
        (version_id,),
    ).fetchone()
    return {
        "reservations": reservations,
        "unmatched": unmatched,
        "supplier_cost_states": _effective_supplier_state_rows(conn, version_id),
        "wb_option": dict(option) if option is not None else None,
        "wb_snapshot": dict(snapshot) if snapshot is not None else None,
    }


def _source_material(
    conn: sqlite3.Connection,
    version_id: str,
    *,
    nm_ids: Iterable[int],
) -> dict[str, Any]:
    active = conn.execute(
        "SELECT * FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
    ).fetchone()
    version = conn.execute(
        "SELECT * FROM sheet_vitrina_v1_warehouse_functional_versions WHERE version_id=?",
        (version_id,),
    ).fetchone()
    targets = sorted({int(value) for value in nm_ids if int(value) > 0})
    placeholders = ",".join("?" for _ in targets)
    balances = [
        dict(row)
        for row in conn.execute(
            f"""SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances
                WHERE version_id=? AND nm_id IN ({placeholders})
                ORDER BY warehouse_key,nm_id""",
            (version_id, *targets),
        ).fetchall()
    ]
    roster = [
        [str(row[0]), int(row[1])]
        for row in conn.execute(
            """SELECT warehouse_key,nm_id
               FROM sheet_vitrina_v1_warehouse_functional_balances
               WHERE version_id=? ORDER BY warehouse_key,nm_id""",
            (version_id,),
        ).fetchall()
    ]
    epoch = conn.execute(
        f"SELECT * FROM {FEATURE_EPOCHS_TABLE} ORDER BY epoch DESC LIMIT 1"
    ).fetchone()
    pool_rows = [
        dict(row)
        for row in conn.execute(
            f"""SELECT * FROM {BALANCES_TABLE}
                WHERE projection_epoch=? AND nm_id IN ({placeholders})
                ORDER BY facility_id,pool,nm_id""",
            (int(epoch["epoch"]) if epoch is not None else 0, *targets),
        ).fetchall()
    ]
    return {
        "active": dict(active) if active is not None else None,
        "version": dict(version) if version is not None else None,
        "target_balances": balances,
        "functional_roster_count": len(roster),
        "functional_roster_digest": _fingerprint(roster),
        "pool_epoch": dict(epoch) if epoch is not None else None,
        "pool_rows": pool_rows,
    }


def _transition_intent(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    status: str,
    evidence: Mapping[str, Any],
    created_at: str,
    increment_attempt: bool = False,
    error: str | None = None,
) -> None:
    changed = conn.execute(
        f"""UPDATE {INTENTS_TABLE}
            SET status=?,attempt_count=attempt_count+?,last_error=?,updated_at=?
            WHERE operation_id=?""",
        (status, int(increment_attempt), error, created_at, operation_id),
    )
    if changed.rowcount != 1:
        raise WarehouseFbsMaterialError(
            "repair_intent_missing", "Durable material repair intent disappeared"
        )
    _append_intent_event(
        conn,
        operation_id=operation_id,
        status=status,
        evidence=evidence,
        created_at=created_at,
    )


def _append_intent_event(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    status: str,
    evidence: Mapping[str, Any],
    created_at: str,
) -> None:
    conn.execute(
        f"INSERT INTO {EVENTS_TABLE}(operation_id,status,evidence_json,created_at) VALUES(?,?,?,?)",
        (operation_id, status, _json(evidence), created_at),
    )


def _intent_public(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {"contract_name": CONTRACT_NAME, "status": UNSAFE_AMBIGUOUS}
    return {
        "contract_name": CONTRACT_NAME,
        "status": str(row["status"]),
        "operation_id": str(row["operation_id"]),
        "plan_fingerprint": str(row["plan_fingerprint"]),
        "attempt_count": int(row["attempt_count"]),
        "typed_evidence": _loads(row["typed_evidence_json"], {}),
        "last_error": str(row["last_error"] or ""),
    }


def _blocked_plan(
    status: str,
    reason: str,
    *,
    business_date: str,
    facility_id: str,
    pool: str,
    nm_ids: list[int],
    source_version_id: str = "",
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "contract_name": CONTRACT_NAME,
        "status": status,
        "reason": reason,
        "business_date": business_date,
        "facility_id": str(facility_id),
        "pool": str(pool),
        "nm_ids": nm_ids,
        "source_version_id": source_version_id,
        "details": dict(details or {}),
    }


def _recoverable_transport_error(error: Exception) -> bool:
    if isinstance(error, (ConnectionError, TimeoutError)):
        return True
    # PermissionError, FileNotFoundError, ENOSPC and every other generic OS
    # resource/path/capacity failure are not transport evidence.
    if isinstance(error, OSError):
        return False
    if not isinstance(error, sqlite3.OperationalError):
        return False
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "database is locked",
            "database table is locked",
            "database is busy",
        )
    )


def _cell_decimal(value: Any) -> Decimal:
    if value in {None, ""}:
        return ZERO
    try:
        return Decimal(str(value))
    except Exception:
        return ZERO


def _iso_date(value: Any) -> str:
    try:
        return date.fromisoformat(str(value or "")[:10]).isoformat()
    except ValueError as exc:
        raise WarehouseFbsMaterialError(
            "fbs_material_business_date_invalid", "business_date must be YYYY-MM-DD"
        ) from exc


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _is_typed_sha256(value: Any) -> bool:
    token = str(value or "")
    return (
        len(token) == 71
        and token.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in token[7:])
    )


def _sha_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return deepcopy(value)
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return deepcopy(fallback)


def _connect(path: Any) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _connect_readonly(path: Any) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn
