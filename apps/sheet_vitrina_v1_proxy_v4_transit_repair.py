"""Guarded Proxy V4 transit exclusion and closed-date repair."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Callable, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_buyout_mature_backfill import (  # noqa: E402
    _digest,
    _file_digest,
    _query_only_connection,
    _require_evidence_outside_repo,
    _validate_exact_deployment,
    _write_private_json,
)
from apps.sheet_vitrina_v1_proxy_v4_initialize import (  # noqa: E402
    _load_target_snapshots,
    _load_version_rows,
    _non_target_ready_snapshot_digest,
    _target_snapshot_rows_digest,
    _v3_parameter_digest,
)
from packages.application.calculation_parameters_v4 import (  # noqa: E402
    AUTOMATIC_RATE_FIELDS,
    PROXY_V4_BLOCK_KEY,
    PROXY_V4_CONTRACT_VERSION,
    PROXY_V4_FORMULA_VERSION,
    PROXY_V4_LEGACY_FORMULA_VERSION,
    ProxyV4Parameters,
    _parameter_fingerprint,
    _parameters_from_row,
    _parameters_from_values,
    build_latest_confirmed_week_window,
)
from packages.application.proxy_v4_historical_projection import (  # noqa: E402
    project_proxy_v4_ready_snapshot,
    proxy_v4_non_target_digest,
    proxy_v4_target_digest,
    reconcile_proxy_v4_target_window,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.warehouse_sync_lock import (  # noqa: E402
    WarehouseSyncBusyError,
    warehouse_sync_lock,
)
from packages.business_time import current_business_date_iso  # noqa: E402


SCHEMA_VERSION = "sheet_vitrina_v1_proxy_v4_transit_repair_v1"
TARGET_DATE_FROM = "2026-08-14"
CORRECTION_EFFECTIVE_DATE = "2026-08-16"
CORRECTION_VERSION_KIND = "historical_correction_no_transit"
MAX_TARGET_DAYS = 31
PROTECTED_OPERATIONAL_TABLES = (
    "sheet_vitrina_v1_canonical_cost_components",
    "sheet_vitrina_v1_canonical_cost_daily_state",
    "sheet_vitrina_v1_canonical_cost_movement_layers",
    "sheet_vitrina_v1_canonical_cost_wb_outstanding_layers",
    "sheet_vitrina_v1_warehouse_wb_daily_cost",
    "sheet_vitrina_v1_wb_cost_daily_state",
    "sheet_vitrina_v1_wb_supply_cost_layers",
    "wb_finance_weekly_aggregates",
    "wb_finance_weekly_cost_coverage",
    "wb_finance_weekly_reconciliation",
    "wb_finance_weekly_sku_aggregates",
    "wb_finance_weekly_sync",
)
REQUIRED_PROTECTED_TABLES = (
    "sheet_vitrina_v1_warehouse_wb_daily_cost",
    "sheet_vitrina_v1_wb_cost_daily_state",
    "wb_finance_weekly_aggregates",
    "wb_finance_weekly_sku_aggregates",
    "wb_finance_weekly_sync",
)


class ProxyV4TransitRepairError(RuntimeError):
    """A guarded transit-exclusion repair condition failed closed."""


def run_transit_repair(
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
        raise ProxyV4TransitRepairError("canonical operational SQLite DB is missing")
    effective_now = now or datetime.now(timezone.utc)
    if effective_now.tzinfo is None or effective_now.utcoffset() is None:
        raise ProxyV4TransitRepairError("now must be timezone-aware")
    business_date = current_business_date_iso(effective_now)
    last_closed_date = (date.fromisoformat(business_date) - timedelta(days=1)).isoformat()
    _validate_window(last_closed_date=last_closed_date)
    created_at = (
        effective_now.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    if not apply:
        return _build_manifest(
            runtime=runtime,
            evidence_dir=evidence_dir,
            business_date=business_date,
            last_closed_date=last_closed_date,
            created_at=created_at,
        )
    if (
        manifest_path is None
        or not expected_manifest_sha256
        or not expected_deployed_sha
        or not approval_reference
    ):
        raise ProxyV4TransitRepairError(
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
                last_closed_date=last_closed_date,
                applied_at=created_at,
            )
    except WarehouseSyncBusyError as exc:
        raise ProxyV4TransitRepairError(
            "canonical warehouse writer is busy; no Proxy V4 repair was attempted"
        ) from exc


def _build_manifest(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    evidence_dir: Path,
    business_date: str,
    last_closed_date: str,
    created_at: str,
) -> dict[str, Any]:
    state = _build_desired_state(
        runtime=runtime,
        last_closed_date=last_closed_date,
        correction_created_at=created_at,
    )
    if state["already_applied"]:
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "dry-run",
            "status": "already_reconciled",
            "database_written": False,
            "business_date": business_date,
            "last_closed_date": last_closed_date,
            "correction_version_id": state["correction"]["version_id"],
            "idempotent_noop": True,
        }
    snapshots = state["current_snapshots"]
    target_keys = {
        (str(item["bundle_version"]), str(item["as_of_date"])) for item in snapshots
    }
    pre_change = {
        "legacy_v4_version_rows_digest": _digest(state["legacy_version_rows"]),
        "target_snapshot_rows_digest": _target_snapshot_rows_digest(snapshots),
        "target_snapshot_non_v4_digest": _digest(
            [
                [
                    item["bundle_version"],
                    item["as_of_date"],
                    proxy_v4_non_target_digest(str(item["plan_json"])),
                ]
                for item in snapshots
            ]
        ),
        "non_target_ready_snapshot_digest": _non_target_ready_snapshot_digest(
            runtime.db_path,
            targets=target_keys,
        ),
        "v3_parameter_digest": _v3_parameter_digest(runtime.db_path),
        "protected_operational_digest": _protected_operational_digest(runtime.db_path),
        "finance_raw_store_identity": _finance_raw_store_identity(runtime),
    }
    desired_snapshots = state["desired_snapshots"]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "ready",
        "mode": "dry-run",
        "database_written": False,
        "created_at": created_at,
        "business_date": business_date,
        "last_closed_date": last_closed_date,
        "formula_contract": {
            "parameter_contract_version": PROXY_V4_CONTRACT_VERSION,
            "formula_version": PROXY_V4_FORMULA_VERSION,
            "other_expense": (
                "subscriptions + paid_services + review_points + other_deductions + "
                "acceptance - capitalized_acceptance"
            ),
            "excluded": ["transit_logistics", "capitalized_transit_logistics"],
        },
        "scope": {
            "date_from": TARGET_DATE_FROM,
            "date_to": last_closed_date,
            "correction_effective_date": CORRECTION_EFFECTIVE_DATE,
            "target_snapshot_keys": [
                [item["bundle_version"], item["as_of_date"]]
                for item in desired_snapshots
            ],
            "expected_snapshot_count": len(_date_range(TARGET_DATE_FROM, last_closed_date)),
            "target_tables": [
                "sheet_vitrina_v1_proxy_v4_parameter_versions",
                "sheet_vitrina_v1_ready_snapshots.plan_json V4 rows/metadata only",
            ],
        },
        "source": state["source_evidence"],
        "pre_change": pre_change,
        "desired": {
            "correction_version": state["correction"],
            "ready_snapshots": desired_snapshots,
            "target_digest": _digest(
                [
                    [item["bundle_version"], item["as_of_date"], item["after_plan_sha256"]]
                    for item in desired_snapshots
                ]
            ),
        },
        "expected_effect": {
            "inserted_parameter_revision_count": 1,
            "updated_ready_snapshot_count": sum(
                int(item["before_plan_sha256"] != item["after_plan_sha256"])
                for item in desired_snapshots
            ),
            "changed_v4_cell_count": sum(
                int(item["changed_cells"]) for item in desired_snapshots
            ),
            "inserted_v4_row_count": sum(
                int(item["inserted_rows"]) for item in desired_snapshots
            ),
            "non_target_invariant": (
                "V3, Finance facts/projections, canonical WAC/COGS, all prior V4 revisions, "
                "all non-V4 snapshot fields and all non-target ready snapshots remain unchanged"
            ),
        },
        "idempotency": (
            "exact correction row plus exact per-snapshot V4 target plan; repeated reviewed apply is already_applied"
        ),
        "recovery": (
            "capacity-checked coherent verified operational SQLite backup before one atomic insert/CAS transaction"
        ),
    }
    evidence_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = evidence_dir / (
        f"proxy-v4-transit-repair-plan-{created_at.replace(':', '').replace('-', '')}.json"
    )
    _write_private_json(manifest_path, manifest)
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "dry-run",
        "status": "ready",
        "database_written": False,
        "manifest_path": str(manifest_path),
        "manifest_sha256": _file_digest(manifest_path),
        "business_date": business_date,
        "date_from": TARGET_DATE_FROM,
        "date_to": last_closed_date,
        "target_snapshot_count": len(desired_snapshots),
        "correction_version_id": state["correction"]["version_id"],
        "legacy_other_expense_rate": state["source_evidence"]["legacy_other_expense_rate"],
        "corrected_other_expense_rate": state["source_evidence"]["corrected_other_expense_rate"],
        "transit_residual_rate": state["source_evidence"]["transit_residual_rate"],
        "changed_v4_cell_count": manifest["expected_effect"]["changed_v4_cell_count"],
        "inserted_v4_row_count": manifest["expected_effect"]["inserted_v4_row_count"],
        "protected_operational_digest": pre_change["protected_operational_digest"],
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
    last_closed_date: str,
    applied_at: str,
) -> dict[str, Any]:
    if not manifest_path.is_file():
        raise ProxyV4TransitRepairError("reviewed Proxy V4 transit manifest is missing")
    manifest_sha256 = _file_digest(manifest_path)
    if manifest_sha256 != expected_manifest_sha256:
        raise ProxyV4TransitRepairError("reviewed Proxy V4 transit manifest SHA-256 mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("status") != "ready":
        raise ProxyV4TransitRepairError("reviewed Proxy V4 transit manifest is not ready")
    if not approval_reference or len(approval_reference) > 500:
        raise ProxyV4TransitRepairError("human approval reference is missing or invalid")
    if business_date != str(manifest.get("business_date") or ""):
        raise ProxyV4TransitRepairError(
            "business date changed after dry-run; rebuild the manifest through the new last closed date"
        )
    if last_closed_date != str(manifest.get("last_closed_date") or ""):
        raise ProxyV4TransitRepairError("last closed date changed after dry-run")
    _validate_exact_deployment(
        expected_deployed_sha=deployed_sha,
        deployed_sha_file=deployed_sha_file,
    )
    desired = dict(manifest.get("desired") or {})
    desired_correction = dict(desired.get("correction_version") or {})
    desired_snapshots = list(desired.get("ready_snapshots") or [])
    if not desired_correction or not desired_snapshots:
        raise ProxyV4TransitRepairError("reviewed manifest has empty targets")

    current_versions = _load_version_rows(runtime.db_path)
    current_snapshots = _load_target_snapshots(
        runtime.db_path,
        date_from=TARGET_DATE_FROM,
        date_to=last_closed_date,
    )
    _validate_target_snapshots(current_snapshots, last_closed_date=last_closed_date)
    if _desired_is_applied(
        runtime=runtime,
        manifest=manifest,
        current_versions=current_versions,
        current_snapshots=current_snapshots,
    ):
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
    _validate_pre_change(
        runtime=runtime,
        pre=pre,
        current_versions=current_versions,
        current_snapshots=current_snapshots,
    )
    rebuilt = _build_desired_state(
        runtime=runtime,
        last_closed_date=last_closed_date,
        correction_created_at=str(desired_correction.get("created_at") or ""),
    )
    if rebuilt["already_applied"]:
        raise ProxyV4TransitRepairError("target changed to an unreviewed applied state")
    if _digest(rebuilt["correction"]) != _digest(desired_correction):
        raise ProxyV4TransitRepairError("corrected V4 revision sources drifted after dry-run")
    if _digest(rebuilt["desired_snapshots"]) != _digest(desired_snapshots):
        raise ProxyV4TransitRepairError("target snapshot operands or desired V4 cells drifted after dry-run")

    evidence_dir.mkdir(parents=True, exist_ok=True)
    backup_path = evidence_dir / "backups" / (
        f"proxy-v4-transit-{applied_at.replace(':', '').replace('-', '')}-{manifest_sha256[-12:]}.sqlite3"
    )
    backup = runtime.backup_database(
        backup_path,
        admission_owner="proxy_v4_transit_repair",
    )
    backup_descriptor = os.open(backup_path, os.O_RDONLY)
    try:
        os.fsync(backup_descriptor)
    finally:
        os.close(backup_descriptor)
    if backup_path.stat().st_size != int(backup["size_bytes"]):
        raise ProxyV4TransitRepairError("coherent backup size changed after verification")
    backup_sha256 = "sha256:" + str(backup["sha256"])

    current_by_key = {
        (str(item["bundle_version"]), str(item["as_of_date"])): str(item["plan_json"])
        for item in current_snapshots
    }
    with sqlite3.connect(runtime.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM sheet_vitrina_v1_proxy_v4_parameter_versions WHERE version_id=? OR revision=?",
            (desired_correction["version_id"], desired_correction["revision"]),
        ).fetchone() is not None:
            raise ProxyV4TransitRepairError("correction version/revision stopped being empty")
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_proxy_v4_parameter_versions(
                   version_id,block_key,revision,effective_date,source_window_from,
                   source_window_to,source_window_fingerprint,parameters_json,
                   fingerprint,version_kind,created_by,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(
                desired_correction[key]
                for key in (
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
                )
            ),
        )
        for item in desired_snapshots:
            key = (str(item["bundle_version"]), str(item["as_of_date"]))
            if str(item["before_plan_sha256"]) == str(item["after_plan_sha256"]):
                continue
            cursor = conn.execute(
                """UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?
                   WHERE bundle_version=? AND as_of_date=? AND plan_json=?""",
                (str(item["after_plan_json"]), key[0], key[1], current_by_key[key]),
            )
            if cursor.rowcount != 1:
                raise ProxyV4TransitRepairError(
                    f"target snapshot compare-and-swap failed: {key[1]}"
                )
        conn.commit()

    after_versions = _load_version_rows(runtime.db_path)
    after_snapshots = _load_target_snapshots(
        runtime.db_path,
        date_from=TARGET_DATE_FROM,
        date_to=last_closed_date,
    )
    if not _desired_is_applied(
        runtime=runtime,
        manifest=manifest,
        current_versions=after_versions,
        current_snapshots=after_snapshots,
    ):
        raise ProxyV4TransitRepairError("post-apply target/non-target reconciliation failed")

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
        "backup_size_bytes": int(backup["size_bytes"]),
        "backup_integrity_check": str(backup["integrity_check"]),
        "inserted_parameter_revision_count": 1,
        "updated_ready_snapshot_count": int(
            (manifest.get("expected_effect") or {}).get("updated_ready_snapshot_count") or 0
        ),
        "affected_snapshot_dates": [str(item["as_of_date"]) for item in desired_snapshots],
        "post_version_rows_digest": _digest(after_versions),
        "post_snapshot_target_digest": _digest(
            [
                [item["bundle_version"], item["as_of_date"], proxy_v4_target_digest(str(item["plan_json"]))]
                for item in after_snapshots
            ]
        ),
        "v3_parameter_digest": str(pre["v3_parameter_digest"]),
        "protected_operational_digest": str(pre["protected_operational_digest"]),
        "finance_raw_store_identity": pre["finance_raw_store_identity"],
        "non_target_ready_snapshot_digest": str(pre["non_target_ready_snapshot_digest"]),
        "non_target_preserved": True,
        "idempotent_noop": False,
    }
    reconciliation_path = evidence_dir / (
        f"proxy-v4-transit-reconciliation-{applied_at.replace(':', '').replace('-', '')}.json"
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


def _build_desired_state(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    last_closed_date: str,
    correction_created_at: str,
) -> dict[str, Any]:
    version_rows = _load_version_rows(runtime.db_path)
    parameters = _load_parameter_objects(runtime.db_path)
    correction, source_evidence, already_present = _plan_correction(
        runtime=runtime,
        parameters=parameters,
        created_at=correction_created_at,
    )
    correction_row = _planned_version_row(correction)
    legacy_version_rows = [
        item for item in version_rows if str(item["version_id"]) != correction.version_id
    ]
    snapshots = _load_target_snapshots(
        runtime.db_path,
        date_from=TARGET_DATE_FROM,
        date_to=last_closed_date,
    )
    _validate_target_snapshots(snapshots, last_closed_date=last_closed_date)
    resolver = _resolver([*parameters, *([] if already_present else [correction])])
    desired_snapshots = [
        _project_snapshot(
            item,
            resolver=resolver,
            last_closed_date=last_closed_date,
            materialized_at=correction.created_at,
        )
        for item in snapshots
    ]
    already_applied = already_present and all(
        str(item["before_plan_sha256"]) == str(item["after_plan_sha256"])
        for item in desired_snapshots
    )
    return {
        "correction": correction_row,
        "source_evidence": source_evidence,
        "current_snapshots": snapshots,
        "desired_snapshots": desired_snapshots,
        "legacy_version_rows": legacy_version_rows,
        "already_applied": already_applied,
    }


def _plan_correction(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    parameters: list[ProxyV4Parameters],
    created_at: str,
) -> tuple[ProxyV4Parameters, dict[str, Any], bool]:
    legacy = [
        item
        for item in parameters
        if item.effective_date == CORRECTION_EFFECTIVE_DATE
        and item.formula_version == PROXY_V4_LEGACY_FORMULA_VERSION
    ]
    if not legacy:
        raise ProxyV4TransitRepairError(
            "legacy Proxy V4 revision effective 2026-08-16 is missing"
        )
    source = max(legacy, key=lambda item: item.revision)
    existing = [
        item
        for item in parameters
        if item.effective_date == CORRECTION_EFFECTIVE_DATE
        and item.formula_version == PROXY_V4_FORMULA_VERSION
        and item.version_kind == CORRECTION_VERSION_KIND
    ]
    window = build_latest_confirmed_week_window(
        runtime=runtime,
        today=date.fromisoformat(CORRECTION_EFFECTIVE_DATE),
    )
    if window.get("status") != "ready":
        raise ProxyV4TransitRepairError(
            "corrected 2026-08-16 source window is not READY COMPLETE"
        )
    source_ranges = tuple(
        (str(item[0]), str(item[1])) for item in window.get("source_week_ranges") or []
    )
    if source_ranges != source.source_week_ranges:
        raise ProxyV4TransitRepairError("2026-08-16 source week changed from the legacy revision")
    automatic = {
        field: Decimal(str(window["automatic_rates"][field]))
        for field in AUTOMATIC_RATE_FIELDS
    }
    for field in AUTOMATIC_RATE_FIELDS:
        if field == "other_expense_rate":
            continue
        if getattr(source, field) != automatic[field]:
            raise ProxyV4TransitRepairError(
                f"non-transit automatic rate drifted for 2026-08-16: {field}"
            )
    aligned_finance = dict(window.get("aligned_finance") or {})
    excluded = dict(aligned_finance.get("excluded_amounts") or {})
    net_revenue = Decimal(str(aligned_finance.get("net_revenue") or "0"))
    if net_revenue <= 0:
        raise ProxyV4TransitRepairError("corrected Finance net revenue is not positive")
    transit = Decimal(str(excluded.get("transit_logistics") or "0"))
    capitalized_transit = Decimal(
        str(excluded.get("capitalized_transit_logistics") or "0")
    )
    transit_residual_rate = (transit - capitalized_transit) / net_revenue
    corrected_other = automatic["other_expense_rate"]
    if source.other_expense_rate != corrected_other + transit_residual_rate:
        raise ProxyV4TransitRepairError(
            "legacy other-expense rate does not reconcile to corrected non-transit rate plus transit residual"
        )
    expected = _parameters_from_values(
        effective_date=CORRECTION_EFFECTIVE_DATE,
        tax_rate=source.tax_rate,
        automatic_rates=automatic,
        source_window_from=str(window["source_window_from"]),
        source_window_to=str(window["source_window_to"]),
        source_window_fingerprint=str(window["source_window_fingerprint"]),
        source_week_ranges=source_ranges,
        source_slot_from=str(window["source_slot_from"]),
        source_slot_to=str(window["source_slot_to"]),
        buyout_order_count_weight=Decimal(
            str(window["aligned_buyout"]["order_count_weight"])
        ),
        finance_net_revenue_weight=net_revenue,
        version_id=(
            max(existing, key=lambda item: item.revision).version_id
            if existing
            else f"proxy_v4_v{max(item.revision for item in parameters) + 1}_20260816"
        ),
        revision=(
            max(existing, key=lambda item: item.revision).revision
            if existing
            else max(item.revision for item in parameters) + 1
        ),
        version_kind=CORRECTION_VERSION_KIND,
        created_at=(
            max(existing, key=lambda item: item.revision).created_at
            if existing
            else created_at
        ),
        created_by="production_mutation",
        formula_version=PROXY_V4_FORMULA_VERSION,
    )
    if existing:
        current = max(existing, key=lambda item: item.revision)
        if _parameter_semantic(current) != _parameter_semantic(expected):
            raise ProxyV4TransitRepairError("existing transit-exclusion revision is not canonical")
        expected = current
    source_evidence = {
        "legacy_version_id": source.version_id,
        "legacy_revision": source.revision,
        "legacy_formula_version": source.formula_version,
        "legacy_source_window_fingerprint": source.source_window_fingerprint,
        "selected_week_range": list(source_ranges[0]),
        "finance_net_revenue": str(net_revenue),
        "transit_logistics": str(transit),
        "capitalized_transit_logistics": str(capitalized_transit),
        "transit_residual_rate": str(transit_residual_rate),
        "legacy_other_expense_rate": str(source.other_expense_rate),
        "corrected_other_expense_rate": str(corrected_other),
        "corrected_source_window_fingerprint": expected.source_window_fingerprint,
        "non_transit_rates_preserved": True,
    }
    return expected, source_evidence, bool(existing)


def _project_snapshot(
    item: Mapping[str, Any],
    *,
    resolver: Callable[[str], ProxyV4Parameters | None],
    last_closed_date: str,
    materialized_at: str,
) -> dict[str, Any]:
    before = str(item["plan_json"])
    projection = project_proxy_v4_ready_snapshot(
        before,
        parameters_for_date=resolver,
        materialized_at=materialized_at,
    )
    raw = json.loads(before)
    dates = [str(value) for value in raw.get("date_columns") or []]
    scoped = [day for day in dates if TARGET_DATE_FROM <= day <= last_closed_date]
    if not scoped:
        raise ProxyV4TransitRepairError(
            f"target snapshot has no closed V4 date columns: {item['as_of_date']}"
        )
    repaired = reconcile_proxy_v4_target_window(
        before,
        reference_plan_json=str(projection["after_plan_json"]),
        date_from=scoped[0],
        date_to=scoped[-1],
        reconciled_at=materialized_at,
    )
    if repaired["non_target_before"] != repaired["non_target_after"]:
        raise ProxyV4TransitRepairError(
            f"projection changed non-V4 fields for {item['as_of_date']}"
        )
    after = str(repaired["after_plan_json"])
    return {
        "bundle_version": str(item["bundle_version"]),
        "as_of_date": str(item["as_of_date"]),
        "scoped_dates": list(repaired["dates"]),
        "before_plan_sha256": _plan_digest(before),
        "after_plan_json": after,
        "after_plan_sha256": _plan_digest(after),
        "after_target_digest": proxy_v4_target_digest(after),
        "changed_cells": int(repaired["changed_cells"]),
        "inserted_rows": int(repaired["inserted_rows"]),
        "eligibility_by_date": {
            day: dict(projection["eligibility_by_date"][day]) for day in scoped
        },
    }


def _load_parameter_objects(db_path: Path) -> list[ProxyV4Parameters]:
    with _query_only_connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM sheet_vitrina_v1_proxy_v4_parameter_versions
               WHERE block_key=? ORDER BY revision""",
            (PROXY_V4_BLOCK_KEY,),
        ).fetchall()
    return [_parameters_from_row(row) for row in rows]


def _planned_version_row(item: ProxyV4Parameters) -> dict[str, Any]:
    fingerprint = _parameter_fingerprint(item)
    public = item.public()
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
        "version_kind": item.version_kind,
        "created_by": item.created_by,
        "created_at": item.created_at,
    }


def _parameter_semantic(item: ProxyV4Parameters) -> dict[str, Any]:
    public = item.public()
    return {
        key: public[key]
        for key in (
            "effective_date",
            "formula_version",
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
            "source_week_ranges",
            "source_slot_from",
            "source_slot_to",
            "buyout_order_count_weight",
            "finance_net_revenue_weight",
        )
    }


def _resolver(
    parameters: list[ProxyV4Parameters],
) -> Callable[[str], ProxyV4Parameters | None]:
    ordered = sorted(parameters, key=lambda item: (item.effective_date, item.revision))

    def resolve(day: str) -> ProxyV4Parameters | None:
        candidates = [item for item in ordered if item.effective_date <= str(day)[:10]]
        return candidates[-1] if candidates else None

    return resolve


def _validate_window(*, last_closed_date: str) -> None:
    start = date.fromisoformat(TARGET_DATE_FROM)
    end = date.fromisoformat(last_closed_date)
    if end < date.fromisoformat(CORRECTION_EFFECTIVE_DATE):
        raise ProxyV4TransitRepairError("last closed date precedes the correction boundary")
    if (end - start).days + 1 > MAX_TARGET_DAYS:
        raise ProxyV4TransitRepairError("Proxy V4 transit repair exceeds its 31-day bounded lifetime")


