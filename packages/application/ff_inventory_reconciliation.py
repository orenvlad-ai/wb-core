"""Audited FF inventory reconciliation with exact movement capital.

The manager workbook is a physical target, never an editable balance
snapshot.  A plan separates proven WB-supply returns from genuine inventory
receipt/write-off documents and freezes every cost basis before mutation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping

from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
)
from packages.application.simple_xlsx import read_first_sheet_rows
from packages.application.warehouse_business_projection import (
    ensure_warehouse_projection_source_outbox,
)
from packages.application.warehouse_functional import (
    ensure_warehouse_functional_schema,
)


CONTRACT_NAME = "ff_inventory_reconciliation_v1"
PLAN_VERSION = "v1"
ZERO = Decimal("0")
REQUIRED_HEADERS = ("nmId", "Комментарий SKU", "Остаток ФФ", "Дата остатка")

OPERATION_RETURN = "auto_return"
OPERATION_INVENTORY_RECEIPT = "inventory_receipt"
OPERATION_INVENTORY_WRITEOFF = "inventory_writeoff"
OPERATION_ROLLBACK = "inventory_rollback"
SOURCE_RETURN = "wb_supply_return"
SOURCE_INVENTORY = "inventory_reconciliation"


class FfInventoryReconciliationError(ValueError):
    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


class FfInventoryReconciliation:
    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        timestamp_factory: Any | None = None,
    ) -> None:
        self.runtime = runtime
        self.timestamp_factory = timestamp_factory or _now

    def build_plan(
        self,
        *,
        source_bytes: bytes,
        source_filename: str,
        business_date: str,
        return_supply_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        source_sha256 = "sha256:" + hashlib.sha256(source_bytes).hexdigest()
        target_rows = _parse_target_workbook(source_bytes, business_date=business_date)
        target_nm_ids = sorted(target_rows)
        source_key = f"{source_sha256}:{business_date}"
        with _connect(self.runtime.db_path, query_only=True) as conn:
            _require_tables(conn)
            existing = conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_ff_inventory_reconciliations
                WHERE source_sha256=? AND business_date=?
                """,
                (source_sha256, business_date),
            ).fetchone()
            before = _ff_balances(conn, target_nm_ids)
            whole_before = _ff_total(conn)
            if existing is not None and str(existing["status"] or "") == "applied":
                target_matches = all(
                    before.get(nm_id, ZERO) == target_rows[nm_id]["target_quantity"]
                    for nm_id in target_nm_ids
                ) and whole_before == sum(
                    (item["target_quantity"] for item in target_rows.values()), ZERO
                )
                return {
                    "contract_name": CONTRACT_NAME,
                    "plan_version": PLAN_VERSION,
                    "status": "already_applied" if target_matches else "applied_target_drifted",
                    "apply_allowed": False,
                    "idempotent": True,
                    "fingerprint": str(existing["plan_fingerprint"] or ""),
                    "reconciliation_id": str(existing["reconciliation_id"] or ""),
                    "source": {
                        "filename": str(existing["source_filename"] or source_filename),
                        "sha256": source_sha256,
                        "business_date": business_date,
                    },
                    "readback": {
                        "target_matches": target_matches,
                        "ff_total": _text(whole_before),
                        "target_total": _text(sum(
                            (item["target_quantity"] for item in target_rows.values()), ZERO
                        )),
                    },
                }
            if existing is not None:
                return {
                    "contract_name": CONTRACT_NAME,
                    "plan_version": PLAN_VERSION,
                    "status": str(existing["status"] or "blocked_existing_operation"),
                    "apply_allowed": False,
                    "idempotent": False,
                    "fingerprint": str(existing["plan_fingerprint"] or ""),
                    "reconciliation_id": str(existing["reconciliation_id"] or ""),
                    "source": {
                        "filename": str(existing["source_filename"] or source_filename),
                        "sha256": source_sha256,
                        "business_date": business_date,
                    },
                    "blockers": [
                        {
                            "code": "source_operation_already_terminal",
                            "status": str(existing["status"] or ""),
                            "message": "One source hash/business date cannot create a second inventory operation",
                        }
                    ],
                }

            nomenclature, nomenclature_blockers = _exact_nomenclature(
                conn, target_nm_ids
            )
            blockers: list[dict[str, Any]] = list(nomenclature_blockers)
            returns: list[dict[str, Any]] = []
            return_by_nm: dict[int, Decimal] = {}
            for supply_id in sorted({str(item).strip() for item in return_supply_ids if str(item).strip()}):
                try:
                    planned_return = _plan_supply_return(
                        conn,
                        supply_id=supply_id,
                        business_date=business_date,
                        nomenclature=nomenclature,
                    )
                except FfInventoryReconciliationError as exc:
                    blockers.append(
                        {
                            "code": exc.code,
                            "supply_id": supply_id,
                            "message": str(exc),
                            "details": exc.details,
                        }
                    )
                    continue
                returns.append(planned_return)
                for line in planned_return["lines"]:
                    nm_id = int(line["nm_id"])
                    return_by_nm[nm_id] = return_by_nm.get(nm_id, ZERO) + _decimal(
                        line["quantity_delta"]
                    )

            cost_snapshot: dict[int, dict[str, Any]] = {}
            adjustment_lines: list[dict[str, Any]] = []
            for nm_id in target_nm_ids:
                baseline_after_returns = before.get(nm_id, ZERO) + return_by_nm.get(nm_id, ZERO)
                delta = target_rows[nm_id]["target_quantity"] - baseline_after_returns
                if delta == ZERO:
                    continue
                basis = _frozen_ff_cost_basis(
                    conn,
                    nm_id=nm_id,
                    business_date=business_date,
                )
                if basis is None:
                    blockers.append(
                        {
                            "code": "inventory_cost_basis_missing",
                            "nm_id": nm_id,
                            "message": "No admissible same-SKU FF cost basis exists on or before the business date",
                        }
                    )
                    continue
                cost_snapshot[nm_id] = basis
                identity = nomenclature.get(nm_id, {})
                adjustment_lines.append(
                    _costed_line(
                        nm_id=nm_id,
                        quantity_delta=delta,
                        unit_cost=_decimal(basis["unit_cost_rub"]),
                        identity=identity,
                        quality=str(basis["quality"]),
                        provenance={
                            "basis_kind": str(basis["basis_kind"]),
                            "basis_version_id": str(basis["version_id"]),
                            "basis_business_date": str(basis["business_effective_date"]),
                            "frozen_for_business_date": business_date,
                        },
                    )
                )

            receipt_lines = [line for line in adjustment_lines if _decimal(line["quantity_delta"]) > ZERO]
            writeoff_lines = [line for line in adjustment_lines if _decimal(line["quantity_delta"]) < ZERO]
            target_total = sum(
                (item["target_quantity"] for item in target_rows.values()), ZERO
            )
            return_total = sum(
                (_decimal(line["quantity_delta"]) for item in returns for line in item["lines"]),
                ZERO,
            )
            adjustment_delta = sum(
                (_decimal(line["quantity_delta"]) for line in adjustment_lines), ZERO
            )
            projected_total = whole_before + return_total + adjustment_delta
            if projected_total != target_total:
                blockers.append(
                    {
                        "code": "projected_total_mismatch",
                        "before": _text(whole_before),
                        "return_delta": _text(return_total),
                        "inventory_delta": _text(adjustment_delta),
                        "projected": _text(projected_total),
                        "target": _text(target_total),
                    }
                )
            per_sku: list[dict[str, Any]] = []
            return_lines_by_nm: dict[int, list[dict[str, Any]]] = {}
            for return_document in returns:
                for return_line in return_document["lines"]:
                    return_lines_by_nm.setdefault(
                        int(return_line["nm_id"]), []
                    ).append(dict(return_line))
            adjustment_by_nm = {int(line["nm_id"]): line for line in adjustment_lines}
            for nm_id in target_nm_ids:
                item = target_rows[nm_id]
                return_lines = return_lines_by_nm.get(nm_id, [])
                adjustment = adjustment_by_nm.get(nm_id)
                return_quantity_for_sku = sum(
                    (_decimal(line.get("quantity_delta")) for line in return_lines),
                    ZERO,
                )
                return_capital_for_sku = sum(
                    (_decimal(line.get("capital_delta_rub")) for line in return_lines),
                    ZERO,
                )
                return_unit_costs = {
                    str(line.get("unit_cost_rub") or "") for line in return_lines
                }
                per_sku.append(
                    {
                        "nm_id": nm_id,
                        "sku_comment": item["sku_comment"],
                        "before_quantity": _text(before.get(nm_id, ZERO)),
                        "return_quantity": _text(return_quantity_for_sku),
                        "inventory_delta": _text(_decimal((adjustment or {}).get("quantity_delta"))),
                        "target_quantity": _text(item["target_quantity"]),
                        "unit_cost_rub": (
                            (adjustment or {}).get("unit_cost_rub")
                            or (
                                next(iter(return_unit_costs))
                                if len(return_unit_costs) == 1
                                else _text(return_capital_for_sku / return_quantity_for_sku)
                                if return_quantity_for_sku > ZERO
                                else None
                            )
                        ),
                        "capital_delta_rub": _text(
                            return_capital_for_sku
                            + _decimal((adjustment or {}).get("capital_delta_rub"))
                        ),
                        "cost_basis": (
                            cost_snapshot.get(nm_id)
                            or (
                                (return_lines[0] or {}).get("cost_provenance")
                                if len(return_lines) == 1
                                else {
                                    "basis_kind": "multiple_exact_original_ff_debits",
                                    "line_count": len(return_lines),
                                    "sources": [
                                        dict(line.get("cost_provenance") or {})
                                        for line in return_lines
                                    ],
                                }
                                if return_lines
                                else None
                            )
                        ),
                        "return_cost_lines": [
                            {
                                "unit_cost_rub": line.get("unit_cost_rub"),
                                "quantity_delta": line.get("quantity_delta"),
                                "capital_delta_rub": line.get("capital_delta_rub"),
                                "cost_provenance": dict(line.get("cost_provenance") or {}),
                            }
                            for line in return_lines
                        ],
                    }
                )
            source_revisions = {
                "active_functional_version": _active_version_guard(conn),
                "nomenclature_digest": _digest(nomenclature),
                "return_proofs": [item["proof"] for item in returns],
                "cost_snapshot_digest": _digest(cost_snapshot),
            }
            non_target_digest = _non_target_digest(conn, target_nm_ids)
            relevant_ledger_digest = _relevant_ledger_digest(conn, target_nm_ids)

        documents: list[dict[str, Any]] = []
        for item in returns:
            documents.append(
                {
                    "operation_type": OPERATION_RETURN,
                    "source_type": SOURCE_RETURN,
                    "source_key": item["source_key"],
                    "source_object_id": item["supply_id"],
                    "source_object_label": f"Возврат WB-поставки {item['supply_id']}",
                    "lines": item["lines"],
                    "diagnostics": item["proof"],
                }
            )
        if receipt_lines:
            documents.append(
                {
                    "operation_type": OPERATION_INVENTORY_RECEIPT,
                    "source_type": SOURCE_INVENTORY,
                    "source_key": f"ff_inventory_receipt:{source_key}",
                    "source_object_id": source_sha256,
                    "source_object_label": f"Инвентаризация FF {business_date}: излишки",
                    "lines": receipt_lines,
                    "diagnostics": {"reason": "manager_physical_inventory_target"},
                }
            )
        if writeoff_lines:
            documents.append(
                {
                    "operation_type": OPERATION_INVENTORY_WRITEOFF,
                    "source_type": SOURCE_INVENTORY,
                    "source_key": f"ff_inventory_writeoff:{source_key}",
                    "source_object_id": source_sha256,
                    "source_object_label": f"Инвентаризация FF {business_date}: недостачи",
                    "lines": writeoff_lines,
                    "diagnostics": {"reason": "manager_physical_inventory_target"},
                }
            )
        operation_ids = [
            "ffso_inv_" + hashlib.sha256(item["source_key"].encode("utf-8")).hexdigest()[:20]
            for item in documents
        ]
        for item, operation_id in zip(documents, operation_ids, strict=True):
            item["operation_id"] = operation_id
        manifest = {
            "plan_version": PLAN_VERSION,
            "source": {
                "filename": source_filename,
                "sha256": source_sha256,
                "business_date": business_date,
                "row_count": len(target_rows),
            },
            "before_total": _text(whole_before),
            "target_total": _text(target_total),
            "return_quantity": _text(return_total),
            "inventory_quantity_delta": _text(adjustment_delta),
            "return_capital_delta_rub": _text(sum(
                (_decimal(line["capital_delta_rub"]) for item in returns for line in item["lines"]), ZERO
            )),
            "inventory_capital_delta_rub": _text(sum(
                (_decimal(line["capital_delta_rub"]) for line in adjustment_lines), ZERO
            )),
            "documents": documents,
            "per_sku": per_sku,
            "source_revisions": source_revisions,
            "relevant_ledger_digest": relevant_ledger_digest,
            "non_target_digest": non_target_digest,
            "expected_operation_ids": operation_ids,
            "invariants": {
                "target_sku_count": len(target_rows),
                "unmatched_or_ambiguous_count": len(nomenclature_blockers),
                "negative_target_count": sum(
                    1 for item in target_rows.values() if item["target_quantity"] < ZERO
                ),
                "missing_cost_basis_count": sum(
                    1 for item in blockers if item.get("code") == "inventory_cost_basis_missing"
                ),
                "post_apply_target_exact": True,
                "repeat_apply_t0_noop": True,
            },
        }
        fingerprint = "sha256:" + _digest(manifest)
        return {
            "contract_name": CONTRACT_NAME,
            "plan_version": PLAN_VERSION,
            "status": "ready" if not blockers else "blocked",
            "apply_allowed": not blockers,
            "idempotent": False,
            "fingerprint": fingerprint,
            "manifest": manifest,
            "blockers": blockers,
            "human_summary": (
                f"FF {whole_before} → {target_total}; возврат {return_total}; "
                f"инвентаризационная дельта {adjustment_delta}; документов {len(documents)}"
            ),
        }

    def apply_plan(
        self,
        *,
        source_bytes: bytes,
        source_filename: str,
        business_date: str,
        return_supply_ids: Iterable[str],
        confirmation_fingerprint: str,
        approval_reference: str,
        created_by: str,
    ) -> dict[str, Any]:
        if not approval_reference.strip():
            raise FfInventoryReconciliationError(
                "approval_reference_required",
                "An exact production-mutation approval reference is required",
            )
        plan = self.build_plan(
            source_bytes=source_bytes,
            source_filename=source_filename,
            business_date=business_date,
            return_supply_ids=return_supply_ids,
        )
        if plan.get("idempotent"):
            if confirmation_fingerprint != str(plan.get("fingerprint") or ""):
                raise FfInventoryReconciliationError(
                    "stale_or_invalid_fingerprint",
                    "Confirmation fingerprint does not match the existing operation",
                )
            if str(plan.get("status") or "") != "already_applied":
                raise FfInventoryReconciliationError(
                    "applied_target_drifted",
                    "The prior reconciliation exists but its exact FF target no longer holds",
                    details=plan.get("readback") or {},
                )
            return {"status": "already_applied", "idempotent": True, "plan": plan}
        if confirmation_fingerprint != str(plan.get("fingerprint") or ""):
            raise FfInventoryReconciliationError(
                "stale_or_invalid_fingerprint",
                "Confirmation fingerprint does not match the fresh dry-run",
            )
        if not plan.get("apply_allowed"):
            raise FfInventoryReconciliationError(
                "plan_blocked",
                "The current dry-run is blocked",
                details=plan.get("blockers") or [],
            )
        manifest = dict(plan["manifest"])
        source = dict(manifest["source"])
        reconciliation_id = "ffir_" + confirmation_fingerprint.split(":", 1)[-1][:24]
        now = _parse_timestamp(self.timestamp_factory())
        with _connect(self.runtime.db_path) as conn:
            ensure_warehouse_functional_schema(conn)
            ensure_inventory_reconciliation_schema(conn)
            ensure_warehouse_projection_source_outbox(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    """
                    SELECT * FROM sheet_vitrina_v1_ff_inventory_reconciliations
                    WHERE source_sha256=? AND business_date=?
                    """,
                    (source["sha256"], business_date),
                ).fetchone()
                if existing is not None and str(existing["status"] or "") == "applied":
                    existing_manifest = _loads(existing["manifest_json"], {})
                    repeated_readback = _target_readback(
                        conn,
                        per_sku=existing_manifest.get("per_sku") or [],
                        target_total=_decimal(existing_manifest.get("target_total")),
                    )
                    if not repeated_readback["target_matches"]:
                        raise FfInventoryReconciliationError(
                            "applied_target_drifted",
                            "The prior reconciliation exists but its exact FF target no longer holds",
                            details=repeated_readback,
                        )
                    conn.rollback()
                    return {
                        "status": "already_applied",
                        "idempotent": True,
                        "reconciliation_id": str(existing["reconciliation_id"]),
                        "fingerprint": str(existing["plan_fingerprint"]),
                        "readback": repeated_readback,
                    }
                target_nm_ids = [int(item["nm_id"]) for item in manifest["per_sku"]]
                if _relevant_ledger_digest(conn, target_nm_ids) != manifest["relevant_ledger_digest"]:
                    raise FfInventoryReconciliationError(
                        "relevant_ledger_changed",
                        "FF ledger changed after dry-run",
                    )
                if _non_target_digest(conn, target_nm_ids) != manifest["non_target_digest"]:
                    raise FfInventoryReconciliationError(
                        "non_target_digest_changed",
                        "Non-target production state changed after dry-run",
                    )
                if _active_version_guard(conn) != manifest["source_revisions"]["active_functional_version"]:
                    raise FfInventoryReconciliationError(
                        "active_functional_version_changed",
                        "The frozen cost source version changed after dry-run",
                    )
                locked_nomenclature, locked_nomenclature_blockers = _exact_nomenclature(
                    conn,
                    target_nm_ids,
                )
                if (
                    locked_nomenclature_blockers
                    or _digest(locked_nomenclature)
                    != str(manifest["source_revisions"]["nomenclature_digest"])
                ):
                    raise FfInventoryReconciliationError(
                        "nomenclature_changed",
                        "Exact SKU/nmID/barcode identity changed after dry-run",
                        details=locked_nomenclature_blockers,
                    )
                locked_cost_snapshot: dict[int, dict[str, Any]] = {}
                for document in manifest["documents"]:
                    if str(document.get("source_type") or "") != SOURCE_INVENTORY:
                        continue
                    for line in document.get("lines") or []:
                        nm_id = int(line["nm_id"])
                        basis = _frozen_ff_cost_basis(
                            conn,
                            nm_id=nm_id,
                            business_date=business_date,
                        )
                        if basis is None:
                            raise FfInventoryReconciliationError(
                                "inventory_cost_basis_changed",
                                f"Frozen FF cost basis disappeared for nmID {nm_id}",
                            )
                        locked_cost_snapshot[nm_id] = basis
                if _digest(locked_cost_snapshot) != str(
                    manifest["source_revisions"]["cost_snapshot_digest"]
                ):
                    raise FfInventoryReconciliationError(
                        "inventory_cost_basis_changed",
                        "Frozen FF cost basis changed after dry-run",
                    )
                for document in manifest["documents"]:
                    if str(document.get("source_type") or "") != SOURCE_RETURN:
                        continue
                    fresh_return = _plan_supply_return(
                        conn,
                        supply_id=str(document.get("source_object_id") or ""),
                        business_date=business_date,
                        nomenclature=locked_nomenclature,
                    )
                    reviewed_return = {
                        "source_key": document.get("source_key"),
                        "lines": document.get("lines") or [],
                        "proof": document.get("diagnostics") or {},
                    }
                    if _digest(fresh_return) != _digest(
                        {
                            "supply_id": str(document.get("source_object_id") or ""),
                            **reviewed_return,
                        }
                    ):
                        raise FfInventoryReconciliationError(
                            "wb_supply_return_proof_changed",
                            "Confirmed WB-supply return evidence changed after dry-run",
                            details={"supply_id": document.get("source_object_id")},
                        )
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_ff_inventory_reconciliations(
                        reconciliation_id,source_sha256,source_filename,source_content_type,
                        source_file_blob,business_date,plan_fingerprint,manifest_json,
                        approval_reference,created_by,created_at,status,operation_ids_json,
                        before_digest,non_target_digest,after_digest,reconciliation_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        reconciliation_id,
                        source["sha256"],
                        source_filename,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        sqlite3.Binary(source_bytes),
                        business_date,
                        confirmation_fingerprint,
                        _json(manifest),
                        approval_reference.strip(),
                        created_by.strip() or "operator",
                        _format_timestamp(now),
                        "applying",
                        _json(manifest["expected_operation_ids"]),
                        manifest["relevant_ledger_digest"],
                        manifest["non_target_digest"],
                        "",
                        "{}",
                    ),
                )
                inventory_source_attached = False
                for index, document in enumerate(manifest["documents"]):
                    created_at = _format_timestamp(now + timedelta(microseconds=index))
                    attach_inventory_source = (
                        document["source_type"] == SOURCE_INVENTORY
                        and not inventory_source_attached
                    )
                    _insert_operation(
                        conn,
                        document=document,
                        created_at=created_at,
                        business_date=business_date,
                        created_by=created_by,
                        source_filename=source_filename if attach_inventory_source else "",
                        source_sha256=source["sha256"] if attach_inventory_source else "",
                        source_bytes=source_bytes if attach_inventory_source else None,
                        reconciliation_id=reconciliation_id,
                        plan_fingerprint=confirmation_fingerprint,
                        approval_reference=approval_reference,
                    )
                    if document["source_type"] == SOURCE_RETURN:
                        proof = dict(document.get("diagnostics") or {})
                        updated = conn.execute(
                            """
                            UPDATE sheet_vitrina_v1_ff_stock_wb_supply_lifecycle
                            SET lifecycle_state='returned',original_debit_operation_id=?,
                                return_operation_id=?,return_source_revision=?,updated_at=?
                            WHERE supply_id=? AND return_operation_id=''
                              AND lifecycle_state IN ('missing_confirmed','cancelled_confirmed')
                            """,
                            (
                                str(proof.get("original_operation_id") or ""),
                                str(document["operation_id"]),
                                str(
                                    proof.get("return_source_revision")
                                    or proof.get("proof_revision")
                                    or ""
                                ),
                                created_at,
                                str(document.get("source_object_id") or ""),
                            ),
                        ).rowcount
                        if updated != 1:
                            raise FfInventoryReconciliationError(
                                "wb_supply_return_lifecycle_changed",
                                "Confirmed WB-supply return lifecycle changed during apply",
                                details={
                                    "supply_id": document.get("source_object_id"),
                                    "updated_rows": updated,
                                },
                            )
                    inventory_source_attached = inventory_source_attached or attach_inventory_source
                readback = _target_readback(
                    conn,
                    per_sku=manifest["per_sku"],
                    target_total=_decimal(manifest["target_total"]),
                )
                if not readback["target_matches"]:
                    raise FfInventoryReconciliationError(
                        "post_apply_target_mismatch",
                        "Atomic post-write FF target readback failed",
                        details=readback,
                    )
                if _non_target_digest(conn, target_nm_ids) != manifest["non_target_digest"]:
                    raise FfInventoryReconciliationError(
                        "post_apply_non_target_digest_changed",
                        "Non-target invariant changed inside the transaction",
                    )
                after_digest = _relevant_ledger_digest(conn, target_nm_ids)
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_ff_inventory_reconciliations
                    SET status='applied',after_digest=?,reconciliation_json=?
                    WHERE reconciliation_id=?
                    """,
                    (after_digest, _json(readback), reconciliation_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            "contract_name": CONTRACT_NAME,
            "status": "applied",
            "idempotent": False,
            "reconciliation_id": reconciliation_id,
            "fingerprint": confirmation_fingerprint,
            "operation_ids": list(manifest["expected_operation_ids"]),
            "readback": readback,
            "source_sha256": source["sha256"],
            "non_target_digest": manifest["non_target_digest"],
            "after_digest": after_digest,
            "capital": {
                "return_delta_rub": manifest["return_capital_delta_rub"],
                "inventory_delta_rub": manifest["inventory_capital_delta_rub"],
            },
        }

    def readback(
        self,
        *,
        source_sha256: str,
        business_date: str,
    ) -> dict[str, Any]:
        with _connect(self.runtime.db_path, query_only=True) as conn:
            _require_tables(conn)
            row = conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_ff_inventory_reconciliations
                WHERE source_sha256=? AND business_date=?
                """,
                (source_sha256, business_date),
            ).fetchone()
            if row is None:
                return {"contract_name": CONTRACT_NAME, "status": "not_found"}
            manifest = _loads(row["manifest_json"], {})
            target = _target_readback(
                conn,
                per_sku=manifest.get("per_sku") or [],
                target_total=_decimal(manifest.get("target_total")),
            )
            operation_ids = _loads(row["operation_ids_json"], [])
            operations = [
                dict(item)
                for item in conn.execute(
                    f"SELECT operation_id,operation_type,source_type,source_key,created_at,business_effective_date,sku_count,total_quantity_delta,total_quantity_abs,source_file_sha256 FROM sheet_vitrina_v1_ff_stock_operations WHERE operation_id IN ({','.join('?' for _ in operation_ids)}) ORDER BY created_at,operation_id",
                    tuple(operation_ids),
                ).fetchall()
            ] if operation_ids else []
            return {
                "contract_name": CONTRACT_NAME,
                "status": str(row["status"]),
                "reconciliation_id": str(row["reconciliation_id"]),
                "fingerprint": str(row["plan_fingerprint"]),
                "source_sha256": str(row["source_sha256"]),
                "business_date": str(row["business_date"]),
                "approval_reference": str(row["approval_reference"]),
                "operations": operations,
                "target_readback": target,
                "non_target_digest_matches": (
                    _non_target_digest(
                        conn,
                        [int(item["nm_id"]) for item in manifest.get("per_sku") or []],
                    )
                    == str(row["non_target_digest"])
                ),
                "audit": _loads(row["reconciliation_json"], {}),
            }

    def rollback(
        self,
        *,
        confirmation_fingerprint: str,
        approval_reference: str,
        reason: str,
        created_by: str,
    ) -> dict[str, Any]:
        """Append exact compensating movements; immutable source history is retained."""

        if not approval_reference.strip() or not reason.strip():
            raise FfInventoryReconciliationError(
                "rollback_authority_required",
                "Rollback requires an approval reference and a non-empty reason",
            )
        now = _parse_timestamp(self.timestamp_factory())
        with _connect(self.runtime.db_path) as conn:
            ensure_warehouse_functional_schema(conn)
            ensure_inventory_reconciliation_schema(conn)
            ensure_warehouse_projection_source_outbox(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT * FROM sheet_vitrina_v1_ff_inventory_reconciliations
                    WHERE plan_fingerprint=?
                    """,
                    (confirmation_fingerprint,),
                ).fetchone()
                if row is None:
                    raise FfInventoryReconciliationError(
                        "rollback_reconciliation_missing",
                        "No FF reconciliation owns the provided fingerprint",
                    )
                if str(row["status"] or "") == "rolled_back":
                    conn.rollback()
                    return {
                        "contract_name": CONTRACT_NAME,
                        "status": "already_rolled_back",
                        "idempotent": True,
                        "reconciliation_id": str(row["reconciliation_id"]),
                        "fingerprint": confirmation_fingerprint,
                        "audit": _loads(row["reconciliation_json"], {}),
                    }
                if str(row["status"] or "") != "applied":
                    raise FfInventoryReconciliationError(
                        "rollback_state_invalid",
                        f"Reconciliation state {row['status']} cannot be rolled back",
                    )
                manifest = _loads(row["manifest_json"], {})
                target_nm_ids = [int(item["nm_id"]) for item in manifest.get("per_sku") or []]
                if _relevant_ledger_digest(conn, target_nm_ids) != str(row["after_digest"]):
                    raise FfInventoryReconciliationError(
                        "rollback_scope_changed",
                        "Target FF ledger changed after apply; bounded T1 compensation is unsafe",
                    )
                if _non_target_digest(conn, target_nm_ids) != str(row["non_target_digest"]):
                    raise FfInventoryReconciliationError(
                        "rollback_non_target_changed",
                        "Non-target invariant changed after apply",
                    )
                original_ids = list(_loads(row["operation_ids_json"], []))
                rollback_ids: list[str] = []
                for index, original_id in enumerate(original_ids):
                    original = conn.execute(
                        "SELECT * FROM sheet_vitrina_v1_ff_stock_operations WHERE operation_id=?",
                        (str(original_id),),
                    ).fetchone()
                    if original is None:
                        raise FfInventoryReconciliationError(
                            "rollback_original_operation_missing",
                            f"Original operation {original_id} is missing",
                        )
                    source_key = f"ff_inventory_rollback:{row['reconciliation_id']}:{original_id}"
                    rollback_id = "ffso_inv_rb_" + hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:18]
                    lines: list[dict[str, Any]] = []
                    for source_line in conn.execute(
                        """
                        SELECT * FROM sheet_vitrina_v1_ff_stock_operation_lines
                        WHERE operation_id=? ORDER BY line_no
                        """,
                        (str(original_id),),
                    ).fetchall():
                        raw = _loads(source_line["raw_json"], {})
                        snapshot = raw.get("cost_snapshot") if isinstance(raw, Mapping) else None
                        if not isinstance(snapshot, Mapping):
                            raise FfInventoryReconciliationError(
                                "rollback_cost_snapshot_missing",
                                f"Original line {original_id}:{source_line['line_no']} has no exact cost snapshot",
                            )
                        lines.append(
                            _costed_line(
                                nm_id=int(source_line["nm_id"]),
                                quantity_delta=-_decimal(source_line["quantity_delta"]),
                                unit_cost=_decimal(snapshot.get("unit_cost_rub")),
                                identity={
                                    "barcode": source_line["barcode"],
                                    "our_sku": source_line["sku"],
                                    "nomenclature_name": source_line["nomenclature_name"],
                                    "group_name": source_line["group_name"],
                                },
                                quality="exact_compensating_original_line",
                                provenance={
                                    "original_operation_id": str(original_id),
                                    "original_line_no": int(source_line["line_no"]),
                                    "original_cost_snapshot": dict(snapshot),
                                    "rollback_reason": reason.strip(),
                                },
                            )
                        )
                    document = {
                        "operation_id": rollback_id,
                        "operation_type": OPERATION_ROLLBACK,
                        "source_type": "inventory_reconciliation_rollback",
                        "source_key": source_key,
                        "source_object_id": str(original_id),
                        "source_object_label": f"Компенсация {original['source_object_label']}",
                        "lines": lines,
                        "diagnostics": {"reason": reason.strip(), "original_operation_id": str(original_id)},
                    }
                    _insert_operation(
                        conn,
                        document=document,
                        created_at=_format_timestamp(now + timedelta(microseconds=index)),
                        business_date=str(row["business_date"]),
                        created_by=created_by,
                        source_filename="",
                        source_sha256="",
                        source_bytes=None,
                        reconciliation_id=str(row["reconciliation_id"]),
                        plan_fingerprint=confirmation_fingerprint,
                        approval_reference=approval_reference,
                    )
                    if str(original["source_type"] or "") == SOURCE_RETURN:
                        reopened = conn.execute(
                            """
                            UPDATE sheet_vitrina_v1_ff_stock_wb_supply_lifecycle
                            SET lifecycle_state='rollback_pending_reobservation',
                                consecutive_missing_complete_snapshots=1,
                                last_observation_id=?,last_observation_at=?,
                                return_operation_id='',return_source_revision='',updated_at=?
                            WHERE supply_id=? AND return_operation_id=?
                            """,
                            (
                                f"rollback:{rollback_id}",
                                _format_timestamp(now),
                                _format_timestamp(now),
                                str(original["source_object_id"] or ""),
                                str(original_id),
                            ),
                        ).rowcount
                        if reopened != 1:
                            raise FfInventoryReconciliationError(
                                "rollback_return_lifecycle_changed",
                                "WB-supply return lifecycle changed before compensation",
                                details={
                                    "supply_id": str(original["source_object_id"] or ""),
                                    "return_operation_id": str(original_id),
                                    "updated_rows": reopened,
                                },
                            )
                    rollback_ids.append(rollback_id)
                expected_before = [
                    {**dict(item), "target_quantity": item.get("before_quantity")}
                    for item in manifest.get("per_sku") or []
                ]
                readback = _target_readback(
                    conn,
                    per_sku=expected_before,
                    target_total=_decimal(manifest.get("before_total")),
                )
                if not readback["target_matches"]:
                    raise FfInventoryReconciliationError(
                        "rollback_readback_mismatch",
                        "Compensating documents did not restore the exact before state",
                        details=readback,
                    )
                audit = {
                    **_loads(row["reconciliation_json"], {}),
                    "rollback": {
                        "status": "applied",
                        "operation_ids": rollback_ids,
                        "approval_reference": approval_reference,
                        "reason": reason.strip(),
                        "created_by": created_by,
                        "created_at": _format_timestamp(now),
                        "readback": readback,
                    },
                }
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_ff_inventory_reconciliations
                    SET status='rolled_back',reconciliation_json=?
                    WHERE reconciliation_id=?
                    """,
                    (_json(audit), str(row["reconciliation_id"])),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            "contract_name": CONTRACT_NAME,
            "status": "rolled_back",
            "idempotent": False,
            "reconciliation_id": str(row["reconciliation_id"]),
            "fingerprint": confirmation_fingerprint,
            "rollback_operation_ids": rollback_ids,
            "readback": readback,
        }


