"""Application-слой DB-backed runtime для registry upload."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any, Mapping

from packages.application.cost_price_upload import CostPriceUploadBlock, parse_cost_price_upload_payload
from packages.application.registry_upload_bundle_v1 import (
    RegistryUploadBundleV1Block,
    load_registry_upload_bundle_v1_from_path,
    parse_registry_upload_bundle_v1_payload,
)
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

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS_DIR = ROOT / "artifacts" / "registry_upload_db_backed_runtime"
INPUT_BUNDLE_FIXTURE = ARTIFACTS_DIR / "input" / "registry_upload_bundle__fixture.json"
DB_FILENAME = "registry_upload_runtime.sqlite3"


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
        self.runtime_dir = runtime_dir
        self.db_path = runtime_dir / DB_FILENAME
        self.bundle_block = bundle_block or RegistryUploadBundleV1Block()
        self.cost_price_block = cost_price_block or CostPriceUploadBlock()

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

    def save_factory_order_result_state(
        self,
        *,
        calculated_at: str,
        payload: Mapping[str, Any],
    ) -> None:
        _validate_timestamp(calculated_at, field_name="calculated_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_factory_order_result_state(
                    slot,
                    calculated_at,
                    result_json
                )
                VALUES(1, ?, ?)
                ON CONFLICT(slot) DO UPDATE SET
                    calculated_at = excluded.calculated_at,
                    result_json = excluded.result_json
                """,
                (
                    calculated_at,
                    json.dumps(dict(payload), ensure_ascii=False),
                ),
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

    def save_wb_regional_supply_result_state(
        self,
        *,
        calculated_at: str,
        payload: Mapping[str, Any],
    ) -> None:
        _validate_timestamp(calculated_at, field_name="calculated_at")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_wb_regional_supply_result_state(
                    slot,
                    calculated_at,
                    result_json
                )
                VALUES(1, ?, ?)
                ON CONFLICT(slot) DO UPDATE SET
                    calculated_at = excluded.calculated_at,
                    result_json = excluded.result_json
                """,
                (
                    calculated_at,
                    json.dumps(dict(payload), ensure_ascii=False),
                ),
            )
            conn.commit()

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


def _validate_iso_date(value: str, field_name: str) -> None:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid ISO 8601 date") from exc


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
        return _coverage_reason(requested_count=requested_count, covered_count=covered_count)
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
    replacements = (
        (
            "source is not available for today_current in the bounded live contour; today column stays blank instead of inventing fresh values",
            "текущий день для этого источника не требуется",
        ),
        (
            "source is current-only in the bounded live contour; yesterday_closed is left blank instead of backfilling current values into a closed-day column",
            "закрытый день для этого источника materialize-ится только через current-rollover",
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


def _note_requires_warning(note: str) -> bool:
    normalized = str(note or "").strip()
    if not normalized:
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
        return plan
    return SheetVitrinaV1Envelope(
        plan_version=plan.plan_version,
        snapshot_id=plan.snapshot_id,
        as_of_date=plan.as_of_date,
        date_columns=plan.date_columns,
        temporal_slots=plan.temporal_slots,
        source_temporal_policies=effective_policies,
        sheets=plan.sheets,
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
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_factory_order_result_state (
            slot INTEGER PRIMARY KEY CHECK (slot = 1),
            calculated_at TEXT NOT NULL,
            result_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_wb_regional_supply_result_state (
            slot INTEGER PRIMARY KEY CHECK (slot = 1),
            calculated_at TEXT NOT NULL,
            result_json TEXT NOT NULL
        );
        """
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


def _ensure_column(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    column_name: str,
    column_sql: str,
) -> None:
    existing = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    if any(str(row["name"]) == column_name for row in existing):
        return
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")
