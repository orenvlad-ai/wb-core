#!/usr/bin/env python3
"""Guarded functional cutover and bounded hourly WB warehouse runner."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import sqlite3
import sys
import tempfile
import time
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.stocks_block import HttpBackedStocksSource  # noqa: E402
from packages.application.our_wb_costs import OurWbCostBlock  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
    registry_runtime_sqlite_busy_timeout,
)
from packages.application.warehouse_functional import (  # noqa: E402
    WarehouseFunctionalBlock,
)
from packages.application.warehouse_functional_economics_backfill import (  # noqa: E402
    apply_functional_economics_backfill_plan,
    build_functional_economics_backfill_plan,
)
from packages.application.warehouse_functional_lock import (  # noqa: E402
    warehouse_functional_write_lock,
)
from packages.application.warehouse_supplier_cost_state_replay import (  # noqa: E402
    apply_supplier_cost_state_replay_plan,
    build_supplier_cost_state_replay_plan,
    rollback_supplier_cost_state_replay,
)
from packages.application.wb_supplies import WbSuppliesBlock  # noqa: E402
from packages.application.stocks_block import StocksBlock  # noqa: E402

WAREHOUSE_SYNC_SQLITE_BUSY_TIMEOUT_MS = 120_000
WAREHOUSE_SYNC_SQLITE_BUSY_TIMEOUT_ENV = (
    "WB_CORE_WAREHOUSE_SYNC_SQLITE_BUSY_TIMEOUT_MS"
)
WAREHOUSE_SYNC_COMMANDS = frozenset({"hourly-sync", "manual-sync", "sync-apply"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--env-file", default="")
    commands = parser.add_subparsers(dest="command", required=True)

    cutover_dry_run = commands.add_parser("cutover-dry-run")
    cutover_dry_run.add_argument("--output", default="")
    cutover_dry_run.add_argument("--skip-supply-refresh", action="store_true")

    cutover_apply = commands.add_parser("cutover-apply")
    _add_exact_plan_args(cutover_apply)
    cutover_apply.add_argument("--backup-dir", required=True)

    commands.add_parser("readback")
    backup = commands.add_parser("backup")
    backup.add_argument("--backup-dir", required=True)
    commands.add_parser("hourly-sync")
    manual_sync = commands.add_parser("manual-sync")
    manual_sync.add_argument("--backup-dir", required=True)
    sync_dry_run = commands.add_parser("sync-dry-run")
    sync_dry_run.add_argument("--output", default="")
    sync_apply = commands.add_parser("sync-apply")
    _add_exact_plan_args(sync_apply)
    sync_apply.add_argument("--backup-dir", required=True)

    emergency_dry_run = commands.add_parser("emergency-dry-run")
    emergency_dry_run.add_argument("--output", default="")

    emergency_apply = commands.add_parser("emergency-apply")
    _add_exact_plan_args(emergency_apply)
    emergency_apply.add_argument("--backup-dir", required=True)

    economics_dry_run = commands.add_parser("economics-backfill-dry-run")
    economics_dry_run.add_argument("--output", default="")

    economics_apply = commands.add_parser("economics-backfill-apply")
    _add_exact_plan_args(economics_apply)
    economics_apply.add_argument("--backup-dir", required=True)

    certification_dry_run = commands.add_parser("supplier-certification-dry-run")
    certification_dry_run.add_argument("--output", default="")
    certification_dry_run.add_argument("--shipment-id", action="append", default=[])

    certification_apply = commands.add_parser("supplier-certification-apply")
    _add_exact_plan_args(certification_apply)
    certification_apply.add_argument("--backup-dir", required=True)

    certification_rollback = commands.add_parser("supplier-certification-rollback")
    certification_rollback.add_argument("--fingerprint", required=True)
    certification_rollback.add_argument("--reason", required=True)
    certification_rollback.add_argument("--backup-dir", required=True)

    rollback = commands.add_parser("rollback")
    rollback.add_argument("--fingerprint", required=True)
    rollback.add_argument("--backup-dir", required=True)
    return parser


def _add_exact_plan_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--plan-file", required=True)
    parser.add_argument("--fingerprint", required=True)


def run(args: argparse.Namespace) -> dict[str, Any]:
    if str(args.env_file or "").strip():
        _load_env_file(Path(str(args.env_file)).resolve())
    sqlite_busy_timeout_ms = _warehouse_sync_sqlite_busy_timeout_ms(args.command)
    with registry_runtime_sqlite_busy_timeout(sqlite_busy_timeout_ms):
        return _run(args, sqlite_busy_timeout_ms=sqlite_busy_timeout_ms)


def _run(
    args: argparse.Namespace,
    *,
    sqlite_busy_timeout_ms: int | None,
) -> dict[str, Any]:
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(str(args.runtime_dir)).resolve())
    block = WarehouseFunctionalBlock(runtime=runtime, stocks_block=_fresh_stocks_block())

    if args.command == "cutover-dry-run":
        if bool(args.skip_supply_refresh):
            supply_refresh = None
            plan = block.build_cutover_plan()
        else:
            plan, supply_refresh = _build_cutover_plan_from_disposable_refresh(runtime)
        return _write_optional_plan(plan, str(args.output or ""), supply_refresh=supply_refresh)
    if args.command == "cutover-apply":
        reviewed_plan = _read_exact_plan(
            args.plan_file,
            args.fingerprint,
            expected_kind="functional_cutover",
        )
        active = block.readback()
        if active.get("status") == "ready":
            active_fingerprint = str((active.get("cutover") or {}).get("plan_fingerprint") or "")
            if active_fingerprint != str(args.fingerprint):
                raise RuntimeError("another functional cutover fingerprint is already active")
            return block.apply_plan(
                reviewed_plan,
                confirm_fingerprint=str(args.fingerprint),
                backup_dir=Path(str(args.backup_dir)).resolve(),
            ) | {
                "external_optimistic_recheck": {
                    "status": "skipped_exact_cutover_already_active",
                    "primary_sources_changed": False,
                }
            }
        fresh_plan, external_recheck = _build_cutover_plan_from_disposable_refresh(runtime)
        _verify_cutover_external_recheck(reviewed_plan, fresh_plan)
        return block.apply_plan(
            reviewed_plan,
            confirm_fingerprint=str(args.fingerprint),
            backup_dir=Path(str(args.backup_dir)).resolve(),
        ) | {"external_optimistic_recheck": external_recheck}
    if args.command == "readback":
        return block.readback()
    if args.command == "backup":
        with warehouse_functional_write_lock(runtime.runtime_dir):
            return {
                "status": "success",
                "mode": "backup",
                "backup": _create_pre_sync_backup(
                    runtime,
                    backup_dir=Path(str(args.backup_dir)),
                    timestamp=block.timestamp_factory(),
                ),
            }
    if args.command == "sync-dry-run":
        plan, preflight = _build_sync_plan_from_disposable_refresh(runtime)
        return _write_optional_plan(plan, str(args.output or ""), supply_refresh=preflight)
    if args.command == "sync-apply":
        reviewed_plan = _read_exact_plan(
            args.plan_file,
            args.fingerprint,
            expected_kind="hourly_wb_sync",
        )
        with warehouse_functional_write_lock(runtime.runtime_dir):
            block.calculation_parameters.preflight_fresh_economics_backup_capacity(
                Path(str(args.backup_dir)),
            )
            backup_result = {
                **_create_pre_sync_backup(
                    runtime,
                    backup_dir=Path(str(args.backup_dir)),
                    timestamp=block.timestamp_factory(),
                ),
                "backup_scope": "reviewed_bounded_recovery",
            }
            if str(backup_result.get("integrity_check") or "").lower() != "ok":
                _discard_uncommitted_backup(backup_result)
                raise RuntimeError(
                    "reviewed bounded recovery backup integrity_check is not ok"
                )
            try:
                supply_refresh = _refresh_official_supply_state(
                    runtime,
                    record_ff_movements=False,
                )
                downstream_cost_layers = _materialize_downstream_cost_layers(runtime)
                ff_state = WbSuppliesBlock(runtime=runtime).reconcile_functional_ff_state()
                fresh_plan = block.build_sync_plan()
                recheck = _verify_sync_external_recheck(reviewed_plan, fresh_plan)
                result = block.apply_plan(
                    reviewed_plan,
                    confirm_fingerprint=str(args.fingerprint),
                )
                proxy_recalculation = (
                    block.calculation_parameters.process_pending_targeted_recalculations(
                        verified_backup=backup_result,
                    )
                )
                if str(proxy_recalculation.get("status") or "") == "failed":
                    raise RuntimeError(
                        "targeted Proxy recalculation failed: "
                        + str(proxy_recalculation.get("error") or "unknown error")
                    )
                economics_publication = (
                    proxy_recalculation
                    if int(proxy_recalculation.get("request_count") or 0) > 0
                    else block.calculation_parameters.publish_current_functional_economics(
                        verified_backup=backup_result,
                    )
                )
                return {
                    "status": "success",
                    "mode": "reviewed_sync_apply",
                    "reviewed_plan_fingerprint": args.fingerprint,
                    "backup": backup_result,
                    "supply_refresh": supply_refresh,
                    "downstream_cost_layers_materialized": downstream_cost_layers,
                    "ff_state": ff_state,
                    "external_optimistic_recheck": recheck,
                    "diff": reviewed_plan.get("diff"),
                    "active_version": result.get("active_version"),
                    "sync": result.get("sync"),
                    "reconciliation": result.get("reconciliation"),
                    "proxy_targeted_recalculation": proxy_recalculation,
                    "functional_economics_publication": {
                        "plan_fingerprint": economics_publication.get("plan_fingerprint"),
                        "changed_snapshot_count": economics_publication.get(
                            "changed_snapshot_count"
                        ),
                        "database_written": economics_publication.get("database_written"),
                        "backup_archive": economics_publication.get("backup_archive"),
                    },
                }
            except Exception as exc:
                block.record_failed_sync(exc)
                raise
    if args.command in {"hourly-sync", "manual-sync"}:
        phase_timings_ms: dict[str, float] = {}
        with warehouse_functional_write_lock(runtime.runtime_dir) as lock_evidence:
            phase_timings_ms["warehouse_lock_wait"] = float(
                lock_evidence.get("wait_ms") or 0
            )
            if args.command == "manual-sync":
                _run_sync_phase(
                    "backup_capacity_preflight",
                    phase_timings_ms,
                    lambda: (
                        block.calculation_parameters.preflight_fresh_economics_backup_capacity(
                            Path(str(args.backup_dir)),
                        )
                    ),
                )
            backup_result = (
                {
                    **_run_sync_phase(
                        "create_manual_restore_point",
                        phase_timings_ms,
                        lambda: _create_pre_sync_backup(
                            runtime,
                            backup_dir=Path(str(args.backup_dir)),
                            timestamp=block.timestamp_factory(),
                        ),
                    ),
                    "backup_scope": "fresh_manual_sync",
                }
                if args.command == "manual-sync"
                else None
            )
            try:
                economics_backup = _run_sync_phase(
                    "prepare_economics_restore_point",
                    phase_timings_ms,
                    lambda: (
                        backup_result
                        if backup_result is not None
                        else block.calculation_parameters.prepare_functional_economics_backup()
                    ),
                )
                supply_refresh = _run_sync_phase(
                    "refresh_official_supply_state",
                    phase_timings_ms,
                    lambda: _refresh_official_supply_state(
                        runtime,
                        record_ff_movements=False,
                    ),
                )
                downstream_cost_layers = _run_sync_phase(
                    "materialize_downstream_cost_layers",
                    phase_timings_ms,
                    lambda: _materialize_downstream_cost_layers(runtime),
                )
                ff_state = _run_sync_phase(
                    "reconcile_functional_ff_state",
                    phase_timings_ms,
                    lambda: WbSuppliesBlock(runtime=runtime).reconcile_functional_ff_state(),
                )
                plan = _run_sync_phase(
                    "build_sync_plan",
                    phase_timings_ms,
                    block.build_sync_plan,
                )
                result = _run_sync_phase(
                    "publish_functional_version",
                    phase_timings_ms,
                    lambda: block.apply_plan(
                        plan,
                        confirm_fingerprint=str(plan["plan_fingerprint"]),
                    ),
                )
                proxy_recalculation = _run_sync_phase(
                    "process_targeted_recalculations",
                    phase_timings_ms,
                    lambda: (
                        block.calculation_parameters.process_pending_targeted_recalculations(
                            verified_backup=economics_backup,
                        )
                    ),
                )
                if str(proxy_recalculation.get("status") or "") == "failed":
                    raise RuntimeError(
                        "targeted Proxy recalculation failed: "
                        + str(proxy_recalculation.get("error") or "unknown error")
                    )
                economics_publication = _run_sync_phase(
                    "publish_functional_economics",
                    phase_timings_ms,
                    lambda: (
                        proxy_recalculation
                        if int(proxy_recalculation.get("request_count") or 0) > 0
                        else block.calculation_parameters.publish_current_functional_economics(
                            verified_backup=economics_backup,
                        )
                    ),
                )
                completed_backup = (
                    economics_publication.get("backup_archive")
                    if backup_result is not None
                    else backup_result
                )
                return {
                    "status": "success",
                    "mode": args.command,
                    "sqlite_busy_timeout_ms": sqlite_busy_timeout_ms,
                    "phase_timings_ms": phase_timings_ms,
                    "backup": completed_backup,
                    "raw_backup": (
                        {
                            **dict(backup_result),
                            "source_removed": bool(
                                (economics_publication.get("backup_archive") or {}).get(
                                    "source_removed"
                                )
                            ),
                        }
                        if backup_result is not None
                        else None
                    ),
                    "supply_refresh": supply_refresh,
                    "downstream_cost_layers_materialized": downstream_cost_layers,
                    "ff_state": ff_state,
                    "plan_fingerprint": plan["plan_fingerprint"],
                    "diff": plan["diff"],
                    "active_version": result.get("active_version"),
                    "sync": result.get("sync"),
                    "reconciliation": result.get("reconciliation"),
                    "proxy_targeted_recalculation": proxy_recalculation,
                    "functional_economics_publication": {
                        "plan_fingerprint": economics_publication.get("plan_fingerprint"),
                        "changed_snapshot_count": economics_publication.get("changed_snapshot_count"),
                        "database_written": economics_publication.get("database_written"),
                        "backup_archive": economics_publication.get("backup_archive"),
                    },
                }
            except Exception as exc:
                failure = _sync_failure_record(
                    exc,
                    sqlite_busy_timeout_ms=sqlite_busy_timeout_ms,
                    phase_timings_ms=phase_timings_ms,
                )
                try:
                    block.record_failed_sync(failure)
                except sqlite3.OperationalError as record_exc:
                    if not _is_sqlite_locked_error(record_exc):
                        raise
                raise
    if args.command == "emergency-dry-run":
        plan = block.build_emergency_rebuild_plan()
        return _write_optional_plan(plan, str(args.output or ""))
    if args.command == "emergency-apply":
        return block.apply_plan(
            _read_exact_plan(args.plan_file, args.fingerprint, expected_kind="emergency_rebuild"),
            confirm_fingerprint=str(args.fingerprint),
            backup_dir=Path(str(args.backup_dir)).resolve(),
        )
    if args.command == "economics-backfill-dry-run":
        return _write_optional_plan(
            build_functional_economics_backfill_plan(runtime),
            str(args.output or ""),
        )
    if args.command == "economics-backfill-apply":
        return apply_functional_economics_backfill_plan(
            runtime,
            _read_exact_plan(
                args.plan_file,
                args.fingerprint,
                expected_kind="functional_economics_backfill",
            ),
            confirm_fingerprint=str(args.fingerprint),
            backup_dir=Path(str(args.backup_dir)).resolve(),
        )
    if args.command == "supplier-certification-dry-run":
        return _write_optional_plan(
            build_supplier_cost_state_replay_plan(
                runtime,
                shipment_ids=args.shipment_id,
            ),
            str(args.output or ""),
        )
    if args.command == "supplier-certification-apply":
        return apply_supplier_cost_state_replay_plan(
            runtime,
            _read_exact_plan(
                args.plan_file,
                args.fingerprint,
                expected_kind="supplier_certification_replay",
            ),
            confirm_fingerprint=str(args.fingerprint),
            backup_dir=Path(str(args.backup_dir)).resolve(),
        )
    if args.command == "supplier-certification-rollback":
        return rollback_supplier_cost_state_replay(
            runtime,
            replay_plan_fingerprint=str(args.fingerprint),
            reason=str(args.reason),
            backup_dir=Path(str(args.backup_dir)).resolve(),
        )
    if args.command == "rollback":
        return block.rollback_functional_cutover(
            confirm_fingerprint=str(args.fingerprint),
            backup_dir=Path(str(args.backup_dir)).resolve(),
        )
    raise ValueError(f"unsupported command: {args.command}")


def _build_cutover_plan_from_disposable_refresh(
    runtime: RegistryUploadDbBackedRuntime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Refresh official supplies without mutating production WB/FF source rows."""

    with tempfile.TemporaryDirectory(prefix="wb-core-functional-cutover-") as raw_dir:
        disposable_dir = Path(raw_dir) / "state"
        disposable_dir.mkdir(parents=True, exist_ok=True)
        runtime.backup_database(disposable_dir / "registry_upload_runtime.sqlite3")
        disposable_runtime = RegistryUploadDbBackedRuntime(runtime_dir=disposable_dir)
        supply_refresh = _refresh_official_supply_state(
            disposable_runtime,
            record_ff_movements=False,
        )
        downstream_cost_layers = _materialize_downstream_cost_layers(disposable_runtime)
        disposable_block = WarehouseFunctionalBlock(
            runtime=disposable_runtime,
            stocks_block=_fresh_stocks_block(),
        )
        plan = disposable_block.build_cutover_plan()
    return plan, {
        **supply_refresh,
        "downstream_cost_layers_materialized": downstream_cost_layers,
        "production_source_mutation": False,
        "capture_mode": "coherent_disposable_sqlite_copy",
    }