def ensure_inventory_reconciliation_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_ff_inventory_reconciliations(
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
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_ff_inventory_cost_bases(
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
        )
        """
    )


def _parse_target_workbook(
    source_bytes: bytes,
    *,
    business_date: str,
) -> dict[int, dict[str, Any]]:
    rows = read_first_sheet_rows(source_bytes)
    if not rows or tuple(str(item or "").strip() for item in rows[0][:4]) != REQUIRED_HEADERS:
        raise FfInventoryReconciliationError(
            "invalid_workbook_headers",
            "Manager workbook headers do not match the exact inventory contract",
            details={"expected": REQUIRED_HEADERS, "actual": rows[0][:4] if rows else []},
        )
    result: dict[int, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    for row_no, row in enumerate(rows[1:], start=2):
        if not any(value not in (None, "") for value in row):
            continue
        try:
            nm_id = int(row[0])
            quantity = _decimal(row[2])
        except (ValueError, TypeError, InvalidOperation):
            errors.append({"row": row_no, "code": "invalid_nm_or_quantity"})
            continue
        row_date = str(row[3] or "")[:10]
        if nm_id <= 0 or quantity < ZERO or quantity != quantity.to_integral_value():
            errors.append({"row": row_no, "code": "invalid_target", "nm_id": nm_id})
            continue
        if row_date != business_date:
            errors.append(
                {"row": row_no, "code": "business_date_mismatch", "actual": row_date}
            )
            continue
        if nm_id in result:
            errors.append({"row": row_no, "code": "duplicate_nm_id", "nm_id": nm_id})
            continue
        result[nm_id] = {
            "nm_id": nm_id,
            "sku_comment": str(row[1] or "").strip(),
            "target_quantity": quantity,
            "row_no": row_no,
        }
    if errors or not result:
        raise FfInventoryReconciliationError(
            "invalid_workbook_rows",
            "Manager workbook contains invalid, duplicate or mismatched rows",
            details=errors,
        )
    return result


def _plan_supply_return(
    conn: sqlite3.Connection,
    *,
    supply_id: str,
    business_date: str,
    nomenclature: Mapping[int, Mapping[str, Any]],
) -> dict[str, Any]:
    canonical_source_key = f"wb_supply_debit:supply:{supply_id}"
    operation = conn.execute(
        """
        SELECT * FROM sheet_vitrina_v1_ff_stock_operations
        WHERE source_key IN (?,?)
           OR (source_object_id=? AND source_type='wb_supply')
        ORDER BY CASE WHEN source_key=? THEN 0 WHEN source_key=? THEN 1 ELSE 2 END,
                 created_at DESC LIMIT 1
        """,
        (
            canonical_source_key,
            f"wb_supply_debit:{supply_id}",
            supply_id,
            canonical_source_key,
            f"wb_supply_debit:{supply_id}",
        ),
    ).fetchone()
    if operation is None:
        raise FfInventoryReconciliationError(
            "original_wb_debit_missing",
            f"Original FF debit is missing for WB supply {supply_id}",
        )
    source_key = str(operation["source_key"])
    cached = conn.execute(
        "SELECT supply_id FROM sheet_vitrina_v1_wb_supplies WHERE supply_id=? OR wb_supply_id=?",
        (supply_id, supply_id),
    ).fetchone()
    if cached is not None:
        raise FfInventoryReconciliationError(
            "wb_supply_still_present",
            f"WB supply {supply_id} is still present in the official cache",
        )
    lifecycle = conn.execute(
        """
        SELECT * FROM sheet_vitrina_v1_ff_stock_wb_supply_lifecycle
        WHERE supply_id=?
        """,
        (supply_id,),
    ).fetchone()
    if lifecycle is None or str(lifecycle["lifecycle_state"] or "") not in {
        "missing_confirmed",
        "cancelled_confirmed",
    }:
        raise FfInventoryReconciliationError(
            "wb_supply_return_proof_missing",
            f"WB supply {supply_id} has no confirmed complete-snapshot/cancellation proof",
            details={
                "lifecycle_state": str(lifecycle["lifecycle_state"] or "") if lifecycle else "missing",
                "missing_complete_snapshots": int(lifecycle["consecutive_missing_complete_snapshots"] or 0) if lifecycle else 0,
            },
        )
    debit_lines = conn.execute(
        """
        SELECT line.* FROM sheet_vitrina_v1_ff_stock_operation_lines AS line
        WHERE line.operation_id=? ORDER BY line.nm_id,line.line_no
        """,
        (operation["operation_id"],),
    ).fetchall()
    if not debit_lines:
        raise FfInventoryReconciliationError(
            "original_wb_debit_lines_missing",
            f"Original FF debit lines are missing for WB supply {supply_id}",
        )
    facts = _historical_supply_cost_facts(
        conn,
        supply_id=supply_id,
        debit_lines=debit_lines,
        debit_created_at=str(operation["created_at"] or ""),
    )
    lines: list[dict[str, Any]] = []
    proof_revisions: set[str] = set()
    for debit in debit_lines:
        nm_id = int(debit["nm_id"])
        debit_quantity = abs(_decimal(debit["quantity_delta"]))
        fact = facts.get(nm_id)
        if fact is None:
            raise FfInventoryReconciliationError(
                "original_wb_debit_cost_missing",
                f"Exact original FF debit cost is missing for {supply_id}:{nm_id}",
            )
        if not bool(fact.get("accepted_evidence_available")):
            raise FfInventoryReconciliationError(
                "accepted_quantity_evidence_missing",
                f"Exact accepted quantity is missing for {supply_id}:{nm_id}",
            )
        packed = _decimal(fact["packed_quantity"] or fact["flow_quantity"])
        accepted = _decimal(fact["accepted_quantity"])
        if packed != debit_quantity or accepted < ZERO or accepted > packed:
            raise FfInventoryReconciliationError(
                "original_wb_debit_quantity_inconsistent",
                f"Historical supply fact does not conserve {supply_id}:{nm_id}",
                details={
                    "debit": _text(debit_quantity),
                    "packed": _text(packed),
                    "accepted": _text(accepted),
                },
            )
        return_quantity = packed - accepted
        if return_quantity == ZERO:
            continue
        proof_revisions.add(str(fact["source_revision"]))
        identity = nomenclature.get(nm_id, {})
        lines.append(
            _costed_line(
                nm_id=nm_id,
                quantity_delta=return_quantity,
                unit_cost=_decimal(fact["unit_cost_rub"]),
                identity=identity,
                quality="exact_original_ff_debit",
                provenance={
                    "original_operation_id": str(operation["operation_id"]),
                    "original_source_key": source_key,
                    "original_supply_id": supply_id,
                    "original_source_revision": str(fact["source_revision"]),
                    "original_functional_version_id": str(fact["version_id"]),
                    "packed_quantity": _text(packed),
                    "accepted_quantity": _text(accepted),
                    "return_quantity": _text(return_quantity),
                    "policy": "return_unaccepted_quantity_at_exact_original_ff_debit_cost",
                },
            )
        )
    if not lines:
        raise FfInventoryReconciliationError(
            "wb_supply_has_no_unaccepted_quantity",
            f"WB supply {supply_id} has no quantity eligible for return",
        )
    proof = {
        "proof_kind": (
            "confirmed_cancelled_status"
            if str(lifecycle["lifecycle_state"]) == "cancelled_confirmed"
            else "repeated_complete_official_supply_snapshots"
        ),
        "supply_id": supply_id,
        "original_operation_id": str(operation["operation_id"]),
        "original_source_key": source_key,
        "cache_absent": True,
        "observation_id": str(lifecycle["last_observation_id"] or ""),
        "observation_at": str(lifecycle["last_observation_at"] or ""),
        "consecutive_missing_complete_snapshots": int(
            lifecycle["consecutive_missing_complete_snapshots"] or 0
        ),
        "historical_source_revisions": sorted(proof_revisions),
        "business_date": business_date,
        "returned_quantity": _text(sum((_decimal(line["quantity_delta"]) for line in lines), ZERO)),
        "returned_capital_rub": _text(sum((_decimal(line["capital_delta_rub"]) for line in lines), ZERO)),
    }
    return_source_revision = "sha256:" + _digest(
        {
            "supply_id": supply_id,
            "original_operation_id": str(operation["operation_id"]),
            "lines": [
                {
                    "nm_id": int(line["nm_id"]),
                    "quantity_delta": str(line["quantity_delta"]),
                    "unit_cost_rub": str(line["unit_cost_rub"]),
                    "capital_delta_rub": str(line["capital_delta_rub"]),
                    "original_source_revision": str(
                        (line.get("cost_provenance") or {}).get(
                            "original_source_revision"
                        )
                        or ""
                    ),
                }
                for line in lines
            ],
        }
    )
    proof["return_source_revision"] = return_source_revision
    proof_revision = "sha256:" + _digest(proof)
    proof["proof_revision"] = proof_revision
    return {
        "supply_id": supply_id,
        "source_key": (
            f"wb_supply_return:supply:{supply_id}:"
            f"{operation['operation_id']}:{return_source_revision.split(':', 1)[-1][:16]}"
        ),
        "lines": lines,
        "proof": proof,
    }


def _historical_supply_cost_facts(
    conn: sqlite3.Connection,
    *,
    supply_id: str,
    debit_lines: Iterable[sqlite3.Row],
    debit_created_at: str,
) -> dict[int, dict[str, Any]]:
    debit_rows = list(debit_lines)
    nm_ids = sorted({int(row["nm_id"]) for row in debit_rows})
    frozen_debit_costs: dict[int, dict[str, Any]] = {}
    for debit in debit_rows:
        raw = _loads(debit["raw_json"], {})
        snapshot = raw.get("cost_snapshot") if isinstance(raw, Mapping) else None
        if not isinstance(snapshot, Mapping):
            continue
        unit_cost = _decimal(snapshot.get("unit_cost_rub"))
        quantity = abs(_decimal(debit["quantity_delta"]))
        if unit_cost <= ZERO or quantity <= ZERO:
            continue
        frozen_debit_costs[int(debit["nm_id"])] = {
            "unit_cost_rub": _text(unit_cost),
            "flow_quantity": _text(quantity),
            "packed_quantity": _text(quantity),
            "accepted_quantity": "0",
            "accepted_evidence_available": False,
            "capital_rub": _text(unit_cost * quantity),
            "source_revision": str(snapshot.get("source_plan_fingerprint") or "frozen_debit_cost_snapshot"),
            "version_id": str(snapshot.get("source_version_id") or ""),
            "business_effective_date": str(snapshot.get("source_business_date") or ""),
        }
    placeholders = ",".join("?" for _ in nm_ids)
    debit_date = str(debit_created_at)[:10]
    rows = conn.execute(
        f"""
        SELECT balance.version_id,balance.nm_id,balance.provenance_json,
               version.business_effective_date,version.effective_at,
               version.created_at,version.published_at
        FROM sheet_vitrina_v1_warehouse_functional_balances AS balance
        JOIN sheet_vitrina_v1_warehouse_functional_versions AS version
          ON version.version_id=balance.version_id
        WHERE balance.warehouse_key='ff_to_wb'
          AND balance.nm_id IN ({placeholders})
          AND version.business_effective_date>=?
          AND balance.provenance_json LIKE ?
        ORDER BY version.business_effective_date,version.created_at
        """,
        (*nm_ids, debit_date, f"%{supply_id}%"),
    ).fetchall()
    candidates: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        stack: list[Any] = [_loads(row["provenance_json"], {})]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                if str(value.get("supply_id") or "") == supply_id:
                    unit_cost = value.get("ff_wac_at_ledger_debit_rub")
                    flow_quantity = value.get("flow_quantity") or value.get("packed_quantity")
                    capital = value.get("flow_capital_rub")
                    if unit_cost is not None and flow_quantity is not None:
                        candidates.setdefault(int(row["nm_id"]), []).append(
                            {
                                "unit_cost_rub": _text(_decimal(unit_cost)),
                                "flow_quantity": _text(_decimal(flow_quantity)),
                                "packed_quantity": _text(_decimal(value.get("packed_quantity") or flow_quantity)),
                                "accepted_quantity": _text(_decimal(value.get("accepted_quantity"))),
                                "accepted_evidence_available": True,
                                "capital_rub": _text(
                                    _decimal(capital)
                                    if capital is not None
                                    else _decimal(unit_cost) * _decimal(flow_quantity)
                                ),
                                "source_revision": str(value.get("source_revision") or ""),
                                "version_id": str(row["version_id"]),
                                "business_effective_date": str(row["business_effective_date"]),
                                "version_order": (
                                    str(row["business_effective_date"]),
                                    str(row["published_at"] or row["created_at"] or row["effective_at"] or ""),
                                    str(row["version_id"]),
                                ),
                            }
                        )
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
    result: dict[int, dict[str, Any]] = {}
    for nm_id, items in candidates.items():
        latest_order = max(tuple(item.get("version_order") or ()) for item in items)
        latest_items = [
            item for item in items
            if tuple(item.get("version_order") or ()) == latest_order
        ]
        economic_keys = {
            (
                item["unit_cost_rub"],
                item["flow_quantity"],
                item["packed_quantity"],
                item["accepted_quantity"],
                item["capital_rub"],
            )
            for item in latest_items
        }
        if len(economic_keys) != 1:
            raise FfInventoryReconciliationError(
                "ambiguous_original_wb_debit_cost",
                f"Historical original cost is ambiguous for {supply_id}:{nm_id}",
                details=latest_items,
            )
        selected = dict(latest_items[-1])
        selected.pop("version_order", None)
        result[nm_id] = selected
    for nm_id, fact in frozen_debit_costs.items():
        result.setdefault(nm_id, fact)
    return result


def _frozen_ff_cost_basis(
    conn: sqlite3.Connection,
    *,
    nm_id: int,
    business_date: str,
) -> dict[str, Any] | None:
    original = _explicit_inventory_cost_basis(
        conn,
        nm_id=nm_id,
        business_date=business_date,
        basis_kind="exact_original_source_debit",
    )
    if original is not None:
        return original
    row = conn.execute(
        """
        SELECT balance.wac_rub,balance.quantity,balance.capital_rub,balance.quality,
               version.version_id,version.business_effective_date,version.plan_fingerprint
        FROM sheet_vitrina_v1_warehouse_functional_balances AS balance
        JOIN sheet_vitrina_v1_warehouse_functional_versions AS version
          ON version.version_id=balance.version_id
        WHERE balance.warehouse_key='ff' AND balance.nm_id=?
          AND version.status='good' AND version.business_effective_date<=?
          AND balance.wac_rub IS NOT NULL AND CAST(balance.wac_rub AS NUMERIC)>0
        ORDER BY version.business_effective_date DESC,
                 COALESCE(version.published_at,version.created_at) DESC
        LIMIT 1
        """,
        (nm_id, business_date),
    ).fetchone()
    if row is not None:
        basis_date = str(row["business_effective_date"])
        return {
            "unit_cost_rub": _text(_decimal(row["wac_rub"])),
            "quality": str(row["quality"] or "moving_weighted_average"),
            "basis_kind": (
                "same_sku_same_stage_canonical_ff_wac"
                if basis_date == business_date
                else "last_valid_same_sku_ff_wac"
            ),
            "business_effective_date": basis_date,
            "version_id": str(row["version_id"]),
            "version_fingerprint": str(row["plan_fingerprint"]),
            "basis_quantity": str(row["quantity"]),
            "basis_capital_rub": str(row["capital_rub"]),
        }

    inbound = conn.execute(
        """
        SELECT line.sku_ff_unit_cost_rub,line.qty,line.line_total_cost_rub,
               line.source_status,layer.layer_id,layer.supplier_shipment_id,
               layer.accepted_ff_date,layer.inputs_hash,layer.calculated_at
        FROM sheet_vitrina_v1_supplier_ff_cost_layer_lines AS line
        JOIN sheet_vitrina_v1_supplier_ff_cost_layers AS layer
          ON layer.layer_id=line.layer_id
        JOIN sheet_vitrina_v1_supplier_shipments AS shipment
          ON shipment.shipment_id=layer.supplier_shipment_id
        WHERE line.nm_id=? AND layer.is_current=1
          AND layer.status='confirmed' AND layer.reconciliation_status='ok'
          AND shipment.expenses_complete=1
          AND line.source_status='confirmed'
          AND layer.accepted_ff_date<>'' AND layer.accepted_ff_date<=?
          AND line.sku_ff_unit_cost_rub IS NOT NULL
          AND CAST(line.sku_ff_unit_cost_rub AS NUMERIC)>0
        ORDER BY layer.accepted_ff_date DESC,layer.calculated_at DESC,
                 layer.layer_id DESC,line.layer_line_id DESC
        LIMIT 1
        """,
        (nm_id, business_date),
    ).fetchone()
    if inbound is not None:
        return {
            "unit_cost_rub": _text(_decimal(inbound["sku_ff_unit_cost_rub"])),
            "quality": "certified_supplier_landed_ff_cost",
            "basis_kind": "latest_certified_inbound_landed_ff_cost",
            "business_effective_date": str(inbound["accepted_ff_date"]),
            "version_id": str(inbound["layer_id"]),
            "version_fingerprint": str(inbound["inputs_hash"]),
            "basis_quantity": str(inbound["qty"]),
            "basis_capital_rub": str(inbound["line_total_cost_rub"]),
            "supplier_shipment_id": str(inbound["supplier_shipment_id"]),
        }

    return _explicit_inventory_cost_basis(
        conn,
        nm_id=nm_id,
        business_date=business_date,
        basis_kind="business_approved_estimate",
    )


def _explicit_inventory_cost_basis(
    conn: sqlite3.Connection,
    *,
    nm_id: int,
    business_date: str,
    basis_kind: str,
) -> dict[str, Any] | None:
    if basis_kind not in {
        "exact_original_source_debit",
        "business_approved_estimate",
    }:
        raise FfInventoryReconciliationError(
            "invalid_explicit_cost_basis_kind",
            f"Unsupported explicit FF inventory cost basis: {basis_kind}",
        )
    explicit = conn.execute(
        """
        SELECT * FROM sheet_vitrina_v1_ff_inventory_cost_bases
        WHERE nm_id=? AND effective_from<=? AND status='active'
          AND CAST(unit_cost_rub AS NUMERIC)>0
          AND basis_kind=?
        ORDER BY effective_from DESC,created_at DESC,basis_version_id DESC
        LIMIT 1
        """,
        (nm_id, business_date, basis_kind),
    ).fetchone()
    if explicit is None:
        return None
    provenance = _loads(explicit["provenance_json"], {})
    if not isinstance(provenance, Mapping) or not provenance:
        return None
    approval_reference = str(explicit["approval_reference"] or "").strip()
    if basis_kind == "business_approved_estimate" and not approval_reference:
        return None
    return {
        "unit_cost_rub": _text(_decimal(explicit["unit_cost_rub"])),
        "quality": str(explicit["quality"] or basis_kind),
        "basis_kind": basis_kind,
        "business_effective_date": str(explicit["effective_from"]),
        "version_id": str(explicit["basis_version_id"]),
        "version_fingerprint": "sha256:" + _digest(
            {
                "basis_version_id": str(explicit["basis_version_id"]),
                "nm_id": nm_id,
                "effective_from": str(explicit["effective_from"]),
                "unit_cost_rub": str(explicit["unit_cost_rub"]),
                "basis_kind": basis_kind,
                "source_reference": str(explicit["source_reference"]),
                "approval_reference": approval_reference,
                "provenance": provenance,
            }
        ),
        "source_reference": str(explicit["source_reference"]),
        "approval_reference": approval_reference,
        "provenance": provenance,
    }


def _costed_line(
    *,
    nm_id: int,
    quantity_delta: Decimal,
    unit_cost: Decimal,
    identity: Mapping[str, Any],
    quality: str,
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    if unit_cost <= ZERO:
        raise FfInventoryReconciliationError(
            "invalid_cost_basis",
            f"Synthetic/non-positive cost is forbidden for nmID {nm_id}",
        )
    capital_delta = quantity_delta * unit_cost
    cost_provenance = {"quality": quality, **dict(provenance)}
    return {
        "nm_id": nm_id,
        "barcode": str(identity.get("barcode") or ""),
        "sku": str(identity.get("our_sku") or identity.get("nomenclature_name") or nm_id),
        "nomenclature_name": str(identity.get("nomenclature_name") or ""),
        "comment": "",
        "group_name": str(identity.get("group_name") or ""),
        "quantity_delta": _text(quantity_delta),
        "unit_cost_rub": _text(unit_cost),
        "capital_delta_rub": _text(capital_delta),
        "cost_quality": quality,
        "cost_provenance": cost_provenance,
        "raw": {
            "nm_id": nm_id,
            "quantity_delta": _text(quantity_delta),
            "cost_snapshot": {
                "unit_cost_rub": _text(unit_cost),
                "capital_delta_rub": _text(capital_delta),
                "quality": quality,
                "provenance": cost_provenance,
            },
        },
    }


def _insert_operation(
    conn: sqlite3.Connection,
    *,
    document: Mapping[str, Any],
    created_at: str,
    business_date: str,
    created_by: str,
    source_filename: str,
    source_sha256: str,
    source_bytes: bytes | None,
    reconciliation_id: str,
    plan_fingerprint: str,
    approval_reference: str,
) -> None:
    lines = [dict(item) for item in document.get("lines") or []]
    operation_id = str(document["operation_id"])
    existing = conn.execute(
        "SELECT operation_id FROM sheet_vitrina_v1_ff_stock_operations WHERE source_key=?",
        (str(document["source_key"]),),
    ).fetchone()
    if existing is not None:
        if str(existing["operation_id"]) != operation_id:
            raise FfInventoryReconciliationError(
                "source_key_collision",
                f"Unexpected operation already owns {document['source_key']}",
            )
        return
    total_delta = sum((_decimal(item["quantity_delta"]) for item in lines), ZERO)
    total_abs = sum((abs(_decimal(item["quantity_delta"])) for item in lines), ZERO)
    diagnostics = {
        **dict(document.get("diagnostics") or {}),
        "reconciliation_id": reconciliation_id,
        "plan_fingerprint": plan_fingerprint,
        "approval_reference": approval_reference,
        "quantity_decimal": True,
        "capital_decimal": True,
    }
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_ff_stock_operations(
            operation_id,operation_type,source_type,source_key,source_object_id,
            source_object_label,created_at,business_effective_date,created_by,
            sku_count,total_quantity_delta,total_quantity_abs,warnings_json,
            diagnostics_json,source_filename,source_content_type,source_file_sha256,
            source_file_blob
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            operation_id,
            str(document["operation_type"]),
            str(document["source_type"]),
            str(document["source_key"]),
            str(document.get("source_object_id") or ""),
            str(document.get("source_object_label") or ""),
            created_at,
            business_date,
            created_by,
            len({int(item["nm_id"]) for item in lines}),
            float(total_delta),
            float(total_abs),
            "[]",
            _json(diagnostics),
            source_filename,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if source_bytes is not None else "",
            source_sha256,
            sqlite3.Binary(source_bytes) if source_bytes is not None else None,
        ),
    )
    conn.executemany(
        """
        INSERT INTO sheet_vitrina_v1_ff_stock_operation_lines(
            operation_id,line_no,nm_id,barcode,sku,nomenclature_name,comment,
            group_name,quantity_delta,raw_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                operation_id,
                index,
                int(item["nm_id"]),
                str(item.get("barcode") or ""),
                str(item.get("sku") or ""),
                str(item.get("nomenclature_name") or ""),
                str(item.get("comment") or ""),
                str(item.get("group_name") or ""),
                float(_decimal(item["quantity_delta"])),
                _json(item.get("raw") or item),
            )
            for index, item in enumerate(lines, start=1)
        ],
    )


