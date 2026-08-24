#!/usr/bin/env python3
"""Repo-owned CLI for WB Finance weekly backfill, sync, recalculation and due tick."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.wb_finance_weekly import WbFinanceApiClient, block_from_env  # noqa: E402
from packages.application.warehouse_recovery_policy import (  # noqa: E402
    BeforeImageQuery,
    RecoveryState,
    WarehouseRecoveryRegistry,
)


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in os.environ:
            continue
        try:
            parsed = shlex.split(value, posix=True)
        except ValueError:
            parsed = []
        os.environ[key.strip()] = parsed[0] if parsed else value.strip().strip("\"'")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "ensure-schema",
            "backfill",
            "sync-week",
            "recalculate",
            "recalculate-all",
            "recalculate-stale-cost",
            "canonical-cost-backfill",
            "business-approved-backfill",
            "repair-derived-orphans",
            "tick",
            "status",
        ),
    )
    parser.add_argument(
        "--runtime-dir",
        default=os.environ.get(
            "REGISTRY_UPLOAD_RUNTIME_DIR", ".runtime/registry_upload"
        ),
    )
    parser.add_argument("--env-file", default="/opt/wb-ai/.env")
    parser.add_argument("--date-from", default="")
    parser.add_argument("--date-to", default="")
    parser.add_argument("--today", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-fingerprint", default="")
    parser.add_argument("--backup-dir", default="")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--min-interval-seconds", type=float, default=60.0)
    args = parser.parse_args(argv)
    _load_env(Path(args.env_file))
    block = block_from_env(Path(args.runtime_dir))
    if args.command == "ensure-schema":
        block.ensure_schema()
        result = {"status": "ok", "schema": "wb_finance_weekly_v1"}
    elif args.command == "status":
        result = block.build_payload()
    elif args.command == "recalculate":
        result = block.recalculate_week(
            date.fromisoformat(args.date_from), date.fromisoformat(args.date_to)
        )
    elif args.command == "recalculate-all":
        result = block.recalculate_all_weeks()
    elif args.command == "recalculate-stale-cost":
        date_from = date.fromisoformat(args.date_from) if args.date_from else None
        date_to = date.fromisoformat(args.date_to) if args.date_to else None
        plan_kwargs = {"date_to": date_to}
        if date_from is not None:
            plan_kwargs["date_from"] = date_from
        plan = block.plan_stale_cost_weeks(**plan_kwargs)
        if not args.apply:
            result = plan
        else:
            if not args.confirm_fingerprint:
                parser.error("--apply requires --confirm-fingerprint from dry-run")
            if not args.backup_dir:
                parser.error("--apply requires an explicit --backup-dir")
            if args.confirm_fingerprint != str(plan["fingerprint"]):
                parser.error("--confirm-fingerprint does not match current dry-run")
            registry = WarehouseRecoveryRegistry(
                runtime_dir=Path(args.runtime_dir),
                db_path=block.db_path,
            )
            if int(plan["stale_week_count"]) > 0:
                recovery = _prepare_bounded_finance_recovery(
                    registry,
                    block.db_path,
                    plan,
                )
                if recovery["lifecycle"] == RecoveryState.VERIFIED.value:
                    recovery = registry.begin_mutation(
                        recovery["operation_id"],
                        expected_source_digest=str(plan["target_before_digest"]),
                    )
            else:
                recovery = registry.plan_noop(
                    mutation_kind="canonical_cost_bounded_publication",
                    closure_kind="sku_date",
                    plan_fingerprint=str(plan["fingerprint"]),
                    scope={
                        "date_from": plan["date_from"],
                        "date_to": plan["date_to"],
                        "weeks": [],
                    },
                )
            apply_kwargs = {
                "expected_fingerprint": args.confirm_fingerprint,
                "date_to": date_to,
            }
            if date_from is not None:
                apply_kwargs["date_from"] = date_from
            try:
                result = block.apply_stale_cost_weeks(**apply_kwargs)
            except Exception as exc:
                if recovery["tier"] != "T0":
                    registry.fail_recoverable(
                        recovery["operation_id"],
                        error=str(exc),
                        next_action="resume_or_rollback_stale_finance_cost",
                    )
                raise
            if recovery["tier"] != "T0":
                recovery = registry.retain(
                    recovery["operation_id"],
                    after_digest=str(result["fingerprint"]),
                    non_target_digest=str(result["non_target_digest_after"]),
                )
            result["backup"] = (
                None
                if recovery["tier"] == "T0"
                else {
                    "kind": "target_scoped_before_image",
                    "operation_id": recovery["operation_id"],
                    "copy_bytes": 0,
                }
            )
            result["recovery_policy"] = recovery
    elif args.command == "canonical-cost-backfill":
        date_from = date.fromisoformat(args.date_from) if args.date_from else None
        date_to = date.fromisoformat(args.date_to) if args.date_to else None
        if not args.apply:
            result = block.plan_canonical_finance_backfill(
                date_from=date_from,
                date_to=date_to,
            )
        else:
            # Planning is query-only and intentionally does not hold the global
            # warehouse writer lock. The application method rechecks the exact
            # target fingerprint and holds only its short target CAS write.
            with nullcontext():
                plan = block.plan_canonical_finance_backfill(
                    date_from=date_from,
                    date_to=date_to,
                )
                if not args.confirm_fingerprint:
                    parser.error("--apply requires --confirm-fingerprint from the new dry-run")
                if not args.approval_reference:
                    parser.error("--apply requires --approval-reference for the new human gate")
                already_applied = block.canonical_finance_fingerprint_applied(
                    fingerprint=args.confirm_fingerprint
                )
                plan_date_from = str(plan.get("date_from") or args.date_from or "")
                plan_date_to = str(plan.get("date_to") or args.date_to or "")
                if args.confirm_fingerprint != str(plan["fingerprint"]) and not already_applied:
                    parser.error("--confirm-fingerprint does not match the current canonical dry-run")
                if not bool(plan.get("apply_allowed")) and not already_applied:
                    parser.error("canonical dry-run contains blockers; apply is forbidden")
                registry = WarehouseRecoveryRegistry(
                    runtime_dir=Path(args.runtime_dir),
                    db_path=block.db_path,
                )
                if already_applied:
                    recovery = registry.plan_noop(
                        mutation_kind="canonical_cost_bounded_publication",
                        closure_kind="sku_date",
                        plan_fingerprint=args.confirm_fingerprint,
                        scope={
                            "date_from": plan_date_from,
                            "date_to": plan_date_to,
                            "weeks": [],
                        },
                    )
                else:
                    recovery = _prepare_bounded_finance_recovery(
                        registry,
                        block.db_path,
                        plan,
                    )
                    if recovery["lifecycle"] == RecoveryState.VERIFIED.value:
                        recovery = registry.begin_mutation(
                            recovery["operation_id"],
                            expected_source_digest=str(
                                plan["backup_recovery_plan"]["before_image_digest"]
                            ),
                        )
                try:
                    result = block.apply_canonical_finance_backfill(
                        expected_fingerprint=args.confirm_fingerprint,
                        approval_reference=args.approval_reference,
                        date_from=date_from,
                        date_to=date_to,
                    )
                except Exception as exc:
                    if recovery["tier"] != "T0":
                        registry.fail_recoverable(
                            recovery["operation_id"],
                            error=str(exc),
                            next_action="resume_or_rollback_canonical_finance",
                        )
                    raise
                if recovery["tier"] != "T0":
                    recovery = registry.retain(
                        recovery["operation_id"],
                        after_digest=str(
                            result.get("post_apply_fingerprint")
                            or result.get("fingerprint")
                            or args.confirm_fingerprint
                        ),
                        non_target_digest=str(
                            plan.get("non_target_digest") or ""
                        ),
                    )
                result["backup"] = (
                    None
                    if recovery["tier"] == "T0"
                    else {
                        "kind": "target_scoped_before_image",
                        "operation_id": recovery["operation_id"],
                        "copy_bytes": 0,
                    }
                )
                result["recovery_policy"] = recovery
    elif args.command == "business-approved-backfill":
        parser.error(
            "business-approved-backfill is permanently revoked; use canonical-cost-backfill and a new human approval"
        )
    elif args.command == "repair-derived-orphans":
        result = block.repair_orphan_derived_rows()
    else:
        client = WbFinanceApiClient(
            os.environ.get("WB_API_TOKEN", ""),
            min_interval_seconds=args.min_interval_seconds,
        )
        if args.command == "backfill":
            result = block.run_backfill(
                client, today=date.fromisoformat(args.today) if args.today else None
            )
        elif args.command == "sync-week":
            result = block.sync_week(
                date.fromisoformat(args.date_from),
                date.fromisoformat(args.date_to),
                client,
            )
        else:
            outbox_recovery = block.recover_receipted_split_outbox()
            due = block.due_tick_week()
            result = (
                {"status": "no_due_week"}
                if due is None
                else block.sync_week(due[0], due[1], client)
            )
            result["storage_outbox_recovery"] = outbox_recovery
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return (
        0 if str(result.get("status")) not in {"error", "completed_with_errors"} else 1
    )


def _create_sqlite_backup(
    db_path: Path,
    backup_dir: Path,
    *,
    fingerprint: str,
    prefix: str = "wb-finance-stale-cost",
) -> dict[str, object]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    source_size = db_path.stat().st_size
    free_bytes = shutil.disk_usage(backup_dir).free
    required_free_bytes = max(source_size * 2, 16 * 1024 * 1024)
    if free_bytes < required_free_bytes:
        raise ValueError(
            f"not enough free space for coherent SQLite backup: free={free_bytes}, required={required_free_bytes}"
        )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = fingerprint.removeprefix("sha256:")[:12]
    backup_path = backup_dir / f"{prefix}-{stamp}-{suffix}.sqlite3"
    if backup_path.exists():
        raise ValueError(f"backup already exists: {backup_path}")
    source_uri = f"file:{db_path.resolve()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=60) as source:
        with sqlite3.connect(backup_path) as target:
            source.backup(target)
    backup_path.chmod(0o600)
    with sqlite3.connect(f"file:{backup_path.resolve()}?mode=ro", uri=True) as verify:
        integrity = str(verify.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity != "ok":
        backup_path.unlink(missing_ok=True)
        raise ValueError(f"backup integrity_check failed: {integrity}")
    sha256 = hashlib.sha256()
    with backup_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha256.update(chunk)
    return {
        "created": True,
        "path": str(backup_path),
        "size_bytes": backup_path.stat().st_size,
        "source_size_bytes": source_size,
        "free_space_before_bytes": free_bytes,
        "required_free_bytes": required_free_bytes,
        "sha256": f"sha256:{sha256.hexdigest()}",
        "integrity_check": integrity,
    }


def _prepare_bounded_finance_recovery(
    registry: WarehouseRecoveryRegistry,
    db_path: Path,
    plan: dict[str, object],
) -> dict[str, object]:
    canonical_recovery = plan.get("backup_recovery_plan")
    source_digest = (
        str(canonical_recovery.get("before_image_digest") or "")
        if isinstance(canonical_recovery, dict)
        else str(plan.get("target_before_digest") or "")
    )
    if not source_digest:
        raise ValueError("bounded Finance recovery source digest is missing")
    weeks = [
        (str(item["week_start"]), str(item["week_end"]))
        for item in plan.get("weeks", [])
        if isinstance(item, dict)
    ]
    if not weeks:
        raise ValueError("bounded Finance recovery requires target weeks")
    predicates = " OR ".join("(week_start=? AND week_end=?)" for _ in weeks)
    parameters: tuple[object, ...] = (
        str(plan["seller_id"]),
        *(value for week in weeks for value in week),
    )
    queries: list[BeforeImageQuery] = []
    with sqlite3.connect(
        f"file:{db_path.resolve()}?mode=ro",
        uri=True,
    ) as conn:
        conn.execute("PRAGMA query_only=ON")
        for table in (
            "wb_finance_weekly_aggregates",
            "wb_finance_weekly_cost_coverage",
            "wb_finance_weekly_reconciliation",
            "wb_finance_weekly_sku_aggregates",
            "wb_finance_weekly_sync",
        ):
            primary_key = tuple(
                str(row[1])
                for row in sorted(
                    conn.execute(f'PRAGMA table_info("{table}")').fetchall(),
                    key=lambda row: int(row[5]) if int(row[5]) > 0 else 10_000,
                )
                if int(row[5]) > 0
            )
            if not primary_key:
                raise ValueError(f"Finance recovery table has no primary key: {table}")
            queries.append(
                BeforeImageQuery(
                    table=table,
                    query=(
                        f'SELECT * FROM "{table}" '
                        f"WHERE seller_id=? AND ({predicates}) "
                        f"ORDER BY {','.join(primary_key)}"
                    ),
                    parameters=parameters,
                    key_columns=primary_key,
                )
            )
    return registry.prepare_t1_from_queries(
        mutation_kind="canonical_cost_bounded_publication",
        closure_kind="sku_date",
        plan_fingerprint=str(plan["fingerprint"]),
        scope={
            "date_from": plan["date_from"],
            "date_to": plan["date_to"],
            "weeks": [
                {"week_start": start, "week_end": end}
                for start, end in weeks
            ],
        },
        queries=queries,
        expected_after_images=[
            item.get("expected") or {}
            for item in plan.get("weeks", [])
            if isinstance(item, dict)
        ],
        source_digest=source_digest,
        non_target_digest=str(plan.get("non_target_digest") or ""),
    )


def _json_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