def _build_sync_plan_from_disposable_refresh(
    runtime: RegistryUploadDbBackedRuntime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a mutation-free reviewed recovery plan against fresh official sources."""

    with tempfile.TemporaryDirectory(prefix="wb-core-functional-sync-plan-") as raw_dir:
        disposable_dir = Path(raw_dir) / "state"
        disposable_dir.mkdir(parents=True, exist_ok=True)
        runtime.backup_database(disposable_dir / "registry_upload_runtime.sqlite3")
        disposable_runtime = RegistryUploadDbBackedRuntime(runtime_dir=disposable_dir)
        supply_refresh = _refresh_official_supply_state(
            disposable_runtime,
            record_ff_movements=False,
        )
        downstream_cost_layers = _materialize_downstream_cost_layers(disposable_runtime)
        ff_state = WbSuppliesBlock(runtime=disposable_runtime).reconcile_functional_ff_state()
        disposable_block = WarehouseFunctionalBlock(
            runtime=disposable_runtime,
            stocks_block=_fresh_stocks_block(),
        )
        plan = disposable_block.build_sync_plan()
    return plan, {
        **supply_refresh,
        "downstream_cost_layers_materialized": downstream_cost_layers,
        "ff_state": ff_state,
        "production_source_mutation": False,
        "capture_mode": "coherent_disposable_sqlite_copy",
    }


def _refresh_official_supply_state(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    record_ff_movements: bool = True,
) -> dict[str, Any]:
    block = WbSuppliesBlock(runtime=runtime)
    result = block.sync_functional_sources(
        record_ff_movements=record_ff_movements,
    )
    sync = dict(result.get("sync") or {})
    return {
        "run_id": str(sync.get("run_id") or ""),
        "new_rows": int(sync.get("new_rows") or 0),
        "changed_rows": int(sync.get("changed_rows") or 0),
        "accepted_qty_changed_rows": int(sync.get("accepted_qty_changed_rows") or 0),
        "active_reconciliation_complete": bool(sync.get("active_reconciliation_complete")),
        "partial_status_slices": bool(sync.get("partial_status_slices")),
        "failed_enrich": int(sync.get("failed_enrich") or 0),
        "enrichment_failures": list(sync.get("enrichment_failures") or []),
        "warnings": list(sync.get("warnings") or []),
        "record_ff_movements": record_ff_movements,
        "ff_stock_debits": dict(sync.get("ff_stock_debits") or {}),
        "ff_auto_writeoff_checkpoint": dict(sync.get("ff_auto_writeoff_checkpoint") or {}),
    }


def _fresh_stocks_block() -> StocksBlock:
    """Return an official source whose cutover/hourly fetch cannot reuse a prior capture."""

    return StocksBlock(HttpBackedStocksSource(reuse_ttl_seconds=0.0))


def _create_pre_sync_backup(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    backup_dir: Path,
    timestamp: str,
) -> dict[str, Any]:
    if not backup_dir.is_absolute():
        raise ValueError("warehouse functional backup requires an absolute backup directory")
    resolved_dir = backup_dir.resolve()
    resolved_dir.mkdir(parents=True, exist_ok=True)
    normalized_timestamp = str(timestamp).replace(":", "").replace("-", "")
    destination = resolved_dir / f"warehouse-functional-pre-sync-{normalized_timestamp}.sqlite3"
    backup_result = runtime.backup_database(destination)
    destination.chmod(0o600)
    return backup_result


def _materialize_downstream_cost_layers(runtime: RegistryUploadDbBackedRuntime) -> int:
    """Refresh only supply-specific cost components used by the functional engine.

    This deliberately does not publish the legacy WB daily cost model, rebuild
    product capital, or trigger the global vitrina refresh.
    """

    return OurWbCostBlock(runtime=runtime).materialize_wb_supply_cost_layers(
        opening_date="2026-07-01"
    )


def _verify_cutover_external_recheck(
    reviewed: Mapping[str, Any],
    fresh: Mapping[str, Any],
) -> None:
    reviewed_snapshot = dict(reviewed.get("wb_snapshot") or {})
    fresh_snapshot = dict(fresh.get("wb_snapshot") or {})
    comparisons = {
        "local_source_digest": (
            reviewed.get("local_source_digest"),
            fresh.get("local_source_digest"),
        ),
        "wb_supply_source_digest": (
            reviewed.get("wb_supply_source_digest"),
            fresh.get("wb_supply_source_digest"),
        ),
        "wb_snapshot.raw_rows_digest": (
            reviewed_snapshot.get("raw_rows_digest"),
            fresh_snapshot.get("raw_rows_digest"),
        ),
        "wb_snapshot.requested_nm_ids": (
            reviewed_snapshot.get("requested_nm_ids"),
            fresh_snapshot.get("requested_nm_ids"),
        ),
        "wb_snapshot.raw_row_count": (
            reviewed_snapshot.get("raw_row_count"),
            fresh_snapshot.get("raw_row_count"),
        ),
        "calculation_digest": (
            reviewed.get("calculation_digest"),
            fresh.get("calculation_digest"),
        ),
    }
    drifted = [key for key, (before, after) in comparisons.items() if before != after]
    if drifted:
        raise RuntimeError(
            "official WB sources drifted after reviewed cutover dry-run: " + ",".join(drifted)
        )
    if not bool(fresh_snapshot.get("pagination_complete")):
        raise RuntimeError("official WB optimistic recheck is incomplete")


def _verify_sync_external_recheck(
    reviewed: Mapping[str, Any],
    fresh: Mapping[str, Any],
) -> dict[str, Any]:
    reviewed_snapshot = dict(reviewed.get("wb_snapshot") or {})
    fresh_snapshot = dict(fresh.get("wb_snapshot") or {})
    comparisons = {
        "base_active_version_id": (
            reviewed.get("base_active_version_id"),
            fresh.get("base_active_version_id"),
        ),
        "local_source_digest": (
            reviewed.get("local_source_digest"),
            fresh.get("local_source_digest"),
        ),
        "wb_supply_source_digest": (
            reviewed.get("wb_supply_source_digest"),
            fresh.get("wb_supply_source_digest"),
        ),
        "wb_snapshot.snapshot_date": (
            reviewed_snapshot.get("snapshot_date"),
            fresh_snapshot.get("snapshot_date"),
        ),
        "wb_snapshot.raw_rows_digest": (
            reviewed_snapshot.get("raw_rows_digest"),
            fresh_snapshot.get("raw_rows_digest"),
        ),
        "wb_snapshot.requested_nm_ids": (
            reviewed_snapshot.get("requested_nm_ids"),
            fresh_snapshot.get("requested_nm_ids"),
        ),
        "wb_snapshot.raw_row_count": (
            reviewed_snapshot.get("raw_row_count"),
            fresh_snapshot.get("raw_row_count"),
        ),
        "calculation_digest": (
            reviewed.get("calculation_digest"),
            fresh.get("calculation_digest"),
        ),
        "diff": (reviewed.get("diff"), fresh.get("diff")),
        "invariants": (reviewed.get("invariants"), fresh.get("invariants")),
    }
    drifted = [key for key, (before, after) in comparisons.items() if before != after]
    if drifted:
        raise RuntimeError(
            "reviewed warehouse sync plan is stale after source recheck: "
            + ",".join(drifted)
        )
    if not bool(fresh_snapshot.get("pagination_complete")):
        raise RuntimeError("official WB optimistic recheck is incomplete")
    return {
        "status": "matched",
        "primary_sources_changed": False,
        "checked_fields": sorted(comparisons),
        "fresh_plan_fingerprint": fresh.get("plan_fingerprint"),
    }


def _read_exact_plan(plan_file: str, fingerprint: str, *, expected_kind: str) -> dict[str, Any]:
    path = str(plan_file or "").strip()
    text = sys.stdin.read() if path in {"-", "/dev/stdin"} else Path(path).resolve().read_text(encoding="utf-8")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("plan-file must contain a JSON object")
    actual_kind = str(payload.get("kind") or "")
    if expected_kind == "functional_economics_backfill":
        if str(payload.get("contract_name") or "") != "sheet_vitrina_v1_functional_economics_backfill":
            raise ValueError("expected functional economics backfill plan")
    elif expected_kind == "supplier_certification_replay":
        if str(payload.get("contract_name") or "") != (
            "sheet_vitrina_v1_warehouse_supplier_cost_state_replay"
        ):
            raise ValueError("expected supplier certification replay plan")
    elif actual_kind != expected_kind:
        raise ValueError(f"expected {expected_kind} plan")
    if str(payload.get("plan_fingerprint") or "") != str(fingerprint or ""):
        raise ValueError("plan and --fingerprint do not match")
    return payload


def _write_optional_plan(
    plan: dict[str, Any],
    output: str,
    *,
    supply_refresh: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = str(output or "").strip()
    result = {**plan}
    if supply_refresh is not None:
        result["preflight_supply_refresh"] = supply_refresh
    if path:
        target = Path(path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_json(plan) + "\n", encoding="utf-8")
        result["plan_file"] = str(target)
    return result


def _warehouse_sync_sqlite_busy_timeout_ms(command: str) -> int | None:
    if str(command or "") not in WAREHOUSE_SYNC_COMMANDS:
        return None
    raw_value = str(
        os.environ.get(
            WAREHOUSE_SYNC_SQLITE_BUSY_TIMEOUT_ENV,
            WAREHOUSE_SYNC_SQLITE_BUSY_TIMEOUT_MS,
        )
    ).strip()
    try:
        timeout_ms = int(raw_value)
    except ValueError as exc:
        raise ValueError(
            f"{WAREHOUSE_SYNC_SQLITE_BUSY_TIMEOUT_ENV} must be an integer"
        ) from exc
    if not 5_000 <= timeout_ms <= 300_000:
        raise ValueError(
            f"{WAREHOUSE_SYNC_SQLITE_BUSY_TIMEOUT_ENV} must be between 5000 and 300000"
        )
    return timeout_ms


def _run_sync_phase(
    phase: str,
    phase_timings_ms: dict[str, float],
    callback,
):
    started = time.monotonic()
    try:
        result = callback()
    except Exception as exc:
        elapsed_ms = round((time.monotonic() - started) * 1000, 3)
        phase_timings_ms[phase] = elapsed_ms
        if _is_sqlite_locked_error(exc):
            raise RuntimeError(
                "warehouse_sync_sqlite_write_wait_expired "
                f"phase={phase} elapsed_ms={elapsed_ms}; last-good version preserved"
            ) from exc
        raise
    else:
        phase_timings_ms[phase] = round((time.monotonic() - started) * 1000, 3)
        return result


def _sync_failure_record(
    error: Exception,
    *,
    sqlite_busy_timeout_ms: int | None,
    phase_timings_ms: Mapping[str, float],
) -> RuntimeError:
    bounded_error = str(error).replace("\n", " ").strip()[:1000]
    timings = json.dumps(
        dict(phase_timings_ms),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return RuntimeError(
        f"{bounded_error}; sqlite_busy_timeout_ms={sqlite_busy_timeout_ms}; "
        f"phase_timings_ms={timings}"
    )


def _is_sqlite_locked_error(error: BaseException) -> bool:
    normalized = str(error).casefold()
    return "database is locked" in normalized or "database table is locked" in normalized


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        raise ValueError(f"environment file does not exist: {path}")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key.isidentifier():
            continue
        lexer = shlex.shlex(raw_value, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        os.environ[key] = " ".join(lexer)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def main() -> int:
    try:
        result = run(build_parser().parse_args())
    except Exception as exc:
        print(_json({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    print(_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
