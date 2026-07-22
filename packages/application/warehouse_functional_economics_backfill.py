"""Guarded targeted ready-snapshot publication of functional WB economics."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from packages.business_time import business_date_from_timestamp, current_business_date_iso
from packages.application.calculation_parameters import (
    CalculationParametersBlock,
    calculate_proxy_3,
)
from packages.application.canonical_wb_cost_resolver import CANONICAL_COST_POLICY_DATE
from packages.application.own_product_capital import OwnProductCapitalBlock
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_our_wb_costs import (
    OUR_WB_PROXY_MARGIN_3_PCT_LABEL,
    OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
    OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_LABEL,
    OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY,
    OUR_WB_PROXY_PROFIT_3_RUB_LABEL,
    OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_UNIT_COST_RUB_LABEL,
    OUR_WB_UNIT_COST_RUB_METRIC_KEY,
    TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY,
)
from packages.application.sheet_vitrina_v1_archived_metrics import ARCHIVED_PUBLIC_METRIC_KEYS
from packages.application.sheet_vitrina_v1_own_product_capital import (
    OWN_AVG_COST_RUB_TOTAL_METRIC_KEY,
    OWN_PRODUCT_CAPITAL_STAGES,
    OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS,
    OWN_PRODUCT_CAPITAL_TOTAL_METRIC_KEYS,
    OWN_TOTAL_CAPITAL_RUB_METRIC_KEY,
    OWN_TOTAL_CAPITAL_RUB_TOTAL_METRIC_KEY,
    OWN_TOTAL_QTY_METRIC_KEY,
    OWN_TOTAL_QTY_TOTAL_METRIC_KEY,
    build_own_product_capital_metric_items,
    own_stage_metric_key,
    own_stage_total_metric_key,
)
from packages.application.sheet_vitrina_v1_proxy_margin_3_historical_backfill import (
    _data_sheet,
    _date_columns,
    _update_data_dimensions,
)
from packages.application.warehouse_functional import (
    FUNCTIONAL_CUTOVER_ID,
    _warehouse_balance_status_presentation,
)


CONTRACT_NAME = "sheet_vitrina_v1_functional_economics_backfill"
TARGET_KEYS = {
    OUR_WB_UNIT_COST_RUB_METRIC_KEY,
    TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY,
    OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
    OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY,
}
WAREHOUSE_TARGET_KEYS = set(OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS) | set(
    OWN_PRODUCT_CAPITAL_TOTAL_METRIC_KEYS
)
TARGET_KEYS.update(WAREHOUSE_TARGET_KEYS)
ARCHIVED_READY_METRIC_KEYS = ARCHIVED_PUBLIC_METRIC_KEYS
MUTATED_READY_METRIC_KEYS = frozenset(TARGET_KEYS | set(ARCHIVED_READY_METRIC_KEYS))
ZERO = Decimal("0")


class FunctionalEconomicsBackfillError(RuntimeError):
    pass


def build_functional_economics_backfill_plan(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    business_date: str | None = None,
    _enforce_business_date_boundary: bool = True,
) -> dict[str, Any]:
    operation_business_date = str(business_date or current_business_date_iso())[:10]
    try:
        date.fromisoformat(operation_business_date)
    except ValueError as exc:
        raise FunctionalEconomicsBackfillError("canonical operation business date is invalid") from exc
    with _connect(runtime.db_path) as conn:
        cutover = conn.execute(
            """SELECT cutover_at,plan_fingerprint FROM sheet_vitrina_v1_warehouse_functional_cutovers
               WHERE cutover_id=? AND status='posted'""",
            (FUNCTIONAL_CUTOVER_ID,),
        ).fetchone()
        if cutover is None:
            raise FunctionalEconomicsBackfillError("functional cutover is not posted")
        cutover_business_date = business_date_from_timestamp(str(cutover["cutover_at"]))
        snapshots = [dict(row) for row in conn.execute(
            """SELECT bundle_version,as_of_date,plan_json,refreshed_at
               FROM sheet_vitrina_v1_ready_snapshots ORDER BY bundle_version,as_of_date"""
        ).fetchall()]
    dates = sorted({day for row in snapshots for day in _snapshot_dates(row["plan_json"])})
    warehouse_dates = [
        day for day in dates if day >= CANONICAL_COST_POLICY_DATE.isoformat()
    ]
    warehouse_input_manifest_digest = _warehouse_input_manifest_digest(
        runtime,
        dates=dates,
    )
    costs = {day: runtime.load_our_wb_cost_daily_state(as_of_date=day) for day in dates}
    capital = OwnProductCapitalBlock(runtime=runtime)
    warehouse_context = _exact_functional_snapshot_context(runtime, warehouse_dates)
    warehouse_covered_nm_ids = {
        day: set(item["covered_nm_ids"])
        for day, item in warehouse_context.items()
    }
    warehouse_version_ids = {
        day: str(item["version_id"])
        for day, item in warehouse_context.items()
    }
    warehouse_exact_dates = set(warehouse_covered_nm_ids)
    warehouse_metrics = {
        day: capital.load_daily_metric_lookup(
            day,
            requested_nm_ids=warehouse_covered_nm_ids[day],
            revalidate_current_sources=True,
        )
        if day in warehouse_exact_dates
        else {}
        for day in dates
    }
    parameters = CalculationParametersBlock(runtime=runtime)
    parameter_by_date = {
        day: parameters.parameters_for_date(
            max(day, CANONICAL_COST_POLICY_DATE.isoformat())
        )
        for day in dates
    }
    if _warehouse_input_manifest_digest(runtime, dates=dates) != warehouse_input_manifest_digest:
        raise FunctionalEconomicsBackfillError(
            "functional warehouse/cost/settings inputs drifted during dry-run"
        )
    source_fingerprint = "sha256:" + _hash(
        {
            "cutover_fingerprint": str(cutover["plan_fingerprint"]),
            "costs": costs,
            "warehouse_metrics": warehouse_metrics,
            "warehouse_exact_dates": sorted(warehouse_exact_dates),
            "warehouse_covered_nm_ids": {
                day: sorted(nm_ids)
                for day, nm_ids in sorted(warehouse_covered_nm_ids.items())
            },
            "warehouse_version_ids": warehouse_version_ids,
            "parameters": {day: item.public() for day, item in parameter_by_date.items()},
        }
    )
    updates: list[dict[str, Any]] = []
    changed_cells = 0
    inserted_rows = 0
    archived_rows_removed = 0
    presentation_changes = 0
    coverage_changes = 0
    non_target_before: list[list[str]] = []
    non_target_after: list[list[str]] = []
    non_target_mismatches: list[dict[str, str]] = []
    for snapshot in snapshots:
        try:
            transformed = _transform_snapshot(
                snapshot,
                costs=costs,
                warehouse_metrics=warehouse_metrics,
                warehouse_exact_dates=warehouse_exact_dates,
                warehouse_covered_nm_ids=warehouse_covered_nm_ids,
                warehouse_version_ids=warehouse_version_ids,
                parameters=parameter_by_date,
                source_fingerprint=source_fingerprint,
                cutover_business_date=cutover_business_date,
                operation_business_date=operation_business_date,
            )
        except Exception as exc:
            raise FunctionalEconomicsBackfillError(
                "functional economics ready snapshot failed: "
                f"bundle_version={snapshot['bundle_version']} "
                f"as_of_date={snapshot['as_of_date']}: {exc}"
            ) from exc
        non_target_before.append([snapshot["bundle_version"], snapshot["as_of_date"], transformed["non_target_before"]])
        non_target_after.append([snapshot["bundle_version"], snapshot["as_of_date"], transformed["non_target_after"]])
        if transformed["non_target_before"] != transformed["non_target_after"]:
            non_target_mismatches.append(
                {
                    "bundle_version": str(snapshot["bundle_version"]),
                    "as_of_date": str(snapshot["as_of_date"]),
                    "before_digest": str(transformed["non_target_before"]),
                    "after_digest": str(transformed["non_target_after"]),
                }
            )
        changed_cells += int(transformed["changed_cells"])
        inserted_rows += int(transformed["inserted_rows"])
        archived_rows_removed += int(transformed["archived_rows_removed"])
        presentation_changes += int(transformed["presentation_changes"])
        coverage_changes += int(transformed["coverage_changes"])
        # Marker/timestamp churn is not a business change.  Do not force a
        # coherent multi-gigabyte backup when every target cell already equals
        # the canonical value and no row is inserted or archived.
        material_change = any(
            int(transformed[key]) > 0
            for key in (
                "changed_cells",
                "inserted_rows",
                "archived_rows_removed",
                "presentation_changes",
                "coverage_changes",
            )
        )
        if material_change and transformed["after_plan_json"] != snapshot["plan_json"]:
            updates.append(
                {
                    "bundle_version": snapshot["bundle_version"],
                    "as_of_date": snapshot["as_of_date"],
                    "before_plan_sha256": "sha256:" + _sha(snapshot["plan_json"]),
                    "after_plan_json": transformed["after_plan_json"],
                    "changed_cells": transformed["changed_cells"],
                    "inserted_rows": transformed["inserted_rows"],
                    "archived_rows_removed": transformed["archived_rows_removed"],
                    "presentation_changes": transformed["presentation_changes"],
                    "coverage_changes": transformed["coverage_changes"],
                    "dates": transformed["dates"],
                }
            )
    before_digest = "sha256:" + _hash(non_target_before)
    after_digest = "sha256:" + _hash(non_target_after)
    if before_digest != after_digest or non_target_mismatches:
        raise FunctionalEconomicsBackfillError(
            "non-target ready-snapshot content changed: "
            + json.dumps(
                non_target_mismatches[:20],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    plan = {
        "contract_name": CONTRACT_NAME,
        "contract_version": "v1",
        "status": "dry_run_ready",
        "cutover_id": FUNCTIONAL_CUTOVER_ID,
        "cutover_at": str(cutover["cutover_at"]),
        "business_date": operation_business_date,
        "source_fingerprint": source_fingerprint,
        "snapshot_count": len(snapshots),
        "date_from": dates[0] if dates else None,
        "date_to": dates[-1] if dates else None,
        "source_dates": dates,
        "changed_snapshot_count": len(updates),
        "changed_cell_count": changed_cells,
        "inserted_row_count": inserted_rows,
        "archived_row_count": archived_rows_removed,
        "presentation_change_count": presentation_changes,
        "coverage_change_count": coverage_changes,
        "archived_metric_keys": sorted(ARCHIVED_READY_METRIC_KEYS),
        "ready_snapshot_manifest_digest": _snapshot_manifest_digest(snapshots),
        "warehouse_input_manifest_digest": warehouse_input_manifest_digest,
        "non_target_digest": before_digest,
        "updates": updates,
    }
    plan["plan_fingerprint"] = _plan_fingerprint(plan)
    if _enforce_business_date_boundary and current_business_date_iso() != operation_business_date:
        raise FunctionalEconomicsBackfillError(
            "functional economics dry-run crossed the canonical business-date boundary"
        )
    return plan


def apply_functional_economics_backfill_plan(
    runtime: RegistryUploadDbBackedRuntime,
    plan: Mapping[str, Any],
    *,
    confirm_fingerprint: str,
    backup_dir: Any,
    verified_backup: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = json.loads(json.dumps(dict(plan), ensure_ascii=False))
    fingerprint = str(normalized.get("plan_fingerprint") or "")
    if fingerprint != str(confirm_fingerprint or "") or fingerprint != _plan_fingerprint(
        {key: value for key, value in normalized.items() if key != "plan_fingerprint"}
    ):
        raise FunctionalEconomicsBackfillError("exact functional economics plan fingerprint is required")
    operation_business_date = str(normalized.get("business_date") or "")[:10]
    if not operation_business_date or current_business_date_iso() != operation_business_date:
        raise FunctionalEconomicsBackfillError(
            "functional economics apply crossed the canonical business-date boundary"
        )
    fresh = build_functional_economics_backfill_plan(
        runtime,
        business_date=operation_business_date,
    )
    if str(fresh["plan_fingerprint"]) != fingerprint:
        raise FunctionalEconomicsBackfillError(
            "functional cost/settings or ready snapshots drifted after dry-run"
        )
    backup = (
        _validate_verified_backup(
            verified_backup,
            expected_business_date=operation_business_date,
        )
        if verified_backup is not None
        else None
    )
    if not normalized.get("updates"):
        return {**fresh, "status": "applied", "idempotent": True, "database_written": False}
    if backup is None:
        backup_root = Path(backup_dir)
        if not backup_root.is_absolute():
            raise FunctionalEconomicsBackfillError("absolute backup_dir is required")
        backup_root.mkdir(parents=True, exist_ok=True)
        destination = backup_root / f"functional-economics-{fingerprint.removeprefix('sha256:')[:16]}.sqlite3"
        if destination.exists():
            destination = backup_root / f"functional-economics-{fingerprint.removeprefix('sha256:')[:24]}.sqlite3"
        backup = runtime.backup_database(destination)
        destination.chmod(0o600)
    if str(backup.get("integrity_check") or "") != "ok":
        raise FunctionalEconomicsBackfillError("functional economics backup integrity_check failed")
    if current_business_date_iso() != operation_business_date:
        raise FunctionalEconomicsBackfillError(
            "functional economics apply crossed the canonical business-date boundary during backup"
        )
    with _connect(runtime.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if current_business_date_iso() != operation_business_date:
                raise FunctionalEconomicsBackfillError(
                    "functional economics apply crossed the canonical business-date boundary before write"
                )
            locked_snapshots = [dict(row) for row in conn.execute(
                """SELECT bundle_version,as_of_date,plan_json,refreshed_at
                   FROM sheet_vitrina_v1_ready_snapshots ORDER BY bundle_version,as_of_date"""
            ).fetchall()]
            if _snapshot_manifest_digest(locked_snapshots) != str(
                normalized.get("ready_snapshot_manifest_digest") or ""
            ):
                raise FunctionalEconomicsBackfillError(
                    "ready snapshot manifest drifted before atomic backfill"
                )
            locked_input_digest = _warehouse_input_manifest_digest(
                runtime,
                dates=_plan_dates(normalized),
                connection=conn,
            )
            if locked_input_digest != str(
                normalized.get("warehouse_input_manifest_digest") or ""
            ):
                raise FunctionalEconomicsBackfillError(
                    "functional warehouse/cost/settings inputs drifted before atomic backfill"
                )
            for item in normalized["updates"]:
                before = conn.execute(
                    """SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots
                       WHERE bundle_version=? AND as_of_date=?""",
                    (item["bundle_version"], item["as_of_date"]),
                ).fetchone()
                if before is None or "sha256:" + _sha(str(before["plan_json"])) != item["before_plan_sha256"]:
                    raise FunctionalEconomicsBackfillError("ready snapshot drifted before atomic backfill")
                cursor = conn.execute(
                    """UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?
                       WHERE bundle_version=? AND as_of_date=? AND plan_json=?""",
                    (
                        item["after_plan_json"],
                        item["bundle_version"],
                        item["as_of_date"],
                        before["plan_json"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise FunctionalEconomicsBackfillError("ready snapshot optimistic update conflict")
            for item in normalized["updates"]:
                stored = conn.execute(
                    """SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots
                       WHERE bundle_version=? AND as_of_date=?""",
                    (item["bundle_version"], item["as_of_date"]),
                ).fetchone()
                if stored is None or str(stored["plan_json"]) != str(item["after_plan_json"]):
                    raise FunctionalEconomicsBackfillError(
                        "functional economics in-transaction readback failed"
                    )
            if current_business_date_iso() != operation_business_date:
                raise FunctionalEconomicsBackfillError(
                    "functional economics apply crossed the canonical business-date boundary before commit"
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    readback = build_functional_economics_backfill_plan(
        runtime,
        business_date=operation_business_date,
        _enforce_business_date_boundary=False,
    )
    if readback.get("updates"):
        raise FunctionalEconomicsBackfillError("functional economics backfill is not idempotent")
    return {
        **readback,
        "status": "applied",
        "idempotent": False,
        "database_written": True,
        "applied_snapshot_count": len(normalized["updates"]),
        "backup": backup,
        "applied_plan_fingerprint": fingerprint,
    }


def _validate_verified_backup(
    value: Mapping[str, Any],
    *,
    expected_business_date: str = "",
) -> dict[str, Any]:
    backup = json.loads(json.dumps(dict(value), ensure_ascii=False))
    if str(backup.get("integrity_check") or backup.get("source_integrity_check") or "") != "ok":
        raise FunctionalEconomicsBackfillError("verified economics backup integrity_check is required")
    raw_path = str(backup.get("path") or "").strip()
    archive_path = str(backup.get("archive_path") or "").strip()
    if not raw_path and not archive_path:
        raise FunctionalEconomicsBackfillError("verified economics backup path is required")
    backup_business_date = str(backup.get("business_date") or "")[:10]
    if (
        str(backup.get("backup_scope") or "") == "business_day"
        and backup_business_date != str(expected_business_date or backup_business_date)[:10]
    ):
        raise FunctionalEconomicsBackfillError(
            "verified economics backup belongs to another business date"
        )
    if raw_path:
        from apps.sqlite_backup_archive import build_plan

        path = Path(raw_path)
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise FunctionalEconomicsBackfillError("verified economics backup file is unavailable")
        if path.stat().st_mode & 0o777 != 0o600:
            raise FunctionalEconomicsBackfillError("verified economics backup must use mode 0600")
        actual = build_plan(source=path)
        declared_sha = str(backup.get("sha256") or backup.get("source_sha256") or "")
        declared_sha = declared_sha if declared_sha.startswith("sha256:") else f"sha256:{declared_sha}"
        declared_size = int(backup.get("size_bytes") or backup.get("source_size_bytes") or -1)
        if (
            declared_sha != str(actual.get("source_sha256") or "")
            or declared_size != int(actual.get("source_size_bytes") or -2)
            or str(actual.get("source_integrity_check") or "") != "ok"
        ):
            raise FunctionalEconomicsBackfillError(
                "verified economics backup bytes do not match their declared fingerprint"
            )
        backup.update(
            {
                "path": str(actual["source_path"]),
                "size_bytes": int(actual["source_size_bytes"]),
                "sha256": str(actual["source_sha256"]).removeprefix("sha256:"),
                "integrity_check": "ok",
            }
        )
    else:
        from apps.sqlite_backup_archive import verify_archive_manifest

        archive = Path(archive_path)
        if not archive.is_absolute() or archive.is_symlink() or not archive.is_file():
            raise FunctionalEconomicsBackfillError("verified economics backup archive is unavailable")
        if archive.stat().st_mode & 0o777 != 0o600:
            raise FunctionalEconomicsBackfillError("verified economics backup archive must use mode 0600")
        actual = verify_archive_manifest(
            archive.with_name(archive.name + ".manifest.json")
        )
        if str(actual.get("archive_path") or "") != str(archive.resolve()):
            raise FunctionalEconomicsBackfillError(
                "verified economics backup archive provenance does not match"
            )
        for field in ("archive_sha256", "decompressed_sha256", "source_sha256"):
            declared = str(backup.get(field) or "")
            if declared and declared != str(actual.get(field) or ""):
                raise FunctionalEconomicsBackfillError(
                    "verified economics backup archive fingerprint changed"
                )
        backup.update(actual)
    if str(backup.get("backup_scope") or "") == "business_day":
        provenance_date = str(expected_business_date or backup_business_date)[:10]
        expected_name = f"functional-economics-daily-{provenance_date.replace('-', '')}.sqlite3"
        source_identity = Path(str(backup.get("source_path") or backup.get("path") or ""))
        if source_identity.name != expected_name:
            raise FunctionalEconomicsBackfillError(
                "verified economics backup has invalid business-day provenance"
            )
    return backup


def _plan_dates(plan: Mapping[str, Any]) -> list[str]:
    if plan.get("source_dates") is not None:
        return sorted(
            {
                str(day or "")[:10]
                for day in plan.get("source_dates") or []
                if str(day or "")[:10]
            }
        )
    return sorted(
        {
            str(day or "")[:10]
            for update in plan.get("updates") or []
            for day in update.get("dates") or []
            if str(day or "")[:10]
        }
    )


def _warehouse_input_manifest_digest(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    dates: list[str],
    connection: sqlite3.Connection | None = None,
) -> str:
    """Fingerprint every persisted input used by warehouse/economics projection.

    The same manifest is captured before/after dry-run and rechecked while the
    ready-snapshot write lock is held.  This prevents an hourly functional sync
    or settings publication during the coherent backup from committing stale
    warehouse-history or Proxy cells.
    """

    selected_set = {str(day or "")[:10] for day in dates if str(day or "")[:10]}
    if any(day < CANONICAL_COST_POLICY_DATE.isoformat() for day in selected_set):
        selected_set.add(CANONICAL_COST_POLICY_DATE.isoformat())
    selected = sorted(selected_set)
    own_connection = connection is None
    conn = connection or _connect(runtime.db_path)
    try:
        table_names = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        manifest: dict[str, Any] = {"dates": selected}
        if "sheet_vitrina_v1_warehouse_functional_cutovers" in table_names:
            manifest["cutover"] = _query_manifest_rows(
                conn,
                """SELECT * FROM sheet_vitrina_v1_warehouse_functional_cutovers
                   WHERE cutover_id=? ORDER BY cutover_id""",
                (FUNCTIONAL_CUTOVER_ID,),
            )
        if selected and {
            "sheet_vitrina_v1_warehouse_functional_versions",
            "sheet_vitrina_v1_warehouse_wb_snapshots",
        }.issubset(table_names):
            placeholders = ",".join("?" for _ in selected)
            versions = _query_manifest_rows(
                conn,
                    f"""SELECT version.*,snapshot.snapshot_id,snapshot.snapshot_date,
                               snapshot.requested_nm_ids_json,snapshot.pagination_complete,
                               snapshot.raw_rows_digest,snapshot.items_json
                    FROM sheet_vitrina_v1_warehouse_functional_versions version
                    JOIN sheet_vitrina_v1_warehouse_wb_snapshots snapshot
                      ON snapshot.version_id=version.version_id
                    WHERE version.cutover_id=?
                      AND snapshot.snapshot_date IN ({placeholders})
                    ORDER BY snapshot.snapshot_date,version.created_at,version.version_id""",
                (FUNCTIONAL_CUTOVER_ID, *selected),
            )
            manifest["versions"] = versions
            version_ids = sorted({str(row["version_id"]) for row in versions})
            if version_ids and "sheet_vitrina_v1_warehouse_functional_balances" in table_names:
                version_placeholders = ",".join("?" for _ in version_ids)
                manifest["balances"] = _query_manifest_rows(
                    conn,
                    f"""SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances
                        WHERE version_id IN ({version_placeholders})
                        ORDER BY version_id,warehouse_key,nm_id""",
                    tuple(version_ids),
                )
                if "sheet_vitrina_v1_warehouse_supplier_cost_states" in table_names:
                    manifest["supplier_cost_states"] = _query_manifest_rows(
                        conn,
                        f"""SELECT * FROM sheet_vitrina_v1_warehouse_supplier_cost_states
                            WHERE version_id IN ({version_placeholders})
                            ORDER BY version_id,shipment_id""",
                        tuple(version_ids),
                    )
                if "sheet_vitrina_v1_warehouse_supplier_cost_state_replays" in table_names:
                    manifest["supplier_cost_state_replays"] = _query_manifest_rows(
                        conn,
                        f"""SELECT *
                            FROM sheet_vitrina_v1_warehouse_supplier_cost_state_replays
                            WHERE version_id IN ({version_placeholders})
                            ORDER BY version_id,sequence_no""",
                        tuple(version_ids),
                    )
                if "sheet_vitrina_v1_warehouse_supplier_cost_state_corrections" in table_names:
                    manifest["supplier_cost_state_corrections"] = _query_manifest_rows(
                        conn,
                        f"""SELECT *
                            FROM sheet_vitrina_v1_warehouse_supplier_cost_state_corrections
                            WHERE version_id IN ({version_placeholders})
                            ORDER BY version_id,shipment_id,replay_id""",
                        tuple(version_ids),
                    )
                if (
                    "sheet_vitrina_v1_warehouse_supplier_cost_state_replay_rollbacks"
                    in table_names
                ):
                    manifest["supplier_cost_state_replay_rollbacks"] = _query_manifest_rows(
                        conn,
                        f"""SELECT rollback.*
                            FROM sheet_vitrina_v1_warehouse_supplier_cost_state_replay_rollbacks rollback
                            JOIN sheet_vitrina_v1_warehouse_supplier_cost_state_replays replay
                              ON replay.replay_id=rollback.replay_id
                            WHERE replay.version_id IN ({version_placeholders})
                            ORDER BY replay.version_id,replay.sequence_no""",
                        tuple(version_ids),
                    )
        if "sheet_vitrina_v1_warehouse_functional_active" in table_names:
            manifest["active_version"] = _query_manifest_rows(
                conn,
                "SELECT * FROM sheet_vitrina_v1_warehouse_functional_active ORDER BY slot",
            )
        # Current green/yellow presentation is a function of mutable supplier
        # evidence as well as frozen balances.  Include every persisted input
        # read by load_supplier_line_cost_breakdown() so optimistic recheck also
        # closes the mutation-before-replay race.
        supplier_source_queries = {
            "supplier_shipments": (
                "SELECT * FROM sheet_vitrina_v1_supplier_shipments ORDER BY shipment_id"
            ),
            "supplier_shipment_lines": (
                "SELECT * FROM sheet_vitrina_v1_supplier_shipment_lines "
                "ORDER BY shipment_id,sort_order,line_id"
            ),
            "cny_ledger_operations": (
                "SELECT * FROM sheet_vitrina_v1_cny_ledger_operations "
                "ORDER BY sequence_key,operation_id"
            ),
            "supplier_financial_documents": (
                "SELECT * FROM sheet_vitrina_v1_supplier_financial_documents "
                "ORDER BY supplier_order_id,document_date,document_id"
            ),
            "supplier_financial_expense_lines": (
                "SELECT * FROM sheet_vitrina_v1_supplier_financial_expense_lines "
                "ORDER BY supplier_order_id,financial_document_id,sort_order,line_id"
            ),
            "cny_documents": (
                "SELECT * FROM sheet_vitrina_v1_cny_documents "
                "ORDER BY source_order_id,operation_date,operation_datetime,document_id"
            ),
        }
        for key, query in supplier_source_queries.items():
            table = "sheet_vitrina_v1_" + key
            if table in table_names:
                manifest[key] = _query_manifest_rows(conn, query)
        if selected and "sheet_vitrina_v1_warehouse_wb_daily_cost" in table_names:
            placeholders = ",".join("?" for _ in selected)
            manifest["daily_cost"] = _query_manifest_rows(
                conn,
                f"""SELECT * FROM sheet_vitrina_v1_warehouse_wb_daily_cost
                    WHERE cutover_id=? AND as_of_date IN ({placeholders})
                    ORDER BY as_of_date,nm_id""",
                (FUNCTIONAL_CUTOVER_ID, *selected),
            )
        if "sheet_vitrina_v1_calculation_parameter_versions" in table_names:
            manifest["parameters"] = _query_manifest_rows(
                conn,
                """SELECT * FROM sheet_vitrina_v1_calculation_parameter_versions
                   ORDER BY block_key,effective_date,revision,created_at,version_id""",
            )
        return "sha256:" + _hash(manifest)
    finally:
        if own_connection:
            conn.close()


def _query_manifest_rows(
    conn: sqlite3.Connection,
    query: str,
    parameters: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query, parameters).fetchall()]


def _transform_snapshot(
    snapshot: Mapping[str, Any],
    *,
    costs: Mapping[str, Mapping[int, Mapping[str, Any]]],
    warehouse_metrics: Mapping[str, Mapping[int, Mapping[str, Any]]],
    warehouse_exact_dates: set[str],
    warehouse_covered_nm_ids: Mapping[str, set[int]],
    warehouse_version_ids: Mapping[str, str],
    parameters: Mapping[str, Any],
    source_fingerprint: str,
    cutover_business_date: str,
    operation_business_date: str | None = None,
) -> dict[str, Any]:
    original = json.loads(str(snapshot["plan_json"]))
    plan = deepcopy(original)
    sheet = _data_sheet(plan)
    rows = sheet.get("rows")
    if not isinstance(rows, list):
        raise FunctionalEconomicsBackfillError("DATA_VITRINA rows are missing")
    dates = _date_columns(plan)
    relevant_indices = list(range(len(dates)))
    include_warehouse_rows = any(
        day >= CANONICAL_COST_POLICY_DATE.isoformat() for day in dates
    )
    active_target_keys = (
        set(TARGET_KEYS)
        if include_warehouse_rows
        else set(TARGET_KEYS) - WAREHOUSE_TARGET_KEYS
    )
    before_digest = _non_target_digest(original)
    _validate_data_projection_layout(sheet, dates=dates)
    archived_rows_removed = _remove_archived_metric_rows(rows)
    metadata_present = "metadata" in plan
    metadata = plan.get("metadata") if metadata_present else {}
    if not isinstance(metadata, dict):
        raise FunctionalEconomicsBackfillError("ready snapshot metadata must be an object")
    timestamps = metadata.get("row_last_updated_at_by_row_id")
    if isinstance(timestamps, dict):
        for row_id in list(timestamps):
            if "|" in row_id and row_id.split("|", 1)[1] in ARCHIVED_READY_METRIC_KEYS:
                timestamps.pop(row_id, None)
    if not relevant_indices:
        if plan == original:
            return {
                "after_plan_json": str(snapshot["plan_json"]),
                "changed_cells": 0,
                "inserted_rows": 0,
                "archived_rows_removed": 0,
                "presentation_changes": 0,
                "coverage_changes": 0,
                "dates": [],
                "non_target_before": before_digest,
                "non_target_after": before_digest,
            }
        if archived_rows_removed:
            _update_data_dimensions(sheet)
        after = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return {
            "after_plan_json": after,
            "changed_cells": 0,
            "inserted_rows": 0,
            "archived_rows_removed": archived_rows_removed,
            "presentation_changes": 0,
            "coverage_changes": 0,
            "dates": [],
            "non_target_before": before_digest,
            "non_target_after": _non_target_digest(plan),
        }
    by_id = _rows_by_id(rows)
    scopes = sorted({row_id.split("|", 1)[0] for row_id in by_id if row_id.startswith("SKU:")})
    if not scopes:
        raise FunctionalEconomicsBackfillError("ready snapshot has no SKU scopes")
    scope_nm_ids = {int(scope.split(":", 1)[1]) for scope in scopes}
    if not metadata_present:
        plan["metadata"] = metadata
    inserted = _ensure_target_rows(
        rows,
        by_id=by_id,
        scopes=scopes,
        date_count=len(dates),
        include_warehouse=include_warehouse_rows,
    )
    by_id = _rows_by_id(rows)
    changed = 0
    presentation_changes = 0
    sku_result: dict[tuple[str, int], dict[str, Decimal | None]] = {}
    warehouse_coverage: dict[str, dict[str, Any]] = {}
    for index in relevant_indices:
        day = dates[index]
        params = parameters[day]
        day_warehouse = warehouse_metrics.get(day, {})
        warehouse_applicable = day >= CANONICAL_COST_POLICY_DATE.isoformat()
        warehouse_known = warehouse_applicable and day in warehouse_exact_dates
        covered_nm_ids = set(warehouse_covered_nm_ids.get(day) or set())
        uncovered_scope_nm_ids = sorted(scope_nm_ids - covered_nm_ids) if warehouse_known else sorted(scope_nm_ids)
        warehouse_totals_known = warehouse_known and not uncovered_scope_nm_ids
        live_day = day == str(operation_business_date or current_business_date_iso())[:10]
        if warehouse_applicable:
            warehouse_coverage[day] = {
                "status": (
                    ("live" if live_day else "closed")
                    if warehouse_totals_known
                    else ("partial" if warehouse_known else "unavailable")
                ),
                "reason_ru": (
                    (
                        "Текущие незакрытые сутки: показан live-снимок канонической бизнес-даты."
                        if live_day
                        else "Точная функциональная версия склада сохранена для закрытой бизнес-даты."
                    )
                    if warehouse_totals_known
                    else (
                        "Исторические итоги недоступны: не все SKU активной витрины входили в scope "
                        "точного складского снимка этой даты. Частичная сумма не публикуется."
                        if warehouse_known
                        else _warehouse_history_unavailable_reason(
                            day=day,
                            cutover_business_date=cutover_business_date,
                        )
                    )
                ),
                "covered_nm_id_count": len(covered_nm_ids) if warehouse_known else 0,
                "uncovered_scope_nm_ids": uncovered_scope_nm_ids,
                "functional_version_id": str(warehouse_version_ids.get(day) or ""),
            }
        for scope in scopes:
            nm_id = int(scope.split(":", 1)[1])
            warehouse_state = day_warehouse.get(nm_id, {})
            sku_warehouse_known = warehouse_known and nm_id in covered_nm_ids
            unavailable_reason = (
                _warehouse_history_unavailable_reason(
                    day=day,
                    cutover_business_date=cutover_business_date,
                )
                if not warehouse_known
                else (
                    "Исторические данные отсутствуют: SKU не входила в requested nmID scope "
                    "и canonical balances точного складского снимка этой даты. Нулевой остаток не предполагается."
                    if not sku_warehouse_known
                    else ""
                )
            )
            for metric_key in (
                OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS if warehouse_applicable else ()
            ):
                row = by_id.get(f"{scope}|{metric_key}")
                if row is None:
                    continue
                value = _warehouse_sku_metric_value(
                    warehouse_state,
                    metric_key=metric_key,
                    warehouse_known=sku_warehouse_known,
                )
                changed += _set_cell(row, index, value)
                presentation_changes += _set_warehouse_cell_presentation(
                    metadata,
                    row_id=f"{scope}|{metric_key}",
                    day=day,
                    unavailable_reason=unavailable_reason,
                    quality_presentation=(
                        _warehouse_sku_quality_presentation(
                            warehouse_state,
                            metric_key=metric_key,
                        )
                        if sku_warehouse_known
                        else None
                    ),
                )
            cost_state = costs.get(day, {}).get(nm_id)
            cost = _optional_decimal((cost_state or {}).get("our_wb_unit_cost_rub"))
            order_sum = _cell_decimal(by_id.get(f"{scope}|orderSum"), index)
            order_count = _cell_decimal(by_id.get(f"{scope}|orderCount"), index)
            ads_sum = _cell_decimal(by_id.get(f"{scope}|ads_sum"), index)
            calculated = calculate_proxy_3(
                order_sum=order_sum,
                order_count=order_count,
                canonical_wb_wac=cost,
                ads_sum=ads_sum,
                parameters=params,
            )
            values = {
                OUR_WB_UNIT_COST_RUB_METRIC_KEY: cost,
                OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY: calculated["proxy_profit_3"],
                OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY: calculated["proxy_margin_3"],
            }
            sku_result[(scope, index)] = calculated
            for metric_key, value in values.items():
                changed += _set_cell(by_id[f"{scope}|{metric_key}"], index, value)

        complete = [sku_result[(scope, index)] for scope in scopes]
        profits = [item["proxy_profit_3"] for item in complete]
        revenues = [item["expected_buyout_revenue"] for item in complete]
        total_profit = None if any(value is None for value in profits) else sum((value for value in profits if value is not None), ZERO)
        total_revenue = None if any(value is None for value in revenues) else sum((value for value in revenues if value is not None), ZERO)
        total_margin = None if total_revenue in (None, ZERO) or total_profit is None else total_profit / total_revenue
        # Public TOTAL WB cost is the whole official contour, not merely the
        # configured/visible SKU subset used for Proxy row aggregation.
        day_costs = costs.get(day, {})
        cost_states = list(day_costs.values())
        quantity_cost_pairs = [
            (
                _optional_decimal((item or {}).get("stock_qty")) or ZERO,
                _optional_decimal((item or {}).get("our_wb_unit_cost_rub")),
            )
            for item in cost_states
        ]
        total_qty = sum((quantity for quantity, _ in quantity_cost_pairs), ZERO)
        missing_visible_cost_row = any(nm_id not in day_costs for nm_id in scope_nm_ids)
        missing_positive_cost = any(quantity > ZERO and cost is None for quantity, cost in quantity_cost_pairs)
        total_capital = sum(
            (quantity * cost for quantity, cost in quantity_cost_pairs if cost is not None),
            ZERO,
        )
        total_cost = (
            total_capital / total_qty
            if total_qty > ZERO
            and not missing_visible_cost_row
            and not missing_positive_cost
            else None
        )
        total_values = {
            TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY: total_cost,
            OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY: total_profit,
            OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY: total_margin,
        }
        for metric_key, value in total_values.items():
            changed += _set_cell(by_id[f"TOTAL|{metric_key}"], index, value)
        if warehouse_applicable:
            visible_warehouse_states = {
                nm_id: state
                for nm_id, state in day_warehouse.items()
                if nm_id in scope_nm_ids
            }
            warehouse_total_values = _warehouse_total_metric_values(
                visible_warehouse_states,
                warehouse_known=warehouse_totals_known,
            )
            totals_unavailable_reason = (
                ""
                if warehouse_totals_known
                else (
                    "Исторические итоги недоступны: не все SKU активной витрины входили в scope "
                    "точного складского снимка этой даты. Частичная сумма не публикуется."
                    if warehouse_known
                    else _warehouse_history_unavailable_reason(
                        day=day,
                        cutover_business_date=cutover_business_date,
                    )
                )
            )
            for metric_key, value in warehouse_total_values.items():
                row = by_id.get(f"TOTAL|{metric_key}")
                if row is not None:
                    changed += _set_cell(row, index, value)
                    presentation_changes += _set_warehouse_cell_presentation(
                        metadata,
                        row_id=f"TOTAL|{metric_key}",
                        day=day,
                        unavailable_reason=totals_unavailable_reason,
                        quality_presentation=(
                            _warehouse_total_quality_presentation(
                                visible_warehouse_states,
                                metric_key=metric_key,
                            )
                            if warehouse_totals_known
                            else None
                        ),
                    )
    marker = {
        "cutover_id": FUNCTIONAL_CUTOVER_ID,
        "source_fingerprint": source_fingerprint,
        "date_from": dates[relevant_indices[0]],
        "date_to": dates[relevant_indices[-1]],
        "target_metric_keys": sorted(active_target_keys),
        "archived_metric_keys": sorted(ARCHIVED_READY_METRIC_KEYS),
    }
    if metadata.get("functional_economics_backfill") != marker:
        metadata["functional_economics_backfill"] = marker
    coverage_changes = int(metadata.get("warehouse_history_coverage") != warehouse_coverage)
    metadata["warehouse_history_coverage"] = warehouse_coverage
    timestamps = metadata.setdefault("row_last_updated_at_by_row_id", {})
    if isinstance(timestamps, dict):
        for row_id in by_id:
            if "|" in row_id and row_id.split("|", 1)[1] in active_target_keys:
                timestamps[row_id] = str(snapshot.get("refreshed_at") or "")
    if inserted or archived_rows_removed:
        _update_data_dimensions(sheet)
    after = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    after_digest = _non_target_digest(plan)
    return {
        "after_plan_json": after,
        "changed_cells": changed,
        "inserted_rows": inserted,
        "archived_rows_removed": archived_rows_removed,
        "presentation_changes": presentation_changes,
        "coverage_changes": coverage_changes,
        "dates": [dates[index] for index in relevant_indices],
        "non_target_before": before_digest,
        "non_target_after": after_digest,
    }


def _warehouse_history_unavailable_reason(
    *,
    day: str,
    cutover_business_date: str,
) -> str:
    if cutover_business_date and day < cutover_business_date:
        return (
            "Исторические данные отсутствуют: до функционального cutover не сохранялся "
            "полный согласованный шестиступенчатый складской снимок. Текущий snapshot назад не копируется."
        )
    return (
        "Исторические данные отсутствуют: для этой даты нет точной успешной "
        "функциональной версии склада. Last-good или snapshot другой даты сюда не переносится."
    )


def _set_warehouse_cell_presentation(
    metadata: dict[str, Any],
    *,
    row_id: str,
    day: str,
    unavailable_reason: str,
    quality_presentation: Mapping[str, str] | None = None,
) -> int:
    """Publish fail-closed history state through the contract consumed by Web UI."""

    raw = metadata.get("server_cell_presentation")
    if raw is None:
        raw = {}
        metadata["server_cell_presentation"] = raw
    if not isinstance(raw, dict):
        raise FunctionalEconomicsBackfillError(
            "ready snapshot server_cell_presentation must be an object"
        )
    by_date = raw.get(row_id)
    if by_date is None:
        by_date = {}
        raw[row_id] = by_date
    if not isinstance(by_date, dict):
        raise FunctionalEconomicsBackfillError(
            f"ready snapshot presentation for {row_id} must be an object"
        )
    if unavailable_reason:
        expected = {
            "state": "unavailable",
            "tone": "neutral",
            "reason": unavailable_reason,
            "source": "WebCore",
        }
        if by_date.get(day) == expected:
            return 0
        by_date[day] = expected
        return 1
    if quality_presentation:
        expected = dict(quality_presentation)
        if by_date.get(day) == expected:
            return 0
        by_date[day] = expected
        return 1
    current = by_date.get(day)
    if (
        isinstance(current, Mapping)
        and str(current.get("source") or "") == "WebCore"
        and str(current.get("state") or "") in {"unavailable", "unconfirmed"}
    ):
        by_date.pop(day, None)
        if not by_date:
            raw.pop(row_id, None)
        if not raw:
            metadata.pop("server_cell_presentation", None)
        return 1
    if not by_date:
        raw.pop(row_id, None)
    if not raw:
        metadata.pop("server_cell_presentation", None)
    return 0


def _warehouse_sku_quality_presentation(
    state: Mapping[str, Any],
    *,
    metric_key: str,
) -> dict[str, str] | None:
    """Project exact-date provisional quality for one canonical warehouse cell."""

    quality_code = ""
    for stage in OWN_PRODUCT_CAPITAL_STAGES:
        if metric_key not in {
            own_stage_metric_key(stage, field)
            for field in ("qty", "unit_cost_rub", "capital_rub")
        }:
            continue
        stage_state = (state.get("stage_presentation") or {}).get(stage, {})
        if str(stage_state.get("state") or "") != "unconfirmed":
            return None
        quality_code = str(stage_state.get("reason") or "provisional")
        break
    else:
        if str(state.get("presentation_state") or "") != "unconfirmed":
            return None
        quality_code = str(state.get("presentation_reason") or "provisional")
    return _unconfirmed_quality_presentation(quality_code)


def _warehouse_total_quality_presentation(
    states: Mapping[int, Mapping[str, Any]],
    *,
    metric_key: str,
) -> dict[str, str] | None:
    """Project yellow status only from the exact states contributing to TOTAL."""

    reasons: list[str] = []
    for state in states.values():
        sku_metric_key = ""
        for stage in OWN_PRODUCT_CAPITAL_STAGES:
            for field in ("qty", "unit_cost_rub", "capital_rub"):
                if metric_key == own_stage_total_metric_key(stage, field):
                    sku_metric_key = own_stage_metric_key(stage, field)
                    break
            if sku_metric_key:
                break
        presentation = (
            _warehouse_sku_quality_presentation(state, metric_key=sku_metric_key)
            if sku_metric_key
            else (
                _unconfirmed_quality_presentation(
                    str(state.get("presentation_reason") or "provisional")
                )
                if str(state.get("presentation_state") or "") == "unconfirmed"
                else None
            )
        )
        if presentation:
            reasons.append(str(presentation["reason"]))
    if not reasons:
        return None
    return {
        "state": "unconfirmed",
        "tone": "yellow",
        "reason": "; ".join(sorted(set(reasons))),
        "source": "WebCore",
    }


def _unconfirmed_quality_presentation(value: str) -> dict[str, str]:
    codes = [item.strip() for item in str(value or "").split(";") if item.strip()]
    presentations = [
        _warehouse_balance_status_presentation(code, certified=False)
        for code in (codes or ["provisional"])
    ]
    return {
        "state": "unconfirmed",
        "tone": "yellow",
        "reason": "; ".join(
            f"{item['label_ru']}. {item['description_ru']}" for item in presentations
        ),
        "source": "WebCore",
    }


def _exact_functional_snapshot_context(
    runtime: RegistryUploadDbBackedRuntime,
    dates: list[str],
) -> dict[str, dict[str, Any]]:
    """Return the exact functional version and SKU scope for each business date."""

    selected = sorted({str(day or "")[:10] for day in dates if str(day or "")[:10]})
    if not selected:
        return {}
    placeholders = ",".join("?" for _ in selected)
    with _connect(runtime.db_path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required = {
            "sheet_vitrina_v1_warehouse_functional_versions",
            "sheet_vitrina_v1_warehouse_wb_snapshots",
        }
        if not required.issubset(tables):
            return {}
        rows = conn.execute(
            f"""SELECT snapshot.snapshot_date,snapshot.requested_nm_ids_json,
                       snapshot.items_json,version.version_id,version.effective_at,
                       version.created_at
                FROM sheet_vitrina_v1_warehouse_functional_versions version
                JOIN sheet_vitrina_v1_warehouse_wb_snapshots snapshot
                  ON snapshot.version_id=version.version_id
                WHERE version.cutover_id=? AND version.status='good'
                  AND snapshot.snapshot_date IN ({placeholders})
                ORDER BY snapshot.snapshot_date,version.created_at DESC,version.version_id DESC""",
            (FUNCTIONAL_CUTOVER_ID, *selected),
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        version_by_day: dict[str, str] = {}
        for row in rows:
            day = str(row["snapshot_date"])
            if day in result or business_date_from_timestamp(str(row["effective_at"])) != day:
                continue
            covered = {
                nm_id
                for value in _json_list(row["requested_nm_ids_json"])
                if (nm_id := _positive_nm_id(value)) is not None
            }
            for item in _json_list(row["items_json"]):
                if not isinstance(item, Mapping):
                    continue
                nm_id = _positive_nm_id(item.get("nm_id") or item.get("nmId"))
                if nm_id is not None:
                    covered.add(nm_id)
            result[day] = {
                "version_id": str(row["version_id"]),
                "covered_nm_ids": covered,
            }
            version_by_day[day] = str(row["version_id"])
        if version_by_day:
            version_ids = sorted(set(version_by_day.values()))
            version_placeholders = ",".join("?" for _ in version_ids)
            balances = conn.execute(
                f"""SELECT version_id,nm_id
                    FROM sheet_vitrina_v1_warehouse_functional_balances
                    WHERE version_id IN ({version_placeholders})""",
                tuple(version_ids),
            ).fetchall()
            day_by_version = {version_id: day for day, version_id in version_by_day.items()}
            for row in balances:
                day = day_by_version.get(str(row["version_id"]))
                nm_id = _positive_nm_id(row["nm_id"])
                if day is not None and nm_id is not None:
                    result[day]["covered_nm_ids"].add(nm_id)
    return result


def _exact_functional_snapshot_coverage(
    runtime: RegistryUploadDbBackedRuntime,
    dates: list[str],
) -> dict[str, set[int]]:
    """Compatibility projection of the exact functional SKU scope."""

    return {
        day: set(item["covered_nm_ids"])
        for day, item in _exact_functional_snapshot_context(runtime, dates).items()
    }


def _exact_functional_snapshot_dates(
    runtime: RegistryUploadDbBackedRuntime,
    dates: list[str],
) -> set[str]:
    """Compatibility projection of exact business dates for diagnostics/tests."""

    return set(_exact_functional_snapshot_coverage(runtime, dates))


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _positive_nm_id(value: Any) -> int | None:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _warehouse_sku_metric_value(
    state: Mapping[str, Any],
    *,
    metric_key: str,
    warehouse_known: bool,
) -> Decimal | None:
    if not warehouse_known:
        return None
    if metric_key in state:
        return _optional_decimal(state.get(metric_key))
    zero_keys = {
        own_stage_metric_key(stage, field)
        for stage in OWN_PRODUCT_CAPITAL_STAGES
        for field in ("qty", "capital_rub")
    } | {OWN_TOTAL_QTY_METRIC_KEY, OWN_TOTAL_CAPITAL_RUB_METRIC_KEY}
    return ZERO if metric_key in zero_keys else None


def _warehouse_total_metric_values(
    states: Mapping[int, Mapping[str, Any]],
    *,
    warehouse_known: bool,
) -> dict[str, Decimal | None]:
    result: dict[str, Decimal | None] = {}
    if not warehouse_known:
        for key in OWN_PRODUCT_CAPITAL_TOTAL_METRIC_KEYS:
            result[key] = None
        return result
    total_quantity = ZERO
    total_capital = ZERO
    for stage in OWN_PRODUCT_CAPITAL_STAGES:
        qty_key = own_stage_metric_key(stage, "qty")
        capital_key = own_stage_metric_key(stage, "capital_rub")
        quantity = sum(
            (_optional_decimal(item.get(qty_key)) or ZERO for item in states.values()),
            ZERO,
        )
        capital = sum(
            (_optional_decimal(item.get(capital_key)) or ZERO for item in states.values()),
            ZERO,
        )
        total_quantity += quantity
        total_capital += capital
        result[own_stage_total_metric_key(stage, "qty")] = quantity
        result[own_stage_total_metric_key(stage, "capital_rub")] = capital
        result[own_stage_total_metric_key(stage, "unit_cost_rub")] = (
            capital / quantity if quantity > ZERO else None
        )
    result[OWN_TOTAL_QTY_TOTAL_METRIC_KEY] = total_quantity
    result[OWN_TOTAL_CAPITAL_RUB_TOTAL_METRIC_KEY] = total_capital
    result[OWN_AVG_COST_RUB_TOTAL_METRIC_KEY] = (
        total_capital / total_quantity if total_quantity > ZERO else None
    )
    return result


def _ensure_target_rows(
    rows: list[Any],
    *,
    by_id: Mapping[str, list[Any]],
    scopes: list[str],
    date_count: int,
    include_warehouse: bool,
) -> int:
    specs = [
        ("TOTAL", TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY, OUR_WB_UNIT_COST_RUB_LABEL),
        ("TOTAL", OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY, OUR_WB_PROXY_PROFIT_3_RUB_LABEL),
        ("TOTAL", OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY, OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_LABEL),
    ]
    warehouse_catalog = {
        (item.scope, item.metric_key): item.label_ru
        for item in build_own_product_capital_metric_items()
        if item.metric_key in WAREHOUSE_TARGET_KEYS
    }
    if include_warehouse:
        specs.extend(
            ("TOTAL", metric_key, label)
            for (scope, metric_key), label in warehouse_catalog.items()
            if scope == "TOTAL"
        )
    for scope in scopes:
        prefix = _scope_label_prefix(by_id, scope)
        specs.extend(
            [
                (scope, OUR_WB_UNIT_COST_RUB_METRIC_KEY, f"{prefix}: {OUR_WB_UNIT_COST_RUB_LABEL}"),
                (scope, OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY, f"{prefix}: {OUR_WB_PROXY_PROFIT_3_RUB_LABEL}"),
                (scope, OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY, f"{prefix}: {OUR_WB_PROXY_MARGIN_3_PCT_LABEL}"),
            ]
        )
        if include_warehouse:
            specs.extend(
                (scope, metric_key, f"{prefix}: {label}")
                for (metric_scope, metric_key), label in warehouse_catalog.items()
                if metric_scope == "SKU"
            )
    inserted = 0
    for scope, metric, label in specs:
        row_id = f"{scope}|{metric}"
        if row_id in by_id:
            continue
        rows.append([label, row_id, *([""] * date_count)])
        inserted += 1
    return inserted


def _remove_archived_metric_rows(rows: list[Any]) -> int:
    retained = []
    removed = 0
    for row in rows:
        row_id = str(row[1] or "") if isinstance(row, list) and len(row) > 1 else ""
        metric_key = row_id.split("|", 1)[1] if "|" in row_id else ""
        if metric_key in ARCHIVED_READY_METRIC_KEYS:
            removed += 1
            continue
        retained.append(row)
    if removed:
        rows[:] = retained
    return removed


def _scope_label_prefix(by_id: Mapping[str, list[Any]], scope: str) -> str:
    for suffix in ("orderSum", "proxy_profit_2_rub", "proxy_profit_rub"):
        row = by_id.get(f"{scope}|{suffix}")
        if row:
            label = str(row[0] or scope)
            return label.split(": ", 1)[0]
    return scope


def _set_cell(row: list[Any], index: int, value: Decimal | None) -> int:
    target_index = 2 + index
    while len(row) <= target_index:
        row.append("")
    normalized: Any = "" if value is None else float(value)
    current = row[target_index]
    if _same_cell(current, normalized):
        return 0
    row[target_index] = normalized
    return 1


def _same_cell(current: Any, expected: Any) -> bool:
    if current in (None, "") or expected in (None, ""):
        return current in (None, "") and expected in (None, "")
    try:
        return abs(Decimal(str(current).replace(",", ".")) - Decimal(str(expected))) <= Decimal("0.0000005")
    except (InvalidOperation, ValueError):
        return False


def _cell_decimal(row: list[Any] | None, index: int) -> Decimal | None:
    if row is None or len(row) <= 2 + index or row[2 + index] in (None, ""):
        return None
    return _optional_decimal(row[2 + index])


def _optional_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _rows_by_id(rows: list[Any]) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 2 or not isinstance(row[1], str):
            continue
        row_id = row[1].strip()
        # Historical ready snapshots can retain presentation-only rows whose
        # second cell is a value rather than a stable projection key.  Public
        # vitrina reads already ignore those rows.  Preserve them byte-for-byte
        # and index only the same stable ``scope|metric`` contract here.
        if not _is_projection_row_id(row_id):
            continue
        if row_id in result:
            raise FunctionalEconomicsBackfillError(f"duplicate ready projection row id: {row_id}")
        result[row_id] = row
    return result


def _is_projection_row_id(value: str) -> bool:
    scope, separator, metric = str(value or "").partition("|")
    return bool(separator and scope.strip() and metric.strip())


def _validate_data_projection_layout(sheet: Mapping[str, Any], *, dates: list[str]) -> None:
    header = sheet.get("header")
    if not isinstance(header, list):
        raise FunctionalEconomicsBackfillError("DATA_VITRINA header is missing")
    if len(header) != 2 + len(dates):
        raise FunctionalEconomicsBackfillError(
            "DATA_VITRINA header width does not match date_columns"
        )
    if [str(value) for value in header[2:]] != dates:
        raise FunctionalEconomicsBackfillError(
            "DATA_VITRINA header dates do not match date_columns"
        )


def _snapshot_dates(plan_json: str) -> list[str]:
    try:
        payload = json.loads(str(plan_json))
        return _date_columns(payload)
    except Exception as exc:
        raise FunctionalEconomicsBackfillError(f"invalid ready snapshot plan: {exc}") from exc


def _non_target_digest(plan: Mapping[str, Any]) -> str:
    value = deepcopy(dict(plan))
    sheet = _data_sheet(value)
    rows = sheet.get("rows") or []
    sheet["rows"] = [
        row
        for row in rows
        if not (
            isinstance(row, list)
            and len(row) > 1
            and "|" in str(row[1])
            and str(row[1]).split("|", 1)[1] in MUTATED_READY_METRIC_KEYS
        )
    ]
    sheet.pop("row_count", None)
    sheet.pop("write_rect", None)
    metadata = value.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("functional_economics_backfill", None)
        metadata.pop("warehouse_history_coverage", None)
        presentation = metadata.get("server_cell_presentation")
        if isinstance(presentation, dict):
            for row_id in list(presentation):
                if "|" in row_id and row_id.split("|", 1)[1] in WAREHOUSE_TARGET_KEYS:
                    presentation.pop(row_id, None)
            if not presentation:
                metadata.pop("server_cell_presentation", None)
        timestamps = metadata.get("row_last_updated_at_by_row_id")
        if isinstance(timestamps, dict):
            for row_id in list(timestamps):
                if "|" in row_id and row_id.split("|", 1)[1] in MUTATED_READY_METRIC_KEYS:
                    timestamps.pop(row_id, None)
            if not timestamps:
                metadata.pop("row_last_updated_at_by_row_id", None)
        if not metadata:
            value.pop("metadata", None)
    return "sha256:" + _hash(value)


def _plan_fingerprint(plan: Mapping[str, Any]) -> str:
    return "sha256:" + _hash({key: value for key, value in plan.items() if key != "plan_fingerprint"})


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _snapshot_manifest_digest(rows: list[Mapping[str, Any]]) -> str:
    return "sha256:" + _hash(
        [
            [
                str(row.get("bundle_version") or ""),
                str(row.get("as_of_date") or ""),
                "sha256:" + _sha(str(row.get("plan_json") or "")),
                str(row.get("refreshed_at") or ""),
            ]
            for row in rows
        ]
    )


def _connect(path: Any) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn
