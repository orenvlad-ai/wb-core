"""Application-слой DB-backed runtime для registry upload."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from packages.application.registry_upload_bundle_v1 import (
    RegistryUploadBundleV1Block,
    load_registry_upload_bundle_v1_from_path,
    parse_registry_upload_bundle_v1_payload,
)
from packages.application.sheet_vitrina_v1 import parse_sheet_write_plan_payload
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
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope, SheetVitrinaV1RefreshResult

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS_DIR = ROOT / "artifacts" / "registry_upload_db_backed_runtime"
INPUT_BUNDLE_FIXTURE = ARTIFACTS_DIR / "input" / "registry_upload_bundle__fixture.json"
DB_FILENAME = "registry_upload_runtime.sqlite3"


class RegistryUploadDbBackedRuntime:
    def __init__(
        self,
        runtime_dir: Path,
        bundle_block: RegistryUploadBundleV1Block | None = None,
    ) -> None:
        self.runtime_dir = runtime_dir
        self.db_path = runtime_dir / DB_FILENAME
        self.bundle_block = bundle_block or RegistryUploadBundleV1Block()

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

        return SheetVitrinaV1RefreshResult(
            status="success",
            bundle_version=current_state.bundle_version,
            activated_at=current_state.activated_at,
            refreshed_at=refreshed_at,
            as_of_date=plan.as_of_date,
            snapshot_id=plan.snapshot_id,
            plan_version=plan.plan_version,
            sheet_row_counts=_sheet_row_counts_from_plan(plan),
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
            return SheetVitrinaV1RefreshResult(
                status="success",
                bundle_version=current_state.bundle_version,
                activated_at=row["activated_at"],
                refreshed_at=row["refreshed_at"],
                as_of_date=row["as_of_date"],
                snapshot_id=row["snapshot_id"],
                plan_version=row["plan_version"],
                sheet_row_counts=_sheet_row_counts_from_plan(plan),
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


def _coerce_bundle(bundle_input: RegistryUploadBundleV1 | Mapping[str, Any]) -> RegistryUploadBundleV1:
    if isinstance(bundle_input, RegistryUploadBundleV1):
        return bundle_input
    return parse_registry_upload_bundle_v1_payload(bundle_input)


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


def _validate_timestamp(value: str, field_name: str) -> None:
    if not value.endswith("Z"):
        raise ValueError(f"{field_name} must be an ISO 8601 UTC timestamp ending with Z")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid ISO 8601 timestamp") from exc


def _sheet_row_counts_from_plan(plan: SheetVitrinaV1Envelope) -> dict[str, int]:
    return {item.sheet_name: item.row_count for item in plan.sheets}


def _serialize_sheet_vitrina_plan(plan: SheetVitrinaV1Envelope) -> str:
    payload = {
        "plan_version": plan.plan_version,
        "snapshot_id": plan.snapshot_id,
        "as_of_date": plan.as_of_date,
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
    return parse_sheet_write_plan_payload(payload)


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
        """
    )
