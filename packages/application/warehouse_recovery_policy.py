"""Unified recovery policy for warehouse, cost, and dependent economics writes.

The policy deliberately lives below individual business blocks.  A caller must
classify its mutation before recovery bytes are reserved and then drive the
durable lifecycle around the business transaction.  Bounded operations use
row-level before images; wide warehouse publications use a filtered domain
checkpoint that cannot contain Finance raw tables.  A coherent full-store
backup is available only to explicitly allowlisted schema/store migrations.
"""

from __future__ import annotations

import base64
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
from typing import Any, Callable, Iterator, Mapping, Sequence
from uuid import uuid4

from packages.application.sqlite_contention import connect_sqlite


CONTRACT_NAME = "warehouse_recovery_policy_v1"
REGISTRY_SCHEMA_VERSION = 3
DEFAULT_OPERATIONAL_RESERVE_BYTES = 512 * 1024 * 1024
DEFAULT_RESERVATION_TTL_SECONDS = 6 * 60 * 60
DEFAULT_ROLLBACK_RETENTION_DAYS = 14
RECOVERY_DIRNAME = "warehouse-recovery"
CHECKPOINT_DIRNAME = "domain-checkpoints"
MANIFEST_SUFFIX = ".manifest.json"
TEMP_SUFFIX = ".tmp"
T2_RETENTION_MIN_COUNT = 2
T2_RETENTION_MAX_COUNT = 3
T2_RETENTION_MAX_BYTES = 2 * 1024 * 1024 * 1024
T2_RETENTION_MAX_AGE_HOURS = 24
T2_DEGRADED_FREE_BYTES = 8 * 1024 * 1024 * 1024
T2_HARD_STOP_FREE_BYTES = 4 * 1024 * 1024 * 1024
SANITATION_AUDIT_DIRNAME = "storage-recovery-sanitation"
SANITATION_CONTRACT_NAME = "storage_recovery_sanitation_v1"


class RecoveryPolicyError(RuntimeError):
    """Fail-closed policy or lifecycle violation."""


class RecoveryTier(str, Enum):
    T0 = "T0"
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"


class RecoveryState(str, Enum):
    PLANNED = "planned"
    RESERVED = "reserved"
    WRITING = "writing"
    VERIFIED = "verified"
    MUTATION_RUNNING = "mutation_running"
    RETAINED = "retained"
    RELEASED = "released"
    ROLLED_BACK = "rolled_back"
    FAILED_RECOVERABLE = "failed_recoverable"
    QUARANTINED = "quarantined"
    SUPERSEDED = "superseded"


TERMINAL_STATES = frozenset(
    {
        RecoveryState.RELEASED.value,
        RecoveryState.ROLLED_BACK.value,
        RecoveryState.QUARANTINED.value,
        RecoveryState.SUPERSEDED.value,
    }
)

ALLOWED_TRANSITIONS: Mapping[str, frozenset[str]] = {
    RecoveryState.PLANNED.value: frozenset(
        {
            RecoveryState.RESERVED.value,
            RecoveryState.RELEASED.value,
            RecoveryState.FAILED_RECOVERABLE.value,
            RecoveryState.QUARANTINED.value,
        }
    ),
    RecoveryState.RESERVED.value: frozenset(
        {
            RecoveryState.WRITING.value,
            RecoveryState.FAILED_RECOVERABLE.value,
            RecoveryState.QUARANTINED.value,
        }
    ),
    RecoveryState.WRITING.value: frozenset(
        {
            RecoveryState.VERIFIED.value,
            RecoveryState.FAILED_RECOVERABLE.value,
            RecoveryState.QUARANTINED.value,
        }
    ),
    RecoveryState.VERIFIED.value: frozenset(
        {
            RecoveryState.MUTATION_RUNNING.value,
            RecoveryState.RETAINED.value,
            RecoveryState.RELEASED.value,
            RecoveryState.FAILED_RECOVERABLE.value,
            RecoveryState.QUARANTINED.value,
        }
    ),
    RecoveryState.MUTATION_RUNNING.value: frozenset(
        {
            RecoveryState.RETAINED.value,
            RecoveryState.FAILED_RECOVERABLE.value,
            RecoveryState.QUARANTINED.value,
        }
    ),
    RecoveryState.RETAINED.value: frozenset(
        {
            RecoveryState.RELEASED.value,
            RecoveryState.ROLLED_BACK.value,
            RecoveryState.FAILED_RECOVERABLE.value,
            RecoveryState.QUARANTINED.value,
        }
    ),
    RecoveryState.FAILED_RECOVERABLE.value: frozenset(
        {
            RecoveryState.RESERVED.value,
            RecoveryState.WRITING.value,
            RecoveryState.VERIFIED.value,
            RecoveryState.MUTATION_RUNNING.value,
            RecoveryState.RETAINED.value,
            RecoveryState.RELEASED.value,
            RecoveryState.ROLLED_BACK.value,
            RecoveryState.QUARANTINED.value,
            RecoveryState.SUPERSEDED.value,
        }
    ),
    RecoveryState.RELEASED.value: frozenset(),
    RecoveryState.ROLLED_BACK.value: frozenset(),
    RecoveryState.QUARANTINED.value: frozenset(),
    RecoveryState.SUPERSEDED.value: frozenset(),
}


@dataclass(frozen=True)
class OperationPolicy:
    mutation_kind: str
    tier: RecoveryTier
    closure_kinds: frozenset[str]
    enabled: bool = True


def _policy(
    mutation_kind: str,
    tier: RecoveryTier,
    *closures: str,
    enabled: bool = True,
) -> OperationPolicy:
    return OperationPolicy(
        mutation_kind=mutation_kind,
        tier=tier,
        closure_kinds=frozenset(closures),
        enabled=enabled,
    )


OPERATION_POLICIES: Mapping[str, OperationPolicy] = {
    "supplier_document_confirmation": _policy(
        "supplier_document_confirmation", RecoveryTier.T1, "document", "shipment"
    ),
    "supplier_cost_queue_replay": _policy(
        "supplier_cost_queue_replay", RecoveryTier.T1, "shipment", "sku_date"
    ),
    "supplier_factual_date_correction": _policy(
        "supplier_factual_date_correction", RecoveryTier.T1, "shipment"
    ),
    "ff_ledger_operation": _policy(
        "ff_ledger_operation", RecoveryTier.T1, "document", "shipment", "sku_date"
    ),
    "ff_inventory_reconciliation": _policy(
        "ff_inventory_reconciliation", RecoveryTier.T1, "sku_date"
    ),
    "ff_pool_document_posting": _policy(
        "ff_pool_document_posting", RecoveryTier.T1, "document"
    ),
    "wb_supplies_refresh": _policy(
        "wb_supplies_refresh", RecoveryTier.T1, "shipment", "sku_date"
    ),
    "targeted_warehouse_publication": _policy(
        "targeted_warehouse_publication",
        RecoveryTier.T1,
        "document",
        "shipment",
        "sku_date",
    ),
    "functional_economics_targeted_publication": _policy(
        "functional_economics_targeted_publication", RecoveryTier.T1, "sku_date"
    ),
    "functional_economics_historical_repair": _policy(
        "functional_economics_historical_repair", RecoveryTier.T1, "sku_date"
    ),
    "wbc0027_product_capital_version_bound_recovery": _policy(
        "wbc0027_product_capital_version_bound_recovery",
        RecoveryTier.T1,
        "sku_date",
    ),
    "wbc0027_functional_economics_missing_recovery": _policy(
        "wbc0027_functional_economics_missing_recovery",
        RecoveryTier.T1,
        "sku_date",
    ),
    "calculation_parameters_update": _policy(
        "calculation_parameters_update", RecoveryTier.T1, "date"
    ),
    "warehouse_archival_estimate": _policy(
        "warehouse_archival_estimate", RecoveryTier.T1, "sku_date"
    ),
    "supplier_certification_replay": _policy(
        "supplier_certification_replay", RecoveryTier.T1, "shipment", "sku_date"
    ),
    "canonical_cost_bounded_publication": _policy(
        "canonical_cost_bounded_publication", RecoveryTier.T1, "sku_date"
    ),
    "hourly_warehouse_sync": _policy(
        "hourly_warehouse_sync", RecoveryTier.T2, "warehouse_domain"
    ),
    "manual_warehouse_sync": _policy(
        "manual_warehouse_sync", RecoveryTier.T2, "warehouse_domain"
    ),
    "emergency_warehouse_rebuild": _policy(
        "emergency_warehouse_rebuild", RecoveryTier.T2, "warehouse_domain"
    ),
    "canonical_cost_wide_publication": _policy(
        "canonical_cost_wide_publication", RecoveryTier.T2, "warehouse_domain"
    ),
    "warehouse_opening_publication": _policy(
        "warehouse_opening_publication", RecoveryTier.T2, "warehouse_domain"
    ),
    "fbs_mapping_backlog_publication": _policy(
        "fbs_mapping_backlog_publication", RecoveryTier.T2, "warehouse_domain"
    ),
    "schema_migration": _policy(
        "schema_migration", RecoveryTier.T3, "full_store"
    ),
    "store_migration": _policy(
        "store_migration", RecoveryTier.T3, "full_store"
    ),
    "legacy_invoice_recovery": _policy(
        "legacy_invoice_recovery", RecoveryTier.T1, "shipment", enabled=False
    ),
}

# These identifiers are reviewed code constants, never free-form operator input.
T3_MIGRATION_ALLOWLIST = frozenset(
    {
        "warehouse_functional_cutover_v1",
        "warehouse_recovery_registry_schema_v1",
    }
)

FINANCE_RAW_TABLES = frozenset(
    {
        "wb_finance_weekly_raw_rows",
    }
)

DOMAIN_TABLE_PREFIXES = (
    "sheet_vitrina_v1_warehouse_",
    "sheet_vitrina_v1_calculation_",
    "sheet_vitrina_v1_proxy_",
    "sheet_vitrina_v1_onec_",
    "sheet_vitrina_v1_own_capital_",
    "sheet_vitrina_v1_ff_stock_",
    "sheet_vitrina_v1_ff_pool_",
    "sheet_vitrina_v1_wb_suppl",
    "sheet_vitrina_v1_supplier_",
    "sheet_vitrina_v1_supplier_cost",
    "sheet_vitrina_v1_trade_",
    "sheet_vitrina_v1_invoice_",
    "sheet_vitrina_v1_canonical_cost",
    "cost_price_",
    "wb_finance_",
)

DOMAIN_EXACT_TABLES = frozenset(
    {
        "sheet_vitrina_v1_ready_snapshots",
        "sheet_vitrina_v1_nomenclature_items",
        "sheet_vitrina_v1_ff_facilities",
        "sheet_vitrina_v1_ff_facility_changes",
        "sheet_vitrina_v1_wb_opening_baseline",
        "sheet_vitrina_v1_cny_documents",
        "sheet_vitrina_v1_cny_ledger_operations",
        "sheet_vitrina_v1_cny_ledger_operation_lines",
    }
)

SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SQLITE_BLOB_MARKER = "__wb_core_sqlite_blob_base64__"


@dataclass(frozen=True)
class RecoverySelection:
    mutation_kind: str
    closure_kind: str
    tier: RecoveryTier
    would_change: bool
    migration_id: str
    reason: str

    def public(self) -> dict[str, Any]:
        return {
            "contract_name": CONTRACT_NAME,
            "mutation_kind": self.mutation_kind,
            "closure_kind": self.closure_kind,
            "tier": self.tier.value,
            "would_change": self.would_change,
            "migration_id": self.migration_id or None,
            "reason": self.reason,
        }


def select_recovery_tier(
    *,
    mutation_kind: str,
    closure_kind: str,
    would_change: bool,
    migration_id: str = "",
) -> RecoverySelection:
    """Return exactly one deterministic tier or fail closed."""

    normalized_kind = str(mutation_kind or "").strip()
    normalized_closure = str(closure_kind or "").strip()
    normalized_migration = str(migration_id or "").strip()
    policy = OPERATION_POLICIES.get(normalized_kind)
    if policy is None:
        raise RecoveryPolicyError(
            f"unclassified recovery mutation kind: {normalized_kind or '<empty>'}"
        )
    if not policy.enabled:
        raise RecoveryPolicyError(
            f"legacy mutation entrypoint is disabled: {normalized_kind}"
        )
    if normalized_closure not in policy.closure_kinds:
        raise RecoveryPolicyError(
            "recovery closure is not allowed for mutation kind: "
            f"{normalized_kind}/{normalized_closure or '<empty>'}"
        )
    if not bool(would_change):
        return RecoverySelection(
            mutation_kind=normalized_kind,
            closure_kind=normalized_closure,
            tier=RecoveryTier.T0,
            would_change=False,
            migration_id="",
            reason="semantic_noop",
        )
    if policy.tier is RecoveryTier.T3:
        if normalized_migration not in T3_MIGRATION_ALLOWLIST:
            raise RecoveryPolicyError(
                "T3 full coherent backup requires an explicit allowlisted migration id"
            )
    elif normalized_migration:
        raise RecoveryPolicyError("migration_id is valid only for a T3 operation")
    return RecoverySelection(
        mutation_kind=normalized_kind,
        closure_kind=normalized_closure,
        tier=policy.tier,
        would_change=True,
        migration_id=normalized_migration,
        reason=f"policy:{normalized_kind}:{policy.tier.value}",
    )


def registered_policy_table() -> list[dict[str, Any]]:
    return [
        {
            "mutation_kind": item.mutation_kind,
            "tier": item.tier.value,
            "closure_kinds": sorted(item.closure_kinds),
            "enabled": item.enabled,
        }
        for item in sorted(OPERATION_POLICIES.values(), key=lambda row: row.mutation_kind)
    ]


def recovery_operation_id(
    mutation_kind: str,
    plan_fingerprint: str,
) -> str:
    """Stable public operation identity used by crash/restart callers."""

    return _operation_id(mutation_kind, plan_fingerprint)


@dataclass(frozen=True)
class BeforeImageQuery:
    table: str
    query: str
    parameters: tuple[Any, ...] = ()
    key_columns: tuple[str, ...] = ()


def capture_before_images(
    db_path: Path,
    queries: Sequence[BeforeImageQuery],
) -> tuple[list[dict[str, Any]], int]:
    """Read exact bounded rows without permitting Finance raw reachability."""

    images: list[dict[str, Any]] = []
    read_bytes = 0
    with _connect_readonly(db_path) as conn:
        for spec in queries:
            _require_identifier(spec.table)
            lowered = spec.query.lower()
            if any(table.lower() in lowered for table in FINANCE_RAW_TABLES):
                raise RecoveryPolicyError(
                    "bounded recovery cannot read Finance raw tables"
                )
            rows = [dict(row) for row in conn.execute(spec.query, spec.parameters)]
            for row in rows:
                key_columns = spec.key_columns or tuple(row.keys())
                key = {column: row[column] for column in key_columns}
                image = {
                    "table": spec.table,
                    "key": key,
                    "before": row,
                    "after": None,
                }
                encoded = _json_bytes(_clone(image))
                read_bytes += len(encoded)
                images.append(image)
        if int(conn.total_changes) != 0:
            raise RecoveryPolicyError("bounded before-image capture mutated SQLite")
    return images, read_bytes


