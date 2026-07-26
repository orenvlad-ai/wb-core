#!/usr/bin/env python3
"""Replay exact durable supplier-cost queues without a full SQLite backup."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from typing import Any, Iterable, Iterator, Mapping
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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
from packages.application.warehouse_recovery_policy import (  # noqa: E402
    BeforeImageQuery,
    RecoveryState,
    WarehouseRecoveryRegistry,
)


CONTRACT_NAME = "warehouse_cost_queue_replay_v1"
AUDIT_TABLE = "sheet_vitrina_v1_warehouse_cost_queue_replay_audit"
AUDIT_SQLITE_LOCK_WAIT_MS = 300_000
MIN_OPERATIONAL_RESERVE_BYTES = 512 * 1024 * 1024


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--invoice-no", action="append", default=[])
    parser.add_argument("--actor", default="warehouse-cost-queue-replay")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-file", default="")
    parser.add_argument("--fingerprint", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        payload = run(args)
    except Exception as exc:
        print(
            json.dumps(
                {"status": "error", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def run(args: argparse.Namespace) -> dict[str, Any]:
    runtime = RegistryUploadDbBackedRuntime(
        runtime_dir=Path(str(args.runtime_dir)).resolve()
    )
    invoices = _invoice_numbers(args.invoice_no)
    if not args.apply:
        return build_plan(runtime, invoice_numbers=invoices)
    fingerprint = str(args.fingerprint or "").strip()
    if not fingerprint:
        raise ValueError("queue replay apply requires an exact fingerprint")
    existing = _load_audit_record(runtime, fingerprint)
    if existing is not None and existing["status"] == "complete":
        return {
            **dict(existing.get("report") or {}),
            "mode": "apply",
            "applied": False,
            "idempotent": True,
        }
    if existing is not None and existing["status"] in {"running", "failed"}:
        plan = dict(existing["plan"])
    else:
        if not str(args.plan_file or "").strip():
            raise ValueError("queue replay apply requires --plan-file")
        plan = json.loads(
            Path(str(args.plan_file)).read_text(encoding="utf-8")
        )
        if not isinstance(plan, dict):
            raise ValueError("queue replay plan must be a JSON object")
    if str(plan.get("fingerprint") or "") != fingerprint:
        raise ValueError("queue replay plan and fingerprint do not match")
    if invoices and invoices != list(
        (plan.get("scope") or {}).get("invoice_numbers") or []
    ):
        raise ValueError("queue replay invoice scope differs from reviewed plan")
    return apply_plan(
        runtime,
        plan,
        actor=str(args.actor or "warehouse-cost-queue-replay"),
    )


def build_plan(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    invoice_numbers: Iterable[str],
) -> dict[str, Any]:
    invoices = _invoice_numbers(invoice_numbers)
    if not invoices:
        raise ValueError("at least one exact invoice number is required")
    started = time.monotonic()
    with _connect_readonly(runtime.db_path) as conn:
        targets = [_target_invoice_plan(conn, invoice) for invoice in invoices]
        pending_requests = [
            dict(target["queue"])
            for target in targets
            if str((target.get("queue") or {}).get("status") or "")
            in {"queued", "running"}
        ]
        affected_nm_ids = sorted(
            {
                int(value)
                for request in pending_requests
                for value in request["affected_nm_ids"]
            }
        )
        all_target_nm_ids = sorted(
            {
                int(value)
                for target in targets
                for value in target["queue"]["affected_nm_ids"]
            }
        )
        earliest_business_date = min(
            str(target["capital"]["effective_date_from"])
            for target in targets
        )
        active_version = _active_version(conn)
        target_active_states = _active_supplier_states(
            conn,
            shipment_ids=[str(item["shipment_id"]) for item in targets],
        )
        source_identity_digest = _source_identity_digest(targets)
        non_target_queue_digest = _non_target_queue_digest(
            conn,
            queue_ids=[str(item["queue"]["queue_id"]) for item in targets],
        )
        non_target_warehouse_digest = _active_warehouse_digest(
            conn,
            excluded_nm_ids=all_target_nm_ids,
        )
        target_quantity_digest = _active_target_quantity_digest(
            conn,
            nm_ids=all_target_nm_ids,
        )
        economics_before_image_bytes = int(
            conn.execute(
                """
                SELECT COALESCE(SUM(LENGTH(plan_json)),0)
                FROM sheet_vitrina_v1_ready_snapshots
                WHERE EXISTS(
                    SELECT 1 FROM json_each(plan_json,'$.date_columns') day
                    WHERE CAST(day.value AS TEXT)>=?
                )
                """,
                (earliest_business_date,),
            ).fetchone()[0]
        )
        query_only = int(conn.execute("PRAGMA query_only").fetchone()[0])
        total_changes = int(conn.total_changes)
    operational_reserve_bytes = max(
        MIN_OPERATIONAL_RESERVE_BYTES,
        economics_before_image_bytes * 2,
    )
    capacity_requirement = {
        "target_scoped_undo_estimate_bytes": economics_before_image_bytes * 2,
        "audit_estimate_bytes": max(1024 * 1024, len(_json(targets)) * 4),
        "operational_reserve_bytes": operational_reserve_bytes,
    }
    capacity_requirement["required_free_bytes"] = sum(
        int(value) for value in capacity_requirement.values()
    )
    available_free_bytes = shutil.disk_usage(runtime.runtime_dir).free
    material = {
        "contract_name": CONTRACT_NAME,
        "scope": {
            "invoice_numbers": invoices,
            "shipment_ids": [str(item["shipment_id"]) for item in targets],
            "affected_nm_ids": affected_nm_ids,
            "all_target_nm_ids": all_target_nm_ids,
            "earliest_business_date": earliest_business_date,
            "stable_source_ids": [
                str(item["queue"]["stable_source_id"]) for item in targets
            ],
        },
        "targets": targets,
        "targeted_recalc_requests": pending_requests,
        "active_version_before": active_version,
        "active_supplier_states_before": target_active_states,
        "source_identity_digest": source_identity_digest,
        "non_target_queue_digest": non_target_queue_digest,
        "non_target_warehouse_digest": non_target_warehouse_digest,
        "target_quantity_digest": target_quantity_digest,
        "capacity_requirement": capacity_requirement,
        "would_change": bool(pending_requests),
    }
    fingerprint = _fingerprint(material)
    required_free_bytes = int(capacity_requirement["required_free_bytes"])
    return {
        **material,
        "status": "dry_run_ready",
        "mode": "dry-run",
        "fingerprint": fingerprint,
        "capacity": {
            **capacity_requirement,
            "available_free_bytes": int(available_free_bytes),
            "shortfall_bytes": max(
                0,
                required_free_bytes - int(available_free_bytes),
            ),
            "sufficient": int(available_free_bytes) >= required_free_bytes,
        },
        "performance": {
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "copy_bytes": 0,
            "full_database_copy": False,
            "full_database_integrity_scan": False,
            "finance_raw_rows_read": 0,
            "query_only": query_only,
            "sqlite_total_changes": total_changes,
            "complexity": "O(exact shipments + exact queues + operational warehouse publication)",
        },
    }


def apply_plan(
    runtime: RegistryUploadDbBackedRuntime,
    plan: Mapping[str, Any],
    *,
    actor: str,
) -> dict[str, Any]:
    normalized = json.loads(json.dumps(dict(plan), ensure_ascii=False))
    fingerprint = str(normalized.get("fingerprint") or "")
    material = _fingerprint_material(normalized)
    if not fingerprint or fingerprint != _fingerprint(material):
        raise ValueError("queue replay exact reviewed fingerprint is invalid")
    if not normalized.get("would_change"):
        recovery = WarehouseRecoveryRegistry(
            runtime_dir=runtime.runtime_dir,
            db_path=runtime.db_path,
        ).plan_noop(
            mutation_kind="supplier_cost_queue_replay",
            closure_kind="shipment",
            plan_fingerprint=fingerprint,
            scope=dict(normalized.get("scope") or {}),
        )
        return {
            **normalized,
            "mode": "apply",
            "applied": False,
            "idempotent": True,
            "recovery_policy": recovery,
        }
    free_bytes = shutil.disk_usage(runtime.runtime_dir).free
    required_free_bytes = int(
        (normalized.get("capacity_requirement") or {}).get(
            "required_free_bytes"
        )
        or 0
    )
    if free_bytes < required_free_bytes:
        raise ValueError(
            "insufficient queue replay headroom: "
            f"required_free_bytes={required_free_bytes}, "
            f"available_free_bytes={free_bytes}, "
            f"shortfall_bytes={required_free_bytes - free_bytes}"
        )
    _ensure_audit_schema(runtime)
    existing = _load_audit_record(runtime, fingerprint)
    steps = dict((existing or {}).get("steps") or {})
    resuming = bool(
        existing and existing.get("status") in {"running", "failed"}
    )
    _start_audit(runtime, normalized)
    started = time.monotonic()
    recovery_registry = WarehouseRecoveryRegistry(
        runtime_dir=runtime.runtime_dir,
        db_path=runtime.db_path,
    )
    recovery_operation: dict[str, Any] | None = None
    non_target_recovery_digest = _fingerprint(
        {
            "queue": normalized.get("non_target_queue_digest"),
            "warehouse": normalized.get("non_target_warehouse_digest"),
        }
    )
    try:
        with warehouse_functional_write_lock(
            runtime.runtime_dir,
            timeout_seconds=300,
        ) as lock_info:
            recovery_operation = recovery_registry.prepare_t1_from_queries(
                mutation_kind="supplier_cost_queue_replay",
                closure_kind="shipment",
                plan_fingerprint=fingerprint,
                scope=dict(normalized.get("scope") or {}),
                queries=_recovery_before_image_queries(normalized),
                source_digest=str(
                    normalized.get("source_identity_digest") or ""
                ),
                non_target_digest=non_target_recovery_digest,
            )
            if (
                recovery_operation.get("lifecycle")
                == RecoveryState.VERIFIED.value
            ):
                recovery_operation = recovery_registry.begin_mutation(
                    str(recovery_operation["operation_id"]),
                    expected_source_digest=str(
                        normalized.get("source_identity_digest") or ""
                    ),
                )
            if "functional" not in steps:
                recovered_functional = (
                    _recover_functional_checkpoint(
                        runtime,
                        normalized,
                        steps.get("functional_plan") or {},
                    )
                    if resuming and steps.get("functional_plan")
                    else None
                )
                if recovered_functional is not None:
                    steps["functional"] = recovered_functional
                    _checkpoint_audit(runtime, normalized, steps)
                else:
                    fresh = build_plan(
                        runtime,
                        invoice_numbers=(normalized.get("scope") or {}).get(
                            "invoice_numbers"
                        )
                        or [],
                    )
                    if str(fresh["fingerprint"]) != fingerprint:
                        raise ValueError(
                            "queue replay sources changed after reviewed dry-run"
                        )
                    warehouse = WarehouseFunctionalBlock(runtime=runtime)
                    functional_plan = warehouse.build_targeted_recovery_plan(
                        affected_nm_ids=(normalized.get("scope") or {}).get(
                            "affected_nm_ids"
                        )
                        or [],
                        stable_source_ids=(normalized.get("scope") or {}).get(
                            "stable_source_ids"
                        )
                        or [],
                        targeted_recalc_requests=normalized.get(
                            "targeted_recalc_requests"
                        )
                        or [],
                    )
                    expected_target_states = _target_supplier_states_from_plan(
                        functional_plan,
                        shipment_ids=(normalized.get("scope") or {}).get(
                            "shipment_ids"
                        )
                        or [],
                    )
                    steps["functional_plan"] = {
                        "plan_fingerprint": str(
                            functional_plan["plan_fingerprint"]
                        ),
                        "base_active_version_id": str(
                            functional_plan.get("base_active_version_id") or ""
                        ),
                        "local_source_digest": str(
                            functional_plan.get("local_source_digest") or ""
                        ),
                        "target_scope": dict(
                            functional_plan.get("target_scope") or {}
                        ),
                        "invariants": dict(
                            functional_plan.get("invariants") or {}
                        ),
                        "expected_target_supplier_states": expected_target_states,
                    }
                    _checkpoint_audit(runtime, normalized, steps)
                    functional_result = warehouse.apply_plan(
                        functional_plan,
                        confirm_fingerprint=str(
                            functional_plan["plan_fingerprint"]
                        ),
                    )
                    steps["functional"] = {
                        **steps["functional_plan"],
                        "active_version_id": str(
                            (functional_result.get("active_version") or {}).get(
                                "version_id"
                            )
                            or ""
                        ),
                        "idempotent": bool(
                            functional_result.get("idempotent")
                        ),
                    }
                    _checkpoint_audit(runtime, normalized, steps)
            elif not resuming:
                raise ValueError("queue replay functional checkpoint is invalid")

            if "economics" not in steps:
                recovered_economics = (
                    _recover_economics_checkpoint(
                        runtime,
                        steps.get("economics_plan") or {},
                    )
                    if resuming and steps.get("economics_plan")
                    else None
                )
                if recovered_economics is not None:
                    steps["economics"] = recovered_economics
                    _checkpoint_audit(runtime, normalized, steps)
                else:
                    economics_plan = build_functional_economics_backfill_plan(
                        runtime,
                        affected_nm_ids=(normalized.get("scope") or {}).get(
                            "all_target_nm_ids"
                        )
                        or [],
                        earliest_business_date=str(
                            (normalized.get("scope") or {}).get(
                                "earliest_business_date"
                            )
                            or ""
                        ),
                    )
                    steps["economics_plan"] = {
                        "plan_fingerprint": str(
                            economics_plan["plan_fingerprint"]
                        ),
                        "changed_snapshot_count": int(
                            economics_plan.get("changed_snapshot_count") or 0
                        ),
                        "changed_cell_count": int(
                            economics_plan.get("changed_cell_count") or 0
                        ),
                        "non_target_digest": str(
                            economics_plan.get("non_target_digest") or ""
                        ),
                        "target_scope": dict(
                            economics_plan.get("target_scope") or {}
                        ),
                    }
                    _checkpoint_audit(runtime, normalized, steps)
                    economics_result = apply_functional_economics_backfill_plan(
                        runtime,
                        economics_plan,
                        confirm_fingerprint=str(
                            economics_plan["plan_fingerprint"]
                        ),
                        backup_dir=(
                            runtime.runtime_dir
                            / "backups"
                            / "targeted-economics"
                        ).resolve(),
                        target_scoped_undo=True,
                    )
                    steps["economics"] = {
                        **steps["economics_plan"],
                        "database_written": bool(
                            economics_result.get("database_written")
                        ),
                        "idempotent": bool(
                            economics_result.get("idempotent")
                        ),
                        "undo_manifest_digest": str(
                            economics_result.get(
                                "rollback_manifest_digest"
                            )
                            or ""
                        ),
                    }
                    _checkpoint_audit(runtime, normalized, steps)

            after = _post_apply_readback(runtime, normalized, steps)
            report = {
                "contract_name": CONTRACT_NAME,
                "plan_fingerprint": fingerprint,
                "status": "complete",
                "applied_at": _now(),
                "actor": actor,
                "lock_wait_ms": int(lock_info["wait_ms"]),
                "steps": steps,
                "before": {
                    "active_version": normalized.get(
                        "active_version_before"
                    ),
                    "targets": normalized.get("targets"),
                },
                "after": after,
                "performance": {
                    "elapsed_ms": round(
                        (time.monotonic() - started) * 1000,
                        3,
                    ),
                    "copy_bytes": 0,
                    "full_database_copy": False,
                    "finance_raw_rows_read": 0,
                },
                "second_run": {
                    "would_change": False,
                    "idempotent": True,
                },
            }
            _save_audit(runtime, report)
            recovery_operation = recovery_registry.retain(
                str(recovery_operation["operation_id"]),
                after_digest=_fingerprint(after),
                non_target_digest=non_target_recovery_digest,
                timer_state="unchanged",
            )
            report["recovery_policy"] = recovery_operation
    except Exception as exc:
        _mark_audit_failed(runtime, normalized, steps, exc)
        if recovery_operation is not None:
            recovery_registry.fail_recoverable(
                str(recovery_operation["operation_id"]),
                error=str(exc),
                next_action="resume_exact_supplier_cost_queue_replay",
            )
        raise
    return {
        **report,
        "mode": "apply",
        "applied": True,
        "idempotent": False,
    }


def _recovery_before_image_queries(
    plan: Mapping[str, Any],
) -> list[BeforeImageQuery]:
    scope = dict(plan.get("scope") or {})
    queue_ids = sorted(
        {
            str(item["queue"]["queue_id"])
            for item in plan.get("targets") or []
            if str((item.get("queue") or {}).get("queue_id") or "")
        }
    )
    nm_ids = sorted(
        {int(value) for value in scope.get("all_target_nm_ids") or []}
    )
    shipment_ids = sorted(
        {str(value) for value in scope.get("shipment_ids") or [] if str(value)}
    )
    earliest_date = str(scope.get("earliest_business_date") or "")[:10]
    queries = [
        BeforeImageQuery(
            table="sheet_vitrina_v1_warehouse_functional_active",
            query=(
                "SELECT * FROM sheet_vitrina_v1_warehouse_functional_active "
                "WHERE slot=1"
            ),
            key_columns=("slot",),
        )
    ]
    if queue_ids:
        placeholders = ",".join("?" for _ in queue_ids)
        queries.append(
            BeforeImageQuery(
                table="sheet_vitrina_v1_warehouse_targeted_recalc_queue",
                query=(
                    "SELECT * FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue "
                    f"WHERE queue_id IN ({placeholders}) ORDER BY queue_id"
                ),
                parameters=tuple(queue_ids),
                key_columns=("queue_id",),
            )
        )
    if nm_ids:
        placeholders = ",".join("?" for _ in nm_ids)
        queries.extend(
            [
                BeforeImageQuery(
                    table="sheet_vitrina_v1_warehouse_functional_balances",
                    query=(
                        "SELECT balance.* "
                        "FROM sheet_vitrina_v1_warehouse_functional_active active "
                        "JOIN sheet_vitrina_v1_warehouse_functional_balances balance "
                        "ON balance.version_id=active.version_id "
                        f"WHERE active.slot=1 AND balance.nm_id IN ({placeholders}) "
                        "ORDER BY balance.version_id,balance.warehouse_key,balance.nm_id"
                    ),
                    parameters=tuple(nm_ids),
                    key_columns=("version_id", "warehouse_key", "nm_id"),
                ),
                BeforeImageQuery(
                    table="sheet_vitrina_v1_warehouse_functional_document_lines",
                    query=(
                        "SELECT line.* "
                        "FROM sheet_vitrina_v1_warehouse_functional_active active "
                        "JOIN sheet_vitrina_v1_warehouse_functional_document_lines line "
                        "ON line.version_id=active.version_id "
                        f"WHERE active.slot=1 AND line.nm_id IN ({placeholders}) "
                        "ORDER BY line.line_id"
                    ),
                    parameters=tuple(nm_ids),
                    key_columns=("line_id",),
                ),
            ]
        )
    if shipment_ids:
        placeholders = ",".join("?" for _ in shipment_ids)
        queries.append(
            BeforeImageQuery(
                table="sheet_vitrina_v1_warehouse_supplier_cost_states",
                query=(
                    "SELECT state.* "
                    "FROM sheet_vitrina_v1_warehouse_functional_active active "
                    "JOIN sheet_vitrina_v1_warehouse_supplier_cost_states state "
                    "ON state.version_id=active.version_id "
                    f"WHERE active.slot=1 AND state.shipment_id IN ({placeholders}) "
                    "ORDER BY state.version_id,state.shipment_id"
                ),
                parameters=tuple(shipment_ids),
                key_columns=("version_id", "shipment_id"),
            )
        )
    if earliest_date:
        queries.append(
            BeforeImageQuery(
                table="sheet_vitrina_v1_ready_snapshots",
                query=(
                    "SELECT snapshot.* FROM sheet_vitrina_v1_ready_snapshots snapshot "
                    "WHERE EXISTS("
                    "SELECT 1 FROM json_each(snapshot.plan_json,'$.date_columns') day "
                    "WHERE CAST(day.value AS TEXT)>=?"
                    ") ORDER BY snapshot.bundle_version,snapshot.as_of_date"
                ),
                parameters=(earliest_date,),
                key_columns=("bundle_version", "as_of_date"),
            )
        )
    return queries


def _target_invoice_plan(
    conn: sqlite3.Connection,
    invoice_no: str,
) -> dict[str, Any]:
    shipments = conn.execute(
        """
        SELECT shipment_id,invoice_no,shipment_date,actual_shipment_date,
               actual_ff_acceptance_date,order_status,expenses_complete,
               updated_at
        FROM sheet_vitrina_v1_supplier_shipments
        WHERE invoice_no=? AND archived_at IS NULL
        ORDER BY shipment_id
        """,
        (invoice_no,),
    ).fetchall()
    if len(shipments) != 1:
        raise ValueError(
            f"exact invoice must identify one active shipment: {invoice_no}"
        )
    shipment = dict(shipments[0])
    shipment_id = str(shipment["shipment_id"])
    product_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT line_id,sort_order,internal_nm_id,qty,unit_price,amount,
                   currency,match_status,raw_json
            FROM sheet_vitrina_v1_supplier_shipment_lines
            WHERE shipment_id=? AND line_type='product'
            ORDER BY sort_order,line_id
            """,
            (shipment_id,),
        )
    ]
    product_nm_ids = sorted(
        {
            int(row["internal_nm_id"])
            for row in product_rows
            if int(row.get("internal_nm_id") or 0) > 0
        }
    )
    if not product_nm_ids:
        raise ValueError(f"invoice has no exact matched product nmIDs: {invoice_no}")
    documents = conn.execute(
        """
        SELECT document_id,document_type,file_sha256,parse_status,
               document_date,total_amount_rub,updated_at
        FROM sheet_vitrina_v1_supplier_financial_documents
        WHERE supplier_order_id=? AND document_type='bank_fee_statement'
          AND parse_status='confirmed'
        ORDER BY updated_at,document_id
        """,
        (shipment_id,),
    ).fetchall()
    if len(documents) != 1:
        raise ValueError(
            f"invoice must have one confirmed bank-fee statement: {invoice_no}"
        )
    document = dict(documents[0])
    expense_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT line_id,sort_order,category,stage,description,amount,
                   currency,amount_rub,vat_amount_rub,status,raw_json
            FROM sheet_vitrina_v1_supplier_financial_expense_lines
            WHERE supplier_order_id=? AND financial_document_id=?
            ORDER BY sort_order,line_id
            """,
            (shipment_id, str(document["document_id"])),
        )
    ]
    direct_rub_rows = [
        row
        for row in expense_rows
        if str(row.get("currency") or "").upper() == "RUB"
        and Decimal(str(row.get("amount_rub") or 0)) > 0
    ]
    expense_total = sum(
        (Decimal(str(row["amount_rub"])) for row in direct_rub_rows),
        Decimal("0"),
    )
    if not direct_rub_rows or expense_total <= 0:
        raise ValueError(
            f"confirmed commission has no positive RUB expense lines: {invoice_no}"
        )
    capital_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT event_id,effective_date,nm_id,quantity,capital_rub,
                   evidence_hash,payload_json
            FROM sheet_vitrina_v1_own_capital_events
            WHERE shipment_id=? AND event_type='cost_payment'
              AND json_extract(
                    payload_json,'$.provenance.financial_document_id'
                  )=?
            ORDER BY effective_date,nm_id,event_id
            """,
            (shipment_id, str(document["document_id"])),
        )
    ]
    capital_total = sum(
        (Decimal(str(row["capital_rub"])) for row in capital_rows),
        Decimal("0"),
    )
    if (
        not capital_rows
        or capital_total.quantize(Decimal("0.01"))
        != expense_total.quantize(Decimal("0.01"))
    ):
        raise ValueError(
            f"commission expense/capital totals do not reconcile: {invoice_no}"
        )
    queue_rows = conn.execute(
        """
        SELECT queue_id,stable_source_id,source_revision,effective_date,
               affected_nm_ids_json,status,requested_at,started_at,
               finished_at,error
        FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue
        WHERE stable_source_id=?
        ORDER BY requested_at DESC,queue_id DESC
        """,
        (f"supplier_costs:{shipment_id}",),
    ).fetchall()
    active_queue = [
        row
        for row in queue_rows
        if str(row["status"]) in {"queued", "running"}
    ]
    if len(active_queue) > 1 or (not active_queue and not queue_rows):
        raise ValueError(
            f"invoice does not have one actionable or completed cost queue: {invoice_no}"
        )
    queue_row = active_queue[0] if active_queue else queue_rows[0]
    queue = _queue_identity(queue_row)
    capital_nm_ids = sorted({int(row["nm_id"]) for row in capital_rows})
    if capital_nm_ids != queue["affected_nm_ids"]:
        raise ValueError(
            f"commission capital and queue SKU closure differ: {invoice_no}"
        )
    if not set(capital_nm_ids).issubset(product_nm_ids):
        raise ValueError(
            f"commission capital contains non-shipment nmIDs: {invoice_no}"
        )
    return {
        "invoice_no": invoice_no,
        "shipment_id": shipment_id,
        "shipment": shipment,
        "product": {
            "line_count": len(product_rows),
            "nm_ids": product_nm_ids,
            "source_revision": _fingerprint(product_rows),
        },
        "commission": {
            "document": document,
            "expense_line_count": len(direct_rub_rows),
            "expense_total_rub": _money(expense_total),
            "expense_source_revision": _fingerprint(direct_rub_rows),
        },
        "capital": {
            "event_count": len(capital_rows),
            "nm_ids": capital_nm_ids,
            "total_rub": _money(capital_total),
            "effective_date_from": min(
                str(row["effective_date"]) for row in capital_rows
            ),
            "effective_date_to": max(
                str(row["effective_date"]) for row in capital_rows
            ),
            "source_revision": _fingerprint(capital_rows),
        },
        "queue": queue,
    }


def _recover_functional_checkpoint(
    runtime: RegistryUploadDbBackedRuntime,
    plan: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
) -> dict[str, Any] | None:
    functional_fingerprint = str(
        checkpoint.get("plan_fingerprint") or ""
    )
    if not functional_fingerprint:
        return None
    with _connect_readonly(runtime.db_path) as conn:
        version = conn.execute(
            """
            SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_versions
            WHERE plan_fingerprint=?
            """,
            (functional_fingerprint,),
        ).fetchone()
        if version is None:
            return None
        queue_ids = [
            str(item["queue"]["queue_id"])
            for item in plan.get("targets") or []
        ]
        placeholders = ",".join("?" for _ in queue_ids)
        complete = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue
                WHERE queue_id IN ({placeholders}) AND status='complete'
                """,
                queue_ids,
            ).fetchone()[0]
        )
        states = _active_supplier_states(
            conn,
            shipment_ids=(plan.get("scope") or {}).get("shipment_ids") or [],
        )
    if (
        complete != len(queue_ids)
        or states
        != list(checkpoint.get("expected_target_supplier_states") or [])
    ):
        raise ValueError(
            "functional replay committed but exact recovery readback failed"
        )
    return {
        **dict(checkpoint),
        "active_version_id": str(version["version_id"]),
        "idempotent": True,
        "resumed_from_committed_checkpoint": True,
    }


def _recover_economics_checkpoint(
    runtime: RegistryUploadDbBackedRuntime,
    checkpoint: Mapping[str, Any],
) -> dict[str, Any] | None:
    fingerprint = str(checkpoint.get("plan_fingerprint") or "")
    if not fingerprint:
        return None
    with _connect_readonly(runtime.db_path) as conn:
        exists = conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table'
              AND name='sheet_vitrina_v1_functional_economics_undo_manifests'
            """
        ).fetchone()
        if not exists:
            return None
        row = conn.execute(
            """
            SELECT manifest_digest,status
            FROM sheet_vitrina_v1_functional_economics_undo_manifests
            WHERE plan_fingerprint=?
            """,
            (fingerprint,),
        ).fetchone()
    if row is None:
        return None
    if str(row["status"]) != "ready":
        raise ValueError(
            "target-scoped economics checkpoint is not in retained state"
        )
    target_scope = dict(checkpoint.get("target_scope") or {})
    readback = build_functional_economics_backfill_plan(
        runtime,
        affected_nm_ids=target_scope.get("affected_nm_ids") or [],
        earliest_business_date=str(
            target_scope.get("earliest_business_date") or ""
        ),
    )
    if int(readback.get("changed_snapshot_count") or 0) != 0:
        raise ValueError(
            "committed target-scoped economics checkpoint is not a no-op"
        )
    return {
        **dict(checkpoint),
        "database_written": True,
        "idempotent": True,
        "undo_manifest_digest": str(row["manifest_digest"]),
        "resumed_from_committed_checkpoint": True,
    }


