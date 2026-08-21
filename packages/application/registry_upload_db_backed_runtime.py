"""Application-слой DB-backed runtime для registry upload."""

from __future__ import annotations

from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from types import SimpleNamespace
from typing import Any, Iterable, Mapping

from packages.business_time import business_date_from_timestamp

from packages.application.cost_price_upload import CostPriceUploadBlock, parse_cost_price_upload_payload
from packages.application.ff_pool_documents import ensure_ff_pool_document_schema
from packages.application.ff_pool_foundation import ensure_ff_pool_foundation_schema
from packages.application.ff_pool_cutover import ensure_ff_pool_cutover_schema
from packages.application.ff_wb_supply_origins import ensure_ff_wb_supply_origin_schema
from packages.application.inventory_planning_read_model import ensure_inventory_planning_schema
from packages.application.wb_fbs_orders import ensure_wb_fbs_orders_schema
from packages.application.registry_upload_bundle_v1 import (
    RegistryUploadBundleV1Block,
    load_registry_upload_bundle_v1_from_path,
    parse_registry_upload_bundle_v1_payload,
)
from packages.application.supplier_shipment_status import apply_derived_supplier_status
from packages.application.sqlite_contention import connect_sqlite
from packages.application.storage_registry import StorageRegistryError, StoreRegistry
from packages.application.sheet_vitrina_v1 import parse_sheet_write_plan_payload
from packages.application.sheet_vitrina_v1_temporal_policy import (
    effective_source_temporal_policies,
    reduce_source_temporal_semantics,
)
from packages.contracts.cost_price_upload import (
    CostPriceCurrentState,
    CostPriceRow,
    CostPriceUploadAcceptedCounts,
    CostPriceUploadPayload,
    CostPriceUploadResult,
)
from packages.contracts.registry_upload_bundle_v1 import (
    ConfigV2Item,
    FormulaV2Item,
    MetricV2Item,
    RegistryUploadBundleV1,
)
from packages.contracts.registry_upload_db_backed_runtime import RegistryUploadDbBackedCurrentState
from packages.contracts.registry_upload_file_backed_service import (
    RegistryUploadAcceptedCounts,
    RegistryUploadResult,
)
from packages.contracts.sheet_vitrina_v1 import (
    SheetVitrinaV1AutoUpdateState,
    SheetVitrinaV1Envelope,
    SheetVitrinaV1ManualOperatorState,
    SheetVitrinaV1RefreshResult,
)
from packages.contracts.supplier_shipments import ORDER_STATUS_DEFAULT, TRADE_DOCUMENT_STATUS_ACTIVE
from packages.contracts.supplier_financial_documents import (
    FINANCIAL_DOCUMENT_PARSE_STATUS_PARSED,
    FINANCIAL_DOCUMENT_PARSE_STATUSES,
)
from packages.contracts.cny_ledger import (
    CNY_DOCUMENT_STATUS_POSTED,
    CNY_DOCUMENT_STATUSES,
    CNY_DOCUMENT_TYPES,
    CNY_LEDGER_OPERATION_STATUS_POSTED,
    CNY_LEDGER_OPERATION_STATUSES,
    CNY_LEDGER_OPERATION_TYPES,
)
from packages.contracts.wb_supply_planning_zones import CENTRAL_PLANNING_ZONE_KEYS

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS_DIR = ROOT / "artifacts" / "registry_upload_db_backed_runtime"
INPUT_BUNDLE_FIXTURE = ARTIFACTS_DIR / "input" / "registry_upload_bundle__fixture.json"
DB_FILENAME = "registry_upload_runtime.sqlite3"
SUPPLIER_CONFIRMATION_DB_FILENAME = "supplier_confirmation_runtime.sqlite3"
MIN_SQLITE_BUSY_TIMEOUT_MS = 5_000
DEFAULT_SQLITE_BUSY_TIMEOUT_MS = 30_000
MAX_SQLITE_BUSY_TIMEOUT_MS = 300_000
SUPPLY_CALCULATION_REGISTRY_MAX_COMPLETE_RECORDS = 200
SUPPLY_CALCULATION_REGISTRY_DEFAULT_PAGE_SIZE = 25
SUPPLY_CALCULATION_REGISTRY_MAX_PAGE_SIZE = 100
SUPPLY_CALCULATION_REGISTRY_MAX_PAYLOAD_BYTES = 32 * 1024 * 1024
SUPPLY_CALCULATION_REGISTRY_MAX_METADATA_BYTES = 8 * 1024 * 1024
SUPPLY_CALCULATION_REGISTRY_MAX_EXPORT_BYTES = 64 * 1024 * 1024
_SCHEMA_READY_KEYS: set[tuple[str, int, int, int]] = set()
_SCHEMA_READY_LOCK = threading.Lock()
_CONFIRMATION_SCHEMA_READY_KEYS: set[tuple[str, int, int, int]] = set()
_CONFIRMATION_SCHEMA_READY_LOCK = threading.Lock()
_SQLITE_BUSY_TIMEOUT_MS: ContextVar[int | None] = ContextVar(
    "registry_upload_sqlite_busy_timeout_ms",
    default=None,
)
_OPERATIONAL_STORE_REGISTRIES: dict[Path, StoreRegistry] = {}
_OPERATIONAL_STORE_REGISTRIES_LOCK = threading.Lock()


@contextmanager
def registry_runtime_sqlite_busy_timeout(timeout_ms: int | None):
    """Temporarily bound write contention for one runtime command process."""

    if timeout_ms is not None:
        timeout_ms = int(timeout_ms)
        if not MIN_SQLITE_BUSY_TIMEOUT_MS <= timeout_ms <= MAX_SQLITE_BUSY_TIMEOUT_MS:
            raise ValueError(
                "registry runtime SQLite busy timeout must be between "
                f"{MIN_SQLITE_BUSY_TIMEOUT_MS} and {MAX_SQLITE_BUSY_TIMEOUT_MS} ms"
            )
    token = _SQLITE_BUSY_TIMEOUT_MS.set(timeout_ms)
    try:
        yield
    finally:
        _SQLITE_BUSY_TIMEOUT_MS.reset(token)


@dataclass(frozen=True)
class TemporalSourceClosureState:
    source_key: str
    target_date: str
    slot_kind: str
    state: str
    attempt_count: int
    next_retry_at: str | None
    last_reason: str | None
    last_attempt_at: str | None
    last_success_at: str | None
    accepted_at: str | None


@dataclass(frozen=True)
class SheetVitrinaV1LoadState:
    loaded_at: str | None
    snapshot_id: str | None
    as_of_date: str | None
    refreshed_at: str | None
    plan_fingerprint: str | None
    result: dict[str, Any] | None


class RegistryUploadDbBackedRuntime:
    def __init__(
        self,
        runtime_dir: Path,
        bundle_block: RegistryUploadBundleV1Block | None = None,
        cost_price_block: CostPriceUploadBlock | None = None,
    ) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.store_registry = StoreRegistry(self.runtime_dir)
        self.db_path = self.store_registry.resolve("operational")
        with _OPERATIONAL_STORE_REGISTRIES_LOCK:
            _OPERATIONAL_STORE_REGISTRIES[self.db_path.resolve()] = self.store_registry
        self.bundle_block = bundle_block or RegistryUploadBundleV1Block()
        self.cost_price_block = cost_price_block or CostPriceUploadBlock()

    def backup_database(self, destination: Path) -> dict[str, Any]:
        """Create a coherent SQLite backup without copying a live WAL file set."""
        if not self.db_path.is_file():
            raise ValueError(f"Runtime SQLite database does not exist: {self.db_path}")
        target = Path(destination)
        if target.exists():
            raise ValueError(f"Backup destination already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        source_size = self.coherent_backup_size_bytes()
        safety_margin = max(256 * 1024 * 1024, source_size // 20)
        required_free_bytes = source_size + safety_margin
        available_free_bytes = shutil.disk_usage(target.parent).free
        if available_free_bytes < required_free_bytes:
            raise ValueError(
                "insufficient filesystem capacity for coherent SQLite backup: "
                f"required_free_bytes={required_free_bytes}, available_free_bytes={available_free_bytes}"
            )
        destination_created = False
        try:
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            destination_created = True
            with closing(sqlite3.connect(self.db_path)) as source_conn, closing(
                sqlite3.connect(target)
            ) as target_conn:
                source_conn.backup(target_conn)
                integrity_rows = target_conn.execute("PRAGMA integrity_check").fetchall()
                integrity_check = [str(row[0]) for row in integrity_rows]
                if integrity_check != ["ok"]:
                    raise ValueError(f"SQLite backup integrity_check failed: {integrity_check}")
            digest = hashlib.sha256()
            size_bytes = 0
            with target.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    size_bytes += len(chunk)
                    digest.update(chunk)
        except Exception:
            if destination_created:
                _remove_incomplete_backup_destination(target)
            raise
        return {
            "path": str(target),
            "size_bytes": size_bytes,
            "sha256": digest.hexdigest(),
            "integrity_check": "ok",
        }

    def coherent_backup_size_bytes(self) -> int:
        """Conservatively bound a coherent backup, including committed WAL pages."""

        main_size = self.db_path.stat().st_size
        with closing(
            sqlite3.connect(
                f"file:{self.db_path.resolve()}?mode=ro",
                uri=True,
            )
        ) as conn:
            page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
            page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        wal_path = Path(str(self.db_path) + "-wal")
        wal_size = wal_path.stat().st_size if wal_path.is_file() else 0
        return max(main_size, page_count * page_size, main_size + wal_size)

    def ingest_bundle_from_path(self, bundle_path: Path, activated_at: str) -> RegistryUploadResult:
        bundle = load_registry_upload_bundle_v1_from_path(bundle_path)
        return self.ingest_bundle(bundle, activated_at=activated_at)

    def ingest_bundle(
        self,
        bundle_input: RegistryUploadBundleV1 | Mapping[str, Any],
        activated_at: str,
    ) -> RegistryUploadResult:
        bundle = _coerce_bundle(bundle_input)
        errors = self._collect_validation_errors(bundle, activated_at)
        if errors:
            return _rejected_result(bundle.bundle_version, errors)

        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            if _bundle_version_exists(conn, bundle.bundle_version):
                return _rejected_result(
                    bundle.bundle_version,
                    [f"bundle_version already accepted in runtime DB: {bundle.bundle_version}"],
                )

            result = RegistryUploadResult(
                status="accepted",
                bundle_version=bundle.bundle_version,
                accepted_counts=_accepted_counts(bundle),
                validation_errors=[],
                activated_at=activated_at,
            )
            _persist_bundle(conn, bundle, result)
            conn.commit()
            return result

    def load_current_state(self) -> RegistryUploadDbBackedCurrentState:
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            current_row = conn.execute(
                """
                SELECT bundle_version, activated_at
                FROM registry_upload_current_state
                WHERE slot = 1
                """
            ).fetchone()
            if current_row is None:
                raise ValueError("runtime current state is not materialized")

            bundle_version = current_row["bundle_version"]
            return RegistryUploadDbBackedCurrentState(
                bundle_version=bundle_version,
                activated_at=current_row["activated_at"],
                config_v2=_load_config_items(conn, bundle_version),
                metrics_v2=_load_metric_items(conn, bundle_version),
                formulas_v2=_load_formula_items(conn, bundle_version),
            )

    def ingest_cost_price_payload(
        self,
        payload_input: CostPriceUploadPayload | Mapping[str, Any],
        activated_at: str,
    ) -> CostPriceUploadResult:
        payload = _coerce_cost_price_payload(payload_input)
        errors = self._collect_cost_price_validation_errors(payload, activated_at)
        if errors:
            return _rejected_cost_price_result(payload.dataset_version, errors)

        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            if _cost_price_dataset_version_exists(conn, payload.dataset_version):
                return _rejected_cost_price_result(
                    payload.dataset_version,
                    [f"dataset_version already accepted in runtime DB: {payload.dataset_version}"],
                )

            result = CostPriceUploadResult(
                status="accepted",
                dataset_version=payload.dataset_version,
                accepted_counts=CostPriceUploadAcceptedCounts(cost_price_rows=len(payload.cost_price_rows)),
                validation_errors=[],
                activated_at=activated_at,
            )
            _persist_cost_price_payload(conn, payload, result)
            conn.commit()
            return result

    def load_cost_price_current_state(self) -> CostPriceCurrentState:
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            current_row = conn.execute(
                """
                SELECT dataset_version, activated_at
                FROM cost_price_current_state
                WHERE slot = 1
                """
            ).fetchone()
            if current_row is None:
                raise ValueError("cost price current state is not materialized")

            dataset_version = current_row["dataset_version"]
            return CostPriceCurrentState(
                dataset_version=dataset_version,
                activated_at=current_row["activated_at"],
                cost_price_rows=_load_cost_price_rows(conn, dataset_version),
            )

    def load_persisted_cost_price_upload_result(self, dataset_version: str) -> CostPriceUploadResult:
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT status, row_count, validation_errors_json, activated_at
                FROM cost_price_upload_results
                WHERE dataset_version = ?
                """,
                (dataset_version,),
            ).fetchone()
            if row is None:
                raise ValueError(f"cost price upload result is not materialized for dataset_version: {dataset_version}")

            return CostPriceUploadResult(
                status=row["status"],
                dataset_version=dataset_version,
                accepted_counts=CostPriceUploadAcceptedCounts(cost_price_rows=row["row_count"]),
                validation_errors=json.loads(row["validation_errors_json"]),
                activated_at=row["activated_at"],
            )

    def list_cost_price_dataset_versions(self) -> list[str]:
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT dataset_version
                FROM cost_price_upload_versions
                ORDER BY activated_at, dataset_version
                """
            ).fetchall()
            return [row["dataset_version"] for row in rows]

    def save_sheet_vitrina_ready_snapshot(
        self,
        *,
        current_state: RegistryUploadDbBackedCurrentState,
        refreshed_at: str,
        plan: SheetVitrinaV1Envelope,
    ) -> SheetVitrinaV1RefreshResult:
        _validate_timestamp(refreshed_at, field_name="refreshed_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_ready_snapshots(
                    bundle_version,
                    activated_at,
                    as_of_date,
                    snapshot_id,
                    plan_version,
                    refreshed_at,
                    plan_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bundle_version, as_of_date) DO UPDATE SET
                    activated_at = excluded.activated_at,
                    snapshot_id = excluded.snapshot_id,
                    plan_version = excluded.plan_version,
                    refreshed_at = excluded.refreshed_at,
                    plan_json = excluded.plan_json
                """,
                (
                    current_state.bundle_version,
                    current_state.activated_at,
                    plan.as_of_date,
                    plan.snapshot_id,
                    plan.plan_version,
                    refreshed_at,
                    _serialize_sheet_vitrina_plan(plan),
                ),
            )
            conn.commit()

        semantic_summary = _derive_sheet_vitrina_refresh_semantic_summary(plan)
        effective_policies = effective_source_temporal_policies(plan.source_temporal_policies)
        return SheetVitrinaV1RefreshResult(
            status="success",
            bundle_version=current_state.bundle_version,
            activated_at=current_state.activated_at,
            refreshed_at=refreshed_at,
            as_of_date=plan.as_of_date,
            date_columns=plan.date_columns,
            temporal_slots=plan.temporal_slots,
            source_temporal_policies=effective_policies,
            snapshot_id=plan.snapshot_id,
            plan_version=plan.plan_version,
            sheet_row_counts=_sheet_row_counts_from_plan(plan),
            semantic_status=semantic_summary["status"],
            semantic_label=semantic_summary["label"],
            semantic_tone=semantic_summary["tone"],
            semantic_reason=semantic_summary["reason"],
            source_outcome_counts=dict(semantic_summary["counts"]),
            source_outcomes=list(semantic_summary["sources"]),
        )

    def apply_sheet_vitrina_incident_rematerialization(
        self,
        *,
        operation_id: str,
        plan_fingerprint: str,
        approval_reference: str,
        actor: str,
        applied_at: str,
        snapshots: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Atomically replace only reviewed derived incident cells.

        Each snapshot carries an exact semantic before/after digest plus compact
        target before/after images.  The audit images are sufficient to recover
        the bounded derived cells without copying the multi-gigabyte runtime
        store.  Source stock snapshots and unrelated ready-plan cells are never
        updated by this method.
        """

        normalized_operation_id = str(operation_id or "").strip()
        normalized_fingerprint = str(plan_fingerprint or "").strip()
        normalized_approval = str(approval_reference or "").strip()
        if not normalized_operation_id:
            raise ValueError("incident rematerialization operation_id is required")
        if not normalized_fingerprint.startswith("sha256:"):
            raise ValueError("incident rematerialization plan fingerprint is required")
        if not normalized_approval:
            raise ValueError("incident rematerialization approval_reference is required")
        _validate_timestamp(applied_at, field_name="applied_at")

        changed_snapshots = 0
        changed_cells = 0
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                for item in snapshots:
                    bundle_version = str(item.get("bundle_version") or "")
                    as_of_date = str(item.get("as_of_date") or "")
                    expected_snapshot_id = str(item.get("snapshot_id") or "")
                    before_digest = str(item.get("before_plan_digest") or "")
                    after_digest = str(item.get("after_plan_digest") or "")
                    after_plan = item.get("after_plan")
                    if not isinstance(after_plan, SheetVitrinaV1Envelope):
                        raise ValueError(
                            "incident rematerialization after_plan must be a SheetVitrinaV1Envelope"
                        )
                    row = conn.execute(
                        """
                        SELECT snapshot_id, plan_json
                        FROM sheet_vitrina_v1_ready_snapshots
                        WHERE bundle_version = ? AND as_of_date = ?
                        """,
                        (bundle_version, as_of_date),
                    ).fetchone()
                    if row is None:
                        raise ValueError(
                            "incident rematerialization ready snapshot disappeared: "
                            f"{bundle_version} {as_of_date}"
                        )
                    if str(row["snapshot_id"] or "") != expected_snapshot_id:
                        raise ValueError(
                            "incident rematerialization snapshot identity changed"
                        )
                    current_plan = _deserialize_sheet_vitrina_plan(row["plan_json"])
                    current_digest = _sheet_vitrina_plan_digest(current_plan)
                    if current_digest not in {before_digest, after_digest}:
                        raise ValueError(
                            "incident rematerialization ready snapshot changed after review"
                        )
                    existing_audit = conn.execute(
                        """
                        SELECT plan_fingerprint
                        FROM sheet_vitrina_v1_incident_rematerialization_audit
                        WHERE operation_id = ? AND bundle_version = ? AND as_of_date = ?
                        """,
                        (normalized_operation_id, bundle_version, as_of_date),
                    ).fetchone()
                    if existing_audit is not None and str(
                        existing_audit["plan_fingerprint"] or ""
                    ) != normalized_fingerprint:
                        raise ValueError(
                            "incident rematerialization operation identity conflict"
                        )
                    if current_digest == before_digest:
                        serialized_after = _serialize_sheet_vitrina_plan(after_plan)
                        conn.execute(
                            """
                            UPDATE sheet_vitrina_v1_ready_snapshots
                            SET plan_json = ?
                            WHERE bundle_version = ? AND as_of_date = ?
                            """,
                            (serialized_after, bundle_version, as_of_date),
                        )
                        changed_snapshots += 1
                        changed_cells += int(item.get("changed_cells") or 0)
                    conn.execute(
                        """
                        INSERT INTO sheet_vitrina_v1_incident_rematerialization_audit(
                            operation_id, bundle_version, as_of_date, snapshot_id,
                            plan_fingerprint, approval_reference, actor,
                            target_dates_json, before_plan_digest, after_plan_digest,
                            non_target_digest, changed_cells, before_manifest_json,
                            after_manifest_json, applied_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(operation_id, bundle_version, as_of_date) DO NOTHING
                        """,
                        (
                            normalized_operation_id,
                            bundle_version,
                            as_of_date,
                            expected_snapshot_id,
                            normalized_fingerprint,
                            normalized_approval,
                            str(actor or ""),
                            json.dumps(
                                list(item.get("target_dates") or []),
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            before_digest,
                            after_digest,
                            str(item.get("non_target_digest") or ""),
                            int(item.get("changed_cells") or 0),
                            json.dumps(
                                item.get("before_manifest") or {},
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            json.dumps(
                                item.get("after_manifest") or {},
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            applied_at,
                        ),
                    )
                    readback = conn.execute(
                        """
                        SELECT plan_json
                        FROM sheet_vitrina_v1_ready_snapshots
                        WHERE bundle_version = ? AND as_of_date = ?
                        """,
                        (bundle_version, as_of_date),
                    ).fetchone()
                    if readback is None or _sheet_vitrina_plan_digest(
                        _deserialize_sheet_vitrina_plan(readback["plan_json"])
                    ) != after_digest:
                        raise ValueError(
                            "incident rematerialization transactional readback mismatch"
                        )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            "status": "applied" if changed_snapshots else "no_op",
            "operation_id": normalized_operation_id,
            "plan_fingerprint": normalized_fingerprint,
            "changed_snapshots": changed_snapshots,
            "changed_cells": changed_cells,
            "audit_rows": len(snapshots),
            "applied_at": applied_at,
        }

    def load_sheet_vitrina_ready_snapshot(self, as_of_date: str | None = None) -> SheetVitrinaV1Envelope:
        current_state = self.load_current_state()
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            if as_of_date:
                row = conn.execute(
                    """
                    SELECT plan_json
                    FROM sheet_vitrina_v1_ready_snapshots
                    WHERE bundle_version = ? AND as_of_date = ?
                    """,
                    (current_state.bundle_version, as_of_date),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT plan_json
                    FROM sheet_vitrina_v1_ready_snapshots
                    WHERE bundle_version = ?
                    ORDER BY refreshed_at DESC, as_of_date DESC
                    LIMIT 1
                    """,
                    (current_state.bundle_version,),
                ).fetchone()
            if row is None:
                detail = (
                    f"bundle_version={current_state.bundle_version} as_of_date={as_of_date}"
                    if as_of_date
                    else f"bundle_version={current_state.bundle_version}"
                )
                raise ValueError(f"sheet_vitrina_v1 ready snapshot missing: {detail}")
            return _deserialize_sheet_vitrina_plan(row["plan_json"])

    def load_sheet_vitrina_ready_snapshot_any_bundle(self, *, as_of_date: str) -> SheetVitrinaV1Envelope:
        if not str(as_of_date or "").strip():
            raise ValueError("as_of_date is required for cross-bundle ready snapshot read")
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT plan_json
                FROM sheet_vitrina_v1_ready_snapshots
                WHERE as_of_date = ?
                ORDER BY activated_at DESC, refreshed_at DESC, bundle_version DESC
                LIMIT 1
                """,
                (as_of_date,),
            ).fetchone()
            if row is None:
                raise ValueError(f"sheet_vitrina_v1 ready snapshot missing: as_of_date={as_of_date}")
            return _deserialize_sheet_vitrina_plan(row["plan_json"])

    def load_sheet_vitrina_ready_snapshot_covering_date_any_bundle(
        self,
        *,
        column_date: str,
    ) -> SheetVitrinaV1Envelope:
        """Load the newest ready plan which contains an exact historical column.

        Period snapshots are keyed by their publication ``as_of_date`` while
        carrying several independently materialized business dates.  Historical
        consumers must select the requested column, not substitute the outer
        snapshot key or the latest current value.
        """

        date_key = str(column_date or "").strip()
        if not date_key:
            raise ValueError("column_date is required for cross-bundle ready snapshot read")
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT snapshot.plan_json
                FROM sheet_vitrina_v1_ready_snapshots AS snapshot
                WHERE EXISTS (
                    SELECT 1
                    FROM json_each(snapshot.plan_json, '$.date_columns') AS date_column
                    WHERE date_column.value = ?
                )
                ORDER BY snapshot.activated_at DESC, snapshot.refreshed_at DESC,
                         snapshot.as_of_date DESC, snapshot.bundle_version DESC
                LIMIT 1
                """,
                (date_key,),
            ).fetchone()
        if row is None:
            raise ValueError(
                f"sheet_vitrina_v1 ready snapshot column missing: column_date={date_key}"
            )
        return _deserialize_sheet_vitrina_plan(row["plan_json"])

    def list_sheet_vitrina_ready_snapshot_dates(
        self,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        descending: bool = False,
    ) -> list[str]:
        current_state = self.load_current_state()
        conditions = ["bundle_version = ?"]
        params: list[Any] = [current_state.bundle_version]
        if date_from:
            conditions.append("as_of_date >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("as_of_date <= ?")
            params.append(date_to)
        order = "DESC" if descending else "ASC"
        query = f"""
            SELECT as_of_date
            FROM sheet_vitrina_v1_ready_snapshots
            WHERE {" AND ".join(conditions)}
            ORDER BY as_of_date {order}
        """
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(query, tuple(params)).fetchall()
        return [str(row["as_of_date"]) for row in rows]

    def list_sheet_vitrina_ready_snapshot_dates_any_bundle(
        self,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        descending: bool = False,
    ) -> list[str]:
        conditions: list[str] = []
        params: list[Any] = []
        if date_from:
            conditions.append("as_of_date >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("as_of_date <= ?")
            params.append(date_to)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        order = "DESC" if descending else "ASC"
        query = f"""
            SELECT DISTINCT as_of_date
            FROM sheet_vitrina_v1_ready_snapshots
            {where}
            ORDER BY as_of_date {order}
        """
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(query, tuple(params)).fetchall()
        return [str(row["as_of_date"]) for row in rows]

    def load_our_wb_cost_daily_state(self, *, as_of_date: str) -> dict[int, dict[str, Any]]:
        date_key = str(as_of_date or "").strip()
        if not date_key:
            return {}
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            functional_cutover_table = conn.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type='table' AND name='sheet_vitrina_v1_warehouse_functional_cutovers'"""
            ).fetchone()
            posted_functional_cutover = (
                conn.execute(
                    """SELECT 1 FROM sheet_vitrina_v1_warehouse_functional_cutovers
                       WHERE cutover_id='warehouse_functional_cutover_v1' AND status='posted'"""
                ).fetchone()
                if functional_cutover_table is not None
                else None
            )
            if posted_functional_cutover is not None:
                # Every active consumer, including dates before the policy
                # boundary, must resolve through the warehouse-domain temporal
                # resolver.  The legacy branches below remain only for
                # pre-cutover fixtures/migrations.
                from packages.application.canonical_wb_cost_resolver import (
                    load_canonical_wb_cost_lookup,
                )

                return load_canonical_wb_cost_lookup(
                    conn,
                    as_of_date=date.fromisoformat(date_key[:10]),
                )
            functional_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sheet_vitrina_v1_warehouse_functional_balances'"
            ).fetchone()
            if functional_exists is not None:
                cutover = conn.execute(
                    """SELECT cutover_at FROM sheet_vitrina_v1_warehouse_functional_cutovers
                       WHERE cutover_id='warehouse_functional_cutover_v1' AND status='posted'"""
                ).fetchone()
                if cutover is not None and date_key >= "2026-07-01":
                    daily_rows = conn.execute(
                        """SELECT * FROM sheet_vitrina_v1_warehouse_wb_daily_cost
                           WHERE cutover_id='warehouse_functional_cutover_v1' AND as_of_date=?
                           ORDER BY nm_id""",
                        (date_key,),
                    ).fetchall()
                    if daily_rows:
                        result = {}
                        for row in daily_rows:
                            quantity = float(row["quantity"])
                            quality = str(row["quality"])
                            fallback = quantity if quality == "fallback_average" else 0.0
                            unit_cost = (
                                None
                                if quality == "zero_quantity_without_cost_basis"
                                else float(row["wac_rub"])
                            )
                            result[int(row["nm_id"])] = {
                                "as_of_date": date_key,
                                "nm_id": int(row["nm_id"]),
                                "stock_qty": quantity,
                                "cost_covered_qty": quantity,
                                "our_wb_unit_cost_rub": unit_cost,
                                "confirmed_qty": 0.0,
                                "estimated_qty": max(quantity - fallback, 0.0),
                                "fallback_qty": fallback,
                                "confirmed_share_pct": 0.0,
                                "source_status": quality,
                                "component_status_json": row["provenance_json"],
                                "calculated_at": row["created_at"],
                                "warehouse_cutover_id": str(row["cutover_id"]),
                            }
                        return result
                cutover_date = (
                    business_date_from_timestamp(str(cutover["cutover_at"]))
                    if cutover is not None
                    else ""
                )
                if cutover is not None and date_key >= cutover_date:
                    from packages.application.warehouse_business_projection import (
                        ensure_functional_version_business_time_schema,
                    )

                    ensure_functional_version_business_time_schema(conn)
                    version_candidates = conn.execute(
                        """SELECT version.*
                           FROM sheet_vitrina_v1_warehouse_functional_versions version
                           JOIN sheet_vitrina_v1_warehouse_wb_snapshots snapshot
                             ON snapshot.version_id=version.version_id
                           WHERE version.cutover_id='warehouse_functional_cutover_v1'
                             AND version.status='good' AND snapshot.snapshot_date=?
                           ORDER BY
                             COALESCE(NULLIF(version.published_at,''),version.created_at) DESC,
                             version.created_at DESC,version.version_id DESC""",
                        (date_key,),
                    ).fetchall()
                    version = next(
                        (
                            row
                            for row in version_candidates
                            if str(
                                row["business_effective_date"]
                                or date_key
                            ) <= date_key
                        ),
                        None,
                    )
                    if version is not None:
                        functional_rows = conn.execute(
                            """SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances
                               WHERE version_id=? AND warehouse_key='wb' ORDER BY nm_id""",
                            (version["version_id"],),
                        ).fetchall()
                        if functional_rows:
                            result = {}
                            for row in functional_rows:
                                quantity = float(row["quantity"])
                                covered = min(float(row["cost_covered_quantity"]), quantity)
                                certified = bool(row["certified"])
                                fallback = (
                                    covered if str(row["quality"]) == "fallback_average" else 0.0
                                )
                                confirmed = covered if certified else 0.0
                                estimated = max(covered - confirmed - fallback, 0.0)
                                result[int(row["nm_id"])] = {
                                    "as_of_date": date_key,
                                    "nm_id": int(row["nm_id"]),
                                    "stock_qty": quantity,
                                    "cost_covered_qty": covered,
                                    "our_wb_unit_cost_rub": (
                                        float(row["wac_rub"]) if row["wac_rub"] not in (None, "") else None
                                    ),
                                    "confirmed_qty": confirmed,
                                    "estimated_qty": estimated,
                                    "fallback_qty": fallback,
                                    "confirmed_share_pct": confirmed / quantity if quantity > 0 else None,
                                    "source_status": str(row["quality"]),
                                    "component_status_json": row["provenance_json"],
                                    "calculated_at": version["created_at"],
                                    "warehouse_version_id": str(version["version_id"]),
                                }
                            return result
                    return {}
                if cutover is not None and "2026-07-01" <= date_key < cutover_date:
                    return {}
            canonical_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sheet_vitrina_v1_canonical_cost_daily_state'"
            ).fetchone()
            if canonical_exists is not None and date_key >= "2026-07-01":
                canonical_rows = conn.execute(
                    """
                    SELECT * FROM sheet_vitrina_v1_canonical_cost_daily_state
                    WHERE as_of_date=? AND stage='WB'
                    """,
                    (date_key,),
                ).fetchall()
                if canonical_rows:
                    return {
                        int(row["nm_id"]): {
                            "as_of_date": str(row["as_of_date"]),
                            "nm_id": int(row["nm_id"]),
                            "stock_qty": float(row["physical_quantity"]),
                            "cost_covered_qty": float(row["cost_covered_quantity"]),
                            "our_wb_unit_cost_rub": (
                                float(row["recognized_unit_cost_rub"])
                                if row["recognized_unit_cost_rub"] is not None else None
                            ),
                            "confirmed_qty": float(row["confirmed_quantity"]),
                            "estimated_qty": max(float(row["cost_covered_quantity"]) - float(row["confirmed_quantity"]), 0.0),
                            "fallback_qty": float(row["cost_covered_quantity"]) if str(row["source_quality"]) == "legacy_1c_fallback" else 0.0,
                            "confirmed_share_pct": (
                                float(row["confirmed_quantity"]) / float(row["physical_quantity"])
                                if float(row["physical_quantity"]) > 0 else None
                            ),
                            "source_status": str(row["source_quality"]),
                            "component_status_json": row["diagnostics_json"],
                            "calculated_at": row["calculated_at"],
                        }
                        for row in canonical_rows
                    }
            rows = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_wb_cost_daily_state
                WHERE as_of_date = ?
                """,
                (date_key,),
            ).fetchall()
        return {
            int(row["nm_id"]): {
                "as_of_date": str(row["as_of_date"]),
                "nm_id": int(row["nm_id"]),
                "stock_qty": row["stock_qty"],
                "our_wb_unit_cost_rub": row["our_wb_unit_cost_rub"],
                "confirmed_qty": row["confirmed_qty"],
                "estimated_qty": row["estimated_qty"],
                "fallback_qty": row["fallback_qty"],
                "confirmed_share_pct": row["confirmed_share_pct"],
                "source_status": row["source_status"],
                "component_status_json": row["component_status_json"],
                "calculated_at": row["calculated_at"],
            }
            for row in rows
            if row["nm_id"] is not None
        }

    def load_sheet_vitrina_refresh_status(self, as_of_date: str | None = None) -> SheetVitrinaV1RefreshResult:
        current_state = self.load_current_state()
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            if as_of_date:
                row = conn.execute(
                    """
                    SELECT activated_at, as_of_date, snapshot_id, plan_version, refreshed_at, plan_json
                    FROM sheet_vitrina_v1_ready_snapshots
                    WHERE bundle_version = ? AND as_of_date = ?
                    """,
                    (current_state.bundle_version, as_of_date),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT activated_at, as_of_date, snapshot_id, plan_version, refreshed_at, plan_json
                    FROM sheet_vitrina_v1_ready_snapshots
                    WHERE bundle_version = ?
                    ORDER BY refreshed_at DESC, as_of_date DESC
                    LIMIT 1
                    """,
                    (current_state.bundle_version,),
                ).fetchone()
            if row is None:
                detail = (
                    f"bundle_version={current_state.bundle_version} as_of_date={as_of_date}"
                    if as_of_date
                    else f"bundle_version={current_state.bundle_version}"
                )
                raise ValueError(f"sheet_vitrina_v1 ready snapshot missing: {detail}")

            plan = _deserialize_sheet_vitrina_plan(row["plan_json"])
            semantic_summary = _derive_sheet_vitrina_refresh_semantic_summary(plan)
            effective_policies = effective_source_temporal_policies(plan.source_temporal_policies)
            return SheetVitrinaV1RefreshResult(
                status="success",
                bundle_version=current_state.bundle_version,
                activated_at=row["activated_at"],
                refreshed_at=row["refreshed_at"],
                as_of_date=row["as_of_date"],
                date_columns=plan.date_columns,
                temporal_slots=plan.temporal_slots,
                source_temporal_policies=effective_policies,
                snapshot_id=row["snapshot_id"],
                plan_version=row["plan_version"],
                sheet_row_counts=_sheet_row_counts_from_plan(plan),
                semantic_status=semantic_summary["status"],
                semantic_label=semantic_summary["label"],
                semantic_tone=semantic_summary["tone"],
                semantic_reason=semantic_summary["reason"],
                source_outcome_counts=dict(semantic_summary["counts"]),
                source_outcomes=list(semantic_summary["sources"]),
            )

    def load_sheet_vitrina_refresh_status_any_bundle(self, *, as_of_date: str) -> SheetVitrinaV1RefreshResult:
        if not str(as_of_date or "").strip():
            raise ValueError("as_of_date is required for cross-bundle ready snapshot status read")
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT bundle_version, activated_at, as_of_date, snapshot_id, plan_version, refreshed_at, plan_json
                FROM sheet_vitrina_v1_ready_snapshots
                WHERE as_of_date = ?
                ORDER BY activated_at DESC, refreshed_at DESC, bundle_version DESC
                LIMIT 1
                """,
                (as_of_date,),
            ).fetchone()
            if row is None:
                raise ValueError(f"sheet_vitrina_v1 ready snapshot missing: as_of_date={as_of_date}")

            plan = _deserialize_sheet_vitrina_plan(row["plan_json"])
            semantic_summary = _derive_sheet_vitrina_refresh_semantic_summary(plan)
            effective_policies = effective_source_temporal_policies(plan.source_temporal_policies)
            return SheetVitrinaV1RefreshResult(
                status="success",
                bundle_version=row["bundle_version"],
                activated_at=row["activated_at"],
                refreshed_at=row["refreshed_at"],
                as_of_date=row["as_of_date"],
                date_columns=plan.date_columns,
                temporal_slots=plan.temporal_slots,
                source_temporal_policies=effective_policies,
                snapshot_id=row["snapshot_id"],
                plan_version=row["plan_version"],
                sheet_row_counts=_sheet_row_counts_from_plan(plan),
                semantic_status=semantic_summary["status"],
                semantic_label=semantic_summary["label"],
                semantic_tone=semantic_summary["tone"],
                semantic_reason=semantic_summary["reason"],
                source_outcome_counts=dict(semantic_summary["counts"]),
                source_outcomes=list(semantic_summary["sources"]),
            )

    def mark_sheet_vitrina_auto_update_started(
        self,
        *,
        started_at: str,
        as_of_date: str | None,
    ) -> None:
        _validate_timestamp(started_at, field_name="started_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            previous = conn.execute(
                """
                SELECT last_successful_auto_update_at
                FROM sheet_vitrina_v1_auto_update_state
                WHERE slot = 1
                """
            ).fetchone()
            last_successful_auto_update_at = (
                previous["last_successful_auto_update_at"] if previous is not None else None
            )
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_auto_update_state(
                    slot,
                    last_run_started_at,
                    last_run_finished_at,
                    last_run_status,
                    last_run_error,
                    last_run_snapshot_id,
                    last_run_as_of_date,
                    last_run_refreshed_at,
                    last_run_result_json,
                    last_successful_auto_update_at
                )
                VALUES(1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(slot) DO UPDATE SET
                    last_run_started_at = excluded.last_run_started_at,
                    last_run_finished_at = excluded.last_run_finished_at,
                    last_run_status = excluded.last_run_status,
                    last_run_error = excluded.last_run_error,
                    last_run_snapshot_id = excluded.last_run_snapshot_id,
                    last_run_as_of_date = excluded.last_run_as_of_date,
                    last_run_refreshed_at = excluded.last_run_refreshed_at,
                    last_run_result_json = excluded.last_run_result_json,
                    last_successful_auto_update_at = excluded.last_successful_auto_update_at
                """,
                (
                    started_at,
                    None,
                    "running",
                    None,
                    None,
                    as_of_date,
                    None,
                    None,
                    last_successful_auto_update_at,
                ),
            )
            conn.commit()

    def save_sheet_vitrina_auto_update_result(
        self,
        *,
        started_at: str,
        finished_at: str,
        status: str,
        as_of_date: str | None,
        snapshot_id: str | None,
        refreshed_at: str | None,
        error: str | None,
        result_payload: Mapping[str, Any] | None = None,
    ) -> None:
        _validate_timestamp(started_at, field_name="started_at")
        _validate_timestamp(finished_at, field_name="finished_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            previous = conn.execute(
                """
                SELECT last_successful_auto_update_at
                FROM sheet_vitrina_v1_auto_update_state
                WHERE slot = 1
                """
            ).fetchone()
            last_successful_auto_update_at = (
                finished_at
                if status == "success"
                else (previous["last_successful_auto_update_at"] if previous is not None else None)
            )
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_auto_update_state(
                    slot,
                    last_run_started_at,
                    last_run_finished_at,
                    last_run_status,
                    last_run_error,
                    last_run_snapshot_id,
                    last_run_as_of_date,
                    last_run_refreshed_at,
                    last_run_result_json,
                    last_successful_auto_update_at
                )
                VALUES(1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(slot) DO UPDATE SET
                    last_run_started_at = excluded.last_run_started_at,
                    last_run_finished_at = excluded.last_run_finished_at,
                    last_run_status = excluded.last_run_status,
                    last_run_error = excluded.last_run_error,
                    last_run_snapshot_id = excluded.last_run_snapshot_id,
                    last_run_as_of_date = excluded.last_run_as_of_date,
                    last_run_refreshed_at = excluded.last_run_refreshed_at,
                    last_run_result_json = excluded.last_run_result_json,
                    last_successful_auto_update_at = excluded.last_successful_auto_update_at
                """,
                (
                    started_at,
                    finished_at,
                    status,
                    error,
                    snapshot_id,
                    as_of_date,
                    refreshed_at,
                    _serialize_optional_state_payload(result_payload),
                    last_successful_auto_update_at,
                ),
            )
            conn.commit()

    def load_sheet_vitrina_auto_update_state(self) -> SheetVitrinaV1AutoUpdateState:
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT
                    last_run_started_at,
                    last_run_finished_at,
                    last_run_status,
                    last_run_error,
                    last_run_snapshot_id,
                    last_run_as_of_date,
                    last_run_refreshed_at,
                    last_run_result_json,
                    last_successful_auto_update_at
                FROM sheet_vitrina_v1_auto_update_state
                WHERE slot = 1
                """
            ).fetchone()
            if row is None:
                return SheetVitrinaV1AutoUpdateState()
            return SheetVitrinaV1AutoUpdateState(
                last_run_started_at=row["last_run_started_at"],
                last_run_finished_at=row["last_run_finished_at"],
                last_run_status=row["last_run_status"],
                last_run_error=row["last_run_error"],
                last_run_snapshot_id=row["last_run_snapshot_id"],
                last_run_as_of_date=row["last_run_as_of_date"],
                last_run_refreshed_at=row["last_run_refreshed_at"],
                last_successful_auto_update_at=row["last_successful_auto_update_at"],
                last_run_result=_deserialize_optional_state_payload(row["last_run_result_json"]),
            )

    def save_sheet_vitrina_manual_refresh_result(
        self,
        *,
        result_payload: Mapping[str, Any] | None,
        refreshed_at: str | None = None,
    ) -> None:
        _validate_optional_timestamp(refreshed_at, field_name="refreshed_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            previous = conn.execute(
                """
                SELECT
                    last_successful_manual_refresh_at,
                    last_successful_manual_load_at
                FROM sheet_vitrina_v1_manual_operator_state
                WHERE slot = 1
                """
            ).fetchone()
            last_successful_manual_refresh_at = (
                refreshed_at
                if refreshed_at is not None
                else (previous["last_successful_manual_refresh_at"] if previous is not None else None)
            )
            last_successful_manual_load_at = (
                previous["last_successful_manual_load_at"] if previous is not None else None
            )
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_manual_operator_state(
                    slot,
                    last_successful_manual_refresh_at,
                    last_successful_manual_load_at,
                    last_manual_refresh_result_json
                )
                VALUES(1, ?, ?, ?)
                ON CONFLICT(slot) DO UPDATE SET
                    last_successful_manual_refresh_at = excluded.last_successful_manual_refresh_at,
                    last_successful_manual_load_at = excluded.last_successful_manual_load_at,
                    last_manual_refresh_result_json = excluded.last_manual_refresh_result_json
                """,
                (
                    last_successful_manual_refresh_at,
                    last_successful_manual_load_at,
                    _serialize_optional_state_payload(result_payload),
                ),
            )
            conn.commit()

    def save_sheet_vitrina_manual_load_result(
        self,
        *,
        result_payload: Mapping[str, Any] | None,
        loaded_at: str | None = None,
    ) -> None:
        _validate_optional_timestamp(loaded_at, field_name="loaded_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            previous = conn.execute(
                """
                SELECT
                    last_successful_manual_refresh_at,
                    last_successful_manual_load_at
                FROM sheet_vitrina_v1_manual_operator_state
                WHERE slot = 1
                """
            ).fetchone()
            last_successful_manual_refresh_at = (
                previous["last_successful_manual_refresh_at"] if previous is not None else None
            )
            last_successful_manual_load_at = (
                loaded_at
                if loaded_at is not None
                else (previous["last_successful_manual_load_at"] if previous is not None else None)
            )
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_manual_operator_state(
                    slot,
                    last_successful_manual_refresh_at,
                    last_successful_manual_load_at,
                    last_manual_load_result_json
                )
                VALUES(1, ?, ?, ?)
                ON CONFLICT(slot) DO UPDATE SET
                    last_successful_manual_refresh_at = excluded.last_successful_manual_refresh_at,
                    last_successful_manual_load_at = excluded.last_successful_manual_load_at,
                    last_manual_load_result_json = excluded.last_manual_load_result_json
                """,
                (
                    last_successful_manual_refresh_at,
                    last_successful_manual_load_at,
                    _serialize_optional_state_payload(result_payload),
                ),
            )
            conn.commit()

    def load_sheet_vitrina_manual_operator_state(self) -> SheetVitrinaV1ManualOperatorState:
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT
                    last_successful_manual_refresh_at,
                    last_successful_manual_load_at,
                    last_manual_refresh_result_json,
                    last_manual_load_result_json
                FROM sheet_vitrina_v1_manual_operator_state
                WHERE slot = 1
                """
            ).fetchone()
            if row is None:
                return SheetVitrinaV1ManualOperatorState()
            return SheetVitrinaV1ManualOperatorState(
                last_successful_manual_refresh_at=row["last_successful_manual_refresh_at"],
                last_successful_manual_load_at=row["last_successful_manual_load_at"],
                last_manual_refresh_result=_deserialize_optional_state_payload(row["last_manual_refresh_result_json"]),
                last_manual_load_result=_deserialize_optional_state_payload(row["last_manual_load_result_json"]),
            )

    def load_sheet_vitrina_user_config(
        self,
        *,
        user_key: str,
        config_key: str,
    ) -> dict[str, Any]:
        normalized_user_key = _normalize_required_storage_key(user_key, field_name="user_key")
        normalized_config_key = _normalize_required_storage_key(config_key, field_name="config_key")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT user_key, config_key, schema_version, payload_json, updated_at, revision
                FROM sheet_vitrina_v1_user_configs
                WHERE user_key = ? AND config_key = ?
                """,
                (normalized_user_key, normalized_config_key),
            ).fetchone()
            if row is None:
                return {
                    "status": "missing",
                    "user_key": normalized_user_key,
                    "config_key": normalized_config_key,
                    "schema_version": 0,
                    "revision": 0,
                    "updated_at": "",
                    "config": None,
                }
            return _sheet_vitrina_user_config_row_to_dict(row)

    def save_sheet_vitrina_user_config(
        self,
        *,
        user_key: str,
        config_key: str,
        schema_version: int,
        payload: Mapping[str, Any],
        updated_at: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        normalized_user_key = _normalize_required_storage_key(user_key, field_name="user_key")
        normalized_config_key = _normalize_required_storage_key(config_key, field_name="config_key")
        normalized_schema_version = int(schema_version)
        if normalized_schema_version < 1:
            raise ValueError("schema_version must be a positive integer")
        _validate_timestamp(updated_at, field_name="updated_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            current = conn.execute(
                """
                SELECT user_key, config_key, schema_version, payload_json, updated_at, revision
                FROM sheet_vitrina_v1_user_configs
                WHERE user_key = ? AND config_key = ?
                """,
                (normalized_user_key, normalized_config_key),
            ).fetchone()
            current_revision = int(current["revision"]) if current is not None else 0
            if expected_revision is not None and current_revision != int(expected_revision):
                return {
                    "status": "conflict",
                    "expected_revision": int(expected_revision),
                    "current": (
                        _sheet_vitrina_user_config_row_to_dict(current)
                        if current is not None
                        else {
                            "status": "missing",
                            "user_key": normalized_user_key,
                            "config_key": normalized_config_key,
                            "schema_version": 0,
                            "revision": 0,
                            "updated_at": "",
                            "config": None,
                        }
                    ),
                }
            next_revision = current_revision + 1
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_user_configs(
                    user_key,
                    config_key,
                    schema_version,
                    payload_json,
                    updated_at,
                    revision
                )
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_key, config_key) DO UPDATE SET
                    schema_version = excluded.schema_version,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at,
                    revision = excluded.revision
                """,
                (
                    normalized_user_key,
                    normalized_config_key,
                    normalized_schema_version,
                    json.dumps(dict(payload), ensure_ascii=False, sort_keys=True),
                    updated_at,
                    next_revision,
                ),
            )
            conn.commit()
        return self.load_sheet_vitrina_user_config(
            user_key=normalized_user_key,
            config_key=normalized_config_key,
        )

    def list_sheet_vitrina_user_configs(self, *, config_key: str) -> list[dict[str, Any]]:
        """Read every legacy per-user value for one config key without mutating it."""

        normalized_config_key = _normalize_required_storage_key(config_key, field_name="config_key")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT user_key, config_key, schema_version, payload_json, updated_at, revision
                FROM sheet_vitrina_v1_user_configs
                WHERE config_key = ?
                ORDER BY user_key
                """,
                (normalized_config_key,),
            ).fetchall()
        return [_sheet_vitrina_user_config_row_to_dict(row) for row in rows]

    def load_latest_wb_incident_policy(self, *, seller_id: str) -> dict[str, Any]:
        normalized_seller_id = _normalize_required_storage_key(seller_id, field_name="seller_id")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_wb_incident_policy_revisions
                WHERE seller_id = ?
                ORDER BY revision DESC
                LIMIT 1
                """,
                (normalized_seller_id,),
            ).fetchone()
        return _wb_incident_policy_row_to_dict(row, seller_id=normalized_seller_id)

    def list_wb_incident_policy_revisions(
        self,
        *,
        seller_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Read bounded append-only policy history for the explicit legacy UI."""

        normalized_seller_id = _normalize_required_storage_key(seller_id, field_name="seller_id")
        bounded_limit = max(1, min(int(limit), 500))
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_wb_incident_policy_revisions
                WHERE seller_id = ?
                ORDER BY revision DESC
                LIMIT ?
                """,
                (normalized_seller_id, bounded_limit),
            ).fetchall()
        return [
            _wb_incident_policy_row_to_dict(row, seller_id=normalized_seller_id)
            for row in rows
        ]

    def load_wb_incident_policy_for_date(
        self,
        *,
        seller_id: str,
        snapshot_date: str,
    ) -> dict[str, Any]:
        normalized_seller_id = _normalize_required_storage_key(seller_id, field_name="seller_id")
        normalized_snapshot_date = date.fromisoformat(str(snapshot_date)).isoformat()
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_wb_incident_policy_revisions
                WHERE seller_id = ?
                  AND effective_from <= ?
                ORDER BY revision DESC
                LIMIT 1
                """,
                (normalized_seller_id, normalized_snapshot_date),
            ).fetchone()
        return _wb_incident_policy_row_to_dict(row, seller_id=normalized_seller_id)

    def load_wb_incident_policy_started_by_date(
        self,
        *,
        seller_id: str,
        snapshot_date: str,
    ) -> dict[str, Any]:
        """Return the latest revision that had started, regardless of its end date."""

        normalized_seller_id = _normalize_required_storage_key(seller_id, field_name="seller_id")
        normalized_snapshot_date = date.fromisoformat(str(snapshot_date)).isoformat()
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_wb_incident_policy_revisions
                WHERE seller_id = ? AND effective_from <= ?
                ORDER BY revision DESC
                LIMIT 1
                """,
                (normalized_seller_id, normalized_snapshot_date),
            ).fetchone()
        return _wb_incident_policy_row_to_dict(row, seller_id=normalized_seller_id)

    def append_wb_incident_policy_revision(
        self,
        *,
        seller_id: str,
        active: bool,
        warehouse_ids: Iterable[int],
        warehouse_identities: Iterable[Mapping[str, Any]],
        warehouse_entries: Iterable[Mapping[str, Any]] = (),
        reason: str,
        effective_from: str,
        effective_to: str,
        policy_status: str,
        actor: str,
        created_at: str,
        source: str,
        legacy_payloads: Iterable[Mapping[str, Any]] = (),
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Append an immutable seller-level policy revision with optimistic locking."""

        normalized_seller_id = _normalize_required_storage_key(seller_id, field_name="seller_id")
        normalized_from = date.fromisoformat(str(effective_from)).isoformat()
        normalized_to = date.fromisoformat(str(effective_to)).isoformat() if effective_to else ""
        _validate_timestamp(created_at, field_name="created_at")
        normalized_ids = sorted({int(item) for item in warehouse_ids})
        identities = [dict(item) for item in warehouse_identities]
        entries = [dict(item) for item in warehouse_entries]
        legacy = [dict(item) for item in legacy_payloads]
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                """
                SELECT revision
                FROM sheet_vitrina_v1_wb_incident_policy_revisions
                WHERE seller_id = ?
                ORDER BY revision DESC
                LIMIT 1
                """,
                (normalized_seller_id,),
            ).fetchone()
            current_revision = int(current["revision"]) if current is not None else 0
            if expected_revision is not None and current_revision != int(expected_revision):
                return {"status": "conflict", "current_revision": current_revision}
            next_revision = current_revision + 1
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_wb_incident_policy_revisions(
                    seller_id,
                    revision,
                    active,
                    warehouse_ids_json,
                    warehouse_identities_json,
                    warehouse_entries_json,
                    reason,
                    effective_from,
                    effective_to,
                    policy_status,
                    actor,
                    created_at,
                    source,
                    legacy_payloads_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_seller_id,
                    next_revision,
                    1 if active else 0,
                    json.dumps(normalized_ids, ensure_ascii=False, sort_keys=True),
                    json.dumps(identities, ensure_ascii=False, sort_keys=True),
                    json.dumps(entries, ensure_ascii=False, sort_keys=True),
                    str(reason or ""),
                    normalized_from,
                    normalized_to,
                    str(policy_status or ""),
                    str(actor or ""),
                    created_at,
                    str(source or "incident_policy"),
                    json.dumps(legacy, ensure_ascii=False, sort_keys=True),
                ),
            )
            conn.commit()
        return self.load_latest_wb_incident_policy(seller_id=normalized_seller_id)

    def load_wb_incident_projection_cache(
        self,
        *,
        seller_id: str,
        snapshot_digest: str,
        policy_revision: int,
        snapshot_date: str,
    ) -> dict[str, Any]:
        normalized_seller_id = _normalize_required_storage_key(seller_id, field_name="seller_id")
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT projection_json, created_at
                FROM sheet_vitrina_v1_wb_incident_projection_cache
                WHERE seller_id = ?
                  AND snapshot_digest = ?
                  AND policy_revision = ?
                  AND snapshot_date = ?
                """,
                (
                    normalized_seller_id,
                    str(snapshot_digest or ""),
                    int(policy_revision),
                    date.fromisoformat(str(snapshot_date)).isoformat(),
                ),
            ).fetchone()
        if row is None:
            return {"status": "missing"}
        return {
            "status": "ok",
            "projection": json.loads(row["projection_json"]),
            "created_at": str(row["created_at"] or ""),
        }

    def save_wb_incident_projection_cache(
        self,
        *,
        seller_id: str,
        snapshot_digest: str,
        policy_revision: int,
        snapshot_date: str,
        projection: Mapping[str, Any],
        created_at: str,
    ) -> dict[str, Any]:
        normalized_seller_id = _normalize_required_storage_key(seller_id, field_name="seller_id")
        _validate_timestamp(created_at, field_name="created_at")
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_wb_incident_projection_cache(
                    seller_id,
                    snapshot_digest,
                    policy_revision,
                    snapshot_date,
                    projection_json,
                    created_at
                )
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(seller_id, snapshot_digest, policy_revision, snapshot_date) DO NOTHING
                """,
                (
                    normalized_seller_id,
                    str(snapshot_digest or ""),
                    int(policy_revision),
                    date.fromisoformat(str(snapshot_date)).isoformat(),
                    json.dumps(dict(projection), ensure_ascii=False, sort_keys=True),
                    created_at,
                ),
            )
            conn.commit()
        return {"status": "ok"}

    def create_sku_action_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """Persist one immutable SKU impact attempt/readback event in runtime SQLite."""

        event_id = _normalize_required_storage_key(event.get("event_id"), field_name="event_id")
        nm_id = int(event.get("nm_id") or 0)
        if nm_id <= 0:
            raise ValueError("sku action event nm_id must be positive")
        parameter = str(event.get("parameter") or "").strip()
        if parameter not in {"seller_price", "advertising_bid"}:
            raise ValueError("unsupported sku action event parameter")
        requested_at = str(event.get("requested_at") or "").strip()
        if requested_at:
            _validate_timestamp(requested_at, field_name="requested_at")
        confirmed_at = str(event.get("confirmed_at") or "").strip()
        if confirmed_at:
            _validate_timestamp(confirmed_at, field_name="confirmed_at")
        commit_status = str(event.get("commit_status") or "error").strip()
        if commit_status not in {"confirmed", "error"}:
            raise ValueError("unsupported sku action event commit_status")
        advert_id = event.get("advert_id")
        placement = str(event.get("placement") or "").strip()
        if parameter == "advertising_bid":
            if not isinstance(advert_id, int) or isinstance(advert_id, bool) or advert_id <= 0:
                raise ValueError("advertising bid event requires positive advert_id")
            if placement not in {"combined", "search", "recommendations"}:
                raise ValueError("advertising bid event requires exact supported placement")
        if commit_status == "confirmed":
            if not confirmed_at:
                raise ValueError("confirmed sku action event requires confirmed_at")
            if event.get("confirmed_value") is None or event.get("delta") is None:
                raise ValueError("confirmed sku action event requires confirmed_value and delta")
        readback_status = str(
            event.get("readback_status") or ("matching" if commit_status == "confirmed" else "error")
        ).strip()
        if readback_status not in {"matching", "mismatch", "error", "not_started"}:
            raise ValueError("unsupported sku action event readback_status")
        payload = {
            "event_id": event_id,
            "nm_id": nm_id,
            "parameter": parameter,
            "old_value": event.get("old_value"),
            "requested_value": event.get("requested_value"),
            "confirmed_value": event.get("confirmed_value"),
            "delta": event.get("delta"),
            "requested_at": requested_at,
            "confirmed_at": confirmed_at,
            "actor": str(event.get("actor") or ""),
            "source": str(event.get("source") or "sku_management"),
            "advert_id": advert_id,
            "campaign": str(event.get("campaign") or ""),
            "placement": placement,
            "preview_id": str(event.get("preview_id") or ""),
            "correlation_id": str(event.get("correlation_id") or ""),
            "commit_status": commit_status,
            "readback_status": readback_status,
            "readback": dict(event.get("readback") or {}) if isinstance(event.get("readback"), Mapping) else {},
            "warnings": list(event.get("warnings") or []) if isinstance(event.get("warnings"), (list, tuple)) else [],
            "stabilization_override": bool(event.get("stabilization_override")),
            "warning_override": bool(event.get("warning_override")),
            "error": str(event.get("error") or ""),
        }
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_sku_action_events(
                    event_id, nm_id, parameter, old_value, requested_value,
                    confirmed_value, delta, requested_at, confirmed_at, actor,
                    source, advert_id, campaign, placement, preview_id,
                    correlation_id, commit_status, readback_status, readback_json, warnings_json,
                    stabilization_override, warning_override, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["event_id"], payload["nm_id"], payload["parameter"], payload["old_value"],
                    payload["requested_value"], payload["confirmed_value"], payload["delta"],
                    payload["requested_at"], payload["confirmed_at"] or None, payload["actor"],
                    payload["source"], payload["advert_id"], payload["campaign"], payload["placement"],
                    payload["preview_id"], payload["correlation_id"], payload["commit_status"],
                    payload["readback_status"],
                    json.dumps(payload["readback"], ensure_ascii=False, sort_keys=True),
                    json.dumps(payload["warnings"], ensure_ascii=False),
                    1 if payload["stabilization_override"] else 0,
                    1 if payload["warning_override"] else 0,
                    payload["error"],
                ),
            )
            conn.commit()
        return payload

    def list_sku_action_events(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        nm_id: int | None = None,
        parameter: str = "",
        status: str = "",
    ) -> dict[str, Any]:
        limit = min(max(int(limit), 1), 200)
        offset = max(int(offset), 0)
        clauses: list[str] = []
        params: list[Any] = []
        if nm_id is not None:
            clauses.append("nm_id = ?")
            params.append(int(nm_id))
        if parameter:
            clauses.append("parameter = ?")
            params.append(str(parameter))
        if status:
            clauses.append("commit_status = ?")
            params.append(str(status))
        where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            total = int(conn.execute(f"SELECT COUNT(*) AS count FROM sheet_vitrina_v1_sku_action_events{where_sql}", params).fetchone()["count"])
            rows = conn.execute(
                f"""
                SELECT * FROM sheet_vitrina_v1_sku_action_events
                {where_sql}
                ORDER BY COALESCE(confirmed_at, requested_at) DESC, event_id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        return {
            "contract_name": "sheet_vitrina_v1_sku_action_history",
            "rows": [_sku_action_event_row_to_dict(row) for row in rows],
            "pagination": {"limit": limit, "offset": offset, "total": total},
            "canonical_store": "server_runtime_sqlite",
        }

    def latest_sku_action_events_by_nm(self, nm_ids: Iterable[int]) -> dict[int, dict[str, dict[str, Any]]]:
        requested = sorted({int(value) for value in nm_ids if int(value) > 0})
        if not requested:
            return {}
        placeholders = ",".join("?" for _ in requested)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                f"""
                SELECT * FROM sheet_vitrina_v1_sku_action_events
                WHERE nm_id IN ({placeholders})
                  AND commit_status = 'confirmed'
                  AND readback_status = 'matching'
                  AND confirmed_at IS NOT NULL
                ORDER BY confirmed_at DESC, event_id DESC
                """,
                requested,
            ).fetchall()
        result: dict[int, dict[str, dict[str, Any]]] = {}
        for row in rows:
            nm_id = int(row["nm_id"])
            parameter = str(row["parameter"])
            if parameter in result.setdefault(nm_id, {}):
                continue
            result[nm_id][parameter] = _sku_action_event_row_to_dict(row)
        return result

    def load_sku_action_daily_metric_lookup(self, as_of_date: str) -> dict[int, dict[str, float]]:
        """Confirmed daily deltas; missing day stays absent rather than fake zero."""

        _validate_iso_date(as_of_date, field_name="as_of_date")
        target = date.fromisoformat(as_of_date)
        candidate_from = (target - timedelta(days=1)).isoformat()
        candidate_to = (target + timedelta(days=2)).isoformat()
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT nm_id, parameter, delta, confirmed_at
                FROM sheet_vitrina_v1_sku_action_events
                WHERE commit_status = 'confirmed'
                  AND readback_status = 'matching'
                  AND confirmed_at IS NOT NULL
                  AND substr(confirmed_at, 1, 10) >= ?
                  AND substr(confirmed_at, 1, 10) < ?
                """,
                (candidate_from, candidate_to),
            ).fetchall()
        result: dict[int, dict[str, float]] = {}
        for row in rows:
            if business_date_from_timestamp(str(row["confirmed_at"])) != as_of_date:
                continue
            key = "seller_price_change_rub" if row["parameter"] == "seller_price" else "advertising_bid_change_rub"
            bucket = result.setdefault(int(row["nm_id"]), {})
            total = Decimal(str(bucket.get(key, 0.0))) + Decimal(str(row["delta"]))
            bucket[key] = float(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        return result

    def list_sheet_vitrina_users(self) -> list[dict[str, Any]]:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_users
                ORDER BY is_active DESC, role ASC, username ASC
                """
            ).fetchall()
            return [_sheet_vitrina_user_row_to_dict(row, include_password_hash=False) for row in rows]

    def load_sheet_vitrina_user(self, user_id: str) -> dict[str, Any] | None:
        normalized_user_id = _normalize_required_storage_key(user_id, field_name="user_id")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_users
                WHERE user_id = ?
                """,
                (normalized_user_id,),
            ).fetchone()
            return _sheet_vitrina_user_row_to_dict(row, include_password_hash=True) if row is not None else None

    def load_sheet_vitrina_user_by_username(self, username: str) -> dict[str, Any] | None:
        normalized_username = _normalize_username_storage_value(username)
        if not normalized_username:
            return None
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_users
                WHERE username = ?
                """,
                (normalized_username,),
            ).fetchone()
            return _sheet_vitrina_user_row_to_dict(row, include_password_hash=True) if row is not None else None

    def save_sheet_vitrina_user(self, user: Mapping[str, Any]) -> dict[str, Any]:
        user_id = _normalize_required_storage_key(str(user.get("user_id") or ""), field_name="user_id")
        username = _normalize_username_storage_value(str(user.get("username") or ""))
        if not username:
            raise ValueError("username is required")
        role = str(user.get("role") or "").strip()
        if not role:
            raise ValueError("role is required")
        allowed_sections = _normalize_sheet_vitrina_user_sections(
            user.get("allowed_sections"),
            role=role,
        )
        manage_users = bool(user.get("manage_users", _default_sheet_vitrina_manage_users_for_role(role)))
        password_hash = str(user.get("password_hash") or "").strip()
        if not password_hash:
            raise ValueError("password_hash is required")
        created_at = str(user.get("created_at") or "").strip()
        updated_at = str(user.get("updated_at") or "").strip()
        _validate_timestamp(created_at, field_name="created_at")
        _validate_timestamp(updated_at, field_name="updated_at")
        is_active = 1 if bool(user.get("is_active", True)) else 0
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_users(
                    user_id,
                    username,
                    display_name,
                    role,
                    allowed_sections_json,
                    manage_users,
                    password_hash,
                    is_active,
                    created_at,
                    updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    username,
                    str(user.get("display_name") or "").strip(),
                    role,
                    json.dumps(allowed_sections, ensure_ascii=False),
                    1 if manage_users else 0,
                    password_hash,
                    is_active,
                    created_at,
                    updated_at,
                ),
            )
            conn.commit()
        saved = self.load_sheet_vitrina_user(user_id)
        if saved is None:
            raise ValueError(f"sheet vitrina user was not saved: {user_id}")
        saved.pop("password_hash", None)
        return saved

    def update_sheet_vitrina_user(
        self,
        user_id: str,
        updates: Mapping[str, Any],
        *,
        updated_at: str,
    ) -> dict[str, Any]:
        normalized_user_id = _normalize_required_storage_key(user_id, field_name="user_id")
        _validate_timestamp(updated_at, field_name="updated_at")
        existing = self.load_sheet_vitrina_user(normalized_user_id)
        if existing is None:
            raise ValueError(f"sheet vitrina user not found: {normalized_user_id}")
        display_name = str(updates.get("display_name", existing.get("display_name") or "") or "").strip()
        role = str(updates.get("role", existing.get("role") or "") or "").strip()
        allowed_sections = _normalize_sheet_vitrina_user_sections(
            updates.get("allowed_sections", existing.get("allowed_sections")),
            role=role,
        )
        manage_users = bool(
            updates.get(
                "manage_users",
                existing.get("manage_users", _default_sheet_vitrina_manage_users_for_role(role)),
            )
        )
        password_hash = str(updates.get("password_hash", existing.get("password_hash") or "") or "").strip()
        if not role:
            raise ValueError("role is required")
        if not password_hash:
            raise ValueError("password_hash is required")
        is_active = existing.get("is_active", True)
        if "is_active" in updates:
            is_active = bool(updates.get("is_active"))
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            cursor = conn.execute(
                """
                UPDATE sheet_vitrina_v1_users
                SET display_name = ?,
                    role = ?,
                    allowed_sections_json = ?,
                    manage_users = ?,
                    password_hash = ?,
                    is_active = ?,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (
                    display_name,
                    role,
                    json.dumps(allowed_sections, ensure_ascii=False),
                    1 if manage_users else 0,
                    password_hash,
                    1 if bool(is_active) else 0,
                    updated_at,
                    normalized_user_id,
                ),
            )
            conn.commit()
            if cursor.rowcount <= 0:
                raise ValueError(f"sheet vitrina user not found: {normalized_user_id}")
        saved = self.load_sheet_vitrina_user(normalized_user_id)
        if saved is None:
            raise ValueError(f"sheet vitrina user not found: {normalized_user_id}")
        saved.pop("password_hash", None)
        return saved

    def archive_sheet_vitrina_user(self, user_id: str, *, updated_at: str) -> dict[str, Any]:
        return self.update_sheet_vitrina_user(
            user_id,
            {"is_active": False},
            updated_at=updated_at,
        )

    def save_sheet_vitrina_load_state(
        self,
        *,
        loaded_at: str,
        snapshot_id: str | None,
        as_of_date: str | None,
        refreshed_at: str | None,
        plan_fingerprint: str | None,
        result_payload: Mapping[str, Any] | None,
    ) -> None:
        _validate_timestamp(loaded_at, field_name="loaded_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_load_state(
                    slot,
                    loaded_at,
                    snapshot_id,
                    as_of_date,
                    refreshed_at,
                    plan_fingerprint,
                    result_json
                )
                VALUES(1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(slot) DO UPDATE SET
                    loaded_at = excluded.loaded_at,
                    snapshot_id = excluded.snapshot_id,
                    as_of_date = excluded.as_of_date,
                    refreshed_at = excluded.refreshed_at,
                    plan_fingerprint = excluded.plan_fingerprint,
                    result_json = excluded.result_json
                """,
                (
                    loaded_at,
                    snapshot_id,
                    as_of_date,
                    refreshed_at,
                    plan_fingerprint,
                    _serialize_optional_state_payload(result_payload),
                ),
            )
            conn.commit()

    def load_sheet_vitrina_load_state(self) -> SheetVitrinaV1LoadState:
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT
                    loaded_at,
                    snapshot_id,
                    as_of_date,
                    refreshed_at,
                    plan_fingerprint,
                    result_json
                FROM sheet_vitrina_v1_load_state
                WHERE slot = 1
                """
            ).fetchone()
            if row is None:
                return SheetVitrinaV1LoadState(
                    loaded_at=None,
                    snapshot_id=None,
                    as_of_date=None,
                    refreshed_at=None,
                    plan_fingerprint=None,
                    result=None,
                )
            return SheetVitrinaV1LoadState(
                loaded_at=row["loaded_at"],
                snapshot_id=row["snapshot_id"],
                as_of_date=row["as_of_date"],
                refreshed_at=row["refreshed_at"],
                plan_fingerprint=row["plan_fingerprint"],
                result=_deserialize_optional_state_payload(row["result_json"]),
            )

    def load_persisted_upload_result(self, bundle_version: str) -> RegistryUploadResult:
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT status, config_count, metrics_count, formulas_count, validation_errors_json, activated_at
                FROM registry_upload_results
                WHERE bundle_version = ?
                """,
                (bundle_version,),
            ).fetchone()
            if row is None:
                raise ValueError(f"upload result is not materialized for bundle_version: {bundle_version}")

            return RegistryUploadResult(
                status=row["status"],
                bundle_version=bundle_version,
                accepted_counts=RegistryUploadAcceptedCounts(
                    config_v2=row["config_count"],
                    metrics_v2=row["metrics_count"],
                    formulas_v2=row["formulas_count"],
                ),
                validation_errors=json.loads(row["validation_errors_json"]),
                activated_at=row["activated_at"],
            )

    def list_bundle_versions(self) -> list[str]:
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT bundle_version
                FROM registry_upload_versions
                ORDER BY activated_at, bundle_version
                """
            ).fetchall()
            return [row["bundle_version"] for row in rows]

    def save_temporal_source_snapshot(
        self,
        *,
        source_key: str,
        snapshot_date: str,
        captured_at: str,
        payload: Any,
    ) -> None:
        _validate_timestamp(captured_at, field_name="captured_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO temporal_source_snapshots(
                    source_key,
                    snapshot_date,
                    captured_at,
                    payload_json
                )
                VALUES(?, ?, ?, ?)
                ON CONFLICT(source_key, snapshot_date) DO UPDATE SET
                    captured_at = excluded.captured_at,
                    payload_json = excluded.payload_json
                """,
                (
                    source_key,
                    snapshot_date,
                    captured_at,
                    _serialize_temporal_source_payload(payload),
                ),
            )
            conn.commit()

    def replace_temporal_source_snapshot_window(
        self,
        *,
        source_key: str,
        date_from: str,
        date_to: str,
        captured_at: str,
        exact_date_payloads: Mapping[str, Any],
    ) -> dict[str, int]:
        """Atomically replace one bounded source/date window."""

        _validate_iso_date(date_from, field_name="date_from")
        _validate_iso_date(date_to, field_name="date_to")
        _validate_timestamp(captured_at, field_name="captured_at")
        if date_to < date_from:
            raise ValueError("date_to must be >= date_from")
        payloads = {
            str(snapshot_date): payload
            for snapshot_date, payload in exact_date_payloads.items()
        }
        if any(
            snapshot_date < date_from or snapshot_date > date_to
            for snapshot_date in payloads
        ):
            raise ValueError("exact_date_payloads contains a date outside the bounded window")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            # The first schema check in a fresh process can run idempotent DML
            # backfills and therefore leave an implicit transaction open.
            # Close that schema transaction before opening the bounded write.
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            try:
                deleted = conn.execute(
                    """
                    DELETE FROM temporal_source_snapshots
                    WHERE source_key = ?
                      AND snapshot_date >= ?
                      AND snapshot_date <= ?
                    """,
                    (source_key, date_from, date_to),
                ).rowcount
                for snapshot_date, payload in sorted(payloads.items()):
                    conn.execute(
                        """
                        INSERT INTO temporal_source_snapshots(
                            source_key,
                            snapshot_date,
                            captured_at,
                            payload_json
                        )
                        VALUES(?, ?, ?, ?)
                        """,
                        (
                            source_key,
                            snapshot_date,
                            captured_at,
                            _serialize_temporal_source_payload(payload),
                        ),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            "deleted_snapshot_count": int(deleted or 0),
            "saved_snapshot_count": len(payloads),
        }

    def load_temporal_source_snapshot(
        self,
        *,
        source_key: str,
        snapshot_date: str,
    ) -> tuple[Any | None, str | None]:
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT captured_at, payload_json
                FROM temporal_source_snapshots
                WHERE source_key = ? AND snapshot_date = ?
                """,
                (source_key, snapshot_date),
            ).fetchone()
            if row is None:
                return None, None
            return _deserialize_temporal_source_payload(row["payload_json"]), row["captured_at"]

    def list_temporal_source_snapshot_dates(self, *, source_key: str) -> list[str]:
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT snapshot_date
                FROM temporal_source_snapshots
                WHERE source_key = ?
                ORDER BY snapshot_date
                """,
                (source_key,),
            ).fetchall()
            return [str(row["snapshot_date"]) for row in rows]

    def list_temporal_source_snapshots(
        self,
        *,
        source_key: str,
        date_to: str,
    ) -> list[tuple[str, Any, str]]:
        """Read one source's accepted candidates in descending date order."""

        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT snapshot_date, captured_at, payload_json
                FROM temporal_source_snapshots
                WHERE source_key = ? AND snapshot_date <= ?
                ORDER BY snapshot_date DESC
                """,
                (source_key, date_to),
            ).fetchall()
            return [
                (
                    str(row["snapshot_date"]),
                    _deserialize_temporal_source_payload(row["payload_json"]),
                    str(row["captured_at"] or ""),
                )
                for row in rows
            ]

    def delete_temporal_source_snapshots(
        self,
        *,
        source_key: str,
        date_from: str,
        date_to: str,
    ) -> int:
        _validate_iso_date(date_from, field_name="date_from")
        _validate_iso_date(date_to, field_name="date_to")
        if date_to < date_from:
            raise ValueError("date_to must be >= date_from")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            cursor = conn.execute(
                """
                DELETE FROM temporal_source_snapshots
                WHERE source_key = ?
                  AND snapshot_date >= ?
                  AND snapshot_date <= ?
                """,
                (source_key, date_from, date_to),
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    def save_temporal_source_slot_snapshot(
        self,
        *,
        source_key: str,
        snapshot_date: str,
        snapshot_role: str,
        captured_at: str,
        payload: Any,
    ) -> None:
        _validate_timestamp(captured_at, field_name="captured_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO temporal_source_slot_snapshots(
                    source_key,
                    snapshot_date,
                    snapshot_role,
                    captured_at,
                    payload_json
                )
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(source_key, snapshot_date, snapshot_role) DO UPDATE SET
                    captured_at = excluded.captured_at,
                    payload_json = excluded.payload_json
                """,
                (
                    source_key,
                    snapshot_date,
                    snapshot_role,
                    captured_at,
                    _serialize_temporal_source_payload(payload),
                ),
            )
            conn.commit()

    def load_temporal_source_slot_snapshot(
        self,
        *,
        source_key: str,
        snapshot_date: str,
        snapshot_role: str,
    ) -> tuple[Any | None, str | None]:
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT captured_at, payload_json
                FROM temporal_source_slot_snapshots
                WHERE source_key = ? AND snapshot_date = ? AND snapshot_role = ?
                """,
                (source_key, snapshot_date, snapshot_role),
            ).fetchone()
            if row is None:
                return None, None
            return _deserialize_temporal_source_payload(row["payload_json"]), row["captured_at"]

    def delete_temporal_source_slot_snapshots(
        self,
        *,
        source_key: str,
        date_from: str,
        date_to: str,
        snapshot_roles: list[str] | None = None,
    ) -> int:
        _validate_iso_date(date_from, field_name="date_from")
        _validate_iso_date(date_to, field_name="date_to")
        if date_to < date_from:
            raise ValueError("date_to must be >= date_from")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            params: list[Any] = [source_key, date_from, date_to]
            where_roles = ""
            if snapshot_roles:
                placeholders = ",".join("?" for _ in snapshot_roles)
                where_roles = f" AND snapshot_role IN ({placeholders})"
                params.extend(snapshot_roles)
            cursor = conn.execute(
                f"""
                DELETE FROM temporal_source_slot_snapshots
                WHERE source_key = ?
                  AND snapshot_date >= ?
                  AND snapshot_date <= ?
                  {where_roles}
                """,
                tuple(params),
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    def save_temporal_source_closure_state(
        self,
        *,
        source_key: str,
        target_date: str,
        slot_kind: str,
        state: str,
        attempt_count: int,
        next_retry_at: str | None,
        last_reason: str | None,
        last_attempt_at: str | None,
        last_success_at: str | None,
        accepted_at: str | None,
    ) -> None:
        _validate_iso_date(target_date, field_name="target_date")
        _validate_optional_timestamp(next_retry_at, field_name="next_retry_at")
        _validate_optional_timestamp(last_attempt_at, field_name="last_attempt_at")
        _validate_optional_timestamp(last_success_at, field_name="last_success_at")
        _validate_optional_timestamp(accepted_at, field_name="accepted_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO temporal_source_closure_state(
                    source_key,
                    target_date,
                    slot_kind,
                    state,
                    attempt_count,
                    next_retry_at,
                    last_reason,
                    last_attempt_at,
                    last_success_at,
                    accepted_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key, target_date, slot_kind) DO UPDATE SET
                    state = excluded.state,
                    attempt_count = excluded.attempt_count,
                    next_retry_at = excluded.next_retry_at,
                    last_reason = excluded.last_reason,
                    last_attempt_at = excluded.last_attempt_at,
                    last_success_at = excluded.last_success_at,
                    accepted_at = excluded.accepted_at
                """,
                (
                    source_key,
                    target_date,
                    slot_kind,
                    state,
                    attempt_count,
                    next_retry_at,
                    last_reason,
                    last_attempt_at,
                    last_success_at,
                    accepted_at,
                ),
            )
            conn.commit()

    def load_temporal_source_closure_state(
        self,
        *,
        source_key: str,
        target_date: str,
        slot_kind: str,
    ) -> TemporalSourceClosureState | None:
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT
                    source_key,
                    target_date,
                    slot_kind,
                    state,
                    attempt_count,
                    next_retry_at,
                    last_reason,
                    last_attempt_at,
                    last_success_at,
                    accepted_at
                FROM temporal_source_closure_state
                WHERE source_key = ? AND target_date = ? AND slot_kind = ?
                """,
                (source_key, target_date, slot_kind),
            ).fetchone()
            if row is None:
                return None
            return TemporalSourceClosureState(
                source_key=row["source_key"],
                target_date=row["target_date"],
                slot_kind=row["slot_kind"],
                state=row["state"],
                attempt_count=int(row["attempt_count"]),
                next_retry_at=row["next_retry_at"],
                last_reason=row["last_reason"],
                last_attempt_at=row["last_attempt_at"],
                last_success_at=row["last_success_at"],
                accepted_at=row["accepted_at"],
            )

    def list_temporal_source_closure_states(
        self,
        *,
        source_keys: list[str] | None = None,
        slot_kind: str | None = None,
        states: list[str] | None = None,
    ) -> list[TemporalSourceClosureState]:
        query = [
            """
            SELECT
                source_key,
                target_date,
                slot_kind,
                state,
                attempt_count,
                next_retry_at,
                last_reason,
                last_attempt_at,
                last_success_at,
                accepted_at
            FROM temporal_source_closure_state
            WHERE 1 = 1
            """
        ]
        params: list[Any] = []
        if source_keys:
            placeholders = ",".join("?" for _ in source_keys)
            query.append(f"AND source_key IN ({placeholders})")
            params.extend(source_keys)
        if slot_kind:
            query.append("AND slot_kind = ?")
            params.append(slot_kind)
        if states:
            placeholders = ",".join("?" for _ in states)
            query.append(f"AND state IN ({placeholders})")
            params.extend(states)
        query.append("ORDER BY target_date, source_key, slot_kind")
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute("\n".join(query), tuple(params)).fetchall()
            return [
                TemporalSourceClosureState(
                    source_key=row["source_key"],
                    target_date=row["target_date"],
                    slot_kind=row["slot_kind"],
                    state=row["state"],
                    attempt_count=int(row["attempt_count"]),
                    next_retry_at=row["next_retry_at"],
                    last_reason=row["last_reason"],
                    last_attempt_at=row["last_attempt_at"],
                    last_success_at=row["last_success_at"],
                    accepted_at=row["accepted_at"],
                )
                for row in rows
            ]

    def delete_temporal_source_closure_states(
        self,
        *,
        source_key: str,
        date_from: str,
        date_to: str,
    ) -> int:
        _validate_iso_date(date_from, field_name="date_from")
        _validate_iso_date(date_to, field_name="date_to")
        if date_to < date_from:
            raise ValueError("date_to must be >= date_from")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            cursor = conn.execute(
                """
                DELETE FROM temporal_source_closure_state
                WHERE source_key = ?
                  AND target_date >= ?
                  AND target_date <= ?
                """,
                (source_key, date_from, date_to),
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    def save_factory_order_dataset_state(
        self,
        *,
        dataset_type: str,
        uploaded_at: str,
        rows: list[Mapping[str, Any]],
        uploaded_filename: str,
        uploaded_content_type: str,
        workbook_bytes: bytes,
    ) -> None:
        _validate_timestamp(uploaded_at, field_name="uploaded_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_factory_order_dataset_state(
                    dataset_type,
                    uploaded_at,
                    row_count,
                    rows_json,
                    uploaded_filename,
                    uploaded_content_type,
                    workbook_blob
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dataset_type) DO UPDATE SET
                    uploaded_at = excluded.uploaded_at,
                    row_count = excluded.row_count,
                    rows_json = excluded.rows_json,
                    uploaded_filename = excluded.uploaded_filename,
                    uploaded_content_type = excluded.uploaded_content_type,
                    workbook_blob = excluded.workbook_blob
                """,
                (
                    dataset_type,
                    uploaded_at,
                    len(rows),
                    json.dumps(list(rows), ensure_ascii=False),
                    uploaded_filename,
                    uploaded_content_type,
                    sqlite3.Binary(workbook_bytes),
                ),
            )
            conn.commit()

    def load_factory_order_dataset_state(
        self,
        dataset_type: str,
        *,
        include_file_blob: bool = False,
    ) -> dict[str, Any] | None:
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            select_columns = [
                "uploaded_at",
                "row_count",
                "rows_json",
                "uploaded_filename",
                "uploaded_content_type",
                "workbook_blob IS NOT NULL AS file_available",
            ]
            if include_file_blob:
                select_columns.append("workbook_blob")
            row = conn.execute(
                f"""
                SELECT {", ".join(select_columns)}
                FROM sheet_vitrina_v1_factory_order_dataset_state
                WHERE dataset_type = ?
                """,
                (dataset_type,),
            ).fetchone()
            if row is None:
                return None
            payload = {
                "dataset_type": dataset_type,
                "uploaded_at": row["uploaded_at"],
                "row_count": int(row["row_count"]),
                "rows": json.loads(row["rows_json"]),
                "uploaded_filename": str(row["uploaded_filename"] or "") or None,
                "uploaded_content_type": str(row["uploaded_content_type"] or "") or None,
                "file_available": bool(row["file_available"]),
            }
            if include_file_blob:
                payload["workbook_bytes"] = bytes(row["workbook_blob"] or b"")
            return payload

    def delete_factory_order_dataset_state(self, dataset_type: str) -> bool:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            cursor = conn.execute(
                """
                DELETE FROM sheet_vitrina_v1_factory_order_dataset_state
                WHERE dataset_type = ?
                """,
                (dataset_type,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def save_ff_stock_operation_preview(
        self,
        *,
        preview_id: str,
        operation_type: str,
        created_at: str,
        uploaded_filename: str,
        uploaded_content_type: str,
        source_file_sha256: str,
        workbook_bytes: bytes,
        parsed_lines: list[Mapping[str, Any]],
        summary: Mapping[str, Any],
        warnings: list[str],
        errors: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        normalized_preview_id = str(preview_id or "").strip()
        if not normalized_preview_id:
            raise ValueError("preview_id is required")
        _validate_timestamp(created_at, field_name="created_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_ff_stock_operation_previews(
                    preview_id,
                    operation_type,
                    created_at,
                    uploaded_filename,
                    uploaded_content_type,
                    source_file_sha256,
                    source_file_blob,
                    parsed_lines_json,
                    summary_json,
                    warnings_json,
                    errors_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(preview_id) DO UPDATE SET
                    operation_type = excluded.operation_type,
                    created_at = excluded.created_at,
                    uploaded_filename = excluded.uploaded_filename,
                    uploaded_content_type = excluded.uploaded_content_type,
                    source_file_sha256 = excluded.source_file_sha256,
                    source_file_blob = excluded.source_file_blob,
                    parsed_lines_json = excluded.parsed_lines_json,
                    summary_json = excluded.summary_json,
                    warnings_json = excluded.warnings_json,
                    errors_json = excluded.errors_json
                """,
                (
                    normalized_preview_id,
                    str(operation_type or "").strip(),
                    created_at,
                    str(uploaded_filename or "").strip(),
                    str(uploaded_content_type or "").strip(),
                    str(source_file_sha256 or "").strip(),
                    sqlite3.Binary(workbook_bytes or b""),
                    json.dumps(list(parsed_lines), ensure_ascii=False),
                    json.dumps(dict(summary), ensure_ascii=False),
                    json.dumps(list(warnings), ensure_ascii=False),
                    json.dumps(list(errors), ensure_ascii=False),
                ),
            )
            conn.commit()
        preview = self.load_ff_stock_operation_preview(normalized_preview_id, include_file_blob=False)
        if preview is None:
            raise ValueError(f"ФФ stock preview was not saved: {normalized_preview_id}")
        return preview

    def load_ff_stock_operation_preview(
        self,
        preview_id: str,
        *,
        include_file_blob: bool = False,
    ) -> dict[str, Any] | None:
        normalized_preview_id = str(preview_id or "").strip()
        if not normalized_preview_id:
            return None
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            select_columns = [
                "preview_id",
                "operation_type",
                "created_at",
                "uploaded_filename",
                "uploaded_content_type",
                "source_file_sha256",
                "source_file_blob IS NOT NULL AS file_available",
                "parsed_lines_json",
                "summary_json",
                "warnings_json",
                "errors_json",
            ]
            if include_file_blob:
                select_columns.append("source_file_blob")
            row = conn.execute(
                f"""
                SELECT {", ".join(select_columns)}
                FROM sheet_vitrina_v1_ff_stock_operation_previews
                WHERE preview_id = ?
                """,
                (normalized_preview_id,),
            ).fetchone()
            if row is None:
                return None
            payload = _ff_stock_preview_to_dict(row)
            if include_file_blob:
                payload["workbook_bytes"] = bytes(row["source_file_blob"] or b"")
            return payload

    def delete_ff_stock_operation_preview(self, preview_id: str) -> bool:
        normalized_preview_id = str(preview_id or "").strip()
        if not normalized_preview_id:
            return False
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            cursor = conn.execute(
                """
                DELETE FROM sheet_vitrina_v1_ff_stock_operation_previews
                WHERE preview_id = ?
                """,
                (normalized_preview_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def load_ff_stock_operation_by_source_key(self, source_key: str) -> dict[str, Any] | None:
        normalized_source_key = str(source_key or "").strip()
        if not normalized_source_key:
            return None
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_ff_stock_operations
                WHERE source_key = ?
                """,
                (normalized_source_key,),
            ).fetchone()
            if row is None:
                return None
            return _ff_stock_operation_to_dict(row)

    def load_ff_stock_activation_operation(self) -> dict[str, Any] | None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_ff_stock_operations
                WHERE source_type <> 'wb_supply'
                  AND total_quantity_delta > 0
                ORDER BY created_at ASC, operation_id ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            return _ff_stock_operation_to_dict(row)

    def load_ff_stock_wb_auto_writeoff_checkpoint(self, *, slot: str = "current") -> dict[str, Any] | None:
        normalized_slot = str(slot or "current").strip() or "current"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_ff_stock_wb_auto_writeoff_checkpoint
                WHERE slot = ?
                """,
                (normalized_slot,),
            ).fetchone()
            if row is None:
                return None
            return _ff_stock_wb_auto_writeoff_checkpoint_to_dict(row)

    def save_ff_stock_wb_auto_writeoff_checkpoint(
        self,
        *,
        checkpoint_id: str,
        created_at: str,
        created_by: str = "",
        reason: str = "",
        slot: str = "current",
        baseline_cache_keys: list[str] | None = None,
        baseline_source_keys: list[str] | None = None,
        baseline_supply_ids: list[str] | None = None,
        watermark_source_created_at: str = "",
        watermark_supply_date: str = "",
        diagnostics: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_slot = str(slot or "current").strip() or "current"
        normalized_checkpoint_id = str(checkpoint_id or "").strip()
        if not normalized_checkpoint_id:
            raise ValueError("checkpoint_id is required")
        _validate_timestamp(created_at, field_name="created_at")
        if watermark_source_created_at:
            _validate_timestamp(watermark_source_created_at, field_name="watermark_source_created_at")
        cache_keys = sorted({str(item).strip() for item in (baseline_cache_keys or []) if str(item or "").strip()})
        source_keys = sorted({str(item).strip() for item in (baseline_source_keys or []) if str(item or "").strip()})
        supply_ids = sorted({str(item).strip() for item in (baseline_supply_ids or []) if str(item or "").strip()})
        baseline_record_count = max(len(cache_keys), len(source_keys), len(supply_ids))
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_ff_stock_wb_auto_writeoff_checkpoint(
                    slot,
                    checkpoint_id,
                    created_at,
                    created_by,
                    reason,
                    baseline_cache_keys_json,
                    baseline_source_keys_json,
                    baseline_supply_ids_json,
                    baseline_record_count,
                    watermark_source_created_at,
                    watermark_supply_date,
                    diagnostics_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(slot) DO UPDATE SET
                    checkpoint_id = excluded.checkpoint_id,
                    created_at = excluded.created_at,
                    created_by = excluded.created_by,
                    reason = excluded.reason,
                    baseline_cache_keys_json = excluded.baseline_cache_keys_json,
                    baseline_source_keys_json = excluded.baseline_source_keys_json,
                    baseline_supply_ids_json = excluded.baseline_supply_ids_json,
                    baseline_record_count = excluded.baseline_record_count,
                    watermark_source_created_at = excluded.watermark_source_created_at,
                    watermark_supply_date = excluded.watermark_supply_date,
                    diagnostics_json = excluded.diagnostics_json
                """,
                (
                    normalized_slot,
                    normalized_checkpoint_id,
                    created_at,
                    str(created_by or "").strip(),
                    str(reason or "").strip(),
                    json.dumps(cache_keys, ensure_ascii=False),
                    json.dumps(source_keys, ensure_ascii=False),
                    json.dumps(supply_ids, ensure_ascii=False),
                    baseline_record_count,
                    watermark_source_created_at,
                    str(watermark_supply_date or "").strip(),
                    json.dumps(dict(diagnostics or {}), ensure_ascii=False),
                ),
            )
            conn.commit()
            row = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_ff_stock_wb_auto_writeoff_checkpoint
                WHERE slot = ?
                """,
                (normalized_slot,),
            ).fetchone()
            return _ff_stock_wb_auto_writeoff_checkpoint_to_dict(row)

    def create_ff_stock_operation(
        self,
        *,
        operation_id: str,
        operation_type: str,
        source_type: str,
        source_key: str,
        source_object_id: str = "",
        source_object_label: str = "",
        created_at: str,
        business_effective_date: str = "",
        created_by: str = "",
        warnings: list[str] | None = None,
        diagnostics: Mapping[str, Any] | None = None,
        source_filename: str = "",
        source_content_type: str = "",
        source_file_sha256: str = "",
        source_file_bytes: bytes | None = None,
        lines: list[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        normalized_operation_id = str(operation_id or "").strip()
        if not normalized_operation_id:
            raise ValueError("operation_id is required")
        normalized_source_key = str(source_key or "").strip()
        if not normalized_source_key:
            raise ValueError("source_key is required")
        _validate_timestamp(created_at, field_name="created_at")
        normalized_business_date = (
            str(business_effective_date or "").strip()
            or business_date_from_timestamp(created_at)
        )
        _validate_iso_date(
            normalized_business_date,
            field_name="business_effective_date",
        )
        normalized_lines = [dict(item) for item in (lines or [])]
        sku_ids = {
            int(item.get("nm_id"))
            for item in normalized_lines
            if item.get("nm_id") is not None
        }
        total_quantity_delta = sum(float(item.get("quantity_delta") or 0.0) for item in normalized_lines)
        total_quantity_abs = sum(abs(float(item.get("quantity_delta") or 0.0)) for item in normalized_lines)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            from packages.application.warehouse_business_projection import (
                ensure_warehouse_projection_source_outbox,
            )

            ensure_warehouse_projection_source_outbox(conn)
            existing = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_ff_stock_operations
                WHERE source_key = ?
                """,
                (normalized_source_key,),
            ).fetchone()
            if existing is not None:
                payload = _ff_stock_operation_to_dict(existing)
                payload["idempotent"] = True
                return payload
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_ff_stock_operations(
                    operation_id,
                    operation_type,
                    source_type,
                    source_key,
                    source_object_id,
                    source_object_label,
                    created_at,
                    business_effective_date,
                    created_by,
                    sku_count,
                    total_quantity_delta,
                    total_quantity_abs,
                    warnings_json,
                    diagnostics_json,
                    source_filename,
                    source_content_type,
                    source_file_sha256,
                    source_file_blob
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_operation_id,
                    str(operation_type or "").strip(),
                    str(source_type or "").strip(),
                    normalized_source_key,
                    str(source_object_id or "").strip(),
                    str(source_object_label or "").strip(),
                    created_at,
                    normalized_business_date,
                    str(created_by or "").strip(),
                    len(sku_ids),
                    total_quantity_delta,
                    total_quantity_abs,
                    json.dumps(list(warnings or []), ensure_ascii=False),
                    json.dumps(dict(diagnostics or {}), ensure_ascii=False),
                    str(source_filename or "").strip(),
                    str(source_content_type or "").strip(),
                    str(source_file_sha256 or "").strip(),
                    sqlite3.Binary(source_file_bytes or b"") if source_file_bytes is not None else None,
                ),
            )
            conn.executemany(
                """
                INSERT INTO sheet_vitrina_v1_ff_stock_operation_lines(
                    operation_id,
                    line_no,
                    nm_id,
                    barcode,
                    sku,
                    nomenclature_name,
                    comment,
                    group_name,
                    quantity_delta,
                    raw_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        normalized_operation_id,
                        index,
                        int(item.get("nm_id") or 0),
                        str(item.get("barcode") or "").strip(),
                        str(item.get("sku") or item.get("our_sku") or "").strip(),
                        str(item.get("nomenclature_name") or "").strip(),
                        str(item.get("comment") or "").strip(),
                        str(item.get("group_name") or "").strip(),
                        float(item.get("quantity_delta") or 0.0),
                        json.dumps(dict(item.get("raw") or item), ensure_ascii=False),
                    )
                    for index, item in enumerate(normalized_lines, start=1)
                ],
            )
            conn.commit()
            row = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_ff_stock_operations
                WHERE operation_id = ?
                """,
                (normalized_operation_id,),
            ).fetchone()
            payload = _ff_stock_operation_to_dict(row)
            payload["idempotent"] = False
            return payload

    def create_ff_stock_operation_guarded(
        self,
        *,
        operation_id: str,
        operation_type: str,
        source_type: str,
        source_key: str,
        source_object_id: str,
        source_object_label: str,
        created_at: str,
        created_by: str,
        business_effective_date: str = "",
        warnings: list[str] | None,
        diagnostics: Mapping[str, Any] | None,
        lines: list[Mapping[str, Any]],
        expected_balances: Mapping[int, float],
        expected_supply_guard: Mapping[str, Any] | None = None,
        expected_checkpoint: Mapping[str, Any] | None = None,
        expected_activation: Mapping[str, Any] | None = None,
        expected_active_nomenclature: Mapping[int, Mapping[str, Any]] | None = None,
        expected_ledger_totals: Mapping[str, float] | None = None,
        reservation_transition: Mapping[str, Any] | None = None,
        expected_downstream_costs: Mapping[int, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Atomically recheck a targeted plan and append one ledger operation."""
        normalized_operation_id = str(operation_id or "").strip()
        normalized_source_key = str(source_key or "").strip()
        if not normalized_operation_id:
            raise ValueError("operation_id is required")
        if not normalized_source_key:
            raise ValueError("source_key is required")
        _validate_timestamp(created_at, field_name="created_at")
        normalized_business_date = (
            str(business_effective_date or "").strip()
            or business_date_from_timestamp(created_at)
        )
        _validate_iso_date(
            normalized_business_date,
            field_name="business_effective_date",
        )
        normalized_lines = [dict(item) for item in lines]
        if not normalized_lines:
            raise ValueError("guarded FF stock operation requires lines")
        expected_balance_map = {int(key): float(value) for key, value in expected_balances.items()}
        line_nm_ids = {int(item.get("nm_id") or 0) for item in normalized_lines}
        if 0 in line_nm_ids or line_nm_ids != set(expected_balance_map):
            raise ValueError("guarded FF stock operation balance scope does not match lines")

        total_quantity_delta = sum(float(item.get("quantity_delta") or 0.0) for item in normalized_lines)
        total_quantity_abs = sum(abs(float(item.get("quantity_delta") or 0.0)) for item in normalized_lines)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            from packages.application.warehouse_business_projection import (
                ensure_warehouse_projection_source_outbox,
            )

            ensure_warehouse_projection_source_outbox(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_ff_stock_operations WHERE source_key = ?",
                    (normalized_source_key,),
                ).fetchone()
                if existing is not None:
                    payload = _ff_stock_operation_to_dict(existing)
                    payload["idempotent"] = True
                    conn.rollback()
                    return payload

                if expected_supply_guard is not None:
                    target_supply_id = str(expected_supply_guard.get("supply_id") or "").strip()
                    supply_row = conn.execute(
                        """
                        SELECT supply_id, cache_key, wb_supply_id, preorder_id,
                               normalized_row_json, raw_goods_json, raw_goods_hash
                        FROM sheet_vitrina_v1_wb_supplies
                        WHERE supply_id = ?
                        """,
                        (target_supply_id,),
                    ).fetchone()
                    actual_guard = _targeted_wb_supply_guard_from_row(supply_row)
                    if _canonical_json(actual_guard) != _canonical_json(dict(expected_supply_guard)):
                        raise ValueError(
                            "targeted_wb_supply_changed: "
                            + _canonical_json({"expected": dict(expected_supply_guard), "actual": actual_guard})
                        )
                    actual_source_key = f"wb_supply_debit:{actual_guard.get('cache_key') or ''}"
                    if actual_source_key != normalized_source_key:
                        raise ValueError(
                            "targeted_wb_supply_source_key_changed: "
                            + _canonical_json({"expected": normalized_source_key, "actual": actual_source_key})
                        )

                if expected_checkpoint is not None:
                    checkpoint_row = conn.execute(
                        """
                        SELECT checkpoint_id, created_at,
                               baseline_cache_keys_json, baseline_source_keys_json, baseline_supply_ids_json
                        FROM sheet_vitrina_v1_ff_stock_wb_auto_writeoff_checkpoint
                        WHERE slot = 'current'
                        """
                    ).fetchone()
                    actual_checkpoint = {
                        "checkpoint_id": str(checkpoint_row["checkpoint_id"] or "") if checkpoint_row else "",
                        "created_at": str(checkpoint_row["created_at"] or "") if checkpoint_row else "",
                        "baseline_cache_keys": _loads_json_list(checkpoint_row["baseline_cache_keys_json"]) if checkpoint_row else [],
                        "baseline_source_keys": _loads_json_list(checkpoint_row["baseline_source_keys_json"]) if checkpoint_row else [],
                        "baseline_supply_ids": _loads_json_list(checkpoint_row["baseline_supply_ids_json"]) if checkpoint_row else [],
                    }
                    if _canonical_json(actual_checkpoint) != _canonical_json(dict(expected_checkpoint)):
                        raise ValueError(
                            "targeted_wb_supply_checkpoint_changed: "
                            + _canonical_json({"expected": dict(expected_checkpoint), "actual": actual_checkpoint})
                        )

                if expected_activation is not None:
                    activation_row = conn.execute(
                        """
                        SELECT operation_id, created_at
                        FROM sheet_vitrina_v1_ff_stock_operations
                        WHERE source_type <> 'wb_supply'
                          AND total_quantity_delta > 0
                        ORDER BY created_at ASC, operation_id ASC
                        LIMIT 1
                        """
                    ).fetchone()
                    actual_activation = {
                        "operation_id": str(activation_row["operation_id"] or "") if activation_row else "",
                        "created_at": str(activation_row["created_at"] or "") if activation_row else "",
                    }
                    if _canonical_json(actual_activation) != _canonical_json(dict(expected_activation)):
                        raise ValueError(
                            "targeted_ff_stock_activation_changed: "
                            + _canonical_json({"expected": dict(expected_activation), "actual": actual_activation})
                        )

                if expected_active_nomenclature is not None:
                    expected_nomenclature = {
                        int(nm_id): dict(item)
                        for nm_id, item in expected_active_nomenclature.items()
                    }
                    item_ids = [str(item.get("item_id") or "") for item in expected_nomenclature.values()]
                    if not item_ids or any(not item_id for item_id in item_ids):
                        raise ValueError("targeted_active_nomenclature_guard_invalid")
                    nomenclature_placeholders = ",".join("?" for _ in item_ids)
                    nomenclature_rows = conn.execute(
                        f"""
                        SELECT item_id, nm_id, is_active, is_hidden, updated_at
                        FROM sheet_vitrina_v1_nomenclature_items
                        WHERE item_id IN ({nomenclature_placeholders})
                        """,
                        tuple(item_ids),
                    ).fetchall()
                    actual_nomenclature = {
                        int(row["nm_id"] or 0): {
                            "item_id": str(row["item_id"] or ""),
                            "nm_id": int(row["nm_id"] or 0),
                            "is_active": bool(row["is_active"]),
                            "is_hidden": bool(row["is_hidden"]),
                            "updated_at": str(row["updated_at"] or ""),
                        }
                        for row in nomenclature_rows
                    }
                    if _canonical_json(actual_nomenclature) != _canonical_json(expected_nomenclature):
                        raise ValueError(
                            "targeted_active_nomenclature_changed: "
                            + _canonical_json({"expected": expected_nomenclature, "actual": actual_nomenclature})
                        )
                    if any(not item["is_active"] or item["is_hidden"] for item in actual_nomenclature.values()):
                        raise ValueError("targeted_active_nomenclature_not_eligible")

                placeholders = ",".join("?" for _ in expected_balance_map)
                balance_rows = conn.execute(
                    f"""
                    SELECT nm_id, SUM(quantity_delta) AS balance
                    FROM sheet_vitrina_v1_ff_stock_operation_lines
                    WHERE nm_id IN ({placeholders})
                    GROUP BY nm_id
                    """,
                    tuple(sorted(expected_balance_map)),
                ).fetchall()
                actual_balances = {int(row["nm_id"]): float(row["balance"] or 0.0) for row in balance_rows}
                actual_balances = {nm_id: actual_balances.get(nm_id, 0.0) for nm_id in expected_balance_map}
                changed = [
                    {
                        "nm_id": nm_id,
                        "expected_balance": expected_balance_map[nm_id],
                        "actual_balance": actual_balances[nm_id],
                    }
                    for nm_id in sorted(expected_balance_map)
                    if abs(expected_balance_map[nm_id] - actual_balances[nm_id]) > 1e-9
                ]
                if changed:
                    raise ValueError("targeted_ff_stock_balances_changed: " + _canonical_json(changed))

                expected_total_map = {str(key): float(value) for key, value in (expected_ledger_totals or {}).items()}
                if expected_total_map:
                    required_total_keys = {"before", "delta", "after"}
                    if set(expected_total_map) != required_total_keys:
                        raise ValueError("targeted_ff_stock_total_guard_invalid")
                    total_row = conn.execute(
                        "SELECT COALESCE(SUM(quantity_delta), 0) AS total FROM sheet_vitrina_v1_ff_stock_operation_lines"
                    ).fetchone()
                    actual_total_before = float(total_row["total"] or 0.0) if total_row is not None else 0.0
                    if abs(actual_total_before - expected_total_map["before"]) > 1e-9:
                        raise ValueError(
                            "targeted_ff_stock_total_changed: "
                            + _canonical_json(
                                {"expected": expected_total_map["before"], "actual": actual_total_before}
                            )
                        )
                    if abs(total_quantity_delta - expected_total_map["delta"]) > 1e-9:
                        raise ValueError("targeted_ff_stock_delta_changed")
                    if abs(actual_total_before + total_quantity_delta - expected_total_map["after"]) > 1e-9:
                        raise ValueError("targeted_ff_stock_projected_total_changed")

                projected_by_nm = dict(actual_balances)
                for item in normalized_lines:
                    nm_id = int(item.get("nm_id") or 0)
                    projected_by_nm[nm_id] += float(item.get("quantity_delta") or 0.0)
                negative = [
                    {
                        "nm_id": nm_id,
                        "nmID": nm_id,
                        "current_balance": actual_balances[nm_id],
                        "required_debit": abs(
                            sum(
                                float(item.get("quantity_delta") or 0.0)
                                for item in normalized_lines
                                if int(item.get("nm_id") or 0) == nm_id
                            )
                        ),
                        "projected_balance": projected_by_nm[nm_id],
                        "expected_balance": projected_by_nm[nm_id],
                    }
                    for nm_id in sorted(projected_by_nm)
                    if projected_by_nm[nm_id] < -1e-9
                ]
                if negative:
                    raise ValueError("targeted_ff_stock_would_make_negative_balance: " + _canonical_json(negative))

                reservation_payload = dict(reservation_transition or {})
                if reservation_payload:
                    reservation_supply_id = str(reservation_payload.get("supply_id") or "")
                    reservation_expected = {
                        int(key): float(value)
                        for key, value in dict(reservation_payload.get("expected_current") or {}).items()
                    }
                    actual_reservations = _ff_stock_reservation_quantities(
                        conn,
                        reservation_supply_id,
                    )
                    reservation_scope = set(reservation_expected) | set(actual_reservations)
                    if any(
                        abs(
                            reservation_expected.get(nm_id, 0.0)
                            - actual_reservations.get(nm_id, 0.0)
                        )
                        > 1e-9
                        for nm_id in reservation_scope
                    ):
                        raise ValueError(
                            "ff_stock_reservation_changed: "
                            + _canonical_json(
                                {
                                    "expected": reservation_expected,
                                    "actual": actual_reservations,
                                }
                            )
                        )

                for nm_id, expected_cost in dict(expected_downstream_costs or {}).items():
                    expected_cost_payload = dict(expected_cost)
                    cost_row = conn.execute(
                        """
                        SELECT wb_supply_id,nm_id,inputs_hash,source_status,
                               transit_cost_status,sku_ff_unit_cost_rub,
                               pre_acceptance_unit_cost_rub
                        FROM sheet_vitrina_v1_wb_supply_cost_layers
                        WHERE wb_supply_id=? AND nm_id=? AND is_current=1
                        """,
                        (
                            str(expected_cost_payload.get("wb_supply_id") or ""),
                            int(nm_id),
                        ),
                    ).fetchone()
                    actual_cost = dict(cost_row) if cost_row is not None else {}
                    if _canonical_json(actual_cost) != _canonical_json(expected_cost_payload):
                        raise ValueError(
                            "validated_downstream_cost_state_changed: "
                            + _canonical_json(
                                {"expected": expected_cost_payload, "actual": actual_cost}
                            )
                        )

                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_ff_stock_operations(
                        operation_id, operation_type, source_type, source_key,
                        source_object_id, source_object_label, created_at,
                        business_effective_date, created_by, sku_count,
                        total_quantity_delta, total_quantity_abs,
                        warnings_json, diagnostics_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_operation_id,
                        str(operation_type or "").strip(),
                        str(source_type or "").strip(),
                        normalized_source_key,
                        str(source_object_id or "").strip(),
                        str(source_object_label or "").strip(),
                        created_at,
                        normalized_business_date,
                        str(created_by or "").strip(),
                        len(line_nm_ids),
                        total_quantity_delta,
                        total_quantity_abs,
                        json.dumps(list(warnings or []), ensure_ascii=False),
                        json.dumps(dict(diagnostics or {}), ensure_ascii=False),
                    ),
                )
                conn.executemany(
                    """
                    INSERT INTO sheet_vitrina_v1_ff_stock_operation_lines(
                        operation_id, line_no, nm_id, barcode, sku,
                        nomenclature_name, comment, group_name, quantity_delta, raw_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            normalized_operation_id,
                            index,
                            int(item.get("nm_id") or 0),
                            str(item.get("barcode") or "").strip(),
                            str(item.get("sku") or item.get("our_sku") or "").strip(),
                            str(item.get("nomenclature_name") or "").strip(),
                            str(item.get("comment") or "").strip(),
                            str(item.get("group_name") or "").strip(),
                            float(item.get("quantity_delta") or 0.0),
                            json.dumps(dict(item.get("raw") or item), ensure_ascii=False),
                        )
                        for index, item in enumerate(normalized_lines, start=1)
                    ],
                )
                if reservation_payload:
                    reservation_lines = [
                        dict(item) for item in reservation_payload.get("lines") or []
                    ]
                    _insert_ff_stock_reservation_operation(
                        conn,
                        operation_id=str(reservation_payload.get("operation_id") or ""),
                        source_key=str(reservation_payload.get("source_key") or ""),
                        supply_id=str(reservation_payload.get("supply_id") or ""),
                        supply_revision=str(reservation_payload.get("supply_revision") or ""),
                        operation_type=str(reservation_payload.get("operation_type") or "fulfill"),
                        created_at=str(reservation_payload.get("created_at") or created_at),
                        diagnostics=dict(reservation_payload.get("diagnostics") or {}),
                        lines=reservation_lines,
                    )
                if expected_total_map:
                    post_total_row = conn.execute(
                        "SELECT COALESCE(SUM(quantity_delta), 0) AS total FROM sheet_vitrina_v1_ff_stock_operation_lines"
                    ).fetchone()
                    post_total = float(post_total_row["total"] or 0.0) if post_total_row is not None else 0.0
                    if abs(post_total - expected_total_map["after"]) > 1e-9:
                        raise ValueError(
                            "targeted_ff_stock_post_write_total_mismatch: "
                            + _canonical_json({"expected": expected_total_map["after"], "actual": post_total})
                        )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            row = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_ff_stock_operations WHERE operation_id = ?",
                (normalized_operation_id,),
            ).fetchone()
            payload = _ff_stock_operation_to_dict(row)
            payload["idempotent"] = False
            return payload

    def count_ff_stock_operations(
        self,
        *,
        include_technical_archive: bool = True,
        archive_cutoff_created_at: str = "",
    ) -> int:
        where_sql, params = _ff_stock_operations_archive_where(
            include_technical_archive=include_technical_archive,
            archive_cutoff_created_at=archive_cutoff_created_at,
        )
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM sheet_vitrina_v1_ff_stock_operations
                {where_sql}
                """,
                params,
            ).fetchone()
            return int(row["count"] or 0) if row is not None else 0

    def list_ff_stock_operations(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        include_technical_archive: bool = True,
        archive_cutoff_created_at: str = "",
    ) -> list[dict[str, Any]]:
        normalized_limit = max(1, min(int(limit or 100), 500))
        normalized_offset = max(0, int(offset or 0))
        where_sql, params = _ff_stock_operations_archive_where(
            include_technical_archive=include_technical_archive,
            archive_cutoff_created_at=archive_cutoff_created_at,
        )
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                f"""
                SELECT *
                FROM sheet_vitrina_v1_ff_stock_operations
                {where_sql}
                ORDER BY created_at DESC, operation_id DESC
                LIMIT ?
                OFFSET ?
                """,
                [*params, normalized_limit, normalized_offset],
            ).fetchall()
            return [_ff_stock_operation_to_dict(row) for row in rows]

    def load_ff_stock_operation(
        self,
        operation_id: str,
        *,
        include_file_blob: bool = False,
    ) -> dict[str, Any] | None:
        normalized_operation_id = str(operation_id or "").strip()
        if not normalized_operation_id:
            return None
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            select_columns = ["*"] if include_file_blob else [
                "operation_id",
                "operation_type",
                "source_type",
                "source_key",
                "source_object_id",
                "source_object_label",
                "created_at",
                "created_by",
                "sku_count",
                "total_quantity_delta",
                "total_quantity_abs",
                "warnings_json",
                "diagnostics_json",
                "source_filename",
                "source_content_type",
                "source_file_sha256",
                "source_file_blob IS NOT NULL AS file_available",
            ]
            row = conn.execute(
                f"""
                SELECT {", ".join(select_columns)}
                FROM sheet_vitrina_v1_ff_stock_operations
                WHERE operation_id = ?
                """,
                (normalized_operation_id,),
            ).fetchone()
            if row is None:
                return None
            payload = _ff_stock_operation_to_dict(row)
            line_rows = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_ff_stock_operation_lines
                WHERE operation_id = ?
                ORDER BY line_no ASC
                """,
                (normalized_operation_id,),
            ).fetchall()
            payload["lines"] = [_ff_stock_operation_line_to_dict(line_row) for line_row in line_rows]
            if include_file_blob:
                payload["source_file_bytes"] = bytes(row["source_file_blob"] or b"")
            return payload

    def list_ff_stock_balances(self) -> list[dict[str, Any]]:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT nm_id,
                       SUM(quantity_delta) AS balance
                FROM sheet_vitrina_v1_ff_stock_operation_lines
                GROUP BY nm_id
                ORDER BY nm_id ASC
                """
            ).fetchall()
            return [
                {
                    "nm_id": int(row["nm_id"]),
                    "balance": float(row["balance"] or 0.0),
                }
                for row in rows
            ]

    def list_ff_stock_reservations(self, *, supply_id: str = "") -> list[dict[str, Any]]:
        """Return current positive reservations reconstructed from append-only deltas."""
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            where = "WHERE operation.supply_id = ?" if str(supply_id or "").strip() else ""
            params: tuple[Any, ...] = (str(supply_id).strip(),) if where else ()
            rows = conn.execute(
                f"""
                SELECT operation.supply_id, line.nm_id,
                       SUM(line.quantity_delta) AS quantity,
                       MAX(operation.created_at) AS updated_at,
                       (
                         SELECT latest.operation_id
                         FROM sheet_vitrina_v1_ff_stock_reservation_operations AS latest
                         WHERE latest.supply_id = operation.supply_id
                         ORDER BY latest.created_at DESC, latest.operation_id DESC
                         LIMIT 1
                       ) AS latest_operation_id
                FROM sheet_vitrina_v1_ff_stock_reservation_operations AS operation
                JOIN sheet_vitrina_v1_ff_stock_reservation_lines AS line
                  ON line.operation_id = operation.operation_id
                {where}
                GROUP BY operation.supply_id, line.nm_id
                HAVING SUM(line.quantity_delta) > 0.000000001
                ORDER BY operation.supply_id, line.nm_id
                """,
                params,
            ).fetchall()
            return [
                {
                    "supply_id": str(row["supply_id"] or ""),
                    "nm_id": int(row["nm_id"]),
                    "quantity": float(row["quantity"] or 0.0),
                    "updated_at": str(row["updated_at"] or ""),
                    "latest_operation_id": str(row["latest_operation_id"] or ""),
                }
                for row in rows
            ]

    def list_current_wb_supply_cost_layers(
        self,
        *,
        supply_ids: Iterable[str],
    ) -> list[dict[str, Any]]:
        identifiers = sorted({str(item or "").strip() for item in supply_ids if str(item or "").strip()})
        if not identifiers:
            return []
        placeholders = ",".join("?" for _ in identifiers)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                f"""
                SELECT *
                FROM sheet_vitrina_v1_wb_supply_cost_layers
                WHERE is_current=1
                  AND (wb_supply_id IN ({placeholders}) OR cache_key IN ({placeholders}))
                ORDER BY wb_supply_id,nm_id
                """,
                tuple(identifiers) + tuple(identifiers),
            ).fetchall()
            return [
                {
                    **dict(row),
                    "component_status": _loads_json_object(row["component_status_json"]),
                }
                for row in rows
            ]

    def create_ff_stock_reservation_operation(
        self,
        *,
        operation_id: str,
        source_key: str,
        supply_id: str,
        supply_revision: str,
        operation_type: str,
        created_at: str,
        diagnostics: Mapping[str, Any] | None,
        lines: list[Mapping[str, Any]],
        expected_current: Mapping[int, float],
    ) -> dict[str, Any]:
        """Atomically recheck and append one idempotent reservation delta."""
        normalized_lines = [dict(item) for item in lines]
        if not normalized_lines:
            return {"idempotent": True, "operation_id": "", "no_op": True}
        _validate_timestamp(created_at, field_name="created_at")
        expected = {int(key): float(value) for key, value in expected_current.items()}
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_ff_stock_reservation_operations WHERE source_key=?",
                    (str(source_key),),
                ).fetchone()
                if existing is not None:
                    conn.rollback()
                    return {**dict(existing), "idempotent": True}
                actual = _ff_stock_reservation_quantities(conn, str(supply_id))
                actual = {nm_id: actual.get(nm_id, 0.0) for nm_id in set(expected) | set(actual)}
                expected_full = {nm_id: expected.get(nm_id, 0.0) for nm_id in set(expected) | set(actual)}
                if any(abs(actual[nm_id] - expected_full[nm_id]) > 1e-9 for nm_id in actual):
                    raise ValueError(
                        "ff_stock_reservation_changed: "
                        + _canonical_json({"expected": expected_full, "actual": actual})
                    )
                projected = dict(actual)
                for item in normalized_lines:
                    nm_id = int(item.get("nm_id") or 0)
                    if nm_id <= 0:
                        raise ValueError("reservation nm_id is required")
                    projected[nm_id] = projected.get(nm_id, 0.0) + float(item.get("quantity_delta") or 0.0)
                negative = {key: value for key, value in projected.items() if value < -1e-9}
                if negative:
                    raise ValueError("ff_stock_reservation_would_be_negative: " + _canonical_json(negative))
                _insert_ff_stock_reservation_operation(
                    conn,
                    operation_id=str(operation_id),
                    source_key=str(source_key),
                    supply_id=str(supply_id),
                    supply_revision=str(supply_revision),
                    operation_type=str(operation_type),
                    created_at=created_at,
                    diagnostics=dict(diagnostics or {}),
                    lines=normalized_lines,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return {
                "operation_id": str(operation_id),
                "source_key": str(source_key),
                "supply_id": str(supply_id),
                "supply_revision": str(supply_revision),
                "operation_type": str(operation_type),
                "idempotent": False,
            }

    def upsert_wb_supplies(
        self,
        *,
        rows: list[Mapping[str, Any]],
        warehouses: list[Mapping[str, Any]],
        synced_at: str,
        last_successful_sync_at: str,
        last_error: str,
        last_limit: int,
        last_offset: int,
        latest_synced_count: int,
        last_mode: str | None = None,
        latest_window_synced_at: str | None = None,
        latest_window_limit: int | None = None,
        latest_window_returned_count: int | None = None,
        may_have_more: bool | None = None,
        backfill_complete: bool | None = None,
        backfill_started_at: str | None = None,
        backfill_completed_at: str | None = None,
        highest_synced_offset: int | None = None,
        last_successful_offset: int | None = None,
    ) -> None:
        _validate_timestamp(str(synced_at or ""), field_name="synced_at")
        _validate_timestamp(str(last_successful_sync_at or ""), field_name="last_successful_sync_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.executemany(
                """
                INSERT INTO sheet_vitrina_v1_wb_supplies_warehouses(
                    warehouse_id,
                    warehouse_name,
                    raw_json,
                    synced_at
                )
                VALUES(?, ?, ?, ?)
                ON CONFLICT(warehouse_id) DO UPDATE SET
                    warehouse_name = excluded.warehouse_name,
                    raw_json = excluded.raw_json,
                    synced_at = excluded.synced_at
                """,
                [
                    (
                        str(_first_existing_value(item, "warehouse_id", "ID", "id") or "").strip(),
                        str(_first_existing_value(item, "warehouse_name", "name", "warehouseName") or "").strip(),
                        json.dumps(dict(item), ensure_ascii=False),
                        synced_at,
                    )
                    for item in warehouses
                    if str(_first_existing_value(item, "warehouse_id", "ID", "id") or "").strip()
                ],
            )
            conn.executemany(
                """
                INSERT INTO sheet_vitrina_v1_wb_supplies(
                    supply_id,
                    cache_key,
                    wb_supply_id,
                    preorder_id,
                    normalized_row_json,
                    raw_list_json,
                    raw_detail_json,
                    raw_goods_json,
                    raw_package_json,
                    raw_list_hash,
                    raw_detail_hash,
                    raw_goods_hash,
                    raw_package_hash,
                    warehouse_id,
                    status_id,
                    quantity_for_size_filter,
                    source_created_at,
                    supply_date,
                    fact_date,
                    updated_date,
                    synced_at,
                    last_list_synced_at,
                    last_enriched_at,
                    enrichment_status,
                    enrichment_error
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(supply_id) DO UPDATE SET
                    cache_key = excluded.cache_key,
                    wb_supply_id = excluded.wb_supply_id,
                    preorder_id = excluded.preorder_id,
                    normalized_row_json = excluded.normalized_row_json,
                    raw_list_json = excluded.raw_list_json,
                    raw_detail_json = COALESCE(excluded.raw_detail_json, raw_detail_json),
                    raw_goods_json = COALESCE(excluded.raw_goods_json, raw_goods_json),
                    raw_package_json = COALESCE(excluded.raw_package_json, raw_package_json),
                    raw_list_hash = excluded.raw_list_hash,
                    raw_detail_hash = COALESCE(excluded.raw_detail_hash, raw_detail_hash),
                    raw_goods_hash = COALESCE(excluded.raw_goods_hash, raw_goods_hash),
                    raw_package_hash = COALESCE(excluded.raw_package_hash, raw_package_hash),
                    warehouse_id = excluded.warehouse_id,
                    status_id = excluded.status_id,
                    quantity_for_size_filter = excluded.quantity_for_size_filter,
                    source_created_at = excluded.source_created_at,
                    supply_date = excluded.supply_date,
                    fact_date = excluded.fact_date,
                    updated_date = excluded.updated_date,
                    synced_at = excluded.synced_at,
                    last_list_synced_at = excluded.last_list_synced_at,
                    last_enriched_at = COALESCE(excluded.last_enriched_at, last_enriched_at),
                    enrichment_status = excluded.enrichment_status,
                    enrichment_error = excluded.enrichment_error
                """,
                [_wb_supply_row_values(row, synced_at) for row in rows],
            )
            _upsert_wb_supplies_sync_state(
                conn,
                last_synced_at=synced_at,
                last_successful_sync_at=last_successful_sync_at,
                last_error=last_error,
                last_limit=last_limit,
                last_offset=last_offset,
                latest_synced_count=latest_synced_count,
                last_mode=last_mode,
                latest_window_synced_at=latest_window_synced_at,
                latest_window_limit=latest_window_limit,
                latest_window_returned_count=latest_window_returned_count,
                may_have_more=may_have_more,
                backfill_complete=backfill_complete,
                backfill_started_at=backfill_started_at,
                backfill_completed_at=backfill_completed_at,
                highest_synced_offset=highest_synced_offset,
                last_successful_offset=last_successful_offset,
            )
            conn.commit()

    def save_wb_supply_rows(
        self,
        *,
        rows: list[Mapping[str, Any]],
        warehouses: list[Mapping[str, Any]],
        synced_at: str,
    ) -> None:
        _validate_timestamp(str(synced_at or ""), field_name="synced_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.executemany(
                """
                INSERT INTO sheet_vitrina_v1_wb_supplies_warehouses(
                    warehouse_id,
                    warehouse_name,
                    raw_json,
                    synced_at
                )
                VALUES(?, ?, ?, ?)
                ON CONFLICT(warehouse_id) DO UPDATE SET
                    warehouse_name = excluded.warehouse_name,
                    raw_json = excluded.raw_json,
                    synced_at = excluded.synced_at
                """,
                [
                    (
                        str(_first_existing_value(item, "warehouse_id", "ID", "id") or "").strip(),
                        str(_first_existing_value(item, "warehouse_name", "name", "warehouseName") or "").strip(),
                        json.dumps(dict(item), ensure_ascii=False),
                        synced_at,
                    )
                    for item in warehouses
                    if str(_first_existing_value(item, "warehouse_id", "ID", "id") or "").strip()
                ],
            )
            conn.executemany(
                """
                INSERT INTO sheet_vitrina_v1_wb_supplies(
                    supply_id,
                    cache_key,
                    wb_supply_id,
                    preorder_id,
                    normalized_row_json,
                    raw_list_json,
                    raw_detail_json,
                    raw_goods_json,
                    raw_package_json,
                    raw_list_hash,
                    raw_detail_hash,
                    raw_goods_hash,
                    raw_package_hash,
                    warehouse_id,
                    status_id,
                    quantity_for_size_filter,
                    source_created_at,
                    supply_date,
                    fact_date,
                    updated_date,
                    synced_at,
                    last_list_synced_at,
                    last_enriched_at,
                    enrichment_status,
                    enrichment_error
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(supply_id) DO UPDATE SET
                    cache_key = excluded.cache_key,
                    wb_supply_id = excluded.wb_supply_id,
                    preorder_id = excluded.preorder_id,
                    normalized_row_json = excluded.normalized_row_json,
                    raw_list_json = excluded.raw_list_json,
                    raw_detail_json = COALESCE(excluded.raw_detail_json, raw_detail_json),
                    raw_goods_json = COALESCE(excluded.raw_goods_json, raw_goods_json),
                    raw_package_json = COALESCE(excluded.raw_package_json, raw_package_json),
                    raw_list_hash = excluded.raw_list_hash,
                    raw_detail_hash = COALESCE(excluded.raw_detail_hash, raw_detail_hash),
                    raw_goods_hash = COALESCE(excluded.raw_goods_hash, raw_goods_hash),
                    raw_package_hash = COALESCE(excluded.raw_package_hash, raw_package_hash),
                    warehouse_id = excluded.warehouse_id,
                    status_id = excluded.status_id,
                    quantity_for_size_filter = excluded.quantity_for_size_filter,
                    source_created_at = excluded.source_created_at,
                    supply_date = excluded.supply_date,
                    fact_date = excluded.fact_date,
                    updated_date = excluded.updated_date,
                    synced_at = excluded.synced_at,
                    last_list_synced_at = excluded.last_list_synced_at,
                    last_enriched_at = COALESCE(excluded.last_enriched_at, last_enriched_at),
                    enrichment_status = excluded.enrichment_status,
                    enrichment_error = excluded.enrichment_error
                """,
                [_wb_supply_row_values(row, synced_at) for row in rows],
            )
            conn.commit()

    def save_wb_supplies_sync_state(
        self,
        *,
        last_synced_at: str,
        last_successful_sync_at: str | None,
        last_error: str,
        last_limit: int,
        last_offset: int,
        latest_synced_count: int,
        last_mode: str | None = None,
        latest_window_synced_at: str | None = None,
        latest_window_limit: int | None = None,
        latest_window_returned_count: int | None = None,
        may_have_more: bool | None = None,
        backfill_complete: bool | None = None,
        backfill_started_at: str | None = None,
        backfill_completed_at: str | None = None,
        highest_synced_offset: int | None = None,
        last_successful_offset: int | None = None,
    ) -> None:
        _validate_timestamp(str(last_synced_at or ""), field_name="last_synced_at")
        if last_successful_sync_at:
            _validate_timestamp(str(last_successful_sync_at), field_name="last_successful_sync_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            _upsert_wb_supplies_sync_state(
                conn,
                last_synced_at=last_synced_at,
                last_successful_sync_at=last_successful_sync_at,
                last_error=last_error,
                last_limit=last_limit,
                last_offset=last_offset,
                latest_synced_count=latest_synced_count,
                last_mode=last_mode,
                latest_window_synced_at=latest_window_synced_at,
                latest_window_limit=latest_window_limit,
                latest_window_returned_count=latest_window_returned_count,
                may_have_more=may_have_more,
                backfill_complete=backfill_complete,
                backfill_started_at=backfill_started_at,
                backfill_completed_at=backfill_completed_at,
                highest_synced_offset=highest_synced_offset,
                last_successful_offset=last_successful_offset,
            )
            conn.commit()

    def load_wb_supplies_sync_state(self) -> dict[str, Any]:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT last_synced_at,
                       last_successful_sync_at,
                       last_error,
                       last_limit,
                       last_offset,
                       latest_synced_count,
                       backfill_complete,
                       backfill_started_at,
                       backfill_completed_at,
                       highest_synced_offset,
                       last_successful_offset,
                       last_mode,
                       latest_window_synced_at,
                       latest_window_limit,
                       latest_window_returned_count,
                       may_have_more
                FROM sheet_vitrina_v1_wb_supplies_sync_state
                WHERE slot = 1
                """
            ).fetchone()
            if row is None:
                return {
                    "last_synced_at": "",
                    "last_successful_sync_at": "",
                    "last_error": "",
                    "last_limit": None,
                    "last_offset": None,
                    "latest_synced_count": None,
                    "backfill_complete": False,
                    "backfill_started_at": "",
                    "backfill_completed_at": "",
                    "highest_synced_offset": 0,
                    "last_successful_offset": None,
                    "last_mode": "",
                    "latest_window_synced_at": "",
                    "latest_window_limit": None,
                    "latest_window_returned_count": None,
                    "may_have_more": False,
                }
            return {
                "last_synced_at": row["last_synced_at"] or "",
                "last_successful_sync_at": row["last_successful_sync_at"] or "",
                "last_error": row["last_error"] or "",
                "last_limit": row["last_limit"],
                "last_offset": row["last_offset"],
                "latest_synced_count": row["latest_synced_count"],
                "backfill_complete": bool(row["backfill_complete"]),
                "backfill_started_at": row["backfill_started_at"] or "",
                "backfill_completed_at": row["backfill_completed_at"] or "",
                "highest_synced_offset": row["highest_synced_offset"] or 0,
                "last_successful_offset": row["last_successful_offset"],
                "last_mode": row["last_mode"] or "",
                "latest_window_synced_at": row["latest_window_synced_at"] or "",
                "latest_window_limit": row["latest_window_limit"],
                "latest_window_returned_count": row["latest_window_returned_count"],
                "may_have_more": bool(row["may_have_more"]),
            }

    def list_wb_supplies(self) -> list[dict[str, Any]]:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT normalized_row_json
                FROM sheet_vitrina_v1_wb_supplies
                ORDER BY COALESCE(updated_date, supply_date, source_created_at, synced_at) DESC,
                         supply_id DESC
                """
            ).fetchall()
            return [json.loads(row["normalized_row_json"]) for row in rows]

    def list_wb_supplies_warehouses(self) -> list[dict[str, Any]]:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT warehouse_id,
                       warehouse_name,
                       raw_json,
                       synced_at
                FROM sheet_vitrina_v1_wb_supplies_warehouses
                ORDER BY warehouse_name ASC, warehouse_id ASC
                """
            ).fetchall()
            result = []
            for row in rows:
                payload = json.loads(row["raw_json"])
                payload.setdefault("warehouse_id", row["warehouse_id"])
                payload.setdefault("warehouse_name", row["warehouse_name"])
                payload.setdefault("synced_at", row["synced_at"])
                result.append(payload)
            return result

    def upsert_wb_supply_transit_cost_enrichment(self, record: Mapping[str, Any]) -> dict[str, Any]:
        supply_id = str(record.get("supply_id") or "").strip()
        if not supply_id:
            raise ValueError("supply_id is required")
        now = str(record.get("updated_at") or record.get("fetched_at") or record.get("created_at") or "").strip()
        if now:
            _validate_timestamp(now, field_name="updated_at")
        created_at = str(record.get("created_at") or now).strip()
        if created_at:
            _validate_timestamp(created_at, field_name="created_at")
        amount = _optional_float(record.get("amount"))
        currency = str(record.get("currency") or "RUB").strip()[:16]
        status = str(record.get("status") or "failed").strip()[:40]
        source = str(record.get("source") or "seller_portal_browser").strip()[:80]
        evidence_type = str(record.get("evidence_type") or "network_json").strip()[:80]
        confidence = str(record.get("confidence") or "").strip()[:40]
        fetched_at = str(record.get("fetched_at") or "").strip()
        amount_label = str(record.get("amount_label") or "").strip()[:80]
        error = _safe_runtime_error(record.get("error"))
        endpoint = str(record.get("source_endpoint_path") or "").strip()[:260]
        is_transit = 1 if record.get("is_transit", True) else 0
        successful_fact = status == "success" and amount is not None and amount >= 0
        fact_material = {
            "supply_id": supply_id,
            "amount": amount,
            "currency": currency,
            "is_transit": bool(is_transit),
            "source": source,
            "evidence_type": evidence_type,
            "confidence": confidence,
            "source_endpoint_path": endpoint,
        }
        source_revision = "sha256:" + hashlib.sha256(
            json.dumps(
                fact_material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        attempt_material = {
            **fact_material,
            "fetched_at": fetched_at,
            "status": status,
            "error": error,
            "attempted_at": now,
        }
        attempt_fingerprint = "sha256:" + hashlib.sha256(
            json.dumps(
                attempt_material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        attempt_id = "wbtr_attempt_" + attempt_fingerprint.split(":", 1)[-1][:24]
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT OR IGNORE INTO sheet_vitrina_v1_wb_supply_transit_cost_enrichment_attempts(
                    attempt_id,supply_id,amount,currency,amount_label,is_transit,
                    source,evidence_type,confidence,fetched_at,status,error,
                    source_endpoint_path,attempted_at,attempt_fingerprint
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    attempt_id,
                    supply_id,
                    amount,
                    currency,
                    amount_label,
                    is_transit,
                    source,
                    evidence_type,
                    confidence,
                    fetched_at,
                    status,
                    error,
                    endpoint,
                    now,
                    attempt_fingerprint,
                ),
            )
            existing = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_supply_transit_cost_enrichment WHERE supply_id=?",
                (supply_id,),
            ).fetchone()
            existing_success = bool(
                existing is not None
                and str(existing["status"] or "") == "success"
                and existing["amount"] is not None
            )
            unchanged_success = bool(
                successful_fact
                and existing_success
                and str(existing["source_revision"] or "") == source_revision
            )
            if successful_fact and not unchanged_success:
                next_revision = int(existing["success_revision"] or 0) + 1 if existing is not None else 1
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_wb_supply_transit_cost_enrichment(
                        supply_id,amount,currency,amount_label,is_transit,source,
                        evidence_type,confidence,fetched_at,status,error,
                        source_endpoint_path,created_at,updated_at,
                        last_attempt_status,last_attempt_error,last_attempt_at,
                        source_revision,success_revision,recalculation_status,
                        recalculation_error,recalculation_updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(supply_id) DO UPDATE SET
                        amount=excluded.amount,currency=excluded.currency,
                        amount_label=excluded.amount_label,is_transit=excluded.is_transit,
                        source=excluded.source,evidence_type=excluded.evidence_type,
                        confidence=excluded.confidence,fetched_at=excluded.fetched_at,
                        status='success',error='',source_endpoint_path=excluded.source_endpoint_path,
                        updated_at=excluded.updated_at,last_attempt_status=excluded.last_attempt_status,
                        last_attempt_error=excluded.last_attempt_error,last_attempt_at=excluded.last_attempt_at,
                        source_revision=excluded.source_revision,success_revision=excluded.success_revision,
                        recalculation_status='awaiting_recalculation',recalculation_error='',
                        recalculation_updated_at=excluded.recalculation_updated_at
                    """,
                    (
                        supply_id,amount,currency,amount_label,is_transit,source,
                        evidence_type,confidence,fetched_at,"success","",endpoint,
                        created_at,now,status,error,now,source_revision,next_revision,
                        "awaiting_recalculation","",now,
                    ),
                )
            elif successful_fact or existing_success:
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_wb_supply_transit_cost_enrichment
                    SET last_attempt_status=?,last_attempt_error=?,last_attempt_at=?,updated_at=?
                    WHERE supply_id=?
                    """,
                    (status, error, now, now, supply_id),
                )
            else:
                conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_wb_supply_transit_cost_enrichment(
                    supply_id,
                    amount,
                    currency,
                    amount_label,
                    is_transit,
                    source,
                    evidence_type,
                    confidence,
                    fetched_at,
                    status,
                    error,
                    source_endpoint_path,
                    created_at,updated_at,last_attempt_status,last_attempt_error,
                    last_attempt_at,source_revision,success_revision,
                    recalculation_status,recalculation_error,recalculation_updated_at
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(supply_id) DO UPDATE SET
                    amount = NULL,
                    currency = excluded.currency,
                    amount_label = excluded.amount_label,
                    is_transit = excluded.is_transit,
                    source = excluded.source,
                    evidence_type = excluded.evidence_type,
                    confidence = excluded.confidence,
                    fetched_at = excluded.fetched_at,
                    status = excluded.status,
                    error = excluded.error,
                    source_endpoint_path = excluded.source_endpoint_path,
                    updated_at = excluded.updated_at,
                    last_attempt_status = excluded.last_attempt_status,
                    last_attempt_error = excluded.last_attempt_error,
                    last_attempt_at = excluded.last_attempt_at
                """,
                (
                    supply_id,
                    None,currency,amount_label,is_transit,source,evidence_type,
                    confidence,fetched_at,status,error,endpoint,created_at,now,
                    status,error,now,"",0,"","",now,
                ),
            )
            conn.commit()
        return self.load_wb_supply_transit_cost_enrichment(supply_id) or {}

    def update_wb_supply_transit_cost_recalculation_status(
        self,
        supply_id: str,
        *,
        status: str,
        error: str = "",
        updated_at: str,
    ) -> dict[str, Any]:
        normalized_id = str(supply_id or "").strip()
        if not normalized_id:
            raise ValueError("supply_id is required")
        _validate_timestamp(updated_at, field_name="updated_at")
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            changed = conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_supply_transit_cost_enrichment
                SET recalculation_status=?,recalculation_error=?,
                    recalculation_updated_at=?,updated_at=?
                WHERE supply_id=? AND status='success' AND amount IS NOT NULL
                """,
                (str(status)[:40], _safe_runtime_error(error), updated_at, updated_at, normalized_id),
            )
            if int(changed.rowcount or 0) != 1:
                raise ValueError("successful transit fact is missing")
            conn.commit()
        return self.load_wb_supply_transit_cost_enrichment(normalized_id) or {}

    def finalize_completed_wb_transit_cost_recalculations(
        self,
        *,
        completed_at: str,
    ) -> dict[str, Any]:
        """Finalize facts whose exact targeted queue completed downstream."""

        _validate_timestamp(completed_at, field_name="completed_at")
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            changed = conn.execute(
                """
                UPDATE sheet_vitrina_v1_wb_supply_transit_cost_enrichment AS fact
                SET recalculation_status='complete',recalculation_error='',
                    recalculation_updated_at=?,updated_at=?
                WHERE fact.status='success'
                  AND fact.amount IS NOT NULL
                  AND fact.recalculation_status IN (
                      'awaiting_recalculation','queued'
                  )
                  AND EXISTS (
                      SELECT 1
                      FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue AS queue
                      WHERE queue.stable_source_id=(
                          'wb_transit_cost:' || fact.supply_id
                      )
                        AND queue.source_revision=fact.source_revision
                        AND queue.status='complete'
                  )
                """,
                (completed_at, completed_at),
            )
            conn.commit()
        return {
            "status": "complete",
            "changed_supply_count": int(changed.rowcount or 0),
            "completed_at": completed_at,
        }

    def list_wb_supply_transit_cost_enrichment_attempts(
        self,
        *,
        supply_id: str | None = None,
    ) -> list[dict[str, Any]]:
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            if supply_id:
                rows = conn.execute(
                    """SELECT * FROM sheet_vitrina_v1_wb_supply_transit_cost_enrichment_attempts
                       WHERE supply_id=? ORDER BY attempted_at,attempt_id""",
                    (str(supply_id),),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM sheet_vitrina_v1_wb_supply_transit_cost_enrichment_attempts
                       ORDER BY attempted_at,attempt_id"""
                ).fetchall()
            return [dict(row) for row in rows]

    def list_wb_supply_transit_cost_enrichments(self) -> list[dict[str, Any]]:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_wb_supply_transit_cost_enrichment
                ORDER BY updated_at DESC, supply_id DESC
                """
            ).fetchall()
            return [_wb_supply_transit_cost_enrichment_to_dict(row) for row in rows]

    def load_wb_supply_transit_cost_enrichment(self, supply_id: str) -> dict[str, Any] | None:
        normalized_id = str(supply_id or "").strip()
        if not normalized_id:
            return None
        lookup_values = {
            normalized_id,
            normalized_id.removeprefix("supply:"),
            normalized_id.removeprefix("preorder:"),
        }
        placeholders = ",".join("?" for _ in lookup_values)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                f"""
                SELECT *
                FROM sheet_vitrina_v1_wb_supply_transit_cost_enrichment
                WHERE supply_id IN ({placeholders})
                LIMIT 1
                """,
                tuple(lookup_values),
            ).fetchone()
            return _wb_supply_transit_cost_enrichment_to_dict(row) if row else None

    def load_wb_supply_record(self, supply_id: str) -> dict[str, Any] | None:
        normalized_id = str(supply_id or "").strip()
        if not normalized_id:
            return None
        lookup_values = {
            normalized_id,
            f"supply:{normalized_id}",
            normalized_id.removeprefix("supply:"),
            normalized_id.removeprefix("preorder:"),
        }
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT normalized_row_json,
                       raw_list_json,
                       raw_detail_json,
                       raw_goods_json,
                       raw_package_json,
                       supply_id,
                       cache_key,
                       wb_supply_id,
                       preorder_id,
                       raw_list_hash,
                       raw_detail_hash,
                       raw_goods_hash,
                       raw_package_hash,
                       last_enriched_at,
                       enrichment_status,
                       enrichment_error
                FROM sheet_vitrina_v1_wb_supplies
                WHERE supply_id IN ({placeholders})
                   OR cache_key IN ({placeholders})
                   OR wb_supply_id IN ({placeholders})
                   OR preorder_id IN ({placeholders})
                LIMIT 1
                """.replace("{placeholders}", ",".join("?" for _ in lookup_values)),
                tuple(lookup_values) * 4,
            ).fetchone()
            if row is None:
                return None
            return _wb_supply_record_from_row(row)

    def load_wb_supply(self, supply_id: str) -> dict[str, Any] | None:
        record = self.load_wb_supply_record(supply_id)
        if record is None:
            return None
        payload = dict(record["normalized"])
        payload["raw"] = {
            "list": record.get("raw_list"),
            "detail": record.get("raw_detail"),
            "goods": record.get("raw_goods"),
            "package": record.get("raw_package"),
        }
        return payload

    def list_wb_supplies_cache_records(self) -> list[dict[str, Any]]:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT supply_id,
                       cache_key,
                       wb_supply_id,
                       preorder_id,
                       normalized_row_json,
                       raw_list_json,
                       raw_detail_json,
                       raw_goods_json,
                       raw_package_json,
                       raw_list_hash,
                       raw_detail_hash,
                       raw_goods_hash,
                       raw_package_hash,
                       last_enriched_at,
                       enrichment_status,
                       enrichment_error
                FROM sheet_vitrina_v1_wb_supplies
                """
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                result.append(_wb_supply_record_from_row(row))
            return result

    def delete_wb_supply_records(self, keys: list[str]) -> int:
        normalized_keys = sorted({str(key or "").strip() for key in keys if str(key or "").strip()})
        if not normalized_keys:
            return 0
        expanded_keys = sorted(
            {
                value
                for key in normalized_keys
                for value in (
                    key,
                    key.removeprefix("supply:"),
                    key.removeprefix("preorder:"),
                    f"supply:{key}" if not key.startswith(("supply:", "preorder:")) else key,
                    f"preorder:{key}" if not key.startswith(("supply:", "preorder:")) else key,
                )
                if value
            }
        )
        placeholders = ",".join("?" for _ in expanded_keys)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            cursor = conn.execute(
                f"""
                DELETE FROM sheet_vitrina_v1_wb_supplies
                WHERE supply_id IN ({placeholders})
                   OR cache_key IN ({placeholders})
                   OR wb_supply_id IN ({placeholders})
                   OR preorder_id IN ({placeholders})
                """,
                tuple(expanded_keys) * 4,
            )
            return int(cursor.rowcount or 0)

    def create_wb_supplies_sync_run(
        self,
        *,
        run_id: str,
        mode: str,
        status: str,
        phase: str,
        started_at: str,
        limit: int,
        offset: int,
        logs: list[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_wb_supplies_sync_runs(
                    run_id,
                    mode,
                    status,
                    phase,
                    started_at,
                    updated_at,
                    offset,
                    run_limit,
                    logs_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    mode,
                    status,
                    phase,
                    started_at,
                    started_at,
                    int(offset or 0),
                    int(limit or 0),
                    json.dumps(list(logs or []), ensure_ascii=False),
                ),
            )
            conn.commit()
        return self.load_wb_supplies_sync_run(run_id) or {}

    def update_wb_supplies_sync_run(self, run_id: str, **fields: Any) -> dict[str, Any]:
        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            raise ValueError("run_id is required")
        allowed = {
            "status",
            "phase",
            "updated_at",
            "completed_at",
            "offset",
            "run_limit",
            "pages_fetched",
            "raw_fetched",
            "upserted",
            "new_rows",
            "changed_rows",
            "unchanged_rows",
            "enriched",
            "failed_enrich",
            "may_have_more",
            "last_error",
            "logs_json",
        }
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            column = "logs_json" if key == "logs" else "run_limit" if key == "limit" else key
            if column not in allowed:
                continue
            assignments.append(f"{column} = ?")
            if column == "logs_json":
                values.append(json.dumps(value, ensure_ascii=False))
            elif column == "may_have_more":
                values.append(1 if value else 0)
            else:
                values.append(value)
        if not assignments:
            return self.load_wb_supplies_sync_run(normalized_run_id) or {}
        values.append(normalized_run_id)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                f"""
                UPDATE sheet_vitrina_v1_wb_supplies_sync_runs
                SET {', '.join(assignments)}
                WHERE run_id = ?
                """,
                values,
            )
            conn.commit()
        return self.load_wb_supplies_sync_run(normalized_run_id) or {}

    def load_wb_supplies_sync_run(self, run_id: str) -> dict[str, Any] | None:
        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            return None
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_wb_supplies_sync_runs
                WHERE run_id = ?
                """,
                (normalized_run_id,),
            ).fetchone()
            return _wb_supplies_sync_run_to_dict(row) if row else None

    def load_active_wb_supplies_sync_run(self) -> dict[str, Any] | None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_wb_supplies_sync_runs
                WHERE status IN ('queued', 'running')
                ORDER BY started_at DESC, updated_at DESC
                LIMIT 1
                """
            ).fetchone()
            return _wb_supplies_sync_run_to_dict(row) if row else None

    def create_wb_supply_transit_cost_enrichment_run(
        self,
        *,
        run_id: str,
        status: str,
        phase: str,
        started_at: str,
        candidate_count: int,
        logs: list[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        _validate_timestamp(started_at, field_name="started_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_wb_supply_transit_cost_enrichment_runs(
                    run_id,
                    status,
                    phase,
                    started_at,
                    updated_at,
                    candidate_count,
                    logs_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(run_id),
                    str(status),
                    str(phase),
                    started_at,
                    started_at,
                    int(candidate_count or 0),
                    json.dumps(list(logs or []), ensure_ascii=False),
                ),
            )
            conn.commit()
        return self.load_wb_supply_transit_cost_enrichment_run(run_id) or {}

    def update_wb_supply_transit_cost_enrichment_run(self, run_id: str, **fields: Any) -> dict[str, Any]:
        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            raise ValueError("run_id is required")
        allowed = {
            "status",
            "phase",
            "updated_at",
            "completed_at",
            "candidate_count",
            "processed_count",
            "success_count",
            "not_found_count",
            "failed_count",
            "session_expired_count",
            "auth_required_count",
            "route_unavailable_count",
            "collector_unavailable_count",
            "lock_busy_count",
            "last_error",
            "lock_status_json",
            "logs_json",
        }
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            column = "logs_json" if key == "logs" else "lock_status_json" if key == "lock_status" else key
            if column not in allowed:
                continue
            assignments.append(f"{column} = ?")
            if column in {"logs_json", "lock_status_json"}:
                values.append(json.dumps(value, ensure_ascii=False))
            elif column == "last_error":
                values.append(_safe_runtime_error(value))
            else:
                values.append(value)
        if not assignments:
            return self.load_wb_supply_transit_cost_enrichment_run(normalized_run_id) or {}
        values.append(normalized_run_id)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                f"""
                UPDATE sheet_vitrina_v1_wb_supply_transit_cost_enrichment_runs
                SET {', '.join(assignments)}
                WHERE run_id = ?
                """,
                values,
            )
            conn.commit()
        return self.load_wb_supply_transit_cost_enrichment_run(normalized_run_id) or {}

    def load_wb_supply_transit_cost_enrichment_run(self, run_id: str) -> dict[str, Any] | None:
        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            return None
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_wb_supply_transit_cost_enrichment_runs
                WHERE run_id = ?
                """,
                (normalized_run_id,),
            ).fetchone()
            return _wb_supply_transit_cost_run_to_dict(row) if row else None

    def load_active_wb_supply_transit_cost_enrichment_run(self) -> dict[str, Any] | None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_wb_supply_transit_cost_enrichment_runs
                WHERE status IN ('queued', 'running')
                ORDER BY started_at DESC, updated_at DESC
                LIMIT 1
                """
            ).fetchone()
            return _wb_supply_transit_cost_run_to_dict(row) if row else None

    def load_latest_wb_supply_transit_cost_enrichment_run(self) -> dict[str, Any] | None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_wb_supply_transit_cost_enrichment_runs
                ORDER BY started_at DESC, updated_at DESC
                LIMIT 1
                """
            ).fetchone()
            return _wb_supply_transit_cost_run_to_dict(row) if row else None

    def save_source_health_status(
        self,
        source_key: str,
        *,
        payload: Mapping[str, Any],
        checked_at: str,
    ) -> dict[str, Any]:
        normalized_key = str(source_key or "").strip()
        if not normalized_key:
            raise ValueError("source_key is required")
        _validate_timestamp(checked_at, field_name="checked_at")
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_source_health_status(
                    source_key,payload_json,checked_at
                ) VALUES(?,?,?)
                ON CONFLICT(source_key) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    checked_at=excluded.checked_at
                """,
                (
                    normalized_key,
                    json.dumps(dict(payload), ensure_ascii=False, sort_keys=True),
                    checked_at,
                ),
            )
            conn.commit()
        return self.load_source_health_status(normalized_key) or {}

    def load_source_health_status(self, source_key: str) -> dict[str, Any] | None:
        normalized_key = str(source_key or "").strip()
        if not normalized_key:
            return None
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT payload_json,checked_at
                FROM sheet_vitrina_v1_source_health_status
                WHERE source_key=?
                """,
                (normalized_key,),
            ).fetchone()
            if row is None:
                return None
            payload = _loads_json_object(row["payload_json"])
            payload.setdefault("source_key", normalized_key)
            payload["checked_at"] = str(row["checked_at"] or "")
            return payload

    def save_supplier_shipment_upload(
        self,
        *,
        upload_id: str,
        created_at: str,
        source_filename: str,
        content_type: str,
        source_file_sha256: str,
        source_file_path: str,
        parser_version: str,
        parsed_payload: Mapping[str, Any],
    ) -> None:
        _validate_timestamp(created_at, field_name="created_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_supplier_shipment_uploads(
                    upload_id,
                    created_at,
                    source_filename,
                    content_type,
                    source_file_sha256,
                    source_file_path,
                    parser_version,
                    parsed_payload_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    upload_id,
                    created_at,
                    source_filename,
                    content_type,
                    source_file_sha256,
                    source_file_path,
                    parser_version,
                    json.dumps(dict(parsed_payload), ensure_ascii=False),
                ),
            )
            conn.commit()

    def load_supplier_shipment_upload(self, upload_id: str) -> dict[str, Any] | None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT upload_id,
                       created_at,
                       source_filename,
                       content_type,
                       source_file_sha256,
                       source_file_path,
                       parser_version,
                       parsed_payload_json
                FROM sheet_vitrina_v1_supplier_shipment_uploads
                WHERE upload_id = ?
                """,
                (upload_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "upload_id": row["upload_id"],
                "created_at": row["created_at"],
                "source_filename": row["source_filename"],
                "content_type": row["content_type"],
                "source_file_sha256": row["source_file_sha256"],
                "source_file_path": row["source_file_path"],
                "parser_version": row["parser_version"],
                "parsed_payload": json.loads(row["parsed_payload_json"]),
            }

    def save_supplier_shipment(
        self,
        *,
        header: Mapping[str, Any],
        lines: list[Mapping[str, Any]],
    ) -> None:
        shipment_id = str(header.get("shipment_id") or "").strip()
        if not shipment_id:
            raise ValueError("supplier shipment_id is required")
        _validate_timestamp(str(header.get("created_at") or ""), field_name="created_at")
        _validate_timestamp(str(header.get("updated_at") or ""), field_name="updated_at")
        _validate_iso_date(str(header.get("shipment_date") or ""), field_name="shipment_date")
        _validate_optional_iso_date(header.get("actual_shipment_date"), field_name="actual_shipment_date")
        _validate_optional_iso_date(header.get("actual_ff_acceptance_date"), field_name="actual_ff_acceptance_date")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_supplier_shipments(
                    shipment_id,
                    created_at,
                    updated_at,
                    shipment_date,
                    actual_shipment_date,
                    actual_ff_acceptance_date,
                    historical_status_exception,
                    order_status,
                    target_facility_id,
                    target_facility_name,
                    expenses_complete,
                    invoice_no,
                    invoice_date,
                    contract_no,
                    contract_date,
                    supplier_name,
                    customer_name,
                    currency,
                    approx_yuan_rate,
                    product_qty_total,
                    product_amount_total,
                    extras_amount_total,
                    invoice_amount_total,
                    declared_invoice_total,
                    match_status,
                    source_filename,
                    source_file_sha256,
                    source_file_path,
                    invoice_document_id,
                    parser_version,
                    warnings_json,
                    errors_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(shipment_id) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    shipment_date = excluded.shipment_date,
                    actual_shipment_date = excluded.actual_shipment_date,
                    actual_ff_acceptance_date = excluded.actual_ff_acceptance_date,
                    historical_status_exception = excluded.historical_status_exception,
                    order_status = excluded.order_status,
                    target_facility_id = excluded.target_facility_id,
                    target_facility_name = excluded.target_facility_name,
                    expenses_complete = excluded.expenses_complete,
                    invoice_no = excluded.invoice_no,
                    invoice_date = excluded.invoice_date,
                    contract_no = excluded.contract_no,
                    contract_date = excluded.contract_date,
                    supplier_name = excluded.supplier_name,
                    customer_name = excluded.customer_name,
                    currency = excluded.currency,
                    approx_yuan_rate = excluded.approx_yuan_rate,
                    product_qty_total = excluded.product_qty_total,
                    product_amount_total = excluded.product_amount_total,
                    extras_amount_total = excluded.extras_amount_total,
                    invoice_amount_total = excluded.invoice_amount_total,
                    declared_invoice_total = excluded.declared_invoice_total,
                    match_status = excluded.match_status,
                    source_filename = excluded.source_filename,
                    source_file_sha256 = excluded.source_file_sha256,
                    source_file_path = excluded.source_file_path,
                    invoice_document_id = COALESCE(NULLIF(excluded.invoice_document_id, ''), invoice_document_id),
                    parser_version = excluded.parser_version,
                    warnings_json = excluded.warnings_json,
                    errors_json = excluded.errors_json
                """,
                (
                    shipment_id,
                    header.get("created_at"),
                    header.get("updated_at"),
                    header.get("shipment_date"),
                    header.get("actual_shipment_date") or None,
                    header.get("actual_ff_acceptance_date") or None,
                    header.get("historical_status_exception") or "",
                    header.get("order_status") or ORDER_STATUS_DEFAULT,
                    header.get("target_facility_id") or None,
                    header.get("target_facility_name") or None,
                    1 if bool(header.get("expenses_complete")) else 0,
                    header.get("invoice_no") or "",
                    header.get("invoice_date") or "",
                    header.get("contract_no") or "",
                    header.get("contract_date") or "",
                    header.get("supplier_name") or "",
                    header.get("customer_name") or "",
                    header.get("currency") or "",
                    header.get("approx_yuan_rate"),
                    header.get("product_qty_total"),
                    header.get("product_amount_total"),
                    header.get("extras_amount_total"),
                    header.get("invoice_amount_total"),
                    header.get("declared_invoice_total"),
                    header.get("match_status") or "",
                    header.get("source_filename") or "",
                    header.get("source_file_sha256") or "",
                    header.get("source_file_path") or "",
                    header.get("invoice_document_id") or "",
                    header.get("parser_version") or "",
                    json.dumps(list(header.get("warnings") or []), ensure_ascii=False),
                    json.dumps(list(header.get("errors") or []), ensure_ascii=False),
                ),
            )
            conn.execute(
                """
                DELETE FROM sheet_vitrina_v1_supplier_shipment_lines
                WHERE shipment_id = ?
                """,
                (shipment_id,),
            )
            conn.executemany(
                """
                INSERT INTO sheet_vitrina_v1_supplier_shipment_lines(
                    line_id,
                    shipment_id,
                    line_type,
                    sort_order,
                    source_no,
                    barcode,
                    product_type,
                    model_raw,
                    model_normalized,
                    match_key,
                    internal_sku,
                    internal_nm_id,
                    internal_name,
                    qty,
                    unit_price,
                    amount,
                    currency,
                    comment,
                    match_status,
                    manual_override,
                    invoice_price_yuan_snapshot,
                    reference_purchase_price_yuan_snapshot,
                    price_conformity_status,
                    price_conformity_checked_at,
                    price_conformity_check_mode,
                    price_conformity_reason,
                    price_conformity_actor,
                    price_conformity_context_json,
                    raw_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(item.get("line_id") or ""),
                        shipment_id,
                        str(item.get("line_type") or ""),
                        int(item.get("sort_order") or index),
                        str(item.get("source_no") or ""),
                        str(item.get("barcode") or ""),
                        str(item.get("product_type") or ""),
                        str(item.get("model_raw") or ""),
                        str(item.get("model_normalized") or ""),
                        str(item.get("match_key") or ""),
                        str(item.get("internal_sku") or ""),
                        item.get("internal_nm_id"),
                        str(item.get("internal_name") or ""),
                        item.get("qty"),
                        item.get("unit_price"),
                        item.get("amount"),
                        str(item.get("currency") or ""),
                        str(item.get("comment") or ""),
                        str(item.get("match_status") or ""),
                        1 if bool(item.get("manual_override")) else 0,
                        item.get("invoice_price_yuan_snapshot"),
                        item.get("reference_purchase_price_yuan_snapshot"),
                        str(item.get("price_conformity_status") or "not_checked"),
                        str(item.get("price_conformity_checked_at") or ""),
                        str(item.get("price_conformity_check_mode") or "not_checked"),
                        str(item.get("price_conformity_reason") or "not_checked"),
                        str(item.get("price_conformity_actor") or ""),
                        json.dumps(dict(item.get("price_conformity_context") or {}), ensure_ascii=False),
                        json.dumps(dict(item.get("raw") or {}), ensure_ascii=False),
                    )
                    for index, item in enumerate(lines, start=1)
                ],
            )
            conn.commit()

    def list_supplier_shipments(self) -> list[dict[str, Any]]:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT shipment_id,
                       created_at,
                       updated_at,
                       shipment_date,
                       actual_shipment_date,
                       actual_ff_acceptance_date,
                       historical_status_exception,
                       order_status,
                       target_facility_id,
                       target_facility_name,
                       expenses_complete,
                       invoice_no,
                       invoice_date,
                       contract_no,
                       contract_date,
                       supplier_name,
                       currency,
                       approx_yuan_rate,
                       cny_ledger_effective_rate,
                       cny_payment_currency_rub_cost,
                       cny_paid_amount,
                       cny_bank_fee_rub,
                       cny_calculation_status,
                       cny_calculation_error,
                       cny_calculated_at,
                       product_qty_total,
                       product_amount_total,
                       extras_amount_total,
                       invoice_amount_total,
                       match_status,
                       source_filename,
                       source_file_sha256,
                       source_file_path,
                       invoice_document_id,
                       archived_at,
                       archive_event_id,
                       archive_reason,
                       archive_actor
                FROM sheet_vitrina_v1_supplier_shipments
                WHERE archived_at IS NULL OR archived_at = ''
                ORDER BY shipment_date DESC, created_at DESC
                """
            ).fetchall()
            return [_supplier_shipment_row_to_dict(row) for row in rows]

    def list_active_ff_facilities(self) -> list[dict[str, Any]]:
        try:
            with _connect(self.db_path) as conn:
                _ensure_schema(conn)
                rows = conn.execute(
                    """
                    SELECT facility.facility_id,
                           facility.code,
                           facility.name,
                           COALESCE(profile.city, '') AS city
                    FROM sheet_vitrina_v1_ff_facilities AS facility
                    LEFT JOIN sheet_vitrina_v1_ff_facility_profiles AS profile
                      ON profile.facility_id = facility.facility_id
                    WHERE facility.active = 1
                    ORDER BY facility.code, facility.facility_id
                    """
                ).fetchall()
        except (StorageRegistryError, sqlite3.OperationalError):
            return []
        return [
            {
                "facility_id": str(row["facility_id"]),
                "code": str(row["code"] or ""),
                "name": str(row["name"] or ""),
                "city": str(row["city"] or ""),
                "active": True,
            }
            for row in rows
        ]

    def resolve_active_ff_facility(self, facility_id: str) -> dict[str, Any]:
        normalized = str(facility_id or "").strip()
        if not normalized:
            raise ValueError("Целевой фулфилмент обязателен")
        for facility in self.list_active_ff_facilities():
            if facility["facility_id"] == normalized:
                return facility
        raise ValueError("Выбранный целевой фулфилмент не существует или не active")

    def load_supplier_shipment(self, shipment_id: str) -> dict[str, Any] | None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            header_row = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_supplier_shipments
                WHERE shipment_id = ?
                """,
                (shipment_id,),
            ).fetchone()
            if header_row is None:
                return None
            line_rows = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_supplier_shipment_lines
                WHERE shipment_id = ?
                ORDER BY sort_order ASC, line_id ASC
                """,
                (shipment_id,),
            ).fetchall()
            return {
                "header": _supplier_shipment_header_to_dict(header_row),
                "lines": [_supplier_shipment_line_to_dict(row) for row in line_rows],
            }

    def update_supplier_shipment_order_status(
        self,
        *,
        shipment_id: str,
        order_status: str,
        updated_at: str,
    ) -> bool:
        shipment_id = str(shipment_id or "").strip()
        if not shipment_id:
            raise ValueError("supplier shipment_id is required")
        _validate_timestamp(str(updated_at or ""), field_name="updated_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            cursor = conn.execute(
                """
                UPDATE sheet_vitrina_v1_supplier_shipments
                SET order_status = ?,
                    updated_at = ?
                WHERE shipment_id = ?
                """,
                (str(order_status or ""), updated_at, shipment_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def update_supplier_shipment_expenses_complete(
        self,
        *,
        shipment_id: str,
        expenses_complete: bool,
        updated_at: str,
    ) -> bool:
        shipment_id = str(shipment_id or "").strip()
        if not shipment_id:
            raise ValueError("supplier shipment_id is required")
        _validate_timestamp(str(updated_at or ""), field_name="updated_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            cursor = conn.execute(
                """
                UPDATE sheet_vitrina_v1_supplier_shipments
                SET expenses_complete = ?,
                    updated_at = ?
                WHERE shipment_id = ?
                """,
                (1 if expenses_complete else 0, updated_at, shipment_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def delete_supplier_shipment(self, shipment_id: str) -> bool:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            cursor = conn.execute(
                """
                DELETE FROM sheet_vitrina_v1_supplier_shipments
                WHERE shipment_id = ?
                """,
                (shipment_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def archive_supplier_shipment(
        self,
        *,
        shipment_id: str,
        archived_at: str,
        actor: str = "operator",
        reason: str = "operator_delete_controlled_archive",
    ) -> dict[str, Any] | None:
        """Archive a supplier source without deleting posted evidence.

        The legacy DELETE method remains available only for old fixtures and
        migrations.  Operator-facing removal uses this append-only audit event
        and an inactive persisted status so warehouse replay can deterministically
        reverse the source while retaining its complete provenance.
        """

        shipment_id = str(shipment_id or "").strip()
        if not shipment_id:
            raise ValueError("supplier shipment_id is required")
        _validate_timestamp(str(archived_at or ""), field_name="archived_at")
        normalized_actor = str(actor or "operator").strip() or "operator"
        normalized_reason = str(reason or "operator_delete_controlled_archive").strip()
        if not normalized_reason:
            raise ValueError("supplier shipment archive reason is required")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            header_row = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id = ?",
                (shipment_id,),
            ).fetchone()
            if header_row is None:
                conn.rollback()
                return None
            existing_event_id = str(header_row["archive_event_id"] or "")
            if str(header_row["archived_at"] or ""):
                event = conn.execute(
                    """
                    SELECT * FROM sheet_vitrina_v1_supplier_shipment_archive_events
                    WHERE event_id = ?
                    """,
                    (existing_event_id,),
                ).fetchone()
                conn.commit()
                if event is not None:
                    payload = dict(event)
                    payload["idempotent"] = True
                    return payload
                return {
                    "event_id": existing_event_id,
                    "shipment_id": shipment_id,
                    "archived_at": str(header_row["archived_at"] or ""),
                    "source_fingerprint": "",
                    "idempotent": True,
                }
            line_rows = conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_supplier_shipment_lines
                WHERE shipment_id = ? ORDER BY sort_order, line_id
                """,
                (shipment_id,),
            ).fetchall()
            snapshot = {
                "header": dict(header_row),
                "lines": [dict(row) for row in line_rows],
            }
            snapshot_json = json.dumps(
                snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            source_fingerprint = "sha256:" + hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
            event_id = "supplier_archive_" + hashlib.sha256(
                f"{shipment_id}|{archived_at}|{source_fingerprint}".encode("utf-8")
            ).hexdigest()[:24]
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_supplier_shipment_archive_events(
                    event_id, shipment_id, archived_at, actor, reason,
                    source_fingerprint, source_snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    shipment_id,
                    archived_at,
                    normalized_actor,
                    normalized_reason,
                    source_fingerprint,
                    snapshot_json,
                ),
            )
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_supplier_shipments
                SET order_status = 'archived',
                    expenses_complete = 0,
                    updated_at = ?,
                    archived_at = ?,
                    archive_event_id = ?,
                    archive_reason = ?,
                    archive_actor = ?
                WHERE shipment_id = ?
                """,
                (
                    archived_at,
                    archived_at,
                    event_id,
                    normalized_reason,
                    normalized_actor,
                    shipment_id,
                ),
            )
            conn.commit()
            return {
                "event_id": event_id,
                "shipment_id": shipment_id,
                "archived_at": archived_at,
                "actor": normalized_actor,
                "reason": normalized_reason,
                "source_fingerprint": source_fingerprint,
                "idempotent": False,
            }

    def load_supplier_shipment_archive(self, shipment_id: str) -> dict[str, Any] | None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_supplier_shipment_archive_events
                WHERE shipment_id = ? ORDER BY archived_at DESC, event_id DESC LIMIT 1
                """,
                (str(shipment_id or "").strip(),),
            ).fetchone()
            if row is None:
                return None
            payload = dict(row)
            payload["source_snapshot"] = _loads_json_object(payload.pop("source_snapshot_json"))
            return payload

    def save_supplier_financial_document(
        self,
        *,
        document: Mapping[str, Any],
        expense_lines: list[Mapping[str, Any]],
        bank_operation_assignments: list[Mapping[str, Any]] | None = None,
        cny_documents: list[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        document_id = str(document.get("document_id") or "").strip()
        supplier_order_id = str(document.get("supplier_order_id") or document.get("order_id") or "").strip()
        if not document_id:
            raise ValueError("financial document_id is required")
        if not supplier_order_id:
            raise ValueError("financial supplier_order_id is required")
        uploaded_at = str(document.get("uploaded_at") or "").strip()
        updated_at = str(document.get("updated_at") or uploaded_at).strip()
        _validate_timestamp(uploaded_at, field_name="uploaded_at")
        _validate_timestamp(updated_at, field_name="updated_at")
        parse_status = str(document.get("parse_status") or FINANCIAL_DOCUMENT_PARSE_STATUS_PARSED)
        if parse_status not in FINANCIAL_DOCUMENT_PARSE_STATUSES:
            raise ValueError(f"unsupported financial document parse_status: {parse_status}")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            assignments = [
                dict(item) for item in (bank_operation_assignments or [])
            ]
            for assignment in assignments:
                operation_id = str(
                    assignment.get("semantic_operation_id") or ""
                ).strip()
                if not operation_id:
                    raise ValueError(
                        "bank statement semantic operation id is required"
                    )
                existing_assignment = conn.execute(
                    """
                    SELECT supplier_order_id,financial_document_id
                    FROM sheet_vitrina_v1_supplier_bank_operation_assignments
                    WHERE semantic_operation_id = ?
                    """,
                    (operation_id,),
                ).fetchone()
                if (
                    existing_assignment is not None
                    and (
                        str(existing_assignment["supplier_order_id"])
                        != supplier_order_id
                        or str(existing_assignment["financial_document_id"])
                        != document_id
                    )
                ):
                    raise ValueError(
                        "Банковская операция уже назначена другой поставке; "
                        "для переноса требуется отдельное подтверждение перепривязки."
                    )
            is_bank_statement = (
                str(document.get("document_type") or "")
                == "bank_fee_statement"
            )
            if assignments or is_bank_statement:
                source_sha256 = str(document.get("file_sha256") or "").strip()
                if len(source_sha256) != 64:
                    raise ValueError("exact bank statement source SHA-256 is required")
                source_normalized_parse = dict(
                    document.get("normalized_parse") or {}
                )
                source_normalized_parse.pop("statement_import", None)
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_supplier_financial_sources(
                        source_sha256,stored_file_path,original_filename,
                        file_content_type,parser_version,raw_parse_json,
                        normalized_parse_json,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(source_sha256) DO UPDATE SET
                        stored_file_path=excluded.stored_file_path,
                        parser_version=excluded.parser_version,
                        raw_parse_json=excluded.raw_parse_json,
                        normalized_parse_json=excluded.normalized_parse_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        source_sha256,
                        str(document.get("stored_file_path") or ""),
                        str(document.get("original_filename") or ""),
                        str(document.get("file_content_type") or ""),
                        str(document.get("parser_version") or ""),
                        json.dumps(
                            dict(document.get("raw_parse") or {}),
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            source_normalized_parse,
                            ensure_ascii=False,
                        ),
                        uploaded_at,
                        updated_at,
                    ),
                )
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_supplier_financial_documents(
                    document_id,
                    supplier_order_id,
                    document_type,
                    original_filename,
                    stored_file_path,
                    file_content_type,
                    file_sha256,
                    uploaded_at,
                    updated_at,
                    parse_status,
                    vendor,
                    document_number,
                    document_date,
                    currency,
                    total_amount,
                    total_amount_rub,
                    vat_rate,
                    vat_amount_rub,
                    due_date,
                    route,
                    contract_ref,
                    cbr_usd_rate_requested_date,
                    cbr_usd_rate_effective_date,
                    cbr_usd_rate_value,
                    rate_source,
                    rate_source_status,
                    raw_parse_json,
                    normalized_parse_json,
                    parser_version,
                    warnings_json,
                    errors_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    supplier_order_id = excluded.supplier_order_id,
                    document_type = excluded.document_type,
                    original_filename = excluded.original_filename,
                    stored_file_path = excluded.stored_file_path,
                    file_content_type = excluded.file_content_type,
                    file_sha256 = excluded.file_sha256,
                    updated_at = excluded.updated_at,
                    parse_status = excluded.parse_status,
                    vendor = excluded.vendor,
                    document_number = excluded.document_number,
                    document_date = excluded.document_date,
                    currency = excluded.currency,
                    total_amount = excluded.total_amount,
                    total_amount_rub = excluded.total_amount_rub,
                    vat_rate = excluded.vat_rate,
                    vat_amount_rub = excluded.vat_amount_rub,
                    due_date = excluded.due_date,
                    route = excluded.route,
                    contract_ref = excluded.contract_ref,
                    cbr_usd_rate_requested_date = excluded.cbr_usd_rate_requested_date,
                    cbr_usd_rate_effective_date = excluded.cbr_usd_rate_effective_date,
                    cbr_usd_rate_value = excluded.cbr_usd_rate_value,
                    rate_source = excluded.rate_source,
                    rate_source_status = excluded.rate_source_status,
                    raw_parse_json = excluded.raw_parse_json,
                    normalized_parse_json = excluded.normalized_parse_json,
                    parser_version = excluded.parser_version,
                    warnings_json = excluded.warnings_json,
                    errors_json = excluded.errors_json
                """,
                (
                    document_id,
                    supplier_order_id,
                    str(document.get("document_type") or ""),
                    str(document.get("original_filename") or ""),
                    str(document.get("stored_file_path") or ""),
                    str(document.get("file_content_type") or ""),
                    str(document.get("file_sha256") or ""),
                    uploaded_at,
                    updated_at,
                    parse_status,
                    str(document.get("vendor") or ""),
                    str(document.get("document_number") or ""),
                    str(document.get("document_date") or ""),
                    str(document.get("currency") or ""),
                    document.get("total_amount"),
                    document.get("total_amount_rub"),
                    document.get("vat_rate"),
                    document.get("vat_amount_rub"),
                    str(document.get("due_date") or ""),
                    str(document.get("route") or ""),
                    str(document.get("contract_ref") or ""),
                    str(document.get("cbr_usd_rate_requested_date") or ""),
                    str(document.get("cbr_usd_rate_effective_date") or ""),
                    document.get("cbr_usd_rate_value"),
                    str(document.get("rate_source") or ""),
                    str(document.get("rate_source_status") or ""),
                    json.dumps(dict(document.get("raw_parse") or {}), ensure_ascii=False),
                    json.dumps(dict(document.get("normalized_parse") or {}), ensure_ascii=False),
                    str(document.get("parser_version") or ""),
                    json.dumps(list(document.get("warnings") or []), ensure_ascii=False),
                    json.dumps(list(document.get("errors") or []), ensure_ascii=False),
                ),
            )
            conn.execute(
                """
                DELETE FROM sheet_vitrina_v1_supplier_financial_expense_lines
                WHERE financial_document_id = ?
                """,
                (document_id,),
            )
            conn.executemany(
                """
                INSERT INTO sheet_vitrina_v1_supplier_financial_expense_lines(
                    line_id,
                    financial_document_id,
                    supplier_order_id,
                    sort_order,
                    category,
                    stage,
                    description,
                    amount,
                    currency,
                    amount_rub,
                    vat_rate,
                    vat_amount_rub,
                    included_in_logistics_efficiency,
                    included_in_customs_total,
                    status,
                    confidence,
                    raw_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(line.get("line_id") or ""),
                        document_id,
                        supplier_order_id,
                        int(line.get("sort_order") or index),
                        str(line.get("category") or ""),
                        str(line.get("stage") or ""),
                        str(line.get("description") or ""),
                        line.get("amount"),
                        str(line.get("currency") or ""),
                        line.get("amount_rub"),
                        line.get("vat_rate"),
                        line.get("vat_amount_rub"),
                        1 if bool(line.get("included_in_logistics_efficiency")) else 0,
                        1 if bool(line.get("included_in_customs_total")) else 0,
                        str(line.get("status") or ""),
                        line.get("confidence"),
                        json.dumps(dict(line.get("raw") or {}), ensure_ascii=False),
                    )
                    for index, line in enumerate(expense_lines, start=1)
                ],
            )
            conn.executemany(
                """
                INSERT INTO sheet_vitrina_v1_supplier_bank_operation_assignments(
                    semantic_operation_id,source_sha256,logical_fee_id,
                    supplier_order_id,financial_document_id,assigned_at,raw_json
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(semantic_operation_id) DO UPDATE SET
                    raw_json=excluded.raw_json,
                    assigned_at=excluded.assigned_at
                WHERE supplier_order_id=excluded.supplier_order_id
                  AND financial_document_id=excluded.financial_document_id
                """,
                [
                    (
                        str(item.get("semantic_operation_id") or ""),
                        str(document.get("file_sha256") or ""),
                        str(item.get("logical_fee_id") or ""),
                        supplier_order_id,
                        document_id,
                        updated_at,
                        json.dumps(dict(item.get("raw") or item), ensure_ascii=False),
                    )
                    for item in assignments
                ],
            )
            for cny_document in cny_documents or []:
                natural_key = str(
                    cny_document.get("natural_key") or ""
                ).strip()
                if not natural_key:
                    raise ValueError(
                        "atomic CNY bank fee natural_key is required"
                    )
                existing_cny = conn.execute(
                    """
                    SELECT document_id,source_order_id,
                           linked_financial_document_id
                    FROM sheet_vitrina_v1_cny_documents
                    WHERE natural_key=?
                    """,
                    (natural_key,),
                ).fetchone()
                if existing_cny is not None:
                    if (
                        str(existing_cny["source_order_id"])
                        != supplier_order_id
                        or str(existing_cny["linked_financial_document_id"])
                        != document_id
                    ):
                        raise ValueError(
                            "CNY-операция комиссии уже связана с другой "
                            "поставкой или выпиской."
                        )
                    continue
                _save_cny_document_in_connection(conn, cny_document)
            conn.commit()
        loaded = self.load_supplier_financial_document(
            supplier_order_id=supplier_order_id,
            document_id=document_id,
        )
        if loaded is None:
            raise ValueError(f"financial document was not saved: {document_id}")
        return loaded

    def list_supplier_bank_operation_assignments(self) -> list[dict[str, Any]]:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT semantic_operation_id,source_sha256,logical_fee_id,
                       supplier_order_id,financial_document_id,assigned_at,
                       raw_json
                FROM sheet_vitrina_v1_supplier_bank_operation_assignments
                ORDER BY semantic_operation_id
                """
            ).fetchall()
            return [
                {
                    **dict(row),
                    "raw": json.loads(str(row["raw_json"] or "{}")),
                }
                for row in rows
            ]

    def list_supplier_payment_fee_confirmations(
        self,
        supplier_order_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            normalized_order_id = str(supplier_order_id or "").strip()
            rows = conn.execute(
                """
                SELECT confirmation_id,supplier_order_id,payment_document_id,
                       payment_fingerprint,confirmation_type,status,reason,
                       actor,created_at,updated_at
                FROM sheet_vitrina_v1_supplier_payment_fee_confirmations
                WHERE (? = '' OR supplier_order_id = ?)
                ORDER BY supplier_order_id,payment_document_id,confirmation_id
                """,
                (normalized_order_id, normalized_order_id),
            ).fetchall()
            return [dict(row) for row in rows]

    def save_supplier_payment_zero_fee_confirmation(
        self,
        *,
        confirmation_id: str,
        supplier_order_id: str,
        payment_document_id: str,
        payment_fingerprint: str,
        reason: str,
        actor: str,
        confirmed_at: str,
    ) -> dict[str, Any]:
        values = {
            "confirmation_id": str(confirmation_id or "").strip(),
            "supplier_order_id": str(supplier_order_id or "").strip(),
            "payment_document_id": str(payment_document_id or "").strip(),
            "payment_fingerprint": str(payment_fingerprint or "").strip(),
            "reason": str(reason or "").strip(),
            "actor": str(actor or "").strip(),
            "confirmed_at": str(confirmed_at or "").strip(),
        }
        missing = [key for key, value in values.items() if not value]
        if missing:
            raise ValueError(
                "zero-fee confirmation fields are required: " + ",".join(missing)
            )
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_supplier_payment_fee_confirmations(
                    confirmation_id,supplier_order_id,payment_document_id,
                    payment_fingerprint,confirmation_type,status,reason,actor,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(supplier_order_id,payment_document_id,confirmation_type)
                DO UPDATE SET
                    payment_fingerprint=excluded.payment_fingerprint,
                    status='active',
                    reason=excluded.reason,
                    actor=excluded.actor,
                    updated_at=excluded.updated_at
                """,
                (
                    values["confirmation_id"],
                    values["supplier_order_id"],
                    values["payment_document_id"],
                    values["payment_fingerprint"],
                    "zero_fee",
                    "active",
                    values["reason"],
                    values["actor"],
                    values["confirmed_at"],
                    values["confirmed_at"],
                ),
            )
            conn.commit()
        confirmation = next(
            (
                item
                for item in self.list_supplier_payment_fee_confirmations(
                    values["supplier_order_id"]
                )
                if str(item.get("payment_document_id") or "")
                == values["payment_document_id"]
                and str(item.get("confirmation_type") or "") == "zero_fee"
            ),
            None,
        )
        if confirmation is None:
            raise ValueError("zero-fee confirmation readback failed")
        return confirmation

    def load_supplier_financial_source(
        self,
        source_sha256: str,
    ) -> dict[str, Any] | None:
        normalized_sha256 = str(source_sha256 or "").strip()
        if len(normalized_sha256) != 64:
            return None
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_supplier_financial_sources
                WHERE source_sha256=?
                """,
                (normalized_sha256,),
            ).fetchone()
        if row is None:
            return None
        payload = dict(row)
        payload["raw_parse"] = _loads_json_object(
            str(payload.pop("raw_parse_json") or "{}")
        )
        payload["normalized_parse"] = _loads_json_object(
            str(payload.pop("normalized_parse_json") or "{}")
        )
        return payload

    def migrate_supplier_financial_source_paths(
        self,
        *,
        contract_version: str,
        applied_at: str,
        manifest_sha256: str,
        source_paths: Mapping[str, str],
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        _validate_timestamp(applied_at, field_name="applied_at")
        normalized_contract = str(contract_version or "").strip()
        if not normalized_contract:
            raise ValueError("financial source migration contract is required")
        normalized_paths = {
            str(source_sha256): str(path)
            for source_sha256, path in source_paths.items()
            if len(str(source_sha256)) == 64 and str(path).strip()
        }
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            for source_sha256, stored_file_path in normalized_paths.items():
                cursor = conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_supplier_financial_documents
                    SET stored_file_path=?
                    WHERE document_type='bank_fee_statement'
                      AND file_sha256=?
                    """,
                    (stored_file_path, source_sha256),
                )
                if cursor.rowcount <= 0:
                    conn.rollback()
                    raise ValueError(
                        f"bank statement source disappeared: {source_sha256}"
                    )
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_supplier_financial_sources
                    SET stored_file_path=?,updated_at=?
                    WHERE source_sha256=?
                    """,
                    (stored_file_path, applied_at, source_sha256),
                )
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_supplier_financial_source_migrations(
                    contract_version,applied_at,manifest_sha256,result_json
                ) VALUES(?,?,?,?)
                ON CONFLICT(contract_version) DO UPDATE SET
                    applied_at=excluded.applied_at,
                    manifest_sha256=excluded.manifest_sha256,
                    result_json=excluded.result_json
                """,
                (
                    normalized_contract,
                    applied_at,
                    str(manifest_sha256 or ""),
                    json.dumps(
                        dict(result),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )
            conn.commit()
            if normalized_paths:
                rows = conn.execute(
                    """
                    SELECT file_sha256,COUNT(*) AS document_count,
                           COUNT(DISTINCT stored_file_path) AS path_count,
                           MIN(stored_file_path) AS stored_file_path
                    FROM sheet_vitrina_v1_supplier_financial_documents
                    WHERE document_type='bank_fee_statement'
                      AND file_sha256 IN (
                    """
                    + ",".join("?" for _ in normalized_paths)
                    + """
                      )
                    GROUP BY file_sha256
                    ORDER BY file_sha256
                    """,
                    tuple(normalized_paths),
                ).fetchall()
            else:
                rows = []
        return {
            "contract_version": normalized_contract,
            "groups": [dict(row) for row in rows],
            "readback_confirmed": all(
                int(row["path_count"]) == 1
                and str(row["stored_file_path"])
                == normalized_paths.get(str(row["file_sha256"]))
                for row in rows
            )
            and len(rows) == len(normalized_paths),
        }

    def restore_supplier_financial_source_paths(
        self,
        *,
        restored_at: str,
        document_paths: Mapping[str, str],
    ) -> int:
        _validate_timestamp(restored_at, field_name="restored_at")
        normalized = {
            str(document_id): str(path)
            for document_id, path in document_paths.items()
            if str(document_id).strip() and str(path).strip()
        }
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            affected = 0
            for document_id, stored_file_path in normalized.items():
                cursor = conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_supplier_financial_documents
                    SET stored_file_path=?
                    WHERE document_id=? AND document_type='bank_fee_statement'
                    """,
                    (stored_file_path, document_id),
                )
                affected += max(0, int(cursor.rowcount))
            conn.execute(
                """
                DELETE FROM sheet_vitrina_v1_supplier_financial_source_migrations
                WHERE contract_version='supplier_financial_source_migration_v1'
                """
            )
            conn.commit()
        return affected

    def save_supplier_confirmation_preview(
        self,
        *,
        token: str,
        confirmation_type: str,
        supplier_order_id: str,
        subject_id: str,
        target_revision: str,
        source_sha256: str,
        payload: Mapping[str, Any],
        created_at: str,
        expires_at: str,
    ) -> dict[str, Any]:
        normalized_token = str(token or "").strip()
        if not normalized_token:
            raise ValueError("confirmation token is required")
        _validate_timestamp(created_at, field_name="created_at")
        _validate_timestamp(expires_at, field_name="expires_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect_supplier_confirmation_store(self.runtime_dir) as conn:
            _cleanup_expired_supplier_confirmation_previews(
                conn,
                runtime_dir=self.runtime_dir,
                now=created_at,
            )
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_supplier_confirmation_previews(
                    token,confirmation_type,supplier_order_id,subject_id,
                    target_revision,source_sha256,payload_json,created_at,expires_at,
                    consumed_at,result_json
                ) VALUES(?,?,?,?,?,?,?,?,?,NULL,'{}')
                """,
                (
                    normalized_token,
                    str(confirmation_type or "").strip(),
                    str(supplier_order_id or "").strip(),
                    str(subject_id or "").strip(),
                    str(target_revision or "").strip(),
                    str(source_sha256 or "").strip(),
                    json.dumps(dict(payload or {}), ensure_ascii=False, sort_keys=True),
                    created_at,
                    expires_at,
                ),
            )
            conn.commit()
        loaded = self.load_supplier_confirmation_preview(normalized_token)
        if loaded is None:
            raise ValueError("confirmation preview was not saved")
        return loaded

    def load_supplier_confirmation_preview(self, token: str) -> dict[str, Any] | None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect_supplier_confirmation_store(self.runtime_dir) as conn:
            row = conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_supplier_confirmation_previews
                WHERE token=?
                """,
                (str(token or "").strip(),),
            ).fetchone()
        if row is None:
            # Transitional read-only fallback for previews created by the
            # preceding release. New previews are never written to the main DB.
            with _connect(self.db_path) as conn:
                _ensure_schema(conn)
                row = conn.execute(
                    """
                    SELECT * FROM sheet_vitrina_v1_supplier_confirmation_previews
                    WHERE token=?
                    """,
                    (str(token or "").strip(),),
                ).fetchone()
        if row is None:
            return None
        payload = dict(row)
        payload["payload"] = _loads_json_object(payload.pop("payload_json"))
        payload["result"] = _loads_json_object(payload.pop("result_json"))
        return payload

    def complete_supplier_confirmation_preview(
        self,
        *,
        token: str,
        consumed_at: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        _validate_timestamp(consumed_at, field_name="consumed_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect_supplier_confirmation_store(self.runtime_dir) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT consumed_at,result_json,payload_json
                FROM sheet_vitrina_v1_supplier_confirmation_previews
                WHERE token=?
                """,
                (str(token or "").strip(),),
            ).fetchone()
            if row is None:
                conn.rollback()
                # Old previews are bounded to their short TTL. Preserve their
                # compatibility without routing any new preview write here.
                with _connect(self.db_path) as legacy:
                    _ensure_schema(legacy)
                    legacy_row = legacy.execute(
                        """
                        SELECT consumed_at,result_json,payload_json
                        FROM sheet_vitrina_v1_supplier_confirmation_previews
                        WHERE token=?
                        """,
                        (str(token or "").strip(),),
                    ).fetchone()
                    if legacy_row is None:
                        raise ValueError("confirmation token is invalid")
                    if not str(legacy_row["consumed_at"] or "").strip():
                        legacy.execute(
                            """
                            UPDATE sheet_vitrina_v1_supplier_confirmation_previews
                            SET consumed_at=?,result_json=?
                            WHERE token=? AND consumed_at IS NULL
                            """,
                            (
                                consumed_at,
                                json.dumps(
                                    dict(result or {}),
                                    ensure_ascii=False,
                                    sort_keys=True,
                                ),
                                str(token or "").strip(),
                            ),
                        )
                        legacy.commit()
                    _finalize_supplier_confirmation_staging(
                        legacy,
                        runtime_dir=self.runtime_dir,
                        token=str(token or "").strip(),
                        payload_json=str(legacy_row["payload_json"] or "{}"),
                    )
                loaded = self.load_supplier_confirmation_preview(token)
                if loaded is None:
                    raise ValueError("confirmation token is invalid")
                return loaded
            if str(row["consumed_at"] or "").strip():
                conn.rollback()
                loaded = self.load_supplier_confirmation_preview(token)
                if loaded is None:
                    raise ValueError("confirmation token is invalid")
                return loaded
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_supplier_confirmation_previews
                SET consumed_at=?,result_json=?
                WHERE token=? AND consumed_at IS NULL
                """,
                (
                    consumed_at,
                    json.dumps(dict(result or {}), ensure_ascii=False, sort_keys=True),
                    str(token or "").strip(),
                ),
            )
            conn.commit()
            _finalize_supplier_confirmation_staging(
                conn,
                runtime_dir=self.runtime_dir,
                token=str(token or "").strip(),
                payload_json=str(row["payload_json"] or "{}"),
            )
        loaded = self.load_supplier_confirmation_preview(token)
        if loaded is None:
            raise ValueError("confirmation token is invalid")
        return loaded

    def cleanup_expired_supplier_confirmation_previews(
        self,
        *,
        now: str,
    ) -> int:
        _validate_timestamp(now, field_name="now")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect_supplier_confirmation_store(self.runtime_dir) as conn:
            return _cleanup_expired_supplier_confirmation_previews(
                conn,
                runtime_dir=self.runtime_dir,
                now=now,
            )

    def list_supplier_financial_documents(self, supplier_order_id: str) -> list[dict[str, Any]]:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_supplier_financial_documents
                WHERE supplier_order_id = ?
                ORDER BY document_date DESC, uploaded_at DESC, document_id ASC
                """,
                (str(supplier_order_id or "").strip(),),
            ).fetchall()
            return [_supplier_financial_document_to_dict(row) for row in rows]

    def list_supplier_financial_expense_lines(self, supplier_order_id: str) -> list[dict[str, Any]]:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_supplier_financial_expense_lines
                WHERE supplier_order_id = ?
                ORDER BY financial_document_id ASC, sort_order ASC, line_id ASC
                """,
                (str(supplier_order_id or "").strip(),),
            ).fetchall()
            return [_supplier_financial_expense_line_to_dict(row) for row in rows]

    def load_supplier_financial_document(
        self,
        *,
        supplier_order_id: str,
        document_id: str,
    ) -> dict[str, Any] | None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_supplier_financial_documents
                WHERE supplier_order_id = ?
                  AND document_id = ?
                """,
                (str(supplier_order_id or "").strip(), str(document_id or "").strip()),
            ).fetchone()
            if row is None:
                return None
            lines = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_supplier_financial_expense_lines
                WHERE supplier_order_id = ?
                  AND financial_document_id = ?
                ORDER BY sort_order ASC, line_id ASC
                """,
                (str(supplier_order_id or "").strip(), str(document_id or "").strip()),
            ).fetchall()
            payload = _supplier_financial_document_to_dict(row)
            payload["expense_lines"] = [_supplier_financial_expense_line_to_dict(line) for line in lines]
            return payload

    def delete_supplier_financial_document(
        self,
        *,
        supplier_order_id: str,
        document_id: str,
    ) -> dict[str, Any] | None:
        supplier_order_id = str(supplier_order_id or "").strip()
        document_id = str(document_id or "").strip()
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_supplier_financial_documents
                WHERE supplier_order_id = ?
                  AND document_id = ?
                """,
                (supplier_order_id, document_id),
            ).fetchone()
            if row is None:
                return None
            document = _supplier_financial_document_to_dict(row)
            conn.execute(
                """
                DELETE FROM sheet_vitrina_v1_supplier_financial_expense_lines
                WHERE supplier_order_id = ?
                  AND financial_document_id = ?
                """,
                (supplier_order_id, document_id),
            )
            cursor = conn.execute(
                """
                DELETE FROM sheet_vitrina_v1_supplier_financial_documents
                WHERE supplier_order_id = ?
                  AND document_id = ?
                """,
                (supplier_order_id, document_id),
            )
            conn.commit()
            if cursor.rowcount <= 0:
                return None
            return document

    def update_supplier_financial_document_status(
        self,
        *,
        supplier_order_id: str,
        document_id: str,
        parse_status: str,
        updated_at: str,
    ) -> dict[str, Any]:
        if parse_status not in FINANCIAL_DOCUMENT_PARSE_STATUSES:
            raise ValueError(f"unsupported financial document parse_status: {parse_status}")
        _validate_timestamp(str(updated_at or ""), field_name="updated_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            cursor = conn.execute(
                """
                UPDATE sheet_vitrina_v1_supplier_financial_documents
                SET parse_status = ?,
                    updated_at = ?
                WHERE supplier_order_id = ?
                  AND document_id = ?
                """,
                (
                    str(parse_status or ""),
                    updated_at,
                    str(supplier_order_id or "").strip(),
                    str(document_id or "").strip(),
                ),
            )
            conn.commit()
            if cursor.rowcount <= 0:
                raise ValueError(f"financial document not found: {document_id}")
        loaded = self.load_supplier_financial_document(
            supplier_order_id=supplier_order_id,
            document_id=document_id,
        )
        if loaded is None:
            raise ValueError(f"financial document not found: {document_id}")
        return loaded

    def list_supplier_financial_documents_all(self) -> list[dict[str, Any]]:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_supplier_financial_documents
                ORDER BY supplier_order_id ASC, document_date ASC, uploaded_at ASC, document_id ASC
                """
            ).fetchall()
            return [_supplier_financial_document_to_dict(row) for row in rows]

    def save_cny_document(self, document: Mapping[str, Any]) -> dict[str, Any]:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            document_id = _save_cny_document_in_connection(conn, document)
            conn.commit()
        loaded = self.load_cny_document(document_id)
        if loaded is None:
            raise ValueError(f"CNY document was not saved: {document_id}")
        return loaded

    def update_cny_document_context(
        self,
        *,
        document_id: str,
        source_order_id: str,
        context_order_id: str,
        updated_at: str,
    ) -> dict[str, Any]:
        _validate_timestamp(updated_at, field_name="updated_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE sheet_vitrina_v1_cny_documents
                SET source_order_id = ?,
                    context_order_id = ?,
                    updated_at = ?
                WHERE document_id = ?
                """,
                (
                    str(source_order_id or "").strip(),
                    str(context_order_id or "").strip(),
                    updated_at,
                    str(document_id or "").strip(),
                ),
            )
            conn.commit()
            if cursor.rowcount <= 0:
                raise ValueError(f"CNY document not found: {document_id}")
        loaded = self.load_cny_document(document_id)
        if loaded is None:
            raise ValueError(f"CNY document not found: {document_id}")
        return loaded

    def load_cny_document(self, document_id: str) -> dict[str, Any] | None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_cny_documents
                WHERE document_id = ?
                """,
                (str(document_id or "").strip(),),
            ).fetchone()
            return _cny_document_to_dict(row) if row else None

    def load_cny_document_by_natural_key(self, natural_key: str) -> dict[str, Any] | None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_cny_documents
                WHERE natural_key = ?
                """,
                (str(natural_key or "").strip(),),
            ).fetchone()
            return _cny_document_to_dict(row) if row else None

    def delete_cny_document(self, document_id: str) -> dict[str, Any]:
        normalized_id = str(document_id or "").strip()
        if not normalized_id:
            raise ValueError("CNY document_id is required")
        existing = self.load_cny_document(normalized_id)
        if existing is None:
            raise ValueError(f"CNY document not found: {normalized_id}")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                DELETE FROM sheet_vitrina_v1_cny_documents
                WHERE document_id = ?
                """,
                (normalized_id,),
            )
            conn.commit()
        return existing

    def list_cny_documents(self) -> list[dict[str, Any]]:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_cny_documents
                ORDER BY COALESCE(NULLIF(operation_datetime, ''), NULLIF(operation_date, ''), uploaded_at) ASC,
                         uploaded_at ASC,
                         document_id ASC
                """
            ).fetchall()
            return [_cny_document_to_dict(row) for row in rows]

    def replace_cny_ledger_operations(self, operations: list[Mapping[str, Any]]) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute("DELETE FROM sheet_vitrina_v1_cny_ledger_operations")
            conn.executemany(
                """
                INSERT INTO sheet_vitrina_v1_cny_ledger_operations(
                    operation_id,
                    operation_type,
                    source_document_id,
                    source_order_id,
                    operation_date,
                    operation_datetime,
                    sequence_key,
                    cny_delta,
                    rub_value_delta,
                    effective_rate_before,
                    balance_cny_after,
                    balance_rub_value_after,
                    average_rate_after,
                    status,
                    error_reason,
                    created_at,
                    updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(item.get("operation_id") or ""),
                        _validated_choice(
                            str(item.get("operation_type") or ""),
                            CNY_LEDGER_OPERATION_TYPES,
                            field_name="CNY ledger operation_type",
                        ),
                        str(item.get("source_document_id") or ""),
                        str(item.get("source_order_id") or ""),
                        str(item.get("operation_date") or ""),
                        str(item.get("operation_datetime") or ""),
                        str(item.get("sequence_key") or ""),
                        str(item.get("cny_delta") or ""),
                        str(item.get("rub_value_delta") or ""),
                        str(item.get("effective_rate_before") or ""),
                        str(item.get("balance_cny_after") or ""),
                        str(item.get("balance_rub_value_after") or ""),
                        str(item.get("average_rate_after") or ""),
                        _validated_choice(
                            str(item.get("status") or CNY_LEDGER_OPERATION_STATUS_POSTED),
                            CNY_LEDGER_OPERATION_STATUSES,
                            field_name="CNY ledger operation status",
                        ),
                        str(item.get("error_reason") or ""),
                        str(item.get("created_at") or ""),
                        str(item.get("updated_at") or ""),
                    )
                    for item in operations
                ],
            )
            conn.commit()

    def list_cny_ledger_operations(self) -> list[dict[str, Any]]:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_cny_ledger_operations
                ORDER BY sequence_key ASC, operation_id ASC
                """
            ).fetchall()
            return [_cny_ledger_operation_to_dict(row) for row in rows]

    def save_cny_ledger_replay_state(self, state: Mapping[str, Any]) -> None:
        replayed_at = str(state.get("replayed_at") or "").strip()
        _validate_timestamp(replayed_at, field_name="replayed_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_cny_ledger_replay_state(
                    slot,
                    status,
                    reason,
                    replayed_at,
                    operation_count,
                    document_count,
                    balance_cny,
                    balance_rub_value,
                    average_rate,
                    diagnostics_json
                )
                VALUES(1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(slot) DO UPDATE SET
                    status = excluded.status,
                    reason = excluded.reason,
                    replayed_at = excluded.replayed_at,
                    operation_count = excluded.operation_count,
                    document_count = excluded.document_count,
                    balance_cny = excluded.balance_cny,
                    balance_rub_value = excluded.balance_rub_value,
                    average_rate = excluded.average_rate,
                    diagnostics_json = excluded.diagnostics_json
                """,
                (
                    str(state.get("status") or ""),
                    str(state.get("reason") or ""),
                    replayed_at,
                    int(state.get("operation_count") or 0),
                    int(state.get("document_count") or 0),
                    str(state.get("balance_cny") or ""),
                    str(state.get("balance_rub_value") or ""),
                    str(state.get("average_rate") or ""),
                    json.dumps(list(state.get("diagnostics") or []), ensure_ascii=False),
                ),
            )
            conn.commit()

    def load_cny_ledger_replay_state(self) -> dict[str, Any] | None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_cny_ledger_replay_state
                WHERE slot = 1
                """
            ).fetchone()
            if row is None:
                return None
            return {
                "status": row["status"] or "",
                "reason": row["reason"] or "",
                "replayed_at": row["replayed_at"] or "",
                "operation_count": row["operation_count"] or 0,
                "document_count": row["document_count"] or 0,
                "balance_cny": row["balance_cny"] or "",
                "balance_rub_value": row["balance_rub_value"] or "",
                "average_rate": row["average_rate"] or "",
                "diagnostics": _loads_json_list(row["diagnostics_json"]),
            }

    def update_supplier_shipments_cny_calculations(self, updates: list[Mapping[str, Any]]) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.executemany(
                """
                UPDATE sheet_vitrina_v1_supplier_shipments
                SET cny_ledger_effective_rate = ?,
                    cny_payment_currency_rub_cost = ?,
                    cny_paid_amount = ?,
                    cny_bank_fee_rub = ?,
                    cny_calculation_status = ?,
                    cny_calculation_error = ?,
                    cny_calculated_at = ?
                WHERE shipment_id = ?
                """,
                [
                    (
                        str(item.get("cny_ledger_effective_rate") or ""),
                        str(item.get("cny_payment_currency_rub_cost") or ""),
                        str(item.get("cny_paid_amount") or ""),
                        str(item.get("cny_bank_fee_rub") or ""),
                        str(item.get("cny_calculation_status") or ""),
                        str(item.get("cny_calculation_error") or ""),
                        str(item.get("cny_calculated_at") or ""),
                        str(item.get("shipment_id") or ""),
                    )
                    for item in updates
                ],
            )
            conn.commit()

    def save_trade_document(self, document: Mapping[str, Any]) -> dict[str, Any]:
        document_id = str(document.get("document_id") or "").strip()
        if not document_id:
            raise ValueError("trade document_id is required")
        created_at = str(document.get("created_at") or "").strip()
        updated_at = str(document.get("updated_at") or "").strip()
        _validate_timestamp(created_at, field_name="created_at")
        _validate_timestamp(updated_at, field_name="updated_at")
        parsed_metadata = document.get("parsed_metadata")
        warnings = document.get("warnings")
        errors = document.get("errors")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_trade_documents(
                    document_id,
                    document_type,
                    number,
                    document_date,
                    supplier_name,
                    currency,
                    amount_total,
                    source,
                    source_shipment_id,
                    source_upload_id,
                    file_original_name,
                    file_content_type,
                    file_sha256,
                    file_path,
                    parser_version,
                    parsed_metadata_json,
                    warnings_json,
                    errors_json,
                    status,
                    created_at,
                    updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    document_type = excluded.document_type,
                    number = excluded.number,
                    document_date = excluded.document_date,
                    supplier_name = excluded.supplier_name,
                    currency = excluded.currency,
                    amount_total = excluded.amount_total,
                    source = excluded.source,
                    source_shipment_id = excluded.source_shipment_id,
                    source_upload_id = excluded.source_upload_id,
                    file_original_name = excluded.file_original_name,
                    file_content_type = excluded.file_content_type,
                    file_sha256 = excluded.file_sha256,
                    file_path = excluded.file_path,
                    parser_version = excluded.parser_version,
                    parsed_metadata_json = excluded.parsed_metadata_json,
                    warnings_json = excluded.warnings_json,
                    errors_json = excluded.errors_json,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    document_id,
                    str(document.get("document_type") or ""),
                    str(document.get("number") or ""),
                    str(document.get("document_date") or ""),
                    str(document.get("supplier_name") or ""),
                    str(document.get("currency") or ""),
                    document.get("amount_total"),
                    str(document.get("source") or ""),
                    str(document.get("source_shipment_id") or ""),
                    str(document.get("source_upload_id") or ""),
                    str(document.get("file_original_name") or ""),
                    str(document.get("file_content_type") or ""),
                    str(document.get("file_sha256") or ""),
                    str(document.get("file_path") or ""),
                    str(document.get("parser_version") or ""),
                    json.dumps(dict(parsed_metadata) if isinstance(parsed_metadata, Mapping) else {}, ensure_ascii=False),
                    json.dumps(list(warnings) if isinstance(warnings, list) else [], ensure_ascii=False),
                    json.dumps(list(errors) if isinstance(errors, list) else [], ensure_ascii=False),
                    str(document.get("status") or TRADE_DOCUMENT_STATUS_ACTIVE),
                    created_at,
                    updated_at,
                ),
            )
            conn.commit()
        loaded = self.load_trade_document(document_id)
        if loaded is None:
            raise ValueError(f"trade document was not saved: {document_id}")
        return loaded

    def list_trade_documents(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        where_clause = "" if include_archived else "WHERE d.status = 'active'"
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                f"""
                SELECT d.*,
                       link.contract_document_id AS linked_contract_document_id,
                       contract.number AS linked_contract_number,
                       contract.document_date AS linked_contract_date,
                       COALESCE(invoice_counts.invoice_count, 0) AS linked_invoice_count
                FROM sheet_vitrina_v1_trade_documents d
                LEFT JOIN sheet_vitrina_v1_invoice_contract_links link
                  ON link.invoice_document_id = d.document_id
                LEFT JOIN sheet_vitrina_v1_trade_documents contract
                  ON contract.document_id = link.contract_document_id
                LEFT JOIN (
                    SELECT contract_document_id, COUNT(*) AS invoice_count
                    FROM sheet_vitrina_v1_invoice_contract_links
                    GROUP BY contract_document_id
                ) invoice_counts
                  ON invoice_counts.contract_document_id = d.document_id
                {where_clause}
                ORDER BY d.updated_at DESC, d.created_at DESC, d.document_id ASC
                """
            ).fetchall()
            return [_trade_document_to_dict(row) for row in rows]

    def load_trade_document(self, document_id: str) -> dict[str, Any] | None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT d.*,
                       link.contract_document_id AS linked_contract_document_id,
                       contract.number AS linked_contract_number,
                       contract.document_date AS linked_contract_date,
                       COALESCE(invoice_counts.invoice_count, 0) AS linked_invoice_count
                FROM sheet_vitrina_v1_trade_documents d
                LEFT JOIN sheet_vitrina_v1_invoice_contract_links link
                  ON link.invoice_document_id = d.document_id
                LEFT JOIN sheet_vitrina_v1_trade_documents contract
                  ON contract.document_id = link.contract_document_id
                LEFT JOIN (
                    SELECT contract_document_id, COUNT(*) AS invoice_count
                    FROM sheet_vitrina_v1_invoice_contract_links
                    GROUP BY contract_document_id
                ) invoice_counts
                  ON invoice_counts.contract_document_id = d.document_id
                WHERE d.document_id = ?
                """,
                (str(document_id or "").strip(),),
            ).fetchone()
            return _trade_document_to_dict(row) if row is not None else None

    def find_settings_trade_document_duplicate(self, *, document_type: str, file_sha256: str) -> dict[str, Any] | None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT document_id
                FROM sheet_vitrina_v1_trade_documents
                WHERE document_type = ?
                  AND file_sha256 = ?
                  AND source = 'settings_upload'
                  AND status = 'active'
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (str(document_type or "").strip(), str(file_sha256 or "").strip()),
            ).fetchone()
            if row is None:
                return None
            return self.load_trade_document(str(row["document_id"]))

    def find_trade_document_by_source_file(
        self,
        *,
        document_type: str,
        file_sha256: str,
        source_shipment_id: str,
    ) -> dict[str, Any] | None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT document_id
                FROM sheet_vitrina_v1_trade_documents
                WHERE document_type = ?
                  AND file_sha256 = ?
                  AND source_shipment_id = ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (
                    str(document_type or "").strip(),
                    str(file_sha256 or "").strip(),
                    str(source_shipment_id or "").strip(),
                ),
            ).fetchone()
            if row is None:
                return None
            return self.load_trade_document(str(row["document_id"]))

    def update_trade_document(self, document_id: str, updates: Mapping[str, Any], *, updated_at: str) -> dict[str, Any]:
        existing = self.load_trade_document(document_id)
        if existing is None:
            raise ValueError(f"trade document not found: {document_id}")
        _validate_timestamp(str(updated_at or ""), field_name="updated_at")
        payload = {**existing, **dict(updates), "document_id": str(document_id), "created_at": existing["created_at"], "updated_at": updated_at}
        return self.save_trade_document(payload)

    def archive_trade_document(self, document_id: str, *, updated_at: str) -> dict[str, Any]:
        _validate_timestamp(str(updated_at or ""), field_name="updated_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            cursor = conn.execute(
                """
                UPDATE sheet_vitrina_v1_trade_documents
                SET status = 'archived',
                    updated_at = ?
                WHERE document_id = ?
                """,
                (updated_at, str(document_id or "").strip()),
            )
            conn.commit()
            if cursor.rowcount <= 0:
                raise ValueError(f"trade document not found: {document_id}")
        loaded = self.load_trade_document(document_id)
        if loaded is None:
            raise ValueError(f"trade document not found: {document_id}")
        return loaded

    def save_invoice_contract_link(
        self,
        *,
        invoice_document_id: str,
        contract_document_id: str,
        created_at: str,
        updated_at: str,
        linked_by: str,
        source: str,
    ) -> dict[str, Any]:
        _validate_timestamp(str(created_at or ""), field_name="created_at")
        _validate_timestamp(str(updated_at or ""), field_name="updated_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_invoice_contract_links(
                    invoice_document_id,
                    contract_document_id,
                    created_at,
                    updated_at,
                    linked_by,
                    source
                )
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(invoice_document_id) DO UPDATE SET
                    contract_document_id = excluded.contract_document_id,
                    updated_at = excluded.updated_at,
                    linked_by = excluded.linked_by,
                    source = excluded.source
                """,
                (
                    str(invoice_document_id or "").strip(),
                    str(contract_document_id or "").strip(),
                    created_at,
                    updated_at,
                    str(linked_by or ""),
                    str(source or ""),
                ),
            )
            conn.commit()
        link = self.load_invoice_contract_link(invoice_document_id)
        if link is None:
            raise ValueError(f"invoice contract link was not saved: {invoice_document_id}")
        return link

    def load_invoice_contract_link(self, invoice_document_id: str) -> dict[str, Any] | None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_invoice_contract_links
                WHERE invoice_document_id = ?
                """,
                (str(invoice_document_id or "").strip(),),
            ).fetchone()
            if row is None:
                return None
            return {
                "invoice_document_id": row["invoice_document_id"],
                "contract_document_id": row["contract_document_id"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "linked_by": row["linked_by"] or "",
                "source": row["source"] or "",
            }

    def delete_invoice_contract_link(self, invoice_document_id: str) -> bool:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            cursor = conn.execute(
                """
                DELETE FROM sheet_vitrina_v1_invoice_contract_links
                WHERE invoice_document_id = ?
                """,
                (str(invoice_document_id or "").strip(),),
            )
            conn.commit()
            return cursor.rowcount > 0

    def count_contract_document_links(self, contract_document_id: str) -> int:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT COUNT(*) AS link_count
                FROM sheet_vitrina_v1_invoice_contract_links
                WHERE contract_document_id = ?
                """,
                (str(contract_document_id or "").strip(),),
            ).fetchone()
            return int(row["link_count"] if row is not None else 0)

    def find_contract_document_candidates(self, *, number: str, document_date: str = "") -> list[dict[str, Any]]:
        normalized_number = str(number or "").strip()
        normalized_date = str(document_date or "").strip()
        if not normalized_number and not normalized_date:
            return []
        clauses = ["document_type = 'contract'", "status = 'active'"]
        params: list[Any] = []
        if normalized_number:
            clauses.append("number = ?")
            params.append(normalized_number)
        if normalized_date:
            clauses.append("document_date = ?")
            params.append(normalized_date)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                f"""
                SELECT *
                FROM sheet_vitrina_v1_trade_documents
                WHERE {" AND ".join(clauses)}
                ORDER BY updated_at DESC, created_at DESC, document_id ASC
                """,
                tuple(params),
            ).fetchall()
            return [_trade_document_to_dict(row) for row in rows]

    def set_supplier_shipment_invoice_document_id(
        self,
        *,
        shipment_id: str,
        invoice_document_id: str,
        updated_at: str,
    ) -> bool:
        _validate_timestamp(str(updated_at or ""), field_name="updated_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            cursor = conn.execute(
                """
                UPDATE sheet_vitrina_v1_supplier_shipments
                SET invoice_document_id = ?,
                    updated_at = ?
                WHERE shipment_id = ?
                """,
                (
                    str(invoice_document_id or "").strip(),
                    updated_at,
                    str(shipment_id or "").strip(),
                ),
            )
            conn.commit()
            return cursor.rowcount > 0

    def list_nomenclature_items(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            where_clause = "WHERE is_active = 1" if active_only else ""
            rows = conn.execute(
                f"""
                SELECT *
                FROM sheet_vitrina_v1_nomenclature_items
                {where_clause}
                ORDER BY is_hidden ASC, is_active DESC, created_at ASC, product_type ASC, match_key ASC, nomenclature_name ASC
                """
            ).fetchall()
            return [_nomenclature_item_to_dict(row) for row in rows]

    def load_nomenclature_item(self, item_id: str) -> dict[str, Any] | None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_nomenclature_items
                WHERE item_id = ?
                """,
                (item_id,),
            ).fetchone()
            return _nomenclature_item_to_dict(row) if row is not None else None

    def active_nomenclature_match_key_exists(self, *, match_key: str, exclude_item_id: str = "") -> bool:
        normalized = str(match_key or "").strip()
        if not normalized:
            return False
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT item_id
                FROM sheet_vitrina_v1_nomenclature_items
                WHERE is_active = 1
                  AND match_key = ?
                  AND item_id != ?
                LIMIT 1
                """,
                (normalized, str(exclude_item_id or "")),
            ).fetchone()
            return row is not None

    def save_nomenclature_item(self, item: Mapping[str, Any]) -> dict[str, Any]:
        saved_items = self.save_nomenclature_items_atomic([item])
        if not saved_items:
            raise ValueError("nomenclature item was not saved")
        return saved_items[0]

    def save_nomenclature_items_atomic(self, items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        prepared_items: list[dict[str, Any]] = []
        for item in items:
            item_id = str(item.get("item_id") or "").strip()
            if not item_id:
                raise ValueError("nomenclature item_id is required")
            created_at = str(item.get("created_at") or "").strip()
            updated_at = str(item.get("updated_at") or "").strip()
            _validate_timestamp(created_at, field_name="created_at")
            _validate_timestamp(updated_at, field_name="updated_at")
            aliases = item.get("aliases") if isinstance(item.get("aliases"), list) else []
            compatible_model_keys = (
                item.get("compatible_model_keys") if isinstance(item.get("compatible_model_keys"), list) else []
            )
            raw_barcodes = item.get("barcodes") if isinstance(item.get("barcodes"), list) else []
            barcode = str(item.get("barcode") or item.get("primary_barcode") or "").strip()
            normalized_barcodes: list[str] = []
            seen_barcodes: set[str] = set()
            for raw_barcode in [barcode, *raw_barcodes]:
                normalized_barcode = str(raw_barcode or "").strip()
                if not normalized_barcode or normalized_barcode in seen_barcodes:
                    continue
                seen_barcodes.add(normalized_barcode)
                normalized_barcodes.append(normalized_barcode)
            if not barcode and normalized_barcodes:
                barcode = normalized_barcodes[0]
            purchase_price_yuan = item.get("purchase_price_yuan")
            if purchase_price_yuan is not None:
                purchase_price_yuan = float(purchase_price_yuan)
            factory_box_size = item.get("factory_box_size")
            if factory_box_size not in (None, ""):
                try:
                    box_value = Decimal(str(factory_box_size))
                except (InvalidOperation, ValueError):
                    raise ValueError(
                        "factory_box_size must be a positive integer"
                    ) from None
                if (
                    not box_value.is_finite()
                    or box_value <= 0
                    or box_value != box_value.to_integral_value()
                ):
                    raise ValueError(
                        "factory_box_size must be a positive integer"
                    )
                factory_box_size = int(box_value)
            else:
                factory_box_size = None
            prepared_items.append(
                {
                    "item_id": item_id,
                    "is_active": 1 if bool(item.get("is_active")) else 0,
                    "is_hidden": 1 if bool(item.get("is_hidden")) else 0,
                    "hidden_at": str(item.get("hidden_at") or ""),
                    "hidden_reason": str(item.get("hidden_reason") or ""),
                    "our_sku": str(item.get("our_sku") or ""),
                    "nm_id": item.get("nm_id"),
                    "barcode": barcode,
                    "barcodes_json": json.dumps(normalized_barcodes, ensure_ascii=False),
                    "barcode_source": str(item.get("barcode_source") or ("manual" if barcode else "missing")),
                    "barcode_status": str(item.get("barcode_status") or ("ready" if barcode else "missing")),
                    "barcode_synced_at": str(item.get("barcode_synced_at") or ""),
                    "barcode_updated_at": str(item.get("barcode_updated_at") or ""),
                    "barcode_evidence_json": json.dumps(
                        item.get("barcode_evidence") if isinstance(item.get("barcode_evidence"), Mapping) else {},
                        ensure_ascii=False,
                    ),
                    "vendor_code": str(item.get("vendor_code") or item.get("seller_article") or ""),
                    "wb_title": str(item.get("wb_title") or ""),
                    "wb_subject_name": str(item.get("wb_subject_name") or ""),
                    "wb_updated_at": str(item.get("wb_updated_at") or ""),
                    "wb_synced_at": str(item.get("wb_synced_at") or ""),
                    "wb_sync_status": str(item.get("wb_sync_status") or ""),
                    "wb_sync_evidence_json": json.dumps(
                        item.get("wb_sync_evidence") if isinstance(item.get("wb_sync_evidence"), Mapping) else {},
                        ensure_ascii=False,
                    ),
                    "nomenclature_name": str(item.get("nomenclature_name") or ""),
                    "product_type": str(item.get("product_type") or ""),
                    "match_key": str(item.get("match_key") or ""),
                    "purchase_price_yuan": purchase_price_yuan,
                    "factory_box_size": factory_box_size,
                    "aliases_json": json.dumps([str(alias) for alias in aliases if str(alias or "").strip()], ensure_ascii=False),
                    "compatible_models_text": str(item.get("compatible_models_text") or ""),
                    "compatible_model_keys_json": json.dumps(
                        [str(model_key) for model_key in compatible_model_keys if str(model_key or "").strip()],
                        ensure_ascii=False,
                    ),
                    "comment": str(item.get("comment") or ""),
                    "created_at": created_at,
                    "updated_at": updated_at,
                }
            )
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            for prepared in prepared_items:
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_nomenclature_items(
                        item_id,
                        is_active,
                        is_hidden,
                        hidden_at,
                        hidden_reason,
                        our_sku,
                        nm_id,
                        barcode,
                        barcodes_json,
                        barcode_source,
                        barcode_status,
                        barcode_synced_at,
                        barcode_updated_at,
                        barcode_evidence_json,
                        vendor_code,
                        wb_title,
                        wb_subject_name,
                        wb_updated_at,
                        wb_synced_at,
                        wb_sync_status,
                        wb_sync_evidence_json,
                        nomenclature_name,
                        product_type,
                        match_key,
                        purchase_price_yuan,
                        factory_box_size,
                        aliases_json,
                        compatible_models_text,
                        compatible_model_keys_json,
                        comment,
                        created_at,
                        updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(item_id) DO UPDATE SET
                        is_active = excluded.is_active,
                        is_hidden = excluded.is_hidden,
                        hidden_at = excluded.hidden_at,
                        hidden_reason = excluded.hidden_reason,
                        our_sku = excluded.our_sku,
                        nm_id = excluded.nm_id,
                        barcode = excluded.barcode,
                        barcodes_json = excluded.barcodes_json,
                        barcode_source = excluded.barcode_source,
                        barcode_status = excluded.barcode_status,
                        barcode_synced_at = excluded.barcode_synced_at,
                        barcode_updated_at = excluded.barcode_updated_at,
                        barcode_evidence_json = excluded.barcode_evidence_json,
                        vendor_code = excluded.vendor_code,
                        wb_title = excluded.wb_title,
                        wb_subject_name = excluded.wb_subject_name,
                        wb_updated_at = excluded.wb_updated_at,
                        wb_synced_at = excluded.wb_synced_at,
                        wb_sync_status = excluded.wb_sync_status,
                        wb_sync_evidence_json = excluded.wb_sync_evidence_json,
                        nomenclature_name = excluded.nomenclature_name,
                        product_type = excluded.product_type,
                        match_key = excluded.match_key,
                        purchase_price_yuan = excluded.purchase_price_yuan,
                        factory_box_size = excluded.factory_box_size,
                        aliases_json = excluded.aliases_json,
                        compatible_models_text = excluded.compatible_models_text,
                        compatible_model_keys_json = excluded.compatible_model_keys_json,
                        comment = excluded.comment,
                        updated_at = excluded.updated_at
                    """,
                    (
                        prepared["item_id"],
                        prepared["is_active"],
                        prepared["is_hidden"],
                        prepared["hidden_at"],
                        prepared["hidden_reason"],
                        prepared["our_sku"],
                        prepared["nm_id"],
                        prepared["barcode"],
                        prepared["barcodes_json"],
                        prepared["barcode_source"],
                        prepared["barcode_status"],
                        prepared["barcode_synced_at"],
                        prepared["barcode_updated_at"],
                        prepared["barcode_evidence_json"],
                        prepared["vendor_code"],
                        prepared["wb_title"],
                        prepared["wb_subject_name"],
                        prepared["wb_updated_at"],
                        prepared["wb_synced_at"],
                        prepared["wb_sync_status"],
                        prepared["wb_sync_evidence_json"],
                        prepared["nomenclature_name"],
                        prepared["product_type"],
                        prepared["match_key"],
                        prepared["purchase_price_yuan"],
                        prepared["factory_box_size"],
                        prepared["aliases_json"],
                        prepared["compatible_models_text"],
                        prepared["compatible_model_keys_json"],
                        prepared["comment"],
                        prepared["created_at"],
                        prepared["updated_at"],
                    ),
                )
            conn.commit()
        loaded_items: list[dict[str, Any]] = []
        for prepared in prepared_items:
            loaded = self.load_nomenclature_item(str(prepared["item_id"]))
            if loaded is None:
                raise ValueError(f"nomenclature item was not saved: {prepared['item_id']}")
            loaded_items.append(loaded)
        return loaded_items

    def delete_nomenclature_item(self, item_id: str, *, updated_at: str) -> dict[str, Any]:
        _validate_timestamp(updated_at, field_name="updated_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            cursor = conn.execute(
                """
                UPDATE sheet_vitrina_v1_nomenclature_items
                SET is_active = 0,
                    updated_at = ?
                WHERE item_id = ?
                """,
                (updated_at, item_id),
            )
            conn.commit()
            if cursor.rowcount <= 0:
                raise ValueError(f"nomenclature item not found: {item_id}")
        loaded = self.load_nomenclature_item(item_id)
        if loaded is None:
            raise ValueError(f"nomenclature item not found: {item_id}")
        return loaded

    def list_sku_groups(self, *, include_inactive: bool = True) -> list[dict[str, Any]]:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            where_clause = "" if include_inactive else "WHERE is_active = 1"
            rows = conn.execute(
                f"""
                SELECT *
                FROM sheet_vitrina_v1_sku_groups
                {where_clause}
                ORDER BY is_active DESC, group_key ASC
                """
            ).fetchall()
            return [_sku_group_to_dict(row) for row in rows]

    def load_sku_group(self, group_key: str) -> dict[str, Any] | None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_sku_groups
                WHERE group_key = ?
                """,
                (str(group_key or "").strip(),),
            ).fetchone()
            return _sku_group_to_dict(row) if row is not None else None

    def save_sku_group(self, group: Mapping[str, Any]) -> dict[str, Any]:
        group_key = str(group.get("group_key") or "").strip()
        if not group_key:
            raise ValueError("sku group_key is required")
        created_at = str(group.get("created_at") or "").strip()
        updated_at = str(group.get("updated_at") or "").strip()
        _validate_timestamp(created_at, field_name="created_at")
        _validate_timestamp(updated_at, field_name="updated_at")
        aliases = group.get("aliases") if isinstance(group.get("aliases"), list) else []
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_sku_groups(
                    group_key,
                    label,
                    aliases_json,
                    is_active,
                    is_system,
                    created_at,
                    updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_key) DO UPDATE SET
                    label = excluded.label,
                    aliases_json = excluded.aliases_json,
                    is_active = excluded.is_active,
                    is_system = excluded.is_system,
                    updated_at = excluded.updated_at
                """,
                (
                    group_key,
                    str(group.get("label") or group_key),
                    json.dumps([str(alias) for alias in aliases if str(alias or "").strip()], ensure_ascii=False),
                    1 if bool(group.get("is_active", True)) else 0,
                    1 if bool(group.get("is_system")) else 0,
                    created_at,
                    updated_at,
                ),
            )
            conn.commit()
        loaded = self.load_sku_group(group_key)
        if loaded is None:
            raise ValueError(f"sku group was not saved: {group_key}")
        return loaded

    def sku_group_active_item_count(self, group_key: str) -> int:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT COUNT(*) AS item_count
                FROM sheet_vitrina_v1_nomenclature_items
                WHERE is_active = 1
                  AND product_type = ?
                """,
                (str(group_key or "").strip(),),
            ).fetchone()
            return int(row["item_count"] if row is not None else 0)

    def save_plan_report_monthly_baseline(
        self,
        *,
        rows: list[Mapping[str, Any]],
        uploaded_at: str,
        source_kind: str,
        uploaded_filename: str,
        uploaded_content_type: str,
        workbook_checksum: str,
        note: str | None = None,
    ) -> None:
        _validate_timestamp(uploaded_at, field_name="uploaded_at")
        if not rows:
            raise ValueError("plan-report monthly baseline rows must not be empty")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            for row in rows:
                month = str(row.get("month", "") or "").strip()
                _validate_month(month, field_name="month")
                fin_buyout_rub = float(row.get("fin_buyout_rub"))
                ads_sum = float(row.get("ads_sum"))
                if fin_buyout_rub < 0:
                    raise ValueError("fin_buyout_rub must be >= 0")
                if ads_sum < 0:
                    raise ValueError("ads_sum must be >= 0")
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_plan_report_monthly_baseline(
                        month,
                        fin_buyout_rub,
                        ads_sum,
                        uploaded_at,
                        source_kind,
                        uploaded_filename,
                        uploaded_content_type,
                        workbook_checksum,
                        note
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(month) DO UPDATE SET
                        fin_buyout_rub = excluded.fin_buyout_rub,
                        ads_sum = excluded.ads_sum,
                        uploaded_at = excluded.uploaded_at,
                        source_kind = excluded.source_kind,
                        uploaded_filename = excluded.uploaded_filename,
                        uploaded_content_type = excluded.uploaded_content_type,
                        workbook_checksum = excluded.workbook_checksum,
                        note = excluded.note
                    """,
                    (
                        month,
                        fin_buyout_rub,
                        ads_sum,
                        uploaded_at,
                        source_kind,
                        uploaded_filename,
                        uploaded_content_type,
                        workbook_checksum,
                        str(note or "").strip(),
                    ),
                )
            conn.commit()

    def load_plan_report_monthly_baseline(self) -> list[dict[str, Any]]:
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT
                    month,
                    fin_buyout_rub,
                    ads_sum,
                    uploaded_at,
                    source_kind,
                    uploaded_filename,
                    uploaded_content_type,
                    workbook_checksum,
                    note
                FROM sheet_vitrina_v1_plan_report_monthly_baseline
                ORDER BY month
                """
            ).fetchall()
            return [
                {
                    "month": row["month"],
                    "fin_buyout_rub": float(row["fin_buyout_rub"]),
                    "ads_sum": float(row["ads_sum"]),
                    "uploaded_at": row["uploaded_at"],
                    "source_kind": row["source_kind"],
                    "uploaded_filename": str(row["uploaded_filename"] or "") or None,
                    "uploaded_content_type": str(row["uploaded_content_type"] or "") or None,
                    "workbook_checksum": str(row["workbook_checksum"] or "") or None,
                    "note": str(row["note"] or "") or None,
                }
                for row in rows
            ]

    def save_factory_order_result_state(
        self,
        *,
        calculated_at: str,
        payload: Mapping[str, Any],
        evidence: Mapping[str, Any] | None = None,
        export_bytes: bytes | None = None,
        export_filename: str | None = None,
        export_content_type: str | None = None,
    ) -> None:
        _validate_timestamp(calculated_at, field_name="calculated_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            _save_supply_calculation_record(
                conn,
                calculation_type="factory_order",
                calculated_at=calculated_at,
                payload=payload,
                evidence=evidence,
                export_bytes=export_bytes,
                export_filename=export_filename,
                export_content_type=export_content_type,
                latest_table="sheet_vitrina_v1_factory_order_result_state",
            )
            conn.commit()

    def load_factory_order_result_state(self) -> dict[str, Any] | None:
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT calculated_at, result_json
                FROM sheet_vitrina_v1_factory_order_result_state
                WHERE slot = 1
                """
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(row["result_json"])
            if isinstance(payload, dict):
                payload.setdefault("calculated_at", row["calculated_at"])
            return payload

    def save_fbs_fulfillment_order_result_state(
        self,
        *,
        calculated_at: str,
        payload: Mapping[str, Any],
        evidence: Mapping[str, Any] | None = None,
        export_bytes: bytes | None = None,
        export_filename: str | None = None,
        export_content_type: str | None = None,
    ) -> None:
        _validate_timestamp(calculated_at, field_name="calculated_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            _save_supply_calculation_record(
                conn,
                calculation_type="fbs_fulfillment_order",
                calculated_at=calculated_at,
                payload=payload,
                evidence=evidence,
                export_bytes=export_bytes,
                export_filename=export_filename,
                export_content_type=export_content_type,
                latest_table="sheet_vitrina_v1_fbs_fulfillment_order_result_state",
            )
            conn.commit()

    def load_fbs_fulfillment_order_result_state(self) -> dict[str, Any] | None:
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT calculated_at, result_json
                FROM sheet_vitrina_v1_fbs_fulfillment_order_result_state
                WHERE slot = 1
                """
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(row["result_json"])
            if isinstance(payload, dict):
                payload.setdefault("calculated_at", row["calculated_at"])
            return payload

    def save_wb_regional_supply_result_state(
        self,
        *,
        calculated_at: str,
        payload: Mapping[str, Any],
        evidence: Mapping[str, Any] | None = None,
        export_bytes: bytes | None = None,
        export_filename: str | None = None,
        export_content_type: str | None = None,
    ) -> None:
        _validate_timestamp(calculated_at, field_name="calculated_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            registry_inserted = _save_supply_calculation_record(
                conn,
                calculation_type="wb_regional",
                calculated_at=calculated_at,
                payload=payload,
                evidence=evidence,
                export_bytes=export_bytes,
                export_filename=export_filename,
                export_content_type=export_content_type,
                latest_table="sheet_vitrina_v1_wb_regional_supply_result_state",
            )
            if registry_inserted:
                audit_row = _build_wb_regional_supply_calculation_audit_row(
                    calculated_at=calculated_at,
                    payload=payload,
                )
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_wb_regional_supply_calculation_audit(
                        saved_at,
                        calculated_at,
                        calculation_id,
                        report_date,
                        metadata_json
                    )
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (
                        calculated_at,
                        audit_row["calculated_at"],
                        audit_row["calculation_id"],
                        audit_row["report_date"],
                        json.dumps(audit_row, ensure_ascii=False, sort_keys=True),
                    ),
                )
            conn.execute(
                """
                DELETE FROM sheet_vitrina_v1_wb_regional_supply_calculation_audit
                WHERE id NOT IN (
                    SELECT id
                    FROM sheet_vitrina_v1_wb_regional_supply_calculation_audit
                    ORDER BY id DESC
                    LIMIT 200
                )
                """
            )
            conn.commit()

    def describe_temporal_source_window(
        self,
        *,
        source_key: str,
        date_from: str,
        date_to: str,
    ) -> dict[str, Any]:
        _validate_iso_date(date_from, field_name="date_from")
        _validate_iso_date(date_to, field_name="date_to")
        if date_to < date_from:
            raise ValueError("date_to must be >= date_from")
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT snapshot_date, captured_at, payload_json
                FROM temporal_source_snapshots
                WHERE source_key = ?
                  AND snapshot_date >= ?
                  AND snapshot_date <= ?
                ORDER BY snapshot_date ASC
                """,
                (str(source_key or "").strip(), date_from, date_to),
            ).fetchall()
        digest_payload = [
            {
                "snapshot_date": str(row["snapshot_date"]),
                "captured_at": str(row["captured_at"] or ""),
                "payload_sha256": "sha256:" + hashlib.sha256(
                    str(row["payload_json"] or "").encode("utf-8")
                ).hexdigest(),
            }
            for row in rows
        ]
        return {
            "source_key": str(source_key or "").strip(),
            "date_from": date_from,
            "date_to": date_to,
            "snapshot_count": len(rows),
            "earliest_snapshot_date": str(rows[0]["snapshot_date"]) if rows else "",
            "latest_snapshot_date": str(rows[-1]["snapshot_date"]) if rows else "",
            "captured_at_min": min((str(row["captured_at"] or "") for row in rows), default=""),
            "captured_at_max": max((str(row["captured_at"] or "") for row in rows), default=""),
            "fingerprint": _canonical_json_sha256(digest_payload),
        }

    def describe_factory_order_dataset_evidence(self, dataset_type: str) -> dict[str, Any]:
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT
                    uploaded_at,
                    row_count,
                    rows_json,
                    uploaded_filename,
                    uploaded_content_type,
                    workbook_blob
                FROM sheet_vitrina_v1_factory_order_dataset_state
                WHERE dataset_type = ?
                """,
                (str(dataset_type or "").strip(),),
            ).fetchone()
        if row is None:
            return {
                "dataset_type": str(dataset_type or "").strip(),
                "status": "missing",
                "row_count": 0,
                "rows_fingerprint": _canonical_json_sha256([]),
                "workbook_sha256": "",
            }
        workbook_bytes = bytes(row["workbook_blob"] or b"")
        try:
            rows_payload = json.loads(str(row["rows_json"] or "[]"))
        except json.JSONDecodeError:
            rows_payload = []
        return {
            "dataset_type": str(dataset_type or "").strip(),
            "status": "uploaded",
            "uploaded_at": str(row["uploaded_at"] or ""),
            "row_count": int(row["row_count"] or 0),
            "uploaded_filename": str(row["uploaded_filename"] or ""),
            "uploaded_content_type": str(row["uploaded_content_type"] or ""),
            "rows_fingerprint": _canonical_json_sha256(rows_payload),
            "workbook_sha256": (
                "sha256:" + hashlib.sha256(workbook_bytes).hexdigest()
                if workbook_bytes
                else ""
            ),
        }

    def list_supply_calculation_registry(
        self,
        *,
        calculation_type: str | None = None,
        report_date_from: str | None = None,
        report_date_to: str | None = None,
        limit: int = SUPPLY_CALCULATION_REGISTRY_DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> dict[str, Any]:
        normalized_type = _normalize_supply_calculation_type(
            calculation_type,
            allow_empty=True,
        )
        normalized_date_from = str(report_date_from or "").strip()
        normalized_date_to = str(report_date_to or "").strip()
        if normalized_date_from:
            _validate_iso_date(normalized_date_from, field_name="report_date_from")
        if normalized_date_to:
            _validate_iso_date(normalized_date_to, field_name="report_date_to")
        if normalized_date_from and normalized_date_to and normalized_date_to < normalized_date_from:
            raise ValueError("report_date_to must be >= report_date_from")
        normalized_limit = min(
            max(int(limit), 1),
            SUPPLY_CALCULATION_REGISTRY_MAX_PAGE_SIZE,
        )
        normalized_offset = max(int(offset), 0)
        clauses: list[str] = []
        params: list[Any] = []
        if normalized_type:
            clauses.append("calculation_type = ?")
            params.append(normalized_type)
        if normalized_date_from:
            clauses.append("report_date >= ?")
            params.append(normalized_date_from)
        if normalized_date_to:
            clauses.append("report_date <= ?")
            params.append(normalized_date_to)
        where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            total_row = conn.execute(
                f"""
                SELECT COUNT(*) AS row_count
                FROM sheet_vitrina_v1_supply_calculation_registry
                {where_sql}
                """,
                tuple(params),
            ).fetchone()
            rows = conn.execute(
                f"""
                SELECT *
                FROM sheet_vitrina_v1_supply_calculation_registry
                {where_sql}
                ORDER BY calculated_at DESC, record_id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, normalized_limit, normalized_offset),
            ).fetchall()
        return {
            "contract_name": "sheet_vitrina_v1_supply_calculation_registry",
            "contract_version": 1,
            "retention": {
                "complete_record_limit": SUPPLY_CALCULATION_REGISTRY_MAX_COMPLETE_RECORDS,
                "legacy_metadata_source_limit": 200,
                "default_page_size": SUPPLY_CALCULATION_REGISTRY_DEFAULT_PAGE_SIZE,
                "max_page_size": SUPPLY_CALCULATION_REGISTRY_MAX_PAGE_SIZE,
                "order": "calculated_at DESC, record_id DESC",
                "max_payload_bytes": SUPPLY_CALCULATION_REGISTRY_MAX_PAYLOAD_BYTES,
                "max_metadata_bytes": SUPPLY_CALCULATION_REGISTRY_MAX_METADATA_BYTES,
                "max_export_bytes": SUPPLY_CALCULATION_REGISTRY_MAX_EXPORT_BYTES,
            },
            "filters": {
                "calculation_type": normalized_type or "",
                "report_date_from": normalized_date_from,
                "report_date_to": normalized_date_to,
            },
            "pagination": {
                "limit": normalized_limit,
                "offset": normalized_offset,
                "total": int(total_row["row_count"] if total_row is not None else 0),
            },
            "records": [_supply_calculation_registry_row_to_list_item(row) for row in rows],
        }

    def load_supply_calculation_registry_record(self, record_id: str) -> dict[str, Any] | None:
        normalized_record_id = str(record_id or "").strip()
        if not normalized_record_id:
            raise ValueError("record_id is required")
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_supply_calculation_registry
                WHERE record_id = ?
                """,
                (normalized_record_id,),
            ).fetchone()
        return _supply_calculation_registry_row_to_detail(row) if row is not None else None

    def load_supply_calculation_registry_export(
        self,
        record_id: str,
    ) -> tuple[bytes, str, str]:
        normalized_record_id = str(record_id or "").strip()
        if not normalized_record_id:
            raise ValueError("record_id is required")
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT
                    completeness,
                    export_filename,
                    export_content_type,
                    export_blob
                FROM sheet_vitrina_v1_supply_calculation_registry
                WHERE record_id = ?
                """,
                (normalized_record_id,),
            ).fetchone()
        if row is None:
            raise KeyError(normalized_record_id)
        if str(row["completeness"] or "") != "complete":
            raise ValueError(
                "Legacy metadata-only запись не содержит воспроизводимого payload/export."
            )
        export_bytes = bytes(row["export_blob"] or b"")
        if not export_bytes:
            raise ValueError("Историческая выгрузка для этой записи недоступна.")
        return (
            export_bytes,
            str(row["export_filename"] or "calculation-export.bin"),
            str(row["export_content_type"] or "application/octet-stream"),
        )

    def load_wb_regional_supply_result_state(self) -> dict[str, Any] | None:
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                """
                SELECT calculated_at, result_json
                FROM sheet_vitrina_v1_wb_regional_supply_result_state
                WHERE slot = 1
                """
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(row["result_json"])
            if isinstance(payload, dict):
                payload.setdefault("calculated_at", row["calculated_at"])
            return payload

    def list_wb_regional_supply_calculation_audit(self, *, limit: int = 50) -> list[dict[str, Any]]:
        normalized_limit = min(max(int(limit), 1), 200)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT id, saved_at, calculated_at, calculation_id, report_date, metadata_json
                FROM sheet_vitrina_v1_wb_regional_supply_calculation_audit
                ORDER BY id DESC
                LIMIT ?
                """,
                (normalized_limit,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"])
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            metadata.setdefault("id", row["id"])
            metadata.setdefault("saved_at", row["saved_at"])
            metadata.setdefault("calculated_at", row["calculated_at"])
            metadata.setdefault("calculation_id", row["calculation_id"])
            metadata.setdefault("report_date", row["report_date"])
            result.append(metadata)
        return result

    def _collect_validation_errors(self, bundle: RegistryUploadBundleV1, activated_at: str) -> list[str]:
        errors: list[str] = []
        try:
            self.bundle_block.validate_bundle(bundle, enforce_fixture_uniqueness=False)
        except ValueError as exc:
            errors.append(str(exc))

        try:
            _validate_timestamp(activated_at, field_name="activated_at")
        except ValueError as exc:
            errors.append(str(exc))
        return errors

    def _collect_cost_price_validation_errors(
        self,
        payload: CostPriceUploadPayload,
        activated_at: str,
    ) -> list[str]:
        errors: list[str] = []
        try:
            self.cost_price_block.validate_dataset(payload)
        except ValueError as exc:
            errors.append(str(exc))

        try:
            _validate_timestamp(activated_at, field_name="activated_at")
        except ValueError as exc:
            errors.append(str(exc))
        return errors


def _persist_bundle(
    conn: sqlite3.Connection,
    bundle: RegistryUploadBundleV1,
    result: RegistryUploadResult,
) -> None:
    conn.execute(
        """
        INSERT INTO registry_upload_versions(bundle_version, uploaded_at, activated_at)
        VALUES(?, ?, ?)
        """,
        (bundle.bundle_version, bundle.uploaded_at, result.activated_at),
    )
    conn.execute(
        """
        INSERT INTO registry_upload_results(
            bundle_version,
            status,
            config_count,
            metrics_count,
            formulas_count,
            validation_errors_json,
            activated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            result.bundle_version,
            result.status,
            result.accepted_counts.config_v2,
            result.accepted_counts.metrics_v2,
            result.accepted_counts.formulas_v2,
            json.dumps(result.validation_errors, ensure_ascii=False),
            result.activated_at,
        ),
    )
    conn.executemany(
        """
        INSERT INTO registry_upload_config_v2(
            bundle_version,
            nm_id,
            enabled,
            display_name,
            group_name,
            display_order
        )
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        [
            (
                bundle.bundle_version,
                item.nm_id,
                int(item.enabled),
                item.display_name,
                item.group,
                item.display_order,
            )
            for item in bundle.config_v2
        ],
    )
    conn.executemany(
        """
        INSERT INTO registry_upload_metrics_v2(
            bundle_version,
            metric_key,
            enabled,
            scope,
            label_ru,
            calc_type,
            calc_ref,
            show_in_data,
            format_name,
            display_order,
            section_name
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                bundle.bundle_version,
                item.metric_key,
                int(item.enabled),
                item.scope,
                item.label_ru,
                item.calc_type,
                item.calc_ref,
                int(item.show_in_data),
                item.format,
                item.display_order,
                item.section,
            )
            for item in bundle.metrics_v2
        ],
    )
    conn.executemany(
        """
        INSERT INTO registry_upload_formulas_v2(
            bundle_version,
            row_order,
            formula_id,
            expression,
            description
        )
        VALUES(?, ?, ?, ?, ?)
        """,
        [
            (
                bundle.bundle_version,
                index,
                item.formula_id,
                item.expression,
                item.description,
            )
            for index, item in enumerate(bundle.formulas_v2, start=1)
        ],
    )
    conn.execute(
        """
        INSERT INTO registry_upload_current_state(slot, bundle_version, activated_at)
        VALUES(1, ?, ?)
        ON CONFLICT(slot) DO UPDATE SET
            bundle_version = excluded.bundle_version,
            activated_at = excluded.activated_at
        """,
        (bundle.bundle_version, result.activated_at),
    )


def _persist_cost_price_payload(
    conn: sqlite3.Connection,
    payload: CostPriceUploadPayload,
    result: CostPriceUploadResult,
) -> None:
    conn.execute(
        """
        INSERT INTO cost_price_upload_versions(dataset_version, uploaded_at, activated_at)
        VALUES(?, ?, ?)
        """,
        (payload.dataset_version, payload.uploaded_at, result.activated_at),
    )
    conn.execute(
        """
        INSERT INTO cost_price_upload_results(
            dataset_version,
            status,
            row_count,
            validation_errors_json,
            activated_at
        )
        VALUES(?, ?, ?, ?, ?)
        """,
        (
            payload.dataset_version,
            result.status,
            result.accepted_counts.cost_price_rows,
            json.dumps(result.validation_errors, ensure_ascii=False),
            result.activated_at,
        ),
    )
    conn.executemany(
        """
        INSERT INTO cost_price_upload_rows(
            dataset_version,
            row_order,
            group_name,
            cost_price_rub,
            effective_from
        )
        VALUES(?, ?, ?, ?, ?)
        """,
        [
            (
                payload.dataset_version,
                index,
                item.group,
                item.cost_price_rub,
                item.effective_from,
            )
            for index, item in enumerate(payload.cost_price_rows, start=1)
        ],
    )
    conn.execute(
        """
        INSERT INTO cost_price_current_state(slot, dataset_version, activated_at)
        VALUES(1, ?, ?)
        ON CONFLICT(slot) DO UPDATE SET
            dataset_version = excluded.dataset_version,
            activated_at = excluded.activated_at
        """,
        (payload.dataset_version, result.activated_at),
    )


def _load_config_items(conn: sqlite3.Connection, bundle_version: str) -> list[ConfigV2Item]:
    rows = conn.execute(
        """
        SELECT nm_id, enabled, display_name, group_name, display_order
        FROM registry_upload_config_v2
        WHERE bundle_version = ?
        ORDER BY display_order
        """,
        (bundle_version,),
    ).fetchall()
    return [
        ConfigV2Item(
            nm_id=row["nm_id"],
            enabled=bool(row["enabled"]),
            display_name=row["display_name"],
            group=row["group_name"],
            display_order=row["display_order"],
        )
        for row in rows
    ]


def _load_metric_items(conn: sqlite3.Connection, bundle_version: str) -> list[MetricV2Item]:
    rows = conn.execute(
        """
        SELECT metric_key, enabled, scope, label_ru, calc_type, calc_ref, show_in_data, format_name, display_order, section_name
        FROM registry_upload_metrics_v2
        WHERE bundle_version = ?
        ORDER BY display_order
        """,
        (bundle_version,),
    ).fetchall()
    return [
        MetricV2Item(
            metric_key=row["metric_key"],
            enabled=bool(row["enabled"]),
            scope=row["scope"],
            label_ru=row["label_ru"],
            calc_type=row["calc_type"],
            calc_ref=row["calc_ref"],
            show_in_data=bool(row["show_in_data"]),
            format=row["format_name"],
            display_order=row["display_order"],
            section=row["section_name"],
        )
        for row in rows
    ]


def _load_formula_items(conn: sqlite3.Connection, bundle_version: str) -> list[FormulaV2Item]:
    rows = conn.execute(
        """
        SELECT formula_id, expression, description
        FROM registry_upload_formulas_v2
        WHERE bundle_version = ?
        ORDER BY row_order
        """,
        (bundle_version,),
    ).fetchall()
    return [
        FormulaV2Item(
            formula_id=row["formula_id"],
            expression=row["expression"],
            description=row["description"],
        )
        for row in rows
    ]


def _load_cost_price_rows(conn: sqlite3.Connection, dataset_version: str) -> list[CostPriceRow]:
    rows = conn.execute(
        """
        SELECT group_name, cost_price_rub, effective_from
        FROM cost_price_upload_rows
        WHERE dataset_version = ?
        ORDER BY row_order
        """,
        (dataset_version,),
    ).fetchall()
    return [
        CostPriceRow(
            group=row["group_name"],
            cost_price_rub=row["cost_price_rub"],
            effective_from=row["effective_from"],
        )
        for row in rows
    ]


def _coerce_bundle(bundle_input: RegistryUploadBundleV1 | Mapping[str, Any]) -> RegistryUploadBundleV1:
    if isinstance(bundle_input, RegistryUploadBundleV1):
        return bundle_input
    return parse_registry_upload_bundle_v1_payload(bundle_input)


def _coerce_cost_price_payload(
    payload_input: CostPriceUploadPayload | Mapping[str, Any],
) -> CostPriceUploadPayload:
    if isinstance(payload_input, CostPriceUploadPayload):
        return payload_input
    return parse_cost_price_upload_payload(payload_input)


def _accepted_counts(bundle: RegistryUploadBundleV1) -> RegistryUploadAcceptedCounts:
    return RegistryUploadAcceptedCounts(
        config_v2=len(bundle.config_v2),
        metrics_v2=len(bundle.metrics_v2),
        formulas_v2=len(bundle.formulas_v2),
    )


def _rejected_result(bundle_version: str, errors: list[str]) -> RegistryUploadResult:
    return RegistryUploadResult(
        status="rejected",
        bundle_version=bundle_version,
        accepted_counts=RegistryUploadAcceptedCounts(config_v2=0, metrics_v2=0, formulas_v2=0),
        validation_errors=errors,
        activated_at=None,
    )


def _rejected_cost_price_result(dataset_version: str, errors: list[str]) -> CostPriceUploadResult:
    return CostPriceUploadResult(
        status="rejected",
        dataset_version=dataset_version,
        accepted_counts=CostPriceUploadAcceptedCounts(cost_price_rows=0),
        validation_errors=errors,
        activated_at=None,
    )


def _validate_timestamp(value: str, field_name: str) -> None:
    if not value.endswith("Z"):
        raise ValueError(f"{field_name} must be an ISO 8601 UTC timestamp ending with Z")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid ISO 8601 timestamp") from exc


def _validate_optional_timestamp(value: str | None, field_name: str) -> None:
    if value is None:
        return
    _validate_timestamp(value, field_name=field_name)


def _normalize_required_storage_key(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    if len(normalized) > 160:
        raise ValueError(f"{field_name} is too long")
    return normalized


def _normalize_username_storage_value(value: str) -> str:
    return str(value or "").strip().lower()


def _validate_iso_date(value: str, field_name: str) -> None:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid ISO 8601 date") from exc


def _validate_optional_iso_date(value: Any, field_name: str) -> None:
    normalized = str(value or "").strip()
    if not normalized:
        return
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
        raise ValueError(f"{field_name} must be a valid ISO 8601 date YYYY-MM-DD or blank")
    _validate_iso_date(normalized, field_name=field_name)


def _validated_choice(value: str, allowed: set[str], *, field_name: str) -> str:
    normalized = str(value or "").strip()
    if normalized not in allowed:
        raise ValueError(f"unsupported {field_name}: {normalized}")
    return normalized


def _validate_month(value: str, field_name: str) -> None:
    try:
        date.fromisoformat(f"{value}-01")
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid ISO 8601 month YYYY-MM") from exc


def _sheet_row_counts_from_plan(plan: SheetVitrinaV1Envelope) -> dict[str, int]:
    return {item.sheet_name: item.row_count for item in plan.sheets}


def _derive_sheet_vitrina_refresh_semantic_summary(plan: SheetVitrinaV1Envelope) -> dict[str, Any]:
    status_sheet = next((item for item in plan.sheets if item.sheet_name == "STATUS"), None)
    if status_sheet is None:
        return {
            "status": "warning",
            "label": _semantic_label("warning"),
            "tone": "warning",
            "reason": "STATUS-таблица в persisted snapshot отсутствует; semantic result не подтверждён.",
            "counts": {"success": 0, "warning": 1, "error": 0},
            "sources": [],
        }

    source_slots: dict[str, list[dict[str, Any]]] = {}
    seen_source_order: list[str] = []
    for row in status_sheet.rows:
        if len(row) < 11:
            continue
        raw_source_key = str(row[0] or "").strip()
        if not raw_source_key or raw_source_key in {
            "registry_upload_current_state",
            "sheet_vitrina_v1_temporal_live_v1",
        }:
            continue
        source_key, temporal_slot = _split_temporal_source_key(raw_source_key)
        if not source_key:
            continue
        if source_key not in source_slots:
            seen_source_order.append(source_key)
        source_slots.setdefault(source_key, []).append(
            _derive_sheet_vitrina_source_slot_outcome(
                source_key=source_key,
                temporal_slot=temporal_slot,
                row=row,
            )
        )

    effective_policies = effective_source_temporal_policies(plan.source_temporal_policies)
    source_order = list(dict.fromkeys([*effective_policies.keys(), *seen_source_order]))
    source_outcomes: list[dict[str, Any]] = []
    counts = {"success": 0, "warning": 0, "error": 0}
    for source_key in source_order:
        slot_outcomes = sorted(
            source_slots.get(source_key, []),
            key=lambda item: _slot_sort_key(str(item.get("temporal_slot") or "")),
        )
        effective_policy = effective_policies.get(source_key, "")
        if not slot_outcomes:
            reduction = reduce_source_temporal_semantics(
                source_key=source_key,
                temporal_policy=effective_policy,
                slot_outcomes=[],
            )
            source_outcome = {
                "source_key": source_key,
                "status": str(reduction["status"]),
                "tone": str(reduction["status"]),
                "label": _semantic_label(str(reduction["status"])),
                "reason": str(reduction["reason"] or "persisted STATUS не содержит итог по источнику"),
                "slots": [],
            }
        else:
            reduction = reduce_source_temporal_semantics(
                source_key=source_key,
                temporal_policy=effective_policy,
                slot_outcomes=slot_outcomes,
            )
            source_outcome = {
                "source_key": source_key,
                "status": str(reduction["status"]),
                "tone": str(reduction["status"]),
                "label": _semantic_label(str(reduction["status"])),
                "reason": str(reduction["reason"]),
                "slots": slot_outcomes,
            }
        counts[str(source_outcome["status"])] += 1
        source_outcomes.append(source_outcome)

    if not source_outcomes:
        return {
            "status": "warning",
            "label": _semantic_label("warning"),
            "tone": "warning",
            "reason": "Persisted STATUS не содержит source rows; semantic refresh не подтверждён.",
            "counts": {"success": 0, "warning": 1, "error": 0},
            "sources": [],
        }

    overall_status = _reduce_semantic_status([item["status"] for item in source_outcomes])
    return {
        "status": overall_status,
        "label": _semantic_label(overall_status),
        "tone": overall_status,
        "reason": _compose_overall_reason(counts, source_outcomes),
        "counts": counts,
        "sources": source_outcomes,
    }


def _derive_sheet_vitrina_source_slot_outcome(
    *,
    source_key: str,
    temporal_slot: str,
    row: list[Any],
) -> dict[str, Any]:
    kind = str(row[1] or "").strip().lower()
    freshness = str(row[2] or "").strip()
    snapshot_date = str(row[3] or "").strip()
    requested_count = _coerce_int(row[7])
    covered_count = _coerce_int(row[8])
    raw_note = str(row[10] or "").strip()
    status = _semantic_status_from_source_slot(
        kind=kind,
        requested_count=requested_count,
        covered_count=covered_count,
        note=raw_note,
    )
    reason = _semantic_reason_from_source_slot(
        kind=kind,
        requested_count=requested_count,
        covered_count=covered_count,
        note=raw_note,
        freshness=freshness,
        snapshot_date=snapshot_date,
    )
    return {
        "source_key": source_key,
        "temporal_slot": temporal_slot or "snapshot",
        "status": status,
        "tone": status,
        "label": _semantic_label(status),
        "kind": kind,
        "freshness": freshness,
        "snapshot_date": snapshot_date,
        "date": str(row[4] or "").strip(),
        "date_from": str(row[5] or "").strip(),
        "date_to": str(row[6] or "").strip(),
        "requested_count": requested_count,
        "covered_count": covered_count,
        "missing_nm_ids": _parse_missing_nm_ids(row[9]),
        "note": raw_note,
        "reason": reason,
        "status_line": f"{_slot_label(temporal_slot)}: {reason}",
    }


def _semantic_status_from_source_slot(
    *,
    kind: str,
    requested_count: int,
    covered_count: int,
    note: str,
) -> str:
    if kind in {"error", "closure_exhausted"}:
        return "error"
    if kind in {
        "missing",
        "incomplete",
        "not_available",
        "blocked",
        "closure_pending",
        "closure_retrying",
        "closure_rate_limited",
        "not_found",
    }:
        return "warning"
    if kind != "success":
        return "warning"
    if _note_requires_warning(note):
        return "warning"
    if requested_count > 0 and covered_count < requested_count:
        return "warning"
    return "success"


def _semantic_reason_from_source_slot(
    *,
    kind: str,
    requested_count: int,
    covered_count: int,
    note: str,
    freshness: str,
    snapshot_date: str,
) -> str:
    mapped_reason = _humanize_status_note(note)
    if kind == "closure_pending":
        return "closed-day snapshot ещё не готов; ожидается retry"
    if kind == "closure_retrying":
        return "closed-day snapshot ещё не принят; будет retry"
    if kind == "closure_rate_limited":
        return "источник ограничил запросы; retry запланирован"
    if kind == "closure_exhausted":
        return "retry для closed-day snapshot исчерпан"
    if kind == "blocked":
        return mapped_reason or "источник помечен как blocked в текущем contour"
    if kind == "not_available":
        return mapped_reason or "слот не обновлялся в текущем contour"
    if kind == "missing":
        return mapped_reason or "payload не materialized"
    if kind == "not_found":
        return mapped_reason or "источник не вернул данные на точную дату"
    if kind == "incomplete":
        return mapped_reason or _coverage_reason(requested_count=requested_count, covered_count=covered_count)
    if kind == "error":
        return mapped_reason or note or "источник завершился ошибкой"
    if requested_count > 0 and covered_count < requested_count:
        return mapped_reason or _coverage_reason(
            requested_count=requested_count,
            covered_count=covered_count,
        )
    if mapped_reason:
        return mapped_reason
    if freshness and snapshot_date and freshness != snapshot_date:
        return f"использована сохранённая дата {freshness}"
    return "обновлено"


def _humanize_status_note(note: str) -> str:
    normalized = str(note or "").strip()
    if not normalized:
        return ""
    if "zero_stock_stage_buckets=" in normalized:
        missing = _status_note_value(normalized, "zero_stock_stage_buckets")
        bucket_text = f": {missing}" if missing else ""
        return (
            f"1C source свежий: stage bucket{bucket_text} отсутствует по active SKU; "
            "трактуется как нулевой остаток"
        )
    if "missing_stage_buckets=" in normalized:
        missing = _status_note_value(normalized, "missing_stage_buckets")
        bucket_text = f": {missing}" if missing else ""
        fallback = _status_note_value(normalized, "accepted_fallback_stage_buckets")
        if fallback:
            return (
                f"1C не вернула stage bucket{bucket_text}; "
                "строки bucket заполнены из ранее принятой server-side версии"
            )
        return f"1C не вернула stage bucket{bucket_text}; строки bucket оставлены blank без fake zeros"
    replacements = (
        (
            "seller_portal_session_invalid",
            "сессия seller portal больше не действует; требуется повторный вход",
        ),
        (
            "seller_portal_session_missing",
            "сессия seller portal отсутствует; требуется повторный вход",
        ),
        (
            "source is not available for today_current in the bounded live contour; today column stays blank instead of inventing fresh values",
            "текущий день для этого источника не требуется",
        ),
        (
            "source is current-only in the bounded live contour; yesterday_closed is left blank instead of backfilling current values into a closed-day column",
            "закрытый день для этого источника materialize-ится только через current-rollover",
        ),
        (
            "current-snapshot-only yesterday_closed requires a prior accepted current snapshot for requested date; endpoint has no historical date parameter, so current values are not backfilled into a closed-day column",
            "нет ранее принятого current snapshot для этой даты; текущие значения не подставлены в закрытый день",
        ),
        (
            "resolution_rule=accepted_closed_preserved_after_invalid_attempt",
            "сохранён ранее принятый closed snapshot после невалидной попытки",
        ),
        (
            "resolution_rule=accepted_current_preserved_after_invalid_attempt",
            "сохранён ранее принятый current snapshot после невалидной попытки",
        ),
        (
            "resolution_rule=accepted_closed_from_prior_current_snapshot",
            "использован ранее принятый current snapshot предыдущего дня",
        ),
        (
            "resolution_rule=accepted_closed_from_prior_current_cache",
            "использован ранее принятый current snapshot из runtime cache",
        ),
        (
            "resolution_rule=accepted_closed_runtime_snapshot",
            "использован ранее принятый closed-day snapshot",
        ),
        (
            "resolution_rule=accepted_closed_from_interval_replay",
            "использован сохранённый closed-day snapshot из interval replay",
        ),
        (
            "resolution_rule=accepted_prior_current_runtime_cache",
            "использован сохранённый current snapshot из runtime cache",
        ),
        (
            "resolution_rule=exact_date_stocks_history_runtime_cache",
            "использован exact-date runtime cache по stocks",
        ),
        (
            "resolution_rule=exact_date_promo_current_runtime_cache",
            "использован exact-date runtime cache по promo",
        ),
        (
            "resolution_rule=exact_date_runtime_cache",
            "использован exact-date runtime cache",
        ),
        (
            "invalid_exact_snapshot=zero_filled_seller_funnel_snapshot",
            "нулевой seller_funnel snapshot отклонён",
        ),
        (
            "invalid_exact_snapshot=zero_filled_web_source_snapshot",
            "нулевой web_source snapshot отклонён",
        ),
        (
            "invalid_exact_snapshot=zero_filled_prices_snapshot",
            "нулевой prices snapshot отклонён",
        ),
        (
            "invalid_exact_snapshot=zero_filled_ads_bids_snapshot",
            "нулевой ads_bids snapshot отклонён",
        ),
        (
            "invalid_exact_snapshot=promo_live_source_incomplete",
            "promo snapshot отклонён как incomplete",
        ),
        ("no payload returned", "источник не вернул payload"),
        (
            "resolution_rule=latest_effective_from<=slot_date",
            "",
        ),
    )
    for marker, message in replacements:
        if marker in normalized:
            return message
    if "closure_state=closure_retrying" in normalized:
        return "closed-day snapshot ещё не принят; будет retry"
    if "closure_state=closure_pending" in normalized:
        return "closed-day snapshot ожидает retry"
    if "closure_state=closure_rate_limited" in normalized:
        return "источник ограничил запросы; retry запланирован"
    if "closure_state=closure_exhausted" in normalized:
        return "retry для closed-day snapshot исчерпан"
    return normalized


def _status_note_value(note: str, key: str) -> str:
    prefix = f"{key}="
    for part in str(note or "").split(";"):
        text = part.strip()
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return ""


def _note_requires_warning(note: str) -> bool:
    normalized = str(note or "").strip()
    if not normalized:
        return False
    if _status_note_is_unverified_closed_day_fallback(normalized):
        return True
    if _status_note_is_latest_confirmed(normalized):
        return False
    success_markers = {
        "resolution_rule=accepted_closed_current_attempt",
        "resolution_rule=accepted_current_current_attempt",
        "resolution_rule=latest_effective_from<=slot_date",
    }
    if any(marker in normalized for marker in success_markers):
        return False
    warning_markers = {
        "runtime_cache",
        "preserved_after_invalid_attempt",
        "resolution_rule=accepted_closed_from_",
        "resolution_rule=accepted_closed_runtime_snapshot",
        "resolution_rule=accepted_prior_current_runtime_cache",
    }
    return any(marker in normalized for marker in warning_markers)


def _status_note_is_latest_confirmed(note: str) -> bool:
    normalized = str(note or "").strip().lower()
    if not normalized:
        return False
    if _status_note_is_unverified_closed_day_fallback(normalized):
        return False
    latest_confirmed_tokens = (
        "latest_confirmed",
        "fallback",
        "runtime_cache",
        "accepted_closed_runtime_snapshot",
        "accepted_current_runtime_snapshot",
        "accepted_closed_from_prior_current_snapshot",
        "accepted_closed_from_prior_current_cache",
        "accepted_prior_current_runtime_cache",
        "exact_date_provisional_runtime_cache",
        "accepted_closed_from_interval_replay",
        "accepted_current_from_prior",
        "accepted_closed_preserved_after_invalid_attempt",
        "accepted_current_preserved_after_invalid_attempt",
        "exact_date_stocks_history_runtime_cache",
        "exact_date_promo_current_runtime_cache",
        "exact_date_runtime_cache",
    )
    return any(token in normalized for token in latest_confirmed_tokens)


def _status_note_is_unverified_closed_day_fallback(note: str) -> bool:
    normalized = str(note or "").strip().lower()
    return "accepted_current_from_prior_closed_day_latest_confirmed" in normalized


def _compose_source_reason(slot_outcomes: list[dict[str, Any]]) -> str:
    if not slot_outcomes:
        return "persisted STATUS не содержит слот-итогов"
    return " · ".join(str(item["status_line"]) for item in slot_outcomes)


def _compose_overall_reason(
    counts: Mapping[str, int],
    source_outcomes: list[Mapping[str, Any]],
) -> str:
    total_sources = len(source_outcomes)
    if counts.get("error", 0):
        return (
            f"Ошибки по {counts['error']} из {total_sources} источников; "
            f"ещё {counts.get('warning', 0)} требуют внимания."
        )
    if counts.get("warning", 0):
        return f"{counts['warning']} из {total_sources} источников требуют внимания."
    return f"Все {total_sources} источников соответствуют ожидаемой temporal model."


def _coverage_reason(*, requested_count: int, covered_count: int) -> str:
    if requested_count <= 0:
        return "покрытие не подтверждено"
    if covered_count <= 0:
        return f"нет покрытия по {requested_count} позициям"
    return f"покрыто {covered_count} из {requested_count}"


def _semantic_label(status: str) -> str:
    if status == "success":
        return "Успешно"
    if status == "error":
        return "Ошибка"
    return "Внимание"


def _reduce_semantic_status(statuses: list[str]) -> str:
    if any(status == "error" for status in statuses):
        return "error"
    if any(status == "warning" for status in statuses):
        return "warning"
    return "success"


def _split_temporal_source_key(source_key: str) -> tuple[str, str]:
    normalized = str(source_key or "").strip()
    if normalized.endswith("]") and "[" in normalized:
        name, slot = normalized[:-1].split("[", 1)
        return name, slot
    return normalized, ""


def _slot_sort_key(slot: str) -> tuple[int, str]:
    if slot == "yesterday_closed":
        return (0, slot)
    if slot == "today_current":
        return (1, slot)
    if slot == "snapshot":
        return (2, slot)
    return (3, slot)


def _slot_label(slot: str) -> str:
    if slot == "yesterday_closed":
        return "вчера"
    if slot == "today_current":
        return "сегодня"
    if slot == "snapshot":
        return "snapshot"
    return slot or "snapshot"


def _coerce_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(text)
    except ValueError:
        return 0


def _parse_missing_nm_ids(value: Any) -> list[int]:
    raw = str(value or "").strip()
    if not raw:
        return []
    out: list[int] = []
    for item in raw.split(","):
        item_text = item.strip()
        if not item_text:
            continue
        try:
            out.append(int(item_text))
        except ValueError:
            continue
    return out


def _serialize_sheet_vitrina_plan(plan: SheetVitrinaV1Envelope) -> str:
    payload = {
        "plan_version": plan.plan_version,
        "snapshot_id": plan.snapshot_id,
        "as_of_date": plan.as_of_date,
        "date_columns": plan.date_columns,
        "temporal_slots": [
            {
                "slot_key": item.slot_key,
                "slot_label": item.slot_label,
                "column_date": item.column_date,
            }
            for item in plan.temporal_slots
        ],
        "source_temporal_policies": plan.source_temporal_policies,
        "metadata": _to_jsonable(dict(getattr(plan, "metadata", {}) or {})),
        "sheets": [
            {
                "sheet_name": item.sheet_name,
                "write_start_cell": item.write_start_cell,
                "write_rect": item.write_rect,
                "clear_range": item.clear_range,
                "write_mode": item.write_mode,
                "partial_update_allowed": item.partial_update_allowed,
                "header": item.header,
                "rows": item.rows,
                "row_count": item.row_count,
                "column_count": item.column_count,
            }
            for item in plan.sheets
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def _sheet_vitrina_plan_digest(plan: SheetVitrinaV1Envelope) -> str:
    payload = json.loads(_serialize_sheet_vitrina_plan(plan))
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _deserialize_sheet_vitrina_plan(raw_value: str) -> SheetVitrinaV1Envelope:
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError as exc:  # pragma: no cover - persisted data corruption guard
        raise ValueError("sheet_vitrina_v1 ready snapshot contains invalid JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("sheet_vitrina_v1 ready snapshot must contain a JSON object")
    plan = parse_sheet_write_plan_payload(payload)
    effective_policies = effective_source_temporal_policies(plan.source_temporal_policies)
    if effective_policies == plan.source_temporal_policies:
        if isinstance(payload.get("metadata"), Mapping):
            return SheetVitrinaV1Envelope(
                plan_version=plan.plan_version,
                snapshot_id=plan.snapshot_id,
                as_of_date=plan.as_of_date,
                date_columns=plan.date_columns,
                temporal_slots=plan.temporal_slots,
                source_temporal_policies=plan.source_temporal_policies,
                sheets=plan.sheets,
                metadata=dict(payload.get("metadata") or {}),
            )
        return plan
    return SheetVitrinaV1Envelope(
        plan_version=plan.plan_version,
        snapshot_id=plan.snapshot_id,
        as_of_date=plan.as_of_date,
        date_columns=plan.date_columns,
        temporal_slots=plan.temporal_slots,
        source_temporal_policies=effective_policies,
        sheets=plan.sheets,
        metadata=dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), Mapping) else {},
    )


def _serialize_optional_state_payload(payload: Mapping[str, Any] | None) -> str | None:
    if payload is None:
        return None
    return json.dumps(_to_jsonable(dict(payload)), ensure_ascii=False)


def _deserialize_optional_state_payload(payload_json: str | None) -> dict[str, Any] | None:
    if not payload_json:
        return None
    payload = json.loads(payload_json)
    if not isinstance(payload, dict):
        raise ValueError("state payload must contain a JSON object")
    return payload


def _serialize_temporal_source_payload(payload: Any) -> str:
    return json.dumps(_to_jsonable(payload), ensure_ascii=False)


def _deserialize_temporal_source_payload(payload_json: str) -> Any:
    return _to_namespace(json.loads(payload_json))


def _supplier_shipment_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return apply_derived_supplier_status({
        "shipment_id": row["shipment_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "shipment_date": row["shipment_date"],
        "planned_shipment_date": row["shipment_date"],
        "actual_shipment_date": row["actual_shipment_date"] or "",
        "actual_ff_acceptance_date": row["actual_ff_acceptance_date"] or "",
        "historical_status_exception": row["historical_status_exception"] or "",
        "order_status": row["order_status"] or ORDER_STATUS_DEFAULT,
        "target_facility_id": row["target_facility_id"] or "",
        "target_facility_name": row["target_facility_name"] or "",
        "expenses_complete": bool(row["expenses_complete"]),
        "invoice_no": row["invoice_no"] or "",
        "invoice_date": row["invoice_date"] or "",
        "contract_no": row["contract_no"] or "",
        "contract_date": row["contract_date"] or "",
        "supplier_name": row["supplier_name"] or "",
        "currency": row["currency"] or "",
        "approx_yuan_rate": row["approx_yuan_rate"],
        "cny_ledger_effective_rate": row["cny_ledger_effective_rate"] or "",
        "cny_payment_currency_rub_cost": row["cny_payment_currency_rub_cost"] or "",
        "cny_paid_amount": row["cny_paid_amount"] or "",
        "cny_bank_fee_rub": row["cny_bank_fee_rub"] or "",
        "cny_calculation_status": row["cny_calculation_status"] or "",
        "cny_calculation_error": row["cny_calculation_error"] or "",
        "cny_calculated_at": row["cny_calculated_at"] or "",
        "product_qty_total": row["product_qty_total"],
        "product_amount_total": row["product_amount_total"],
        "extras_amount_total": row["extras_amount_total"],
        "invoice_amount_total": row["invoice_amount_total"],
        "match_status": row["match_status"],
        "source_filename": row["source_filename"] or "",
        "source_file_sha256": row["source_file_sha256"] or "",
        "source_file_path": row["source_file_path"] or "",
        "invoice_document_id": row["invoice_document_id"] or "",
        "archived_at": row["archived_at"] or "",
        "archive_event_id": row["archive_event_id"] or "",
        "archive_reason": row["archive_reason"] or "",
        "archive_actor": row["archive_actor"] or "",
    })


def _supplier_shipment_header_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return apply_derived_supplier_status({
        "shipment_id": row["shipment_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "shipment_date": row["shipment_date"],
        "planned_shipment_date": row["shipment_date"],
        "actual_shipment_date": row["actual_shipment_date"] or "",
        "actual_ff_acceptance_date": row["actual_ff_acceptance_date"] or "",
        "historical_status_exception": row["historical_status_exception"] or "",
        "order_status": row["order_status"] or ORDER_STATUS_DEFAULT,
        "target_facility_id": row["target_facility_id"] or "",
        "target_facility_name": row["target_facility_name"] or "",
        "expenses_complete": bool(row["expenses_complete"]),
        "invoice_no": row["invoice_no"] or "",
        "invoice_date": row["invoice_date"] or "",
        "contract_no": row["contract_no"] or "",
        "contract_date": row["contract_date"] or "",
        "supplier_name": row["supplier_name"] or "",
        "customer_name": row["customer_name"] or "",
        "currency": row["currency"] or "",
        "approx_yuan_rate": row["approx_yuan_rate"],
        "cny_ledger_effective_rate": row["cny_ledger_effective_rate"] or "",
        "cny_payment_currency_rub_cost": row["cny_payment_currency_rub_cost"] or "",
        "cny_paid_amount": row["cny_paid_amount"] or "",
        "cny_bank_fee_rub": row["cny_bank_fee_rub"] or "",
        "cny_calculation_status": row["cny_calculation_status"] or "",
        "cny_calculation_error": row["cny_calculation_error"] or "",
        "cny_calculated_at": row["cny_calculated_at"] or "",
        "product_qty_total": row["product_qty_total"],
        "product_amount_total": row["product_amount_total"],
        "extras_amount_total": row["extras_amount_total"],
        "invoice_amount_total": row["invoice_amount_total"],
        "declared_invoice_total": row["declared_invoice_total"],
        "match_status": row["match_status"],
        "source_filename": row["source_filename"] or "",
        "source_file_sha256": row["source_file_sha256"] or "",
        "source_file_path": row["source_file_path"] or "",
        "invoice_document_id": row["invoice_document_id"] or "",
        "archived_at": row["archived_at"] or "",
        "archive_event_id": row["archive_event_id"] or "",
        "archive_reason": row["archive_reason"] or "",
        "archive_actor": row["archive_actor"] or "",
        "parser_version": row["parser_version"] or "",
        "warnings": _loads_json_list(row["warnings_json"]),
        "errors": _loads_json_list(row["errors_json"]),
    })


def _supplier_shipment_line_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    raw_payload = _loads_json_object(row["raw_json"])
    return {
        "line_id": row["line_id"],
        "line_type": row["line_type"],
        "sort_order": row["sort_order"],
        "source_no": row["source_no"] or "",
        "barcode": row["barcode"] or "",
        "product_type": row["product_type"] or "",
        "source_product_type": str(raw_payload.get("source_product_type") or ""),
        "model_raw": row["model_raw"] or "",
        "model_normalized": row["model_normalized"] or "",
        "match_key": row["match_key"] or "",
        "source_match_key": str(raw_payload.get("source_match_key") or ""),
        "internal_sku": row["internal_sku"] or "",
        "internal_nm_id": row["internal_nm_id"],
        "internal_name": row["internal_name"] or "",
        "qty": row["qty"],
        "unit_price": row["unit_price"],
        "amount": row["amount"],
        "currency": row["currency"] or "",
        "comment": row["comment"] or "",
        "match_status": row["match_status"] or "",
        "manual_override": bool(row["manual_override"]),
        "match_evidence": dict(raw_payload.get("match_evidence") or {}),
        "invoice_price_yuan_snapshot": row["invoice_price_yuan_snapshot"],
        "reference_purchase_price_yuan_snapshot": row["reference_purchase_price_yuan_snapshot"],
        "price_conformity_status": row["price_conformity_status"] or "not_checked",
        "price_conformity_checked_at": row["price_conformity_checked_at"] or "",
        "price_conformity_check_mode": row["price_conformity_check_mode"] or "not_checked",
        "price_conformity_reason": row["price_conformity_reason"] or "not_checked",
        "price_conformity_actor": row["price_conformity_actor"] or "",
        "price_conformity_context": _loads_json_object(row["price_conformity_context_json"]),
        "raw": raw_payload,
    }


def _supplier_financial_document_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "document_id": row["document_id"],
        "supplier_order_id": row["supplier_order_id"],
        "order_id": row["supplier_order_id"],
        "document_type": row["document_type"] or "",
        "original_filename": row["original_filename"] or "",
        "stored_file_path": row["stored_file_path"] or "",
        "file_content_type": row["file_content_type"] or "",
        "file_sha256": row["file_sha256"] or "",
        "uploaded_at": row["uploaded_at"],
        "updated_at": row["updated_at"],
        "parse_status": row["parse_status"] or "",
        "vendor": row["vendor"] or "",
        "document_number": row["document_number"] or "",
        "document_date": row["document_date"] or "",
        "currency": row["currency"] or "",
        "total_amount": row["total_amount"],
        "total_amount_rub": row["total_amount_rub"],
        "vat_rate": row["vat_rate"],
        "vat_amount_rub": row["vat_amount_rub"],
        "due_date": row["due_date"] or "",
        "route": row["route"] or "",
        "contract_ref": row["contract_ref"] or "",
        "cbr_usd_rate_requested_date": row["cbr_usd_rate_requested_date"] or "",
        "cbr_usd_rate_effective_date": row["cbr_usd_rate_effective_date"] or "",
        "cbr_usd_rate_value": row["cbr_usd_rate_value"],
        "rate_source": row["rate_source"] or "",
        "rate_source_status": row["rate_source_status"] or "",
        "raw_parse": _loads_json_object(row["raw_parse_json"]),
        "normalized_parse": _loads_json_object(row["normalized_parse_json"]),
        "parser_version": row["parser_version"] or "",
        "warnings": _loads_json_list(row["warnings_json"]),
        "errors": _loads_json_list(row["errors_json"]),
    }


def _supplier_financial_expense_line_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "line_id": row["line_id"],
        "financial_document_id": row["financial_document_id"],
        "supplier_order_id": row["supplier_order_id"],
        "order_id": row["supplier_order_id"],
        "sort_order": row["sort_order"],
        "category": row["category"] or "",
        "stage": row["stage"] or "",
        "description": row["description"] or "",
        "amount": row["amount"],
        "currency": row["currency"] or "",
        "amount_rub": row["amount_rub"],
        "vat_rate": row["vat_rate"],
        "vat_amount_rub": row["vat_amount_rub"],
        "included_in_logistics_efficiency": bool(row["included_in_logistics_efficiency"]),
        "included_in_customs_total": bool(row["included_in_customs_total"]),
        "status": row["status"] or "",
        "confidence": row["confidence"],
        "raw": _loads_json_object(row["raw_json"]),
    }


def _save_cny_document_in_connection(
    conn: sqlite3.Connection,
    document: Mapping[str, Any],
) -> str:
    """Upsert one validated CNY document in the caller-owned transaction."""

    document_id = str(document.get("document_id") or "").strip()
    if not document_id:
        raise ValueError("CNY document_id is required")
    document_type = str(document.get("document_type") or "").strip()
    if document_type not in CNY_DOCUMENT_TYPES:
        raise ValueError(f"unsupported CNY document_type: {document_type}")
    status = str(
        document.get("status") or CNY_DOCUMENT_STATUS_POSTED
    ).strip()
    if status not in CNY_DOCUMENT_STATUSES:
        raise ValueError(f"unsupported CNY document status: {status}")
    uploaded_at = str(
        document.get("uploaded_at") or document.get("created_at") or ""
    ).strip()
    created_at = str(document.get("created_at") or uploaded_at).strip()
    updated_at = str(document.get("updated_at") or uploaded_at).strip()
    _validate_timestamp(uploaded_at, field_name="uploaded_at")
    _validate_timestamp(created_at, field_name="created_at")
    _validate_timestamp(updated_at, field_name="updated_at")
    operation_date = str(document.get("operation_date") or "").strip()
    if operation_date:
        _validate_iso_date(operation_date, field_name="operation_date")
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_cny_documents(
            document_id,document_type,source,source_order_id,context_order_id,
            linked_financial_document_id,original_filename,stored_file_path,
            file_content_type,file_sha256,natural_key,uploaded_at,created_at,
            updated_at,operation_date,operation_datetime,status,document_number,
            currency,rub_amount,cny_amount,bank_rate,parsed_payload_json,
            raw_parse_json,parser_version,warnings_json,errors_json
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(document_id) DO UPDATE SET
            document_type=excluded.document_type,
            source=excluded.source,
            source_order_id=excluded.source_order_id,
            context_order_id=excluded.context_order_id,
            linked_financial_document_id=excluded.linked_financial_document_id,
            original_filename=excluded.original_filename,
            stored_file_path=excluded.stored_file_path,
            file_content_type=excluded.file_content_type,
            file_sha256=excluded.file_sha256,
            natural_key=excluded.natural_key,
            updated_at=excluded.updated_at,
            operation_date=excluded.operation_date,
            operation_datetime=excluded.operation_datetime,
            status=excluded.status,
            document_number=excluded.document_number,
            currency=excluded.currency,
            rub_amount=excluded.rub_amount,
            cny_amount=excluded.cny_amount,
            bank_rate=excluded.bank_rate,
            parsed_payload_json=excluded.parsed_payload_json,
            raw_parse_json=excluded.raw_parse_json,
            parser_version=excluded.parser_version,
            warnings_json=excluded.warnings_json,
            errors_json=excluded.errors_json
        """,
        (
            document_id,
            document_type,
            str(document.get("source") or ""),
            str(document.get("source_order_id") or ""),
            str(document.get("context_order_id") or ""),
            str(document.get("linked_financial_document_id") or ""),
            str(document.get("original_filename") or ""),
            str(document.get("stored_file_path") or ""),
            str(document.get("file_content_type") or ""),
            str(document.get("file_sha256") or ""),
            str(document.get("natural_key") or ""),
            uploaded_at,
            created_at,
            updated_at,
            operation_date,
            str(document.get("operation_datetime") or ""),
            status,
            str(document.get("document_number") or ""),
            str(document.get("currency") or ""),
            str(document.get("rub_amount") or ""),
            str(document.get("cny_amount") or ""),
            str(document.get("bank_rate") or ""),
            json.dumps(
                dict(document.get("parsed_payload") or {}),
                ensure_ascii=False,
            ),
            json.dumps(
                dict(document.get("raw_parse") or {}),
                ensure_ascii=False,
            ),
            str(document.get("parser_version") or ""),
            json.dumps(
                list(document.get("warnings") or []),
                ensure_ascii=False,
            ),
            json.dumps(
                list(document.get("errors") or []),
                ensure_ascii=False,
            ),
        ),
    )
    return document_id


def _cny_document_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "document_id": row["document_id"],
        "id": row["document_id"],
        "document_type": row["document_type"] or "",
        "source": row["source"] or "",
        "source_order_id": row["source_order_id"] or "",
        "context_order_id": row["context_order_id"] or "",
        "linked_financial_document_id": row["linked_financial_document_id"] or "",
        "original_filename": row["original_filename"] or "",
        "stored_file_path": row["stored_file_path"] or "",
        "file_content_type": row["file_content_type"] or "",
        "file_sha256": row["file_sha256"] or "",
        "natural_key": row["natural_key"] or "",
        "uploaded_at": row["uploaded_at"] or "",
        "created_at": row["created_at"] or "",
        "updated_at": row["updated_at"] or "",
        "operation_date": row["operation_date"] or "",
        "operation_datetime": row["operation_datetime"] or "",
        "status": row["status"] or "",
        "parse_status": row["status"] or "",
        "document_number": row["document_number"] or "",
        "currency": row["currency"] or "",
        "rub_amount": row["rub_amount"] or "",
        "cny_amount": row["cny_amount"] or "",
        "bank_rate": row["bank_rate"] or "",
        "parsed_payload": _loads_json_object(row["parsed_payload_json"]),
        "raw_parse": _loads_json_object(row["raw_parse_json"]),
        "parser_version": row["parser_version"] or "",
        "warnings": _loads_json_list(row["warnings_json"]),
        "errors": _loads_json_list(row["errors_json"]),
    }


def _cny_ledger_operation_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "operation_id": row["operation_id"],
        "id": row["operation_id"],
        "operation_type": row["operation_type"] or "",
        "source_document_id": row["source_document_id"] or "",
        "source_order_id": row["source_order_id"] or "",
        "operation_date": row["operation_date"] or "",
        "operation_datetime": row["operation_datetime"] or "",
        "sequence_key": row["sequence_key"] or "",
        "cny_delta": row["cny_delta"] or "",
        "rub_value_delta": row["rub_value_delta"] or "",
        "effective_rate_before": row["effective_rate_before"] or "",
        "balance_cny_after": row["balance_cny_after"] or "",
        "balance_rub_value_after": row["balance_rub_value_after"] or "",
        "average_rate_after": row["average_rate_after"] or "",
        "status": row["status"] or "",
        "error_reason": row["error_reason"] or "",
        "created_at": row["created_at"] or "",
        "updated_at": row["updated_at"] or "",
    }


def _ff_stock_preview_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "preview_id": row["preview_id"],
        "operation_type": row["operation_type"] or "",
        "created_at": row["created_at"] or "",
        "uploaded_filename": row["uploaded_filename"] or "",
        "uploaded_content_type": row["uploaded_content_type"] or "",
        "source_file_sha256": row["source_file_sha256"] or "",
        "file_available": bool(row["file_available"]),
        "parsed_lines": _loads_json_list(row["parsed_lines_json"]),
        "summary": _loads_json_object(row["summary_json"]),
        "warnings": _loads_json_list(row["warnings_json"]),
        "errors": _loads_json_list(row["errors_json"]),
    }


def _ff_stock_operations_archive_where(
    *,
    include_technical_archive: bool,
    archive_cutoff_created_at: str = "",
) -> tuple[str, list[Any]]:
    if include_technical_archive:
        return "", []
    predicates = ["COALESCE(source_type, '') != 'runtime_repair'"]
    params: list[Any] = []
    cutoff = str(archive_cutoff_created_at or "").strip()
    if cutoff:
        predicates.append("COALESCE(created_at, '') > ?")
        params.append(cutoff)
    return "WHERE " + " AND ".join(predicates), params


def _ff_stock_operation_to_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    keys = set(row.keys())
    file_available = bool(row["file_available"]) if "file_available" in keys else bool(row["source_file_blob"])
    return {
        "operation_id": row["operation_id"],
        "operation_type": row["operation_type"] or "",
        "source_type": row["source_type"] or "",
        "source_key": row["source_key"] or "",
        "source_object_id": row["source_object_id"] or "",
        "source_object_label": row["source_object_label"] or "",
        "created_at": row["created_at"] or "",
        "business_effective_date": (
            (row["business_effective_date"] or "")
            if "business_effective_date" in keys
            else ""
        ),
        "created_by": row["created_by"] or "",
        "sku_count": int(row["sku_count"] or 0),
        "total_quantity_delta": float(row["total_quantity_delta"] or 0.0),
        "total_quantity_abs": float(row["total_quantity_abs"] or 0.0),
        "warnings": _loads_json_list(row["warnings_json"]),
        "diagnostics": _loads_json_object(row["diagnostics_json"]),
        "source_filename": row["source_filename"] or "",
        "source_content_type": row["source_content_type"] or "",
        "source_file_sha256": row["source_file_sha256"] or "",
        "file_available": file_available,
    }


def _ff_stock_wb_auto_writeoff_checkpoint_to_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {
        "slot": row["slot"] or "",
        "checkpoint_id": row["checkpoint_id"] or "",
        "created_at": row["created_at"] or "",
        "created_by": row["created_by"] or "",
        "reason": row["reason"] or "",
        "baseline_cache_keys": [str(item) for item in _loads_json_list(row["baseline_cache_keys_json"])],
        "baseline_source_keys": [str(item) for item in _loads_json_list(row["baseline_source_keys_json"])],
        "baseline_supply_ids": [str(item) for item in _loads_json_list(row["baseline_supply_ids_json"])],
        "baseline_record_count": int(row["baseline_record_count"] or 0),
        "watermark_source_created_at": row["watermark_source_created_at"] or "",
        "watermark_supply_date": row["watermark_supply_date"] or "",
        "diagnostics": _loads_json_object(row["diagnostics_json"]),
    }


def _ff_stock_operation_line_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "operation_id": row["operation_id"],
        "line_no": int(row["line_no"] or 0),
        "nm_id": int(row["nm_id"] or 0),
        "barcode": row["barcode"] or "",
        "sku": row["sku"] or "",
        "nomenclature_name": row["nomenclature_name"] or "",
        "comment": row["comment"] or "",
        "group_name": row["group_name"] or "",
        "quantity_delta": float(row["quantity_delta"] or 0.0),
        "raw": _loads_json_object(row["raw_json"]),
    }


def _first_existing_value(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping.get(key) is not None:
            return mapping.get(key)
    return None


def _wb_supply_row_values(row: Mapping[str, Any], synced_at: str) -> tuple[Any, ...]:
    supply_id = str(row.get("supply_id") or "").strip()
    if not supply_id:
        raise ValueError("WB supply_id is required")
    normalized_row = dict(row)
    raw_list = normalized_row.pop("raw_list", None)
    raw_detail = normalized_row.pop("raw_detail", None)
    raw_goods = normalized_row.pop("raw_goods", None)
    raw_package = normalized_row.pop("raw_package", None)
    return (
        supply_id,
        str(row.get("cache_key") or supply_id).strip(),
        str(row.get("wb_supply_id") or "").strip(),
        str(row.get("preorder_id") or "").strip(),
        json.dumps(normalized_row, ensure_ascii=False),
        json.dumps(raw_list, ensure_ascii=False) if raw_list is not None else None,
        json.dumps(raw_detail, ensure_ascii=False) if raw_detail is not None else None,
        json.dumps(raw_goods, ensure_ascii=False) if raw_goods is not None else None,
        json.dumps(raw_package, ensure_ascii=False) if raw_package is not None else None,
        str(row.get("raw_list_hash") or "").strip(),
        str(row.get("raw_detail_hash") or "").strip() or None,
        str(row.get("raw_goods_hash") or "").strip() or None,
        str(row.get("raw_package_hash") or "").strip() or None,
        str(row.get("warehouse_id") or "").strip(),
        row.get("status_id"),
        row.get("quantity_for_size_filter"),
        str(row.get("source_created_at") or "").strip(),
        str(row.get("supply_date") or "").strip(),
        str(row.get("fact_date") or "").strip(),
        str(row.get("updated_date") or "").strip(),
        synced_at,
        str(row.get("last_list_synced_at") or synced_at).strip(),
        str(row.get("last_enriched_at") or "").strip() or None,
        str(row.get("enrichment_status") or "not_requested").strip(),
        str(row.get("enrichment_error") or "").strip(),
    )


def _upsert_wb_supplies_sync_state(
    conn: sqlite3.Connection,
    *,
    last_synced_at: str,
    last_successful_sync_at: str | None,
    last_error: str,
    last_limit: int,
    last_offset: int,
    latest_synced_count: int,
    last_mode: str | None = None,
    latest_window_synced_at: str | None = None,
    latest_window_limit: int | None = None,
    latest_window_returned_count: int | None = None,
    may_have_more: bool | None = None,
    backfill_complete: bool | None = None,
    backfill_started_at: str | None = None,
    backfill_completed_at: str | None = None,
    highest_synced_offset: int | None = None,
    last_successful_offset: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_wb_supplies_sync_state(
            slot,
            last_synced_at,
            last_successful_sync_at,
            last_error,
            last_limit,
            last_offset,
            latest_synced_count,
            backfill_complete,
            backfill_started_at,
            backfill_completed_at,
            highest_synced_offset,
            last_successful_offset,
            last_mode,
            latest_window_synced_at,
            latest_window_limit,
            latest_window_returned_count,
            may_have_more
        )
        VALUES(1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(slot) DO UPDATE SET
            last_synced_at = excluded.last_synced_at,
            last_successful_sync_at = COALESCE(excluded.last_successful_sync_at, last_successful_sync_at),
            last_error = excluded.last_error,
            last_limit = excluded.last_limit,
            last_offset = excluded.last_offset,
            latest_synced_count = excluded.latest_synced_count,
            backfill_complete = COALESCE(excluded.backfill_complete, backfill_complete),
            backfill_started_at = COALESCE(excluded.backfill_started_at, backfill_started_at),
            backfill_completed_at = COALESCE(excluded.backfill_completed_at, backfill_completed_at),
            highest_synced_offset = COALESCE(excluded.highest_synced_offset, highest_synced_offset),
            last_successful_offset = COALESCE(excluded.last_successful_offset, last_successful_offset),
            last_mode = COALESCE(excluded.last_mode, last_mode),
            latest_window_synced_at = COALESCE(excluded.latest_window_synced_at, latest_window_synced_at),
            latest_window_limit = COALESCE(excluded.latest_window_limit, latest_window_limit),
            latest_window_returned_count = COALESCE(excluded.latest_window_returned_count, latest_window_returned_count),
            may_have_more = COALESCE(excluded.may_have_more, may_have_more)
        """,
        (
            last_synced_at,
            last_successful_sync_at,
            str(last_error or ""),
            int(last_limit or 0),
            int(last_offset or 0),
            int(latest_synced_count or 0),
            None if backfill_complete is None else (1 if backfill_complete else 0),
            backfill_started_at,
            backfill_completed_at,
            highest_synced_offset,
            last_successful_offset,
            last_mode,
            latest_window_synced_at,
            latest_window_limit,
            latest_window_returned_count,
            None if may_have_more is None else (1 if may_have_more else 0),
        ),
    )


def _wb_supplies_sync_run_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "mode": row["mode"],
        "status": row["status"],
        "phase": row["phase"] or "",
        "started_at": row["started_at"] or "",
        "updated_at": row["updated_at"] or "",
        "completed_at": row["completed_at"] or "",
        "offset": row["offset"],
        "limit": row["run_limit"],
        "pages_fetched": row["pages_fetched"],
        "raw_fetched": row["raw_fetched"],
        "upserted": row["upserted"],
        "new_rows": row["new_rows"],
        "changed_rows": row["changed_rows"],
        "unchanged_rows": row["unchanged_rows"],
        "enriched": row["enriched"],
        "failed_enrich": row["failed_enrich"],
        "may_have_more": bool(row["may_have_more"]),
        "last_error": row["last_error"] or "",
        "logs": _loads_json_list(row["logs_json"]),
    }


def _wb_supply_transit_cost_enrichment_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "supply_id": row["supply_id"],
        "amount": row["amount"],
        "currency": row["currency"] or "RUB",
        "amount_label": row["amount_label"] or "",
        "is_transit": bool(row["is_transit"]),
        "source": row["source"] or "",
        "evidence_type": row["evidence_type"] or "",
        "confidence": row["confidence"] or "",
        "fetched_at": row["fetched_at"] or "",
        "status": row["status"] or "",
        "error": row["error"] or "",
        "source_endpoint_path": row["source_endpoint_path"] or "",
        "created_at": row["created_at"] or "",
        "updated_at": row["updated_at"] or "",
        "last_attempt_status": row["last_attempt_status"] or row["status"] or "",
        "last_attempt_error": row["last_attempt_error"] or row["error"] or "",
        "last_attempt_at": row["last_attempt_at"] or row["updated_at"] or "",
        "source_revision": row["source_revision"] or "",
        "success_revision": int(row["success_revision"] or 0),
        "recalculation_status": row["recalculation_status"] or "",
        "recalculation_error": row["recalculation_error"] or "",
        "recalculation_updated_at": row["recalculation_updated_at"] or "",
    }


def _wb_supply_transit_cost_run_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "status": row["status"] or "",
        "phase": row["phase"] or "",
        "started_at": row["started_at"] or "",
        "updated_at": row["updated_at"] or "",
        "completed_at": row["completed_at"] or "",
        "candidate_count": row["candidate_count"] or 0,
        "processed_count": row["processed_count"] or 0,
        "success_count": row["success_count"] or 0,
        "not_found_count": row["not_found_count"] or 0,
        "failed_count": row["failed_count"] or 0,
        "session_expired_count": row["session_expired_count"] or 0,
        "auth_required_count": row["auth_required_count"] or 0,
        "route_unavailable_count": row["route_unavailable_count"] or 0,
        "collector_unavailable_count": row["collector_unavailable_count"] or 0,
        "lock_busy_count": row["lock_busy_count"] or 0,
        "last_error": row["last_error"] or "",
        "lock_status": _loads_json_object(row["lock_status_json"]),
        "logs": _loads_json_list(row["logs_json"]),
    }


def _nomenclature_item_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    barcode = str(row["barcode"] or "").strip()
    barcodes = [str(item) for item in _loads_json_list(row["barcodes_json"]) if str(item or "").strip()]
    if barcode and barcode not in barcodes:
        barcodes = [barcode, *barcodes]
    return {
        "item_id": row["item_id"],
        "is_active": bool(row["is_active"]),
        "is_hidden": bool(row["is_hidden"]),
        "hidden_at": row["hidden_at"] or "",
        "hidden_reason": row["hidden_reason"] or "",
        "our_sku": row["our_sku"] or "",
        "nm_id": row["nm_id"],
        "barcode": barcode,
        "primary_barcode": barcode,
        "barcodes": barcodes,
        "barcode_source": row["barcode_source"] or "missing",
        "barcode_status": row["barcode_status"] or ("ready" if barcode else "missing"),
        "barcode_ready": bool(barcode),
        "barcode_synced_at": row["barcode_synced_at"] or "",
        "barcode_updated_at": row["barcode_updated_at"] or "",
        "barcode_evidence": _loads_json_object(row["barcode_evidence_json"]),
        "vendor_code": row["vendor_code"] or "",
        "seller_article": row["vendor_code"] or "",
        "wb_title": row["wb_title"] or "",
        "wb_subject_name": row["wb_subject_name"] or "",
        "wb_updated_at": row["wb_updated_at"] or "",
        "wb_synced_at": row["wb_synced_at"] or "",
        "wb_sync_status": row["wb_sync_status"] or "",
        "wb_sync_evidence": _loads_json_object(row["wb_sync_evidence_json"]),
        "nomenclature_name": row["nomenclature_name"] or "",
        "product_type": row["product_type"] or "",
        "match_key": row["match_key"] or "",
        "purchase_price_yuan": row["purchase_price_yuan"],
        "factory_box_size": row["factory_box_size"],
        "aliases": [str(item) for item in _loads_json_list(row["aliases_json"]) if str(item or "").strip()],
        "compatible_models_text": row["compatible_models_text"] or "",
        "compatible_model_keys": [
            str(item) for item in _loads_json_list(row["compatible_model_keys_json"]) if str(item or "").strip()
        ],
        "comment": row["comment"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _sku_group_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "group_key": row["group_key"],
        "label": row["label"] or row["group_key"],
        "aliases": [str(item) for item in _loads_json_list(row["aliases_json"]) if str(item or "").strip()],
        "is_active": bool(row["is_active"]),
        "is_system": bool(row["is_system"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _trade_document_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    return {
        "document_id": row["document_id"],
        "document_type": row["document_type"] or "",
        "number": row["number"] or "",
        "document_date": row["document_date"] or "",
        "supplier_name": row["supplier_name"] or "",
        "currency": row["currency"] or "",
        "amount_total": row["amount_total"],
        "source": row["source"] or "",
        "source_shipment_id": row["source_shipment_id"] or "",
        "source_upload_id": row["source_upload_id"] or "",
        "file_original_name": row["file_original_name"] or "",
        "file_content_type": row["file_content_type"] or "",
        "file_sha256": row["file_sha256"] or "",
        "file_path": row["file_path"] or "",
        "parser_version": row["parser_version"] or "",
        "parsed_metadata": _loads_json_object(row["parsed_metadata_json"]),
        "warnings": _loads_json_list(row["warnings_json"]),
        "errors": _loads_json_list(row["errors_json"]),
        "status": row["status"] or "",
        "created_at": row["created_at"] or "",
        "updated_at": row["updated_at"] or "",
        "linked_contract_document_id": row["linked_contract_document_id"] if "linked_contract_document_id" in keys else "",
        "linked_contract_number": row["linked_contract_number"] if "linked_contract_number" in keys else "",
        "linked_contract_date": row["linked_contract_date"] if "linked_contract_date" in keys else "",
        "linked_invoice_count": int(row["linked_invoice_count"] if "linked_invoice_count" in keys else 0),
    }


def _loads_json_list(value: Any) -> list[Any]:
    try:
        payload = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


def _loads_json_object(value: Any) -> dict[str, Any]:
    try:
        payload = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _canonical_supply_calculation_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_json_sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical_supply_calculation_json(value).encode("utf-8")
    ).hexdigest()


def _normalize_supply_calculation_type(
    value: Any,
    *,
    allow_empty: bool = False,
) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "factory": "factory_order",
        "factory_order": "factory_order",
        "fbs_fulfillment_order": "fbs_fulfillment_order",
        "fulfillment_order": "fbs_fulfillment_order",
        "regional": "wb_regional",
        "wb_regional": "wb_regional",
    }
    if not normalized and allow_empty:
        return ""
    if normalized not in aliases:
        raise ValueError(
            "calculation_type must be factory_order, fbs_fulfillment_order or wb_regional"
        )
    return aliases[normalized]


def _save_supply_calculation_record(
    conn: sqlite3.Connection,
    *,
    calculation_type: str,
    calculated_at: str,
    payload: Mapping[str, Any],
    evidence: Mapping[str, Any] | None,
    export_bytes: bytes | None,
    export_filename: str | None,
    export_content_type: str | None,
    latest_table: str,
) -> bool:
    normalized_type = _normalize_supply_calculation_type(calculation_type)
    payload_dict = dict(payload)
    calculation_id = str(payload_dict.get("calculation_id") or "").strip()
    if not calculation_id:
        raise ValueError("calculation_id is required for immutable registry persistence")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", calculation_id):
        raise ValueError("calculation_id has invalid format")
    report_date = str(payload_dict.get("report_date") or "").strip()
    _validate_iso_date(report_date, field_name="report_date")
    payload_json = _canonical_supply_calculation_json(payload_dict)
    if len(payload_json.encode("utf-8")) > SUPPLY_CALCULATION_REGISTRY_MAX_PAYLOAD_BYTES:
        raise ValueError("calculation registry payload exceeds bounded storage limit")
    payload_sha256 = "sha256:" + hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    normalized_export_bytes = bytes(export_bytes or b"")
    if len(normalized_export_bytes) > SUPPLY_CALCULATION_REGISTRY_MAX_EXPORT_BYTES:
        raise ValueError("calculation registry export exceeds bounded storage limit")
    normalized_export_filename = str(export_filename or "").strip()
    normalized_export_content_type = str(export_content_type or "").strip()
    export_sha256 = (
        "sha256:" + hashlib.sha256(normalized_export_bytes).hexdigest()
        if normalized_export_bytes
        else ""
    )
    metadata = _build_supply_calculation_registry_metadata(
        calculation_type=normalized_type,
        payload=payload_dict,
        evidence=evidence,
        export_filename=normalized_export_filename,
        export_content_type=normalized_export_content_type,
        export_sha256=export_sha256,
    )
    metadata_json = _canonical_supply_calculation_json(metadata)
    if len(metadata_json.encode("utf-8")) > SUPPLY_CALCULATION_REGISTRY_MAX_METADATA_BYTES:
        raise ValueError("calculation registry metadata exceeds bounded storage limit")
    existing = conn.execute(
        """
        SELECT
            calculation_type,
            completeness,
            calculated_at,
            payload_sha256,
            metadata_json,
            export_sha256,
            export_filename,
            export_content_type
        FROM sheet_vitrina_v1_supply_calculation_registry
        WHERE record_id = ?
        """,
        (calculation_id,),
    ).fetchone()
    if existing is not None:
        exact_same = (
            str(existing["calculation_type"] or "") == normalized_type
            and str(existing["completeness"] or "") == "complete"
            and str(existing["calculated_at"] or "") == calculated_at
            and str(existing["payload_sha256"] or "") == payload_sha256
            and str(existing["metadata_json"] or "") == metadata_json
            and str(existing["export_sha256"] or "") == export_sha256
            and str(existing["export_filename"] or "") == normalized_export_filename
            and str(existing["export_content_type"] or "") == normalized_export_content_type
        )
        if not exact_same:
            raise ValueError(
                f"calculation_id collision for immutable registry record: {calculation_id}"
            )
        registry_inserted = False
    else:
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_supply_calculation_registry(
                record_id,
                calculation_id,
                calculation_type,
                completeness,
                calculated_at,
                report_date,
                status,
                payload_json,
                metadata_json,
                payload_sha256,
                export_filename,
                export_content_type,
                export_blob,
                export_sha256,
                source,
                source_id,
                created_at
            )
            VALUES(?, ?, ?, 'complete', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'calculation', ?, ?)
            """,
            (
                calculation_id,
                calculation_id,
                normalized_type,
                calculated_at,
                report_date,
                str(payload_dict.get("status") or ""),
                payload_json,
                metadata_json,
                payload_sha256,
                normalized_export_filename or None,
                normalized_export_content_type or None,
                sqlite3.Binary(normalized_export_bytes) if normalized_export_bytes else None,
                export_sha256 or None,
                calculation_id,
                calculated_at,
            ),
        )
        registry_inserted = True

    conn.execute(
        f"""
        INSERT INTO {latest_table}(
            slot,
            calculated_at,
            result_json
        )
        VALUES(1, ?, ?)
        ON CONFLICT(slot) DO UPDATE SET
            calculated_at = excluded.calculated_at,
            result_json = excluded.result_json
        """,
        (calculated_at, payload_json),
    )
    protected_record_ids = _latest_supply_calculation_record_ids(conn)
    protected_placeholders = ",".join("?" for _ in protected_record_ids)
    conn.execute(
        f"""
        DELETE FROM sheet_vitrina_v1_supply_calculation_registry
        WHERE completeness = 'complete'
          AND record_id NOT IN (
              SELECT record_id
              FROM sheet_vitrina_v1_supply_calculation_registry
              WHERE completeness = 'complete'
              ORDER BY
                  CASE
                      WHEN record_id IN ({protected_placeholders}) THEN 0
                      ELSE 1
                  END,
                  calculated_at DESC,
                  record_id DESC
              LIMIT ?
          )
        """,
        (
            *protected_record_ids,
            SUPPLY_CALCULATION_REGISTRY_MAX_COMPLETE_RECORDS,
        ),
    )
    return registry_inserted


def _latest_supply_calculation_record_ids(conn: sqlite3.Connection) -> tuple[str, ...]:
    record_ids: set[str] = set()
    for table_name in (
        "sheet_vitrina_v1_factory_order_result_state",
        "sheet_vitrina_v1_fbs_fulfillment_order_result_state",
        "sheet_vitrina_v1_wb_regional_supply_result_state",
    ):
        row = conn.execute(
            f"SELECT result_json FROM {table_name} WHERE slot = 1"
        ).fetchone()
        if row is None:
            continue
        payload = _loads_json_object(row["result_json"])
        calculation_id = str(payload.get("calculation_id") or "").strip()
        if calculation_id:
            record_ids.add(calculation_id)
    return tuple(sorted(record_ids))


def _build_supply_calculation_registry_metadata(
    *,
    calculation_type: str,
    payload: Mapping[str, Any],
    evidence: Mapping[str, Any] | None,
    export_filename: str,
    export_content_type: str,
    export_sha256: str,
) -> dict[str, Any]:
    settings = _mapping_or_empty(payload.get("settings"))
    summary = _mapping_or_empty(payload.get("summary"))
    overlay = _mapping_or_empty(payload.get("wb_supply_overlay"))
    selected_ids_payload = settings.get("selected_wb_supply_ids")
    selected_ids = [
        str(item)
        for item in selected_ids_payload
        if str(item or "").strip()
    ] if isinstance(selected_ids_payload, (list, tuple)) else []
    selected_supply_count = _audit_int(
        overlay.get("selected_supply_count", overlay.get("selected_supplies_count"))
    ) or len(selected_ids)
    selected_supply_qty = _audit_float(
        _mapping_or_empty(overlay.get("stock_ff")).get(
            "total_selected_qty",
            overlay.get("stock_ff_total_selected"),
        )
    )
    incident = _mapping_or_empty(payload.get("wb_warehouse_exclusion"))
    policy = _mapping_or_empty(incident.get("policy"))
    quality = _mapping_or_empty(incident.get("quality"))
    incident_revision = _audit_int(
        incident.get("policy_revision", quality.get("policy_revision", policy.get("revision")))
    )
    incident_status = (
        "active"
        if bool(incident.get("policy_active", policy.get("active")))
        else "inactive"
    )
    if not incident and not policy and not quality:
        incident_status = "unknown"
    key_settings = {
        key: settings.get(key)
        for key in (
            "stock_ff_source",
            "factory_inbound_source",
            "sales_avg_period_days",
            "sales_history_mode",
            "sales_date_from",
            "sales_date_to",
            "target_facility_id",
            "production_days",
            "factory_to_target_ff_days",
            "ff_safety_days",
            "order_cycle_days",
            "prod_lead_time_days",
            "lead_time_factory_to_ff_days",
            "lead_time_ff_to_wb_days",
            "safety_days_mp",
            "safety_days_ff",
            "cycle_order_days",
            "cycle_supply_days",
            "lead_time_to_region_days",
            "lead_time_to_region_days_by_district",
            "safety_days",
            "order_batch_qty",
            "included_district_keys",
        )
        if key in settings
    }
    return {
        "contract_name": "sheet_vitrina_v1_supply_calculation_registry_record",
        "contract_version": 1,
        "calculation_type": calculation_type,
        "calculation_id": str(payload.get("calculation_id") or ""),
        "calculated_at": str(payload.get("calculated_at") or ""),
        "report_date": str(payload.get("report_date") or ""),
        "status": str(payload.get("status") or ""),
        "summary": {
            "total_qty": _audit_int(summary.get("total_qty")),
            "estimated_weight": _audit_float(summary.get("estimated_weight")),
            "estimated_volume": _audit_float(summary.get("estimated_volume")),
        },
        "target_facility": {
            "facility_id": str(payload.get("target_facility_id") or ""),
            "name": str(payload.get("target_facility_name") or ""),
        },
        "national_demand_scope": str(payload.get("national_demand_scope") or ""),
        "wb_stock_used": payload.get("wb_stock_used"),
        "key_settings": key_settings,
        "selected_wb_supply_ids": selected_ids,
        "selected_wb_supply_count": selected_supply_count,
        "selected_wb_supply_qty": selected_supply_qty,
        "incident_policy": {
            "revision": incident_revision,
            "status": incident_status,
            "effective_from": str(
                policy.get("effective_from")
                or quality.get("policy_effective_date")
                or ""
            ),
            "snapshot_date": str(
                incident.get("snapshot_date")
                or quality.get("snapshot_date")
                or ""
            ),
            "snapshot_digest": str(
                incident.get("snapshot_digest")
                or incident.get("raw_rows_digest")
                or quality.get("raw_rows_digest")
                or ""
            ),
            "quality_state": str(quality.get("state") or ""),
        },
        "warning_count": len(payload.get("warnings") or [])
        if isinstance(payload.get("warnings"), (list, tuple))
        else 0,
        "evidence": dict(evidence or {}),
        "export": {
            "filename": export_filename,
            "content_type": export_content_type,
            "sha256": export_sha256,
            "available": bool(export_sha256),
        },
    }


def _supply_calculation_registry_row_metadata(row: sqlite3.Row) -> dict[str, Any]:
    metadata = _loads_json_object(row["metadata_json"])
    if metadata:
        return metadata
    return {
        "calculation_id": str(row["calculation_id"] or ""),
        "calculation_type": str(row["calculation_type"] or ""),
        "calculated_at": str(row["calculated_at"] or ""),
        "report_date": str(row["report_date"] or ""),
        "status": str(row["status"] or ""),
    }


def _supply_calculation_registry_row_to_list_item(row: sqlite3.Row) -> dict[str, Any]:
    metadata = _supply_calculation_registry_row_metadata(row)
    summary = _mapping_or_empty(metadata.get("summary"))
    incident = _mapping_or_empty(metadata.get("incident_policy"))
    export = _mapping_or_empty(metadata.get("export"))
    target_facility = _mapping_or_empty(metadata.get("target_facility"))
    legacy_settings = _mapping_or_empty(metadata.get("settings"))
    key_settings = _mapping_or_empty(metadata.get("key_settings")) or legacy_settings
    legacy_overlay = _mapping_or_empty(metadata.get("wb_supply_overlay_summary"))
    selected_supply_count = _audit_int(metadata.get("selected_wb_supply_count"))
    if not selected_supply_count:
        selected_supply_count = _audit_int(
            legacy_settings.get(
                "selected_wb_supply_ids_count",
                legacy_overlay.get("selected_supply_count"),
            )
        )
    selected_supply_qty = _audit_float(metadata.get("selected_wb_supply_qty"))
    if not selected_supply_qty:
        selected_supply_qty = _audit_float(
            legacy_overlay.get("stock_ff_total_selected")
        )
    return {
        "record_id": str(row["record_id"]),
        "calculation_id": str(row["calculation_id"] or ""),
        "calculation_type": str(row["calculation_type"] or ""),
        "completeness": str(row["completeness"] or ""),
        "is_reproducible": str(row["completeness"] or "") == "complete",
        "calculated_at": str(row["calculated_at"] or ""),
        "report_date": str(row["report_date"] or ""),
        "status": str(row["status"] or ""),
        "summary": dict(summary),
        "target_facility": dict(target_facility),
        "national_demand_scope": str(metadata.get("national_demand_scope") or ""),
        "wb_stock_used": metadata.get("wb_stock_used"),
        "key_settings": dict(key_settings),
        "selected_wb_supply_count": selected_supply_count,
        "selected_wb_supply_qty": selected_supply_qty,
        "incident_policy": dict(incident),
        "warning_count": _audit_int(metadata.get("warning_count")),
        "download_available": bool(
            str(row["completeness"] or "") == "complete"
            and bool(row["export_blob"])
        ),
        "export": dict(export),
        "legacy_note": (
            "Старая metadata-only запись: точный payload и выгрузка не сохранялись."
            if str(row["completeness"] or "") != "complete"
            else ""
        ),
    }


def _supply_calculation_registry_row_to_detail(row: sqlite3.Row) -> dict[str, Any]:
    result = _supply_calculation_registry_row_to_list_item(row)
    payload_json = str(row["payload_json"] or "")
    try:
        payload = json.loads(payload_json) if payload_json else None
    except json.JSONDecodeError:
        payload = None
    metadata = _supply_calculation_registry_row_metadata(row)
    result.update(
        {
            "payload": payload if isinstance(payload, dict) else None,
            "payload_sha256": str(row["payload_sha256"] or ""),
            "metadata": metadata,
            "evidence": dict(_mapping_or_empty(metadata.get("evidence"))),
            "export_sha256": str(row["export_sha256"] or ""),
        }
    )
    return result


def _build_wb_regional_supply_calculation_audit_row(
    *,
    calculated_at: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    settings = _mapping_or_empty(payload.get("settings"))
    summary = _mapping_or_empty(payload.get("summary"))
    diagnostics = _mapping_or_empty(payload.get("diagnostics"))
    overlay = _mapping_or_empty(payload.get("wb_supply_overlay"))
    overlay_regional = _mapping_or_empty(overlay.get("wb_regional"))
    overlay_stock_ff = _mapping_or_empty(overlay.get("stock_ff"))
    districts_payload = payload.get("districts")
    districts = [item for item in districts_payload if isinstance(item, Mapping)] if isinstance(districts_payload, list) else []
    district_totals = {
        str(item.get("district_key", "")): {
            "total_qty": _audit_int(item.get("total_qty")),
            "deficit_qty": _audit_int(item.get("deficit_qty")),
            "row_count": len(item.get("rows") or []) if isinstance(item.get("rows"), list) else 0,
        }
        for item in districts
        if str(item.get("district_key", "")).strip()
    }
    central_planning_totals = {
        key: district_totals.get(key, {"total_qty": 0, "deficit_qty": 0, "row_count": 0})
        for key in CENTRAL_PLANNING_ZONE_KEYS
    }
    included_district_keys = [
        str(item)
        for item in settings.get("included_district_keys", [])
        if str(item or "").strip()
    ]
    selected_wb_supply_ids = settings.get("selected_wb_supply_ids", [])
    selected_wb_supply_ids_count = (
        len(selected_wb_supply_ids)
        if isinstance(selected_wb_supply_ids, (list, tuple))
        else 0
    )
    added_by_district = overlay_regional.get("added_qty_by_district") or overlay.get(
        "regional_added_qty_by_district"
    )
    if not isinstance(added_by_district, Mapping):
        added_by_district = {}
    selected_supplies = overlay.get("selected_supplies")
    skipped_supplies = overlay_regional.get("skipped_supplies") or overlay.get("skipped_supplies")
    unmapped_events = overlay_regional.get("unmapped_events")
    return {
        "calculation_id": str(payload.get("calculation_id", "")),
        "calculated_at": str(payload.get("calculated_at") or calculated_at),
        "report_date": str(payload.get("report_date", "")),
        "status": str(payload.get("status", "")),
        "stock_ff_source": str(payload.get("stock_ff_source") or settings.get("stock_ff_source") or ""),
        "settings": {
            "sales_avg_period_days": _audit_int(settings.get("sales_avg_period_days")),
            "cycle_supply_days": _audit_int(
                settings.get("cycle_supply_days", settings.get("supply_horizon_days"))
            ),
            "lead_time_to_region_days": _audit_int(settings.get("lead_time_to_region_days")),
            "lead_time_to_region_days_by_district": {
                str(key): _audit_int(value)
                for key, value in _mapping_or_empty(
                    settings.get("lead_time_to_region_days_by_district")
                    or settings.get("district_lead_time_days")
                ).items()
                if str(key or "").strip()
            },
            "safety_days": _audit_int(settings.get("safety_days")),
            "order_batch_qty": _audit_int(settings.get("order_batch_qty")),
            "included_district_keys": included_district_keys,
            "included_district_count": len(included_district_keys),
            "selected_wb_supply_ids_count": selected_wb_supply_ids_count,
        },
        "summary": {
            "total_qty": _audit_int(summary.get("total_qty")),
            "estimated_weight": _audit_float(summary.get("estimated_weight")),
            "estimated_volume": _audit_float(summary.get("estimated_volume")),
        },
        "district_totals_by_key": district_totals,
        "central_total_qty": sum(
            _audit_int(item.get("total_qty")) for item in central_planning_totals.values()
        ),
        "central_deficit_qty": sum(
            _audit_int(item.get("deficit_qty")) for item in central_planning_totals.values()
        ),
        "central_planning_totals_by_zone": central_planning_totals,
        "diagnostics_summary": {
            "district_selection_mode": str(diagnostics.get("district_selection_mode", "")),
            "fallback_sku_count": _audit_int(diagnostics.get("fallback_sku_count")),
            "seed_allocated_qty_total": _audit_int(diagnostics.get("seed_allocated_qty_total")),
            "seed_unfulfilled_qty_total": _audit_int(diagnostics.get("seed_unfulfilled_qty_total")),
            "excluded_missing_current_stock_snapshot": _audit_int(
                diagnostics.get("excluded_missing_current_stock_snapshot")
            ),
            "excluded_missing_previous_stock_snapshot": _audit_int(
                diagnostics.get("excluded_missing_previous_stock_snapshot")
            ),
        },
        "wb_supply_overlay_summary": {
            "status": str(overlay.get("status", "")),
            "selected_supply_count": _audit_int(
                overlay.get("selected_supply_count", overlay.get("selected_supplies_count"))
            )
            or (len(selected_supplies) if isinstance(selected_supplies, list) else 0),
            "events_count": _audit_int(overlay_regional.get("events_count", overlay.get("events_count"))),
            "skipped_count": _audit_int(overlay_regional.get("skipped_count", overlay.get("skipped_count")))
            or (len(skipped_supplies) if isinstance(skipped_supplies, list) else 0),
            "warnings_count": _audit_int(overlay.get("warnings_count")),
            "stock_ff_total_base": _audit_float(
                overlay_stock_ff.get("total_base_stock_ff", overlay.get("stock_ff_total_base"))
            ),
            "stock_ff_total_selected": _audit_float(
                overlay_stock_ff.get("total_selected_qty", overlay.get("stock_ff_total_selected"))
            ),
            "stock_ff_total_effective": _audit_float(
                overlay_stock_ff.get("total_effective_stock_ff", overlay.get("stock_ff_total_effective"))
            ),
            "regional_added_qty_by_district": {
                str(key): _audit_float(value)
                for key, value in added_by_district.items()
                if str(key or "").strip()
            },
            "regional_added_qty_total": _audit_float(
                overlay_regional.get("added_qty_total", overlay.get("regional_added_qty_total"))
            ),
            "regional_unmapped_events_count": _audit_int(overlay_regional.get("unmapped_events_count"))
            or (len(unmapped_events) if isinstance(unmapped_events, list) else 0),
        },
    }


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _audit_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _audit_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _wb_supply_record_from_row(row: sqlite3.Row) -> dict[str, Any]:
    normalized = _loads_json_object(row["normalized_row_json"])
    raw_list = _loads_json_object(row["raw_list_json"]) if row["raw_list_json"] else None
    raw_detail = _loads_json_object(row["raw_detail_json"]) if row["raw_detail_json"] else None
    raw_goods = _loads_json_list(row["raw_goods_json"]) if row["raw_goods_json"] else None
    raw_package = _loads_json_list(row["raw_package_json"]) if row["raw_package_json"] else None
    return {
        "supply_id": row["supply_id"],
        "cache_key": row["cache_key"] or normalized.get("cache_key") or row["supply_id"],
        "wb_supply_id": row["wb_supply_id"] or normalized.get("wb_supply_id") or "",
        "preorder_id": row["preorder_id"] or normalized.get("preorder_id") or "",
        "normalized": normalized,
        "raw_list": raw_list,
        "raw_detail": raw_detail,
        "raw_goods": raw_goods,
        "raw_package": raw_package,
        "raw_list_hash": row["raw_list_hash"] or str(normalized.get("raw_list_hash") or ""),
        "raw_detail_hash": row["raw_detail_hash"] or str(normalized.get("raw_detail_hash") or ""),
        "raw_goods_hash": row["raw_goods_hash"] or str(normalized.get("raw_goods_hash") or ""),
        "raw_package_hash": row["raw_package_hash"] or str(normalized.get("raw_package_hash") or ""),
        "last_enriched_at": row["last_enriched_at"] or str(normalized.get("last_enriched_at") or ""),
        "enrichment_status": row["enrichment_status"] or str(normalized.get("enrichment_status") or ""),
        "enrichment_error": row["enrichment_error"] or str(normalized.get("enrichment_error") or ""),
    }


def _targeted_wb_supply_guard_from_row(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    normalized = _loads_json_object(row["normalized_row_json"])
    raw_goods = _loads_json_list(row["raw_goods_json"]) if row["raw_goods_json"] else None
    return {
        "supply_id": str(row["supply_id"] or ""),
        "cache_key": str(row["cache_key"] or normalized.get("cache_key") or ""),
        "wb_supply_id": str(row["wb_supply_id"] or normalized.get("wb_supply_id") or ""),
        "preorder_id": str(row["preorder_id"] or normalized.get("preorder_id") or ""),
        "normalized_supply_id": str(normalized.get("supply_id") or ""),
        "normalized_cache_key": str(normalized.get("cache_key") or ""),
        "status_id": _audit_int(normalized.get("status_id")),
        "virtual_type_id": normalized.get("virtual_type_id"),
        "type_label": str(normalized.get("type_label") or ""),
        "source_created_at": str(normalized.get("source_created_at") or ""),
        "supply_date": str(normalized.get("supply_date") or ""),
        "raw_goods": raw_goods,
        "raw_goods_hash": str(row["raw_goods_hash"] or normalized.get("raw_goods_hash") or ""),
    }


def _ff_stock_reservation_quantities(conn: sqlite3.Connection, supply_id: str) -> dict[int, float]:
    rows = conn.execute(
        """
        SELECT line.nm_id, SUM(line.quantity_delta) AS quantity
        FROM sheet_vitrina_v1_ff_stock_reservation_operations AS operation
        JOIN sheet_vitrina_v1_ff_stock_reservation_lines AS line
          ON line.operation_id = operation.operation_id
        WHERE operation.supply_id = ?
        GROUP BY line.nm_id
        """,
        (str(supply_id),),
    ).fetchall()
    return {
        int(row["nm_id"]): float(row["quantity"] or 0.0)
        for row in rows
        if abs(float(row["quantity"] or 0.0)) > 1e-9
    }


def _insert_ff_stock_reservation_operation(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    source_key: str,
    supply_id: str,
    supply_revision: str,
    operation_type: str,
    created_at: str,
    diagnostics: Mapping[str, Any],
    lines: list[Mapping[str, Any]],
) -> None:
    if not operation_id or not source_key or not supply_id or not lines:
        raise ValueError("complete reservation operation and lines are required")
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_ff_stock_reservation_operations(
            operation_id, source_key, supply_id, supply_revision,
            operation_type, created_at, diagnostics_json
        ) VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            operation_id,
            source_key,
            supply_id,
            supply_revision,
            operation_type,
            created_at,
            json.dumps(dict(diagnostics), ensure_ascii=False),
        ),
    )
    conn.executemany(
        """
        INSERT INTO sheet_vitrina_v1_ff_stock_reservation_lines(
            operation_id, line_no, nm_id, quantity_delta, raw_json
        ) VALUES(?, ?, ?, ?, ?)
        """,
        [
            (
                operation_id,
                index,
                int(item.get("nm_id") or 0),
                float(item.get("quantity_delta") or 0.0),
                json.dumps(dict(item.get("raw") or item), ensure_ascii=False),
            )
            for index, item in enumerate(lines, start=1)
        ],
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sheet_vitrina_user_config_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "status": "ok",
        "user_key": row["user_key"],
        "config_key": row["config_key"],
        "schema_version": int(row["schema_version"]),
        "revision": int(row["revision"]),
        "updated_at": row["updated_at"],
        "config": _loads_json_object(row["payload_json"]),
    }


def _wb_incident_policy_row_to_dict(
    row: sqlite3.Row | None,
    *,
    seller_id: str,
) -> dict[str, Any]:
    if row is None:
        return {"status": "missing", "seller_id": seller_id, "revision": 0}
    column_names = set(row.keys())
    return {
        "status": "ok",
        "seller_id": str(row["seller_id"]),
        "revision": int(row["revision"]),
        "active": bool(row["active"]),
        "warehouse_ids": _loads_json_list(row["warehouse_ids_json"]),
        "warehouse_identities": _loads_json_list(row["warehouse_identities_json"]),
        "warehouse_entries": (
            _loads_json_list(row["warehouse_entries_json"])
            if "warehouse_entries_json" in column_names
            else []
        ),
        "reason": str(row["reason"] or ""),
        "effective_from": str(row["effective_from"] or ""),
        "effective_to": str(row["effective_to"] or ""),
        "policy_status": str(row["policy_status"] or ""),
        "actor": str(row["actor"] or ""),
        "created_at": str(row["created_at"] or ""),
        "source": str(row["source"] or ""),
        "legacy_payloads": _loads_json_list(row["legacy_payloads_json"]),
    }


def _sku_action_event_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "nm_id": int(row["nm_id"]),
        "parameter": row["parameter"],
        "old_value": row["old_value"],
        "requested_value": row["requested_value"],
        "confirmed_value": row["confirmed_value"],
        "delta": row["delta"],
        "requested_at": row["requested_at"] or "",
        "confirmed_at": row["confirmed_at"] or "",
        "actor": row["actor"] or "",
        "source": row["source"] or "",
        "advert_id": row["advert_id"],
        "campaign": row["campaign"] or "",
        "placement": row["placement"] or "",
        "preview_id": row["preview_id"] or "",
        "correlation_id": row["correlation_id"] or "",
        "commit_status": row["commit_status"] or "",
        "readback_status": row["readback_status"] or "",
        "readback": _loads_json_object(row["readback_json"]),
        "warnings": _loads_json_list(row["warnings_json"]),
        "stabilization_override": bool(row["stabilization_override"]),
        "warning_override": bool(row["warning_override"]),
        "error": row["error"] or "",
    }


def _sheet_vitrina_user_row_to_dict(row: sqlite3.Row, *, include_password_hash: bool) -> dict[str, Any]:
    role = row["role"] or ""
    payload = {
        "user_id": row["user_id"],
        "username": row["username"] or "",
        "display_name": row["display_name"] or "",
        "role": role,
        "allowed_sections": _normalize_sheet_vitrina_user_sections(row["allowed_sections_json"], role=role),
        "manage_users": bool(row["manage_users"]),
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"] or "",
        "updated_at": row["updated_at"] or "",
        "source": "runtime",
        "readonly": False,
    }
    if include_password_hash:
        payload["password_hash"] = row["password_hash"] or ""
    return payload


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, SimpleNamespace):
        return {key: _to_jsonable(item) for key, item in vars(value).items()}
    if hasattr(value, "__dataclass_fields__"):
        return {key: _to_jsonable(getattr(value, key)) for key in value.__dataclass_fields__}
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    return value


def _to_namespace(value: Any) -> Any:
    if isinstance(value, dict):
        return SimpleNamespace(**{key: _to_namespace(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_to_namespace(item) for item in value]
    return value


def _connect(db_path: Path) -> sqlite3.Connection:
    timeout_ms = _SQLITE_BUSY_TIMEOUT_MS.get() or DEFAULT_SQLITE_BUSY_TIMEOUT_MS
    with _OPERATIONAL_STORE_REGISTRIES_LOCK:
        registry = _OPERATIONAL_STORE_REGISTRIES.get(Path(db_path).resolve())
    if registry is not None:
        return registry.connect(
            "operational",
            mode="rw",
            operation="registry_upload_db_backed_runtime",
            timeout_ms=timeout_ms,
        )
    conn = connect_sqlite(db_path, timeout_ms=timeout_ms)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _connect_supplier_confirmation_store(
    runtime_dir: Path,
) -> sqlite3.Connection:
    db_path = runtime_dir / SUPPLIER_CONFIRMATION_DB_FILENAME
    conn = connect_sqlite(db_path, timeout_ms=30_000)
    conn.row_factory = sqlite3.Row
    schema_key = _schema_ready_key(conn)
    if schema_key not in _CONFIRMATION_SCHEMA_READY_KEYS:
        with _CONFIRMATION_SCHEMA_READY_LOCK:
            if schema_key not in _CONFIRMATION_SCHEMA_READY_KEYS:
                conn.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_supplier_confirmation_previews (
                        token TEXT PRIMARY KEY,
                        confirmation_type TEXT NOT NULL,
                        supplier_order_id TEXT NOT NULL,
                        subject_id TEXT,
                        target_revision TEXT NOT NULL,
                        source_sha256 TEXT,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        consumed_at TEXT,
                        result_json TEXT NOT NULL DEFAULT '{}'
                    );
                    CREATE INDEX IF NOT EXISTS supplier_confirmation_previews_by_target
                    ON sheet_vitrina_v1_supplier_confirmation_previews(
                        supplier_order_id,confirmation_type,created_at DESC
                    );
                    """
                )
                _CONFIRMATION_SCHEMA_READY_KEYS.add(_schema_ready_key(conn))
    return conn


def _cleanup_expired_supplier_confirmation_previews(
    conn: sqlite3.Connection,
    *,
    runtime_dir: Path,
    now: str,
) -> int:
    rows = conn.execute(
        """
        SELECT token,payload_json,consumed_at,expires_at
        FROM sheet_vitrina_v1_supplier_confirmation_previews
        WHERE (consumed_at IS NOT NULL OR expires_at <= ?)
          AND COALESCE(json_extract(payload_json,'$.staging_path'),'') <> ''
        ORDER BY CASE WHEN consumed_at IS NULL THEN 0 ELSE 1 END, created_at
        LIMIT 250
        """,
        (str(now or ""),),
    ).fetchall()
    removed_expired_tokens: list[tuple[str]] = []
    for row in rows:
        token = str(row["token"])
        if not _finalize_supplier_confirmation_staging(
            conn,
            runtime_dir=runtime_dir,
            token=token,
            payload_json=str(row["payload_json"] or "{}"),
        ):
            continue
        if not str(row["consumed_at"] or "").strip():
            removed_expired_tokens.append((token,))
    if removed_expired_tokens:
        conn.executemany(
            """
            DELETE FROM sheet_vitrina_v1_supplier_confirmation_previews
            WHERE token=? AND consumed_at IS NULL
            """,
            removed_expired_tokens,
        )
        conn.commit()
    return len(removed_expired_tokens)


def _finalize_supplier_confirmation_staging(
    conn: sqlite3.Connection,
    *,
    runtime_dir: Path,
    token: str,
    payload_json: str,
) -> bool:
    payload = _loads_json_object(payload_json)
    relative_path = str(payload.get("staging_path") or "").strip()
    if not relative_path:
        return True
    staging_root = (runtime_dir / "supplier_financial_staging").resolve()
    candidate = (runtime_dir / relative_path).resolve()
    if (
        not candidate.is_relative_to(staging_root)
        or candidate.parent.parent != staging_root
    ):
        return False
    try:
        candidate.unlink(missing_ok=True)
        if candidate.parent.exists():
            candidate.parent.rmdir()
    except OSError:
        return False
    payload["staging_path"] = ""
    conn.execute(
        """
        UPDATE sheet_vitrina_v1_supplier_confirmation_previews
        SET payload_json=?
        WHERE token=?
        """,
        (
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            str(token or "").strip(),
        ),
    )
    conn.commit()
    return True


def _bundle_version_exists(conn: sqlite3.Connection, bundle_version: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM registry_upload_versions
        WHERE bundle_version = ?
        """,
        (bundle_version,),
    ).fetchone()
    return row is not None


def _cost_price_dataset_version_exists(conn: sqlite3.Connection, dataset_version: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM cost_price_upload_versions
        WHERE dataset_version = ?
        """,
        (dataset_version,),
    ).fetchone()
    return row is not None


def _ensure_schema(conn: sqlite3.Connection) -> None:
    schema_key = _schema_ready_key(conn)
    if schema_key in _SCHEMA_READY_KEYS:
        return
    with _SCHEMA_READY_LOCK:
        if schema_key in _SCHEMA_READY_KEYS:
            return
        was_in_transaction = conn.in_transaction
        _ensure_schema_uncached(conn)
        if not was_in_transaction and conn.in_transaction:
            conn.commit()
        _SCHEMA_READY_KEYS.add(_schema_ready_key(conn))


def _schema_ready_key(conn: sqlite3.Connection) -> tuple[str, int, int, int]:
    database_row = conn.execute("PRAGMA database_list").fetchone()
    db_path = Path(str(database_row[2] if database_row else "")).resolve()
    stat = db_path.stat()
    schema_version_row = conn.execute("PRAGMA schema_version").fetchone()
    schema_version = int(schema_version_row[0] if schema_version_row else 0)
    return str(db_path), int(stat.st_dev), int(stat.st_ino), schema_version


def _ensure_schema_uncached(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS registry_upload_versions (
            bundle_version TEXT PRIMARY KEY,
            uploaded_at TEXT NOT NULL,
            activated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS registry_upload_results (
            bundle_version TEXT PRIMARY KEY REFERENCES registry_upload_versions(bundle_version) ON DELETE CASCADE,
            status TEXT NOT NULL,
            config_count INTEGER NOT NULL,
            metrics_count INTEGER NOT NULL,
            formulas_count INTEGER NOT NULL,
            validation_errors_json TEXT NOT NULL,
            activated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS registry_upload_current_state (
            slot INTEGER PRIMARY KEY CHECK (slot = 1),
            bundle_version TEXT NOT NULL REFERENCES registry_upload_versions(bundle_version),
            activated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS registry_upload_config_v2 (
            bundle_version TEXT NOT NULL REFERENCES registry_upload_versions(bundle_version) ON DELETE CASCADE,
            nm_id INTEGER NOT NULL,
            enabled INTEGER NOT NULL,
            display_name TEXT NOT NULL,
            group_name TEXT NOT NULL,
            display_order INTEGER NOT NULL,
            PRIMARY KEY (bundle_version, nm_id)
        );

        CREATE TABLE IF NOT EXISTS registry_upload_metrics_v2 (
            bundle_version TEXT NOT NULL REFERENCES registry_upload_versions(bundle_version) ON DELETE CASCADE,
            metric_key TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            scope TEXT NOT NULL,
            label_ru TEXT NOT NULL,
            calc_type TEXT NOT NULL,
            calc_ref TEXT NOT NULL,
            show_in_data INTEGER NOT NULL,
            format_name TEXT NOT NULL,
            display_order INTEGER NOT NULL,
            section_name TEXT NOT NULL,
            PRIMARY KEY (bundle_version, metric_key)
        );

        CREATE TABLE IF NOT EXISTS registry_upload_formulas_v2 (
            bundle_version TEXT NOT NULL REFERENCES registry_upload_versions(bundle_version) ON DELETE CASCADE,
            row_order INTEGER NOT NULL,
            formula_id TEXT NOT NULL,
            expression TEXT NOT NULL,
            description TEXT NOT NULL,
            PRIMARY KEY (bundle_version, formula_id),
            UNIQUE (bundle_version, row_order)
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_ready_snapshots (
            bundle_version TEXT NOT NULL REFERENCES registry_upload_versions(bundle_version) ON DELETE CASCADE,
            activated_at TEXT NOT NULL,
            as_of_date TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            plan_version TEXT NOT NULL,
            refreshed_at TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            PRIMARY KEY (bundle_version, as_of_date)
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_ready_snapshots_by_bundle_refresh
        ON sheet_vitrina_v1_ready_snapshots(bundle_version, refreshed_at DESC, as_of_date DESC);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_auto_update_state (
            slot INTEGER PRIMARY KEY CHECK (slot = 1),
            last_run_started_at TEXT,
            last_run_finished_at TEXT,
            last_run_status TEXT,
            last_run_error TEXT,
            last_run_snapshot_id TEXT,
            last_run_as_of_date TEXT,
            last_run_refreshed_at TEXT,
            last_run_result_json TEXT,
            last_successful_auto_update_at TEXT
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_manual_operator_state (
            slot INTEGER PRIMARY KEY CHECK (slot = 1),
            last_successful_manual_refresh_at TEXT,
            last_successful_manual_load_at TEXT,
            last_manual_refresh_result_json TEXT,
            last_manual_load_result_json TEXT
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_load_state (
            slot INTEGER PRIMARY KEY CHECK (slot = 1),
            loaded_at TEXT,
            snapshot_id TEXT,
            as_of_date TEXT,
            refreshed_at TEXT,
            plan_fingerprint TEXT,
            result_json TEXT
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_user_configs (
            user_key TEXT NOT NULL,
            config_key TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            revision INTEGER NOT NULL,
            PRIMARY KEY (user_key, config_key)
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_incident_policy_revisions (
            seller_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            active INTEGER NOT NULL CHECK (active IN (0, 1)),
            warehouse_ids_json TEXT NOT NULL DEFAULT '[]',
            warehouse_identities_json TEXT NOT NULL DEFAULT '[]',
            warehouse_entries_json TEXT NOT NULL DEFAULT '[]',
            reason TEXT NOT NULL DEFAULT '',
            effective_from TEXT NOT NULL,
            effective_to TEXT NOT NULL DEFAULT '',
            policy_status TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'incident_policy',
            legacy_payloads_json TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (seller_id, revision)
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_wb_incident_policy_by_effective_date
        ON sheet_vitrina_v1_wb_incident_policy_revisions(
            seller_id,
            effective_from DESC,
            revision DESC
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_incident_projection_cache (
            seller_id TEXT NOT NULL,
            snapshot_digest TEXT NOT NULL,
            policy_revision INTEGER NOT NULL,
            snapshot_date TEXT NOT NULL,
            projection_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (seller_id, snapshot_digest, policy_revision, snapshot_date)
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_incident_rematerialization_audit (
            operation_id TEXT NOT NULL,
            bundle_version TEXT NOT NULL,
            as_of_date TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            plan_fingerprint TEXT NOT NULL,
            approval_reference TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT '',
            target_dates_json TEXT NOT NULL,
            before_plan_digest TEXT NOT NULL,
            after_plan_digest TEXT NOT NULL,
            non_target_digest TEXT NOT NULL,
            changed_cells INTEGER NOT NULL,
            before_manifest_json TEXT NOT NULL,
            after_manifest_json TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            PRIMARY KEY (operation_id, bundle_version, as_of_date)
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_incident_rematerialization_by_date
        ON sheet_vitrina_v1_incident_rematerialization_audit(
            as_of_date,
            applied_at DESC
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_sku_action_events (
            event_id TEXT PRIMARY KEY,
            nm_id INTEGER NOT NULL,
            parameter TEXT NOT NULL,
            old_value REAL,
            requested_value REAL,
            confirmed_value REAL,
            delta REAL,
            requested_at TEXT NOT NULL,
            confirmed_at TEXT,
            actor TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'sku_management',
            advert_id INTEGER,
            campaign TEXT NOT NULL DEFAULT '',
            placement TEXT NOT NULL DEFAULT '',
            preview_id TEXT NOT NULL DEFAULT '',
            correlation_id TEXT NOT NULL DEFAULT '',
            commit_status TEXT NOT NULL,
            readback_status TEXT NOT NULL DEFAULT 'not_started',
            readback_json TEXT NOT NULL DEFAULT '{}',
            warnings_json TEXT NOT NULL DEFAULT '[]',
            stabilization_override INTEGER NOT NULL DEFAULT 0,
            warning_override INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_sku_action_events_by_nm_time
        ON sheet_vitrina_v1_sku_action_events(nm_id, confirmed_at DESC, requested_at DESC);

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_sku_action_events_by_status_time
        ON sheet_vitrina_v1_sku_action_events(commit_status, confirmed_at DESC, requested_at DESC);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_users (
            user_id TEXT PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL,
            allowed_sections_json TEXT NOT NULL DEFAULT '[]',
            manage_users INTEGER NOT NULL DEFAULT 0,
            password_hash TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_users_by_role_active
        ON sheet_vitrina_v1_users(role, is_active, username);

        CREATE TABLE IF NOT EXISTS cost_price_upload_versions (
            dataset_version TEXT PRIMARY KEY,
            uploaded_at TEXT NOT NULL,
            activated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cost_price_upload_results (
            dataset_version TEXT PRIMARY KEY REFERENCES cost_price_upload_versions(dataset_version) ON DELETE CASCADE,
            status TEXT NOT NULL,
            row_count INTEGER NOT NULL,
            validation_errors_json TEXT NOT NULL,
            activated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS cost_price_current_state (
            slot INTEGER PRIMARY KEY CHECK (slot = 1),
            dataset_version TEXT NOT NULL REFERENCES cost_price_upload_versions(dataset_version),
            activated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cost_price_upload_rows (
            dataset_version TEXT NOT NULL REFERENCES cost_price_upload_versions(dataset_version) ON DELETE CASCADE,
            row_order INTEGER NOT NULL,
            group_name TEXT NOT NULL,
            cost_price_rub REAL NOT NULL,
            effective_from TEXT NOT NULL,
            PRIMARY KEY (dataset_version, row_order)
        );

        CREATE INDEX IF NOT EXISTS cost_price_upload_rows_by_dataset_group_date
        ON cost_price_upload_rows(dataset_version, group_name, effective_from, row_order);

        CREATE TABLE IF NOT EXISTS temporal_source_snapshots (
            source_key TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (source_key, snapshot_date)
        );

        CREATE INDEX IF NOT EXISTS temporal_source_snapshots_by_source_date
        ON temporal_source_snapshots(source_key, snapshot_date);

        CREATE TABLE IF NOT EXISTS temporal_source_slot_snapshots (
            source_key TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            snapshot_role TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (source_key, snapshot_date, snapshot_role)
        );

        CREATE INDEX IF NOT EXISTS temporal_source_slot_snapshots_by_source_date_role
        ON temporal_source_slot_snapshots(source_key, snapshot_date, snapshot_role);

        CREATE TABLE IF NOT EXISTS temporal_source_closure_state (
            source_key TEXT NOT NULL,
            target_date TEXT NOT NULL,
            slot_kind TEXT NOT NULL,
            state TEXT NOT NULL,
            attempt_count INTEGER NOT NULL,
            next_retry_at TEXT,
            last_reason TEXT,
            last_attempt_at TEXT,
            last_success_at TEXT,
            accepted_at TEXT,
            PRIMARY KEY (source_key, target_date, slot_kind)
        );

        CREATE INDEX IF NOT EXISTS temporal_source_closure_state_by_state_retry
        ON temporal_source_closure_state(state, next_retry_at, target_date, source_key);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_factory_order_dataset_state (
            dataset_type TEXT PRIMARY KEY,
            uploaded_at TEXT NOT NULL,
            row_count INTEGER NOT NULL,
            rows_json TEXT NOT NULL,
            uploaded_filename TEXT,
            uploaded_content_type TEXT,
            workbook_blob BLOB
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_ff_stock_operation_previews (
            preview_id TEXT PRIMARY KEY,
            operation_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            uploaded_filename TEXT NOT NULL,
            uploaded_content_type TEXT,
            source_file_sha256 TEXT NOT NULL,
            source_file_blob BLOB NOT NULL,
            parsed_lines_json TEXT NOT NULL DEFAULT '[]',
            summary_json TEXT NOT NULL DEFAULT '{}',
            warnings_json TEXT NOT NULL DEFAULT '[]',
            errors_json TEXT NOT NULL DEFAULT '[]'
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_ff_stock_operation_previews_by_created
        ON sheet_vitrina_v1_ff_stock_operation_previews(created_at DESC);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_ff_stock_operations (
            operation_id TEXT PRIMARY KEY,
            operation_type TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_key TEXT NOT NULL UNIQUE,
            source_object_id TEXT,
            source_object_label TEXT,
            created_at TEXT NOT NULL,
            business_effective_date TEXT,
            created_by TEXT,
            sku_count INTEGER NOT NULL DEFAULT 0,
            total_quantity_delta REAL NOT NULL DEFAULT 0,
            total_quantity_abs REAL NOT NULL DEFAULT 0,
            warnings_json TEXT NOT NULL DEFAULT '[]',
            diagnostics_json TEXT NOT NULL DEFAULT '{}',
            source_filename TEXT,
            source_content_type TEXT,
            source_file_sha256 TEXT,
            source_file_blob BLOB
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_ff_stock_operations_by_created
        ON sheet_vitrina_v1_ff_stock_operations(created_at DESC, operation_id DESC);

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_ff_stock_operations_by_source
        ON sheet_vitrina_v1_ff_stock_operations(source_type, source_object_id);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_ff_stock_operation_lines (
            operation_id TEXT NOT NULL REFERENCES sheet_vitrina_v1_ff_stock_operations(operation_id) ON DELETE CASCADE,
            line_no INTEGER NOT NULL,
            nm_id INTEGER NOT NULL,
            barcode TEXT,
            sku TEXT,
            nomenclature_name TEXT,
            comment TEXT,
            group_name TEXT,
            quantity_delta REAL NOT NULL,
            raw_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(operation_id, line_no)
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_ff_stock_operation_lines_by_nm
        ON sheet_vitrina_v1_ff_stock_operation_lines(nm_id);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_ff_inventory_reconciliations (
            reconciliation_id TEXT PRIMARY KEY,
            source_sha256 TEXT NOT NULL,
            source_filename TEXT NOT NULL,
            source_content_type TEXT NOT NULL,
            source_file_blob BLOB NOT NULL,
            business_date TEXT NOT NULL,
            plan_fingerprint TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            approval_reference TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL,
            operation_ids_json TEXT NOT NULL,
            before_digest TEXT NOT NULL,
            non_target_digest TEXT NOT NULL,
            after_digest TEXT NOT NULL,
            reconciliation_json TEXT NOT NULL,
            UNIQUE(source_sha256,business_date)
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_ff_inventory_cost_bases (
            basis_version_id TEXT NOT NULL,
            nm_id INTEGER NOT NULL,
            effective_from TEXT NOT NULL,
            unit_cost_rub TEXT NOT NULL,
            basis_kind TEXT NOT NULL,
            quality TEXT NOT NULL,
            source_reference TEXT NOT NULL,
            approval_reference TEXT NOT NULL,
            provenance_json TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(basis_version_id,nm_id)
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_ff_stock_reservation_operations (
            operation_id TEXT PRIMARY KEY,
            source_key TEXT NOT NULL UNIQUE,
            supply_id TEXT NOT NULL,
            supply_revision TEXT NOT NULL,
            operation_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            diagnostics_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_ff_stock_reservation_operations_by_supply
        ON sheet_vitrina_v1_ff_stock_reservation_operations(supply_id, created_at, operation_id);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_ff_stock_reservation_lines (
            operation_id TEXT NOT NULL REFERENCES sheet_vitrina_v1_ff_stock_reservation_operations(operation_id) ON DELETE CASCADE,
            line_no INTEGER NOT NULL,
            nm_id INTEGER NOT NULL,
            quantity_delta REAL NOT NULL,
            raw_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(operation_id, line_no)
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_ff_stock_reservation_lines_by_nm
        ON sheet_vitrina_v1_ff_stock_reservation_lines(nm_id);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_ff_stock_wb_auto_writeoff_checkpoint (
            slot TEXT PRIMARY KEY,
            checkpoint_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            created_by TEXT,
            reason TEXT,
            baseline_cache_keys_json TEXT NOT NULL DEFAULT '[]',
            baseline_source_keys_json TEXT NOT NULL DEFAULT '[]',
            baseline_supply_ids_json TEXT NOT NULL DEFAULT '[]',
            baseline_record_count INTEGER NOT NULL DEFAULT 0,
            watermark_source_created_at TEXT,
            watermark_supply_date TEXT,
            diagnostics_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_ff_stock_wb_supply_lifecycle (
            supply_id TEXT PRIMARY KEY,
            first_seen_complete_snapshot_at TEXT NOT NULL,
            last_seen_complete_snapshot_at TEXT NOT NULL,
            last_observation_id TEXT NOT NULL,
            last_observation_at TEXT NOT NULL,
            consecutive_missing_complete_snapshots INTEGER NOT NULL DEFAULT 0,
            lifecycle_state TEXT NOT NULL,
            original_debit_operation_id TEXT NOT NULL,
            return_operation_id TEXT NOT NULL,
            return_source_revision TEXT NOT NULL,
            last_record_json TEXT NOT NULL,
            diagnostics_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS ff_stock_wb_supply_lifecycle_state
        ON sheet_vitrina_v1_ff_stock_wb_supply_lifecycle(
            lifecycle_state,consecutive_missing_complete_snapshots,updated_at
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_plan_report_monthly_baseline (
            month TEXT PRIMARY KEY,
            fin_buyout_rub REAL NOT NULL,
            ads_sum REAL NOT NULL,
            uploaded_at TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            uploaded_filename TEXT,
            uploaded_content_type TEXT,
            workbook_checksum TEXT,
            note TEXT
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_factory_order_result_state (
            slot INTEGER PRIMARY KEY CHECK (slot = 1),
            calculated_at TEXT NOT NULL,
            result_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_fbs_fulfillment_order_result_state (
            slot INTEGER PRIMARY KEY CHECK (slot = 1),
            calculated_at TEXT NOT NULL,
            result_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_regional_supply_result_state (
            slot INTEGER PRIMARY KEY CHECK (slot = 1),
            calculated_at TEXT NOT NULL,
            result_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_regional_supply_calculation_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            saved_at TEXT NOT NULL,
            calculated_at TEXT NOT NULL,
            calculation_id TEXT NOT NULL,
            report_date TEXT NOT NULL,
            metadata_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_wb_regional_supply_calculation_audit_recent
        ON sheet_vitrina_v1_wb_regional_supply_calculation_audit(id DESC);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_supply_calculation_registry (
            record_id TEXT PRIMARY KEY,
            calculation_id TEXT NOT NULL,
            calculation_type TEXT NOT NULL,
            completeness TEXT NOT NULL,
            calculated_at TEXT NOT NULL,
            report_date TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT,
            metadata_json TEXT NOT NULL,
            payload_sha256 TEXT,
            export_filename TEXT,
            export_content_type TEXT,
            export_blob BLOB,
            export_sha256 TEXT,
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(source, source_id)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS sheet_vitrina_v1_supply_calculation_registry_complete_identity
        ON sheet_vitrina_v1_supply_calculation_registry(calculation_type, calculation_id)
        WHERE completeness = 'complete';

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_supply_calculation_registry_recent
        ON sheet_vitrina_v1_supply_calculation_registry(calculated_at DESC, record_id DESC);

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_supply_calculation_registry_type_date
        ON sheet_vitrina_v1_supply_calculation_registry(
            calculation_type,
            report_date DESC,
            calculated_at DESC,
            record_id DESC
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_supplies (
            supply_id TEXT PRIMARY KEY,
            cache_key TEXT,
            wb_supply_id TEXT,
            preorder_id TEXT,
            normalized_row_json TEXT NOT NULL,
            raw_list_json TEXT,
            raw_detail_json TEXT,
            raw_goods_json TEXT,
            raw_package_json TEXT,
            raw_list_hash TEXT,
            raw_detail_hash TEXT,
            raw_goods_hash TEXT,
            raw_package_hash TEXT,
            warehouse_id TEXT,
            status_id INTEGER,
            quantity_for_size_filter REAL,
            source_created_at TEXT,
            supply_date TEXT,
            fact_date TEXT,
            updated_date TEXT,
            synced_at TEXT NOT NULL,
            last_list_synced_at TEXT,
            last_enriched_at TEXT,
            enrichment_status TEXT,
            enrichment_error TEXT
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_wb_supplies_by_status
        ON sheet_vitrina_v1_wb_supplies(status_id, supply_date DESC, updated_date DESC);

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_wb_supplies_by_warehouse
        ON sheet_vitrina_v1_wb_supplies(warehouse_id, supply_date DESC, updated_date DESC);

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_wb_supplies_by_quantity
        ON sheet_vitrina_v1_wb_supplies(quantity_for_size_filter);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_supplies_sync_state (
            slot INTEGER PRIMARY KEY CHECK (slot = 1),
            last_synced_at TEXT,
            last_successful_sync_at TEXT,
            last_error TEXT,
            last_limit INTEGER,
            last_offset INTEGER,
            latest_synced_count INTEGER,
            backfill_complete INTEGER DEFAULT 0,
            backfill_started_at TEXT,
            backfill_completed_at TEXT,
            highest_synced_offset INTEGER DEFAULT 0,
            last_successful_offset INTEGER,
            last_mode TEXT,
            latest_window_synced_at TEXT,
            latest_window_limit INTEGER,
            latest_window_returned_count INTEGER,
            may_have_more INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_supplies_warehouses (
            warehouse_id TEXT PRIMARY KEY,
            warehouse_name TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            synced_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_supplies_sync_runs (
            run_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            phase TEXT,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            offset INTEGER DEFAULT 0,
            run_limit INTEGER DEFAULT 0,
            pages_fetched INTEGER DEFAULT 0,
            raw_fetched INTEGER DEFAULT 0,
            upserted INTEGER DEFAULT 0,
            new_rows INTEGER DEFAULT 0,
            changed_rows INTEGER DEFAULT 0,
            unchanged_rows INTEGER DEFAULT 0,
            enriched INTEGER DEFAULT 0,
            failed_enrich INTEGER DEFAULT 0,
            may_have_more INTEGER DEFAULT 0,
            last_error TEXT,
            logs_json TEXT
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_supply_transit_cost_enrichment (
            supply_id TEXT PRIMARY KEY,
            amount REAL,
            currency TEXT NOT NULL DEFAULT 'RUB',
            amount_label TEXT,
            is_transit INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL,
            evidence_type TEXT NOT NULL,
            confidence TEXT,
            fetched_at TEXT,
            status TEXT NOT NULL,
            error TEXT,
            source_endpoint_path TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_supply_transit_cost_enrichment_attempts (
            attempt_id TEXT PRIMARY KEY,
            supply_id TEXT NOT NULL,
            amount REAL,
            currency TEXT NOT NULL DEFAULT 'RUB',
            amount_label TEXT,
            is_transit INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL,
            evidence_type TEXT NOT NULL,
            confidence TEXT,
            fetched_at TEXT,
            status TEXT NOT NULL,
            error TEXT,
            source_endpoint_path TEXT,
            attempted_at TEXT NOT NULL,
            attempt_fingerprint TEXT NOT NULL UNIQUE
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_wb_supply_transit_cost_enrichment_attempts_by_supply
        ON sheet_vitrina_v1_wb_supply_transit_cost_enrichment_attempts(supply_id, attempted_at DESC);

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_wb_supply_transit_cost_enrichment_by_status
        ON sheet_vitrina_v1_wb_supply_transit_cost_enrichment(status, updated_at DESC);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_supply_transit_cost_enrichment_runs (
            run_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            phase TEXT,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            candidate_count INTEGER DEFAULT 0,
            processed_count INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            not_found_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            session_expired_count INTEGER DEFAULT 0,
            auth_required_count INTEGER DEFAULT 0,
            route_unavailable_count INTEGER DEFAULT 0,
            collector_unavailable_count INTEGER DEFAULT 0,
            lock_busy_count INTEGER DEFAULT 0,
            last_error TEXT,
            lock_status_json TEXT,
            logs_json TEXT
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_source_health_status (
            source_key TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            checked_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_supplier_shipment_uploads (
            upload_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            source_filename TEXT NOT NULL,
            content_type TEXT NOT NULL,
            source_file_sha256 TEXT NOT NULL,
            source_file_path TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            parsed_payload_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_supplier_shipments (
            shipment_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            shipment_date TEXT NOT NULL,
            actual_shipment_date TEXT,
            actual_ff_acceptance_date TEXT,
            historical_status_exception TEXT NOT NULL DEFAULT '',
            order_status TEXT NOT NULL DEFAULT 'production',
            target_facility_id TEXT,
            target_facility_name TEXT,
            expenses_complete INTEGER NOT NULL DEFAULT 0,
            invoice_no TEXT,
            invoice_date TEXT,
            contract_no TEXT,
            contract_date TEXT,
            supplier_name TEXT,
            customer_name TEXT,
            currency TEXT,
            approx_yuan_rate REAL,
            cny_ledger_effective_rate TEXT,
            cny_payment_currency_rub_cost TEXT,
            cny_paid_amount TEXT,
            cny_bank_fee_rub TEXT,
            cny_calculation_status TEXT,
            cny_calculation_error TEXT,
            cny_calculated_at TEXT,
            product_qty_total REAL,
            product_amount_total REAL,
            extras_amount_total REAL,
            invoice_amount_total REAL,
            declared_invoice_total REAL,
            match_status TEXT NOT NULL,
            source_filename TEXT,
            source_file_sha256 TEXT,
            source_file_path TEXT,
            invoice_document_id TEXT,
            parser_version TEXT,
            warnings_json TEXT NOT NULL,
            errors_json TEXT NOT NULL,
            archived_at TEXT,
            archive_event_id TEXT,
            archive_reason TEXT NOT NULL DEFAULT '',
            archive_actor TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_supplier_shipments_by_date
        ON sheet_vitrina_v1_supplier_shipments(shipment_date DESC, created_at DESC);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_supplier_shipment_archive_events (
            event_id TEXT PRIMARY KEY,
            shipment_id TEXT NOT NULL,
            archived_at TEXT NOT NULL,
            actor TEXT NOT NULL,
            reason TEXT NOT NULL,
            source_fingerprint TEXT NOT NULL,
            source_snapshot_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS supplier_shipment_archive_events_by_shipment
        ON sheet_vitrina_v1_supplier_shipment_archive_events(
            shipment_id, archived_at DESC, event_id DESC
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_supplier_shipment_historical_status_events (
            event_id TEXT PRIMARY KEY,
            shipment_id TEXT NOT NULL,
            exception_code TEXT NOT NULL,
            action TEXT NOT NULL,
            previous_exception TEXT NOT NULL DEFAULT '',
            new_exception TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL,
            provenance TEXT NOT NULL,
            actor TEXT NOT NULL,
            evidence_fingerprint TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            apply_fingerprint TEXT NOT NULL,
            reverses_event_id TEXT,
            reversible INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS supplier_historical_status_events_by_shipment
        ON sheet_vitrina_v1_supplier_shipment_historical_status_events(
            shipment_id, created_at DESC, event_id DESC
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_supplier_publication_chain_jobs (
            chain_job_id TEXT PRIMARY KEY,
            chain_fingerprint TEXT NOT NULL,
            supplier_fingerprint TEXT NOT NULL,
            publication_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL,
            phase TEXT NOT NULL,
            actor TEXT NOT NULL,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            report_json TEXT NOT NULL DEFAULT '{}',
            error_message TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS supplier_publication_chain_jobs_by_status
        ON sheet_vitrina_v1_supplier_publication_chain_jobs(
            status, updated_at DESC, chain_job_id DESC
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_trade_documents (
            document_id TEXT PRIMARY KEY,
            document_type TEXT NOT NULL,
            number TEXT,
            document_date TEXT,
            supplier_name TEXT,
            currency TEXT,
            amount_total REAL,
            source TEXT NOT NULL,
            source_shipment_id TEXT,
            source_upload_id TEXT,
            file_original_name TEXT NOT NULL,
            file_content_type TEXT NOT NULL,
            file_sha256 TEXT NOT NULL,
            file_path TEXT NOT NULL,
            parser_version TEXT,
            parsed_metadata_json TEXT NOT NULL DEFAULT '{}',
            warnings_json TEXT NOT NULL DEFAULT '[]',
            errors_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_trade_documents_by_type_status
        ON sheet_vitrina_v1_trade_documents(document_type, status, updated_at DESC);

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_trade_documents_by_contract_match
        ON sheet_vitrina_v1_trade_documents(document_type, status, number, document_date);

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_trade_documents_by_source_file
        ON sheet_vitrina_v1_trade_documents(document_type, file_sha256, source_shipment_id);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_invoice_contract_links (
            invoice_document_id TEXT PRIMARY KEY,
            contract_document_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            linked_by TEXT,
            source TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_invoice_contract_links_by_contract
        ON sheet_vitrina_v1_invoice_contract_links(contract_document_id);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_supplier_shipment_lines (
            line_id TEXT PRIMARY KEY,
            shipment_id TEXT NOT NULL REFERENCES sheet_vitrina_v1_supplier_shipments(shipment_id) ON DELETE CASCADE,
            line_type TEXT NOT NULL,
            sort_order INTEGER NOT NULL,
            source_no TEXT,
            barcode TEXT,
            product_type TEXT,
            model_raw TEXT,
            model_normalized TEXT,
            match_key TEXT,
            internal_sku TEXT,
            internal_nm_id INTEGER,
            internal_name TEXT,
            qty REAL,
            unit_price REAL,
            amount REAL,
            currency TEXT,
            comment TEXT,
            match_status TEXT,
            manual_override INTEGER NOT NULL,
            invoice_price_yuan_snapshot REAL,
            reference_purchase_price_yuan_snapshot REAL,
            price_conformity_status TEXT NOT NULL DEFAULT 'not_checked',
            price_conformity_checked_at TEXT,
            price_conformity_check_mode TEXT NOT NULL DEFAULT 'not_checked',
            price_conformity_reason TEXT NOT NULL DEFAULT 'not_checked',
            price_conformity_actor TEXT,
            price_conformity_context_json TEXT NOT NULL DEFAULT '{}',
            raw_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_supplier_shipment_lines_by_shipment
        ON sheet_vitrina_v1_supplier_shipment_lines(shipment_id, sort_order);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_supplier_financial_documents (
            document_id TEXT PRIMARY KEY,
            supplier_order_id TEXT NOT NULL REFERENCES sheet_vitrina_v1_supplier_shipments(shipment_id) ON DELETE CASCADE,
            document_type TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            stored_file_path TEXT NOT NULL,
            file_content_type TEXT NOT NULL,
            file_sha256 TEXT NOT NULL,
            uploaded_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            parse_status TEXT NOT NULL,
            vendor TEXT,
            document_number TEXT,
            document_date TEXT,
            currency TEXT,
            total_amount REAL,
            total_amount_rub REAL,
            vat_rate REAL,
            vat_amount_rub REAL,
            due_date TEXT,
            route TEXT,
            contract_ref TEXT,
            cbr_usd_rate_requested_date TEXT,
            cbr_usd_rate_effective_date TEXT,
            cbr_usd_rate_value REAL,
            rate_source TEXT,
            rate_source_status TEXT,
            raw_parse_json TEXT NOT NULL DEFAULT '{}',
            normalized_parse_json TEXT NOT NULL DEFAULT '{}',
            parser_version TEXT,
            warnings_json TEXT NOT NULL DEFAULT '[]',
            errors_json TEXT NOT NULL DEFAULT '[]'
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_supplier_financial_documents_by_order
        ON sheet_vitrina_v1_supplier_financial_documents(supplier_order_id, document_date DESC, uploaded_at DESC);

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_supplier_financial_documents_by_type
        ON sheet_vitrina_v1_supplier_financial_documents(document_type, parse_status);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_supplier_financial_expense_lines (
            line_id TEXT PRIMARY KEY,
            financial_document_id TEXT NOT NULL REFERENCES sheet_vitrina_v1_supplier_financial_documents(document_id) ON DELETE CASCADE,
            supplier_order_id TEXT NOT NULL REFERENCES sheet_vitrina_v1_supplier_shipments(shipment_id) ON DELETE CASCADE,
            sort_order INTEGER NOT NULL,
            category TEXT NOT NULL,
            stage TEXT,
            description TEXT,
            amount REAL,
            currency TEXT,
            amount_rub REAL,
            vat_rate REAL,
            vat_amount_rub REAL,
            included_in_logistics_efficiency INTEGER NOT NULL DEFAULT 0,
            included_in_customs_total INTEGER NOT NULL DEFAULT 0,
            status TEXT,
            confidence REAL,
            raw_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_supplier_financial_expense_lines_by_order
        ON sheet_vitrina_v1_supplier_financial_expense_lines(supplier_order_id, financial_document_id, sort_order);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_supplier_financial_sources (
            source_sha256 TEXT PRIMARY KEY,
            stored_file_path TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            file_content_type TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            raw_parse_json TEXT NOT NULL DEFAULT '{}',
            normalized_parse_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_supplier_bank_operation_assignments (
            semantic_operation_id TEXT PRIMARY KEY,
            source_sha256 TEXT NOT NULL,
            logical_fee_id TEXT NOT NULL,
            supplier_order_id TEXT NOT NULL,
            financial_document_id TEXT NOT NULL,
            assigned_at TEXT NOT NULL,
            raw_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_supplier_bank_assignments_by_source
        ON sheet_vitrina_v1_supplier_bank_operation_assignments(
            source_sha256, logical_fee_id, semantic_operation_id
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_supplier_bank_assignments_by_order
        ON sheet_vitrina_v1_supplier_bank_operation_assignments(
            supplier_order_id, financial_document_id
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_supplier_payment_fee_confirmations (
            confirmation_id TEXT PRIMARY KEY,
            supplier_order_id TEXT NOT NULL REFERENCES sheet_vitrina_v1_supplier_shipments(shipment_id) ON DELETE CASCADE,
            payment_document_id TEXT NOT NULL,
            payment_fingerprint TEXT NOT NULL,
            confirmation_type TEXT NOT NULL CHECK(confirmation_type IN ('zero_fee')),
            status TEXT NOT NULL CHECK(status IN ('active','revoked')),
            reason TEXT NOT NULL,
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(supplier_order_id,payment_document_id,confirmation_type)
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_supplier_payment_fee_confirmations_by_order
        ON sheet_vitrina_v1_supplier_payment_fee_confirmations(
            supplier_order_id,status,payment_document_id
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_supplier_financial_source_migrations (
            contract_version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL,
            result_json TEXT NOT NULL
        );

        INSERT OR IGNORE INTO sheet_vitrina_v1_supplier_financial_sources(
            source_sha256,stored_file_path,original_filename,file_content_type,
            parser_version,raw_parse_json,normalized_parse_json,created_at,updated_at
        )
        SELECT file_sha256,MAX(stored_file_path),MAX(original_filename),
               MAX(file_content_type),MAX(parser_version),MAX(raw_parse_json),
               MAX(normalized_parse_json),MIN(uploaded_at),MAX(updated_at)
        FROM sheet_vitrina_v1_supplier_financial_documents
        WHERE document_type = 'bank_fee_statement' AND length(file_sha256) = 64
        GROUP BY file_sha256;

        INSERT OR IGNORE INTO sheet_vitrina_v1_supplier_bank_operation_assignments(
            semantic_operation_id,source_sha256,logical_fee_id,supplier_order_id,
            financial_document_id,assigned_at,raw_json
        )
        SELECT
            COALESCE(
                json_extract(line.raw_json,'$.semantic_operation_id'),
                json_extract(line.raw_json,'$.row.semantic_operation_id')
            ),
            document.file_sha256,
            COALESCE(
                json_extract(line.raw_json,'$.logical_fee_id'),
                json_extract(line.raw_json,'$.row.logical_fee_id'),
                ''
            ),
            line.supplier_order_id,
            line.financial_document_id,
            document.updated_at,
            line.raw_json
        FROM sheet_vitrina_v1_supplier_financial_expense_lines AS line
        JOIN sheet_vitrina_v1_supplier_financial_documents AS document
          ON document.document_id = line.financial_document_id
        WHERE document.document_type = 'bank_fee_statement'
          AND document.parse_status <> 'excluded'
          AND COALESCE(
                json_extract(line.raw_json,'$.semantic_operation_id'),
                json_extract(line.raw_json,'$.row.semantic_operation_id'),
                ''
              ) <> '';

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_supplier_confirmation_previews (
            token TEXT PRIMARY KEY,
            confirmation_type TEXT NOT NULL,
            supplier_order_id TEXT NOT NULL,
            subject_id TEXT,
            target_revision TEXT NOT NULL,
            source_sha256 TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            result_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_supplier_confirmation_previews_by_target
        ON sheet_vitrina_v1_supplier_confirmation_previews(
            supplier_order_id,confirmation_type,created_at DESC
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_supplier_ff_cost_layers (
            layer_id TEXT PRIMARY KEY,
            supplier_shipment_id TEXT NOT NULL REFERENCES sheet_vitrina_v1_supplier_shipments(shipment_id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            accepted_ff_date TEXT,
            calculated_at TEXT NOT NULL,
            effective_cny_rate REAL,
            invoice_amount_total_cny REAL,
            invoice_extras_total_cny REAL,
            product_qty_total REAL,
            common_expense_pool_rub REAL,
            common_expense_per_unit_rub REAL,
            weighted_avg_ff_unit_cost_rub REAL,
            reconciliation_status TEXT NOT NULL,
            reconciliation_delta_rub REAL,
            inputs_hash TEXT NOT NULL,
            version INTEGER NOT NULL,
            is_current INTEGER NOT NULL DEFAULT 1,
            supersedes_layer_id TEXT,
            superseded_at TEXT,
            source_status_json TEXT NOT NULL DEFAULT '{}',
            component_status_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(supplier_shipment_id, version)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS sheet_vitrina_v1_supplier_ff_cost_layers_current
        ON sheet_vitrina_v1_supplier_ff_cost_layers(supplier_shipment_id)
        WHERE is_current = 1;

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_supplier_ff_cost_layers_by_status
        ON sheet_vitrina_v1_supplier_ff_cost_layers(status, accepted_ff_date DESC);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_supplier_ff_cost_layer_lines (
            layer_line_id TEXT PRIMARY KEY,
            layer_id TEXT NOT NULL REFERENCES sheet_vitrina_v1_supplier_ff_cost_layers(layer_id) ON DELETE CASCADE,
            supplier_shipment_id TEXT NOT NULL REFERENCES sheet_vitrina_v1_supplier_shipments(shipment_id) ON DELETE CASCADE,
            supplier_line_id TEXT NOT NULL,
            nm_id INTEGER,
            sku TEXT,
            display_name TEXT,
            qty REAL,
            invoice_unit_price_cny REAL,
            sku_purchase_cost_rub REAL,
            allocated_common_expenses_per_unit_rub REAL,
            sku_ff_unit_cost_rub REAL,
            line_total_cost_rub REAL,
            allocation_method TEXT NOT NULL,
            source_status TEXT NOT NULL,
            missing_reason TEXT
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_supplier_ff_cost_layer_lines_by_nm
        ON sheet_vitrina_v1_supplier_ff_cost_layer_lines(nm_id, supplier_shipment_id);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_supply_cost_layers (
            wb_supply_cost_layer_id TEXT PRIMARY KEY,
            wb_supply_id TEXT NOT NULL,
            cache_key TEXT,
            nm_id INTEGER NOT NULL,
            accepted_qty REAL NOT NULL DEFAULT 0,
            qty_denominator REAL NOT NULL DEFAULT 0,
            supply_date TEXT,
            accepted_date TEXT,
            supplier_ff_cost_layer_id TEXT,
            supplier_ff_cost_layer_line_id TEXT,
            sku_ff_unit_cost_rub REAL,
            transit_cost_status TEXT NOT NULL,
            transit_amount_total REAL,
            transit_per_unit_rub REAL,
            ff_upload_id TEXT,
            ff_services_amount_total REAL NOT NULL DEFAULT 0,
            ff_services_per_unit_rub REAL NOT NULL DEFAULT 0,
            ff_storage_amount_total REAL NOT NULL DEFAULT 0,
            ff_storage_per_unit_rub REAL NOT NULL DEFAULT 0,
            pre_acceptance_unit_cost_rub REAL,
            wb_acceptance_amount_total REAL NOT NULL DEFAULT 0,
            wb_acceptance_per_accepted_unit_rub REAL NOT NULL DEFAULT 0,
            our_wb_unit_cost_rub REAL,
            source_status TEXT NOT NULL,
            component_status_json TEXT NOT NULL DEFAULT '{}',
            missing_reason TEXT,
            calculated_at TEXT NOT NULL,
            inputs_hash TEXT NOT NULL,
            version INTEGER NOT NULL,
            is_current INTEGER NOT NULL DEFAULT 1,
            supersedes_id TEXT,
            superseded_at TEXT,
            UNIQUE(wb_supply_id, nm_id, version)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS sheet_vitrina_v1_wb_supply_cost_layers_current
        ON sheet_vitrina_v1_wb_supply_cost_layers(wb_supply_id, nm_id)
        WHERE is_current = 1;

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_wb_supply_cost_layers_by_date_nm
        ON sheet_vitrina_v1_wb_supply_cost_layers(supply_date, nm_id, source_status);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_opening_baseline (
            as_of_date TEXT NOT NULL,
            nm_id INTEGER NOT NULL,
            display_name TEXT,
            opening_stock_qty REAL NOT NULL DEFAULT 0,
            opening_unit_cost_rub REAL,
            source_priority INTEGER NOT NULL,
            source_status TEXT NOT NULL,
            supplier_ff_cost_layer_id TEXT,
            supplier_ff_cost_layer_line_id TEXT,
            metric11_value REAL,
            confirmed_qty REAL NOT NULL DEFAULT 0,
            estimated_qty REAL NOT NULL DEFAULT 0,
            fallback_qty REAL NOT NULL DEFAULT 0,
            missing_reason TEXT,
            component_status_json TEXT NOT NULL DEFAULT '{}',
            calculated_at TEXT NOT NULL,
            inputs_hash TEXT NOT NULL,
            PRIMARY KEY(as_of_date, nm_id)
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_wb_opening_baseline_by_source
        ON sheet_vitrina_v1_wb_opening_baseline(as_of_date, source_status, source_priority);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_cost_daily_state (
            as_of_date TEXT NOT NULL,
            nm_id INTEGER NOT NULL,
            stock_qty REAL NOT NULL DEFAULT 0,
            our_wb_unit_cost_rub REAL,
            confirmed_qty REAL NOT NULL DEFAULT 0,
            estimated_qty REAL NOT NULL DEFAULT 0,
            fallback_qty REAL NOT NULL DEFAULT 0,
            confirmed_share_pct REAL,
            source_status TEXT NOT NULL,
            component_status_json TEXT NOT NULL DEFAULT '{}',
            calculated_at TEXT NOT NULL,
            inputs_hash TEXT NOT NULL,
            PRIMARY KEY(as_of_date, nm_id)
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_wb_cost_daily_state_by_date
        ON sheet_vitrina_v1_wb_cost_daily_state(as_of_date, source_status);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_cny_documents (
            document_id TEXT PRIMARY KEY,
            document_type TEXT NOT NULL,
            source TEXT NOT NULL,
            source_order_id TEXT,
            context_order_id TEXT,
            linked_financial_document_id TEXT,
            original_filename TEXT,
            stored_file_path TEXT,
            file_content_type TEXT,
            file_sha256 TEXT,
            natural_key TEXT NOT NULL UNIQUE,
            uploaded_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            operation_date TEXT,
            operation_datetime TEXT,
            status TEXT NOT NULL,
            document_number TEXT,
            currency TEXT,
            rub_amount TEXT,
            cny_amount TEXT,
            bank_rate TEXT,
            parsed_payload_json TEXT NOT NULL DEFAULT '{}',
            raw_parse_json TEXT NOT NULL DEFAULT '{}',
            parser_version TEXT,
            warnings_json TEXT NOT NULL DEFAULT '[]',
            errors_json TEXT NOT NULL DEFAULT '[]'
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_cny_documents_by_type_date
        ON sheet_vitrina_v1_cny_documents(document_type, operation_date, operation_datetime, document_id);

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_cny_documents_by_order
        ON sheet_vitrina_v1_cny_documents(source_order_id, document_type, operation_date);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_cny_ledger_operations (
            operation_id TEXT PRIMARY KEY,
            operation_type TEXT NOT NULL,
            source_document_id TEXT,
            source_order_id TEXT,
            operation_date TEXT,
            operation_datetime TEXT,
            sequence_key TEXT NOT NULL,
            cny_delta TEXT,
            rub_value_delta TEXT,
            effective_rate_before TEXT,
            balance_cny_after TEXT,
            balance_rub_value_after TEXT,
            average_rate_after TEXT,
            status TEXT NOT NULL,
            error_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_cny_ledger_operations_by_sequence
        ON sheet_vitrina_v1_cny_ledger_operations(sequence_key, operation_id);

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_cny_ledger_operations_by_order
        ON sheet_vitrina_v1_cny_ledger_operations(source_order_id, operation_date, sequence_key);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_cny_ledger_replay_state (
            slot INTEGER PRIMARY KEY CHECK (slot = 1),
            status TEXT NOT NULL,
            reason TEXT,
            replayed_at TEXT NOT NULL,
            operation_count INTEGER NOT NULL DEFAULT 0,
            document_count INTEGER NOT NULL DEFAULT 0,
            balance_cny TEXT,
            balance_rub_value TEXT,
            average_rate TEXT,
            diagnostics_json TEXT NOT NULL DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_nomenclature_items (
            item_id TEXT PRIMARY KEY,
            is_active INTEGER NOT NULL,
            is_hidden INTEGER NOT NULL DEFAULT 0,
            hidden_at TEXT NOT NULL DEFAULT '',
            hidden_reason TEXT NOT NULL DEFAULT '',
            our_sku TEXT,
            nm_id INTEGER,
            barcode TEXT NOT NULL DEFAULT '',
            barcodes_json TEXT NOT NULL DEFAULT '[]',
            barcode_source TEXT NOT NULL DEFAULT 'missing',
            barcode_status TEXT NOT NULL DEFAULT 'missing',
            barcode_synced_at TEXT,
            barcode_updated_at TEXT,
            barcode_evidence_json TEXT NOT NULL DEFAULT '{}',
            vendor_code TEXT NOT NULL DEFAULT '',
            wb_title TEXT NOT NULL DEFAULT '',
            wb_subject_name TEXT NOT NULL DEFAULT '',
            wb_updated_at TEXT NOT NULL DEFAULT '',
            wb_synced_at TEXT NOT NULL DEFAULT '',
            wb_sync_status TEXT NOT NULL DEFAULT '',
            wb_sync_evidence_json TEXT NOT NULL DEFAULT '{}',
            nomenclature_name TEXT NOT NULL,
            product_type TEXT NOT NULL,
            match_key TEXT NOT NULL,
            purchase_price_yuan REAL,
            factory_box_size INTEGER,
            aliases_json TEXT NOT NULL,
            compatible_models_text TEXT NOT NULL DEFAULT '',
            compatible_model_keys_json TEXT NOT NULL DEFAULT '[]',
            comment TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_nomenclature_items_by_match_key
        ON sheet_vitrina_v1_nomenclature_items(is_active, match_key);

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_sku_groups (
            group_key TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            aliases_json TEXT NOT NULL DEFAULT '[]',
            is_active INTEGER NOT NULL DEFAULT 1,
            is_system INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    ensure_ff_pool_foundation_schema(conn)
    ensure_ff_pool_document_schema(conn)
    ensure_ff_wb_supply_origin_schema(conn)
    ensure_wb_fbs_orders_schema(conn)
    ensure_ff_pool_cutover_schema(conn)
    ensure_inventory_planning_schema(conn)
    ambiguous_bank_assignment = conn.execute(
        """
        SELECT 1
        FROM sheet_vitrina_v1_supplier_financial_expense_lines AS line
        JOIN sheet_vitrina_v1_supplier_financial_documents AS document
          ON document.document_id = line.financial_document_id
        WHERE document.document_type='bank_fee_statement'
          AND document.parse_status <> 'excluded'
          AND COALESCE(
                json_extract(line.raw_json,'$.semantic_operation_id'),
                json_extract(line.raw_json,'$.row.semantic_operation_id'),
                ''
              ) <> ''
        GROUP BY COALESCE(
            json_extract(line.raw_json,'$.semantic_operation_id'),
            json_extract(line.raw_json,'$.row.semantic_operation_id')
        )
        HAVING COUNT(
            DISTINCT line.supplier_order_id || char(31) || line.financial_document_id
        ) > 1
        LIMIT 1
        """
    ).fetchone()
    if ambiguous_bank_assignment is not None:
        raise ValueError(
            "Обнаружено неоднозначное прежнее назначение банковской операции; "
            "требуется отдельная проверяемая миграция."
        )
    users_access_columns_added = False
    users_access_columns_added = (
        _ensure_column(
            conn,
            table_name="sheet_vitrina_v1_users",
            column_name="allowed_sections_json",
            column_sql="TEXT NOT NULL DEFAULT '[]'",
        )
        or users_access_columns_added
    )
    users_access_columns_added = (
        _ensure_column(
            conn,
            table_name="sheet_vitrina_v1_users",
            column_name="manage_users",
            column_sql="INTEGER NOT NULL DEFAULT 0",
        )
        or users_access_columns_added
    )
    if users_access_columns_added:
        _backfill_sheet_vitrina_user_access_columns(conn)
    for column_name, column_sql in (
        ("cache_key", "TEXT"),
        ("wb_supply_id", "TEXT"),
        ("raw_list_hash", "TEXT"),
        ("raw_detail_hash", "TEXT"),
        ("raw_goods_hash", "TEXT"),
        ("raw_package_hash", "TEXT"),
        ("fact_date", "TEXT"),
        ("last_list_synced_at", "TEXT"),
        ("last_enriched_at", "TEXT"),
        ("enrichment_status", "TEXT"),
        ("enrichment_error", "TEXT"),
    ):
        _ensure_column(
            conn,
            table_name="sheet_vitrina_v1_wb_supplies",
            column_name=column_name,
            column_sql=column_sql,
        )
    for column_name in (
        "auth_required_count",
        "route_unavailable_count",
        "collector_unavailable_count",
        "lock_busy_count",
    ):
        _ensure_column(
            conn,
            table_name="sheet_vitrina_v1_wb_supply_transit_cost_enrichment_runs",
            column_name=column_name,
            column_sql="INTEGER NOT NULL DEFAULT 0",
        )
    for column_name, column_sql in (
        ("backfill_complete", "INTEGER DEFAULT 0"),
        ("backfill_started_at", "TEXT"),
        ("backfill_completed_at", "TEXT"),
        ("highest_synced_offset", "INTEGER DEFAULT 0"),
        ("last_successful_offset", "INTEGER"),
        ("last_mode", "TEXT"),
        ("latest_window_synced_at", "TEXT"),
        ("latest_window_limit", "INTEGER"),
        ("latest_window_returned_count", "INTEGER"),
        ("may_have_more", "INTEGER DEFAULT 0"),
    ):
        _ensure_column(
            conn,
            table_name="sheet_vitrina_v1_wb_supplies_sync_state",
            column_name=column_name,
            column_sql=column_sql,
        )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_nomenclature_items",
        column_name="purchase_price_yuan",
        column_sql="REAL",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_nomenclature_items",
        column_name="factory_box_size",
        column_sql="INTEGER",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_nomenclature_items",
        column_name="compatible_models_text",
        column_sql="TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_nomenclature_items",
        column_name="compatible_model_keys_json",
        column_sql="TEXT NOT NULL DEFAULT '[]'",
    )
    for column_name, column_sql in (
        ("is_hidden", "INTEGER NOT NULL DEFAULT 0"),
        ("hidden_at", "TEXT NOT NULL DEFAULT ''"),
        ("hidden_reason", "TEXT NOT NULL DEFAULT ''"),
        ("barcode", "TEXT NOT NULL DEFAULT ''"),
        ("barcodes_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("barcode_source", "TEXT NOT NULL DEFAULT 'missing'"),
        ("barcode_status", "TEXT NOT NULL DEFAULT 'missing'"),
        ("barcode_synced_at", "TEXT"),
        ("barcode_updated_at", "TEXT"),
        ("barcode_evidence_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("vendor_code", "TEXT NOT NULL DEFAULT ''"),
        ("wb_title", "TEXT NOT NULL DEFAULT ''"),
        ("wb_subject_name", "TEXT NOT NULL DEFAULT ''"),
        ("wb_updated_at", "TEXT NOT NULL DEFAULT ''"),
        ("wb_synced_at", "TEXT NOT NULL DEFAULT ''"),
        ("wb_sync_status", "TEXT NOT NULL DEFAULT ''"),
        ("wb_sync_evidence_json", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        _ensure_column(
            conn,
            table_name="sheet_vitrina_v1_nomenclature_items",
            column_name=column_name,
            column_sql=column_sql,
        )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS sheet_vitrina_v1_nomenclature_items_by_wb
        ON sheet_vitrina_v1_nomenclature_items(nm_id, vendor_code)
        """
    )
    _ensure_nullable_wb_transit_cost(conn)
    for column_name, column_sql in (
        ("last_attempt_status", "TEXT NOT NULL DEFAULT ''"),
        ("last_attempt_error", "TEXT NOT NULL DEFAULT ''"),
        ("last_attempt_at", "TEXT NOT NULL DEFAULT ''"),
        ("source_revision", "TEXT NOT NULL DEFAULT ''"),
        ("success_revision", "INTEGER NOT NULL DEFAULT 0"),
        ("recalculation_status", "TEXT NOT NULL DEFAULT ''"),
        ("recalculation_error", "TEXT NOT NULL DEFAULT ''"),
        ("recalculation_updated_at", "TEXT NOT NULL DEFAULT ''"),
    ):
        _ensure_column(
            conn,
            table_name="sheet_vitrina_v1_wb_supply_transit_cost_enrichment",
            column_name=column_name,
            column_sql=column_sql,
        )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_auto_update_state",
        column_name="last_run_result_json",
        column_sql="TEXT",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_manual_operator_state",
        column_name="last_manual_refresh_result_json",
        column_sql="TEXT",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_manual_operator_state",
        column_name="last_manual_load_result_json",
        column_sql="TEXT",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_supplier_shipments",
        column_name="order_status",
        column_sql="TEXT NOT NULL DEFAULT 'production'",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_supplier_shipments",
        column_name="target_facility_id",
        column_sql="TEXT",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_supplier_shipments",
        column_name="target_facility_name",
        column_sql="TEXT",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_supplier_shipments",
        column_name="expenses_complete",
        column_sql="INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_supplier_shipments",
        column_name="actual_shipment_date",
        column_sql="TEXT",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_supplier_shipments",
        column_name="actual_ff_acceptance_date",
        column_sql="TEXT",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_supplier_shipments",
        column_name="historical_status_exception",
        column_sql="TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_supplier_shipments",
        column_name="approx_yuan_rate",
        column_sql="REAL",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_supplier_shipments",
        column_name="invoice_document_id",
        column_sql="TEXT",
    )
    for column_name, column_sql in (
        ("archived_at", "TEXT"),
        ("archive_event_id", "TEXT"),
        ("archive_reason", "TEXT NOT NULL DEFAULT ''"),
        ("archive_actor", "TEXT NOT NULL DEFAULT ''"),
    ):
        _ensure_column(
            conn,
            table_name="sheet_vitrina_v1_supplier_shipments",
            column_name=column_name,
            column_sql=column_sql,
        )
    for column_name in (
        "cny_ledger_effective_rate",
        "cny_payment_currency_rub_cost",
        "cny_paid_amount",
        "cny_bank_fee_rub",
        "cny_calculation_status",
        "cny_calculation_error",
        "cny_calculated_at",
    ):
        _ensure_column(
            conn,
            table_name="sheet_vitrina_v1_supplier_shipments",
            column_name=column_name,
            column_sql="TEXT",
        )
    for column_name, column_sql in (
        ("pre_acceptance_unit_cost_rub", "REAL"),
        ("wb_acceptance_amount_total", "REAL NOT NULL DEFAULT 0"),
        ("wb_acceptance_per_accepted_unit_rub", "REAL NOT NULL DEFAULT 0"),
    ):
        _ensure_column(
            conn,
            table_name="sheet_vitrina_v1_wb_supply_cost_layers",
            column_name=column_name,
            column_sql=column_sql,
        )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_supplier_shipment_lines",
        column_name="barcode",
        column_sql="TEXT",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_supplier_shipment_lines",
        column_name="invoice_price_yuan_snapshot",
        column_sql="REAL",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_supplier_shipment_lines",
        column_name="reference_purchase_price_yuan_snapshot",
        column_sql="REAL",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_supplier_shipment_lines",
        column_name="price_conformity_status",
        column_sql="TEXT NOT NULL DEFAULT 'not_checked'",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_supplier_shipment_lines",
        column_name="price_conformity_checked_at",
        column_sql="TEXT",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_supplier_shipment_lines",
        column_name="price_conformity_check_mode",
        column_sql="TEXT NOT NULL DEFAULT 'not_checked'",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_supplier_shipment_lines",
        column_name="price_conformity_reason",
        column_sql="TEXT NOT NULL DEFAULT 'not_checked'",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_supplier_shipment_lines",
        column_name="price_conformity_actor",
        column_sql="TEXT",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_supplier_shipment_lines",
        column_name="price_conformity_context_json",
        column_sql="TEXT NOT NULL DEFAULT '{}'",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_wb_incident_policy_revisions",
        column_name="warehouse_entries_json",
        column_sql="TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_sku_action_events",
        column_name="readback_status",
        column_sql="TEXT NOT NULL DEFAULT 'not_started'",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_sku_action_events",
        column_name="warning_override",
        column_sql="INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_ff_stock_operations",
        column_name="business_effective_date",
        column_sql="TEXT",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_factory_order_dataset_state",
        column_name="uploaded_filename",
        column_sql="TEXT",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_factory_order_dataset_state",
        column_name="uploaded_content_type",
        column_sql="TEXT",
    )
    _ensure_column(
        conn,
        table_name="sheet_vitrina_v1_factory_order_dataset_state",
        column_name="workbook_blob",
        column_sql="BLOB",
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO sheet_vitrina_v1_supply_calculation_registry(
            record_id,
            calculation_id,
            calculation_type,
            completeness,
            calculated_at,
            report_date,
            status,
            payload_json,
            metadata_json,
            payload_sha256,
            export_filename,
            export_content_type,
            export_blob,
            export_sha256,
            source,
            source_id,
            created_at
        )
        SELECT
            'legacy-regional-audit:' || CAST(audit.id AS TEXT),
            audit.calculation_id,
            'wb_regional',
            'legacy_metadata',
            audit.calculated_at,
            audit.report_date,
            'legacy',
            NULL,
            audit.metadata_json,
            NULL,
            NULL,
            NULL,
            NULL,
            NULL,
            'regional_metadata_audit',
            CAST(audit.id AS TEXT),
            audit.saved_at
        FROM (
            SELECT
                id,
                saved_at,
                calculated_at,
                calculation_id,
                report_date,
                metadata_json
            FROM sheet_vitrina_v1_wb_regional_supply_calculation_audit
            ORDER BY id DESC
            LIMIT 200
        ) AS audit
        WHERE NOT EXISTS (
            SELECT 1
            FROM sheet_vitrina_v1_supply_calculation_registry AS complete
            WHERE complete.completeness = 'complete'
              AND complete.calculation_type = 'wb_regional'
              AND complete.calculation_id = audit.calculation_id
        )
        """
    )
    conn.execute(
        """
        DELETE FROM sheet_vitrina_v1_supply_calculation_registry
        WHERE source = 'regional_metadata_audit'
          AND source_id NOT IN (
              SELECT CAST(id AS TEXT)
              FROM sheet_vitrina_v1_wb_regional_supply_calculation_audit
              ORDER BY id DESC
              LIMIT 200
          )
        """
    )
def _ensure_column(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    column_name: str,
    column_sql: str,
) -> bool:
    existing = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    if any(str(row["name"]) == column_name for row in existing):
        return False
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")
    return True


def _ensure_nullable_wb_transit_cost(conn: sqlite3.Connection) -> None:
    """Remove the legacy false-zero constraint from the small derived table."""

    table_name = "sheet_vitrina_v1_wb_supply_cost_layers"
    info = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    transit = next(
        (row for row in info if str(row["name"]) == "transit_per_unit_rub"),
        None,
    )
    if transit is None or int(transit["notnull"] or 0) == 0:
        return
    legacy_name = table_name + "__legacy_notnull_transit"
    columns = [str(row["name"]) for row in info]
    quoted = ",".join('"' + value.replace('"', '""') + '"' for value in columns)
    conn.execute("SAVEPOINT migrate_nullable_wb_transit")
    try:
        conn.execute(f"ALTER TABLE {table_name} RENAME TO {legacy_name}")
        conn.execute(
            f"""
            CREATE TABLE {table_name} (
                wb_supply_cost_layer_id TEXT PRIMARY KEY,
                wb_supply_id TEXT NOT NULL,
                cache_key TEXT,
                nm_id INTEGER NOT NULL,
                accepted_qty REAL NOT NULL DEFAULT 0,
                qty_denominator REAL NOT NULL DEFAULT 0,
                supply_date TEXT,
                accepted_date TEXT,
                supplier_ff_cost_layer_id TEXT,
                supplier_ff_cost_layer_line_id TEXT,
                sku_ff_unit_cost_rub REAL,
                transit_cost_status TEXT NOT NULL,
                transit_amount_total REAL,
                transit_per_unit_rub REAL,
                ff_upload_id TEXT,
                ff_services_amount_total REAL NOT NULL DEFAULT 0,
                ff_services_per_unit_rub REAL NOT NULL DEFAULT 0,
                ff_storage_amount_total REAL NOT NULL DEFAULT 0,
                ff_storage_per_unit_rub REAL NOT NULL DEFAULT 0,
                pre_acceptance_unit_cost_rub REAL,
                wb_acceptance_amount_total REAL NOT NULL DEFAULT 0,
                wb_acceptance_per_accepted_unit_rub REAL NOT NULL DEFAULT 0,
                our_wb_unit_cost_rub REAL,
                source_status TEXT NOT NULL,
                component_status_json TEXT NOT NULL DEFAULT '{{}}',
                missing_reason TEXT,
                calculated_at TEXT NOT NULL,
                inputs_hash TEXT NOT NULL,
                version INTEGER NOT NULL,
                is_current INTEGER NOT NULL DEFAULT 1,
                supersedes_id TEXT,
                superseded_at TEXT,
                UNIQUE(wb_supply_id,nm_id,version)
            )
            """
        )
        conn.execute(
            f"INSERT INTO {table_name}({quoted}) SELECT {quoted} FROM {legacy_name}"
        )
        conn.execute(f"DROP TABLE {legacy_name}")
        conn.execute(
            f"""CREATE UNIQUE INDEX sheet_vitrina_v1_wb_supply_cost_layers_current
                ON {table_name}(wb_supply_id,nm_id) WHERE is_current=1"""
        )
        conn.execute(
            f"""CREATE INDEX sheet_vitrina_v1_wb_supply_cost_layers_by_date_nm
                ON {table_name}(supply_date,nm_id,source_status)"""
        )
        conn.execute("RELEASE SAVEPOINT migrate_nullable_wb_transit")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT migrate_nullable_wb_transit")
        conn.execute("RELEASE SAVEPOINT migrate_nullable_wb_transit")
        raise


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        amount = float(value)
        return amount if amount == amount else None
    text = str(value).strip().replace("\u00a0", " ").replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        amount = float(text)
    except ValueError:
        return None
    return amount if amount == amount else None


def _safe_runtime_error(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    lowered = text.lower()
    blocked = ("token", "cookie", "secret", "password", "authorization", "storage_state", "header")
    if any(marker in lowered for marker in blocked):
        return "[redacted]"
    return text[:800]


_SHEET_VITRINA_USER_SECTION_IDS = (
    "vitrina",
    "supply",
    "reports",
    "feedbacks",
    "feedbacks.ai_review",
    "feedbacks.autoanswers_admin",
    "ads",
    "prices",
    "sku_management",
    "research",
    "instructions",
    "settings",
)


def _backfill_sheet_vitrina_user_access_columns(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT user_id, role
        FROM sheet_vitrina_v1_users
        """
    ).fetchall()
    for row in rows:
        role = str(row["role"] or "").strip()
        conn.execute(
            """
            UPDATE sheet_vitrina_v1_users
            SET allowed_sections_json = ?,
                manage_users = ?
            WHERE user_id = ?
            """,
            (
                json.dumps(_default_sheet_vitrina_sections_for_role(role), ensure_ascii=False),
                1 if _default_sheet_vitrina_manage_users_for_role(role) else 0,
                row["user_id"],
            ),
        )


def _normalize_sheet_vitrina_user_sections(value: Any, *, role: str = "") -> list[str]:
    raw_value = value
    if raw_value is None:
        return _default_sheet_vitrina_sections_for_role(role)
    if isinstance(raw_value, str):
        stripped = raw_value.strip()
        if not stripped:
            return _default_sheet_vitrina_sections_for_role(role)
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = []
        raw_value = parsed
    if not isinstance(raw_value, (list, tuple, set)):
        return _default_sheet_vitrina_sections_for_role(role)
    allowed: list[str] = []
    seen: set[str] = set()
    valid = set(_SHEET_VITRINA_USER_SECTION_IDS)
    for item in raw_value:
        section_id = str(item or "").strip()
        if section_id not in valid or section_id in seen:
            continue
        seen.add(section_id)
        allowed.append(section_id)
    return allowed


def _default_sheet_vitrina_sections_for_role(role: str) -> list[str]:
    normalized = str(role or "").strip()
    if normalized == "admin":
        return list(_SHEET_VITRINA_USER_SECTION_IDS)
    if normalized == "operator":
        # New knowledge-base access is intentionally opt-in for non-admin
        # users, including historical role-only records.
        return [
            section_id
            for section_id in _SHEET_VITRINA_USER_SECTION_IDS
            if section_id not in {"instructions", "feedbacks.ai_review", "feedbacks.autoanswers_admin"}
        ]
    if normalized == "supply_operator":
        return ["supply"]
    return []


def _default_sheet_vitrina_manage_users_for_role(role: str) -> bool:
    return str(role or "").strip() == "admin"


def _remove_incomplete_backup_destination(target: Path) -> None:
    """Remove only files owned by one backup attempt that did not complete."""
    failures: list[str] = []
    for candidate in (
        target,
        Path(str(target) + "-wal"),
        Path(str(target) + "-shm"),
        Path(str(target) + "-journal"),
    ):
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            failures.append(candidate.name)
    if failures:
        raise RuntimeError(
            "SQLite backup failed and incomplete destination cleanup failed: "
            + ", ".join(failures)
        )