class WarehouseRecoveryRegistry:
    """DB-backed recovery registry with CAS lifecycle and capacity ownership."""

    def __init__(
        self,
        *,
        runtime_dir: Path,
        db_path: Path,
        clock: Callable[[], datetime] | None = None,
        fault_injector: Callable[[str, str], None] | None = None,
        operational_reserve_bytes: int = DEFAULT_OPERATIONAL_RESERVE_BYTES,
        recovery_root: Path | None = None,
    ) -> None:
        self.runtime_dir = Path(runtime_dir).resolve()
        self.db_path = Path(db_path).resolve()
        configured_recovery_root = (
            Path(recovery_root).resolve()
            if recovery_root is not None
            else (self.runtime_dir / "backups" / RECOVERY_DIRNAME).resolve()
        )
        allowed_recovery_parent = (self.runtime_dir / "backups").resolve()
        if not _path_is_below(configured_recovery_root, allowed_recovery_parent):
            raise RecoveryPolicyError(
                "warehouse recovery root must stay below the runtime backup root"
            )
        self.recovery_root = configured_recovery_root
        self.legacy_recovery_root = (self.runtime_dir / RECOVERY_DIRNAME).resolve()
        self.recovery_roots = tuple(
            dict.fromkeys((self.recovery_root, self.legacy_recovery_root))
        )
        self.checkpoint_root = self.recovery_root / CHECKPOINT_DIRNAME
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.fault_injector = fault_injector
        self.operational_reserve_bytes = max(int(operational_reserve_bytes), 0)

    def ensure_schema(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.commit()

    def plan_noop(
        self,
        *,
        mutation_kind: str,
        closure_kind: str,
        plan_fingerprint: str,
        scope: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Return T0 evidence without opening the DB or creating any artifact."""

        selection = select_recovery_tier(
            mutation_kind=mutation_kind,
            closure_kind=closure_kind,
            would_change=False,
        )
        return {
            **selection.public(),
            "operation_id": _operation_id(mutation_kind, plan_fingerprint),
            "plan_fingerprint": str(plan_fingerprint),
            "scope": _clone(scope),
            "lifecycle": "released",
            "planned_bytes": 0,
            "actual_bytes": 0,
            "read_bytes": 0,
            "reservation": None,
            "artifacts": [],
            "next_action": "none",
            "rollback": {"available": False, "expires_at": None},
        }

    def prepare_t1(
        self,
        *,
        mutation_kind: str,
        closure_kind: str,
        plan_fingerprint: str,
        scope: Mapping[str, Any],
        before_images: Sequence[Mapping[str, Any]],
        expected_after_images: Sequence[Mapping[str, Any]] = (),
        source_digest: str = "",
        non_target_digest: str = "",
        read_bytes: int = 0,
        rollback_retention_days: int = DEFAULT_ROLLBACK_RETENTION_DAYS,
    ) -> dict[str, Any]:
        selection = select_recovery_tier(
            mutation_kind=mutation_kind,
            closure_kind=closure_kind,
            would_change=True,
        )
        if selection.tier is not RecoveryTier.T1:
            raise RecoveryPolicyError("prepare_t1 requires a T1 operation")
        normalized_images = [
            _normalize_before_image(item, sequence_no=index + 1)
            for index, item in enumerate(before_images)
        ]
        normalized_after = [_clone(item) for item in expected_after_images]
        payload_bytes = len(
            _json_bytes(
                {
                    "before_images": normalized_images,
                    "expected_after_images": normalized_after,
                }
            )
        )
        if payload_bytes <= 0:
            raise RecoveryPolicyError("T1 requires an exact non-empty undo journal")
        expires_at = self.clock() + timedelta(days=max(rollback_retention_days, 1))
        self.ensure_schema()
        operation_id = self._resolve_operation_id(
            mutation_kind=mutation_kind,
            plan_fingerprint=plan_fingerprint,
        )
        existing = self.get_operation(operation_id)
        start_state = RecoveryState.PLANNED.value
        if existing is not None:
            self._verify_existing_identity(
                existing,
                selection=selection,
                plan_fingerprint=plan_fingerprint,
                scope=scope,
            )
            start_state = str(existing["lifecycle"])
            if start_state in {
                RecoveryState.VERIFIED.value,
                RecoveryState.MUTATION_RUNNING.value,
                RecoveryState.RETAINED.value,
                RecoveryState.RELEASED.value,
                RecoveryState.ROLLED_BACK.value,
                RecoveryState.QUARANTINED.value,
                RecoveryState.SUPERSEDED.value,
            }:
                return existing
            if start_state == RecoveryState.FAILED_RECOVERABLE.value:
                if any(
                    artifact.get("artifact_kind") == "undo"
                    and artifact.get("state") == "verified"
                    for artifact in existing.get("artifacts", [])
                ):
                    resume_state = (
                        RecoveryState.MUTATION_RUNNING.value
                        if self._failed_from_state(operation_id)
                        in {
                            RecoveryState.MUTATION_RUNNING.value,
                            RecoveryState.RETAINED.value,
                        }
                        else RecoveryState.VERIFIED.value
                    )
                    self._transition(
                        operation_id,
                        expected_state=RecoveryState.FAILED_RECOVERABLE.value,
                        next_state=resume_state,
                        next_action=(
                            "reconcile_or_complete_business_mutation"
                            if resume_state
                            == RecoveryState.MUTATION_RUNNING.value
                            else "run_business_mutation"
                        ),
                        writer_state=(
                            "resuming"
                            if resume_state
                            == RecoveryState.MUTATION_RUNNING.value
                            else "idle"
                        ),
                    )
                    resumed = self.get_operation(operation_id)
                    if resumed is None:
                        raise RecoveryPolicyError(
                            "T1 operation disappeared during verified resume"
                        )
                    return resumed
                self._transition(
                    operation_id,
                    expected_state=RecoveryState.FAILED_RECOVERABLE.value,
                    next_state=RecoveryState.RESERVED.value,
                    next_action="rewrite_undo_journal",
                    writer_state="resuming",
                )
                start_state = RecoveryState.RESERVED.value
        else:
            self._create_operation(
                operation_id=operation_id,
                selection=selection,
                plan_fingerprint=plan_fingerprint,
                scope=scope,
                planned_bytes=payload_bytes,
                source_digest=source_digest,
                non_target_digest=non_target_digest,
                rollback_expires_at=_timestamp(expires_at),
            )
        try:
            reservation = self._reserve_capacity(
                operation_id=operation_id,
                required_bytes=payload_bytes,
                target_root=self.runtime_dir,
            )
            if start_state == RecoveryState.PLANNED.value:
                self._transition(
                    operation_id,
                    expected_state=RecoveryState.PLANNED.value,
                    next_state=RecoveryState.RESERVED.value,
                    next_action="write_undo_journal",
                )
                start_state = RecoveryState.RESERVED.value
            if start_state == RecoveryState.RESERVED.value:
                self._transition(
                    operation_id,
                    expected_state=RecoveryState.RESERVED.value,
                    next_state=RecoveryState.WRITING.value,
                    next_action="verify_undo_journal",
                )
                start_state = RecoveryState.WRITING.value
            if start_state != RecoveryState.WRITING.value:
                raise RecoveryPolicyError(
                    f"T1 resume cannot continue from lifecycle {start_state}"
                )
            self._inject(operation_id, "before_undo_write")
            with _connect(self.db_path) as conn:
                _ensure_schema(conn)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(
                        "DELETE FROM sheet_vitrina_v1_recovery_undo_rows WHERE operation_id=?",
                        (operation_id,),
                    )
                    conn.execute(
                        """
                        DELETE FROM sheet_vitrina_v1_recovery_artifacts
                        WHERE operation_id=? AND artifact_kind='undo'
                        """,
                        (operation_id,),
                    )
                    for image in normalized_images:
                        conn.execute(
                            """
                            INSERT INTO sheet_vitrina_v1_recovery_undo_rows(
                                operation_id,sequence_no,table_name,key_json,
                                before_json,after_json,action,status,created_at
                            ) VALUES(?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                operation_id,
                                int(image["sequence_no"]),
                                str(image["table"]),
                                _json(image["key"]),
                                _json(image["before"]) if image["before"] is not None else None,
                                _json(image["after"]) if image["after"] is not None else None,
                                str(image["action"]),
                                "verified",
                                self._now(),
                            ),
                        )
                    conn.execute(
                        """
                        INSERT INTO sheet_vitrina_v1_recovery_artifacts(
                            artifact_id,operation_id,artifact_kind,path,size_bytes,
                            digest,state,created_at,expires_at,metadata_json
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            f"{operation_id}:undo",
                            operation_id,
                            "undo",
                            "",
                            payload_bytes,
                            "sha256:" + hashlib.sha256(
                                _json_bytes(normalized_images)
                            ).hexdigest(),
                            "verified",
                            self._now(),
                            _timestamp(expires_at),
                            _json(
                                {
                                    "row_count": len(normalized_images),
                                    "expected_after_images": normalized_after,
                                }
                            ),
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE sheet_vitrina_v1_recovery_operations
                        SET actual_bytes=?,read_bytes=?,checkpoint_digest=?,
                            updated_at=?,last_heartbeat_at=?
                        WHERE operation_id=? AND lifecycle_state=?
                        """,
                        (
                            payload_bytes,
                            max(int(read_bytes), 0),
                            "sha256:" + hashlib.sha256(
                                _json_bytes(normalized_images)
                            ).hexdigest(),
                            self._now(),
                            self._now(),
                            operation_id,
                            RecoveryState.WRITING.value,
                        ),
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            self._fsync_registry()
            self._inject(operation_id, "after_undo_write")
            self._assert_post_write_reserve(self.runtime_dir)
            self._transition(
                operation_id,
                expected_state=RecoveryState.WRITING.value,
                next_state=RecoveryState.VERIFIED.value,
                next_action="run_business_mutation",
            )
            self._mark_reservation_consumed(reservation["reservation_id"])
        except Exception as exc:
            self.fail_recoverable(
                operation_id,
                error=str(exc),
                next_action="resume_prepare_t1",
            )
            raise
        result = self.get_operation(operation_id)
        if result is None:
            raise RecoveryPolicyError("T1 operation disappeared after verification")
        return result

    def prepare_t1_from_queries(
        self,
        *,
        mutation_kind: str,
        closure_kind: str,
        plan_fingerprint: str,
        scope: Mapping[str, Any],
        queries: Sequence[BeforeImageQuery],
        expected_after_images: Sequence[Mapping[str, Any]] = (),
        source_digest: str = "",
        non_target_digest: str = "",
    ) -> dict[str, Any]:
        images, read_bytes = capture_before_images(self.db_path, queries)
        return self.prepare_t1(
            mutation_kind=mutation_kind,
            closure_kind=closure_kind,
            plan_fingerprint=plan_fingerprint,
            scope=scope,
            before_images=images,
            expected_after_images=expected_after_images,
            source_digest=source_digest,
            non_target_digest=non_target_digest,
            read_bytes=read_bytes,
        )

    def prepare_t2(
        self,
        *,
        mutation_kind: str,
        plan_fingerprint: str,
        scope: Mapping[str, Any],
        source_digest: str,
        non_target_digest: str,
        source_watermarks: Mapping[str, Any],
        schema_revision: str = "",
    ) -> dict[str, Any]:
        selection = select_recovery_tier(
            mutation_kind=mutation_kind,
            closure_kind="warehouse_domain",
            would_change=True,
        )
        if selection.tier is not RecoveryTier.T2:
            raise RecoveryPolicyError("prepare_t2 requires a T2 operation")
        self.ensure_schema()
        operation_id = self._resolve_operation_id(
            mutation_kind=mutation_kind,
            plan_fingerprint=plan_fingerprint,
        )
        existing = self.get_operation(operation_id)
        if existing is not None:
            self._verify_existing_identity(
                existing,
                selection=selection,
                plan_fingerprint=plan_fingerprint,
                scope=scope,
            )
            current = str(existing["lifecycle"])
            if current in {
                RecoveryState.VERIFIED.value,
                RecoveryState.MUTATION_RUNNING.value,
                RecoveryState.RETAINED.value,
                RecoveryState.RELEASED.value,
                RecoveryState.ROLLED_BACK.value,
                RecoveryState.QUARANTINED.value,
                RecoveryState.SUPERSEDED.value,
            }:
                return existing
            if current == RecoveryState.FAILED_RECOVERABLE.value:
                checkpoint_artifact = next(
                    (
                        artifact
                        for artifact in existing.get("artifacts", [])
                        if artifact.get("artifact_kind") == "domain_checkpoint"
                        and artifact.get("state") == "verified"
                    ),
                    None,
                )
                if checkpoint_artifact is not None and Path(
                    str(checkpoint_artifact.get("path") or "")
                ).is_file():
                    resume_state = (
                        RecoveryState.MUTATION_RUNNING.value
                        if self._failed_from_state(operation_id)
                        in {
                            RecoveryState.MUTATION_RUNNING.value,
                            RecoveryState.RETAINED.value,
                        }
                        else RecoveryState.VERIFIED.value
                    )
                    self._transition(
                        operation_id,
                        expected_state=RecoveryState.FAILED_RECOVERABLE.value,
                        next_state=resume_state,
                        next_action=(
                            "reconcile_or_complete_business_mutation"
                            if resume_state
                            == RecoveryState.MUTATION_RUNNING.value
                            else "run_business_mutation"
                        ),
                        writer_state=(
                            "resuming"
                            if resume_state
                            == RecoveryState.MUTATION_RUNNING.value
                            else "idle"
                        ),
                    )
                    resumed = self.get_operation(operation_id)
                    if resumed is None:
                        raise RecoveryPolicyError(
                            "T2 operation disappeared during verified resume"
                        )
                    return resumed
                self._transition(
                    operation_id,
                    expected_state=RecoveryState.FAILED_RECOVERABLE.value,
                    next_state=RecoveryState.RESERVED.value,
                    next_action="rewrite_domain_checkpoint",
                    writer_state="resuming",
                )
                with _connect(self.db_path) as conn:
                    conn.execute(
                        """
                        DELETE FROM sheet_vitrina_v1_recovery_artifacts
                        WHERE operation_id=? AND artifact_kind IN ('domain_checkpoint','manifest')
                        """,
                        (operation_id,),
                    )
                    conn.commit()
            elif current not in {
                RecoveryState.PLANNED.value,
                RecoveryState.RESERVED.value,
                RecoveryState.WRITING.value,
            }:
                raise RecoveryPolicyError(
                    f"T2 resume cannot continue from lifecycle {current}"
                )
            # Continue below from the durable state already reached.
            start_state = (
                RecoveryState.RESERVED.value
                if current == RecoveryState.FAILED_RECOVERABLE.value
                else current
            )
        else:
            start_state = RecoveryState.PLANNED.value
        table_names, planned_bytes = self._domain_inventory()
        if not table_names:
            raise RecoveryPolicyError("warehouse domain checkpoint has no tables")
        if existing is None:
            self._create_operation(
                operation_id=operation_id,
                selection=selection,
                plan_fingerprint=plan_fingerprint,
                scope=scope,
                planned_bytes=planned_bytes,
                source_digest=source_digest,
                non_target_digest=non_target_digest,
                rollback_expires_at=_timestamp(
                    self.clock() + timedelta(days=DEFAULT_ROLLBACK_RETENTION_DAYS)
                ),
            )
        self.checkpoint_root.mkdir(parents=True, exist_ok=True)
        reservation = self._reserve_capacity(
            operation_id=operation_id,
            required_bytes=max(planned_bytes, 1),
            target_root=self.checkpoint_root,
        )
        if start_state == RecoveryState.PLANNED.value:
            self._transition(
                operation_id,
                expected_state=RecoveryState.PLANNED.value,
                next_state=RecoveryState.RESERVED.value,
                next_action="write_domain_checkpoint",
            )
            start_state = RecoveryState.RESERVED.value
        if start_state == RecoveryState.RESERVED.value:
            self._transition(
                operation_id,
                expected_state=RecoveryState.RESERVED.value,
                next_state=RecoveryState.WRITING.value,
                next_action="verify_domain_checkpoint",
            )
            start_state = RecoveryState.WRITING.value
        if start_state != RecoveryState.WRITING.value:
            raise RecoveryPolicyError(
                f"T2 resume cannot continue from lifecycle {start_state}"
            )
        checkpoint = self.checkpoint_root / f"{operation_id}.sqlite3"
        temporary = checkpoint.with_name(checkpoint.name + TEMP_SUFFIX)
        manifest_path = checkpoint.with_name(checkpoint.name + MANIFEST_SUFFIX)
        try:
            self._inject(operation_id, "before_checkpoint_write")
            write_result = self._write_domain_checkpoint(
                temporary=temporary,
                final=checkpoint,
                table_names=table_names,
                operation_id=operation_id,
                plan_fingerprint=plan_fingerprint,
                source_digest=source_digest,
                source_watermarks=source_watermarks,
                schema_revision=schema_revision,
            )
            self._inject(operation_id, "after_checkpoint_fsync")
            manifest = {
                "contract_name": CONTRACT_NAME,
                "schema_version": REGISTRY_SCHEMA_VERSION,
                "operation_id": operation_id,
                "tier": RecoveryTier.T2.value,
                "plan_fingerprint": plan_fingerprint,
                "source_digest": source_digest,
                "non_target_digest": non_target_digest,
                "checkpoint_path": str(checkpoint),
                "checkpoint_size_bytes": int(write_result["size_bytes"]),
                "checkpoint_sha256": str(write_result["sha256"]),
                "table_names": table_names,
                "source_watermarks": _clone(source_watermarks),
                "schema_revision": schema_revision,
                "finance_raw_opened": False,
                "created_at": self._now(),
            }
            _atomic_write_json(manifest_path, manifest)
            self._inject(operation_id, "after_manifest_rename")
            self._assert_post_write_reserve(self.checkpoint_root)
            with _connect(self.db_path) as conn:
                _ensure_schema(conn)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for kind, path, size, digest in (
                        (
                            "domain_checkpoint",
                            checkpoint,
                            int(write_result["size_bytes"]),
                            str(write_result["sha256"]),
                        ),
                        (
                            "manifest",
                            manifest_path,
                            manifest_path.stat().st_size,
                            _sha256_file(manifest_path),
                        ),
                    ):
                        conn.execute(
                            """
                            INSERT INTO sheet_vitrina_v1_recovery_artifacts(
                                artifact_id,operation_id,artifact_kind,path,size_bytes,
                                digest,state,created_at,expires_at,metadata_json
                            ) VALUES(?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                f"{operation_id}:{kind}",
                                operation_id,
                                kind,
                                str(path),
                                size,
                                digest,
                                "verified",
                                self._now(),
                                _timestamp(
                                    self.clock()
                                    + timedelta(days=DEFAULT_ROLLBACK_RETENTION_DAYS)
                                ),
                                _json({"table_count": len(table_names)}),
                            ),
                        )
                    conn.execute(
                        """
                        UPDATE sheet_vitrina_v1_recovery_operations
                        SET actual_bytes=?,read_bytes=?,checkpoint_digest=?,
                            updated_at=?,last_heartbeat_at=?
                        WHERE operation_id=? AND lifecycle_state=?
                        """,
                        (
                            int(write_result["size_bytes"]),
                            int(write_result["read_bytes"]),
                            str(write_result["sha256"]),
                            self._now(),
                            self._now(),
                            operation_id,
                            RecoveryState.WRITING.value,
                        ),
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            self._transition(
                operation_id,
                expected_state=RecoveryState.WRITING.value,
                next_state=RecoveryState.VERIFIED.value,
                next_action="run_business_mutation",
            )
            self._mark_reservation_consumed(reservation["reservation_id"])
        except Exception as exc:
            self.fail_recoverable(
                operation_id,
                error=str(exc),
                next_action="resume_or_quarantine_domain_checkpoint",
            )
            raise
        result = self.get_operation(operation_id)
        if result is None:
            raise RecoveryPolicyError("T2 operation disappeared after verification")
        return result

    def write_disposable_domain_checkpoint(
        self,
        destination: Path,
        *,
        purpose: str,
    ) -> dict[str, Any]:
        """Create a temporary warehouse-domain-only planning database.

        This is not recovery evidence and therefore creates no registry row or
        capacity reservation.  It exists solely for mutation-free planning and
        is required to live outside the runtime recovery directory.
        """

        final = Path(destination).resolve()
        if any(_path_is_below(final, root) for root in self.recovery_roots):
            raise RecoveryPolicyError(
                "disposable planning checkpoint cannot masquerade as recovery evidence"
            )
        final.parent.mkdir(parents=True, exist_ok=True)
        tables, _ = self._domain_inventory()
        result = self._write_domain_checkpoint(
            temporary=Path(str(final) + TEMP_SUFFIX),
            final=final,
            table_names=tables,
            operation_id="disposable_domain_checkpoint",
            plan_fingerprint="sha256:"
            + hashlib.sha256(str(purpose).encode("utf-8")).hexdigest(),
            source_digest="planning_only",
            source_watermarks={"purpose": str(purpose)},
            schema_revision=str(REGISTRY_SCHEMA_VERSION),
        )
        return {
            **result,
            "kind": "disposable_warehouse_domain_checkpoint",
            "finance_raw_included": False,
            "registry_rows_created": 0,
            "table_count": len(tables),
        }

    def domain_content_digest(self) -> dict[str, Any]:
        """Hash the warehouse/cost domain row stream without opening Finance raw."""

        digest = hashlib.sha256()
        read_bytes = 0
        row_count = 0
        with _connect_readonly(self.db_path) as conn:
            tables = [
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name NOT LIKE 'sqlite_%'
                    ORDER BY name
                    """
                )
                if _is_domain_table(str(row[0]))
            ]
            if any(table in FINANCE_RAW_TABLES for table in tables):
                raise RecoveryPolicyError(
                    "Finance raw reached warehouse domain content digest"
                )
            for table in tables:
                _require_identifier(table)
                schema = conn.execute(
                    "SELECT COALESCE(sql,'') FROM sqlite_master "
                    "WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                header = _json_bytes(
                    {"table": table, "schema": str(schema[0] if schema else "")}
                )
                digest.update(header)
                read_bytes += len(header)
                cursor = conn.execute(f"SELECT * FROM {_quoted(table)}")
                while True:
                    rows = cursor.fetchmany(500)
                    if not rows:
                        break
                    for row in rows:
                        encoded = _json_bytes(
                            [_hashable_sqlite_value(value) for value in row]
                        )
                        digest.update(encoded)
                        read_bytes += len(encoded)
                        row_count += 1
        return {
            "digest": "sha256:" + digest.hexdigest(),
            "table_count": len(tables),
            "row_count": row_count,
            "read_bytes": read_bytes,
            "finance_raw_opened": False,
        }

    def prepare_t3(
        self,
        *,
        runtime: Any,
        mutation_kind: str,
        migration_id: str,
        plan_fingerprint: str,
        scope: Mapping[str, Any],
        destination: Path,
        source_digest: str = "",
    ) -> dict[str, Any]:
        selection = select_recovery_tier(
            mutation_kind=mutation_kind,
            closure_kind="full_store",
            would_change=True,
            migration_id=migration_id,
        )
        self.ensure_schema()
        operation_id = self._resolve_operation_id(
            mutation_kind=mutation_kind,
            plan_fingerprint=plan_fingerprint,
        )
        existing = self.get_operation(operation_id)
        if existing is not None:
            self._verify_existing_identity(
                existing,
                selection=selection,
                plan_fingerprint=plan_fingerprint,
                scope=scope,
            )
            return existing
        planned_bytes = int(runtime.coherent_backup_size_bytes())
        self._create_operation(
            operation_id=operation_id,
            selection=selection,
            plan_fingerprint=plan_fingerprint,
            scope={**dict(scope), "migration_id": migration_id},
            planned_bytes=planned_bytes,
            source_digest=source_digest,
            non_target_digest="",
            rollback_expires_at=_timestamp(
                self.clock() + timedelta(days=DEFAULT_ROLLBACK_RETENTION_DAYS)
            ),
        )
        reservation = self._reserve_capacity(
            operation_id=operation_id,
            required_bytes=planned_bytes,
            target_root=Path(destination).parent,
        )
        self._transition(
            operation_id,
            expected_state=RecoveryState.PLANNED.value,
            next_state=RecoveryState.RESERVED.value,
            next_action="write_allowlisted_full_backup",
        )
        self._transition(
            operation_id,
            expected_state=RecoveryState.RESERVED.value,
            next_state=RecoveryState.WRITING.value,
            next_action="verify_allowlisted_full_backup",
        )
        try:
            backup = runtime.backup_database(
                Path(destination),
                admission_owner="warehouse_recovery_policy",
            )
            if str(backup.get("integrity_check") or "").lower() != "ok":
                raise RecoveryPolicyError("T3 coherent backup integrity check failed")
            backup_path = Path(str(backup["path"]))
            artifacts = [
                {
                    "kind": "raw",
                    "path": backup_path,
                    "size_bytes": int(backup["size_bytes"]),
                    "digest": "sha256:"
                    + str(backup["sha256"]).removeprefix("sha256:"),
                }
            ]
            for suffix, kind in (
                ("-wal", "wal"),
                ("-shm", "shm"),
                ("-journal", "journal"),
            ):
                sidecar = Path(str(backup_path) + suffix)
                if sidecar.is_file():
                    artifacts.append(
                        {
                            "kind": kind,
                            "path": sidecar,
                            "size_bytes": sidecar.stat().st_size,
                            "digest": _sha256_file(sidecar),
                        }
                    )
            actual_bytes = sum(
                int(artifact["size_bytes"]) for artifact in artifacts
            )
            with _connect(self.db_path) as conn:
                _ensure_schema(conn)
                for artifact in artifacts:
                    conn.execute(
                        """
                        INSERT INTO sheet_vitrina_v1_recovery_artifacts(
                            artifact_id,operation_id,artifact_kind,path,size_bytes,digest,
                            state,created_at,expires_at,metadata_json
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            f"{operation_id}:{artifact['kind']}",
                            operation_id,
                            str(artifact["kind"]),
                            str(artifact["path"]),
                            int(artifact["size_bytes"]),
                            str(artifact["digest"]),
                            "verified",
                            self._now(),
                            _timestamp(
                                self.clock()
                                + timedelta(days=DEFAULT_ROLLBACK_RETENTION_DAYS)
                            ),
                            _json({"migration_id": migration_id}),
                        ),
                    )
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_recovery_operations
                    SET actual_bytes=?,read_bytes=?,checkpoint_digest=?,
                        updated_at=?,last_heartbeat_at=?
                    WHERE operation_id=? AND lifecycle_state=?
                    """,
                    (
                        actual_bytes,
                        int(backup["size_bytes"]) * 2
                        + actual_bytes
                        - int(backup["size_bytes"]),
                        "sha256:" + str(backup["sha256"]).removeprefix("sha256:"),
                        self._now(),
                        self._now(),
                        operation_id,
                        RecoveryState.WRITING.value,
                    ),
                )
                conn.commit()
            self._transition(
                operation_id,
                expected_state=RecoveryState.WRITING.value,
                next_state=RecoveryState.VERIFIED.value,
                next_action="run_schema_or_store_migration",
            )
            self._mark_reservation_consumed(reservation["reservation_id"])
        except Exception as exc:
            self.fail_recoverable(
                operation_id,
                error=str(exc),
                next_action="resume_allowlisted_full_backup",
            )
            raise
        result = self.get_operation(operation_id)
        if result is None:
            raise RecoveryPolicyError("T3 operation disappeared after verification")
        return result

    def begin_mutation(
        self,
        operation_id: str,
        *,
        expected_source_digest: str = "",
        writer_state: str = "running",
    ) -> dict[str, Any]:
        operation = self.get_operation(operation_id)
        if operation is None:
            raise RecoveryPolicyError("unknown recovery operation")
        if operation.get("lifecycle") == RecoveryState.MUTATION_RUNNING.value:
            return operation
        if expected_source_digest and expected_source_digest != str(
            operation.get("source_digest") or ""
        ):
            self.quarantine(operation_id, "source_digest_drift_before_mutation")
            raise RecoveryPolicyError("recovery source digest drifted before mutation")
        self._inject(operation_id, "before_business_mutation")
        self._transition(
            operation_id,
            expected_state=RecoveryState.VERIFIED.value,
            next_state=RecoveryState.MUTATION_RUNNING.value,
            next_action="complete_and_verify_business_mutation",
            writer_state=writer_state,
        )
        result = self.get_operation(operation_id)
        if result is None:
            raise RecoveryPolicyError("recovery operation disappeared before mutation")
        return result

    def retain(
        self,
        operation_id: str,
        *,
        after_digest: str,
        non_target_digest: str = "",
        timer_state: str = "",
    ) -> dict[str, Any]:
        operation = self.get_operation(operation_id)
        if operation is None:
            raise RecoveryPolicyError("unknown recovery operation")
        if (
            non_target_digest
            and operation.get("non_target_digest")
            and non_target_digest != operation.get("non_target_digest")
        ):
            self.quarantine(operation_id, "non_target_digest_drift_after_mutation")
            raise RecoveryPolicyError("non-target digest changed after mutation")
        current = str(operation["lifecycle"])
        if current == RecoveryState.RETAINED.value:
            return operation
        self._inject(operation_id, "after_business_mutation")
        self._transition(
            operation_id,
            expected_state=current,
            next_state=RecoveryState.RETAINED.value,
            next_action="rollback_available_until_expiry",
            after_digest=after_digest,
            writer_state="idle",
            timer_state=timer_state,
        )
        result = self.get_operation(operation_id)
        if result is None:
            raise RecoveryPolicyError("recovery operation disappeared after retain")
        return result

    def record_mutation_commit(
        self,
        conn: sqlite3.Connection,
        operation_id: str,
        *,
        after_digest: str,
        non_target_digest: str,
    ) -> None:
        """Persist exact post-COMMIT truth in the business transaction itself.

        The caller invokes this after its in-transaction target/non-target
        readback and immediately before ``commit()``.  Therefore these fields
        can become durable only together with the business rows.  A later
        retain/readback failure must not erase the fact that one submit wrote
        the database.
        """

        exact_after = str(after_digest or "").strip()
        exact_non_target = str(non_target_digest or "").strip()
        if not exact_after or not exact_non_target:
            raise RecoveryPolicyError(
                "committed mutation requires exact after and non-target digests"
            )
        row = conn.execute(
            "SELECT lifecycle_state,after_digest,non_target_digest "
            "FROM sheet_vitrina_v1_recovery_operations WHERE operation_id=?",
            (str(operation_id),),
        ).fetchone()
        if row is None:
            raise RecoveryPolicyError("unknown recovery operation at commit boundary")
        current = dict(row)
        if str(current["lifecycle_state"]) != RecoveryState.MUTATION_RUNNING.value:
            raise RecoveryPolicyError(
                "recovery operation is not mutation-running at commit boundary"
            )
        if str(current.get("non_target_digest") or "") != exact_non_target:
            raise RecoveryPolicyError(
                "recovery non-target digest drifted before committed mutation"
            )
        prior_after = str(current.get("after_digest") or "")
        if prior_after and prior_after != exact_after:
            raise RecoveryPolicyError("recovery after digest already owns another commit")
        now = self._now()
        cursor = conn.execute(
            """
            UPDATE sheet_vitrina_v1_recovery_operations
            SET after_digest=?,next_action='same_operation_query_only_reconciliation',
                writer_state='committed_pending_reconciliation',
                updated_at=?,last_heartbeat_at=?
            WHERE operation_id=? AND lifecycle_state=?
              AND non_target_digest=? AND (after_digest='' OR after_digest=?)
            """,
            (
                exact_after,
                now,
                now,
                str(operation_id),
                RecoveryState.MUTATION_RUNNING.value,
                exact_non_target,
                exact_after,
            ),
        )
        if cursor.rowcount != 1:
            raise RecoveryPolicyError("committed mutation truth CAS update lost")

    def fail_recoverable(
        self,
        operation_id: str,
        *,
        error: str,
        next_action: str,
    ) -> dict[str, Any] | None:
        operation = self.get_operation(operation_id)
        if operation is None:
            return None
        current = str(operation["lifecycle"])
        if current in TERMINAL_STATES:
            return operation
        if current == RecoveryState.FAILED_RECOVERABLE.value:
            with _connect(self.db_path) as conn:
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_recovery_operations
                    SET last_error=?,next_action=?,writer_state='failed',
                        updated_at=?,last_heartbeat_at=?
                    WHERE operation_id=?
                    """,
                    (str(error), str(next_action), self._now(), self._now(), operation_id),
                )
                conn.commit()
        else:
            self._transition(
                operation_id,
                expected_state=current,
                next_state=RecoveryState.FAILED_RECOVERABLE.value,
                next_action=next_action,
                last_error=error,
                writer_state="failed",
            )
        return self.get_operation(operation_id)

    def supersede_failed_operation(
        self,
        operation_id: str,
        *,
        superseding_operation_id: str,
        proof_contract: str,
        proof_fingerprint: str,
        proof: Mapping[str, Any],
        actor: str,
        authorization_reference: str,
    ) -> dict[str, Any]:
        """Terminalize one failed Stage 7C checkpoint by exact later proof.

        The failed operation, its transitions and every checkpoint artifact stay
        intact.  This method appends one immutable relation and one ordinary CAS
        lifecycle transition; it never releases recovery bytes or rewrites the
        earlier failure evidence.
        """

        target_id = str(operation_id or "").strip()
        replacement_id = str(superseding_operation_id or "").strip()
        contract = str(proof_contract or "").strip()
        fingerprint = str(proof_fingerprint or "").strip().lower()
        operator = str(actor or "").strip()
        authorization = str(authorization_reference or "").strip()
        if not target_id or not replacement_id or target_id == replacement_id:
            raise RecoveryPolicyError(
                "distinct target and superseding recovery operations are required"
            )
        if not contract or not operator or not authorization:
            raise RecoveryPolicyError(
                "supersession proof contract, actor and authorization reference are required"
            )
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint):
            raise RecoveryPolicyError("supersession proof fingerprint must be exact sha256")
        normalized_proof = _clone(proof)
        expected_fingerprint = "sha256:" + hashlib.sha256(
            _json_bytes(normalized_proof)
        ).hexdigest()
        if fingerprint != expected_fingerprint:
            raise RecoveryPolicyError("supersession proof fingerprint mismatch")
        if str(normalized_proof.get("contract_name") or "") != contract:
            raise RecoveryPolicyError("supersession proof contract mismatch")
        if str(normalized_proof.get("target_operation_id") or "") != target_id:
            raise RecoveryPolicyError("supersession proof target mismatch")
        if (
            str(normalized_proof.get("superseding_operation_id") or "")
            != replacement_id
        ):
            raise RecoveryPolicyError("supersession proof replacement mismatch")

        supersession_id = (
            "recovery_supersession_" + fingerprint.removeprefix("sha256:")[:32]
        )
        now = self._now()
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                target = conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_recovery_operations "
                    "WHERE operation_id=?",
                    (target_id,),
                ).fetchone()
                replacement = conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_recovery_operations "
                    "WHERE operation_id=?",
                    (replacement_id,),
                ).fetchone()
                if target is None or replacement is None:
                    raise RecoveryPolicyError(
                        "target and superseding recovery operations must exist"
                    )
                target_row = dict(target)
                replacement_row = dict(replacement)
                existing_relation = conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_recovery_supersessions "
                    "WHERE target_operation_id=?",
                    (target_id,),
                ).fetchone()
                if str(target_row["lifecycle_state"]) == RecoveryState.SUPERSEDED.value:
                    if (
                        existing_relation is None
                        or str(existing_relation["superseding_operation_id"])
                        != replacement_id
                        or str(existing_relation["proof_fingerprint"])
                        != fingerprint
                    ):
                        raise RecoveryPolicyError(
                            "superseded recovery owns another immutable proof"
                        )
                    conn.rollback()
                    result = self.get_operation(target_id)
                    if result is None:
                        raise RecoveryPolicyError(
                            "superseded recovery operation disappeared"
                        )
                    return result
                if existing_relation is not None:
                    raise RecoveryPolicyError(
                        "failed recovery already owns an immutable supersession relation"
                    )
                if (
                    str(target_row["operation_kind"])
                    != "warehouse_opening_publication"
                    or str(target_row["closure_kind"]) != "warehouse_domain"
                    or str(target_row["tier"]) != RecoveryTier.T2.value
                    or str(target_row["lifecycle_state"])
                    != RecoveryState.FAILED_RECOVERABLE.value
                    or str(target_row["next_action"])
                    != "exact_ff_pool_cutover_readback_or_retry"
                ):
                    raise RecoveryPolicyError(
                        "only the exact failed Stage 7C T2 recovery is supersedable"
                    )
                if (
                    str(replacement_row["operation_kind"])
                    != str(target_row["operation_kind"])
                    or str(replacement_row["closure_kind"])
                    != str(target_row["closure_kind"])
                    or str(replacement_row["tier"]) != RecoveryTier.T2.value
                    or str(replacement_row["lifecycle_state"])
                    not in {
                        RecoveryState.RETAINED.value,
                        RecoveryState.RELEASED.value,
                    }
                    or not str(replacement_row["after_digest"] or "")
                    or str(replacement_row["created_at"])
                    <= str(target_row["updated_at"])
                ):
                    raise RecoveryPolicyError(
                        "superseding recovery is not a later successful Stage 7C T2 operation"
                    )
                target_proof = dict(normalized_proof.get("target_operation") or {})
                replacement_proof = dict(
                    normalized_proof.get("superseding_operation") or {}
                )
                expected_target = {
                    "operation_id": target_id,
                    "plan_fingerprint": str(target_row["plan_fingerprint"]),
                    "state_version": int(target_row["state_version"]),
                    "checkpoint_digest": str(target_row["checkpoint_digest"]),
                }
                expected_replacement = {
                    "operation_id": replacement_id,
                    "plan_fingerprint": str(replacement_row["plan_fingerprint"]),
                    "state_version": int(replacement_row["state_version"]),
                    "after_digest": str(replacement_row["after_digest"]),
                }
                if target_proof != expected_target:
                    raise RecoveryPolicyError("target recovery proof drifted before apply")
                if replacement_proof != expected_replacement:
                    raise RecoveryPolicyError(
                        "superseding recovery proof drifted before apply"
                    )
                pre_change = dict(normalized_proof.get("pre_change") or {})
                current_target_row_digest = "sha256:" + hashlib.sha256(
                    _json_bytes(
                        {
                            "operation_id": target_id,
                            "lifecycle": str(target_row["lifecycle_state"]),
                            "state_version": int(target_row["state_version"]),
                            "next_action": str(target_row["next_action"]),
                            "writer_state": str(target_row["writer_state"]),
                            "rollback_available": int(
                                target_row["rollback_available"]
                            ),
                            "checkpoint_digest": str(
                                target_row["checkpoint_digest"]
                            ),
                        }
                    )
                ).hexdigest()
                target_failure = dict(
                    normalized_proof.get("target_failure") or {}
                )
                if (
                    str(pre_change.get("target_row_digest") or "")
                    != current_target_row_digest
                    or pre_change.get("supersession_relation_absent") is not True
                    or str(target_failure.get("last_error") or "")
                    != str(target_row["last_error"])
                    or dict(normalized_proof.get("target_scope") or {})
                    != _json_object(target_row["target_scope_json"])
                ):
                    raise RecoveryPolicyError(
                        "target recovery row or scope drifted before supersession"
                    )

                next_version = int(target_row["state_version"]) + 1
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_recovery_supersessions(
                        supersession_id,target_operation_id,
                        superseding_operation_id,proof_contract,
                        proof_fingerprint,proof_json,actor,
                        authorization_reference,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        supersession_id,
                        target_id,
                        replacement_id,
                        contract,
                        fingerprint,
                        _json(normalized_proof),
                        operator,
                        authorization,
                        now,
                    ),
                )
                cursor = conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_recovery_operations
                    SET lifecycle_state=?,state_version=?,next_action='none',
                        writer_state='idle',
                        updated_at=?,last_heartbeat_at=?
                    WHERE operation_id=? AND lifecycle_state=? AND state_version=?
                    """,
                    (
                        RecoveryState.SUPERSEDED.value,
                        next_version,
                        now,
                        now,
                        target_id,
                        RecoveryState.FAILED_RECOVERABLE.value,
                        int(target_row["state_version"]),
                    ),
                )
                if cursor.rowcount != 1:
                    raise RecoveryPolicyError(
                        "supersession lifecycle CAS update lost"
                    )
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_recovery_transitions(
                        operation_id,from_state,to_state,state_version,
                        transitioned_at,detail_json
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        target_id,
                        RecoveryState.FAILED_RECOVERABLE.value,
                        RecoveryState.SUPERSEDED.value,
                        next_version,
                        now,
                        _json(
                            {
                                "next_action": "none",
                                "supersession_id": supersession_id,
                                "superseding_operation_id": replacement_id,
                                "proof_contract": contract,
                                "proof_fingerprint": fingerprint,
                                "artifacts_preserved": True,
                            }
                        ),
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        self._fsync_registry()
        self._inject(target_id, f"after_transition:{RecoveryState.SUPERSEDED.value}")
        result = self.get_operation(target_id)
        if result is None:
            raise RecoveryPolicyError("superseded recovery operation disappeared")
        return result

    def quarantine(self, operation_id: str, reason: str) -> dict[str, Any]:
        operation = self.get_operation(operation_id)
        if operation is None:
            raise RecoveryPolicyError("unknown recovery operation")
        current = str(operation["lifecycle"])
        if current == RecoveryState.QUARANTINED.value:
            return operation
        self._transition(
            operation_id,
            expected_state=current,
            next_state=RecoveryState.QUARANTINED.value,
            next_action="operator_review_quarantine",
            quarantine_reason=str(reason),
            writer_state="blocked",
        )
        result = self.get_operation(operation_id)
        if result is None:
            raise RecoveryPolicyError("quarantined operation disappeared")
        return result

    def rollback_t1(
        self,
        operation_id: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        operation = self.get_operation(operation_id)
        if operation is None or operation.get("tier") != RecoveryTier.T1.value:
            raise RecoveryPolicyError("T1 recovery operation is required")
        if operation.get("lifecycle") not in {
            RecoveryState.RETAINED.value,
            RecoveryState.FAILED_RECOVERABLE.value,
        }:
            raise RecoveryPolicyError("T1 rollback is not available in current lifecycle")
        undo_rows = self._undo_rows(operation_id)
        if not undo_rows:
            raise RecoveryPolicyError("T1 undo journal is missing")
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                for item in reversed(undo_rows):
                    _apply_undo_row(conn, item)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        current = str((self.get_operation(operation_id) or operation)["lifecycle"])
        self._transition(
            operation_id,
            expected_state=current,
            next_state=RecoveryState.ROLLED_BACK.value,
            next_action="none",
            last_error=f"rolled back: {reason}",
            writer_state="idle",
        )
        result = self.get_operation(operation_id)
        if result is None:
            raise RecoveryPolicyError("rolled-back operation disappeared")
        return result

    def rollback_t2(
        self,
        operation_id: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        """Restore only the warehouse/cost domain from a verified checkpoint."""

        operation = self.get_operation(operation_id)
        if operation is None or operation.get("tier") != RecoveryTier.T2.value:
            raise RecoveryPolicyError("T2 recovery operation is required")
        if operation.get("lifecycle") not in {
            RecoveryState.RETAINED.value,
            RecoveryState.FAILED_RECOVERABLE.value,
        }:
            raise RecoveryPolicyError("T2 rollback is not available in current lifecycle")
        artifact = next(
            (
                item
                for item in operation.get("artifacts", [])
                if item.get("artifact_kind") == "domain_checkpoint"
                and item.get("state") == "verified"
            ),
            None,
        )
        if artifact is None:
            raise RecoveryPolicyError("T2 domain checkpoint artifact is missing")
        checkpoint = Path(str(artifact.get("path") or ""))
        if (
            not checkpoint.is_file()
            or not any(
                _path_is_below(checkpoint, root)
                for root in self.recovery_roots
            )
            or _sha256_file(checkpoint) != str(artifact.get("digest") or "")
        ):
            self.quarantine(operation_id, "t2_checkpoint_identity_drift")
            raise RecoveryPolicyError("T2 checkpoint identity verification failed")
        try:
            with _connect_readonly(checkpoint) as source, _connect(self.db_path) as target:
                checkpoint_tables = [
                    str(row[0])
                    for row in source.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type='table'
                          AND name<>'recovery_checkpoint_metadata'
                          AND name NOT LIKE 'sqlite_%'
                        ORDER BY name
                        """
                    )
                ]
                if any(not _is_domain_table(table) for table in checkpoint_tables):
                    raise RecoveryPolicyError(
                        "T2 checkpoint contains a non-domain table"
                    )
                target.execute("PRAGMA foreign_keys=OFF")
                target.execute("BEGIN IMMEDIATE")
                try:
                    # A wide publication may create a domain table that did not
                    # exist when the checkpoint was taken.  Restoring only the
                    # checkpoint tables would leak that mutation.  The writer
                    # lock serializes domain writers, so tables currently in
                    # the domain but absent from the checkpoint belong to this
                    # operation and must be removed.
                    current_domain_tables = [
                        str(row[0])
                        for row in target.execute(
                            """
                            SELECT name FROM sqlite_master
                            WHERE type='table' AND name NOT LIKE 'sqlite_%'
                            ORDER BY name
                            """
                        )
                        if _is_domain_table(str(row[0]))
                    ]
                    # Recreate the entire checkpointed domain, rather than
                    # deleting rows in the current tables.  A wide publication
                    # may also alter a table or its indexes before failing; row
                    # replay alone would leave that schema drift behind.
                    for table in reversed(sorted(current_domain_tables)):
                        _require_identifier(table)
                        target.execute(f"DROP TABLE {_quoted(table)}")
                    for table in checkpoint_tables:
                        _require_identifier(table)
                        schema = source.execute(
                            """
                            SELECT sql FROM sqlite_master
                            WHERE type='table' AND name=?
                            """,
                            (table,),
                        ).fetchone()
                        if schema is None or not schema[0]:
                            raise RecoveryPolicyError(
                                f"T2 rollback schema is missing: {table}"
                            )
                        target.execute(str(schema[0]))
                    for table in checkpoint_tables:
                        columns = [
                            str(row[1])
                            for row in source.execute(
                                f"PRAGMA table_info({_quoted(table)})"
                            )
                        ]
                        if not columns:
                            continue
                        placeholders = ",".join("?" for _ in columns)
                        insert = (
                            f"INSERT INTO {_quoted(table)}("
                            + ",".join(_quoted(column) for column in columns)
                            + f") VALUES({placeholders})"
                        )
                        cursor = source.execute(f"SELECT * FROM {_quoted(table)}")
                        while True:
                            rows = cursor.fetchmany(500)
                            if not rows:
                                break
                            target.executemany(insert, [tuple(row) for row in rows])
                    for row in source.execute(
                        """
                        SELECT type,name,sql FROM sqlite_master
                        WHERE type IN ('index','trigger') AND sql IS NOT NULL
                          AND tbl_name IN ({})
                        ORDER BY CASE type WHEN 'index' THEN 0 ELSE 1 END,name
                        """.format(",".join("?" for _ in checkpoint_tables)),
                        tuple(checkpoint_tables),
                    ):
                        if not row[2]:
                            raise RecoveryPolicyError(
                                f"T2 rollback {row[0]} schema is missing: {row[1]}"
                            )
                        target.execute(str(row[2]))
                    target.commit()
                except Exception:
                    target.rollback()
                    raise
        except Exception as exc:
            self.fail_recoverable(
                operation_id,
                error=f"T2 rollback failed: {exc}",
                next_action="retry_t2_domain_rollback",
            )
            raise
        current = str((self.get_operation(operation_id) or operation)["lifecycle"])
        self._transition(
            operation_id,
            expected_state=current,
            next_state=RecoveryState.ROLLED_BACK.value,
            next_action="none",
            last_error=f"rolled back domain checkpoint: {reason}",
            writer_state="idle",
        )
        result = self.get_operation(operation_id)
        if result is None:
            raise RecoveryPolicyError("rolled-back T2 operation disappeared")
        return result

    def plan_retention(self) -> dict[str, Any]:
        """Build one stable exact plan over age, count and retained bytes."""

        now = self.clock()
        now_text = _timestamp(now)
        operations = self.list_operations(limit=1000)
        eligible = [
            item
            for item in operations
            if item.get("lifecycle")
            in {
                RecoveryState.RETAINED.value,
                RecoveryState.ROLLED_BACK.value,
            }
        ]
        retained_t2 = sorted(
            (
                item
                for item in eligible
                if item.get("tier") == RecoveryTier.T2.value
                and item.get("lifecycle") == RecoveryState.RETAINED.value
            ),
            key=lambda item: (
                str(item.get("created_at") or ""),
                str(item.get("operation_id") or ""),
            ),
            reverse=True,
        )
        protected_ids = {
            str(item["operation_id"])
            for item in retained_t2[:T2_RETENTION_MIN_COUNT]
        }
        retained_bytes = sum(
            int(item.get("actual_bytes") or 0) for item in retained_t2
        )
        running_bytes = 0
        candidates: list[dict[str, Any]] = []
        for index, operation in enumerate(retained_t2):
            operation_id = str(operation["operation_id"])
            operation_bytes = int(operation.get("actual_bytes") or 0)
            running_bytes += operation_bytes
            if operation_id in protected_ids:
                continue
            reasons: list[str] = []
            created_at = _parse_timestamp(str(operation.get("created_at") or ""))
            age_hours = max(
                0.0,
                (now - created_at).total_seconds() / 3600,
            )
            if index >= T2_RETENTION_MAX_COUNT:
                reasons.append("superseded_count_cap")
            if running_bytes > T2_RETENTION_MAX_BYTES:
                reasons.append("projected_byte_cap")
            if age_hours >= T2_RETENTION_MAX_AGE_HOURS:
                reasons.append("rollback_age_cap")
            if reasons:
                candidates.append(
                    _retention_candidate(operation, reasons=reasons)
                )

        t2_candidate_ids = {
            str(item["operation_id"]) for item in candidates
        }
        for operation in eligible:
            operation_id = str(operation["operation_id"])
            if operation_id in t2_candidate_ids:
                continue
            expires_at = str(
                (operation.get("rollback") or {}).get("expires_at") or ""
            )
            if (
                expires_at
                and expires_at <= now_text
                and operation_id not in protected_ids
            ):
                candidates.append(
                    _retention_candidate(
                        operation,
                        reasons=["rollback_expired"],
                    )
                )

        candidates.sort(
            key=lambda item: (
                str(item.get("created_at") or ""),
                str(item.get("operation_id") or ""),
            )
        )
        material = {
            "contract_name": CONTRACT_NAME,
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "action": "bounded_retention",
            "policy": {
                "t2_min_count": T2_RETENTION_MIN_COUNT,
                "t2_max_count": T2_RETENTION_MAX_COUNT,
                "t2_max_bytes": T2_RETENTION_MAX_BYTES,
                "t2_max_age_hours": T2_RETENTION_MAX_AGE_HOURS,
                "protected_lifecycle_states": [
                    RecoveryState.PLANNED.value,
                    RecoveryState.RESERVED.value,
                    RecoveryState.WRITING.value,
                    RecoveryState.VERIFIED.value,
                    RecoveryState.MUTATION_RUNNING.value,
                    RecoveryState.FAILED_RECOVERABLE.value,
                    RecoveryState.QUARANTINED.value,
                ],
            },
            "protected_operation_ids": sorted(protected_ids),
            "candidates": candidates,
        }
        fingerprint = "sha256:" + hashlib.sha256(
            _json_bytes(material)
        ).hexdigest()
        projected = self._retention_projection(
            retained_t2=retained_t2,
            candidate_ids={
                str(item["operation_id"]) for item in candidates
            },
        )
        return {
            **material,
            "generated_at": now_text,
            "status": "dry_run_ready",
            "fingerprint": fingerprint,
            "would_change": bool(candidates),
            # Backward-compatible alias retained for the original
            # release_expired contract and existing operator tooling.
            "operation_ids": [
                str(item["operation_id"]) for item in candidates
            ],
            "candidate_count": len(candidates),
            "candidate_bytes": sum(
                sum(
                    int(artifact.get("size_bytes") or 0)
                    for artifact in item.get("artifacts", [])
                    if artifact.get("path")
                )
                for item in candidates
            ),
            "retained_t2_count": len(retained_t2),
            "retained_t2_bytes": retained_bytes,
            "projection": projected,
        }

    def apply_retention(self, *, plan_fingerprint: str) -> dict[str, Any]:
        """Apply or resume one audited exact retention plan."""

        approved = str(plan_fingerprint or "").strip()
        if not approved:
            raise RecoveryPolicyError("exact retention plan fingerprint is required")
        self.ensure_schema()
        with _connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_recovery_retention_runs
                WHERE plan_fingerprint=?
                """,
                (approved,),
            ).fetchone()
        stored_removed_paths: list[str] = []
        stored_removed_bytes = 0
        if row is None:
            plan = self.plan_retention()
            if approved != str(plan["fingerprint"]):
                raise RecoveryPolicyError(
                    "exact retention plan fingerprint is stale"
                )
            retention_run_id = (
                "retention_"
                + approved.removeprefix("sha256:")[:24]
            )
            now = self._now()
            with _connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_recovery_retention_runs(
                        retention_run_id,plan_fingerprint,plan_json,status,
                        removed_bytes,removed_paths_json,error_json,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        retention_run_id,
                        approved,
                        _json(plan),
                        "applying",
                        0,
                        "[]",
                        "[]",
                        now,
                        now,
                    ),
                )
                conn.commit()
            self._fsync_registry()
            self._inject(retention_run_id, "after_retention_audit_start")
        else:
            stored = dict(row)
            stored_removed_paths = [
                str(path)
                for path in _json_array(
                    stored.get("removed_paths_json") or "[]"
                )
            ]
            stored_removed_bytes = int(stored.get("removed_bytes") or 0)
            retention_run_id = str(stored["retention_run_id"])
            plan = _json_object(stored["plan_json"])
            if str(plan.get("fingerprint") or "") != approved:
                raise RecoveryPolicyError("retention audit plan fingerprint drifted")
            if str(stored.get("status") or "") == "applied":
                return {
                    **plan,
                    "status": "applied",
                    "idempotent": True,
                    "retention_run_id": retention_run_id,
                    "removed_bytes": int(stored.get("removed_bytes") or 0),
                    "removed_paths": _json_array(
                        stored.get("removed_paths_json") or "[]"
                    ),
                    "errors": _json_array(stored.get("error_json") or "[]"),
                    "projection": self.plan_retention()["projection"],
                }

        removed_paths: list[str] = list(stored_removed_paths)
        removed_bytes = stored_removed_bytes
        errors: list[dict[str, Any]] = []
        for candidate in plan.get("candidates", []):
            operation_id = str(candidate.get("operation_id") or "")
            with _connect_readonly(self.db_path) as conn:
                operation_row = conn.execute(
                    """
                    SELECT lifecycle_state,state_version
                    FROM sheet_vitrina_v1_recovery_operations
                    WHERE operation_id=?
                    """,
                    (operation_id,),
                ).fetchone()
            if operation_row is None:
                errors.append(
                    {
                        "operation_id": operation_id,
                        "error": "retention_operation_missing",
                    }
                )
                continue
            lifecycle = str(operation_row["lifecycle_state"])
            if lifecycle == RecoveryState.RELEASED.value:
                with _connect(self.db_path) as conn:
                    conn.execute(
                        """
                        DELETE FROM sheet_vitrina_v1_recovery_undo_rows
                        WHERE operation_id=?
                        """,
                        (operation_id,),
                    )
                    conn.execute(
                        """
                        UPDATE sheet_vitrina_v1_recovery_artifacts
                        SET state='released' WHERE operation_id=?
                        """,
                        (operation_id,),
                    )
                    conn.commit()
                continue
            if (
                lifecycle != str(candidate.get("lifecycle") or "")
                or int(operation_row["state_version"])
                != int(candidate.get("state_version") or 0)
            ):
                errors.append(
                    {
                        "operation_id": operation_id,
                        "error": "retention_operation_cas_drift",
                        "actual_lifecycle": lifecycle,
                        "actual_state_version": int(operation_row["state_version"]),
                    }
                )
                continue
            operation_error = False
            for artifact in candidate.get("artifacts", []):
                path_value = str(artifact.get("path") or "")
                if not path_value:
                    continue
                path = Path(path_value)
                if not any(
                    _path_is_below(path, root) for root in self.recovery_roots
                ):
                    operation_error = True
                    errors.append(
                        {
                            "operation_id": operation_id,
                            "path": path_value,
                            "error": "retention_path_outside_recovery_roots",
                        }
                    )
                    if lifecycle == RecoveryState.RETAINED.value:
                        self.quarantine(
                            operation_id,
                            "retention_path_outside_recovery_roots",
                        )
                    break
                if not path.exists():
                    continue
                if path.is_symlink() or not path.is_file():
                    operation_error = True
                    errors.append(
                        {
                            "operation_id": operation_id,
                            "path": path_value,
                            "error": "retention_artifact_not_regular",
                        }
                    )
                    break
                expected_size = int(artifact.get("size_bytes") or 0)
                expected_digest = str(artifact.get("digest") or "")
                if (
                    path.stat().st_size != expected_size
                    or (expected_digest and _sha256_file(path) != expected_digest)
                ):
                    operation_error = True
                    errors.append(
                        {
                            "operation_id": operation_id,
                            "path": path_value,
                            "error": "retention_artifact_digest_drift",
                        }
                    )
                    if lifecycle == RecoveryState.RETAINED.value:
                        self.quarantine(
                            operation_id,
                            "retention_artifact_digest_drift",
                        )
                    break
                self._inject(retention_run_id, f"before_retention_unlink:{operation_id}")
                path.unlink()
                _fsync_directory(path.parent)
                removed_paths.append(str(path))
                removed_bytes += expected_size
                self._inject(retention_run_id, f"after_retention_unlink:{operation_id}")
            if operation_error:
                continue
            if lifecycle == RecoveryState.RETAINED.value:
                self._transition(
                    operation_id,
                    expected_state=RecoveryState.RETAINED.value,
                    next_state=RecoveryState.RELEASED.value,
                    next_action="none",
                )
            with _connect(self.db_path) as conn:
                conn.execute(
                    """
                    DELETE FROM sheet_vitrina_v1_recovery_undo_rows
                    WHERE operation_id=?
                    """,
                    (operation_id,),
                )
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_recovery_artifacts
                    SET state='released' WHERE operation_id=?
                    """,
                    (operation_id,),
                )
                conn.commit()

        status = "applied" if not errors else "partial_failure"
        with _connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_recovery_retention_runs
                SET status=?,removed_bytes=?,
                    removed_paths_json=?,error_json=?,updated_at=?
                WHERE plan_fingerprint=?
                """,
                (
                    status,
                    removed_bytes,
                    _json(sorted(set(removed_paths))),
                    _json(errors),
                    self._now(),
                    approved,
                ),
            )
            conn.commit()
        self._fsync_registry()
        return {
            **plan,
            "status": status,
            "idempotent": False,
            "retention_run_id": retention_run_id,
            "removed_bytes": removed_bytes,
            "removed_paths": sorted(set(removed_paths)),
            "errors": errors,
            "projection": self.plan_retention()["projection"],
        }

    def release_expired(
        self,
        *,
        apply: bool = False,
        plan_fingerprint: str = "",
    ) -> dict[str, Any]:
        """Backward-compatible entrypoint for the bounded retention policy."""

        if not apply:
            return self.plan_retention()
        return self.apply_retention(plan_fingerprint=plan_fingerprint)

    def _retention_projection(
        self,
        *,
        retained_t2: Sequence[Mapping[str, Any]],
        candidate_ids: set[str],
    ) -> dict[str, Any]:
        hourly = sorted(
            (
                item
                for item in retained_t2
                if item.get("operation_kind") == "hourly_warehouse_sync"
            ),
            key=lambda item: str(item.get("created_at") or ""),
        )
        deltas = [
            max(
                1,
                int(
                    (
                        _parse_timestamp(str(after["created_at"]))
                        - _parse_timestamp(str(before["created_at"]))
                    ).total_seconds()
                ),
            )
            for before, after in zip(hourly, hourly[1:])
        ]
        cadence_seconds = (
            sorted(deltas)[len(deltas) // 2] if deltas else 3600
        )
        recent_checkpoint_bytes = max(
            (int(item.get("actual_bytes") or 0) for item in hourly[-3:]),
            default=0,
        )
        current_bytes = sum(
            int(item.get("actual_bytes") or 0) for item in retained_t2
        )
        candidate_bytes = sum(
            int(item.get("actual_bytes") or 0)
            for item in retained_t2
            if str(item.get("operation_id") or "") in candidate_ids
        )
        bounded_bytes_after = max(0, current_bytes - candidate_bytes)
        capacity = self.capacity_status()
        free_after_plan = int(capacity["free_bytes"]) + candidate_bytes
        next_cycle_peak_available = (
            free_after_plan - recent_checkpoint_bytes
        )
        return {
            "observed_cadence_seconds": cadence_seconds,
            "recent_checkpoint_bytes": recent_checkpoint_bytes,
            "current_retained_bytes": current_bytes,
            "projected_24h_without_gc_bytes": current_bytes
            + ((24 * 3600) // cadence_seconds) * recent_checkpoint_bytes,
            "projected_14d_without_gc_bytes": current_bytes
            + ((14 * 24 * 3600) // cadence_seconds) * recent_checkpoint_bytes,
            "bounded_bytes_after_plan": bounded_bytes_after,
            "steady_state_cap_bytes": T2_RETENTION_MAX_BYTES,
            "steady_state_count_cap": T2_RETENTION_MAX_COUNT,
            "projected_30d_growth_bytes": 0,
            "filesystem_free_after_plan_bytes": free_after_plan,
            "next_cycle_peak_available_bytes": next_cycle_peak_available,
            "thirty_day_headroom_bytes": next_cycle_peak_available
            - T2_HARD_STOP_FREE_BYTES,
            "hard_stop": next_cycle_peak_available < T2_HARD_STOP_FREE_BYTES,
            "degraded": next_cycle_peak_available < T2_DEGRADED_FREE_BYTES,
        }

    def release_failed_canary_pre_mutations(self) -> dict[str, Any]:
        """Release exact failed canary evidence that never reached mutation."""

        candidates = [
            operation
            for operation in self.list_operations(limit=1000)
            if operation.get("lifecycle")
            == RecoveryState.FAILED_RECOVERABLE.value
            and bool((operation.get("scope") or {}).get("canary"))
            and operation.get("tier") in {RecoveryTier.T1.value, RecoveryTier.T2.value}
            and self._failed_from_state(str(operation["operation_id"]))
            in {
                RecoveryState.PLANNED.value,
                RecoveryState.RESERVED.value,
                RecoveryState.WRITING.value,
                RecoveryState.VERIFIED.value,
            }
        ]
        released: list[str] = []
        removed_paths: list[str] = []
        for operation in candidates:
            operation_id = str(operation["operation_id"])
            owned_paths = {
                Path(str(artifact["path"]))
                for artifact in operation.get("artifacts", [])
                if artifact.get("path")
            }
            if operation.get("tier") == RecoveryTier.T2.value:
                checkpoint = self.checkpoint_root / f"{operation_id}.sqlite3"
                owned_paths.update(
                    {
                        checkpoint,
                        checkpoint.with_name(checkpoint.name + TEMP_SUFFIX),
                        checkpoint.with_name(checkpoint.name + MANIFEST_SUFFIX),
                        Path(str(checkpoint) + "-wal"),
                        Path(str(checkpoint) + "-shm"),
                        Path(str(checkpoint) + "-journal"),
                    }
                )
            for path in sorted(owned_paths):
                if not any(
                    _path_is_below(path, root)
                    for root in self.recovery_roots
                ):
                    self.quarantine(
                        operation_id,
                        "failed_canary_release_path_outside_recovery_root",
                    )
                    raise RecoveryPolicyError(
                        "failed canary artifact escaped recovery root"
                    )
                if path.is_symlink() or (path.exists() and not path.is_file()):
                    self.quarantine(
                        operation_id,
                        "failed_canary_release_unsafe_path",
                    )
                    raise RecoveryPolicyError(
                        "failed canary artifact path is not a regular file"
                    )
                if path.is_file():
                    path.unlink()
                    removed_paths.append(str(path))
            if self.checkpoint_root.is_dir():
                _fsync_directory(self.checkpoint_root)
            with _connect(self.db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(
                        "DELETE FROM sheet_vitrina_v1_recovery_undo_rows "
                        "WHERE operation_id=?",
                        (operation_id,),
                    )
                    conn.execute(
                        "UPDATE sheet_vitrina_v1_recovery_artifacts "
                        "SET state='released' WHERE operation_id=?",
                        (operation_id,),
                    )
                    conn.execute(
                        """
                        UPDATE sheet_vitrina_v1_recovery_capacity_reservations
                        SET state='released',released_at=?
                        WHERE operation_id=? AND state IN ('active','consumed')
                        """,
                        (self._now(), operation_id),
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            self._transition(
                operation_id,
                expected_state=RecoveryState.FAILED_RECOVERABLE.value,
                next_state=RecoveryState.RELEASED.value,
                next_action="none",
                last_error="released failed pre-mutation canary evidence",
                writer_state="idle",
            )
            released.append(operation_id)
        return {
            "status": "released" if released else "noop",
            "released_operation_ids": released,
            "removed_paths": removed_paths,
        }

    def reconcile_failed_hourly_precheckpoint_locks(
        self,
        *,
        current_active_version_id: str,
    ) -> dict[str, Any]:
        """Release exact hourly T2 lock failures before any checkpoint existed.

        This is deliberately narrower than an ordinary T2 resume.  It preserves
        the failed operation and its error/transition evidence, and may only
        terminalize a pre-checkpoint lock failure whose pinned base version is
        still the exact active functional version.  Any artifact, owned path,
        byte, digest, unexpected scope/state or concurrent CAS change remains a
        protected failure for operator reconciliation.  The sole accepted path
        trace is an unregistered ``.sqlite3.tmp`` containing only the empty
        recovery metadata schema created before the source read failed; it is
        preserved in place and bound into the terminal transition evidence.
        """

        active_version_id = str(current_active_version_id or "").strip()
        if not active_version_id:
            return {
                "contract": "hourly_t2_precheckpoint_lock_reconciliation_v1",
                "status": "not_applicable",
                "reason": "active_functional_version_missing",
                "released_operation_ids": [],
            }
        candidates = [
            operation
            for operation in self.list_operations(limit=1000)
            if operation.get("operation_kind") == "hourly_warehouse_sync"
            and operation.get("tier") == RecoveryTier.T2.value
            and operation.get("lifecycle")
            == RecoveryState.FAILED_RECOVERABLE.value
        ]
        released: list[str] = []
        rejected: list[dict[str, str]] = []
        preserved_precheckpoint_traces: list[dict[str, Any]] = []
        for operation in candidates:
            operation_id = str(operation.get("operation_id") or "")
            scope = dict(operation.get("scope") or {})
            reasons: list[str] = []
            if set(scope) != {"base_active_version_id", "effective_date"}:
                reasons.append("target_scope_not_exact")
            if str(scope.get("base_active_version_id") or "") != active_version_id:
                reasons.append("base_active_version_drift")
            if not str(scope.get("effective_date") or "").strip():
                reasons.append("effective_date_missing")
            if self._failed_from_state(operation_id) != RecoveryState.WRITING.value:
                reasons.append("failure_not_from_checkpoint_writing")
            if int(operation.get("actual_bytes") or 0) != 0:
                reasons.append("checkpoint_bytes_present")
            if int(operation.get("read_bytes") or 0) != 0:
                reasons.append("checkpoint_read_bytes_present")
            if str(operation.get("checkpoint_digest") or ""):
                reasons.append("checkpoint_digest_present")
            if str(operation.get("after_digest") or ""):
                reasons.append("business_after_digest_present")
            if list(operation.get("artifacts") or []):
                reasons.append("registered_artifacts_present")
            if str(operation.get("next_action") or "") != (
                "resume_or_quarantine_domain_checkpoint"
            ):
                reasons.append("unexpected_next_action")
            if str(operation.get("writer_state") or "") != "failed":
                reasons.append("writer_state_not_failed")
            if str(operation.get("last_error") or "").strip().lower() not in {
                "database is locked",
                "database table is locked",
            }:
                reasons.append("failure_not_exact_sqlite_lock")

            owned_paths: set[Path] = set()
            temporary_paths: set[Path] = set()
            for root in self.recovery_roots:
                checkpoint = root / CHECKPOINT_DIRNAME / f"{operation_id}.sqlite3"
                temporary = checkpoint.with_name(checkpoint.name + TEMP_SUFFIX)
                temporary_paths.add(temporary)
                owned_paths.update(
                    {
                        checkpoint,
                        temporary,
                        checkpoint.with_name(checkpoint.name + MANIFEST_SUFFIX),
                        Path(str(checkpoint) + "-wal"),
                        Path(str(checkpoint) + "-shm"),
                        Path(str(checkpoint) + "-journal"),
                    }
                )
            present_owned_paths = sorted(
                path for path in owned_paths if path.exists() or path.is_symlink()
            )
            precheckpoint_trace: dict[str, Any] | None = None
            if present_owned_paths:
                if (
                    len(present_owned_paths) == 1
                    and present_owned_paths[0] in temporary_paths
                ):
                    try:
                        precheckpoint_trace = _empty_precheckpoint_trace(
                            present_owned_paths[0]
                        )
                    except RecoveryPolicyError:
                        reasons.append("owned_checkpoint_path_present")
                else:
                    reasons.append("owned_checkpoint_path_present")
            if reasons:
                rejected.append(
                    {"operation_id": operation_id, "reason": ",".join(sorted(reasons))}
                )
                continue

            now = self._now()
            with _connect(self.db_path) as conn:
                _ensure_schema(conn)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    row = conn.execute(
                        """SELECT lifecycle_state,state_version,operation_kind,tier,
                                  target_scope_json,actual_bytes,read_bytes,
                                  checkpoint_digest,after_digest,next_action,
                                  writer_state,last_error
                           FROM sheet_vitrina_v1_recovery_operations
                           WHERE operation_id=?""",
                        (operation_id,),
                    ).fetchone()
                    failed_from = conn.execute(
                        """SELECT from_state
                           FROM sheet_vitrina_v1_recovery_transitions
                           WHERE operation_id=? AND to_state=?
                           ORDER BY transition_id DESC LIMIT 1""",
                        (operation_id, RecoveryState.FAILED_RECOVERABLE.value),
                    ).fetchone()
                    artifact_count = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM sheet_vitrina_v1_recovery_artifacts "
                            "WHERE operation_id=?",
                            (operation_id,),
                        ).fetchone()[0]
                    )
                    undo_count = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM sheet_vitrina_v1_recovery_undo_rows "
                            "WHERE operation_id=?",
                            (operation_id,),
                        ).fetchone()[0]
                    )
                    unsafe_reservation_count = int(
                        conn.execute(
                            """SELECT COUNT(*)
                               FROM sheet_vitrina_v1_recovery_capacity_reservations
                               WHERE operation_id=?
                                 AND state NOT IN ('active','expired','released')""",
                            (operation_id,),
                        ).fetchone()[0]
                    )
                    locked_precheckpoint_trace = (
                        _empty_precheckpoint_trace(
                            Path(str(precheckpoint_trace["path"]))
                        )
                        if precheckpoint_trace is not None
                        else None
                    )
                    exact = (
                        row is not None
                        and str(row["lifecycle_state"])
                        == RecoveryState.FAILED_RECOVERABLE.value
                        and str(row["operation_kind"]) == "hourly_warehouse_sync"
                        and str(row["tier"]) == RecoveryTier.T2.value
                        and _json_object(row["target_scope_json"]) == scope
                        and str(scope["base_active_version_id"]) == active_version_id
                        and int(row["actual_bytes"] or 0) == 0
                        and int(row["read_bytes"] or 0) == 0
                        and not str(row["checkpoint_digest"] or "")
                        and not str(row["after_digest"] or "")
                        and str(row["next_action"])
                        == "resume_or_quarantine_domain_checkpoint"
                        and str(row["writer_state"]) == "failed"
                        and str(row["last_error"] or "").strip().lower()
                        in {"database is locked", "database table is locked"}
                        and failed_from is not None
                        and str(failed_from[0]) == RecoveryState.WRITING.value
                        and artifact_count == 0
                        and undo_count == 0
                        and unsafe_reservation_count == 0
                        and locked_precheckpoint_trace == precheckpoint_trace
                    )
                    if not exact:
                        raise RecoveryPolicyError(
                            "hourly T2 pre-checkpoint lock evidence drifted during reconciliation"
                        )
                    next_version = int(row["state_version"]) + 1
                    changed = conn.execute(
                        """UPDATE sheet_vitrina_v1_recovery_operations
                           SET lifecycle_state=?,state_version=?,next_action='none',
                               writer_state='idle',rollback_available=0,
                               updated_at=?,last_heartbeat_at=?
                           WHERE operation_id=? AND lifecycle_state=?
                             AND state_version=?""",
                        (
                            RecoveryState.RELEASED.value,
                            next_version,
                            now,
                            now,
                            operation_id,
                            RecoveryState.FAILED_RECOVERABLE.value,
                            int(row["state_version"]),
                        ),
                    )
                    if changed.rowcount != 1:
                        raise RecoveryPolicyError(
                            "hourly T2 pre-checkpoint reconciliation CAS update lost"
                        )
                    conn.execute(
                        """UPDATE sheet_vitrina_v1_recovery_capacity_reservations
                           SET state='released',released_at=?
                           WHERE operation_id=? AND state IN ('active','expired')""",
                        (now, operation_id),
                    )
                    conn.execute(
                        """INSERT INTO sheet_vitrina_v1_recovery_transitions(
                               operation_id,from_state,to_state,state_version,
                               transitioned_at,detail_json)
                           VALUES(?,?,?,?,?,?)""",
                        (
                            operation_id,
                            RecoveryState.FAILED_RECOVERABLE.value,
                            RecoveryState.RELEASED.value,
                            next_version,
                            now,
                            _json(
                                {
                                    "contract": (
                                        "hourly_t2_precheckpoint_lock_reconciliation_v1"
                                    ),
                                    "next_action": "none",
                                    "active_version_id": active_version_id,
                                    "failed_error_preserved": True,
                                    "artifacts_preserved": True,
                                    "empty_precheckpoint_trace_preserved": (
                                        precheckpoint_trace
                                    ),
                                    "business_mutation_reconciled": False,
                                }
                            ),
                        ),
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            self._fsync_registry()
            released.append(operation_id)
            if precheckpoint_trace is not None:
                preserved_precheckpoint_traces.append(
                    {
                        "operation_id": operation_id,
                        **precheckpoint_trace,
                    }
                )
        return {
            "contract": "hourly_t2_precheckpoint_lock_reconciliation_v1",
            "status": "released" if released else "noop",
            "active_version_id": active_version_id,
            "released_operation_ids": released,
            "rejected": rejected,
            "business_mutation_reconciled": False,
            "preserved_precheckpoint_traces": preserved_precheckpoint_traces,
            "removed_paths": [],
        }

    def scan_orphans(self) -> dict[str, Any]:
        """Classify complete artifact families without deleting anything."""

        policy_activation_at = ""
        policy_activation_epoch = 0.0
        registered: dict[str, dict[str, Any]] = {}
        if self.db_path.is_file():
            with _connect_readonly(self.db_path) as conn:
                if _table_exists(conn, "sheet_vitrina_v1_recovery_operations"):
                    row = conn.execute(
                        """
                        SELECT MIN(created_at)
                        FROM sheet_vitrina_v1_recovery_operations
                        """
                    ).fetchone()
                    policy_activation_at = str(row[0] if row else "")
                if _table_exists(conn, "sheet_vitrina_v1_recovery_artifacts"):
                    registered = {
                        str(item["path"]): dict(item)
                        for item in conn.execute(
                            """
                            SELECT *
                            FROM sheet_vitrina_v1_recovery_artifacts
                            WHERE state<>'released'
                            """
                        )
                        if item["path"]
                    }
        if policy_activation_at:
            try:
                policy_activation_epoch = datetime.fromisoformat(
                    policy_activation_at.replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                policy_activation_epoch = 0.0
        files: list[dict[str, Any]] = []
        unclassified: list[str] = []
        foreign_non_target: list[str] = []
        pre_policy_legacy: list[str] = []
        corrupt_registered: list[dict[str, Any]] = []
        backup_root = (self.runtime_dir / "backups").resolve()
        roots = [backup_root]
        if not _path_is_below(self.legacy_recovery_root, backup_root):
            roots.append(self.legacy_recovery_root)
        legacy_managed: set[str] = set()
        valid_lossless_archives: set[str] = set()
        if backup_root.is_dir():
            for manifest_path in backup_root.rglob(f"*{MANIFEST_SUFFIX}"):
                try:
                    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, Mapping):
                    continue
                archive_path = Path(str(payload.get("archive_path") or ""))
                if (
                    payload.get("contract_name") == "sqlite_backup_lossless_archive_v1"
                    and payload.get("lifecycle_state") == "retained"
                    and payload.get("source_removed") is True
                    and archive_path.is_file()
                    and archive_path.parent == manifest_path.parent
                    and manifest_path
                    == archive_path.with_name(archive_path.name + MANIFEST_SUFFIX)
                    and int(payload.get("archive_size_bytes") or -1)
                    == archive_path.stat().st_size
                ):
                    pair = {
                        str(manifest_path.resolve()),
                        str(archive_path.resolve()),
                    }
                    legacy_managed.update(pair)
                    valid_lossless_archives.update(pair)
        sanitation_verified = _sanitation_verified_archive_paths(
            runtime_dir=self.runtime_dir,
            backup_root=backup_root,
            valid_lossless_archives=valid_lossless_archives,
        )
        for root in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if path.is_symlink() or not path.is_file():
                    continue
                kind = _artifact_kind(path)
                resolved_path = str(path.resolve())
                path_stat = path.stat()
                is_registered = (
                    str(path) in registered or resolved_path in registered
                )
                is_pre_policy_legacy = (
                    kind != "foreign"
                    and root == backup_root
                    and not _path_is_below(path, self.recovery_root)
                    and policy_activation_epoch > 0
                    and path_stat.st_mtime <= policy_activation_epoch
                )
                is_legacy_manifest = (
                    resolved_path in legacy_managed
                    and (
                        policy_activation_epoch <= 0
                        or is_pre_policy_legacy
                    )
                )
                is_sanitation_verified = resolved_path in sanitation_verified
                is_managed = (
                    is_registered
                    or is_sanitation_verified
                    or is_legacy_manifest
                    or is_pre_policy_legacy
                )
                record = {
                    "path": str(path),
                    "kind": kind,
                    "registered": is_registered,
                    "managed": is_managed,
                    "classification": (
                        "registered"
                        if is_registered
                        else "sanitation_verified"
                        if is_sanitation_verified
                        else "legacy_manifest"
                        if is_legacy_manifest
                        else "pre_policy_legacy"
                        if is_pre_policy_legacy
                        else "foreign_non_target"
                        if kind == "foreign"
                        else "unclassified"
                    ),
                    "size_bytes": path_stat.st_size,
                }
                registered_artifact = registered.get(
                    str(path), registered.get(resolved_path)
                )
                if registered_artifact is not None:
                    expected_size = int(
                        registered_artifact.get("size_bytes") or 0
                    )
                    expected_digest = str(
                        registered_artifact.get("digest") or ""
                    )
                    digest_matches = (
                        not expected_digest
                        or _sha256_file(path) == expected_digest
                    )
                    size_matches = (
                        expected_size <= 0
                        or path.stat().st_size == expected_size
                    )
                    if not size_matches or not digest_matches:
                        corruption = {
                            "operation_id": str(
                                registered_artifact.get("operation_id") or ""
                            ),
                            "path": str(path),
                            "expected_size_bytes": expected_size,
                            "actual_size_bytes": path.stat().st_size,
                            "expected_digest": expected_digest,
                            "reason": "registered_artifact_identity_drift",
                        }
                        record["corruption"] = corruption
                        corrupt_registered.append(corruption)
                files.append(record)
                if kind == "foreign":
                    foreign_non_target.append(str(path))
                elif is_pre_policy_legacy:
                    pre_policy_legacy.append(str(path))
                elif not is_managed:
                    unclassified.append(str(path))
        registry_without_bytes = sorted(
            path
            for path in registered
            if path and not Path(path).is_file()
        )
        undo_without_registry = 0
        stuck: list[dict[str, Any]] = []
        expired_reservations: list[dict[str, Any]] = []
        if self.db_path.is_file():
            with _connect_readonly(self.db_path) as conn:
                if _table_exists(conn, "sheet_vitrina_v1_recovery_undo_rows"):
                    undo_without_registry = int(
                        conn.execute(
                            """
                            SELECT COUNT(*) FROM sheet_vitrina_v1_recovery_undo_rows undo
                            LEFT JOIN sheet_vitrina_v1_recovery_operations op
                              ON op.operation_id=undo.operation_id
                            WHERE op.operation_id IS NULL
                            """
                        ).fetchone()[0]
                    )
                if _table_exists(conn, "sheet_vitrina_v1_recovery_operations"):
                    stuck = [
                        dict(row)
                        for row in conn.execute(
                            """
                            SELECT operation_id,lifecycle_state,updated_at,next_action
                            FROM sheet_vitrina_v1_recovery_operations
                            WHERE lifecycle_state IN ('planned','reserved','writing','mutation_running')
                            ORDER BY updated_at,operation_id
                            """
                        )
                    ]
                if _table_exists(
                    conn, "sheet_vitrina_v1_recovery_capacity_reservations"
                ):
                    expired_reservations = [
                        dict(row)
                        for row in conn.execute(
                            """
                            SELECT reservation_id,operation_id,filesystem_id,
                                   required_bytes,operational_reserve_bytes,
                                   created_at,expires_at
                            FROM sheet_vitrina_v1_recovery_capacity_reservations
                            WHERE state='active' AND expires_at<?
                            ORDER BY expires_at,reservation_id
                            """,
                            (self._now(),),
                        )
                    ]
        status = (
            "clean"
            if not unclassified
            and not registry_without_bytes
            and not undo_without_registry
            and not stuck
            and not expired_reservations
            and not corrupt_registered
            else "attention_required"
        )
        return {
            "contract_name": CONTRACT_NAME,
            "status": status,
            "read_only": True,
            "files": files,
            "policy_activation_at": policy_activation_at,
            "pre_policy_legacy_paths": sorted(set(pre_policy_legacy)),
            "pre_policy_legacy_count": len(set(pre_policy_legacy)),
            "sanitation_verified_paths": sorted(sanitation_verified),
            "sanitation_verified_count": len(sanitation_verified),
            "unclassified_paths": sorted(set(unclassified)),
            "foreign_non_target_paths": sorted(set(foreign_non_target)),
            "registry_without_bytes": registry_without_bytes,
            "quarantine_candidates": corrupt_registered,
            "undo_without_registry_count": undo_without_registry,
            "stuck_operations": stuck,
            "expired_reservations": expired_reservations,
            "orphan_count": len(set(unclassified))
            + len(registry_without_bytes)
            + undo_without_registry
            + len(stuck)
            + len(expired_reservations)
            + len(corrupt_registered),
        }

    def capacity_status(self) -> dict[str, Any]:
        capacity_root = (
            self.checkpoint_root
            if self.checkpoint_root.exists()
            else self.recovery_root.parent
            if self.recovery_root.parent.exists()
            else self.runtime_dir
        )
        filesystem_id = _filesystem_id(capacity_root)
        runtime_filesystem_id = _filesystem_id(self.runtime_dir)
        free_bytes = shutil.disk_usage(capacity_root).free
        reserved = 0
        expired_reservation_count = 0
        if self.db_path.is_file():
            with _connect_readonly(self.db_path) as conn:
                if _table_exists(
                    conn, "sheet_vitrina_v1_recovery_capacity_reservations"
                ):
                    reserved = int(
                        conn.execute(
                            """
                            SELECT COALESCE(
                              SUM(required_bytes+operational_reserve_bytes),0
                            )
                            FROM sheet_vitrina_v1_recovery_capacity_reservations
                            WHERE filesystem_id=? AND state='active' AND expires_at>=?
                            """,
                            (filesystem_id, self._now()),
                        ).fetchone()[0]
                    )
                    expired_reservation_count = int(
                        conn.execute(
                            """
                            SELECT COUNT(*)
                            FROM sheet_vitrina_v1_recovery_capacity_reservations
                            WHERE filesystem_id=? AND state='active' AND expires_at<?
                            """,
                            (filesystem_id, self._now()),
                        ).fetchone()[0]
                    )
        available = max(0, free_bytes - reserved)
        t2_degraded = available < T2_DEGRADED_FREE_BYTES
        t2_hard_stop = available < T2_HARD_STOP_FREE_BYTES
        return {
            "filesystem_id": filesystem_id,
            "runtime_filesystem_id": runtime_filesystem_id,
            "routed_to_distinct_filesystem": (
                filesystem_id != runtime_filesystem_id
            ),
            "free_bytes": int(free_bytes),
            "reserved_bytes": reserved,
            "expired_reservation_count": expired_reservation_count,
            "operational_reserve_bytes": self.operational_reserve_bytes,
            "artifact_root": str(self.recovery_root),
            "legacy_artifact_root": str(self.legacy_recovery_root),
            "degraded_watermark_bytes": T2_DEGRADED_FREE_BYTES,
            "hard_stop_watermark_bytes": T2_HARD_STOP_FREE_BYTES,
            "available_after_reservations_bytes": available,
            # Keep the established generic capacity semantics for T1 and
            # callers that use a custom operational reserve. T2 receives
            # separate absolute watermarks because it is routed to the
            # backup filesystem and needs a stronger disk-full guard.
            "degraded": available < self.operational_reserve_bytes * 2,
            "hard_stop": available < self.operational_reserve_bytes,
            "t2_degraded": t2_degraded,
            "t2_hard_stop": t2_hard_stop,
        }

    def writer_state(self) -> dict[str, Any]:
        lock_path = self.runtime_dir / ".warehouse-functional-sync.lock"
        if not lock_path.is_file():
            return {
                "state": "idle",
                "lock_path": str(lock_path),
                "lock_exists": False,
            }
        handle = lock_path.open("r+", encoding="utf-8")
        held = False
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except BlockingIOError:
                held = True
        finally:
            handle.close()
        return {
            "state": "busy" if held else "idle",
            "lock_path": str(lock_path),
            "lock_exists": True,
        }

    def public_status(self, *, limit: int = 50) -> dict[str, Any]:
        schema_initialized = False
        if self.db_path.is_file():
            with _connect_readonly(self.db_path) as conn:
                schema_initialized = _table_exists(
                    conn, "sheet_vitrina_v1_recovery_operations"
                )
        operations = self.list_operations(limit=limit)
        orphan = self.scan_orphans()
        capacity = self.capacity_status()
        retention = self.plan_retention()
        has_failure = any(
            item.get("lifecycle")
            in {
                RecoveryState.FAILED_RECOVERABLE.value,
                RecoveryState.QUARANTINED.value,
            }
            for item in operations
        )
        return {
            "contract_name": CONTRACT_NAME,
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "registry_initialized": schema_initialized,
            "status": (
                "not_initialized"
                if not schema_initialized
                else "attention_required"
                if orphan["status"] != "clean" or has_failure
                else "ready"
            ),
            "operations": operations,
            "capacity": capacity,
            "retention": {
                "status": retention["status"],
                "fingerprint": retention["fingerprint"],
                "would_change": retention["would_change"],
                "candidate_count": retention["candidate_count"],
                "candidate_bytes": retention["candidate_bytes"],
                "retained_t2_count": retention["retained_t2_count"],
                "retained_t2_bytes": retention["retained_t2_bytes"],
                "protected_operation_ids": retention[
                    "protected_operation_ids"
                ],
                "policy": retention["policy"],
                "projection": retention["projection"],
            },
            "orphan_scanner": {
                "status": orphan["status"],
                "orphan_count": orphan["orphan_count"],
                "policy_activation_at": orphan["policy_activation_at"],
                "pre_policy_legacy_count": orphan[
                    "pre_policy_legacy_count"
                ],
                "pre_policy_legacy_paths": orphan[
                    "pre_policy_legacy_paths"
                ][:20],
                "sanitation_verified_count": orphan[
                    "sanitation_verified_count"
                ],
                "sanitation_verified_paths": orphan[
                    "sanitation_verified_paths"
                ][:20],
                "unclassified_paths": orphan["unclassified_paths"][:20],
                "registry_without_bytes": orphan["registry_without_bytes"][:20],
                "stuck_operations": orphan["stuck_operations"][:20],
                "expired_reservations": orphan["expired_reservations"][:20],
                "quarantine_candidates": orphan["quarantine_candidates"][:20],
                "foreign_non_target_paths": orphan[
                    "foreign_non_target_paths"
                ][:20],
            },
            "writer": self.writer_state(),
            "timer": self._timer_state(),
            "tiers": registered_policy_table(),
        }

    def list_operations(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if not self.db_path.is_file():
            return []
        with _connect_readonly(self.db_path) as conn:
            if not _table_exists(conn, "sheet_vitrina_v1_recovery_operations"):
                return []
            rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM sheet_vitrina_v1_recovery_operations
                    ORDER BY
                      CASE lifecycle_state
                        WHEN 'quarantined' THEN 0
                        WHEN 'failed_recoverable' THEN 1
                        WHEN 'mutation_running' THEN 2
                        WHEN 'writing' THEN 3
                        WHEN 'reserved' THEN 4
                        WHEN 'planned' THEN 5
                        ELSE 6
                      END,
                      updated_at DESC,operation_id
                    LIMIT ?
                    """,
                    (max(1, min(int(limit), 1000)),),
                )
            ]
            artifacts_by_operation: dict[str, list[dict[str, Any]]] = {}
            for artifact in conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_recovery_artifacts
                WHERE operation_id IN(
                    SELECT operation_id FROM sheet_vitrina_v1_recovery_operations
                    ORDER BY updated_at DESC LIMIT ?
                )
                ORDER BY operation_id,artifact_kind,artifact_id
                """,
                (max(1, min(int(limit), 1000)),),
            ):
                item = dict(artifact)
                item["metadata"] = _json_object(item.pop("metadata_json", "{}"))
                artifacts_by_operation.setdefault(
                    str(item["operation_id"]), []
                ).append(item)
            supersessions_by_operation: dict[str, dict[str, Any]] = {}
            if _table_exists(
                conn, "sheet_vitrina_v1_recovery_supersessions"
            ):
                for relation in conn.execute(
                    """
                    SELECT supersession_id,target_operation_id,
                           superseding_operation_id,proof_contract,
                           proof_fingerprint,actor,
                           authorization_reference,created_at
                    FROM sheet_vitrina_v1_recovery_supersessions
                    ORDER BY created_at,target_operation_id
                    """
                ):
                    item = dict(relation)
                    supersessions_by_operation[
                        str(item["target_operation_id"])
                    ] = item
        return [
            _public_operation(
                row,
                artifacts=artifacts_by_operation.get(str(row["operation_id"]), []),
                supersession=supersessions_by_operation.get(
                    str(row["operation_id"])
                ),
            )
            for row in rows
        ]

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        if not self.db_path.is_file():
            return None
        with _connect_readonly(self.db_path) as conn:
            if not _table_exists(conn, "sheet_vitrina_v1_recovery_operations"):
                return None
            row = conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_recovery_operations
                WHERE operation_id=?
                """,
                (str(operation_id),),
            ).fetchone()
            if row is None:
                return None
            artifacts = [
                {
                    **dict(item),
                    "metadata": _json_object(dict(item).get("metadata_json") or "{}"),
                }
                for item in conn.execute(
                    """
                    SELECT * FROM sheet_vitrina_v1_recovery_artifacts
                    WHERE operation_id=? ORDER BY artifact_kind,artifact_id
                    """,
                    (str(operation_id),),
                )
            ]
            supersession = None
            if _table_exists(
                conn, "sheet_vitrina_v1_recovery_supersessions"
            ):
                relation = conn.execute(
                    """
                    SELECT supersession_id,target_operation_id,
                           superseding_operation_id,proof_contract,
                           proof_fingerprint,actor,
                           authorization_reference,created_at
                    FROM sheet_vitrina_v1_recovery_supersessions
                    WHERE target_operation_id=?
                    """,
                    (str(operation_id),),
                ).fetchone()
                if relation is not None:
                    supersession = dict(relation)
        for artifact in artifacts:
            artifact.pop("metadata_json", None)
        return _public_operation(
            dict(row), artifacts=artifacts, supersession=supersession
        )

    def _create_operation(
        self,
        *,
        operation_id: str,
        selection: RecoverySelection,
        plan_fingerprint: str,
        scope: Mapping[str, Any],
        planned_bytes: int,
        source_digest: str,
        non_target_digest: str,
        rollback_expires_at: str,
    ) -> None:
        now = self._now()
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_recovery_operations(
                        operation_id,operation_kind,closure_kind,tier,target_scope_json,
                        plan_fingerprint,lifecycle_state,state_version,planned_bytes,
                        actual_bytes,read_bytes,source_digest,checkpoint_digest,
                        after_digest,non_target_digest,next_action,writer_state,timer_state,
                        rollback_available,rollback_expires_at,orphan_status,
                        quarantine_reason,last_error,created_at,updated_at,last_heartbeat_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        operation_id,
                        selection.mutation_kind,
                        selection.closure_kind,
                        selection.tier.value,
                        _json(scope),
                        str(plan_fingerprint),
                        RecoveryState.PLANNED.value,
                        1,
                        max(int(planned_bytes), 0),
                        0,
                        0,
                        str(source_digest or ""),
                        "",
                        "",
                        str(non_target_digest or ""),
                        "reserve_capacity",
                        "idle",
                        "",
                        1,
                        rollback_expires_at,
                        "classified",
                        "",
                        "",
                        now,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_recovery_transitions(
                        operation_id,from_state,to_state,state_version,transitioned_at,
                        detail_json
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        operation_id,
                        "",
                        RecoveryState.PLANNED.value,
                        1,
                        now,
                        _json({"reason": selection.reason}),
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        self._fsync_registry()
        self._inject(operation_id, f"after_transition:{RecoveryState.PLANNED.value}")

    def _resolve_operation_id(
        self,
        *,
        mutation_kind: str,
        plan_fingerprint: str,
    ) -> str:
        """Return the active deterministic generation for one exact plan.

        A successfully rolled-back or retention-released attempt is immutable
        audit evidence.  Reapplying the same still-current business plan gets a
        new deterministic generation instead of reviving a terminal lifecycle
        or overwriting its journal.
        """

        base = _operation_id(mutation_kind, plan_fingerprint)
        with _connect_readonly(self.db_path) as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT operation_id,lifecycle_state,created_at
                    FROM sheet_vitrina_v1_recovery_operations
                    WHERE operation_id=? OR operation_id LIKE ?
                    ORDER BY created_at,operation_id
                    """,
                    (base, base + "_g%"),
                )
            ]
        if not rows:
            return base
        latest = rows[-1]
        lifecycle = str(latest["lifecycle_state"])
        if lifecycle not in {
            RecoveryState.RELEASED.value,
            RecoveryState.ROLLED_BACK.value,
        }:
            return str(latest["operation_id"])
        generations = [
            1
            if str(item["operation_id"]) == base
            else int(str(item["operation_id"]).rsplit("_g", 1)[1])
            for item in rows
        ]
        return f"{base}_g{max(generations) + 1}"

    def _failed_from_state(self, operation_id: str) -> str:
        with _connect_readonly(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT from_state
                FROM sheet_vitrina_v1_recovery_transitions
                WHERE operation_id=? AND to_state=?
                ORDER BY transition_id DESC
                LIMIT 1
                """,
                (operation_id, RecoveryState.FAILED_RECOVERABLE.value),
            ).fetchone()
        return str(row[0] or "") if row is not None else ""

    def _reserve_capacity(
        self,
        *,
        operation_id: str,
        required_bytes: int,
        target_root: Path,
    ) -> dict[str, Any]:
        target_root = Path(target_root)
        target_root.mkdir(parents=True, exist_ok=True)
        self._expire_reservations()
        filesystem_id = _filesystem_id(target_root)
        free_bytes = shutil.disk_usage(target_root).free
        required = max(int(required_bytes), 0)
        is_t2_artifact = any(
            _path_is_below(target_root.resolve(), root)
            for root in self.recovery_roots
        )
        operational_reserve = (
            max(self.operational_reserve_bytes, T2_HARD_STOP_FREE_BYTES)
            if is_t2_artifact
            else self.operational_reserve_bytes
        )
        now = self.clock()
        expires_at = now + timedelta(seconds=DEFAULT_RESERVATION_TTL_SECONDS)
        reservation_id = f"rsv_{hashlib.sha256(operation_id.encode()).hexdigest()[:24]}"
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    """
                    SELECT * FROM sheet_vitrina_v1_recovery_capacity_reservations
                    WHERE operation_id=?
                    """,
                    (operation_id,),
                ).fetchone()
                if existing is not None:
                    existing_payload = dict(existing)
                    if str(existing_payload.get("state") or "") in {
                        "active",
                        "consumed",
                    }:
                        conn.commit()
                        return existing_payload
                reserved = int(
                    conn.execute(
                        """
                        SELECT COALESCE(SUM(required_bytes+operational_reserve_bytes),0)
                        FROM sheet_vitrina_v1_recovery_capacity_reservations
                        WHERE filesystem_id=? AND state='active'
                        """,
                        (filesystem_id,),
                    ).fetchone()[0]
                )
                available = free_bytes - reserved
                if available < required + operational_reserve:
                    raise RecoveryPolicyError(
                        "recovery capacity hard stop: "
                        f"required_bytes={required}, operational_reserve_bytes={operational_reserve}, "
                        f"available_after_reservations_bytes={max(available, 0)}"
                    )
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO sheet_vitrina_v1_recovery_capacity_reservations(
                            reservation_id,operation_id,filesystem_id,required_bytes,
                            operational_reserve_bytes,available_at_reservation_bytes,
                            state,created_at,expires_at,consumed_at,released_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            reservation_id,
                            operation_id,
                            filesystem_id,
                            required,
                            operational_reserve,
                            free_bytes,
                            "active",
                            _timestamp(now),
                            _timestamp(expires_at),
                            None,
                            None,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE sheet_vitrina_v1_recovery_capacity_reservations
                        SET filesystem_id=?,required_bytes=?,
                            operational_reserve_bytes=?,
                            available_at_reservation_bytes=?,state='active',
                            created_at=?,expires_at=?,consumed_at=NULL,released_at=NULL
                        WHERE operation_id=?
                        """,
                        (
                            filesystem_id,
                            required,
                            operational_reserve,
                            free_bytes,
                            _timestamp(now),
                            _timestamp(expires_at),
                            operation_id,
                        ),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        self._fsync_registry()
        self._inject(operation_id, "after_capacity_reservation")
        return {
            "reservation_id": reservation_id,
            "operation_id": operation_id,
            "filesystem_id": filesystem_id,
            "required_bytes": required,
            "operational_reserve_bytes": operational_reserve,
            "available_at_reservation_bytes": int(free_bytes),
            "state": "active",
            "created_at": _timestamp(now),
            "expires_at": _timestamp(expires_at),
        }

    def _mark_reservation_consumed(self, reservation_id: str) -> None:
        with _connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_recovery_capacity_reservations
                SET state='consumed',consumed_at=?
                WHERE reservation_id=? AND state='active'
                """,
                (self._now(), reservation_id),
            )
            conn.commit()

    def _assert_post_write_reserve(self, target_root: Path) -> None:
        free_bytes = int(shutil.disk_usage(Path(target_root)).free)
        required_reserve = (
            max(self.operational_reserve_bytes, T2_HARD_STOP_FREE_BYTES)
            if any(
                _path_is_below(Path(target_root).resolve(), root)
                for root in self.recovery_roots
            )
            else self.operational_reserve_bytes
        )
        if free_bytes < required_reserve:
            raise RecoveryPolicyError(
                "recovery post-write reserve breached: "
                f"free_bytes={free_bytes}, "
                f"required_reserve_bytes={required_reserve}"
            )

    def _expire_reservations(self) -> None:
        if not self.db_path.is_file():
            return
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_recovery_capacity_reservations
                SET state='expired',released_at=?
                WHERE state='active' AND expires_at<?
                """,
                (self._now(), self._now()),
            )
            conn.commit()

    def _transition(
        self,
        operation_id: str,
        *,
        expected_state: str,
        next_state: str,
        next_action: str,
        after_digest: str | None = None,
        quarantine_reason: str | None = None,
        last_error: str | None = None,
        writer_state: str | None = None,
        timer_state: str | None = None,
    ) -> None:
        allowed = ALLOWED_TRANSITIONS.get(expected_state, frozenset())
        if next_state not in allowed:
            raise RecoveryPolicyError(
                f"invalid recovery lifecycle transition: {expected_state}->{next_state}"
            )
        now = self._now()
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT state_version FROM sheet_vitrina_v1_recovery_operations
                    WHERE operation_id=? AND lifecycle_state=?
                    """,
                    (operation_id, expected_state),
                ).fetchone()
                if row is None:
                    current = conn.execute(
                        """
                        SELECT lifecycle_state FROM sheet_vitrina_v1_recovery_operations
                        WHERE operation_id=?
                        """,
                        (operation_id,),
                    ).fetchone()
                    raise RecoveryPolicyError(
                        "recovery lifecycle CAS failed: "
                        f"expected={expected_state}, actual={current[0] if current else '<missing>'}"
                    )
                next_version = int(row[0]) + 1
                cursor = conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_recovery_operations
                    SET lifecycle_state=?,state_version=?,next_action=?,
                        after_digest=COALESCE(?,after_digest),
                        quarantine_reason=COALESCE(?,quarantine_reason),
                        last_error=COALESCE(?,last_error),
                        writer_state=COALESCE(?,writer_state),
                        timer_state=COALESCE(?,timer_state),
                        rollback_available=?,updated_at=?,last_heartbeat_at=?
                    WHERE operation_id=? AND lifecycle_state=? AND state_version=?
                    """,
                    (
                        next_state,
                        next_version,
                        next_action,
                        after_digest,
                        quarantine_reason,
                        last_error,
                        writer_state,
                        timer_state,
                        0 if next_state in TERMINAL_STATES else 1,
                        now,
                        now,
                        operation_id,
                        expected_state,
                        int(row[0]),
                    ),
                )
                if cursor.rowcount != 1:
                    raise RecoveryPolicyError("recovery lifecycle CAS update lost")
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_recovery_transitions(
                        operation_id,from_state,to_state,state_version,transitioned_at,
                        detail_json
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        operation_id,
                        expected_state,
                        next_state,
                        next_version,
                        now,
                        _json(
                            {
                                "next_action": next_action,
                                "after_digest": after_digest,
                                "quarantine_reason": quarantine_reason,
                                "last_error": last_error,
                            }
                        ),
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        self._fsync_registry()
        self._inject(operation_id, f"after_transition:{next_state}")

    def _domain_inventory(self) -> tuple[list[str], int]:
        with _connect_readonly(self.db_path) as conn:
            names = [
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name NOT LIKE 'sqlite_%'
                    ORDER BY name
                    """
                )
                if _is_domain_table(str(row[0]))
            ]
            forbidden = sorted(set(names) & FINANCE_RAW_TABLES)
            if forbidden:
                raise RecoveryPolicyError(
                    f"Finance raw reached T2 inventory: {forbidden}"
                )
            index_names = [
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='index'
                      AND tbl_name IN ({})
                    ORDER BY name
                    """.format(",".join("?" for _ in names)),
                    tuple(names),
                )
            ]
            page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
            btree_names = [*names, *index_names]
            try:
                allocated = int(
                    conn.execute(
                        "SELECT COALESCE(SUM(pgsize),0) FROM dbstat WHERE name IN ({})".format(
                            ",".join("?" for _ in btree_names)
                        ),
                        tuple(btree_names),
                    ).fetchone()[0]
                )
            except sqlite3.OperationalError:
                allocated = 0
                for name in names:
                    cursor = conn.execute(f"SELECT * FROM {_quoted(name)}")
                    while True:
                        rows = cursor.fetchmany(500)
                        if not rows:
                            break
                        allocated += sum(
                            len(
                                _json_bytes(
                                    [
                                        _hashable_sqlite_value(value)
                                        for value in row
                                    ]
                                )
                            )
                            for row in rows
                        )
                allocated *= 2
            # The checkpoint adds sqlite_schema plus the recovery metadata
            # table and its primary-key autoindex b-trees.
            # Explicit index pages must be counted separately from their table
            # pages; dbstat reports them under the index name.
            planned = max(
                allocated + (3 * page_size),
                (len(btree_names) + 3) * page_size,
            )
        return names, max(planned, page_size)

    def _write_domain_checkpoint(
        self,
        *,
        temporary: Path,
        final: Path,
        table_names: Sequence[str],
        operation_id: str,
        plan_fingerprint: str,
        source_digest: str,
        source_watermarks: Mapping[str, Any],
        schema_revision: str,
    ) -> dict[str, Any]:
        if final.exists():
            digest = _sha256_file(final)
            return {
                "size_bytes": final.stat().st_size,
                "read_bytes": final.stat().st_size,
                "sha256": digest,
                "resumed": True,
            }
        temporary.unlink(missing_ok=True)
        read_bytes = 0
        with _connect_readonly(self.db_path) as source, closing(
            sqlite3.connect(temporary)
        ) as target:
            os.chmod(temporary, 0o600)
            target.execute("PRAGMA journal_mode=DELETE")
            target.execute("PRAGMA synchronous=FULL")
            target.execute(
                """
                CREATE TABLE recovery_checkpoint_metadata(
                    operation_id TEXT PRIMARY KEY,
                    contract_name TEXT NOT NULL,
                    plan_fingerprint TEXT NOT NULL,
                    source_digest TEXT NOT NULL,
                    source_watermarks_json TEXT NOT NULL,
                    schema_revision TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            target.execute(
                """
                INSERT INTO recovery_checkpoint_metadata
                VALUES(?,?,?,?,?,?,?)
                """,
                (
                    operation_id,
                    CONTRACT_NAME,
                    plan_fingerprint,
                    source_digest,
                    _json(source_watermarks),
                    schema_revision,
                    self._now(),
                ),
            )
            for table in table_names:
                if table in FINANCE_RAW_TABLES:
                    raise RecoveryPolicyError("Finance raw cannot enter a T2 checkpoint")
                _require_identifier(table)
                schema_row = source.execute(
                    """
                    SELECT sql FROM sqlite_master
                    WHERE type='table' AND name=?
                    """,
                    (table,),
                ).fetchone()
                if schema_row is None or not schema_row[0]:
                    raise RecoveryPolicyError(
                        f"domain checkpoint table schema is missing: {table}"
                    )
                target.execute(str(schema_row[0]))
                columns = [
                    str(row[1])
                    for row in source.execute(f"PRAGMA table_info({_quoted(table)})")
                ]
                if not columns:
                    continue
                placeholders = ",".join("?" for _ in columns)
                insert_sql = (
                    f"INSERT INTO {_quoted(table)}("
                    + ",".join(_quoted(column) for column in columns)
                    + f") VALUES({placeholders})"
                )
                cursor = source.execute(f"SELECT * FROM {_quoted(table)}")
                while True:
                    rows = cursor.fetchmany(500)
                    if not rows:
                        break
                    read_bytes += sum(
                        len(
                            _json_bytes(
                                [
                                    _hashable_sqlite_value(value)
                                    for value in row
                                ]
                            )
                        )
                        for row in rows
                    )
                    target.executemany(insert_sql, [tuple(row) for row in rows])
            for row in source.execute(
                """
                SELECT sql FROM sqlite_master
                WHERE type IN ('index','trigger') AND sql IS NOT NULL
                  AND tbl_name IN ({})
                ORDER BY CASE type WHEN 'index' THEN 0 ELSE 1 END,name
                """.format(",".join("?" for _ in table_names)),
                tuple(table_names),
            ):
                target.execute(str(row[0]))
            target.commit()
            integrity = str(target.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise RecoveryPolicyError(
                    f"domain checkpoint integrity check failed: {integrity}"
                )
        _fsync_file(temporary)
        os.replace(temporary, final)
        _fsync_directory(final.parent)
        return {
            "size_bytes": final.stat().st_size,
            "read_bytes": read_bytes,
            "sha256": _sha256_file(final),
            "resumed": False,
        }

    def _undo_rows(self, operation_id: str) -> list[dict[str, Any]]:
        with _connect_readonly(self.db_path) as conn:
            return [
                {
                    **dict(row),
                    "key": _json_object(row["key_json"]),
                    "before": _json_value(row["before_json"]),
                    "after": _json_value(row["after_json"]),
                }
                for row in conn.execute(
                    """
                    SELECT * FROM sheet_vitrina_v1_recovery_undo_rows
                    WHERE operation_id=? ORDER BY sequence_no
                    """,
                    (operation_id,),
                )
            ]

    def _verify_existing_identity(
        self,
        existing: Mapping[str, Any],
        *,
        selection: RecoverySelection,
        plan_fingerprint: str,
        scope: Mapping[str, Any],
    ) -> None:
        if (
            existing.get("tier") != selection.tier.value
            or existing.get("operation_kind") != selection.mutation_kind
            or existing.get("closure_kind") != selection.closure_kind
            or existing.get("plan_fingerprint") != plan_fingerprint
            or existing.get("scope") != _clone(scope)
        ):
            raise RecoveryPolicyError("recovery operation identity drifted on resume")

    def _timer_state(self) -> dict[str, Any]:
        if not self.db_path.is_file():
            return {"state": "unknown", "last_attempt_at": "", "last_success_at": ""}
        with _connect_readonly(self.db_path) as conn:
            if not _table_exists(
                conn, "sheet_vitrina_v1_warehouse_wb_sync_status"
            ):
                return {"state": "unknown", "last_attempt_at": "", "last_success_at": ""}
            row = conn.execute(
                """
                SELECT last_attempt_at,last_success_at,last_error,active_version_id,updated_at
                FROM sheet_vitrina_v1_warehouse_wb_sync_status WHERE slot=1
                """
            ).fetchone()
        if row is None:
            return {"state": "never", "last_attempt_at": "", "last_success_at": ""}
        return {
            "state": "failed" if row["last_error"] else "idle",
            "last_attempt_at": str(row["last_attempt_at"] or ""),
            "last_success_at": str(row["last_success_at"] or ""),
            "last_error": str(row["last_error"] or ""),
            "active_version_id": str(row["active_version_id"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    def _fsync_registry(self) -> None:
        if self.db_path.is_file():
            _fsync_file(self.db_path)
        wal = Path(str(self.db_path) + "-wal")
        if wal.is_file():
            _fsync_file(wal)
        _fsync_directory(self.db_path.parent)

    def _inject(self, operation_id: str, boundary: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(operation_id, boundary)

    def _now(self) -> str:
        return _timestamp(self.clock())


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_recovery_operations(
            operation_id TEXT PRIMARY KEY,
            operation_kind TEXT NOT NULL,
            closure_kind TEXT NOT NULL,
            tier TEXT NOT NULL CHECK(tier IN ('T1','T2','T3')),
            target_scope_json TEXT NOT NULL,
            plan_fingerprint TEXT NOT NULL,
            lifecycle_state TEXT NOT NULL,
            state_version INTEGER NOT NULL,
            planned_bytes INTEGER NOT NULL,
            actual_bytes INTEGER NOT NULL,
            read_bytes INTEGER NOT NULL,
            source_digest TEXT NOT NULL,
            checkpoint_digest TEXT NOT NULL,
            after_digest TEXT NOT NULL,
            non_target_digest TEXT NOT NULL,
            next_action TEXT NOT NULL,
            writer_state TEXT NOT NULL,
            timer_state TEXT NOT NULL,
            rollback_available INTEGER NOT NULL,
            rollback_expires_at TEXT,
            orphan_status TEXT NOT NULL,
            quarantine_reason TEXT NOT NULL,
            last_error TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_heartbeat_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_recovery_operations_by_plan
          ON sheet_vitrina_v1_recovery_operations(operation_kind,plan_fingerprint);
        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_recovery_operations_by_state
          ON sheet_vitrina_v1_recovery_operations(lifecycle_state,updated_at);
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_recovery_transitions(
            transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_id TEXT NOT NULL,
            from_state TEXT NOT NULL,
            to_state TEXT NOT NULL,
            state_version INTEGER NOT NULL,
            transitioned_at TEXT NOT NULL,
            detail_json TEXT NOT NULL,
            UNIQUE(operation_id,state_version),
            FOREIGN KEY(operation_id)
              REFERENCES sheet_vitrina_v1_recovery_operations(operation_id)
              ON DELETE RESTRICT
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_recovery_supersessions(
            supersession_id TEXT PRIMARY KEY,
            target_operation_id TEXT NOT NULL UNIQUE,
            superseding_operation_id TEXT NOT NULL,
            proof_contract TEXT NOT NULL,
            proof_fingerprint TEXT NOT NULL UNIQUE,
            proof_json TEXT NOT NULL CHECK(json_valid(proof_json)),
            actor TEXT NOT NULL,
            authorization_reference TEXT NOT NULL,
            created_at TEXT NOT NULL,
            CHECK(target_operation_id<>superseding_operation_id),
            FOREIGN KEY(target_operation_id)
              REFERENCES sheet_vitrina_v1_recovery_operations(operation_id)
              ON DELETE RESTRICT,
            FOREIGN KEY(superseding_operation_id)
              REFERENCES sheet_vitrina_v1_recovery_operations(operation_id)
              ON DELETE RESTRICT
        );
        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_recovery_supersessions_by_replacement
          ON sheet_vitrina_v1_recovery_supersessions(
            superseding_operation_id,created_at,target_operation_id
          );
        CREATE TRIGGER IF NOT EXISTS recovery_supersession_immutable
        BEFORE UPDATE ON sheet_vitrina_v1_recovery_supersessions
        BEGIN
          SELECT RAISE(ABORT,'recovery supersession proof is immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS recovery_supersession_append_only
        BEFORE DELETE ON sheet_vitrina_v1_recovery_supersessions
        BEGIN
          SELECT RAISE(ABORT,'recovery supersession proof is append-only');
        END;
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_recovery_capacity_reservations(
            reservation_id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL UNIQUE,
            filesystem_id TEXT NOT NULL,
            required_bytes INTEGER NOT NULL,
            operational_reserve_bytes INTEGER NOT NULL,
            available_at_reservation_bytes INTEGER NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            released_at TEXT,
            FOREIGN KEY(operation_id)
              REFERENCES sheet_vitrina_v1_recovery_operations(operation_id)
              ON DELETE RESTRICT
        );
        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_recovery_capacity_by_fs
          ON sheet_vitrina_v1_recovery_capacity_reservations(filesystem_id,state,expires_at);
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_recovery_artifacts(
            artifact_id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL,
            artifact_kind TEXT NOT NULL,
            path TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            digest TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            metadata_json TEXT NOT NULL,
            FOREIGN KEY(operation_id)
              REFERENCES sheet_vitrina_v1_recovery_operations(operation_id)
              ON DELETE RESTRICT
        );
        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_recovery_artifacts_by_operation
          ON sheet_vitrina_v1_recovery_artifacts(operation_id,artifact_kind,state);
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_recovery_retention_runs(
            retention_run_id TEXT PRIMARY KEY,
            plan_fingerprint TEXT NOT NULL UNIQUE,
            plan_json TEXT NOT NULL,
            status TEXT NOT NULL,
            removed_bytes INTEGER NOT NULL,
            removed_paths_json TEXT NOT NULL,
            error_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_recovery_undo_rows(
            operation_id TEXT NOT NULL,
            sequence_no INTEGER NOT NULL,
            table_name TEXT NOT NULL,
            key_json TEXT NOT NULL,
            before_json TEXT,
            after_json TEXT,
            action TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(operation_id,sequence_no),
            FOREIGN KEY(operation_id)
              REFERENCES sheet_vitrina_v1_recovery_operations(operation_id)
              ON DELETE RESTRICT
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_recovery_canary(
            marker_id TEXT PRIMARY KEY,
            marker_value TEXT NOT NULL,
            deployed_sha TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )


def _normalize_before_image(
    raw: Mapping[str, Any],
    *,
    sequence_no: int,
) -> dict[str, Any]:
    table = str(raw.get("table") or raw.get("table_name") or "").strip()
    _require_identifier(table)
    if table in FINANCE_RAW_TABLES:
        raise RecoveryPolicyError("T1 undo cannot contain Finance raw rows")
    key = raw.get("key")
    if not isinstance(key, Mapping) or not key:
        raise RecoveryPolicyError("T1 before image requires a non-empty exact key")
    for column in key:
        _require_identifier(str(column))
    before = raw.get("before")
    after = raw.get("after")
    if before is not None and not isinstance(before, Mapping):
        raise RecoveryPolicyError("T1 before row must be an object or null")
    if after is not None and not isinstance(after, Mapping):
        raise RecoveryPolicyError("T1 expected after row must be an object or null")
    action = (
        "delete_inserted"
        if before is None
        else "restore_deleted"
        if after is None
        else "restore_updated"
    )
    return {
        "sequence_no": sequence_no,
        "table": table,
        "key": _clone(key),
        "before": _clone(before) if before is not None else None,
        "after": _clone(after) if after is not None else None,
        "action": action,
    }


def _apply_undo_row(conn: sqlite3.Connection, item: Mapping[str, Any]) -> None:
    table = str(item["table_name"])
    _require_identifier(table)
    key = dict(_restore_sqlite_values(item["key"]))
    before = _restore_sqlite_values(item.get("before"))
    expected_after = _restore_sqlite_values(item.get("after"))
    where = " AND ".join(f"{_quoted(column)}=?" for column in key)
    current_row = conn.execute(
        f"SELECT * FROM {_quoted(table)} WHERE {where}",
        tuple(key.values()),
    ).fetchone()
    current = dict(current_row) if current_row is not None else None
    if expected_after is not None and current != expected_after:
        raise RecoveryPolicyError(
            f"T1 rollback current row differs from expected after-image: {table}/{key}"
        )
    if before is None:
        conn.execute(
            f"DELETE FROM {_quoted(table)} WHERE {where}",
            tuple(key.values()),
        )
        return
    columns = list(before)
    for column in columns:
        _require_identifier(column)
    placeholders = ",".join("?" for _ in columns)
    updates = ",".join(
        f"{_quoted(column)}=excluded.{_quoted(column)}" for column in columns
    )
    key_columns = list(key)
    conn.execute(
        f"INSERT INTO {_quoted(table)}("
        + ",".join(_quoted(column) for column in columns)
        + f") VALUES({placeholders}) ON CONFLICT("
        + ",".join(_quoted(column) for column in key_columns)
        + f") DO UPDATE SET {updates}",
        tuple(before[column] for column in columns),
    )


def _public_operation(
    row: Mapping[str, Any],
    *,
    artifacts: Sequence[Mapping[str, Any]],
    supersession: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    scope = _json_object(row.get("target_scope_json") or "{}")
    lifecycle = str(row.get("lifecycle_state") or "")
    return {
        "operation_id": str(row.get("operation_id") or ""),
        "operation_kind": str(row.get("operation_kind") or ""),
        "closure_kind": str(row.get("closure_kind") or ""),
        "tier": str(row.get("tier") or ""),
        "scope": scope,
        "plan_fingerprint": str(row.get("plan_fingerprint") or ""),
        "lifecycle": lifecycle,
        "state_version": int(row.get("state_version") or 0),
        "planned_bytes": int(row.get("planned_bytes") or 0),
        "actual_bytes": int(row.get("actual_bytes") or 0),
        "read_bytes": int(row.get("read_bytes") or 0),
        "source_digest": str(row.get("source_digest") or ""),
        "checkpoint_digest": str(row.get("checkpoint_digest") or ""),
        "after_digest": str(row.get("after_digest") or ""),
        "non_target_digest": str(row.get("non_target_digest") or ""),
        "next_action": str(row.get("next_action") or ""),
        "writer_state": str(row.get("writer_state") or ""),
        "timer_state": str(row.get("timer_state") or ""),
        "orphan_status": str(row.get("orphan_status") or ""),
        "quarantine_reason": str(row.get("quarantine_reason") or ""),
        "last_error": str(row.get("last_error") or ""),
        "last_heartbeat_at": str(row.get("last_heartbeat_at") or ""),
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
        "rollback": {
            "available": bool(row.get("rollback_available"))
            and lifecycle not in TERMINAL_STATES,
            "expires_at": row.get("rollback_expires_at"),
        },
        "artifacts": [_clone(item) for item in artifacts],
        "supersession": (
            {
                "supersession_id": str(
                    supersession.get("supersession_id") or ""
                ),
                "target_operation_id": str(
                    supersession.get("target_operation_id") or ""
                ),
                "superseding_operation_id": str(
                    supersession.get("superseding_operation_id") or ""
                ),
                "proof_contract": str(
                    supersession.get("proof_contract") or ""
                ),
                "proof_fingerprint": str(
                    supersession.get("proof_fingerprint") or ""
                ),
                "actor": str(supersession.get("actor") or ""),
                "authorization_reference": str(
                    supersession.get("authorization_reference") or ""
                ),
                "created_at": str(supersession.get("created_at") or ""),
                "artifacts_preserved": True,
            }
            if supersession is not None
            else None
        ),
    }


def _retention_candidate(
    operation: Mapping[str, Any],
    *,
    reasons: Sequence[str],
) -> dict[str, Any]:
    return {
        "operation_id": str(operation.get("operation_id") or ""),
        "operation_kind": str(operation.get("operation_kind") or ""),
        "tier": str(operation.get("tier") or ""),
        "lifecycle": str(operation.get("lifecycle") or ""),
        "state_version": int(operation.get("state_version") or 0),
        "created_at": str(operation.get("created_at") or ""),
        "rollback_expires_at": str(
            (operation.get("rollback") or {}).get("expires_at") or ""
        ),
        "actual_bytes": int(operation.get("actual_bytes") or 0),
        "reasons": sorted(set(str(reason) for reason in reasons)),
        "artifacts": [
            {
                "artifact_id": str(artifact.get("artifact_id") or ""),
                "artifact_kind": str(artifact.get("artifact_kind") or ""),
                "path": str(artifact.get("path") or ""),
                "size_bytes": int(artifact.get("size_bytes") or 0),
                "digest": str(artifact.get("digest") or ""),
                "state": str(artifact.get("state") or ""),
            }
            for artifact in operation.get("artifacts", [])
        ],
    }


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecoveryPolicyError(
            f"invalid recovery timestamp: {value!r}"
        ) from exc
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone.utc)
    )


def _operation_id(mutation_kind: str, plan_fingerprint: str) -> str:
    material = f"{str(mutation_kind).strip()}:{str(plan_fingerprint).strip()}"
    return "recovery_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _is_domain_table(name: str) -> bool:
    if name in FINANCE_RAW_TABLES:
        return False
    if name in DOMAIN_EXACT_TABLES:
        return True
    return name.startswith(DOMAIN_TABLE_PREFIXES)


def _artifact_kind(path: Path) -> str:
    name = path.name
    if name.endswith(MANIFEST_SUFFIX):
        return "manifest"
    if name.endswith(".sqlite3-wal") or name.endswith("-wal"):
        return "wal"
    if name.endswith(".sqlite3-shm") or name.endswith("-shm"):
        return "shm"
    if name.endswith(".sqlite3-journal") or name.endswith("-journal"):
        return "journal"
    if name.endswith(".zst"):
        return "zst"
    if name.endswith(TEMP_SUFFIX) or ".tmp." in name:
        return "temp"
    if name.endswith(".sqlite3"):
        return "raw"
    if name.endswith(".undo.json") or name.endswith(".journal.json"):
        return "undo"
    return "foreign"


def _filesystem_id(path: Path) -> str:
    stat = Path(path).stat()
    return f"dev:{int(stat.st_dev)}"


def _path_is_below(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def _sanitation_verified_archive_paths(
    *,
    runtime_dir: Path,
    backup_root: Path,
    valid_lossless_archives: set[str],
) -> set[str]:
    """Trust only exact archive pairs proven by a terminal sanitation audit."""

    audit_dir = (Path(runtime_dir).resolve() / SANITATION_AUDIT_DIRNAME)
    if audit_dir.is_symlink() or not audit_dir.is_dir():
        return set()
    managed: set[str] = set()
    for audit_path in sorted(audit_dir.glob("*.json")):
        if audit_path.is_symlink() or not audit_path.is_file():
            continue
        try:
            payload = json.loads(audit_path.read_text(encoding="utf-8"))
            fingerprint = str(payload.get("fingerprint") or "")
            if (
                payload.get("contract_name") != SANITATION_CONTRACT_NAME
                or payload.get("status") != "applied"
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint)
                or audit_path.name != fingerprint.removeprefix("sha256:") + ".json"
            ):
                continue
            plan = payload.get("plan")
            result = payload.get("result")
            if not isinstance(plan, Mapping) or not isinstance(result, Mapping):
                continue
            if (
                plan.get("action") != "archive_raw_sqlite"
                or plan.get("root") != "backup"
                or result.get("status") != "archived"
            ):
                continue
            archive = result.get("archive")
            if not isinstance(archive, Mapping):
                continue
            archive_path = Path(str(archive.get("archive_path") or "")).resolve()
            manifest_path = Path(
                str(archive.get("manifest_path") or "")
            ).resolve()
            family_path = Path(str(plan.get("family_path") or "")).resolve()
            if (
                family_path.parent != backup_root
                or family_path.name != str(plan.get("family") or "")
                or archive_path.parent != family_path
                or manifest_path.parent != family_path
                or manifest_path
                != archive_path.with_name(archive_path.name + MANIFEST_SUFFIX)
                or str(archive_path) not in valid_lossless_archives
                or str(manifest_path) not in valid_lossless_archives
                or archive_path.is_symlink()
                or manifest_path.is_symlink()
                or not archive_path.is_file()
                or not manifest_path.is_file()
            ):
                continue
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                manifest.get("contract_name")
                != "sqlite_backup_lossless_archive_v1"
                or manifest.get("lifecycle_state") != "retained"
                or manifest.get("source_removed") is not True
                or str(manifest.get("archive_path") or "")
                != str(archive_path)
                or int(manifest.get("archive_size_bytes") or -1)
                != int(archive.get("archive_size_bytes") or -2)
                or int(archive.get("archive_size_bytes") or -1)
                != archive_path.stat().st_size
                or str(manifest.get("archive_sha256") or "")
                != str(archive.get("archive_sha256") or "")
                or str(manifest.get("source_sha256") or "")
                != str(archive.get("source_sha256") or "")
                or str(manifest.get("actual_decompressed_sha256") or "")
                != str(archive.get("decompressed_sha256") or "")
                or int(manifest.get("actual_decompressed_size_bytes") or -1)
                != int(archive.get("decompressed_size_bytes") or -2)
                or archive.get("restore_probe") != "verified"
            ):
                continue
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        managed.update({str(archive_path), str(manifest_path)})
    return managed


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect_sqlite(db_path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        yield conn
    finally:
        conn.close()


def _empty_precheckpoint_trace(path: Path) -> dict[str, Any]:
    """Prove one preserved SQLite trace has no checkpoint metadata or data."""

    selected = Path(path)
    if (
        selected.is_symlink()
        or not selected.is_file()
        or not selected.name.endswith(f".sqlite3{TEMP_SUFFIX}")
    ):
        raise RecoveryPolicyError("pre-checkpoint trace is not an exact regular temp file")
    before = selected.stat()
    if before.st_size <= 0 or before.st_size > 64 * 1024:
        raise RecoveryPolicyError("pre-checkpoint trace size is outside the empty bound")
    connection = sqlite3.connect(
        f"file:{selected.resolve()}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        connection.execute("PRAGMA query_only=ON")
        if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise RecoveryPolicyError("pre-checkpoint trace query_only preflight failed")
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        indexes = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name"
            )
        ]
        columns = [
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(recovery_checkpoint_metadata)"
            )
        ]
        row_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM recovery_checkpoint_metadata"
            ).fetchone()[0]
        ) if tables == ["recovery_checkpoint_metadata"] else -1
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        freelist_count = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
        if (
            integrity != "ok"
            or tables != ["recovery_checkpoint_metadata"]
            or indexes != ["sqlite_autoindex_recovery_checkpoint_metadata_1"]
            or columns
            != [
                "operation_id",
                "contract_name",
                "plan_fingerprint",
                "source_digest",
                "source_watermarks_json",
                "schema_revision",
                "created_at",
            ]
            or row_count != 0
            or page_count != 3
            or freelist_count != 0
            or schema_version != 1
            or int(connection.total_changes) != 0
        ):
            raise RecoveryPolicyError("pre-checkpoint temp contains non-empty or unknown SQLite material")
    except sqlite3.Error as exc:
        raise RecoveryPolicyError("pre-checkpoint temp is not a readable empty SQLite trace") from exc
    finally:
        connection.close()
    file_sha256 = _sha256_file(selected)
    after = selected.stat()
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise RecoveryPolicyError("pre-checkpoint trace changed during query-only proof")
    return {
        "path": str(selected.resolve()),
        "size_bytes": int(after.st_size),
        "sha256": file_sha256,
        "device": int(after.st_dev),
        "inode": int(after.st_ino),
        "page_count": page_count,
        "schema_version": schema_version,
        "metadata_row_count": row_count,
        "query_only": True,
        "preserved": True,
    }


@contextmanager
def _connect_readonly(db_path: Path) -> Iterator[sqlite3.Connection]:
    path = Path(db_path).resolve()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise RecoveryPolicyError("SQLite query_only preflight failed")
        yield conn
    finally:
        conn.close()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _require_identifier(value: str) -> None:
    if not SAFE_IDENTIFIER.fullmatch(str(value)):
        raise RecoveryPolicyError(f"unsafe SQLite identifier: {value!r}")


def _quoted(value: str) -> str:
    _require_identifier(value)
    return '"' + value + '"'


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_bytes(value: Any) -> bytes:
    return _json(value).encode("utf-8")


def _hashable_sqlite_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {
            "blob_size_bytes": len(value),
            "blob_sha256": hashlib.sha256(value).hexdigest(),
        }
    return value


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    return json.loads(str(value))


def _json_object(value: Any) -> dict[str, Any]:
    parsed = _json_value(value)
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _json_array(value: Any) -> list[Any]:
    parsed = _json_value(value)
    if not isinstance(parsed, list):
        raise RecoveryPolicyError("stored recovery JSON must be an array")
    return parsed


def _clone(value: Any) -> Any:
    return json.loads(
        json.dumps(value, ensure_ascii=False, default=_json_clone_default)
    )


def _json_clone_default(value: Any) -> Any:
    if isinstance(value, bytes):
        return {
            SQLITE_BLOB_MARKER: base64.b64encode(value).decode("ascii"),
        }
    return str(value)


def _restore_sqlite_values(value: Any) -> Any:
    if isinstance(value, Mapping):
        if set(value) == {SQLITE_BLOB_MARKER}:
            try:
                return base64.b64decode(
                    str(value[SQLITE_BLOB_MARKER]),
                    validate=True,
                )
            except (ValueError, TypeError) as exc:
                raise RecoveryPolicyError(
                    "T1 undo contains an invalid SQLite BLOB before-image"
                ) from exc
        return {
            str(key): _restore_sqlite_values(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_restore_sqlite_values(item) for item in value]
    return value


def _timestamp(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _fsync_file(path: Path) -> None:
    descriptor = os.open(Path(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(Path(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{uuid4().hex}{TEMP_SUFFIX}"
    )
    descriptor = os.open(
        temporary,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(_json_bytes(payload))
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, destination)
    _fsync_directory(destination.parent)
