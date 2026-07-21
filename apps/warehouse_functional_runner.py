#!/usr/bin/env python3
"""Guarded functional cutover and bounded hourly WB warehouse runner."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.stocks_block import HttpBackedStocksSource  # noqa: E402
from packages.application.our_wb_costs import OurWbCostBlock  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
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
    if args.command in {"hourly-sync", "manual-sync"}:
        with warehouse_functional_write_lock(runtime.runtime_dir):
            backup_result = (
                _create_pre_sync_backup(
                    runtime,
                    backup_dir=Path(str(args.backup_dir)),
                    timestamp=block.timestamp_factory(),
                )
                if args.command == "manual-sync"
                else None
            )
            try:
                supply_refresh = _refresh_official_supply_state(
                    runtime,
                    record_ff_movements=False,
                )
                downstream_cost_layers = _materialize_downstream_cost_layers(runtime)
                ff_state = WbSuppliesBlock(runtime=runtime).reconcile_functional_ff_state()
                plan = block.build_sync_plan()
                result = block.apply_plan(
                    plan,
                    confirm_fingerprint=str(plan["plan_fingerprint"]),
                )
                proxy_recalculation = block.calculation_parameters.process_pending_targeted_recalculations()
                economics_publication = block.calculation_parameters.publish_current_functional_economics()
                return {
                    "status": "success",
                    "mode": args.command,
                    "backup": backup_result,
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
                    },
                }
            except Exception as exc:
                block.record_failed_sync(exc)
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
