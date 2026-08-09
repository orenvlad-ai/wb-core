"""Guarded one-time Proxy V4 parameter and ready-snapshot initialization."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_buyout_mature_backfill import (  # noqa: E402
    _backup_and_verify,
    _digest,
    _file_digest,
    _query_only_connection,
    _require_evidence_outside_repo,
    _validate_exact_deployment,
    _write_private_json,
)
from packages.application.calculation_parameters_v4 import (  # noqa: E402
    PROXY_V4_BLOCK_KEY,
    PROXY_V4_FIXED_BOUNDARY,
    PROXY_V4_INITIAL_EFFECTIVE_DATES,
    ProxyV4Parameters,
    plan_initial_historical_versions,
)
from packages.application.proxy_v4_historical_projection import (  # noqa: E402
    project_proxy_v4_ready_snapshot,
    proxy_v4_non_target_digest,
    proxy_v4_target_digest,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.warehouse_sync_lock import (  # noqa: E402
    WarehouseSyncBusyError,
    warehouse_sync_lock,
)
from packages.business_time import current_business_date_iso  # noqa: E402


SCHEMA_VERSION = "sheet_vitrina_v1_proxy_v4_initialization_v1"
TARGET_DATE_FROM = PROXY_V4_FIXED_BOUNDARY
MAX_TARGET_DAYS = 31


class ProxyV4InitializationError(RuntimeError):
    """A guarded V4 initialization condition failed closed."""


def run_initialization(
    *,
    runtime_dir: Path,
    evidence_dir: Path,
    apply: bool,
    manifest_path: Path | None = None,
    expected_manifest_sha256: str | None = None,
    expected_deployed_sha: str | None = None,
    deployed_sha_file: Path | None = None,
    approval_reference: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    runtime_dir = runtime_dir.expanduser().resolve()
    evidence_dir = evidence_dir.expanduser().resolve()
    _require_evidence_outside_repo(evidence_dir)
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    if not runtime.db_path.is_file():
        raise ProxyV4InitializationError("canonical runtime SQLite DB is missing")
    effective_now = now or datetime.now(timezone.utc)
    if effective_now.tzinfo is None or effective_now.utcoffset() is None:
        raise ProxyV4InitializationError("now must be timezone-aware")
    business_date = current_business_date_iso(effective_now)
    if business_date < TARGET_DATE_FROM:
        raise ProxyV4InitializationError("current business date precedes the V4 boundary")
    if (datetime.fromisoformat(business_date).date() - datetime.fromisoformat(TARGET_DATE_FROM).date()).days + 1 > MAX_TARGET_DAYS:
        raise ProxyV4InitializationError("V4 initialization target window exceeds its bounded lifetime")
    created_at = effective_now.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    if not apply:
        return _build_manifest(
            runtime=runtime,
            evidence_dir=evidence_dir,
            business_date=business_date,
            created_at=created_at,
        )
    if (
        manifest_path is None
        or not expected_manifest_sha256
        or not expected_deployed_sha
        or not approval_reference
    ):
        raise ProxyV4InitializationError(
            "--apply requires a reviewed manifest SHA, exact deployed SHA and human approval reference"
        )
    sha_file = (
        deployed_sha_file.expanduser().resolve()
        if deployed_sha_file is not None
        else runtime_dir.parent / "app" / ".wb-core-runtime-sha"
    )
    deployed_sha = _validate_exact_deployment(
        expected_deployed_sha=expected_deployed_sha,
        deployed_sha_file=sha_file,
    )
    try:
        with warehouse_sync_lock(runtime.runtime_dir, blocking=False):
            return _apply_manifest(
                runtime=runtime,
                evidence_dir=evidence_dir,
                manifest_path=manifest_path.expanduser().resolve(),
                expected_manifest_sha256=str(expected_manifest_sha256),
                deployed_sha=deployed_sha,
                deployed_sha_file=sha_file,
                approval_reference=str(approval_reference).strip(),
                business_date=business_date,
                applied_at=created_at,
            )
    except WarehouseSyncBusyError as exc:
        raise ProxyV4InitializationError(
            "canonical warehouse writer is busy; no Proxy V4 initialization was attempted"
        ) from exc


def _build_manifest(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    evidence_dir: Path,
    business_date: str,
    created_at: str,
) -> dict[str, Any]:
    existing_versions = _load_version_rows(runtime.db_path)
    if existing_versions:
        raise ProxyV4InitializationError(
            "Proxy V4 version table is not empty; initial historical mutation will not append or rewrite it"
        )
    planned_versions = [
        replace(
            item,
            created_at=created_at,
            created_by="proxy_v4_historical_initialization",
        )
        for item in plan_initial_historical_versions(
            runtime=runtime,
            tax_rate_resolver=lambda day: _v3_tax_for_date(runtime.db_path, day),
            effective_dates=PROXY_V4_INITIAL_EFFECTIVE_DATES,
        )
    ]
    snapshots = _load_target_snapshots(
        runtime.db_path,
        date_from=TARGET_DATE_FROM,
        date_to=business_date,
    )
    if not snapshots:
        raise ProxyV4InitializationError("no ready snapshots exist on or after the V4 boundary")

    resolver = _resolver(planned_versions)
    planned_snapshots: list[dict[str, Any]] = []
    total_changed_cells = 0
    total_inserted_rows = 0
    for row in snapshots:
        transformed = project_proxy_v4_ready_snapshot(
            str(row["plan_json"]),
            parameters_for_date=resolver,
            materialized_at=created_at,
        )
        if transformed["non_target_before"] != transformed["non_target_after"]:
            raise ProxyV4InitializationError(
                "Proxy V4 projection changed a non-V4 ready-snapshot field"
            )
        for day, coverage in transformed["eligibility_by_date"].items():
            if day >= TARGET_DATE_FROM and int(coverage["eligible_sku_count"]) <= 0:
                raise ProxyV4InitializationError(
                    f"Proxy V4 has no eligible SKU operands for required date {day}"
                )
        planned_snapshots.append(
            {
                "bundle_version": str(row["bundle_version"]),
                "as_of_date": str(row["as_of_date"]),
                "before_plan_sha256": _digest(json.loads(str(row["plan_json"]))),
                "before_non_target_digest": transformed["non_target_before"],
                "after_plan_json": transformed["after_plan_json"],
                "after_plan_sha256": _digest(json.loads(transformed["after_plan_json"])),
                "after_target_digest": transformed["target_digest"],
                "eligibility_by_date": transformed["eligibility_by_date"],
                "changed_cells": int(transformed["changed_cells"]),
                "inserted_rows": int(transformed["inserted_rows"]),
            }
        )
        total_changed_cells += int(transformed["changed_cells"])
        total_inserted_rows += int(transformed["inserted_rows"])

    pre_change = {
        "v4_version_rows_digest": _digest(existing_versions),
        "target_snapshot_rows_digest": _target_snapshot_rows_digest(snapshots),
        "target_snapshot_non_v4_digest": _digest(
            [
                [
                    str(row["bundle_version"]),
                    str(row["as_of_date"]),
                    proxy_v4_non_target_digest(str(row["plan_json"])),
                ]
                for row in snapshots
            ]
        ),
        "non_target_ready_snapshot_digest": _non_target_ready_snapshot_digest(
            runtime.db_path,
            targets={(str(row["bundle_version"]), str(row["as_of_date"])) for row in snapshots},
        ),
        "v3_parameter_digest": _v3_parameter_digest(runtime.db_path),
    }
    version_rows = [_planned_version_row(item) for item in planned_versions]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "ready",
        "mode": "dry-run",
        "database_written": False,
        "created_at": created_at,
        "business_date": business_date,
        "scope": {
            "fixed_boundary": TARGET_DATE_FROM,
            "date_from": TARGET_DATE_FROM,
            "date_to": business_date,
            "historical_version_effective_dates": list(PROXY_V4_INITIAL_EFFECTIVE_DATES),
            "version_table": "sheet_vitrina_v1_proxy_v4_parameter_versions",
            "ready_snapshot_table": "sheet_vitrina_v1_ready_snapshots",
            "target_snapshot_keys": [
                [item["bundle_version"], item["as_of_date"]]
                for item in planned_snapshots
            ],
        },
        "pre_change": pre_change,
        "desired": {
            "version_rows": version_rows,
            "version_rows_digest": _digest(version_rows),
            "ready_snapshots": planned_snapshots,
            "ready_snapshot_target_digest": _digest(
                [
                    [item["bundle_version"], item["as_of_date"], item["after_target_digest"]]
                    for item in planned_snapshots
                ]
            ),
        },
        "expected_effect": {
            "insert_version_count": len(version_rows),
            "update_ready_snapshot_count": len(planned_snapshots),
            "insert_v4_row_count": total_inserted_rows,
            "change_v4_cell_count": total_changed_cells,
            "non_target_invariant": (
                "V3 versions/formula, every non-V4 ready-snapshot field and every non-target ready snapshot remain byte/semantic identical"
            ),
        },
        "idempotency": "exact desired V4 version rows plus per-snapshot V4 target digest; repeated apply is already_applied",
        "recovery": "a coherent verified SQLite backup is created before one atomic exact-key transaction",
    }
    evidence_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = evidence_dir / (
        f"proxy-v4-initialization-plan-{created_at.replace(':', '').replace('-', '')}.json"
    )
    _write_private_json(manifest_path, manifest)
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "dry-run",
        "status": "ready",
        "database_written": False,
        "manifest_path": str(manifest_path),
        "manifest_sha256": _file_digest(manifest_path),
        "date_from": TARGET_DATE_FROM,
        "date_to": business_date,
        "planned_version_count": len(version_rows),
        "target_snapshot_count": len(planned_snapshots),
        "insert_v4_row_count": total_inserted_rows,
        "change_v4_cell_count": total_changed_cells,
        "pre_change_v4_version_digest": pre_change["v4_version_rows_digest"],
        "pre_change_target_snapshot_digest": pre_change["target_snapshot_rows_digest"],
        "desired_version_digest": manifest["desired"]["version_rows_digest"],
        "desired_snapshot_target_digest": manifest["desired"]["ready_snapshot_target_digest"],
    }


def _apply_manifest(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    evidence_dir: Path,
    manifest_path: Path,
    expected_manifest_sha256: str,
    deployed_sha: str,
    deployed_sha_file: Path,
    approval_reference: str,
    business_date: str,
    applied_at: str,
) -> dict[str, Any]:
    if not manifest_path.is_file():
        raise ProxyV4InitializationError("reviewed V4 manifest is missing")
    manifest_sha256 = _file_digest(manifest_path)
    if manifest_sha256 != expected_manifest_sha256:
        raise ProxyV4InitializationError("reviewed V4 manifest SHA-256 mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("status") != "ready":
        raise ProxyV4InitializationError("reviewed V4 manifest is not ready")
    if not approval_reference or len(approval_reference) > 500:
        raise ProxyV4InitializationError("human approval reference is missing or invalid")
    if business_date < str(manifest.get("business_date") or ""):
        raise ProxyV4InitializationError("business date moved backwards after dry-run")
    _validate_exact_deployment(
        expected_deployed_sha=deployed_sha,
        deployed_sha_file=deployed_sha_file,
    )
    desired = dict(manifest.get("desired") or {})
    desired_versions = list(desired.get("version_rows") or [])
    desired_snapshots = list(desired.get("ready_snapshots") or [])
    if not desired_versions or not desired_snapshots:
        raise ProxyV4InitializationError("reviewed V4 manifest has empty targets")
    _require_v4_schema(runtime.db_path)

    existing_versions = _load_version_rows(runtime.db_path)
    current_snapshots = _load_exact_snapshots(
        runtime.db_path,
        keys=[(str(item["bundle_version"]), str(item["as_of_date"])) for item in desired_snapshots],
    )
    if _is_desired_state(existing_versions, current_snapshots, desired_versions, desired_snapshots):
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "apply",
            "status": "already_applied",
            "database_written": False,
            "manifest_sha256": manifest_sha256,
            "deployed_sha": deployed_sha,
            "approval_reference": approval_reference,
            "idempotent_noop": True,
            "non_target_preserved": True,
        }

    pre = dict(manifest.get("pre_change") or {})
    if _digest(existing_versions) != str(pre.get("v4_version_rows_digest") or ""):
        raise ProxyV4InitializationError("V4 version rows changed after dry-run")
    if _target_snapshot_rows_digest(current_snapshots) != str(pre.get("target_snapshot_rows_digest") or ""):
        raise ProxyV4InitializationError("target ready snapshots changed after dry-run")
    target_keys = {
        (str(item["bundle_version"]), str(item["as_of_date"])) for item in desired_snapshots
    }
    if _non_target_ready_snapshot_digest(runtime.db_path, targets=target_keys) != str(
        pre.get("non_target_ready_snapshot_digest") or ""
    ):
        raise ProxyV4InitializationError("non-target ready snapshots changed after dry-run")
    if _v3_parameter_digest(runtime.db_path) != str(pre.get("v3_parameter_digest") or ""):
        raise ProxyV4InitializationError("V3 parameters changed after dry-run")

    current_semantics = [
        _version_semantic(item)
        for item in plan_initial_historical_versions(
            runtime=runtime,
            tax_rate_resolver=lambda day: _v3_tax_for_date(runtime.db_path, day),
            effective_dates=PROXY_V4_INITIAL_EFFECTIVE_DATES,
        )
    ]
    if current_semantics != [_version_semantic(item) for item in desired_versions]:
        raise ProxyV4InitializationError("confirmed Buyout/Finance source versions drifted after dry-run")

    evidence_dir.mkdir(parents=True, exist_ok=True)
    backup_path = evidence_dir / "backups" / (
        f"proxy-v4-{applied_at.replace(':', '').replace('-', '')}-{manifest_sha256[-12:]}.sqlite3"
    )
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_sha256 = _backup_and_verify(runtime.db_path, backup_path)
    with sqlite3.connect(runtime.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM sheet_vitrina_v1_proxy_v4_parameter_versions LIMIT 1"
        ).fetchone() is not None:
            raise ProxyV4InitializationError("V4 version target stopped being empty")
        for row in desired_versions:
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_proxy_v4_parameter_versions(
                       version_id,block_key,revision,effective_date,source_window_from,
                       source_window_to,source_window_fingerprint,parameters_json,
                       fingerprint,version_kind,created_by,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["version_id"],
                    row["block_key"],
                    row["revision"],
                    row["effective_date"],
                    row["source_window_from"],
                    row["source_window_to"],
                    row["source_window_fingerprint"],
                    row["parameters_json"],
                    row["fingerprint"],
                    row["version_kind"],
                    row["created_by"],
                    row["created_at"],
                ),
            )
        before_by_key = {
            (str(row["bundle_version"]), str(row["as_of_date"])): str(row["plan_json"])
            for row in current_snapshots
        }
        for item in desired_snapshots:
            key = (str(item["bundle_version"]), str(item["as_of_date"]))
            cursor = conn.execute(
                """UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?
                   WHERE bundle_version=? AND as_of_date=? AND plan_json=?""",
                (
                    str(item["after_plan_json"]),
                    key[0],
                    key[1],
                    before_by_key[key],
                ),
            )
            if cursor.rowcount != 1:
                raise ProxyV4InitializationError(
                    f"target snapshot compare-and-swap failed: {key[1]}"
                )
        conn.commit()

    after_versions = _load_version_rows(runtime.db_path)
    after_snapshots = _load_exact_snapshots(runtime.db_path, keys=sorted(target_keys))
    if not _is_desired_state(after_versions, after_snapshots, desired_versions, desired_snapshots):
        raise ProxyV4InitializationError("post-apply V4 target readback mismatch")
    if _non_target_ready_snapshot_digest(runtime.db_path, targets=target_keys) != str(
        pre.get("non_target_ready_snapshot_digest") or ""
    ):
        raise ProxyV4InitializationError("post-apply non-target ready snapshot digest mismatch")
    if _v3_parameter_digest(runtime.db_path) != str(pre.get("v3_parameter_digest") or ""):
        raise ProxyV4InitializationError("post-apply V3 parameter digest mismatch")

    reconciliation = {
        "schema_version": SCHEMA_VERSION,
        "status": "reconciled",
        "applied_at": applied_at,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "deployed_sha": deployed_sha,
        "deployed_sha_file": str(deployed_sha_file),
        "approval_reference": approval_reference,
        "backup_path": str(backup_path),
        "backup_sha256": backup_sha256,
        "inserted_version_count": len(desired_versions),
        "updated_ready_snapshot_count": len(desired_snapshots),
        "post_version_digest": _digest(after_versions),
        "post_snapshot_target_digest": _digest(
            [
                [
                    str(row["bundle_version"]),
                    str(row["as_of_date"]),
                    proxy_v4_target_digest(str(row["plan_json"])),
                ]
                for row in after_snapshots
            ]
        ),
        "non_target_ready_snapshot_digest": str(pre["non_target_ready_snapshot_digest"]),
        "v3_parameter_digest": str(pre["v3_parameter_digest"]),
        "non_target_preserved": True,
        "idempotent_noop": False,
    }
    reconciliation_path = evidence_dir / (
        f"proxy-v4-initialization-reconciliation-{applied_at.replace(':', '').replace('-', '')}.json"
    )
    _write_private_json(reconciliation_path, reconciliation)
    reconciliation_sha256 = _file_digest(reconciliation_path)
    evidence_sha256 = _digest(
        {
            "manifest_sha256": manifest_sha256,
            "deployed_sha": deployed_sha,
            "approval_reference": approval_reference,
            "backup_sha256": backup_sha256,
            "reconciliation_sha256": reconciliation_sha256,
        }
    )
    return {
        **reconciliation,
        "mode": "apply",
        "database_written": True,
        "reconciliation_path": str(reconciliation_path),
        "reconciliation_sha256": reconciliation_sha256,
        "evidence_sha256": evidence_sha256,
    }


def _planned_version_row(item: ProxyV4Parameters) -> dict[str, Any]:
    public = item.public()
    fingerprint = _digest(
        {
            "contract": "sheet_vitrina_v1_proxy_v4_parameters_v1",
            "effective_date": item.effective_date,
            "source_window_fingerprint": item.source_window_fingerprint,
            "rates": {
                key: public[key]
                for key in (
                    "buyout_rate",
                    "tax_rate",
                    "agent_remuneration_rate",
                    "acquiring_rate",
                    "wb_logistics_rate",
                    "wb_storage_rate",
                    "penalties_adjustments_rate",
                    "other_expense_rate",
                )
            },
        }
    )
    public["fingerprint"] = fingerprint
    return {
        "version_id": item.version_id,
        "block_key": PROXY_V4_BLOCK_KEY,
        "revision": item.revision,
        "effective_date": item.effective_date,
        "source_window_from": item.source_window_from,
        "source_window_to": item.source_window_to,
        "source_window_fingerprint": item.source_window_fingerprint,
        "parameters_json": json.dumps(
            public,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
        "fingerprint": fingerprint,
        "version_kind": "historical_initialization",
        "created_by": item.created_by,
        "created_at": item.created_at,
    }


def _version_semantic(item: ProxyV4Parameters | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(item, ProxyV4Parameters):
        values = item.public()
    else:
        raw = item.get("parameters_json")
        values = json.loads(str(raw)) if raw else dict(item)
    return {
        key: values.get(key)
        for key in (
            "effective_date",
            "buyout_rate",
            "tax_rate",
            "agent_remuneration_rate",
            "acquiring_rate",
            "wb_logistics_rate",
            "wb_storage_rate",
            "penalties_adjustments_rate",
            "other_expense_rate",
            "source_window_from",
            "source_window_to",
            "source_window_fingerprint",
        )
    }


def _resolver(
    versions: list[ProxyV4Parameters],
):
    ordered = sorted(versions, key=lambda item: (item.effective_date, item.revision))

    def resolve(day: str) -> ProxyV4Parameters | None:
        candidates = [item for item in ordered if item.effective_date <= str(day)[:10]]
        return candidates[-1] if candidates else None

    return resolve


def _v3_tax_for_date(db_path: Path, day: str) -> Decimal:
    with _query_only_connection(db_path) as conn:
        row = conn.execute(
            """SELECT rates_json FROM sheet_vitrina_v1_calculation_parameter_versions
               WHERE block_key=? AND effective_date<=?
               ORDER BY effective_date DESC,revision DESC,created_at DESC LIMIT 1""",
            ("proxy_profit_margin", day),
        ).fetchone()
    if row is None:
        raise ProxyV4InitializationError(f"V3 tax seed is unavailable for {day}")
    return Decimal(str(json.loads(str(row[0])).get("tax_rate")))


def _load_version_rows(db_path: Path) -> list[dict[str, Any]]:
    with _query_only_connection(db_path) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type=? AND name=?",
            ("table", "sheet_vitrina_v1_proxy_v4_parameter_versions"),
        ).fetchone()
        if exists is None:
            return []
        columns = [
            "version_id",
            "block_key",
            "revision",
            "effective_date",
            "source_window_from",
            "source_window_to",
            "source_window_fingerprint",
            "parameters_json",
            "fingerprint",
            "version_kind",
            "created_by",
            "created_at",
        ]
        rows = conn.execute(
            f"SELECT {','.join(columns)} FROM sheet_vitrina_v1_proxy_v4_parameter_versions ORDER BY revision"
        ).fetchall()
    return [dict(zip(columns, row)) for row in rows]


def _load_target_snapshots(
    db_path: Path,
    *,
    date_from: str,
    date_to: str,
) -> list[dict[str, Any]]:
    with _query_only_connection(db_path) as conn:
        rows = conn.execute(
            """SELECT bundle_version,as_of_date,plan_json
               FROM sheet_vitrina_v1_ready_snapshots
               WHERE as_of_date>=? AND as_of_date<=?
               ORDER BY bundle_version,as_of_date""",
            (date_from, date_to),
        ).fetchall()
    return [
        {"bundle_version": str(row[0]), "as_of_date": str(row[1]), "plan_json": str(row[2])}
        for row in rows
    ]


def _load_exact_snapshots(
    db_path: Path,
    *,
    keys: list[tuple[str, str]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    with _query_only_connection(db_path) as conn:
        for bundle_version, as_of_date in keys:
            row = conn.execute(
                """SELECT bundle_version,as_of_date,plan_json
                   FROM sheet_vitrina_v1_ready_snapshots
                   WHERE bundle_version=? AND as_of_date=?""",
                (bundle_version, as_of_date),
            ).fetchone()
            if row is None:
                raise ProxyV4InitializationError(
                    f"target ready snapshot disappeared: {as_of_date}"
                )
            result.append(
                {
                    "bundle_version": str(row[0]),
                    "as_of_date": str(row[1]),
                    "plan_json": str(row[2]),
                }
            )
    return result


def _target_snapshot_rows_digest(rows: list[Mapping[str, Any]]) -> str:
    return _digest(
        [
            [
                str(row["bundle_version"]),
                str(row["as_of_date"]),
                json.loads(str(row["plan_json"])),
            ]
            for row in rows
        ]
    )


def _non_target_ready_snapshot_digest(
    db_path: Path,
    *,
    targets: set[tuple[str, str]],
) -> str:
    with _query_only_connection(db_path) as conn:
        rows = conn.execute(
            """SELECT bundle_version,as_of_date,plan_json
               FROM sheet_vitrina_v1_ready_snapshots ORDER BY bundle_version,as_of_date"""
        ).fetchall()
    return _digest(
        [list(row) for row in rows if (str(row[0]), str(row[1])) not in targets]
    )


def _v3_parameter_digest(db_path: Path) -> str:
    with _query_only_connection(db_path) as conn:
        rows = conn.execute(
            """SELECT version_id,block_key,revision,effective_date,rates_json,fingerprint,
                      source,created_by,created_at
               FROM sheet_vitrina_v1_calculation_parameter_versions
               WHERE block_key=? ORDER BY revision""",
            ("proxy_profit_margin",),
        ).fetchall()
    return _digest([list(row) for row in rows])


def _require_v4_schema(db_path: Path) -> None:
    with _query_only_connection(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type=? AND name=?",
            ("table", "sheet_vitrina_v1_proxy_v4_parameter_versions"),
        ).fetchone()
    if row is None:
        raise ProxyV4InitializationError(
            "deployed Proxy V4 schema is missing; canonical service initialization must run first"
        )


def _is_desired_state(
    current_versions: list[Mapping[str, Any]],
    current_snapshots: list[Mapping[str, Any]],
    desired_versions: list[Mapping[str, Any]],
    desired_snapshots: list[Mapping[str, Any]],
) -> bool:
    if _digest(current_versions) != _digest(desired_versions):
        return False
    desired_by_key = {
        (str(item["bundle_version"]), str(item["as_of_date"])): str(item["after_target_digest"])
        for item in desired_snapshots
    }
    return all(
        proxy_v4_target_digest(str(row["plan_json"]))
        == desired_by_key.get((str(row["bundle_version"]), str(row["as_of_date"])))
        for row in current_snapshots
    ) and len(current_snapshots) == len(desired_snapshots)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--evidence-dir", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--manifest")
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--expected-deployed-sha")
    parser.add_argument("--deployed-sha-file")
    parser.add_argument("--approval-reference")
    args = parser.parse_args()
    if not args.apply:
        args.dry_run = True
    return args


def main() -> None:
    args = _parse_args()
    try:
        result = run_initialization(
            runtime_dir=Path(args.runtime_dir),
            evidence_dir=Path(args.evidence_dir),
            apply=bool(args.apply),
            manifest_path=Path(args.manifest) if args.manifest else None,
            expected_manifest_sha256=args.expected_manifest_sha256,
            expected_deployed_sha=args.expected_deployed_sha,
            deployed_sha_file=Path(args.deployed_sha_file) if args.deployed_sha_file else None,
            approval_reference=args.approval_reference,
        )
    except (ProxyV4InitializationError, ValueError) as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "mode": "apply" if args.apply else "dry-run",
            "status": "blocked",
            "database_written": False,
            "blocker": str(exc),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if result.get("status") == "blocked":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