def _post_apply_readback(
    runtime: RegistryUploadDbBackedRuntime,
    plan: Mapping[str, Any],
    steps: Mapping[str, Any],
) -> dict[str, Any]:
    scope = dict(plan.get("scope") or {})
    with _connect_readonly(runtime.db_path) as conn:
        current_targets = [
            _target_invoice_plan(conn, invoice)
            for invoice in scope.get("invoice_numbers") or []
        ]
        if _source_identity_digest(current_targets) != str(
            plan.get("source_identity_digest") or ""
        ):
            raise ValueError("queue replay business sources changed during apply")
        target_by_invoice = {
            str(item["invoice_no"]): item for item in current_targets
        }
        for original in plan.get("targets") or []:
            current = target_by_invoice.get(str(original["invoice_no"])) or {}
            if (
                str((current.get("queue") or {}).get("queue_id") or "")
                != str((original.get("queue") or {}).get("queue_id") or "")
                or str(
                    (current.get("queue") or {}).get("source_revision") or ""
                )
                != str(
                    (original.get("queue") or {}).get(
                        "source_revision"
                    )
                    or ""
                )
                or str((current.get("queue") or {}).get("status") or "")
                != "complete"
            ):
                raise ValueError(
                    "exact queue revision did not reach complete readback"
                )
        if _non_target_queue_digest(
            conn,
            queue_ids=[
                str(item["queue"]["queue_id"])
                for item in plan.get("targets") or []
            ],
        ) != str(plan.get("non_target_queue_digest") or ""):
            raise ValueError("non-target queue digest changed during replay")
        if _active_warehouse_digest(
            conn,
            excluded_nm_ids=scope.get("all_target_nm_ids") or [],
        ) != str(plan.get("non_target_warehouse_digest") or ""):
            raise ValueError("non-target warehouse digest changed during replay")
        if _active_target_quantity_digest(
            conn,
            nm_ids=scope.get("all_target_nm_ids") or [],
        ) != str(plan.get("target_quantity_digest") or ""):
            raise ValueError("target warehouse quantities changed during cost replay")
        active_version = _active_version(conn)
        states = _active_supplier_states(
            conn,
            shipment_ids=scope.get("shipment_ids") or [],
        )
        expected_states = (
            steps.get("functional")
            or steps.get("functional_plan")
            or {}
        ).get("expected_target_supplier_states") or []
        if states != expected_states:
            raise ValueError(
                "active warehouse supplier-cost states differ from reviewed publication"
            )
    return {
        "active_version": active_version,
        "targets": current_targets,
        "active_supplier_states": states,
        "non_target_queue_digest": str(
            plan.get("non_target_queue_digest") or ""
        ),
        "non_target_warehouse_digest": str(
            plan.get("non_target_warehouse_digest") or ""
        ),
        "target_quantity_digest": str(
            plan.get("target_quantity_digest") or ""
        ),
        "source_identity_digest": str(
            plan.get("source_identity_digest") or ""
        ),
    }


