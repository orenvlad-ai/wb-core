"""Audited FF inventory reconciliation with exact movement capital.

The manager workbook is a physical target, never an editable balance
snapshot.  A plan separates proven WB-supply returns from genuine inventory
receipt/write-off documents and freezes every cost basis before mutation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Mapping

from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
)
from packages.application.simple_xlsx import (
    XlsxCell,
    build_single_sheet_workbook_bytes,
    read_first_sheet_cells,
)
from packages.application.warehouse_business_projection import (
    ensure_warehouse_projection_source_outbox,
)
from packages.application.warehouse_functional import (
    ensure_warehouse_functional_schema,
)


CONTRACT_NAME = "ff_inventory_reconciliation_v1"
PLAN_VERSION = "v2_target_intent"
CONFIRM_RETRY_LIMIT = 5
CONFIRM_RETRYABLE_CODES = {
    "relevant_ledger_changed",
    "non_target_digest_changed",
    "inventory_cost_basis_changed",
    "wb_supply_return_proof_changed",
    "wb_supply_return_lifecycle_changed",
}
ZERO = Decimal("0")
REQUIRED_HEADERS = (
    "nmId",
    "Штрихкод",
    "Комментарий SKU",
    "Остаток ФФ",
    "Дата остатка",
)
LEGACY_NM_ID_HEADERS = (
    "nmId",
    "Комментарий SKU",
    "Остаток ФФ",
    "Дата остатка",
)

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
        with _connect(self.runtime.db_path) as conn:
            ensure_inventory_reconciliation_schema(conn)
            conn.commit()

    def build_template(
        self,
        *,
        business_date: str,
    ) -> tuple[bytes, str, str]:
        normalized_date = str(business_date or "").strip()[:10]
        try:
            datetime.fromisoformat(normalized_date)
        except ValueError as exc:
            raise FfInventoryReconciliationError(
                "invalid_business_date",
                "Business date must be YYYY-MM-DD",
            ) from exc
        with _connect(self.runtime.db_path, query_only=True) as conn:
            catalog, blockers = _complete_active_nomenclature(conn)
            blockers.extend(_template_barcode_blockers(catalog))
            if blockers:
                raise FfInventoryReconciliationError(
                    "active_nomenclature_ambiguous",
                    "Active nomenclature cannot produce one stable FF inventory template",
                    details=blockers,
                )
            balances = _ff_balances_as_of(
                conn,
                catalog,
                business_date=normalized_date,
            )
        rows = [list(REQUIRED_HEADERS)]
        for nm_id, item in sorted(catalog.items()):
            balance = balances.get(nm_id, ZERO)
            if balance != balance.to_integral_value():
                raise FfInventoryReconciliationError(
                    "non_integral_physical_balance",
                    f"FF balance for nmId {nm_id} is not an integral physical quantity",
                )
            rows.append(
                [
                    nm_id,
                    str(item.get("barcode") or ""),
                    str(item.get("our_sku") or item.get("nomenclature_name") or nm_id),
                    int(balance),
                    normalized_date,
                ]
            )
        workbook = build_single_sheet_workbook_bytes(
            "Инвентаризация FF",
            rows,
            column_widths=[14, 34, 36, 16, 18],
            text_columns={2},
        )
        return (
            workbook,
            f"Инвентаризация_FF_{normalized_date}.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def create_preview(
        self,
        *,
        source_bytes: bytes,
        source_filename: str,
        business_date: str,
        return_supply_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        normalized_returns = sorted(
            {str(item).strip() for item in return_supply_ids if str(item).strip()}
        )
        plan = self.build_plan(
            source_bytes=source_bytes,
            source_filename=source_filename,
            business_date=business_date,
            return_supply_ids=normalized_returns,
        )
        source_sha256 = "sha256:" + hashlib.sha256(source_bytes).hexdigest()
        preview_id = "ffip_" + hashlib.sha256(
            f"{source_sha256}:{business_date}:{plan.get('fingerprint') or ''}".encode("utf-8")
        ).hexdigest()[:24]
        created_at = str(self.timestamp_factory())
        with _connect(self.runtime.db_path) as conn:
            ensure_inventory_reconciliation_schema(conn)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_ff_inventory_previews(
                    preview_id,source_sha256,source_filename,source_file_blob,
                    business_date,return_supply_ids_json,plan_fingerprint,
                    plan_json,created_at,status
                ) VALUES(?,?,?,?,?,?,?,?,?,'previewed')
                ON CONFLICT(preview_id) DO UPDATE SET
                    plan_json=excluded.plan_json,created_at=excluded.created_at
                """,
                (
                    preview_id,
                    source_sha256,
                    source_filename,
                    sqlite3.Binary(source_bytes),
                    str(business_date),
                    _json(normalized_returns),
                    str(plan.get("fingerprint") or ""),
                    _json(plan),
                    created_at,
                ),
            )
            conn.commit()
        return {**plan, "preview_id": preview_id, "source_sha256": source_sha256}

    def confirm_preview(
        self,
        *,
        preview_id: str,
        confirmation_fingerprint: str,
        created_by: str,
    ) -> dict[str, Any]:
        with _connect(self.runtime.db_path, query_only=True) as conn:
            row = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_ff_inventory_previews WHERE preview_id=?",
                (str(preview_id),),
            ).fetchone()
        if row is None:
            raise FfInventoryReconciliationError(
                "preview_not_found",
                "Inventory preview not found",
            )
        if confirmation_fingerprint != str(row["plan_fingerprint"] or ""):
            raise FfInventoryReconciliationError(
                "preview_confirmation_mismatch",
                "Confirmation does not match the stored inventory target",
            )
        stored_plan = dict(_loads(row["plan_json"], {}))
        if str(stored_plan.get("fingerprint") or "") != str(
            row["plan_fingerprint"] or ""
        ):
            raise FfInventoryReconciliationError(
                "stored_preview_identity_invalid",
                "Stored inventory preview identity is inconsistent",
            )
        if str(row["status"] or "") not in {"previewed", "confirmed"} or not bool(
            stored_plan.get("apply_allowed")
        ):
            raise FfInventoryReconciliationError(
                "preview_not_confirmable",
                "Inventory preview did not pass validation",
                details=stored_plan.get("blockers") or [],
            )
        confirmed_target_intent = _target_intent_from_stored_plan(
            stored_plan,
            source_sha256=str(row["source_sha256"] or ""),
            business_date=str(row["business_date"] or ""),
            return_supply_ids=_loads(row["return_supply_ids_json"], []),
        )
        result = self.apply_plan(
            source_bytes=bytes(row["source_file_blob"]),
            source_filename=str(row["source_filename"]),
            business_date=str(row["business_date"]),
            return_supply_ids=_loads(row["return_supply_ids_json"], []),
            confirmation_fingerprint=confirmation_fingerprint,
            approval_reference=f"ui-explicit-confirm:{preview_id}",
            created_by=created_by,
            _confirmed_target_intent=confirmed_target_intent,
        )
        with _connect(self.runtime.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_ff_inventory_previews SET status='confirmed' WHERE preview_id=?",
                (str(preview_id),),
            )
            conn.commit()
        return {**result, "preview_id": str(preview_id)}

    def build_plan(
        self,
        *,
        source_bytes: bytes,
        source_filename: str,
        business_date: str,
        return_supply_ids: Iterable[str] = (),
        _confirmed_target_intent: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        source_sha256 = "sha256:" + hashlib.sha256(source_bytes).hexdigest()
        normalized_return_supply_ids = sorted(
            {str(item).strip() for item in return_supply_ids if str(item).strip()}
        )
        parsed_rows = _parse_target_workbook(source_bytes, business_date=business_date)
        source_key = f"{source_sha256}:{business_date}"
        with _connect(self.runtime.db_path, query_only=True) as conn:
            _require_tables(conn)
            if _confirmed_target_intent is None:
                active_catalog, _ = _complete_active_nomenclature(conn)
                target_rows, identity_blockers = _resolve_target_rows(
                    parsed_rows,
                    active_catalog=active_catalog,
                )
                target_nm_ids = sorted(target_rows)
                nomenclature, nomenclature_blockers = _exact_nomenclature(
                    conn, target_nm_ids
                )
                blockers: list[dict[str, Any]] = [
                    *identity_blockers,
                    *nomenclature_blockers,
                ]
                missing_nm_ids = sorted(set(active_catalog) - set(target_nm_ids))
                if missing_nm_ids:
                    blockers.append(
                        {
                            "code": "active_nomenclature_rows_missing",
                            "nm_ids": missing_nm_ids,
                            "message": (
                                "Manager workbook must contain every active FF nomenclature identity, "
                                "including explicit zero targets"
                            ),
                        }
                    )
            else:
                target_rows, nomenclature = _materialize_confirmed_target_intent(
                    _confirmed_target_intent,
                    source_sha256=source_sha256,
                    business_date=business_date,
                    return_supply_ids=normalized_return_supply_ids,
                )
                target_nm_ids = sorted(target_rows)
                blockers = []
            existing = conn.execute(
                """
                SELECT * FROM sheet_vitrina_v1_ff_inventory_reconciliations
                WHERE source_sha256=? AND business_date=?
                """,
                (source_sha256, business_date),
            ).fetchone()
            before = _ff_balances(conn, target_nm_ids)
            whole_before = _ff_total(conn)
            if (
                not blockers
                and existing is not None
                and str(existing["status"] or "") == "applied"
            ):
                target_matches = all(
                    before.get(nm_id, ZERO) == target_rows[nm_id]["target_quantity"]
                    for nm_id in target_nm_ids
                ) and whole_before == sum(
                    (item["target_quantity"] for item in target_rows.values()), ZERO
                )
                return {
                    "contract_name": CONTRACT_NAME,
                    "plan_version": PLAN_VERSION,
                    "status": "already_applied",
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
            if not blockers and existing is not None:
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

            returns: list[dict[str, Any]] = []
            return_by_nm: dict[int, Decimal] = {}
            for supply_id in normalized_return_supply_ids:
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
            available_cost_bases: dict[int, dict[str, Any]] = {}
            adjustment_lines: list[dict[str, Any]] = []
            for nm_id in target_nm_ids:
                baseline_after_returns = before.get(nm_id, ZERO) + return_by_nm.get(nm_id, ZERO)
                delta = target_rows[nm_id]["target_quantity"] - baseline_after_returns
                basis = _frozen_ff_cost_basis(
                    conn,
                    nm_id=nm_id,
                    business_date=business_date,
                )
                validate_without_delta = (
                    _confirmed_target_intent is None
                    or bool(_confirmed_target_intent.get("all_target_costs_validated"))
                )
                if basis is None:
                    if delta != ZERO or validate_without_delta:
                        blockers.append(
                            {
                                "code": "inventory_cost_basis_missing",
                                "nm_id": nm_id,
                                "message": "No admissible same-SKU FF cost basis exists on or before the business date",
                            }
                        )
                    continue
                available_cost_bases[nm_id] = basis
                if delta == ZERO:
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
                        "source_nm_id": item.get("source_nm_id"),
                        "source_barcode": item.get("source_barcode") or "",
                        "identity_source": item.get("identity_source") or "",
                        "source_row": item.get("row_no"),
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
                "audit_active_functional_version": _active_version_guard(conn),
                "nomenclature_digest": _digest(nomenclature),
                "return_proofs": [item["proof"] for item in returns],
                "cost_snapshot_digest": _digest(cost_snapshot),
                "available_cost_bases_digest": _digest(available_cost_bases),
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
        target_intent = (
            _normalize_target_intent(_confirmed_target_intent)
            if _confirmed_target_intent is not None
            else _build_target_intent(
                source_sha256=source_sha256,
                business_date=business_date,
                return_supply_ids=normalized_return_supply_ids,
                target_rows=target_rows,
                nomenclature=nomenclature,
                all_target_costs_validated=True,
            )
        )
        manifest = {
            "plan_version": PLAN_VERSION,
            "source": {
                "filename": source_filename,
                "sha256": source_sha256,
                "business_date": business_date,
                "row_count": len(target_rows),
                "input_row_count": len(parsed_rows),
                "header_profile": (
                    parsed_rows[0].get("header_profile") if parsed_rows else ""
                ),
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
            "target_intent": target_intent,
            "source_revisions": source_revisions,
            "relevant_ledger_digest": relevant_ledger_digest,
            "non_target_digest": non_target_digest,
            "expected_operation_ids": operation_ids,
            "invariants": {
                "target_sku_count": len(target_rows),
                "unmatched_or_ambiguous_count": sum(
                    1
                    for item in blockers
                    if str(item.get("code") or "")
                    in {
                        "unknown_nm_id",
                        "unknown_barcode",
                        "ambiguous_barcode",
                        "nm_id_barcode_conflict",
                        "empty_inventory_identity",
                        "duplicate_resolved_sku",
                        "nomenclature_unmatched_or_ambiguous",
                    }
                ),
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
        manifest["semantic_snapshot_fingerprint"] = "sha256:" + _digest(manifest)
        fingerprint = _target_intent_fingerprint(target_intent)
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
        _confirmed_target_intent: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Apply one confirmed absolute target with bounded optimistic retries.

        The confirmation token authorizes the server-stored target.  Every
        attempt rebuilds the actual before/delta/cost plan from canonical data;
        source drift between that read and BEGIN IMMEDIATE is absorbed here and
        never turned into a second owner confirmation.
        """

        normalized_return_supply_ids = tuple(
            sorted({str(item).strip() for item in return_supply_ids if str(item).strip()})
        )
        last_retryable: Exception | None = None
        for attempt in range(CONFIRM_RETRY_LIMIT):
            try:
                return self._apply_plan_once(
                    source_bytes=source_bytes,
                    source_filename=source_filename,
                    business_date=business_date,
                    return_supply_ids=normalized_return_supply_ids,
                    confirmation_fingerprint=confirmation_fingerprint,
                    approval_reference=approval_reference,
                    created_by=created_by,
                    confirmed_target_intent=_confirmed_target_intent,
                )
            except FfInventoryReconciliationError as exc:
                if exc.code not in CONFIRM_RETRYABLE_CODES or attempt + 1 >= CONFIRM_RETRY_LIMIT:
                    raise
                last_retryable = exc
            except sqlite3.OperationalError as exc:
                lowered = str(exc).lower()
                if not any(token in lowered for token in ("locked", "busy", "snapshot")):
                    raise
                if attempt + 1 >= CONFIRM_RETRY_LIMIT:
                    raise
                last_retryable = exc
        assert last_retryable is not None
        raise last_retryable

    def _apply_plan_once(
        self,
        *,
        source_bytes: bytes,
        source_filename: str,
        business_date: str,
        return_supply_ids: Iterable[str],
        confirmation_fingerprint: str,
        approval_reference: str,
        created_by: str,
        confirmed_target_intent: Mapping[str, Any] | None,
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
            _confirmed_target_intent=confirmed_target_intent,
        )
        if plan.get("idempotent"):
            if confirmed_target_intent is None and confirmation_fingerprint != str(
                plan.get("fingerprint") or ""
            ):
                raise FfInventoryReconciliationError(
                    "confirmation_target_mismatch",
                    "Confirmation does not match the existing inventory target",
                )
            replay = _ensure_existing_inventory_replay(
                self.runtime,
                reconciliation_id=str(plan.get("reconciliation_id") or ""),
                requested_at=str(self.timestamp_factory()),
            )
            return {
                "status": "already_applied",
                "idempotent": True,
                "reconciliation_id": str(plan.get("reconciliation_id") or ""),
                "fingerprint": str(plan.get("fingerprint") or ""),
                "readback": dict(plan.get("readback") or {}),
                "replay": replay,
                "plan": plan,
            }
        if confirmed_target_intent is None and confirmation_fingerprint != str(
            plan.get("fingerprint") or ""
        ):
            raise FfInventoryReconciliationError(
                "confirmation_target_mismatch",
                "Confirmation does not match the inventory target",
            )
        if not plan.get("apply_allowed"):
            raise FfInventoryReconciliationError(
                "plan_blocked",
                "The current dry-run is blocked",
                details=plan.get("blockers") or [],
            )
        manifest = dict(plan["manifest"])
        source = dict(manifest["source"])
        intent_fingerprint = _target_intent_fingerprint(manifest["target_intent"])
        manifest["confirmation_fingerprint"] = confirmation_fingerprint
        manifest["intent_fingerprint"] = intent_fingerprint
        reconciliation_id = "ffir_" + intent_fingerprint.split(":", 1)[-1][:24]
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
                locked_nomenclature = {
                    int(item["nm_id"]): dict(item["identity"])
                    for item in manifest["target_intent"].get("targets") or []
                }
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
                replay = _enqueue_inventory_replay(
                    conn,
                    reconciliation_id=reconciliation_id,
                    plan_fingerprint=confirmation_fingerprint,
                    business_date=business_date,
                    nm_ids=target_nm_ids,
                    readback=readback,
                    requested_at=_format_timestamp(now),
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
            "replay": replay,
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
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_ff_inventory_previews(
            preview_id TEXT PRIMARY KEY,
            source_sha256 TEXT NOT NULL,
            source_filename TEXT NOT NULL,
            source_file_blob BLOB NOT NULL,
            business_date TEXT NOT NULL,
            return_supply_ids_json TEXT NOT NULL,
            plan_fingerprint TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL
        )
        """
    )
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


def _enqueue_inventory_replay(
    conn: sqlite3.Connection,
    *,
    reconciliation_id: str,
    plan_fingerprint: str,
    business_date: str,
    nm_ids: Iterable[int],
    readback: Mapping[str, Any],
    requested_at: str,
) -> dict[str, Any]:
    """Persist exact functional replay in the same transaction as the ledger.

    This removes the crash window between durable reconciliation commit and a
    second enqueue transaction.  The existing canonical warehouse queue and
    hourly worker remain the only replay executor.
    """

    stable_source_id = "ff_inventory:" + reconciliation_id
    source_revision = "sha256:" + _digest(
        {
            "fingerprint": plan_fingerprint,
            "status": "applied",
            "reversal": False,
            "audit": _json(dict(readback)),
        }
    )
    queue_id = "whrq_" + _digest(
        {
            "stable_source_id": stable_source_id,
            "source_revision": source_revision,
        }
    )[:24]
    affected_nm_ids = sorted({int(item) for item in nm_ids if int(item) > 0})
    conn.execute(
        """
        INSERT INTO sheet_vitrina_v1_warehouse_targeted_recalc_queue(
            queue_id,stable_source_id,source_revision,effective_date,
            affected_nm_ids_json,status,requested_at,started_at,finished_at,error
        ) VALUES(?,?,?,?,?,'queued',?,NULL,NULL,NULL)
        ON CONFLICT(stable_source_id,source_revision) DO NOTHING
        """,
        (
            queue_id,
            stable_source_id,
            source_revision,
            business_date,
            _json(affected_nm_ids),
            requested_at,
        ),
    )
    row = conn.execute(
        "SELECT queue_id,status,requested_at FROM "
        "sheet_vitrina_v1_warehouse_targeted_recalc_queue "
        "WHERE stable_source_id=? AND source_revision=?",
        (stable_source_id, source_revision),
    ).fetchone()
    return {
        "status": str(row["status"] if row is not None else "queued"),
        "queue_id": str(row["queue_id"] if row is not None else queue_id),
        "requested_at": str(row["requested_at"] if row is not None else requested_at),
    }


def _ensure_existing_inventory_replay(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    reconciliation_id: str,
    requested_at: str,
) -> dict[str, Any]:
    """Repair only the legacy commit-to-enqueue gap on an exact T0 retry."""

    with _connect(runtime.db_path) as conn:
        ensure_warehouse_functional_schema(conn)
        row = conn.execute(
            "SELECT business_date,plan_fingerprint,manifest_json,reconciliation_json "
            "FROM sheet_vitrina_v1_ff_inventory_reconciliations "
            "WHERE reconciliation_id=? AND status='applied'",
            (reconciliation_id,),
        ).fetchone()
        if row is None:
            raise FfInventoryReconciliationError(
                "applied_reconciliation_missing",
                "Applied inventory reconciliation is missing during replay readback",
            )
        manifest = dict(_loads(row["manifest_json"], {}))
        readback = dict(_loads(row["reconciliation_json"], {}))
        stable_source_id = "ff_inventory:" + reconciliation_id
        source_revision = "sha256:" + _digest(
            {
                "fingerprint": str(row["plan_fingerprint"] or ""),
                "status": "applied",
                "reversal": False,
                "audit": _json(readback),
            }
        )
        existing = conn.execute(
            "SELECT queue_id,status,requested_at FROM "
            "sheet_vitrina_v1_warehouse_targeted_recalc_queue "
            "WHERE stable_source_id=? AND source_revision=?",
            (stable_source_id, source_revision),
        ).fetchone()
        if existing is not None:
            return {
                "status": str(existing["status"] or "queued"),
                "queue_id": str(existing["queue_id"] or ""),
                "requested_at": str(existing["requested_at"] or ""),
            }
        replay = _enqueue_inventory_replay(
            conn,
            reconciliation_id=reconciliation_id,
            plan_fingerprint=str(row["plan_fingerprint"] or ""),
            business_date=str(row["business_date"] or ""),
            nm_ids=[int(item.get("nm_id") or 0) for item in manifest.get("per_sku") or []],
            readback=readback,
            requested_at=requested_at,
        )
        conn.commit()
        return replay


def _build_target_intent(
    *,
    source_sha256: str,
    business_date: str,
    return_supply_ids: Iterable[str],
    target_rows: Mapping[int, Mapping[str, Any]],
    nomenclature: Mapping[int, Mapping[str, Any]],
    all_target_costs_validated: bool,
) -> dict[str, Any]:
    targets: list[dict[str, Any]] = []
    for nm_id in sorted(target_rows):
        row = dict(target_rows[nm_id])
        identity = dict(nomenclature.get(nm_id) or {})
        targets.append(
            {
                "nm_id": int(nm_id),
                "target_quantity": _text(row.get("target_quantity")),
                "source_nm_id": row.get("source_nm_id"),
                "source_barcode": str(row.get("source_barcode") or ""),
                "identity_source": str(row.get("identity_source") or ""),
                "matched_barcode_role": str(row.get("matched_barcode_role") or ""),
                "sku_comment": str(row.get("sku_comment") or ""),
                "row_no": int(row.get("row_no") or 0),
                "header_profile": str(row.get("header_profile") or ""),
                "identity": {
                    "item_id": str(identity.get("item_id") or ""),
                    "nm_id": int(nm_id),
                    "our_sku": str(identity.get("our_sku") or ""),
                    "nomenclature_name": str(identity.get("nomenclature_name") or ""),
                    "barcode": str(identity.get("barcode") or ""),
                    "barcodes": sorted(
                        {str(value) for value in identity.get("barcodes") or [] if str(value)}
                    ),
                    "group_name": str(identity.get("group_name") or ""),
                },
            }
        )
    return _normalize_target_intent(
        {
            "contract_name": "ff_inventory_target_intent_v1",
            "source_sha256": source_sha256,
            "business_date": business_date,
            "return_supply_ids": sorted(
                {str(item).strip() for item in return_supply_ids if str(item).strip()}
            ),
            "all_target_costs_validated": bool(all_target_costs_validated),
            "targets": targets,
        }
    )


def _normalize_target_intent(value: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(value or {})
    normalized_targets: list[dict[str, Any]] = []
    for raw_target in raw.get("targets") or []:
        target = dict(raw_target or {})
        nm_id = int(target.get("nm_id") or 0)
        identity = dict(target.get("identity") or {})
        normalized_targets.append(
            {
                "nm_id": nm_id,
                "target_quantity": _text(target.get("target_quantity")),
                "source_nm_id": target.get("source_nm_id"),
                "source_barcode": str(target.get("source_barcode") or ""),
                "identity_source": str(target.get("identity_source") or ""),
                "matched_barcode_role": str(target.get("matched_barcode_role") or ""),
                "sku_comment": str(target.get("sku_comment") or ""),
                "row_no": int(target.get("row_no") or 0),
                "header_profile": str(target.get("header_profile") or ""),
                "identity": {
                    "item_id": str(identity.get("item_id") or ""),
                    "nm_id": nm_id,
                    "our_sku": str(identity.get("our_sku") or ""),
                    "nomenclature_name": str(identity.get("nomenclature_name") or ""),
                    "barcode": str(identity.get("barcode") or ""),
                    "barcodes": sorted(
                        {str(item) for item in identity.get("barcodes") or [] if str(item)}
                    ),
                    "group_name": str(identity.get("group_name") or ""),
                },
            }
        )
    normalized_targets.sort(key=lambda item: int(item["nm_id"]))
    return {
        "contract_name": "ff_inventory_target_intent_v1",
        "source_sha256": str(raw.get("source_sha256") or ""),
        "business_date": str(raw.get("business_date") or ""),
        "return_supply_ids": sorted(
            {str(item).strip() for item in raw.get("return_supply_ids") or [] if str(item).strip()}
        ),
        "all_target_costs_validated": bool(raw.get("all_target_costs_validated")),
        "targets": normalized_targets,
    }


def _target_intent_fingerprint(value: Mapping[str, Any]) -> str:
    return "sha256:" + _digest(_normalize_target_intent(value))


def _target_intent_from_stored_plan(
    plan: Mapping[str, Any],
    *,
    source_sha256: str,
    business_date: str,
    return_supply_ids: Iterable[str],
) -> dict[str, Any]:
    manifest = dict(plan.get("manifest") or {})
    current = manifest.get("target_intent")
    if isinstance(current, Mapping) and current.get("targets"):
        intent = _normalize_target_intent(current)
        if _target_intent_fingerprint(intent) != str(plan.get("fingerprint") or ""):
            raise FfInventoryReconciliationError(
                "stored_preview_identity_invalid",
                "Stored inventory target fingerprint is inconsistent",
            )
    else:
        line_identity: dict[int, dict[str, Any]] = {}
        for document in manifest.get("documents") or []:
            for raw_line in dict(document or {}).get("lines") or []:
                line = dict(raw_line or {})
                nm_id = int(line.get("nm_id") or 0)
                line_identity.setdefault(
                    nm_id,
                    {
                        "nm_id": nm_id,
                        "our_sku": str(line.get("sku") or ""),
                        "nomenclature_name": str(line.get("nomenclature_name") or ""),
                        "barcode": str(line.get("barcode") or ""),
                        "barcodes": [str(line.get("barcode") or "")]
                        if str(line.get("barcode") or "")
                        else [],
                        "group_name": str(line.get("group_name") or ""),
                    },
                )
        targets = []
        for raw_item in manifest.get("per_sku") or []:
            item = dict(raw_item or {})
            nm_id = int(item.get("nm_id") or 0)
            identity = dict(line_identity.get(nm_id) or {})
            if not identity:
                identity = {
                    "nm_id": nm_id,
                    "our_sku": str(item.get("sku_comment") or nm_id),
                    "nomenclature_name": str(item.get("sku_comment") or ""),
                    "barcode": str(item.get("source_barcode") or ""),
                    "barcodes": [str(item.get("source_barcode") or "")]
                    if str(item.get("source_barcode") or "")
                    else [],
                    "group_name": "",
                }
            targets.append(
                {
                    "nm_id": nm_id,
                    "target_quantity": item.get("target_quantity"),
                    "source_nm_id": item.get("source_nm_id"),
                    "source_barcode": item.get("source_barcode"),
                    "identity_source": item.get("identity_source"),
                    "sku_comment": item.get("sku_comment"),
                    "row_no": item.get("source_row"),
                    "header_profile": str(dict(manifest.get("source") or {}).get("header_profile") or ""),
                    "identity": identity,
                }
            )
        intent = _build_target_intent(
            source_sha256=source_sha256,
            business_date=business_date,
            return_supply_ids=return_supply_ids,
            target_rows={int(item["nm_id"]): item for item in targets},
            nomenclature={int(item["nm_id"]): item["identity"] for item in targets},
            all_target_costs_validated=False,
        )
    _materialize_confirmed_target_intent(
        intent,
        source_sha256=source_sha256,
        business_date=business_date,
        return_supply_ids=return_supply_ids,
    )
    return intent


def _materialize_confirmed_target_intent(
    value: Mapping[str, Any],
    *,
    source_sha256: str,
    business_date: str,
    return_supply_ids: Iterable[str],
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    intent = _normalize_target_intent(value)
    expected_returns = sorted(
        {str(item).strip() for item in return_supply_ids if str(item).strip()}
    )
    if (
        intent["source_sha256"] != source_sha256
        or intent["business_date"] != business_date
        or intent["return_supply_ids"] != expected_returns
        or not intent["targets"]
    ):
        raise FfInventoryReconciliationError(
            "confirmed_target_intent_invalid",
            "Stored inventory target does not match its source/date identity",
        )
    target_rows: dict[int, dict[str, Any]] = {}
    nomenclature: dict[int, dict[str, Any]] = {}
    for item in intent["targets"]:
        nm_id = int(item["nm_id"])
        target_quantity = _decimal(item["target_quantity"])
        if (
            nm_id <= 0
            or nm_id in target_rows
            or target_quantity < ZERO
            or target_quantity != target_quantity.to_integral_value()
        ):
            raise FfInventoryReconciliationError(
                "confirmed_target_intent_invalid",
                "Stored inventory target contains an invalid SKU or quantity",
                details={"nm_id": nm_id, "target_quantity": str(target_quantity)},
            )
        target_rows[nm_id] = {
            "nm_id": nm_id,
            "source_nm_id": item.get("source_nm_id"),
            "source_barcode": str(item.get("source_barcode") or ""),
            "matched_barcode_role": str(item.get("matched_barcode_role") or ""),
            "identity_source": str(item.get("identity_source") or ""),
            "sku_comment": str(item.get("sku_comment") or ""),
            "target_quantity": target_quantity,
            "row_no": int(item.get("row_no") or 0),
            "header_profile": str(item.get("header_profile") or ""),
        }
        identity = dict(item.get("identity") or {})
        identity["nm_id"] = nm_id
        nomenclature[nm_id] = identity
    return target_rows, nomenclature


def _parse_target_workbook(
    source_bytes: bytes,
    *,
    business_date: str,
) -> list[dict[str, Any]]:
    rows = read_first_sheet_cells(source_bytes)
    actual_headers = tuple(
        str(cell.value or "").strip() for cell in (rows[0] if rows else [])
    )
    if actual_headers == REQUIRED_HEADERS:
        header_profile = "barcode_v2"
        barcode_index: int | None = 1
        comment_index = 2
        quantity_index = 3
        date_index = 4
    elif actual_headers == LEGACY_NM_ID_HEADERS:
        header_profile = "legacy_nm_id_v1"
        barcode_index = None
        comment_index = 1
        quantity_index = 2
        date_index = 3
    else:
        raise FfInventoryReconciliationError(
            "invalid_workbook_headers",
            "Manager workbook headers do not match the exact inventory contract",
            details={
                "expected": [REQUIRED_HEADERS, LEGACY_NM_ID_HEADERS],
                "actual": actual_headers,
            },
        )
    result: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for row_no, row in enumerate(rows[1:], start=2):
        if not any(cell.value not in (None, "") for cell in row):
            continue
        try:
            nm_id = _parse_optional_nm_id_cell(_cell_at(row, 0))
        except ValueError as exc:
            errors.append({"row": row_no, "code": str(exc), "field": "nmId"})
            continue
        try:
            barcode = (
                _parse_barcode_cell(_cell_at(row, barcode_index))
                if barcode_index is not None
                else ""
            )
        except ValueError as exc:
            errors.append({"row": row_no, "code": str(exc), "field": "Штрихкод"})
            continue
        try:
            quantity = _parse_target_quantity_cell(_cell_at(row, quantity_index))
        except ValueError as exc:
            errors.append({"row": row_no, "code": str(exc), "field": "Остаток ФФ"})
            continue
        try:
            row_date = _parse_business_date_cell(_cell_at(row, date_index))
        except ValueError as exc:
            errors.append({"row": row_no, "code": str(exc), "field": "Дата остатка"})
            continue
        if row_date != business_date:
            errors.append(
                {"row": row_no, "code": "business_date_mismatch", "actual": row_date}
            )
            continue
        result.append(
            {
                "source_nm_id": nm_id,
                "source_barcode": barcode,
                "sku_comment": str(
                    _cell_at(row, comment_index).value or ""
                ).strip(),
                "target_quantity": quantity,
                "row_no": row_no,
                "header_profile": header_profile,
            }
        )
    if errors or not result:
        raise FfInventoryReconciliationError(
            "invalid_workbook_rows",
            "Manager workbook contains invalid, duplicate or mismatched rows",
            details=errors,
        )
    return result


def _cell_at(row: list[XlsxCell], index: int | None) -> XlsxCell:
    if index is None or index < 0 or index >= len(row):
        return XlsxCell(
            value=None,
            kind="blank",
            raw_value="",
            cell_type="",
            style_index=0,
        )
    return row[index]


def _parse_optional_nm_id_cell(cell: XlsxCell) -> int | None:
    if cell.value in (None, ""):
        return None
    if cell.kind not in {"number", "text"}:
        raise ValueError("unsafe_nm_id_cell")
    token = cell.raw_value if cell.kind == "number" else str(cell.value or "").strip()
    if not re.fullmatch(r"[1-9][0-9]*", token):
        raise ValueError(
            "scientific_notation_nm_id"
            if re.search(r"[eE]", token)
            else "invalid_nm_id"
        )
    value = int(token)
    if value > 9_223_372_036_854_775_807:
        raise ValueError("unsafe_nm_id_cell")
    return value


def _parse_barcode_cell(cell: XlsxCell) -> str:
    if cell.value in (None, ""):
        return ""
    if cell.kind != "text":
        raise ValueError("barcode_must_be_text")
    try:
        return _normalize_barcode_text(str(cell.value))
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


def _parse_target_quantity_cell(cell: XlsxCell) -> Decimal:
    if cell.value in (None, "") or cell.kind not in {"number", "text"}:
        raise ValueError("invalid_target_quantity")
    token = cell.raw_value if cell.kind == "number" else str(cell.value or "").strip()
    if re.search(r"[eE]", token):
        raise ValueError("scientific_notation_target_quantity")
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)", token):
        raise ValueError("invalid_target_quantity")
    return Decimal(token)


def _parse_business_date_cell(cell: XlsxCell) -> str:
    if cell.value in (None, "") or cell.kind not in {"date", "text"}:
        raise ValueError("invalid_business_date_cell")
    value = str(cell.value or "").strip()
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("invalid_business_date_cell")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("invalid_business_date_cell") from exc
    return value


def _resolve_target_rows(
    rows: list[Mapping[str, Any]],
    *,
    active_catalog: Mapping[int, Mapping[str, Any]],
) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    barcode_candidates: dict[str, list[dict[str, Any]]] = {}
    for item in active_catalog.values():
        for barcode in item.get("barcodes") or []:
            candidates = barcode_candidates.setdefault(str(barcode), [])
            if not any(
                int(candidate["nm_id"]) == int(item["nm_id"])
                for candidate in candidates
            ):
                candidates.append(dict(item))

    result: dict[int, dict[str, Any]] = {}
    blockers: list[dict[str, Any]] = []
    for row in rows:
        row_no = int(row.get("row_no") or 0)
        source_nm_id = row.get("source_nm_id")
        source_barcode = str(row.get("source_barcode") or "")
        nm_candidate = (
            active_catalog.get(int(source_nm_id))
            if source_nm_id is not None
            else None
        )
        by_barcode = barcode_candidates.get(source_barcode, []) if source_barcode else []

        row_blocked = False
        if source_nm_id is None and not source_barcode:
            blockers.append({"row": row_no, "code": "empty_inventory_identity"})
            row_blocked = True
        if source_nm_id is not None and nm_candidate is None:
            blockers.append(
                {"row": row_no, "code": "unknown_nm_id", "nm_id": source_nm_id}
            )
            row_blocked = True
        if source_barcode and not by_barcode:
            blockers.append(
                {"row": row_no, "code": "unknown_barcode", "barcode": source_barcode}
            )
            row_blocked = True
        if source_barcode and len(by_barcode) > 1:
            blockers.append(
                {
                    "row": row_no,
                    "code": "ambiguous_barcode",
                    "barcode": source_barcode,
                    "nm_ids": sorted(int(item["nm_id"]) for item in by_barcode),
                }
            )
            row_blocked = True
        if row_blocked:
            continue

        barcode_candidate = by_barcode[0] if by_barcode else None
        if (
            nm_candidate is not None
            and barcode_candidate is not None
            and int(nm_candidate["nm_id"]) != int(barcode_candidate["nm_id"])
        ):
            blockers.append(
                {
                    "row": row_no,
                    "code": "nm_id_barcode_conflict",
                    "nm_id": int(nm_candidate["nm_id"]),
                    "barcode": source_barcode,
                    "barcode_nm_id": int(barcode_candidate["nm_id"]),
                }
            )
            continue

        resolved = nm_candidate or barcode_candidate
        if resolved is None:
            continue
        resolved_nm_id = int(resolved["nm_id"])
        if resolved_nm_id in result:
            blockers.append(
                {
                    "row": row_no,
                    "code": "duplicate_resolved_sku",
                    "nm_id": resolved_nm_id,
                    "first_row": int(result[resolved_nm_id]["row_no"]),
                }
            )
            continue
        identity_source = (
            "nm_id+barcode"
            if source_nm_id is not None and source_barcode
            else "nm_id"
            if source_nm_id is not None
            else "barcode"
        )
        result[resolved_nm_id] = {
            "nm_id": resolved_nm_id,
            "source_nm_id": source_nm_id,
            "source_barcode": source_barcode,
            "matched_barcode_role": (
                "primary"
                if source_barcode and source_barcode == str(resolved.get("barcode") or "")
                else "additional"
                if source_barcode
                else ""
            ),
            "identity_source": identity_source,
            "sku_comment": str(row.get("sku_comment") or ""),
            "target_quantity": _decimal(row.get("target_quantity")),
            "row_no": row_no,
            "header_profile": str(row.get("header_profile") or ""),
        }
    return result, blockers


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
    requested = {int(item) for item in nm_ids}
    catalog, blockers = _complete_active_nomenclature(conn)
    current = set(catalog)
    if requested != current:
        blockers.append(
            {
                "code": "active_nomenclature_target_changed",
                "missing_nm_ids": sorted(current - requested),
                "unexpected_nm_ids": sorted(requested - current),
            }
        )
    result: dict[int, dict[str, Any]] = {}
    for nm_id in sorted(requested):
        item = catalog.get(nm_id)
        if item is None:
            blockers.append(
                {
                    "code": "nomenclature_unmatched_or_ambiguous",
                    "nm_id": nm_id,
                    "active_candidate_count": 0,
                }
            )
        else:
            result[nm_id] = dict(item)
    return result, blockers


def _complete_active_nomenclature(
    conn: sqlite3.Connection,
) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT item_id,nm_id,our_sku,nomenclature_name,barcode,barcodes_json,
               product_type AS group_name,updated_at
        FROM sheet_vitrina_v1_nomenclature_items
        WHERE is_active=1 AND is_hidden=0
        ORDER BY nm_id,item_id
        """
    ).fetchall()
    grouped: dict[int, list[dict[str, Any]]] = {}
    blockers: list[dict[str, Any]] = []
    for row in rows:
        if row["nm_id"] is None or int(row["nm_id"] or 0) <= 0:
            blockers.append(
                {
                    "code": "active_nomenclature_identity_missing",
                    "item_id": str(row["item_id"] or ""),
                }
            )
            continue
        item = dict(row)
        try:
            item["barcode"], item["barcodes"] = _canonical_nomenclature_barcodes(
                item
            )
        except ValueError as exc:
            blockers.append(
                {
                    "code": "active_nomenclature_barcode_invalid",
                    "item_id": str(row["item_id"] or ""),
                    "nm_id": int(row["nm_id"]),
                    "reason": str(exc),
                }
            )
            item["barcode"] = ""
            item["barcodes"] = []
        grouped.setdefault(int(row["nm_id"]), []).append(item)
    result: dict[int, dict[str, Any]] = {}
    for nm_id, items in sorted(grouped.items()):
        if len(items) != 1:
            blockers.append(
                {
                    "code": "active_nomenclature_identity_ambiguous",
                    "nm_id": nm_id,
                    "active_candidate_count": len(items),
                }
            )
        else:
            result[nm_id] = items[0]
    return result, blockers


def _canonical_nomenclature_barcodes(
    item: Mapping[str, Any],
) -> tuple[str, list[str]]:
    primary_raw = item.get("barcode")
    raw_json = str(item.get("barcodes_json") or "[]")
    try:
        decoded = json.loads(raw_json)
    except (TypeError, ValueError) as exc:
        raise ValueError("barcodes_json_invalid") from exc
    if not isinstance(decoded, list):
        raise ValueError("barcodes_json_must_be_list")
    raw_values = [primary_raw, *decoded]
    normalized: list[str] = []
    for raw in raw_values:
        if raw in (None, ""):
            continue
        if not isinstance(raw, str):
            raise ValueError("canonical_barcode_must_be_text")
        barcode = _normalize_barcode_text(raw)
        if barcode and barcode not in normalized:
            normalized.append(barcode)
    primary = _normalize_barcode_text(str(primary_raw)) if primary_raw not in (None, "") else ""
    return primary, normalized


def _normalize_barcode_text(value: str) -> str:
    text = str(value).replace("\u00a0", "").replace("\u202f", "")
    text = re.sub(r"\s+", "", text)
    if text.startswith("'"):
        text = text[1:]
    if not text:
        return ""
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)[eE][+-]?\d+", text):
        raise ValueError("scientific_notation_barcode")
    if not text.isascii() or not text.isdigit():
        raise ValueError("malformed_barcode")
    return text


def _template_barcode_blockers(
    catalog: Mapping[int, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_barcode: dict[str, set[int]] = {}
    for nm_id, item in catalog.items():
        for barcode in item.get("barcodes") or []:
            by_barcode.setdefault(str(barcode), set()).add(int(nm_id))
    blockers: list[dict[str, Any]] = []
    for nm_id, item in catalog.items():
        primary = str(item.get("barcode") or "")
        if not primary:
            continue
        owners = sorted(by_barcode.get(primary, set()))
        if owners != [int(nm_id)]:
            blockers.append(
                {
                    "code": "active_nomenclature_primary_barcode_ambiguous",
                    "barcode": primary,
                    "nm_ids": owners,
                }
            )
    return blockers


def _ff_balances_as_of(
    conn: sqlite3.Connection,
    nm_ids: Iterable[int],
    *,
    business_date: str,
) -> dict[int, Decimal]:
    normalized = sorted({int(item) for item in nm_ids})
    if not normalized:
        return {}
    placeholders = ",".join("?" for _ in normalized)
    rows = conn.execute(
        f"""
        SELECT line.nm_id,SUM(line.quantity_delta) AS quantity
        FROM sheet_vitrina_v1_ff_stock_operation_lines line
        JOIN sheet_vitrina_v1_ff_stock_operations operation
          ON operation.operation_id=line.operation_id
        WHERE line.nm_id IN ({placeholders})
          AND COALESCE(NULLIF(operation.business_effective_date,''),
                       substr(operation.created_at,1,10))<=?
        GROUP BY line.nm_id
        """,
        (*normalized, business_date),
    ).fetchall()
    result = {int(row["nm_id"]): _decimal(row["quantity"]) for row in rows}
    return {nm_id: result.get(nm_id, ZERO) for nm_id in normalized}


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