def _exact_nomenclature(
    conn: sqlite3.Connection,
    nm_ids: Iterable[int],
) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    normalized = sorted({int(item) for item in nm_ids})
    placeholders = ",".join("?" for _ in normalized)
    rows = conn.execute(
        f"""
        SELECT item_id,nm_id,our_sku,nomenclature_name,barcode,
               product_type AS group_name,updated_at
        FROM sheet_vitrina_v1_nomenclature_items
        WHERE nm_id IN ({placeholders}) AND is_active=1 AND is_hidden=0
        ORDER BY nm_id,item_id
        """,
        tuple(normalized),
    ).fetchall()
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(int(row["nm_id"]), []).append(dict(row))
    result: dict[int, dict[str, Any]] = {}
    blockers: list[dict[str, Any]] = []
    for nm_id in normalized:
        candidates = grouped.get(nm_id, [])
        if len(candidates) != 1:
            blockers.append(
                {
                    "code": "nomenclature_unmatched_or_ambiguous",
                    "nm_id": nm_id,
                    "active_candidate_count": len(candidates),
                }
            )
        else:
            result[nm_id] = candidates[0]
    return result, blockers


def _target_readback(
    conn: sqlite3.Connection,
    *,
    per_sku: Iterable[Mapping[str, Any]],
    target_total: Decimal,
) -> dict[str, Any]:
    expected = {int(item["nm_id"]): _decimal(item["target_quantity"]) for item in per_sku}
    actual = _ff_balances(conn, expected)
    mismatches = [
        {
            "nm_id": nm_id,
            "expected": _text(expected[nm_id]),
            "actual": _text(actual.get(nm_id, ZERO)),
        }
        for nm_id in sorted(expected)
        if expected[nm_id] != actual.get(nm_id, ZERO)
    ]
    actual_total = _ff_total(conn)
    return {
        "target_matches": not mismatches and actual_total == target_total,
        "sku_count": len(expected),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "expected_total": _text(target_total),
        "actual_total": _text(actual_total),
        "negative_balance_count": sum(1 for value in actual.values() if value < ZERO),
    }