def _active_version(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT version.version_id,version.version_kind,version.effective_at,
               version.plan_fingerprint,version.local_source_digest,
               version.created_at
        FROM sheet_vitrina_v1_warehouse_functional_active active
        JOIN sheet_vitrina_v1_warehouse_functional_versions version
          ON version.version_id=active.version_id
        WHERE active.slot=1
        """
    ).fetchone()
    if row is None:
        raise ValueError("active warehouse functional version is unavailable")
    return dict(row)


def _active_supplier_states(
    conn: sqlite3.Connection,
    *,
    shipment_ids: Iterable[str],
) -> list[dict[str, Any]]:
    ids = sorted({str(value) for value in shipment_ids if str(value)})
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT state.shipment_id,state.source_fingerprint,
                   state.calculation_fingerprint,state.expenses_complete,
                   state.calculation_available
            FROM sheet_vitrina_v1_warehouse_functional_active active
            JOIN sheet_vitrina_v1_warehouse_supplier_cost_states state
              ON state.version_id=active.version_id
            WHERE active.slot=1
              AND state.shipment_id IN ({placeholders})
            ORDER BY state.shipment_id
            """,
            ids,
        )
    ]


def _target_supplier_states_from_plan(
    plan: Mapping[str, Any],
    *,
    shipment_ids: Iterable[str],
) -> list[dict[str, Any]]:
    ids = {str(value) for value in shipment_ids if str(value)}
    return sorted(
        (
            {
                "shipment_id": str(item["shipment_id"]),
                "source_fingerprint": str(item["source_fingerprint"]),
                "calculation_fingerprint": str(
                    item["calculation_fingerprint"]
                ),
                "expenses_complete": int(
                    bool(item["expenses_complete"])
                ),
                "calculation_available": int(
                    bool(item["calculation_available"])
                ),
            }
            for item in plan.get("supplier_cost_states") or []
            if str(item.get("shipment_id") or "") in ids
        ),
        key=lambda item: item["shipment_id"],
    )