def _validate_target_snapshots(
    snapshots: list[Mapping[str, Any]],
    *,
    last_closed_date: str,
) -> None:
    expected = _date_range(TARGET_DATE_FROM, last_closed_date)
    actual = [str(item["as_of_date"]) for item in snapshots]
    if actual != expected:
        raise ProxyV4TransitRepairError(
            f"closed ready-snapshot coverage is not exact: expected={expected}, actual={actual}"
        )
    bundles = {str(item["bundle_version"]) for item in snapshots}
    if len(bundles) != 1:
        raise ProxyV4TransitRepairError("target snapshots do not share one exact bundle version")


def _validate_pre_change(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    pre: Mapping[str, Any],
    current_versions: list[Mapping[str, Any]],
    current_snapshots: list[Mapping[str, Any]],
) -> None:
    if _digest(current_versions) != str(pre.get("legacy_v4_version_rows_digest") or ""):
        raise ProxyV4TransitRepairError("V4 parameter versions changed after dry-run")
    if _target_snapshot_rows_digest(current_snapshots) != str(
        pre.get("target_snapshot_rows_digest") or ""
    ):
        raise ProxyV4TransitRepairError("target ready snapshots changed after dry-run")
    target_keys = {
        (str(item["bundle_version"]), str(item["as_of_date"])) for item in current_snapshots
    }
    if _digest(
        [
            [
                item["bundle_version"],
                item["as_of_date"],
                proxy_v4_non_target_digest(str(item["plan_json"])),
            ]
            for item in current_snapshots
        ]
    ) != str(pre.get("target_snapshot_non_v4_digest") or ""):
        raise ProxyV4TransitRepairError("target snapshot non-V4 fields changed after dry-run")
    if _non_target_ready_snapshot_digest(runtime.db_path, targets=target_keys) != str(
        pre.get("non_target_ready_snapshot_digest") or ""
    ):
        raise ProxyV4TransitRepairError("non-target ready snapshots changed after dry-run")
    if _v3_parameter_digest(runtime.db_path) != str(pre.get("v3_parameter_digest") or ""):
        raise ProxyV4TransitRepairError("V3 parameters changed after dry-run")
    if _protected_operational_digest(runtime.db_path) != str(
        pre.get("protected_operational_digest") or ""
    ):
        raise ProxyV4TransitRepairError("Finance/canonical WAC/COGS protected state changed after dry-run")
    if _finance_raw_store_identity(runtime) != dict(pre.get("finance_raw_store_identity") or {}):
        raise ProxyV4TransitRepairError("canonical Finance raw store identity changed after dry-run")