def _active_version_guard(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT version.version_id,version.business_effective_date,version.effective_at,
               version.plan_fingerprint
        FROM sheet_vitrina_v1_warehouse_functional_active AS active
        JOIN sheet_vitrina_v1_warehouse_functional_versions AS version
          ON version.version_id=active.version_id WHERE active.slot=1
        """
    ).fetchone()
    return dict(row) if row is not None else {}


def _relevant_ledger_digest(conn: sqlite3.Connection, nm_ids: Iterable[int]) -> str:
    normalized = sorted({int(item) for item in nm_ids})
    placeholders = ",".join("?" for _ in normalized)
    rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT operation.operation_id,operation.operation_type,operation.source_type,
                   operation.source_key,operation.created_at,operation.business_effective_date,
                   line.line_no,line.nm_id,line.quantity_delta,line.raw_json
            FROM sheet_vitrina_v1_ff_stock_operations AS operation
            JOIN sheet_vitrina_v1_ff_stock_operation_lines AS line
              ON line.operation_id=operation.operation_id
            WHERE line.nm_id IN ({placeholders})
            ORDER BY operation.created_at,operation.operation_id,line.line_no
            """,
            tuple(normalized),
        ).fetchall()
    ]
    return "sha256:" + _digest(rows)


def _non_target_digest(conn: sqlite3.Connection, nm_ids: Iterable[int]) -> str:
    normalized = sorted({int(item) for item in nm_ids})
    placeholders = ",".join("?" for _ in normalized)
    ledger = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT operation.operation_id,operation.operation_type,
                   operation.source_type,operation.source_key,
                   operation.business_effective_date,line.line_no,line.nm_id,
                   line.quantity_delta,line.raw_json
            FROM sheet_vitrina_v1_ff_stock_operation_lines AS line
            JOIN sheet_vitrina_v1_ff_stock_operations AS operation
              ON operation.operation_id=line.operation_id
            WHERE line.nm_id NOT IN ({placeholders})
            ORDER BY operation.created_at,operation.operation_id,line.line_no
            """,
            tuple(normalized),
        ).fetchall()
    ]
    return "sha256:" + _digest({"non_target_ff_ledger": ledger})


def _ff_balances(conn: sqlite3.Connection, nm_ids: Iterable[int]) -> dict[int, Decimal]:
    normalized = sorted({int(item) for item in nm_ids})
    if not normalized:
        return {}
    placeholders = ",".join("?" for _ in normalized)
    rows = conn.execute(
        f"""
        SELECT nm_id,SUM(quantity_delta) AS quantity
        FROM sheet_vitrina_v1_ff_stock_operation_lines
        WHERE nm_id IN ({placeholders}) GROUP BY nm_id
        """,
        tuple(normalized),
    ).fetchall()
    values = {int(row["nm_id"]): _decimal(row["quantity"]) for row in rows}
    return {nm_id: values.get(nm_id, ZERO) for nm_id in normalized}


def _ff_total(conn: sqlite3.Connection) -> Decimal:
    return _decimal(
        conn.execute(
            "SELECT COALESCE(SUM(quantity_delta),0) FROM sheet_vitrina_v1_ff_stock_operation_lines"
        ).fetchone()[0]
    )


def _require_tables(conn: sqlite3.Connection) -> None:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {
        "sheet_vitrina_v1_ff_stock_operations",
        "sheet_vitrina_v1_ff_stock_operation_lines",
        "sheet_vitrina_v1_nomenclature_items",
        "sheet_vitrina_v1_wb_supplies",
        "sheet_vitrina_v1_ff_stock_wb_supply_lifecycle",
        "sheet_vitrina_v1_warehouse_functional_versions",
        "sheet_vitrina_v1_warehouse_functional_balances",
        "sheet_vitrina_v1_warehouse_functional_active",
        "sheet_vitrina_v1_ff_inventory_reconciliations",
    }
    missing = sorted(required - tables)
    if missing:
        raise FfInventoryReconciliationError(
            "required_tables_missing",
            "Required reconciliation tables are missing",
            details=missing,
        )


def _connect(db_path: Path, *, query_only: bool = False) -> sqlite3.Connection:
    if query_only:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=ON")
    else:
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=120000")
    conn.row_factory = sqlite3.Row
    return conn


def _decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return ZERO
    return Decimal(str(value))


def _text(value: Any) -> str:
    decimal = _decimal(value)
    if decimal == ZERO:
        return "0"
    return format(decimal.normalize(), "f")


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError):
        return default


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