def _active_warehouse_digest(
    conn: sqlite3.Connection,
    *,
    excluded_nm_ids: Iterable[int],
) -> str:
    excluded = sorted({int(value) for value in excluded_nm_ids})
    predicate = ""
    params: list[Any] = []
    if excluded:
        predicate = (
            " AND balance.nm_id NOT IN ("
            + ",".join("?" for _ in excluded)
            + ")"
        )
        params.extend(excluded)
    rows = [
        list(row)
        for row in conn.execute(
            """
            SELECT balance.warehouse_key,balance.nm_id,balance.quantity,
                   balance.wac_rub,balance.capital_rub,
                   balance.cost_covered_quantity,balance.quality,
                   balance.certified,balance.wb_quantity,
                   balance.wb_in_way_to_client,balance.wb_in_way_from_client,
                   balance.provenance_json
            FROM sheet_vitrina_v1_warehouse_functional_active active
            JOIN sheet_vitrina_v1_warehouse_functional_balances balance
              ON balance.version_id=active.version_id
            WHERE active.slot=1
            """
            + predicate
            + " ORDER BY balance.warehouse_key,balance.nm_id",
            params,
        )
    ]
    return _fingerprint(rows)


def _active_target_quantity_digest(
    conn: sqlite3.Connection,
    *,
    nm_ids: Iterable[int],
) -> str:
    ids = sorted({int(value) for value in nm_ids})
    if not ids:
        return _fingerprint([])
    placeholders = ",".join("?" for _ in ids)
    rows = [
        list(row)
        for row in conn.execute(
            f"""
            SELECT balance.warehouse_key,balance.nm_id,balance.quantity
            FROM sheet_vitrina_v1_warehouse_functional_active active
            JOIN sheet_vitrina_v1_warehouse_functional_balances balance
              ON balance.version_id=active.version_id
            WHERE active.slot=1 AND balance.nm_id IN ({placeholders})
            ORDER BY balance.warehouse_key,balance.nm_id
            """,
            ids,
        )
    ]
    return _fingerprint(rows)