def _desired_is_applied(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    manifest: Mapping[str, Any],
    current_versions: list[Mapping[str, Any]],
    current_snapshots: list[Mapping[str, Any]],
) -> bool:
    desired = dict(manifest.get("desired") or {})
    correction = dict(desired.get("correction_version") or {})
    matches = [
        item for item in current_versions if str(item["version_id"]) == str(correction.get("version_id"))
    ]
    if len(matches) != 1 or _digest(matches[0]) != _digest(correction):
        return False
    pre = dict(manifest.get("pre_change") or {})
    legacy = [item for item in current_versions if item not in matches]
    if _digest(legacy) != str(pre.get("legacy_v4_version_rows_digest") or ""):
        return False
    desired_by_key = {
        (str(item["bundle_version"]), str(item["as_of_date"])): str(item["after_plan_sha256"])
        for item in desired.get("ready_snapshots") or []
    }
    if len(current_snapshots) != len(desired_by_key) or any(
        _plan_digest(str(item["plan_json"]))
        != desired_by_key.get((str(item["bundle_version"]), str(item["as_of_date"])))
        for item in current_snapshots
    ):
        return False
    try:
        _validate_pre_change_after_apply(
            runtime=runtime,
            pre=pre,
            current_snapshots=current_snapshots,
        )
    except ProxyV4TransitRepairError:
        return False
    return True


def _validate_pre_change_after_apply(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    pre: Mapping[str, Any],
    current_snapshots: list[Mapping[str, Any]],
) -> None:
    target_keys = {
        (str(item["bundle_version"]), str(item["as_of_date"])) for item in current_snapshots
    }
    if _digest(
        [
            [
                item["bundle_version"],
                item["as_of_date"],
                proxy_v4_non_target_digest(str(item["plan_json"])),
            ]
            for item in current_snapshots
        ]
    ) != str(pre.get("target_snapshot_non_v4_digest") or ""):
        raise ProxyV4TransitRepairError("post-apply target non-V4 invariant failed")
    if _non_target_ready_snapshot_digest(runtime.db_path, targets=target_keys) != str(
        pre.get("non_target_ready_snapshot_digest") or ""
    ):
        raise ProxyV4TransitRepairError("post-apply non-target snapshot invariant failed")
    if _v3_parameter_digest(runtime.db_path) != str(pre.get("v3_parameter_digest") or ""):
        raise ProxyV4TransitRepairError("post-apply V3 invariant failed")
    if _protected_operational_digest(runtime.db_path) != str(
        pre.get("protected_operational_digest") or ""
    ):
        raise ProxyV4TransitRepairError("post-apply Finance/WAC/COGS invariant failed")
    if _finance_raw_store_identity(runtime) != dict(pre.get("finance_raw_store_identity") or {}):
        raise ProxyV4TransitRepairError("post-apply Finance raw-store invariant failed")