def _non_target_queue_digest(
    conn: sqlite3.Connection,
    *,
    queue_ids: Iterable[str],
) -> str:
    ids = sorted({str(value) for value in queue_ids if str(value)})
    predicate = ""
    params: list[Any] = []
    if ids:
        predicate = (
            " WHERE queue_id NOT IN ("
            + ",".join("?" for _ in ids)
            + ")"
        )
        params.extend(ids)
    rows = [
        list(row)
        for row in conn.execute(
            """
            SELECT queue_id,stable_source_id,source_revision,effective_date,
                   affected_nm_ids_json,status,requested_at,started_at,
                   finished_at,error
            FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue
            """
            + predicate
            + " ORDER BY queue_id",
            params,
        )
    ]
    return _fingerprint(rows)


def _queue_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    nm_ids = sorted(
        {
            int(value)
            for value in _loads(row["affected_nm_ids_json"], [])
            if int(value) > 0
        }
    )
    result = {
        "queue_id": str(row["queue_id"]),
        "stable_source_id": str(row["stable_source_id"]),
        "source_revision": str(row["source_revision"]),
        "effective_date": str(row["effective_date"])[:10],
        "affected_nm_ids": nm_ids,
        "status": str(row["status"]),
        "requested_at": str(row["requested_at"]),
        "started_at": str(row["started_at"] or ""),
        "finished_at": str(row["finished_at"] or ""),
        "error": str(row["error"] or ""),
    }
    if not all(
        (
            result["queue_id"],
            result["stable_source_id"],
            result["source_revision"],
            result["effective_date"],
            result["affected_nm_ids"],
        )
    ):
        raise ValueError("target queue identity is incomplete")
    return result