def _protected_operational_digest(db_path: Path) -> str:
    digest = hashlib.sha256()
    with _query_only_connection(db_path) as conn:
        existing = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        missing = sorted(set(REQUIRED_PROTECTED_TABLES) - existing)
        if missing:
            raise ProxyV4TransitRepairError(
                f"required Finance/WAC/COGS protected tables are missing: {missing}"
            )
        for table in PROTECTED_OPERATIONAL_TABLES:
            if table not in existing:
                continue
            columns = [
                (str(row[1]), int(row[5]))
                for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            ]
            order = [name for name, pk in sorted(columns, key=lambda item: item[1]) if pk]
            query = f'SELECT * FROM "{table}"'
            if order:
                query += " ORDER BY " + ",".join(f'"{name}"' for name in order)
            else:
                query += " ORDER BY rowid"
            _hash_item(digest, {"table": table, "columns": [name for name, _ in columns]})
            for row in conn.execute(query):
                _hash_item(digest, [_digest_value(value) for value in row])
    return "sha256:" + digest.hexdigest()


def _finance_raw_store_identity(runtime: RegistryUploadDbBackedRuntime) -> dict[str, Any]:
    manifest = runtime.store_registry.load(require_files=True)
    raw_path = runtime.store_registry.resolve("finance_raw", manifest=manifest)
    shared_with_operational = raw_path.resolve() == runtime.db_path.resolve()
    if shared_with_operational:
        return {
            "generation_id": manifest.raw.generation_id,
            "generation_epoch": manifest.raw.generation_epoch,
            "schema_revision": manifest.raw.schema_revision,
            "manifest_sha256": manifest.manifest_sha256,
            "path": str(raw_path),
            "shared_with_operational": True,
        }
    stat = raw_path.stat()
    with _query_only_connection(raw_path) as conn:
        pragmas = {
            "schema_version": int(conn.execute("PRAGMA schema_version").fetchone()[0]),
            "user_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
            "page_count": int(conn.execute("PRAGMA page_count").fetchone()[0]),
            "freelist_count": int(conn.execute("PRAGMA freelist_count").fetchone()[0]),
        }
    return {
        "generation_id": manifest.raw.generation_id,
        "generation_epoch": manifest.raw.generation_epoch,
        "schema_revision": manifest.raw.schema_revision,
        "manifest_sha256": manifest.manifest_sha256,
        "path": str(raw_path),
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "size_bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "shared_with_operational": False,
        **pragmas,
    }


def _hash_item(digest: Any, value: Any) -> None:
    digest.update(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )
    digest.update(b"\n")


def _digest_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {
            "bytes": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
    return value


def _plan_digest(plan_json: str) -> str:
    return _digest(json.loads(str(plan_json)))


def _date_range(date_from: str, date_to: str) -> list[str]:
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    return [
        (start + timedelta(days=offset)).isoformat()
        for offset in range((end - start).days + 1)
    ]


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
        result = run_transit_repair(
            runtime_dir=Path(args.runtime_dir),
            evidence_dir=Path(args.evidence_dir),
            apply=bool(args.apply),
            manifest_path=Path(args.manifest) if args.manifest else None,
            expected_manifest_sha256=args.expected_manifest_sha256,
            expected_deployed_sha=args.expected_deployed_sha,
            deployed_sha_file=(
                Path(args.deployed_sha_file) if args.deployed_sha_file else None
            ),
            approval_reference=args.approval_reference,
        )
    except (ProxyV4TransitRepairError, ValueError) as exc:
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