def _source_identity_digest(targets: Iterable[Mapping[str, Any]]) -> str:
    return _fingerprint(
        [
            {
                "invoice_no": item["invoice_no"],
                "shipment_id": item["shipment_id"],
                "shipment": item["shipment"],
                "product": item["product"],
                "commission": item["commission"],
                "capital": item["capital"],
                "queue": {
                    key: item["queue"][key]
                    for key in (
                        "queue_id",
                        "stable_source_id",
                        "source_revision",
                        "effective_date",
                        "affected_nm_ids",
                    )
                },
            }
            for item in targets
        ]
    )


def _fingerprint_material(plan: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "contract_name",
        "scope",
        "targets",
        "targeted_recalc_requests",
        "active_version_before",
        "active_supplier_states_before",
        "source_identity_digest",
        "non_target_queue_digest",
        "non_target_warehouse_digest",
        "target_quantity_digest",
        "capacity_requirement",
        "would_change",
    )
    return {key: plan.get(key) for key in keys}


def _connect_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        conn.close()
        raise ValueError("queue replay could not enable SQLite query_only")
    return conn


@contextmanager
def _audit_write_connection(
    runtime: RegistryUploadDbBackedRuntime,
) -> Iterator[tuple[sqlite3.Connection, float]]:
    conn = sqlite3.connect(
        runtime.db_path,
        timeout=AUDIT_SQLITE_LOCK_WAIT_MS / 1000,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={AUDIT_SQLITE_LOCK_WAIT_MS}")
    started = time.monotonic()
    try:
        conn.execute("BEGIN IMMEDIATE")
        wait_ms = (time.monotonic() - started) * 1000
        try:
            yield conn, wait_ms
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def _ensure_audit_schema(
    runtime: RegistryUploadDbBackedRuntime,
) -> float:
    with _audit_write_connection(runtime) as (conn, wait_ms):
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {AUDIT_TABLE}(
                plan_fingerprint TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                applied_at TEXT,
                plan_json TEXT NOT NULL,
                steps_json TEXT NOT NULL,
                report_json TEXT NOT NULL,
                error TEXT
            )
            """
        )
    return wait_ms


def _load_audit_record(
    runtime: RegistryUploadDbBackedRuntime,
    fingerprint: str,
) -> dict[str, Any] | None:
    if not runtime.db_path.is_file():
        return None
    with _connect_readonly(runtime.db_path) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (AUDIT_TABLE,),
        ).fetchone()
        if not exists:
            return None
        row = conn.execute(
            f"SELECT * FROM {AUDIT_TABLE} WHERE plan_fingerprint=?",
            (fingerprint,),
        ).fetchone()
    if row is None:
        return None
    item = dict(row)
    return {
        "status": str(item.get("status") or ""),
        "plan": _loads(item.get("plan_json"), {}),
        "steps": _loads(item.get("steps_json"), {}),
        "report": _loads(item.get("report_json"), {}),
        "error": str(item.get("error") or ""),
    }


def _start_audit(
    runtime: RegistryUploadDbBackedRuntime,
    plan: Mapping[str, Any],
) -> float:
    now = _now()
    with _audit_write_connection(runtime) as (conn, wait_ms):
        conn.execute(
            f"""
            INSERT INTO {AUDIT_TABLE}(
                plan_fingerprint,status,started_at,updated_at,applied_at,
                plan_json,steps_json,report_json,error
            ) VALUES(?,'running',?,?,NULL,?,'{{}}','{{}}',NULL)
            ON CONFLICT(plan_fingerprint) DO UPDATE SET
                status=CASE
                    WHEN {AUDIT_TABLE}.status='complete' THEN 'complete'
                    ELSE 'running'
                END,
                updated_at=excluded.updated_at,
                error=CASE
                    WHEN {AUDIT_TABLE}.status='complete'
                    THEN {AUDIT_TABLE}.error
                    ELSE NULL
                END
            """,
            (
                str(plan["fingerprint"]),
                now,
                now,
                _json(plan),
            ),
        )
    return wait_ms


def _checkpoint_audit(
    runtime: RegistryUploadDbBackedRuntime,
    plan: Mapping[str, Any],
    steps: Mapping[str, Any],
) -> float:
    with _audit_write_connection(runtime) as (conn, wait_ms):
        updated = conn.execute(
            f"""
            UPDATE {AUDIT_TABLE}
            SET status='running',updated_at=?,steps_json=?,error=NULL
            WHERE plan_fingerprint=? AND status IN ('running','failed')
            """,
            (_now(), _json(steps), str(plan["fingerprint"])),
        )
        if int(updated.rowcount or 0) != 1:
            raise ValueError("queue replay audit checkpoint changed")
    return wait_ms


def _mark_audit_failed(
    runtime: RegistryUploadDbBackedRuntime,
    plan: Mapping[str, Any],
    steps: Mapping[str, Any],
    exc: Exception,
) -> None:
    _ensure_audit_schema(runtime)
    with _audit_write_connection(runtime) as (conn, _):
        conn.execute(
            f"""
            UPDATE {AUDIT_TABLE}
            SET status='failed',updated_at=?,steps_json=?,error=?
            WHERE plan_fingerprint=? AND status<>'complete'
            """,
            (
                _now(),
                _json(steps),
                str(exc).replace("\n", " ")[:1000],
                str(plan["fingerprint"]),
            ),
        )


def _save_audit(
    runtime: RegistryUploadDbBackedRuntime,
    report: Mapping[str, Any],
) -> float:
    with _audit_write_connection(runtime) as (conn, wait_ms):
        updated = conn.execute(
            f"""
            UPDATE {AUDIT_TABLE}
            SET status='complete',updated_at=?,applied_at=?,
                steps_json=?,report_json=?,error=NULL
            WHERE plan_fingerprint=? AND status='running'
            """,
            (
                _now(),
                str(report["applied_at"]),
                _json(report.get("steps") or {}),
                _json(report),
                str(report["plan_fingerprint"]),
            ),
        )
        if int(updated.rowcount or 0) != 1:
            raise ValueError("queue replay audit could not complete")
    return wait_ms


def _invoice_numbers(values: Iterable[str]) -> list[str]:
    normalized = [
        str(value).strip() for value in values if str(value).strip()
    ]
    result = sorted(set(normalized))
    if len(result) != len(normalized):
        raise ValueError("queue replay invoice numbers must be unique")
    return result


def _loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value)) if value not in (None, "") else fallback
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode()).hexdigest()


def _money(value: Any) -> str:
    return format(
        Decimal(str(value or 0)).quantize(Decimal("0.01")),
        "f",
    )


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


if __name__ == "__main__":
    raise SystemExit(main())
